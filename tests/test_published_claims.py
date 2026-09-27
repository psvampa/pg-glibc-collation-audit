"""The numbers and quotes the documentation publishes.

Two documentation-correction passes in one day, on 2026-09-06, found the same
class of defect each time: a count, a position or a quoted output line that no
longer matched what the tool or the measurement actually produced. Re-reading
caught them; re-reading is not a control.

This is the checkable half of that, turned into a test. It reads the published
files and asserts they agree with each other and with the scripts. It cannot
check prose, and it deliberately does not try: what it covers is exactly the
part where a human re-read is wasted effort.

No glibc clone needed -- everything here is in the repository. It was
switched off from 2026-09-23 to 2026-09-27 while the documentation was
rewritten (backlog 12.1).

The holes false-negative-reviewer measured in it on 2026-09-23 are closed
(backlog 12.2). The path test now also reads paths to examples/, sql/ and
scripts/, since 2026-09-27. The rest were in tests removed that day, together
with the page or the quote each one read. breakage/ stays outside every check
by Pablo's decision.
"""

import collections
import os
import re
import subprocess
import unittest

import _harness
from _harness import REPO_ROOT, flat

import glibc_locale_data as g

PROBE = os.path.join(REPO_ROOT, 'sql', 'c_utf8_probe.sql')
EXAMPLE_8_9 = os.path.join(REPO_ROOT, 'examples',
                           'c-utf8-probe-rhel8-vs-rhel9.txt')
EXAMPLE_9_10 = os.path.join(REPO_ROOT, 'examples',
                            'c-utf8-probe-rhel9-vs-rhel10.txt')
BUILDS = ('glibc-2.28-251.el8_10.40', 'glibc-2.34-275.el9_8',
          'glibc-2.39-128.el10_2')


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def echoed_block(wrapper, heading):
    r"""The heading's echo and the contiguous run of echoes under it, as
    printed, so a doc that quotes the block can be compared line for line.

    Ends at the `fi` that closes the block, and RAISES on any other line it
    cannot read as a printed one. Stopping there quietly was this helper's own
    false negative, measured by false-negative-reviewer on 2026-09-21: eight
    line shapes -- a bare `echo`, `echo "..." >&2`, single quotes, a guarded
    echo, a `\` continuation, a here-doc, `printf`, a comment -- all ended the
    walk, so appending `echo` and "Your node's C.UTF-8 is therefore cleared."
    to the block left the quote test green while the wrapper printed the extra
    sentence. A short block and a block that could not be read whole are
    different facts. Raises for the same reason when the heading is gone.
    """
    lines = wrapper.split('\n')
    start = next((i for i, line in enumerate(lines)
                  if re.match(r'^\s*echo "' + re.escape(heading) + r'"$',
                              line)), None)
    if start is None:
        raise AssertionError(f'audit.sh echoes no {heading!r}')
    out = []
    for line in lines[start:]:
        if re.match(r'^\s*fi\b', line):
            return out
        m = re.match(r'^\s*echo "(.*)"$', line)
        if not m:
            raise AssertionError(
                f'unquotable line inside the {heading!r} block: {line!r}')
        # The shell's source spelling is the printed text only while the line
        # has no expansion in it. `echo "$OLD -> $NEW"` would be compared to a
        # doc as the two variable NAMES and agree with a doc that quotes them,
        # which is a tie to something no reader ever sees.
        if re.search(r'[$`\\]', m.group(1)):
            raise AssertionError(
                f'line expands before it is printed, so its source is not '
                f'what the reader sees: {line!r}')
        out.append(m.group(1))
    raise AssertionError(f'the {heading!r} block is not closed by fi')


Evidence = collections.namedtuple('Evidence', 'path shape section',
                                  defaults=('{n}', None))
Evidence.__doc__ = """One file that has to carry a figure, and where to look.

`shape` is the sentence the number appears in, with `{n}` where the number
goes; the default checks for the bare number, which is all a figure needs when
no other fact in the file is spelled the same way.

`section` is text that opens the region to search, and the region ends at the
next `====` banner. How much that narrows is a property of the transcript, not
a promise: in the half these rows read -- the five scripts run by hand, which
carries none of the `STEP n` banners `audit.sh` prints -- the first `====`
after step 4 is an editorial note, so the region is lines 90 to 496, 407 of
them, most belonging to step 5. Measured 2026-09-22. What the narrowing buys
is the thing it was added for: keeping a tag's figures apart from the figures
a node built from that tag reprints word for word, lower down, under a later
banner.

A `section` is only ever a narrowing of a NAMED sentence, never of a bare
number, and the combination is refused. A bare number takes whatever digits
the region holds, and it does not need a wide region to go wrong: the
measurement that bought this rule drifted `docs/method.md` from 331 to 327 and
stayed GREEN on `via iso14651_t1: 327 locale(s)`, the line directly below the
sentence that should have been read -- inside step 4's own output, so cutting
at the end of step 4 would not have saved it. Both fields are strings, so the
call sites pass them by keyword; what actually catches a swap is the `{n}`
guard.
"""


def evidence_pattern(shape, bare):
    """The regex an evidence shape becomes for one number.

    Shared so that the class which proves a tie discriminates uses the SAME
    expression as the class that relies on it. Two copies would let the proof
    pass over an expression nothing runs.
    """
    number = rf'(?<!\d){bare}(?!\d)'
    return number.join(re.escape(part) for part in shape.split('{n}'))


def evidence_agrees(shape, bare, text):
    """Does `text` state this figure the way a row with this shape requires?

    The rule in one place, because the class that proves a tie discriminates
    has to apply the SAME rule as the check that relies on it. A named
    sentence must appear exactly once; the bare number needs only to be there,
    since a transcript may state a figure twice for good reason.
    """
    found = re.findall(evidence_pattern(shape, bare), text)
    return bool(found) if shape == '{n}' else len(found) == 1


def region_after(text, section):
    """`text` from `section` to the next banner, or a loud failure.

    It refuses here rather than trusting a caller to have checked: one of its
    two callers checks first and the other does not, and an anchor appearing
    twice would otherwise return the span BETWEEN the copies -- narrower, and
    green for the wrong reason.
    """
    parts = text.split(section)
    if len(parts) != 2:
        raise ValueError(f'the section anchor {section!r} appears '
                         f'{len(parts) - 1} time(s), once expected')
    if '\n====' not in parts[1]:
        raise ValueError(f'nothing closes the section under {section!r}, so '
                         f'cutting it would read to the end of the file')
    return parts[1].split('\n====')[0]


