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

from _harness import (COMPARED_BANNER, STEP2_LISTED, SUMMARY_STEP2,
                      EXPECTED_SHA, MID, NEW, OLD,
                      assert_collation_version, assert_no_route_looks,
                      pg_version_of,
                      REPO_ROOT, SCRIPTS_DIR,
                      backported_c, before_compared, compared_text,
                      deleted_steps_named, flat, locale_file,
                      needs_clone, assert_no_deleted_block,
                      nothing_compared_lines, summary_warnings,
                      run_wrapper, upstream_c, summary_block)


import filter_lc_collate_changes as step2
import glibc_locale_data as g
import locale_order as lom

# The `!!` under 'What was compared' for two sides read as tags
# (glibc_locale_data.tag_sides_text)
# over 2.28..2.34, as a run with neither machine's file asks for the builds;
# the summary repeats it whole among its warnings.
TAGS_FACT = ("!! Not checked for glibc-2.28 and glibc-2.34: they are upstream "
               "tags, which hold none of a distro's own locale files or "
               "patches. Step 2 reads only what the tags hold.")
TAGS_NOTICE = (TAGS_FACT + " If the machines run RHEL 8, 9 or 10, name each "
               "side as its build instead, as rpm -q glibc prints it on the "
               "machine without the architecture (glibc-2.34-283.el9_8), or as "
               "latest-el8, latest-el9 or latest-el10.")
from _harness import (GLIBC_CLONE, Step2Report, materialise_tag,
                      swap_alpha_beta, tag_charmaps)
from test_locale_order import (CHANGED_8_TO_9, RHEL8, RHEL9, RHEL10,
                               changes_in, parse_characters, parse_summary,
                               parse_rules_beside, trimmed)


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


def charmaps_state(out_dir):
    """What step 2's step2_rules_by_name says became of its character set
    check: the one file of the run, its 'charmaps' field."""
    found = glob.glob(os.path.join(out_dir, 'step2_rules_by_name.*.json'))
    if len(found) != 1:
        raise AssertionError(f'step2_rules_by_name: {found}')
    with open(found[0], encoding='utf-8') as fh:
        return json.load(fh)['charmaps']


# The `!!` step 2 prints for the files only the new side has when the old
# side is an upstream tag (16.123), up to its count.
ADDED_AT_OLD_TAG = ('!! Not compared, no file at {} ({}). A file added at '
                    'the new side')


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
WARNINGS = '-- Warnings the clean results above do NOT cover'
# Removed's line when a machine's file made both sides read their tags.
MACHINE_FILE_MADE_TAGS = ("A machine's file made both sides read their tags; "
                          "why is the first warning below.")
NOT_CHECKED = ('The tags cannot show what your distro adds or drops: '
               'NOT CHECKED.')
# The whole section over 2.28..2.34 given the tags alone.
NOTHING_GONE_OVER_OLD_MID = [
    NOT_CHECKED,
    "Pass --old-node and --new-node, each machine's file, to check it.",
    f'Between the tags: none -- no locale file at {OLD} is gone at {MID}',
]


# audit.sh's usage text, whole, with the script's own path as "audit.sh".
USAGE = [
    'usage: audit.sh <old> <new>',
    '         [--old-node FILE --new-node FILE]',
    '       e.g. audit.sh glibc-2.28 glibc-2.34',
    '            audit.sh glibc-2.28-251.el8_10.40 glibc-2.34-275.el9_8',
    '            audit.sh glibc-2.34-275.el9_8 latest-el10',
    '       each side is a glibc tag, glibc-<version> (run `ldd --version` on',
    '       the node), or a build as `rpm -q glibc` names it without the',
    '       architecture, rebuilt from its source RPM, or latest-elN for a',
    '       machine not installed yet: the newest build of RHEL N on Rocky',
    "       Linux's mirror, named before anything is read.",
    '       OLD first, NEW second: a reversed pair is refused, not',
    '       answered. Distance is NOT checked: RHEL8 to RHEL10 is one',
    '       pair, glibc-2.28 glibc-2.39, not two runs added up. What',
    '       that reports was measured with glibc-2.34 in the middle',
    '       (docs/scope.md). Every --* option takes a value.',
    '',
    '       The --*-node options are OPTIONAL and go together. Each is the',
    '       one file scripts/locale_order.py --extract wrote on that',
    '       machine, with its measurement, its build, its locale sources and',
    '       its charmaps (a file without them says why). The measurement is',
    "       how the machine's own glibc sorts every locale, which step 11",
    '       compares: it sees what a comparison of files cannot, a locale',
    '       whose files did not change can still sort differently. Step 2',
    "       reads each machine's own locale sources and charmaps, as the",
    '       build its file names, in place of the tag (the side given is',
    '       that build or its tag), and downloads nothing. A file without',
    '       the sources, or naming a build that is not of RHEL 8, 9 or 10,',
    '       makes both sides read their tags when that side was given as',
    '       its tag, and the run says so; given as the build itself, the',
    "       run is refused. When step 2 compares the two machines' own",
    '       locale sources, each locale of the old machine step 11 names is',
    "       set beside step 2's rules for it; otherwise the run says what",
    '       step 2 compared.',
]

REINDEX = SUMMARY_STEP2
REINDEX_POINTER = [
    "This list is from the tags. What each machine's own glibc says",
    "is under '-- Measured order', below.",
]
MEASURED = "-- Measured order: each machine's own glibc"
MEASURED_NOT_RUN = '-- Measured order: NOT RUN'
MEASURED_NOT_RUN_BODY = [
    "Pass --old-node and --new-node, each machine's file written by",
    'scripts/locale_order.py --extract. Without them nothing above',
    "asked either machine's glibc how it sorts, and a locale whose files",
    'did not change can still sort differently. Nor did anything compare',
    'the decimals of money, check which C library PostgreSQL runs on',
    'either machine, check that each machine accepts every locale and',
    "converts its text, or compare what each machine's character rules",
    '(LC_CTYPE) answer.',
]
MEASURED_FOOTER = '     Full report: step 11 above.'




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


def rules_beside(out):
    """What the run printed of step 2's rules beside step 11's verdict, as
    parse_rules_beside reads it: the lines under the Measured order footer
    of the summary, which step 11's section must end with, the same (one
    file, printed twice). Refuses a footer that is not there once."""
    summary = out.split('AUDIT SUMMARY', 1)[-1]
    lines = summary.splitlines()
    at = [i for i, line in enumerate(lines) if line == MEASURED_FOOTER]
    if len(at) != 1:
        raise AssertionError(f'{MEASURED_FOOTER!r} is a whole line '
                             f'{len(at)} time(s):\n{summary}')
    body = []
    for line in lines[at[0] + 1:]:
        if not line.strip() or line.startswith('--'):
            break
        body.append(line)
    text = '\n'.join(body) + '\n'
    step11 = out.split('\n== STEP 11  MEASURED ORDER  ', 1)[1].split(
        '\n== AUDIT SUMMARY', 1)[0]
    step11 = step11.rsplit('\n' + '=' * 64, 1)[0]
    if not body or not (step11.rstrip('\n') + '\n').endswith('\n' + text):
        raise AssertionError(f'step 11 does not end with the lines the '
                             f'summary prints:\n{text}\n---\n{step11[-2000:]}')
    return parse_rules_beside(text)


# The one line rules_beside prints where step 2 compared the two tags, not
# the machines' own locale sources.
RULES_OF_THE_TAGS = (f"Rules (step 2) are not set beside each locale: step "
                     f"2 compared the tags {OLD} and {MID}, not the "
                     f"machines' own locale sources.")


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


