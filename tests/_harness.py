"""Shared setup for the test suite: import paths, the glibc clone, subprocesses.

Nothing here asserts anything. It exists so that a test that needs the real
glibc clone SKIPS with a reason instead of failing, and so that running the
suite never writes into the shared /tmp output directory the scripts default to.
"""
import atexit
import functools
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
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



def fill_reader_cache(parts=None):
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

    The parts (FILL_PARTS) run at once on the processors reader_cache gives
    its own runs (rc._jobs: PG_GLIBC_READER_JOBS, or one per processor),
    in fill_plan's order, each after the parts it waits for. A tag whose
    tree is kept is not read again, and step 2 is not run again while its
    record holds (fill_record_holds). `parts` limits the fill to those
    parts, for the tests of the fill.

    tests/run_parallel.py calls this before the classes start. Without a
    usable clone it does nothing; the tests that need one skip."""
    if not have_clone():
        return
    import reader_cache as rc
    step2_done = not has_tags(OLD, MID, NEW) or fill_record_holds()
    todo = [(p, j) for p, j in fill_plan(rc._jobs())
            if (parts is None or p in parts) and not (
                step2_done if _is_step2(p) else _tag_part_done(p))]
    if todo:
        _fill_parts(todo, rc._jobs())


# The parts of the fill: a tag's tree read for a build (the tag's own, or
# with what a RHEL major adds), or step 2 over one pair of FIXTURES; and the
# parts each waits for.
#  - The five tags, each for its own build.
#  - MID as RHEL 9 reads it (codepoint_collation is a keyword there): the
#    machine files the wrapper tests make from MID's files with an el9
#    build, which step 2 reads as that build (WrapperMachinesReadTheirOwnFiles,
#    NodeFileCharmaps).
#  - Step 2 over the three fixture builds, el8 -> el9 -> el10, as
#    test_build_sources.AStepReadsTheFixtureBuilds runs it, by that class's
#    own code. None of their trees is one of the tags above, and on GitHub
#    each class gets one processor: with nothing kept the class ran past
#    the 600 s limit there (main after #145, 2026-11-04).
# A locale's run file in the reader's cache keeps up to 32 candidates and
# is read, added to and written back by the part that computes it
# (reader_cache.tree_runs, under a lock per tree only), so two parts that
# write other trees into one build directory at once can leave one's
# candidates out: a later tree that would have found one computes it again.
# Only time is at stake, never an answer. So the parts that may run at once
# write other directories, or one same tree, and every wait below is
# between two parts that write other trees into one directory:
# test_build_sources.TheFillRecordsEveryTreeStep2Asks derives the
# directories from reader_cache.build_id and the record, and fails on
# either.
FILL_PARTS = {
    '2.12': ((FLOOR_OLD, None), ()),
    '2.17': ((FLOOR_NEW, None), ('2.12',)),
    '2.28': ((OLD, None), ()),
    '2.34': ((MID, None), ('2.28',)),
    'el9': ((MID, '.el9'), ()),
    '2.39': ((NEW, None), ()),
    's1': (0, ('2.28', '2.34', 'el9')),
    's2': (1, ('2.39', 'el9')),
}


def _is_step2(part):
    return isinstance(FILL_PARTS[part][0], int)


def fill_plan(n):
    """[(part, processors)] for n processors, in the order the fill starts
    them, two at a time above four processors: the order measured fastest
    in step 13.6, against one part after another, with the cache empty and
    with it full. Every part of FILL_PARTS once, each given 1 to n
    processors."""
    if n <= 4:
        plan = [('2.28', n), ('2.34', n), ('el9', n), ('2.39', n - 1),
                ('s1', 1), ('2.12', 1), ('s2', 1), ('2.17', n - 1)]
    else:
        h = n // 2
        plan = [('2.28', h), ('el9', n - h), ('2.34', h), ('2.39', n - h),
                ('s1', h), ('s2', n - h), ('2.12', h), ('2.17', h)]
    return [(p, max(1, min(n, j))) for p, j in plan]


def _tag_part(part):
    """(tag, Build) of a tag part."""
    import glibc_tree
    tag, rhel = FILL_PARTS[part][0]
    return tag, glibc_tree.build_at(GLIBC_CLONE, tag, rhel)


def _tag_part_done(part):
    """No such tag in the clone, or its tree is kept for the part's build:
    from the listing alone, so a full cache costs a git ls-tree per tree and
    no file read (0.75 s where reading them took 2.4 s)."""
    import glibc_locale_data as g
    import reader_cache as rc
    if not has_tags(FILL_PARTS[part][0][0]):
        return True
    tag, build = _tag_part(part)
    paths, ids = g.list_tree_ids(GLIBC_CLONE, tag, [])
    return rc.kept_tree({os.path.basename(p).encode(
        'utf-8', 'surrogateescape'): ids[p] for p in paths}, build)


def fill_tag_part(part):
    """Every locale of a tag part's tree, read for its build: what the fill
    runs in a process of its own (FILL_TASK)."""
    import glibc_tree
    tag, build = _tag_part(part)
    glibc_tree.Tree.at_tag(GLIBC_CLONE, tag, build)


FILL_TASK = 'import _harness; _harness.fill_tag_part(%r)'


def run_parts(todo, waits, n, start):
    """Run start(part, jobs) for each (part, jobs) of `todo`, in order, each
    in a thread of its own, a part once every part it waits for has ended
    (a part not in `todo` is done already), and never more than n jobs at
    once. Returns the most jobs that were in use at once.

    A part that raises stops further starts; the parts already running are
    waited for, then RuntimeError names each failed part. Parts that can no
    longer start (they wait for each other, or for more than n) raise too,
    instead of waiting for ever."""
    import threading
    left = list(todo)
    names = {p for p, _ in todo}
    cv = threading.Condition()
    st = {'free': n, 'ended': set(), 'failed': [], 'most': 0}

    def run(part, jobs):
        why = None
        try:
            start(part, jobs)
        except BaseException as exc:  # noqa: B902 -- named below
            why = '%s (%s: %s)' % (part, type(exc).__name__, exc)
        with cv:
            st['free'] += jobs
            st['ended'].add(part)
            if why:
                st['failed'].append(why)
            cv.notify_all()

    threads = []
    try:
        with cv:
            while left and not st['failed']:
                for i, (part, jobs) in enumerate(left):
                    if jobs <= st['free'] and all(
                            w in st['ended'] or w not in names
                            for w in waits.get(part, ())):
                        st['free'] -= jobs
                        st['most'] = max(st['most'], n - st['free'])
                        th = threading.Thread(target=run, args=(part, jobs),
                                              name='fill ' + part)
                        th.start()
                        threads.append(th)
                        del left[i]
                        break
                else:
                    if st['free'] == n:
                        st['failed'].append('%s cannot start' % ', '.join(
                            p for p, _ in left))
                        break
                    cv.wait()
    finally:
        for th in threads:
            th.join()
    if st['failed']:
        raise RuntimeError('a part of the fill failed: '
                           + '; '.join(st['failed']))
    return st['most']


def _fill_parts(todo, n):
    """The parts of `todo` on run_parts: a tag in a process of its own
    (FILL_TASK), in this process's group so that the runner's cut ends it;
    a pair of step 2 by _step2_pair. Then the record, when step 2 ran."""
    spied = {}

    def start(part, jobs):
        if _is_step2(part):
            spied[part] = _step2_pair(FILL_PARTS[part][0], jobs)
        else:
            subprocess.run([sys.executable, '-c', FILL_TASK % part],
                           cwd=TESTS_DIR, check=True, env=dict(
                               os.environ, PG_GLIBC_READER_JOBS=str(jobs)))
    run_parts(todo, {p: w for p, (_, w) in FILL_PARTS.items()}, n, start)
    if spied:
        write_fill_record(spied)


def _step2_pair(i, jobs):
    """What the spy saw of step 2 over FIXTURES[i] -> FIXTURES[i + 1], run by
    test_build_sources.AStepReadsTheFixtureBuilds' own code in a class of
    its own (its own repository, builds and output folder), its process
    started on STEP2_SPY with `jobs` processors."""
    import json
    import test_build_sources as tb
    base = tb.AStepReadsTheFixtureBuilds

    def command(self, script, old, new):
        return [sys.executable, '-c', STEP2_SPY, SCRIPTS_DIR,
                os.path.realpath(REPO_ROOT), self.spy, script] + \
            base.step2_command(self, script, old, new)[2:]

    def environ(self, env):
        return dict(base.step2_environ(self, env),
                    PG_GLIBC_READER_JOBS=str(jobs))
    cls = type('FillPair%d' % i, (base,), {'step2_command': command,
                                           'step2_environ': environ})
    cls.setUpClass()
    try:
        cls.spy = os.path.join(cls.tmp, 'spy.json')
        (old, _), (new, _) = tb.FIXTURES[i], tb.FIXTURES[i + 1]
        cls('test_rhel8_to_rhel9').step2(old, new)
        with open(cls.spy, encoding='utf-8') as fh:
            return json.load(fh)
    finally:
        cls.tearDownClass()


# The record of what step 2 over the fixture builds asked the reader, kept
# beside the reader's answers. With it, a fill whose every asked tree is
# kept, and whose inputs are what they were when the record was made, skips
# step 2: only the time of asking again is saved, never an answer. The
# inputs are not a list: they are the repository's modules that the fill
# and the step 2 processes imported, their bytes, the whole of
# tests/build_sources/ and the commits of the three tags.
FILL_RECORD = 'fill-fixture-step2.json'

# Runs step 2 as the class runs it, with reader_cache.tree_runs spied: every
# tree asked (names and blob ids, the build's fields) and every module of the
# repository the process imported are written to `record` at exit.
STEP2_SPY = r"""
import atexit, json, os, runpy, sys
scripts, root, record, script = sys.argv[1:5]
sys.argv = [script] + sys.argv[5:]
sys.path.insert(0, scripts)
import reader_cache as rc
real, asked = rc.tree_runs, []
def lst(x):
    return [lst(v) for v in x] if isinstance(x, (list, tuple)) else (
        x.decode('latin-1') if isinstance(x, bytes) else x)
def spy(files, build, cache_dir=None):
    asked.append(None if cache_dir is not None else [
        sorted([k.decode('latin-1'), rc.blob_id(v)] for k, v in files.items()),
        [bool(build.pre236), bool(build.signed_char),
         lst(sorted(build.locfile_kw.items())),
         lst(sorted(build.charmap_kw.items()))], rc.build_id(build)])
    return real(files, build, cache_dir)
rc.tree_runs = spy
def dump():
    mods = sorted({os.path.realpath(m.__file__) for m in list(sys.modules.values())
                   if getattr(m, '__file__', None)} | {os.path.realpath(script)})
    with open(record, 'w') as fh:
        json.dump({'asked': asked, 'modules': [m for m in mods
                   if m.startswith(root + os.sep)]}, fh)
atexit.register(dump)
runpy.run_path(script, run_name='__main__')
"""


def _fill_build(fields):
    """A stand-in with the four fields reader_cache.build_id reads, from the
    record's JSON: bytes back from latin-1, lists back to tuples."""
    import types

    def kw(items):
        return {k.encode('latin-1'): tuple(v) for k, v in items}
    pre236, signed, lk, ck = fields
    return types.SimpleNamespace(pre236=pre236, signed_char=signed,
                                 locfile_kw=kw(lk), charmap_kw=kw(ck))


def _fill_inputs(modules):
    """{what: sha1} of every input the record holds the fill to."""
    import hashlib

    def sha(path):
        with open(path, 'rb') as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    real = os.path.realpath(REPO_ROOT)
    out = {os.path.relpath(m, real): sha(m) for m in sorted(modules)}
    h = hashlib.sha1()
    top = os.path.join(TESTS_DIR, 'build_sources')
    for d, dirs, files in sorted(os.walk(top)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, top).encode('utf-8', 'surrogateescape')
                     + b'\0' + sha(p).encode('ascii') + b'\0')
    out['tests/build_sources/'] = h.hexdigest()
    p = subprocess.run(['git', 'rev-parse'] + ['%s^{commit}' % t for t in
                                               (OLD, MID, NEW)],
                       cwd=GLIBC_CLONE, capture_output=True, check=True)
    out['tags'] = p.stdout.decode('ascii').split()
    return out


def fill_record_path():
    import reader_cache as rc
    root = os.environ.get('PG_GLIBC_READER_CACHE') or rc.DEFAULT_DIR
    return os.path.join(root, rc.VERSION, FILL_RECORD)


def _fill_kept(rec):
    """Every tree the record lists is kept, for the build it names."""
    import reader_cache as rc
    for _, ids, fields, bid in rec['trees']:
        build = _fill_build(fields)
        if rc.build_id(build) != bid or not rc.kept_tree(
                {k.encode('latin-1'): v for k, v in ids}, build):
            return False
    return True


def fill_record_holds(path=None):
    """True only when the record exists, holds every step 2 part of
    FILL_PARTS, every input it names has the bytes it had, and every tree
    step 2 asked is kept. Anything else, including a record that does not
    read, is False: step 2 then runs."""
    import json
    path = path or fill_record_path()
    try:
        with open(path, encoding='utf-8') as fh:
            rec = json.load(fh)
        modules = [os.path.join(os.path.realpath(REPO_ROOT), m)
                   for m in rec['inputs']
                   if m not in ('tests/build_sources/', 'tags')]
        if {t[0] for t in rec['trees']} != {
                p for p in FILL_PARTS if _is_step2(p)}:
            return False
        if _fill_inputs(modules) != rec['inputs']:
            return False
        return _fill_kept(rec)
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.CalledProcessError):
        return False


def write_fill_record(spied):
    """Write the record of {part: what its spy saw}, only when every tree
    it lists is kept and no spy saw a tree asked with a cache of its own;
    through reader_cache's own writer (a file of this process's own, renamed
    over the record). Returns False when it does not write for one of those
    reasons. A write that fails leaves the record as it was, which holds
    only for the inputs and trees it names; the function returns True even
    then."""
    import reader_cache as rc
    trees = []
    modules = {os.path.realpath(m.__file__) for m in list(sys.modules.values())
               if getattr(m, '__file__', None)}
    for part in sorted(spied):
        if None in spied[part]['asked']:
            return False
        trees += [[part] + t for t in spied[part]['asked']]
        modules |= set(spied[part]['modules'])
    root = os.path.realpath(REPO_ROOT) + os.sep
    rec = {'trees': trees, 'inputs': _fill_inputs(
        {m for m in modules if m.startswith(root)})}
    if not _fill_kept(rec):
        return False
    rc._write_json(fill_record_path(), rec)
    return True


def collate_bounds(text):
    """1-based (start_line, end_line) of the LC_COLLATE block as a plain
    line reading finds it, or None: the first line that starts with
    LC_COLLATE, to the first after it that starts with END LC_COLLATE,
    or to the last line of the file. The tests compare glibc_reader
    with it on every real file (test_glibc_reader) and read the
    published files with it (test_published_claims)."""
    lines = text.split('\n')
    start = None
    for idx, line in enumerate(lines, start=1):
        if start is None:
            if line.startswith('LC_COLLATE'):
                start = idx
        elif line.startswith('END LC_COLLATE'):
            return start, idx
    return (start, len(lines)) if start is not None else None


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
        if script == 'filter_lc_collate_changes.py':
            step2_ran(_STEP_CACHE[key][1], env.get('PG_GLIBC_AUDIT_OUT'),
                      list(args))
    return _STEP_CACHE[key]


def run_wrapper(*args, out_dir=None, env_extra=None, check=True):
    """Run ./audit.sh as a subprocess. Returns (exit_code, stdout+stderr).
    check=False leaves check_report to the caller: only for a run whose
    steps a test made contradict each other, which no real run does, and
    whose test asserts that check_report refuses it.

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
    out = (p.stdout + p.stderr).decode('utf-8', 'replace')
    if check:
        check_report(p.returncode, out, env['PG_GLIBC_AUDIT_OUT'])
    return p.returncode, out


# The summary's headings that check_report reads, as audit.sh prints them.
LISTED = '-- Listed by step 2'
REMOVED_HEADING = ('-- Removed: locale files the old side has and the new '
                   'side does not')
MEASURED_RUN = "-- Measured order: each machine's own glibc"
MEASURED_NOT_RUN = '-- Measured order: NOT RUN'


def _summary_names(lines, end):
    """The names printed one per line before a line starting with one of
    END, and the rest; C.UTF-8 read back as glibc's file C."""
    names = []
    for at, line in enumerate(lines):
        if line.startswith(end):
            return names, lines[at:]
        names.append('C' if line == 'C.UTF-8' else line)
    raise AssertionError(f'no line starting with {end!r}: {lines!r}')


def check_report(returncode, out, out_dir):
    """A run that printed its summary wrote report.<pair>.json, which
    report_data.load reads whole, and whose facts are the ones the run
    printed: step 2's names, compared and not, or that it compared
    nothing; whether its character set check ran; the Removed rows and
    where they come from; whether step 11 ran and whether its rules were
    set beside; every `!!` block of the logs of steps 0, 1 and 2, one by
    one, as a warning of the data file with the same text; the
    summary's warnings block, those warnings once each; and step 11's
    log against its warnings file, and that file against the report's
    warnings of step 11 (check_step11_warnings); step 11's log, summary,
    characters file and SQL files against its data file, and that file
    against the report's step11.counts, step11.findings, step11.checks
    and step11.sql (check_step11_counts); and what step 2 printed of its rules, as
    Step2Report reads it, against the report's step2.rules, kept in
    STEP2_RUNS for a test that cuts the step's text out of the run its own
    way; and what it printed of its character set check and of its
    transliterations against the report's step2.character_sets and
    step2.transliteration (charmaps_held, translit_held); and what it
    printed before and under 'What was compared' against the report's
    compared (compared_held); and the summary page beside the data file,
    report.<pair>.html, against the report (report_page_held). A run that
    printed the summary and has no data file or no page fails here,
    whatever its exit code. No run leaves a report.*.partial.
    Called on the runs of run_wrapper (the one with check=False asserts
    that this refuses it), _AuditOnBuilds and the two other runs that
    reach the summary (test_wrapper, test_build_sources), with no run of
    its own, until the text is turned off (branch 24 of the plan).
    A run that stopped before the summary is not checked here beyond the
    .partial."""
    import glibc_locale_data
    import report_data
    names = os.listdir(out_dir) if os.path.isdir(out_dir) else []
    partial = [n for n in names
               if n.startswith('report.') and n.endswith('.partial')]
    if partial:
        raise AssertionError(f'a run left {partial} in {out_dir}')
    reports = [n for n in names
               if n.startswith('report.') and n.endswith('.json')]
    m = re.search(r'^== AUDIT SUMMARY  (.+) -> (.+)$', out, re.M)
    if not m:
        return
    path = os.path.join(out_dir, 'report.%s.json'
                        % glibc_locale_data.pair_slug(m[1], m[2]))
    if not os.path.isfile(path):
        raise AssertionError(f'summary printed, exit {returncode}, and no '
                             f'{path}; written: {reports}')
    r = report_data.load(path)
    s2 = r['step2']
    # Step 2's list.
    body = summary_block(out, LISTED)
    listed = s2['listed']
    if listed['state'] == 'not reported':
        raise AssertionError(f'{path}: step 2 list {listed!r}, but audit.sh '
                             'went on')
    if listed['state'] == 'not compared':
        if not body or not body[0].startswith('none -- nothing was '
                                              'compared'):
            raise AssertionError(f'{path}: step 2 compared nothing, summary '
                                 f'says {body[:1]!r}')
        total = None
    else:
        total = len(listed['compared']) + len(listed['not_compared'])
    if total is None:
        pass
    elif total:
        shown, rest = _summary_names(body, ('Listed because', '('))
        if shown != listed['compared']:
            raise AssertionError(f'summary lists {shown!r}, {path} '
                                 f'{listed["compared"]!r}')
        if listed['not_compared']:
            if rest[:1] != ['Listed because step 2 could not compare them '
                            '(the reason for each']:
                raise AssertionError(f'no not-compared lines: {rest!r}')
            shown, rest = _summary_names(rest[2:], ('(',))
            if shown != listed['not_compared']:
                raise AssertionError(f'summary does not compare {shown!r}, '
                                     f'{path} {listed["not_compared"]!r}')
        if not rest[0].startswith(f'({total} name(s); full list: '):
            raise AssertionError(f'{total} names, summary says {rest[0]!r}')
    elif not body or not body[0].startswith(
            'none -- of what step 2 compared, nothing differs'):
        raise AssertionError(f'{path}: no names, summary says {body[:1]!r}')
    # What step 2 printed of what it compared, and of its character set
    # check, against step2_rules_by_name as the data file holds it.
    rb = s2['rules_by_name']
    if rb['state'] != 'reported':
        raise AssertionError(f'{path}: step 2 rules by name {rb!r}')
    step2 = flat(out.split('\n== STEP 2 ', 1)[1].split('\n== ', 1)[0]) \
        if '\n== STEP 2 ' in out else ''
    nothing = rb['nothing']
    said = (f"Rules: not compared, {nothing['what']}." in step2
            if nothing['state'] != 'something compared' else
            'Rules: not compared,' not in step2)
    if not said:
        raise AssertionError(f'{path}: step 2 nothing {nothing!r}, step 2 '
                             'printed otherwise')
    state = rb['charmaps']['state']
    printed = {
        'compared': 'Character sets (' in step2,
        'not run': '!! NOT RUN: the comparison of' in step2,
        'not read': ("Character sets and transliterations: not compared, a "
                     "machine's file holds no charmaps this reads.") in step2,
    }
    printed['one commit'] = printed['one tree'] = (
        f"Character sets and transliterations: not compared, "
        f"{nothing.get('what')}." in step2)
    if not printed[state] or (state != 'not run'
                              and printed['not run']):
        raise AssertionError(f'{path}: charmaps {state!r}, step 2 printed '
                             f'{printed!r}')
    # What step 2 printed of its rules against the report's step2.rules.
    rules = s2['rules']
    if rules['state'] == 'not reported':
        raise AssertionError(f'{path}: step 2 rules {rules!r}')
    lines = out.split('\n')
    at = [i for i, x in enumerate(lines) if x.startswith('== STEP 2 ')]
    if len(at) != 1:
        raise AssertionError(f'{path}: step 2 banner {len(at)} times')
    end = next(j for j in range(at[0] + 2, len(lines))
               if lines[j].startswith('== '))
    Step2Report('\n'.join(lines[at[0] + 2:end - 1]), data=rules)
    STEP2_RUNS.append((out, rules))
    # What it printed of its character set check against the report's
    # step2.character_sets.
    cs = s2['character_sets']
    if cs['state'] == 'not reported':
        raise AssertionError(f'{path}: step 2 character sets {cs!r}')
    charmaps_held('\n'.join(lines[at[0] + 2:end - 1]), cs, path,
                  (r['pair']['old'], r['pair']['new']), r['run']['word'])
    # And of its transliterations against step2.transliteration.
    tr = s2['transliteration']
    if tr['state'] == 'not reported':
        raise AssertionError(f'{path}: step 2 transliteration {tr!r}')
    translit_held('\n'.join(lines[at[0] + 2:end - 1]), tr, path,
                  (r['pair']['old'], r['pair']['new']), r['run']['word'],
                  machines=cs.get('basis') == 'measured')
    # Two machines' own sources read: the route the summary names.
    machines = (r['run']['machines']['state'] == 'given'
                and r['run']['route'] == 'builds')
    if rb['machines'] != machines:
        raise AssertionError(f'{path}: step 2 machines={rb["machines"]}, '
                             f'run {r["run"]["route"]!r} with machines '
                             f'{r["run"]["machines"]["state"]!r}')
    # Removed.
    body = summary_block(out, REMOVED_HEADING)
    removed = s2['removed']
    first = ' '.join(body)
    source = ('machine sources' if first.startswith("Each machine's locale")
              else 'build packages' if first.startswith("Each build's locale")
              else 'old side is a tag' if 'what your old distro adds' in first
              else 'new side is a tag' if 'what your new distro adds' in first
              else "tags, because of a machine's file"
              if "A machine's file made both sides read their tags" in first
              else 'tags' if 'cannot show what your distro adds' in first
              else None)
    if removed['state'] == 'not reported':
        if 'NOT REPORTED -- step 2 wrote no removal list' not in first:
            raise AssertionError(f'{path}: removed not reported, summary '
                                 f'{body!r}')
    else:
        if source != removed['source']:
            raise AssertionError(f'summary reads as {source!r}, {path} says '
                                 f'{removed["source"]!r}: {body!r}')
        rows = [f"{x['name']} (deleted)" if x['how'] == 'deleted' else
                f"{x['name']} (renamed to {x['to']})"
                for x in removed['rows']]
        rows = ['C.UTF-8 ' + x[2:] if x.startswith('C ') else x
                for x in rows]
        heads = [i for i, x in enumerate(body) if x.startswith('Between the ')]
        if len(heads) != 1:
            raise AssertionError(f'Removed has {len(heads)} Between lines')
        after = body[heads[0]:]
        if rows:
            if not after[0].endswith(':') or \
                    after[1:1 + len(rows)] != rows or \
                    not after[1 + len(rows)].startswith(
                        f'({len(rows)} name(s); full list: '):
                raise AssertionError(f'{path} removes {rows!r}, summary '
                                     f'says {after!r}')
        elif ': none -- ' not in after[0]:
            raise AssertionError(f'{path} removes nothing, summary says '
                                 f'{after!r}')
    # Step 11, and its rules beside step 2's.
    s11 = r['step11']['state']
    summary = out.split('AUDIT SUMMARY', 1)[-1].splitlines()
    ran = (MEASURED_RUN in summary, MEASURED_NOT_RUN in summary)
    if ran != {'not run': (False, True)}.get(s11, (True, False)):
        raise AssertionError(f'{path}: step 11 {s11!r}, summary headings '
                             f'{ran!r}')
    if s11 == 'reported':
        beside = r['step11']['rules_beside']['state']
        tail = flat(out.split('AUDIT SUMMARY', 1)[-1])
        said = ('not reported' if "NOT REPORTED -- step 2's rules could not "
                "be set beside" in tail else
                'not set beside' if 'Rules (step 2) are not set beside each '
                'locale:' in tail else 'set beside')
        if said != beside:
            raise AssertionError(f'{path}: rules beside {beside!r}, summary '
                                 f'reads {said!r}')
        if beside == 'not set beside' and \
                r['step11']['rules_beside']['why'] + '.' not in tail:
            raise AssertionError(f'{path}: why {r["step11"]["rules_beside"]!r}'
                                 ' is not what the summary says')
    check_warnings(r, out_dir, path)
    check_summary_warnings(r, out, path)
    check_step11_warnings(r, out_dir, path)
    check_step11_counts(r, out_dir, path)
    compared_held(out, r, path)
    report_page_held(path, r)


