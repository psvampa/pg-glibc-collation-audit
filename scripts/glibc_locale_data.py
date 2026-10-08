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
  python3 glibc_locale_data.py pair-slug <old> <new>
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
# 366 (2.39). The node-reading modes have refused a directory below this floor
# since they were written; the tag modes did not check at all, so a tag whose
# tree lacks localedata/locales/ -- a restructured checkout, a tag from before
# the directory existed -- produced "0 files changed", "No locale uses ellipsis
# ranges here" and exit 0.
MIN_LOCALE_FILES = 200

# Prepended to every git invocation. `git diff` obeys the user's config, and
# these four settings were the first measured to change the text this tool
# parses. Measured with git 2.50 on the
# 2.34..2.39 pair: diff.noprefix=true drops the a/ b/ that the file-header
# regex in filter_lc_collate_changes.py expects (the step then dies, correctly,
# with "the diff does not cover"); diff.renameLimit=1 turns the one rename into
# delete+add with only a warning on stderr, so the added file lands in "not
# analysed" -- silently; diff.renames=false does the same to step 1, whose
# published count goes from 318 to 319; color.ui=always writes escape codes
# into the pipe. -c on the command line outranks every configuration source.
# diff.srcPrefix/dstPrefix replace the a/ b/ as noprefix does (git 2.54):
# step 2 then died saying "Drop --diff-file" when none was passed. The two
# settings below them move where a hunk starts and ends. For the diffs a step
# reads, git_diff's flags already hold them -- and only the flag beats a
# per-driver `diff.<driver>.algorithm` from the reader's attributes file --
# so here they are for any other git call, not the guarantee.
GIT_CONFIG_OVERRIDES = ['-c', 'color.ui=false',
                        '-c', 'diff.noprefix=false',
                        '-c', 'diff.mnemonicPrefix=false',
                        '-c', 'diff.srcPrefix=a/',
                        '-c', 'diff.dstPrefix=b/',
                        '-c', 'diff.renames=true',
                        '-c', 'diff.renameLimit=0',
                        '-c', 'diff.algorithm=myers',
                        '-c', 'diff.indentHeuristic=true']

# Every `git diff` whose TEXT a step reads goes through git_diff() and carries
# these. On a machine with no configuration none of them changes the text --
# the locale sources are text, so even --text is what git would do anyway.
# Each one is here because a setting this run does not control changed that
# text and a step read the result as an answer. Measured with git 2.50 for
# step 5 and git 2.54 for step 2:
#
#   --text         `-diff` or `binary` in the reader's core.attributesFile makes
#                  git print "Binary files a/x and b/x differ" -- no hunk. With
#                  `localedata/locales/* -diff`, step 2 put every locale in
#                  'other', and the full audit of 2.28..2.34 printed "Reindex:
#                  none" at exit 0 over or_IN and sv_SE.
#   --no-textconv  a `diff.<driver>.textconv` reached the same way. Git numbers
#                  the hunks on the converted text, and step 2 places them on
#                  the raw file: a textconv that prepends 300 lines lost sv_SE
#                  at exit 0. One that empties both sides left step 5 an EMPTY
#                  diff, which its hunk-less guard never sees.
#   --no-ext-diff  `diff.external`, or GIT_EXTERNAL_DIFF in the environment
#                  (which beats config, so pinning config alone is not
#                  enough). GIT_EXTERNAL_DIFF=/usr/bin/true made all 39 files
#                  step 5 diffs over 2.28..2.34 read as unchanged, with 6 hunks
#                  in ld-collate.c alone.
#   --no-color     `color.diff` beats the `color.ui=false` above (more specific
#                  wins). With `color.diff.frag=normal` the hunk headers stay
#                  plain so they still match, every body line starts with an
#                  escape, each hunk comes back empty and therefore all-noise,
#                  and step 5 printed its clean sentence over the pair that
#                  carries Bug 22668.
#   --inter-hunk-context=0
#                  how far apart two changes must be to stay two hunks.
#                  diff.interHunkContext=50 merges them: step 5 counts 31 hunks
#                  over 2.34..2.39 instead of 52, and step 2 flags 5 files over
#                  2.28..2.34 instead of 2. Noise, not a hidden change, but a
#                  published number must not move with a reader's config.
#   --diff-algorithm=myers, --indent-heuristic
#                  patience and histogram pair the same changed lines into
#                  different hunks: 731 `>>` lines in step 5 instead of 733, and
#                  a different list of changed characters in step 2. Nothing
#                  hidden -- again a number that moved. The flag, not the
#                  `-c diff.algorithm` above, is what holds: `* diff=alg` with
#                  `diff.alg.algorithm=patience` beats the setting.
#
# The context count is not here, because the two readers need different ones:
# step 2 reads -U0 and step 5 -U3. GIT_DIFF_OPTS would beat either, so run_git
# drops it from the environment.
DIFF_FLAGS = ['--text', '--no-textconv', '--no-ext-diff', '--no-color',
              '--inter-hunk-context=0', '--diff-algorithm=myers',
              '--indent-heuristic']

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