# The `!!` under 'What was compared' on the code no step compares
# (glibc_locale_data.CODE_NOT_COMPARED), whitespace collapsed, up to the end of its first
# sentence: what has to reach the reader on every route.
CODE_NOT_COMPARED_HEAD = (
    "!! Not compared on any route: the code that compiles and compares the "
    "rules (localedef's and strcoll's, each build's patches to it "
    "included).")


class _CodeNotComparedAndNoDeletedStep:
    """Two properties of every run, asserted on the routes the summary can
    take: tags (Wrapper), one commit (WrapperEmptyPair) and the two
    machines' files (WrapperNodeFiles); the runs on builds and on a tag and
    a build are test_build_sources._AuditOnBuilds.

    Step 9.3 deleted steps 4 and 5, which were what a run without the
    machines had to say about localedef's and strcoll's code: ko_KR.utf8
    moved between RHEL 8 and 9 through localedef's code, its file the same
    in both tags. So 'What was compared' says, on every route, that no step
    compares that
    code, and the summary repeats it once, under its warnings. And nothing a
    run prints or writes names a deleted step or its script: a sentence that
    still points to step 4 or 5 sends the reader to an answer that is not
    there. FILES_ARE_THIS_RUNS says whether the class's output folder holds
    one run's files only."""

    FILES_ARE_THIS_RUNS = True
    # The class's two sides, whose glibc versions 'What was compared'
    # compares as PostgreSQL does (16.166).
    SIDES = (OLD, MID)

    def test_what_postgresql_warns_about_is_said_once(self):
        """16.166: one sentence for the pair, from each side's glibc
        version; and 13.9: no route looks for a user's or an extension's
        function, said once and repeated once by the summary."""
        self.assertEqual(self.rc, 0, self.out[-2000:])
        assert_collation_version(self, self.out,
                                 *map(pg_version_of, self.SIDES))
        assert_no_route_looks(self, self.out)

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
        # 'What was compared' printed it, once, before the summary repeated
        # it, and nothing else before the summary did.
        self.assertEqual(flat(compared_text(self.out))
                         .count(CODE_NOT_COMPARED_HEAD), 1)
        self.assertEqual(flat(warnings[0]).count(CODE_NOT_COMPARED_HEAD) +
                         flat(before_compared(self.out))
                         .count(CODE_NOT_COMPARED_HEAD), 0)

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
        # Set as a reader's shell might have it: audit.sh unsets it, and
        # only its own step 11 sets it, so the C.UTF-8 `!!` below must not
        # say step 11 measured anything (test_C_UTF_8_is_flagged...).
        cls.rc, cls.out = run_wrapper(
            OLD, MID, out_dir=cls.out_dir,
            env_extra={g.STEP11_ENV: '1'})

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


    def test_the_names_with_no_file_at_the_old_tag_reach_the_warnings(self):
        """An upstream tag cannot show that the old system lacked them
        (16.123): step 2's `!!` names ckb_IQ and mnw_MM, and the summary
        repeats it once, whole."""
        warnings = summary_warnings(self.out)
        head = flat(ADDED_AT_OLD_TAG.format(OLD, 2))
        self.assertEqual(warnings.count(head), 1, self.out)
        block = warnings.split(head, 1)[1].split(' !! ', 1)[0]
        self.assertIn('ckb_IQ -> ckb_IQ mnw_MM -> mnw_MM', block)
        self.assertIn('Confirm with `locale -a` on the OLD machine', block)

    def test_the_character_set_check_is_written_as_compared(self):
        """2.1: a state with its reason on every route, never a bare
        null; over two tags the check ran."""
        self.assertEqual(charmaps_state(self.out_dir),
                         {'state': 'compared', 'why': None})

    def test_sv_FI_reached_through_its_copy_of_sv_SE(self):
        """Between 2.28 and 2.34 sv_FI's own file does not change; its rules
        differ because step 2 follows its `copy "sv_SE"`, and they name
        sv_SE's two characters."""
        self.assertEqual(Step2Report(step_text(self.out, 2)).differs['sv_FI'],
                         ([0x57, 0x77], False))

    def test_step_2_list_is_written_and_feeds_the_summary(self):
        """The file the summary's step 2 list reads exists, is named for the
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

    def test_what_was_compared_names_no_machines_file_without_one(self):
        compared = compared_text(self.out)
        self.assertTrue(compared.startswith('Tag provenance'), compared)
        self.assertNotIn("Machines' files", self.out)

    def test_the_summary_says_c_utf8_was_not_compared(self):
        """Absent is not empty, at the summary level (16.123).

        Neither tag holds glibc's file C, so nothing compared C.UTF-8's
        rules, and PostgreSQL gives it no collation version. A summary that
        simply omits it reads exactly like one that cleared it, which is
        false negative #1 in a new costume. Step 2's `!!` says so, once, and
        the warnings block repeats it."""
        summary = flat(self.out.split('AUDIT SUMMARY')[1])
        self.assertEqual(summary.count(
            f"!! {OLD} and {MID} hold no localedata/locales/C, the file "
            f"C.UTF-8 is built from, and a distro's build can ship its own, "
            f"so C.UTF-8's sort rules were not compared. "
            f"{step2.C_NO_VERSION}"), 1, summary)

    def test_reindex_points_nowhere_else_without_the_measurements(self):
        self.assertNotIn(REINDEX_POINTER[0],
                         summary_block(self.out, REINDEX))

    def test_the_summary_says_the_measured_order_was_NOT_RUN(self):
        """Absent is not empty: without the two machines' measurements the
        summary says what went unasked, not nothing."""
        self.assertEqual(summary_block(self.out, MEASURED_NOT_RUN),
                         MEASURED_NOT_RUN_BODY)


    def test_what_the_tags_cannot_see_is_said_before_their_none(self):
        """Over 2.28..2.34 the tags remove nothing, and the
        upgrade removes en_US@ampm, a file only RHEL8 ships. A "none" as the
        section's first line is that false negative, so the line saying what
        the tags cannot check comes first."""
        self.assertEqual(removed_lines(self.out), NOTHING_GONE_OVER_OLD_MID)

    def test_the_removed_section_is_on_the_first_screen(self):
        """Directly under the summary's step 2 list, above the long lists and
        the warnings: the defect was a finding nobody reading the summary
        would reach."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertLess(summary.index(REINDEX), summary.index(REMOVED))
        self.assertLess(summary.index(REMOVED),
                        summary.index(MEASURED_NOT_RUN))