def report_page_held(path, r):
    """The summary page report.<pair>.html beside the data file `path`,
    held to the report `r` read from it (scripts/report_html.py).

    - Every value marked data-p in the body is the value at its place in
      `r`, in the page's spelling: a whole number with thousands
      separators; a state in capitals; step 2's file C as C.UTF-8; a
      warning's text without its `!!` margin; a closed word (data-v) in
      the words this check gives it, but a tag's signature, held by the
      colour of its badge; a name a map is keyed by (data-k) with its
      verdict.
    - Every value marked data-d is worked out again by this check's rule.
    - Each label (data-l) carries the words this check's table gives it
      (data-t), and holds only values of the places that table allows;
      a label whose words are its own first value (a build, a latest-elN,
      a tag, an SQL file) holds one entry's places in the order they are
      said, as does a machine's "compared as"; a latest-elN's whole line
      is held to its entry, its date or NO DATE included, and so are an
      SQL file's (its number of tests) and each entry no SQL file checks
      (its names, reason and "also" line). So a value under the
      wrong words, the wrong side or in another order is refused.
    - The labels the data calls for are there, as many times as it calls
      for each: the counts or NOT MEASURED, the locales on one machine
      only, step 2's files gone and new, each machine, build, latest-elN,
      tag, SQL file and entry no SQL file checks, those two kinds each
      entry once and in the data's order. So are the sentences the page says whole (data-f),
      each in this check's words, the one at the top the route's; the
      cards it places outside Not verified; and every member of step 2's
      rows and of step 11's list of what it did not do.
    - The Not verified entries are which and in which order the data
      gives; each block not done shows its state in the section the data
      calls for; the lists that hold nothing are the ones marked data-e,
      compared as a set, not by where each sits.
    - Each list that holds nothing says "(none)" ("(no SQL file was
      written)" for the SQL files).
    - Every other word of the page outside marks, labels' words, data-e,
      data-f, <title> and <style> is one of this check's own list, so a
      sentence the data does not call for is refused; a label whose words
      are its first value says that value and this check's word after it."""
    import html.parser
    page_path = path[:-len('.json')] + '.html'
    if not os.path.exists(page_path):
        raise AssertionError(f'{page_path}: no page beside the data file')
    with open(page_path, encoding='utf-8') as f:
        page = f.read()

    def fail(what):
        raise AssertionError(f'{page_path}: {what}')

    class Page(html.parser.HTMLParser):
        VOID = {'meta', 'br', 'hr', 'img', 'input', 'link'}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.marks, self.labels, self.loose = [], [], [], []
            self.fixed, self.empties = [], []
            self.section = None

        def handle_starttag(self, tag, attrs):
            if tag in self.VOID:
                return
            a = {k: (v or '') for k, v in attrs}
            node = {'tag': tag, 'a': a, 'text': [], 'section': self.section,
                    'marks': [], 'label': None}
            if 'data-p' in a or 'data-d' in a:
                self.marks.append(node)
                for x in self.stack:
                    if 'data-l' in x['a']:
                        x['marks'].append(node)
            if 'data-l' in a:
                self.labels.append(node)
            if 'data-f' in a:
                self.fixed.append(node)
            if 'data-e' in a:
                self.empties.append(node)
            if 'data-t' in a:
                owner = next((x for x in reversed(self.stack)
                              if 'data-l' in x['a']), None)
                if owner is None or owner['label'] is not None:
                    fail(f'words {tag} outside a label or twice in one')
                owner['label'] = node
            self.stack.append(node)
            if tag == 'h2':
                self.section = ''

        def handle_endtag(self, tag):
            if tag in self.VOID:
                return
            if not self.stack or self.stack[-1]['tag'] != tag:
                fail(f'</{tag}> closes nothing open')
            self.stack.pop()

        def handle_data(self, d):
            if self.stack and self.stack[-1]['tag'] == 'h2':
                self.section += d
            for x in self.stack:
                x['text'].append(d)
            held = any(('data-p' in x['a'] or 'data-d' in x['a']
                        or 'data-e' in x['a'] or 'data-f' in x['a']
                        or 'data-t' in x['a']
                        or x['tag'] in ('style', 'title'))
                       for x in self.stack)
            if not held:
                self.loose.append(d)

    p = Page()
    p.feed(page)
    p.close()
    if p.stack:
        fail(f'{p.stack[-1]["tag"]} never closed')
    # The page's own words outside marks, labels and fixed sentences: each
    # one of these, so that no sentence the data does not call for (a
    # count, "none", "sorts as before") reaches the page unseen.
    loose_words = {
        ',', '.', ';', '\u2014', 'as', 'to', 'of its', ', as commit',
        ', by its metadata of', ', the newest glibc build in',
        ': the two sides are one', 'Builds rebuilt from their packages',
        'Collation version PostgreSQL records', 'Confirm in PostgreSQL',
        'How each machine sorts every locale (the measured order)',
        'How this was compared', 'Locale source files between the',
        'Locales added and removed', 'Machines', 'Not verified',
        'On the machines (what each machine measured)',
        'Run each file unchanged on both servers, in a database of its '
        'encoding, then compare the two outputs:',
        "The SQL files that confirm a change are written from the "
        "machines' measurements.",
        'Upstream tags', 'What changes from', 'its locale sources on',
        'its locale sources and charmaps on', 'latest-elN',
        'patches change them), as commit', 'renamed from',
        'the measured order',
        'with localedata/ and locale/ from its source RPM (',
        '\u2014 no glibc version for', 'The warnings of',
        'Whatever that step warned about is not on this page.',
        "\u2014 a machine's file made both sides read their upstream tags; "
        'why is under Not verified', ', and its metadata gives',
        'this reads', 'test(s)', 'also, in PostgreSQL:'}
    for d in p.loose:
        d = re.sub(r'\s+', ' ', d).strip()
        if d and d not in loose_words:
            fail(f'words outside any mark this check does not know: {d!r}')

    def text(node):
        return ''.join(node['text'])

    def at(place):
        v = r
        for part in place.split('/'):
            key, _, idx = part.partition('#')
            if key:
                if not isinstance(v, dict) or key not in v:
                    fail(f'no {place} in the data')
                v = v[key]
            if idx:
                if not isinstance(v, list) or int(idx) >= len(v):
                    fail(f'no {place} in the data')
                v = v[int(idx)]
        return v

    def spelt(v):
        return (f'{v:,}' if isinstance(v, int) and not isinstance(v, bool)
                else str(v))

    def body(t):
        lines = t.split('\n')
        return '\n'.join([lines[0][3:]] + [ln[3:] if ln.startswith('   ')
                                           else ln for ln in lines[1:]])

    run = r['run']
    notice = ('The old side is an upstream ' + run['old_class'] + ': a file '
              'it does not have does not prove the locale was absent on the '
              'old system (see Not verified).')
    sources = {
        'machine sources': "Each machine's locale files are its own locale "
                           'sources as its file holds them, checked against '
                           'no package.',
        'build packages': "Each build's locale files are the ones its "
                          'glibc-locale-source package installs, checked '
                          'one by one.',
        'old side is a tag': 'The old side is an upstream '
                             + run['old_class'] + ', so what the old distro '
                             'adds to it is NOT CHECKED: a file only that '
                             'distro ships, and the new side does not, is '
                             'not listed. Name the old side as a build to '
                             'check it.',
        'new side is a tag': 'The new side is an upstream '
                             + run['new_class'] + ', so what the new distro '
                             'adds to it or drops is NOT CHECKED: a file '
                             'listed here may still ship there, and one it '
                             'drops is not listed. Name the new side as a '
                             'build to check it.',
        'tags': 'The ' + run['word'] + ' cannot show what your distro adds '
                'or drops: NOT '
                "CHECKED. Pass --old-node and --new-node, each machine's "
                'file, to check it.',
        "tags, because of a machine's file": 'The ' + run['word']
                                             + ' cannot show what your '
                                             'distro adds or drops: NOT '
                                             "CHECKED. A machine's file made "
                                             'both sides read their tags; why '
                                             'is under Not verified.'}
    for m in p.marks:
        a, t = m['a'], text(m)
        if 'data-p' not in a:
            continue
        place = a['data-p']
        if 'data-k' in a:          # a name a map is keyed by, and its value
            key = place[len(a['data-k']) + 1:].split('/')[0]
            if t != key or at(place) != a.get('data-v'):
                fail(f'{place} shows {t!r} as {a.get("data-v")!r}')
            continue
        v = at(place)
        if 'data-v' in a:
            if v != a['data-v']:
                fail(f'{place} is {v!r}, the page says {a["data-v"]!r}')
            if place.endswith('/status'):
                if ('ok' in a.get('class', '').split()) != (v == 'good'):
                    fail(f'{place} {v!r} shown as {a.get("class")!r}')
                continue
            words = (sources.get(v) if place == 'step2/removed/source'
                     else notice if place == 'step2/rules/added/state'
                     else {'renamed': 'renamed to',
                           'in the C.UTF-8 notice':
                               'C.UTF-8: see Not verified'}.get(v, v))
            if t != words:
                fail(f'{place} {v!r} shown as {t!r}')
            continue
        if place.endswith('/state'):
            want = v.upper()
        elif place.endswith('/text'):
            want = body(v)
        elif 'data-as' in a:
            if (v, a['data-as']) != ('C', 'C.UTF-8'):
                fail(f'{place} {v!r} shown as {a["data-as"]!r}')
            want = 'C.UTF-8'
        else:
            want = spelt(v)
        if t != want:
            fail(f'{place} shows {t!r}, the data holds {want!r}')

    def shown(place, section=False):
        return [m for m in p.marks if m['a'].get('data-p') == place
                and (section is False or m['section'] == section)]

    # The labels: the words this check gives each, the places it holds.
    new_at = r['pair']['new']
    words = {
        'not measured': ('NOT MEASURED: how many locales sort differently, '
                         'are gone or new',
                         r'step11(/counts)?/(state|why.*)$'),
        'changed': ('locales sort differently', r'step11/counts/changed$'),
        'tile only old': ('locales gone on the new machine',
                          r'step11/counts/only_old$'),
        'tile only new': ('locales new on the new machine',
                          r'step11/counts/only_new$'),
        'unchanged': ('locales unchanged', r'step11/counts/unchanged$'),
        'far': ('of them only as far as measured:',
                r'step11/counts/unchanged_as_far_as_measured$'),
        'not compared': ('locales not compared',
                         r'd:not_measured\+refused$'),
        'not measured count': ('not measured:',
                               r'step11/counts/not_measured$'),
        'refused': ('refused by glibc:', r'step11/counts/refused$'),
        'not verified': ('things NOT verified, listed below',
                         r'd:not_verified$'),
        'gone': ('Gone at ' + new_at,
                 r'pair/new$|step2/removed/(state|why|source|rows#)'),
        'new': ('New at ' + new_at,
                r'pair/new$|run/old_class$|step2/rules/(state|why|added/)'),
        'machine old': ('old', r'compared/machines#0/'),
        'machine new': ('new', r'compared/machines#1/'),
        'word': ('rpm -q glibc:', r'compared/machines#\d+/word$'),
        'locales': ('locale sources:', r'compared/machines#\d+/locales$'),
        'charmaps': ('charmaps:', r'compared/machines#\d+/charmaps/'),
        'commit': ('compared as:', r'compared/machines#\d+/(tag|commit)$'),
        'build': (None, r'compared/builds/entries#(\d+)/'),
        'latest': (None, r'compared/latest/resolved#(\d+)/'),
        'tag': (None, r'compared/tags#(\d+)/'),
        'sql': (None, r'step11/sql/files#(\d+)/'),
        'not checked': ('NOT VERIFIED by these files:',
                        r'step11/sql/not_checked#(\d+)/'),
        'cv old': ('old side:', r'compared/collation_version/old$'),
        'cv new': ('new side:', r'compared/collation_version/new$'),
        'cv same': ('both sides:', r'compared/collation_version/version$'),
        'measured old': ('Old machine measured:',
                         r'step11/counts/machines/old/'),
        'measured new': ('new machine measured:',
                         r'step11/counts/machines/new/'),
    }
    def entry(key, i):
        """The places of one entry's label, in the order it says them."""
        if key == 'latest':
            x = r['compared']['latest']['resolved'][int(i)]
            return ['name', 'build', 'folder',
                    'when/utc' if x['when']['state'] == 'dated'
                    else 'when/state']
        return {'build': ['build', 'tag', 'patches/applied', 'patches/of',
                          'commit'],
                'tag': ['ref', 'commit', 'status', 'detail'],
                'sql': ['encoding', 'file', 'tests']}[key]

    lists = {'only old': 'Not on the new machine',
             'only new': 'Only on the new machine'}
    seen = {}
    # Which entry each SQL file's and each unchecked entry's label shows,
    # in page order: each once, in the data's order (shown_order below).
    shown_order = {'sql': [], 'not checked': []}
    for lab in p.labels:
        key = lab['a']['data-l']
        if lab['label'] is None:
            fail(f'label {key!r} without its words')
        said = text(lab['label'])
        places = [m['a'].get('data-p') or 'd:' + m['a']['data-d']
                  for m in lab['marks']]
        if key in lists:
            if said != lists[key] or any(
                    m['a'].get('data-v', key) != key for m in lab['marks']):
                fail(f'label {key!r} says {said!r} over {places}')
            seen[key] = seen.get(key, 0) + 1
            continue
        if key not in words:
            fail(f'a label this check does not know: {key!r}')
        want, rx = words[key]
        if want is None:       # its words are its first value: one entry
            idx = {re.match(rx, x).group(1) for x in places
                   if re.match(rx, x)}
            if len(idx) != 1 or not all(re.match(rx, x) for x in places):
                fail(f'label {key!r} holds {places}')
            i = idx.pop()
            if key in shown_order:
                shown_order[key].append(int(i))
            if key == 'latest':
                x = r['compared']['latest']['resolved'][int(i)]
                whole = (f'{x["name"]} = {x["build"]}, the newest glibc build '
                         f'in {x["folder"]}' + (
                             f', by its metadata of {x["when"]["utc"]}'
                             if x['when']['state'] == 'dated' else
                             ', and its metadata gives NO DATE this reads'))
                if re.sub(r'\s+', ' ', text(lab)).strip() != whole:
                    fail(f'latest-elN said {text(lab)!r}, not {whole!r}')
            first = text(lab['marks'][0]) if lab['marks'] else ''
            if said != first + {'build': ':', 'latest': ' =', 'tag': ' =',
                                'sql': ' database:'}[key]:
                fail(f'label {key!r} says {said!r}')
            if [x[re.match(rx, x).end():] for x in places] != entry(key, i):
                fail(f'label {key!r} holds {places}, not in the order '
                     f'{entry(key, i)}')
            if key == 'sql':
                x = r['step11']['sql']['files'][int(i)]
                whole = (f'{x["encoding"]} database: {x["file"]} \u2014 '
                         f'{x["tests"]:,} test(s)')
                if re.sub(r'\s+', ' ', text(lab)).strip() != whole:
                    fail(f'SQL file said {text(lab)!r}, not {whole!r}')
        else:
            if said != want:
                fail(f'label {key!r} says {said!r}, not {want!r}')
            bad = [x for x in places if not re.match(rx, x)]
            if bad:
                fail(f'label {key!r} holds {bad}')
            if key == 'commit' and [x.rsplit('/', 1)[1] for x in places] != [
                    'tag', 'commit']:
                fail(f'label {key!r} holds {places}')
            if key == 'not checked':
                # One entry, whole, in its order: its names, its reason
                # and what else PostgreSQL calls it, where it says.
                i = re.match(rx, places[0]).group(1) if places else None
                shown_order[key].append(None if i is None else int(i))
                g = (r['step11']['sql']['not_checked'][int(i)]
                     if i is not None else None)
                p0 = f'step11/sql/not_checked#{i}/'
                if g is None or places != (
                        [p0 + f'names#{j}' for j in range(len(g['names']))]
                        + [p0 + 'why']
                        + ([p0 + 'also'] if 'also' in g else [])):
                    fail(f'label {key!r} holds {places}')
                whole = ('NOT VERIFIED by these files: '
                         + ', '.join(g['names']) + ' \u2014 ' + g['why']
                         # its own line (a div), joined with no blank
                         + ('also, in PostgreSQL: ' + g['also']
                            if 'also' in g else ''))
                if re.sub(r'\s+', ' ', text(lab)).strip() != whole:
                    fail(f'not checked said {text(lab)!r}, not {whole!r}')
        if key == 'not verified' and [m['a'].get('data-d') for m in
                                      lab['marks']] != ['not_verified']:
            fail('the count of what was not verified is not under its words')
        seen[key] = seen.get(key, 0) + 1

    # What the page works out, by this check's own rules.
    s11 = r['step11']
    counts = s11.get('counts', {})
    texts, nc = set(), []
    for st in ('step0', 'step1', 'step2', 'step11'):
        if r['warnings'][st]['state'] == 'not reported':
            nc.append(f'warnings/{st}')
    if s11['state'] != 'reported':
        nc.append('step11')
    for st in ('step0', 'step1', 'step2', 'step11'):
        for i, w in enumerate(r['warnings'][st].get('items', [])):
            if w['text'] in texts:
                continue
            texts.add(w['text'])
            if w['class'] == 'not checked':
                nc.append(f'warnings/{st}/items#{i}')
    rules = {'not_verified': len(nc)}
    measured = s11['state'] == 'reported' and counts.get('state') == 'reported'
    if measured:
        rules['not_measured+refused'] = (counts['not_measured']
                                         + counts['refused'])
    for m in p.marks:
        rule = m['a'].get('data-d')
        if rule is not None and (rule not in rules
                                 or text(m) != spelt(rules[rule])):
            fail(f'{rule} shows {text(m)!r}, worked out {rules.get(rule)!r}')

    # The labels the data calls for, once each.
    need = ['not verified', 'gone', 'new']
    if measured:
        need += ['changed', 'tile only old', 'tile only new', 'unchanged',
                 'not compared', 'not measured count', 'refused',
                 'measured old', 'measured new']
        if counts['unchanged_as_far_as_measured']:
            need.append('far')
    else:
        need.append('not measured')
    if s11['state'] == 'reported':
        need += ['only old', 'only new']
    c = r['compared']
    if c['state'] == 'reported':
        if run['machines']['state'] == 'given' and \
                c['note']['state'] != 'fallback':
            for i, _ in enumerate(c['machines']):
                need += ['machine ' + ('old', 'new')[i], 'word', 'locales',
                         'charmaps', 'commit']
        need += ['tag'] * len(c['tags'])
        need += ['build'] * len(c['builds'].get('entries', []))
        if c['latest']['state'] == 'reported':
            need += ['latest'] * len(c['latest']['resolved'])
        need += {'differ': ['cv old', 'cv new'], 'same': ['cv same'],
                 'unknown': []}[c['collation_version']['state']]
    if s11['state'] == 'reported' and s11['sql']['state'] == 'reported':
        need += ['sql'] * len(s11['sql']['files'])
        need += ['not checked'] * len(s11['sql']['not_checked'])
    want = {}
    for k in need:
        want[k] = want.get(k, 0) + 1
    if seen != want:
        fail(f'labels {sorted(seen.items())}, the data calls for '
             f'{sorted(want.items())}')
    # A count of labels is not which entries they show: one file or group
    # shown twice and another left out keeps the count.
    for key, entries in (('sql', s11.get('sql', {}).get('files', [])),
                         ('not checked',
                          s11.get('sql', {}).get('not_checked', []))):
        if shown_order[key] != list(range(len(entries) if s11['state'] ==
                                          'reported' else 0)):
            fail(f'label {key!r} shows the entries {shown_order[key]}, the '
                 f'data gives {len(entries)} in order')
    if s11['state'] == 'reported':
        for verdict in ('only old', 'only new'):
            names = [k for k, v in s11['verdicts'].items()
                     if v['verdict'] == verdict]
            got = [text(m) for m in p.marks
                   if m['a'].get('data-k') == 'step11/verdicts'
                   and m['a'].get('data-v') == verdict]
            if got != names:
                fail(f'{verdict} shows {got}, the data gives {names}')

    # Fixed sentences, and each value whose data calls for it.
    fixed = sorted(re.findall(r'data-f="([^"]*)"', page))
    sub = ('sub measured' if measured else 'sub unreported'
           if run['machines']['state'] == 'given' else 'sub sources')
    want_f = sorted(['disc', 'nv', sub]
                    + (['nothing'] if run['nothing_compared'] != 'none'
                       else [])
                    + (['nd-list'] if s11['state'] != 'reported'
                       and s11.get('not_done') else [])
                    + (['psql'] if s11['state'] == 'reported'
                       and s11['sql']['state'] == 'reported'
                       and s11['sql']['files'] else [])
                    + ['block ' + st for st in ('step0', 'step1', 'step2',
                                                'step11')
                       if r['warnings'][st]['state'] == 'not reported'])
    if fixed != want_f:
        fail(f'fixed sentences {fixed}, the data calls for {want_f}')
    said_f = {
        'disc': 'This report is a complement to testing on your own '
                'servers, not a guarantee. What it could not verify is '
                'listed under \u201cNot verified\u201d.',
        'nv': 'What this run could not check. None of it means \u201cno '
              'change\u201d.',
        'nothing': 'Nothing was compared',
        'nd-list': 'So none of these was checked:',
        'psql': 'psql -X -d DATABASE -f FILE > old.out 2>&1 # and new.out '
                'on the new server python3 scripts/confirm_sql.py read FILE '
                'old.out new.out',
        'block step0': "the machines' files", 'block step1': 'the two sides',
        'block step2': 'step 2', 'block step11': 'step 11',
        'sub unreported': "The two machines' files were given; what they "
                          'measured is not reported (see below).',
        'sub sources': 'Compared as ' + run['word'] + ' from their glibc '
                       "sources; no machine's files given."}
    if measured:
        m2 = counts['machines']
        said_f['sub measured'] = (
            f'Old machine measured: {m2["old"]["label"]}, '
            f'{spelt(m2["old"]["locales"])} locales; new machine measured: '
            f'{m2["new"]["label"]}, {spelt(m2["new"]["locales"])} locales.')
    for n in p.fixed:
        k = n['a']['data-f']
        if re.sub(r'\s+', ' ', text(n)).strip() != said_f[k]:
            fail(f'fixed sentence {k!r} says {text(n)!r}')
    if s11['state'] != 'reported':
        got = [m['a']['data-p'] for m in p.marks
               if m['a'].get('data-p', '').startswith('step11/not_done#')]
        if got != [f'step11/not_done#{i}'
                   for i in range(len(s11.get('not_done', [])))]:
            fail(f"step 11's list of what it did not do shown as {got}")
    if run['nothing_compared'] != 'none' and \
            len(shown('run/nothing_compared')) != 1:
        fail('nothing compared, and the page does not say it once')
    rm, ru = r['step2']['removed'], r['step2']['rules']
    if rm['state'] == 'reported':
        if len(shown('step2/removed/source')) != 1:
            fail("where the files gone come from not said once")
        for i, x in enumerate(rm['rows']):
            if len(shown(f'step2/removed/rows#{i}/to')) != (
                    x['how'] == 'renamed'):
                fail(f'removed row {i}: where it went, shown or not, '
                     'against the data')
        n = [m['a']['data-p'] for m in p.marks if re.match(
            r'step2/removed/rows#\d+/name$', m['a'].get('data-p', ''))]
        if n != [f'step2/removed/rows#{i}/name'
                 for i in range(len(rm['rows']))]:
            fail(f'removed rows shown {n}')
    if ru['state'] == 'reported':
        n = [m['a']['data-p'] for m in p.marks if re.match(
            r'step2/rules/added/rows#\d+/name$', m['a'].get('data-p', ''))]
        if n != [f'step2/rules/added/rows#{i}/name'
                 for i in range(len(ru['added']['rows']))]:
            fail(f'added rows shown {n}')
        for i, x in enumerate(ru['added']['rows']):
            sp = x['spellings']
            got = [m['a']['data-p'] for m in p.marks if m['a'].get(
                'data-p', '').startswith(f'step2/rules/added/rows#{i}/'
                                         'spellings')]
            want = ([f'step2/rules/added/rows#{i}/spellings#{j}'
                     for j in range(len(sp))] if isinstance(sp, list)
                    else [f'step2/rules/added/rows#{i}/spellings/state'])
            if got != want:
                fail(f'added row {i} spelt {got}, the data gives {want}')
            if len(shown(f'step2/rules/added/rows#{i}/renamed_from')) != (
                    x['renamed_from'] != '-'):
                fail(f'added row {i}: renamed from, shown or not, against '
                     'the data')
        if (ru['added']['state'] == 'notice') != bool(
                shown('step2/rules/added/state')):
            fail('the notice on an upstream old side, said or not, against '
                 'the data')

    # Each part not done shows its state where the data calls for it.
    states = []
    if s11['state'] != 'reported':
        states += [('step11/state', s) for s in
                   (None, 'Locales added and removed', 'Not verified',
                    'Confirm in PostgreSQL')]
    else:
        if counts.get('state') != 'reported':
            states.append(('step11/counts/state', None))
        if s11['sql']['state'] != 'reported':
            states.append(('step11/sql/state', 'Confirm in PostgreSQL'))
    for st in ('step0', 'step1', 'step2', 'step11'):
        if r['warnings'][st]['state'] == 'not reported':
            states.append((f'warnings/{st}/state', 'Not verified'))
    for place, b in (('step2/removed', rm), ('step2/rules', ru)):
        if b['state'] != 'reported':
            states.append((place + '/state', 'Locales added and removed'))
    if c['state'] != 'reported':
        states.append(('compared/state', 'How this was compared'))
    else:
        if run['machines']['state'] != 'given':
            states.append(('run/machines/state', 'How this was compared'))
        elif c['note']['state'] == 'fallback':
            states.append(('compared/note/state', 'How this was compared'))
        for i, x in enumerate(c['machines']):
            if run['machines']['state'] == 'given' and \
                    c['note']['state'] != 'fallback' and \
                    x['charmaps']['state'] != 'counted':
                states.append((f'compared/machines#{i}/charmaps/state',
                               'How this was compared'))
        if c['latest']['state'] == 'not reported':
            states.append(('compared/latest/state', 'How this was compared'))
        for i, x in enumerate(c['latest'].get('resolved', [])):
            if x['when']['state'] != 'dated':
                states.append((f'compared/latest/resolved#{i}/when/state',
                               'How this was compared'))
        if c['collation_version']['state'] == 'unknown':
            states.append(('compared/collation_version/state',
                           'How this was compared'))
    for place, section in states:
        if len(shown(place, section)) != 1:
            fail(f'{place} not shown once in {section or "the counts"}')

    # Each list that holds nothing said where it would be, and only those.
    gaps = []
    if s11['state'] == 'reported':
        for verdict in ('only old', 'only new'):
            if not any(v['verdict'] == verdict
                       for v in s11['verdicts'].values()):
                gaps.append('step11/verdicts:' + verdict)
        if s11['sql']['state'] == 'reported' and not s11['sql']['files']:
            gaps.append('step11/sql/files')
    if rm['state'] == 'reported' and not rm['rows']:
        gaps.append('step2/removed/rows')
    if ru['state'] == 'reported' and not ru['added']['rows']:
        gaps.append('step2/rules/added/rows')
    if c['state'] == 'reported' and run['machines']['state'] == 'given' \
            and c['note']['state'] != 'fallback' and not c['machines']:
        gaps.append('compared/machines')
    if not nc:
        gaps.append('not verified')
    said = re.findall(r'data-e="([^"]*)"', page)
    for n in p.empties:
        words = ('(no SQL file was written)'
                 if n['a']['data-e'] == 'step11/sql/files' else '(none)')
        if text(n) != words:
            fail(f'{n["a"]["data-e"]} empty, said as {text(n)!r}')
    if sorted(said) != sorted(gaps):
        fail(f'empty lists said {said}, the data gives {gaps}')

    # The Not verified entries: which, and in order.
    nv = [m for m in p.marks if m['section'] == 'Not verified']
    entries = []
    for m in nv:
        place = m['a'].get('data-p', '')
        if place.endswith('/title') and 'items#' in place:
            entries.append(place[:-len('/title')])
        elif place in ('step11/state', 'warnings/step0/state',
                       'warnings/step1/state', 'warnings/step2/state',
                       'warnings/step11/state'):
            entries.append(place[:-len('/state')])
    if entries != nc:
        fail(f'Not verified shows {entries}, the data gives {nc}')
    for place in nc:
        if 'items#' in place and len(shown(place + '/text')) != 1:
            fail(f'{place} without its text')

    # The warnings of class "change" this part places, each once in its
    # section; no other warning outside Not verified.
    homes = {'not_on_new_machine': 'Locales added and removed',
             'collation_version_same': 'How this was compared'}
    want_w, texts = [], set()
    for st in ('step0', 'step1', 'step2', 'step11'):
        for i, w in enumerate(r['warnings'][st].get('items', [])):
            if w['text'] not in texts:
                texts.add(w['text'])
                if w['kind'] in homes:
                    want_w.append((f'warnings/{st}/items#{i}',
                                   homes[w['kind']]))
    got_w = [(m['a']['data-p'][:-len('/title')], m['section'])
             for m in p.marks if m['a'].get('data-p', '').endswith('/title')
             and 'items#' in m['a']['data-p']
             and m['section'] != 'Not verified']
    if got_w != want_w:
        fail(f'warnings placed {got_w}, the data gives {want_w}')
    if not measured and [m for m in p.marks if m['a'].get('data-p', '')
                         .startswith('step11/counts/')
                         and m['a']['data-p'] not in
                         ('step11/counts/state', 'step11/counts/why')]:
        fail('a count of step 11 shown where its counts are not reported')


