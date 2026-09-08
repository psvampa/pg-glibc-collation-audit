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
from _harness import REPO_ROOT

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
        """Same tie as the block above. The directory-mode scan used to be a
        manual step nobody ran; wiring it into audit.sh only helps if its
        absent-is-not-empty notice is real, so the doc quotes the heading and
        this asserts the wrapper still prints exactly it."""
        wrapper = read(os.path.join(REPO_ROOT, 'audit.sh'))
        printed = [m.group(1) for m in
                   re.finditer(r'^\s*echo "(-- Node\'s own ellipsis scan: NOT '
                               r'RUN)"', wrapper, re.M)]
        self.assertEqual(len(printed), 1, 'audit.sh no longer prints it')
        found = [name for name, text in docs().items() if printed[0] in text]
        self.assertTrue(found, 'no doc quotes the NOT RUN heading any more')

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

    def test_no_doc_states_a_test_count(self):
        """It went stale twice in one day, so it was removed rather than
        corrected. A number that is not written down cannot rot."""
        for name, text in docs().items():
            self.assertNotRegex(
                text, r'\b\d{2,4} tests\b',
                f'{name} states a test count; tests/README.md deliberately '
                f'does not, because it went stale twice on 2026-09-06')

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
