"""Layer 3: the real scripts, end to end, against the published results.

This is what stops a refactor from moving a verdict quietly. The numbers below
are the ones the README states and the examples/ files record; if a change moves
one, that is either a discovery worth a CHANGELOG entry or a regression, and
either way somebody has to look.

Run as subprocesses: the contract these tools offer is their printed output and
their exit status.
"""
import os
import re
import shutil
import tempfile
import unittest

from _harness import (FLOOR_NEW, FLOOR_OLD, GLIBC_CLONE, MID, NEW, OLD,
                       needs_clone, needs_floor_pair, run_step)

import glibc_locale_data as g


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

    def test_the_blast_radius_the_readme_states_for_both_tags(self):
        """The README's CJK row says iso14651_t1 is inherited by 328 locales at
        2.34 and 338 at 2.39. The 328 sat in five files and the 338 in one
        example, and nothing asserted either against the clone -- which is how
        the step-4 "2" rotted. Step 1 computes the radius at the NEW tag."""
        for old, new, radius in ((OLD, MID, 328), (MID, NEW, 338)):
            with self.subTest(tag=new):
                out = self.step('audit-locale-diff.sh', old, new)
                self.assertIn(f'\n   {radius} locales inherit from iso14651_t1\n',
                              out)


@needs_clone
class Step2Filter(StepRun):
    EXPECTED = {(OLD, MID): (2, ['or_IN', 'sv_SE']),
                (MID, NEW): (3, ['ber_DZ', 'kab_DZ', 'th_TH'])}

    def test_the_step_3_list_is_written_for_each_pair(self):
        """audit.sh reads this file instead of the user retyping the names.

        Two things are pinned. The file must exist -- reverting step 2's write
        fails here -- and it must be named for the version pair, because this
        class runs step 2 four times into one scratch directory and a
        pair-agnostic name would have each run clobber the last. That is the
        same collision that, in the shared /tmp default, would hand step 3 a
        list belonging to a different pair.
        """
        import glibc_locale_data as g
        for (old, new), (_, names) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                self.step('filter_lc_collate_changes.py', old, new)
                path = os.path.join(
                    self.out_dir,
                    f'step2_changed_collate.{g.pair_slug(old, new)}.txt')
                self.assertTrue(os.path.exists(path), f'{path} missing')
                with open(path, encoding='utf-8') as fh:
                    written = sorted(n.strip() for n in fh if n.strip())
                self.assertEqual(written, sorted(names))

    def test_the_written_list_matches_the_printed_command(self):
        """Two representations of one list must not drift.

        Step 2 both prints a paste-ready step-3 command and writes the file
        audit.sh reads. If those ever disagree, a hand-run audit and a wrapped
        one silently audit different locale sets.
        """
        import glibc_locale_data as g
        for (old, new), (_, names) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                m = re.search(r'resolve_copy_closure\.py \S+ (.+)$', out, re.M)
                self.assertIsNotNone(m, 'no step-3 command printed')
                printed = sorted(m.group(1).split())
                path = os.path.join(
                    self.out_dir,
                    f'step2_changed_collate.{g.pair_slug(old, new)}.txt')
                with open(path, encoding='utf-8') as fh:
                    written = sorted(n.strip() for n in fh if n.strip())
                self.assertEqual(printed, written)

    def test_files_touching_lc_collate(self):
        for (old, new), (count, names) in self.EXPECTED.items():
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                self.assertEqual(
                    one_int(r'(\d+) touch LC_COLLATE', out, 'the count'), count)
                for name in names:
                    self.assertIn(f'localedata/locales/{name}', out)

    def test_c_utf8_is_named_on_both_pairs(self):
        """#1: C.UTF-8 is backported by RHEL8 and RHEL9, so 'added upstream'
        does not mean 'new on your system'. It is absent from both tags on the
        first pair and added on the second -- both must warn."""
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                self.assertIn('C.UTF-8', out)
                self.assertRegex(
                    out, re.compile(r'^!! localedata/locales/C\b', re.M))

    def test_the_false_blanket_claim_is_gone(self):
        """Added files used to be reported as unable to affect an existing
        index, flat. They can, if the locale existed on the old system --
        distros backport, which is the whole C.UTF-8 story.

        Asserted on whitespace-collapsed output, and that is the point of this
        docstring: the claim is printed across two lines, so the original
        `assertNotIn('They cannot affect an existing index', out)` matched
        nothing whether the claim was there or not. It guarded the thing it
        named and would have passed if the claim came back. Found 2026-09-06,
        the fourth test in this suite caught guarding nothing.
        """
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                out = ' '.join(
                    self.step('filter_lc_collate_changes.py', old, new).split())
                self.assertIn('cannot affect an existing index ONLY IF', out)
                self.assertNotIn('cannot affect an existing index.', out)

    def test_the_files_with_no_collate_block_are_named_not_just_counted(self):
        """A count alone leaves a reader unable to tell a transliteration
        table from a locale that was skipped by mistake."""
        out = self.step('filter_lc_collate_changes.py', MID, NEW)
        self.assertIn('no LC_COLLATE on either side', out)
        for name in ('i18n_ctype', 'translit_combining'):
            self.assertIn(name, out)

    def test_no_file_gains_a_collate_block_in_either_pair(self):
        """Recorded as a measured fact: it has never happened between glibc
        2.17 and 2.42. If this ever fails, a locale acquired collation rules
        and that is a finding, not a bug in this test."""
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                self.assertNotIn('GAINED an LC_COLLATE block', out)

    def test_a_pair_where_c_exists_at_both_tags_does_not_warn(self):
        """Control: the warning must be about the old side being missing, not
        about the name C. At 2.39..2.41 the file is in both tags."""
        out = self.step('filter_lc_collate_changes.py', NEW, 'glibc-2.41')
        self.assertNotIn('!! localedata/locales/C ', out)


