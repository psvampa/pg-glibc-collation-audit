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

from _harness import (MID, NEW, OLD, backported_c, needs_clone,
                      run_wrapper, upstream_c)

import diff_distro_locales as dd
from _harness import GLIBC_CLONE


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

    def test_summary_says_node_to_node_was_NOT_RUN(self):
        """Absent is not empty, at the summary level.

        Without node directories nothing in the whole audit says anything
        about C.UTF-8 -- its source file is in neither tag and its collversion
        is always NULL. A summary that simply omits the section reads exactly
        like one that cleared it, which is false negative #1 in a new costume.
        """
        self.assertIn('-- Node-to-node locale data: NOT RUN', self.out)
        self.assertIn('C.UTF-8', self.out)


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

    def test_one_tag_compared_with_itself_says_nothing_was_compared(self):
        """An intra-major upgrade -- RHEL 8.1 -> 8.10 -- is two builds of one
        upstream release, so the tag pair is 2.28..2.28 and steps 1-5 are
        structurally empty. C.UTF-8's order moved across exactly such a bump
        (glibc-2.28-93.el8), so "nothing changed" here must not read as a
        clean result."""
        self.assertIn('are the same tag', self.out)
        self.assertIn('-- One tag, compared with itself', self.out)
        self.assertIn("means", self.out)


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


@needs_clone
class WrapperNodeToNode(unittest.TestCase):
    """Step 8, driven by the wrapper. Two materialised tags stand in for the
    two nodes, and the backported C is written into them -- it exists at no
    tag, which is the entire point of the step."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-nodes-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-trees-')
        cls.old_root = dd.materialise_tag(GLIBC_CLONE, OLD,
                                          os.path.join(cls.nodes, 'a'))
        cls.new_root = dd.materialise_tag(GLIBC_CLONE, MID,
                                          os.path.join(cls.nodes, 'b'))
        for root, body in ((cls.old_root, backported_c()),
                           (cls.new_root, upstream_c())):
            with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
                fh.write(body)
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-locales-dir', cls.old_root, '--old-build-id', 'build-old',
            '--new-locales-dir', cls.new_root, '--new-build-id', 'build-new',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_it_runs_and_logs_as_step_8(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn('NODE TO NODE', self.out)
        self.assertTrue(os.path.exists(os.path.join(
            self.out_dir, f'step8.{pair_slug(OLD, MID)}.log')), self.out)

    def test_the_summary_names_C_UTF_8_as_differing(self):
        """The payoff line: a verdict on the one locale no other step sees."""
        self.assertIn('C (C.UTF-8): DIFFERS', self.out)
        self.assertIn('no other step sees it', self.out)

    def test_the_count_excludes_the_provenance_header(self):
        """count_lines would have counted the leading `#` line, reporting one
        finding where there are none."""
        path = os.path.join(self.out_dir,
                            'node_collate_diffs.build-old..build-new.txt')
        with open(path, encoding='utf-8') as fh:
            names = [ln for ln in fh if ln.strip()
                     and not ln.startswith('#')]
        self.assertIn(f'{len(names)} locale(s) differ inside LC_COLLATE',
                      self.out)

    def test_the_same_caveat_from_three_steps_is_printed_once(self):
        """Steps 6, 7 and 8 all close with the charmaps caveat. Repeating it
        three times in the summary trains the reader to skip the section,
        which costs more than the repetition buys."""
        block = 'localedata/charmaps/ is NOT compared'
        summary = self.out.split('-- Warnings the clean results above')[-1]
        self.assertEqual(summary.count(block), 1, summary)


@needs_clone
class WrapperStaleNodeList(unittest.TestCase):
    """Its own output directory: this run deliberately leaves the state the
    other node-to-node tests read in a different one."""

    def test_a_stale_node_to_node_file_is_not_summarised(self):
        """Same invariant as the step 3 list: every file the summary reads was
        written by this run. Build ids with no directories still set the path,
        so the stale file is removed up front and the summary must fall back
        to NOT RUN rather than reporting a previous pair's finding."""
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-stale-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        stale = os.path.join(out_dir,
                             'node_collate_diffs.build-old..build-new.txt')
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('no_SUCH_locale\n')
        rc, out = run_wrapper(NEW, NEW,
                              '--old-build-id', 'build-old',
                              '--new-build-id', 'build-new',
                              out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('no_SUCH_locale', out)
        self.assertIn('-- Node-to-node locale data: NOT RUN', out)


if __name__ == '__main__':
    unittest.main()
