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
import json
import os
import re
import subprocess
import sys
import unittest

import _harness
from _harness import COMPARED_BANNER, REPO_ROOT, deleted_steps_named, flat

import glibc_locale_data as g

PROBE = os.path.join(REPO_ROOT, 'sql', 'c_utf8_probe.sql')
EXAMPLE_8_9 = os.path.join(REPO_ROOT, 'examples',
                           'c-utf8-probe-rhel8-vs-rhel9.txt')
EXAMPLE_9_10 = os.path.join(REPO_ROOT, 'examples',
                            'c-utf8-probe-rhel9-vs-rhel10.txt')
# The probe's PostgreSQL 14 branch, which no published transcript shows: one
# run of the probe as shipped in 11.3, `psql -X -e -f sql/c_utf8_probe.sql
# 2>&1`, on PostgreSQL 14.24, glibc-2.34-284.el9_8.4, a database whose
# default is en_US.UTF-8 (handoff of 11.3, m2).
PROBE_PG14 = os.path.join(REPO_ROOT, 'tests', 'c_utf8_probe', 'pg14.el9.out')
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
a promise. What the narrowing buys is the thing it was added for: keeping a
tag's figures apart from the figures a node built from that tag reprints word
for word, lower down, under a later banner.

A `section` is only ever a narrowing of a NAMED sentence, never of a bare
number, and the combination is refused. A bare number takes whatever digits
the region holds, and it does not need a wide region to go wrong: the
measurement that bought this rule drifted a page from 331 to 327 and
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


def evidence_counts(shape, bare, text):
    """(statements of this figure, statements with any number) of `shape`.

    Both the verdict and the failure message read these, so a message never
    prints a count the verdict did not use.
    """
    return (len(re.findall(evidence_pattern(shape, bare), text)),
            len(re.findall(evidence_pattern(shape, r'\d+'), text)))


def evidence_agrees(shape, bare, text):
    """Does `text` state this figure the way a row with this shape requires?

    The rule in one place, because the class that proves a tie discriminates
    has to apply the SAME rule as the check that relies on it. A named
    sentence must appear exactly once whatever number it carries, and that
    one must carry this figure. Counting only the copies that already carried
    the figure could not see a second copy carrying another number, and let a
    line that merely starts the same way stand in for the real one unless the
    shape shut it out by hand (measured 2026-09-29). The bare number needs
    only to be there, since a transcript may state a figure twice for good
    reason.
    """
    found, sentences = evidence_counts(shape, bare, text)
    if shape == '{n}':
        return found > 0
    return sentences == 1 and found == 1


def region_after(text, section):
    """`text` from `section` to the next banner, or a loud failure.

    It refuses here rather than trusting a caller to have checked: not every
    caller checks first, and an anchor appearing twice would otherwise return
    the span BETWEEN the copies -- narrower, and green for the wrong reason.
    """
    parts = text.split(section)
    if len(parts) != 2:
        raise ValueError(f'the section anchor {section!r} appears '
                         f'{len(parts) - 1} time(s), once expected')
    if '\n====' not in parts[1]:
        raise ValueError(f'nothing closes the section under {section!r}, so '
                         f'cutting it would read to the end of the file')
    return parts[1].split('\n====')[0]


def docs():
    """{relative path: text} for every published .md."""
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
            path = os.path.join(root, name)
            out[os.path.relpath(path, REPO_ROOT)] = read(path)
    return out


# Run in a fresh interpreter: the skip decorators decide when a module is
# imported, and this process may already have imported them with the clone
# it has.
_WITHOUT_A_CLONE = """
import json, sys, unittest
tests = sys.argv[1]
sys.path.insert(0, tests)
import _harness
_harness.have_clone = lambda: False
suite = unittest.defaultTestLoader.discover(tests, top_level_dir=tests)
counts, broken, stack = {}, [], [suite]
while stack:
    item = stack.pop()
    if isinstance(item, unittest.TestSuite):
        stack.extend(item)
        continue
    cls = type(item)
    if cls.__name__ == '_FailedTest':
        broken.append(item.id())
        continue
    why = (getattr(cls, '__unittest_skip_why__', None) or
           getattr(getattr(cls, item._testMethodName, None),
                   '__unittest_skip_why__', None))
    counts.setdefault(cls.__module__ + '.py', [0, 0])[
        why == _harness._SKIP_NO_CLONE] += 1
if broken:
    sys.exit('did not import: ' + ', '.join(broken))
print(json.dumps(counts))
"""


def tests_without_a_clone(tests_dir):
    """{module file: (tests that run, tests that skip)} with the glibc clone
    made to look absent. Only the clone's own skip counts as a skip: the
    question is what the clone gates, not what this machine lacks."""
    p = subprocess.run([sys.executable, '-c', _WITHOUT_A_CLONE, tests_dir],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError('could not load the suite without a clone: '
                             + p.stderr.strip())
    return {name: tuple(pair) for name, pair in json.loads(p.stdout).items()}


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
        start, end = g.collate_bounds(self.real)
        block = '\n'.join(self.real.split('\n')[start:end - 1])
        real_ranges = re.findall(r'<U([0-9A-F]+)>', block)
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
        stated = re.findall(r'count\(\*\) = (\d+) AS c_utf8_probe_rows_ok',
                            self.probe)
        self.assertEqual(stated, ['41'])

    def test_the_corpus_carries_the_collation_under_test(self):
        """The corpus is no table any more, so nothing declares the column's
        collation but this. Without it query 3, whose comparisons take the
        column's collation, read the database default instead: measured in
        step 11.3 (f1-M), U+007F < U+FFFF went from t to f in an en_US
        database on PostgreSQL 14 to 18, and nothing changed in a C.UTF-8
        one, which is the database the transcripts come from."""
        corpus = self.probe.split('SELECT $corpus$')[1].split('$corpus$')[0]
        self.assertEqual(corpus.count('w COLLATE pg_catalog."C.utf8" AS w'), 1)


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
        table = re.findall(r'^ c_utf8_equals_byte_order \| '
                           r'positions_differing\s*\n-+\+-+\n'
                           r' ([tf]) +\| +(\d+)\n\(1 row\)$',
                           read(EXAMPLE_8_9), re.M)
        self.assertEqual(len(table), 1, 'query 2 table not read once')
        differing = int(table[0][1])
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


def node_output(text, name):
    """The first node's own output in a probe transcript: the lines between
    its `$ psql` line and its `$ diff` line.

    Refuses a transcript where either line is missing, doubled or out of
    order, rather than comparing whatever part of it a looser cut would
    return."""
    lines = text.split('\n')
    psql = [i for i, line in enumerate(lines) if line.startswith('$ psql ')]
    diff = [i for i, line in enumerate(lines) if line.startswith('$ diff ')]
    if len(psql) != 1 or len(diff) != 1 or psql[0] > diff[0]:
        raise AssertionError(f'{name}: expected one `$ psql` line followed by '
                             f'one `$ diff` line, found {len(psql)} and '
                             f'{len(diff)}')
    return lines[psql[0] + 1:diff[0]]


def psql_script(text):
    r"""What psql runs from `text`, in order, read by psql's own scanner
    rules (src/fe_utils/psqlscan.l, REL_18_6) rather than by line shapes.

    Returns [(kind, raw, words, refs)]: kind 'sql' for a statement sent to
    the server, or the name of a backslash command ('echo', '!', 'set',
    'gset', 'if', 'else', 'endif'). raw is the text as written: for a
    statement, from its first token to its `;` (or to the `\gset` that
    sends it), comments inside it kept, as psqlscan's {whitespace} rule
    ECHOes them once the buffer holds something. words is raw with its `--`
    comments dropped and its whitespace collapsed outside literals: the form
    the pinned list holds, variables left as written. refs lists the
    `:name` references of a statement as (offset in raw, name): psql
    replaces each with the variable's value before sending
    (psqlscan.l:717-761, the value pushed back and scanned again), and
    `psql -e` echoes the replaced text.

    A `;` ends a statement only outside a literal, a quoted identifier, a
    dollar quote, a comment and parentheses, as in psqlscan. What this
    reader cannot read the way psql does is refused, never guessed: a block
    comment (psql reads it in an exclusive state where `;` and backslash
    commands are only text), an E'' string, a quoted or tested variable
    (`:'name'`, `:"name"`, `:{?name}`, psqlscan.l:763-775), a backslash
    command this file does not model or a variable that changes what psql
    echoes or prints, text after the last statement."""
    items, i, n, start, depth, refs = [], 0, len(text), None, 0, []
    word = re.compile(r'[A-Za-z_\x80-\U0010ffff][\w$\x80-\U0010ffff]*')

    def close_quote(q, at):
        j = at
        while True:
            j = text.find(q, j)
            if j < 0:
                raise AssertionError(f'unclosed {q} at offset {at}')
            if text.startswith(q * 2, j):
                j += 2
                continue
            return j + 1

    def statement(end):
        nonlocal start, refs
        raw = text[start:end]
        items.append(('sql', raw, sql_words(raw.rstrip(';')),
                      [(at - start, name) for at, name in refs]))
        start, refs = None, []

    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith('--', i):
            i = text.find('\n', i) if '\n' in text[i:] else n
        elif text.startswith('/*', i):
            raise AssertionError(f'a block comment at offset {i}: psql reads '
                                 f'what is inside it as text, never runs it')
        elif c == '\\':
            end = text.find('\n', i)
            end = n if end < 0 else end
            line = text[i:end]
            name = re.match(r'\\(!|[a-z]+)', line)
            name = name.group(1) if name else line[:2]
            if start is not None:
                if name != 'gset' or line.strip() != '\\gset':
                    raise AssertionError(f'a psql command inside a statement: '
                                         f'{line!r}')
                statement(len(text[:i].rstrip()))
            elif name not in ('echo', '!', 'set', 'if', 'else', 'endif'):
                raise AssertionError(f'a psql command this test does not '
                                     f'model: {line!r}')
            elif name == 'set' and re.match(
                    r'\\set\s+(ECHO|ECHO_HIDDEN|QUIET|SINGLELINE|SINGLESTEP|'
                    r'FETCH_COUNT|SHOW_ALL_RESULTS)\b', line):
                raise AssertionError(f'a variable that changes what psql '
                                     f'echoes or prints: {line!r}')
            items.append((name, line, line.strip(), []))
            i = end
        else:
            if start is None:
                start = i
            m = word.match(text, i)
            if m:
                i = m.end()
            elif c in '\'"':
                if c == "'" and i and text[i - 1] in 'eE' and (
                        i < 2 or not re.match(r'\w', text[i - 2])):
                    raise AssertionError(f"an E'' string at offset {i}")
                i = close_quote(c, i + 1)
            elif c == '$':
                m = re.compile(r'\$([A-Za-z_]\w*)?\$').match(text, i)
                if not m:
                    raise AssertionError(f'a $ this test cannot read at '
                                         f'offset {i}')
                j = text.find(m.group(0), m.end())
                if j < 0:
                    raise AssertionError(f'unclosed dollar quote at {i}')
                i = j + len(m.group(0))
            elif text.startswith('::', i):
                i += 2
            elif c == ':':
                m = re.compile(r':([A-Za-z\x80-\xff_0-9]+)').match(text, i)
                if not m:
                    raise AssertionError(f'a quoted or tested psql variable '
                                         f'at offset {i}: '
                                         f'{text[i:i + 20]!r}')
                refs.append((i, m.group(1)))
                i = m.end()
            elif c == '(':
                depth, i = depth + 1, i + 1
            elif c == ')':
                depth, i = max(depth - 1, 0), i + 1
            elif c == ';' and depth == 0:
                statement(i + 1)
                i += 1
            else:
                i += 1
    if start is not None:
        raise AssertionError('text after the last ; that psql would send '
                             'unfinished: ' + text[start:start + 60])
    return items


def sql_words(raw):
    """`raw` with its comments dropped and each run of whitespace outside a
    literal collapsed to one space. Inside a literal or a quoted identifier
    a space is data ('U+007F  < U+FFFF' is a label psql prints), so it is
    kept; a dollar-quoted body is read the same way."""
    def one(m):
        if m.group('lit'):
            return m.group('lit')
        if m.group('dol') is not None:
            tag = m.group('tag')
            return f'${tag}$ ' + sql_words(m.group('body')) + f' ${tag}$'
        return ' '
    # Twice: a comment between two runs of whitespace leaves two spaces.
    pattern = (r"(?P<lit>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")"
               r"|(?P<dol>\$(?P<tag>[A-Za-z_]\w*|)\$(?P<body>.*?)"
               r"\$(?P=tag)\$)|--[^\n]*|\s+")
    once = re.sub(pattern, one, raw, flags=re.S)
    return re.sub(pattern, one, once, flags=re.S).strip()


