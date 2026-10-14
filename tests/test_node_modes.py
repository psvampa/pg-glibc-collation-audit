"""Layer 6: the two modes that read a NODE's own locale sources.

Both exist for one reason. A locale the distro BACKPORTS may be in neither tag
of the pair, and then no tag-to-tag diff can compare it --
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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _harness import (GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, backported_c,
                      default_weight_section, flat, locale_file, needs_clone,
                      swap_alpha_beta, tag_charmaps, upstream_c)

import diff_distro_locales as dd
import glibc_locale_data as g
from filter_lc_collate_changes import KNOWN_BACKPORTED

_TREES = {}


def tearDownModule():
    for path in _TREES.values():
        shutil.rmtree(os.path.dirname(os.path.dirname(path)),
                      ignore_errors=True)
    for path in _CHARMAPS.values():
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


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


@needs_clone
class NodeToNodeWritesWhatTheUpgradeRemoves(NodeCase):
    """The summary never named a locale the upgrade removes,
    because the only place step 8 said it was its own output. It now writes
    two lists the summary reads -- what is removed, and what it cannot say
    either way -- and these tests are the step's half of that."""

    def listed(self, stem):
        """(header, entries) of the one `stem`.*.txt this run wrote."""
        paths = glob.glob(os.path.join(self.out, f'{stem}.*.txt'))
        self.assertEqual(len(paths), 1, f'expected one {stem} list: {paths}')
        with open(paths[0], encoding='utf-8') as fh:
            lines = [ln.rstrip('\n') for ln in fh if ln.strip()]
        self.assertTrue(lines and lines[0].startswith('# '),
                        f'{stem}: no provenance header: {lines[:2]}')
        return lines[0], lines[1:]

    def without(self, tag, name, drop):
        """A stand-in node for `tag` that lacks `drop` -- a copy that lost it."""
        return self.node(tag, name, keep=set(os.listdir(_tree(tag))) - {drop})

    def test_both_lists_are_written_when_nothing_is_removed(self):
        """Absent is not empty: 2.28 to 2.34 removes no file, and both lists
        must still exist, or the summary could not tell "none" from "step 8
        never got here"."""
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'build-A', 'build-B',
                                     '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        header, removed = self.listed('node_removed_locales')
        self.assertEqual(removed, [])
        self.assertIn('build-A', header)
        self.assertIn('build-B', header)
        _, undetermined = self.listed('node_removed_undetermined')
        self.assertEqual(undetermined, [])
        self.assertNotIn('Undetermined (', text)

    def test_a_file_only_on_the_old_copy_is_listed_as_removed(self):
        """The en_US@ampm shape: on the old node, in no tag."""
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'zz_GONE': backported_c()}),
            self.node(MID, 'b'), 'build-A', 'build-B',
            '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_locales')[1], ['zz_GONE'])
        self.assertEqual(self.listed('node_removed_undetermined')[1], [])

    def test_the_real_removal_between_the_published_tags_is_listed(self):
        """The control for the next test: complete copies of 2.34 and 2.39
        remove aa_ER@saaho, and nothing is undetermined."""
        rc, text = self.node_to_node(self.node(MID, 'a'), self.node(NEW, 'b'),
                                     'build-A', 'build-B',
                                     '--old-tag', MID, '--new-tag', NEW)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_locales')[1],
                         ['aa_ER@saaho'])
        self.assertEqual(self.listed('node_removed_undetermined')[1], [])

    def test_a_file_of_the_old_tag_that_neither_copy_holds_is_undetermined(self):
        """A copy of 2.34 that lost aa_ER@saaho removes nothing on paper, and
        that "nothing" would be printed above step 6's `!!` naming the file.
        Checked against the old tag, so it needs no list of what distros ship.
        """
        rc, text = self.node_to_node(self.without(MID, 'a', 'aa_ER@saaho'),
                                     self.node(NEW, 'b'), 'build-A', 'build-B',
                                     '--old-tag', MID, '--new-tag', NEW)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_locales')[1], [])
        header, undetermined = self.listed('node_removed_undetermined')
        self.assertEqual(undetermined,
                         [f'aa_ER@saaho: in {MID}, and in neither copy'])
        self.assertIn(f'checked against {MID}', header)
        self.assertIn('Undetermined (1) -- this comparison cannot say whether '
                      'the upgrade removes these:', text)

    def test_without_the_tags_the_list_says_what_it_did_not_check(self):
        """No tag, no way to know what the old copy should hold. The list
        says so rather than reading as a checked empty one."""
        rc, text = self.node_to_node(self.without(MID, 'a', 'aa_ER@saaho'),
                                     self.node(NEW, 'b'), 'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        header, undetermined = self.listed('node_removed_undetermined')
        self.assertEqual(undetermined, [])
        self.assertIn('NOT checked against a tag', header)

    def test_a_backported_locale_on_the_old_copy_only_is_not_called_removed(self):
        """Step 8's own verdict on C there is "present on the old node ONLY
        ... test it empirically" -- not examined. The summary must not relay
        it as a collation that is gone."""
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'C': backported_c()}),
            self.node(MID, 'b'), 'build-A', 'build-B',
            '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_locales')[1], [])
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         ['C: backported (C.UTF-8), on the old node only: '
                          'not examined'])

    def test_an_entry_the_old_copy_holds_and_did_not_read_is_undetermined(self):
        """A symlink on the old side is skipped, so it is in neither side's
        names and could never appear as removed."""
        old_root = self.node(OLD, 'a')
        os.symlink('en_US', os.path.join(old_root, 'zz_LINK'))
        rc, text = self.node_to_node(old_root, self.node(MID, 'b'),
                                     'build-A', 'build-B',
                                     '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         ['zz_LINK: on the old side and not read (symlink), '
                          'and not on the new side'])

    def test_an_entry_neither_side_read_is_undetermined_and_says_so(self):
        """The same symlink on both sides: this step read neither, so it can
        say nothing about either. This test used to assert the opposite --
        a new-side entry counted as present -- and that is what printed
        "none" over a file step 7 named as not read (false-negative-reviewer,
        round 2)."""
        old_root, new_root = self.node(OLD, 'a'), self.node(MID, 'b')
        for root in (old_root, new_root):
            os.symlink('en_US', os.path.join(root, 'zz_LINK'))
        rc, text = self.node_to_node(old_root, new_root, 'build-A', 'build-B',
                                     '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         ['zz_LINK: on the old side and not read (symlink), '
                          'and on the new side but not read (symlink)'])

    def dangling(self, root, name):
        """Replace `name` in a stand-in node by a symlink to nothing."""
        os.remove(os.path.join(root, name))
        os.symlink('nowhere', os.path.join(root, name))
        return root

    def test_a_tag_file_only_the_new_side_holds_unread_is_undetermined(self):
        """th_TH gone from the old copy and a dangling symlink in the new: in
        neither copy as a file this step read, so not "none"."""
        rc, text = self.node_to_node(
            self.without(OLD, 'a', 'th_TH'),
            self.dangling(self.node(MID, 'b'), 'th_TH'), 'build-A', 'build-B',
            '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         [f'th_TH: in {OLD}, not in the old copy, and on the '
                          f'new side but not read (symlink)'])

    def test_a_file_the_new_side_holds_unread_is_not_called_gone(self):
        """Read on the old side, a symlink on the new: gone if it dangles, not
        if it resolves on the machine, and this step cannot tell which."""
        rc, text = self.node_to_node(
            self.node(OLD, 'a'),
            self.dangling(self.node(MID, 'b'), 'th_TH'), 'build-A', 'build-B',
            '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_locales')[1], [])
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         ['th_TH: read on the old side, and on the new side '
                          'but not read (symlink)'])

    def test_a_name_ending_in_a_newline_stays_one_line(self):
        """SAFE_NAME's `$` also matches before a final newline, so "x\\n"
        is read as a locale and "zz\\n" passed the quoting check: each was
        written as two lines of its list."""
        old_root = self.node(OLD, 'a', extra={'x\n': 'a file'})
        os.symlink('en_US', os.path.join(old_root, 'zz\n'))
        rc, text = self.node_to_node(old_root, self.node(MID, 'b'),
                                     'build-A', 'build-B',
                                     '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_locales')[1],
                         [repr('x\n')])
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         [f"{repr('zz' + chr(10))}: on the old side and not "
                          f"read (symlink), and not on the new side"])

    def test_a_dotfile_in_the_old_copy_is_not_undetermined(self):
        """Finder writes .DS_Store into any directory it opens, and no locale
        is named with a leading dot: one in the old copy alone must not turn
        the summary's "none" into an undetermined line."""
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'.DS_Store': 'finder'}),
            self.node(MID, 'b'), 'build-A', 'build-B',
            '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertIn('.DS_Store  (dotfile)', text)
        self.assertEqual(self.listed('node_removed_undetermined')[1], [])

    def test_an_unsafe_name_is_quoted_so_it_cannot_read_as_the_header(self):
        """An editor's #en_US# is skipped as unsafe. Written bare, its line
        began with `#`, the summary took it for the list's header, and the
        undetermined entry vanished under a "none"."""
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'#en_US#': 'autosave'}),
            self.node(MID, 'b'), 'build-A', 'build-B',
            '--old-tag', OLD, '--new-tag', MID)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.listed('node_removed_undetermined')[1],
                         ["'#en_US#': on the old side and not read (unsafe "
                          "name), and not on the new side"])


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
class NodeChecksSeeTheReadingCharacters(NodeCase):
    """Backlog 13.7. The same LC_COLLATE text read with another comment
    character is other rules, and the directive sits outside the block, so
    comparing blocks alone filed the file under "differ outside LC_COLLATE".
    One file of a tag copy is edited by its first line only."""

    def edited(self, name):
        root = self.node(MID, name)
        path = os.path.join(root, 'sv_SE')
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        self.assertTrue(text.startswith('comment_char %\n'))
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('comment_char %', 'comment_char #', 1))
        return root

    def test_node_to_node(self):
        rc, text = self.node_to_node(self.node(MID, 'a'), self.edited('b'),
                                     'build-a', 'build-b')
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.result_names(), ['sv_SE'])
        self.assertIn('differ INSIDE LC_COLLATE:   1', text)
        self.assertIn('sv_SE  (both, read differently', text)
        self.assertIn('read with comment_char % and escape_char / at build-a',
                      text)
        self.assertIn('read with comment_char # and escape_char / at build-b',
                      text)
        self.assertNotIn('No locale present on both nodes differs',
                         flat(text))

    def test_the_distro_check(self):
        rc, text = run('diff_distro_locales.py', MID, '--locales-dir',
                       self.edited('n'), '--build-id', 'edited-build',
                       '--node-label', 'n', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('differ INSIDE LC_COLLATE:  1', text)
        self.assertIn('sv_SE  (both, read differently)', text)
        self.assertIn(f'read with comment_char % and escape_char / at {MID}',
                      text)
        self.assertIn('read with comment_char # and escape_char / at node',
                      text)
        self.assertNotIn(f'No locale compared with {MID} differs', flat(text))

    def test_unedited_copies_say_nothing_differs_among_what_was_compared(self):
        """The control for the two above, and the line's scope. Steps 6/7
        and 8 print their clean line under counts that can hold files they
        never compared (absent on the node, on one node only), so the line
        names what it covers: printed bare, "Nothing differs inside
        LC_COLLATE" read as covering a copy that lost 140 files
        (false-negative-reviewer, 2026-10-12)."""
        rc, text = self.node_to_node(self.node(MID, 'a'), self.node(MID, 'b'),
                                     'build-a', 'build-b')
        self.assertEqual(rc, 0, text)
        self.assertIn('No locale present on both nodes differs inside '
                      'LC_COLLATE.', flat(text))
        rc, text = run('diff_distro_locales.py', MID, '--locales-dir',
                       self.node(MID, 'n'), '--build-id', 'unedited-build',
                       '--node-label', 'n', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn(f'No locale compared with {MID} differs inside '
                      f'LC_COLLATE.', flat(text))


@needs_clone
class NodeToNodeRefusesToGuess(NodeCase):
    """The failure modes that report a flawless clean upgrade."""

    def test_both_directories_truncated_aborts(self):
        keep = sorted(os.listdir(_tree(OLD)))[:3]
        old_root = self.node(OLD, 'a', keep=keep)
        new_root = self.node(MID, 'b', keep=keep)
        rc, text = self.node_to_node(old_root, new_root)
        self.assertEqual(rc, 2, text)
        self.assertIn('below the floor', flat(text))

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

    def test_a_reversed_tag_pair_is_refused(self):
        """The two tags decide which side's SUPPORTED maps which node's
        names, so a reversed pair labels the older node as the new one. This
        guard had no test: deleting the call left every test in this file
        green, because no other assertion here depends on the tags' order."""
        rc, text = self.node_to_node(self.node(OLD, 'a'), self.node(MID, 'b'),
                                     'x', 'y', '--old-tag', MID,
                                     '--new-tag', OLD)
        self.assertEqual(rc, 2, text)
        self.assertIn('REVERSED', text)
        self.assertNotIn('exists at neither tag', text)

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
        self.assertNotIn('so their data leaves localedef nothing to compute',
                         flat(text))
        self.assertIn('NOT compared by this run', flat(text))

    def test_a_locale_on_one_node_only_is_not_reassured_about_either(self):
        rc, text = self.node_to_node(
            self.node(OLD, 'a', extra={'C': backported_c()}),
            self.node(MID, 'b'), 'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        self.assertIn('present on the old node', flat(text))
        self.assertNotIn('so their data leaves localedef nothing to compute',
                         flat(text))
        self.assertIn('NOT compared by this run', flat(text))

    def test_the_reassuring_branch_fires_only_when_it_was_actually_compared(self):
        same = upstream_c()
        rc, text = self.node_to_node(self.node(OLD, 'a', extra={'C': same}),
                                     self.node(MID, 'b', extra={'C': same}),
                                     'build-A', 'build-B')
        self.assertEqual(rc, 0, text)
        # The reason as well as the verdict: "no backported locale here is
        # ellipsis-based" was the reason, and backlog 1.6 made it false.
        # Not "the whole story" since backlog 13.7: changed code can move a
        # byte-order locale, and step 5 is what says whether it changed.
        self.assertIn('Every backported locale here declares '
                      'codepoint_collation on both nodes, so their data leaves '
                      "localedef nothing to compute; whether changed code "
                      "moves their order is step 5's question.", flat(text))
        self.assertNotIn('whole story', flat(text))
        self.assertNotIn('NOT compared by this run', flat(text))
        self.assertNotIn('no ellipsis range on either node', flat(text))

    def test_no_ellipsis_is_not_the_whole_story_without_codepoint(self):
        """A C with explicit weights, or one that only copies, uses no
        ellipsis range and still takes localedef's default weight for every
        character it does not list (backlog 1.6). "The data comparison is the
        whole story" was printed for both, which the default weight makes
        false. A C with no LC_COLLATE block at all gets
        neither line: nothing measured says what localedef builds for it."""
        explicit = locale_file('order_start forward',
                               '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                               'order_end')
        no_block = ('comment_char %\nescape_char /\n\nLC_CTYPE\n'
                    'copy "i18n"\nEND LC_CTYPE\n')
        copy_only = locale_file('copy "iso14651_t1"')
        # The last case: codepoint_collation on one node only is not settled.
        cases = (('explicit', explicit, explicit, True),
                 ('copy-only', copy_only, copy_only, True),
                 ('none', no_block, no_block, False),
                 ('codepoint-none', upstream_c(), no_block, False))
        for name, old_body, new_body, warned in cases:
            with self.subTest(case=name):
                rc, text = self.node_to_node(
                    self.node(OLD, f'{name}-a', extra={'C': old_body}),
                    self.node(MID, f'{name}-b', extra={'C': new_body}),
                    'build-A', 'build-B')
                self.assertEqual(rc, 0, text)
                self.assertNotIn('so their data leaves localedef nothing to '
                                 'compute', flat(text))
                self.assertEqual('C.UTF-8: no ellipsis range on either node, '
                                 "but every character the file does not list "
                                 "takes localedef's default weight, so the "
                                 'order is NOT settled by comparing the data '
                                 'either.' in flat(text), warned)

    def test_the_keyword_beside_anything_else_is_not_the_whole_story(self):
        """Backlog 13.2. `copy "iso14651_t1"` plus codepoint_collation
        compiles on RHEL9 and RHEL10 into tables whose strcoll returns garbage
        and crashes (glibc study, E5), and it read here as codepoint_collation
        on both nodes, so "the data comparison is the whole story". It gets a
        warning of its own, which names the reason, on one node or on both,
        and either node may be the one (round 2 of false-negative-reviewer
        found the old-only side untested); the default-weight warning gives a
        reason that is not this one."""
        mixed = locale_file('copy "iso14651_t1"', 'codepoint_collation')
        for name, old_body, new_body, side in (
                ('both', mixed, mixed, 'build-B'),
                ('new only', upstream_c(), mixed, 'build-B'),
                ('old only', mixed, upstream_c(), 'build-A')):
            with self.subTest(case=name):
                slug = name.replace(' ', '-')
                rc, text = self.node_to_node(
                    self.node(OLD, f'{slug}-a', extra={'C': old_body}),
                    self.node(MID, f'{slug}-b', extra={'C': new_body}),
                    'build-A', 'build-B')
                self.assertEqual(rc, 0, text)
                self.assertNotIn('so their data leaves localedef nothing to '
                                 'compute', flat(text))
                self.assertIn('C.UTF-8: names codepoint_collation on at least '
                              'one of these nodes, but could not be read as '
                              'the keyword alone in its LC_COLLATE, the one '
                              'form glibc builds in byte order.', flat(text))
                self.assertIn(f'{side}: names codepoint_collation, but could '
                              'not be read as the keyword alone -- NOT '
                              'cleared', flat(text))
                self.assertNotIn('no ellipsis range on either node',
                                 flat(text))

    def test_a_C_that_only_copies_a_byte_order_locale_is_settled_data(self):
        """Backlog 6.9 in step 8. A C whose LC_COLLATE copies a locale holding
        the keyword alone, and nothing else, is byte order (measured on RHEL9
        and RHEL10: byte-identical to the installed C.UTF-8). It took the
        default-weight warning, whose reason is false there, while step 4
        named it byte order. The closing line says how it got there. The
        control: on the node where the copied locale is not the keyword
        alone, it is not settled."""
        copy_c = locale_file('copy "zz_ZZ"')
        mixed = locale_file('copy "iso14651_t1"', 'codepoint_collation')
        for name, new_target, settled in (('alone', upstream_c(), True),
                                          ('control', mixed, False)):
            with self.subTest(case=name):
                rc, text = self.node_to_node(
                    self.node(OLD, f'{name}-a',
                              extra={'C': copy_c, 'zz_ZZ': upstream_c()}),
                    self.node(MID, f'{name}-b',
                              extra={'C': copy_c, 'zz_ZZ': new_target}),
                    'build-A', 'build-B')
                self.assertEqual(rc, 0, text)
                self.assertIn('build-A: pure copy; inherits its order, of '
                              'zz_ZZ, which glibc builds in byte order',
                              flat(text))
                self.assertEqual(
                    'Every backported locale here declares codepoint_collation '
                    'alone, or copies nothing but a byte-order locale, on both '
                    'nodes, so their data leaves localedef nothing to compute; '
                    "whether changed code moves their order is step 5's "
                    'question.' in flat(text), settled)
                self.assertEqual('no ellipsis range on either node'
                                 in flat(text), not settled)

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
        self.assertIn('342 locale(s), and 43 more', tag_text)
        self.assertIn('342 locale(s), and 43 more', dir_text)
        self.assertEqual(default_weight_section(dir_text),
                         default_weight_section(tag_text))

    def test_the_help_does_not_call_this_the_only_way_to_see_C(self):
        """--locales-dir was described as "the only way to
        see a locale the distro backports"; steps 6/7 and 8 read that file
        too, and the tag holds localedata/locales/C from glibc 2.35."""
        rc, text = run('flag_algorithmic_ranges.py', '--help', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        # The whole entry, compared as one string, so the claim cannot come
        # back in a wording nobody thought to forbid.
        entry = text.split('\n  --locales-dir', 1)[1].split('\n  --', 1)[0]
        self.assertEqual(flat(entry), (
            "LOCALES_DIR scan a copy of a node's /usr/share/i18n/locales/ "
            "instead of a tag, which reaches a locale the distro backports, "
            "such as C (C.UTF-8)."))

    def test_the_old_tag_help_is_scoped_to_the_two_tags(self):
        """--old-tag's help said the findings at neither tag are "the ones no
        tag diff could ever see". A later tag can hold them: the file `C` is
        upstream from glibc 2.35."""
        rc, text = run('diff_node_locales.py', '--help', out_dir=self.out)
        self.assertEqual(rc, 0, text)
        entry = text.split('\n  --old-tag', 1)[1].split('\n  --', 1)[0]
        self.assertEqual(flat(entry), (
            "OLD_TAG the upstream tag the audit used for the old side. With "
            "--new-tag, reports which findings exist at neither tag -- the "
            "ones a diff of these two tags cannot see -- and lets the list "
            "of what cannot be decided include a file of the old tag that "
            "neither copy holds."))

    def test_a_backported_C_in_the_directory_is_flagged(self):
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n',
                                                  extra={'C': backported_c()}),
                       '--build-id', 'glibc-2.28-251.el8_10.40',
                       '--supported-tag', MID, out_dir=self.out)
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
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
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
                       '--build-id', 'truncated', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 2, text)
        self.assertIn('below the floor of', flat(text))
        self.assertNotIn('No locale uses ellipsis ranges', text)

    def lost_in_transit(self, tag, keep_count):
        """A copy of `tag` down to `keep_count` files, deleting only files no
        remaining locale copies -- so no dangling `copy` target gives the loss
        away and the file count is the only thing that could."""
        root = self.node(tag, 'lost')
        while len(os.listdir(root)) > keep_count:
            texts = {}
            for n in os.listdir(root):
                with open(os.path.join(root, n), 'rb') as fh:
                    texts[n] = fh.read().decode('utf-8', 'surrogateescape')
            copied = {t for text in texts.values()
                      for t in g.copy_targets(text)}
            loose = sorted(n for n in texts if n not in copied)
            for n in loose[:len(texts) - keep_count]:
                os.remove(os.path.join(root, n))
        return root

    def test_a_copy_that_lost_files_says_so_against_the_tag(self):
        """Backlog 1.19, measured: 250 of glibc-2.34's 355 files, losing only
        files nothing copies, passed the floor of 200 at exit 0 with the full
        ellipsis verdict and no `!!`."""
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.lost_in_transit(MID, 250),
                       '--build-id', 'lost', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        warning = re.search(r'^!! (\d+) file\(s\) that glibc-2\.34 has '
                            r'under localedata/locales/ are missing from',
                            text, re.M)
        self.assertIsNotNone(warning, text)
        self.assertEqual(int(warning.group(1)), 105)

    def test_a_tag_file_held_as_a_symlink_is_named_as_not_read(self):
        """The mirror of step 6's case: skipped, so not read, and not
        missing either (false-negative-reviewer on this fix)."""
        root = self.node(MID, 'n')
        path = os.path.join(root, 'th_TH')
        os.rename(path, os.path.join(self.tmp, 'th_TH.real'))
        os.symlink(os.path.join(self.tmp, 'th_TH.real'), path)
        rc, text = run('flag_algorithmic_ranges.py', '--locales-dir', root,
                       '--build-id', 'link', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('but were not read: th_TH (symlink).', flat(text))
        self.assertNotIn('are missing from', flat(text))

    def test_a_complete_copy_prints_no_such_warning(self):
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n'),
                       '--build-id', 'full', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('are missing from', flat(text))
        self.assertNotIn('not checked against any tag', flat(text))

    def test_without_a_tag_it_refuses_to_read_the_directory(self):
        """The files are read the way glibc at --supported-tag reads them
        (glibc_tree): without one there is no telling how localedef reads
        them, and no list to notice a copy that lost files against. Refused,
        by name, before anything is read (backlog 15.2)."""
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.lost_in_transit(MID, 250),
                       '--build-id', 'lost', out_dir=self.out)
        self.assertEqual(rc, 2, text)
        self.assertIn('--locales-dir requires --supported-tag', flat(text))
        self.assertNotIn('Files at', text)

    def test_the_directory_run_does_not_clobber_the_tag_list(self):
        tag_list = os.path.join(self.out, 'step4_exposed_locales.txt')
        rc, text = run('flag_algorithmic_ranges.py', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        with open(tag_list, encoding='utf-8') as fh:
            before = fh.read()
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(OLD, 'n'),
                       '--build-id', 'fake', '--supported-tag', OLD,
                       out_dir=self.out)
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
        glibc-2.28-251.el8_10.40, 2026-09-07).
        The reported set and the written set are one set.
        """
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'n',
                                                  extra={'C': backported_c()}),
                       '--build-id', 'glibc-2.28-251.el8_10.40',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        counts = re.search(r'(\d+) locale\(s\), and (\d+) more\nname\(s\), '
                           r"the aliases glibc's locale.alias gives them",
                           text)
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
        # Every locale reported, and the aliases: the two numbers above it.
        self.assertEqual(len(written),
                         int(counts.group(1)) + int(counts.group(2)))

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
        # The tag-mode label, which test_known_answers asserts is printed
        # there, so this cannot pass by the label being renamed away.
        self.assertNotIn('not in SUPPORTED (not built by default)', flat(text))

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
                               '--build-id', 'fake.el9',
                               '--supported-tag', MID, out_dir=self.out)
                self.assertEqual(rc, 0, text)
                self.assertIn(expected, text)

    def test_an_explicit_C_is_declared_exposed_by_the_default_weight(self):
        """Step 4 lists a C with explicit weights for localedef's default
        weight (backlog 1.6), so its declaration says so too; "no ellipsis
        range for localedef to expand" alone read as cleared. Both places that
        declare are driven: the scan with the templates' ranges intact, and
        the one where nothing uses an ellipsis. The control is a C declaring
        codepoint_collation, which no default weight reaches."""
        explicit = locale_file('order_start forward',
                               '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                               'order_end')
        no_ranges = {name: explicit for name in
                     ('i18n', 'iso14651_t1', 'iso14651_t1_common', 'ko_KR')}
        clause = ("takes localedef's default weight -- so this locale IS "
                  "exposed")
        cases = (('ranges', {'C': explicit}, True),
                 ('noranges', {**no_ranges, 'C': explicit}, True),
                 ('control', {'C': upstream_c()}, False))
        for name, extra, exposed in cases:
            with self.subTest(case=name):
                rc, text = run('flag_algorithmic_ranges.py',
                               '--locales-dir',
                               self.node(MID, f'explicit-{name}', extra=extra),
                               '--build-id', 'fake.el9',
                               '--supported-tag', MID, out_dir=self.out)
                self.assertEqual(rc, 0, text)
                line = re.search(r'^  C \(C\.UTF-8\): (.*)$', text, re.M)
                self.assertIsNotNone(line, text)
                if exposed:
                    self.assertTrue(line.group(1).startswith('explicit weights'),
                                    line.group(1))
                    self.assertIn(clause, line.group(1))
                else:
                    self.assertTrue(
                        line.group(1).startswith('codepoint_collation'),
                        line.group(1))
                    self.assertNotIn('default weight', line.group(1))

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
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('so this locale IS exposed', flat(text))
        with open(os.path.join(self.out,
                               'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_C_with_a_copy_beside_the_keyword_is_exposed(self):
        """This test asserted the opposite until backlog 13.2: that
        `codepoint_collation` discards what the copy brings, so "IS exposed"
        was an alarm over a locale glibc had settled. Measured false (glibc
        study, E5): `copy "iso14651_t1"` plus the keyword compiles on RHEL9
        and RHEL10 into tables whose strcoll returns garbage and crashes, and
        into plain iso14651_t1 order on RHEL8. Byte order is the keyword
        alone. The status must not start with the keyword either: audit.sh
        reads C's line by how it starts."""
        body = upstream_c().replace('\ncodepoint_collation\n',
                                    '\ncopy "iso14651_t1"\ncodepoint_collation\n')
        self.assertIn('copy "iso14651_t1"', body)
        self.assertEqual(body.count('codepoint_collation'), 2)  # prose + keyword
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'cpc', extra={'C': body}),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('C (C.UTF-8): names codepoint_collation, but could '
                      'not be read as the keyword alone', flat(text))
        self.assertIn('so this locale IS exposed', flat(text))
        self.assertNotIn('C (C.UTF-8): codepoint_collation', flat(text))
        self.assertNotIn('Declare codepoint_collation', flat(text))
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_sole_copy_of_a_byte_order_locale_is_named_not_listed(self):
        """Backlog 6.9. A locale whose LC_COLLATE is `copy "C"` and nothing
        else shares C's data, so glibc builds it in byte order: measured on
        RHEL9 and RHEL10 byte-identical to the installed C.UTF-8. It was
        listed for the default weight. It leaves the list and is named on a
        line of its own; a copy of C with a sort rule of its own stays
        listed, because that rule is read in the same build."""
        extra = {'C': upstream_c(),
                 'xx_XX': locale_file('copy "C"'),
                 'yy_YY': locale_file('copy "C"', 'order_start forward',
                                      '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                      'order_end')}
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'copyofc', extra=extra),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('Declare codepoint_collation, so no expansion change '
                      'can move them: C', flat(text))
        self.assertIn('Copy a byte-order locale and nothing else, so glibc '
                      'builds them in byte order too: xx_XX (copies C)',
                      flat(text))
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh]
        self.assertNotIn('xx_XX', written)
        self.assertNotIn('C', written)
        self.assertIn('yy_YY', written)

    def test_a_C_that_only_copies_a_byte_order_locale_says_so(self):
        """Backlog 6.9 in the status steps 9 and 10 declare. Step 4 names such
        a C as byte order and leaves it off the list, and the status stopped
        at "copy-only", true of the file and silent about the order; the two
        disagreed until false-negative-reviewer noticed. The control: when
        what it copies is not the keyword alone, the copy is no byte order.
        The status is declared on two paths, one for a corpus where nothing
        uses an ellipsis range, so that path has a case of its own."""
        flat_body = locale_file('order_start forward',
                                '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                'order_end')
        no_ranges = {name: flat_body for name in
                     ('i18n', 'iso14651_t1', 'iso14651_t1_common', 'ko_KR')}
        cases = (('alone', upstream_c(), {}, True),
                 ('alone, no ranges', upstream_c(), no_ranges, True),
                 ('beside a copy', locale_file('copy "iso14651_t1"',
                                               'codepoint_collation'), {},
                  False))
        for name, target, more, byte_order in cases:
            with self.subTest(case=name):
                slug = re.sub(r'\W+', '_', name)
                out = os.path.join(self.out, slug)
                os.makedirs(out)
                rc, text = run('flag_algorithmic_ranges.py', '--locales-dir',
                               self.node(MID, f'ccopy-{slug}',
                                         extra={'C': locale_file('copy "zz_ZZ"'),
                                                'zz_ZZ': target, **more}),
                               '--build-id', 'fake.el9',
                               '--supported-tag', MID, out_dir=out)
                self.assertEqual(rc, 0, text)
                self.assertEqual(
                    'C (C.UTF-8): copy-only <- its order is whatever it '
                    'inherits; follow the copy chain; and it copies zz_ZZ and '
                    'nothing else, which glibc builds in byte order -- so '
                    'this locale is byte order too' in flat(text), byte_order)
                with open(os.path.join(out, 'step4_exposed_locales.fake.el9.txt'),
                          encoding='utf-8') as fh:
                    self.assertEqual('C' in [ln.strip() for ln in fh],
                                     not byte_order)

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
        # Named, not just counted: a reader looks the collation their database
        # uses up in this list, by its locale or its alias (backlog 13.1).
        self.assertIn('sv_SE', flat(text))
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.txt'),
                  encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh]
        self.assertIn('C', written)
        self.assertIn('sv_SE', written)
        self.assertIn('swedish', written)
        # The two numbers above the list add up to it with an unresolved
        # locale in it too: N once left C and sv_SE out, and the sum fell
        # short of the list.
        counts = re.findall(r'Full set needing empirical confirmation: (\d+) '
                            r'locale\(s\), and (\d+) more', text)
        listed = re.findall(r'full list \((\d+) name\(s\)\)', text)
        self.assertEqual((len(counts), len(listed)), (1, 1), text)
        self.assertEqual(int(counts[0][0]) + int(counts[0][1]),
                         int(listed[0]))
        self.assertEqual(int(listed[0]), len(written))

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
        self.assertIn('so this locale is NOT cleared', flat(text))
        # Every other locale here takes the default weight (backlog 1.6), so
        # the sentence counts those and the unresolved ones together. Each
        # locale once: one reaching the absent target is in the `!!` block and
        # not in the default-weight section as well.
        defined = int(re.search(r'of which (\d+) define LC_COLLATE',
                                text).group(1))
        unresolved = int(re.search(r'(\d+) locale\(s\) reach one',
                                   text).group(1))
        default = default_weight_section(text)[0]
        self.assertEqual(default + unresolved, defined)
        self.assertIn(f'No locale uses ellipsis ranges here, but the '
                      f'{defined} locale(s) above are NOT cleared by steps '
                      f'1-3.', flat(text))
        # Opened at the path the output NAMES, not at one this test knows:
        # announcing a file nobody wrote sends the reader to "No such file",
        # and a test that looks elsewhere cannot tell.
        named = re.search(r'full list \((\d+) name\(s\)\): (\S+)', text)
        self.assertIsNotNone(named, text)
        with open(named.group(2), encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertIn('C', written)
        # This path adds the aliases of locale.alias too (backlog 13.1), and
        # had no test saying so: sv_SE reaches the absent target here.
        self.assertIn('sv_SE', written)
        self.assertIn('swedish', written)
        # And it carries the default-weight locales, not only the unresolved.
        self.assertIn('ja_JP', written)
        self.assertEqual(int(named.group(1)), len(written))

    def test_a_corpus_left_only_unresolved_says_so(self):
        """The branch the test above no longer reaches: nothing uses an
        ellipsis and nothing is left for the default weight, because every
        locale copies a target the corpus does not contain. It must say those
        are unresolved, never "sufficient", and print no default-weight
        section over nothing."""
        tree = _tree(MID)
        extra = {}
        for fname in os.listdir(tree):
            if not os.path.isfile(os.path.join(tree, fname)):
                continue
            with open(os.path.join(tree, fname), 'rb') as fh:
                text = fh.read().decode('utf-8', 'surrogateescape')
            if g.collate_block(text) is not None:
                extra[fname] = locale_file('copy "no_such_locale"')
        self.assertEqual(len(extra), 342)
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'alldangle', extra=extra),
                       '--build-id', 'fake', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('steps 1-3 are sufficient', flat(text))
        self.assertIn('Nothing that could be READ here uses an ellipsis range, '
                      'but the 342 locale(s) named above under `!!` were not '
                      'read the way localedef reads them: steps 1-3 are NOT '
                      'sufficient for those', flat(text))
        self.assertNotIn('default weight', text)

    def test_a_C_with_a_missing_copy_beside_the_keyword_is_not_cleared(self):
        """This test asserted the opposite until backlog 13.2, on the same
        false premise as the one above. A copy of a file that is not there
        stops localedef with exit 4, keyword or not (localedef.c load_locale),
        so what this C would sort by was never read."""
        body = upstream_c().replace('\ncodepoint_collation\n',
                                    '\ncopy "no_such_locale"\ncodepoint_collation\n')
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'cpdangle', extra={'C': body}),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        # glibc stops at the copy and never reads the keyword, so neither
        # the keyword nor "copy-only" describes what was read (backlog 15.2).
        self.assertIn('C (C.UTF-8): not read to the end the way localedef '
                      'reads it <- NOT cleared; and it copies no_such_locale, '
                      'which is NOT in this corpus', flat(text))
        self.assertIn('so this locale is NOT cleared', flat(text))
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_C_the_reader_cannot_settle_is_not_cleared(self):
        """A run this reader cannot settle is not read as clean: C's status
        says so and C is on the list (backlog 15.2). reorder-sections-after
        is one such statement until backlog 15.3 reads it."""
        body = locale_file('copy "iso14651_t1"',
                           'reorder-sections-after <LATIN> <X>')
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'unsettled', extra={'C': body}),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('C (C.UTF-8): not read to the end the way localedef '
                      'reads it <- NOT cleared;', flat(text))
        self.assertIn('; and how localedef reads it could not be settled '
                      '(C:6: reorder-sections-after in LC_COLLATE) -- so this '
                      'locale is NOT cleared', flat(text))
        self.assertRegex(text, r'(?m)^!! 1 locale\(s\) could not be read the '
                               r'way localedef reads them')
        self.assertRegex(text, r'(?m)^     C: C:\d+: reorder-sections-after '
                               r'in LC_COLLATE$')
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_C_the_reader_cannot_settle_is_listed_without_a_copy(self):
        """The case above inherits a range too, so it is listed either way.
        Without a copy, only the reader's `!!` puts C on the list
        (false-negative-reviewer, round 1 of B2)."""
        body = locale_file('order_start forward', '<U0041>', 'order_end',
                           'reorder-sections-after <LATIN> <X>')
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir',
                       self.node(MID, 'unsettled2', extra={'C': body}),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r'(?m)^     C: C:\d+: reorder-sections-after '
                               r'in LC_COLLATE$')
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

    def test_a_C_glibc_stops_reading_before_its_LC_COLLATE_is_listed(self):
        """A 0x1A alone on a line above LC_COLLATE: before 2.36 localedef
        reads nothing past it (case b02, el8 and el9). C must not read as
        "defines no LC_COLLATE" and drop from every list (false-negative-
        reviewer, round 1 of B2)."""
        root = self.node(MID, 'cut')
        with open(os.path.join(root, 'C'), 'wb') as fh:
            fh.write(b'\x1a\n' + upstream_c().encode('utf-8'))
        rc, text = run('flag_algorithmic_ranges.py', '--locales-dir', root,
                       '--build-id', 'fake.el9', '--supported-tag', MID,
                       out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('C (C.UTF-8): not read to the end the way localedef '
                      'reads it <- NOT cleared', flat(text))
        self.assertIn('C: localedef stops reading C at line 1, before the '
                      'end of the file', flat(text))
        self.assertNotIn('defines no LC_COLLATE block', text)
        with open(os.path.join(self.out, 'step4_exposed_locales.fake.el9.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('C', [ln.strip() for ln in fh])

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
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 2, text)
        self.assertNotIn('No locale uses ellipsis ranges', text)
        self.assertIn('not one defines LC_COLLATE', flat(text))

    def test_a_directory_without_any_ellipsis_still_declares_C(self):
        """The most reassuring output this step had -- "No locale uses ellipsis
        ranges here; steps 1-3 are sufficient" -- returned before the
        declaration, so the wrapper printed NOT DECLARED over a scan that had
        looked and found an answer. Every path declares.

        Since backlog 1.6 this input takes the default-weight path: with no
        ellipsis anywhere and no copy target missing, which is the shape a real
        corpus has, every locale but C is still exposed, and the sentence the
        two paths share must not end in "sufficient" here."""
        flat_body = locale_file('order_start forward',
                                '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                'order_end')
        extra = {name: flat_body for name in
                 ('i18n', 'iso14651_t1', 'iso14651_t1_common', 'ko_KR')}
        extra['C'] = upstream_c()
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'flat', extra=extra),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('steps 1-3 are sufficient', flat(text))
        default = default_weight_section(text)[0]
        self.assertEqual(default, int(re.search(
            r'of which (\d+) define LC_COLLATE', text).group(1)) - 1)
        self.assertIn(f'No locale uses ellipsis ranges here, but the {default} '
                      f'locale(s) above are NOT cleared by steps 1-3.',
                      flat(text))
        self.assertIn('C (C.UTF-8): codepoint_collation', text)

    def test_an_all_codepoint_directory_is_the_one_clean_result(self):
        """The only input left that earns "steps 1-3 are sufficient": every
        locale declares codepoint_collation, so localedef has no range to
        expand and no default weight to give. No real corpus has this shape,
        so it is injected; without it the branch had no test once the input
        above moved to the default-weight path. The list is still written and
        C still declared on it."""
        tree = _tree(MID)
        extra = {'C': upstream_c()}
        for fname in os.listdir(tree):
            if not os.path.isfile(os.path.join(tree, fname)):
                continue
            with open(os.path.join(tree, fname), 'rb') as fh:
                text = fh.read().decode('utf-8', 'surrogateescape')
            if g.collate_block(text) is not None:
                extra[fname] = upstream_c()
        self.assertEqual(len(extra), 343)
        rc, text = run('flag_algorithmic_ranges.py',
                       '--locales-dir', self.node(MID, 'allcp', extra=extra),
                       '--build-id', 'fake.el9',
                       '--supported-tag', MID, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertIn('No locale uses ellipsis ranges here; steps 1-3 are '
                      'sufficient.', text)
        self.assertNotIn('default weight', text)
        named = re.search(r'full list \((\d+) name\(s\)\): (\S+)', text)
        self.assertIsNotNone(named, text)
        with open(named.group(2), encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertEqual((int(named.group(1)), written), (0, []))
        self.assertIn('C (C.UTF-8): codepoint_collation', text)

    def test_the_tag_scan_declares_no_backported_status(self):
        """A tag has no distro backports by construction, and a heading that
        appeared there would invite the reader to trust a tag scan on the one
        question it structurally cannot answer."""
        rc, text = run('flag_algorithmic_ranges.py', NEW, out_dir=self.out)
        self.assertEqual(rc, 0, text)
        self.assertNotIn('C (C.UTF-8):', text)


_CHARMAPS = {}


def _charmaps(tag):
    """One copy of a tag's charmaps, gzipped as a machine's, per module."""
    if tag not in _CHARMAPS:
        base = tempfile.mkdtemp(prefix='pg-glibc-tagcm-')
        _CHARMAPS[tag] = tag_charmaps(tag, os.path.join(base, 'charmaps'))
    return _CHARMAPS[tag]


def _swap(a, ba, b, bb):
    """A charmap edit: the lines of code points a and b trade bytes."""
    def edit(text):
        la = next(ln for ln in text.split('\n') if ln.startswith(a))
        lb = next(ln for ln in text.split('\n') if ln.startswith(b))
        assert ba in la and bb in lb, (la, lb)
        return text.replace(la, la.replace(ba, bb)).replace(
            lb, lb.replace(bb, ba))
    return edit


DISTRO_NOTICE = ("A machine's own localedata/charmaps/ is NOT compared, nor "
                 "what its own transliterations build.")


@needs_clone
class MachineCharmaps(NodeCase):
    """Steps 6/7 and 8 with a machine's charmaps and measurement
    (node_charmaps, 13.7 PR D), run directly. The measurement is written by
    hand with a few names: which builds a machine has comes from it."""

    BUILD = 'glibc-2.28-1.test'

    def cms(self, tag, name='cm', edit=None, extra=None, link=False):
        if edit is None and extra is None and not link:
            return _charmaps(tag)
        dest = tag_charmaps(tag, os.path.join(self.tmp, name), edit)
        for fname, text in (extra or {}).items():
            with open(os.path.join(dest, fname), 'w', encoding='utf-8') as fh:
                fh.write(text)
        if link:
            os.symlink('UTF-8.gz', os.path.join(dest, 'LINKED.gz'))
        return dest

    def measurement(self, locales, build=None, name='m.json'):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='ascii') as fh:
            json.dump({'glibc_build': build or self.BUILD,
                       'locales': {n: {'codeset': c}
                                   for n, c in locales.items()}}, fh)
        return path

    def step6(self, node, *extra, tag=OLD):
        return run('diff_distro_locales.py', tag, '--locales-dir', node,
                   '--build-id', self.BUILD, '--node-label', 'old', *extra,
                   out_dir=self.out)

    def keyed(self, pattern):
        """The names in the list the check wrote, or None when it wrote
        none."""
        paths = glob.glob(os.path.join(self.out, pattern))
        if not paths:
            return None
        self.assertEqual(len(paths), 1, paths)
        with open(paths[0], encoding='utf-8') as fh:
            lines = fh.read().split('\n')
        self.assertTrue(lines[0].startswith('# '), lines[0])
        return [ln for ln in lines[1:] if ln]

    GREEK = {'el_GR': 'ISO-8859-7', 'el_GR.iso88597': 'ISO-8859-7',
             'de_DE.utf8': 'UTF-8', 'C': 'ANSI_X3.4-1968',
             'POSIX': 'ANSI_X3.4-1968'}

    def test_identical_charmaps_list_nothing_and_say_so(self):
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir',
                             self.cms(OLD), '--measurement',
                             self.measurement(self.GREEK))
        self.assertEqual(rc, 0, out)
        self.assertIn('Character sets (/usr/share/i18n/charmaps/): the 2 that '
                      'the 2 locale builds on this machine use are identical '
                      f'on this machine and at {OLD}.', flat(out))
        self.assertIn(f'Locale names measured on {self.BUILD}: 5, built as 2 '
                      f'locale build(s)', flat(out))
        self.assertEqual(self.keyed('distro_keyed_locales.*.txt'), [])
        self.assertNotIn(DISTRO_NOTICE, flat(out))

    def test_a_charmap_the_distro_changed_lists_the_builds_it_moves(self):
        cm = self.cms(OLD, edit={'ISO-8859-7': swap_alpha_beta})
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir', cm,
                             '--measurement', self.measurement(self.GREEK))
        self.assertEqual(rc, 0, out)
        self.assertIn('ISO-8859-7: 2 characters given other bytes.', flat(out))
        self.assertIn('el_GR (ISO-8859-7): 2 characters, named in '
                      'iso14651_t1_common', flat(out))
        self.assertEqual(self.keyed('distro_keyed_locales.*.txt'), ['el_GR'])

    def test_a_file_without_charmaps_is_not_run_with_its_reason(self):
        rc, out = self.step6(self.node(OLD, 'n'), '--no-charmaps',
                             '/a/node-file.tar holds no charmaps, because x')
        self.assertEqual(rc, 0, out)
        self.assertIn('!! NOT RUN: the comparison of the character sets '
                      f"{self.BUILD} builds its locales with, against {OLD}'s "
                      '(/usr/share/i18n/charmaps/). /a/node-file.tar holds no '
                      'charmaps, because x.', flat(out))
        self.assertIsNone(self.keyed('distro_keyed_locales.*.txt'))
        self.assertNotIn(DISTRO_NOTICE, flat(out))

    def test_a_copied_folder_keeps_its_notice_and_writes_no_list(self):
        rc, out = self.step6(self.node(OLD, 'n'))
        self.assertEqual(rc, 0, out)
        self.assertIn(DISTRO_NOTICE, flat(out))
        self.assertIsNone(self.keyed('distro_keyed_locales.*.txt'))

    def test_a_charmaps_folder_it_cannot_read_whole_is_not_run(self):
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir',
                             self.cms(OLD, link=True), '--measurement',
                             self.measurement(self.GREEK))
        self.assertEqual(rc, 0, out)
        self.assertIn('NOT RUN: the comparison of the character sets',
                      flat(out))
        self.assertIn('LINKED.gz is not a file', flat(out))
        self.assertIsNone(self.keyed('distro_keyed_locales.*.txt'))

    def test_the_options_that_go_together(self):
        node, cm = self.node(OLD, 'n'), self.cms(OLD)
        rc, out = self.step6(node, '--charmaps-dir', cm)
        self.assertEqual(rc, 2, out)
        self.assertIn('--charmaps-dir and --measurement go together', out)
        rc, out = self.step6(node, '--charmaps-dir', cm, '--measurement',
                             self.measurement(self.GREEK), '--no-charmaps',
                             'x')
        self.assertEqual(rc, 2, out)
        self.assertIn('cannot both be given', out)

    LOOSE = ('<code_set_name> ISO-8859/-7\n<comment> %\n<escape_char> /\n'
             'CHARMAP\n<U0041> /x41\nEND CHARMAP\n')

    def test_a_loose_charmap_the_machine_added_is_not_run(self):
        """A charmap added on the machine whose name this cannot read
        whole: which codeset it gives is not known, so the check says NOT
        RUN rather than trace builds past it (round 3 of PR D)."""
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir',
                             self.cms(OLD, extra={'LOCAL-GREEK': self.LOOSE}),
                             '--measurement', self.measurement(self.GREEK))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"NOT RUN: the comparison of the character sets "
                      f"{self.BUILD} builds its locales with, against {OLD}'s "
                      f"(/usr/share/i18n/charmaps/). LOCAL-GREEK on the "
                      f"machine is not {OLD}'s and could not be read whole",
                      flat(out))
        self.assertIsNone(self.keyed('distro_keyed_locales.*.txt'))

    def test_a_locale_the_tag_lacks_is_compared_with_the_machines_files(self):
        """C is on RHEL8's machine and at no tag before 2.35. Its rules,
        read from the machine on both sides, against the tag's charmap and
        the machine's: no `!!` saying it was not compared."""
        node = self.node(OLD, 'n', extra={'C': backported_c()})
        rc, out = self.step6(node, '--charmaps-dir', self.cms(OLD),
                             '--measurement',
                             self.measurement({'C.utf8': 'UTF-8',
                                               'de_DE.utf8': 'UTF-8'}))
        self.assertEqual(rc, 0, out)
        self.assertIn('the 1 that the 2 locale builds on this machine use',
                      flat(out))
        self.assertNotIn('not compared', flat(out))

    def test_a_charmap_the_tag_lacks_lists_its_builds(self):
        cm = self.cms(OLD, extra={
            'XTEST': '<code_set_name> XTEST\n<comment_char> %\n'
                     '<escape_char> /\nCHARMAP\n<U0041> /x41\n'
                     'END CHARMAP\n'})
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir', cm,
                             '--measurement',
                             self.measurement({'de_DE.xtest': 'XTEST'}))
        self.assertEqual(rc, 0, out)
        self.assertIn(f'de_DE.xtest (XTEST): {OLD} has no charmap XTEST',
                      flat(out))
        self.assertEqual(self.keyed('distro_keyed_locales.*.txt'), ['de_DE'])

    def test_a_locale_this_step_lists_already_is_counted_not_repeated(self):
        node = self.node(OLD, 'n')
        with open(os.path.join(node, 'de_DE'), encoding='utf-8') as fh:
            text = fh.read()
        with open(os.path.join(node, 'de_DE'), 'w', encoding='utf-8') as fh:
            fh.write(text.replace('END LC_COLLATE',
                                  '% changed by the distro\nEND LC_COLLATE',
                                  1))
        cm = self.cms(OLD, edit={'ISO-8859-1': _swap(
            '<U00E8>', '/xe8', '<U00E9>', '/xe9')})
        rc, out = self.step6(node, '--charmaps-dir', cm, '--measurement',
                             self.measurement({'de_DE': 'ISO-8859-1'}))
        self.assertEqual(rc, 0, out)
        self.assertIn('Inside LC_COLLATE (1)', out)
        self.assertIn('Locale builds this step lists above, whose LC_COLLATE '
                      'names one of those characters or could not be compared '
                      '(1): de_DE (ISO-8859-1)', flat(out))
        self.assertEqual(self.keyed('distro_keyed_locales.*.txt'), [])

    def test_a_name_it_cannot_trace_is_named_and_listed(self):
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir',
                             self.cms(OLD), '--measurement',
                             self.measurement({'xx_XX.odd': 'NOPE',
                                               'de_DE.utf8': 'UTF-8'}))
        self.assertEqual(rc, 0, out)
        self.assertIn(f'!! 1 locale name(s) on {self.BUILD} could not be '
                      f'traced to the charmap they are built with, so their '
                      f'character set is not compared: xx_XX.odd: no charmap '
                      f'on the machine declares its encoding NOPE', flat(out))
        self.assertEqual(self.keyed('distro_keyed_locales.*.txt'),
                         ['xx_XX.odd'])

    FINNISH = {'fi_FI': 'ISO-8859-1', 'fi_FI@euro': 'ISO-8859-15',
               'fi_FI.utf8': 'UTF-8', 'C': 'ANSI_X3.4-1968'}

    def translit_changed(self, name):
        """A tag tree whose translit_combining gives U+0335 an answer it has
        none for at 2.28. fi_FI's collating elements spell U+0335, which
        ISO-8859-1 and ISO-8859-15 lack, so localedef builds them out of
        that line (false-negative-reviewer, round 1 of PR D)."""
        node = self.node(OLD, name)
        path = os.path.join(node, 'translit_combining')
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        self.assertEqual(text.count('<U0335> ""'), 1)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('<U0335> ""', '<U0335> "<U002D>"'))
        return node

    def test_a_machine_s_own_transliteration_is_compared(self):
        """Only the transliteration file changed: no charmap, no
        LC_COLLATE. Step 6 compares the machine's own texts with the tag's,
        and lists the two builds whose elements it builds differently."""
        rc, out = self.step6(self.translit_changed('n'), '--charmaps-dir',
                             self.cms(OLD), '--measurement',
                             self.measurement(self.FINNISH))
        self.assertEqual(rc, 0, out)
        self.assertIn('Transliteration (LC_CTYPE): 20 collating elements of 2 '
                      'locale builds depend on a transliteration; 20 are '
                      'built differently on this machine.', flat(out))
        self.assertEqual(self.keyed('distro_keyed_locales.*.txt'),
                         ['fi_FI', 'fi_FI@euro'])
        # The file differs, outside any LC_COLLATE: counted as differing.
        self.assertEqual(re.findall(r'\((\d+) of \d+ differ in some way\)',
                                    out), ['1'])

    def test_the_two_machines_own_transliterations_are_compared(self):
        rc, out = self.step8(self.node(OLD, 'o'), self.translit_changed('n'),
                             *self.four(self.cms(OLD), self.cms(OLD),
                                        locales=self.FINNISH),
                             tags=(OLD, OLD))
        self.assertEqual(rc, 0, out)
        self.assertIn('20 are built differently on b-new.', flat(out))
        self.assertEqual(self.keyed('node_keyed_locales.*.txt'),
                         ['fi_FI', 'fi_FI@euro'])

    def test_a_machine_with_no_build_to_compare_writes_no_list(self):
        """Only glibc's built-in locale: nothing to compare and nothing to
        name. The step says NOT RUN and writes no list, which the summary
        reads as not run, never as nothing to add."""
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir',
                             self.cms(OLD), '--measurement',
                             self.measurement({'C': 'ANSI_X3.4-1968',
                                               'POSIX': 'ANSI_X3.4-1968'}))
        self.assertEqual(rc, 0, out)
        self.assertIn('NOT RUN: the comparison of the character sets '
                      f'{self.BUILD} builds its locales with, against '
                      f"{OLD}'s, and of the collating elements they build out "
                      'of transliterations. No locale build on this machine '
                      'could be compared.', flat(out))
        self.assertIsNone(self.keyed('distro_keyed_locales.*.txt'))

    # Step 8.

    def test_step_8_counts_what_it_lists_already(self):
        new = self.node(OLD, 'n')
        with open(os.path.join(new, 'de_DE'), encoding='utf-8') as fh:
            text = fh.read()
        with open(os.path.join(new, 'de_DE'), 'w', encoding='utf-8') as fh:
            fh.write(text.replace('END LC_COLLATE',
                                  '% changed on the new machine\n'
                                  'END LC_COLLATE', 1))
        cm = self.cms(OLD, edit={'ISO-8859-1': _swap(
            '<U00E8>', '/xe8', '<U00E9>', '/xe9')})
        rc, out = self.step8(self.node(OLD, 'o'), new,
                             *self.four(self.cms(OLD), cm,
                                        locales={'de_DE': 'ISO-8859-1'}),
                             tags=(OLD, OLD))
        self.assertEqual(rc, 0, out)
        self.assertIn('Locale builds this step lists above, whose LC_COLLATE '
                      'names one of those characters or could not be compared '
                      '(1): de_DE (ISO-8859-1)', flat(out))
        self.assertEqual(self.keyed('node_keyed_locales.*.txt'), [])

    def step8(self, old_root, new_root, *extra, tags=(OLD, MID),
              builds=('b-old', 'b-new')):
        return run('diff_node_locales.py',
                   '--old-locales-dir', old_root, '--old-build-id', builds[0],
                   '--new-locales-dir', new_root, '--new-build-id', builds[1],
                   '--old-tag', tags[0], '--new-tag', tags[1], *extra,
                   out_dir=self.out)

    def four(self, old_cm, new_cm, builds=('b-old', 'b-new'),
             locales=None):
        locales = locales or self.GREEK
        return ['--old-charmaps-dir', old_cm, '--old-measurement',
                self.measurement(locales, builds[0], 'old.json'),
                '--new-charmaps-dir', new_cm, '--new-measurement',
                self.measurement(locales, builds[1], 'new.json')]

    def test_two_machines_with_the_same_charmaps(self):
        rc, out = self.step8(self.node(OLD, 'o'), self.node(MID, 'n'),
                             *self.four(self.cms(OLD), self.cms(OLD)))
        self.assertEqual(rc, 0, out)
        self.assertIn('Character sets (/usr/share/i18n/charmaps/): the 2 that '
                      'the 2 locale builds on both machines use are identical '
                      'on b-old and b-new.', flat(out))
        self.assertEqual(self.keyed('node_keyed_locales.*.txt'), [])
        self.assertNotIn(DISTRO_NOTICE, flat(out))

    def test_the_new_machine_s_charmap_moves_a_build(self):
        cm = self.cms(OLD, edit={'ISO-8859-7': swap_alpha_beta})
        rc, out = self.step8(self.node(OLD, 'o'), self.node(OLD, 'n'),
                             *self.four(self.cms(OLD), cm),
                             tags=(OLD, OLD))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.keyed('node_keyed_locales.*.txt'), ['el_GR'])

    def test_each_machine_s_names_are_its_own(self):
        """A name only the new machine has, that no charmap traces: the
        counts and the `!!` name the machine they belong to."""
        new_names = dict(self.GREEK, **{'xx_XX.odd': 'NOPE'})
        rc, out = self.step8(
            self.node(OLD, 'o'), self.node(MID, 'n'),
            '--old-charmaps-dir', self.cms(OLD), '--old-measurement',
            self.measurement(self.GREEK, 'b-old', 'old.json'),
            '--new-charmaps-dir', self.cms(OLD), '--new-measurement',
            self.measurement(new_names, 'b-new', 'new.json'))
        self.assertEqual(rc, 0, out)
        self.assertIn('Locale names measured on b-old: 5, built as 2 locale '
                      'build(s)', flat(out))
        self.assertIn('Locale names measured on b-new: 6, built as 2 locale '
                      'build(s)', flat(out))
        self.assertIn('!! 1 locale name(s) on b-new could not be traced',
                      flat(out))
        self.assertNotIn('on b-old could not be traced', flat(out))
        self.assertEqual(self.keyed('node_keyed_locales.*.txt'),
                         ['xx_XX.odd'])

    def test_a_charmap_the_machine_lacks_is_not_run(self):
        # A copy: self.cms(OLD) is the module's shared folder.
        cm = os.path.join(self.tmp, 'cm-short')
        shutil.copytree(self.cms(OLD), cm)
        os.remove(os.path.join(cm, 'IBM1133.gz'))
        rc, out = self.step6(self.node(OLD, 'n'), '--charmaps-dir', cm,
                             '--measurement',
                             self.measurement({'lo_LA.ibm1133': 'IBM1133'}))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"NOT RUN: the comparison of the character sets "
                      f"{self.BUILD} builds its locales with, against {OLD}'s "
                      f"(/usr/share/i18n/charmaps/). The machine lacks 1 of "
                      f"{OLD}'s charmaps (IBM1133)", flat(out))
        self.assertIsNone(self.keyed('distro_keyed_locales.*.txt'))

    def test_each_machine_is_held_to_its_own_tag_s_charmaps(self):
        """between_machines gives each machine its own tag's charmap ids.
        No real file is one tag's own and not the other's (the three loose
        ones are one blob at every tag), so the ids are made to differ: a
        loose file only one tag holds, on the machine of that tag only."""
        import diff_node_locales as dn
        import node_charmaps as nc
        real = nc._tag_charmaps
        for side in ('old', 'new'):
            with self.subTest(side=side):
                tag = OLD if side == 'old' else MID
                local = os.path.join(self.tmp, f'cm-{side}')
                shutil.copytree(self.cms(tag), local)
                with open(os.path.join(local, 'LOCAL-GREEK'), 'w') as fh:
                    fh.write(self.LOOSE)
                oid = nc.CharmapsDir(local).oid('LOCAL-GREEK')

                def ids(repo, t, tag=tag, oid=oid):
                    got = real(repo, t)
                    return dict(got, **{'LOCAL-GREEK': oid}) if t == tag \
                        else got
                cms = {'old': self.cms(OLD), 'new': self.cms(MID)}
                cms[side] = local
                sides = [dn.resolve_side(s, self.node(t, f'{s}-{side}'),
                                         f'b-{s}')
                         for s, t in (('old', OLD), ('new', MID))]
                with mock.patch.object(nc, '_tag_charmaps', ids):
                    r = nc.between_machines(
                        GLIBC_CLONE, OLD, MID, *sides, cms['old'],
                        cms['new'],
                        self.measurement(self.GREEK, 'b-old', 'o.json'),
                        self.measurement(self.GREEK, 'b-new', 'n.json'),
                        set())
                # Held to its own tag the file is read, not refused; it
                # could declare ISO-8859-7, so el_GR's two names are
                # untraced on that side and de_DE.utf8 is the one build.
                self.assertEqual(r.report.builds, 1)
                m = r.measured[0 if side == 'old' else 1]
                self.assertEqual([n for n, _ in m.unmapped],
                                 ['el_GR', 'el_GR.iso88597'])

    def test_a_loose_charmap_on_the_new_machine_stops_step_8(self):
        """The machine's tag is the one the refusal names. Which tag's
        ids each machine is held to is the test above."""
        rc, out = self.step8(
            self.node(OLD, 'o'), self.node(MID, 'n'),
            *self.four(self.cms(OLD),
                       self.cms(MID, extra={'LOCAL-GREEK': self.LOOSE})))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LOCAL-GREEK on the machine is not {MID}'s and could "
                      f"not be read whole", flat(out))
        self.assertIsNone(self.keyed('node_keyed_locales.*.txt'))

    def test_one_build_on_both_machines_gets_two_names(self):
        """A control run, one build on both: the two sides still need two
        names, which the check keys its plan by."""
        rc, out = self.step8(self.node(OLD, 'o'), self.node(OLD, 'n'),
                             *self.four(self.cms(OLD), self.cms(OLD),
                                        builds=('b', 'b')),
                             tags=(OLD, OLD), builds=('b', 'b'))
        self.assertEqual(rc, 0, out)
        self.assertIn('are identical on b (old) and b (new).', flat(out))

    def test_each_machine_is_read_with_its_own_tag(self):
        """2.39 reads its files as UTF-8 and 2.34 does not (UTF8_READER).
        Two machines with the same files and charmaps, given those two
        tags, read the same bytes differently, so the collating elements
        are compared; read with one tag they would not be."""
        rc, out = self.step8(self.node(MID, 'o'), self.node(MID, 'n'),
                             *self.four(self.cms(MID), self.cms(MID)),
                             tags=(MID, NEW))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("no character set and no locale file's LC_CTYPE "
                         "changed between the two machines", flat(out))
        self.assertIn('Transliteration (LC_CTYPE):', out)

    def test_the_options_of_step_8_that_go_together(self):
        o, n, cm = self.node(OLD, 'o'), self.node(MID, 'n'), self.cms(OLD)
        rc, out = self.step8(o, n, '--old-charmaps-dir', cm)
        self.assertEqual(rc, 2, out)
        self.assertIn('go together', out)
        rc, out = run('diff_node_locales.py', '--old-locales-dir', o,
                      '--old-build-id', 'b-old', '--new-locales-dir', n,
                      '--new-build-id', 'b-new', *self.four(cm, cm),
                      out_dir=self.out)
        self.assertEqual(rc, 2, out)
        self.assertIn('needs --old-tag and --new-tag', out)
        rc, out = self.step8(o, n, *self.four(cm, cm), '--no-charmaps', 'x')
        self.assertEqual(rc, 2, out)
        self.assertIn('cannot both be given', out)

    def test_step_8_without_charmaps_is_not_run_with_its_reason(self):
        rc, out = self.step8(self.node(OLD, 'o'), self.node(MID, 'n'),
                             '--no-charmaps', 'the reason given')
        self.assertEqual(rc, 0, out)
        self.assertIn('!! NOT RUN: the comparison of the character sets the '
                      'two machines build their locales with '
                      '(/usr/share/i18n/charmaps/). the reason given.',
                      flat(out))
        self.assertIsNone(self.keyed('node_keyed_locales.*.txt'))
        self.assertNotIn(DISTRO_NOTICE, flat(out))

if __name__ == '__main__':
    unittest.main()