def docs(include_changelog=False):
    """{relative path: text} for every published .md, CHANGELOG excluded.

    The CHANGELOG records what the docs used to say, so a phrase this suite
    forbids elsewhere is correct there. Its LINKS still have to resolve, which
    is what include_changelog is for.
    """
    out = {}
    for root, dirs, files in os.walk(REPO_ROOT):
        # .claude/ is gitignored whole (PR #18): the private working rules
        # under it are not published documentation, and asserting them as
        # such made every commit fail on files the repository does not carry.
        #
        # breakage/ is evidence gathered for an article rather than part of
        # this tool, and nothing else in the repository may reference it.
        dirs[:] = [d for d in dirs
                   if d not in ('.git', 'glibc', '.claude', 'breakage')]
        for name in files:
            if not name.endswith('.md'):
                continue
            if name == 'CHANGELOG.md' and not include_changelog:
                continue
            path = os.path.join(root, name)
            out[os.path.relpath(path, REPO_ROOT)] = read(path)
    return out


def ordering_rows(text):
    """[(position, code point, label)] from a probe output's query 1 table."""
    # The probe's own section header. It was '(query 1)' until the two
    # probe examples were recaptured as plain output: that string was in
    # an editorial heading written above the run, not in anything psql
    # prints.
    block = text.split('=== 1. the order')[1].split('(41 rows)')[0]
    return [(int(m.group(1)), m.group(2), m.group(3).strip())
            for m in re.finditer(r'^\s*(\d+) \| (U\+[0-9A-F]+)\s*\| (.+)$',
                                 block, re.M)]


class TheProbeDescribesTheFileItProbes(unittest.TestCase):
    """The defect that started this: the probe's header claimed the backported
    C declares one ellipsis range per plane, and labelled its corpus rows
    accordingly. The real file declares six. Both the header and the labels
    were wrong, and so were two published outputs generated from them.

    The test fixture is that file, copied off collaudit8. So the probe's own
    description of it can be checked against it, which is what would have
    caught this the first time.
    """

    def setUp(self):
        self.probe = read(PROBE)
        self.real = _harness.backported_c()

    def test_the_probe_lists_exactly_the_ranges_the_real_file_declares(self):
        real_ranges = re.findall(r'<U([0-9A-F]+)>',
                                 g.collate_text(self.real) or '')
        # The header quotes them as `<U0000>..<UFFFF>` pairs, one per line.
        quoted = re.findall(r'<U([0-9A-F]+)>\.\.<U([0-9A-F]+)>', self.probe)
        self.assertEqual(len(quoted), 6,
                         'the probe header must quote six declared ranges')
        flat = [cp for pair in quoted for cp in pair]
        self.assertEqual([cp.lstrip('0') or '0' for cp in flat],
                         [cp.lstrip('0') or '0' for cp in real_ranges],
                         'the probe header and the real file disagree')

    def test_the_probe_says_six_not_one_per_plane(self):
        self.assertIn('SIX ranges, not one per plane', self.probe)
        self.assertNotIn('one range per plane', self.probe)

    def test_no_corpus_label_names_a_range_that_does_not_exist(self):
        """`range 4 start` was a label for a plane the file declares no range
        for. There is no range 4, and there never was."""
        self.assertNotRegex(self.probe, r"'range \d+ (start|end)")

    def test_every_no_range_label_is_a_plane_the_file_really_omits(self):
        declared = {int(a, 16) >> 16 for a, _ in
                    re.findall(r'<U([0-9A-F]+)>\.\.<U([0-9A-F]+)>', self.probe)}
        for m in re.finditer(r"'plane (\d+) (?:first|last) \(NO range\)'",
                             self.probe):
            plane = int(m.group(1))
            self.assertNotIn(plane, declared,
                             f'plane {plane} is labelled NO range but the '
                             f'header declares a range for it')
        for m in re.finditer(r"'plane (\d+) (?:first|last) \(declared\)'",
                             self.probe):
            self.assertIn(int(m.group(1)), declared)

    def test_the_corpus_arithmetic_the_header_states_adds_up(self):
        """11 declared endpoints + 22 undeclared-plane values + 5 UTF-8
        boundaries + 3 ASCII anchors = 41. It is 11 and not 12 because U+0000
        cannot be stored in a PostgreSQL text column, which the header now
        says out loud."""
        labels = re.findall(r"\('U\+[0-9A-F]+',\s*'([^']+)'", self.probe)
        self.assertEqual(len(labels), 41)
        self.assertEqual(sum(1 for l in labels if '(declared)' in l), 11)
        self.assertEqual(sum(1 for l in labels if '(NO range)' in l), 22)
        self.assertEqual(sum(1 for l in labels if l.startswith('UTF-8')), 5)
        self.assertEqual(sum(1 for l in labels if l.startswith('ASCII')), 3)
        self.assertIn('U+0000', self.probe)

    def test_the_row_count_assertion_matches_the_rows(self):
        stated = int(re.search(r'IF n <> (\d+) THEN', self.probe).group(1))
        self.assertEqual(stated, 41)


