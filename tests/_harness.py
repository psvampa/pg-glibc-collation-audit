"""Shared setup for the test suite: import paths, the glibc clone, subprocesses.

Nothing here asserts anything. It exists so that a test that needs the real
glibc clone SKIPS with a reason instead of failing, and so that running the
suite never writes into the shared /tmp output directory the scripts default to.
"""
import atexit
import functools
import gzip
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(REPO_ROOT, 'scripts')
GLIBC_CLONE = os.path.join(SCRIPTS_DIR, 'glibc')

# Where audit.sh and every step write when PG_GLIBC_AUDIT_OUT is unset.
# audit.sh and glibc_locale_data.OUT_DIR each spell it; test_parallel_runner ties every one of them to this name, because
# run_parallel watches this folder and a guard watching a folder nothing
# writes to any more would pass for ever.
DEFAULT_OUT_DIR = '/tmp/pg-glibc-collation-audit'

# Every process that imports this module sends the steps' output to a folder
# of its own, before any script module is imported (glibc_locale_data reads
# the variable at import), and whatever the caller had set, since a value
# inherited from the shell can be a real audit's folder too. A test that
# passes out_dir= still gets the folder it names; one that forgets, or that
# launches a step through a helper of its own, lands here. In the default
# folder it would overwrite the step 2 list that a real audit.sh running at
# the same time reads back between its steps (backlog 13.37).
SUITE_OUT_DIR = tempfile.mkdtemp(prefix='pg-glibc-audit-suite-')
os.environ['PG_GLIBC_AUDIT_OUT'] = SUITE_OUT_DIR
atexit.register(shutil.rmtree, SUITE_OUT_DIR, True)
# The same for scripts/build_sources.py's cache: a test that forgets cache=
# keeps its builds in the suite's folder, not in scripts/build-sources-cache.
os.environ['PG_GLIBC_BUILD_SOURCES_CACHE'] = os.path.join(SUITE_OUT_DIR,
                                                          'build-sources')
# And its mirror: a test that forgets mirror= reads a folder that is not
# there and is refused, instead of reaching the network or a mirror named in
# the shell.
os.environ['PG_GLIBC_BUILD_MIRROR'] = 'file://' + os.path.join(SUITE_OUT_DIR,
                                                               'no-mirror/')

# The scripts import each other as top-level modules (`import glibc_locale_data
# as g`), so they are only importable with scripts/ on the path.
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# The three tags the published results are stated against.
OLD, MID, NEW = 'glibc-2.28', 'glibc-2.34', 'glibc-2.39'
PAIRS = ((OLD, MID), (MID, NEW))

# The pair below the OLD version floor. Not audited and not published as a
# result -- it is the pair that demonstrates what the pre-2.24 failure looked
# like, and test_known_answers asserts numbers from it. Those numbers need the
# same pinning as any other: the last figure published for this pair went
# stale without anyone noticing, which is exactly what a test is for.
FLOOR_OLD, FLOOR_NEW = 'glibc-2.12', 'glibc-2.17'

# The commit each of those tags MUST resolve to.
#
# A git tag is a mutable pointer, and this audit reads a third-party mirror
# (github.com/bminor/glibc). Without this, the known-answer tests assert numbers
# derived from "whatever glibc-2.39 points at today" -- and if that ever moved,
# they would fail with "the count is 51, expected 53", which reads exactly like
# a regression in our own code. Pinning turns an ambiguous failure into a
# precise one: the tag moved, or the mirror is not serving what it used to.
#
# Verified against the signed release tags. To re-derive:
#     git -C scripts/glibc rev-parse glibc-2.39^{commit}
EXPECTED_SHA = {
    'glibc-2.28': '3c03baca37fdcb52c3881e653ca392bba7a99c2b',
    'glibc-2.34': 'ae37d06c7d127817ba43850f0f898b793d42aea7',
    'glibc-2.39': 'ef321e23c20eebc6d6fb4044425c00e6df27b05f',
    'glibc-2.12': 'e28c88707ef0529593fccedf1a94c3fce3df0ef3',
    'glibc-2.17': 'c758a6861537815c759cba2018a3b1abb1943842',
}


@functools.lru_cache(maxsize=None)
def have_clone():
    """Is there a usable glibc clone to run the git-backed layers against?

    Asked of git, not of the filesystem: glibc_locale_data.compared_sides
    clones with
    --no-checkout, so localedata/locales never appears on disk.

    Asked once per process. Every decorated class of a module asks when the
    module is imported, and run_parallel imports each module once per class,
    so unremembered the answer cost thousands of git calls a run. A process
    imports its test modules before its tests run, so a test that changes
    the clone (one fetches tags into it) cannot change an answer a decorator
    was given. One test imports them while it runs:
    test_parallel_runner's test_it_counts_what_unittest_discover_counts,
    which reads only how many tests each module holds, and a skip does not
    change that. A test that needs the clone to look absent replaces this
    function, which the decorators below look up each time they run
    (test_published_claims, tests_without_a_clone).
    """
    if not os.path.isdir(GLIBC_CLONE):
        return False
    import glibc_locale_data as g
    return g._is_glibc_clone(GLIBC_CLONE)