# glibc's keyword for byte order (locale/programs/ld-collate.c, from 2.35;
# RHEL9 backports it). This pattern only answers "does the file NAME the
# keyword outside a comment", which is not "does the locale sort by bytes":
# declares_byte_order answers that, and a file this matches and that does not
# answer gets a style of its own. Matched as a whole token because the file
# that declares it also DISCUSSES it in a comment. `<` and `>` are in the
# guards because glibc's lexer reads `<name>` as a collating symbol and never
# as this keyword.
_CODEPOINT_RE = re.compile(
    r'(?<![A-Za-z0-9_<])codepoint_collation(?![A-Za-z0-9_>])')

# What localedef's line reader counts as space (isspace in the C locale).
# Python's str.strip() also strips U+00A0 and other Unicode spaces, which
# glibc reads as part of a word.
_SPACE = ' \t\n\r\f\v'

# The two directives, in the one place declares_byte_order accepts them.
_DIRECTIVE_RE = re.compile(r'(comment_char|escape_char)[ \t\r\f\v]+(\S)')

# A physical line whose first word opens this category.
_COLLATE_WORD_RE = re.compile(
    r'^[ \t\r\f\v]*LC_COLLATE(?![A-Za-z0-9_])', re.M)

# A character a word can hold on both sides of an escape.
_WORD_CHAR = '[A-Za-z0-9_]'

# A `copy` line and nothing else on it.
_SOLE_COPY_RE = re.compile(r'copy[ \t\r\f\v]+"([^"]*)"')

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

    `cat-file --batch` does NOT fail that way: it exits 0 and prints the
    unfetchable blob as `missing` (read_blobs).
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


def git_diff(repo, args):
    """`git diff` with DIFF_FLAGS, decoded: the one way a step reads a diff.

    The flags lived in step 5's single call until step 2 was found reading a
    diff shaped by the reader's attributes file (DIFF_FLAGS, above). A step
    that asks git for a diff through here cannot forget one of them.
    """
    return run_git(['diff', *DIFF_FLAGS, *args],
                   repo).stdout.decode('utf-8', 'replace')


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


def warn(text, split_words=True):
    """The `!!` block shape audit.sh collects and repeats at the bottom.

    One copy, in the shared module, because two copies of a formatter drift and
    this repository has already paid for that: the suite's `flat()` exists
    because output wrapped at 78 columns made a negative assertion vacuous, and
    the wrapper's warnings block matches `^!!` followed by three-space
    continuation lines. `diff_distro_locales.warn` is an alias for this.

    `split_words=False` keeps a path or an option name whole: textwrap breaks
    at hyphens by default, and a temp directory's name printed as `pg-glibc-` /
    `distro-...` cannot be pasted back into a shell. Splitting stays on by
    default, so every other block wraps exactly as the published transcripts
    show it.
    """
    print(textwrap.fill(text, width=78,
                        initial_indent='!! ', subsequent_indent='   ',
                        break_on_hyphens=split_words,
                        break_long_words=split_words))


