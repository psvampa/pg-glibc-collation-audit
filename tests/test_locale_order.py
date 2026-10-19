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
import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
import unittest.mock
import zlib
from concurrent.futures import ThreadPoolExecutor

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

# What relation_words() can say about a pair of characters: a level, or two
# or three next to each other where a level's pair differs on the two
# machines, or less than the weakest level compared.
LEVEL_WORDS = ('as different letters', 'like an accent', 'by case')
TOLD_APART = ({'not at all (the bytes decide)', 'by less than a letter',
               'by less than an accent', 'by less than case', 'told apart'}
              | {' or '.join(LEVEL_WORDS[a:b]) for a in range(3)
                 for b in range(a + 1, 4)})
# The two lines that name the characters file, in the report and in the
# summary.
FILE_LINE = 'Every character and pair found, by locale:'
SUMMARY_FILE_LINE = '       every character and pair:'
NO_FILE_LINE = 'Pass --characters-to FILE for every character and pair found.'


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


def parse_runs(text):
    """'U+0030..U+0039, U+0B66' as [(0x30, 0x39), (0xB66, 0xB66)]."""
    runs = []
    for part in text.split(', '):
        mt = re.fullmatch(r'U\+([0-9A-F]{4,6})(?:\.\.U\+([0-9A-F]{4,6}))?', part)
        if not mt:
            raise ReportShapeError(f'unknown run of characters: {part!r}')
        a = int(mt[1], 16)
        b = int(mt[2], 16) if mt[2] else a
        if b < a or (runs and a <= runs[-1][1] + 1):
            raise ReportShapeError(f'runs out of order or touching: {text!r}')
        runs.append((a, b))
    return runs


def run_count(runs):
    return sum(b - a + 1 for a, b in runs)


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


SIDE = r'(OLD|NEW)'
EARLIER_FILES = (r'(?:the OLD and NEW files were|the (OLD|NEW) file was) '
                 r'written by an earlier version of locale_order\.py, which '
                 r'did not measure this; run it again on (both machines|the '
                 r'(OLD|NEW) machine)\.')


def earlier_sides(mt, one, where, again):
    """The sides a not-checked-for-an-earlier-version line names, refused
    unless its three mentions of them agree."""
    sides = [mt[one]] if mt[one] else ['OLD', 'NEW']
    named = ([mt[again]] if mt[again] else ['OLD', 'NEW'])
    if where is not None:
        named_where = [mt[where]] if mt[where] else ['OLD', 'NEW']
        if named_where != sides:
            raise ReportShapeError(f'sides disagree: {mt[0]!r}')
    if named != sides:
        raise ReportShapeError(f'sides disagree: {mt[0]!r}')
    return sides


def check_kind(text):
    """(kind, data) of a line about the checks beside the order: the
    control, the decimals of money, the PostgreSQL processes. Raises on a
    line of any other shape, so a reader cannot skip one."""
    mt = re.fullmatch(r'Control: C and POSIX measured as byte order on both '
                      r'machines, which is how glibc sorts them in every '
                      r'version\.', text)
    if mt:
        return 'control', {'held': True}
    mt = re.fullmatch(r'!! Control FAILED on the ' + SIDE + r' machine: '
                      r'(C|POSIX|C and POSIX) did not measure as byte order, '
                      r'which is how glibc sorts C and POSIX in every '
                      r'version\. This script did not measure there what it '
                      r'means to, and nothing it measured on that machine can '
                      r'be trusted; measure it again with this checkout\'s '
                      r'scripts/locale_order\.py\.', text)
    if mt:
        return 'control', {'failed': mt[1], 'names': mt[2].split(' and ')}
    mt = re.fullmatch(r'!! Control NOT checked: (.+)\.', text)
    if mt:
        return 'control', {'unknown': mt[1].split('; ')}
    mt = re.fullmatch(r'Decimals of money: the same in the (\d+) locale\(s\) '
                      r'measured on both machines\.', text)
    if mt:
        return 'money', {'same': int(mt[1])}
    mt = re.fullmatch(r'!! Decimals of money CHANGE in (\d+) locale\(s\): a '
                      r'money value stored with lc_monetary set to one of them '
                      r'reads with another number of decimals on the new '
                      r'machine, as a number 10 or more times larger or '
                      r'smaller: (.+)\.', text)
    if mt:
        changed = {}
        for item in mt[2].split(', '):
            it = re.fullmatch(r'(\S+) \((\d+) -> (\d+)\)', item)
            if not it or it[2] == it[3]:
                raise ReportShapeError(f'unknown money change: {item!r}')
            changed[it[1]] = (int(it[2]), int(it[3]))
        if len(changed) != int(mt[1]):
            raise ReportShapeError(f'money says {mt[1]} and lists {len(changed)}')
        return 'money', {'changed': changed}
    mt = re.fullmatch(r'!! Decimals of money NOT checked in (\d+) '
                      r'locale\(s\): (.+)\.', text)
    if mt:
        unknown = {}
        for item in mt[2].split('; '):
            it = re.fullmatch(r'(\S+) \((old|new) machine\): (.+)', item)
            if not it:
                raise ReportShapeError(f'unknown money item: {item!r}')
            unknown.setdefault(it[1], {})[it[2]] = it[3]
        if len(unknown) != int(mt[1]):
            raise ReportShapeError(f'money says {mt[1]} and lists {len(unknown)}')
        return 'money', {'unknown': unknown}
    mt = re.fullmatch(r'!! Decimals of money NOT checked: ' + EARLIER_FILES, text)
    if mt:
        return 'money', {'earlier': earlier_sides(mt, 1, None, 3)}
    mt = re.fullmatch(r'PostgreSQL on the ' + SIDE + r' machine: (\d+) '
                      r'process\(es\) named postgres or postmaster, all running '
                      r'the C library measured there\.', text)
    if mt:
        return 'libc', {'side': mt[1], 'clean': int(mt[2])}
    mt = re.fullmatch(r'!! PostgreSQL on the ' + SIDE + r' machine: no process '
                      r'named postgres or postmaster was found, so nothing '
                      r'checked which C library PostgreSQL runs there\.', text)
    if mt:
        return 'libc', {'side': mt[1], 'none': True}
    mt = re.fullmatch(r'!! PostgreSQL on the ' + SIDE + r' machine: (\d+) '
                      r'process\(es\) run another C library than the one '
                      r'measured there, as when PostgreSQL runs in a container, '
                      r'so they do not sort with the glibc measured there\.', text)
    if mt:
        return 'libc', {'side': mt[1], 'other': int(mt[2])}
    mt = re.fullmatch(r'!! PostgreSQL on the ' + SIDE + r' machine: (\d+) '
                      r'process\(es\) still run the glibc that was replaced on '
                      r'disk, so they do not sort with the glibc measured '
                      r'there until PostgreSQL is restarted\.', text)
    if mt:
        return 'libc', {'side': mt[1], 'stale': int(mt[2])}
    mt = re.fullmatch(r'!! PostgreSQL processes NOT checked on (both machines|'
                      r'the (OLD|NEW) machine): ' + EARLIER_FILES, text)
    if mt:
        return 'libc', {'earlier': earlier_sides(mt, 3, 2, 5)}
    mt = re.fullmatch(r'!! PostgreSQL processes NOT checked on the ' + SIDE
                      + r' machine: (.+)\. Run locale_order\.py there as root, '
                      r'or as the user PostgreSQL runs as where /proc does not '
                      r'hide other users\' processes\.', text)
    if mt:
        return 'libc', {'side': mt[1], 'why': mt[2]}
    mt = re.fullmatch(r'!! The glibc of the ' + SIDE + r' machine was replaced '
                      r'on disk while it was measured, so what was measured may '
                      r'not be what that machine runs now; measure it again\.',
                      text)
    if mt:
        return 'libc', {'side': mt[1], 'replaced': True}
    if text == ('This looks at the C library PostgreSQL runs, not at its '
                'locale data: a PostgreSQL that was not restarted can already '
                'use locales installed after it started.'):
        return 'libc', {'note': True}
    raise ReportShapeError(f'unknown check line: {text!r}')


def voids(kind, d):
    """Does this check void the comparison, and so stand above the list of
    changes: the control; the OLD machine's PostgreSQL on a replaced or
    another C library; the NEW machine's on another; a side whose glibc was
    replaced while it was measured."""
    return kind == 'control' or (kind == 'libc' and (
        d.get('replaced') or 'other' in d
        or ('stale' in d and d['side'] == 'OLD')))


def rank(kind, d):
    """Where a check stands: the control, then what else voids the
    comparison, above the list; money, then the rest of PostgreSQL's, after
    the totals."""
    if kind == 'control':
        return 0
    if voids(kind, d):
        return 1
    return 2 if kind == 'money' else 3


def after_totals(found, where):
    """A check read after the totals, refused if it voids the comparison:
    that stands above the list of changes, where it is read before the list
    it voids."""
    if voids(*found):
        raise ReportShapeError(f'{where}: {found} after the totals')
    return found


def above_the_list(found, where):
    """A check read above the list, refused unless it voids the comparison."""
    if not voids(*found):
        raise ReportShapeError(f'{where}: {found} above the list')
    return found


def checks_in_order(found, where):
    """found, refused unless the checks come in rank order."""
    ranks = [rank(*f) for f in found]
    if ranks != sorted(ranks):
        raise ReportShapeError(f'{where}: checks out of order: {found}')
    return found


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
    while (line().startswith('     !! ') and not line().startswith('     !! Not measured: ')
           and not line().startswith(tuple('     ' + p for p in ('Control: ', '!! Control ', '!! PostgreSQL on the ', '!! The glibc of the ')))):
        s['warnings'].append(warning_kind(item(5, 8)))
    s['checks'] = []
    while line().startswith(tuple('     ' + p for p in ('Control: ', '!! Control ', '!! PostgreSQL on the ', '!! The glibc of the '))):
        s['checks'].append(above_the_list(check_kind(item(5, 8)), 'summary'))
    s['changes'] = {}
    s['characters_file'] = None
    if line() == '     no locale measured on both machines sorts differently':
        count = 0
        at[0] += 1
    else:
        count = int(take(r'     (\d+) locale\(s\) sort differently -- '
                         r'reindex what uses them:')[1])
        while True:
            if line() == SUMMARY_FILE_LINE:
                at[0] += 1
                s['characters_file'] = take(r'         (\S.*)')[1]
                break
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
    while re.fullmatch(r'     \S.*', line()) and not line().startswith('     !! Not measured: '):
        s['checks'].append(after_totals(check_kind(item(5, 8)), 'summary'))
    checks_in_order(s['checks'], 'summary')
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
    while line().startswith('!! ') and not line().startswith(('Control: ', '!! Control ', '!! PostgreSQL on the ', '!! The glibc of the ')):
        r['warnings'].append(warning_kind(paragraph()))
    r['checks'] = []
    while line().startswith(('Control: ', '!! Control ', '!! PostgreSQL on the ', '!! The glibc of the ')):
        r['checks'].append(above_the_list(check_kind(paragraph()), 'report'))
    take('')

    changed = int(take(r'Sort order CHANGED in (\d+) locale\(s\):')[1])
    r['changes'] = []
    r['characters_file'] = None
    while True:
        take('')
        if line().startswith('Unchanged: '):
            # Every change is followed by where its whole list is, or by how
            # to get it.
            if r['changes']:
                fail('changes with no word on the characters file')
            break
        if line() in (FILE_LINE, NO_FILE_LINE):
            if not r['changes']:
                fail('the characters file named with no change')
            if line() == FILE_LINE:
                at[0] += 1
                r['characters_file'] = take(r'  (\S.*)')[1]
            else:
                at[0] += 1
            take('')
            if not line().startswith('Unchanged: '):
                fail('expected the unchanged count after the characters file')
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
    while re.fullmatch(r'\S.*', line()) and not line().startswith('!! Not measured: '):
        r['checks'].append(after_totals(check_kind(paragraph()), 'report'))
    checks_in_order(r['checks'], 'report')
    if not line().startswith('!! Not measured: '):
        fail('expected the closing "!! Not measured" paragraph')
    r['closing'] = paragraph()
    if line() != END:
        fail('a line after the closing paragraph')
    return r


def number(text):
    return int(text.replace(',', ''))


def parse_details(details):
    """What changed in one locale, in the order the report gives it: the
    characters that moved; how many sort differently against at least one
    other; the probe pairs, when they changed, and the levels still
    compared; the pairs told apart differently, and the characters among
    them that did not move; the pairs the measurement cannot decide."""
    out = {'moved': None, 'moved_count': None, 'moved_runs': None,
           'reversed': None, 'all_reversed': False, 'pairs': None,
           'pairs_count': None, 'probes': None, 'compared': None,
           'not_measured': None, 'still': None, 'still_count': None,
           'undecided': 0}
    if not details:
        raise ReportShapeError('a change with no detail lines')
    rest = list(details)

    def wrapped_runs(text, indent):
        """Runs that wrap onto lines indented by indent."""
        while rest and re.fullmatch(' ' * indent + r'U\+\S.*', rest[0]):
            text += ' ' + rest.pop(0).strip()
        return parse_runs(text)

    first = rest.pop(0)
    many = re.fullmatch(r'([\d,]+) characters moved, too many to list here',
                        first)
    ranged = re.fullmatch(r'([\d,]+) characters moved: (U\+.*)', first)
    listed = re.fullmatch(r'(\d+) character\(s\) moved:', first)
    if first == 'no character moved':
        out['moved'] = []
    elif many:
        out['moved_count'] = number(many[1])
    elif ranged:
        out['moved_runs'] = wrapped_runs(ranged[2], 2)
        out['moved_count'] = number(ranged[1])
        if run_count(out['moved_runs']) != out['moved_count']:
            raise ReportShapeError(f'moved says {ranged[1]} and its runs hold '
                                   f'{run_count(out["moved_runs"])}')
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

    mt = rest and re.fullmatch(r'(all )?([\d,]+) character\(s\) sort '
                               r'differently against at least one other', rest[0])
    if mt:
        out['reversed'], out['all_reversed'] = number(mt[2]), bool(mt[1])
        rest.pop(0)
    if rest and rest[0] == ('the pairs that tell letters, accents and case '
                            'apart here are told apart differently themselves:'):
        before = re.fullmatch(r'  before: (.+)', rest[1] if len(rest) > 1 else '')
        now = re.fullmatch(r'  now:    (.+)', rest[2] if len(rest) > 2 else '')
        compared = re.fullmatch(r'  so pairs are compared only by (.+)|  so no '
                                r'pair can be compared',
                                rest[3] if len(rest) > 3 else '')
        if not (before and now and compared):
            raise ReportShapeError(f'unknown probe lines: {rest[1:4]!r}')
        out['probes'] = (before[1], now[1])
        out['compared'] = compared[1].split(' and ') if compared[1] else []
        rest = rest[4:]
    mt = rest and re.fullmatch(r'(?:pairs are compared only by (.+)|no pair can '
                               r'be compared): no pair measured (.+)', rest[0])
    if mt:
        if out['probes'] is not None:
            raise ReportShapeError('levels not measured beside changed probes')
        out['compared'] = mt[1].split(' and ') if mt[1] else []
        out['not_measured'] = mt[2].split(' and ')
        rest.pop(0)
    many = rest and re.fullmatch(r'([\d,]+) pairs of characters told apart '
                                 r'differently, too many to list here', rest[0])
    listed = rest and re.fullmatch(r'(\d+) pair\(s\) of characters told apart '
                                   r'differently:', rest[0])
    if many:
        out['pairs_count'] = number(many[1])
        rest.pop(0)
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
    mt = rest and re.fullmatch(r'  among them ([\d,]+) character\(s\) that did '
                               r'not move(?:: (U\+.*)|, too many to list here)',
                               rest[0])
    if mt:
        if out['pairs_count'] is None:
            raise ReportShapeError('characters that did not move, with no pairs')
        rest.pop(0)
        out['still_count'] = number(mt[1])
        if mt[2]:
            out['still'] = wrapped_runs(mt[2], 4)
            if run_count(out['still']) != out['still_count']:
                raise ReportShapeError(f'did not move says {mt[1]} and its runs '
                                       f'hold {run_count(out["still"])}')
    mt = rest and re.fullmatch(r'([\d,]+) more pair\(s\) may be told apart '
                               r'differently; what was measured cannot decide',
                               rest[0])
    if mt:
        out['undecided'] = number(mt[1])
        rest.pop(0)
    if rest:
        raise ReportShapeError(f'unknown detail lines: {rest!r}')
    return out


def parse_brief(text):
    """A summary's one phrase of what moved, read into the keys parse_details
    gives the report's lines, so that the two can be compared one to one."""
    out = {'moved': None, 'moved_count': None, 'reversed': None,
           'pairs': None, 'pairs_count': None, 'probes': False, 'still': None,
           'still_count': None, 'compared': None, 'undecided': 0}
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
        many = re.fullmatch(r'([\d,]+) pairs of characters told apart '
                            r'differently', part)
        listed = re.fullmatch(r'(\d+) pair\(s\) of characters told apart '
                              r'differently: (.+)', part)
        reversed_ = re.fullmatch(r'([\d,]+) sort differently against another',
                                 part)
        still = re.fullmatch(r'among them ([\d,]+) character\(s\) that did not '
                             r'move(?:: (.+))?', part)
        compared = re.fullmatch(r'pairs compared only by (.+)|no pair compared',
                                part)
        undecided = re.fullmatch(r'([\d,]+) more pair\(s\) the measurement '
                                 r'cannot decide', part)
        if part == ('the pairs that tell letters, accents and case apart are '
                    'told apart differently themselves'):
            out['probes'] = True
        elif reversed_:
            out['reversed'] = number(reversed_[1])
        elif compared:
            out['compared'] = compared[1].split(' and ') if compared[1] else []
        elif undecided:
            out['undecided'] = number(undecided[1])
        elif still:
            out['still_count'] = number(still[1])
            if still[2]:
                out['still'] = parse_runs(still[2])
                if run_count(out['still']) != out['still_count']:
                    raise ReportShapeError(f'phrase counts {still[1]}: {text!r}')
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


# --- every character, in a file ------------------------------------------------

FINDINGS = ('moved', 'sorts differently', 'pairs changed', 'not compared',
            'told apart differently', 'not decided', 'not measured',
            'not on the new machine', 'unchanged only as far as measured',
            'also in PostgreSQL')
# The rows of a locale NOT known to be unchanged, beside the changes.
NOT_KNOWN = ('not measured', 'not on the new machine')
# And of a locale unchanged as far as measured: none of the three changed.
NOT_CHANGED = NOT_KNOWN + ('unchanged only as far as measured',)
PARTLY_ROW = (r'(\d+) pair\(s\) of neighbours differ by less than a letter, '
              r'and no pair could tell whether by an accent or by case')
MEASURED_ON = {'on both machines',
               'on the old machine; the new one by the characters between',
               'on the new machine; the old one by the characters between'}
CODE = r'U\+([0-9A-F]{4,6})'


def parse_chain(text):
    """'U+0030, U+0B66' or 'U+0378..U+0380': the characters of a chain of
    pairs, in the order of the chain, which need not be ascending."""
    out = []
    for part in text.split(', '):
        mt = re.fullmatch(rf'{CODE}(?:\.\.{CODE})?', part)
        if not mt:
            raise ReportShapeError(f'unknown chain of characters: {text!r}')
        a = int(mt[1], 16)
        b = int(mt[2], 16) if mt[2] else a
        if b < a:
            raise ReportShapeError(f'a run that goes down: {text!r}')
        out.extend(range(a, b + 1))
    return out


def parse_test(text):
    """'U+0030 U+0041 / U+0B66 U+0061' as ([0x30, 0x41], [0xB66, 0x61])."""
    halves = text.split(' / ')
    if len(halves) != 2:
        raise ReportShapeError(f'unknown test pair: {text!r}')
    out = []
    for half in halves:
        if not re.fullmatch(rf'{CODE}(?: {CODE})*', half):
            raise ReportShapeError(f'unknown test string: {half!r}')
        out.append([int(h, 16) for h in re.findall(CODE, half)])
    return tuple(out)