@functools.lru_cache(maxsize=None)
def has_tags(*tags):
    """Are these tags present locally? A shallow or fresh clone may lack them.

    Asked once per process for each list of tags, in the order given, as
    have_clone is."""
    for tag in tags:
        p = subprocess.run(['git', 'rev-parse', '--verify', '--quiet',
                            f'{tag}^{{commit}}'],
                           cwd=GLIBC_CLONE, capture_output=True)
        if p.returncode != 0:
            return False
    return True


_SKIP_NO_CLONE = (
    "no glibc clone at scripts/glibc -- run ./audit.sh "
    "glibc-2.28 glibc-2.34 first. The pure-function layer still runs.")
_SKIP_NO_TAGS = (
    f"the glibc clone is missing one of {OLD}, {MID}, {NEW}; "
    f"run `git -C scripts/glibc fetch --tags`.")
_SKIP_NO_FLOOR_TAGS = (
    f"the glibc clone is missing {FLOOR_OLD} or {FLOOR_NEW}; "
    f"run `git -C scripts/glibc fetch --tags`.")



def fill_reader_cache():
    """Compute, with every processor, the localedef runs most of the suite
    asks scripts/reader_cache.py for: every locale of the five tags, and of
    MID read as RHEL 9 reads it (codepoint_collation is a keyword there),
    and what step 2 asks over the three fixture builds of
    tests/build_sources/.
    Most trees the tests build are one of these with a file or two changed,
    so after this they find nearly every run kept. Without it, the classes
    that run at once each computed nearly the same tree on their own, one
    processor each: on GitHub's four processors that took a class past its
    600 s limit and the job past 20 minutes (backlog 15.2, measured
    2026-10-14). Changes no answer: a kept run is the run.

    tests/run_parallel.py calls this before the classes start. Without a
    usable clone it does nothing; the tests that need one skip."""
    if not have_clone():
        return
    import glibc_locale_data as g
    import glibc_tree
    import reader_cache as rc

    def kept(tag, build):
        # From the listing alone, so a full cache costs a git ls-tree per
        # tree and no file read: 0.75 s where reading them took 2.4 s.
        paths, ids = g.list_tree_ids(GLIBC_CLONE, tag, [])
        return rc.kept_tree({os.path.basename(p).encode(
            'utf-8', 'surrogateescape'): ids[p] for p in paths}, build)
    for tag in (FLOOR_OLD, FLOOR_NEW, OLD, MID, NEW):
        if has_tags(tag) and not kept(tag, glibc_tree.build_at(GLIBC_CLONE,
                                                               tag)):
            glibc_tree.Tree.at_tag(GLIBC_CLONE, tag)
    # glibc_tree.across is not filled: since step 9.2 no step asks it
    # (step 2 reads glibc_tree.compare_rules), and its tests build trees of
    # their own. Filled, it cost up to 602 s of one processor whenever
    # glibc_tree.py or glibc_locale_data.py changed (measured 2026-11-07).
    # OLD as RHEL 9 reads it was filled here for
    # test_wrapper.WrapperNodesOnOneBuild (PR #117), which step 9.3 deleted
    # with steps 9 and 10; no other test gives OLD's files an el9 build.
    # MID as RHEL 9 reads it: the machine files the wrapper tests make from
    # MID's files with an el9 build, which step 2 reads as that build
    # (WrapperMachinesReadTheirOwnFiles, NodeFileCharmaps).
    if has_tags(MID):
        rhel9 = glibc_tree.build_at(GLIBC_CLONE, MID, '.el9')
        if not kept(MID, rhel9):
            paths = g.list_locale_files(GLIBC_CLONE, MID)
            contents, missing = g.read_blobs(GLIBC_CLONE, MID, paths,
                                             raw=True)
            g.require_read(MID, paths, missing, 'the locale files')
            files = {os.path.basename(p).encode('utf-8', 'surrogateescape'):
                     v for p, v in contents.items()}
            glibc_tree.Tree(MID, files, rhel9, g.decoded)
    # Step 2 over the three fixture builds, el8 -> el9 -> el10, as
    # test_build_sources.AStepReadsTheFixtureBuilds runs it, by that class's
    # own code. None of their trees is one of the tags above, and on GitHub
    # each class gets one processor: with nothing kept the class ran past
    # the 600 s limit there (main after #145, 2026-11-04).
    if has_tags(OLD, MID, NEW):
        import test_build_sources as tb
        cls = tb.AStepReadsTheFixtureBuilds
        cls.setUpClass()
        try:
            for (old, _), (new, _) in zip(tb.FIXTURES, tb.FIXTURES[1:]):
                cls('test_rhel8_to_rhel9').step2(old, new)
        finally:
            cls.tearDownClass()