def report_missing_from_copy(copy_names, tag_names, tag, where, skipped=()):
    """`!!` over the files `tag` has that a copy of a node's locales lacks.

    `skipped` is node_entries' (entry, why) list. A tag file the copy holds
    as a symlink or a directory is in the directory and was not read, which
    is neither missing nor examined, so it is named apart: counting entries
    would clear it (measured, th_TH as a symlink: 355 entries against the
    tag's 355).

    Returns every name it reported, sorted; prints nothing when there is none.
    A copy that lost files in transit reported 0 differences inside
    LC_COLLATE over what was left, and listed the rest as an ordinary finding
    with no `!!`, so it reached the AUDIT SUMMARY in no form at all (backlog
    1.15). Measured at 300 of glibc-2.34's 355 files: exit 0, "Nothing
    differs", th_TH among the 55 lost. The measured nodes lack none of their
    tag's files (el8 against 2.28, el9 against 2.34, el10 against 2.39), so on
    a complete copy this prints nothing.

    It concludes nothing about WHY a file is missing. A count the reader
    asserts only proves the directory holds that many files, not that they
    came from the node -- the conclusion a first attempt at this fix drew,
    and a false negative of its own. It advises comparing names on the
    machine rather than counts for the same reason. One text for steps 6/7
    and 9/10, so the wrapper's summary, which deduplicates identical blocks,
    prints it once.
    """
    why = dict(skipped)
    unread = sorted((set(tag_names) & set(why)) - set(copy_names))
    missing = sorted(set(tag_names) - set(copy_names) - set(unread))
    said = []
    if missing:
        said.append(f"{len(missing)} file(s) that {tag} has under "
                    f"{LOCALES_DIR}/ are missing from {where}: "
                    f"{', '.join(missing)}.")
    if unread:
        said.append(f"{len(unread)} file(s) that {tag} has under "
                    f"{LOCALES_DIR}/ are in {where} but were not read: "
                    f"{', '.join(f'{n} ({why[n]})' for n in unread)}.")
    if said:
        said.append("The checks that read this directory skipped them, so "
                    "they say nothing about those locales on this machine, "
                    "nor about any locale that copies one of them.")
    if missing:
        said.append("This run cannot tell whether the machine does not ship "
                    "them or the copy lost them. Check on the machine "
                    "whether /usr/share/i18n/locales/ has them.")
    if unread:
        said.append("Copy those again as regular files.")
    if said:
        warn(' '.join(said), split_words=False)
    return sorted(missing + unread)


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
        warn(f"{old} and {new} are the same commit. Steps 1, 2 and 5 compare "
             f"upstream source against itself. Steps 1 to 3 can only report "
             f"'nothing changed', which for an intra-major upgrade "
             f"(RHEL 8.1 -> 8.2, say) says nothing at all, and step 5 reports "
             f"that nothing was compared. Step 4 reads only the new tag, so "
             f"its list still needs confirming. The distro's own "
             f"builds are where such a change lives. With each machine's file "
             f"(--old-node and --new-node), step 11 measures how each machine "
             f"sorts, and steps 6 to 10 read its locale sources when the file "
             f"holds them; sql/c_utf8_probe.sql checks C.UTF-8 on each "
             f"machine. C.UTF-8's order moved in glibc-2.28-93.el8 with the "
             f"upstream tag unchanged. See docs/results.md.",
             split_words=False)
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
                f"arguments.")
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
    came back without a body. Distinguishing "missing" from "failed" is what
    lets callers report a locale added in the new tag instead of silently
    skipping it.

    "Missing" is not only "not at that tag". On a --filter=blob:none clone a
    blob that cannot be fetched -- GIT_NO_LAZY_FETCH=1, or a promisor that
    answers "I do not have it" -- is printed by `cat-file --batch` as
    `<request> missing` at exit 0 (measured, git 2.50), the same line as a path
    that does not exist. A caller whose paths came from that tag's own tree
    uses read_blobs_strict, which says the read failed.
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


