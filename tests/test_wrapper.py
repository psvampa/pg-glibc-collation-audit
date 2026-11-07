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

from _harness import (EXPECTED_SHA, MID, NEW, OLD, REPO_ROOT, SCRIPTS_DIR,
                      backported_c, deleted_steps_named, flat, locale_file,
                      needs_clone,
                      run_wrapper, upstream_c, summary_block)

import diff_distro_locales as dd
import glibc_locale_data as g
import locale_order as lom

# Step 1's `!!` for two sides read as tags (glibc_locale_data.tag_sides_text)
# over 2.28..2.34, as a run with neither machine's file asks for the builds;
# the summary repeats it whole among its warnings.
TAGS_FACT = ("!! Not checked for glibc-2.28 and glibc-2.34: they are upstream "
               "tags, which hold none of a distro's own locale files or "
               "patches. Steps 1 and 2 read only what the tags hold.")
TAGS_NOTICE = (TAGS_FACT + " If the machines run RHEL 8, 9 or 10, name each "
               "side as its build instead, as rpm -q glibc prints it on the "
               "machine without the architecture (glibc-2.34-283.el9_8), or as "
               "latest-el8, latest-el9 or latest-el10.")
# The same with the locale folders or the measurements, which a build
# refuses.
TAGS_NOTICE_FOLDERS = (TAGS_FACT + " Pass each machine's file with --old-node "
                       "and --new-node instead of every other --old-*/--new-* "
                       "option: steps 1 and 2 then read each machine's own "
                       "locale sources, which its file holds.")
from _harness import GLIBC_CLONE, Step2Report, swap_alpha_beta, tag_charmaps
from test_locale_order import (CHANGED_8_TO_9, RHEL8, RHEL9, RHEL10,
                               changes_in, parse_characters, parse_summary,
                               trimmed)


# The classes that run audit.sh on the recorded measurements read them cut
# to the locales they assert about, every part cut and the sort tables
# repacked (cut_to_pair): the fourteen that change from RHEL8 to RHEL9, the
# two RHEL9 removes, C and POSIX (the control, and C's charmap line), the
# three in ISO-8859-7 the hand-made greek charmap reaches, and one unchanged
# locale. The classes that assert nothing of the fourteen (refusals, node
# files without sources or with other charmaps) read them cut to FEW_NAMES,
# the same with one of the fourteen, C.utf8 (built from the C the stand-in
# sources lack, and a change step 11's charmap lines are about): describing
# a change is most of what step 11 costs.
FEW_NAMES = ('C', 'POSIX', 'en_US@ampm', 'en_US.utf8@ampm', 'el_CY', 'el_GR',
             'el_GR@euro', 'en_US.utf8', 'C.utf8')
PAIR_NAMES = tuple(sorted(CHANGED_8_TO_9 | set(FEW_NAMES)))
_CUT = {}


def cut_to_pair(path, names=PAIR_NAMES):
    """path cut to names (those it holds), written once per process."""
    key = (path, names)
    if key not in _CUT:
        with open(path, encoding='utf-8') as f:
            text = f.read()
        held = set(json.loads(text)['locales'])
        out = tempfile.mkdtemp(prefix='pg-glibc-wrapper-cut-')
        dest = os.path.join(out, os.path.basename(path))
        with open(dest, 'w', encoding='utf-8') as f:
            f.write(trimmed(text, [n for n in names if n in held],
                            repack=True))
        _CUT[key] = dest
    return _CUT[key]


def cut_few(path):
    """path cut to FEW_NAMES."""
    return cut_to_pair(path, FEW_NAMES)


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
    'usage: audit.sh <old> <new>',
    '         [--old-locales-dir DIR --old-build-id NVR]',
    '         [--new-locales-dir DIR --new-build-id NVR]',
    '         [--old-order FILE --new-order FILE]',
    '         [--old-node FILE --new-node FILE]',
    '       e.g. audit.sh glibc-2.28 glibc-2.34',
    '            audit.sh glibc-2.28-251.el8_10.40 glibc-2.34-275.el9_8',
    '            audit.sh glibc-2.34-275.el9_8 latest-el10',
    '       each side is a glibc tag, glibc-<version> (run `ldd --version` on',
    '       the node), or a build as `rpm -q glibc` names it without the',
    '       architecture, rebuilt from its source RPM, or latest-elN for a',
    '       machine not installed yet: the newest build of RHEL N on Rocky',
    "       Linux's mirror, named before step 1; with a build, only",
    '       --old-node and --new-node of the options below are read',
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
    '       for old, step 7 for new).',
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
    '       machine, with its measurement, its build, its locale sources and',
    '       its charmaps (a file without them says why), and stands for all',
    "       the options above. Steps 1 and 2 then read each machine's own",
    '       locale sources and charmaps, as the build its file names, in',
    '       place of the tag (the side given is that build or its tag),',
    '       and download nothing.',
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
    'did not change can still sort differently. Nor did anything compare',
    'the decimals of money, check which C library PostgreSQL runs on',
    'either machine, check that each machine accepts every locale and',
    "converts its text, or compare what each machine's character rules",
    '(LC_CTYPE) answer.',
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
# The steps 6 and 7 block when no machine was given, whole, like the others.
DISTRO_NOT_RUN_BODY = [
    'Pass --old-node and --new-node instead of every other --old-*/--new-*',
    'option, or --old-locales-dir and --new-locales-dir with their build ids.',
    "So nothing above says whether either machine's own patches touch",
    'sort order, which the tags cannot show.',
]