def glibc_commit(base, files, tmp):
    """(repo, commit): a clone of scripts/glibc sharing its objects, made in
    `tmp`, with one commit on top of `base` whose localedata/locales holds
    `files` ({name: bytes}) added or replaced. Called again with the same
    `tmp`, it adds the commit to the clone made the first time, so `base`
    can be a commit an earlier call returned: a pair of refs for step 2.

    For a step that needs a glibc git can place: glibc_tree refuses a ref it
    cannot put before or after glibc's lr_getc fix, which a fabricated
    repository never can. Nothing is written to scripts/glibc; the commit
    carries its own identity, as a fabricated repository must."""
    repo = os.path.join(tmp, 'glibc')
    if not os.path.isdir(repo):
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout',
                        GLIBC_CLONE, repo], check=True)
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, 'index'),
               GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid')

    def git(*args, data=None):
        p = subprocess.run(['git', '-C', repo] + list(args), env=env,
                           input=data, stdout=subprocess.PIPE, check=True)
        return p.stdout.decode('ascii').strip()
    git('read-tree', base)
    for name, data in sorted(files.items()):
        oid = git('hash-object', '-w', '--stdin', data=data)
        git('update-index', '--add', '--cacheinfo',
            '100644,%s,localedata/locales/%s' % (oid, name))
    # --missing-ok: on CI's --filter=blob:none clone, write-tree checks every
    # blob of a tree it rebuilds, localedata/C.UTF-8.in among them, which
    # only an on-demand fetch would bring; one failed there on 2026-10-15.
    # The commit needs none of them to exist. A step that reads a file of
    # the tree reads it through require_read, so one it cannot read is
    # refused, never read as empty.
    tree = git('write-tree', '--missing-ok')
    commit = git('commit-tree', tree, '-p', '%s^{commit}' % base,
                 '-m', 'test locales on top of %s' % base)
    return repo, commit


def glibc_shaped(root, base=MID):
    """Make `root` a git repository whose first commit sits on `base`, a
    real glibc tag, and holds only what the fixture writes there plus
    glibc's two keyword tables and the tag's intl/locale.alias. Step 2
    reads the aliases at both sides, and compare_rules leaves out every
    alias whose locale has no file on either side, which is all of them
    unless the fixture writes a locale of that name.

    For a fabricated history that a step reads with glibc_reader: the step
    refuses a ref git cannot place before or after glibc's lr_getc fix,
    which a repository of its own never can, and reads the keyword tables
    at each ref. The objects and the tags come from scripts/glibc by
    reference (objects/info/alternates and a copy of its packed-refs), so
    nothing is copied and nothing is written there; this costs about
    0.06 s against 0.4 s for a --shared clone that checks out and deletes
    the tag's 355 locale files (backlog 15.2 B3, measured on the Mac). The
    fixture's commits carry its own identity, as before. `root` may hold
    the fixture's files already; the caller adds and commits them.

    A class that calls it needs the clone, and says so with @needs_clone,
    so that it skips without one and test_published_claims counts it.
    One that does not fails here, saying which mark it lacks, rather than
    with a FileNotFoundError (review of the whole of block 15)."""
    if not have_clone():
        raise RuntimeError('glibc_shaped needs the glibc clone; mark the '
                           'calling class @needs_clone')

    def git_(*args):
        return subprocess.run(['git', '-C', root] + list(args), check=True,
                              stdout=subprocess.PIPE).stdout.decode().strip()
    os.makedirs(root, exist_ok=True)
    git_('init', '-q')
    common = os.path.join(GLIBC_CLONE, '.git')
    with open(os.path.join(root, '.git', 'objects', 'info', 'alternates'),
              'w') as fh:
        fh.write(os.path.join(common, 'objects') + '\n')
    shutil.copyfile(os.path.join(common, 'packed-refs'),
                    os.path.join(root, '.git', 'packed-refs'))
    git_('update-ref', 'HEAD', git_('rev-parse', base + '^{commit}'))
    git_('read-tree', '--empty')
    git_('checkout', base, '--', 'locale/programs/locfile-kw.gperf',
         'locale/programs/charmap-kw.gperf', 'intl/locale.alias')


def needs_clone(cls):
    """Class decorator: skip the whole TestCase without a usable clone.

    Skipping rather than failing is deliberate. The clone is gitignored and
    several GB, so a contributor who has not built it should still get a green
    run of everything that does not need it -- and a reason printed for what
    was not checked, so a skip is never mistaken for a pass.
    """
    if not have_clone():
        return unittest.skip(_SKIP_NO_CLONE)(cls)
    if not has_tags(OLD, MID, NEW):
        return unittest.skip(_SKIP_NO_TAGS)(cls)
    return cls


def needs_floor_pair(cls):
    """Class decorator: skip without the two tags below the old version floor.

    Separate from needs_clone because these two are not part of any published
    audit. A contributor whose clone was built for one audited pair should not
    have the whole suite refuse over tags no result depends on -- but CI fetches
    every tag and fails on any skip, so there it runs.
    """
    if not have_clone():
        return unittest.skip(_SKIP_NO_CLONE)(cls)
    if not has_tags(FLOOR_OLD, FLOOR_NEW):
        return unittest.skip(_SKIP_NO_FLOOR_TAGS)(cls)
    return cls


_STEP_CACHE = {}


