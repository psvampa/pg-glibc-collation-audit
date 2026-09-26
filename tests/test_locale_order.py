"""The measured order: locale_order.py, on what three real machines measured.

The other layers read locale SOURCES. locale_order.py asks each machine's own
glibc how every locale sorts, and --compare says what changed between two
machines. The measuring half needs Linux with glibc and its language packs, so
it does not run here (tests/README.md, "What this suite does NOT cover"). What
runs here is everything --compare does with a measurement, on the files three
machines really wrote, kept in tests/locale_order/:

  rhel8.out   Rocky Linux 8.9,  glibc-2.28-251.el8_10.40, 867 locales
  rhel9.out   Rocky Linux 9.3,  glibc-2.34-275.el9_8,     869 locales
  rhel10.out  Rocky Linux 10.1, glibc-2.39-128.el10_2,    885 locales

each measured on 2026-09-26 with `python3 locale_order.py > rhelN.out`
(`/usr/libexec/platform-python` on RHEL8), every language pack installed.
rhelN.pg_collation.txt is the same machine's libc collations as PostgreSQL
18.6 imported them (the query is in PostgresNames), and rpm_order.json is
rpm's own order of every pair of 62 version and release strings, identical on
the three machines' rpm (4.14.3, 4.16.1.3, 4.19.1.1).

The measurements are kept whole, not cut down to the locales these tests
name: once those machines are gone, they are the only complete record of what
the three builds did.

Every class freezes a way the comparison can print a clean result it has no
right to, and its docstring says which.
"""
import base64
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

import _harness  # also puts scripts/ on sys.path
from _harness import flat

import locale_order as m

DATA = os.path.join(_harness.TESTS_DIR, 'locale_order')
SCRIPT = os.path.join(_harness.SCRIPTS_DIR, 'locale_order.py')
RHEL8, RHEL9, RHEL10 = (os.path.join(DATA, f'rhel{n}.out') for n in (8, 9, 10))
MACHINES = (('rhel8', RHEL8), ('rhel9', RHEL9), ('rhel10', RHEL10))

CHANGED_8_TO_9 = {
    'C.utf8', 'ko_KR.utf8', 'or_IN', 'or_IN.utf8', 'sv_FI', 'sv_FI.iso88591',
    'sv_FI.iso885915@euro', 'sv_FI.utf8', 'sv_FI@euro', 'sv_SE',
    'sv_SE.iso88591', 'sv_SE.iso885915', 'sv_SE.utf8', 'swedish'}
SWEDISH_LEGACY = {
    'sv_FI', 'sv_FI.iso88591', 'sv_FI.iso885915@euro', 'sv_FI@euro', 'sv_SE',
    'sv_SE.iso88591', 'sv_SE.iso885915', 'swedish'}
CHANGED_9_TO_10 = {'th_TH', 'th_TH.tis620', 'th_TH.utf8', 'thai'}
# glibc-2.28's commit, a ref that names no release (tests/_harness.py).
EXPECTED_2_28 = _harness.EXPECTED_SHA['glibc-2.28']
# Python has no converter for their encodings (ARMSCII-8, GEORGIAN-PS, EUC-TW).
UNMEASURABLE = {'hy_AM.armscii8', 'ka_GE', 'ka_GE.georgianps', 'zh_TW.euctw'}

# What told_apart_words() can say about a pair of neighbours.
TOLD_APART = {'not at all (the bytes decide)', 'as different letters',
              'like an accent', 'by case', 'by less than case'}


def run_compare(old, new, *more):
    """(exit status, stdout, stderr) of `locale_order.py --compare old new`.

    A subprocess, because what the script offers a reader is its printed
    report and its exit status. The output is UTF-8 whatever the runner's
    locale, so that the characters print the same way everywhere.
    """
    return run_script('--compare', old, new, *more)


def run_script(*args):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    p = subprocess.run([sys.executable, SCRIPT, *args],
                       capture_output=True, env=env)
    return (p.returncode, p.stdout.decode('utf-8'),
            p.stderr.decode('utf-8', 'replace'))


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# --- the report as data -------------------------------------------------------

class ReportShapeError(AssertionError):
    """The report holds a line parse_report() does not know."""


END = '\0end of report'
CHARACTER = r'(?:. \()?U\+([0-9A-F]{4,6})\)?'   # 'V (U+0056)' or 'U+0056'


def parse_also(text):
    """An 'also, in PostgreSQL: ...' line as {'aliases': {encoding: [name]},
    'spellings': [database locale]}."""
    aliases, spellings = {}, []
    for part in text[len('also, in PostgreSQL: '):].split('; '):
        mt = re.fullmatch(r'collations? ((?:"[^"]+", )*"[^"]+") where the '
                          r'database encoding is (\S+)', part)
        if mt:
            aliases[mt[2]] = re.findall(r'"([^"]+)"', mt[1])
            continue
        mt = re.fullmatch(r'database locales? (\S+(?:, \S+)*)', part)
        if mt:
            spellings.extend(mt[1].split(', '))
            continue
        raise ReportShapeError(f'unknown part of an "also" line: {part!r}')
    return {'aliases': aliases, 'spellings': spellings}


def warning_kind(text):
    """Which of the warnings above a report or a summary this is."""
    if text.startswith('!! Both machines run the same glibc build.'):
        return 'same build'
    if re.match(r'!! Both machines run glibc \S+ and the build of at least '
                r'one is unknown', text):
        return 'order unknown'
    # The side is part of the kind: a warning that names the wrong one says
    # the other side was checked. Each names it twice, and both must agree:
    # the whole sentence is matched, the second side as a backreference.
    mt = re.fullmatch(r'!! The (OLD|NEW) tag, \S+, names no glibc release, so '
                      r'nothing checked that the \1 file was measured on that '
                      r'glibc\.', text)
    if mt:
        return f'{mt[1]} tag unchecked'
    mt = re.fullmatch(r'!! The (OLD|NEW) file was measured on glibc \S+, which '
                      r'names no glibc release, so nothing checked that it is '
                      r'the glibc of the \1 tag, \S+\.', text)
    if mt:
        return f'{mt[1]} file unchecked'
    raise ReportShapeError(f'unknown warning: {text!r}')


# The two lines of the summary that name locales under a warning: what the
# warning says comes first, the names after the colon.
NOT_ON_NEW = ('not on the new machine, so a database or collation that uses '
              'one cannot use it there: ')
PARTLY = ('!! Unchanged only as far as measured, because a change between an '
          'accent and case would not show in them: ')


