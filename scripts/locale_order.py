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

Each is kept whole, compressed, once for all the locales that share it, so
that the comparison can name the characters that moved without a second run
on the machines. The file was 2.6 MB on each of glibc 2.28, 2.34 and 2.39.

Not measured: a rule that applies only to a particular combination of letters,
such as a contraction ("ch" sorted as one letter). Two machines that agree on
both can still sort some longer strings differently. Where a character counts
for nothing at some level (glibc ignores it there), how it is told apart from
its neighbour is read less reliably, so a change there may go unseen. Only sort
order is measured: upper(), lower() and the other character rules of LC_CTYPE
are not.

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
memory per process. Comparing needs only Python.
"""
import argparse
import base64
import bisect
import codecs
import collections
import ctypes
import ctypes.util
import gzip
import hashlib
import io
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

FORMAT = 'locale_order/1'

# The file --extract writes holds the measurement, the machine's locale sources
# and its glibc build. The sources are what glibc-locale-source installs on
# RHEL, and they are copied as found or not at all, with the reason.
EXTRACT_FORMAT = 'locale_order_extract/1'
LOCALE_SOURCES = '/usr/share/i18n/locales'

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

# Characters or pairs listed by name under a changed locale; past this many,
# only the count is printed.
LIST_LIMIT = 20


def die(msg):
    sys.stderr.write(f'locale_order.py: {msg}\n')
    sys.exit(2)


def corpus_digest():
    """What both machines must have measured for their results to compare.

    Taken from the data rather than from a description of it, so a copy of
    this script with a different corpus cannot pass for the same one.
    """
    h = hashlib.sha256(FORMAT.encode())
    h.update(''.join(chr(c) for c in CODEPOINTS).encode('utf-8'))
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


def unpack_order(raw):
    """pack_order's input back, or None when the bytes end mid-number."""
    seq = []
    prev = z = shift = 0
    for b in raw:
        z |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
            continue
        prev += z // 2 if not z & 1 else -(z + 1) // 2
        seq.append(prev)
        z = shift = 0
    return None if shift else seq


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
    the two orders differ: by U+0001 in every UTF-8 locale tried, and by 482
    of 17,175 characters in ko_KR (EUC-KR). The argument types are declared,
    so that a str passed by mistake fails instead of reaching C as wide
    characters.
    """
    global LIBC
    if LIBC is None:
        lib = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6')
        lib.strxfrm.restype = ctypes.c_size_t
        lib.strxfrm.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.strcoll.restype = ctypes.c_int
        lib.strcoll.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        LIBC = lib
    return LIBC


def corpus_for(codeset):
    """(code points, their bytes, digest) of what a database in this encoding
    can hold, in byte order; None when Python has no converter for it.

    Byte order, because the sort that follows is stable and PostgreSQL breaks
    a strcoll tie by the bytes, so equal characters end up in PostgreSQL's
    order. A character the encoding cannot hold is left out: no database in
    that encoding can contain it. A character Python's codec cannot encode is
    left out as well, which is a limit: against glibc 2.34's charmaps, EUC-KR
    loses U+327E and EUC-JP loses U+FF5E, both of which PostgreSQL accepts,
    besides the lone bytes 0x80-0x9F, which it does not (pg_euckr_verifychar
    and pg_eucjp_verifychar in its src/common/wchar.c). The digest covers each
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
    try:
        saved = locale.setlocale(locale.LC_CTYPE)
        locale.setlocale(locale.LC_CTYPE, name)
        codeset = locale.nl_langinfo(locale.CODESET) or None
        locale.setlocale(locale.LC_CTYPE, saved)
    except (locale.Error, ValueError, AttributeError):
        return None
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

    Two builds of one glibc version are two measurements: a distro backport
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
    results, order_data, pair_data = {}, {}, {}
    try:
        with fork_pool(jobs) as pool:
            measured = pool.map(measure_one, names)
            for done, (name, result, data) in enumerate(measured, 1):
                line = progress(done, len(names), time.time() - start)
                if line:
                    sys.stderr.write(line)
                    sys.stderr.flush()
                results[name] = result
                if data:
                    order_data.setdefault(result['order'], data['order'])
                    pair_data.setdefault(result['pairs'], data['pairs'])
    except BrokenProcessPool:
        # A process killed from outside, most often for want of memory. The
        # pool reports it rather than waiting forever for its answer.
        die('a measuring process was killed before it finished, most likely '
            'for lack of memory; run again with a smaller --jobs')
    out = {
        'format': FORMAT,
        'corpus': corpus_digest(),
        'glibc_version': version,
        'glibc_build': build,
        'python': platform.python_version(),
        'locales_listed': len(names),
        'locales': results,
        'order_data': order_data,
        'pair_data': pair_data,
    }
    out['content'] = content_digest(out)
    text = json.dumps(out, sort_keys=True) + '\n'
    return text, out, time.time() - start