class CharactersFile(dict):
    """{locales (sorted tuple): [row]}, with the notes under the rows:
    .warnings, their kinds as warning_kind() names them, .checks, as
    check_kind() reads them, and .closing."""


def parse_characters(text):
    """The file --characters-to writes, every row read and checked, as a
    CharactersFile; each row a dict. Strict like the report: a line it does
    not know raises, and so does a note above a row."""
    lines = text.split('\n')
    if lines[-1] != '':
        raise ReportShapeError('the characters file does not end in a newline')
    lines.pop()
    if not re.fullmatch(r'# Step 11, .+ -> .+: every character and pair '
                        r'found, by locale\.', lines[0]):
        raise ReportShapeError(f'unknown first line: {lines[0]!r}')
    if lines[1] != 'locales\tfinding\tcharacters\thow\ttest\tbefore\tnow\tmeasured':
        raise ReportShapeError(f'unknown column line: {lines[1]!r}')
    # The last line counts the rows above it: a file cut anywhere says so.
    end = re.fullmatch(r'# end: (\d+) locale group\(s\), (\d+) row\(s\)',
                       lines.pop() if len(lines) > 2 else '')
    if not end:
        raise ReportShapeError('the characters file has no end line')
    # The notes: the report's warnings, then the closing paragraph, last.
    notes = []
    while len(lines) > 2 and lines[-1].startswith('# '):
        notes.insert(0, lines.pop()[2:])
    if not notes or not notes[-1].startswith('!! Not measured: '):
        raise ReportShapeError('the characters file has no closing note')
    out = CharactersFile()
    out.closing = notes.pop()
    at = 0
    while at < len(notes) and not re.match(
            r'(Control: |!! Control |Decimals of money|!! Decimals of money|'
            r'PostgreSQL on |!! PostgreSQL|!! The glibc of |This looks at )',
            notes[at]):
        at += 1
    out.warnings = [warning_kind(note) for note in notes[:at]]
    out.checks = checks_in_order([check_kind(note) for note in notes[at:]],
                                 'characters file')
    if int(end[2]) != len(lines) - 2:
        raise ReportShapeError(f'the end line counts {end[2]} rows and the file '
                               f'holds {len(lines) - 2}')
    for n, line in enumerate(lines[2:], 3):
        cells = line.split('\t')
        if len(cells) != 8 or cells[1] not in FINDINGS:
            raise ReportShapeError(f'characters line {n}: {line!r}')
        names, finding, chars, how, test, before, now, measured = cells
        row = {'finding': finding, 'how': None, 'test': None,
               'before': None, 'now': None, 'measured': None}
        if finding == 'also in PostgreSQL':
            if (chars, test, before, now, measured) != ('-',) * 5:
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['also'] = parse_also('also, in PostgreSQL: ' + how)
            group = out.setdefault(tuple(names.split(',')), [])
            if any(r['finding'] == finding for r in group):
                raise ReportShapeError(f'characters line {n}: a second {line!r}')
            group.append(row)
            continue
        if out.get(tuple(names.split(',')), [{}])[-1].get('finding') == 'also in PostgreSQL':
            raise ReportShapeError(f'characters line {n}: a row after the '
                                   f'PostgreSQL names: {line!r}')
        if finding in ('pairs changed', 'unchanged only as far as measured'):
            mt = (re.fullmatch(r'before: (.+); now: (.+)', how)
                  if finding == 'pairs changed' else re.fullmatch(PARTLY_ROW, how))
            if (chars, test, before, now, measured) != ('-',) * 5 or not mt:
                raise ReportShapeError(f'characters line {n}: {line!r}')
            if finding == 'pairs changed':
                row['probes'] = (mt[1], mt[2])
            else:
                row['count'] = int(mt[1])
            out.setdefault(tuple(names.split(',')), []).append(row)
            continue
        if finding in NOT_KNOWN:
            if (chars, test, before, now, measured) != ('-',) * 5 or not how or (
                    (how == '-') != (finding == 'not on the new machine')):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['why'] = None if how == '-' else how
            out.setdefault(tuple(names.split(',')), []).append(row)
            continue
        if finding == 'not compared':
            levels = how.split(' and ')
            if (chars, test, before, now, measured) != ('-',) * 5 or not (
                    set(levels) <= {'letters', 'accents', 'case'}):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['levels'] = levels
            out.setdefault(tuple(names.split(',')), []).append(row)
            continue
        if finding in ('moved', 'sorts differently'):
            row['runs'] = parse_runs(chars)
        elif finding == 'told apart differently':
            row['chars'] = parse_chain(chars)
            if len(row['chars']) < 2:
                raise ReportShapeError(f'characters line {n}: {line!r}')
        else:
            mt = re.fullmatch(rf'{CODE} {CODE}', chars)
            if not mt or mt[1] == mt[2]:
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['chars'] = [int(mt[1], 16), int(mt[2], 16)]
        if how != '-':
            pair = how.split(' -> ')
            if (finding != 'told apart differently' or len(pair) != 2
                    or not set(pair) <= TOLD_APART):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['how'] = tuple(pair)
        if test != '-':
            row['test'] = parse_test(test)
            if before not in ('<', '>') or now not in ('<', '>') or before == now:
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['before'], row['now'] = before, now
        elif (before, now) != ('-', '-'):
            raise ReportShapeError(f'characters line {n}: {line!r}')
        if measured != '-':
            if measured not in MEASURED_ON or finding == 'not decided':
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['measured'] = measured
        elif finding != 'not decided':
            raise ReportShapeError(f'characters line {n}: {line!r}')
        if finding == 'told apart differently' and row['how'] is None:
            raise ReportShapeError(f'characters line {n}: {line!r}')
        if finding == 'moved' and row['test'] is None:
            raise ReportShapeError(f'characters line {n}: {line!r}')
        out.setdefault(tuple(names.split(',')), []).append(row)
    if len(out) != int(end[1]):
        raise ReportShapeError(f'the end line counts {end[1]} groups and the '
                               f'file holds {len(out)}')
    return out


def changes_in(files):
    """The groups of a characters file that changed, without those NOT
    known to be unchanged or unchanged only as far as measured."""
    out = {}
    for names, rows in files.items():
        kinds = {r['finding'] in NOT_CHANGED for r in rows
                 if r['finding'] != 'also in PostgreSQL'}
        if len(kinds) != 1:
            raise ReportShapeError(f'{names} is both changed and not')
        if kinds == {False}:
            out[names] = rows
    return out


def characters_of(rows, finding):
    """Every character the rows of one finding name, as a set."""
    out = set()
    for row in rows:
        if row['finding'] == finding:
            if 'runs' in row:
                for a, b in row['runs']:
                    out.update(range(a, b + 1))
            else:
                out.update(row['chars'])
    return out


# What each output says of each kind of finding of a change, in one form
# the three can be compared in: a count, or whether the kind is there.
LOCALE_KINDS = ('moved', 'reversed', 'probes changed', 'left out',
                'told apart', 'did not move', 'undecided')


def kinds_in_report(c):
    """From the report's lines of a change, as parse_details reads them."""
    return {'moved': c['moved_count'], 'reversed': c['reversed'],
            'probes changed': c['probes'] is not None,
            'left out': c['compared'] is not None,
            'told apart': c['pairs_count'] or 0,
            'did not move': c['still_count'], 'undecided': c['undecided']}


def kinds_in_brief(b):
    """From the summary's phrase, as parse_brief reads it."""
    return {'moved': b['moved_count'], 'reversed': b['reversed'],
            'probes changed': b['probes'],
            'left out': b['compared'] is not None,
            'told apart': b['pairs_count'] or 0,
            'did not move': b['still_count'], 'undecided': b['undecided']}


def kinds_in_rows(rows):
    """From the file's rows of a change. The characters that did not move
    have no rows of their own: they are those the told-apart rows name and
    no moved row does, and the report names them only when some moved."""
    moved = characters_of(rows, 'moved')
    reversed_ = characters_of(rows, 'sorts differently')
    still = characters_of(rows, 'told apart differently') - moved
    return {'moved': len(moved), 'reversed': len(reversed_) or None,
            'probes changed': any(r['finding'] == 'pairs changed' for r in rows),
            'left out': any(r['finding'] == 'not compared' for r in rows),
            'told apart': sum(len(r['chars']) - 1 for r in rows
                              if r['finding'] == 'told apart differently'),
            'did not move': len(still) if moved and still else None,
            'undecided': sum(r['finding'] == 'not decided' for r in rows)}


def runs_set(runs):
    return {c for a, b in runs for c in range(a, b + 1)}


def named_in_report(c):
    """The characters and pairs the report's lines of a change name, where
    it lists them rather than count them; None where it only counts."""
    moved = (set(c['moved']) if c['moved'] is not None
             else runs_set(c['moved_runs']) if c['moved_runs'] else None)
    return {'moved': moved,
            'did not move': runs_set(c['still']) if c['still'] else None,
            'told apart': ({(x, y) for x, y, *_ in c['pairs']}
                           if c['pairs'] is not None else None)}


def named_in_brief(b):
    return {'moved': set(b['moved']) if b['moved'] is not None else None,
            'did not move': runs_set(b['still']) if b['still'] else None,
            'told apart': set(b['pairs']) if b['pairs'] is not None else None}


def named_in_rows(rows):
    moved = characters_of(rows, 'moved')
    return {'moved': moved,
            'did not move': characters_of(rows, 'told apart differently') - moved,
            'told apart': {pair for r in rows
                           if r['finding'] == 'told apart differently'
                           for pair in zip(r['chars'], r['chars'][1:])}}


def assert_named_alike(test, report, brief, rows):
    """Wherever the report or the summary lists what changed, it lists what
    the file's rows hold: the same characters, the same pairs."""
    in_rows = named_in_rows(rows)
    for said in (named_in_report(report), named_in_brief(brief)):
        for kind, names in said.items():
            if names is not None:
                test.assertEqual(names, in_rows[kind], kind)


def also_of(alsos):
    """parse_also answers merged, as ({encoding: {collation}}, {spelling})."""
    aliases, spellings = {}, set()
    for also in alsos:
        if also:
            for encoding, names in also['aliases'].items():
                aliases.setdefault(encoding, set()).update(names)
            spellings.update(also['spellings'])
    return aliases, spellings


def file_of(rows, label='x', warnings=(), closing=m.CLOSING):
    """The text of a characters file holding rows (cells) under one label,
    as compare() writes one."""
    lines = ['# Step 11, a -> b: every character and pair found, by locale.',
             'locales\tfinding\tcharacters\thow\ttest\tbefore\tnow\tmeasured']
    lines += [label + '\t' + '\t'.join(row) for row in rows]
    lines += ['# ' + text for text in list(warnings) + [closing]]
    lines.append(f'# end: {1 if rows else 0} locale group(s), {len(rows)} row(s)')
    return '\n'.join(lines) + '\n'


LOADED = {}


def order_of(path, name):
    """The measured order of one locale in a file, as the comparison reads it."""
    if path not in LOADED:
        LOADED[path] = m.load(path, m.corpus_digest())[0]
    data = LOADED[path]
    r = data['locales'][name]
    return m.unpacked(data, 'order', r['order'], r['chars'])


# --- what audit.sh asks of the comparison -------------------------------------

