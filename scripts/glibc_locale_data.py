#!/usr/bin/env python3
"""
Shared helpers for reading glibc locale data out of a git clone.

The scripts in this directory need the same handful of primitives: locate the
glibc clone, read blobs at a tag, find the LC_COLLATE block, and walk the
`copy` graph. They used to each spawn one `git show` per locale file (~350
subprocesses) with a 30s timeout and an ignored return code, so a slow blob
fetch on a `--filter=blob:none` clone silently dropped files from the analysis
and the locale was reported unaffected. Everything here fails loudly instead,
and blobs are read in a single `git cat-file --batch` stream.

Also runnable directly; audit.sh and step 1 use fanin, sides, resolve,
machines, latest, order and pair-slug, and provenance and
corpus are kept for a hand run:
  python3 glibc_locale_data.py fanin <tag> [changed_files.txt] [--as NAME]
  python3 glibc_locale_data.py provenance <tag> [<tag> ...]
  python3 glibc_locale_data.py corpus <tag> [<tag> ...]
  python3 glibc_locale_data.py order [--allow-reverse] [--quiet] <old> <new>
      prints the direction (same, forward, reversed, undetermined) and
      whether nothing is compared (commit, tree, none)
  python3 glibc_locale_data.py pair-slug <old> <new>
  python3 glibc_locale_data.py sides [--allow-reverse] --refs-file F
          <old> <new>
      step 1's sides: each a tag of the clone, or a build named as
      glibc-2.34-283.el9_8, fetched and made a commit of the clone
      (build_sources), or, under audit.sh given each machine's file, made
      a commit from that file with nothing fetched (machine_side, through
      MACHINES_ENV); checks the order and the floor, prints the
      provenance, and writes to F the two refs the step reads; under
      audit.sh it also records each build's commit for the steps after
      it (SIDES_ENV)
  python3 glibc_locale_data.py resolve --to F <old> <new>
      for audit.sh, before anything runs: writes to F `<side>\t<kind>` a
      line each, the side without the architecture `rpm -q` prints
      (without_arch), kind `tag` or `build` (is_build_name); latest-elN is
      a build with a third field `latest`
  python3 glibc_locale_data.py machines --to F <old> <new> <old_build>
          <new_build> <old_folder> <new_folder>
      for audit.sh, given each machine's file: <old_build> and <new_build>
      are what `rpm -q glibc` printed on each, each folder that file laid
      out (whether it holds locales/, else the sources-not-included
      reason); writes to F, a line each, the side steps 1 and 2 read, its
      kind and its upstream tag, and prints which each side reads
  python3 glibc_locale_data.py latest --to F <old> <new>
      for audit.sh, once: writes to F each side a line, latest-elN
      replaced by the newest build of RHEL N the mirror lists, and prints
      which it took
"""
import collections
import contextlib
import io
import os
import re
import subprocess
import sys
import textwrap

LOCALES_DIR = 'localedata/locales'
SUPPORTED = 'localedata/SUPPORTED'

# Where a build's commit lives in the clone (build_sources.build_ref): its
# own namespace, outside refs/tags, so `git describe` never reads a build as
# a release and no bare name resolves to it.
BUILD_REFS = 'refs/pg-glibc-audit/builds/'
# The committer of every commit build_sources.build_commit makes. A step that
# resolves its refs refuses a commit with it (refuse_build_commits): such a
# commit holds the upstream tag's tree outside localedata/ and locale/, so
# read as a tag it hides the build's patches to strcoll's code.
BUILD_EMAIL = 'pg-glibc-audit@invalid'

# Fewer files than this under localedata/locales/ is not a glibc locale corpus.
# The five pinned tags carry 286 (2.12), 312 (2.17), 353 (2.28), 355 (2.34) and
# 366 (2.39). Read by below_floor alone, which side() applies to every side,
# a tag's or a machine's. The tag modes once did not check at all, so a tag
# whose tree lacks localedata/locales/ -- a restructured checkout, a tag from
# before the directory existed -- produced "0 files changed", "No locale uses
# ellipsis ranges here" and exit 0.
MIN_LOCALE_FILES = 200


def below_floor(count):
    """The floor, when `count` locale files are too few to be a glibc locale
    corpus; None when they are enough.

    The one place that reads MIN_LOCALE_FILES. Every side that holds files
    (side(), through _require_corpus: a tag, a build's commit or a
    machine's commit) is refused by the same number: two copies of it
    drift."""
    return MIN_LOCALE_FILES if count < MIN_LOCALE_FILES else None


# A directory entry becomes a path and a git argument. read_blobs' batch
# protocol desynchronises on an embedded newline, and audit.sh already made
# this decision for the same reason.
SAFE_NAME = re.compile(r'^[A-Za-z0-9_@.+-]+$')


def shown(name):
    """`name` as a message or a list line can carry it: bare when it is a
    plain file name (SAFE_NAME, the characters a locale file name is made
    of), else ascii(), quoted with every character outside printable ASCII
    escaped. Defined by what is safe to print rather than by what is not:
    the empty name (cases c17 and c54), a blank, a comma, a newline, a
    control character, a character that draws as nothing visible
    (U+3164) and a combining mark all fall outside it
    (false-negative-reviewer, rounds 1 to 3 of 13.44). fullmatch, not
    SAFE_NAME.match: `$` also matches before a final newline, so "zz\\n"
    passed the check and was written as two lines."""
    return name if SAFE_NAME.fullmatch(name) else ascii(name)


def node_entries(root):
    """Depth-1 regular files with usable names, plus everything skipped.

    Every skipped entry is returned so it can be printed. An unreported skip
    shrinks the corpus, and a smaller corpus produces a cleaner answer.
    """
    names, skipped = [], []
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if entry.startswith('.'):
            skipped.append((entry, 'dotfile'))
        elif os.path.islink(path):
            skipped.append((entry, 'symlink'))
        elif os.path.isdir(path):
            skipped.append((entry, 'directory'))
        elif not os.path.isfile(path):
            skipped.append((entry, 'not a regular file'))
        elif not SAFE_NAME.match(entry):
            skipped.append((entry, 'unsafe name'))
        else:
            names.append(entry)
    return names, skipped


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
# step 5 (deleted in step 9.3; its cases are kept as the evidence for each
# flag) and git 2.54 for step 2:
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
# The context count is not here: no step reads context lines since step 5
# was deleted (step 9.3), and GIT_DIFF_OPTS would beat it, so run_git drops
# it from the environment.
DIFF_FLAGS = ['--text', '--no-textconv', '--no-ext-diff', '--no-color',
              '--inter-hunk-context=0', '--diff-algorithm=myers',
              '--indent-heuristic']


# What localedef's line reader counts as space (isspace in the C locale).
# Python's str.strip() also strips U+00A0 and other Unicode spaces, which
# glibc reads as part of a word.
_SPACE = ' \t\n\r\f\v'

# The two directives, in the one place declares_byte_order accepts them.
_DIRECTIVE_RE = re.compile(r'(comment_char|escape_char)[ \t\r\f\v]+(\S)')


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
    `returncode` and a non-zero exit is itself the answer. Never use it to
    READ CONTENT. git prints nothing on stdout when it fails, so a suppressed
    failure is indistinguishable from "there is nothing here", and in this
    tool "nothing here" means "this file did not change" -- a clean verdict,
    produced by not having looked. That is what
    `report_file` and `check_paths` used to do, on a --filter=blob:none clone
    whose fetches really do fail:

        fatal: could not fetch <oid> from promisor remote

    `cat-file --batch` does NOT fail that way: it exits 0 and prints the
    unfetchable blob as `missing` (read_blobs).
    """
    # GIT_DIFF_OPTS is applied AFTER the command line, so `-U3` on the argv
    # does not win: measured, `GIT_DIFF_OPTS=-u0 git diff -U3` returns zero
    # context lines, and step 5's comment tracking (deleted in step 9.3), which
    # read those lines, then hid a comment that closes on one. It is dropped
    # rather than overridden because there is no flag that outranks it.
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


_COMMIT_LINE_RE = re.compile(rb'([0-9a-f]{40}|[0-9a-f]{64}) commit \d+')


def resolve_commits(repo, refs):
    """{ref: commit id} for each ref that names a commit, asked of git in ONE
    `cat-file --batch-check` rather than one `rev-parse` per ref.

    Each request line gets one answer line, in order, at exit 0: the commit,
    or the request followed by `missing` or `ambiguous`. A ref with a newline
    in it would be two requests, so it is not sent and stays unresolved.
    Measured with git 2.54 on the pinned clone against what this replaced,
    `rev-parse --verify <ref>^{commit}`: the same commit for tags and for
    full and short ids, and unresolved wherever rev-parse exits 1 -- a name
    that does not exist, an ambiguous short id, a tree, an empty name, a name
    with spaces or a leading `-`, a tag spelt in the other Unicode form once
    the refs are packed.

    An answer it cannot read dies: a ref left out of the result is called
    missing by check_refs, so an unread line must not become one.
    """
    asked = [r for r in refs if '\n' not in r]
    if not asked:
        return {}
    # Bytes, as argv carries them: a ref that is not UTF-8 is sent as given,
    # and git echoes it back byte for byte.
    requests = [f'{r}^{{commit}}'.encode('utf-8', 'surrogateescape')
                for r in asked]
    env = {k: v for k, v in os.environ.items() if k != 'GIT_DIFF_OPTS'}
    p = subprocess.run(['git', *GIT_CONFIG_OVERRIDES, 'cat-file',
                        '--batch-check'], cwd=repo, env=env,
                       input=b''.join(q + b'\n' for q in requests),
                       capture_output=True)
    if p.returncode != 0:
        die(f"`git cat-file --batch-check` failed in {repo}:\n"
            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    lines = p.stdout.split(b'\n')
    if lines[-1] == b'':
        lines.pop()
    if len(lines) != len(asked):
        die(f"`git cat-file --batch-check` answered {len(lines)} line(s) for "
            f"{len(asked)} ref(s) in {repo}, so no answer can be matched to "
            f"its ref.")
    found = {}
    for ref, request, line in zip(asked, requests, lines):
        m = _COMMIT_LINE_RE.fullmatch(line)
        if m:
            found[ref] = m.group(1).decode()
        elif line not in (request + b' missing', request + b' ambiguous'):
            die(f"unreadable `git cat-file --batch-check` answer for {ref!r}: "
                f"{line!r}")
    return found


def check_refs(repo, *refs, builds=False):
    """Verify every ref resolves, fetching tags once if some are missing.

    A stale clone from an earlier run simply does not have newer tags, and
    `git diff` against a missing tag is a confusing failure at best.

    Returns {ref: commit id}, read after any fetch, for pair_order to take
    instead of asking git again. A ref whose commit build_commit made is
    refused (refuse_build_commits) unless `builds`, which only step 1's
    fan-in passes, for the build it has just made.
    """
    refuse_latest(refs)
    commits = resolve_commits(repo, refs)
    missing = [r for r in refs if r not in commits]
    if not missing:
        if not builds:
            refuse_build_commits(repo, commits)
        return commits
    print(f"note: {', '.join(missing)} not present in {repo}, fetching tags...",
          file=sys.stderr)
    run_git(['fetch', '--tags', '--quiet'], repo)
    # Every ref again, not only the missing ones: the fetch also updates
    # remote-tracking branches, and the steps read every ref after it.
    commits = resolve_commits(repo, refs)
    still = [r for r in refs if r not in commits]
    if still:
        die(f"unknown git ref(s) after fetching tags: {', '.join(still)}")
    if not builds:
        refuse_build_commits(repo, commits)
    return commits


def refuse_build_commits(repo, commits):
    """Die when a ref of {ref: commit id} names a commit build_commit made
    (its committer is BUILD_EMAIL), however it is spelt: its id as step 1
    prints it, its ref under BUILD_REFS, an id from an older run. Read as a
    tag, such a commit is the upstream tag outside localedata/ and locale/,
    and the run then calls it an upstream tag and names none of the
    build's patches (false-negative-reviewer, round 1: `./audit.sh
    glibc-2.34 <the 275 build's id>` ran to exit 0 with its five strcoll
    patches nowhere). A build is read only by its name, through build_side."""
    shas = sorted(set(commits.values()))
    if not shas:
        return
    out = run_git(['log', '--no-walk', '--format=%H %ce', *shas],
                  repo).stdout.decode('utf-8', 'replace').split('\n')
    made = {ln.split(' ', 1)[0] for ln in out
            if ln.partition(' ')[2] == BUILD_EMAIL}
    for ref, sha in commits.items():
        if sha in made:
            die(f"{ref} is a commit this tool made for a distro build: "
                f"outside localedata/ and locale/ it holds the upstream "
                f"tag's tree, so read as a tag it hides the build's own "
                f"patches. Name the build itself (glibc-2.34-283.el9_8) "
                f"instead.")


def warn(text, split_words=True):
    """The `!!` block shape audit.sh collects and repeats at the bottom.

    One copy, in the shared module, because two copies of a formatter drift and
    this repository has already paid for that: the suite's `flat()` exists
    because output wrapped at 78 columns made a negative assertion vacuous, and
    the wrapper's warnings block matches `^!!` followed by three-space
    continuation lines.

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