def check_warnings(r, out_dir, path):
    """The `!!` of steps 0, 1 and 2 in the report `r` read from `path`:
    every block each step's log in `out_dir` holds, as audit.sh's warnings
    block reads it, is a warning of the data file, one by one and in
    order, with the same text; and each one's data to its text
    (data_in_text)."""
    slug = os.path.basename(path)[len('report.'):-len('.json')]
    for step in ('step0', 'step1', 'step2'):
        w = r['warnings'][step]
        if w['state'] == 'not run':
            continue
        if w['state'] != 'reported':
            raise AssertionError(f'{path}: warnings of {step} {w!r}, but '
                                 'audit.sh reached its summary')
        log = os.path.join(out_dir, f'{step}.{slug}.log')
        blocks = bang_blocks(log)
        texts = [i['text'] for i in w['items']]
        if blocks != texts:
            raise AssertionError(f'{log} holds the !! blocks {blocks!r}, '
                                 f'{path} the warnings {texts!r}')
        for i in w['items']:
            data_in_text(i['kind'], i['data'], i['text'])


WARNINGS_END = '(see docs/limitations.md)'


def summary_warning_blocks(out):
    """The `!!` blocks of the summary's warnings block of a run's output,
    as printed; [] when the run printed no such block. A heading with no
    end or over no block, a second heading, or a line of no block is
    refused: a block cut short would hold fewer warnings than the run
    printed."""
    tail = out.split('AUDIT SUMMARY', 1)[-1].split('\n')
    heads = [i for i, ln in enumerate(tail) if ln == SUMMARY_WARNINGS]
    if not heads:
        return []
    if len(heads) != 1:
        raise AssertionError(f'{len(heads)} warnings headings in the summary')
    rest = tail[heads[0] + 1:]
    if WARNINGS_END not in rest:
        raise AssertionError('the summary\'s warnings block has no end')
    blocks, cur = [], None
    for line in rest[:rest.index(WARNINGS_END)]:
        if line.startswith('!!'):
            if cur is not None:
                blocks.append('\n'.join(cur))
            cur = [line]
        elif cur is not None and line.startswith('   '):
            cur.append(line)
        else:
            raise AssertionError(f'not a line of a !! block in the '
                                 f'summary\'s warnings: {line!r}')
    if cur is not None:
        blocks.append('\n'.join(cur))
    if not blocks:
        raise AssertionError('the summary prints its warnings heading over '
                             'no warning')
    return blocks


def check_summary_warnings(r, out, path):
    """The summary's warnings block of the run `out` is the warnings of
    steps 0, 1 and 2 of the report `r` read from `path`, in order, each
    text once (audit.sh repeats a block two steps printed once)."""
    texts = []
    for step in ('step0', 'step1', 'step2'):
        w = r['warnings'][step]
        if w['state'] == 'reported':
            texts += [i['text'] for i in w['items']]
    want = list(dict.fromkeys(texts))
    got = summary_warning_blocks(out)
    if got != want:
        raise AssertionError(f'the summary repeats the warnings {got!r}, '
                             f'{path} holds {want!r}')


# Data keys whose value is a word of a closed list (a side, a state, the
# kind of reason, or a reason the block prints in fixed words, as
# libc_not_checked's; how two machines' refusals compare, and the sides
# whose file cannot say, of the lines on upper()), not a name the block
# prints: checked by
# report_data.KINDS, not as a name of the text; step 11's kinds hold each
# such word to its block too (closed_phrases).
CLOSED_WORDS = {'status', 'classes', 'ask', 'side', 'state', 'reason',
                'alike', 'unknown_on'}
# The side each kind's block names first: "{old} and {new} ...", or "the
# new side ... against the old side ..." (the direction's and the reversed
# pair's). Only same_commit names one side twice.
FIRST_SIDE = {'same_commit': 'old', 'same_tree': 'old',
              'same_tree_machines': 'old', 'direction_undetermined': 'new',
              'reversed_allowed': 'new', 'added_rules_unknown': 'new'}


def closed_phrases(kind, data):
    """[(phrase, whether the block holds it)]: what step 11's block says
    of each closed word or flag its data holds (CLOSED_WORDS: a side, a
    state, a reason; named_by_bytes), of how many names its lists hold,
    and each entry of a list as the line that prints it, from that
    entry's own values, and, for the checks beside the order, the
    block's words around its data (_check_phrases): so that the word
    itself is checked too, as data_in_text checks a name, a list of data
    that leaves one out does not pass because each name it holds is in
    the block, and two entries that swap a value (a count, a reason, a
    name) do not pass because each value is somewhere in it."""
    import locale_order as lo

    def on(side):
        return 'both machines' if side == 'both' else f'{side} machine'

    def names(d):
        return len(d['names']) + d['more']
    counted = {
        'not_on_new_machine': lambda: [
            f"NOT known to be unchanged: {len(data['locales'])} locale(s). "
            f"Either"],
        'partly_measured': lambda: [
            f"Unchanged only as far as measured: {len(data['locales'])} of "
            f"them."],
        'glibc_refused': lambda: [
            f"does not open: {len({x['name'] for x in data['locales']})} "
            f"locale(s)."],
        'not_measured': lambda: [
            f"NOT known to be unchanged: "
            f"{len({x['name'] for x in data['locales']})} locale(s):"],
        'tables_disagree': lambda: [
            f"found a change in {names(data)} locale(s) whose"],
        'tables_not_compared': lambda: [
            f"sort tables: {sum(map(names, data['groups']))} locale(s)."]
            + [f"{g['why']}: {names(g)}:" for g in data['groups']],
        'measured_in_part': lambda: [
            f"Measured in part: "
            f"{len({x for g in data['groups'] for x in g['names']})} "
            f"locale(s)."],
        'charmaps_not_checked': lambda: [
            f"for {sum(len(g['names']) for g in data['groups'])} locale(s) "
            f"of the"],
    }
    out = [(x, True) for x in counted.get(kind, lambda: [])()]
    return out + [(x, True) for x in _entry_lines(kind, data, on)] \
        + _closed_words(kind, data, lo, on)


# Opens a phrase that is an entry line of its own, printed at two blanks
# with its continuation at four: it is matched only from where such a
# line starts to where it ends (data_in_text), so that an entry that
# leaves out its first name, or cuts its last value short, is not found
# inside the line that names it.
LINE = '\0'


def _entry_lines(kind, data, on):
    """closed_phrases' entries: each one's printed line, rebuilt from its
    own values (the printers of step 11's lists; locale_order.py); LINE
    before those printed as a line of their own. The lists of
    partly_measured and tables_disagree are one line each, wrapped at two
    blanks, and their counts hold their length."""
    def listed(d):
        return ', '.join(d['names']) + (f", and {d['more']} more"
                                        if d['more'] else '')

    def such_as(ex, count):
        said = (', '.join(ex[:-1]) + ' and ' + ex[-1] if len(ex) > 1
                else ex[0])
        return (', such as ' if count > 3 else ': ') + said
    if kind == 'partly_measured':
        return [', '.join(f"{x['name']} ({x['count']})"
                          for x in data['locales'])]
    if kind == 'tables_disagree':
        return [listed(data)]
    if kind == 'tables_not_compared':
        return [LINE + f"{g['why']}: {len(g['names']) + g['more']}: "
                f"{listed(g)}" for g in data['groups']]
    if kind == 'measured_in_part':
        return [LINE + f"{', '.join(g['names'])} ({g['side']} machine): "
                f"{g['charmap']} gives a character to {g['count']} "
                f"sequence(s) not measured{such_as(g['examples'], g['count'])}"
                for g in data['groups']]
    if kind == 'charmaps_not_checked':
        return [LINE + f"{', '.join(g['names'])}: {g['why']}"
                for g in data['groups']]
    if kind == 'glibc_refused':
        return [LINE + f"{x['name']} ({on(x['side'])}): "
                f"{' and '.join(x['categories'])} refused"
                for x in data['locales']]
    if kind == 'not_measured':
        return [LINE + (f"{x['name']}: {x['why']}"
                        if x['reason'] == 'different strings'
                        else f"{x['name']} ({on(x['side'])}): {x['why']}")
                for x in data['locales']]
    return []


def _closed_words(kind, data, lo, on):
    """closed_phrases' words."""
    if kind == 'tag_names_no_release':
        return [(f"The {data['side'].upper()} tag, {data['tag']},", True)]
    if kind == 'version_names_no_release':
        return [(f"The {data['side'].upper()} file was measured on "
                 f"{data['version']},", True)]
    if kind == 'charmaps_not_checked':
        return [(f"of the {data['side']} machine", True),
                (lo.NAMED_BY_BYTES_LOCALES, data['named_by_bytes'])]
    if kind == 'charmaps_machine_not_checked':
        return [('either machine' if data['side'] == 'either'
                 else f"the {data['side']} machine", True),
                (lo.NAMED_BY_BYTES_MACHINE, data['named_by_bytes'])]
    if kind == 'closing':
        # Written out here, not read from lo.CLOSINGS: the map the block was
        # written with is what this checks.
        return [({'plain': lo.CLOSING, 'tables': lo.CLOSING_TABLES,
                  'mixed': lo.CLOSING_MIXED}[data['state']], True)]
    return [(x, True) for x in _check_phrases(kind, data)] + [
        (x, False) for x in _ctype_absent(kind, data)]


def _ctype_absent(kind, data):
    """The parts of step 11's lines on the character rules and on another
    encoding that their data says they leave out: the count of the
    characters the old machine did not classify where it classified every
    one, and, on another encoding, each clause whose list is empty, so
    that a list cut to nothing does not pass because its words are no
    longer looked for, and what a line says where no machine told, in a
    line a machine told."""
    if kind in ('ctype_characters_changed', 'euc_characters_changed'):
        return (['it did not classify']
                if data['count'] == len(data['classified']) else [])
    if kind == 'other_encoding':
        return [x for x, empty in (
            ('Nor is whether upper()', not data['unknown']),
            ('does not convert (', not data['errors']),
            ('convert every byte', not data['whole']),
            ('converts every byte', not data['whole']),
            ('On the new machine glibc converts', not data['other']),
            # What only a line no machine told says, never in one that a
            # machine told.
            ('give an error there is not known', data['told']),
            ('neither its order nor whether', data['told'])) if empty]
    return []


def _check_phrases(kind, data):
    """closed_phrases of step 11's control, decimals of money and
    PostgreSQL processes (control_texts, money_texts, libc_texts), and of
    its lines on upper(), lower() and ILIKE (use_texts, _use_phrases): each
    block's words around its data, written out here from the data alone,
    from its `!!` (and money_changed's list from the words opening it) to
    the fixed words after the last value the data gives, so that a
    value cut short, a name left out or two entries that swap a value
    are not in it."""
    def up(side):
        return side.upper()

    def machines(side):
        return 'both machines' if side == 'both' else f"the {up(side)} machine"

    def earlier(side):
        files = ('the OLD and NEW files were' if side == 'both'
                 else f"the {up(side)} file was")
        return (f"{files} written by an earlier version of locale_order.py, "
                f"which did not measure this; run it again on "
                f"{machines(side)}.")
    if kind == 'control_failed':
        return [f"!! Control FAILED on the {up(data['side'])} machine: "
                f"{' and '.join(data['names'])} did not measure as byte "
                f"order,"]
    if kind == 'control_not_checked':
        # In the order the control reads them: the old machine first, C
        # before POSIX.
        said = {(x['side'], x['name']): f"{x['name']} is not in the "
                f"{up(x['side'])} file" for x in data['absent']}
        said.update({(x['side'], x['name']): f"{x['name']} could not be "
                     f"measured on the {up(x['side'])} machine "
                     f"({x['error']})" for x in data['unmeasured']})
        order = [(s, n) for s in ('old', 'new') for n in ('C', 'POSIX')]
        if len(said) != len(data['absent']) + len(data['unmeasured']):
            # One locale twice, or in both lists: an entry the block
            # cannot hold (review of 24.1c-2, round 1).
            raise AssertionError(f'{kind}: an entry twice in {data!r}')
        return ["!! Control NOT checked: "
                + '; '.join(said[k] for k in order if k in said) + '.']
    if kind == 'money_earlier_format':
        return ["!! Decimals of money NOT checked: " + earlier(data['side'])]
    if kind == 'money_changed':
        return [f"!! Decimals of money CHANGE in {len(data['locales'])} "
                f"locale(s):",
                "larger or smaller: " + ', '.join(
                    f"{x['name']} ({x['old']} -> {x['new']})"
                    for x in data['locales']) + '.']
    if kind == 'money_not_checked':
        names = {x['name'] for x in data['locales']}
        return [f"!! Decimals of money NOT checked in {len(names)} "
                f"locale(s): " + '; '.join(
                    f"{x['name']} ({x['side']} machine): {x['why']}"
                    for x in data['locales']) + '.']
    if kind == 'libc_earlier_format':
        return [f"!! PostgreSQL processes NOT checked on "
                f"{machines(data['side'])}: " + earlier(data['side'])]
    if kind == 'glibc_replaced':
        return [f"!! The glibc of the {up(data['side'])} machine was "
                f"replaced on disk"]
    if kind == 'libc_stale':
        return [f"!! PostgreSQL on the {up(data['side'])} machine: "
                f"{data['processes']} process(es) still run the glibc"]
    if kind == 'libc_other':
        return [f"!! PostgreSQL on the {up(data['side'])} machine: "
                f"{data['processes']} process(es) run another C library"]
    if kind == 'libc_not_checked':
        why = list(data['machine'])
        why += [f"{x['count']} of {x['processes']} process(es) named "
                f"{' or '.join(x['looked_for'])} {x['reason']}"
                for x in data['named']]
        if data['unnamed']:
            why.append(f"the name of {data['unnamed']} other process(es) "
                       f"could not be read")
        return [f"!! PostgreSQL processes NOT checked on the "
                f"{up(data['side'])} machine: " + '; '.join(why)
                + ". Run locale_order.py there"]
    if kind == 'libc_no_process':
        return [f"!! PostgreSQL on the {up(data['side'])} machine: no "
                f"process named {' or '.join(data['looked_for'])} was found,"]
    if kind.startswith('use_'):
        return _use_phrases(kind, data, earlier)
    if kind.startswith(('ctype_', 'euc_', 'utf8_', 'other_encoding')):
        return _ctype_phrases(kind, data, earlier)
    return []