def run_step(script, *args, out_dir=None):
    """Run one audit script as a subprocess. Returns (exit_code, stdout+stderr).

    Memoised per (script, args, out_dir): the same step is asserted on from
    several tests, and a step can take seconds a call. Caching turns the suite
    from ~40s into ~10s without any test losing its independence -- these
    scripts are read-only against a fixed tag, so a second run cannot print
    anything different.

    Note that a cached call does NOT repeat the step's side effects. Step 2
    writes its locale list to out_dir (for audit.sh to read), so a test that
    deletes that file and calls run_step again gets the memoised output and no
    new file. Assert on files from the same (script, args, out_dir) that wrote
    them, or use run_wrapper, which is not cached.

    A subprocess, not an in-process main(), for two reasons. The scripts read
    PG_GLIBC_AUDIT_OUT at import time (glibc_locale_data.OUT_DIR), so only a
    fresh process can be pointed at a scratch directory other than this
    process's SUITE_OUT_DIR. And the
    contract these tools actually offer a user is their
    printed output and their exit status, which is what this returns.
    """
    env = dict(os.environ)
    if out_dir:
        env['PG_GLIBC_AUDIT_OUT'] = out_dir
    cmd = ([sys.executable, os.path.join(SCRIPTS_DIR, script)]
           if script.endswith('.py')
           else [os.path.join(SCRIPTS_DIR, script)])
    key = (script, args, out_dir)
    if key not in _STEP_CACHE:
        p = subprocess.run(cmd + list(args), cwd=SCRIPTS_DIR, env=env,
                           capture_output=True)
        _STEP_CACHE[key] = (p.returncode,
                            (p.stdout + p.stderr).decode('utf-8', 'replace'))
    return _STEP_CACHE[key]


def run_wrapper(*args, out_dir=None, env_extra=None):
    """Run ./audit.sh as a subprocess. Returns (exit_code, stdout+stderr).

    Deliberately NOT memoised: every wrapper test asserts on the files the run
    leaves in out_dir, and a cached call would skip writing them.
    """
    env = dict(os.environ)
    if out_dir:
        env['PG_GLIBC_AUDIT_OUT'] = out_dir
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([os.path.join(REPO_ROOT, 'audit.sh'), *args],
                       cwd=REPO_ROOT, env=env, capture_output=True)
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


# --- fixtures for C.UTF-8 ----------------------------------------------------
#
# C.UTF-8's source file exists upstream only from glibc 2.35, while RHEL8 and
# RHEL9 ship one anyway. Test modules need both shapes, and none
# can get RHEL8's from the clone -- fabricating it is the only way
# to test the locale this project's first false negative was about.

def summary_block(out, heading):
    """The body under a summary heading, one stripped line each.

    It refuses the same things removed_lines below does. The heading must be
    a whole line of the summary, exactly once, so text appended to it is
    caught, and the body must end at a blank line or the next heading.
    """
    summary = out.split('AUDIT SUMMARY', 1)[-1]
    lines = summary.splitlines()
    at = [i for i, line in enumerate(lines) if line == heading]
    if len(at) != 1:
        raise AssertionError(f'{heading!r} is a whole line {len(at)} '
                             f'time(s):\n{summary}')
    body = []
    for line in lines[at[0] + 1:]:
        if not line.strip() or line.startswith('--'):
            return body
        body.append(line.strip())
    raise AssertionError(f'{heading!r} never ends:\n{summary}')


# The summary's warnings block, whose `!!` blocks are each step's own,
# repeated once (audit.sh's awk).
SUMMARY_WARNINGS = '-- Warnings the clean results above do NOT cover'


def summary_warnings(out):
    """The summary's warnings block, whitespace collapsed; refused when the
    run printed none (summary_block refuses a heading not there once)."""
    return flat(' '.join(summary_block(out, SUMMARY_WARNINGS)))


def nothing_compared_lines(noun):
    """The summary's line under step 2's list when the two sides are one
    commit or one source tree (10.1): the order check's `!!`, in the
    warnings, says which, and what that leaves."""
    return [f'none -- nothing was compared: the two {noun} are one commit '
            f'or one',
            'source tree (the warnings below say which, and what that '
            'leaves)']


# The headings of the summary blocks 10.1 deleted, each of which said the
# order check's `!!` again in other words: none may come back.
DELETED_SUMMARY_BLOCKS = ('-- One commit, compared with itself',
                          '-- One source tree, compared with itself',
                          '-- Direction of the pair: NOT ESTABLISHED')


def assert_no_deleted_block(case, out):
    summary = out.split('AUDIT SUMMARY', 1)[-1]
    for heading in DELETED_SUMMARY_BLOCKS:
        case.assertNotIn(heading, summary)


def materialise_tag(repo, tag, dest):
    """Extract localedata/locales at `tag` into `dest`, byte-exact, and
    return the folder: a machine's locale sources as a test builds them.
    git archive rather than read_blobs, so the files are the tag's bytes."""
    p = subprocess.run(['git', 'archive', tag, 'localedata/locales'],
                       cwd=repo, capture_output=True)
    if p.returncode != 0:
        raise AssertionError(f"git archive {tag} failed: "
                             f"{p.stderr.decode('utf-8', 'replace').strip()}")
    with tempfile.TemporaryFile() as fh:
        fh.write(p.stdout)
        fh.seek(0)
        with tarfile.open(fileobj=fh, mode='r|') as tar:
            tar.extractall(dest)
    return os.path.join(dest, 'localedata', 'locales')


def flat(text):
    """Output with every run of whitespace collapsed to one space.

    glibc_locale_data.warn wraps at 78 columns, so a phrase of more than a few words is split
    across lines. Asserting on the raw text makes a POSITIVE assertion brittle
    and -- far worse -- makes a NEGATIVE one vacuous: `assertNotIn` on a phrase
    that is always broken up passes whether the warning is printed or not.
    Caught by exactly that, on 2026-09-06. Every layer that asserts on
    wrapped output goes through
    this one copy; a second copy is how two helpers drift.
    """
    return ' '.join(text.split())