class ThePublishedOutputsMatchTheirProse(unittest.TestCase):
    """"the FIRST code point of each declared range sorts last (38-41)" was
    false on the table three lines below it. Every positional claim the docs
    make about that table is checked here against the table."""

    def setUp(self):
        self.rows = ordering_rows(read(EXAMPLE_8_9))
        self.by_cp = {cp: pos for pos, cp, _ in self.rows}

    def test_both_examples_carry_41_rows(self):
        for path in (EXAMPLE_8_9, EXAMPLE_9_10):
            with self.subTest(path=os.path.basename(path)):
                self.assertEqual(len(ordering_rows(read(path))), 41)

    def test_ascii_is_where_every_doc_says_it_is(self):
        position = self.by_cp['U+0041']
        self.assertEqual(position, 31)
        for name, text in docs().items():
            if 'ASCII' in text and 'position' in text:
                for m in re.finditer(r'ASCII (?:sorts|lands) at position (\d+)',
                                     text):
                    self.assertEqual(int(m.group(1)), position,
                                     f'{name} states the wrong position')

    def test_the_documented_number_of_moved_positions_matches_the_output(self):
        differing = int(re.search(r'\|\s*(\d+)\s*$',
                                  read(EXAMPLE_8_9).split(
                                      'positions_differing')[1]
                                  .split('(1 row)')[0].strip(), re.M).group(1))
        self.assertEqual(differing, 40)
        for name, text in docs().items():
            for m in re.finditer(r'(\d+) of (?:the )?41', text):
                self.assertEqual(int(m.group(1)), differing,
                                 f'{name} states the wrong count')

    def test_the_rhel9_output_really_is_code_point_order(self):
        """The order the probe printed, read as numbers and sorted.

        It used to check a sentence as well -- the file claimed "This is code
        point order, exactly" -- which went when the examples were recaptured
        as plain output. The probe answers the same question itself, in query
        2, and that answer is machine-readable rather than prose: this now
        asserts both halves of it."""
        text = read(EXAMPLE_9_10)
        rows = ordering_rows(text)
        cps = [int(cp[2:], 16) for _, cp, _ in rows]
        self.assertEqual(cps, sorted(cps))
        query_2 = text.split('=== 2. does C.utf8 equal byte order?')[1]
        verdict = query_2.split('(1 row)')[0].strip().split('\n')[-1]
        self.assertRegex(verdict, r'^\s*t\s*\|\s*0\s*$',
                         'query 2 no longer says the order equals byte order '
                         'with nothing out of position')

    def test_the_examples_name_the_builds_they_were_measured_on(self):
        for path, expected in ((EXAMPLE_8_9, BUILDS[:2]),
                               (EXAMPLE_9_10, BUILDS[1:])):
            text = read(path)
            for build in expected:
                self.assertIn(build, text, os.path.basename(path))


class TheDocsQuoteWhatTheToolsPrint(unittest.TestCase):
    """A quoted output block goes stale silently: the tool changes and the
    quote keeps reading as current."""

    def test_no_doc_states_a_test_count(self):
        """It went stale twice in one day, so it was removed rather than
        corrected. A number that is not written down cannot rot."""
        for name, text in docs().items():
            self.assertNotRegex(
                text, r'\b\d{2,4} tests\b',
                f'{name} states a test count; tests/README.md deliberately '
                f'does not, because it went stale twice on 2026-09-06')

    def test_no_doc_times_the_suite(self):
        """The same rot, one column over, and it had already happened twice:
        a `~16s`/`~17s` disagreement between two files (quoted in
        TheRepairDocumentQuotesWhatIsPublished below), and "about a minute and
        a half" in three files while the suite took 208 s on the machine that
        measured it on 2026-09-21 (thirty-first entry). A runtime is a fact
        about someone else's machine, so it is the reader's to measure.

        The unit is the WHOLE FILE that publishes the command, after two
        narrower scopes were measured full of holes by
        false-negative-reviewer. Three lines around the command missed a
        runtime eight lines under the block. The Markdown section then missed
        a `#` comment line after the last command (split off as a heading), a
        figure in backticks, and a `### How long` sub-heading right under the
        block -- the heading somebody actually writes. Each fix was the form
        the author had seen rather than the family (ninth, twenty-third), so
        the scope is now the file: the three files that publish this command
        publish no duration at all, measured, and the fourth escape route was
        closed by deleting the exemption rather than by widening it again.

        The cost is that a duration about something else in one of those
        three files fires too. That is deliberate and the message says what
        to do about it: those pages are short, and the runner's own header is
        where a measurement belongs, with its date and its machine.
        """
        # Spelled-out durations, and the "and a half" tail, are in here
        # because the pattern was written without them and measured: "about
        # two minutes" passed, and so did "two and a half minutes" while
        # "an hour and a half" was caught. The figure that started all this
        # was spelled out too.
        # A digit may sit against its unit (`47s`); a number in words needs
        # whitespace and a spelled-out unit, or `a s` is found inside "as".
        digits = r'\d+(?:\.\d+)?'
        words = (r'an?|one|two|three|four|five|six|seven|eight|nine|ten|'
                 r'a few|a couple of|several|half a')
        half = r'(?:\s+and\s+a\s+half)?'
        duration = re.compile(
            r'(?i)\b(?:%s)%s\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hours?)\b'
            r'|\b(?:%s)%s\s+(?:secs?|seconds?|mins?|minutes?|hours?)\b'
            r'|\b(?:%s)\s+and\s+a\s+half\b'
            % (digits, half, words, half, words))
        command = re.compile(r'unittest discover -s tests|run_parallel\.py')
        for name, text in docs().items():
            if not command.search(text):
                continue
            for number_of, line in enumerate(text.split('\n'), start=1):
                found = duration.search(line)
                if found is not None:
                    self.fail(
                        f'{name}:{number_of} states a duration '
                        f'({found.group(0)!r} in {line.strip()!r}) in a file '
                        f'that publishes the test-suite command, so a reader '
                        f'will take it for the suite\'s. Drop the figure, or '
                        f'move the sentence to a page that does not publish '
                        f'the command; a measurement belongs in '
                        f'tests/run_parallel.py\'s header, with its date and '
                        f'its machine')

    def test_the_layer_counts_come_from_the_files(self):
        """Which layers need the glibc clone is stated twice in tests/README.md
        -- the Layers table's yes/no column, and the paragraph under it that
        names the layers that still run without one -- and once more in the CI
        comment. All three are hand-written statements of a fact the
        filesystem already holds.

        Written the day the eighth layer became the ninth, which moved a count
        in one file, added a row in a second and a filename list in a third --
        by hand, with nothing to catch the fourth place. The CI comment was the
        one that had already gone stale. Until 2026-09-27 the count was read
        from docs/requirements.md, which then stopped stating it; tests/README.md
        is the page that describes the layers.

        A layer needs the clone exactly when its source asks for one of the
        two skip decorators.
        """
        tests_dir = os.path.join(REPO_ROOT, 'tests')
        modules = sorted(name for name in os.listdir(tests_dir)
                         if name.startswith('test_') and name.endswith('.py'))
        self.assertGreater(len(modules), 1, 'no test modules found at all')
        # Spelled in two pieces on purpose. A pattern written whole appears
        # in this file's own source, so the first two versions of this test
        # counted test_published_claims.py among the layers that need a
        # clone -- a probe that matches itself, in the layer whose job is to
        # notice exactly that.
        applied = '@' + 'needs_'
        gated = [name for name in modules
                 if applied in read(os.path.join(tests_dir, name))]
        free = [name for name in modules if name not in gated]

        # The ROWS, not the file: asserting that each filename appears
        # somewhere in tests/README.md passed with the row deleted, because
        # the paragraph under the table names three of the modules too.
        # Measured with a mutant that renamed a row.
        table = docs()[os.path.join('tests', 'README.md')]
        rows = dict(re.findall(r'(?m)^\| `(test_\w+\.py)` \| (\w+) \|', table))
        self.assertEqual(set(rows), set(modules),
                         'the Layers table in tests/README.md and tests/ do '
                         'not hold the same modules')
        for name in modules:
            with self.subTest(layer=name):
                said = rows[name]
                self.assertIn(said, ('yes', 'mostly', 'no'),
                              f'{name}: the table says {said!r}')
                self.assertEqual(said != 'no', name in gated,
                                 f'{name}: the table says {said!r} about the '
                                 f'clone, and the module '
                                 f'{"asks" if name in gated else "does not ask"}'
                                 f' for a skip decorator')

        paragraph = re.search(r'Without a clone at `scripts/glibc`.*?(?=\n## )',
                              table, re.S)
        self.assertIsNotNone(
            paragraph, 'tests/README.md no longer says which layers run '
                       'without a clone in the shape this test reads; it is '
                       'the sentence that goes stale, so it cannot be left '
                       'unasserted')
        # Both directions: a layer left out, and a layer named that needs the
        # clone and so skips. Checking only the first let the paragraph tell
        # the reader test_wrapper.py runs without one (false-negative-reviewer,
        # 2026-09-27). The modules marked "mostly" are named too, for the
        # classes of theirs that fabricate a repository.
        named = set(re.findall(r'`(test_\w+\.py)`', paragraph.group(0)))
        partly = {name for name in modules if rows[name] == 'mostly'}
        self.assertEqual(named, set(free) | partly,
                         'the layers tests/README.md says still run without a '
                         'clone are not the ones that do')
        workflow = read(os.path.join(REPO_ROOT, '.github', 'workflows',
                                     'tests.yml'))
        for name in free:
            self.assertIn(name[:-3], workflow,
                          f'the CI comment does not name {name} among the '
                          f'clone-free layers a broken clone step would '
                          f'leave running alone')

    def test_every_tracked_module_compiles_without_warning(self):
        """Compiling every tracked .py with warnings as errors.

        A `SyntaxWarning` printed above the suite is noise, and noise is what
        this project refuses to emit anywhere else: the reader stops reading
        the line that matters. One lived in `echoed_block`'s docstring (a
        backslash that is not a valid escape) and printed on every CI run and
        on every run after the file was touched -- not on every run, because
        CPython warns at compile time and a warm `__pycache__` skips it, which
        is what made it survive so long.

        Compiled rather than imported: importing runs module-level code and
        would make this a slow, side-effecting test of something that is a
        property of the source text (thirty-third entry).
        """
        import warnings
        tracked = subprocess.run(['git', 'ls-files', '*.py'], cwd=REPO_ROOT,
                                 capture_output=True, text=True)
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        names = [n for n in tracked.stdout.split('\n') if n]
        self.assertGreater(len(names), 5,
                           'git ls-files returned almost nothing; this test '
                           'would then pass by looking at no file at all')
        for name in names:
            with self.subTest(module=name):
                with warnings.catch_warnings():
                    warnings.simplefilter('error', SyntaxWarning)
                    try:
                        compile(read(os.path.join(REPO_ROOT, name)), name,
                                'exec')
                    except SyntaxWarning as warned:
                        self.fail(f'{name} compiles with a warning printed '
                                  f'above every CI run: {warned}')

    def test_every_measured_build_is_cited_on_the_results_page(self):
        """A measurement is bound to the build it ran on, so a result whose
        build is not stated where the results are stated cannot be cited.

        Deliberately NOT a spelling check across all docs: glibc-2.28-93.el8 is
        a legitimately different build -- the RHEL 8.2 one the intra-major
        finding is about -- and a test that flagged it would fire on correct
        prose.
        """
        results = docs()[os.path.join('docs', 'results.md')]
        for build in BUILDS:
            self.assertIn(build, results,
                          f'docs/results.md does not say {build} was measured')