@needs_clone
class WrapperTakesTheKeyedListToReindex(unittest.TestCase):
    """What step 2's character set check lists (backlog 13.7, PR B) reaches
    the summary's step 2 list, beside the locales whose rules differ. No pinned
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
    the summary's step 2 list says nothing was compared.
    """

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-empty-')
        cls.rc, cls.out = run_wrapper(NEW, NEW, out_dir=cls.out_dir)

    SIDES = (NEW, NEW)

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
        summary's step 2 list, whose other branch is
        WrapperDirectionUndetermined's
        (doc-sweep, 2026-09-30, of step 3's notice then)."""
        report = Step2Report(step_text(self.out, 2))
        self.assertEqual(report.nothing, 'the two tags are one commit.')
        self.assertEqual(summary_block(self.out, REINDEX),
                         nothing_compared_lines('tags'))

    def test_the_character_set_check_is_written_as_one_commit(self):
        self.assertEqual(charmaps_state(self.out_dir),
                         {'state': 'one commit', 'why': None})

    def test_tag_provenance_names_the_tag_once(self):
        """The tag given twice is one tag: one line under 'Tag provenance',
        whatever gpg answers on this machine (16.173)."""
        lines = re.findall(r'(?m)^  (?:OK|INVALID|UNCHECKED|UNSIGNED) +'
                           + re.escape(NEW) + ' = ',
                           compared_text(self.out))
        self.assertEqual(len(lines), 1, self.out)

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
        # Through flat(): the notice is one `warn()` block, wrapped at 78
        # columns, so the phrase is split across lines in the raw text.
        self.assertIn('are the same commit', flat(self.out))
        # Repeated at the bottom, where a reader who scrolled past it will
        # see it, and once: the sides and step 2 each print it, and the
        # warnings block keeps one of two identical blocks (16.168).
        self.assertEqual(summary_warnings(self.out).count(
            'are the same commit'), 1, self.out)
        # And said nowhere else in the summary, in other words (10.1).
        assert_no_deleted_block(self, self.out)

    def test_the_one_commit_notice_names_every_step_that_still_has_evidence(self):
        """The notice once said the evidence was "the node-to-node check
        and sql/c_utf8_probe.sql, nothing else". Each machine's own file
        and the measured order can show a change on this pair too. The
        summary says it with the order check's own `!!`, in its warnings,
        compared whole, so an "only" cannot come back in other words."""
        self.assertEqual(summary_warnings(self.out).count(flat(
            f"!! {NEW} and {NEW} are the same commit. Step 2 compares "
            f"nothing: it would compare upstream source against itself. "
            f"{g._NOTHING_COMPARED_REST.format(on='the tags')}")), 1,
            self.out)

    def test_the_removed_none_sits_above_the_one_commit_notice(self):
        """The tags' "none" is printed on one commit compared with itself
        too, and it is the notice below it, not the section, that says
        nothing was compared: the order check's `!!`, in the warnings. This
        pins the arrangement: the notice must come after the section."""
        lines = removed_lines(self.out)
        self.assertEqual(lines[-1], f'Between the tags: none -- no locale '
                                    f'file at {NEW} is gone at {NEW}')
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertGreater(summary.index(WARNINGS), summary.index(REMOVED))
        self.assertIn('are the same commit', summary_warnings(self.out))

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
        self.assertEqual(summary_warnings(self.out).count(
            'are the same commit'), 1, self.out)
        # Compared as text, the tag and its sha are two commits, and step 5
        # (deleted in step 9.3) printed its clean sentence over this pair
        # (false-negative-reviewer, 2026-09-30). The summary says so instead.
        # A tag and a commit are two sides, not two tags (16.183).
        self.assertIn('none -- nothing was compared: the two sides are one '
                      'commit or one source tree', summary)

    def test_the_summary_calls_the_pair_two_sides(self):
        """16.183: a tag and its sha are a tag and a commit, so the summary
        names them as sides, and the order check's `!!` speaks of upstream
        commits; once each among the warnings."""
        self.assertEqual(removed_lines(self.out), [
            'The sides cannot show what your distro adds or drops: '
            'NOT CHECKED.',
            "Pass --old-node and --new-node, each machine's file, to check "
            "it.",
            f'Between the sides: none -- no locale file at {NEW} is gone at '
            f'{EXPECTED_SHA[NEW]}'])
        self.assertEqual(summary_warnings(self.out).count(
            'A run on upstream commits says nothing about an intra-major '
            'upgrade'), 1, self.out)

    def test_the_sha_is_called_a_commit(self):
        """The sha is a commit, not an upstream tag; the tag stays one
        (16.155). Said once under 'What was compared' and once among the
        summary's warnings."""
        said = (f'Not checked for {NEW} and {EXPECTED_SHA[NEW]}: {NEW} is '
                f'an upstream tag and {EXPECTED_SHA[NEW]} an upstream '
                f'commit, which hold none of a distro\'s own locale files '
                f'or patches. Step 2 reads only what they hold.')
        self.assertEqual(flat(compared_text(self.out)).count(said), 1,
                         self.out)
        self.assertEqual(summary_warnings(self.out).count(said), 1, self.out)
        self.assertNotIn('they are upstream tags', flat(self.out))
        # Step 2 says the same of the pair, two screens below.
        self.assertEqual(Step2Report(step_text(self.out, 2)).nothing,
                         'the two sides are one commit.')

    def test_postgresql_warns_about_none_of_one_commit(self):
        """The sha's version is read from version.h at that commit, as the
        tag's is (16.166): one version, so the `!!`."""
        assert_collation_version(self, self.out, '2.39', '2.39')


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
        self.env = self.shim("'undetermined none tag tag tags'")

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

    def test_reindex_keeps_its_sentence_when_the_pair_is_two_commits(self):
        """The other branch of the summary's empty step 2 list. With the order
        check stood in for, the wrapper takes this pair for two commits and
        step 2 lists nothing: the line must not say that nothing was
        compared, which is the one-commit branch."""
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary_block(out, REINDEX), [
            'none -- of what step 2 compared, nothing differs; step 2, above,',
            'names what it did not compare'])

    def test_an_unrecognised_state_stops_the_run(self):
        """The branch that keeps the four states honest: a word the wrapper
        does not know means the check did not answer, and "the pair is in
        order" is the assumption that runs the audit against the wrong tag.
        Each of the first two words, apart."""
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

    def test_the_sides_words_are_checked(self):
        """16.183: after its two words the order check gives each side's
        class and the pair's word, which the summary's sentences use. Three
        words out of place, one too few or one too many stop the run, after
        the checks of the first two."""
        for answer in ('forward none tags tag tags', 'forward none tag tag',
                       'forward none tag commit tags',
                       'forward none tag tag tags more'):
            with self.subTest(answer=answer):
                rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                                      env_extra=self.shim(repr(answer)))
                self.assertNotEqual(rc, 0)
                self.assertIn(f"answered '{answer}', whose last three words",
                              out)
                self.assertNotIn('AUDIT SUMMARY', out)

    def test_one_tree_git_cannot_order_compares_nothing(self):
        """Two commits of one source tree that neither descends from the
        other, as a master commit and the same patch on a wip branch are in
        the clone (158e939a and 42f22e7c). Both states are said:
        the direction was not established, and nothing was compared -- the
        summary's step 2 list must not call the empty list a real result
        (false-negative-reviewer, round 1 of 7.1, of step 3's notice then).
        """
        rc, out = run_wrapper(NEW, NEW, out_dir=self.out_dir,
                              env_extra=self.shim(
                                  "'undetermined tree tag tag tags'"))
        self.assertEqual(rc, 0, out)
        # audit.sh's own decision: the line never calls the empty list a
        # result. What the pair is, and that its direction is not known,
        # the steps' `!!` say, which this shim cannot make them print
        # (test_git_helpers and AuditShOnATagAndABuild hold them).
        self.assertEqual(summary_block(out, REINDEX),
                         nothing_compared_lines('tags'))
        assert_no_deleted_block(self, out)


