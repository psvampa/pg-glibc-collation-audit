"""Layer 2: the git-backed helpers, against the real clone.

This layer exists for one class of bug: code that cannot tell "there is nothing
here" from "I could not look". Both print the same thing, and in this tool the
thing they print is a clean verdict.

The failures are provoked with invalid tags and paths. No network is touched and
the clone is never modified.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _harness import GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, needs_clone

import glibc_locale_data as g
import diff_collation_code as d

BAD = 'no-such-tag-4c1f'


def in_subprocess(code):
    """Run code that is expected to call die(); return (exit, output).

    die() calls sys.exit, so these have to run out of process to be observed
    without tearing down the test runner.
    """
    boot = ("import sys; sys.path.insert(0, %r); "
            "import glibc_locale_data as g, diff_collation_code as d; "
            "repo = %r\n" % (os.path.dirname(g.__file__), GLIBC_CLONE))
    p = subprocess.run([sys.executable, '-c', boot + code],
                       capture_output=True, cwd=os.path.dirname(g.__file__))
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


@needs_clone
class GitFailureIsNotSilence(unittest.TestCase):
    """"A failed git was indistinguishable from 'this file did not change'".

    On a --filter=blob:none clone, fetches really do fail:
        fatal: could not fetch <oid> from promisor remote
    An invalid revision range produces the same shape -- non-zero exit, empty
    stdout -- without needing the network to be down.
    """

    def test_report_file_aborts_instead_of_reporting_no_change(self):
        """It used to return 0, i.e. 'no substantive change', for a file whose
        diff it never read."""
        rc, out = in_subprocess(
            "d.report_file(repo, 'locale/programs/ld-collate.c',"
            " '%s1..%s2', False)" % (BAD, BAD))
        self.assertNotEqual(rc, 0, 'a failed git diff was swallowed')
        self.assertIn('fatal', out.lower())

    def test_check_paths_aborts_instead_of_calling_a_real_file_harmless(self):
        """It used to return outside=['locale/programs/ld-collate.c'], printed
        as 'nothing to read, and nothing to miss', for a file that exists."""
        rc, out = in_subprocess(
            "print(d.check_paths(repo, ['locale/programs/ld-collate.c'],"
            " '%s1', '%s2'))" % (BAD, BAD))
        self.assertNotEqual(rc, 0, 'a failed ls-tree was swallowed')
        self.assertNotIn("outside=['locale/programs/ld-collate.c']", out)

    def test_a_diff_that_really_is_empty_still_reports_no_change(self):
        """The other direction: aborting must not turn 'unchanged' into an
        error. coll-lookup.h has only a copyright change over this pair."""
        n = d.report_file(GLIBC_CLONE, 'locale/coll-lookup.h',
                          f'{MID}..{NEW}', False, quiet_when_clean=True)
        self.assertEqual(n, 0)


@needs_clone
class UnreadableBlobsAreNotDropped(unittest.TestCase):
    """read_blobs_strict: paths that came out of the tree at this tag must all
    be readable. Dropping one shrinks the copy graph or the include walk, and a
    smaller graph yields a shorter affected-locale list."""

    def test_aborts_and_names_the_path_it_could_not_read(self):
        rc, out = in_subprocess(
            "g.read_blobs_strict(repo, '%s',"
            " ['localedata/locales/no-such-locale'], 'the copy graph')" % NEW)
        self.assertNotEqual(rc, 0)
        self.assertIn('no-such-locale', out)
        self.assertIn('the copy graph', out, 'error does not say what broke')

    def test_valid_paths_are_returned(self):
        got = g.read_blobs_strict(GLIBC_CLONE, NEW,
                                  ['localedata/locales/sv_SE'], 'a test')
        self.assertIn('localedata/locales/sv_SE', got)
        self.assertIn('LC_COLLATE', got['localedata/locales/sv_SE'])

    # The two tests below assert the CALL SITES, not the helper. Mutation
    # testing showed why: reverting build_copy_graph to `contents, _ =
    # read_blobs(...)` left the whole suite green, because every path it reads
    # comes from ls-tree at the same tag and so `missing` is empty in practice.
    # Testing the helper alone guards nothing. Injecting a missing blob is what
    # makes the guard observable.

    def test_build_copy_graph_aborts_on_an_unreadable_blob(self):
        """A dropped blob silently shrinks the copy graph, and a smaller graph
        yields a shorter affected-locale list -- the reassuring direction."""
        rc, out = in_subprocess(
            "real = g.read_blobs\n"
            "def fake(repo, tag, paths):\n"
            "    c, _ = real(repo, tag, paths)\n"
            "    dropped = sorted(c)[0]\n"
            "    del c[dropped]\n"
            "    return c, {dropped}\n"
            "g.read_blobs = fake\n"
            "g.build_copy_graph(repo, %r)\n" % NEW)
        self.assertNotEqual(rc, 0, 'an unreadable locale was dropped silently')
        self.assertIn('copy graph', out)

    def test_the_include_walk_aborts_on_an_unreadable_blob(self):
        rc, out = in_subprocess(
            "real = g.read_blobs\n"
            "def fake(repo, tag, paths):\n"
            "    c, _ = real(repo, tag, paths)\n"
            "    dropped = sorted(c)[0]\n"
            "    del c[dropped]\n"
            "    return c, {dropped}\n"
            "g.read_blobs = fake\n"
            "d.reachable_from_entry_points(repo, %r)\n" % NEW)
        self.assertNotEqual(rc, 0, 'an unreadable header shrank the walk')
        self.assertIn('include walk', out)


@needs_clone
class MissingIsNotUnchanged(unittest.TestCase):
    """"Step 5 could not tell 'unchanged' from 'not there'".

    git diff over a path absent at both tags is empty and exits 0 -- and so is
    the truth. A path that vanished between the tags reads the same way and is
    not the truth.
    """

    def test_a_path_that_vanished_is_reported_as_vanished(self):
        # gen-translit.pl exists at 2.28 and is deleted by 2.34.
        vanished, outside = d.check_paths(GLIBC_CLONE,
                                          ['locale/gen-translit.pl'], OLD, MID)
        self.assertEqual(vanished, ['locale/gen-translit.pl'])
        self.assertEqual(outside, [])

    def test_a_path_absent_at_both_tags_is_reported_as_outside(self):
        # C-collate-seq.c arrives upstream after 2.34.
        vanished, outside = d.check_paths(GLIBC_CLONE,
                                          ['locale/C-collate-seq.c'], OLD, MID)
        self.assertEqual(vanished, [])
        self.assertEqual(outside, ['locale/C-collate-seq.c'])

    def test_a_path_present_at_both_tags_is_neither(self):
        vanished, outside = d.check_paths(
            GLIBC_CLONE, ['locale/programs/ld-collate.c'], OLD, MID)
        self.assertEqual((vanished, outside), ([], []))


@needs_clone
class IncludeWalk(unittest.TestCase):
    """"Step 5's file list was the ceiling of what it could see".

    linereader.h and elem-hash.h both changed over 2.34..2.39 and were in
    neither hand-written tier, so they were never read.
    """

    def test_the_walk_reaches_the_files_the_hand_lists_missed(self):
        got = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        for path in ('locale/programs/linereader.h', 'locale/elem-hash.h',
                     'locale/programs/locfile-token.h'):
            self.assertIn(path, got)

    def test_the_walk_picks_up_separately_compiled_units(self):
        """coll-lookup.c and simple-hash.c are linked, not included; they are
        reached as the sibling .c of a header the walk found."""
        got = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        self.assertIn('locale/coll-lookup.c', got)
        self.assertIn('locale/programs/simple-hash.c', got)

    def test_the_walk_stays_bounded(self):
        """Unbounded it reaches 265 files dominated by stdio.h and cdefs.h.
        The bound is what makes the output readable, so it is asserted."""
        got = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        self.assertLess(len(got), 60, 'the include walk escaped locale/')
        for path in got:
            self.assertTrue(path.startswith('locale/'), path)

    def test_hand_listed_paths_the_walk_cannot_reach_are_still_tracked(self):
        """weight.h arrives via `#include WEIGHT_H`, a macro no regex resolves,
        and it HAS a substantive change over 2.34..2.39. Replacing the curated
        lists with the walk would have lost it."""
        walk = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        for path in ('locale/weight.h', 'locale/weightwc.h',
                     'locale/lc-collate.c', 'locale/C-collate.c'):
            self.assertNotIn(path, walk)
            self.assertIn(path, d.TIER1)


@needs_clone
class SupportedMap(unittest.TestCase):
    """"supported_map() returned an empty map when SUPPORTED was missing",
    which made every locale print as 'not built by default'."""

    def test_aborts_when_supported_is_absent(self):
        rc, out = in_subprocess("g.supported_map(repo, '%s')" % BAD)
        self.assertNotEqual(rc, 0)

    def test_maps_source_names_to_generated_names(self):
        got = g.supported_map(GLIBC_CLONE, NEW)
        self.assertIn('sv_SE.utf8', got['sv_SE'])
        self.assertIn('C.utf8', got['C'])


@needs_clone
class CloneDetection(unittest.TestCase):
    """"Nothing ran on a clean machine": the clone is made --no-checkout, so
    localedata/locales never appears on disk and a filesystem test says no."""

    def test_accepts_the_real_clone(self):
        self.assertTrue(g._is_glibc_clone(GLIBC_CLONE))

    def test_rejects_a_directory_that_is_not_a_repo(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertFalse(g._is_glibc_clone(tmp))
        finally:
            shutil.rmtree(tmp)

    def test_rejects_an_unrelated_git_repo(self):
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            self.assertFalse(g._is_glibc_clone(tmp))
        finally:
            shutil.rmtree(tmp)

    def test_rejects_a_path_that_does_not_exist(self):
        self.assertFalse(g._is_glibc_clone('/no/such/path/4c1f'))


def git(repo, *args, env=None):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True,
                   env=env)


def make_glibc_shaped_repo(root, n_files, rename=False):
    """A git repository that passes _is_glibc_clone: localedata/locales/ with
    `n_files` locale sources and a SUPPORTED, committed and tagged `t1`. With
    `rename`, a second commit `t2` renames the block-less file `x` to `y` AND
    gives it an LC_COLLATE block -- the case no upstream pair has produced.
    """
    loc = os.path.join(root, 'localedata', 'locales')
    os.makedirs(loc)
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    body = ''.join(f'% filler line {i}\n' for i in range(40))
    for i in range(n_files):
        with open(os.path.join(loc, f'loc_{i:03d}'), 'w') as fh:
            fh.write(f'comment_char %\nescape_char /\n{body}LC_CTYPE\n'
                     f'copy "i18n"\nEND LC_CTYPE\n')
    with open(os.path.join(loc, 'x'), 'w') as fh:
        fh.write(f'comment_char %\nescape_char /\n{body}LC_CTYPE\n'
                 f'copy "i18n"\nEND LC_CTYPE\n')
    with open(os.path.join(root, 'localedata', 'SUPPORTED'), 'w') as fh:
        fh.write('x.UTF-8/UTF-8 \\\ny.UTF-8/UTF-8 \\\n')
    git(root, 'init', '-q', env=env)
    git(root, 'add', '.', env=env)
    git(root, 'commit', '-q', '-m', 't1', env=env)
    git(root, 'tag', 't1', env=env)
    if rename:
        git(root, 'mv', os.path.join(loc, 'x'), os.path.join(loc, 'y'), env=env)
        with open(os.path.join(loc, 'y'), 'a') as fh:
            fh.write('LC_COLLATE\norder_start forward\n<U0041>\norder_end\n'
                     'END LC_COLLATE\n')
        git(root, 'add', '.', env=env)
        git(root, 'commit', '-q', '-m', 't2', env=env)
        git(root, 'tag', 't2', env=env)
    return root


def run_script(script, *args, env_extra=None):
    """A step as a subprocess, uncached, optionally with extra environment."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    cmd = ([sys.executable, os.path.join(SCRIPTS_DIR, script)]
           if script.endswith('.py') else [os.path.join(SCRIPTS_DIR, script)])
    p = subprocess.run(cmd + list(args), cwd=SCRIPTS_DIR, env=env,
                       capture_output=True)
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