def _use_phrases(kind, data, earlier):
    """_check_phrases of step 11's lines on upper(), lower() and ILIKE
    (use_texts): the line's `!!` with its count and the words of its
    data, and its list whole, from the words before it to its full stop
    (the pointer to the characters file, after it, is not data)."""
    fn = 'upper(), lower() and ILIKE'
    gconv = 'measure that machine again. '
    machines = {'both': 'both machines', 'new': 'the new machine',
                'old': 'the old machine'}

    def codesets(groups):
        return '; '.join(f"{g['encoding']}: {', '.join(g['names'])}"
                         for g in groups)

    def count(groups):
        return sum(len(g['names']) for g in groups)

    def lacking(p, unit):
        cs, sides = p['encoding'], p['sides']
        if p['alike'] == 'same':
            return f"{cs}: {sides[0]['count']} {unit} on both machines"
        if p['alike'] == 'as many':
            return (f"{cs}: {sides[0]['count']} {unit} on each machine, not "
                    f"the same ones")
        return f"{cs}: " + ' and '.join(
            f"{x['count']} {unit} on the {x['side']} machine"
            for x in sides) + (', not all the same'
                               if p['alike'] == 'mixed' else '')

    def entries(said):
        return '; '.join(f"{said(e)} ({', '.join(e['names'])})"
                         for e in data['entries'])

    def partial(e):
        return (' and '.join(lacking(p, 'sequence(s)') for p in e['parts'])
                + ''.join(f", not known on the {s} machine"
                          for s in e['unknown_on']))

    def wrong(e):
        text = ' and '.join(lacking(p, 'character(s)') for p in e['parts'])
        if len(e['lower']) == 1:
            k = e['lower'][0]['count']
            return text + (f", {k} of them through lower()" if k
                           else ", none through lower()")
        return text + ', through lower(): ' + ' and '.join(
            f"{x['count']} on the {x['side']} machine" for x in e['lower'])

    def names(es):
        return sum(len(e['names']) for e in es)
    if kind == 'use_earlier_format':
        return [f"!! {fn} NOT checked: " + earlier(data['side'])]
    if kind == 'use_error_new':
        return [f"!! {fn} give an ERROR on the new machine in "
                f"{count(data['groups'])} locale(s) whose encoding the old",
                gconv + codesets(data['groups']) + '.']
    if kind == 'use_error_both':
        return [f"!! {fn} give an ERROR on both machines in "
                f"{count(data['groups'])} locale(s), as before the upgrade:",
                gconv + codesets(data['groups']) + '.']
    if kind == 'use_error_moved':
        x, y = data['from'], data['to']
        before = ''.join(f"; what they do on the old machine is not known "
                         f"({w})" for w in data['why'])
        return [f"!! {fn} give an ERROR on {machines[data['side']]} in "
                f"{count(data['groups'])} locale(s) whose encoding changed "
                f"from {x} on the old machine to {y} on the new one: a "
                f"database of {x} made on the old machine opens there under "
                f"{y}, and a glibc that cannot convert {y} converts no text "
                f"that is not ASCII (\"ERROR: invalid multibyte character for "
                f"locale\"){before}. On RHEL",
                gconv + codesets(data['groups']) + '.']
    if kind == 'use_error_any_db':
        return [f"!! {fn} give an ERROR on {machines[data['side']]} in "
                f"{count(data['groups'])} locale(s) that databases of an "
                f"encoding not known here may use ({data['why']}): in such",
                gconv + codesets(data['groups']) + '.']
    if kind == 'use_error_one_side':
        other = 'old' if data['side'] == 'new' else 'new'
        said = {'refused': f" that glibc refused on the {other} machine, "
                           f"where no database could use them",
                'absent': " that the old machine does not have",
                'unknown': ''.join(f", where it is not known what they do "
                                   f"on the {other} machine ({w})"
                                   for w in data['why'])}[data['reason']]
        return [f"!! {fn} give an ERROR on the {data['side']} machine in "
                f"{count(data['groups'])} locale(s){said}. In a database",
                gconv + codesets(data['groups']) + '.']
    if kind == 'use_error_partial':
        return [f"!! {fn} give an ERROR on part of the text in "
                f"{names(data['entries'])} locale(s): in a database",
                'character for locale". ' + entries(partial) + '.']
    if kind == 'use_wrong_result':
        return [f"!! {fn} give a wrong result on part of the text in "
                f"{names(data['entries'])} locale(s): in a database",
                'or as other bytes). ' + entries(wrong) + '.']
    if kind == 'use_back_changed':
        return [f"!! {fn} CHANGE on part of the text in "
                f"{names(data['entries'])} locale(s): glibc's",
                'than on the old one. ' + entries(
                    lambda e: f"{e['encoding']}: {e['count']} character(s)")
                + '.']
    if kind == 'use_partial_unknown':
        n = sum(count(p['groups']) for p in data['places'])
        return [f"!! {fn}: whether they give an ERROR on part of the text is "
                f"NOT known in {n} locale(s): a file",
                "can refuse others. " + ' '.join(
                    f"On {machines[p['side']]}: {codesets(p['groups'])}."
                    for p in data['places'])]
    if kind == 'use_back_not_checked':
        n = sum(x['count'] for x in data['encodings'])
        nor = (f", nor, in {data['utf8']} of UTF-8, whether glibc reads all "
               f"their text as it is" if data['utf8'] else '')
        return [f"!! {fn}: whether their result converts back to the "
                f"database's bytes is NOT checked in {n} locale(s){nor}: "
                + earlier(data['side']) + ' ' + '; '.join(
                    f"{x['encoding']}: {x['count']} locale(s)"
                    for x in data['encodings']) + '.']
    if kind == 'use_not_checked':
        n = sum(count(r['groups']) for r in data['reasons'])
        return [f"!! {fn} NOT checked in {n} locale(s): " + ' '.join(
            f"{r['why']}: {codesets(r['groups'])}." for r in data['reasons'])]
    raise AssertionError(f'{kind}: no phrases for it')


def _ctype_phrases(kind, data, earlier):
    """_check_phrases of step 11's lines on the character rules
    (ctype_texts, euc_texts, utf8_texts) and on a database of another
    encoding (other_encoding_text): the line's `!!` with its counts and
    the words of its data, its list whole and in order to its full stop,
    and each part the data says the line holds. A character is written
    as the report writes one (locale_order.character, and wide for what
    UTF-8's converter gives): what is checked is that each value is the
    data's, in its place."""
    import locale_order as lo
    c = 'Character rules (LC_CTYPE)'
    euc = f'{c} in an EUC_JP, EUC_KR, EUC_CN or EUC_TW database'
    utf8 = f'{c} in a UTF8 database'
    on = {'both': 'on both machines', 'old': 'on the old machine',
          'new': 'on the new machine'}

    def cp(u):
        return int(u[2:], 16)

    def names(xs):
        return ', '.join(map(lo.shown, xs))

    def codesets(groups):
        return '; '.join(f"{g['encoding']}: {names(g['names'])}"
                         for g in groups)

    def count(groups):
        return sum(len(g['names']) for g in groups)

    def listed(items):
        """A list of items, each ended by ';' but the last by '.'."""
        return ' '.join(x + (';' if i + 1 < len(items) else '.')
                        for i, x in enumerate(items))

    def group(d, which=''):
        if not d['names']:
            return (f"the {d['locales']} locale(s) compared on both "
                    f"machines{which}")
        return f"{d['locales']} locale(s): {names(d['names'])}"

    def classified(d, head):
        known = d['classified']
        out = [f"holds one. {len(known)} of them the old machine already "
               f"classified{':' if known else '.'}"]
        if known:
            out.append(head + ' ' + listed(
                [f"{lo.character(cp(x['character']))}: {x['change']}"
                 for x in known]))
        return out

    def sequences(d, said):
        return listed([f"{x['bytes']}: {said(cp(x['old']))} -> "
                       f"{said(cp(x['new']))}" for x in d['sequences']])

    def lacking(p):
        cs, sides = p['encoding'], p['sides']
        if p['alike'] == 'same':
            return f"{cs}: {sides[0]['count']} byte(s) on both machines"
        if p['alike'] == 'as many':
            return (f"{cs}: {sides[0]['count']} byte(s) on each machine, not "
                    f"the same ones")
        return f"{cs}: " + ' and '.join(
            f"{x['count']} byte(s) on the {x['side']} machine"
            for x in sides) + (', not all the same'
                               if p['alike'] == 'mixed' else '')
    if kind == 'ctype_earlier_format':
        return [f"!! {c} NOT checked: " + earlier(data['side'])]
    if kind == 'ctype_characters_changed':
        other = data['count'] - len(data['classified'])
        return ([f"!! {c} CHANGE for {data['count']} character(s) in "
                 f"{group(data, ' that a UTF8 database can use')}: each gets"]
                + classified(data, 'classified:')
                + ([f"The other {other} it did not classify;"]
                   if other else []))
    if kind == 'ctype_bytes_changed':
        return [f"!! {c} CHANGE for {len(data['bytes'])} byte(s) in "
                f"{group(data)}. A database",
                'in regular expressions: ' + listed(
                    [f"byte {x['byte']}: {x['change']}"
                     for x in data['bytes']])]
    if kind == 'ctype_not_checked':
        where = {'both': 'both machines', 'old': 'old machine',
                 'new': 'new machine'}
        n = len({x['name'] for x in data['locales']})
        return [f"!! {c} NOT checked in {n} locale(s): " + '; '.join(
            f"{lo.shown(x['name'])} ({where[x['side']]}): {x['why']}"
            for x in data['locales']) + '.']
    if kind == 'euc_sequences_changed':
        return [f"!! {euc}: CHANGE for {len(data['sequences'])} byte "
                f"sequence(s) in {count(data['groups'])} locale(s) "
                f"({codesets(data['groups'])}): glibc converts",
                'text that holds one: ' + sequences(data, lo.character)]
    if kind == 'euc_characters_changed':
        other = data['count'] - len(data['classified'])
        return ([f"!! {euc}: CHANGE for {data['count']} character(s) in "
                 f"{count(data['groups'])} locale(s) "
                 f"({codesets(data['groups'])}): each gets"]
                + classified(data, 'classified:')
                + ([f"The other {other} it did not classify."]
                   if other else []))
    if kind == 'euc_same':
        return [f"!! {euc}: the same in the "
                f"{'other ' if data['other'] else ''}"
                f"{count(data['groups'])} locale(s) compared on both "
                f"machines:",
                f"rules change. {codesets(data['groups'])}."]
    if kind == 'euc_ascii_same':
        machine = {'both': "neither machine's glibc can",
                   'old': "the old machine's glibc could not",
                   'new': "the new machine's glibc cannot"}[data['side']]
        return [f"!! {euc}: the same for ASCII, the only text compared, in "
                f"{count(data['groups'])} locale(s), since {machine} convert "
                f"their encoding;",
                f"of the text. {codesets(data['groups'])}."]
    if kind == 'euc_not_checked':
        n = len({x for r in data['reasons'] for g in r['groups']
                 for x in g['names']})
        return [f"!! {euc}: NOT checked in {n} locale(s). " + ' '.join(
            f"{r['why']} ({on[r['side']]}): {codesets(r['groups'])}."
            for r in data['reasons'])]
    if kind == 'utf8_changed':
        return [f"!! {utf8}: CHANGE for {len(data['sequences'])} byte "
                f"sequence(s) in {data['locales']} locale(s) of UTF-8: glibc "
                f"converts",
                'text that holds one: ' + sequences(data, lo.wide)]
    if kind == 'utf8_one_side':
        other = 'old' if data['side'] == 'new' else 'new'
        said = (f"the {other.upper()} file did not measure this"
                if not data['why'] else
                f"the {other} machine could not measure this "
                f"({data['why'][0]})")
        return [f"!! {utf8}: on the {data['side']} machine glibc reads "
                f"{len(data['sequences'])} byte sequence(s) as another "
                f"character than the one they encode, in {data['locales']} "
                f"locale(s) of UTF-8,",
                f"holds one; {said}, so whether it changed is not known: "
                + sequences(data, lo.wide)]
    if kind == 'other_encoding':
        n, same = count(data['groups']), data['same']
        listed = codesets(data['groups']) + '.'
        out = [f"!! Not measured in a database of another encoding: {n} "
               f"locale(s) whose encoding PostgreSQL does not know,"]
        if not same:
            out.append(', so neither its order nor whether upper(), lower() '
                       'and ILIKE give an error there is known. ' + listed
                       if not data['told'] else
                       ', so its order there is not known. ' + listed)
        elif len(same) == n:
            out.append('its order there is the same on both machines. '
                       + listed)
        else:
            out.append(f"Its order there is the same on both machines in "
                       f"the {len(same)} whose compiled sort table")
            out.append(f"in the other {n - len(same)} it is not known. "
                       f"{listed} The same sort table on both machines: "
                       f"{names(same)}.")
        if same and not data['told']:
            out.append('Whether upper(), lower() and ILIKE give an error '
                       'there is not known.')
        if data['unknown']:
            out.append('there known ' + '; '.join(
                f"{on[x['side']]}: {codesets(x['groups'])}"
                for x in data['unknown']) + '.')
        if data['errors']:
            out.append('does not convert (' + '; '.join(
                map(lacking, data['errors'])) + ')')
        for x in data['whole']:
            many = len(x['encodings']) > 1
            out.append(f"glibc's converter{'s' if many else ''} of "
                       f"{' and '.join(x['encodings'])} "
                       f"convert{'' if many else 's'} every byte "
                       f"{on[x['side']]}")
        for x in data['other']:
            out.append(f"On the new machine glibc converts {x['count']} "
                       f"byte(s) of {x['encoding']} to another character")
        return out
    raise AssertionError(f'{kind}: no phrases for it')


def data_in_text(kind, data, text):
    """Every name and reason a warning's data holds is in the block it was
    printed in, every count above 0 is a number of it, what
    closed_phrases rebuilds from the data is in it (an entry line whole,
    from where its line opens to where the next one opens, the line of
    the names PostgreSQL also knows it by opens, or the block ends), and
    its two sides are two names, in the order the block
    names them (FIRST_SIDE): so a value taken from another variable, or
    the two sides swapped, does not pass because its
    shape is right. Compared with every blank removed, as the block wraps
    at 78 columns and may break a name at a hyphen."""
    bare = re.sub(r'\s', '', text)

    def leaves(v, key=None):
        if isinstance(v, dict):
            for k, x in v.items():
                yield from leaves(x, k)
        elif isinstance(v, list):
            for x in v:
                yield from leaves(x, key)
        elif isinstance(v, str) and key not in CLOSED_WORDS:
            yield key, v
        elif isinstance(v, int) and not isinstance(v, bool) and v > 0:
            # A count, written as the block prints it (0 is a part the
            # block leaves out, as "and N more" with none more).
            yield key, v
    for key, v in leaves(data):
        if isinstance(v, int):
            if not re.search(rf'(?<![\d,]){v}(?![\d,])', text):
                raise AssertionError(f'{kind}: {key} {v} is not a number '
                                     f'of its block {text!r}')
        elif re.sub(r'\s', '', v) not in bare:
            raise AssertionError(f'{kind}: {key} {v!r} is not in its block '
                                 f'{text!r}')
    # Each line that opens at two blanks and no third, marked LINE; an
    # entry ends where the next one opens, at the end of the block, or at
    # the line of the names PostgreSQL also knows it by (ALSO).
    import locale_order as lo
    lines = re.sub(r'\s', '', re.sub(r'\n  (?! )', LINE, '\n' + text))
    ends = '(?=' + '|'.join([re.escape(LINE), '$', re.escape(
        re.sub(r'\s', '', lo.ALSO))]) + ')'
    for phrase, held in closed_phrases(kind, data):
        p = re.sub(r'\s', '', phrase)
        if phrase.startswith(LINE):
            found = re.search(re.escape(p) + ends, lines) is not None
        else:
            found = p in bare
        if found != held:
            said = 'holds' if held else 'does not hold'
            raise AssertionError(f'{kind}: its data says its block {said} '
                                 f'{phrase!r}: {text!r}')
    if kind in FIRST_SIDE:
        if data['old'] == data['new'] and kind != 'same_commit':
            raise AssertionError(f'{kind}: old and new are both '
                                 f'{data["old"]!r}')
        first = FIRST_SIDE[kind]
        second = 'new' if first == 'old' else 'old'
        a, b = (bare.find(re.sub(r'\s', '', data[k]))
                for k in (first, second))
        if data['old'] != data['new'] and a > b:
            raise AssertionError(f'{kind}: {first} {data[first]!r} after '
                                 f'{second} {data[second]!r} in {text!r}')


def written_warnings(call):
    """call() under glibc_locale_data.WARNINGS_ENV, as audit.sh runs a
    step: (what it printed, each `!!` it wrote as data). Each one written
    is a block it printed, read by report_data.step_warnings, and its data
    is in its text (data_in_text)."""
    import contextlib
    import tempfile
    from unittest import mock
    import glibc_locale_data as g
    import report_data as rd
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'w.jsonl')
        out = io.StringIO()
        with mock.patch.dict(os.environ, {g.WARNINGS_ENV: path}), \
                contextlib.redirect_stdout(out):
            call()
        recs = (rd.step_warnings(path)['items'] if os.path.exists(path)
                else [])
    for w in recs:
        assert w['text'] + '\n' in out.getvalue(), w['text']
        data_in_text(w['kind'], w['data'], w['text'])
    return out.getvalue(), recs


def held_warnings(call):
    """written_warnings, and the converse: every `!!` block call() printed,
    as audit.sh's warnings block reads it, is one it wrote, in order. For
    a step 2 printer called directly, whose blocks no audit.sh run of the
    suite reaches."""
    out, recs = written_warnings(call)
    blocks = text_bang_blocks(out)
    texts = [w['text'] for w in recs]
    assert blocks == texts, (blocks, texts)
    return out, recs


def step11_blocks(text):
    """The `!!` blocks of step 11's report: a line opening with `!!` and
    every line after it that opens with a blank (its lists among them), up
    to one that does not or an empty one."""
    blocks, cur = [], None
    for line in text.split('\n'):
        if line.startswith('!!'):
            if cur is not None:
                blocks.append('\n'.join(cur))
            cur = [line]
        elif cur is not None and line.startswith(' '):
            cur.append(line)
        else:
            if cur is not None:
                blocks.append('\n'.join(cur))
            cur = None
    if cur is not None:
        blocks.append('\n'.join(cur))
    return blocks


def step11_written(text, path, where):
    """The `!!` step 11 printed in `text` against what it wrote to the
    warnings file `path`, one by one and in order, with the same text;
    each one is read by report_data.warning_item (a kind it does not know
    is refused) and its data is in its text (data_in_text). A file that
    is not there holds none. Returns the records."""
    import report_data as rd
    recs = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            recs = [json.loads(ln) for ln in f.read().split('\n') if ln]
    texts = [w['text'] for w in recs]
    blocks = step11_blocks(text)
    if blocks != texts:
        raise AssertionError(f'{where} prints the !! blocks {blocks!r}, '
                             f'{path} holds {texts!r}')
    for w in recs:
        if set(w) != {'kind', 'data', 'text'}:
            raise AssertionError(f'{path}: not a warning: {w!r}')
        rd.warning_item(path, w['kind'], w['data'], w['text'])
        data_in_text(w['kind'], w['data'], w['text'])
    return recs


def check_step11_warnings(r, out_dir, path):
    """Step 11's `!!` in the run of the report `r` read from `path`: its
    log's blocks are the warnings file's, one by one (step11_written), and
    the report's warnings of step 11 are that file's, one by one; no file,
    and no warnings, where step 11 did not run."""
    slug = os.path.basename(path)[len('report.'):-len('.json')]
    f = os.path.join(out_dir, f'step11.{slug}.warnings.jsonl')
    w = r['warnings']['step11']
    if r['step11']['state'] == 'not run':
        if os.path.exists(f):
            raise AssertionError(f'{f} written, but step 11 did not run')
        if w['state'] != 'not run':
            raise AssertionError(f'{path}: step 11 did not run, its '
                                 f'warnings are {w!r}')
        return
    if not os.path.exists(f):
        raise AssertionError(f'step 11 ran and wrote no {f}')
    log = os.path.join(out_dir, f'measured_order.{slug}.log')
    with open(log, encoding='utf-8') as fh:
        recs = step11_written(fh.read(), f, log)
    if w['state'] != 'reported' or [(i['kind'], i['data'], i['text'])
                                     for i in w['items']] != [
            (x['kind'], x['data'], x['text']) for x in recs]:
        raise AssertionError(f'{path}: step 11 warnings {w!r}, {f} holds '
                             f'{recs!r}')


def check_step11_counts(r, out_dir, path):
    """Step 11's counts, findings, checks and SQL files in the run of the
    report `r` read from `path`: the data file step 11 wrote holds what
    its log and its summary print and what its SQL files hold
    (step11_counts_held, given the --sql-to audit.sh always gives), and
    the report's step11.counts, step11.findings, step11.checks and
    step11.sql are that file's, each SQL file named as in the run's
    folder; no file where step 11 did not run."""
    import report_data as rd
    slug = os.path.basename(path)[len('report.'):-len('.json')]
    f = os.path.join(out_dir, f'step11.{slug}.data.json')
    s11 = r['step11']
    if s11['state'] == 'not run':
        if os.path.exists(f):
            raise AssertionError(f'{f} written, but step 11 did not run')
        return
    if s11['state'] != 'reported':
        raise AssertionError(f'{path}: step 11 is {s11["state"]!r}, but '
                             'audit.sh reached its summary')
    log = os.path.join(out_dir, f'measured_order.{slug}.log')
    summary = os.path.join(out_dir, f'measured_order_summary.{slug}.txt')
    with open(log, encoding='utf-8') as fh:
        report = fh.read()
    with open(summary, encoding='utf-8') as fh:
        brief = fh.read()
    # audit.sh always asks step 11 for its characters file, written
    # before the report: one that is not there fails here.
    chars = os.path.join(out_dir, f'measured_order_characters.{slug}.tsv')
    with open(chars, encoding='utf-8') as fh:
        text = fh.read()
    prefix = os.path.join(out_dir, f'measured_order_confirm.{slug}')
    step11_counts_held(report, brief, f, log, characters=text,
                       warnings=r['warnings']['step11'],
                       verdicts=s11['verdicts'], sql_to=prefix)
    sql = rd.step11_data(f)['sql']
    if s11['sql'] != {'state': 'reported', 'files': [
            {'encoding': x['encoding'],
             'file': os.path.basename(prefix) + f'.{x["encoding"]}.sql',
             'tests': x['tests']} for x in sql['files']],
            'not_checked': sql['not_checked']}:
        raise AssertionError(f'{path}: step 11 sql {s11["sql"]!r}, {f} '
                             f'holds {sql!r}')
    if s11['counts'] != rd.step11_counts(f):
        raise AssertionError(f'{path}: step 11 counts {s11["counts"]!r}, '
                             f'{f} holds {rd.step11_counts(f)!r}')
    if s11['findings'] != rd.step11_findings(f):
        raise AssertionError(f'{path}: step 11 findings '
                             f'{s11["findings"]!r}, {f} holds '
                             f'{rd.step11_findings(f)!r}')
    if s11['checks'] != rd.step11_checks(f):
        raise AssertionError(f'{path}: step 11 checks {s11["checks"]!r}, '
                             f'{f} holds {rd.step11_checks(f)!r}')


def _verdicts_of(path):
    """The verdicts a --verdicts-to wrote to PATH, as report_data reads
    them ({locale: {'verdict', 'words'}}); None where no --verdicts-to
    was given, 'missing' where it was and the file is not there."""
    if path is None:
        return None
    if not os.path.exists(path):
        return 'missing'
    with open(path, encoding='utf-8') as f:
        v = json.load(f)
    return {n: {'verdict': x[0], 'words': x[1]}
            for n, x in v['locales'].items()}


def _with_verdicts(argv, tmp):
    """(argv, the verdicts file): a --compare given a --verdicts-to in TMP
    where it has none, so that every comparison's verdicts are read."""
    argv = [str(a) for a in argv]
    if '--compare' not in argv:
        return argv, None
    if '--verdicts-to' in argv:
        return argv, argv[argv.index('--verdicts-to') + 1]
    v = os.path.join(tmp, 'verdicts.json')
    return argv + ['--verdicts-to', v], v


