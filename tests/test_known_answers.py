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
                       default_weight_section, flat, needs_clone,
                       needs_floor_pair, run_step, step2_characters_file)

import glibc_locale_data as g
import test_locale_order as tlo


def one_int(pattern, text, what):
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"could not find {what} in output:\n{text[:800]}")
    return int(m.group(1))


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
class Step1Templates(StepRun):
    def test_each_template_gets_an_explicit_verdict(self):
        """"audit-locale-diff.sh reported 'unchanged' as empty output", which
        is indistinguishable from an error."""
        out = self.step('audit-locale-diff.sh', MID, NEW)
        for tmpl in ('iso14651_t1', 'iso14651_t1_common', 'iso14651_t1_pinyin'):
            m = re.search(rf'^\s+{tmpl}\s+(CHANGED|UNCHANGED|ABSENT)',
                          out, re.M)
            self.assertIsNotNone(m, f'no verdict printed for {tmpl}')

    def test_fan_in_names_iso14651_t1_not_only_common(self):
        """It used to hardcode iso14651_t1_common as 'the master table' and
        miss that iso14651_t1 is the one hundreds of locales inherit."""
        out = self.step('audit-locale-diff.sh', MID, NEW)
        self.assertRegex(out, r'\d+ locales inherit from iso14651_t1\b')

    def test_the_blast_radius_published_for_both_tags(self):
        """The docs say iso14651_t1 is inherited by 328 locales at 2.34, and
        the examples print 338 at 2.39. Nothing asserted either against the
        clone -- which is how the step-4 "2" rotted. Step 1 computes the radius
        at the NEW tag. Both transcripts of each pair are read as well, so a
        figure changed here without regenerating them goes red, and
        test_published_claims holds the pages that state 328 to those
        transcripts."""
        for old, new, radius, pair in ((OLD, MID, 328, 'rhel8-to-rhel9'),
                                       (MID, NEW, 338, 'rhel9-to-rhel10')):
            with self.subTest(tag=new):
                line = f'\n   {radius} locales inherit from iso14651_t1\n'
                out = self.step('audit-locale-diff.sh', old, new)
                self.assertIn(line, out)
                for kind in ('audit-output', 'tags-only'):
                    name = f'examples/{pair}-{kind}.txt'
                    with self.subTest(transcript=kind):
                        with open(os.path.join(REPO_ROOT, *name.split('/')),
                                  encoding='utf-8') as f:
                            # The tool may append its "<== has some
                            # change" mark to the line; a copy carrying it
                            # still counts.
                            said = re.findall(
                                r'(?m)^ +(\d+) locales inherit from '
                                r'iso14651_t1(?:  <==[^\n]*)?$', f.read())
                        self.assertEqual(
                            said, [str(radius)],
                            f'{name} prints {said} as the locales that '
                            f'inherit iso14651_t1, not [{radius}]; '
                            f'regenerate it')

    def test_the_changed_file_count_published_for_the_pair(self):
        """283 is published for glibc 2.28 -> 2.34, and test_published_claims
        holds the pages that state it to the rhel8-to-rhel9 tags-only
        transcript. This holds that transcript, and the figure, to the run: a
        count that moved without regenerating it goes red, as the blast
        radius does. Step 1 prints it; step 2 printed it too until it became
        the comparison of rules (9.2). The extended transcript compares each
        machine's build in steps 1 to 5 (7.6a)."""
        label = r'(?m)^Locale files added, modified or deleted: (\d+)$'
        out = self.step('audit-locale-diff.sh', OLD, MID)
        self.assertEqual(re.findall(label, out), ['283'])
        name = 'examples/rhel8-to-rhel9-tags-only.txt'
        with open(os.path.join(REPO_ROOT, *name.split('/')),
                  encoding='utf-8') as f:
            said = re.findall(label, f.read())
        self.assertEqual(said, ['283'],
                         f'{name} prints {said} as the locale files added, '
                         f'modified or deleted between glibc-2.28 and '
                         f'glibc-2.34, not [283]; regenerate it')


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
        extended ones compare each machine's build (7.6a)."""
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

    def test_c_utf8_is_one_more_file_between_the_tags(self):
        """No list of what a distro ships (backlog 13.6, 7.7): over 2.28..2.34
        C is in neither tag, so step 2 says nothing of it and step 1's `!!`
        says what a tag does not hold; over 2.34..2.39 upstream adds it, and
        it is named with the other files not compared for want of an old
        one, whose sentence says an added file can still exist on the old
        system, under the name `locale -a` shows. No `!!` says C.UTF-8's
        order can change: the old tag cannot say that, and on RHEL 9 and 10
        it does not."""
        for (old, new), added in (((OLD, MID), None), ((MID, NEW), 'C.utf8')):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                r = Step2Report(out)
                self.assertNotIn('!!', out)
                self.assertNotIn('C.UTF-8', out)
                self.assertEqual(r.added.get('C'), added)
                self.assertNotIn('C', r.differs)
                self.assertNotIn('C', r.reindex)
                if added:
                    self.assertIn('Confirm with `locale -a` on the OLD '
                                  'machine', flat(out))

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
        characters file included (step 9.2 added it)."""
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
            + [f'step2_rules_characters.{slug}.tsv']))

    def test_what_step_2_does_not_compare_is_said_once(self):
        """NOT_COMPARED_HERE: the compiler and strcoll are step 5's, said in
        one sentence, once."""
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
class Step4AlgorithmicRanges(StepRun):
    def test_run_alone_its_hint_names_the_byte_order_locales(self):
        """Backlog 13.7: the hint that sends the reader to step 5 covers the
        locales this step names as built in byte order, which it leaves off
        its list. At glibc-2.39 that is C."""
        out = flat(self.step('flag_algorithmic_ranges.py', NEW))
        self.assertIn('Declare codepoint_collation, so no expansion change '
                      'can move them: C', out)
        self.assertIn('if it did, test these, and any locale named above as '
                      'built in byte order, empirically', out)

    def test_four_locales_use_ellipsis_ranges(self):
        for tag in (MID, NEW):
            with self.subTest(tag=tag):
                out = self.step('flag_algorithmic_ranges.py', tag)
                self.assertEqual(
                    one_int(r'ellipsis \(algorithmic\) ranges: (\d+)', out,
                            'the count'), 4)

    def test_the_four_are_named(self):
        out = self.step('flag_algorithmic_ranges.py', NEW)
        for name in ('i18n', 'iso14651_t1', 'iso14651_t1_common', 'ko_KR'):
            self.assertRegex(
                out, re.compile(rf'^  {re.escape(name)}$', re.M))

    def test_zh_cn_is_flagged_as_exposed(self):
        """"Step 4 cleared zh_CN and three siblings that it should have
        flagged". zh_CN reaches iso14651_t1_common through
        iso14651_t1_pinyin, whose exposure is an inline ellipsis."""
        out = self.step('flag_algorithmic_ranges.py', NEW)
        for name in ('zh_CN', 'cmn_TW', 'iso14651_t1_pinyin',
                     'cns11643_stroke'):
            self.assertIn(name, out)

    def test_the_exposed_total_is_unchanged(self):
        """342 at MID, as the examples print it. Pinned here because what
        decides which files define LC_COLLATE has changed twice (collate_block
        to collate_text, then glibc_tree), and the two readers first differed
        on files that open with it."""
        out = self.step('flag_algorithmic_ranges.py', MID)
        self.assertIn('Full set needing empirical confirmation: 342 locale(s)',
                      out)
        self.assertIn('of which 342 define LC_COLLATE', out)

    def test_each_way_in_is_counted_apart(self):
        """The total now takes in every locale but one declaring
        codepoint_collation, so it no longer moves when a detection fails: a
        locale the ellipsis scan or the copy walk stops reaching lands in the
        default-weight section and the total stays put. Each part is pinned on
        its own so that a failure moves a number something reads."""
        expected = {
            MID: (331, ['POSIX', 'ar_SA', 'ja_JP', 'km_KH', 'lo_LA', 'sl_SI',
                        'th_TH']),
            NEW: (341, ['POSIX', 'ar_SA', 'ja_JP', 'km_KH', 'lo_LA', 'sl_SI']),
        }
        for tag, (inherited, default) in expected.items():
            with self.subTest(tag=tag):
                out = self.step('flag_algorithmic_ranges.py', tag)
                ellipsis = one_int(r'ellipsis \(algorithmic\) ranges: (\d+)',
                                   out, 'the ellipsis count')
                self.assertEqual(
                    one_int(r'exposed via `copy` inheritance: (\d+)', out,
                            'the copy count'), inherited)
                self.assertEqual(default_weight_section(out),
                                 (len(default), default))
                self.assertEqual(
                    one_int(r'Full set needing empirical confirmation: (\d+) '
                            r'locale', out, 'the total'),
                    ellipsis + inherited + len(default))

    def test_a_locale_no_range_reaches_is_in_the_written_list(self):
        """Backlog 1.6. ja_JP and ar_SA use no ellipsis range and copy nothing
        that does, and every character they do not list takes localedef's
        default weight. ar_SA shows the list follows that rule, not the
        keyword, because its LC_COLLATE never writes UNDEFINED. The control is
        C, which declares codepoint_collation at this tag and so stays out."""
        contents, missing = g.read_blobs(GLIBC_CLONE, NEW,
                                         [f'{g.LOCALES_DIR}/ar_SA'])
        self.assertEqual(missing, set())
        block = g.collate_text(contents[f'{g.LOCALES_DIR}/ar_SA'])
        self.assertIsNotNone(block)
        self.assertNotIn('UNDEFINED', block)
        # Its own directory: MID and NEW write the same file name, and the
        # memoised steps of this class would leave whichever ran last.
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_step('flag_algorithmic_ranges.py', NEW, out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        named = re.search(r'full list \((\d+) name\(s\)\): (\S+)', out)
        self.assertIsNotNone(named, out)
        with open(named.group(2), encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertEqual(int(named.group(1)), len(written))
        for name in ('ja_JP', 'ar_SA'):
            self.assertIn(name, written)
        self.assertNotIn('C', written)
        # POSIX is listed by its source name, among the files SUPPORTED does
        # not build. Not "templates": nothing copies POSIX.
        self.assertIn('  not in SUPPORTED (not built by default): POSIX, ',
                      out)
        self.assertIn('POSIX', written)

    def test_the_upstream_C_is_byte_order_from_2_35(self):
        """localedata/locales/C exists upstream from glibc 2.35 and declares
        codepoint_collation, so nothing localedef does to ranges can move
        C.UTF-8 there. Said out loud rather than left unflagged: unflagged and
        cleared look identical on a terminal."""
        out = self.step('flag_algorithmic_ranges.py', NEW)
        self.assertIn('Declare codepoint_collation, so no expansion change '
                      'can move them: C', out)
        self.assertNotRegex(out, re.compile(r'^  C$', re.M))

    def test_the_prose_above_the_keyword_does_not_count(self):
        """Driven by the real file, not a fixture. glibc-2.39's C names
        codepoint_collation in a comment above declaring it, so a
        substring search reads the comment as the declaration -- and would
        then report RHEL8's ellipsis-based C as byte order."""
        path = f'{g.LOCALES_DIR}/C'
        contents, missing = g.read_blobs(GLIBC_CLONE, NEW, [path])
        self.assertEqual(missing, set())
        text = contents[path]
        self.assertIn("The keyword 'codepoint_collation'", text)
        self.assertEqual(g.classify_collation_style(text), 'codepoint')
        # And the prose alone, with the declaration removed, must not.
        prose_only = text.replace('\ncodepoint_collation', '\n% removed')
        self.assertNotEqual(g.classify_collation_style(prose_only), 'codepoint')