# A step deleted in branch 9 (3 in step 9.2; 4, 5, 9 and 10 in step 9.3;
# 6, 7 and 8 in step 9.4b; 1 in step 9.5, which became 'What was compared')
# named in text the tool prints or writes, in any of the spellings the
# deleted texts used: "step 4", "Steps 1 to 5", "steps 2 and 5", "steps
# 9/10", "Steps 1-5", "steps 6 to 10", "steps 6 and 7", "steps 1 and 2",
# "step 1". "step 11" is not step 1, and "step 9.3" is a step of the plan,
# not of the run: neither counts. Nor does the file name step1.<pair>.log,
# which 'What was compared' keeps on purpose (audit.sh). And the scripts of
# the deleted steps by name, and the names of the lists steps 1 and 6 to 8
# wrote, so that a leftover one is never taken for this run's.
DELETED_STEP = re.compile(
    r'\b[Ss]teps?\s+(?:\d+\s*(?:,|and|to|-|/)\s*)*'
    r'(?:1|3|4|5|6|7|8|9|10)\b(?!\.\d)'
    r'|audit-locale-diff|changed_locales\.'
    r'|flag_algorithmic_ranges|diff_collation_code'
    r'|diff_distro_locales|diff_node_locales'
    r'|copy_missing\.|backports_inside_collate\.|distro_keyed_locales\.'
    r'|node_collate_diffs\.|node_collate_inherited\.'
    r'|node_removed_locales\.|node_removed_undetermined\.'
    r'|node_keyed_locales\.')


def deleted_steps_named(text):
    """Every place `text`, whitespace collapsed, names a deleted step or
    its script (DELETED_STEP)."""
    return [m.group(0) for m in DELETED_STEP.finditer(flat(text))]


# The block audit.sh prints where step 1 stood until step 9.5: each side,
# where it was read from, and what no step reads.
COMPARED_BANNER = '== WHAT WAS COMPARED'


def _compared_at(out):
    """The index of the one line that is COMPARED_BANNER; refuses none
    or two, so that a cut never runs from a banner that is not there."""
    lines = out.split('\n')
    at = [i for i, line in enumerate(lines) if line == COMPARED_BANNER]
    if len(at) != 1:
        raise AssertionError(f'{COMPARED_BANNER!r} is a line {len(at)} '
                             f'time(s):\n{out[-3000:]}')
    return lines, at[0]


def compared_text(out):
    """What audit.sh printed under 'What was compared', from the line
    after its banner's closing rule to the next banner, which must be there
    (a step's or the summary's)."""
    lines, at = _compared_at(out)
    for j in range(at + 2, len(lines)):
        if lines[j].startswith(('== STEP ', '== AUDIT SUMMARY ')):
            # The rule above the next banner, and the blank line above it,
            # are the banner's.
            return '\n'.join(lines[at + 2:j - 2])
    raise AssertionError(f'no banner ends {COMPARED_BANNER!r}:\n'
                         f'{out[-3000:]}')


def before_compared(out):
    """Everything audit.sh printed before 'What was compared', whose
    banner must be a line exactly once."""
    lines, at = _compared_at(out)
    return '\n'.join(lines[:at])


def after_compared(out):
    """Everything from 'What was compared' to the end, its banner
    included; the banner must be a line exactly once."""
    lines, at = _compared_at(out)
    return '\n'.join(lines[at:])


# Step 2's and the summary's words for step 2's list, read by the
# parsers below and by the tests (16.122): one spelling, so a rewording
# reddens the positive parsers instead of silencing a negative test.
STEP2_LISTED = 'Listed in the summary'
STEP2_NOT_COMPARED = '!! Not compared, so listed in the summary ('
# Step 2's `!!` for the files only the new side has, when the old side is an
# upstream tag (16.123); with a build or a machine there it is a plain
# heading, without the `!! `.
STEP2_ADDED = 'Not compared, no file at '
SUMMARY_STEP2 = '-- Listed by step 2'
# The `!!` report_tag_provenance prints where a tag's signature was not
# checked (16.154), which the summary's warnings repeat.
SIGNATURES_UNCHECKED = '!! Signatures NOT checked for '


