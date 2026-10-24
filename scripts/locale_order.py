#!/usr/bin/env python3
"""
Measure how every locale on THIS machine sorts, or compare what two machines
measured.

The other checks in this repository read locale SOURCES. A source is not the
order: localedef computes weights when it builds a locale, so a byte-identical
file can sort differently on the next glibc (ko_KR, RHEL8 -> RHEL9), and a file
that changed can sort exactly as before (ber_DZ and kab_DZ swapped roles,
RHEL9 -> RHEL10). This asks the machine's own glibc instead.

Two things are measured for each locale that `locale -a` lists:

  1. Where every character sorts. Every character the locale's encoding can
     hold (in a UTF-8 locale every Unicode scalar value but NUL, 1,112,063)
     is put in the order glibc's strcoll gives its bytes in that encoding,
     the call PostgreSQL makes under a libc collation. Python's own
     locale.strcoll compares wide characters instead, and does not order
     them the same (see libc()). Characters strcoll calls equal stay in byte
     order, which is PostgreSQL's tie-break, and the tie is recorded too.
  2. How each two neighbours in that order are told apart: as different
     letters, by an accent, or by case. It is measured by comparing
     two-letter strings built from the pair and from a pair of characters
     that this locale tells apart at each of those levels, chosen per locale
     (choose_probes), never by reading glibc's internal keys. A character can
     keep its place and change how it is told apart from its neighbour, and
     then only strings of more than one letter sort differently.

Beside the order, the file holds each locale's decimals of money (frac_digits
of its LC_MONETARY, the one number there that changes what a stored money
value means); which PostgreSQL processes of the machine run another C library
than the one measured, because a glibc update replaced it on disk or because
they run another file, as in a container (root can see that, and so can the
user PostgreSQL runs as where /proc does not hide other users' processes;
otherwise the file says it was not checked); and how PostgreSQL can use each
locale: whether setlocale accepts it for LC_COLLATE and for LC_CTYPE, without
which a database that uses it does not open, and whether glibc converts a
character of its encoding to a wide character, without which upper(), lower()
and ILIKE stop with an error (postgres_use). A file written before these were
measured still compares, and the comparison says they were not checked there.
The file also holds what glibc's character rules (LC_CTYPE) answer for every
character and every byte, once for each distinct LC_CTYPE glibc loads
(ctype_tables), and the comparison reports the characters and bytes whose
rules change (ctype_texts). It holds too, for each encoding of
CONVERTER_CODESETS, what glibc's converter gives for every byte sequence
PostgreSQL can hand it under a locale of that encoding (converter_part),
which the comparison does not read yet. It also names what PostgreSQL
stores in EUC-JP, EUC-KR and TIS-620, converting from UTF-8, that no order
measured here places (POSTGRES_ONLY). C and POSIX, which glibc sorts in
byte order in every version, are the control: a machine where they measured
otherwise did not measure what this means to.

Each order and told-apart record is kept whole, compressed, once for all the
locales that share it, so that the comparison can name the characters that
moved without a second run on the machines.

Not measured: a rule that applies only to a particular combination of letters,
such as a contraction ("ch" sorted as one letter). Two machines that agree on
both can still sort some longer strings differently. Where a character counts
for nothing at some level (glibc ignores it there), how it is told apart from
its neighbour is read less reliably, so a change there may go unseen.

Usage:
  python3 locale_order.py > old.out          # on each machine
  python3 locale_order.py --compare old.out new.out

On RHEL8 the python3 command comes in a package of its own (python36) and may
be missing; /usr/libexec/platform-python, which dnf itself runs on, is always
there and is Python 3.6:
  /usr/libexec/platform-python locale_order.py > old.out

Measuring needs Python 3.6 or newer and the language packs of the locales to
be measured (on RHEL, glibc-all-langpacks or glibc-langpack-*). It needs no
PostgreSQL and no root, runs at low priority, and takes about 0.3 GB of
memory per process; to see PostgreSQL's processes it runs as root, or as their
user where /proc does not hide other users' processes. Comparing needs only
Python.
"""
import argparse
import array
import base64
import bisect
import codecs
import collections
import ctypes
import ctypes.util
import gzip
import hashlib
import io
import itertools
import operator
import json
import locale
import multiprocessing
import os
import platform
import re
import stat
import string
import subprocess
import sys
import tarfile
import textwrap
import time
import unicodedata
import zlib
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

FORMAT = 'locale_order/5'

# The parts a file of each format holds, as its 'parts' says. A file of an
# earlier format lacks the parts added since, and the comparison says each
# part it reads was not checked and why, rather than refuse a file that is
# whole (it does not read 'converter' yet); a file
# that lacks a part its format holds is refused. The corpus digest starts
# with the first format's name, not with FORMAT, because a file of that
# format measured the same strings and still compares.
PARTS = {'locale_order/1': ('order',),
         'locale_order/2': ('order', 'money', 'postgres_libc'),
         'locale_order/3': ('order', 'money', 'postgres_libc', 'postgres_use'),
         'locale_order/4': ('order', 'money', 'postgres_libc', 'postgres_use',
                            'ctype'),
         'locale_order/5': ('order', 'money', 'postgres_libc', 'postgres_use',
                            'ctype', 'converter')}
# The parts beside the order, each checked whole by load().
SIDE_PARTS = ('money', 'postgres_libc', 'postgres_use', 'ctype', 'converter')
CORPUS_VERSION = 'locale_order/1'

# The file --extract writes holds the measurement, the machine's locale sources
# and charmaps, and its glibc build. The sources and the charmaps are what
# glibc-locale-source installs on RHEL, and each is copied as found or not at
# all, with the reason. A file written before the charmaps were packed has no
# entry for them, and --unpack says so in the reason (13.7 PR D).
EXTRACT_FORMAT = 'locale_order_extract/1'
LOCALE_SOURCES = '/usr/share/i18n/locales'
CHARMAPS = '/usr/share/i18n/charmaps'

# What one more measuring process adds to the peak memory of the whole run:
# 255 to 280 MiB on glibc 2.34 (the cgroup's memory.peak was 396, 2356 and
# 4394 MiB with 1, 8 and 16 processes). A process's own resident size is
# larger, 385 MB, because it counts memory the processes share. The default
# number of processes is planned at this much each.
PROCESS_BYTES = 300 * 1024 * 1024

# Every Unicode scalar value but NUL, which neither a C string nor a
# PostgreSQL text value can hold. Ascending, which measured() relies on.
CODEPOINTS = [c for c in range(1, 0x110000) if not 0xD800 <= c <= 0xDFFF]

# Pairs that may tell two characters apart as different letters, by an accent
# and by case, tried in this order for each locale; choose_probes() keeps the
# first pair that behaves as a pair of that level there. A fixed pair does not
# work everywhere: in is_IS U+00E1 is a letter of its own, in da_DK "aa" reads
# as one letter, in fr_CA accents are compared from the end of the string. The
# list is where to look, not a ceiling: a level no pair measures is reported,
# not skipped. Built with chr() so that this file stays ASCII.
CANDIDATES = (
    (('a', 'b'), ('e', 'f'), ('o', 'p')),
    (('a', chr(0xE1)), ('a', chr(0xE0)), ('e', chr(0xE9)), ('e', chr(0xE8)),
     ('o', chr(0xF3)), ('u', chr(0xFC)),
     (chr(0x435), chr(0x451)),          # Cyrillic ie, io
     (chr(0x3B1), chr(0x3AC)),          # Greek alpha, alpha with tonos
     (chr(0x304B), chr(0x304C))),       # hiragana ka, ga
    (('a', 'A'), ('e', 'E'), ('b', 'B'),
     (chr(0x435), chr(0x415)),          # Cyrillic ie, IE
     (chr(0x3B1), chr(0x391)),          # Greek alpha, ALPHA
     (chr(0x3042), chr(0x30A2))),       # hiragana a, katakana a
)
ABSENT = 'absent'   # a level shown not to exist in a locale (derived_probe)

LIBC = None         # glibc, loaded only to measure
UTF8_BYTES = None   # CODEPOINTS in UTF-8; built once, before the workers fork
CORPORA = {}        # per encoding, what corpus_for() built, kept per process

# Characters or pairs named one by one under a changed locale. Past this
# many, characters are listed as runs of code points while the runs are no
# more than this, and only counted after that; pairs are only counted.
LIST_LIMIT = 20

# The levels of a told-apart record, as the report names them.
LEVEL_NAMES = ('letters', 'accents', 'case')


def die(msg):
    sys.stderr.write(f'locale_order.py: {msg}\n')
    sys.exit(2)


def corpus_digest():
    """What both machines must have measured for their results to compare.

    Taken from the data rather than from a description of it, so a copy of
    this script with a different corpus cannot pass for the same one.
    """
    h = hashlib.sha256(CORPUS_VERSION.encode())
    h.update(''.join(map(chr, CODEPOINTS)).encode('utf-8'))
    h.update('|'.join(','.join(p + q for p, q in level)
                      for level in CANDIDATES).encode('utf-8'))
    return h.hexdigest()


def sign(r):
    """strcoll's answer as 0, 1 or 2: before, equal, after."""
    return 0 if r < 0 else (2 if r > 0 else 1)


def pack(data):
    return base64.b64encode(zlib.compress(data, 9)).decode('ascii')


def pack_order(seq):
    """A sequence of code points, stored as the step from each to the next.

    Most of an order is long runs of neighbouring code points, so the steps
    are mostly 1 and compress to almost nothing: measured on glibc 2.34, the
    123 distinct orders of 869 locales took 1.6 MB this way and 291 MB stored
    as the code points themselves.
    """
    out = bytearray()
    prev = 0
    for c in seq:
        d = c - prev
        prev = c
        z = d * 2 if d >= 0 else -d * 2 - 1
        while z > 0x7F:
            out.append(z & 0x7F | 0x80)
            z >>= 7
        out.append(z)
    return pack(bytes(out))


def content_digest(data):
    """A digest of the whole file, taken on the machine and again on load.

    It covers every key but itself, the build and the glibc version
    included, so damage anywhere is caught, even in a part the comparison
    would not otherwise open, and a file edited to claim another build does
    not pass. The serialisation is spelled out so that the Python that
    measured and the one that compares write the same bytes.
    """
    sealed = {k: v for k, v in data.items() if k not in ('content', 'path')}
    text = json.dumps(sealed, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True)
    return hashlib.sha256(text.encode('ascii')).hexdigest()


# A one-byte step of pack_order, decoded.
ONE_BYTE_STEP = [z // 2 if not z & 1 else -(z + 1) // 2 for z in range(0x80)]
# bytes.translate tables: 1 for a byte with the high bit; 1 for any byte but 0.
HIGH_BIT = bytes(0x80) + bytes([1]) * 0x80
NONZERO = bytes(1) + bytes([1]) * 255
ONE = bytes([1])
# A locale name in the characters file, which is tab-separated and lists the
# names of one change separated by commas.
TSV_ESCAPES = {9: '\\t', 10: '\\n', 13: '\\r', 44: '\\x2c', 92: '\\\\'}
# Free text in one cell of it, such as why a locale was not measured.
TEXT_ESCAPES = {9: '\\t', 10: '\\n', 13: '\\r', 92: '\\\\'}
# A run of one byte other than 0, repeated.
SAME_BYTE_RUNS = re.compile(rb'([^\x00])\1*', re.DOTALL)
# An array type of four bytes, and the codec that reads it as text.
CODE_POINT = 'I' if array.array('I').itemsize == 4 else 'L'
UTF32 = 'utf-32-le' if sys.byteorder == 'little' else 'utf-32-be'


def unpack_order(raw):
    """pack_order's input back, or None when the bytes end mid-number.

    Most steps take one byte, and are decoded through a table between the
    bytes that carry the high bit, which are read one by one."""
    steps = []
    marks = raw.translate(HIGH_BIT)
    start = 0
    while True:
        end = marks.find(1, start)
        if end < 0:
            steps.extend(map(ONE_BYTE_STEP.__getitem__, raw[start:]))
            break
        steps.extend(map(ONE_BYTE_STEP.__getitem__, raw[start:end]))
        z = shift = 0
        while end < len(raw) and raw[end] & 0x80:
            z |= (raw[end] & 0x7F) << shift
            shift += 7
            end += 1
        if end == len(raw):
            return None
        z |= raw[end] << shift
        steps.append(z // 2 if not z & 1 else -(z + 1) // 2)
        start = end + 1
    return list(itertools.accumulate(steps))


def measure_one(name):
    """(name, result, data) for one locale.

    result holds two digests or an error, and data the two things measured,
    packed, or None. An error is returned, never raised and never turned into
    an empty result. A locale that could not be measured has to reach the
    comparison as exactly that, and not as a locale that did not change.
    """
    try:
        locale.setlocale(locale.LC_COLLATE, name)
    except (locale.Error, ValueError) as e:
        # ValueError covers a name Python cannot pass to C at all, such as
        # one `locale -a` printed in a legacy encoding.
        return name, {'error': f'cannot be selected: {e}'}, None
    codeset = codeset_of(name)
    if codeset is None:
        return name, {'error': 'cannot be measured: its encoding could not be '
                               'read, so neither could the bytes PostgreSQL '
                               'would compare'}, None
    try:
        result, data = measured(codeset)
    except Exception as e:
        result, data = {'error': f'measuring failed: {e!r}'}, None
    # Kept for a locale that could not be measured too: the names PostgreSQL
    # gives it depend on it, and a reader looks it up by those names.
    result['codeset'] = codeset
    return name, result, data


def libc():
    """glibc's strxfrm and strcoll, the functions PostgreSQL calls.

    Python's locale.strxfrm and locale.strcoll are wcsxfrm and wcscoll, which
    compare wide characters through tables of their own. PostgreSQL calls
    strcoll on the bytes of the database's encoding. Measured on glibc 2.34
    the two orders differ, by U+0001 in every UTF-8 locale tried. The
    argument types are declared, so that a str passed by mistake fails
    instead of reaching C as wide characters.
    """
    global LIBC
    if LIBC is None:
        lib = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6')
        lib.strxfrm.restype = ctypes.c_size_t
        lib.strxfrm.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.strcoll.restype = ctypes.c_int
        lib.strcoll.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.mbstowcs.restype = ctypes.c_size_t
        lib.mbstowcs.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        LIBC = lib
    return LIBC


class Lconv(ctypes.Structure):
    """The head of glibc's struct lconv, up to frac_digits: ten strings, then
    int_frac_digits and frac_digits, each a char (locale/locale.h, in this
    order in 2.28, 2.34 and 2.39). Read as signed, as PostgreSQL reads it on
    x86_64; where char is unsigned a value past 127 differs, and cash.c
    takes every value outside 0 to 10 as 2 either way."""
    _fields_ = [(n, ctypes.c_char_p) for n in (
        'decimal_point', 'thousands_sep', 'grouping', 'int_curr_symbol',
        'currency_symbol', 'mon_decimal_point', 'mon_thousands_sep',
        'mon_grouping', 'positive_sign', 'negative_sign')] + [
        ('int_frac_digits', ctypes.c_byte), ('frac_digits', ctypes.c_byte)]


def money_digits(names):
    """{name: frac_digits, or why there is none}: the decimals PostgreSQL's
    money type keeps in each locale.

    cash.c reads frac_digits of the locale's LC_MONETARY: through
    setlocale and localeconv() up to PostgreSQL 17 (pg_locale.c), through
    nl_langinfo_l in 18 (pg_localeconv_r.c), the same data. glibc's
    localeconv() is called directly: Python's own decodes every string of
    the struct first, and failed in 86 locales on glibc 2.34 and 2.39
    (measured). What changes a stored value is this number alone.
    """
    lib = libc()
    lib.localeconv.restype = ctypes.POINTER(Lconv)
    lib.localeconv.argtypes = []
    saved = locale.setlocale(locale.LC_MONETARY)
    out = {}
    try:
        for name in names:
            try:
                locale.setlocale(locale.LC_MONETARY, name)
            except (locale.Error, ValueError) as e:
                out[name] = f'cannot be selected: {e}'
                continue
            out[name] = lib.localeconv().contents.frac_digits
    finally:
        put_back(locale.LC_MONETARY, saved)
    return out


def money_scale(digits):
    """The decimals cash.c keeps for frac_digits: outside 0 to 10 it takes 2
    (cash.c:201-202 in REL_13_23, :217-218 in REL_18_6; the same from 13 to
    18). C, POSIX and C.utf8 give 127, glibc's CHAR_MAX, and keep 2."""
    return digits if 0 <= digits <= 10 else 2


# What postgres_use() says of one category, and of the text it converts.
CATEGORY_STATES = ('accepted', 'refused', 'unknown')
TEXT_STATES = ('converts', 'fails', 'one byte', 'not checked')
NO_CONVERTER = ('Python has no converter for their encoding, so no character '
                'of it could be built')
NO_CODESET = 'their encoding could not be read'
NO_CTYPE = 'LC_CTYPE could not be set to it'
# (size_t)-1, what mbstowcs returns for bytes it cannot convert.
NOT_CONVERTED = ctypes.c_size_t(-1).value


class NotPutBack(Exception):
    """A category of this process could not be put back as it was: what
    the process measures after that would be measured under another locale,
    so the run stops (measure())."""


def put_back(category, saved):
    """setlocale(category, saved), or NotPutBack saying why. glibc gives no
    locale back when it runs short of memory as well as when it refuses a
    name (setlocale.c, findlocale.c, 2.28 to 2.39), and saved is a name it
    accepted, so a failure here is most likely memory."""
    try:
        locale.setlocale(category, saved)
    except (locale.Error, ValueError) as e:
        names = {locale.LC_COLLATE: 'LC_COLLATE', locale.LC_CTYPE: 'LC_CTYPE',
                 locale.LC_MONETARY: 'LC_MONETARY'}
        raise NotPutBack(f'glibc could not set {names.get(category, category)} '
                         f'back to {saved!r} ({e}), as when the machine runs '
                         'short of memory') from None


def postgres_use(name):
    """What PostgreSQL does with a locale on this machine, as the calls it
    makes: {'collate', 'ctype', 'codeset', 'text'}.

    A database opens only when setlocale accepts its LC_COLLATE and its
    LC_CTYPE (CheckMyDatabase, postinit.c, REL_13_23 to REL_18_6; refused,
    it stops with "database locale is incompatible with operating system").
    Each is asked, the second whatever the first answered, as [state,
    why]: accepted, refused (setlocale gave no locale: glibc refuses the
    name, or ran short of memory, and does not say which, since a failed
    mmap of the locale archive ends in the same search of the folders as a
    name it does not have, findlocale.c and loadarchive.c in 2.39), or
    unknown (Python cannot pass the name to C at all, so glibc was not
    asked).

    Then the one thing PostgreSQL asks of LC_CTYPE that can fail outright:
    turning the database's bytes into wide characters (char2wchar, the only
    caller of mbstowcs, under upper(), lower(), initcap(), ILIKE and
    full-text search, and only in a database whose encoding has characters
    of more than one byte). glibc that cannot load the encoding's converter
    falls back to ASCII and fails on every other byte (__wcsmbs_load_conv,
    wcsmbsload.c, 2.28 to 2.39), so one character is enough: the first of
    more than one byte in the locale's corpus, in byte order, the bytes
    measured() sorts. 'text' is [converts or fails, [code point, bytes in
    hex]], ['one byte', None] when the encoding has no such character, or
    ['not checked', why]. Nothing here raises: a check that could not run
    says so. LC_COLLATE and LC_CTYPE are put back as they were.
    """
    out = {}
    for key, category in (('collate', locale.LC_COLLATE),
                          ('ctype', locale.LC_CTYPE)):
        saved = locale.setlocale(category)
        try:
            locale.setlocale(category, name)
            out[key] = ['accepted', None]
        except locale.Error as e:
            out[key] = ['refused', str(e) or 'refused']
        except ValueError as e:
            # A name `locale -a` printed in bytes Python cannot hand to C.
            out[key] = ['unknown', f'Python cannot pass the name to the C '
                                   f'library ({e})']
        finally:
            put_back(category, saved)
    out['codeset'] = None
    if out['ctype'][0] != 'accepted':
        out['text'] = ['not checked', NO_CTYPE]
        return out
    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        out['codeset'] = locale.nl_langinfo(locale.CODESET) or None
        corpus = corpus_for(out['codeset']) if out['codeset'] else None
        if out['codeset'] is None:
            out['text'] = ['not checked', NO_CODESET]
        elif corpus is None:
            out['text'] = ['not checked', NO_CONVERTER]
        else:
            hit = next(((c, b) for c, b in zip(corpus[0], corpus[1])
                        if len(b) > 1), None)
            if hit is None:
                out['text'] = ['one byte', None]
            else:
                buf = (ctypes.c_wchar * (len(hit[1]) + 1))()
                r = libc().mbstowcs(buf, hit[1], len(hit[1]) + 1)
                out['text'] = ['fails' if r == NOT_CONVERTED else 'converts',
                               [hit[0], hit[1].hex()]]
    except Exception as e:
        out['text'] = ['not checked', f'checking failed: {e!r}']
    finally:
        put_back(locale.LC_CTYPE, saved)
    return out


def measure_locale(name):
    """(name, result, data, use, ctype): measure_one's answer, then
    postgres_use's, then ctype_identity's, in the same measuring process,
    where the locale's corpus is built."""
    name, result, data = measure_one(name)
    use = postgres_use(name)
    return name, result, data, use, ctype_identity(name, use)


# The character classes PostgreSQL asks glibc about, isw<class> of a wide
# character and is<class> of a byte, under upper(), lower(), initcap(),
# ILIKE, regular expressions and full-text search: nine as isw<class> by
# name, and xdigit through the full-text parser's p_iswhat, as isxdigit of a
# byte and iswxdigit of a wide character (wparser_def.c:461-469 in
# REL_13_23, :454-462 in REL_18_6). iswcntrl and
# iswblank appear nowhere in it (REL_13_23 and REL_18_6); its regular
# expressions fix [:cntrl:] and [:blank:] themselves (regc_locale.c).
CTYPE_CLASSES = ('alpha', 'upper', 'lower', 'digit', 'space', 'punct',
                 'alnum', 'print', 'graph', 'xdigit')
# 1 << LC_CTYPE, which is 0 in glibc.
LC_CTYPE_MASK = 1
# The first four bytes of a compiled LC_CTYPE: LIMAGIC (LC_CTYPE),
# locale/localeinfo.h:33-38 in 2.28, 2.34 and 2.39.
CTYPE_MAGIC = 0x20090720
# toupper, tolower and the classes, one byte each for each of the 256 bytes.
CTYPE_BYTES = (2 + len(CTYPE_CLASSES)) * 256
# What a locale's entry in the ctype part says, beside the table it uses:
# glibc refused it, Python could not pass it (both as postgres_use said), or
# its data could not be read.
CTYPE_STATES = ('table', 'refused', 'unknown', 'not checked')
CTYPE_UNREAD = ('glibc did not give its LC_CTYPE data the way glibc 2.28 to '
                '2.39 lay it out')
CTYPE_MOVED = ('its LC_CTYPE data changed while this machine was measured; '
               'measure it again')
# The identity of a compiled LC_CTYPE: its sha256, or glibc's own.
CTYPE_IDENTITY = re.compile(r'[0-9a-f]{64}|builtin')
CTYPE_FUNCTIONS = None   # glibc's LC_CTYPE functions, loaded once per process


def ctype_libc():
    """glibc with newlocale, freelocale and the functions of LC_CTYPE that
    PostgreSQL calls, their argument types declared: towupper, towlower and
    isw<class> take a wint_t, toupper, tolower and is<class> an int."""
    global CTYPE_FUNCTIONS
    if CTYPE_FUNCTIONS is None:
        lib = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6',
                          use_errno=True)
        lib.newlocale.restype = ctypes.c_void_p
        lib.newlocale.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p]
        lib.freelocale.restype = None
        lib.freelocale.argtypes = [ctypes.c_void_p]
        for n in ['towupper', 'towlower'] + ['isw' + c for c in CTYPE_CLASSES]:
            f = getattr(lib, n)
            f.argtypes = [ctypes.c_uint32]
            f.restype = ctypes.c_uint32 if n.startswith('tow') else ctypes.c_int
        for n in ['toupper', 'tolower'] + ['is' + c for c in CTYPE_CLASSES]:
            f = getattr(lib, n)
            f.argtypes = [ctypes.c_int]
            f.restype = ctypes.c_int
        CTYPE_FUNCTIONS = lib
    return CTYPE_FUNCTIONS


def ctype_identity(name, use):
    """[state, detail]: which LC_CTYPE data glibc loads for the locale.

    ['table', the sha256 of the data] for a compiled LC_CTYPE, whether glibc
    took it from the locale archive or from a folder, or ['table',
    'builtin'] for the one glibc carries inside itself (C and POSIX: no file,
    C-ctype.c). Two names with the same data answer every call of LC_CTYPE
    alike, so one table measured stands for both (ctype_tables). The data is
    what newlocale() loaded, read through the head of struct __locale_data
    (name, filedata, filesize; localeinfo.h:49-53 in 2.28, 2.34 and 2.39),
    the first entry of a locale_t (__locale_t.h); a compiled file starts with
    CTYPE_MAGIC, and anything else is not read as data.

    Whether glibc accepts the name is postgres_use's answer, copied for a
    name setlocale did not accept. newlocale is asked only about a name it
    did, and finds a locale by the same path (_nl_find_locale, setlocale.c:414
    and newlocale.c:169 in 2.39), but can still give none: for want of
    memory, or for a name holding ';', which it reads as a list of
    categories (newlocale.c:121-158 in 2.39). Its LC_CTYPE is then not
    checked, with the reason errno holds then, cleared before the call,
    and the run says so when it ends."""
    if use['ctype'][0] != 'accepted':
        return list(use['ctype'])
    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8:
        return ['not checked', 'this machine is not 64-bit, and only the '
                               'layout of a 64-bit glibc is read']
    lib = ctype_libc()
    ctypes.set_errno(0)
    loc = lib.newlocale(LC_CTYPE_MASK, name.encode('utf-8', 'surrogateescape'),
                        None)
    if not loc:
        errno = ctypes.get_errno()
        return ['not checked', 'newlocale gave no locale for it: '
                + (os.strerror(errno) if errno else 'no reason given')]
    try:
        data = ctypes.c_void_p.from_address(loc).value
        if not data:
            return ['not checked', CTYPE_UNREAD]
        filedata = ctypes.c_void_p.from_address(data + 8).value
        size = ctypes.c_int64.from_address(data + 16).value
        if filedata is None:
            return ['table', 'builtin'] if size == 0 else ['not checked', CTYPE_UNREAD]
        if size < 4:
            return ['not checked', CTYPE_UNREAD]
        raw = ctypes.string_at(filedata, size)
        if int.from_bytes(raw[:4], sys.byteorder) != CTYPE_MAGIC:
            return ['not checked', CTYPE_UNREAD]
        return ['table', hashlib.sha256(raw).hexdigest()]
    except Exception as e:
        return ['not checked', f'reading it failed: {e!r}']
    finally:
        lib.freelocale(loc)