def parse_summary(text):
    """The block --summary-to writes for audit.sh, as names and counts.

    As strict as parse_report: every line is accounted for, and a count
    that disagrees with the names under it raises.
    """
    lines = text.split('\n')
    if lines[-1] == '':
        lines.pop()
    lines.append(END)
    at = [0]

    def line():
        return lines[at[0]]

    def fail(what):
        raise ReportShapeError(f'summary line {at[0] + 1}: {what}: {line()!r}')

    def take(pattern):
        mt = re.fullmatch(pattern, line())
        if not mt:
            fail(f'expected {pattern!r}')
        at[0] += 1
        return mt

    def item(indent, more):
        """A line at indent and the wrapped lines under it, whitespace
        collapsed; None when the next line is not at indent. The PostgreSQL
        names under a list of locales are at the indent the list wraps to,
        and are told apart, as a reader tells them, by how they begin."""
        if not re.fullmatch(' ' * indent + r'\S.*', line()):
            return None
        parts = [line()]
        at[0] += 1
        while (re.fullmatch(' ' * more + r'\S.*', line())
               and not line().strip().startswith('also, in PostgreSQL: ')):
            parts.append(line())
            at[0] += 1
        return flat(' '.join(parts))

    def names_of(text):
        return [n for n in text.split(', ')]

    s = {}
    mt = take(r'   \((.+) -> (.+)\)')
    s['machines'] = (mt[1], mt[2])
    s['warnings'] = []
    while line().startswith('     !! ') and not line().startswith('     !! Not measured: '):
        s['warnings'].append(warning_kind(item(5, 8)))
    s['changes'] = {}
    if line() == '     no locale measured on both machines sorts differently':
        count = 0
        at[0] += 1
    else:
        count = int(take(r'     (\d+) locale\(s\) sort differently -- '
                         r'reindex what uses them:')[1])
        while True:
            heading = []
            while re.fullmatch(r'       \S.*', line()):
                heading.append(line().strip())
                at[0] += 1
            if not heading:
                break
            names = []
            for group in ' '.join(heading).split('; '):
                g = re.fullmatch(r'(.+) \(([^()]+)\)', group)
                if not g:
                    raise ReportShapeError(f'unknown heading group: {group!r}')
                names += names_of(g[1])
            brief = item(9, 11)
            if brief is None or brief.startswith('also, in PostgreSQL: '):
                fail('expected what moved')
            also = item(9, 11)
            if also is not None and not also.startswith('also, in PostgreSQL: '):
                fail('expected the PostgreSQL names')
            for name in names:
                s['changes'][name] = {'brief': brief,
                                      'also': also and parse_also(also)}
    if len(s['changes']) != count:
        fail(f'{count} changed and {len(s["changes"])} named')
    s['unmeasured'], s['not_on_new'], s['unmeasured_also'] = [], [], None
    mt = re.fullmatch(r'     NOT known to be unchanged: (\d+) locale\(s\)',
                      line())
    if mt:
        at[0] += 1
        listed = item(7, 9)
        if listed and listed.startswith('could not be measured: '):
            s['unmeasured'] = names_of(listed[len('could not be measured: '):])
            also = item(9, 11)
            if also is not None:
                if not also.startswith('also, in PostgreSQL: '):
                    fail('expected the PostgreSQL names')
                s['unmeasured_also'] = parse_also(also)
            listed = item(7, 9)
        if listed and listed.startswith(NOT_ON_NEW):
            s['not_on_new'] = names_of(listed[len(NOT_ON_NEW):])
            listed = None
        if listed is not None:
            raise ReportShapeError(f'unknown not-known item: {listed!r}')
        if len(s['unmeasured']) + len(s['not_on_new']) != int(mt[1]):
            fail(f'NOT known says {mt[1]}')
    partly = item(5, 8) if line().startswith('     !! Unchanged only') else None
    if partly is not None and not partly.startswith(PARTLY):
        raise ReportShapeError(f'unknown partly-measured line: {partly!r}')
    s['partly'] = names_of(partly[len(PARTLY):]) if partly else []
    s['totals'] = item(5, 7)
    if not (s['totals'] or '').startswith('Of the '):
        fail('expected the totals')
    s['closing'] = item(5, 8)
    if not (s['closing'] or '').startswith('!! Not measured: '):
        fail('expected the closing warning')
    if line() != END:
        fail('a line after the closing warning')
    return s


def parse_report(text):
    """The report --compare prints, as names and counts rather than sentences.

    Every line is accounted for. A line of a shape not listed here raises
    instead of being skipped, and so does a section whose count disagrees
    with the names listed under it: a parser that stops at what it cannot
    read returns the part before it, and a test reading that part passes
    whatever came after.
    """
    lines = text.split('\n')
    if lines[-1] == '':
        lines.pop()
    lines.append(END)
    at = [0]

    def line():
        return lines[at[0]]

    def fail(what):
        raise ReportShapeError(f'report line {at[0] + 1}: {what}: {line()!r}')

    def take(pattern):
        mt = re.fullmatch(pattern, line())
        if not mt:
            fail(f'expected {pattern!r}')
        at[0] += 1
        return mt

    def indented(prefix_pattern):
        """The lines that match, stripped, until one does not."""
        out = []
        while re.fullmatch(prefix_pattern, line()):
            out.append(line().strip())
            at[0] += 1
        return out

    def paragraph():
        """A '!! ...' paragraph, whose wrapped lines are indented by three."""
        first = line()
        at[0] += 1
        return flat(' '.join([first] + indented(r'   \S.*')))

    def also():
        """An 'also, in PostgreSQL' line and its wrapped lines, or None."""
        if not line().startswith('    also, in PostgreSQL: '):
            return None
        first = line()
        at[0] += 1
        return parse_also(flat(' '.join([first] + indented(r'      \S.*'))))

    def listed(count, what):
        names = indented(r'  \S+')
        if len(names) != count:
            fail(f'{what} says {count} and lists {len(names)}')
        return names

    r = {}
    mt = take(r'Old machine: (.+), (\d+) locale\(s\)')
    r['old'] = (mt[1], int(mt[2]))
    mt = take(r'New machine: (.+), (\d+) locale\(s\)')
    r['new'] = (mt[1], int(mt[2]))
    r['warnings'] = []
    while line().startswith('!! '):
        r['warnings'].append(warning_kind(paragraph()))
    take('')

    changed = int(take(r'Sort order CHANGED in (\d+) locale\(s\):')[1])
    r['changes'] = []
    while True:
        take('')
        if line().startswith('Unchanged: '):
            break
        heading = indented(r'  \S.*')
        if not heading:
            fail('expected the names of a change')
        change = {'names': {}}
        for group in ' '.join(heading).split('; '):
            mt = re.fullmatch(r'(.+) \(([^()]+)\)', group)
            if not mt:
                raise ReportShapeError(f'unknown heading group: {group!r}')
            for name in mt[1].split(', '):
                change['names'][name] = mt[2]
        change['also'] = also()
        details = []
        while line().startswith('    '):
            details.append(line()[4:])
            at[0] += 1
        change.update(parse_details(details))
        r['changes'].append(change)
    names = [n for c in r['changes'] for n in c['names']]
    if len(names) != changed or len(set(names)) != changed:
        fail(f'CHANGED says {changed} and the changes name {len(names)}')

    r['unchanged'] = int(take(r'Unchanged: (\d+) locale\(s\)\.')[1])
    r['partly'] = {}
    if line().startswith('!! Unchanged only as far as measured: '):
        count = int(re.match(r'!! Unchanged only as far as measured: (\d+) of',
                             paragraph())[1])
        for item in ' '.join(indented(r'  \S.*')).split(', '):
            mt = re.fullmatch(r'(\S+) \((\d+)\)', item)
            if not mt:
                raise ReportShapeError(f'unknown partly-measured item: {item!r}')
            r['partly'][mt[1]] = int(mt[2])
        if len(r['partly']) != count:
            fail(f'partly measured says {count} and lists {len(r["partly"])}')

    r['unmeasured'] = {}
    if line().startswith('!! Could not be measured'):
        count = int(take(r'!! Could not be measured, so NOT known to be '
                         r'unchanged: (\d+) locale\(s\):')[1])
        while re.fullmatch(r'  \S.*', line()):
            mt = re.fullmatch(r'  (\S+) \((both machines|old machine|new '
                              r'machine)\): (.+)', line())
            if mt:
                name, side, reason = mt[1], mt[2], mt[3]
            elif re.fullmatch(r'  \S+: measured over different characters on '
                              r'the two machines .+', line()):
                name, side, reason = (line().split()[0][:-1], 'both machines',
                                      'different characters')
            else:
                fail('unknown not-measured line')
            at[0] += 1
            entry = r['unmeasured'].setdefault(name, {'why': {}, 'also': None})
            entry['why'][side] = reason
            entry['also'] = also() or entry['also']
        if len(r['unmeasured']) != count:
            fail(f'not measured says {count} and lists {len(r["unmeasured"])}')

    r['not_on_new'] = []
    if line().startswith('!! Not on the new machine'):
        count = int(re.match(r'!! Not on the new machine, so NOT known to be '
                             r'unchanged: (\d+) locale', paragraph())[1])
        r['not_on_new'] = listed(count, 'not on the new machine')
    count = int(take(r'Only on the new machine: (\d+) locale\(s\)\.')[1])
    r['only_new'] = listed(count, 'only on the new machine')
    take('')
    mt = take(r'Of the (\d+) locale\(s\) on the old machine: (\d+) changed, '
              r'(\d+) unchanged(?: \((\d+) of them only as far as measured\))?, '
              r'(\d+) not measured, (\d+) not on the new machine\.')
    r['totals_line'] = mt[0]
    r['totals'] = {'old': int(mt[1]), 'changed': int(mt[2]),
                   'unchanged': int(mt[3]), 'partly': int(mt[4] or 0),
                   'not measured': int(mt[5]), 'not on new': int(mt[6])}
    if not line().startswith('!! Not measured: '):
        fail('expected the closing "!! Not measured" paragraph')
    r['closing'] = paragraph()
    if line() != END:
        fail('a line after the closing paragraph')
    return r