class Step2Report:
    """What step 2 (filter_lc_collate_changes.py) printed, read strictly.

    totals: (names, same, differ, not compared) from its first lines, or
      None when it said nothing was compared (nothing holds why);
    differs: {name: (chars, every)}, chars a list of code points when the
      line lists them, an int when it counts them and names the file, None
      when it says which characters is not known, () when it says the
      lines name none; every is True under "every rule can change";
    added: {name: what follows its arrow}, the not-compared files only the
      new side has, as a plain block or as the `!!` block printed when the
      old side is a tag, whose C is said by the C.UTF-8 `!!` alone (C is
      then added as 'C.UTF-8 !!' when that `!!` names the old side only);
      added_warned: whether they came as the `!!` block;
      removed: {name: its "(renamed to ...)" or ''};
    listed: {name: why}, the `!!` block of names listed in the summary;
    reindex: the names of the "Listed in the summary" line; unbuilt and
      not_ascii: the two notes under it, as printed.

    Every count a heading prints must equal the names under it, every line
    of a block it reads must be one of the block's forms, and the totals
    must add up: a block it cannot read raises, never reads as empty.
    """

    def __init__(self, out):
        self.out = out
        self.totals = self.nothing = None
        self.differs, self.added, self.removed = {}, {}, {}
        self.added_warned = False
        self.sides = None
        self.listed, self.reindex = {}, []
        self.unbuilt, self.not_ascii = [], []
        self.reindex_line = False
        # A blank line ends a block, and so does the next heading: pair_check
        # prints its `!!` right above the totals, with no blank line. The
        # next block's indentation is kept.
        out = re.sub(r'(?m)^(?=The sort rules localedef builds|Rules differ|'
                     r'Not compared, no file at |!! Not compared, so listed|'
                     r'!! Not compared, no file at |'
                     r'Listed in the summary \()', '\n', out)
        paras = [p.strip('\n') for p in re.split(r'\n(?:[ \t]*\n)+', out)
                 if p.strip()]
        for para in paras:
            lines = para.split('\n')
            head = lines[0]
            if head.startswith('The sort rules localedef builds'):
                self._totals(para)
            elif head == 'Rules differ: none.':
                self._seen('differs')
            elif head.startswith('Rules differ ('):
                self._seen('differs')
                self._differs(lines)
            elif head.startswith('Not compared, no file at ') and \
                    head.endswith('listed under Removed in the summary:'):
                self._seen('removed')
                self._removed(lines)
            elif head.startswith(STEP2_ADDED):
                self._seen('added')
                self._added(lines)
            elif head.startswith('!! ' + STEP2_ADDED):
                self._seen('added')
                self._added_warned(lines)
            elif head.startswith(STEP2_NOT_COMPARED):
                self._seen('listed')
                self._listed(lines)
            elif head.startswith(STEP2_LISTED + ' ('):
                self._seen('reindex')
                self._reindex(lines)
        # Printed right under pair_check's `!!`, with no blank line between.
        nothing = re.findall(r'(?m)^Rules: not compared, (.+)$', out)
        if len(nothing) > 1:
            raise AssertionError('"Rules: not compared" printed twice')
        self.nothing = nothing[0] if nothing else None
        if self.nothing is not None and self.totals is not None:
            raise AssertionError('step 2 printed its totals and "not '
                                 'compared"')
        if self.nothing is None and self.totals is None:
            raise AssertionError(f'step 2 printed neither its totals nor '
                                 f'"not compared":\n{out[:1500]}')
        if self.totals is not None and self.sides:
            # The C.UTF-8 `!!` of a tag without the file C: when it names
            # the old side alone, C is a file only the new side has, which
            # the `!!` of the added files leaves to it.
            old = self.sides[0]
            if re.search(r'(?m)^!! ' + re.escape(old) + r' holds no '
                         r'localedata/locales/C,', out):
                if 'C' in self.added:
                    raise AssertionError('C named among the added files '
                                         'and in the C.UTF-8 `!!`')
                self.added['C'] = 'C.UTF-8 !!'
        if self.totals is not None:
            n, same, differ, nc = self.totals
            if n != same + differ + nc:
                raise AssertionError(f'step 2 totals do not add up: '
                                     f'{self.totals}')
            if differ != len(self.differs):
                raise AssertionError(f'{differ} differ and {len(self.differs)}'
                                     f' named under "Rules differ"')
            named = len(self.added) + len(self.removed) + len(self.listed)
            if nc != named:
                raise AssertionError(f'{nc} not compared and {named} named '
                                     f'under the not-compared headings')
            if not self.reindex_line:
                raise AssertionError('step 2 compared and printed no '
                                     f'"{STEP2_LISTED}" line')

    _once = None

    def _seen(self, block):
        self._once = self._once or set()
        if block in self._once:
            raise AssertionError(f'step 2 printed its {block} block twice')
        self._once.add(block)

    def _count(self, head):
        m = re.search(r'\((\d+)\)', head)
        if not m:
            raise AssertionError(f'no count in {head!r}')
        return int(m.group(1))

    def _totals(self, para):
        m = re.search(r'(\d+) name\(s\), locale files and locale\.alias '
                      r'aliases: (\d+) the same, (\d+) differ, '
                      r'(\d+) not compared\.', flat(para))
        if not m:
            raise AssertionError(f'unreadable step 2 totals: {para!r}')
        self.totals = tuple(int(x) for x in m.groups())
        m = re.search(r'The sort rules localedef builds for each locale at '
                      r'(\S+) and at (\S+?)[,:]', flat(para))
        if not m:
            raise AssertionError(f'step 2 totals name no sides: {para!r}')
        self.sides = m.groups()

    def _differs(self, lines):
        want = self._count(lines[0])
        groups, names, desc = [], None, None
        for line in lines[1:]:
            if line.startswith('      '):
                if names is None:
                    raise AssertionError(f'a description with no names '
                                         f'above it: {line!r}')
                desc.append(line.strip())
            elif line.startswith('  ') and not line.startswith('   '):
                if desc:
                    groups.append((names, desc))
                    names, desc = None, None
                names = (names or []) + [line.strip()]
                desc = desc if desc is not None else []
            else:
                raise AssertionError(f'unexpected line under Rules differ: '
                                     f'{line!r}')
        if names is not None:
            if not desc:
                raise AssertionError(f'names with no description: {names}')
            groups.append((names, desc))
        for names, desc in groups:
            text = flat(' '.join(desc))
            every = False
            tail = (', so every rule can change')
            if text.endswith(tail):
                every = True
                text = text[:-len(tail)]
                end = (' a changed line decides how other lines are read or '
                       'where other rules sort')
                if not text.endswith(end):
                    raise AssertionError(f'unreadable every-rule line: '
                                         f'{text!r}')
                text = text[:-len(end)].strip()
            if text.startswith('which characters is not known: '):
                chars = None
            elif text == 'the lines that count name no character':
                chars = ()
            elif text == '' and every:
                raise AssertionError('an every-rule line with nothing '
                                     'about the characters above it')
            else:
                m = re.fullmatch(r'(\d+) character\(s\) named by the lines '
                                 r'that changed or are read otherwise; '
                                 r'listed in (\S+)', text)
                if m:
                    chars = int(m.group(1))
                else:
                    m = re.fullmatch(r'characters in the changed rules '
                                     r'\((\d+)\): (.+)', text)
                    if not m:
                        raise AssertionError(f'unreadable characters line: '
                                             f'{text!r}')
                    chars = [int(h, 16) for h in
                             re.findall(r'U\+([0-9A-F]{4,6})', m.group(2))]
                    if len(chars) != int(m.group(1)):
                        raise AssertionError(f'says {m.group(1)} characters '
                                             f'and prints {len(chars)}')
            for name in ' '.join(names).split(', '):
                if not name or name in self.differs:
                    raise AssertionError(f'name {name!r} empty or twice')
                self.differs[name] = (chars, every)
        if len(self.differs) != want:
            raise AssertionError(f'Rules differ ({want}) names '
                                 f'{len(self.differs)}')

    def _added(self, lines):
        want = self._count(lines[0])
        for line in lines[1:]:
            if line.startswith('   ') and not line.startswith('    '):
                continue        # the note under the names
            m = re.fullmatch(r'  (\S+) -> (.+)', line)
            if not m:
                raise AssertionError(f'unexpected line under {lines[0]!r}: '
                                     f'{line!r}')
            self.added[m.group(1)] = m.group(2)
        if len(self.added) != want:
            raise AssertionError(f'{lines[0]!r} names {len(self.added)}')

    def _added_warned(self, lines):
        """The `!!` block: a heading wrapped at three blanks that ends in
        ':', then one line per file at five (seven for a line that goes
        on)."""
        want = self._count(lines[0])
        self.added_warned = True
        at = 0
        while not lines[at].endswith(':'):
            at += 1
            if at == len(lines) or not lines[at].startswith('   ') or \
                    lines[at].startswith('     '):
                raise AssertionError(f'unreadable heading {lines[0]!r}')
        for line in lines[at + 1:]:
            m = re.fullmatch(r'     (\S+) -> (.+)', line)
            if not m:
                raise AssertionError(f'unexpected line under {lines[0]!r}: '
                                     f'{line!r}')
            if m.group(1) in self.added:
                raise AssertionError(f'{m.group(1)} named twice')
            self.added[m.group(1)] = m.group(2)
        if len(self.added) != want:
            raise AssertionError(f'{lines[0]!r} names {len(self.added)}')

    def _removed(self, lines):
        want = self._count(lines[0])
        for line in lines[1:]:
            if line.startswith('   ') and not line.startswith('    '):
                continue
            m = re.fullmatch(r'  (\S+)( \(renamed to \S+\))?', line)
            if not m:
                raise AssertionError(f'unexpected line under {lines[0]!r}: '
                                     f'{line!r}')
            self.removed[m.group(1)] = (m.group(2) or '').strip()
        if len(self.removed) != want:
            raise AssertionError(f'{lines[0]!r} names {len(self.removed)}')

    def _listed(self, lines):
        want = self._count(lines[0])
        entries = []
        for line in lines[1:]:
            if line.startswith('     ') and not line.startswith('      '):
                entries.append(line.strip())
            elif line.startswith('       ') and entries:
                entries[-1] += ' ' + line.strip()
            elif line.startswith('   ') and not entries:
                continue        # the heading's own wrapped lines
            else:
                raise AssertionError(f'unexpected line in the listed block: '
                                     f'{line!r}')
        for entry in entries:
            names, sep, why = entry.partition(': ')
            if not sep:
                raise AssertionError(f'a listed entry with no reason: '
                                     f'{entry!r}')
            for name in names.split(', '):
                if name in self.listed:
                    raise AssertionError(f'{name} listed twice')
                self.listed[name] = why
        if len(self.listed) != want:
            raise AssertionError(f'listed ({want}) names {len(self.listed)}')

    def _reindex(self, lines):
        self.reindex_line = True
        want = self._count(lines[0])
        part, notes = [lines[0].split(': ', 1)[1]], []
        for line in lines[1:]:
            if line.startswith('  Not in ') or line.startswith('  Not listed'):
                notes.append([line.strip()])
            elif line.startswith('  ') and notes:
                notes[-1].append(line.strip())
            elif line.startswith('  '):
                part.append(line.strip())
            else:
                raise AssertionError(f'unexpected line under '
                                     f'{STEP2_LISTED}: {line!r}')
        text = ' '.join(part)
        self.reindex = [] if text == 'none' else text.split(', ')
        if len(self.reindex) != want:
            raise AssertionError(f'{STEP2_LISTED} ({want}) names '
                                 f'{len(self.reindex)}')
        for note in notes:
            text = ' '.join(note)
            what, _, names = text.rpartition(': ')
            if what.startswith('Not listed in localedata/SUPPORTED'):
                self.unbuilt = names.split(', ')
            elif what.startswith('Not listed: alias(es) whose name is not '
                                 'ASCII'):
                self.not_ascii = names.split(', ')
            else:
                raise AssertionError(f'unreadable note: {text!r}')