def ctype_tables(name):
    """(name, wide, bytes, why, identity): what glibc's LC_CTYPE functions
    answer under the locale, for every code point and for every byte, or why
    it could not be measured; and ctype_identity of the data they answered
    from, taken in this process right after setlocale loaded it (newlocale
    finds the data setlocale cached, by the same _nl_find_locale), so that
    ctype_part files the table under that data and no other.

    wide is canonical JSON: 'upper' and 'lower', the [code point, what
    towupper or towlower gives] of each code point it changes, and
    'classes', the runs of code points in each class of CTYPE_CLASSES. bytes
    is toupper, tolower and is<class> of the 256 bytes, one byte each. Both
    are what a database hands glibc: upper(), lower() and full-text search
    turn the text of a database of more than one byte per character into
    wide characters and ask of each byte in one of one byte (formatting.c up
    to PostgreSQL 17, pg_locale_libc.c in 18; wparser_def.c); regular
    expressions use wide characters only in UTF-8, and bytes in every other
    encoding (regc_pg_locale.c). LC_CTYPE is put back as it was."""
    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        identity = ctype_identity(name, {'ctype': ['accepted', None]})
        lib = ctype_libc()
        up = array.array(CODE_POINT, map(lib.towupper, CODEPOINTS))
        lo = array.array(CODE_POINT, map(lib.towlower, CODEPOINTS))
        # code_point_runs splits a run where CODEPOINTS skips the
        # surrogates, as the code points either side are not consecutive.
        wide = {'upper': [[c, u] for c, u in zip(CODEPOINTS, up) if u != c],
                'lower': [[c, u] for c, u in zip(CODEPOINTS, lo) if u != c],
                'classes': {k: code_point_runs(
                    [c for c, yes in zip(CODEPOINTS, map(
                        getattr(lib, 'isw' + k), CODEPOINTS)) if yes])
                    for k in CTYPE_CLASSES}}
        rows = [bytes(map(lib.toupper, range(256))),
                bytes(map(lib.tolower, range(256)))]
        rows += [bytes(map(bool, map(getattr(lib, 'is' + k), range(256))))
                 for k in CTYPE_CLASSES]
        return (name, json.dumps(wide, sort_keys=True,
                                 separators=(',', ':')).encode('ascii'),
                b''.join(rows), None, identity)
    except Exception as e:
        return name, None, None, f'measuring it failed: {e!r}', None
    finally:
        put_back(locale.LC_CTYPE, saved)


def ctype_part(identities, measure_tables):
    """The ctype part of a measurement: {'locales': each name's
    ctype_identity, 'tables': for each identity its [wide key, bytes key] or
    ['not checked', why], 'wide' and 'bytes': each table, packed, under the
    sha256 of its raw bytes (never of the packed text: zlib-ng packs the
    same bytes otherwise). measure_tables maps ctype_tables over the names
    given, one per identity, the first of its locales by name. A table
    measured on other data than its identity names (the locale changed on
    disk between the two passes) is not checked: filed under the old data,
    it would stand for rules nobody measured there."""
    members = collections.defaultdict(list)
    for name, entry in identities.items():
        if entry[0] == 'table':
            members[entry[1]].append(name)
    keys = sorted(members)
    tables, wide, byte = {}, {}, {}
    for key, (_, w, b, why, identity) in zip(keys, measure_tables(
            [min(members[k]) for k in keys])):
        if why is not None:
            tables[key] = ['not checked', why]
            continue
        if identity != ['table', key]:
            tables[key] = ['not checked', CTYPE_MOVED if identity[0] == 'table'
                           else f'its LC_CTYPE data could not be read again: '
                                f'{identity[1]}']
            continue
        wk, bk = hashlib.sha256(w).hexdigest(), hashlib.sha256(b).hexdigest()
        tables[key] = [wk, bk]
        wide.setdefault(wk, pack(w))
        byte.setdefault(bk, pack(b))
    return {'locales': dict(identities), 'tables': tables, 'wide': wide,
            'bytes': byte}


# The encodings whose bytes PostgreSQL hands to a converter glibc loads
# from a module of its own (gconv), which RHEL ships apart, in
# glibc-gconv-extra: the four EUC a database can be in (EUC_JP, EUC_KR,
# EUC_CN, EUC_TW), and the five of one byte PostgreSQL does not know, whose
# locales a database of any encoding may use (check_encoding_locale_matches,
# dbcommands.c, and chklocale.c, REL_13_23 and REL_18_6). Every other
# encoding of a glibc locale is UTF-8, of one byte in PostgreSQL (never
# converted), or one PostgreSQL allows only on the client (no database).
CONVERTER_CODESETS = ('EUC-JP', 'EUC-KR', 'GB2312', 'EUC-TW', 'TIS-620',
                      'KOI8-T', 'PT154', 'ARMSCII-8', 'GEORGIAN-PS')
# What the converter part says of an encoding: measured under one of its
# locales, no locale of it on the machine, or not checked, with why.
CONVERTER_STATES = ('measured', 'no locale', 'not checked')
# A sha256 in hex, as sequence_digest() gives it.
SHA256 = re.compile(r'[0-9a-f]{64}')
# What iconv_open said of the converter: it opens, it fails (errno), or it
# could not be asked.
ICONV_STATES = ('opens', 'fails', 'not asked')
EUC_BYTE = range(0xA1, 0xFF)
SEQUENCES = {}            # per encoding, accepted_sequences(), kept per process
SEQUENCE_DIGESTS = {}     # per encoding, sequence_digest(), kept per process
CONVERTER_FUNCTIONS = None   # glibc's iconv_open and mbrtowc, loaded once
# What iconv_open returns for a converter it cannot open, (iconv_t)-1.
NO_ICONV = ctypes.c_void_p(-1).value
# Written into the wide character before each mbrtowc: no wchar_t of glibc.
UNWRITTEN = 0xFFFFFFFF


def accepted_sequences(codeset):
    """Every character a database can hold in the encoding, as the bytes
    PostgreSQL stores, in byte order: ASCII but NUL, and what
    pg_eucjp_verifychar, pg_euckr_verifychar (EUC_CN's too) and
    pg_euctw_verifychar accept (wchar.c:1133-1267 in REL_13_23, where they
    are pg_*_verifier, and :1104-1320 in REL_18_6). For an encoding of one
    byte, every byte but NUL: a database of more than one byte per character
    hands glibc its own bytes, which the locale's converter reads one by
    one."""
    if codeset not in SEQUENCES:
        low = [bytes([b]) for b in range(1, 0x80)]
        two = [bytes([a, b]) for a in EUC_BYTE for b in EUC_BYTE]
        if codeset in ('EUC-KR', 'GB2312'):
            out = low + two
        elif codeset == 'EUC-JP':
            out = (low + two + [bytes([0x8E, b]) for b in range(0xA1, 0xE0)]
                   + [bytes([0x8F, a, b]) for a in EUC_BYTE for b in EUC_BYTE])
        elif codeset == 'EUC-TW':
            out = (low + [bytes([0x8E, p, a, b]) for p in range(0xA1, 0xA8)
                          for a in EUC_BYTE for b in EUC_BYTE]
                   + [bytes([a, b]) for a in range(0x80, 0x100)
                      if a not in (0x8E, 0x8F) for b in EUC_BYTE])
        else:
            out = [bytes([b]) for b in range(1, 0x100)]
        SEQUENCES[codeset] = sorted(out)
    return SEQUENCES[codeset]


def sequence_digest(codeset):
    """The sha256 of accepted_sequences(codeset), each in hex on a line of
    its own: a measured entry keeps it, so that a copy of this script with
    another list of sequences of the same length does not pair each value
    with another sequence's bytes."""
    if codeset not in SEQUENCE_DIGESTS:
        SEQUENCE_DIGESTS[codeset] = hashlib.sha256(b''.join(
            s.hex().encode('ascii') + b'\n'
            for s in accepted_sequences(codeset))).hexdigest()
    return SEQUENCE_DIGESTS[codeset]


def converter_libc():
    """glibc with iconv_open, iconv_close and mbrtowc, their argument types
    declared."""
    global CONVERTER_FUNCTIONS
    if CONVERTER_FUNCTIONS is None:
        lib = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6',
                          use_errno=True)
        lib.mbrtowc.restype = ctypes.c_size_t
        lib.mbrtowc.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.c_size_t, ctypes.c_void_p]
        lib.iconv_open.restype = ctypes.c_void_p
        lib.iconv_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.iconv_close.restype = ctypes.c_int
        lib.iconv_close.argtypes = [ctypes.c_void_p]
        CONVERTER_FUNCTIONS = lib
    return CONVERTER_FUNCTIONS


def converter_of(job):
    """(codeset, entry): what glibc's converter of codeset gives on this
    machine, job being (codeset, the locale to ask under, or None when the
    machine lists none of that encoding).

    entry is [state, iconv_open's answer, the locale, detail, the
    sequence_digest of what was measured, or None]. iconv_open
    (WCHAR_T, codeset) says whether glibc can load the converter, ['opens',
    None] or ['fails', strerror of errno, or 'no reason given' where errno
    was left 0]: without glibc-gconv-extra it fails, and
    glibc then converts every LC_CTYPE of that encoding as ASCII
    (__wcsmbs_load_conv finds the converter by the same
    __gconv_find_transform, wcsmbsload.c 2.28 to 2.39). Then, under the
    locale, mbrtowc of each of accepted_sequences, from a cleared state
    (glibc keeps the bytes of a character it has not seen whole in it,
    iconv/loop.c): detail is, packed, the code point of each whole, or 0
    where glibc refuses it (EILSEQ), what makes PostgreSQL stop with
    "invalid multibyte character for locale". Measured on collaudit8, glibc
    gives each of them whole or refuses it; where it reads one otherwise
    (in part, or as the start of a longer one), the encoding is not
    checked, with the first such sequence, as no value here says what
    PostgreSQL then does; so is one glibc reads whole without writing a
    character, which none of the nine converters does in 2.28 or 2.39
    (their BODY), as the value left from the sequence before would read as
    its own. ['no locale', answer, None, None, None] when there is no
    locale to ask under; ['not checked', answer, the locale, why, None]
    when a step failed. Nothing here raises but put_back's NotPutBack,
    which stops the run (measure()): LC_CTYPE is put back as it was."""
    codeset, name = job
    try:
        lib = converter_libc()
        ctypes.set_errno(0)
        handle = lib.iconv_open(b'WCHAR_T', codeset.encode('ascii'))
        if handle in (None, NO_ICONV):
            errno = ctypes.get_errno()
            opens = ['fails', os.strerror(errno) if errno else 'no reason given']
        else:
            lib.iconv_close(handle)
            opens = ['opens', None]
    except Exception as e:
        return codeset, ['not checked', ['not asked', None], name,
                         f'asking glibc failed: {e!r}', None]
    if name is None:
        return codeset, ['no locale', opens, None, None, None]
    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        wc = ctypes.c_uint32()
        state = ctypes.create_string_buffer(16)
        refused = ctypes.c_size_t(-1).value
        got = []
        for s in accepted_sequences(codeset):
            ctypes.memset(state, 0, len(state))
            # glibc's wchar_t holds 31 bits: a value left as written here
            # was not written by mbrtowc.
            wc.value = UNWRITTEN
            r = lib.mbrtowc(ctypes.byref(wc), s, len(s), state)
            if r == len(s) and wc.value != UNWRITTEN:
                got.append(wc.value)
            elif r == refused:
                got.append(0)
            else:
                how = ('as one character but wrote none' if r == len(s) else
                       f'neither as one character nor as an error (mbrtowc '
                       f'gave {ctypes.c_ssize_t(r).value})')
                return codeset, ['not checked', opens, name,
                                 f'glibc read the bytes {spaced(s)} {how}', None]
        return codeset, ['measured', opens, name, pack_order(got),
                         sequence_digest(codeset)]
    except Exception as e:
        return codeset, ['not checked', opens, name,
                         f'measuring it failed: {e!r}', None]
    finally:
        put_back(locale.LC_CTYPE, saved)


def first_locales(use):
    """{codeset: the first locale by name whose encoding postgres_use read
    as it}."""
    first = {}
    for name in sorted(use):
        first.setdefault(use[name]['codeset'], name)
    return first


def converter_part(uses, mapper):
    """The converter part of a measurement: {codeset: converter_of's entry}
    for each of CONVERTER_CODESETS, asked under the first of its locales by
    name. One locale is enough: glibc picks the converter by the encoding's
    name alone, and every locale of each of the nine converted alike on
    glibc 2.28, 2.34 and 2.39 (measured, 2026-10-24). mapper maps
    converter_of over the jobs, as the pool's map does."""
    first = first_locales(uses)
    return dict(mapper(converter_of, [(cs, first.get(cs))
                                      for cs in CONVERTER_CODESETS]))


# The names a PostgreSQL process has in /proc/PID/comm: the program's, which
# PostgreSQL never changes (its one prctl is PR_SET_PDEATHSIG; pmsignal.c),
# and postmaster, the link PostgreSQL 15 and earlier install beside it.
POSTGRES_NAMES = (b'postgres', b'postmaster')
# The C library in a line of /proc/PID/maps: libc.so.6 on glibc 2.34 and
# later, libc-2.28.so on 2.28 (measured on the three fixtures). The kernel
# ends the path with " (deleted)" once the file was replaced, as a glibc
# update replaces it; what comes before that must end the libc's name, or
# libc.so.6.old or libcrypto would count.
LIBC_PATH = re.compile(rb'/libc(?:-[0-9.]+)?\.so(?:\.[0-9]+)*$')
DELETED = b' (deleted)'


def hidden_processes(proc='/proc', mounts='/proc/mounts'):
    """Why /proc may hide other users' processes from this one, or None.

    Under hidepid an ordinary user lists none of them: measured on Rocky 9,
    a user saw no postgres process at all, where without it the same user
    sees every one and cannot read their maps. Seeing none would then read
    as "no PostgreSQL runs here".
    """
    try:
        with open(mounts, 'rb') as f:
            lines = f.read().split(b'\n')
    except OSError as e:
        return (f'{mounts} could not be read ({e.strerror}), so whether /proc '
                'hides other users\' processes is not known')
    found = None
    for line in lines:
        fields = line.split()
        if (len(fields) > 3 and fields[2] == b'proc'
                and fields[1] == proc.encode()):
            found = fields[3]     # the last mount over /proc is the one seen
    if found is None:
        return f'{mounts} lists no proc file system mounted on {proc}'
    hide = [o for o in found.split(b',') if o.startswith(b'hidepid=')
            and o not in (b'hidepid=0', b'hidepid=off')]
    if hide:
        return (f'{proc} is mounted with {hide[-1].decode("ascii", "replace")}, '
                'which hides other users\' processes')
    return None


def libc_lines(maps):
    """[(path, device, inode, deleted)] of the C library lines of a
    /proc/PID/maps text: libc.so.6 on glibc 2.34 and later, libc-2.28.so on
    2.28. The kernel ends the path with " (deleted)" once the file was
    replaced, as a glibc update replaces it."""
    out = []
    for line in maps.split(b'\n'):
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        name, deleted = fields[5], fields[5].endswith(DELETED)
        if deleted:
            name = name[:-len(DELETED)]
        if LIBC_PATH.search(name):
            out.append((fields[5].decode('utf-8', 'surrogateescape'),
                        fields[3], fields[4], deleted))
    return out


def postgres_libc(proc='/proc', mounts='/proc/mounts', euid=None):
    """Which PostgreSQL processes of this machine do not run the C library
    this process measured with.

    Two ways, both measured. A glibc update replaces the file, and a process
    started before it keeps the old one, as does every process it forks
    (Rocky 9, glibc 2.34-83 to 2.34-283 with the postmaster running: every
    postgres process mapped "libc.so.6 (deleted)" until the restart). And a
    PostgreSQL in a container runs the container's C library, another file
    that was never deleted (false-negative-reviewer, round 1, measured on a
    host whose containers mapped twelve such files). So a process is clean
    only when it maps the very file this process does, by device and inode
    in /proc/PID/maps.

    Only root and the user PostgreSQL runs as can read a postgres process's
    maps. Every way of not knowing is said: a process whose maps could not
    be read or show no C library, a process whose name could not be read,
    this process's own C library not found, and a /proc that hides other
    users' processes. None of them reads as "none", and finding no postgres
    process at all is said too, by the outputs. This process's own C library
    deleted means glibc was replaced while it measured ('replaced'), which
    the outputs say voids what was measured (measured on Rocky 9: a process
    loaded before an update). A process whose maps read empty runs no code,
    a zombie or one exiting (measured: 0 bytes), and is passed over.
    """
    out = {'looked_for': [n.decode('ascii') for n in POSTGRES_NAMES],
           'processes': 0, 'stale': [], 'other': [], 'unreadable': 0,
           'no_libc': 0, 'unnamed': 0, 'not_checked': None,
           'replaced': False}
    try:
        pids = sorted(int(p) for p in os.listdir(proc) if p.isdigit())
    except OSError as e:
        out['not_checked'] = f'{proc} could not be read ({e.strerror})'
        return out
    why = []
    # This process's own C library, the one strcoll ran from, deleted or
    # not: a glibc update while it measured deletes it, and a process that
    # maps that very file runs what was measured.
    try:
        with open(f'{proc}/self/maps', 'rb') as f:
            mine = libc_lines(f.read())
    except OSError:
        mine = []
    own = {(dev, ino) for _, dev, ino, _ in mine}
    out['replaced'] = any(deleted for _, _, _, deleted in mine)
    if not own:
        why.append(f'the C library this process measured with is not in '
                   f'{proc}/self/maps')
    if (os.geteuid() if euid is None else euid) != 0:
        hidden = hidden_processes(proc, mounts)
        if hidden:
            why.append(hidden)
    out['not_checked'] = '; '.join(why) or None
    for pid in pids:
        try:
            with open(f'{proc}/{pid}/comm', 'rb') as f:
                comm = f.read().rstrip(b'\n')
        except (FileNotFoundError, ProcessLookupError):
            continue        # it ended between the listing and now
        except OSError:
            out['unnamed'] += 1
            continue
        if comm not in POSTGRES_NAMES:
            continue
        try:
            with open(f'{proc}/{pid}/maps', 'rb') as f:
                maps = f.read()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            out['processes'] += 1
            out['unreadable'] += 1
            continue
        if not maps:
            continue        # no memory left to map: a zombie, or exiting
        out['processes'] += 1
        found = libc_lines(maps)
        gone = [p for p, dev, ino, deleted in found
                if deleted and (dev, ino) not in own]
        elsewhere = [p for p, dev, ino, deleted in found
                     if not deleted and own and (dev, ino) not in own]
        if not found:
            out['no_libc'] += 1
        elif gone:
            out['stale'].append([pid, gone[0]])
        elif elsewhere:
            out['other'].append([pid, elsewhere[0]])
    return out


def control_fingerprint():
    """What C and POSIX measure on every glibc: (order, pairs, probes).

    Both are the locale glibc carries inside itself (findlocale.c, from
    2.12 to 2.39), with no rules, so strcoll is strcmp (strcoll_l.c, the
    same versions): every character in byte order, every neighbour told
    apart as a different letter, the first pair of letters as the letters'
    pair and none for accents or case, which the C locale's 127 characters
    cannot spell. A machine whose C differs did not measure what this
    script means to measure, and nothing it measured can be trusted.
    """
    seq = list(range(1, 0x80))
    a, b = CANDIDATES[0][0]
    probes = f'{ord(a):X}.{ord(b):X}> ? ?'
    told = bytes([3 << 3 | 3 << 5]) * (len(seq) - 1)
    return (hashlib.sha256(''.join(map(chr, seq)).encode('utf-8')).hexdigest(),
            hashlib.sha256(probes.encode('utf-8') + b'\n' + told).hexdigest(),
            probes)


def corpus_for(codeset):
    """(code points, their bytes, digest) of what a database in this encoding
    can hold, in byte order; None when Python has no converter for it.

    Byte order, because the sort that follows is stable and PostgreSQL breaks
    a strcoll tie by the bytes, so equal characters end up in PostgreSQL's
    order. A character the encoding cannot hold is left out: no database in
    that encoding can contain it. A character Python's codec cannot encode is
    left out as well, which is a limit, and so are the bytes PostgreSQL stores
    for a character where they are not Python's: the byte sequences it stores,
    converting from UTF-8, that no corpus here holds are in POSTGRES_ONLY,
    worked out against PostgreSQL's own conversions, and the comparison names
    them (EUC-KR's U+327E, A2 E8, is one). The lone bytes 0x80-0x9F of EUC-KR
    and EUC-JP PostgreSQL does not accept (pg_euckr_verifychar and
    pg_eucjp_verifychar in its src/common/wchar.c). The digest covers each
    character and the bytes it was measured as, so that two machines whose
    converters differ, in what they encode or in how, are not compared as if
    they measured the same thing.
    """
    try:
        codec = codecs.lookup(codeset).name
    except LookupError:
        return None
    if codec not in CORPORA:
        if codec == 'utf-8' and UTF8_BYTES is not None:
            pairs = list(zip(UTF8_BYTES, CODEPOINTS))   # already in byte order
        else:
            # One character of the encoding each: at most four bytes, and
            # bytes that decode to the character itself, or that Python
            # cannot decode and no other character encodes to. Not a sequence
            # Python builds for a character the encoding lacks (EUC-KR: the
            # 8,822 Hangul syllables outside KS X 1001, eight bytes each), nor
            # another character's bytes under a second name (EUC-JP: U+00A5
            # and U+203E encode to the bytes of '\' and '~'). U+3164 HANGUL
            # FILLER is the case the second clause keeps: glibc's EUC-KR has
            # it as A4 D4, which Python reads as the start of an eight-byte
            # sequence and will not decode alone.
            encoded = []
            for c in CODEPOINTS:
                try:
                    b = chr(c).encode(codec)
                except UnicodeError:
                    continue
                if len(b) <= 4:
                    encoded.append((b, c))
            count = collections.Counter(b for b, c in encoded)
            pairs = []
            for b, c in encoded:
                try:
                    keep = b.decode(codec) == chr(c)
                except UnicodeError:
                    keep = count[b] == 1
                if keep:
                    pairs.append((b, c))
            pairs.sort()
        cps = [c for b, c in pairs]
        CORPORA[codec] = (cps, [b for b, c in pairs], corpus_id(pairs))
    return CORPORA[codec]


def corpus_id(pairs):
    """A digest of (bytes, code point) pairs: each code point with the bytes it
    was measured as, in code point order."""
    h = hashlib.sha256()
    for b, c in sorted(pairs, key=lambda pair: pair[1]):
        h.update(b'%X:%s\n' % (c, b.hex().encode('ascii')))
    return h.hexdigest()


def codeset_of(name):
    """The locale's encoding as glibc names it ('UTF-8', 'ISO-8859-1'), or None.

    PostgreSQL uses a locale only in a database of that encoding, compares
    the bytes of that encoding, and gives the locale other names too (see
    postgres_names). So the encoding decides what is measured and which of a
    reader's names a change applies to. None means it could not be read.
    """
    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, name)
        codeset = locale.nl_langinfo(locale.CODESET) or None
    except (locale.Error, ValueError, AttributeError):
        return None
    finally:
        put_back(locale.LC_CTYPE, saved)
    return codeset


def measured(codeset):
    corpus = corpus_for(codeset)
    if corpus is None:
        return {'error': f'cannot be measured: Python has no converter for '
                         f'{codeset}, the encoding of this locale, so the '
                         'bytes PostgreSQL would compare cannot be built'}, None
    cps, data, corpus_digest_ = corpus
    lib = libc()
    strcoll, strxfrm = lib.strcoll, lib.strxfrm
    buf = ctypes.create_string_buffer(512)

    def key(b):
        n = strxfrm(buf, b, 512)
        if n < 512:
            return ctypes.string_at(buf, n)
        big = ctypes.create_string_buffer(n + 1)
        strxfrm(big, b, n + 1)
        return ctypes.string_at(big, n)

    keys = [key(b) for b in data]
    # The sort is stable and the corpus is in byte order, so characters with
    # equal keys stay in the order PostgreSQL's tie-break gives them.
    order = sorted(range(len(data)), key=keys.__getitem__)
    del keys

    # The pairs that tell letters, accents and case apart in this locale; a
    # level without one is recorded as not built (3).
    probes = choose_probes(codecs.lookup(codeset).name, strcoll)
    plan = [(1 + 2 * k, p) for k, p in enumerate(probes)]

    # strxfrm is a shortcut to the sort; PostgreSQL compares with strcoll.
    # Every neighbour is checked with strcoll, and since strcoll is a
    # transitive order, neighbours that agree mean the whole order is
    # strcoll's.
    disagree = 0
    told_apart = bytearray(len(order) - 1)
    for i in range(len(order) - 1):
        x, y = data[order[i]], data[order[i + 1]]
        r = strcoll(x, y)
        if r > 0 or (r == 0 and x > y):
            disagree += 1
        # Whether strcoll calls the neighbours equal: where it does, the
        # bytes decide, and a tie that later becomes an order moves strings
        # of more than one letter even where no character moves.
        v = 1 if r == 0 else 0
        # x+hi against y+lo: when x and y differ at least as strongly as lo
        # and hi do (different letters against an accent, say), x decides and
        # x+hi sorts first; when they differ more weakly, y+lo sorts first.
        # Where the level is compared from the end, hi+x against lo+y. The
        # answer is kept as strcoll gave it, equal included, so that nothing
        # here depends on a tie-break.
        for shift, probe in plan:
            if probe is None:
                v |= 3 << shift
            elif probe[4]:
                v |= sign(strcoll(probe[3] + x, probe[2] + y)) << shift
            else:
                v |= sign(strcoll(x + probe[3], y + probe[2])) << shift
        told_apart[i] = v
    if disagree:
        return {'error': f'strxfrm and strcoll disagree on {disagree} '
                         'neighbouring pair(s), so strcoll\'s order was not '
                         'measured'}, None

    # A level no candidate measured is looked for among the neighbours
    # themselves, and measured with the pair found there.
    for k, probe in enumerate(probes):
        if probe is None:
            probes[k] = derived_probe(k, probes, told_apart, data, order,
                                      strcoll, codecs.lookup(codeset).name)
            if isinstance(probes[k], tuple):
                shift, keep = 1 + 2 * k, ~(3 << (1 + 2 * k)) & 0xFF
                lob, hib, prepend = probes[k][2], probes[k][3], probes[k][4]
                for i in range(len(order) - 1):
                    x, y = data[order[i]], data[order[i + 1]]
                    r = strcoll(hib + x, lob + y) if prepend else strcoll(x + hib, y + lob)
                    told_apart[i] = (told_apart[i] & keep) | (sign(r) << shift)

    seq = [cps[i] for i in order]
    sequence = ''.join(map(chr, seq)).encode('utf-8')
    told_apart = bytes(told_apart)
    # Which pairs measured it goes into the digest: pairs chosen differently
    # on two machines mean those very pairs are told apart differently, which
    # is a change of its own.
    # Code points in hex, so that a pair taken from the neighbours (a space,
    # a '<') cannot be misread.
    spec = ' '.join('?' if p is None else 'none' if p is ABSENT
                    else f'{ord(p[0]):X}.{ord(p[1]):X}' + ('<' if p[4] else '>')
                    for p in probes)
    return ({'order': hashlib.sha256(sequence).hexdigest(),
             'pairs': hashlib.sha256(spec.encode('utf-8') + b'\n' + told_apart)
             .hexdigest(),
             'chars': len(seq), 'corpus': corpus_digest_, 'probes': spec,
             'partly': unclassified(probes, told_apart)},
            {'order': pack_order(seq), 'pairs': pack(told_apart)})