def parse_details(details):
    """What changed in one locale: the characters that moved, the neighbours
    told apart differently, or the probe pairs that changed."""
    out = {'moved': None, 'moved_count': None, 'pairs': None,
           'pairs_count': None, 'probes': None}
    if not details:
        raise ReportShapeError('a change with no detail lines')
    first, rest = details[0], details[1:]
    many = re.fullmatch(r'([\d,]+) characters moved, too many to list here',
                        first)
    listed = re.fullmatch(r'(\d+) character\(s\) moved:', first)
    if first == 'no character moved':
        out['moved'] = []
    elif many:
        out['moved_count'] = int(many[1].replace(',', ''))
    elif listed:
        items, rest = rest[:int(listed[1])], rest[int(listed[1]):]
        out['moved'] = []
        for item in items:
            mt = re.match(r'  (?:. \()?U\+([0-9A-F]{4,6})\b', item)
            if not mt:
                raise ReportShapeError(f'unknown moved character: {item!r}')
            out['moved'].append(int(mt[1], 16))
    else:
        raise ReportShapeError(f'unknown first detail line: {first!r}')
    if out['moved'] is not None:
        out['moved_count'] = len(out['moved'])

    if rest and rest[0] == ('the pairs that tell letters, accents and case '
                            'apart here are told apart differently themselves:'):
        before = re.fullmatch(r'  before: (.+)', rest[1] if len(rest) > 1 else '')
        now = re.fullmatch(r'  now:    (.+)', rest[2] if len(rest) > 2 else '')
        if not (before and now):
            raise ReportShapeError(f'unknown probe lines: {rest[1:3]!r}')
        out['probes'] = (before[1], now[1])
        rest = rest[3:]
    elif rest:
        many = re.fullmatch(r'([\d,]+) pairs of neighbouring characters told '
                            r'apart differently, too many to list here', rest[0])
        listed = re.fullmatch(r'(\d+) pair\(s\) of neighbouring characters told '
                              r'apart differently:', rest[0])
        if many:
            out['pairs_count'] = int(many[1].replace(',', ''))
            rest = rest[1:]
        elif listed:
            count = int(listed[1])
            items, rest = rest[1:1 + count], rest[1 + count:]
            out['pairs'] = []
            for item in items:
                mt = re.fullmatch(rf'  {CHARACTER} and {CHARACTER}: before '
                                  r'(.+), now (.+)', item)
                if not mt or not {mt[3], mt[4]} <= TOLD_APART:
                    raise ReportShapeError(f'unknown pair line: {item!r}')
                out['pairs'].append((int(mt[1], 16), int(mt[2], 16),
                                     mt[3], mt[4]))
            out['pairs_count'] = len(out['pairs'])
    if rest:
        raise ReportShapeError(f'unknown detail lines: {rest!r}')
    return out


def parse_brief(text):
    """A summary's one phrase of what moved, read into the keys parse_details
    gives the report's lines, so that the two can be compared one to one."""
    out = {'moved': None, 'moved_count': None, 'pairs': None,
           'pairs_count': None, 'probes': False}
    first, *rest = text.split('; ')
    many = re.fullmatch(r'([\d,]+) characters moved', first)
    listed = re.fullmatch(r'(\d+) character\(s\) moved: (.+)', first)
    if first == 'no character moved':
        out['moved'], out['moved_count'] = [], 0
    elif many:
        out['moved_count'] = int(many[1].replace(',', ''))
    elif listed:
        out['moved'] = [int(c, 16) for c in
                        re.findall(r'U\+([0-9A-F]{4,6})', listed[2])]
        out['moved_count'] = len(out['moved'])
        if out['moved_count'] != int(listed[1]):
            raise ReportShapeError(f'phrase counts {listed[1]}: {text!r}')
    else:
        raise ReportShapeError(f'unknown phrase: {text!r}')
    for part in rest:
        many = re.fullmatch(r'([\d,]+) pairs of neighbouring characters told '
                            r'apart differently', part)
        listed = re.fullmatch(r'(\d+) pair\(s\) of neighbouring characters '
                              r'told apart differently: (.+)', part)
        if part == ('the pairs that tell letters, accents and case apart are '
                    'told apart differently themselves'):
            out['probes'] = True
        elif many:
            out['pairs_count'] = int(many[1].replace(',', ''))
        elif listed:
            out['pairs'] = [(int(x, 16), int(y, 16)) for x, y in re.findall(
                rf'{CHARACTER} and {CHARACTER}', listed[2])]
            out['pairs_count'] = len(out['pairs'])
            if out['pairs_count'] != int(listed[1]):
                raise ReportShapeError(f'phrase counts {listed[1]}: {text!r}')
        else:
            raise ReportShapeError(f'unknown part of a phrase: {part!r}')
    return out


def by_name(report):
    """{locale: the change it is listed under}."""
    return {n: c for c in report['changes'] for n in c['names']}


# --- the two published pairs -------------------------------------------------