@needs_clone
class WrapperRefusesBadInput(unittest.TestCase):
    """The failure modes that would otherwise produce a clean-looking audit."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-bad-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)

    def test_bogus_tag_fails_and_prints_no_summary(self):
        """Non-zero means a real error. A summary here would be a lie."""
        rc, out = run_wrapper(OLD, 'glibc-9.99-nope', out_dir=self.out_dir)
        self.assertEqual(rc, 2, out)
        # By its message: every refusal of audit.sh exits 2.
        self.assertIn("error: unknown git ref 'glibc-9.99-nope' even after "
                      "fetching tags", out)
        self.assertNotIn('Tag provenance', out)
        self.assertNotIn('== STEP 2', out)
        self.assertNotIn('AUDIT SUMMARY', out)

    def python3_saying(self, version):
        """PATH with a python3 that prints `version` for audit.sh's version
        question and runs the real interpreter for everything else."""
        import sys
        d = tempfile.mkdtemp(prefix='pg-glibc-wrapper-py-')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        shim = os.path.join(d, 'python3')
        with open(shim, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\n'
                     'case "$2" in *sys.version_info*) '
                     + (f'echo {version}; exit 0' if version else 'exit 1')
                     + ' ;; esac\n'
                     f'exec {sys.executable} "$@"\n')
        os.chmod(shim, 0o755)
        return {'PATH': d + os.pathsep + os.environ['PATH']}

    def test_a_python3_older_than_3_7_is_refused_first(self):
        """The steps need 3.7; an older python3 stopped the first of them
        with a traceback (2.28). Refused before anything runs, by its
        message, and nothing after it: the pair given is refused right
        after the check, so a run that printed the message and went on
        would print that refusal too. 3.7 and 3.10 (not a string order)
        pass and reach it; a version that cannot be read, and a python3 that
        does not run, are each said so."""
        later = 'error: --old-node and --new-node go together'
        for version, said in (
                ('3.6', 'error: audit.sh needs python3 3.7 or newer; '
                        'python3 here is 3.6'),
                ('2.7', 'error: audit.sh needs python3 3.7 or newer; '
                        'python3 here is 2.7'),
                ('x', "error: could not read python3's version ('x')"),
                (None, 'error: python3 did not run here; audit.sh needs '
                       'python3 3.7 or newer'),
                ('3.7', later), ('3.10', later)):
            with self.subTest(version=version):
                rc, out = run_wrapper(OLD, MID, '--old-node', 'x',
                                      out_dir=self.out_dir,
                                      env_extra=self.python3_saying(version))
                self.assertEqual(rc, 2, out)
                self.assertEqual(flat(out).count('error:'), 1, out)
                self.assertIn(said, flat(out))

    def test_wrong_argc_is_exit_2(self):
        rc, out = run_wrapper(OLD, out_dir=self.out_dir)
        self.assertEqual(rc, 2)
        self.assertIn('usage:', out)

    def test_the_usage_claims_no_single_step_for_C_UTF_8(self):
        """The usage text once called step 8 "the only source-level evidence
        there is about C.UTF-8". Compared whole, so no such claim comes back
        in other words."""
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
        self.assertIn('Every step takes <old_tag> <new_tag>', flat(out))
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

    def test_the_folder_and_build_id_options_are_gone(self):
        """--*-locales-dir and --*-build-id fed steps 6 to 8, deleted in
        step 9.4b, and --*-order took a plain measurement, deleted in step
        11.1: each is refused as an unknown argument, before anything runs,
        never taken for an option that still exists."""
        for opt in ('--old-locales-dir', '--new-locales-dir',
                    '--old-build-id', '--new-build-id',
                    '--old-order', '--new-order'):
            with self.subTest(opt=opt):
                rc, out = run_wrapper(OLD, MID, opt, 'x',
                                      out_dir=self.out_dir)
                self.assertEqual(rc, 2, out)
                self.assertIn(f"error: unknown argument '{opt}'", out)
                self.assertNotIn(COMPARED_BANNER, out)
                self.assertNotIn('Tag provenance', out)

    def test_an_option_without_a_value_says_so(self):
        """An option as the last word on the line died in `shift 2` under
        `set -e`: exit 1, no message at all. An empty value is refused for
        the same reason -- it leaves the variable unset, which is
        indistinguishable from never having asked for it."""
        options = ('--old-node', '--new-node')
        # Every branch of the case, not one of them: a branch once asserted
        # by nothing let a mutation to it leave the suite green.
        cases = [(opt,) for opt in options] + [(opt, '') for opt in options]
        for tail in cases:
            with self.subTest(args=tail):
                rc, out = run_wrapper(OLD, MID, *tail, out_dir=self.out_dir)
                self.assertEqual(rc, 2, out)
                self.assertIn(f'error: {tail[0]} needs a value', out)
                self.assertIn('usage:', out)



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
        # Both lists step 2 writes: the locales whose rules differ and the
        # locales the character set check lists (backlog 13.7, PR B).
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

        step3_affected_locales.txt is named for no particular pair, nor is
        step 1's changed_locales.txt, so a
        leftover from a different audit sits where the summary reads. Seed it,
        then audit a pair that finds nothing: the stale locale must not appear
        as this pair's answer.

        Since step 9.2 no step writes it and the summary does not read it;
        audit.sh still removes it up front, so a list an earlier version
        left there is gone after the run. The same for changed_locales.txt
        since step 9.5.
        """
        os.makedirs(self.out_dir, exist_ok=True)
        stale = os.path.join(self.out_dir, 'step3_affected_locales.txt')
        with open(stale, 'w', encoding='utf-8') as fh:
            fh.write('no_SUCH_locale\n')
        changed = os.path.join(self.out_dir, 'changed_locales.txt')
        with open(changed, 'w', encoding='utf-8') as fh:
            fh.write('localedata/locales/no_SUCH_locale\n')
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
        self.assertFalse(os.path.exists(changed))
        self.assertFalse(os.path.exists(listed))
        self.assertEqual([x for x in sql if os.path.exists(x)], [])


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