def choose_probes(codec, strcoll):
    """For each level, (lo, hi, lo bytes, hi bytes, prepend), or None.

    A candidate measures a level in this locale when the pairs chosen for the
    stronger levels do not tell its two characters apart, and when, put
    against itself, it tells them apart. That second test also finds the
    direction: where a level is compared from the end of the string, as
    accents are in fr_CA, the probe only works in front of the character, so
    it goes there (prepend).
    """
    def tells(probe, x, y):
        lo, hi, prepend = probe[2], probe[3], probe[4]
        return strcoll(*((hi + x, lo + y) if prepend else (x + hi, y + lo))) < 0

    chosen = []
    for level in CANDIDATES:
        pick = None
        for p, q in level:
            try:
                pb, qb = p.encode(codec), q.encode(codec)
            except UnicodeError:
                continue
            r = strcoll(pb, qb)
            if r == 0:
                continue
            lo, hi, lob, hib = (p, q, pb, qb) if r < 0 else (q, p, qb, pb)
            if any(tells(c, lob, hib) for c in chosen if c):
                continue      # a stronger level already tells them apart
            for prepend in (False, True):
                if tells((lo, hi, lob, hib, prepend), lob, hib):
                    pick = (lo, hi, lob, hib, prepend)
                    break
            if pick:
                break
        chosen.append(pick)
    return chosen


def derived_probe(k, probes, told_apart, data, order, strcoll, codec):
    """A pair of neighbours that differ at level k exactly, as that level's
    probe; ABSENT when there is none; None when it cannot be told.

    It can be told only where a weaker level has a pair. A neighbour pair
    that the stronger levels' pairs do not tell apart, that tells itself
    apart, and that does not tell the weaker level's pair apart differs at
    level k exactly: less than a letter, more than case. Where no neighbour
    pair does, no two characters differ at that level (between any two, the
    level at which they differ is that of some pair of neighbours between
    them), so nothing is lost by not measuring it. In the Arabic locales the
    fixed candidates find no accent pair, and the neighbours with a
    difference below the letter are the case pairs and nine Arabic marks
    (measured on glibc 2.34).
    """
    weaker = next((p for p in probes[k + 1:] if isinstance(p, tuple)), None)
    if weaker is None:
        return None
    stronger = [1 + 2 * j for j in range(k) if isinstance(probes[j], tuple)]

    def tells(lob, hib, prepend, x, y):
        return strcoll(*((hib + x, lob + y) if prepend else (x + hib, y + lob))) < 0

    for i, v in enumerate(told_apart):
        if v & 1 or not all((v >> s) & 3 != 0 for s in stronger):
            continue
        x, y = data[order[i]], data[order[i + 1]]
        for prepend in (False, True):
            if tells(x, y, prepend, x, y) and not tells(x, y, prepend,
                                                        weaker[2], weaker[3]):
                return (x.decode(codec), y.decode(codec), x, y, prepend)
    return ABSENT


def unclassified(probes, told_apart):
    """How many neighbour pairs differ by less than a letter in a way no pair
    could classify: 0 when everything was measured.

    Where a level has no pair and could not be shown absent, a difference at
    it cannot be told from a weaker one. Nor can a pair chosen for a level
    between the letters and the missing one be shown to measure its own
    level rather than the missing one: choose_probes tests a candidate only
    against the stronger levels, and derived_probe, which also tests against
    the weaker one, has none to test against here. So every pair the letters
    pair does not tell apart is counted: none of them is assured. C.utf8 has
    no accent or case pairs and loses nothing, since it tells every
    neighbour apart as a different letter; where some neighbours differ by
    less, they are counted.
    """
    if not any(p is None for p in probes):
        return 0
    letters = [1] if isinstance(probes[0], tuple) else []
    return sum(1 for v in told_apart
               if not v & 1 and all((v >> s) & 3 != 0 for s in letters))


def libc_version():
    """'glibc 2.34', or None on a system that is not glibc."""
    try:
        v = os.confstr('CS_GNU_LIBC_VERSION')
    except (AttributeError, ValueError, OSError):
        return None
    return v or None


def version_tuple(text):
    """(2, 34) from 'glibc 2.34'; None when it does not read that way."""
    try:
        return tuple(int(p) for p in text.split()[1].split('.')[:2])
    except (AttributeError, IndexError, ValueError):
        return None


def build_parts(build):
    """(version, release) from 'glibc-2.34-275.el9_8.x86_64', or None.

    `rpm -q glibc` prints one such word per installed architecture. None when
    there is no build, when a word does not read that way, or when two words
    disagree: then the build cannot be ordered, and the comparison says so.
    """
    if not isinstance(build, str):
        return None
    parts = set()
    for word in build.split():
        m = re.fullmatch(r'glibc-([^-]+)-(.+)\.[A-Za-z0-9_]+', word)
        if not m:
            return None
        parts.add(m.groups())
    return parts.pop() if len(parts) == 1 else None


def rpm_vercmp(a, b):
    """rpm's own order of two version or release strings: -1, 0 or 1.

    A port of rpmvercmp() (rpmio/rpmvercmp.c): segments of digits or of
    letters, compared in turn; digits by value and newer than letters; '~'
    before everything and '^' after the base version. Checked against rpm
    itself (rpm.labelCompare) on rpm 4.14.3, 4.16.1.3 and 4.19.1.1: every
    pair of 55, 46 and 45 release strings, the glibc builds the repositories
    offered among them, with no disagreement.
    """
    if a == b:
        return 0
    letters, digits = frozenset(string.ascii_letters), frozenset(string.digits)
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and a[i] not in letters | digits and a[i] not in '~^':
            i += 1
        while j < len(b) and b[j] not in letters | digits and b[j] not in '~^':
            j += 1
        ca, cb = a[i:i + 1], b[j:j + 1]
        if ca == '~' or cb == '~':
            if ca != '~':
                return 1
            if cb != '~':
                return -1
            i, j = i + 1, j + 1
            continue
        if ca == '^' or cb == '^':
            if not ca:
                return -1
            if not cb:
                return 1
            if ca != '^':
                return 1
            if cb != '^':
                return -1
            i, j = i + 1, j + 1
            continue
        if not (ca and cb):
            break
        kind = digits if ca in digits else letters
        si, sj = i, j
        while i < len(a) and a[i] in kind:
            i += 1
        while j < len(b) and b[j] in kind:
            j += 1
        sa, sb = a[si:i], b[sj:j]
        if not sb:
            return 1 if kind is digits else -1
        if kind is digits:
            sa, sb = sa.lstrip('0'), sb.lstrip('0')
            if len(sa) != len(sb):
                return 1 if len(sa) > len(sb) else -1
        if sa != sb:
            return 1 if sa > sb else -1
    if i >= len(a) and j >= len(b):
        return 0
    return -1 if i >= len(a) else 1