class TagModeCorpusFloor(unittest.TestCase):
    """"No corpus floor in tag mode": a tag whose tree holds a handful of
    locale files -- or none -- made step 2 print "0 changed", step 4 print
    "No locale uses ellipsis ranges here", and both exit 0. The node modes had
    refused such a directory from the start. No clone needed: the repository
    is fabricated, because no pinned tag is small enough to show it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-tiny-repo-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_step_4_refuses_a_tag_below_the_floor(self):
        repo = make_glibc_shaped_repo(self.tmp, n_files=3)
        rc, out = run_script('flag_algorithmic_ranges.py', 't1', '--repo', repo)
        self.assertEqual(rc, 2, out)
        self.assertIn(f'below the floor of {g.MIN_LOCALE_FILES}', out)
        self.assertNotIn('No locale uses ellipsis ranges', out)

    def test_step_2_refuses_a_tag_below_the_floor(self):
        repo = make_glibc_shaped_repo(self.tmp, n_files=3, rename=True)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo)
        self.assertEqual(rc, 2, out)
        self.assertIn('below the floor', out)
        self.assertNotIn('Files with changes inside LC_COLLATE', out)

    def test_the_shell_step_refuses_too(self):
        """Step 1 is shell; it asks glibc_locale_data.py, which dies."""
        repo = make_glibc_shaped_repo(self.tmp, n_files=3)
        p = subprocess.run([sys.executable,
                            os.path.join(SCRIPTS_DIR, 'glibc_locale_data.py'),
                            'corpus', 't1'],
                           cwd=repo, capture_output=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(b'below the floor', p.stderr)

    def test_a_corpus_at_the_floor_passes(self):
        """The control: the guard refuses size, not fabricated repositories."""
        repo = make_glibc_shaped_repo(self.tmp, n_files=g.MIN_LOCALE_FILES,
                                      rename=True)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo)
        self.assertEqual(rc, 0, out)


class RenamedFileGainsABlock(unittest.TestCase):
    """"Step 2 aborted on a rename without an old LC_COLLATE block", end to end
    on a fabricated repository: `x` (no block) becomes `y` (with one). The old
    code died reading `x` at t2; with the read fixed but the lookup not, it
    reported y as 'no LC_COLLATE on either side'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-rename-repo-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-rename-out-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(self.tmp, n_files=g.MIN_LOCALE_FILES,
                                           rename=True)

    def test_the_renamed_file_is_reported_as_gained_collate(self):
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', self.repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_dir})
        self.assertEqual(rc, 0, out)
        self.assertIn('renamed: 1', out)
        self.assertIn('1 file(s) GAINED an LC_COLLATE block at t2', out)
        self.assertIn('Files with changes inside LC_COLLATE: 1', out)
        self.assertIn('x -> y', out)
        with open(os.path.join(self.out_dir, 'step2_changed_collate.t1..t2.txt'),
                  encoding='utf-8') as fh:
            self.assertEqual(fh.read().split(), ['y'],
                             'step 3 must get the name that exists at t2')