class SummaryForTheAudit(unittest.TestCase):
    """--summary-to and --characters-to: the block audit.sh prints under its
    summary, and the file of every character and pair it names.

    Freezes the summary as a second account of the comparison that could say
    less than the report: every locale the report names as changed, not
    measured or missing on the new machine is named here too, with the names
    PostgreSQL gives it, the counts are the report's own, and so are the
    warnings.

    Freezes the characters file as complete. The report prints twenty names
    at most, and until now printed "too many to list here" and nothing else;
    the file is where every character is, so a file that held fewer than the
    comparison found would read as the whole answer. Each list is checked
    against the comparison's own functions run on the measurements, not
    against the report's counts. Both pairs are compared once, for every
    test here.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='locale_order_summary_')
        cls.addClassCleanup(shutil.rmtree, cls.tmp)
        cls.summaries, cls.reports, cls.files = {}, {}, {}
        for key, old, new in (('8-9', RHEL8, RHEL9), ('9-10', RHEL9, RHEL10)):
            path = os.path.join(cls.tmp, key)
            rc, out, err = run_compare(old, new, '--summary-to', path + '.txt',
                                       '--characters-to', path + '.tsv')
            assert (rc, err) == (0, ''), err
            cls.reports[key] = parse_report(out)
            with open(path + '.txt', encoding='utf-8') as f:
                cls.summaries[key] = parse_summary(f.read())
            with open(path + '.tsv', encoding='utf-8') as f:
                cls.files[key] = parse_characters(f.read())

    def summary_and_report(self, key):
        self.assertEqual(self.reports[key]['characters_file'],
                         os.path.join(self.tmp, key + '.tsv'))
        return self.summaries[key], self.reports[key]

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
                    (brief['moved'], brief['moved_count'], brief['reversed'],
                     brief['probes'], brief['pairs'], brief['pairs_count'],
                     brief['still'], brief['still_count'], brief['compared'],
                     brief['undecided']),
                    (change['moved'], change['moved_count'], change['reversed'],
                     change['probes'] is not None,
                     change['pairs'] and [(x, y) for x, y, *_ in change['pairs']],
                     change['pairs_count'], change['still'], change['still_count'],
                     change['compared'], change['undecided']))
        self.assertEqual(summary['characters_file'], report['characters_file'])
        self.assertEqual(summary['machines'],
                         (report['old'][0], report['new'][0]))
        self.assertEqual(summary['unmeasured'], list(report['unmeasured']))
        self.assertEqual(summary['not_on_new'], report['not_on_new'])
        self.assertEqual(summary['partly'], list(report['partly']))
        self.assertEqual(summary['totals'], report['totals_line'])
        self.assertEqual(summary['warnings'], report['warnings'])
        self.assertEqual(summary['closing'], report['closing'])

    def test_rhel8_to_rhel9(self):
        summary, report = self.summary_and_report('8-9')
        self.assertSameAnswer(summary, report)
        self.assertEqual(set(summary['changes']), CHANGED_8_TO_9)
        self.assertIn('U+D7A3', summary['changes']['ko_KR.utf8']['brief'])
        self.assertEqual(summary['unmeasured_also']['aliases'].get('EUC-TW'),
                         ['zh_TW'])

    def test_rhel9_to_rhel10(self):
        summary, report = self.summary_and_report('9-10')
        self.assertSameAnswer(summary, report)
        self.assertEqual(set(summary['changes']), CHANGED_9_TO_10)

    def rows(self, key, name):
        """The rows of the change that lists name."""
        found = [rows for names, rows in self.files[key].items() if name in names]
        self.assertEqual(len(found), 1, name)
        return found[0]

    def test_every_change_of_the_report_has_its_rows(self):
        """The file keeps apart locales measured differently that the report
        prints under one heading because they read alike (the Swedish
        locales in ISO-8859-1 and ISO-8859-15): their characters differ."""
        for key in self.files:
            with self.subTest(pair=key):
                report = [set(c['names']) for c in self.reports[key]['changes']]
                changes = changes_in(self.files[key])
                for names in changes:
                    self.assertEqual(sum(set(names) <= r for r in report), 1, names)
                self.assertEqual(set().union(*map(set, changes)),
                                 set().union(*report))

    def test_the_file_holds_what_the_report_counts(self):
        """For every change of both pairs: the moved characters, those that
        sort differently, the pairs told apart differently and the pairs not
        decided, counted from the file's rows. A file cut anywhere, at its
        head or its tail, holds less than the report says."""
        for key in self.files:
            report = by_name(self.reports[key])
            for names, rows in changes_in(self.files[key]).items():
                change = report[names[0]]
                with self.subTest(pair=key, locales=names):
                    moved = characters_of(rows, 'moved')
                    told = sum(len(r['chars']) - 1 for r in rows
                               if r['finding'] == 'told apart differently')
                    undecided = sum(r['finding'] == 'not decided' for r in rows)
                    self.assertEqual(len(moved), change['moved_count'])
                    self.assertEqual(len(characters_of(rows, 'sorts differently')),
                                     change['reversed'] or 0)
                    self.assertEqual(told, change['pairs_count'] or 0)
                    self.assertEqual(undecided, change['undecided'])

    def test_the_locales_not_known_to_be_unchanged_have_rows(self):
        """Each locale the report lists as not measured, with its reason, and
        each one missing on the new machine: a file that left them out would
        read them as unchanged."""
        for key in self.files:
            with self.subTest(pair=key):
                report = self.reports[key]
                rows = {names[0]: rows[0] for names, rows in self.files[key].items()
                        if rows[0]['finding'] in NOT_KNOWN}
                self.assertEqual(
                    {n for n, r in rows.items() if r['finding'] == 'not measured'},
                    set(report['unmeasured']))
                self.assertEqual(
                    {n for n, r in rows.items()
                     if r['finding'] == 'not on the new machine'},
                    set(report['not_on_new']))
                for name, entry in report['unmeasured'].items():
                    self.assertEqual(rows[name]['why'], 'both machines: '
                                     + entry['why']['both machines'])

    def test_a_cut_file_is_refused(self):
        """The end line counts the rows: a file cut on a row boundary, which
        reads whole otherwise, is refused by its own last line."""
        path = os.path.join(self.tmp, '9-10.tsv')
        with open(path, encoding='utf-8') as f:
            lines = f.read().split('\n')
        parse_characters('\n'.join(lines))
        for cut in (lines[:-2] + [''], lines[:-3] + lines[-2:]):
            with self.subTest(lines=len(cut)):
                with self.assertRaises(ReportShapeError):
                    parse_characters('\n'.join(cut))

    def test_every_moved_character_is_listed(self):
        """Against moved_characters() on the two orders: 1, 2, 135,360 and 88."""
        for key, old, new, name in (('8-9', RHEL8, RHEL9, 'ko_KR.utf8'),
                                    ('8-9', RHEL8, RHEL9, 'sv_SE.utf8'),
                                    ('8-9', RHEL8, RHEL9, 'C.utf8'),
                                    ('9-10', RHEL9, RHEL10, 'th_TH')):
            with self.subTest(locale=name):
                moved = m.moved_characters(order_of(old, name), order_of(new, name))
                self.assertEqual(characters_of(self.rows(key, name), 'moved'),
                                 set(moved))
                change = by_name(self.reports[key])[name]
                self.assertEqual(change['moved_count'], len(moved))

    def test_every_character_in_a_reversed_pair_is_listed(self):
        """Counted a second way here: a character is in no reversed pair
        exactly when it stands at the same place in both orders and the
        characters before it are the same. ko_KR: the one that moved and the
        4,888 it moved past."""
        for key, old, new, name, count in (
                ('8-9', RHEL8, RHEL9, 'ko_KR.utf8', 4889),
                ('9-10', RHEL9, RHEL10, 'th_TH', 245)):
            with self.subTest(locale=name):
                a, b = order_of(old, name), order_of(new, name)
                # seen[c] is 1 while c is before the place in one order only;
                # apart counts those.
                seen, apart, outside = bytearray(0x110000), 0, set()
                for x, y in zip(a, b):
                    if x == y and not apart:
                        outside.add(x)
                    for c in (x, y):
                        seen[c] ^= 1
                        apart += 1 if seen[c] else -1
                listed = characters_of(self.rows(key, name), 'sorts differently')
                self.assertEqual(listed, set(a) - outside)
                self.assertEqual(len(listed), count)
                self.assertEqual(by_name(self.reports[key])[name]['reversed'], count)
        # The Swedish locales in UTF-8, counted by the comparison alone.
        self.assertEqual(len(characters_of(self.rows('8-9', 'sv_SE.utf8'),
                                           'sorts differently')), 44)
        self.assertEqual(by_name(self.reports['8-9'])['sv_SE.utf8']['reversed'], 44)

    def test_a_rewritten_order_reverses_every_character(self):
        changes = by_name(self.reports['8-9'])
        self.assertEqual((changes['or_IN']['reversed'],
                          changes['or_IN']['all_reversed']), (1112063, True))
        self.assertEqual((changes['C.utf8']['reversed'],
                          changes['C.utf8']['all_reversed']), (1112062, False))
        self.assertEqual(by_name(self.reports['9-10'])['th_TH.utf8']['reversed'],
                         1112062)

    def test_each_moved_run_has_a_test_pair_that_turns_round(self):
        """Two single characters, the first of the run and one that kept its
        place, in the order each machine measured: the pair a database would
        sort differently."""
        for key, old, new, name in (('8-9', RHEL8, RHEL9, 'ko_KR.utf8'),
                                    ('8-9', RHEL8, RHEL9, 'sv_SE.utf8'),
                                    ('9-10', RHEL9, RHEL10, 'th_TH')):
            with self.subTest(locale=name):
                before = {c: i for i, c in enumerate(order_of(old, name))}
                now = {c: i for i, c in enumerate(order_of(new, name))}
                rows = [r for r in self.rows(key, name) if r['finding'] == 'moved']
                self.assertTrue(rows)
                for row in rows:
                    (x,), (y,) = row['test']
                    self.assertEqual(x, row['runs'][0][0])
                    self.assertEqual(row['before'], '<' if before[x] < before[y] else '>')
                    self.assertEqual(row['now'], '<' if now[x] < now[y] else '>')
                    self.assertEqual(row['measured'], 'on both machines')

    def test_the_oriya_digits_are_named(self):
        """or_IN, RHEL8 -> RHEL9: each Oriya digit was a neighbour of its
        ASCII digit; on RHEL9 seven zeros of other scripts stand between
        them, so no pair of neighbours holds both, and they are told apart by
        less than case. "0A" against U+0B66 "a" turns round: strcoll on the
        three test machines gave < on glibc 2.28 and > on 2.34 and 2.39.
        None of the ten moved."""
        orya = by_name(self.reports['8-9'])['or_IN']
        self.assertEqual(orya['compared'], ['letters', 'case'])
        self.assertIn((0xB66, 0xB6F), orya['still'])
        rows = self.rows('8-9', 'or_IN')
        self.assertFalse(set(range(0xB66, 0xB70)) & characters_of(rows, 'moved'))
        for k in range(10):
            with self.subTest(digit=k):
                row = [r for r in rows if r['finding'] == 'told apart differently'
                       and r['chars'] == [0x30 + k, 0xB66 + k]]
                self.assertEqual(len(row), 1)
                self.assertEqual(
                    (row[0]['how'], row[0]['test'], row[0]['before'],
                     row[0]['now'], row[0]['measured']),
                    (('like an accent or by case', 'by less than case'),
                     ([0x30 + k, 0x41], [0xB66 + k, 0x61]), '<', '>',
                     'on the old machine; the new one by the characters between'))

    def test_swedish_V_and_w_with_their_test_pair(self):
        """"Vb" against "wa", measured on both machines."""
        for name in ('sv_SE', 'sv_SE.iso885915'):
            with self.subTest(locale=name):
                rows = [r for r in self.rows('8-9', name)
                        if r['finding'] not in ('sorts differently',
                                                'also in PostgreSQL')]
                self.assertEqual(rows, [{
                    'finding': 'told apart differently', 'chars': [0x56, 0x77],
                    'how': ('like an accent', 'as different letters'),
                    'test': ([0x56, 0x62], [0x77, 0x61]), 'before': '>',
                    'now': '<', 'measured': 'on both machines'}])

    def test_a_pair_the_measurement_cannot_decide_is_counted_not_dropped(self):
        """sv_SE.utf8: W and the fullwidth W were neighbours on RHEL9 only;
        what RHEL8 measured between them says they differ more strongly,
        which a character glibc ignores at a level can fake. And four pairs
        whose relation worked out between them is the one measured, a level
        included, which can fake it as well. Not a change, not nothing:
        counted in the report and the summary, named in the file."""
        change = by_name(self.reports['8-9'])['sv_SE.utf8']
        self.assertEqual((change['pairs_count'], change['undecided']), (None, 7))
        rows = self.rows('8-9', 'sv_SE.utf8')
        self.assertEqual(sorted(r['chars'] for r in rows
                                if r['finding'] == 'not decided'),
                         [[0x57, 0xFF37], [0x77, 0x57], [0x77, 0xFF57],
                          [0x1DBA, 0xFF57], [0x1E7E, 0x77], [0x1E7E, 0x2175],
                          [0x24E6, 0xFF37]])
        brief = parse_brief(self.summaries['8-9']['changes']['sv_SE.utf8']['brief'])
        self.assertEqual(brief['undecided'], 7)

    def test_pairs_are_compared_at_the_levels_whose_pair_is_the_same(self):
        """The probe pairs changed in or_IN and th_TH. The levels measured
        with the same pair on both machines still compare: 39,930 pairs of
        neighbours in or_IN and 27 in th_TH in TIS-620 that no line named
        before, each now among the pairs counted."""
        for key, name, compared, count, both in (
                ('8-9', 'or_IN', ['letters', 'case'], 46136, 39930),
                ('9-10', 'th_TH', ['letters'], 31, 27),
                ('9-10', 'th_TH.utf8', ['letters'], 45693, 39924)):
            with self.subTest(locale=name):
                change = by_name(self.reports[key])[name]
                self.assertEqual((change['compared'], change['pairs_count']),
                                 (compared, count))
                pairs = sum(len(r['chars']) - 1 for r in self.rows(key, name)
                            if r['finding'] == 'told apart differently'
                            and r['measured'] == 'on both machines')
                self.assertEqual(pairs, both)

    def test_the_levels_left_out_are_said(self):
        """A level no pair measured (C.utf8 and ko_KR.utf8 have pairs for
        letters only) or whose pair differs is not compared, and the report,
        the summary and the file say which: a list that compared less than
        every level must not read as the whole of it."""
        changes = by_name(self.reports['8-9'])
        for name in ('C.utf8', 'ko_KR.utf8'):
            with self.subTest(locale=name):
                self.assertEqual((changes[name]['compared'],
                                  changes[name]['not_measured']),
                                 (['letters'], ['accents', 'case']))
        self.assertEqual((changes['or_IN']['compared'],
                          changes['or_IN']['not_measured']),
                         (['letters', 'case'], None))
        for key, name, left_out in (('8-9', 'C.utf8', ['accents', 'case']),
                                    ('8-9', 'ko_KR.utf8', ['accents', 'case']),
                                    ('8-9', 'or_IN', ['accents']),
                                    ('9-10', 'th_TH', ['accents', 'case']),
                                    ('9-10', 'th_TH.utf8', ['accents', 'case']),
                                    ('8-9', 'sv_SE', None)):
            with self.subTest(locale=name):
                rows = [r['levels'] for r in self.rows(key, name)
                        if r['finding'] == 'not compared']
                self.assertEqual(rows, [left_out] if left_out else [])

    def test_a_pair_measured_on_the_new_machine_reads_old_to_new(self):
        """th_TH in TIS-620: the Thai baht sign and 0 are neighbours on RHEL10
        only, told apart as different letters there and by less than a
        letter between them on RHEL9: before is the old machine's."""
        rows = [r for r in self.rows('9-10', 'th_TH')
                if r['finding'] == 'told apart differently'
                and r['chars'] == [0x0E3F, 0x30]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]['how'], rows[0]['test'], rows[0]['before'], rows[0]['now'],
             rows[0]['measured']),
            (('by less than a letter', 'as different letters'),
             ([0x0E3F, 0x62], [0x30, 0x61]), '>', '<',
             'on the new machine; the old one by the characters between'))

    def test_each_kind_of_finding_is_counted_alike_in_the_three(self):
        """For every change of both pairs, each kind of finding: the count
        or the presence the report gives, the summary's phrase gives, and
        the file's rows give are the same. And for the run: the locales not
        measured, missing on the new machine and unchanged only as far as
        measured, the warnings and the closing paragraph. The three are
        drawn from one list of findings; this is what that buys."""
        for key in self.files:
            report, summary, files = (self.reports[key], self.summaries[key],
                                      self.files[key])
            changes = by_name(report)
            groups = changes_in(files)
            self.assertTrue(groups)
            for names, rows in groups.items():
                change = changes[names[0]]
                brief = parse_brief(summary['changes'][names[0]]['brief'])
                said = (kinds_in_report(change), kinds_in_brief(brief),
                        kinds_in_rows(rows))
                for kind in LOCALE_KINDS:
                    with self.subTest(pair=key, locales=names, kind=kind):
                        self.assertEqual(said[0][kind], said[1][kind])
                        self.assertEqual(said[0][kind], said[2][kind])
                with self.subTest(pair=key, locales=names, kind='named'):
                    assert_named_alike(self, change, brief, rows)
                with self.subTest(pair=key, locales=names, kind='exact'):
                    probes = [r['probes'] for r in rows
                              if r['finding'] == 'pairs changed']
                    self.assertEqual(probes, [change['probes']]
                                     if change['probes'] else [])
                    left = [r['levels'] for r in rows
                            if r['finding'] == 'not compared']
                    if change['probes'] is None:
                        self.assertEqual(left, [change['not_measured']]
                                         if change['not_measured'] else [])
                    else:
                        self.assertEqual(len(left), 1)
                        self.assertFalse(set(left[0]) & set(change['compared']))
                    self.assertEqual(brief['compared'], change['compared'])
            with self.subTest(pair=key, kind='also'):
                # The names PostgreSQL gives each change, merged over the
                # file's groups the report prints under one heading, and each
                # locale not measured, as the report gives them.
                for change in report['changes']:
                    self.assertEqual(also_of(
                        r['also'] for names, rows in files.items()
                        if set(names) <= set(change['names']) for r in rows
                        if r['finding'] == 'also in PostgreSQL'),
                        also_of([change['also']]))
                for name, entry in report['unmeasured'].items():
                    self.assertEqual(also_of(
                        r['also'] for r in files[(name,)]
                        if r['finding'] == 'also in PostgreSQL'),
                        also_of([entry['also']]))
            with self.subTest(pair=key, kind='run'):
                by_kind = {}
                for names, rows in files.items():
                    for row in rows:
                        if row['finding'] in NOT_CHANGED:
                            self.assertEqual(len(names), 1)
                            by_kind.setdefault(row['finding'], {})[names[0]] = row
                self.assertEqual(set(by_kind.get('not measured', ())),
                                 set(report['unmeasured']))
                self.assertEqual(sorted(by_kind.get('not measured', ())),
                                 summary['unmeasured'])
                self.assertEqual(sorted(by_kind.get('not on the new machine', ())),
                                 report['not_on_new'])
                self.assertEqual(summary['not_on_new'], report['not_on_new'])
                partly = {name: row['count'] for name, row in
                          by_kind.get('unchanged only as far as measured',
                                      {}).items()}
                self.assertEqual(partly, report['partly'])
                self.assertEqual(sorted(partly), summary['partly'])
                self.assertTrue(partly)
                self.assertEqual(files.warnings, report['warnings'])
                self.assertEqual(summary['warnings'], report['warnings'])
                self.assertEqual(files.closing, report['closing'])
                self.assertEqual(summary['closing'], report['closing'])

    def test_too_many_characters_that_did_not_move_are_counted(self):
        """C.utf8: too many runs to print, so the count, never nothing."""
        change = by_name(self.reports['8-9'])['C.utf8']
        self.assertEqual((change['still'], change['still_count']), (None, 845406))

    def test_no_new_character_where_nothing_else_changed(self):
        """ko_KR.utf8 and the Swedish locales in UTF-8 gain no pair: what
        the rule adds is the Oriya case, not noise."""
        for name in ('ko_KR.utf8', 'sv_SE.utf8'):
            with self.subTest(locale=name):
                change = by_name(self.reports['8-9'])[name]
                self.assertEqual((change['pairs_count'], change['still']),
                                 (None, None))


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
        for path in (RHEL8, RHEL9, RHEL10):
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
                     ('--summary-to', os.path.join(self.tmp, 'lone.txt')),
                     ('--characters-to', os.path.join(self.tmp, 'lone.tsv'))):
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

    def test_a_characters_file_that_cannot_be_written_stops_the_run(self):
        """Before the report and before the summary: a run whose whole list
        is not on disk must not print a report that points at it."""
        summary = os.path.join(self.tmp, 'beside-no-list.txt')
        path = os.path.join(self.tmp, 'no-such-directory', 'characters.tsv')
        rc, out, err = run_compare(RHEL9, RHEL10, '--summary-to', summary,
                                   '--characters-to', path)
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, '')
        self.assertRegex(err, r'^locale_order\.py: cannot write .*characters\.tsv')
        self.assertFalse(os.path.exists(summary))

    def test_a_summary_that_cannot_be_written_leaves_no_list(self):
        """The list is written first, under another name, and takes its own
        only once the summary is written: a run that died leaves neither."""
        chars = os.path.join(self.tmp, 'list-of-a-dead-run.tsv')
        rc, out, err = run_compare(
            RHEL9, RHEL10, '--characters-to', chars, '--summary-to',
            os.path.join(self.tmp, 'no-such-directory', 'summary.txt'))
        self.assertEqual((rc, out), (2, ''))
        self.assertFalse(os.path.exists(chars))
        self.assertFalse(os.path.exists(chars + '.partial'))

    def test_a_list_cut_short_on_disk_is_not_left_behind(self):
        """A file size limit stops the write half way: the run is refused,
        and no list that reads as if fewer locales changed is left."""
        import resource
        chars = os.path.join(self.tmp, 'cut-list.tsv')

        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (100000, 100000))
        p = subprocess.run([sys.executable, SCRIPT, '--compare', RHEL8, RHEL9,
                            '--characters-to', chars],
                           capture_output=True, preexec_fn=limit,
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(p.stdout, b'')
        self.assertFalse(os.path.exists(chars))
        self.assertFalse(os.path.exists(chars + '.partial'))

    def test_no_characters_file_is_written_for_a_refused_pair(self):
        """And an earlier run's list under that name is gone: it would read
        as this run's answer."""
        path = os.path.join(self.tmp, 'reversed.tsv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('an earlier run\n')
        rc, out, err = run_compare(RHEL9, RHEL8, '--characters-to', path)
        self.assertEqual((rc, out), (2, ''))
        self.assertFalse(os.path.exists(path))

    def test_without_the_file_the_report_says_how_to_get_it(self):
        """A report that cuts a list at twenty and names no file reads as the
        whole list; with nowhere to write it, it says which option writes it."""
        rc, out, err = run_compare(RHEL9, RHEL10)
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(flat(out).count(NO_FILE_LINE), 1)
        self.assertIsNone(parse_report(out)['characters_file'])

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
            # The same record, sealed with the other probes as measured()
            # seals it: the comparison opens it now, to compare the levels
            # whose pair is the same.
            e = d['locales']['de_DE.utf8']
            blob = d['pair_data'][e['pairs']]
            e['probes'] = e['probes'].replace('>', '<', 1)
            e['pairs'] = hashlib.sha256(
                e['probes'].encode('utf-8') + b'\n'
                + zlib.decompress(base64.b64decode(blob))).hexdigest()
            d['pair_data'][e['pairs']] = blob
        path = os.path.join(self.tmp, 'probes-only.tsv')
        rc, out, err = run_compare(RHEL9, self.variant(edit, reseal=True),
                                   '--characters-to', path)
        self.assertEqual((rc, err), (0, ''))
        de = by_name(parse_report(out))
        self.assertEqual(set(de), {'de_DE.utf8'})
        self.assertEqual(de['de_DE.utf8']['moved'], [])
        self.assertIsNotNone(de['de_DE.utf8']['probes'])
        self.assertEqual(de['de_DE.utf8']['compared'], ['accents', 'case'])
        # Nothing else changed, so the change's only rows say which pairs
        # changed, as the report does, and what was not compared: a change
        # with no row would read as nothing found.
        with open(path, encoding='utf-8') as f:
            rows = parse_characters(f.read())
        self.assertEqual(changes_in(rows), {('de_DE.utf8',): [
            {'finding': 'pairs changed', 'how': None, 'test': None,
             'before': None, 'now': None, 'measured': None,
             'probes': de['de_DE.utf8']['probes']},
            {'finding': 'not compared', 'how': None, 'test': None,
             'before': None, 'now': None, 'measured': None,
             'levels': ['letters']},
            {'finding': 'also in PostgreSQL', 'how': None, 'test': None,
             'before': None, 'now': None, 'measured': None,
             'also': de['de_DE.utf8']['also']}]})

    def test_a_locale_that_could_not_be_measured_on_one_machine(self):
        def edit(d):
            d['locales']['ko_KR.utf8'] = {'error': 'cannot be selected: test',
                                          'codeset': 'UTF-8'}
        path = os.path.join(self.tmp, 'one-machine.tsv')
        rc, out, err = run_compare(RHEL9, self.variant(edit, reseal=True),
                                   '--characters-to', path)
        self.assertEqual((rc, err), (0, ''))
        r = parse_report(out)
        ko = r['unmeasured']['ko_KR.utf8']
        self.assertEqual(ko['why'], {'new machine': 'cannot be selected: test'})
        self.assertEqual(ko['also']['aliases'], {'UTF-8': ['ko_KR']})
        self.assertEqual(by_name(r), {})
        # The file says which machine, like the report.
        with open(path, encoding='utf-8') as f:
            rows = parse_characters(f.read())
        self.assertEqual(rows[('ko_KR.utf8',)][0]['why'],
                         'new machine: cannot be selected: test')

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
        changed = []

        def edit(d):
            e = d['locales']['de_DE.utf8']
            raw = bytearray(zlib.decompress(base64.b64decode(
                d['pair_data'][e['pairs']])))
            # Thirty neighbours told apart as different letters that are now
            # told apart more weakly: a record a machine can write.
            for i in range(1000, len(raw)):
                if len(changed) == 30:
                    break
                if raw[i] & 7 == 0:
                    raw[i] ^= 4
                    changed.append(i)
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
        self.assertEqual(brief, {'moved': [], 'moved_count': 0, 'reversed': None,
                                 'pairs': None, 'pairs_count': len(changed),
                                 'probes': False, 'still': None,
                                 'still_count': None, 'compared': None,
                                 'undecided': 0})

    def test_a_locale_name_with_a_comma_and_a_tab(self):
        """The file is tab-separated and lists the names of a change with
        commas: such a name is escaped, never written raw to split a row."""
        odd = 'th,TH' + chr(9) + 'x'
        paths = []
        for base in (RHEL9, RHEL10):
            with open(base, encoding='utf-8') as f:
                data = json.loads(f.read())
            data['locales'][odd] = data['locales']['th_TH']
            # And a reason, free text in one cell, holding a tab too.
            data['locales']['ka_GE'] = {'error': 'cannot be selected: a'
                                        + chr(9) + 'b', 'codeset': 'GEORGIAN-PS'}
            data['locales_listed'] += 1
            data['content'] = m.content_digest(data)
            path = os.path.join(self.tmp, f'odd-name-{len(paths)}.out')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps(data, sort_keys=True) + '\n')
            paths.append(path)
        chars = os.path.join(self.tmp, 'odd-name.tsv')
        rc, out, err = run_compare(*paths, '--characters-to', chars)
        self.assertEqual((rc, err), (0, ''))
        with open(chars, encoding='utf-8') as f:
            text = f.read()
        rows = parse_characters(text)
        label = 'th\\x2cTH\\tx'
        self.assertIn((label, 'th_TH', 'th_TH.tis620', 'thai'), rows)
        self.assertNotIn(odd, text)
        self.assertEqual(rows[('ka_GE',)][0]['why'],
                         'both machines: cannot be selected: a\\tb')

    def test_measured_over_different_characters(self):
        def edit(d):
            d['locales']['ko_KR']['corpus'] = '0' * 64
        path = os.path.join(self.tmp, 'different-characters.tsv')
        rc, out, err = run_compare(RHEL9, self.variant(edit, reseal=True),
                                   '--characters-to', path)
        self.assertEqual((rc, err), (0, ''))
        r = parse_report(out)
        with open(path, encoding='utf-8') as f:
            rows = parse_characters(f.read())
        self.assertEqual(rows[('ko_KR',)][0]['why'],
                         'measured over different characters on the two machines')
        self.assertEqual(r['unmeasured']['ko_KR']['why'],
                         {'both machines': 'different characters'})
        self.assertEqual(by_name(r), {})

    def test_each_kind_of_the_run_is_said_alike_in_the_three(self):
        """One edited new machine with each kind of the run: three warnings
        (both machines run one build, and each tag names no release), a
        locale measured only in part, one that could not be measured there,
        one missing there, and the closing paragraph. The report, the summary
        and the file say each alike, every warning included. And the file
        gives the missing one the names PostgreSQL gave it on the old
        machine, where the cluster created its collations."""
        def edit(d):
            d['locales']['en_US']['partly'] = 5
            d['locales']['ko_KR.utf8'] = {'error': 'cannot be selected: test',
                                          'codeset': 'UTF-8'}
            del d['locales']['de_DE.utf8']
            d['locales_listed'] -= 1
        new = self.variant(edit, reseal=True)
        summary_path = os.path.join(self.tmp, 'run-kinds.txt')
        file_path = os.path.join(self.tmp, 'run-kinds.tsv')
        # Both tags commit ids: two warnings more, one per side.
        rc, out, err = run_compare(RHEL9, new, '--summary-to', summary_path,
                                   '--characters-to', file_path,
                                   '--tags', '3c03baca37fd', '1a2b3c4d5e6f')
        self.assertEqual((rc, err), (0, ''))
        report = parse_report(out)
        with open(summary_path, encoding='utf-8') as f:
            summary = parse_summary(f.read())
        with open(file_path, encoding='utf-8') as f:
            files = parse_characters(f.read())
        rows = {names[0]: rows[0] for names, rows in files.items()}
        self.assertEqual(changes_in(files), {})
        warned = ['same build', 'OLD tag unchecked', 'NEW tag unchecked']
        self.assertEqual(report['warnings'], warned)
        self.assertEqual(summary['warnings'], warned)
        self.assertEqual(files.warnings, warned)
        self.assertEqual(report['partly'].get('en_US'), 5)
        self.assertIn('en_US', summary['partly'])
        self.assertEqual((rows['en_US']['finding'], rows['en_US']['count']),
                         ('unchanged only as far as measured', 5))
        self.assertEqual(report['unmeasured']['ko_KR.utf8']['why'],
                         {'new machine': 'cannot be selected: test'})
        self.assertEqual(set(report['unmeasured']), UNMEASURABLE | {'ko_KR.utf8'})
        self.assertEqual(summary['unmeasured'], sorted(report['unmeasured']))
        self.assertEqual({n for n, r in rows.items()
                          if r['finding'] == 'not measured'},
                         set(report['unmeasured']))
        self.assertEqual(rows['ko_KR.utf8']['why'],
                         'new machine: cannot be selected: test')
        self.assertEqual(report['not_on_new'], ['de_DE.utf8'])
        self.assertEqual(summary['not_on_new'], ['de_DE.utf8'])
        self.assertEqual(
            [(r['finding'], r.get('also')) for r in files[('de_DE.utf8',)]],
            [('not on the new machine', None),
             ('also in PostgreSQL', {'aliases': {'UTF-8': ['de_DE']},
                                     'spellings': ['de_DE.UTF-8']})])
        self.assertEqual(summary['closing'], report['closing'])
        self.assertEqual(files.closing, report['closing'])

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

    # Told-apart records as measured() writes them, every level compared:
    # bit 0 a tie, then per level 0 when x+hi sorts first (they differ at
    # least that strongly) and 2 when it does not.
    REC = {'letters': 0, 'accent': 2 << 1, 'case': (2 << 1) | (2 << 3),
           'less': (2 << 1) | (2 << 3) | (2 << 5),
           'tie': 1 | (2 << 1) | (2 << 3) | (2 << 5)}
    ALL = (0, 1, 2)
    # The letters pair compared equal and the rest weaker: worked out between
    # two characters it would read as a weaker relation, a change, if the
    # equal answer were not taken as saying nothing.
    UNSURE = (1 << 1) | (2 << 3) | (2 << 5)

    def records(self, seq, special):
        """Told-apart records of seq: letters, but special {(x, y): kind}."""
        told = bytearray(len(seq) - 1)
        for i in range(len(seq) - 1):
            told[i] = self.REC[special.get((seq[i], seq[i + 1]), 'letters')]
        return bytes(told)

    def rel(self, kind, levels=ALL):
        return m.relation_tables(levels)[0][self.REC[kind]]

    def inserted(self, c, before):
        """BASE with c moved to just before the character before."""
        seq = [x for x in self.BASE if x != c]
        seq.insert(seq.index(before), c)
        return seq

    def test_a_weaker_level_worked_out_between_is_a_change(self):
        """The Oriya case: 0 and its neighbour were told apart like an
        accent; on the new machine a character stands between them, told
        apart from each by less than case. The pair is named, measured on the
        old machine. The pairs around the moved character, letters apart both
        where measured and where worked out, cannot be decided: a letter
        worked out between them may not be one."""
        base = self.BASE
        new = self.inserted(0x70, 0x31)
        old_told = self.records(base, {(0x30, 0x31): 'accent'})
        new_told = self.records(new, {(0x30, 0x70): 'less', (0x70, 0x31): 'less'})
        i = base.index(0x30)
        self.assertEqual(
            m.level_changes(base, old_told, new, new_told, self.ALL,
                            m.reordering(base, new)),
            ([['old', i, i, self.rel('accent'), self.rel('less')]],
             [(0x70, 0x71), (0x30, 0x70), (0x6F, 0x71)]))

    def test_a_weaker_level_worked_out_on_the_old_machine_is_a_change(self):
        """The mirror of the Oriya case: neighbours on the new machine only,
        told apart like an accent there, and by less than case through the
        character that stood between them on the old one. Before is the old
        machine's, worked out; now is the new one's, measured."""
        base = self.BASE
        new = self.inserted(0x31, 0x70)
        old_told = self.records(base, {(0x30, 0x31): 'less', (0x31, 0x32): 'less'})
        new_told = self.records(new, {(0x30, 0x32): 'accent'})
        j = new.index(0x30)
        changes = m.level_changes(base, old_told, new, new_told, self.ALL,
                                  m.reordering(base, new))[0]
        self.assertEqual(changes, [['new', j, j, self.rel('less'),
                                    self.rel('accent')]])

    def test_an_unsure_record_on_either_side_is_undecided(self):
        """Where the pair is measured, or between them where it is worked
        out, a record that compared equal says nothing about the level."""
        base = self.BASE
        new = self.inserted(0x70, 0x31)
        kinds = {(0x30, 0x70): 'less', (0x70, 0x31): 'less'}
        for where in ('measured', 'between'):
            with self.subTest(where=where):
                old_told = bytearray(self.records(base, {(0x30, 0x31): 'accent'}))
                new_told = bytearray(self.records(new, kinds))
                if where == 'measured':
                    old_told[base.index(0x30)] = 1 << 1
                else:
                    new_told[new.index(0x70)] = self.UNSURE
                changes, undecided = m.level_changes(
                    base, bytes(old_told), new, bytes(new_told), self.ALL,
                    m.reordering(base, new))
                self.assertEqual(changes, [])
                self.assertIn((0x30, 0x31), undecided)

    def test_an_unsure_record_on_the_new_machine_only_route(self):
        """Neighbours on the new machine only: an unsure record of their own,
        or among the old machine's characters between them, is undecided."""
        base = self.BASE
        new = self.inserted(0x31, 0x70)
        for where in ('measured', 'between'):
            with self.subTest(where=where):
                old_told = bytearray(self.records(
                    base, {(0x30, 0x31): 'less', (0x31, 0x32): 'less'}))
                new_told = bytearray(self.records(new, {(0x30, 0x32): 'less'}))
                if where == 'measured':
                    new_told[new.index(0x30)] = self.UNSURE
                else:
                    old_told[base.index(0x30)] = self.UNSURE
                changes, undecided = m.level_changes(
                    base, bytes(old_told), new, bytes(new_told), self.ALL,
                    m.reordering(base, new))
                self.assertIn((0x30, 0x32), undecided)
                self.assertNotIn('new', [side for side, *_ in changes])

    def test_neighbours_in_both_unsure_on_one_machine_only(self):
        """The same relation from different bytes, one of them unsure: on the
        old machine with the same order, and on the new one after a
        character moved elsewhere."""
        base = self.BASE
        old_told = bytearray(self.records(base, {}))
        new_told = bytearray(old_told)
        i = base.index(0x61)
        old_told[i] = 1 << 1                    # letters '=', the rest '<'
        new_told[i] = 2 << 1                    # letters '>', the rest '<'
        rel = m.relation_tables(self.ALL)[0]
        self.assertEqual(rel[old_told[i]], rel[new_told[i]])
        self.assertEqual(m.level_changes(base, bytes(old_told), base,
                                         bytes(new_told), self.ALL),
                         ([], [(0x61, 0x62)]))
        new = self.inserted(0x70, 0x31)
        old_told = bytearray(self.records(base, {(0x61, 0x62): 'case'}))
        new_told = bytearray(self.records(new, {(0x61, 0x62): 'case'}))
        new_told[new.index(0x61)] = (2 << 1) | (1 << 3)   # accent '='
        self.assertEqual(rel[old_told[i]], rel[new_told[new.index(0x61)]])
        self.assertIn((0x61, 0x62), m.level_changes(
            base, bytes(old_told), new, bytes(new_told), self.ALL,
            m.reordering(base, new))[1])

    def test_a_long_way_between_is_read_the_same(self):
        """More than 64 characters between: the counts path, not the short
        loop, works the relation out; weaker is a change, and an unsure
        record anywhere between makes it undecided."""
        base = self.BASE
        block = list(range(0x2F00, 0x2F50))
        new = [c for c in base if c not in block]
        at = new.index(0x31)
        new[at:at] = block
        between = {(0x30, block[0]): 'less', (block[-1], 0x31): 'less'}
        between.update({(x, y): 'less' for x, y in zip(block, block[1:])})
        old_told = self.records(base, {(0x30, 0x31): 'accent'})
        new_told = bytearray(self.records(new, between))
        i = base.index(0x30)
        moves = m.reordering(base, new)
        self.assertEqual(
            m.level_changes(base, old_told, new, bytes(new_told), self.ALL,
                            moves)[0][0],
            ['old', i, i, self.rel('accent'), self.rel('less')])
        # Inside the way, and at each of its two ends: the first record and
        # the last count as much as the middle one.
        for where in (block[40], 0x30, block[-1]):
            with self.subTest(unsure_after=f'U+{where:04X}'):
                told = bytearray(new_told)
                told[new.index(where)] = self.UNSURE
                changes, undecided = m.level_changes(base, old_told, new,
                                                     bytes(told), self.ALL, moves)
                self.assertNotIn(i, [first for side, first, *_ in changes])
                self.assertIn((0x30, 0x31), undecided)

    def test_the_levels_compared_and_left_out(self):
        """'none' is a level shown absent on both machines: nothing to
        compare, and nothing left out. '?' is a level no pair measured."""
        self.assertEqual(m.comparable_levels('61.62> none 61.41>',
                                             '61.62> none 61.41>'), (0, 2))
        self.assertEqual(m.uncompared_levels('61.62> none 61.41>',
                                             '61.62> none 61.41>'), ())
        self.assertEqual(m.comparable_levels('61.62> ? ?', '61.62> ? ?'), (0,))
        self.assertEqual(m.uncompared_levels('61.62> ? ?', '61.62> ? ?'), (1, 2))
        self.assertEqual(m.uncompared_levels('61.62> 30.B66> 61.41>',
                                             '61.62> 61.E1> 61.41>'), (1,))

    def test_a_reordered_locale_with_the_same_records_is_compared(self):
        """The records of the two machines can be the same bytes while the
        order differs: the pairs they belong to are not the same pairs, and
        are compared."""
        probes = '61.62> 61.E1> 61.41>'
        base = self.BASE
        new = self.inserted(0x70, 0x31)
        told = self.records(base, {(0x30, 0x31): 'accent'})

        def measurement(seq):
            text = m.order_text(seq).encode('utf-8')
            order, pairs = (hashlib.sha256(text).hexdigest(),
                            hashlib.sha256(probes.encode() + b'\n' + told).hexdigest())
            return ({'path': 'x', 'order_data': {order: m.pack_order(seq)},
                     'pair_data': {pairs: m.pack(told)}},
                    {'order': order, 'pairs': pairs, 'chars': len(seq),
                     'probes': probes})
        (old, o), (nw, n) = measurement(base), measurement(new)
        self.assertEqual(o['pairs'], n['pairs'])
        rows = m.describe_change(old, nw, o, n)[2]
        self.assertIn(('not decided', 'U+0030 U+0031', '-', '-', '-', '-', '-'),
                      rows)

    def test_a_run_keeps_where_it_was_measured(self):
        """Two pairs in a row change alike, one measured on both machines and
        one worked out on the new: two entries, or the file would call the
        second measured on both."""
        base = self.BASE
        new = self.inserted(0x70, 0x32)
        old_told = self.records(base, {(0x30, 0x31): 'accent',
                                       (0x31, 0x32): 'accent'})
        new_told = self.records(new, {(0x30, 0x31): 'less', (0x31, 0x70): 'less',
                                      (0x70, 0x32): 'less'})
        i = base.index(0x30)
        changes, undecided = m.level_changes(base, old_told, new, new_told,
                                             self.ALL, m.reordering(base, new))
        self.assertEqual(changes, [
            ['both', i, i, self.rel('accent'), self.rel('less')],
            ['old', i + 1, i + 1, self.rel('accent'), self.rel('less')]])

    def test_a_stronger_level_worked_out_between_is_not_a_change(self):
        """The direction a character glibc ignores at a level can fake: a
        letter between two neighbours told apart by less than case does not
        make them letters apart. Counted as undecided, not named."""
        base = self.BASE
        new = self.inserted(0x70, 0x41)
        old_told = self.records(base, {(0x40, 0x41): 'less'})
        new_told = self.records(new, {})
        changes, undecided = m.level_changes(base, old_told, new, new_told,
                                             self.ALL, m.reordering(base, new))
        self.assertEqual(changes, [])
        self.assertEqual(undecided, [(0x40, 0x41), (0x70, 0x71), (0x40, 0x70),
                                     (0x6F, 0x71)])

    def test_neighbours_in_both_orders_compare_record_to_record(self):
        """A run of pairs that change alike is one entry."""
        base = self.BASE
        old_told = self.records(base, {(0x56, 0x57): 'accent',
                                       (0x57, 0x58): 'accent'})
        new_told = self.records(base, {})
        i = base.index(0x56)
        self.assertEqual(m.level_changes(base, old_told, base, new_told, self.ALL),
                         ([['both', i, i + 1, self.rel('accent'),
                            self.rel('letters')]], []))

    def test_a_record_that_compared_equal_is_undecided(self):
        """strcoll called the probe strings equal: the record says nothing
        about the level, on either side."""
        base = self.BASE
        old_told = self.records(base, {})
        new_told = bytearray(self.records(base, {}))
        new_told[base.index(0x61)] = 1 << 1
        self.assertEqual(m.level_changes(base, old_told, base, bytes(new_told),
                                         self.ALL),
                         ([], [(0x61, 0x62)]))

    def test_records_unsure_on_both_machines_that_differ_are_undecided(self):
        """Both records hold an answer that says nothing, so their relations
        read the same, and the bytes still differ: undecided, not dropped."""
        base = self.BASE
        old_told = bytearray(self.records(base, {}))
        new_told = bytearray(old_told)
        i = base.index(0x61)
        old_told[i] = (1 << 1) | (2 << 3) | (2 << 5)
        new_told[i] = (1 << 1) | (2 << 3) | (3 << 5)
        rel, unsure = m.relation_tables(self.ALL)
        self.assertEqual((rel[old_told[i]], unsure[old_told[i]]),
                         (rel[new_told[i]], unsure[new_told[i]]))
        self.assertEqual(m.level_changes(base, bytes(old_told), base,
                                         bytes(new_told), self.ALL),
                         ([], [(0x61, 0x62)]))

    def test_a_record_with_no_pair_at_a_compared_level_is_undecided(self):
        """3 is "this level had no pair", which a level compared on both
        machines had: a record that says so changed in a way the comparison
        cannot read, and must count rather than read as unchanged."""
        base = self.BASE
        old_told = self.records(base, {(0x61, 0x62): 'tie'})
        new_told = bytearray(old_told)
        new_told[base.index(0x61)] ^= 2         # the letters field, 2 -> 3
        self.assertEqual(m.level_changes(base, old_told, base, bytes(new_told),
                                         self.ALL),
                         ([], [(0x61, 0x62)]))

    def test_only_the_levels_compared_count(self):
        """With the accent pair different on the two machines, an accent
        record that changed is not a change; the case level still is."""
        base = self.BASE
        old_told = self.records(base, {(0x61, 0x62): 'accent'})
        new_told = self.records(base, {(0x61, 0x62): 'case'})
        self.assertEqual(m.level_changes(base, old_told, base, new_told, (0, 2)),
                         ([], []))
        new_told = self.records(base, {(0x61, 0x62): 'less'})
        self.assertEqual(
            m.level_changes(base, old_told, base, new_told, (0, 2))[0],
            [['both', base.index(0x61), base.index(0x61),
              self.rel('accent', (0, 2)), self.rel('less', (0, 2))]])

    def test_the_relation_in_words(self):
        self.assertEqual(m.relation_words(self.rel('accent'), self.ALL),
                         'like an accent')
        self.assertEqual(m.relation_words(self.rel('accent', (0, 2)), (0, 2)),
                         'like an accent or by case')
        self.assertEqual(m.relation_words(self.rel('less', (0, 2)), (0, 2)),
                         'by less than case')
        self.assertEqual(m.relation_words(self.rel('case', (0,)), (0,)),
                         'by less than a letter')
        self.assertEqual(m.relation_words(self.rel('tie'), self.ALL),
                         'not at all (the bytes decide)')
        for kind in self.REC:
            for levels in ((0,), (0, 2), self.ALL):
                with self.subTest(kind=kind, levels=levels):
                    self.assertIn(m.relation_words(self.rel(kind, levels), levels),
                                  TOLD_APART)

    def test_the_test_pair_of_a_level_change(self):
        """The Oriya digit zero: "0A" against U+0B66 "a", with the probes
        or_IN measured on both machines (letters a/b, case a/A)."""
        self.assertEqual(
            m.test_pair(0x30, 0xB66, self.rel('accent', (0, 2)),
                        self.rel('less', (0, 2)), '61.62> 30.B66> 61.41>', (0, 2)),
            ((0x30, 0x41), (0xB66, 0x61), '<', '>'))
        # From the end of the string where the level is read from the end.
        self.assertEqual(
            m.test_pair(0x56, 0x77, self.rel('accent'), self.rel('letters'),
                        '61.62< 61.E1> 61.41>', self.ALL),
            ((0x62, 0x56), (0x61, 0x77), '>', '<'))
        self.assertIsNone(m.test_pair(0x56, 0x77, self.rel('tie'),
                                      self.rel('less'), '61.62> 61.E1> 61.41>',
                                      self.ALL))

    def permutations(self):
        """200 small orders, each a few moves and reversals away from the
        first: runs of neighbours that stay together, as in a real upgrade,
        and characters that cross them."""
        rng = random.Random(4)
        for _ in range(200):
            n = rng.randint(1, 30)
            seq = list(range(100, 100 + n))
            new = seq[:]
            for _ in range(rng.randint(0, 4)):
                i, j = sorted(rng.sample(range(n), 2)) if n > 1 else (0, 0)
                new[i:j + 1] = (new[i:j + 1][::-1] if rng.random() < .3
                                else new[i + 1:j + 1] + new[i:i + 1])
            yield seq, new

    def test_the_characters_in_a_reversed_pair(self):
        """Against every pair, on 200 small orders."""
        for seq, new in self.permutations():
            at = {c: i for i, c in enumerate(new)}
            want = [int(any((at[x] < at[y]) != (i < j)
                            for j, y in enumerate(seq) if y != x))
                    for i, x in enumerate(seq)]
            self.assertEqual(list(m.in_a_reversed_pair(m.new_positions(seq, new))),
                             want, (seq, new))

    def test_the_kept_characters_are_a_longest_increasing_subsequence(self):
        """In order in both, and as many as any such set: the shortcuts that
        append a run whole and read it back a run at a time keep it so."""
        for seq, new in self.permutations():
            positions = m.new_positions(seq, new)
            kept = m.kept_in_order(positions)
            chosen = [p for p, k in zip(positions, kept) if k]
            self.assertEqual(chosen, sorted(chosen))
            longest = [1] * len(positions)
            for i in range(len(positions)):
                for j in range(i):
                    if positions[j] < positions[i]:
                        longest[i] = max(longest[i], longest[j] + 1)
            self.assertEqual(len(chosen), max(longest), (seq, new))

    def test_every_moved_character_has_a_witness_on_its_other_side(self):
        for seq, new in self.permutations():
            moves = m.reordering(seq, new)
            kept = m.kept_in_order(moves[0])
            found = m.witnesses(seq, new, moves, kept)
            self.assertEqual(set(found), set(m.moved_characters(seq, new)))
            at, keep = {c: i for i, c in enumerate(new)}, set(
                c for c, k in zip(seq, kept) if k)
            for c, (w, sign) in found.items():
                self.assertIn(w, keep)
                before = seq.index(c) < seq.index(w)
                self.assertEqual(sign, '<' if before else '>')
                self.assertNotEqual(before, at[c] < at[w])

    def test_an_order_as_text(self):
        """Built as UTF-32 for speed, it must say what joining chr() says,
        and refuse what chr() refuses: a damaged order is a refusal, never a
        traceback (unpacked catches these two)."""
        seq = [0x41, 0xD7A3, 0x10FFFF, 1]
        self.assertEqual(m.order_text(seq), ''.join(map(chr, seq)))
        with self.assertRaises(ValueError):
            m.order_text([0x110000])
        with self.assertRaises(OverflowError):
            m.order_text([-1])

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


class FindingKinds(unittest.TestCase):
    """The table each output is drawn from, and one made-up change per kind
    of finding, small enough that every kind appears in both of its forms:
    named, and counted as too many to name."""

    BASE = list(range(0x20, 0x3000))
    REC = Pieces.REC

    def test_every_kind_has_all_three_outputs(self):
        self.assertEqual(tuple(m.LOCALE_KINDS), LOCALE_KINDS)
        self.assertEqual(set(m.RUN_KINDS), {'warning', 'partly', 'not measured',
                                            'not on the new machine', 'control',
                                            'postgres libc above', 'money',
                                            'postgres libc', 'closing'})
        for table in (m.LOCALE_KINDS, m.RUN_KINDS):
            for kind, how in table.items():
                with self.subTest(kind=kind):
                    self.assertEqual(len(how), 3)
                    self.assertTrue(all(map(callable, how)))

    def test_a_kind_with_no_entry_raises(self):
        """Rather than be left out of an output: a new kind of finding
        with no way to be printed, and a kind one output never takes."""
        for part in (m.REPORT, m.SUMMARY, m.FILE):
            with self.assertRaises(LookupError):
                m.printer(m.LOCALE_KINDS, 'a new kind', part)
        with self.assertRaises(LookupError):
            m.Drawn([('a new kind', {})], m.REPORT)
        run = m.Drawn([('closing', {'text': m.CLOSING}),
                       ('not on the new machine', {'name': 'x'})], m.REPORT)
        run('closing')
        with self.assertRaises(LookupError):
            run.done()
        run('not on the new machine')
        run.done()

    def measurement(self, seq, told, probes):
        text = m.order_text(seq).encode('utf-8')
        order = hashlib.sha256(text).hexdigest()
        pairs = hashlib.sha256(probes.encode() + b'\n' + told).hexdigest()
        return ({'path': 'x', 'order_data': {order: m.pack_order(seq)},
                 'pair_data': {pairs: m.pack(told)}},
                {'order': order, 'pairs': pairs, 'chars': len(seq),
                 'probes': probes})

    def records(self, seq, special):
        told = bytearray(len(seq) - 1)
        for i in range(len(seq) - 1):
            told[i] = self.REC[special.get((seq[i], seq[i + 1]), 'letters')]
        return bytes(told)

    def changes(self):
        """{name: (old, new, o, n)}: the Oriya case (a character moved
        between 0 and 1, told apart like an accent before and by less than
        case through it now); the same with the accent pair changed; two
        machines that measured a pair for letters only; and the mirror of the
        Oriya case, a pair told apart differently measured on the new machine
        only (1 moved between 0 and 2 there)."""
        all3, other = '61.62> 61.E1> 61.41>', '61.62> 30.B66> 61.41>'
        new = [x for x in self.BASE if x != 0x70]
        new.insert(new.index(0x31), 0x70)
        old_told = self.records(self.BASE, {(0x30, 0x31): 'accent'})
        new_told = self.records(new, {(0x30, 0x70): 'less', (0x70, 0x31): 'less'})
        out = {}
        for name, before, now in (('oriya', all3, all3),
                                  ('probes', all3, other),
                                  ('letters only', '61.62> ? ?', '61.62> ? ?')):
            (old, o), (nw, n) = (self.measurement(self.BASE, old_told, before),
                                 self.measurement(new, new_told, now))
            out[name] = (old, nw, o, n)
        mirror = [x for x in self.BASE if x != 0x31]
        mirror.insert(mirror.index(0x70), 0x31)
        (old, o), (nw, n) = (
            self.measurement(self.BASE, self.records(
                self.BASE, {(0x30, 0x31): 'less', (0x31, 0x32): 'less'}), all3),
            self.measurement(mirror, self.records(
                mirror, {(0x30, 0x32): 'accent'}), all3))
        out['mirror'] = (old, nw, o, n)
        return out

    def test_each_kind_is_counted_alike_in_the_three(self):
        """Every kind of finding of a change, from describe_change itself:
        its count in the report's lines, in the summary's phrase and in the
        rows of the file is the same, named and counted as too many."""
        seen = {kind: set() for kind in LOCALE_KINDS}
        sides = set()
        for limit in (m.LIST_LIMIT, 0):
            with unittest.mock.patch.object(m, 'LIST_LIMIT', limit):
                for name, args in self.changes().items():
                    lines, brief, rows = m.describe_change(*args)
                    found = m.locale_findings(*args)
                    kinds = {kind for kind, d in found}
                    sides.update(side for kind, d in found if kind == 'told apart'
                                 for *_, side in d['chains'])
                    files = parse_characters(file_of(rows))
                    said = (kinds_in_report(parse_details(lines)),
                            kinds_in_brief(parse_brief(brief)),
                            kinds_in_rows(files.get(('x',), [])))
                    for kind in LOCALE_KINDS:
                        with self.subTest(change=name, limit=limit, kind=kind):
                            self.assertEqual(said[0][kind], said[1][kind])
                            self.assertEqual(said[0][kind], said[2][kind])
                            if kind in kinds:
                                seen[kind].add(limit)
                                self.assertTrue(said[0][kind])
                    with self.subTest(change=name, limit=limit, kind='named'):
                        assert_named_alike(self, parse_details(lines),
                                           parse_brief(brief),
                                           files.get(('x',), []))
        # Each kind came up, in both forms, and changes measured on each side.
        self.assertEqual(sides, {'old', 'new'})
        for kind in LOCALE_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(seen[kind], {m.LIST_LIMIT, 0})


def pair_by_pair(old_seq, old_told, new_seq, new_told, levels):
    """level_changes worked out the slow way, one pair of neighbours at a
    time, as (changes, undecided): changes a set of (side, x, y, before,
    now), one per pair, undecided a set of (x, y).

    A pair of neighbours in both orders is compared record to record; a pair
    of neighbours in one order that the other keeps in order is compared
    with the chain of neighbours between them there, and is a change only
    when that chain tells them apart more weakly; a pair the other order
    reverses is not a pair of this kind at all. No runs, no bit tricks, no
    shortcut for long chains: written from the rule, not from the code."""
    rel, unsure = m.relation_tables(levels)
    new_at = {c: i for i, c in enumerate(new_seq)}
    old_at = {c: i for i, c in enumerate(old_seq)}
    changes, undecided = set(), set()

    def chain(told, a, b):
        r, u = 0, False
        for v in told[a:b]:
            r |= rel[v]
            u |= bool(unsure[v])
        return r, u

    def weigh(x, y, mine, mine_unsure, theirs, theirs_unsure, side):
        if mine_unsure or theirs_unsure:
            undecided.add((x, y))
        elif m.strength(theirs) < m.strength(mine):
            changes.add((side, x, y) + ((mine, theirs) if side == 'old'
                                        else (theirs, mine)))
        elif theirs != mine or theirs & ~1:
            undecided.add((x, y))

    for i in range(len(old_seq) - 1):
        x, y = old_seq[i], old_seq[i + 1]
        a, b = new_at[x], new_at[y]
        if b == a + 1:
            before, now = old_told[i], new_told[a]
            if unsure[before] or unsure[now]:
                if before != now:
                    undecided.add((x, y))
            elif rel[before] != rel[now]:
                changes.add(('both', x, y, rel[before], rel[now]))
        elif b > a:
            weigh(x, y, rel[old_told[i]], unsure[old_told[i]],
                  *chain(new_told, a, b), 'old')
    for j in range(len(new_seq) - 1):
        x, y = new_seq[j], new_seq[j + 1]
        a, b = old_at[x], old_at[y]
        if b > a + 1:
            weigh(x, y, rel[new_told[j]], unsure[new_told[j]],
                  *chain(old_told, a, b), 'new')
    return changes, undecided


class LevelChangesPairByPair(unittest.TestCase):
    """level_changes against pair_by_pair on made-up orders and records,
    the same every run: reorderings by moves and reversed stretches, records
    with ties, levels absent and pairs that compared equal, and some orders
    long enough that a chain between two neighbours is longer than the 64
    characters Between adds up one by one."""

    def record(self, rng, levels):
        v = 1 if rng.random() < .1 else 0
        for k in range(3):
            v |= (rng.choice((0, 0, 2, 2, 1)) if k in levels
                  else rng.choice((0, 1, 2, 3))) << (1 + 2 * k)
        return v

    def case(self, rng, long):
        n = rng.randint(70, 200) if long else rng.randint(2, 30)
        seq = list(range(100, 100 + n))
        new = seq[:]
        for _ in range(rng.randint(1 if long else 0, 4)):
            i, j = sorted(rng.sample(range(n), 2)) if n > 1 else (0, 0)
            if long and rng.random() < .5:
                j = min(n - 1, i + rng.randint(66, 120))
            new[i:j + 1] = (new[i:j + 1][::-1] if rng.random() < .3
                            else new[i + 1:j + 1] + new[i:i + 1])
        levels = tuple(k for k in range(3) if rng.random() < .7)
        old_told = bytes(self.record(rng, levels) for _ in range(n - 1))
        new_told = bytearray(self.record(rng, levels) for _ in range(n - 1))
        if rng.random() < .5:
            # Mostly the same record for a pair of neighbours in both orders,
            # so that the unchanged ones are many too.
            at = {c: i for i, c in enumerate(seq)}
            for j in range(n - 1):
                a, b = at[new[j]], at[new[j + 1]]
                if b == a + 1 and rng.random() < .8:
                    new_told[j] = old_told[a]
        return seq, old_told, new, bytes(new_told), levels

    def test_the_same_answer_as_pair_by_pair(self):
        rng = random.Random(20261018)
        found = {'change': 0, 'undecided': 0, 'long': 0}
        for t in range(200):
            seq, old_told, new, new_told, levels = self.case(rng, t % 10 == 0)
            moves = None if seq == new else m.reordering(seq, new)
            runs, undecided = m.level_changes(seq, old_told, new, new_told,
                                              levels, moves)
            changes = set()
            for side, first, last, before, now in runs:
                order = new if side == 'new' else seq
                for i in range(first, last + 1):
                    changes.add((side, order[i], order[i + 1], before, now))
            with self.subTest(case=t):
                self.assertEqual(len(undecided), len(set(undecided)))
                self.assertEqual((changes, set(undecided)),
                                 pair_by_pair(seq, old_told, new, new_told,
                                              levels))
            found['change'] += bool(changes)
            found['undecided'] += bool(undecided)
            found['long'] += len(seq) > 64 and seq != new
        # The cases reach what they are meant to: changes, undecided pairs,
        # and long chains.
        self.assertGreater(min(found.values()), 10, found)


# --- one file per machine: --extract and --unpack -----------------------------

BUILD9 = 'glibc-2.34-275.el9_8.x86_64'


def small_measurement(build=BUILD9, version='glibc 2.34'):
    """The text of a measurement, cut to what --extract and --unpack read of
    it, the build and the version. Neither of them reads the rest."""
    return json.dumps({'format': m.FORMAT, 'glibc_build': build,
                       'glibc_version': version}, sort_keys=True) + '\n'


def members_of(data):
    """[(TarInfo, its bytes or None)] of a file --extract wrote."""
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
        return [(i, t.extractfile(i).read() if i.isfile() else None)
                for i in t.getmembers()]


def repacked(members):
    """Members written back the way --extract writes them: a file changed on
    purpose, still whole to gzip and tar."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=buf, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w',
                          format=tarfile.PAX_FORMAT) as t:
            for info, data in members:
                info = copy.copy(info)
                if data is not None:
                    info.size = len(data)
                t.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def a_member(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def with_manifest(members, **changes):
    """members with extract.json replaced by the same manifest, changed."""
    manifest = json.loads(members[-1][1])
    manifest.update(changes)
    return members[:-1] + [a_member('extract.json',
                                    json.dumps(manifest).encode())]


class ExtractPacksTheSources(unittest.TestCase):
    """The file --extract writes: the measurement, the machine's locale
    sources as found, and a manifest last.

    Freezes the ways the copy could reach the comparison smaller than the
    machine's folder while reading as whole. A copy with a file missing
    reads, on the other side, as a distro that does not ship that file.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-extract-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'locales')
        os.makedirs(os.path.join(self.src, 'sub'))
        for name, body in (
                ('C', b'LC_COLLATE\ncodepoint_collation\nEND LC_COLLATE\n'),
                ('en_US', b'LC_COLLATE\ncopy "iso14651_t1"\nEND LC_COLLATE\n'),
                ('iso14651_t1', b'% a template\n' * 400),
                (os.path.join('sub', 'inner'), b'inner\n')):
            with open(os.path.join(self.src, name), 'wb') as f:
                f.write(body)
        os.symlink('en_US', os.path.join(self.src, 'en_US_link'))
        os.symlink('sub', os.path.join(self.src, 'sub_link'))
        os.link(os.path.join(self.src, 'en_US'),
                os.path.join(self.src, 'en_US_hard'))
        self.text = small_measurement()

    def packed(self, directory=None):
        return m.archive(self.text, json.loads(self.text),
                         directory or self.src)

    def test_two_runs_write_the_same_bytes(self):
        """Owners, file times and gzip's own time are dropped, so that two
        extractions of one machine can be compared byte for byte."""
        first, _ = self.packed()
        os.utime(os.path.join(self.src, 'C'), (1, 1))   # a new time, same bytes
        second, _ = self.packed()
        self.assertEqual(first, second)
        self.assertEqual(first[4:8], b'\0\0\0\0', "gzip's time is set")

    def test_the_measurement_first_and_the_manifest_last(self):
        data, manifest = self.packed()
        members = members_of(data)
        self.assertEqual(members[0][0].name, 'locale_order.out')
        self.assertEqual(members[0][1], self.text.encode())
        self.assertEqual(members[-1][0].name, 'extract.json')
        self.assertEqual(json.loads(members[-1][1]), manifest)

    def test_the_sources_are_copied_as_found(self):
        """A link stays a link, as tar keeps it and the node checks report
        it, and a link to a folder is not walked; a second name of one file
        carries the content; a folder is walked."""
        members = {i.name: (i, d) for i, d in members_of(self.packed()[0])}
        self.assertEqual(
            sorted(n for n in members if n.startswith('locales/')),
            ['locales/C', 'locales/en_US', 'locales/en_US_hard',
             'locales/en_US_link', 'locales/iso14651_t1', 'locales/sub',
             'locales/sub/inner', 'locales/sub_link'])
        for name, target in (('en_US_link', 'en_US'), ('sub_link', 'sub')):
            link = members['locales/' + name][0]
            self.assertTrue(link.issym(), name)
            self.assertEqual(link.linkname, target)
        hard, content = members['locales/en_US_hard']
        self.assertTrue(hard.isfile())
        self.assertEqual(content, members['locales/en_US'][1])
        self.assertTrue(members['locales/sub'][0].isdir())

    def test_the_manifest_describes_every_entry(self):
        data, manifest = self.packed()
        entries = manifest['locale_sources']['entries']
        files = {i.name[len('locales/'):]:
                 'file sha256:' + hashlib.sha256(d).hexdigest()
                 for i, d in members_of(data)[1:-1] if i.isfile()}
        self.assertEqual(
            entries, dict(files, en_US_link='link to en_US', sub='folder',
                          sub_link='link to sub'))
        self.assertEqual(manifest['glibc_build'], BUILD9)
        self.assertEqual(manifest['glibc_version'], 'glibc 2.34')
        self.assertEqual(manifest['measurement']['sha256'],
                         hashlib.sha256(self.text.encode()).hexdigest())

    def assertNothingPacked(self, packed, reason):
        data, manifest = packed
        self.assertEqual([i.name for i, _ in members_of(data)],
                         ['locale_order.out', 'extract.json'])
        sources = manifest['locale_sources']
        self.assertNotIn('entries', sources)
        self.assertIn(reason, sources['not_included'])

    def test_a_missing_folder_is_named_not_packed(self):
        packed = self.packed(os.path.join(self.tmp, 'no-such-folder'))
        self.assertNothingPacked(packed, 'does not exist')
        self.assertIn('glibc-locale-source',
                      packed[1]['locale_sources']['not_included'])

    def test_an_empty_folder_is_named(self):
        empty = os.path.join(self.tmp, 'empty')
        os.mkdir(empty)
        self.assertNothingPacked(self.packed(empty), 'is empty')

    def test_a_file_that_cannot_be_read_packs_none_of_them(self):
        target = os.path.join(self.src, 'iso14651_t1')

        def refusing(path, *args, **kwargs):
            if path == target:
                raise PermissionError(13, 'Permission denied', path)
            return open(path, *args, **kwargs)
        with unittest.mock.patch.object(m, 'open', refusing, create=True):
            packed = self.packed()
        self.assertNothingPacked(packed, 'could not be read whole')
        self.assertIn('iso14651_t1', packed[1]['locale_sources']['not_included'])

    def test_a_folder_that_cannot_be_listed_packs_none_of_them(self):
        listdir, sub = os.listdir, os.path.join(self.src, 'sub')

        def refusing(path):
            if os.path.normpath(path) == sub:
                raise PermissionError(13, 'Permission denied', path)
            return listdir(path)
        with unittest.mock.patch.object(m.os, 'listdir', refusing):
            packed = self.packed()
        self.assertNothingPacked(packed, 'could not be read whole')

    def test_a_file_that_changes_while_read_packs_none_of_them(self):
        target = os.path.join(self.src, 'iso14651_t1')

        def shorter(path, *args, **kwargs):
            if path == target:
                return io.BytesIO(b'% cut')
            return open(path, *args, **kwargs)
        with unittest.mock.patch.object(m, 'open', shorter, create=True):
            packed = self.packed()
        self.assertNothingPacked(packed, 'changed while it was read')