@needs_clone
class Step3Closure(StepRun):
    def test_copy_closure_counts(self):
        cases = ((MID, ['or_IN', 'sv_SE'], 4),
                 (NEW, ['ber_DZ', 'kab_DZ', 'th_TH'], 3))
        for tag, names, expected in cases:
            with self.subTest(tag=tag):
                out = self.step('resolve_copy_closure.py', tag, *names)
                self.assertEqual(
                    one_int(r'Full affected set \((\d+) locale', out, 'the set'),
                    expected)

    def test_sv_fi_is_reached_only_through_inheritance(self):
        """sv_FI has no tailoring of its own; it copies sv_SE. A plain file
        diff never flags it."""
        out = self.step('resolve_copy_closure.py', MID, 'or_IN', 'sv_SE')
        self.assertIn('sv_FI', out)
        self.assertIn('sv_FI@euro', out)

    def test_paths_are_accepted_as_well_as_bare_names(self):
        """"resolve_copy_closure.py reported '0 additionally affected'
        silently" when fed step 2's output verbatim -- step 2 prints paths and
        this compared bare names. That dropped sv_FI from the result."""
        out = self.step('resolve_copy_closure.py', MID,
                        'localedata/locales/or_IN', 'localedata/locales/sv_SE')
        self.assertEqual(
            one_int(r'Full affected set \((\d+) locale', out, 'the set'), 4)
        self.assertIn('sv_FI', out)

    def test_an_unknown_locale_is_an_error_not_a_quiet_zero(self):
        rc, out = run_step('resolve_copy_closure.py', MID, 'no_SUCH_locale',
                           out_dir=self.out_dir)
        self.assertNotEqual(rc, 0)

    def test_generated_names_use_the_locale_a_spelling(self):
        out = self.step('resolve_copy_closure.py', MID, 'sv_SE')
        self.assertIn('sv_SE.utf8', out)
        self.assertNotIn('sv_SE.UTF-8', out)


    def test_the_written_list_keeps_a_locale_SUPPORTED_does_not_name(self):
        """Step 3's list is what audit.sh summarises and what a reader feeds to
        the empirical test. Writing only the names SUPPORTED maps dropped every
        affected locale it does not name -- the same line that dropped C from
        step 4's list on a node that builds it. Both halves are asserted: the
        mapped names must survive too, or the fix trades one omission for
        another."""
        out = self.step('resolve_copy_closure.py', MID, 'sv_SE',
                        'cns11643_stroke')
        self.assertIn('cns11643_stroke', out)
        with open(os.path.join(self.out_dir, 'step3_affected_locales.txt'),
                  encoding='utf-8') as fh:
            written = [ln.strip() for ln in fh if ln.strip()]
        self.assertIn('cns11643_stroke', written)
        self.assertIn('sv_SE.utf8', written)


