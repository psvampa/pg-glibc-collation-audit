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
import ast
import glob
import json
import os
import re
import shutil
import tempfile
import unittest

from _harness import (EXPECTED_SHA, MID, NEW, OLD, REPO_ROOT, backported_c,
                      flat, locale_file, needs_clone, run_wrapper, upstream_c)

import diff_distro_locales as dd
import glibc_locale_data as g
import locale_order as lom
from _harness import GLIBC_CLONE
from test_locale_order import (CHANGED_8_TO_9, RHEL8, RHEL9, RHEL10,
                               parse_summary)


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
    'Pass --old-node and --new-node instead of every other --old-*/--new-*',
    'option, or --old-locales-dir and --new-locales-dir with their build ids.',
    f'Between the tags: none -- no locale file at {OLD} is gone at {MID}',
]


# audit.sh's usage text, whole, with the script's own path as "audit.sh".
USAGE = [
    'usage: audit.sh <old_tag> <new_tag>',
    '         [--old-locales-dir DIR --old-build-id NVR]',
    '         [--new-locales-dir DIR --new-build-id NVR]',
    '         [--old-order FILE --new-order FILE]',
    '         [--old-node FILE --new-node FILE]',
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
    '',
    '       The --*-order options are OPTIONAL and go together: each is the',
    '       output of scripts/locale_order.py run on that machine, which',
    "       asks the machine's own glibc how every locale sorts (step 11).",
    '       It sees what a comparison of files cannot: a locale whose files',
    '       did not change can still sort differently.',
    '',
    '       The --*-node options are OPTIONAL and go together. Each is the',
    '       one file scripts/locale_order.py --extract wrote on that',
    '       machine, with its measurement, its build and its locale sources',
    '       (a file without the sources says why), and stands for all the',
    '       options above.',
]

REINDEX = '-- Reindex: sort order changes, confirm then REINDEX'
REINDEX_POINTER = [
    "This list is from the tags. What each machine's own glibc says",
    "is under '-- Measured order', below.",
]
MEASURED = "-- Measured order: each machine's own glibc"
MEASURED_NOT_RUN = '-- Measured order: NOT RUN'
MEASURED_NOT_RUN_BODY = [
    'Pass --old-node and --new-node instead of every other --old-*/--new-*',
    'option, or --old-order and --new-order, each written by',
    'scripts/locale_order.py on that machine. Without them nothing above',
    "asked either machine's glibc how it sorts, and a locale whose files",
    'did not change can still sort differently.',
]
MEASURED_FOOTER = '     Full report: step 11 above.'
# audit.sh's last word when step 11 exits non-zero: a refusal or a crash.
COULD_NOT_COMPARE = ('error: step 11 could not compare the two measurements '
                     '(above). Not continuing.')

