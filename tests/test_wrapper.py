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

from _harness import (EXPECTED_SHA, MID, NEW, OLD, backported_c, flat,
                      locale_file, needs_clone, run_wrapper, upstream_c)

import diff_distro_locales as dd
import glibc_locale_data as g
from _harness import GLIBC_CLONE


def pair_slug(old, new):
    return f"{old}..{new}"


def section_body(summary, heading):
    """The indented body under a summary heading, whitespace collapsed.

    Stops at the next heading as well as at a blank line: on a run given the
    NEW directory alone, the scan of the side that ran follows the NOT RUN
    block with no blank line between them, so splitting on a blank line alone
    would swallow it and carry its text into the assertions.
    """
    body = []
    for line in summary.split(heading, 1)[1].splitlines()[1:]:
        if not line.strip() or line.startswith('--'):
            break
        body.append(line)
    return flat('\n'.join(body))


REMOVED = '-- Removed: locale files the old side has and the new side does not'
NOT_CHECKED = ('The tags cannot show what your distro adds or drops: '
               'NOT CHECKED.')
# The whole section over 2.28..2.34 when step 8 did not run and nothing is
# missing: no copy, or one complete copy.
NOTHING_GONE_OVER_OLD_MID = [
    NOT_CHECKED,
    'Pass --old-locales-dir and --new-locales-dir with their build ids.',
    f'Between the tags: none -- no locale file at {OLD} is gone at {MID}',
]


# audit.sh's usage text, whole, with the script's own path as "audit.sh".
USAGE = [
    'usage: audit.sh <old_tag> <new_tag>',
    '         [--old-locales-dir DIR --old-build-id NVR]',
    '         [--new-locales-dir DIR --new-build-id NVR]',
    '       e.g. audit.sh glibc-2.28 glibc-2.34',
    '       tags are glibc-<version>; run `ldd --version` on each node',
    '       OLD first, NEW second: a reversed pair is refused, not',
    '       answered. Distance is NOT checked: RHEL8 to RHEL10 is one',
    '       pair, glibc-2.28 glibc-2.39, not two runs added up. What',
    '       that reports was measured with glibc-2.34 in the middle',
    '       (docs/scope.md). Every --* option takes a value.',
    '',
    '       The --*-locales-dir options are OPTIONAL. Given a copy of a',
    "       node's /usr/share/i18n/locales/, the run also checks whether",
    "       the distro's patches touch LC_COLLATE -- a thing an upstream",
    "       tag diff structurally cannot see. Needs the node's",
    '       build id too: a result is bound to the build it ran on.',
    '',
    '       Either side on its own adds that check for that side (step 6',
    "       for old, step 7 for new), and scans that node's own data for",
    '       ellipsis ranges (step 9 for old, step 10 for new) -- which is',
    '       the only way that question is asked of a node supplied on its',
    '       own, since step 4 scans the new TAG, which holds at most',
    "       upstream's C and never speaks for what your node built.",
    '',
    '       Supply BOTH and the run also compares the two nodes to each',
    '       other (step 8).',
]

NODE_TO_NODE_NOT_RUN = '-- Node-to-node locale data: NOT RUN'
# The whole block, compared line for line. A check for one phrase would let
# the same false claim come back in other words.
NODE_TO_NODE_NOT_RUN_BODY = [
    'Pass --old-locales-dir and --new-locales-dir with their build',
    "ids. Without both, nothing above compared the two nodes' C.UTF-8",
    'against each other, and PostgreSQL reports collversion as NULL for',
    'every C.* collation, so no mismatch can ever fire.',
    'Then run sql/c_utf8_probe.sql on both nodes.',
]


def summary_block(out, heading):
    """The body under a summary heading, one stripped line each.

    It refuses the same things removed_lines below does. The heading must be
    a whole line of the summary, exactly once, so text appended to it is
    caught, and the body must end at a blank line or the next heading.
    """
    summary = out.split('AUDIT SUMMARY', 1)[-1]
    lines = summary.splitlines()
    at = [i for i, line in enumerate(lines) if line == heading]
    if len(at) != 1:
        raise AssertionError(f'{heading!r} is a whole line {len(at)} '
                             f'time(s):\n{summary}')
    body = []
    for line in lines[at[0] + 1:]:
        if not line.strip() or line.startswith('--'):
            return body
        body.append(line.strip())
    raise AssertionError(f'{heading!r} never ends:\n{summary}')