def progress(done, total, seconds):
    """The line that says how far a measurement has got, or None when this
    locale starts no new tenth.

    One line a tenth, not one a locale: a machine with every language pack
    lists some 870. None for the last locale as well, whose line finished()
    writes with more in it. "Done", not "measured": a locale that could not
    be measured is done too, and only finished() says how many were.
    """
    if done >= total or done * 10 // total == (done - 1) * 10 // total:
        return None
    return (f'  {done} of {total} locale(s) done '
            f'({done * 100 // total}%), {seconds:.0f} s so far\n')


def finished(out, seconds, size):
    """Report a measurement on stderr, once written; the exit status, 1 when
    some locale could not be measured."""
    results = out['locales']
    failed = sorted(n for n, r in results.items() if 'error' in r)
    sys.stderr.write(f'Done in {seconds:.0f} s: '
                     f'{out["locales_listed"] - len(failed)} locale(s) measured, '
                     f'{len(failed)} could not be; '
                     f'{size / 1e6:.1f} MB written.\n')
    for n in failed:
        sys.stderr.write(f'  {shown(n)}: {results[n]["error"]}\n')
    return 1 if failed else 0


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


def pack_sources(tar, directory):
    """Add the locale sources under 'locales/' in tar, and return their
    manifest entry.

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
            ti = tar.gettarinfo(full, 'locales/' + rel)
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


def archive(text, out, directory=LOCALE_SOURCES):
    """The file of --extract as (its bytes, the manifest written last in it).

    The measurement first, exactly the file a plain run writes; then the
    locale sources; then extract.json, which describes both and is written
    last, so a file cut short lacks it. gzip without a name or a time, so
    that two runs on one machine write the same bytes.
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
    data, manifest = archive(text, out)
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
                         f'{sources["not_included"]}. The file holds the '
                         'measurement and the glibc build only.\n')
    return status