class Step5HunkCountsAgreeEverywhere(unittest.TestCase):
    """Step 5's two hunk counts are published in three places per pair: the
    step's own total, the summary's "step 5 found N" and the summary's "N
    hunk(s) marked >>". Nothing tied them together. They moved on 2026-09-05 (a third tier) and again on 2026-09-07
    (the filter learned to read context lines), and a page left behind is
    exactly what the step-4 "2" was for six weeks.

    The counts against the real clone are asserted in test_known_answers;
    this layer only checks that every published copy says the same thing.
    """

    EXAMPLES = {'rhel8-to-rhel9': 24, 'rhel9-to-rhel10': 52}

    def counts(self, name):
        text = read(os.path.join(REPO_ROOT, 'examples',
                                 f'{name}-audit-output.txt'))
        return (
            re.findall(r'(?m)^(\d+) substantive hunk\(s\) found', text)
            + re.findall(r'step 5 found (\d+) substantive hunk', text)
            + re.findall(r'(\d+) hunk\(s\) marked >> in step 5', text))

    def test_each_example_states_one_count_in_three_places(self):
        for name, expected in self.EXAMPLES.items():
            with self.subTest(example=name):
                got = self.counts(name)
                self.assertEqual(len(got), 3,
                                 f'{name}: found {got}, expected three copies')
                self.assertEqual(set(got), {str(expected)}, got)


