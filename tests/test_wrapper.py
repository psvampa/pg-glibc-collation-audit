"""Layer 4: ./audit.sh, the one-command wrapper, end to end.

The wrapper's whole job is to remove a manual handoff -- step 2 prints locale
names, the user retyped them into step 3 -- and automating a handoff is how you
reintroduce the bug filter_lc_collate_changes.py's docstring records removing:
a leftover result from a different version pair, analysed silently and reported
as if it were the answer.

So these tests are mostly about the wrapper's failure modes, not its happy
path. Every one of them fails if a specific guard is deleted; that is the
point. A wrapper that returns a plausible clean audit when a step crashed is
worse than five commands.
"""
import os
import shutil
import tempfile
import unittest

from _harness import MID, NEW, OLD, needs_clone, run_wrapper


def pair_slug(old, new):
    return f"{old}..{new}"


@needs_clone
class Wrapper(unittest.TestCase):
    """A pair with real findings: the wrapper must not change the answer."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-')
        cls.rc, cls.out = run_wrapper(OLD, MID, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_exits_zero_on_a_pair_with_findings(self):
        """A finding is not an error. Step 5 reports 25 hunks and returns 0."""
        self.assertEqual(self.rc, 0, self.out)

    def test_answer_matches_the_published_result(self):
        """The published set for 2.28..2.34, as generated locale names.

        docs/results.md states these. A wrapper that changes an answer is a
        bug, not a feature -- so this is the assertion that would catch the
        wrapper passing the wrong tag to the wrong step.
        """
        listed = os.path.join(self.out_dir, 'step3_affected_locales.txt')
        with open(listed, encoding='utf-8') as fh:
            names = sorted(n.strip() for n in fh if n.strip())
        self.assertEqual(names, ['or_IN', 'sv_FI', 'sv_FI.utf8', 'sv_FI@euro',
                                 'sv_SE', 'sv_SE.utf8'])

    def test_sv_FI_proves_the_new_tag_reached_step_3(self):
        """sv_FI is reachable only through the NEW tag's copy graph.

        It never appears in a plain file diff. If the wrapper swapped old and
        new when calling step 3, it would produce a fully plausible reversed
        audit at exit 0, and only this catches it.
        """
        self.assertIn('sv_FI', self.out)

    def test_step_2_list_is_written_and_feeds_step_3(self):
        """The handoff file exists, is named for the pair, and holds the names.

        Reverting step 2's write makes this fail with the file missing, which
        is the mutation test for that write.
        """
        path = os.path.join(
            self.out_dir, f'step2_changed_collate.{pair_slug(OLD, MID)}.txt')
        self.assertTrue(os.path.exists(path), f"{path} missing\n{self.out}")
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(sorted(n.strip() for n in fh if n.strip()),
                             ['or_IN', 'sv_SE'])

    def test_summary_repeats_the_C_UTF_8_warning_in_full(self):
        """A warning that scrolled past 400 lines ago was not delivered.

        And it must be the whole block: the first `!!` line alone stops at
        "...nor glibc-2.34, but", cutting off exactly where the reason starts.
        """
        summary = self.out[self.out.index('AUDIT SUMMARY'):]
        self.assertIn('Warnings the clean results above do NOT cover', summary)
        self.assertIn('collversion is NULL', summary)

    def test_summary_does_not_decide_step_5_for_you(self):
        """Step 5 reports; it does not decide. The summary must say so."""
        summary = self.out[self.out.index('AUDIT SUMMARY'):]
        self.assertIn('Not decided for you', summary)
        self.assertIn('25 hunk(s)', summary)

    def test_next_step_hints_are_suppressed(self):
        """The steps' "run this next" hints name commands already run."""
        self.assertNotIn('Next: python3 filter_lc_collate_changes.py',
                         self.out)
        self.assertNotIn('python3 flag_algorithmic_ranges.py glibc-2.34\n',
                         self.out)