def require_full_history(repo, step, why):
    """Die unless git says this clone is not shallow; `why` says what the
    step could not tell on a shallow one. Only `true` and `false` are
    answers. `--is-shallow-repository` dates from git 2.15, and an older
    `rev-parse` ECHOES an option it does not know and exits 0 -- which is
    not `true`, so the guard would be off with nothing said. "Could not
    ask" is its own case. Step 2 asks it when descends_from does not
    answer yes (13.7 PR C, node_charmaps.reads_utf8)."""
    shallow = run_git(['rev-parse', '--is-shallow-repository'],
                      repo).stdout.strip()
    if shallow not in (b'true', b'false'):
        die(f"`git rev-parse --is-shallow-repository` answered "
            f"{shallow!r}, which is neither true nor false, so whether "
            f"this clone has the history {step} needs is unknown "
            f"(git older than 2.15?).", 2)
    if shallow == b'true':
        die(f"this glibc clone is shallow, so {why}. Clone without --depth "
            f"(--filter=blob:none is fine).", 2)


def descends_from(repo, ancestor, rev):
    """True or False when git can say whether `rev` descends from
    `ancestor`; None when it cannot (a commit this clone does not hold, a
    fabricated repository). The caller counts None as not known, never as
    no: unlike _is_ancestor, nothing here orders a pair."""
    p = run_git(['merge-base', '--is-ancestor', ancestor, rev], repo,
                allow_fail=True)
    return {0: True, 1: False}.get(p.returncode)


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
    answered. Both refs are resolved to commits before `pair_order` asks --
    by check_refs, or by its own `rev-parse --verify` -- which removes the
    unreadable-rev cause for its calls; a clone that never fetched tags can
    still reach the glob case, and lands on undetermined with neither side
    named, which is the conservative direction.
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