class _CommitSides:
    """16.183: one side or both given by its commit id, over the pair whose
    tags remove a locale file (WrapperTagsRemoveALocale). The route is the
    tags', the words are the sides': each sentence about both says sides,
    and each about one side names its class. SIDES is set by each
    subclass."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-commit-')
        cls.rc, cls.out = run_wrapper(*cls.SIDES, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_removed_names_the_sides(self):
        self.assertEqual(self.rc, 0, self.out)
        lines = removed_lines(self.out)
        self.assertEqual(lines[:4], [
            'The sides cannot show what your distro adds or drops: '
            'NOT CHECKED.',
            "Pass --old-node and --new-node, each machine's file, to check "
            "it.",
            f'Between the sides, gone at {self.SIDES[1]}:',
            'aa_ER@saaho (renamed to ssy_ER)'])

    def test_step_2_names_the_sides(self):
        step2 = flat(self.out.split('== STEP 2', 1)[1].split(
            'AUDIT SUMMARY', 1)[0])
        self.assertIn('in SUPPORTED at both sides use differ between', step2)


@needs_clone
class WrapperCommitOnTheOldSide(_CommitSides, unittest.TestCase):
    SIDES = (EXPECTED_SHA[MID], NEW)

    def test_the_unproven_additions_name_the_old_commit(self):
        """What only the new side has is not compared, in a `!!` naming the
        old side as a commit, with its eleven names, once among the
        warnings (the gate is _is_tag of the old side, false-negative-
        reviewer F3)."""
        sha = EXPECTED_SHA[MID]
        warnings = summary_warnings(self.out)
        self.assertEqual(warnings.count(
            f'!! Not compared, no file at {sha} (11). A file added at the new '
            'side cannot change what PostgreSQL already stored ONLY IF the '
            'locale did not exist on the old system, and an upstream commit '
            'cannot establish that -- distros backport.'), 1, self.out)
        for line in ('ab_GE -> (not in SUPPORTED)',
                     'ssy_ER -> ssy_ER; renamed from aa_ER@saaho',
                     'zgh_MA -> zgh_MA'):
            self.assertIn(line, warnings)
        # C.UTF-8's `!!` names the old commit too (print_c_utf8's gate).
        self.assertIn(f'!! {sha} holds no localedata/locales/C, the file '
                      'C.UTF-8 is built from', warnings)

    def test_the_new_tag_stays_a_tag(self):
        """Control: the new side is a tag, and step 2 says so of it."""
        self.assertIn('Unless its distro ships the file anyway, which an '
                      'upstream tag cannot show', flat(self.out))


@needs_clone
class WrapperCommitOnTheNewSide(_CommitSides, unittest.TestCase):
    SIDES = (MID, EXPECTED_SHA[NEW])

    def test_the_removed_files_name_the_new_commit(self):
        self.assertIn('Unless its distro ships the file anyway, which an '
                      'upstream commit cannot show', flat(self.out))

    def test_the_old_tag_stays_a_tag(self):
        """Control: the old side is a tag, and the `!!` says so of it."""
        self.assertIn(f'!! Not compared, no file at {MID} (11). A file added '
                      'at the new side cannot change what PostgreSQL already '
                      'stored ONLY IF the locale did not exist on the old '
                      'system, and an upstream tag cannot establish that',
                      summary_warnings(self.out))


@needs_clone
class WrapperCommitOnBothSides(_CommitSides, unittest.TestCase):
    """Two commits: the one pair of classes no other run of audit.sh gives
    (false-negative-reviewer, final round of 10.4)."""
    SIDES = (EXPECTED_SHA[MID], EXPECTED_SHA[NEW])


@needs_clone
class WrapperStaleStepLog(unittest.TestCase):
    """Its own output directory, so the stale log it plants reaches no other
    test."""


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
    """(out without the `!!` for the sides read as tags, under 'What was
    compared' and among the summary's warnings, ((how many of each), what it
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
    standing in for the node's (CI has no node).
    Without sources it names NO_SOURCES_DIR for both.
    `charmaps`: 'tag', the tag's; 'greek', the tag's with alpha and beta
    swapped in ISO-8859-7, a distro's patch made by hand; 'link', the tag's
    and a link, a folder CharmapsDir refuses to read; 'before', none and no
    entry for them, as a file written before --extract packed them.
    Built once per module."""
    key = (name, measurement, tag, sources, charmaps)
    if key not in _NODE_FILES:
        base = tempfile.mkdtemp(prefix='pg-glibc-node-file-')
        directory = (materialise_tag(GLIBC_CLONE, tag, base) if sources
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


def unread_side_text(side):
    """What the first warning says of a side whose file holds no sources,
    whitespace collapsed, up to its reason (glibc_locale_data.machine_sides
    names the side and the build, then why)."""
    return (f"which this run cannot read: it holds no locale sources, "
            f"because {NO_SOURCES_REASON.rstrip('.')}")


def first_warning(summary):
    """The first `!!` block under the summary's warnings, whitespace
    collapsed: the one the step0 log carries, which names the files."""
    warnings = section_body(summary, WARNINGS)
    if not warnings.startswith('!! '):
        raise AssertionError(f'no warning opens the block:\n{summary}')
    return warnings.split(' !! ', 1)[0]


OPTIONAL_SECTIONS = ('MEASURED ORDER',)


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
class WrapperNodeFiles(_CodeNotComparedAndNoDeletedStep, unittest.TestCase):
    """--old-node and --new-node on RHEL8 -> RHEL9, each file holding a
    tag's locale sources and charmaps in place of its machine's: the run
    reads each machine's own files as the build it runs, and step 11
    measures both."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-')
        cls.old_file = node_file('old.tar', cut_to_pair(RHEL8), OLD)
        cls.new_file = node_file('new.tar', cut_to_pair(RHEL9), MID)
        cls.rc, cls.out = run_wrapper(
            OLD, MID, '--old-node', cls.old_file, '--new-node', cls.new_file,
            out_dir=cls.out_dir)
        # The two builds the files name, as audit.sh names the pair.
        cls.pair = pair_slug(g.without_arch(build_of(RHEL8)),
                             g.without_arch(build_of(RHEL9)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)


    def test_every_section_ran(self):
        self.assertEqual(banners(self.out), {'MEASURED ORDER': 1})
        self.assertEqual(re.findall(r'^== STEP (\d+)', self.out, re.M),
                         ['2', '11'])
        # 'What was compared' once, before every step.
        self.assertNotIn('== STEP', before_compared(self.out))

    def test_the_machines_files_are_named_as_given(self):
        """What was compared names each machine's file as the reader gave
        it, before what the run read from it."""
        said = compared_text(self.out).split('\n')
        self.assertEqual(said[:3], ["Machines' files, as given:",
                                    f"  old  {self.old_file}",
                                    f"  new  {self.new_file}"])

    def test_step_11_carries_its_number(self):
        """The number the docs use. Steps 3 to 10 were deleted in branch 9
        and their numbers are not reused."""
        self.assertEqual(numbered(self.out), [(11, 'MEASURED ORDER')])

    def test_the_summary_names_the_fourteen_locales(self):
        summary = parse_summary(measured_block(self.out))
        self.assertEqual(set(summary['changes']), CHANGED_8_TO_9)
        self.assertNotIn('NOT RUN', self.out.split('AUDIT SUMMARY')[1])

    def test_a_run_that_prints_step_11_does_not_point_to_its_log(self):
        """The control of WrapperNamesStep11sReportWhenALaterStepStops:
        the report was printed, so nothing points to where it is."""
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertNotIn(STEP11_NOT_PRINTED, self.out)

    def test_the_summary_names_this_runs_characters_file(self):
        """Every character, other string and pair step 11 found, in a file
        named for the pair in this run's output folder, holding all fourteen
        locales."""
        path = os.path.join(self.out_dir, 'measured_order_characters.'
                            + self.pair + '.tsv')
        self.assertEqual(
            parse_summary(measured_block(self.out))['characters_file'], path)
        with open(path, encoding='utf-8') as f:
            names = set().union(*map(set, changes_in(parse_characters(
                f.read()))))
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
                              + self.pair)
        self.assertEqual(named, [(enc, f'{prefix}.{enc}.sql')
                                 for enc in ('LATIN1', 'LATIN9', 'UTF8')])
        for enc, path in named:
            self.assertTrue(os.path.isfile(path), path)
        self.assertIn('  python3 scripts/confirm_sql.py read FILE old.out '
                      'new.out', step11.split('\n'))
        self.assertNotIn('measured_order_confirm.',
                         self.out[self.out.index('AUDIT SUMMARY'):])

    def test_the_block_sits_under_removed(self):
        """Removed stays directly under step 2's list, where it was put so
        that a locale the upgrade deletes is on the first screen: this block
        is long, and above Removed it pushed that section down."""
        summary = self.out.split('AUDIT SUMMARY')[1]
        self.assertLess(summary.index(REINDEX), summary.index(REMOVED))
        self.assertLess(summary.index(REMOVED), summary.index(MEASURED))
        self.assertNotIn(MEASURED_NOT_RUN, summary)

    def test_the_warnings_block_does_not_repeat_step_11(self):
        """Its `!!` headers there would stand over lists the block does not
        carry: "4 locale(s):" and nothing under it."""
        warnings = flat(self.out.split('-- Warnings the clean results above '
                                       'do NOT cover')[1])
        for header in ('!! Could not be measured', '!! Not on the new machine',
                       '!! Unchanged only as far as measured'):
            with self.subTest(header=header):
                self.assertNotIn(header, warnings)

    def test_each_locale_beside_its_rules(self):
        """Step 2's rules beside step 11's verdict, every locale step 11
        names: or_IN's order changes with its rules, ko_KR.utf8's does
        not, and the two RHEL9 removes get step 11's words first. The
        stand-in sources are the tags', which hold no C: C.utf8's rules are
        not compared, never "do not"."""
        got, alone = rules_beside(self.out)
        self.assertEqual(alone, [lom.RULES_LEAD])
        by_name = {name: line for names, line in got.items()
                   for name in names}
        self.assertEqual(set(by_name), CHANGED_8_TO_9
                         | {'en_US@ampm', 'en_US.utf8@ampm'})
        changes = 'the order changes, and so do its rules (step 2)'
        for name in ('or_IN', 'or_IN.utf8', 'sv_SE', 'swedish'):
            self.assertEqual(by_name[name], changes)
        self.assertEqual(by_name['ko_KR.utf8'],
                         'the order changes; its rules (step 2) do not')
        self.assertEqual(by_name['C.utf8'],
                         'the order changes; rules (step 2): not compared, '
                         'no locale file of that name')
        # No stand-in holds the file en_US@ampm, Red Hat's (the recorded
        # 8 -> 9 example names it "no file en_US@ampm on the new side").
        self.assertEqual(
            got[('en_US.utf8@ampm', 'en_US@ampm')],
            'not on the new machine; rules (step 2): not compared, no locale '
            'file of that name; this does not show the order')

    def test_no_step_sends_the_reader_to_the_package_changelog(self):
        """Steps 6 to 10, since deleted, suggested `rpm -q --changelog glibc | grep -i
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
        # that print the steps' advice must be among those read (the scripts
        # of steps 6 to 10 that printed it are deleted).
        self.assertTrue({'audit.sh', 'filter_lc_collate_changes.py',
                         'glibc_locale_data.py', 'locale_order.py'}
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
    """The run reads each machine's own locale sources and charmaps
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
        directory = materialise_tag(GLIBC_CLONE, tag, base)
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
            # The same sources and neither file with charmaps: one tree,
            # no charmap read.
            'same_no_charmaps': (
                cls.machine_file('old.tar', few9, MID, charmaps=False),
                cls.machine_file('new.tar', cls.measurement(
                    cut_few(RHEL9), later9), MID, charmaps=False)),
        }
        tags = {'link': (OLD, MID), 'same_sources': (MID, MID),
                'one_build': (MID, MID), 'same_no_charmaps': (MID, MID)}
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
        listed = re.search(
            r'Listed in the summary \(\d+\): ([^.]*?)(?: Not |$)', step2)
        self.assertTrue(listed, step2)
        names = listed.group(1).replace(',', ' ').split()
        for name in ('de_IT', 'zz_LINK'):
            with self.subTest(name=name):
                self.assertIn(name, names)
        # Step 2's own list of what is gone, which the summary's Removed
        # prints: neither link is in it, nor among the files added.
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
        self.assertNotIn('zz_LINK', step2.split(STEP2_LISTED)[0]
                         .split(unread_line)[0])
        skipped = ("2 entries of the machine's files were not read, so a "
                   "locale that is, or copies, one of them is not "
                   "compared: de_IT (symlink), zz_LINK (symlink).")
        self.assertIn(skipped, flat(compared_text(out)))
        # A `!!`, so the summary repeats it among its warnings.
        warnings = out.split('AUDIT SUMMARY', 1)[1].split(
            '-- Warnings the clean results above do NOT cover', 1)
        self.assertEqual(len(warnings), 2, out[-2000:])
        self.assertIn(skipped, flat(warnings[1]))

    def test_two_builds_of_one_release_with_the_same_sources(self):
        out = self.out('same_sources')
        before = flat(before_compared(out))
        self.assertNotIn('DIRECTION', flat(out))
        self.assertIn('Rules: not compared, the two machines\' files hold '
                      'the same locale sources and charmaps.',
                      flat(self.step(out, 2)))
        self.assertIn("glibc-2.34-283.el9_8: the two machines' files hold the "
                      "same locale sources and charmaps, so no locale's sort "
                      "rules can differ between them",
                      flat(compared_text(out)))
        # The summary says it with the order check's `!!` alone, once, and
        # its line under step 2's list points there (10.1).
        self.assertEqual(summary_warnings(out).count(
            "the two machines' files hold the same locale sources and "
            "charmaps, so no locale's sort rules can differ between them"),
            1, out)
        self.assertEqual(summary_block(out, REINDEX)[:2],
                         nothing_compared_lines('builds'))
        assert_no_deleted_block(self, out)
        # One tree holds its charmaps once: no line of step 2's beside step
        # 11's verdict says they were not compared.
        self.assertEqual(rules_beside(out), ({}, [
            "Rules (step 2) are not set beside each locale: step 2 compared "
            "nothing, the two machines' files hold the same locale sources "
            "and charmaps."]))
        self.assertNotIn('cannot read', before)

    def test_the_same_sources_without_charmaps_never_claim_the_charmaps(self):
        """Neither file holds charmaps, so no charmap was read: one source
        tree, said as the same locale sources, and the character sets as
        not compared, under 'What was compared', in step 2, in the
        summary's warnings (where its line under step 2's list points) and
        in the line beside step 11's verdict -- never "the same charmaps"
        (false-negative-reviewer and doc-sweep, round 2 of 9.4a)."""
        out = self.out('same_no_charmaps')
        self.assertNotIn('DIRECTION', flat(out))
        compared, step2 = (flat(compared_text(out)),
                           flat(self.step(out, 2)))
        self.assertIn("the two machines' files hold the same locale sources, "
                      "so no locale's sort rules can differ between them, "
                      "and step 2 has nothing to compare; their "
                      "character sets were not compared, as a file holds no "
                      "charmaps this reads.", compared)
        self.assertIn("Rules: not compared, the two machines' files hold the "
                      "same locale sources.", step2)
        self.assertIn("Character sets and transliterations: not compared, a "
                      "machine's file holds no charmaps this reads.", step2)
        # Each file's own reason, which no other line of a one-tree run
        # carries: one line per machine, the two builds being two.
        self.assertEqual(len(re.findall(
            r"The file of the machine that runs \S+ holds none, because "
            r"the file was written by an --extract that did not pack",
            step2)), 2, step2)
        summary = out.split('AUDIT SUMMARY', 1)[1]
        reindex = section_body(summary, REINDEX)
        self.assertEqual(summary_block(out, REINDEX)[:2],
                         nothing_compared_lines('builds'))
        warnings = summary_warnings(out)
        self.assertIn('their character sets were not compared', warnings)
        # Beside step 11's verdict too (2.1): step 2's state for its
        # charmaps is "not read", and the line gives each file's reason.
        got, alone = rules_beside(out)
        self.assertEqual(got, {})
        self.assertEqual(len(alone), 2, alone)
        self.assertEqual(len(re.findall(
            r"The file of the machine that runs \S+ holds none, because "
            r"the file was written by an --extract that did not pack",
            flat(alone[1]))), 2, alone)
        self.assertTrue(flat(alone[1]).startswith(
            'Character sets (step 2): not compared. '), alone)
        for text in (compared, step2, reindex, warnings, flat(' '.join(
                alone))):
            with self.subTest(text=text[:40]):
                self.assertNotIn('the same locale sources and charmaps',
                                 text)

    def test_two_machines_of_one_build(self):
        out = self.out('one_build')
        summary = out.split('AUDIT SUMMARY', 1)[1]
        self.assertEqual(summary_block(out, REINDEX)[:2],
                         nothing_compared_lines('builds'))
        self.assertEqual(summary_warnings(out).count('are the same commit'),
                         1, out)
        assert_no_deleted_block(self, out)
        self.assertIn('!! Both machines run the same glibc build. This '
                      'compares two machines, not an upgrade.', flat(summary))

    def test_each_route_writes_what_became_of_the_character_sets(self):
        """2.1: a closed state with its reason on every route: the check
        did not run (a file without charmaps), one tree whose charmaps are
        read once, one tree whose charmaps no file holds (each file's
        reason), one commit."""
        state = {label: charmaps_state(self.runs[label][0])
                 for label in self.runs}
        self.assertEqual(state['link']['state'], 'not run')
        self.assertRegex(state['link']['why'], r'^The file of the machine '
                         r'that runs \S+ holds no charmaps this reads, '
                         r'because ')
        self.assertEqual(state['same_sources'],
                         {'state': 'one tree', 'why': None})
        self.assertEqual(state['one_build'],
                         {'state': 'one commit', 'why': None})
        self.assertEqual(state['same_no_charmaps']['state'], 'not read')
        self.assertEqual(len(state['same_no_charmaps']['why']), 2)

    def test_the_names_step_2_could_not_compare_come_apart(self):
        """13.31: the two links step 2 did not read are listed apart,
        under the line that says why, and the rest above it; the count is
        the whole list's."""
        out = self.out('link')
        body = summary_block(out, REINDEX)
        sub = ("Listed because step 2 could not compare them (each one's "
               "reason is in the warnings below):")
        at = [i for i in range(len(body) - 1)
              if flat(body[i] + ' ' + body[i + 1]) == sub]
        self.assertEqual(len(at), 1, body)
        above, below = body[:at[0]], body[at[0] + 2:]
        apart = below[:below.index(next(x for x in below
                                        if x.startswith('(')))]
        self.assertEqual(apart, ['de_IT', 'zz_LINK'])
        self.assertTrue(above, body)
        self.assertFalse({'de_IT', 'zz_LINK'} & set(above), body)
        self.assertRegex(next(x for x in below if x.startswith('(')),
                         r'^\(%d name\(s\); full list: ' %
                         (len(above) + len(apart)))

    def test_a_file_without_charmaps(self):
        out = self.out('link')
        step2 = flat(self.step(out, 2))
        self.assertIn('Rules differ', step2)
        self.assertRegex(step2, r'NOT RUN: the comparison of .*\. The file '
                                r'of the machine that runs \S+ holds no '
                                r'charmaps this reads, because ')
        self.assertIn('charmaps none, because', flat(compared_text(out)))
        # Beside step 11's verdict too, once, with step 2's reason.
        got, alone = rules_beside(out)
        self.assertEqual(alone[0], lom.RULES_LEAD)
        self.assertEqual(len(alone), 2)
        self.assertRegex(flat(alone[1]), r'^Character sets \(step 2\): not '
                         r'compared\. The file of the machine that runs \S+ '
                         r'holds no charmaps this reads, because ')


class NodeFileWithoutSources:
    """One side's file holds no locale sources. Both sides read their tags
    (glibc_locale_data.machine_sides), the first warning names that side's
    file and why, and Removed points to it instead of telling the reader to
    pass options the files replace.

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

    def test_only_the_measured_order_ran(self):
        self.assertEqual(banners(self.out), {'MEASURED ORDER': 1})
        self.assertEqual(re.findall(r'^== STEP (\d+)', self.out, re.M),
                         ['2', '11'])
        self.assertNotIn('== STEP', before_compared(self.out))

    def test_removed_points_to_the_warning_that_says_which_file_and_why(self):
        self.assertEqual(
            section_body(self.summary, REMOVED),
            f"{NOT_CHECKED} {MACHINE_FILE_MADE_TAGS} Between the tags: none "
            f"-- no locale file at {OLD} is gone at {MID}")
        first = first_warning(self.summary)
        other = 'new' if self.BARE == 'old' else 'old'
        self.assertIn(unread_side_text(self.BARE), first)
        self.assertNotIn(f"the {other} machine's file names", first)


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
    or 'link' (a folder CharmapsDir refuses to read).

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
        cls.rc, cls.out = run_wrapper(OLD, MID, '--old-node', cls.old,
                                      '--new-node', cls.new,
                                      out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_run_ends_cleanly(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])


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

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_only_the_measured_order_ran(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertEqual(banners(self.out), {'MEASURED ORDER': 1})

    def test_no_rules_beside_each_locale_from_the_tags(self):
        """Both sides read their tags because of the files: no locale gets
        the tags' rules as if they were the machines'."""
        self.assertEqual(rules_beside(self.out), ({}, [RULES_OF_THE_TAGS]))

    def test_the_tags_warning_asks_for_nothing(self):
        """The files were given and hold no sources: the `!!` for the two
        tags says what a tag does not hold and asks for nothing, never to
        name each side as its build, which with these files is refused
        (TAG_ASK none; false-negative-reviewer, 11.1)."""
        summary = flat(self.summary)
        self.assertIn(TAGS_FACT, summary)
        self.assertNotIn('name each side as its build', flat(self.out))

    def test_step_2s_list_says_where_the_measured_answer_is(self):
        """Step 2's list, from the tags, comes first; the block that lists
        what each machine's glibc says is further down. Without a pointer
        the list reads as the whole answer. It names the tags, which both
        sides read here, never the machines' own sources."""
        body = summary_block(self.out, REINDEX)
        self.assertEqual(body[-2:], REINDEX_POINTER)
        self.assertEqual(len(body), 5 + 1 + 5 + 2)
        self.assertNotIn("each machine's own locale sources", ' '.join(body))

    def test_the_C_UTF_8_warning_names_step_11_and_the_probe(self):
        """The tags hold no file C, so step 2 did not compare C.UTF-8; step
        11 measured each machine, and the `!!` says so beside the probe,
        once in the summary (16.210)."""
        summary = flat(self.summary)
        self.assertEqual(summary.count(
            f"!! {OLD} and {MID} hold no localedata/locales/C, the file "
            f"C.UTF-8 is built from, and a distro's build can ship its own, "
            f"so C.UTF-8's sort rules were not compared. "
            f"{step2.C_NO_VERSION_STEP11}"), 1, summary)
        self.assertNotIn(step2.C_NO_VERSION, summary)


    def test_removed_points_to_the_reason(self):
        """Both sides read their tags because of the files: Removed says
        so and points to the warning that names each file and its reason,
        the first one, which is repeated from the run's opening lines."""
        self.assertEqual(
            section_body(self.summary, REMOVED),
            f"{NOT_CHECKED} {MACHINE_FILE_MADE_TAGS} Between the tags: none "
            f"-- no locale file at {OLD} is gone at {MID}")
        first = first_warning(self.summary)
        for side in ('old', 'new'):
            self.assertIn(unread_side_text(side), first)
        self.assertNotIn('Pass --', flat(self.summary))


@needs_clone
class WrapperOneTagFromTheFiles(unittest.TestCase):
    """Two machines of one RHEL 9 build whose files hold no sources, given
    as its tag on both sides: both sides read that one tag, so nothing is
    compared. The one-commit block must not send the reader to the files
    already given; it points to the warning that says why they were not
    read as the build (false-negative-reviewer, 9.4b)."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-one-tag-')
        old = node_file('one-old.tar', cut_few(RHEL9), MID, sources=False)
        new = node_file('one-new.tar', cut_to_pair(RHEL9), MID, sources=False)
        cls.rc, cls.out = run_wrapper(MID, MID, '--old-node', old,
                                      '--new-node', new, out_dir=cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_the_one_commit_block_points_to_why(self):
        """The warnings say both: why the files were not read as their
        builds (the first one, step0's), and that the two tags are one
        commit, with what can still show a change."""
        self.assertEqual(self.rc, 0, self.out[-2000:])
        summary = self.out.split('AUDIT SUMMARY', 1)[1]
        self.assertEqual(summary_block(self.out, REINDEX)[:2],
                         nothing_compared_lines('tags'))
        self.assertIn(unread_side_text('old'), first_warning(summary))
        self.assertEqual(summary_warnings(self.out).count(flat(
            f"!! {MID} and {MID} are the same commit. Step 2 compares "
            f"nothing: it would compare upstream source against itself. "
            f"{g._NOTHING_COMPARED_REST_STEP11.format(On='The tags')}")), 1,
            self.out)
        # Step 11 ran: the files were given, and nothing asks for them.
        self.assertNotIn('With each machine\'s file (--old-node',
                         flat(self.out))
        assert_no_deleted_block(self, self.out)

    def test_no_rules_beside_each_locale_of_one_commit(self):
        self.assertEqual(rules_beside(self.out), ({}, [
            'Rules (step 2) are not set beside each locale: step 2 compared '
            'nothing, the two tags are one commit.']))


@needs_clone
class WrapperNodeFileRefused(unittest.TestCase):
    """A node file that cannot be trusted, or node files given the wrong way,
    stop the run before the sides are read ('What was compared'): no step,
    no summary, and the message names the reason."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-node-bad-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.old = node_file('old.tar', cut_few(RHEL8), OLD, sources=False)
        self.new = node_file('new.tar', cut_few(RHEL9), MID, sources=False)

    def assertRefusedBeforeTheSides(self, *args, tags=(OLD, MID)):
        rc, out = run_wrapper(*tags, *args, out_dir=self.out_dir)
        self.assertEqual(rc, 2, out[-2000:])
        self.assertNotIn(COMPARED_BANNER, out)
        self.assertNotIn('Tag provenance', out)
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
                              self.assertRefusedBeforeTheSides(*args))

    def test_an_old_file_cut_short(self):
        cut = self.copy_of(self.old, cut=100000)
        out = self.assertRefusedBeforeTheSides('--old-node', cut,
                                            '--new-node', self.new)
        self.assertIn(f'{cut} is not a whole file written by --extract', out)
        self.assertIn("the old node's file could not be used", out)

    def test_text_printed_before_the_new_file(self):
        banner = self.copy_of(self.new, prefix=b'Welcome to el9\n')
        out = self.assertRefusedBeforeTheSides('--old-node', self.old,
                                            '--new-node', banner)
        self.assertIn('such as a login message', out)
        self.assertIn("the new node's file could not be used", out)

    def test_the_same_file_twice(self):
        """Step 11 refuses, and the characters file and SQL an earlier run
        left are gone: a refused run leaves nothing that reads as its
        list."""
        stale = os.path.join(self.out_dir, 'measured_order_characters.'
                             + pair_slug(OLD, OLD) + '.tsv')
        sql = os.path.join(self.out_dir, 'measured_order_confirm.'
                           + pair_slug(OLD, OLD) + '.UTF8.sql')
        for path in (stale, sql):
            with open(path, 'w', encoding='utf-8') as f:
                f.write('left by an earlier run\n')
        out = self.assertRefusedBeforeTheSides('--old-node', self.old,
                                            '--new-node', self.old,
                                            tags=(OLD, OLD))
        self.assertIn('the two files hold the same measurement', out)
        self.assertIn(f'{COULD_NOT_COMPARE_NODES} {self.old} and {self.old}',
                      out)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(sql))

    def test_the_files_the_wrong_way_round(self):
        out = self.assertRefusedBeforeTheSides('--old-node', self.new,
                                            '--new-node', self.old)
        self.assertIn('pass the machine you upgrade FROM first', out)

    def test_a_second_run_lays_the_files_out_again(self):
        """The folders a run lays the files out in are its own: a second run
        in the same output folder removes them first, like every other file
        this run reads, instead of reading the first run's.

        The two runs are given different files on both sides, so a second run
        that read the first one's folders would print the first one's answer.
        """
        first = self.assertRefusedBeforeTheSides('--old-node', self.new,
                                              '--new-node', self.old)
        self.assertIn('pass the machine you upgrade FROM first', first)
        rhel10 = node_file('rhel10.tar', cut_few(RHEL10), NEW, sources=False)
        second = self.assertRefusedBeforeTheSides('--old-node', self.old,
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
        out = self.assertRefusedBeforeTheSides('--old-node', self.new,
                                            '--new-node', rhel10)
        self.assertIn("the old side given is glibc-2.28: give that build's "
                      "tag, glibc-2.34", out)


@needs_clone
class WrapperWarningsKeepEachBlockOnce(unittest.TestCase):
    """16.168: the summary repeats each `!!` block once, by the whole
    block: two identical blocks are one warning, and two that open with the
    same line and go on otherwise are two (a key on the `!!` line alone
    would eat the second). No step prints such blocks on its own, so step
    2's are injected by a sitecustomize on PYTHONPATH."""

    @classmethod
    def setUpClass(cls):
        cls.out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-dedup-')
        hook = os.path.join(cls.out_dir, 'hook')
        os.makedirs(hook)
        with open(os.path.join(hook, 'sitecustomize.py'), 'w',
                  encoding='utf-8') as fh:
            fh.write("import sys\n"
                     "if sys.argv[0].endswith(\n"
                     "        'filter_lc_collate_changes.py'):\n"
                     "    for tail in ('tail A.', 'tail A.', 'tail B.'):\n"
                     "        print('!! Injected: one opening line.')\n"
                     "        print('   ' + tail)\n"
                     "        print()\n")
        cls.rc, cls.out = run_wrapper(NEW, NEW, out_dir=cls.out_dir,
                                      env_extra={'PYTHONPATH': hook})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out_dir, ignore_errors=True)

    def test_identical_blocks_once_and_different_blocks_each(self):
        self.assertEqual(self.rc, 0, self.out[-2000:])
        self.assertEqual(step_text(self.out, 2).count(
            '!! Injected: one opening line.'), 3)
        warnings = summary_warnings(self.out)
        self.assertEqual(warnings.count(
            '!! Injected: one opening line. tail A.'), 1, warnings)
        self.assertEqual(warnings.count(
            '!! Injected: one opening line. tail B.'), 1, warnings)


