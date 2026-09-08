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
import re
import shutil
import tempfile
import unittest

from _harness import (MID, NEW, OLD, backported_c, locale_file,
                      needs_clone, run_wrapper, upstream_c)

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
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn('-- Node-to-node locale data: NOT RUN', summary)
        # On the summary, not the whole run: step 2's own `!!` warning names
        # C.UTF-8 on every pair, so the unsplit assertion could not fail.
        self.assertIn('C.UTF-8', summary)


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
        # The whole sentence, whitespace collapsed. This used to assert the
        # word "means", which every step's prose contains.
        flat = ' '.join(self.out.split())
        self.assertIn("Everything above that says 'nothing changed' means "
                      "'nothing was compared'", flat)

    def test_the_summary_counts_without_a_shell_error(self):
        """count_lines was `grep -c . FILE || echo 0`. grep -c prints "0" AND
        exits 1 on no match, so the fallback printed a second 0, `[ "0\\n0"
        -gt 0 ]` failed with "integer expression expected" on stderr, and the
        summary fell into the else branch -- the right one, by luck. Every
        clean run printed that error. Restore the old function and this fails.
        """
        self.assertNotIn('integer expression expected', self.out)
        self.assertIn("none -- no locale's LC_COLLATE changed between these "
                      "two tags", self.out)

    def test_the_clean_step_5_branch_is_the_one_printed(self):
        """HUNKS == 0 used to be executed by this class and asserted by
        nobody. The same tag against itself is the one real input that reaches
        it: step 5 prints its clean sentence and the summary must say
        "sufficient" -- and NOT the unresolved wording, which is what an absent
        clean sentence produces."""
        summary = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertIn('Step 5 found no substantive change, so a clean data '
                      'diff is sufficient even for the locales step 4 flagged',
                      summary)
        self.assertIn('Nothing from step 5.', summary)
        self.assertNotIn('did NOT reach a clean result', summary)


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

    def test_the_ellipsis_scan_lines_for_each_node_are_printed(self):
        """audit.sh's steps 9/10 block prints "C (C.UTF-8): ellipsis-based"
        for the backported C and "codepoint_collation" for the upstream one.
        This class has always produced both lines and asserted neither."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn("-- Node's own locale data, ellipsis scan (build-old)",
                      summary)
        self.assertIn("-- Node's own locale data, ellipsis scan (build-new)",
                      summary)
        self.assertEqual(summary.count('ellipsis-based locale(s):'), 2)
        flat = ' '.join(summary.split())
        self.assertIn('C (C.UTF-8): ellipsis-based <- localedef computes its '
                      'weights, so identical data does NOT mean identical '
                      'order', flat)
        self.assertIn('C (C.UTF-8): codepoint_collation <- byte order by '
                      'construction', flat)

    def test_the_blast_radius_of_the_differing_files_is_in_the_summary(self):
        """"Node-to-node did not close over the copy graph." The summary now
        carries the count of locales inheriting a differing file's LC_COLLATE,
        read from the list step 8 writes. For these two trees or_IN and sv_SE
        differ and sv_FI copies sv_SE -- step 3's answer, from the node side.
        """
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        m = re.search(r"plus (\d+) locale\(s\) that inherit one of those "
                      r"files' LC_COLLATE via copy on build-new", flat)
        self.assertIsNotNone(m, flat)
        path = os.path.join(self.out_dir,
                            'node_collate_inherited.build-old..build-new.txt')
        with open(path, encoding='utf-8') as fh:
            names = [ln.strip() for ln in fh if ln.strip()
                     and not ln.startswith('#')]
        self.assertEqual(int(m.group(1)), len(names))
        self.assertIn('sv_FI', names)

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
class WrapperOneSideOnly(unittest.TestCase):
    """Only --old-locales-dir: steps 6 and 9 run, 7, 8 and 10 do not, and the
    summary says which node-side questions went unasked. This branch of
    audit.sh had no test at all."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-oneside-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-onetree-')
        root = dd.materialise_tag(GLIBC_CLONE, OLD, os.path.join(cls.nodes, 'a'))
        with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(backported_c())
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-locales-dir', root, '--old-build-id', 'build-old',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_steps_6_and_9_run_and_7_8_10_do_not(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn("DISTRO CHECK  do build-old's patches", self.out)
        self.assertIn("NODE ELLIPSIS  does build-old's own locale data",
                      self.out)
        self.assertNotIn('NODE TO NODE', self.out)
        for n in (7, 8, 10):
            self.assertFalse(os.path.exists(os.path.join(
                self.out_dir, f'step{n}.{pair_slug(OLD, MID)}.log')), n)

    def test_the_summary_says_node_to_node_was_NOT_RUN_but_scans_the_old_node(self):
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn('-- Node-to-node locale data: NOT RUN', summary)
        self.assertIn("-- Node's own locale data, ellipsis scan (build-old)",
                      summary)
        self.assertIn('C (C.UTF-8): ellipsis-based', summary)
        self.assertNotIn('ellipsis scan (build-new)', summary)