class Rhel8ToRhel9(unittest.TestCase):
    """RHEL8 -> RHEL9 on the full measurements: the exact list of 14 locales,
    and what moved in each.

    Freezes the answer the comparison exists to give. Three of its parts are
    what reading locale files cannot give: ko_KR.utf8 changes with a
    byte-identical source, the eight Swedish locales outside UTF-8 change with
    no character moving at all, and C.utf8 changes although no upgrade tag
    carries the C file RHEL8 builds it from.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, out, cls.err = run_compare(RHEL8, RHEL9)
        cls.report = parse_report(out)
        cls.changes = by_name(cls.report)

    def test_a_clean_run_of_the_two_machines(self):
        self.assertEqual((self.rc, self.err), (0, ''))
        self.assertEqual(self.report['old'],
                         ('glibc-2.28-251.el8_10.40.x86_64', 867))
        self.assertEqual(self.report['new'],
                         ('glibc-2.34-275.el9_8.x86_64', 869))
        self.assertEqual(self.report['warnings'], [])

    def test_exactly_these_locales_changed(self):
        self.assertEqual(set(self.changes), CHANGED_8_TO_9)

    def test_every_locale_of_the_old_machine_is_counted_once(self):
        """Changed, unchanged, not measured and missing on the new machine are
        separate numbers, and they add up to the old machine's locales."""
        t = self.report['totals']
        self.assertEqual(t, {'old': 867, 'changed': 14, 'unchanged': 847,
                             'partly': 5, 'not measured': 4, 'not on new': 2})
        self.assertEqual(t['changed'] + t['unchanged'] + t['not measured']
                         + t['not on new'], t['old'])
        self.assertEqual(t['unchanged'], self.report['unchanged'])
        self.assertEqual(t['partly'], len(self.report['partly']))
        self.assertEqual(t['not measured'], len(self.report['unmeasured']))
        self.assertEqual(t['not on new'], len(self.report['not_on_new']))

    def test_ko_KR_utf8_moves_one_character(self):
        """U+D7A3 moved while ko_KR's source did not change: localedef did.
        And the report gives the name a UTF-8 database knows it by, "ko_KR",
        which `locale -a` gives to a different, EUC-KR locale that did not
        change: a reader looking for ko_KR must not find only that one."""
        ko = self.changes['ko_KR.utf8']
        self.assertEqual(ko['moved'], [0xD7A3])
        self.assertEqual(ko['also'], {'aliases': {'UTF-8': ['ko_KR']},
                                      'spellings': ['ko_KR.UTF-8']})
        self.assertNotIn('ko_KR', self.changes)

    def test_swedish_in_utf8_moves_W_and_w(self):
        """Their told-apart records differ too, but only around the places W
        and w left: a pair that is next to each other in one order only is
        the moved characters' business, not a second finding."""
        sv = self.changes['sv_SE.utf8']
        self.assertEqual(set(sv['names']), {'sv_FI.utf8', 'sv_SE.utf8'})
        self.assertEqual(sv['moved'], [0x57, 0x77])
        self.assertEqual((sv['pairs'], sv['pairs_count'], sv['probes']),
                         (None, None, None))

    def test_swedish_outside_utf8_changes_with_no_character_moving(self):
        """Every character keeps its place and V and w stop being told apart
        like an accent: only strings of two letters sort differently (`va wa
        wb vc` becomes `va vc wa wb`). An order of single characters calls
        these eight locales unchanged."""
        sv = self.changes['sv_SE']
        self.assertEqual(set(sv['names']), SWEDISH_LEGACY)
        self.assertEqual(sv['names']['sv_SE'], 'ISO-8859-1')
        self.assertEqual(sv['names']['sv_SE.iso885915'], 'ISO-8859-15')
        self.assertEqual(sv['moved'], [])
        self.assertEqual(sv['pairs'], [(0x56, 0x77, 'like an accent',
                                        'as different letters')])

    def test_a_rewritten_order_is_counted(self):
        self.assertEqual(self.changes['C.utf8']['moved_count'], 135360)
        orya = self.changes['or_IN']
        self.assertEqual(set(orya['names']), {'or_IN', 'or_IN.utf8'})
        self.assertEqual(orya['moved_count'], 50554)
        self.assertIsNotNone(orya['probes'])

    def test_what_could_not_be_measured_is_named(self):
        """A locale Python cannot build the bytes of is listed as NOT known to
        be unchanged, with the name PostgreSQL gives it: in an EUC_TW database
        "zh_TW" is zh_TW.euctw, while `locale -a` gives "zh_TW" to a BIG5
        locale that was measured."""
        u = self.report['unmeasured']
        self.assertEqual(set(u), UNMEASURABLE)
        self.assertEqual({tuple(e['why']) for e in u.values()},
                         {('both machines',)})
        self.assertEqual(u['zh_TW.euctw']['also']['aliases'],
                         {'EUC-TW': ['zh_TW']})

    def test_the_partly_measured_are_named(self):
        self.assertEqual(self.report['partly'],
                         {'ja_JP.utf8': 3, 'ko_KR': 1, 'ko_KR.euckr': 1,
                          'korean': 1, 'korean.euc': 1})

    def test_locales_on_one_machine_only(self):
        self.assertEqual(self.report['not_on_new'],
                         ['en_US.utf8@ampm', 'en_US@ampm'])
        self.assertEqual(self.report['only_new'],
                         ['ckb_IQ', 'ckb_IQ.utf8', 'mnw_MM', 'mnw_MM.utf8'])

    def test_the_closing_warning_is_always_printed(self):
        self.assertTrue(self.report['closing'].startswith('!! Not measured:'))


class Rhel9ToRhel10(unittest.TestCase):
    """RHEL9 -> RHEL10 on the full measurements: th_TH changes, and ber_DZ
    and kab_DZ do not.

    Freezes the other half of what reading files gets wrong: ber_DZ's and
    kab_DZ's sources swapped their rules between these versions, so a check of
    the files flags both, and measured they sort exactly as before.
    """

    @classmethod
    def setUpClass(cls):
        cls.rc, out, cls.err = run_compare(RHEL9, RHEL10)
        cls.report = parse_report(out)
        cls.changes = by_name(cls.report)

    def test_a_clean_run_of_the_two_machines(self):
        self.assertEqual((self.rc, self.err), (0, ''))
        self.assertEqual(self.report['old'],
                         ('glibc-2.34-275.el9_8.x86_64', 869))
        self.assertEqual(self.report['new'],
                         ('glibc-2.39-128.el10_2.x86_64', 885))
        self.assertEqual(self.report['warnings'], [])

    def test_exactly_these_locales_changed(self):
        self.assertEqual(set(self.changes), CHANGED_9_TO_10)

    def test_every_locale_of_the_old_machine_is_counted_once(self):
        t = self.report['totals']
        self.assertEqual(t, {'old': 869, 'changed': 4, 'unchanged': 859,
                             'partly': 6, 'not measured': 4, 'not on new': 2})
        self.assertEqual(t['changed'] + t['unchanged'] + t['not measured']
                         + t['not on new'], t['old'])
        self.assertEqual(t['unchanged'], self.report['unchanged'])
        self.assertEqual(t['partly'], len(self.report['partly']))
        self.assertEqual(t['not measured'], len(self.report['unmeasured']))
        self.assertEqual(t['not on new'], len(self.report['not_on_new']))

    def test_thai_moves(self):
        tis = self.changes['th_TH']
        self.assertEqual(tis['names'], {'th_TH': 'TIS-620',
                                        'th_TH.tis620': 'TIS-620',
                                        'thai': 'TIS-620'})
        self.assertEqual(tis['moved_count'], 88)
        self.assertEqual(self.changes['th_TH.utf8']['moved_count'], 50584)

    def test_ber_DZ_and_kab_DZ_are_unchanged(self):
        """Present on both machines and in no list but the unchanged count."""
        listed = (set(self.changes) | set(self.report['unmeasured'])
                  | set(self.report['not_on_new']) | set(self.report['only_new']))
        new = read_json(RHEL10)['locales']
        for name in ('ber_DZ', 'ber_DZ.utf8', 'kab_DZ', 'kab_DZ.utf8'):
            with self.subTest(name=name):
                self.assertIn(name, new)
                self.assertNotIn(name, listed)

    def test_what_could_not_be_measured_is_named(self):
        self.assertEqual(set(self.report['unmeasured']), UNMEASURABLE)

    def test_locales_on_one_machine_only(self):
        self.assertEqual(self.report['not_on_new'],
                         ['aa_ER.utf8@saaho', 'aa_ER@saaho'])
        self.assertEqual(len(self.report['only_new']), 18)


# --- what audit.sh asks of the comparison -------------------------------------