class ExtractOnTheCommandLine(unittest.TestCase):

    def test_a_terminal_is_refused_before_measuring(self):
        """Without `> old.tar` the file would go to the screen after minutes
        of measuring. Refused at once, on any machine; on one with glibc a
        missing check would start measuring, and the timeout ends that."""
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        try:
            p = subprocess.run([sys.executable, SCRIPT, '--extract'],
                               stdout=slave, stderr=subprocess.PIPE,
                               timeout=60)
        finally:
            os.close(slave)
        self.assertEqual(p.returncode, 2)
        self.assertIn('writes a compressed file, not text', p.stderr.decode())

    def test_the_three_runs_do_not_mix(self):
        for args in (('--extract', '--compare', RHEL8, RHEL9),
                     ('--extract', '--unpack', 'x', 'y'),
                     ('--compare', RHEL8, RHEL9, '--unpack', 'x', 'y')):
            with self.subTest(args=args):
                rc, out, err = run_script(*args)
                self.assertEqual(rc, 2)
                self.assertIn('three different runs', err)
                self.assertEqual(out, '')


class WorkersOfAScriptOnStdin(unittest.TestCase):
    """`ssh node python3 - --extract < locale_order.py`: on the machine the
    script has no file. Python 3.14 starts each worker fresh by default on
    Linux (forkserver), and a fresh worker reads the script from its file:
    measured with forkserver on 3.12, every worker died and the pool
    reported them killed. fork_pool asks for copies instead.

    Here the default is set to one that starts workers fresh, and a function
    the script on stdin defines must still run in them.
    """

    def test_the_workers_run_a_function_of_the_script(self):
        driver = textwrap.dedent(f'''\
            import multiprocessing, sys
            sys.path.insert(0, {_harness.SCRIPTS_DIR!r})
            import locale_order
            multiprocessing.set_start_method('spawn')
            def square(x):
                return x * x
            if __name__ == '__main__':
                with locale_order.fork_pool(2) as pool:
                    print(sum(pool.map(square, range(10))))
            ''')
        p = subprocess.run([sys.executable, '-'], input=driver.encode(),
                           capture_output=True, timeout=120)
        self.assertEqual((p.returncode, p.stdout.decode().strip()), (0, '285'),
                         p.stderr.decode()[-2000:])


