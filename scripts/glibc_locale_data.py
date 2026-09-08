#!/usr/bin/env python3
"""
Shared helpers for reading glibc locale data out of a git clone.

Every script in this directory needs the same handful of primitives: locate the
glibc clone, read blobs at a tag, find the LC_COLLATE block, and walk the
`copy` graph. They used to each spawn one `git show` per locale file (~350
subprocesses) with a 30s timeout and an ignored return code, so a slow blob
fetch on a `--filter=blob:none` clone silently dropped files from the analysis
and the locale was reported unaffected. Everything here fails loudly instead,
and blobs are read in a single `git cat-file --batch` stream.

Also runnable directly, for the shell script:
  python3 glibc_locale_data.py fanin <tag> [changed_files.txt]
  python3 glibc_locale_data.py provenance <tag> [<tag> ...]
  python3 glibc_locale_data.py corpus <tag> [<tag> ...]
  python3 glibc_locale_data.py order [--allow-reverse] [--quiet] <old_tag> <new_tag>
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import textwrap

LOCALES_DIR = 'localedata/locales'

# Fewer files than this under localedata/locales/ is not a glibc locale corpus.
# The five pinned tags carry 286 (2.12), 312 (2.17), 353 (2.28), 355 (2.34) and
# 366 (2.39); the three measured RHEL nodes 355, 356 and 366. The node-reading
# modes have refused a directory below this floor since they were written; the
# tag modes did not check at all, so a tag whose tree lacks localedata/locales/
# -- a restructured checkout, a tag from before the directory existed -- produced
# "0 files changed", "No locale uses ellipsis ranges here" and exit 0.
MIN_LOCALE_FILES = 200

# Prepended to every git invocation. `git diff` obeys the user's config, and
# four settings change the text this tool parses. Measured with git 2.50 on the
# 2.34..2.39 pair: diff.noprefix=true drops the a/ b/ that the file-header
# regex in filter_lc_collate_changes.py expects (the step then dies, correctly,
# with "the diff does not cover"); diff.renameLimit=1 turns the one rename into
# delete+add with only a warning on stderr, so the added file lands in "not
# analysed" -- silently; diff.renames=false does the same to step 1, whose
# published count goes from 318 to 319; color.ui=always writes escape codes
# into the pipe. -c on the command line outranks every configuration source.
GIT_CONFIG_OVERRIDES = ['-c', 'color.ui=false',
                        '-c', 'diff.noprefix=false',
                        '-c', 'diff.mnemonicPrefix=false',
                        '-c', 'diff.renames=true',
                        '-c', 'diff.renameLimit=0']

# Does a line use an ellipsis range? A range like `<UAC00>`/`..`/`<UD7A3>` is
# expanded algorithmically by localedef at build time, so the weights are NOT in
# the locale source and a source diff cannot prove the locale's order is
# unchanged.
#
# locale/programs/linereader.c accepts exactly five ellipsis tokens, having
# already consumed one `.` before it compares the rest: `..`, `...`, `....`,
# `..(2)..` and `....(2)....`. The count is always a literal 2, never an
# arbitrary number. That list is worth keeping written down -- it is the part
# that took reading glibc's tokeniser to learn.
#
# The pattern does not enumerate them, because every one is a run of two or more
# dots and this only has to answer yes or no. Spelling out the five alternatives
# also silently excluded anything longer: a run of five dots, which glibc reads
# as `....` plus a stray `.`, matched no alternative and fell through the
# dot-boundary guards as "no ellipsis here". Measured identical to the old
# pattern across glibc 2.28, 2.34, 2.39 and 2.42 -- every dot run in the corpus
# is length 2, so the alternatives were doing no work.
#
# It must NOT be anchored to the start of the line. The dominant form in glibc
# is inline -- `collating-symbol <SAC00>..<SD7A3>` in iso14651_t1_common carries
# the constructed Hangul and Han weights -- and anchoring it missed every one of
# them, which cleared zh_CN and its pinyin siblings.
ELLIPSIS_RE = re.compile(r'(?<!\.)\.{2,}')

# Locale files declare their comment character; every one in glibc uses `%`.
# Prose in a comment ("2.28..2.34", "and so on ...") is not a range, so
# comments are stripped before looking for one.
_COMMENT_CHAR_RE = re.compile(r'^\s*comment_char\s+(\S)', re.M)

# glibc's own keyword for "this locale sorts by code point, full stop"
# (locale/programs/ld-collate.c). Present in upstream's C from 2.35 on, and the
# thing that makes C.UTF-8 immovable from that release forward. Matched as a
# whole token because the file that declares it also DISCUSSES it in a comment.
# `<` and `>` are in the guards because glibc's lexer reads `<name>` as a
# collating symbol and never as this keyword, while the bare word without them
# reads as the keyword: `collating-symbol <codepoint_collation>` used to
# classify a file as byte-order-by-construction, which is the one verdict here
# that clears a locale outright.
_CODEPOINT_RE = re.compile(
    r'(?<![A-Za-z0-9_<])codepoint_collation(?![A-Za-z0-9_>])')

_COPY_RE = re.compile(r'^\s*copy\s+"([^"]+)"', re.M)

# glibc's symbolic notation for a character. localedef decodes it wherever a
# string is read (locale/programs/linereader.c, get_string), so
# `copy "<U0069><U0073><U006F>..."` names iso14651_t1 as surely as spelling it
# out -- and ky_KG and uk_UA spell it exactly that way at glibc-2.12 and 2.17.
# Left undecoded, those two dropped out of the iso14651_t1 closure and the
# floor pair reported them as unaffected.
_UCHAR_RE = re.compile(r'<U([0-9A-Fa-f]{4,8})>')


def die(msg, code=2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _is_glibc_clone(path):
    """Is `path` a git clone of glibc?

    Asked of git, not of the filesystem. audit-locale-diff.sh clones with
    `--no-checkout`, so the clone has no working tree at all and a
    `localedata/locales` directory never appears on disk -- every step used to
    die with "run audit-locale-diff.sh first" on the very run that had just
    cloned it.
    """
    if not os.path.isdir(path):
        return False
    if run_git(['rev-parse', '--git-dir'], path, allow_fail=True).returncode != 0:
        return False
    if os.path.isdir(os.path.join(path, LOCALES_DIR)):
        return True
    # No working tree: ask the object store instead. `--filter=blob:none`
    # keeps every tree, so this needs no network.
    return run_git(['cat-file', '-e', f'HEAD:{LOCALES_DIR}'],
                   path, allow_fail=True).returncode == 0


def find_repo(explicit=None):
    """Locate the glibc clone.

    Accepts being run from the clone itself, from scripts/, or from the repo
    root, so that a wrong working directory can no longer produce an empty
    diff that later gets mistaken for "nothing changed".
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates += [os.getcwd(),
                       os.path.join(os.getcwd(), 'glibc'),
                       os.path.join(here, 'glibc'),
                       os.path.join(here, os.pardir, 'glibc')]
    for c in candidates:
        if _is_glibc_clone(c):
            return os.path.abspath(c)
    die("could not find the glibc clone (no git repository containing "
        "localedata/locales among the paths tried). Run "
        "scripts/audit-locale-diff.sh first, or pass "
        "--repo <path-to-glibc-clone>.")