@needs_clone
class WrapperEmptyPair(unittest.TestCase):
    """A pair with no LC_COLLATE change at all.

    glibc-2.39 against itself: no new tag to pin, so EXPECTED_SHA and
    has_tags() stay as they are. Step 2 finds nothing, and step 3 must be
    skipped rather than called with no arguments -- its `nargs='+'` makes an
    empty invocation an argparse error, exit 2.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-empty-')
        cls.rc, cls.out = run_wrapper(NEW, NEW, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_exits_zero(self):
        self.assertEqual(self.rc, 0, self.out)

    def test_step_3_was_skipped_not_called_empty(self):
        """Delete the wrapper's empty-list guard and this fails.

        Asserting rc == 0 alone would be vacuous: a wrapper that does not
        propagate child status stays 0 with argparse's complaint on stderr.
        So assert the complaint is absent, not just that the run was green.
        """
        self.assertNotIn('the following arguments are required', self.out)
        self.assertIn('STEP 3  Skipped', self.out)

    def test_steps_4_and_5_still_ran(self):
        """An empty step 2 is not the end of the audit.

        Steps 4 and 5 cover what a data diff cannot settle, so skipping them
        here would hide the only findings this pair could have.
        """
        self.assertIn('STEP 4', self.out)
        self.assertIn('STEP 5', self.out)

    def test_empty_list_is_written_not_absent(self):
        """Written-and-empty is a different fact from missing.

        Missing means step 2 never got there. The wrapper treats the two
        differently and this pins the protocol.
        """
        path = os.path.join(
            self.out_dir, f'step2_changed_collate.{pair_slug(NEW, NEW)}.txt')
        self.assertTrue(os.path.exists(path), self.out)
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(fh.read().strip(), '')


@needs_clone
class WrapperRefusesBadInput(unittest.TestCase):
    """The failure modes that would otherwise produce a clean-looking audit."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-bad-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def test_bogus_tag_fails_and_prints_no_summary(self):
        """Non-zero means a real error. A summary here would be a lie."""
        rc, out = run_wrapper(OLD, 'glibc-9.99-nope', out_dir=self.out_dir)
        self.assertNotEqual(rc, 0)
        self.assertNotIn('AUDIT SUMMARY', out)

    def test_wrong_argc_is_exit_2(self):
        rc, out = run_wrapper(OLD, out_dir=self.out_dir)
        self.assertEqual(rc, 2)
        self.assertIn('usage:', out)

    def test_step_2_rewrites_the_list_so_a_seed_cannot_survive(self):
        """This is the real protection on the file that becomes step 3's argv.

        Step 2 writes the list unconditionally for the pair being audited,
        before step 3 reads it, so nothing a previous run or another process
        left there can reach step 3. Revert step 2's write and this fails: the
        seeded line survives into the file and into the run.

        audit.sh also validates each name and refuses anything that is not a
        locale file name, but that guard is unreachable while this property
        holds -- see the comment on it.
        """
        import glibc_locale_data as g
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(
            self.out_dir,
            f'step2_changed_collate.{g.pair_slug(NEW, NEW)}.txt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('--repo /nonexistent/elsewhere\n')
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir)
        self.assertEqual(rc, 0, out)
        with open(path, encoding='utf-8') as fh:
            self.assertNotIn('nonexistent', fh.read())
        self.assertNotIn('/nonexistent/elsewhere', out)

    def test_a_stale_step_3_list_is_not_summarised(self):
        """The bug this project already removed once, in a new place.

        step3_affected_locales.txt is named for no particular pair, so a
        leftover from a different audit sits where the summary reads. Seed it,
        then audit a pair that finds nothing: the stale locale must not appear
        as this pair's answer.

        What makes that hold is that every file the summary reads is written
        during this run -- step 3 by running, or by the skip branch truncating
        it. audit.sh also removes them up front, which is belt and braces on
        the same invariant.
        """
        os.makedirs(self.out_dir, exist_ok=True)
        stale = os.path.join(self.out_dir, 'step3_affected_locales.txt')
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('no_SUCH_locale\n')
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('no_SUCH_locale', out)


if __name__ == '__main__':
    unittest.main()
