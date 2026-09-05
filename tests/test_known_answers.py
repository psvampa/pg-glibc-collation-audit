"""Layer 3: the real scripts, end to end, against the published results.

This is what stops a refactor from moving a verdict quietly. The numbers below
are the ones the README states and the examples/ files record; if a change moves
one, that is either a discovery worth a CHANGELOG entry or a regression, and
either way somebody has to look.

Run as subprocesses: the contract these tools offer is their printed output and
their exit status.
"""
import re
import shutil
import tempfile
import unittest

from _harness import MID, NEW, OLD, needs_clone, run_step


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


@needs_clone
class Step2Filter(StepRun):
    EXPECTED = {(OLD, MID): (2, ['or_IN', 'sv_SE']),
                (MID, NEW): (3, ['ber_DZ', 'kab_DZ', 'th_TH'])}

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
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                out = self.step('filter_lc_collate_changes.py', old, new)
                self.assertNotIn('They cannot affect an existing index', out)

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


if __name__ == '__main__':
    unittest.main()