def step11_counts_held(report, summary, path, where, *, warnings,
                       unread=None, characters=None, verdicts=None,
                       sql_to=None):
    """The counts step 11 wrote to `path` (locale_order.write_counts)
    against what test_locale_order's readers take from the text it
    printed: parse_report of its report (`report`), and parse_summary of
    its summary (`summary`, None where it wrote none). Each count, each
    machine's name and number of locales, and the line of totals of both;
    then the findings it wrote beside them (step11_groups_held) and each
    group's sort table (step11_table_held), with the characters file's
    text (`characters`, None where none was written); and the checks
    beside the order (step11_checks_held),
    upper(), lower() and ILIKE (step11_use_held) and the character rules
    (step11_ctype_held), read by report_data's
    reader too, against the run's `!!` (WARNINGS, as report.json holds
    step 11's warnings); the line on the character sets and the closing
    (step11_charmaps_held), and the locales the tables alone decided
    (step11_tables_only_held). VERDICTS: what the run's --verdicts-to
    wrote, as report.json holds it (_verdicts_of), which report_data's
    readers of the verdicts' words, the closing, the tables alone and the
    character sets hold the data to; 'missing' where the run was given
    one and wrote none, refused once a report was printed. SQL_TO: the
    run's --sql-to, None where it was given none. What step 11 wrote of
    its SQL files is held on every comparison, its report read or not
    (step11_sql_files_held): to 'not asked' where SQL_TO is None, and
    otherwise to SQL_TO and to the files themselves; and to the printed
    lines where the report is read (step11_sql_held).
    A file that is not there is right only where no report was printed
    (a comparison refused). UNREAD is why the readers cannot read this
    report (a test whose locale names they cannot split): then the text
    is not read, and the file only by report_data's reader and by
    step11_sql_files_held. Its readers of each group's
    sort table run in either case: _sort_table and _tables_held, and,
    where the characters file was written, _table_rows_held against its
    rows; and so do its readers of the verdicts, the closing, the tables
    alone and the line on the character sets."""
    import report_data as rd
    printed = any(ln.startswith('Old machine: ')
                  for ln in report.split('\n'))
    if not os.path.exists(path):
        if printed:
            raise AssertionError(f'{where} printed a report and wrote no '
                                 f'{path}')
        return None
    if not printed:
        raise AssertionError(f'{where} wrote {path} and printed no report')
    c = rd.step11_counts(path)
    # report_data's reader of the checks, which audit.sh runs only on
    # its own pairs: here on every comparison, the readers' or not.
    rd._checks(f'{where}: step11.checks', rd.step11_checks(path), c,
               warnings)
    # report_data's reader of each group's sort table and of its rows in
    # the characters file, on every comparison too.
    fnd = rd.step11_findings(path)
    for i, g in enumerate(fnd['groups']):
        rd._sort_table(f'{where}: groups[{i}].sort_table', g['sort_table'],
                       g['found_by'])
    if characters is not None:
        rows = [ln.split('\t') for ln in characters.split('\n')[2:]
                if ln and not ln.startswith('# ')]
        rd._table_rows_held(where, fnd['groups'], rows)
    rd._tables_held(where, fnd['groups'], warnings)
    cm = rd.step11_data(path)['checks']['charmaps']
    # report_data's readers of the verdicts' words, the closing, the
    # locales the tables alone decided and the line on the character sets,
    # against the verdicts of the same run, on every comparison too.
    if verdicts == 'missing':
        raise AssertionError(f'{where} printed a report and wrote no '
                             'verdicts file')
    if verdicts is not None:
        basis = rd._verdicts_held(f'{where}: verdicts', verdicts, warnings)
        rd._closing_held(where, basis, warnings)
        rd._tables_only_held(f'{where}: findings.tables_only',
                             fnd['tables_only'], basis,
                             rd.step11_data(path)['checks']['control'])
        rd._charmaps_held(f'{where}: checks.charmaps', cm, fnd['groups'],
                          basis, warnings)
    sql = rd.step11_data(path)['sql']
    step11_sql_files_held(sql, sql_to, path, where)
    if unread is not None:
        if not (isinstance(unread, str) and unread):
            raise AssertionError(f'no reason why {where} is not read')
        return c
    import test_locale_order as t
    m = c['machines']
    totals = {'old': m['old']['locales'], 'changed': c['changed'],
              'unchanged': c['unchanged'],
              'partly': c['unchanged_as_far_as_measured'],
              'not measured': c['not_measured'], 'refused': c['refused'],
              'not on new': c['only_old']}
    r = t.parse_report(report)
    got = {'old': r['old'], 'new': r['new'],
           'changed': len({x for ch in r['changes'] for x in ch['names']}),
           'unchanged': r['unchanged'], 'totals': r['totals'],
           'not measured': len(r['unmeasured']), 'refused': len(r['refused']),
           'not on new': len(r['not_on_new']),
           'only new': len(r['only_new'])}
    want = {'old': (m['old']['label'], m['old']['locales']),
            'new': (m['new']['label'], m['new']['locales']),
            'changed': c['changed'], 'unchanged': c['unchanged'],
            'totals': totals, 'not measured': c['not_measured'],
            'refused': c['refused'], 'not on new': c['only_old'],
            'only new': c['only_new']}
    if got != want:
        raise AssertionError(f'{where} prints the counts {got!r}, {path} '
                             f'holds {want!r}')
    if summary is not None:
        s = t.parse_summary(summary)
        got = {'machines': s['machines'], 'changed': len(s['changes']),
               'not known': len(s['refused']) + len(s['unmeasured'])
               + len(s['not_on_new']),
               'only new': len(s['only_new']),
               'totals': t.totals_of(s['totals'])}
        want = {'machines': (m['old']['label'], m['new']['label']),
                'changed': c['changed'],
                'not known': c['refused'] + c['not_measured']
                + c['only_old'],
                'only new': c['only_new'], 'totals': totals}
        if got != want:
            raise AssertionError(f'the summary of {where} prints the counts '
                                 f'{got!r}, {path} holds {want!r}')
    files = (t.parse_characters(characters) if characters is not None
             else None)
    step11_groups_held(r, s if summary is not None else None, fnd, path,
                       where, files, characters)
    step11_table_held(r, s if summary is not None else None, fnd, path,
                      where, files)
    step11_checks_held(r, s if summary is not None else None, files,
                       rd.step11_data(path)['checks'], path, where)
    step11_use_held(r, s if summary is not None else None, files,
                    rd.step11_data(path)['checks']['postgres_use'], path,
                    where)
    step11_ctype_held(r, s if summary is not None else None, files,
                      rd.step11_data(path)['checks']['ctype'], warnings,
                      path, where)
    step11_charmaps_held(r, s if summary is not None else None, files, cm,
                         warnings, path, where)
    step11_tables_only_held(r, s if summary is not None else None, files,
                            fnd['tables_only'], path, where)
    step11_sql_held(r, sql, sql_to, path, where)
    return c


def step11_sql_files_held(sql, sql_to, path, where):
    """What step 11 wrote to PATH of its SQL files (locale_order.sql_data)
    against the run: written where it was given --sql-to (SQL_TO), not
    asked where it was not, each entry whole (report_data._sql); and
    against the files themselves, read by confirm_sql: the encodings it
    wrote, each one's number of tests (its last line), and the entries
    no file checks, each file's own notes of them, in order."""
    import confirm_sql
    import report_data as rd
    if sql_to is None:
        if sql != {'state': 'not asked'}:
            raise AssertionError(f'{where} was given no --sql-to and wrote '
                                 f'{sql!r} to {path}')
        return
    if not isinstance(sql, dict) or sql.get('state') != 'written':
        raise AssertionError(f'{where} was given --sql-to and wrote {sql!r} '
                             f'to {path}')
    rd._sql(f'{where}: sql', {
        'state': 'reported', 'not_checked': sql.get('not_checked'),
        'files': [dict(x, file=f'{sql_to}.{x.get("encoding")}.sql')
                  if isinstance(x, dict) else x
                  for x in sql.get('files') or []]})
    if set(sql) != {'state', 'files', 'not_checked'}:
        raise AssertionError(f'{path}: sql has the keys {sorted(sql)}')
    folder, base = os.path.split(sql_to)
    on_disk = sorted(n[len(base) + 1:-len('.sql')]
                     for n in os.listdir(folder or '.')
                     if n.startswith(base + '.') and n.endswith('.sql')
                     and re.fullmatch(r'[A-Z0-9_]+',
                                      n[len(base) + 1:-len('.sql')]))
    if [x['encoding'] for x in sql['files']] != on_disk:
        raise AssertionError(f'{where} wrote the SQL files of {on_disk}, '
                             f'{path} names {sql["files"]!r}')
    notes = ['not checked in PostgreSQL by this file: '
             + ', '.join(g['names']) + ': ' + g['why']
             for g in sql['not_checked']]
    for x in sql['files']:
        f = confirm_sql.Sql(f'{sql_to}.{x["encoding"]}.sql')
        said = [n for n in f.notes if n.startswith('not checked in ')]
        if f.tests != x['tests'] or said != notes:
            raise AssertionError(
                f'{sql_to}.{x["encoding"]}.sql holds {f.tests} test(s) and '
                f'{said!r}; {path} gives {x["tests"]} and {notes!r}')


def step11_sql_held(r, sql, sql_to, path, where):
    """What the report says of the SQL files, read by parse_report (R),
    against what step 11 wrote of them to PATH (SQL): each file's
    encoding, number of tests and name, in order; each entry no file
    checks, its line whole from its names and reason, and the line under
    it whole from its "also" words, or none; and no such block where the
    run was given no --sql-to (SQL_TO)."""
    import test_locale_order as t
    got = r['sql']
    if sql_to is None:
        if got is not None:
            raise AssertionError(f'{where} prints SQL files it was not '
                                 'asked for')
        return
    want = {'files': [(x['encoding'], x['tests'],
                       f'{sql_to}.{x["encoding"]}.sql')
                      for x in sql['files']],
            'not_checked': ['Not checked in PostgreSQL by these files: '
                            + ', '.join(g['names']) + ': ' + g['why'] + '.'
                            for g in sql['not_checked']],
            'also_text': ['also, in PostgreSQL: ' + g['also']
                          if 'also' in g else None
                          for g in sql['not_checked']]}
    if got is None or {k: got[k] for k in want} != want:
        raise AssertionError(f'{where} prints of its SQL files {got!r}, '
                             f'{path} holds {want!r}')


def step11_tables_only_held(r, s, files, only, path, where):
    """The line on the locales step 11 could not measure that their sort
    table alone found unchanged, as step 11 wrote its names to PATH
    (findings['tables_only']), against the names the readers take from the
    report and the summary (the first LIST_LIMIT; the count the report
    prints is held to its names by parse_report, not to the data) and from
    the characters file's note (every one)."""
    import locale_order as lo
    names = [lo.shown(x) for x in only]
    shown = names[:lo.LIST_LIMIT]
    for what, got in (('report', r['tables_only']),
                      ('summary', s['tables_only'] if s is not None else None),
                      ('characters file',
                       files.tables_only if files is not None else None)):
        if got is None:
            continue
        want = names if what == 'characters file' else shown
        if got != want:
            raise AssertionError(f'{where}: the {what} names {got!r} '
                                 f'unchanged by their tables alone, {path} '
                                 f'holds {want!r}')


def step11_charmaps_held(r, s, files, cm, warnings, path, where):
    """The line without `!!` on the character sets, as step 11 wrote it
    to PATH (charmaps_data), against what check_kind reads of the report,
    the summary (S None where none was written) and the characters
    file's notes (FILES None where none was written): none where the
    data says 'not checked', else its counts and C and POSIX aside; and
    the closing, as the readers take it from the three, the text of the
    state step 11's warnings hold."""
    import locale_order as lo
    if cm['state'] == 'not checked':
        want = []
    elif cm['state'] == 'not needed':
        want = [('charmaps', {'needed': False, 'by_table': cm['by_table'],
                              'builtin': cm['builtin']})]
    else:
        want = [('charmaps', dict({'checked': cm['checked']},
                                  **({'by_table': cm['by_table']}
                                     if cm['by_table'] else {}),
                                  builtin=cm['builtin']))]
    closing = [i['data']['state'] for i in warnings['items']
               if i['kind'] == 'closing']
    # Written out here, not read from lo.CLOSINGS (_closed_words).
    text = {'plain': lo.CLOSING, 'tables': lo.CLOSING_TABLES,
            'mixed': lo.CLOSING_MIXED}[closing[0]] if closing else None
    for what, checks, said in (
            ('report', r['checks'], r['closing']),
            ('summary', s['checks'] if s is not None else None,
             s['closing'] if s is not None else None),
            ('characters file', files.checks if files is not None else None,
             files.closing if files is not None else None)):
        if checks is None:
            continue
        got = [x for x in checks if x[0] == 'charmaps']
        if got != want:
            raise AssertionError(f'{where}: the {what} prints the character '
                                 f'sets as {got!r}, {path} holds {want!r}')
        if text is not None and said != text:
            raise AssertionError(f'{where}: the {what} closes with {said!r}, '
                                 f'the warnings hold {closing!r}')


# The control's words for C or POSIX not checked on a side (control_texts),
# and the words of why a machine's processes were not all checked
# (libc_why), the tests' own.
CONTROL_SAID = {'not in the file': '{name} is not in the {side} file',
                'not measured': '{name} could not be measured on the {side} '
                                'machine ({error})'}


def _libc_why_said(n):
    why = list(n['machine'])
    why += [f'{x["count"]} of {x["processes"]} process(es) named '
            f'{" or ".join(x["looked_for"])} {x["reason"]}'
            for x in n['named']]
    if n['unnamed']:
        why.append(f'the name of {n["unnamed"]} other process(es) could not '
                   'be read')
    return '; '.join(why)


def _checks_said(c, where):
    """[(kind, data)]: what check_kind reads of the control's, the
    decimals of money's and the PostgreSQL processes' lines, as the
    checks C step 11 wrote say it, in no particular order."""
    out = []
    ctl = c['control']
    for side in ('old', 'new'):
        names = [n for n in ('C', 'POSIX')
                 if ctl[side][n]['state'] == 'not byte order']
        if names:
            out.append(('control', {'failed': side.upper(), 'names': names}))
    unknown = [CONTROL_SAID[x['state']].format(name=n, side=side.upper(),
                                               error=x.get('error'))
               for side in ('old', 'new') for n in ('C', 'POSIX')
               for x in [ctl[side][n]] if x['state'] in CONTROL_SAID]
    if unknown:
        out.append(('control', {'unknown': '; '.join(unknown)}))
    if all(x['state'] == 'byte order' for s in ctl.values()
           for x in s.values()):
        out.append(('control', {'held': True}))
    m = c['money']
    if m['state'] == 'earlier format':
        out.append(('money', {'earlier': ['OLD', 'NEW'] if m['side'] == 'both'
                              else [m['side'].upper()]}))
    else:
        if m['changed']:
            out.append(('money', {'changed': {
                x['name']: [x['old'], x['new']] for x in m['changed']}}))
        if m['not_checked']:
            unknown = {}
            for x in m['not_checked']:
                unknown.setdefault(x['name'], {})[x['side']] = x['why']
            out.append(('money', {'unknown': unknown}))
        if not m['changed']:
            out.append(('money', {'same': m['compared']}))
    lc = c['postgres_libc']
    earlier = [s.upper() for s in ('old', 'new')
               if lc[s]['state'] == 'earlier format']
    if earlier:
        out.append(('libc', {'earlier': earlier}))
    for s in ('old', 'new'):
        p = lc[s]
        if p['state'] == 'earlier format':
            continue
        S = s.upper()
        if p['replaced']:
            out.append(('libc', {'side': S, 'replaced': True}))
        if p['stale']:
            out.append(('libc', {'side': S, 'stale': p['stale']}))
        if p['other']:
            out.append(('libc', {'side': S, 'other': p['other']}))
        why = _libc_why_said(p['not_checked'])
        if why:
            out.append(('libc', {'side': S, 'why': why}))
        if p['result'] in ('clean', 'no process found'):
            # check_kind reads these two lines with the names step 11
            # looks for written out.
            if p['looked_for'] != ['postgres', 'postmaster']:
                raise AssertionError(f'{where}: the {s} machine looked for '
                                     f'{p["looked_for"]!r}')
            out.append(('libc', {'side': S, 'clean': p['processes']}
                        if p['result'] == 'clean' else
                        {'side': S, 'none': True}))
    if lc['note']:
        out.append(('libc', {'note': True}))
    return out


def _checks_read(found):
    """check_kind's (kind, data) of the three checks, as _checks_said
    gives them: the control's reasons not checked joined again, each
    money change a list."""
    out = []
    for kind, data in found:
        if kind not in ('control', 'money', 'libc'):
            continue
        if kind == 'control' and 'unknown' in data:
            data = {'unknown': '; '.join(data['unknown'])}
        if kind == 'money' and 'changed' in data:
            data = {'changed': {k: list(v)
                                for k, v in data['changed'].items()}}
        out.append((kind, data))
    return out


def step11_checks_held(r, s, files, checks, path, where):
    """step 11's checks beside the order, as it wrote them to PATH
    (control_data, money_data, libc_data), against what check_kind reads
    of the report (parse_report), of the summary (parse_summary; S None
    where none was written) and of the characters file's notes
    (parse_characters; FILES None where none was written): the same
    lines, each with the same states, counts, sides and reasons."""
    want = sorted(json.dumps(x, sort_keys=True)
                  for x in _checks_said(checks, where))
    for text, found in (('report', r['checks']),
                        ('summary', s['checks'] if s is not None else None),
                        ('characters file',
                         files.checks if files is not None else None)):
        if found is None:
            continue
        got = sorted(json.dumps(x, sort_keys=True)
                     for x in _checks_read(found))
        if got != want:
            raise AssertionError(f'{where}: the {text} prints the checks '
                                 f'{got!r}, {path} holds {want!r}')


# The rows of the characters file on upper(), lower() and ILIKE, and the
# words the tests give each machine and an escape of a reason (the tests'
# own, beside locale_order's).
USE_ROW_FINDINGS = ('upper/lower/ILIKE error', 'upper/lower/ILIKE not checked',
                    'upper/lower/ILIKE group', 'upper/lower back not checked',
                    'upper/lower back group', 'upper/lower back error',
                    'upper/lower back changed')
USE_WHERE = {'both': 'on both machines', 'old': 'on the old machine',
             'new': 'on the new machine'}
USE_ESCAPES = {9: '\\t', 10: '\\n', 13: '\\r', 92: '\\\\'}


def _use_measured(before, now):
    if 'not measured' not in (before, now):
        return USE_WHERE['both']
    return USE_WHERE['old' if now == 'not measured' else 'new']


def _use_spaced(h):
    return ' '.join(f'{b:02X}' for b in bytes.fromhex(h))


def _use_rows_said(u):
    """{locales: [row]}: the rows parse_characters reads of upper(),
    lower() and ILIKE, as step 11's data U say them."""
    out = {}

    def row(key, finding, **kw):
        r = {'finding': finding, 'how': None, 'test': None, 'before': None,
             'now': None, 'measured': None}
        r.update(kw)
        out.setdefault(key, []).append(r)
    for x in u.get('rows', []):
        if x['kind'] == 'not checked':
            row((x['name'],), 'upper/lower/ILIKE not checked',
                why=x['why'].translate(USE_ESCAPES))
            continue
        for r in x['rows']:
            row((x['name'],), 'upper/lower/ILIKE error',
                chars=[r['char']] if 'char' in r else [],
                bytes=_use_spaced(r['bytes']), before=r['before'],
                now=r['now'], measured=_use_measured(r['before'], r['now']))
    for i, g in enumerate(u.get('groups', []), 1):
        label = f'upper_lower_ILIKE_group_{i}'
        row(tuple(g['names']), 'upper/lower/ILIKE group', label=label,
            encoding=g['encoding'])
        for h, b, n in sorted(((bytes.fromhex(h), s['before'], s['now'])
                               for s in g['sequences'] for h in s['bytes'])):
            row((label,), 'upper/lower/ILIKE error', chars=[],
                bytes=_use_spaced(h.hex()), before=b, now=n,
                measured=_use_measured(b, n))
    for i, g in enumerate(u.get('back_groups', []), 1):
        label = f'upper_lower_back_group_{i}'
        row(tuple(g['names']), 'upper/lower back group', label=label,
            encoding=g['encoding'])
        for c in g['characters']:
            how = '; '.join(
                f"{t['fn']}(): U+{t['to']:04X}"
                + (' (itself)' if t['to'] == c['char'] else '')
                + f", which glibc's converter of {g['encoding']} does not "
                  'turn back into bytes' for t in c['through'])
            row((label,), 'upper/lower back error', chars=[c['char']],
                how=how, before=c['before'], now=c['now'],
                measured=_use_measured(c['before'], c['now']))

    def side(x):
        return (_use_spaced(x['bytes']) if x['state'] == 'bytes'
                else x['state'])
    for x in u.get('back_changed', []):
        for c in x['characters']:
            row((x['name'],), 'upper/lower back changed', chars=[c['char']],
                how=f"bytes {side(c['old'])} -> {side(c['new'])}",
                measured=USE_WHERE['both'])
    for x in u.get('part_unknown', []):
        row((x['name'],), 'upper/lower/ILIKE not checked',
            why='whether part of the text gives an error: not known '
                + USE_WHERE[x['side']])
    for x in u.get('back_not_checked', []):
        row((x['name'],), 'upper/lower back not checked',
            why=('whether glibc reads all the text as it is, and '
                 if x['utf8'] else '')
            + 'whether the result converts back to bytes: not known '
            + USE_WHERE[x['side']])
    return out


def step11_use_held(r, s, files, u, path, where):
    """step 11's data on upper(), lower() and ILIKE (use_texts), as it
    wrote them to PATH, against what check_kind reads of the line on what
    converts and of the line on what converts back, in the report, the
    summary and the characters file's notes, and against every row
    parse_characters reads of them (S, FILES None where not written)."""
    want = []
    if u['state'] == 'checked':
        d = {'converts': u['converts'], 'one byte': u['one_byte'],
             'left out': {n: g['encoding'] for g in u['left_out']
                          for n in g['names']}}
        if u['elsewhere']:
            d['elsewhere'] = u['elsewhere']
        want.append(d)
    if u['state'] != 'not checked' and u['back']:
        want.append({'back': {n: g['encoding'] for g in u['back']
                              for n in g['names']}})
    want = sorted(json.dumps(x, sort_keys=True) for x in want)
    for text, found in (('report', r['checks']),
                        ('summary', s['checks'] if s is not None else None),
                        ('characters file',
                         files.checks if files is not None else None)):
        if found is None:
            continue
        got = sorted(json.dumps(d, sort_keys=True) for k, d in found
                     if k == 'use' and ('converts' in d or 'back' in d))
        if got != want:
            raise AssertionError(f'{where}: the {text} prints {got!r}, '
                                 f'{path} holds {want!r}')
    if files is not None:
        got = {k: [x for x in rows if x['finding'] in USE_ROW_FINDINGS]
               for k, rows in files.items()}
        got = {k: v for k, v in got.items() if v}
        said = _use_rows_said(u)
        if got != said:
            keys = sorted(set(got) | set(said))
            bad = next(k for k in keys if got.get(k) != said.get(k))
            raise AssertionError(f'{where}: the characters file prints the '
                                 f'rows {got.get(bad)!r} under {bad!r}, '
                                 f'{path} holds {said.get(bad)!r}')



# What check_kind reads of the lines without `!!` this data prints: the
# same, the built-in locales and those of another encoding (the one on EUC
# compared for ASCII alone is read by its own keys, in step11_ctype_held).
CTYPE_SAID_KEYS = ({'same', 'count', 'other', 'utf8'}, {'builtin'},
                   {'elsewhere', 'with euc'})
# The words the tests give a side of a locale whose character rules were
# not checked (the tests' own, beside locale_order's).
CTYPE_WHERE = {'both': 'both machines', 'old': 'old machine',
               'new': 'new machine'}


def _ctype_how(how, own):
    """What parse_characters reads of an LC_CTYPE changed row's 'how'
    ('upper: itself -> U+A7C5; classes: none -> alpha alnum'), as the
    data writes a change: OWN, the row's own character or byte, for
    'itself'; each class list, [] for 'none'."""
    out = {}
    for part in how.split('; '):
        k, v = part.split(': ', 1)
        a, b = v.split(' -> ')
        if k == 'classes':
            out[k] = {'old': [] if a == 'none' else a.split(' '),
                      'new': [] if b == 'none' else b.split(' ')}
        else:
            out[k] = {'old': own if a == 'itself' else a,
                      'new': own if b == 'itself' else b}
    return out