# What audit.sh says when a run stops after step 11 compared the two
# measurements and before its report was printed (16.144).
STEP11_NOT_PRINTED = ('note: step 11 compared the two measurements before '
                      'the run stopped;')


@needs_clone
class WrapperNamesStep11sReportWhenALaterStepStops(unittest.TestCase):
    """16.144: step 11 is worked out first and printed after 'What was
    compared' and step 2. When either of those stops the run, the exit code
    stays theirs, no summary is printed, and the run names step 11's
    report, which it had written. Each stop injected by a sitecustomize:
    the sides refusing (a first clone with no network, say), step 2
    failing. The machines' files hold no sources, so the pair is the two
    tags and the report is named for them."""

    def run_stopped(self, where):
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-wrapper-stop11-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        hook = os.path.join(out_dir, 'hook')
        os.makedirs(hook)
        with open(os.path.join(hook, 'sitecustomize.py'), 'w',
                  encoding='utf-8') as fh:
            fh.write("import sys\n"
                     f"if {where}:\n"
                     "    sys.exit('injected: this step stops the run')\n")
        rc, out = run_wrapper(
            OLD, MID,
            '--old-node', node_file('old.tar', cut_few(RHEL8), OLD,
                                    sources=False),
            '--new-node', node_file('new.tar', cut_few(RHEL9), MID,
                                    sources=False),
            out_dir=out_dir, env_extra={'PYTHONPATH': hook})
        return out_dir, rc, out

    def test_each_stop_names_the_report(self):
        for label, where in (
                ('sides', "sys.argv[0].endswith('glibc_locale_data.py') "
                          "and sys.argv[1:2] == ['sides']"),
                ('step 2', "sys.argv[0].endswith("
                           "'filter_lc_collate_changes.py')")):
            with self.subTest(stop=label):
                out_dir, rc, out = self.run_stopped(where)
                self.assertNotEqual(rc, 0, out[-2000:])
                self.assertIn('injected: this step stops the run', out)
                self.assertNotIn('AUDIT SUMMARY', out)
                self.assertNotIn('== STEP 11', out)
                log = os.path.join(out_dir, 'measured_order.%s.log'
                                   % pair_slug(OLD, MID))
                self.assertTrue(os.path.getsize(log), out[-2000:])
                self.assertIn(STEP11_NOT_PRINTED + ' its report, not '
                              'printed above, is in ' + log, flat(out))


if __name__ == '__main__':
    unittest.main()