def glibc_build():
    """The package build, e.g. 'glibc-2.34-275.el9_8.x86_64', or None.

    Two builds of one glibc version are two measurements: a distro patch
    can sit between them, as one did inside RHEL8 at glibc-2.28-93.
    """
    try:
        p = subprocess.run(['rpm', '-q', 'glibc'], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    return ' '.join(p.stdout.split()) or None


def list_locales():
    try:
        p = subprocess.run(['locale', '-a'], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    except OSError as e:
        die(f'cannot run `locale -a`: {e}')
    if p.returncode != 0:
        die(f'`locale -a` failed with exit {p.returncode}: '
            + p.stderr.decode('utf-8', 'replace').strip())
    names = sorted(set(p.stdout.decode('utf-8', 'surrogateescape').split()))
    if not names:
        die('`locale -a` listed no locale')
    return names


def read_number(path):
    """The first number in a file of /sys/fs/cgroup; None for 'max' or unreadable."""
    try:
        with open(path) as f:
            word = f.read().split()[0]
        return int(word)
    except (OSError, IndexError, ValueError):
        return None


def cgroup_limits():
    """(bytes this process's cgroup still allows, CPUs its quota allows).

    Inside a container /proc/meminfo and os.cpu_count() report the host:
    measured, the fixtures see 128 CPUs. A limit set on the container, or on
    any cgroup above this process, is read here instead: the smallest along
    the path, on cgroup v2 (memory.max, memory.current, cpu.max) and v1
    (memory.limit_in_bytes, memory.usage_in_bytes, cpu.cfs_quota_us). Each
    is None when no limit is set or none can be read.
    """
    memory = cpus = None
    try:
        with open('/proc/self/cgroup') as f:
            lines = f.read().splitlines()
    except OSError:
        return None, None
    for line in lines:
        hierarchy, controllers, path = (line.split(':', 2) + ['', ''])[:3]
        if hierarchy == '0':
            bases = [('/sys/fs/cgroup', 'v2')]
        else:
            names = controllers.split(',')
            bases = [('/sys/fs/cgroup/' + controllers, 'v1')] if (
                'memory' in names or 'cpu' in names) else []
        for base, version in bases:
            here = os.path.normpath(base + '/' + path)
            while True:
                # Inside a v1 container the cgroup is often mounted as the
                # root, so a path that does not exist is walked up, not taken
                # as "no limit".
                if version == 'v2':
                    limit = read_number(here + '/memory.max')
                    used = read_number(here + '/memory.current') or 0
                    quota = read_number(here + '/cpu.max')
                    period = _cpu_max_period(here)
                else:
                    limit = read_number(here + '/memory.limit_in_bytes')
                    limit = limit if limit is not None and limit < 1 << 60 else None
                    used = read_number(here + '/memory.usage_in_bytes') or 0
                    quota = read_number(here + '/cpu.cfs_quota_us')
                    quota = quota if quota is not None and quota > 0 else None
                    period = read_number(here + '/cpu.cfs_period_us')
                if limit is not None:
                    left = max(0, limit - used)
                    memory = left if memory is None else min(memory, left)
                if quota and period:
                    allowed = max(1, -(-quota // period))
                    cpus = allowed if cpus is None else min(cpus, allowed)
                if here == base or not here.startswith(base):
                    break
                here = os.path.dirname(here)
    return memory, cpus


def _cpu_max_period(directory):
    """The period of cgroup v2's cpu.max ('quota period'), or None."""
    try:
        with open(directory + '/cpu.max') as f:
            return int(f.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None


def default_jobs():
    """One process per CPU this process may use, but no more than half the
    free memory holds.

    The machine measured is often a database server, and the other half is
    left to it. Inside a container the container's limits count, not the
    host's (cgroup_limits). Where free memory cannot be read at all, four.
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    memory, quota_cpus = cgroup_limits()
    if quota_cpus:
        cpus = min(cpus, quota_cpus)
    try:
        with open('/proc/meminfo') as f:
            fields = dict(line.split(':', 1) for line in f)
        available = int(fields['MemAvailable'].split()[0]) * 1024
    except (OSError, KeyError, ValueError, IndexError):
        available = None
    if memory is not None:
        available = memory if available is None else min(available, memory)
    if available is None:
        return min(cpus, 4)
    return max(1, min(cpus, available // 2 // PROCESS_BYTES))


def fork_pool(jobs):
    """A pool of measuring processes, each one a copy of this process.

    Python 3.14 changed the default on Linux to start each one fresh
    (forkserver), and a fresh process reads the script again from its file.
    Run as `python3 -`, the script arrives on stdin and there is no file.
    Measured with forkserver on Python 3.12, every process died and the pool
    reported them killed. Copies also inherit UTF8_BYTES. Python 3.6 has no
    mp_context, and forks already, the default on Linux until 3.14.
    """
    if sys.version_info >= (3, 7):
        return ProcessPoolExecutor(
            jobs, mp_context=multiprocessing.get_context('fork'))
    return ProcessPoolExecutor(jobs)


def measure(jobs):
    """This machine's measurement as (the text of its file, what that holds,
    seconds taken).

    Nothing goes to stdout here, so that --extract has packed everything
    before the first byte leaves. A run that dies while measuring or packing
    leaves an empty file.
    """
    global UTF8_BYTES
    version = libc_version()
    if version is None:
        die('this machine does not run glibc, and only glibc is measured here')
    names = list_locales()
    build = glibc_build()
    try:
        os.nice(10)   # below the database, if one runs here
    except OSError:
        pass
    # Flushed, and so is each line of progress: before 3.9, Python holds
    # stderr back when it is not a terminal. Measured on RHEL8 over ssh, a
    # line written at the start arrived with the last one, minutes later.
    sys.stderr.write(f'Measuring {len(names)} locale(s) on {build or version} '
                     f'with {jobs} process(es)...\n')
    sys.stderr.flush()
    start = time.time()
    UTF8_BYTES = [chr(c).encode('utf-8') for c in CODEPOINTS]
    results, order_data, pair_data, uses, identities = {}, {}, {}, {}, {}
    try:
        with fork_pool(jobs) as pool:
            measured = pool.map(measure_locale, names)
            for done, (name, result, data, use, ident) in enumerate(measured, 1):
                line = progress(done, len(names), time.time() - start)
                if line:
                    sys.stderr.write(line)
                    sys.stderr.flush()
                results[name] = result
                uses[name] = use
                identities[name] = ident
                if data:
                    order_data.setdefault(result['order'], data['order'])
                    pair_data.setdefault(result['pairs'], data['pairs'])

            def measure_tables(reps):
                sys.stderr.write(f'Measuring the character rules (LC_CTYPE) of '
                                 f'one locale for each of the {len(reps)} '
                                 f'distinct LC_CTYPE glibc loaded...\n')
                sys.stderr.flush()
                tables, begun = [], time.time()
                for done, r in enumerate(pool.map(ctype_tables, reps), 1):
                    line = progress(done, len(reps), time.time() - begun,
                                    'LC_CTYPE')
                    if line:
                        sys.stderr.write(line)
                        sys.stderr.flush()
                    tables.append(r)
                return tables
            ctype = ctype_part(identities, measure_tables)
            converter = converter_part(uses, pool.map)
        money = money_digits(names)
    except NotPutBack as e:
        die(f'{e}; nothing was written: run again with a smaller --jobs')
    except BrokenProcessPool:
        # A process killed from outside, most often for want of memory. The
        # pool reports it rather than waiting forever for its answer.
        die('a measuring process was killed before it finished, most likely '
            'for lack of memory; run again with a smaller --jobs')
    out = {
        'format': FORMAT,
        'parts': list(PARTS[FORMAT]),
        'corpus': corpus_digest(),
        'glibc_version': version,
        'glibc_build': build,
        'python': platform.python_version(),
        'locales_listed': len(names),
        'locales': results,
        'order_data': order_data,
        'pair_data': pair_data,
        # After the measuring processes, which each select a locale of
        # their own: this one's LC_MONETARY is put back as it was.
        'money': money,
        'postgres_libc': postgres_libc(),
        'postgres_use': uses,
        'ctype': ctype,
        'converter': converter,
    }
    out['content'] = content_digest(out)
    text = json.dumps(out, sort_keys=True) + '\n'
    return text, out, time.time() - start


def progress(done, total, seconds, what='locale(s)'):
    """The line that says how far a measurement has got, or None when this
    item starts no new tenth: a locale, or what says what else.

    One line a tenth, not one an item: a machine with every language pack
    lists some 870 locales, and some 80 distinct LC_CTYPE. None for the last
    item as well: the last line is finished()'s, written once both are done.
    "Done", not "measured": a locale that could not be measured is done
    too, and only finished() says how many were.
    """
    if done >= total or done * 10 // total == (done - 1) * 10 // total:
        return None
    return (f'  {done} of {total} {what} done '
            f'({done * 100 // total}%), {seconds:.0f} s so far\n')


def finished(out, seconds, size):
    """Report a measurement on stderr, once written; the exit status, 1 when
    some locale could not be measured, its order or its LC_CTYPE, or the
    converter of some encoding."""
    results = out['locales']
    failed = sorted(n for n, r in results.items() if 'error' in r)
    sys.stderr.write(f'Done in {seconds:.0f} s: '
                     f'{out["locales_listed"] - len(failed)} locale(s) measured, '
                     f'{len(failed)} could not be; '
                     f'{size / 1e6:.1f} MB written.\n')
    for n in failed:
        sys.stderr.write(f'  {shown(n)}: {results[n]["error"]}\n')
    unread = ctype_not_checked(out['ctype'])
    if unread:
        sys.stderr.write(f'!! The character rules (LC_CTYPE) of {len(unread)} '
                         f'locale(s) could not be measured:\n')
        for n in sorted(unread):
            sys.stderr.write(f'  {shown(n)}: {unread[n]}\n')
    unconverted = [cs for cs in CONVERTER_CODESETS
                   if out['converter'][cs][0] == 'not checked']
    if unconverted:
        sys.stderr.write(f'!! What glibc\'s converter gives could not be '
                         f'measured for {len(unconverted)} encoding(s):\n')
        for cs in unconverted:
            sys.stderr.write(f'  {cs}: {out["converter"][cs][3]}\n')
    for line in machine_libc_lines(out['postgres_libc']):
        sys.stderr.write(line + '\n')
    return 1 if failed or unread or unconverted else 0


def ctype_not_checked(part):
    """{name: why} of the locales glibc accepted whose LC_CTYPE was not
    measured: their data was not read, or their table could not be."""
    out = {}
    for name, entry in part['locales'].items():
        if entry[0] == 'not checked':
            out[name] = entry[1]
        elif entry[0] == 'table' and part['tables'][entry[1]][0] == 'not checked':
            out[name] = part['tables'][entry[1]][1]
    return out


def machine_libc_lines(p):
    """What the machine's own run says about its PostgreSQL processes, so
    that a check that could not look is fixed there and then."""
    out = []
    if p['replaced']:
        out.append(filled(
            '!! glibc was replaced on disk while this measured: what was '
            'measured may not be what this machine runs now; measure it '
            'again.'))
    if p['stale']:
        out.append(filled(
            f'!! {len(p["stale"])} PostgreSQL process(es) still run the glibc '
            'that was replaced on disk: what this machine measured is not '
            'what that PostgreSQL sorts with until it is restarted.'))
    if p['other']:
        out.append(filled(
            f'!! {len(p["other"])} PostgreSQL process(es) run another C '
            'library than the one measured here, as when PostgreSQL runs in '
            'a container: what this machine measured is not what that '
            'PostgreSQL sorts with.'))
    why = libc_why(p)
    if why:
        out.append(filled(
            f'!! PostgreSQL processes NOT checked: {why}. To check, run this '
            f'{ADVICE} (README, command 1.b).'))
    if not out and not p['processes']:
        out.append(filled(
            f'!! No process named {" or ".join(p["looked_for"])} was found, '
            'so nothing checked which C library PostgreSQL runs here.'))
    if not out:
        out.append(f'PostgreSQL processes: {libc_clean(p)}.')
    return out


def libc_why(p):
    """Why the PostgreSQL processes of a machine were not all checked, or
    None when every one was."""
    why = [p['not_checked']] if p['not_checked'] else []
    if p['unreadable']:
        why.append(f'{p["unreadable"]} of {p["processes"]} process(es) named '
                   f'{" or ".join(p["looked_for"])} could not be read')
    if p['no_libc']:
        why.append(f'{p["no_libc"]} of {p["processes"]} process(es) named '
                   f'{" or ".join(p["looked_for"])} show no C library')
    if p['unnamed']:
        why.append(f'the name of {p["unnamed"]} other process(es) could not '
                   'be read')
    return '; '.join(why) or None


def libc_clean(p):
    """The line for a machine whose PostgreSQL processes were all read and
    all run the C library measured there."""
    return (f'{p["processes"]} process(es) named '
            f'{" or ".join(p["looked_for"])}, all running the C library '
            'measured there')


ADVICE = ('as root, or as the user PostgreSQL runs as where /proc does not '
          'hide other users\' processes')


def source_entries(directory):
    """Every entry under directory as (path relative to it, full path),
    parents first and each level sorted.

    A link is an entry and is not followed, as tar keeps it; the node checks
    that read the copy report links and folders rather than compare them.
    Raises OSError on a folder it cannot list. A folder skipped here would be
    missing from the copy, and read on the other side as one the distro does
    not ship.
    """
    entries = []

    def walk(rel):
        for name in sorted(os.listdir(os.path.join(directory, rel))):
            path = os.path.join(rel, name)
            full = os.path.join(directory, path)
            entries.append((path, full))
            if os.path.isdir(full) and not os.path.islink(full):
                walk(path)

    walk('')
    return entries


def on_disk(directory):
    """What a folder holds, read from the disk, in the words of a manifest:
    {path relative to it: 'file sha256:...', 'link to ...', 'folder' or
    'other'}. Walked as source_entries walks it."""
    out = {}
    for rel, full in source_entries(directory):
        mode = os.lstat(full).st_mode
        if stat.S_ISLNK(mode):
            out[rel] = 'link to ' + os.readlink(full)
        elif stat.S_ISDIR(mode):
            out[rel] = 'folder'
        elif stat.S_ISREG(mode):
            with open(full, 'rb') as f:
                out[rel] = 'file sha256:' + hashlib.sha256(f.read()).hexdigest()
        else:
            out[rel] = 'other'
    return out


def pack_sources(tar, directory, prefix='locales/'):
    """Add the files of directory under `prefix` in tar, and return their
    manifest entry: the locale sources under 'locales/', the charmaps under
    'charmaps/'.

    All of them or none. When none, the entry says why, so that a copy is
    never handed on with a file missing. Owners and times are dropped, so
    that two runs over the same files write the same bytes.
    """
    if not os.path.isdir(directory):
        return {'directory': directory, 'not_included':
                f'{directory} does not exist; it comes with the package '
                'glibc-locale-source'}
    members, listed = [], {}
    try:
        for rel, full in source_entries(directory):
            ti = tar.gettarinfo(full, prefix + rel)
            if ti is None:
                raise OSError(f'{full} is a socket, which tar cannot hold')
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, '', ''
            if ti.islnk():
                # A second name of a file packed above. Kept as a file of its
                # own, so that every name carries its content.
                ti.type, ti.linkname = tarfile.REGTYPE, ''
                ti.size = os.lstat(full).st_size
            data = None
            if ti.isfile():
                with open(full, 'rb') as f:
                    data = f.read()
                if len(data) != ti.size:
                    raise OSError(f'{full} changed while it was read')
                listed[rel] = 'file sha256:' + hashlib.sha256(data).hexdigest()
            elif ti.issym():
                listed[rel] = 'link to ' + ti.linkname
            elif ti.isdir():
                listed[rel] = 'folder'
            else:
                listed[rel] = 'other'
            members.append((ti, data))
    except OSError as e:
        return {'directory': directory,
                'not_included': f'{directory} could not be read whole: {e}'}
    if not listed:
        return {'directory': directory,
                'not_included': f'{directory} is empty'}
    for ti, data in members:
        tar.addfile(ti, io.BytesIO(data) if data is not None else None)
    return {'directory': directory, 'entries': listed}


def generated_member(name, data):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
    return ti, io.BytesIO(data)


def archive(text, out, directory=LOCALE_SOURCES, charmaps=None):
    """The file of --extract as (its bytes, the manifest written last in it).

    The measurement first, exactly the file a plain run writes; then the
    locale sources; then the charmaps; then extract.json, which describes
    them and is written last, so a file cut short lacks it. gzip without a
    name or a time, so that two runs on one machine write the same bytes.
    extract() passes CHARMAPS; with charmaps=None the manifest has no entry
    for them, as a file written before they were packed has none, and a
    test that builds a file never reads the charmaps of the machine it runs
    on.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=buf, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w',
                          format=tarfile.PAX_FORMAT) as tar:
            measurement = text.encode('ascii')
            tar.addfile(*generated_member('locale_order.out', measurement))
            manifest = {
                'format': EXTRACT_FORMAT,
                'glibc_version': out['glibc_version'],
                'glibc_build': out['glibc_build'],
                'measurement': {
                    'member': 'locale_order.out',
                    'sha256': hashlib.sha256(measurement).hexdigest()},
                'locale_sources': pack_sources(tar, directory),
            }
            if charmaps is not None:
                manifest['charmaps'] = pack_sources(tar, charmaps,
                                                    'charmaps/')
            tar.addfile(*generated_member(
                'extract.json',
                (json.dumps(manifest, sort_keys=True, indent=1) + '\n')
                .encode('ascii')))
    return buf.getvalue(), manifest


def extract(jobs):
    """Measure, pack, and write the one file of --extract to stdout."""
    if sys.stdout.isatty():
        die('--extract writes a compressed file, not text; send it to a file '
            '(python3 locale_order.py --extract > old.tar)')
    text, out, seconds = measure(jobs)
    data, manifest = archive(text, out, LOCALE_SOURCES, CHARMAPS)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    status = finished(out, seconds, len(data))
    sources = manifest['locale_sources']
    if 'entries' in sources:
        files = sum(1 for v in sources['entries'].values()
                    if v.startswith('file '))
        others = len(sources['entries']) - files
        sys.stderr.write(f'Locale sources: {files} file(s) from '
                         f'{sources["directory"]}'
                         + (f', and {others} link(s) or folder(s)'
                            if others else '') + '.\n')
    else:
        sys.stderr.write(f'!! Locale sources NOT included: '
                         f'{sources["not_included"]}.\n')
    charmaps = manifest['charmaps']
    if 'entries' in charmaps:
        sys.stderr.write(f'Charmaps: {len(charmaps["entries"])} entr(ies) '
                         f'from {charmaps["directory"]}.\n')
    else:
        sys.stderr.write(f'!! Charmaps NOT included: '
                         f'{charmaps["not_included"]}. The character sets '
                         'its locales are built with cannot be compared.\n')
    return status


def unpack(path, dest):
    """--unpack: check a file --extract wrote, and lay it out in dest.

    dest must not exist. What audit.sh reads there: locale_order.out, the
    measurement; build-id, the glibc build; either locales/, the sources, or
    sources-not-included, the reason they are missing; and either charmaps/
    or charmaps-not-included, the same for the charmaps. Every entry must be
    the one the manifest describes, so a file cut short, edited, or with text
    in front of it stops here with the reason, and nothing is laid out. What
    is written is then read back and held to the manifest too.
    """
    incomplete = (f'{path} is not a whole file written by --extract. A run '
                  'that was cut short leaves such a file, and so does text '
                  'the machine printed before it, such as a login message; '
                  'extract again')
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        die(f'cannot read {path}: {e}')
    try:
        # One gzip member and nothing after it. gzip.decompress would join a
        # second file written after the first (`>>` over an old one), and tar
        # would then read the first and never say the second was there.
        gz = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = gz.decompress(raw) + gz.flush()
        if not gz.eof or gz.unused_data:
            die(incomplete)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as tar:
            listed = tar.getmembers()
            # tar stops at its end marker, and at a block it cannot read,
            # without a word: past that point there must be nothing but the
            # zeros that pad the end.
            if data[tar.offset:].strip(b'\0'):
                die(incomplete)
            members = [(m, tar.extractfile(m).read() if m.isfile() else None)
                       for m in listed]
    except (OSError, EOFError, zlib.error, tarfile.TarError):
        die(incomplete)
    names = [m.name for m, _ in members]
    if (len(names) < 2 or names[0] != 'locale_order.out'
            or names[-1] != 'extract.json' or not members[0][0].isfile()
            or not members[-1][0].isfile()):
        die(incomplete)
    try:
        manifest = json.loads(members[-1][1].decode('ascii'))
    except ValueError:
        die(incomplete)
    if not isinstance(manifest, dict) or manifest.get('format') != EXTRACT_FORMAT:
        die(f'{path} was not written by this version of --extract; extract '
            'again with this checkout\'s scripts/locale_order.py')

    def refuse(what):
        die(f'{path} does not match its own manifest: {what}; extract again')

    measurement = members[0][1]
    described = manifest.get('measurement')
    if not isinstance(described, dict) or hashlib.sha256(
            measurement).hexdigest() != described.get('sha256'):
        refuse('the measurement is not the one it describes')
    build = manifest.get('glibc_build')
    if not isinstance(build, str) or not build.strip():
        die(f'{path} names no glibc build: rpm -q glibc printed none on that '
            'machine, and a result has to name the build it ran on')
    try:
        measured_on = json.loads(measurement.decode('ascii'))
    except ValueError:
        refuse('the measurement does not read')
    if not isinstance(measured_on, dict) or (
            measured_on.get('glibc_build'), measured_on.get('glibc_version')) != (
            build, manifest.get('glibc_version')):
        refuse('its build is not the one the measurement names')
    sources = manifest.get('locale_sources')
    entries = sources.get('entries') if isinstance(sources, dict) else None
    missing = sources.get('not_included') if isinstance(sources, dict) else None
    # A reason with nothing printable in it is none: audit.sh reads it with
    # $(cat), which drops newlines and NUL bytes and can leave nothing, and
    # a summary block with nothing to say about a side reads as a side with
    # nothing to report.
    if not ((isinstance(entries, dict) and entries and missing is None)
            or (_says_why(missing) and entries is None)):
        refuse('it says neither which locale sources it holds nor why it '
               'holds none')
    # The charmaps (13.7 PR D). A file written before --extract packed them
    # has no entry: steps 6 to 8 then say the character sets were not
    # compared, and why, rather than refuse a file that is whole.
    if 'charmaps' in manifest:
        charmaps = manifest['charmaps']
        cm_entries = (charmaps.get('entries') if isinstance(charmaps, dict)
                      else None)
        cm_missing = (charmaps.get('not_included')
                      if isinstance(charmaps, dict) else None)
        if not ((isinstance(cm_entries, dict) and cm_entries
                 and cm_missing is None)
                or (_says_why(cm_missing) and cm_entries is None)):
            refuse('it says neither which charmaps it holds nor why it holds '
                   'none')
    else:
        cm_entries = None
        cm_missing = (f'the file was written by an --extract that did not '
                      f'pack the machine\'s charmaps ({CHARMAPS}); extract '
                      f'it again with this checkout\'s '
                      f'scripts/locale_order.py')
    groups = {'locales/': [], 'charmaps/': []}
    for m, content in members[1:-1]:
        prefix = m.name.split('/', 1)[0] + '/'
        if prefix not in groups:
            refuse(f'an entry, {m.name!r}, is outside locales/ and charmaps/')
        groups[prefix].append((m, content))
    check_entries(groups['locales/'], 'locales/', entries, refuse,
                  'its locale sources are not the ones it lists')
    check_entries(groups['charmaps/'], 'charmaps/', cm_entries, refuse,
                  'its charmaps are not the ones it lists')

    try:
        os.makedirs(dest)
    except OSError as e:
        die(f'cannot lay the file out in {dest}: {e}')
    with open(os.path.join(dest, 'locale_order.out'), 'wb') as f:
        f.write(measurement)
    with open(os.path.join(dest, 'build-id'), 'w') as f:
        f.write(build.strip() + '\n')
    for name, why in (('sources-not-included', missing),
                      ('charmaps-not-included', cm_missing)):
        if why is not None:
            with open(os.path.join(dest, name), 'w') as f:
                f.write(why + '\n')
    for prefix, listed in (('locales/', entries), ('charmaps/', cm_entries)):
        if listed is not None:
            lay_out(groups[prefix], prefix, os.path.join(dest, prefix[:-1]),
                    listed, path, dest)
    return 0


def _says_why(reason):
    """Is `reason` a text with something printable in it?"""
    return isinstance(reason, str) and any(
        c.isprintable() and not c.isspace() for c in reason)


def check_entries(packed, prefix, entries, refuse, differs):
    """Hold the members under `prefix` to the manifest's `entries`, or
    refuse with the reason. Each name once, even to a disk that does not
    tell case apart (macOS, Windows): there two names that differ only in
    case are one file, and the second would land on the first, or go
    through it if it is a link."""
    found, folded = {}, {}
    for m, content in packed:
        rel = m.name[len(prefix):]
        parts = rel.split('/')
        if not rel or any(p in ('', '.', '..') for p in parts):
            refuse(f'an entry, {m.name!r}, is outside {prefix}')
        if rel in found:
            refuse(f'{m.name} appears twice')
        if rel.casefold() in folded:
            refuse(f'{prefix}{folded[rel.casefold()]} and {m.name} differ '
                   'only in case')
        folded[rel.casefold()] = rel
        parent = '/'.join(parts[:-1])
        if parent and found.get(parent, '').startswith('link '):
            refuse(f'{m.name} lies under a link')
        if parent and found.get(parent) != 'folder':
            refuse(f'{m.name} comes before, or without, the folder that '
                   'holds it')
        if m.isfile():
            found[rel] = 'file sha256:' + hashlib.sha256(content).hexdigest()
        elif m.issym():
            found[rel] = 'link to ' + m.linkname
        elif m.isdir():
            found[rel] = 'folder'
        else:
            refuse(f'{m.name} is not a file, a folder or a link')
    if found != (entries or {}):
        refuse(differs)


def lay_out(packed, prefix, root, entries, path, dest):
    """Write the members under `prefix` into root, then read root back and
    hold it to the manifest."""
    try:
        os.mkdir(root)
        for m, content in packed:
            target = os.path.join(root, m.name[len(prefix):])
            # Every folder above an entry is one made here, before it
            # (checked above). Each entry is made new: nothing that exists
            # is reused, and no link is followed, so none lands outside dest.
            if m.isdir():
                os.mkdir(target)
            elif m.issym():
                os.symlink(m.linkname, target)
            else:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o644)
                with os.fdopen(fd, 'wb') as f:
                    f.write(content)
        # Read back from the disk and held to the manifest: a write that went
        # wrong would otherwise reach steps 6 to 10 as the machine's files.
        written = on_disk(root)
    except OSError as e:
        die(f'cannot lay the file out in {dest}: {e}')
    if written != entries:
        die(f'what was written to {root} is not what {path} lists, so it '
            'cannot stand for that machine\'s files')


def shown(name):
    """A locale name that prints on any terminal, whatever bytes it holds."""
    return name.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def load(path, digest):
    """One machine's measurement, refused unless it is whole."""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        die(f'cannot read {path}: {e}')
    try:
        data = json.loads(raw.decode('utf-8'))
    except ValueError:
        die(f'{path} is not a complete output of locale_order.py. A run that '
            'was cut short leaves exactly such a file; measure again.')
    if not isinstance(data, dict) or data.get('format') not in PARTS:
        die(f'{path} is not an output of this version of locale_order.py')
    held = PARTS[data['format']]
    # A file of the first format has no list; one of a later format lists
    # what its format holds, and holds each of it.
    if 'parts' in data or len(held) > 1:
        if data.get('parts') != list(held):
            die(f'{path} does not list the parts its format holds; it is not a '
                'whole measurement')
    for part in SIDE_PARTS:
        if (part in data) != (part in held):
            die(f'{path} lacks a part its format holds, or holds one it does '
                'not; it is not a whole measurement')
    if data.get('corpus') != digest:
        die(f'{path} was measured over a different set of strings than this '
            'script uses; measure both machines with the same copy of it')
    locales = data.get('locales')
    if not isinstance(locales, dict) or len(locales) != data.get('locales_listed'):
        die(f'{path} holds a different number of locales than its machine '
            'listed; it is not a whole measurement')
    order_data, pair_data = data.get('order_data'), data.get('pair_data')
    if not isinstance(order_data, dict) or not isinstance(pair_data, dict):
        die(f'{path} holds no measured orders; it is not a whole measurement')
    for name, r in locales.items():
        # A measurement, or an error that says why there is none. An error
        # with nothing to say is neither, and the report would stop half-way
        # through trying to print it.
        if not isinstance(r, dict) or not (
                ('error' in r and isinstance(r['error'], str) and r['error'])
                or ('error' not in r and 'order' in r and 'pairs' in r)):
            die(f'{path}: the entry for {shown(name)} is malformed')
        # The names PostgreSQL gives a locale that could not be measured are
        # built from its codeset, so one that is not text would stop the
        # comparison with a traceback instead of a refusal.
        if 'error' in r and 'codeset' in r and not (
                isinstance(r['codeset'], str) and r['codeset']):
            die(f'{path}: the entry for {shown(name)} is malformed')
        if 'error' not in r and not all(
                k in r for k in ('codeset', 'chars', 'corpus', 'probes', 'partly')):
            die(f'{path} is not an output of this version of locale_order.py')
        if 'error' not in r and not (
                all(isinstance(r[k], str) for k in ('order', 'pairs', 'corpus',
                                                     'probes', 'codeset'))
                and all(type(r[k]) is int for k in ('chars', 'partly'))):
            die(f'{path}: the entry for {shown(name)} is not of this version of '
                'locale_order.py, or is malformed')
        if 'error' not in r and not (
                isinstance(order_data.get(r['order']), str)
                and isinstance(pair_data.get(r['pairs']), str)):
            die(f'{path}: what was measured for {shown(name)} is missing; it '
                'is not a whole measurement')
    if 'money' in held and not (
            isinstance(data['money'], dict) and set(data['money']) == set(locales)
            and all((type(v) is int and -128 <= v <= 127)
                    or (isinstance(v, str) and v) for v in data['money'].values())):
        die(f'{path}: the decimals of money are not one per locale; it is not '
            'a whole measurement')
    if 'postgres_libc' in held and not libc_part_whole(data['postgres_libc']):
        die(f'{path}: what it says of the PostgreSQL processes is malformed; '
            'it is not a whole measurement')
    if 'postgres_use' in held and not use_part_whole(data['postgres_use'],
                                                     locales):
        die(f'{path}: what it says of how PostgreSQL uses each locale is '
            'malformed; it is not a whole measurement')
    if 'ctype' in held and not ctype_part_whole(data['ctype'], locales):
        die(f'{path}: what it says of the character rules (LC_CTYPE) of each '
            'locale is malformed; it is not a whole measurement')
    if 'converter' in held and not converter_part_whole(data['converter'],
                                                        data['postgres_use']):
        die(f'{path}: what it says of the converter of each encoding is '
            'malformed; it is not a whole measurement')
    if 'converter' in held:
        other = [cs for cs in CONVERTER_CODESETS
                 if data['converter'][cs][0] == 'measured'
                 and data['converter'][cs][4] != sequence_digest(cs)]
        if other:
            die(f'{path} measured the converter of {", ".join(other)} over '
                'other byte sequences than this script uses; measure both '
                'machines with the same copy of it')
    if 'content' not in data:
        die(f'{path} is not an output of this version of locale_order.py')
    if data['content'] != content_digest(data):
        die(f'{path} does not match the digest its machine wrote into it; the '
            'file was damaged after it was written')
    if 'postgres_use' in held:
        # measure_one selects LC_COLLATE and then LC_CTYPE (codeset_of), and
        # postgres_use reads the same nl_langinfo right after it in the same
        # process: a locale either refused, or one Python could not hand to
        # C, has no order of its own, and both read one encoding. A file
        # that says otherwise was measured by two glibcs.
        for name, r in data['postgres_use'].items():
            # measure_one keeps no encoding after either refusal (it returns
            # before codeset_of, or codeset_of gave None).
            if ('accepted' != r['collate'][0] or 'accepted' != r['ctype'][0]) \
                    and ('error' not in locales[name] or 'codeset' in locales[name]):
                # A name Python cannot pass to C stops measure_one with the
                # same ValueError, whatever glibc: no second glibc explains it.
                if 'unknown' in (r['collate'][0], r['ctype'][0]):
                    die(f'{path}: the entry for {shown(name)} says Python could '
                        'not pass its name to the C library, and holds the order '
                        'or the encoding measured for it; the file contradicts '
                        'itself, so measure that machine again')
                die(f'{path}: the entry for {shown(name)} says glibc refused '
                    'it, and holds the order or the encoding measured for it; '
                    'glibc changed while that machine was measured, so measure '
                    'it again')
            # Both accepted: codeset_of and postgres_use read nl_langinfo under
            # the same LC_CTYPE, one right after the other, so they read the
            # same encoding, or neither read one.
            order_codeset = locales[name].get('codeset')
            if ('accepted' == r['collate'][0] == r['ctype'][0]
                    and r['codeset'] != order_codeset):
                first = (f'reads its encoding as {order_codeset} for the order'
                         if order_codeset else
                         'could not read its encoding for the order')
                second = ('could not read it' if not r['codeset'] else
                          f'as {r["codeset"]}' if order_codeset else
                          f'read it as {r["codeset"]}')
                die(f'{path}: the entry for {shown(name)} {first} and {second} '
                    'for how PostgreSQL uses it; glibc changed while that '
                    'machine was measured, so measure it again')
    if 'ctype' in held:
        # ctype_identity copies postgres_use's answer for a name setlocale
        # did not accept: the two say the same, in the same process.
        for name, r in data['postgres_use'].items():
            entry = data['ctype']['locales'][name]
            if r['ctype'][0] != 'accepted' and entry != r['ctype']:
                die(f'{path}: the entry for {shown(name)} says glibc did not '
                    'accept it for LC_CTYPE, and its character rules say '
                    'otherwise; the file contradicts itself, so measure that '
                    'machine again')
    if version_tuple(data.get('glibc_version')) is None:
        die(f'{path} does not say which glibc it was measured on')
    parts = build_parts(data.get('glibc_build'))
    if parts and version_tuple('glibc ' + parts[0]) != version_tuple(data['glibc_version']):
        die(f'{path} says it was measured on {data["glibc_version"]} but names '
            f'the build {data["glibc_build"]}; a measurement is bound to one '
            'build, so measure that machine again')
    data['path'] = path
    data['held'] = held
    return data, raw


def use_part_whole(use, locales):
    """Is what a file says of how PostgreSQL uses each locale one entry per
    locale, each of the shape postgres_use() writes? A category accepted
    has no reason and one not accepted has one; an encoding is read only
    under an LC_CTYPE that was set, and a character is tried only with an
    encoding; a character is a code point and the bytes of more than one
    byte it was measured as, in lower-case hex."""
    def category(c):
        return (isinstance(c, list) and len(c) == 2 and c[0] in CATEGORY_STATES
                and ((c[0] == 'accepted' and c[1] is None)
                     or (c[0] != 'accepted' and isinstance(c[1], str) and c[1])))

    def text(t, codeset):
        if not (isinstance(t, list) and len(t) == 2 and t[0] in TEXT_STATES):
            return False
        state, detail = t
        if state == 'not checked':
            return isinstance(detail, str) and bool(detail)
        if codeset is None:
            return False
        if state == 'one byte':
            return detail is None
        return (isinstance(detail, list) and len(detail) == 2
                and type(detail[0]) is int
                and 0 < detail[0] < 0x110000 and not 0xD800 <= detail[0] <= 0xDFFF
                and isinstance(detail[1], str)
                and re.fullmatch(r'(?:[0-9a-f]{2}){2,}', detail[1]) is not None)

    def entry(r):
        return (isinstance(r, dict)
                and set(r) == {'collate', 'ctype', 'codeset', 'text'}
                and category(r['collate']) and category(r['ctype'])
                and (r['codeset'] is None
                     or (isinstance(r['codeset'], str) and r['codeset']))
                and (r['ctype'][0] == 'accepted' or r['codeset'] is None)
                and text(r['text'], r['codeset']))
    return (isinstance(use, dict) and set(use) == set(locales)
            and all(entry(r) for r in use.values()))


def ctype_part_whole(part, locales):
    """Is the ctype part of a file of the shape ctype_part() writes: one
    entry per locale, every table an entry names present, every table
    present named, and each one unpacking to data of its shape whose sha256
    is its key? A table that does not match its key is damage, never "the
    same"; content_digest catches damage too, but only of a file whose
    digest was not rewritten with it."""
    if not (isinstance(part, dict)
            and set(part) == {'locales', 'tables', 'wide', 'bytes'}
            and all(isinstance(part[k], dict) for k in part)
            and set(part['locales']) == set(locales)):
        return False
    named = set()
    for name, entry in part['locales'].items():
        if not (isinstance(entry, list) and len(entry) == 2
                and entry[0] in CTYPE_STATES and isinstance(entry[1], str)
                and entry[1]):
            return False
        if entry[0] == 'table':
            # glibc gives its own LC_CTYPE to the names C and POSIX and to no
            # other (_nl_find_locale, findlocale.c:131-137 in 2.39), and
            # loads data for every other name.
            if not (CTYPE_IDENTITY.fullmatch(entry[1])
                    and (entry[1] == 'builtin') == (name in ('C', 'POSIX'))):
                return False
            named.add(entry[1])
    # Every table an entry names is present, and every one present is named.
    if set(part['tables']) != named:
        return False
    wide, byte = set(), set()
    for t in part['tables'].values():
        if not (isinstance(t, list) and len(t) == 2
                and all(isinstance(x, str) and x for x in t)):
            return False
        if t[0] == 'not checked':
            continue
        wide.add(t[0])
        byte.add(t[1])
    # Every table named is present, and every one present is named.
    if set(part['wide']) != wide or set(part['bytes']) != byte:
        return False
    return (all(ctype_unpacked(part['wide'], k, wide_whole) is not None
                for k in wide)
            and all(ctype_unpacked(part['bytes'], k, bytes_whole) is not None
                    for k in byte))


def ctype_unpacked(tables, key, whole):
    """The raw bytes of tables[key], when they unpack, their sha256 is the
    key and whole() says they are of their kind's shape; else None."""
    packed = tables[key]
    if not isinstance(packed, str):
        return None
    try:
        raw = zlib.decompress(base64.b64decode(packed.encode('ascii'),
                                               validate=True))
    except (ValueError, zlib.error, UnicodeError):
        return None
    if hashlib.sha256(raw).hexdigest() != key or not whole(raw):
        return None
    return raw


def converter_part_whole(part, use):
    """Is the converter part of the shape converter_part() writes: one
    entry per encoding of CONVERTER_CODESETS, each asked under the first of
    its locales by name in postgres_use, or under none where there is none;
    iconv_open's answer of one of its forms; and each measured list one
    value per accepted sequence?"""
    if not (isinstance(part, dict) and set(part) == set(CONVERTER_CODESETS)):
        return False
    first = first_locales(use)
    for cs, e in part.items():
        if not (isinstance(e, list) and len(e) == 5 and e[0] in CONVERTER_STATES
                and isinstance(e[1], list) and len(e[1]) == 2
                and e[1][0] in ICONV_STATES
                and ((isinstance(e[1][1], str) and bool(e[1][1]))
                     if e[1][0] == 'fails' else e[1][1] is None)):
            return False
        # converter_of asks under the locale it was handed, and is handed
        # none only where the machine lists none of the encoding; with none,
        # it measures nothing, and says not checked only when glibc could
        # not be asked.
        if e[2] != first.get(cs):
            return False
        if e[0] == 'no locale' and e[2] is not None:
            return False
        if e[2] is None and not (e[0] == 'no locale' or e[1][0] == 'not asked'):
            return False
        if e[1][0] == 'not asked' and e[0] != 'not checked':
            return False
        if e[0] == 'no locale':
            if e[3] is not None or e[4] is not None:
                return False
        elif e[0] == 'not checked':
            if not (isinstance(e[3], str) and e[3] and e[4] is None):
                return False
        # Values measured over other sequences are not read here: load()
        # refuses them as such, whatever their number.
        elif not (isinstance(e[4], str) and SHA256.fullmatch(e[4])
                  and (e[4] != sequence_digest(cs)
                       or converter_unpacked(e[3], cs) is not None)):
            return False
    return True


def converter_unpacked(packed, codeset):
    """The values of a measured entry, one per accepted sequence of the
    encoding, each 0 or what mbrtowc wrote into a wchar_t (glibc's are
    of 31 bits); None when they do not unpack to that."""
    if not isinstance(packed, str):
        return None
    try:
        raw = zlib.decompress(base64.b64decode(packed.encode('ascii'),
                                               validate=True))
    except (ValueError, zlib.error, UnicodeError):
        return None
    got = unpack_order(raw)
    if (got is None or len(got) != len(accepted_sequences(codeset))
            or not all(0 <= c < 0x80000000 for c in got)):
        return None
    return got


def bytes_whole(raw):
    """toupper, tolower and one row of 0 and 1 per class, 256 bytes each."""
    return (len(raw) == CTYPE_BYTES
            and raw[512:].translate(None, b'\x00\x01') == b'')


def wide_whole(raw):
    """ctype_tables' JSON: each map a list of [code point, another value it
    maps to], ascending by code point, and each class of CTYPE_CLASSES a list
    of runs [first, last] of code points, ascending and apart. Every value
    is a code point of CODEPOINTS, which is what was measured, and no run
    holds a surrogate."""
    try:
        w = json.loads(raw.decode('ascii'))
    except ValueError:
        return False

    def point(c):
        return type(c) is int and 0 < c < 0x110000 and not 0xD800 <= c <= 0xDFFF

    def mapping(pairs):
        return (isinstance(pairs, list)
                and all(isinstance(p, list) and len(p) == 2 and point(p[0])
                        and point(p[1]) and p[0] != p[1] for p in pairs)
                and all(a[0] < b[0] for a, b in zip(pairs, pairs[1:])))

    def runs(rs):
        # A run never holds the surrogates: CODEPOINTS skips them, and
        # code_point_runs ends a run at the gap.
        return (isinstance(rs, list)
                and all(isinstance(r, list) and len(r) == 2 and point(r[0])
                        and point(r[1]) and r[0] <= r[1]
                        and not r[0] < 0xD800 < r[1] for r in rs)
                and all(a[1] + 1 < b[0] for a, b in zip(rs, rs[1:])))
    return (isinstance(w, dict) and set(w) == {'upper', 'lower', 'classes'}
            and mapping(w['upper']) and mapping(w['lower'])
            and isinstance(w['classes'], dict)
            and set(w['classes']) == set(CTYPE_CLASSES)
            and all(runs(w['classes'][k]) for k in CTYPE_CLASSES))


def libc_part_whole(p):
    """Is what a file says of its machine's PostgreSQL processes of the shape
    postgres_libc() writes, with counts that add up?"""
    counts = ('processes', 'unreadable', 'no_libc', 'unnamed')
    return (isinstance(p, dict)
            and set(p) == {'looked_for', 'stale', 'other',
                           'not_checked', 'replaced'} | set(counts)
            and type(p['replaced']) is bool
            and all(type(p[k]) is int and p[k] >= 0 for k in counts)
            and isinstance(p['looked_for'], list) and p['looked_for']
            and all(isinstance(n, str) and n for n in p['looked_for'])
            and (p['not_checked'] is None
                 or (isinstance(p['not_checked'], str) and p['not_checked']))
            and all(isinstance(p[k], list)
                    and all(isinstance(s, list) and len(s) == 2
                            and type(s[0]) is int and isinstance(s[1], str)
                            for s in p[k]) for k in ('stale', 'other'))
            and len(p['stale']) + len(p['other']) + p['unreadable']
            + p['no_libc'] <= p['processes'])


def order_text(seq):
    """The characters of an order as one string, built in C as UTF-32: three
    times faster than joining chr() of each. A number that is no character
    raises ValueError or OverflowError either way."""
    return array.array(CODE_POINT, seq).tobytes().decode(UTF32)


def unpacked(data, kind, digest, count=None, probes=None):
    """One measured order or told-apart record, checked against its digest.

    The digest was taken on the machine, so a copy damaged on the way cannot
    pass for the measurement. An order must also hold as many characters as
    its locale was measured over (count); a told-apart record's digest covers
    the pairs it was measured with (probes) as well, as measured() takes it.
    """
    try:
        raw = zlib.decompress(base64.b64decode(
            data[kind + '_data'][digest].encode('ascii'), validate=True))
        if kind == 'pair':
            if hashlib.sha256(probes.encode('utf-8') + b'\n' + raw).hexdigest() == digest:
                return raw
        else:
            seq = unpack_order(raw)
            if (seq is not None and len(seq) == count
                    and hashlib.sha256(order_text(seq).encode('utf-8'))
                    .hexdigest() == digest):
                return seq
    except (ValueError, OverflowError, zlib.error):
        # A damaged copy fails here, as a number out of range, bytes that do
        # not decompress, or text that is not base64.
        pass
    die(f'{data["path"]}: a measured {kind} record does not match its digest; '
        'the file was damaged after it was written')


def new_positions(old_seq, new_seq):
    """Where each character of the old order stands in the new one."""
    position = [0] * 0x110000
    for i, c in enumerate(new_seq):
        position[c] = i
    return [position[c] for c in old_seq]


def reordering(old_seq, new_seq):
    """(positions, old_at, steps, apart) of two orders of the same
    characters: where each character of the old order stands in the new one;
    where in the old order each place of the new one was; the step from each
    character's new place to the next one's; and 1 where that step is not 1,
    that is, where two neighbours of the old order are not neighbours, in
    that order, in the new one. Worked out once for every use."""
    positions = new_positions(old_seq, new_seq)
    old_at = [0] * len(positions)
    for i, p in enumerate(positions):
        old_at[p] = i
    steps = list(map(operator.sub, positions[1:], positions))
    return positions, old_at, steps, bytes(map((1).__ne__, steps))


def kept_in_order(positions, apart=None):
    """1 for each character of the old order that is among the most that
    kept their relative order: the longest increasing subsequence of their
    new positions.

    Patience sorting, with two shortcuts that leave the answer as it was:
    once a character goes on the end, the rest of its run of consecutive
    positions goes on after it, in one step; and the subsequence is read
    back a run at a time.
    """
    n = len(positions)
    tail_positions, tail_indexes = [], []
    previous = [-1] * n
    # 1 for an index appended right after the index before it, as part of a
    # run; its previous is that index.
    batched = bytearray(n)
    if apart is None:
        apart = bytes(map((1).__ne__, map(operator.sub, positions[1:], positions)))
    ends = apart + ONE
    last = -1           # tail_positions[-1]
    i = 0
    while i < n:
        p = positions[i]
        if p > last:
            k = len(tail_positions)
            j = ends.find(1, i) + 1
            tail_positions.extend(positions[i:j])
            tail_indexes.extend(range(i, j))
            previous[i] = tail_indexes[k - 1] if k else -1
            batched[i + 1:j] = ONE * (j - i - 1)
            last = positions[j - 1]
            i = j
            continue
        k = bisect.bisect_left(tail_positions, p)
        tail_positions[k] = p
        tail_indexes[k] = i
        if k == len(tail_positions) - 1:
            last = p
        previous[i] = tail_indexes[k - 1] if k else -1
        i += 1
    kept = bytearray(n)
    i = tail_indexes[-1] if tail_indexes else -1
    while i >= 0:
        if batched[i]:
            start = batched.rfind(0, 0, i)
            kept[start:i + 1] = ONE * (i - start + 1)
            i = previous[start]
        else:
            kept[i] = 1
            i = previous[i]
    return kept


def moved_characters(old_seq, new_seq):
    """The characters that moved: all but the most that kept their order.

    The characters that kept their relative order are the longest increasing
    subsequence of the new positions read in the old order; every other
    character is one that moved. A swap of two characters counts one of them,
    so where more than one subsequence is longest, which characters are
    called moved is a choice; in_a_reversed_pair() is not.
    """
    kept = kept_in_order(new_positions(old_seq, new_seq))
    return sorted(c for c, k in zip(old_seq, kept) if not k)


def in_a_reversed_pair(positions):
    """1 for each character of the old order that the two orders put on
    different sides of at least one other character, ties broken by the
    bytes as PostgreSQL breaks them.

    Unlike the moved characters, this does not depend on a choice: in ko_KR
    one character moved, and it and the 4,888 it moved past are each in such
    a pair. A character is in none exactly when every character before it in
    one order is before it in the other, which is what the two passes check:
    the largest new position before it, and the smallest after it.
    """
    n = len(positions)
    flag = bytearray(n)
    largest = -1
    for i, p in enumerate(positions):
        if largest > p:
            flag[i] = 1
        elif p > largest:
            largest = p
    smallest = n
    for i in range(n - 1, -1, -1):
        p = positions[i]
        if smallest < p:
            flag[i] = 1
        elif p < smallest:
            smallest = p
    return flag


def witnesses(old_seq, new_seq, moves, kept):
    """For each character that moved, one that kept its place and that the
    two orders put on the other side of it: the nearest such in the new
    order. There is always one, or the longest subsequence would not be the
    longest: a moved character that no kept character crossed could join
    it."""
    positions, old_at = moves[:2]
    kept_new = [p for p, k in zip(positions, kept) if k]   # increasing
    out = {}
    i = kept.find(0)
    while i >= 0:
        p = positions[i]
        j = bisect.bisect_left(kept_new, p)
        if j and old_at[kept_new[j - 1]] > i:
            out[old_seq[i]] = (new_seq[kept_new[j - 1]], '<')
        elif j < len(kept_new) and old_at[kept_new[j]] < i:
            out[old_seq[i]] = (new_seq[kept_new[j]], '>')
        else:
            raise ValueError(f'no kept character crosses U+{old_seq[i]:04X}')
        i = kept.find(0, i + 1)
    return out


def uncompared_levels(old_probes, new_probes):
    """The levels a told-apart record cannot be compared at: no pair measured
    them on one machine ('?'), or the pairs differ. A level shown not to exist
    on both ('none') is not one."""
    a, b = old_probes.split(' '), new_probes.split(' ')
    return tuple(k for k in range(3)
                 if a[k] != b[k] or '?' in (a[k], b[k]))


def comparable_levels(old_probes, new_probes):
    """The levels (0 letters, 1 accent, 2 case) measured with the same pair on
    both machines. Only there does a told-apart record mean the same thing on
    both sides; where the pairs differ, that is reported on its own."""
    a, b = old_probes.split(' '), new_probes.split(' ')
    return tuple(k for k in range(3)
                 if a[k] == b[k] and a[k] not in ('?', 'none'))


def relation_tables(levels):
    """Two bytes.translate tables over told-apart records. The first gives
    the relation over the levels compared: bit 0 when strcoll does not call
    the two equal, bit 1 + k when they differ at least as strongly as the
    pair of level k. The second gives 1 where that pair compared equal,
    which says nothing about the level, or where the record says the level
    had no pair, which a level compared on both machines had: either way two
    records that differ there are a pair the comparison cannot decide, never
    one it can drop."""
    rel, unsure = bytearray(256), bytearray(256)
    for v in range(256):
        r = 0 if v & 1 else 1
        for k in levels:
            f = (v >> (1 + 2 * k)) & 3
            if f == 0:
                r |= 2 << k
            elif f != 2:
                unsure[v] = 1
        rel[v] = r
    return bytes(rel), bytes(unsure)


def strength(rel):
    """0 tied, 1 told apart by less than every level compared, 2 + (2 - k)
    told apart at least as strongly as level k: higher is stronger."""
    if not rel & 1:
        return 0
    for k in range(3):
        if rel >> (1 + k) & 1:
            return 4 - k
    return 1


class Between:
    """How two characters of one order that are not neighbours are told
    apart, worked out from the neighbours between them: they tie only if
    every neighbour between them ties, and they differ at least as strongly
    as a level if some neighbour between them does.

    That holds for characters glibc weighs at every level, and is only
    trusted in one direction (level_changes): where glibc ignores a character
    at some level, a two-letter string compares the probe letter with the
    other character instead, and a strong neighbour between two characters
    does not make them differ strongly. Measured with strcoll on the three
    fixtures, 2026-10-18: a weaker relation worked out this way held for
    3,405 pairs of 3,405, a stronger one failed for 395 of 23,591.
    """

    def __init__(self, rel, unsure, bits):
        self.rel, self.unsure, self.bits = rel, unsure, bits
        self.counts = None

    def relation(self, i, j):
        """(relation, unsure) of the characters at positions i < j."""
        if j - i <= 64:
            r = 0
            for v in self.rel[i:j]:
                r |= v
            return r, 1 in self.unsure[i:j]
        if self.counts is None:
            self.counts = {}
            for b in self.bits:
                column = bytes((v >> b) & 1 for v in range(256))
                self.counts[b] = [0] + list(itertools.accumulate(
                    self.rel.translate(column)))
            self.counts['unsure'] = [0] + list(itertools.accumulate(self.unsure))
        r = 0
        for b in self.bits:
            if self.counts[b][j] - self.counts[b][i]:
                r |= 1 << b
        return r, self.counts['unsure'][j] - self.counts['unsure'][i] > 0


def level_changes(old_seq, old_told, new_seq, new_told, levels, moves=None):
    """Pairs told apart at a different level on the two machines.

    ([(side, first, last, old relation, new relation)], [(x, y)]): the
    changes, as runs of pairs that follow one another in one order (pair i
    is the i-th character of that order and the one after it; side 'both'
    and 'old' count in the old order, 'new' in the new one) and change alike;
    then the pairs whose change the measurement cannot decide. moves is
    reordering() of the two orders, or None when they are the same.

    A pair of neighbours in both orders is compared record to record, over
    the levels compared. A pair of neighbours in one order only, kept in the
    same order by the other, is compared with what the characters between
    them say there (Between); that is a change only when the other machine
    tells them apart more weakly, the one direction that reading can be
    trusted in. The other direction goes to the second list. So the ten Oriya
    digits of or_IN, RHEL 8 -> RHEL 9, are named: each was a neighbour of its
    ASCII digit, told apart like an accent, and on RHEL 9 seven zeros of other
    scripts stand between them, each told apart from the next by less than
    case ("0A" against U+0B66 "a" turns round; strcoll on the fixtures).
    """
    rel_table, unsure_table = relation_tables(levels)
    old_rel, old_unsure = old_told.translate(rel_table), old_told.translate(unsure_table)
    new_rel, new_unsure = new_told.translate(rel_table), new_told.translate(unsure_table)
    bits = (0,) + tuple(1 + k for k in levels)
    runs, undecided = [], []

    def change(side, i, before, now):
        last = runs[-1] if runs else None
        if (last and last[0] == side and last[2] == i - 1
                and last[3] == before and last[4] == now):
            last[2] = i
        else:
            runs.append([side, i, i, before, now])

    def weigh(seq, i, mine, mine_unsure, theirs, theirs_unsure, side):
        # theirs & ~1: a level worked out between, which only says "at least
        # this strongly" when every character between is weighed at it; the
        # real relation can be weaker, a change. Measured with strcoll on the
        # fixtures: 23 of 1,977 such equal readings were changes.
        if mine_unsure or theirs_unsure:
            undecided.append((seq[i], seq[i + 1]))
        elif strength(theirs) < strength(mine):
            change(side, i, *((mine, theirs) if side == 'old' else (theirs, mine)))
        elif theirs != mine or theirs & ~1:
            undecided.append((seq[i], seq[i + 1]))

    if moves is None:
        aligned, aligned_rel, aligned_unsure = new_told, new_rel, new_unsure
        positions = old_at = steps = None
        moved_apart = bytes(len(old_rel))
    else:
        positions, old_at, steps, moved_apart = moves
        # The new record from each character to the one after it there; the
        # last character has none, and its pair is never a neighbour pair.
        aligned = bytes(map((new_told + bytes(1)).__getitem__, positions[:-1]))
        aligned_rel = aligned.translate(rel_table)
        aligned_unsure = aligned.translate(unsure_table)
    # Pairs of neighbours in both orders whose record differs, and neither
    # record holds an equal answer, are found as runs: each byte the two
    # records side by side, or 0, and a run of one same byte is one row.
    size = len(old_rel)
    old_n, aligned_n = int.from_bytes(old_rel, 'big'), int.from_bytes(aligned_rel, 'big')
    unsure_n = int.from_bytes(old_unsure, 'big') | int.from_bytes(aligned_unsure, 'big')
    changed_n = int.from_bytes((old_n ^ aligned_n).to_bytes(size, 'big').translate(NONZERO), 'big')
    apart_n = int.from_bytes(moved_apart, 'big')
    both_n = changed_n & ~apart_n & ~unsure_n
    # Every record is below 16, so shifting the whole by four bits moves each
    # byte into its own upper half; a byte of 0 or 1 times 255 is 0 or 255.
    key = (((old_n << 4) | aligned_n) & (both_n * 255)).to_bytes(size, 'big')
    for m in SAME_BYTE_RUNS.finditer(key):
        runs.append(['both', m.start(), m.end() - 1, key[m.start()] >> 4,
                     key[m.start()] & 15])
    # The rest are looked at one by one: pairs that stopped being neighbours,
    # and neighbours whose records differ at all, the raw bytes, while one
    # holds an answer that cannot be compared. The relation of two such
    # records can read the same where the bytes do not; they are undecided.
    raw_n = int.from_bytes(old_told, 'big') ^ int.from_bytes(aligned, 'big')
    raw_changed_n = int.from_bytes(raw_n.to_bytes(size, 'big').translate(NONZERO), 'big')
    mark = (apart_n | (raw_changed_n & unsure_n)).to_bytes(size, 'big')
    new_between = old_between = None
    i = mark.find(1)
    while i >= 0:
        if steps is None or steps[i] == 1:
            undecided.append((old_seq[i], old_seq[i + 1]))
        elif steps[i] > 0:
            if new_between is None:
                new_between = Between(new_rel, new_unsure, bits)
            a = positions[i]
            theirs, theirs_unsure = new_between.relation(a, a + steps[i])
            weigh(old_seq, i, old_rel[i], old_unsure[i], theirs, theirs_unsure,
                  'old')
        i = mark.find(1, i + 1)
    runs.sort(key=lambda r: r[1])
    if steps is not None:
        apart = bytes(map((1).__lt__, map(operator.sub, old_at[1:], old_at)))
        j = apart.find(1)
        while j >= 0:
            if old_between is None:
                old_between = Between(old_rel, old_unsure, bits)
            a, b = old_at[j], old_at[j + 1]
            theirs, theirs_unsure = old_between.relation(a, b)
            weigh(new_seq, j, new_rel[j], new_unsure[j], theirs, theirs_unsure,
                  'new')
            j = apart.find(1, j + 1)
    return runs, undecided


def glyph(cp):
    """The character itself, where it is visible and this terminal can print it."""
    ch = chr(cp)
    if unicodedata.category(ch)[0] not in 'LNPS':
        return ''
    try:
        ch.encode(sys.stdout.encoding or 'ascii')
    except (UnicodeError, LookupError):
        return ''
    return ch


def character(cp):
    """'W (U+0057 LATIN CAPITAL LETTER W)'."""
    name = unicodedata.name(chr(cp), '')
    detail = f'U+{cp:04X} {name}' if name else f'U+{cp:04X}'
    g = glyph(cp)
    return f'{g} ({detail})' if g else detail


def short_character(cp):
    """'W (U+0057)'."""
    g = glyph(cp)
    return f'{g} (U+{cp:04X})' if g else f'U+{cp:04X}'


def without_codeset(name):
    """The name with its encoding part removed: 'sv_FI.iso885915@euro' ->
    'sv_FI@euro'. The rule of normalize_libc_locale_name in PostgreSQL's
    collationcmds.c (the same at REL_14_24 and REL_18_6)."""
    return re.sub(r'\.[A-Za-z0-9-]*', '', name)


def postgres_names(locales):
    """For each locale, the other names PostgreSQL may know it by.

    {locale: (collation alias or None, database locale spelling or None)}.
    Importing the system locales, PostgreSQL also creates each one under its
    name without the encoding part, for the locale's own encoding, unless a
    collation of that name already exists for that encoding or for every
    encoding (C, POSIX, default); candidates are taken in the order of their
    locale names and the first wins. So in a UTF-8 database "ko_KR" is
    ko_KR.utf8, while `locale -a` gives the name ko_KR to a different,
    EUC-KR locale, and in a LATIN9 database "sv_SE" is sv_SE.iso885915.
    PostgreSQL skips a locale whose encoding it cannot hold (TIS-620, BIG5);
    the alias computed for one names a database that cannot exist, which
    misleads nobody.

    A database's own locale is written as initdb was given it, most often
    with the encoding spelled as glibc prints it (ko_KR.UTF-8); glibc reads
    that the same as ko_KR.utf8, since it compares encoding names in lower
    case without punctuation (_nl_normalize_codeset). That spelling is given
    for every name that carries an encoding part.
    """
    by_codeset = {}
    for n, r in locales.items():
        if r.get('codeset'):
            by_codeset.setdefault(r['codeset'], []).append(n)
    names = {}
    for codeset, group in by_codeset.items():
        taken = set(group) | {'C', 'POSIX', 'default'}
        for n in sorted(group):
            alias = without_codeset(n)
            if alias != n and alias not in taken:
                taken.add(alias)
            else:
                alias = None
            head, at, modifier = n.partition('@')
            base, dot, _ = head.partition('.')
            spelling = base + '.' + codeset + at + modifier if dot else None
            names[n] = (alias, spelling)
    return names


def heading(names, o, n):
    """The locale names of one change, each with its encoding."""
    by_codeset = {}
    for name in sorted(names):
        before, now = o[name].get('codeset'), n[name].get('codeset')
        if before == now:
            label = before or 'encoding unknown'
        else:
            label = f'{before or "encoding unknown"} before, {now or "unknown"} now'
        by_codeset.setdefault(label, []).append(shown(name))
    return '; '.join(f'{", ".join(group)} ({label})'
                     for label, group in sorted(by_codeset.items()))


def also_known_as(names, pg_names, locales):
    """What else a PostgreSQL user may call the locales of one change."""
    aliases, spellings = {}, []
    for name in sorted(names):
        if name in pg_names:
            alias, spelling = pg_names[name]
            if alias:
                aliases.setdefault(locales[name]['codeset'], []).append(f'"{alias}"')
            if spelling and spelling not in names and spelling not in spellings:
                spellings.append(spelling)
    parts = []
    for codeset, group in sorted(aliases.items()):
        word = 'collation' if len(group) == 1 else 'collations'
        parts.append(f'{word} {", ".join(group)} where the database encoding '
                     f'is {codeset}')
    if spellings:
        word = 'database locale' if len(spellings) == 1 else 'database locales'
        parts.append(f'{word} {", ".join(spellings)}')
    return ALSO + '; '.join(parts) if parts else None


def readable_probes(spec):
    """'61.62> 61.E1< 61.41>' as 'letters a/b, accent a/U+00E1 from the end,
    case a/A' (with the character itself where it can be printed)."""
    words = []
    for level, token in zip(('letters', 'accent', 'case'), spec.split(' ')):
        if token == '?':
            words.append(f'{level} not measured')
        elif token == 'none':
            words.append(f'{level} none')
        else:
            pair = '/'.join(glyph(int(h, 16)) or f'U+{int(h, 16):04X}'
                            for h in token[:-1].split('.'))
            words.append(f'{level} {pair}' + (' from the end' if token[-1] == '<' else ''))
    return ', '.join(words)


def code_point_runs(cps):
    """[(first, last)] of runs of consecutive code points, in order."""
    runs = []
    for c in cps:
        if runs and c == runs[-1][1] + 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    return runs


def flagged_runs(seq, flags):
    """code_point_runs of the characters of seq whose flag is 1, without
    sorting them: marked in a table by code point and read back in runs."""
    table = bytearray(0x110000)
    for c in itertools.compress(seq, flags):
        table[c] = 1
    runs, start = [], table.find(1)
    while start >= 0:
        end = table.find(0, start)
        if end < 0:
            end = len(table)
        runs.append([start, end - 1])
        start = table.find(1, end)
    return runs


def report_lines(text, indent):
    """A line of a change, cut to fit the report beside its four spaces."""
    return textwrap.wrap(text, width=74, initial_indent=indent,
                         subsequent_indent=indent + '  ',
                         break_on_hyphens=False, break_long_words=False)


def runs_text(runs):
    return ', '.join(f'U+{a:04X}' if a == b else f'U+{a:04X}..U+{b:04X}'
                     for a, b in runs)


def relation_words(rel, levels):
    """How two characters are told apart, over the levels compared."""
    if not rel & 1:
        return 'not at all (the bytes decide)'
    stronger = -1       # the strongest level compared that does not tell them apart
    for k in range(3):
        if k in levels:
            if rel >> (1 + k) & 1:
                # Somewhere between that level and this one; a level whose
                # pair differs on the two machines cannot narrow it.
                return ' or '.join(('as different letters', 'like an accent',
                                    'by case')[stronger + 1:k + 1])
            stronger = k
    return ('by less than ' + ('a letter', 'an accent', 'case')[stronger]
            if levels else 'told apart')


def test_pair(x, y, before, now, probes, levels):
    """(string a, string b, sign before, sign now) that shows a level change
    between x and y, or None when no level compared shows it."""
    for k in levels:
        if (before ^ now) >> (1 + k) & 1:
            token = probes.split(' ')[k]
            lo_, hi_ = (int(h, 16) for h in token[:-1].split('.'))
            if token[-1] == '<':
                a, b = (hi_, x), (lo_, y)
            else:
                a, b = (x, hi_), (y, lo_)
            return (a, b, '<' if before >> (1 + k) & 1 else '>',
                    '<' if now >> (1 + k) & 1 else '>')
    return None


def run_characters(seq, first, last):
    """The characters of pairs first..last of an order, as code point runs;
    most such runs are consecutive code points, which is checked whole."""
    chars = seq[first:last + 2]
    if chars == list(range(chars[0], chars[0] + len(chars))):
        return [[chars[0], chars[-1]]]
    return code_point_runs(chars)


MEASURED = {'both': 'on both machines',
            'old': 'on the old machine; the new one by the characters between',
            'new': 'on the new machine; the old one by the characters between'}


# --- what a comparison found, and how each output prints it -------------------
#
# A comparison's answer is one list of findings, each a (kind, data): for
# every changed locale what changed in it (locale_findings), and for the run
# what is not known to be unchanged and what a reader must be warned of
# (run_findings). The report, the summary and the characters file are all
# drawn from that list through the two tables below, LOCALE_KINDS and
# RUN_KINDS, which give for each kind what each of the three prints. A kind
# with no entry, or a finding one of the three never printed, raises before
# anything is written: the file and the summary are not second accounts of
# the comparison that could say less than the report.


def locale_findings(old, new, o, n):
    """[(kind, data)]: what changed between two measurements of a locale, in
    the order the report prints it.

    What moved and how many characters sort differently against another are
    found when the order changed; how pairs of characters are told apart,
    when the records or the order changed. A pair the measurement cannot
    decide is a finding of its own, never dropped: the locale changed either
    way, and a reader of the list must know it is not the whole of what may
    differ.
    """
    old_seq = unpacked(old, 'order', o['order'], o['chars'])
    same_order = n['order'] == o['order']
    new_seq = old_seq if same_order else unpacked(new, 'order', n['order'], n['chars'])
    found = []
    if same_order:
        moved, moves = [], None
        found.append(('moved', {'moved': moved, 'runs': [], 'partner': {}}))
    else:
        moves = reordering(old_seq, new_seq)
        positions, old_at = moves[:2]
        kept = kept_in_order(positions, moves[3])
        moved = sorted(c for c, k in zip(old_seq, kept) if not k)
        found.append(('moved', {
            'moved': moved, 'runs': code_point_runs(moved),
            'partner': witnesses(old_seq, new_seq, moves, kept)}))
    if moved:
        flags = in_a_reversed_pair(positions)
        count = flags.count(1)
        found.append(('reversed', {'count': count, 'all': count == len(old_seq),
                                   'runs': flagged_runs(old_seq, flags)}))
    probes_differ = o['probes'] != n['probes']
    if probes_differ:
        found.append(('probes changed', {'before': o['probes'],
                                         'now': n['probes']}))
    if o['pairs'] != n['pairs'] or not same_order:
        levels = comparable_levels(o['probes'], n['probes'])
        old_told = unpacked(old, 'pair', o['pairs'], probes=o['probes'])
        new_told = unpacked(new, 'pair', n['pairs'], probes=n['probes'])
        changes, undecided = level_changes(old_seq, old_told, new_seq,
                                           new_told, levels, moves)
        # What the pairs are compared by, when not by every level: a level
        # whose pair differs is the finding above; one no pair measured is
        # said here. Either way it is not compared, and says so.
        left_out = uncompared_levels(o['probes'], n['probes'])
        if left_out:
            found.append(('left out', {'levels': levels, 'left_out': left_out,
                                       'probes_differ': probes_differ}))
        count = sum(last - first + 1 for side, first, last, b_, n_ in changes)
        if count:
            chains = []
            for side, first, last, before, now in changes:
                seq = new_seq if side == 'new' else old_seq
                chains.append((run_characters(seq, first, last), before, now,
                               test_pair(seq[first], seq[first + 1], before,
                                         now, o['probes'], levels), side))
            pairs = None
            if count <= LIST_LIMIT:
                pairs = [(seq[i], seq[i + 1], before, now)
                         for side, first, last, before, now in changes
                         for seq in [new_seq if side == 'new' else old_seq]
                         for i in range(first, last + 1)]
            found.append(('told apart', {'count': count, 'pairs': pairs,
                                         'chains': chains, 'levels': levels}))
            if moved:
                # The characters these pairs name that did not move, marked by
                # their place in the old order.
                named = bytearray(len(old_seq))
                for side, first, last, b_, n_ in changes:
                    if side == 'new':
                        for i in range(first, last + 2):
                            named[old_at[i]] = 1
                    else:
                        named[first:last + 2] = ONE * (last - first + 2)
                still = flagged_runs(old_seq, bytes(map(operator.and_, named, kept)))
                if still:
                    found.append(('did not move', {'runs': still}))
        if undecided:
            found.append(('undecided', {'pairs': undecided}))
    return found


def moved_lines(d):
    moved, runs = d['moved'], d['runs']
    if not moved:
        return ['no character moved']
    if len(moved) <= LIST_LIMIT:
        return ([f'{len(moved)} character(s) moved:']
                + [f'  {character(c)}' for c in moved])
    if len(runs) <= LIST_LIMIT:
        return report_lines(f'{len(moved):,} characters moved: '
                            f'{runs_text(runs)}', '')
    return [f'{len(moved):,} characters moved, too many to list here']


def moved_brief(d):
    moved = d['moved']
    if not moved:
        return ['no character moved']
    if len(moved) <= LIST_LIMIT:
        return [f'{len(moved)} character(s) moved: '
                + ', '.join(short_character(c) for c in moved)]
    return [f'{len(moved):,} characters moved']


def moved_rows(d):
    """A run of moved characters and its test pair: the first of the run and
    a character that kept its place, which the two orders put on opposite
    sides of it."""
    rows = []
    for a, b in d['runs']:
        w, sign = d['partner'][a]
        rows.append(('moved', runs_text([(a, b)]), '-', f'U+{a:04X} / U+{w:04X}',
                     sign, '>' if sign == '<' else '<', MEASURED['both']))
    return rows


def reversed_lines(d):
    everyone = 'all ' if d['all'] else ''
    return [f'{everyone}{d["count"]:,} character(s) sort differently against '
            'at least one other']


def reversed_brief(d):
    return [f'{d["count"]:,} sort differently against another']


def reversed_rows(d):
    return [('sorts differently', runs_text([(a, b)]), '-', '-', '-', '-',
             MEASURED['both']) for a, b in d['runs']]


PROBES_CHANGED = ('the pairs that tell letters, accents and case apart here '
                  'are told apart differently themselves')


def probes_lines(d):
    return [PROBES_CHANGED + ':',
            f'  before: {readable_probes(d["before"])}',
            f'  now:    {readable_probes(d["now"])}']


def probes_brief(d):
    return [PROBES_CHANGED.replace(' here ', ' ')]


def probes_rows(d):
    return [('pairs changed', '-',
             (f'before: {readable_probes(d["before"])}; '
              f'now: {readable_probes(d["now"])}').translate(TEXT_ESCAPES),
             '-', '-', '-', '-')]


def compared_by(d):
    return ' and '.join(LEVEL_NAMES[k] for k in d['levels'])


def left_out_lines(d):
    named = compared_by(d)
    if d['probes_differ']:
        # Under the changed pairs, whose lines say why.
        return [f'  so pairs are compared only by {named}' if d['levels']
                else '  so no pair can be compared']
    return [(f'pairs are compared only by {named}' if d['levels']
             else 'no pair can be compared') + ': no pair measured '
            + ' and '.join(LEVEL_NAMES[k] for k in d['left_out'])]


def left_out_brief(d):
    return [f'pairs compared only by {compared_by(d)}' if d['levels']
            else 'no pair compared']


def left_out_rows(d):
    return [('not compared', '-',
             ' and '.join(LEVEL_NAMES[k] for k in d['left_out']),
             '-', '-', '-', '-')]


def told_apart_lines(d):
    if d['pairs'] is None:
        return [f'{d["count"]:,} pairs of characters told apart differently, '
                'too many to list here']
    return ([f'{d["count"]} pair(s) of characters told apart differently:']
            + [f'  {short_character(x)} and {short_character(y)}: before '
               f'{relation_words(before, d["levels"])}, now '
               f'{relation_words(now, d["levels"])}'
               for x, y, before, now in d['pairs']])


def told_apart_brief(d):
    if d['pairs'] is None:
        return [f'{d["count"]:,} pairs of characters told apart differently']
    return [f'{d["count"]} pair(s) of characters told apart differently: '
            + ', '.join(f'{short_character(x)} and {short_character(y)}'
                        for x, y, b_, n_ in d['pairs'])]


def told_apart_rows(d):
    """A chain of neighbours told apart differently, with the test pair of
    its first two characters where a level compared shows the change."""
    rows = []
    for runs, before, now, t, side in d['chains']:
        rows.append(('told apart differently', runs_text(runs),
                     f'{relation_words(before, d["levels"])} -> '
                     f'{relation_words(now, d["levels"])}',
                     ' / '.join(' '.join(f'U+{c:04X}' for c in s_) for s_ in t[:2])
                     if t else '-', t[2] if t else '-', t[3] if t else '-',
                     MEASURED[side]))
    return rows


def among(d):
    count = sum(b - a + 1 for a, b in d['runs'])
    return f'among them {count:,} character(s) that did not move'


def still_lines(d):
    if len(d['runs']) <= LIST_LIMIT:
        return report_lines(f'{among(d)}: {runs_text(d["runs"])}', '  ')
    return [f'  {among(d)}, too many to list here']


def still_brief(d):
    if len(d['runs']) <= LIST_LIMIT:
        return [f'{among(d)}: {runs_text(d["runs"])}']
    return [among(d)]


def still_rows(d):
    # No rows of their own: they are the characters of the told-apart rows
    # that no moved row names, each in the chain that shows how it changed.
    return []


def undecided_lines(d):
    return [f'{len(d["pairs"]):,} more pair(s) may be told apart differently; '
            'what was measured cannot decide']


def undecided_brief(d):
    return [f'{len(d["pairs"]):,} more pair(s) the measurement cannot decide']


def undecided_rows(d):
    return [('not decided', f'U+{x:04X} U+{y:04X}', '-', '-', '-', '-', '-')
            for x, y in d['pairs']]


# kind: (lines of the report, phrases of the summary, rows of the file), each
# from the finding's data.
LOCALE_KINDS = {
    'moved': (moved_lines, moved_brief, moved_rows),
    'reversed': (reversed_lines, reversed_brief, reversed_rows),
    'probes changed': (probes_lines, probes_brief, probes_rows),
    'left out': (left_out_lines, left_out_brief, left_out_rows),
    'told apart': (told_apart_lines, told_apart_brief, told_apart_rows),
    'did not move': (still_lines, still_brief, still_rows),
    'undecided': (undecided_lines, undecided_brief, undecided_rows),
}
REPORT, SUMMARY, FILE = 0, 1, 2


def printer(table, kind, part):
    """What prints a kind of finding in one of the three outputs; a kind the
    table lacks raises, rather than be left out of an output."""
    if kind not in table:
        raise LookupError(f'no way to print a finding of kind {kind!r}')
    return table[kind][part]


def describe_change(old, new, o, n):
    """(lines, phrase, rows): what changed between two measurements of a
    locale, drawn from its findings for the report, the summary and the
    characters file, in which nothing is cut short."""
    lines, brief, rows = [], [], []
    for kind, d in locale_findings(old, new, o, n):
        lines += printer(LOCALE_KINDS, kind, REPORT)(d)
        brief += printer(LOCALE_KINDS, kind, SUMMARY)(d)
        rows += printer(LOCALE_KINDS, kind, FILE)(d)
    return lines, '; '.join(brief), rows


# Printed under every report and every summary, and at the end of the
# characters file: what two machines that agree here can still disagree on.
CLOSING = ('!! Not measured: a rule that applies only to a particular '
           'combination of letters, such as a contraction ("ch" sorted as one '
           'letter), so two machines that agree here can still sort some longer '
           'strings apart. Where a character counts for nothing at some level '
           '(glibc ignores it there), how it is told apart from its neighbour is '
           'read less reliably, so a change there may go unseen.')


EARLIER = ('written by an earlier version of locale_order.py, which did not '
           'measure this; run it again on {}')


def which_machines(sides):
    """'the OLD machine', 'the NEW machine' or 'both machines'."""
    return 'both machines' if len(sides) == 2 else f'the {sides[0]} machine'


def control_texts(old, new):
    """[(text, is a warning)]: C and POSIX on each machine against
    control_fingerprint(). A file of any format holds them, so this needs
    nothing a file of an earlier format lacks."""
    order, pairs, probes = control_fingerprint()
    held, broken, unknown = [], [], []
    for side, data in zip(('OLD', 'NEW'), (old, new)):
        for name in ('C', 'POSIX'):
            r = data['locales'].get(name)
            if r is None:
                unknown.append(f'{name} is not in the {side} file')
            elif 'error' in r:
                unknown.append(f'{name} could not be measured on the {side} '
                               f'machine ({r["error"]})')
            elif (r['order'], r['pairs'], r['probes'], r['chars'],
                  r['partly']) != (order, pairs, probes, 0x7F, 0):
                broken.append((side, name))
            else:
                held.append(side)
    out = []
    for side in ('OLD', 'NEW'):
        names = [name for s, name in broken if s == side]
        if names:
            out.append((f'!! Control FAILED on the {side} machine: '
                        f'{" and ".join(names)} did not measure as byte order, '
                        'which is how glibc sorts C and POSIX in every '
                        'version. This '
                        'script did not measure there what it means to, and '
                        'nothing it measured on that machine can be trusted; '
                        'measure it again with this checkout\'s '
                        'scripts/locale_order.py.', True))
    if unknown:
        out.append(('!! Control NOT checked: ' + '; '.join(unknown) + '.', True))
    if not broken and not unknown:
        out.append(('Control: C and POSIX measured as byte order on both '
                    'machines, which is how glibc sorts them in every version.',
                    False))
    return out


def money_texts(old, new, both):
    """[(text, is a warning)]: the decimals of money of each locale on both
    machines, as cash.c keeps them."""
    earlier = [side for side, data in zip(('OLD', 'NEW'), (old, new))
               if 'money' not in data['held']]
    if earlier:
        files = ('the OLD and NEW files were' if len(earlier) == 2
                 else f'the {earlier[0]} file was')
        return [(f'!! Decimals of money NOT checked: {files} '
                 + EARLIER.format(which_machines(earlier)) + '.', True)]
    changed, unknown = [], []
    for name in both:
        a, b = old['money'][name], new['money'][name]
        if isinstance(a, str) or isinstance(b, str):
            unknown.append('; '.join(
                f'{shown(name)} ({side} machine): {v}' for side, v
                in (('old', a), ('new', b)) if isinstance(v, str)))
        elif money_scale(a) != money_scale(b):
            changed.append(f'{shown(name)} ({money_scale(a)} -> '
                           f'{money_scale(b)})')
    out = []
    if changed:
        out.append((f'!! Decimals of money CHANGE in {len(changed)} locale(s): '
                    'a money value stored with lc_monetary set to one of them '
                    'reads with another number of decimals on the new machine, '
                    'as a number 10 or more times larger or smaller: '
                    + ', '.join(changed) + '.', True))
    if unknown:
        out.append((f'!! Decimals of money NOT checked in {len(unknown)} '
                    'locale(s): ' + '; '.join(unknown) + '.', True))
    compared = len(both) - len(unknown)
    if not changed:
        out.append((f'Decimals of money: the same in the {compared} locale(s) '
                    'measured on both machines.', False))
    return out


def libc_texts(old, new):
    """[(text, is a warning, above)]: each machine's PostgreSQL processes
    against the C library measured there, and what that does not look at.
    above marks what voids the comparison, drawn above the list of changes
    as the control is: the OLD machine's PostgreSQL on a replaced or another
    C library (its indexes were not built with what was measured there), the
    NEW machine's on another (it will not sort with what was measured there
    even after a restart), and a side whose glibc was replaced while it was
    measured. The NEW machine's PostgreSQL still on the replaced glibc is
    not above: the list is what changes when it restarts."""
    out = []
    earlier = [side for side, data in zip(('OLD', 'NEW'), (old, new))
               if 'postgres_libc' not in data['held']]
    if earlier:
        files = ('the OLD and NEW files were' if len(earlier) == 2
                 else f'the {earlier[0]} file was')
        out.append((f'!! PostgreSQL processes NOT checked on '
                    f'{which_machines(earlier)}: {files} '
                    + EARLIER.format(which_machines(earlier)) + '.', True,
                    False))
    for side, data in zip(('OLD', 'NEW'), (old, new)):
        if side in earlier:
            continue
        p = data['postgres_libc']
        if p['replaced']:
            out.append((f'!! The glibc of the {side} machine was replaced on '
                        'disk while it was measured, so what was measured may '
                        'not be what that machine runs now; measure it again.',
                        True, True))
        if p['stale']:
            out.append((f'!! PostgreSQL on the {side} machine: '
                        f'{len(p["stale"])} process(es) still run the glibc '
                        'that was replaced on disk, so they do not sort with '
                        'the glibc measured there until PostgreSQL is '
                        'restarted.', True, side == 'OLD'))
        if p['other']:
            out.append((f'!! PostgreSQL on the {side} machine: '
                        f'{len(p["other"])} process(es) run another C library '
                        'than the one measured there, as when PostgreSQL runs '
                        'in a container, so they do not sort with the glibc '
                        'measured there.', True, True))
        why = libc_why(p)
        if why:
            out.append((f'!! PostgreSQL processes NOT checked on the {side} '
                        f'machine: {why}. Run locale_order.py there {ADVICE}.',
                        True, False))
        if not (p['stale'] or p['other'] or why or p['replaced']):
            if p['processes']:
                out.append((f'PostgreSQL on the {side} machine: '
                            f'{libc_clean(p)}.', False, False))
            else:
                out.append((f'!! PostgreSQL on the {side} machine: no process '
                            f'named {" or ".join(p["looked_for"])} was found, '
                            'so nothing checked which C library PostgreSQL '
                            'runs there.', True, False))
    if len(earlier) < 2:
        out.append(('This looks at the C library PostgreSQL runs, not at its '
                    'locale data: a PostgreSQL that was not restarted can '
                    'already use locales installed after it started.', False,
                    False))
    return out


FN = 'upper(), lower() and ILIKE'
GCONV = ('On RHEL this is what a machine without glibc-gconv-extra does; '
         'install it and measure that machine again.')
CATEGORY_NAMES = (('collate', 'LC_COLLATE'), ('ctype', 'LC_CTYPE'))
REFUSED = ('!! Refused by glibc, so a database that uses one does not open: '
           '{} locale(s). locale -a lists them, but setlocale() did not '
           'accept them when that machine was measured: PostgreSQL stops a '
           'database created with one with "FATAL: database locale is '
           'incompatible with operating system", and CREATE DATABASE refuses '
           'it. Their sort order is not measured either. glibc does not say '
           'why. On RHEL this is what it does with the language packs '
           'installed as folders (glibc-langpack-*) and glibc-gconv-extra not '
           'installed, and a machine short of memory gets the same answer; '
           'install it, or free memory, and measure that machine again.')


def refused_by_glibc(old, new, names):
    """{name: {side: [category]}}: the categories each side's glibc refused
    for a locale of names, from each file that holds how PostgreSQL uses
    them. A database whose LC_COLLATE or LC_CTYPE setlocale refuses does
    not open, so such a locale is named as that, never as unchanged nor
    only as not measured. A name glibc was never asked about ('unknown') is
    not refused."""
    out = {}
    for side, data in (('old', old), ('new', new)):
        if 'postgres_use' not in data['held']:
            continue
        for name in names:
            r = data['postgres_use'][name]
            cats = [cat for key, cat in CATEGORY_NAMES if r[key][0] == 'refused']
            if cats:
                out.setdefault(name, {})[side] = cats
    return out


def refused_sides(d):
    """[(where, categories)] of one refused locale: both machines when they
    refused the same, else each side that refused."""
    sides = d['sides']
    if len(sides) == 2 and sides['old'] == sides['new']:
        return [('both machines', sides['old'])]
    return [(f'{side} machine', sides[side]) for side in ('old', 'new')
            if side in sides]


def refused_lines(items, ctx):
    out = [textwrap.fill(REFUSED.format(len(items)), width=78,
                         subsequent_indent='   ', break_on_hyphens=False)]
    for d in items:
        for where, cats in refused_sides(d):
            out.append(f'  {shown(d["name"])} ({where}): '
                       f'{" and ".join(cats)} refused')
        also = also_known_as([d['name']], ctx['pg_names'], ctx['o'])
        if also:
            out.append(filled(also, 4, 6))
    return out


def refused_summary(items, ctx):
    groups = {}
    for d in items:
        where = ('both machines' if len(d['sides']) == 2
                 else f'the {next(iter(d["sides"]))} machine')
        groups.setdefault(where, []).append(d['name'])
    out = []
    for where in ('both machines', 'the old machine', 'the new machine'):
        if where in groups:
            out += wrapped(f'refused by glibc on {where}, so a database that '
                           'uses one does not open there: '
                           + ', '.join(map(shown, groups[where])), 7, 9)
            also = also_known_as(groups[where], ctx['pg_names'], ctx['o'])
            if also:
                out += wrapped(also, 9, 11)
    return out


def refused_rows(items):
    return [([d['name']], [
        ('refused by glibc', '-', f'{side} machine: setlocale() does not '
         f'accept {" and ".join(d["sides"][side])}', '-', '-', '-', '-')
        for side in ('old', 'new') if side in d['sides']]) for d in items]


def spaced(data):
    """Bytes as 'AD E2'."""
    return ' '.join(f'{b:02X}' for b in data)


def by_codeset(names, codesets):
    """'EUC-JP: ja_JP, ja_JP.eucjp; EUC-KR: ko_KR', by encoding."""
    groups = {}
    for name in sorted(names):
        groups.setdefault(codesets.get(name) or 'unknown', []).append(shown(name))
    return '; '.join(f'{cs}: {", ".join(group)}'
                     for cs, group in sorted(groups.items()))


def postgres_codesets():
    """(the encodings PostgreSQL allows only on the client, every encoding it
    knows, those of a database of more than one byte per character), in
    upper case, as collation_keys keeps them; the comparison runs beside
    it, in scripts/."""
    try:
        import collation_keys
    except ImportError as e:
        die(f'cannot read which encodings PostgreSQL knows ({e}); run the '
            'comparison from this checkout\'s scripts/')
    return (collation_keys.CLIENT_ONLY_CODESETS, collation_keys.POSTGRES_CODESETS,
            collation_keys.MULTIBYTE_CODESETS)


def error_row(cp, hexed, before, now, measured):
    return ('upper/lower/ILIKE error', f'U+{cp:04X}',
            f'bytes {spaced(bytes.fromhex(hexed))}, converted to a wide '
            'character as PostgreSQL does', '-', before, now, measured)


def use_view(data, name, side, refused):
    """What one machine says of a locale, as use_texts needs it: (state,
    encoding, text, why, side). state is 'refused' (glibc refused
    LC_COLLATE or LC_CTYPE), 'unread' (nothing was tried: a file of an
    earlier format, or a name Python could not pass to C), or 'accepted';
    use_texts adds 'absent', a machine that does not have the locale.
    The encoding is the one glibc gave the locale's LC_CTYPE; in a file of
    an earlier format,
    the one its order read; None where none was read. text is
    postgres_use's ['converts' | 'fails' | 'one byte' | 'not checked',
    detail], None where nothing was tried; why says why nothing is known."""
    if side in refused.get(name, {}):
        return 'refused', None, None, None, side
    if 'postgres_use' not in data['held']:
        return ('unread', data['locales'][name].get('codeset'), None,
                (f'the {side.upper()} file was written by an earlier version of '
                 'locale_order.py', None), side)
    r = data['postgres_use'][name]
    if 'accepted' != r['collate'][0] or 'accepted' != r['ctype'][0]:
        # Not refused, so a category was never asked (refused_by_glibc).
        return 'unread', None, None, (r['text'][1], side), side
    return 'accepted', r['codeset'], r['text'], (
        (r['text'][1], side) if r['text'][0] == 'not checked' else None), side


def use_kinds(view, client_only, known):
    """The databases a locale can serve, made on the machine view describes
    (PostgreSQL 13 to 18): none if glibc refuses it there (CREATE DATABASE
    refuses it, check_locale) or the machine does not have it; ('enc', X) for an encoding X PostgreSQL
    allows in a database (check_encoding_locale_matches, dbcommands.c);
    none for one it allows only on the client, where the database can only
    be SQL_ASCII, of one byte, which never converts; ('any',) for one it
    does not know, which a database of any encoding may use; ('unread',
    why) where the encoding is not known."""
    state, enc, _, why, _ = view
    if state in ('refused', 'absent'):
        return []
    if enc is None:
        return [('unread', why)]
    if enc.upper() not in known:
        return [('any',)]
    if enc.upper() in client_only:
        return []
    return [('enc', enc)]


def use_on(view, kind, multibyte, change):
    """What one machine does with a database of kind, as (state, why, text):
    state 'closed' (glibc refuses the locale: the database does not open),
    'works', 'one byte' (PostgreSQL never converts), 'error', 'unknown'
    or 'elsewhere' (an encoding PostgreSQL does not know, named in the line
    on a database of another encoding), with why as (text, the machine it
    is about, or None). A database of an encoding PostgreSQL keeps in one
    byte per character never converts (multibyte, from pg_wchar_table). A
    database of the locale's encoding on that machine gets what was
    measured there. One of
    another encoding gets an error where that machine's glibc could not
    convert the locale's own encoding: glibc then falls back to ASCII for
    the whole LC_CTYPE and fails on every byte past 0x7F, whatever the
    encoding (wcsmbsload.c, gconv_simple.c, 2.28 and 2.34)."""
    state, enc, text, why, side = view
    if state == 'refused':
        return 'closed', None, None
    if kind[0] == 'enc' and kind[1].upper() not in multibyte:
        return 'one byte', None, None
    if state == 'unread':
        return 'unknown', why, None
    s, detail = text
    if s == 'not checked' and (enc is None or kind == ('enc', enc)):
        return 'unknown', why, None
    if kind == ('enc', enc):
        # 'one byte' of an encoding of more: no character of it failed.
        return {'converts': 'works', 'fails': 'error',
                'one byte': 'works'}[s], None, text
    if s == 'fails':
        return 'error', None, text
    if kind[0] == 'any':
        return 'elsewhere', ('its encoding is one PostgreSQL does not know, so '
                             'a database of any encoding can use it', None), None
    if kind[0] == 'unread':
        return 'unknown', kind[1], None
    return 'unknown', (change, None), None


USE_BEFORE = {'works': 'converts', 'error': 'error'}


def use_texts(old, new, names, refused, only_new=()):
    """([(text, is a warning)], [file rows]): what upper(), lower(),
    initcap(), ILIKE and full-text search do with each locale of names. In
    a database of more than one byte per character they turn its text into
    wide characters through glibc (char2wchar, PostgreSQL 13 to 18), and
    stop with an error where glibc cannot.

    It follows the databases, not the machines. A database keeps the
    encoding it was made with, and PostgreSQL checks that encoding against
    the locale only when the database is made (createdb); on the new
    machine it opens through setlocale alone (postinit.c). So, for each
    locale: which databases can exist with it (use_kinds), made on the old
    machine and carried over, or made on the new one; what each machine
    does with each (use_on); and the locale goes in one line, that of its
    worst database: an error, then what is not known, then an encoding
    PostgreSQL does not know, then what works. C and POSIX never reach
    glibc (asc_tolower). A locale glibc refuses on a side is named on its
    own, and here only beside an error of the other side, or, when the old
    machine refused it, beside a NOT checked of the new one; a locale only
    on the new machine (only_new) only where its databases made there give
    an error, were not checked, or are of an encoding PostgreSQL does not
    know."""
    earlier = [side for side, data in zip(('OLD', 'NEW'), (old, new))
               if 'postgres_use' not in data['held']]
    out = []
    if earlier:
        files = ('the OLD and NEW files were' if len(earlier) == 2
                 else f'the {earlier[0]} file was')
        out.append((f'!! {FN} NOT checked: {files} '
                    + EARLIER.format(which_machines(earlier)) + '.', True))
        if len(earlier) == 2:
            return out, []
    client_only, known, multibyte = postgres_codesets()
    groups = {k: [] for k in ('changed', 'back', 'both', 'converts', 'one byte',
                              'left out', 'elsewhere')}
    unknown, one_side, moved, anydb, rows, codesets = {}, {}, {}, {}, [], {}

    def error(name, label, before, after, text, apart=None):
        # One row of the character measured; where the two machines
        # measured characters of two encodings (apart: (old's, new's)),
        # one row each, of each error.
        codesets[name] = label
        if apart:
            rows.append({'names': [name], 'rows': [
                error_row(*t[1], *states, f'on the {side} machine')
                for side, s, t, states in (
                    ('old', before, apart[0], (before, 'not measured')),
                    ('new', after, apart[1], ('not measured', after)))
                if s == 'error']})
            return
        measured = ('on both machines' if before in USE_BEFORE and after in USE_BEFORE
                    else 'on the new machine' if after in USE_BEFORE
                    else 'on the old machine')
        rows.append({'names': [name], 'rows': [error_row(
            *text[1], USE_BEFORE.get(before, 'not measured'),
            USE_BEFORE.get(after, 'not measured'), measured)]})

    # A locale only on the new machine is checked only where it holds
    # what postgres_use said, and glibc did not refuse it there: no database
    # with it was made on the old machine, and where glibc refuses it on the
    # new one, none opens there.
    added = [name for name in only_new if 'postgres_use' in new['held']
             and all(new['postgres_use'][name][k][0] != 'refused'
                     for k in ('collate', 'ctype'))]
    for name in list(names) + added:
        if name in ('C', 'POSIX'):
            groups['one byte'].append(name)
            continue
        vo = (('absent', None, None, None, 'old') if name in added
              else use_view(old, name, 'old', refused))
        vn = use_view(new, name, 'new', refused)
        if vo[0] == vn[0] == 'refused':
            continue
        change = (f'its encoding changed from {vo[1]} on the old machine to '
                  f'{vn[1]} on the new one')
        carried = use_kinds(vo, client_only, known)
        made = [k for k in use_kinds(vn, client_only, known) if k not in carried]
        found = ([(k, use_on(vo, k, multibyte, change),
                   use_on(vn, k, multibyte, change)) for k in carried]
                 + [(k, ('none', None, None), use_on(vn, k, multibyte, change))
                    for k in made])
        if not found:
            # Every database it can serve is of one byte (SQL_ASCII), or
            # none can be made: named as refused, left out, or among those
            # only on the new machine.
            if name not in refused and name not in added:
                codesets[name] = vn[1] or vo[1]
                groups['left out'].append(name)
            continue

        def rank(f):
            states = (f[1][0], f[2][0])
            return (0 if 'error' in states else 1 if 'unknown' in states
                    else 2 if 'elsewhere' in states else 3)
        kind, (b, bwhy, btext), (a, awhy, atext) = min(found, key=rank)
        label = kind[1] if kind[0] == 'enc' else (atext and vn[1]) or vo[1] or vn[1]
        if 'error' in (b, a):
            text = atext if a == 'error' else btext
            if a == 'closed':
                one_side.setdefault(('old', 'refused', ''), []).append(name)
                error(name, label, b, a, text)
            elif b == 'none' and vo[0] == 'refused':
                one_side.setdefault(('new', 'refused', ''), []).append(name)
                error(name, label, b, a, text)
            elif b == 'none' and vo[0] == 'absent':
                one_side.setdefault(('new', 'absent', ''), []).append(name)
                error(name, label, b, a, text)
            elif b == 'none':
                one_side.setdefault(('new', 'unknown', change), []).append(name)
                error(name, label, b, a, text)
            elif kind[0] != 'enc':
                # The databases' encoding is not known, and glibc that
                # cannot convert the locale's own falls back to ASCII for
                # every one of them.
                where = ('both machines' if a == b == 'error' else
                         'the new machine' if a == 'error' else 'the old machine')
                why = (kind[1] if kind[0] == 'unread' else
                       ('its encoding is one PostgreSQL does not know, so a '
                        'database of any encoding can use it', None))
                anydb.setdefault((where, why[0] + (f' (on the {why[1]} machine)'
                                                   if why[1] else '')),
                                 []).append(name)
                error(name, label, b, a, text, apart=(btext, atext))
            elif kind[0] == 'enc' and vn[0] == 'accepted' and vn[1] != kind[1] \
                    and a == 'error':
                # A database of the old encoding under the new one, whose
                # converter the new machine's glibc lacks.
                where = 'both machines' if b == 'error' else 'the new machine'
                moved.setdefault((where, vo[1], vn[1], bwhy[0] if b == 'unknown'
                                  else ''), []).append(name)
                error(name, label, b, a, text, apart=(btext, atext))
            elif a == 'error' and b == 'error':
                groups['both'].append(name)
                error(name, label, b, a, atext)
            elif a == 'error' and b == 'works':
                groups['changed'].append(name)
                error(name, label, b, a, atext)
            elif b == 'error' and a == 'works':
                groups['back'].append(name)
                error(name, label, b, a, btext)
            elif a == 'error':
                one_side.setdefault(('new', 'unknown', bwhy[0]), []).append(name)
                error(name, label, b, a, atext)
            else:
                one_side.setdefault(('old', 'unknown', awhy[0]), []).append(name)
                error(name, label, b, a, btext)
            continue
        # A database that does not open on the new machine says nothing
        # more than its refusal.
        if 'unknown' in (b, a) and a != 'closed':
            # Each reason once, with the machine it is about; one both
            # machines gave alike is said once, with neither.
            whys = []
            for s, w in ((b, bwhy), (a, awhy)):
                if s == 'unknown' and w not in whys:
                    whys.append(w)
            if (len(whys) == 2 and whys[0][0] == whys[1][0]
                    and None not in (whys[0][1], whys[1][1])):
                whys = [(whys[0][0], None)]
            why = ' and '.join(w + (f' (on the {m} machine)' if m else '')
                               for w, m in whys)
            why = why[:1].upper() + why[1:] if why.startswith('its encoding') else why
            codesets[name] = label
            unknown.setdefault(why, []).append(name)
            rows.append({'names': [name], 'rows': [(
                'upper/lower/ILIKE not checked', '-', why.translate(TEXT_ESCAPES),
                '-', '-', '-', '-')]})
        elif name in refused or (name in added and 'elsewhere' not in (b, a)):
            # Named as refused, or among those only on the new machine; one
            # of an encoding PostgreSQL does not know is in that line too.
            continue
        elif 'elsewhere' in (b, a):
            groups['elsewhere'].append(name)
        elif b in ('one byte', 'none') and a == 'one byte':
            groups['one byte'].append(name)
        else:
            groups['converts'].append(name)
    if groups['changed']:
        out.append((f'!! {FN} give an ERROR on the new machine in '
                    f'{len(groups["changed"])} locale(s) where they work on the '
                    'old one. In a database of the locale\'s own encoding, any '
                    'text that is not ASCII stops them with "ERROR: invalid '
                    'multibyte character for locale", and initcap() and '
                    'full-text search too, because the new machine\'s glibc '
                    'cannot convert that encoding. ' + GCONV + ' '
                    + by_codeset(groups['changed'], codesets) + '.', True))
    if groups['back']:
        out.append((f'{FN} work on the new machine in {len(groups["back"])} '
                    'locale(s) where they gave an error on the old one: '
                    + by_codeset(groups['back'], codesets) + '.', False))
    if groups['both']:
        out.append((f'!! {FN} give an ERROR on both machines in '
                    f'{len(groups["both"])} locale(s), as before the upgrade: in '
                    'a database of the locale\'s own encoding, any text that is '
                    'not ASCII stops them with "ERROR: invalid multibyte '
                    'character for locale". ' + GCONV + ' '
                    + by_codeset(groups['both'], codesets) + '.', True))
    for (where, x, y, why), group in sorted(moved.items()):
        before = (f'; what they do on the old machine is not known ({why})'
                  if why else '')
        out.append((f'!! {FN} give an ERROR on {where} in {len(group)} '
                    f'locale(s) whose encoding changed from {x} on the old '
                    f'machine to {y} on the new one: a database of {x} made on '
                    f'the old machine opens there under {y}, and a glibc that '
                    f'cannot convert {y} converts no text that is not ASCII '
                    f'("ERROR: invalid multibyte character for locale"){before}. '
                    + GCONV + ' ' + by_codeset(group, codesets) + '.', True))
    for (where, why), group in sorted(anydb.items()):
        out.append((f'!! {FN} give an ERROR on {where} in {len(group)} '
                    f'locale(s) that databases of an encoding not known here '
                    f'may use ({why}): whatever that encoding, a glibc that '
                    'cannot convert the locale\'s own converts no text that is '
                    'not ASCII ("ERROR: invalid multibyte character for '
                    'locale"). ' + GCONV + ' ' + by_codeset(group, codesets) + '.',
                    True))
    for (side, kind, why), group in sorted(one_side.items()):
        other = 'old' if side == 'new' else 'new'
        if kind == 'refused':
            said = (f' that glibc refused on the {other} machine, where no '
                    'database could use them')
        elif kind == 'absent':
            said = ' that the old machine does not have'
        else:
            said = (f', where it is not known what they do on the {other} '
                    f'machine ({why})')
        out.append((f'!! {FN} give an ERROR on the {side} machine in '
                    f'{len(group)} locale(s){said}. In a database of the '
                    'locale\'s own encoding, any text that is not ASCII stops '
                    'them there with "ERROR: invalid multibyte character for '
                    'locale". ' + GCONV + ' ' + by_codeset(group, codesets) + '.',
                    True))
    if earlier:
        return out, rows
    if unknown:
        out.append((f'!! {FN} NOT checked in '
                    f'{sum(map(len, unknown.values()))} locale(s): '
                    + ' '.join(f'{why}: {by_codeset(group, codesets)}.'
                               for why, group in sorted(unknown.items())), True))
    line = (f'{FN}: text that is not ASCII converts on both machines in '
            f'{len(groups["converts"])} locale(s); in the '
            f'{len(groups["one byte"])} of an encoding of one byte PostgreSQL '
            'never converts it in a database of that encoding.')
    if groups['left out']:
        line += (f' Left out: {len(groups["left out"])} locale(s) of an '
                 'encoding PostgreSQL allows only on the client, so no '
                 'database converts text with them ('
                 + by_codeset(groups['left out'], codesets) + ').')
    if groups['elsewhere']:
        line += (f' The {len(groups["elsewhere"])} locale(s) of an encoding '
                 'PostgreSQL does not know are in the line on a database of '
                 'another encoding.')
    out.append((line, False))
    return out, rows


CTYPE_LINE = 'Character rules (LC_CTYPE)'
# The encodings of the databases whose character rules are not checked:
# EUC_JP, EUC_KR, EUC_CN and EUC_TW, as glibc names the locales' encodings
# (PostgreSQL reads GB2312 as EUC_CN, chklocale.c).
CTYPE_EUC = ('EUC-JP', 'EUC-KR', 'GB2312', 'EUC-TW')
CTYPE_BYTE_ROWS = ('toupper', 'tolower') + CTYPE_CLASSES
# What a reader calls the class a character is in, by the first class of
# these it is in.
CTYPE_KINDS = (('alpha', 'letter'), ('digit', 'digit'),
               ('punct', 'punctuation'), ('space', 'space'),
               ('print', 'another printable character'))
# The classes a change of kind accounts for; a change of any other is named.
CTYPE_NAMED = ('upper', 'lower', 'xdigit')


def ctype_wide_of(part, key):
    """(towupper, towlower, classes) of a wide table: two maps, and each
    class as its runs."""
    w = json.loads(ctype_unpacked(part['wide'], key, wide_whole).decode('ascii'))
    return dict(map(tuple, w['upper'])), dict(map(tuple, w['lower'])), w['classes']


def in_runs(runs, starts, c):
    """Is c in one of runs, whose first code points are starts?"""
    i = bisect.bisect_right(starts, c) - 1
    return i >= 0 and c <= runs[i][1]


def runs_differ(a, b):
    """The code points in one of two lists of runs and not in the other.
    Between two neighbouring edges of either list, a code point is in both,
    in one or in neither alike, so one look per stretch tells."""
    edges = sorted({x for r in a + b for x in (r[0], r[1] + 1)})
    sa, sb = [r[0] for r in a], [r[0] for r in b]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        if in_runs(a, sa, lo) != in_runs(b, sb, lo):
            out.extend(range(lo, hi))
    return out


def ctype_kind(classes):
    return next((word for k, word in CTYPE_KINDS if k in classes), 'no class')


def ctype_how(cp, maps, was, now, shown_as, exact_as):
    """(what the report says, what the file says) of a character or byte
    whose rules changed: maps are ((old upper, new upper), (old lower, new
    lower)), was and now its classes on each machine; shown_as and exact_as
    write a value for the report and for the file."""
    words, exact = [], []
    for (x, y), name in zip(maps, ('capital', 'small letter')):
        if x == y:
            continue
        if x == cp:
            words.append(f'gains the {name} {shown_as(y)}')
        elif y == cp:
            words.append(f'loses the {name} {shown_as(x)}')
        else:
            words.append(f'{name} {shown_as(x)} -> {shown_as(y)}')
        exact.append(f'{"upper" if name == "capital" else "lower"}: '
                     f'{"itself" if x == cp else exact_as(x)} -> '
                     f'{"itself" if y == cp else exact_as(y)}')
    if was != now:
        exact.append(f'classes: {" ".join(was) or "none"} -> '
                     f'{" ".join(now) or "none"}')
        before, after = ctype_kind(was), ctype_kind(now)
        if before != after:
            words.insert(0, f'{before} -> {after}')
        for k in CTYPE_CLASSES if before == after else CTYPE_NAMED:
            if (k in was) != (k in now):
                words.append(f'{"joins" if k in now else "leaves"} [[:{k}:]]')
    return ', '.join(words), '; '.join(exact)


def ctype_wide_changes(old_part, ok, new_part, nk):
    """[(code point, classified on the old machine, report's words, file's
    words)] of every code point whose towupper, towlower or class differs
    between two wide tables. Classified: the old table maps it or puts it
    in a class."""
    a, b = ctype_wide_of(old_part, ok), ctype_wide_of(new_part, nk)
    changed = set()
    for i in (0, 1):
        changed |= {c for c in set(a[i]) | set(b[i]) if a[i].get(c, c) != b[i].get(c, c)}
    starts = {}
    for k in CTYPE_CLASSES:
        changed.update(runs_differ(a[2][k], b[2][k]))
        starts[k] = ([r[0] for r in a[2][k]], [r[0] for r in b[2][k]])
    out = []
    for cp in sorted(changed):
        was = [k for k in CTYPE_CLASSES if in_runs(a[2][k], starts[k][0], cp)]
        now = [k for k in CTYPE_CLASSES if in_runs(b[2][k], starts[k][1], cp)]
        maps = tuple((a[i].get(cp, cp), b[i].get(cp, cp)) for i in (0, 1))
        words, exact = ctype_how(cp, maps, was, now, short_character,
                                 lambda c: f'U+{c:04X}')
        out.append((cp, bool(was) or cp in a[0] or cp in a[1], words, exact))
    return out


def ctype_byte_changes(old_part, ok, new_part, nk):
    """[(byte, report's words, file's words)] of every byte whose toupper,
    tolower or class differs between two byte tables."""
    A = ctype_unpacked(old_part['bytes'], ok, bytes_whole)
    B = ctype_unpacked(new_part['bytes'], nk, bytes_whole)
    out = []
    for i in range(256):
        if all(A[r * 256 + i] == B[r * 256 + i] for r in range(len(CTYPE_BYTE_ROWS))):
            continue
        maps = tuple((A[r * 256 + i], B[r * 256 + i]) for r in (0, 1))
        was = [k for r, k in enumerate(CTYPE_CLASSES, 2) if A[r * 256 + i]]
        now = [k for r, k in enumerate(CTYPE_CLASSES, 2) if B[r * 256 + i]]
        words, exact = ctype_how(i, maps, was, now, lambda c: f'0x{c:02X}',
                                 lambda c: f'{c:02X}')
        out.append((i, words, exact))
    return out


UTF8_USE = ' that a UTF8 database can use'


def ctype_group(members, compared, which=''):
    """How a line names its locales: all those compared (which says of
    which kind), or each."""
    if len(members) == compared:
        return f'the {compared} locale(s) compared on both machines{which}'
    return f'{len(members)} locale(s): {", ".join(map(shown, members))}'


def ctype_texts(old, new, names, refused, only_new=()):
    """([finding], [file rows]): what glibc's character rules (LC_CTYPE)
    answer differently on the two machines, for the locales of names.

    PostgreSQL picks glibc's functions by the database's encoding, never the
    locale's: in UTF8, towupper, towlower and isw<class> of each character
    (upper(), lower(), initcap(), ILIKE, regular expressions, full-text
    search); in an encoding of one byte per character, SQL_ASCII too, toupper,
    tolower and is<class> of each byte; in every other, the byte functions for
    regular expressions and the wide ones for the rest, on the characters
    glibc's converter makes of the text (regc_pg_locale.c, formatting.c,
    ts_locale.c, REL_13_23; pg_locale_libc.c in REL_18_6). So the locales are
    compared by the pair of tables they use on the two machines, which are a
    handful, and every locale of a pair changes alike. A character the old
    table did not classify counts as much as one it did: regular expressions,
    initcap() and full-text search read it otherwise all the same (measured on
    PostgreSQL 17 and 18). The characters are compared only in the locales a
    UTF8 database can use, the bytes in all.

    C and POSIX are not compared: PostgreSQL handles them itself. A locale
    glibc refused on either side is named as that, by refused_by_glibc; one
    whose rules either side could not measure is named with why, never
    counted as the same; one on the new machine only has nothing to be
    compared with. EUC_JP, EUC_KR, EUC_CN and EUC_TW are not checked here:
    which characters such a database hands glibc is a question of glibc's
    converters."""
    earlier = [side for side, data in zip(('OLD', 'NEW'), (old, new))
               if 'ctype' not in data['held']]
    if earlier:
        files = ('the OLD and NEW files were' if len(earlier) == 2
                 else f'the {earlier[0]} file was')
        return [{'text': f'!! {CTYPE_LINE} NOT checked: {files} '
                         + EARLIER.format(which_machines(earlier)) + '.'}], []
    _, known, _ = postgres_codesets()
    euc = {}
    for name in list(names) + list(only_new):
        for data in (old, new):
            r = data['locales'].get(name)
            if r and r.get('codeset') in CTYPE_EUC:
                euc[name] = r['codeset']
                break
    wide_pairs, byte_pairs, unknown, elsewhere = {}, {}, {}, []
    for name in names:
        if name in refused or name in ('C', 'POSIX'):
            continue
        keys, why = [], []
        for side, data in (('old', old), ('new', new)):
            entry = data['ctype']['locales'][name]
            table = data['ctype']['tables'].get(entry[1]) if entry[0] == 'table' else None
            if table is None or table[0] == 'not checked':
                why.append((side, (table or entry)[1]))
            else:
                keys.append(table)
        if why:
            # Each side's reason on its own, as the decimals of money say
            # theirs; one reason for both, once.
            unknown[name] = ([('both', why[0][1])] if len(why) == 2
                             and why[0][1] == why[1][1] else why)
            continue
        # A database or collation of UTF8 opens with a locale of UTF-8, of
        # US-ASCII (SQL_ASCII to PostgreSQL, chklocale.c:185) or of an
        # encoding PostgreSQL does not know, and with no other
        # (check_encoding_locale_matches); one made on either machine opens
        # on the new one. An encoding not read counts as one that could.
        if any(cs is None or cs.upper() in ('UTF-8', 'UTF8', 'US-ASCII')
               or cs.upper() not in known
               for cs in (data['postgres_use'][name]['codeset']
                          or data['locales'][name].get('codeset')
                          for data in (old, new))):
            wide_pairs.setdefault((keys[0][0], keys[1][0]), []).append(name)
        else:
            elsewhere.append(name)
        byte_pairs.setdefault((keys[0][1], keys[1][1]), []).append(name)
    found, rows, sames = [], [], []
    for what, pairs, changes in (
            ('character', wide_pairs, ctype_wide_changes),
            ('byte', byte_pairs, ctype_byte_changes)):
        compared = sum(map(len, pairs.values()))
        groups = {}
        for (ok, nk), members in pairs.items():
            got = tuple(changes(old['ctype'], ok, new['ctype'], nk))
            if got:
                groups.setdefault(got, []).extend(members)
        for got, members in sorted(groups.items(), key=lambda g: sorted(g[1])):
            members = sorted(members)
            # The rows of each character stand under the group's label, its
            # locales named once: under every locale of a group of 863, the
            # file of 2.28 -> 2.34 was 61 MB (measured).
            label = f'LC_CTYPE_group_{sum(1 for r in rows if "label" in r) + 1}'
            rows.append({'names': members, 'label': label, 'rows': [
                ('LC_CTYPE group', '-', f'{label}: the {what}(s) whose rules '
                 'change in these locale(s), each a row under that name',
                 '-', '-', '-', '-')]})
            if what == 'character':
                found.append(ctype_character_finding(
                    got, ctype_group(members, compared, UTF8_USE)))
                rows.append({'names': [label], 'rows': [
                    ('LC_CTYPE changed', f'U+{cp:04X}', exact, '-',
                     'classified' if known else 'not classified', '-',
                     'on both machines') for cp, known, _, exact in got]})
            else:
                found.append(ctype_byte_finding(got, ctype_group(members, compared)))
                rows.append({'names': [label], 'rows': [
                    ('LC_CTYPE changed', f'byte {b:02X}', exact, '-', '-',
                     '-', 'on both machines') for b, _, exact in got]})
        same = compared - sum(map(len, groups.values()))
        if same:
            sames.append({'text': f'{CTYPE_LINE} of each {what}: the same in the '
                                  f'{"other " if groups else ""}{same} '
                                  'locale(s) compared on both machines'
                                  f'{UTF8_USE if what == "character" else ""}.'})
        if what == 'character' and elsewhere:
            sames.append({'text': f'{CTYPE_LINE} of each character: not compared '
                                  f'in {len(elsewhere)} locale(s) of an encoding '
                                  'no UTF8 database can use; a database that can '
                                  'use them reads their rules byte by byte'
                                  + (', or, in EUC, as the line on EUC says'
                                     if set(elsewhere) & set(euc) else '') + '.'})
    # Right after the changes it points at, ahead of what stayed the same.
    kinds = [what for what in ('character', 'byte')
             if any(r['rows'][0][1].startswith('U+' if what == 'character'
                                               else 'byte')
                    for r in rows if 'label' not in r)]
    if kinds:
        found.append({'where': ' and '.join(kinds)})
    found += sames
    if unknown:
        where = {'both': 'both machines', 'old': 'old machine',
                 'new': 'new machine'}
        found.append({'text': f'!! {CTYPE_LINE} NOT checked in {len(unknown)} '
                              'locale(s): ' + '; '.join(
                                  f'{shown(n)} ({where[side]}): {why}'
                                  for n, sides in sorted(unknown.items())
                                  for side, why in sides) + '.'})
        rows += [{'names': [n], 'rows': [
            ('LC_CTYPE not checked', '-', f'{where[side]}: {why}'.translate(
                TEXT_ESCAPES), '-', '-', '-', '-') for side, why in sides]}
                 for n, sides in sorted(unknown.items())]
    if euc:
        found.append({'text': f'!! {CTYPE_LINE} in an EUC_JP, EUC_KR, EUC_CN or '
                              f'EUC_TW database: NOT checked here, '
                              f'{len(euc)} locale(s). '
                              + by_codeset(euc, euc) + '.'})
    return found, rows


def ctype_character_finding(got, group):
    """The finding of a group of locales whose wide tables change alike:
    a head, one item per character the old machine classified, a tail."""
    known = [(cp, words) for cp, k, words, _ in got if k]
    head = (f'!! {CTYPE_LINE} CHANGE for {len(got)} character(s) in '
            f'{group}: each gets another upper case, '
            'lower case or class. upper(), lower(), initcap(), ILIKE, regular '
            'expressions and full-text search read these rules in a UTF8 '
            'database, so they can give other results there for text that '
            f'holds one. {len(known)} of them the old machine already '
            f'classified{":" if known else "."}')
    other = len(got) - len(known)
    tail = (f'The other {other} it did not classify; a UTF8 database can hold '
            'them too, and they change those results as well.' if other else '')
    return {'head': head, 'items': [f'{character(cp)}: {words}'
                                    for cp, words in known], 'tail': tail}


def ctype_byte_finding(got, group):
    head = (f'!! {CTYPE_LINE} CHANGE for {len(got)} byte(s) in '
            f'{group}. A database of one byte per '
            'character, SQL_ASCII too, reads them in upper(), lower(), '
            'initcap(), ILIKE, regular expressions and full-text search, and '
            'a database of any other encoding but UTF8 in regular '
            'expressions:')
    return {'head': head, 'items': [f'byte {b:02X}: {words}' for b, words, _ in got],
            'tail': ''}


def ctype_text(d, where):
    """A finding of the character rules as one text: the file's note, and
    what the tests read."""
    if 'where' in d:
        return where(d)
    if 'items' not in d:
        return d['text']
    return ' '.join(x for x in (d['head'], '; '.join(d['items'])
                                + ('.' if d['items'] else ''), d['tail']) if x)


def ctype_where(d, ctx):
    return (f'Every such {d["where"]} is in {ctx["characters_to"]}.'
            if ctx['characters_to'] else
            f'Pass --characters-to FILE for every such {d["where"]}.')


def ctype_lines(items, ctx):
    out = []
    for d in items:
        if 'items' not in d:
            out.append('\n'.join(whole_words(
                ctype_text(d, lambda d: ctype_where(d, ctx)), 0, 3)))
            continue
        out += wrapped(d['head'], 0, 3)
        for i, item in enumerate(d['items']):
            out += wrapped(item + (';' if i + 1 < len(d['items']) else '.'), 5, 7)
        if d['tail']:
            out += wrapped(d['tail'], 3, 3)
    return out


def ctype_summary(items, ctx):
    out = []
    for d in items:
        if 'items' not in d:
            out += whole_words(ctype_text(d, lambda d: ctype_where(d, ctx)), 5, 8)
            continue
        out += wrapped(d['head'], 5, 8)
        for i, item in enumerate(d['items']):
            out += wrapped(item + (';' if i + 1 < len(d['items']) else '.'), 10, 12)
        if d['tail']:
            out += wrapped(d['tail'], 8, 8)
    return out


def ctype_notes(items):
    return ['# ' + ctype_text(d, lambda d: f'Every such {d["where"]} is a row '
                                           'of this file.').translate(TEXT_ESCAPES)
            for d in items]


def other_encoding_text(names, o, n):
    """The line that names the locales of names whose encoding PostgreSQL
    does not know, or None: a database of any encoding can use one, and
    nothing here measured what it does there. C and POSIX are accepted with
    every encoding and convert nothing."""
    _, known, _ = postgres_codesets()
    codesets = {}
    for name in names:
        if name in ('C', 'POSIX'):
            continue
        # Either machine's: a locale whose encoding PostgreSQL does not know
        # on one side can serve a database of another encoding there.
        for codeset in (o.get(name, {}).get('codeset'), n[name].get('codeset')):
            if codeset and codeset.upper() not in known:
                codesets[name] = codeset
                break
    if not codesets:
        return None
    return (f'!! Not measured in a database of another encoding: '
            f'{len(codesets)} locale(s) whose encoding PostgreSQL does not '
            'know, so that CREATE DATABASE lets a database of any encoding use '
            'one, with only a WARNING ("could not determine encoding for '
            'locale"). In such a database, a UTF8 one say, PostgreSQL hands the '
            'locale the database\'s bytes, not the ones measured here, so '
            'neither its order nor whether upper(), lower() and ILIKE give an '
            'error there is known. ' + by_codeset(codesets, codesets) + '.')


# The byte sequences PostgreSQL stores in a database of a locale's encoding,
# converting from UTF-8, that corpus_for() does not hold, so that no order
# measured here places them: {codeset: (PostgreSQL's encoding, the corpus
# digest they were worked out against, 'code point:bytes ...')}. Measured
# with convert_to() of every code point on PostgreSQL 13.23, 14.24, 15.19,
# 16.15, 17.11 and 18.6 (the same bytes in all six), and read in its source
# for 10 to 12: its utf8_to_*.map tables change only in comments from
# REL_10_0 to REL_18_6, and pg_mb_radix_conv not at all. The 17 other
# server encodings the three test machines' locales use hold nothing more
# than their corpus. EUC_JP's are NEC row 13 (lead byte AD) and IBM
# extensions (8F F3, 8F F4); U+2116 is among them because PostgreSQL stores
# it as AD E2 and Python as 8F A2 F1.
POSTGRES_ONLY = {
    'EUC-JP': (
        'EUC_JP',
        '50349d2c24ac0020d2c8abed3fc5be911c8b8ef7c659ef04dac808952844f27d',
        '2170:8FF3F3 2171:8FF3F4 2172:8FF3F5 2173:8FF3F6 2174:8FF3F7 2175:8FF3F8 '
        '2176:8FF3F9 2177:8FF3FA 2178:8FF3FB 2179:8FF3FC FF07:8FF4A9 FF02:8FF4AA '
        '70BB:8FF4AE 4EFC:8FF4AF 50F4:8FF4B0 51EC:8FF4B1 5307:8FF4B2 5324:8FF4B3 '
        'FA0E:8FF4B4 548A:8FF4B5 5759:8FF4B6 FA0F:8FF4B7 FA10:8FF4B8 589E:8FF4B9 '
        '5BEC:8FF4BA 5CF5:8FF4BB 5D53:8FF4BC FA11:8FF4BD 5FB7:8FF4BE 6085:8FF4BF '
        '6120:8FF4C0 654E:8FF4C1 663B:8FF4C2 6665:8FF4C3 FA12:8FF4C4 F929:8FF4C5 '
        '6801:8FF4C6 FA13:8FF4C7 FA14:8FF4C8 6A6B:8FF4C9 6AE2:8FF4CA 6DF8:8FF4CB '
        '6DF2:8FF4CC 7028:8FF4CD FA15:8FF4CE FA16:8FF4CF 7501:8FF4D0 7682:8FF4D1 '
        '769E:8FF4D2 FA17:8FF4D3 7930:8FF4D4 FA18:8FF4D5 FA19:8FF4D6 FA1A:8FF4D7 '
        'FA1B:8FF4D8 7AE7:8FF4D9 FA1C:8FF4DA FA1D:8FF4DB 7DA0:8FF4DC 7DD6:8FF4DD '
        'FA1E:8FF4DE 8362:8FF4DF FA1F:8FF4E0 85B0:8FF4E1 FA20:8FF4E2 FA21:8FF4E3 '
        '8807:8FF4E4 FA22:8FF4E5 8B7F:8FF4E6 8CF4:8FF4E7 8D76:8FF4E8 FA23:8FF4E9 '
        'FA24:8FF4EA FA25:8FF4EB 90DE:8FF4EC FA26:8FF4ED 9115:8FF4EE FA27:8FF4EF '
        'FA28:8FF4F0 9592:8FF4F1 F9DC:8FF4F2 FA29:8FF4F3 973B:8FF4F4 974D:8FF4F5 '
        '9751:8FF4F6 FA2A:8FF4F7 FA2B:8FF4F8 FA2C:8FF4F9 999E:8FF4FA 9AD9:8FF4FB '
        '9B72:8FF4FC FA2D:8FF4FD 9ED1:8FF4FE 2460:ADA1 2461:ADA2 2462:ADA3 '
        '2463:ADA4 2464:ADA5 2465:ADA6 2466:ADA7 2467:ADA8 2468:ADA9 2469:ADAA '
        '246A:ADAB 246B:ADAC 246C:ADAD 246D:ADAE 246E:ADAF 246F:ADB0 2470:ADB1 '
        '2471:ADB2 2472:ADB3 2473:ADB4 2160:ADB5 2161:ADB6 2162:ADB7 2163:ADB8 '
        '2164:ADB9 2165:ADBA 2166:ADBB 2167:ADBC 2168:ADBD 2169:ADBE 3349:ADC0 '
        '3314:ADC1 3322:ADC2 334D:ADC3 3318:ADC4 3327:ADC5 3303:ADC6 3336:ADC7 '
        '3351:ADC8 3357:ADC9 330D:ADCA 3326:ADCB 3323:ADCC 332B:ADCD 334A:ADCE '
        '333B:ADCF 339C:ADD0 339D:ADD1 339E:ADD2 338E:ADD3 338F:ADD4 33C4:ADD5 '
        '33A1:ADD6 337B:ADDF 301D:ADE0 301F:ADE1 2116:ADE2 33CD:ADE3 2121:ADE4 '
        '32A4:ADE5 32A5:ADE6 32A6:ADE7 32A7:ADE8 32A8:ADE9 3231:ADEA 3232:ADEB '
        '3239:ADEC 337E:ADED 337D:ADEE 337C:ADEF 222E:ADF3 2211:ADF4 221F:ADF8 '
        '22BF:ADF9'),
    'EUC-KR': (
        'EUC_KR',
        'eeaf009011911d66acfaf7dd75494a83510ab07cc2e1d7653b8918aaef960b5c',
        '327E:A2E8'),
    'TIS-620': (
        'WIN874',
        '97fb5ae48fceaac5076172ee8f22ca7b8a0a0a0b895c0c75ad81727d269fcee3',
        '00A0:A0'),
}
STORED_SOURCE = ('These are PostgreSQL\'s own conversions, measured on '
                 'PostgreSQL 13 to 18 and read in its source for 10 to 12, '
                 'where they are the same.')


def postgres_only(codeset):
    """POSTGRES_ONLY's sequences of an encoding as [(code point, bytes)], in
    byte order."""
    return [(int(cp, 16), bytes.fromhex(b)) for cp, b in
            (entry.split(':') for entry in POSTGRES_ONLY[codeset][2].split())]


def stored_texts(o, codesets_of):
    """[(text, codeset or None, names)]: for each encoding of POSTGRES_ONLY
    with a locale in codesets_of ({name: (codeset, corpus digest)}), what
    PostgreSQL stores there, converting from UTF-8, that no measured order
    places. A locale
    measured over other strings than the list was worked out against is
    NOT checked, since the list may not be what its order lacks."""
    out = []
    for codeset in sorted(POSTGRES_ONLY):
        encoding, digest, _ = POSTGRES_ONLY[codeset]
        names = sorted(n for n, (cs, _) in codesets_of.items() if cs == codeset)
        fit = [n for n in names if codesets_of[n][1] == digest]
        other = [n for n in names if codesets_of[n][1] != digest]
        stored = postgres_only(codeset)
        listed = ', '.join(map(shown, fit))
        if fit and codeset == 'EUC-JP':
            jp = dict(stored)
            out.append((
                f'!! Not measured in EUC-JP ({listed}): {len(stored)} byte '
                'sequences that PostgreSQL stores in an EUC_JP database, for '
                'characters it converts from UTF-8, are not among the strings '
                'measured here, so where they sort is not known, nor whether '
                f'that changed. Among them {chr(0x2116)} (U+2116), which '
                f'PostgreSQL stores as {spaced(jp[0x2116])} rather than the 8F '
                f'A2 F1 measured here, and {chr(0x2460)} (U+2460), '
                f'{spaced(jp[0x2460])}.', codeset, fit))
        elif fit and codeset == 'EUC-KR':
            (cp, data), = stored
            out.append((
                f'!! Not measured in EUC-KR ({listed}): {spaced(data)}, which '
                f'PostgreSQL stores in an EUC_KR database for {chr(cp)} '
                f'(U+{cp:04X}), is not among the strings measured here, so '
                'where it sorts is not known.', codeset, fit))
        elif fit:
            (cp, data), = stored
            out.append((
                f'!! Not measured in TIS-620 ({listed}): {spaced(data)}, which '
                f'PostgreSQL stores in a WIN874 database for the no-break '
                f'space (U+{cp:04X}), is not among the strings measured here. '
                'PostgreSQL lets a TIS-620 locale serve a database of any '
                'encoding, and in one other than WIN874 the bytes it compares '
                'are not the ones measured here at all.', codeset, fit))
        if other:
            out.append((
                f'!! NOT checked in {codeset} ({", ".join(map(shown, other))}): '
                'which byte sequences PostgreSQL stores that were not measured '
                'here, since these locales were measured over other strings '
                'than the ones that list was made from.', None, other))
    return out


def stored_text_of(d, where):
    return d['text'] if 'text' in d else f'{where} {STORED_SOURCE}'


def stored_where(ctx):
    return (f'Every such byte sequence is in {ctx["characters_to"]}.'
            if ctx['characters_to'] else
            'Pass --characters-to FILE for every such byte sequence.')


def whole_words(text, indent, more):
    """wrapped(), but a word longer than the line, such as the path of the
    characters file audit.sh names, stays whole on a line of its own: cut,
    it could not be copied."""
    return textwrap.wrap(text, width=78, initial_indent=' ' * indent,
                         subsequent_indent=' ' * more, break_on_hyphens=False,
                         break_long_words=False)


def stored_lines(items, ctx):
    return ['\n'.join(whole_words(stored_text_of(d, stored_where(ctx)), 0, 3))
            for d in items]


def stored_summary(items, ctx):
    return [line for d in items
            for line in whole_words(stored_text_of(d, stored_where(ctx)), 5, 8)]


def stored_notes(items):
    return ['# ' + stored_text_of(d, 'Every such byte sequence is a row of '
                                     'this file.').translate(TEXT_ESCAPES)
            for d in items]


def stored_rows(items):
    out = []
    for d in items:
        encoding = POSTGRES_ONLY[d['codeset']][0]
        article = 'an' if encoding[0] in 'AEIOU' else 'a'
        out.append((d['names'], [
            ('stored by PostgreSQL, not measured', f'U+{cp:04X}',
             f'PostgreSQL stores {spaced(data)} in {article} {encoding} '
             'database; the strings measured here do not hold it',
             '-', '-', '-', '-') for cp, data in postgres_only(d['codeset'])]))
    return out


def nothing(items, ctx):
    """What an output that takes a kind's findings elsewhere prints of it."""
    return []


def rows_of(items):
    return [(d['names'], d['rows']) for d in items]


def run_findings(o, n, warnings, partly, unmeasured, only_old,
                 checks=(), refused=None, beside=()):
    """[(kind, data)]: what the run says beside the changed locales -- its
    warnings, the locales NOT known to be unchanged (glibc refused them, they
    could not be measured, or they are not on the new machine) and those
    unchanged only as far as measured, the checks of each machine beside the
    order (control, money, PostgreSQL processes, how PostgreSQL uses each
    locale), what the order does not place (beside), and what no comparison
    measures."""
    found = [('warning', {'text': text, 'wrap': wrap}) for text, wrap in warnings]
    found += [(kind, {'text': text, 'warning': warning})
              for kind, texts in checks for text, warning in texts]
    found += [('partly', {'name': name,
                          'count': max(o[name]['partly'], n[name]['partly'])})
              for name in partly]
    found += [('refused', {'name': name, 'sides': sides})
              for name, sides in sorted((refused or {}).items())]
    found += [('not measured', {'name': name, 'old': o[name], 'new': n[name]})
              for name in unmeasured]
    found += [('not on the new machine', {'name': name}) for name in only_old]
    found += list(beside)
    found.append(('closing', {'text': CLOSING}))
    return found


def wrapped(text, indent, more):
    return textwrap.wrap(text, width=78, initial_indent=' ' * indent,
                         subsequent_indent=' ' * more, break_on_hyphens=False)


def filled(text, indent=0, more=3):
    return textwrap.fill(text, width=78, initial_indent=' ' * indent,
                         subsequent_indent=' ' * more, break_on_hyphens=False)


def warning_lines(items, ctx):
    return [textwrap.fill(d['text'], width=78, subsequent_indent='   ')
            if d['wrap'] else d['text'] for d in items]


def warning_summary(items, ctx):
    return [line for d in items for line in wrapped(d['text'], 5, 8)]


def notes(items):
    return ['# ' + d['text'].translate(TEXT_ESCAPES) for d in items]


def partly_lines(items, ctx):
    return [textwrap.fill(
        f'!! Unchanged only as far as measured: {len(items)} of them. In '
        'each, the pairs of neighbouring characters counted in brackets '
        'differ by less than a letter, and no pair could be found there to '
        'tell whether by an accent or by case; a change from one to the '
        'other would not show:', width=78, subsequent_indent='   '),
        filled(', '.join(f'{shown(d["name"])} ({d["count"]})' for d in items),
               2, 2)]


def partly_summary(items, ctx):
    return wrapped('!! Unchanged only as far as measured, because a change '
                   'between an accent and case would not show in them: '
                   + ', '.join(shown(d['name']) for d in items), 5, 8)


def partly_rows(items):
    return [([d['name']], [('unchanged only as far as measured', '-',
                            f'{d["count"]} pair(s) of neighbours differ by less '
                            'than a letter, and no pair could tell whether by '
                            'an accent or by case', '-', '-', '-', '-')])
            for d in items]


def unmeasured_lines(items, ctx):
    out = [f'!! Could not be measured, so NOT known to be unchanged: '
           f'{len(items)} locale(s):']
    for d in items:
        name, o_, n_ = d['name'], d['old'], d['new']
        before, now = o_.get('error'), n_.get('error')
        if not before and not now:
            out.append(f'  {shown(name)}: measured over different characters on '
                       f'the two machines ({o_["chars"]:,} as {o_["codeset"]}, '
                       f'then {n_["chars"]:,} as {n_["codeset"]}), so the '
                       'orders cannot be compared')
        elif before == now:
            out.append(f'  {shown(name)} (both machines): {before}')
        else:
            for side, error in (('old', before), ('new', now)):
                if error:
                    out.append(f'  {shown(name)} ({side} machine): {error}')
        also = also_known_as([name], ctx['pg_names'], ctx['o'])
        if also:
            out.append(filled(also, 4, 6))
    return out


def unmeasured_summary(items, ctx):
    names = [d['name'] for d in items]
    out = wrapped('could not be measured: ' + ', '.join(map(shown, names)), 7, 9)
    also = also_known_as(names, ctx['pg_names'], ctx['o'])
    if also:
        out += wrapped(also, 9, 11)
    return out


def unmeasured_rows(items):
    out = []
    for d in items:
        before, now = d['old'].get('error'), d['new'].get('error')
        if not before and not now:
            why = 'measured over different characters on the two machines'
        elif before == now:
            why = f'both machines: {before}'
        else:
            why = '; '.join(f'{side} machine: {error}' for side, error
                            in (('old', before), ('new', now)) if error)
        out.append(([d['name']], [('not measured', '-',
                                   why.translate(TEXT_ESCAPES), '-', '-', '-',
                                   '-')]))
    return out


def only_old_lines(items, ctx):
    # Not measured on the new machine is not measured: a machine missing
    # its language packs would otherwise look like a clean upgrade.
    return [textwrap.fill(
        f'!! Not on the new machine, so NOT known to be unchanged: '
        f'{len(items)} locale(s). Either the upgrade removes them or '
        'their language pack is not installed there; a database or '
        'collation that uses one cannot use it on the new machine. If a '
        'language pack is missing, install it and measure the new machine '
        'again.', width=78, subsequent_indent='   ')] + [
        f'  {shown(d["name"])}' for d in items]


def only_old_summary(items, ctx):
    return wrapped('not on the new machine, so a database or collation that '
                   'uses one cannot use it there: '
                   + ', '.join(shown(d['name']) for d in items), 7, 9)


def only_old_rows(items):
    return [([d['name']], [('not on the new machine', '-', '-', '-', '-', '-',
                            '-')]) for d in items]


def check_lines(items, ctx):
    return [filled(d['text']) for d in items]


def check_summary(items, ctx):
    return [line for d in items for line in wrapped(d['text'], 5, 8)]


def closing_lines(items, ctx):
    return [textwrap.fill(d['text'], width=78, subsequent_indent='   ')
            for d in items]


def closing_summary(items, ctx):
    return [line for d in items for line in wrapped(d['text'], 5, 8)]


# kind: (lines of the report, lines of the summary, what the file gets), each
# from every finding of the kind at once. The file gets ([locale], rows)
# for a locale, which are sorted with the changes, or a note: a line under
# the rows, starting "# ".
RUN_KINDS = {
    'warning': (warning_lines, warning_summary, notes),
    'partly': (partly_lines, partly_summary, partly_rows),
    'not measured': (unmeasured_lines, unmeasured_summary, unmeasured_rows),
    'not on the new machine': (only_old_lines, only_old_summary, only_old_rows),
    'control': (check_lines, check_summary, notes),
    'postgres libc above': (check_lines, check_summary, notes),
    'money': (check_lines, check_summary, notes),
    'postgres libc': (check_lines, check_summary, notes),
    'refused': (refused_lines, refused_summary, refused_rows),
    'postgres use': (check_lines, check_summary, notes),
    'postgres use rows': (nothing, nothing, rows_of),
    'ctype': (ctype_lines, ctype_summary, ctype_notes),
    'ctype rows': (nothing, nothing, rows_of),
    'unmeasured bytes': (stored_lines, stored_summary, stored_notes),
    'unmeasured bytes rows': (nothing, nothing, stored_rows),
    'closing': (closing_lines, closing_summary, notes),
}


class Drawn:
    """One output's view of the run's findings, taken a kind at a time in
    the order the output prints them. done() raises if a kind present was
    never taken, so a finding cannot drop out of one output unseen."""

    def __init__(self, found, part, ctx=None):
        self.part, self.ctx, self.by_kind = part, ctx, {}
        for kind, d in found:
            printer(RUN_KINDS, kind, part)
            self.by_kind.setdefault(kind, []).append(d)
        self.left = set(self.by_kind)

    def count(self, kind):
        return len(self.by_kind.get(kind, ()))

    def __call__(self, kind):
        self.left.discard(kind)
        items = self.by_kind.get(kind)
        if not items:
            return []
        how = printer(RUN_KINDS, kind, self.part)
        return how(items) if self.part == FILE else how(items, self.ctx)

    def done(self):
        if self.left:
            raise LookupError(f'findings never printed: {sorted(self.left)}')


def machine_label(d):
    """How a measurement names its machine: the build, or the version."""
    return d['glibc_build'] or d['glibc_version'] + ' (build unknown)'


def tag_version(tag):
    """(2, 28) from the tag 'glibc-2.28'; None when it names no release,
    such as a commit id, or glibc-2.34.9000, which opens 2.35's development
    and is not 2.34."""
    m = re.fullmatch(r'glibc-(\d+)\.(\d+)', tag)
    return (int(m[1]), int(m[2])) if m else None


ALSO = 'also, in PostgreSQL: '


def also_rows(names, pg_names, locales):
    """The file's row of the other names PostgreSQL knows a group by, the
    text the report prints under its heading; none when there is none."""
    also = also_known_as(names, pg_names, locales)
    if not also:
        return []
    return [('also in PostgreSQL', '-', also[len(ALSO):].translate(TEXT_ESCAPES),
             '-', '-', '-', '-')]


def summary_lines(old, new, groups, run, totals, characters_to=None):
    """The report in brief, for the summary audit.sh prints at the end.

    Every locale is named, and so are the names PostgreSQL knows it by: a
    reader looks a locale up by the name the database uses, and "ko_KR" in a
    UTF-8 database is ko_KR.utf8. What moved is said in one phrase; the
    characters' names and how each pair is told apart are in the report,
    and every one of them in the characters file, named under the changes.
    The report's warnings are here whole, so the summary needs no other copy.
    """
    o, n = old['locales'], new['locales']
    out = [f'   ({machine_label(old)} -> {machine_label(new)})']
    out += run('warning')
    out += run('control')
    out += run('postgres libc above')
    count = sum(len(names) for names, brief in groups)
    if groups:
        out.append(f'     {count} locale(s) sort differently -- reindex what '
                   'uses them:')
        for names, brief in groups:
            # Wrapped at the same indent, as in the report, so that where the
            # names end and what moved begins is plain.
            out += wrapped(heading(names, o, n), 7, 7)
            out += wrapped(brief, 9, 11)
            also = also_known_as(names, run.ctx['pg_names'], o)
            if also:
                out += wrapped(also, 9, 11)
        if characters_to:
            # On a line of its own, whole, like every other list audit.sh names.
            out.append('       every character and pair:')
            out.append('         ' + characters_to)
    else:
        out.append('     no locale measured on both machines sorts differently')
    unknown = (run.count('refused') + run.count('not measured')
               + run.count('not on the new machine'))
    if unknown:
        out.append(f'     NOT known to be unchanged: {unknown} locale(s)')
    out += run('refused')
    out += run('not measured')
    out += run('not on the new machine')
    out += run('partly')
    out += wrapped(totals, 5, 7)
    out += run('money')
    out += run('postgres libc')
    out += run('postgres use')
    out += run('postgres use rows')
    out += run('ctype')
    out += run('ctype rows')
    out += run('unmeasured bytes')
    out += run('unmeasured bytes rows')
    out += run('closing')
    run.done()
    return out


def compare(old_path, new_path, tags=None, summary_to=None, characters_to=None):
    if characters_to:
        # An earlier run's list under this name would read as this run's
        # answer if this one is refused or gives up: it goes first.
        try:
            os.remove(characters_to)
        except FileNotFoundError:
            pass
        except OSError as e:
            die(f'cannot write {characters_to}: {e}')
    digest = corpus_digest()
    old, old_raw = load(old_path, digest)
    new, new_raw = load(new_path, digest)
    if old_raw == new_raw:
        die('the two files hold the same measurement, so nothing would be '
            'compared')
    ov, nv = version_tuple(old['glibc_version']), version_tuple(new['glibc_version'])
    if ov > nv:
        die(f'the OLD file is from {old["glibc_version"]} and the NEW one from '
            f'{new["glibc_version"]}: pass the machine you upgrade FROM first')
    # Within one glibc version the builds decide, by rpm's own rule: a
    # distro can change the order inside a major release, as RHEL8 did at
    # glibc-2.28-93, so two builds of one version are a real pair to compare.
    op, np_ = build_parts(old['glibc_build']), build_parts(new['glibc_build'])
    build_order = None
    if ov == nv and op and np_:
        build_order = rpm_vercmp(op[0], np_[0]) or rpm_vercmp(op[1], np_[1])
        if build_order > 0:
            die(f'the OLD file is from {old["glibc_build"]} and the NEW one from '
                f'{new["glibc_build"]}, an older build of the same glibc: pass '
                'the machine you upgrade FROM first')
    # The tags audit.sh audits. A file measured on another glibc would put
    # another upgrade's changes in this one's report, so it is refused; a tag
    # that names no release, such as a commit id, cannot be checked, and the
    # report says so.
    unchecked = []
    for side, data, tag in zip(('OLD', 'NEW'), (old, new), tags or ()):
        wanted = tag_version(tag)
        if wanted is None:
            unchecked.append(
                f'!! The {side} tag, {tag}, names no glibc release, so nothing '
                f'checked that the {side} file was measured on that glibc.')
        elif wanted != version_tuple(data['glibc_version']):
            die(f'the {side} file, {data["path"]}, was measured on '
                f'{data["glibc_version"]}, and the {side} tag is {tag}: pass '
                'the measurement of a machine that runs that glibc')
        elif not re.fullmatch(r'glibc \d+\.\d+', data['glibc_version']):
            # version_tuple reads 'glibc 2.34.9000', a development snapshot on
            # the way to 2.35, as 2.34: agreeing on those two numbers does not
            # make it the release the tag names.
            unchecked.append(
                f'!! The {side} file was measured on {data["glibc_version"]}, '
                'which names no glibc release, so nothing checked that it is '
                f'the glibc of the {side} tag, {tag}.')

    o, n = old['locales'], new['locales']
    both = sorted(set(o) & set(n))
    # A locale glibc refuses on either side is named as that, ahead of what
    # its order says: a database that uses it does not open there.
    refused = refused_by_glibc(old, new, both)
    changed, same, unmeasured = [], [], []
    for name in both:
        if name in refused:
            continue
        if 'error' in o[name] or 'error' in n[name]:
            unmeasured.append(name)
        elif o[name]['corpus'] != n[name]['corpus']:
            # Two orders of different sets of characters cannot be compared,
            # and treating them as changed would name characters that did
            # not move.
            unmeasured.append(name)
        elif (o[name]['order'], o[name]['pairs']) != (n[name]['order'], n[name]['pairs']):
            changed.append(name)
        else:
            same.append(name)
    only_old = sorted(set(o) - set(n))
    only_new = sorted(set(n) - set(o))

    # Locales whose measurements are the same on both sides changed the same
    # way, so each change is worked out once; then every locale whose change
    # reads the same is printed under one heading, and every locale measured
    # alike gets the file's rows under one label. All three outputs are
    # worked out before anything is written, so a record that turns out
    # damaged stops the run before any part of a report is on the screen.
    measured_alike = {}
    for name in changed:
        key = (o[name]['order'], o[name]['pairs'], n[name]['order'], n[name]['pairs'])
        measured_alike.setdefault(key, []).append(name)
    reads_alike, briefs, rows = {}, {}, []
    for names in measured_alike.values():
        lines, brief, found = describe_change(old, new, o[names[0]], n[names[0]])
        reads_alike.setdefault(tuple(lines), []).extend(names)
        briefs[tuple(lines)] = brief
        rows.append((sorted(names), found))
    groups = sorted(reads_alike.items(), key=lambda item: sorted(item[1]))
    # The names PostgreSQL gives the locales, taken from the old machine: a
    # cluster being upgraded had its collations created there. A locale that
    # could not be measured takes part as well, since PostgreSQL imported it
    # all the same.
    pg_names = postgres_names(o)
    # Unchanged only as far as it was measured: some neighbours differ by less
    # than a letter in a way no pair could classify, so a change between an
    # accent and case among them would not show.
    partly = [name for name in same if o[name]['partly'] or n[name]['partly']]
    in_part = f' ({len(partly)} of them only as far as measured)' if partly else ''
    in_refused = f'{len(refused)} refused by glibc, ' if refused else ''
    totals = (f'Of the {len(o)} locale(s) on the old machine: {len(changed)} '
              f'changed, {len(same)} unchanged{in_part}, {len(unmeasured)} not '
              f'measured, {in_refused}{len(only_old)} not on the new machine.')
    warnings = []   # (text, wrapped in the report)
    if build_order == 0 or (old['glibc_build'] and old['glibc_build'] == new['glibc_build']):
        warnings.append(('!! Both machines run the same glibc build. This '
                         'compares two machines, not an upgrade.', False))
    elif ov == nv and build_order is None:
        warnings.append((
            f'!! Both machines run {old["glibc_version"]} and the build of at '
            'least one is unknown or cannot be read, so which one is older '
            'cannot be told: check that the machine you upgrade FROM was given '
            'first. This may also compare two machines rather than an upgrade.',
            True))
    warnings += [(text, True) for text in unchecked]
    libc = libc_texts(old, new)
    checks = (('control', control_texts(old, new)),
              ('postgres libc above',
               [(text, warning) for text, warning, above in libc if above]),
              ('money', money_texts(old, new, both)),
              ('postgres libc',
               [(text, warning) for text, warning, above in libc if not above]))
    use, use_rows = use_texts(old, new, both, refused, only_new)
    checks += (('postgres use', use),)
    ctype, ctype_rows = ctype_texts(old, new, both, refused, only_new)
    # What PostgreSQL stores in the locales measured on both machines that
    # their order does not place. It needs only the order part, so a file
    # of any format gets it.
    stored = stored_texts(o, {name: (o[name]['codeset'], o[name]['corpus'])
                              for name in same + changed})
    beside = [('postgres use rows', d) for d in use_rows]
    beside += [('ctype', d) for d in ctype]
    beside += [('ctype rows', d) for d in ctype_rows]
    # A locale refused on both sides keeps no encoding in its order on
    # either (load() refuses one that does), so it is never named here; nor
    # is one only on the new machine that glibc refuses there. One only on
    # the new machine serves the databases made there.
    other = other_encoding_text(both + only_new, o, n)
    if other:
        beside.append(('unmeasured bytes', {'text': other}))
    beside += [('unmeasured bytes', {'text': text}) for text, _, _ in stored]
    if any(codeset for _, codeset, _ in stored):
        beside.append(('unmeasured bytes', {'note': True}))
    beside += [('unmeasured bytes rows', {'names': names, 'codeset': codeset})
               for _, codeset, names in stored if codeset]
    found = run_findings(o, n, warnings, partly, unmeasured, only_old, checks,
                         refused, beside)
    ctx = {'pg_names': pg_names, 'o': o, 'characters_to': characters_to}

    report = [f'Old machine: {machine_label(old)}, {len(old["locales"])} locale(s)',
              f'New machine: {machine_label(new)}, {len(new["locales"])} locale(s)']
    run = Drawn(found, REPORT, ctx)
    report += run('warning')
    report += run('control')
    report += run('postgres libc above')
    report += ['', f'Sort order CHANGED in {len(changed)} locale(s):']
    for lines, names in groups:
        report += ['', filled(heading(names, o, n), 2, 2)]
        also = also_known_as(names, pg_names, o)
        if also:
            report.append(filled(also, 4, 6))
        report += [f'    {line}' for line in lines]
    if changed:
        # The lists above stop at LIST_LIMIT; say where all of it is.
        report.append('')
        if characters_to:
            report += ['Every character and pair found, by locale:',
                       f'  {characters_to}']
        else:
            report.append('Pass --characters-to FILE for every character and '
                          'pair found.')
    report += ['', f'Unchanged: {len(same)} locale(s).']
    report += run('partly')
    report += run('refused')
    report += run('not measured')
    report += run('not on the new machine')
    report.append(f'Only on the new machine: {len(only_new)} locale(s).')
    report += [f'  {shown(name)}' for name in only_new]
    report += ['', totals]
    report += run('money')
    report += run('postgres libc')
    report += run('postgres use')
    report += run('postgres use rows')
    report += run('ctype')
    report += run('ctype rows')
    report += run('unmeasured bytes')
    report += run('unmeasured bytes rows')
    report += run('closing')
    run.done()
    brief = None
    if summary_to:
        brief = summary_lines(
            old, new, [(names, briefs[lines]) for lines, names in groups],
            Drawn(found, SUMMARY, ctx), totals, characters_to)
    file_notes = []
    if characters_to:
        # The locales NOT known to be unchanged, and those unchanged only as
        # far as measured, have rows of their own, so that the file does not
        # read them as unchanged; the warnings and what is not measured are
        # notes under the rows, as in the report.
        run = Drawn(found, FILE)
        for kind in ('partly', 'refused', 'not measured',
                     'not on the new machine', 'postgres use rows',
                     'ctype rows', 'unmeasured bytes rows'):
            rows += run(kind)
        file_notes = (run('warning') + run('control')
                      + run('postgres libc above') + run('money')
                      + run('postgres libc') + run('postgres use')
                      + run('ctype') + run('unmeasured bytes')
                      + run('closing'))
        run.done()
        # One group per set of names: a locale can have rows of several
        # findings (unchanged only as far as measured, and an error of
        # upper() beside it), and they are read as one group.
        merged = {}
        for names, found_rows in rows:
            merged.setdefault(tuple(names), []).extend(found_rows)
        # Each group closes with the names PostgreSQL gives it, as the report
        # prints them under its heading: a reader looks a locale up by the
        # name the database uses.
        rows = [(list(names), found_rows + also_rows(list(names), pg_names, o))
                for names, found_rows in merged.items()]

    partial = characters_to + '.partial' if characters_to else None
    written = []    # what this run wrote, removed again if it gives up

    def give_up(msg):
        # A list half written, or whole beside a summary that is not, is not
        # this run's answer: nothing is left under either name, nor a summary
        # that names a list that is not there.
        for path in [partial] + written:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        die(msg)

    if characters_to:
        # Every character and pair, nothing cut at LIST_LIMIT: the report and
        # the summary say how many and name this file. Written first, like
        # the summary, so that a list that cannot be written stops the run
        # before anything points at it; written under another name, and given
        # its own only once the summary is written too. The last line counts
        # what is above it, so that a file cut short says so by itself.
        try:
            with open(partial, 'w', encoding='utf-8') as f:
                f.write(f'# Step 11, {machine_label(old)} -> {machine_label(new)}: '
                        'every character and pair found, by locale.\n')
                f.write('locales\tfinding\tcharacters\thow\ttest\tbefore\tnow\tmeasured\n')
                count = 0
                for names, found_rows in sorted(rows):
                    label = ','.join(shown(x).translate(TSV_ESCAPES) for x in names)
                    for row in found_rows:
                        f.write(label + '\t' + '\t'.join(row) + '\n')
                        count += 1
                for note in file_notes:
                    f.write(note + '\n')
                f.write(f'# end: {len(rows)} locale group(s), {count} row(s)\n')
        except OSError as e:
            give_up(f'cannot write {characters_to}: {e}')
    if summary_to:
        # Written before the report, so that a summary that cannot be written
        # stops the run with no report on the screen, like damage does.
        try:
            with open(summary_to, 'w', encoding='utf-8') as f:
                f.write('\n'.join(brief) + '\n')
        except OSError as e:
            give_up(f'cannot write {summary_to}: {e}')
        written.append(summary_to)
    if characters_to:
        try:
            os.replace(partial, characters_to)
        except OSError as e:
            give_up(f'cannot write {characters_to}: {e}')

    for line in report:
        print(line)
    return 0


def main():
    # The RHEL8 note is taken from the docstring, so that there is one copy.
    note = next((p for p in __doc__.split('\n\n') if p.startswith('On RHEL8')), None)
    ap = argparse.ArgumentParser(
        description='Measure how every locale on this machine sorts, or '
                    'compare two\nmachines\' measurements.',
        epilog=note, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--compare', nargs=2, metavar=('OLD', 'NEW'),
                    help='compare the outputs of two machines, the one you '
                         'upgrade from first, and name the characters that '
                         'moved and those whose character rules (LC_CTYPE) '
                         'change')
    ap.add_argument('--tags', nargs=2, metavar=('OLD_TAG', 'NEW_TAG'),
                    help='with --compare: the glibc tags of the pair audited, '
                         'as audit.sh passes them; a file measured on another '
                         'glibc than its tag is refused')
    ap.add_argument('--summary-to', metavar='FILE',
                    help='with --compare: also write the report in brief to '
                         'FILE, for the summary audit.sh prints')
    ap.add_argument('--characters-to', metavar='FILE',
                    help='with --compare: also write every character and pair '
                         'found, by locale, to FILE (tab-separated)')
    ap.add_argument('--jobs', type=int,
                    help='processes to measure with (default: one per CPU, '
                         'fewer when half the free memory does not hold them '
                         'at about 0.3 GB each)')
    ap.add_argument('--extract', action='store_true',
                    help='measure, and write one compressed file with the '
                         'measurement, this machine\'s locale sources and '
                         'charmaps, and its glibc build (> old.tar)')
    ap.add_argument('--unpack', nargs=2, metavar=('FILE', 'DIR'),
                    help='check a file --extract wrote and lay it out in DIR, '
                         'which must not exist, for audit.sh')
    args = ap.parse_args()
    if (args.tags or args.summary_to or args.characters_to) and not args.compare:
        die('--tags, --summary-to and --characters-to go with --compare')
    if sum(map(bool, (args.extract, args.compare, args.unpack))) > 1:
        die('--extract, --compare and --unpack are three different runs; '
            'give one')
    if args.unpack:
        return unpack(*args.unpack)
    if args.compare:
        return compare(*args.compare, tags=args.tags,
                       summary_to=args.summary_to,
                       characters_to=args.characters_to)
    if args.jobs is not None and args.jobs < 1:
        die('--jobs must be at least 1')
    jobs = args.jobs or default_jobs()
    if args.extract:
        return extract(jobs)
    text, out, seconds = measure(jobs)
    sys.stdout.write(text)
    return finished(out, seconds, len(text))


if __name__ == '__main__':
    sys.exit(main())