def step11_ctype_held(r, s, files, c, warned, path, where):
    """step 11's data on the character rules (ctype_texts, euc_texts,
    utf8_texts), as it wrote them to PATH, against what check_kind reads
    of the lines on what is the same, what is not compared and what was
    compared for ASCII alone in EUC, in the report, the summary and the
    characters file's notes; and against every group parse_characters
    reads, in the order the file numbers them (characters, bytes, EUC by
    their locales with the byte sequences before the characters, UTF-8),
    each row of each from the data or from its `!!` (WARNED), and every
    row of a locale not checked (S, FILES None where not written)."""
    import locale_order as lo
    want = []
    if c['state'] == 'compared':
        lines = c['lines']

        def count(*kinds):
            return sum(1 for v in lines.values() if v in kinds)
        for what, key, kinds in (('character', 'character_groups', ('utf8',)),
                                 ('byte', 'byte_groups',
                                  ('utf8', 'other encoding', 'euc'))):
            same = count(*kinds) - sum(len(g['names']) for g in c[key])
            if same:
                want.append({'same': what, 'count': same,
                             'other': bool(c[key]),
                             'utf8': what == 'character'})
        builtin = [n for n in ('C', 'POSIX') if lines.get(n) == 'built in']
        if builtin:
            want.append({'builtin': builtin})
        if count('other encoding', 'euc'):
            want.append({'elsewhere': count('other encoding', 'euc'),
                         'with euc': bool(count('euc'))})
        # The lines on EUC compared for ASCII alone (no `!!`).
        want += [{'euc ascii': x['side'], 'same': False,
                  'names': {n: g['encoding'] for g in x['groups']
                            for n in g['names']}}
                 for x in c['euc']['ascii_alone']]
    want = sorted(json.dumps(x, sort_keys=True) for x in want)
    for text, found in (('report', r['checks']),
                        ('summary', s['checks'] if s is not None else None),
                        ('characters file',
                         files.checks if files is not None else None)):
        if found is None:
            continue
        got = sorted(json.dumps(d, sort_keys=True) for k, d in found
                     if k == 'ctype' and (set(d) in CTYPE_SAID_KEYS or (
                         'euc ascii' in d and not d['same'])))
        if got != want:
            raise AssertionError(f'{where}: the {text} prints {got!r}, '
                                 f'{path} holds {want!r}')
    if files is None:
        return
    bangs = {}
    if warned['state'] == 'reported':
        for item in warned['items']:
            bangs.setdefault(item['kind'], []).append(item['data'])

    def changed_rows(g, unit):
        said = []
        each = [dict({k: v for k, v in e.items() if k != unit + 's'},
                     **{unit: u}) for e in g['changes']
                for u in e[unit + 's']]
        for x in sorted(each, key=lambda x: int(
                x[unit][2:] if unit == 'char' else x[unit], 16)):
            change = {k: v for k, v in x.items()
                      if k not in (unit, 'classified')}
            own = x[unit]
            said.append((own if unit == 'char' else f'byte {own}', change,
                         ('classified' if x['classified'] else
                          'not classified') if unit == 'char' else '-'))
        return said

    def sequence_rows(d, measured):
        return [(f"bytes {x['bytes']}", f"{x['old']} -> {x['new']}",
                 measured) for x in d['sequences']]
    # Every group the file names, in the order it numbers them: of
    # characters, of bytes, of EUC (by their locales, the byte sequences
    # converted before the characters), of UTF-8.
    groups = []
    if c['state'] == 'compared':
        for what, key, unit in (('character', 'character_groups', 'char'),
                                ('byte', 'byte_groups', 'byte')):
            groups += [(tuple(g['names']), what, changed_rows(g, unit))
                       for g in c[key]]
        euc = [(sorted(n for x in d['groups'] for n in x['names']), 0,
                'byte sequence', sequence_rows(d, 'on both machines'))
               for d in bangs.get('euc_sequences_changed', [])]
        euc += [(g['names'], 1, 'character', changed_rows(g, 'char'))
                for g in c['euc']['character_groups']]
        groups += [(tuple(n), what, rows)
                   for n, _, what, rows in sorted(euc, key=lambda x: x[:2])]
    for kind in ('utf8_changed', 'utf8_one_side'):
        groups += [(tuple(c['utf8']['names']), 'byte sequence',
                    sequence_rows(d, 'on both machines' if 'side' not in d
                                  else f"on the {d['side']} machine"))
                   for d in bangs.get(kind, [])]
    for i, (names, what, said) in enumerate(groups, 1):
        label = f'LC_CTYPE_group_{i}'
        if files.ctype_groups.get(label) != (names, what):
            raise AssertionError(
                f'{where}: the characters file names {label} '
                f'{files.ctype_groups.get(label)!r}, {path} holds '
                f'{(names, what)!r}')
        if what == 'byte sequence':
            got = [(row['chars'], row['how'], row['measured'])
                   for row in files.get((label,), [])
                   if row['finding'] == 'LC_CTYPE converted']
        else:
            got = [(row['chars'],
                    _ctype_how(row['how'], row['chars'] if what == 'character'
                               else row['chars'][5:]),
                    row['before'])
                   for row in files.get((label,), [])
                   if row['finding'] == 'LC_CTYPE changed']
        if got != said:
            bad = next((a, b) for a, b in zip(got + [None] * len(said),
                                              said + [None] * len(got))
                       if a != b)
            raise AssertionError(f'{where}: the characters file prints '
                                 f'under {label} {bad[0]!r}, {path} '
                                 f'holds {bad[1]!r}')
    # No group more: where step 11 did not report its warnings, the
    # groups their `!!` give are not known here.
    if warned['state'] == 'reported' and set(files.ctype_groups) != {
            f'LC_CTYPE_group_{i}' for i in range(1, len(groups) + 1)}:
        raise AssertionError(f'{where}: the characters file names the groups '
                             f'{sorted(files.ctype_groups)!r}, {path} and '
                             f'the `!!` hold {len(groups)}')
    # Every locale with a row not checked: those on that line, whose rows
    # the `!!` gives, and those the data on EUC give, with their rows.
    rowed = {k[0] for k, rows in files.items()
             if len(k) == 1 and any(row['finding'] == 'LC_CTYPE not checked'
                                    for row in rows)}
    names = sorted(n for n, v in c.get('lines', {}).items()
                   if v == 'not checked')
    held = {}
    for x in c.get('euc', {}).get('not_checked', []):
        held.setdefault(x['name'], []).append(
            f"{CTYPE_WHERE[x['side']]}: {x['why']}".translate(
                lo.TEXT_ESCAPES))
    if set(names) & set(held) or sorted(rowed) != sorted(set(names)
                                                         | set(held)):
        raise AssertionError(f'{where}: the characters file has rows not '
                             f'checked of {sorted(rowed)!r}, {path} holds '
                             f'{names!r} and {sorted(held)!r}')
    got = {n: [row['why'] for row in files.get((n,), [])
               if row['finding'] == 'LC_CTYPE not checked'] for n in held}
    if got != held:
        raise AssertionError(f'{where}: the characters file prints the rows '
                             f'not checked {got!r}, {path} holds {held!r}')
    if warned['state'] != 'reported':
        return
    told = {}
    for item in warned['items']:
        if item['kind'] == 'ctype_not_checked':
            for x in item['data']['locales']:
                told.setdefault(x['name'], []).append(
                    f'{CTYPE_WHERE[x["side"]]}: {x["why"]}'.translate(
                        lo.TEXT_ESCAPES))
    got = {n: [row['why'] for row in files.get((n,), [])
               if row['finding'] == 'LC_CTYPE not checked'] for n in names}
    if got != {n: told.get(n) for n in names}:
        raise AssertionError(f'{where}: the characters file prints the rows '
                             f'not checked {got!r}, the `!!` say {told!r}')


def _codeset_label(cs):
    # heading()'s label of a locale's two encodings, 'unknown' as data
    # writes a codeset it did not read.
    a, b = (None if x == 'unknown' else x for x in (cs['old'], cs['new']))
    if a == b:
        return a or 'encoding unknown'
    return f'{a or "encoding unknown"} before, {b or "unknown"} now'


def _also_of(names, locales):
    """What parse_also reads of the 'also, in PostgreSQL' line of the
    locales NAMES, from their entries of step 11's data (None where the
    line has nothing to say)."""
    aliases, spellings, who = {}, [], {'anywhere': [], 'superuser': []}
    spelt = []
    for x in sorted(names):
        cs = locales[x]['codeset']['old']
        p = locales[x]['postgres']
        if p['alias'] != 'none':
            aliases.setdefault(cs, []).append(p['alias'])
        sp = p['spelling']
        if p['reach'] == 'superuser':
            if sp != 'none' and sp not in names:
                spelt.append(sp)
        elif sp != 'none' and sp not in names and sp not in spellings:
            spellings.append(sp)
        if p['reach'] != 'imported':
            who[p['reach']].append(x)
    out = {'aliases': aliases, 'spellings': spellings}
    for reach, group in who.items():
        if not group:
            continue
        if len(group) < len(names):
            w = ', '.join(group)
        else:
            w = 'this name' if len(group) == 1 else 'these names'
        out[reach] = [w] if reach == 'anywhere' else [(w, spelt)]
    return out if (aliases or spellings or len(out) > 2) else None


# How the outputs say each relation step 11's data write
# (locale_order.relation_state), and each side a chain is measured on: the
# tests' own words, so that a state printed with another's words is seen.
RELATION_SAID = {
    'not at all': 'not at all (the bytes decide)', 'told apart': 'told apart',
    'less than letters': 'by less than a letter',
    'less than accents': 'by less than an accent',
    'less than case': 'by less than case',
    'letters': 'as different letters', 'accents': 'like an accent',
    'case': 'by case',
    'letters or accents': 'as different letters or like an accent',
    'accents or case': 'like an accent or by case',
    'letters or accents or case':
        'as different letters or like an accent or by case'}
SIDE_SAID = {'both': 'on both machines',
             'old': 'on the old machine; the new one by the characters '
                    'between',
             'new': 'on the new machine; the old one by the characters '
                    'between'}


def _probes_said(levels, text, escapes=None):
    """Whether TEXT is how the outputs print one machine's probe pairs
    LEVELS (locale_order.probe_data): each level in its place, a pair's
    strings each as the character itself or its code point, or its bytes;
    the whole line, both ends anchored. ESCAPES, the characters file's."""
    def one(t):
        if t.startswith('U+'):
            ch = chr(int(t[2:], 16))
            if escapes:
                ch = ch.translate(escapes)
            return f'(?:{re.escape(ch)}|{re.escape(t)})'
        return re.escape(t)
    parts = []
    for word, x in zip(('letters', 'accent', 'case'), levels):
        if x['state'] == 'pair':
            parts.append(re.escape(word + ' ') + one(x['pair'][0]) + '/'
                         + one(x['pair'][1])
                         + (re.escape(' from the end') if x['from_the_end']
                            else ''))
        else:
            parts.append(re.escape(f'{word} {x["state"]}'))
    return re.fullmatch(', '.join(parts), text) is not None


def step11_levels_held(f, details, brief, rows, where):
    """Step 11's probes changed, left out and told apart of one change
    (F, {kind: data}) against what test_locale_order's readers take from
    its text: DETAILS, parse_details of its lines in the report (or the
    change parse_report gives), BRIEF, parse_brief of its phrase in the
    summary, ROWS, its rows of parse_characters; BRIEF and ROWS None where
    that output was not written. The probe pairs of each machine, each
    line whole; the levels compared and those no pair measured; the pairs
    told apart differently, counted, and each named with how it was told
    apart before and now where the report lists them; and in the
    characters file every chain of neighbours whole and in order, with
    its test pair rebuilt from its level and the old machine's probe
    pairs."""
    import test_locale_order as t
    import locale_order as lo
    pc, left, ta = (f.get(k) for k in ('probes changed', 'left out',
                                       'told apart'))
    want = {'compared': None, 'not_measured': None, 'pairs': None,
            'pairs_count': None}
    if left:
        want['compared'] = left['compared']
        if not pc:
            want['not_measured'] = left['left_out']
    if ta:
        # What told apart compares is printed only where a level is left
        # out; else it is every level the old machine has a pair for.
        compared = (left['compared'] if left else
                    [e['level'] for e in ta['probes'] if e['state'] == 'pair'])
        if ta['compared'] != compared:
            raise AssertionError(f'{where}: told apart compares '
                                 f'{ta["compared"]!r}, the text {compared!r}')
        if pc and ta['probes'] != pc['before']:
            raise AssertionError(f'{where}: told apart takes other probe '
                                 'pairs than the old machine printed')
        want['pairs_count'] = ta['count']
        if ta['listed'] == 'each':
            want['pairs'] = [(t.element(p['strings'][0]),
                              t.element(p['strings'][1]),
                              RELATION_SAID[p['before']],
                              RELATION_SAID[p['now']])
                             for p in ta['pairs']]
    got = {k: details.get(k) for k in want}
    if got != want:
        raise AssertionError(f'{where}: the report prints the levels and '
                             f'pairs as {got!r}, the data hold {want!r}')
    said = details.get('probes')
    if (said is not None) != bool(pc) or pc and not all(
            _probes_said(pc[k], x) for k, x in zip(('before', 'now'), said)):
        raise AssertionError(f'{where}: the report prints the probe pairs '
                             f'as {said!r}, the data hold {pc!r}')
    if brief is not None:
        want_b = {'probes': bool(pc), 'compared': want['compared'],
                  'pairs_count': want['pairs_count'],
                  'pairs': ([p[:2] for p in want['pairs']]
                            if want['pairs'] is not None else None)}
        got = {k: brief.get(k) for k in want_b}
        if got != want_b:
            raise AssertionError(f'{where}: the summary prints the levels and '
                                 f'pairs as {got!r}, the data hold {want_b!r}')
    if rows is None:
        return
    said = [r['probes'] for r in rows if r['finding'] == 'pairs changed']
    if len(said) != bool(pc) or pc and not all(
            _probes_said(pc[k], x, lo.TEXT_ESCAPES)
            for k, x in zip(('before', 'now'), said[0])):
        raise AssertionError(f'{where}: the characters file prints the '
                             f'probe pairs as {said!r}, the data hold {pc!r}')
    said = [r['levels'] for r in rows if r['finding'] == 'not compared']
    if said != ([left['left_out']] if left else []):
        raise AssertionError(f'{where}: the characters file leaves out '
                             f'{said!r}, the data {left!r}')

    def chain_row(c):
        s = [e for a, b in c['runs']
             for e in t.run_elements(t.element(a), t.element(b))]
        x = c['test']
        if x['state'] == 'none':
            test = before = now = None
        else:
            probe = ta['probes'][lo.LEVEL_NAMES.index(x['level'])]
            if probe['state'] != 'pair':
                raise AssertionError(f'{where}: a chain is tested at '
                                     f'{x["level"]}, which has no pair')
            low, high = (t.element(v) for v in probe['pair'])
            test = (([high, s[0]], [low, s[1]]) if probe['from_the_end']
                    else ([s[0], high], [s[1], low]))
            before, now = x['before'], x['now']
        return {'chars': s, 'how': (RELATION_SAID[c['before']],
                                    RELATION_SAID[c['now']]),
                'test': test, 'before': before, 'now': now,
                'measured': SIDE_SAID[c['measured']]}
    said = [{k: r[k] for k in ('chars', 'how', 'test', 'before', 'now',
                               'measured')}
            for r in rows if r['finding'] == 'told apart differently']
    if said != ([chain_row(c) for c in ta['chains']] if ta else []):
        raise AssertionError(f'{where}: the characters file prints the '
                             'chains told apart differently otherwise than '
                             'the data')


# The rows of the characters file that step 11's findings of a change
# write (locale_order.LOCALE_KINDS' rows), not those of its sort table.
STEP11_ROWS = ('moved', 'sorts differently', 'pairs changed',
               'not compared', 'told apart differently', 'not decided')


def _table_said(t):
    """((why, said), named) as test_locale_order.table_phrase reads the
    line of a group's sort table T, items left out; None where it prints
    no line."""
    import locale_order as lo
    if t['state'] == 'no line':
        return None
    why = lo.NO_LEVELS_ONE if t['reason'] == 'no levels on one machine' \
        else None
    said = lo.TABLE_SAID[t['step11']]
    nm = t.get('naming')
    if nm is None:
        return (why, said), None
    if nm['state'] == 'named':
        return (why, said), {'counts': nm['counts'], 'more': nm['more'],
                             'unnamed': None}
    unnamed = nm['state']
    if nm['state'] == 'unread':
        unnamed = '; '.join(nm['reasons'])
    elif nm['state'] == 'levels':
        unnamed = (nm['old'], nm['new'])
    return (why, said), {'counts': None, 'more': 0, 'unnamed': unnamed}


def step11_table_held(r, s, findings, path, where, files=None):
    """Each group's sort_table, as step 11 wrote it to PATH
    (change_groups), against the line the readers take from the report
    (parse_details), the summary (parse_brief) and the characters file's
    row of it (parse_characters): no line where the data says none, else
    why, what step 11 said, and what the table names, its counts and how
    many more than the report lists, or why it names nothing; and the
    report's names among them, as many as the file's rows and in their
    order."""
    import test_locale_order as t
    import locale_order as lo
    by = t.by_name(r)
    for g in findings['groups']:
        names = sorted(g['locales'])
        want = _table_said(g['sort_table'])
        ch = by[names[0]]
        got = None if ch.get('table') is None else (
            ch['table'], ch.get('named') and {
                k: v for k, v in ch['named'].items() if k != 'items'})
        if got != want:
            raise AssertionError(f'{where}: the report prints the sort table '
                                 f'of {names} as {got!r}, {path} holds '
                                 f'{want!r}')
        if s is not None:
            b = t.parse_brief(s['changes'][names[0]]['brief'])
            sgot = None if b.get('table') is None else (
                b['table'], b.get('named') and {
                    k: v for k, v in b['named'].items() if k != 'items'})
            if sgot != want:
                raise AssertionError(f'{where}: the summary prints the sort '
                                     f'table of {names} as {sgot!r}, {path} '
                                     f'holds {want!r}')
            if want and want[1] and b['named']['items'] != \
                    ch['named']['items']:
                raise AssertionError(f'{where}: the summary names otherwise '
                                     f'than the report in the table of '
                                     f'{names}')
        if files is None:
            continue
        rows = [x for x in files.get(tuple(names), [])
                if x['finding'] == 'sort table changed']
        heads = [x for x in rows if 'table' in x]
        fgot = None if not heads else (
            heads[0]['table'], heads[0]['named'] and {
                k: v for k, v in heads[0]['named'].items() if k != 'items'})
        # The file's line counts and names nothing: its rows hold the
        # names (held to the counts by report_data._table_rows_held).
        fwant = want and (want[0], want[1] and dict(want[1], more=0))
        if len(heads) > 1 or fgot != fwant:
            raise AssertionError(f'{where}: the characters file holds the '
                                 f'sort table of {names} as {fgot!r}, '
                                 f'{path} holds {want!r}')
        if want and want[1] and want[1]['counts'] is not None:
            items = ch['named']['items']
            listed = [x['chars'] for x in rows if 'kind' in x]
            # A character the report prints by its glyph, as the file
            # names it: by its code point (table_naming).
            plain = [re.sub(r'^.+ \((U\+[0-9A-F]{4,6})\)$', r'\1', x)
                     for x in items]
            if plain != listed[:len(plain)] or \
                    len(plain) != min(len(listed), lo.LIST_LIMIT):
                raise AssertionError(f'{where}: the report lists {plain[:3]} '
                                     f'... of the sort table of {names}, the '
                                     f'file {listed[:3]} ...')


