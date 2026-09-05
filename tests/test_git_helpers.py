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

from _harness import GLIBC_CLONE, MID, NEW, OLD, needs_clone

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


if __name__ == '__main__':
    unittest.main()