def _glibc_chars(lines):
    """(comment char, escape char) localedef reads this file with, or None.

    linereader starts every file at `#` and `\\` (lr_create,
    locale/programs/linereader.c:79-80@2.39), and a `comment_char` or
    `escape_char` line at top level changes them from that line on
    (locfile.c:123-149), in order, so the last one read wins. The answer is
    certain only in one shape: the directives come first, with nothing but
    blank lines before them, so no earlier line can be continued into one,
    and none appears anywhere else, where it would change the characters for
    what follows. Each value must be one glibc's own files use or the default
    it restates: glibc refuses an argument it does not read as a
    one-character word ("bad argument"; `<` reads as a symbol, a digit as a
    number, linereader.c lr_token), and keeps the old character. A directive
    line ending in the escape character in force when it is read is joined
    to the next line before its argument is (lr_next :162-171; locfile.c:126
    reads it with lr_token), so `escape_char \\` with `/` below it sets `/`.
    Any of these shapes is None, which declares_byte_order reads as "not
    byte order".
    """
    chars = {'comment_char': '#', 'escape_char': '\\'}
    allowed = {'comment_char': '%#', 'escape_char': '/\\'}
    leading = True
    for line in lines:
        stripped = line.strip(_SPACE)
        directive = re.match(r'(comment_char|escape_char)(?![A-Za-z0-9_])',
                             stripped)
        if directive:
            word = directive.group(1)
            m = _DIRECTIVE_RE.fullmatch(stripped)
            if (not leading or m is None
                    or m.group(2) not in allowed[word]
                    or line.endswith(chars['escape_char'])):
                return None
            chars[word] = m.group(2)
        elif stripped:
            leading = False
    return chars['comment_char'], chars['escape_char']


def _escape_builds_a_word(lines, cc, ec):
    """Can the escape character build a word out of pieces, outside a comment?

    glibc's reader keeps a word going across it: inside a word the character
    after the escape is taken as it is (get_ident, linereader.c:580-589@2.39),
    and a line ending in the escape is joined to the next one with only that
    last escape removed (lr_next :162-171, lr_getc linereader.h:125). So
    `LC_COLL/ATE`, `LC_COL/` then `LATE`, `LC_COL/` then `/LATE`, or
    `LC_COLL//` then `ATE` all open an LC_COLLATE section that a search for
    the word cannot see, and a second section is read in the same run.

    Lines are joined here the way lr_next joins them, and the join is refused
    when it glues a word character to a word character, or when the joined
    line holds the escape between two of them. Refused rather than reassembled:
    no file that declares the keyword does either at any tag from 2.35 to
    2.42 or on the RHEL8, RHEL9 and RHEL10 fixtures. A line opening with the
    comment character is skipped whole by glibc, continued or not
    (linereader.c:222-229), and so is skipped here when it opens a line.
    """
    word = re.compile(_WORD_CHAR)
    joined = re.compile(_WORD_CHAR + re.escape(ec) + _WORD_CHAR)
    cur = None
    for line in lines:
        if cur is None and line.strip(_SPACE).startswith(cc):
            continue
        if cur and line and word.fullmatch(cur[-1]) and word.match(line):
            return True
        cur = line if cur is None else cur + line
        if line.endswith(ec):
            cur = cur[:-len(ec)]
            continue
        if joined.search(cur):
            return True
        cur = None
    return bool(cur) and joined.search(cur) is not None


def _collate_body(text):
    """(comment char, body lines) of an LC_COLLATE localedef reads with
    certainty, or None.

    Certain means: the characters are known (_glibc_chars); the file has one
    line opening LC_COLLATE, at column 0, with nothing else on it, and no
    word built with the escape character (_escape_builds_a_word) that could
    open another, because a second one would be read too (locfile.c:179-180
    hands every LC_COLLATE section to collate_read); the block ends at a bare
    `END LC_COLLATE`; and no line from the header to that END ends in the
    escape character, which joins it to the next line (linereader.c:162-171):
    the header would swallow the first line of the block, a `copy` line the
    line after it (ld-collate.c:2661@2.39, lr_ignore_rest).
    """
    lines = text.split('\n')
    chars = _glibc_chars(lines)
    if chars is None or len(_COLLATE_WORD_RE.findall(text)) != 1:
        return None
    cc, ec = chars
    if _escape_builds_a_word(lines, cc, ec):
        return None
    bounds = collate_bounds(text)
    if bounds is None:
        return None
    start, end = bounds
    if (lines[start - 1].strip(_SPACE) != 'LC_COLLATE'
            or lines[end - 1].strip(_SPACE) != 'END LC_COLLATE'):
        return None
    if any(line.endswith(ec) for line in lines[start - 1:end]):
        return None
    return cc, lines[start:end - 1]