class SummaryForTheAudit(unittest.TestCase):
    """--summary-to: the block audit.sh prints under its summary.

    Freezes the summary as a second account of the comparison that could say
    less than the report: every locale the report names as changed, not
    measured or missing on the new machine is named here too, with the names
    PostgreSQL gives it, the counts are the report's own, and so are the
    warnings.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='locale_order_summary_')
        cls.addClassCleanup(shutil.rmtree, cls.tmp)

    def summary_and_report(self, old, new):
        path = os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}.txt')
        rc, out, err = run_compare(old, new, '--summary-to', path)
        self.assertEqual((rc, err), (0, ''))
        with open(path, encoding='utf-8') as f:
            return parse_summary(f.read()), parse_report(out)

    def assertSameAnswer(self, summary, report):
        changes = by_name(report)
        self.assertEqual(set(summary['changes']), set(changes))
        for name, change in changes.items():
            with self.subTest(locale=name):
                self.assertEqual(summary['changes'][name]['also'],
                                 change['also'])
                # What moved, in the phrase, is what the report's lines say.
                brief = parse_brief(summary['changes'][name]['brief'])
                self.assertEqual(
                    (brief['moved'], brief['moved_count'], brief['probes'],
                     brief['pairs'], brief['pairs_count']),
                    (change['moved'], change['moved_count'],
                     change['probes'] is not None,
                     change['pairs'] and [(x, y) for x, y, *_ in change['pairs']],
                     change['pairs_count']))
        self.assertEqual(summary['machines'],
                         (report['old'][0], report['new'][0]))
        self.assertEqual(summary['unmeasured'], list(report['unmeasured']))
        self.assertEqual(summary['not_on_new'], report['not_on_new'])
        self.assertEqual(summary['partly'], list(report['partly']))
        self.assertEqual(summary['totals'], report['totals_line'])
        self.assertEqual(summary['warnings'], report['warnings'])
        self.assertEqual(summary['closing'], report['closing'])

    def test_rhel8_to_rhel9(self):
        summary, report = self.summary_and_report(RHEL8, RHEL9)
        self.assertSameAnswer(summary, report)
        self.assertEqual(set(summary['changes']), CHANGED_8_TO_9)
        self.assertIn('U+D7A3', summary['changes']['ko_KR.utf8']['brief'])
        self.assertEqual(summary['unmeasured_also']['aliases'].get('EUC-TW'),
                         ['zh_TW'])

    def test_rhel9_to_rhel10(self):
        summary, report = self.summary_and_report(RHEL9, RHEL10)
        self.assertSameAnswer(summary, report)
        self.assertEqual(set(summary['changes']), CHANGED_9_TO_10)


# --- measurements that are not whole ------------------------------------------

def damaged(packed, offset, bit):
    """A packed record with one bit flipped, packed again so that it still
    decompresses: damage only a digest can see."""
    raw = bytearray(zlib.decompress(base64.b64decode(packed)))
    raw[offset] ^= bit
    return base64.b64encode(zlib.compress(bytes(raw))).decode('ascii')


class Variants(unittest.TestCase):
    """Base for the classes that edit a real measurement: each edit is
    written to a scratch directory, from a fresh copy of the file."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='locale_order_test_')
        cls.addClassCleanup(shutil.rmtree, cls.tmp)
        cls.texts = {}
        for path in (RHEL8, RHEL9):
            with open(path, encoding='utf-8') as f:
                cls.texts[path] = f.read()

    def variant(self, edit, base=RHEL9, reseal=False):
        """base with edit(data) applied, as a new file. reseal recomputes the
        digest of the whole file, so that only the checks after it can see
        the edit."""
        data = json.loads(self.texts[base])
        edit(data)
        if reseal:
            data['content'] = m.content_digest(data)
        path = os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}.out')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, sort_keys=True) + '\n')
        return path


class TagsOfTheAuditedPair(Variants):
    """--tags: the glibc tags audit.sh audits, one per file.

    Freezes the audit of one upgrade run over another upgrade's
    measurements: the report would print the other pair's changes under this
    pair's heading, at exit 0. A file measured on another glibc than its tag
    is refused before a line of report and before the summary is written; a
    tag that names no release is said to be unchecked, never taken as a
    match.
    """

    def test_the_tags_of_the_measured_versions_change_nothing(self):
        rc, out, err = run_compare(RHEL8, RHEL9, '--tags', 'glibc-2.28',
                                   'glibc-2.34')
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(out, run_compare(RHEL8, RHEL9)[1])

    def assertRefusedWithNoSummary(self, side, old, new, *tags):
        """Refused, naming the side that is wrong: naming the other one
        would say this one was checked."""
        path = os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}.txt')
        rc, out, err = run_compare(old, new, '--tags', *tags,
                                   '--summary-to', path)
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, '')
        # Both mentions of the side: the file and the tag it was held to.
        self.assertRegex(err, rf'^locale_order\.py: the {side} file, .+?, was '
                              rf'measured on .+?, and the {side} tag is ')
        self.assertFalse(os.path.exists(path))

    def test_an_old_file_from_another_glibc_is_refused(self):
        self.assertRefusedWithNoSummary('OLD', RHEL9, RHEL10,
                                        'glibc-2.28', 'glibc-2.39')

    def test_a_new_file_from_another_glibc_is_refused(self):
        self.assertRefusedWithNoSummary('NEW', RHEL8, RHEL9,
                                        'glibc-2.28', 'glibc-2.39')

    def test_a_tag_that_names_no_release_is_said_to_be_unchecked(self):
        for tags, said in (((EXPECTED_2_28, 'glibc-2.34'), 'OLD tag unchecked'),
                           (('glibc-2.28', 'glibc-2.34.9000'), 'NEW tag unchecked')):
            with self.subTest(tags=tags):
                rc, out, err = run_compare(RHEL8, RHEL9, '--tags', *tags)
                self.assertEqual((rc, err), (0, ''))
                self.assertEqual(parse_report(out)['warnings'], [said])

    def test_a_new_file_from_a_development_glibc_is_said_to_be_unchecked(self):
        """glibc 2.34.9000 is on the way to 2.35: reading it as 2.34 passed
        it for the 2.34 release with no word said."""
        def edit(d):
            d['glibc_version'] = 'glibc 2.34.9000'
            d['glibc_build'] = 'glibc-2.34.9000-1.fc35.x86_64'
        rc, out, err = run_compare(RHEL8, self.variant(edit, reseal=True),
                                   '--tags', 'glibc-2.28', 'glibc-2.34')
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(parse_report(out)['warnings'], ['NEW file unchecked'])

    def test_an_old_file_from_a_development_glibc_is_said_to_be_unchecked(self):
        def edit(d):
            d['glibc_version'] = 'glibc 2.28.9000'
            d['glibc_build'] = 'glibc-2.28.9000-1.fc29.x86_64'
        rc, out, err = run_compare(self.variant(edit, base=RHEL8, reseal=True),
                                   RHEL9, '--tags', 'glibc-2.28', 'glibc-2.34')
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(parse_report(out)['warnings'], ['OLD file unchecked'])

    def test_a_development_glibc_of_another_version_is_refused(self):
        def edit(d):
            d['glibc_version'] = 'glibc 2.35.9000'
            d['glibc_build'] = 'glibc-2.35.9000-1.fc36.x86_64'
        self.assertRefusedWithNoSummary('NEW', RHEL8,
                                        self.variant(edit, reseal=True),
                                        'glibc-2.28', 'glibc-2.34')

    def test_the_options_go_with_compare(self):
        for args in (('--tags', 'glibc-2.28', 'glibc-2.34'),
                     ('--summary-to', os.path.join(self.tmp, 'lone.txt'))):
            with self.subTest(args=args[0]):
                rc, out, err = run_script(*args)
                self.assertEqual(rc, 2, err)
                self.assertEqual(out, '')
                self.assertIn('--compare', err)

    def test_a_summary_that_cannot_be_written_stops_the_run(self):
        """Before the report, so that a run which cannot hand audit.sh its
        summary prints nothing that reads like a result."""
        path = os.path.join(self.tmp, 'no-such-directory', 'summary.txt')
        rc, out, err = run_compare(RHEL8, RHEL9, '--summary-to', path)
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, '')
        self.assertTrue(err.startswith('locale_order.py: '), err)

    def test_no_summary_is_written_for_a_refused_pair(self):
        path = os.path.join(self.tmp, 'reversed.txt')
        rc, out, err = run_compare(RHEL9, RHEL8, '--summary-to', path)
        self.assertEqual((rc, out), (2, ''))
        self.assertFalse(os.path.exists(path))