class Screen(io.StringIO):
    """stderr as the person running the script sees it: only what was
    flushed, as over ssh from Python before 3.9.

    Each write also notes whether something written before it was still
    unflushed. Checking only at the end cannot tell a line shown as it was
    written from every line shown together at the end, which is the very
    screen that looked hung on RHEL8."""

    def __init__(self):
        super().__init__()
        self.shown = ''
        self.held = []

    def write(self, text):
        self.held.append(self.getvalue() != self.shown)
        return super().write(text)

    def flush(self):
        self.shown = self.getvalue()


class ProgressOnTheScreen(unittest.TestCase):
    """What a measurement shows while it runs. On the smallest EC2 machine
    it measured one locale at a time, about ten seconds each, and on RHEL8
    over ssh every line arrived at the end: minutes of a screen that looked
    hung.

    Freezes a line held back until the end, and a line that says more was
    measured than was.
    """

    def test_one_line_a_tenth_and_never_ahead(self):
        for total in (1, 3, 9, 10, 40, 867):
            shown = [(done, m.progress(done, total, 5.4))
                     for done in range(1, total + 1)]
            shown = [(done, line) for done, line in shown if line]
            self.assertEqual(len(shown), min(total - 1, 9), total)
            for done, line in shown:
                self.assertEqual(
                    line, f'  {done} of {total} locale(s) done '
                          f'({done * 100 // total}%), 5 s so far\n')
            tenths = [done * 10 // total for done, _ in shown]
            self.assertEqual(tenths, sorted(set(tenths)), total)

    def measured_on(self, one, names):
        """What the screen got while measure() ran over names with one as
        the measuring function."""
        screen = Screen()
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: names,
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=one, UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', screen):
            m.measure(1)
        return screen

    def test_no_line_calls_a_failed_locale_measured(self):
        """Every locale fails here, as one in an encoding Python cannot
        convert does. The progress lines count what is done, and only the
        last line says how many were measured."""
        screen = self.measured_on(
            lambda name: (name, {'error': 'cannot be measured'}, None),
            [f'l{i}' for i in range(20)])
        lines = [l for l in screen.getvalue().splitlines() if '%)' in l]
        self.assertEqual(len(lines), 9, screen.getvalue())
        for line in lines:
            self.assertNotIn('measured', line)

    def test_every_line_reaches_the_screen_when_written(self):
        screen, seen = Screen(), []

        def one(name):
            seen.append(screen.shown)
            return name, {'order': 'o', 'pairs': 'p'}, None

        names = [f'l{i}' for i in range(20)]
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: names,
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=one, UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', screen):
            m.measure(1)
        self.assertEqual(len(seen), 20)
        self.assertIn('Measuring 20 locale(s)', seen[0])
        self.assertEqual(
            len(re.findall(r'locale\(s\) done \(', screen.getvalue())), 9)
        # The Measuring line and nine of progress, none written while an
        # earlier one was still held; the last is checked just below.
        self.assertEqual(len(screen.held), 10)
        self.assertEqual(screen.held, [False] * 10, 'a line was held back')
        self.assertEqual(screen.shown, screen.getvalue(), 'held back')


class UnpackRefusesWhatItCannotTrust(unittest.TestCase):
    """--unpack, the way audit.sh reads a node file. A whole file is laid out;
    anything else stops with the reason and lays out nothing, because a file
    read in part would reach steps 6 to 11 as a machine that has less."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-unpack-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        src = os.path.join(self.tmp, 'locales')
        os.makedirs(os.path.join(src, 'sub'))
        # One file larger than a write of 4 KiB, as over half the files of a
        # real tag are: a copy cut at that size must not pass for whole.
        self.big = b''.join(b'<U%04X> %% a weight line\n' % i
                            for i in range(600))
        for name, body in (('C', b'c\n'), ('en_US', b'en\n'),
                           ('iso14651_t1', self.big),
                           (os.path.join('sub', 'inner'), b'in\n')):
            with open(os.path.join(src, name), 'wb') as f:
                f.write(body)
        os.symlink('en_US', os.path.join(src, 'en_US_link'))
        self.text = small_measurement()
        self.data, _ = m.archive(self.text, json.loads(self.text), src)
        self.members = members_of(self.data)
        self.dest = os.path.join(self.tmp, 'laid-out')

    def unpack(self, data):
        path = os.path.join(self.tmp, 'node.tar')
        with open(path, 'wb') as f:
            f.write(data)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rc = m.unpack(path, self.dest)
            except SystemExit as e:
                rc = e.code
        return rc, err.getvalue()

    def assertRefused(self, data, reason):
        rc, err = self.unpack(data)
        self.assertEqual(rc, 2, err)
        self.assertIn(reason, flat(err))
        self.assertFalse(os.path.exists(self.dest), 'something was laid out')

    def read(self, *path):
        with open(os.path.join(self.dest, *path), 'rb') as f:
            return f.read()

    def test_a_whole_file_is_laid_out(self):
        self.assertEqual(self.unpack(self.data), (0, ''))
        self.assertEqual(self.read('locale_order.out'), self.text.encode())
        self.assertEqual(self.read('build-id'), (BUILD9 + '\n').encode())
        self.assertEqual(sorted(os.listdir(os.path.join(self.dest, 'locales'))),
                         ['C', 'en_US', 'en_US_link', 'iso14651_t1', 'sub'])
        self.assertGreater(len(self.big), 4096)
        self.assertEqual(self.read('locales', 'iso14651_t1'), self.big)
        self.assertEqual(self.read('locales', 'sub', 'inner'), b'in\n')
        self.assertEqual(
            os.readlink(os.path.join(self.dest, 'locales', 'en_US_link')),
            'en_US')
        self.assertFalse(os.path.exists(
            os.path.join(self.dest, 'sources-not-included')))

    def test_a_file_without_sources_lays_out_the_reason(self):
        data, manifest = m.archive(self.text, json.loads(self.text),
                                   os.path.join(self.tmp, 'none'))
        self.assertEqual(self.unpack(data), (0, ''))
        self.assertEqual(self.read('sources-not-included').decode(),
                         manifest['locale_sources']['not_included'] + '\n')
        self.assertEqual(self.read('build-id'), (BUILD9 + '\n').encode())
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'locales')))

    def test_an_empty_file(self):
        self.assertRefused(b'', 'is not a whole file written by --extract')

    def test_a_file_cut_short(self):
        self.assertRefused(self.data[:len(self.data) // 2],
                           'is not a whole file written by --extract')

    def test_text_printed_before_it(self):
        """A login message the server's shell prints on stdout lands in front
        of the file when it comes back through ssh."""
        self.assertRefused(b'Welcome to el9\n' + self.data,
                           'such as a login message')

    def test_two_files_joined(self):
        """`>> old.tar` over an older extraction of the same name leaves the
        old file first and the new one after it. Read as the first alone, the
        old build and sources would be audited as the machine's."""
        other = self.members[:1] + [(i, d + b'# newer\n') if d is not None
                                    and i.name == 'locales/C' else (i, d)
                                    for i, d in self.members[1:]]
        for name, second in (('the same file', self.data),
                             ('another file', repacked(other))):
            with self.subTest(second=name):
                self.assertRefused(self.data + second, 'is not a whole file')

    def test_anything_after_it(self):
        for tail in (b'garbage', b'\0' * 64):
            with self.subTest(tail=tail[:8]):
                self.assertRefused(self.data + tail, 'is not a whole file')

    def test_something_after_the_end_of_the_tar(self):
        """Inside one gzip member, tar stops at its end marker and never
        reads what follows it."""
        whole = gzip.decompress(self.data)
        extra = gzip.decompress(repacked([a_member('locales/extra', b'x\n')]))
        self.assertRefused(gzip.compress(whole + extra), 'is not a whole file')

    def test_no_manifest_at_the_end(self):
        self.assertRefused(repacked(self.members[:-1]), 'is not a whole file')

    def test_a_manifest_that_does_not_read(self):
        self.assertRefused(
            repacked(self.members[:-1] + [a_member('extract.json', b'{cut')]),
            'is not a whole file')

    def test_the_manifest_not_last(self):
        self.assertRefused(
            repacked([self.members[0], self.members[-1]] + self.members[1:-1]),
            'is not a whole file')

    def test_an_entry_after_the_manifest(self):
        """Whatever follows the manifest is not described by it. Read as the
        manifest, an entry that happens to be JSON would be refused for the
        wrong reason, and one that happened to match would be believed."""
        self.assertRefused(
            repacked(self.members + [a_member('locales/extra', b'{}')]),
            'is not a whole file')

    def test_the_measurement_not_first(self):
        self.assertRefused(
            repacked(self.members[1:-1] + [self.members[0], self.members[-1]]),
            'is not a whole file')

    def test_another_format(self):
        self.assertRefused(
            repacked(with_manifest(self.members,
                                   format='locale_order_extract/0')),
            'not written by this version of --extract')

    def test_the_measurement_edited(self):
        info, text = self.members[0]
        edited = [(info, text.replace(b'2.34', b'2.35'))] + self.members[1:]
        self.assertRefused(repacked(edited),
                           'the measurement is not the one it describes')

    def test_no_build(self):
        self.assertRefused(
            repacked(with_manifest(self.members, glibc_build=None)),
            'names no glibc build')

    def test_a_build_the_measurement_does_not_name(self):
        self.assertRefused(
            repacked(with_manifest(self.members,
                                   glibc_build='glibc-2.34-100.el9.x86_64')),
            'its build is not the one the measurement names')

    def test_neither_sources_nor_a_reason(self):
        for sources in (None, {'directory': 'x'},
                        {'directory': 'x', 'entries': {}},
                        # nothing printable: audit.sh's $(cat) reads nothing
                        {'directory': 'x', 'not_included': '\n'},
                        {'directory': 'x', 'not_included': '\x00'},
                        {'directory': 'x', 'entries': {'C': 'folder'},
                         'not_included': 'y'}):
            with self.subTest(sources=sources):
                self.assertRefused(
                    repacked(with_manifest(self.members,
                                           locale_sources=sources)),
                    'neither which locale sources it holds nor why')

    def test_a_source_edited(self):
        edited = [(i, d + b'# edited\n' if i.name == 'locales/C' else d)
                  for i, d in self.members]
        self.assertRefused(repacked(edited),
                           'its locale sources are not the ones it lists')

    def test_a_source_left_out(self):
        self.assertRefused(
            repacked([x for x in self.members if x[0].name != 'locales/C']),
            'its locale sources are not the ones it lists')

    def test_a_source_it_does_not_list(self):
        self.assertRefused(
            repacked(self.members[:-1] + [a_member('locales/extra', b'x\n'),
                                          self.members[-1]]),
            'its locale sources are not the ones it lists')

    def test_an_entry_outside_locales(self):
        for name in ('locales/../evil', 'evil', 'locales/./evil'):
            with self.subTest(name=name):
                self.assertRefused(
                    repacked(self.members[:-1] + [a_member(name, b'x\n'),
                                                  self.members[-1]]),
                    'is outside locales/')

    def test_an_entry_under_a_link(self):
        """A link to another folder, then a file under the link, would write
        outside dest. Refused before anything is written."""
        outside = os.path.join(self.tmp, 'outside')
        os.mkdir(outside)
        link = tarfile.TarInfo('locales/x')
        link.type, link.linkname = tarfile.SYMTYPE, outside
        entries = {'x': 'link to ' + outside,
                   'x/y': 'file sha256:' + hashlib.sha256(b'y\n').hexdigest()}
        bad = with_manifest([self.members[0], (link, None),
                             a_member('locales/x/y', b'y\n'), self.members[-1]],
                            locale_sources={'directory': 'x',
                                            'entries': entries})
        self.assertRefused(repacked(bad), 'lies under a link')
        self.assertEqual(os.listdir(outside), [])

    def outside_and_link(self, name):
        """A folder beside dest, and a link in the file that points into it."""
        outside = os.path.join(self.tmp, 'outside')
        os.makedirs(outside, exist_ok=True)
        link = tarfile.TarInfo(name)
        link.type, link.linkname = tarfile.SYMTYPE, outside
        return outside, link

    def with_entries(self, members, entries):
        """The measurement, members, and a manifest listing entries: a file
        that agrees with itself, so only the check under test can refuse
        it."""
        return repacked(with_manifest(
            [self.members[0]] + members + [self.members[-1]],
            locale_sources={'directory': 'x', 'entries': entries}))

    def test_a_name_twice(self):
        """A link, then a file of the same name: the file would be written
        through the link, outside dest, with the manifest satisfied by the
        second entry alone."""
        outside, link = self.outside_and_link('locales/x')
        sha = 'file sha256:' + hashlib.sha256(b'y\n').hexdigest()
        self.assertRefused(
            self.with_entries([(link, None), a_member('locales/x', b'y\n')],
                              {'x': sha}),
            'locales/x appears twice')
        self.assertEqual(os.listdir(outside), [])

    def test_a_link_then_a_folder_of_the_same_name(self):
        outside, link = self.outside_and_link('locales/d')
        folder = tarfile.TarInfo('locales/d')
        folder.type = tarfile.DIRTYPE
        sha = 'file sha256:' + hashlib.sha256(b'y\n').hexdigest()
        self.assertRefused(
            self.with_entries([(link, None), (folder, None),
                               a_member('locales/d/y', b'y\n')],
                              {'d': 'folder', 'd/y': sha}),
            'locales/d appears twice')
        self.assertEqual(os.listdir(outside), [])

    def test_names_that_differ_only_in_case(self):
        """On a disk that does not tell case apart the two are one file: the
        copy would hold one where the machine has two, and read as whole."""
        sha = {b: 'file sha256:' + hashlib.sha256(b).hexdigest()
               for b in (b'upper\n', b'lower\n')}
        self.assertRefused(
            self.with_entries([a_member('locales/C', b'upper\n'),
                               a_member('locales/c', b'lower\n')],
                              {'C': sha[b'upper\n'], 'c': sha[b'lower\n']}),
            'locales/C and locales/c differ only in case')

    def test_an_entry_under_a_link_spelt_in_another_case(self):
        outside, link = self.outside_and_link('locales/D')
        sha = 'file sha256:' + hashlib.sha256(b'y\n').hexdigest()
        self.assertRefused(
            self.with_entries([(link, None), a_member('locales/d/y', b'y\n')],
                              {'D': 'link to ' + outside, 'd/y': sha}),
            'locales/d/y comes before, or without, the folder that holds it')
        self.assertEqual(os.listdir(outside), [])

    def test_an_entry_before_its_folder(self):
        folder = tarfile.TarInfo('locales/sub')
        folder.type = tarfile.DIRTYPE
        sha = 'file sha256:' + hashlib.sha256(b'in\n').hexdigest()
        self.assertRefused(
            self.with_entries([a_member('locales/sub/inner', b'in\n'),
                               (folder, None)],
                              {'sub': 'folder', 'sub/inner': sha}),
            'comes before, or without, the folder that holds it')

    def test_an_entry_that_is_not_a_file_a_folder_or_a_link(self):
        hard = tarfile.TarInfo('locales/C2')
        hard.type, hard.linkname = tarfile.LNKTYPE, 'locales/C'
        self.assertRefused(
            repacked(self.members[:-1] + [(hard, None), self.members[-1]]),
            'is not a file, a folder or a link')

    def test_a_write_that_goes_wrong_is_refused(self):
        """What reaches the disk is read back and held to the manifest's
        hashes: a copy that lost part of a file is refused instead of reaching
        steps 6 to 10 as the machine's files."""
        fdopen = os.fdopen

        class Halving:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

            def write(self, data):
                return self.f.write(data[:len(data) // 2])
        with unittest.mock.patch.object(
                m.os, 'fdopen', lambda fd, mode: Halving(fdopen(fd, mode))):
            rc, err = self.unpack(self.data)
        self.assertEqual(rc, 2, err)
        self.assertIn('is not what', err)
        self.assertIn('lists, so it cannot stand for', flat(err))

    def test_a_destination_that_exists(self):
        os.mkdir(self.dest)
        rc, err = self.unpack(self.data)
        self.assertEqual(rc, 2, err)
        self.assertIn('cannot lay the file out in', err)
        self.assertEqual(os.listdir(self.dest), [])



class UnpackLaysOutTheCharmaps(unittest.TestCase):
    """--unpack and the charmaps --extract packs since 13.7 PR D: laid out
    and read back like the sources, or the reason they are missing, written
    for steps 6 to 8 to print. A file written before they were packed is
    read, never refused, and says so."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-unpack-cm-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'locales')
        os.makedirs(self.src)
        with open(os.path.join(self.src, 'C'), 'wb') as f:
            f.write(b'c\n')
        self.cms = os.path.join(self.tmp, 'charmaps')
        os.makedirs(self.cms)
        for name, body in (('UTF-8.gz', b'utf8 charmap\n'),
                           ('ISO-8859-1.gz', b'latin1 charmap\n')):
            with open(os.path.join(self.cms, name), 'wb') as f:
                f.write(gzip.compress(body, mtime=0))
        self.text = small_measurement()
        self.dest = os.path.join(self.tmp, 'laid-out')

    def archive(self, charmaps):
        return m.archive(self.text, json.loads(self.text), self.src, charmaps)

    def unpack(self, data):
        path = os.path.join(self.tmp, 'node.tar')
        with open(path, 'wb') as f:
            f.write(data)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rc = m.unpack(path, self.dest)
            except SystemExit as e:
                rc = e.code
        return rc, err.getvalue()

    def assertRefused(self, data, reason):
        rc, err = self.unpack(data)
        self.assertEqual(rc, 2, err)
        self.assertIn(reason, flat(err))
        self.assertFalse(os.path.exists(self.dest), 'something was laid out')

    def read(self, *path):
        with open(os.path.join(self.dest, *path), 'rb') as f:
            return f.read()

    def test_the_charmaps_are_packed_and_laid_out_whole(self):
        data, manifest = self.archive(self.cms)
        self.assertEqual(sorted(manifest['charmaps']['entries']),
                         ['ISO-8859-1.gz', 'UTF-8.gz'])
        self.assertEqual(self.unpack(data), (0, ''))
        self.assertEqual(sorted(os.listdir(os.path.join(self.dest,
                                                        'charmaps'))),
                         ['ISO-8859-1.gz', 'UTF-8.gz'])
        with open(os.path.join(self.cms, 'UTF-8.gz'), 'rb') as f:
            self.assertEqual(self.read('charmaps', 'UTF-8.gz'), f.read())
        self.assertFalse(os.path.exists(
            os.path.join(self.dest, 'charmaps-not-included')))

    def test_a_file_from_before_the_charmaps_says_so(self):
        """No entry at all: the file is whole, and what it lacks is named."""
        data, manifest = self.archive(None)
        self.assertNotIn('charmaps', manifest)
        self.assertEqual(self.unpack(data), (0, ''))
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'charmaps')))
        self.assertIn('was written by an --extract that did not pack the '
                      "machine's charmaps",
                      flat(self.read('charmaps-not-included').decode()))
        self.assertTrue(os.path.isdir(os.path.join(self.dest, 'locales')))

    def test_a_machine_without_the_charmaps_says_why(self):
        data, manifest = self.archive(os.path.join(self.tmp, 'none'))
        self.assertEqual(self.unpack(data), (0, ''))
        self.assertEqual(self.read('charmaps-not-included').decode(),
                         manifest['charmaps']['not_included'] + '\n')
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'charmaps')))

    def test_a_charmap_edited_left_out_or_added_is_refused(self):
        data, _ = self.archive(self.cms)
        members = members_of(data)
        edited = [(i, d + b'x' if i.name == 'charmaps/UTF-8.gz' else d)
                  for i, d in members]
        self.assertRefused(repacked(edited),
                           'its charmaps are not the ones it lists')
        self.assertRefused(
            repacked([x for x in members if x[0].name != 'charmaps/UTF-8.gz']),
            'its charmaps are not the ones it lists')
        self.assertRefused(
            repacked(members[:-1] + [a_member('charmaps/EXTRA.gz', b'x'),
                                     members[-1]]),
            'its charmaps are not the ones it lists')

    def test_charmaps_a_manifest_does_not_list_are_refused(self):
        """A file from before the charmaps holds none: one that holds some
        and has no entry for them is not that file."""
        data, _ = self.archive(None)
        members = members_of(data)
        self.assertRefused(
            repacked(members[:-1] + [a_member('charmaps/UTF-8.gz', b'x'),
                                     members[-1]]),
            'its charmaps are not the ones it lists')

    def test_neither_charmaps_nor_a_reason(self):
        data, _ = self.archive(self.cms)
        members = members_of(data)
        for charmaps in (None, {'directory': 'x'},
                         {'directory': 'x', 'entries': {}},
                         # nothing printable: audit.sh's $(cat) reads
                         # nothing (NUL included, which strip() keeps)
                         {'directory': 'x', 'not_included': '\n'},
                         {'directory': 'x', 'not_included': ' \t'},
                         {'directory': 'x', 'not_included': '\x00'},
                         {'directory': 'x', 'not_included': '\x00\n\x01'},
                         {'directory': 'x', 'entries': {'A': 'folder'},
                          'not_included': 'y'}):
            with self.subTest(charmaps=charmaps):
                self.assertRefused(
                    repacked(with_manifest(members, charmaps=charmaps)),
                    'neither which charmaps it holds nor why')

    def test_an_entry_outside_both_folders(self):
        data, _ = self.archive(self.cms)
        members = members_of(data)
        for name, reason in (('evil', 'is outside locales/ and charmaps/'),
                             ('charmaps/../evil', 'is outside charmaps/'),
                             ('charmaps/./evil', 'is outside charmaps/')):
            with self.subTest(name=name):
                self.assertRefused(
                    repacked(members[:-1] + [a_member(name, b'x'),
                                             members[-1]]), reason)

    def test_extract_packs_this_machine_s_charmaps(self):
        """archive() packs charmaps only when given them; --extract gives it
        the machine's, from CHARMAPS."""
        class Out(io.StringIO):
            buffer = io.BytesIO()

            def isatty(self):
                return False
        out, err = Out(), io.StringIO()
        with unittest.mock.patch.multiple(
                m, LOCALE_SOURCES=self.src, CHARMAPS=self.cms,
                measure=lambda jobs: (self.text, json.loads(self.text), 1),
                finished=lambda *a: 0), \
                unittest.mock.patch.object(m.sys, 'stdout', out), \
                contextlib.redirect_stderr(err):
            self.assertEqual(m.extract(1), 0)
        manifest = json.loads(members_of(out.buffer.getvalue())[-1][1])
        self.assertEqual(sorted(manifest['charmaps']['entries']),
                         ['ISO-8859-1.gz', 'UTF-8.gz'])
        self.assertIn(f'Charmaps: 2 entr(ies) from {self.cms}.', err.getvalue())

    def test_two_charmaps_one_name_apart_in_case(self):
        data, _ = self.archive(self.cms)
        members = members_of(data)
        info, body = next((i, d) for i, d in members
                          if i.name == 'charmaps/UTF-8.gz')
        twin = copy.copy(info)
        twin.name = 'charmaps/utf-8.gz'
        entries = json.loads(members[-1][1])['charmaps']['entries']
        entries['utf-8.gz'] = entries['UTF-8.gz']
        self.assertRefused(
            repacked(with_manifest(members[:-1] + [(twin, body),
                                                   members[-1]],
                                   charmaps={'directory': 'x',
                                             'entries': entries})),
            'charmaps/UTF-8.gz and charmaps/utf-8.gz differ only in case')


# --- the parts of a file beside the order --------------------------------------

CLEAN_LIBC = {'looked_for': ['postgres', 'postmaster'], 'processes': 2,
              'stale': [], 'other': [], 'unreadable': 0, 'no_libc': 0,
              'unnamed': 0, 'not_checked': None, 'replaced': False}
EARLIER_BOTH = [('money', {'earlier': ['OLD', 'NEW']}),
                ('libc', {'earlier': ['OLD', 'NEW']})]


def with_parts(money=None, libc=None):
    """An edit that makes a file of the first format one of this format:
    the list of its parts, the decimals of money (2 in every locale, but
    those in money) and its PostgreSQL processes (CLEAN_LIBC, but what libc
    changes). The order part is the machine's own, untouched."""
    def edit(d):
        d['format'] = m.FORMAT
        d['parts'] = list(m.PARTS[m.FORMAT])
        d['money'] = {name: 2 for name in d['locales']}
        d['money'].update(money or {})
        d['postgres_libc'] = dict(CLEAN_LIBC, **(libc or {}))
    return edit


# Kept of each machine by the classes below, as the machines measured them:
# C and POSIX (the control), and locales whose order did not change from
# RHEL 9 to 10, two of them with money decimals that differ (2 and 0). A
# changed locale would cost each comparison the analysis of its change,
# which these checks do not read.
FEW = ('C', 'POSIX', 'de_DE.utf8', 'en_US.utf8', 'ja_JP.utf8', 'sv_SE.utf8')


def trimmed(text, names=FEW):
    """A recorded measurement cut to names, with the records they use, as a
    machine listing only those would have written it."""
    d = json.loads(text)
    d['locales'] = {n: d['locales'][n] for n in names}
    d['locales_listed'] = len(names)
    for kind, key in (('order_data', 'order'), ('pair_data', 'pairs')):
        used = {r[key] for r in d['locales'].values() if 'error' not in r}
        d[kind] = {k: v for k, v in d[kind].items() if k in used}
    d['content'] = m.content_digest(d)
    return json.dumps(d, sort_keys=True) + '\n'


class Checks(Variants):
    """Base for the classes that read the checks beside the order: each run
    writes the report, the summary and the characters file, and the three
    must name the same checks, in the same order. RHEL9 and RHEL10 are read
    cut to FEW, which these checks need and which is quick to compare."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for path in (RHEL9, RHEL10):
            cls.texts[path] = trimmed(cls.texts[path])

    def made(self, base, tag, *edits):
        """base with every edit applied, resealed, as its own file."""
        data = json.loads(self.texts[base])
        for edit in edits:
            edit(data)
        data['content'] = m.content_digest(data)
        path = os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}-{tag}.out')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, sort_keys=True) + '\n')
        return path

    def checks(self, old, new, command_line=False):
        """[(kind, data)] the run names beside the order, the same in its
        three outputs. Run through main() in this process, with the argv a
        command line would give it, unless command_line."""
        tag = self.id().rsplit('.', 1)[-1]
        summary = os.path.join(self.tmp, f'{tag}.sum')
        chars = os.path.join(self.tmp, f'{tag}.tsv')
        args = ['--compare', old, new, '--summary-to', summary,
                '--characters-to', chars]
        if command_line:
            rc, out, err = run_compare(*args[1:])
        else:
            stdout, stderr = io.StringIO(), io.StringIO()
            with unittest.mock.patch.object(m.sys, 'argv', [SCRIPT] + args), \
                    contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                rc = m.main()
            out, err = stdout.getvalue(), stderr.getvalue()
        self.assertEqual((rc, err), (0, ''))
        report = parse_report(out)['checks']
        with open(summary, encoding='utf-8') as f:
            brief = parse_summary(f.read())['checks']
        with open(chars, encoding='utf-8') as f:
            notes = parse_characters(f.read()).checks
        self.assertEqual(report, brief)
        self.assertEqual(report, notes)
        return report

    def common(self, old, new):
        """The locales on both machines."""
        return set(json.loads(self.texts[old])['locales']) & set(
            json.loads(self.texts[new])['locales'])


class FilesOfEachFormat(Checks):
    """A file written before the decimals of money and the PostgreSQL
    processes were measured is whole, and is compared: each part it lacks is
    NOT checked, naming the side that lacks it, and never reads as "the
    same". The control needs nothing such a file lacks, and runs on it."""

    HELD = [('control', {'held': True})]

    def test_two_files_of_the_first_format(self):
        """The two whole files as recorded, on the command line."""
        self.assertEqual(self.checks(RHEL9, RHEL10, command_line=True),
                         self.HELD + EARLIER_BOTH)

    def test_two_files_of_the_first_format_cut(self):
        """The same cut to FEW: what the classes below build on."""
        self.assertEqual(self.checks(self.made(RHEL9, 'old'),
                                     self.made(RHEL10, 'new')),
                         self.HELD + EARLIER_BOTH)

    def test_the_first_format_on_the_old_side(self):
        new = self.made(RHEL10, 'new', with_parts())
        self.assertEqual(self.checks(self.made(RHEL9, 'old'), new), self.HELD + [
            ('money', {'earlier': ['OLD']}), ('libc', {'earlier': ['OLD']}),
            ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True})])

    def test_the_first_format_on_the_new_side(self):
        old = self.made(RHEL9, 'old', with_parts())
        self.assertEqual(self.checks(old, self.made(RHEL10, 'new')), self.HELD + [
            ('money', {'earlier': ['NEW']}), ('libc', {'earlier': ['NEW']}),
            ('libc', {'side': 'OLD', 'clean': 2}), ('libc', {'note': True})])

    def test_two_files_of_this_format(self):
        old = self.made(RHEL9, 'old', with_parts())
        new = self.made(RHEL10, 'new', with_parts())
        self.assertEqual(self.checks(old, new), self.HELD + [
            ('money', {'same': len(self.common(RHEL9, RHEL10))}),
            ('libc', {'side': 'OLD', 'clean': 2}),
            ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True})])

    def test_the_corpus_digest_did_not_move_with_the_format(self):
        """The recorded files are of the first format; their corpus digest
        is still this script's (MeasurementsAreCurrent asserts the same of
        each file; this names why)."""
        self.assertNotEqual(m.FORMAT, m.CORPUS_VERSION)
        self.assertEqual(json.loads(self.texts[RHEL9])['corpus'],
                         m.corpus_digest())