def step11_groups_held(r, s, findings, path, where, files=None,
                       text=None):
    """step 11's findings, as it wrote them to PATH (change_groups),
    against what test_locale_order's readers take from the text: each
    group's locales under one heading of the report (parse_report) and
    of the summary (parse_summary), with the encodings and the PostgreSQL
    names the heading prints, the groups that share a heading exactly
    those that say so; and what moved, what sorts differently, what did
    not move and the pairs no measurement decides, counted and named as
    parse_details and parse_brief read them; and the note on names in
    bytes where a group names one; and, where the run wrote its
    characters file (TEXT, and FILES, parse_characters of it; else None),
    every run that moved or sorts differently and every pair no
    measurement decides, as its rows name them, what did not move, as the
    strings of its chains that no moved row names, and each group's
    names_bytes, as whether its rows of step 11 name a string by its
    bytes; and the probe pairs that changed, the levels left out and the
    pairs told apart differently (step11_levels_held), in the report and
    the characters file, and in the summary what its phrase says of them.
    What did not move is counted, not named, where the report lists too
    many; then only the characters file holds it. S is None where no
    summary was written."""
    import test_locale_order as t
    import locale_order as lo
    by = t.by_name(r)
    groups = findings['groups']
    # Whether each group's rows of step 11 name a string by its bytes,
    # the separator of a test pair aside (two U+0001, named alike on
    # every machine): the characters file names every string the report
    # and the summary name or count, and more.
    in_bytes = {}
    if text is not None:
        sep = 'bytes ' + lo.spaced(lo.SEPARATOR)
        for line in text.split('\n'):
            f_ = line.split('\t')
            if len(f_) == 8 and f_[1] in STEP11_ROWS:
                in_bytes[f_[0]] = in_bytes.get(f_[0], False) or any(
                    x != sep for x in re.findall(
                        r'\bbytes [0-9A-F]{2}(?: [0-9A-F]{2})*',
                        '\t'.join(f_[2:])))
    printed = {}
    for g in groups:
        names = sorted(g['locales'])
        heads = {id(by.get(x)) for x in names}
        if any(x not in by for x in names) or len(heads) != 1:
            raise AssertionError(f'{where}: {path} groups {names}, which the '
                                 'report does not print under one heading')
        printed.setdefault(g['printed_under'], set()).update(heads)
    if any(len(v) != 1 for v in printed.values()) or \
            len({next(iter(v)) for v in printed.values()}) != len(printed) \
            or len(printed) != len(r['changes']):
        raise AssertionError(f'{where}: {path} puts its groups under '
                             f'{len(printed)} heading(s), the report prints '
                             f'{len(r["changes"])}')
    for g in groups:
        names = sorted(g['locales'])
        ch = by[names[0]]
        k = g['printed_under']
        if k >= len(r['changes']) or r['changes'][k] is not ch:
            raise AssertionError(f'{where}: {path} puts {names} under '
                                 f'heading {k}, the report under another')
        union = sorted(x for h in groups
                       if h['printed_under'] == g['printed_under']
                       for x in h['locales'])
        locales = {x: e for h in groups for x, e in h['locales'].items()
                   if x in union}
        want = {x: _codeset_label(g['locales'][x]['codeset']) for x in names}
        got = {x: ch['names'][x] for x in names}
        if got != want:
            raise AssertionError(f'{where}: the heading of {names} prints '
                                 f'{got!r}, {path} holds {want!r}')
        also = _also_of(union, locales)
        if ch['also'] != also:
            raise AssertionError(f'{where}: the PostgreSQL names of {union} '
                                 f'print {ch["also"]!r}, {path} holds '
                                 f'{also!r}')
        if s is not None:
            sa = s['changes'][names[0]]['also']
            if sa != also:
                raise AssertionError(f'{where}: the summary prints the '
                                     f'PostgreSQL names of {union} as '
                                     f'{sa!r}, {path} holds {also!r}')
        if g['found_by'] == 'its sort table alone':
            if ch['moved'] is not None or ch['moved_count'] is not None:
                raise AssertionError(f'{where}: the report prints what moved '
                                     f'in {names}, which {path} says its '
                                     'sort table alone found')
            continue
        f = {x['kind']: x for x in g['findings']}
        m = f['moved']

        def runs(x):
            return [(t.element(a), t.element(b)) for a, b in x['runs']]

        def strings(x):
            return [e for a, b in runs(x) for e in t.run_elements(a, b)]

        want = {'moved_count': m['count'], 'moved': None, 'moved_runs': None,
                'moved_others': None, 'reversed': None,
                'reversed_others': None, 'all_reversed': False,
                'still_count': None, 'still_others': None, 'still': None,
                'undecided': 0}
        if m['listed'] == 'none':
            want['moved'] = []
        elif m['listed'] == 'each':
            want['moved'] = strings(m)
        else:
            want['moved_others'] = m['other_strings']
            if m['listed'] == 'runs':
                want['moved_runs'] = runs(m)
        if 'reversed' in f:
            want.update(reversed=f['reversed']['count'],
                        reversed_others=f['reversed']['other_strings'],
                        all_reversed=f['reversed']['all'])
        if 'did not move' in f:
            d = f['did not move']
            want.update(still_count=d['count'],
                        still_others=d['other_strings'])
            if d['listed'] == 'runs':
                want['still'] = runs(d)
        if 'undecided' in f:
            want['undecided'] = f['undecided']['count']
        got = {k: ch.get(k) for k in want}
        if m['listed'] == 'each':
            got['moved_others'] = None
            if sum(isinstance(e, bytes) for e in ch['moved']) != \
                    m['other_strings']:
                raise AssertionError(f'{where}: the report names other '
                                     f'strings that moved in {names} '
                                     f'otherwise than {path}')
        if got != want:
            raise AssertionError(f'{where}: the report prints what changed in '
                                 f'{names} as {got!r}, {path} holds {want!r}')
        if files is not None:
            rows = files.get(tuple(names), [])

            def pair_key(p):
                return (t.run_key(p[0]), t.run_key(p[1]))
            in_data = {'moved': runs(m),
                       'sorts differently': (runs(f['reversed'])
                                             if 'reversed' in f else []),
                       'not decided': sorted(
                           ([t.element(a), t.element(b)]
                            for a, b in (f['undecided']['pairs']
                                         if 'undecided' in f else [])),
                           key=pair_key)}
            in_rows = {'moved': [x for r_ in rows if r_['finding'] == 'moved'
                                 for x in r_['runs']],
                       'sorts differently': [
                           x for r_ in rows
                           if r_['finding'] == 'sorts differently'
                           for x in r_['runs']],
                       'not decided': sorted(
                           (r_['chars'] for r_ in rows
                            if r_['finding'] == 'not decided'),
                           key=pair_key)}
            if in_rows != in_data:
                raise AssertionError(f'{where}: the characters file names '
                                     f'what changed in {names} otherwise '
                                     f'than {path}')
            # Asked only where something moved (locale_findings).
            moved = set(strings(m))
            still = sorted({e for r_ in rows
                            if r_['finding'] == 'told apart differently'
                            for e in r_['chars']} - moved,
                           key=t.run_key) if moved else []
            dn = f.get('did not move')
            if still != (strings(dn) if dn else []):
                raise AssertionError(f'{where}: the chains of the characters '
                                     f'file leave {len(still)} string(s) of '
                                     f'{names} that did not move, {path} '
                                     'says otherwise')
            if g['names_bytes'] != in_bytes.get(','.join(names), False):
                raise AssertionError(f'{where}: {path} says {names} name a '
                                     f'string by its bytes: '
                                     f'{g["names_bytes"]}, its rows say '
                                     'otherwise')
        if s is not None:
            b = t.parse_brief(s['changes'][names[0]]['brief'])
            want = {'moved_count': m['count'],
                    'moved': (strings(m) if m['listed'] in ('none', 'each')
                              else None),
                    'reversed': want['reversed'],
                    'still_count': want['still_count'],
                    'still_others': want['still_others'],
                    'still': want['still'], 'undecided': want['undecided']}
            got = {k: b.get(k) for k in want}
            if got != want:
                raise AssertionError(f'{where}: the summary prints what '
                                     f'changed in {names} as {got!r}, {path} '
                                     f'holds {want!r}')
        step11_levels_held(
            f, ch, t.parse_brief(s['changes'][names[0]]['brief'])
            if s is not None else None,
            files.get(tuple(names), []) if files is not None else None,
            f'{where}: {names} in {path}')
    if any(g['names_bytes'] for g in groups):
        for text, said in (('report', r.get('others_note')),
                           ('summary', s and s.get('others_note'))):
            if text == 'summary' and s is None:
                continue
            if not (said or '').startswith(t.OTHERS_NOTE):
                raise AssertionError(f'{where}: {path} names a string by its '
                                     f'bytes and the {text} does not say how '
                                     'such a string is named')


def _summary_of(argv, out, where):
    """The summary a comparison wrote to the file its argv's --summary-to
    names; None where it was given none, or printed no report (refused).
    A summary asked for and not written beside a printed report raises:
    compare writes it before the report."""
    argv = [str(x) for x in argv]
    if '--summary-to' not in argv:
        return None
    if not any(ln.startswith('Old machine: ') for ln in out.split('\n')):
        return None
    path = argv[argv.index('--summary-to') + 1]
    if not os.path.exists(path):
        raise AssertionError(f'{where} printed a report and wrote no '
                             f'summary to {path}')
    with open(path, encoding='utf-8') as f:
        return f.read()


def _sql_to_of(argv):
    """The --sql-to of a comparison's argv, or None where it has none."""
    argv = [str(x) for x in argv]
    return (argv[argv.index('--sql-to') + 1] if '--sql-to' in argv
            else None)


def _characters_of(argv, out):
    """The characters file a comparison wrote to its argv's
    --characters-to; None where it was given none, or printed no report
    (refused). compare writes it before the report, so one asked for and
    not there beside a printed report raises in open()."""
    argv = [str(x) for x in argv]
    if '--characters-to' not in argv:
        return None
    if not any(ln.startswith('Old machine: ') for ln in out.split('\n')):
        return None
    with open(argv[argv.index('--characters-to') + 1],
              encoding='utf-8') as f:
        return f.read()


def held_main(main, stdout):
    """main() of locale_order.py, run in process with its stdout sent to
    the StringIO `stdout`, under glibc_locale_data.WARNINGS_ENV and
    locale_order.STEP11_DATA_ENV: what it printed and what it wrote of its
    `!!` agree (step11_written), and so do its counts, findings, checks
    and SQL files (step11_counts_held, its summary and characters file read
    from the --summary-to and --characters-to of sys.argv, its SQL files
    from its --sql-to), on a refusal too; the
    sys.argv of a --compare given a --verdicts-to in its temporary folder
    where it has none (_with_verdicts), whose verdicts step11_counts_held
    holds the data to.
    Returns what main() returns."""
    import tempfile
    from unittest import mock
    import glibc_locale_data as g
    import locale_order
    start = len(stdout.getvalue())
    with tempfile.TemporaryDirectory() as tmp:
        argv, vpath = _with_verdicts(sys.argv, tmp)
        path = os.path.join(tmp, 'w.jsonl')
        data = os.path.join(tmp, 'data.json')
        with mock.patch.dict(os.environ, {g.WARNINGS_ENV: path,
                                          locale_order.STEP11_DATA_ENV: data}), \
                mock.patch.object(sys, 'argv', argv):
            try:
                rc = main()
            finally:
                out = stdout.getvalue()[start:]
                recs = step11_written(out, path, 'main()')
                step11_counts_held(out, _summary_of(argv, out, 'main()'),
                                   data, 'main()',
                                   characters=_characters_of(argv, out),
                                   warnings={'state': 'reported',
                                             'items': recs},
                                   verdicts=_verdicts_of(vpath),
                                   sql_to=_sql_to_of(argv))
    return rc


def held_compare(argv, *, counts_unread=None, **kw):
    """subprocess.run(argv, capture_output=True, **kw) of a
    `locale_order.py --compare`, under glibc_locale_data.WARNINGS_ENV and
    locale_order.STEP11_DATA_ENV: what it printed and what it wrote of its
    `!!` agree (step11_written), and so do its counts, findings, checks
    and SQL files (step11_counts_held, with the summary of its --summary-to,
    the characters file of its --characters-to and the SQL files of its
    --sql-to), on a refusal too. ARGV is run
    with a --verdicts-to in its temporary folder where it has none
    (_with_verdicts), whose verdicts step11_counts_held holds the data to.
    COUNTS_UNREAD is why the readers cannot read its report
    (step11_counts_held's unread). Returns the CompletedProcess, whose args
    are those run."""
    import tempfile
    import glibc_locale_data as g
    import locale_order
    env = dict(kw.pop('env', None) or os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        argv, vpath = _with_verdicts(argv, tmp)
        path = os.path.join(tmp, 'w.jsonl')
        data = os.path.join(tmp, 'data.json')
        env[g.WARNINGS_ENV] = path
        env[locale_order.STEP11_DATA_ENV] = data
        p = subprocess.run(argv, capture_output=True, env=env, **kw)
        out = p.stdout
        if isinstance(out, bytes):
            out = out.decode('utf-8')
        where = ' '.join(map(str, argv))
        recs = step11_written(out, path, where)
        step11_counts_held(out, _summary_of(argv, out, where), data, where,
                           unread=counts_unread,
                           characters=_characters_of(argv, out),
                           warnings={'state': 'reported', 'items': recs},
                           verdicts=_verdicts_of(vpath),
                           sql_to=_sql_to_of(argv))
    return p


def bang_blocks(log):
    """The `!!` blocks of a step's log as audit.sh's warnings block reads
    them: a line opening with `!!` and the lines after it that open with
    three blanks. A log that is not there holds none (step 0's is written
    only when it printed something)."""
    if not os.path.exists(log):
        return []
    with open(log, encoding='utf-8') as f:
        return text_bang_blocks(f.read())


def _without_bangs(lines):
    """LINES without the `!!` blocks (text_bang_blocks' rule): those are
    held to the run's warnings by check_warnings."""
    out, inside = [], False
    for line in lines:
        if line.startswith('!!'):
            inside = True
        elif inside and line.startswith('   '):
            pass
        else:
            inside = False
            out.append(line)
    return out


def compared_held(out, r, path):
    """What the run printed before 'What was compared' and under it,
    without its `!!` (check_warnings holds those), against the report's
    compared and run.machines, line by line, both ends: step 0's note;
    the machines' files as given; the tags, the machines and the line of
    the glibc versions, rebuilt from the data by the functions that print
    them; latest-elN's lines and the rebuilt builds' the same way. Set
    aside: what the run says it does (cloning, fetching tags)."""
    import glibc_locale_data as g
    c = r['compared']

    def no(what, got, want):
        raise AssertionError(f'{path}: printed {what} {got!r}, the data '
                             f'{want!r}')
    if c['state'] != 'reported':
        no('a block', None, c)
    lines, at = _compared_at(out)
    before = lines[:at]
    if before[-2:] != ['', '=' * 64]:
        no('above the banner', before[-2:], ['', '=' * 64])
    before = _without_bangs(before[:-2])
    note = c['note']
    if note['state'] == 'not reported':
        no("step 0's note", None, note)
    latest = c['latest']
    if latest['state'] == 'not reported':
        no("latest-elN's line", None, latest)
    if note['state'] == 'not run':
        # Nothing, or latest-elN's lines.
        want = [x for e in (latest.get('resolved') or ())
                for x in g.latest_lines(e)]
        if before != want:
            no('before the block', before, want)
    elif latest['state'] != 'not asked':
        no("latest-elN's line beside the machines' files", None, latest)
    elif before != g.machine_note_lines(note):
        no("step 0's note", before, note)
    body = compared_text(out).split('\n')
    m = r['run']['machines']
    if m['state'] == 'given':
        given = ["Machines' files, as given:", '  old  ' + m['old'],
                 '  new  ' + m['new']]
        if body[:3] != given:
            no("the machines' files", body[:3], given)
        body = body[3:]
    body = [x for x in _without_bangs(body)
            if not x.startswith(('Cloning glibc (full history',
                                 'Refs not present locally: '))]
    want = g.tag_provenance_lines(c['tags'])
    if c['machines']:
        want += g.machine_provenance_lines(c['machines'])
    if c['builds']['state'] != 'none':
        want += g.build_provenance_lines(c['builds']['entries'])
    want += g.collation_version_lines(c['collation_version'])
    if body != want:
        no('the block', body, want)


def text_bang_blocks(text):
    """bang_blocks of a step's output."""
    blocks, cur = [], None
    for line in text.split('\n'):
        if line.startswith('!!'):
            if cur is not None:
                blocks.append('\n'.join(cur))
            cur = [line]
        elif cur is not None and line.startswith('   '):
            cur.append(line)
        else:
            if cur is not None:
                blocks.append('\n'.join(cur))
            cur = None
    if cur is not None:
        blocks.append('\n'.join(cur))
    return blocks


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


def columns(text):
    """The terminal columns text takes: 2 for a wide or full-width
    character (East Asian Width W or F, such as 七), 0 for a combining
    mark, 1 for the rest. Written here and not imported, so that a fault
    in the scripts' own count cannot hide from the tests (16.337)."""
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in 'WF' else 1
               for c in text)


def wide_lines(text):
    """The lines of text wider than 78 terminal columns that a wrap could
    have broken (16.186, 16.200, 16.337). A line holding a word of 40
    characters or more (a path, a commit id, a sha256) is left out."""
    return [line for line in text.split('\n')
            if columns(line) > 78 and not any(len(w) >= 40 for w in line.split())]


# A step deleted in branch 9 (3 in step 9.2; 4, 5, 9 and 10 in step 9.3;
# 6, 7 and 8 in step 9.4b; 1 in step 9.5, which became 'What was compared')
# named in text the tool prints or writes, in any of the spellings the
# deleted texts used: "step 4", "Steps 1 to 5", "steps 2 and 5", "steps
# 9/10", "Steps 1-5", "steps 6 to 10", "steps 6 and 7", "steps 1 and 2",
# "step 1". "step 11" is not step 1, and "step 9.3" is a step of the plan,
# not of the run: neither counts. Nor does the file name step1.<pair>.log,
# which 'What was compared' keeps on purpose (audit.sh). And the scripts of
# the deleted steps by name, and the names of the lists steps 1 and 6 to 8
# wrote, and step 2's list of what the character set check listed until
# step 12.4, so that a leftover one is never taken for this run's.
DELETED_STEP = re.compile(
    r'\b[Ss]teps?\s+(?:\d+\s*(?:,|and|to|-|/)\s*)*'
    r'(?:1|3|4|5|6|7|8|9|10)\b(?!\.\d)'
    r'|audit-locale-diff|changed_locales\.'
    r'|flag_algorithmic_ranges|diff_collation_code'
    r'|diff_distro_locales|diff_node_locales'
    r'|copy_missing\.|backports_inside_collate\.|distro_keyed_locales\.'
    r'|node_collate_diffs\.|node_collate_inherited\.'
    r'|node_removed_locales\.|node_removed_undetermined\.'
    r'|node_keyed_locales\.|step2_keyed_locales\.')


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
# How step 2 prints the locale built from glibc's file C, everywhere it
# names it (filter_lc_collate_changes.shown_name); its files keep "C".
STEP2_C = 'C.UTF-8'
STEP2_NOT_COMPARED = '!! Not compared, so listed in the summary ('
# Step 2's `!!` for the files only the new side has, when the old side is an
# upstream tag (16.123); with a build or a machine there it is a plain
# heading, without the `!! `.
STEP2_ADDED = 'Not compared, no file at '
SUMMARY_STEP2 = '-- Listed by step 2'
# The `!!` report_tag_provenance prints where a tag's signature was not
# checked (16.154), which the summary's warnings repeat.
SIGNATURES_UNCHECKED = '!! Signatures NOT checked for '
# What 'What was compared' says of PostgreSQL's collation version (16.166,
# glibc_locale_data.print_collation_version), written here and never
# imported from the script, so that a rewording reddens the tests instead of
# being followed by them. Two sides of one glibc version: a `!!`, which the
# summary's warnings repeat; two versions: a plain line, which they do not.
PG_VERSION_SAME = (
    "!! PostgreSQL warns about no libc collation between these two sides: "
    "both are glibc {v}, the version a PostgreSQL that runs it records for a "
    "collation and compares. Whatever this run finds changed between them, "
    "PostgreSQL does not report.")
PG_VERSION_DIFFER = (
    "glibc {old} on the old side, {new} on the new: the version a PostgreSQL "
    "that runs it records for a libc collation, where it records one. "
    "PostgreSQL warns about a collation, and PostgreSQL 15 or later about a "
    "database's default collation, where the version it recorded is not "
    "{new} ({old} if it was recorded on the old side), whether that locale's "
    "order changed or not. Where it recorded {new}, it does not warn, "
    "whatever changed: initdb and a restore on the new side record {new} for "
    "every collation, and pg_upgrade there for those of pg_catalog, for "
    "every collation from PostgreSQL 12 or older, and for a database's "
    "default from 14 or older.")
PG_VERSION_UNKNOWN = ("!! Whether PostgreSQL warns about a collation's "
                      "version is NOT known: no glibc version could be read "
                      "for ")
# Words only one of the three says, to count each across a whole run.
_PG_VERSION_MARKS = {
    'same': 'PostgreSQL warns about no libc collation',
    'differ': 'records for a libc collation, where it records one',
    'unknown': "Whether PostgreSQL warns about a collation's version",
}
# The sentence the `!! Not compared on any route` block ends with (13.9,
# the P13 line): no route looks for a user's or an extension's function.
NO_ROUTE_LOOKS = ("No route looks either for a function of a user or of an "
                  "extension that calls glibc's locale functions itself.")


def pg_version_of(name):
    """The glibc version a test expects for a side it names: X.Y of a
    release tag or a build (glibc-2.34, glibc-2.34-275.el9_8), and the
    release latest-elN resolves to in the suite's mirrors."""
    latest = {'latest-el8': '2.28', 'latest-el9': '2.34',
              'latest-el10': '2.39'}
    if name in latest:
        return latest[name]
    # A pinned tag's commit, given by its id, is that release (16.183).
    name = {sha: tag for tag, sha in EXPECTED_SHA.items()}.get(name, name)
    m = re.match(r'glibc-(\d+\.\d+)(?:$|-)', name)
    if not m:
        raise AssertionError(f'no version known for side {name!r}')
    return m.group(1)


def assert_collation_version(case, out, old, new, label=None):
    """16.166 on one whole run whose sides are glibc `old` and `new`:
    'What was compared' says once, in full, what PostgreSQL does about the
    pair's collation version, and no other of the three sentences is said
    anywhere; a `!!` (one version) is repeated once among the summary's
    warnings, the plain line (two versions) never."""
    kind = 'same' if old == new else 'differ'
    want = (PG_VERSION_SAME.format(v=new) if kind == 'same' else
            PG_VERSION_DIFFER.format(old=old, new=new))
    case.assertEqual(flat(compared_text(out)).count(want), 1,
                     (label, want, compared_text(out)[-3000:]))
    for k, mark in _PG_VERSION_MARKS.items():
        case.assertEqual(flat(out).count(mark),
                         (2 if k == 'same' else 1) if k == kind else 0,
                         (label, k))
    case.assertEqual(summary_warnings(out).count(want),
                     1 if kind == 'same' else 0, label)


def assert_no_route_looks(case, out, label=None):
    """13.9 on one whole run: the P13 sentence is said once under 'What was
    compared', inside the `!! Not compared on any route` block, and once
    among the summary's warnings, and nowhere else."""
    case.assertEqual(flat(compared_text(out)).count(NO_ROUTE_LOOKS), 1, label)
    case.assertEqual(summary_warnings(out).count(NO_ROUTE_LOOKS), 1, label)
    case.assertEqual(flat(out).count(NO_ROUTE_LOOKS), 2, label)


# What each run of step 2 the suite made wrote of its rules
# (step2_rules.<pair>.json), by the text it printed: step2_ran fills it
# right after the run, before another run can write the same folder, and
# Step2Report holds what it read of that text to it.
STEP2_DATA = {}
# What the step 2 text of each audit.sh run was cut from (check_report),
# with that run's record: a test that cuts the step's text out of the run
# its own way reads a part of one of these.
STEP2_RUNS = []
# Step2Report(text, data=NO_RECORD): text made up in a test, of no run.
NO_RECORD = object()


def step2_ran(text, out_dir, args):
    """Keep the record step 2 wrote to OUT_DIR for the run whose output is
    TEXT and whose arguments are ARGS (its two sides the first two that
    are not options or --repo's value); nothing when it wrote none (it
    stopped first). And hold what TEXT says of the character set check
    and of the transliterations to the record of each the same run wrote
    (charmaps_held, translit_held), which must be there where TEXT prints
    them."""
    import glibc_locale_data as g
    sides = [a for a in args if not a.startswith('-')]
    if len(sides) < 2 or not out_dir:
        return
    if '--repo' in args:
        repo = args[args.index('--repo') + 1]
        sides = [a for a in sides if a != repo]
    path = os.path.join(out_dir, 'step2_rules.%s.json'
                        % g.pair_slug(sides[0], sides[1]))
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            STEP2_DATA[text] = json.load(f)
    path = os.path.join(out_dir, 'step2_character_sets.%s.json'
                        % g.pair_slug(sides[0], sides[1]))
    cs = None
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            cs = json.load(f)
        charmaps_held(text, cs, 'step2_ran', sides[:2])
    elif _cs_block(text) is not None:
        raise AssertionError(f'step 2 printed its character set check and '
                             f'wrote no {path}')
    path = os.path.join(out_dir, 'step2_transliteration.%s.json'
                        % g.pair_slug(sides[0], sides[1]))
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            translit_held(text, json.load(f), 'step2_ran', sides[:2],
                          machines=bool(cs) and cs.get('basis') == 'measured')
    elif _tr_block(text) is not None:
        raise AssertionError(f'step 2 printed its transliterations and '
                             f'wrote no {path}')


CS_HEAD = re.compile(
    r"Character sets \((?P<label>[^)]*)\): (?:the (?P<n>\d+) that the "
    r"(?P<b>\d+) locale builds (?P<words>.+?) use are identical at "
    r"(?P<o>\S+) and (?P<w>\S+?)\.|(?P<k>\d+) of the (?P<n2>\d+) that "
    r"the (?P<b2>\d+) locale builds (?P<words2>.+?) use "
    r"(?P<verb>differs|differ) between (?P<o2>\S+) and (?P<w2>\S+?)\.)"
    r"(?: |$)")
CS_SIZE = re.compile(
    r"(?P<name>\S+(?: -> \S+)?): (?:(?P<none>no character changed)|"
    r"(?P<bad>could not be compared \(see below\))|(?P<parts>\d+ "
    r"characters? (?:added|removed|given other bytes)(?:, \d+ "
    r"(?:added|removed|given other bytes))*))\.(?: |$)")
CS_NONE = re.compile(r"No (?P<c>compared )?build's LC_COLLATE names one of "
                     r"those characters\.(?: |$)")
CS_LISTED = ("Locale builds whose LC_COLLATE names one of those characters "
             "({n}), listed in the summary:")
CS_ANYWAY = ("Locale builds the summary lists anyway, whose LC_COLLATE "
             "names one of those characters or could not be compared ({n}):")
CS_PHRASE = re.compile(r"(?P<n>\d+) characters?, named in (?P<via>.+)")


def _cs_block(text):
    """The lines of the character set check's paragraph without `!!`, from
    its "Character sets (" line to a blank line, a `!!` or the
    transliteration's line; None where there is none."""
    lines = text.split('\n')
    at = [i for i, x in enumerate(lines) if x.startswith('Character sets (')]
    if not at:
        return None
    if len(at) > 1:
        raise AssertionError('step 2 printed "Character sets (" twice')
    out = []
    for x in lines[at[0]:]:
        if not x.strip() or x.startswith('!!') or \
                x.startswith('Transliteration (LC_CTYPE)') or \
                x.startswith('Locale builds whose collating elements') or \
                re.match(r'\d+ more locale build', x):
            break
        out.append(x)
    return out