@needs_clone
class Step5CollationCode(StepRun):
    def test_a_hunk_count_asks_for_the_byte_order_locales_too(self):
        """Backlog 13.7. Step 4 leaves the locales glibc builds in byte order
        off its list, and the code compares them on a branch of its own, so
        a code change asks for them too. Run alone, the step names the script
        that lists them."""
        out = flat(self.step('diff_collation_code.py', MID, NEW))
        self.assertIn('read them and decide whether they can change an order.',
                      out)
        self.assertIn('needs an empirical sort-order test, however clean its '
                      'data diff is, and so does every locale that script '
                      'names as built in byte order.', out)

    def test_substantive_hunk_totals(self):
        """24 and 52 since the noise filter reads the context lines: two hunks
        that are comment on both sides -- localedef.c @@ -226,7 +232,8 @@ and
        strcoll_l.c @@ -104,7 +103,7 @@ -- opened their comment on a context
        line and were counted as substantive. 29 and 55 since ld-ctype.c
        joined TIER 2 for find_translit (backlog 13.7): five and three hunks,
        none in the translit code."""
        for (old, new), expected in (((OLD, MID), 29), ((MID, NEW), 55)):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('diff_collation_code.py', old, new)
                self.assertEqual(
                    one_int(r'(\d+) substantive hunk\(s\) found', out,
                            'the total'), expected)

    def test_code_after_a_comment_that_closes_in_context_is_marked(self):
        """`>>` is meant to mark the actual code changes. It
        did not for eight lines in locale/programs/linereader.c over
        2.34..2.39: the comment above them opens on a changed line and closes
        two CONTEXT lines below, which the filter never read, so the rest of
        the hunk stayed marked as prose."""
        out = self.step('diff_collation_code.py', MID, NEW)
        marked = [ln for ln in out.split('\n')
                  if 'seq = charmap_find_value (charmap,' in ln]
        self.assertTrue(marked, 'the line is no longer in the diff')
        for ln in marked:
            self.assertIn('>>', ln, ln)

    def test_the_bug_22668_commit_is_surfaced(self):
        """"ko_KR was reported unaffected between glibc 2.28 and 2.34. It
        changes." Step 5 exists because this commit is the cause, and it is in
        the code, not in ko_KR's data file."""
        out = self.step('diff_collation_code.py', OLD, MID)
        self.assertIn('82292c99b2', out)

    def test_tier3_shows_the_hunks_the_hand_lists_missed(self):
        """#2: both of these are real changes over 2.34..2.39 that no tier
        listed."""
        out = self.step('diff_collation_code.py', MID, NEW)
        tier3 = out.split('TIER 3 --')[1]
        self.assertIn('lr_getc', tier3)
        self.assertIn('elem_hash', tier3)

    def test_weight_h_is_still_reported_under_tier1(self):
        """Control for the same change: the curated lists were kept precisely
        because the walk cannot reach weight.h, and it does change here."""
        out = self.step('diff_collation_code.py', MID, NEW)
        tier1 = out.split('TIER 2 --')[0]
        self.assertRegex(tier1, r'locale/weight\.h: \d+ substantive')

    def test_no_file_is_reported_in_two_tiers(self):
        out = self.step('diff_collation_code.py', MID, NEW)
        for path in ('locale/programs/linereader.h', 'locale/weight.h',
                     'locale/programs/ld-collate.c'):
            self.assertLessEqual(out.count(f'  {path}: '), 1, path)

    def test_c_collate_seq_is_read_not_just_its_include_line(self):
        """"Step 5 was not reading the C locale's collation data": tracking
        only ld-collate.c showed `#include "C-collate-seq.c"` and none of the
        weights behind it."""
        out = self.step('diff_collation_code.py', MID, NEW)
        self.assertIn('locale/C-collate-seq.c', out)

    def test_the_wide_char_wrappers_are_diffed_under_tier_1(self):
        """"Two entry points of step 5 were diffed by nobody": wcscoll_l.c and
        wcsxfrm_l.c were ENTRY_POINTS, in no tier, and the include walk
        subtracts its entry points -- so their existence was checked and
        their diff never read. Both pairs: named under TIER 1, with a verdict.
        """
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('diff_collation_code.py', old, new)
                tier1 = out.split('TIER 1 --')[1].split('TIER 2 --')[0]
                for path in ('wcsmbs/wcscoll_l.c', 'wcsmbs/wcsxfrm_l.c'):
                    self.assertRegex(
                        tier1, rf'(?m)^  {re.escape(path)}: (no substantive '
                               rf'change|\d+ substantive hunk)', path)

    def test_a_vanished_path_is_a_warning_the_summary_can_repeat(self):
        """"The summary contradicted step 5 when a tracked path vanished."
        Real data: reversed, 2.39 -> 2.34 loses locale/C-collate-seq.c, which
        exists only from 2.35. The notice must be a `!!` block with three-space
        continuation lines, because that is the shape audit.sh collects and
        repeats at the bottom; as plain prose it scrolled away 300 lines above
        a summary that said the opposite."""
        out = self.step('diff_collation_code.py', NEW, MID, '--allow-reverse')
        self.assertIn('\n!! 1 tracked path(s) present at glibc-2.39 and GONE '
                      'at glibc-2.34.', out)
        self.assertIn(f'\n     locale/C-collate-seq.c: ABSENT at {MID}\n', out)
        # This pair is deliberately backwards, which the step now says out
        # loud in a `!!` block of its own -- so the block under test is
        # selected by what it says, not by being the first one printed.
        self.assertIn('!! REVERSED PAIR, allowed on request', out)
        marker = '!! 1 tracked path(s)'
        block = marker + out.split(marker)[1].split('\n\n')[0]
        for line in block.split('\n')[1:]:
            self.assertTrue(line.startswith('   '), repr(line))

    def test_the_reversed_pair_needs_the_flag(self):
        """The call site, not the helper: this class runs 2.39 -> 2.34 twice
        on purpose, and both runs assert what the WARNING says. Hard-code
        allow_reverse=True in step 5 and every one of them stays green, so the
        refusal itself needs its own test -- without the flag, exit 2 and no
        report at all."""
        rc, out = run_step('diff_collation_code.py', NEW, MID,
                           out_dir=self.out_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn('This pair is REVERSED', flat(out))
        self.assertNotIn('Collation code changes between', out)

    def test_the_clean_sentence_is_the_one_the_wrapper_keys_on(self):
        """audit.sh takes exactly this sentence as step 5's clean verdict and
        treats anything else as unresolved. Neither documented pair is clean,
        so the sentence is tied to the source here: reword it in one place and
        not the other, and every run would be summarised as unresolved.
        Between builds the sentence says "... change in the sources.", and
        the pattern takes both forms (7.5b). The vanished-path variant must
        NOT say it -- that is the whole fix."""
        src = open(os.path.join(_harness_scripts(), 'diff_collation_code.py'),
                   encoding='utf-8').read()
        self.assertIn('"No substantive collation code change. ', src)
        self.assertIn('"No substantive collation code change in the sources. ',
                      src)
        audit = open(os.path.join(_harness_scripts(), '..', 'audit.sh'),
                     encoding='utf-8').read()
        self.assertIn("'^No substantive collation code change"
                      "( in the sources)?\\.'", audit)
        out = self.step('diff_collation_code.py', NEW, MID, '--allow-reverse')
        self.assertNotIn('No substantive collation code change', out)


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
                            f'to the copy chains steps 1 and 2 walk; a block '
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
        its own, the reason beside each group; step 1 says its counts may
        miss them (backlog 15.2). wal_ET, cleared as "does not touch
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
        rc, fan = run_step('glibc_locale_data.py', 'fanin', FLOOR_NEW)
        self.assertEqual(rc, 0, fan)
        self.assertEqual(
            one_int(r'(?m)^!! (\d+) locale\(s\) could not be read the way '
                    r'localedef reads them, so the counts above may miss',
                    fan, 'the count'),
            17)

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

    def test_step_5_prints_68_hunks(self):
        """It was 63 until the two wide-char wrappers joined TIER 1: each
        contributes one hunk, the 2012 FSF postal-address change, which the
        filter keeps because it cannot prove a bare licence continuation is
        prose. Conservative, and counted. 68 since ld-ctype.c joined TIER 2
        (backlog 13.7), with three hunks of its own."""
        out = self.step('diff_collation_code.py', FLOOR_OLD, FLOOR_NEW)
        self.assertEqual(
            one_int(r'(\d+) substantive hunk\(s\) found', out, 'the total'),
            68)

    def test_step_4_reaches_278_and_22(self):
        """277 was the figure before the collate_block fix and 279 after it;
        281 added ky_KG and uk_UA, which copy iso14651_t1 spelled in symbolic
        notation. The 2 docs/limitations.md used to publish was older still,
        and already wrong when it was quoted.

        278 since step 4 reads what localedef reads (backlog 15.2, 13.8):
        glibc's default comment character is `#`, and at 2.12 and 2.17
        iso14651_t1_common's only ranges are inside `#` comments, so it no
        longer counts as a range, and iso14651_t1_pinyin and zh_CN, which
        reached a range only through it, move to the default-weight section,
        19 -> 22 (measured with the fix alone on 2026-10-06: 281/19 ->
        278/22). The total stays 300.

        The sum is the ellipsis scan plus the copy walk. Since backlog 1.6
        the total adds the locales only the default weight exposes, and it
        does not move when the walk loses a locale to that section, so it is
        read from its two parts. The 17 locales whose reading meets
        reorder-sections-after at am_ET and ber_MA, which the reader does
        not settle, are named under their own `!!`."""
        out = self.step('flag_algorithmic_ranges.py', FLOOR_NEW)
        self.assertEqual(
            one_int(r'ellipsis \(algorithmic\) ranges: (\d+)', out,
                    'the ellipsis count')
            + one_int(r'exposed via `copy` inheritance: (\d+)', out,
                      'the copy count'),
            278)
        self.assertEqual(default_weight_section(out)[0], 22)
        self.assertEqual(
            one_int(r'(?m)^!! (\d+) locale\(s\) could not be read the way '
                    r'localedef reads them', out, 'the unsettled count'),
            17)
        self.assertEqual(
            one_int(r'Full set needing empirical confirmation: (\d+) locale',
                    out, 'the exposed set'),
            300)
        # Both halves of the sentence, and the file it names. Only the first
        # used to be pinned. The aliases were 445 SUPPORTED spellings, and the
        # list 451, until backlog 13.1.
        self.assertEqual(
            one_int(r'confirmation: \d+ locale\(s\), and (\d+) more', out,
                    'the aliases'),
            43)
        self.assertEqual(
            one_int(r'full list \((\d+) name\(s\)\)', out, 'the list'),
            300 + 43)
        self.assertIn('not listed: an alias of fr_FR, nb_NO whose name is not '
                      'ASCII', out)



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
        ko_KR.utf8 moved through localedef's code with its file unchanged, so
        it is on step 4's list; C.utf8 is the distro's own file, which no
        tag holds, as step 1's `!!` says."""
        lists = ((tlo.CHANGED_8_TO_9, set(self._step2(OLD, MID).reindex),
                  {'C.utf8', 'ko_KR.utf8'}),
                 (tlo.CHANGED_9_TO_10, set(self._step2(MID, NEW).reindex),
                  set()))
        for measured, listed, elsewhere in lists:
            with self.subTest(listed=sorted(listed)):
                missed = {n for n in measured
                          if n not in listed and g.locale_source(n) not in listed}
                self.assertEqual(missed, elsewhere)
        self.assertIn('ko_KR', self._step4_written(MID))

    def _step4_written(self, tag):
        """The names step 4 writes at `tag`, read whole from its list file.

        Not from stdout, which prints a few names of each section and "...".
        Its own directory per call: MID and NEW write the same file name, and
        a memoised run does not write it again.
        """
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_step('flag_algorithmic_ranges.py', tag, out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        named = re.search(r'full list \((\d+) name\(s\)\): (\S+)', out)
        self.assertIsNotNone(named, f'step 4 at {tag} names no list file')
        with open(named.group(2), encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertEqual(int(named.group(1)), len(written),
                         f'step 4 at {tag} says {named.group(1)} names and '
                         f'writes {len(written)}')
        return set(written)

    def test_step_4_is_the_union_but_for_the_locale_renamed_on_the_way(self):
        """Step 4 reads the new tag alone (audit.sh passes it "$NEW"), so the
        direct pair's list is the second step's, and it differs from the
        union only by what step 4 flags at 2.34 and no longer flags at 2.39.
        Measured: one name, aa_ER@saaho, renamed to ssy_ER at 2.39. The
        direct run does not lose it: step 2 lists the old name as removed,
        and step 4 flags the new one. Any other name in that difference is a
        locale step 4 stopped flagging at the newer tag, which is the
        reassuring direction."""
        at_mid, at_new = self._step4_written(MID), self._step4_written(NEW)
        self.assertEqual(
            sorted(at_mid - at_new), ['aa_ER@saaho'],
            'step 4 flags at 2.34 a locale it no longer flags at 2.39, other '
            'than the one renamed on the way')
        self.assertIn('ssy_ER', at_new)
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-audit-test-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_step('filter_lc_collate_changes.py', OLD, NEW,
                           out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        path = os.path.join(
            out_dir, f'step2_removed_locales.{g.pair_slug(OLD, NEW)}.txt')
        with open(path, encoding='utf-8') as fh:
            removed = [ln.strip() for ln in fh
                       if ln.strip() and not ln.startswith('#')]
        self.assertEqual(removed, ['aa_ER@saaho (renamed to ssy_ER)'])

    def test_step_5_prints_83_hunks_and_not_the_sum(self):
        """84 is what the two steps add up to, and it is the wrong number: the
        copyright string in locale/programs/localedef.c changes "2018" ->
        "2021" -> "2024", which is one hunk read end to end and two read in
        steps. The direct figure is the right one; if this ever becomes 84,
        the sum is being reported rather than the diff. (75 and 76 until
        ld-ctype.c joined TIER 2, backlog 13.7.)"""
        out = self.step('diff_collation_code.py', OLD, NEW)
        self.assertEqual(
            one_int(r'(\d+) substantive hunk\(s\) found', out, 'the total'),
            83)

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