def _content(text):
    """The lines of the LC_COLLATE block that are not blank and not a
    comment, stripped, or None when _collate_body cannot read it.

    None too when this module's own ellipsis reader sees a range in the
    block. That reader takes `%` as the comment character when a file
    declares none, and glibc takes `#` (backlog 13.8), so a `#` comment
    holding `..` is a range to one and a comment to the other. Where they
    disagree the range wins: a locale is never both listed by step 4 and
    called byte order beside it.
    """
    frame = _collate_body(text)
    if frame is None:
        return None
    if ellipsis_hits(collate_text(text), comment_char(text)):
        return None
    cc, body = frame
    words = [line.strip(_SPACE) for line in body]
    return [w for w in words if w and not w.startswith(cc)]


def declares_byte_order(text):
    """Does glibc build this locale in byte order from its own LC_COLLATE?

    Only when `codepoint_collation` is the whole of the block, comments and
    blank lines aside. The keyword sets a flag (ld-collate.c:2691-2692@2.39),
    but the number of sort rules is one global for the whole localedef run
    (:273), set by the first `order_start` or `reorder-sections-after` read in
    it (:613-619, :3197-3202, :3678), and collate_output writes that global
    before it looks at the flag (:2120-2123). Measured (glibc study, E5):
    `copy "iso14651_t1"` plus the keyword compiles on RHEL9 and RHEL10 with no
    message into tables whose strcoll returns garbage and crashes; on the same
    line as the copy, inside an `ifdef`, glued to `%`, after a continued line
    or as the argument of `define` the keyword does nothing (E6). Anything but
    the keyword alone is therefore not byte order here, and the conservative
    answer for a shape not read with certainty is False: the locale stays
    listed.

    The other categories of the file do not reach this. A file read for
    LC_TIME or LC_CTYPE reads its LC_COLLATE with ignore_content
    (locfile.c:53, :179-180), and `order_start` is skipped there
    (ld-collate.c:3113-3117).

    Where the build does not know the keyword (upstream before 2.35, RHEL8)
    the keyword alone does not compile at all: "too many errors; giving up"
    (ld-collate.c:1852@2.28), measured on glibc-2.28-251.el8_10.40. No
    collation is built, so nothing can be sorted wrongly by it; PostgreSQL
    refuses a collation whose locale the machine lacks ("could not create
    locale", pg_locale.c:1445@REL_14_24, pg_locale_libc.c:831@REL_18_6).
    """
    return _content(text) == ['codepoint_collation']


def _sole_copy_target(text):
    """The locale this LC_COLLATE copies, when that copy is the whole block,
    or None.

    None too unless classify_collation_style also reads the file as nothing
    but a copy. It strips comments with `%` when a file declares no comment
    character, and glibc with `#` (backlog 13.8), so a `#` comment naming the
    keyword made a C both 'codepoint-not-alone' and a copy of a byte-order
    locale in one status. Every reader here has to agree before a copy
    clears anything.
    """
    if classify_collation_style(text) != 'copy-only':
        return None
    content = _content(text)
    if content is None or len(content) != 1:
        return None
    m = _SOLE_COPY_RE.fullmatch(content[0])
    if m is None:
        return None
    target = decode_symbolic(m.group(1))
    return target if re.fullmatch(r'[A-Za-z0-9_.@+-]+', target) else None