def gset_columns(raw):
    """[(variable, value or None)] a `\\gset` query sets: one per item of
    its select list, named by its AS alias (StoreQueryTuple,
    src/bin/psql/common.c:803-850, REL_18_6, sets one variable per column
    of its one row). The value is known here when the item is one literal
    (a '' string or a dollar quote); otherwise it comes from the server and
    the transcript names it. An item with no alias is refused: its column
    name would be psql's guess."""
    lits = r"'(?:[^']|'')*'|\$([A-Za-z_]\w*|)\$.*?\$\1\$"
    words = re.sub(lits + r'|--[^\n]*', lambda m: m.group(0) if not
                   m.group(0).startswith('--') else ' ' * len(m.group(0)),
                   raw, flags=re.S)
    hidden = re.sub(lits, lambda m: '\0' * len(m.group(0)), words, flags=re.S)
    m = re.match(r'SELECT\s', hidden, re.I)
    if not m:
        raise AssertionError(f'a \\gset query this test cannot read: '
                             f'{words[:60]!r}')
    depth, cut, parts, at = 0, len(hidden), [], m.end()
    for k in range(m.end(), len(hidden)):
        ch = hidden[k]
        depth += ch == '('
        depth -= ch == ')'
        if depth == 0 and ch == ',':
            parts.append((at, k))
            at = k + 1
        if depth == 0 and re.match(r'\sFROM\b', hidden[k:], re.I):
            cut = k
            break
    parts.append((at, cut))
    out = []
    for a, b in parts:
        item = words[a:b].strip()
        alias = re.search(r'\sAS\s+([A-Za-z_]\w*)$', item, re.I)
        if not alias:
            raise AssertionError(f'a \\gset column with no AS name: {item!r}')
        expr = item[:alias.start()].strip()
        lit = re.fullmatch(r"'((?:[^']|'')*)'|\$([A-Za-z_]\w*|)\$(.*)\$\2\$",
                           expr, re.S)
        value = None
        if lit:
            value = (lit.group(1).replace("''", "'") if lit.group(1)
                     is not None else lit.group(3))
        out.append((alias.group(1), value))
    return out


def echo_arguments(raw, values):
    """What `\\echo` prints: its arguments joined by one space
    (exec_command_echo, src/bin/psql/command.c:1566-1592, REL_18_6), a
    quoted one unquoted ('' is ') and a `:name` replaced by its value
    (psqlscanslash.l, xslasharg). A backslash in a quoted argument, or an
    argument glued to another, is refused."""
    rest, out = raw.strip()[len('\\echo'):], []
    for m in re.finditer(r"\s+|'((?:[^'\\]|'')*)'(?=\s|$)|:([A-Za-z_]\w*)"
                         r"(?=\s|$)|([^\s'\\:]+)(?=\s|$)|(.)", rest):
        if m.group(0).isspace():
            continue
        if m.group(4) is not None:
            raise AssertionError(f'an \\echo argument this test cannot '
                                 f'read: {raw!r}')
        if m.group(1) is not None:
            out.append(m.group(1).replace("''", "'"))
        elif m.group(2) is not None:
            if m.group(2) not in values:
                raise AssertionError(f'{raw!r} reads :{m.group(2)}, which '
                                     f'nothing set')
            out.append(values[m.group(2)])
        else:
            out.append(m.group(3))
    return ' '.join(out)


#: The tag psql prints for each kind of statement the probe runs
#: (PostgreSQL's CommandTag, as psql prints it). A statement of another
#: kind is refused until it is written here.
TAGS = (('SELECT', None), ('WITH', None), ('SHOW', None), ('EXPLAIN', None),
        ('SET', 'SET'), ('RESET', 'RESET'), ('DO', 'DO'),
        ('DROP TABLE', 'DROP TABLE'), ('CREATE TABLE', 'CREATE TABLE'),
        ('CREATE INDEX', 'CREATE INDEX'), ('INSERT', r'INSERT 0 \d+'))


def expected_output(items, echo, branch):
    r"""What psql prints for `items`, as events: ('echo', text) where `psql
    -e` echoes a statement, its variables replaced; ('table',) for a
    result; ('tag', regex) for a command tag; ('line', text) for an
    `\echo`; ('shell',) for a `\!`. A `\gset` query prints nothing but its
    echo (PrintQueryResult hands its row to StoreQueryTuple, and
    PrintQueryStatus prints no tag for rows: common.c:1056-1057, 1002-1009,
    REL_18_6). `branch` gives the value the server gave each variable a
    `\gset` sets from a query this file cannot evaluate: True/False, or
    text. A variable read and never set, or given but set by no `\gset`,
    is refused; so is a `\gset` whose value the file holds and `branch`
    restates."""
    values, live, settable, events = {}, [True], set(), []
    for k, (kind, raw, words, refs) in enumerate(items):
        if kind == 'if':
            var = re.fullmatch(r'\\if :([A-Za-z_]\w*)', words)
            if not var:
                raise AssertionError(f'an \\if this test cannot read: {raw!r}')
            if var.group(1) not in values:
                raise AssertionError(f'{raw!r} reads a variable nothing set '
                                     f'or that no value was given for')
            v = values[var.group(1)]
            if v not in ('t', 'f'):
                raise AssertionError(f'{raw!r}: {v!r} is not a boolean psql '
                                     f'reads the same everywhere')
            live.append(live[-1] and v == 't')
            continue
        if kind == 'else':
            top = live.pop()
            live.append(live[-1] and not top)
            continue
        if kind == 'endif':
            live.pop()
            continue
        if kind == 'sql':
            gset = k + 1 < len(items) and items[k + 1][0] == 'gset'
            if gset:
                for name, value in gset_columns(raw):
                    settable.add(name)
                    if not live[-1]:
                        continue
                    if value is not None:
                        if name in branch:
                            raise AssertionError(
                                f'{name} is set from the file itself; the '
                                f'transcript must not restate it')
                        values[name] = value
                    elif name in branch:
                        b = branch[name]
                        values[name] = (('t' if b else 'f')
                                        if isinstance(b, bool) else str(b))
        if not live[-1]:
            continue
        if kind == 'sql':
            text, shift = raw, 0
            for at, name in refs:
                if name not in values:
                    raise AssertionError(f':{name} is read before anything '
                                         f'sets it, or no value was given')
                value = values[name]
                inner = psql_script(value + '\n;')
                if len(inner) != 1 or inner[0][3]:
                    raise AssertionError(f':{name} holds text psql would '
                                         f'not send as one piece')
                text = (text[:at + shift] + value
                        + text[at + shift + 1 + len(name):])
                shift += len(value) - 1 - len(name)
            if echo:
                events.append(('echo', text))
            if gset:
                continue
            tag = [t for lead, t in TAGS
                   if re.match(lead.replace(' ', r'\s+') + r'\b', words, re.I)]
            if not tag:
                raise AssertionError(f'a statement whose output this test '
                                     f'does not know: {words[:60]!r}')
            events.append(('table',) if tag[0] is None else ('tag', tag[0]))
        elif kind == 'echo':
            events.append(('line', echo_arguments(raw, values)))
        elif kind == '!':
            events.append(('shell',))
    if len(live) != 1:
        raise AssertionError('an \\if without its \\endif')
    unknown = set(branch) - settable
    if unknown:
        raise AssertionError(f'values given for variables no \\gset sets: '
                             f'{sorted(unknown)}')
    return events


def match_output(lines, events):
    """None if `lines` is exactly what `events` print, else why not.
    Every line is accounted for: an extra statement, table or line between
    two expected ones is a mismatch, not something to skip."""
    i = 0
    for e, event in enumerate(events):
        at = f'line {i + 1} ({event[0]} #{e})'
        if event[0] == 'shell':
            # What a shell command prints cannot be predicted, so each \!
            # takes exactly one line, as each did on every published node.
            # Skipping to the next expected line instead let any line stand
            # in for the output (the label above them deleted, in 11.3's
            # battery).
            if i >= len(lines):
                return f'{at}: the \\! printed nothing'
            i += 1
        elif event[0] == 'echo':
            want = [ln.rstrip() for ln in event[1].split('\n')]
            got = [ln.rstrip() for ln in lines[i:i + len(want)]]
            if got != want:
                k = next(k for k in range(len(want))
                         if k >= len(got) or got[k] != want[k])
                return (f'line {i + k + 1} (echo #{e}): echoed '
                        f'{got[k] if k < len(got) else None!r}, the probe '
                        f'runs {want[k]!r}')
            i += len(want)
        elif event[0] == 'line':
            if lines[i:i + 1] != [event[1]]:
                return f'{at}: {lines[i:i + 1]!r}, expected {event[1]!r}'
            i += 1
        elif event[0] == 'tag':
            if not (i < len(lines) and re.fullmatch(event[1], lines[i])):
                return f'{at}: {lines[i:i + 1]!r}, expected tag {event[1]!r}'
            i += 1
        else:
            if not (i + 1 < len(lines) and lines[i].strip()
                    and re.fullmatch(r'-+(\+-+)*', lines[i + 1])):
                return f'{at}: no result table: {lines[i:i + 2]!r}'
            j = i + 2
            while j < len(lines) and not re.fullmatch(r'\((\d+) rows?\)',
                                                      lines[j]):
                j += 1
            if j == len(lines):
                return f'{at}: a table with no (N rows) line'
            if int(re.search(r'\d+', lines[j]).group(0)) != j - i - 2:
                return f'{at}: {lines[j]!r} under {j - i - 2} row line(s)'
            if lines[j + 1:j + 2] != ['']:
                return f'{at}: no blank line after {lines[j]!r}'
            i = j + 2
    rest = [ln for ln in lines[i:] if ln.strip()]
    if rest:
        return f'line {i + 1}: printed after the last statement: {rest[:2]!r}'
    return None