class Refusals(Variants):
    """A measurement that is not whole is refused: exit 2, a message, and not
    one line of report.

    Freezes every way two files can agree while one of them says less than
    its machine measured: cut short, edited, damaged on the way, or read by
    another version of the script. Each check has a case that only it can
    catch: the RESEALED cases recompute the file's digest, so the one check
    that would otherwise catch everything cannot hide a check behind it that
    no longer works.
    """

    def assertRefused(self, old, new):
        rc, out, err = run_compare(old, new)
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, '')
        # die()'s prefix: argparse also exits 2, with 'usage:' first.
        self.assertTrue(err.startswith('locale_order.py: '), err)

    # the file as a whole

    def test_a_missing_file(self):
        self.assertRefused(RHEL8, os.path.join(self.tmp, 'absent.out'))

    def test_a_file_cut_short(self):
        path = os.path.join(self.tmp, 'cut.out')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.texts[RHEL9][:1300000])
        self.assertRefused(RHEL8, path)

    def test_an_empty_file(self):
        path = os.path.join(self.tmp, 'empty.out')
        open(path, 'w').close()
        self.assertRefused(RHEL8, path)

    def test_another_format_resealed(self):
        def edit(d):
            d['format'] = 'locale_order/0'
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_measured_by_another_copy_of_the_script_resealed(self):
        def edit(d):
            d['corpus'] = '0' * 64
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_a_locale_removed_resealed(self):
        def edit(d):
            del d['locales']['sv_SE.utf8']
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_no_measured_orders_resealed(self):
        def edit(d):
            del d['order_data']
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_one_measured_order_missing_resealed(self):
        def edit(d):
            del d['order_data'][d['locales']['ko_KR.utf8']['order']]
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_no_content_digest(self):
        def edit(d):
            del d['content']
        self.assertRefused(RHEL8, self.variant(edit))

    def test_the_glibc_version_edited(self):
        def edit(d):
            d['glibc_version'] = 'glibc 2.38'
        self.assertRefused(RHEL8, self.variant(edit))

    def test_no_glibc_version_resealed(self):
        """With no build either, so that the build cannot stand in for it."""
        def edit(d):
            d['glibc_version'], d['glibc_build'] = 'unknown', None
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_a_build_that_contradicts_its_version_resealed(self):
        def edit(d):
            d['glibc_build'] = 'glibc-2.28-251.el8_10.40.x86_64'
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    # one locale's entry

    def test_an_entry_without_its_record_resealed(self):
        def edit(d):
            del d['locales']['sv_SE.utf8']['pairs']
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_an_entry_whose_error_says_nothing_resealed(self):
        """It used to print 45 lines of report and then fail with a
        traceback, exit 1: an error entry is read as one that could not be
        measured, and one with no reason fell through to the branch for two
        measured entries."""
        # Empty, and not text at all: 1 and a list would print as a reason.
        for error in ('', 0, 1, ['x']):
            with self.subTest(error=error):
                def edit(d):
                    d['locales']['en_US'] = {'error': error,
                                             'codeset': 'ISO-8859-1'}
                self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_an_error_entry_whose_codeset_is_not_text_resealed(self):
        """On the old machine, where PostgreSQL's names are worked out: a
        number or a list stopped the comparison with a traceback, exit 1,
        and an empty codeset dropped the locale's names without a word."""
        for codeset in (5, ['UTF-8'], ''):
            with self.subTest(codeset=codeset):
                def edit(d):
                    d['locales']['sv_SE.utf8'] = {
                        'error': 'cannot be selected: test', 'codeset': codeset}
                self.assertRefused(self.variant(edit, base=RHEL8, reseal=True),
                                   RHEL9)

    def test_an_entry_without_probes_resealed(self):
        def edit(d):
            del d['locales']['sv_SE.utf8']['probes']
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_an_entry_without_codeset_resealed(self):
        def edit(d):
            del d['locales']['sv_SE.utf8']['codeset']
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_a_count_written_as_text_resealed(self):
        def edit(d):
            d['locales']['sv_SE.utf8']['chars'] = '1112063'
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_a_locale_pointed_at_another_order(self):
        def edit(d):
            d['locales']['en_US']['order'] = d['locales']['en_US.utf8']['order']
        self.assertRefused(RHEL8, self.variant(edit))

    # damage inside a measured record

    def test_an_order_damaged(self):
        def edit(d):
            digest = d['locales']['ko_KR.utf8']['order']
            d['order_data'][digest] = damaged(d['order_data'][digest], 5000, 2)
        self.assertRefused(RHEL8, self.variant(edit))

    def test_a_record_no_report_line_opens_damaged(self):
        """en_US.utf8 did not change, so the comparison never unpacks its
        record: only the digest of the whole file can see this."""
        old, new = (read_json(p)['locales'] for p in (RHEL8, RHEL9))
        digest = new['en_US.utf8']['pairs']
        self.assertEqual(old['en_US.utf8']['pairs'], digest)
        for name in CHANGED_8_TO_9:   # the premise: nothing that changed uses it
            self.assertNotIn(digest, (old[name]['pairs'], new[name]['pairs']))

        def edit(d):
            d['pair_data'][digest] = damaged(d['pair_data'][digest], 100, 1)
        self.assertRefused(RHEL8, self.variant(edit))

    def test_an_order_damaged_resealed(self):
        def edit(d):
            digest = d['locales']['ko_KR.utf8']['order']
            d['order_data'][digest] = damaged(d['order_data'][digest], 5000, 2)
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_a_told_apart_record_damaged_resealed(self):
        """sv_SE's record is unpacked, since only it changed there."""
        old, new = (read_json(p)['locales']['sv_SE'] for p in (RHEL8, RHEL9))
        self.assertNotEqual(old['pairs'], new['pairs'])
        self.assertEqual(old['probes'], new['probes'])

        def edit(d):
            digest = d['locales']['sv_SE']['pairs']
            d['pair_data'][digest] = damaged(d['pair_data'][digest], 10, 1)
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_an_order_not_base64_resealed(self):
        def edit(d):
            d['order_data'][d['locales']['ko_KR.utf8']['order']] = '@@not base64@@'
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_an_order_ending_mid_number_resealed(self):
        def edit(d):
            digest = d['locales']['ko_KR.utf8']['order']
            raw = zlib.decompress(base64.b64decode(d['order_data'][digest]))
            d['order_data'][digest] = base64.b64encode(
                zlib.compress(raw + b'\x80')).decode('ascii')
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    def test_an_order_one_character_short_resealed(self):
        def edit(d):
            d['locales']['ko_KR.utf8']['chars'] -= 1
        self.assertRefused(RHEL8, self.variant(edit, reseal=True))

    # the pair of files

    def test_the_same_file_twice(self):
        self.assertRefused(RHEL9, RHEL9)

    def test_the_machines_given_the_wrong_way_round(self):
        self.assertRefused(RHEL9, RHEL8)

    def test_two_builds_of_one_glibc_given_the_wrong_way_round(self):
        def edit(d):
            d['glibc_build'] = 'glibc-2.34-999.el9_8.x86_64'
        self.assertRefused(self.variant(edit, reseal=True), RHEL9)


