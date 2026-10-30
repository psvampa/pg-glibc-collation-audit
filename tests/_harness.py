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
# audit.sh, scripts/audit-locale-diff.sh and glibc_locale_data.OUT_DIR each
# spell it; test_parallel_runner ties every one of them to this name, because
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

    Asked of git, not of the filesystem: audit-locale-diff.sh clones with
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
    "no glibc clone at scripts/glibc -- run scripts/audit-locale-diff.sh "
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
    OLD and MID read as RHEL 9 reads them (codepoint_collation is a keyword
    there).
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
    # What step 2 asks when a pair crosses glibc 2.36: every file the two
    # tags' localedef read differently (glibc_tree.across), for the three
    # such pairs the suite runs. Computed by every class at once, each one
    # waited on the first and the whole suite took 512 s against 260 s
    # (backlog 15.2 B3, measured on the Mac with nothing kept).
    import filter_lc_collate_changes as f
    for old, new in ((MID, NEW), (NEW, MID), (OLD, NEW)):
        if not has_tags(old, new):
            continue
        builds = glibc_tree.builds_at(GLIBC_CLONE, [old, new])
        paths, ids = g.list_tree_ids(GLIBC_CLONE, old, [])
        f.read_across(GLIBC_CLONE, old, (builds[old], builds[new]), paths,
                      ids)
    # OLD as RHEL 9 reads it: the tree test_wrapper.WrapperNodesOnOneBuild
    # reads, with its own C (two machines on one el9 build). Left to that
    # class, one processor, it ran past the 600 s limit on GitHub (PR #117).
    if has_tags(OLD):
        old9 = glibc_tree.build_at(GLIBC_CLONE, OLD, '.el9')
        if not kept(OLD, old9):
            glibc_tree.Tree.at_tag(GLIBC_CLONE, OLD, old9)
    if has_tags(MID):
        rhel9 = glibc_tree.build_at(GLIBC_CLONE, MID, '.el9')
        if kept(MID, rhel9):
            return
        paths = g.list_locale_files(GLIBC_CLONE, MID)
        contents, missing = g.read_blobs(GLIBC_CLONE, MID, paths, raw=True)
        g.require_read(MID, paths, missing, 'the locale files')
        files = {os.path.basename(p).encode('utf-8', 'surrogateescape'): v
                 for p, v in contents.items()}
        glibc_tree.Tree(MID, files, rhel9, g.decoded)


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
    glibc's two keyword tables.

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
         'locale/programs/charmap-kw.gperf')


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
    several tests, and step 5 takes ~3.5s a call. Caching turns the suite from
    ~40s into ~10s without any test losing its independence -- these scripts
    are read-only against a fixed tag, so a second run cannot print anything
    different.

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
    env.pop('PG_GLIBC_AUDIT_WRAPPED', None)
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

def flat(text):
    """Output with every run of whitespace collapsed to one space.

    dd.warn wraps at 78 columns, so a phrase of more than a few words is split
    across lines. Asserting on the raw text makes a POSITIVE assertion brittle
    and -- far worse -- makes a NEGATIVE one vacuous: `assertNotIn` on a phrase
    that is always broken up passes whether the warning is printed or not.
    Caught by exactly that, on 2026-09-06. Every layer that asserts on
    wrapped output goes through
    this one copy; a second copy is how two helpers drift.
    """
    return ' '.join(text.split())


def default_weight_section(out):
    """(count, [names]) from step 4's default-weight section.

    Raises when the section is missing, because "no locale depends only on the
    default weight" and "the section was never printed" are different facts.
    The names line carries the first twelve; a count above that comes back
    with the names it printed, and the caller decides what it can compare.
    """
    m = re.search(r"^Additionally exposed through localedef's default weight: "
                  r"(\d+)\n {6}(.+)$", out, re.M)
    if not m:
        raise ValueError(f"no default-weight section in output:\n{out[:800]}")
    names = m.group(2).split(', ')
    if names[-1] == '...':
        names = names[:-1]
    return int(m.group(1)), names


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