class AFigureStatedTwiceIsStatedOnce(unittest.TestCase):
    r"""A number the docs publish, tied to every page that states it and
    to the run it came from.

    The rot this catches is the cheapest kind to cause: change a measurement
    on the page you are editing and leave the other five saying the old one.
    Nothing announces it, because each page is internally consistent. Measured
    2026-09-22 across the published Markdown: 117 figures no test asserts AS
    PUBLISHED. Ten of them became rows of this table, six tied that morning and
    four more later the same day; one has since come out with the only page
    that still stated it, leaving nine. "As published" is the whole of it: two of
    those four, 335 and 342, were already asserted against a live run by
    `test_known_answers` and `test_node_modes`, which read the tool's OUTPUT.
    Neither compared the page with the run, so the page was free to say
    anything. The ones that appear in more than one file are the ones that can
    drift -- a measurement quoted once is right or wrong, never inconsistent,
    which is why a row stated in a single file earns its place through the
    transcript under it rather than through agreement with another page.

    Adding a figure is one row: the label, a pattern whose branches have one
    group each, how many FILES must state it, and an `Evidence` per file whose
    transcript has to carry the same number -- a path alone for the bare
    check, or the sentence and, where a transcript repeats itself, the section
    to read it in. The patterns run over `flat()` text, because one of these
    sentences wraps between the number and the noun that gives it meaning
    (`inherited by` / `328 locales`, in docs/results.md).

    **Files, not mentions.** The floor counted occurrences until
    false-negative-reviewer measured what that allows: rewrite
    docs/requirements.md -- the page whose job is stating the requirement --
    to a DIFFERENT floor in a wording no pattern reaches, add one more correct
    mention to README.md, and the total still agrees with itself. Six of
    those rows have most of their mentions inside one file, so a total is
    exactly the wrong denominator.

    **A pattern is anchored to the phrase, not just to the digits.** The same
    pass measured `(\d{3}) at glibc 2.34` capturing the last three digits of
    `1355`, so two pages published different numbers and this test reported
    agreement -- and firing on `342 at glibc 2.34`, a real figure of this
    project in a sentence somebody may legitimately write. Every row now
    carries enough of its sentence to mean one thing, and `\d+` rather than a
    fixed width, so a longer number fails loudly instead of matching in part.

    An evidence file is named only where the number really appears in query
    output, and only where it is the SAME fact:

      * a file carrying the figure in prose and nowhere else is not listed:
        comma-stripped, the check would find the prose it was meant to be
        independent of and pass whatever the measurement said -- the vacuous
        assertion of the sixteenth entry.
      * neither is a file whose bare number is a DIFFERENT fact that happens
        to agree, such as the complement of a count on one fixture. Tying it
        would tie two facts that agree by arithmetic accident.

    What the BARE form asks is whether the transcript still carries the
    number, so it catches a page re-measured away from the prose and does NOT
    catch one cell of three edited by hand while the others keep the old
    value. That is the honest limit of an existence check, written down here
    rather than left for a reader to discover: the mutation that proves such a
    tie changes every occurrence, because that is the case it can see. Two
    rows kept it, carrying 9,616 and 9,619, until PR #56 removed them with the
    pages that stated those counts; today the row of the locale files that
    differ between 2.28 and 2.34 is the one that uses it. What made those two
    safe was not that the counts were longer but a measurement: bumping each by
    one reddened it, because neither named file carried the bumped value. That
    is narrower than
    it sounds, and deliberately stated so: a file that states BOTH counts
    cannot tell a drift from one to the other, and what reddens such a row is
    the file carrying only its own. An evidence tie is only as good as the
    narrowest file under it.

    **A row may instead name the sentence its number appears in, and the part
    of the transcript that produced it.** Both halves were paid for on
    2026-09-22, in that order. The bare check first: bumping 342 in the prose
    to 343 stayed GREEN, because the same transcript states 343 six hundred
    lines down -- the el9 NODE's `356, of which 343 define LC_COLLATE`
    against the 2.34 TAG's `355, of which 342`. Naming the sentence closed
    that row and did NOT close 331 and 335, which false-negative-reviewer
    then measured on the fix: the el9 node is built from the 2.34 tag, so the
    node scan repeats step 4's copy-closure figures in BYTE-IDENTICAL
    sentences, lines 107 and 113 against 724 and 730. A sentence two facts
    share is not an anchor. Those three rows named the banner of step 4 at
    the tag; they came out on 2026-09-27, with the page that stated their
    figures, and the mechanism stays for the next row that needs it: the
    search is cut at the next banner, and an anchor that is missing or
    doubled refuses instead of widening to the whole file. A named sentence
    must appear exactly ONCE in what is left: two copies mean a drifting one
    is satisfied by a stale one, which is this test's own defect class turned
    on itself. The bare form keeps "at least once", because a transcript may
    state a number twice for good reason.

    `least` is not decoration. Without it, deleting every mention leaves this
    test green over a claim that no longer exists, which is "absent is not
    empty" (tenth entry) one level up: it would then assert agreement among
    nothing. If a figure is deliberately dropped from the docs, its row comes
    out of this table in the same commit.
    """

    FIGURES = (
        # "need", and a list item, since 2026-09-27: README's Prerequisites
        # and confirming-on-a-real-system.md state it in those two shapes, and
        # drifting either to 14 stayed green (false-negative-reviewer).
        ('the PostgreSQL floor the tool requires',
         r'(?:needs? |Needs? |\*\*|(?<!\S)- )(?:PostgreSQL|version) (\d+) '
         r'or newer',
         4,
         ()),
        ('the locales that inherit iso14651_t1 at glibc 2.34',
         r'inherited by (\d+) locales|template that (\d+) locales'
         r'|the (\d+) to \d+ locales that inherit it',
         2,
         ()),
        ('the locale files that differ between glibc 2.28 and 2.34',
         r'(\d+) files that differ between (?:glibc )?2\.28 and 2\.34'
         r'|one line out of (\d+)',
         2,
         (Evidence('examples/rhel8-to-rhel9-audit-output.txt',
                   shape='Locale files changed between glibc-2.28 and '
                         'glibc-2.34: {n}'),)),
    )

    @staticmethod
    def _value(found):
        return next(group for group in found.groups() if group)

    def test_every_page_that_states_it_states_the_same_one(self):
        for label, pattern, least, evidence in self.FIGURES:
            with self.subTest(figure=label):
                seen = {}
                for name, text in sorted(docs().items()):
                    for found in re.finditer(pattern, flat(text)):
                        seen.setdefault(self._value(found), []).append(name)
                files = {name for names in seen.values() for name in names}
                self.assertGreaterEqual(
                    len(files), least,
                    f'{label}: stated in {len(files)} published file(s) '
                    f'({", ".join(sorted(files)) or "none"}), {least} '
                    f'expected. A page that stopped stating it -- or that now '
                    f'states it in a wording this row does not reach -- has '
                    f'left the set this test compares, which is not the same '
                    f'as agreeing with it. Restore the sentence, widen the '
                    f'pattern, or delete this row if the claim went on '
                    f'purpose')
                self.assertEqual(
                    len(seen), 1,
                    f'{label} is published as more than one number: '
                    + '; '.join(f'{value} in {", ".join(sorted(set(files)))}'
                                for value, files in sorted(seen.items())))

    def test_the_evidence_under_it_carries_the_same_number(self):
        """The prose figure against the query output it summarises, which is
        where this project's numbers come from. Prose said 9,616 and the psql
        transcript in the case file said 9616, while a row still tied the
        two."""
        for label, pattern, _, evidence in self.FIGURES:
            if not evidence:
                continue
            values = {self._value(found) for text in docs().values()
                      for found in re.finditer(pattern, flat(text))}
            self.assertEqual(len(values), 1, f'{label}: {values}')
            bare = values.pop().replace(',', '')
            for entry in evidence:
                ev = entry if isinstance(entry, Evidence) else Evidence(entry)
                # A shape that lost its placeholder would join nothing into
                # nothing and quietly become "does this sentence appear",
                # never looking at the number -- and the failure message
                # would print the sentence without it. Found by
                # false-negative-reviewer on this change.
                self.assertIn(
                    '{n}', ev.shape,
                    f'{label}: an evidence shape with no {{n}} checks the '
                    f'sentence and never the number')
                # Not `shape == '{n}'`: ` {n}`, `{n} ` and `: {n}` are the
                # same weak check, and one space walked past the first
                # version of this guard -- measured GREEN on a page drifted
                # from 335 to 327. A shape with no letters outside the
                # placeholder is a bare number however it is spelled.
                self.assertFalse(
                    ev.section is not None
                    and not re.search(r'[A-Za-z]',
                                      ev.shape.replace('{n}', '')),
                    f'{label}: a bare number narrowed to a section is neither '
                    f'check -- any stray digit in the region satisfies it, '
                    f'starting with the one on the line below the sentence. '
                    f'Name the sentence instead')
                path = os.path.join(REPO_ROOT, *ev.path.split('/'))
                text = read(path)
                if ev.section is not None:
                    parts = text.split(ev.section)
                    self.assertEqual(
                        len(parts), 2,
                        f'{label}: the section anchor {ev.section!r} appears '
                        f'{len(parts) - 1} time(s) in {ev.path}, once '
                        f'expected. "I could not find the section" and "the '
                        f'number is not in it" are different answers, so this '
                        f'refuses rather than searching the whole file')
                    self.assertIn(
                        '\n====', parts[1],
                        f'{label}: nothing closes the section under '
                        f'{ev.section!r} in {ev.path}, so cutting it would '
                        f'read to the end of the file -- the whole-file '
                        f'search this anchor exists to avoid. "The section '
                        f'ends here" and "I never found where it ends" are '
                        f'different answers. Two files in examples/ carry no '
                        f'banner at all, so this is reachable by the next row '
                        f'rather than by this one')
                    text = region_after(text, ev.section)
                # Digit boundaries: `9616` is otherwise satisfied by a 19616
                # or a 96160 sitting anywhere in the file. The escape makes a
                # shape's `(s)` and `.` literal.
                found = re.findall(evidence_pattern(ev.shape, bare), text)
                agrees = evidence_agrees(ev.shape, bare, text)
                where = ('' if ev.section is None
                         else f' in the section under "{ev.section}"')
                said = f'"{ev.shape.replace("{n}", bare)}"'
                if ev.shape == '{n}':
                    # The bare form asks only that the figure be in the file,
                    # which is what it has always asked: a transcript may
                    # legitimately print the same number more than once, and
                    # a transcript may state one twice for good reason.
                    self.assertTrue(
                        agrees,
                        f'{label} is published as {bare} but {ev.path} -- '
                        f'the measurement it summarises -- does not contain '
                        f'that number anywhere')
                else:
                    # A named sentence is a claim about one measurement, so
                    # two of them is not reassurance: a drifting copy is
                    # satisfied by a stale one, and nothing says which was
                    # read.
                    self.assertTrue(
                        agrees,
                        f'{label} is published as {bare}, and '
                        f'{ev.path}{where} states it as {said} '
                        f'{len(found)} time(s), once expected')