def byte_order_locales(texts):
    """({names that declare byte order}, {name: the locale it copies}).

    The second part is backlog 6.9: a locale whose LC_COLLATE is a `copy` and
    nothing else shares the copied locale's data (ld-collate.c:1518-1521@2.39,
    first-statement copy), so copying one that declares byte order, directly
    or through more such copies, reads no sort rule either. Measured on
    glibc-2.34-275.el9_8 and glibc-2.39-128.el10_2: `copy "C"`, a copy of that
    copy, and a copy of a file holding the keyword alone all compile
    byte-identical to the installed C.UTF-8. A cycle or a target this corpus
    lacks is not byte order: localedef refuses both (exit 5 and exit 4).
    """
    declared = {name for name, text in texts.items()
                if declares_byte_order(text)}
    copies = {}
    for name, text in texts.items():
        target = _sole_copy_target(text)
        if target is not None:
            copies[name] = target
    by_copy = {}
    for name, target in copies.items():
        seen, cur = {name}, target
        while cur in copies and cur not in seen:
            seen.add(cur)
            cur = copies[cur]
        if cur in declared:
            by_copy[name] = target
    return declared, by_copy


def classify_collation_style(text):
    """How does this locale's LC_COLLATE define its order? One of:

      'none'      -- no LC_COLLATE block; no sort order of its own
      'codepoint' -- `codepoint_collation` is the whole block
                     (declares_byte_order). Byte order by construction, and
                     nothing localedef does to ranges can move it. Upstream's
                     C is this from glibc 2.35 on, and RHEL9's and RHEL10's.
      'ellipsis'  -- uses ellipsis ranges, whose weights localedef computes at
                     build time, so a data diff can never clear it. RHEL8's
                     own C is this -- which is why C.UTF-8's order
                     moved from RHEL8 to RHEL9 from a file no tag diff can see.
      'codepoint-not-alone'
                  -- names `codepoint_collation`, but declares_byte_order
                     could not read it as the keyword alone: either glibc
                     gives no byte order (a copied template or a sort rule
                     beside it, the keyword in a branch not taken, ...) or
                     this code cannot tell with certainty. Not cleared
                     either way.
      'copy-only' -- nothing but `copy`; its order is whatever it inherits
      'explicit'  -- the weights are spelled out in this file

    Precedence: 'ellipsis' outranks 'codepoint-not-alone', because both are
    not cleared and the range is the reason the warnings already name; and
    both outrank 'copy-only', because a copy cannot undo what this file adds.

    Comments are stripped and the keyword is matched as a whole token.
    glibc-2.39:localedata/locales/C names `codepoint_collation` in prose three
    lines ABOVE the declaration, so a substring search reads that comment as
    the keyword.
    """
    block = collate_text(text)
    if block is None:
        return 'none'
    if declares_byte_order(text):
        return 'codepoint'
    cc = comment_char(text)
    body = [line.split(cc)[0] for line in block.split('\n')
            if not line.startswith(('LC_COLLATE', 'END LC_COLLATE'))]
    if any(ELLIPSIS_RE.search(line) for line in body):
        return 'ellipsis'
    if any(_CODEPOINT_RE.search(line) for line in body):
        return 'codepoint-not-alone'
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
_SOURCE_RE = re.compile(r'\.[^@]*')


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


def locale_source(name):
    """The locale source file a locale name is built from.

    glibc's own rule: localedata/Makefile builds each SUPPORTED entry from
    `locales/<name>` with everything from the first dot up to the next @, or
    to the end, removed (`sed 's/\\([^.]*\\)[^@]*\\(.*\\)/\\1\\2/'`, the same
    at 2.12 and 2.39). setlocale reads a codeset the same way only when its
    dot comes before any @ (intl/explodename.c): glibc-2.12's
    tt_RU@iqtelif.UTF-8 is built from tt_RU@iqtelif, and setlocale reads
    iqtelif.UTF-8 as its modifier. So sv_SE.utf8, sv_SE.iso885915, sv_SE and
    a database's sv_SE.UTF-8 are all sv_SE, sv_FI.iso885915@euro is
    sv_FI@euro, and a.b.c is a, as the sed makes it. The one rule this module
    uses for it: supported_map derives its source names with it too, and
    sql/collation_confirmation_template.sql applies the same expression.
    """
    return _SOURCE_RE.sub('', name, count=1)


