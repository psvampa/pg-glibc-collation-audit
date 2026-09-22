"""Layer 7: the numbers and quotes the documentation publishes.

Two documentation-correction passes in one day, on 2026-09-06, found the same
class of defect each time: a count, a position or a quoted output line that no
longer matched what the tool or the measurement actually produced. Re-reading
caught them; re-reading is not a control.

This is the checkable half of that, turned into a test. It reads the published
files and asserts they agree with each other and with the scripts. It cannot
check prose, and it deliberately does not try: what it covers is exactly the
part where a human re-read is wasted effort.

No glibc clone needed -- everything here is in the repository.
"""
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
    """The heading's echo and the contiguous run of echoes under it, as
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
        dirs[:] = [d for d in dirs if d not in ('.git', 'glibc', '.claude')]
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
    block = text.split('(query 1)')[1].split('(41 rows)')[0]
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
        """The RHEL9-vs-RHEL10 file claims "This is code point order, exactly".
        Nothing else checks that claim."""
        rows = ordering_rows(read(EXAMPLE_9_10))
        cps = [int(cp[2:], 16) for _, cp, _ in rows]
        self.assertEqual(cps, sorted(cps))
        self.assertIn('code point order, exactly', read(EXAMPLE_9_10))

    def test_the_examples_name_the_builds_they_were_measured_on(self):
        for path, expected in ((EXAMPLE_8_9, BUILDS[:2]),
                               (EXAMPLE_9_10, BUILDS[1:])):
            text = read(path)
            for build in expected:
                self.assertIn(build, text, os.path.basename(path))


class TheDocsQuoteWhatTheToolsPrint(unittest.TestCase):
    """A quoted output block goes stale silently: the tool changes and the
    quote keeps reading as current."""

    def test_the_NOT_RUN_block_is_quoted_verbatim(self):
        wrapper = read(os.path.join(REPO_ROOT, 'audit.sh'))
        printed = [m.group(1) for m in
                   re.finditer(r'^\s*echo "(-- Node-to-node locale data: NOT '
                               r'RUN)"', wrapper, re.M)]
        self.assertEqual(len(printed), 1, 'audit.sh no longer prints it')
        found = [name for name, text in docs().items() if printed[0] in text]
        self.assertTrue(found, 'no doc quotes the NOT RUN heading any more')

    def test_the_ellipsis_scan_NOT_RUN_block_is_quoted_verbatim(self):
        """Same tie as the block above, and the whole body rather than the
        heading alone -- which is what this test's name has always promised.
        The heading-only form let the body drift and say something false:
        backlog 1.17 was the sentence "no tag of this pair holds
        localedata/locales/C" -- true of 2.28..2.34 and of the floor pair
        2.12..2.17, false of 2.34..2.39 and of any pair whose new tag is 2.35
        or later -- and rewording it failed nothing in this suite."""
        wrapper = read(os.path.join(REPO_ROOT, 'audit.sh'))
        heading = "-- Node's own ellipsis scan: NOT RUN"
        printed = [m.group(1) for m in
                   re.finditer(r'^\s*echo "(' + re.escape(heading) + r')"',
                               wrapper, re.M)]
        self.assertEqual(len(printed), 1, 'audit.sh no longer prints it')
        limits = read(os.path.join(REPO_ROOT, 'docs', 'limitations.md'))
        m = re.search(r'Given neither directory it reads instead:.*?```\n'
                      r'(.*?)```', limits, re.S)
        self.assertIsNotNone(m, 'the quoted block is gone from limitations.md')
        self.assertEqual(echoed_block(wrapper, heading),
                         m.group(1).rstrip('\n').split('\n'))

    def test_the_one_sided_ellipsis_scan_NOT_RUN_block_is_quoted_verbatim(self):
        """The fourth of these ties. Given one of the two directories, the
        side that was NOT scanned prints this heading -- it used to print
        nothing at all, and the scan of the other node says nothing about it
        (backlog 1.16). A doc quotes the heading, and this asserts the
        wrapper still prints exactly it. The heading is fixed text so that
        this grep can find it; the side and its flag are named in the body.
        """
        wrapper = read(os.path.join(REPO_ROOT, 'audit.sh'))
        printed = [m.group(1) for m in
                   re.finditer(r'^\s*echo "(-- Node\'s own locale data, '
                               r'ellipsis scan: NOT RUN)"', wrapper, re.M)]
        self.assertEqual(len(printed), 1, 'audit.sh no longer prints it')
        found = [name for name, text in docs().items() if printed[0] in text]
        self.assertTrue(found, 'no doc quotes the NOT RUN heading any more')

    def test_the_direction_NOT_ESTABLISHED_block_is_quoted_verbatim(self):
        """The third of these ties. The wrapper prints this heading when
        neither tag is an ancestor of the other and the newest glibc tag
        behind each one does not order them either -- "the direction was not
        checked", which reads as "the direction is fine" if it is left
        unsaid. A doc quotes the heading, and this asserts the wrapper still
        prints exactly it."""
        wrapper = read(os.path.join(REPO_ROOT, 'audit.sh'))
        printed = [m.group(1) for m in
                   re.finditer(r'^\s*echo "(-- Direction of the pair: NOT '
                               r'ESTABLISHED)"', wrapper, re.M)]
        self.assertEqual(len(printed), 1, 'audit.sh no longer prints it')
        found = [name for name, text in docs().items() if printed[0] in text]
        self.assertTrue(found, 'no doc quotes the heading any more')

    def test_the_below_floor_example_matches_the_asserted_numbers(self):
        """The example is a saved run, so it can go stale exactly the way the
        old step 4 figure did. Its table is tied to the same numbers
        test_known_answers asserts against the pinned tags."""
        path = os.path.join(REPO_ROOT, 'examples',
                            'below-the-floor-2.12-to-2.17.txt')
        text = read(path)
        for label, before, after in (
                ('Step 3, affected locale source files', '11', '280'),
                ('Step 4, needing empirical confirmation', '277', '281'),
                ('Step 4, generated names per SUPPORTED', '404', '411')):
            self.assertRegex(
                text, rf'{re.escape(label)}\s+{before}\s+{after}\b',
                f'{label} no longer reads {before} -> {after}')
        self.assertIn('Full affected set (280 locale source file(s))', text)
        self.assertIn('281 locale source file(s), 411 generated', text)
        # The header counts generated names; the written list also carries the
        # names SUPPORTED does not list. Putting one where the other belongs
        # is how this example gained a line the tool never printed.
        self.assertIn('pg_collation show (409):', text)

    def test_the_skipped_release_example_matches_the_method_page(self):
        """docs/method.md publishes a five-row table comparing the two steps
        with the direct jump, and examples/ carries the run it came from. Two
        copies of five numbers is exactly the shape the step-4 "2" rotted in.

        Rows 1, 2 and 3 are asserted against the pinned clone in
        test_known_answers (SkippingAReleaseReportsTheUnion). The two step 8
        rows are NOT, and cannot be: they need both nodes'
        /usr/share/i18n/locales/, which no test has. Those four numbers are
        pinned here instead, to the tool's own lines inside the three example
        bodies -- otherwise a hand-written table would be tied only to another
        hand-written table, which is two copies of prose and no measurement.
        """
        example = read(os.path.join(REPO_ROOT, 'examples',
                                    'skipping-a-release-2.28-to-2.39.txt'))
        method = flat(docs()[os.path.join('docs', 'method.md')])
        rows = (('Step 2, files changed inside `LC_COLLATE`', 2, 3, 5, 5),
                ('Step 3, generated names to reindex', 6, 4, 10, 10),
                ('Step 5, substantive hunks', 24, 52, 76, 75),
                ('Step 8, locales differing on the two nodes', 3, 3, 6, 6),
                ('Step 8, locales the upgrade removes', 1, 1, 2, 2))
        for label, first, second, both, direct in rows:
            with self.subTest(row=label):
                self.assertIn(
                    f'| {label} | {first} | {second} | {both} | **{direct}** |',
                    method,
                    f'docs/method.md no longer states {label} as '
                    f'{first}/{second}/{both}/{direct}')
                self.assertRegex(
                    example,
                    rf'{re.escape(label.replace("`", ""))}\s+{first}\s+'
                    rf'{second}\s+{both}\s+{direct}\b',
                    f'the example no longer states {label} the same way')

        # The one row that is not a sum is the only one worth a sentence, and
        # both copies have to carry the same explanation of why.
        self.assertIn('75 hunks, not 76', method)
        self.assertIn('75 substantive hunk(s) found', example)
        self.assertIn('THE ONE FIGURE THAT IS NOT A SUM: 75, NOT 76', example)

        # C.UTF-8 is the claim a reader is most likely to doubt, so the
        # example has to carry the line the TOOL printed, not the header's
        # quotation of it. Asserting the bare phrase passed on the prose
        # alone: an example regenerated from a run that no longer reported
        # C.UTF-8 as DIFFERS would have stayed green, which is two copies of
        # prose tied to each other and no measurement.
        summary = example.split('== AUDIT SUMMARY')[1]
        self.assertIn('C (C.UTF-8): DIFFERS  <- in neither tag', summary)
        self.assertIn('C (C.UTF-8): DIFFERS', method)

    STEP_8 = {
        'rhel8-to-rhel9-audit-output.txt':
            (3, {'C', 'or_IN', 'sv_SE'}, 1, {'en_US@ampm'}),
        'rhel9-to-rhel10-audit-output.txt':
            (3, {'ber_DZ', 'kab_DZ', 'th_TH'}, 1, {'aa_ER@saaho'}),
        'skipping-a-release-2.28-to-2.39.txt':
            (6, {'C', 'ber_DZ', 'kab_DZ', 'or_IN', 'sv_SE', 'th_TH'},
             2, {'aa_ER@saaho', 'en_US@ampm'}),
    }

    def _node_section(self, name):
        """The differing and removed sets step 8 printed, from the example."""
        text = read(os.path.join(REPO_ROOT, 'examples', name))
        diff_n = int(re.search(r'(?m)^Differ INSIDE LC_COLLATE \((\d+)\)',
                               text).group(1))
        diff_block = text.split('Differ INSIDE LC_COLLATE (')[1]
        diff_block = diff_block.split('\n', 1)[1].split('\n\n')[0]
        differing = set(re.findall(r'(?m)^  (\S+)  \(', diff_block))
        gone_n = int(re.search(r'(?m)^Only on the old node \((\d+)\)',
                               text).group(1))
        gone_block = text.split('Only on the old node (')[1]
        gone_block = gone_block.split('\n', 1)[1].split('\n\n')[0]
        gone = set(re.findall(r'(?m)^  (\S+): ', gone_block))
        # The header count and the list under it are two different facts, and
        # the whole point of this class is that one of them can go stale.
        self.assertEqual(len(differing), diff_n,
                         f'{name}: step 8 says {diff_n} differing and names '
                         f'{len(differing)}')
        self.assertEqual(len(gone), gone_n,
                         f'{name}: step 8 says {gone_n} removed and names '
                         f'{len(gone)}')
        return differing, gone

    def test_step_8_rows_come_from_the_examples(self):
        """The two step 8 rows of the method.md table, against the
        node-to-node section each of the three examples carries.

        Collected by unittest in its own right. Written first as a helper
        called from the test above, where deleting the single call line left
        the layer green and the whole STEP_8 tie stopped running with nothing
        saying so -- and it ran at all only if every assertion above that call
        had passed first.
        """
        got = {}
        for name, (dn, dset, gn, gset) in self.STEP_8.items():
            differing, gone = self._node_section(name)
            self.assertEqual(differing, dset, f'{name}: step 8 differing set')
            self.assertEqual(gone, gset, f'{name}: step 8 removed set')
            got[name] = (differing, gone)
        first, second, direct = (got['rhel8-to-rhel9-audit-output.txt'],
                                 got['rhel9-to-rhel10-audit-output.txt'],
                                 got['skipping-a-release-2.28-to-2.39.txt'])
        self.assertEqual(direct[0], first[0] | second[0],
                         'step 8 on the direct pair is no longer the union')
        self.assertEqual(direct[1], first[1] | second[1],
                         'the removed locales are no longer the union')

    def test_the_skipped_release_example_is_not_sold_as_an_audited_pair(self):
        """docs/scope.md publishes two pairs and this is not a third. The
        floor pair carries the same disclaimer for the same reason: a
        measured-but-unpublished pair is one careless sentence away from
        becoming a supported one, which is how RHEL7 kept coming back."""
        example = read(os.path.join(REPO_ROOT, 'examples',
                                    'skipping-a-release-2.28-to-2.39.txt'))
        self.assertIn('NOT AN AUDITED PAIR', example)
        scope = flat(docs()[os.path.join('docs', 'scope.md')])
        self.assertIn('Two upgrade pairs: RHEL8 \u2192 RHEL9 and RHEL9 \u2192 RHEL10',
                      scope)

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
        """"Six of the suite's nine layers need the glibc clone", the three
        modules named as needing none, the Layers table's rows and the CI
        comment's list are four hand-written statements of one fact that the
        filesystem already holds.

        Written the day the eighth layer became the ninth, which moved the
        number in one file, added a row in a second and a filename list in a
        third -- by hand, with nothing to catch the fourth place. The CI
        comment was the one that had already gone stale: it named the
        pure-function layer alone, from back when that was the only clone-free
        one, in the file whose whole purpose is that a green build means the
        layers ran. Seventeenth entry: every number a doc publishes gets a
        test the same day.

        A layer needs the clone exactly when its source asks for one of the
        two skip decorators, which is the same fact the table's yes/no column
        states.
        """
        spelled = {n: i for i, n in enumerate(
            'zero one two three four five six seven eight nine ten eleven '
            'twelve thirteen fourteen fifteen sixteen seventeen eighteen '
            'nineteen twenty'.split())}
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

        requirements = flat(docs()[os.path.join('docs', 'requirements.md')])
        stated = re.search(r"(\w+) of the suite's (\w+) layers need the "
                           r"glibc clone", requirements, re.I)
        self.assertIsNotNone(
            stated, 'docs/requirements.md no longer states the layer counts '
                    'in the shape this test reads; it is the sentence that '
                    'goes stale, so it cannot be left unasserted')
        said_gated, said_total = (stated.group(1).lower(),
                                  stated.group(2).lower())
        self.assertEqual(spelled[said_total], len(modules),
                         'docs/requirements.md says %r layers; tests/ holds '
                         '%d' % (stated.group(2), len(modules)))
        self.assertEqual(spelled[said_gated], len(gated),
                         'docs/requirements.md says %r layers need the clone; '
                         '%d ask for a skip decorator: %s'
                         % (stated.group(1), len(gated), ', '.join(gated)))

        # The ROWS, not the file: asserting that each filename appears
        # somewhere in tests/README.md passed with the row deleted, because
        # the paragraph under the table names three of the modules too.
        # Measured with a mutant that renamed a row.
        table = docs()[os.path.join('tests', 'README.md')]
        rows = set(re.findall(r'(?m)^\| `(test_\w+\.py)` \|', table))
        self.assertEqual(rows, set(modules),
                         'the Layers table in tests/README.md and tests/ do '
                         'not hold the same modules')
        workflow = read(os.path.join(REPO_ROOT, '.github', 'workflows',
                                     'tests.yml'))
        for name in free:
            self.assertIn(name, requirements,
                          f'docs/requirements.md does not name {name} among '
                          f'the layers that run without a clone')
            self.assertIn(name[:-3], workflow,
                          f'the CI comment does not name {name} among the '
                          f'clone-free layers a broken clone step would '
                          f'leave running alone')

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
    """Step 5's two hunk counts are published in four places per pair: the
    step's own total, the summary's "step 5 found N", the summary's "N hunk(s)
    marked >>", and the sentence in docs/results.md. Nothing tied them
    together. They moved on 2026-09-05 (a third tier) and again on 2026-09-07
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

    def test_the_results_page_states_the_same_two_counts(self):
        results = docs()[os.path.join('docs', 'results.md')]
        self.assertIn(f"counts from 8 and 48 to the "
                      f"{self.EXAMPLES['rhel8-to-rhel9']} and "
                      f"{self.EXAMPLES['rhel9-to-rhel10']} a run prints today",
                      ' '.join(results.split()))