class EveryTieWouldNoticeItsFigureMoving(unittest.TestCase):
    """The mutation battery, in the repository instead of in a transcript.

    A row of the table above asserts that the pages agree with each other and
    with the run behind them. Nothing asserted that the row would NOTICE the
    figure moving, and the first row measured for it did not: 342 was tied to
    a whole transcript that states 343 six hundred lines down, as the el9
    node's own count, so the tie agreed with the page and would have agreed
    with a page saying 343. Finding that cost a review round, and the proof
    was a battery of file mutations run once, by hand, in a scratch directory
    nobody else can see -- so "thirty-eight red" was a sentence in a CHANGELOG
    entry and not a mechanism, which is the shape of defect this whole layer
    exists to refuse.

    This asks that battery's question without touching a file: if the page
    said one more than it says, would every file under it still confirm? A row
    that answers yes cannot tell its figure from its neighbour, and is
    decoration. Measured 2026-09-22 by putting the 342 row back the way it
    first shipped -- bare, against the whole transcript -- which this reddens.

    It is one guard of several and does not stand in for the others. A
    sentence that two facts in one file share, and a shape too weak for the
    region it is narrowed to, are refused where the tie is checked rather than
    here; what this adds is the question no assertion was asking at all.

    What it CANNOT see is a figure with no run behind it. Three rows have none,
    and for those a drift is invisible as long as every page drifts together
    -- they are tied to each other and to nothing else. That is a real limit
    of the table, so the rows it applies to are named below rather than left
    to be discovered: adding a fourth means writing it in, which is a decision,
    not an omission.
    """

    #: Rows tied only to the other pages that state them. Untied, not
    #: untieable: the tool prints 328 (line 18 of the rhel8-to-rhel9
    #: transcript), and the PostgreSQL floor is a requirement rather than a
    #: measurement. The 6,525 row was here until docs/limitations.md stopped
    #: publishing evidence; it was the one figure that could not be tied --
    #: prose and no query output -- and with no page stating it there is
    #: nothing left for this class to compare. The measurement is in the
    #: CHANGELOG entry that removed it.
    NO_RUN_BEHIND_THEM = (
        'the PostgreSQL floor the tool requires',
        'the locales that inherit iso14651_t1 at glibc 2.34',
    )

    def rows(self):
        return AFigureStatedTwiceIsStatedOnce.FIGURES

    def published(self, pattern):
        """The one value the docs state for this row, commas stripped."""
        values = {next(g for g in m.groups() if g)
                  for text in docs().values()
                  for m in re.finditer(pattern, flat(text))}
        self.assertEqual(len(values), 1, f'{pattern}: {values}')
        return values.pop().replace(',', '')

    def test_the_rows_with_no_run_behind_them_are_the_ones_named(self):
        """A row that quietly loses its evidence stops being checked against
        anything the tool produced, and the table looks the same afterwards.
        """
        bare = frozenset(label for label, _, _, evidence in self.rows()
                         if not evidence)
        self.assertEqual(
            bare, frozenset(self.NO_RUN_BEHIND_THEM),
            'the rows with no evidence are no longer the ones named in '
            'NO_RUN_BEHIND_THEM. A row that lost its evidence is only held '
            'to the other pages that state it; a new one that never had any '
            'has to be written in here, so that it is a decision on the '
            'record rather than something a reader has to go and count')

    def test_moving_one_statement_of_a_figure_breaks_the_tie(self):
        """The battery's question, asked ONE OCCURRENCE AT A TIME.

        Moving the figure in the abstract -- asking whether `n + 1` appears
        anywhere under the row -- is arithmetically the same as bumping every
        copy at once, and that is the battery the invariants file says cannot
        see this defect: it was the per-LINE version that found a sentence
        satisfied by the node's identical copy of it. The first version of
        this class made exactly that mistake and was measured on the one row
        where it happened to work, which is how a proof of three rows was
        generalised from one.

        So: for each occurrence of the figure in each file the row names,
        rebuild that file in memory with THAT occurrence moved and nothing
        else, and ask whether the row still agrees. At least one such move has
        to break it. A row where no single move breaks it cannot tell its
        figure from another statement of the same number in the same file,
        which is decoration.

        Measured 2026-09-22: on the table as it stands every row with evidence
        has such a move, and reverting any of the three sectioned rows to the
        whole-file form it first shipped in reddens this -- where the earlier
        version reddened for one of the three.
        """
        for label, pattern, _, evidence in self.rows():
            if not evidence:
                continue
            with self.subTest(figure=label):
                bare = self.published(pattern)
                moved = str(int(bare) + 1)
                statements, noticed = 0, []
                for entry in evidence:
                    ev = (entry if isinstance(entry, Evidence)
                          else Evidence(entry))
                    text = read(os.path.join(REPO_ROOT, *ev.path.split('/')))
                    if ev.section is not None:
                        text = region_after(text, ev.section)
                    for said in re.finditer(
                            evidence_pattern(ev.shape, bare), text):
                        digits = re.search(rf'(?<!\d){bare}(?!\d)',
                                           said.group(0))
                        at = said.start() + digits.start()
                        statements += 1
                        lifted = text[:at] + moved + text[at + len(bare):]
                        if not evidence_agrees(ev.shape, bare, lifted):
                            noticed.append(ev.path)
                # Nothing to move is not proof of anything: it is the row
                # failing to find its own figure, which the check above
                # reports and this must not read as success.
                self.assertTrue(
                    statements,
                    f'{label}: no file under this row states {bare} in the '
                    f'shape the row names, so there was nothing to move and '
                    f'nothing was proved')
                self.assertTrue(
                    noticed,
                    f'{label}: {statements} statement(s) of {bare} under this '
                    f'row, and moving any one of them on its own leaves the '
                    f'tie agreeing. The row cannot tell its figure from '
                    f'another statement of the same number in the same file, '
                    f'so it would not notice the page being re-measured away '
                    f'from the run. Name the sentence the run states it in, '
                    f'and the section to read that sentence in')