@needs_clone
class Step4AlgorithmicRanges(StepRun):
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
        """335 at MID, from the README. Pinned here because scan_ellipsis
        switched from collate_block to collate_text, which changes which files
        count as defining LC_COLLATE for files that open with it."""
        out = self.step('flag_algorithmic_ranges.py', MID)
        self.assertIn('335 locale source file(s), 478 generated', out)
        self.assertIn('of which 342 define LC_COLLATE', out)

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
        codepoint_collation in a comment three lines above declaring it, so a
        substring search reads the comment as the declaration -- and would
        then report RHEL's ellipsis-based backport of the same file as byte
        order."""
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
    def test_substantive_hunk_totals(self):
        for (old, new), expected in (((OLD, MID), 25), ((MID, NEW), 53)):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('diff_collation_code.py', old, new)
                self.assertEqual(
                    one_int(r'(\d+) substantive hunk\(s\) found', out,
                            'the total'), expected)

    def test_the_bug_22668_commit_is_surfaced(self):
        """"ko_KR was reported unaffected between glibc 2.28 and 2.34. It
        changes." Step 5 exists because this commit is the cause, and it is in
        the code, not in ko_KR's data file."""
        out = self.step('diff_collation_code.py', OLD, MID)
        self.assertIn('82292c99b2', out)

    def test_tier3_shows_the_hunks_the_hand_lists_missed(self):
        """#2: both of these are real changes over 2.34..2.39 that no tier
        listed, and the RHEL9-to-RHEL10 ko_KR verdict is argued from the hunks
        step 5 prints."""
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
        out = self.step('diff_collation_code.py', NEW, MID)
        self.assertIn('\n!! 1 tracked path(s) present at glibc-2.39 and GONE '
                      'at glibc-2.34.', out)
        self.assertIn(f'\n     locale/C-collate-seq.c: ABSENT at {MID}\n', out)
        block = out.split('!! ')[1].split('\n\n')[0]
        for line in block.split('\n')[1:]:
            self.assertTrue(line.startswith('   '), repr(line))

    def test_the_clean_sentence_is_the_one_the_wrapper_keys_on(self):
        """audit.sh takes exactly this sentence as step 5's clean verdict and
        treats anything else as unresolved. Neither documented pair is clean,
        so the sentence is tied to the source here: reword it in one place and
        not the other, and every run would be summarised as unresolved.
        The vanished-path variant must NOT say it -- that is the whole fix."""
        src = open(os.path.join(_harness_scripts(), 'diff_collation_code.py'),
                   encoding='utf-8').read()
        self.assertIn('"No substantive collation code change. ', src)
        audit = open(os.path.join(_harness_scripts(), '..', 'audit.sh'),
                     encoding='utf-8').read()
        self.assertIn("'^No substantive collation code change\\.'", audit)
        out = self.step('diff_collation_code.py', NEW, MID)
        self.assertNotIn('No substantive collation code change', out)


def _harness_scripts():
    from _harness import SCRIPTS_DIR
    return SCRIPTS_DIR