class TheProbeRunsWhatWasMeasured(unittest.TestCase):
    r"""Everything sql/c_utf8_probe.sql makes psql do, pinned, and each
    published transcript tied to it statement by statement.

    Until step 11.3 the transcripts were tied to the probe only by its
    `\echo` headings and query 3's labels: an EXPLAIN added before query 6,
    query 5's verdict reworded, query 6's statements deleted under a heading
    kept, all left this module green (measured in step 11.3's phase 0).
    Forbidding the words of what went is the list that never closed in
    13.1, so the whole sequence is pinned instead: each statement as written
    (its `:name` variables too), its comments dropped and its whitespace
    collapsed outside literals, and each backslash command as written. A
    statement added, dropped, moved or changed fails here; a deliberate
    change is run on PostgreSQL again and then written below, with the
    transcripts recorded again from the same file.

    Each transcript is then read line by line against what psql prints for
    those items: the echo of every statement (`psql -e`), with each
    variable replaced by the value psql would send, a table or the command
    tag for each statement but a `\gset` one, which prints nothing, every
    `\echo` line, nothing else. A value the file itself sets (the corpus)
    is computed from the file; one the server gives (its version, the
    corpus count) is named by the transcript, so a branch of an `\if` the
    server did not take prints nothing.
    """

    #: Pinned from PostgreSQL: the statements of the probe whose `psql -X -e`
    #: and plain runs on PostgreSQL 14.24 to 18.6 matched every line
    #: (step 11.3, f1-M/out, 80 runs).
    STATEMENTS = (
        '\\set ON_ERROR_STOP on',
        "\\echo '=== 0. node identity: a result is bound to the build it ran "
        "on ==='",
        "\\echo '--- the machine psql runs on (the three shell lines below "
        "run there, not on the server):'",
        "\\! rpm -q glibc 2>/dev/null || echo 'rpm not available -- record "
        "the glibc build by hand'",
        '\\! ldd --version | head -1',
        "\\! locale -a | grep -ix 'c.utf8\\|c.utf-8' || echo '!! C.utf8 is "
        "NOT in locale -a on this node'",
        "\\echo '--- the server: its glibc release as PostgreSQL reports it "
        '(one number for every build of that release; record the build by '
        "hand)'",
        'SELECT coalesce( (SELECT pg_collation_actual_version(oid) FROM '
        "pg_collation WHERE collprovider = 'c' AND "
        'pg_collation_actual_version(oid) IS NOT NULL ORDER BY collname '
        "LIMIT 1), 'NOT KNOWN: no libc collation in this database reports a "
        "version ' '(only C, POSIX and C.* locales are here); record the "
        "server''s glibc by hand') AS server_glibc",
        "\\echo '=== 0b. guards: without these, both nodes can agree for the "
        "wrong reason ==='",
        'SHOW server_encoding',
        'SHOW standard_conforming_strings',
        "DO $$ BEGIN IF current_setting('server_version_num')::int < 140000 "
        "THEN RAISE EXCEPTION 'PostgreSQL % is older than 14, the oldest "
        "version this probe was ' 'measured on.', "
        "current_setting('server_version'); END IF; IF "
        "current_setting('server_encoding') <> 'UTF8' THEN RAISE EXCEPTION "
        "'server_encoding is %, not UTF8. Every comparison in this probe "
        "would ' 'collapse to byte order and two nodes would agree while "
        "proving nothing.', current_setting('server_encoding'); END IF; IF "
        "current_setting('standard_conforming_strings') <> 'on' THEN RAISE "
        "EXCEPTION 'standard_conforming_strings is off; the U&''\\+xxxxxx'' "
        "literals below ' 'would not mean what they say.'; END IF; END $$",
        'DO $$ DECLARE prov "char"; BEGIN SELECT collprovider INTO prov FROM '
        "pg_collation WHERE collname = 'C.utf8' AND collnamespace = "
        "'pg_catalog'::regnamespace; IF prov IS NULL THEN RAISE EXCEPTION "
        "'no collation named C.utf8 in pg_catalog. Run ' 'SELECT "
        "pg_import_system_collations(''pg_catalog''); as superuser.'; END "
        "IF; IF prov <> 'c' THEN RAISE EXCEPTION 'the collation named C.utf8 "
        "has provider %, not the libc provider ''c''. ' 'This probe measures "
        "glibc; a builtin collation cannot change with it.', prov; END IF; "
        'END $$',
        "\\echo '=== 0c. the C-ish collations on this node, and their "
        "versions ==='",
        'SELECT collname, collprovider, collversion, '
        'pg_collation_actual_version(oid) AS actual FROM pg_collation WHERE '
        "collname IN ('C', 'POSIX', 'C.utf8', 'C.UTF-8', 'pg_c_utf8') AND "
        "collnamespace = 'pg_catalog'::regnamespace ORDER BY collname",
        'SELECT $corpus$ SELECT cp, kind, w COLLATE pg_catalog."C.utf8" AS w '
        "FROM (VALUES ('U+0001', 'UTF-8 1-byte', U&'\\+000001'), ('U+0041', "
        "'ASCII anchor A', U&'\\+000041'), ('U+005A', 'ASCII anchor Z', "
        "U&'\\+00005A'), ('U+0061', 'ASCII anchor a', U&'\\+000061'), "
        "('U+007F', 'UTF-8 1-byte last', U&'\\+00007F'), ('U+0080', 'UTF-8 "
        "2-byte first', U&'\\+000080'), ('U+07FF', 'UTF-8 2-byte last', "
        "U&'\\+0007FF'), ('U+0800', 'UTF-8 3-byte first', U&'\\+000800'), "
        "('U+FFFF', 'plane 0 last (declared)', U&'\\+00FFFF'), ('U+10000', "
        "'plane 1 first (declared)', U&'\\+010000'), ('U+1FFFF', 'plane 1 "
        "last (declared)', U&'\\+01FFFF'), ('U+20000', 'plane 2 first "
        "(declared)', U&'\\+020000'), ('U+2FFFF', 'plane 2 last (declared)', "
        "U&'\\+02FFFF'), ('U+30000', 'plane 3 first (NO range)', "
        "U&'\\+030000'), ('U+3FFFF', 'plane 3 last (NO range)', "
        "U&'\\+03FFFF'), ('U+40000', 'plane 4 first (NO range)', "
        "U&'\\+040000'), ('U+4FFFF', 'plane 4 last (NO range)', "
        "U&'\\+04FFFF'), ('U+50000', 'plane 5 first (NO range)', "
        "U&'\\+050000'), ('U+5FFFF', 'plane 5 last (NO range)', "
        "U&'\\+05FFFF'), ('U+60000', 'plane 6 first (NO range)', "
        "U&'\\+060000'), ('U+6FFFF', 'plane 6 last (NO range)', "
        "U&'\\+06FFFF'), ('U+70000', 'plane 7 first (NO range)', "
        "U&'\\+070000'), ('U+7FFFF', 'plane 7 last (NO range)', "
        "U&'\\+07FFFF'), ('U+80000', 'plane 8 first (NO range)', "
        "U&'\\+080000'), ('U+8FFFF', 'plane 8 last (NO range)', "
        "U&'\\+08FFFF'), ('U+90000', 'plane 9 first (NO range)', "
        "U&'\\+090000'), ('U+9FFFF', 'plane 9 last (NO range)', "
        "U&'\\+09FFFF'), ('U+A0000', 'plane 10 first (NO range)', "
        "U&'\\+0A0000'), ('U+AFFFF', 'plane 10 last (NO range)', "
        "U&'\\+0AFFFF'), ('U+B0000', 'plane 11 first (NO range)', "
        "U&'\\+0B0000'), ('U+BFFFF', 'plane 11 last (NO range)', "
        "U&'\\+0BFFFF'), ('U+C0000', 'plane 12 first (NO range)', "
        "U&'\\+0C0000'), ('U+CFFFF', 'plane 12 last (NO range)', "
        "U&'\\+0CFFFF'), ('U+D0000', 'plane 13 first (NO range)', "
        "U&'\\+0D0000'), ('U+DFFFF', 'plane 13 last (NO range)', "
        "U&'\\+0DFFFF'), ('U+E0000', 'plane 14 first (declared)', "
        "U&'\\+0E0000'), ('U+EFFFF', 'plane 14 last (declared)', "
        "U&'\\+0EFFFF'), ('U+F0000', 'plane 15 first (declared)', "
        "U&'\\+0F0000'), ('U+FFFFF', 'plane 15 last (declared)', "
        "U&'\\+0FFFFF'), ('U+100000', 'plane 16 first (declared)', "
        "U&'\\+100000'), ('U+10FFFF', 'plane 16 last (declared)', "
        "U&'\\+10FFFF') ) AS v(cp, kind, w) $corpus$ AS c_utf8_probe_corpus",
        '\\gset',
        'SELECT count(*) AS c_utf8_probe_rows, count(*) = 41 AS '
        'c_utf8_probe_rows_ok FROM (:c_utf8_probe_corpus) AS c',
        '\\gset',
        '\\if :c_utf8_probe_rows_ok',
        '\\else',
        "\\echo '!! the corpus is' :c_utf8_probe_rows 'row(s), expected 41.'",
        "DO $$ BEGIN RAISE EXCEPTION 'corpus is not 41 rows. A value was "
        "rejected or collapsed; ' 'the comparison below would be narrower "
        "than the one this ' 'file documents.'; END $$",
        '\\endif',
        "\\echo '=== 1. the order C.utf8 produces on this node (diff this) "
        "==='",
        'WITH c_utf8_probe AS (:c_utf8_probe_corpus) SELECT row_number() '
        'OVER (ORDER BY w COLLATE pg_catalog."C.utf8") AS pos, cp, kind FROM '
        'c_utf8_probe ORDER BY pos',
        "\\echo '=== 2. does C.utf8 equal byte order? READ BOTH CASES ==='",
        'WITH c_utf8_probe AS (:c_utf8_probe_corpus), under_locale AS ( '
        'SELECT cp, row_number() OVER (ORDER BY w COLLATE '
        'pg_catalog."C.utf8") AS pos FROM c_utf8_probe ), under_bytes AS ( '
        'SELECT cp, row_number() OVER (ORDER BY w COLLATE "C") AS pos FROM '
        'c_utf8_probe ) SELECT bool_and(l.pos = b.pos) AS '
        'c_utf8_equals_byte_order, count(*) FILTER (WHERE l.pos <> b.pos) AS '
        'positions_differing FROM under_locale l JOIN under_bytes b USING '
        '(cp)',
        "\\echo '=== 3. the named pairs, so a diff says which side is which "
        "==='",
        'WITH c_utf8_probe AS (:c_utf8_probe_corpus) SELECT pair, holds FROM '
        "( SELECT 1 AS n, 'U+007F  < U+FFFF' AS pair, (SELECT w FROM "
        "c_utf8_probe WHERE cp = 'U+007F') < (SELECT w FROM c_utf8_probe "
        "WHERE cp = 'U+FFFF') AS holds UNION ALL SELECT 2, 'U+07FF  < "
        "U+FFFF', (SELECT w FROM c_utf8_probe WHERE cp = 'U+07FF') < (SELECT "
        "w FROM c_utf8_probe WHERE cp = 'U+FFFF') ) t ORDER BY n",
        "\\echo '=== 4. collversion asserted as expected-NULL ==='",
        'SELECT collname, collversion, pg_collation_actual_version(oid) AS '
        'actual, (collversion IS NULL AND pg_collation_actual_version(oid) '
        'IS NULL) AS both_null_as_expected FROM pg_collation WHERE collname '
        "= 'C.utf8' AND collprovider = 'c' AND collnamespace = "
        "'pg_catalog'::regnamespace",
        "SELECT current_setting('server_version_num')::int >= 150000 AS "
        'c_utf8_probe_pg15',
        '\\gset',
        '\\if :c_utf8_probe_pg15',
        'SELECT datname, datcollate, datcollversion, '
        'pg_database_collation_actual_version(oid) AS actual, '
        '(datcollversion IS NULL AND '
        'pg_database_collation_actual_version(oid) IS NULL) AS '
        "both_null_as_expected FROM pg_database WHERE datlocprovider = 'c' "
        "AND datcollate ~ '^[Cc]\\.' ORDER BY datname",
        '\\else',
        'SELECT datname, datcollate FROM pg_database WHERE datcollate ~ '
        "'^[Cc]\\.' ORDER BY datname",
        "\\echo 'PostgreSQL 14 has no pg_database.datcollversion, so the "
        "rows above carry no version to assert.'",
        '\\endif',
        "\\echo '=== 5. is THIS database exposed through its default "
        "collation? ==='",
        '\\if :c_utf8_probe_pg15',
        'SELECT datname, datlocprovider, datcollate, CASE WHEN '
        "datlocprovider <> 'c' THEN 'not libc' WHEN datcollate IN ('C', "
        "'POSIX') THEN 'byte order' WHEN datcollate ~ '^[Cc]\\.' THEN "
        "'EXPOSED via C.UTF-8, and no collversion can ever warn' ELSE "
        "'EXPOSED via ' || datcollate END AS verdict FROM pg_database WHERE "
        'datname = current_database()',
        '\\else',
        "SELECT datname, datcollate, CASE WHEN datcollate IN ('C', 'POSIX') "
        "THEN 'byte order' WHEN datcollate ~ '^[Cc]\\.' THEN 'EXPOSED via "
        "C.UTF-8, and no collversion can ever warn' ELSE 'EXPOSED via ' || "
        'datcollate END AS verdict FROM pg_database WHERE datname = '
        'current_database()',
        '\\endif',
        "\\echo '=== done. diff this output against the other node. ==='",
    )

    #: (transcript, captured with `psql -e`, the value its server gave each
    #: variable a \gset sets from a query this file cannot evaluate). The
    #: last is a whole run, not a transcript: the only one of the branch
    #: PostgreSQL 14 takes.
    TRANSCRIPTS = (
        (EXAMPLE_8_9, True, {'c_utf8_probe_pg15': True,
                             'c_utf8_probe_rows_ok': True,
                             'c_utf8_probe_rows': 41}),
        (EXAMPLE_9_10, True, {'c_utf8_probe_pg15': True,
                              'c_utf8_probe_rows_ok': True,
                              'c_utf8_probe_rows': 41}),
        (PROBE_PG14, True, {'c_utf8_probe_pg15': False,
                            'c_utf8_probe_rows_ok': True,
                            'c_utf8_probe_rows': 41}))

    def output_of(self, path):
        """A transcript's first node, or a recorded run whole."""
        name = os.path.basename(path)
        if path == PROBE_PG14:
            text = read(path)
            self.assertTrue(text.endswith('\n'), name)
            return text[:-1].split('\n')
        return node_output(read(path), name)

    def test_the_end_line_the_docs_check_for_is_the_last_and_only_one(self):
        """README and confirming teach `grep -L '^=== done'` before the diff.
        It names a file whose run stopped, since two runs stopped the same way
        diff as alike. That holds only while the probe prints exactly one line
        starting `=== done`, as its last item. In round 2 of 11.3
        false-negative-reviewer showed that an earlier heading of that shape,
        pinned and recorded, would leave this module green and a stopped run
        unnamed. It is read in what psql prints, on each branch a record
        takes, not in how the file spells it. An unquoted `\\echo === done`
        prints the same line."""
        items = psql_script(read(PROBE))
        read_on = set()
        for _, _, branch in self.TRANSCRIPTS:
            with self.subTest(branch=branch):
                self.assertIsNone(end_line_problem(items, branch))
                read_on.add(branch['c_utf8_probe_pg15'])
        self.assertEqual(read_on, {True, False})
        for text, problem in (
                ("\\echo '=== done'\n\\echo 'x'\n", 'not the last'),
                ("\\echo === done early\n\\echo '=== done'\n",
                 '2 printed lines start')):
            with self.subTest(text=text):
                self.assertIn(problem, end_line_problem(psql_script(text), {})
                              or '')
        for name in ('README.md', 'docs/confirming-on-a-real-system.md'):
            with self.subTest(page=name):
                text = read(os.path.join(REPO_ROOT, *name.split('/')))
                self.assertEqual(re.findall(r"grep -L '([^']*)'", text),
                                 ['^=== done'])

    def test_the_branches_each_record_takes_are_both_branches(self):
        """The \\if on the server's version has two branches, and the
        records between them take both: a record dropped, or one recorded
        again on the other side, would leave a branch tied to nothing."""
        taken = {branch['c_utf8_probe_pg15'] for _, _, branch in
                 self.TRANSCRIPTS}
        self.assertEqual(taken, {True, False})

    def test_the_probe_runs_these_statements_and_nothing_else(self):
        self.assertEqual(tuple(item[2] for item in psql_script(read(PROBE))),
                         self.STATEMENTS)

    def test_each_transcript_prints_what_the_probe_runs(self):
        items = psql_script(read(PROBE))
        for path, echo, branch in self.TRANSCRIPTS:
            name = os.path.basename(path)
            with self.subTest(path=name):
                why = match_output(self.output_of(path),
                                   expected_output(items, echo, branch))
                self.assertIsNone(why, f'{name} is not what the probe '
                                       f'prints: {why}')

    def test_what_the_reader_refuses(self):
        """The forms psql reads differently from a plain split, each
        refused by its message (a refusal is asserted by its message)."""
        for text, message in (
                ('SELECT 1;\n/* SELECT 2; */\n', 'block comment'),
                ("SELECT E'a';\n", "E'' string"),
                ("SELECT :'x';\n", 'quoted or tested psql variable'),
                ('SELECT :"x";\n', 'quoted or tested psql variable'),
                ('SELECT :{?x};\n', 'quoted or tested psql variable'),
                ('SELECT 1\n\\echo x\n;\n', 'inside a statement'),
                ('SELECT 1 \\gset p_\n', 'inside a statement'),
                ('\\o /dev/null\nSELECT 1;\n', 'does not model'),
                ('\\set ECHO none\nSELECT 1;\n', 'changes what psql'),
                ('SELECT 1;\nSELECT 2\n', 'after the last ;')):
            with self.subTest(text=text):
                with self.assertRaisesRegex(AssertionError,
                                            re.escape(message)):
                    psql_script(text)
        for text, branch, message in (
                ('SELECT * FROM :t;\n', {}, 'read before anything sets it'),
                ('SELECT 1 AS a \\gset\n', {'b': True}, 'no \\gset sets'),
                ("SELECT 'x' AS a \\gset\n", {'a': 'x'}, 'must not restate'),
                ('SELECT 1 \\gset\n', {}, 'no AS name'),
                ("SELECT $q$1; SELECT 2$q$ AS q \\gset\nSELECT :q;\n", {},
                 'not send as one piece'),
                ('SELECT 1 AS v \\gset\n\\if :v\n\\endif\n', {'v': 2},
                 'not a boolean'),
                ("\\echo 'a'b\n", {}, 'cannot read'),
                ('\\echo :nope\n', {}, 'which nothing set')):
            with self.subTest(text=text):
                with self.assertRaisesRegex(AssertionError,
                                            re.escape(message)):
                    expected_output(psql_script(text), True, branch)

    def test_the_cut_reads_what_psql_reads(self):
        """The controls of the refusals: a `;` in a comment, a literal, a
        dollar quote or parentheses ends nothing; two spaces in a literal
        are kept; `::` is a cast; a variable stays as written in the pin;
        a query sent by \\gset."""
        got = [item[2] for item in psql_script(
            "SELECT 'a;b'  -- c; d\n  , 1::int\nFROM t;\n"
            "DO $$ BEGIN PERFORM 1; END $$;\n"
            "CREATE RULE r AS ON INSERT TO t DO (SELECT 1; SELECT 2);\n"
            "SELECT x AS 'U  1' \\gset\n"
            "WITH c AS (:corpus) SELECT * FROM c;\n")]
        self.assertEqual(got, ["SELECT 'a;b' , 1::int FROM t",
                               'DO $$ BEGIN PERFORM 1; END $$',
                               'CREATE RULE r AS ON INSERT TO t DO '
                               '(SELECT 1; SELECT 2)',
                               "SELECT x AS 'U  1'", '\\gset',
                               'WITH c AS (:corpus) SELECT * FROM c'])

    def test_a_variable_is_sent_and_echoed_as_its_value(self):
        """A \\gset of a literal sets the variable to the literal's text,
        and `psql -e` echoes the statement with that text in place of
        `:name`; a \\gset prints nothing but its own echo; \\echo prints a
        variable's value between its quoted arguments."""
        items = psql_script("SELECT $c$\nVALUES (1)\n$c$ AS c,\n"
                            "  'it''s' AS s \\gset\n"
                            "WITH t AS (:c)\nSELECT * FROM t;\n"
                            "\\echo 'say' :s 'won''t'\n")
        table = [' column1 ', '---------', '       1', '(1 row)', '']
        echoed = ['SELECT $c$', 'VALUES (1)', '$c$ AS c,', "  'it''s' AS s",
                  'WITH t AS (', 'VALUES (1)', ')', 'SELECT * FROM t;']
        self.assertIsNone(match_output(echoed + table + ["say it's won't"],
                                       expected_output(items, True, {})))
        self.assertIsNone(match_output(table + ["say it's won't"],
                                       expected_output(items, False, {})))
        self.assertIn("echoed 'WITH t AS (:c)'", match_output(
            echoed[:4] + ['WITH t AS (:c)', 'SELECT * FROM t;'] + table
            + ["say it's won't"], expected_output(items, True, {})) or '')

    def test_an_if_prints_only_the_branch_its_server_took(self):
        """The probe's transcripts come from one server each, so each shows
        one branch of an \\if; this drives both, and the \\gset query that
        sets the variable, which prints nothing but its echo."""
        items = psql_script("SELECT v >= 15 AS new \\gset\n\\if :new\n"
                            "SELECT 1;\n\\else\n\\echo 'old'\n\\endif\n"
                            "\\echo 'end'\n")
        table = [' ?column? ', '----------', '        1', '(1 row)', '']
        for echo, lines_new, lines_old in (
                (False, table + ['end'], ['old', 'end']),
                (True, ['SELECT v >= 15 AS new', 'SELECT 1;'] + table
                 + ['end'], ['SELECT v >= 15 AS new', 'old', 'end'])):
            with self.subTest(echo=echo):
                new = expected_output(items, echo, {'new': True})
                old = expected_output(items, echo, {'new': False})
                self.assertIsNone(match_output(lines_new, new))
                self.assertIsNone(match_output(lines_old, old))
                self.assertIsNotNone(match_output(lines_old, new))
                self.assertIsNotNone(match_output(lines_new, old))

    def test_what_a_transcript_cannot_hide(self):
        """Each way a transcript can differ from what psql prints for the
        probe, refused by its message: a wrong tag, a table whose count
        is not its rows, a missing blank line, a line after the end, a \\!
        that printed nothing or more than one line, an \\if with no value
        given."""
        items = psql_script("\\! date\n\\echo 'a'\nSET x = 1;\n"
                            "SELECT 1\n  AS c;\n\\echo 'z'\n")
        ev = expected_output(items, False, {})
        good = ['Mon', 'a', 'SET', ' c ', '---', ' 1', '(1 row)', '', 'z']
        self.assertIsNone(match_output(good, ev))
        echoed = good[:2] + ['SET x = 1;'] + good[2:3] + [
            'SELECT 1', '  AS c;'] + good[3:]
        self.assertIsNone(match_output(echoed, expected_output(items, True,
                                                               {})))
        self.assertIn("echoed '  AS d;', the probe runs '  AS c;'",
                      match_output(echoed[:5] + ['  AS d;'] + echoed[6:],
                                   expected_output(items, True, {})) or '')
        for lines, message in (
                (good[:-1] + ['y'], "expected 'z'"),
                (good[:3] + good[5:], 'no result table'),
                (good[:2] + ['RESET'] + good[3:], "expected tag 'SET'"),
                (good[:6] + ['(2 rows)'] + good[7:], 'under 1 row line'),
                (good[:7] + good[8:], 'no blank line'),
                (good + ['SET'], 'printed after the last statement'),
                ([], 'the \\! printed nothing'),
                (good[:1] + ['Tue'] + good[1:], "['Tue'], expected 'a'")):
            with self.subTest(lines=lines):
                self.assertIn(message, match_output(lines, ev) or '')
        with self.assertRaisesRegex(AssertionError, 'no value was given'):
            expected_output(psql_script("SELECT 1 AS v \\gset\n\\if :v\n"
                                        "\\endif\n"), False, {})


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
        a `~16s`/`~17s` disagreement between two files, and "about a minute and
        a half" in three files while the suite took 208 s on the machine that
        measured it on 2026-09-21. A runtime is a fact
        about someone else's machine, so it is the reader's to measure.

        The unit is the WHOLE FILE that publishes the command, after two
        narrower scopes were measured full of holes by
        false-negative-reviewer. Three lines around the command missed a
        runtime eight lines under the block. The Markdown section then missed
        a `#` comment line after the last command (split off as a heading), a
        figure in backticks, and a `### How long` sub-heading right under the
        block -- the heading somebody actually writes. Each fix was the form
        the author had seen rather than the family, so
        the scope is now the file: the files that publish this command
        publish no duration at all, measured, and the fourth escape route was
        closed by deleting the exemption rather than by widening it again.

        The cost is that a duration about something else in one of those
        files fires too. That is deliberate and the message says what
        to do about it: the runner's own header is where a measurement
        belongs, with its date and its machine.
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
        # Two more places read as being about the suite without printing the
        # command. README.md, whole: its Tests section moved to
        # tests/README.md and it still sends readers there to run them. And
        # any section whose heading names the tests, to its next heading of
        # the same level or above: docs/requirements.md's "The test suite"
        # took a duration and passed while README was named file by file.
        # Headings are read outside fenced code, where a `# comment` would
        # otherwise end a section early and hide what follows.
        heading = re.compile(r'^(#{1,6})\s')
        about_tests = re.compile(r'(?i)^#{1,6}\s.*\b(?:test suite|tests)\b')
        published = docs()
        self.assertIn('README.md', published)
        for name, text in published.items():
            lines = text.split('\n')
            levels, fenced = [], False
            for line in lines:
                if line.startswith('```'):
                    fenced = not fenced
                found = None if fenced else heading.match(line)
                levels.append(len(found.group(1)) if found else None)
            if command.search(text) or name == 'README.md':
                scan = range(len(lines))
            else:
                scan = []
                for i, line in enumerate(lines):
                    if levels[i] is None or not about_tests.match(line):
                        continue
                    end = next((j for j in range(i + 1, len(lines))
                                if levels[j] is not None
                                and levels[j] <= levels[i]), len(lines))
                    scan += range(i, end)
            for i in scan:
                found = duration.search(lines[i])
                if found is not None:
                    self.fail(
                        f'{name}:{i + 1} states a duration '
                        f'({found.group(0)!r} in {lines[i].strip()!r}) where '
                        f'a reader will take it for the test suite\'s: in a '
                        f'file that publishes the suite command, in README.md, '
                        f'or in a section about the tests. Drop the figure, '
                        f'or move the sentence out of those; a measurement '
                        f'belongs in tests/run_parallel.py\'s header, with its '
                        f'date and its machine')

    def test_the_layer_counts_come_from_the_files(self):
        """Which layers need the glibc clone is stated twice in tests/README.md
        -- the Layers table's yes/mostly/no column, and the paragraph under it
        that names the layers that still run without one -- and once more in
        the CI comment. All three are hand-written statements of a fact the
        filesystem already holds.

        Written the day the eighth layer became the ninth, which moved a count
        in one file, added a row in a second and a filename list in a third --
        by hand, with nothing to catch the fourth place. The CI comment was the
        one that had already gone stale. Until 2026-09-27 the count was read
        from docs/requirements.md, which then stopped stating it; tests/README.md
        is the page that describes the layers.

        Which layers need the clone is measured, not read: the suite is
        loaded in a fresh interpreter with the clone made to look absent, and
        each module's tests that a skip decorator marks for it, on the class
        or on the method, are counted. Reading the source for the decorators
        told "needs it" from "does not" and could not tell "yes" from
        "mostly": test_provenance.py kept its "yes" after one of its classes
        stopped needing the clone, with this test green. A skip decided any
        other way is not counted; the check below refuses only the two names
        of _harness such a skip would most likely use.
        """
        tests_dir = os.path.join(REPO_ROOT, 'tests')
        modules = sorted(name for name in os.listdir(tests_dir)
                         if name.startswith('test_') and name.endswith('.py'))
        self.assertGreater(len(modules), 1, 'no test modules found at all')
        counts = tests_without_a_clone(tests_dir)
        self.assertEqual(set(counts), set(modules),
                         'the suite loaded without a clone and tests/ do not '
                         'hold the same modules')
        needs = {name: 'no' if skipped == 0 else 'yes' if ran == 0
                 else 'mostly' for name, (ran, skipped) in counts.items()}
        free = {name for name in modules if needs[name] == 'no'}
        partly = {name for name in modules if needs[name] == 'mostly'}
        # Built in two pieces so that the probe above, which needs both
        # names, is the one place that spells them.
        asks = ('have_' + 'clone', '_SKIP_' + 'NO_CLONE')
        for name in modules:
            if name == 'test_published_claims.py':
                continue
            text = read(os.path.join(tests_dir, name))
            self.assertEqual([w for w in asks if w in text], [],
                             f'{name} asks for the clone outside the skip '
                             f'decorators, where the count cannot see it')

        # The ROWS, not the file: asserting that each filename appears
        # somewhere in tests/README.md passed with the row deleted, because
        # the paragraph under the table names some of the modules too.
        # Measured with a mutant that renamed a row.
        table = docs()[os.path.join('tests', 'README.md')]
        found = re.findall(r'(?m)^\| `(test_\w+\.py)` \| (\w+) \|', table)
        twice = sorted(name for name, n in
                       collections.Counter(name for name, _ in found).items()
                       if n > 1)
        self.assertEqual(twice, [], f'the Layers table has more than one row '
                                    f'for {twice}; the test would read one')
        rows = dict(found)
        self.assertEqual(set(rows), set(modules),
                         'the Layers table in tests/README.md and tests/ do '
                         'not hold the same modules')
        for name in modules:
            with self.subTest(layer=name):
                ran, skipped = counts[name]
                self.assertEqual(rows[name], needs[name],
                                 f'{name}: the table says {rows[name]!r} '
                                 f'about the clone, and without one {ran} of '
                                 f'its tests run and {skipped} skip')

        self.assertEqual(table.count('Without a clone at `scripts/glibc`'), 1,
                         'tests/README.md does not say once which layers run '
                         'without a clone')
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
        # 2026-09-27). The modules that run in part are named after "the
        # classes of", and a module named before it is one that runs whole.
        halves = paragraph.group(0).split('the classes of')
        self.assertEqual(len(halves), 2, 'the paragraph under the Layers '
                         'table does not say "the classes of" once')
        whole, part = (set(re.findall(r'`(test_\w+\.py)`', h))
                       for h in halves)
        self.assertEqual((whole, part), (free, partly),
                         'the layers tests/README.md says still run without a '
                         'clone are not the ones that do')
        # The CI comment the same way, both directions, cut at both ends.
        workflow = read(os.path.join(REPO_ROOT, '.github', 'workflows',
                                     'tests.yml'))
        self.assertEqual(workflow.count('# Run once'), 1,
                         'the CI comment on running the suite is not there '
                         'once')
        rest = workflow.split('# Run once')[1]
        self.assertIn('run: |', rest, 'the CI comment has no step under it')
        halves = rest.split('run: |')[0].split('the classes of')
        self.assertEqual(len(halves), 2, 'the CI comment does not say "the '
                         'classes of" once')
        whole, part = ({n + '.py' for n in re.findall(r'\btest_\w+', h)}
                       for h in halves)
        self.assertEqual((whole, part), (free, partly),
                         'the CI comment does not name what a broken clone '
                         'step would leave running alone')

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
        property of the source text.
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