class TheExamplesCarryTheNodeSteps(unittest.TestCase):
    """Both worked examples carry the node steps and the full summary of a
    run given both machines' files, so what the docs say about those steps can
    be checked against real output. Until 2026-09-07 no example carried
    them."""

    # The builds as each machine's file names them: `rpm -q glibc`, the
    # architecture included.
    EXAMPLES = {
        'rhel8-to-rhel9': ('glibc-2.28-251.el8_10.40.x86_64',
                           'glibc-2.34-275.el9_8.x86_64'),
        'rhel9-to-rhel10': ('glibc-2.34-275.el9_8.x86_64',
                            'glibc-2.39-128.el10_2.x86_64'),
    }

    def example(self, name):
        return read(os.path.join(REPO_ROOT, 'examples',
                                 f'{name}-audit-output.txt'))

    def test_every_node_step_banner_is_in_both_examples(self):
        for name, (old, new) in self.EXAMPLES.items():
            text = self.example(name)
            for banner in (
                    f"== STEP 6  DISTRO CHECK  do {old}'s patches touch "
                    "LC_COLLATE?",
                    f"== STEP 7  DISTRO CHECK  do {new}'s patches touch "
                    "LC_COLLATE?",
                    f"== STEP 8  NODE TO NODE  does {old}'s collation data "
                    f"differ from {new}'s?",
                    f"== STEP 9  NODE ELLIPSIS  does {old}'s own locale data "
                    "use ellipsis ranges?",
                    f"== STEP 10  NODE ELLIPSIS  does {new}'s own locale data "
                    "use ellipsis ranges?",
                    "== STEP 11  MEASURED ORDER  how each machine's own glibc "
                    "sorts every locale",
                    '== AUDIT SUMMARY'):
                with self.subTest(example=name, banner=banner[:40]):
                    self.assertIn(banner, text)

    def test_the_summary_blast_radius_line_matches_step_8(self):
        """The summary's "plus N locale(s) that inherit" is read from the list
        step 8 writes; in the example, N must be the count step 8 printed."""
        for name in self.EXAMPLES:
            text = self.example(name)
            step8 = re.search(r'Additionally affected via `copy` inheritance '
                              r'at \S+: (\d+) locale', text)
            summary = re.search(r'plus (\d+) locale\(s\) that inherit', text)
            with self.subTest(example=name):
                self.assertIsNotNone(step8, text[-2000:])
                self.assertIsNotNone(summary)
                self.assertEqual(step8.group(1), summary.group(1))


    def test_the_published_list_length_is_the_set_that_was_reported(self):
        """"full list (N name(s))" against the two numbers printed above it.
        The written list used to hold only the names SUPPORTED maps, so it was
        narrower than the set the same paragraph reported -- on a node, by
        exactly the locale the audit exists for. The arithmetic is the check a
        reader can repeat."""
        blocks = 0
        for name in ('rhel8-to-rhel9-audit-output.txt',
                     'rhel9-to-rhel10-audit-output.txt'):
            text = read(os.path.join(REPO_ROOT, 'examples', name))
            # Anchored line by line: a non-greedy `.*?` here would pair one
            # block's count with the next block's list, which is how the first
            # version of this test read 404 and 413 as the same paragraph.
            for m in re.finditer(
                    r'^Full set needing empirical confirmation: \d+ locale '
                    r'source file\(s\), (\d+) generated locale name\(s\)'
                    r'[^\n]*\n  e\.g\. [^\n]*\n'
                    r'  not in [^\n]*?: ([^\n]+)\n'
                    r'  full list \((\d+) name\(s\)\):',
                    text, re.M):
                blocks += 1
                generated, unbuilt, listed = m.group(1), m.group(2), m.group(3)
                with self.subTest(example=name, listed=listed):
                    self.assertEqual(int(listed),
                                     int(generated) + len(unbuilt.split(', ')))
        self.assertEqual(blocks, 6, 'a step 4 block stopped being checked')


