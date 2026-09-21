"""Layer 5: diff_distro_locales.py against a node it does not have.

The script's real input is a node's /usr/share/i18n/locales/, and CI has no
node. But the comparison it performs -- whole-block equality between two
directories of locale sources -- can be checked against an answer the suite
already trusts: step 2's, which reaches the same verdict by a completely
different route (diff hunks overlapped against the old side's line numbers).

Materialising an upstream tag as a stand-in for a node makes the whole script
exercisable on every push. Only the transport off a real node stays
un-exercised, which is the same position sql/collation_confirmation_template.sql
is in and is recorded as such in tests/README.md.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from _harness import (GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, flat,
                      needs_clone, run_step)

import diff_distro_locales as dd
import glibc_locale_data as g


def materialise(tag, dest):
    """A tag's locale sources on disk, standing in for a node's copy."""
    return dd.materialise_tag(GLIBC_CLONE, tag, dest)


def run_script(*args):
    """Not run_step: its cache is keyed on argv, and these tests deliberately
    re-run the same argv against a mutated directory."""
    p = subprocess.run([sys.executable,
                        os.path.join(SCRIPTS_DIR, 'diff_distro_locales.py'),
                        *args],
                       cwd=SCRIPTS_DIR, capture_output=True)
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


@needs_clone
class MatchesStepTwo(unittest.TestCase):
    """The equivalence that makes this script trustworthy without a node.

    Two independent algorithms must agree. Step 2 counts a renamed file by its
    OLD path, so its content-changed total is `modified + renames` while a
    both-tags file set can only see `modified`. Asserting raw equality would
    fail on 2.34..2.39, which has one rename -- so the relation is asserted
    instead.
    """

    EXPECTED = {
        # pair: (files differing, locales differing inside LC_COLLATE)
        (OLD, MID): (281, ['or_IN', 'sv_SE']),
        (MID, NEW): (306, ['ber_DZ', 'kab_DZ', 'th_TH']),
    }

    def test_reproduces_step_2_on_both_pairs(self):
        for (old, new), (differ, inside) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                tmp = tempfile.mkdtemp(prefix='pg-glibc-distro-')
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                node = materialise(old, tmp)          # old tag stands in as the node
                rc, out = run_script(new, '--locales-dir', node,
                                     '--build-id', f'stand-in-for-{old}',
                                     '--node-label', 'fixture')
                self.assertEqual(rc, 0, out)

                def n(label):
                    return int(re.search(rf'{label}:\s+(\d+)', out).group(1))
                got_inside = n('differ INSIDE LC_COLLATE')
                self.assertEqual(got_inside, len(inside), out)
                # The three buckets step 2 also prints: "N touch LC_COLLATE,
                # M do not, K have no LC_COLLATE block". All three are
                # differing files; only the first is a collation finding.
                self.assertEqual(
                    got_inside + n('differ outside LC_COLLATE')
                    + n('no LC_COLLATE either side'),
                    differ, out)
                for name in inside:
                    self.assertIn(name, out)

    def test_the_delta_from_step_2_is_exactly_the_renames(self):
        """Step 2's content-changed count includes each rename's old path.
        Pair 1 has no rename and matches exactly; pair 2 has one and is off by
        one. Pinning the relation stops that looking like a regression."""
        for (old, new), (differ, _) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                step2 = run_step('filter_lc_collate_changes.py', old, new)[1]
                content = int(re.search(r'Of the (\d+) content-changed',
                                        step2).group(1))
                renames = int(re.search(r'renamed:\s+(\d+)', step2).group(1))
                self.assertEqual(content - renames, differ)


@needs_clone
class RefusesToGuess(unittest.TestCase):
    """The failure modes that would otherwise report a clean zero."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-distro-bad-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.node = materialise(MID, self.tmp)

    def test_a_truncated_directory_aborts(self):
        """A half-copied directory reports '0 inside LC_COLLATE', which is
        indistinguishable from a clean result. The recorded docker cp trap."""
        keep = sorted(os.listdir(self.node))[:3]
        for name in os.listdir(self.node):
            if name not in keep:
                os.remove(os.path.join(self.node, name))
        rc, out = run_script(MID, '--locales-dir', self.node,
                             '--build-id', 'truncated', '--node-label', 'trunc')
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn('differ INSIDE LC_COLLATE:  0', out)

    def test_expect_files_is_enforced(self):
        rc, out = run_script(MID, '--locales-dir', self.node,
                             '--build-id', 'x', '--node-label', 'x',
                             '--expect-files', '1')
        self.assertNotEqual(rc, 0, out)

    def test_build_id_is_required(self):
        """A result that does not say which build it was taken on cannot move
        the table in docs/limitations.md."""
        rc, out = run_script(MID, '--locales-dir', self.node)
        self.assertEqual(rc, 2, out)

    def test_a_skipped_entry_is_named_not_dropped(self):
        os.mkdir(os.path.join(self.node, 'a_subdir'))
        rc, out = run_script(MID, '--locales-dir', self.node,
                             '--build-id', 'x', '--node-label', 'skip')
        self.assertEqual(rc, 0, out)
        self.assertIn('a_subdir', out)

    def test_a_file_absent_upstream_is_named_and_resolved(self):
        """en_US@ampm is Red Hat-only and in no upstream tag. The script holds
        the node's copy, so it can say whether the blindness matters."""
        extra = os.path.join(self.node, 'zz_MADEUP')
        with open(extra, 'w', encoding='utf-8') as fh:
            fh.write('comment_char %\nLC_COLLATE\ncopy "iso14651_t1"\n'
                     'END LC_COLLATE\n')
        rc, out = run_script(MID, '--locales-dir', self.node,
                             '--build-id', 'x', '--node-label', 'absent')
        self.assertEqual(rc, 0, out)
        self.assertIn('zz_MADEUP', out)
        self.assertIn('pure copy of iso14651_t1', out)

    def test_the_C_code_caveat_is_printed(self):
        """A clean data result does not clear a backported C-code change --
        Bug 22668 is in C, not localedata. Warned in audit.sh's !! format."""
        rc, out = run_script(MID, '--locales-dir', self.node,
                             '--build-id', 'x', '--node-label', 'warn')
        self.assertEqual(rc, 0, out)
        self.assertIn('!!', out)
        self.assertIn('22668', out)
        self.assertIn('charmaps', out)


