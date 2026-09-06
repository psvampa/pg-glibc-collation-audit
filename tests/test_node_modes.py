"""Layer 6: the two modes that read a NODE's own locale sources.

Both exist for one reason. A locale the distro BACKPORTS is in no upstream tag,
so no tag-to-tag diff can compare it however the tags are chosen --
localedata/locales/C above all, which is C.UTF-8, which is the database
collation almost everywhere initdb runs in a container. The node has that file;
the clone does not.

CI has no node, so a tag is materialised as a stand-in and the backported C is
fabricated. That is not a shortcut: the file being tested exists at no tag, so
writing it out is the only way to test the locale this project's first false
negative was about. What stays un-exercised is the transport off a real node,
the same position sql/ is in and recorded as such in tests/README.md.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _harness import (GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, backported_c,
                      needs_clone, upstream_c)

import diff_distro_locales as dd
import glibc_locale_data as g
from filter_lc_collate_changes import KNOWN_BACKPORTED

_TREES = {}


def tearDownModule():
    for path in _TREES.values():
        shutil.rmtree(os.path.dirname(os.path.dirname(path)),
                      ignore_errors=True)


def _tree(tag):
    """One materialised copy of a tag per module run; git archive is not free."""
    if tag not in _TREES:
        base = tempfile.mkdtemp(prefix='pg-glibc-tagtree-')
        _TREES[tag] = dd.materialise_tag(GLIBC_CLONE, tag, base)
    return _TREES[tag]


def flat(text):
    """Output with every run of whitespace collapsed to one space.

    dd.warn wraps at 78 columns, so a phrase of more than a few words is split
    across lines. Asserting on the raw text makes a POSITIVE assertion brittle
    and -- far worse -- makes a NEGATIVE one vacuous: `assertNotIn` on a phrase
    that is always broken up passes whether the warning is printed or not.
    Caught by exactly that, on 2026-09-06.
    """
    return ' '.join(text.split())


def run(script, *args, out_dir=None):
    """One script as a subprocess. Returns (exit code, stdout+stderr).

    Not _harness.run_step: that memoises on argv, and these tests deliberately
    re-run the same argv against a mutated directory.
    """
    env = dict(os.environ)
    if out_dir:
        env['PG_GLIBC_AUDIT_OUT'] = out_dir
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, script),
                        *args], cwd=SCRIPTS_DIR, env=env, capture_output=True)
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


class NodeCase(unittest.TestCase):
    """Scratch directories, and fake nodes built by copying a materialised tag."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-node-mode-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, 'out')

    def node(self, tag, name, extra=None, keep=None):
        """A stand-in node directory. `extra` writes files the tag has no idea
        about -- which is the whole point -- and `keep` truncates it."""
        dest = os.path.join(self.tmp, name)
        shutil.copytree(_tree(tag), dest)
        for fname, body in (extra or {}).items():
            with open(os.path.join(dest, fname), 'w', encoding='utf-8') as fh:
                fh.write(body)
        if keep is not None:
            for fname in os.listdir(dest):
                if fname not in keep:
                    os.remove(os.path.join(dest, fname))
        return dest

    def node_to_node(self, old_root, new_root, old_build='old-build',
                     new_build='new-build', *args):
        return run('diff_node_locales.py',
                   '--old-locales-dir', old_root, '--old-build-id', old_build,
                   '--new-locales-dir', new_root, '--new-build-id', new_build,
                   *args, out_dir=self.out)

    def result_names(self):
        """The names in the written result list, minus its provenance header."""
        paths = glob.glob(os.path.join(self.out, 'node_collate_diffs.*.txt'))
        self.assertEqual(len(paths), 1, f'expected one result file, got {paths}')
        with open(paths[0], encoding='utf-8') as fh:
            return sorted(ln.strip() for ln in fh
                          if ln.strip() and not ln.startswith('#'))


@needs_clone
class NodeToNodeReproducesStepTwo(NodeCase):
    """The equivalence that makes this mode trustworthy without a node.

    Step 2 reaches the same verdict by a completely different route -- diff
    hunks overlapped against the old side's LC_COLLATE line numbers -- so two
    tag trees compared as nodes must produce step 2's answer.
    """

    def test_two_tag_trees_reproduce_step_2(self):
        for pair, expected in (((OLD, MID), ['or_IN', 'sv_SE']),
                               ((MID, NEW), ['ber_DZ', 'kab_DZ', 'th_TH'])):
            with self.subTest(pair=pair):
                self.setUp()
                old_root = self.node(pair[0], 'a')
                new_root = self.node(pair[1], 'b')
                rc, text = self.node_to_node(old_root, new_root, *pair)
                self.assertEqual(rc, 0, text)
                self.assertEqual(self.result_names(), expected)


