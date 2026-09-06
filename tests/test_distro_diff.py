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

from _harness import (GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, needs_clone,
                      run_step)

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


if __name__ == '__main__':
    unittest.main()