class FilesOfEachFormatRefused(Checks):
    """A file of this format that lacks a part its format holds, or holds it
    malformed, is refused with the reason, like a file cut short: it is not
    a whole measurement, and a part read as missing would read as not
    checked when the machine did check it."""

    def refused(self, phrase, *edits):
        """load() of the file stops the run with exit 2 and the phrase: the
        first thing --compare does with each file, run here in this process
        (test_refused_on_the_command_line runs the whole way)."""
        new = self.made(RHEL10, 'new', with_parts(), *edits)
        err = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.load(new, m.corpus_digest())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn(phrase, flat(err.getvalue()))

    def test_refused_on_the_command_line(self):
        new = self.made(RHEL10, 'new', with_parts(), lambda d: d.pop('money'))
        rc, out, err = run_compare(self.made(RHEL9, 'old'), new)
        self.assertEqual((rc, out), (2, ''), err)
        self.assertIn('lacks a part its format holds', flat(err))

    def test_without_the_decimals_of_money(self):
        self.refused('lacks a part its format holds',
                     lambda d: d.pop('money'))

    def test_without_the_postgresql_processes(self):
        self.refused('lacks a part its format holds',
                     lambda d: d.pop('postgres_libc'))

    def test_without_its_list_of_parts(self):
        self.refused('does not list the parts its format holds',
                     lambda d: d.pop('parts'))

    def test_with_another_list_of_parts(self):
        for parts in (['order', 'money'], ['money', 'order', 'postgres_libc'],
                      ['order', 'money', 'postgres_libc', 'more']):
            with self.subTest(parts=parts):
                self.refused('does not list the parts its format holds',
                             lambda d: d.update(parts=parts))

    def test_a_first_format_file_with_a_part_of_this_one(self):
        def first(d):
            d['format'] = 'locale_order/1'
            del d['parts'], d['postgres_libc']
        self.refused('holds one it does not', first)

    def test_a_first_format_file_that_lists_a_part_of_this_one(self):
        def first(d):
            d['format'] = 'locale_order/1'
            d['parts'] = ['order', 'money']
            del d['money'], d['postgres_libc']
        self.refused('does not list the parts its format holds', first)

    def test_decimals_of_money_not_one_per_locale(self):
        for case, edit in (
                ('a locale missing', lambda d: d['money'].pop('C')),
                ('a name more', lambda d: d['money'].update(xx_XX=2)),
                ('a boolean', lambda d: d['money'].update(C=True)),
                ('past a char', lambda d: d['money'].update(C=128)),
                ('below a char', lambda d: d['money'].update(C=-129)),
                ('a fraction', lambda d: d['money'].update(C=2.0)),
                ('an empty reason', lambda d: d['money'].update(C='')),
                ('not a map', lambda d: d.update(money=[]))):
            with self.subTest(case=case):
                self.refused('the decimals of money are not one per locale', edit)

    def test_the_postgresql_processes_malformed(self):
        libc = lambda **k: (lambda d: d['postgres_libc'].update(**k))
        for case, edit in (
                ('a key missing', lambda d: d['postgres_libc'].pop('unnamed')),
                ('a key more', libc(more=1)),
                ('a count below zero', libc(unreadable=-1)),
                ('a count that is a boolean', libc(processes=True)),
                ('more stale than processes', libc(stale=[[1, 'a'], [2, 'b'], [3, 'c']])),
                ('more on another file than processes', libc(other=[[1, 'a'], [2, 'b'], [3, 'c']])),
                ('stale and other past processes', libc(stale=[[1, 'a']], other=[[2, 'b']], unreadable=1)),
                ('no C library past processes', libc(no_libc=3)),
                ('an other entry of text', libc(other=[['1', 'a']])),
                ('no list of other', lambda d: d['postgres_libc'].pop('other')),
                ('replaced not said', lambda d: d['postgres_libc'].pop('replaced')),
                ('replaced as a number', libc(replaced=1)),
                ('more unread than processes', libc(unreadable=3)),
                ('stale and unread past processes', libc(stale=[[1, 'a']], unreadable=2)),
                ('a stale entry of one item', libc(stale=[[1]])),
                ('a stale pid that is text', libc(stale=[['1', 'a']])),
                ('an empty reason', libc(not_checked='')),
                ('no name looked for', libc(looked_for=[])),
                ('not a map', lambda d: d.update(postgres_libc=[]))):
            with self.subTest(case=case):
                self.refused('what it says of the PostgreSQL processes is '
                             'malformed', edit)