@needs_clone
class NodeToNodeSeesWhatNoTagCan(NodeCase):
    """"C.UTF-8 cannot be audited by this method" -- true of a tag diff, and
    the reason this mode exists. These are the tests that say the limitation is
    closed on the data half."""

    def test_a_backported_file_in_neither_tag_gets_a_verdict(self):
        old_root = self.node(OLD, 'a', extra={'C': backported_c()})
        new_root = self.node(MID, 'b', extra={'C': upstream_c()})
        rc, text = self.node_to_node(old_root, new_root, OLD, MID)
        self.assertEqual(rc, 0, text)
        self.assertIn('C', self.result_names())
        self.assertIn('ellipsis -> codepoint', text)

    def test_C_is_named_even_when_identical(self):
        """Absent is not empty. A run that says nothing about C.UTF-8 and a run
        that cleared it must not look the same on a terminal."""
        same = backported_c()
        old_root = self.node(OLD, 'a', extra={'C': same})
        new_root = self.node(MID, 'b', extra={'C': same})
        rc, text = self.node_to_node(old_root, new_root, OLD, MID)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('C', self.result_names())
        self.assertIn('C (C.UTF-8): present on both nodes, byte-identical', text)
        self.assertIn('identical data does NOT clear the order', flat(text))

    def test_the_ellipsis_shape_is_reported_for_both_sides(self):
        same = backported_c()
        rc, text = self.node_to_node(self.node(OLD, 'a', extra={'C': same}),
                                     self.node(MID, 'b', extra={'C': same}),
                                     'glibc-2.28-251.el8_10.40',
                                     'glibc-2.34-275.el9_8')
        self.assertEqual(rc, 0, text)
        self.assertIn('glibc-2.28-251.el8_10.40: ellipsis ranges', text)
        self.assertIn('glibc-2.34-275.el9_8: ellipsis ranges', text)

    def test_every_known_backported_name_is_reported(self):
        """One dict, two consumers. A name reported by step 2 and dropped here
        would be a locale nobody looks at twice."""
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     OLD, MID)
        self.assertEqual(rc, 0, text)
        for name, locale in KNOWN_BACKPORTED.items():
            self.assertIn(f'{name} ({locale})', text)

    def test_a_backported_locale_missing_from_the_nodes_says_so(self):
        """Neither materialised tag has C, and the answer must be "this says
        nothing about it" -- not silence."""
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     OLD, MID)
        self.assertEqual(rc, 0, text)
        self.assertIn('on NEITHER node', text)

    def test_the_in_neither_tag_annotation_names_C(self):
        old_root = self.node(OLD, 'a', extra={'C': backported_c()})
        new_root = self.node(MID, 'b', extra={'C': upstream_c()})
        # OLD and MID deliberately: C exists upstream from 2.35, so it is in
        # neither of these two -- which is exactly the RHEL8 -> RHEL9 case.
        rc, text = self.node_to_node(old_root, new_root, OLD, MID,
                                     '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertIn('exists at neither tag', text)
        self.assertIn('exist at NEITHER tag', text)

    def test_a_locale_only_on_one_node_is_framed_as_a_removal(self):
        old_root = self.node(OLD, 'a', extra={'zz_GONE': backported_c()})
        new_root = self.node(MID, 'b')
        rc, text = self.node_to_node(old_root, new_root, OLD, MID)
        self.assertEqual(rc, 0, text)
        self.assertIn('zz_GONE', text)
        self.assertIn('the collation is gone', text)


@needs_clone
class NodeToNodeRefusesToGuess(NodeCase):
    """The failure modes that report a flawless clean upgrade."""

    def test_both_directories_truncated_aborts(self):
        keep = sorted(os.listdir(_tree(OLD)))[:3]
        old_root = self.node(OLD, 'a', keep=keep)
        new_root = self.node(MID, 'b', keep=keep)
        rc, text = self.node_to_node(old_root, new_root)
        self.assertEqual(rc, 2, text)
        self.assertNotIn('differ INSIDE LC_COLLATE:   0', text)

    def test_comparing_a_directory_with_itself_is_refused(self):
        root = self.node(OLD, 'a')
        rc, text = self.node_to_node(root, os.path.join(root, '.'))
        self.assertEqual(rc, 2, text)
        self.assertIn('same directory', text)

    def test_equal_build_ids_warn_rather_than_abort(self):
        """A legitimate control run that proves nothing about an upgrade."""
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'same-build', 'same-build')
        self.assertEqual(rc, 0, text)
        self.assertIn('!!', text)
        self.assertIn('compares a build with itself', text)

    def test_each_missing_build_id_is_exit_2(self):
        old_root, new_root = self.node(OLD, 'a'), self.node(MID, 'b')
        for args in (('--old-locales-dir', old_root, '--new-locales-dir',
                      new_root, '--new-build-id', 'x'),
                     ('--old-locales-dir', old_root, '--old-build-id', 'x',
                      '--new-locales-dir', new_root)):
            with self.subTest(args=args):
                rc, text = run('diff_node_locales.py', *args, out_dir=self.out)
                self.assertEqual(rc, 2, text)

    def test_one_tag_without_the_other_is_refused(self):
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'x', 'y', '--old-tag', OLD)
        self.assertEqual(rc, 2, text)

    def test_both_build_ids_reach_the_output_and_the_filename(self):
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'glibc-2.28-251.el8_10.40',
                                     'glibc-2.34-275.el9_8')
        self.assertEqual(rc, 0, text)
        self.assertIn('glibc-2.28-251.el8_10.40', text)
        self.assertIn('glibc-2.34-275.el9_8', text)
        name = os.path.basename(glob.glob(
            os.path.join(self.out, 'node_collate_diffs.*.txt'))[0])
        self.assertEqual(name, 'node_collate_diffs.glibc-2.28-251.el8_10.40..'
                               'glibc-2.34-275.el9_8.txt')

    def test_the_result_file_carries_a_provenance_header(self):
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        path = glob.glob(os.path.join(self.out,
                                      'node_collate_diffs.*.txt'))[0]
        with open(path, encoding='utf-8') as fh:
            first = fh.readline()
        self.assertTrue(first.startswith('# build-A '), first)
        self.assertIn('build-B', first)

    def test_each_side_prints_a_manifest_fingerprint(self):
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'))
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.count('fingerprint '), 2, text)

    def test_a_skipped_entry_is_named_on_either_side(self):
        old_root = self.node(OLD, 'a')
        os.mkdir(os.path.join(old_root, 'a_subdir'))
        rc, text = self.node_to_node(old_root, self.node(MID, 'b'))
        self.assertEqual(rc, 0, text)
        self.assertIn('a_subdir', text)

    def test_the_caveat_never_reassures_about_a_locale_it_could_not_compare(self):
        """The regression the conditional warning introduced and this pins shut.

        `computed` is empty both when nothing is ellipsis-based AND when
        nothing could be compared at all, and collapsing the two printed "the
        data comparison is the whole story" directly under "this comparison
        says nothing about C.UTF-8". A reassurance over an absence is the exact
        false negative this script exists to remove.
        """
        # Neither materialised tag has C, so it is absent from both nodes.
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        self.assertIn('on NEITHER node', flat(text))
        self.assertNotIn('the data comparison is the whole story', flat(text))
        self.assertIn('NOT compared by this run', flat(text))

    def test_a_locale_on_one_node_only_is_not_reassured_about_either(self):
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'C': backported_c()}),
            self.node(MID, 'b'), 'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        self.assertIn('present on the old node', flat(text))
        self.assertNotIn('the data comparison is the whole story', flat(text))
        self.assertIn('NOT compared by this run', flat(text))

    def test_the_reassuring_branch_fires_only_when_it_was_actually_compared(self):
        same = upstream_c()
        rc, text = self.node_to_node(self.node(OLD, 'a', extra={'C': same}),
                                     self.node(MID, 'b', extra={'C': same}),
                                     'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        self.assertIn('the data comparison is the whole story', flat(text))
        self.assertNotIn('NOT compared by this run', flat(text))

    def test_the_caveat_does_not_assert_a_shape_it_did_not_see(self):
        """It used to say "C.UTF-8, whose backported source IS built from
        ellipsis ranges" on every run -- including the RHEL9 -> RHEL10 run
        whose own output a few lines above reports codepoint_collation on both
        nodes. A warning that contradicts the output beside it is a warning
        nobody believes twice."""
        same = upstream_c()
        rc, text = self.node_to_node(self.node(OLD, 'a', extra={'C': same}),
                                     self.node(MID, 'b', extra={'C': same}),
                                     'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        self.assertIn('codepoint_collation', flat(text))
        self.assertNotIn('built from ellipsis ranges', flat(text))
        self.assertIn('c_utf8_probe.sql', flat(text))

    def test_the_caveat_names_the_locale_when_it_IS_ellipsis_based(self):
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'C': backported_c()}),
            self.node(MID, 'b', extra={'C': upstream_c()}),
            'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        self.assertIn('C.UTF-8: built from ellipsis ranges on at least one',
                      flat(text))

    def test_the_data_only_caveat_is_printed(self):
        """Data equality is not order equality: Bug 22668 reordered ko_KR from
        a byte-identical file. Without this the mode's cleanest result is also
        its most misleading."""
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'))
        self.assertEqual(rc, 0, text)
        self.assertIn('!!', text)
        self.assertIn('22668', text)
        self.assertIn('c_utf8_probe.sql', text)
        self.assertIn('charmaps', text)


