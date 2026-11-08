"""Layer 3: the real scripts, end to end, on the pinned pairs.

This is what stops a refactor from moving a verdict quietly. The numbers below
for the two audited pairs are ones docs/results.md publishes or the
examples/ files record, and the other pairs' are pinned here alone; if a
change moves one, that is either a discovery or a regression, and either way
somebody has to look.

Run as subprocesses: the contract these tools offer is their printed output and
their exit status.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from _harness import (FLOOR_NEW, FLOOR_OLD, GLIBC_CLONE, MID, NEW, OLD,
                       REPO_ROOT, SCRIPTS_DIR, Step2Report,
                       flat, needs_clone,
                       needs_floor_pair, run_step, step2_characters_file)

import filter_lc_collate_changes as f
import glibc_locale_data as g
import test_locale_order as tlo


class StepRun(unittest.TestCase):
    """Base: a scratch output dir, so the suite never writes to the shared
    /tmp/pg-glibc-collation-audit that a real run uses."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def step(self, script, *args):
        rc, out = run_step(script, *args, out_dir=self.out_dir)
        self.assertEqual(rc, 0, f"{script} {' '.join(args)} exited {rc}:\n{out}")
        return out


@needs_clone
class Step2Compare(StepRun):
    """Step 2 over the audited pairs: which locales' rules differ, the
    totals, and what it lists for Reindex (step 9.2: the comparison of
    rules, every copy followed, replaced the file diff and step 3's copy
    walk). Measured on 2026-11-07 with the code of this step."""

    # (names, same, differ, not compared), the names whose rules differ,
    # and Reindex.
    EXPECTED = {
        (OLD, MID): ((398, 391, 5, 2),
                     ['or_IN', 'sv_FI', 'sv_FI@euro', 'sv_SE', 'swedish']),
        (MID, NEW): ((410, 395, 2, 13), ['th_TH', 'thai'])}

    def report(self, old, new, *more):
        return Step2Report(self.step('filter_lc_collate_changes.py', old,
                                     new, *more))

    def test_the_totals_and_the_locales_whose_rules_differ(self):
        """sv_FI, sv_FI@euro and swedish differ because they copy sv_SE,
        and thai is th_TH's alias: compare_rules follows every copy and
        every alias, which step 3's copy walk used to add after step 2.
        ber_DZ and kab_DZ, which step 2 listed over 2.34..2.39 because their
        files changed, are the same: kab_DZ took ber_DZ's rules into its
        own file, the same statements in the same order."""
        for (old, new), (totals, names) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                r = self.report(old, new)
                self.assertEqual(r.totals, totals)
                self.assertEqual(sorted(r.differs), names)
                self.assertEqual(r.listed, {})
                for name in ('ber_DZ', 'kab_DZ'):
                    self.assertNotIn(name, r.differs)
                    self.assertNotIn(name, r.added)
                    self.assertNotIn(name, r.reindex)

    def test_the_reindex_list_is_written_for_each_pair(self):
        """audit.sh's summary reads this file. The file must exist --
        reverting step 2's write fails here -- and it must be named for the
        version pair, because this class runs step 2 for both pairs into one
        scratch directory and a pair-agnostic name would have each run
        clobber the last."""
        for (old, new), (_, names) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                self.report(old, new)
                path = os.path.join(
                    self.out_dir,
                    f'step2_changed_collate.{g.pair_slug(old, new)}.txt')
                self.assertTrue(os.path.exists(path), f'{path} missing')
                with open(path, encoding='utf-8') as fh:
                    written = [n.strip() for n in fh if n.strip()]
                self.assertEqual(written, names)

    def test_the_written_list_is_the_printed_one(self):
        """Two representations of one list must not drift: what step 2
        prints as "Listed for Reindex" and what the summary reads."""
        for old, new in self.EXPECTED:
            with self.subTest(pair=f'{old}..{new}'):
                r = self.report(old, new)
                path = os.path.join(
                    self.out_dir,
                    f'step2_changed_collate.{g.pair_slug(old, new)}.txt')
                with open(path, encoding='utf-8') as fh:
                    written = [n.strip() for n in fh if n.strip()]
                self.assertEqual(r.reindex, written)

    def test_the_list_holds_locales_not_spellings(self):
        """Backlog 13.1. The list holds locales and the aliases glibc's
        locale.alias gives them, and here no name with a codeset: every
        spelling reaches its locale by glibc's rule, and a spelling in the
        list would invite matching names one by one again."""
        for old, new in self.EXPECTED:
            with self.subTest(pair=f'{old}..{new}'):
                r = self.report(old, new)
                self.assertEqual(
                    [n for n in r.reindex if g.locale_source(n) != n], [])
        self.assertIn('swedish', self.report(OLD, MID).reindex)

    def test_the_character_set_lines_published_for_each_pair(self):
        """Backlog 13.7, PR B. Over each audited pair step 2 compares the
        charmaps the SUPPORTED builds use and lists nothing: no build's
        rules name a character they changed. K is exact, and checked by a
        brute-force expansion when it was written (2026-10-09): over
        2.34..2.39 GB18030 gains 24 characters and moves 6, 30 in all, where
        the time-gate prototype counted 38. The tags-only transcripts must
        print the run's lines, so a figure that moves without regenerating
        them goes red.

        PR C: the transliteration line under it. 24 collating elements of
        fi_FI, fi_FI@euro and uz_UZ in ISO-8859-1 and ISO-8859-15 depend on
        a transliteration over both pairs, the 24 instances the 2026-10-08
        emulation found, and none differs. The 20 of fi_FI and fi_FI@euro
        are not built at either tag (translit_combining gives U+0335 an
        empty string). The tags-only transcripts print these lines; the
        extended ones compare each machine's own files (7.6a, 9.4a)."""
        expect = {(OLD, MID): ('rhel8-to-rhel9', 486, 1,
                               'UTF-8: 6485 characters added.'),
                  (MID, NEW): ('rhel9-to-rhel10', 487, 2,
                               'GB18030: 24 characters added, 6 given other '
                               'bytes. UTF-8: 5954 characters added.')}
        for (old, new), (pair, builds, differ, figures) in expect.items():
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                m = re.search(r'(?ms)^Character sets \(localedata/charmaps/\)'
                              r'.*?\n\n', out)
                self.assertIsNotNone(m, out)
                block = m.group(0)
                self.assertIn(f'{differ} of the 30 that the {builds} locale '
                              f'builds in SUPPORTED at both tags use',
                              flat(block))
                self.assertIn(figures, flat(block))
                self.assertIn("No build's LC_COLLATE names one of those "
                              "characters.", block)
                self.assertIn(f'Transliteration (LC_CTYPE): 24 collating '
                              f'elements of 3 locale builds depend on a '
                              f'transliteration; none is built differently '
                              f'at {new}.', flat(block))
                path = os.path.join(
                    self.out_dir,
                    f'step2_keyed_locales.{g.pair_slug(old, new)}.txt')
                with open(path, encoding='utf-8') as fh:
                    self.assertEqual(fh.read(), '')
                for kind in ('tags-only',):
                    name = f'examples/{pair}-{kind}.txt'
                    with self.subTest(transcript=kind):
                        with open(os.path.join(REPO_ROOT, *name.split('/')),
                                  encoding='utf-8') as f:
                            said = f.read().count(block)
                        self.assertEqual(said, 1, f'{name} does not print '
                                                  f'the run\'s character set '
                                                  f'lines once; regenerate it')

    def test_c_utf8_is_named_not_compared_where_a_tag_has_no_file_c(self):
        """No list of what a distro ships (backlog 13.6, 7.7, 16.123): the
        `!!` comes from the trees. Over 2.28..2.34 neither tag holds
        localedata/locales/C, over 2.34..2.39 the old one does not; step 2
        names exactly those tags, says C.UTF-8's rules were not compared and
        what PostgreSQL does with it, and never that its order changes: a
        tag cannot say that, and on RHEL 9 and 10 it does not. It is the
        step's only `!!` on these pairs. Over 2.34..2.39 C is also named
        with the other files not compared for want of an old one."""
        for (old, new), lacking, added in (
                ((OLD, MID), f'{OLD} and {MID} hold', None),
                ((MID, NEW), f'{MID} holds', 'C.utf8')):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                r = Step2Report(out)
                said = flat(out)
                self.assertEqual(out.count('!!'), 1, out)
                self.assertEqual(said.count(
                    f'!! {lacking} no localedata/locales/C, the file '
                    f'C.UTF-8 is built from, and a distro\'s build can ship '
                    f'its own, so C.UTF-8\'s sort rules were not compared. '
                    f'{f.C_NO_VERSION}'), 1, out)
                self.assertNotIn('differ, and it is listed', said)
                self.assertEqual(r.added.get('C'), added)
                self.assertNotIn('C', r.differs)
                self.assertNotIn('C', r.reindex)
                if added:
                    self.assertIn('Confirm with `locale -a` on the OLD '
                                  'machine', said)

    def test_the_false_blanket_claim_is_gone(self):
        """Added files used to be reported as unable to affect an existing
        index, flat. They can, if the locale existed on the old system --
        distros ship locales upstream does not have, which is the whole
        C.UTF-8 story.

        Asserted on whitespace-collapsed output, and that is the point of this
        docstring: the claim is printed across two lines, so the original
        `assertNotIn('They cannot affect an existing index', out)` matched
        nothing whether the claim was there or not. It guarded the thing it
        named and would have passed if the claim came back. Found 2026-09-06,
        the fourth test in this suite caught guarding nothing.
        """
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                out = flat(self.step('filter_lc_collate_changes.py', old,
                                     new))
                self.assertIn('cannot affect an existing index ONLY IF', out)
                self.assertNotIn('cannot affect an existing index.', out)

    def test_no_locale_is_left_uncompared_on_both_sides(self):
        """Recorded as a measured fact: over both pairs every name with a
        file on both sides is compared -- the reader settles every one and
        no file gains or loses LC_COLLATE (it has never happened between
        glibc 2.17 and 2.42). Every name not compared lacks a file on one
        side. If this ever fails, a locale acquired or lost its sort rules,
        or the reader met a shape it cannot settle, and that is a finding,
        not a bug in this test."""
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                self.assertEqual(self.report(old, new).listed, {})

    def test_every_file_step_2_writes_is_named_for_the_pair(self):
        """A file a run reads must have been written by this run, for this
        pair: every file step 2 writes carries the pair in its name, the
        characters file and the rules by name included."""
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_step('filter_lc_collate_changes.py', OLD, MID,
                           out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        slug = g.pair_slug(OLD, MID)
        self.assertEqual(sorted(os.listdir(out_dir)), sorted(
            [f'step2_{k}.{slug}.txt' for k in ('changed_collate',
                                               'keyed_locales',
                                               'removed_locales')]
            + [f'step2_rules_characters.{slug}.tsv',
               f'step2_rules_by_name.{slug}.json']))

    def test_what_step_2_does_not_compare_is_said_once(self):
        """NOT_COMPARED_HERE: the code no step compares, pointed to once."""
        import filter_lc_collate_changes as f
        out = flat(self.step('filter_lc_collate_changes.py', OLD, MID))
        self.assertEqual(out.count(f.NOT_COMPARED_HERE), 1)
        self.assertEqual(out.count('strcoll'), 1)

    def test_a_reversed_pair_lists_c_as_removed_not_added(self):
        """Control: the note of the files not compared for want of an old
        one is about the old side being missing, not about the name C.
        2.39 -> 2.34, reversed, has C on the old side alone; and 2.39
        against itself compares nothing."""
        r = self.report(NEW, MID, '--allow-reverse')
        self.assertIn('C', r.removed)
        self.assertNotIn('C', r.added)
        self.assertEqual(self.report(NEW, NEW).nothing,
                         'the two tags are one commit.')


@needs_clone
class Step2NamesTheCharacters(StepRun):
    """"Step 2 named the file and stopped". The
    confirmation template needs three test values, and reaching them meant a
    `git diff` by hand; these pin what step 2 prints instead. A list longer
    than a line is written to step2_rules_characters.<pair>.tsv."""

    def characters(self, old, new):
        """(Step2Report, the characters file read whole), from one run in a
        directory of its own: run_step is memoised and does not write
        again."""
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_step('filter_lc_collate_changes.py', old, new,
                           out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        return Step2Report(out), step2_characters_file(os.path.join(
            out_dir, f'step2_rules_characters.{g.pair_slug(old, new)}.tsv'))

    def test_sv_SE_names_W_and_w(self):
        """Two deleted weight lines over 2.28..2.34: `w` stops sorting as a
        variant of `v`. Measured on the fixtures, `va wa vb` on 2.28 and
        `va vb wa` on 2.34. The three that copy sv_SE name the same two."""
        r, rows = self.characters(OLD, MID)
        for name in ('sv_FI', 'sv_FI@euro', 'sv_SE', 'swedish'):
            with self.subTest(name=name):
                self.assertEqual(r.differs[name], ([0x57, 0x77], False))
                self.assertEqual(rows[name], (False, [0x57, 0x77]))

    def test_th_TH_names_the_five_leading_vowels(self):
        """Over 2.34..2.39 the deleted `collating-element` lines pair each of
        the five Thai leading vowels, U+0E40..U+0E44, with a consonant: that
        is the rule that changed. The list is in the characters file, and
        the line prints its count, the same number."""
        r, rows = self.characters(MID, NEW)
        every, chars = rows['th_TH']
        self.assertTrue(every)
        self.assertLessEqual(set(range(0x0E40, 0x0E45)), set(chars))
        self.assertEqual(r.differs['th_TH'], (len(chars), True))

    def test_an_edited_instruction_says_every_rule_can_change(self):
        """Backlog 13.36: a changed reorder-after, collating-symbol,
        order_start or conditional inside LC_COLLATE moves rules on lines
        that did not change. or_IN over 2.28..2.34 (it stopped holding its
        own table and copies the common one), and th_TH, with its alias
        thai, over 2.34..2.39. The others name only rules."""
        for old, new, want in ((OLD, MID, ['or_IN']),
                               (MID, NEW, ['th_TH', 'thai'])):
            with self.subTest(pair=f'{old}..{new}'):
                r, rows = self.characters(old, new)
                self.assertEqual(want, sorted(n for n, (_, every) in
                                              r.differs.items() if every))
                self.assertEqual(want, sorted(n for n, (every, _) in
                                              rows.items() if every))

    def test_every_locale_that_differs_on_the_audited_pairs_names_some(self):
        """Measured: all seven name at least one character, on the line or
        in the file, and the file and the line agree on how many, so on
        these two pairs "not known" or "name no character" is a regression
        rather than a finding."""
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                r, rows = self.characters(old, new)
                self.assertEqual(sorted(rows), sorted(r.differs))
                for name, (chars, _) in r.differs.items():
                    self.assertTrue(chars, f'{name} names no character')
                    self.assertEqual(
                        len(chars) if isinstance(chars, list) else chars,
                        len(rows[name][1]))


@needs_clone
def _harness_scripts():
    from _harness import SCRIPTS_DIR
    return SCRIPTS_DIR


@needs_floor_pair
class BelowTheOldVersionFloor(StepRun):
    """glibc-2.12 -> glibc-2.17, the pair docs/scope.md names.

    Not an audited pair and not a published verdict: it is the pair that
    demonstrated the pre-2.24 failure, and after that failure was fixed it is
    what shows the fix reaches. These numbers are asserted because the last set
    published for this pair went stale silently -- it said step 4
    reported 2 exposed locales long after a partial fix had moved that to 277,
    and nothing caught it. That is the whole reason for this class.

    The mechanism: in glibc 2.23 and earlier the three master templates open
    with LC_COLLATE at byte 0. While collate_block could not read those, they
    never entered the copy graph and everything inheriting from them looked
    unaffected.
    """

    def report(self):
        return Step2Report(self.step('filter_lc_collate_changes.py',
                                     FLOOR_OLD, FLOOR_NEW))

    def test_the_three_templates_are_in_the_copy_graph(self):
        """The root cause, asserted directly rather than through a count.
        These three are the highest fan-in files in the corpus; dropping them
        is what collapsed the closure."""
        import glibc_tree
        tree = glibc_tree.Tree.at_tag(GLIBC_CLONE, FLOOR_NEW)
        for tmpl in ('iso14651_t1', 'iso14651_t1_common',
                     'iso14651_t1_pinyin'):
            self.assertTrue(tree.defines(tmpl),
                            f'{tmpl} defines no LC_COLLATE at {FLOOR_NEW} '
                            f'to the copy chains step 2 walks; a block '
                            f'at byte 0 is not read any more')

    def test_each_side_is_read_with_its_own_localedef(self):
        """Step 2 compares the old tag's files read with the old tag's
        localedef against the new tag's read with the new one's: a side read
        with the other side's reader is not what either machine builds
        (before 2.36, 0x1A and 0xFF end a file; false-negative-reviewer,
        15.2 B3). Spied on Tree.at_tag, the one door to a tree."""
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-floor-tree-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import glibc_tree\n"
                "real = glibc_tree.Tree.at_tag.__func__\n"
                "def spy(cls, repo, tag, build=None, *a, **k):\n"
                "    print('TREE', sys._getframe(1).f_code.co_name, tag,\n"
                "          build.label if build else None)\n"
                "    return real(cls, repo, tag, build, *a, **k)\n"
                "glibc_tree.Tree.at_tag = classmethod(spy)\n"
                "import filter_lc_collate_changes as f\n"
                "sys.exit(f.main([%r, %r]))\n"
                % (SCRIPTS_DIR, FLOOR_OLD, FLOOR_NEW))
        p = subprocess.run([sys.executable, '-c', code], cwd=SCRIPTS_DIR,
                           capture_output=True, timeout=600,
                           env=dict(os.environ, PG_GLIBC_AUDIT_OUT=out_dir))
        out = (p.stdout + p.stderr).decode('utf-8', 'replace')
        self.assertEqual(0, p.returncode, out[-2000:])
        # The label last and whole: an RHEL build's has blanks in it.
        trees = re.findall(r'^TREE (\S+) (\S+) (.+)$', out, re.M)
        self.assertEqual([(FLOOR_OLD, FLOOR_OLD), (FLOOR_NEW, FLOOR_NEW)],
                         [(t, b) for c, t, b in trees if c == '_main'],
                         out[-2000:])
        self.assertEqual([], [t for t in trees if t[1] != t[2]])

    def test_the_totals_and_the_17_runs_the_reader_cannot_settle(self):
        """reorder-sections-after, which the reader does not settle, is
        reached from am_ET and ber_MA by 17 locales at both tags. Step 2
        cannot compare them, so it lists them for Reindex under a `!!` of
        its own, the reason beside each group (backlog 15.2). wal_ET, cleared as "does not touch
        LC_COLLATE" before step 2 read with the reader (15.2 B3), is one."""
        r = self.report()
        self.assertEqual(r.totals, (357, 52, 262, 43))
        self.assertEqual(len(r.listed), 17)
        self.assertEqual(r.listed['wal_ET'], 'both sides: am_ET:584: '
                                             'reorder-sections-after in '
                                             'LC_COLLATE')
        self.assertEqual(r.listed['ber_MA'], 'both sides: ber_MA:75: '
                                             'reorder-sections-after in '
                                             'LC_COLLATE')
        self.assertLessEqual(set(r.listed), set(r.reindex))
        # Indented past audit.sh's relay of `!!` blocks, which keeps the
        # lines under one that start with three spaces: the names reach the
        # summary, not only the count.
        self.assertRegex(r.out, r'(?m)^     ber_MA: both sides: ber_MA:75: '
                                r'reorder-sections-after in LC_COLLATE$')

    def test_one_commit_at_2_12_lists_nothing_it_doubts(self):
        """One commit compared with itself compares nothing, and says so
        before anything else: the 17 locales the reader cannot settle at
        glibc-2.12 are not listed, nor is anything else, because nothing
        was compared, which the summary says."""
        r = Step2Report(self.step('filter_lc_collate_changes.py', FLOOR_OLD,
                                  FLOOR_OLD))
        self.assertEqual(r.nothing, 'the two tags are one commit.')
        self.assertEqual((r.listed, r.reindex), ({}, []))

    def test_the_26_files_2_12_lacks_are_not_compared_and_not_listed(self):
        """A file the old tag lacks is not compared: there are no old rules
        to compare with. Step 3 used to list ayc_PE, sw_KE and the 24 others
        for Reindex because their files changed (were added); step 2 names
        them, with the note that a distro may have shipped them earlier.
        The note's two halves are both asserted."""
        r = self.report()
        self.assertEqual(len(r.added), 26)
        for name in ('ayc_PE', 'sw_KE', 'yue_HK'):
            self.assertIn(name, r.added)
            self.assertNotIn(name, r.reindex)
        self.assertEqual(set(r.added) & set(r.reindex), set())
        self.assertIn('Confirm with `locale -a` on the OLD machine',
                      flat(r.out))

    def test_every_rule_can_change_in_each_of_the_262(self):
        """At glibc 2.12 and 2.17 iso14651_t1_common changed, and every
        locale whose rules differ copies it: every rule can change in each,
        and each names its characters in the file rather than on the
        line."""
        r = self.report()
        self.assertEqual(len(r.differs), 262)
        self.assertEqual({every for _, every in r.differs.values()}, {True})
        self.assertEqual(
            [n for n, (chars, _) in r.differs.items()
             if not isinstance(chars, int) or chars < 1], [])

    def test_zh_CN_GB18030_names_206_characters_GB18030_changed(self):
        """Backlog 13.7, PR B: the one pinned pair where a charmap change
        reaches a build's rules. GB18030 changes 12,384 characters, and the
        rules zh_CN.GB18030 reads name 206 of them, all in
        iso14651_t1_common. zh_CN's rules differ anyway, so Reindex lists it
        already: counted, not repeated, and nothing goes to the keyed list.
        With step 2's own changed files as the skip, instead of what Reindex
        lists, the time-gate prototype listed 287 sources here."""
        out = self.step('filter_lc_collate_changes.py', FLOOR_OLD, FLOOR_NEW)
        self.assertIn('GB18030: 12352 characters added, 24 removed, 8 given '
                      'other bytes. UTF-8: 4357 characters added.', flat(out))
        self.assertRegex(flat(out), r'Reindex lists anyway, whose LC_COLLATE '
                                    r'names one of those characters or '
                                    r'could not be compared \(1\): '
                                    r'zh_CN\.GB18030 \(GB18030\): 206 '
                                    r'characters, named in iso14651_t1_common')
        self.assertIn('zh_CN', Step2Report(out).reindex)
        path = os.path.join(
            self.out_dir,
            f'step2_keyed_locales.{g.pair_slug(FLOOR_OLD, FLOOR_NEW)}.txt')
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), '')

    def test_reindex_writes_277_names_and_leaves_two_aliases_out(self):
        """277 since step 9.2: the 262 that differ and the 17 not compared,
        less the two non-ASCII aliases, bokm\\xe5l of nb_NO and
        fran\\xe7ais of fr_FR, in Latin-1 bytes in glibc-2.17's
        locale.alias: PostgreSQL never imports such a name as a collation.
        They are not written, and the step says so rather than dropping
        them in silence or writing a name with U+FFFD in it. Step 3's
        figure was 280 locales and 24 aliases; 11 with the three roots
        missing. What SUPPORTED does not build is listed and named under
        its own note."""
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_step('filter_lc_collate_changes.py', FLOOR_OLD,
                           FLOOR_NEW, out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(len(r.reindex), 277)
        # The two print escaped, as a terminal can carry them.
        self.assertEqual(sorted(r.reindex),
                         sorted((set(r.differs) | set(r.listed))
                                - set(r.not_ascii)))
        with open(os.path.join(
                out_dir, f'step2_changed_collate.'
                         f'{g.pair_slug(FLOOR_OLD, FLOOR_NEW)}.txt'),
                encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertEqual(written, r.reindex)
        self.assertTrue(all(n.isascii() for n in written))
        self.assertEqual(r.not_ascii, ["'bokm\\ufffdl'", "'fran\\ufffdais'"])
        self.assertEqual(r.unbuilt, ['el_GR@euro', 'iso14651_t1',
                                     'iso14651_t1_common',
                                     'iso14651_t1_pinyin'])
        for name in ('en_US', 'de_DE', 'fr_FR', 'es_ES', 'it_IT', 'nl_NL',
                     'pt_BR', 'ru_RU', 'sv_SE', 'zh_CN', 'zh_TW', 'ky_KG',
                     'uk_UA'):
            self.assertIn(name, r.reindex, f'{name} is not listed')


@needs_clone
class EveryMeasuredNameReachesItsLocale(unittest.TestCase):
    """glibc's rule, checked against every libc collation PostgreSQL imported
    on the three machines (tests/locale_order/*.pg_collation.txt): each name,
    through that tag's locale.alias when it is an alias, is a locale source
    file of the machine's tag, or one of the two the distro adds (C before
    glibc 2.35, en_US@ampm on RHEL8). A name the rule cannot take back to a
    file is a name the Reindex list cannot cover (backlog 13.1)."""

    MACHINES = (('rhel8', OLD), ('rhel9', MID), ('rhel10', NEW))
    DISTRO_ONLY = {'C', 'en_US@ampm'}

    def test_every_collation_name_reaches_a_locale_file(self):
        for machine, tag in self.MACHINES:
            files = {os.path.basename(p)
                     for p in g.list_locale_files(GLIBC_CLONE, tag)}
            aliases = g.locale_aliases(GLIBC_CLONE, tag)
            with open(os.path.join(REPO_ROOT, 'tests', 'locale_order',
                                   f'{machine}.pg_collation.txt'),
                      encoding='utf-8') as fh:
                names = {ln.rstrip('\n').split('|')[2] for ln in fh if ln.strip()}
            with self.subTest(machine=machine, tag=tag):
                self.assertGreater(len(names), 800)
                lost = sorted(n for n in names
                              if g.locale_source(aliases.get(n, n))
                              not in files | self.DISTRO_ONLY)
                self.assertEqual(lost, [])


@needs_clone
class SkippingAReleaseReportsTheUnion(StepRun):
    """glibc-2.28 -> glibc-2.39, the pair that leaves 2.34 out.

    Not an audited pair and not a published verdict. It is the pair the claim
    that an upgrade skipping a release is one audit is measured on.

    The claim under test is a SET equality, not a count: a direct jump reports
    what the two steps between it report, name for name, except where a test
    below names the difference. A count would pass on the right total reached
    the wrong way -- five names of which one is wrong is still five. The README
    used to call the audited pairs "the two adjacent upgrades", which reads as
    a restriction; there is no adjacency check anywhere in the tool, and this
    class is what keeps that true.
    """

    def _step2(self, old, new):
        """Step 2's report for the pair, read from stdout rather than from
        step2_changed_collate.*.txt: run_step is memoised and a cached call
        does not rewrite its files, so a second test reading that path could
        assert on a previous pair's list."""
        return Step2Report(self.step('filter_lc_collate_changes.py', old,
                                     new))

    def _step2_names(self, old, new):
        """The names whose rules step 2 says differ."""
        return set(self._step2(old, new).differs)

    def test_step_2_is_the_exact_union_of_the_two_steps(self):
        first, second = self._step2_names(OLD, MID), self._step2_names(MID, NEW)
        direct = self._step2_names(OLD, NEW)
        self.assertEqual(first, {'or_IN', 'sv_FI', 'sv_FI@euro', 'sv_SE',
                                 'swedish'})
        self.assertEqual(second, {'th_TH', 'thai'})
        self.assertEqual(
            direct, first | second,
            'the direct pair no longer reports the union of the two steps: '
            f'missing {sorted(first | second - direct)}, '
            f'extra {sorted(direct - (first | second))}')

    def test_reindex_is_the_exact_union_of_the_two_steps(self):
        """The reindex verdict, which is what a reader acts on. Each side is
        compared with every copy followed at both of its tags, which is why
        sv_FI and sv_FI@euro, which copy sv_SE, are on both lists. The list
        is locales and their aliases (backlog 13.1)."""
        first = set(self._step2(OLD, MID).reindex)
        second = set(self._step2(MID, NEW).reindex)
        direct = set(self._step2(OLD, NEW).reindex)
        self.assertEqual(first, {'or_IN', 'sv_FI', 'sv_FI@euro', 'sv_SE',
                                 'swedish'})
        self.assertEqual(second, {'th_TH', 'thai'})
        self.assertEqual(
            direct, first | second,
            'the direct pair no longer reports the union: '
            f'missing {sorted(first | second - direct)}, '
            f'extra {sorted(direct - (first | second))}')
        self.assertEqual(len(direct), 7)

    def test_every_name_the_machines_measured_as_changed_is_on_the_list(self):
        """Backlog 13.1, the defect itself. Step 11 measured on the three
        fixtures which names sort differently (tests/test_locale_order.py):
        for RHEL8 -> RHEL9 that included sv_SE.iso885915, swedish and
        or_IN.utf8, which no line of the tags-only list reached. Each one now
        reaches it, as a locale by glibc's rule or as an alias. Two names are
        on no Reindex list by design, and this pins exactly those two:
        ko_KR.utf8 moved through localedef's code with its file unchanged,
        which the `!!` on the code under 'What was compared' says no step
        compares (step 4's list named it until step 9.3); C.utf8 is the
        distro's own file, which no tag holds, as the `!!` on the tags
        there says."""
        lists = ((tlo.CHANGED_8_TO_9, set(self._step2(OLD, MID).reindex),
                  {'C.utf8', 'ko_KR.utf8'}),
                 (tlo.CHANGED_9_TO_10, set(self._step2(MID, NEW).reindex),
                  set()))
        for measured, listed, elsewhere in lists:
            with self.subTest(listed=sorted(listed)):
                missed = {n for n in measured
                          if n not in listed and g.locale_source(n) not in listed}
                self.assertEqual(missed, elsewhere)
        # What covers ko_KR.utf8 now: the pair's sides say, as a `!!`,
        # that localedef's code is compared by no step (test_wrapper holds
        # the summary to repeating it).
        self.assertIn("!! Not compared on any route: the code that compiles "
                      "and compares the rules (localedef's and strcoll's,",
                      flat(self.step('glibc_locale_data.py', 'sides', OLD,
                                     MID)))

    def test_only_a_rename_differs_in_the_two_steps_and_not_end_to_end(self):
        """The one thing a two-endpoint diff structurally cannot see is a
        change that is undone before the end. This asserts 350 and 349 for this
        span, and names the single exception,
        because "no locale changed and changed back" is the sentence the whole
        section rests on."""
        def changed(old, new):
            p = g.run_git(['diff', '--name-only', '--find-renames',
                           f'{old}..{new}', '--', 'localedata/locales/'],
                          GLIBC_CLONE)
            out = p.stdout.decode('utf-8', 'replace')
            return {ln for ln in out.split('\n') if ln.strip()}

        either = changed(OLD, MID) | changed(MID, NEW)
        end_to_end = changed(OLD, NEW)
        self.assertEqual(len(either), 350)
        self.assertEqual(len(end_to_end), 349)
        self.assertEqual(
            sorted(either - end_to_end),
            ['localedata/locales/aa_ER@saaho'],
            'a locale file now changes in one step and is back to its old '
            'content by the end')
        self.assertEqual(
            end_to_end - either, set(),
            'the direct pair reports a file neither step does, which a '
            'two-endpoint diff cannot do')

    def test_no_locale_is_added_on_the_way_and_then_changed(self):
        """The second class a direct pair reports differently, asserted so that
        a future
        union failure names its own cause.

        A file ADDED between 2.28 and 2.34 and then changed inside LC_COLLATE
        between 2.34 and 2.39 is in the second step's reindex list, and in the
        direct run it is named as not compared for want of a file at
        glibc-2.28 instead -- named either way, never silent, but under a
        different heading, so the union would break. Two files fit the
        first half on this triple (ckb_IQ, mnw_MM) and neither changed inside
        LC_COLLATE. Without this, that day's failure would read as a lost
        locale.
        """
        p = g.run_git(['diff', '--name-only', '--diff-filter=A',
                       f'{OLD}..{MID}', '--', 'localedata/locales/'],
                      GLIBC_CLONE)
        added = {ln.rsplit('/', 1)[1]
                 for ln in p.stdout.decode('utf-8', 'replace').split('\n')
                 if ln.strip()}
        self.assertTrue(added, 'no file is added over 2.28..2.34, which '
                               'contradicts step 2 reporting two additions')
        self.assertEqual(
            added & self._step2_names(MID, NEW), set(),
            'a locale added on the way is now also changed inside LC_COLLATE '
            'by the end; the union tests above will fail, and this is why')


@needs_clone
class CharmapOnlyIsTheCharmaps(unittest.TestCase):
    """CHARMAP_ONLY is what glibc's charmaps define at the pinned tags.

    Freezes the list of backlog 16.34 against its source: every sequence of
    more than one byte that the charmap of an encoding gives a character at
    glibc-2.28, 2.34 or 2.39, read by collation_keys.read_charmap (the
    repository's model of charmap.c: a range counted up big-endian from its
    first bytes), that the encoding's own list lacks: for UTF-8 those that
    are not UTF-8, for the four EUC encodings those outside
    accepted_sequences, for every other charmap of more than one byte that
    Python has a codec for every one. A list kept by hand that drifts from the charmaps, or a reader
    that walks a range otherwise, fails here.
    """

    TAGS = (OLD, MID, NEW)
    ENCODINGS = ('UTF-8', 'EUC-JP', 'EUC-KR', 'GB2312', 'EUC-TW', 'BIG5',
                 'BIG5-HKSCS', 'CP949', 'EUC-JISX0213', 'GB18030', 'GBK',
                 'JOHAB', 'SHIFT_JIS', 'SHIFT_JISX0213', 'WINDOWS-31J')

    def charmaps(self, name):
        import collation_keys as ck
        for tag in self.TAGS:
            text = subprocess.run(
                ['git', 'show', f'{tag}:localedata/charmaps/{name}'],
                cwd=GLIBC_CLONE, capture_output=True, check=True).stdout
            yield ck.read_charmap(text.decode('latin-1'), name)

    def test_every_multibyte_charmap_with_a_codec_has_a_list(self):
        """Every charmap glibc ships at the three tags with sequences of more
        than one byte that Python has a codec for is UTF-8, one of the four
        EUC whose lists are their own, or in ENCODINGS, under its
        <code_set_name>. A charmap is read when it declares <mb_cur_max>
        over 1 or Python has a codec of its name; one collation_keys cannot
        read must have no codec (ANSI_X3.110-1983, EBCDIC-PT), or it would be
        a member nobody checked."""
        import collation_keys as ck
        multi, unread = set(), set()
        for tag in self.TAGS:
            arc = subprocess.run(['git', 'archive', tag, 'localedata/charmaps'],
                                 cwd=GLIBC_CLONE, capture_output=True,
                                 check=True).stdout
            with tarfile.open(fileobj=io.BytesIO(arc)) as tf:
                files = {os.path.basename(i.name): tf.extractfile(i).read()
                         for i in tf.getmembers() if i.isfile()}
            self.assertGreater(len(files), 150)
            for name, raw in sorted(files.items()):
                body = raw.decode('latin-1')
                declared = re.search(r'^<mb_cur_max>\s+(\d+)', body, re.M)
                try:
                    tlo.m.codecs.lookup(name)
                    codec = True
                except LookupError:
                    codec = False
                if not codec and not (declared and int(declared[1]) > 1):
                    continue
                try:
                    cm = ck.read_charmap(body, name)
                except ck.Unsettled:
                    unread.add(name)
                    continue
                if (any(len(h.split('/x')) > 2 for h in cm.singles.values())
                        or any(size > 1 for *_, size in cm.ranges)):
                    multi.add(cm.code_set_name)
        self.assertFalse({n for n in unread if self.has_codec(n)})
        with_codec = {n for n in multi if self.has_codec(n)}
        # EUC-TW has no codec, and its own list (accepted_sequences).
        self.assertEqual(with_codec, set(self.ENCODINGS) - {'EUC-TW'})
        self.assertEqual(set(self.ENCODINGS) - {'EUC-JP', 'EUC-KR', 'GB2312'},
                         set(tlo.m.CHARMAP_ONLY))

    @staticmethod
    def has_codec(name):
        try:
            tlo.m.codecs.lookup(name)
            return True
        except LookupError:
            return False

    def test_ascii_below_80_where_named_so(self):
        """The encodings Python has no codec for that the comparison names
        bytes 01 to 7F of as ASCII: glibc's charmaps give each of those
        bytes its own code point, at the three tags."""
        for name in sorted(tlo.m.ASCII_BELOW_80):
            with self.subTest(charmap=name):
                with self.assertRaises(LookupError):
                    tlo.m.codecs.lookup(name)
                for cm in self.charmaps(name):
                    one = {}
                    for cp, hexed in cm.singles.items():
                        b = bytes(int(h, 16) for h in hexed.split('/x')[1:])
                        if len(b) == 1:
                            one.setdefault(b[0], set()).add(cp)
                    for lo, hi, first, size in cm.ranges:
                        if size == 1:
                            for k in range(hi - lo + 1):
                                one.setdefault(first + k, set()).add(lo + k)
                    self.assertEqual([one.get(v) for v in range(1, 0x80)],
                                     [{v} for v in range(1, 0x80)])

    def sequences(self, name):
        import collation_keys as ck
        out = set()
        for tag in self.TAGS:
            text = subprocess.run(
                ['git', 'show', f'{tag}:localedata/charmaps/{name}'],
                cwd=GLIBC_CLONE, capture_output=True, check=True).stdout
            out |= ck.sequences(ck.read_charmap(text.decode('latin-1'),
                                                name))
        return {s for s in out if len(s) > 1}

    def test_the_list_is_rebuilt_from_the_charmaps(self):
        def utf8(s):
            try:
                s.decode('utf-8')
                return True
            except UnicodeDecodeError:
                return False
        counts = {}
        for name in self.ENCODINGS:
            with self.subTest(encoding=name):
                found = self.sequences(name)
                if name == 'UTF-8':
                    lacking = {s for s in found if not utf8(s)}
                elif name in tlo.m.EUC_CODESETS:
                    lacking = found - set(tlo.m.accepted_sequences(name))
                else:
                    lacking = found
                self.assertEqual(set(tlo.m.charmap_only(name)), lacking)
                self.assertEqual(tlo.m.charmap_only(name), sorted(lacking))
                counts[name] = len(lacking)
        # The counts the backlog item names, so that an empty list on both
        # sides cannot pass for a rebuilt one.
        self.assertEqual({k: v for k, v in counts.items() if v},
                         {'UTF-8': 9983, 'EUC-TW': 6721, 'BIG5': 13901,
                          'BIG5-HKSCS': 18451, 'CP949': 17048,
                          'EUC-JISX0213': 11271, 'GB18030': 244913, 'GBK': 21791,
                          'JOHAB': 17049, 'SHIFT_JIS': 6879,
                          'SHIFT_JISX0213': 11208, 'WINDOWS-31J': 9206})


if __name__ == '__main__':
    unittest.main()