def step2_characters_file(path):
    """{name: (every rule can change, [code point, ...] or None)} from step
    2's step2_rules_characters.<pair>.tsv. Refuses a header or a row of
    another shape, and a name twice."""
    with open(path, encoding='utf-8') as fh:
        rows = fh.read().split('\n')
    if rows[-1] == '':
        rows.pop()
    if not rows or rows[0] != ('# name\tstate\tevery rule can change\t'
                               'characters'):
        raise AssertionError(f'{path}: no header: {rows[:1]}')
    got = {}
    for row in rows[1:]:
        cells = row.split('\t')
        if (len(cells) != 4 or cells[1] != 'differs'
                or cells[2] not in ('yes', 'no') or cells[0] in got):
            raise AssertionError(f'{path}: unreadable row {row!r}')
        if cells[3].startswith('not known: '):
            chars = None
        else:
            chars = [int(c[2:], 16) for c in cells[3].split()]
            if any(not re.fullmatch(r'U\+[0-9A-F]{4,}', c)
                   for c in cells[3].split()):
                raise AssertionError(f'{path}: unreadable characters {row!r}')
        got[cells[0]] = (cells[2] == 'yes', chars)
    return got


def locale_file(*body, comment='%'):
    """A minimal locale source file with an LC_COLLATE block."""
    return '\n'.join([f'comment_char {comment}', 'escape_char /', '',
                      'LC_COLLATE', *body, 'END LC_COLLATE', ''])