@needs_floor_pair
class BelowTheOldVersionFloor(StepRun):
    """glibc-2.12 -> glibc-2.17, the pair docs/limitations.md quotes.

    Not an audited pair and not a published verdict: it is the pair that
    demonstrated the pre-2.24 failure, and after that failure was fixed it is
    what shows the fix reaches. These numbers are asserted because the last set
    this page carried for this pair went stale silently -- it said step 4
    reported 2 exposed locales long after a partial fix had moved that to 277,
    and nothing caught it. That is the whole reason for this class.

    The mechanism: in glibc 2.23 and earlier the three master templates open
    with LC_COLLATE at byte 0. While collate_block could not read those, they
    never entered the copy graph and everything inheriting from them looked
    unaffected.
    """

    def test_the_three_templates_are_in_the_copy_graph(self):
        """The root cause, asserted directly rather than through a count.
        These three are the highest fan-in files in the corpus; dropping them
        is what collapsed the closure."""
        graph = g.build_copy_graph(GLIBC_CLONE, FLOOR_NEW)
        for tmpl in ('iso14651_t1', 'iso14651_t1_common',
                     'iso14651_t1_pinyin'):
            self.assertIn(tmpl, graph,
                          f'{tmpl} is not a node of the {FLOOR_NEW} copy '
                          f'graph; collate_block has regressed to a form that '
                          f'cannot read a block at byte 0')

    def test_step_2_finds_six_locales_touching_lc_collate(self):
        out = self.step('filter_lc_collate_changes.py', FLOOR_OLD, FLOOR_NEW)
        self.assertEqual(
            one_int(r'Files with changes inside LC_COLLATE: (\d+)', out,
                    'the step 2 count'),
            6)

    def test_step_3_reaches_280_not_11(self):
        """11 was what it reported with the three roots missing; 278 was the
        figure after that fix and before `copy` targets written in glibc's
        symbolic notation were decoded, which ky_KG and uk_UA are."""
        out = self.step('resolve_copy_closure.py', FLOOR_NEW,
                        'dz_BT', 'fi_FI', 'hu_HU', 'iso14651_t1_common',
                        'se_NO', 'ug_CN')
        self.assertEqual(
            one_int(r'Full affected set \((\d+) locale', out, 'the set'),
            280)

    def test_step_3_maps_its_280_files_to_409_generated_names(self):
        """The example quotes this header, and the first patch of the
        twenty-third entry put the WRITTEN list's count there instead -- 414,
        which also carries the five names SUPPORTED does not list. Two
        different numbers one line apart, and only one of them was printed."""
        out = self.step('resolve_copy_closure.py', FLOOR_NEW,
                        'dz_BT', 'fi_FI', 'hu_HU', 'iso14651_t1_common',
                        'se_NO', 'ug_CN')
        self.assertEqual(
            one_int(r'pg_collation show \((\d+)\)', out,
                    'the generated names'),
            409)
        # And the written list, which is the other number: 409 mapped names
        # plus the five SUPPORTED does not list. The step prints this one
        # nowhere, so nothing but this line keeps it honest.
        with open(os.path.join(self.out_dir, 'step3_affected_locales.txt'),
                  encoding='utf-8') as fh:
            self.assertEqual(len([ln for ln in fh if ln.strip()]), 414)

    def test_step_5_prints_65_hunks(self):
        """examples/below-the-floor-2.12-to-2.17.txt quotes this figure in
        prose. It was 63 until the two wide-char wrappers joined TIER 1: each
        contributes one hunk, the 2012 FSF postal-address change, which the
        filter keeps because it cannot prove a bare licence continuation is
        prose. Conservative, and counted."""
        out = self.step('diff_collation_code.py', FLOOR_OLD, FLOOR_NEW)
        self.assertEqual(
            one_int(r'(\d+) substantive hunk\(s\) found', out, 'the total'),
            65)

    def test_step_4_reaches_281_not_279(self):
        """277 was the figure before the collate_block fix and 279 after it;
        281 adds ky_KG and uk_UA, which copy iso14651_t1 spelled in symbolic
        notation. The 2 this page used to publish was older still, and already
        wrong when it was quoted."""
        out = self.step('flag_algorithmic_ranges.py', FLOOR_NEW)
        self.assertEqual(
            one_int(r'Full set needing empirical confirmation: (\d+) locale',
                    out, 'the exposed set'),
            281)
        # Both halves of the sentence, and the file it names: docs and the
        # worked example publish all three, and only the first was pinned.
        self.assertEqual(
            one_int(r'confirmation: \d+ locale source file\(s\), (\d+) '
                    r'generated', out, 'the generated names'),
            411)
        self.assertEqual(
            one_int(r'full list \((\d+) name\(s\)\)', out, 'the list'), 416)

    def test_the_locales_the_bug_used_to_drop_are_reported(self):
        """A count can be right for the wrong reason. These are named in
        docs/limitations.md as examples of what was silently dropped."""
        out = self.step('resolve_copy_closure.py', FLOOR_NEW,
                        'dz_BT', 'fi_FI', 'hu_HU', 'iso14651_t1_common',
                        'se_NO', 'ug_CN')
        for name in ('en_US', 'de_DE', 'fr_FR', 'es_ES', 'it_IT', 'nl_NL',
                     'pt_BR', 'ru_RU', 'sv_SE', 'zh_CN', 'zh_TW'):
            self.assertIn(name, out, f'{name} is not in the affected set')


if __name__ == '__main__':
    unittest.main()