class Reports(Variants):
    """Edited measurements that still compare, and must say what is odd.

    Freezes the report's own warnings: a locale not measured the same way on
    both machines, measured only in part, or missing on the new machine is
    listed as not known to be unchanged, never counted as unchanged, and two
    machines on one glibc are named as such.
    """

    def report(self, old, new):
        rc, out, err = run_compare(old, new)
        self.assertEqual((rc, err), (0, ''))
        return parse_report(out)

    def test_two_builds_of_one_glibc_in_order(self):
        def edit(d):
            d['glibc_build'] = 'glibc-2.34-999.el9_8.x86_64'
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        self.assertEqual(r['warnings'], [])
        self.assertEqual(r['totals']['changed'], 0)

    def test_the_same_build_on_both_sides(self):
        def edit(d):
            d['python'] = '0.0'
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        self.assertEqual(r['warnings'], ['same build'])

    def test_a_build_that_cannot_be_read(self):
        def edit(d):
            d['glibc_build'] = 'weird-build-string'
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        self.assertEqual(r['warnings'], ['order unknown'])

    def test_a_locale_measured_only_in_part(self):
        def edit(d):
            d['locales']['en_US']['partly'] = 5
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        self.assertEqual(r['partly'].get('en_US'), 5)
        self.assertEqual(r['totals']['partly'], len(r['partly']))

    def test_the_probe_pairs_changed(self):
        def edit(d):
            e = d['locales']['de_DE.utf8']
            blob = d['pair_data'][e['pairs']]
            e['probes'] = e['probes'].replace('>', '<', 1)
            e['pairs'] = 'f' * 64
            d['pair_data'][e['pairs']] = blob
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        de = by_name(r)
        self.assertEqual(set(de), {'de_DE.utf8'})
        self.assertEqual(de['de_DE.utf8']['moved'], [])
        self.assertIsNotNone(de['de_DE.utf8']['probes'])

    def test_a_locale_that_could_not_be_measured_on_one_machine(self):
        def edit(d):
            d['locales']['ko_KR.utf8'] = {'error': 'cannot be selected: test',
                                          'codeset': 'UTF-8'}
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        ko = r['unmeasured']['ko_KR.utf8']
        self.assertEqual(ko['why'], {'new machine': 'cannot be selected: test'})
        self.assertEqual(ko['also']['aliases'], {'UTF-8': ['ko_KR']})
        self.assertEqual(by_name(r), {})

    def test_the_summary_carries_the_warnings_and_says_nothing_changed(self):
        def edit(d):
            d['python'] = '0.0'
        path = os.path.join(self.tmp, 'same-build-summary.txt')
        rc, out, err = run_compare(RHEL9, self.variant(edit, reseal=True),
                                   '--summary-to', path)
        self.assertEqual((rc, err), (0, ''))
        with open(path, encoding='utf-8') as f:
            summary = parse_summary(f.read())
        self.assertEqual(summary['warnings'], ['same build'])
        self.assertEqual(summary['changes'], {})

    def test_many_neighbours_told_apart_differently_and_nothing_moved(self):
        """More than twenty pairs told apart differently while no character
        moves: the report's "too many to list here" and the summary's count.
        No real pair of machines reaches this form, so without it the
        comparison of the two was blind to one of the four."""
        changed = [1000 + 7 * k for k in range(30)]

        def edit(d):
            e = d['locales']['de_DE.utf8']
            raw = bytearray(zlib.decompress(base64.b64decode(
                d['pair_data'][e['pairs']])))
            for i in changed:
                raw[i] ^= 2
            digest = hashlib.sha256(e['probes'].encode('utf-8') + b'\n'
                                    + bytes(raw)).hexdigest()
            d['pair_data'][digest] = m.pack(bytes(raw))
            e['pairs'] = digest
        path = os.path.join(self.tmp, 'many-pairs-summary.txt')
        rc, out, err = run_compare(RHEL9, self.variant(edit, reseal=True),
                                   '--summary-to', path)
        self.assertEqual((rc, err), (0, ''))
        change = by_name(parse_report(out))['de_DE.utf8']
        self.assertEqual((change['moved'], change['pairs'], change['pairs_count']),
                         ([], None, len(changed)))
        with open(path, encoding='utf-8') as f:
            brief = parse_brief(parse_summary(f.read())['changes']
                                ['de_DE.utf8']['brief'])
        self.assertEqual(brief, {'moved': [], 'moved_count': 0, 'pairs': None,
                                 'pairs_count': len(changed), 'probes': False})

    def test_measured_over_different_characters(self):
        def edit(d):
            d['locales']['ko_KR']['corpus'] = '0' * 64
        r = self.report(RHEL9, self.variant(edit, reseal=True))
        self.assertEqual(r['unmeasured']['ko_KR']['why'],
                         {'both machines': 'different characters'})
        self.assertEqual(by_name(r), {})

    def test_a_new_machine_with_only_the_english_language_pack(self):
        """13 of the 14 changed locales exist only on the old machine: each
        is listed as not on the new machine, and none is counted unchanged."""
        keep = []

        def edit(d):
            kept = {k: v for k, v in d['locales'].items()
                    if k in ('C', 'C.utf8', 'POSIX') or k.startswith('en_')}
            d['locales'], d['locales_listed'] = kept, len(kept)
            d['order_data'] = {k: v for k, v in d['order_data'].items()
                               if k in {e.get('order') for e in kept.values()}}
            d['pair_data'] = {k: v for k, v in d['pair_data'].items()
                              if k in {e.get('pairs') for e in kept.values()}}
            keep.extend(kept)
        r = self.report(RHEL8, self.variant(edit, reseal=True))
        old = read_json(RHEL8)['locales']
        self.assertEqual(r['not_on_new'], sorted(set(old) - set(keep)))
        self.assertEqual(set(by_name(r)), {'C.utf8'})
        self.assertLessEqual(CHANGED_8_TO_9 - {'C.utf8'}, set(r['not_on_new']))
        t = r['totals']
        self.assertEqual(t['changed'] + t['unchanged'] + t['not measured']
                         + t['not on new'], len(old))


# --- what the files are, and the pieces with known answers --------------------

class MeasurementsAreCurrent(unittest.TestCase):
    """The three files are what this script measures today.

    Freezes the fixture itself: if what the script measures changes, the
    files above stop being its measurements, and every answer in this module
    would be checked against another script's output. Rebuild them on three
    machines when this fails. Each locale's set of characters is rebuilt here
    with this machine's Python, so a Python whose converters differ fails
    here too, and so would a measurement taken with it.
    """

    def test_the_files_name_this_scripts_corpus(self):
        for machine, path in MACHINES:
            with self.subTest(machine=machine):
                self.assertEqual(read_json(path)['corpus'], m.corpus_digest())

    def test_every_locale_was_measured_over_the_characters_built_here(self):
        for machine, path in MACHINES:
            measured = {n: e for n, e in read_json(path)['locales'].items()
                        if 'error' not in e}
            self.assertGreater(len(measured), 800)
            for name, e in sorted(measured.items()):
                corpus = m.corpus_for(e['codeset'])
                with self.subTest(machine=machine, locale=name):
                    self.assertIsNotNone(corpus)
                    self.assertEqual((e['corpus'], e['chars']),
                                     (corpus[2], len(corpus[0])))