def backported_c():
    """localedata/locales/C as RHEL8 actually ships it.

    Copied from glibc-2.28-251.el8_10.40 on collaudit8, 2026-09-06 -- not
    invented. Six ellipsis ranges, which is why step 4 flags it.
    """
    return locale_file(
        'order_start forward',
        '<U0000>', '..', '<UFFFF>',
        '<U00010000>', '..', '<U0001FFFF>',
        '<U00020000>', '..', '<U0002FFFF>',
        '<U000E0000>', '..', '<U000EFFFF>',
        '<U000F0000>', '..', '<U000FFFFF>',
        '<U00100000>', '..', '<U0010FFFF>',
        'UNDEFINED',
        'order_end')


def upstream_c():
    """What replaced all of that: upstream from glibc 2.35, and RHEL9 by
    backport -- collaudit9's and collaudit10's C are byte-identical to each
    other.

    The prose above the keyword is quoted from the real file, and is the trap:
    it names codepoint_collation before declaring it.
    """
    return locale_file(
        "% The keyword 'codepoint_collation' in any part of any LC_COLLATE",
        '% immediately discards all collation information and causes the',
        '% locale to use strcmp/wcscmp for collation comparison.',
        'codepoint_collation')


def tag_charmaps(tag, dest, edit=None):
    """The tag's localedata/charmaps/ in dest, each file gzipped as RHEL
    installs them in /usr/share/i18n/charmaps/, standing in for a machine's
    as a tag's sources stand in for its locale sources. `edit` maps a
    charmap's name to a function of its text: a distro's patch, made by
    hand."""
    arc = subprocess.run(['git', 'archive', tag, 'localedata/charmaps'],
                         cwd=GLIBC_CLONE, capture_output=True,
                         check=True).stdout
    os.mkdir(dest)
    with tarfile.open(fileobj=io.BytesIO(arc)) as tf:
        for member in tf.getmembers():
            if member.isfile():
                name = os.path.basename(member.name)
                data = tf.extractfile(member).read()
                if edit and name in edit:
                    data = edit[name](data.decode('latin-1')).encode('latin-1')
                with open(os.path.join(dest, name + '.gz'), 'wb') as fh:
                    # Level 6, gzip's own default: nothing that reads these
                    # depends on the level, and
                    # gzip.compress's default of 9 took several times as
                    # long per tag.
                    fh.write(gzip.compress(data, compresslevel=6, mtime=0))
    return dest


def swap_alpha_beta(text):
    """ISO-8859-7 with the bytes of alpha and beta swapped: a charmap change
    no RHEL makes, which every Greek build's rules name (through
    iso14651_t1_common)."""
    a = next(ln for ln in text.split('\n') if ln.startswith('<U03B1>'))
    b = next(ln for ln in text.split('\n') if ln.startswith('<U03B2>'))
    assert '/xe1' in a and '/xe2' in b, (a, b)
    return text.replace(a, a.replace('/xe1', '/xe2')).replace(
        b, b.replace('/xe2', '/xe1'))