@needs_clone
class UserGitConfigCannotChangeTheAnswer(unittest.TestCase):
    """"git diff respects the user's config": diff.noprefix broke the header
    regex (loudly), a low diff.renameLimit or diff.renames=false turned the
    one rename over 2.34..2.39 into delete+add (silently: the added file lands
    in 'not analysed', and step 1's count reads 319 instead of 318), and
    color.ui=always put escape codes in the pipe. Measured, git 2.50. The
    steps now pin every one of those with -c; this runs them under the hostile
    config and asserts the output is the one the suite already trusts."""

    HOSTILE = ('[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n'
               '\trenames = false\n\trenameLimit = 1\n'
               '[color]\n\tui = always\n')

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-gitconfig-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.cfg = os.path.join(tmp, 'gitconfig')
        with open(self.cfg, 'w', encoding='utf-8') as fh:
            fh.write(self.HOSTILE)
        self.out_a = tempfile.mkdtemp(prefix='pg-glibc-cfg-a-')
        self.out_b = tempfile.mkdtemp(prefix='pg-glibc-cfg-b-')
        self.addCleanup(shutil.rmtree, self.out_a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.out_b, ignore_errors=True)

    def test_the_hostile_config_really_is_hostile(self):
        """Control: without the overrides the config changes the text."""
        p = subprocess.run(['git', 'diff', '--name-only', f'{MID}..{NEW}',
                            '--', 'localedata/locales/'],
                           cwd=GLIBC_CLONE, capture_output=True,
                           env=dict(os.environ, GIT_CONFIG_GLOBAL=self.cfg))
        names = p.stdout.decode().split()
        self.assertEqual(len(names), 319, 'renames=false no longer changes '
                         'the count; the mutation this class guards is gone')

    def test_step_2_is_identical_under_the_hostile_config(self):
        plain = run_script('filter_lc_collate_changes.py', MID, NEW,
                           env_extra={'PG_GLIBC_AUDIT_OUT': self.out_a})
        hostile = run_script('filter_lc_collate_changes.py', MID, NEW,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_b,
                                        'GIT_CONFIG_GLOBAL': self.cfg})
        self.assertEqual(plain[0], 0, plain[1])
        self.assertEqual(hostile, plain)
        self.assertIn('renamed: 1', hostile[1])

    def test_step_1_is_identical_under_the_hostile_config(self):
        plain = run_script('audit-locale-diff.sh', MID, NEW,
                           env_extra={'PG_GLIBC_AUDIT_OUT': self.out_a})
        hostile = run_script('audit-locale-diff.sh', MID, NEW,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_b,
                                        'GIT_CONFIG_GLOBAL': self.cfg})
        self.assertEqual(plain[0], 0, plain[1])
        self.assertIn('Locale files added, modified or deleted: 318', plain[1])
        self.assertEqual(hostile[1].replace(self.out_b, self.out_a), plain[1])


if __name__ == '__main__':
    unittest.main()