def removed_lines(out):
    """The body of the summary's Removed section, one stripped line each.

    Refuses rather than widening: the heading must be in the summary exactly
    once, and the body must end at a blank line or the next heading -- a cut
    that never finds its end would run on into the sections below it.
    """
    summary = out.split('AUDIT SUMMARY', 1)[-1]
    if summary.count(REMOVED) != 1:
        raise AssertionError(f'the Removed heading appears '
                             f'{summary.count(REMOVED)} time(s):\n{summary}')
    body = []
    for line in summary.split(REMOVED, 1)[1].splitlines()[1:]:
        if not line.strip() or line.startswith('--'):
            return body
        body.append(line.strip())
    raise AssertionError(f'the Removed section never ends:\n{summary}')


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
        """A finding is not an error. Step 5 reports 24 hunks and returns 0."""
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
        self.assertIn('24 hunk(s)', summary)

    def test_the_backport_caveat_reaches_this_branch(self):
        """An upstream diff cannot see a distro's patches, whatever step 5
        found. The sentence lived in the `clean` branch alone, and all three
        published runs take `hunks`, so no output this tool ever printed
        carried it (backlog 1.14, measured 2026-09-20)."""
        summary = flat(self.out[self.out.index('AUDIT SUMMARY'):])
        self.assertIn("An upstream diff cannot see your distro's backports",
                      summary)

    def test_next_step_hints_are_suppressed(self):
        """The steps' "run this next" hints name commands already run."""
        self.assertNotIn('Next: python3 filter_lc_collate_changes.py',
                         self.out)
        self.assertNotIn('python3 flag_algorithmic_ranges.py glibc-2.34\n',
                         self.out)

    def test_summary_says_node_to_node_was_NOT_RUN(self):
        """Absent is not empty, at the summary level.

        Without both node directories nothing compared the two nodes' C.UTF-8
        against each other, and its collversion is always NULL. A summary that
        simply omits the section reads exactly like one that cleared it, which
        is false negative #1 in a new costume.
        """
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn('-- Node-to-node locale data: NOT RUN', summary)
        # On the summary, not the whole run: step 2's own `!!` warning names
        # C.UTF-8 on every pair, so the unsplit assertion could not fail.
        self.assertIn('C.UTF-8', summary)

    def test_the_NOT_RUN_block_gives_no_reason_a_tag_can_falsify(self):
        """The block used to say C.UTF-8's "source file is
        in neither tag", which glibc-2.39 falsifies (the file is upstream from
        2.35), and that "nothing above says anything about C.UTF-8", which a
        run given one node's directory falsifies three lines below it."""
        self.assertEqual(summary_block(self.out, NODE_TO_NODE_NOT_RUN),
                         NODE_TO_NODE_NOT_RUN_BODY)

    def test_what_the_tags_cannot_see_is_said_before_their_none(self):
        """Over 2.28..2.34 the tags remove nothing, and the
        upgrade removes en_US@ampm, a file only RHEL8 ships. A "none" as the
        section's first line is that false negative, so the line saying what
        the tags cannot check comes first."""
        self.assertEqual(removed_lines(self.out), NOTHING_GONE_OVER_OLD_MID)

    def test_the_removed_section_is_on_the_first_screen(self):
        """Directly under Reindex, above the long lists and the warnings: the
        defect was a finding nobody reading the summary would reach."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertLess(summary.index('-- Reindex:'), summary.index(REMOVED))
        self.assertLess(summary.index(REMOVED),
                        summary.index('-- Needs an empirical test'))


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

    def test_the_summary_does_not_deny_the_C_that_step_4_just_named(self):
        """Backlog 1.17. Given neither node directory, the summary used to
        justify its NOT RUN with "no tag of this pair holds
        localedata/locales/C" -- true of 2.28..2.34 and false from 2.35 on.

        This pair is glibc-2.39 against itself, where the file is at BOTH
        tags: step 2 says nothing about it (present at the old tag, so it is
        judged like any other file) and step 4 names it, so the old sentence
        was false with nothing else in the summary to contradict it. The
        premise is computed here rather than assumed, so a corpus that stops
        holding the file fails this test instead of passing it vacuously.
        """
        at_new = g.run_git(['ls-tree', '--name-only', NEW, '--',
                            f'{g.LOCALES_DIR}/C'], GLIBC_CLONE).stdout.strip()
        self.assertTrue(at_new, f'{NEW} no longer holds the file this is about')
        self.assertIn('can move them: C', self.out, 'step 4 did not name C')
        summary = self.out.split('AUDIT SUMMARY')[1]
        body = section_body(summary, "-- Node's own ellipsis scan: NOT RUN")
        # The positive half is what keeps the negative one honest: assertNotIn
        # on a phrase the block never contained would pass whatever the block
        # said, which is how three tests in this suite came to guard nothing.
        self.assertIn("a tag holds at most upstream's C", body)
        self.assertNotIn('no tag of this pair holds', body)

    def test_one_tag_compared_with_itself_says_nothing_was_compared(self):
        """An intra-major upgrade -- RHEL 8.1 -> 8.10 -- is two builds of one
        upstream release, so the tag pair is 2.28..2.28 and steps 1-5 are
        structurally empty. C.UTF-8's order moved across exactly such a bump
        (glibc-2.28-93.el8), so "nothing changed" here must not read as a
        clean result."""
        # Through flat(): the notice is one `warn()` block now, wrapped at
        # 78 columns, so the phrase is split across lines in the raw text.
        self.assertIn('are the same commit', flat(self.out))
        self.assertIn('-- One commit, compared with itself', self.out)
        # And it is repeated at the bottom, where a reader who scrolled past
        # step 1 will see it.
        warnings = self.out.split('-- Warnings the clean results above')[1]
        self.assertIn('are the same commit', flat(warnings))
        # The whole sentence, whitespace collapsed. This used to assert the
        # word "means", which every step's prose contains.
        self.assertIn("Everything above that says 'nothing changed' means "
                      "'nothing was compared'", flat(self.out))

    def test_the_one_commit_notice_names_every_step_that_still_has_evidence(self):
        """The notice said the evidence was "the node-to-node check and
        sql/c_utf8_probe.sql, nothing else". Steps 6, 7, 9 and 10 read the
        machines' files on this pair too, and a code change a distro
        backports inside one major shows only on real nodes. Compared whole,
        so an "only" cannot come back in other words."""
        self.assertEqual(summary_block(self.out,
                                       '-- One commit, compared with itself'), [
            f"{NEW} -> {NEW}. Everything above that says 'nothing changed' "
            f"means",
            "'nothing was compared'. For an intra-major upgrade, what can "
            "still",
            "show a change is the machines' own files (steps 6 to 10),",
            "sql/c_utf8_probe.sql and the confirmation on real nodes",
            "(docs/confirming-on-a-real-system.md).",
        ])

    def test_the_removed_none_sits_above_the_one_commit_notice(self):
        """The tags' "none" is printed on one commit compared with itself
        too, and it is the notice below it, not the section, that says
        nothing was compared -- the same arrangement as Reindex. This pins
        the arrangement: the notice must come after the section."""
        lines = removed_lines(self.out)
        self.assertEqual(lines[-1], f'Between the tags: none -- no locale '
                                    f'file at {NEW} is gone at {NEW}')
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertGreater(summary.index('-- One commit, compared with itself'),
                           summary.index(REMOVED))

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
        # The branch that always carried the backport caveat keeps it. Matched
        # without its first word, so this assertion holds both before and after
        # the line moved out of the case: it is the control for the two tests
        # that fail when it moves back in (backlog 1.14).
        self.assertIn("upstream diff cannot see your distro's backports",
                      summary)


@needs_clone
class WrapperSameCommitSpeltTwoWays(unittest.TestCase):
    """"The same-tag guard compared text, not commits."

    `./audit.sh glibc-2.39 ef321e23...` -- the tag and the sha this run's own
    provenance line prints for it -- is one commit compared with itself, and
    the string comparison never fired: rc 0, no "nothing was compared", and a
    summary reading "none -- no locale\'s LC_COLLATE changed". That is the
    same-tag false negative reopened by spelling. Mutation: put the
    text comparison back and this class fails.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-samesha-')
        cls.rc, cls.out = run_wrapper(NEW, EXPECTED_SHA[NEW],
                                      out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_it_says_nothing_was_compared(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn('are the same commit', flat(self.out))
        summary = flat(self.out.split('AUDIT SUMMARY')[1])
        self.assertIn("-- One commit, compared with itself", summary)
        self.assertIn("Everything above that says 'nothing changed' means "
                      "'nothing was compared'", summary)


@needs_clone
class WrapperDirectionUndetermined(unittest.TestCase):
    """The fourth state of the pair: neither commit is an ancestor of the
    other, and the newest glibc tag behind each one does not order them either
    -- a master snapshot against a backport branch off the same release, say.
    The wrapper cannot be pointed at a fabricated clone, so the order
    subcommand is stood in for by a `python3` shim -- the pattern
    WrapperStep5Unresolved uses for step 5 -- and every other invocation goes
    to the real interpreter. Silence here would read as "the direction was
    checked and is fine"."""

    def setUp(self):
        import sys
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-undet-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        shim_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-undetshim-')
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)
        shim = os.path.join(shim_dir, 'python3')
        with open(shim, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\n'
                     'case "$1" in\n'
                     '  *glibc_locale_data.py)\n'
                     '    if [ "$2" = order ]; then echo undetermined; exit 0; '
                     'fi ;;\n'
                     'esac\n'
                     f'exec "{sys.executable}" "$@"\n')
        os.chmod(shim, 0o755)
        self.env = {'PATH': shim_dir + os.pathsep + os.environ.get('PATH', '')}

    def test_the_summary_says_the_direction_was_not_established(self):
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        summary = flat(out.split('AUDIT SUMMARY')[1])
        self.assertIn('-- Direction of the pair: NOT ESTABLISHED', summary)
        self.assertIn('nothing here checked that', summary)
        # Not the other state: an undetermined pair is not one commit.
        self.assertNotIn('One commit, compared with itself', summary)

    def test_an_unrecognised_state_stops_the_run(self):
        """The branch that keeps the four states honest: a word the wrapper
        does not know means the check did not answer, and "the pair is in
        order" is the assumption that runs the audit against the wrong tag."""
        shim_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-badstate-')
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)
        import sys
        shim = os.path.join(shim_dir, 'python3')
        with open(shim, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\n'
                     'case "$1" in\n'
                     '  *glibc_locale_data.py)\n'
                     '    if [ "$2" = order ]; then echo perhaps; exit 0; '
                     'fi ;;\n'
                     'esac\n'
                     f'exec "{sys.executable}" "$@"\n')
        os.chmod(shim, 0o755)
        rc, out = run_wrapper(
            NEW, NEW, out_dir=self.out_dir,
            env_extra={'PATH': shim_dir + os.pathsep
                       + os.environ.get('PATH', '')})
        self.assertNotEqual(rc, 0)
        self.assertIn("answered 'perhaps'", out)
        self.assertNotIn('AUDIT SUMMARY', out)


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

    def test_the_usage_claims_no_single_step_for_C_UTF_8(self):
        """The usage text called step 8 "the only
        source-level evidence there is about C.UTF-8"; steps 6/7 read that
        file and steps 9/10 declare its status with one node alone."""
        rc, out = run_wrapper(OLD, out_dir=self.out_dir)
        self.assertEqual(rc, 2)
        script = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'audit.sh')
        self.assertEqual(out.replace(script, 'audit.sh').rstrip('\n')
                         .split('\n'), USAGE)

    def test_a_reversed_pair_is_refused_before_any_summary(self):
        """"Nothing stopped a reversed pair." Reversed, all five steps run to
        the end and print a plausible clean result: step 4 scans the older
        tag, so the locales added in the newer one -- ckb_IQ and mnw_MM, both
        `copy "iso14651_t1"` -- leave the exposed set, and step 2 reports a
        locale DELETED in the real upgrade as an addition it did not analyse.
        Nothing in the output said which direction it had been given."""
        rc, out = run_wrapper(MID, OLD, out_dir=self.out_dir)
        self.assertEqual(rc, 2, out)
        self.assertNotIn('AUDIT SUMMARY', out)
        self.assertIn('This pair is REVERSED', flat(out))
        self.assertIn('Swap the arguments', flat(out))

    def test_the_wrapper_offers_no_way_to_run_a_reversed_pair(self):
        """--allow-reverse exists on the individual steps, for the deliberate
        backwards read the suite itself does on 2.39 -> 2.34. A reversed WHOLE
        audit answers no question of the method, so the wrapper does not take
        the flag -- and must refuse it as unknown rather than ignoring it."""
        rc, out = run_wrapper(MID, OLD, '--allow-reverse',
                              out_dir=self.out_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("unknown argument '--allow-reverse'", out)
        self.assertNotIn('AUDIT SUMMARY', out)

    def test_an_option_without_a_value_says_so(self):
        """`--new-locales-dir` as the last word on the line died in `shift 2`
        under `set -e`: exit 1, no message at all. An empty value is refused
        for the same reason -- it leaves the variable unset, which is
        indistinguishable from never having asked for the node checks."""
        options = ('--old-locales-dir', '--old-build-id',
                   '--new-locales-dir', '--new-build-id')
        # Every branch of the case, not one of them: three of the four were
        # asserted by nothing, and a mutation to any of those three left the
        # suite green.
        cases = [(opt,) for opt in options] + [(opt, '') for opt in options]
        for tail in cases:
            with self.subTest(args=tail):
                rc, out = run_wrapper(OLD, MID, *tail, out_dir=self.out_dir)
                self.assertEqual(rc, 2, out)
                self.assertIn(f'error: {tail[0]} needs a value', out)
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
        """The payoff line: the two nodes' C.UTF-8 data differ.

        It used to add "in neither tag; no other step sees
        it". Steps 9 and 10 declare C's status in this same summary, steps 6
        and 7 read its file, and over 2.34..2.39 the new tag holds it."""
        block = summary_block(
            self.out.replace(self.out_dir, '<out>'),
            '-- Node-to-node locale data (build-old -> build-new)')
        self.assertEqual(block, [
            '3 locale(s) differ inside LC_COLLATE between the two',
            "nodes' OWN sources; full list: "
            '<out>/node_collate_diffs.build-old..build-new.txt',
            "plus 2 locale(s) that inherit one of those files'",
            'LC_COLLATE via copy on build-new; full list: '
            '<out>/node_collate_inherited.build-old..build-new.txt',
            'C (C.UTF-8): DIFFERS  <- LC_COLLATE is not the same on the two '
            'nodes',
        ])

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

    def test_nothing_removed_is_said_of_the_two_directories(self):
        """With both copies the nodes are the answer, so the section is one
        line and the tags' caveat is not in it. It names the directories, not
        the builds: it is a fact about what the two copies hold."""
        self.assertEqual(removed_lines(self.out),
                         [f'none -- every file in {self.old_root} is also in '
                          f'{self.new_root}'])


@needs_clone
class WrapperTagsRemoveALocale(unittest.TestCase):
    """Tags only, over the one published pair whose tags remove a locale
    file: aa_ER@saaho, renamed to ssy_ER at 2.39. Step 2 printed the rename
    in its own output and the summary never named it."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-tagsgone-')
        cls.rc, cls.out = run_wrapper(MID, NEW, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_rename_away_is_named_with_where_it_went(self):
        self.assertEqual(self.rc, 0, self.out)
        lines = removed_lines(self.out)
        self.assertEqual(lines[0], NOT_CHECKED)
        at = lines.index(f'Between the tags, gone at {NEW}:')
        self.assertEqual(lines[at + 1], 'aa_ER@saaho (renamed to ssy_ER)')
        m = re.fullmatch(r'\((\d+) name\(s\); full list: (\S+)\)',
                         lines[at + 2])
        self.assertIsNotNone(m, lines)
        self.assertEqual(int(m.group(1)), 1)
        self.assertEqual(m.group(2), os.path.join(
            self.out_dir, f'step2_removed_locales.{pair_slug(MID, NEW)}.txt'))
        self.assertNotIn('none', ' '.join(lines))


@needs_clone
class WrapperNodesRemoveALocale(unittest.TestCase):
    """The en_US@ampm shape, driven through the wrapper: a file on the old
    node and in no tag. The tags remove nothing over 2.28..2.34, so only step
    8 can name it -- and the summary must."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-nodesgone-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-gonetrees-')
        cls.old_root = dd.materialise_tag(GLIBC_CLONE, OLD,
                                          os.path.join(cls.nodes, 'a'))
        cls.new_root = dd.materialise_tag(GLIBC_CLONE, MID,
                                          os.path.join(cls.nodes, 'b'))
        with open(os.path.join(cls.old_root, 'zz_GONE'), 'w',
                  encoding='utf-8') as fh:
            fh.write(locale_file('copy "iso14651_t1"'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-locales-dir', cls.old_root, '--old-build-id', 'build-old',
            '--new-locales-dir', cls.new_root, '--new-build-id', 'build-new',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_removal_only_the_nodes_see_is_named(self):
        self.assertEqual(self.rc, 0, self.out)
        lines = removed_lines(self.out)
        self.assertEqual(lines[0], 'zz_GONE')
        tail = ' '.join(lines[1:])
        m = re.match(r'\((\d+) name\(s\) on build-old and not on build-new;',
                     tail)
        self.assertIsNotNone(m, lines)
        path = os.path.join(self.out_dir,
                            'node_removed_locales.build-old..build-new.txt')
        with open(path, encoding='utf-8') as fh:
            names = [ln.strip() for ln in fh
                     if ln.strip() and not ln.startswith('#')]
        self.assertEqual(int(m.group(1)), len(names))
        self.assertEqual(names, ['zz_GONE'])
        self.assertIn('the collation is gone', tail)
        self.assertIn(f'Full list: {path})', tail)

    def test_the_tags_none_is_not_printed_over_it(self):
        """The tags say nothing is removed over this pair. That answer must
        not reach this section at all when both copies were given."""
        joined = ' '.join(removed_lines(self.out))
        self.assertNotIn('none', joined)
        self.assertNotIn('Between the tags', joined)
        self.assertNotIn(NOT_CHECKED, joined)


@needs_clone
class WrapperNodesRemoveAndCannotDecide(unittest.TestCase):
    """Both at once: a file only the old copy has, and a backported C only
    the old copy has. The removal comes first under its own count and the
    undetermined after it under its own heading; no test drove this state,
    and a mutant that hid the removal whenever something was undetermined
    passed the whole suite (false-negative-reviewer on this change)."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-both-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-bothtrees-')
        cls.old_root = dd.materialise_tag(GLIBC_CLONE, OLD,
                                          os.path.join(cls.nodes, 'a'))
        cls.new_root = dd.materialise_tag(GLIBC_CLONE, MID,
                                          os.path.join(cls.nodes, 'b'))
        with open(os.path.join(cls.old_root, 'zz_GONE'), 'w',
                  encoding='utf-8') as fh:
            fh.write(locale_file('copy "iso14651_t1"'))
        with open(os.path.join(cls.old_root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(backported_c())
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-locales-dir', cls.old_root, '--old-build-id', 'build-old',
            '--new-locales-dir', cls.new_root, '--new-build-id', 'build-new',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_removal_and_the_undetermined_are_both_printed_apart(self):
        self.assertEqual(self.rc, 0, self.out)
        path = os.path.join(self.out_dir,
                            'node_removed_locales.build-old..build-new.txt')
        self.assertEqual(removed_lines(self.out), [
            'zz_GONE',
            '(1 name(s) on build-old and not on build-new;',
            'an index on one does not just sort differently; the collation '
            'is gone.',
            f'Full list: {path})',
            'UNDETERMINED -- step 8 cannot say whether the upgrade removes '
            'these:',
            'C: backported (C.UTF-8), on the old node only: not examined',
        ])


@needs_clone
class WrapperNodeCopiesDisagreeWithTheTags(unittest.TestCase):
    """Both copies over 2.34..2.39, arranged so the tags and the nodes give
    different answers. The new copy keeps aa_ER@saaho, which the tags rename
    away, so the nodes remove nothing the tags do. And the new copy lacks C,
    which the old one has: step 8's verdict on that is "not examined", so the
    section is UNDETERMINED and may not print "none"."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-disagree-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-disagreetrees-')
        cls.old_root = dd.materialise_tag(GLIBC_CLONE, MID,
                                          os.path.join(cls.nodes, 'a'))
        cls.new_root = dd.materialise_tag(GLIBC_CLONE, NEW,
                                          os.path.join(cls.nodes, 'b'))
        with open(os.path.join(cls.old_root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(backported_c())
        os.remove(os.path.join(cls.new_root, 'C'))
        shutil.copy(os.path.join(cls.old_root, 'aa_ER@saaho'), cls.new_root)
        cls.rc, cls.out = run_wrapper(
            MID, NEW,
            '--old-locales-dir', cls.old_root, '--old-build-id', 'build-old',
            '--new-locales-dir', cls.new_root, '--new-build-id', 'build-new',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_undetermined_comes_first_and_there_is_no_none(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertEqual(removed_lines(self.out), [
            'UNDETERMINED -- step 8 cannot say whether the upgrade removes '
            'these:',
            'C: backported (C.UTF-8), on the old node only: not examined',
            f'every other file in {self.old_root} is also in {self.new_root}',
        ])

    def test_the_tags_rename_is_not_relayed_when_the_new_copy_keeps_it(self):
        """The control first: step 2 did list aa_ER@saaho for this pair, so
        its absence from the section is the choice of source, not a list that
        came out empty."""
        with open(os.path.join(self.out_dir, f'step2_removed_locales.'
                                             f'{pair_slug(MID, NEW)}.txt'),
                  encoding='utf-8') as fh:
            self.assertIn('aa_ER@saaho (renamed to ssy_ER)', fh.read())
        self.assertNotIn('aa_ER@saaho', ' '.join(removed_lines(self.out)))


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

    def test_the_NOT_RUN_block_does_not_deny_the_scan_below_it(self):
        """Backlog 1.18. With one directory the block said
        "nothing above says anything about C.UTF-8" over a summary that
        declares the old node's C status a few lines further down."""
        self.assertEqual(summary_block(self.out, NODE_TO_NODE_NOT_RUN),
                         NODE_TO_NODE_NOT_RUN_BODY)

    def test_the_side_that_was_not_scanned_says_NOT_RUN(self):
        """Absent is not empty, one level below the node-to-node block.

        The summary printed the old node's ellipsis verdict and simply left
        the new node out -- and a section that is not there reads exactly
        like a section with nothing to report. A one-sided run is a
        supported shape (docs/commands.md), so
        this was the reader's ordinary view, not a misuse (backlog 1.16).
        """
        summary = self.out.split('AUDIT SUMMARY')[1]
        heading = "-- Node's own locale data, ellipsis scan: NOT RUN"
        self.assertIn(heading, summary)
        # On this block alone. The node-to-node NOT RUN above it names both
        # flags, so either assertion below would be vacuous on the whole
        # summary -- the first always true, the second always false.
        body = section_body(summary, heading)
        self.assertIn('--new-locales-dir', body)
        self.assertNotIn('--old-locales-dir', body)

    def test_one_copy_is_not_enough_for_the_nodes_answer_on_removals(self):
        """Step 8 needs both copies, so the section takes the tags and says
        first what that leaves unchecked. The whole section, not its ends:
        a complete copy has nothing undetermined, and a line printed for an
        empty list would sit between them unseen."""
        self.assertEqual(removed_lines(self.out), NOTHING_GONE_OVER_OLD_MID)

    def test_a_complete_copy_raises_no_missing_files_warning(self):
        """The control for WrapperOldSideLostFiles."""
        self.assertNotIn('are missing from', flat(self.out))


@needs_clone
class WrapperNewSideOnly(unittest.TestCase):
    """The mirror of WrapperOneSideOnly: only --new-locales-dir.

    One class exercising one of the two sides leaves a block that names a
    FIXED side passing: on a new-only run such a block prints "No
    --new-locales-dir" directly above the scan of build-new, which tells the
    reader the side just scanned is the one that was not. Measured by
    false-negative-reviewer, 2026-09-21, as a mutant the first test survived.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-newside-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-newtree-')
        root = dd.materialise_tag(GLIBC_CLONE, MID, os.path.join(cls.nodes, 'b'))
        with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(backported_c())
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--new-locales-dir', root, '--new-build-id', 'build-new',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_side_that_was_not_scanned_is_the_old_one(self):
        self.assertEqual(self.rc, 0, self.out)
        summary = self.out.split('AUDIT SUMMARY')[1]
        heading = "-- Node's own locale data, ellipsis scan: NOT RUN"
        self.assertIn(heading, summary)
        body = section_body(summary, heading)
        self.assertIn('--old-locales-dir', body)
        self.assertNotIn('--new-locales-dir', body)
        # And the side that DID run is still reported, under the heading that
        # carries its build id -- the NOT RUN must not replace it.
        self.assertIn("-- Node's own locale data, ellipsis scan (build-new)",
                      summary)

    def test_one_copy_is_not_enough_for_the_nodes_answer_on_removals(self):
        """The mirror of WrapperOneSideOnly's test of the same name."""
        self.assertEqual(removed_lines(self.out), NOTHING_GONE_OVER_OLD_MID)

    def test_the_NOT_RUN_block_does_not_deny_the_scan_below_it(self):
        """The mirror of WrapperOneSideOnly's test of the same name."""
        self.assertEqual(summary_block(self.out, NODE_TO_NODE_NOT_RUN),
                         NODE_TO_NODE_NOT_RUN_BODY)

    def test_a_complete_copy_raises_no_missing_files_warning(self):
        """The control for WrapperNewSideLostFiles."""
        self.assertNotIn('are missing from', flat(self.out))


class WrapperSideLostFiles:
    """A copy that lost files reached the AUDIT SUMMARY in no form at all.

    Steps 6/7 listed the lost files with no `!!`, and the summary repeats
    only `!!` blocks (backlog 1.15). Now steps 6/7 and 9/10
    each print the same block, and the summary, which drops identical
    blocks, prints it once. One class per side, because a block naming a
    fixed side would pass a test that drives only one of them.
    """

    SIDE = TAG = STEPS = None

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-lost-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-losttree-')
        root = dd.materialise_tag(GLIBC_CLONE, cls.TAG,
                                  os.path.join(cls.nodes, 'n'))
        cls.lost = sorted(os.listdir(root))[300:]
        for name in cls.lost:
            os.remove(os.path.join(root, name))
        with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(backported_c())
        cls.rc, cls.out = run_wrapper(
            OLD, MID, f'--{cls.SIDE}-locales-dir', root,
            f'--{cls.SIDE}-build-id', f'build-{cls.SIDE}',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    HEAD = re.compile(r'^!! (\d+) file\(s\) that (\S+) has under '
                      r'localedata/locales/ are missing from', re.M)

    def test_both_steps_that_read_the_copy_say_so(self):
        self.assertEqual(self.rc, 0, self.out)
        for n in self.STEPS:
            with open(os.path.join(self.out_dir,
                                   f'step{n}.{pair_slug(OLD, MID)}.log'),
                      encoding='utf-8') as fh:
                found = self.HEAD.findall(fh.read())
            self.assertEqual(found, [(str(len(self.lost)), self.TAG)], n)

    def test_the_summary_repeats_it_once_under_the_warnings(self):
        summary = self.out.split('AUDIT SUMMARY')[1]
        warnings = summary.split(
            '-- Warnings the clean results above do NOT cover')[1]
        warnings = warnings.split('\n--')[0]
        heads = re.findall(r'!! (\d+) file\(s\) that (\S+) has under',
                           warnings)
        self.assertEqual(heads, [(str(len(self.lost)), self.TAG)], warnings)
        # The whole list, not each name: a name that prefixes another
        # (tt_RU, tt_RU@iqtelif) passed a per-name check with itself dropped.
        names = re.search(r'are missing from \S+: (.*?)\. ',
                          flat(warnings)).group(1).split(', ')
        self.assertEqual(names, self.lost)

    def test_the_removed_section_on_a_copy_that_lost_files(self):
        """The new copy alone: what it lacks may be what
        the new machine does not ship, so the section says it cannot decide
        those rather than ending on the tags' "none" -- measured printing
        "none" above step 7's `!!` for exactly this fixture. The old copy
        alone: what it lacks is not a removal, and nothing is added."""
        lines = removed_lines(self.out)
        if self.SIDE == 'old':
            self.assertEqual(lines, NOTHING_GONE_OVER_OLD_MID)
            return
        self.assertEqual(lines[0], NOT_CHECKED)
        self.assertEqual(lines[-1], f'Between the tags: none -- no locale '
                                    f'file at {OLD} is gone at {MID}')
        heads = [i for i, ln in enumerate(lines)
                 if ln.startswith('UNDETERMINED')]
        self.assertEqual(len(heads), 1, lines)
        at = heads[0]
        self.assertEqual(lines[at], f'UNDETERMINED -- files of {MID} the new '
                                    f'copy does not hold. If')
        self.assertEqual(lines[at + 2], 'machine had:')
        end = next(i for i in range(at + 3, len(lines))
                   if lines[i].startswith('('))
        self.assertEqual(lines[at + 3:end], self.lost)
        m = re.fullmatch(r'\((\d+) name\(s\); full list: (\S+)\)', lines[end])
        self.assertIsNotNone(m, lines[end])
        self.assertEqual(int(m.group(1)), len(self.lost))


@needs_clone
class WrapperOldSideLostFiles(WrapperSideLostFiles, unittest.TestCase):
    SIDE, TAG, STEPS = 'old', OLD, (6, 9)


@needs_clone
class WrapperNewSideLostFiles(WrapperSideLostFiles, unittest.TestCase):
    SIDE, TAG, STEPS = 'new', MID, (7, 10)


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
    C-collate-seq.c but still finds 52 hunks. So step 5 is stood in for by a
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
        "No substantive change in the files this audit could read.\n"
        "This is NOT a clean result:\n"
        "  - 1 tracked path(s) vanished before glibc-2.39\n"
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
        # The caveat is true of this branch too: the run that could not read
        # the upstream code still cannot see a distro patch. It printed in the
        # `clean` branch only until backlog 1.14 was fixed.
        self.assertIn("An upstream diff cannot see your distro's backports",
                      flat)

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