class AuditShPipesNothingIntoAQuietGrep(unittest.TestCase):
    """Backlog 13.7, found by doc-sweep in a regenerated example. audit.sh
    runs under `set -o pipefail`, and `grep -q` exits at its first match:
    whatever writes into the pipe then dies of SIGPIPE, the pipeline fails,
    and the test reads "no match". `cat lists | grep -qx C` left the
    C.UTF-8 note out of a published transcript at random -- measured 158
    prints in 200 runs with three lists. A race no wrapper run can catch
    reliably, so the source is read for the shape instead."""

    def test_no_pipe_ends_in_grep_q(self):
        with open(os.path.join(REPO_ROOT, 'audit.sh'), encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn('set -euo pipefail', source)
        shape = re.compile(r'\|\s*grep\b[^|;&]*\s-(-quiet\b|-silent\b|[A-Za-z]*q)')
        hits = [line.strip() for line in source.splitlines()
                if shape.search(line) and not line.lstrip().startswith('#')]
        self.assertEqual(hits, [])

    def test_the_control_the_shape_is_found(self):
        shape = re.compile(r'\|\s*grep\b[^|;&]*\s-(-quiet\b|-silent\b|[A-Za-z]*q)')
        for line in ('cat a | grep -qx C', 'x | grep -e A -q', 'y |grep -q z',
                     'z | grep --quiet -x C'):
            with self.subTest(line=line):
                self.assertIsNotNone(shape.search(line))


class Place(collections.namedtuple('Place', 'path section start end')):
    r"""Where a page states one command's PostgreSQL floor: in `path`, under
    the heading line `section` (None: the text above the first `## `),
    from the one match of the regex `start` (None: the section's first
    paragraph) to the first match of `end` after it. The anchors name the
    command and the part of the page, never the words of the sentence, so
    a sentence reworded is still read, and one moved out of its place is
    refused instead of counted for the other command."""


def place_text(place):
    return place_span(read(os.path.join(REPO_ROOT, *place.path.split('/'))),
                      place)


def place_span(text, place):
    """The span of `place` in `text`, flat. Refuses a section missing or
    doubled, a start missing or doubled, and an end that never comes: each
    would otherwise read some other part of the page, or nothing."""
    # Offsets kept: a `#` inside a fenced block is a shell comment.
    blank = re.sub(r'```.*?```', lambda m: re.sub(r'[^\n]', ' ', m.group(0)),
                   text, flags=re.S)
    heads = list(re.finditer(r'^#+ .*$', blank, re.M))
    if place.section is None:
        lo = 0
        hi = next((m.start() for m in heads if m.group(0).startswith('## ')),
                  len(text))
    else:
        mine = [k for k, m in enumerate(heads)
                if m.group(0) == place.section]
        if len(mine) != 1:
            raise AssertionError(f'{place.path}: heading {place.section!r} '
                                 f'found {len(mine)} times, once expected')
        lo = heads[mine[0]].end()
        hi = (heads[mine[0] + 1].start() if mine[0] + 1 < len(heads)
              else len(text))
    region = text[lo:hi]
    if place.start is None:
        body = region.strip('\n')
        if not body:
            raise AssertionError(f'{place.path}: nothing under '
                                 f'{place.section!r}')
        return flat(body.split('\n\n')[0])
    starts = list(re.finditer(place.start, region))
    if len(starts) != 1:
        raise AssertionError(f'{place.path}: {place.start!r} found '
                             f'{len(starts)} times under {place.section!r}, '
                             f'once expected')
    end = re.compile(place.end).search(region, starts[0].end())
    if not end:
        raise AssertionError(f'{place.path}: nothing ends the span that '
                             f'starts at {place.start!r}')
    return flat(region[starts[0].start():end.start()])


#: A PostgreSQL floor in any wording these pages use for one. Every match
#: in the published pages must sit inside a place a row reads.
FLOOR = r'(?:PostgreSQL|[Vv]ersion)\W+(\d+)(?:\.\d+)?\W+or (?:newer|later)'


def end_line_problem(items, branch):
    """None if exactly one `\\echo` line psql prints for `items` (with the
    \\if values in `branch`) starts `=== done`, and it is the last thing
    printed; else what is wrong. The docs' `grep -L '^=== done'` names a
    stopped run only while that holds."""
    events = expected_output(items, False, branch)
    done = [event for event in events
            if event[0] == 'line' and event[1].startswith('=== done')]
    if len(done) != 1:
        return f'{len(done)} printed lines start === done, one expected'
    if events[-1] != done[0]:
        return 'the === done line is not the last thing printed'
    return None


def floors_no_row_reads(pages, places, text_of):
    """(page, floors it states, floors its places hold) for every page where
    the two counts differ: a floor sentence outside every place is read by
    no row. A place may state the floor of two commands and sit in both
    rows; it is one statement on its page, so each place counts once."""
    held = collections.Counter()
    for place in places:
        held[place.path] += len(re.findall(FLOOR, text_of(place)))
    return [(name, len(re.findall(FLOOR, flat(text))), held[name])
            for name, text in sorted(pages.items())
            if len(re.findall(FLOOR, flat(text))) != held[name]]


def floor_of(span):
    """The one PostgreSQL version `span` names, in whatever words, or a
    refusal: none, or two, is not one floor."""
    found = re.findall(r'(?:PostgreSQL|[Vv]ersion)\W+(\d+)', span)
    if len(found) != 1:
        raise AssertionError(f'{len(found)} PostgreSQL version(s) where one '
                             f'floor is expected: {found} in {span[:80]!r}')
    return found[0]


class AFigureStatedTwiceIsStatedOnce(unittest.TestCase):
    r"""A number the docs publish, tied to every page that states it and,
    where there is one, to the run it came from.

    The rot this catches is the cheapest kind to cause: change a measurement on
    the page you are editing and leave the others saying the old one. Nothing
    announces it, because each page is internally consistent. Measured
    2026-09-22 across the published Markdown: 117 figures no test asserts AS
    PUBLISHED. Ten of them became rows of this table, six tied that morning and
    four more later the same day; most have since come out. "As published" is
    the whole of it: two of those four, 335 and 342, were already asserted
    against a live run by `test_known_answers` and the node tests of the
    time, which read the tool's OUTPUT. Neither compared the page with the run, so the page
    was free to say anything. The ones that appear in more than one file are
    the ones that can drift -- a measurement quoted once is right or wrong,
    never inconsistent, which is why a row stated in a single file earns its
    place through the transcript under it rather than through agreement with
    another page.

    Adding a figure is one row: the label, a pattern whose branches have one
    group each, how many FILES must state it, and an `Evidence` per file whose
    transcript has to carry the same number -- a path alone for the bare
    check, or the sentence and, where a transcript repeats itself, the section
    to read it in. The patterns run over `flat()` text, because a sentence
    can wrap inside its phrase.

    **Files, not mentions.** The floor counted occurrences until
    false-negative-reviewer measured what that allows: rewrite
    docs/requirements.md -- the page whose job is stating the requirement --
    to a DIFFERENT floor in a wording no pattern reaches, add one more correct
    mention to README.md, and the total still agrees with itself. Several of
    the rows the table held on 2026-09-22 had most of their mentions inside
    one file, so a total is exactly the wrong denominator.

    **A pattern is anchored to the phrase, not just to the digits.** The same
    pass measured `(\d{3}) at glibc 2.34` capturing the last three digits of
    `1355`, so two pages published different numbers and this test reported
    agreement -- and firing on `342 at glibc 2.34`, a real figure of this
    project in a sentence somebody may legitimately write. Every row now
    carries enough of its sentence to mean one thing, and `\d+` rather than a
    fixed width, so a longer number fails loudly instead of matching in part.

    An evidence file is named only where the number really appears in the
    output of a run, and only where it is the SAME fact:

      * a file carrying the figure in prose and nowhere else is not listed:
        comma-stripped, the check would find the prose it was meant to be
        independent of and pass whatever the measurement said -- a vacuous
        assertion.
      * neither is a file whose bare number is a DIFFERENT fact that happens
        to agree, such as the complement of a count on one fixture. Tying it
        would tie two facts that agree by arithmetic accident.

    What the BARE form asks is whether the transcript still carries the number,
    so it catches a page re-measured away from the prose and does NOT catch one
    cell of three edited by hand while the others keep the old value. That is
    the honest limit of an existence check, written down here rather than left
    for a reader to discover: the mutation that proves such a tie changes every
    occurrence, because that is the case it can see. Two rows kept it, carrying
    9,616 and 9,619, until PR #56 removed them. What made those two safe was
    not that the counts were longer but a measurement: bumping each by one
    reddened it, because neither named file carried the bumped value. That is
    narrower than it sounds, and deliberately stated so: a file that states
    BOTH counts cannot tell a drift from one to the other, and what reddens
    such a row is the file carrying only its own. An evidence tie is only as
    good as the narrowest file under it.

    **A row may instead name the sentence its number appears in, and the part
    of the transcript that produced it.** Both halves were paid for on
    2026-09-22, in that order. The bare check first: bumping 342 in the prose
    to 343 stayed GREEN, because the same transcript states 343 six hundred
    lines down -- the el9 NODE's `356, of which 343 define LC_COLLATE` against
    the 2.34 TAG's `355, of which 342`. Naming the sentence closed that row and
    did NOT close 331 and 335, which false-negative-reviewer then measured on
    the fix: the el9 node is built from the 2.34 tag, so the node scan repeats
    step 4's copy-closure figures in BYTE-IDENTICAL sentences. A sentence two
    facts share is not an anchor. Those three rows named the banner of step 4
    at the tag; they came out on 2026-09-27, after the page that stated their
    figures was deleted, and the mechanism stays for the next row that needs
    it: the search is cut at the next banner, and an anchor that is missing or
    doubled refuses instead of widening to the whole file. A named sentence
    must appear exactly ONCE in what is left: two copies mean a drifting one is
    satisfied by a stale one, which is this test's own defect class turned on
    itself. The bare form keeps "at least once", because a transcript may state
    a number twice for good reason.

    `least` is not decoration. Without it, deleting every mention leaves this
    test green over a claim that no longer exists, which is "absent is not
    empty" one level up: it would then assert agreement among
    nothing. If a figure is deliberately dropped from the docs, its row comes
    out of this table in the same commit.
    """

    FIGURES = (
        # The two PostgreSQL floors are read where each page states them
        # for that command (16.224): told apart by wording, command 2
        # reworded as "runs on PostgreSQL 14 or newer" was counted as
        # command 3's, and a page was counted once however many times it
        # stated the floor. One sentence that gives the floor of both
        # commands is a place in both rows.
        ('the PostgreSQL floor of command 2, the C.UTF-8 probe', (
            Place('README.md', '### Prerequisites',
                  r'\n- for commands 2 and 3\b', r'\n(?:- |\n)'),
            Place('docs/requirements.md', '## For the confirmation step',
                  r'\b[Cc]ommands 2 and 3\b', r'\n\n')), None, ()),
        ('the PostgreSQL floor of command 3, the template', (
            Place('README.md', '### Prerequisites',
                  r'\n- for commands 2 and 3\b', r'\n(?:- |\n)'),
            Place('README.md', '### The commands', r'\*\*3 — ', r'```'),
            Place('docs/requirements.md', '## For the confirmation step',
                  r'\b[Cc]ommands 2 and 3\b', r'\n\n')), None, ()),
        # The locales that inherit iso14651_t1 (328) and the locale files
        # that differ between glibc 2.28 and 2.34 (283) were rows here, held
        # to step 1's output; step 9.5 deleted it, and the docs' sentences
        # that stated them went with it, a count no run states.
    )

    @staticmethod
    def _value(found):
        return next(group for group in found.groups() if group)

    def test_every_page_that_states_it_states_the_same_one(self):
        for label, pattern, least, evidence in self.FIGURES:
            with self.subTest(figure=label):
                if isinstance(pattern, tuple):
                    self.assertIsNone(least, f'{label}: a row that names '
                                      f'its places takes no file count')
                    self.check_places(label, pattern)
                    continue
                self.assertIsInstance(least, int, label)
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

    def check_places(self, label, places, text_of=None):
        """Each place states one number, and every place the same one.
        `text_of` reads a place (place_text, the published page); the test
        of this check hands it pages of its own."""
        text_of = text_of or place_text
        self.assertTrue(places, label)
        seen = {}
        for place in places:
            try:
                floor = floor_of(text_of(place))
            except AssertionError as e:
                self.fail(f'{label}: {place.path} under {place.section!r}: '
                          f'{e}')
            seen.setdefault(floor, []).append(place.path)
        self.assertEqual(len(seen), 1, f'{label} is published as more than '
                                       f'one number: {seen}')

    def test_every_floor_the_pages_state_is_read_by_a_row(self):
        """A floor sentence outside every place would be read by no row:
        per page, the floors stated must be the ones its places hold."""
        places = {place for _, pattern, _, _ in self.FIGURES
                  if isinstance(pattern, tuple) for place in pattern}
        self.assertTrue(places, 'no row names a place')
        pages = {name.replace(os.sep, '/'): text
                 for name, text in docs().items()}
        self.assertEqual(floors_no_row_reads(pages, places, place_text), [])

    def test_the_floor_checks_see_what_they_are_for(self):
        """Both checks on pages of their own, since the published pages
        agree and hold no stray floor, and so never reach the branch that
        refuses (11.3's battery: dropping either refusal stayed green)."""
        pages = {'p': '## A\n\n- for command 2, PostgreSQL 14 or newer\n'
                      '\n## B\n\nIt needs PostgreSQL 15 or newer.\n'}

        def text_of(place):
            return place_span(pages[place.path], place)
        two = (Place('p', '## A', r'\n- for command 2\b', r'\n(?:- |\n)'),
               Place('p', '## B', None, None))
        with self.assertRaisesRegex(AssertionError,
                                    'published as more than one number'):
            self.check_places('floor', two, text_of)
        self.check_places('floor', two[:1], text_of)
        self.assertEqual(floors_no_row_reads(pages, set(two), text_of), [])
        stray = floors_no_row_reads(pages, set(two[:1]), text_of)
        self.assertEqual(stray, [('p', 2, 1)])

    def test_a_place_refuses_what_it_cannot_read(self):
        """Each refusal of place_span and floor_of by its message, with the
        control that reads the span."""
        page = ('# T\n\nIntro.\n\n## A\n\n- for command 2, PostgreSQL 15 '
                'or newer\n- other\n\n```sh\n# a comment\n```\n\n## B\n')
        good = Place('p', '## A', r'\n- for command 2\b', r'\n(?:- |\n)')
        self.assertEqual(place_span(page, good),
                         '- for command 2, PostgreSQL 15 or newer')
        self.assertEqual(place_span(page, Place('p', None, r'Intro', r'\n')),
                         'Intro.')
        self.assertEqual(place_span(page, Place('p', '## A', None, None)),
                         '- for command 2, PostgreSQL 15 or newer - other')
        for text, place, message in (
                (page + '## A\n', good, 'found 2 times, once expected'),
                (page, good._replace(start='command 9'), 'found 0 times'),
                (page.replace('- other', '- for command 2 again'), good,
                 'found 2 times under'),
                (page, good._replace(end='NOPE'), 'nothing ends the span')):
            with self.subTest(message=message):
                with self.assertRaisesRegex(AssertionError, message):
                    place_span(text, place)
        self.assertEqual(floor_of('needs **version 15 or newer**'), '15')
        for span in ('runs on PostgreSQL release 14', 'PostgreSQL 14 or '
                     'newer, PostgreSQL 13 with a patch'):
            with self.subTest(span=span):
                with self.assertRaisesRegex(AssertionError,
                                            'where one floor is expected'):
                    floor_of(span)

    def test_the_evidence_under_it_carries_the_same_number(self):
        """The prose figure against the output it summarises, which is where
        this project's numbers come from. Prose said 9,616 and the psql
        transcript in the case file said 9616, while a row still tied the
        two.

        Each row, and each file under it, is its own subtest. Otherwise a
        first failure would hide every row and file after it, and a mutation
        battery reading the result could not tell which tie failed."""
        for label, pattern, _, evidence in self.FIGURES:
            if not evidence:
                continue
            with self.subTest(figure=label):
                values = {self._value(found) for text in docs().values()
                          for found in re.finditer(pattern, flat(text))}
                self.assertEqual(len(values), 1, f'{label}: {values}')
                bare = values.pop().replace(',', '')
                for entry in evidence:
                    ev = (entry if isinstance(entry, Evidence)
                          else Evidence(entry))
                    with self.subTest(path=ev.path):
                        self.check_evidence(label, bare, ev)

    def check_evidence(self, label, bare, ev):
        """One file under one row. Does it state `bare` the way `ev` says?"""
        # A shape that lost its placeholder would join nothing into
        # nothing and quietly become "does this sentence appear",
        # never looking at the number -- and the failure message
        # would print the sentence without it. Found by
        # false-negative-reviewer.
        self.assertIn(
            '{n}', ev.shape,
            f'{label}: an evidence shape with no {{n}} checks the '
            f'sentence and never the number')
        # Not `shape == '{n}'`: one space walked past the first
        # version of this guard -- measured GREEN on a page drifted
        # from 335 to 327. A shape with no letters outside the
        # placeholder names no sentence, however it is spelled.
        self.assertFalse(
            ev.section is not None
            and not re.search(r'[A-Za-z]',
                              ev.shape.replace('{n}', '')),
            f'{label}: a bare number narrowed to a section is neither '
            f'check. Name the sentence instead')
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
                f'different answers. Some files in examples/ carry no '
                f'banner at all')
            text = region_after(text, ev.section)
        # Digit boundaries: `9616` is otherwise satisfied by a 19616
        # or a 96160 sitting anywhere in the file. The escape makes a
        # shape's `(s)` and `.` literal.
        found, sentences = evidence_counts(ev.shape, bare, text)
        agrees = evidence_agrees(ev.shape, bare, text)
        where = ('' if ev.section is None
                 else f' in the section under "{ev.section}"')
        said = repr(ev.shape.replace('{n}', bare))
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
                f'{found} time(s); the same sentence with any '
                f'number appears {sentences} time(s). Both '
                f'counts must be 1')