class AFigureStatedTwiceIsStatedOnce(unittest.TestCase):
    r"""A number restated across pages, tied to the pages that state it.

    The rot this catches is the cheapest kind to cause: change a measurement
    on the page you are editing and leave the other five saying the old one.
    Nothing announces it, because each page is internally consistent. Measured
    2026-09-22 across the published Markdown: 117 figures nothing asserts, of
    which the ones that appear in more than one file are the ones that can
    drift -- a measurement quoted once is right or wrong, never inconsistent.

    Adding a figure is one row: the label, a pattern with one group, how many
    FILES must state it, and any file whose transcript has to carry the same
    number. The patterns run over `flat()` text, because one of these
    sentences wraps between the number and the noun that gives it meaning
    (`inherited by` / `328 locales`, in docs/results.md).

    **Files, not mentions.** The floor counted occurrences until
    false-negative-reviewer measured what that allows: rewrite
    docs/requirements.md -- the page whose job is stating the requirement --
    to a DIFFERENT floor in a wording no pattern reaches, add one more correct
    mention to README.md, and the total still agrees with itself. Three of
    these six rows have most of their mentions inside one file, so a total is
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

      * `breakage/cases/04-lc-ctype.md` carries 6,525 in prose and nowhere
        else, so it is not listed: comma-stripped, the check would find the
        prose it was meant to be independent of and pass whatever the
        measurement said -- the vacuous assertion of the sixteenth entry.
      * `breakage/cases/05-partial-brin-gist.md` and
        `cases/B-planner-statistics.md` do carry a bare 9616, but theirs is
        `count(*) WHERE w < 'vz'`, the complement of case 8's violating count,
        equal to it only on this fixture. Tying them would tie two facts that
        agree by arithmetic accident.

    What the evidence tie asks is whether the transcript still carries the
    number, so it catches a page re-measured away from the prose and does NOT
    catch one cell of three edited by hand while the others keep the old
    value. That is the honest limit of an existence check, written down here
    rather than left for a reader to discover: the mutation that proves the
    tie changes every occurrence, because that is the case it can see.

    `least` is not decoration. Without it, deleting every mention leaves this
    test green over a claim that no longer exists, which is "absent is not
    empty" (tenth entry) one level up: it would then assert agreement among
    nothing. If a figure is deliberately dropped from the docs, its row comes
    out of this table in the same commit.
    """

    #     label, pattern with ONE group, FILES expected, evidence files
    FIGURES = (
        ('the rows that break in breakage/ cases 8 and 9',
         r'(\d[\d,]*) (?:stored rows violate|stored values no longer match'
         r'|offending rows)',
         2,
         ('breakage/cases/08-check-constraint.md',
          'breakage/cases/09-generated-column-matview.md')),
        ('the PostgreSQL floor the tool requires',
         r'(?:needs |Needs |\*\*)(?:PostgreSQL|version) (\d+) or newer',
         3,
         ()),
        ('the characters that answer differently between the two builds',
         r'(\d[\d,]*) (?:figure in \[case 4\]|characters of case 4'
         r'|characters that answer differently|of them answer differently)',
         4,
         ()),
        ('the locales that inherit iso14651_t1 at glibc 2.34',
         r'inherited by (\d+) locales|template that (\d+) locales'
         r'|the (\d+) to \d+ locales that inherit it',
         5,
         ()),
        ('the rows that land in the wrong partition in case 7',
         r'(\d[\d,]*) rows sit in the wrong partition',
         1,
         ('breakage/cases/07-range-partition.md',
          'breakage/repair.md')),
        ('the locale files in the tree at glibc 2.34',
         r'in the tree[^0-9]{0,4}(\d+) at glibc 2\.34',
         2,
         ()),
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
        where this project's numbers come from. Prose says 9,616; the psql
        transcript in the case file says 9616."""
        for label, pattern, _, evidence in self.FIGURES:
            if not evidence:
                continue
            values = {self._value(found) for text in docs().values()
                      for found in re.finditer(pattern, flat(text))}
            self.assertEqual(len(values), 1, f'{label}: {values}')
            bare = values.pop().replace(',', '')
            for relative in evidence:
                path = os.path.join(REPO_ROOT, *relative.split('/'))
                # Digit boundaries: `9616` is otherwise satisfied by a 19616
                # or a 96160 sitting anywhere in the file.
                self.assertRegex(
                    read(path), rf'(?<!\d){bare}(?!\d)',
                    f'{label} is published as {bare} but {relative} -- the '
                    f'measurement it summarises -- does not contain that '
                    f'number anywhere')


class TheExamplesCarryTheNodeSteps(unittest.TestCase):
    """docs/limitations.md quotes the summary block steps 9 and 10 add, and
    until 2026-09-07 that block appeared in no examples/*.txt and no test tied
    it -- the one published place a reader could check it against was
    missing. Both worked examples now carry steps 6 to 10 and the full summary
    of a run given both nodes' directories."""

    EXAMPLES = {
        'rhel8-to-rhel9': ('glibc-2.28-251.el8_10.40', 'glibc-2.34-275.el9_8'),
        'rhel9-to-rhel10': ('glibc-2.34-275.el9_8', 'glibc-2.39-128.el10_2'),
    }

    def example(self, name):
        return read(os.path.join(REPO_ROOT, 'examples',
                                 f'{name}-audit-output.txt'))

    def test_every_node_step_banner_is_in_both_examples(self):
        for name, (old, new) in self.EXAMPLES.items():
            text = self.example(name)
            for banner in (f"== DISTRO CHECK  do {old}'s patches touch LC_COLLATE?",
                           f"== DISTRO CHECK  do {new}'s patches touch LC_COLLATE?",
                           f"== NODE TO NODE  does {old}'s collation data differ from {new}'s?",
                           f"== NODE ELLIPSIS  does {old}'s own locale data use ellipsis ranges?",
                           f"== NODE ELLIPSIS  does {new}'s own locale data use ellipsis ranges?",
                           '== AUDIT SUMMARY'):
                with self.subTest(example=name, banner=banner[:30]):
                    self.assertIn(banner, text)

    def test_the_quoted_steps_9_10_block_is_verbatim_from_the_example(self):
        """The fenced block under "What steps 9 and 10 add to the summary" in
        docs/limitations.md, line for line in the RHEL8->RHEL9 example."""
        limits = read(os.path.join(REPO_ROOT, 'docs', 'limitations.md'))
        m = re.search(r'What steps 9 and 10 add to the summary.*?```\n(.*?)```',
                      limits, re.S)
        self.assertIsNotNone(m, 'the quoted block is gone from limitations.md')
        self.assertIn(m.group(1), self.example('rhel8-to-rhel9'))

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


    def test_every_C_status_the_docs_list_is_one_the_tool_can_print(self):
        """docs/limitations.md enumerates the states the steps 9/10 summary
        line can carry. A state renamed in the code and left standing in that
        list is the seventeenth entry's defect in a new place: prose a reader
        checks their own output against, describing output that no longer
        exists.

        The statuses are taken from the function that prints them, not grepped
        out of the file -- a first version of this test searched the whole
        source and passed on a renamed status, because the old wording still
        sat in a comment three lines above. `report_backported` is pure, so
        this needs no clone.
        """
        import contextlib
        import io

        import flag_algorithmic_ranges as far

        def status(body):
            with contextlib.redirect_stdout(io.StringIO()):
                return far.report_backported({'C': body} if body else {})['C']

        printed = [status(b) for b in (
            _harness.backported_c(),
            _harness.upstream_c(),
            _harness.locale_file('order_start forward',
                                 '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                                 'order_end'),
            _harness.locale_file('copy "iso14651_t1"'),
            'comment_char %\nescape_char /\n',
            None)]
        self.assertEqual(len(set(printed)), 6, printed)

        limits = read(os.path.join(REPO_ROOT, 'docs', 'limitations.md'))
        wrapper = read(os.path.join(REPO_ROOT, 'audit.sh'))
        for full in printed:
            # The scripts append "  <- why it matters"; the docs list the name.
            name = full.split('  <-')[0]
            with self.subTest(status=name):
                self.assertIn(f'`{name}`'.replace('`ABSENT from this directory`',
                                                  '`ABSENT from this locale '
                                                  'directory`'),
                              limits)
        self.assertIn('C (C.UTF-8): NOT DECLARED', wrapper)
        self.assertIn('`NOT DECLARED`', limits)

    def test_the_published_list_length_is_the_set_that_was_reported(self):
        """"full list (N name(s))" against the two numbers printed above it.
        The written list used to hold only the names SUPPORTED maps, so it was
        narrower than the set the same paragraph reported -- on a node, by
        exactly the locale the audit exists for. The arithmetic is the check a
        reader can repeat."""
        blocks = 0
        for name in ('rhel8-to-rhel9-audit-output.txt',
                     'rhel9-to-rhel10-audit-output.txt',
                     'below-the-floor-2.12-to-2.17.txt'):
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
        self.assertEqual(blocks, 7, 'a step 4 block stopped being checked')


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
    dropped, punctuation removed, runs of whitespace to one hyphen."""
    s = heading.lower().replace('`', '')
    s = re.sub(r'[^\w\s-]', '', s)
    return re.sub(r'\s+', '-', s.strip())


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
        """audit.sh, sql/ and examples/ send the reader to docs/*.md by path
        in plain text, outside any Markdown link."""
        named = {}
        candidates = [os.path.join(REPO_ROOT, 'audit.sh')]
        for sub in ('sql', 'examples', 'scripts'):
            folder = os.path.join(REPO_ROOT, sub)
            candidates += [os.path.join(folder, f) for f in os.listdir(folder)
                           if os.path.isfile(os.path.join(folder, f))]
        for cand in candidates:
            for rel in re.findall(r'\b((?:docs|tests)/[\w./-]+\.md)\b',
                                  read(cand)):
                named.setdefault(rel, set()).add(
                    os.path.relpath(cand, REPO_ROOT))
        self.assertTrue(named, 'nothing names a doc any more: pattern stale')
        for rel, sources in sorted(named.items()):
            with self.subTest(doc=rel):
                self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, rel)),
                                f'{rel} is named by {sorted(sources)} and '
                                f'does not exist')


class TheRepairDocumentQuotesWhatIsPublished(unittest.TestCase):
    """breakage/repair.md used to print the whole repair script a second time,
    under a heading, and breakage/scripts/04-repair.sql is that script as a
    runnable file. Two copies of one text drift -- this repository has been
    bitten by exactly that, a '~16s'/'~17s' disagreement about one runtime and
    a 'four things' count against a five-item list. Those two copies drifted
    three times in the session that published the file, so the document now
    links the script instead of repeating it.

    One quotation is left, the check_index helper in the header, because step 0
    cannot be read without it. This pins that one.
    """

    def _quoted_helper(self):
        md = docs()[os.path.join('breakage', 'repair.md')]
        blocks = re.findall(r'```sql\n(.*?)```', md, re.S)
        self.assertEqual(
            1, len(blocks),
            'breakage/repair.md is expected to quote exactly one sql block, the '
            'check_index helper. A second one is a copy of something that is '
            'published elsewhere, which is what this class exists to prevent')
        return blocks[0].strip()

    @staticmethod
    def _script(name):
        """Read with newline='' so a CRLF file does not compare equal to an LF
        one. The default translates them and would make this test pass over a
        real difference -- measured on a scratch copy."""
        path = os.path.join(REPO_ROOT, 'breakage', 'scripts', name)
        with open(path, encoding='utf-8', newline='') as fh:
            return fh.read()

    def test_the_helper_quoted_in_the_header_is_the_published_one(self):
        """repair.md's header quotes check_index and says it is defined in
        scripts/01b-helpers.sql. That is a second copy of a text, with the same
        way of going wrong."""
        quoted = self._quoted_helper()
        published = re.search(
            r'CREATE OR REPLACE FUNCTION check_index\(ix regclass\).*?'
            r'END \$\$ LANGUAGE plpgsql;',
            self._script('01b-helpers.sql'), re.S)
        self.assertIsNotNone(
            published,
            'breakage/scripts/01b-helpers.sql no longer defines check_index')
        # assertEqual, not assertIn: a substring test passes over a quote that
        # simply stops early, and what a truncated quote drops first is the
        # NOT ASKED arm -- the half that keeps an absence from reading as an
        # answer. Measured on a scratch copy: cutting that arm out of the
        # document passed the assertIn form.
        self.assertEqual(
            published.group(0), quoted,
            "breakage/repair.md's quoted helper is not the one "
            "breakage/scripts/01b-helpers.sql publishes")


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