def unpack(path, dest):
    """--unpack: check a file --extract wrote, and lay it out in dest.

    dest must not exist. What audit.sh reads there: locale_order.out, the
    measurement; build-id, the glibc build; and either locales/, the sources,
    or sources-not-included, the reason they are missing. Every entry must be
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
    packed = members[1:-1]
    if not ((isinstance(entries, dict) and entries and missing is None)
            or (isinstance(missing, str) and missing and entries is None)):
        refuse('it says neither which locale sources it holds nor why it '
               'holds none')
    # Each name once, even to a disk that does not tell case apart (macOS,
    # Windows): there two names that differ only in case are one file, and
    # the second would land on the first, or go through it if it is a link.
    found, folded = {}, {}
    for m, content in packed:
        rel = m.name[len('locales/'):] if m.name.startswith('locales/') else ''
        parts = rel.split('/')
        if not rel or any(p in ('', '.', '..') for p in parts):
            refuse(f'an entry, {m.name!r}, is outside locales/')
        if rel in found:
            refuse(f'{m.name} appears twice')
        if rel.casefold() in folded:
            refuse(f'locales/{folded[rel.casefold()]} and {m.name} differ '
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
        refuse('its locale sources are not the ones it lists')

    try:
        os.makedirs(dest)
    except OSError as e:
        die(f'cannot lay the file out in {dest}: {e}')
    with open(os.path.join(dest, 'locale_order.out'), 'wb') as f:
        f.write(measurement)
    with open(os.path.join(dest, 'build-id'), 'w') as f:
        f.write(build.strip() + '\n')
    if missing is not None:
        with open(os.path.join(dest, 'sources-not-included'), 'w') as f:
            f.write(missing + '\n')
        return 0
    root = os.path.join(dest, 'locales')
    try:
        os.mkdir(root)
        for m, content in packed:
            target = os.path.join(root, m.name[len('locales/'):])
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
    return 0


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
    if not isinstance(data, dict) or data.get('format') != FORMAT:
        die(f'{path} is not an output of this version of locale_order.py')
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
    if 'content' not in data:
        die(f'{path} is not an output of this version of locale_order.py')
    if data['content'] != content_digest(data):
        die(f'{path} does not match the digest its machine wrote into it; the '
            'file was damaged after it was written')
    if version_tuple(data.get('glibc_version')) is None:
        die(f'{path} does not say which glibc it was measured on')
    parts = build_parts(data.get('glibc_build'))
    if parts and version_tuple('glibc ' + parts[0]) != version_tuple(data['glibc_version']):
        die(f'{path} says it was measured on {data["glibc_version"]} but names '
            f'the build {data["glibc_build"]}; a measurement is bound to one '
            'build, so measure that machine again')
    data['path'] = path
    return data, raw


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
                    and hashlib.sha256(''.join(map(chr, seq)).encode('utf-8'))
                    .hexdigest() == digest):
                return seq
    except (ValueError, OverflowError, zlib.error):
        # A damaged copy fails here, as a number out of range, bytes that do
        # not decompress, or text that is not base64.
        pass
    die(f'{data["path"]}: a measured {kind} record does not match its digest; '
        'the file was damaged after it was written')


def moved_characters(old_seq, new_seq):
    """The characters that moved: all but the most that kept their order.

    The characters that kept their relative order are the longest increasing
    subsequence of the new positions read in the old order; every other
    character is one that moved. A swap of two characters counts one of them.
    """
    position = [0] * 0x110000
    for i, c in enumerate(new_seq):
        position[c] = i
    tail_positions, tail_indexes = [], []
    previous = [-1] * len(old_seq)
    for i, c in enumerate(old_seq):
        p = position[c]
        k = bisect.bisect_left(tail_positions, p)
        if k == len(tail_positions):
            tail_positions.append(p)
            tail_indexes.append(i)
        else:
            tail_positions[k] = p
            tail_indexes[k] = i
        previous[i] = tail_indexes[k - 1] if k else -1
    kept = bytearray(len(old_seq))
    i = tail_indexes[-1] if tail_indexes else -1
    while i >= 0:
        kept[i] = 1
        i = previous[i]
    return sorted(c for i, c in enumerate(old_seq) if not kept[i])


def pair_changes(old_seq, old_told, new_seq, new_told):
    """Neighbours in both orders whose told-apart record differs.

    (x, y, old record, new record) for each. A pair that is next to each other
    in only one of the orders is left to moved_characters.
    """
    successor = [-1] * 0x110000
    record = bytearray(0x110000)
    for i in range(len(new_seq) - 1):
        successor[new_seq[i]] = new_seq[i + 1]
        record[new_seq[i]] = new_told[i]
    changes = []
    for i in range(len(old_seq) - 1):
        x, y = old_seq[i], old_seq[i + 1]
        if successor[x] == y and record[x] != old_told[i]:
            changes.append((x, y, old_told[i], record[x]))
    return changes


def told_apart_words(v):
    """What a told-apart record says, in words."""
    if v & 1:
        return 'not at all (the bytes decide)'
    for k, words in enumerate(('as different letters', 'like an accent',
                               'by case')):
        if (v >> (1 + 2 * k)) & 3 == 0:
            return words
    return 'by less than case'


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
    return 'also, in PostgreSQL: ' + '; '.join(parts) if parts else None


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


def describe_change(old, new, o, n):
    """(lines, phrase): the lines that say what changed between two
    measurements of a locale, and the same in one phrase for a summary."""
    old_seq = unpacked(old, 'order', o['order'], o['chars'])
    new_seq = (old_seq if n['order'] == o['order']
               else unpacked(new, 'order', n['order'], n['chars']))
    lines, brief = [], []
    moved = [] if new_seq is old_seq else moved_characters(old_seq, new_seq)
    if not moved:
        lines.append('no character moved')
        brief.append('no character moved')
    elif len(moved) > LIST_LIMIT:
        lines.append(f'{len(moved):,} characters moved, too many to list here')
        brief.append(f'{len(moved):,} characters moved')
    else:
        lines.append(f'{len(moved)} character(s) moved:')
        lines.extend(f'  {character(c)}' for c in moved)
        brief.append(f'{len(moved)} character(s) moved: '
                     + ', '.join(short_character(c) for c in moved))
    if o['pairs'] != n['pairs'] and o['probes'] != n['probes']:
        # Records taken with different pairs do not compare position by
        # position; that the pairs had to change is the finding.
        lines.append('the pairs that tell letters, accents and case apart here '
                     'are told apart differently themselves:')
        lines.append(f'  before: {readable_probes(o["probes"])}')
        lines.append(f'  now:    {readable_probes(n["probes"])}')
        brief.append('the pairs that tell letters, accents and case apart are '
                     'told apart differently themselves')
    elif o['pairs'] != n['pairs']:
        changes = pair_changes(
            old_seq, unpacked(old, 'pair', o['pairs'], probes=o['probes']),
            new_seq, unpacked(new, 'pair', n['pairs'], probes=n['probes']))
        if len(changes) > LIST_LIMIT and len(moved) > LIST_LIMIT:
            pass   # a rewrite: the count of moved characters already says it
        elif len(changes) > LIST_LIMIT:
            lines.append(f'{len(changes):,} pairs of neighbouring characters '
                         'told apart differently, too many to list here')
            brief.append(f'{len(changes):,} pairs of neighbouring characters '
                         'told apart differently')
        elif changes:
            lines.append(f'{len(changes)} pair(s) of neighbouring characters '
                         'told apart differently:')
            for x, y, ov, nv in changes:
                lines.append(f'  {short_character(x)} and {short_character(y)}: '
                             f'before {told_apart_words(ov)}, now '
                             f'{told_apart_words(nv)}')
            brief.append(f'{len(changes)} pair(s) of neighbouring characters '
                         'told apart differently: ' + ', '.join(
                             f'{short_character(x)} and {short_character(y)}'
                             for x, y, ov, nv in changes))
    return lines, '; '.join(brief)


# Printed under every report and every summary: what two machines that agree
# here can still disagree on.
CLOSING = ('!! Not measured: a rule that applies only to a particular '
           'combination of letters, such as a contraction ("ch" sorted as one '
           'letter), so two machines that agree here can still sort some longer '
           'strings apart. Where a character counts for nothing at some level '
           '(glibc ignores it there), how it is told apart from its neighbour is '
           'read less reliably, so a change there may go unseen. Only sort order '
           'is measured: upper(), lower() and the other character rules of '
           'LC_CTYPE are not.')


def machine_label(d):
    """How a measurement names its machine: the build, or the version."""
    return d['glibc_build'] or d['glibc_version'] + ' (build unknown)'


def tag_version(tag):
    """(2, 28) from the tag 'glibc-2.28'; None when it names no release,
    such as a commit id, or glibc-2.34.9000, which opens 2.35's development
    and is not 2.34."""
    m = re.fullmatch(r'glibc-(\d+)\.(\d+)', tag)
    return (int(m[1]), int(m[2])) if m else None


def wrapped(text, indent, more):
    return textwrap.wrap(text, width=78, initial_indent=' ' * indent,
                         subsequent_indent=' ' * more, break_on_hyphens=False)


def summary_lines(old, new, groups, pg_names, unmeasured, only_old, partly,
                  totals, warnings):
    """The report in brief, for the summary audit.sh prints at the end.

    Every locale is named, and so are the names PostgreSQL knows it by: a
    reader looks a locale up by the name the database uses, and "ko_KR" in a
    UTF-8 database is ko_KR.utf8. What moved is said in one phrase; the
    characters' names and how each pair is told apart are in the report. The
    report's warnings are here whole, so the summary needs no other copy.
    """
    o, n = old['locales'], new['locales']
    out = [f'   ({machine_label(old)} -> {machine_label(new)})']
    for text in warnings:
        out += wrapped(text, 5, 8)
    count = sum(len(names) for names, brief in groups)
    if groups:
        out.append(f'     {count} locale(s) sort differently -- reindex what '
                   'uses them:')
        for names, brief in groups:
            # Wrapped at the same indent, as in the report, so that where the
            # names end and what moved begins is plain.
            out += wrapped(heading(names, o, n), 7, 7)
            out += wrapped(brief, 9, 11)
            also = also_known_as(names, pg_names, o)
            if also:
                out += wrapped(also, 9, 11)
    else:
        out.append('     no locale measured on both machines sorts differently')
    if unmeasured or only_old:
        out.append(f'     NOT known to be unchanged: '
                   f'{len(unmeasured) + len(only_old)} locale(s)')
        if unmeasured:
            out += wrapped('could not be measured: '
                           + ', '.join(shown(x) for x in unmeasured), 7, 9)
            also = also_known_as(unmeasured, pg_names, o)
            if also:
                out += wrapped(also, 9, 11)
        if only_old:
            out += wrapped('not on the new machine, so a database or collation '
                           'that uses one cannot use it there: '
                           + ', '.join(shown(x) for x in only_old), 7, 9)
    if partly:
        out += wrapped('!! Unchanged only as far as measured, because a change '
                       'between an accent and case would not show in them: '
                       + ', '.join(shown(x) for x in partly), 5, 8)
    out += wrapped(totals, 5, 7)
    out += wrapped(CLOSING, 5, 8)
    return out


def compare(old_path, new_path, tags=None, summary_to=None):
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
    changed, same, unmeasured = [], [], []
    for name in both:
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
    # reads the same is printed under one heading. All of it is worked out
    # before the first line is printed, so a record that turns out damaged
    # stops the run before any part of a report is on the screen.
    measured_alike = {}
    for name in changed:
        key = (o[name]['order'], o[name]['pairs'], n[name]['order'], n[name]['pairs'])
        measured_alike.setdefault(key, []).append(name)
    reads_alike, briefs = {}, {}
    for names in measured_alike.values():
        lines, brief = describe_change(old, new, o[names[0]], n[names[0]])
        reads_alike.setdefault(tuple(lines), []).extend(names)
        briefs[tuple(lines)] = brief
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
    totals = (f'Of the {len(o)} locale(s) on the old machine: {len(changed)} '
              f'changed, {len(same)} unchanged{in_part}, {len(unmeasured)} not '
              f'measured, {len(only_old)} not on the new machine.')
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
    if summary_to:
        # Written before the report, so that a summary that cannot be written
        # stops the run with no report on the screen, like damage does.
        brief = summary_lines(
            old, new, [(names, briefs[lines]) for lines, names in groups],
            pg_names, unmeasured, only_old, partly, totals,
            [text for text, wrap in warnings])
        try:
            with open(summary_to, 'w', encoding='utf-8') as f:
                f.write('\n'.join(brief) + '\n')
        except OSError as e:
            die(f'cannot write {summary_to}: {e}')

    print(f'Old machine: {machine_label(old)}, {len(old["locales"])} locale(s)')
    print(f'New machine: {machine_label(new)}, {len(new["locales"])} locale(s)')
    for text, wrap in warnings:
        print(textwrap.fill(text, width=78, subsequent_indent='   ')
              if wrap else text)
    print()
    print(f'Sort order CHANGED in {len(changed)} locale(s):')
    for lines, names in groups:
        print()
        print(textwrap.fill(heading(names, o, n), width=78, initial_indent='  ',
                            subsequent_indent='  ', break_on_hyphens=False))
        also = also_known_as(names, pg_names, o)
        if also:
            print(textwrap.fill(also, width=78, initial_indent='    ',
                                subsequent_indent='      ',
                                break_on_hyphens=False))
        for line in lines:
            print(f'    {line}')
    print()
    print(f'Unchanged: {len(same)} locale(s).')
    if partly:
        print(textwrap.fill(
            f'!! Unchanged only as far as measured: {len(partly)} of them. In '
            'each, the pairs of neighbouring characters counted in brackets '
            'differ by less than a letter, and no pair could be found there to '
            'tell whether by an accent or by case; a change from one to the '
            'other would not show:', width=78, subsequent_indent='   '))
        print(textwrap.fill(
            ', '.join(f'{shown(name)} ({max(o[name]["partly"], n[name]["partly"])})'
                      for name in partly),
            width=78, initial_indent='  ', subsequent_indent='  ',
            break_on_hyphens=False))
    if unmeasured:
        print(f'!! Could not be measured, so NOT known to be unchanged: '
              f'{len(unmeasured)} locale(s):')
        for name in unmeasured:
            before, now = o[name].get('error'), n[name].get('error')
            if not before and not now:
                print(f'  {shown(name)}: measured over different characters on '
                      f'the two machines ({o[name]["chars"]:,} as '
                      f'{o[name]["codeset"]}, then {n[name]["chars"]:,} as '
                      f'{n[name]["codeset"]}), so the orders cannot be compared')
            elif before == now:
                print(f'  {shown(name)} (both machines): {before}')
            else:
                for side, error in (('old', before), ('new', now)):
                    if error:
                        print(f'  {shown(name)} ({side} machine): {error}')
            also = also_known_as([name], pg_names, o)
            if also:
                print(textwrap.fill(also, width=78, initial_indent='    ',
                                    subsequent_indent='      ',
                                    break_on_hyphens=False))
    if only_old:
        # Not measured on the new machine is not measured: a machine missing
        # its language packs would otherwise look like a clean upgrade.
        print(textwrap.fill(
            f'!! Not on the new machine, so NOT known to be unchanged: '
            f'{len(only_old)} locale(s). Either the upgrade removes them or '
            'their language pack is not installed there; a database or '
            'collation that uses one cannot use it on the new machine. If a '
            'language pack is missing, install it and measure the new machine '
            'again.', width=78, subsequent_indent='   '))
        for name in only_old:
            print(f'  {shown(name)}')
    print(f'Only on the new machine: {len(only_new)} locale(s).')
    for name in only_new:
        print(f'  {shown(name)}')
    print()
    print(totals)
    print(textwrap.fill(CLOSING, width=78, subsequent_indent='   '))
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
                         'moved')
    ap.add_argument('--tags', nargs=2, metavar=('OLD_TAG', 'NEW_TAG'),
                    help='with --compare: the glibc tags of the pair audited, '
                         'as audit.sh passes them; a file measured on another '
                         'glibc than its tag is refused')
    ap.add_argument('--summary-to', metavar='FILE',
                    help='with --compare: also write the report in brief to '
                         'FILE, for the summary audit.sh prints')
    ap.add_argument('--jobs', type=int,
                    help='processes to measure with (default: one per CPU, '
                         'fewer when half the free memory does not hold them '
                         'at about 0.3 GB each)')
    ap.add_argument('--extract', action='store_true',
                    help='measure, and write one compressed file with the '
                         'measurement, this machine\'s locale sources and its '
                         'glibc build (> old.tar)')
    ap.add_argument('--unpack', nargs=2, metavar=('FILE', 'DIR'),
                    help='check a file --extract wrote and lay it out in DIR, '
                         'which must not exist, for audit.sh')
    args = ap.parse_args()
    if (args.tags or args.summary_to) and not args.compare:
        die('--tags and --summary-to go with --compare')
    if sum(map(bool, (args.extract, args.compare, args.unpack))) > 1:
        die('--extract, --compare and --unpack are three different runs; '
            'give one')
    if args.unpack:
        return unpack(*args.unpack)
    if args.compare:
        return compare(*args.compare, tags=args.tags,
                       summary_to=args.summary_to)
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