class EveryTieWouldNoticeItsFigureMoving(unittest.TestCase):
    """The mutation battery, in the repository instead of in a transcript.

    A row of the table above asserts that the pages agree with each other and
    with the run behind them. Nothing asserted that the row would NOTICE the
    figure moving, and the first row measured for it did not: 342 was tied to
    a whole transcript that states 343 six hundred lines down, as the el9
    node's own count, so the tie agreed with the page and would have agreed
    with a page saying 343. Finding that cost a review round, and the proof
    was a battery of file mutations run once, by hand, in a scratch directory
    nobody else can see -- so "thirty-eight red" was a sentence and not a
    mechanism, which is the shape of defect this whole layer
    exists to refuse.

    This asks that battery's question without touching a file: if the page
    said one more than it says, would every file under it still confirm? A row
    that answers yes cannot tell its figure from its neighbour, and is
    decoration. Measured 2026-09-22 by putting the 342 row back the way it
    first shipped -- bare, against the whole transcript -- which reddened this.

    It is one guard of several and does not stand in for the others. A
    sentence that two facts in one file share, and a shape too weak for the
    region it is narrowed to, are refused where the tie is checked rather than
    here.

    What it CANNOT see is a figure with no run behind it. For such a row a
    drift is invisible as long as every page drifts together -- it is tied to
    the other pages and to nothing else. That is a real limit of the table,
    so the rows it applies to are named below rather than left to be
    discovered: adding one means writing it in, which is a decision, not an
    omission.
    """

    #: Rows tied only to the other pages that state them. The PostgreSQL
    #: floors are requirements rather than measurements. The 6,525 row was
    #: here until docs/limitations.md stopped publishing evidence; it was the
    #: one measured figure that could not be tied -- prose and no query
    #: output -- and with no page this layer reads stating it, there is
    #: nothing left for this class to compare.
    NO_RUN_BEHIND_THEM = (
        'the PostgreSQL floor of command 2, the C.UTF-8 probe',
        'the PostgreSQL floor of command 3, the template',
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
        copy at once, and that is the battery that cannot see this defect: it
        was the per-LINE version that found a sentence
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

        Measured 2026-09-22, on the table as it stood then: every row with
        evidence had such a move, and reverting any of the three sectioned
        rows to the whole-file form it first shipped in reddened this -- where
        the earlier version reddened for one of the three.
        """
        for label, pattern, _, evidence in self.rows():
            if not evidence:
                continue
            with self.subTest(figure=label):
                self.check_moves(label, self.published(pattern), evidence)

    def check_moves(self, label, bare, evidence):
        """One row. Does moving one statement of `bare`, on its own, break
        the tie?"""
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


class ASectionNeverWidensInSilence(unittest.TestCase):
    """The refusals that keep an evidence section from reading more than it
    names, driven directly.

    A row reaches them only when it names a section, so they are exercised
    here rather than left to the next row that needs one. A guard nothing
    reaches is a guard nobody would see break (false-negative-reviewer,
    2026-09-30). Each mechanism has a control that shows it still agrees
    where it should, and the cut has one on a sentence the transcript states
    more than once, which is the case a section exists for.
    """

    AUDIT = 'examples/rhel8-to-rhel9-audit-output.txt'
    # Between the two machines' builds, which this transcript's step 2
    # reads: the names it compared (step 9.2).
    SHAPE = '{n} name(s), locale files and locale.alias aliases:'
    STEP_2 = ("== STEP 2  Which locales' sort rules changed\n"
              + '=' * 64)
    # Step 11 prints it in its report, and its summary block repeats it word
    # for word (step 9.4b deleted step 8, and step 9.3 step 4, whose
    # sentences this test used before).
    REPEATED = 'Of the {n} locale(s) on the old machine:'
    STEP_11 = ("== STEP 11  MEASURED ORDER  how each machine's own glibc "
               "sorts every locale\n" + '=' * 64)
    PROBE = 'examples/c-utf8-probe-rhel8-vs-rhel9.txt'

    def check(self, ev, bare='400'):
        row = AFigureStatedTwiceIsStatedOnce(
            'test_the_evidence_under_it_carries_the_same_number')
        row.check_evidence('a row written for this test', bare, ev)

    def test_a_named_sentence_inside_its_section_agrees(self):
        self.check(Evidence(self.AUDIT, shape=self.SHAPE, section=self.STEP_2))

    def test_the_cut_is_what_keeps_a_repeated_sentence_apart(self):
        with self.assertRaisesRegex(AssertionError, 'Both counts must be 1'):
            self.check(Evidence(self.AUDIT, shape=self.REPEATED), '867')
        self.check(Evidence(self.AUDIT, shape=self.REPEATED,
                            section=self.STEP_11), '867')

    def test_a_missing_anchor_refuses(self):
        with self.assertRaisesRegex(AssertionError,
                                    r'section anchor .* appears 0 time\(s\)'):
            self.check(Evidence(self.AUDIT, shape=self.SHAPE,
                                section='an anchor no transcript prints'))

    def test_a_doubled_anchor_refuses(self):
        with self.assertRaisesRegex(
                AssertionError,
                r'section anchor .* appears ([2-9]|\d{2,}) time\(s\)'):
            # Said under 'What was compared' and repeated in the warnings.
            self.check(Evidence(self.AUDIT, shape=self.SHAPE,
                                section='Not compared on any route'))

    def test_a_section_nothing_closes_refuses(self):
        # The probe transcripts print no `====` banner at all.
        with self.assertRaisesRegex(AssertionError,
                                    'nothing closes the section'):
            self.check(Evidence(self.PROBE, shape='rows {n}',
                                section='=== 1. the order'))

    def test_a_bare_number_narrowed_to_a_section_refuses(self):
        # The spaced spelling, because it is the one that walked past the
        # first version of this guard.
        with self.assertRaisesRegex(AssertionError, 'neither check'):
            self.check(Evidence(self.AUDIT, shape=' {n}',
                                section=self.STEP_2))

    def test_region_after_refuses_a_missing_or_doubled_anchor(self):
        for text in ('no anchor here', 'A\nA\n'):
            with self.subTest(text=text), \
                    self.assertRaisesRegex(ValueError, 'once expected'):
                region_after(text, 'A')

    def test_region_after_refuses_a_region_nothing_closes(self):
        with self.assertRaisesRegex(ValueError, 'nothing closes'):
            region_after('A\nbody\n', 'A')

    def test_region_after_cuts_at_the_next_banner(self):
        self.assertEqual(region_after('A\nbody\n==== next\nmore', 'A'),
                         '\nbody')


class TheBareFormAndTheMoveStillBite(unittest.TestCase):
    """The bare-figure check and the one-move check, driven directly.

    Every row of the figures table names its sentence, so no row reaches the
    bare form, and for a named sentence the one-move check is implied by the
    evidence check. Neither would be seen to break (false-negative-reviewer,
    2026-09-30), so each is driven here on the real transcript, next to a
    control that shows it still agrees where it should. The transcript states
    869 once (the new machine's locales, step 11) and 335 twice.
    """

    AUDIT = 'examples/rhel8-to-rhel9-audit-output.txt'
    LABEL = 'a row written for this test'

    def evidence_row(self):
        return AFigureStatedTwiceIsStatedOnce(
            'test_the_evidence_under_it_carries_the_same_number')

    def move_row(self):
        return EveryTieWouldNoticeItsFigureMoving(
            'test_moving_one_statement_of_a_figure_breaks_the_tie')

    def test_a_bare_figure_the_transcript_carries_agrees(self):
        self.evidence_row().check_evidence(self.LABEL, '869',
                                           Evidence(self.AUDIT))

    def test_a_bare_figure_the_transcript_lacks_refuses(self):
        with self.assertRaisesRegex(AssertionError,
                                    'does not contain that number anywhere'):
            self.evidence_row().check_evidence(self.LABEL, '99999',
                                               Evidence(self.AUDIT))

    def test_a_figure_stated_once_breaks_when_it_moves(self):
        self.move_row().check_moves(self.LABEL, '869', (Evidence(self.AUDIT),))

    def test_a_bare_figure_stated_twice_refuses_the_move(self):
        with self.assertRaisesRegex(AssertionError,
                                    'leaves the tie agreeing'):
            self.move_row().check_moves(self.LABEL, '335',
                                        (Evidence(self.AUDIT),))


class TheVerdictTableIsTheSameInBothPlaces(unittest.TestCase):
    """The verdict table is published twice: in the README, and at the top of
    docs/results.md. Until 2026-09-27 it lived in the README alone, so that
    there was one copy to keep current; that day Pablo decided the page named
    Results has to show the results, and the README has to keep them too.
    Two copies drift the day a verdict moves and only one page is edited, and
    each page still reads as consistent with itself, so no re-read notices.

    Each copy is found by its section and its header row, never by its cells,
    and read the way GitHub delimits a table: the row above a delimiter row,
    and every line below it down to the first blank line, a blank line being
    one of spaces or tabs only. The first version read the lines that start
    with `|`, and false-negative-reviewer measured what that let through on
    GitHub the same day: a row written without its leading pipe, or the note
    under the table with the blank line above it deleted, showed as one more
    row of one copy while the comparison stayed green.

    So that the rows compared are the rows a reader sees, it refuses: a
    missing or doubled heading; a section nothing closes; no table, or a
    second one, under the heading; a table with no header row, or glued to
    the line above it; a line of the table that does not start with `|`; any
    other line of the section with a `|` in it; a table that is not the
    verdict table, or has no rows; and, anywhere else in the file, a line
    with a `|` that carries a verdict (one of the four marks, or Changed,
    Unaffected, No difference), or an HTML `<table>`.

    It does not try to see a copy hidden whole (an HTML comment, a `<pre>` or
    a code fence left open above the heading, a `<details>` around the
    table), nor a table written without a single `|` outside the section.
    Telling every such case apart would mean rebuilding GitHub's parser, and
    Pablo chose not to on 2026-09-27.
    """

    PLACES = (('README.md', '## Results for the two RHEL pairs'),
              (os.path.join('docs', 'results.md'), '## The answer'))

    # A delimiter row, read a little more loosely than GitHub does: cells of
    # dashes with optional colons, the outer pipes optional. Taking a line
    # for a delimiter that GitHub would not can only make this refuse.
    DELIMITER = re.compile(r' {0,3}\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*')
    # What makes a row a verdict row, whatever its header says: the marks the
    # table uses, or the words beside them.
    VERDICT = re.compile('[\U0001F534\U0001F7E1\U0001F7E2⚪]'
                         '|Changed|Unaffected|No difference')

    @classmethod
    def table(cls, text, heading):
        """The lines of the one verdict table under `heading`, or a refusal."""
        lines = text.split('\n')
        starts = [i for i, line in enumerate(lines) if line == heading]
        if len(starts) != 1:
            raise AssertionError(
                f'{heading!r} appears {len(starts)} time(s), once expected')
        rest = lines[starts[0] + 1:]
        ends = [i for i, line in enumerate(rest) if line.startswith('## ')]
        if not ends:
            raise AssertionError(
                f'nothing closes the section under {heading!r}, so reading it '
                f'would run to the end of the file')
        section = rest[:ends[0]]
        delimiters = [i for i, line in enumerate(section)
                      if ('|' in line or ':' in line)
                      and cls.DELIMITER.fullmatch(line)]
        if len(delimiters) != 1:
            raise AssertionError(
                f'{len(delimiters)} table(s) under {heading!r}, one expected')
        top = delimiters[0] - 1
        if top < 0 or not section[top].strip(' \t'):
            raise AssertionError(
                f'the table under {heading!r} has no header row')
        if top > 0 and section[top - 1].strip(' \t'):
            raise AssertionError(
                f'the table under {heading!r} is glued to the line above it, '
                f'{section[top - 1]!r}')
        bottom = next((i for i in range(delimiters[0], len(section))
                       if not section[i].strip(' \t')), len(section))
        rows = section[top:bottom]
        odd = [row for row in rows if not row.startswith('|')]
        if odd:
            raise AssertionError(
                f'GitHub shows {odd[0]!r} as part of the table under '
                f'{heading!r}, and it does not start with "|"')
        stray = [line for line in section[:top] + section[bottom:]
                 if '|' in line]
        if stray:
            raise AssertionError(
                f'{stray[0]!r} sits outside the table under {heading!r} and '
                f'has a "|" in it, so a reader can take it for a row')
        if not rows[0].startswith('| Locale |'):
            raise AssertionError(
                f'the table under {heading!r} is not the verdict table: '
                f'{rows[0]!r}')
        if len(rows) < 3:
            raise AssertionError(
                f'the verdict table under {heading!r} has no rows')
        # Last, so that each case above keeps its own message. The table's
        # own lines are left out by position, not by content, so an exact
        # second copy elsewhere is still seen.
        first = starts[0] + 1 + top
        own = range(first, first + len(rows))
        elsewhere = [line for i, line in enumerate(lines)
                     if i not in own and '|' in line
                     and cls.VERDICT.search(line)]
        if elsewhere:
            raise AssertionError(
                f'{elsewhere[0]!r} carries a verdict outside the table under '
                f'{heading!r}: a second copy, or a row a reader can take for '
                f'one')
        if any('<table' in line.lower() for line in lines):
            raise AssertionError(
                'an HTML table in this file can carry another copy')
        return rows

    def test_both_pages_publish_the_same_table(self):
        (first, a), (second, b) = (
            (name, self.table(read(os.path.join(REPO_ROOT, name)), heading))
            for name, heading in self.PLACES)
        self.assertEqual(
            a, b, f'the verdict table in {first} and in {second} differ; a '
                  f'verdict that moves has to move in both copies')

    def test_what_it_cannot_read_whole_is_refused(self):
        head = '## The answer'
        table = ('| Locale | old | new |\n|---|---|---|\n'
                 '| `x` | \U0001F534 **Changed** | ⚪ Unaffected |')
        other = '| Name | x |\n|---|---|\n| a | b |'
        empty = '| Locale | old | new |\n|---|---|---|'
        bare = 'Locale | old | new\n---|---|---\n`y` | a | b'
        pipeless = 'Verdict\n:---\n`y` — Unaffected'
        quoted = '\n'.join('> ' + line for line in table.split('\n'))
        listed = '\n'.join('    ' + line for line in table.split('\n'))
        cornered = table.replace('| Locale |', '|  |')
        elsewhere = 'carries a verdict outside the table'
        cases = (
            ('no heading', f'## Other\n\n{table}\n\n## Next', 'appears 0'),
            ('doubled heading',
             f'{head}\n\n{table}\n\n{head}\n\n{table}\n\n## Next',
             'appears 2'),
            ('nothing closes it', f'{head}\n\n{table}\n', 'nothing closes'),
            ('two tables', f'{head}\n\n{table}\n\nx\n\n{table}\n\n## Next',
             '2 table'),
            ('a second table without outer pipes',
             f'{head}\n\n{table}\n\n{bare}\n\n## Next', '2 table'),
            ('a second table with no pipe at all',
             f'{head}\n\n{table}\n\n{pipeless}\n\n## Next', '2 table'),
            ('no table', f'{head}\n\nx\n\n## Next', '0 table'),
            ('no header row',
             f'{head}\n\n|---|---|---|\n| `x` | a | b |\n\n## Next',
             'no header row'),
            ('glued to the line above',
             f'{head}\n\n<summary>x</summary>\n{table}\n\n## Next',
             'glued to the line above'),
            ('a row with no leading pipe',
             f'{head}\n\n{table}\n`y` | c | d\n\n## Next',
             'does not start with'),
            ('an indented row',
             f'{head}\n\n{table}\n   | `y` | c | d |\n\n## Next',
             'does not start with'),
            ('the note glued to the table',
             f'{head}\n\n{table}\n<sup>†</sup> note\n\n## Next',
             'does not start with'),
            ('a blank line made of a no-break space',
             f'{head}\n\n{table}\n \n<sup>†</sup> note\n\n## Next',
             'does not start with'),
            ('a row after a blank line',
             f'{head}\n\n{table}\n\n| `y` | c | d |\n\n## Next',
             'outside the table'),
            ('a quoted row under the heading',
             f'{head}\n\n{table}\n\n> | `y` | c | d |\n\n## Next',
             'outside the table'),
            ('another table', f'{head}\n\n{other}\n\n## Next',
             'not the verdict table'),
            ('no rows', f'{head}\n\n{empty}\n\n## Next', 'has no rows'),
            ('a second copy elsewhere in the file',
             f'{head}\n\n{table}\n\n## Next\n\n{table}\n', elsewhere),
            ('a copy in a quote elsewhere',
             f'{head}\n\n{table}\n\n## Next\n\n{quoted}\n', elsewhere),
            ('a copy in a list elsewhere',
             f'{head}\n\n{table}\n\n## Next\n\n- item\n\n{listed}\n',
             elsewhere),
            ('a copy with another header elsewhere',
             f'{head}\n\n{table}\n\n## Next\n\n{cornered}\n', elsewhere),
            ('an HTML table in the file',
             f'{head}\n\n{table}\n\n## Next\n\n'
             f'<table><tr><th>Name</th></tr></table>\n', 'an HTML table'),
        )
        for label, text, message in cases:
            with self.subTest(case=label):
                with self.assertRaisesRegex(AssertionError, message):
                    self.table(text, head)
        # The control: the same table, whole and closed, is read.
        self.assertEqual(self.table(f'{head}\n\n{table}\n\n## Next', head),
                         table.split('\n'))


class TheExamplesCarryTheMachineSteps(unittest.TestCase):
    """Both worked examples given each machine's file carry every step such a
    run prints and its full summary, so what the docs say about those steps
    can be checked against real output; and no example names a deleted step
    (steps 1 and 3 to 10, branch 9) or its script."""

    def example(self, name):
        return read(os.path.join(REPO_ROOT, 'examples',
                                 f'{name}-audit-output.txt'))

    def test_every_step_banner_is_in_both_examples(self):
        for name in ('rhel8-to-rhel9', 'rhel9-to-rhel10'):
            text = self.example(name)
            for banner in (
                    COMPARED_BANNER + '\n',
                    "== STEP 2  Which locales' sort rules changed",
                    "== STEP 11  MEASURED ORDER  how each machine's own glibc "
                    "sorts every locale",
                    '== AUDIT SUMMARY'):
                with self.subTest(example=name, banner=banner[:40]):
                    self.assertEqual(text.count(banner), 1)
            with self.subTest(example=name):
                self.assertEqual(
                    re.findall(r'^== STEP (\d+)', text, re.M),
                    ['2', '11'])
                self.assertLess(text.index(COMPARED_BANNER + '\n'),
                                text.index('== STEP 2 '))

    def test_no_example_names_a_deleted_step(self):
        """The four transcripts of the two pairs, with and without each
        machine's file: a sentence that still points to a deleted step
        sends the reader to an answer that is not there."""
        for name in ('rhel8-to-rhel9', 'rhel9-to-rhel10'):
            for kind in ('audit-output', 'tags-only'):
                with self.subTest(example=f'{name}-{kind}'):
                    text = read(os.path.join(REPO_ROOT, 'examples',
                                             f'{name}-{kind}.txt'))
                    self.assertIn(COMPARED_BANNER + '\n', text)
                    self.assertEqual(deleted_steps_named(text), [])


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
    nothing errors. Two such links shipped, one created by retitling the
    very section being documented; another left examples/rhel8-to-rhel9.sql
    pointing at a README section that had moved.
    The check used to be a snippet run by hand before a commit, when somebody
    remembered. A check that depends on somebody remembering is not a check.
    """

    def setUp(self):
        self.pages = {path: without_fences(text)
                      for path, text in docs().items()}
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
        # Each half that names something today; examples/ is named too, by
        # command 3's readings in examples/ (8.3), and test_confirm_sql
        # holds those to the files.
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


class TheExamplesPrintStep11sWordsOfToday(unittest.TestCase):
    """Nothing else ties the published examples to these two sentences of
    step 11: an example left unregenerated after either changes would keep
    the old words with every other test green (10.2). The note on names in
    bytes is printed in the report and in the summary of both pairs; the
    sort table without levels, C.utf8's, in both of 8 -> 9 only."""

    def test_the_note_on_names_in_bytes_and_the_table_without_levels(self):
        import locale_order as m
        for pair, levels in (('rhel8-to-rhel9', 2), ('rhel9-to-rhel10', 0)):
            path = os.path.join(REPO_ROOT, 'examples',
                                f'{pair}-audit-output.txt')
            with self.subTest(example=pair), \
                    open(path, encoding='utf-8') as fh:
                text = flat(fh.read())
                self.assertEqual(text.count(flat(m.OTHER_STRINGS)), 2, path)
                self.assertEqual(text.count(m.NO_LEVELS_ONE), levels, path)


def recorded_measurements():
    """{'rhel8': path, ...}: the measurement inside each recorded --extract
    file, as test_locale_order unpacks it."""
    import test_locale_order as t
    return {'rhel8': t.RHEL8, 'rhel9': t.RHEL9, 'rhel10': t.RHEL10}


class TheLimitationNamesTheLocalesStep11Names(unittest.TestCase):
    """docs/limitations.md names, by encoding, every locale whose encoding
    PostgreSQL does not know on the RHEL 8, 9 and 10 machines measured. A
    list of locales goes stale with the measurements, so it is held to the
    other copies of it, which must agree with it and with each other: what
    step 11's own other_encoding_text names for each recorded file alone
    (every locale of each machine, one on RHEL 8 only among them) and for
    the two pairs, and the line both examples print, each copy of which
    must also be the one step 11 names for that example's pair."""

    LIMITATIONS = os.path.join(REPO_ROOT, 'docs', 'limitations.md')
    PAIRS = (('rhel8', 'rhel9'), ('rhel9', 'rhel10'))
    LINE = '!! Not measured in a database of another encoding: '
    WORDS = dict(enumerate('zero one two three four five six seven eight '
                           'nine ten eleven twelve'.split()))

    def named(self, old, new, label):
        """{locale: encoding} other_encoding_text names for old -> new, as
        the comparison asks it, taken from the by_codeset it calls and held
        to the text it returns."""
        import unittest.mock
        import locale_order as m
        o, n = old['locales'], new['locales']
        both = sorted(set(o) & set(n))
        only_new = sorted(set(n) - set(o))
        seen = []
        real = m.by_codeset

        def spy(names, codesets):
            seen.append(dict(codesets))
            return real(names, codesets)
        with unittest.mock.patch.object(m, 'by_codeset', spy):
            text = m.other_encoding_text(
                both + only_new, m.TextModel(old, new, both, only_new), ())
        self.assertIsNotNone(text, f'{label}: no line')
        self.assertTrue(seen, f'{label}: by_codeset was not called')
        self.assertIn(' ' + real(seen[0], seen[0]) + '.', text,
                      f'{label}: the list captured is not the one printed')
        return seen[0]

    def printed(self):
        """[(label, {locale: encoding})] step 11 names for each recorded
        file against itself and for each pair."""
        import locale_order as m
        digest = m.corpus_digest()
        files = {n: m.load(path, digest)[0]
                 for n, path in recorded_measurements().items()}
        out = [(n, self.named(d, d, n)) for n, d in files.items()]
        for a, b in self.PAIRS:
            out.append((f'{a}-to-{b}', self.named(files[a], files[b], f'{a} -> {b}')))
        return out

    def in_examples(self):
        """[(pair, label, {locale: encoding})] of the two copies of the line
        in each example, the report's and the summary's."""
        out = []
        for a, b in self.PAIRS:
            path = os.path.join(REPO_ROOT, 'examples', f'{a}-to-{b}-audit-output.txt')
            text = ' '.join(read(path).split())
            parts = text.split(self.LINE)[1:]
            self.assertEqual(len(parts), 2, f'{path}: the line is not in the '
                             'report and the summary, once each')
            for i, part in enumerate(parts, 1):
                mt = re.match(r'(\d+) locale\(s\) .*?(?: is (?:not )?known'
                              r'| is the same on both machines)\. ', part)
                self.assertIsNotNone(mt, f'{path}: copy {i} unread')
                groups = part[mt.end():].split('. ')[0].rstrip('.')
                listed = {}
                for group in groups.split('; '):
                    codeset, _, names = group.partition(': ')
                    self.assertRegex(codeset, r'^[A-Z0-9-]+$', f'{path}: {group!r}')
                    for name in names.split(', '):
                        listed[name] = codeset
                self.assertEqual(len(listed), int(mt[1]),
                                 f'{path}: copy {i} counts another number')
                out.append((f'{a}-to-{b}', f'{os.path.basename(path)} copy {i}',
                            listed))
        return out

    def documented(self):
        """(N, {locale: encoding}) of the sentence "these are N: `a` and
        `b` (ENC), ..." up to "Step 11 measures", or a loud failure."""
        text = ' '.join(read(self.LIMITATIONS).split())
        found = re.findall(r'these are (\w+): (.*?)Step 11 measures', text)
        self.assertEqual(len(found), 1, 'docs/limitations.md: the sentence '
                         f'"these are N: ..." appears {len(found)} time(s)')
        word, names = found[0]
        out = {}
        for group, codeset in re.findall(r'((?:`[^`]+`(?:, |,? and )?)+) '
                                         r'\(([A-Z0-9-]+)\)', names):
            for name in re.findall(r'`([^`]+)`', group):
                self.assertNotIn(name, out, f'{name} listed twice')
                out[name] = codeset
        self.assertEqual(len(re.findall(r'`[^`]+`', names)), len(out),
                         f'a locale outside the "(ENCODING)" groups: {names}')
        return word, out

    def test_the_list_is_the_one_step_11_prints(self):
        word, listed = self.documented()
        printed = self.printed()
        examples = self.in_examples()
        by_pair = dict(printed)
        for pair, label, named in examples:
            self.assertEqual(named, by_pair[pair], f'{label} is not what step '
                             f'11 names for the recorded {pair} files')
        union = {}
        for label, named in printed + [(label, named) for _, label, named in examples]:
            for name, codeset in named.items():
                self.assertEqual(union.setdefault(name, codeset), codeset,
                                 f'{name}: {codeset} in {label}, '
                                 f'{union[name]} before')
        self.assertEqual(listed, union)
        self.assertEqual(word, self.WORDS.get(len(union), str(len(union))))

    def test_the_docs_quote_the_line_step_11_prints(self):
        """docs/confirming-on-a-real-system.md sends the reader of the
        template to this line by its opening words. Reworded in the code and
        not there, the reader looks for a line no run prints and reads its
        absence as "none"."""
        import locale_order as m
        digest = m.corpus_digest()
        recorded = recorded_measurements()
        old, new = (m.load(recorded[n], digest)[0]
                    for n in self.PAIRS[1])
        both = sorted(set(old['locales']) & set(new['locales']))
        only_new = sorted(set(new['locales']) - set(old['locales']))
        text = m.other_encoding_text(both + only_new,
                                     m.TextModel(old, new, both, only_new), ())
        self.assertIsNotNone(text)
        self.assertTrue(text.startswith(self.LINE), text[:80])
        quote = f'"{self.LINE[:-2]}"'
        for doc in (os.path.join('docs', 'confirming-on-a-real-system.md'),):
            with self.subTest(doc=doc):
                self.assertIn(quote, flat(read(os.path.join(REPO_ROOT, doc))))


class OneProcessIsTheOneTheCodePlans(unittest.TestCase):
    """docs/requirements.md says what the one process the script always
    starts can take; the figure is the plan default_jobs makes for it,
    RUN_BYTES and PROCESS_BYTES (16.39)."""

    PAGE = os.path.join(REPO_ROOT, 'docs', 'requirements.md')
    HEADING = '### Memory and time\n'

    def section(self):
        text = read(self.PAGE)
        self.assertEqual(text.count(self.HEADING), 1,
                         f'{self.PAGE}: the section is missing or doubled')
        rest = text.split(self.HEADING, 1)[1]
        end = re.search(r'^#', rest, re.M)
        self.assertIsNotNone(end, f'{self.PAGE}: no heading ends the section')
        return flat(rest[:end.start()])

    def test_the_figure_is_the_plan_for_one_process(self):
        import locale_order as m
        said = re.findall(r'but always one, which can take up to (\S+) MiB',
                          self.section())
        self.assertEqual(len(said), 1, 'the sentence is missing or doubled')
        self.assertEqual(said[0],
                         str((m.RUN_BYTES + m.PROCESS_BYTES) // (1024 * 1024)))


if __name__ == '__main__':
    unittest.main()