@needs_clone
class DirectoryModeStepFour(NodeCase):
    """Step 4 over a node's directory. The only way it sees a backported
    locale -- and the step whose empty output is its most reassuring."""

    def test_a_materialised_tag_reproduces_the_tag_answer(self):
        """The equivalence that validates the directory loader against the git
        one: same corpus, same answer, two different readers."""
        rc_tag, tag_text = run('flag_algorithmic_ranges.py', MID,
                               out_dir=self.out)
        self.assertEqual(rc_tag, 0, tag_text)
        rc_dir, dir_text = run('flag_algorithmic_ranges.py',
                               '--locales-dir', self.node(MID, 'n'),
                               '--build-id', 'fake-build',
                               '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc_dir, 0, dir_text)
        for line in ('of which 342 define LC_COLLATE',
                     'ellipsis (algorithmic) ranges: 4'):
            self.assertIn(line, tag_text)
            self.assertIn(line, dir_text)
        self.assertIn('335 locale source file(s), 478 generated', tag_text)
        self.assertIn('335 locale source file(s), 478 generated', dir_text)

    def test_a_backported_C_in_the_directory_is_flagged(self):
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n',
                                                  extra={'C': backported_c()}),
                       '--build-id', 'glibc-2.28-251.el8_10.40',
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('ranges: 5', text)
        with open(os.path.join(
                self.out,
                'step4_exposed_locales.glibc-2.28-251.el8_10.40.txt'),
                encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_codepoint_collation_C_is_reported_not_silently_unflagged(self):
        """Unflagged and cleared look identical on a terminal. byte-order-by-
        construction is a positive statement and gets said out loud."""
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n',
                                                  extra={'C': upstream_c()}),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('Declare codepoint_collation', text)
        self.assertIn('ranges: 4', text)

    def test_the_upstream_C_is_reported_in_tag_mode_too(self):
        rc, text = run('flag_algorithmic_ranges.py', NEW, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('Declare codepoint_collation, so no expansion change '
                      'can move them: C', text)

    def test_a_truncated_directory_cannot_report_no_ellipsis_locales(self):
        """A node without glibc-locale-source presents an empty directory, and
        an empty scan prints "steps 1-3 are sufficient"."""
        keep = sorted(os.listdir(_tree(MID)))[:3]
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n', keep=keep),
                       '--build-id', 'truncated', out_dir=self.out)
        self.assertEqual(rc, 2, text)
        self.assertNotIn('No locale uses ellipsis ranges', text)

    def test_the_directory_run_does_not_clobber_the_tag_list(self):
        tag_list = os.path.join(self.out, 'step4_exposed_locales.txt')
        rc, text = run('flag_algorithmic_ranges.py', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        with open(tag_list, encoding='utf-8') as fh:
            before = fh.read()
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(OLD, 'n'),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        with open(tag_list, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), before)
        self.assertTrue(os.path.exists(
            os.path.join(self.out, 'step4_exposed_locales.fake.txt')))

    def test_a_missing_build_id_is_exit_2(self):
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n'), out_dir=self.out)
        self.assertEqual(rc, 2, text)

    def test_a_tag_and_a_directory_together_are_refused(self):
        rc, text = run('flag_algorithmic_ranges.py', MID,
                       '--locales-dir', self.node(MID, 'n'),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 2, text)

    def test_neither_a_tag_nor_a_directory_is_refused(self):
        rc, text = run('flag_algorithmic_ranges.py', out_dir=self.out)
        self.assertEqual(rc, 2, text)


if __name__ == '__main__':
    unittest.main()