@needs_clone
class ATruncatedCopySaysSo(unittest.TestCase):
    """Backlog 1.15: a copy that lost files in transit reported a clean zero.

    Measured 2026-09-21 on 300 of glibc-2.34's 355 files: exit 0, "Nothing
    differs inside LC_COLLATE", and the 55 missing ones printed as an ordinary
    list with no `!!`. Bisected the same day: the only guard was
    `compared < reference // 2`, so 177 of 355 passed and 176 did not, and
    steps 9/10 refused a directory this step accepted.
    """

    ABSENT = 'locale file(s) are NOT in'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-distro-trunc-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.node = materialise(MID, self.tmp)
        self.total = len(os.listdir(self.node))

    def truncate_to(self, keep):
        """Drop all but `keep` files; returns how many went missing."""
        for name in sorted(os.listdir(self.node))[keep:]:
            os.remove(os.path.join(self.node, name))
        return self.total - keep

    def run_node(self, *extra):
        return run_script(MID, '--locales-dir', self.node,
                          '--build-id', 'truncated', '--node-label', 'trunc',
                          *extra)

    def test_a_partial_copy_earns_the_warning_marker(self):
        missing = self.truncate_to(250)
        rc, out = self.run_node()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"!! {missing} of {MID}'s {self.total} "
                      f"locale file(s) are NOT in", flat(out))

    def test_a_complete_copy_earns_no_absent_file_warning(self):
        """The control. A guard that fires on every corpus guards nothing."""
        rc, out = self.run_node('--expect-files', str(self.total))
        self.assertEqual(rc, 0, out)
        self.assertNotIn(self.ABSENT, flat(out))
        self.assertNotIn('were not compared at all', flat(out))

    def test_an_unasserted_count_says_so_even_when_nothing_is_absent(self):
        """Nothing absent means nothing the TAG holds is missing. A locale
        the distro adds is in no tag, so its loss moves no count this step
        can check: measured with two node-only files, one deleted -- exit 0,
        `absent on the node: 0`, and before this, no marker at all."""
        rc, out = self.run_node()
        self.assertEqual(rc, 0, out)
        self.assertIn('was NOT asserted: no --expect-files', flat(out))

    def test_an_asserted_count_is_not_nagged_about(self):
        """The mirror control: a reader who passed the count must not be told
        to pass it."""
        rc, out = self.run_node('--expect-files', str(self.total))
        self.assertEqual(rc, 0, out)
        self.assertNotIn('was NOT asserted', flat(out))

    def test_the_clean_sentence_stops_reading_as_coverage(self):
        """"For every locale compared" is true and reads as "for every
        locale". When something was not compared, the sentence says so."""
        missing = self.truncate_to(250)
        rc, out = self.run_node()
        self.assertEqual(rc, 0, out)
        self.assertIn('Nothing differs inside LC_COLLATE', out)
        self.assertIn(f'the {missing} absent from the node were not '
                      f'compared at all', flat(out))

    def test_the_sentence_is_untouched_when_nothing_is_absent(self):
        """Byte-for-byte what main printed, so the published examples and
        the acceptance runs do not move."""
        rc, out = self.run_node()
        self.assertEqual(rc, 0, out)
        self.assertIn('Nothing differs inside LC_COLLATE. For every locale '
                      'compared, the tag diff is reading the same collation '
                      'data truncated runs.', flat(out))

    def test_a_copy_below_the_floor_is_refused_though_above_half(self):
        """The window this step used to accept and steps 9/10 did not: more
        than half of the tag, fewer than the 200 every other node-reading
        mode requires."""
        keep = g.MIN_LOCALE_FILES - 10
        self.assertGreater(keep, self.total // 2,
                           "fixture no longer sits in the window it tests")
        self.assertLess(keep, g.MIN_LOCALE_FILES)
        self.truncate_to(keep)
        rc, out = self.run_node()
        self.assertEqual(rc, 2, out)
        self.assertIn(f'below the floor of {g.MIN_LOCALE_FILES}', flat(out))

    def test_the_read_count_says_whether_anyone_asserted_it(self):
        """An asserted run and an unasserted one used to differ only by the
        ABSENCE of a warning -- which is also what every older version of this
        tool printed, so a saved transcript could not tell them apart."""
        rc, out = self.run_node()
        self.assertEqual(rc, 0, out)
        self.assertIn(f'Files read: {self.total} '
                      f'(NOT asserted, no --expect-files)', flat(out))
        rc, out = self.run_node('--expect-files', str(self.total))
        self.assertEqual(rc, 0, out)
        self.assertIn(f'Files read: {self.total} (asserted, --expect-files)',
                      flat(out))

    def test_a_reader_who_passed_the_count_is_not_told_to_pass_it(self):
        """The absent-file `!!` still fires with an expectation given and met
        -- a node may genuinely lack a locale the tag has -- but its closing
        advice is to do the thing the reader already did."""
        keep = 250
        self.truncate_to(keep)
        rc, out = self.run_node('--expect-files', str(keep))
        self.assertEqual(rc, 0, out)
        self.assertIn(self.ABSENT, flat(out))
        self.assertNotIn('and pass --expect-files N', flat(out))

    def test_a_met_expectation_does_not_clear_the_missing_files(self):
        """A met --expect-files proves N equals what THIS DIRECTORY holds --
        not that N came from the node. The reader is told to take N from this
        tool's own output when entries are skipped, which closes the circle.
        Measured: 250 of 355 files with a subdirectory added and
        `--expect-files 250` exits 0, and the clause this test removes called
        those 105 lost files "locales the node does not ship"."""
        self.truncate_to(250)
        for extra in ((), ('--expect-files', '250')):
            with self.subTest(args=extra):
                rc, out = self.run_node(*extra)
                self.assertEqual(rc, 0, out)
                self.assertIn('are indistinguishable here, and the truncated '
                              'one', flat(out))
                self.assertNotIn('not files the copy lost', flat(out))
                self.assertNotIn('the node does not ship', flat(out))

    def test_without_an_expectation_the_advice_is_there(self):
        """The mirror control: the advice must survive for the reader who
        has not yet passed a count."""
        self.truncate_to(250)
        rc, out = self.run_node()
        self.assertEqual(rc, 0, out)
        self.assertIn('and pass --expect-files N', flat(out))

    def test_expect_files_counts_the_directory_not_the_intersection(self):
        """The count a reader can produce is `ls | wc -l`. On the el8 node
        that is 355 while step 6 compares 353, so an expectation checked
        against the intersection refuses a correct run and the number that
        would pass cannot be known without running the tool first."""
        with open(os.path.join(self.node, 'zz_MADEUP'), 'w',
                  encoding='utf-8') as fh:
            fh.write('comment_char %\nLC_COLLATE\ncopy "iso14651_t1"\n'
                     'END LC_COLLATE\n')
        directory, intersection = self.total + 1, self.total

        rc, out = self.run_node('--expect-files', str(directory))
        self.assertEqual(rc, 0, out)
        self.assertIn(f'Compared {intersection} file(s)', out)

        rc, out = self.run_node('--expect-files', str(intersection))
        self.assertEqual(rc, 2, out)
        self.assertIn(f'read {directory} file(s) from --locales-dir, '
                      f'expected {intersection}', flat(out))


if __name__ == '__main__':
    unittest.main()