class ControlLocale(Checks):
    """C and POSIX sort in byte order on every glibc: they are the locale
    glibc carries inside itself, whose strcoll is strcmp. A machine where
    they did not measure so did not measure what the script means to, and
    the run says so, on that side, for each of the two."""

    def test_the_fingerprint_is_what_three_machines_measured(self):
        order, pairs, probes = m.control_fingerprint()
        for machine, path in MACHINES:
            for name in ('C', 'POSIX'):
                r = read_json(path)['locales'][name]
                with self.subTest(machine=machine, locale=name):
                    self.assertEqual((r['order'], r['pairs'], r['probes'],
                                      r['chars'], r['partly']),
                                     (order, pairs, probes, 0x7F, 0))

    def without(self, side):
        return lambda d: d['locales'][side].update(partly=1)

    def control(self, old, new):
        """Every line the run says about the control, none left out."""
        return [d for kind, d in self.checks(old, new) if kind == 'control']

    def test_failed_on_the_new_machine(self):
        new = self.made(RHEL10, 'new', self.without('C'))
        self.assertEqual(self.control(self.made(RHEL9, 'old'), new),
                         [{'failed': 'NEW', 'names': ['C']}])

    def test_failed_on_the_old_machine(self):
        old = self.made(RHEL9, 'old', lambda d: d['locales']['POSIX'].update(
            probes='61.62> none ?'))
        self.assertEqual(self.control(old, self.made(RHEL10, 'new')),
                         [{'failed': 'OLD', 'names': ['POSIX']}])

    def test_both_failed_on_one_machine(self):
        new = self.made(RHEL10, 'new', self.without('C'), self.without('POSIX'))
        self.assertEqual(self.control(self.made(RHEL9, 'old'), new),
                         [{'failed': 'NEW', 'names': ['C', 'POSIX']}])

    def test_failed_on_both_machines(self):
        old = self.made(RHEL9, 'old', self.without('POSIX'))
        new = self.made(RHEL10, 'new', self.without('C'))
        self.assertEqual(self.control(old, new), [
            {'failed': 'OLD', 'names': ['POSIX']},
            {'failed': 'NEW', 'names': ['C']}])

    def test_not_checked_without_C_and_POSIX(self):
        def gone(d):
            del d['locales']['C'], d['locales']['POSIX']
            d['locales_listed'] -= 2
        new = self.made(RHEL10, 'new', gone)
        self.assertEqual(self.control(self.made(RHEL9, 'old'), new), [{'unknown': [
            'C is not in the NEW file', 'POSIX is not in the NEW file']}])

    def test_not_checked_where_C_was_not_measured(self):
        old = self.made(RHEL9, 'old', lambda d: d['locales'].update(
            C={'error': 'measuring failed: x', 'codeset': 'ANSI_X3.4-1968'}))
        self.assertEqual(self.control(old, self.made(RHEL10, 'new')), [{'unknown': [
            'C could not be measured on the OLD machine (measuring failed: x)']}])