def charmaps_read(text):
    """What step 2 printed of its character set check without `!!`, read
    strictly: None where it printed no "Character sets (" line, else
    {'n', 'builds', 'words', 'changed': [(name, state, added, removed,
    other)], 'none': None or 'compared ' or '', 'listed': [(entry,
    charmap, count, via, codepoints)], 'anyway': [(what, charmap, state,
    why or (count, via))]}. A line it cannot place raises."""
    block = _cs_block(text)
    if block is None:
        return None
    segs = []        # [prose, [rows]]
    for x in block:
        if x.startswith('      '):
            if not segs or not segs[-1][1]:
                raise AssertionError(f'characters with no build: {x!r}')
            segs[-1][1][-1].append(x.strip())
        elif x.startswith('  ') and not x.startswith('   '):
            if not segs:
                raise AssertionError(f'a build with no heading: {x!r}')
            segs[-1][1].append([x[2:]])
        elif x.startswith(' '):
            raise AssertionError(f'unexpected line: {x!r}')
        elif segs and not segs[-1][1]:
            # textwrap breaks a name at its hyphen (EUC-JP): the line then
            # ends in the hyphen, and goes on with no blank.
            segs[-1][0] += ('' if segs[-1][0].endswith('-') else ' ') + x
        else:
            segs.append([x, []])
    got = {'changed': [], 'none': None, 'listed': [], 'anyway': []}
    t = segs[0][0]
    m = CS_HEAD.match(t)
    if not m:
        raise AssertionError(f'unreadable "Character sets" line: {t!r}')
    if m['n'] is not None:
        got.update(n=int(m['n']), builds=int(m['b']), words=m['words'],
                   k=0, sides=(m['o'], m['w']))
    else:
        got.update(n=int(m['n2']), builds=int(m['b2']), words=m['words2'],
                   k=int(m['k']), sides=(m['o2'], m['w2']))
        if (m['verb'] == 'differs') != (got['k'] == 1):
            raise AssertionError(f'{m["verb"]} with {got["k"]}')
    t = t[m.end():]
    while True:
        m = CS_SIZE.match(t)
        if not m:
            break
        if m['none']:
            x = (m['name'], 'compared', 0, 0, 0)
        elif m['bad']:
            x = (m['name'], 'not compared', None, None, None)
        else:
            parts = m['parts'].split(', ')
            parts[0] = re.sub(r' characters? ', ' ', parts[0], count=1)
            v = {'added': 0, 'removed': 0, 'given other bytes': 0}
            for part in parts:
                n, what = part.split(' ', 1)
                v[what] = int(n)
            x = (m['name'], 'compared', v['added'], v['removed'],
                 v['given other bytes'])
        got['changed'].append(x)
        t = t[m.end():]
    if len(got['changed']) != got['k']:
        raise AssertionError(f'{got["k"]} differ and {len(got["changed"])} '
                             f'said: {segs[0][0]!r}')
    m = CS_NONE.match(t)
    if m:
        got['none'] = m['c'] or ''
        t = t[m.end():]
    heads = [t] + [x for x, _ in segs[1:]]
    rowss = [r for _, r in segs]
    for head, rows in zip(heads, rowss):
        if head == '' and not rows:
            continue
        for kind, form in (('listed', CS_LISTED), ('anyway', CS_ANYWAY)):
            n = len(rows)
            if head == form.format(n=n):
                break
        else:
            raise AssertionError(f'unreadable heading {head!r} over {rows!r}')
        if got[kind]:
            raise AssertionError(f'{kind} printed twice')
        for row in rows:
            if kind == 'listed':
                m = re.fullmatch(r'(\S+) \((\S+)\): (.+)', row[0])
                ph = CS_PHRASE.fullmatch(m[3]) if m else None
                if not ph or len(row) < 2:
                    raise AssertionError(f'unreadable build {row!r}')
                chars = ' '.join(row[1:])
                c = re.fullmatch(r'characters its character set changed '
                                 r'\((\d+)\): (.+)', chars)
                cps = ([int(h, 16) for h in
                        re.findall(r'U\+([0-9A-F]{4,6})', c[2])]
                       if c else None)
                if not c or len(cps) != int(c[1]) or \
                        len(cps) != int(ph['n']):
                    raise AssertionError(f'unreadable characters {row!r}')
                got['listed'].append((m[1], m[2], int(ph['n']), ph['via'],
                                      cps))
            else:
                if len(row) != 1:
                    raise AssertionError(f'unreadable build {row!r}')
                m = re.fullmatch(r'(.+?) \((\S+)\): (?:could not be '
                                 r'compared: (.+)|(.+))', row[0])
                if not m:
                    raise AssertionError(f'unreadable build {row!r}')
                if m[3] is not None:
                    got['anyway'].append((m[1], m[2], 'not compared', m[3]))
                else:
                    ph = CS_PHRASE.fullmatch(m[4])
                    if not ph:
                        raise AssertionError(f'unreadable build {row!r}')
                    got['anyway'].append((m[1], m[2], 'named',
                                          (int(ph['n']), ph['via'])))
    return got


def charmaps_held(text, rec, where, sides=None, word=None):
    """What step 2 printed of its character set check without `!!`
    (charmaps_read) against REC, the record the same run wrote
    (step2_character_sets) or the report's step2.character_sets: none of
    its lines where the check did not compare, else each line field by
    field; SIDES, the two sides' names, and WORD, what the builds are
    said at both of (tags, builds or sides), where the caller has them.
    Its `!!` are held to the run's warnings elsewhere
    (check_report), and report_data ties them to the record."""
    def no(what, a, b):
        raise AssertionError(f'{where}: step 2 printed {what} {a!r}, its '
                             f'record says {b!r}')
    got = charmaps_read(text)
    if rec['state'] not in ('compared', 'reported'):
        if got is not None:
            no('"Character sets ("', got, rec)
        return
    if got is None:
        no('no "Character sets (" line', None, rec)
    measured = rec['basis'] == 'measured'
    if (got['words'] == 'measured on both machines') != measured or \
            (not measured and not got['words'].startswith(
                'in SUPPORTED at both ')):
        no('the builds as', got['words'], rec['basis'])
    if word is not None and not measured and \
            got['words'] != f'in SUPPORTED at both {word}':
        no('the builds as', got['words'], word)
    if sides is not None and got['sides'] != tuple(sides):
        no('the sides', got['sides'], sides)
    if (got['n'], got['builds']) != (len(rec['charmaps']), rec['builds']):
        no('the counts', (got['n'], got['builds']),
           (len(rec['charmaps']), rec['builds']))
    changed = [(x['new'] if x['old'] == x['new']
                else f"{x['old']} -> {x['new']}", x['state'],
                x.get('added'), x.get('removed'), x.get('other'))
               for x in rec['changed']]
    if got['changed'] != changed:
        no('the charmaps that changed', got['changed'], changed)
    rows = any(rec[k] for k in ('listed', 'anyway', 'unsettled', 'distro'))
    none = (None if not changed or rows else
            'compared ' if rec['not_compared'] else '')
    if got['none'] != none:
        no('"No build\'s LC_COLLATE"', got['none'], none)
    listed = [(x['entry'], x['new'], len(x['codepoints']),
               ', '.join(x['via']) or '?', x['codepoints'])
              for x in rec['listed']]
    if got['listed'] != listed:
        no('the builds it lists', got['listed'], listed)
    anyway = [(x['entry'] if x['entry'] != '-' else
               f"{x['source']} (not in SUPPORTED in this charmap)",
               x['new'], x['state'],
               x['why'] if x['state'] == 'not compared' else
               (x['count'], ', '.join(x['via']) or '?'))
              for x in rec['anyway']]
    if got['anyway'] != anyway:
        no('the builds the summary lists anyway', got['anyway'], anyway)


TR_HEAD = 'Transliteration (LC_CTYPE): '
TR_LIST = re.compile(r"Locale builds whose collating elements are built "
                     r"differently \((?P<n>\d+)\), listed in the summary:")
TR_MORE = re.compile(r"(?P<n>\d+) more locale builds? with collating "
                     r"elements built differently, or not compared, "
                     r"(?P<verb>is|are) listed already\.")
TR_LINE = [
    ('no change', re.compile(
        r"no character set and no locale file's LC_CTYPE changed "
        r"(?P<between>between the \w+), so no collating element is built "
        r"differently\.")),
    ('elements', re.compile(
        r"(?P<e>\d+) collating elements? of (?P<b>\d+) locale builds? "
        r"(?P<dep>depends?) on a transliteration; (?P<n>none|\d+) "
        r"(?P<verb>is|are) built differently at (?P<at>\S+?)\.")),
    ('unmoved', re.compile(
        r"(?P<u>\d+) locale builds? may build a collating element out of a "
        r"transliteration, and nothing (?P<read>they read|it reads) changed "
        r"(?P<between>between the \w+)\.")),
    ('none', re.compile(r"no compared locale build builds a collating "
                        r"element out of a transliteration\.")),
    ('listed already', re.compile(
        r"the (?P<k>\d+) locale builds? that may build a collating element "
        r"out of a transliteration (?P<verb>is|are) listed already, so none "
        r"was compared\.")),
]


def _tr_block(text):
    """The lines of the transliterations' part without `!!`, from the
    first of its lines (its "Transliteration (LC_CTYPE):" line, else its
    list, else its "N more" line) to a blank line or a `!!`; None where
    there is none."""
    lines = text.split('\n')
    at = [i for i, x in enumerate(lines)
          if x.startswith(TR_HEAD)
          or x.startswith('Locale builds whose collating elements')
          or re.match(r'\d+ more locale builds? with collating', x)]
    if not at:
        return None
    out = []
    for x in lines[at[0]:]:
        if not x.strip() or x.startswith('!!'):
            break
        out.append(x)
    if len(out) < at[-1] - at[0] + 1:
        raise AssertionError(f'step 2 printed its transliterations in two '
                             f'places: {[lines[i] for i in at]!r}')
    return out


def translit_read(text):
    """What step 2 printed of its transliterations without `!!`, read
    strictly: None where it printed none of its lines, else {'line': one
    of node_charmaps.TRANSLIT_LINES or 'no change', its numbers and words,
    'listed': [(entry, charmap, count, via, codepoints)], 'more': N or
    None}. A line it cannot place raises."""
    block = _tr_block(text)
    if block is None:
        return None
    segs = []        # [prose, [rows]]
    for x in block:
        if x.startswith('      '):
            if not segs or not segs[-1][1]:
                raise AssertionError(f'characters with no build: {x!r}')
            segs[-1][1][-1].append(x.strip())
        elif x.startswith('  ') and not x.startswith('   '):
            if not segs:
                raise AssertionError(f'a build with no heading: {x!r}')
            segs[-1][1].append([x[2:]])
        elif x.startswith(' '):
            raise AssertionError(f'unexpected line: {x!r}')
        elif segs and not segs[-1][1]:
            # textwrap breaks a name at its hyphen: the line then ends in
            # the hyphen, and goes on with no blank.
            segs[-1][0] += ('' if segs[-1][0].endswith('-') else ' ') + x
        else:
            segs.append([x, []])
    got = {'line': 'no line', 'listed': [], 'more': None}
    t = segs[0][0]
    if t.startswith(TR_HEAD):
        t = t[len(TR_HEAD):]
        for name, form in TR_LINE:
            m = re.compile(form.pattern + r'(?= |$)').match(t)
            if m:
                break
        else:
            raise AssertionError(f'unreadable transliteration line: {t!r}')
        got['line'] = name
        d = m.groupdict()
        if name == 'elements':
            e, b = int(d['e']), int(d['b'])
            n = 0 if d['n'] == 'none' else int(d['n'])
            if d['n'] == '0' or (d['dep'] == 'depends') != (e == 1) or \
                    (d['verb'] == 'is') != (n < 2) or \
                    m[0].startswith(f"{e} collating elements ") == (e == 1):
                raise AssertionError(f'unreadable transliteration line: '
                                     f'{t!r}')
            got.update(elements=e, builds=b, changed=n, at=d['at'])
        elif name == 'unmoved':
            u = int(d['u'])
            if (d['read'] == 'it reads') != (u == 1):
                raise AssertionError(f'{d["read"]} with {u}: {t!r}')
            got.update(unmoved=u, between=d['between'])
        elif name == 'listed already':
            k = int(d['k'])
            if (d['verb'] == 'is') != (k == 1):
                raise AssertionError(f'{d["verb"]} with {k}: {t!r}')
            got.update(skipped=k)
        elif name == 'no change':
            got.update(between=d['between'])
        t = t[m.end():].lstrip(' ')
    heads = [t] + [x for x, _ in segs[1:]]
    rowss = [r for _, r in segs]
    for head, rows in zip(heads, rowss):
        m = TR_LIST.match(head)
        if m:
            if got['listed'] or int(m['n']) != len(rows) or not rows:
                raise AssertionError(f'unreadable list {head!r} over '
                                     f'{rows!r}')
            for row in rows:
                r = re.fullmatch(r'(\S+) \((\S+)\): (\d+) elements?, '
                                 r'defined in (.+)', row[0])
                chars = ' '.join(row[1:])
                c = re.fullmatch(r'transliterated characters \((\d+)\):'
                                 r'(?: (.+))?', chars)
                cps = ([int(h, 16) for h in
                        re.findall(r'U\+([0-9A-F]{4,})', c[2] or '')]
                       if c else None)
                if not r or not c or len(cps) != int(c[1]):
                    raise AssertionError(f'unreadable build {row!r}')
                got['listed'].append((r[1], r[2], int(r[3]), r[4], cps))
            head = head[m.end():].lstrip(' ')
        elif rows:
            raise AssertionError(f'unreadable heading {head!r} over '
                                 f'{rows!r}')
        if head:
            m = TR_MORE.fullmatch(head)
            if not m or got['more'] is not None or \
                    (m['verb'] == 'is') != (m['n'] == '1'):
                raise AssertionError(f'unreadable line {head!r}')
            got['more'] = int(m['n'])
    return got


def translit_held(text, rec, where, sides=None, word=None, machines=False):
    """What step 2 printed of its transliterations without `!!`
    (translit_read) against REC, the record the same run wrote
    (step2_transliteration) or the report's step2.transliteration: none
    of its lines where the check compared no build, or where all it has
    are builds it could not compare (their `!!`), else each line field
    by field; SIDES, the two sides' names, and WORD, what the sides are
    (tags, builds or sides; MACHINES, two machines), where the caller has
    them. Its `!!` is held to the run's warnings elsewhere (check_report),
    and report_data ties it to the record."""
    def no(what, a, b):
        raise AssertionError(f'{where}: step 2 printed {what} {a!r}, its '
                             f'record says {b!r}')
    got = translit_read(text)
    if rec['state'] not in ('compared', 'reported', 'no change'):
        if got is not None:
            no('its transliteration lines', got, rec)
        return
    more = (len(rec['skipped']) if rec.get('skipped') and rec.get('line')
            in ('elements', 'unmoved', 'no line') else None)
    if rec['state'] != 'no change' and rec['line'] == 'no line' and \
            more is None:
        # Only builds it could not compare: their `!!` is all it prints.
        if got is not None:
            no('its transliteration lines', got, rec)
        return
    if got is None:
        no('none of its transliteration lines', None, rec)
    between = ('between the machines' if machines else
               f'between the {word}' if word else None)
    if rec['state'] == 'no change':
        if got['line'] != 'no change' or got['listed'] or got['more'] \
                is not None:
            no('its transliterations', got, rec)
        if between and got['between'] != between:
            no('the sides as', got['between'], between)
        return
    if got['line'] != rec['line']:
        no('the line', got['line'], rec['line'])
    want = {'elements': ('elements', 'builds', 'changed'),
            'unmoved': ('unmoved',), 'listed already': ('skipped',)}
    for k in want.get(rec['line'], ()):
        v = len(rec[k]) if k == 'skipped' else rec[k]
        if got[k] != v:
            no(k, got[k], v)
    if sides is not None and rec['line'] == 'elements' and \
            got['at'] != sides[1]:
        no('the new side', got['at'], sides[1])
    if between and rec['line'] == 'unmoved' and got['between'] != between:
        no('the sides as', got['between'], between)
    listed = [(x['entry'], x['new'], x['count'], ', '.join(x['via']),
               x['codepoints']) for x in rec['listed']]
    if got['listed'] != listed:
        no('the builds it lists', got['listed'], listed)
    if got['more'] != more:
        no('"more locale builds ... listed already"', got['more'], more)


MISSING_WORDS = {'not in SUPPORTED': '(not in SUPPORTED)',
                 'not measured on that machine':
                 '(not measured on that machine)'}


def step2_held(rep, d, where):
    """What Step2Report REP read of step 2's text against D, the record
    the same run wrote (or the report's step2.rules): every line it
    prints of its rules, field by field."""
    def no(what, a, b):
        raise AssertionError(f'{where}: step 2 printed {what} {a!r}, its '
                             f'record says {b!r}')
    if d['state'] == 'not compared':
        if rep.nothing != d['why'] + '.':
            no('"Rules: not compared"', rep.nothing, d['why'])
        return
    t = d['totals']
    want = (t['names'], t['same'], t['differ'], t['not_compared'])
    if rep.totals != want:
        no('the totals', rep.totals, want)
    groups = []
    for x in d['differ']:
        c = x['characters']
        chars = (c['codepoints'] if c['state'] == 'listed' else
                 c['count'] if c['state'] == 'counted' else
                 () if c['state'] == 'none named' else
                 ('not known', c['why']))
        groups.append((x['names'], chars, x['every_rule']))
    if rep.groups != groups:
        no('the groups whose rules differ', rep.groups, groups)
    a = d['added']
    added = {}
    for x in a['rows']:
        if a['state'] == 'notice' and x['name'] == STEP2_C:
            added[STEP2_C] = 'C.UTF-8 !!'
            continue
        sp = x['spellings']
        tail = ', '.join(sp) if isinstance(sp, list) else \
            MISSING_WORDS[sp['state']]
        if x['renamed_from'] != '-':
            tail += f"; renamed from {x['renamed_from']}"
        added[x['name']] = tail
    if rep.added != added:
        no('the files only the new side has', rep.added, added)
    if rep.added_warned != (a['state'] == 'notice' and
                            any(n != STEP2_C for n in added)):
        no('the files only the new side has as a `!!`', rep.added_warned,
           a['state'])
    removed = {x['name']: '' if x['renamed_to'] == '-' else
               f"(renamed to {x['renamed_to']})" for x in d['removed']}
    if rep.removed != removed:
        no('the files only the old side has', rep.removed, removed)
    unread = (flat(f"Not compared, not read among a machine's locale "
                   f"sources ({len(d['unread'])}): "
                   + ', '.join(f"{x['name']} ({'; '.join(x['why'])})"
                               for x in d['unread']))
              if d['unread'] else None)
    if rep.unread != unread:
        no('the entries not read', rep.unread, unread)
    ub = d['unbuilt']
    unbuilt = ub['names'] if ub['state'] == 'listed' else []
    if rep.unbuilt != unbuilt:
        no('the names not in SUPPORTED', rep.unbuilt, unbuilt)
    if rep.not_ascii != d['not_ascii']:
        no('the names not ASCII', rep.not_ascii, d['not_ascii'])


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
      then added under STEP2_C as 'C.UTF-8 !!' when that `!!` names the old
      side only);
      added_warned: whether they came as the `!!` block;
      removed: {name: its "(renamed to ...)" or ''};
    listed: {name: why}, the `!!` block of names listed in the summary;
    reindex: the names of the "Listed in the summary" line; unbuilt and
      not_ascii: the two notes under it, as printed;
    groups: [(names, characters, every)] as printed, characters read as
      in differs, or ('not known', why); unread: the paragraph of the
      entries of a machine's sources not read, flattened, or None.

    Held to the record of the run that printed it (step2_held): DATA, or
    what step2_ran kept for this text, or that of the audit.sh runs whose
    output holds it, when they agree; a text with none fails, unless DATA is
    NO_RECORD (a text made up in a test).

    Every count a heading prints must equal the names under it, every line
    of a block it reads must be one of the block's forms, and the totals
    must add up: a block it cannot read raises, never reads as empty. A
    name printed as a bare "C" raises too: step 2 prints that locale as
    STEP2_C everywhere (16.139).
    """

    def __init__(self, out, data=None):
        self.out = out
        self.groups, self.unread = [], None
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
        # These three headings are wrapped at 78, going on at three blanks
        # (16.186), and read here as one line each: the removed heading
        # then still ends in its colon. The names under them are at two.
        joined = None
        while joined != out:
            joined, out = out, re.sub(
                r'(?m)^((?:Not compared, no file at |Rules: not compared, |'
                r'Character sets and transliterations: not compared, )'
                r'[^\n]*)\n   (?=\S)', r'\1 ', out)
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
            elif head.startswith("Not compared, not read among a "
                                 "machine's locale sources ("):
                self._seen('unread')
                self.unread = flat(para)
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
                if STEP2_C in self.added:
                    raise AssertionError('C named among the added files '
                                         'and in the C.UTF-8 `!!`')
                self.added[STEP2_C] = 'C.UTF-8 !!'
        for what, names in (('added', self.added), ('removed', self.removed),
                            ('differs', self.differs),
                            ('listed', self.listed),
                            ('listed in the summary', self.reindex),
                            ('not in SUPPORTED', self.unbuilt)):
            if 'C' in names:
                raise AssertionError(f'step 2 printed a bare C among the '
                                     f'{what} names, not {STEP2_C}')
        if self.totals is not None:
            n, same, differ, nc = self.totals
            if n != same + differ + nc:
                raise AssertionError(f'step 2 totals do not add up: '
                                     f'{self.totals}')
            if differ != len(self.differs):
                raise AssertionError(f'{differ} differ and {len(self.differs)}'
                                     f' named under "Rules differ"')
            named = len(self.added) + len(self.removed) + len(self.listed)
            # An entry of a machine's sources not read that the other side
            # holds is counted, and named only on its own line: the record
            # says which (step2_rules' unread, counted).
            d_ = self._record(data)
            if d_ not in (None, NO_RECORD) and \
                    d_.get('state') != 'not compared':
                named += sum(1 for x in d_['unread'] if x['counted'])
            if nc != named:
                raise AssertionError(f'{nc} not compared and {named} named '
                                     f'under the not-compared headings')
            if not self.reindex_line:
                raise AssertionError('step 2 compared and printed no '
                                     f'"{STEP2_LISTED}" line')
        d = self._record(data)
        if d is None:
            raise AssertionError(
                'Step2Report read a step 2 text with no record of the run '
                'that printed it: its runner calls step2_ran, check_report '
                'keeps an audit.sh run\'s, or the test passes data= (or '
                'NO_RECORD for text of no run)')
        if d is not NO_RECORD:
            step2_held(self, d, 'Step2Report')

    _once = None

    def _record(self, data):
        """The record of the run this text came from: DATA, or the one
        step2_ran kept for this text, or that of the audit.sh runs whose
        output holds it, when they agree; None when there is none."""
        if data is not None:
            return data
        if self.out in STEP2_DATA:
            return STEP2_DATA[self.out]
        runs = [d for out, d in STEP2_RUNS if self.out in out]
        if runs and all(d == runs[0] for d in runs):
            return runs[0]
        return None

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
                why = ('not known',
                       text[len('which characters is not known: '):])
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
            group = ' '.join(names).split(', ')
            self.groups.append((group, why if chars is None else chars,
                                every))
            for name in group:
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
    another shape, a name twice, and a bare "C": step 2 names glibc's file
    C as STEP2_C there too, as it prints it (16.185)."""
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
                or cells[2] not in ('yes', 'no') or cells[0] in got
                or cells[0] == 'C'):
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