def run_git(args, repo, allow_fail=False):
    """Run git, aborting on failure unless explicitly allowed.

    `allow_fail=True` is for EXISTENCE PROBES only -- where the caller inspects
    `returncode` and a non-zero exit is itself the answer (`_is_glibc_clone`,
    `check_refs`). Never use it to READ CONTENT. git prints nothing on stdout
    when it fails, so a suppressed failure is indistinguishable from "there is
    nothing here", and in this tool "nothing here" means "this file did not
    change" -- a clean verdict, produced by not having looked. That is what
    `report_file` and `check_paths` used to do, on a --filter=blob:none clone
    whose fetches really do fail:

        fatal: could not fetch <oid> from promisor remote
    """
    # GIT_DIFF_OPTS is applied AFTER the command line, so `-U3` on the argv
    # does not win: measured, `GIT_DIFF_OPTS=-u0 git diff -U3` returns zero
    # context lines, and step 5's comment tracking -- which is what reads those
    # lines -- then hides a comment that closes on one. It is dropped rather
    # than overridden because there is no flag that outranks it.
    env = {k: v for k, v in os.environ.items() if k != 'GIT_DIFF_OPTS'}
    p = subprocess.run(['git', *GIT_CONFIG_OVERRIDES, *args], cwd=repo,
                       capture_output=True, env=env)
    if p.returncode != 0 and not allow_fail:
        die(f"`git {' '.join(args)}` failed in {repo}:\n"
            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    return p


def check_refs(repo, *refs):
    """Verify every ref resolves, fetching tags once if some are missing.

    A stale clone from an earlier run simply does not have newer tags, and
    `git diff` against a missing tag is a confusing failure at best.
    """
    missing = [r for r in refs
               if run_git(['rev-parse', '--verify', '--quiet', f'{r}^{{commit}}'],
                          repo, allow_fail=True).returncode != 0]
    if not missing:
        return
    print(f"note: {', '.join(missing)} not present in {repo}, fetching tags...",
          file=sys.stderr)
    run_git(['fetch', '--tags', '--quiet'], repo)
    still = [r for r in missing
             if run_git(['rev-parse', '--verify', '--quiet', f'{r}^{{commit}}'],
                        repo, allow_fail=True).returncode != 0]
    if still:
        die(f"unknown git ref(s) after fetching tags: {', '.join(still)}")


def warn(text):
    """The `!!` block shape audit.sh collects and repeats at the bottom.

    One copy, in the shared module, because two copies of a formatter drift and
    this repository has already paid for that: the suite's `flat()` exists
    because output wrapped at 78 columns made a negative assertion vacuous, and
    the wrapper's warnings block matches `^!!` followed by three-space
    continuation lines. `diff_distro_locales.warn` is an alias for this.
    """
    print(textwrap.fill(text, width=78,
                        initial_indent='!! ', subsequent_indent='   '))


def _is_ancestor(repo, maybe_ancestor, descendant):
    """Does git say the first commit is an ancestor of the second?

    `merge-base --is-ancestor` exits 0 for yes, 1 for no, and anything else is
    git failing to answer -- which must not read as "no". That third case is
    why this is not written as `== 0`: allow_fail is for existence probes, and
    everywhere else in this tool a swallowed git failure has meant a clean
    verdict produced by not having looked.
    """
    p = run_git(['merge-base', '--is-ancestor', maybe_ancestor, descendant],
                repo, allow_fail=True)
    if p.returncode not in (0, 1):
        die(f"`git merge-base --is-ancestor {maybe_ancestor[:12]} "
            f"{descendant[:12]}` exited {p.returncode} in {repo}:\n"
            f"       {p.stderr.decode('utf-8', 'replace').strip()}\n"
            f"       That is not an answer. Refusing to treat a failed probe "
            f"as 'the pair is in order'.")
    return p.returncode == 0


_GLIBC_TAG_RE = re.compile(r'^glibc-(\d+)\.(\d+)')


def nearest_glibc_tag(repo, rev):
    """The newest glibc tag this commit descends from, and which RELEASE it is.

    Returns three different things, because three different facts reach the
    caller and only one of them can order a pair:

      (tag, (major, minor))  -- the tag, and the release it belongs to
      (tag, None)            -- a tag whose NAME this tool does not read as a
                                release, `glibc-2x-tps` in shape. No tag in
                                the mirror looks like that today: all 125 that
                                the `describe` glob matches parse, the oddly
                                spelt ones (`glibc-2.16-tps`,
                                `glibc-2.16-ports-merge`, `glibc-2.0.5b`)
                                included -- they read as 2.16 and 2.0, which
                                is what they are. The state is kept because a
                                name nobody parsed is not a release, and only
                                a fabricated tag exercises it.
      None                   -- `describe` answered no name

    `git describe --tags --abbrev=0 --match 'glibc-[0-9]*'` asks it: the tag
    itself for a release commit, `glibc-2.28` for anything on
    `release/2.28/master`, `glibc-2.28.9000` for a master commit just after
    2.28. That is what orders two commits on different branches, where
    ancestry cannot.

    **The release is the first two components, and nothing after them.** What
    follows is either a point release (`glibc-2.12.2`) or a development
    snapshot -- `glibc-2.28.9000`, the tag one commit after `glibc-2.28` that
    opens master for 2.29, and `glibc-2.17.90` in the older style. Both say
    where inside or after a release a commit sits, and neither can order two
    commits on DIFFERENT lines off that release: master after 2.12 describes as
    `glibc-2.12` while `release/2.12/master`'s tip describes as `glibc-2.12.2`,
    and reading that third component as a version ranked the branch above
    master -- an unearned `forward` one way and a false refusal the other,
    measured on the pinned clone. Between two commits on ONE line ancestry has
    already answered, before this is asked.

    Compared as NUMBERS, never as text: `glibc-2.4` is a much older release
    than `glibc-2.34`, and every string comparison gets that backwards.

    A tag reached but not read, and no name at all, are kept apart because the
    caller prints which one happened -- "no glibc tag behind it" over a ref
    whose own name is a tag would be a false sentence. Neither ever reads as
    "the pair is in order".

    What the None branch does NOT claim is a cause. `git describe` exits 128
    for several -- a commit no tag describes, a glob that matches nothing, a
    rev this clone cannot read -- and what it prints for each is git's own
    prose, which differs by cause and by git build. Measured on the pinned
    clone with git 2.50.1: `No names found, cannot describe anything.` for the
    glob that matches nothing, `<rev> is neither a commit nor blob` for a rev
    the clone has no object for; and in a repository where NO tag matches the
    glob, git checks the empty name set first and every cause collapses into
    the first message. None of it is parsed: the phrase says what describe
    answered. `rev-parse --verify` upstream removes the unreadable-rev cause
    for `pair_order`'s own calls; a clone that never fetched tags can still
    reach the glob case, and lands on undetermined with neither side named,
    which is the conservative direction.
    """
    p = run_git(['describe', '--tags', '--abbrev=0', '--match',
                 'glibc-[0-9]*', rev], repo, allow_fail=True)
    if p.returncode != 0:
        return None
    name = p.stdout.decode('utf-8', 'replace').strip()
    if not name:
        return None
    m = _GLIBC_TAG_RE.match(name)
    if not m:
        return name, None
    return name, (int(m.group(1)), int(m.group(2)))


def lineage_phrase(ref, found):
    """How pair_order says what it learnt about one side's lineage."""
    if not found:
        return f'git describe found no glibc tag behind {ref}'
    name, release = found
    if release is None:
        return (f'the newest glibc tag behind {ref} is {name}, whose name this '
                f'tool does not read as a release')
    return (f'the newest glibc tag behind {ref} is {name} '
            f'(release {release[0]}.{release[1]})')


def pair_order(repo, old, new):
    """Which direction this pair runs in. Returns (status, detail).

    status is one of:
      'same'         -- both refs resolve to ONE commit, however each is spelt
      'forward'      -- new is newer than old: what every step assumes
      'reversed'     -- new is the OLDER of the two
      'undetermined' -- two commits nothing here can order. Named rather than
                        folded into 'forward', because "could not tell" and
                        "in order" are different facts and only one of them is
                        a clean result.

    A status and not a bool because the four cases are treated differently and
    two of them are not errors -- the same reason verify_tag() returns one.

    The questions, in the order they are asked:

    1. Do both refs resolve to one commit? `glibc-2.39` and
       `ef321e23c20eebc6d6fb4044425c00e6df27b05f` are one commit spelt two
       ways, and the string comparison this replaces let that pair through as
       if it were two versions.
    2. Is one an ancestor of the other? That is git's own answer about which
       came first, and it needs no heuristic. It settles every pair of release
       tags -- 2.12..2.17, 2.28..2.34, 2.34..2.39 -- and also a tag against a
       later commit on its own release branch.
    3. Divergent commits are ordered by the RELEASE each one descends from
       (nearest_glibc_tag above): a commit on `release/2.28/master` is a 2.28,
       whatever its date. Only the release counts, never a point-release or
       snapshot suffix, so two lines off one release stay unordered instead of
       being ranked by a `.2` or a `.9000`.
    4. Anything left is undetermined, and says so.

    Commit dates decide nothing, and are not read. They were this helper's
    first signal, and glibc's release branches are the reason they are not:
    `origin/release/2.28/master` carries commits dated years AFTER
    `glibc-2.34`, so "the newer commit date wins" declared
    `glibc-2.34 -> a 2.28 backport commit` a forward pair -- exactly the
    reversed run this guard exists to refuse -- and refused the same pair given
    in the correct order. Measured on the pinned clone before this shipped.

    Why the question is asked at all: nothing here used to compare the order,
    and a reversed pair runs to the end at exit 0 with a plausible summary --
    step 2 reporting the same two files that touch LC_COLLATE, step 5 a
    substantive hunk count, no `!!` anywhere. What it hides: step 4 scans the
    tag it is handed, so reversed it scans the older one and the locales added
    in the newer tag (ckb_IQ and mnw_MM, both `copy "iso14651_t1"` at
    glibc-2.34, in no tag before it) drop out of the exposed set; step 2 swaps
    the reassuring "Added ... not analysed" for the noisy "Deleted ... any
    index using one of these will fail", so a locale DELETED in the real
    upgrade reads as a harmless addition; and step 3 closes over the wrong
    tag's copy graph.
    """
    shas = []
    for ref in (old, new):
        p = run_git(['rev-parse', '--verify', f'{ref}^{{commit}}'], repo)
        shas.append(p.stdout.decode('utf-8', 'replace').strip())
    if shas[0] == shas[1]:
        return 'same', f'{old} and {new} are one commit, {shas[0][:12]}'

    pair = f'the new tag {new} against the old tag {old}'
    if _is_ancestor(repo, shas[0], shas[1]):
        return 'forward', f'{pair}: {old} is an ancestor of {new}'
    if _is_ancestor(repo, shas[1], shas[0]):
        return 'reversed', f'{pair}: {new} is an ancestor of {old}'

    v_old = nearest_glibc_tag(repo, shas[0])
    v_new = nearest_glibc_tag(repo, shas[1])
    described = (f'{lineage_phrase(old, v_old)}, and '
                 f'{lineage_phrase(new, v_new)}')
    r_old = v_old[1] if v_old else None
    r_new = v_new[1] if v_new else None
    if r_old and r_new and r_old != r_new:
        detail = (f'{pair}: they are on different branches, and {described}')
        return ('reversed' if r_new < r_old else 'forward'), detail
    return 'undetermined', (
        f'{pair}: neither is an ancestor of the other, and {described}')


def require_pair_order(repo, old, new, allow_reverse=False):
    """Refuse a reversed pair -- or say out loud that one was allowed.

    Deciding what the pair IS (pair_order) and deciding what to do about it are
    different mistakes, so they are different functions with a test on each.

    Silent only on 'forward'. It used to be silent on 'same' as well, on the
    grounds that audit.sh prints a block of its own for that case -- which
    left a hand-run `diff_collation_code.py <tag> <tag>` ending in "No
    substantive collation code change", rc 0, no `!!`: a clean verdict over a
    comparison that never happened. The notice lives here now, in one copy for
    all four entry points, so under the wrapper every step that takes the pair
    prints it into its own log -- measured, steps 1, 2 and 5 -- and the
    summary's warnings block repeats it once.
    """
    status, detail = pair_order(repo, old, new)
    if status == 'same':
        warn(f"{old} and {new} are the same commit. Steps 1-5 compare "
             f"upstream source against itself, so they can only report "
             f"'nothing changed' -- which for an intra-major upgrade "
             f"(RHEL 8.1 -> 8.2, say) says nothing at all. The distro's own "
             f"builds are where such a change lives: supply both "
             f"--*-locales-dir to audit.sh, and run sql/c_utf8_probe.sql. "
             f"C.UTF-8's order moved in glibc-2.28-93.el8 with the upstream "
             f"tag unchanged. See docs/limitations.md.")
    elif status == 'reversed':
        if not allow_reverse:
            die(f"{detail}.\n"
                f"       This pair is REVERSED. Every step takes <old_tag> "
                f"<new_tag>, and reversed\n"
                f"       they all run to the end and print a plausible clean "
                f"result: step 4 scans\n"
                f"       the older tag, step 2 reports a deleted locale as a "
                f"harmless addition, and\n"
                f"       step 3 closes over the wrong copy graph. Swap the "
                f"arguments, or pass\n"
                f"       --allow-reverse if this is deliberate.")
        warn(f"REVERSED PAIR, allowed on request: {detail}. Every finding "
             f"below has old and new the other way round -- what reads as "
             f"added was deleted in the real upgrade, and step 4 scanned the "
             f"older tag. Not an audit of an upgrade.")
    elif status == 'undetermined':
        warn(f"The DIRECTION of this pair could not be established: {detail}. "
             f"Nothing below is wrong on that account, but neither is it "
             f"checked: if the two arguments are the wrong way round, every "
             f"step still runs and still prints a plausible clean result. "
             f"Confirm which build is the older one.")
    return status


def verify_tag(repo, tag):
    """Check a tag's GPG signature. Returns (status, detail).

    status is one of:
      'good'      -- signature verified against a key in the local keyring
      'bad'       -- signature present and INVALID. Stop.
      'no-key'    -- signed, but the signing key is not in the keyring
      'no-gpg'    -- gpg is not installed, so nothing was checked
      'unsigned'  -- the tag carries no signature
      'not-a-tag' -- a lightweight tag or a commit; nothing to verify

    Why this exists: this audit reads glibc from a THIRD-PARTY MIRROR
    (github.com/bminor/glibc), and a git tag is a mutable pointer. Nothing else
    in the tool checks that the source it diffed is the source the glibc
    maintainers released. The release tags are signed; this is the only
    mechanism available that answers that question.

    Why it returns a status instead of a bool: `git verify-tag` exits 1 for
    "gpg is missing", "I do not have that key" and "the signature is forged"
    alike. Collapsing those loses the only distinction that matters -- the
    first two mean "unchecked", the third means "stop". A tool that reports
    "unverified" identically to "invalid" trains people to ignore both.
    """
    kind = run_git(['cat-file', '-t', tag], repo, allow_fail=True)
    if kind.returncode != 0:
        return 'not-a-tag', f'{tag} does not resolve'
    if kind.stdout.strip() != b'tag':
        return 'not-a-tag', 'lightweight tag or commit -- no signature to check'

    body = run_git(['cat-file', 'tag', tag], repo, allow_fail=True).stdout
    if b'-----BEGIN PGP SIGNATURE-----' not in body:
        return 'unsigned', 'the tag object carries no signature'

    signer = ''
    for line in body.decode('utf-8', 'replace').split('\n'):
        if line.startswith('tagger '):
            signer = line[len('tagger '):].rsplit(' ', 2)[0]
            break

    p = run_git(['verify-tag', '--raw', tag], repo, allow_fail=True)
    err = p.stderr.decode('utf-8', 'replace')
    if 'cannot run gpg' in err or 'gpg: not found' in err:
        return 'no-gpg', 'gpg is not installed; the signature was NOT checked'
    if 'NO_PUBKEY' in err or 'errsig' in err.lower():
        return 'no-key', f'signed by {signer}, whose key is not in your keyring'
    if 'BADSIG' in err or 'ERRSIG' in err:
        return 'bad', f'INVALID signature on {tag}'
    if p.returncode == 0 or 'GOODSIG' in err or 'VALIDSIG' in err:
        return 'good', f'signed by {signer}'
    return 'no-key', f'signed by {signer}; could not verify ({err.strip()[:80]})'


def report_tag_provenance(repo, *tags):
    """Print what is known about where each tag's content came from.

    Aborts on a bad signature. Everything else is reported and the audit
    continues -- an unchecked signature is a gap in evidence, not proof of
    tampering, and refusing to run without gpg would make the tool unusable on
    a stock container for no gain in truth.
    """
    print("Tag provenance (this audit reads a third-party mirror; a tag is a "
          "mutable pointer):")
    worst = 'good'
    for tag in tags:
        status, detail = verify_tag(repo, tag)
        sha = run_git(['rev-parse', f'{tag}^{{commit}}'], repo,
                      allow_fail=True).stdout.decode().strip()
        mark = {'good': 'OK      ', 'bad': 'INVALID ', 'no-key': 'UNCHECKED',
                'no-gpg': 'UNCHECKED', 'unsigned': 'UNSIGNED',
                'not-a-tag': 'UNSIGNED'}[status]
        print(f"  {mark} {tag} = {sha[:12]}  {detail}")
        if status == 'bad':
            worst = 'bad'
        elif status != 'good' and worst == 'good':
            worst = status
    if worst == 'bad':
        die("a tag's signature is INVALID. The source this would audit is not "
            "what the glibc maintainers released. Refusing to continue.")
    if worst != 'good':
        print("  NOTE: signatures were not checked. The commit ids above are "
              "what was actually read;")
        print("        compare them against a source you trust "
              "(sourceware.org/git/glibc.git) if it matters.")


def read_blobs(repo, tag, paths):
    """Read many blobs at `tag` in one pass.

    Returns (contents, missing): a {path: text} map and the set of paths that
    do not exist at that tag. Distinguishing "missing" from "failed" is what
    lets callers report a locale added in the new tag instead of silently
    skipping it.
    """
    paths = list(paths)
    if not paths:
        return {}, set()
    req = ''.join(f'{tag}:{p}\n' for p in paths).encode()
    p = subprocess.run(['git', 'cat-file', '--batch'], cwd=repo,
                       input=req, capture_output=True)
    if p.returncode != 0:
        die(f"`git cat-file --batch` failed in {repo}:\n"
            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    out = p.stdout
    contents, missing, pos = {}, set(), 0
    for path in paths:
        nl = out.find(b'\n', pos)
        if nl < 0:
            die(f"truncated `git cat-file --batch` output while reading "
                f"{tag}:{path} (read {len(contents)} of {len(paths)} blobs)")
        header = out[pos:nl].decode('utf-8', 'replace')
        pos = nl + 1
        # "<oid> missing" / "<oid> ambiguous" carry no body.
        if header.endswith(' missing') or header.endswith(' ambiguous'):
            missing.add(path)
            continue
        try:
            size = int(header.split()[2])
        except (IndexError, ValueError):
            die(f"unparsable `git cat-file --batch` header for {tag}:{path}: "
                f"{header!r}")
        contents[path] = out[pos:pos + size].decode('utf-8', 'replace')
        pos += size + 1  # body plus its trailing newline
    return contents, missing


def read_blobs_strict(repo, tag, paths, what):
    """read_blobs, but every path MUST exist at `tag`; otherwise abort.

    For callers whose path list came from the tree at this same tag, so a
    missing blob is not "that file isn't there" but "I could not read what I
    was just told exists". Dropping those silently shrinks whatever is built
    from them -- the `copy` graph, the include walk -- and a smaller graph
    yields a shorter affected-locale list, which is the reassuring direction.

    `what` names what was being read, so the error says which analysis is
    incomplete rather than just listing paths.
    """
    contents, missing = read_blobs(repo, tag, paths)
    if missing:
        die(f"{len(missing)} of {len(paths)} file(s) listed at {tag} could not "
            f"be read while building {what}: "
            f"{', '.join(sorted(missing)[:5])}"
            f"{', ...' if len(missing) > 5 else ''}.\n"
            f"       These came from the tree at {tag}, so they exist -- the "
            f"read failed. On a --filter=blob:none clone that usually means a "
            f"fetch could not happen. Re-run with the promisor reachable.")
    return contents


def list_locale_files(repo, tag):
    """Every file under localedata/locales/ at `tag`, as repo-relative paths.

    Aborts below MIN_LOCALE_FILES. Every caller builds a result from this list
    -- the copy graph, the ellipsis scan, step 2's diff pathspec -- and a
    result built from nothing reads as "nothing changed", which is the
    reassuring direction. The node-reading modes had this guard from the
    start; the tag modes did not.
    """
    out = run_git(['ls-tree', '-r', '--name-only', tag, '--', LOCALES_DIR + '/'],
                  repo).stdout.decode('utf-8', 'replace')
    paths = [ln for ln in out.splitlines() if ln.strip()]
    if len(paths) < MIN_LOCALE_FILES:
        die(f"only {len(paths)} file(s) under {LOCALES_DIR}/ at {tag}, below "
            f"the floor of {MIN_LOCALE_FILES}. That is not a glibc locale "
            f"corpus -- a restructured tree, or a tag from before the "
            f"directory existed. Refusing to report: an empty corpus reads "
            f"as 'nothing changed' and 'no locale uses ellipsis ranges', "
            f"which is indistinguishable from a clean run.")
    return paths


def collate_block(text):
    """The body of the LC_COLLATE...END LC_COLLATE block, or None.

    Built on collate_bounds, which is line-based, rather than on a regex
    requiring a newline before LC_COLLATE. That regex returned None for a file
    opening with LC_COLLATE at byte 0, and glibc 2.23 and earlier write the
    three master templates -- iso14651_t1, iso14651_t1_common and
    iso14651_t1_pinyin, the highest fan-in files in the corpus -- exactly that
    way. copy_graph_from_texts skips whatever this returns None for, so the
    graph lost its three roots and the inheritance closure collapsed under it.
    That was the whole of the glibc 2.24 version floor: on glibc-2.12..2.17
    step 3 reported 11 affected locales where there are 280.

    collate_text worked around the same regex, and scan_ellipsis then worked
    around collate_block. Fixing the function means the next caller inherits
    the fix instead of having to know about the trap.

    Returns the body WITHOUT the LC_COLLATE and END LC_COLLATE lines;
    collate_text returns the block with them, and callers depend on that.

    Unlike the regex this accepts an unterminated block, running it to the end
    of the file, because collate_bounds does. That is the conservative
    direction -- a locale stays in the copy graph rather than silently leaving
    it -- and it makes the two agree. Measured over glibc-2.12, 2.17, 2.28,
    2.34 and 2.39: the regex and this differ on exactly the three templates at
    the first two tags, and on nothing at the last three.
    """
    bounds = collate_bounds(text)
    if bounds is None:
        return None
    start, end = bounds
    lines = text.split('\n')
    head = lines[start - 1][len('LC_COLLATE'):]
    # collate_bounds' end is the END LC_COLLATE line when there is one, and the
    # file's last line of content when the block is unterminated. Drop it only
    # in the first case, or an unterminated block loses its last line.
    last = end - 1 if lines[end - 1].startswith('END LC_COLLATE') else end
    body = lines[start:last]
    return head + ('\n' + '\n'.join(body) if body else '')


def collate_bounds(text):
    """1-based (start_line, end_line) of the LC_COLLATE block, or None.

    If the block is unterminated, end is the last line of the file, which keeps
    the overlap test conservative (a change is more likely to be flagged).
    """
    lines = text.split('\n')
    start = None
    for idx, line in enumerate(lines, start=1):
        if start is None:
            if line.startswith('LC_COLLATE'):
                start = idx
        elif line.startswith('END LC_COLLATE'):
            return start, idx
    return (start, len(lines)) if start is not None else None


def collate_text(text):
    """The LC_COLLATE block as text, its header and footer lines included.

    Sliced from collate_bounds' line numbers rather than taken from
    collate_block: that regex requires a newline before LC_COLLATE, so a file
    beginning with LC_COLLATE at byte 0 returns None from it. glibc 2.23 and
    earlier write the three master templates exactly that way, so using
    collate_block here would silently file iso14651_t1_common -- the highest
    fan-in file in the corpus -- under "no block at all".

    Lived in diff_distro_locales.py until three callers needed it.
    """
    bounds = collate_bounds(text)
    if bounds is None:
        return None
    start, end = bounds
    return '\n'.join(text.split('\n')[start - 1:end])


def ellipsis_hits(block, comment_char='%'):
    """Lines of an LC_COLLATE block that use an algorithmic ellipsis range.

    Returns the original lines, stripped, so the caller can show what it found.
    """
    hits = []
    for line in block.split('\n'):
        if ELLIPSIS_RE.search(line.split(comment_char)[0]):
            hits.append(line.strip())
    return hits


def comment_char(text):
    """The locale file's comment character, `%` unless it says otherwise."""
    m = _COMMENT_CHAR_RE.search(text)
    return m.group(1) if m else '%'


def decode_symbolic(name):
    """`<U0069><U0073><U006F>` -> `iso`, and anything else unchanged.

    A locale name written in glibc's symbolic notation is the same name to
    localedef, and was a different one here: the graph kept the escaped
    spelling as a key nothing matched, so the locale looked like a leaf that
    copies nothing reachable. Two files in the corpus do this.
    """
    return _UCHAR_RE.sub(lambda m: chr(int(m.group(1), 16)), name)


def copy_targets(text):
    """Every `copy "..."` target inside LC_COLLATE, in order.

    All of them, not just the first: om_ET copies both am_ET and om_KE, and
    taking only the first hides any change to the second. Symbolic spellings
    are decoded, because localedef decodes them and the graph is a claim about
    what localedef will build.
    """
    block = collate_block(text)
    if block is None:
        return []
    return [decode_symbolic(t) for t in _COPY_RE.findall(block)]


def classify_collation_style(text):
    """How does this locale's LC_COLLATE define its order? One of:

      'none'      -- no LC_COLLATE block; no sort order of its own
      'codepoint' -- declares `codepoint_collation`. Byte order by
                     construction, and nothing localedef does to ranges can
                     move it. Upstream's C is this from glibc 2.35 on.
      'ellipsis'  -- uses ellipsis ranges, whose weights localedef computes at
                     build time, so a data diff can never clear it. RHEL8's and
                     RHEL9's BACKPORTED C is this -- which is why C.UTF-8's
                     order moved between them from a file no tag diff can see.
      'copy-only' -- nothing but `copy`; its order is whatever it inherits
      'explicit'  -- the weights are spelled out in this file

    Precedence is deliberate. `codepoint_collation` "in any part of any
    LC_COLLATE immediately discards all collation information" (glibc's own
    comment), so it outranks an ellipsis in the same block; and 'ellipsis'
    outranks 'copy-only' because a copy cannot undo a range this file declares.

    Comments are stripped first and the keyword is matched as a whole token.
    glibc-2.39:localedata/locales/C names `codepoint_collation` in prose three
    lines ABOVE the declaration -- "The keyword 'codepoint_collation' in any
    part of any LC_COLLATE..." -- so a substring search reads that comment as a
    declaration. Which direction that fails in is what makes it worth a test:
    it would report an ellipsis-based backport as byte order, i.e. clear the
    one locale this whole classification exists to catch.
    """
    block = collate_text(text)
    if block is None:
        return 'none'
    cc = comment_char(text)
    body = [line.split(cc)[0] for line in block.split('\n')
            if not line.startswith(('LC_COLLATE', 'END LC_COLLATE'))]
    if any(_CODEPOINT_RE.search(line) for line in body):
        return 'codepoint'
    if any(ELLIPSIS_RE.search(line) for line in body):
        return 'ellipsis'
    content = [line.strip() for line in body if line.strip()]
    if content and all(line.startswith('copy') for line in content):
        return 'copy-only'
    return 'explicit'


def scan_ellipsis(texts):
    """({name: [hit lines]}, how many of `texts` define LC_COLLATE).

    Pure and shared, so a scan of a git tag and a scan of a node's
    /usr/share/i18n/locales/ cannot drift on comment_char handling.

    Uses collate_text, not collate_block: see collate_text. In a tag scan the
    difference is nil (every file from 2.24 on opens with escape_char), but a
    node directory holds arbitrary distro files, and there the regex's blind
    spot would clear a template rather than flag it.
    """
    flagged, with_collate = {}, 0
    for name, text in texts.items():
        block = collate_text(text)
        if block is None:
            continue
        with_collate += 1
        hits = ellipsis_hits(block, comment_char(text))
        if hits:
            flagged[name] = hits
    return flagged, with_collate


def copy_graph_from_texts(texts):
    """{name: [copy targets]} for every entry of `texts` defining LC_COLLATE.

    Pure, so the graph can be built from a git tag (build_copy_graph) or from a
    directory of locale sources with one implementation. Keys are used
    verbatim: pass the names the caller wants to see, not paths.

    Locales with no `copy` map to an empty list, so the graph doubles as the set
    of names that define collation in this corpus.
    """
    graph = {}
    for name, text in texts.items():
        if collate_block(text) is None:
            continue
        graph[name] = copy_targets(text)
    return graph


def build_copy_graph(repo, tag):
    """copy_graph_from_texts over every locale file at `tag`."""
    paths = list_locale_files(repo, tag)
    contents = read_blobs_strict(repo, tag, paths, 'the LC_COLLATE copy graph')
    return copy_graph_from_texts(
        {os.path.basename(path): text for path, text in contents.items()})


def inherited_from(graph, roots):
    """{locale: [roots it inherits from]} for every locale reaching `roots`.

    Walks all parents, not a single chain, and guards against cycles. Reports
    EVERY root reached, not the first one a depth-first walk happens to find:
    the traversal order is an artefact of `stack.pop()`, and attributing a
    locale to an arbitrary one of several roots makes the "reaches X"
    explanation untrue even when the affected set is right.
    """
    result = {}
    for name in graph:
        if name in roots:
            continue
        seen, stack, hits = set(), list(graph[name]), set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur in roots:
                hits.add(cur)
                continue
            stack.extend(graph.get(cur, ()))
        if hits:
            result[name] = sorted(hits)
    return result


def fan_in(graph):
    """{locale: number of other locales that inherit its LC_COLLATE}."""
    return {name: len(inherited_from(graph, {name})) for name in graph}


_CODESET_RE = re.compile(r'\.([^.@]+)(?=@|$)')


def normalize_locale_name(entry):
    """A SUPPORTED entry as `locale -a` and pg_collation actually spell it.

    localedef normalises the codeset when it builds the locale -- lowercase,
    punctuation dropped, `iso` prefixed to an all-digit codeset -- so
    `sv_SE.UTF-8` is installed, listed and imported into pg_collation as
    `sv_SE.utf8`. Printing the raw SUPPORTED spelling and calling it "the name
    locale -a shows" sends people to `COLLATE "sv_SE.UTF-8"`, which fails with
    `collation ... does not exist`.
    """
    def norm(m):
        cs = ''.join(c for c in m.group(1) if c.isalnum()).lower()
        return '.' + ('iso' + cs if cs.isdigit() else cs)
    return _CODESET_RE.sub(norm, entry)


def supported_map(repo, tag):
    """{source file name: [generated locale names]} from localedata/SUPPORTED.

    The audit works in source file names (sv_FI@euro), but `locale -a` and
    pg_collation show generated names with codesets (sv_FI.utf8, sv_FI). This
    is the mapping between them, in the spelling those tools use.
    """
    contents, missing = read_blobs(repo, tag, ['localedata/SUPPORTED'])
    if missing:
        # Returning {} here made every locale print as "not listed in
        # SUPPORTED, so normally absent from `locale -a`" -- a false and
        # reassuring claim -- and wrote an empty step4 list under the heading
        # "full list of generated names".
        die(f"localedata/SUPPORTED does not exist at {tag}; cannot map source "
            f"file names to the generated names `locale -a` shows.")
    out = {}
    for line in contents['localedata/SUPPORTED'].split('\n'):
        line = line.strip().rstrip('\\').strip()
        if not line or line.startswith('#') or '=' in line:
            continue
        entry = line.split('/')[0].strip()
        if not entry:
            continue
        source = _CODESET_RE.sub('', entry)
        generated = normalize_locale_name(entry)
        if generated not in out.setdefault(source, []):
            out[source].append(generated)
    return out


OUT_DIR = os.environ.get('PG_GLIBC_AUDIT_OUT', '/tmp/pg-glibc-collation-audit')


def wrapped():
    """True when audit.sh is driving this step, rather than a human.

    The steps print a hint naming the command to run next, which is right for
    a hand-run audit and wrong under the wrapper: it tells the reader to run a
    step that has already run. Only the hints are suppressed -- never a
    finding, a count or a warning.
    """
    return os.environ.get('PG_GLIBC_AUDIT_WRAPPED') == '1'


def pair_slug(old_tag, new_tag):
    """Filename fragment identifying a version pair.

    A result list named after the pair it describes cannot be mistaken for a
    leftover from a different one. That matters most where a file becomes argv
    for a later step: see write_list.
    """
    safe = lambda tag: re.sub(r'[^A-Za-z0-9_.@+-]', '_', tag)
    return f"{safe(old_tag)}..{safe(new_tag)}"


def write_list(name, items):
    """Write a long result list to OUT_DIR and return the path.

    Keeps terminal output scannable: the scripts print counts and a sample,
    and park the full several-hundred-entry lists here.
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, 'w', encoding='utf-8') as fh:
        for item in items:
            fh.write(f"{item}\n")
    return path


def _main(argv):
    if len(argv) >= 2 and argv[0] == 'provenance':
        repo = find_repo()
        check_refs(repo, *argv[1:])
        report_tag_provenance(repo, *argv[1:])
        return 0
    if len(argv) >= 2 and argv[0] == 'corpus':
        # The corpus floor, for the shell step. Silent on success so that step
        # 1's output stays byte-identical; list_locale_files dies otherwise.
        repo = find_repo()
        check_refs(repo, *argv[1:])
        for tag in argv[1:]:
            list_locale_files(repo, tag)
        return 0
    if argv and argv[0] == 'order':
        # The direction of the pair, for the two shell entry points. The status
        # WORD is the only thing on stdout, because audit.sh captures it in a
        # variable; every human sentence goes to stderr. A forward pair
        # therefore adds nothing at all to the run's output, which is what
        # keeps the audited pairs byte-identical.
        #
        # --quiet suppresses the `!!` blocks, not the refusal: the steps are
        # the callers that print them, into their own logs, where the summary's
        # warnings block finds them. audit.sh asks only for the word, and one
        # more copy of a warning the reader has already seen teaches them to
        # skip the section.
        allow = '--allow-reverse' in argv[1:]
        quiet = '--quiet' in argv[1:]
        tags = [a for a in argv[1:]
                if a not in ('--allow-reverse', '--quiet')]
        if len(tags) != 2:
            die("usage: glibc_locale_data.py order [--allow-reverse] "
                "[--quiet] <old_tag> <new_tag>")
        repo = find_repo()
        check_refs(repo, *tags)
        sink = io.StringIO() if quiet else sys.stderr
        with contextlib.redirect_stdout(sink):
            status = require_pair_order(repo, tags[0], tags[1],
                                        allow_reverse=allow)
        print(status)
        return 0
    if len(argv) >= 2 and argv[0] == 'fanin':
        repo = find_repo()
        tag = argv[1]
        check_refs(repo, tag)
        graph = build_copy_graph(repo, tag)
        counts = fan_in(graph)
        changed = None
        if len(argv) >= 3:
            with open(argv[2], encoding='utf-8') as fh:
                changed = {os.path.basename(ln.strip())
                           for ln in fh if ln.strip()}
        print(f"Collation templates by blast radius at {tag} "
              f"(locales inheriting via `copy`):")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
            if n == 0:
                break
            mark = ''
            if changed is not None and name in changed:
                mark = '  <== has some change in this version pair'
            print(f"  {n:4d} locales inherit from {name}{mark}")
        if changed is not None:
            hot = sorted(((counts.get(c, 0), c) for c in changed
                          if counts.get(c, 0) > 0), reverse=True)
            print()
            if not hot:
                print("No changed file is inherited from by any other locale.")
                return 0
            # These files changed SOMEWHERE -- usually LC_TIME or LC_MONETARY.
            # Step 2 decides which of them changed LC_COLLATE. Reported here
            # only so that, if step 2 does flag one, its reach is already
            # visible.
            print(f"Changed files that others inherit from ({len(hot)}) -- "
                  f"reach if the change turns out to be in LC_COLLATE.")
            print("NOT a collation verdict: most of these changed LC_TIME or "
                  "LC_MONETARY. Step 2 filters.")
            for n, name in hot[:8]:
                print(f"  {name}: {n} dependent locale(s)")
            if len(hot) > 8:
                rest = ', '.join(name for _, name in hot[8:])
                print(f"  ... and {len(hot) - 8} more with 1-2 dependents: "
                      f"{rest}")
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