def locale_aliases(repo, tag):
    """{alias: locale} from intl/locale.alias at `tag`.

    These are names too: `locale -a` lists swedish, and PostgreSQL imports it
    as a collation, because the locale archive adds every alias whose locale
    is built (locale/programs/locarchive.c, add_locale_to_archive). Parsed as
    glibc parses it (intl/localealias.c, read_alias_file): a line whose first
    non-blank character is `#` is a comment, and an entry is the first two
    words of a line. A missing file is an error rather than no aliases: an
    empty map would drop swedish from the list and read as nothing to add.
    """
    path = 'intl/locale.alias'
    contents, missing = read_blobs(repo, tag, [path])
    if missing:
        die(f"{path} does not exist at {tag}; cannot name the aliases glibc "
            f"gives a locale (swedish for sv_SE).")
    out = {}
    for line in contents[path].split('\n'):
        words = line.split()
        if len(words) < 2 or words[0].startswith('#'):
            continue
        out[words[0]] = words[1]
    if not out:
        die(f"{path} at {tag} holds no alias at all; that is a reader or a "
            f"corpus problem, not an answer.")
    return out


def _aliases_reaching(sources, aliases):
    return {alias: locale_source(target) for alias, target in aliases.items()
            if locale_source(target) in sources
            and alias != locale_source(target)}


def aliases_of(sources, aliases):
    """{alias: source} for every alias whose locale is built from one of
    `sources` (swedish -> sv_SE.ISO-8859-1 -> sv_SE). An alias spelled as its
    own locale (ko_KR -> ko_KR.eucKR, ja_JP -> ja_JP.eucJP) is that locale's
    name already, and is left out. So is an alias whose name is not ASCII,
    which non_ascii_alias_targets names for the caller to say so."""
    return {a: s for a, s in _aliases_reaching(sources, aliases).items()
            if a.isascii()}


def non_ascii_alias_targets(sources, aliases):
    """The locales of `sources` that have an alias whose name is not ASCII.

    Until glibc 2.22 locale.alias carried bokmal and francais spelled in
    Latin-1 bytes; glibc removed them because they broke `locale -a`
    (Bug 18412). read_blobs decodes such a byte to U+FFFD, so the name cannot
    be written as it is, and PostgreSQL never imports a name that is not
    ASCII as a collation (pg_import_system_collations). They are left out of
    the list, and this is what lets the caller say so instead of dropping
    them in silence."""
    return sorted({s for a, s in _aliases_reaching(sources, aliases).items()
                   if not a.isascii()})


def supported_map(repo, tag):
    """{source file name: [names SUPPORTED installs it under]} from
    localedata/SUPPORTED, in the spelling localedef gives them (sv_FI.utf8).

    Step 2 prints these names for the locales a tag adds; steps 3 and 4 read
    the map to say which locales are not built by default, and step 4 also
    takes an empty map for a run with no tag. They are not every name a
    locale takes: the archive and RHEL add others (sv_SE.iso885915), and
    locale.alias adds swedish (backlog 13.1).
    """
    contents, missing = read_blobs(repo, tag, ['localedata/SUPPORTED'])
    if missing:
        # Returning {} here made every locale print as "not listed in
        # SUPPORTED, so normally absent from `locale -a`" -- a false and
        # reassuring claim -- and wrote an empty step4 list under the heading
        # "full list of generated names".
        die(f"localedata/SUPPORTED does not exist at {tag}; cannot tell which "
            f"locales are built by default, or the names they are installed "
            f"under.")
    out = {}
    for line in contents['localedata/SUPPORTED'].split('\n'):
        line = line.strip().rstrip('\\').strip()
        if not line or line.startswith('#') or '=' in line:
            continue
        entry = line.split('/')[0].strip()
        if not entry:
            continue
        source = locale_source(entry)
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
    if len(argv) == 3 and argv[0] == 'pair-slug':
        # audit.sh names every file a Python step also writes with this rather
        # than with bash's own replacement, which under LC_ALL=C replaces each
        # byte of a character outside ASCII where this replaces the character
        # once. The summary then looked for a file the step, which uses this
        # function, had written under another name.
        print(pair_slug(argv[1], argv[2]))
        return 0
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
