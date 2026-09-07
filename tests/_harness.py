"""Shared setup for the test suite: import paths, the glibc clone, subprocesses.

Nothing here asserts anything. It exists so that a test that needs the real
glibc clone SKIPS with a reason instead of failing, and so that running the
suite never writes into the shared /tmp output directory the scripts default to.
"""
import os
import subprocess
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(REPO_ROOT, 'scripts')
GLIBC_CLONE = os.path.join(SCRIPTS_DIR, 'glibc')

# The scripts import each other as top-level modules (`import glibc_locale_data
# as g`), so they are only importable with scripts/ on the path.
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# The three tags the published results are stated against.
OLD, MID, NEW = 'glibc-2.28', 'glibc-2.34', 'glibc-2.39'
PAIRS = ((OLD, MID), (MID, NEW))

# The pair below the OLD version floor. Not audited and not published as a
# result -- it is the pair that demonstrates what the pre-2.24 failure looked
# like, and docs/limitations.md now quotes numbers from it. Those numbers need
# the same pinning as any other: the last figure that page carried for this
# pair went stale without anyone noticing, which is exactly what a test is for.
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


def have_clone():
    """Is there a usable glibc clone to run the git-backed layers against?

    Asked of git, not of the filesystem: audit-locale-diff.sh clones with
    --no-checkout, so localedata/locales never appears on disk.
    """
    if not os.path.isdir(GLIBC_CLONE):
        return False
    import glibc_locale_data as g
    return g._is_glibc_clone(GLIBC_CLONE)


def has_tags(*tags):
    """Are these tags present locally? A shallow or fresh clone may lack them."""
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
    fresh process can be pointed at a scratch directory instead of the shared
    /tmp one. And the contract these tools actually offer a user is their
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


# --- fixtures for the one locale that is in no tag ---------------------------
#
# C.UTF-8's source file exists upstream only from glibc 2.35, while RHEL8 and
# RHEL9 ship a backported copy. Two test modules need both shapes, and neither
# can get the backported one from the clone -- fabricating it is the only way
# to test the locale this project's first false negative was about.

def locale_file(*body, comment='%'):
    """A minimal locale source file with an LC_COLLATE block."""
    return '\n'.join([f'comment_char {comment}', 'escape_char /', '',
                      'LC_COLLATE', *body, 'END LC_COLLATE', ''])


def backported_c():
    """localedata/locales/C as RHEL8 actually ships it.

    Copied from glibc-2.28-251.el8_10.40 on collaudit8, 2026-09-06 -- not
    invented. Six ellipsis ranges, which is why step 4 flags it and why Bug
    22668 ("LC_COLLATE: Fix last character ellipsis handling") could move
    C.UTF-8's order with this file untouched.

    Note which planes are NOT here: 3 through 13 have no range at all, so
    every code point in them falls to UNDEFINED. That is the defect Red Hat
    bug 1361965 fixed in glibc-2.28-93.el8, and it is why 40 of 41 measured
    code points sort out of code point order on RHEL8.
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
    other and carry exactly this block.

    The prose above the keyword is quoted from the real file, and is the trap:
    it names codepoint_collation three lines before declaring it.
    """
    return locale_file(
        "% The keyword 'codepoint_collation' in any part of any LC_COLLATE",
        '% immediately discards all collation information and causes the',
        '% locale to use strcmp/wcscmp for collation comparison.',
        'codepoint_collation')
