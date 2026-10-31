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

each measured on 2026-10-26 with `python3 locale_order.py --extract`
(`/usr/libexec/platform-python` on RHEL8), every language pack installed,
and cut to what a file of the first format holds and the lists of strings
its orders number: the measurement inside the file --extract wrote, every
other part taken out and the file sealed again.
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
import collections
import contextlib
import copy
import ctypes
import gzip
import hashlib
import io
import itertools
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
import types
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
# Python has no converter for their encodings (ARMSCII-8, GEORGIAN-PS,
# EUC-TW); their strings are measured as bytes.
NO_PYTHON_CODEC = {'hy_AM.armscii8', 'ka_GE', 'ka_GE.georgianps', 'zh_TW.euctw'}

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
FILE_LINE = 'Every character, other string and pair found, by locale:'
SUMMARY_FILE_LINE = '       every character, other string and pair:'
NO_FILE_LINE = ('Pass --characters-to FILE for every character, other '
                'string and pair found.')
# How a string that is not a character is named, under the changes.
OTHERS_NOTE = 'In the sort orders, a string that'


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
# The bytes of a string that is not a character: 'bytes 80', 'bytes F0 AB'.
HEX = r'[0-9A-F]{2}(?: [0-9A-F]{2}){0,3}'
# One string as a list names it: 'V (U+0056)', 'U+0056' or 'bytes 80'.
CHARACTER = rf'((?:. \()?U\+[0-9A-F]{{4,6}}\)?|bytes {HEX})'


def element(text):
    """A string as the outputs name it, as the tests compare it: a
    character as its code point, any other string as its bytes."""
    mt = re.fullmatch(r'(?:. \()?U\+([0-9A-F]{4,6})(?: [A-Z0-9][A-Z0-9 -]*)?\)?',
                      text)
    if mt:
        return int(mt[1], 16)
    mt = re.fullmatch(rf'bytes ({HEX})', text)
    if mt:
        return bytes.fromhex(mt[1])
    raise ReportShapeError(f'unknown string: {text!r}')


def run_key(e):
    """Where a string goes in a list: characters by code point, then the
    others, shorter first and then by their bytes."""
    if isinstance(e, int):
        return (0, e)
    return (1, len(e), int.from_bytes(e, 'big'))


def run_elements(a, b):
    """The strings of the run a..b."""
    if isinstance(a, int):
        return list(range(a, b + 1))
    return [v.to_bytes(len(a), 'big') for v in
            range(int.from_bytes(a, 'big'), int.from_bytes(b, 'big') + 1)]


def parse_run(part):
    """'U+0030..U+0039', 'U+0B66', 'bytes 80..FF' or 'bytes F0 AB A0 C0..F0
    AB A0 DF' as (first, last)."""
    mt = re.fullmatch(r'U\+([0-9A-F]{4,6})(?:\.\.U\+([0-9A-F]{4,6}))?', part)
    if mt:
        a = int(mt[1], 16)
        return a, int(mt[2], 16) if mt[2] else a
    mt = re.fullmatch(rf'bytes ({HEX})(?:\.\.({HEX}))?', part)
    if mt:
        a = bytes.fromhex(mt[1])
        b = bytes.fromhex(mt[2]) if mt[2] else a
        if len(a) == len(b):
            return a, b
    raise ReportShapeError(f'unknown run of strings: {part!r}')


def parse_also(text):
    """An 'also, in PostgreSQL: ...' line as {'aliases': {encoding: [name]},
    'spellings': [database locale]}, plus, where the line names them,
    'anywhere': [who] (the names PostgreSQL never imports, usable in a
    database of any encoding) and 'superuser': [(who, [spelling])] (those
    usable only in SQL_ASCII, set there by initdb or a superuser); who is
    'this name', 'these names' or the names listed."""
    aliases, spellings, out = {}, [], {}
    for part in text[len('also, in PostgreSQL: '):].split('; '):
        mt = re.fullmatch(r'(.+?) in a database of any encoding, as its '
                          r'locale or in CREATE COLLATION', part)
        if mt:
            out.setdefault('anywhere', []).append(mt[1])
            continue
        mt = re.fullmatch(r'(.+?)(?:, also spelt (\S+(?:, \S+)*),)? only in a '
                          r'SQL_ASCII database, as its locale or in CREATE '
                          r'COLLATION, which only initdb or a superuser can set',
                          part)
        if mt:
            out.setdefault('superuser', []).append(
                (mt[1], mt[2].split(', ') if mt[2] else []))
            continue
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
    return {'aliases': aliases, 'spellings': spellings, **out}


def parse_runs(text):
    """'U+0030..U+0039, U+0B66, bytes 80..FF' as [(0x30, 0x39), (0xB66,
    0xB66), (b'\\x80', b'\\xff')], refused unless the runs are in the order
    the report lists strings and none touches the one before it."""
    runs = []
    for part in text.split(', '):
        a, b = parse_run(part)
        if run_key(b) < run_key(a) or (runs and (
                run_key(a)[:-1] == run_key(runs[-1][1])[:-1]
                and run_key(a)[-1] <= run_key(runs[-1][1])[-1] + 1
                or run_key(a) < run_key(runs[-1][1]))):
            raise ReportShapeError(f'runs out of order or touching: {text!r}')
        runs.append((a, b))
    return runs


def run_count(runs):
    return sum(len(run_elements(a, b)) for a, b in runs)


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
    mt = re.fullmatch(r"Character sets: every byte sequence that each "
                      r"machine's charmaps give a character, in the (\d+) "
                      r"locale\(s\) compared, is among the strings measured\.",
                      text)
    if mt:
        return 'charmaps', {'checked': int(mt[1])}
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
    found = use_kind(text) or ctype_kind(text) or bytes_kind(text)
    if found:
        return found
    raise ReportShapeError(f'unknown check line: {text!r}')


CTYPE = r'Character rules \(LC_CTYPE\)'
EUC = CTYPE + r' in an EUC_JP, EUC_KR, EUC_CN or EUC_TW database'
NAMES = r'(\S+(?:, \S+)*)'
# One item of a list of characters the old machine classified, as
# character() writes it, then what changed: 'ʂ (U+0282 LATIN ... HOOK): x'
# or 'U+1063 MYANMAR ...: x'. Unicode names hold capitals, digits, spaces
# and hyphens only.
CTYPE_ITEM = (r'(?:(\S) \()?U\+([0-9A-F]{4,6})(?: ([A-Z0-9][A-Z0-9 -]*))?(\))?: '
              r'(.+?)')
CTYPE_NEXT = r'(?=; (?:\S \()?U\+[0-9A-F]{4,6}[ :)]|$)'
CTYPE_WORDS = (r'(?:(?:letter|digit|punctuation|space|another printable '
               r'character|no class) -> (?:letter|digit|punctuation|space|'
               r'another printable character|no class)|gains the (?:capital|small '
               r'letter) \S+(?: \(U\+[0-9A-F]{4,6}\))?|loses the (?:capital|small '
               r'letter) \S+(?: \(U\+[0-9A-F]{4,6}\))?|(?:capital|small letter) '
               r'\S+(?: \(U\+[0-9A-F]{4,6}\))? -> \S+(?: \(U\+[0-9A-F]{4,6}\))?|'
               r'(?:joins|leaves) \[\[:[a-z]+:\]\])')


def ctype_words(text):
    """What changed of one character or byte, refused unless every part is
    one the comparison writes."""
    for part in text.split(', '):
        if not re.fullmatch(CTYPE_WORDS, part):
            raise ReportShapeError(f'unknown change of rules: {text!r}')
    return text


def ctype_items(text):
    """'U+1063 ...: punctuation -> letter; ...' as {code point: words},
    refused unless the items rebuild the text."""
    out, rebuilt = {}, []
    for mt in re.finditer(CTYPE_ITEM + CTYPE_NEXT, text):
        glyph, code, name, paren, words = mt.groups()
        if bool(glyph) != bool(paren):
            raise ReportShapeError(f'unknown character: {mt[0]!r}')
        cp = int(code, 16)
        if cp in out:
            raise ReportShapeError(f'U+{code} named twice: {text!r}')
        out[cp] = ctype_words(words)
        rebuilt.append(mt[0])
    if '; '.join(rebuilt) != text:
        raise ReportShapeError(f'unknown list of characters: {text!r}')
    return out


def ctype_group(text):
    """{'all': N} for the locales compared, or {'names': [...]}."""
    mt = re.fullmatch(r'the (\d+) locale\(s\) compared on both machines'
                      r'( that a UTF8 database can use)?', text)
    if mt:
        return {'all': int(mt[1]), 'utf8': bool(mt[2])}
    mt = re.fullmatch(r'(\d+) locale\(s\): ' + NAMES, text)
    if not mt:
        raise ReportShapeError(f'unknown group of locales: {text!r}')
    names = mt[2].split(', ')
    if len(names) != int(mt[1]) or len(set(names)) != len(names):
        raise ReportShapeError(f'says {mt[1]} and names {len(names)}: {text!r}')
    return {'names': names}


def ctype_kind(text):
    """('ctype', data) of a line about the character rules; None when the
    line is not about them. Each output gives the line as one text, a list
    joined with '; ' (ctype_paragraph reads the report's and the summary's
    lines into it)."""
    mt = re.fullmatch(r'!! ' + CTYPE + r' NOT checked: ' + EARLIER_FILES, text)
    if mt:
        return 'ctype', {'earlier': earlier_sides(mt, 1, None, 3)}
    mt = re.fullmatch(CTYPE + r' of each (character|byte): the same in the '
                      r'(other )?(\d+) locale\(s\) compared on both machines'
                      r'( that a UTF8 database can use)?\.', text)
    if mt:
        if bool(mt[4]) != (mt[1] == 'character'):
            raise ReportShapeError(f'which locales, unsaid or wrong: {text!r}')
        return 'ctype', {'same': mt[1], 'count': int(mt[3]), 'other': bool(mt[2]),
                         'utf8': bool(mt[4])}
    mt = re.fullmatch(CTYPE + r' of each character: not compared in (\d+) '
                      r'locale\(s\) of an encoding no UTF8 database can use; a '
                      r'database that can use them reads their rules byte '
                      r'by byte'
                      r'(, or, in EUC, as the line on EUC says)?\.', text)
    if mt:
        return 'ctype', {'elsewhere': int(mt[1]), 'with euc': bool(mt[2])}
    mt = re.fullmatch(r'!! ' + CTYPE + r' CHANGE for (\d+) character\(s\) in (.+?): '
                      r'each gets another upper case, lower case or class\. '
                      r'upper\(\), lower\(\), initcap\(\), ILIKE, regular '
                      r'expressions and full-text search read these rules in a '
                      r'UTF8 database, so they can give other results there for '
                      r'text that holds one\. (\d+) of them the old machine '
                      r'already classified(?:\.|: (.+?)\.)(?: The other (\d+) it '
                      r'did not classify; a UTF8 database can hold them too, and '
                      r'they change those results as well\.)?', text)
    if mt:
        known = ctype_items(mt[4]) if mt[4] else {}
        other = int(mt[5]) if mt[5] else 0
        if (len(known) != int(mt[3]) or int(mt[3]) + other != int(mt[1])
                or (mt[5] is not None and other == 0)):
            raise ReportShapeError(f'counts disagree: {text!r}')
        group = ctype_group(mt[2])
        if group.pop('utf8', True) is not True:
            raise ReportShapeError(f'which locales, unsaid: {text!r}')
        return 'ctype', dict(group, change='character',
                             count=int(mt[1]), classified=known, other=other)
    mt = re.fullmatch(r'!! ' + CTYPE + r' CHANGE for (\d+) byte\(s\) in (.+?)\. A '
                      r'database of one byte per character, SQL_ASCII too, reads '
                      r'them in upper\(\), lower\(\), initcap\(\), ILIKE, regular '
                      r'expressions and full-text search\. A database of any other '
                      r'encoding, UTF8 too, reads every byte in fuzzystrmatch and, '
                      r'on PostgreSQL 13 to 17, in ltree\'s labels, and reads the '
                      r'ASCII bytes in the character tests of full-text search, '
                      r'ltree and pg_trgm and, except in UTF8, in regular '
                      r'expressions: (.+)\.', text)
    if mt:
        found = {}
        for item in mt[3].split('; '):
            it = re.fullmatch(r'byte ([0-9A-F]{2}): (.+)', item)
            if not it or int(it[1], 16) in found:
                raise ReportShapeError(f'unknown byte: {item!r}')
            found[int(it[1], 16)] = ctype_words(it[2])
        if len(found) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and lists {len(found)}: {text!r}')
        group = ctype_group(mt[2])
        if group.pop('utf8', False) is not False:
            raise ReportShapeError(f'bytes said of UTF8 alone: {text!r}')
        return 'ctype', dict(group, change='byte', count=int(mt[1]),
                             bytes=found)
    mt = re.fullmatch(rf'!! {CTYPE} in a UTF8 database: on the (old|new) machine '
                      r'glibc reads (\d+) byte sequence\(s\) as another character '
                      r'than the one they encode, in (\d+) locale\(s\) of UTF-8, so '
                      r'upper\(\), lower\(\), initcap\(\), ILIKE and full-text '
                      r'search can give other results for text that holds one; (the (?:OLD|NEW) file did not measure this|the '
                      r'(?:old|new) machine could not measure this \(.+\)), so '
                      r'whether it changed is not known: (.+)\.', text)
    if mt:
        found = {}
        for item in mt[5].split('; '):
            it = re.fullmatch(rf'{SPACED_BYTES}: (?:\S \()?{CODE}(?: [A-Z0-9][A-Z0-9 -]*)?'
                              rf'\)? -> (?:\S \()?{CODE}(?: [A-Z0-9][A-Z0-9 -]*)?\)?',
                              item)
            if not it or it[1] in found or it[2] == it[3]:
                raise ReportShapeError(f'unknown sequence: {item!r}')
            found[it[1]] = (int(it[2], 16), int(it[3], 16))
        if len(found) != int(mt[2]):
            raise ReportShapeError(f'says {mt[2]} and lists {len(found)}: {text!r}')
        return 'ctype', {'utf8 read': found, 'side': mt[1], 'count': int(mt[3]),
                         'why': mt[4]}
    mt = re.fullmatch(rf'!! {CTYPE} in a UTF8 database: CHANGE for (\d+) byte '
                      r'sequence\(s\) in (\d+) locale\(s\) of UTF-8: glibc converts '
                      r'each to another character on the new machine than on the '
                      r'old one, so upper\(\), lower\(\), initcap\(\), ILIKE and '
                      r'full-text search can give other results for text that '
                      r'holds one: (.+)\.', text)
    if mt:
        found = {}
        for item in mt[3].split('; '):
            it = re.fullmatch(rf'{SPACED_BYTES}: (?:\S \()?{CODE}(?: [A-Z0-9][A-Z0-9 -]*)?'
                              rf'\)? -> (?:\S \()?{CODE}(?: [A-Z0-9][A-Z0-9 -]*)?\)?',
                              item)
            if not it or it[1] in found or it[2] == it[3]:
                raise ReportShapeError(f'unknown sequence: {item!r}')
            found[it[1]] = (int(it[2], 16), int(it[3], 16))
        if len(found) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and lists {len(found)}: {text!r}')
        return 'ctype', {'utf8 converted': found, 'count': int(mt[2])}
    mt = re.fullmatch(rf'!! {EUC}: CHANGE for (\d+) byte sequence\(s\) in (\d+) '
                      rf'locale\(s\) \(({GROUPS})\): glibc converts each to another '
                      r'character on the new machine than on the old one, so '
                      r'upper\(\), lower\(\), initcap\(\), ILIKE and full-text '
                      r'search can give other results for text that holds one: '
                      r'(.+)\.', text)
    if mt:
        found = {}
        for item in mt[4].split('; '):
            it = re.fullmatch(rf'{SPACED_BYTES}: (?:\S \()?{CODE}(?: [A-Z0-9][A-Z0-9 -]*)?'
                              rf'\)? -> (?:\S \()?{CODE}(?: [A-Z0-9][A-Z0-9 -]*)?\)?',
                              item)
            if not it or it[1] in found or it[2] == it[3]:
                raise ReportShapeError(f'unknown sequence: {item!r}')
            found[it[1]] = (int(it[2], 16), int(it[3], 16))
        if len(found) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and lists {len(found)}: {text!r}')
        return 'ctype', {'euc converted': found, 'names': by_codeset(mt[3], mt[2])}
    mt = re.fullmatch(rf'!! {EUC}: CHANGE for (\d+) character\(s\) in (\d+) '
                      rf'locale\(s\) \(({GROUPS})\): each gets another upper case, '
                      r'lower case or class, and glibc converts the text of such a '
                      r'database to it alike on both machines\. upper\(\), '
                      r'lower\(\), initcap\(\), ILIKE and full-text search read '
                      r'these rules there, so they can give other results for '
                      r'text that holds one\. (\d+) of them the old machine '
                      r'already classified(?:\.|: (.+?)\.)(?: The other (\d+) it '
                      r'did not classify\.)?', text)
    if mt:
        known = ctype_items(mt[5]) if mt[5] else {}
        other = int(mt[6]) if mt[6] else 0
        if (len(known) != int(mt[4]) or int(mt[4]) + other != int(mt[1])
                or (mt[6] is not None and other == 0)):
            raise ReportShapeError(f'counts disagree: {text!r}')
        return 'ctype', {'euc change': known, 'other': other,
                         'names': by_codeset(mt[3], mt[2])}
    mt = re.fullmatch(rf'!! {EUC}: the same in the (other )?(\d+) locale\(s\) '
                      r'compared on both machines: each byte sequence such a '
                      r'database accepts that glibc converts on both machines it '
                      r'converts to the same character on both, and none of those '
                      rf'characters\' rules change\. ({GROUPS})\.', text)
    if mt:
        return 'ctype', {'euc same': by_codeset(mt[3], mt[2]), 'other': bool(mt[1])}
    mt = re.fullmatch(rf'(!! )?{EUC}: (the same for ASCII, the only text compared,|'
                      r'compared for ASCII alone) in (\d+) locale\(s\), since '
                      r'(neither machine\'s glibc can|the old machine\'s glibc '
                      r'could not|the new machine\'s glibc cannot) convert their '
                      r'encoding; the line on upper\(\), lower\(\) and ILIKE above '
                      rf'says what that does to the rest of the text\. ({GROUPS})\.',
                      text)
    if mt:
        if not bool(mt[1]) == mt[2].startswith('the same'):
            raise ReportShapeError(f'what was not compared, unsaid: {text!r}')
        return 'ctype', {'euc ascii': {'neither': 'both'}.get(mt[4].split()[0],
                                                              mt[4].split()[1]),
                         'same': mt[2].startswith('the same'),
                         'names': by_codeset(mt[5], mt[3])}
    mt = re.fullmatch(rf'!! {EUC}: NOT checked in (\d+) locale\(s\)\. (.+)', text)
    if mt:
        unknown, rest = {}, mt[2]
        while rest:
            it = re.match(rf'(.+?) \(on (both machines|the old machine|the new '
                          rf'machine)\): ({GROUPS})\.(?: |$)', rest)
            if not it:
                raise ReportShapeError(f'unknown EUC reasons: {rest!r}')
            side = 'both machines' if it[2] == 'both machines' else it[2][4:]
            why = (it[1] if it[1].startswith(('glibc', 'iconv_open'))
                   else it[1][:1].lower() + it[1][1:])
            for name in by_codeset(it[3]):
                if side in unknown.get(name, {}):
                    raise ReportShapeError(f'{name} twice: {text!r}')
                unknown.setdefault(name, {})[side] = why
            rest = rest[it.end():]
        if len(unknown) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and lists {len(unknown)}')
        return 'ctype', {'euc unknown': unknown}
    mt = re.fullmatch(r'(?!.*\. These are PostgreSQL\'s own)'
                      r'(?:Every such (character|byte|byte sequence|character and '
                      r'byte(?: and byte sequence)?|character and byte sequence|byte '
                      r'and byte sequence) is (?:in (\S.*)|(a row of this file))|'
                      r'Pass --characters-to FILE for every such (character|byte|'
                      r'byte sequence|character and byte(?: and byte sequence)?|'
                      r'character and byte sequence|byte and byte sequence))'
                      r'\.', text)
    if mt:
        # Which file, and whether one was named, the report, the summary and
        # the file itself say each in its own words.
        return 'ctype', {'where': mt[1] or mt[4]}
    mt = re.fullmatch(r'!! ' + CTYPE + r' NOT checked in (\d+) locale\(s\): '
                      r'(.+)\.', text)
    if mt:
        unknown = {}
        for item in mt[2].split('; '):
            it = re.fullmatch(r'(\S+) \((old machine|new machine|both '
                              r'machines)\): (.+)', item)
            if not it or it[2] in unknown.get(it[1], {}) or (
                    'both machines' in unknown.get(it[1], {})) or (
                    it[2] == 'both machines' and it[1] in unknown):
                raise ReportShapeError(f'unknown LC_CTYPE item: {item!r}')
            unknown.setdefault(it[1], {})[it[2]] = it[3]
        if len(unknown) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and lists {len(unknown)}')
        return 'ctype', {'unknown': unknown}
    if 'LC_CTYPE' in text.split(':')[0] or re.match(
            r'(?:Every such|Pass --characters-to FILE for every such) '
            r'(?:character|byte(?! sequence))', text):
        raise ReportShapeError(f'unknown line about LC_CTYPE: {text!r}')
    return None


def ctype_listed(take_line, peek, indent, more, item, item_more):
    """The report's or the summary's lines of a finding with a list (a head
    at indent and more, one item per line at item and item_more, ending ';'
    but the last, '.', and a tail at more) as the one text ctype_kind reads;
    None when the next line is not such a finding."""
    if not re.fullmatch(' ' * indent + r'!! Character rules \(LC_CTYPE\) (?:CHANGE '
                        r'for|in an EUC_JP|in a UTF8 database).*', peek()):
        return None

    def run(at):
        out = []
        while re.fullmatch(' ' * at + r'\S.*', peek()):
            out.append(take_line())
        return out
    head = [take_line()] + run(more)
    items = []
    while re.fullmatch(' ' * item + r'\S.*', peek()):
        items.append(flat(' '.join([take_line()] + run(item_more))))
    tail = run(more)
    if items and (not items[-1].endswith('.') or any(
            not i.endswith(';') for i in items[:-1])):
        raise ReportShapeError(f'a list that does not end where it says: {items!r}')
    text = flat(' '.join(head))
    if items:
        text += ' ' + '; '.join(i[:-1] for i in items) + '.'
    if tail:
        text += ' ' + flat(' '.join(tail))
    return text


FN = r'upper\(\), lower\(\) and ILIKE'
GCONV = (r'On RHEL this is what a machine without glibc-gconv-extra does; '
         r'install it and measure that machine again\.')
GROUP = r'\S+: \S+(?:, \S+)*'
GROUPS = rf'{GROUP}(?:; {GROUP})*'


def by_codeset(text, count=None):
    """'EUC-JP: ja_JP, ja_JP.eucjp; EUC-KR: ko_KR' as {name: encoding},
    refused unless every name is there once and, given count, as many."""
    out = {}
    for group in text.split('; '):
        mt = re.fullmatch(r'(\S+): (\S+(?:, \S+)*)', group)
        if not mt:
            raise ReportShapeError(f'unknown group of locales: {group!r}')
        for name in mt[2].split(', '):
            if name in out:
                raise ReportShapeError(f'{name} named twice: {text!r}')
            out[name] = mt[1]
    if count is not None and len(out) != int(count):
        raise ReportShapeError(f'says {count} and names {len(out)}: {text!r}')
    return out


def use_kind(text):
    """('use', data) of a line about upper(), lower() and ILIKE; None when
    the line is not about them."""
    mt = re.fullmatch(rf'!! {FN} give an ERROR on the new machine in (\d+) '
                      r'locale\(s\) whose encoding the old machine\'s glibc '
                      r'can convert\. In a database of the locale\'s own '
                      r'encoding, any text that '
                      r'is not ASCII stops them with "ERROR: invalid multibyte '
                      r'character for locale", and initcap\(\) and full-text '
                      r'search too, because the new machine\'s glibc cannot '
                      rf'convert that encoding\. {GCONV} ({GROUPS})\.', text)
    if mt:
        return 'use', {'changed': by_codeset(mt[2], mt[1])}
    mt = re.fullmatch(rf'{FN} convert text on the new machine in (\d+) '
                      r'locale\(s\) whose encoding the old machine\'s glibc '
                      rf'could not convert: ({GROUPS})\.', text)
    if mt:
        return 'use', {'back': by_codeset(mt[2], mt[1])}
    mt = re.fullmatch(rf'!! {FN} give an ERROR on both machines in (\d+) '
                      r'locale\(s\), as before the upgrade: in a database of '
                      r'the locale\'s own encoding, any text that is not ASCII '
                      r'stops them with "ERROR: invalid multibyte character '
                      rf'for locale"\. {GCONV} ({GROUPS})\.', text)
    if mt:
        return 'use', {'both': by_codeset(mt[2], mt[1])}
    mt = re.fullmatch(rf'!! {FN} give an ERROR on (both machines|the old '
                      r'machine|the new machine) in (\d+) locale\(s\) that '
                      r'databases of an encoding not known here may use '
                      r'\((.+)\): in such a database of more than one byte per '
                      r'character, a glibc that cannot convert the locale\'s own '
                      r'converts no text that is not ASCII \("ERROR: invalid '
                      rf'multibyte character for locale"\)\. {GCONV} ({GROUPS})\.',
                      text)
    if mt:
        return 'use', {'anydb': 'both' if mt[1] == 'both machines'
                       else mt[1].split()[1], 'why': mt[3],
                       'names': by_codeset(mt[4], mt[2])}
    mt = re.fullmatch(rf'!! {FN} give an ERROR on (both machines|the old '
                      r'machine|the new machine) in (\d+) locale\(s\)(.+?)\. '
                      r'In a database of the locale\'s own encoding, any text '
                      r'that is not ASCII stops them there with "ERROR: invalid '
                      rf'multibyte character for locale"\. {GCONV} ({GROUPS})\.',
                      text)
    if mt:
        side = 'both' if mt[1] == 'both machines' else mt[1].split()[1]
        other = {'old': 'new', 'new': 'old'}.get(side)
        out = {'one side': side, 'names': by_codeset(mt[4], mt[2])}
        said = mt[3]
        unknown = re.fullmatch(rf', where it is not known what they do on the '
                               rf'{other} machine \((.+)\)', said) if other else None
        if unknown:
            out['why'] = unknown[1]
        elif other and said == (f' that glibc refused on the {other} machine, '
                                'where no database could use them'):
            out['refused'] = True
        elif side == 'new' and said == ' that the old machine does not have':
            out['absent'] = True
        else:
            raise ReportShapeError(f'unknown error on one side: {text!r}')
        return 'use', out
    mt = re.fullmatch(rf'!! {FN} give an ERROR on (both machines|the new machine) '
                      r'in (\d+) locale\(s\) whose encoding changed from (\S+) on '
                      r'the old machine to (\S+) on the new one: a database of '
                      r'(\S+) made on the old machine opens there under (\S+), and '
                      r'a glibc that cannot convert (\S+) converts no text that '
                      r'is not ASCII \("ERROR: invalid multibyte character for '
                      r'locale"\)(?:; what they do on the old machine is not '
                      rf'known \((.+)\))?\. {GCONV} ({GROUPS})\.', text)
    if mt:
        if not (mt[3] == mt[5] and mt[4] == mt[6] == mt[7]):
            raise ReportShapeError(f'encodings disagree: {text!r}')
        out = {'moved': 'both' if mt[1] == 'both machines' else 'new',
               'from': mt[3], 'to': mt[4], 'names': by_codeset(mt[9], mt[2])}
        if mt[8]:
            out['why'] = mt[8]
        return 'use', out
    mt = re.fullmatch(rf'!! {FN} give an ERROR on part of the text in (\d+) '
                      r'locale\(s\): in a database of the locale\'s own '
                      r'encoding, text holding a byte sequence that such a '
                      r'database accepts and glibc\'s converter of that encoding '
                      r'does not convert stops them with "ERROR: invalid '
                      r'multibyte character for locale"\. (.+?)\. (?:Every such '
                      r'byte sequence is (?:in \S.*|a row of this file)|Pass '
                      r'--characters-to FILE for every such byte sequence)\.', text)
    if mt:
        partly = {}
        for item in mt[2].split('; '):
            it = re.fullmatch(r'(.+?) \((\S+(?:, \S+)*)\)', item)
            if not it:
                raise ReportShapeError(f'unknown part of the text: {item!r}')
            # One encoding on both sides, or one each, joined; then each
            # machine whose file says nothing of it.
            desc, unknown = it[1], []
            while True:
                tail = re.fullmatch(r'(.+), not known on the (old|new) machine', desc)
                if not tail:
                    break
                desc = tail[1]
                unknown.insert(0, tail[2])
            said = dict(lacking_item(x, 'sequence(s)')
                        for x in re.split(r' and (?=\S+: )', desc))
            if unknown:
                said['not known'] = unknown
            for name in it[2].split(', '):
                if name in partly:
                    raise ReportShapeError(f'{name} twice: {text!r}')
                partly[name] = said
        if len(partly) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and names {len(partly)}')
        return 'use', {'partly': partly}
    where = lambda what: (rf'(?:Every such {what} is (?:in \S.*|a row of this '
                          rf'file)|Pass --characters-to FILE for every such {what})\.')
    mt = re.fullmatch(rf'!! {FN} give a wrong result on part of the text in (\d+) '
                      r'locale\(s\): in a database of more than one byte per '
                      r'character, glibc\'s converter of the locale\'s encoding '
                      r'does not turn back into the database\'s bytes the upper '
                      r'case glibc gives some characters, which upper\(\) and '
                      r'initcap\(\) read, or their lower case, which lower\(\), '
                      r'initcap\(\), ILIKE, full-text search and pg_trgm read\. '
                      r'upper\(\), lower\(\) and '
                      r'initcap\(\) do not check that conversion: PostgreSQL 13 '
                      r'to 17 return empty text or other bytes, which ILIKE then '
                      r'compares, while full-text search and pg_trgm stop with '
                      r'"ERROR: conversion from wchar_t to server encoding '
                      r'failed"; on PostgreSQL 18 none checks it, and the result '
                      r'is not defined \(measured: the server process crashed, '
                      r'"ERROR: pfree called with invalid pointer", or the text '
                      r'came back unchanged or as other bytes\)\. (.+?)\. '
                      + where('character'), text)
    if mt:
        back = {}
        for item in mt[2].split('; '):
            it = re.fullmatch(r'(.+?), (none through lower\(\)|(\d+) of them through '
                              r'lower\(\)|through lower\(\): (\d+) on the old machine '
                              r'and (\d+) on the new machine) \((\S+(?:, \S+)*)\)', item)
            if not it:
                raise ReportShapeError(f'unknown back error: {item!r}')
            said = dict(lacking_item(x, 'character(s)')
                        for x in re.split(r' and (?=\S+: )', it[1]))
            said['lower'] = ({'old': int(it[4]), 'new': int(it[5])} if it[4] else
                             int(it[3]) if it[3] else 0)
            for name in it[6].split(', '):
                if name in back:
                    raise ReportShapeError(f'{name} twice: {text!r}')
                back[name] = said
        if len(back) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and names {len(back)}')
        return 'use', {'back error': back}
    mt = re.fullmatch(rf'!! {FN} CHANGE on part of the text in (\d+) locale\(s\): '
                      r'glibc\'s converter of the locale\'s encoding turns some '
                      r'characters their result can hold back into other bytes on '
                      r'the new machine than on the old one\. (.+?)\. '
                      + where('character'), text)
    if mt:
        back = {}
        for item in mt[2].split('; '):
            it = re.fullmatch(r'(\S+): (\d+) character\(s\) \((\S+(?:, \S+)*)\)', item)
            if not it:
                raise ReportShapeError(f'unknown back change: {item!r}')
            for name in it[3].split(', '):
                if name in back:
                    raise ReportShapeError(f'{name} twice: {text!r}')
                back[name] = (it[1], int(it[2]))
        if len(back) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and names {len(back)}')
        return 'use', {'back change': back}
    mt = re.fullmatch(rf'!! {FN}: whether their result converts back to the '
                      r'database\'s bytes is NOT checked in (\d+) locale\(s\)'
                      r'(?:, nor, in (\d+) of UTF-8, whether glibc reads all '
                      r'their text as it is)?: ' + EARLIER_FILES[:-2] + r'\. (\S+: \d+ locale\(s\)'
                      r'(?:; \S+: \d+ locale\(s\))*)\. ' + where('locale'), text)
    if mt:
        counts = dict((k, int(v)) for k, v in re.findall(r'(\S+): (\d+) locale', mt[6]))
        if sum(counts.values()) != int(mt[1]) or (
                mt[2] and int(mt[2]) > counts.get('UTF-8', 0)):
            raise ReportShapeError(f'says {mt[1]}: {text!r}')
        out = {'back earlier': earlier_sides(mt, 3, None, 5), 'counts': counts}
        if mt[2]:
            out['utf8'] = int(mt[2])
        return 'use', out
    mt = re.fullmatch(rf'!! {FN}: whether they give an ERROR on part of the text '
                      r'is NOT known in (\d+) locale\(s\): a file written by an '
                      r'earlier version of locale_order\.py tried one character of '
                      r'their encoding, and glibc\'s converter can refuse others\. '
                      r'(.+)', text)
    if mt:
        untold, rest = {}, mt[2]
        while rest:
            it = re.match(rf'On (both machines|the old machine|the new machine): '
                          rf'({GROUPS})\.(?: |$)', rest)
            if not it or it[1] in untold:
                raise ReportShapeError(f'unknown machines: {rest!r}')
            group = by_codeset(it[2])
            # One machine per locale: a name under two says two things.
            twice = set(group) & {n for g in untold.values() for n in g}
            if twice:
                raise ReportShapeError(f'{sorted(twice)} under two machines: {text!r}')
            untold[it[1]] = group
            rest = rest[it.end():]
        if sum(map(len, untold.values())) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]}: {text!r}')
        return 'use', {'untold': untold}
    mt = re.fullmatch(rf'!! {FN} NOT checked in (\d+) locale\(s\): (.+)', text)
    if mt:
        unknown, rest = {}, mt[2]
        while rest:
            it = re.match(rf'(.+?): ({GROUPS})\.(?: |$)', rest)
            if not it:
                raise ReportShapeError(f'unknown not-checked reasons: {rest!r}')
            for name in by_codeset(it[2]):
                if name in unknown:
                    raise ReportShapeError(f'{name} twice: {text!r}')
                unknown[name] = it[1]
            rest = rest[it.end():]
        if len(unknown) != int(mt[1]):
            raise ReportShapeError(f'says {mt[1]} and names {len(unknown)}')
        return 'use', {'unknown': unknown}
    mt = re.fullmatch(rf'{FN}: text that is not ASCII converts on both '
                      r'machines in (\d+) locale\(s\); in the (\d+) of an '
                      r'encoding of one byte PostgreSQL never converts it in '
                      r'a database of that encoding\.'
                      r'(?: Left out: (\d+) locale\(s\) of an encoding '
                      r'PostgreSQL allows only on the client, so no database '
                      rf'converts text with them \(({GROUPS})\)\.)?'
                      r'(?: The (\d+) locale\(s\) of an encoding PostgreSQL '
                      r'does not know are in the line on a database of another '
                      r'encoding\.)?', text)
    if mt:
        out = {'converts': int(mt[1]), 'one byte': int(mt[2]),
               'left out': by_codeset(mt[4], mt[3]) if mt[3] else {}}
        if mt[5]:
            out['elsewhere'] = int(mt[5])
        return 'use', out
    mt = re.fullmatch(rf'!! {FN} NOT checked: ' + EARLIER_FILES, text)
    if mt:
        return 'use', {'earlier': earlier_sides(mt, 1, None, 3)}
    if re.match(rf'(!! )?{FN}', text):
        raise ReportShapeError(f'unknown line about {FN}: {text!r}')
    return None


WHERE = r'on (?:both machines|the old machine|the new machine)'
LACKING_ITEM = (r'(\S+): (?:(\d+) {u} on both machines|(\d+) {u} on each '
                r'machine, not the same ones|(\d+) {u} on the old machine and '
                r'(\d+) {u} on the new machine(, not all the same)?|(\d+) {u} '
                r'on the (old|new) machine)')


def lacking_item(item, unit):
    """(codeset, {'both' | 'each, not the same' | 'old' | 'new': count}),
    with 'mixed' where neither machine refuses all the other does."""
    it = re.fullmatch(LACKING_ITEM.format(u=re.escape(unit)), item)
    if not it:
        raise ReportShapeError(f'unknown count of what a converter refuses: {item!r}')
    if it[4]:
        got = {'old': int(it[4]), 'new': int(it[5])}
        if it[6]:
            got['mixed'] = True
        return it[1], got
    return it[1], ({'both': int(it[2])} if it[2] else
                   {'each, not the same': int(it[3])} if it[3] else
                   {it[8]: int(it[7])})


def other_tail(rest, names):
    """The line on a database of another encoding, after its names: what
    it says of upper() there, read clause by clause, every clause known."""
    out = {'other encoding': names}
    mt = re.match(r' Nor is whether upper\(\), lower\(\) and ILIKE give an '
                  r'error there known (.+?)\.(?= Measured: | On the new|$)', rest)
    if mt:
        out['upper unknown'] = {}
        pieces = re.split(rf'(?:^|; )({WHERE}): ', mt[1])
        if pieces[0] != '' or len(pieces) % 2 != 1:
            raise ReportShapeError(f'unknown machines not known: {mt[1]!r}')
        for where, groups in zip(pieces[1::2], pieces[2::2]):
            if where in out['upper unknown']:
                raise ReportShapeError(f'{where} twice: {mt[1]!r}')
            out['upper unknown'][where] = by_codeset(groups)
        rest = rest[mt.end():]
    mt = re.match(r' Measured: in a database of more than one byte per '
                  r'character, (.+?); on other text upper\(\), lower\(\) and '
                  r'ILIKE change the database\'s bytes as if they were of the '
                  r'locale\'s encoding, which can give text that is not valid in '
                  r'the database\'s encoding\.', rest)
    if mt:
        out['lacking'], out['whole'] = {}, {}
        for part in mt[1].split(', and '):
            err = re.fullmatch(r'upper\(\), lower\(\) and ILIKE stop with '
                               r'"ERROR: invalid multibyte character for '
                               r'locale" on text holding a byte that glibc\'s '
                               r'converter of the locale\'s encoding does not '
                               r'convert \((.+)\)', part)
            whole = re.fullmatch(rf'glibc\'s (?:converter of (\S+)|converters of '
                                 rf'(\S+(?: and \S+)+)) converts? every byte '
                                 rf'({WHERE})', part)
            if whole and (whole[1] is None) != part.endswith(
                    'convert every byte ' + whole[3]):
                raise ReportShapeError(f'one converter or more: {part!r}')
            if err and not out['lacking']:
                for item in err[1].split('; '):
                    cs, got = lacking_item(item, 'byte(s)')
                    if cs in out['lacking']:
                        raise ReportShapeError(f'{cs} twice: {part!r}')
                    out['lacking'][cs] = got
            elif whole:
                for cs in (whole[1] or whole[2]).split(' and '):
                    if cs in out['whole'] or cs in out['lacking']:
                        raise ReportShapeError(f'{cs} twice: {part!r}')
                    out['whole'][cs] = whole[3]
            else:
                raise ReportShapeError(f'unknown measured clause: {part!r}')
        rest = rest[mt.end():]
    out['other character'] = {}
    while rest:
        mt = re.match(r' On the new machine glibc converts (\d+) byte\(s\) of '
                      r'(\S+) to another character than on the old one, so '
                      r'upper\(\), lower\(\) and ILIKE can give another result '
                      r'there\.', rest)
        if not mt or mt[2] in out['other character']:
            raise ReportShapeError(f'unknown end of the line: {rest!r}')
        out['other character'][mt[2]] = int(mt[1])
        rest = rest[mt.end():]
    if not out['other character']:
        del out['other character']
    return out


def bytes_kind(text):
    """('bytes', data) of the line on databases of another encoding than
    the locale's; None when the line is not about it."""
    mt = re.fullmatch(r'!! Not measured in a database of another encoding: '
                      r'(\d+) locale\(s\) whose encoding PostgreSQL does not '
                      r'know, so that CREATE DATABASE lets a database of any '
                      r'encoding use one, with only a WARNING \("could not '
                      r'determine encoding for locale"\)\. In such a database, a '
                      r'UTF8 one say, PostgreSQL hands the locale the '
                      r'database\'s bytes, not the ones measured here, so neither '
                      r'its order nor whether upper\(\), lower\(\) and ILIKE give '
                      rf'an error there is known\. ({GROUPS})\.', text)
    if mt:
        return 'bytes', {'other encoding': by_codeset(mt[2], mt[1])}
    mt = re.match(r'!! Not measured in a database of another encoding: '
                  r'(\d+) locale\(s\) whose encoding PostgreSQL does not '
                  r'know, so that CREATE DATABASE lets a database of any '
                  r'encoding use one, with only a WARNING \("could not '
                  r'determine encoding for locale"\)\. In such a database, a '
                  r'UTF8 one say, PostgreSQL hands the locale the '
                  r'database\'s bytes, not the ones measured here, so its '
                  rf'order there is not known\. ({GROUPS})\.(?= |$)', text)
    if mt:
        return 'bytes', other_tail(text[mt.end():], by_codeset(mt[2], mt[1]))
    if text.startswith(('!! Not measured in ', '!! NOT checked in ',
                        '!! Not measured in a database of another',
                        'Every such byte', 'Pass --characters-to FILE for every such')):
        raise ReportShapeError(f'unknown line about the bytes: {text!r}')
    return None


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
    comparison, above the list; money, the rest of PostgreSQL's processes,
    upper() and lower(), then what PostgreSQL stores that is not measured,
    after the totals."""
    if kind == 'control':
        return 0
    if voids(kind, d):
        return 1
    return {'money': 2, 'charmaps': 3, 'libc': 4, 'use': 5, 'ctype': 6,
            'bytes': 7}[kind]


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
PARTLY = ('!! Unchanged only as far as measured, because how some of their '
          'neighbours are told apart was not measured at every level: ')
# The lines of the check of each machine's charmaps (charmap_check).
IN_PART = (r"!! Measured in part: (\d+) locale\(s\)\. The machine's character "
           r"set gives a character to byte sequences that are not among the "
           r"strings measured, so a change in where those sort would not "
           r"show; an unchanged one among them is unchanged only as far as "
           r"measured\. (Every such sequence is in:|Pass --characters-to FILE "
           r"for every such sequence\.)")
IN_PART_ITEM = (r'(\S+(?:, \S+)*) \((old|new) machine\): (\S+) gives a '
                r'character to (\d+) sequence\(s\) not measured(?:: |, such as )'
                r'(.+)')
IN_PART_BRIEF = ("!! Measured in part, because the machine's character set gives "
                 "a character to byte sequences not measured: ")
IN_PART_ROW = (r'on the (old|new) machine (\S+) gives a character to (\d+) '
               r'sequence\(s\) not measured: ((?:[0-9A-F]{2}(?: [0-9A-F]{2})*)'
               r'(?:, [0-9A-F]{2}(?: [0-9A-F]{2})*)*)')
NOT_CHECKED_WHOLE = (r"!! NOT checked whether the character sets of (either "
                     r"machine|the (?:old|new) machine) give a character to byte "
                     r"sequences that are not among the strings measured, "
                     r"because (.+)\. Where one does, a locale of that encoding "
                     r"is unchanged only as far as measured\.")
NOT_CHECKED_SOME = (r"!! NOT checked whether their character set gives a "
                    r"character to byte sequences that are not among the strings "
                    r"measured, for (\d+) locale\(s\) of the (old|new) machine:")


def charmaps_unchecked(text, names=None):
    """('either machine' or 'the old machine' ..., reason, None) of a line that
    says a machine's charmaps were not checked, or ('old'/'new', None,
    {name: why}) of one that names the locales, from the report's lines
    under it (names) or, in the summary and the file, from the text after
    its colon."""
    whole = re.fullmatch(NOT_CHECKED_WHOLE, text)
    if whole:
        if names:
            raise ReportShapeError(f'names under a whole machine: {text!r}')
        return (whole[1], whole[2], None)
    some = re.match(NOT_CHECKED_SOME, text)
    if not some:
        raise ReportShapeError(f'unknown charmaps line: {text!r}')
    if names is None:
        rest = text[some.end():]
        names = re.findall(r' (\S+(?:, \S+)*) \((.+?)\)\.(?= |$)', rest)
        if ''.join(f' {a} ({b}).' for a, b in names) != rest:
            raise ReportShapeError(f'unknown charmaps names: {rest!r}')
    elif some.end() != len(text):
        raise ReportShapeError(f'unknown charmaps line: {text!r}')
    out = {}
    for listed, why in names:
        for name in listed.split(', '):
            if name in out:
                raise ReportShapeError(f'{name} not checked twice: {text!r}')
            out[name] = why
    if len(out) != int(some[1]):
        raise ReportShapeError(f'says {some[1]} and names {len(out)}: {text!r}')
    return (some[2], None, out)


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
        s['others_note'] = None
        while True:
            if line() == SUMMARY_FILE_LINE:
                at[0] += 1
                s['characters_file'] = take(r'         (\S.*)')[1]
            if line().startswith('       ' + OTHERS_NOTE):
                s['others_note'] = item(7, 7)
                if s['others_note'] != m.OTHER_STRINGS:
                    fail('unknown note on strings that are not characters')
                break
            if s['characters_file'] is not None:
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
    s['refused'], s['refused_also'] = {}, []
    mt = re.fullmatch(r'     NOT known to be unchanged: (\d+) locale\(s\)',
                      line())
    if mt:
        at[0] += 1
        listed = item(7, 9)
        while listed:
            it = re.fullmatch(r'refused by glibc on (both machines|the (?:old|new) '
                              r'machine), so a database that uses one does not '
                              r'open there: (\S+(?:, \S+)*)', listed)
            if not it:
                break
            for name in names_of(it[2]):
                if name in s['refused']:
                    fail(f'{name} refused twice')
                s['refused'][name] = it[1]
            also = item(9, 11)
            if also is not None:
                if not also.startswith('also, in PostgreSQL: '):
                    fail('expected the PostgreSQL names')
                s['refused_also'].append(parse_also(also))
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
        if (len(s['unmeasured']) + len(s['not_on_new'])
                + len(s['refused'])) != int(mt[1]):
            fail(f'NOT known says {mt[1]}')
    partly = item(5, 8) if line().startswith('     !! Unchanged only') else None
    if partly is not None and not partly.startswith(PARTLY):
        raise ReportShapeError(f'unknown partly-measured line: {partly!r}')
    s['partly'] = names_of(partly[len(PARTLY):]) if partly else []
    in_part = (item(5, 8) if line().startswith('     !! Measured in part')
               else None)
    if in_part is not None and not in_part.startswith(IN_PART_BRIEF):
        raise ReportShapeError(f'unknown measured-in-part line: {in_part!r}')
    s['in_part'] = names_of(in_part[len(IN_PART_BRIEF):]) if in_part else []
    s['charmaps_unchecked'] = []
    while line().startswith('     !! NOT checked whether '):
        s['charmaps_unchecked'].append(charmaps_unchecked(item(5, 8)))
    s['totals'] = item(5, 7)
    if not (s['totals'] or '').startswith('Of the '):
        fail('expected the totals')
    def take():
        at[0] += 1
        return lines[at[0] - 1]
    while re.fullmatch(r'     \S.*', line()) and not line().startswith('     !! Not measured: '):
        text = ctype_listed(take, line, 5, 8, 10, 12) or item(5, 8)
        s['checks'].append(after_totals(check_kind(text), 'summary'))
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
            r['others_note'] = None
            if line().startswith(OTHERS_NOTE):
                r['others_note'] = flat(' '.join(indented(r'\S.*')))
                if r['others_note'] != m.OTHER_STRINGS:
                    fail('unknown note on strings that are not characters')
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

    r['in_part'], r['in_part_file'] = {}, None
    if line().startswith('!! Measured in part'):
        mt = re.fullmatch(IN_PART, paragraph())
        if not mt:
            fail('unknown measured-in-part paragraph')
        if mt[2].startswith('Every'):
            r['in_part_file'] = take(r'  (\S.*)')[1]
        while re.fullmatch(r'  \S.*', line()):
            first = line()
            at[0] += 1
            it = re.fullmatch(IN_PART_ITEM,
                              flat(' '.join([first] + indented(r'    \S.*'))))
            if not it:
                fail('unknown measured-in-part line')
            seqs = re.split(r', | and ', it[5])
            if not all(re.fullmatch(SPACED_BYTES, s) for s in seqs) or (
                    len(seqs) != min(int(it[4]), 3)):
                fail('unknown sequences')
            for name in it[1].split(', '):
                r['in_part'].setdefault(name, []).append(
                    (it[2], it[3], int(it[4]), seqs))
        if len(r['in_part']) != int(mt[1]):
            fail(f'measured in part says {mt[1]} and lists {len(r["in_part"])}')
    r['charmaps_unchecked'] = []
    while line().startswith('!! NOT checked whether '):
        text = paragraph()
        names = []
        while re.fullmatch(r'  \S.*', line()):
            first = line()
            at[0] += 1
            it = re.fullmatch(r'(\S+(?:, \S+)*): (.+)',
                              flat(' '.join([first] + indented(r'    \S.*'))))
            if not it:
                fail('unknown charmaps-not-checked line')
            names.append((it[1], it[2]))
        r['charmaps_unchecked'].append(charmaps_unchecked(text, names))

    r['refused'] = {}
    if line().startswith('!! Refused by glibc'):
        mt = re.fullmatch(
            r'!! Refused by glibc, so a database that uses one does not open: '
            r'(\d+) locale\(s\)\. locale -a lists them, but setlocale\(\) did '
            r'not accept them when that machine was measured: PostgreSQL stops '
            r'a database created with one with "FATAL: database locale is '
            r'incompatible with operating system", and CREATE DATABASE refuses '
            r'it\. Their sort order is not measured either\. glibc does not say '
            r'why\. On RHEL this is what it does with the language packs '
            r'installed as folders \(glibc-langpack-\*\) and glibc-gconv-extra '
            r'not installed, and a machine short of memory gets the same answer; '
            r'install it, or free memory, and measure that machine again\.',
            paragraph())
        if not mt:
            fail('unknown refused paragraph')
        while re.fullmatch(r'  \S.*', line()):
            it = re.fullmatch(r'  (\S+) \((both machines|old machine|new '
                              r'machine)\): (LC_COLLATE and LC_CTYPE|LC_COLLATE|'
                              r'LC_CTYPE) refused', line())
            if not it:
                fail('unknown refused line')
            at[0] += 1
            entry = r['refused'].setdefault(it[1], {'sides': {}, 'also': None})
            if it[2] in entry['sides']:
                fail(f'{it[1]} on the {it[2]} twice')
            entry['sides'][it[2]] = it[3]
            entry['also'] = also() or entry['also']
        if len(r['refused']) != int(mt[1]):
            fail(f'refused says {mt[1]} and lists {len(r["refused"])}')

    r['unmeasured'] = {}
    if line().startswith('!! Could not be measured'):
        count = int(take(r'!! Could not be measured, so NOT known to be '
                         r'unchanged: (\d+) locale\(s\):')[1])
        while re.fullmatch(r'  \S.*', line()):
            mt = re.fullmatch(r'  (\S+) \((both machines|old machine|new '
                              r'machine)\): (.+)', line())
            if mt:
                name, side, reason = mt[1], mt[2], mt[3]
            elif re.fullmatch(r'  \S+: measured over different strings on '
                              r'the two machines .+', line()):
                name, side, reason = (line().split()[0][:-1], 'both machines',
                                      'different strings')
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
              r'(\d+) not measured, (?:([1-9]\d*) refused by glibc, )?(\d+) not '
              r'on the new machine\.')
    r['totals_line'] = mt[0]
    r['totals'] = {'old': int(mt[1]), 'changed': int(mt[2]),
                   'unchanged': int(mt[3]), 'partly': int(mt[4] or 0),
                   'not measured': int(mt[5]), 'refused': int(mt[6] or 0),
                   'not on new': int(mt[7])}
    if r['totals']['refused'] != len(r['refused']):
        fail(f'the totals count {r["totals"]["refused"]} refused and the '
             f'report lists {len(r["refused"])}')
    def take():
        at[0] += 1
        return lines[at[0] - 1]
    while re.fullmatch(r'\S.*', line()) and not line().startswith('!! Not measured: '):
        text = ctype_listed(take, line, 0, 3, 5, 7) or paragraph()
        r['checks'].append(after_totals(check_kind(text), 'report'))
    checks_in_order(r['checks'], 'report')
    if not line().startswith('!! Not measured: '):
        fail('expected the closing "!! Not measured" paragraph')
    r['closing'] = paragraph()
    if line() != END:
        fail('a line after the closing paragraph')
    return r


def number(text):
    return int(text.replace(',', ''))


# A count of strings, as Naming.counted writes it, with the noun the line
# uses for the characters: '3 character(s)', '3 character(s) and 2 other
# string(s)' or '2 other string(s)'. Three groups.
COUNTED = (r'(?:([\d,]+) {0}(?: and ([\d,]+) other string\(s\))?|'
           r'([\d,]+) other string\(s\))')


def counted(mt, at=1):
    """(strings, of them not characters) of a COUNTED match whose first
    group is at."""
    chars, others, only = mt[at], mt[at + 1], mt[at + 2]
    if only:
        return number(only), number(only)
    others = number(others) if others else 0
    return number(chars) + others, others


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
        while rest and re.fullmatch(' ' * indent + r'(?:U\+|bytes )\S.*', rest[0]):
            text += ' ' + rest.pop(0).strip()
        return parse_runs(text)

    def wrapped_line(text, indent):
        """A line that wraps onto lines indented by indent."""
        while rest and re.fullmatch(' ' * indent + r'\S.*', rest[0]):
            text += ' ' + rest.pop(0).strip()
        return text

    first = rest.pop(0)
    if re.fullmatch(r'.+ moved, too many', first) or re.fullmatch(
            r'.+ moved: (?:U\+|bytes ).*', first):
        first = wrapped_line(first, 2)
    many = re.fullmatch(COUNTED.format('characters') + r' moved, too many to '
                        r'list here', first)
    ranged = re.fullmatch(COUNTED.format('characters') + r' moved: '
                          r'((?:U\+|bytes ).*)', first)
    listed = re.fullmatch(COUNTED.format(r'character\(s\)') + r' moved:', first)
    if first == 'no character moved':
        out['moved'] = []
    elif many:
        out['moved_count'], out['moved_others'] = counted(many)
    elif ranged:
        out['moved_runs'] = parse_runs(ranged[4])
        out['moved_count'], out['moved_others'] = counted(ranged)
        if run_count(out['moved_runs']) != out['moved_count']:
            raise ReportShapeError(f'moved says {ranged[1]} and its runs hold '
                                   f'{run_count(out["moved_runs"])}')
    elif listed:
        count, others = counted(listed)
        items, rest = rest[:count], rest[count:]
        out['moved'] = []
        for item in items:
            if not item.startswith('  '):
                raise ReportShapeError(f'unknown moved string: {item!r}')
            out['moved'].append(element(item[2:]))
        if sum(isinstance(e, bytes) for e in out['moved']) != others:
            raise ReportShapeError(f'moved counts {others} other strings: {first!r}')
    else:
        raise ReportShapeError(f'unknown first detail line: {first!r}')
    if out['moved'] is not None:
        out['moved_count'] = len(out['moved'])

    if rest and re.match(r'(all )?[\d,]+ (character|other string)', rest[0]):
        rest.insert(0, wrapped_line(rest.pop(0), 2))
    mt = rest and re.fullmatch(r'(all )?' + COUNTED.format(r'character\(s\)')
                               + r' sort differently against at least one '
                               r'other', rest[0])
    if mt:
        out['all_reversed'] = bool(mt[1])
        out['reversed'], out['reversed_others'] = counted(mt, 2)
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
    many = rest and re.fullmatch(r'([\d,]+) pairs told apart differently, too '
                                 r'many to list here', rest[0])
    listed = rest and re.fullmatch(r'(\d+) pair\(s\) told apart differently:',
                                   rest[0])
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
            out['pairs'].append((element(mt[1]), element(mt[2]), mt[3], mt[4]))
        out['pairs_count'] = len(out['pairs'])
    if rest and rest[0].startswith('  among them '):
        rest.insert(0, wrapped_line(rest.pop(0), 4))
    mt = rest and re.fullmatch(r'  among them ' + COUNTED.format(r'character\(s\)')
                               + r' that did not move(?:: ((?:U\+|bytes ).*)|, too '
                               r'many to list here)', rest[0])
    if mt:
        if out['pairs_count'] is None:
            raise ReportShapeError('strings that did not move, with no pairs')
        rest.pop(0)
        out['still_count'], out['still_others'] = counted(mt)
        if mt[4]:
            out['still'] = parse_runs(mt[4])
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
    many = re.fullmatch(COUNTED.format('characters') + r' moved', first)
    listed = re.fullmatch(COUNTED.format(r'character\(s\)') + r' moved: (.+)',
                          first)
    if first == 'no character moved':
        out['moved'], out['moved_count'] = [], 0
    elif many:
        out['moved_count'], out['moved_others'] = counted(many)
        out['moved_others'] = out['moved_others']
    elif listed:
        out['moved'] = [element(e) for e in listed[4].split(', ')]
        out['moved_count'] = len(out['moved'])
        if (out['moved_count'], sum(isinstance(e, bytes) for e in out['moved'])) \
                != counted(listed):
            raise ReportShapeError(f'phrase counts {listed[0]}: {text!r}')
    else:
        raise ReportShapeError(f'unknown phrase: {text!r}')
    for part in rest:
        many = re.fullmatch(r'([\d,]+) pairs told apart differently', part)
        listed = re.fullmatch(r'(\d+) pair\(s\) told apart differently: (.+)',
                              part)
        reversed_ = re.fullmatch(r'([\d,]+) sort differently against another',
                                 part)
        still = re.fullmatch(r'among them ' + COUNTED.format(r'character\(s\)')
                             + r' that did not move(?:: (.+))?', part)
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
            out['still_count'], out['still_others'] = counted(still)
            if still[4]:
                out['still'] = parse_runs(still[4])
                if run_count(out['still']) != out['still_count']:
                    raise ReportShapeError(f'phrase counts {still[0]}: {text!r}')
        elif many:
            out['pairs_count'] = int(many[1].replace(',', ''))
        elif listed:
            out['pairs'] = [(element(x), element(y)) for x, y in re.findall(
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
        self.assertEqual(t, {'old': 867, 'changed': 14, 'unchanged': 851,
                             'partly': 10, 'not measured': 0, 'refused': 0,
                             'not on new': 2})
        self.assertEqual(t['changed'] + t['unchanged'] + t['not measured']
                         + t['refused'] + t['not on new'], t['old'])
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
        """C.utf8: 135,360 characters and 8,531 strings that are not one
        (the lone bytes CE to FF and charmap sequences that are not UTF-8)."""
        c = self.changes['C.utf8']
        self.assertEqual((c['moved_count'], c['moved_others']), (143891, 8531))
        orya = self.changes['or_IN']
        self.assertEqual(set(orya['names']), {'or_IN', 'or_IN.utf8'})
        self.assertEqual((orya['moved_count'], orya['moved_others']), (50554, 0))
        self.assertIsNotNone(orya['probes'])

    def test_what_could_not_be_measured_is_named(self):
        """Every locale is measured, the four Python has no codec for
        among them (their strings are bytes), and those four are
        unchanged, measured whole."""
        self.assertEqual(self.report['unmeasured'], {})
        self.assertFalse(NO_PYTHON_CODEC & set(self.changes))
        self.assertFalse(NO_PYTHON_CODEC & set(self.report['partly']))

    def test_the_partly_measured_are_named(self):
        """The five EUC-JP locales among them: two pairs of neighbours the
        letters pair does not tell apart, FF next to A2 AF and FE FE next to
        8F A2 C5 (read in the three files). The Chinese locales of GB2312,
        GBK, GB18030, BIG5, BIG5-HKSCS and EUC-TW are not among them: the
        separator keeps each pair from joining with a neighbour's bytes."""
        self.assertEqual(self.report['partly'],
                         {'ja_JP': 2, 'ja_JP.eucjp': 2, 'ja_JP.ujis': 2,
                          'ja_JP.utf8': 3, 'japanese': 2, 'japanese.euc': 2,
                          'ko_KR': 1, 'ko_KR.euckr': 1, 'korean': 1,
                          'korean.euc': 1})

    def test_the_charmaps_are_not_checked_without_them(self):
        """Given the two measurements alone, step 11 says once that neither
        machine's charmaps were checked, and nothing is measured in part."""
        self.assertEqual(self.report['charmaps_unchecked'],
                         [('either machine', m.ALONE_BOTH, None)])
        self.assertEqual(self.report['in_part'], {})

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
        self.assertEqual(t, {'old': 869, 'changed': 4, 'unchanged': 863,
                             'partly': 11, 'not measured': 0, 'refused': 0,
                             'not on new': 2})
        self.assertEqual(t['changed'] + t['unchanged'] + t['not measured']
                         + t['refused'] + t['not on new'], t['old'])
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
        self.assertEqual(self.report['unmeasured'], {})
        self.assertFalse(NO_PYTHON_CODEC & set(self.changes))
        self.assertFalse(NO_PYTHON_CODEC & set(self.report['partly']))

    def test_locales_on_one_machine_only(self):
        self.assertEqual(self.report['not_on_new'],
                         ['aa_ER.utf8@saaho', 'aa_ER@saaho'])
        self.assertEqual(len(self.report['only_new']), 18)


# --- every character, in a file ------------------------------------------------

FINDINGS = ('moved', 'sorts differently', 'pairs changed', 'not compared',
            'told apart differently', 'not decided', 'not measured',
            'not on the new machine', 'unchanged only as far as measured',
            'also in PostgreSQL', 'refused by glibc', 'upper/lower/ILIKE error',
            'upper/lower/ILIKE not checked',
            'LC_CTYPE group', 'LC_CTYPE changed', 'LC_CTYPE not checked',
            'upper/lower/ILIKE group', 'LC_CTYPE converted',
            'upper/lower back not checked', 'upper/lower back group',
            'upper/lower back error', 'upper/lower back changed',
            'measured in part')
# The rows of a locale NOT known to be unchanged, beside the changes.
NOT_KNOWN = ('not measured', 'not on the new machine')
# And of a locale unchanged as far as measured: none of the three changed.
NOT_CHANGED = NOT_KNOWN + ('unchanged only as far as measured', 'refused by glibc')
# Rows about the locale beside its order, which say nothing of whether the
# order changed.
BESIDE_THE_ORDER = ('also in PostgreSQL', 'upper/lower/ILIKE error',
                    'upper/lower/ILIKE not checked', 'LC_CTYPE group',
                    'LC_CTYPE changed', 'LC_CTYPE not checked',
                    'upper/lower/ILIKE group', 'LC_CTYPE converted',
                    'upper/lower back not checked', 'upper/lower back group',
                    'upper/lower back error', 'upper/lower back changed')
SPACED_BYTES = r'((?:[0-9A-F]{2} )*[0-9A-F]{2})'
PARTLY_ROW = r'(\d+) pair\(s\) of neighbours not measured at every level'
MEASURED_ON = {'on both machines',
               'on the old machine; the new one by the characters between',
               'on the new machine; the old one by the characters between'}
CODE = r'U\+([0-9A-F]{4,6})'


def parse_chain(text):
    """'U+0030, U+0B66', 'U+0378..U+0380' or 'bytes 80..C2': the strings of
    a chain of pairs, in the order of the chain, which need not be the
    order a list gives them."""
    out = []
    for part in text.split(', '):
        a, b = parse_run(part)
        if run_key(b) < run_key(a):
            raise ReportShapeError(f'a run that goes down: {text!r}')
        out.extend(run_elements(a, b))
    return out


# One string of a test pair or of a pair the measurement cannot decide.
TOKEN = rf'(?:{CODE}|bytes {HEX})'


def tokens(text):
    """'U+0030 bytes 80' as [0x30, b'\\x80'], refused unless the tokens
    rebuild the text."""
    found = [mt[0] for mt in re.finditer(TOKEN + r'(?= |$)', text)]
    if ' '.join(found) != text:
        raise ReportShapeError(f'unknown strings: {text!r}')
    return [element(t) for t in found]


def parse_test(text):
    """'U+0030 U+0041 / U+0B66 U+0061' as ([0x30, 0x41], [0xB66, 0x61]); a
    string that is not a character is its bytes."""
    halves = text.split(' / ')
    if len(halves) != 2:
        raise ReportShapeError(f'unknown test pair: {text!r}')
    return tuple(tokens(half) for half in halves)


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
    first = re.fullmatch(r'# Step 11, .+ -> .+: every character, other string '
                         r'and pair found, by locale\.(?: (.+))?', lines[0])
    if not first or first[1] not in (None, m.OTHER_STRINGS):
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
    out.others_note = first[1]
    out.ctype_groups = {}
    out.use_groups = {}
    out.back_groups = {}
    out.closing = notes.pop()
    # The charmaps not checked, after the warnings and the control.
    out.charmaps_unchecked = [charmaps_unchecked(note) for note in notes
                              if note.startswith('!! NOT checked whether ')]
    notes = [note for note in notes
             if not note.startswith('!! NOT checked whether ')]
    at = 0
    while at < len(notes) and not re.match(
            r'(Control: |!! Control |Decimals of money|!! Decimals of money|'
            r'Character sets: |PostgreSQL on |!! PostgreSQL|!! The glibc of |This looks at |'
            r'(!! )?upper\(\), lower\(\)|(!! )?Character rules |'
            r'!! Not measured in |!! NOT checked in |'
            r'Every such byte sequence|Pass --characters-to FILE for every such)',
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
        if finding in ('refused by glibc', 'upper/lower/ILIKE not checked'):
            mt = (re.fullmatch(r'(old|new) machine: setlocale\(\) does not '
                               r'accept (LC_COLLATE and LC_CTYPE|LC_COLLATE|'
                               r'LC_CTYPE)', how)
                  if finding == 'refused by glibc' else re.fullmatch(r'\S.*', how))
            if (chars, test, before, now, measured) != ('-',) * 5 or not mt or (
                    ',' in names):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['why'] = (mt[1], mt[2]) if finding == 'refused by glibc' else how
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'upper/lower/ILIKE error':
            mt = re.fullmatch(rf'bytes {SPACED_BYTES}, converted to a wide '
                              r'character as PostgreSQL does', how)
            if (not mt or not (chars == '-' or re.fullmatch(CODE, chars))
                    or test != '-'
                    or ',' in names
                    or (measured, before, now) not in (
                        ('on both machines', 'converts', 'error'),
                        ('on both machines', 'error', 'converts'),
                        ('on both machines', 'error', 'error'),
                        ('on the new machine', 'not measured', 'error'),
                        ('on the old machine', 'error', 'not measured'))):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(chars=[] if chars == '-' else [int(chars[2:], 16)],
                       bytes=mt[1], before=before,
                       now=now, measured=measured)
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'upper/lower/ILIKE group':
            mt = re.fullmatch(r'(upper_lower_ILIKE_group_\d+): the byte '
                              r'sequence\(s\) a database of these locale\(s\) '
                              r'accepts and glibc\'s converter of (\S+) does not '
                              r'convert on a machine, each a row under that name',
                              how)
            if (chars, test, before, now, measured) != ('-',) * 5 or not mt or (
                    mt[1] in out.use_groups):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(label=mt[1], encoding=mt[2])
            out.use_groups[mt[1]] = (tuple(names.split(',')), mt[2])
            out.setdefault(tuple(names.split(',')), []).append(row)
            continue
        if finding == 'upper/lower back not checked':
            if ((chars, test, before, now, measured) != ('-',) * 5 or ',' in names
                    or not re.fullmatch(r'(?:whether glibc reads all the text as '
                                        r'it is, and )?'
                                        r'whether the result converts back to '
                                        r'bytes: not known on (both machines|the '
                                        r'old machine|the new machine)', how)):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['why'] = how
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'upper/lower back group':
            mt = re.fullmatch(r'(upper_lower_back_group_\d+): the character\(s\) a '
                              r'database of these locale\(s\) holds whose upper or '
                              r'lower case glibc\'s converter of (\S+) '
                              r'does not turn back into bytes on a machine, each a '
                              r'row under that name', how)
            if (chars, test, before, now, measured) != ('-',) * 5 or not mt or (
                    mt[1] in out.back_groups):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(label=mt[1], encoding=mt[2])
            out.back_groups[mt[1]] = (tuple(names.split(',')), mt[2])
            out.setdefault(tuple(names.split(',')), []).append(row)
            continue
        if finding == 'upper/lower back error':
            fn = (r'(?:upper|lower)\(\): U\+[0-9A-F]{4,6}(?: \(itself\))?, which '
                  r'glibc\'s converter of \S+ does not turn back into bytes')
            states = ('not turned back', 'turned back', 'not read', 'not measured')
            if (not re.fullmatch(r'upper_lower_back_group_\d+', names)
                    or not re.fullmatch(CODE, chars) or test != '-'
                    or not re.fullmatch(rf'{fn}(?:; {fn})*', how)
                    or before not in states or now not in states
                    or 'not turned back' not in (before, now)
                    or measured not in ('on both machines', 'on the old machine',
                                        'on the new machine')):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(chars=[int(chars[2:], 16)], how=how, before=before, now=now,
                       measured=measured)
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'upper/lower back changed':
            side = rf'(?:{SPACED_BYTES[1:-1]}|refused|no bytes)'
            if (',' in names or not re.fullmatch(CODE, chars)
                    or not re.fullmatch(rf'bytes {side} -> {side}', how)
                    or (test, before, now, measured) != ('-', '-', '-',
                                                         'on both machines')):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(chars=[int(chars[2:], 16)], how=how, measured=measured)
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'LC_CTYPE converted':
            mt = re.fullmatch(rf'{CODE} -> {CODE}', how)
            if (not re.fullmatch(r'LC_CTYPE_group_\d+', names)
                    or not re.fullmatch(rf'bytes {SPACED_BYTES}', chars)
                    or not mt or mt[1] == mt[2]
                    or (test, before, now) != ('-', '-', '-')
                    or measured not in ('on both machines', 'on the old machine',
                                        'on the new machine')):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(chars=chars, how=how, measured=measured)
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'LC_CTYPE group':
            mt = re.fullmatch(r'(LC_CTYPE_group_\d+): the (character|byte)\(s\) '
                              r'whose rules change in these locale\(s\), each a '
                              r'row under that name|(LC_CTYPE_group_\d+): the '
                              r'(byte sequence)\(s\) glibc converts to another '
                              r'character in these locale\(s\), each a row under '
                              r'that name', how)
            if mt and mt[3]:
                mt = (None, mt[3], mt[4])
            if (chars, test, before, now, measured) != ('-',) * 5 or not mt or (
                    mt[1] in out.ctype_groups):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(label=mt[1], what=mt[2])
            out.ctype_groups[mt[1]] = (tuple(names.split(',')), mt[2])
            out.setdefault(tuple(names.split(',')), []).append(row)
            continue
        if finding == 'LC_CTYPE not checked':
            if ((chars, test, before, now, measured) != ('-',) * 5
                    or not re.fullmatch(r'(old machine|new machine|both '
                                        r'machines): .+', how)
                    or ',' in names):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row['why'] = how
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'LC_CTYPE changed':
            wide = chars.startswith('U+')
            part = r'(?:upper|lower): (?:itself|{0}) -> (?:itself|{0})'.format(
                CODE if wide else '[0-9A-F]{2}')
            classes = r'classes: (?:none|[a-z]+(?: [a-z]+)*) -> (?:none|[a-z]+(?: [a-z]+)*)'
            if (not re.fullmatch(r'LC_CTYPE_group_\d+', names)
                    or not re.fullmatch(CODE if wide else r'byte [0-9A-F]{2}', chars)
                    or not re.fullmatch(rf'(?:{part}|{classes})(?:; (?:{part}|{classes}))*', how)
                    or (test, now, measured) != ('-', '-', 'on both machines')
                    or before not in (('classified', 'not classified') if wide
                                      else ('-',))):
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(chars=chars, how=how, before=before)
            out.setdefault((names,), []).append(row)
            continue
        if finding == 'measured in part':
            mt = re.fullmatch(IN_PART_ROW, how)
            if (chars, test, before, now, measured) != ('-',) * 5 or not mt or (
                    len(mt[4].split(', ')) != int(mt[3])) or ',' in names:
                raise ReportShapeError(f'characters line {n}: {line!r}')
            row.update(side=mt[1], charmap=mt[2],
                       seqs=[bytes.fromhex(s) for s in mt[4].split(', ')])
            out.setdefault((names,), []).append(row)
            continue
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
            row['chars'] = tokens(chars)
            if len(row['chars']) != 2 or row['chars'][0] == row['chars'][1]:
                raise ReportShapeError(f'characters line {n}: {line!r}')
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
    # Every group of characters is named by one row of its locales, and every
    # such row has characters under it, of the kind it says.
    labels = {names[0] for names, rows in out.items()
              if any(r['finding'] in ('LC_CTYPE changed', 'LC_CTYPE converted')
                     for r in rows)}
    if labels != set(out.ctype_groups):
        raise ReportShapeError(f'LC_CTYPE groups named {sorted(out.ctype_groups)} '
                               f'and with rows {sorted(labels)}')
    start = {'character': 'U+', 'byte': 'byte ', 'byte sequence': 'bytes '}
    for label, (_, what) in out.ctype_groups.items():
        if any(not r['chars'].startswith(start[what])
               or (r['finding'] == 'LC_CTYPE converted') != (what == 'byte sequence')
               for r in out[(label,)]):
            raise ReportShapeError(f'{label} holds rows of another kind')
    labels = {names[0] for names in out if names[0] in out.use_groups
              or names[0].startswith('upper_lower_ILIKE_group_')}
    if labels != set(out.use_groups) or any(
            r['finding'] != 'upper/lower/ILIKE error' or r['chars']
            for label in labels for r in out[(label,)]):
        raise ReportShapeError(f'groups of upper() named {sorted(out.use_groups)} '
                               f'and with rows {sorted(labels)}')
    labels = {names[0] for names in out if names[0] in out.back_groups
              or names[0].startswith('upper_lower_back_group_')}
    if labels != set(out.back_groups) or any(
            r['finding'] != 'upper/lower back error'
            for label in labels for r in out[(label,)]):
        raise ReportShapeError(f'groups of the conversion back named '
                               f'{sorted(out.back_groups)} and with rows {sorted(labels)}')
    return out


def changes_in(files):
    """The groups of a characters file that changed, without those NOT
    known to be unchanged or unchanged only as far as measured."""
    out = {}
    for names, rows in files.items():
        kinds = {r['finding'] in NOT_CHANGED for r in rows
                 if r['finding'] not in BESIDE_THE_ORDER}
        if not kinds:
            continue        # nothing about the order: no change, and not unchanged
        if len(kinds) != 1:
            raise ReportShapeError(f'{names} is both changed and not')
        if kinds == {False}:
            out[names] = rows
    return out


def characters_of(rows, finding):
    """Every string the rows of one finding name, as a set: a character
    as its code point, another string as its bytes."""
    out = set()
    for row in rows:
        if row['finding'] == finding:
            if 'runs' in row:
                for a, b in row['runs']:
                    out.update(run_elements(a, b))
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
    return {c for a, b in runs for c in run_elements(a, b)}


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
    """parse_also answers, each given as (the names it is printed under,
    answer), merged as ({encoding: {collation}}, {spelling}, {(where, name)}
    for the names PostgreSQL never imports, plus ('superuser spelt',
    spelling) for the spellings given with them; 'this name' and 'these
    names' are read as the names printed above, so groups merged
    differently compare alike)."""
    aliases, spellings, elsewhere = {}, set(), set()
    for group, also in alsos:
        if also:
            for encoding, names in also['aliases'].items():
                aliases.setdefault(encoding, set()).update(names)
            spellings.update(also['spellings'])
            parts = [('anywhere', who) for who in also.get('anywhere', ())]
            for who, spelt in also.get('superuser', ()):
                parts.append(('superuser', who))
                elsewhere.update(('superuser spelt', s) for s in spelt)
            for where, who in parts:
                named = (group if who in ('this name', 'these names')
                         else who.split(', '))
                elsewhere.update((where, n) for n in named)
    return aliases, spellings, elsewhere


def file_of(rows, label='x', warnings=(), closing=m.CLOSING):
    """The text of a characters file holding rows (cells) under one label,
    as compare() writes one."""
    lines = ['# Step 11, a -> b: every character, other string and pair found, '
             'by locale.',
             'locales\tfinding\tcharacters\thow\ttest\tbefore\tnow\tmeasured']
    lines += [label + '\t' + '\t'.join(row) for row in rows]
    lines += ['# ' + text for text in list(warnings) + [closing]]
    lines.append(f'# end: {1 if rows else 0} locale group(s), {len(rows)} row(s)')
    return '\n'.join(lines) + '\n'


LOADED = {}


def measured_as(seq, told, probes):
    """(file, entry) of an order of code points as compare() reads a
    measurement of a UTF-8 locale: its list of strings is the UTF-8 of
    each, in byte order, and the order is places in that list. Two orders
    of the same code points share the list, as two machines' do."""
    strings = sorted(chr(c).encode('utf-8') for c in set(seq))
    place = {s: i for i, s in enumerate(strings)}
    order = [place[chr(c).encode('utf-8')] for c in seq]
    corpus = m.strings_digest(strings)
    digest = m.order_digest([strings[i] for i in order])
    pairs = hashlib.sha256(probes.encode() + b'\n' + told).hexdigest()
    return ({'path': 'x', 'order_data': {digest: m.pack_order(order)},
             'pair_data': {pairs: m.pack(told)},
             'strings_data': {corpus: m.pack_strings(strings)}},
            {'order': digest, 'pairs': pairs, 'chars': len(order),
             'probes': probes, 'corpus': corpus, 'codeset': 'UTF-8'})


def order_numbers(path, name):
    """The measured order of one locale in a file, as the comparison reads
    it: places in the list of strings it was measured over."""
    if path not in LOADED:
        LOADED[path] = m.load(path, m.corpus_digest())[0]
    data = LOADED[path]
    r = data['locales'][name]
    return m.unpacked(data, 'order', r['order'], r['chars'],
                      strings=m.strings_of(data, r['corpus']))


def order_names(path, name):
    """Each place of that list as the outputs name its string: a character
    as its code point, any other as its bytes (element())."""
    data = LOADED[path] if path in LOADED else m.load(path, m.corpus_digest())[0]
    LOADED[path] = data
    r = data['locales'][name]
    strings = m.strings_of(data, r['corpus'])[0]
    cps = m.names_for(strings, r['codeset']).cps
    return [strings[i] if c is None else c for i, c in enumerate(cps)]


def order_of(path, name):
    """The measured order of one locale, each string as the outputs name it."""
    named = order_names(path, name)
    return [named[e] for e in order_numbers(path, name)]


# --- what audit.sh asks of the comparison -------------------------------------

class SummaryForTheAudit(unittest.TestCase):
    """--summary-to and --characters-to: the block audit.sh prints under its
    summary, and the file of every character, other string and pair it
    names.

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
        self.assertEqual(summary['others_note'], report['others_note'])
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
        # C.utf8 counts strings that are not characters: the note on how
        # they are named is under both.
        self.assertEqual(report['others_note'], m.OTHER_STRINGS)
        self.assertEqual(summary['unmeasured'], [])

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
                named = order_names(old, name)
                moved = [named[e] for e in m.moved_characters(
                    order_numbers(old, name), order_numbers(new, name))]
                self.assertEqual(characters_of(self.rows(key, name), 'moved'),
                                 set(moved))
                change = by_name(self.reports[key])[name]
                self.assertEqual(change['moved_count'], len(moved))

    def test_every_character_in_a_reversed_pair_is_listed(self):
        """Counted a second way here: a character is in no reversed pair
        exactly when it stands at the same place in both orders and the
        characters before it are the same. ko_KR: the one that moved and the
        4,888 it moved past; th_TH: 245 characters and the 9 bytes no
        character of TIS-620 has (A0, DB to DE, FC to FF)."""
        for key, old, new, name, count in (
                ('8-9', RHEL8, RHEL9, 'ko_KR.utf8', 4889),
                ('9-10', RHEL9, RHEL10, 'th_TH', 254)):
            with self.subTest(locale=name):
                a, b = order_of(old, name), order_of(new, name)
                # seen[c] is 1 while c is before the place in one order only;
                # apart counts those.
                seen, apart, outside = collections.Counter(), 0, set()
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
        """Every string of or_IN: the 1,112,063 characters and the 10,111
        that are not one (the lone bytes and the charmap sequences)."""
        changes = by_name(self.reports['8-9'])
        self.assertEqual((changes['or_IN']['reversed'],
                          changes['or_IN']['reversed_others'],
                          changes['or_IN']['all_reversed']), (1122174, 10111, True))
        self.assertEqual((changes['C.utf8']['reversed'],
                          changes['C.utf8']['reversed_others'],
                          changes['C.utf8']['all_reversed']), (1122173, 10111, False))
        th = by_name(self.reports['9-10'])['th_TH.utf8']
        self.assertEqual((th['reversed'], th['reversed_others']), (1122173, 10111))

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
                        (names, r['also']) for names, rows in files.items()
                        if set(names) <= set(change['names']) for r in rows
                        if r['finding'] == 'also in PostgreSQL'),
                        also_of([(change['names'], change['also'])]))
                for name, entry in report['unmeasured'].items():
                    self.assertEqual(also_of(
                        ((name,), r['also']) for r in files[(name,)]
                        if r['finding'] == 'also in PostgreSQL'),
                        also_of([((name,), entry['also'])]))
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
        self.assertEqual((change['still'], change['still_count'],
                          change['still_others']), (None, 846975, 1569))

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

    def test_measured_over_different_strings(self):
        """Another list of strings on one machine: its own list, under its
        own digest, as a machine would write it."""
        def edit(d):
            d['strings_data']['0' * 64] = d['strings_data'][
                d['locales']['ko_KR']['corpus']]
            d['locales']['ko_KR']['corpus'] = '0' * 64
        path = os.path.join(self.tmp, 'different-characters.tsv')
        rc, out, err = run_compare(RHEL9, self.variant(edit, reseal=True),
                                   '--characters-to', path)
        self.assertEqual((rc, err), (0, ''))
        r = parse_report(out)
        with open(path, encoding='utf-8') as f:
            rows = parse_characters(f.read())
        self.assertEqual(rows[('ko_KR',)][0]['why'],
                         'measured over different strings on the two machines')
        self.assertEqual(r['unmeasured']['ko_KR']['why'],
                         {'both machines': 'different strings'})
        self.assertEqual(by_name(r), {})

    def test_each_kind_of_the_run_is_said_alike_in_the_three(self):
        """One edited new machine with each kind of the run: three warnings
        (both machines run one build, and each tag names no release), a
        locale measured only in part, two that could not be measured there,
        one missing there, and the closing paragraph. The report, the summary
        and the file say each alike, every warning included. And the file
        gives the missing one the names PostgreSQL gave it on the old
        machine, where the cluster created its collations."""
        def edit(d):
            d['locales']['en_US']['partly'] = 5
            d['locales']['ko_KR.utf8'] = {'error': 'cannot be selected: test',
                                          'codeset': 'UTF-8'}
            d['locales']['fr_FR.utf8'] = {'error': 'cannot be selected: test',
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
        self.assertEqual(set(report['unmeasured']), {'ko_KR.utf8', 'fr_FR.utf8'})
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

class StoredStrings(unittest.TestCase):
    """stored_strings(): what a database can hold with a locale, the one
    place that decides what is measured (T4 step 4c).

    Freezes a list that drops the lone bytes or the charmaps' sequences
    (each a string a database holds that no order would place), an encoding
    of one byte measured as another, and an encoding of more than one byte
    Python has no codec for measured over nothing instead of refused."""

    def setUp(self):
        # A list built in another test, under a patched CODEPOINTS, would be
        # read here (STRINGS keeps them per process).
        patch = unittest.mock.patch.object(m, 'STRINGS', {})
        patch.start()
        self.addCleanup(patch.stop)

    def test_utf8_holds_every_character_the_lone_bytes_and_the_charmaps(self):
        with unittest.mock.patch.object(m, 'CODEPOINTS', [0x41, 0xE9, 0x10FFFF]), \
                unittest.mock.patch.object(m, 'UTF8_BYTES', None):
            strings, digest, packed = m.stored_strings('UTF-8', 6)
        want = sorted({b'A', b'\xc3\xa9', b'\xf4\x8f\xbf\xbf'} | set(m.LONE_BYTES)
                      | set(m.charmap_only('UTF-8')))
        self.assertEqual(strings, want)
        self.assertEqual(len(strings), 3 + 128 + 9983)
        self.assertEqual(digest, m.strings_digest(strings))
        self.assertEqual(m.unpack_strings(zlib.decompress(base64.b64decode(packed))),
                         strings)

    def test_euc_holds_what_postgresql_accepts_and_the_lone_bytes(self):
        for codeset in m.EUC_CODESETS:
            with self.subTest(codeset=codeset):
                strings = m.stored_strings(codeset, 4)[0]
                self.assertEqual(strings, sorted(
                    set(m.accepted_sequences(codeset)) | set(m.LONE_BYTES)
                    | set(m.charmap_only(codeset))))
        # EUC-TW's plane 15, which PostgreSQL does not accept and glibc's
        # charmap defines (backlog 16.34).
        self.assertIn(b'\x8e\xaf\xa1\xa1', m.stored_strings('EUC-TW', 4)[0])
        self.assertIn(b'\xff', m.stored_strings('EUC-JP', 3)[0])

    def test_one_byte_per_character_is_every_byte(self):
        """By MB_CUR_MAX, whatever the encoding: one Python knows, one it
        does not, and C's."""
        for codeset in ('ISO-8859-1', 'TIS-620', 'ARMSCII-8', 'ANSI_X3.4-1968'):
            with self.subTest(codeset=codeset):
                self.assertEqual(m.stored_strings(codeset, 1)[0],
                                 [bytes([b]) for b in range(1, 0x100)])

    def test_client_only_is_the_codec_the_lone_bytes_and_the_charmap(self):
        strings = m.stored_strings('BIG5', 2)[0]
        codec = set(m.corpus_for('BIG5')[1])
        self.assertTrue(codec <= set(strings))
        self.assertTrue(set(m.LONE_BYTES) <= set(strings))
        lacking = set(m.charmap_only('BIG5')) - codec
        self.assertTrue(lacking)
        self.assertTrue(lacking <= set(strings))

    def test_every_multibyte_charmap_with_a_codec_is_listed(self):
        """What Python's codec lacks and glibc's charmap gives a character:
        EUC-JISX0213 AE A1, SHIFT_JISX0213 87 9F, JOHAB D9 E8, WINDOWS-31J's
        373 (backlog 16.34, the whole family)."""
        for codeset, s in (('EUC-JISX0213', b'\xae\xa1'),
                           ('SHIFT_JISX0213', b'\x87\x9f'),
                           ('JOHAB', b'\xd9\xe8')):
            with self.subTest(codeset=codeset):
                self.assertNotIn(s, m.corpus_for(codeset)[1])
                self.assertIn(s, m.stored_strings(codeset, 2)[0])
        lacking = set(m.charmap_only('WINDOWS-31J')) - set(m.corpus_for('WINDOWS-31J')[1])
        self.assertEqual(len(lacking), 373)
        self.assertTrue(lacking <= set(m.stored_strings('WINDOWS-31J', 2)[0]))

    def test_more_than_one_byte_with_a_codec_and_no_list_is_refused(self):
        """HZ: Python reads it, CHARMAP_ONLY has no list of it, so what
        glibc reads as a character there is not known."""
        self.assertIsNotNone(m.corpus_for('HZ'))
        self.assertIsNone(m.stored_strings('HZ', 2))
        result, data = m.measured('HZ', 2)
        self.assertIsNone(data)
        self.assertEqual(result, {'error': 'cannot be measured: its encoding, HZ, '
                                  'is not among those whose character sets this '
                                  'script lists the sequences of (CHARMAP_ONLY), so '
                                  'the strings PostgreSQL would compare cannot all '
                                  'be built'})

    def test_more_than_one_byte_with_no_codec_is_refused(self):
        self.assertIsNone(m.stored_strings('NO-SUCH-CODESET', 2))
        result, data = m.measured('NO-SUCH-CODESET', 2)
        self.assertIsNone(data)
        self.assertEqual(result, {'error': 'cannot be measured: Python has no '
                                  'converter for NO-SUCH-CODESET, the encoding '
                                  'of this locale, so the bytes PostgreSQL would '
                                  'compare cannot be built'})

    def test_a_string_that_cannot_be_packed_raises(self):
        """Longer than four bytes, or holding a NUL: a number could not give
        it back, so packing refuses it rather than store another string."""
        for bad in (b'\x8e\xa1\xa1\xa1\xa1', b'A\x00B', b''):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    m.pack_strings([b'A', bad])


class ListsOfStringsRead(Variants):
    """A list of strings in a file, read back: the comparison names each
    place of an order from it, so a list it cannot read whole is a refusal,
    never a list that names other strings."""

    NAMES = ('C', 'POSIX', 'sv_SE', 'zh_TW.euctw')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.texts[RHEL9] = trimmed(cls.texts[RHEL9], cls.NAMES)

    def raw(self, numbers):
        return zlib.decompress(base64.b64decode(m.pack_order(numbers)))

    def test_a_list_back(self):
        strings = [b'A', b'\x80', b'\xc3\xa9', b'\xf0\xab\xa0\xc0']
        self.assertEqual(m.unpack_strings(zlib.decompress(base64.b64decode(
            m.pack_strings(strings)))), strings)

    def test_what_is_not_a_list_is_refused(self):
        a, b = 0x41000000, 0x42000000
        for case, numbers in (('empty', []), ('starts at 0', [0, a]),
                              ('the same twice', [a, a]), ('going down', [b, a]),
                              ('past four bytes', [a, 1 << 32]),
                              ('a NUL inside', [a, 0x41004200])):
            with self.subTest(case=case):
                self.assertIsNone(m.unpack_strings(self.raw(numbers)))

    def refused(self, edit, name='sv_SE'):
        """What load(), then reading name's list and order as the
        comparison reads them, refuses the edited file with."""
        path = self.variant(edit, reseal=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            d = m.load(path, m.corpus_digest())[0]
            r = d['locales'][name]
            strings = m.strings_of(d, r['corpus'])
            m.unpacked(d, 'order', r['order'], r['chars'], strings=strings)
        return stderr.getvalue()

    def test_a_file_with_no_lists_is_refused(self):
        self.assertIn('holds no measured orders; it is not a whole measurement',
                      self.refused(lambda d: d.pop('strings_data')))

    def test_a_locale_whose_list_is_missing_is_refused(self):
        """zh_TW.euctw is the one locale measured over EUC-TW's list."""
        def edit(d):
            d['strings_data'].pop(d['locales']['zh_TW.euctw']['corpus'])
        self.assertIn('what was measured for zh_TW.euctw is missing',
                      self.refused(edit))

    def test_a_list_that_does_not_match_its_digest_is_refused(self):
        def edit(d):
            key = d['locales']['sv_SE']['corpus']
            d['strings_data'][key] = m.pack_strings([b'A', b'B'])
        self.assertIn('a measured list of strings does not match its digest',
                      self.refused(edit))

    def test_an_order_past_its_list_is_refused(self):
        """A place below 0 or past the last string names no string. -1
        put where the last string is would name that same string, read
        from the end, and the order's digest would match."""
        last = lambda seq: seq.index(len(seq) - 1)
        for case, change in (
                ('below', lambda seq: seq[:last(seq)] + [-1] + seq[last(seq) + 1:]),
                ('past', lambda seq: seq[:-1] + [len(seq)])):
            def edit(d, change=change):
                r = d['locales']['sv_SE']
                seq = m.unpack_order(zlib.decompress(base64.b64decode(
                    d['order_data'][r['order']])))
                d['order_data'][r['order']] = m.pack_order(change(seq))
            with self.subTest(case=case):
                self.assertIn('a measured order record does not match its digest',
                              self.refused(edit))


class OrderOnTheMachine(unittest.TestCase):
    """measured(): the order and how neighbours are told apart, over the 255
    bytes of an encoding of one byte. glibc's strcoll and strxfrm are
    doubles here, a collation of three levels (letter, accent, case) over
    a few bytes, every other byte its own letter after them.

    Freezes strxfrm's order taken where strcoll's differs, a strcoll that
    gives no one order measured as if it did, and a level's pair taken from
    the neighbours kept as anything but its bytes."""

    # byte: (letter, accent, case)
    LETTERS = {b'a': (1, 0, 0), b'A': (1, 0, 1), b'\xe1': (1, 1, 0),
               b'b': (2, 0, 0), b'B': (2, 0, 1)}

    def weights(self, s):
        out = [self.LETTERS.get(bytes([c]), (100 + c, 0, 0)) for c in s]
        return tuple(tuple(w[k] for w in out) for k in range(3))

    def lib(self, key=None, strcoll=None):
        def coll(a, b):
            x, y = self.weights(a), self.weights(b)
            return (x > y) - (x < y)

        def xfrm(buf, s, n):
            k = (key or self.key)(s)
            if n > len(k):
                ctypes.memmove(buf, k, len(k))
            return len(k)
        return unittest.mock.Mock(strcoll=strcoll or coll, strxfrm=xfrm)

    def key(self, s):
        """strxfrm's key as glibc writes it: each level's weights, two bytes
        each and none of them 00 or 01, then byte 01 (ld-collate.c,
        output_weight), so that keys compare as the weights do and
        first_level() reads the levels."""
        return b'\x01'.join(b''.join(bytes([2 + w // 250, 2 + w % 250])
                                      for w in level)
                             for level in self.weights(s))

    def measure(self, lib, codeset='ARMSCII-8'):
        with unittest.mock.patch.object(m, 'libc', lambda: lib):
            return m.measured(codeset, 1)

    def expected_order(self):
        return sorted(m.ONE_BYTE_STRINGS, key=lambda s: (self.weights(s), s))

    def test_the_order_is_strcolls(self):
        result, data = self.measure(self.lib())
        self.assertEqual(result['order'], m.order_digest(self.expected_order()))
        self.assertEqual(result['chars'], 255)

    def test_where_strxfrm_differs_strcoll_sorts(self):
        """strxfrm's key puts 'b' first; strcoll does not. The order is
        strcoll's, ties broken by the bytes, and how neighbours are told
        apart is measured on it."""
        def key(s):
            return b'\x00' if s == b'b' else self.key(s)
        result, data = self.measure(self.lib(key=key))
        self.assertNotIn('error', result)
        right = self.measure(self.lib())[0]
        self.assertEqual((result['order'], result['pairs']),
                         (right['order'], right['pairs']))

    def test_a_strcoll_with_no_one_order_is_refused(self):
        """strcoll puts x after y and y after x: whatever order the bytes
        are sorted in, those two neighbours disagree, and no order is
        strcoll's. (A cycle over three strings that no two neighbours show
        is beyond what checking neighbours can see: measured() takes strcoll
        to be transitive.)"""
        def coll(a, b):
            if {a, b} == {b'x', b'y'}:
                return 1
            x, y = self.weights(a), self.weights(b)
            return (x > y) - (x < y)
        result, data = self.measure(self.lib(strcoll=coll))
        self.assertIsNone(data)
        self.assertRegex(result['error'], r'^strcoll puts [1-9]\d* neighbouring '
                         r'pair\(s\) of its own order the other way round, so '
                         r'it gives no one order to measure$')

    def test_a_pair_from_the_neighbours_is_kept_as_bytes(self):
        """ARMSCII-8 has no codec: the accent candidates cannot be spelled,
        and the accent pair is the first neighbours that differ by an
        accent and not by case, A and E1, as bytes."""
        result, data = self.measure(self.lib())
        self.assertEqual(result['probes'], '61.62> 41.E1> 61.41>')
        self.assertEqual(result['partly'], 0)

    def test_a_tie_strxfrm_puts_against_byte_order(self):
        """strcoll calls x and y equal, strxfrm puts y first: PostgreSQL
        breaks the tie by the bytes, so x first, and the pair is a tie."""
        def coll(a, b):
            if {a, b} == {b'x', b'y'}:
                return 0
            x, y = self.weights(a), self.weights(b)
            return (x > y) - (x < y)

        def key(s):
            return self.key(b'x') + (b'\x00' if s == b'y' else b'\x01') \
                if s in (b'x', b'y') else self.key(s)
        result, data = self.measure(self.lib(key=key, strcoll=coll))
        self.assertNotIn('error', result)
        order = self.expected_order()
        order.remove(b'y')
        order.insert(order.index(b'x') + 1, b'y')
        self.assertEqual(result['order'], m.order_digest(order))

    def test_measure_one_reads_mb_cur_max(self):
        """measure_one asks glibc's MB_CUR_MAX under the locale and builds
        the strings with it: BIG5 with 2 is its own list, not the 255
        bytes; 0 is not read, and nothing is measured."""
        def run(width):
            def mb():
                return width
            lib = types.SimpleNamespace(
                strcoll=self.lib().strcoll, strxfrm=self.lib().strxfrm,
                **{'__ctype_get_mb_cur_max': mb})
            with unittest.mock.patch.object(m, 'libc', lambda: lib), \
                    unittest.mock.patch.object(m.locale, 'setlocale',
                                               lambda c, n=None: n or 'C'), \
                    unittest.mock.patch.object(m.locale, 'nl_langinfo',
                                               lambda item: 'BIG5'):
                return m.measure_one('zh_TW')
        name, result, data = run(2)
        self.assertEqual(result['chars'], len(m.stored_strings('BIG5', 2)[0]))
        self.assertGreater(result['chars'], 255)
        name, result, data = run(0)
        self.assertIsNone(data)
        self.assertEqual(result['error'], 'cannot be measured: its encoding could '
                         'not be read, so neither could the bytes PostgreSQL '
                         'would compare')

    def test_the_encoder_spells_bytes(self):
        self.assertEqual(m.encoder('UTF-8')(chr(0xE1)), b'\xc3\xa1')
        self.assertEqual(m.encoder('ISO-8859-1')(chr(0xE1)), b'\xe1')
        self.assertEqual(m.encoder('ARMSCII-8')('a'), b'a')
        with self.assertRaises(UnicodeError):
            m.encoder('ARMSCII-8')(chr(0xE1))


class SeparatorBetweenTheBytes(unittest.TestCase):
    """apart() puts SEPARATOR between a neighbour and a pair, and a pair is
    taken for a level only where glibc decides its own test at the level at
    which its two strings differ (tells_itself, first_level).

    The double reads, as findidx does, the longest string of WEIGHTS at each
    place (weight.h:63-95@2.39), and any other byte as an element of its own;
    a weight of 0 is IGNORE at that level, and byte 01 weighs nothing at the
    first three levels, as U+0001 does in iso14651_t1_common. Its strxfrm key
    holds each level's weights (from 2 up) followed by byte 01, as
    ld-collate.c writes them, and strcoll compares those keys.

    Freezes the lone byte A1 put before A8 A2 in GB2312, which glibc read as
    A1 A8 and then A2 before the separator (review round 3, 2026-10-27), and
    the pair km_KH took for its accents with one separator, U+0005 and
    U+17CE, which only level 4 told apart beside each other (measured on the
    three RHEL fixtures, 2026-10-28)."""

    # GB2312: a, b, A, B, A8 A2 (a with acute), A1 A8 and three lone bytes,
    # weights at levels 1, 2, 3 and 4.
    WEIGHTS = {b'a': (1, 1, 1, 1), b'\xa8\xa2': (1, 2, 1, 2),
               b'A': (1, 1, 2, 3), b'b': (2, 1, 1, 4), b'B': (2, 1, 2, 5),
               b'\xa1\xa8': (50, 1, 1, 6), b'\xa1': (60, 1, 1, 7),
               b'\xa2': (61, 1, 1, 8), b'\xa8': (62, 1, 1, 9),
               b'\x01': (0, 0, 0, 10)}
    STRINGS = sorted(set(WEIGHTS) - {b'\x01'})

    def key(self, s):
        out, i = [], 0
        while i < len(s):
            k = 2 if s[i:i + 2] in self.WEIGHTS else 1
            out.append(self.WEIGHTS.get(s[i:i + k], (100 + s[i], 1, 1, 11)))
            i += k
        return b'\x01'.join(bytes(w[level] + 1 for w in out if w[level])
                            for level in range(4))

    def coll(self, a, b):
        x, y = self.key(a), self.key(b)
        return (x > y) - (x < y)

    def measure(self):
        strings = (self.STRINGS, m.strings_digest(self.STRINGS),
                   m.pack_strings(self.STRINGS))

        def xfrm(buf, s, n):
            k = self.key(s)
            if n > len(k):
                ctypes.memmove(buf, k, len(k))
            return len(k)
        lib = unittest.mock.Mock(strcoll=self.coll, strxfrm=xfrm)
        with unittest.mock.patch.object(m, 'libc', lambda: lib), \
                unittest.mock.patch.object(m, 'stored_strings',
                                           lambda codeset, width: strings):
            return m.measured('GB2312', 2)

    def test_the_lone_byte_before_the_accent_pair_is_measured(self):
        """The order is a, A, A8 A2, b, B, A1 A8, A1, A2, A8. The accent pair
        is a and A8 A2; put after A1 with the separator between, it is read
        as itself, so every neighbour pair is measured at every level: those
        of different letters before the pair at each level (0), a and A,
        and b and B, apart by case only, A and A8 A2 by an accent."""
        result, data = self.measure()
        self.assertEqual(result['probes'], '61.62> 61.A8A2> 61.41>')
        order = [b'a', b'A', b'\xa8\xa2', b'b', b'B', b'\xa1\xa8', b'\xa1',
                 b'\xa2', b'\xa8']
        self.assertEqual(result['order'], m.order_digest(order))
        told = zlib.decompress(base64.b64decode(data['pairs']))
        levels = [tuple((v >> s) & 3 for s in (1, 3, 5)) for v in told]
        self.assertEqual(levels, [(2, 2, 0), (2, 0, 0), (0, 0, 0), (2, 2, 0),
                                  (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)])
        self.assertEqual(result['partly'], 0)

    def test_apart_puts_the_separator_between_on_each_side(self):
        """x, the separator and hi against y, the separator and lo; from the
        end, hi, the separator and x against lo, the separator and y."""
        asked = []

        def strcoll(a, b):
            asked.append((a, b))
            return -1
        lo, hi, sep = b'L', b'H', m.SEPARATOR
        self.assertEqual(sep, b'\x01\x01')
        self.assertEqual(m.apart(strcoll, (lo, hi, False), b'x', b'y'), 0)
        self.assertEqual(m.apart(strcoll, (lo, hi, True), b'x', b'y'), 0)
        self.assertEqual(asked, [(b'x' + sep + hi, b'y' + sep + lo),
                                 (hi + sep + b'x', lo + sep + b'y')])

    def test_the_first_level_is_read_from_the_key(self):
        """0, 1 and 2 for the first three levels, 3 for any after them,
        None for equal keys."""
        def key(s):
            return s
        self.assertEqual(m.first_level(key, b'\x02\x01\x02', b'\x03\x01\x02'), 0)
        self.assertEqual(m.first_level(key, b'\x02\x01\x02', b'\x02\x01\x03'), 1)
        self.assertEqual(m.first_level(key, b'\x02\x01\x01\x02',
                                       b'\x02\x01\x01\x03'), 2)
        self.assertEqual(m.first_level(key, b'\x02\x01\x01\x01\x02\x01\x05',
                                       b'\x02\x01\x01\x01\x02\x01\x06'), 3)
        self.assertEqual(m.first_level(key, b'\x02\x01', b'\x02\x01'), None)

    # km_KH's shape: U weighs nothing at levels 1 to 3, K has an accent;
    # V has one too, a lesser one. All three weigh at level 4.
    KM = {b'U': (0, 0, 0, 5), b'K': (0, 7, 2, 9), b'V': (0, 3, 2, 4),
          b'a': (10, 1, 1, 1), b'b': (11, 1, 1, 2), b'A': (10, 1, 2, 3),
          b'\x01': (0, 0, 0, 11)}

    def km_key(self, s):
        return b'\x01'.join(bytes(self.KM[bytes([c])][level] + 1 for c in s
                                  if self.KM[bytes([c])][level])
                            for level in range(4))

    def km_coll(self, a, b):
        x, y = self.km_key(a), self.km_key(b)
        return (x > y) - (x < y)

    def test_a_pair_told_apart_only_at_a_later_level_is_refused(self):
        """U and K differ at level 2, but beside each other they hold the
        same weights there in either order, and only level 4 parts them:
        refused. V and K beside each other are parted at level 2: taken. And
        a pair that does not sort lo's side first is refused too."""
        self.assertLess(self.km_coll(b'U\x01\x01K', b'K\x01\x01U'), 0)
        self.assertFalse(m.tells_itself(self.km_coll, self.km_key,
                                        (b'U', b'K', False)))
        self.assertTrue(m.tells_itself(self.km_coll, self.km_key,
                                       (b'V', b'K', False)))
        self.assertFalse(m.tells_itself(self.km_coll, self.km_key,
                                        (b'K', b'V', False)))

    def test_derived_probe_asks_tells_itself(self):
        """The call site: neighbours U, K, the only pair below the letters,
        are refused, and since glibc's keys put them between the letters
        and case, the level cannot be told (None), not shown absent.
        Neighbours V, K are taken. The control: a and A, which differ by
        case alone, are no accent pair, and with them alone the level is
        absent."""
        letters, case = (b'a', b'b', False), (b'a', b'A', False)
        told = bytes([2 << 1])     # the letters pair does not part them
        self.assertIsNone(m.derived_probe(1, [letters, None, case], told,
                                          [b'U', b'K'], [0, 1], self.km_coll,
                                          self.km_key))
        self.assertEqual(m.derived_probe(1, [letters, None, case], told,
                                         [b'V', b'K'], [0, 1], self.km_coll,
                                         self.km_key), (b'V', b'K', False))
        self.assertIs(m.derived_probe(1, [letters, None, case], told,
                                      [b'a', b'A'], [0, 1], self.km_coll,
                                      self.km_key), m.ABSENT)
        # Any such pair makes it None, not the last one walked: U, K first,
        # a, A (not between) after them.
        self.assertIsNone(m.derived_probe(
            1, [letters, None, case], bytes([2 << 1, 0, 2 << 1]),
            [b'U', b'K', b'a', b'A'], [0, 1, 2, 3], self.km_coll, self.km_key))
        # A refused pair that differs as letters, as strong as the letters
        # pair, is not between: U and a, and the level is absent.
        self.assertIs(m.derived_probe(1, [letters, None, case], told,
                                      [b'U', b'a'], [0, 1], self.km_coll,
                                      self.km_key), m.ABSENT)

    def test_derived_probe_skips_what_a_stronger_pair_parts(self):
        """Neighbours a, b differ as letters (their record at the letters
        level is 0) and come first; the accent pair is V, K after them."""
        letters, case = (b'a', b'b', False), (b'a', b'A', False)
        told = bytes([0, 0, 2 << 1])
        self.assertEqual(m.derived_probe(1, [letters, None, case], told,
                                         [b'a', b'b', b'V', b'K'],
                                         [0, 1, 2, 3], self.km_coll,
                                         self.km_key), (b'V', b'K', False))

    def test_a_pair_glibc_keys_alike_does_not_tell_itself(self):
        """strcoll parts the two strings, their strxfrm keys do not (as
        where the two disagree, th_TH on glibc 2.39): refused."""
        def coll(a, b):
            return (a > b) - (a < b)
        self.assertFalse(m.tells_itself(coll, lambda s: b'\x02',
                                        (b'x', b'y', False)))

    def test_choose_probes_asks_tells_itself(self):
        """The call site: the candidate U, K is not taken for the accents,
        V, K is."""
        def encode(text):
            return text.encode('ascii')
        for accent, want in ((('U', 'K'), None), (('V', 'K'), (b'V', b'K', False))):
            with self.subTest(accent=accent), unittest.mock.patch.object(
                    m, 'CANDIDATES', ((('a', 'b'),), (accent,), (('a', 'A'),))):
                chosen = m.choose_probes(encode, self.km_coll, self.km_key)
                self.assertEqual(chosen, [(b'a', b'b', False), want,
                                          (b'a', b'A', False)])


class NamesOfStrings(unittest.TestCase):
    """How the outputs name a string: as a character exactly where the tool
    named that character before (corpus_for), any other by its bytes.

    Freezes two strings named alike, a string named as a character Python's
    codec would write otherwise, a run that joins a character to bytes or
    reads as covering strings it does not hold, and a chain listed out of
    its order, which would name pairs that are not neighbours."""

    def names(self, strings, codeset):
        return m.Naming(m.Names(sorted(strings), codeset))

    def test_a_character_where_the_tool_named_one(self):
        for codeset, s, cp in (
                ('UTF-8', b'\xc3\xa9', 0xE9), ('UTF-8', b'\x80', None),
                ('UTF-8', b'\xf0\xab\xa0\xc0', None), ('ISO-8859-1', b'\xe9', 0xE9),
                ('TIS-620', b'\xa0', None), ('TIS-620', b'\x80', 0x80),
                # Python cannot read A4 D4 alone and writes U+3164 so.
                ('EUC-KR', b'\xa4\xd4', 0x3164),
                # PostgreSQL's U+327E; Python's codec has nothing there.
                ('EUC-KR', b'\xa2\xe8', None),
                # Read as '~', which Python writes 7E: not this string.
                ('EUC-JP', b'\x8f\xa2\xb7', None), ('EUC-JP', b'~', 0x7E),
                ('ARMSCII-8', b'a', 0x61), ('ARMSCII-8', b'\xae', None),
                # No codec, and glibc's VISCII has letters at 02 and so on.
                ('VISCII', b'\x02', None), ('VISCII', b'a', None)):
            with self.subTest(codeset=codeset, string=s):
                self.assertEqual(m.Names([s], codeset).code_point(s), cp)

    def test_no_two_strings_are_named_alike(self):
        """Over each list the fixtures' locales are measured over."""
        for codeset, width in (('EUC-JP', 3), ('TIS-620', 1), ('ARMSCII-8', 1)):
            with self.subTest(codeset=codeset):
                strings = m.stored_strings(codeset, width)[0]
                naming = m.Naming(m.Names(strings, codeset))
                tokens = [naming.token(e) for e in range(len(strings))]
                self.assertEqual(len(set(tokens)), len(tokens))

    def test_runs(self):
        strings = [b'A', b'B', b'C', b'\x80', b'\x81', b'\x82',
                   b'\xf0\xab\xa0\xc0', b'\xf0\xab\xa0\xc1', b'\xf4\x8f\xbf\xbf']
        nm = self.names(strings, 'UTF-8')
        everything = range(len(strings))
        self.assertEqual(nm.runs_text(nm.runs(everything)),
                         'U+0041..U+0043, U+10FFFF, bytes 80..82, '
                         'bytes F0 AB A0 C0..F0 AB A0 C1')
        at = {s: i for i, s in enumerate(nm.names.strings)}
        some = [at[b'A'], at[b'C'], at[b'\x80'], at[b'\x82']]
        self.assertEqual(nm.runs_text(nm.runs(some)),
                         'U+0041, U+0043, bytes 80, bytes 82')
        self.assertTrue(nm.others)

    def test_a_chain_keeps_its_order(self):
        strings = [b'A', b'B', b'C', b'\x80', b'\x81']
        nm = self.names(strings, 'UTF-8')
        at = {s: i for i, s in enumerate(nm.names.strings)}
        chain = [at[b'C'], at[b'A'], at[b'B'], at[b'\x81'], at[b'\x80']]
        self.assertEqual(nm.runs_text(nm.chain_runs(chain)),
                         'U+0043, U+0041..U+0042, bytes 81, bytes 80')

    def test_counts(self):
        strings = [b'A', b'B', b'\x80']
        nm = self.names(strings, 'UTF-8')
        self.assertEqual(nm.counted([0, 1]), '2 character(s)')
        self.assertFalse(nm.others)
        self.assertEqual(nm.counted([0, 1, 2], 'characters'),
                         '2 characters and 1 other string(s)')
        self.assertEqual(nm.counted([2]), '1 other string(s)')
        self.assertTrue(nm.others)

    def test_a_run_is_never_cut_across_lines(self):
        strings = [bytes([0xF0, 0xAB, a, b]) for a in range(0xA0, 0xB0)
                   for b in (0xC0, 0xC1)]
        nm = self.names(strings, 'UTF-8')
        runs = nm.runs(range(len(strings)))
        text = f'{len(strings)} other string(s) moved: {nm.runs_text(runs, whole=True)}'
        for lines in (m.report_lines(text, ''), m.wrapped(text, 9, 11)):
            with self.subTest(lines=lines[0][:20]):
                self.assertGreater(len(lines), 2)
                for line in lines[1:]:
                    self.assertRegex(line.strip(), r'^bytes F0 AB ')
                self.assertNotIn('\0', ''.join(lines))
                self.assertEqual(flat(' '.join(lines)),
                                 flat(text.replace('\0', ' ')))


class MeasurementsAreCurrent(unittest.TestCase):
    """The three files were measured over what this script measures today:
    the same strings, candidate pairs, charmap sequences, version and
    separator (corpus_digest). The orders and told-apart records in them
    are not measured again here, which takes glibc; a change to how the
    script tells two neighbours apart takes a new CORPUS_VERSION, and fails
    here until the files are measured again.

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

    def test_every_locale_was_measured_over_the_strings_built_here(self):
        """Each locale's list of strings, built again with stored_strings().
        The file does not keep MB_CUR_MAX; an encoding has one byte per
        character here when Python's codec encodes no character in more, or
        when Python has no codec for it and it is neither UTF-8 nor EUC
        (ARMSCII-8, GEORGIAN-PS)."""
        def width(codeset):
            if codeset == 'UTF-8' or codeset in m.EUC_CODESETS:
                return 4
            corpus = m.corpus_for(codeset)
            return 1 if corpus is None or all(len(b) == 1 for b in corpus[1]) else 4
        for machine, path in MACHINES:
            measured = {n: e for n, e in read_json(path)['locales'].items()
                        if 'error' not in e}
            self.assertGreater(len(measured), 800)
            for name, e in sorted(measured.items()):
                built = m.stored_strings(e['codeset'], width(e['codeset']))
                with self.subTest(machine=machine, locale=name):
                    self.assertIsNotNone(built)
                    self.assertEqual((e['corpus'], e['chars']),
                                     (built[1], len(built[0])))
        # The four locales Python has no codec for are measured now.
        for machine, path in MACHINES:
            with self.subTest(machine=machine):
                self.assertTrue(NO_PYTHON_CODEC <= set(
                    n for n, e in read_json(path)['locales'].items()
                    if 'error' not in e))


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


class WherePostgresUsesALocale(unittest.TestCase):
    """Where the 'also, in PostgreSQL' line sends a reader for a locale
    PostgreSQL never imports (backlog 16.42 b and 16.49).

    Freezes two wrong lines: a collation alias for a locale of an encoding
    PostgreSQL does not know (TIS-620), allows only on the client (GBK) or
    reads as SQL_ASCII (US-ASCII), which pg_import_system_collations never
    makes; and a line that sends a reader only to databases, when CREATE
    COLLATION brings such a locale into a database of any encoding (or, for
    a client-only one, into SQL_ASCII by a superuser)."""

    LOCALES = {
        'th_TH': {'codeset': 'TIS-620'}, 'th_TH.tis620': {'codeset': 'TIS-620'},
        'thai': {'codeset': 'TIS-620'}, 'th_TH.utf8': {'codeset': 'UTF-8'},
        'zh_CN.gbk': {'codeset': 'GBK'}, 'zh_CN': {'codeset': 'GB2312'},
        'zh_TW': {'codeset': 'BIG5'}, 'zh_TW.big5': {'codeset': 'BIG5'},
        'xx_YY.ascii': {'codeset': 'US-ASCII'}, 'ko_KR.utf8': {'codeset': 'UTF-8'},
        'C': {'codeset': 'ANSI_X3.4-1968'}, 'yy_ZZ': {}}

    def also(self, names):
        return m.also_known_as(names, m.postgres_names(self.LOCALES),
                               self.LOCALES)

    def test_the_reach_of_each_kind_of_encoding(self):
        for codeset, reach in (
                ('UTF-8', 'imported'), ('EUC-KR', 'imported'),
                ('ISO-8859-15', 'imported'), ('GB2312', 'imported'),
                ('TIS-620', 'anywhere'), ('ARMSCII-8', 'anywhere'),
                ('GEORGIAN-PS', 'anywhere'), ('KOI8-T', 'anywhere'),
                ('PT154', 'anywhere'), ('US-ASCII', 'anywhere'),
                ('BIG5', 'superuser'), ('BIG5-HKSCS', 'superuser'),
                ('big5-hkscs', 'superuser'), ('GBK', 'superuser'),
                ('GB18030', 'superuser')):
            with self.subTest(codeset=codeset):
                self.assertEqual(m.postgres_reach(codeset), reach)

    def test_no_alias_for_a_locale_postgres_never_imports(self):
        names = m.postgres_names(self.LOCALES)
        for name in ('th_TH.tis620', 'zh_CN.gbk', 'zh_TW.big5', 'xx_YY.ascii'):
            with self.subTest(name=name):
                self.assertIsNone(names[name][0])
        self.assertEqual(names['ko_KR.utf8'], ('ko_KR', 'ko_KR.UTF-8'))
        self.assertEqual(names['th_TH.utf8'], ('th_TH', 'th_TH.UTF-8'))

    def test_the_catalogs_hold_none_of_them(self):
        """On the three machines' catalogs, after the import: no collation
        of a locale the line calls never imported, and every locale it
        calls imported that has an alias is there."""
        for machine, path in MACHINES:
            with self.subTest(machine=machine):
                locales = {k: v for k, v in read_json(path)['locales'].items()
                           if v.get('codeset')}
                with open(os.path.join(DATA, f'{machine}.pg_collation.txt'),
                          encoding='utf-8') as f:
                    made = {row.split('|')[2] for row in f.read().splitlines()}
                never = {n for n, v in locales.items() if n not in ('C', 'POSIX')
                         and m.postgres_reach(v['codeset']) != 'imported'}
                # TIS-620 (3), ARMSCII-8, GEORGIAN-PS (2), KOI8-T (2),
                # PT154 (2), BIG5 (2), BIG5-HKSCS (2), GBK (2), GB18030
                self.assertEqual(len(never), 17)
                self.assertEqual(never & made, set())
                predicted = m.postgres_names(locales)
                self.assertEqual({n for n in never if predicted[n][0]}, set())
                imported = {n for n, (alias, _) in predicted.items() if alias}
                self.assertTrue(imported)
                self.assertLessEqual(imported, made)

    def test_the_line_for_each_kind(self):
        for names, text in (
                (['th_TH', 'th_TH.tis620', 'thai'],
                 'database locale th_TH.TIS-620; these names in a database of '
                 'any encoding, as its locale or in CREATE COLLATION'),
                (['zh_CN.gbk'],
                 'this name, also spelt zh_CN.GBK, only in a SQL_ASCII database, '
                 'as its locale or in CREATE COLLATION, which only initdb or a '
                 'superuser can set'),
                (['zh_TW', 'zh_TW.big5'],
                 'these names, also spelt zh_TW.BIG5, only in a SQL_ASCII '
                 'database, as its locale or in CREATE COLLATION, which only '
                 'initdb or a superuser can set'),
                (['zh_TW'],
                 'this name only in a SQL_ASCII database, as its locale or in '
                 'CREATE COLLATION, which only initdb or a superuser can set'),
                (['xx_YY.ascii'],
                 'database locale xx_YY.US-ASCII; this name in a database of any '
                 'encoding, as its locale or in CREATE COLLATION'),
                (['th_TH.tis620', 'th_TH.utf8'],
                 'collation "th_TH" where the database encoding is UTF-8; '
                 'database locales th_TH.TIS-620, th_TH.UTF-8; th_TH.tis620 in '
                 'a database of any encoding, as its locale or in CREATE '
                 'COLLATION'),
                (['ko_KR.utf8'],
                 'collation "ko_KR" where the database encoding is UTF-8; '
                 'database locale ko_KR.UTF-8')):
            with self.subTest(names=names):
                line = self.also(names)
                self.assertEqual(line, m.ALSO + text)
                parse_also(line)

    def test_no_line_where_postgres_has_nothing_more(self):
        """C is a collation of every encoding already; a locale whose
        encoding was not read gets what its names alone give."""
        self.assertIsNone(self.also(['C']))
        self.assertIsNone(self.also(['yy_ZZ']))


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
        probes = '61.62> 61.C3A1> 61.41>'
        base = self.BASE
        new = self.inserted(0x70, 0x31)
        told = self.records(base, {(0x30, 0x31): 'accent'})
        (old, o), (nw, n) = (measured_as(base, told, probes),
                             measured_as(new, told, probes))
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
        or_IN measured on both machines (letters a/b, case a/A). The
        strings are places in a list, and the probes their bytes."""
        strings = sorted(chr(c).encode('utf-8')
                         for c in (0x30, 0x56, 0x77, 0xB66, 0x80))
        naming = m.Naming(m.Names(strings, 'UTF-8'))
        at = {s: i for i, s in enumerate(strings)}
        e = lambda c: at[chr(c).encode('utf-8')]
        self.assertEqual(
            m.test_pair(e(0x30), e(0xB66), self.rel('accent', (0, 2)),
                        self.rel('less', (0, 2)), '61.62> 30.E0ADA6> 61.41>',
                        (0, 2), naming),
            ('U+0030 U+0041', 'U+0B66 U+0061', '<', '>'))
        # From the end of the string where the level is read from the end.
        self.assertEqual(
            m.test_pair(e(0x56), e(0x77), self.rel('accent'), self.rel('letters'),
                        '61.62< 61.C3A1> 61.41>', self.ALL, naming),
            ('U+0062 U+0056', 'U+0061 U+0077', '>', '<'))
        self.assertIsNone(m.test_pair(e(0x56), e(0x77), self.rel('tie'),
                                      self.rel('less'), '61.62> 61.C3A1> 61.41>',
                                      self.ALL, naming))
        # A probe that is no character is named by its bytes.
        self.assertEqual(
            m.test_pair(e(0x56), e(0x77), self.rel('accent'), self.rel('letters'),
                        '80.62< 61.C3A1> 61.41>', self.ALL, naming),
            ('U+0062 U+0056', 'bytes 80 U+0077', '>', '<'))

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

    def test_an_order_digest_frames_each_string(self):
        """Each string as its length and its bytes: two orders that join
        to the same bytes are told apart."""
        self.assertNotEqual(m.order_digest([b'A', b'B']), m.order_digest([b'AB']))
        self.assertNotEqual(m.order_digest([b'\xc3', b'\xa1']),
                            m.order_digest([b'\xc3\xa1']))
        self.assertEqual(m.order_digest([b'A', b'B']),
                         hashlib.sha256(b'\x01A\x01B').hexdigest())

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
        whole = bytes([0 | (2 << 3) | (2 << 5),
                       (2 << 1) | (0 << 3) | (2 << 5),
                       (2 << 1) | (2 << 3) | (0 << 5)])
        self.assertEqual(m.unclassified([probe, probe, probe], whole), 0)
        self.assertEqual(m.unclassified([probe, m.ABSENT, probe], whole), 0)

    def test_the_corpus_digest_covers_the_bytes(self):
        """Two machines whose converters write one character as different
        bytes did not measure the same thing."""
        euro = [(b'\x41', 0x41), (b'\xa4', 0x20AC)]
        self.assertNotEqual(m.corpus_id(euro),
                            m.corpus_id([(b'\x41', 0x41), (b'\x80', 0x20AC)]))
        self.assertEqual(m.corpus_id(euro), m.corpus_id(euro[::-1]))

    def test_the_corpus_digest_kept_is_of_the_lists_in_force(self):
        """corpus_digest() keeps its answer, and gives another one for
        another list of characters, other pairs, other charmap sequences,
        another version or another separator, and the first again once they
        are put back."""
        first = m.corpus_digest()
        with unittest.mock.patch.object(m.hashlib, 'sha256',
                                        side_effect=AssertionError('taken again')):
            self.assertEqual(m.corpus_digest(), first)
        fewer = dict(m.CHARMAP_ONLY)
        fewer.pop('EUC-TW')
        for name, other in (('CODEPOINTS', m.CODEPOINTS[:-1]),
                            ('CANDIDATES', m.CANDIDATES[:-1]),
                            ('CHARMAP_ONLY', fewer),
                            ('CORPUS_VERSION', 'locale_order/0'),
                            ('SEPARATOR', b'\x01')):
            with self.subTest(changed=name):
                with unittest.mock.patch.object(m, name, other):
                    self.assertNotEqual(m.corpus_digest(), first)
                self.assertEqual(m.corpus_digest(), first)

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
                                            'postgres libc', 'refused',
                                            'postgres use', 'postgres use rows',
                                            'ctype', 'ctype rows',
                                            'other encoding', 'closing',
                                            'in part', 'charmaps not checked',
                                            'charmaps checked'})
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
        return measured_as(seq, told, probes)

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
        all3, other = '61.62> 61.C3A1> 61.41>', '61.62> 30.E0ADA6> 61.41>'
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
                    lines, brief, rows, others = m.describe_change(*args)
                    # Code points in UTF-8: every string a character.
                    self.assertFalse(others)
                    old, nw, o, n = args
                    found = m.locale_findings(*args, m.Naming(m.names_for(
                        m.strings_of(old, o['corpus'])[0], o['codeset'])))
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
        # The Measuring line, nine of progress and the one that starts the
        # tables of LC_CTYPE, none written while an earlier one was still
        # held; the last is checked just below.
        self.assertIn('Measuring the character rules (LC_CTYPE) of one locale '
                      'for each of the 0 distinct LC_CTYPE glibc loaded',
                      flat(screen.getvalue()))
        self.assertEqual(len(screen.held), 11)
        self.assertEqual(screen.held, [False] * 11, 'a line was held back')
        self.assertEqual(screen.shown, screen.getvalue(), 'held back')


    def test_the_tables_of_lc_ctype_say_how_far_they_got(self):
        """The second pass, one table per distinct LC_CTYPE, says how far it
        has got every tenth of the way too: with one process it takes some
        eight minutes (6 s a table on collaudit8, 81 tables)."""
        screen = Screen()
        names = [f'l{i}' for i in range(20)]
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: names,
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=lambda name: (name, {'error': 'x'}, None),
                postgres_use=lambda name: use_of('one byte', 'X'),
                ctype_identity=lambda name, use: ['table', name],
                ctype_tables=lambda name: (name, SMALL_WIDE, SMALL_BYTES, None,
                                           ['table', name]),
                UTF8_BYTES=None, fork_pool=lambda jobs: ThreadPoolExecutor(1),
                money_digits=lambda names: {n: 3 for n in names},
                postgres_libc=lambda: dict(CLEAN_LIBC)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', screen):
            m.measure(1)
        text = screen.getvalue()
        self.assertEqual(len(re.findall(r'of 20 LC_CTYPE done \(', text)), 9)
        self.assertLess(text.index('distinct LC_CTYPE glibc loaded'),
                        text.index('of 20 LC_CTYPE done ('))
        self.assertEqual(screen.held, [False] * len(screen.held),
                         'a line was held back')


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
    for steps 6 to 8 and 11 to print. A file written before they were packed
    is read, never refused, and says so."""

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
                ('libc', {'earlier': ['OLD', 'NEW']}),
                ('use', {'earlier': ['OLD', 'NEW']}),
                ('ctype', {'earlier': ['OLD', 'NEW']})]


def use_of(text, codeset, char=None, collate='accepted', ctype='accepted',
           why='unsupported locale setting'):
    """One locale's record of how PostgreSQL uses it, as postgres_use()
    writes it: text is converts, fails, one byte or not checked (char then
    the reason); a category is accepted, refused or unknown (why)."""
    state = lambda s: [s, None if s == 'accepted' else why]
    detail = (None if text == 'one byte' else
              char if text == 'not checked' else [char[0], char[1]])
    return {'collate': state(collate), 'ctype': state(ctype),
            'codeset': codeset if ctype == 'accepted' else None,
            'text': [text, detail]}


# What postgres_use() writes for the locales of FEW on any of the three
# machines: the C locale's encoding and ISO-8859-1 have no character of more
# than one byte; UTF-8's first is U+0080, C2 80, which glibc converts.
DEFAULT_USE = {'ANSI_X3.4-1968': ('one byte', None),
               'ISO-8859-1': ('one byte', None),
               'UTF-8': ('converts', (0x80, 'c280'))}


def with_parts(money=None, libc=None, use=None, form=m.FORMAT, ctype=None,
               converter=None):
    """An edit that makes a file of the first format one of a later format
    (this one unless form): the list of its parts, the decimals of money (2
    in every locale, but those in money), its PostgreSQL processes
    (CLEAN_LIBC, but what libc changes), how PostgreSQL uses each locale
    (DEFAULT_USE by its encoding, but those in use), the character rules
    of each (ctype_of, but the entries in ctype) and the converter of each
    encoding (converter_of_use, but the entries in converter). The order
    part is the machine's own, untouched."""
    def edit(d):
        d['format'] = form
        d['parts'] = list(m.PARTS[form])
        d['money'] = {name: 2 for name in d['locales']}
        d['money'].update(money or {})
        d['postgres_libc'] = dict(CLEAN_LIBC, **(libc or {}))
        if 'postgres_use' in m.PARTS[form]:
            d['postgres_use'] = {}
            for name, r in d['locales'].items():
                if name not in (use or {}):
                    text, char = DEFAULT_USE[r['codeset']]
                    d['postgres_use'][name] = use_of(text, r['codeset'], char)
            d['postgres_use'].update(copy.deepcopy(use or {}))
        if 'ctype' in m.PARTS[form]:
            d['ctype'] = ctype_of(d['postgres_use'], ctype)
        if 'converter' in m.PARTS[form]:
            d['converter'] = converter_of_use(d['postgres_use'], converter)
            for r in d['postgres_use'].values():
                if r['codeset'] in m.CONVERTER_CODESETS:
                    r['text'] = ['converter', None]
        if 'back' in m.PARTS[form]:
            d['utf8_forward'], d['back'] = round_trip_of_use(
                d['postgres_use'], d['converter'])
    return edit


# A table of LC_CTYPE as ctype_tables() writes one, kept small: the tests
# of the part read its shape, not glibc's rules.
SMALL_WIDE = json.dumps(
    {'upper': [[0x61, 0x41]], 'lower': [[0x41, 0x61]],
     'classes': {k: [[0x41, 0x41]] for k in m.CTYPE_CLASSES}},
    sort_keys=True, separators=(',', ':')).encode('ascii')
SMALL_BYTES = bytes(range(256)) * 2 + bytes(256) * len(m.CTYPE_CLASSES)
# The identity of the compiled LC_CTYPE every locale of ctype_of() uses.
SOME_DATA = 'd' * 64


def ctype_of(use, entries=None):
    """The ctype part of a machine whose postgres_use is use: each locale
    setlocale accepted for LC_CTYPE on one table (C and POSIX on glibc's
    builtin one, every other on SOME_DATA), each other one as postgres_use
    said, and entries in place; every table named is SMALL_WIDE and
    SMALL_BYTES."""
    locales = {name: (['table', 'builtin' if name in ('C', 'POSIX') else SOME_DATA]
                      if r['ctype'][0] == 'accepted' else list(r['ctype']))
               for name, r in use.items()}
    locales.update(entries or {})
    wk = hashlib.sha256(SMALL_WIDE).hexdigest()
    bk = hashlib.sha256(SMALL_BYTES).hexdigest()
    return {'locales': locales,
            'tables': {e[1]: [wk, bk] for e in locales.values() if e[0] == 'table'},
            'wide': {wk: m.pack(SMALL_WIDE)}, 'bytes': {bk: m.pack(SMALL_BYTES)}}


# What the whole files of RHEL9 and RHEL10 say PostgreSQL stores and their
# order does not place: every locale of the three encodings, measured on
# both, th_TH's three included though their order changed.
STORED_9_TO_10 = [
    ('bytes', {'other encoding': {
        'hy_AM.armscii8': 'ARMSCII-8', 'ka_GE': 'GEORGIAN-PS',
        'ka_GE.georgianps': 'GEORGIAN-PS', 'kk_KZ': 'PT154',
        'kk_KZ.pt154': 'PT154', 'tg_TJ': 'KOI8-T', 'tg_TJ.koi8t': 'KOI8-T',
        'th_TH': 'TIS-620', 'th_TH.tis620': 'TIS-620', 'thai': 'TIS-620'}})]

# The clean line of FEW: four UTF-8 locales, and C and POSIX of one byte.
USE_FEW = ('use', {'converts': 4, 'one byte': 2, 'left out': {}})
# And of their character rules: the four besides C and POSIX on one table.
CTYPE_FEW = [('ctype', {'same': 'character', 'count': 4, 'other': False,
                        'utf8': True}),
             ('ctype', {'same': 'byte', 'count': 4, 'other': False,
                        'utf8': False})]


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
    for kind, key in (('order_data', 'order'), ('pair_data', 'pairs'),
                      ('strings_data', 'corpus')):
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


class OtherStringsNamedInTheOutputs(Checks):
    """The line that says how a string that is not a character is named:
    under the report's list, in the summary and atop the characters file,
    when a change names or counts one, and nowhere when none does. And a
    string is named under its locale's encoding, so two locales with the
    same bytes in the same order under two encodings are two groups."""

    def outputs(self, old, new):
        tag = self.id().rsplit('.', 1)[-1]
        summary = os.path.join(self.tmp, f'{tag}.sum')
        chars = os.path.join(self.tmp, f'{tag}.tsv')
        rc, out, err = run_compare(old, new, '--summary-to', summary,
                                   '--characters-to', chars)
        self.assertEqual((rc, err), (0, ''))
        with open(summary, encoding='utf-8') as f:
            brief = parse_summary(f.read())
        with open(chars, encoding='utf-8') as f:
            rows = parse_characters(f.read())
        return parse_report(out), brief, rows

    def test_said_where_one_is_named(self):
        report, summary, rows = self.outputs(RHEL9, RHEL10)
        self.assertEqual((report['others_note'], summary['others_note'],
                          rows.others_note), (m.OTHER_STRINGS,) * 3)

    def test_not_said_where_none_is(self):
        """sv_SE.utf8 alone, 8 -> 9: two characters moved, nothing else."""
        old = self.made_from(RHEL8, 'old', ('C', 'POSIX', 'sv_SE.utf8'))
        new = self.made_from(RHEL9, 'new', ('C', 'POSIX', 'sv_SE.utf8'))
        report, summary, rows = self.outputs(old, new)
        self.assertEqual(set(by_name(report)), {'sv_SE.utf8'})
        self.assertEqual((report['others_note'], summary['others_note'],
                          rows.others_note), (None, None, None))

    def made_from(self, base, tag, names):
        with open(base, encoding='utf-8') as f:
            text = trimmed(f.read(), names)
        path = os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}-{tag}.out')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_named_under_each_encoding(self):
        """th_TH (TIS-620) 9 -> 10, and the same two measurements given a
        second locale, ka_GE, under GEORGIAN-PS: byte C0, a character of
        TIS-620 (U+0E40), is a byte there, so the two are named apart."""
        def as_georgian(d):
            d['locales']['ka_GE'] = dict(d['locales']['th_TH'],
                                         codeset='GEORGIAN-PS')
        names = ('C', 'POSIX', 'th_TH', 'ka_GE')
        old = self.made_from(RHEL9, 'old', names)
        new = self.made_from(RHEL10, 'new', names)
        for path in (old, new):
            with open(path, encoding='utf-8') as f:
                d = json.load(f)
            as_georgian(d)
            d['content'] = m.content_digest(d)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps(d, sort_keys=True) + '\n')
        report, summary, rows = self.outputs(old, new)
        changes = by_name(report)
        self.assertIsNot(changes['th_TH'], changes['ka_GE'])
        moved = {name: characters_of(group, 'sorts differently')
                 for names_, group in rows.items() for name in names_
                 if name in ('th_TH', 'ka_GE')}
        self.assertIn(0x0E40, moved['th_TH'])
        self.assertIn(b'\xc0', moved['ka_GE'])
        self.assertNotIn(b'\xc0', moved['th_TH'])


class CharmapsCheckedInTheComparison(Checks):
    """charmap_check and what step 11 prints of it: a machine's charmap that
    gives a character to a sequence the strings measured lack makes its
    locales measured in part; a charmap that cannot be read, or a codeset no
    file declares, makes them not checked; a machine with no charmaps is
    not checked, and says why."""

    UTF8 = ('<code_set_name> UTF-8\n<comment_char> %\n<escape_char> /\n'
            '<mb_cur_min> 1\n<mb_cur_max> 6\nCHARMAP\n<U0041>     /x41         LATIN CAPITAL LETTER A\n'
            '<U00E9>     /xc3/xa9     LATIN SMALL LETTER E WITH ACUTE\n'
            '{extra}END CHARMAP\n')
    # Not UTF-8, and not among the sequences CHARMAP_ONLY adds.
    EXTRA = '<U0010FFFD> /xc0/x80     <private>\n'

    def charmaps(self, tag, **files):
        root = os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}-{tag}')
        os.mkdir(root)
        for name, text in files.items():
            with open(os.path.join(root, name.replace('_', '-')), 'w',
                      encoding='latin-1') as f:
                f.write(text)
        return root

    def data(self, locales, strings):
        digest = m.strings_digest(strings)
        return {'path': 'x.out', 'strings_data': {digest: m.pack_strings(strings)},
                'locales': {name: (dict(r, corpus=digest) if 'error' not in r
                                   else r) for name, r in locales.items()}}

    def test_the_premise(self):
        """C0 80 is not in the UTF-8 list, so the extra line is a sequence
        the strings measured lack."""
        self.assertNotIn(b'\xc0\x80', m.stored_strings('UTF-8', 6)[0])

    def test_a_sequence_the_list_lacks(self):
        root = self.charmaps('x', UTF_8=self.UTF8.format(extra=self.EXTRA))
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'},
                          'de_DE.utf8': {'codeset': 'UTF-8'},
                          'C': {'codeset': 'UTF-8'},
                          'xx_XX': {'error': 'cannot be selected'}},
                         [b'A', b'\xc3\xa9'])
        lacking, cannot, why = m.charmap_check(data, ('dir', root))
        self.assertEqual(lacking, {'de_DE.utf8': [('UTF-8', [b'\xc0\x80'])],
                                   'en_US.utf8': [('UTF-8', [b'\xc0\x80'])]})
        self.assertEqual((cannot, why), ({}, None))
        # The control: the same charmap without it.
        root = self.charmaps('y', UTF_8=self.UTF8.format(extra=''))
        self.assertEqual(m.charmap_check(data, ('dir', root)), ({}, {}, None))

    def test_a_range_member_the_list_lacks(self):
        """A range of three, C3 A0 to C3 A2, whose middle member the
        strings measured lack: that member is named, the two others not."""
        root = self.charmaps('x', UTF_8=self.UTF8.format(
            extra='<U00E0>..<U00E2> /xc3/xa0 <latin>\n'))
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'}},
                         [b'A', b'\xc3\xa0', b'\xc3\xa2', b'\xc3\xa9'])
        self.assertEqual(m.charmap_check(data, ('dir', root))[0],
                         {'en_US.utf8': [('UTF-8', [b'\xc3\xa1'])]})

    def test_nul_is_not_a_sequence_lacking(self):
        root = self.charmaps('x', UTF_8=self.UTF8.format(
            extra='<U0000>     /x00         NULL\n'))
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'}},
                         [b'A', b'\xc3\xa9'])
        self.assertEqual(m.charmap_check(data, ('dir', root)), ({}, {}, None))

    def test_every_file_that_declares_the_codeset_is_read(self):
        """Two files declare UTF-8; the second gives a character to C0 80.
        Which one built the locale is not known, so both are read."""
        root = self.charmaps('x', UTF_8=self.UTF8.format(extra=''),
                             UTF_8B=self.UTF8.format(extra=self.EXTRA))
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'}},
                         [b'A', b'\xc3\xa9'])
        self.assertEqual(m.charmap_check(data, ('dir', root))[0],
                         {'en_US.utf8': [('UTF-8B', [b'\xc0\x80'])]})

    def test_a_file_that_could_declare_the_codeset_is_read(self):
        """A charmap whose prolog is not read whole (a line glibc skips, as
        in MAC-CENTRALEUROPE) could declare UTF-8: it is read too, and,
        unread, leaves the locales not checked."""
        loose = self.UTF8.format(extra='').replace(
            'CHARMAP\n', '<comment> a line glibc skips\nCHARMAP\n', 1)
        root = self.charmaps('x', UTF_8=self.UTF8.format(extra=''),
                             LOOSE=loose)
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'}},
                         [b'A', b'\xc3\xa9'])
        lacking, cannot, why = m.charmap_check(data, ('dir', root))
        self.assertEqual((lacking, why), ({}, None))
        self.assertRegex(cannot.get('en_US.utf8', ''),
                         r'^its charmap on the machine, LOOSE, cannot be read '
                         r'whole: ')

    def test_a_compressed_charmap_is_read(self):
        root = self.charmaps('x')
        with open(os.path.join(root, 'UTF-8.gz'), 'wb') as f:
            f.write(gzip.compress(self.UTF8.format(extra=self.EXTRA)
                                  .encode('latin-1')))
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'}},
                         [b'A', b'\xc3\xa9'])
        self.assertEqual(m.charmap_check(data, ('dir', root))[0],
                         {'en_US.utf8': [('UTF-8', [b'\xc0\x80'])]})

    def test_what_cannot_be_checked(self):
        root = self.charmaps('x', UTF_8=self.UTF8.format(extra='')
                             + 'a line this does not read\n')
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'},
                          'zh_TW': {'codeset': 'BIG5'}}, [b'A', b'\xc3\xa9'])
        lacking, cannot, why = m.charmap_check(data, ('dir', root))
        self.assertEqual((lacking, why), ({}, None))
        self.assertEqual(set(cannot), {'en_US.utf8', 'zh_TW'})
        self.assertRegex(cannot['en_US.utf8'], r'^its charmap on the machine, '
                         r'UTF-8, cannot be read whole: ')
        self.assertEqual(cannot['zh_TW'], 'no charmap on the machine declares '
                                          'its encoding, BIG5')

    def test_a_machine_not_checked_at_all(self):
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'}}, [b'A'])
        self.assertEqual(m.charmap_check(data, ('why', 'a reason')),
                         ({}, {}, 'a reason'))
        root = self.charmaps('x')
        os.mkdir(os.path.join(root, 'a folder'))
        lacking, cannot, why = m.charmap_check(data, ('dir', root))
        self.assertEqual((lacking, cannot), ({}, {}))
        self.assertRegex(why, r'^its charmaps cannot be read: ')

    def outputs(self, *args):
        """(report, summary, characters file) of a run through main()."""
        tag = self.id().rsplit('.', 1)[-1]
        summary = os.path.join(self.tmp, f'{tag}.sum')
        chars = os.path.join(self.tmp, f'{tag}.tsv')
        # The two files cut to FEW, whose locales are all UTF-8 but C and
        # POSIX.
        argv = [SCRIPT, '--compare', self.made(RHEL9, 'old'),
                self.made(RHEL10, 'new'), '--summary-to', summary,
                '--characters-to', chars, *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', argv), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            rc = m.main()
        self.assertEqual((rc, stderr.getvalue()), (0, ''))
        with open(summary, encoding='utf-8') as f:
            brief = parse_summary(f.read())
        with open(chars, encoding='utf-8') as f:
            rows = parse_characters(f.read())
        return parse_report(stdout.getvalue()), brief, rows

    def test_the_outputs_name_the_locales_measured_in_part(self):
        """The new machine's UTF-8 charmap gives a character to C0 80: the
        four UTF-8 locales of FEW are measured in part, in the report, the
        summary and the characters file alike, and an unchanged one counts
        as unchanged only as far as measured. C and POSIX are glibc's
        built-in locale."""
        old = self.charmaps('old', UTF_8=self.UTF8.format(extra=''))
        new = self.charmaps('new', UTF_8=self.UTF8.format(extra=self.EXTRA))
        report, brief, rows = self.outputs('--old-charmaps', old,
                                           '--new-charmaps', new)
        utf8 = {'de_DE.utf8', 'en_US.utf8', 'ja_JP.utf8', 'sv_SE.utf8'}
        self.assertEqual(report['in_part'], {
            name: [('new', 'UTF-8', 1, ['C0 80'])] for name in utf8})
        self.assertEqual(set(brief['in_part']), utf8)
        in_rows = {names[0]: [(r['side'], r['charmap'], r['seqs'])
                              for r in group if r['finding'] == 'measured in part']
                   for names, group in rows.items()}
        self.assertEqual({k: v for k, v in in_rows.items() if v},
                         {name: [('new', 'UTF-8', [b'\xc0\x80'])]
                          for name in utf8})
        self.assertEqual(report['charmaps_unchecked'], [])
        self.assertEqual(brief['charmaps_unchecked'], [])
        self.assertEqual(rows.charmaps_unchecked, [])
        unchanged = utf8 - set(by_name(report))
        self.assertTrue(unchanged)
        self.assertEqual(report['totals']['partly'],
                         len(unchanged | set(report['partly'])))
        # A changed one stays changed.
        self.assertEqual(set(by_name(report)) & utf8,
                         utf8 - unchanged)

    def test_a_check_that_finds_nothing_is_counted(self):
        """Both machines checked whole and nothing found: one line with the
        locales it covers, the four UTF-8 locales of FEW (C and POSIX are
        glibc's built-in locale), after the money line, in the three outputs
        alike. Where something is found, or a machine is not checked, the
        line is not there."""
        old = self.charmaps('old', UTF_8=self.UTF8.format(extra=''))
        new = self.charmaps('new', UTF_8=self.UTF8.format(extra=''))
        report, brief, rows = self.outputs('--old-charmaps', old,
                                           '--new-charmaps', new)
        for checks in (report['checks'], brief['checks'], rows.checks):
            self.assertIn(('charmaps', {'checked': 4}), checks)
        self.assertEqual((report['in_part'], report['charmaps_unchecked']),
                         ({}, []))
        new = self.charmaps('found', UTF_8=self.UTF8.format(extra=self.EXTRA))
        for args in (('--old-charmaps', old, '--new-charmaps', new),
                     ('--old-no-charmaps', 'a reason', '--new-charmaps', old),
                     ()):
            with self.subTest(args=args):
                report, brief, rows = self.outputs(*args)
                for checks in (report['checks'], brief['checks'], rows.checks):
                    self.assertNotIn('charmaps', [k for k, d in checks])

    def test_the_count_is_of_the_locales_compared(self):
        """The counted line names the locales compared, not every locale of
        a machine: of en_US.utf8, de_DE.utf8 and C, only en_US.utf8 and C
        are compared, and C is glibc's built-in locale."""
        root = self.charmaps('x', UTF_8=self.UTF8.format(extra=''))
        data = self.data({'en_US.utf8': {'codeset': 'UTF-8'},
                          'de_DE.utf8': {'codeset': 'UTF-8'},
                          'C': {'codeset': 'UTF-8'}}, [b'A', b'\xc3\xa9'])
        lacking, found = m.charmap_findings(data, data, (('dir', root),) * 2,
                                            ['en_US.utf8', 'C'])
        self.assertEqual(lacking, {})
        self.assertEqual([(k, d['text']) for k, d in found], [(
            'charmaps checked', "Character sets: every byte sequence that each "
            "machine's charmaps give a character, in the 1 locale(s) compared, "
            "is among the strings measured.")])

    def test_the_outputs_say_what_was_not_checked(self):
        """One machine given no charmaps, with its reason, and on the other
        a codeset no file declares: two lines, each in the three outputs;
        given none on either side, one line for both."""
        new = self.charmaps('new', UTF_8=self.UTF8.format(extra=''))
        report, brief, rows = self.outputs('--old-no-charmaps', 'a reason',
                                           '--new-charmaps', new)
        self.assertEqual(report['charmaps_unchecked'],
                         [('the old machine', 'a reason', None)])
        self.assertEqual(brief['charmaps_unchecked'],
                         report['charmaps_unchecked'])
        self.assertEqual(rows.charmaps_unchecked, report['charmaps_unchecked'])
        self.assertEqual(report['in_part'], {})
        new = self.charmaps('newer')
        with open(os.path.join(new, 'BIG5'), 'w', encoding='latin-1') as f:
            f.write(self.UTF8.format(extra='').replace('UTF-8', 'BIG5'))
        report, brief, rows = self.outputs('--old-no-charmaps', 'a reason',
                                           '--new-charmaps', new)
        why = 'no charmap on the machine declares its encoding, UTF-8'
        some = ('new', None, {name: why for name in (
            'de_DE.utf8', 'en_US.utf8', 'ja_JP.utf8', 'sv_SE.utf8')})
        self.assertEqual(report['charmaps_unchecked'],
                         [('the old machine', 'a reason', None), some])
        self.assertEqual(brief['charmaps_unchecked'], report['charmaps_unchecked'])
        self.assertEqual(rows.charmaps_unchecked, report['charmaps_unchecked'])
        report, brief, rows = self.outputs()
        self.assertEqual(report['charmaps_unchecked'],
                         [('either machine', m.ALONE_BOTH, None)])
        # A reason given whole, with its full stop, ends the sentence once.
        report, brief, rows = self.outputs('--new-no-charmaps', 'another.')
        self.assertEqual(report['charmaps_unchecked'],
                         [('the old machine', m.ALONE, None),
                          ('the new machine', 'another', None)])

    def test_the_options(self):
        for argv, said in (
                (['--old-charmaps', 'x'], 'go with --compare'),
                (['--compare', 'a', 'b', '--old-no-charmaps', ' '],
                 '--old-no-charmaps needs a reason that prints')):
            with self.subTest(argv=argv):
                rc, out, err = run_script(*argv)
                self.assertNotEqual(rc, 0)
                self.assertIn(said, err)
        rc, out, err = run_script('--compare', 'a', 'b', '--new-charmaps', 'x',
                                  '--new-no-charmaps', 'y')
        self.assertEqual(rc, 2)
        self.assertIn('not allowed with argument', err)


class FilesOfEachFormat(Checks):
    """A file of a format written before the decimals of money and the
    PostgreSQL processes were measured, over the strings measured now, is
    whole, and is compared: each part it lacks is NOT checked, naming the
    side that lacks it, and never reads as "the same". The control needs
    nothing such a file lacks, and runs on it. (One an earlier version
    wrote, over other strings, is refused: load.)"""

    HELD = [('control', {'held': True})]

    def test_two_files_of_the_first_format(self):
        """The two whole files as recorded, on the command line. The line
        on databases of another encoding needs nothing such a file lacks,
        and is said."""
        self.assertEqual(self.checks(RHEL9, RHEL10, command_line=True),
                         self.HELD + EARLIER_BOTH + STORED_9_TO_10)

    def test_two_files_of_the_first_format_cut(self):
        """The same cut to FEW: what the classes below build on."""
        self.assertEqual(self.checks(self.made(RHEL9, 'old'),
                                     self.made(RHEL10, 'new')),
                         self.HELD + EARLIER_BOTH)

    def test_the_first_format_on_the_old_side(self):
        new = self.made(RHEL10, 'new', with_parts())
        self.assertEqual(self.checks(self.made(RHEL9, 'old'), new), self.HELD + [
            ('money', {'earlier': ['OLD']}), ('libc', {'earlier': ['OLD']}),
            ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True}),
            ('use', {'earlier': ['OLD']}), ('ctype', {'earlier': ['OLD']})])

    def test_the_first_format_on_the_new_side(self):
        old = self.made(RHEL9, 'old', with_parts())
        self.assertEqual(self.checks(old, self.made(RHEL10, 'new')), self.HELD + [
            ('money', {'earlier': ['NEW']}), ('libc', {'earlier': ['NEW']}),
            ('libc', {'side': 'OLD', 'clean': 2}), ('libc', {'note': True}),
            ('use', {'earlier': ['NEW']}), ('ctype', {'earlier': ['NEW']})])

    def test_two_files_of_this_format(self):
        old = self.made(RHEL9, 'old', with_parts())
        new = self.made(RHEL10, 'new', with_parts())
        self.assertEqual(self.checks(old, new), self.HELD + [
            ('money', {'same': len(self.common(RHEL9, RHEL10))}),
            ('libc', {'side': 'OLD', 'clean': 2}),
            ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True}),
            USE_FEW] + CTYPE_FEW)

    def test_two_files_of_the_second_format(self):
        """Written by step 2's version: money and the processes checked,
        how PostgreSQL uses each locale NOT checked, on both sides."""
        old = self.made(RHEL9, 'old', with_parts(form='locale_order/2'))
        new = self.made(RHEL10, 'new', with_parts(form='locale_order/2'))
        self.assertEqual(self.checks(old, new), self.HELD + [
            ('money', {'same': len(self.common(RHEL9, RHEL10))}),
            ('libc', {'side': 'OLD', 'clean': 2}),
            ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True}),
            ('use', {'earlier': ['OLD', 'NEW']}),
            ('ctype', {'earlier': ['OLD', 'NEW']})])

    def test_the_second_format_beside_this_one(self):
        old = self.made(RHEL9, 'old', with_parts(form='locale_order/2'))
        new = self.made(RHEL10, 'new', with_parts())
        self.assertEqual(self.checks(old, new)[-2:], [
            ('use', {'earlier': ['OLD']}), ('ctype', {'earlier': ['OLD']})])

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
                    # The 255 bytes of an encoding of one byte per
                    # character (stored_strings), in byte order.
                    self.assertEqual((r['order'], r['pairs'], r['probes'],
                                      r['chars'], r['partly']),
                                     (order, pairs, probes, 255, 0))

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
            same, ('libc', {'side': 'NEW', 'clean': 2}), ('libc', {'note': True}),
            USE_FEW] + CTYPE_FEW)

    def test_the_new_side_on_the_replaced_glibc_stands_below(self):
        same = ('money', {'same': len(self.common(RHEL9, RHEL10))})
        self.assertEqual(self.where(new={'stale': [[7, 'x (deleted)']]}), [
            ('control', {'held': True}), same,
            ('libc', {'side': 'OLD', 'clean': 2}),
            ('libc', {'side': 'NEW', 'stale': 1}), ('libc', {'note': True}),
            USE_FEW] + CTYPE_FEW)

    def test_replaced_while_measured_stands_above(self):
        same = ('money', {'same': len(self.common(RHEL9, RHEL10))})
        self.assertEqual(self.where(new={'replaced': True}), [
            ('control', {'held': True}),
            ('libc', {'side': 'NEW', 'replaced': True}), same,
            ('libc', {'side': 'OLD', 'clean': 2}), ('libc', {'note': True}),
            USE_FEW] + CTYPE_FEW)

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
               'postgres_libc': dict(CLEAN_LIBC, stale=[[1, 'x']]),
               'ctype': {'locales': {}, 'tables': {}, 'wide': {}, 'bytes': {}},
               'converter': NO_CONVERTER_LOCALES, **NO_ROUND_TRIP}
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


# --- how PostgreSQL uses each locale --------------------------------------------

class PostgresUseOnTheMachine(unittest.TestCase):
    """postgres_use(): the calls PostgreSQL makes with a locale, asked of
    glibc. setlocale, nl_langinfo, mbstowcs and the corpus are doubles here,
    since this machine need not be Linux; the double of setlocale keeps the
    two categories as glibc would, and every call is checked to leave both
    as they were. Freezes a refusal read as accepted, a name never asked
    about read as refused, and a conversion that could not be tried read as
    one that worked."""

    NAME = 'ja_JP.eucjp'
    CORPUS = ([0x41, 0x3000, 0x3001], [b'A', b'\xa1\xa1', b'\xa1\xa2'], 'd')

    def run_use(self, refuse=(), unpassable=False, codeset='BIG5',
                corpus=CORPUS, converts=True, mbstowcs=None):
        state = {m.locale.LC_COLLATE: 'C', m.locale.LC_CTYPE: 'C'}
        self.calls = []

        def setlocale(category, name=None):
            if name is None:
                return state[category]
            if name == self.NAME and unpassable:
                raise ValueError('embedded null character')
            if name == self.NAME and category in refuse:
                raise m.locale.Error('unsupported locale setting')
            state[category] = name
            return name

        def langinfo(item):
            self.assertEqual(item, m.locale.CODESET)
            return codeset if state[m.locale.LC_CTYPE] == self.NAME else 'X'

        def convert(buf, data, n):
            self.calls.append((data, n, state[m.locale.LC_CTYPE]))
            return 1 if converts else m.NOT_CONVERTED

        lib = unittest.mock.Mock(mbstowcs=mbstowcs or convert)
        with unittest.mock.patch.object(m.locale, 'setlocale', setlocale), \
                unittest.mock.patch.object(m.locale, 'nl_langinfo', langinfo), \
                unittest.mock.patch.object(m, 'libc', lambda: lib), \
                unittest.mock.patch.object(m, 'corpus_for', lambda cs: (
                    self.assertEqual(cs, codeset), corpus)[1]):
            got = m.postgres_use(self.NAME)
        self.assertEqual(state, {m.locale.LC_COLLATE: 'C', m.locale.LC_CTYPE: 'C'},
                         'a category was not put back')
        return got

    def test_converts(self):
        self.assertEqual(self.run_use(), use_of('converts', 'BIG5',
                                                (0x3000, 'a1a1')))
        self.assertEqual(self.calls, [(b'\xa1\xa1', 3, self.NAME)])

    def test_fails(self):
        self.assertEqual(self.run_use(converts=False),
                         use_of('fails', 'BIG5', (0x3000, 'a1a1')))

    def test_the_first_character_of_more_than_one_byte(self):
        corpus = ([0x41, 0x42, 0x3001, 0x3000], [b'A', b'B', b'\xa1\xa2', b'\xa1\xa1'], 'd')
        self.assertEqual(self.run_use(corpus=corpus)['text'],
                         ['converts', [0x3001, 'a1a2']])

    def test_one_byte(self):
        corpus = ([0x41, 0xC0], [b'A', b'\xc0'], 'd')
        self.assertEqual(self.run_use(corpus=corpus)['text'], ['one byte', None])
        self.assertEqual(self.calls, [])

    def test_no_converter_in_python(self):
        self.assertEqual(self.run_use(corpus=None)['text'],
                         ['not checked', m.NO_CONVERTER])

    def test_no_encoding(self):
        got = self.run_use(codeset='')
        self.assertEqual((got['codeset'], got['text']),
                         (None, ['not checked', m.NO_CODESET]))

    def test_both_refused(self):
        self.assertEqual(
            self.run_use(refuse=(m.locale.LC_COLLATE, m.locale.LC_CTYPE)),
            use_of('not checked', None, m.NO_CTYPE, collate='refused',
                   ctype='refused'))

    def test_lc_ctype_is_asked_when_lc_collate_is_refused(self):
        got = self.run_use(refuse=(m.locale.LC_COLLATE,))
        self.assertEqual(got, use_of('converts', 'BIG5', (0x3000, 'a1a1'),
                                     collate='refused'))

    def test_lc_ctype_alone_refused(self):
        self.assertEqual(self.run_use(refuse=(m.locale.LC_CTYPE,)),
                         use_of('not checked', None, m.NO_CTYPE, ctype='refused'))

    def test_a_name_glibc_was_never_asked_about(self):
        got = self.run_use(unpassable=True)
        self.assertEqual([got['collate'][0], got['ctype'][0]], ['unknown', 'unknown'])
        self.assertIn('Python cannot pass the name', got['collate'][1])
        self.assertEqual(got['text'], ['not checked', m.NO_CTYPE])

    def test_the_nine_are_read_from_the_converter_part(self):
        """A locale of CONVERTER_CODESETS says only that the converter part
        holds it: no corpus is built and no character converted for it.
        Any other encoding keeps the test of one character (16.16): one PostgreSQL
        does not know, CP1125, and one it does, BIG5."""
        for codeset in m.CONVERTER_CODESETS:
            with self.subTest(codeset=codeset):
                got = self.run_use(codeset=codeset, corpus=None)
                self.assertEqual((got['codeset'], got['text']), (codeset, ['converter', None]))
                self.assertEqual(self.calls, [])
        got = self.run_use(codeset='CP1125', corpus=([0x41, 0x410], [b'A', b'\x80'], 'd'))
        self.assertEqual(got['text'], ['one byte', None])
        got = self.run_use(codeset='BIG5')
        self.assertEqual(got['text'], ['converts', [0x3000, 'a1a1']])

    def test_a_check_that_fails_says_so(self):
        def boom(*args):
            raise OSError('x')
        got = self.run_use(mbstowcs=boom)
        self.assertEqual(got['text'], ['not checked', "checking failed: OSError('x')"])

    def test_the_measuring_process_runs_it(self):
        """The call site: measure_locale gives measure_one's answer,
        postgres_use's of the same name, and ctype_identity's of that name
        given that answer."""
        with unittest.mock.patch.multiple(
                m, measure_one=lambda name: (name, {'error': 'e'}, None),
                postgres_use=lambda name: {'for': name},
                ctype_identity=lambda name, use: ['of', name, use]):
            self.assertEqual(m.measure_locale('x'), ('x', {'error': 'e'}, None,
                                                     {'for': 'x'},
                                                     ['of', 'x', {'for': 'x'}]))

    def test_measure_keeps_the_part_whole(self):
        """The call site: measure() writes one entry per locale, each what
        postgres_use returned for that locale, under the parts its format
        lists."""
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: ['a', 'b'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=lambda name: (name, {'error': 'x'}, None),
                postgres_use=lambda name: use_of('one byte', name),
                UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1),
                money_digits=lambda names: {n: 3 for n in names},
                postgres_libc=lambda: dict(CLEAN_LIBC)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', Screen()):
            text, out, _ = m.measure(1)
        data = json.loads(text)
        self.assertIn('postgres_use', data['parts'])
        self.assertEqual(data['postgres_use'], {'a': use_of('one byte', 'a'),
                                                'b': use_of('one byte', 'b')})


class PostgresUseRead(Checks):
    """A file of this format whose part about PostgreSQL's use of each
    locale is missing or malformed is refused, like any part: read as
    missing, it would say "not checked" where the machine did check."""

    def refused(self, phrase, *edits, form=m.FORMAT):
        new = self.made(RHEL10, 'new', with_parts(form=form), *edits)
        err = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.load(new, m.corpus_digest())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn(phrase, flat(err.getvalue()))

    def test_without_the_part(self):
        self.refused('lacks a part its format holds',
                     lambda d: d.pop('postgres_use'))

    def test_a_second_format_file_with_the_part(self):
        self.refused('holds one it does not',
                     lambda d: d.update(postgres_use={}), form='locale_order/2')

    def test_malformed(self):
        def entry(**k):
            return lambda d: d['postgres_use']['C'].update(**k)
        ok = use_of('converts', 'UTF-8', (0x80, 'c280'))
        for case, edit in (
                ('not a map', lambda d: d.update(postgres_use=[])),
                ('a locale missing', lambda d: d['postgres_use'].pop('C')),
                ('a name more', lambda d: d['postgres_use'].update(xx_XX=ok)),
                ('an entry that is not a map', lambda d: d['postgres_use'].update(C=[])),
                ('a key missing', lambda d: d['postgres_use']['C'].pop('codeset')),
                ('a key more', entry(more=1)),
                ('an unknown state', entry(collate=['maybe', None])),
                ('accepted with a reason', entry(collate=['accepted', 'x'])),
                ('refused with no reason', entry(collate=['refused', None])),
                ('refused with an empty reason', entry(collate=['refused', ''])),
                ('a state of three items', entry(ctype=['accepted', None, 1])),
                ('an unknown text', entry(text=['maybe', None])),
                ('converts with no character', entry(text=['converts', None])),
                ('a character that is text', entry(text=['converts', ['80', 'c280']])),
                ('a character past Unicode', entry(text=['fails', [0x110000, 'c280']])),
                ('a character that is a boolean', entry(text=['fails', [True, 'c280']])),
                ('bytes of one byte', entry(text=['converts', [0x80, 'c2']])),
                ('bytes that are not hex', entry(text=['converts', [0x80, 'zz80']])),
                ('bytes in capitals', entry(text=['converts', [0x80, 'C280']])),
                ('one byte with a character', entry(text=['one byte', [0x80, 'c280']])),
                ('not checked with no reason', entry(text=['not checked', ''])),
                ('an empty encoding', entry(codeset='')),
                ('an encoding with LC_CTYPE refused',
                 entry(ctype=['refused', 'x'], text=['not checked', 'y'])),
                ('a conversion with LC_CTYPE refused',
                 entry(ctype=['refused', 'x'], codeset=None)),
                ('a conversion with no encoding', entry(codeset=None))):
            with self.subTest(case=case):
                self.refused('what it says of how PostgreSQL uses each locale '
                             'is malformed', edit)

    def test_the_converter_state_only_where_the_part_holds_it(self):
        """In a file of this format a locale of the nine says the converter
        part holds it, and nothing else; in one of the format before, none
        says so; and an encoding outside the nine never says so."""
        def ja(text, codeset='EUC-JP'):
            def edit(d):
                d['locales']['de_DE.utf8']['codeset'] = codeset
                d['postgres_use']['de_DE.utf8'] = {
                    'collate': ['accepted', None], 'ctype': ['accepted', None],
                    'codeset': codeset, 'text': text}
                refirst(d)
            return edit
        for case, edit, form in (
                ('the nine with a test of one character',
                 ja(['converts', [0x3000, 'a1a1']]), m.FORMAT),
                ('the nine one byte', ja(['one byte', None]), m.FORMAT),
                ('the converter with a detail', ja(['converter', 'x']), m.FORMAT),
                ('the converter outside the nine', ja(['converter', None], 'BIG5'),
                 m.FORMAT),
                ('the converter in the format before', ja(['converter', None]),
                 'locale_order/4')):
            with self.subTest(case=case):
                self.refused('what it says of how PostgreSQL uses each locale '
                             'is malformed', edit, form=form)

    def test_each_form_postgres_use_writes_is_read(self):
        """The control: every shape the tests above build from is whole."""
        use = {'ja_JP.utf8': use_of('not checked', None, m.NO_CTYPE,
                                    collate='unknown', ctype='unknown'),
               'en_US.utf8': use_of('not checked', None, m.NO_CTYPE,
                                    collate='refused', ctype='refused'),
               'POSIX': use_of('not checked', 'ANSI_X3.4-1968', m.NO_CONVERTER),
               'de_DE.utf8': use_of('fails', 'UTF-8', (0x80, 'c280'))}
        new = self.made(RHEL10, 'new', with_parts(use=use),
                        selected_not('en_US.utf8', 'ja_JP.utf8'))
        self.assertEqual(m.load(new, m.corpus_digest())[0]['postgres_use']['ja_JP.utf8'],
                         use['ja_JP.utf8'])

    def test_a_refusal_beside_its_measured_order(self):
        """measure_one selects LC_COLLATE, then LC_CTYPE: a locale glibc
        refused either has no order. A file that holds both was measured by
        two glibcs, and is refused (review round 2, finding 5)."""
        for collate, ctype in (('refused', 'refused'), ('refused', 'accepted'),
                               ('accepted', 'refused')):
            with self.subTest(collate=collate, ctype=ctype):
                use = {'de_DE.utf8': use_of(
                    'not checked' if ctype == 'refused' else 'converts',
                    None if ctype == 'refused' else 'UTF-8',
                    m.NO_CTYPE if ctype == 'refused' else (0x80, 'c280'),
                    collate=collate, ctype=ctype)}
                new = self.made(RHEL10, 'new', with_parts(use=use))
                err = io.StringIO()
                with unittest.mock.patch.object(m.sys, 'stderr', err), \
                        self.assertRaises(SystemExit):
                    m.load(new, m.corpus_digest())
                self.assertIn('and holds the order or the encoding measured '
                              'for it; glibc changed while that machine was measured',
                              flat(err.getvalue()))

    def test_the_twins_of_that_contradiction(self):
        """Review round 3, finding 2: a name Python could not hand to C
        has no order either, and postgres_use reads the encoding the order
        read, in the same process; a file that says otherwise is refused.
        Review round 5: the name Python could not hand to C is its own
        message, as no glibc explains it, and an encoding read for only one
        of the two, the order or PostgreSQL's use, is two encodings too, in
        either direction."""
        kept = {'de_DE.utf8': {'error': 'measuring failed: x', 'codeset': 'UTF-8'}}
        unread = {'de_DE.utf8': {'error': 'cannot be measured: its encoding '
                                          'could not be read'}}
        for case, use, locales, phrase in (
                ('never asked', {'de_DE.utf8': use_of('not checked', None, m.NO_CTYPE,
                                                      collate='unknown', ctype='unknown')},
                 None,
                 'says Python could not pass its name to the C library, and holds '
                 'the order or the encoding measured for it; the file contradicts '
                 'itself'),
                ('two encodings', {'de_DE.utf8': use_of('converts', 'ISO-8859-1',
                                                        (0x80, 'c280'))},
                 None,
                 'reads its encoding as UTF-8 for the order and as ISO-8859-1 for '
                 'how PostgreSQL uses it'),
                ('two encodings beside an order that failed',
                 {'de_DE.utf8': use_of('converts', 'ISO-8859-1', (0x80, 'c280'))},
                 kept,
                 'reads its encoding as UTF-8 for the order and as ISO-8859-1 for '
                 'how PostgreSQL uses it'),
                ('no encoding beside an order that read one',
                 {'de_DE.utf8': use_of('not checked', None, m.NO_CODESET)},
                 None,
                 'reads its encoding as UTF-8 for the order and could not read it '
                 'for how PostgreSQL uses it'),
                ('an encoding beside an order that read none',
                 {'de_DE.utf8': use_of('converts', 'UTF-8', (0x80, 'c280'))},
                 unread,
                 'could not read its encoding for the order and read it as UTF-8 '
                 'for how PostgreSQL uses it'),
                ('an encoding kept beside a refusal',
                 {'de_DE.utf8': use_of('not checked', None, m.NO_CTYPE,
                                       collate='refused', ctype='refused')},
                 kept,
                 'says glibc refused it, and holds the order or the encoding '
                 'measured for it')):
            with self.subTest(case=case):
                edit = ((lambda d, e=locales: d['locales'].update(**e)) if locales
                        else (lambda d: None))
                new = self.made(RHEL10, 'new', with_parts(use=use), edit)
                err = io.StringIO()
                with unittest.mock.patch.object(m.sys, 'stderr', err), \
                        self.assertRaises(SystemExit):
                    m.load(new, m.corpus_digest())
                self.assertIn(phrase, flat(err.getvalue()))


# --- the character rules (LC_CTYPE) of each locale --------------------------


class FakeLocale:
    """A locale_t as newlocale() hands one back, laid out in memory the way
    glibc 2.28 to 2.39 lay it: a pointer to its LC_CTYPE's struct
    __locale_data first, whose head is name, filedata and filesize.
    filedata is the payload's address, or NULL when payload is None; data
    False leaves the pointer to the struct NULL."""

    def __init__(self, payload, size=None, data=True):
        self.raw = (ctypes.create_string_buffer(payload, len(payload))
                    if payload is not None else None)
        self.head = (ctypes.c_uint64 * 3)(
            0, ctypes.addressof(self.raw) if self.raw is not None else 0,
            len(payload) if size is None else size)
        self.loc = (ctypes.c_uint64 * 13)(
            ctypes.addressof(self.head) if data else 0)
        self.address = ctypes.addressof(self.loc)


class CtypeOnTheMachine(unittest.TestCase):
    """ctype_identity(), ctype_tables() and ctype_part(): which LC_CTYPE
    data glibc loads for each locale, what its functions answer, and the
    part they make. glibc is a double here, since this machine need not be
    Linux: newlocale hands back a FakeLocale, and the functions of LC_CTYPE
    answer under the locale the double of setlocale holds. Freezes data
    not of a compiled LC_CTYPE read as one, a refusal read as a table, a
    table measured on another locale than its own, and a measurement that
    failed read as one that worked."""

    PAYLOAD = m.CTYPE_MAGIC.to_bytes(4, sys.byteorder) + b'rules of a locale'

    def identity(self, fake, use=None, name='de_DE.utf8'):
        """ctype_identity of name, newlocale handing back fake (None for a
        refusal), with every call to the double recorded."""
        self.calls = []

        def newlocale(mask, raw, base):
            self.calls.append(('newlocale', mask, raw, base))
            return None if fake is None else fake.address

        lib = unittest.mock.Mock(newlocale=newlocale, freelocale=lambda loc:
                                 self.calls.append(('freelocale', loc)))
        with unittest.mock.patch.object(m, 'ctype_libc', lambda: lib):
            return m.ctype_identity(name, use or use_of('converts', 'UTF-8',
                                                         (0x80, 'c280')))

    def test_the_data_glibc_loaded(self):
        fake = FakeLocale(self.PAYLOAD)
        self.assertEqual(self.identity(fake),
                         ['table', hashlib.sha256(self.PAYLOAD).hexdigest()])
        self.assertEqual(self.calls, [('newlocale', 1, b'de_DE.utf8', None),
                                      ('freelocale', fake.address)])

    def test_two_names_of_the_same_data_are_one_table(self):
        self.assertEqual(self.identity(FakeLocale(self.PAYLOAD)),
                         self.identity(FakeLocale(self.PAYLOAD), name='de_AT.utf8'))
        self.assertNotEqual(self.identity(FakeLocale(self.PAYLOAD)),
                            self.identity(FakeLocale(self.PAYLOAD + b'!')))

    def test_glibc_s_own(self):
        """C and POSIX: no file, and glibc's builtin table."""
        self.assertEqual(self.identity(FakeLocale(None, size=0)),
                         ['table', 'builtin'])

    def test_what_is_not_a_compiled_lc_ctype_is_not_read(self):
        for case, fake in (
                ('another magic', FakeLocale(b'\x00' * 4 + b'rules')),
                ('the magic of LC_COLLATE',
                 FakeLocale((0x20051014 ^ 3).to_bytes(4, sys.byteorder) + b'x')),
                ('shorter than the magic', FakeLocale(self.PAYLOAD[:3])),
                # string_at reads a negative size up to the first NUL.
                ('a negative size', FakeLocale(self.PAYLOAD, size=-1)),
                ('no file but a size', FakeLocale(None, size=5)),
                ('no data at all', FakeLocale(self.PAYLOAD, data=False))):
            with self.subTest(case=case):
                self.assertEqual(self.identity(fake), ['not checked', m.CTYPE_UNREAD])
                self.assertEqual(self.calls[-1][0], 'freelocale')

    def test_a_refusal_is_postgres_use_s(self):
        """Whether glibc accepts the name is asked once, by postgres_use;
        newlocale is not asked about a name setlocale did not accept."""
        for state in ('refused', 'unknown'):
            with self.subTest(state=state):
                use = use_of('not checked', None, m.NO_CTYPE, ctype=state,
                             why='why ' + state)
                self.assertEqual(self.identity(FakeLocale(self.PAYLOAD), use),
                                 [state, 'why ' + state])
                self.assertEqual(self.calls, [])

    def test_newlocale_refusing_what_setlocale_accepted(self):
        """For want of memory, or a name holding ';': LC_CTYPE not checked,
        with the reason errno holds, or one saying there was none (errno 0
        reads "Success"), never a refusal the comparison would read as glibc
        changing (review round 2)."""
        with unittest.mock.patch.object(m.ctypes, 'get_errno', lambda: 12):
            self.assertEqual(self.identity(None), [
                'not checked', 'newlocale gave no locale for it: ' + os.strerror(12)])
        self.assertEqual([c[0] for c in self.calls], ['newlocale'])
        with unittest.mock.patch.object(m.ctypes, 'get_errno', lambda: 0):
            self.assertEqual(self.identity(None), [
                'not checked', 'newlocale gave no locale for it: no reason given'])

    def test_a_name_in_bytes_python_cannot_decode(self):
        name = 'de_DE.' + b'\xff'.decode('utf-8', 'surrogateescape')
        self.identity(FakeLocale(self.PAYLOAD), name=name)
        self.assertEqual(self.calls[0][2], b'de_DE.\xff')

    def test_a_machine_not_of_64_bits(self):
        """The layout read is a 64-bit glibc's: elsewhere nothing is read,
        and the reason says why."""
        size = ctypes.sizeof
        with unittest.mock.patch.object(m.ctypes, 'sizeof', lambda t: 4 if t in (
                ctypes.c_void_p, ctypes.c_long) else size(t)):
            got = self.identity(FakeLocale(self.PAYLOAD))
        self.assertEqual(got[0], 'not checked')
        self.assertIn('not 64-bit', got[1])
        self.assertEqual(self.calls, [])

    def test_a_reading_that_fails_says_so(self):
        fake = FakeLocale(self.PAYLOAD)
        with unittest.mock.patch.object(m.ctypes, 'string_at',
                                        unittest.mock.Mock(side_effect=OSError('x'))):
            self.assertEqual(self.identity(fake),
                             ['not checked', "reading it failed: OSError('x')"])
        self.assertEqual(self.calls[-1], ('freelocale', fake.address))

    def tables(self, codepoints=(0x41, 0x61, 0xD7FF, 0xE000, 0xE001), upper=None,
               boom=None):
        """ctype_tables('xx_XX') with every function answering only under
        xx_XX, as the double of setlocale holds it: towupper and toupper
        map a to A, towlower and tolower A to a, and a code point is alpha
        when it is not 0x41's neighbour 0x42; LC_CTYPE checked put back."""
        state = {m.locale.LC_CTYPE: 'C'}

        def setlocale(category, name=None):
            self.assertEqual(category, m.locale.LC_CTYPE)
            if name is not None:
                state[category] = name
            return state[category]

        def under(f):
            def g(c):
                self.assertEqual(state[m.locale.LC_CTYPE], 'xx_XX')
                if boom is not None:
                    raise boom
                return f(c)
            return g
        functions = {
            'towupper': under(lambda c: 0x41 if c == 0x61 else c),
            'towlower': under(lambda c: 0x61 if c == 0x41 else c),
            'toupper': under(upper or (lambda c: 0x41 if c == 0x61 else c)),
            'tolower': under(lambda c: 0x61 if c == 0x41 else c)}
        # glibc's is<class>, and isw<class> of an ASCII character, answer
        # the class's bit of the table, not 1 (ctype.h, _ISbit: 1024 for
        # alpha on little-endian); isw<class> above ASCII answers 0 or 1
        # (wcfuncs.c, wchar-lookup.h).
        for k in m.CTYPE_CLASSES:
            functions['isw' + k] = under(
                lambda c, k=k: 1024 if k == 'alpha' and c != 0x42 else 0)
            functions['is' + k] = under(
                lambda c, k=k: 2048 if k == 'digit' and c == 0x30 else 0)
        lib = unittest.mock.Mock(**functions)

        def identity(name, use):
            self.assertEqual((name, use, state[m.locale.LC_CTYPE]),
                             ('xx_XX', {'ctype': ['accepted', None]}, 'xx_XX'))
            return ['table', 'f' * 64]
        with unittest.mock.patch.object(m.locale, 'setlocale', setlocale), \
                unittest.mock.patch.object(m, 'ctype_libc', lambda: lib), \
                unittest.mock.patch.object(m, 'ctype_identity', identity), \
                unittest.mock.patch.object(m, 'CODEPOINTS', list(codepoints)):
            got = m.ctype_tables('xx_XX')
        self.assertEqual(state, {m.locale.LC_CTYPE: 'C'}, 'LC_CTYPE not put back')
        return got

    def test_the_tables_of_a_locale(self):
        name, wide, byte, why, identity = self.tables()
        self.assertEqual((name, why, identity), ('xx_XX', None, ['table', 'f' * 64]))
        self.assertEqual(json.loads(wide), {
            'upper': [[0x61, 0x41]], 'lower': [[0x41, 0x61]],
            'classes': dict({k: [] for k in m.CTYPE_CLASSES},
                            alpha=[[0x41, 0x41], [0x61, 0x61], [0xD7FF, 0xD7FF],
                                   [0xE000, 0xE001]])})
        self.assertTrue(m.wide_whole(wide))
        self.assertEqual(len(byte), m.CTYPE_BYTES)
        self.assertEqual((byte[0x61], byte[256 + 0x41]), (0x41, 0x61))
        rows = {k: byte[512 + 256 * i:768 + 256 * i]
                for i, k in enumerate(m.CTYPE_CLASSES)}
        self.assertEqual(rows['digit'], bytes(0x30) + b'\x01' + bytes(0xCF))
        self.assertEqual(rows['alpha'], bytes(256))
        self.assertTrue(m.bytes_whole(byte))

    def test_a_run_stops_at_the_surrogates(self):
        """0xD7FF and 0xE000 are neighbours in CODEPOINTS, never a run."""
        wide = json.loads(self.tables()[1])
        self.assertIn([0xD7FF, 0xD7FF], wide['classes']['alpha'])
        self.assertNotIn([0xD7FF, 0xE001], wide['classes']['alpha'])

    def test_a_measurement_that_fails_says_so(self):
        self.assertEqual(self.tables(boom=OSError('x')),
                         ('xx_XX', None, None, "measuring it failed: OSError('x')",
                          None))

    def test_a_case_past_a_byte_is_a_failure(self):
        name, wide, byte, why, _ = self.tables(upper=lambda c: 0x1E9E if c == 0xDF else c)
        self.assertEqual((wide, byte), (None, None))
        self.assertIn('measuring it failed', why)

    def part(self, identities, answers):
        """ctype_part over identities, measure_tables answering answers
        {name: (wide, bytes, why)}, the names it was asked about kept."""
        self.asked = []

        def measure_tables(names):
            self.asked.append(list(names))
            return [(n,) + answers[n] for n in names]
        return m.ctype_part(identities, measure_tables)

    def own(self, key):
        """What ctype_tables says it measured, for a table of key."""
        return ['table', key]

    def test_one_table_measured_per_data_on_its_first_locale(self):
        a, b = 'a' * 64, 'b' * 64
        part = self.part(
            {'de_DE.utf8': ['table', a], 'de_AT.utf8': ['table', a],
             'C': ['table', 'builtin'], 'xx_XX': ['refused', 'no'],
             'yy_YY': ['not checked', 'unread'], 'tr_TR.utf8': ['table', b]},
            {'de_AT.utf8': (SMALL_WIDE, SMALL_BYTES, None, self.own(a)),
             'C': (SMALL_WIDE, SMALL_BYTES, None, self.own('builtin')),
             'tr_TR.utf8': (None, None, 'measuring it failed: x', None)})
        self.assertEqual(self.asked, [['de_AT.utf8', 'tr_TR.utf8', 'C']])
        wk, bk = (hashlib.sha256(SMALL_WIDE).hexdigest(),
                  hashlib.sha256(SMALL_BYTES).hexdigest())
        self.assertEqual(part['tables'], {a: [wk, bk], 'builtin': [wk, bk],
                                          b: ['not checked', 'measuring it failed: x']})
        self.assertEqual(part['locales']['xx_XX'], ['refused', 'no'])
        self.assertEqual((part['wide'], part['bytes']),
                         ({wk: m.pack(SMALL_WIDE)}, {bk: m.pack(SMALL_BYTES)}))

    def test_the_key_is_of_the_data_not_of_how_it_was_packed(self):
        """zlib-ng packs the same bytes otherwise (RHEL10): the key of a
        table is the digest of what was measured."""
        args = ({'C': ['table', 'builtin']},
                {'C': (SMALL_WIDE, SMALL_BYTES, None, self.own('builtin'))})
        once = self.part(*args)
        with unittest.mock.patch.object(m, 'pack', lambda raw: base64.b64encode(
                zlib.compress(raw, 1)).decode('ascii')):
            again = self.part(*args)
        self.assertEqual(once['tables'], again['tables'])
        self.assertNotEqual(once['wide'], again['wide'])

    def test_a_table_measured_on_other_data(self):
        """The locale's LC_CTYPE changed on disk between the two passes:
        the table was measured on data its identity does not name, and is
        not checked, never filed under the old data (review round 1)."""
        a = 'a' * 64
        for got, why in ((['table', 'b' * 64], m.CTYPE_MOVED),
                         (['not checked', 'x'],
                          'its LC_CTYPE data could not be read again: x')):
            with self.subTest(got=got):
                part = self.part({'de_DE.utf8': ['table', a]},
                                 {'de_DE.utf8': (SMALL_WIDE, SMALL_BYTES, None, got)})
                self.assertEqual(part['tables'], {a: ['not checked', why]})
                self.assertEqual((part['wide'], part['bytes']), ({}, {}))

    def test_the_classes_are_those_postgresql_asks_about(self):
        """isw<class> and is<class> as PostgreSQL 13 to 18 call them:
        alnum, alpha, digit, graph, lower, print, punct, space and upper by
        name, xdigit by name (isxdigit) and through the full-text parser's
        isw##type
        (wparser_def.c:461-469 in REL_13_23, :454-462 in REL_18_6); never
        iswcntrl or iswblank (review round 1: nothing held the list)."""
        self.assertEqual(sorted(m.CTYPE_CLASSES), sorted([
            'alnum', 'alpha', 'digit', 'graph', 'lower', 'print', 'punct',
            'space', 'upper', 'xdigit']))

    def test_the_run_says_when_lc_ctype_was_not_measured(self):
        """finished(): a locale whose data was not read and one whose table
        could not be measured are named, with why, and the exit status is 1,
        as for a locale whose order could not be measured (review round 1)."""
        out = {'locales': {}, 'locales_listed': 3, 'postgres_libc': dict(CLEAN_LIBC),
               'ctype': {'locales': {'a': ['not checked', 'unread'],
                                     'b': ['table', 'b' * 64],
                                     'c': ['table', 'c' * 64],
                                     'd': ['refused', 'no']},
                         'tables': {'b' * 64: ['not checked', 'failed'],
                                    'c' * 64: ['w', 'b']},
                         'wide': {}, 'bytes': {}},
               'converter': NO_CONVERTER_LOCALES, **NO_ROUND_TRIP}
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            rc = m.finished(out, 1, 10)
        self.assertEqual(rc, 1)
        text = screen.getvalue()
        self.assertIn('!! The character rules (LC_CTYPE) of 2 locale(s) could '
                      'not be measured:\n  a: unread\n  b: failed\n', text)
        out['ctype']['locales'] = {'c': ['table', 'c' * 64], 'd': ['refused', 'no']}
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 0)
        self.assertNotIn('LC_CTYPE', screen.getvalue())

    def test_measure_writes_the_part(self):
        """The call site: measure() keeps each locale's identity, measures
        a table once per identity in its pool, and writes the part under
        the parts its format lists."""
        a = 'a' * 64
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: ['a', 'b', 'c'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=lambda name: (name, {'error': 'x'}, None),
                postgres_use=lambda name: use_of('one byte', 'X'),
                ctype_identity=lambda name, use: (
                    ['refused', 'no'] if name == 'c' else ['table', a]),
                ctype_tables=lambda name: (name, SMALL_WIDE, SMALL_BYTES, None,
                                           ['table', a]),
                UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1),
                money_digits=lambda names: {n: 3 for n in names},
                postgres_libc=lambda: dict(CLEAN_LIBC)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', Screen()):
            text, out, _ = m.measure(1)
        data = json.loads(text)
        self.assertIn('ctype', data['parts'])
        self.assertEqual(data['ctype']['locales'], {
            'a': ['table', a], 'b': ['table', a], 'c': ['refused', 'no']})
        self.assertEqual(list(data['ctype']['tables']), [a])
        self.assertTrue(m.ctype_part_whole(data['ctype'], data['locales']))


class CtypeRead(Checks):
    """A file of this format whose part about the character rules of each
    locale is missing, malformed, or says otherwise than its postgres_use
    is refused: read as missing it would say "not checked" where the
    machine did check, and a table that does not unpack to what its key
    names would compare as rules nobody measured."""

    def refused(self, phrase, *edits, form=m.FORMAT, use=None):
        new = self.made(RHEL10, 'new', with_parts(form=form, use=use), *edits)
        err = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.load(new, m.corpus_digest())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn(phrase, flat(err.getvalue()))

    MALFORMED = ('what it says of the character rules (LC_CTYPE) of each '
                 'locale is malformed')

    def test_without_the_part(self):
        self.refused('lacks a part its format holds', lambda d: d.pop('ctype'))

    def test_a_third_format_file_with_the_part(self):
        self.refused('holds one it does not',
                     lambda d: d.update(ctype={}), form='locale_order/3')

    def test_malformed(self):
        wk = hashlib.sha256(SMALL_WIDE).hexdigest()
        bk = hashlib.sha256(SMALL_BYTES).hexdigest()

        def part(f):
            return lambda d: f(d['ctype'])

        def wide(raw):
            """The wide table replaced by raw, keyed and named as its own."""
            key = hashlib.sha256(raw).hexdigest()

            def edit(c):
                c['wide'] = {key: m.pack(raw)}
                for t in c['tables'].values():
                    t[0] = key
            return part(edit)

        def bytes_(raw):
            key = hashlib.sha256(raw).hexdigest()

            def edit(c):
                c['bytes'] = {key: m.pack(raw)}
                for t in c['tables'].values():
                    t[1] = key
            return part(edit)

        def shaped(**k):
            w = json.loads(SMALL_WIDE)
            w.update(k)
            return json.dumps(w).encode('ascii')

        def classes(**k):
            w = json.loads(SMALL_WIDE)
            w['classes'].update(k)
            return json.dumps(w).encode('ascii')
        for case, edit in (
                ('not a map', lambda d: d.update(ctype=[])),
                ('a key missing', part(lambda c: c.pop('bytes'))),
                ('a key more', part(lambda c: c.update(more={}))),
                ('a locale missing', part(lambda c: c['locales'].pop('C'))),
                ('a name more', part(lambda c: c['locales'].update(xx_XX=['table', 'builtin']))),
                ('an entry not a list', part(lambda c: c['locales'].update(C='builtin'))),
                ('an unknown state', part(lambda c: c['locales'].update(C=['maybe', 'x']))),
                ('a state of three items', part(lambda c: c['locales'].update(C=['table', 'builtin', 1]))),
                ('an empty reason', part(lambda c: c['locales'].update(C=['not checked', '']))),
                ('a table no entry has', part(lambda c: c['locales'].update(C=['table', 'e' * 64]))),
                ('a table nobody names', part(lambda c: c['tables'].update(x=[wk, bk]))),
                ('a table of one item', part(lambda c: c['tables'].update(builtin=[wk]))),
                ('a table with no reason', part(lambda c: c['tables'].update(builtin=['not checked', '']))),
                ('a wide table missing', part(lambda c: c['tables'].update(builtin=['f' * 64, bk]))),
                ('a byte table missing', part(lambda c: c['tables'].update(builtin=[wk, 'f' * 64]))),
                ('a wide table nobody names', part(lambda c: c['wide'].update({'f' * 64: m.pack(SMALL_WIDE)}))),
                ('a byte table nobody names', part(lambda c: c['bytes'].update({'f' * 64: m.pack(SMALL_BYTES)}))),
                ('a table that is not base64', part(lambda c: c['wide'].update({wk: '!!'}))),
                ('a table that is not text', part(lambda c: c['wide'].update({wk: 5}))),
                ('a table that is not zlib', part(lambda c: c['wide'].update({wk: base64.b64encode(b'x').decode()}))),
                ('a table that is not its key', part(lambda c: c['wide'].update({wk: m.pack(SMALL_WIDE + b' ')}))),
                ('a byte table that is not its key', part(lambda c: c['bytes'].update({bk: m.pack(SMALL_BYTES[:-1] + b'\x02')}))),
                ('a key not of sha256', wide_key_upper()),
                ('wide: not JSON', wide(b'{')),
                ('wide: a key missing', wide(json.dumps({'upper': [], 'lower': []}).encode())),
                ('wide: a class missing', wide(shaped(classes={'alpha': []}))),
                ('wide: a class more', wide(classes(cntrl=[]))),
                ('wide: a map out of order', wide(shaped(upper=[[0x62, 0x42], [0x61, 0x41]]))),
                ('wide: a code point twice', wide(shaped(upper=[[0x61, 0x41], [0x61, 0x41]]))),
                ('wide: a map onto itself', wide(shaped(upper=[[0x61, 0x61]]))),
                ('wide: a map onto a surrogate', wide(shaped(upper=[[0x61, 0xD800]]))),
                ('wide: a map onto NUL', wide(shaped(lower=[[0x41, 0]]))),
                ('wide: a map past Unicode', wide(shaped(lower=[[0x41, 0x110000]]))),
                ('wide: a code point as a boolean', wide(shaped(upper=[[True, 0x41]]))),
                ('wide: a pair of three', wide(shaped(upper=[[0x61, 0x41, 0]]))),
                ('wide: a run reversed', wide(classes(alpha=[[0x42, 0x41]]))),
                ('wide: runs that touch', wide(classes(alpha=[[0x41, 0x41], [0x42, 0x42]]))),
                ('wide: runs that overlap', wide(classes(alpha=[[0x41, 0x43], [0x42, 0x44]]))),
                ('wide: a run on a surrogate', wide(classes(alpha=[[0xD800, 0xD800]]))),
                ('wide: a run across the surrogates', wide(classes(alpha=[[0xD000, 0xE100]]))),
                ('an identity not of sha256', identity_renamed('zz')),
                ('an identity in capitals', identity_renamed('D' * 64)),
                ('glibc\'s own for another name',
                 part(lambda c: c['locales'].update({'de_DE.utf8': ['table', 'builtin']}))),
                ('data for C', part(lambda c: (c['locales'].update(C=['table', SOME_DATA]),
                                               c['locales'].update(POSIX=['table', SOME_DATA]),
                                               c['tables'].pop('builtin')))),
                ('bytes: one short', bytes_(SMALL_BYTES[:-1])),
                ('bytes: one more', bytes_(SMALL_BYTES + b'\x00')),
                ('bytes: a class of 2', bytes_(SMALL_BYTES[:-1] + b'\x02'))):
            with self.subTest(case=case):
                self.refused(self.MALFORMED, edit)

    def test_each_form_ctype_part_writes_is_read(self):
        """The control: a table, glibc's own, a locale glibc refused or that
        Python could not pass, as postgres_use said, data not read, and a
        table that could not be measured, are whole."""
        use = {'en_US.utf8': use_of('not checked', None, m.NO_CTYPE,
                                    collate='refused', ctype='refused'),
               'ja_JP.utf8': use_of('not checked', None, m.NO_CTYPE,
                                    collate='unknown', ctype='unknown')}

        def edit(d):
            c = d['ctype']
            c['locales']['de_DE.utf8'] = ['not checked', m.CTYPE_UNREAD]
            c['locales']['sv_SE.utf8'] = ['table', 'e' * 64]
            c['tables']['e' * 64] = ['not checked', 'measuring it failed: x']
            # No locale of FEW is left on SOME_DATA.
            del c['tables'][SOME_DATA]
        new = self.made(RHEL10, 'new', with_parts(use=use),
                        selected_not('en_US.utf8', 'ja_JP.utf8'), edit)
        c = m.load(new, m.corpus_digest())[0]['ctype']
        self.assertEqual((c['locales']['C'], c['locales']['en_US.utf8'][0],
                          c['locales']['ja_JP.utf8'][0]),
                         (['table', 'builtin'], 'refused', 'unknown'))

    def test_a_locale_newlocale_refused_is_read_and_named(self):
        """The call sites: the file loads, and ctype_not_checked, which the
        run prints when it ends and which makes its exit 1, names the
        locale (review round 2: it loaded as "glibc changed", and the run
        ended clean)."""
        refused = ['not checked', 'newlocale gave no locale for it: x']
        new = self.made(RHEL10, 'new', with_parts(ctype={'de_DE.utf8': refused}))
        part = m.load(new, m.corpus_digest())[0]['ctype']
        self.assertEqual(part['locales']['de_DE.utf8'], refused)
        self.assertEqual(m.ctype_not_checked(part),
                         {'de_DE.utf8': 'newlocale gave no locale for it: x'})

    def test_what_setlocale_and_newlocale_said_disagrees(self):
        """ctype_identity copies postgres_use's answer for a name setlocale
        did not accept: a file where the two differ contradicts itself."""
        refused = {'de_DE.utf8': use_of('not checked', None, m.NO_CTYPE,
                                        collate='refused', ctype='refused')}
        contradicts = ('says glibc did not accept it for LC_CTYPE, and its '
                       'character rules say otherwise')
        for case, use, entry, phrase in (
                ('a table of a refused locale', refused, ['table', SOME_DATA], contradicts),
                ('not read of a refused locale', refused, ['not checked', 'x'], contradicts),
                ('refused for another reason', refused, ['refused', 'another'], contradicts),
                ('unknown where setlocale refused', refused,
                 ['unknown', 'unsupported locale setting'], contradicts)):
            with self.subTest(case=case):
                self.refused(phrase, selected_not('de_DE.utf8'),
                             lambda d, e=entry: d['ctype']['locales'].update(
                                 {'de_DE.utf8': e}),
                             use=use)


def identity_renamed(new):
    """An edit that files every locale on SOME_DATA under the identity new."""
    def edit(d):
        c = d['ctype']
        for e in c['locales'].values():
            if e == ['table', SOME_DATA]:
                e[1] = new
        c['tables'][new] = c['tables'].pop(SOME_DATA)
    return edit


def wide_key_upper():
    """An edit that names the wide table by its key in capitals."""
    def edit(d):
        c = d['ctype']
        (key, packed), = c['wide'].items()
        c['wide'] = {key.upper(): packed}
        for t in c['tables'].values():
            t[0] = key.upper()
    return edit


# Measured on 2026-10-19 with the standalone probe of T4 step 3, which made
# the calls postgres_use() then made for every encoding, on collaudit8
# (glibc-gconv-extra installed) and collaudit9 (not installed): (encoding,
# text, first character of more than one byte). The same answers as
# PostgreSQL 13 to 18 gave in six own environments (372 cases, 0
# disagreements). In a file of this format a locale of CONVERTER_CODESETS
# holds ['converter', None] instead, and with_parts writes the converter part
# these answers stand for (converter_of_use).
USE_MEASURED = {
    8: {'C': ('ANSI_X3.4-1968', 'one byte', None),
        'POSIX': ('ANSI_X3.4-1968', 'one byte', None),
        'de_DE': ('ISO-8859-1', 'one byte', None),
        'de_DE.utf8': ('UTF-8', 'converts', (0x80, 'c280')),
        'hy_AM.armscii8': ('ARMSCII-8', 'not checked', m.NO_CONVERTER),
        'ja_JP': ('EUC-JP', 'converts', (0xFF61, '8ea1')),
        'ja_JP.eucjp': ('EUC-JP', 'converts', (0xFF61, '8ea1')),
        'ko_KR': ('EUC-KR', 'converts', (0x3000, 'a1a1')),
        'ko_KR.euckr': ('EUC-KR', 'converts', (0x3000, 'a1a1')),
        'th_TH.tis620': ('TIS-620', 'one byte', None),
        'zh_CN': ('GB2312', 'converts', (0x3000, 'a1a1')),
        'zh_CN.gbk': ('GBK', 'converts', (0x4E02, '8140')),
        'zh_TW': ('BIG5', 'converts', (0x3000, 'a140'))},
    9: {'C': ('ANSI_X3.4-1968', 'one byte', None),
        'POSIX': ('ANSI_X3.4-1968', 'one byte', None),
        'de_DE': ('ISO-8859-1', 'one byte', None),
        'de_DE.utf8': ('UTF-8', 'converts', (0x80, 'c280')),
        'hy_AM.armscii8': ('ARMSCII-8', 'not checked', m.NO_CONVERTER),
        'ja_JP': ('EUC-JP', 'fails', (0xFF61, '8ea1')),
        'ja_JP.eucjp': ('EUC-JP', 'fails', (0xFF61, '8ea1')),
        'ko_KR': ('EUC-KR', 'fails', (0x3000, 'a1a1')),
        'ko_KR.euckr': ('EUC-KR', 'fails', (0x3000, 'a1a1')),
        'th_TH.tis620': ('TIS-620', 'one byte', None),
        'zh_CN': ('GB2312', 'fails', (0x3000, 'a1a1')),
        'zh_CN.gbk': ('GBK', 'fails', (0x4E02, '8140')),
        'zh_TW': ('BIG5', 'fails', (0x3000, 'a140'))}}
USE_NAMES = tuple(USE_MEASURED[8])
# The locales of USE_NAMES of an encoding PostgreSQL does not know, whose
# converter RHEL9's glibc cannot load (no glibc-gconv-extra there).
OTHER_9 = {'hy_AM.armscii8': 'ARMSCII-8', 'th_TH.tis620': 'TIS-620'}


def measured_use(machine, **edits):
    """USE_MEASURED of a machine as postgres_use() recorded it before
    the converter part (with_parts rewrites the nine encodings' for this
    format), with edits ({name: record}) in place."""
    out = {name: use_of(text, codeset, char) for name, (codeset, text, char)
           in USE_MEASURED[machine].items()}
    out.update(edits)
    return out


def refused_use(name, machine, collate='refused', ctype='refused'):
    codeset, text, char = USE_MEASURED[machine][name]
    if ctype != 'accepted':
        return use_of('not checked', None, m.NO_CTYPE, collate=collate, ctype=ctype)
    return use_of(text, codeset, char, collate=collate, ctype=ctype)


def selected_not(*names):
    """An edit that records the order of names as measure_one does when
    setlocale refuses them."""
    def edit(d):
        for name in names:
            d['locales'][name] = {'error': 'cannot be selected: unsupported '
                                           'locale setting'}
    return edit


class PostgresUseCompared(Checks):
    """What the comparison says of how PostgreSQL uses each locale, on RHEL8
    and RHEL9 cut to USE_NAMES, with what the two machines measured.

    Freezes: a locale glibc refuses read as unchanged, or only as "could not
    be measured" with nothing said about the database not opening; a
    conversion that fails on one side read as no change; a locale left out
    or not checked dropped from the count; the three outputs naming
    different locales."""

    @classmethod
    def setUpClass(cls):
        super(Checks, cls).setUpClass()
        for path in (RHEL8, RHEL9):
            cls.texts[path] = trimmed(cls.texts[path], USE_NAMES)

    def outputs(self, old, new):
        """(report, summary, file) of the run, each read whole."""
        tag = self.id().rsplit('.', 1)[-1]
        summary = os.path.join(self.tmp, f'{tag}.sum')
        chars = os.path.join(self.tmp, f'{tag}.tsv')
        self.checks(old, new)
        stdout = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', [
                SCRIPT, '--compare', old, new, '--summary-to', summary,
                '--characters-to', chars]), contextlib.redirect_stdout(stdout):
            self.assertEqual(m.main(), 0)
        with open(summary, encoding='utf-8') as f, \
                open(chars, encoding='utf-8') as g:
            return parse_report(stdout.getvalue()), parse_summary(f.read()), \
                parse_characters(g.read())

    def report_text(self, old, new):
        stdout = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', [SCRIPT, '--compare', old, new]), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(m.main(), 0)
        return stdout.getvalue()

    def pair(self, old_use, new_use, old_edits=(), new_edits=(), form=m.FORMAT,
             old_converter=None, new_converter=None):
        return (self.made(RHEL8, 'old', with_parts(use=old_use, form=form,
                                                   converter=old_converter),
                          *old_edits),
                self.made(RHEL9, 'new', with_parts(use=new_use, form=form,
                                                   converter=new_converter),
                          *new_edits))

    def use(self, *pair):
        return [d for kind, d in self.checks(*pair) if kind == 'use']

    def test_rhel8_to_rhel9_as_measured(self):
        self.assertEqual(self.use(*self.pair(measured_use(8), measured_use(9))), [
            {'changed': {'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP',
                         'ko_KR': 'EUC-KR', 'ko_KR.euckr': 'EUC-KR',
                         'zh_CN': 'GB2312'}},
            {'anydb': 'new', 'why': 'its encoding is one PostgreSQL does not know, so a database of any encoding can use it', 'names': OTHER_9},
            {'converts': 1, 'one byte': 3,
             'left out': {'zh_CN.gbk': 'GBK', 'zh_TW': 'BIG5'}}])

    def test_failing_on_both_machines(self):
        self.assertEqual(self.use(*self.pair(measured_use(9), measured_use(9))), [
            {'both': {'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP',
                      'ko_KR': 'EUC-KR', 'ko_KR.euckr': 'EUC-KR',
                      'zh_CN': 'GB2312'}},
            {'anydb': 'both', 'why': 'its encoding is one PostgreSQL does not know, so a database of any encoding can use it', 'names': OTHER_9},
            {'converts': 1, 'one byte': 3,
             'left out': {'zh_CN.gbk': 'GBK', 'zh_TW': 'BIG5'}}])

    def test_working_again_on_the_new_machine(self):
        found = self.use(*self.pair(measured_use(9), measured_use(8)))
        self.assertEqual(found[0], {'back': {
            'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP', 'ko_KR': 'EUC-KR',
            'ko_KR.euckr': 'EUC-KR', 'zh_CN': 'GB2312'}})

    def test_one_side_not_checked_is_named_with_its_side(self):
        new = measured_use(9, **{'de_DE.utf8': use_of('not checked', 'UTF-8',
                                                      'checking failed: x')})
        found = self.use(*self.pair(measured_use(8), new))
        self.assertEqual(found[2], {'unknown': {
            'de_DE.utf8': 'checking failed: x (on the new machine)'}})
        self.assertEqual(found[-1]['converts'], 0)
        # An encoding of one byte never converts: ISO-8859-1 is of one
        # byte in PostgreSQL (pg_wchar_table), so a check that failed on
        # the new changes nothing for de_DE.
        new = measured_use(9, de_DE=use_of('not checked', 'ISO-8859-1',
                                           'checking failed: x'))
        found = self.use(*self.pair(measured_use(8), new))
        self.assertEqual(found[-1]['one byte'], 3)

    def test_an_error_beside_a_file_of_an_earlier_format(self):
        """Review round 1, finding 1: the side that measured an error names
        it; the other side's NOT checked is said beside it, not instead. On
        each side, and in the file's rows."""
        old = self.made(RHEL8, 'old', with_parts(form='locale_order/2'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9)))
        report, summary, rows = self.outputs(old, new)
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertEqual(use, [
            {'earlier': ['OLD']},
            {'anydb': 'new', 'why': 'its encoding is one PostgreSQL does not know, so a database of any encoding can use it', 'names': OTHER_9},
            {'one side': 'new', 'why': 'the OLD file was written by an earlier '
                                       'version of locale_order.py',
             'names': {'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP',
                       'ko_KR': 'EUC-KR', 'ko_KR.euckr': 'EUC-KR',
                       'zh_CN': 'GB2312'}}])
        errors = {names[0]: [(r['before'], r['now'], r['measured']) for r in g
                             if r['finding'] == 'upper/lower/ILIKE error']
                  for names, g in rows.items() if len(names) == 1}
        self.assertEqual(errors['ja_JP'], [('not measured', 'error', 'on the new machine')])
        old = self.made(RHEL8, 'old', with_parts(use=measured_use(9)))
        new = self.made(RHEL9, 'new', with_parts(form='locale_order/2'))
        use = self.use(old, new)
        self.assertEqual([d.get('one side') for d in use], [None, None, 'old'])
        self.assertEqual(len(use[2]['names']), 5)

    def test_an_error_beside_a_side_not_checked(self):
        """Review round 1, finding 2: one side not checked does not hide the
        other side's error; the locale is in that line alone, and the counts
        still add up."""
        why = use_of('not checked', 'GB2312', 'checking failed: x')
        said = "glibc's converter of GB2312 could not be measured: checking failed: x"
        found = self.use(*self.pair(measured_use(8, zh_CN=why), measured_use(9)))
        self.assertEqual(found[2], {'one side': 'new', 'why': said,
                                    'names': {'zh_CN': 'GB2312'}})
        self.assertNotIn('zh_CN', found[0]['changed'])
        self.assertEqual(found[3], {'converts': 1, 'one byte': 3, 'left out': {
            'zh_CN.gbk': 'GBK', 'zh_TW': 'BIG5'}})
        found = self.use(*self.pair(measured_use(9), measured_use(8, zh_CN=why)))
        self.assertEqual(found[2], {'one side': 'old', 'why': said,
                                    'names': {'zh_CN': 'GB2312'}})
        report, summary, rows = self.outputs(*self.pair(measured_use(8, zh_CN=why),
                                                        measured_use(9)))
        self.assertEqual([(r['finding'], r['before'], r['now'], r['measured'])
                          for r in rows[('zh_CN',)]
                          if r['finding'].startswith('upper/')],
                         [('upper/lower/ILIKE error', 'not measured', 'error',
                           'on the new machine')])

    def test_locales_whose_encoding_postgresql_does_not_know(self):
        """Review round 1, findings 3 and 4: a database of another encoding
        can use them, and the run says that nothing measured it, on the
        three outputs; C and POSIX, accepted with every encoding, are not
        named."""
        report, summary, rows = self.outputs(*self.pair(measured_use(8),
                                                        measured_use(9)))
        lines = [d for kind, d in report['checks']
                 if kind == 'bytes' and 'other encoding' in d]
        self.assertEqual([d['other encoding'] for d in lines], [OTHER_9])
        # The old machine measured both converters, which the double has
        # refuse nothing; the new one has neither (named among the errors).
        self.assertEqual(lines[0]['whole'], {cs: 'on the old machine'
                                             for cs in OTHER_9.values()})

    def test_the_encodings_postgresql_knows(self):
        import collation_keys
        known = collation_keys.POSTGRES_CODESETS
        self.assertEqual(len(known), 110)
        self.assertLessEqual(collation_keys.CLIENT_ONLY_CODESETS, known)
        for codeset in ('UTF-8', 'EUC-JP', 'ISO-8859-1', 'KOI8-R', 'GB2312'):
            self.assertIn(codeset.upper(), known)
        for codeset in ('TIS-620', 'KOI8-T', 'PT154', 'ARMSCII-8', 'GEORGIAN-PS',
                        'ANSI_X3.4-1968'):
            self.assertNotIn(codeset.upper(), known)

    def test_two_not_checked_for_one_reason(self):
        why = use_of('not checked', 'EUC-JP', 'checking failed: x')
        utf8 = use_of('not checked', 'UTF-8', 'checking failed: x')
        found = self.use(*self.pair(
            measured_use(8, ja_JP=why, **{'de_DE.utf8': utf8}),
            measured_use(9, ja_JP=why, **{'de_DE.utf8': utf8})))
        said = "glibc's converter of EUC-JP could not be measured: checking failed: x"
        self.assertEqual(found[2], {'unknown': {
            'ja_JP': said, 'ja_JP.eucjp': said, 'de_DE.utf8': 'checking failed: x'}})

    def test_one_byte_is_what_postgresql_says(self):
        """Review round 7, findings 3 and 4: an encoding is of one byte
        when PostgreSQL keeps it so (pg_wchar_table), whatever a probe
        said. A database of ISO-8859-1 never converts, even beside an
        error in BIG5; one of EUC-JP does, and the new machine's error is
        named though the old machine's file said 'one byte'. That file
        tried no character of more than one byte, so what the old machine
        does with the rest of the text is not known there (16.18 c), never
        "converts"."""
        found = self.use(*self.pair(
            measured_use(8, de_DE=use_of('not checked', 'ISO-8859-1', 'checking failed: x')),
            measured_use(9, de_DE=use_of('fails', 'BIG5', (0x3000, 'a140'))),
            new_edits=[lambda d: d['locales']['de_DE'].update(codeset='BIG5')]))
        self.assertNotIn('de_DE', {n for d in found for v in d.values()
                                   if isinstance(v, dict) for n in v})
        self.assertEqual(found[-1]['one byte'], 3)
        found = self.use(*self.pair(
            measured_use(9, ja_JP=use_of('one byte', 'EUC-JP')), measured_use(9),
            form='locale_order/4'))
        self.assertIn({'one side': 'new', 'why': m.UNSAID,
                       'names': {'ja_JP': 'EUC-JP'}}, found)
        self.assertFalse([d for d in found if 'changed' in d])

    def test_a_locale_only_on_the_new_machine(self):
        """Review round 7, finding 1: a locale the old machine does not
        have serves the databases made on the new one. Their error is
        named, and that they could not be checked; that they convert is
        not counted with the locales of both machines."""
        def dropped(d):
            for part in ('locales', 'money', 'postgres_use'):
                d[part].pop('ja_JP.eucjp')
            d['ctype']['locales'].pop('ja_JP.eucjp')
            d['locales_listed'] = len(d['locales'])
            refirst(d)
        found = self.use(*self.pair(measured_use(8), measured_use(9),
                                    old_edits=[dropped]))
        self.assertIn({'one side': 'new', 'absent': True,
                       'names': {'ja_JP.eucjp': 'EUC-JP'}}, found)
        self.assertNotIn('ja_JP.eucjp', found[0]['changed'])
        found = self.use(*self.pair(
            measured_use(8),
            measured_use(9, **{'ja_JP.eucjp': use_of('not checked', 'EUC-JP',
                                                     'checking failed: x')}),
            old_edits=[dropped]))
        self.assertEqual([d['unknown']['ja_JP.eucjp'] for d in found if 'unknown' in d],
                         ["glibc's converter of EUC-JP could not be measured: "
                          'checking failed: x (on the new machine)'])
        found = self.use(*self.pair(measured_use(8), measured_use(8),
                                    old_edits=[dropped]))
        self.assertNotIn('ja_JP.eucjp', {n for d in found for v in d.values()
                                         if isinstance(v, dict) for n in v})
        self.assertEqual(found[-1]['converts'], 5)
        # Of an encoding PostgreSQL does not know: in the line on a
        # database of another encoding, as one on both machines is.
        def gone(d):
            for name in ('th_TH.tis620', 'hy_AM.armscii8'):
                for part in ('locales', 'money', 'postgres_use'):
                    d[part].pop(name)
                d['ctype']['locales'].pop(name)
            d['locales_listed'] = len(d['locales'])
            refirst(d)
        checks = self.checks(*self.pair(measured_use(8), measured_use(8),
                                        old_edits=[gone]))
        self.assertEqual([d for kind, d in checks if kind == 'use'][-1]['elsewhere'], 2)
        self.assertEqual([d['other encoding'] for kind, d in checks
                          if kind == 'bytes' and 'other encoding' in d],
                         [{'hy_AM.armscii8': 'ARMSCII-8', 'th_TH.tis620': 'TIS-620'}])
        # Refused there too: no database with it opens anywhere.
        found = self.use(*self.pair(
            measured_use(8),
            measured_use(9, **{'ja_JP.eucjp': refused_use('ja_JP.eucjp', 9)}),
            old_edits=[dropped], new_edits=[selected_not('ja_JP.eucjp')]))
        self.assertNotIn('ja_JP.eucjp', {n for d in found for v in d.values()
                                         if isinstance(v, dict) for n in v})

    def test_a_refusal_beside_a_side_not_checked(self):
        """Review round 7, finding 2: glibc refused the locale on the old
        machine, and on the new one its text could not be checked: the
        databases made there are NOT checked, beside the refusal. A
        refusal on the new machine says all there is: nothing opens."""
        not_checked = use_of('not checked', 'EUC-JP', 'checking failed: x')
        said = ("glibc's converter of EUC-JP could not be measured: checking "
                'failed: x (on the new machine)')
        report, summary, rows = self.outputs(*self.pair(
            measured_use(8, **{'ja_JP.eucjp': refused_use('ja_JP.eucjp', 8)}),
            measured_use(9, **{'ja_JP.eucjp': not_checked}),
            old_edits=[selected_not('ja_JP.eucjp')]))
        self.assertIn('ja_JP.eucjp', report['refused'])
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertEqual([d['unknown'].get('ja_JP.eucjp') for d in use
                          if 'unknown' in d], [said])
        found = self.use(*self.pair(
            measured_use(8, **{'ja_JP.eucjp': not_checked}),
            measured_use(9, **{'ja_JP.eucjp': refused_use('ja_JP.eucjp', 9)}),
            new_edits=[selected_not('ja_JP.eucjp')]))
        self.assertNotIn('ja_JP.eucjp', {n for d in found for v in d.values()
                                         if isinstance(v, dict) for n in v})

    def test_c_and_posix_never_reach_glibc(self):
        """PostgreSQL lowers text under C or POSIX itself (asc_tolower,
        formatting.c, 13 to 18), so what glibc does with them is no
        finding: counted with the encodings of one byte."""
        c = use_of('not checked', 'ANSI_X3.4-1968', 'checking failed: x')
        fails = use_of('fails', 'ANSI_X3.4-1968', (0x80, 'c280'))
        found = self.use(*self.pair(measured_use(8, C=c, POSIX=fails),
                                    measured_use(9, C=fails, POSIX=c)))
        self.assertFalse({'C', 'POSIX'} & {n for d in found for v in d.values()
                                           if isinstance(v, dict) for n in v})
        self.assertEqual(found[-1]['one byte'], 3)

    def test_left_out_only_where_every_encoding_is_client_only(self):
        """A locale whose encoding is BIG5 on one machine and one PostgreSQL
        allows for a database on the other is compared, not left out."""
        old = measured_use(8)
        old['zh_TW'] = use_of('converts', 'EUC-TW', (0x3000, 'a1a1'))
        def euctw(d):
            d['locales']['zh_TW']['codeset'] = 'EUC-TW'
        found = self.use(*self.pair(old, measured_use(9), old_edits=[euctw]))
        # Review round 6: a database of EUC-TW made on the old machine opens
        # on the new one under BIG5, whose converter glibc lacks there.
        self.assertIn({'moved': 'new', 'from': 'EUC-TW', 'to': 'BIG5',
                       'names': {'zh_TW': 'EUC-TW'}}, found)
        self.assertEqual(found[-1]['left out'], {'zh_CN.gbk': 'GBK'})

    def test_both_errors_beside_a_client_only_one(self):
        """Review round 6: both sides fail and the encoding changed to
        BIG5: the databases of EUC-JP made on the old machine fail there
        and on the new one, each machine's error a row of its own
        character."""
        report, summary, rows = self.outputs(*self.pair(
            measured_use(9), measured_use(9, ja_JP=use_of('fails', 'BIG5', (0x3000, 'a140'))),
            new_edits=[lambda d: d['locales']['ja_JP'].update(codeset='BIG5')]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn({'moved': 'both', 'from': 'EUC-JP', 'to': 'BIG5',
                       'names': {'ja_JP': 'EUC-JP'}}, use)
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         # The new machine's test of one BIG5 character
                         # failed, which use_on reads as glibc falling back
                         # to ASCII: it refuses every byte past ASCII there
                         # too, as the line on that error says (16.19 d).
                         [('8E A1', 'error', 'error', 'on both machines'),
                          # The old machine's glibc cannot convert EUC-JP, so it
                          # refuses every byte past ASCII (review of PR 3).
                          ('A1 40', 'error', 'error', 'on both machines')])

    def test_an_earlier_file_beside_an_encoding_whose_converter_is_missing(self):
        """Review round 6, finding 2: the old file is of an earlier
        format and its order read EUC-TW; on the new machine the locale is
        BIG5 and glibc cannot convert it. The databases of EUC-TW carried
        over fail there, and the line says what is not known of the old."""
        old = self.made(RHEL8, 'old', with_parts(form='locale_order/2'),
                        lambda d: d['locales']['zh_TW'].update(codeset='EUC-TW'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9)))
        self.assertIn({'moved': 'new', 'from': 'EUC-TW', 'to': 'BIG5',
                       'why': 'the OLD file was written by an earlier version of '
                              'locale_order.py',
                       'names': {'zh_TW': 'EUC-TW'}}, self.use(old, new))

    def test_a_change_between_encodings_of_one_byte(self):
        """A database of an encoding of one byte never converts, so a
        locale whose encoding changes between two of them is counted
        with those, not NOT checked."""
        found = self.use(*self.pair(
            measured_use(8), measured_use(9, de_DE=use_of('one byte', 'ISO-8859-15')),
            new_edits=[lambda d: d['locales']['de_DE'].update(codeset='ISO-8859-15')]))
        self.assertEqual(found[-1]['one byte'], 3)
        self.assertNotIn('de_DE', {n for d in found for v in d.values()
                                   if isinstance(v, dict) for n in v})

    def test_an_encoding_postgresql_does_not_know_beside_an_error(self):
        """A database of any encoding can use such a locale; where the
        new machine's glibc cannot convert the locale's own encoding, it
        converts no text that is not ASCII, so every such database fails
        there."""
        found = self.use(*self.pair(measured_use(8), measured_use(8),
                                    new_converter=ARMSCII_MISSING))
        self.assertIn({'anydb': 'new', 'why': 'its encoding is one PostgreSQL '
                       'does not know, so a database of any encoding can use it',
                       'names': {'hy_AM.armscii8': 'ARMSCII-8'}}, found)
        self.assertEqual(found[-1]['elsewhere'], 1)

    def test_an_encoding_of_the_client_before_one_that_fails(self):
        """BIG5 on the old machine (no database there converts with it),
        EUC-TW failing on the new: the databases made on the new machine
        fail."""
        found = self.use(*self.pair(
            measured_use(8), measured_use(9, zh_TW=use_of('fails', 'EUC-TW', (0x3000, 'a1a1'))),
            new_edits=[lambda d: d['locales']['zh_TW'].update(codeset='EUC-TW')]))
        self.assertIn({'one side': 'new', 'why': 'its encoding changed from BIG5 on '
                       'the old machine to EUC-TW on the new one',
                       'names': {'zh_TW': 'EUC-TW'}}, found)

    def test_an_old_error_beside_databases_not_checked_on_the_new(self):
        """An error is the worst a locale's databases can give: the old
        machine's EUC-JP error is named, though the databases made on the
        new machine, in EUC-TW, could not be checked there."""
        found = self.use(*self.pair(
            measured_use(9),
            measured_use(9, ja_JP=use_of('not checked', 'EUC-TW', 'checking failed: x')),
            new_edits=[lambda d: d['locales']['ja_JP'].update(codeset='EUC-TW')]))
        self.assertIn({'one side': 'old', 'why': 'its encoding changed from EUC-JP '
                       'on the old machine to EUC-TW on the new one',
                       'names': {'ja_JP': 'EUC-JP'}}, found)
        self.assertFalse(any('ja_JP' in d.get('unknown', {}) for d in found))

    def test_an_encoding_postgresql_does_not_know_beside_an_old_error(self):
        """The mirror of the test above it: the error the old machine
        measured, named on the old machine."""
        found = self.use(*self.pair(measured_use(8), measured_use(8),
                                    old_converter=ARMSCII_MISSING))
        self.assertIn({'anydb': 'old', 'why': 'its encoding is one PostgreSQL '
                       'does not know, so a database of any encoding can use it',
                       'names': {'hy_AM.armscii8': 'ARMSCII-8'}}, found)

    def test_the_rows_of_the_file(self):
        report, summary, rows = self.outputs(*self.pair(measured_use(8),
                                                        measured_use(9)))
        found = {names[0]: [r for r in group if r['finding'].startswith('upper/')]
                 for names, group in rows.items() if len(names) == 1}
        errors = {n for n, g in found.items() for r in g
                  if r['finding'] == 'upper/lower/ILIKE error'}
        self.assertEqual(errors, {'ja_JP', 'ja_JP.eucjp', 'ko_KR', 'ko_KR.euckr',
                                  'zh_CN', 'hy_AM.armscii8', 'th_TH.tis620'})
        self.assertEqual(found['ja_JP'][0]['chars'], [0xFF61])
        self.assertEqual((found['ja_JP'][0]['bytes'], found['ja_JP'][0]['before'],
                          found['ja_JP'][0]['now']), ('8E A1', 'converts', 'error'))
        # ARMSCII-8 is an encoding PostgreSQL does not know: the new
        # machine's glibc cannot convert it, and a row says so, with what
        # the old machine's converter does with the same bytes (16.18 b:
        # it said "not measured" there before, though the old machine
        # measured them).
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in found['hy_AM.armscii8']],
                         [('80', 'converts', 'error', 'on both machines')])
        # Where the old machine's converter refuses 80, the row names the
        # first byte it converts (named_on_the_other), and says so.
        values = m.unpack_order(zlib.decompress(base64.b64decode(
            converter_values('ARMSCII-8'))))
        values[m.accepted_sequences('ARMSCII-8').index(b'\x80')] = 0
        old = {'ARMSCII-8': ['measured', ['opens', None], 'hy_AM.armscii8',
                             m.pack_order(values), sequences_of('ARMSCII-8')]}
        report, summary, rows = self.outputs(*self.pair(measured_use(8), measured_use(9),
                                                        old_converter=old))
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[('hy_AM.armscii8',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('81', 'converts', 'error', 'on both machines')])
        # Neither machine's converter opens: an error on both, said once.
        report, summary, rows = self.outputs(*self.pair(measured_use(9), measured_use(9)))
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[('hy_AM.armscii8',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('80', 'error', 'error', 'on both machines')])
        # A file of the format before says nothing of those bytes.
        old4 = self.made(RHEL9, 'old4', with_parts(use=measured_use(8),
                                                    form='locale_order/4'))
        report, summary, rows = self.outputs(old4, self.pair(measured_use(8),
                                                             measured_use(9))[1])
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[('hy_AM.armscii8',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('80', 'not measured', 'error', 'on the new machine')])

    def test_refused_on_the_new_machine(self):
        """Two locales, as glibc refuses them on a machine with its
        language packs as folders and no glibc-gconv-extra: named in the
        three outputs, counted in the totals, and never as unchanged or as
        only not measured."""
        new_use = measured_use(9, **{'ja_JP.eucjp': refused_use('ja_JP.eucjp', 9),
                                     'ko_KR.euckr': refused_use('ko_KR.euckr', 9)})
        old, new = self.pair(measured_use(8), new_use,
                             new_edits=[selected_not('ja_JP.eucjp', 'ko_KR.euckr')])
        report, summary, rows = self.outputs(old, new)
        refused = {'ja_JP.eucjp', 'ko_KR.euckr'}
        self.assertEqual({n: e['sides'] for n, e in report['refused'].items()},
                         {n: {'new machine': 'LC_COLLATE and LC_CTYPE'} for n in refused})
        self.assertEqual(summary['refused'], {n: 'the new machine' for n in refused})
        self.assertEqual({names[0]: [r['why'] for r in g
                                     if r['finding'] == 'refused by glibc']
                          for names, g in rows.items()
                          if any(r['finding'] == 'refused by glibc' for r in g)},
                         {n: [('new', 'LC_COLLATE and LC_CTYPE')] for n in refused})
        self.assertFalse(refused & set(report['unmeasured']))
        self.assertFalse(refused & set(summary['unmeasured']))
        self.assertEqual(report['totals']['refused'], 2)
        self.assertEqual(report['totals']['unchanged'] + report['totals']['changed']
                         + report['totals']['not measured'] + 2
                         + report['totals']['not on new'], len(USE_NAMES))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertEqual(set(use[0]['changed']), {'ja_JP', 'ko_KR', 'zh_CN'})
        # Named as refused, and in no line about the text as well.
        self.assertFalse(refused & {name for d in use for v in d.values()
                                    if isinstance(v, dict) for name in v})
        self.assertEqual(use[-1]['converts'] + use[-1]['one byte']
                         + len(use[-1]['left out']) + use[-1].get('elsewhere', 0)
                         + sum(len(d.get(k, {})) for d in use
                               for k in ('changed', 'back', 'both', 'unknown'))
                         + sum(len(d['names']) for d in use if 'anydb' in d),
                         len(USE_NAMES) - len(refused))
        self.assertIsNotNone(report['refused']['ja_JP.eucjp']['also'])

    def test_an_error_beside_a_refusal(self):
        """Review round 2, finding 2: a side glibc refused does not hide
        the error the other side measured; the locale is named as refused,
        and in the line of errors measured on one side."""
        old, new = self.pair(measured_use(8, ja_JP=refused_use('ja_JP', 8)),
                             measured_use(9), old_edits=[selected_not('ja_JP')])
        report, summary, rows = self.outputs(old, new)
        self.assertEqual(report['refused']['ja_JP']['sides'],
                         {'old machine': 'LC_COLLATE and LC_CTYPE'})
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertNotIn('ja_JP', use[0]['changed'])
        self.assertEqual(use[2], {'one side': 'new', 'refused': True,
                                  'names': {'ja_JP': 'EUC-JP'}})
        self.assertEqual([(r['before'], r['now'], r['measured']) for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('not measured', 'error', 'on the new machine')])
        old, new = self.pair(measured_use(9), measured_use(8, ja_JP=refused_use('ja_JP', 8)),
                             new_edits=[selected_not('ja_JP')])
        report, summary, rows = self.outputs(old, new)
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertEqual(use[2], {'one side': 'old', 'refused': True,
                                  'names': {'ja_JP': 'EUC-JP'}})
        self.assertEqual([(r['before'], r['now'], r['measured']) for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('error', 'not measured', 'on the old machine')])

    def test_an_error_beside_an_encoding_that_changed(self):
        """Review round 3, finding 1: the error a side measured is named
        though the encoding changed, on one side and on both. Review round
        5: on one side, it is not known what the other does with that
        encoding, and the line says so."""
        def euckr(d):
            d['locales']['ja_JP']['codeset'] = 'EUC-KR'
        fails = use_of('fails', 'EUC-KR', (0x3000, 'a1a1'))
        report, summary, rows = self.outputs(*self.pair(
            measured_use(8), measured_use(9, ja_JP=fails), new_edits=[euckr]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        # The databases of EUC-JP made on the old machine open on the new
        # one under EUC-KR, whose converter glibc lacks there.
        self.assertIn({'moved': 'new', 'from': 'EUC-JP', 'to': 'EUC-KR',
                       'names': {'ja_JP': 'EUC-JP'}}, use)
        # Review of PR 3, finding 3: the row's bytes are the old machine's
        # own conversion, so the old machine says what it does with them.
        self.assertEqual([(r['before'], r['now'], r['measured']) for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('converts', 'error', 'on both machines')])
        # Both sides failing: one line, under the old encoding, and a row
        # of each machine's character.
        report, summary, rows = self.outputs(*self.pair(
            measured_use(9), measured_use(9, ja_JP=fails), new_edits=[euckr]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn({'moved': 'both', 'from': 'EUC-JP', 'to': 'EUC-KR',
                       'names': {'ja_JP': 'EUC-JP'}}, use)
        self.assertNotIn('ja_JP', use[0].get('both', {}))
        self.assertEqual([(r['before'], r['now'], r['measured']) for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         # Neither machine's glibc converts its locale's
                         # encoding, so each refuses every byte past ASCII,
                         # the other's too (review of PR 3).
                         [('error', 'error', 'on both machines'),
                          ('error', 'error', 'on both machines')])

    def test_an_old_error_beside_an_encoding_that_changed(self):
        """Review round 4, findings 1 and 3: the error the old machine
        measured is named, under the encoding it was measured in."""
        def utf8(d):
            d['locales']['ja_JP']['codeset'] = 'UTF-8'
        report, summary, rows = self.outputs(*self.pair(
            measured_use(9), measured_use(9, ja_JP=use_of('converts', 'UTF-8', (0x80, 'c280'))),
            new_edits=[utf8]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn({'one side': 'old',
                       'why': 'its encoding changed from EUC-JP on the old machine '
                              'to UTF-8 on the new one',
                       'names': {'ja_JP': 'EUC-JP'}}, use)
        self.assertEqual([(r['before'], r['now'], r['measured']) for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('error', 'not measured', 'on the old machine')])

    def test_an_unread_encoding_is_not_left_out(self):
        """Review round 3, finding 3: a side never asked beside a BIG5
        error on the other is named as an error on the side that measured
        one, not left out: databases made on the old machine may exist,
        and the new machine's glibc converts no text that is not ASCII."""
        never = use_of('not checked', None, m.NO_CTYPE, collate='unknown', ctype='unknown')
        found = self.use(*self.pair(
            measured_use(8, zh_TW=never), measured_use(9),
            old_edits=[lambda d: d['locales'].update(
                zh_TW={'error': 'cannot be selected: embedded null'})]))
        self.assertIn({'anydb': 'new', 'why': m.NO_CTYPE + ' (on the old machine)',
                       'names': {'zh_TW': 'BIG5'}}, found)
        self.assertNotIn('zh_TW', found[-1]['left out'])

    def test_a_refusal_beside_a_client_only_error(self):
        """Review round 3: refused on one side, BIG5 failing on the other,
        is named as refused and nowhere as an error: no database was made
        with it on the refusing machine, and one made on the other can
        only be SQL_ASCII, which never converts."""
        report, summary, rows = self.outputs(*self.pair(
            measured_use(8, zh_TW=refused_use('zh_TW', 8)), measured_use(9),
            old_edits=[selected_not('zh_TW')]))
        self.assertIn('zh_TW', report['refused'])
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertFalse({'zh_TW'} & {n for d in use for v in d.values()
                                      if isinstance(v, dict) for n in v})

    def test_refused_on_both_sides_is_not_among_the_other_encodings(self):
        both = refused_use('th_TH.tis620', 8)
        lines = [d['other encoding'] for kind, d in self.checks(*self.pair(
            measured_use(8, **{'th_TH.tis620': both}),
            measured_use(9, **{'th_TH.tis620': refused_use('th_TH.tis620', 9)}),
            old_edits=[selected_not('th_TH.tis620')],
            new_edits=[selected_not('th_TH.tis620')]))
            if kind == 'bytes' and 'other encoding' in d]
        self.assertEqual(lines, [{'hy_AM.armscii8': 'ARMSCII-8'}])

    def test_an_encoding_that_changed(self):
        """Review round 2, finding 3: one character of one encoding against
        one of another says nothing of the database's bytes; NOT checked,
        with both encodings named, and its row in the file."""
        new = measured_use(9, ja_JP=use_of('converts', 'UTF-8', (0x80, 'c280')))
        def utf8(d):
            d['locales']['ja_JP']['codeset'] = 'UTF-8'
        report, summary, rows = self.outputs(*self.pair(measured_use(8), new,
                                                        new_edits=[utf8]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertEqual(use[2]['unknown']['ja_JP'],
                         'Its encoding changed from EUC-JP on the old machine to '
                         'UTF-8 on the new one')
        self.assertNotIn('ja_JP', {n for d in use for k in ('changed', 'back', 'both')
                                   for n in d.get(k, {})})
        self.assertEqual([r['finding'] for r in rows[('ja_JP',)]
                          if r['finding'].startswith('upper/')],
                         ['upper/lower/ILIKE not checked'])

    def test_another_encoding_read_on_either_side(self):
        """Review round 2, finding 3: a locale whose encoding PostgreSQL
        does not know on the new machine alone is named."""
        def koi8t(d):
            d['locales']['de_DE']['codeset'] = 'KOI8-T'
        old, new = self.pair(measured_use(8),
                             measured_use(9, de_DE=use_of('one byte', 'KOI8-T')),
                             new_edits=[koi8t])
        lines = [d['other encoding'] for kind, d in self.checks(old, new)
                 if kind == 'bytes' and 'other encoding' in d]
        self.assertEqual(lines, [{'de_DE': 'KOI8-T', 'hy_AM.armscii8': 'ARMSCII-8',
                                  'th_TH.tis620': 'TIS-620'}])

    def test_refused_on_each_side_and_by_category(self):
        old_use = measured_use(8, **{'de_DE': refused_use('de_DE', 8, ctype='accepted')})
        new_use = measured_use(9, **{
            'de_DE': refused_use('de_DE', 9),
            'th_TH.tis620': refused_use('th_TH.tis620', 9, collate='accepted')})
        report, summary, rows = self.outputs(*self.pair(
            old_use, new_use, old_edits=[selected_not('de_DE')],
            new_edits=[selected_not('de_DE', 'th_TH.tis620')]))
        self.assertEqual({n: e['sides'] for n, e in report['refused'].items()}, {
            'de_DE': {'old machine': 'LC_COLLATE', 'new machine': 'LC_COLLATE and LC_CTYPE'},
            'th_TH.tis620': {'new machine': 'LC_CTYPE'}})
        self.assertEqual(summary['refused'], {'de_DE': 'both machines',
                                              'th_TH.tis620': 'the new machine'})
        # Refused on one side only, so the other side's encoding is read.
        self.assertEqual([d['other encoding'] for kind, d in report['checks']
                          if kind == 'bytes' and 'other encoding' in d],
                         [{'hy_AM.armscii8': 'ARMSCII-8', 'th_TH.tis620': 'TIS-620'}])
        self.assertEqual([r['why'] for r in rows[('de_DE',)]
                          if r['finding'] == 'refused by glibc'],
                         [('old', 'LC_COLLATE'), ('new', 'LC_COLLATE and LC_CTYPE')])

    def test_a_name_glibc_was_never_asked_about_is_not_refused(self):
        new_use = measured_use(9, de_DE=refused_use('de_DE', 9, collate='unknown',
                                                    ctype='unknown'))
        report, summary, rows = self.outputs(*self.pair(
            measured_use(8), new_use, new_edits=[selected_not('de_DE')]))
        self.assertEqual(report['refused'], {})
        self.assertIn('de_DE', report['unmeasured'])
        unknown = [d['unknown'] for kind, d in report['checks']
                   if kind == 'use' and 'unknown' in d]
        self.assertEqual(unknown[0]['de_DE'], m.NO_CTYPE + ' (on the new machine)')

    def test_refused_beside_a_file_of_an_earlier_format(self):
        """The side that measured it still names its refusals; what PostgreSQL
        does with the text is NOT checked, naming the other side."""
        new_use = measured_use(9, **{'ja_JP.eucjp': refused_use('ja_JP.eucjp', 9)})
        old = self.made(RHEL8, 'old', with_parts(form='locale_order/2'))
        new = self.made(RHEL9, 'new', with_parts(use=new_use),
                        selected_not('ja_JP.eucjp'))
        report, summary, rows = self.outputs(old, new)
        self.assertEqual(set(report['refused']), {'ja_JP.eucjp'})
        self.assertEqual([d for kind, d in report['checks'] if kind == 'use'], [
            {'earlier': ['OLD']},
            {'anydb': 'new', 'why': 'its encoding is one PostgreSQL does not know, so a database of any encoding can use it', 'names': OTHER_9},
            {'one side': 'new', 'why': 'the OLD file was written by an earlier '
                                       'version of locale_order.py',
             'names': {'ja_JP': 'EUC-JP', 'ko_KR': 'EUC-KR',
                       'ko_KR.euckr': 'EUC-KR', 'zh_CN': 'GB2312'}}])

    def bytes_lines(self, *pair):
        return [d for kind, d in self.checks(*pair) if kind == 'bytes']

    def test_only_the_line_on_another_encoding_is_left(self):
        """What PostgreSQL stores in EUC-JP, EUC-KR and TIS-620 is in the
        strings measured now (every sequence such a database accepts, and
        every byte of TIS-620), so no line or row is left about it; the
        line on databases of another encoding stays."""
        pair = self.pair(measured_use(8), measured_use(9))
        found = self.bytes_lines(*pair)
        found[0].pop('lacking'), found[0].pop('whole')
        self.assertEqual(found, [
            {'other encoding': {'hy_AM.armscii8': 'ARMSCII-8',
                                'th_TH.tis620': 'TIS-620'}}])
        # A2 E8 (U+327E in an EUC_KR database), the EUC_JP sequences of
        # NEC row 13 and the WIN874 no-break space are among the strings.
        for codeset, wanted in (('EUC-KR', [b'\xa2\xe8']),
                                ('EUC-JP', [b'\xad\xe2', b'\xad\xa1',
                                            b'\x8f\xf3\xf3']),
                                ('TIS-620', [b'\xa0'])):
            with self.subTest(codeset=codeset):
                strings = m.stored_strings(codeset, 1 if codeset == 'TIS-620' else 3)[0]
                self.assertTrue(set(wanted) <= set(strings))


# A wide table of LC_CTYPE built from the classes of each code point, as
# ctype_tables() writes one: ASCII letters, digits and space as glibc
# classes them, U+0282 a small letter with no capital, U+1063 and U+1064
# punctuation, and nothing else.
def rules(upper=None, lower=None, add=None, drop=None):
    """The wide table of BASE_CLASSES with the maps upper and lower (pairs
    added to the base's) and the code points of add joined to and of drop
    taken from the classes named ({class: [code point]})."""
    classes = {k: set(v) for k, v in BASE_CLASSES.items()}
    for k, cps in (add or {}).items():
        classes[k] |= set(cps)
    for k, cps in (drop or {}).items():
        classes[k] -= set(cps)
    up = dict(BASE_UPPER)
    up.update(upper or {})
    lo = dict(BASE_LOWER)
    lo.update(lower or {})
    return json.dumps(
        {'upper': sorted([c, u] for c, u in up.items() if c != u),
         'lower': sorted([c, u] for c, u in lo.items() if c != u),
         'classes': {k: m.code_point_runs(sorted(classes[k]))
                     for k in m.CTYPE_CLASSES}},
        sort_keys=True, separators=(',', ':')).encode('ascii')


LETTERS = set(range(0x41, 0x5B)) | set(range(0x61, 0x7B)) | {0x282}
DIGITS = set(range(0x30, 0x3A))
BASE_CLASSES = {
    'alpha': LETTERS, 'upper': set(range(0x41, 0x5B)),
    'lower': set(range(0x61, 0x7B)) | {0x282}, 'digit': DIGITS,
    'space': {0x20}, 'punct': {0x1063, 0x1064}, 'alnum': LETTERS | DIGITS,
    'print': LETTERS | DIGITS | {0x20, 0x1063, 0x1064},
    'graph': LETTERS | DIGITS | {0x1063, 0x1064},
    'xdigit': DIGITS | set(range(0x41, 0x47)) | set(range(0x61, 0x67))}
BASE_UPPER = {c: c - 0x20 for c in range(0x61, 0x7B)}
BASE_LOWER = {c: c + 0x20 for c in range(0x41, 0x5B)}
BASE = rules()
# U+1063 and U+1064 as glibc 2.34 has them: letters, no longer punctuation.
LETTER_CLASSES = ('alpha', 'alnum')


def as_letters(*cps):
    return rules(add={k: cps for k in LETTER_CLASSES}, drop={'punct': cps})


def unclassified(*cps):
    """BASE with code points it did not classify made letters."""
    return rules(add={k: cps for k in LETTER_CLASSES + ('print', 'graph')})


def byte_rules(edits=None):
    """SMALL_BYTES with edits {(row, byte): value} in place, rows as
    CTYPE_BYTE_ROWS names them."""
    raw = bytearray(SMALL_BYTES)
    for (row, b), v in (edits or {}).items():
        raw[m.CTYPE_BYTE_ROWS.index(row) * 256 + b] = v
    return bytes(raw)


def retabled(assign):
    """An edit that puts each locale of assign on the tables it names,
    (wide, bytes), every other locale on a table staying on SMALL_WIDE and
    SMALL_BYTES: identities, tables and the packed tables rebuilt, as
    ctype_part() writes them."""

    def edit(d):
        c = d['ctype']
        tables, wide, byte = {}, {}, {}
        for name, entry in c['locales'].items():
            if entry[0] != 'table':
                continue
            w, b = assign.get(name, (SMALL_WIDE, SMALL_BYTES))
            if entry[1] != 'builtin':
                entry[1] = hashlib.sha256(w + b'/' + b).hexdigest()
            wk, bk = hashlib.sha256(w).hexdigest(), hashlib.sha256(b).hexdigest()
            tables[entry[1]] = [wk, bk]
            wide[wk], byte[bk] = m.pack(w), m.pack(b)
        c.update(tables=tables, wide=wide, bytes=byte)
    return edit


def table_not_checked(name, why):
    """An edit that marks the table of name as one that could not be
    measured, its packed tables dropped when nothing else names them."""
    def edit(d):
        c = d['ctype']
        c['tables'][c['locales'][name][1]] = ['not checked', why]
        named = [t for t in c['tables'].values() if t[0] != 'not checked']
        c['wide'] = {k: v for k, v in c['wide'].items() if k in {t[0] for t in named}}
        c['bytes'] = {k: v for k, v in c['bytes'].items() if k in {t[1] for t in named}}
    return edit


def entry_set(name, entry):
    def edit(d):
        d['ctype']['locales'][name] = list(entry)
    return edit


FOUR = ['de_DE.utf8', 'en_US.utf8', 'ja_JP.utf8', 'sv_SE.utf8']


class CtypeCompared(Checks):
    """What the comparison says of the character rules (LC_CTYPE), on RHEL9
    and RHEL10 cut to FEW, each locale on the tables a test gives it.

    Freezes: a change read as none; a character the old machine classified
    left unnamed, or one it did not named or dropped from the count; a class
    or a map left out of the comparison; locales of two pairs of tables
    under one line, or one change twice; a locale not checked on either side
    read as the same, or dropped; a file of an earlier format read as the
    same; the three outputs naming different characters or locales; the
    characters file naming a group's locales on every row (61 MB for
    2.28 -> 2.34)."""

    def pair(self, old=None, new=None, old_edits=(), new_edits=(), old_use=None,
             new_use=None):
        """Two files of this format, the locales of old and new ({name:
        (wide, bytes)}) on those tables, every other on SMALL_WIDE; the
        edits after."""
        return (self.made(RHEL9, 'old', with_parts(use=old_use),
                          retabled(old or {}), *old_edits),
                self.made(RHEL10, 'new', with_parts(use=new_use),
                          retabled(new or {}), *new_edits))

    def ctype(self, *files):
        return [d for kind, d in self.checks(*files) if kind == 'ctype']

    def outputs(self, old, new):
        """(report, summary, file) of the run, each read whole; checks()
        first, which holds the three to naming the same checks."""
        self.checks(old, new)
        tag = self.id().rsplit('.', 1)[-1]
        summary = os.path.join(self.tmp, f'{tag}.sum')
        chars = os.path.join(self.tmp, f'{tag}.tsv')
        with open(summary, encoding='utf-8') as f, \
                open(chars, encoding='utf-8') as g:
            report = self.report_text(old, new, summary, chars)
            return parse_report(report), parse_summary(f.read()), \
                parse_characters(g.read())

    def report_text(self, old, new, summary, chars):
        stdout = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', [
                SCRIPT, '--compare', old, new, '--summary-to', summary,
                '--characters-to', chars]), contextlib.redirect_stdout(stdout):
            self.assertEqual(m.main(), 0)
        return stdout.getvalue()

    def every(self, table, byte=SMALL_BYTES):
        return {name: (table, byte) for name in FOUR}

    SAME = [{'same': 'character', 'count': 4, 'other': False, 'utf8': True},
            {'same': 'byte', 'count': 4, 'other': False, 'utf8': False}]

    def test_the_same_rules(self):
        self.assertEqual(self.ctype(*self.pair(self.every(BASE), self.every(BASE))),
                         self.SAME)

    def test_a_classified_character_changes_class(self):
        found = self.ctype(*self.pair(self.every(BASE),
                                      self.every(as_letters(0x1063))))
        self.assertEqual(found, [
            {'all': 4, 'change': 'character', 'count': 1,
             'classified': {0x1063: 'punctuation -> letter'}, 'other': 0},
            {'where': 'character'},
            {'same': 'byte', 'count': 4, 'other': False, 'utf8': False}])

    def test_an_unclassified_character_is_counted_not_named(self):
        """U+AB69, which glibc 2.28 does not classify: regular expressions,
        initcap() and full-text search read it otherwise on 2.34 all the
        same (measured on PostgreSQL), so it counts."""
        found = self.ctype(*self.pair(self.every(BASE),
                                      self.every(unclassified(0xAB69))))
        self.assertEqual(found[0], {'all': 4, 'change': 'character', 'count': 1,
                                    'classified': {}, 'other': 1})

    def test_two_of_each_in_the_three_outputs(self):
        new = rules(add={'alpha': [0x1063, 0x1064, 0xAB69, 0xAB6A],
                         'alnum': [0x1063, 0x1064, 0xAB69, 0xAB6A],
                         'print': [0xAB69, 0xAB6A], 'graph': [0xAB69, 0xAB6A]},
                    drop={'punct': [0x1063, 0x1064]})
        report, summary, rows = self.outputs(*self.pair(self.every(BASE),
                                                        self.every(new)))
        found = [d for kind, d in report['checks'] if kind == 'ctype']
        self.assertEqual(found[0]['classified'], {0x1063: 'punctuation -> letter',
                                                  0x1064: 'punctuation -> letter'})
        self.assertEqual(found[0]['other'], 2)
        self.assertEqual(report['checks'], summary['checks'])
        self.assertEqual(rows.ctype_groups, {'LC_CTYPE_group_1': (tuple(FOUR), 'character')})
        self.assertEqual([(r['chars'], r['before'], r['how'])
                          for r in rows[('LC_CTYPE_group_1',)]], [
            ('U+1063', 'classified',
             'classes: punct print graph -> alpha alnum print graph'),
            ('U+1064', 'classified',
             'classes: punct print graph -> alpha alnum print graph'),
            ('U+AB69', 'not classified', 'classes: none -> alpha alnum print graph'),
            ('U+AB6A', 'not classified', 'classes: none -> alpha alnum print graph')])

    def test_each_locale_is_named_once_in_the_file(self):
        """Its group's row names it; the rows of the characters and bytes
        stand under the group's label."""
        changed = byte_rules({('alpha', 0xB5): 1})
        report, summary, rows = self.outputs(*self.pair(
            self.every(BASE), self.every(as_letters(0x1063), changed)))
        self.assertEqual(rows.ctype_groups, {
            'LC_CTYPE_group_1': (tuple(FOUR), 'character'),
            'LC_CTYPE_group_2': (tuple(FOUR), 'byte')})
        for name in FOUR:
            self.assertEqual(sum(name in names for names, group in rows.items()
                                 if any(r['finding'].startswith('LC_CTYPE')
                                        for r in group)), 1)
        self.assertEqual([r['chars'] for r in rows[('LC_CTYPE_group_2',)]], ['byte B5'])

    def test_the_upper_and_lower_case(self):
        """U+0282 gains a capital in glibc 2.34 (U+A7C5), and U+A7C5, which
        2.28 did not classify, the small letter."""
        new = rules(upper={0x282: 0xA7C5}, lower={0xA7C5: 0x282})
        report, summary, rows = self.outputs(*self.pair(self.every(BASE),
                                                        self.every(new)))
        found = [d for kind, d in report['checks'] if kind == 'ctype']
        self.assertEqual(found[0]['classified'], {0x282: 'gains the capital U+A7C5'})
        self.assertEqual(found[0]['other'], 1)
        self.assertEqual([r['how'] for r in rows[('LC_CTYPE_group_1',)]],
                         ['upper: itself -> U+A7C5', 'lower: itself -> U+0282'])
        found = self.ctype(*self.pair(self.every(BASE),
                                      self.every(rules(upper={0x61: 0x61}))))
        # A capital this terminal can print is shown as itself.
        self.assertEqual(found[0]['classified'], {0x61: 'loses the capital A (U+0041)'})
        # Mapped and in no class on the old machine: classified all the same.
        found = self.ctype(*self.pair(self.every(rules(upper={0x345: 0x399})),
                                      self.every(BASE)))
        self.assertEqual((found[0]['classified'], found[0]['other']),
                         ({0x345: 'loses the capital U+0399'}, 0))

    def test_each_class_on_its_own(self):
        for k in m.CTYPE_CLASSES:
            with self.subTest(cls=k):
                found = self.ctype(*self.pair(
                    self.every(BASE), self.every(rules(add={k: [0xAB69]}))))
                self.assertEqual((found[0]['count'], found[0]['other']), (1, 1))

    def test_what_each_change_reads(self):
        """ctype_how: the kind a class makes, the case maps, and a class
        the kind does not account for, in the report's words and the file's."""
        def how(maps, was, now, cp=0x61):
            return m.ctype_how(cp, maps, was, now, lambda c: f'<{c:X}>',
                               lambda c: f'[{c:X}]')
        same = ((0x61, 0x61), (0x61, 0x61))
        for case, args, words, exact in (
                ('a kind', (same, ['punct', 'print'], ['alpha', 'print']),
                 'punctuation -> letter', 'classes: punct print -> alpha print'),
                ('a kind and the lower class',
                 (same, ['punct'], ['alpha', 'lower']),
                 'punctuation -> letter, joins [[:lower:]]',
                 'classes: punct -> alpha lower'),
                ('one class, the kind the same',
                 (same, ['alpha', 'print'], ['alpha', 'print', 'graph']),
                 'joins [[:graph:]]', 'classes: alpha print -> alpha print graph'),
                ('no class', (same, ['print'], []),
                 'another printable character -> no class', 'classes: print -> none'),
                ('gains a capital', (((0x61, 0x41), (0x61, 0x61)), [], []),
                 'gains the capital <41>', 'upper: itself -> [41]'),
                ('loses a small letter', (((0x61, 0x61), (0x62, 0x61)), [], []),
                 'loses the small letter <62>', 'lower: [62] -> itself'),
                ('another capital', (((0x41, 0x42), (0x61, 0x61)), [], []),
                 'capital <41> -> <42>', 'upper: [41] -> [42]')):
            with self.subTest(case=case):
                self.assertEqual(how(*args), (words, exact))

    def test_locales_on_two_tables(self):
        new = {'de_DE.utf8': (as_letters(0x1063), SMALL_BYTES),
               'en_US.utf8': (as_letters(0x1063), SMALL_BYTES),
               'ja_JP.utf8': (as_letters(0x1064), SMALL_BYTES),
               'sv_SE.utf8': (as_letters(0x1064), SMALL_BYTES)}
        found = self.ctype(*self.pair(self.every(BASE), new))
        self.assertEqual([(d['names'], list(d['classified'])) for d in found[:2]],
                         [(['de_DE.utf8', 'en_US.utf8'], [0x1063]),
                          (['ja_JP.utf8', 'sv_SE.utf8'], [0x1064])])

    def test_two_pairs_of_tables_that_change_alike(self):
        """The Turkish and Ethiopic tables differ from the common one, and
        change alike from glibc 2.28 to 2.34: one line for all of them."""
        turkish = rules(upper={0x69: 0x130})
        turkish_new = rules(upper={0x69: 0x130},
                            add={k: [0x1063] for k in LETTER_CLASSES},
                            drop={'punct': [0x1063]})
        old = {'de_DE.utf8': (BASE, SMALL_BYTES), 'en_US.utf8': (BASE, SMALL_BYTES),
               'ja_JP.utf8': (turkish, SMALL_BYTES), 'sv_SE.utf8': (turkish, SMALL_BYTES)}
        new = {'de_DE.utf8': (as_letters(0x1063), SMALL_BYTES),
               'en_US.utf8': (as_letters(0x1063), SMALL_BYTES),
               'ja_JP.utf8': (turkish_new, SMALL_BYTES),
               'sv_SE.utf8': (turkish_new, SMALL_BYTES)}
        found = self.ctype(*self.pair(old, new))
        self.assertEqual([d.get('all') for d in found if 'change' in d], [4])

    def test_one_locale_changes(self):
        found = self.ctype(*self.pair(
            self.every(BASE), dict(self.every(BASE), **{
                'sv_SE.utf8': (as_letters(0x1063), SMALL_BYTES)})))
        self.assertEqual([found[0]['names'], found[1:]], [['sv_SE.utf8'], [
            {'where': 'character'},
            {'same': 'character', 'count': 3, 'other': True, 'utf8': True},
            {'same': 'byte', 'count': 4, 'other': False, 'utf8': False}]])

    def test_bytes(self):
        """A database of one byte per character reads the byte tables; a
        change there is named apart from the wide table's."""
        changed = byte_rules({('toupper', 0xB5): 0xCC, ('alpha', 0xB5): 1})
        report, summary, rows = self.outputs(*self.pair(
            self.every(BASE), self.every(BASE, changed)))
        found = [d for kind, d in report['checks'] if kind == 'ctype']
        self.assertEqual(found, [
            {'all': 4, 'change': 'byte', 'count': 1,
             'bytes': {0xB5: 'no class -> letter, gains the capital 0xCC'}},
            {'where': 'byte'},
            {'same': 'character', 'count': 4, 'other': False, 'utf8': True}])
        self.assertEqual([(r['chars'], r['how']) for r in rows[('LC_CTYPE_group_1',)]],
                         [('byte B5', 'upper: itself -> CC; classes: none -> alpha')])

    def test_every_byte_row_counts(self):
        """Each of the 12 rows of a byte table on its own: toupper, tolower
        and the ten classes."""
        for row in m.CTYPE_BYTE_ROWS:
            with self.subTest(row=row):
                changed = byte_rules({(row, 0xB5): 0xB6 if row.startswith('to') else 1})
                found = self.ctype(*self.pair(self.every(BASE),
                                              self.every(BASE, changed)))
                self.assertEqual(list(found[0]['bytes']), [0xB5])

    def test_files_of_earlier_formats(self):
        old3 = self.made(RHEL9, 'old3', with_parts(form='locale_order/3'))
        new3 = self.made(RHEL10, 'new3', with_parts(form='locale_order/3'))
        old, new = self.pair(self.every(BASE), self.every(as_letters(0x1063)))
        for case, files, sides in (('old', (old3, new), ['OLD']),
                                   ('new', (old, new3), ['NEW']),
                                   ('both', (old3, new3), ['OLD', 'NEW'])):
            with self.subTest(case=case):
                self.assertEqual(self.ctype(*files), [{'earlier': sides}])

    def test_a_locale_not_checked_on_one_side(self):
        """Not checked on either side is its own state: never the same,
        never dropped, and the others are still compared."""
        why = ['not checked', 'newlocale gave no locale for it: x']
        for side in ('old', 'new'):
            with self.subTest(side=side):
                edits = [entry_set('de_DE.utf8', why), retabled(
                    self.every(BASE) if side == 'old'
                    else self.every(as_letters(0x1063)))]
                found = self.ctype(*self.pair(
                    self.every(BASE), self.every(as_letters(0x1063)),
                    **{f'{side}_edits': edits}))
                self.assertEqual(found[0]['all'], 3)
                self.assertIn({'unknown': {'de_DE.utf8': {f'{side} machine': why[1]}}},
                              found)

    def test_not_checked_on_both_sides(self):
        why = ['not checked', 'x']
        found = self.ctype(*self.pair(
            self.every(BASE), self.every(BASE),
            old_edits=[entry_set('de_DE.utf8', why), retabled(self.every(BASE))],
            new_edits=[entry_set('de_DE.utf8', why), retabled(self.every(BASE))]))
        self.assertEqual(found, [
            {'same': 'character', 'count': 3, 'other': False, 'utf8': True},
            {'same': 'byte', 'count': 3, 'other': False, 'utf8': False},
            {'unknown': {'de_DE.utf8': {'both machines': 'x'}}}])
        found = self.ctype(*self.pair(
            self.every(BASE), self.every(BASE),
            old_edits=[entry_set('de_DE.utf8', why), retabled(self.every(BASE))],
            new_edits=[entry_set('de_DE.utf8', ['not checked', 'y']),
                       retabled(self.every(BASE))]))
        self.assertEqual(found[-1], {'unknown': {'de_DE.utf8': {
            'old machine': 'x', 'new machine': 'y'}}})

    def test_a_table_not_checked(self):
        for side in ('old', 'new'):
            with self.subTest(side=side):
                edit = table_not_checked('de_DE.utf8', 'measuring it failed: x')
                old, new = self.pair(
                    dict(self.every(BASE), **{'de_DE.utf8': (unclassified(0xAB6A),
                                                            SMALL_BYTES)}),
                    dict(self.every(as_letters(0x1063)), **{
                        'de_DE.utf8': (unclassified(0xAB69), SMALL_BYTES)}),
                    **{f'{side}_edits': [edit]})
                found = self.ctype(old, new)
                self.assertEqual(found[0]['all'], 3)
                self.assertIn({'unknown': {'de_DE.utf8': {
                    f'{side} machine': 'measuring it failed: x'}}}, found)

    def test_a_locale_python_could_not_pass(self):
        use = {'de_DE.utf8': use_of('not checked', None, m.NO_CTYPE,
                                    collate='unknown', ctype='unknown')}
        old, new = self.pair(self.every(BASE), self.every(BASE), old_use=use,
                             old_edits=[selected_not('de_DE.utf8'),
                                        retabled(self.every(BASE))])
        self.assertEqual(self.ctype(old, new), [
            {'same': 'character', 'count': 3, 'other': False, 'utf8': True},
            {'same': 'byte', 'count': 3, 'other': False, 'utf8': False},
            {'unknown': {'de_DE.utf8': {'old machine': 'unsupported locale setting'}}}])

    def test_a_refused_locale_is_named_as_refused(self):
        """Refused on one side: named as refused, not compared, not counted
        as the same."""
        use = {'de_DE.utf8': use_of('not checked', None, m.NO_CTYPE,
                                    collate='refused', ctype='refused')}
        for side in ('old', 'new'):
            with self.subTest(side=side):
                report, summary, rows = self.outputs(*self.pair(
                    self.every(BASE), self.every(as_letters(0x1063)),
                    **{f'{side}_use': use,
                       f'{side}_edits': [selected_not('de_DE.utf8'), retabled(
                           self.every(BASE) if side == 'old'
                           else self.every(as_letters(0x1063)))]}))
                self.assertIn('de_DE.utf8', report['refused'])
                found = [d for kind, d in report['checks'] if kind == 'ctype']
                self.assertEqual(found[0]['all'], 3)
                self.assertNotIn('unknown', [k for d in found for k in d])

    def test_c_and_posix_are_not_compared(self):
        """PostgreSQL handles them itself: four compared of FEW's six."""
        found = self.ctype(*self.pair(self.every(BASE), self.every(BASE)))
        self.assertEqual([d['count'] for d in found], [4, 4])

    def test_a_locale_on_one_machine_only(self):
        def dropped(d):
            for part in ('locales', 'money', 'postgres_use'):
                d[part].pop('de_DE.utf8')
            d['ctype']['locales'].pop('de_DE.utf8')
            d['locales_listed'] = len(d['locales'])
            refirst(d)
        for side in ('old', 'new'):
            with self.subTest(side=side):
                edits = [dropped, retabled(self.every(BASE) if side == 'old'
                                           else self.every(as_letters(0x1063)))]
                found = self.ctype(*self.pair(self.every(BASE),
                                              self.every(as_letters(0x1063)),
                                              **{f'{side}_edits': edits}))
                self.assertEqual(found[0]['all'], 3)

    def test_the_order_of_the_lines(self):
        new = {'de_DE.utf8': (as_letters(0x1063), SMALL_BYTES),
               'en_US.utf8': (BASE, byte_rules({('alpha', 0xB5): 1})),
               'ja_JP.utf8': (BASE, SMALL_BYTES), 'sv_SE.utf8': (BASE, SMALL_BYTES)}
        found = self.ctype(*self.pair(
            self.every(BASE), new,
            new_edits=[entry_set('sv_SE.utf8', ['not checked', 'x']), retabled(new)]))
        self.assertEqual(found, [
            {'names': ['de_DE.utf8'], 'change': 'character', 'count': 1,
             'classified': {0x1063: 'punctuation -> letter'}, 'other': 0},
            {'names': ['en_US.utf8'], 'change': 'byte', 'count': 1,
             'bytes': {0xB5: 'no class -> letter'}},
            {'where': 'character and byte'},
            {'same': 'character', 'count': 2, 'other': True, 'utf8': True},
            {'same': 'byte', 'count': 2, 'other': True, 'utf8': False},
            {'unknown': {'sv_SE.utf8': {'new machine': 'x'}}}])

    def test_where_the_characters_are(self):
        """The path of the characters file whole on a line, never cut; and
        without the file, how to get it."""
        old, new = self.pair(self.every(BASE), self.every(as_letters(0x1063)))
        deep = os.path.join(self.tmp, 'd' * 90)
        os.mkdir(deep)
        chars = os.path.join(deep, 'characters.tsv')
        text = self.report_text(old, new, os.path.join(self.tmp, 'w.sum'), chars)
        self.assertIn('\n   ' + chars + '.\n', text)
        stdout = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', [SCRIPT, '--compare', old, new]), \
                contextlib.redirect_stdout(stdout):
            m.main()
        self.assertIn('Pass --characters-to FILE for every such character.',
                      stdout.getvalue())


# USE_NAMES and zh_TW.euctw, which Python cannot convert: a locale of each
# EUC encoding, of one byte, of encodings PostgreSQL does not know and of
# encodings only its clients use.
EUC_NAMES = USE_NAMES + ('zh_TW.euctw',)
EUC_TW_USE = {'zh_TW.euctw': use_of('not checked', 'EUC-TW', m.NO_CONVERTER)}


def small_with(cls=None, cps=(), upper=None):
    """SMALL_WIDE with the code points of cps joined to cls and upper's
    pairs added to its upper case map: a table that differs from
    SMALL_WIDE at those alone."""
    w = json.loads(SMALL_WIDE)
    if cls:
        w['classes'][cls] = m.code_point_runs(sorted(
            {c for r in w['classes'][cls] for c in range(r[0], r[1] + 1)} | set(cps)))
    w['upper'] = sorted(w['upper'] + [list(p) for p in (upper or {}).items()])
    return json.dumps(w, sort_keys=True, separators=(',', ':')).encode('ascii')


# The locales of EUC_NAMES of an EUC encoding, as glibc names it.
EUC_OF = {'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP', 'ko_KR': 'EUC-KR',
          'ko_KR.euckr': 'EUC-KR', 'zh_CN': 'GB2312', 'zh_TW.euctw': 'EUC-TW'}


class CtypeComparedInEuc(Checks):
    """The locales of other encodings than UTF-8, on RHEL8 and RHEL9 cut to
    EUC_NAMES: the lines on EUC databases, read from each machine's
    converter part, with each of EUC-JP, EUC-KR, GB2312 (EUC_CN) and
    EUC-TW; and the characters, compared only in the locales a UTF8
    database can use, those of UTF-8 or of an encoding PostgreSQL does not
    know (check_encoding_locale_matches).

    Freezes: a sequence the two machines convert to two characters read as
    the same, or one either refuses read as a change; a change of rules of
    a character both convert to alike left out, or one of a character no
    such database reaches named; a machine whose converter does not open
    read as comparing more than ASCII; a converter not measured, a file of
    the format before the part, or an encoding that changed, read as the
    same; the three outputs and the characters file naming different
    sequences or locales."""

    @classmethod
    def setUpClass(cls):
        super(Checks, cls).setUpClass()
        for path in (RHEL8, RHEL9):
            cls.texts[path] = trimmed(cls.texts[path], EUC_NAMES)

    def pair(self, old_edits=(), new_edits=(), new_tables=None, old_use=8,
             new_use=9, old_converter=None, new_converter=None):
        """RHEL8 and RHEL9 as old_use and new_use measured how PostgreSQL
        uses each locale (8: every converter opens; 9: none does), the
        converter entries given in place; the new machine's locales on
        new_tables, the rest on SMALL_WIDE; the edits after."""
        def use(u):
            return u if isinstance(u, dict) else measured_use(u, **EUC_TW_USE)
        old = self.made(RHEL8, 'old', with_parts(use=use(old_use),
                                                 converter=old_converter),
                        *old_edits)
        new = self.made(RHEL9, 'new', with_parts(use=use(new_use),
                                                 converter=new_converter),
                        retabled(new_tables or {}), *new_edits)
        return old, new

    def euc(self, *pair):
        return [d for kind, d in self.checks(*pair)
                if kind == 'ctype' and any(k.startswith('euc') for k in d)]

    def rows(self, *pair):
        """The checks of the run, and its characters file read."""
        found = self.checks(*pair)
        with open(os.path.join(self.tmp, f'{self.id().rsplit(".", 1)[-1]}.tsv'),
                  encoding='utf-8') as f:
            return found, parse_characters(f.read())

    def entry(self, cs, name, drop=(), other=(), opens=True):
        return ConverterCompared.entry(self, cs, name, drop, other, opens)

    def test_the_euc_locales_are_named(self):
        """RHEL9 without glibc-gconv-extra: only ASCII compared, and the
        line names every EUC locale."""
        self.assertEqual(self.euc(*self.pair()), [
            {'euc ascii': 'new', 'same': True, 'names': EUC_OF}])

    def test_both_machines_convert_alike(self):
        self.assertEqual(self.euc(*self.pair(new_use=8)), [
            {'euc same': EUC_OF, 'other': False}])

    def test_a_machine_whose_converter_does_not_open(self):
        for case, uses, where in (('old', (9, 8), 'old'), ('new', (8, 9), 'new'),
                                  ('both', (9, 9), 'both')):
            with self.subTest(case=case):
                self.assertEqual(self.euc(*self.pair(old_use=uses[0], new_use=uses[1])),
                                 [{'euc ascii': where, 'same': True, 'names': EUC_OF}])

    def test_a_sequence_converted_to_another_character(self):
        """The new machine's glibc converts A1 A2 of EUC-KR to the character
        after the old one's: named with both characters, in the three
        outputs and as a row under its group in the file; the other locales
        the same."""
        new = self.entry('EUC-KR', 'ko_KR', other=(b'\xa1\xa2', b'\xb0\xa1'))
        found, rows = self.rows(*self.pair(new_use=8, new_converter=new))
        euc = [d for kind, d in found if kind == 'ctype' and any(
            k.startswith('euc') for k in d)]
        self.assertEqual(euc, [
            {'euc converted': {'A1 A2': (0x3001, 0x3002), 'B0 A1': (0xAC00, 0xAC01)},
             'names': {'ko_KR': 'EUC-KR', 'ko_KR.euckr': 'EUC-KR'}},
            {'euc same': {n: c for n, c in EUC_OF.items() if c != 'EUC-KR'},
             'other': True}])
        self.assertIn(('ctype', {'where': 'byte sequence'}), found)
        (label, (names, what)), = rows.ctype_groups.items()
        self.assertEqual((names, what), (('ko_KR', 'ko_KR.euckr'), 'byte sequence'))
        self.assertEqual([(r['chars'], r['how']) for r in rows[(label,)]],
                         [('bytes A1 A2', 'U+3001 -> U+3002'),
                          ('bytes B0 A1', 'U+AC00 -> U+AC01')])

    def test_a_sequence_refused_on_one_machine_is_not_a_change_here(self):
        """A sequence one machine refuses gives an error there, which the
        line on upper() names; it is no character to compare."""
        for side in ('old', 'new'):
            with self.subTest(side=side):
                drop = {f'{side}_converter': self.entry('EUC-KR', 'ko_KR',
                                                        drop=(b'\xa1\xa2',))}
                self.assertEqual(self.euc(*self.pair(new_use=8, **drop)), [
                    {'euc same': EUC_OF, 'other': False}])

    def test_a_character_both_convert_to_whose_rules_change(self):
        """U+3001, which A1 A2 of EUC-JP, EUC-KR and GB2312 converts to on
        both machines, joins [[:punct:]] on the new one: a change in those
        locales, never in EUC-TW's, whose converter here gives no U+3001;
        and U+1063, which no EUC sequence converts to, changes none."""
        euc_three = {n: c for n, c in EUC_OF.items() if c != 'EUC-TW'}
        new = {n: (small_with('punct', [0x3001, 0x1063]), SMALL_BYTES) for n in EUC_OF}
        found, rows = self.rows(*self.pair(new_use=8, new_tables=new))
        euc = [d for kind, d in found if kind == 'ctype' and any(
            k.startswith('euc') for k in d)]
        self.assertEqual(euc, [
            {'euc change': {}, 'other': 1, 'names': euc_three},
            {'euc same': {'zh_TW.euctw': 'EUC-TW'}, 'other': True}])
        label, = [k for k, (names, what) in rows.ctype_groups.items()
                  if names == tuple(sorted(euc_three))]
        self.assertEqual([(r['chars'], r['before']) for r in rows[(label,)]],
                         [('U+3001', 'not classified')])
        # A character the old machine classified is named.
        new = {n: (small_with(upper={0x41: 0x42}), SMALL_BYTES) for n in EUC_OF}
        euc = self.euc(*self.pair(new_use=8, new_tables=new))
        self.assertEqual(euc[0], {'euc change': {0x41: 'gains the capital B (U+0042)'}, 'other': 0,
                                  'names': EUC_OF})

    def test_ascii_alone_where_a_converter_does_not_open(self):
        """The new machine converts ASCII alone: a change of A's rules is
        named, and the line says only ASCII was compared."""
        new = {n: (small_with(upper={0x41: 0x42}), SMALL_BYTES) for n in EUC_OF}
        self.assertEqual(self.euc(*self.pair(new_tables=new)), [
            {'euc change': {0x41: 'gains the capital B (U+0042)'}, 'other': 0, 'names': EUC_OF},
            {'euc ascii': 'new', 'same': False, 'names': EUC_OF}])
        # And a change of a character past ASCII, which that machine never
        # converts to, is none.
        new = {n: (small_with('punct', [0x3001]), SMALL_BYTES) for n in EUC_OF}
        self.assertEqual(self.euc(*self.pair(new_tables=new)), [
            {'euc ascii': 'new', 'same': True, 'names': EUC_OF}])

    def test_a_converter_not_measured(self):
        """Not measured on a side, or its two answers disagreeing: NOT
        checked with why, never the same."""
        entry = {'EUC-KR': ['not checked', ['opens', None], 'ko_KR',
                            'measuring it failed: x', None]}
        said = "glibc's converter of EUC-KR could not be measured: measuring it failed: x"
        disagree = self.entry('EUC-KR', 'ko_KR', opens=False)
        for side in ('old', 'new'):
            for case, conv, why in (
                    ('not checked', entry, said),
                    ('disagree', disagree,
                     "iconv_open said glibc's converter of EUC-KR does not open, "
                     'and glibc converted text past ASCII')):
                with self.subTest(side=side, case=case):
                    euc = self.euc(*self.pair(new_use=8, **{f'{side}_converter': conv}))
                    self.assertEqual(euc, [
                        {'euc same': {n: c for n, c in EUC_OF.items() if c != 'EUC-KR'},
                         'other': False},
                        {'euc unknown': {n: {f'{side} machine': why}
                                         for n in ('ko_KR', 'ko_KR.euckr')}}])

    def test_a_file_of_the_format_before_the_converter_part(self):
        for side in ('old', 'new'):
            with self.subTest(side=side):
                other = 'new' if side == 'old' else 'old'
                files = {other: self.made(RHEL8 if other == 'old' else RHEL9, other,
                                          with_parts(use=measured_use(8, **EUC_TW_USE))),
                         side: self.made(RHEL8 if side == 'old' else RHEL9, side,
                                         with_parts(use=measured_use(8, **EUC_TW_USE),
                                                    form='locale_order/4'))}
                euc = self.euc(files['old'], files['new'])
                why = (f'the {side.upper()} file was written by an earlier version of '
                       "locale_order.py, which did not measure glibc's converter")
                self.assertEqual(euc, [{'euc unknown': {
                    n: {f'{side} machine': why} for n in EUC_OF}}])

    def test_an_encoding_that_changed(self):
        def jp(d):
            d['postgres_use']['ja_JP']['codeset'] = 'EUC-KR'
            d['locales']['ja_JP']['codeset'] = 'EUC-KR'
            refirst(d)
        euc = self.euc(*self.pair(new_use=8, new_edits=[jp]))
        self.assertIn({'euc unknown': {'ja_JP': {
            'both machines': 'its encoding changed from EUC-JP on the old machine '
                             'to EUC-KR on the new one'}}}, euc)

    def test_a_locale_refused_on_a_side_is_named_as_refused(self):
        """Refused on a side: named as refused, not on the lines on EUC."""
        for side in ('old', 'new'):
            with self.subTest(side=side):
                use = measured_use(8, **EUC_TW_USE, ko_KR=refused_use('ko_KR', 8))
                k = {'old_use': 8, 'new_use': 8, f'{side}_use': use,
                     f'{side}_edits': [selected_not('ko_KR')]}
                self.assertEqual(self.euc(*self.pair(**k)),
                    [{'euc same': {n: c for n, c in EUC_OF.items() if n != 'ko_KR'},
                      'other': False}])

    def test_characters_only_where_a_utf8_database_can_use_the_locale(self):
        """Every locale's wide table changes: the line names those of
        UTF-8 and of an encoding PostgreSQL does not know (ARMSCII-8,
        TIS-620), and counts the rest apart; the bytes are compared in all."""
        names = [n for n in EUC_NAMES if n not in ('C', 'POSIX', 'th_TH.tis620')]
        changed = {n: (as_letters(0x1063), SMALL_BYTES) for n in names}
        found = [d for kind, d in self.checks(*self.pair(new_tables=changed))
                 if kind == 'ctype']
        self.assertEqual(found[0]['names'], ['de_DE.utf8', 'hy_AM.armscii8'])
        self.assertIn({'same': 'character', 'count': 1, 'other': True,
                       'utf8': True}, found)
        self.assertIn({'elsewhere': 9, 'with euc': True}, found)
        self.assertIn({'same': 'byte', 'count': 12, 'other': False, 'utf8': False},
                      found)

    def test_an_encoding_not_read(self):
        """Neither the order nor how PostgreSQL uses the locale read its
        encoding: it may be one a UTF8 database can use, so its characters
        are compared."""
        def unread(d):
            d['locales']['de_DE'] = {'error': 'cannot be measured: its encoding '
                                              'could not be read'}
            d['postgres_use']['de_DE'] = use_of('not checked', None, m.NO_CODESET)
        names = [n for n in EUC_NAMES if n not in ('C', 'POSIX', 'th_TH.tis620')]
        changed = {n: (as_letters(0x1063), SMALL_BYTES) for n in names}
        found = [d for kind, d in self.checks(*self.pair(
            old_edits=[unread], new_edits=[unread], new_tables=changed))
            if kind == 'ctype']
        self.assertEqual(found[0]['names'], ['de_DE', 'de_DE.utf8', 'hy_AM.armscii8'])

    def test_utf8_on_one_machine(self):
        """A database made in UTF8 on the old machine opens on the new one
        whatever the new machine calls the locale's encoding, and one made
        on the new machine is of the new machine's."""
        def utf8(d):
            d['locales']['de_DE']['codeset'] = 'UTF-8'
            d['postgres_use']['de_DE'] = use_of('converts', 'UTF-8', (0x80, 'c280'))
            refirst(d)
        names = [n for n in EUC_NAMES if n not in ('C', 'POSIX')]
        changed = {n: (as_letters(0x1063), SMALL_BYTES) for n in names}
        for side in ('old', 'new'):
            with self.subTest(side=side):
                found = [d for kind, d in self.checks(*self.pair(
                    new_tables=changed, **{f'{side}_edits': [utf8]}))
                    if kind == 'ctype']
                self.assertEqual(found[0]['all'], 4)

    def test_us_ascii(self):
        """PostgreSQL reads US-ASCII as SQL_ASCII, which any database
        accepts (chklocale.c:185): a UTF8 database can use such a locale."""
        def ascii_(d):
            d['locales']['de_DE']['codeset'] = 'US-ASCII'
            d['postgres_use']['de_DE'] = use_of('one byte', 'US-ASCII')
        names = [n for n in EUC_NAMES if n not in ('C', 'POSIX')]
        changed = {n: (as_letters(0x1063), SMALL_BYTES) for n in names}
        found = [d for kind, d in self.checks(*self.pair(
            old_edits=[ascii_], new_edits=[ascii_], new_tables=changed))
            if kind == 'ctype']
        self.assertEqual(found[0]['all'], 4)

    def test_the_euc_clause_only_for_euc_locales_left_out(self):
        """The line of the locales left out says nothing of EUC when none
        of them is, and then no line on EUC is printed."""
        def no_euc(d):
            for name in [n for n, r in d['locales'].items()
                         if r.get('codeset') in m.CTYPE_EUC]:
                for part in ('locales', 'money', 'postgres_use'):
                    d[part].pop(name)
                d['ctype']['locales'].pop(name)
            d['locales_listed'] = len(d['locales'])
            refirst(d)
        found = [d for kind, d in self.checks(*self.pair(old_edits=[no_euc]))
                 if kind == 'ctype']
        self.assertIn({'elsewhere': 3, 'with euc': False}, found)
        self.assertFalse([d for d in found if any(k.startswith('euc') for k in d)])

    def test_one_on_the_new_machine_only(self):
        """No database with it was made on the old machine: nothing to
        compare, and not named on the lines on EUC."""
        def dropped(d):
            for part in ('locales', 'money', 'postgres_use'):
                d[part].pop('ja_JP.eucjp')
            d['ctype']['locales'].pop('ja_JP.eucjp')
            d['locales_listed'] = len(d['locales'])
            refirst(d)
        self.assertEqual(self.euc(*self.pair([dropped])), [
            {'euc ascii': 'new', 'same': True,
             'names': {n: c for n, c in EUC_OF.items() if n != 'ja_JP.eucjp'}}])

    def test_each_euc_encoding_on_its_own(self):
        """Each member of CTYPE_EUC, with the locales of the others gone:
        still named, each on the line of its own state."""
        for codeset in m.CTYPE_EUC:
            for uses, key in (((8, 9), 'euc ascii'), ((8, 8), 'euc same')):
                with self.subTest(codeset=codeset, uses=uses):
                    def others(d, keep=codeset):
                        for name in [n for n, r in d['locales'].items()
                                     if r.get('codeset') in m.CTYPE_EUC
                                     and r['codeset'] != keep]:
                            for part in ('locales', 'money', 'postgres_use'):
                                d[part].pop(name)
                            d['ctype']['locales'].pop(name)
                        d['locales_listed'] = len(d['locales'])
                        refirst(d)
                    got, = self.euc(*self.pair([others], [others], old_use=uses[0],
                                               new_use=uses[1]))
                    names = got['names'] if key == 'euc ascii' else got[key]
                    self.assertEqual(set(names.values()), {codeset})


class ConverterCompared(PostgresUseCompared):
    """Step 3 read from the converter part, on RHEL8 and RHEL9 cut to
    EUC_NAMES: EUC-TW measured (16.11, 16.13); a converter not measured,
    or whose two answers disagree, NOT checked with why, for every locale
    of its encoding; a file of the format before read as before beside one
    of this format; the line on a database of another encoding, measured
    for the encodings of the part and never for another (16.16)."""

    @classmethod
    def setUpClass(cls):
        super(Checks, cls).setUpClass()
        for path in (RHEL8, RHEL9):
            cls.texts[path] = trimmed(cls.texts[path], EUC_NAMES)

    def pair(self, old_use=None, new_use=None, **k):
        return super().pair(measured_use(8, **EUC_TW_USE) if old_use is None else old_use,
                            measured_use(9, **EUC_TW_USE) if new_use is None else new_use,
                            **k)

    # The tests of the class above run on USE_NAMES; here, only these.
    for name in [n for n in vars(PostgresUseCompared) if n.startswith('test_')]:
        locals()[name] = None
    del name

    def other(self, *pair):
        return [d for kind, d in self.checks(*pair)
                if kind == 'bytes' and 'other encoding' in d]

    def test_euc_tw_is_measured(self):
        found = self.use(*self.pair())
        self.assertEqual(found[0]['changed']['zh_TW.euctw'], 'EUC-TW')
        self.assertFalse(any(m.NO_CONVERTER in w for d in found
                             for w in d.get('unknown', {}).values()))
        found = self.use(*self.pair(measured_use(8, **EUC_TW_USE),
                                    measured_use(8, **EUC_TW_USE)))
        self.assertNotIn('zh_TW.euctw', {n for d in found for v in d.values()
                                         if isinstance(v, dict) for n in v})
        self.assertEqual(found[-1]['converts'], 7)

    def test_a_converter_not_measured_on_one_side(self):
        """Every locale of its encoding NOT checked, with why and which
        machine; on each side."""
        entry = {'EUC-TW': ['not checked', ['opens', None], 'zh_TW.euctw',
                            'measuring it failed: x', None]}
        said = ("glibc's converter of EUC-TW could not be measured: measuring "
                'it failed: x')
        for side in ('old', 'new'):
            with self.subTest(side=side):
                found = self.use(*self.pair(measured_use(8, **EUC_TW_USE),
                                            measured_use(8, **EUC_TW_USE),
                                            **{f'{side}_converter': entry}))
                self.assertIn({'unknown': {'zh_TW.euctw': f'{said} (on the {side} machine)'}},
                              found)

    def test_iconv_open_and_mbrtowc_disagree(self):
        cs = 'EUC-KR'
        for case, entry, said in (
                ('opens, converts nothing',
                 ['measured', ['opens', None], 'ko_KR', ascii_only(cs), sequences_of(cs)],
                 "iconv_open said glibc's converter of EUC-KR opens, and glibc "
                 'converted nothing past ASCII'),
                ('fails, converts',
                 ['measured', ['fails', 'Invalid argument'], 'ko_KR',
                  converter_values(cs), sequences_of(cs)],
                 "iconv_open said glibc's converter of EUC-KR does not open, and "
                 'glibc converted text past ASCII')):
            with self.subTest(case=case):
                found = self.use(*self.pair(measured_use(8, **EUC_TW_USE),
                                            measured_use(8, **EUC_TW_USE),
                                            new_converter={cs: entry}))
                self.assertIn({'unknown': {'ko_KR': f'{said} (on the new machine)',
                                           'ko_KR.euckr': f'{said} (on the new machine)'}},
                              found)

    def test_a_file_of_the_format_before_beside_one_of_this(self):
        """The old file's test of one character is read as before; the new
        file's converter part beside it."""
        old = self.made(RHEL8, 'old', with_parts(use=measured_use(9, **EUC_TW_USE),
                                                 form='locale_order/4'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(8, **EUC_TW_USE)))
        found = self.use(old, new)
        self.assertEqual(found[0], {'back': {
            'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP', 'ko_KR': 'EUC-KR',
            'ko_KR.euckr': 'EUC-KR', 'zh_CN': 'GB2312'}})
        old = self.made(RHEL8, 'old', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                 form='locale_order/4'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9, **EUC_TW_USE)))
        found = self.use(old, new)
        # The old file's test of one character could not build an EUC-TW
        # character; the new machine's error is named beside that.
        self.assertIn({'one side': 'new', 'why': m.NO_CONVERTER,
                       'names': {'zh_TW.euctw': 'EUC-TW'}}, found)
        # It tried one character of the others, which converted: the rest
        # of their text is not known there (16.18 c), so the new machine's
        # error is named beside that, never as "the old one converts".
        self.assertIn({'one side': 'new', 'why': m.UNSAID, 'names': {
            'ja_JP': 'EUC-JP', 'ja_JP.eucjp': 'EUC-JP', 'ko_KR': 'EUC-KR',
            'ko_KR.euckr': 'EUC-KR', 'zh_CN': 'GB2312'}}, found)
        self.assertFalse([d for d in found if 'changed' in d])

    def entry(self, cs, name, drop=(), other=(), opens=True):
        """A converter entry of cs measured under name: converter_values,
        with the sequences of drop refused and those of other converted to
        another character; iconv_open fails where not opens."""
        values = m.unpack_order(zlib.decompress(base64.b64decode(converter_values(cs))))
        seqs = m.accepted_sequences(cs)
        for s in drop + other:
            self.assertNotEqual(values[seqs.index(s)], 0, 'already refused')
        for s in drop:
            values[seqs.index(s)] = 0
        for s in other:
            values[seqs.index(s)] += 1
        return {cs: ['measured', ['opens', None] if opens else ['fails', 'x'], name,
                     m.pack_order(values), sequences_of(cs)]}

    def tis(self, *drop, other=()):
        return self.entry('TIS-620', 'th_TH.tis620', tuple(bytes([b]) for b in drop),
                          tuple(bytes([b]) for b in other))

    def test_the_characters_file_is_named_whole(self):
        """audit.sh names a characters file whose path is longer than a line;
        the line that points at it keeps it whole, in the report and in the
        summary, or it could not be copied. Here the line on upper() of an
        EUC-JP converter that refuses one sequence."""
        old, new = self.pair(measured_use(8, **EUC_TW_USE),
                             measured_use(8, **EUC_TW_USE),
                             new_converter=self.entry('EUC-JP', 'ja_JP',
                                                      (b'\xa4\xa2',)))
        deep = os.path.join(self.tmp, 'work.glibc-2.28.glibc-2.34.dir' * 2)
        os.makedirs(deep, exist_ok=True)
        chars = os.path.join(deep, 'measured_order_characters.glibc-2.28..glibc-2.34.tsv')
        summary = os.path.join(deep, 'summary.txt')
        stdout = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', [
                SCRIPT, '--compare', old, new, '--summary-to', summary,
                '--characters-to', chars]), contextlib.redirect_stdout(stdout):
            self.assertEqual(m.main(), 0)
        with open(summary, encoding='utf-8') as f:
            brief = f.read()
        for text in (stdout.getvalue(), brief):
            self.assertIn('\n' + ' ' * (3 if text is not brief else 8) + chars + '.\n', text)


    def lacking(self, **k):
        lines = self.other(*self.pair(measured_use(8, **EUC_TW_USE),
                                      measured_use(8, **EUC_TW_USE), **k))
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_the_other_encoding_line_measured(self):
        """Both machines measured the converter: upper() measured, the bytes
        it refuses counted, one that refuses none said apart, never inside
        the clause about the error; the order still not measured."""
        line = self.lacking(old_converter=self.tis(0x80), new_converter=self.tis(0x80))
        self.assertEqual(line, {'other encoding': OTHER_9,
                                'lacking': {'TIS-620': {'both': 1}},
                                'whole': {'ARMSCII-8': 'on both machines'}})
        line = self.lacking()
        self.assertEqual(line, {'other encoding': OTHER_9, 'lacking': {},
                                'whole': {'ARMSCII-8': 'on both machines',
                                          'TIS-620': 'on both machines'}})

    def test_an_encoding_outside_the_nine_stays_not_measured(self):
        """16.16: a locale built by hand from CP1125, which PostgreSQL does
        not know and the converter part does not measure: named as not
        known on both machines, never among the measured."""
        def cp1125(d):
            d['locales']['de_DE']['codeset'] = 'CP1125'
            d['postgres_use']['de_DE'] = use_of('one byte', 'CP1125')
        line = self.lacking(old_edits=[cp1125], new_edits=[cp1125])
        self.assertEqual(line['upper unknown'], {'on both machines': {'de_DE': 'CP1125'}})
        self.assertNotIn('CP1125', line['lacking'])
        self.assertNotIn('CP1125', line['whole'])

    def test_the_other_encoding_line_of_a_file_of_the_format_before(self):
        """Only files of a format before the part: as before, neither the
        order nor upper() known. One beside a file of this format: what that
        file measured is said, and the other machine named as not known."""
        def old4(use):
            return self.made(RHEL8, 'old', with_parts(use=use, form='locale_order/4'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                 form='locale_order/4'))
        self.assertEqual(self.other(old4(measured_use(8, **EUC_TW_USE)), new),
                         [{'other encoding': OTHER_9}])
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                 converter=self.tis(0x80)))
        self.assertEqual(self.other(old4(measured_use(8, **EUC_TW_USE)), new), [
            {'other encoding': OTHER_9,
             'upper unknown': {'on the old machine': OTHER_9},
             'lacking': {'TIS-620': {'new': 1}},
             'whole': {'ARMSCII-8': 'on the new machine'}}])

    def test_the_bytes_a_converter_lacks_on_each_machine(self):
        """Counted on each machine, and two machines that refuse as many
        bytes, not the same ones, told apart from two that refuse the same."""
        for case, edits, want in (
                ('the same', {'old_converter': self.tis(0x80),
                              'new_converter': self.tis(0x80)}, {'both': 1}),
                ('one more on the new', {'old_converter': self.tis(0x80),
                                         'new_converter': self.tis(0x80, 0xC0)},
                 {'old': 1, 'new': 2}),
                ('none on the old', {'new_converter': self.tis(0x80)},
                 {'old': 0, 'new': 1}),
                ('one more on the old', {'old_converter': self.tis(0x80, 0xC0),
                                         'new_converter': self.tis(0x80)},
                 {'old': 2, 'new': 1}),
                ('as many, not the same', {'old_converter': self.tis(0x80, 0xC0),
                                           'new_converter': self.tis(0x80, 0xC1)},
                 {'each, not the same': 2})):
            with self.subTest(case=case):
                line = self.lacking(**edits)
                self.assertEqual(line['lacking']['TIS-620'], want)
                # A byte refused on one machine is counted there, never as
                # converting to another character.
                self.assertNotIn('other character', line)

    def test_a_byte_that_converts_to_another_character(self):
        """The same bytes refused on both, one converted to another
        character on the new machine: named, beside the count."""
        line = self.lacking(old_converter=self.tis(0x80),
                            new_converter=self.tis(0x80, other=(0xC0, 0xC1)))
        self.assertEqual(line['lacking'], {'TIS-620': {'both': 1}})
        self.assertEqual(line['other character'], {'TIS-620': 2})
        self.assertNotIn('other character', self.lacking(
            old_converter=self.tis(0x80), new_converter=self.tis(0x80)))

    def test_a_side_that_does_not_say(self):
        """A converter that does not open is named among the errors, and
        the other machine's bytes are still counted here; one not checked,
        or whose iconv_open and mbrtowc disagree, is named as not known on
        its machine, beside the other machine's count."""
        fails = {'TIS-620': ['measured', ['fails', 'Invalid argument'], 'th_TH.tis620',
                             ascii_only('TIS-620'), sequences_of('TIS-620')]}
        not_checked = {'TIS-620': ['not checked', ['opens', None], 'th_TH.tis620',
                                   'measuring it failed: x', None]}
        disagree = self.entry('TIS-620', 'th_TH.tis620', opens=False)
        for side in ('old', 'new'):
            other = 'old' if side == 'new' else 'new'
            with self.subTest(side=side, converter='fails'):
                line = self.lacking(**{f'{side}_converter': fails,
                                       f'{other}_converter': self.tis(0x80)})
                self.assertEqual(line['lacking']['TIS-620'], {other: 1})
                self.assertNotIn('upper unknown', line)
            for case, entry in (('not checked', not_checked), ('disagree', disagree)):
                with self.subTest(side=side, converter=case):
                    line = self.lacking(**{f'{side}_converter': entry,
                                           f'{other}_converter': self.tis(0x80)})
                    self.assertEqual(line['lacking']['TIS-620'], {other: 1})
                    self.assertEqual(line['upper unknown'], {
                        f'on the {side} machine': {'th_TH.tis620': 'TIS-620'}})

    def test_a_locale_only_on_the_new_machine_in_the_line(self):
        def gone(d):
            for part in ('locales', 'money', 'postgres_use'):
                d[part].pop('th_TH.tis620')
            d['ctype']['locales'].pop('th_TH.tis620')
            d['locales_listed'] = len(d['locales'])
            refirst(d)
        line = self.lacking(old_edits=[gone], new_converter=self.tis(0x80))
        self.assertEqual(line['lacking'], {'TIS-620': {'new': 1}})
        self.assertEqual(line['other encoding'], OTHER_9)
        self.assertNotIn('upper unknown', line)

    def test_a_locale_whose_encoding_changed_on_one_side(self):
        """The side where the locale has another encoding says nothing of
        this one's converter: not known there, beside the other's count."""
        def koi8r(d):
            d['locales']['th_TH.tis620']['codeset'] = 'KOI8-R'
            d['postgres_use']['th_TH.tis620'] = use_of('one byte', 'KOI8-R')
            refirst(d)
        line = self.lacking(new_edits=[koi8r], old_converter=self.tis(0x80))
        self.assertEqual(line['upper unknown'],
                         {'on the new machine': {'th_TH.tis620': 'TIS-620'}})
        self.assertEqual(line['lacking'], {'TIS-620': {'old': 1}})

    def test_another_locale_of_the_encoding_does_not_answer_for_it(self):
        """On the new machine th_TH.tis620 is of KOI8-R and hy_AM.armscii8
        of TIS-620, so the converter of TIS-620 is measured there, under
        hy_AM.armscii8: it says nothing of a database that uses
        th_TH.tis620, so both are not known on the new machine."""
        def swapped(d):
            d['locales']['th_TH.tis620']['codeset'] = 'KOI8-R'
            d['postgres_use']['th_TH.tis620'] = use_of('one byte', 'KOI8-R')
            d['locales']['hy_AM.armscii8']['codeset'] = 'TIS-620'
            d['postgres_use']['hy_AM.armscii8']['codeset'] = 'TIS-620'
            refirst(d)
            self.assertEqual(d['converter']['TIS-620'][2], 'hy_AM.armscii8')
        line = self.lacking(new_edits=[swapped], new_converter=self.entry(
            'TIS-620', 'hy_AM.armscii8', (b'\x80',)))
        self.assertEqual(line['upper unknown'], {'on the new machine': OTHER_9})
        self.assertEqual(line['lacking'], {})
        self.assertEqual(line['whole'], {'ARMSCII-8': 'on the old machine',
                                         'TIS-620': 'on the old machine'})

    def euc(self, **k):
        return [d for d in self.use(*self.pair(measured_use(8, **EUC_TW_USE),
                                               measured_use(8, **EUC_TW_USE), **k))]

    def test_part_of_the_text_in_a_database_of_the_locales_encoding(self):
        """A converter that converts and refuses some sequence a database
        of its encoding accepts gives an error on that part of the text
        (measured on PostgreSQL 17 and 18): named, counted on each machine,
        and never among those that convert. A machine without the converter
        is in the line of errors, and the other's count still said."""
        jp = lambda **k: self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1', b'\xad\xe2'), **k)
        found = self.euc(old_converter=jp(), new_converter=jp())
        self.assertIn({'partly': {n: {'EUC-JP': {'both': 2}}
                                  for n in ('ja_JP', 'ja_JP.eucjp')}}, found)
        self.assertEqual(found[-1]['converts'], 5)
        more = self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1', b'\xad\xe2', b'\xad\xa2'))
        found = self.euc(old_converter=jp(), new_converter=more)
        self.assertIn({'partly': {n: {'EUC-JP': {'old': 2, 'new': 3}}
                                  for n in ('ja_JP', 'ja_JP.eucjp')}}, found)
        fails = {'EUC-JP': ['measured', ['fails', 'x'], 'ja_JP', ascii_only('EUC-JP'),
                            sequences_of('EUC-JP')]}
        found = self.euc(old_converter=jp(), new_converter=fails)
        self.assertIn({'partly': {n: {'EUC-JP': {'old': 2}}
                                  for n in ('ja_JP', 'ja_JP.eucjp')}}, found)
        self.assertEqual(set(found[0]['changed']), {'ja_JP', 'ja_JP.eucjp'})
        found = self.euc()
        self.assertFalse(any('partly' in d for d in found))
        self.assertEqual(found[-1]['converts'], 7)
        # A converter of an encoding PostgreSQL does not know that refuses
        # bytes: no database is of that encoding, so not here, but in the
        # line on a database of another encoding.
        found = self.euc(old_converter=self.tis(0x80), new_converter=self.tis(0x80))
        self.assertFalse(any('partly' in d for d in found))

    def test_a_row_names_bytes_the_other_machine_converts(self):
        """The old machine's converter refuses A1 A1, the first sequence
        past ASCII, and converts A1 A2; the new one converts nothing: the
        row of the new machine's error names A1 A2 and its character, which
        the old machine converts, never A1 A1, which neither does (review
        round 2 found a row saying the old machine converted bytes it
        refuses)."""
        values = m.unpack_order(zlib.decompress(base64.b64decode(
            converter_values('EUC-KR'))))
        first = m.accepted_sequences('EUC-KR').index(b'\xa1\xa1')
        values[first] = 0
        old = {'EUC-KR': ['measured', ['opens', None], 'ko_KR', m.pack_order(values),
                          sequences_of('EUC-KR')]}
        report, summary, rows = self.outputs(*self.pair(old_converter=old))
        self.assertEqual([(r['chars'], r['bytes'], r['before'], r['now'])
                          for r in rows[('ko_KR',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [([0x3001], 'A1 A2', 'converts', 'error')])

    def test_part_of_the_text_beside_a_file_of_the_format_before(self):
        """The other machine's file holds a test of one character, which
        says nothing of the sequences a converter refuses: the measured
        side's count is named with "not known" on that machine, either way
        round (review round 2: the format before is what a machine measured
        by the version before this one holds)."""
        jp = self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1', b'\xad\xe2'))
        names = ('ja_JP', 'ja_JP.eucjp')
        for side, other in (('old', 'new'), ('new', 'old')):
            with self.subTest(measured=side):
                files = {other: self.made(RHEL8 if other == 'old' else RHEL9, other,
                                          with_parts(use=measured_use(8, **EUC_TW_USE),
                                                     form='locale_order/4')),
                         side: self.made(RHEL8 if side == 'old' else RHEL9, side,
                                         with_parts(use=measured_use(8, **EUC_TW_USE),
                                                    converter=jp))}
                found = self.use(files['old'], files['new'])
                self.assertIn({'partly': {n: {'EUC-JP': {side: 2}, 'not known': [other]}
                                          for n in names}}, found)

    def test_counts_whose_bytes_are_not_all_the_same(self):
        """Two machines that refuse a different number of bytes, neither all
        the other refuses: said, not read as one more or one less."""
        line = self.lacking(old_converter=self.tis(0x80, 0xC0),
                            new_converter=self.tis(0x81, 0xC1, 0xC2))
        self.assertEqual(line['lacking']['TIS-620'], {'old': 2, 'new': 3, 'mixed': True})
        line = self.lacking(old_converter=self.tis(0x80),
                            new_converter=self.tis(0x80, 0xC1))
        self.assertEqual(line['lacking']['TIS-620'], {'old': 1, 'new': 2})

    def test_a_row_names_bytes_the_other_machine_converts_the_mirror(self):
        """The old machine converts nothing past ASCII; the new one refuses
        A1 A1 and converts A1 A2: the old machine's row names A1 A2, which
        the new one converts (final review: the other side had no test)."""
        old = {'EUC-KR': ['measured', ['fails', 'x'], 'ko_KR', ascii_only('EUC-KR'),
                          sequences_of('EUC-KR')]}
        new = self.entry('EUC-KR', 'ko_KR', (b'\xa1\xa1',))
        report, summary, rows = self.outputs(*self.pair(old_converter=old,
                                                        new_converter=new))
        self.assertEqual([(r['chars'], r['bytes'], r['before'], r['now'])
                          for r in rows[('ko_KR',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [([0x3001], 'A1 A2', 'error', 'converts')])

    def refusal_rows(self, **k):
        """{label: (names, encoding, [(bytes, before, now, measured)])} of
        the rows under each group of the line on part of the text."""
        report, summary, rows = self.outputs(*self.pair(measured_use(8, **EUC_TW_USE),
                                                        measured_use(8, **EUC_TW_USE),
                                                        **k))
        return {label: (names, cs, [(r['bytes'], r['before'], r['now'], r['measured'])
                                    for r in rows[(label,)]])
                for label, (names, cs) in rows.use_groups.items()}

    def test_each_sequence_refused_is_a_row(self):
        """16.12: every sequence a machine's converter refuses, one row each
        under its encoding's group, with what each machine does with it;
        the locales of the group named once."""
        jp = lambda *drop: self.entry('EUC-JP', 'ja_JP', drop)
        names = ('ja_JP', 'ja_JP.eucjp')
        fails = {'EUC-JP': ['measured', ['fails', 'x'], 'ja_JP', ascii_only('EUC-JP'),
                            sequences_of('EUC-JP')]}
        for case, k, want in (
                ('the same on both', {'old_converter': jp(b'\xad\xa1', b'\xad\xe2'),
                                      'new_converter': jp(b'\xad\xa1', b'\xad\xe2')},
                 [('AD A1', 'error', 'error', 'on both machines'),
                  ('AD E2', 'error', 'error', 'on both machines')]),
                ('one more on the new', {'old_converter': jp(b'\xad\xa1'),
                                         'new_converter': jp(b'\xad\xa1', b'\x8f\xa2\xf1')},
                 [('8F A2 F1', 'converts', 'error', 'on both machines'),
                  ('AD A1', 'error', 'error', 'on both machines')]),
                ('one more on the old', {'old_converter': jp(b'\xad\xa1', b'\xad\xe2'),
                                         'new_converter': jp(b'\xad\xe2')},
                 [('AD A1', 'error', 'converts', 'on both machines'),
                  ('AD E2', 'error', 'error', 'on both machines')]),
                ('no converter on the new', {'old_converter': jp(b'\xad\xa1'),
                                             'new_converter': fails},
                 [('AD A1', 'error', 'error', 'on both machines')]),
                ('no converter on the old', {'old_converter': fails,
                                             'new_converter': jp(b'\xad\xa1')},
                 [('AD A1', 'error', 'error', 'on both machines')])):
            with self.subTest(case=case):
                (names_, cs, got), = self.refusal_rows(**k).values()
                self.assertEqual((names_, cs, got), (names, 'EUC-JP', want))
        # Two encodings: a group each.
        got = self.refusal_rows(old_converter=dict(jp(b'\xad\xa1'), **self.entry(
            'EUC-KR', 'ko_KR', (b'\xa2\xe8',))), new_converter=jp(b'\xad\xa1'))
        self.assertEqual(sorted(got.values()), [
            (names, 'EUC-JP', [('AD A1', 'error', 'error', 'on both machines')]),
            (('ko_KR', 'ko_KR.euckr'), 'EUC-KR',
             [('A2 E8', 'error', 'converts', 'on both machines')])])
        # None where nothing is refused.
        self.assertEqual(self.refusal_rows(), {})

    def test_refused_rows_beside_a_file_of_the_format_before(self):
        """The other machine's file says nothing of the sequences: each row
        is measured on the one machine, never "converts" there."""
        jp = self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1',))
        for side, other in (('old', 'new'), ('new', 'old')):
            with self.subTest(measured=side):
                files = {other: self.made(RHEL8 if other == 'old' else RHEL9, other,
                                          with_parts(use=measured_use(8, **EUC_TW_USE),
                                                     form='locale_order/4')),
                         side: self.made(RHEL8 if side == 'old' else RHEL9, side,
                                         with_parts(use=measured_use(8, **EUC_TW_USE),
                                                    converter=jp))}
                report, summary, rows = self.outputs(files['old'], files['new'])
                (label, (names, cs)), = rows.use_groups.items()
                self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                                  for r in rows[(label,)]],
                                 [('AD A1', 'error' if side == 'old' else 'not measured',
                                   'error' if side == 'new' else 'not measured',
                                   f'on the {side} machine')])

    def test_part_of_the_text_not_known_from_files_of_the_format_before(self):
        """Both files of the format before the converter part, each with a
        test of one character that converts: whether glibc refuses part of
        the text is not known on either machine (every EUC converter
        measured refuses some), so the EUC locales are named as that, never
        counted as converting all their text (review of PR 3, round 2). With
        one such file, on that machine only."""
        jp = ('ja_JP', 'ja_JP.eucjp', 'ko_KR', 'ko_KR.euckr', 'zh_CN')
        names = {n: EUC_OF[n] for n in jp}
        four = lambda base, tag: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE), form='locale_order/4'))
        five = lambda base, tag: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE)))
        for case, files, where in (
                ('both', (four(RHEL8, 'o4'), four(RHEL9, 'n4')), 'both machines'),
                ('old', (four(RHEL8, 'o4'), five(RHEL9, 'n5')), 'the old machine'),
                ('new', (five(RHEL8, 'o5'), four(RHEL9, 'n4')), 'the new machine')):
            with self.subTest(case=case):
                report, summary, rows = self.outputs(*files)
                use = [d for kind, d in report['checks'] if kind == 'use']
                self.assertIn({'untold': {where: names}}, use)
                # de_DE.utf8 alone, of UTF-8, which such a file did not
                # measure back, nor past one character: counted in the line
                # on that file, never as converting.
                self.assertEqual(use[-1]['converts'], 0)
                early, = [d for d in use if 'back earlier' in d]
                self.assertEqual(early['back earlier'], {
                    'both': ['OLD', 'NEW'], 'old': ['OLD'], 'new': ['NEW']}[case])
                self.assertEqual(early['utf8'], 1)
                self.assertEqual([r['why'] for r in rows[('ja_JP',)]
                                  if r['finding'] == 'upper/lower/ILIKE not checked'],
                                 [f'whether part of the text gives an error: not '
                                  f'known on {where}'])
        # Measured on both machines, nothing of the kind.
        use = self.use(*self.pair(measured_use(8, **EUC_TW_USE),
                                  measured_use(8, **EUC_TW_USE)))
        self.assertFalse([d for d in use if 'untold' in d])
        # A file whose test of one character failed is known: its glibc
        # converts nothing past ASCII (RHEL9 without glibc-gconv-extra),
        # never "not known" beside the line that says so (final check).
        old = self.made(RHEL8, 'o9', with_parts(use=measured_use(9, **EUC_TW_USE),
                                                form='locale_order/4'))
        report, summary, rows = self.outputs(old, four(RHEL9, 'n4'))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertEqual(use[0], {'back': names})
        self.assertIn({'untold': {'the new machine': names}}, use)
        report, summary, rows = self.outputs(old, five(RHEL9, 'n5'))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertFalse([d for d in use if 'untold' in d])
        # Nor in the line on part of the text, beside what the new machine
        # measured.
        new = self.made(RHEL9, 'n5r', with_parts(
            use=measured_use(8, **EUC_TW_USE),
            converter=self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1', b'\xad\xe2'))))
        use = self.use(old, new)
        self.assertIn({'partly': {n: {'EUC-JP': {'new': 2}}
                                  for n in ('ja_JP', 'ja_JP.eucjp')}}, use)

    def test_refused_rows_of_a_locale_whose_encoding_changed(self):
        """ja_JP is of EUC-JP on the old machine and of EUC-KR on the new:
        a database of EUC-JP carried over opens there under ja_JP, and glibc
        reads its bytes with the converter of EUC-KR, which converts AD A1;
        the new machine's converter of EUC-JP, measured under another
        locale, is not what reads them. The rows say what the rows of an
        error say of the same bytes (sequence_state, 16.19 c)."""
        def kr(d):
            d['postgres_use']['ja_JP']['codeset'] = 'EUC-KR'
            d['locales']['ja_JP']['codeset'] = 'EUC-KR'
            refirst(d)
        got = self.refusal_rows(old_converter=self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1',)),
                                new_converter=self.entry('EUC-JP', 'ja_JP.eucjp',
                                                         (b'\xad\xa1',)),
                                new_edits=[kr])
        self.assertEqual(sorted(got.values()), [
            (('ja_JP',), 'EUC-JP', [('AD A1', 'error', 'converts',
                                     'on both machines')]),
            (('ja_JP.eucjp',), 'EUC-JP', [('AD A1', 'error', 'error',
                                           'on both machines')])])

    def test_where_the_refused_sequences_are(self):
        """The path of the characters file whole on a line, never cut; and
        without the file, how to get it."""
        jp = self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1',))
        old, new = self.pair(measured_use(8, **EUC_TW_USE), measured_use(8, **EUC_TW_USE),
                             old_converter=jp, new_converter=jp)
        deep = os.path.join(self.tmp, 'd' * 90)
        os.mkdir(deep)
        chars = os.path.join(deep, 'characters.tsv')
        stdout = io.StringIO()
        summary = os.path.join(self.tmp, 'where.sum')
        with unittest.mock.patch.object(m.sys, 'argv', [
                SCRIPT, '--compare', old, new, '--characters-to', chars,
                '--summary-to', summary]), contextlib.redirect_stdout(stdout):
            self.assertEqual(m.main(), 0)
        # In the summary too, which audit.sh relays (review of PR 3).
        with open(summary, encoding='utf-8') as f:
            lines = f.read().split('\n')
        at, = [i for i, x in enumerate(lines) if x.startswith(
            '     !! upper(), lower() and ILIKE give an ERROR on part')]
        end = next(i for i in range(at + 1, len(lines))
                   if not lines[i].startswith('        '))
        self.assertEqual(lines[end - 1], '        ' + chars + '.')

        def paragraph(text):
            # The line on part of the text, to the next line of its own:
            # the line on what PostgreSQL stores names the file too.
            lines = text.split('\n')
            at, = [i for i, x in enumerate(lines)
                   if x.startswith('!! upper(), lower() and ILIKE give an ERROR on part')]
            end = next(i for i in range(at + 1, len(lines))
                       if not lines[i].startswith(' '))
            return lines[at:end]
        self.assertEqual(paragraph(stdout.getvalue())[-1], '   ' + chars + '.')
        stdout = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'argv', [SCRIPT, '--compare', old, new]), \
                contextlib.redirect_stdout(stdout):
            m.main()
        self.assertTrue(flat(' '.join(paragraph(stdout.getvalue()))).endswith(
            'Pass --characters-to FILE for every such byte sequence.'))

    def test_a_row_where_neither_machine_converts(self):
        report, summary, rows = self.outputs(*self.pair(measured_use(9, **EUC_TW_USE),
                                                        measured_use(9, **EUC_TW_USE)))
        self.assertEqual([(r['chars'], r['bytes'], r['before'], r['now'])
                          for r in rows[('zh_TW.euctw',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [([], '80 A1', 'error', 'error')])


class ConverterText(unittest.TestCase):
    """converter_text(): what one machine's converter part says of a
    database of an encoding, each branch."""

    def test_each_branch(self):
        cs = 'EUC-KR'
        seq = sequences_of(cs)
        for entry, want in (
                (['measured', ['opens', None], 'ko_KR', converter_values(cs), seq],
                 ['converts', [0x3000, 'a1a1']]),
                (['measured', ['fails', 'x'], 'ko_KR', ascii_only(cs), seq],
                 ['fails', [None, 'a1a1']]),
                (['not checked', ['opens', None], 'ko_KR', 'why', None],
                 ['not checked', "glibc's converter of EUC-KR could not be measured: why"]),
                (['no locale', ['opens', None], None, None, None],
                 ['not checked', 'the converter part holds no measurement of EUC-KR'])):
            with self.subTest(state=entry[0], opens=entry[1][0]):
                self.assertEqual(m.converter_text(entry, cs), want)

    def test_what_iconv_open_said_is_part_of_what_is_kept(self):
        """The same values read once with a converter that opens and once
        with one that does not give two answers, in either order."""
        cs, seq = 'EUC-KR', sequences_of('EUC-KR')
        opens = ['measured', ['opens', None], 'ko_KR', converter_values(cs), seq]
        fails = ['measured', ['fails', 'x'], 'ko_KR', converter_values(cs), seq]
        for order in ((opens, fails), (fails, opens)):
            with unittest.mock.patch.dict(m.CONVERTERS, clear=True):
                got = [m.converter_text(e, cs)[0] for e in order]
            self.assertEqual(got, ['converts', 'not checked'] if order[0] is opens
                             else ['not checked', 'converts'])


class CtypeErrnoAndRestore(unittest.TestCase):
    """13.62 to 13.64 on the machine: the reason newlocale gives is its own,
    never an earlier call's; a category that cannot be put back stops the
    run with why, never a traceback nor a file measured under another
    locale."""

    def test_errno_is_cleared_before_newlocale(self):
        store = {'errno': 22}
        lib = unittest.mock.Mock(newlocale=lambda mask, raw, base: None,
                                 freelocale=lambda loc: None)
        with unittest.mock.patch.object(m, 'ctype_libc', lambda: lib), \
                unittest.mock.patch.object(m.ctypes, 'get_errno',
                                           lambda: store['errno']), \
                unittest.mock.patch.object(m.ctypes, 'set_errno',
                                           lambda v: store.update(errno=v)):
            got = m.ctype_identity('de_DE.utf8',
                                   use_of('converts', 'UTF-8', (0x80, 'c280')))
        self.assertEqual(got, ['not checked', 'newlocale gave no locale for it: '
                                              'no reason given'])

    def failing_put_back(self, call, category=None, nth=1):
        """call() with setlocale accepting every name but failing to set a
        category back to the name it had: any category, or the nth time
        category is set back."""
        state, backs = {}, {}

        def setlocale(cat, name=None):
            if name is None:
                return state.get(cat, 'C.UTF-8')
            if name == 'C.UTF-8' and state.get(cat) not in (None, 'C.UTF-8'):
                backs[cat] = backs.get(cat, 0) + 1
                if category is None or (cat == category and backs[cat] == nth):
                    raise m.locale.Error('unsupported locale setting')
            state[cat] = name
            return name
        with unittest.mock.patch.object(m.locale, 'setlocale', setlocale), \
                self.assertRaises(m.NotPutBack) as stop:
            call()
        return str(stop.exception)

    def test_each_place_that_puts_a_category_back(self):
        lib = unittest.mock.Mock()
        lib.localeconv.return_value.contents.frac_digits = 2
        for case, call, category in (
                ('postgres_use', lambda: m.postgres_use('de_DE.utf8'), 'LC_COLLATE'),
                # Its second LC_CTYPE, once the encoding was read.
                ('postgres_use after the encoding',
                 lambda: self.failing_put_back(
                     lambda: m.postgres_use('de_DE.utf8'), m.locale.LC_CTYPE, 2),
                 'LC_CTYPE'),
                ('ctype_tables', lambda: m.ctype_tables('de_DE.utf8'), 'LC_CTYPE'),
                ('codeset_of', lambda: m.codeset_of('de_DE.utf8'), 'LC_CTYPE'),
                ('money_digits', lambda: m.money_digits(['de_DE.utf8']),
                 'LC_MONETARY')):
            # CORPORA too: postgres_use builds the corpus of the encoding over
            # CODEPOINTS, and a corpus of one code point kept in this process
            # would stand for UTF-8 in every class after this one.
            with self.subTest(case=case), \
                    unittest.mock.patch.object(m, 'libc', lambda: lib), \
                    unittest.mock.patch.object(m, 'ctype_libc', lambda: lib), \
                    unittest.mock.patch.object(m, 'CODEPOINTS', [0x41]), \
                    unittest.mock.patch.object(m, 'CORPORA', {}):
                got = (call() if case.endswith('encoding')
                       else self.failing_put_back(call))
                self.assertEqual(got,
                                 f"glibc could not set {category} back to "
                                 f"'C.UTF-8' (unsupported locale setting), as "
                                 f"when the machine runs short of memory")

    def test_measure_stops_with_why(self):
        def measure_locale(name):
            raise m.NotPutBack('glibc could not set LC_CTYPE back to x')
        err = io.StringIO()
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34', list_locales=lambda: ['a'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_locale=measure_locale, UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.measure(1)
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('glibc could not set LC_CTYPE back to x; nothing was '
                      'written: run again with a smaller --jobs', err.getvalue())

    def test_measure_stops_with_why_after_the_pool(self):
        """The decimals of money are read in this process, after the
        pool: a category money_digits cannot set back stops the run the
        same way."""
        def money_digits(names):
            raise m.NotPutBack('glibc could not set LC_MONETARY back to x')
        err = io.StringIO()
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34', list_locales=lambda: ['a'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_locale=lambda name: (name, {'error': 'x'}, None,
                                             use_of('one byte', 'X'),
                                             ['table', 'builtin']),
                ctype_part=lambda identities, measure_tables: {},
                money_digits=money_digits, UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.measure(1)
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('glibc could not set LC_MONETARY back to x; nothing was '
                      'written', err.getvalue())


# --- what glibc's converter of each encoding gives -----------------------------


CONVERTER_DEFAULTS = {}   # per codeset, what converter_values() packed


def converter_values(codeset):
    """A list as converter_of() packs one, one value per accepted sequence:
    ASCII as itself, then what Python's codec reads, where it reads one
    character, else a stand-in code point: a converter that refuses
    nothing, so that a test that wants a refusal writes it (TIS-620 of
    glibc refuses 41 bytes, EUC-JP 4726 sequences). Packed once per
    process."""
    if codeset not in CONVERTER_DEFAULTS:
        codec = {'EUC-JP': 'euc_jp', 'EUC-KR': 'euc_kr', 'GB2312': 'gb2312',
                 'TIS-620': 'tis_620', 'KOI8-T': 'koi8_t',
                 'PT154': 'ptcp154'}.get(codeset)
        got = []
        for i, s in enumerate(m.accepted_sequences(codeset)):
            if len(s) == 1 and s[0] < 0x80:
                got.append(s[0])
                continue
            if codec is None:
                got.append(0x4E00 + i % 0x5000)
                continue
            try:
                text = s.decode(codec)
            except UnicodeError:
                text = ''
            got.append(ord(text) if len(text) == 1 else 0x4E00 + i % 0x5000)
        CONVERTER_DEFAULTS[codeset] = m.pack_order(got)
    return CONVERTER_DEFAULTS[codeset]


def sequences_of(codeset):
    """The digest a measured entry keeps of its sequences: sha256 of each
    in lower-case hex on a line of its own, worked out here."""
    return hashlib.sha256(''.join(
        s.hex() + '\n' for s in m.accepted_sequences(codeset)).encode()).hexdigest()


ASCII_ONLY = {}   # per codeset, the values of a converter glibc cannot load


def ascii_only(codeset):
    """converter_values of a machine without the converter: ASCII alone."""
    if codeset not in ASCII_ONLY:
        ASCII_ONLY[codeset] = m.pack_order([
            s[0] if s[0] < 0x80 else 0 for s in m.accepted_sequences(codeset)])
    return ASCII_ONLY[codeset]


def converter_of_use(use, entries=None):
    """The converter part of a machine whose postgres_use is use: each
    encoding measured under its first locale by name, or no locale of it;
    entries in place. A machine whose postgres_use says a locale of one of
    the nine fails, as a test of one character said it in the format
    before, lacks glibc-gconv-extra: every converter fails to open there and
    converts ASCII alone; one not checked for a reason of its own is not
    checked. Worked out here, not with the script's first_locales, so that
    a fault there is not repeated in what the tests read."""
    first = {}
    for name in sorted(use):
        first.setdefault(use[name]['codeset'], name)
    missing = any(r['codeset'] in m.CONVERTER_CODESETS and r['text'][0] == 'fails'
                  for r in use.values())
    why = {r['codeset']: r['text'][1] for r in use.values()
           if r['codeset'] in m.CONVERTER_CODESETS and r['text'][0] == 'not checked'
           and r['text'][1] != m.NO_CONVERTER}
    part = {}
    for cs in m.CONVERTER_CODESETS:
        if cs not in first:
            part[cs] = ['no locale', ['fails', 'Invalid argument'] if missing
                        else ['opens', None], None, None, None]
        elif cs in why:
            part[cs] = ['not checked', ['opens', None], first[cs], why[cs], None]
        elif missing:
            part[cs] = ['measured', ['fails', 'Invalid argument'], first[cs],
                        ascii_only(cs), sequences_of(cs)]
        else:
            part[cs] = ['measured', ['opens', None], first[cs], converter_values(cs),
                        sequences_of(cs)]
    part.update(entries or {})
    return part


def refirst(d):
    """An edit after locales were dropped: each encoding's entry asked
    under the first locale left, as it was, or under none."""
    if 'converter' not in d:
        return
    first = {}
    for name in sorted(d['postgres_use']):
        first.setdefault(d['postgres_use'][name]['codeset'], name)
    parts = [d['converter']]
    if 'back' in d:
        parts += [d['back'], {'UTF-8': d['utf8_forward']}]
    for part in parts:
        for cs, e in part.items():
            if cs not in first and e[0] != 'no locale':
                part[cs] = ['no locale', e[1], None, None, None]
            elif cs in first:
                e[2] = first[cs]
    if 'back' in d:
        d['utf8_forward'] = parts[2]['UTF-8']


# The converter entry of hy_AM.armscii8 on a machine whose glibc cannot load
# the converter of ARMSCII-8: iconv_open fails and only ASCII converts.
ARMSCII_MISSING = {'ARMSCII-8': ['measured', ['fails', 'Invalid argument'],
                                 'hy_AM.armscii8', ascii_only('ARMSCII-8'),
                                 sequences_of('ARMSCII-8')]}


# The converter part of a machine with no locale of any of the nine.
NO_CONVERTER_LOCALES = {cs: ['no locale', ['opens', None], None, None, None]
                        for cs in m.CONVERTER_CODESETS}
# And its two parts of the conversion back, of a machine with no UTF-8
# locale either.
NO_ROUND_TRIP = {'utf8_forward': ['no locale', ['opens', None], None, None, None],
                 'back': {cs: ['no locale', ['opens', None], None, None, None]
                          for cs in ('UTF-8',) + m.CONVERTER_CODESETS}}


class FakeConverter:
    """glibc's iconv_open and mbrtowc as a double. chars maps the bytes of a
    character to its code point. mbrtowc puts the bytes its state holds
    before the ones it is handed, as glibc does with the bytes of a
    character it has not seen whole (iconv/loop.c, SINGLE); it returns -2
    for the start of a longer character and keeps the bytes, and -1 for
    anything else, leaving them in the state too, which POSIX allows: the
    state is undefined after an error. opens is the errno iconv_open fails
    with, or 0 when it opens. Every call is recorded with the LC_CTYPE then
    held."""

    def __init__(self, chars, opens=0, ctype=lambda: None):
        self.chars, self.opens, self.ctype, self.calls = chars, opens, ctype, []

    def iconv_open(self, to, frm):
        self.calls.append(('iconv_open', to, frm))
        if self.opens:
            m.ctypes.set_errno(self.opens)
            return m.NO_ICONV
        return 1234

    def iconv_close(self, h):
        self.calls.append(('iconv_close', h))
        return 0

    def mbrtowc(self, pwc, s, n, state):
        b = state.raw[1:1 + state.raw[0]] + s[:n]
        self.calls.append(('mbrtowc', b, self.ctype()))
        if b in self.chars:
            m.ctypes.memset(state, 0, len(state))
            pwc._obj.value = self.chars[b]
            return n
        state.raw = bytes([len(b)]) + b + bytes(len(state) - 1 - len(b))
        if any(k.startswith(b) for k in self.chars):
            return m.ctypes.c_size_t(-2).value
        return m.ctypes.c_size_t(-1).value


class ConverterOnTheMachine(unittest.TestCase):
    """converter_of(), converter_part() and what the run says when it ends:
    what glibc's converter of each encoding gives, asked once per
    encoding. glibc is a double here, since this machine need not be
    Linux, and the sequences are cut to a few. Freezes a converter that
    could not be opened read as one that was, a sequence glibc reads in
    part or as the start of another read as converted or as an error, a
    state left by the sequence before, and a measurement that failed read
    as one that worked, or left out of the end of the run."""

    SEQS = [b'A', b'\xa1\xa1', b'\xa1\xa2', b'\x8f\xa1\xa1', b'\x8f\xa2\xa2']
    CHARS = {b'A': 0x41, b'\xa1\xa1': 0x3000, b'\xa1\xa2': 0x3001,
             b'\x8f\xa2\xa2': 0x2D8}

    def measure(self, chars=None, opens=0, name='ja_JP.eucjp', refuse=False,
                fake=FakeConverter):
        """converter_of(('EUC-JP', name)) over SEQS, with LC_CTYPE checked
        put back."""
        state = {m.locale.LC_CTYPE: 'C'}

        def setlocale(category, n=None):
            if n is None:
                return state[category]
            if refuse and n == name:
                raise m.locale.Error('unsupported locale setting')
            state[category] = n
            return n
        self.fake = fake(self.CHARS if chars is None else chars, opens,
                         lambda: state[m.locale.LC_CTYPE])
        with unittest.mock.patch.object(m.locale, 'setlocale', setlocale), \
                unittest.mock.patch.object(m, 'converter_libc', lambda: self.fake), \
                unittest.mock.patch.dict(m.SEQUENCES, {'EUC-JP': self.SEQS}), \
                unittest.mock.patch.dict(m.SEQUENCE_DIGESTS, clear=True):
            got = m.converter_of(('EUC-JP', name))
        self.assertEqual(state, {m.locale.LC_CTYPE: 'C'}, 'LC_CTYPE not put back')
        return got

    def values(self, entry):
        with unittest.mock.patch.dict(m.SEQUENCES, {'EUC-JP': self.SEQS}):
            return m.converter_unpacked(entry[3], 'EUC-JP')

    def test_each_sequence_under_the_locale(self):
        cs, entry = self.measure()
        self.assertEqual((cs, entry[:3]), ('EUC-JP', ['measured', ['opens', None],
                                                      'ja_JP.eucjp']))
        with unittest.mock.patch.dict(m.SEQUENCES, {'EUC-JP': self.SEQS}), \
                unittest.mock.patch.dict(m.SEQUENCE_DIGESTS, clear=True):
            self.assertEqual(entry[4], sequences_of('EUC-JP'))
        self.assertEqual(self.values(entry), [0x41, 0x3000, 0x3001, 0, 0x2D8])
        self.assertEqual(self.fake.calls[:2], [('iconv_open', b'WCHAR_T', b'EUC-JP'),
                                               ('iconv_close', 1234)])
        self.assertEqual([c[1:] for c in self.fake.calls if c[0] == 'mbrtowc'],
                         [(s, 'ja_JP.eucjp') for s in self.SEQS])

    def test_a_state_left_by_the_sequence_before_is_cleared(self):
        """8F A1 A1 is refused and its bytes stay in the state: read after
        them, 8F A2 A2 would be refused too."""
        cs, entry = self.measure()
        self.assertEqual(self.values(entry)[3:], [0, 0x2D8])

    def test_a_sequence_read_whole_without_a_character(self):
        """glibc's mbrtowc gives the bytes read even where the converter
        wrote nothing (mbrtowc.c, 2.28): the value left from the sequence
        before would read as this one's, so the encoding is not checked.
        No converter of the nine does it in 2.28 or 2.39."""
        class Silent(FakeConverter):
            def mbrtowc(self, pwc, s, n, state):
                if s == b'\xa1\xa2':
                    return n
                return super().mbrtowc(pwc, s, n, state)
        cs, entry = self.measure(fake=Silent)
        self.assertEqual(entry, ['not checked', ['opens', None], 'ja_JP.eucjp',
                                 'glibc read the bytes A1 A2 as one character '
                                 'but wrote none', None])

    def test_a_sequence_read_neither_whole_nor_refused(self):
        """In part (mbrtowc gives fewer bytes than it was handed) or as the
        start of a longer character (-2): no value says what PostgreSQL
        does with it, so the encoding is not checked, naming the bytes,
        never read as converted or as an error. Measured on collaudit8:
        glibc 2.28 gives every accepted sequence of the nine whole or
        refuses it."""
        class Short(FakeConverter):
            def mbrtowc(self, pwc, s, n, state):
                r = super().mbrtowc(pwc, s, n, state)
                return 1 if s == b'\xa1\xa2' else r
        for case, kw, why in (
                ('in part', {'fake': Short},
                 'glibc read the bytes A1 A2 neither as one character nor as '
                 'an error (mbrtowc gave 1)'),
                ('the start of another',
                 {'chars': {**self.CHARS, b'\x8f\xa1\xa1\xa1': 0x2E00}},
                 'glibc read the bytes 8F A1 A1 neither as one character nor as '
                 'an error (mbrtowc gave -2)')):
            with self.subTest(case=case):
                cs, entry = self.measure(**kw)
                self.assertEqual(entry, ['not checked', ['opens', None],
                                         'ja_JP.eucjp', why, None])

    def test_a_converter_that_does_not_open(self):
        """Without glibc-gconv-extra: iconv_open fails, and what glibc
        converts is still measured (ASCII alone, measured on collaudit9)."""
        cs, entry = self.measure(opens=22, chars={b'A': 0x41})
        self.assertEqual(entry[:3], ['measured', ['fails', os.strerror(22)],
                                     'ja_JP.eucjp'])
        self.assertEqual(self.values(entry), [0x41, 0, 0, 0, 0])

    def test_no_locale_of_the_encoding(self):
        cs, entry = self.measure(name=None)
        self.assertEqual(entry, ['no locale', ['opens', None], None, None, None])
        self.assertEqual([c[0] for c in self.fake.calls], ['iconv_open', 'iconv_close'])

    def test_a_measurement_that_fails_says_so(self):
        cs, entry = self.measure(refuse=True)
        self.assertEqual(entry, ['not checked', ['opens', None], 'ja_JP.eucjp',
                                 "measuring it failed: Error('unsupported "
                                 "locale setting')", None])

    def test_glibc_that_cannot_be_asked(self):
        """A C library without iconv_open (this Mac's): not checked, with
        why, and nothing asked under the locale, whether there is one or
        not."""
        for name in ('ko_KR', None):
            with self.subTest(locale=name), unittest.mock.patch.object(
                    m, 'converter_libc', unittest.mock.Mock(
                        side_effect=AttributeError('iconv_open'))):
                cs, entry = m.converter_of(('EUC-KR', name))
                self.assertEqual(entry, [
                    'not checked', ['not asked', None], name,
                    "asking glibc failed: AttributeError('iconv_open')", None])

    def test_the_run_says_when_a_converter_was_not_measured(self):
        """finished(): each encoding whose converter was not measured is
        named, with why, in the order of CONVERTER_CODESETS, and the exit
        status is 1; one measured, with no locale, or whose converter does
        not open, is not a failure of the run."""
        part = dict(NO_CONVERTER_LOCALES)
        part['GB2312'] = ['not checked', ['opens', None], 'zh_CN', 'why cn', None]
        part['EUC-JP'] = ['not checked', ['not asked', None], None, 'why jp', None]
        part['EUC-KR'] = ['measured', ['fails', 'Invalid argument'], 'ko_KR', 'x', 'd']
        out = {'locales': {}, 'locales_listed': 0, 'postgres_libc': dict(CLEAN_LIBC),
               'ctype': {'locales': {}, 'tables': {}, 'wide': {}, 'bytes': {}},
               'converter': part, **NO_ROUND_TRIP}
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 1)
        self.assertIn("!! What glibc's converter gives could not be measured for "
                      "2 encoding(s):\n  EUC-JP: why jp\n  GB2312: why cn\n",
                      screen.getvalue())
        del part['GB2312'], part['EUC-JP']
        part.update(GB2312=NO_CONVERTER_LOCALES['GB2312'],
                    **{'EUC-JP': NO_CONVERTER_LOCALES['EUC-JP']})
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 0)
        self.assertNotIn('converter', screen.getvalue())

    def test_the_first_locale_of_each_encoding(self):
        use = {'ja_JP': use_of('converts', 'EUC-JP', (0xFF61, '8ea1')),
               'ja_JP.eucjp': use_of('converts', 'EUC-JP', (0xFF61, '8ea1')),
               'C': use_of('one byte', 'ANSI_X3.4-1968')}
        jobs = []
        m.converter_part(use, lambda f, js: (jobs.extend(js), [(cs, n) for cs, n in js])[1])
        self.assertEqual(dict(jobs), dict({cs: None for cs in m.CONVERTER_CODESETS},
                                          **{'EUC-JP': 'ja_JP'}))

    def test_the_accepted_sequences(self):
        """What PostgreSQL's verifiers accept, counted, and the edges each
        refuses (wchar.c, REL_13_23 and REL_18_6)."""
        counts = {cs: len(m.accepted_sequences(cs)) for cs in m.CONVERTER_CODESETS}
        self.assertEqual(counts, {'EUC-JP': 127 + 94 * 94 + 63 + 94 * 94,
                                  'EUC-KR': 127 + 94 * 94, 'GB2312': 127 + 94 * 94,
                                  'EUC-TW': 127 + 7 * 94 * 94 + 126 * 94,
                                  'TIS-620': 255, 'KOI8-T': 255, 'PT154': 255,
                                  'ARMSCII-8': 255, 'GEORGIAN-PS': 255})
        for cs, inside, outside in (
                ('EUC-JP', [b'\x8e\xdf', b'\x8f\xfe\xfe'], [b'\x8e\xe0', b'\xa1\xa0', b'\x80']),
                ('EUC-KR', [b'\xfe\xfe'], [b'\x8e\xa1', b'\xa1\xff']),
                ('EUC-TW', [b'\x80\xa1', b'\x8e\xa7\xa1\xa1', b'\xff\xfe'],
                 [b'\x8e\xa8\xa1\xa1', b'\x8f\xa1', b'\x8e\xa1'])):
            seqs = set(m.accepted_sequences(cs))
            with self.subTest(codeset=cs):
                self.assertTrue(set(inside) <= seqs)
                self.assertFalse(set(outside) & seqs)
        for cs in m.CONVERTER_CODESETS:
            self.assertEqual(m.accepted_sequences(cs), sorted(m.accepted_sequences(cs)))

    def test_measure_writes_the_part(self):
        """The call site: measure() writes the part converter_part makes
        from the answers of postgres_use, under the parts its format lists."""
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: ['a', 'b'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=lambda name: (name, {'error': 'x'}, None),
                postgres_use=lambda name: use_of('converts', 'EUC-KR', (0x3000, 'a1a1')),
                ctype_identity=lambda name, use: ['table', 'd' * 64],
                ctype_part=lambda ids, f: {},
                converter_of=lambda job: (job[0], ['asked', job[1]]),
                UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1),
                money_digits=lambda names: {n: 3 for n in names},
                postgres_libc=lambda: dict(CLEAN_LIBC)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', Screen()):
            text, out, _ = m.measure(1)
        data = json.loads(text)
        self.assertIn('converter', data['parts'])
        self.assertEqual(data['converter']['EUC-KR'], ['asked', 'a'])
        self.assertEqual(data['converter']['EUC-JP'], ['asked', None])


class ConverterRead(Checks):
    """A file of this format whose converter part is missing or malformed is
    refused: read as missing it would say "not checked" where the machine
    did check, and a list of another length would pair characters with the
    wrong sequences."""

    MALFORMED = 'what it says of the converter of each encoding is malformed'

    @classmethod
    def setUpClass(cls):
        super(Checks, cls).setUpClass()
        for path in (RHEL8, RHEL9):
            cls.texts[path] = trimmed(cls.texts[path], USE_NAMES)

    def refused(self, phrase, *edits, form=m.FORMAT):
        new = self.made(RHEL9, 'new', with_parts(form=form, use=measured_use(9)),
                        *edits)
        err = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.load(new, m.corpus_digest())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn(phrase, flat(err.getvalue()))

    def test_without_the_part(self):
        self.refused('lacks a part its format holds', lambda d: d.pop('converter'))

    def test_a_fourth_format_file_with_the_part(self):
        self.refused('holds one it does not', lambda d: d.update(converter={}),
                     form='locale_order/4')

    def test_malformed(self):
        cut = lambda n: m.pack_order(m.unpack_order(zlib.decompress(
            base64.b64decode(converter_values('EUC-KR'))))[:n])
        whole = len(m.accepted_sequences('EUC-KR'))
        values = m.unpack_order(zlib.decompress(base64.b64decode(
            converter_values('EUC-KR'))))

        def entry(cs='EUC-KR', **k):
            def edit(d):
                e = d['converter'][cs]
                for i, v in k.items():
                    e[int(i[1:])] = v
            return edit
        for case, edit in (
                ('not a map', lambda d: d.update(converter=[])),
                ('an encoding missing', lambda d: d['converter'].pop('EUC-TW')),
                # Well formed, of an encoding no locale has: refused for
                # being one more, not for its shape (16.23).
                ('an encoding more', lambda d: d['converter'].update(
                    {'KOI8-X': ['no locale', ['opens', None], None, None, None]})),
                ('an entry not a list', lambda d: d['converter'].update({'EUC-KR': 'x'})),
                ('an entry of six items', lambda d: d['converter']['EUC-KR'].append(None)),
                ('an entry of four items', lambda d: d['converter']['EUC-KR'].pop()),
                ('an unknown state', entry(e0='maybe')),
                ('iconv_open unknown', entry(e1=['maybe', None])),
                ('iconv_open of three items', entry(e1=['opens', None, None])),
                ('opens with a reason', entry(e1=['opens', 'x'])),
                ('fails with no reason', entry(e1=['fails', ''])),
                ('fails with None', entry(e1=['fails', None])),
                ('not asked with a reason', entry(e0='not checked', e1=['not asked', 'x'], e3='y')),
                ('not asked beside a measurement', entry(e1=['not asked', None])),
                ('not the first locale', entry(e2='ko_KR.euckr')),
                ('a locale of another encoding', entry(e2='ja_JP')),
                ('a locale where the machine has none', entry('EUC-TW', e2='ko_KR')),
                ('no locale with a list', entry('EUC-TW', e3=converter_values('EUC-TW'))),
                ('no locale with a digest', entry('EUC-TW', e4=sequences_of('EUC-TW'))),
                ('no locale where there is one', entry(e0='no locale', e3=None, e4=None)),
                ('not checked with no reason', entry(e0='not checked', e3='', e4=None)),
                ('not checked with None', entry(e0='not checked', e3=None, e4=None)),
                ('not checked with a digest', entry(e0='not checked', e3='x')),
                ('not checked with no locale, glibc asked',
                 entry('EUC-TW', e0='not checked', e3='x')),
                ('measured with no locale', entry('EUC-TW', e0='measured',
                                                  e3=converter_values('EUC-TW'),
                                                  e4=sequences_of('EUC-TW'))),
                ('measured with no digest', entry(e4=None)),
                ('a digest in capitals', entry(e4=sequences_of('EUC-KR').upper())),
                ('one value short', entry(e3=cut(whole - 1))),
                ('one value more', entry(e3=m.pack_order(values + [0x41]))),
                ('past a wchar_t of glibc', entry(e3=m.pack_order([0x80000000] + values[1:]))),
                ('below zero', entry(e3=m.pack_order([-1] + values[1:]))),
                ('not base64', entry(e3='!!')),
                ('not zlib', entry(e3=base64.b64encode(b'x').decode())),
                ('cut mid-number', entry(e3=m.pack(b'\x80'))),
                ('not text', entry(e3=5))):
            with self.subTest(case=case):
                self.refused(self.MALFORMED, edit)

    def test_each_form_converter_part_writes_is_read(self):
        """The control: measured, no locale, not checked, glibc not asked,
        a converter that does not open and converts nothing, and values at
        both ends of a wchar_t of glibc."""
        values = m.unpack_order(zlib.decompress(base64.b64decode(
            converter_values('EUC-KR'))))
        values[-2:] = [0, 0x7FFFFFFF]

        def edit(d):
            c = d['converter']
            c['EUC-KR'][1:4] = [['fails', 'Invalid argument'], 'ko_KR',
                                m.pack_order([0] * len(values))]
            c['GB2312'] = ['not checked', ['opens', None], 'zh_CN',
                           'measuring it failed: x', None]
            c['EUC-JP'] = ['not checked', ['not asked', None], 'ja_JP', 'y', None]
            c['EUC-TW'] = ['not checked', ['not asked', None], None, 'z', None]
            c['TIS-620'][3] = m.pack_order(values[:255])
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9)), edit)
        c = m.load(new, m.corpus_digest())[0]['converter']
        self.assertEqual([c[cs][0] for cs in m.CONVERTER_CODESETS],
                         ['not checked', 'measured', 'not checked', 'not checked',
                          'measured', 'no locale', 'no locale', 'measured',
                          'no locale'])

    def test_measured_over_other_sequences(self):
        """A file measured by a copy of this script whose list of
        sequences differs, of the same length or not, is refused by the
        copy that compares, naming the encodings: its values would pair
        with other bytes (review round 2: one more sequence was refused as
        a damaged file)."""
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9)))
        kr, cn = m.accepted_sequences('EUC-KR'), m.accepted_sequences('GB2312')
        for case, lists in (
                ('one more', {'EUC-KR': kr + [b'\xff'], 'GB2312': cn + [b'\xff']}),
                ('one fewer', {'EUC-KR': kr[:-1], 'GB2312': cn}),
                ('one swapped', {'EUC-KR': kr[:-1] + [b'\xff'], 'GB2312': cn})):
            with self.subTest(case=case):
                err = io.StringIO()
                with unittest.mock.patch.dict(m.SEQUENCES, lists), \
                        unittest.mock.patch.dict(m.SEQUENCE_DIGESTS, clear=True), \
                        unittest.mock.patch.object(m.sys, 'stderr', err), \
                        self.assertRaises(SystemExit):
                    m.load(new, m.corpus_digest())
                named = 'EUC-KR, GB2312' if case == 'one more' else 'EUC-KR'
                self.assertIn(f'measured the converter of {named} over other '
                              'byte sequences than this script uses',
                              flat(err.getvalue()))

    def test_the_fourth_format_compares_as_before(self):
        """A file of the format before this one is whole and compares, its
        test of one character read as before: here, where it says what the
        converter part says, a pair with one names the same checks as a
        pair of two files of this format, but for what only the part
        measures, the bytes a converter lacks and what an EUC database
        hands glibc's character rules."""
        pairs = []
        for form in ('locale_order/4', m.FORMAT):
            data = json.loads(self.texts[RHEL8])
            with_parts(form=form, use=measured_use(8))(data)
            data['content'] = m.content_digest(data)
            path = os.path.join(self.tmp, f'fourth-{form[-1]}.out')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps(data, sort_keys=True) + '\n')
            pairs.append(self.checks(path, self.made(RHEL9, 'new', with_parts(
                use=measured_use(9)))))
        said = [{k: d.pop(k) for k in ('upper unknown', 'lacking', 'whole') if k in d}
                for found in pairs for kind, d in found
                if kind == 'bytes' and 'other encoding' in d]
        self.assertEqual(said, [
            {'upper unknown': {'on the old machine': {
                'hy_AM.armscii8': 'ARMSCII-8', 'th_TH.tis620': 'TIS-620'}}},
            {'lacking': {}, 'whole': {'ARMSCII-8': 'on the old machine',
                                      'TIS-620': 'on the old machine'}}])
        euc = [[found.pop(i) for i in reversed(range(len(found)))
                if found[i][0] == 'ctype' and any(k.startswith('euc')
                                                  for k in found[i][1])]
               for found in pairs]
        why = ('the OLD file was written by an earlier version of locale_order.py, '
               "which did not measure glibc's converter")
        names = ('ja_JP', 'ja_JP.eucjp', 'ko_KR', 'ko_KR.euckr', 'zh_CN')
        self.assertEqual(euc, [
            [('ctype', {'euc unknown': {n: {'old machine': why} for n in names}})],
            [('ctype', {'euc ascii': 'new', 'same': True,
                        'names': {n: EUC_OF[n] for n in names}})]])
        # The old file tried one character of each EUC encoding: where the
        # pair of this format says the old machine converts, the other
        # says the rest of the text is not known there (16.18 c).
        changed = [pairs[1].pop(i) for i, (kind, d) in enumerate(pairs[1])
                   if kind == 'use' and 'changed' in d]
        unsaid = [pairs[0].pop(i) for i, (kind, d) in enumerate(pairs[0])
                  if kind == 'use' and d.get('why') == m.UNSAID]
        self.assertEqual(changed, [('use', {'changed': {n: EUC_OF[n] for n in names}})])
        self.assertEqual(unsaid, [('use', {'one side': 'new', 'why': m.UNSAID,
                                           'names': {n: EUC_OF[n] for n in names}})])
        # Nor did it measure the conversion back, nor UTF-8 past one
        # character: every locale whose text it reads past ASCII is counted
        # in the line on that, de_DE.utf8 among them, never as converting;
        # th_TH.tis620 is not, as the line on another encoding says that
        # machine is not known there.
        early = [pairs[0].pop(i) for i, (kind, d) in enumerate(pairs[0])
                 if kind == 'use' and 'back earlier' in d]
        self.assertEqual(early, [('use', {
            'back earlier': ['OLD'], 'utf8': 1, 'counts': {
                'EUC-JP': 2, 'EUC-KR': 2, 'GB2312': 1, 'UTF-8': 1}})])
        self.assertEqual([d['converts'] for found in pairs for kind, d in found
                          if kind == 'use' and 'converts' in d], [0, 1])
        for kind, d in pairs[0]:
            d.pop('converts', None)
        for kind, d in pairs[1]:
            d.pop('converts', None)
        self.assertEqual(pairs[0], pairs[1])


# --- the conversion back to bytes, and UTF-8's converter --------------------


def every_point():
    """CODEPOINTS, worked out here: every Unicode scalar value but NUL."""
    return [c for c in range(1, 0x110000) if not 0xD800 <= c <= 0xDFFF]


POINTS_DIGEST = []   # the digest of every_point(), once per process


def points_of():
    """The digest a measured back or forward entry keeps of its characters:
    sha256 of them in UTF-8, worked out here."""
    if not POINTS_DIGEST:
        POINTS_DIGEST.append(hashlib.sha256(
            ''.join(map(chr, every_point())).encode('utf-8')).hexdigest())
    return POINTS_DIGEST[0]


def leb(n):
    """n in seven bits a byte, the high bit set on all but the last."""
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def back_table(answers):
    """A back table as back_of() packs one from {code point: bytes, b''
    for none, or None for a refusal}: each in ascending order, after the
    step from the one before, its length (0xFF for a refusal) and bytes."""
    raw, prev = bytearray(), 0
    for c in sorted(answers):
        got = answers[c]
        raw += leb(c - prev) + (bytes([0xFF]) if got is None
                                else bytes([len(got)]) + got)
        prev = c
    return base64.b64encode(zlib.compress(bytes(raw), 9)).decode('ascii')


def forward_table(answers):
    """UTF-8's forward table as utf8_forward_of() packs one from {code
    point: what mbrtowc gave}."""
    raw, prev = bytearray(), 0
    for c in sorted(answers):
        raw += leb(c - prev) + leb(answers[c])
        prev = c
    return base64.b64encode(zlib.compress(bytes(raw), 9)).decode('ascii')


TAGS = range(0xE0000, 0xE0080)
BACK_DEFAULTS = {}   # per codeset and whether the converter opens


def back_values(codeset, opens=True):
    """The back table of a machine whose converter part is converter_of_use's:
    UTF-8 as standard (no record); each of the nine giving back, for each
    character converter_values() reads, the first sequence in byte order
    that reads as it, and no bytes for the tag characters, as glibc's nine
    do with glibc-gconv-extra; without it, ASCII alone. Packed once per
    process."""
    key = (codeset, opens)
    if key not in BACK_DEFAULTS:
        if codeset == 'UTF-8':
            answers = {}
        elif not opens:
            answers = {c: bytes([c]) for c in range(1, 0x80)}
        else:
            answers = {}
            values = m.unpack_order(zlib.decompress(base64.b64decode(
                converter_values(codeset))))
            for s, c in zip(m.accepted_sequences(codeset), values):
                if c:
                    answers.setdefault(c, s)
            answers.update({c: b'' for c in TAGS})
        BACK_DEFAULTS[key] = back_table(answers)
    return BACK_DEFAULTS[key]


def round_trip_of_use(use, converter):
    """UTF-8's forward entry and the back part of a machine whose
    postgres_use is use and whose converter part is converter: each asked
    under the first locale of its encoding by name, or no locale of it; the
    back of each of the nine opens and converts as its converter part says
    (ASCII alone where iconv_open failed); UTF-8 converts as standard both
    ways."""
    first = {}
    for name in sorted(use):
        first.setdefault(use[name]['codeset'], name)
    forward = (['measured', ['opens', None], first['UTF-8'], forward_table({}),
                points_of()] if 'UTF-8' in first else
               ['no locale', ['opens', None], None, None, None])
    part = {}
    for cs in ('UTF-8',) + m.CONVERTER_CODESETS:
        opens = cs == 'UTF-8' or converter[cs][1][0] == 'opens'
        answer = ['opens', None] if opens else ['fails', 'Invalid argument']
        if cs not in first:
            part[cs] = ['no locale', answer, None, None, None]
        elif cs != 'UTF-8' and converter[cs][0] == 'not checked':
            part[cs] = ['not checked', answer, first[cs], converter[cs][3], None]
        else:
            part[cs] = ['measured', answer, first[cs], back_values(cs, opens),
                        points_of()]
    return forward, part


class FakeRoundTrip(FakeConverter):
    """FakeConverter with wcrtomb: back maps a code point to the bytes
    wcrtomb writes for it (b'' for none, without an error); any other is
    refused (-1), and leaves the state not clear, as POSIX allows after an
    error; a call that finds the state not clear refuses too, so that a
    state not cleared before each call shows. Every call is recorded with
    the LC_CTYPE then held."""

    def __init__(self, chars, opens=0, ctype=lambda: None, back=None,
                 opens_back=0, long=()):
        super().__init__(chars, opens, ctype)
        self.back, self.opens_back, self.long = back or {}, opens_back, long

    def iconv_open(self, to, frm):
        if frm == b'WCHAR_T':
            self.calls.append(('iconv_open', to, frm))
            if self.opens_back:
                m.ctypes.set_errno(self.opens_back)
                return m.NO_ICONV
            return 4321
        return super().iconv_open(to, frm)

    def wcrtomb(self, buf, wc, state):
        self.calls.append(('wcrtomb', wc, self.ctype()))
        if wc in self.long:
            return m.MB_LEN_MAX + 1
        if state.raw.strip(b'\0') or wc not in self.back:
            state.raw = b'\1' * len(state)
            return m.NOT_CONVERTED
        got = self.back[wc]
        m.ctypes.memmove(buf, got, len(got))
        return len(got)


class BackOnTheMachine(unittest.TestCase):
    """back_of(), utf8_forward_of(), round_trip_parts() and what the run
    says when it ends: what glibc gives converting each character back to
    the bytes of each encoding, and reading each character's UTF-8, asked
    once per encoding. glibc is a double, and the characters are cut to a
    few. Freezes a character converted to no bytes read as one refused or
    the other way round, iconv_open asked one way only, a state left by
    the character before, a measurement that failed read as one that
    worked, or left out of the end of the run."""

    POINTS = [0x41, 0xE9, 0x3000, 0xE0001, 0x10FFFF]
    BACK = {0x41: b'A', 0x3000: b'\xa1\xa1', 0xE0001: b''}

    def run_job(self, job, name='ja_JP.eucjp', refuse=False, **fake):
        """job() under a double, over POINTS, with LC_CTYPE checked put
        back."""
        state = {m.locale.LC_CTYPE: 'C'}

        def setlocale(category, n=None):
            if n is None:
                return state[category]
            if refuse and n == name:
                raise m.locale.Error('unsupported locale setting')
            state[category] = n
            return n
        fake.setdefault('back', self.BACK)
        chars = fake.pop('chars', {chr(c).encode('utf-8'): c for c in self.POINTS})
        self.fake = FakeRoundTrip(chars, ctype=lambda: state[m.locale.LC_CTYPE],
                                  **fake)
        with unittest.mock.patch.object(m.locale, 'setlocale', setlocale), \
                unittest.mock.patch.object(m, 'converter_libc', lambda: self.fake), \
                unittest.mock.patch.object(m, 'BACK_POINTS', self.POINTS), \
                unittest.mock.patch.dict(m.POINT_DIGESTS, clear=True):
            got = job(name)
            digest = m.points_digest()
        self.assertEqual(state, {m.locale.LC_CTYPE: 'C'}, 'LC_CTYPE not put back')
        return got, digest

    def back(self, codeset='EUC-JP', **k):
        (cs, entry), digest = self.run_job(lambda n: m.back_of((codeset, n)), **k)
        self.assertEqual(cs, codeset)
        return entry, digest

    def test_each_character_under_the_locale(self):
        """Bytes, no bytes and a refusal each kept as itself, and only
        what differs from a refusal kept for one of the nine;
        iconv_open asked from wide characters to the encoding."""
        entry, digest = self.back()
        self.assertEqual(entry[:3], ['measured', ['opens', None], 'ja_JP.eucjp'])
        self.assertEqual(entry[4], digest)
        self.assertEqual(m.back_unpacked(entry[3], 'EUC-JP'),
                         {0x41: b'A', 0x3000: b'\xa1\xa1', 0xE0001: b''})
        self.assertEqual(entry[3], back_table(self.BACK))
        self.assertEqual(self.fake.calls[:2], [('iconv_open', b'EUC-JP', b'WCHAR_T'),
                                               ('iconv_close', 4321)])
        self.assertEqual([c[1:] for c in self.fake.calls if c[0] == 'wcrtomb'],
                         [(c, 'ja_JP.eucjp') for c in self.POINTS])

    def test_no_bytes_is_not_a_refusal(self):
        """The tag characters: wcrtomb gives 0 without an error. In UTF-8,
        where a refusal is kept and a character's own bytes are not, each
        of the three is told apart from the other two."""
        entry, _ = self.back('UTF-8', back={0x41: b'A', 0xE9: b'\xc3\xa9',
                                            0x3000: b'\xe3\x80\x80',
                                            0xE0001: b''},
                             name='en_US.utf8')
        self.assertEqual(m.back_unpacked(entry[3], 'UTF-8'),
                         {0xE0001: b'', 0x10FFFF: None})
        self.assertEqual(entry[3], back_table({0xE0001: b'', 0x10FFFF: None}))

    def test_a_state_left_by_the_character_before_is_cleared(self):
        """0xE9 is refused and leaves the state not clear: read after it,
        U+3000 would be refused too."""
        entry, _ = self.back()
        self.assertEqual(m.back_unpacked(entry[3], 'EUC-JP')[0x3000], b'\xa1\xa1')

    def test_more_bytes_than_a_character_can_have(self):
        entry, _ = self.back(long=(0x3000,))
        self.assertEqual(entry, ['not checked', ['opens', None], 'ja_JP.eucjp',
                                 f'glibc gave {m.MB_LEN_MAX + 1} bytes for U+3000, '
                                 'more than a character can have', None])

    def test_a_converter_back_that_does_not_open(self):
        """Without glibc-gconv-extra: iconv_open fails, and what glibc
        gives is still measured."""
        entry, _ = self.back(opens_back=22, back={0x41: b'A'})
        self.assertEqual(entry[:3], ['measured', ['fails', os.strerror(22)],
                                     'ja_JP.eucjp'])
        self.assertEqual(m.back_unpacked(entry[3], 'EUC-JP'), {0x41: b'A'})

    def test_no_locale_failure_and_glibc_not_asked(self):
        """Each way back_of() and utf8_forward_of() measure nothing: no
        locale of the encoding (iconv_open still asked), LC_CTYPE refused,
        and glibc that cannot be asked."""
        for case, job in (('back', lambda n: m.back_of(('EUC-JP', n))[1]),
                          ('forward', m.utf8_forward_of)):
            with self.subTest(case=case):
                entry, _ = self.run_job(job, name=None)
                self.assertEqual(entry[0], 'no locale')
                self.assertEqual(entry[2:], [None, None, None])
                self.assertEqual([c[0] for c in self.fake.calls],
                                 ['iconv_open', 'iconv_close'])
                entry, _ = self.run_job(job, refuse=True)
                self.assertEqual(entry, ['not checked', ['opens', None], 'ja_JP.eucjp',
                                         "measuring it failed: Error('unsupported "
                                         "locale setting')", None])
        for name in ('ko_KR', None):
            with self.subTest(locale=name), unittest.mock.patch.object(
                    m, 'converter_libc', unittest.mock.Mock(
                        side_effect=AttributeError('iconv_open'))):
                self.assertEqual(m.back_of(('EUC-KR', name))[1], [
                    'not checked', ['not asked', None], name,
                    "asking glibc failed: AttributeError('iconv_open')", None])
                self.assertEqual(m.utf8_forward_of(name), [
                    'not checked', ['not asked', None], name,
                    "asking glibc failed: AttributeError('iconv_open')", None])

    def forward(self, **k):
        k.setdefault('name', 'en_US.utf8')
        return self.run_job(m.utf8_forward_of, **k)

    def test_utf8_read_to_wide_characters(self):
        """Only a character not read as itself is kept: a refusal as 0;
        iconv_open asked from UTF-8 to wide characters."""
        chars = {chr(c).encode('utf-8'): c for c in self.POINTS if c != 0xE9}
        chars[chr(0x3000).encode('utf-8')] = 0x3001
        entry, digest = self.forward(chars=chars)
        self.assertEqual(entry[:3], ['measured', ['opens', None], 'en_US.utf8'])
        self.assertEqual(entry[4], digest)
        self.assertEqual(m.forward_unpacked(entry[3]), {0xE9: 0, 0x3000: 0x3001})
        self.assertEqual(entry[3], forward_table({0xE9: 0, 0x3000: 0x3001}))
        self.assertEqual(self.fake.calls[:2], [('iconv_open', b'WCHAR_T', b'UTF-8'),
                                               ('iconv_close', 1234)])
        self.assertEqual([c[1] for c in self.fake.calls if c[0] == 'mbrtowc'],
                         [chr(c).encode('utf-8') for c in self.POINTS])

    def test_utf8_read_neither_whole_nor_refused(self):
        chars = {chr(c).encode('utf-8'): c for c in self.POINTS}
        chars[chr(0x3000).encode('utf-8') + b'x'] = 0x2E00
        del chars[chr(0x3000).encode('utf-8')]
        entry, _ = self.forward(chars=chars)
        self.assertEqual(entry, ['not checked', ['opens', None], 'en_US.utf8',
                                 'glibc read the bytes E3 80 80 neither as one '
                                 'character nor as an error (mbrtowc gave -2)', None])

    def test_the_first_locale_of_each_encoding(self):
        use = {'ja_JP': use_of('one byte', 'EUC-JP'),
               'ja_JP.eucjp': use_of('one byte', 'EUC-JP'),
               'en_US.utf8': use_of('converts', 'UTF-8', (0x80, 'c280')),
               'C.utf8': use_of('converts', 'UTF-8', (0x80, 'c280')),
               'C': use_of('one byte', 'ANSI_X3.4-1968')}
        asked = []
        with unittest.mock.patch.multiple(
                m, utf8_forward_of=lambda name: asked.append(('forward', name)) or 'f',
                back_of=lambda job: asked.append(('back',) + job) or (job[0], job[1])):
            forward, back = m.round_trip_parts(use, lambda f, js: [f(j) for j in js])
        self.assertEqual(forward, 'f')
        self.assertEqual(sorted(asked, key=repr), sorted(
            [('forward', 'C.utf8'), ('back', 'UTF-8', 'C.utf8'),
             ('back', 'EUC-JP', 'ja_JP')]
            + [('back', cs, None) for cs in m.CONVERTER_CODESETS if cs != 'EUC-JP'],
            key=repr))
        self.assertEqual(back, dict({cs: None for cs in m.CONVERTER_CODESETS},
                                    **{'UTF-8': 'C.utf8', 'EUC-JP': 'ja_JP'}))

    def test_the_run_says_when_a_conversion_was_not_measured(self):
        """finished(): UTF-8's forward not measured, and each encoding whose
        conversion back was not measured, named with why, in the order of
        BACK_CODESETS; either alone makes the exit status 1, and one with
        no locale is not a failure of the run."""
        part = {cs: ['no locale', ['opens', None], None, None, None]
                for cs in ('UTF-8',) + m.CONVERTER_CODESETS}
        part['UTF-8'] = ['not checked', ['opens', None], 'C.utf8', 'why u', None]
        part['GB2312'] = ['not checked', ['not asked', None], None, 'why cn', None]
        out = {'locales': {}, 'locales_listed': 0, 'postgres_libc': dict(CLEAN_LIBC),
               'ctype': {'locales': {}, 'tables': {}, 'wide': {}, 'bytes': {}},
               'converter': dict(NO_CONVERTER_LOCALES),
               'utf8_forward': ['not checked', ['opens', None], 'C.utf8', 'why f',
                                None],
               'back': part}
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 1)
        self.assertIn("!! What glibc's converter of UTF-8 gives for each character "
                      "could not be measured:\n  UTF-8: why f\n", screen.getvalue())
        self.assertIn('!! What glibc gives converting characters back to bytes could '
                      'not be measured for 2 encoding(s):\n  UTF-8: why u\n'
                      '  GB2312: why cn\n', screen.getvalue())
        forward = out['utf8_forward']
        out['utf8_forward'] = ['no locale', ['opens', None], None, None, None]
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 1)
        self.assertNotIn('converter of UTF-8', screen.getvalue())
        part['UTF-8'] = part['GB2312'] = ['no locale', ['opens', None], None, None,
                                          None]
        out['utf8_forward'] = forward
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 1)
        self.assertNotIn('back to bytes', screen.getvalue())
        out['utf8_forward'] = ['no locale', ['opens', None], None, None, None]
        screen = Screen()
        with unittest.mock.patch.object(m.sys, 'stderr', screen):
            self.assertEqual(m.finished(out, 1, 10), 0)
        self.assertNotIn('!!', screen.getvalue())

    def test_measure_writes_the_parts(self):
        """The call site: measure() writes the two parts round_trip_parts
        makes from the answers of postgres_use."""
        with unittest.mock.patch.multiple(
                m, libc_version=lambda: 'glibc 2.34',
                list_locales=lambda: ['a', 'b'],
                glibc_build=lambda: 'glibc-2.34-1.el9.x86_64',
                measure_one=lambda name: (name, {'error': 'x'}, None),
                postgres_use=lambda name: use_of('converts', 'UTF-8', (0x80, 'c280')),
                ctype_identity=lambda name, use: ['table', 'd' * 64],
                ctype_part=lambda ids, f: {},
                converter_of=lambda job: (job[0], ['asked', job[1]]),
                round_trip_of=lambda job: (job[0] if job[0] == 'forward' else job[1],
                                           ['asked', job[2]]),
                UTF8_BYTES=None,
                fork_pool=lambda jobs: ThreadPoolExecutor(1),
                money_digits=lambda names: {n: 3 for n in names},
                postgres_libc=lambda: dict(CLEAN_LIBC)), \
                unittest.mock.patch.object(m.os, 'nice'), \
                unittest.mock.patch.object(m.sys, 'stderr', Screen()):
            text, out, _ = m.measure(1)
        data = json.loads(text)
        self.assertEqual(data['parts'][-2:], ['utf8_forward', 'back'])
        self.assertEqual(data['utf8_forward'], ['asked', 'a'])
        self.assertEqual(data['back']['UTF-8'], ['asked', 'a'])
        self.assertEqual(data['back']['EUC-JP'], ['asked', None])


class BackRead(Checks):
    """A file of this format whose back part or UTF-8's forward part is
    missing or malformed is refused, and one of the format before is read
    without them: read as missing they would say "not checked" where the
    machine did check, and a table of another shape would pair characters
    with the wrong bytes."""

    BACK = 'what it says of the conversion of characters back to bytes is malformed'
    FORWARD = 'what it says of the converter of UTF-8 is malformed'

    @classmethod
    def setUpClass(cls):
        super(Checks, cls).setUpClass()
        for path in (RHEL8, RHEL9):
            cls.texts[path] = trimmed(cls.texts[path], USE_NAMES)

    def refused(self, phrase, *edits, form=m.FORMAT):
        new = self.made(RHEL9, 'new', with_parts(form=form, use=measured_use(9)),
                        *edits)
        err = io.StringIO()
        with unittest.mock.patch.object(m.sys, 'stderr', err), \
                self.assertRaises(SystemExit) as stop:
            m.load(new, m.corpus_digest())
        self.assertEqual(stop.exception.code, 2)
        self.assertIn(phrase, flat(err.getvalue()))

    def test_without_either_part(self):
        for part in ('back', 'utf8_forward'):
            with self.subTest(part=part):
                self.refused('lacks a part its format holds', lambda d: d.pop(part))

    def test_a_fifth_format_file_with_a_part(self):
        for part in ('back', 'utf8_forward'):
            with self.subTest(part=part):
                self.refused('holds one it does not', lambda d: d.update({part: {}}),
                             form='locale_order/5')

    def test_back_malformed(self):
        def entry(cs='EUC-KR', **k):
            def edit(d):
                e = d['back'][cs]
                for i, v in k.items():
                    e[int(i[1:])] = v
            return edit
        kr = lambda answers: entry(e3=back_table(answers))
        u8 = lambda answers: entry('UTF-8', e3=back_table(answers))
        raw = lambda b: entry(e3=base64.b64encode(zlib.compress(b)).decode())
        for case, edit in (
                ('not a map', lambda d: d.update(back=[])),
                ('UTF-8 missing', lambda d: d['back'].pop('UTF-8')),
                ('one of the nine missing', lambda d: d['back'].pop('EUC-TW')),
                ('an encoding more', lambda d: d['back'].update(
                    {'KOI8-X': ['no locale', ['opens', None], None, None, None]})),
                ('an entry of four items', lambda d: d['back']['EUC-KR'].pop()),
                ('an unknown state', entry(e0='maybe')),
                ('iconv_open unknown', entry(e1=['maybe', None])),
                ('fails with no reason', entry(e1=['fails', ''])),
                ('not the first locale', entry(e2='ko_KR.euckr')),
                ('a locale where the machine has none', entry('EUC-TW', e2='ko_KR')),
                ('no locale with a table', entry('EUC-TW', e3=back_table({}))),
                ('not checked with no reason', entry(e0='not checked', e3='', e4=None)),
                ('measured with no digest', entry(e4=None)),
                ('a digest in capitals', entry(e4=points_of().upper())),
                ('not base64', entry(e3='!!')),
                ('not zlib', entry(e3=base64.b64encode(b'x').decode())),
                ('not text', entry(e3=5)),
                ('cut mid-step', raw(b'\x80')),
                ('cut before the length', raw(b'\x41')),
                ('cut mid-bytes', raw(b'\x41\x02\x41')),
                ('a length past MB_LEN_MAX', raw(b'\x41\x11' + b'A' * 17)),
                ('a step of nothing', raw(b'\x41\x01A\x00\x01A')),
                ('NUL', kr({0: b'A'})),
                ('a surrogate', kr({0xD800: b'A'})),
                ('past Unicode', kr({0x110000: b'A'})),
                ('a refusal kept for one of the nine', kr({0x41: b'A', 0x42: None})),
                ('UTF-8 kept as its own bytes', u8({0xE9: b'\xc3\xa9'})),
                ('UTF-8 with a refusal of no bytes confused', u8({0xE0001: b'\xf3\xa0\x80\x81'}))):
            with self.subTest(case=case):
                self.refused(self.BACK, edit)

    def test_a_table_read_cannot_be_changed(self):
        """back_unpacked keeps what it read and hands the same answer to
        every reader, two machines with the same table among them: a reader
        cannot change it under the other."""
        got = m.back_unpacked(back_table({0x41: b'A'}), 'EUC-KR')
        with self.assertRaises(TypeError):
            got[0x41] = None
        self.assertEqual(dict(m.back_unpacked(back_table({0x41: b'A'}), 'EUC-KR')),
                         {0x41: b'A'})

    def test_one_table_read_for_each_encoding(self):
        """The same packed table is whole for one of the nine and not for
        UTF-8, where it would keep a character as its own bytes; what was
        read for one encoding is never the answer for another."""
        table = back_table({0x41: b'A'})
        self.assertEqual(m.back_unpacked(table, 'EUC-KR'), {0x41: b'A'})
        self.assertIsNone(m.back_unpacked(table, 'UTF-8'))
        self.assertEqual(m.back_unpacked(table, 'EUC-KR'), {0x41: b'A'})

    def test_forward_malformed(self):
        def entry(**k):
            def edit(d):
                for i, v in k.items():
                    d['utf8_forward'][int(i[1:])] = v
            return edit
        raw = lambda b: entry(e3=base64.b64encode(zlib.compress(b)).decode())
        for case, edit in (
                ('not a list', lambda d: d.update(utf8_forward={})),
                ('an unknown state', entry(e0='maybe')),
                ('not the first locale', entry(e2='en_US.utf8')),
                ('no locale with a table', entry(e0='no locale', e2=None)),
                ('measured with no digest', entry(e4=None)),
                ('not zlib', entry(e3=base64.b64encode(b'x').decode())),
                ('cut mid-value', raw(b'\x41\x80')),
                ('a step of nothing', raw(b'\x41\x00\x00\x00')),
                ('a character kept as itself', entry(e3=forward_table({0x41: 0x41}))),
                ('past a wchar_t of glibc', entry(e3=forward_table({0x41: 0x80000000}))),
                ('a surrogate', entry(e3=forward_table({0xD800: 0})))):
            with self.subTest(case=case):
                self.refused(self.FORWARD, edit)

    def test_measured_over_other_characters(self):
        """A file measured by a copy whose list of characters differs is
        refused by the copy that compares, naming each conversion so
        measured, and only those."""
        def digest(*where):
            def edit(d):
                for part, cs in where:
                    (d[part] if cs is None else d[part][cs])[4] = 'a' * 64
            return edit
        for case, edit, named in (
                ('the back of one', digest(('back', 'EUC-KR')), 'EUC-KR back to bytes'),
                ('the back of two', digest(('back', 'UTF-8'), ('back', 'EUC-KR')),
                 'UTF-8 back to bytes, EUC-KR back to bytes'),
                ('the forward of UTF-8', digest(('utf8_forward', None)),
                 'UTF-8 to wide characters')):
            with self.subTest(case=case):
                self.refused(f'measured the conversion of {named} over other '
                             'characters than this script uses', edit)
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9)))
        data = read_json(new)
        measured = (['UTF-8 to wide characters'] if data['utf8_forward'][0] == 'measured'
                    else []) + [f'{cs} back to bytes' for cs in ('UTF-8',) + m.CONVERTER_CODESETS
                                if data['back'][cs][0] == 'measured']
        self.assertGreater(len(measured), 2)
        for case, points in (('one fewer', every_point()[:-1]),
                             ('one swapped', every_point()[:-1] + [0])):
            with self.subTest(case=case):
                err = io.StringIO()
                with unittest.mock.patch.object(m, 'BACK_POINTS', points), \
                        unittest.mock.patch.dict(m.POINT_DIGESTS, clear=True), \
                        unittest.mock.patch.object(m.sys, 'stderr', err), \
                        self.assertRaises(SystemExit):
                    m.load(new, m.corpus_digest())
                self.assertIn(f'measured the conversion of {", ".join(measured)} over '
                              'other characters than this script uses',
                              flat(err.getvalue()))

    def test_each_form_is_read(self):
        """The control: measured with bytes, no bytes and refusals, no
        locale, not checked, glibc not asked, a converter back that does
        not open; UTF-8 read otherwise than standard both ways."""
        def edit(d):
            b = d['back']
            b['UTF-8'][3] = back_table({0xE0001: b'', 0x10FFFF: None, 0xE9: b'\xe9'})
            b['EUC-KR'][1:4] = [['fails', 'Invalid argument'], 'ko_KR',
                                back_table({0x41: b'A', 0xE0001: b''})]
            b['GB2312'] = ['not checked', ['opens', None], 'zh_CN', 'x', None]
            b['EUC-JP'] = ['not checked', ['not asked', None], 'ja_JP', 'y', None]
            b['EUC-TW'] = ['not checked', ['not asked', None], None, 'z', None]
            d['utf8_forward'][3] = forward_table({0xE9: 0, 0x10FFFF: 0x7FFFFFFF})
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9)), edit)
        d = m.load(new, m.corpus_digest())[0]
        self.assertEqual([d['back'][cs][0] for cs in m.BACK_CODESETS],
                         ['measured', 'not checked', 'measured', 'not checked',
                          'not checked', 'measured', 'no locale', 'no locale',
                          'measured', 'no locale'])
        self.assertEqual(d['utf8_forward'][0], 'measured')

    def test_the_fifth_format_compares_as_before(self):
        """A pair with a file of the format before names the same checks as
        a pair of two files of this one, but the line on what that file did
        not measure (the conversion back, and UTF-8 past one character),
        where it counts every locale whose text the old machine reads past
        ASCII, those in a line of their own on the new machine's error
        too, and none as converting."""
        pairs = []
        for form in ('locale_order/5', m.FORMAT):
            old = self.made(RHEL8, f'old{form[-1]}', with_parts(form=form,
                                                                use=measured_use(8)))
            pairs.append(self.checks(old, self.made(RHEL9, 'new', with_parts(
                use=measured_use(9)))))
        early = [pairs[0].pop(i) for i, (kind, d) in enumerate(pairs[0])
                 if kind == 'use' and 'back earlier' in d]
        self.assertEqual(early, [('use', {
            'back earlier': ['OLD'], 'utf8': 1, 'counts': {
                'ARMSCII-8': 1, 'EUC-JP': 2, 'EUC-KR': 2, 'GB2312': 1, 'TIS-620': 1,
                'UTF-8': 1}})])
        self.assertEqual([d.pop('converts') for found in pairs for kind, d in found
                          if kind == 'use' and 'converts' in d], [0, 1])
        self.assertEqual(pairs[0], pairs[1])


# --- the one model of a database's text on a machine (16.19) ---------------


class TextModelEdges(ConverterCompared):
    """The edges the model closed (backlog 16.18 a and g, 16.19 b, the row
    of US-ASCII), each on its own and read in the three outputs; 16.18 c,
    16.19 c and d are in the tests of the classes above that they changed."""

    # The tests of the classes above run there; here, only these.
    for name in [n for n in vars(ConverterCompared) if n.startswith('test_')]:
        locals()[name] = None
    del name

    def test_not_checked_is_not_known_in_any_database(self):
        """16.18 a: a locale of an encoding PostgreSQL does not know whose
        converter was not checked is NOT checked in a database of any
        encoding, never "in the line on a database of another encoding"
        alone."""
        found = self.use(*self.pair(form='locale_order/4'))
        self.assertIn({'unknown': {'hy_AM.armscii8': m.NO_CONVERTER,
                                   'zh_TW.euctw': m.NO_CONVERTER}}, found)
        # th_TH.tis620 converts its character: that one is elsewhere.
        self.assertEqual(found[-1]['elsewhere'], 1)

    def test_the_other_machine_s_bytes_only_under_one_encoding(self):
        """16.18 g: ja_JP is of EUC-JP on the old machine, whose glibc
        cannot convert it, and of EUC-KR on the new one: the old machine's
        row names a sequence of EUC-JP, never EUC-KR's bytes the new
        machine converts."""
        def kr(d):
            d['postgres_use']['ja_JP']['codeset'] = 'EUC-KR'
            d['locales']['ja_JP']['codeset'] = 'EUC-KR'
            refirst(d)
        report, summary, rows = self.outputs(*self.pair(
            measured_use(9, **EUC_TW_USE), measured_use(8, **EUC_TW_USE),
            new_edits=[kr]))
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('8E A1', 'error', 'not measured', 'on the old machine')])

    def test_a_failure_of_one_character_is_an_error_in_the_rows(self):
        """16.19 b: the old file tried one character of EUC-JP, which
        failed: glibc converts nothing past ASCII there, so the row of a
        sequence the new machine's converter refuses says error on both,
        as the line does, never "not measured" on the old one."""
        old = self.made(RHEL8, 'old', with_parts(use=measured_use(9, **EUC_TW_USE),
                                                 form='locale_order/4'))
        new = self.made(RHEL9, 'new', with_parts(
            use=measured_use(8, **EUC_TW_USE),
            converter=self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1',))))
        report, summary, rows = self.outputs(old, new)
        (label, (names, cs)), = rows.use_groups.items()
        self.assertEqual((names, cs), (('ja_JP', 'ja_JP.eucjp'), 'EUC-JP'))
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[(label,)]],
                         [('AD A1', 'error', 'error', 'on both machines')])

    def test_bytes_of_another_encoding_are_not_measured(self):
        """ja_JP is of EUC-JP on the old machine, which converts it, and of
        BIG5 on the new one, whose test of one character failed: the new
        machine's row is of A1 40, which no database of EUC-JP holds, so
        what the old machine's converter of EUC-JP does with it is not
        measured, never "converts"."""
        report, summary, rows = self.outputs(*self.pair(
            measured_use(8, **EUC_TW_USE),
            measured_use(9, ja_JP=use_of('fails', 'BIG5', (0x3000, 'a140')), **EUC_TW_USE),
            new_edits=[lambda d: d['locales']['ja_JP'].update(codeset='BIG5')]))
        self.assertIn({'from': 'EUC-JP', 'moved': 'new', 'names': {'ja_JP': 'EUC-JP'},
                       'to': 'BIG5'},
                      [d for kind, d in report['checks'] if kind == 'use'])
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower/ILIKE error'],
                         [('A1 40', 'not measured', 'error', 'on the new machine')])

    def test_an_euc_encoding_on_the_new_machine_only(self):
        """ja_JP is of ISO-8859-1 on the old machine and of EUC-JP on the
        new: the line on EUC names it as NOT checked, its encoding changed,
        whichever machine read the EUC encoding."""
        def iso(d):
            d['postgres_use']['ja_JP'] = use_of('one byte', 'ISO-8859-1')
            d['locales']['ja_JP']['codeset'] = 'ISO-8859-1'
            refirst(d)
        found = [d for kind, d in self.checks(*self.pair(old_edits=[iso]))
                 if kind == 'ctype']
        self.assertIn({'euc unknown': {'ja_JP': {
            'both machines': 'its encoding changed from ISO-8859-1 on the old '
                             'machine to EUC-JP on the new one'}}}, found)

    def test_the_other_encoding_line_beside_a_converter_not_checked(self):
        """The new machine could not measure the converter of TIS-620: the
        line on a database of another encoding says that machine is not
        known, beside what the old one measured, never as a converter that
        does not open."""
        entry = {'TIS-620': ['not checked', ['opens', None], 'th_TH.tis620',
                             'measuring it failed: x', None]}
        self.assertEqual(self.other(*self.pair(measured_use(8, **EUC_TW_USE),
                                               measured_use(8, **EUC_TW_USE),
                                               new_converter=entry)), [
            {'other encoding': {'hy_AM.armscii8': 'ARMSCII-8', 'th_TH.tis620': 'TIS-620'},
             'upper unknown': {'on the new machine': {'th_TH.tis620': 'TIS-620'}},
             'lacking': {}, 'whole': {'ARMSCII-8': 'on both machines',
                                      'TIS-620': 'on the old machine'}}])

    def test_us_ascii_serves_a_database_of_any_encoding(self):
        """The row of US-ASCII: PostgreSQL reads it as SQL_ASCII and lets a
        database of any encoding use it (chklocale.c:185), so it is NOT
        checked, never "an encoding of one byte" that never converts."""
        def ascii_(d):
            d['locales']['de_DE']['codeset'] = 'US-ASCII'
            d['postgres_use']['de_DE'] = use_of('one byte', 'US-ASCII')
        found = self.use(*self.pair(old_edits=[ascii_], new_edits=[ascii_]))
        self.assertIn({'unknown': {'de_DE': m.ASCII_ANY[:1].upper() + m.ASCII_ANY[1:]}},
                      found)
        self.assertEqual(found[-1]['one byte'], 2)

    def test_a_name_under_two_machines_is_refused(self):
        """16.19 f: the line on what is not known of part of the text names
        each locale under one machine; the reader refuses one under two."""
        head = (f'!! {m.FN}: whether they give an ERROR on part of the text is '
                'NOT known in 2 locale(s): a file written by an earlier version '
                'of locale_order.py tried one character of their encoding, and '
                "glibc's converter can refuse others. ")
        self.assertEqual(use_kind(head + 'On the new machine: EUC-JP: ja_JP. On the '
                                  'old machine: EUC-JP: ja_JP.eucjp.'),
                         ('use', {'untold': {
                             'the new machine': {'ja_JP': 'EUC-JP'},
                             'the old machine': {'ja_JP.eucjp': 'EUC-JP'}}}))
        with self.assertRaises(ReportShapeError):
            use_kind(head + 'On the new machine: EUC-JP: ja_JP. On the old '
                            'machine: EUC-JP: ja_JP.')


class TextModelBack(ConverterCompared):
    """The back leg the model answers (wcstombs of what upper(), lower() and
    initcap() give, under the locale's converter), as the model says it
    (the lines are BackPrinted's): per database a locale can serve, how
    many characters a database holds whose upper or lower case each
    machine does not turn back, what each machine's file cannot say, and
    how many characters both reach and turn back into other bytes."""

    for name in [n for n in vars(ConverterCompared) if n.startswith('test_')]:
        locals()[name] = None
    del name

    def backs(self, old, new):
        """{name: {kind: back_state}} of the model of the pair of files."""
        digest = m.corpus_digest()
        o, n = m.load(old, digest)[0], m.load(new, digest)[0]
        both = sorted(set(o['locales']) & set(n['locales']))
        model = m.TextModel(o, n, both, sorted(set(n['locales']) - set(o['locales'])))
        return {r['name']: r['back'] for r in model.use_records() if 'back' in r}

    def back_edit(self, codeset, answers):
        """An edit that gives the back part's entry of codeset the table of
        answers."""
        def edit(d):
            d['back'][codeset][3] = back_table(answers)
        return edit

    def test_measured_alike(self):
        """Both machines measured, nothing refused: no error, nothing not
        known, nothing converted otherwise; a database of one byte never
        converts back."""
        got = self.backs(*self.pair())
        nothing = {'error': {}, 'unknown': {}, 'changes': 0}
        self.assertEqual(got['de_DE.utf8'], {('enc', 'UTF-8'): nothing})
        self.assertEqual(got['ja_JP'], {('enc', 'EUC-JP'): nothing})
        self.assertEqual(got['de_DE'], {('enc', 'ISO-8859-1'): None})
        # A locale of an encoding PostgreSQL does not know, of one byte, in
        # a database of any encoding: glibc's converter reads that
        # database's bytes one by one, as it measured them.
        self.assertEqual(got['th_TH.tis620'], {('any',): nothing})

    def test_what_upper_gives_is_reached(self):
        """The character upper() gives for one EUC-JP reads, which the
        converter itself never gives, is refused back: an error there."""
        raw = json.dumps(
            {'upper': [[0x61, 0x41], [0x3000, 0x1E9E]], 'lower': [[0x41, 0x61]],
             'classes': {k: [[0x41, 0x41]] for k in m.CTYPE_CLASSES}},
            sort_keys=True, separators=(',', ':')).encode('ascii')
        key = hashlib.sha256(raw).hexdigest()

        def wide(d):
            for t in d['ctype']['tables'].values():
                t[0] = key
            d['ctype']['wide'] = {key: m.pack(raw)}
        got = self.backs(*self.pair(old_edits=[wide]))
        self.assertEqual(got['ja_JP'][('enc', 'EUC-JP')]['error'], {'old': 1})

    def test_a_database_whose_encoding_changed(self):
        """ja_JP.eucjp is of EUC-KR on the old machine and of EUC-JP on the
        new; ja_JP, of EUC-JP on both and of the same rules, is read first.
        A database of EUC-KR made on the old machine opens on the new one,
        whose converter of EUC-JP reads its bytes: not known there, never
        "nothing refused". One of EUC-JP made on the new machine does not
        exist on the old one, so the two are not compared (review round 1,
        A1 to A3)."""
        def kr(d):
            d['postgres_use']['ja_JP.eucjp']['codeset'] = 'EUC-KR'
            d['locales']['ja_JP.eucjp']['codeset'] = 'EUC-KR'
            refirst(d)
        got = self.backs(*self.pair(measured_use(8, **EUC_TW_USE),
                                    measured_use(8, **EUC_TW_USE), old_edits=[kr]))
        self.assertEqual(got['ja_JP'][('enc', 'EUC-JP')],
                         {'error': {}, 'unknown': {}, 'changes': 0})
        self.assertEqual(got['ja_JP.eucjp'], {
            ('enc', 'EUC-KR'): {'error': {}, 'changes': None, 'unknown': {
                'new': 'a database of another encoding read by the converter '
                       'of EUC-JP is not measured'}},
            ('enc', 'EUC-JP'): {'error': {}, 'unknown': {}, 'changes': None}})

    def test_a_database_of_another_encoding_under_utf8(self):
        """ko_KR is of EUC-KR on the old machine and of UTF-8 on the new:
        the database of EUC-KR carried over is read there by the converter
        of UTF-8, which nothing measured on such bytes."""
        def utf8(d):
            d['postgres_use']['ko_KR'] = use_of('converts', 'UTF-8', (0x80, 'c280'))
            d['locales']['ko_KR']['codeset'] = 'UTF-8'
            refirst(d)
        got = self.backs(*self.pair(measured_use(8, **EUC_TW_USE),
                                    measured_use(8, **EUC_TW_USE), new_edits=[utf8]))
        self.assertEqual(got['ko_KR'][('enc', 'EUC-KR')]['unknown'], {
            'new': 'a database of another encoding than UTF8 read by the '
                   'converter of UTF-8 is not measured'})

    def test_utf8_s_converter_not_checked(self):
        """UTF-8's forward part not checked on the new machine: not known
        there, with why."""
        def forward(d):
            e = d['utf8_forward']
            d['utf8_forward'] = ['not checked', e[1], e[2], 'measuring it failed: x', None]
        got = self.backs(*self.pair(new_edits=[forward]))
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['unknown'], {
            'new': "glibc's converter of UTF-8 could not be measured: measuring "
                   'it failed: x'})

    def test_a_file_that_says_nothing_of_postgresql_s_use(self):
        """A file of the second format: what the old machine does is not
        known, for why its file says nothing at all."""
        old = self.made(RHEL8, 'old', with_parts(form='locale_order/2'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9, **EUC_TW_USE)))
        got = self.backs(old, new)
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['unknown'], {
            'old': 'the OLD file was written by an earlier version of locale_order.py'})

    def test_a_character_refused_back_is_an_error_of_its_machine(self):
        """UTF-8 on the new machine refuses A back: every UTF-8 locale gives
        an error there, counted by the characters a database holds: A
        itself, and a, whose upper() is A. EUC-JP on the old machine gives
        back ASCII but A: what its converter reads past ASCII, A and a."""
        got = self.backs(*self.pair(new_edits=[self.back_edit('UTF-8', {0x41: None})]))
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['error'], {'new': 2})
        values = m.unpack_order(zlib.decompress(base64.b64decode(converter_values('EUC-JP'))))
        read = {v for v in values if v}
        ascii_back = {c: bytes([c]) for c in range(1, 0x80) if c != 0x41}
        got = self.backs(*self.pair(old_edits=[self.back_edit('EUC-JP', ascii_back)]))
        self.assertEqual(got['ja_JP'][('enc', 'EUC-JP')]['error'],
                         {'old': len((read - set(ascii_back)) | {0x61})})

    def test_no_bytes_is_not_a_refusal(self):
        """A tag character glibc gives no bytes for converts back without an
        error."""
        got = self.backs(*self.pair(new_edits=[self.back_edit('UTF-8', {0xE0041: b''})]))
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['error'], {})

    def test_what_utf8_does_not_read_is_not_reached(self):
        """UTF-8's converter on the new machine: a character it refuses
        never reaches the conversion back there, nor one it reads as
        another; A, which it refuses, still does, as upper() of a; a
        character it gives for another is reached."""
        def forward(answers):
            def edit(d):
                d['utf8_forward'][3] = forward_table(answers)
            return edit
        cases = (
            ('refused, no case', {0x100: 0}, {0x100: None}, {}),
            ('read as another', {0x100: 0x101}, {0x100: None}, {}),
            ('given for another', {0x100: 0x101}, {0x101: None}, {'new': 1}),
            ('refused, reached by upper()', {0x41: 0}, {0x41: None}, {'new': 1}))
        for case, read, back, want in cases:
            with self.subTest(case=case):
                got = self.backs(*self.pair(new_edits=[forward(read),
                                                       self.back_edit('UTF-8', back)]))
                self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['error'], want)

    def test_reached_on_one_machine_only(self):
        """A character the old machine's UTF-8 converter refuses, given
        other bytes back on the new one, is not counted: the old machine
        never reaches it."""
        def forward(d):
            d['utf8_forward'][3] = forward_table({0x100: 0})
        got = self.backs(*self.pair(old_edits=[forward],
                                    new_edits=[self.back_edit('UTF-8', {0x100: b'\xc4'})]))
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['changes'], 0)
        got = self.backs(*self.pair(new_edits=[self.back_edit('UTF-8', {0x100: b'\xc4'})]))
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')]['changes'], 1)

    def test_converted_otherwise(self):
        """A character both machines reach, given other bytes on the new
        one, is counted; one only the old machine reaches is not."""
        got = self.backs(*self.pair(new_edits=[self.back_edit('UTF-8', {0x41: b'\x41\x41'})]))
        self.assertEqual(got['de_DE.utf8'][('enc', 'UTF-8')],
                         {'error': {}, 'unknown': {}, 'changes': 1})
        # The new machine's EUC-JP converter does not open: it reads ASCII
        # alone, and the old machine's other bytes for a character past
        # ASCII are not reached there.
        values = m.unpack_order(zlib.decompress(base64.b64decode(converter_values('EUC-JP'))))
        past = next(v for v in values if v > 0x7F)
        table = m.back_unpacked(back_values('EUC-JP'), 'EUC-JP')
        moved = dict(table)
        moved[past] = b'\xa1\xa1'
        got = self.backs(*self.pair(measured_use(8, **EUC_TW_USE),
                                    measured_use(9, **EUC_TW_USE),
                                    old_edits=[self.back_edit('EUC-JP', moved)]))
        self.assertEqual(got['ja_JP'][('enc', 'EUC-JP')]['changes'], 0)

    def test_a_file_without_the_part_is_not_known(self):
        """A file of the format before says nothing of the way back: not
        known there, with why, and never "the same"."""
        old = self.made(RHEL8, 'old', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                 form='locale_order/5'))
        new = self.made(RHEL9, 'new', with_parts(use=measured_use(9, **EUC_TW_USE)))
        got = self.backs(old, new)
        why = ('the OLD file was written by an earlier version of '
               'locale_order.py, which did not measure the conversion back to bytes')
        for name, kind in (('de_DE.utf8', ('enc', 'UTF-8')), ('ja_JP', ('enc', 'EUC-JP'))):
            with self.subTest(name=name):
                self.assertEqual(got[name][kind], {'error': {}, 'unknown': {'old': why},
                                                   'changes': None})


class BackPrinted(ConverterCompared):
    """What step 11 prints of the back leg (PR 3 of backlog 16.19), on
    RHEL8 and RHEL9 cut to EUC_NAMES, read in its three outputs: the
    characters a machine does not turn back, with the rows of each; those
    the two machines turn back otherwise; a file of a format before the
    back part, counted, those in an error line too; a reason a machine
    cannot say, beside an error and without one; the edges of the forward
    leg the same PR closes (16.24, UTF-8's converter on every character).

    Freezes: a measured refusal of the conversion back left out of every
    line, or counted as converting; a count that does not say which
    functions read it; a file that did not measure the back leg read as
    one that did, or the locale of an error line left out of that count;
    UTF-8's converter refusing or reading otherwise a character, said
    nowhere."""

    for name in [n for n in vars(ConverterCompared) if n.startswith('test_')]:
        locals()[name] = None
    del name

    @staticmethod
    def back(codeset, opens=True, **answers):
        """An edit that gives codeset's back table on a machine whose
        converter opens (or not) the answers ({'U+0041': None, ...}) over
        what back_values gives: a refusal of the nine is a character the
        table does not hold (back_reference)."""
        table = dict(m.back_unpacked(back_values(codeset, opens), codeset))
        for k, v in answers.items():
            if v is None and codeset != 'UTF-8':
                table.pop(int(k[2:], 16))
            else:
                table[int(k[2:], 16)] = v

        def edit(d):
            d['back'][codeset][3] = back_table(table)
        return edit

    @staticmethod
    def first_read(codeset):
        """The first character past ASCII converter_values reads."""
        values = m.unpack_order(zlib.decompress(base64.b64decode(converter_values(codeset))))
        return next(v for v in values if v >= 0x80)

    def both8(self, old_edits=(), new_edits=(), **k):
        return self.pair(measured_use(8, **EUC_TW_USE), measured_use(8, **EUC_TW_USE),
                         old_edits=old_edits, new_edits=new_edits, **k)

    def back_rows(self, rows):
        """{(names, encoding): [(character, how, before, now, measured)]}."""
        return {rows.back_groups[label]: [
            (r['chars'][0], r['how'], r['before'], r['now'], r['measured'])
            for r in rows[(label,)]] for label in rows.back_groups}

    def test_one_machine_does_not_turn_a_character_back(self):
        """EUC-JP on the old machine refuses A back: A, whose upper() is
        itself, and a, whose upper() is A, are named, neither through
        lower(), each a row; the two locales leave the count of those that
        convert."""
        report, summary, rows = self.outputs(*self.both8(
            old_edits=[self.back('EUC-JP', **{'U+0041': None})]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        said = {'EUC-JP': {'old': 2}, 'lower': 0}
        self.assertIn({'back error': {'ja_JP': said, 'ja_JP.eucjp': said}}, use)
        # A refused on one machine and turned back on the other is that
        # machine's error, never also a change.
        self.assertFalse([d for d in use if 'back change' in d])
        self.assertEqual(use[-1]['converts'], 5)
        self.assertEqual(self.back_rows(rows), {(('ja_JP', 'ja_JP.eucjp'), 'EUC-JP'): [
            (0x41, "upper(): U+0041 (itself), which glibc's converter of EUC-JP does "
                   'not turn back into bytes',
             'not turned back', 'turned back', 'on both machines'),
            (0x61, "upper(): U+0041, which glibc's converter of EUC-JP does not "
                   'turn back into bytes',
             'not turned back', 'turned back', 'on both machines')]})

    def test_both_machines_alike(self):
        """Both machines refuse a back: a itself and A, whose lower() is
        a, on both, both through lower()."""
        refuse = self.back('EUC-JP', **{'U+0061': None})
        report, summary, rows = self.outputs(*self.both8(old_edits=[refuse],
                                                         new_edits=[refuse]))
        said = {'EUC-JP': {'both': 2}, 'lower': 2}
        self.assertIn(('use', {'back error': {'ja_JP': said, 'ja_JP.eucjp': said}}),
                      report['checks'])
        self.assertEqual({(r[2], r[3]) for g in self.back_rows(rows).values() for r in g},
                         {('not turned back', 'not turned back')})

    def test_each_machine_its_own(self):
        """The old machine refuses A (so A and a, through upper() alone),
        the new one B (through both): two sets, each counted on its
        machine, and through lower() each on its own."""
        report, summary, rows = self.outputs(*self.both8(
            old_edits=[self.back('EUC-JP', **{'U+0041': None})],
            new_edits=[self.back('EUC-JP', **{'U+0042': None})]))
        said = {'EUC-JP': {'old': 2, 'new': 1, 'mixed': True},
                'lower': {'old': 0, 'new': 1}}
        self.assertIn(('use', {'back error': {'ja_JP': said, 'ja_JP.eucjp': said}}),
                      report['checks'])
        (got,) = self.back_rows(rows).values()
        self.assertEqual([(c, b, a) for c, _, b, a, _ in got], [
            (0x41, 'not turned back', 'turned back'),
            (0x42, 'turned back', 'not turned back'),
            (0x61, 'not turned back', 'turned back')])

    def test_beside_an_error_of_the_other_machine(self):
        """The new machine cannot convert EUC-JP at all (an error line);
        what the old one does not turn back is named all the same, and the
        new machine does not read the character."""
        c = self.first_read('EUC-JP')
        report, summary, rows = self.outputs(*self.pair(
            measured_use(8, **EUC_TW_USE), measured_use(9, **EUC_TW_USE),
            old_edits=[self.back('EUC-JP', **{f'U+{c:04X}': None})]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn('ja_JP', use[0]['changed'])
        said = {'EUC-JP': {'old': 1}, 'lower': 1}
        self.assertIn({'back error': {'ja_JP': said, 'ja_JP.eucjp': said}}, use)
        (got,) = self.back_rows(rows).values()
        self.assertEqual([(x, b, a, w) for x, _, b, a, w in got],
                         [(c, 'not turned back', 'not read', 'on both machines')])

    def test_turned_back_otherwise(self):
        """The new machine turns A back into other bytes: a CHANGE, a row
        with both machines' bytes, never counted as converting."""
        report, summary, rows = self.outputs(*self.both8(
            new_edits=[self.back('EUC-JP', **{'U+0041': b'\xa3\xc1'})]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn({'back change': {'ja_JP': ('EUC-JP', 1),
                                       'ja_JP.eucjp': ('EUC-JP', 1)}}, use)
        self.assertEqual(use[-1]['converts'], 5)
        self.assertEqual([(r['chars'], r['how']) for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower back changed'],
                         [([0x41], 'bytes 41 -> A3 C1')])

    def test_a_file_before_the_back_part_on_each_side(self):
        """Counted on the machine whose file it is, each locale a row; de_DE.utf8
        is also not known past one character of UTF-8 there."""
        five = lambda base, tag: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE), form='locale_order/5'))
        six = lambda base, tag: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE)))
        counts = {'ARMSCII-8': 1, 'EUC-JP': 2, 'EUC-KR': 2, 'EUC-TW': 1,
                  'GB2312': 1, 'TIS-620': 1, 'UTF-8': 1}
        for case, files, sides, where in (
                ('old', (five(RHEL8, 'o5'), six(RHEL9, 'n6')), ['OLD'], 'the old machine'),
                ('new', (six(RHEL8, 'o6'), five(RHEL9, 'n5')), ['NEW'], 'the new machine'),
                ('both', (five(RHEL8, 'o5'), five(RHEL9, 'n5')), ['OLD', 'NEW'],
                 'both machines')):
            with self.subTest(case=case):
                report, summary, rows = self.outputs(*files)
                use = [d for kind, d in report['checks'] if kind == 'use']
                self.assertIn({'back earlier': sides, 'counts': counts, 'utf8': 1}, use)
                self.assertEqual(use[-1]['converts'], 0)
                self.assertEqual([r['why'] for r in rows[('de_DE.utf8',)]
                                  if r['finding'] == 'upper/lower back not checked'],
                                 ['whether glibc reads all the text as it is, and '
                                  'whether the result converts back to bytes: not '
                                  f'known on {where}'])
                self.assertEqual([r['why'] for r in rows[('ja_JP',)]
                                  if r['finding'] == 'upper/lower back not checked'],
                                 ['whether the result converts back to bytes: not '
                                  f'known on {where}'])

    def test_a_file_before_the_back_part_beside_an_error(self):
        """The old machine cannot convert EUC-JP, the new one can, both of
        the fifth format: the EUC locales, in the line on what the new
        machine converts and the old one could not, are counted on the new
        machine, which did not measure the conversion back (review of the
        preview, F1)."""
        five = lambda base, tag, n: self.made(base, tag, with_parts(
            use=measured_use(n, **EUC_TW_USE), form='locale_order/5'))
        report, summary, rows = self.outputs(five(RHEL8, 'o5', 9), five(RHEL9, 'n5', 8))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn('ja_JP', use[0]['back'])
        early, = [d for d in use if 'back earlier' in d]
        self.assertEqual({k: early['counts'][k] for k in ('EUC-JP', 'EUC-KR', 'GB2312')},
                         {'EUC-JP': 2, 'EUC-KR': 2, 'GB2312': 1})
        # On the new machine alone: the old one converts nothing past ASCII.
        self.assertEqual([r['why'] for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower back not checked'],
                         ['whether the result converts back to bytes: not known '
                          'on the new machine'])

    def test_a_back_part_not_measured(self):
        """The old machine's conversion back to EUC-JP could not be
        measured: beside the new machine's error and without one, the
        locales are NOT checked with why, on the old machine."""
        entry = lambda d: d['back'].update({'EUC-JP': [
            'not checked', ['opens', None], 'ja_JP', 'measuring it failed: x', None]})
        why = ("whether their result converts back to the database's bytes is "
               "not known: glibc's conversion back to EUC-JP could not be "
               'measured: measuring it failed: x (on the old machine)')
        for case, new_use in (('beside an error', measured_use(9, **EUC_TW_USE)),
                              ('alone', measured_use(8, **EUC_TW_USE))):
            with self.subTest(case=case):
                use = self.use(*self.pair(measured_use(8, **EUC_TW_USE), new_use,
                                          old_edits=[entry]))
                self.assertIn({'unknown': {'ja_JP': why, 'ja_JP.eucjp': why}}, use)
                if case == 'alone':
                    self.assertEqual(use[-1]['converts'], 5)

    def test_one_database_each_kind(self):
        """ja_JP is of EUC-JP on the old machine, of the sixth format, and
        of EUC-KR on the new one, of the fifth: a database of EUC-KR made on
        the new machine converts there, which did not measure the
        conversion back, so ja_JP is counted on the new machine, whatever
        a database carried from the old one does there (review of the
        preview, N3)."""
        def kr(d):
            d['postgres_use']['ja_JP']['codeset'] = 'EUC-KR'
            d['locales']['ja_JP']['codeset'] = 'EUC-KR'
            refirst(d)
        report, summary, rows = self.outputs(
            self.made(RHEL8, 'o6', with_parts(use=measured_use(8, **EUC_TW_USE))),
            self.made(RHEL9, 'n5', with_parts(use=measured_use(8, **EUC_TW_USE),
                                              form='locale_order/5'), kr))
        use = [d for kind, d in report['checks'] if kind == 'use']
        early, = [d for d in use if 'back earlier' in d]
        self.assertEqual(early['back earlier'], ['NEW'])
        self.assertEqual(early['counts']['EUC-KR'], 3)
        self.assertEqual([r['why'] for r in rows[('ja_JP',)]
                          if r['finding'] == 'upper/lower back not checked'],
                         ['whether the result converts back to bytes: not known '
                          'on the new machine'])

    def test_the_rest_not_known_of_a_locale_in_no_line(self):
        """16.24: ja_JP.eucjp refused by glibc on the old machine, or only
        on the new one, whose file of the fourth format tried one character
        of EUC-JP that converted: named in the line on what is not known of
        part of the text, as ja_JP is."""
        old_use = measured_use(8, **EUC_TW_USE)
        old_use['ja_JP.eucjp'] = refused_use('ja_JP.eucjp', 8, ctype='accepted')

        def drop(d):
            d['locales'].pop('ja_JP.eucjp')
            d['locales_listed'] -= 1
        olds = {'refused on the old machine': self.made(
                    RHEL8, 'o-refused', with_parts(use=old_use),
                    selected_not('ja_JP.eucjp')),
                'only on the new machine': self.made(
                    RHEL8, 'o-absent', drop, with_parts(
                        use={k: v for k, v in measured_use(8, **EUC_TW_USE).items()
                             if k != 'ja_JP.eucjp'}))}
        new = self.made(RHEL9, 'n4', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                form='locale_order/4'))
        for case, old in olds.items():
            with self.subTest(case=case):
                untold, = [d['untold'] for d in self.use(old, new) if 'untold' in d]
                self.assertEqual(untold['the new machine']['ja_JP.eucjp'], 'EUC-JP')
                self.assertEqual(untold['the new machine']['ja_JP'], 'EUC-JP')

    def test_utf8_refuses_a_character(self):
        """The new machine's converter of UTF-8 refuses U+00E9: de_DE.utf8 gives
        an error on part of the text there, its row converts on the old
        machine and is an error on the new one."""
        def refuse(d):
            d['utf8_forward'][3] = forward_table({0xE9: 0})
        report, summary, rows = self.outputs(*self.both8(new_edits=[refuse]))
        use = [d for kind, d in report['checks'] if kind == 'use']
        self.assertIn({'partly': {'de_DE.utf8': {'UTF-8': {'new': 1}}}}, use)
        self.assertEqual(use[-1]['converts'], 6)
        (label, (names, cs)), = rows.use_groups.items()
        self.assertEqual((names, cs), (('de_DE.utf8',), 'UTF-8'))
        self.assertEqual([(r['bytes'], r['before'], r['now'], r['measured'])
                          for r in rows[(label,)]],
                         [('C3 A9', 'converts', 'error', 'on both machines')])

    def test_utf8_reads_a_character_otherwise(self):
        """The new machine's converter of UTF-8 reads U+00E9 as U+00EA: a CHANGE in a
        UTF8 database, a row with both characters; read so on both, none."""
        def otherwise(d):
            d['utf8_forward'][3] = forward_table({0xE9: 0xEA})
        found = [d for kind, d in self.checks(*self.both8(new_edits=[otherwise]))
                 if kind == 'ctype' and 'utf8 converted' in d]
        self.assertEqual(found, [{'utf8 converted': {'C3 A9': (0xE9, 0xEA)}, 'count': 1}])
        found = [d for kind, d in self.checks(*self.both8(
            old_edits=[otherwise], new_edits=[otherwise]))
                 if kind == 'ctype' and 'utf8 converted' in d]
        self.assertEqual(found, [])

    def test_utf8_refused_on_one_machine_is_no_change(self):
        """Both machines measured UTF-8's converter, and one refuses U+00E9,
        which the other reads as itself: an error on part of the text on
        that machine, never also a CHANGE to U+0000; on each side (final
        read, N4)."""
        def refuse(d):
            d['utf8_forward'][3] = forward_table({0xE9: 0})
        six = lambda base, tag, *e: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE)), *e)
        for side in ('old', 'new'):
            with self.subTest(side=side):
                checks = self.checks(
                    six(RHEL8, f'o-{side}', *([refuse] if side == 'old' else [])),
                    six(RHEL9, f'n-{side}', *([refuse] if side == 'new' else [])))
                self.assertEqual([d for kind, d in checks
                                  if kind == 'ctype' and 'utf8 converted' in d], [])
                self.assertIn(('use', {'partly': {'de_DE.utf8': {'UTF-8': {side: 1}}}}),
                              checks)


    def test_the_rest_not_known_beside_a_machine_that_cannot_say(self):
        """16.24 whatever the other machine says (review round 1, A1 and
        A3): the old file is of this format and could not measure EUC-JP's
        conversion back, or its converter; the new one, of the fourth,
        tried one character of EUC-JP, which converted: ja_JP is named on
        the new machine in the line on what is not known of part of the
        text, beside whatever the old machine's reason says."""
        for case, entry in (
                ('back not measured', lambda d: d['back'].update({'EUC-JP': [
                    'not checked', ['opens', None], 'ja_JP', 'measuring it failed: x',
                    None]})),
                ('converter not measured', lambda d: d['converter'].update({'EUC-JP': [
                    'not checked', ['opens', None], 'ja_JP', 'measuring it failed: x',
                    None]}))):
            with self.subTest(case=case):
                old = self.made(RHEL8, 'o6', with_parts(use=measured_use(8, **EUC_TW_USE)),
                                entry)
                new = self.made(RHEL9, 'n4', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                        form='locale_order/4'))
                untold, = [d['untold'] for d in self.use(old, new) if 'untold' in d]
                self.assertEqual(untold['the new machine']['ja_JP'], 'EUC-JP')

    def test_a_row_measured_on_one_machine(self):
        """The new file is of the fifth format: the rows of what the old
        machine does not turn back say the new one is not measured, and
        that they were measured on the old machine (review round 1, A2)."""
        c = self.first_read('EUC-JP')
        report, summary, rows = self.outputs(
            self.made(RHEL8, 'o6', with_parts(use=measured_use(8, **EUC_TW_USE)),
                      self.back('EUC-JP', **{f'U+{c:04X}': None})),
            self.made(RHEL9, 'n5', with_parts(use=measured_use(8, **EUC_TW_USE),
                                              form='locale_order/5')))
        (got,) = self.back_rows(rows).values()
        self.assertEqual([(x, b, a, w) for x, _, b, a, w in got],
                         [(c, 'not turned back', 'not measured', 'on the old machine')])

    def test_rules_not_measured_beside_another_encoding(self):
        """hy_AM.armscii8, of an encoding PostgreSQL does not know: the new
        machine could not measure its character rules, so what comes back
        there is not known, and the line on upper() says so; the line on a
        database of another encoding does not (review round 1, A4)."""
        def unread(d):
            d['ctype']['locales']['hy_AM.armscii8'] = ['not checked', m.CTYPE_UNREAD]
        use = self.use(*self.both8(new_edits=[unread]))
        unknown, = [d['unknown'] for d in use if 'unknown' in d]
        self.assertIn("whether their result converts back to the database's bytes is "
                      'not known', unknown['hy_AM.armscii8'])
        self.assertIn('(on the new machine)', unknown['hy_AM.armscii8'])

    def test_a_row_of_a_machine_under_another_encoding(self):
        """ja_JP is of EUC-JP on the old machine, which refuses A back, and
        of UTF-8 on the new one: the new machine was never measured for an
        EUC-JP database, so its column says not measured, never "turned
        back" from its UTF-8 converter (review round 1, A5)."""
        def utf8(d):
            d['postgres_use']['ja_JP'] = use_of('converts', 'UTF-8', (0x80, 'c280'))
            d['locales']['ja_JP']['codeset'] = 'UTF-8'
            refirst(d)
        report, summary, rows = self.outputs(*self.both8(
            old_edits=[self.back('EUC-JP', **{'U+0041': None})], new_edits=[utf8]))
        got = self.back_rows(rows)
        self.assertEqual({(names, cs): [(x, b, a) for x, _, b, a, _ in r]
                          for (names, cs), r in got.items() if 'ja_JP' in names},
                         {(('ja_JP',), 'EUC-JP'): [
                             (0x41, 'not turned back', 'not measured'),
                             (0x61, 'not turned back', 'not measured')]})


    def test_the_rest_not_known_of_a_database_made_on_the_new_machine(self):
        """ja_JP is of EUC-JP on the old machine and of EUC-KR on the new
        one, whose file of the fourth format tried one character of EUC-KR,
        which converted: a database of EUC-KR made there converts, so ja_JP
        is named on the new machine in the line on what is not known of
        part of the text, whatever a database carried from the old machine
        does there (review round 1, A3)."""
        def kr(d):
            d['postgres_use']['ja_JP']['codeset'] = 'EUC-KR'
            d['postgres_use']['ja_JP']['text'] = ['converts', [0x3000, 'a1a1']]
            d['locales']['ja_JP']['codeset'] = 'EUC-KR'
        old = self.made(RHEL8, 'o6', with_parts(use=measured_use(8, **EUC_TW_USE)))
        new = self.made(RHEL9, 'n4', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                form='locale_order/4'), kr)
        untold, = [d['untold'] for d in self.use(old, new) if 'untold' in d]
        self.assertEqual(untold['the new machine']['ja_JP'], 'EUC-KR')


    def test_the_rest_not_said_where_the_database_does_not_open(self):
        """ja_JP.eucjp is refused by glibc on the new machine; the old file,
        of the fourth format, tried one character of EUC-JP there: a
        database that uses it does not open after the upgrade, which its
        refusal says, and nothing more is said of the old machine."""
        new_use = measured_use(8, **EUC_TW_USE)
        new_use['ja_JP.eucjp'] = refused_use('ja_JP.eucjp', 8, ctype='accepted')
        old = self.made(RHEL8, 'o4', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                form='locale_order/4'))
        new = self.made(RHEL9, 'n6', with_parts(use=new_use), selected_not('ja_JP.eucjp'))
        untold, = [d['untold'] for d in self.use(old, new) if 'untold' in d]
        self.assertIn('ja_JP', untold['the old machine'])
        self.assertNotIn('ja_JP.eucjp', {n for g in untold.values() for n in g})

    def test_a_file_before_the_back_part_beside_a_locale_the_new_machine_refuses(self):
        """The old file is of the fifth format; glibc refuses ja_JP.eucjp and
        de_DE.utf8 on the new machine, where no database that uses them
        opens: neither is counted in the line on a file before the back
        part, nor gets a row of it, as untold leaves them out (final read,
        3)."""
        new_use = measured_use(8, **EUC_TW_USE)
        for name in ('ja_JP.eucjp', 'de_DE.utf8'):
            new_use[name] = refused_use(name, 8, ctype='accepted')
        old = self.made(RHEL8, 'o5', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                form='locale_order/5'))
        new = self.made(RHEL9, 'n6', with_parts(use=new_use),
                        selected_not('ja_JP.eucjp', 'de_DE.utf8'))
        report, summary, rows = self.outputs(old, new)
        early, = [d for kind, d in report['checks'] if kind == 'use' and 'back earlier' in d]
        self.assertEqual(early, {'back earlier': ['OLD'], 'counts': {
            'ARMSCII-8': 1, 'EUC-JP': 1, 'EUC-KR': 2, 'EUC-TW': 1, 'GB2312': 1,
            'TIS-620': 1}})
        self.assertEqual([r for n in ('ja_JP.eucjp', 'de_DE.utf8') for r in rows.get((n,), [])
                          if r['finding'] == 'upper/lower back not checked'], [])


    @staticmethod
    def utf8_unmeasured(d):
        d['utf8_forward'] = ['not checked', ['opens', None], 'de_DE.utf8',
                             'measuring it failed: x', None]

    def test_utf8_not_measured_on_one_machine(self):
        """A machine could not measure UTF-8's converter: de_DE.utf8 is NOT
        checked there, with why and that machine, as a locale of the nine
        whose converter could not be measured; on each side (review round
        2, A1; final check, X1)."""
        for side in ('old', 'new'):
            with self.subTest(side=side):
                use = self.use(*self.both8(**{f'{side}_edits': [self.utf8_unmeasured]}))
                unknown, = [d['unknown'] for d in use if 'unknown' in d]
                self.assertEqual(unknown['de_DE.utf8'],
                                 "glibc's converter of UTF-8 could not be measured: "
                                 f'measuring it failed: x (on the {side} machine)')

    def one_side(self, old, new):
        """(the line on what one machine's UTF-8 converter reads otherwise,
        its rows as (bytes, how, measured))."""
        report, summary, rows = self.outputs(old, new)
        found = [d for kind, d in report['checks'] if kind == 'ctype' and 'utf8 read' in d]
        labels = [lb for lb, v in rows.ctype_groups.items() if v[1] == 'byte sequence']
        return found, [(r['chars'], r['how'], r['measured'])
                       for lb in labels for r in rows[(lb,)]]

    def test_utf8_read_otherwise_on_one_machine_only(self):
        """One machine's file did not measure UTF-8's converter (a file of
        the fifth format, or one that could not); the other machine's reads
        U+00E9 as U+00EA: said as that machine's, the other not known, a
        row measured on that machine; on each side (review round 2, A2;
        final check, X2 and X3)."""
        def otherwise(d):
            d['utf8_forward'][3] = forward_table({0xE9: 0xEA})
        five = lambda base, tag: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE), form='locale_order/5'))
        six = lambda base, tag, *e: self.made(base, tag, with_parts(
            use=measured_use(8, **EUC_TW_USE)), *e)
        for case, files, side, why in (
                ('new measured', (five(RHEL8, 'o5'), six(RHEL9, 'n6', otherwise)),
                 'new', 'the OLD file did not measure this'),
                ('old measured', (six(RHEL8, 'o6', otherwise), five(RHEL9, 'n5')),
                 'old', 'the NEW file did not measure this'),
                ('old not checked', (six(RHEL8, 'o6-unmeasured', self.utf8_unmeasured),
                                     six(RHEL9, 'n6-otherwise', otherwise)),
                 'new', "the old machine could not measure this (glibc's converter "
                        'of UTF-8 could not be measured: measuring it failed: x)')):
            with self.subTest(case=case):
                found, rows = self.one_side(*files)
                self.assertEqual(found, [{'utf8 read': {'C3 A9': (0xE9, 0xEA)},
                                          'side': side, 'count': 1, 'why': why}])
                self.assertEqual(rows, [('bytes C3 A9', 'U+00E9 -> U+00EA',
                                         f'on the {side} machine')])

    def test_utf8_refused_on_one_machine_only(self):
        """The new machine refuses U+00E9 and the old file is of the fifth
        format: an error on part of the text on the new machine, not known
        on the old one, which tried one character of UTF-8, and never "read
        as another character" (final check, X7; review of the alternative,
        M11)."""
        def refuse(d):
            d['utf8_forward'][3] = forward_table({0xE9: 0})
        old = self.made(RHEL8, 'o5', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                form='locale_order/5'))
        new = self.made(RHEL9, 'n6', with_parts(use=measured_use(8, **EUC_TW_USE)),
                        refuse)
        self.assertEqual(self.one_side(old, new), ([], []))
        self.assertIn({'partly': {'de_DE.utf8': {'UTF-8': {'new': 1},
                                                 'not known': ['old']}}},
                      self.use(old, new))

    def test_utf8_read_otherwise_beside_a_file_without_rules(self):
        """The old file is of the third format, which measured no character
        rules, or of the second, which did not say how PostgreSQL uses a
        locale either: the new machine's UTF-8 converter reading U+00E9 as
        U+00EA is said all the same, and where its rows are (final check,
        A-1; last check, 1 and 2)."""
        def otherwise(d):
            d['utf8_forward'][3] = forward_table({0xE9: 0xEA})
        new = self.made(RHEL9, 'n6', with_parts(use=measured_use(8, **EUC_TW_USE)),
                        otherwise)
        for form in ('locale_order/3', 'locale_order/2'):
            with self.subTest(form=form):
                old = self.made(RHEL8, f'o{form[-1]}', with_parts(
                    use=measured_use(8, **EUC_TW_USE), form=form))
                found, rows = self.one_side(old, new)
                self.assertEqual(found, [{'utf8 read': {'C3 A9': (0xE9, 0xEA)},
                                          'side': 'new', 'count': 1,
                                          'why': 'the OLD file did not measure this'}])
                self.assertEqual(rows, [('bytes C3 A9', 'U+00E9 -> U+00EA',
                                         'on the new machine')])
                self.assertIn(('ctype', {'where': 'byte sequence'}), self.checks(old, new))

    def test_the_back_line_beside_a_file_too_old_for_the_rest(self):
        """The old file is of the second format, whose upper() lines say
        only NOT checked; the new one, of the fifth, did not measure the
        conversion back either: the line on that is printed all the same
        (review round 2, A3)."""
        old = self.made(RHEL8, 'o2', with_parts(form='locale_order/2'))
        new = self.made(RHEL9, 'n5', with_parts(use=measured_use(8, **EUC_TW_USE),
                                                form='locale_order/5'))
        use = self.use(old, new)
        self.assertTrue([d for d in use if 'earlier' in d])
        early, = [d for d in use if 'back earlier' in d]
        self.assertEqual(early['back earlier'], ['NEW'])

    def test_a_row_of_another_encoding_on_a_utf8_machine(self):
        """ja_JP is of EUC-JP on the old machine, whose converter refuses
        AD A1, and of UTF-8 on the new one: AD A1 is no UTF-8 a database
        holds, so the new machine's column is not measured, never "converts"
        (review round 2, N1)."""
        def utf8(d):
            d['postgres_use']['ja_JP'] = use_of('converts', 'UTF-8', (0x80, 'c280'))
            d['locales']['ja_JP']['codeset'] = 'UTF-8'
            refirst(d)
        report, summary, rows = self.outputs(*self.both8(
            old_converter=self.entry('EUC-JP', 'ja_JP', (b'\xad\xa1',)),
            new_edits=[utf8]))
        got = {names: [(r['bytes'], r['before'], r['now']) for r in rows[(label,)]]
               for label, (names, cs) in rows.use_groups.items()}
        self.assertEqual(got[('ja_JP',)], [('AD A1', 'error', 'not measured')])


# --- the model against the table, every combination -------------------------

# One locale's name, and each encoding of a row of the approved table, as
# the test spells them (backlog 16.19, handoff paso4b-pr4/pr2/plan-aprobado.md):
# UTF-8; an EUC encoding; one PostgreSQL does not know, of one byte (one of
# the nine converters); one of one byte it knows; one it allows only on the
# client; US-ASCII; and none read.
ORACLE_NAME = 'xx_XX'
ORACLE_ENCODINGS = ('UTF-8', 'EUC-JP', 'TIS-620', 'ISO-8859-1', 'BIG5', 'US-ASCII', None)
# What a file of the format before the converter part can say of each, as
# postgres_use's test of one character; a character each, as it writes one.
ORACLE_EVIDENCE = {'UTF-8': ('converts', 'fails', 'not checked'),
                   'EUC-JP': ('converts', 'fails', 'not checked'),
                   'TIS-620': ('one byte', 'not checked'),
                   'ISO-8859-1': ('one byte', 'not checked'),
                   'BIG5': ('converts', 'fails', 'not checked'),
                   'US-ASCII': ('one byte',), None: ('not checked',)}
ORACLE_CHAR = {'UTF-8': (0x80, 'c280'), 'EUC-JP': (0xFF61, '8ea1'),
               'BIG5': (0x3000, 'a140')}
# And what the converter part says of one of the nine.
ORACLE_CONVERTER = ('converts', 'fails', 'not checked', 'refuses')
# An old machine that does not have the locale.
ORACLE_ABSENT = ('absent', None, None, None)
# The sequence a converter that refuses one refuses.
ORACLE_REFUSED = {'EUC-JP': b'\xad\xa1', 'TIS-620': b'\xdb'}


def oracle_specs():
    """Each machine the oracle compares: (format, category, encoding, what
    was measured), the category 'refused' (glibc refused LC_COLLATE),
    'unknown' (Python could not pass the name) or 'accepted'."""
    out = [('locale_order/2', 'accepted', enc, None) for enc in ORACLE_ENCODINGS]
    for form in ('locale_order/3', m.FORMAT):
        out += [(form, 'refused', 'UTF-8', 'converts'),
                (form, 'unknown', None, 'not checked')]
        for enc in ORACLE_ENCODINGS:
            nine = enc in m.CONVERTER_CODESETS and 'converter' in m.PARTS[form]
            for ev in ORACLE_CONVERTER if nine else ORACLE_EVIDENCE[enc]:
                out.append((form, 'accepted', enc, ev))
        if 'utf8_forward' in m.PARTS[form]:
            # UTF-8's converter measured on every character, which could
            # not be measured: not known, whatever the test of one
            # character said.
            out.append((form, 'accepted', 'UTF-8', 'unmeasured'))
    return out


def oracle_machine(spec):
    """A machine's measurement of ORACLE_NAME as TextModel reads it, built
    from spec, with the parts its format holds."""
    form, cat, enc, ev = spec
    held = m.PARTS[form]
    d = {'held': held, 'locales': {ORACLE_NAME: (
        {'codeset': enc} if enc and cat == 'accepted' else {})}}
    if 'postgres_use' not in held:
        return d
    converter = enc in m.CONVERTER_CODESETS and 'converter' in held
    if cat == 'unknown':
        r = use_of('not checked', None, m.NO_CTYPE, collate='unknown', ctype='unknown')
    elif converter:
        # Its text is the converter part's, set below.
        r = use_of('one byte', enc)
    elif enc is None:
        r = use_of('not checked', None, m.NO_CODESET)
    elif ev == 'not checked':
        r = use_of('not checked', enc, 'checking failed: x')
    elif ev == 'one byte':
        r = use_of('one byte', enc)
    elif ev == 'unmeasured':
        r = use_of('converts', enc, ORACLE_CHAR.get(enc))
    else:
        r = use_of(ev, enc, ORACLE_CHAR.get(enc))
    if cat == 'refused':
        r['collate'] = ['refused', 'unsupported locale setting']
    d['postgres_use'] = {ORACLE_NAME: r}
    if 'ctype' in held:
        d['ctype'] = ctype_of(d['postgres_use'])
    if 'converter' in held:
        part = {cs: ['no locale', ['opens', None], None, None, None]
                for cs in m.CONVERTER_CODESETS}
        if converter and cat != 'unknown':
            r['text'] = ['converter', None]
            if ev == 'not checked':
                part[enc] = ['not checked', ['opens', None], ORACLE_NAME,
                             'measuring it failed: x', None]
            elif ev == 'fails':
                part[enc] = ['measured', ['fails', 'x'], ORACLE_NAME,
                             ascii_only(enc), sequences_of(enc)]
            else:
                values = m.unpack_order(zlib.decompress(base64.b64decode(
                    converter_values(enc))))
                if ev == 'refuses':
                    values[m.accepted_sequences(enc).index(ORACLE_REFUSED[enc])] = 0
                part[enc] = ['measured', ['opens', None], ORACLE_NAME,
                             m.pack_order(values), sequences_of(enc)]
        d['converter'] = part
        d['utf8_forward'], d['back'] = round_trip_of_use(d['postgres_use'], part)
        if ev == 'unmeasured':
            d['utf8_forward'] = ['not checked', ['opens', None], ORACLE_NAME,
                                 'measuring it failed: x', None]
    return d


def oracle_answer(old, new):
    """What the approved table says of ORACLE_NAME on the pair of machines
    old and new (specs): (line, the machines whose error the line names,
    the machines whose converter refuses part of the text, the machines
    whose file tried one character of one of the nine). Written from the
    table, not from the code: which databases a locale serves; what each
    machine does with each, leg by leg; the locale's line is that of its
    worst database, an error, then what is not known, then a database of
    any encoding, then what works. An old machine ORACLE_ABSENT does not
    have the locale: only the databases made on the new one exist, and the
    locale is named only where they give an error, are not known, or are of
    any encoding, and not at all where the new machine refuses it or its
    file says nothing of how PostgreSQL uses it. A locale that converts,
    but on a machine whose file is of a format before the back leg, is
    'back unknown'."""
    if old == ORACLE_ABSENT and (
            'postgres_use' not in m.PARTS[new[0]] or new[1] == 'refused'):
        return None, set(), set(), set()

    def view(spec):
        if spec == ORACLE_ABSENT:
            return 'absent', None, None
        form, cat, enc, ev = spec
        if 'postgres_use' not in m.PARTS[form]:
            return 'unread', enc if cat == 'accepted' else None, None
        if cat == 'refused':
            return 'refused', None, None
        if cat == 'unknown':
            return 'unread', None, None
        if enc is None:
            return 'accepted', None, 'not checked'
        # What the whole measurement could not say outweighs one character.
        return 'accepted', enc, 'not checked' if ev == 'unmeasured' else ev

    def databases(spec):
        state, enc, _ = view(spec)
        if state in ('refused', 'absent'):
            return []
        if enc is None:
            return ['unread']
        return {'US-ASCII': ['ascii'], 'TIS-620': ['any'], 'BIG5': []}.get(enc, [enc])

    def forward(spec, db):
        state, enc, ev = view(spec)
        if state == 'refused':
            return 'closed'
        if db == 'ISO-8859-1':
            return 'one byte'
        if state == 'unread' or db == 'ascii':
            return 'unknown'
        if db == enc:
            return {'converts': 'works', 'refuses': 'works', 'one byte': 'works',
                    'fails': 'error', 'not checked': 'unknown'}[ev]
        if ev == 'fails':
            return 'error'
        if ev == 'not checked':
            return 'unknown'
        return 'elsewhere' if db == 'any' else 'unknown'

    so, sn = view(old), view(new)
    if so[0] == sn[0] == 'refused':
        return None, set(), set(), set()
    refused = 'refused' in (so[0], sn[0])
    partial = {side for side, spec, v in (('old', old, so), ('new', new, sn))
               if v[0] == 'accepted' and v[1] == 'EUC-JP' and v[2] == 'refuses'}
    # A file before the part that measures an encoding's converter tried one
    # character of it: the converter part for the nine, utf8_forward for
    # UTF-8.
    unsaid = {side for side, spec, v in (('old', old, so), ('new', new, sn))
              if v[0] == 'accepted' and v[2] != 'fails' and (
                  (v[1] in m.CONVERTER_CODESETS and 'converter' not in m.PARTS[spec[0]])
                  or (v[1] == 'UTF-8' and 'utf8_forward' not in m.PARTS[spec[0]]))}
    absent = old == ORACLE_ABSENT
    carried = databases(old)
    dbs = carried + [db for db in databases(new) if db not in carried]
    if not dbs:
        return (None if refused or absent else 'left out'), set(), partial, unsaid
    found = [(forward(old, db) if db in carried else 'none', forward(new, db))
             for db in dbs]
    rank = {'error': 0, 'unknown': 1, 'elsewhere': 2}
    b, a = min(found, key=lambda f: min(rank.get(s, 3) for s in f))
    if 'error' in (b, a):
        return 'error', {s for s, x in (('old', b), ('new', a)) if x == 'error'}, \
            partial, unsaid
    if 'unknown' in (b, a) and a != 'closed':
        return 'unknown', set(), partial, unsaid
    if refused:
        return None, set(), partial, unsaid
    if 'elsewhere' in (b, a):
        return 'elsewhere', set(), partial, unsaid
    if absent:
        return None, set(), partial, unsaid
    if b in ('one byte', 'none') and a == 'one byte':
        return 'one byte', set(), partial, unsaid
    # The back leg: measured from the sixth format; a machine whose
    # database converts text there, of a format before, cannot say it.
    if any(x == 'works' and 'back' not in m.PARTS[spec[0]]
           for f in found for x, spec in zip(f, (old, new))):
        return 'back unknown', set(), partial, unsaid
    return 'converts', set(), partial, unsaid


class TextModelAgainstTheTable(unittest.TestCase):
    """The model against the approved table, every pair of machines of
    oracle_specs(): each format, each category, each row of the table and
    each thing a machine can say of it, on both sides. A rule of the model
    that disagrees with the table, in any one combination, is named."""

    def test_every_combination(self):
        specs = oracle_specs()
        machines = {spec: oracle_machine(spec) for spec in specs}
        machines[ORACLE_ABSENT] = {'held': (), 'locales': {}}
        wrong, count = [], 0
        for old, new in itertools.product(specs + [ORACLE_ABSENT], specs):
            if old == ORACLE_ABSENT:
                model = m.TextModel(machines[old], machines[new], [], [ORACLE_NAME])
            else:
                model = m.TextModel(machines[old], machines[new], [ORACLE_NAME])
            recs = model.use_records()
            # A locale only on the new machine that it refuses, or of a file
            # that says nothing of PostgreSQL's use, has no record at all.
            rec = recs[0] if recs else {'line': None}
            line = rec['line']
            sides = ({s for s, x in (('old', rec['b']), ('new', rec['a'])) if x == 'error'}
                     if line == 'error' else set())
            got = (line, sides, set(rec.get('partial', {})), rec.get('unsaid', set()))
            want = oracle_answer(old, new)
            count += 1
            if got != want:
                wrong.append((old, new, got, want))
        self.assertEqual(count, (len(specs) + 1) * len(specs))
        self.assertEqual(wrong, [], f'{len(wrong)} of {count} combinations differ, '
                                    f'the first: {wrong[:1]}')

    def test_each_line_and_side_is_reached(self):
        """The combinations reach every line the model writes, an error on
        each machine and on both, and a refusal of part of the text and a
        file of one character on each side: an oracle whose cases never
        reach one compares nothing there."""
        answers = [oracle_answer(o, n) for o, n in itertools.product(oracle_specs(), repeat=2)]
        self.assertEqual({a[0] for a in answers},
                         {None, 'left out', 'error', 'unknown', 'elsewhere',
                          'one byte', 'converts', 'back unknown'})
        # And a locale only on the new machine: an error there, not known,
        # of any encoding, or named nowhere else.
        absent = [oracle_answer(ORACLE_ABSENT, n) for n in oracle_specs()]
        self.assertEqual({(a[0], frozenset(a[1])) for a in absent},
                         {(None, frozenset()), ('error', frozenset({'new'})),
                          ('unknown', frozenset()), ('elsewhere', frozenset())})
        self.assertEqual({frozenset(a[1]) for a in answers if a[0] == 'error'},
                         {frozenset({'old'}), frozenset({'new'}),
                          frozenset({'old', 'new'})})
        self.assertEqual(set().union(*(a[2] for a in answers)), {'old', 'new'})
        self.assertEqual(set().union(*(a[3] for a in answers)), {'old', 'new'})


if __name__ == '__main__':
    unittest.main()