@needs_clone
class WrapperNodesIdentical(unittest.TestCase):
    """NODE_DIFFS == 0: two copies of one tree under two build ids. The
    fingerprint warning fires, the run continues, and the summary takes the
    "no locale differs" branch -- which no test had ever driven."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-same-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-sametrees-')
        a = dd.materialise_tag(GLIBC_CLONE, MID, os.path.join(cls.nodes, 'a'))
        b = dd.materialise_tag(GLIBC_CLONE, MID, os.path.join(cls.nodes, 'b'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-locales-dir', a, '--old-build-id', 'build-x',
            '--new-locales-dir', b, '--new-build-id', 'build-y',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_no_difference_branch_is_printed_without_a_shell_error(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertNotIn('integer expression expected', self.out)
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertIn("no locale differs inside LC_COLLATE between the two "
                      "nodes' own sources", flat)
        self.assertNotIn('locale(s) differ inside LC_COLLATE', flat)
        self.assertNotIn('plus ', flat)

    def test_the_identical_fingerprint_warning_reaches_the_summary(self):
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn('same fingerprint', summary)


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

    def test_a_previous_runs_step_log_does_not_supply_this_runs_warnings(self):
        """The warnings block globs every step*.PAIR.log in OUT_DIR, so a run
        given both nodes' directories used to leave its notices behind for the
        next run of the same pair to reprint as its own -- including "C.UTF-8:
        built from ellipsis ranges on at least one of these nodes" from a run
        that read no node. Conservative in direction, false in content, and
        against this wrapper's own rule that every file it reads was written by
        this run.
        """
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-stalelog-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        stale = os.path.join(out_dir, f'step9.{NEW}..{NEW}.log')
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('!! a warning from a run that read a node\n'
                     '   with its indented continuation line\n')
        rc, out = run_wrapper(NEW, NEW, out_dir=out_dir)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('a warning from a run that read a node', out)


@needs_clone
class WrapperStep5Unresolved(unittest.TestCase):
    """"The summary contradicted step 5 when a tracked path vanished."

    The summary read its hunk count from the line "N substantive hunk(s)
    found". When step 5 finds a tracked path present at the old tag and gone
    at the new one with nothing else to report, it prints "NOT a clean result"
    and no such line -- and `HUNKS=${HUNKS:-0}` turned that absence into zero,
    which is the branch that says "a clean data diff is sufficient even for
    the locales step 4 flagged". Step 5 said one thing; the summary said the
    opposite, 300 lines lower.

    No tag pair can drive this branch for real: going forward in time no
    tracked path has ever vanished, and reversed, 2.39 -> 2.34 loses
    C-collate-seq.c but still finds 53 hunks. So step 5 is stood in for by a
    `python3` shim on PATH that prints what the real script prints in that
    state -- the same text test_known_answers ties to the real script on the
    reversed pair -- and hands every other step to the real interpreter.
    """

    CANNED = (
        "Collation code changes between glibc-2.39 and glibc-2.39\n"
        "\n"
        "!! 1 tracked path(s) present at glibc-2.39 and GONE at glibc-2.39."
        " `git diff`\n"
        "   over a missing path is empty, not an error, so a rename reads"
        " exactly like\n"
        '   "unchanged":\n'
        "     locale/programs/ld-collate.c: ABSENT at glibc-2.39\n"
        "   Find where each moved and add the new path to TIER1/TIER2 before"
        " trusting\n"
        "   a no-change result.\n"
        "\n"
        "No substantive change in the files this audit could read -- but 1"
        " tracked\n"
        "path(s) vanished before glibc-2.39, so this is NOT a clean result.\n"
        "Resolve the paths listed above, then re-run.\n")

    def setUp(self):
        import sys
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-vanished-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        shim_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-shim-')
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)
        canned = os.path.join(shim_dir, 'step5.txt')
        with open(canned, 'w', encoding='utf-8') as fh:
            fh.write(self.CANNED)
        shim = os.path.join(shim_dir, 'python3')
        with open(shim, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\n'
                     'case "$1" in\n'
                     f'  *diff_collation_code.py) cat "{canned}"; exit 0 ;;\n'
                     'esac\n'
                     f'exec "{sys.executable}" "$@"\n')
        os.chmod(shim, 0o755)
        self.env = {'PATH': shim_dir + os.pathsep + os.environ.get('PATH', '')}

    def test_a_vanished_path_leaves_step_4_unresolved(self):
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        self.assertIn('NOT a clean result', out, 'the shim did not run')
        summary = out.split('AUDIT SUMMARY')[1]
        flat = ' '.join(summary.split())
        self.assertNotIn('a clean data diff is sufficient', flat)
        self.assertNotIn('Nothing from step 5', flat)
        self.assertIn('step 5 did NOT reach a clean result, so the locales '
                      'step 4 flagged stay UNRESOLVED', flat)
        self.assertIn('Step 5 reached no clean result', flat)

    def test_the_vanished_path_is_repeated_in_the_warnings_block(self):
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        warnings = out.split('-- Warnings the clean results above')[1]
        self.assertIn('locale/programs/ld-collate.c: ABSENT at glibc-2.39',
                      warnings)


@needs_clone
class WrapperNodeCIsNeitherEllipsisNorCodepoint(unittest.TestCase):
    """The third state of C, which the summary used to pass over in silence,
    on the shape where getting it wrong costs the most.

    audit.sh decided C's line with two greps -- in the ellipsis list, else
    named on the codepoint line -- so a C that is neither produced no line at
    all. With a single --old-locales-dir the step-8 block that names a missing
    backport does not run either, so nothing distinguished this node from one
    whose C was never looked at. This C copies `iso14651_t1`: its own file has
    no ellipsis, and every weight it sorts by is one localedef computed from
    that template's ranges. The summary relays what step 9 declared, so the
    copy is in the line; the first version of this fix printed "neither
    ellipsis-based nor codepoint_collation" over exactly this input.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-cstate-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-ctree-')
        root = dd.materialise_tag(GLIBC_CLONE, OLD, os.path.join(cls.nodes, 'a'))
        with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(locale_file('copy "iso14651_t1"'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-locales-dir', root, '--old-build-id', 'build-old',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_summary_says_what_the_copy_reaches_rather_than_nothing(self):
        self.assertEqual(self.rc, 0, self.out)
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertIn('C (C.UTF-8): copy-only', flat)
        self.assertIn('it copies iso14651_t1, which this step flagged -- so '
                      'this locale IS exposed', flat)

    def test_it_is_not_reported_as_either_of_the_two_settled_states(self):
        """The direction that matters: codepoint_collation is the reassuring
        one, and a fall-through must never land there."""
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertNotIn('C (C.UTF-8): codepoint_collation', flat)
        self.assertNotIn('C (C.UTF-8): ellipsis-based', flat)


@needs_clone
class WrapperNodeWithoutC(unittest.TestCase):
    """A locale directory with no C at all. Absent is not cleared, and with one
    side only nothing else in the summary says so."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-noc-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-noctree-')
        root = dd.materialise_tag(GLIBC_CLONE, OLD, os.path.join(cls.nodes, 'a'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-locales-dir', root, '--old-build-id', 'build-old',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_summary_calls_an_absent_C_absent_and_not_cleared(self):
        self.assertEqual(self.rc, 0, self.out)
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertIn('C (C.UTF-8): ABSENT from this locale directory <- not '
                      'examined, NOT cleared', flat)
        self.assertNotIn('C (C.UTF-8): codepoint_collation', flat)


if __name__ == '__main__':
    unittest.main()