def without_fences(text):
    """Markdown with its fenced code blocks removed: a `#` inside a shell
    snippet is a comment, not a heading. Inline code is kept, because a
    heading's backticked words are part of its anchor."""
    return re.sub(r'```.*?```', '', text, flags=re.S)


def links_in(text):
    """The `](target)` occurrences of a page, ignoring inline code: a
    link-shaped string inside backticks is an example, not a link."""
    return re.finditer(r'\]\(([^)\s]+)\)', re.sub(r'`[^`\n]*`', '', text))


def slug(heading):
    """The anchor GitHub derives from a heading: lower-cased, backticks
    dropped, punctuation removed, and each space turned into a hyphen. Each,
    not each run: "Command 2 — the probe" loses its dash and keeps both
    spaces, so its anchor is `command-2--the-probe`. Collapsing the run, as
    this did until 2026-09-27, reported the README's correct links broken."""
    s = heading.lower().replace('`', '')
    s = re.sub(r'[^\w\s-]', '', s)
    return re.sub(r'\s', '-', s.strip())


class EveryLinkResolves(unittest.TestCase):
    """Retitling a heading breaks every `#the-old-title` link to it and
    nothing errors. The seventeenth entry records two such links, one created
    by retitling the very section being documented; the eleventh records
    examples/rhel8-to-rhel9.sql pointing at a README section that had moved.
    The check used to be a snippet run by hand before a commit, when somebody
    remembered. A check that depends on somebody remembering is not a check.
    """

    def setUp(self):
        self.pages = {path: without_fences(text)
                      for path, text in docs(include_changelog=True).items()}
        self.anchors = {
            path: {slug(h) for h in re.findall(r'^#{1,6} (.+)$', text, re.M)}
            for path, text in self.pages.items()}

    def test_every_anchored_link_names_a_heading_that_exists(self):
        seen = 0
        for path, text in self.pages.items():
            for m in links_in(text):
                if '#' not in m.group(1):
                    continue
                target, anchor = m.group(1).rsplit('#', 1)
                if not re.fullmatch(r'[\w-]+', anchor):
                    continue
                if '://' in target:
                    continue
                page = path if not target else os.path.normpath(
                    os.path.join(os.path.dirname(path), target))
                seen += 1
                with self.subTest(link=m.group(0), in_file=path):
                    self.assertIn(page, self.anchors,
                                  f'{path} links to {target}, which is not a '
                                  f'published page')
                    self.assertIn(anchor, self.anchors[page],
                                  f'{path} links to #{anchor}, and {page} has '
                                  f'no such heading -- retitled?')
        self.assertGreater(seen, 0, 'no anchored links found: pattern stale')

    def test_every_relative_link_names_a_file_that_exists(self):
        seen = 0
        for path, text in self.pages.items():
            for m in links_in(text):
                target = m.group(1).split('#')[0]
                if not target or '://' in target or ':' in target:
                    continue
                seen += 1
                full = os.path.normpath(os.path.join(
                    REPO_ROOT, os.path.dirname(path), target))
                with self.subTest(link=m.group(1), in_file=path):
                    self.assertTrue(os.path.exists(full),
                                    f'{path} links to {target}, which does '
                                    f'not exist')
        self.assertGreater(seen, 0, 'no relative links found: pattern stale')

    def test_every_doc_a_script_or_example_names_exists(self):
        """audit.sh, sql/, examples/ and scripts/ send the reader to other
        files of the repository by path in plain text, outside any Markdown
        link: a page under docs/ or tests/, and since 2026-09-27 also an
        example, a SQL file or a script. Until then only the .md half was read,
        so a path to a deleted example sat in audit.sh with this test green
        (backlog 12.2 d)."""
        named = {}
        candidates = [os.path.join(REPO_ROOT, 'audit.sh')]
        for sub in ('sql', 'examples', 'scripts'):
            folder = os.path.join(REPO_ROOT, sub)
            candidates += [os.path.join(folder, f) for f in os.listdir(folder)
                           if os.path.isfile(os.path.join(folder, f))]
        for cand in candidates:
            for rel in re.findall(
                    r'\b((?:docs|tests|examples|sql|scripts)/[\w./-]+'
                    r'\.(?:md|txt|sql|py|sh|out|json))\b', read(cand)):
                named.setdefault(rel, set()).add(
                    os.path.relpath(cand, REPO_ROOT))
        self.assertTrue(named, 'nothing names a file any more: pattern stale')
        # Each half that names something today; examples/ is read too, but
        # nothing names a file there as of 2026-09-27.
        for kind in ('docs/', 'sql/', 'scripts/'):
            self.assertTrue(any(rel.startswith(kind) for rel in named),
                            f'nothing names a file under {kind} any more: '
                            f'the pattern is stale, or that half went quiet')
        for rel, sources in sorted(named.items()):
            with self.subTest(doc=rel):
                self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, rel)),
                                f'{rel} is named by {sorted(sources)} and '
                                f'does not exist')


class ThePrivateRulesStayPrivate(unittest.TestCase):
    """The working rules Claude follows in this repository -- the skill, the
    hooks, the local settings -- live under .claude/ and name people, hosts
    and test fixtures. They were published once by accident and unpublished in
    PR #18; the decision since is that .claude/ is ignored whole. This pins
    that decision, because it rests on one line of .gitignore and nothing
    errors when that line goes.
    """

    def test_gitignore_ignores_the_whole_claude_directory(self):
        lines = [l.strip() for l in read(os.path.join(REPO_ROOT, '.gitignore'))
                 .splitlines()]
        self.assertIn('.claude/', lines,
                      '.gitignore no longer ignores .claude/ as a whole')

    def test_nothing_under_claude_is_tracked(self):
        tracked = subprocess.run(
            ['git', '-C', REPO_ROOT, 'ls-files', '--', '.claude'],
            capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(tracked, [],
                         f'private working files are tracked: {tracked}')


if __name__ == '__main__':
    unittest.main()