def step_text(out, n):
    """What step `n` printed, from its banner to the next step's, or to
    the summary's.

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
        if lines[j].startswith(('== STEP ', '== AUDIT SUMMARY ')):
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


# Step 1's `!!` on the code no step compares (glibc_locale_data.
# CODE_NOT_COMPARED), whitespace collapsed, up to the end of its first
# sentence: what has to reach the reader on every route.
CODE_NOT_COMPARED_HEAD = (
    "!! Not compared on any route: the code that compiles and compares the "
    "rules (localedef's and strcoll's, each build's patches to it "
    "included).")


class _CodeNotComparedAndNoDeletedStep:
    """Two properties of every run, asserted on the routes the summary can
    take: tags (Wrapper), one commit (WrapperEmptyPair), the two
    measurements (WrapperMeasuredOrder) and the two machines' files
    (WrapperNodeFiles); the runs on builds and on a tag and a build are
    test_build_sources._AuditOnBuilds.

    Step 9.3 deleted steps 4 and 5, which were what a run without the
    machines had to say about localedef's and strcoll's code: ko_KR.utf8
    moved between RHEL 8 and 9 through localedef's code, its file the same
    in both tags. So step 1 says, on every route, that no step compares that
    code, and the summary repeats it once, under its warnings. And nothing a
    run prints or writes names a deleted step or its script: a sentence that
    still points to step 4 or 5 sends the reader to an answer that is not
    there. FILES_ARE_THIS_RUNS says whether the class's output folder holds
    one run's files only."""

    FILES_ARE_THIS_RUNS = True

    def test_the_code_no_step_compares_is_said_once_in_the_summary(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        summary = self.out.split('AUDIT SUMMARY', 1)[1]
        warnings = summary.partition(
            '-- Warnings the clean results above do NOT cover')
        self.assertTrue(warnings[1], summary[-2000:])
        self.assertEqual(flat(warnings[2]).count(CODE_NOT_COMPARED_HEAD), 1)
        # Once, not also inside another block of the warnings.
        self.assertEqual(flat(warnings[2]).count('Not compared on any route'),
                         1)
        self.assertNotIn('Not compared on any route', flat(warnings[0]))
        # Step 1 printed it, once, before the summary repeated it.
        self.assertEqual(flat(self.out.split('== STEP 2', 1)[0])
                         .count(CODE_NOT_COMPARED_HEAD), 1)

    def test_nothing_printed_or_written_names_a_deleted_step(self):
        self.assertEqual(deleted_steps_named(self.out), [])
        if not self.FILES_ARE_THIS_RUNS:
            return
        files = sorted(os.listdir(self.out_dir))
        self.assertTrue(files)
        for name in files:
            path = os.path.join(self.out_dir, name)
            if not os.path.isfile(path):
                continue
            self.assertEqual(deleted_steps_named(name), [], name)
            with open(path, encoding='utf-8', errors='replace') as fh:
                self.assertEqual(deleted_steps_named(fh.read()), [], name)


@needs_clone
class Wrapper(_CodeNotComparedAndNoDeletedStep, unittest.TestCase):
    """A pair with real findings: the wrapper must not change the answer."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-')
        cls.rc, cls.out = run_wrapper(OLD, MID, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_exits_zero_on_a_pair_with_findings(self):
        """A finding is not an error. Step 2 reports locales whose rules
        differ and returns 0."""
        self.assertEqual(self.rc, 0, self.out)

    def test_answer_matches_the_published_result(self):
        """The published set for 2.28..2.34: locales, and the alias glibc gives
        one of them (backlog 13.1).

        examples/rhel8-to-rhel9-tags-only.txt prints these. A wrapper that
        changes an answer is a bug, not a feature.
        """
        listed = os.path.join(
            self.out_dir, f'step2_changed_collate.{pair_slug(OLD, MID)}.txt')
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
        """Between 2.28 and 2.34 sv_FI's own file does not change; its rules
        differ because step 2 follows its `copy "sv_SE"`, and they name
        sv_SE's two characters."""
        self.assertEqual(Step2Report(step_text(self.out, 2)).differs['sv_FI'],
                         ([0x57, 0x77], False))

    def test_step_2_list_is_written_and_feeds_the_summary(self):
        """The file the summary's Reindex reads exists, is named for the
        pair, and holds the names.

        Reverting step 2's write makes this fail with the file missing, which
        is the mutation test for that write.
        """
        path = os.path.join(
            self.out_dir, f'step2_changed_collate.{pair_slug(OLD, MID)}.txt')
        self.assertTrue(os.path.exists(path), f"{path} missing\n{self.out}")
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(sorted(n.strip() for n in fh if n.strip()),
                             ['or_IN', 'sv_FI', 'sv_FI@euro', 'sv_SE',
                              'swedish'])

    def test_summary_repeats_the_tags_warning_in_full(self):
        """A warning that scrolled past 400 lines ago was not delivered.

        And it must be the whole block: the first `!!` line alone stops at
        "...nor glibc-2.34, but", cutting off exactly where the reason starts.
        """
        summary = self.out[self.out.index('AUDIT SUMMARY'):]
        self.assertIn('Warnings the clean results above do NOT cover', summary)
        self.assertIn(TAGS_NOTICE, flat(summary))

    def test_next_step_hints_are_suppressed(self):
        """The steps' "run this next" hints name commands already run."""
        self.assertNotIn('Next: python3 filter_lc_collate_changes.py',
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
                        summary.index(MEASURED_NOT_RUN))


@needs_clone
class WrapperTakesTheKeyedListToReindex(unittest.TestCase):
    """What step 2's character set check lists (backlog 13.7, PR B) reaches
    the summary's Reindex, beside the locales whose rules differ. No pinned
    pair lists a locale there, so the check's answer is injected: a
    sitecustomize on PYTHONPATH, which every Python the run starts imports,
    makes collation_keys.listed_sources answer sv_SE. glibc-2.34 to 2.39,
    where th_TH and thai differ and sv_SE does not."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-keyed-')
        hook = os.path.join(cls.out_dir, 'hook')
        os.makedirs(hook)
        with open(os.path.join(hook, 'sitecustomize.py'), 'w',
                  encoding='utf-8') as fh:
            fh.write(f"import sys\n"
                     f"sys.path.insert(0, {SCRIPTS_DIR!r})\n"
                     f"import collation_keys as ck\n"
                     f"ck.listed_sources = lambda report: ['sv_SE']\n"
                     f"del sys.path[0]\n")
        cls.rc, cls.out = run_wrapper(MID, NEW, out_dir=cls.out_dir,
                                      env_extra={'PYTHONPATH': hook})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_step_2_writes_the_keyed_list_and_lists_it(self):
        self.assertEqual(self.rc, 0, self.out)
        path = os.path.join(
            self.out_dir, f'step2_keyed_locales.{pair_slug(MID, NEW)}.txt')
        with open(path, encoding='utf-8') as fh:
            self.assertEqual(fh.read().split(), ['sv_SE'])
        # swedish is sv_SE's alias (sv_SE.ISO-8859-1): it goes with it. Its
        # rules are sv_SE's, the same at both tags, so compare_rules alone
        # does not list it (false-negative-reviewer, 9.2).
        self.assertEqual(Step2Report(step_text(self.out, 2)).reindex,
                         ['sv_SE', 'swedish', 'th_TH', 'thai'])

    def test_the_keyed_locale_reaches_the_reindex_list_without_its_copiers(self):
        """The character set check followed every copy each listed build
        reads, at both tags, so the keyed locales are listed as they are,
        with their aliases: sv_FI copies sv_SE and sorts by its own
        charmap, which the check compared for sv_FI's own builds. The
        summary's sentence that the list holds the aliases holds here."""
        names = summary_block(self.out, REINDEX)
        self.assertIn('sv_SE', names)
        self.assertIn('swedish', names)
        self.assertNotIn('sv_FI', names)
        self.assertIn('th_TH', names)


@needs_clone
class WrapperEmptyPair(_CodeNotComparedAndNoDeletedStep, unittest.TestCase):
    """A pair with no LC_COLLATE change at all.

    glibc-2.39 against itself: no new tag to pin, so EXPECTED_SHA and
    has_tags() stay as they are. Step 2 compares nothing and says so, and
    the summary's Reindex says nothing was compared.
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

    def test_there_is_no_step_3(self):
        """Step 9.2 made step 3's copy walk part of step 2: no banner, and
        no step that refuses an empty list (argparse's complaint, exit 2,
        which a wrapper that drops child status would hide at rc 0)."""
        self.assertNotIn('== STEP 3', self.out)
        self.assertNotIn('the following arguments are required', self.out)

    def test_step_2_and_reindex_say_nothing_was_compared(self):
        """Over one commit compared with itself, an empty list is not a
        clean result: step 2 says nothing was compared, and so does the
        summary's Reindex, whose other branch is WrapperDirectionUndetermined's
        (doc-sweep, 2026-09-30, of step 3's notice then)."""
        report = Step2Report(step_text(self.out, 2))
        self.assertEqual(report.nothing, 'the two tags are one commit.')
        self.assertEqual(summary_block(self.out, REINDEX), [
            'none -- nothing was compared: the two tags are one commit or one',
            'source tree (see below)'])

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
            "own files (steps 6 to 8), the measured order (step 11),",
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
        self.assertIn('none -- nothing was compared', self.out)


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
        self.assertIn("Everything above that compares the two tags and says "
                      "'nothing changed' means 'nothing was compared'", summary)
        # Compared as text, the tag and its sha are two commits, and step 5
        # (deleted in step 9.3) printed its clean sentence over this pair
        # (false-negative-reviewer, 2026-09-30). Reindex says so instead.
        self.assertIn('none -- nothing was compared: the two tags are one '
                      'commit or one source tree', summary)


@needs_clone
class WrapperDirectionUndetermined(unittest.TestCase):
    """The fourth state of the pair: neither commit is an ancestor of the
    other, and the newest glibc tag behind each one does not order them either
    -- a master snapshot against a backport branch off the same release, say.
    The wrapper cannot be pointed at a fabricated clone, so the order
    subcommand is stood in for by a `python3` shim, and every other
    invocation goes to the real interpreter. Silence here would read as
    "the direction was checked and is fine"."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-undet-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.env = self.shim('undetermined none')

    def shim(self, answer):
        """PATH with a python3 that answers `answer` to the order check."""
        import sys
        shim_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-undetshim-')
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)
        shim = os.path.join(shim_dir, 'python3')
        with open(shim, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\n'
                     'case "$1" in\n'
                     '  *glibc_locale_data.py)\n'
                     f'    if [ "$2" = order ]; then echo {answer}; exit 0; '
                     'fi ;;\n'
                     'esac\n'
                     f'exec "{sys.executable}" "$@"\n')
        os.chmod(shim, 0o755)
        return {'PATH': shim_dir + os.pathsep + os.environ.get('PATH', '')}

    def test_the_summary_says_the_direction_was_not_established(self):
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        summary = flat(out.split('AUDIT SUMMARY')[1])
        self.assertIn('-- Direction of the pair: NOT ESTABLISHED', summary)
        self.assertIn('nothing here checked that', summary)
        # Not the other state: an undetermined pair is not one commit.
        self.assertNotIn('One commit, compared with itself', summary)
        # What a wrong way round does is said of step 2, the one step left
        # that compares the two sides (it said "step 4 scanned" too until
        # step 9.3).
        self.assertIn('every list above is the wrong tag\'s: step 2 reports '
                      'a locale deleted in the upgrade as a harmless '
                      'addition.', summary)
        self.assertEqual(deleted_steps_named(out), [])

    def test_reindex_keeps_its_sentence_when_the_pair_is_two_commits(self):
        """The other branch of the summary's empty Reindex. With the order
        check stood in for, the wrapper takes this pair for two commits and
        step 2 lists nothing: the line must not say that nothing was
        compared, which is the one-commit branch."""
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary_block(out, REINDEX), [
            'none -- no locale whose rules step 2 compared differs, and step 2',
            'lists none it could not compare'])

    def test_an_unrecognised_state_stops_the_run(self):
        """The branch that keeps the four states honest: a word the wrapper
        does not know means the check did not answer, and "the pair is in
        order" is the assumption that runs the audit against the wrong tag.
        Each of the two words, apart."""
        for answer, said in (('perhaps none', "answered 'perhaps'"),
                             ('forward perhaps', "answered 'forward perhaps', "
                                                 "whose second word"),
                             ('forward', "answered 'forward', whose second "
                                         "word")):
            with self.subTest(answer=answer):
                rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                                      env_extra=self.shim(repr(answer)))
                self.assertNotEqual(rc, 0)
                self.assertIn(said, out)
                self.assertNotIn('AUDIT SUMMARY', out)

    def test_one_tree_git_cannot_order_compares_nothing(self):
        """Two commits of one source tree that neither descends from the
        other, as a master commit and the same patch on a wip branch are in
        the clone (158e939a and 42f22e7c). Both states are said:
        the direction was not established, and nothing was compared -- the
        summary's Reindex must not call the empty list a real result
        (false-negative-reviewer, round 1 of 7.1, of step 3's notice then).
        """
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.shim("'undetermined tree'"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary_block(out, REINDEX), [
            'none -- nothing was compared: the two tags are one commit or one',
            'source tree (see below)'])
        summary = flat(out.split('AUDIT SUMMARY')[1])
        self.assertIn('-- Direction of the pair: NOT ESTABLISHED', summary)
        self.assertIn('-- One source tree, compared with itself', summary)
        self.assertIn('are two commits of one source tree. Everything above '
                      "that compares the two tags and says 'nothing changed' "
                      "means 'nothing was compared'", summary)
        self.assertNotIn('One commit, compared with itself', summary)


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
        file with one node alone."""
        rc, out = run_wrapper(OLD, out_dir=self.out_dir)
        self.assertEqual(rc, 2)
        script = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'audit.sh')
        self.assertEqual(out.replace(script, 'audit.sh').rstrip('\n')
                         .split('\n'), USAGE)

    def test_a_reversed_pair_is_refused_before_any_summary(self):
        """"Nothing stopped a reversed pair." Reversed, every step ran to
        the end, and step 2 reports a locale DELETED in the real upgrade as an
        addition it did not analyse.
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

    def test_two_machines_the_other_way_round_are_refused_first(self):
        """Backlog 16.61, measured on 3575cc3: the fixtures' rhel9 directory
        as the old machine and rhel8's as the new, the tags in order, ran to
        exit 0, and the summary's Removed block named ckb_IQ and mnw_MM, which
        the upgrade adds, in place of en_US@ampm, which it removes. Refused
        before step 1, so no step runs on the pair as given; empty
        directories are enough, since nothing reads them first."""
        old_dir = os.path.join(self.out_dir, 'old-machine')
        new_dir = os.path.join(self.out_dir, 'new-machine')
        os.makedirs(old_dir)
        os.makedirs(new_dir)
        rc, out = run_wrapper(
            OLD, MID, '--old-locales-dir', old_dir,
            '--old-build-id', 'glibc-2.34-275.el9_8.x86_64',
            '--new-locales-dir', new_dir,
            '--new-build-id', 'glibc-2.28-251.el8_10.40.x86_64',
            out_dir=self.out_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn('the other way round', flat(out))
        self.assertNotIn('STEP 1', out)

    def test_step_2_rewrites_the_list_so_a_seed_cannot_survive(self):
        """The protection on the files the summary reads from step 2.

        Step 2 writes both lists unconditionally for the pair being audited,
        so nothing a previous run or another process left there reaches the
        summary. Revert either write and this fails: the seeded line
        survives into the file and into the run. (Until step 9.2 these
        names became step 3's argv, and audit.sh also refused anything
        that was not a locale file name; it now only prints them.)
        """
        import glibc_locale_data as g
        os.makedirs(self.out_dir, exist_ok=True)
        # Both lists step 2 writes: Reindex and the locales the character
        # set check lists (backlog 13.7, PR B).
        paths = [os.path.join(self.out_dir,
                              f'step2_{kind}.{g.pair_slug(NEW, NEW)}.txt')
                 for kind in ('changed_collate', 'keyed_locales')]
        for path in paths:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('--repo /nonexistent/elsewhere\n')
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir)
        self.assertEqual(rc, 0, out)
        for path in paths:
            with self.subTest(os.path.basename(path)):
                with open(path, encoding='utf-8') as fh:
                    self.assertNotIn('nonexistent', fh.read())
        self.assertNotIn('/nonexistent/elsewhere', out)

    def test_a_stale_step_3_list_is_not_summarised(self):
        """The bug this project already removed once, in a new place.

        step3_affected_locales.txt is named for no particular pair, so a
        leftover from a different audit sits where the summary reads. Seed it,
        then audit a pair that finds nothing: the stale locale must not appear
        as this pair's answer.

        Since step 9.2 no step writes it and the summary does not read it;
        audit.sh still removes it up front, so a list an earlier version
        left there is gone after the run.
        """
        os.makedirs(self.out_dir, exist_ok=True)
        stale = os.path.join(self.out_dir, 'step3_affected_locales.txt')
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('no_SUCH_locale\n')
        # And step 11's list of characters, which a run given no measurements
        # never writes: only audit.sh's removal up front clears it, since
        # locale_order.py, which removes it too, does not run.
        listed = os.path.join(self.out_dir, 'measured_order_characters.'
                              + pair_slug(NEW, NEW) + '.tsv')
        with open(listed, 'w', encoding='utf-8') as fh:
            fh.write('left by an earlier run\n')
        # And its SQL files, one per database encoding.
        sql = [os.path.join(self.out_dir, 'measured_order_confirm.'
                            + pair_slug(NEW, NEW) + f'.{enc}.sql')
               for enc in ('UTF8', 'LATIN1')]
        for path in sql:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('left by an earlier run\n')
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('no_SUCH_locale', out)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(listed))
        self.assertEqual([x for x in sql if os.path.exists(x)], [])


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
        # C and zz_TWO are at no tag and differ inside LC_COLLATE:
        # two names for the summary to relay (backlog 6.3). zz_TWO only
        # copies, so steps 6 and 7 compare it and name only C.
        for root, c, two in ((cls.old_root, backported_c(), 'iso14651_t1'),
                             (cls.new_root, upstream_c(), 'en_US')):
            with open(os.path.join(root, 'C'), 'w', encoding='utf-8') as fh:
                fh.write(c)
            with open(os.path.join(root, 'zz_TWO'), 'w',
                      encoding='utf-8') as fh:
                fh.write(locale_file(f'copy "{two}"'))
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-locales-dir', cls.old_root, '--old-build-id', 'build-old',
            '--new-locales-dir', cls.new_root, '--new-build-id', 'build-new.el9',
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
        for n, build, tag in ((6, 'build-old', OLD), (7, 'build-new.el9', MID)):
            with self.subTest(step=n):
                self.assertEqual(summary_block(
                    self.out, f'-- Distro patches, step {n} ({build} '
                              f'against {tag})'), distro_clean(tag, n))

    def test_the_summary_relays_every_name_not_at_both_tags(self):
        """Every name step 8 found inside LC_COLLATE and not at both tags,
        not only C's (backlog 6.3), and with steps 1 and 2 on the tags, what
        that leaves out of Reindex and how to measure it."""
        block = summary_block(
            self.out.replace(self.out_dir, '<out>'),
            '-- Node-to-node locale data (build-old -> build-new.el9)')
        starts = [i for i, ln in enumerate(block)
                  if ln.endswith('that differ, not at both tags:')]
        self.assertEqual(len(starts), 1, block)
        start = starts[0]
        self.assertEqual(block[start:start + 7], [
            'Of the 4 that differ, not at both tags:',
            'C.UTF-8, zz_TWO',
            'Steps 1 and 2 read the tags and compare none of these files, so',
            'Reindex above does not hold them: their order can change. Step 11',
            'measures their order: pass --old-node and --new-node. For C.UTF-8,',
            'sql/c_utf8_probe.sql measures it inside PostgreSQL, which reports',
            'collversion as NULL for every C.* collation.'])
        # Named by the count it qualifies, the one that opens the block.
        self.assertEqual(block[0], '4 locale(s) differ inside LC_COLLATE '
                                   'between the two')

    def test_the_folders_get_the_tags_warning_that_asks_for_the_files(self):
        """With the locale folders a build is refused, so step 1's `!!` asks
        for the machines' files, and the summary repeats it whole."""
        summary = self.out[self.out.index('AUDIT SUMMARY'):]
        self.assertIn(TAGS_NOTICE_FOLDERS, flat(summary))
        self.assertNotIn('name each side as its build', flat(self.out))

    def test_the_blast_radius_of_the_differing_files_is_in_the_summary(self):
        """"Node-to-node did not close over the copy graph." The summary now
        carries the count of locales inheriting a differing file's LC_COLLATE,
        read from the list step 8 writes. For these two trees or_IN and sv_SE
        differ and sv_FI copies sv_SE -- step 3's answer, from the node side.
        """
        flat = ' '.join(self.out.split('AUDIT SUMMARY')[1].split())
        m = re.search(r"plus (\d+) locale\(s\) that inherit one of those "
                      r"files' LC_COLLATE via copy on build-new.el9", flat)
        self.assertIsNotNone(m, flat)
        path = os.path.join(self.out_dir,
                            'node_collate_inherited.build-old..build-new.el9.txt')
        with open(path, encoding='utf-8') as fh:
            names = [ln.strip() for ln in fh if ln.strip()
                     and not ln.startswith('#')]
        self.assertEqual(int(m.group(1)), len(names))
        self.assertIn('sv_FI', names)

    def test_the_count_excludes_the_provenance_header(self):
        """count_lines would have counted the leading `#` line, reporting one
        finding where there are none."""
        path = os.path.join(self.out_dir,
                            'node_collate_diffs.build-old..build-new.el9.txt')
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
    """Both at once: two files only the old copy has (C among them, at no
    tag), and a file of the old tag neither copy has. The removal comes
    first under its own count and the
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
        for root in (cls.old_root, cls.new_root):
            os.remove(os.path.join(root, 'zu_ZA'))
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
            'C',
            'zz_GONE',
            '(2 name(s) on build-old and not on build-new;',
            'an index on one does not just sort differently; the collation '
            'is gone.',
            f'Full list: {path})',
            'UNDETERMINED -- step 8 cannot say whether the upgrade removes '
            'these:',
            'zu_ZA: in glibc-2.28, and in neither copy',
        ])


@needs_clone
class WrapperNodeCopiesDisagreeWithTheTags(unittest.TestCase):
    """Both copies over 2.34..2.39, arranged so the tags and the nodes give
    different answers. The new copy keeps aa_ER@saaho, which the tags rename
    away, so the nodes remove nothing the tags do. And neither copy has
    zu_ZA, a file of the old tag: the copy may have lost it, so the section
    is UNDETERMINED and may not print "none"."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-disagree-')
        cls.nodes = tempfile.mkdtemp(prefix='pg-glibc-wrapper-disagreetrees-')
        cls.old_root = dd.materialise_tag(GLIBC_CLONE, MID,
                                          os.path.join(cls.nodes, 'a'))
        cls.new_root = dd.materialise_tag(GLIBC_CLONE, NEW,
                                          os.path.join(cls.nodes, 'b'))
        for root in (cls.old_root, cls.new_root):
            os.remove(os.path.join(root, 'zu_ZA'))
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
            'zu_ZA: in glibc-2.34, and in neither copy',
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
    """Only --old-locales-dir: step 6 runs, 7 and 8 do not, and the
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

    def test_step_6_runs_and_7_and_8_do_not(self):
        self.assertEqual(self.rc, 0, self.out)
        self.assertIn("DISTRO CHECK  do build-old's patches", self.out)
        self.assertNotIn('NODE TO NODE', self.out)
        self.assertNotIn('NODE ELLIPSIS', self.out)
        for n in (7, 8):
            self.assertFalse(os.path.exists(os.path.join(
                self.out_dir, f'step{n}.{pair_slug(OLD, MID)}.log')), n)

    def test_the_summary_says_node_to_node_was_NOT_RUN(self):
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertIn('-- Node-to-node locale data: NOT RUN', summary)
        self.assertNotIn('ellipsis scan', flat(summary))

    def test_the_NOT_RUN_block_does_not_deny_the_scan_below_it(self):
        """Backlog 1.18. With one directory the block said
        "nothing above says anything about C.UTF-8" over a summary whose
        old node's scan covers its C a few lines further down."""
        self.assertEqual(summary_block(self.out, NODE_TO_NODE_NOT_RUN),
                         NODE_TO_NODE_NOT_RUN_BODY)

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
        # Of the files after the 300th, those no remaining file copies or
        # includes: their loss changes no other locale's run, so the
        # reader's kept answers for the tag still serve. Losing the
        # translit_* files that nearly every locale includes made the class
        # read the whole tree again on one processor, about 104 s against
        # 7 s after a cold fill (review of the whole of block 15).
        lost = set(sorted(os.listdir(root))[300:])
        named = {}
        for name in os.listdir(root):
            with open(os.path.join(root, name), 'rb') as fh:
                named[name] = set(re.findall(rb'(?:copy|include)\s+"([^"]*)"',
                                             fh.read()))
        while True:
            used = set().union(*(named[n] for n in named if n not in lost))
            still = {n for n in lost if n.encode() not in used}
            if still == lost:
                break
            lost = still
        cls.lost = sorted(lost)
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

    def test_the_step_that_reads_the_copy_says_so(self):
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
        # The fixture has to hold such a pair for that to be tested.
        self.assertIn(('tt_RU', 'tt_RU@iqtelif'),
                      [(a, b) for a in self.lost for b in self.lost
                       if a != b and b.startswith(a)])
        names = re.search(r'are missing from \S+: (.*?)\. ',
                          flat(warnings)).group(1).split(', ')
        self.assertEqual(names, self.lost)

    def test_the_removed_section_on_a_copy_that_lost_files(self):
        """The new copy alone: what it lacks may be what
        the new machine does not ship, so the section says it cannot decide
        those rather than ending on the tags' "none" -- measured printing
        "none" above step 7's `!!`. The old copy
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
    SIDE, TAG, STEPS = 'old', OLD, (6,)


@needs_clone
class WrapperNewSideLostFiles(WrapperSideLostFiles, unittest.TestCase):
    SIDE, TAG, STEPS = 'new', MID, (7,)


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
        reason to doubt it; the default weight is the other (backlog 1.6).
        The whole block, not a phrase of it: a sentence added beside the
        caveat left an assertIn green."""
        block = flat(' '.join(summary_block(
            self.out, '-- Node-to-node locale data (build-x -> build-y)')))
        self.assertEqual(block,
                         "no locale differs inside LC_COLLATE between the two "
                         "nodes' own sources. Data only -- localedef computes "
                         "the weights an ellipsis range expands to, and a "
                         "rule in its code, not these files, picks the "
                         "default weight of every character a locale does "
                         "not list.")

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
class WrapperMeasuredOrder(_CodeNotComparedAndNoDeletedStep,
                           unittest.TestCase):
    """--old-order and --new-order on RHEL8 -> RHEL9: step 11 runs, and the
    summary names what each machine's own glibc says changed.

    Freezes the summary a reader acts on: the fourteen locales, and a
    warnings block that does not repeat step 11's lists with the names cut
    off from under them.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-order-')
        cls.rc, cls.out = run_wrapper(OLD, MID,
                                      '--old-order', cut_to_pair(RHEL8),
                                      '--new-order', cut_to_pair(RHEL9),
                                      out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_tags_warning_asks_for_the_files_not_a_build(self):
        """A build is refused beside --old-order and --new-order, so step 1's
        `!!` for the two tags asks for the machines' files
        (false-negative-reviewer, 7.7 round 1)."""
        summary = self.out[self.out.index('AUDIT SUMMARY'):]
        self.assertIn(TAGS_NOTICE_FOLDERS, flat(summary))
        self.assertNotIn('name each side as its build', flat(self.out))

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

    def test_the_charmaps_are_said_not_checked(self):
        """The --*-order options carry no charmaps: the summary says once
        that neither machine's were checked, and why."""
        summary = parse_summary(measured_block(self.out))
        self.assertEqual(summary['charmaps_unchecked'],
                         [('either machine', lom.ALONE_BOTH,
                           'unless the same table')])

    def test_the_summary_names_this_runs_characters_file(self):
        """Every character, other string and pair step 11 found, in a file
        named for the pair in this run's output folder, holding all fourteen
        locales."""
        path = os.path.join(self.out_dir, 'measured_order_characters.'
                            + pair_slug(OLD, MID) + '.tsv')
        self.assertEqual(parse_summary(measured_block(self.out))['characters_file'],
                         path)
        with open(path, encoding='utf-8') as f:
            names = set().union(*map(set, changes_in(parse_characters(f.read()))))
        self.assertEqual(names, CHANGED_8_TO_9)

    def test_step_11_names_this_runs_sql_files(self):
        """One SQL file per database encoding, named for the pair in this
        run's output folder, in step 11's block with the command that reads
        two servers' outputs; nothing of them in the summary."""
        step11 = self.out.split('\n== STEP 11  MEASURED ORDER  ')[1].split(
            'AUDIT SUMMARY')[0]
        named = re.findall(r'^  (\w+) database, [\d,]+ test\(s\):\n    (\S+)$',
                           step11, re.M)
        prefix = os.path.join(self.out_dir, 'measured_order_confirm.'
                              + pair_slug(OLD, MID))
        self.assertEqual(named, [(enc, f'{prefix}.{enc}.sql')
                                 for enc in ('LATIN1', 'LATIN9', 'UTF8')])
        for enc, path in named:
            self.assertTrue(os.path.isfile(path), path)
        self.assertIn('  python3 scripts/confirm_sql.py read FILE old.out '
                      'new.out', step11.split('\n'))
        self.assertNotIn('measured_order_confirm.',
                         self.out[self.out.index('AUDIT SUMMARY'):])

    def test_the_block_sits_under_removed(self):
        """Removed stays directly under Reindex, where it was put so that a
        locale the upgrade deletes is on the first screen: this block is long,
        and above Removed it pushed that section down."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertLess(summary.index('-- Reindex:'), summary.index(REMOVED))
        self.assertLess(summary.index(REMOVED), summary.index(MEASURED))
        self.assertLess(summary.index(MEASURED),
                        summary.index('-- Distro patches, steps 6 and 7'))
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
        out = self.assertRefusedBeforeStep1('--old-order', cut_few(RHEL8))
        self.assertIn('go together', out)

    def test_the_new_file_alone(self):
        out = self.assertRefusedBeforeStep1('--new-order', cut_few(RHEL9))
        self.assertIn('go together', out)

    def test_the_files_of_another_upgrade(self):
        out = self.assertRefusedBeforeStep1('--old-order', cut_few(RHEL9),
                                            '--new-order', cut_few(RHEL10))
        self.assertIn('locale_order.py: ', out)
        self.assertIn(COULD_NOT_COMPARE, out)

    def test_a_file_cut_short(self):
        """And a characters file an earlier run left is gone: a refused run
        leaves nothing that reads as its list."""
        cut = os.path.join(self.out_dir, 'cut.out')
        with open(cut_few(RHEL9), encoding='utf-8') as f, \
                open(cut, 'w', encoding='utf-8') as g_:
            text = f.read()
            g_.write(text[:len(text) // 3])
        stale = os.path.join(self.out_dir, 'measured_order_characters.'
                             + pair_slug(OLD, MID) + '.tsv')
        sql = os.path.join(self.out_dir, 'measured_order_confirm.'
                           + pair_slug(OLD, MID) + '.UTF8.sql')
        for path in (stale, sql):
            with open(path, 'w', encoding='utf-8') as f:
                f.write('left by an earlier run\n')
        out = self.assertRefusedBeforeStep1('--old-order', cut_few(RHEL8),
                                            '--new-order', cut)
        self.assertIn('locale_order.py: ', out)
        self.assertIn(COULD_NOT_COMPARE, out)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(sql))


@needs_clone
class WrapperMeasuredOrderUncheckedTag(unittest.TestCase):
    """The old tag given as a commit id: nothing can say which glibc it is,
    so the summary says the file was not checked against it, rather than
    passing it as a match."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-order-sha-')
        cls.rc, cls.out = run_wrapper(EXPECTED_SHA[OLD], MID,
                                      '--old-order', cut_to_pair(RHEL8),
                                      '--new-order', cut_to_pair(RHEL9),
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

# What a node file's character set check adds to a run (13.7 PR D), and the
# notice a run without it prints instead.
CHARMAPS_NOTICE = "!! A machine's own localedata/charmaps/ is NOT compared"
KEYED_HEAD = '     Character sets and transliterations, '
# Step 11's line when neither machine's charmaps are given (charmap_check).
STEP_11_NOT_CHECKED = ('!! NOT checked whether the character sets of either '
                       'machine')
BEFORE_CHARMAPS = ("The file was written by an --extract that did not pack "
                   "the machine's charmaps (/usr/share/i18n/charmaps); extract "
                   "it again with this checkout's scripts/locale_order.py.")


def _cut_blocks(lines, starts, indent):
    """lines without the `!!` blocks whose first line is one of `starts`,
    and how many came out. A block is its first line and every line after
    it indented deeper than `indent`, as audit.sh's warnings block reads
    it."""
    out, cut, i = [], 0, 0
    while i < len(lines):
        if lines[i] in starts:
            cut += 1
            i += 1
            while i < len(lines) and lines[i].startswith(indent + '   '):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out, cut


def without_the_tags_notice(out):
    """(out without step 1's `!!` for the sides read as tags, before step 1's
    findings and among the summary's warnings, ((how many of each), what it
    asked for: 'build', 'node', or '' for nothing))."""
    lines = out.split('\n')
    starts = {x for x in lines if x.startswith('!! Not checked for glibc-')}
    blocks = []
    for i, line in enumerate(lines):
        if line.lstrip(' ').startswith('!! Not checked for glibc-'):
            pad = line[:len(line) - len(line.lstrip(' '))] + '   '
            j = i + 1
            while j < len(lines) and lines[j].startswith(pad):
                j += 1
            blocks.append(flat('\n'.join(lines[i:j])))
    ask = {('build' if 'name each side' in b or 'name that side' in b else
            'node' if '--old-node' in b else '') for b in blocks}
    if len(ask) != 1:
        raise AssertionError(f'the tags warning asks for {ask}')
    lines, top = _cut_blocks(lines, starts, '')
    lines, warned = _cut_blocks(lines, {'     ' + x for x in starts}, '     ')
    return '\n'.join(lines), ((top, warned), ask.pop())


# Step 11's line when both machines' charmaps were checked and nothing found.
STEP_11_CHECKED = "Character sets: every byte sequence that each machine's"


NO_SOURCES_REASON = (f'{NO_SOURCES_DIR} does not exist; it comes with the '
                     'package glibc-locale-source.')
_NODE_FILES = {}


def node_file(name, measurement, tag, sources=True, charmaps='tag'):
    """A node file as --extract writes it, with the writer itself: the real
    measurement of that machine, and a tag's locale sources and charmaps
    standing in for the node's (CI has no node; tests/test_node_modes.py
    does the same). Without sources it names NO_SOURCES_DIR for both.
    `charmaps`: 'tag', the tag's; 'greek', the tag's with alpha and beta
    swapped in ISO-8859-7, a distro's patch made by hand; 'link', the tag's
    and a link, which steps 6 to 8 refuse to read; 'before', none and no
    entry for them, as a file written before --extract packed them.
    Built once per module."""
    key = (name, measurement, tag, sources, charmaps)
    if key not in _NODE_FILES:
        base = tempfile.mkdtemp(prefix='pg-glibc-node-file-')
        directory = (dd.materialise_tag(GLIBC_CLONE, tag, base) if sources
                     else NO_SOURCES_DIR)
        kind = charmaps
        if not sources:
            charmaps = NO_SOURCES_DIR
        elif kind == 'before':
            charmaps = None
        else:
            charmaps = tag_charmaps(
                tag, os.path.join(base, 'charmaps'),
                {'ISO-8859-7': swap_alpha_beta} if kind == 'greek' else None)
            if kind == 'link':
                os.symlink('UTF-8.gz', os.path.join(charmaps, 'LINKED.gz'))
        with open(measurement, encoding='ascii') as f:
            text = f.read()
        data, _ = lom.archive(text, json.loads(text), directory, charmaps)
        path = os.path.join(base, name)
        with open(path, 'wb') as f:
            f.write(data)
        _NODE_FILES[key] = path
    return _NODE_FILES[key]


def tearDownModule():
    for path in list(_NODE_FILES.values()) + list(_CUT.values()):
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    # Called by test_build_sources too, in a process that may run this
    # module's classes afterwards: a cached path to a removed file would be
    # handed out, and a refusal of a missing file read as the one a test
    # asserts. Emptied, they are made again when asked.
    _NODE_FILES.clear()
    _CUT.clear()


def build_of(measurement):
    with open(measurement, encoding='ascii') as f:
        return json.load(f)['glibc_build']


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
class WrapperNodeFiles(_CodeNotComparedAndNoDeletedStep, unittest.TestCase):
    """--old-node and --new-node on RHEL8 -> RHEL9, each file holding a
    tag's locale sources and charmaps in place of its machine's: steps 1 and
    2 read each machine's own files as the build it runs, and every step
    that takes a node's files runs. Until step 9.4a the same files given as
    the six options --*-locales-dir, --*-build-id and --*-order gave the
    same run, line for line: no longer, since those options read the tags
    in steps 1 and 2, and they go with steps 6 to 8 (9.4b)."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-')
        cls.rc, cls.out = run_wrapper(
            OLD, MID,
            '--old-node', node_file('old.tar', cut_to_pair(RHEL8), OLD),
            '--new-node', node_file('new.tar', cut_to_pair(RHEL9), MID),
            out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_step_8_s_line_reads_step_8_s_list(self):
        """The summary's character set line under step 8 is step 8's own
        list, by its path and its names (false-negative-reviewer, round 2 of
        PR D). Its one name is C: the stand-in sources lack the C both
        measurements build, so it is named as not compared. Step 6's list
        holds en_US@ampm too, and step 7's has another path."""
        path = os.path.join(self.out_dir, f'node_keyed_locales.'
                            f'{pair_slug(build_of(RHEL8), build_of(RHEL9))}'
                            f'.txt')
        body = summary_block(self.out, f'-- Node-to-node locale data '
                                       f'({build_of(RHEL8)} -> '
                                       f'{build_of(RHEL9)})')
        at = [i for i, x in enumerate(body)
              if x.startswith('Character sets and transliterations')]
        self.assertEqual(len(at), 1, body)
        self.assertEqual(' '.join(body[at[0]:]), (
            f'Character sets and transliterations, between the two machines: '
            f'1 more locale(s) whose LC_COLLATE names a character whose bytes '
            f'differ, whose collating elements are built differently, or that '
            f'could not be compared: C full list: {path}'))

    def test_every_section_ran(self):
        self.assertEqual(banners(self.out), {
            'DISTRO CHECK': 2, 'NODE TO NODE': 1, 'NODE ELLIPSIS': 0,
            'MEASURED ORDER': 1})
        self.assertIn(f"== STEP 8  NODE TO NODE  does {build_of(RHEL8)}'s "
                      f"collation data differ from {build_of(RHEL9)}'s?",
                      self.out)

    def test_steps_6_to_11_carry_their_numbers(self):
        """The numbers the docs use: 6 and 7 one side each, 8 both, 11
        the measured order. 9 and 10 were deleted in step 9.3 and their
        numbers are not reused."""
        self.assertEqual(numbered(self.out), [
            (6, 'DISTRO CHECK'), (7, 'DISTRO CHECK'), (8, 'NODE TO NODE'),
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
                         'diff_node_locales.py'}
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


@needs_clone
class WrapperMachinesReadTheirOwnFiles(unittest.TestCase):
    """Steps 1 and 2 read each machine's own locale sources and charmaps
    from its file (step 9.4a): what a machine's file holds that a build's
    sources do not. Each file holds a tag's sources and charmaps in place
    of its machine's, as node_file's do.

    link:          the new machine's de_IT is a link to de_DE and a link
                   zz_LINK sits beside it: neither is read (its file is
                   not in the commit), neither is added or removed, both
                   are named as not compared and listed. The same file
                   holds no charmaps: the rules are compared and the
                   character sets are NOT RUN, with the reason, never
                   compared against the tag's charmaps.
    same_sources:  two builds of glibc 2.34 whose files hold the same
                   sources: ordered by rpm, forward, and said to hold the
                   same sources, never "nothing was compared".
    one_build:     two machines of one build: one commit, said as such,
                   and step 11's `!!` that this compares two machines."""

    @classmethod
    def machine_file(cls, name, text, tag, links=(), charmaps=True):
        base = tempfile.mkdtemp(prefix='pg-glibc-machine-file-')
        cls.made.append(base)
        directory = dd.materialise_tag(GLIBC_CLONE, tag, base)
        for link, target in links:
            path = os.path.join(directory, link)
            if os.path.exists(path):
                os.remove(path)
            os.symlink(target, path)
        cm = (tag_charmaps(tag, os.path.join(base, 'charmaps'))
              if charmaps else None)
        data, _ = lom.archive(text, json.loads(text), directory, cm)
        path = os.path.join(base, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    @classmethod
    def measurement(cls, path, build=None):
        """The text of the measurement at `path`, with its glibc build
        replaced by `build`: a second machine of the same release (an
        injected value; no recorded measurement is of two builds of one
        release)."""
        with open(path, encoding='utf-8') as f:
            text = f.read()
        if build:
            # Resealed, as test_locale_order.trimmed reseals a cut one.
            data = json.loads(text)
            data['glibc_build'] = build
            data['content'] = lom.content_digest(data)
            text = json.dumps(data, sort_keys=True) + '\n'
        return text

    @classmethod
    def setUpClass(cls):
        # Three runs, each measurement cut to FEW_NAMES but one (two
        # machines of one build need two measurements): step 11 is most of
        # what a run costs. The link and the missing charmaps share a run.
        cls.made, cls.runs = [], {}
        few8 = cls.measurement(cut_few(RHEL8))
        few9 = cls.measurement(cut_few(RHEL9))
        b9 = json.loads(few9)['glibc_build']
        later9 = b9.replace('-275.', '-283.')
        assert later9 != b9
        files = {
            'link': (cls.machine_file('old.tar', few8, OLD),
                     cls.machine_file('new.tar', few9, MID,
                                      links=(('de_IT', 'de_DE'),
                                             ('zz_LINK', 'de_DE')),
                                      charmaps=False)),
            'same_sources': (cls.machine_file('old.tar', few9, MID),
                             cls.machine_file('new.tar', cls.measurement(
                                 cut_few(RHEL9), later9), MID)),
            'one_build': (cls.machine_file('old.tar', few9, MID),
                          cls.machine_file('new.tar', cls.measurement(
                              cut_to_pair(RHEL9)), MID)),
        }
        tags = {'link': (OLD, MID), 'same_sources': (MID, MID),
                'one_build': (MID, MID)}
        for label, (old, new) in files.items():
            out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-machines-')
            cls.made.append(out_dir)
            cls.runs[label] = (out_dir,) + run_wrapper(
                *tags[label], '--old-node', old, '--new-node', new,
                out_dir=out_dir)

    @classmethod
    def tearDownClass(cls):
        for d in cls.made:
            shutil.rmtree(d, ignore_errors=True)

    def out(self, label):
        _, rc, out = self.runs[label]
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn('AUDIT SUMMARY', out)
        return out

    def step(self, out, n):
        """Step n's output: from the line after its banner to the next
        banner, which must be there."""
        after = out.split(f'== STEP {n} ', 1)[1].split('\n', 2)[2]
        body, end, _ = after.partition('\n====')
        self.assertTrue(end, n)
        return body

    def test_a_link_is_named_not_compared_never_added_or_removed(self):
        out = self.out('link')
        step2 = flat(self.step(out, 2))
        self.assertIn("Not compared, not read among a machine's locale "
                      "sources (2): de_IT (symlink on the new machine), "
                      "zz_LINK (symlink on the new machine)", step2)
        listed = re.search(r'Listed for Reindex \(\d+\): ([^.]*?)(?: Not |$)',
                           step2)
        self.assertTrue(listed, step2)
        names = listed.group(1).replace(',', ' ').split()
        for name in ('de_IT', 'zz_LINK'):
            with self.subTest(name=name):
                self.assertIn(name, names)
        # Step 2's own list of what is gone (with machine files the summary's
        # Removed is step 8's until step 9.4b, which names de_IT as
        # undetermined): neither link is in it, nor among the files added.
        out_dir = self.runs['link'][0]
        lists = glob.glob(os.path.join(out_dir, 'step2_removed_locales.*.txt'))
        self.assertEqual(len(lists), 1, lists)
        with open(lists[0], encoding='utf-8') as f:
            gone = f.read()
        self.assertNotIn('de_IT', gone)
        self.assertNotIn('zz_LINK', gone)
        unread_line = 'Not compared, not read among'
        self.assertEqual(step2.count(unread_line), 1, step2)
        self.assertNotIn('de_IT', step2.split('Not compared, no file at')[-1]
                         .split(unread_line)[0])
        self.assertNotIn('zz_LINK', step2.split('Listed for Reindex')[0]
                         .split(unread_line)[0])
        step1 = flat(self.step(out, 1))
        self.assertIn("2 entries of the machine's files were not read, so a "
                      "locale that is, or copies, one of them is not "
                      "compared: de_IT (symlink), zz_LINK (symlink).", step1)

    def test_two_builds_of_one_release_with_the_same_sources(self):
        out = self.out('same_sources')
        before = flat(out.split('== STEP 1', 1)[0])
        self.assertNotIn('DIRECTION', flat(out))
        self.assertIn('Rules: not compared, the two machines\' files hold '
                      'the same locale sources and charmaps.',
                      flat(self.step(out, 2)))
        self.assertIn("glibc-2.34-283.el9_8: the two machines' files hold the "
                      "same locale sources and charmaps, so no locale's sort "
                      "rules can differ between them", flat(self.step(out, 1)))
        summary = out.split('AUDIT SUMMARY', 1)[1]
        block = section_body(summary, '-- One source tree, compared with '
                                      'itself')
        self.assertIn("the two machines' files hold the same locale sources "
                      "and charmaps, so no locale's sort rules can differ "
                      "between them", block)
        self.assertNotIn("means 'nothing was compared'", block)
        reindex = section_body(summary, '-- Reindex: sort order changes, '
                                        'confirm then REINDEX')
        self.assertTrue(reindex.startswith(
            "none -- the two machines' files hold the same locale sources "
            "and charmaps (see below)"), reindex)
        self.assertNotIn('nothing was compared', reindex)
        self.assertNotIn('code under locale/', block)
        self.assertNotIn('cannot read', before)

    def test_two_machines_of_one_build(self):
        out = self.out('one_build')
        summary = out.split('AUDIT SUMMARY', 1)[1]
        self.assertIn('-- One commit, compared with itself', summary)
        self.assertIn('one build given twice', flat(summary))
        self.assertIn('!! Both machines run the same glibc build. This '
                      'compares two machines, not an upgrade.', flat(summary))

    def test_a_file_without_charmaps(self):
        out = self.out('link')
        step2 = flat(self.step(out, 2))
        self.assertIn('Rules differ', step2)
        self.assertRegex(step2, r'NOT RUN: the comparison of .*\. The file '
                                r'of the machine that runs \S+ holds no '
                                r'charmaps this reads, because ')
        self.assertNotIn('Character sets and transliterations: nothing',
                         step2)
        step1 = flat(self.step(out, 1))
        self.assertIn('charmaps none, because', step1)


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
        cls.old = node_file('old.tar', cut_few(RHEL8), OLD,
                            sources=cls.BARE != 'old')
        cls.new = node_file('new.tar', cut_few(RHEL9), MID,
                            sources=cls.BARE != 'new')
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
            'DISTRO CHECK': 1, 'NODE TO NODE': 0, 'NODE ELLIPSIS': 0,
            'MEASURED ORDER': 1})
        self.assertIn(f"  DISTRO CHECK  do {builds[have]}'s", self.out)

    def test_the_steps_left_keep_their_numbers(self):
        """A side left out takes its number with it: the new side alone is
        still 7, never renumbered to 6."""
        distro = 6 if self.BARE == 'new' else 7
        self.assertEqual(numbered(self.out), [
            (distro, 'DISTRO CHECK'), (11, 'MEASURED ORDER')])

    def test_the_distro_patch_block_says_which_file_and_why(self):
        builds = {'old': (build_of(RHEL8), OLD), 'new': (build_of(RHEL9), MID)}
        bare, have = (6, 7) if self.BARE == 'old' else (7, 6)
        side = 'new' if self.BARE == 'old' else 'old'
        self.assertEqual(
            ' '.join(summary_block(self.out,
                                   f'-- Distro patches, step {bare}: NOT RUN')),
            f"{no_sources_text(self.bare)} So nothing above says whether the "
            f"{self.BARE} machine's own patches touch sort order, which the "
            f"tags cannot show.")
        build, tag = builds[side]
        body = summary_block(self.out, f'-- Distro patches, step {have} '
                                       f'({build} against {tag})')
        clean = distro_clean(tag, have, unread=())
        self.assertEqual(body[:len(clean)], clean)
        # The side with its sources compares its charmaps too. Its stand-in
        # sources lack the C (and RHEL8's en_US@ampm) its measurement
        # builds, so those are named as not compared.
        listed = 'C' if side == 'new' else 'C en_US@ampm'
        n = len(listed.split())
        keyed = os.path.join(self.out_dir, f'distro_keyed_locales.'
                                           f'{pair_slug(tag, side)}.txt')
        self.assertEqual(' '.join(body[len(clean):]), (
            f'Character sets and transliterations, this machine against '
            f'{tag}: {n} more locale(s) whose LC_COLLATE names a character '
            f'whose bytes differ, whose collating elements are built '
            f'differently, or that could not be compared: {listed} full '
            f'list: {keyed}'))

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


class NodeFileCharmaps:
    """13.7 PR D in the summary: what a node file's character set check
    found, per side, or why it did not run. A side's charmaps are 'greek'
    (alpha and beta swapped by hand in ISO-8859-7, so its step lists the
    Greek locales), 'before' (a file written before --extract packed them)
    or 'link' (one steps 6 to 8 refuse to read). A list a run before left
    in the folder must not be read as this run's.

    Three classes, so that every kind is on each side in one of them: a
    block that read a FIXED side's list, file or reason would pass a test
    of one side alone.
    """
    KINDS = None   # {'old': kind, 'new': kind}

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-charmaps-')
        cls.old = node_file(f'old-{cls.KINDS["old"]}.tar', cut_few(RHEL8), OLD,
                            charmaps=cls.KINDS['old'])
        cls.new = node_file(f'new-{cls.KINDS["new"]}.tar', cut_few(RHEL9), MID,
                            charmaps=cls.KINDS['new'])
        cls.keyed = {
            'old': os.path.join(cls.out_dir, f'distro_keyed_locales.'
                                             f'{pair_slug(OLD, "old")}.txt'),
            'new': os.path.join(cls.out_dir, f'distro_keyed_locales.'
                                             f'{pair_slug(MID, "new")}.txt'),
            'node': os.path.join(cls.out_dir, f'node_keyed_locales.'
                                 f'{pair_slug(build_of(RHEL8), build_of(RHEL9))}'
                                 f'.txt')}
        for path in cls.keyed.values():
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('# from a run before\nstale_XX\n')
        cls.rc, cls.out = run_wrapper(OLD, MID, '--old-node', cls.old,
                                      '--new-node', cls.new,
                                      out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_run_ends_cleanly(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertNotIn('stale_XX', self.out)

    def test_each_side_s_block(self):
        for side in ('old', 'new'):
            with self.subTest(side=side, kind=self.KINDS[side]):
                self.side_block(side, self.KINDS[side])

    def side_block(self, side, kind):
        n, build, tag, path = ((6, build_of(RHEL8), OLD, self.old)
                               if side == 'old' else
                               (7, build_of(RHEL9), MID, self.new))
        body = summary_block(self.out, f'-- Distro patches, step {n} '
                                       f'({build} against {tag})')
        clean = distro_clean(tag, n, unread=())
        self.assertEqual(body[:len(clean)], clean)
        lines = body[len(clean):]
        what = (f'Character sets and transliterations, this machine against '
                f'{tag}')
        step = flat(step_text(self.out, n))
        if kind == 'greek':
            # The stand-in sources lack the C (and RHEL8's en_US@ampm) the
            # measurement builds, so those are named as not compared.
            listed = (['C', 'el_CY', 'el_GR', 'el_GR@euro']
                      + (['en_US@ampm'] if side == 'old' else []))
            self.assertEqual(' '.join(lines), (
                f'{what}: {len(listed)} more locale(s) whose LC_COLLATE '
                f'names a character whose bytes differ, whose collating '
                f'elements are built differently, or that could not be '
                f'compared: {" ".join(listed)} full list: {self.keyed[side]}'))
            with open(self.keyed[side], encoding='utf-8') as fh:
                self.assertEqual([x for x in fh.read().split('\n')[1:] if x],
                                 listed)
            self.assertIn('el_GR (ISO-8859-7): 2 characters, named in '
                          'iso14651_t1_common', step)
            return
        if kind == 'before':
            # The file whole on its own line, so it can be copied.
            self.assertIn(f'{path} holds no charmaps.', lines)
            self.assertEqual(' '.join(lines), f'{what}: NOT RUN. {path} holds '
                                              f'no charmaps. {BEFORE_CHARMAPS}')
            self.assertIn(f'{path} holds no charmaps, because the file was '
                          f'written by an --extract that did not pack', step)
        else:
            self.assertEqual(' '.join(lines), (
                f'{what}: NOT RUN -- step {n} compared nothing (its warning '
                f'is below). Do not read this as nothing found.'))
            self.assertIn('LINKED.gz is not a file', step)
        self.assertFalse(os.path.exists(self.keyed[side]))

    def test_step_8_s_block(self):
        body = summary_block(self.out, f'-- Node-to-node locale data '
                                       f'({build_of(RHEL8)} -> '
                                       f'{build_of(RHEL9)})')
        at = [i for i, x in enumerate(body)
              if x.startswith('Character sets and transliterations')]
        self.assertEqual(len(at), 1, body)
        text = ' '.join(body[at[0]:])
        before = [p for side, p in (('old', self.old), ('new', self.new))
                  if self.KINDS[side] == 'before']
        if before:
            self.assertEqual(text, (
                'Character sets and transliterations, between the two '
                'machines: NOT RUN. ' + ' '.join(
                    f'{p} holds no charmaps. {BEFORE_CHARMAPS}'
                    for p in before)))
        else:
            self.assertEqual(text, (
                'Character sets and transliterations, between the two '
                'machines: NOT RUN -- step 8 compared nothing (its warning '
                'is below). Do not read this as nothing found.'))
        self.assertFalse(os.path.exists(self.keyed['node']))

    def test_step_11_is_handed_each_side_s_charmaps(self):
        """Step 11 checks the strings it measured against a side's charmaps
        when the file holds them ('greek': the tag's, whose sequences the
        list holds, so nothing is said), and otherwise says, in its report
        and in the summary, which side was not checked and why."""
        text = flat(self.out)
        for side, path in (('old', self.old), ('new', self.new)):
            with self.subTest(side=side, kind=self.KINDS[side]):
                head = (f'!! NOT checked whether the character sets of the '
                        f'{side} machine give a character to byte sequences '
                        f'that are not among the strings measured, because ')
                said = [x.split(' Where one does,')[0][len(head):]
                        for x in re.findall(re.escape(head) + r'.+? Where one '
                                            r'does,', text)]
                kind = self.KINDS[side]
                if kind == 'greek':
                    self.assertEqual(said, [])
                elif kind == 'before':
                    # The path whole: a cut one could not be copied.
                    self.assertEqual(said, [f'{path} holds no charmaps, '
                                            f'because the file was written by '
                                            f'an --extract that did not pack '
                                            f'the machine\'s charmaps '
                                            f'(/usr/share/i18n/charmaps); '
                                            f'extract it again with this '
                                            f'checkout\'s '
                                            f'scripts/locale_order.py.'] * 2)
                else:
                    self.assertEqual(len(said), 2)
                    self.assertTrue(said[0].startswith(
                        'its charmaps cannot be read: '), said)
                    self.assertIn('LINKED.gz is not a file', said[0])
        self.assertNotIn('either machine', text)
        self.assertNotIn('!! Measured in part', text)
        # One side is not checked whole, so nothing says both were.
        self.assertNotIn('Character sets: every byte sequence', text)


@needs_clone
class WrapperNodeFileCharmapsOld(NodeFileCharmaps, unittest.TestCase):
    KINDS = {'old': 'greek', 'new': 'before'}


@needs_clone
class WrapperNodeFileCharmapsNew(NodeFileCharmaps, unittest.TestCase):
    KINDS = {'old': 'before', 'new': 'greek'}


@needs_clone
class WrapperNodeFileCharmapsUnread(NodeFileCharmaps, unittest.TestCase):
    KINDS = {'old': 'link', 'new': 'link'}


@needs_clone
class WrapperNodeFilesWithoutSources(unittest.TestCase):
    """Neither file holds sources: no step reads a node's files, and every
    block names both files, the old one first."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-bare2-')
        cls.old = node_file('old.tar', cut_few(RHEL8), OLD, sources=False)
        cls.new = node_file('new.tar', cut_few(RHEL9), MID, sources=False)
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
            f"patches touch sort order, which the tags cannot show.")

    def test_every_block_names_both_files(self):
        self.assertEqual(
            section_body(self.summary, REMOVED),
            f'{NOT_CHECKED} {self.both} Between the tags: none -- no locale '
            f'file at {OLD} is gone at {MID}')
        self.assertTrue(section_body(self.summary, NODE_TO_NODE_NOT_RUN)
                        .startswith(self.both + ' So nothing above compared'))
        self.assertNotIn('Pass --', flat(self.summary))


@needs_clone
class WrapperNodeFileRefused(unittest.TestCase):
    """A node file that cannot be trusted, or node files given the wrong way,
    stop the run before step 1: no step, no summary, and the message names
    the reason."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-bad-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.old = node_file('old.tar', cut_few(RHEL8), OLD, sources=False)
        self.new = node_file('new.tar', cut_few(RHEL9), MID, sources=False)

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
        for extra in (('--old-order', cut_few(RHEL8)),
                      ('--new-order', cut_few(RHEL9)),
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
        rhel10 = node_file('rhel10.tar', cut_few(RHEL10), NEW, sources=False)
        second = self.assertRefusedBeforeStep1('--old-node', self.old,
                                               '--new-node', rhel10)
        self.assertIn("the new side given is glibc-2.34", second)
        self.assertNotIn('pass the machine you upgrade FROM first', second)

    def test_a_removed_file_is_made_again(self):
        """tearDownModule is called by test_build_sources too, in a process
        that may run this module's classes after it: the cut measurements it
        removed must not be handed out again. (A node file is keyed by its
        cut's path, so once the cut is made again under a new name an old
        node file is never asked for.)"""
        cut = cut_few(RHEL9)
        made = node_file('new.tar', cut, MID, sources=False)
        self.assertTrue(os.path.exists(cut) and os.path.exists(made))
        tearDownModule()
        self.assertFalse(os.path.exists(cut) or os.path.exists(made))
        again = cut_few(RHEL9)
        self.assertTrue(os.path.exists(again), again)
        self.assertTrue(os.path.exists(node_file('new.tar', again, MID,
                                                 sources=False)))

    def test_the_files_of_another_upgrade(self):
        rhel10 = node_file('rhel10.tar', cut_few(RHEL10), NEW, sources=False)
        out = self.assertRefusedBeforeStep1('--old-node', self.new,
                                            '--new-node', rhel10)
        self.assertIn("the old side given is glibc-2.28: give that build's "
                      "tag, glibc-2.34", out)


if __name__ == '__main__':
    unittest.main()