NODE_TO_NODE_NOT_RUN = '-- Node-to-node locale data: NOT RUN'
# The whole block, compared line for line. A check for one phrase would let
# the same false claim come back in other words.
NODE_TO_NODE_NOT_RUN_BODY = [
    'Pass --old-node and --new-node instead of every other --old-*/--new-*',
    'option, or --old-locales-dir and --new-locales-dir with their build ids.',
    "Without both, nothing above compared the two nodes' C.UTF-8",
    'against each other, and PostgreSQL reports collversion as NULL for',
    'every C.* collation, so no mismatch can ever fire.',
    'Then run sql/c_utf8_probe.sql on both nodes.',
]
# The node's-own ellipsis scan block when no machine was given, whole. One
# phrase of it was all a test read, and its closing sentence could go with
# every test green (false-negative-reviewer, 2026-09-30).
ELLIPSIS_NOT_RUN_BODY = [
    'Pass --old-node and --new-node instead of every other --old-*/--new-*',
    'option, or --old-locales-dir and --new-locales-dir with their build ids.',
    "Step 4 above scanned the TAG, and a tag holds at most upstream's C:",
    'the distros this audit targets ship their own C.UTF-8, so if step 4',
    "named C at all, that verdict is evidence about upstream's file and",
    "none about either node's. Nothing above says how either node's own",
    "C.UTF-8 defines its order, and unless both nodes' copies declare",
    'codepoint_collation, a data diff, including the node-to-node one,',
    'cannot clear it.',
]
# The steps 6 and 7 block when no machine was given, whole, like the others.
DISTRO_NOT_RUN_BODY = [
    'Pass --old-node and --new-node instead of every other --old-*/--new-*',
    'option, or --old-locales-dir and --new-locales-dir with their build ids.',
    "So nothing above says whether either machine's own patches touch",
    'LC_COLLATE, which the tags cannot show.',
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


def step_text(out, n):
    """What step `n` printed, from its banner to the next step's.

    Its banner must be a line exactly once, and a later banner must end it,
    so a missing step or a cut that would run to the end of the output fails
    rather than reading more than the step printed.
    """
    lines = out.splitlines()
    at = [i for i, line in enumerate(lines)
          if line.startswith(f'== STEP {n} ')]
    if len(at) != 1:
        raise AssertionError(f'the step {n} banner is a line {len(at)} '
                             f'time(s):\n{out}')
    for j in range(at[0] + 1, len(lines)):
        if lines[j].startswith('== STEP '):
            return '\n'.join(lines[at[0]:j])
    raise AssertionError(f'no banner ends step {n}:\n{out}')


def measured_block(out):
    """The Measured order block of the summary as written, indentation kept,
    without its heading and its footer. Refuses a block that appears other
    than once or does not end at its footer."""
    summary = out.split('AUDIT SUMMARY', 1)[-1]
    lines = summary.splitlines()
    at = [i for i, line in enumerate(lines) if line == MEASURED]
    if len(at) != 1:
        raise AssertionError(f'{MEASURED!r} is a whole line {len(at)} '
                             f'time(s):\n{summary}')
    body = []
    for line in lines[at[0] + 1:]:
        if line == MEASURED_FOOTER:
            return '\n'.join(body) + '\n'
        if not line.strip() or line.startswith('--'):
            break
        body.append(line)
    raise AssertionError(f'the Measured order block does not end at its '
                         f'footer:\n{summary}')


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


def distro_clean(tag, step, unread=('C',)):
    """The steps 6/7 block of a side whose compared files do not differ, with
    the locales the step could not compare named after it."""
    body = [f'No locale compared with {tag} differs inside LC_COLLATE.']
    if unread:
        body += [f'Not compared, because {tag} lacks them, and with sort rules of',
                 f'their own, so no tag diff reads them (step {step} above):',
                 ' '.join(unread)]
    return body


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

    def test_step_4_scanned_the_new_tag(self):
        """The same check as WrapperTagsRemoveALocale's, on the pair whose new
        tag is not glibc-2.39: there the right tag and a hard-coded one are
        the same string, so only this pair can tell them apart."""
        self.assertEqual(self.rc, 0, self.out)
        step4 = step_text(self.out, 4)
        self.assertIn(f'\nFiles at {MID}: ', step4)
        self.assertNotIn(f'Files at {OLD}', flat(step4))

    def test_step_3_read_the_new_tag(self):
        """The premise of SkippingAReleaseReportsTheUnion's step 3 test, which
        runs the step on its own and so cannot see what audit.sh passes it:
        step 3 closes over the NEW tag's copy graph. Given the old one, both
        published pairs still print the same list (sv_FI copies sv_SE at 2.28
        as at 2.34), so only the tag the step names tells the two apart."""
        self.assertEqual(self.rc, 0, self.out)
        step3 = step_text(self.out, 3)
        self.assertIn(f'\nCopy chains read at {MID}\n', step3)
        self.assertNotIn(f'Copy chains read at {OLD}', flat(step3))

    def test_the_step_4_lines_say_how_to_read_their_list(self):
        """The `hunks` branch of the step 4 summary lines, the one every
        published example prints; WrapperEmptyPair holds the other branch
        (backlog 13.1)."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        block = summary.split('-- Needs an empirical test', 1)[1]
        block = block.split('\n\n', 1)[0]
        self.assertIn('step 5 found', block)
        self.assertIn('A collation or a database uses one when its locale, '
                      'without the part from the dot up to any @, is one of '
                      'them.', flat(block))

    def test_answer_matches_the_published_result(self):
        """The published set for 2.28..2.34: locales, and the alias glibc gives
        one of them (backlog 13.1).

        examples/rhel8-to-rhel9-tags-only.txt prints these. A wrapper that
        changes an answer is a bug, not a feature. This does not catch step 3
        handed the old tag, whose set is the same at 2.28;
        test_step_3_read_the_new_tag does.
        """
        listed = os.path.join(self.out_dir, 'step3_affected_locales.txt')
        with open(listed, encoding='utf-8') as fh:
            names = sorted(n.strip() for n in fh if n.strip())
        self.assertEqual(names, ['or_IN', 'sv_FI', 'sv_FI@euro', 'sv_SE',
                                 'swedish'])

    def test_the_distro_patch_block_says_it_did_not_run(self):
        """Steps 6 and 7 need the machines' files. Until 2026-09-27 the
        summary said nothing about them when they did not run, which read the
        same as a check that ran and found nothing."""
        self.assertEqual(summary_block(
            self.out, '-- Distro patches, steps 6 and 7: NOT RUN'),
            DISTRO_NOT_RUN_BODY)

    def test_sv_FI_reached_through_its_copy_of_sv_SE(self):
        """Between 2.28 and 2.34 sv_FI's own file does not change; it is in
        the output because step 3 followed its `copy "sv_SE"`. It cannot
        tell which tag step 3 read, since sv_FI copies sv_SE at 2.28 too;
        test_step_3_read_the_new_tag does.
        """
        self.assertIn('\n  sv_FI: copy "sv_SE" -> reaches sv_SE\n',
                      step_text(self.out, 3))

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

    def test_reindex_points_nowhere_else_without_the_measurements(self):
        self.assertNotIn(REINDEX_POINTER[0],
                         summary_block(self.out, REINDEX))

    def test_the_summary_says_the_measured_order_was_NOT_RUN(self):
        """Absent is not empty: without the two machines' measurements the
        summary says what went unasked, not nothing."""
        self.assertEqual(summary_block(self.out, MEASURED_NOT_RUN),
                         MEASURED_NOT_RUN_BODY)

    def test_the_NOT_RUN_block_gives_no_reason_a_tag_can_falsify(self):
        """The block used to say C.UTF-8's "source file is
        in neither tag", which glibc-2.39 falsifies (the file is upstream from
        2.35), and that "nothing above says anything about C.UTF-8", which a
        run given one node's directory falsifies three lines below it."""
        self.assertEqual(summary_block(self.out, NODE_TO_NODE_NOT_RUN),
                         NODE_TO_NODE_NOT_RUN_BODY)

    def test_the_ellipsis_NOT_RUN_block_says_what_it_leaves_open(self):
        """Absent is not empty: without either machine's files the block says
        what a scan of the tag cannot clear, compared line for line."""
        self.assertEqual(summary_block(self.out, ELLIPSIS_BOTH_NOT_RUN),
                         ELLIPSIS_NOT_RUN_BODY)

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

    def test_step_3_says_nothing_was_compared(self):
        """Skipped over an empty step 2, step 3 called that "a real result,
        not a failure" over one commit compared with itself, where nothing
        was compared (doc-sweep, 2026-09-30). The other branch is held by
        WrapperDirectionUndetermined."""
        step3 = flat(step_text(self.out, 3))
        self.assertIn('The two tags are one commit, so nothing was compared',
                      step3)
        self.assertNotIn('a real result', step3)

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
        upstream release, so the tag pair is 2.28..2.28 and the steps that
        compare the two tags have nothing to compare. C.UTF-8's order moved
        across exactly such a bump
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
        self.assertIn("Everything above that compares the two tags and says "
                      "'nothing changed' means 'nothing was compared'", flat(self.out))

    def test_the_one_commit_notice_names_every_step_that_still_has_evidence(self):
        """The notice said the evidence was "the node-to-node check and
        sql/c_utf8_probe.sql, nothing else". Steps 6, 7, 9 and 10 read the
        machines' files on this pair too, and a code change a distro
        backports inside one major shows only on real nodes. Compared whole,
        so an "only" cannot come back in other words."""
        self.assertEqual(summary_block(self.out,
                                       '-- One commit, compared with itself'), [
            f"{NEW} -> {NEW}. Everything above that compares the two tags",
            "and says 'nothing changed' means 'nothing was compared'. For an",
            "intra-major upgrade, what can still show a change is the "
            "machines'",
            "own files (steps 6 to 10), the measured order (step 11),",
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

    def test_one_commit_leaves_step_4_unresolved(self):
        """One tag against itself compares nothing, and step 5 used to print
        its clean sentence over it, which the summary turned into "a clean
        data diff is sufficient even for the locales step 4 flagged"
        (false-negative-reviewer, 2026-09-30). It is not a clean result now,
        and the summary says why. The clean branch is driven by
        WrapperStep5Clean."""
        summary = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertNotIn('a clean data diff is sufficient', summary)
        self.assertIn('step 5 did NOT reach a clean result, so the locales '
                      'step 4 flagged stay UNRESOLVED', summary)
        self.assertIn('the two tags are one commit', summary)
        # The caveat is true of this branch too: a run that compared nothing
        # still cannot see a distro patch. It printed in the `clean` branch
        # only until backlog 1.14 was fixed.
        self.assertIn("upstream diff cannot see your distro's backports",
                      summary)

    def test_the_step_4_lines_say_how_to_read_their_list(self):
        """Reindex is "none" here, so the explanation printed under a
        non-empty Reindex list is not printed. The step 4 lines carry the rule
        themselves, and must not send the reader to that explanation, which
        is not there (backlog 13.1)."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn('none -- no locale', summary)
        self.assertIn('A collation or a database uses one when its locale, '
                      'without the part from the dot up to any @, is one of '
                      'them.', flat(summary))
        self.assertNotIn('read as the Reindex list is', flat(summary))


@needs_clone
class WrapperSameCommitSpeltTwoWays(unittest.TestCase):
    """"The same-tag guard compared text, not commits."

    `./audit.sh glibc-2.39 ef321e23...` -- the tag and the sha this run's own
    provenance line prints for it -- is one commit compared with itself, and
    the string comparison never fired: rc 0, no "nothing was compared", and a
    summary reading "none -- no locale\'s LC_COLLATE changed". That is the
    same-tag false negative reopened by spelling. Mutation: put the
    text comparison back and this class fails. Step 5 asks git the same
    question for its own verdict, and this is the class that fails if it
    compares the two names as text instead.
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
        self.assertIn("Everything above that compares the two tags and says "
                      "'nothing changed' means 'nothing was compared'", summary)
        # Compared as text, the tag and its sha are two commits, and step 5
        # printed its clean sentence over this pair (false-negative-reviewer,
        # 2026-09-30, measured with that mutant).
        self.assertIn('step 5 did NOT reach a clean result', summary)
        self.assertNotIn('a clean data diff is sufficient', summary)


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

    def test_step_3_keeps_its_sentence_when_the_pair_is_two_commits(self):
        """The other branch of step 3's skip notice. With the order check
        stood in for, the wrapper takes this pair for two commits, step 2
        still finds nothing, and the notice must not say that nothing was
        compared."""
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        step3 = flat(step_text(out, 3))
        self.assertIn('This is a real result, not a failure', step3)
        self.assertNotIn('nothing was compared', step3)

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
        # The refusal told the reader to pass --allow-reverse, which audit.sh
        # refuses as an unknown argument (the next test).
        self.assertNotIn('--allow-reverse', out)

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

    def test_a_locales_dir_that_is_not_a_directory_is_refused_first(self):
        """Measured on 381b5dc: `--old-locales-dir /no/such/dir` ran steps 1
        to 5, and step 6 then refused it as "--locales-dir /no/such/dir is not
        a directory", an option the reader never typed. Each side is checked
        on its own, so each side has its own case."""
        missing = os.path.join(self.out_dir, 'no-such-dir')
        for side in ('old', 'new'):
            with self.subTest(side=side):
                rc, out = run_wrapper(OLD, MID, f'--{side}-locales-dir',
                                      missing, f'--{side}-build-id', 'x',
                                      out_dir=self.out_dir)
                self.assertEqual(rc, 2, out)
                self.assertIn(f'error: --{side}-locales-dir {missing} is not '
                              f'a directory', out)
                self.assertNotIn('STEP 1', out)

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

    def test_the_distro_patch_block_reads_each_side_clean(self):
        for n, build, tag in ((6, 'build-old', OLD), (7, 'build-new', MID)):
            with self.subTest(step=n):
                self.assertEqual(summary_block(
                    self.out, f'-- Distro patches, step {n} ({build} '
                              f'against {tag})'), distro_clean(tag, n))

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
class WrapperNonAsciiBuildId(unittest.TestCase):
    """A build id with a letter outside ASCII. audit.sh named step 8's lists
    with bash's own replacement, which under LC_ALL=C replaces each byte of
    such a letter, while step 8 writes them under glibc_locale_data.pair_slug,
    which replaces the letter once. The summary then printed NOT REPORTED over
    what step 8 had found (false-negative-reviewer, 2026-09-30). Run under
    LC_ALL=C, where the two spellings differed on both platforms measured:
    macOS with bash 3.2, and RHEL 9 with bash 5.1."""

    OLD_BUILD, NEW_BUILD = 'build-öld', 'build-new'

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-nonascii-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-trees-')
        cls.old_root = dd.materialise_tag(GLIBC_CLONE, OLD,
                                          os.path.join(cls.nodes, 'a'))
        cls.new_root = dd.materialise_tag(GLIBC_CLONE, MID,
                                          os.path.join(cls.nodes, 'b'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-locales-dir', cls.old_root, '--old-build-id', cls.OLD_BUILD,
            '--new-locales-dir', cls.new_root, '--new-build-id', cls.NEW_BUILD,
            out_dir=cls.out_dir, env_extra={'LC_ALL': 'C'})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_summary_reads_the_lists_step_8_wrote(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertNotIn('NOT REPORTED', ' '.join(removed_lines(self.out)))
        block = ' '.join(summary_block(
            self.out, f'-- Node-to-node locale data ({self.OLD_BUILD} -> '
                      f'{self.NEW_BUILD})'))
        self.assertIn('differ inside LC_COLLATE', block)
        slug = g.pair_slug(self.OLD_BUILD, self.NEW_BUILD)
        self.assertIn(f'node_collate_diffs.{slug}.txt', block)


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

    def test_step_4_scanned_the_new_tag(self):
        """The premise of SkippingAReleaseReportsTheUnion's step 4 test, which
        runs the step on its own and so cannot see what audit.sh passes it:
        step 4 reads the NEW tag. Given the old one, the run still ends at
        exit 0 with a plausible summary (the header of audit.sh records that
        it once did), and the direct pair loses what was added on the way."""
        self.assertEqual(self.rc, 0, self.out)
        step4 = step_text(self.out, 4)
        self.assertIn(f'\nFiles at {NEW}: ', step4)
        self.assertNotIn(f'Files at {MID}', flat(step4))

    def test_step_3_read_the_new_tag(self):
        """The same check as Wrapper's, on the pair whose new tag is not
        glibc-2.34: there the right tag and a hard-coded one are the same
        string, so only this pair can tell them apart."""
        self.assertEqual(self.rc, 0, self.out)
        step3 = step_text(self.out, 3)
        self.assertIn(f'\nCopy chains read at {NEW}\n', step3)
        self.assertNotIn(f'Copy chains read at {MID}', flat(step3))


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

    def test_the_distro_patch_block_names_the_side_left_out(self):
        self.assertEqual(summary_block(
            self.out, f'-- Distro patches, step 6 (build-old against {OLD})'),
            distro_clean(OLD, 6))
        body = ' '.join(summary_block(self.out,
                                      '-- Distro patches, step 7: NOT RUN'))
        self.assertIn('Pass --new-locales-dir with --new-build-id.', body)
        self.assertIn("the new machine's own patches", body)

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
        # The node-to-node block only: the steps 6/7 block of the same summary
        # compares each node with its tag, and here the old node holds 2.34's
        # files against the 2.28 tag, so it rightly reports differences.
        block = ' '.join(summary_block(
            self.out, '-- Node-to-node locale data (build-x -> build-y)'))
        self.assertIn("no locale differs inside LC_COLLATE between the two "
                      "nodes' own sources", block)
        self.assertNotIn('locale(s) differ inside LC_COLLATE', block)
        self.assertNotIn('plus ', block)

    def test_the_data_only_caveat_names_both_things_localedef_computes(self):
        """The one caveat on a clean data result. It named only the weights
        an ellipsis range expands to, which gave a reader of ja_JP or ar_SA no
        reason to doubt it; the default weight is the other (backlog 1.6)."""
        block = ' '.join(' '.join(summary_block(
            self.out, '-- Node-to-node locale data (build-x -> build-y)'))
            .split())
        self.assertIn('Data only -- the weights an ellipsis range expands to, '
                      'and the default weight of every character a locale '
                      'does not list, are computed by localedef, not stored '
                      'in these files.', block)

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

    No tag pair has a vanished path and nothing else to report: going
    forward in time no tracked path has ever vanished, and reversed, 2.39 ->
    2.34 loses C-collate-seq.c but still finds 52 hunks. So step 5 is stood in
    for by a `python3` shim on PATH that prints the lines the real script
    prints for a vanished path -- its `!!` block is the text test_known_answers
    ties to the real script on the reversed pair -- and hands every other step
    to the real interpreter.
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
        # Text only the canned output carries: the real step 5 also says "NOT
        # a clean result" on one commit compared with itself.
        self.assertIn('1 tracked path(s) vanished before glibc-2.39', out,
                      'the shim did not run')
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
class WrapperStep5Clean(unittest.TestCase):
    """The summary's clean step 5 branch, "a clean data diff is sufficient".

    One tag against itself was the one real input that reached it, and that
    is not a clean result any more: it compares nothing. No pinned pair of
    two different tags has zero substantive hunks, so step 5 is stood in for
    by a `python3` shim that prints the real script's clean sentence over
    glibc-2.28..glibc-2.34, as WrapperStep5Unresolved does for its branch.
    """

    CANNED = (
        "Collation code changes between glibc-2.28 and glibc-2.34\n"
        "\n"
        "No substantive collation code change. Every locale whose data file "
        "is unchanged\n"
        "is genuinely unaffected, including every locale that\n"
        "flag_algorithmic_ranges.py lists -- steps 1-3 are sufficient for "
        "this pair.\n")

    @classmethod
    def setUpClass(cls):
        import sys
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-clean5-')
        cls.shim_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-shim-')
        canned = os.path.join(cls.shim_dir, 'step5.txt')
        with open(canned, 'w', encoding='utf-8') as fh:
            fh.write(cls.CANNED)
        shim = os.path.join(cls.shim_dir, 'python3')
        with open(shim, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\n'
                     'case "$1" in\n'
                     f'  *diff_collation_code.py) cat "{canned}"; exit 0 ;;\n'
                     'esac\n'
                     f'exec "{sys.executable}" "$@"\n')
        os.chmod(shim, 0o755)
        cls.rc, cls.out = run_wrapper(
            OLD, MID, out_dir=cls.out_dir,
            env_extra={'PATH': cls.shim_dir + os.pathsep
                       + os.environ.get('PATH', '')})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.shim_dir, ignore_errors=True)

    def test_the_clean_step_5_branch_is_the_one_printed(self):
        """The summary must say "sufficient", and NOT the unresolved wording,
        which is what an absent clean sentence produces."""
        self.assertEqual(self.rc, 0, self.out[-2000:])
        summary = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        # The real step 5 finds 24 hunks on this pair, so this line is only
        # printed when the shim ran.
        self.assertIn('Step 5 found no substantive change, so a clean data '
                      'diff is sufficient even for the locales step 4 flagged',
                      summary)
        self.assertIn('Nothing from step 5.', summary)
        self.assertNotIn('did NOT reach a clean result', summary)
        # The branch that always carried the backport caveat keeps it. Matched
        # without its first word, so this assertion holds both before and after
        # the line moved out of the case: it is the control for the tests that
        # fail when it moves back in (backlog 1.14).
        self.assertIn("upstream diff cannot see your distro's backports",
                      summary)


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
class WrapperNodeCWithExplicitWeights(unittest.TestCase):
    """A C whose weights are written one by one: no range for localedef to
    expand, and every character it does not list still takes localedef's
    default weight (backlog 1.6). Step 9 lists it, so the line the summary
    relays has to say it is exposed rather than stop at "no ellipsis range".
    No RHEL build ships this shape; it is injected."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-cexpl-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-cexpltree-')
        root = dd.materialise_tag(GLIBC_CLONE, OLD, os.path.join(cls.nodes, 'a'))
        with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(locale_file('order_start forward',
                                 '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                 'order_end'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-locales-dir', root, '--old-build-id', 'build-old',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_summary_says_the_default_weight_exposes_it(self):
        self.assertEqual(self.rc, 0, self.out)
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertIn("C (C.UTF-8): explicit weights <- no ellipsis range for "
                      "localedef to expand; and every character it does not "
                      "list takes localedef's default weight -- so this "
                      "locale IS exposed", flat)


@needs_clone
class WrapperNodeCNamesTheKeywordNotAlone(unittest.TestCase):
    """Backlog 13.2, where it surfaces. A C that copies `iso14651_t1` and
    also holds codepoint_collation compiles on RHEL9 and RHEL10 into tables
    whose strcoll returns garbage and crashes (glibc study, E5). Step 9
    called it byte order, and the summary printed "byte order by
    construction" over it. The summary decides C's line by how the step's
    status starts, so the new status must not start with the keyword. No
    RHEL build ships this shape; it is injected."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-cmixed-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-cmixedtree-')
        root = dd.materialise_tag(GLIBC_CLONE, OLD, os.path.join(cls.nodes, 'a'))
        with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
            fh.write(locale_file('copy "iso14651_t1"', 'codepoint_collation'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-locales-dir', root, '--old-build-id', 'build-old',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_summary_does_not_call_it_byte_order(self):
        self.assertEqual(self.rc, 0, self.out)
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        self.assertIn('C (C.UTF-8): names codepoint_collation, but could not '
                      'be read as the keyword alone in its LC_COLLATE <- the '
                      'one form glibc builds in byte order, so this is NOT '
                      'cleared; and it copies iso14651_t1, which this step '
                      'flagged -- so this locale IS exposed', flat)
        self.assertNotIn('byte order by construction', flat)


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



@needs_clone
class WrapperMeasuredOrder(unittest.TestCase):
    """--old-order and --new-order on RHEL8 -> RHEL9: step 11 runs, and the
    summary names what each machine's own glibc says changed.

    Freezes the summary a reader acts on: the fourteen locales, and a
    warnings block that does not repeat step 11's lists with the names cut
    off from under them.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-order-')
        cls.rc, cls.out = run_wrapper(OLD, MID, '--old-order', RHEL8,
                                      '--new-order', RHEL9,
                                      out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_step_11_runs(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertEqual(self.out.count('\n== STEP 11  MEASURED ORDER  '), 1)
        self.assertIn('Sort order CHANGED in 14 locale(s):',
                      self.out.split('AUDIT SUMMARY')[0])

    def test_the_summary_names_the_fourteen_locales(self):
        summary = parse_summary(measured_block(self.out))
        self.assertEqual(set(summary['changes']), CHANGED_8_TO_9)
        self.assertEqual(summary['not_on_new'],
                         ['en_US.utf8@ampm', 'en_US@ampm'])

    def test_the_block_sits_under_removed(self):
        """Removed stays directly under Reindex, where it was put so that a
        locale the upgrade deletes is on the first screen: this block is some
        forty lines long, and above Removed it pushed that section down."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertLess(summary.index('-- Reindex:'), summary.index(REMOVED))
        self.assertLess(summary.index(REMOVED), summary.index(MEASURED))
        self.assertLess(summary.index(MEASURED),
                        summary.index('-- Needs an empirical test'))
        self.assertNotIn(MEASURED_NOT_RUN, summary)

    def test_reindex_says_where_the_measured_answer_is(self):
        """Reindex lists five names from the tags and comes first; the block
        that lists fourteen from the machines is further down. Without a
        pointer the five read as the whole answer. Between them, the five
        lines that say how to read the list."""
        body = summary_block(self.out, REINDEX)
        self.assertEqual(body[-2:], REINDEX_POINTER)
        self.assertEqual(len(body), 5 + 1 + 5 + 2)

    def test_node_to_node_NOT_RUN_does_not_deny_what_step_11_compared(self):
        """The plain NOT RUN block says nothing compared the two nodes'
        C.UTF-8; after step 11 that may be false, and it says where step 11's
        answer is instead of what it was, since C.UTF-8 can be listed there
        as not measured. Whole, line for line, like the plain one."""
        self.assertEqual(summary_block(self.out, NODE_TO_NODE_NOT_RUN), [
            'Pass --old-node and --new-node instead of every other --old-*/--new-*',
            'option, or --old-locales-dir and --new-locales-dir with their build ids.',
            "Without both, nothing above compared the two nodes' C.UTF-8 "
            'files',
            'against each other; what step 11 measured of it is under',
            "'-- Measured order'. PostgreSQL reports collversion as NULL for",
            'every C.* collation, so no mismatch can ever fire.',
            'Then run sql/c_utf8_probe.sql on both nodes.',
        ])

    def test_the_warnings_block_does_not_repeat_step_11(self):
        """Its `!!` headers there would stand over lists the block does not
        carry: "4 locale(s):" and nothing under it."""
        warnings = flat(self.out.split('-- Warnings the clean results above '
                                       'do NOT cover')[1])
        for header in ('!! Could not be measured', '!! Not on the new machine',
                       '!! Unchanged only as far as measured'):
            with self.subTest(header=header):
                self.assertNotIn(header, warnings)


@needs_clone
class WrapperMeasuredOrderRefused(unittest.TestCase):
    """What step 11 is given is checked before step 1, and a refusal ends the
    run there: no step, no summary.

    Freezes three ways a summary could read as if the order had been
    measured when it was not: one machine's file alone, two files of another
    upgrade, and a file cut short.
    """

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-order-bad-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def assertRefusedBeforeStep1(self, *args):
        rc, out = run_wrapper(OLD, MID, *args, out_dir=self.out_dir)
        self.assertEqual(rc, 2, out[-2000:])
        self.assertNotIn('== STEP 1', out)
        self.assertNotIn('AUDIT SUMMARY', out)
        return out

    def test_the_old_file_alone(self):
        out = self.assertRefusedBeforeStep1('--old-order', RHEL8)
        self.assertIn('go together', out)

    def test_the_new_file_alone(self):
        out = self.assertRefusedBeforeStep1('--new-order', RHEL9)
        self.assertIn('go together', out)

    def test_the_files_of_another_upgrade(self):
        out = self.assertRefusedBeforeStep1('--old-order', RHEL9,
                                            '--new-order', RHEL10)
        self.assertIn('locale_order.py: ', out)
        self.assertIn(COULD_NOT_COMPARE, out)

    def test_a_file_cut_short(self):
        cut = os.path.join(self.out_dir, 'cut.out')
        with open(RHEL9, encoding='utf-8') as f, \
                open(cut, 'w', encoding='utf-8') as g_:
            g_.write(f.read()[:1000000])
        out = self.assertRefusedBeforeStep1('--old-order', RHEL8,
                                            '--new-order', cut)
        self.assertIn('locale_order.py: ', out)
        self.assertIn(COULD_NOT_COMPARE, out)


@needs_clone
class WrapperMeasuredOrderUncheckedTag(unittest.TestCase):
    """The old tag given as a commit id: nothing can say which glibc it is,
    so the summary says the file was not checked against it, rather than
    passing it as a match."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-order-sha-')
        cls.rc, cls.out = run_wrapper(EXPECTED_SHA[OLD], MID,
                                      '--old-order', RHEL8,
                                      '--new-order', RHEL9,
                                      out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_summary_says_the_old_file_was_not_checked(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        summary = parse_summary(measured_block(self.out))
        self.assertEqual(summary['warnings'], ['OLD tag unchecked'])
        self.assertEqual(set(summary['changes']), CHANGED_8_TO_9)


# --- --old-node and --new-node -------------------------------------------------

# Where a node file says its sources were not found. Short, so that the reason
# wraps between words in the summary, and absent from every machine.
NO_SOURCES_DIR = '/nonexistent-pg-glibc-audit/locales'
NO_SOURCES_REASON = (f'{NO_SOURCES_DIR} does not exist; it comes with the '
                     'package glibc-locale-source.')
_NODE_FILES = {}


def node_file(name, measurement, tag, sources=True):
    """A node file as --extract writes it, with the writer itself: the real
    measurement of that machine, and a tag's locale sources standing in for
    the node's (CI has no node; tests/test_node_modes.py does the same).
    Without sources it names NO_SOURCES_DIR. Built once per module."""
    key = (name, measurement, tag, sources)
    if key not in _NODE_FILES:
        base = tempfile.mkdtemp(prefix='pg-glibc-node-file-')
        directory = (dd.materialise_tag(GLIBC_CLONE, tag, base) if sources
                     else NO_SOURCES_DIR)
        with open(measurement, encoding='ascii') as f:
            text = f.read()
        data, _ = lom.archive(text, json.loads(text), directory)
        path = os.path.join(base, name)
        with open(path, 'wb') as f:
            f.write(data)
        _NODE_FILES[key] = path
    return _NODE_FILES[key]


def tearDownModule():
    for path in _NODE_FILES.values():
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def build_of(measurement):
    with open(measurement, encoding='ascii') as f:
        return json.load(f)['glibc_build']


ELLIPSIS_SIDE_NOT_RUN = "-- Node's own locale data, ellipsis scan: NOT RUN"
ELLIPSIS_BOTH_NOT_RUN = "-- Node's own ellipsis scan: NOT RUN"
COULD_NOT_COMPARE_NODES = 'error: step 11 could not compare the measurements in'


def no_sources_text(path):
    """What the summary says of a node file without sources, whitespace
    collapsed."""
    return f'{path} holds no locale sources. {NO_SOURCES_REASON}'


OPTIONAL_SECTIONS = ('DISTRO CHECK', 'NODE TO NODE', 'NODE ELLIPSIS',
                     'MEASURED ORDER')


def numbered(out):
    """Each optional section's banner as (its step number, its name), in the
    order printed. Only a banner that carries its number counts: the docs
    name these sections by number, and a banner without one is a section the
    reader cannot find."""
    names = '|'.join(OPTIONAL_SECTIONS)
    return [(int(n), name) for n, name in
            re.findall(rf'^== STEP (\d+)  ({names})  ', out, re.M)]


def banners(out):
    """How many times each optional section's banner was printed."""
    found = [name for _, name in numbered(out)]
    return {name: found.count(name) for name in OPTIONAL_SECTIONS}


@needs_clone
class WrapperNodePatchedInsideCollate(unittest.TestCase):
    """A machine whose de_DE and sv_SE differ from its tag inside LC_COLLATE,
    as a distro patch would: the steps 6 and 7 block names it, instead of saying
    nothing as the summary did until 2026-09-27."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-patched-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-patchedtree-')
        root = dd.materialise_tag(GLIBC_CLONE, OLD, os.path.join(cls.nodes, 'a'))
        # Two, so that a block printing fewer names than the list holds shows.
        for name in ('de_DE', 'sv_SE'):
            path = os.path.join(root, name)
            with open(path, encoding='utf-8', errors='surrogateescape') as fh:
                text = fh.read()
            opening = re.search(r'(?m)^LC_COLLATE\n', text)
            assert opening, f'{name} at the tag has no LC_COLLATE block'
            with open(path, 'w', encoding='utf-8',
                      errors='surrogateescape') as fh:
                fh.write(text[:opening.end()] + 'reorder-after <U0076>\n'
                         '<U0077> <U0076>;<BASE>;<MIN>;IGNORE\n'
                         + text[opening.end():])
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-locales-dir', root, '--old-build-id', 'build-patched',
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)
        shutil.rmtree(cls.nodes, ignore_errors=True)

    def test_the_block_names_the_patched_locale(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        body = summary_block(
            self.out, f'-- Distro patches, step 6 (build-patched against {OLD})')
        self.assertEqual(body[:3], [
            '2 locale(s) differ inside LC_COLLATE -- for these the tag',
            'diff is NOT reading what this machine runs:', 'de_DE sv_SE'])
        self.assertTrue(body[3].startswith('full list: '), body)
        self.assertEqual(body[4:], [
            'Locales that copy these, and so sort differently too, are',
            'listed in step 6 above.'])


@needs_clone
class WrapperNodeFiles(unittest.TestCase):
    """--old-node and --new-node on RHEL8 -> RHEL9 stand for the six options:
    the same files given as --*-locales-dir, --*-build-id and --*-order give
    the same run, line for line. Freezes what each node file is laid out as,
    side by side."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-')
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-node', node_file('old.tar', RHEL8, OLD),
            '--new-node', node_file('new.tar', RHEL9, MID),
            out_dir=cls.out_dir)
        laid = {side: os.path.join(cls.out_dir,
                                   f'node_file.{pair_slug(OLD, MID)}.{side}')
                for side in ('old', 'new')}
        cls.classic_rc, cls.classic = run_wrapper(
            OLD, MID,
            '--old-locales-dir', os.path.join(laid['old'], 'locales'),
            '--old-build-id', build_of(RHEL8),
            '--new-locales-dir', os.path.join(laid['new'], 'locales'),
            '--new-build-id', build_of(RHEL9),
            '--old-order', os.path.join(laid['old'], 'locale_order.out'),
            '--new-order', os.path.join(laid['new'], 'locale_order.out'),
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_it_is_the_run_the_six_options_give(self):
        self.assertEqual((self.rc, self.classic_rc), (0, 0), self.out[-2000:])
        self.assertEqual(self.out.splitlines(), self.classic.splitlines())

    def test_every_section_ran(self):
        self.assertEqual(banners(self.out), {
            'DISTRO CHECK': 2, 'NODE TO NODE': 1, 'NODE ELLIPSIS': 2,
            'MEASURED ORDER': 1})
        self.assertIn(f"== STEP 8  NODE TO NODE  does {build_of(RHEL8)}'s "
                      f"collation data differ from {build_of(RHEL9)}'s?",
                      self.out)

    def test_steps_6_to_11_carry_their_numbers(self):
        """The numbers the docs use: 6 and 7 one side each, 8 both, 9 and
        10 one side each, 11 the measured order."""
        self.assertEqual(numbered(self.out), [
            (6, 'DISTRO CHECK'), (7, 'DISTRO CHECK'), (8, 'NODE TO NODE'),
            (9, 'NODE ELLIPSIS'), (10, 'NODE ELLIPSIS'),
            (11, 'MEASURED ORDER')])

    def test_the_summary_names_the_fourteen_locales(self):
        summary = parse_summary(measured_block(self.out))
        self.assertEqual(set(summary['changes']), CHANGED_8_TO_9)
        self.assertNotIn('NOT RUN', self.out.split('AUDIT SUMMARY')[1])

    def test_no_step_sends_the_reader_to_the_package_changelog(self):
        """Steps 6 to 10 used to suggest `rpm -q --changelog glibc | grep -i
        collat` on each node, and that search cannot decide whether the order
        changed. RHEL9 and RHEL10 trim the changelog when the package is
        built, and a search for a word finds only the entries that use it. An
        empty result reads as "nothing changed" over what the search cannot
        see, so the output does not send the reader to the changelog.

        This run reaches only some branches of the steps, so the tool's code
        is searched too. Both searches take "change log", rpm's --changes and
        %{CHANGELOGTEXT} as well as the word that was removed, and the code
        search also looks across the quotes and line breaks where the source
        splits a message."""
        # The absence below proves nothing about a run that stopped early.
        self.assertEqual(self.rc, 0, self.out[-2000:])
        # [\s_-]* rather than [\s_-]?, because g.warn breaks "change-log"
        # after its hyphen and flat() rejoins it as "change- log". Nothing
        # after "log", so %{CHANGELOGTEXT} and its siblings match too.
        advice = re.compile(r'(?i)\bchange[\s_-]*log|--changes\b')
        self.assertEqual(advice.findall(flat(self.out)), [])
        # The same words with the quotes, `f` prefixes and `echo` of a split
        # message between them. A message split across two calls is caught
        # by joining a .py file's strings in the order they appear.
        across = re.compile(r'(?i)\bchange'
                            r'(?:[^a-z$]|\b(?:[bfru]{1,2}|echo)\b)*?log'
                            r'|--changes\b')
        code = sorted(glob.glob(os.path.join(REPO_ROOT, 'scripts', '*.py'))
                      + glob.glob(os.path.join(REPO_ROOT, 'scripts', '*.sh'))
                      + glob.glob(os.path.join(REPO_ROOT, 'sql', '*.sql'))
                      + [os.path.join(REPO_ROOT, 'audit.sh')])
        # An empty search over the wrong directory would pass, so the files
        # that printed the advice must be among those read.
        self.assertTrue({'audit.sh', 'diff_distro_locales.py',
                         'diff_node_locales.py', 'flag_algorithmic_ranges.py'}
                        <= {os.path.basename(path) for path in code})
        hits = []
        for path in code:
            with open(path, encoding='utf-8', errors='surrogateescape') as f:
                text = f.read()
            found = across.findall(text)
            if path.endswith('.py'):
                strings = sorted(
                    (node for node in ast.walk(ast.parse(text))
                     if isinstance(node, ast.Constant)
                     and isinstance(node.value, str)),
                    key=lambda node: (node.lineno, node.col_offset))
                found += advice.findall(
                    flat(' '.join(node.value for node in strings)))
            hits += [(os.path.relpath(path, REPO_ROOT), m) for m in found]
        self.assertEqual(hits, [])


class NodeFileWithoutSources:
    """One side's file holds no locale sources. That side's steps do not run,
    and each summary block that would have carried them names the file and
    why, instead of telling the reader to pass options the files replace.

    Two classes, one per side: a block that named a FIXED side would pass a
    test of one side alone.
    """
    BARE = None   # 'old' or 'new'

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-bare-')
        cls.old = node_file('old.tar', RHEL8, OLD, sources=cls.BARE != 'old')
        cls.new = node_file('new.tar', RHEL9, MID, sources=cls.BARE != 'new')
        cls.bare = cls.old if cls.BARE == 'old' else cls.new
        cls.rc, cls.out = run_wrapper(OLD, MID, '--old-node', cls.old,
                                      '--new-node', cls.new,
                                      out_dir=cls.out_dir)
        cls.summary = cls.out.split('AUDIT SUMMARY', 1)[-1]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_run_ends_cleanly(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])

    def test_only_the_side_with_sources_was_checked(self):
        builds = {'old': build_of(RHEL8), 'new': build_of(RHEL9)}
        have = 'new' if self.BARE == 'old' else 'old'
        self.assertEqual(banners(self.out), {
            'DISTRO CHECK': 1, 'NODE TO NODE': 0, 'NODE ELLIPSIS': 1,
            'MEASURED ORDER': 1})
        self.assertIn(f"  DISTRO CHECK  do {builds[have]}'s", self.out)
        self.assertIn(f"  NODE ELLIPSIS  does {builds[have]}'s", self.out)

    def test_the_steps_left_keep_their_numbers(self):
        """A side left out takes its numbers with it: the new side alone is
        still 7 and 10, never renumbered to 6 and 9."""
        distro, ellipsis = (6, 9) if self.BARE == 'new' else (7, 10)
        self.assertEqual(numbered(self.out), [
            (distro, 'DISTRO CHECK'), (ellipsis, 'NODE ELLIPSIS'),
            (11, 'MEASURED ORDER')])

    def test_the_distro_patch_block_says_which_file_and_why(self):
        builds = {'old': (build_of(RHEL8), OLD), 'new': (build_of(RHEL9), MID)}
        bare, have = (6, 7) if self.BARE == 'old' else (7, 6)
        side = 'new' if self.BARE == 'old' else 'old'
        self.assertEqual(
            ' '.join(summary_block(self.out,
                                   f'-- Distro patches, step {bare}: NOT RUN')),
            f"{no_sources_text(self.bare)} So nothing above says whether the "
            f"{self.BARE} machine's own patches touch LC_COLLATE, which the "
            f"tags cannot show.")
        build, tag = builds[side]
        self.assertEqual(summary_block(
            self.out, f'-- Distro patches, step {have} ({build} against '
                      f'{tag})'), distro_clean(tag, have, unread=()))

    def test_removed_says_which_file_and_why(self):
        self.assertEqual(
            section_body(self.summary, REMOVED),
            f'{NOT_CHECKED} {no_sources_text(self.bare)} Between the tags: '
            f'none -- no locale file at {OLD} is gone at {MID}')

    def test_node_to_node_says_which_file_and_why(self):
        self.assertEqual(
            section_body(self.summary, NODE_TO_NODE_NOT_RUN),
            f"{no_sources_text(self.bare)} So nothing above compared the two "
            "nodes' C.UTF-8 files against each other; what step 11 measured "
            "of it is under '-- Measured order'. PostgreSQL reports "
            "collversion as NULL for every C.* collation, so no mismatch can "
            "ever fire. Then run sql/c_utf8_probe.sql on both nodes.")

    def test_the_ellipsis_scan_says_which_file_and_why(self):
        self.assertEqual(
            section_body(self.summary, ELLIPSIS_SIDE_NOT_RUN),
            f"{no_sources_text(self.bare)} So nothing above says whether the "
            f"{self.BARE} node's own C.UTF-8 is ellipsis-based. The other "
            "node's scan does not answer it, because each node built its own "
            "locales.")

    def test_no_block_asks_for_the_options_the_files_replace(self):
        self.assertNotIn('Pass --', flat(self.summary))


@needs_clone
class WrapperNodeFileOldWithoutSources(NodeFileWithoutSources,
                                       unittest.TestCase):
    BARE = 'old'


@needs_clone
class WrapperNodeFileNewWithoutSources(NodeFileWithoutSources,
                                       unittest.TestCase):
    BARE = 'new'


@needs_clone
class WrapperNodeFilesWithoutSources(unittest.TestCase):
    """Neither file holds sources: no step reads a node's files, and every
    block names both files, the old one first."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-bare2-')
        cls.old = node_file('old.tar', RHEL8, OLD, sources=False)
        cls.new = node_file('new.tar', RHEL9, MID, sources=False)
        cls.rc, cls.out = run_wrapper(OLD, MID, '--old-node', cls.old,
                                      '--new-node', cls.new,
                                      out_dir=cls.out_dir)
        cls.summary = cls.out.split('AUDIT SUMMARY', 1)[-1]
        cls.both = f'{no_sources_text(cls.old)} {no_sources_text(cls.new)}'

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_only_the_measured_order_ran(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertEqual(banners(self.out), {
            'DISTRO CHECK': 0, 'NODE TO NODE': 0, 'NODE ELLIPSIS': 0,
            'MEASURED ORDER': 1})

    def test_the_distro_patch_block_names_both_files(self):
        self.assertEqual(
            ' '.join(summary_block(
                self.out, '-- Distro patches, steps 6 and 7: NOT RUN')),
            f"{self.both} So nothing above says whether either machine's own "
            f"patches touch LC_COLLATE, which the tags cannot show.")

    def test_every_block_names_both_files(self):
        self.assertEqual(
            section_body(self.summary, REMOVED),
            f'{NOT_CHECKED} {self.both} Between the tags: none -- no locale '
            f'file at {OLD} is gone at {MID}')
        self.assertTrue(section_body(self.summary, NODE_TO_NODE_NOT_RUN)
                        .startswith(self.both + ' So nothing above compared'))
        self.assertEqual(
            section_body(self.summary, ELLIPSIS_BOTH_NOT_RUN),
            f"{self.both} Step 4 above scanned the TAG, and a tag holds at "
            "most upstream's C. The distros this audit targets ship their own "
            "C.UTF-8, so if step 4 named C at all, that verdict is evidence "
            "about upstream's file and none about either node's. Nothing "
            "above says how either node's own C.UTF-8 defines its order, and "
            "unless both nodes' copies declare codepoint_collation, a data "
            "diff, including the node-to-node one, cannot clear it.")
        self.assertNotIn('Pass --', flat(self.summary))


@needs_clone
class WrapperNodeFileRefused(unittest.TestCase):
    """A node file that cannot be trusted, or node files given the wrong way,
    stop the run before step 1: no step, no summary, and the message names
    the reason."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-bad-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.old = node_file('old.tar', RHEL8, OLD, sources=False)
        self.new = node_file('new.tar', RHEL9, MID, sources=False)

    def assertRefusedBeforeStep1(self, *args, tags=(OLD, MID)):
        rc, out = run_wrapper(*tags, *args, out_dir=self.out_dir)
        self.assertEqual(rc, 2, out[-2000:])
        self.assertNotIn('== STEP 1', out)
        self.assertNotIn('AUDIT SUMMARY', out)
        return flat(out)

    def copy_of(self, path, prefix=b'', cut=None):
        with open(path, 'rb') as f:
            data = f.read()
        dest = os.path.join(self.out_dir, 'changed.tar')
        with open(dest, 'wb') as f:
            f.write(prefix + data[:cut])
        return dest

    def test_one_file_alone(self):
        for args in (('--old-node', self.old), ('--new-node', self.new)):
            with self.subTest(args=args):
                self.assertIn('--old-node and --new-node go together',
                              self.assertRefusedBeforeStep1(*args))

    def test_with_the_options_they_stand_for(self):
        for extra in (('--old-order', RHEL8), ('--new-order', RHEL9),
                      ('--old-locales-dir', self.out_dir),
                      ('--new-build-id', build_of(RHEL9))):
            for nodes in (('--old-node', self.old, '--new-node', self.new),
                          ('--new-node', self.new)):
                with self.subTest(extra=extra, nodes=nodes):
                    self.assertIn('give them without those options',
                                  self.assertRefusedBeforeStep1(*nodes, *extra))

    def test_an_old_file_cut_short(self):
        cut = self.copy_of(self.old, cut=100000)
        out = self.assertRefusedBeforeStep1('--old-node', cut,
                                            '--new-node', self.new)
        self.assertIn(f'{cut} is not a whole file written by --extract', out)
        self.assertIn("the old node's file could not be used", out)

    def test_text_printed_before_the_new_file(self):
        banner = self.copy_of(self.new, prefix=b'Welcome to el9\n')
        out = self.assertRefusedBeforeStep1('--old-node', self.old,
                                            '--new-node', banner)
        self.assertIn('such as a login message', out)
        self.assertIn("the new node's file could not be used", out)

    def test_the_same_file_twice(self):
        out = self.assertRefusedBeforeStep1('--old-node', self.old,
                                            '--new-node', self.old,
                                            tags=(OLD, OLD))
        self.assertIn('the two files hold the same measurement', out)
        self.assertIn(f'{COULD_NOT_COMPARE_NODES} {self.old} and {self.old}',
                      out)

    def test_the_files_the_wrong_way_round(self):
        out = self.assertRefusedBeforeStep1('--old-node', self.new,
                                            '--new-node', self.old)
        self.assertIn('pass the machine you upgrade FROM first', out)
        self.assertIn(f'{COULD_NOT_COMPARE_NODES} {self.new} and {self.old}',
                      out)

    def test_a_second_run_lays_the_files_out_again(self):
        """The folders a run lays the files out in are its own: a second run
        in the same output folder removes them first, like every other file
        this run reads, instead of reading the first run's.

        The two runs are given different files on both sides, so a second run
        that read the first one's folders would print the first one's answer.
        """
        first = self.assertRefusedBeforeStep1('--old-node', self.new,
                                              '--new-node', self.old)
        self.assertIn('pass the machine you upgrade FROM first', first)
        rhel10 = node_file('rhel10.tar', RHEL10, NEW, sources=False)
        second = self.assertRefusedBeforeStep1('--old-node', self.old,
                                               '--new-node', rhel10)
        self.assertIn('and the NEW tag is glibc-2.34', second)
        self.assertNotIn('pass the machine you upgrade FROM first', second)

    def test_the_files_of_another_upgrade(self):
        rhel10 = node_file('rhel10.tar', RHEL10, NEW, sources=False)
        out = self.assertRefusedBeforeStep1('--old-node', self.new,
                                            '--new-node', rhel10)
        self.assertIn('and the OLD tag is glibc-2.28', out)
        self.assertIn(COULD_NOT_COMPARE_NODES, out)


if __name__ == '__main__':
    unittest.main()