class DecimalsOfMoney(Checks):
    """What a stored money value means is frac_digits of the session's
    lc_monetary, as cash.c reads it: outside 0 to 10 it keeps 2. A change
    is named with both numbers, a locale that could not be read is NOT
    checked, and none of either is said with the count of locales read."""

    def pair(self, old_money=None, new_money=None):
        return (self.made(RHEL9, 'old', with_parts(money=old_money)),
                self.made(RHEL10, 'new', with_parts(money=new_money)))

    def money(self, *pair):
        return [d for kind, d in self.checks(*pair) if kind == 'money']

    def test_the_same_everywhere(self):
        self.assertEqual(self.money(*self.pair()),
                         [{'same': len(self.common(RHEL9, RHEL10))}])

    def test_two_locales_change(self):
        self.assertEqual(
            self.money(*self.pair({'ja_JP.utf8': 0}, {'de_DE.utf8': 3})),
            [{'changed': {'de_DE.utf8': (2, 3), 'ja_JP.utf8': (0, 2)}}])

    def test_what_cash_c_reads_alike_is_alike(self):
        names = sorted(self.common(RHEL9, RHEL10))[:6]
        old = dict(zip(names, (127, 11, -1, -128, 10, 0)))
        new = dict(zip(names, (2, 2, 2, 127, 11, -1)))
        self.assertEqual(self.money(*self.pair(old, new)), [
            {'changed': {names[4]: (10, 2), names[5]: (0, 2)}}])

    def test_two_locales_not_read(self):
        names = sorted(self.common(RHEL9, RHEL10))[:2]
        self.assertEqual(
            self.money(*self.pair({names[0]: 'cannot be selected: x'},
                                  {names[1]: 'cannot be selected: y'})),
            [{'unknown': {names[0]: {'old': 'cannot be selected: x'},
                          names[1]: {'new': 'cannot be selected: y'}}},
             {'same': len(self.common(RHEL9, RHEL10)) - 2}])

    def test_the_scale_cash_c_keeps(self):
        self.assertEqual([m.money_scale(v) for v in (-128, -1, 0, 1, 10, 11, 127)],
                         [2, 2, 0, 1, 10, 2, 2])

    def test_a_locale_that_cannot_be_selected(self):
        """Its reason is kept, never a number; a locale after it is still
        read; and LC_MONETARY is put back as it was, since the measurement
        goes on in this process. glibc's localeconv() and setlocale() are
        doubles here: this machine need not be Linux."""
        calls = []

        def setlocale(category, name=None):
            calls.append((category, name))
            if name == 'bad':
                raise m.locale.Error('unsupported locale setting')
            return 'as it was' if name is None else name

        conv = unittest.mock.Mock()
        conv.return_value.contents.frac_digits = 3
        lib = unittest.mock.Mock(localeconv=conv)
        with unittest.mock.patch.object(m, 'libc', lambda: lib), \
                unittest.mock.patch.object(m.locale, 'setlocale', setlocale):
            got = m.money_digits(['bad', 'good'])
        self.assertEqual(got, {'bad': 'cannot be selected: unsupported locale '
                                      'setting', 'good': 3})
        self.assertEqual(calls[-1], (m.locale.LC_MONETARY, 'as it was'))
        self.assertTrue(all(c == m.locale.LC_MONETARY for c, _ in calls))


class PostgresProcessesCompared(Checks):
    """Each machine's PostgreSQL processes against its glibc on disk, said
    for each side: those still on a replaced glibc, those not read, and the
    reason any could not be. What it does not look at is said beside it."""

    def libc(self, old=None, new=None):
        old = self.made(RHEL9, 'old', with_parts(libc=old))
        new = self.made(RHEL10, 'new', with_parts(libc=new))
        return [d for kind, d in self.checks(old, new) if kind == 'libc']

    def test_stale_on_the_new_unread_on_the_old(self):
        self.assertEqual(self.libc(
            old={'unreadable': 1},
            new={'stale': [[7, '/usr/lib64/libc.so.6 (deleted)']]}), [
            {'side': 'OLD', 'why': '1 of 2 process(es) named postgres or '
                                   'postmaster could not be read'},
            {'side': 'NEW', 'stale': 1}, {'note': True}])

    def test_stale_on_the_old_unread_on_the_new_order(self):
        """The order the three outputs give them: above first."""
        self.assertEqual(self.libc(old={'stale': [[7, 'a (deleted)']]},
                                   new={'unnamed': 3})[:2], [
            {'side': 'OLD', 'stale': 1},
            {'side': 'NEW', 'why': 'the name of 3 other process(es) could not '
                                   'be read'}])

    def test_stale_on_the_old_unread_on_the_new(self):
        self.assertEqual(self.libc(
            old={'stale': [[7, 'a (deleted)'], [8, 'b (deleted)']]},
            new={'unnamed': 3}), [
            {'side': 'OLD', 'stale': 2},
            {'side': 'NEW', 'why': 'the name of 3 other process(es) could not '
                                   'be read'}, {'note': True}])

    def test_stale_and_hidden_on_one_machine(self):
        self.assertEqual(self.libc(new={
            'stale': [[7, 'x (deleted)']],
            'not_checked': '/proc is mounted with hidepid=2, which hides other '
                           'users\' processes'}), [
            {'side': 'OLD', 'clean': 2}, {'side': 'NEW', 'stale': 1},
            {'side': 'NEW', 'why': '/proc is mounted with hidepid=2, which '
                                   'hides other users\' processes'},
            {'note': True}])

    def test_none_found(self):
        """No PostgreSQL seen says nothing was checked, with !!: it may run
        in a container, or under another name, or not yet."""
        self.assertEqual(self.libc(old={'processes': 0}), [
            {'side': 'OLD', 'none': True}, {'side': 'NEW', 'clean': 2},
            {'note': True}])

    def test_another_c_library_on_both(self):
        """Another C library on either side voids the comparison and stands
        above the list; the NEW side still on the replaced glibc does not."""
        self.assertEqual(self.libc(
            old={'other': [[7, '/x/libc.so.6']]},
            new={'other': [[7, '/x/libc.so.6'], [8, '/y/libc.so.6']],
                 'stale': [[9, '/usr/lib64/libc.so.6 (deleted)']],
                 'processes': 3}), [
            {'side': 'OLD', 'other': 1}, {'side': 'NEW', 'other': 2},
            {'side': 'NEW', 'stale': 1}, {'note': True}])

    def where(self, old=None, new=None):
        old = self.made(RHEL9, 'old', with_parts(libc=old))
        new = self.made(RHEL10, 'new', with_parts(libc=new))
        return self.checks(old, new)

    def test_the_old_side_on_the_replaced_glibc_stands_above(self):
        same = ('money', {'same': len(self.common(RHEL9, RHEL10))})
        self.assertEqual(self.where(old={'stale': [[7, 'x (deleted)']]}), [
            ('control', {'held': True}), ('libc', {'side': 'OLD', 'stale': 1}),
            same, ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True})])

    def test_the_new_side_on_the_replaced_glibc_stands_below(self):
        same = ('money', {'same': len(self.common(RHEL9, RHEL10))})
        self.assertEqual(self.where(new={'stale': [[7, 'x (deleted)']]}), [
            ('control', {'held': True}), same,
            ('libc', {'side': 'OLD', 'clean': 2}),
            ('libc', {'side': 'NEW', 'stale': 1}), ('libc', {'note': True})])

    def test_replaced_while_measured_stands_above(self):
        same = ('money', {'same': len(self.common(RHEL9, RHEL10))})
        self.assertEqual(self.where(new={'replaced': True}), [
            ('control', {'held': True}),
            ('libc', {'side': 'NEW', 'replaced': True}), same,
            ('libc', {'side': 'OLD', 'clean': 2}), ('libc', {'note': True})])

    def test_no_c_library_is_not_known(self):
        self.assertEqual(self.libc(new={'no_libc': 1}), [
            {'side': 'OLD', 'clean': 2},
            {'side': 'NEW', 'why': '1 of 2 process(es) named postgres or '
                                   'postmaster show no C library'},
            {'note': True}])


class PostgresProcessesRead(unittest.TestCase):
    """postgres_libc() over a /proc built here, and the line the machine's
    own run prints. A file that cannot be opened is a folder in its place,
    which no user can read as a file, root included."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='locale_order_proc_')
        self.addCleanup(shutil.rmtree, self.tmp)
        self.proc = os.path.join(self.tmp, 'proc')
        os.mkdir(self.proc)
        self.mounts = os.path.join(self.tmp, 'mounts')
        self.mounts_say(f'proc {self.proc} proc rw,nosuid,nodev,noexec 0 0')
        # This process's own C library: the file every process() below maps
        # unless it says otherwise (device fd:00, inode 1).
        self.process('self', 'python3', ['/usr/lib64/libc.so.6'])

    def mounts_say(self, *lines):
        with open(self.mounts, 'w') as f:
            f.write('\n'.join(lines) + '\n')

    def process(self, pid, comm, maps=()):
        """A process; comm or maps None for one that cannot be read, False
        for one that is not there. A mapped path is on fd:00, inode 1, the
        same file as this process's, unless given as (path, inode)."""
        d = os.path.join(self.proc, str(pid))
        os.mkdir(d)
        for name, text in (('comm', comm), ('maps', maps)):
            if text is None:
                os.mkdir(os.path.join(d, name))
            elif text is not False:
                with open(os.path.join(d, name), 'w') as f:
                    f.write(text + '\n' if name == 'comm' else ''.join(
                        '7f00-7f01 r-xp 00000000 %s %s %s\n'
                        % (((p + ('fd:00',))[2], p[1], p[0])
                           if isinstance(p, tuple) else ('fd:00', 1, p))
                        for p in text))

    def read(self, euid=1000):
        return m.postgres_libc(self.proc, self.mounts, euid)

    def test_the_replaced_libc_of_each_glibc(self):
        # A deleted file and the live one that replaced it never share an
        # inode (measured: 1813959316 before the update, 1701099458 after).
        for pid, path in ((10, '/usr/lib64/libc.so.6'),
                          (11, '/usr/lib64/libc-2.28.so')):
            self.process(pid, 'postgres', [(path + ' (deleted)', 9),
                                           '/x/libm.so.6'])
        self.process(12, 'postgres', ['/usr/lib64/libc.so.6'])
        got = self.read()
        self.assertEqual((got['processes'], got['stale']), (3, [
            [10, '/usr/lib64/libc.so.6 (deleted)'],
            [11, '/usr/lib64/libc-2.28.so (deleted)']]))
        self.assertIsNone(m.libc_why(got))

    def test_other_deleted_files_are_not_the_libc(self):
        # Each deleted file on an inode of its own, as a deleted file is.
        self.process(10, 'postgres', [(p + ' (deleted)', i) for i, p in enumerate((
            '/usr/lib64/libcrypto.so.3', '/usr/lib64/libc.so.6.old',
            '/usr/lib64/libc_malloc_debug.so.0', '/tmp/libc.so.6x'), 20)]
            + ['/usr/lib64/libc.so.6'])
        self.assertEqual(self.read()['stale'], [])

    def test_only_postgres_and_postmaster_are_read(self):
        stale = [('/usr/lib64/libc.so.6 (deleted)', 9)]
        self.process(10, 'postmaster', stale)
        self.process(11, 'sshd', stale)
        self.process(12, 'postgres:', stale)
        got = self.read()
        self.assertEqual((got['processes'], got['stale']),
                         (1, [[10, '/usr/lib64/libc.so.6 (deleted)']]))

    def test_another_c_library_not_deleted(self):
        """A container's C library is another file, never deleted: as clean
        as the measured one to a check of " (deleted)" alone."""
        self.process(10, 'postgres', [('/usr/lib64/libc.so.6', 2)])
        self.process(11, 'postgres', [('/usr/lib/x86_64-linux-gnu/libc.so.6', 3),
                                      ('/usr/lib64/libc-2.28.so', 4)])
        self.process(12, 'postgres', ['/usr/lib64/libc.so.6'])
        got = self.read(0)
        self.assertEqual((got['processes'], got['stale'], got['other']), (3, [], [
            [10, '/usr/lib64/libc.so.6'],
            [11, '/usr/lib/x86_64-linux-gnu/libc.so.6']]))
        self.assertIsNone(m.libc_why(got))

    def test_no_c_library_in_the_maps(self):
        self.process(10, 'postgres', ['[vdso]'])
        self.process(11, 'postgres', ['/usr/bin/postgres'])
        got = self.read(0)
        self.assertEqual((got['processes'], got['no_libc'], got['stale'],
                          got['other']), (2, 2, [], []))
        self.assertEqual(m.libc_why(got), '2 of 2 process(es) named postgres '
                                          'or postmaster show no C library')

    def test_an_empty_map_is_passed_over(self):
        """A process whose maps read empty runs no code: a zombie, measured
        on Rocky 9 at 0 bytes, or one exiting. Passed over like one that
        ended, not read as one that shows no C library."""
        self.process(10, 'postgres', [])
        self.process(11, 'postgres', ['/usr/lib64/libc.so.6'])
        got = self.read(0)
        self.assertEqual((got['processes'], got['no_libc'], m.libc_why(got)),
                         (1, 0, None))

    def test_the_same_inode_on_another_device(self):
        self.process(10, 'postgres', [('/usr/lib64/libc.so.6', 1, 'fd:01')])
        self.assertEqual(self.read(0)['other'], [[10, '/usr/lib64/libc.so.6']])

    def test_the_measured_library_and_another_in_one_process(self):
        self.process(10, 'postgres', ['/usr/lib64/libc.so.6',
                                      ('/opt/x/libc.so.6', 5)])
        self.assertEqual(self.read(0)['other'], [[10, '/opt/x/libc.so.6']])

    def test_replaced_while_this_measured(self):
        """This process's own C library deleted: an update came while it
        measured. A postgres process on that very file runs what was
        measured; one on another deleted file is stale; one on the new file
        is on another C library."""
        shutil.rmtree(os.path.join(self.proc, 'self'))
        self.process('self', 'python3', ['/usr/lib64/libc.so.6 (deleted)'])
        self.process(10, 'postgres', ['/usr/lib64/libc.so.6 (deleted)'])
        self.process(11, 'postgres', [('/usr/lib64/libc.so.6', 2)])
        self.process(12, 'postgres', [('/usr/lib64/libc.so.6 (deleted)', 3)])
        got = self.read(0)
        self.assertEqual((got['replaced'], got['stale'], got['other'],
                          got['not_checked']), (True, [
            [12, '/usr/lib64/libc.so.6 (deleted)']], [
            [11, '/usr/lib64/libc.so.6']], None))
        self.assertEqual([flat(l) for l in m.machine_libc_lines(got)][0],
                         '!! glibc was replaced on disk while this measured: '
                         'what was measured may not be what this machine runs '
                         'now; measure it again.')

    def test_who_runs_it_decides_what_hides(self):
        """The call measure() makes, with no euid: the process's own."""
        self.mounts_say(f'proc {self.proc} proc rw,hidepid=2 0 0')
        for euid, hidden in ((1000, True), (0, False)):
            with self.subTest(euid=euid), \
                    unittest.mock.patch.object(m.os, 'geteuid', lambda: euid):
                why = m.postgres_libc(self.proc, self.mounts)['not_checked']
                self.assertEqual(why is not None and 'hidepid=2' in why, hidden)

    def test_own_c_library_not_found(self):  # noqa: the cases share the fake /proc
        """Without this process's own C library nothing can be the same
        file: not checked, while a deleted one is still named."""
        for case in ('no maps', 'no libc in them'):
            with self.subTest(case=case):
                shutil.rmtree(os.path.join(self.proc, 'self'))
                if case == 'no maps':
                    os.mkdir(os.path.join(self.proc, 'self'))
                else:
                    self.process('self', 'python3', ['/usr/bin/python3'])
                self.process(10, 'postgres', ['/usr/lib64/libc.so.6 (deleted)'])
                got = self.read(0)
                self.assertIn('the C library this process measured with is not',
                              got['not_checked'])
                self.assertEqual((got['stale'], got['other']),
                                 ([[10, '/usr/lib64/libc.so.6 (deleted)']], []))
                shutil.rmtree(os.path.join(self.proc, '10'))

    def test_maps_that_cannot_be_read(self):
        self.process(10, 'postgres', None)
        self.process(11, 'postgres', ['/usr/lib64/libc.so.6'])
        got = self.read()
        self.assertEqual((got['processes'], got['unreadable']), (2, 1))
        self.assertEqual(m.libc_why(got), '1 of 2 process(es) named postgres '
                                          'or postmaster could not be read')

    def test_a_name_that_cannot_be_read(self):
        self.process(10, None)
        got = self.read()
        self.assertEqual((got['processes'], got['unnamed']), (0, 1))
        self.assertEqual(m.libc_why(got), 'the name of 1 other process(es) '
                                          'could not be read')

    def test_a_process_that_ended_while_read(self):
        self.process(10, False)
        self.process(11, 'postgres', False)
        got = self.read()
        self.assertEqual((got['processes'], got['unnamed'], got['unreadable'],
                          m.libc_why(got)), (0, 0, 0, None))

    def test_no_proc_at_all(self):
        got = m.postgres_libc(os.path.join(self.tmp, 'none'), self.mounts, 0)
        self.assertIsNotNone(got['not_checked'])
        self.assertTrue(got['not_checked'].startswith(
            os.path.join(self.tmp, 'none') + ' could not be read'))

    def test_what_hides_other_users_processes(self):
        p = self.proc
        for mounts, euid, hidden in (
                ([f'proc {p} proc rw,hidepid=2 0 0'], 1000, 'hidepid=2'),
                ([f'proc {p} proc rw,hidepid=invisible 0 0'], 1000, 'hidepid=invisible'),
                ([f'proc {p} proc rw,hidepid=ptraceable 0 0'], 1000, 'hidepid=ptraceable'),
                ([f'proc {p} proc rw,hidepid=2 0 0'], 0, None),
                ([f'proc {p} proc rw,hidepid=0 0 0'], 1000, None),
                ([f'proc {p} proc rw,hidepid=off 0 0'], 1000, None),
                ([f'proc {p} proc rw,hidepid=2 0 0', f'proc {p} proc rw 0 0'], 1000, None),
                ([f'proc {p} proc rw 0 0', f'proc {p} proc rw,hidepid=1 0 0'], 1000, 'hidepid=1'),
                ([f'sysfs {p} sysfs rw 0 0'], 1000, 'lists no proc file system'),
                ([f'proc /elsewhere proc rw 0 0'], 1000, 'lists no proc file system')):
            with self.subTest(mounts=mounts, euid=euid):
                self.mounts_say(*mounts)
                why = self.read(euid)['not_checked']
                if hidden is None:
                    self.assertIsNone(why)
                else:
                    self.assertIn(hidden, why)

    def test_mounts_that_cannot_be_read(self):
        os.remove(self.mounts)
        os.mkdir(self.mounts)
        self.assertIn('could not be read', self.read()['not_checked'])
        self.assertIsNone(self.read(0)['not_checked'])

    def test_what_the_machine_prints(self):
        clean = dict(CLEAN_LIBC)
        self.assertEqual(m.machine_libc_lines(clean), [
            'PostgreSQL processes: 2 process(es) named postgres or postmaster, '
            'all running the C library measured there.'])
        self.assertEqual([flat(l) for l in m.machine_libc_lines(
            dict(clean, processes=0))], [
            '!! No process named postgres or postmaster was found, so nothing '
            'checked which C library PostgreSQL runs here.'])
        three = m.machine_libc_lines(dict(clean, stale=[[1, 'x']],
                                          other=[[2, 'y']], not_checked='why'))
        self.assertEqual([flat(line) for line in three], [
            '!! 1 PostgreSQL process(es) still run the glibc that was replaced '
            'on disk: what this machine measured is not what that PostgreSQL '
            'sorts with until it is restarted.',
            '!! 1 PostgreSQL process(es) run another C library than the one '
            'measured here, as when PostgreSQL runs in a container: what this '
            'machine measured is not what that PostgreSQL sorts with.',
            '!! PostgreSQL processes NOT checked: why. To check, run this as '
            'root, or as the user PostgreSQL runs as where /proc does not hide '
            'other users\' processes (README, command 1.b).'])

    def test_the_machine_prints_it_when_done(self):
        """The call site: what the run prints when it ends carries the line
        about PostgreSQL, so a check that could not look is fixed there."""
        screen = Screen()
        out = {'locales': {}, 'locales_listed': 0,
               'postgres_libc': dict(CLEAN_LIBC, stale=[[1, 'x']])}
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            m.finished(out, 1, 10)
        self.assertIn('!! 1 PostgreSQL process(es) still run the glibc',
                      flat(screen.getvalue()))

    def test_measure_keeps_both_parts(self):
        """The call site: what measure() writes is what each part returned,
        under the parts its format lists."""
        screen = Screen()
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: ['a', 'b'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=lambda name: (name, {'error': 'x'}, None),
                UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1),
                money_digits=lambda names: {n: 3 for n in names},
                postgres_libc=lambda: dict(CLEAN_LIBC, processes=5)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', screen):
            text, out, _ = m.measure(1)
        data = json.loads(text)
        self.assertEqual((data['format'], data['parts']),
                         (m.FORMAT, list(m.PARTS[m.FORMAT])))
        self.assertEqual(data['money'], {'a': 3, 'b': 3})
        self.assertEqual(data['postgres_libc']['processes'], 5)
        self.assertEqual(data['content'], m.content_digest(data))


if __name__ == '__main__':
    unittest.main()