class PostgresNames(unittest.TestCase):
    """The names PostgreSQL gives each locale, against the catalogs of the
    three machines.

    Freezes the case of a reader who looks a change up by PostgreSQL's name
    for it and finds a different locale under that name, reported unchanged
    ("ko_KR" in a UTF-8 database is ko_KR.utf8; `locale -a`'s ko_KR is
    EUC-KR). Each catalog is, on that machine, after
    pg_import_system_collations() in the database `postgres`:

      psql -X -A -t -F'|' -c "SELECT collname, pg_encoding_to_char(collencoding),
          collcollate FROM pg_collation WHERE collprovider = 'c' ORDER BY 1, 2"
    """

    CATALOG = {'rhel8': 1006, 'rhel9': 1008, 'rhel10': 1024}

    def test_every_alias_postgres_created_is_predicted(self):
        for machine, path in MACHINES:
            with self.subTest(machine=machine):
                locales = {k: v for k, v in read_json(path)['locales'].items()
                           if v.get('codeset')}
                predicted = m.postgres_names(locales)
                with open(os.path.join(DATA, f'{machine}.pg_collation.txt'),
                          encoding='utf-8') as f:
                    rows = [row.split('|') for row in f.read().splitlines()]
                self.assertEqual(len(rows), self.CATALOG[machine])
                self.assertEqual({len(row) for row in rows}, {3})
                pg = {(name, enc): coll for name, enc, coll in rows}
                # glibc codeset -> PostgreSQL encoding, as PostgreSQL chose it
                encodings = {}
                for (name, enc), coll in pg.items():
                    if name == coll and coll in locales:
                        encodings.setdefault(locales[coll]['codeset'],
                                             set()).add(enc)
                aliases = {(name, enc): coll for (name, enc), coll in pg.items()
                           if name != coll}
                self.assertEqual(len(aliases), 156)
                for (name, enc), coll in sorted(aliases.items()):
                    self.assertEqual(predicted.get(coll, (None,))[0], name,
                                     f'{name} ({enc}) -> {coll}')
                imported = {coll for (name, enc), coll in pg.items()
                            if name == coll}
                checked = 0
                for coll, (alias, _) in sorted(predicted.items()):
                    if alias and coll in imported:
                        checked += 1
                        self.assertTrue(
                            any(pg.get((alias, e)) == coll
                                for e in encodings.get(locales[coll]['codeset'], ())),
                            f'predicted {alias} -> {coll}, not in PostgreSQL')
                self.assertEqual(checked, 156)


class RpmOrder(unittest.TestCase):
    """rpm_vercmp() against rpm itself.

    Freezes the order of two builds of one glibc: a distro can change the
    sort order inside a release, as RHEL8 did at glibc-2.28-93, and two builds
    given the wrong way round are refused only if they are ordered as rpm
    orders them. The strings are the glibc builds the three machines'
    repositories offered and the rules' corner cases ('~', '^', leading zeros,
    letters against digits).
    """

    def test_every_pair_is_ordered_as_rpm_orders_it(self):
        data = read_json(os.path.join(DATA, 'rpm_order.json'))
        strings, answers = data['strings'], data['answers']
        self.assertEqual(len(answers), len(strings) ** 2)
        self.assertIn('251.el8_10.40', strings)
        wrong = [(a, b) for i, a in enumerate(strings)
                 for j, b in enumerate(strings)
                 if '<=>'[m.rpm_vercmp(a, b) + 1] != answers[i * len(strings) + j]]
        self.assertEqual(wrong, [])


class Pieces(unittest.TestCase):
    """The parts of the comparison that need no measurement, with answers
    known in advance."""

    BASE = list(range(0x20, 0x3000))

    def test_an_order_comes_back_as_stored(self):
        """Negative steps, and the last code point, included."""
        shuffled = self.BASE[:]
        random.Random(1).shuffle(shuffled)
        for seq in (self.BASE, shuffled, self.BASE[::-1], [0x10FFFF, 1]):
            raw = zlib.decompress(base64.b64decode(m.pack_order(seq)))
            self.assertEqual(m.unpack_order(raw), seq)

    def test_an_order_cut_mid_number_is_refused(self):
        raw = zlib.decompress(base64.b64decode(m.pack_order([5, 300000])))
        self.assertIsNone(m.unpack_order(raw[:-1]))

    def test_the_characters_that_moved(self):
        base = self.BASE
        after_z = [c for c in base if c not in (0x57, 0x77)]
        i = after_z.index(0x7A) + 1
        after_z[i:i] = [0x57, 0x77]
        self.assertEqual(m.moved_characters(base, after_z), [0x57, 0x77])
        # after v, w is still between v and x: only W moved
        after_v = [c for c in base if c not in (0x57, 0x77)]
        i = after_v.index(0x76) + 1
        after_v[i:i] = [0x57, 0x77]
        self.assertEqual(m.moved_characters(base, after_v), [0x57])
        to_end = [c for c in base if c != 0x2000] + [0x2000]
        self.assertEqual(m.moved_characters(base, to_end), [0x2000])
        self.assertEqual(m.moved_characters(base, base), [])
        swapped = base[:]
        a = swapped.index(0x61)
        swapped[a], swapped[a + 1] = swapped[a + 1], swapped[a]
        self.assertEqual(len(m.moved_characters(base, swapped)), 1)

    def test_neighbours_told_apart_differently(self):
        """Only pairs that are neighbours in both orders count: once A has
        moved away, what now follows @ is B, and how @ and B are told apart
        says nothing about @ and A."""
        base = self.BASE
        old_told = bytearray(len(base) - 1)
        new_told = bytearray(old_told)
        old_told[base.index(0x56)] = 2 << 1
        self.assertEqual(m.pair_changes(base, old_told, base, new_told),
                         [(0x56, 0x57, 2 << 1, 0)])
        moved = [c for c in base if c != 0x41] + [0x41]
        told = bytearray(len(moved) - 1)
        told[moved.index(0x56)] = 2 << 1
        told[moved.index(0x40)] = 3 << 1
        self.assertEqual(m.pair_changes(base, old_told, moved, told), [])

    def test_a_build_is_read_whole_or_not_at_all(self):
        self.assertEqual(
            m.build_parts('glibc-2.34-275.el9_8.x86_64 glibc-2.34-275.el9_8.i686'),
            ('2.34', '275.el9_8'))
        self.assertIsNone(
            m.build_parts('glibc-2.34-275.el9_8.x86_64 glibc-2.34-83.el9.i686'))
        self.assertIsNone(m.build_parts('weird-build-string'))
        self.assertIsNone(m.build_parts(None))

    def test_a_missing_level_counts_every_pair_it_could_hide_in(self):
        """A pair chosen for a level between the letters and a missing one may
        be measuring the missing one: every pair the letters do not tell
        apart is counted, not only the weaker ones."""
        probe = ('a', 'b', b'a', b'b', False)
        told = bytes([0 | (2 << 3) | (2 << 5),     # different letters
                      (2 << 1) | (0 << 3) | (3 << 5),
                      (2 << 1) | (2 << 3) | (3 << 5)])
        self.assertEqual(m.unclassified([probe, probe, None], told), 2)
        self.assertEqual(m.unclassified([probe, None, None], told), 2)
        self.assertEqual(m.unclassified([probe, None, probe], told), 2)
        self.assertEqual(m.unclassified([probe, probe, probe], told), 0)
        self.assertEqual(m.unclassified([probe, m.ABSENT, probe], told), 0)

    def test_the_corpus_digest_covers_the_bytes(self):
        """Two machines whose converters write one character as different
        bytes did not measure the same thing."""
        euro = [(b'\x41', 0x41), (b'\xa4', 0x20AC)]
        self.assertNotEqual(m.corpus_id(euro),
                            m.corpus_id([(b'\x41', 0x41), (b'\x80', 0x20AC)]))
        self.assertEqual(m.corpus_id(euro), m.corpus_id(euro[::-1]))

    def test_postgres_names_for_one_encoding_each(self):
        names = m.postgres_names({
            'ko_KR': {'codeset': 'EUC-KR'}, 'ko_KR.utf8': {'codeset': 'UTF-8'},
            'C.utf8': {'codeset': 'UTF-8'}, 'sv_SE': {'codeset': 'ISO-8859-1'},
            'sv_SE.iso885915': {'codeset': 'ISO-8859-15'}})
        self.assertEqual(names['ko_KR.utf8'], ('ko_KR', 'ko_KR.UTF-8'))
        self.assertEqual(names['C.utf8'], (None, 'C.UTF-8'))
        self.assertEqual(names['sv_SE.iso885915'], ('sv_SE', 'sv_SE.ISO-8859-15'))
        self.assertEqual(names['ko_KR'], (None, None))


if __name__ == '__main__':
    unittest.main()