def pair_order(repo, old, new, commits=None):
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
    and a reversed pair ran to the end at exit 0 with a plausible summary --
    step 5 (deleted in step 9.3) a substantive hunk count, no `!!` anywhere.
    What it hides: step 2 names a locale
    DELETED in the real upgrade as one with no file at the old side, a
    harmless-looking addition.
    """
    return order(repo, old, new, commits)


def _shas(repo, old, new, commits):
    """The commit of each of two refs: check_refs' answer when the caller
    has it, so they are not asked twice, else `rev-parse --verify`."""
    if commits is not None:
        return [commits[old], commits[new]]
    shas = []
    for ref in (old, new):
        p = run_git(['rev-parse', '--verify', f'{ref}^{{commit}}'], repo)
        shas.append(p.stdout.decode('utf-8', 'replace').strip())
    return shas


def _is_tag(s):
    return isinstance(s, str) or s.kind == 'tag'


def _ref(s):
    """What git reads for a side: a tag's name, a build's commit."""
    return s if isinstance(s, str) else s.ref


def label(s):
    """How a side is named in what is printed: a tag by its name, a build by
    the name it was asked for (Built.build without its `.src`), a machine by
    its build words."""
    if isinstance(s, str) or s.kind == 'tag':
        return _ref(s)
    return _asked(s.build) if s.kind == 'build' else (s.build or '')


def order(repo, old, new, commits=None):
    """The one function that orders two sides of the pair. Returns (status,
    detail), status one of 'same', 'forward', 'reversed', 'undetermined'
    (pair_order says what each means).

    A side is a Side or, for a tag, its ref. By kind:
      two tags          git, the questions pair_order lists: one commit,
                        ancestry, the release each descends from.
      two machines      the glibc version of each, then, within one
                        version, the build by rpm's own rule
                        (locale_order.build_parts and rpm_vercmp): a
                        distro can change the order inside a release, as
                        RHEL8 did at glibc-2.28-93. 'same' when their
                        words and versions are equal, or rpm says the
                        builds are one; otherwise 'undetermined' when a
                        version or, within one version, a build cannot
                        be read.
      two builds, or a  the same rule as two machines: a build named
      build and a       from its SRPM (side(named=...)) is ordered by rpm,
      machine           never by git, although it is a commit of the
                        clone: two builds of one version are two commits
                        on one tag, which git cannot order (measured:
                        .40/.48 and 275/283 read "could not be
                        established"). Its build ends in `.src`
                        (build_sources.identity), which build_parts reads
                        as the architecture.
      a tag and a       the release of each: the tag's through describe,
      build or machine  the build's or machine's from its version. One
                        release on both sides is 'undetermined': nothing
                        here says whether a machine's build is that tag or
                        later, and a named build is ordered like one.

    rpm's rule stays in locale_order.py, which is copied alone to a
    machine and uses only the standard library; it is imported here, only
    for a pair that holds a machine or a build, so a run on tags never
    loads it.
    """
    if _is_tag(old) and _is_tag(new):
        return _order_by_git(repo, _ref(old), _ref(new), commits)
    # Imported here: rpm's rule lives with the measurement (see above).
    import locale_order as lo
    if not _is_tag(old) and not _is_tag(new):
        return _order_by_build(lo, old, new)
    return _order_by_release(repo, lo, old, new)


def _order_by_git(repo, old, new, commits):
    shas = _shas(repo, old, new, commits)
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


def _order_by_build(lo, old, new):
    pair = f'the new build {label(new)} against the old build {label(old)}'
    # Equal words of equal versions are one build, a version that cannot be
    # read included: a machine known only by its measurement (step 11) has
    # the id it recorded, and a free-text id gives none (backlog 16.62). One
    # side's version
    # missing is not that: nothing says the two are one.
    if old.build and old.build == new.build and old.version == new.version:
        return 'same', f'{pair}: the same words'
    ov, nv = lo.version_tuple(old.version), lo.version_tuple(new.version)
    if ov is None or nv is None:
        return 'undetermined', (f'{pair}: the glibc version of '
                                f'{"the old" if ov is None else "the new"} '
                                f'side cannot be read')
    if ov != nv:
        return ('forward' if ov < nv else 'reversed'), (
            f'{pair}: {old.version} against {new.version}')
    op, np_ = lo.build_parts(old.build), lo.build_parts(new.build)
    if op and np_:
        c = lo.rpm_vercmp(op[0], np_[0]) or lo.rpm_vercmp(op[1], np_[1])
        return {-1: 'forward', 0: 'same', 1: 'reversed'}[c], (
            f'{pair}: one glibc version, ordered by rpm')
    return 'undetermined', (f'{pair}: one glibc version, and the build of at '
                            f'least one cannot be read')


def _order_by_release(repo, lo, old, new):
    def release(s):
        if _is_tag(s):
            found = nearest_glibc_tag(repo, _ref(s))
            return found[1] if found else None
        return lo.version_tuple(s.version)
    r_old, r_new = release(old), release(new)
    pair = f'the new side {label(new)} against the old side {label(old)}'
    if r_old and r_new and r_old != r_new:
        return ('forward' if r_old < r_new else 'reversed'), (
            f'{pair}: release {r_old[0]}.{r_old[1]} against '
            f'{r_new[0]}.{r_new[1]}')
    return 'undetermined', (f'{pair}: one release on both sides, or one that '
                            f'cannot be read, and nothing here places a build '
                            f'against a tag of its own release')


def pair_noun(old, new):
    """What the two sides are, as the word a sentence about both uses:
    'tags' (two tags of the clone: each sentence as it has always been
    printed), 'builds', or 'sides' (a tag and a build)."""
    kinds = (_is_tag(old), _is_tag(new))
    return 'tags' if all(kinds) else 'sides' if any(kinds) else 'builds'


ONE = {'tags': 'tag', 'builds': 'build', 'sides': 'side'}


def nothing_compared(repo, old, new, commits=None):
    """'commit' when two refs are one commit, 'tree' when they are two
    commits of one source tree, else None.

    Kept apart from order(): two builds of one glibc version can be in
    order by rpm and still hold the same sources (measured 2026-11-02:
    .40/.48, 275/283 and 128/145 have the same locale sources), and then
    every step that compares the two refs compares a tree with itself.
    That is not a clean result, whatever the order. For two upstream tags
    the two answers coincide: the five pinned tags have five trees
    (measured on the pinned clone)."""
    shas = _shas(repo, _ref(old), _ref(new), commits)
    if shas[0] == shas[1]:
        return 'commit'
    out = run_git(['rev-parse', f'{shas[0]}^{{tree}}', f'{shas[1]}^{{tree}}'],
                  repo).stdout.decode('utf-8', 'replace').split()
    if len(out) != 2:
        die(f"`git rev-parse` gave {len(out)} tree id(s) for 2 commits in "
            f"{repo}, so whether the two hold the same sources is unknown.")
    return 'tree' if out[0] == out[1] else None


# For two builds, or a tag and a build: what was not compared is what this
# tool does not read of a build at all, not a distro's patches.
_NOTHING_COMPARED_BUILDS = (
    "Step 1 can only report 'nothing changed', and step 2 reports "
    "that nothing was compared: the two {noun} hold the same locale "
    "sources and the same code under locale/. What can still differ is "
    "what this tool does not read of a build, which step 1 prints under "
    "'Not checked for these builds'. sql/c_utf8_probe.sql and "
    "docs/confirming-on-a-real-system.md check the order on the machines "
    "themselves.")


# For two machines' files (machine_side): the two hold the same locale
# sources, and the same charmaps when both files hold them, which is a
# result; what no step reads of a machine is said where step 1 names them.
# A file without charmaps leaves them out of its commit (machine_commit), so
# two such files of one source tree are one tree with no charmap read: the
# sentence then says the character sets were not compared, never that they
# are the same (false-negative-reviewer and doc-sweep, 9.4a round 2).
def nothing_compared_machines(charmaps):
    """The machines' sentence for one source tree; `charmaps` is whether
    both files hold charmaps this reads."""
    same = ("The two machines' files hold the same locale sources and "
            "charmaps, so no locale's sort rules can differ between them, "
            "and steps 1 and 2 have nothing to compare."
            if charmaps else
            "The two machines' files hold the same locale sources, so no "
            "locale's sort rules can differ between them, and steps 1 and 2 "
            "have nothing to compare; their character sets were not "
            "compared, as a file holds no charmaps this reads.")
    return (same + " What can still differ is what no step reads of a "
            "machine, which step 1 prints under 'Not checked for these "
            "machines', and how each machine's glibc sorts, which step 11 "
            "measures. sql/c_utf8_probe.sql and "
            "docs/confirming-on-a-real-system.md check the order on the "
            "machines themselves.")


def machines_charmaps(old, new):
    """Do both machine sides' files hold charmaps this reads?"""
    return all(s.prov.get('charmaps') is not None for s in (old, new))


def is_machine(s):
    """Is the side a build read from a machine's own files (machine_side)?"""
    return (not _is_tag(s) and isinstance(s.prov, dict)
            and bool(s.prov.get('machine')))


_NOTHING_COMPARED_REST = (
    "Step 1 can only report "
    "'nothing changed', which for an intra-major upgrade "
    "(RHEL 8.1 -> 8.2, say) says nothing at all, and step 2 reports "
    "that nothing was compared. The distro's own "
    "builds are where such a change lives. With each machine's file "
    "(--old-node and --new-node), step 11 measures how each machine "
    "sorts, and steps 1 and 2 compare the two machines' own locale "
    "sources, when the files hold them; "
    "sql/c_utf8_probe.sql checks C.UTF-8 on each "
    "machine. C.UTF-8's order moved in glibc-2.28-93.el8 with the "
    "upstream tag unchanged. See docs/results.md.")


def pair_check(repo, old, new, allow_reverse=False, commits=None):
    """The pair's order, and what it found: (status,
    nothing_compared's answer). The steps that skip a read, or refuse a
    clean verdict, when nothing was compared take the second; the
    direction is the first."""
    status, detail = pair_order(repo, old, new, commits)
    one = ONE[pair_noun(old, new)]
    if status == 'reversed' and not allow_reverse:
        die(f"{detail}.\n"
            f"       This pair is REVERSED. Every step takes <old_{one}> "
            f"<new_{one}>, and reversed\n"
            f"       they all run to the end: step 2 reports a deleted "
            f"locale as a harmless\n"
            f"       addition and compares every locale the wrong way "
            f"round. Swap the arguments.")
    # Whatever the order: a reversed pair run on request, or one git cannot
    # order, of one source tree compares nothing either; the clone holds
    # such pairs on glibc's own branches.
    nothing = nothing_compared(repo, old, new, commits)
    noun = pair_noun(old, new)
    one = ONE[noun]
    machines = is_machine(old) and is_machine(new)
    rest = (_NOTHING_COMPARED_REST if noun == 'tags' else
            nothing_compared_machines(machines_charmaps(old, new))
            if machines else
            _NOTHING_COMPARED_BUILDS.format(noun=noun, one=one))
    source = 'upstream source' if noun == 'tags' else 'that commit'
    old, new = label(old), label(new)
    if nothing == 'commit':
        warn(f"{old} and {new} are the same commit. Step 1 compares "
             f"{source} against itself, and step 2 compares nothing. {rest}",
             split_words=False)
    elif nothing == 'tree' and machines:
        warn(f"{old} and {new}: {rest[0].lower()}{rest[1:]}",
             split_words=False)
    elif nothing == 'tree':
        warn(f"{old} and {new} are two commits of one source tree. Step 1 "
             f"compares that tree against itself, and step 2 compares "
             f"nothing. {rest}", split_words=False)
    if status == 'reversed':
        warn(f"REVERSED PAIR, allowed on request: {detail}. Every finding "
             f"below has old and new the other way round -- what reads as "
             f"added was deleted in the real upgrade. Not an audit of an "
             f"upgrade.")
    elif status == 'undetermined':
        warn(f"The DIRECTION of this pair could not be established: {detail}. "
             f"Nothing below has checked it: if the two arguments are the "
             f"wrong way round, every "
             f"step still runs, and step 2 reports "
             f"a deleted locale as a harmless addition. "
             f"Confirm which build is the older one.")
    return status, nothing


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


def read_blobs(repo, tag, paths, raw=False):
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
    treats `missing` as a read that failed, as read_blobs_strict and
    require_read do.

    raw=True gives each blob's bytes as they are, for glibc_reader, which
    reads bytes the way glibc does and never a decoded text.
    """
    return read_blobs_at(repo, [(tag, paths)], raw=raw)[0]


# The texts read_blobs_at decoded from bytes that held 0xFF. errors='replace'
# turns 0xFF and every other byte that is not UTF-8 into the same U+FFFD,
# and glibc before 2.36 reads only 0xFF as the end of a string, a section
# or the file (signed char, linereader.h:137@2.35). No locale or charmap
# file at 2.12..2.42 holds one (measured 2026-10-11, 3045 blobs).
TEXTS_WITH_0XFF = set()


def decoded(raw):
    """A locale or charmap file's bytes as the readers here take them:
    UTF-8 with errors='replace', and the text registered in TEXTS_WITH_0XFF
    when the bytes held 0xFF. read_blobs_at decodes every blob this way, a
    machine commit's included, so that collation_keys reads them all
    alike."""
    text = raw.decode('utf-8', 'replace')
    if b'\xff' in raw:
        TEXTS_WITH_0XFF.add(text)
    return text


def read_blobs_at(repo, groups, raw=False):
    """read_blobs for several (tag, paths) groups in ONE `git cat-file
    --batch`, so a step that reads at two tags starts one git, not one per
    read. Returns [(contents, missing), ...], one pair per group, in order;
    with raw=True the contents are the blobs' bytes, not decoded().
    """
    groups = [(tag, list(paths)) for tag, paths in groups]
    results = [({}, set()) for _ in groups]
    wanted = [(i, tag, path) for i, (tag, paths) in enumerate(groups)
              for path in paths]
    if not wanted:
        return results
    req = ''.join(f'{tag}:{p}\n' for _, tag, p in wanted).encode()
    p = subprocess.run(['git', 'cat-file', '--batch'], cwd=repo,
                       input=req, capture_output=True)
    if p.returncode != 0:
        die(f"`git cat-file --batch` failed in {repo}:\n"
            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    out = p.stdout
    pos = 0
    for i, tag, path in wanted:
        contents, missing = results[i]
        nl = out.find(b'\n', pos)
        if nl < 0:
            read = sum(len(c) for c, _ in results)
            die(f"truncated `git cat-file --batch` output while reading "
                f"{tag}:{path} (read {read} of {len(wanted)} blobs)")
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
        # The body and the newline after it, or the stream was cut short:
        # a slice past the end is shorter, not an error, and a cut file
        # would be judged as if it were whole.
        if out[pos + size:pos + size + 1] != b'\n':
            die(f"truncated `git cat-file --batch` output inside {tag}:{path}: "
                f"its header promised {size} bytes")
        body = out[pos:pos + size]
        contents[path] = body if raw else decoded(body)
        pos += size + 1  # body plus its trailing newline
    if pos != len(out):
        die(f"`git cat-file --batch` printed {len(out) - pos} byte(s) more "
            f"than the {len(wanted)} answer(s) asked for, so they may not "
            f"line up with the requests.")
    return results


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
    require_read(tag, paths, missing, what)
    return contents


def require_read(tag, paths, missing, what):
    """read_blobs_strict's refusal, for a caller that read in a batch of its
    own (read_blobs_at)."""
    if missing:
        die(f"{len(missing)} of {len(paths)} file(s) listed at {tag} could not "
            f"be read while building {what}: "
            f"{', '.join(sorted(missing)[:5])}"
            f"{', ...' if len(missing) > 5 else ''}.\n"
            f"       These came from the tree at {tag}, so they exist -- the "
            f"read failed. On a --filter=blob:none clone that usually means a "
            f"fetch could not happen. Re-run with the promisor reachable.")


def list_locale_files(repo, tag):
    """Every file under localedata/locales/ at `tag`, as repo-relative paths:
    side(repo, tag).paths, so the floor is applied where every side is
    built. Kept under this name for the callers that only need the list."""
    return side(repo, tag).paths


def _require_corpus(paths, tag, kind='tag'):
    """Aborts below the floor. Every caller builds a result from this list,
    and a result built from nothing reads as "nothing changed", which is
    the reassuring direction. The node-reading modes had this guard from
    the start; the tag modes did not."""
    floor = below_floor(len(paths))
    if floor is not None:
        die(f"only {len(paths)} file(s) under {LOCALES_DIR}/ at {tag}, below "
            f"the floor of {floor}. That is not a glibc locale "
            + ("corpus -- a restructured tree, or a tag from before the "
               "directory existed. " if kind == 'tag' else "corpus. ")
            + f"Refusing to report: an empty corpus reads "
            f"as 'nothing changed' and 'no locale uses ellipsis ranges', "
            f"which is indistinguishable from a clean run.")


def list_tree_ids(repo, tag, extra):
    """(locale paths as list_locale_files gives them, {path: blob id}) of
    the locales and of every file under the `extra` paths, from ONE `git
    ls-tree -r`: side(repo, tag, extra=extra). Step 2 compares charmaps by
    blob id (collation_keys), and asking for them in the listing it already
    makes costs no git process."""
    s = side(repo, tag, extra=extra)
    return s.paths, s.ids


# One side of an audited pair. kind is 'tag' (a glibc ref of the clone:
# ref, paths and ids), 'build' (a build named from its SRPM, made a commit
# of the clone: ref is that commit, build Built.build, version 'glibc X.Y',
# paths and ids as a tag's) or 'machine' (a machine known only by its
# measurement, as step 11 knows it: its glibc in build, the words `rpm -q
# glibc` printed, and version, the measurement's 'glibc X.Y'; no root, no
# paths, no ref). prov is a build's provenance (build_sources: what was
# fetched, checked and applied, its tag, its patches to strcoll's code),
# None on a tag or a machine; no default, so a constructor cannot forget it.
# A 'build' Side is also made from a machine's own file (machine_side: its
# commit made from the file, build `<name>.src`, prov naming the machine's
# folder, `rpm -q` word, measurement sha256, what was skipped and its
# charmaps; is_machine).
Side = collections.namedtuple(
    'Side', 'kind ref root build version paths ids skipped prov')


def side(repo, ref=None, extra=(), build=None, version=None, named=None,
         make=False):
    """The one function that builds a side of the pair, and applies the
    floor of locale files to it.

    Where the side comes from, in this order: a build named as `rpm -q
    glibc` names it without the architecture (`named`,
    glibc-2.34-283.el9_8: build_side), a machine known by its measurement
    only (`build` and `version`, no directory, so nothing to count), else
    the tag `ref` of the clone, listed with one `git ls-tree -r` over
    localedata/locales/ and the `extra` paths. `build=` is a machine's and
    `named=` a build's: two keywords, so that neither is ever read as the
    other, and both at once is refused. `make` is build_side's, which
    step 1 passes.

    The floor (below_floor) is applied here once for every side that holds
    files, so no step can refuse a smaller corpus on one side than on the
    other. A tag side's list is split on newlines only: git ends each name
    there, and under core.quotePath=false prints U+0085, U+2028 and U+2029
    as they are, where splitlines() also cuts -- a file
    `x<U+2028>localedata/locales/C` listed a C that is not there, and step
    2 then dropped its C.UTF-8 note (false-negative-reviewer, measured with
    git 2.54). A line that is not `<mode> <type> <id>` and a tab dies,
    because a path read without its id would compare as missing.
    """
    if named is None and build is None and is_build_name(ref):
        # A build given where a tag goes (a step's argument): read as a
        # build, never asked of git by its name.
        named, ref = ref, None
    if named is not None:
        if build is not None or version is not None:
            die("side() takes a machine's build= and version= or a build's "
                "named=, not both")
        return build_side(repo, named, extra, make=make)[0]
    if ref is None:
        return Side('machine', None, None, build, version, None, None, (),
                    None)
    return _listed('tag', repo, ref, extra, build, version)


def _listed(kind, repo, ref, extra, build=None, version=None, prov=None):
    """A side of the clone at `ref`, a tag or a build's commit, listed with
    one `git ls-tree -r` and held to the floor."""
    out = run_git(['ls-tree', '-r', ref, '--', LOCALES_DIR + '/', *extra],
                  repo).stdout.decode('utf-8', 'replace')
    paths, ids = [], {}
    for ln in out.split('\n'):
        if not ln.strip():
            continue
        meta, tab, path = ln.partition('\t')
        fields = meta.split(' ')
        if not tab or len(fields) != 3:
            die(f"unreadable `git ls-tree` line at {ref}: {ln!r}")
        ids[path] = fields[2]
        if path.startswith((LOCALES_DIR + '/', f'"{LOCALES_DIR}/')):
            paths.append(path)
    _require_corpus(paths, ref if kind == 'tag' else _asked(build), kind)
    return Side(kind, ref, None, build, version, paths, ids, (), prov)


# The fetch build_side uses; a test hands its own (no network).
def _fetch(name, repo):
    import build_sources as bs
    return bs.fetch_build(name, repo=repo)


def release_el(release):
    """(major, minor or None) from a release's `.elN` or `.elN_M`; None
    unless there is exactly one."""
    found = re.findall(r'\.el(\d+)(?:_(\d+))?(?=\.|$)', release)
    if len(found) != 1:
        return None
    major, minor = found[0]
    return int(major), (int(minor) if minor else None)


def build_name(name):
    """(version, release, major, minor) of a build named as `rpm -q glibc`
    names it without the architecture, `glibc-2.34-283.el9_8`, or None.
    Read whole: no word is taken for an architecture and dropped, since
    glibc-2.39-46.el10_0 is a real build and so is glibc-2.39-46.el10_0.4.
    The release has the shape of the 66 on Rocky's mirror (251.el8_10.48,
    83.el9.12, 189.1.el8): numbers, `.elN` or `.elN_M`, numbers. Here, and
    not in build_sources, so that a run on two tags never imports it."""
    m = re.match(r'^glibc-(\d+\.\d+(?:\.\d+)?)-'
                 r'(\d+(?:\.\d+)*\.el\d+(?:_\d+)?(?:\.\d+)*)$',
                 name or '')
    el = release_el(m.group(2)) if m else None
    if not m or el is None:
        return None
    return m.group(1), m.group(2), el[0], el[1]


def is_build_name(name):
    """Does `name` read as a build, glibc-2.34-283.el9_8? No glibc tag does
    (125 tags glibc-[0-9]* in the mirror, none with an .elN release;
    measured 2026-11-03), so such a name is never asked of git as a ref: a
    build is read only through build_side."""
    return build_name(name) is not None


def without_arch(name):
    """`name` without the architecture `rpm -q glibc` ends each word with,
    when what is left is a build: glibc-2.34-275.el9_8.x86_64 is
    glibc-2.34-275.el9_8 (backlog 16.79). Anything else is returned as it
    is: a tag, a build without one, and a word whose last part is a
    release's own (glibc-2.39-46.el10_0, glibc-2.28-251.el8_10.40), which
    locale_order.build_parts does not read as an architecture."""
    if is_build_name(name):
        return name
    import locale_order as lo
    parts = lo.build_parts(name) if len((name or '').split()) == 1 else None
    if parts:
        bare = 'glibc-%s-%s' % parts
        if is_build_name(bare):
            return bare
    return name


def machine_build(words):
    """The build a machine runs, from what `rpm -q glibc` printed there
    (a machine's file keeps it whole): each word without its
    architecture. A machine with glibc of 64 and of 32 bits prints two
    words, and they are one build: dnf installs and moves the two
    together (measured 2026-11-04 on Rocky 9, glibc.i686 beside x86_64).
    Dies when the words give two builds, since then which one the steps
    should read is not known, and when a word does not read as `rpm -q`
    prints one."""
    import locale_order as lo
    got = []
    for w in (words or '').split():
        parts = lo.build_parts(w)
        if parts is None:
            die(f"`rpm -q glibc` printed {words!r} on that machine, and "
                f"{w!r} does not read as a package and its architecture, "
                f"so the build it runs is not known.")
        got.append('glibc-%s-%s' % parts)
    if not got:
        die("that machine's file names no glibc build")
    if len(set(got)) != 1:
        die(f"`rpm -q glibc` printed {words!r} on that machine: two builds, "
            f"{' and '.join(sorted(set(got)))}, so which one steps 1 and 2 "
            f"should read is not known.")
    return got[0]


def machine_sides(given, words, no_sources=(None, None)):
    """What steps 1 and 2 read for each side when audit.sh is given each
    machine's file: [(name, kind, tag)], kind 'build' or 'tag', tag the
    upstream tag of that side, which step 11 reads; and the lines
    to print, `!!` blocks included.

    `given` are the two sides as typed, `words` what `rpm -q glibc` printed
    on each machine, `no_sources` for each side None when its file holds
    the machine's locale sources, else the reason the file gives for
    holding none. A side typed must be that machine's build, with or
    without the architecture, or its upstream tag; anything else dies, as
    does latest-elN (the machine exists, its file names its build).

    A side whose build is read is read from the machine's own files
    (machine_side, through MACHINES_ENV): nothing is downloaded. When a
    side cannot be read that way -- its file holds no locale sources, or
    its build is not a RHEL build this tool reads -- BOTH sides read their
    tags, and a `!!` says which machine, why, and what the tags lack: a tag
    against a build of one release cannot even be ordered. That holds for
    a side typed as its tag; a side typed as the build itself dies."""
    builds = [machine_build(w) for w in words]
    sides = ('old', 'new')
    # machine_build gives glibc-<version>-<release>.
    own = [('glibc-' + b.split('-', 2)[1], b) for b in builds]
    typed = [without_arch(t) for t in given]
    if typed[0] not in own[0] and typed[1] not in own[1] and \
            typed[0] in own[1] and typed[1] in own[0]:
        die(f"the old machine's file names {builds[0]} and the new "
            f"machine's {builds[1]}, which are the other way round from "
            f"{given[0]} -> {given[1]}: pass the machine you upgrade FROM "
            f"first.")
    tags = []
    for side, typed, b in zip(sides, given, builds):
        tag = 'glibc-' + b.split('-', 2)[1]
        if latest_major(typed) is not None:
            die(f"{typed} stands for a machine not installed yet, and the "
                f"{side} machine's file names the build it runs, {b}: give "
                f"{tag} or {b}.")
        if without_arch(typed) not in (b, tag):
            die(f"the {side} machine's file names {b}, and the {side} side "
                f"given is {typed}: give that build's tag, {tag}, or the "
                f"build itself.")
        tags.append(tag)
    import glibc_reader as gr
    unread = []
    for side, typed, b, why in zip(sides, given, builds, no_sources):
        # A side typed as the build itself asks for that build: one that
        # cannot be read is refused, as it is without a machine's file.
        # Only a side typed as its tag reads the tag in its place. A build
        # of a RHEL major whose keyword tables this tool does not hold
        # (glibc_reader.RHEL_BUILDS: 8, 9 and 10) is not read as one: its
        # localedef could read words the tag's tables do not have.
        asked = without_arch(typed) == b
        major = (build_name(b) or (None, None, None))[2]
        if not is_build_name(b) or f'rhel{major}' not in gr.RHEL_BUILDS:
            why = ("the name does not read as a build of RHEL 8, 9 or 10, "
                   "the releases whose localedef this tool reads")
        elif why is not None:
            why = f"it holds no locale sources, because {why}"
        else:
            continue
        if asked:
            die(f"cannot read the build {b}: {why}", 2)
        unread.append((side, b, why))
    if not unread:
        lines = []
        if list(given) != builds:
            lines.append(textwrap.fill(
                f"Each machine's file names the build it runs: steps 1 and 2 "
                f"read {builds[0]} and {builds[1]}, in place of {given[0]} "
                f"and {given[1]}, from each machine's own locale sources.",
                width=78, break_long_words=False, break_on_hyphens=False))
        return [(b, 'build', t) for b, t in zip(builds, tags)], lines
    out = io.StringIO()
    said = '; and '.join(f"the {side} machine's file names {b}, which this "
                         f"run cannot read: {why}" for side, b, why in unread)
    with contextlib.redirect_stdout(out):
        warn(f"{said[0].upper()}{said[1:]}. So steps 1 and 2 read the tags "
             f"{tags[0]} and {tags[1]}, which hold neither build's patches.",
             split_words=False)
    return [(t, 'tag', t) for t in tags], out.getvalue().rstrip('\n').split('\n')


def latest_major(name):
    """N of `latest-elN`, which stands for the newest build of RHEL N
    Rocky Linux's mirror lists, else None. Only audit.sh reads it, and
    resolves it once for the whole run (`latest`); every step that asks git
    for its sides refuses it (refuse_latest). Step 11's --tags, which asks
    no git, reads it as it reads a build's name: "names no glibc release"."""
    m = re.fullmatch(r'latest-el(\d+)', name or '')
    return int(m.group(1)) if m else None


def refuse_latest(names):
    """Die on the first of `names` that is latest-elN, before git is asked
    anything: read as a tag it made git fetch every tag and then call it an
    unknown ref."""
    for n in names:
        if latest_major(n) is not None:
            die(f"{n} is resolved only by audit.sh, once for the whole run, "
                f"so that every step reads the same build: run audit.sh, or "
                f"give the build's name it prints.")


def latest_line(name, got):
    """The line audit.sh prints for `name` (latest-elN) resolved to `got`
    (build_sources.Latest): which build, where and as of when. Printed once,
    before step 1; neither the URL nor the date is ever cut, so the one can
    be copied whole and the other read on one line."""
    stamp = got.timestamp or ''
    if stamp.isdigit():
        import datetime
        when = datetime.datetime.fromtimestamp(
            int(stamp), datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        # Held together while wrapping: NUL is in no date and no URL.
        when = "by its metadata of " + when.replace(' ', '\x00')
    else:
        when = "and its metadata gives no date this reads"
    return textwrap.fill(
        f"{name} is {got.build}: the newest glibc build in "
        f"{got.mirror}{got.tree}, {when}.", width=78,
        break_long_words=False, break_on_hyphens=False).replace('\x00', ' ')


def _asked(build):
    """A build as it is asked for: Built.build without the `.src`
    build_sources.identity adds."""
    b = build or ''
    return b[:-len('.src')] if b.endswith('.src') else b


# What the steps read of glibc's line reader by ANCESTRY, not from a tree:
# whether a commit holds LR_GETC_FIX (glibc_tree._pre236, step 2's
# predates_lr_getc_fix) and collation_keys.UTF8_READER (step 2's
# reads_utf8). Those two commits change these two files and NEWS, nothing
# else (measured 2026-11-03). A build's commit has the ancestry of its
# upstream tag, so the answer speaks for the build only while the build
# leaves these files as the tag has them.
LINE_READER = ('locale/programs/linereader.c', 'locale/programs/linereader.h')


def _need_tags(repo, tags):
    """Fetch tags once when one of `tags` is missing from the clone; die
    naming any still missing. A clone older than a build lacks the tag the
    build is made on, so a build side needs this as much as a tag does."""
    tags = list(dict.fromkeys(tags))
    if not tags:
        return
    missing = [t for t in tags if t not in resolve_commits(repo, tags)]
    if not missing:
        return
    print("Refs not present locally: %s -- fetching tags..."
          % ' '.join(missing), flush=True)
    p = subprocess.run(['git', 'fetch', '--tags', '--quiet'], cwd=repo)
    if p.returncode:
        sys.exit(p.returncode)
    found = resolve_commits(repo, tags)
    for t in tags:
        if t not in found:
            print(f"error: unknown git ref '{t}' even after fetching tags",
                  file=sys.stderr)
            sys.exit(2)


# Under audit.sh, the file where step 1 records the commit it made for each
# build of the run, `<build>\t<commit>` a line; the steps after it take the
# commit from there instead of making it again (0.46-0.61 s a side and a
# step, against 0.06-0.08 s, measured 2026-11-03). audit.sh removes the file
# before step 1 and exports its path to its steps only: a step run on its
# own makes the commit.
SIDES_ENV = 'PG_GLIBC_AUDIT_SIDES'


def write_recorded(builds):
    """Write `<build>\t<commit>` for each build Side to the file SIDES_ENV
    names, whole or not at all; nothing when the variable is unset. One
    line a build: one build given as both sides is one line."""
    path = os.environ.get(SIDES_ENV)
    if not path:
        return
    lines = dict.fromkeys(f"{_asked(s.build)}\t{s.ref}\n" for s in builds)
    tmp = f"{path}.new-{os.getpid()}"
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.writelines(lines)
    os.replace(tmp, path)


def _recorded(repo, name, built):
    """The commit step 1 of this run made for `name`, from the file
    SIDES_ENV names, checked to be a commit build_commit made (its
    committer) for this very build (its message names the tag, the build
    and the SRPM's sha256). Dies when the file is missing, does not name
    the build, or names a commit that fails either check."""
    import build_sources as bs
    return _recorded_commit(
        repo, name, bs.commit_message(built),
        f"another build or another SRPM (its message does not name "
        f"{built.tag}, {built.build} and the SRPM's sha256)")


def _recorded_commit(repo, name, message, other):
    """The commit SIDES_ENV's file records for `name`, checked to be a
    commit this tool made (its committer) whose message is `message`;
    `other` says what a commit with another message was made for."""
    path = os.environ[SIDES_ENV]
    try:
        with open(path, encoding='utf-8') as fh:
            lines = fh.read().split('\n')
    except OSError as e:
        die(f"cannot read the build {name}: {SIDES_ENV} names {path}, where "
            f"step 1 of this run records each build's commit, and it cannot "
            f"be read ({e.strerror}).", 2)
    found = [ln.split('\t')[1] for ln in lines
             if ln.count('\t') == 1 and ln.split('\t')[0] == name]
    if len(found) != 1:
        die(f"cannot read the build {name}: {path}, where step 1 of this run "
            f"records each build's commit, names it {len(found)} time(s).", 2)
    commit = found[0]
    out = run_git(['log', '-1', '--format=%ce%x00%B', commit],
                  repo).stdout.decode('utf-8', 'replace')
    email, _, got = out.partition('\0')
    if email != BUILD_EMAIL:
        die(f"cannot read the build {name}: {path} names {commit}, which "
            f"this tool did not make for a build (its committer is "
            f"{email!r}).", 2)
    if got.rstrip('\n') != message.rstrip('\n'):
        die(f"cannot read the build {name}: {path} names {commit}, a "
            f"commit this tool made for {other}.", 2)
    return commit


# Under audit.sh given each machine's file, the file that names, for the
# build each machine runs, the folder that machine's file was laid out in
# (`<build>\t<folder>` a line, the build as rpm -q glibc prints it without
# the architecture). A step that reads that build reads the machine's own
# locale sources from there (machine_side) and downloads nothing. audit.sh
# writes it before step 1 and exports its path to its steps only.
MACHINES_ENV = 'PG_GLIBC_AUDIT_MACHINES'
MACHINE_REFS = 'refs/pg-glibc-audit/machines/'


def machine_folders(name):
    """The folders MACHINES_ENV's file names for the build `name`, in the
    file's order: none when the variable is unset or the file does not
    name it. A file that cannot be read, or a line that is not two fields,
    dies: read as "no machine" it would send the step to download the
    build in place of the machine's own files."""
    path = os.environ.get(MACHINES_ENV)
    if not path:
        return []
    try:
        with open(path, encoding='utf-8') as fh:
            lines = [ln for ln in fh.read().split('\n') if ln]
    except OSError as e:
        die(f"{MACHINES_ENV} names {path}, where audit.sh records each "
            f"machine's laid-out file, and it cannot be read ({e.strerror}).",
            2)
    found = []
    for ln in lines:
        fields = ln.split('\t')
        if len(fields) != 2 or not fields[0] or not fields[1]:
            die(f"{path}, where audit.sh records each machine's laid-out "
                f"file, holds a line that is not a build and a folder: "
                f"{ln!r}", 2)
        if fields[0] == name:
            found.append(fields[1])
    return found


def machine_message(tag, name, sha):
    """The message of the commit made from a machine's own files: the tag
    it is made on, the build the machine runs and the sha256 of its
    measurement, so two machines' commits are two commits and step 1's
    record can be checked against the machine it names."""
    return ('pg-glibc-audit: %s with localedata/locales/ and '
            'localedata/charmaps/ of the machine that runs %s\n\n'
            'measurement sha256 %s\n' % (tag, name, sha))


def _machine_files(folder):
    """(locale names, skipped entries, node_charmaps.CharmapsDir or None,
    why there is none or None) of a machine's laid-out file. The names are
    those node_entries reads as regular files: a link or a folder among the
    sources is skipped and named, never refused. The charmaps are read as
    glibc's charmap_open finds them (NAME, NAME.gz or NAME.bz2); a folder
    CharmapsDir cannot read whole gives none, with its reason, never a part
    of them."""
    import node_charmaps as nc
    names, skipped = node_entries(os.path.join(folder, 'locales'))
    cdir = os.path.join(folder, 'charmaps')
    if os.path.isdir(cdir):
        try:
            return names, skipped, nc.CharmapsDir(cdir), None
        except nc.Unusable as e:
            return names, skipped, None, str(e)
    try:
        with open(os.path.join(folder, 'charmaps-not-included'),
                  encoding='utf-8') as fh:
            why = fh.read().strip()
    except OSError:
        why = ''
    return names, skipped, None, why or "the file gives no reason"


def machine_commit(repo, tag, folder, name, sha):
    """The commit of the machine that runs `name`, made from its laid-out
    file `folder`, and MACHINE_REFS + name pointed at it.

    Its one parent is `tag`; its tree is the tag's with
    localedata/locales/ replaced by the machine's locale sources and
    localedata/charmaps/ by its charmaps, or left out when the file holds
    none: a charmap of the tag in its place would be compared as if the
    machine had it. Everything else is the tag's (locale/, SUPPORTED,
    intl/locale.alias), as a machine's file holds none of it; the run says
    so where it names each machine. Made with build_sources' fixed identity
    and the same checks as build_commit: one parent, nothing outside
    localedata/locales/ and localedata/charmaps/ different from the tag."""
    import tempfile
    import build_sources as bs
    import node_charmaps as nc
    names, _, charmaps, _ = _machine_files(folder)

    def hashed(paths, files):
        ids = bs._git(repo, ['hash-object', '-w', '--no-filters',
                             '--stdin-paths'],
                      ''.join(p + '\n' for p in paths).encode()) \
            .decode().split()
        if len(ids) != len(paths):
            raise bs.Refused('`git hash-object` gave %d id(s) for %d file(s) '
                             'of %s' % (len(ids), len(paths), folder))
        return {f: ('blob', '100644', i) for f, i in zip(files, ids)}

    try:
        local = {}
        cm_tree = None
        if charmaps is not None:
            # Uncompressed, under the name glibc opens: the tag's charmap
            # and the machine's same bytes are one blob.
            cm_names = sorted(charmaps.files)
            with tempfile.TemporaryDirectory() as tmp:
                paths = []
                for i, c in enumerate(cm_names):
                    p = os.path.join(tmp, str(i))
                    with open(p, 'wb') as fh:
                        fh.write(charmaps.raw(c))
                    paths.append(p)
                cm_tree = hashed(paths, cm_names)
        for ent in bs._git(repo, ['ls-tree', '-z', tag + ':localedata']) \
                .split(b'\0'):
            if not ent:
                continue
            meta, _, n = ent.partition(b'\t')
            mode, kind, oid = meta.decode().split(' ')
            local[n.decode('utf-8', 'surrogateescape')] = (kind, mode, oid)
        local['locales'] = ('tree', hashed(
            [os.path.join(folder, 'locales', n) for n in names], names))
        local.pop('charmaps', None)
        if cm_tree is not None:
            local['charmaps'] = ('tree', cm_tree)
        root = {}
        for ent in bs._git(repo, ['ls-tree', '-z', tag + '^{tree}']) \
                .split(b'\0'):
            if not ent:
                continue
            meta, _, n = ent.partition(b'\t')
            mode, kind, oid = meta.decode().split(' ')
            root[n.decode('utf-8', 'surrogateescape')] = (kind, mode, oid)
        root['localedata'] = ('tree', '040000',
                              bs._mktree(repo, local, missing=True))
        tree = bs._mktree(repo, root, missing=True)
        parent = bs._git(repo, ['rev-parse', '--verify',
                                tag + '^{commit}']).decode().strip()
        commit = bs._git(repo, ['commit-tree', '--no-gpg-sign', tree, '-p',
                                parent],
                         machine_message(tag, name, sha).encode()) \
            .decode().strip()
        parents = bs._git(repo, ['rev-list', '--parents', '-n', '1',
                                 commit]).decode().split()
        if parents != [commit, parent]:
            raise bs.Refused('the commit made for the machine that runs %s '
                             'has parents %r, not %s alone'
                             % (name, parents[1:], tag))
        outside = bs._git(repo, ['diff-tree', '-r', '--name-only', parent,
                                 commit, '--', '.',
                                 ':(exclude)localedata/locales',
                                 ':(exclude)localedata/charmaps']) \
            .decode().split('\n')
        outside = [p for p in outside if p]
        if outside:
            raise bs.Refused('the commit made for the machine that runs %s '
                             'differs from %s outside localedata/locales/ '
                             'and localedata/charmaps/: %s'
                             % (name, tag, ', '.join(outside[:5])))
        bs._git(repo, ['update-ref', MACHINE_REFS + name, commit])
    except (bs.Refused, nc.Unusable) as e:
        die(f"cannot read the machine that runs {name}: {e}", 2)
    return commit


def _measurement_sha(folder):
    """The sha256 of the measurement a machine's laid-out file holds."""
    import hashlib
    try:
        with open(os.path.join(folder, 'locale_order.out'), 'rb') as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError as e:
        die(f"cannot read the measurement in {folder}: {e.strerror}", 2)


def machine_side(repo, name, folders, extra=(), make=False):
    """(Side, None) of the build `name` read from the machines' own files
    in `folders` (MACHINES_ENV): no download. A build Side like
    build_side's -- its build `<name>.src` and its version 'glibc X.Y', so
    it is ordered and printed as a build is -- whose commit is
    machine_commit's and whose prov says which machine's files it holds.

    Two machines that run one build (both sides of a pair) are one side
    only when their files hold the same locale sources and charmaps; two
    machines of one build whose files differ die, since which of them a
    step reads would be a guess. Under audit.sh a step after step 1 takes
    the commit step 1 recorded (SIDES_ENV), checked against its message."""
    parsed = build_name(name)
    tag = 'glibc-' + parsed[0]
    _need_tags(repo, [tag])
    folder = folders[0]
    for f in folders:
        if not os.path.isdir(os.path.join(f, 'locales')):
            die(f"cannot read the machine that runs {name}: its file, laid "
                f"out in {f}, holds no locale sources", 2)
    sha = _measurement_sha(folder)
    if os.environ.get(SIDES_ENV) and not make:
        commit = _recorded_commit(
            repo, name, machine_message(tag, name, sha),
            "another machine (its message does not name "
            f"{tag}, {name} and that machine's measurement)")
    else:
        commit = machine_commit(repo, tag, folder, name, sha)
        for other in folders[1:]:
            twin = machine_commit(repo, tag, other, name,
                                  _measurement_sha(other))
            trees = [run_git(['rev-parse', '--verify', c + '^{tree}'], repo)
                     .stdout.decode().strip() for c in (commit, twin)]
            if trees[0] != trees[1]:
                die(f"the two machines run {name} and their files hold "
                    f"different locale sources or charmaps, so which of them "
                    f"a step should read is not known.", 2)
        # The ref names the commit of the first folder, the one recorded.
        run_git(['update-ref', MACHINE_REFS + name, commit], repo)
    names, skipped, charmaps, why = _machine_files(folder)
    try:
        with open(os.path.join(folder, 'build-id'), encoding='utf-8') as fh:
            word = fh.read().strip()
    except OSError as e:
        die(f"cannot read the build id in {folder}: {e.strerror}", 2)
    prov = {'tag': tag, 'machine': folder, 'word': word,
            'measurement_sha256': sha, 'locales': len(names),
            'skipped': list(skipped),
            'charmaps': None if charmaps is None else len(charmaps.files),
            'charmaps_dir': None if charmaps is None else charmaps.root,
            'charmaps_why': why}
    return _listed('build', repo, commit, extra, f'{name}.src',
                   f'glibc {parsed[0]}', prov), None


def build_side(repo, name, extra=(), make=False):
    """(Side, Built) of the build `name`: fetched and rebuilt from its SRPM
    with the clone (build_sources.fetch_build, the tarball compared with the
    upstream tag), made a commit of the clone again (build_commit), and
    listed at that commit and held to the floor like a tag. Its upstream
    tag is fetched first when the clone lacks it. Any refusal dies with its
    reason: a build that cannot be read is never replaced by its tag, whose
    sources are not the build's. So does a build that changes glibc's line
    reader (LINE_READER): how localedef reads 0x1A, 0xFF and UTF-8 is
    answered here by the tag's ancestry, which would not speak for it.

    Under audit.sh (SIDES_ENV set) a step after step 1 takes the commit
    step 1 made from that file (_recorded) instead of making it again.
    Step 1 passes `make`: it always makes the commit, and then records it
    (write_recorded), so it never reads a file it has not written.

    When MACHINES_ENV names `name`, none of the above: the build is read
    from the machines' own files (machine_side), nothing is fetched, and
    the commit leaves locale/ as the tag's, so LINE_READER cannot differ."""
    import build_sources as bs
    parsed = build_name(name)
    folders = machine_folders(name) if parsed else []
    if folders:
        # A machine's file of this run names this build: its own locale
        # sources are read, and nothing is downloaded.
        return machine_side(repo, name, folders, extra, make)
    if parsed:
        _need_tags(repo, ['glibc-' + parsed[0]])
    try:
        built = _fetch(name, repo)
        if not built.provenance.get('tarball'):
            raise bs.Refused("the SRPM's tarball was not compared with %s"
                             % built.tag)
        if os.environ.get(SIDES_ENV) and not make:
            commit = _recorded(repo, name, built)
        else:
            commit = bs.build_commit(repo, built)
    except bs.Refused as e:
        die(f"cannot read the build {name}: {e}", 2)
    changed = [p for p in run_git(
        ['diff-tree', '-r', '--name-only', built.tag, commit, '--',
         *LINE_READER], repo).stdout.decode('utf-8', 'replace').split('\n')
               if p]
    if changed:
        die(f"cannot read the build {name}: it changes glibc's line reader "
            f"({', '.join(changed)}), and how localedef reads the bytes 0x1A "
            f"and 0xFF and UTF-8 input is answered here by the ancestry of "
            f"{built.tag}, which does not speak for that change.", 2)
    prov = dict(built.provenance, tag=built.tag)
    return _listed('build', repo, commit, extra, built.build,
                   built.version, prov), built


def step_sides(repo, *names):
    """The sides a step reads, (sides, commits): each argument a tag of the
    clone, kept as its name, or a build (is_build_name), made a Side by
    side() and read by its commit -- a build's name never reaches git.
    commits maps what git reads (a tag's name, a build's commit) to the
    commit, for pair_check and nothing_compared. The tags are checked as
    check_refs checks them, a commit build_commit made refused among them.

    Every line the step writes from then on names a build by its name:
    name_commits replaces the commit's id with it."""
    tags = [n for n in names if not is_build_name(n)]
    commits = check_refs(repo, *tags) if tags else {}
    sides = []
    for n in names:
        if n in tags:
            sides.append(n)
        else:
            s = side(repo, n)
            commits[s.ref] = s.ref
            sides.append(s)
    name_commits({s.ref: label(s) for s in sides if not _is_tag(s)})
    return sides, commits


class _Named(io.TextIOBase):
    """A text stream that writes through to `out` with each commit id of
    `names` replaced by its name. The id is the full 40 or 64 hex digits
    git gives, which no other text a step prints holds."""

    def __init__(self, out, names):
        super().__init__()
        self.out, self.names = out, names

    def write(self, text):
        for sha, name in self.names.items():
            text = text.replace(sha, name)
        return self.out.write(text)

    def writable(self):
        return True

    def flush(self):
        self.out.flush()

    @property
    def encoding(self):
        return self.out.encoding

    def isatty(self):
        return self.out.isatty()


_NAMES = {}


def name_commits(names):
    """From now on the build commits of {commit: name} are printed by name,
    on stdout and stderr: the one place a step turns what git read into
    what its reader is told. Installed once per process; a later call adds
    names."""
    if not names:
        return
    _NAMES.update(names)
    for attr in ('stdout', 'stderr'):
        stream = getattr(sys, attr)
        if not isinstance(stream, _Named):
            setattr(sys, attr, _Named(stream, _NAMES))


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


def decode_symbolic(name):
    """`<U0069><U0073><U006F>` -> `iso`, and anything else unchanged.

    A locale name written in glibc's symbolic notation is the same name to
    localedef, and was a different one here: the graph kept the escaped
    spelling as a key nothing matched, so the locale looked like a leaf that
    copies nothing reachable. Two files in the corpus do this.
    """
    return _UCHAR_RE.sub(lambda m: chr(int(m.group(1), 16)), name)


# Where get_ident (linereader.c, glibc-2.39) ends a word, besides EOF.
_IDENT_STOP = frozenset(_SPACE + '";<,')


def _directive_words(lines):
    """The directive keywords localedef reads first on a logical line, in
    order, or None when a directive's argument leaves the characters in
    force unsettled.

    Read the way glibc reads them, not by their spelling. lr_next joins a
    line that ends in the escape character to the next one; lr_token drops
    a physical line that opens with the comment character, and on a
    continued line only that physical line; get_ident keeps the character
    after an escape inside a word, so `comment_c/har #` is the directive
    (glibc-2.39). A search for the plain keyword finds none of that, and the
    characters it reported were the ones before the hidden directive.
    _glibc_chars compares the two readings and refuses where they differ.
    """
    cc, ec = '#', '\\'
    words, i = [], 0
    while i < len(lines):
        text = ''
        while i < len(lines):
            line = lines[i]
            i += 1
            if not text.strip(_SPACE) and line.lstrip(_SPACE).startswith(cc):
                if line.endswith(ec):
                    continue
                text = None
                break
            if line.endswith(ec):
                text += line[:-len(ec)]
                continue
            text += line
            break
        if not text:
            continue
        s = text.lstrip(_SPACE)
        word, j = '', 0
        # An escape opening a token is a byte value (get_toplvl_escape),
        # never the start of a word.
        if s[:1] != ec:
            while j < len(s) and s[j] not in _IDENT_STOP:
                if s[j] == ec:
                    j += 1
                    if j == len(s):
                        break
                word += s[j]
                j += 1
        if word in ('comment_char', 'escape_char'):
            arg = s[j:].strip(_SPACE)
            if len(arg) != 1:
                return None
            words.append(word)
            if word == 'comment_char':
                cc = arg
            else:
                ec = arg
    return words


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
    # A directive glibc reads that the line-by-line search below does not
    # see, or the other way round (_directive_words).
    plain = []
    for line in lines:
        m = re.match(r'(comment_char|escape_char)(?![A-Za-z0-9_])',
                     line.strip(_SPACE))
        if m:
            plain.append(m.group(1))
    if _directive_words(lines) != plain:
        return None
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


def reading_chars(text):
    """(comment char, escape char) localedef reads this file with, or None
    when _glibc_chars cannot settle them.

    They belong to a file's rules as much as its LC_COLLATE text does. The
    same block read with another comment character keeps or drops other
    lines, and another escape character joins other lines and splits weight
    strings elsewhere (locfile_read, lr_next and collate_read at glibc-2.39).
    Measured on the RHEL 8, 9 and 10 builds: a block identical byte for byte
    sorts differently under another `comment_char` (backlog 13.7).
    """
    return _glibc_chars(text.split('\n'))


@contextlib.contextmanager
def reading_chars_once():
    """For the length of the block, reading_chars answers each text once.

    One run of step 2 asks it of the same texts several times (13.7 PR D).
    The step's main wraps its run in this, and the reader is
    restored on the way out, so nothing is kept past the run: a test that calls two
    steps in one process gets two fresh readings.
    """
    global reading_chars
    real, memo = reading_chars, {}

    def once(text):
        got = memo.get(text, memo)
        if got is memo:
            got = memo[text] = real(text)
        return got
    reading_chars = once
    try:
        yield
    finally:
        reading_chars = real


# The glibc commit from which lr_getc no longer reads 0x1A, or 0xFF through
# a signed char, as the end of the file ("locale: Fix signed char bug in
# lr_getc", first in glibc-2.36; linereader.h:138@2.34 before it). Two
# commits before collation_keys.UTF8_READER, so a tag holding that one holds
# this one; a commit between the two does not hold both.
LR_GETC_FIX = '19d494445981a09503e4a0175732745c39dd7c21'


# The words glibc's top-level reader opens a section with
# (locfile.c:171-255@2.39). Any other word there is a syntax error, and the
# line is dropped.
_CATEGORIES = frozenset((
    'LC_CTYPE', 'LC_COLLATE', 'LC_MONETARY', 'LC_NUMERIC', 'LC_TIME',
    'LC_MESSAGES', 'LC_PAPER', 'LC_NAME', 'LC_ADDRESS', 'LC_TELEPHONE',
    'LC_MEASUREMENT', 'LC_IDENTIFICATION'))


def _first_word(s):
    """The word get_ident reads at the start of `s`: up to a blank, `"`,
    `;`, `<` or `,` (linereader.c:580-581@2.39, :552-553@2.28 and 2.34).
    `END.LC_CTYPE` is one word to glibc, not END."""
    i = 0
    while i < len(s) and s[i] not in _IDENT_STOP:
        i += 1
    return s[:i]


def _logical_lines(lines, comments, escapes):
    """(text, complete) for each line glibc's reader reads out of `lines`,
    blanks stripped from its start: a line ending in an escape is joined to
    the next one (lr_next, linereader.c:162@2.39), and a line opening with
    the comment character is dropped whole (lr_token, :221-229) unless the
    characters are unsettled (more than one comment character) and it ends
    in an escape. A line still joined when `lines` run out comes last, with
    complete False. Blank lines are not given."""
    settled = len(comments) == 1
    logical, joined = '', False
    for line in lines:
        if not joined and line.lstrip(_SPACE).startswith(comments) \
                and (settled or not line.endswith(escapes)):
            continue
        cont = line.endswith(escapes)
        logical = (logical if joined else '') + (line[:-1] if cont else line)
        joined = cont
        if cont:
            continue
        s = logical.lstrip(_SPACE)
        if s:
            yield s, True
    if joined:
        yield logical.lstrip(_SPACE), False


def _collate_below(lines, comments, escapes):
    """Why the lines below the LC_COLLATE block may open it again, or None:
    a line glibc reads whose first word is LC_COLLATE once lines are joined
    (`LC_COLL/` then `ATE`), or a first word that holds the escape, which
    this tool does not read (round 3 of the 13.18 review)."""
    for s, _ in _logical_lines(lines, comments, escapes):
        word = _first_word(s)
        if word == 'LC_COLLATE' or any(e in word for e in escapes):
            return ("a line below the LC_COLLATE block can open LC_COLLATE "
                    "again, and glibc reads every such section into the "
                    "same tables")
    return None


def _sections_above(lines, comments, escapes):
    """Why the last of `lines`, the LC_COLLATE line, may not open a section,
    or None: the lines above it read the way glibc's line reader reads them.

    lr_next joins a line that ends in the escape character to the next one
    (linereader.c:162@2.39), so the LC_COLLATE line continued from the line
    above is part of that line. A section opened above and not closed by an
    `END` that opens a line glibc reads is still open, and a category reader
    takes the LC_COLLATE line as a syntax error inside it, then closes on
    `END LC_COLLATE` whatever its argument (ld-identification.c:387-403,
    ld-ctype.c:2820-2835@2.39): collate_read never runs, and collate_finish
    only warns (ld-collate.c:1563-1568@2.34). An `END` is lost by being
    commented out, deleted, or joined to the line above, and after a `copy`
    line or `END` itself, lr_ignore_rest drops the whole joined line
    (linereader.c:133-136; ld-ctype.c:2143@2.39). Inside LC_CTYPE, a
    `translit_start` anywhere on a line is read until `translit_end` or the
    end of the file, END LC_CTYPE included (ld-ctype.c:2625-2657@2.39): only
    a line opening with `translit_end` leaves it here, though glibc can
    leave it sooner. An LC_CTYPE `copy` with no string after it sends
    ctype_read to skip_category, which stops at the END and leaves the word
    after it to the top level (ld-ctype.c:2104-2122@2.39), so the LC_CTYPE
    after END opens a section again. An LC_COLLATE section above the block
    is a second one, which collate_read reads into the same tables
    (collate_startup, ld-collate.c:1499@2.39).

    Measured on glibc-2.34-283.el9_8 and glibc-2.39-142.el10_2 (2026-10-12,
    localedef -c --no-archive): a commented-out END LC_CTYPE or
    translit_end, an include or copy line ending in the escape above them,
    `END.LC_CTYPE` and `copy i18n` each make localedef say "No definition
    for LC_COLLATE category found" and write a 92-byte LC_COLLATE that does
    not load, where the control writes 2.5 MB.

    When the characters are settled, the directive lines that open the file
    are read with the characters in force before them, which joins none of
    them (glibc_locale_data._glibc_chars), and a line opening with the
    comment character is dropped whole, continued or not (lr_token,
    :221-229). When they are not settled, `comments` and `escapes` hold
    both of each, and such a line that ends in an escape is joined: the
    answer is then a guess in either direction. No step asks it since step
    9.2, when step 2 began comparing each locale's rules
    (glibc_tree.compare_rules); it is kept, with its tests, for branches 12
    and 13.
    A word built with an escape (get_ident keeps the character after one,
    :580-589) is not read here: it is a reason too.
    """
    settled = len(comments) == 1
    start = 0
    if settled:
        while start < len(lines) - 1 and (
                not lines[start].strip(_SPACE)
                or _first_word(lines[start].lstrip(_SPACE))
                in ('comment_char', 'escape_char')):
            start += 1
    open_section, translit = None, False
    for s, complete in _logical_lines(lines[start:-1], comments, escapes):
        if not complete:
            return ("the line above LC_COLLATE ends in the escape "
                    "character, so glibc can read LC_COLLATE as part of "
                    "that line")
        word = _first_word(s)
        if any(e in word for e in escapes):
            return ("a word above LC_COLLATE holds the escape character, "
                    "which this tool does not read")
        if translit:
            translit = word != 'translit_end'
        elif word == 'END':
            open_section = None
        elif word == 'LC_COLLATE':
            return ("a second LC_COLLATE section above the block, which "
                    "glibc reads into the same tables")
        elif open_section == 'LC_CTYPE' and word == 'copy' \
                and not s[len(word):].lstrip(_SPACE).startswith('"'):
            return ("an LC_CTYPE copy above LC_COLLATE with no string after "
                    "it, after which glibc can read LC_COLLATE inside "
                    "LC_CTYPE")
        elif word in _CATEGORIES and open_section is None:
            open_section = word
        elif open_section == 'LC_CTYPE' and re.search(
                r'(?<![A-Za-z0-9_])translit_start(?![A-Za-z0-9_])', s):
            translit = True
    if open_section:
        inside = ' inside its translit_start' if translit else ''
        return (f"{open_section}, opened above LC_COLLATE, has no END line "
                f"glibc reads before it{inside}, so glibc can read LC_COLLATE "
                f"inside {open_section}")
    return None


def byte_ends_it_early(text):
    """Does a 0x1A or 0xFF byte sit above the end of the file's LC_COLLATE
    block? Before LR_GETC_FIX, lr_getc reads either as EOF
    (linereader.h:138@2.34): above the block, glibc never reaches it; inside
    it, collate_read stops there (ld-collate.c:2662@2.34) and every rule
    after the byte is lost. From 2.36 both are ordinary bytes. The 0xFF is
    found through TEXTS_WITH_0XFF, since decoding made it a U+FFFD like any
    byte that is not UTF-8, so every U+FFFD in that part counts. A byte on a
    comment line, which glibc skips without lr_getc, counts too: it only
    keeps a locale listed. False for a text with no block."""
    bounds = collate_bounds(text)
    if bounds is None:
        return False
    head = '\n'.join(text.split('\n')[:bounds[1]])
    return '\x1a' in head or (text in TEXTS_WITH_0XFF and '\ufffd' in head)


def lc_collate_unread(text, early):
    """Why glibc may not read this file's LC_COLLATE block as written, or
    None when none of the shapes below is there.

    The block can be the same text at both tags and still not be read as
    written, by what sits around it (backlog 13.18). The shapes, all read on
    glibc's line reader at 2.28, 2.34 and 2.39:
      - A line above holds only the escape character. lr_next leaves an
        empty buffer, and lr_getc answers EOF from then on
        (linereader.h:134@2.34).
      - The LC_COLLATE line is continued from the line above, or a section
        opened above it is never closed (_sections_above).
      - A second LC_COLLATE section, above or below the block, spelled
        plainly, indented or built across a line end (_sections_above,
        _collate_below): glibc reads each such section into the same tables
        (collate_startup, ld-collate.c:1499@2.39).
      - Before LR_GETC_FIX, a 0x1A or 0xFF byte above the end of the block
        (byte_ends_it_early).

    Each is counted wherever glibc might be stopped by it. No step asks it
    since step 9.2; it is kept, with its tests, for branches 12 and 13. No
    locale file at glibc-2.12..2.42 holds any of
    them (measured 2026-10-12).

    The comment and escape characters are the ones reading_chars settles,
    or both of each when it cannot. `early()` says whether the localedef
    that reads the text predates LR_GETC_FIX: True, False, or None for
    "cannot tell", which counts as yes. It is asked only for a text with
    one of those bytes.

    None for a text with no LC_COLLATE line, which this line search reads
    only at the start of a line, is not a clean answer.
    """
    bounds = collate_bounds(text)
    if bounds is None:
        return None
    lines = text.split('\n')[:bounds[0]]
    chars = reading_chars(text)
    comments, escapes = (((chars[0],), (chars[1],)) if chars is not None
                         else (('%', '#'), ('/', '\\')))
    if any(line in escapes for line in lines[:-1]):
        return ("a line above LC_COLLATE holds only the escape character, "
                "which glibc reads as the end of the file")
    why = (_sections_above(lines, comments, escapes)
           or _collate_below(text.split('\n')[bounds[1]:], comments, escapes))
    if why:
        return why
    if byte_ends_it_early(text) and early() is not False:
        return ("a 0x1A or 0xFF byte above the end of LC_COLLATE, which "
                "glibc before 2.36 reads as the end of the file")
    return None


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

    Step 2 prints these names for the files only the new side has, and
    reads the map to say which locales are not built by default. They are not
    every name a locale takes: the archive and RHEL add others
    (sv_SE.iso885915), and locale.alias adds swedish (backlog 13.1).
    """
    contents, _ = read_blobs(repo, tag, [SUPPORTED])
    return parse_supported(contents.get(SUPPORTED), tag)


def parse_supported(text, tag):
    """supported_map, for a caller that read SUPPORTED in a batch of its own.
    `text` is None when the read found nothing, and that dies here too."""
    if text is None:
        # Returning {} here made every locale print as "not listed in
        # SUPPORTED, so normally absent from `locale -a`" -- a false and
        # reassuring claim -- and wrote an empty step4 list under the heading
        # "full list of generated names".
        die(f"localedata/SUPPORTED does not exist at {tag}; cannot tell which "
            f"locales are built by default, or the names they are installed "
            f"under.")
    out = {}
    for line in text.split('\n'):
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


MIRROR_URL = 'https://github.com/bminor/glibc.git'


def step1_sides(here, old, new, allow_reverse=False):
    """Step 1's two sides, (repo, old Side or tag, new Side or tag), and all
    it used to do in bash before its first finding, with the same texts in
    the same order for two tags: clone glibc into `here`/glibc when it is
    not there, fetch tags once when a tag is missing, refuse a reversed
    pair, print the provenance, hold each side to the floor; and, which the
    bash did not, a `!!` for the sides read as tags (tag_sides_text). A build name
    (is_build_name) is never asked of git: build_side fetches it and makes
    its commit again, or makes it from a machine's own file under audit.sh
    (machine_side). latest-elN is refused (refuse_latest)."""
    refuse_latest((old, new))
    repo = os.path.join(here, 'glibc')
    if not os.path.isdir(repo):
        # Not --depth: step 2 asks git which tag holds the commits that
        # change how localedef reads its input (require_full_history), and a
        # shallow clone can answer no for a tag that holds one.
        print("Cloning glibc (full history, blobs on demand)...", flush=True)
        p = subprocess.run(['git', 'clone', '--filter=blob:none',
                            '--no-checkout', MIRROR_URL, repo])
        if p.returncode:
            sys.exit(p.returncode)
    names = (old, new)
    tags = [n for n in names if not is_build_name(n)]
    # The tags git must hold: the arguments that are tags, and the upstream
    # tag each build is made on, which a clone older than the build lacks
    # (build_side fetches it too, for a step run on its own).
    _need_tags(repo, tags + ['glibc-' + build_name(n)[0] for n in names
                             if n not in tags])
    if tags:
        refuse_build_commits(repo, resolve_commits(repo, tags))
    sides = [n if n in tags else side(repo, n, make=True) for n in names]
    with contextlib.redirect_stdout(sys.stderr):
        pair_check(repo, sides[0], sides[1], allow_reverse=allow_reverse)
    builds = [sd for sd in sides if not _is_tag(sd)]
    # Under audit.sh, for the steps after this one (SIDES_ENV): after the
    # order check, so a refused pair records nothing.
    write_recorded(builds)
    if not builds:
        report_tag_provenance(repo, old, new)
    else:
        under = []
        for sd in sides:
            t = sd if _is_tag(sd) else sd.prov['tag']
            if t not in under:
                under.append(t)
        report_tag_provenance(repo, *under)
        machines = [sd for sd in builds if is_machine(sd)]
        if machines:
            report_machine_provenance(machines)
        rebuilt = [sd for sd in builds if not is_machine(sd)]
        if rebuilt:
            report_build_provenance(rebuilt)
    if tags:
        warn(tag_sides_text(tags), split_words=False)
    warn(CODE_NOT_COMPARED, split_words=False)
    print('---', flush=True)
    # The corpus floor: both tags must hold a real locale corpus, or the
    # diff is empty for a reason that has nothing to do with collation and
    # reads as "nothing changed". A build was held to it by build_side.
    for n in tags:
        side(repo, n)
    return repo, sides[0], sides[1]


# How audit.sh tells step 1 what a reader of this run can do to have a
# side read as a build (tag_sides_text): 'build' (name it), 'node' (the
# locale folders or the measurements, which a build refuses: pass the
# machines' files),
# 'none' (a machine's file holds no locale sources or names a build that is
# not of RHEL 8, 9 or 10, and its own `!!` says so). Unset, by hand:
# 'build'.
TAG_ASK_ENV = 'PG_GLIBC_AUDIT_TAG_ASK'


# What no comparison of sources reads, said once: step 1 prints it as a `!!`
# on every run, whatever the sides are, and the summary repeats it from
# step 1's log. Step 2 compares the rules localedef reads; what localedef
# compiles them into and how strcoll walks the result is code, which two
# tags or two builds can change with every locale file the same (Bug 22668
# reordered ko_KR between glibc 2.28 and 2.34, its file unchanged). Steps 4
# and 5 looked at that code until step 9.3. With both machines' files, step
# 11 measures the order it produces.
CODE_NOT_COMPARED = (
    "Not compared on any route: the code that compiles and compares the "
    "rules (localedef's and strcoll's, each build's patches to it "
    "included). Only step 11 sees what it does, when it measures both "
    "machines; docs/confirming-on-a-real-system.md says how to confirm the "
    "order on them.")


def tag_sides_text(tags):
    """The `!!` for the sides step 1 reads as upstream tags: what a tag
    does not hold, said once here (the summary relays it from step 1's
    log), and what to pass instead, by the route TAG_ASK_ENV names."""
    ask = os.environ.get(TAG_ASK_ENV) or 'build'
    if ask not in ('build', 'node', 'none'):
        die(f"{TAG_ASK_ENV}={ask!r} is not one of build, node, none.")
    one = len(tags) == 1
    text = (f"Not checked for {' and '.join(tags)}: "
            f"{'it is an upstream tag' if one else 'they are upstream tags'}"
            f", which hold{'s' if one else ''} none of a distro's own locale "
            f"files or patches. Steps 1 and 2 read only what "
            f"{'that tag holds' if one else 'the tags hold'}.")
    if ask == 'build':
        text += (f" If {'that machine runs' if one else 'the machines run'} "
                 f"RHEL 8, 9 or 10, name {'that side' if one else 'each side'} "
                 f"as its build instead, as rpm -q glibc prints it on the "
                 f"machine without the architecture (glibc-2.34-283.el9_8), "
                 f"or as latest-el8, latest-el9 or latest-el10.")
    elif ask == 'node':
        text += (" Pass each machine's file with --old-node and --new-node "
                 "instead of every other --old-*/--new-* option: steps 1 and 2 "
                 "then read each machine's own locale sources, which its file "
                 "holds.")
    return text


# What no step reads of a machine whose own files are read (machine_side),
# said once, where step 1 names the machines; every other block points here.
MACHINE_NOT_CHECKED = (
    "Not checked for these machines: their locale sources and charmaps are "
    "read as each machine's file holds them, with no check against the "
    "glibc-locale-source package they came from; localedata/SUPPORTED and "
    "intl/locale.alias are the upstream tag's, as a machine's file holds "
    "neither (step 2 takes which locales each machine builds with each "
    "charmap from its measurement instead); localedef's keyword tables are "
    "the tag's with what Red Hat's patches are known to add for that RHEL "
    "major, and how localedef reads the bytes 0x1A and 0xFF and UTF-8 input "
    "is answered by the tag's ancestry.")


def report_machine_provenance(machines):
    """Print, for each Side read from a machine's own files, which file and
    what it held; a `!!` for the entries of its sources that were not read;
    one `!!` for what no step reads of a machine (MACHINE_NOT_CHECKED)."""
    print("Machine provenance (each machine's own locale sources and charmaps, "
          "from its")
    print("file, on its upstream tag above, as one commit of the clone; "
          "nothing downloaded):")
    for sd in machines:
        prov = sd.prov
        print(f"  {label(sd)} = {sd.ref[:12]}, one parent: {prov['tag']}")
        print(f"    rpm -q glibc   {prov['word']}")
        print(f"    measurement    sha256 {prov['measurement_sha256']}")
        print(f"    locale sources {prov['locales']} file(s)")
        if prov['charmaps'] is None:
            print(textwrap.fill(
                f"charmaps       none, because {prov['charmaps_why']}",
                width=78, initial_indent='    ', subsequent_indent=' ' * 19,
                break_long_words=False, break_on_hyphens=False))
        else:
            print(f"    charmaps       {prov['charmaps']} file(s)")
    for sd in machines:
        skipped = sd.prov['skipped']
        if skipped:
            warn(f"{label(sd)}: {len(skipped)} entr"
                 f"{'y' if len(skipped) == 1 else 'ies'} of the machine's "
                 f"files {'was' if len(skipped) == 1 else 'were'} not read, "
                 f"so a locale that is, or copies, one of them is not "
                 f"compared: "
                 + ', '.join(f"{shown(e)} ({why})" for e, why in skipped)
                 + '.', split_words=False)
    warn(MACHINE_NOT_CHECKED, split_words=False)


def report_build_provenance(builds):
    """Print where each build Side came from and what was checked (its
    prov); one `!!` for what was not. URLs are never wrapped, so they can
    be copied whole."""
    import build_sources as bs
    print("Build provenance (each build is rebuilt here: its upstream tag "
          "above, with")
    print("localedata/ and locale/ from its source RPM, as one commit of the "
          "clone):")
    fetched = False
    warned = []
    for sd in builds:
        prov = sd.prov
        print(f"  {label(sd)} = {sd.ref[:12]}, one parent: {prov['tag']}")
        files = (prov.get('fetched') or {}).get('files') or {}
        fetched = fetched or bool(files)
        for key, title in (('srpm', 'SRPM'),
                           ('glibc-locale-source', 'glibc-locale-source'),
                           ('glibc-common', 'glibc-common')):
            e = files.get(key)
            if e:
                print(f"    {title:<20} {e['url']}")
                print(f"    {'':<20} sha256 {e['sha256']}")
        print(f"    checked: {prov['tarball']}")
        for line in prov.get('verified') or ():
            if line.startswith('!!'):
                warned.append(f"{label(sd)}: {line[2:].strip()}")
            else:
                print(f"    checked: {line}")
        code = [c['patch'] for c in prov.get('collation_code_patches') or ()]
        text = (f"patches: {len(prov.get('applied') or ())} of the SRPM's "
                f"{prov.get('patches')} change localedata/ or locale/, "
                f"applied in the order rpm recorded; {len(code)} change "
                f"strcoll's code" + (f": {', '.join(code)}" if code else ''))
        print(textwrap.fill(text, width=78, initial_indent='    ',
                            subsequent_indent='             ',
                            break_on_hyphens=False, break_long_words=False))
    for w in warned:
        warn(w, split_words=False)
    items = list(bs.NOT_CHECKED)
    gpg = [i for i, t in enumerate(items) if 'GPG signatures' in t]
    tarball = [i for i, t in enumerate(items)
               if t.startswith('localedata/SUPPORTED and locale/')]
    if len(gpg) != 1 or len(tarball) != 1:
        die("build_sources.NOT_CHECKED no longer names the GPG signatures "
            "and the tarball once each, so what was not checked cannot be "
            "said.")
    if fetched:
        items[gpg[0]] += (", and the mirror metadata's, whose sha256 is the "
                          "only check on each file")
    items[tarball[0]] = ("localedata/SUPPORTED and locale/ are in no binary "
                         "package: they are the SRPM's tarball, checked above "
                         "against the upstream tag, and its patches")
    warn("Not checked for these builds: " + '; '.join(items) + '.',
         split_words=False)


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
    if argv and argv[0] == 'sides':
        # Step 1's sides (step1_sides). Every human sentence goes where the
        # bash it replaces sent it; the two refs the step reads go to the
        # file --refs-file names, a line each: a tag's name, a build's
        # commit.
        allow = '--allow-reverse' in argv[1:]
        rest = [a for a in argv[1:] if a != '--allow-reverse']
        if len(rest) != 4 or rest[0] != '--refs-file':
            die("usage: glibc_locale_data.py sides [--allow-reverse] "
                "--refs-file <file> <old> <new>")
        here = os.path.dirname(os.path.abspath(__file__))
        _, o, n = step1_sides(here, rest[2], rest[3], allow)
        with open(rest[1], 'w') as fh:
            fh.write(f"{_ref(o)}\n{_ref(n)}\n")
        return 0
    if argv and argv[0] == 'resolve':
        # audit.sh, before anything runs: the kind of each side, by the one
        # rule every step applies (is_build_name), so that it picks each
        # sentence of its summary by what the side is. latest-elN is a
        # build, marked `latest` in a third field, and asks nothing of the
        # network here: audit.sh refuses a machine's files with a build
        # first, then has `latest` resolve it.
        if len(argv) != 5 or argv[1] != '--to':
            die("usage: glibc_locale_data.py resolve --to <file> <old> <new>")
        with open(argv[2], 'w', encoding='utf-8') as fh:
            for name in argv[3:]:
                if latest_major(name) is not None:
                    fh.write(f"{name}\tbuild\tlatest\n")
                else:
                    bare = without_arch(name)
                    kind = 'build' if is_build_name(bare) else 'tag'
                    fh.write(f"{bare}\t{kind}\n")
        return 0
    if argv and argv[0] == 'machines':
        # audit.sh, given each machine's file, before step 1: which side
        # steps 1 and 2 read (machine_sides), its kind and its tag, a line
        # each in --to; what to say about it on stdout. Each machine's
        # laid-out file says whether it holds the locale sources, and why
        # not (sources-not-included).
        if len(argv) != 9 or argv[1] != '--to':
            die("usage: glibc_locale_data.py machines --to <file> <old> "
                "<new> <old_build> <new_build> <old_folder> <new_folder>")
        no_sources = []
        for folder in argv[7:9]:
            if os.path.isdir(os.path.join(folder, 'locales')):
                no_sources.append(None)
                continue
            try:
                with open(os.path.join(folder, 'sources-not-included'),
                          encoding='utf-8') as fh:
                    why = fh.read().strip()
            except OSError:
                why = ''
            no_sources.append(why or "the file gives no reason")
        sides, lines = machine_sides(argv[3:5], argv[5:7], no_sources)
        with open(argv[2], 'w', encoding='utf-8') as fh:
            for name, kind, tag in sides:
                fh.write(f"{name}\t{kind}\t{tag}\n")
        for ln in lines:
            print(ln, flush=True)
        return 0
    if argv and argv[0] == 'latest':
        # audit.sh, once for the whole run: each latest-elN replaced by the
        # newest build of RHEL N the mirror lists (build_sources.latest,
        # which reads the metadata every time), each N asked once, all of it
        # before anything is written. A refusal dies with its reason: a
        # side that cannot be resolved is never read as a tag.
        if len(argv) != 5 or argv[1] != '--to':
            die("usage: glibc_locale_data.py latest --to <file> <old> <new>")
        found = {}
        for name in argv[3:]:
            major = latest_major(name)
            if major is None or major in found:
                continue
            import build_sources as bs
            try:
                found[major] = (name, bs.latest(major))
            except bs.Refused as e:
                die(f"cannot resolve {name}: {e}")
        with open(argv[2], 'w', encoding='utf-8') as fh:
            for name in argv[3:]:
                major = latest_major(name)
                fh.write((found[major][1].build if major is not None
                          else name) + '\n')
        for name, got in found.values():
            print(latest_line(name, got), flush=True)
        return 0
    if len(argv) >= 2 and argv[0] == 'corpus':
        # The corpus floor of the tags given, silent on success;
        # list_locale_files dies otherwise. Step 1 applies it in step1_sides.
        repo = find_repo()
        check_refs(repo, *argv[1:])
        for tag in argv[1:]:
            list_locale_files(repo, tag)
        return 0
    if argv and argv[0] == 'order':
        # The direction of the pair and whether it compares anything, for
        # audit.sh. The two status WORDS are the only thing
        # on stdout, because audit.sh captures them in a variable; every human
        # sentence goes to stderr. A forward pair that compares something
        # therefore adds nothing at all to the run's output, which is what
        # keeps the audited pairs byte-identical.
        #
        # --quiet suppresses the `!!` blocks, not the refusal: the steps are
        # the callers that print them, into their own logs, where the summary's
        # warnings block finds them. audit.sh asks only for the words, and one
        # more copy of a warning the reader has already seen teaches them to
        # skip the section.
        allow = '--allow-reverse' in argv[1:]
        quiet = '--quiet' in argv[1:]
        names = [a for a in argv[1:]
                 if a not in ('--allow-reverse', '--quiet')]
        if len(names) != 2:
            die("usage: glibc_locale_data.py order [--allow-reverse] "
                "[--quiet] <old> <new>")
        repo = find_repo()
        # A build is a Side, read by its commit and ordered by rpm; anything
        # making it prints goes to stderr, so the two words stay alone.
        with contextlib.redirect_stdout(sys.stderr):
            sides, commits = step_sides(repo, *names)
        sink = io.StringIO() if quiet else sys.stderr
        with contextlib.redirect_stdout(sink):
            status, nothing = pair_check(repo, sides[0], sides[1],
                                         allow_reverse=allow, commits=commits)
        # Two words: the direction, and what nothing_compared answered
        # (commit, tree or none). audit.sh reads the second for "nothing was
        # compared" (SAME_TAG), whatever the first says: a pair of one tree
        # that git cannot order is both.
        print(status, nothing or 'none')
        return 0
    if len(argv) >= 2 and argv[0] == 'fanin':
        repo = find_repo()
        # --as NAME: the ref read is a build's commit, printed by its name.
        shown = None
        if len(argv) >= 4 and argv[-2] == '--as':
            shown, argv = argv[-1], argv[:-2]
        tag = argv[1]
        check_refs(repo, tag, builds=shown is not None)
        # Imported here: glibc_tree imports this module.
        import glibc_tree
        # Who inherits what, from what localedef reads to build each locale
        # (glibc_tree), every `copy` followed the way glibc follows it.
        # A machine's commit holds the tag's keyword tables, not the
        # build's: read with what the build's RHEL major adds, as step 2
        # reads it (one Build, one entry of the reader's cache).
        build = (glibc_tree.build_at(repo, tag, shown)
                 if shown and machine_folders(shown) else None)
        tree = glibc_tree.Tree.at_tag(repo, tag, build)
        # Every locale with an LC_COLLATE of its own first, in name order,
        # so that equal counts print in the order they always have.
        counts = {name: 0 for name in tree.names if tree.defines(name)}
        for name in tree.names:
            for f in tree.reach(name):
                counts[f] = counts.get(f, 0) + 1
        unresolved = tree.unresolved()
        changed = None
        if len(argv) >= 3:
            with open(argv[2], encoding='utf-8') as fh:
                changed = {os.path.basename(ln.strip())
                           for ln in fh if ln.strip()}
        print(f"Collation templates by blast radius at {shown or tag} "
              f"(locales inheriting via `copy`):")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
            if n == 0:
                break
            mark = ''
            if changed is not None and name in changed:
                mark = '  <== has some change in this version pair'
            print(f"  {n:4d} locales inherit from {name}{mark}")
        if unresolved:
            print(f"!! {len(unresolved)} locale(s) could not be read the way "
                  f"localedef reads them, so the counts above may miss what "
                  f"they inherit: {', '.join(sorted(unresolved)[:12])}"
                  f"{', ...' if len(unresolved) > 12 else ''}")
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
