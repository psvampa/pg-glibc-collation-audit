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
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from _harness import (GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, backported_c,
                      flat, locale_file, needs_clone, upstream_c)

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


def with_edited_template(root, name='iso14651_t1'):
    """Edit one line inside `name`'s LC_COLLATE block in a stand-in node --
    the shape of a distro backport to a template."""
    path = os.path.join(root, name)
    with open(path, encoding='utf-8', errors='surrogateescape') as fh:
        text = fh.read()
    start, end = g.collate_bounds(text)
    lines = text.split('\n')
    lines.insert(start, '% injected by the test suite: a backport to a template')
    with open(path, 'w', encoding='utf-8', errors='surrogateescape') as fh:
        fh.write('\n'.join(lines))
    return root


@needs_clone
class NodeChecksCloseOverCopy(NodeCase):
    """"Node-to-node and the distro check did not close over the copy graph":
    a template differing between nodes was "1 locale(s) differ", and the
    hundreds of locales copying it were nowhere -- in the one check that can
    see a backport at all. Injected, as every closure test is: no fixture
    node carries a backport inside LC_COLLATE."""

    def test_node_to_node_reports_the_reach_of_a_differing_template(self):
        old_root = self.node(MID, 'a')
        new_root = with_edited_template(self.node(NEW, 'b'))
        rc, text = self.node_to_node(old_root, new_root, MID, NEW)
        self.assertEqual(rc, 0, text)
        self.assertIn('iso14651_t1', self.result_names())
        m = re.search(r'^Additionally affected via `copy` inheritance at '
                      + re.escape(NEW) + r': (\d+) locale\(s\)$', text, re.M)
        self.assertIsNotNone(m, text)
        self.assertGreater(int(m.group(1)), 300)
        self.assertIn('via iso14651_t1:', text)
        listed = glob.glob(os.path.join(self.out, 'node_collate_inherited.*.txt'))
        self.assertEqual(len(listed), 1, listed)
        with open(listed[0], encoding='utf-8') as fh:
            names = [ln.strip() for ln in fh if ln.strip()
                     and not ln.startswith('#')]
        self.assertEqual(len(names), int(m.group(1)))
        self.assertIn('en_US', names)

    def test_a_differing_leaf_reports_its_small_reach(self):
        """The closure is printed for every non-empty differing list, however
        small; a zero would be printed as a zero, never omitted."""
        old_root = self.node(OLD, 'a')
        new_root = self.node(MID, 'b')
        rc, text = self.node_to_node(old_root, new_root, OLD, MID)
        self.assertEqual(rc, 0, text)
        # or_IN and sv_SE differ; sv_FI and sv_FI@euro copy sv_SE -- the same
        # answer step 3 gives for this pair, reached from the node side.
        self.assertIn(f'Additionally affected via `copy` inheritance at '
                      f'{MID}: 2 locale(s)', text)
        self.assertIn('via sv_SE: 2 locale(s)', text)
        self.assertIn('sv_FI, sv_FI@euro', text)

    def test_the_distro_check_reports_the_reach_too(self):
        node = with_edited_template(self.node(MID, 'n'))
        rc, text = run('diff_distro_locales.py', MID, '--locales-dir', node,
                       '--build-id', 'edited-build', '--node-label', 'n',
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        m = re.search(r'Additionally affected via `copy` inheritance at '
                      r'edited-build: (\d+) locale\(s\)', text)
        self.assertIsNotNone(m, text)
        self.assertGreater(int(m.group(1)), 300)


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

    def test_the_written_list_is_never_narrower_than_what_was_reported(self):
        """The file the output calls the "full list", under the flag audit.sh
        always passes.

        Mapping the exposed set through the TAG's SUPPORTED and writing only
        the mapped names dropped every locale that tag does not build -- C
        first among them, on the node where C.utf8 is the collation initdb
        picked and `locale -a` does build it (measured on collaudit8,
        glibc-2.28-251.el8_10.40, 2026-09-07: 867 locales, C.utf8 among them).
        The reported set and the written set are one set.
        """
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n',
                                                  extra={'C': backported_c()}),
                       '--build-id', 'glibc-2.28-251.el8_10.40',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        counts = re.search(r'(\d+) locale source file\(s\), (\d+) generated '
                           r'locale name\(s\)', text)
        self.assertIsNotNone(counts, text)
        unbuilt = re.search(r"^  not in \S+ SUPPORTED[^:]*: (.+)$", text, re.M)
        self.assertIsNotNone(unbuilt, text)
        unbuilt_names = unbuilt.group(1).split(', ')
        with open(os.path.join(
                self.out,
                'step4_exposed_locales.glibc-2.28-251.el8_10.40.txt'),
                encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertIn('C', written)
        for name in unbuilt_names:
            self.assertIn(name, written)
        self.assertEqual(len(written),
                         int(counts.group(2)) + len(unbuilt_names))

    def test_a_locale_the_node_builds_is_not_called_an_unbuilt_template(self):
        """"Not in SUPPORTED" is a fact about the TAG, printed as a fact about
        the NODE: C was labelled a template not built by default beside a node
        that builds it and runs its databases on it."""
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n',
                                                  extra={'C': backported_c()}),
                       '--build-id', 'glibc-2.28-251.el8_10.40',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn("the node's `locale -a` is the authority", flat(text))
        self.assertNotIn('templates, not built by default', flat(text))

    def test_each_backported_locale_gets_a_declared_status(self):
        """Absent, cleared and unexamined are three answers; the wrapper could
        distinguish two. It grepped the ellipsis list, then the codepoint line,
        and printed NOTHING about C when it was neither -- which is what a run
        that never looked also prints. The scan declares a status per locale
        the distro is known to backport, and the wrapper reads it.
        """
        cases = ((backported_c(), 'C (C.UTF-8): ellipsis-based'),
                 (upstream_c(), 'C (C.UTF-8): codepoint_collation'),
                 (locale_file('order_start forward',
                              '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                              'order_end'),
                  'C (C.UTF-8): explicit weights'),
                 (locale_file('copy "iso14651_t1"'),
                  'C (C.UTF-8): copy-only'),
                 (locale_file('copy "iso14651_t1"'),
                  'it copies iso14651_t1, which this step flagged -- so this '
                  'locale IS exposed'),
                 (None, 'C (C.UTF-8): ABSENT from this directory'))
        for i, (body, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                extra = {'C': body} if body is not None else None
                rc, text = run('flag_algorithmic_ranges.py',
                               '--locales-dir',
                               self.node(MID, f'declared{i}', extra=extra),
                               '--build-id', 'fake', out_dir=self.out)
                self.assertEqual(rc, 0, text)
                self.assertIn(expected, text)

    def test_a_copy_only_C_is_not_declared_clear_of_an_ellipsis_it_inherits(self):
        """A `C` that copies `iso14651_t1` uses no ellipsis of its own and is
        exposed by every weight that template computes. Reporting only its own
        style is true of the file and false of the order -- and step 4 has the
        closure in hand when it declares, so there is no excuse for the
        summary to be told less than the step knows."""
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'copyc',
                                 extra={'C': locale_file('copy "iso14651_t1"')}),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('so this locale IS exposed', flat(text))
        with open(os.path.join(self.out,
                               'step4_exposed_locales.fake.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_codepoint_C_is_not_called_exposed_by_a_copy_it_discards(self):
        """The control on the line above: `codepoint_collation` discards all
        collation information, inherited included, so a copy cannot expose it.
        A fix that appended the exposure note unconditionally would clear
        nothing and alarm about a locale glibc has already settled."""
        body = upstream_c().replace('\ncodepoint_collation\n',
                                    '\ncopy "iso14651_t1"\ncodepoint_collation\n')
        self.assertIn('copy "iso14651_t1"', body)
        self.assertEqual(body.count('codepoint_collation'), 2)  # prose + keyword
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'cpc', extra={'C': body}),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('C (C.UTF-8): codepoint_collation', text)
        self.assertNotIn('so this locale IS exposed', flat(text))

    def test_a_copy_target_absent_from_the_corpus_is_named_not_followed(self):
        """`inherited_from` treats an unknown target as a leaf, so a copy the
        walk could not follow used to end in the same sentence as a copy
        resolved to a clear file. What that target carries was never read: it
        is unresolved, not clear. Measured 0 dangling targets at 2.28, 2.34 and
        2.39 and on the three RHEL fixtures, so this is reachable only by a
        directory that is not the closed source a node built from -- which is
        the input the ABSENT wording already contemplates."""
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'dangling',
                                 extra={'C': locale_file('copy "no_such_locale"'),
                                        'sv_SE': locale_file('copy "no_such_locale"')}),
                       '--build-id', 'fake', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('no_such_locale', text)
        self.assertIn('so this locale is NOT cleared', flat(text))
        # Named, not just counted, and under the spelling pg_collation shows:
        # a reader greps this list for the collation their database uses.
        self.assertIn('sv_SE', flat(text))
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.txt'),
                  encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh]
        self.assertIn('C', written)
        self.assertIn('sv_SE.utf8', written)

    def test_an_absent_copy_target_is_not_cleared_by_an_empty_ellipsis_scan(self):
        """The two reassuring things at once: nothing readable uses an
        ellipsis, and the one file that might have is the one the corpus does
        not contain. "steps 1-3 are sufficient" must not be what comes out."""
        flat_body = locale_file('order_start forward',
                                '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                'order_end')
        extra = {name: flat_body for name in
                 ('i18n', 'iso14651_t1', 'iso14651_t1_common', 'ko_KR')}
        extra['C'] = locale_file('copy "no_such_locale"')
        extra['sv_SE'] = locale_file('copy "no_such_locale"')
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'flatdangle', extra=extra),
                       '--build-id', 'fake', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('steps 1-3 are sufficient', flat(text))
        self.assertIn('steps 1-3 are NOT sufficient for those', flat(text))
        self.assertIn('so this locale is NOT cleared', flat(text))
        # Opened at the path the output NAMES, not at one this test knows:
        # announcing a file nobody wrote sends the reader to "No such file",
        # and a test that looks elsewhere cannot tell.
        named = re.search(r'full list \((\d+) name\(s\)\): (\S+)', text)
        self.assertIsNotNone(named, text)
        with open(named.group(2), encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertIn('C', written)
        # This path maps through SUPPORTED too, and had no test saying so.
        self.assertIn('sv_SE.utf8', written)
        self.assertEqual(int(named.group(1)), len(written))

    def test_a_codepoint_C_is_not_called_unresolved_by_a_copy_it_discards(self):
        """The control the exposure note has and this one lacked:
        `codepoint_collation` discards inherited collation information, so a
        copy it cannot resolve cannot leave it unresolved either."""
        body = upstream_c().replace('\ncodepoint_collation\n',
                                    '\ncopy "no_such_locale"\ncodepoint_collation\n')
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'cpdangle', extra={'C': body}),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('C (C.UTF-8): codepoint_collation', text)
        self.assertNotIn('so this locale is NOT cleared', flat(text))

    def test_a_corpus_with_no_collation_block_at_all_is_refused(self):
        """The file-count floor asks whether enough files were read. This asks
        whether any of them turned out to be a locale: a full corpus that
        yields no LC_COLLATE block is a reader problem, and every sentence
        below it would be the cleanest this step can print."""
        tree = _tree(MID)
        blockless = {name: open(os.path.join(tree, name), encoding='utf-8',
                                errors='surrogateescape').read()
                     .split('LC_COLLATE')[0]
                     for name in os.listdir(tree)}
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'noblocks', extra=blockless),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 2, text)
        self.assertNotIn('No locale uses ellipsis ranges', text)
        self.assertIn('not one defines LC_COLLATE', flat(text))

    def test_a_directory_without_any_ellipsis_still_declares_C(self):
        """The most reassuring output this step has -- "No locale uses ellipsis
        ranges here; steps 1-3 are sufficient" -- returned before the
        declaration, so the wrapper printed NOT DECLARED over a scan that had
        looked and found an answer. Every path declares."""
        flat_body = locale_file('order_start forward',
                                '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                'order_end')
        extra = {name: flat_body for name in
                 ('i18n', 'iso14651_t1', 'iso14651_t1_common', 'ko_KR')}
        extra['C'] = upstream_c()
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'flat', extra=extra),
                       '--build-id', 'fake', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('No locale uses ellipsis ranges here', text)
        self.assertIn('C (C.UTF-8): codepoint_collation', text)

    def test_the_tag_scan_declares_no_backported_status(self):
        """A tag has no distro backports by construction, and a heading that
        appeared there would invite the reader to trust a tag scan on the one
        question it structurally cannot answer."""
        rc, text = run('flag_algorithmic_ranges.py', NEW, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('C (C.UTF-8):', text)

if __name__ == '__main__':
    unittest.main()
