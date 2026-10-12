#!/usr/bin/env python3
"""
Step 2's comparison of the character sets locales are built with, and of the
collating elements they build out of transliterations, between two glibc
tags (backlog 13.7). Steps 6 to 8 make the same comparison between a machine
and its tag, or two machines, through node_charmaps.

localedef resolves every character an LC_COLLATE block names through the
charmap the locale is built with: the head of an order line, the members of a
`..` range, a collating-element or collating-symbol name, the characters of a
`from` string and of the leading `copy` string (ld-collate.c and
linereader.c, glibc-2.39). glibc builds each SUPPORTED entry with
`-f charmaps/<charset>` (localedata/Makefile). So a rule identical at both
tags sorts differently when the charmap gives a character it names other
bytes, or starts or stops defining it: the key of an order line is the bytes,
a range skips the names the charmap lacks, and a name the charmap lacks gets
no entry. Step 2's diff of localedata/locales/ cannot see any of that; this
compares localedata/charmaps/.

For the builds SUPPORTED lists at both tags (same entry, its source file at
both):

  1. The charmaps those builds use, compared by blob id. An identical blob
     gives every name identical bytes, and the build is not looked at again.
     That holds while localedef reads both tags' charmaps the same way:
     parse_charmap and charmap_new_char differ between 2.12 and 2.42 only in
     error reporting. The 2.36 reader changes touch bytes >= 0x80: lr_getc
     stops reading a 0xFF byte anywhere as the end of the file (19d4944459),
     and the files are read as UTF-8 instead of ISO-8859-1 (b15538d77c).
     Stated, not checked here: a charmap with such a byte, identical at two
     tags on either side of 2.36, would be passed over.
  2. For each pair of charmaps that differ, K: every name whose bytes differ,
     or which one side defines and the other does not (read_charmap, delta).
  3. Every LC_COLLATE block a build's run reads, at either tag, searched for
     K. The search is a superset of what glibc resolves through the charmap:
     every `<U....>` spelling, comments included, every raw non-ASCII
     character, the characters of each `copy` string, and every `..` range
     as the interval it spans. A hit found only inside comments is dropped,
     and only where the comment is certain (confirmed_hit).
  4. A build is listed when a file its run reads (the source and what its
     LC_COLLATE copies) names a member of K of the build's charmap pair. A
     build whose source step 3 lists anyway -- it changed inside
     LC_COLLATE, or copies one that did -- is counted, not repeated.
  5. A source SUPPORTED never builds in a changed charmap is searched the
     same way, because a distro can build it in that charmap: Red Hat builds
     en_US, en_GB, da_DK and sv_SE in ISO-8859-15
     (glibc-fedora-localedata-rh61908.patch), which upstream builds only in
     ISO-8859-1 and UTF-8. Not in a charmap whose code_set_name PostgreSQL
     maps to an encoding it never uses on a server (CLIENT_ONLY_CODESETS).
  6. The collating elements a build gets from transliterations. localedef
     reads a `from` string through the charmap, and a `<U....>` character
     the charmap lacks through the LC_CTYPE transliteration table of the
     file being read; the element is built out of the answer, or not at all
     (translate_unicode_codepoint, linereader.c:617-688@2.39). For every
     build of point 1 whose LC_COLLATE closure holds a file that defines
     such an element and has LC_CTYPE, each element is built at both tags
     with glibc's table, in glibc's reading order (TranslitRun, measured on
     RHEL 8, 9 and 10 as M1 of the 13.7 handoff), when the charmap pair's K
     is not empty, a file the run's LC_CTYPE can read differs, or the two
     tags read the same bytes differently (one holds UTF8_READER and the
     other not, and a file holds raw characters or bytes that are not
     UTF-8); the build is listed when one is built differently. With no
     charmap changed, it runs when a locale file's LC_CTYPE is read
     differently (ctype_moved) or the two tags read bytes differently
     (_readers_differ). A 0xFF byte in any file the run holds lists every
     build (_ff_gate).

What it cannot settle it lists with the reason, under a `!!` unless step 3
lists the locale anyway: for example a charmap of a shape this reader does
not model, a range whose start depends on what the run defined before it, a
`...` range or a form other than `..` and `....`, a second LC_COLLATE
section, a repertoiremap line, an escape_char this does not read with, a
range on a continued line, a copy target that is missing or copies in a
cycle, a code_set_name that differs between the two tags (glibc then refuses
to load the category, findlocale.c:256-296@2.39); and for point 6, a
transliteration spelled or reached in a way this does not model, a
translit_ignore line, an include or copy cycle, lookups in two files of one
run. A SUPPORTED this reads differently than glibc, or a charmap it names
that is not there, is NOT RUN.

Not covered here: a character the charmap gains that no rule names, which
takes the UNDEFINED weight -- step 4 lists every locale that relies on
UNDEFINED; an LC_COLLATE keyword built out of escape characters
(`LC_COLL/ATE`), which steps 2 and 3 do not see either, and an
`escape_char` directive spelled that way; the transliterations of a build
SUPPORTED does not list, which point 5 looks for only through the
characters a rule names.
"""
import bisect
import collections
import re

import glibc_locale_data as g

CHARMAPS_DIR = 'localedata/charmaps'

# The code_set_names PostgreSQL maps to an encoding it allows only on the
# client: SJIS, BIG5, GBK, UHC, JOHAB, GB18030 and SHIFT_JIS_2004
# (encoding_match_list in src/port/chklocale.c, compared with
# pg_strcasecmp, REL_13_23 and REL_18_6). CREATE COLLATION imports none of
# them (collationcmds.c:612-613@REL_13_23, :720-723@REL_18_6). A database
# in SQL_ASCII created by a superuser accepts any locale
# (check_encoding_locale_matches, dbcommands.c), which is why a build
# SUPPORTED lists in one of these is still compared; only the search for
# builds a distro could add (point 5 above) leaves these charmaps out.
CLIENT_ONLY_CODESETS = frozenset(name.upper() for name in (
    'SJIS', 'PCK', 'CP932', 'SHIFT_JIS',
    'BIG5', 'BIG5HKSCS', 'Big5-HKSCS', 'CP950',
    'GBK', 'CP936',
    'UHC', 'CP949',
    'JOHAB', 'CP1361',
    'GB18030', 'CP54936',
    'SJIS_2004'))

_WS = ' \t\r\f\v'
_U = r'<U([0-9A-Fa-f]{4}|[0-9A-Fa-f]{8})>'


class Unsettled(Exception):
    """Something this module does not model. The build stays listed."""


# --------------------------------------------------------------- SUPPORTED

Build = collections.namedtuple('Build', 'entry source charmap')


def supported_builds(text, tag):
    """[Build] for every entry of localedata/SUPPORTED, in order.

    The file is a makefile fragment: `SUPPORTED-LOCALES=\\` and one
    `<locale>/<charset> \\` per line, which localedata/Makefile splits at the
    slash and builds from `locales/<source>` with `-f charmaps/<charset>`.
    Lines with `=` and comment lines are skipped, as parse_supported skips
    them. A line of any other shape -- two words, no slash, two slashes -- is
    Unsettled: read loosely it drops a build or names the wrong charmap.
    """
    out = []
    for line in text.split('\n'):
        s = line.strip()
        if s.endswith('\\'):
            s = s[:-1].strip()
        if not s or s.startswith('#') or '=' in s:
            continue
        words = s.split()
        parts = words[0].split('/') if len(words) == 1 else ()
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise Unsettled(f"a line of {g.SUPPORTED} at {tag} this tool does "
                            f"not read: {line.strip()!r}")
        out.append(Build(parts[0], g.locale_source(parts[0]), parts[1]))
    if not out:
        raise Unsettled(f"{g.SUPPORTED} at {tag} lists no locale")
    return out


# ----------------------------------------------------------------- charmaps

Charmap = collections.namedtuple('Charmap',
                                 'name code_set_name singles ranges starts')
Charmap.__doc__ = """A charmap's name table as localedef builds it.

singles: {code point: '/xhh...' in lower case}, the first definition
winning; ranges: sorted [(lo, hi, first bytes as an int, number of bytes)],
none overlapping another or a single; starts: [lo] of each, for bisect."""

_PROLOG_LINE = re.compile(
    r'<(code_set_name|mb_cur_min|mb_cur_max|comment_char|escape_char)>'
    r'[ \t]+(\S+)')
_CODE_SET_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.:+-]*')
# The words the charmap reader takes as keywords even outside `<...>`
# (flag 0 in charmap-kw.gperf:38-42@2.28, the same at 2.39). Read as the
# argument of <code_set_name> one is a keyword, not a name (get_ident,
# linereader.c:569-575@2.28): a bad argument, and glibc stores no name
# (charmap.c:377-385), then the file's (:200-203). Round 4 of the PR D
# review.
_KEYWORDS = frozenset(('CHARMAP', 'END', 'WIDTH', 'WIDTH_VARIABLE',
                       'WIDTH_DEFAULT'))
_END_CHARMAP = re.compile(r'END[ \t]+CHARMAP')


def read_charmap(text, name):
    """The name table glibc builds from a charmap, or Unsettled.

    Modelled on parse_charmap (charmap.c:309-850@2.39) and the line reader
    it uses, and strict: every line must be one of the shapes upstream
    charmaps use, and anything else is Unsettled rather than guessed. glibc
    drops some malformed lines with a warning and refuses to write the
    locale for others (record_error), and a reader that skipped them would
    model neither.

    Before CHARMAP: blank lines, comments, and `<code_set_name>`,
    `<mb_cur_min>`, `<mb_cur_max>`, `<comment_char>` (`%` or `#`) and
    `<escape_char>` (`/` or `\\`), each at most once. The reader starts at
    `#` and `\\` (lr_create, linereader.c:79-80@2.39) and a directive changes
    them from the next line on. `<include>` makes localedef exit
    (charmap.c:476), so it is Unsettled like any other keyword.

    The body, CHARMAP to END CHARMAP: blank lines, comments, and lines
    `<Uxxxx>` or `<Uxxxx>..<Uyyyy>`, blanks, and the bytes as `/xhh`, which
    end at a blank or the end of the line; what follows is ignored
    (lr_ignore_rest, charmap.c:574). glibc stores every `<U>` name as U and
    eight hex digits (charmap.c:519-522), so the four- and eight-digit
    spellings are one name. A range gives its members the bytes counted up
    big-endian from the first (charmap.c:1078-1096); one that runs past the
    last value its byte count holds, ends below its start, or overlaps
    another range or a single line, is Unsettled. A data line that ends in
    the escape character joins the next line, which the reader then drops
    (lr_next and lr_ignore_rest, linereader.c:114-171): Unsettled. A comment
    line that ends in it drops only itself (linereader.c:216-231), measured
    in M3 of the 13.7 handoff. A byte count outside mb_cur_min..mb_cur_max,
    or over 16 (linereader.h:52), is Unsettled.

    After END CHARMAP only WIDTH sections may follow (_check_tail).
    """
    seen, cc, ec, pos = read_prolog(text, name)
    if 'code_set_name' not in seen:
        raise Unsettled(f"charmap {name}: no <code_set_name>")
    mb_max = int(seen.get('mb_cur_max', '1'))
    mb_min = int(seen.get('mb_cur_min', str(mb_max)))
    if mb_min > mb_max:
        raise Unsettled(f"charmap {name}: <mb_cur_min> {mb_min} is over "
                        f"<mb_cur_max> {mb_max}")
    return _read_body(text, name, seen, cc, ec, pos, mb_min, mb_max)


def read_prolog(text, name):
    """(the prolog's declarations {keyword: argument}, comment char, escape
    char, offset of the line after CHARMAP), read as read_charmap reads it:
    every line before CHARMAP blank, a comment or one declaration, each at
    most once, or Unsettled."""
    cc, ec = '#', '\\'
    seen = {}
    pos, lineno = 0, 0
    while True:
        if pos > len(text):
            raise Unsettled(f"charmap {name}: no CHARMAP line")
        nl = text.find('\n', pos)
        nl = len(text) if nl < 0 else nl
        line = text[pos:nl]
        pos, lineno = nl + 1, lineno + 1
        s = line.strip(_WS)
        if s.startswith(cc):
            continue
        if line.endswith(ec):
            raise Unsettled(f"charmap {name}: line {lineno} is continued at "
                            f"the escape character")
        if not s:
            continue
        if s == 'CHARMAP':
            break
        m = _PROLOG_LINE.fullmatch(s)
        if not m:
            raise Unsettled(f"charmap {name}: line {lineno}, before CHARMAP, "
                            f"is not one this tool reads: {s[:60]!r}")
        key_, arg = m.groups()
        if key_ in seen:
            raise Unsettled(f"charmap {name}: <{key_}> given twice")
        if ((key_ == 'comment_char' and arg not in ('%', '#'))
                or (key_ == 'escape_char' and arg not in ('/', '\\'))
                or (key_ in ('mb_cur_min', 'mb_cur_max')
                    and not (arg.isdigit() and int(arg) >= 1))
                or (key_ == 'code_set_name'
                    and (not _CODE_SET_NAME.fullmatch(arg)
                         or arg in _KEYWORDS))):
            raise Unsettled(f"charmap {name}: <{key_}> {arg}")
        seen[key_] = arg
        if key_ == 'comment_char':
            cc = arg
        elif key_ == 'escape_char':
            ec = arg
    return seen, cc, ec, pos


def _read_body(text, name, seen, cc, ec, pos, mb_min, mb_max):
    # The body ends at the first line that is END CHARMAP. A line that
    # opens with END in any other way is left in the body, where it is a
    # line of no shape this reads.
    end = pos - 1
    while True:
        end = text.find('\nEND', end)
        if end < 0:
            raise Unsettled(f"charmap {name}: no END CHARMAP line")
        eol = text.find('\n', end + 1)
        eol = len(text) if eol < 0 else eol
        if _END_CHARMAP.fullmatch(text[end + 1:eol].strip(_WS)):
            break
        end += 1
    body = text[pos:end] if end >= pos else ''
    _check_tail(text[eol + 1:].split('\n'), cc, ec, name)

    e = re.escape(ec)
    found = re.findall(r'^[ \t]*' + _U + r'(?:\.\.' + _U + r')?[ \t]+((?:'
                       + e + r'x[0-9A-Fa-f]{2})+)(?=[ \t\r]|$)', body, re.M)
    # Every line must be data, a comment or blank. Counted from the line
    # starts when every line opens at column 0 with `<`, the comment
    # character or nothing, which is what upstream charmaps do; read line by
    # line otherwise.
    lines = body.split('\n') if body else []
    first = lambda c: body.count('\n' + c) + body.startswith(c)
    if not (len(found) == first('<') and
            first('<') + first(cc) + lines.count('') == len(lines)):
        ignorable = len(re.findall(r'^[ \t\r\f\v]*(?:' + re.escape(cc)
                                   + r'[^\n]*)?$', body, re.M)) if body else 0
        if len(found) + ignorable != len(lines):
            raise Unsettled(f"charmap {name}: "
                            f"{len(lines) - ignorable - len(found)} line(s) "
                            f"between CHARMAP and END CHARMAP of a shape this "
                            f"tool does not read")
    # A data line continued at the escape character drops the next line; a
    # comment line continued at it drops only itself.
    if (body.count(ec + '\n') or body.endswith(ec)) and re.search(
            r'^[ \t\r\f\v]*[^ \t\r\f\v\n' + re.escape(cc) + r'][^\n]*' + e
            + r'$', body, re.M):
        raise Unsettled(f"charmap {name}: a data line ends in the escape "
                        f"character, which joins the next line to it")

    width = len(ec) + 3
    for size in {len(code) for _, _, code in found}:
        nbytes = size // width
        if not mb_min <= nbytes <= mb_max or nbytes > 16:
            raise Unsettled(f"charmap {name}: a character has {nbytes} "
                            f"byte(s), outside {mb_min}..{mb_max}")
    if ec != '/':
        found = [(lo, hi, code.replace(ec + 'x', '/x'))
                 for lo, hi, code in found]
    joined = ''.join(code for _, _, code in found)
    if joined != joined.lower():
        found = [(lo, hi, code.lower()) for lo, hi, code in found]
    # The first definition wins (charmap.c:983-984, simple-hash.c:96-113).
    singles = {int(lo, 16): code for lo, hi, code in reversed(found)
               if not hi}
    ranges = sorted((int(lo, 16), int(hi, 16),
                     int(code.replace('/x', ''), 16), len(code) // 4)
                    for lo, hi, code in found if hi)
    for a, b, start, nbytes in ranges:
        if a > b or start + (b - a) >= 256 ** nbytes:
            raise Unsettled(f"charmap {name}: the range from U+{a:04X} ends "
                            f"below its start or runs past {nbytes} byte(s)")
    starts = [r[0] for r in ranges]
    if ranges:
        keys = sorted(singles)
        for k, (a, b, _, _) in enumerate(ranges):
            if k and a <= ranges[k - 1][1]:
                raise Unsettled(f"charmap {name}: two ranges overlap at "
                                f"U+{a:04X}")
            if bisect.bisect_right(keys, b) != bisect.bisect_left(keys, a):
                raise Unsettled(f"charmap {name}: a single line inside the "
                                f"range that starts at U+{a:04X}")
    return Charmap(name, seen['code_set_name'], singles, ranges, starts)


def _check_tail(lines, cc, ec, name):
    """What follows END CHARMAP: blank lines, comments, `WIDTH_DEFAULT <n>`,
    and WIDTH or WIDTH_VARIABLE sections closed by their END line. glibc
    refuses to write the locale when the file ends anywhere else (state 91,
    charmap.c:854-856@2.39). The sections' contents are not read: WIDTH
    feeds wcwidth only (ld-ctype.c; ld-collate.c never reads it at 2.12,
    2.28, 2.39 or 2.42)."""
    section = None
    for line in lines:
        s = line.strip(_WS)
        if s.startswith(cc):
            continue
        if line.endswith(ec):
            raise Unsettled(f"charmap {name}: a line after END CHARMAP is "
                            f"continued at the escape character")
        if not s:
            continue
        if section is None:
            if s in ('WIDTH', 'WIDTH_VARIABLE'):
                section = s
            elif not re.fullmatch(r'WIDTH_DEFAULT[ \t]+\d+', s):
                raise Unsettled(f"charmap {name}: after END CHARMAP, a line "
                                f"this tool does not read: {s[:60]!r}")
        elif re.fullmatch(r'END[ \t]+' + section, s):
            section = None
        elif s.startswith('END'):
            raise Unsettled(f"charmap {name}: {s[:40]!r} inside {section}")
    if section is not None:
        raise Unsettled(f"charmap {name}: the file ends inside {section}")


def key(cm, cp):
    """The bytes glibc gives code point `cp` in this charmap, as
    '/xhh...', or None when the charmap does not define it."""
    k = cm.singles.get(cp)
    if k is not None:
        return k
    r = _affine(cm, cp)
    if r is None:
        return None
    offset, nbytes = r
    return ''.join('/x%02x' % b for b in (cp + offset).to_bytes(nbytes, 'big'))


def _affine(cm, cp):
    """(value minus code point, nbytes) of the range holding cp, or None."""
    k = bisect.bisect_right(cm.starts, cp) - 1
    if k < 0:
        return None
    a, b, start, nbytes = cm.ranges[k]
    if cp > b:
        return None
    return start - a, nbytes


def delta(old, new):
    """K: the set of code points whose bytes differ between two charmaps,
    or which one defines and the other does not.

    Exact for both kinds of line. A code point either side gives a single
    line is compared on its own, through the other side's single or range.
    The rest is compared range against range over the pieces between the
    ranges' ends: two ranges give a piece the same bytes when they have the
    same byte count and the same distance from code point to value, and a
    range against no range is a difference.
    """
    so, sn = old.singles, new.singles
    out = set()
    for c, v in so.items():
        w = sn.get(c)
        if (key(new, c) if w is None else w) != v:
            out.add(c)
    for c, w in sn.items():
        if c not in so and key(old, c) != w:
            out.add(c)
    moved = sorted(set(old.ranges) ^ set(new.ranges))
    if moved:
        # Only where a range differs: the pieces between the ends of every
        # range that overlaps the spans of the ranges that moved.
        spans = []
        for a, b, _, _ in moved:
            if spans and a <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        for lo, hi in spans:
            cuts = {lo, hi + 1}
            for cm in (old, new):
                k = max(bisect.bisect_right(cm.starts, lo) - 1, 0)
                while k < len(cm.ranges) and cm.ranges[k][0] <= hi:
                    a, b = cm.ranges[k][:2]
                    cuts |= {c for c in (a, b + 1) if lo <= c <= hi + 1}
                    k += 1
            cuts = sorted(cuts)
            for a, b in zip(cuts, cuts[1:]):
                if _affine(old, a) != _affine(new, a):
                    out.update(c for c in range(a, b)
                               if c not in so and c not in sn)
    return frozenset(out)


class K:
    """K of one charmap pair, as the search asks it: code points, and their
    spellings as upper-case hex of four (up to U+FFFF) and eight digits.
    added, removed: how many members only the new or only the old charmap
    defines; the rest have other bytes."""

    def __init__(self, members, old=None, new=None):
        self.members = members
        self.size = len(members)
        self.added = self.removed = 0
        if old is not None:
            for c in members:
                if key(old, c) is None:
                    self.added += 1
                elif key(new, c) is None:
                    self.removed += 1
        spell = set()
        for c in members:
            if c <= 0xFFFF:
                spell.add('%04X' % c)
            spell.add('%08X' % c)
        self.spell = frozenset(spell)

    def within(self, lo, hi):
        """The members in [lo, hi], once per interval: the range of
        iso14651_t1 is asked about by every build that copies it."""
        memo = self.__dict__.setdefault('_within', {})
        got = memo.get((lo, hi))
        if got is None:
            if hi - lo < len(self.members):
                got = frozenset(c for c in range(lo, hi + 1)
                                if c in self.members)
            else:
                got = frozenset(c for c in self.members if lo <= c <= hi)
            memo[(lo, hi)] = got
        return got

    def named(self, names):
        """The members a block's Names hold."""
        return ({int(h, 16) for h in names.spell & self.spell}
                | (names.raw & self.members))


# ------------------------------------------------------- LC_COLLATE search

# A <U....> name with an escape character anywhere inside the brackets,
# before the U too: get_symname keeps the character after it
# (linereader.c:516-524@2.39), so `<U00/41>` and `</U0041>` are U+0041.
# Either escape character may be in force. _escaped_name decides what the
# match names.
_ESC_UNAME = re.compile(r'<(?=[^<>\n]*[/\\])[/\\]*U[0-9A-Fa-f/\\]*>')
# lr_token skips every isspace before a token (linereader.c:189-200@2.39),
# not only blanks and tabs, and needs none after `copy`: get_ident ends the
# word at a `"` (:580), so `copy"x"` is a copy of x (backlog 13.17).
_COPY_LINE = re.compile(r'[ \t\r\f\v]*copy[ \t\r\f\v]*"([^"\n]*)"')
_HEAD = re.compile(r'[ \t\r\f\v]*' + _U)
_HEAD_M = re.compile(r'^[ \t\r\f\v]*' + _U, re.M)
# An ellipsis token (linereader.c:244-286@2.39): a run of two dots or more,
# or a step-2 form.
_ELLIPSIS = re.compile(r'\.{2,}(?:\(2\)\.+)?')
_INLINE_RANGE = re.compile(_U + r'[ \t]*(\.{2,}(?:\(2\)\.+)?)[ \t]*' + _U)

Names = collections.namedtuple('Names', 'spell raw')
Names.__doc__ = """What a block names that glibc could resolve through the
charmap. spell: upper-case hex of every <U....> spelling, escapes inside the
brackets removed; raw: code points of every raw non-ASCII character and of
the characters of each copy string."""


def _lines_with(blk, needle):
    """[(offset, line)] for every line of `blk` holding `needle`, each once,
    found with str.find: a regex over every line of a 4.5 MB block is what
    made the discarded attempt slow (13.7 handoff, step 3)."""
    out, i, last = [], 0, -1
    while True:
        i = blk.find(needle, i)
        if i < 0:
            return out
        ls = blk.rfind('\n', 0, i) + 1
        if ls != last:
            le = blk.find('\n', i)
            out.append((ls, blk[ls:le if le >= 0 else len(blk)]))
            last = ls
        i += len(needle)


class Scan:
    """One locale text's LC_COLLATE, as far as the search needs it.

    has_block: a line opening LC_COLLATE at column 0, as collate_bounds
    reads it. block: from the line after it to the first END LC_COLLATE at
    column 0, or the end of the file. What depends on the block alone is
    read once per distinct block (Block): a file that changed outside
    LC_COLLATE has the same block at both tags. odd: why the block cannot
    be read with certainty when K is not empty, or None. The rest is
    Block's, read with this file's comment character.
    """

    def __init__(self, text):
        self.text = text
        self.has_block = False
        self.block = ''
        self.odd_keyword = None
        self._info = None
        if 'LC_COLLATE' not in text:
            return
        if text.startswith('LC_COLLATE'):
            head = 0
        else:
            head = text.find('\nLC_COLLATE')
            head = head + 1 if head >= 0 else -1
        self._odd_keywords(head)
        if 'repertoiremap' in text and re.search(
                r'^[ \t\r\f\v]*repertoiremap', text, re.M):
            # H10: a repertoire map makes the names a charmap lacks resolve
            # through it instead (ld-collate.c:966-971, linereader.c:632-637
            # @2.39). Unused upstream; one comment in sgs_LT names it.
            self.odd_keyword = ('a repertoiremap line, which this tool '
                                'does not model')
        i, ec = text.find('escape_char'), '\\'
        while i >= 0:
            # Every search here knows `/` and `\\` as escapes. glibc takes
            # any one-character word (locfile.c:123-149@2.39), and a
            # directive line that ends in the escape in force takes its
            # argument from the next line (lr_next, linereader.c:152-171),
            # as does one on a line the line above continues.
            ls = text.rfind('\n', 0, i) + 1
            if not text[ls:i].strip(_WS):
                nl = text.find('\n', i)
                line = text[ls:nl if nl >= 0 else len(text)]
                arg = text[i + 11:nl if nl >= 0 else len(text)].split()
                above = text[max(ls - 2, 0):max(ls - 1, 0)]
                if (not arg or arg[0] not in ('/', '\\')
                        or line.endswith(ec) or above in ('/', '\\')):
                    self.odd_keyword = ('an escape_char line this tool does '
                                        'not read with certainty')
                else:
                    ec = arg[0]
            i = text.find('escape_char', i + 11)
        if head < 0:
            return
        self.has_block = True
        body = text.find('\n', head)
        body = len(text) if body < 0 else body + 1
        end = text.find('\nEND LC_COLLATE', body - 1)
        self.block = text[body:end + 1 if end >= 0 else len(text)]

    def _odd_keywords(self, head):
        """H11 of the 13.7 handoff: glibc reads every LC_COLLATE section of
        a file and takes the keyword after leading blanks (locfile.c:178-182,
        lr_token). A keyword that opens a line, other than the one block
        read here, is a section the search does not see. Mid-line it is an
        argument (`category "i18n:2012";LC_COLLATE` in LC_IDENTIFICATION) or
        part of a comment, never a section."""
        text = self.text
        i = text.find('LC_COLLATE')
        while i >= 0:
            ls = text.rfind('\n', 0, i) + 1
            if text[ls:i].strip(_WS) == '' and i != head:
                self.odd_keyword = ('a second LC_COLLATE section, or the '
                                    'keyword after blanks')
            i = text.find('LC_COLLATE', i + 10)

    @property
    def info(self):
        if self._info is None:
            self._info = _block(self.block)
        return self._info

    @property
    def copies(self):
        return self.info.copies if self.has_block else ()

    def _ranges(self):
        return self.info.ranges(lambda: _reading(self.text))

    @property
    def odd(self):
        return self.odd_keyword or (self._ranges().odd if self.has_block
                                    else None)

    @property
    def absolute(self):
        return self.has_block and self._ranges().absolute

    @property
    def inline(self):
        return self._ranges().inline if self.has_block else ()

    @property
    def line_ranges(self):
        return self._ranges().line_ranges if self.has_block else ()

    def names(self):
        return self.info.names()

    def may_define(self, cp):
        """Could some line of this block define code point cp? True when
        any spelling of it appears anywhere in the block: a superset."""
        names = self.names()
        spell = {'%08X' % cp} | ({'%04X' % cp} if cp <= 0xFFFF else set())
        return bool(spell & names.spell) or cp in names.raw


Ranges = collections.namedtuple('Ranges', 'inline line_ranges odd absolute')
Ranges.__doc__ = """inline: [(lo, hi)] of `<U>..<U>` and `<U>....<U>`
ranges; `....` names a subset of what `..` names between the same ends
(it counts the digits in decimal, ld-collate.c:1325-1398@2.39).
line_ranges: offsets in the block of the lines that open with `..` or
`....`, whose ends come from the run (range_interval). odd: a range form
this does not model, or None. absolute: the block uses `...`, which walks
the charmap's byte table (ld-collate.c:1194@2.39) and so can change with
the blob even when K is empty."""


class Block:
    """What one LC_COLLATE block names and copies, read once per block."""

    def __init__(self, blk):
        self.blk = blk
        self._names = None
        self._ranges = {}
        # A line ending in the escape character is joined to the next one,
        # and that one to the next if it ends in it too (lr_next,
        # linereader.c:152-171@2.39). Which of `/` and `\\` is the escape
        # is the file's, so both joins are read, in addition to the lines as
        # they are: a superset. `joined` holds the logical lines the joins
        # make, and `continued` the joined texts whole, for the names.
        escs = [esc for esc in ('/', '\\') if esc + '\n' in blk]
        self.continued = [blk.replace(esc + '\n', '') for esc in escs]
        joined = []
        for esc in escs:
            cur = None
            for line in blk.split('\n'):
                cur = line if cur is None else cur[:-1] + line
                if cur.endswith(esc):
                    continue
                if cur != line:
                    joined.append(cur)
                cur = None
            if cur is not None:
                joined.append(cur)
        self.joined = joined
        copies = []
        lines = [line for _, line in _lines_with(blk, 'copy')]
        lines += [j for j in joined if 'copy' in j]
        if lines:
            for line in lines:
                m = _COPY_LINE.match(line)
                if m:
                    copies.append(g.decode_symbolic(m.group(1)))
        self.copies = tuple(dict.fromkeys(copies))
        self.dotted = _lines_with(blk, '..') if '..' in blk else []

    def ranges(self, comment_char):
        """Ranges, read with the comment and escape characters
        `comment_char()` gives. They are asked for only when the block, read
        with no comment at all,
        has a range to place: glibc_locale_data.reading_chars reads the
        whole file, about 40 ms for iso14651_t1_common, and that block's
        dots are all between two collating-symbol names."""
        got = self._ranges.get(None)
        if got is None:
            got = self._ranges[None] = self._classify_all(None)
        if got == (( ), (), None, False):
            return got
        chars = comment_char()
        if chars not in self._ranges:
            self._ranges[chars] = self._classify_all(chars)
        return self._ranges[chars]

    def _classify_all(self, chars):
        acc = {'inline': [], 'line': [], 'odd': None, 'absolute': False}
        for start, line in self.dotted:
            code = line[:_certain_comment_start(line, chars)]
            if '..' in code:
                _classify(code, start, acc)
        for line in self.joined:
            code = line[:_certain_comment_start(line, chars)]
            if '..' in code:
                _classify(code, None, acc)
        return Ranges(tuple(acc['inline']), tuple(acc['line']), acc['odd'],
                      acc['absolute'])

    def names(self):
        """Names, read once per block and only when some K is not empty,
        in the block as it is and as the continued lines join it."""
        if self._names is None:
            blk = '\n'.join([self.blk] + self.continued)
            found = _UNAME.findall(blk)
            joined = ''.join(found)
            spell = (set(found) if joined == joined.upper()
                     else {h.upper() for h in found})
            # A name with an escape inside the brackets either opens with
            # `<U`, and then `<U` occurs more often than the plain names, or
            # with `<` and the escape character.
            if (blk.count('<U') != len(found) or '</' in blk
                    or '<\\' in blk):
                for m in _ESC_UNAME.findall(blk):
                    cp = _escaped_name(m)
                    if cp is not None:
                        spell.add('%08X' % cp)
            raw = set()
            if not blk.isascii():
                raw = {ord(c) for c in set(''.join(
                    re.findall(r'[^\x00-\x7f]+', blk)))}
            for target in self.copies:
                raw |= {ord(c) for c in target}
            self._names = Names(frozenset(spell), frozenset(raw))
        return self._names

    def range_ends(self, offset, comment_char):
        """(start, end) of the line range at `offset` as this block alone
        decides them, or None: the <U> name of the nearest line of code
        above and below, the one above opening no other line of the block
        (range_interval). Once per block, reading characters and offset.

        None as well when a line from the one before `above` to `below`
        ends in either escape character: glibc joins it to the next
        (lr_next), so `above` may be a weight of an earlier line, which
        does not move the cursor, and the range may not stand alone. And
        when the start is spelled with an escape inside the brackets, or as
        a raw character opening a line, anywhere in the block: another line
        may define it, which the count of plain heads does not see.
        """
        chars = comment_char()
        memo = self.__dict__.setdefault('_ends', {})
        if (offset, chars) in memo:
            return memo[(offset, chars)]
        blk = self.blk

        def code(line):
            return line[:_certain_comment_start(line, chars)].strip(_WS)
        above = below = None
        pos = top = offset
        while pos > 0 and above is None:
            ls = blk.rfind('\n', 0, pos - 1) + 1
            above = code(blk[ls:pos - 1]) or None
            pos = top = ls
        nl = blk.find('\n', offset)
        pos = bottom = nl + 1 if nl >= 0 else len(blk) + 1
        while pos <= len(blk) and below is None:
            le = blk.find('\n', pos)
            le = len(blk) if le < 0 else le
            below = code(blk[pos:le]) or None
            pos = bottom = le + 1
        before = blk.rfind('\n', 0, max(top - 1, 0)) + 1 if top else top
        span = blk[before:max(bottom - 1, before)].split('\n')
        got = None
        mb = _HEAD.match(above) if above else None
        ma = _HEAD.match(below) if below else None
        if mb and ma and not any(line.endswith(('/', '\\')) for line in span):
            start, end = int(mb.group(1), 16), int(ma.group(1), 16)
            # Over the block as it is and as its continued lines join it,
            # where a split name becomes one.
            texts = [blk] + self.continued
            once = all(sum(1 for m in _HEAD_M.finditer(t)
                           if int(m.group(1), 16) == start) == 1
                       for t in texts)
            raw = start > 0x7f and any(
                re.search('^[ \t\r\f\v]*' + re.escape(chr(start)), t, re.M)
                for t in texts)
            escaped = any(_escaped_name(m) == start
                          for t in texts for m in _ESC_UNAME.findall(t))
            if once and not raw and not escaped:
                got = (start, end)
        memo[(offset, chars)] = got
        return got


def _classify(code, start, acc):
    """Sort the ellipsis of one line of code where glibc resolves it
    through the charmap: as the line's first token (a range whose ends come
    from the run), between two names that open the line (an order line's
    range of heads), or between the names a collating-symbol or
    collating-element declares. Anywhere else it is in the weights, which
    never touch the charmap (find_element, ld-collate.c:638-672@2.39); their
    names are searched with every other name anyway. Between two names that
    are not both <U> it names no charmap entry: read_charmap accepts only
    <U> names. `start` is None for a joined line, where a line range cannot
    be placed."""
    def odd(why):
        acc['odd'] = acc['odd'] or why
    stripped = code.lstrip(_WS)
    m = _ELLIPSIS.match(stripped)
    if m:
        form = m.group(0)
        if form == '...':
            acc['absolute'] = True
        elif form not in ('..', '....'):
            odd(f"a {form!r} range")
        elif start is None:
            odd('a range on a continued line')
        else:
            acc['line'].append(start)
        return
    d = re.match(r'collating-(?:symbol|element)[ \t]+', stripped)
    if d:
        stripped = stripped[d.end():]
    m = _INLINE_RANGE.match(stripped)
    if m:
        a, form, b = m.groups()
        lo, hi = int(a, 16), int(b, 16)
        if form == '...':
            acc['absolute'] = True
        elif form not in ('..', '....'):
            odd(f"a {form!r} range")
        elif lo > hi:
            odd('a range that ends below its start')
        else:
            acc['inline'].append((lo, hi))
        return
    m = re.match(r'(<[^<>\s]*>)[ \t]*(\.{2,}(?:\(2\)\.+)?)', stripped)
    if m and (m.group(1).startswith('<U') or d) and \
            m.group(2) not in ('..', '....'):
        odd(f"a {m.group(2)!r} range")


def _escaped_name(m):
    """The code point a `<U..>` name with escapes inside it names, or
    None."""
    h = m[1:-1].replace('/', '').replace('\\', '')
    return int(h[1:], 16) if re.fullmatch('U' + _U[2:-1], h) else None


_BLOCKS = {}


def _block(blk):
    got = _BLOCKS.get(blk)
    if got is None:
        got = _BLOCKS[blk] = Block(blk)
    return got


_UNAME = re.compile(_U)


def _reading(text):
    """(comment char, escape char) localedef reads `text` with, or None when
    glibc_locale_data.reading_chars cannot settle them."""
    return g.reading_chars(text)


def _certain_comment_start(line, chars):
    """Offset where a comment surely starts on this physical line, or
    len(line).

    glibc takes the comment character as a comment only where a token
    starts (lr_token, linereader.c:189-214@2.39): after blanks, after a
    symbol's `>`, a string's closing quote, a `;` or a `,`. Inside a word it
    is part of the word, and inside `<...>` or `"..."` part of the symbol or
    string; the file's escape character takes the next character as it is,
    and the other one is a plain character (get_string and get_ident,
    linereader.c:560-589, :773ff@2.39). Where this cannot tell -- after a
    number or a byte value, which also end a token -- it says not a comment,
    so a name there is still searched: the error that costs is calling code
    a comment. With the characters unknown (chars None), nothing is a
    comment.
    """
    if chars is None:
        return len(line)
    cc, ec = chars
    if cc not in line:
        return len(line)
    i, n = 0, len(line)
    in_sym = in_str = False
    boundary = True
    while i < n:
        ch = line[i]
        if ch == ec and not boundary:
            i += 2
            continue
        if in_sym:
            if ch == '>':
                in_sym, boundary = False, True
            i += 1
            continue
        if in_str:
            if ch == '"':
                in_str, boundary = False, True
            i += 1
            continue
        if ch == cc and boundary:
            return i
        if ch in ' \t\r\f\v;,':
            boundary = True
        elif ch == '<':
            in_sym, boundary = True, False
        elif ch == '"':
            in_str, boundary = True, False
        else:
            boundary = False
        i += 1
    return n


def confirmed_hit(scan, k):
    """Does the block name a member of K outside a certain comment?

    Every place the block names one is read -- each `<U....>` spelling in
    either case, each name with an escape inside the brackets, each raw
    character -- on its own physical line, with the file's own comment and
    escape characters (glibc_locale_data.reading_chars). A comment never runs
    past its physical line (linereader.c:194-214@2.39). The hit stays a hit
    when that cannot be decided: characters the reader cannot settle, a
    block with a line continued at either escape character (a name can then
    be split across two lines, or start in a token the line before opened),
    a member named only by a `copy` string, or a member the names hold and
    no position below shows.
    """
    chars = g.reading_chars(scan.text)
    if chars is None:
        return True
    blk = scan.block
    if '/\n' in blk or '\\\n' in blk:
        return True
    if any(ord(c) in k.members for t in scan.copies for c in t):
        return True
    places = [m.start() for m in _UNAME.finditer(blk)
              if int(m.group(1), 16) in k.members]
    places += [m.start() for m in _ESC_UNAME.finditer(blk)
               if _escaped_name(m.group(0)) in k.members]
    if not blk.isascii():
        places += [m.start() for m in re.finditer(r'[^\x00-\x7f]', blk)
                   if ord(m.group(0)) in k.members]
    if not places:
        return True
    for i in places:
        ls = blk.rfind('\n', 0, i) + 1
        le = blk.find('\n', i)
        line = blk[ls:le if le >= 0 else len(blk)]
        if i - ls < _certain_comment_start(line, chars):
            return True
    return False


# ------------------------------------------------------------- one tag

class Side:
    """One tag: the locale file names it holds, their texts when read, and
    its charmaps' blob ids and texts (by charmap name). The texts are needed
    only when a charmap a build uses differs. A text identical at both tags
    should be the same object on both Sides: the per-text work is then done
    once."""

    def __init__(self, tag, present, texts, charmap_oids, charmap_texts):
        self.tag = tag
        self.present = present
        self.texts = texts
        self.charmap_oids = charmap_oids
        self.charmap_texts = charmap_texts
        self._closure = {}
        self._charmaps = {}
        # Set by the caller that knows the repository: asks git whether
        # this tag holds UTF8_READER (True, False, or None for "cannot
        # tell").
        self.ask_utf8 = None

    def reads_utf8(self):
        """Does this tag's localedef read its input as UTF-8? True, False,
        or None when nobody could tell, which counts as not known. Asked at
        most once per Side, when compare needs to know whether the two tags
        read bytes alike: a run where nothing else runs point 6, a table or
        a defining file with a raw character, a file with a byte that is not
        UTF-8 above its LC_CTYPE."""
        if not hasattr(self, '_utf8'):
            self._utf8 = self.ask_utf8() if self.ask_utf8 else None
        return self._utf8

    def scan(self, name):
        return _scan(self.texts[name])

    def closure(self, name):
        """The files a build of `name` reads LC_COLLATE from, in the order
        a depth-first walk meets them: `name`, then every `copy` target,
        recursively. A target that is not at this tag, or a cycle, is
        Unsettled: localedef fails or does not finish."""
        got = self._closure.get(name)
        if got is None:
            out, path = [], []

            def walk(n):
                if n in path:
                    raise Unsettled(f"{n} copies its LC_COLLATE in a cycle "
                                    f"at {self.tag}")
                if n in out:
                    return
                if n not in self.texts:
                    where = ('was not read' if n in self.present
                             else 'is not there')
                    raise Unsettled(f"{path[-1]} copies {n}, which {where} "
                                    f"at {self.tag}" if path else
                                    f"{n} {where} at {self.tag}")
                out.append(n)
                path.append(n)
                for t in self.scan(n).copies:
                    walk(t)
                path.pop()
            try:
                walk(name)
                got = tuple(out)
            except Unsettled as e:
                got = e
            self._closure[name] = got
        if isinstance(got, Unsettled):
            raise got
        return got

    def charmap(self, name):
        got = self._charmaps.get(name)
        if got is None:
            text = self.charmap_texts.get(name)
            try:
                if text is None:
                    raise Unsettled(f"charmap {name} was not read at "
                                    f"{self.tag}")
                got = read_charmap(text, name)
            except Unsettled as e:
                got = e
            self._charmaps[name] = got
        if isinstance(got, Unsettled):
            raise got
        return got


_SCANS = {}


def _scan(text):
    """Scan, once per text. Kept for the length of one step-2 run, the only
    caller; nothing else reads it."""
    s = _SCANS.get(text)
    if s is None:
        s = _SCANS[text] = Scan(text)
    return s


def range_interval(side, closure, name, offset):
    """(lo, hi) of the line range at `offset` in name's block, for a run
    that reads `closure`, or None.

    glibc starts the range at its cursor, the last element it inserted
    (handle_ellipsis, ld-collate.c:1085-1086@2.39), and an order line whose
    name was already defined returns before moving it (:1059-1066). So the
    line above gives the start only when it is an order line whose <U>
    name no other line of the run can define: no other line of this block
    opens with it, and no other file the run reads spells it anywhere --
    before or after, a superset of "earlier". The line below must be an
    order line too; at `order_end` glibc ends the range at the cursor's
    successor (:3257-3263). Anything else -- a symbol, a keyword, ifdef, a
    comment the reader cannot place, a start inside another range of the
    run or another range whose own ends cannot be placed -- is None.
    """
    scan = side.scan(name)
    ends = scan.info.range_ends(offset, lambda: _reading(scan.text))
    if ends is None:
        return None
    start, end = ends
    for other in closure:
        sc = side.scan(other)
        if other != name and sc.may_define(start):
            return None
        # A member of another range in the run is defined too, by it.
        if any(lo <= start <= hi for lo, hi in sc.inline):
            return None
        for off in sc.line_ranges:
            if (other, off) == (name, offset):
                continue
            ends2 = sc.info.range_ends(off, lambda: _reading(sc.text))
            if ends2 is None or min(ends2) <= start <= max(ends2):
                return None
    return min(start, end), max(start, end)


# --------------------------------------------------------- transliteration

# localedef reads a collating-element's `from` string through the charmap,
# and looks a character the charmap lacks up in the LC_CTYPE
# transliteration table of the file being read (translate_unicode_codepoint,
# linereader.c:617-688@2.39). The element is built out of the answer, or not
# at all when there is none (ld-collate.c:2820-2827). So the same rule
# builds another element when a translit file changes, or when the charmap
# gains or loses a character an answer names. Measured on RHEL 8, 9 and 10
# (M1 and M2 of the 13.7 handoff).

_UHEX = re.compile(r'U([0-9A-Fa-f]{4}|[0-9A-Fa-f]{8})')
_REACH = re.compile(r'(?:copy|include)[ \t\r\f\v]*(?:"([^"\n]*)"|'
                    r'([^\s;"]+))')
_CATEGORIES = ('LC_CTYPE', 'LC_COLLATE', 'LC_MONETARY', 'LC_NUMERIC',
               'LC_TIME', 'LC_MESSAGES', 'LC_PAPER', 'LC_NAME', 'LC_ADDRESS',
               'LC_TELEPHONE', 'LC_MEASUREMENT', 'LC_IDENTIFICATION')
# The words a statement outside a translit block opens with in every
# LC_CTYPE from 2.12 to 2.42 (charclass and charconv also declare names,
# which open lines too).
_CT_FIRST = frozenset('upper lower alpha digit outdigit space cntrl punct '
                      'graph print xdigit blank toupper tolower class map '
                      'charclass charconv'.split())
# Words that change what a translit lookup reads when glibc meets them as a
# statement: allowed only where the reader below takes them.
_STRUCT = re.compile(r'(?<![A-Za-z0-9_<"])(?:END|LC_[A-Z]+|translit_start|'
                     r'translit_end|translit_ignore|include|copy|'
                     r'default_missing|repertoiremap)(?![A-Za-z0-9_>])')
_WS_ = r'[ \t\r\f\v]'

# Every memo below is keyed by a text and kept for one step-2 run
# (compare clears them).
_CTYPES, _LOWER, _HEADS, _NAMED, _ELEMS, _QUICK, _DEFINES = (
    {}, {}, {}, {}, {}, {}, {})
_ELEMENT_RES, _HAS_CTYPE, _REPMAP, _READING, _BARE, _UNESC, _BLOCK_RES = (
    {}, {}, {}, {}, {}, {}, {})
_RAW, _RAWFROM = {}, {}

# The commit from which localedef reads its input as UTF-8 (first in
# glibc-2.36). Before it a raw non-ASCII character is bytes: a head made of
# them is an identifier that makes read_translit_entry swallow the next
# line, and a string keeps each byte as a character (linereader.c:332 and
# :665-667@2.35; round 5 of the 13.7 review, finding 4).
UTF8_READER = 'b15538d77c6a7893c8bb42831dcd3a1a12b727d4'


def _bare(text):
    """The text with every line continued at `/` or `\\` joined and every
    `/` and `\\` taken out: where a word glibc reads, built with either
    escape character, shows as the word. A superset, for refusing and for
    counting."""
    got = _BARE.get(text)
    if got is None:
        if '/' not in text and '\\' not in text:
            got = text
        elif len(text) < 1 << 20:
            got = (text.replace('/\n', '').replace('\\\n', '')
                   .replace('/', '').replace('\\', ''))
        else:
            # Each escape character out, and the newline right after one.
            # By position: iso14651_t1_common, 3.4 MB, holds four.
            out, i, nxt = [], 0, {}
            while True:
                for c in '/\\':
                    if nxt.get(c, -2) < i:
                        nxt[c] = text.find(c, i)
                hits = [j for j in nxt.values() if j >= 0]
                if not hits:
                    out.append(text[i:])
                    break
                j = min(hits)
                out.append(text[i:j])
                i = j + 1 + (text[j + 1:j + 2] == '\n')
            got = ''.join(out)
        _BARE[text] = got
    return got


def _find_all(text, word, end=None):
    out, i = [], text.find(word, 0, end)
    while i >= 0:
        out.append(i)
        i = text.find(word, i + len(word), end)
    return out


def _at_line_start(text, word, end=None):
    """Offsets where `word` opens a line, after blanks (the ones that end
    by `end`, when given)."""
    return [i for i in _find_all(text, word, end)
            if not text[text.rfind('\n', 0, i) + 1:i].strip(_WS)]


def _line_of(text, i):
    ls = text.rfind('\n', 0, i) + 1
    le = text.find('\n', i)
    return text[ls:le if le >= 0 else len(text)]


def _ctype_reading(text):
    """(comment, escape) the text is read with, when they are `%` or `#`
    and `/` or `\\` and set only by plain directive lines before the first
    section, none of them joined to a line above; None otherwise. glibc
    takes any one character, reads a directive's argument from the next
    line when its line ends in the escape in force, reads directives only
    at the top level (locfile.c:107-150@2.39, lr_next), and skips a
    directive a continued line runs into (a syntax error there drops the
    rest of the logical line, locfile.c:255-258). A directive spelled with an escape
    shows in _bare(text) and not in the text: refused. Cheaper than
    glibc_locale_data.reading_chars, which reads every line: here only the
    lines that hold one of the two words are read. Its own name: PR B's
    _reading above (glibc_locale_data.reading_chars) serves the range
    readers, and one name for both had silently given them this one
    (round 6 of the 13.7 review, finding 4)."""
    got = _READING.get(text, _READING)
    if got is _READING:
        got = _READING[text] = _reading1(text)
    return got


def _reading1(text):
    cc, ec = '#', '\\'
    bare = _bare(text)
    words = ('comment_char', 'escape_char')
    if not any(w in bare for w in words):
        return cc, ec
    if any(bare.count(w) != text.count(w) for w in words):
        return None
    found = sorted((i, w) for w in words for i in _find_all(text, w))
    # Only a section that opens before the last directive can come first:
    # no need to search past it (iso14651_t1_common is 3.4 MB).
    last = found[-1][0] if found else 0
    heads = [i for c in _CATEGORIES
             for i in _at_line_start(text, c, last + len(c))]
    first = min(heads) if heads else len(text)
    for i, w in found:
        ls = text.rfind('\n', 0, i) + 1
        if text[ls:i].strip(_WS):
            continue                      # inside a line: not a directive
        if i > first:
            return None                   # glibc reads it nowhere, or not here
        line = _line_of(text, i)
        m = re.fullmatch(_WS_ + r'*(comment_char|escape_char)' + _WS_
                         + r'+(\S)' + _WS_ + '*', line)
        # Past comment lines that end in the escape: glibc skips each and
        # goes on, so a continued line above them still swallows the
        # directive (round 5, finding 6).
        above = _line_above(text, ls, cc, ec)
        if (not m or line.endswith(ec)
                or (above.endswith(ec)
                    and not above.lstrip(_WS).startswith(cc)
                    # The directive above was read, and checked here, with
                    # the escape in force before it.
                    and not _DIRECTIVE.fullmatch(above))
                or m.group(2) not in ('%#' if m.group(1) == 'comment_char'
                                      else '/\\')):
            return None
        if m.group(1) == 'comment_char':
            cc = m.group(2)
        else:
            ec = m.group(2)
    return cc, ec


def _has_repertoiremap(text):
    got = _REPMAP.get(text)
    if got is None:
        got = _REPMAP[text] = 'repertoiremap' in _bare(text) and any(
            re.search(r'^[ \t\r\f\v]*repertoiremap', v, re.M)
            for v in (text, _unesc(text, '/'), _unesc(text, '\\')))
    return got


def _unesc(text, e):
    """The text as glibc reads it with escape character e: e and a newline
    after it joined, e before any other character taken out and that
    character kept (lr_next; get_ident, linereader.c:582-589@2.39), except
    on a comment line (`%` or `#`) or a comment_char or escape_char
    directive."""
    got = _UNESC.get((text, e))
    if got is None:
        if e not in text:
            got = text
        else:
            out, i = [], 0
            while True:
                j = text.find(e, i)
                if j < 0:
                    out.append(text[i:])
                    break
                out.append(text[i:j])
                if text[j + 1:j + 2] == '\n':
                    line = text[text.rfind('\n', 0, j) + 1:j + 1]
                    if (line.lstrip(_WS).startswith(('%', '#'))
                            or _DIRECTIVE.fullmatch(line)):
                        out.append(e + '\n')
                else:
                    out.append(text[j + 1:j + 2])
                i = j + 2
            got = ''.join(out)
        _UNESC[(text, e)] = got
    return got


_DIRECTIVE = re.compile(_WS_ + r'*(?:comment_char|escape_char)' + _WS_
                        + r'+\S' + _WS_ + '*')


def _joined(body, cc, ec):
    """body as glibc's reader makes its logical lines: a line that ends in
    the escape is joined to the next (lr_next and lr_getc,
    linereader.c:149-171@2.39, linereader.h:125-137). A comment line ends
    the logical line, unless it ends in the escape itself: then only that
    physical line is skipped and the logical line goes on with the next one
    (lr_token :221-229), which M3 of the 13.7 handoff measured as the
    comment dropping only itself."""
    if ec + '\n' not in body:
        return body
    out, cur = [], ''
    for line in body.split('\n'):
        if line.lstrip(_WS).startswith(cc):
            if line.endswith(ec):
                continue
            out.append(cur)
            cur = ''
            continue
        cur += line
        if line.endswith(ec):
            cur = cur[:-1]
            continue
        out.append(cur)
        cur = ''
    if cur:
        out.append(cur)
    return '\n'.join(out)


def _escape_alone(text, e):
    """Is a line just e? Where e is the escape in force, glibc stops
    reading the file there (lr_next leaves an empty buffer, lr_getc returns
    EOF, linereader.h:125-137@2.39); callers ask for both characters, since
    the escape in force changes at escape_char."""
    return (text.startswith(e + '\n') or ('\n' + e + '\n') in text
            or text.endswith('\n' + e))


def _line_above(text, ls, cc, ec):
    """The line above the one that opens at ls, past every comment line
    that ends in the escape (glibc skips those and goes on, lr_token
    :221-229@2.39), or '' at the top."""
    while ls > 0:
        prev = text.rfind('\n', 0, ls - 1) + 1
        line = text[prev:ls - 1]
        if not (line.lstrip(_WS).startswith(cc) and line.endswith(ec)):
            return line
        ls = prev
    return ''


def _continues(line, cc, ec, text):
    """Does this line join the next one? Not a comment line; and not the
    one escape_char directive that set this escape, read before it was in
    force."""
    return (line.endswith(ec) and not line.lstrip(_WS).startswith(cc)
            and not (ec != '\\' and text.count('escape_char') == 1
                     and re.fullmatch(_WS_ + r'*escape_char' + _WS_ + r'+'
                                      + re.escape(ec) + _WS_ + '*', line)))


def _block_re(cc, ec):
    """What a line of a translit_start block may be, in the forms every
    such block from 2.12 to 2.42 uses: an entry whose head is a `<U....>`
    name or a raw non-ASCII character (de_DE and translit_emojis from 2.36;
    _translit_lines refuses one that names the character looked up),
    followed by strings, `<...>` symbols, raw non-ASCII characters and
    `;`; an entry whose head is a string, followed by exactly one string
    (a head glibc drops would leave the rest of the line read as an entry
    of its own, read_translit_entry, ld-ctype.c:1862-1864@2.39, and a `;`
    after two dropped strings is read as a head that swallows the next
    line, :1842-1848, round 5 finding 3); `include "name";""`;
    default_missing; a comment or a blank line. Strings hold `<U....>` and
    other names, printable ASCII, raw non-ASCII characters and the escape
    before printable ASCII. Anything else -- an escape outside a string, a
    keyword -- does not match. A line that ends in the escape is refused by
    _read_ctype before this is asked: a comment does not stop the join."""
    got = _BLOCK_RES.get((cc, ec))
    if got is None:
        E, C, W = re.escape(ec), re.escape(cc), _WS_
        uname = r'<U[0-9A-Fa-f]{4}(?:[0-9A-Fa-f]{4})?>'
        raw = r'[^\x00-\x7f\s"]'
        ch = (r'(?:' + uname + r'|<[^<>"\n/\\]+>|' + E + r'[ -~]|(?!' + E
              + r')[ !#-;=?-~]|' + raw + r')')
        string = '"' + ch + '*"'
        tok = r'(?:' + string + r'|<[^<>\s"/\\]+>|' + raw + r'+)'
        items = r'(?:' + W + r'*(?:' + tok + r'|;))*'
        tail = r'(?:' + W + r'*' + C + r'[^\n]*)?' + W + r'*'
        line = (r'(?:' + W + r'*(?:' + uname + r'|' + raw + r')' + items
                + tail
                + r'|' + W + r'*' + string + W + r'*' + string + tail
                + r'|' + W + r'*include' + W + r'*"(?:[A-Za-z0-9_@.+-]|'
                + uname + r')+"' + W
                + r'*;' + W + r'*""' + tail
                + r'|' + W + r'*default_missing' + items + tail
                + r'|' + W + r'*(?:' + C + r'[^\n]*)?)')
        got = _BLOCK_RES[(cc, ec)] = re.compile(r'(?:' + line + r'\n)*')
    return got


Ctype = collections.namedtuple('Ctype', 'offset copy includes blocks chars '
                                        'section odd')
Ctype.__doc__ = """One file's LC_CTYPE, as far as transliteration needs it.

offset: where the section opens; copy: the file a leading `copy` names, or
None; includes: what its translit_start blocks include, in order; blocks:
[(offset in the text, block text)]; chars: (comment, escape); section: the
text from LC_CTYPE to END LC_CTYPE; odd: why it cannot be read with
certainty, or None."""


_ODD_0XFF = Ctype(None, None, (), (), None, None,
                  'a 0xFF byte, which glibc before 2.36 reads as the end of '
                  'a string, a section or the file')


def ctype_of(text):
    """Ctype of a locale text, or None when it has no LC_CTYPE section. A
    text read from bytes that held 0xFF is odd wherever the byte stands,
    a file with no LC_CTYPE in sight included (round 8 of the 13.7
    review): before 2.36 the byte ends a string, a section or the file,
    and from 2.36 it is not UTF-8. The record is asked before the cache,
    which is keyed by value: a text with the same value, decoded from
    another invalid byte, may have been looked at first (round 9)."""
    if text in g.TEXTS_WITH_0XFF:
        return _ODD_0XFF
    got = _CTYPES.get(text, _CTYPES)
    if got is _CTYPES:
        got = _CTYPES[text] = _read_ctype(text)
    return got


def _read_ctype(text):
    """LC_CTYPE as ctype_read reads it (ld-ctype.c:2070-2850@2.39). It is
    meant to accept only the forms every LC_CTYPE from 2.12 to 2.42 is
    written in and to make everything else odd; what it checks is this.
    One section, at the top level (every section opened above it ended
    above it, by plain lines no line above joins); its keyword and END
    LC_CTYPE each at column 0 and alone on their lines, neither joined to
    the line above nor ending in the escape; no line holding only `/` or
    `\\`; no byte that is not UTF-8 inside it;
    `copy` only as the first statement (:2096-2135); outside translit
    blocks, statements that open with an LC_CTYPE keyword or a name a
    charclass or charconv line declared, and no keyword that ends the
    section, opens a block or reads another file (an unknown word does not
    end the line, so what follows it is read as a statement, :2788-2818);
    inside them, the lines _block_re takes and none that ends in the
    escape (a comment does not stop the join, linereader.c:218-229, and
    lr_ignore_rest drops a continued line, :133-136) (:2630-2760); the
    markers likewise. translit_ignore,
    on which find_translit2 can loop (:1730-1740), and a repertoiremap,
    through which symbols resolve (H10 of the 13.7 handoff), are odd."""
    def odd(why, offset=None):
        return Ctype(offset, None, (), (), None, None, why)
    if 'LC_CTYPE' not in _bare(text):
        if _ctype_reading(text) is not None:
            return None
        return odd('comment or escape characters this tool does not read, '
                   'which could spell LC_CTYPE')
    chars = _ctype_reading(text)
    if chars is None:
        return odd('comment or escape characters other than % or # and / '
                   'or \\, or set where this tool does not read them')
    cc, ec = chars
    if _has_repertoiremap(text):
        return odd('a repertoiremap line, which this tool does not model')
    # Either character: a line of the default escape above escape_char
    # also ends the file for glibc (round 5, finding 5).
    if _escape_alone(text, '/') or _escape_alone(text, '\\'):
        return odd('a line holding only the escape character, at which '
                   'glibc stops reading the file')
    heads = _at_line_start(text, 'LC_CTYPE')
    v = _unesc(text, ec)
    if not heads and not _at_line_start(v, 'LC_CTYPE'):
        # Named inside a line only, as LC_IDENTIFICATION's category does.
        return None
    if (len(heads) != 1 or (heads[0] and text[heads[0] - 1] != '\n')
            or len(_at_line_start(v, 'LC_CTYPE')) != 1
            or v.count('LC_CTYPE') != text.count('LC_CTYPE')):
        return odd('an LC_CTYPE section this tool does not read: a second '
                   'one, the keyword after blanks or built with the escape')
    head = heads[0]
    # A section opened above and not ended above takes LC_CTYPE's lines as
    # its own, and ends at its END LC_CTYPE (each reader ends at any END,
    # ld-identification.c:387-399@2.39): the file then has no LC_CTYPE
    # (round 5, finding 2). So every opening and END above has to be a
    # plain line that no line above joins, and the last one an END.
    opens = [i for c in _CATEGORIES if c != 'LC_CTYPE'
             for i in _at_line_start(text, c, head)]
    closes = _at_line_start(text, 'END', head)
    pre = text[:head]
    if ((opens and (not closes or max(opens) > max(closes)))
            or any(_bare(pre).count(w) != pre.count(w)
                   for w in ('END',) + _CATEGORIES)
            or any(not re.fullmatch(
                _WS_ + r'*LC_[A-Z]+' + _WS_ + '*',
                _line_of(text, x)[:_certain_comment_start(
                    _line_of(text, x), chars)]) for x in opens)
            or any(not re.fullmatch(
                _WS_ + r'*END' + _WS_ + r'+LC_[A-Z]+' + _WS_ + '*',
                _line_of(text, x)[:_certain_comment_start(
                    _line_of(text, x), chars)]) for x in closes)
            # From the line's start: the keyword may be indented (round 6,
            # finding 2).
            or any(_continues(_line_above(text, text.rfind('\n', 0, x) + 1,
                                          cc, ec), cc, ec, text)
                   for x in opens + closes)):
        return odd('an LC_CTYPE section this tool cannot place at the top '
                   'level: a section above it not ended, or ended in a way '
                   'this tool does not read', head)
    hle = text.find('\n', head)
    hle = len(text) if hle < 0 else hle
    hl = text[head:hle]
    if (not re.fullmatch(r'LC_CTYPE' + _WS_ + '*', hl[:_certain_comment_start(
            hl, chars)]) or hl.endswith(ec)
            or _continues(_line_above(text, head, cc, ec), cc, ec, text)):
        return odd('an LC_CTYPE line joined to another one', head)
    ends = [x for x in _at_line_start(text, 'END') if x > head]
    if not ends:
        return odd('an LC_CTYPE section with no END LC_CTYPE', head)
    end = ends[0]
    el = _line_of(text, end)
    if (text[end - 1] != '\n'
            or not re.fullmatch(r'END' + _WS_ + r'+LC_CTYPE' + _WS_ + '*',
                                el[:_certain_comment_start(el, chars)])
            # lr_ignore_rest would drop the next line, the head of the
            # next section among them (round 5, finding 8).
            or el.endswith(ec)
            or _continues(_line_above(text, end, cc, ec), cc, ec, text)):
        return odd('an END in LC_CTYPE this tool does not read', head)
    body = text[hle + 1:end]
    section = text[head:end + len(el) + 1]
    if '\x1a' in text[:head + len(section)]:
        # glibc before 2.36 takes 0x1A as the end of the file and never
        # moves past it (linereader.h:137@2.35, removed by 19d4944459); it
        # decodes as U+001A, so the check below does not see it (round 6,
        # finding 1).
        return odd('a 0x1A byte, at which glibc before 2.36 stops reading '
                   'the file', head)
    if '\ufffd' in section:
        # read_blobs_at decodes with errors='replace': two different bytes
        # would read as one character here (round 5, finding 7).
        return odd('bytes that are not UTF-8 in LC_CTYPE, which this tool '
                   'reads as one replacement character', head)
    # translit_start ... translit_end blocks, at the start of a line.
    marks = [(m.start(), m.group(1)) for m in re.finditer(
        r'^' + _WS_ + r'*translit_(start|end)(?![A-Za-z0-9_])', body, re.M)]
    blocks, outside, pos, includes = [], [], 0, []
    for k, (at, word) in enumerate(marks):
        line = _line_of(body, at)
        ls = body.rfind('\n', 0, at) + 1
        if (word != ('start', 'end')[k % 2]
                or line[:_certain_comment_start(line, chars)].strip(_WS)
                != 'translit_' + word
                or line.endswith(ec)
                or _continues(_line_above(body, ls, cc, ec), cc, ec, text)):
            return odd('a translit_start or translit_end line this tool '
                       'does not read', head)
        le = body.find('\n', at) + 1
        if word == 'start':
            outside.append(body[pos:at])
            start = le
        else:
            blk = body[start:at]
            if 'translit_ignore' in _bare(blk):
                return odd('a translit_ignore line, on which glibc can loop '
                           'for ever', head)
            if (ec + '\n') in blk or blk.endswith(ec):
                return odd('a line of a translit_start block that ends in '
                           'the escape character, which glibc joins to the '
                           'next one', head)
            if not _block_re(cc, ec).fullmatch(blk):
                return odd('a line of a translit_start block this tool does '
                           'not read', head)
            blocks.append((hle + 1 + start, blk))
            includes += [g.decode_symbolic(n) for n in re.findall(
                r'^' + _WS_ + r'*include' + _WS_ + r'*"([^"\n]*)"', blk,
                re.M)]
            pos = le
    if len(marks) % 2:
        return odd('a translit_start block with no translit_end', head)
    outside.append(body[pos:])
    logical = _joined(''.join(outside), cc, ec)
    words = re.sub(re.escape(ec) + r'(.)', r'\1', logical)
    copy, declared, first = None, set(), True
    for n, line in enumerate(words.split('\n')):
        code = line[:_certain_comment_start(line, chars)].strip(_WS)
        if not code:
            continue
        m = re.match(r'[A-Za-z_][A-Za-z0-9_]*', code)
        word = m.group(0) if m else None
        hits = _STRUCT.findall(code)
        if first and word == 'copy':
            c = re.fullmatch(r'copy' + _WS_ + r'*"([^"\n]*)"', code)
            if not c or hits != ['copy']:
                return odd(f"an LC_CTYPE copy line this tool does not "
                           f"read: {code[:60]!r}", head)
            copy = g.decode_symbolic(c.group(1))
        elif hits:
            return odd(f"{hits[0]} where this tool does not read it in "
                       f"LC_CTYPE", head)
        elif word in ('charclass', 'charconv'):
            declared |= set(re.split(r'[;\s]+', code[len(word):].strip()))
        elif word not in _CT_FIRST and word not in declared:
            return odd(f"an LC_CTYPE statement this tool does not read: "
                       f"{code[:60]!r}", head)
        first = False
    return Ctype(head, copy, tuple(includes), tuple(blocks), chars, section,
                 None)


def _raw_code(ct):
    """Does a translit block of ct hold a raw non-ASCII character outside a
    certain comment? Every one before 2.36 is in a comment, which glibc
    skips without reading it as characters."""
    # The answer depends on the reading characters as much as on the
    # section (round 10 of the 13.7 review).
    got = _RAW.get((ct.section, ct.chars))
    if got is None:
        got = _RAW[(ct.section, ct.chars)] = any(
            not line[:_certain_comment_start(line, ct.chars)].isascii()
            for _, blk in ct.blocks if not blk.isascii()
            for line in blk.split('\n') if not line.isascii())
    return got


def _lower(v):
    got = _LOWER.get(v)
    if got is None:
        got = _LOWER[v] = v.lower()
    return got


def _translit_lines(text, ct, cp):
    """[(line number, what follows the head)] of every line of ct's translit
    blocks whose first token is cp as a `<U....>` name, in file order. glibc
    puts each entry at the head of the table as it reads it
    (ld-ctype.c:1906-1907@2.39), so the last line is the newest entry. The
    blocks are read by _read_ctype, which takes no other head that can
    name cp: a quoted head names cp only when the charmap has it, and cp is
    looked up only when it does not (read_widestring and get_string with
    no locale, ld-ctype.c:1803-1810, linereader.c:897-899@2.39); `<u...>` is
    a symbol, not a code point (get_symname :534-547)."""
    key_ = (text, cp)
    got = _HEADS.get(key_)
    if got is None:
        found = []
        needles = (['<u%04x>' % cp] if cp <= 0xFFFF else []) + \
            ['<u%08x>' % cp]
        raw = chr(cp) if 0x7f < cp < 0x110000 else None
        for offset, blk in ct.blocks:
            if raw and raw in blk and re.search(
                    r'^' + _WS_ + '*' + re.escape(raw), blk, re.M):
                found = Unsettled(f"a transliteration of U+{cp:04X} spelled "
                                  f"as the raw character, which glibc 2.36 "
                                  f"and later read as it and earlier ones "
                                  f"do not (lr_token, "
                                  f"linereader.c:332-342@2.39)")
                break
            low = _lower(blk)
            for nd in needles:
                i = low.find(nd)
                while i >= 0:
                    ls = blk.rfind('\n', 0, i) + 1
                    if blk[i + 1] == 'U' and not blk[ls:i].strip(_WS):
                        le = blk.find('\n', i)
                        found.append((offset + ls, blk[i + len(nd):le if
                                                       le >= 0 else len(blk)]))
                    i = low.find(nd, i + 1)
        got = _HEADS[key_] = found if isinstance(found, Unsettled) else [
            (text.count('\n', 0, at) + 1, rest) for at, rest in sorted(found)]
    if isinstance(got, Unsettled):
        raise got
    return got


def _alternatives(rest, chars, has=None):
    """[[code point]] of each alternative of a translit line after its
    head, in order, None for one glibc drops; or None when glibc drops the
    whole entry. Modelled on read_translit_entry and read_widestring
    (ld-ctype.c:1786-1917@2.39) and get_string (linereader.c:773-1000).

    An alternative is what lies between two `;`, or before the end of the
    line, after the first token: an empty one is kept, and gives nothing
    for the character (the 95 lines that end in `;` at 2.28, 2.34 and 2.39
    have one). Its tokens are strings -- `<U....>` names and raw ASCII
    characters -- and bare `<U....>`, concatenated. A string that names
    something no charmap here has (any name but `<U....>`), or a `<U....>`
    the charmap lacks (`has(cp)` is False: get_string with no locale,
    linereader.c:897-899), or is empty, drops its alternative; so does a
    bare symbol other than `<U....>`, with no repertoire map read. A `;` as the first token, or no token at all,
    is a syntax error and drops the entry. An alternative with a dropped
    token and another is Unsettled: glibc leaves the other's characters in
    its buffer for the next alternative. A byte value, a raw non-ASCII
    character or any other token is Unsettled."""
    cc, ec = chars
    toks, i, n = [], 0, len(rest)

    def name_at(j):
        """(name, index after `>`) of a name opening at rest[j]."""
        out = []
        j += 1
        while j < n and rest[j] != '>':
            if rest[j] == ec:
                j += 1
                if j >= n:
                    break
            out.append(rest[j])
            j += 1
        if j >= n:
            raise Unsettled('an unterminated name in a transliteration')
        return ''.join(out), j + 1
    while True:
        while i < n and rest[i] in _WS:
            i += 1
        if i >= n or rest[i] == cc:
            break
        ch = rest[i]
        if ch == ';':
            toks.append(';')
            i += 1
        elif ch == '"':
            i += 1
            got = []
            while True:
                if i >= n:
                    raise Unsettled('an unterminated string in a '
                                    'transliteration')
                c = rest[i]
                if c == '"':
                    i += 1
                    break
                if c == ec:
                    if i + 1 >= n or ord(rest[i + 1]) > 0x7f:
                        raise Unsettled('an escape this tool does not read '
                                        'in a transliteration')
                    got.append(ord(rest[i + 1]))
                    i += 2
                elif c == '<':
                    name, i = name_at(i)
                    m = _UHEX.fullmatch(name)
                    cp = int(m.group(1), 16) if m else None
                    if (got is not None and cp is not None
                            and (has is None or has(cp))):
                        got.append(cp)
                    else:
                        got = None
                elif ord(c) > 0x7f:
                    raise Unsettled('a raw non-ASCII character in a '
                                    'transliteration')
                else:
                    if got is not None:
                        got.append(ord(c))
                    i += 1
            toks.append(got or None)
        elif ch == '<':
            name, i = name_at(i)
            m = _UHEX.fullmatch(name)
            toks.append([int(m.group(1), 16)] if m else None)
        else:
            raise Unsettled(f"a transliteration token this tool does not "
                            f"read: {rest[i:i + 20]!r}")
    if not toks or toks[0] == ';':
        return None
    alts, cur, dropped, grown = [], [], False, False
    for t in toks + [';']:
        if t == ';':
            if dropped and grown:
                raise Unsettled('an alternative with a dropped string and '
                                'another, which glibc carries into the next '
                                'alternative')
            alts.append(None if dropped else cur)
            cur, dropped, grown = [], False, False
        elif t is None:
            dropped = True
        else:
            cur = cur + t
            grown = True
    return alts


class TranslitRun:
    """One localedef run's transliteration lookups, in one charmap at one
    tag, for the build of `source` (M1 of the 13.7 handoff).

    An LC_CTYPE that opens with `copy "X"` writes into X's table instead of
    one of its own (ctype_startup, ld-ctype.c:357-359@2.39), after X is read
    (ctype_read :2111-2121). Each entry goes to the head of the table and
    each include to its end (:1906-1907, :2700-2707). The source is read
    top to bottom, so its LC_CTYPE counts only when it comes before its
    LC_COLLATE; a file reached by a `copy` in LC_COLLATE is read for
    LC_COLLATE only (locfile.c:53). The file D whose LC_COLLATE defines the
    element has its LC_CTYPE read at its first lookup, if it was not read
    before (linereader.c:643-655), and an include is read when a lookup
    reaches it (find_translit, ld-ctype.c:1747-1779). A lookup takes the
    table's newest entry for the code point: its first alternative the
    charmap has, or nothing from this table; then each include, in order
    (find_translit2, :1686-1744). Lookups in two files D of one run are
    Unsettled: which one loads its LC_CTYPE first depends on where glibc
    meets each from string."""

    def __init__(self, side, cm, source):
        self.side, self.cm = side, cm
        self.done, self.tables, self.defs, self.answers = {}, {}, [], {}
        text = self._text(source)
        ct = self._ct(source)
        if ct is None:
            return
        col = 0 if text.startswith('LC_COLLATE') else text.find('\nLC_COLLATE')
        if col < 0 or ct.offset < col:
            self.read(source)

    def _text(self, name):
        text = self.side.texts.get(name)
        if text is None:
            where = ('was not read' if name in self.side.present
                     else 'is not there')
            raise Unsettled(f"LC_CTYPE reads {name}, which {where} at "
                            f"{self.side.tag}")
        return text

    def _ct(self, name):
        ct = ctype_of(self._text(name))
        if ct is not None and ct.odd:
            raise Unsettled(f"{name} at {self.side.tag}: {ct.odd}")
        if ct is not None and _not_utf8_above(self._text(name), ct):
            if self.side.reads_utf8() is not False:
                raise Unsettled(
                    f"{name} at {self.side.tag}: a byte that is not UTF-8 "
                    f"above LC_CTYPE, at which localedef from glibc 2.36 on "
                    f"ends a string and reads the rest of it as statements "
                    f"(linereader.c:857, ld-identification.c:303-317@2.39)")
        if ct is not None and _raw_code(ct):
            utf8 = self.side.reads_utf8()
            if utf8 is not True:
                raise Unsettled(
                    f"{name} at {self.side.tag}: a raw non-ASCII character "
                    f"in a transliteration, which localedef reads as one "
                    f"character only from glibc 2.36 on (commit "
                    f"{UTF8_READER[:10]}), and "
                    + ("this tag does not hold it" if utf8 is False else
                       "this tool could not tell whether this tag holds "
                       "it"))
        return ct

    def read(self, name, path=()):
        """Read name's LC_CTYPE into its table, once; its table's name, or
        None when it has no LC_CTYPE (then nothing transliterates through
        it, M1 h)."""
        if name in self.done:
            return self.done[name]
        if name in path:
            raise Unsettled(f"{name} copies its LC_CTYPE in a cycle at "
                            f"{self.side.tag}")
        ct = self._ct(name)
        if ct is None:
            self.done[name] = None
            return None
        root, includes = name, ct.includes
        if ct.copy is not None:
            root = self.read(ct.copy, path + (name,))
            if root is None:
                raise Unsettled(f"{name} copies the LC_CTYPE of "
                                f"{ct.copy}, which has none, at "
                                f"{self.side.tag}")
        self.done[name] = root
        files, incs = self.tables.setdefault(root, ([], []))
        files.append(name)
        incs.extend(includes)
        return root

    def lookup(self, defining, cp):
        """The code points glibc builds U+cp out of in a from string read
        in `defining`, or None."""
        got = self.answers.get((defining, cp), self.answers)
        if got is self.answers:
            if defining not in self.defs:
                self.defs.append(defining)
                if len(self.defs) > 1:
                    raise Unsettled(
                        f"transliterations looked up in more than one file "
                        f"of the run at {self.side.tag}: "
                        f"{', '.join(self.defs)}")
            root = self.read(defining)
            got = None if root is None else self._find(root, cp, ())
            self.answers[(defining, cp)] = got
        return got

    def _find(self, root, cp, path):
        files, incs = self.tables[root]
        for name in reversed(files):
            text = self.side.texts[name]
            ct = self._ct(name)
            where = f"{name} at {self.side.tag}"
            entry = None
            try:
                lines = _translit_lines(text, ct, cp)
            except Unsettled as e:
                raise Unsettled(f"{where}: {e}")
            for lineno, rest in reversed(lines):
                try:
                    entry = _alternatives(
                        rest, ct.chars, lambda c: key(self.cm, c) is not None)
                except Unsettled as e:
                    raise Unsettled(f"{where}, line {lineno}: {e}")
                if entry is not None:
                    break
            if entry is None:
                continue
            for alt in entry:
                if alt is not None and all(key(self.cm, c) is not None
                                           for c in alt):
                    return alt
            break
        for inc in list(incs):
            r = self.read(inc)
            if r is None:
                continue
            if r == root or r in path:
                # find_translit searches that table again, from the top,
                # and recurses for ever unless an entry now answers.
                raise Unsettled(f"{inc}, included at {self.side.tag}, leads "
                                f"back to the table of {r}")
            got = self._find(r, cp, path + (root,))
            if got is not None:
                return got
        return None


def _element_re(ec):
    got = _ELEMENT_RES.get(ec)
    if got is None:
        e = re.escape(ec)
        got = _ELEMENT_RES[ec] = re.compile(
            r'[ \t\r\f\v]*collating-element[ \t\r\f\v]+'
            r'(<(?:[^>\n' + e + r']|' + e + r'.)*>)[ \t\r\f\v]+from'
            r'[ \t\r\f\v]+"((?:[^"\n' + e + r']|' + e + r'.)*)"[ \t\r\f\v]*')
    return got


def elements_of(scan):
    """{element name: from string parts} of every collating-element line
    of the block, or Unsettled. Once per block."""
    blk = scan.block
    chars = g.reading_chars(scan.text)
    got = _ELEMS.get((blk, chars))
    if got is None:
        got = {}
        try:
            if chars is None:
                raise Unsettled('comment and escape characters this tool '
                                'cannot settle')
            ec = chars[1]
            if blk.count('collating-element') != _bare(blk).count(
                    'collating-element'):
                raise Unsettled('a collating-element keyword spelled with '
                                'an escape character')
            for _, line in _lines_with(blk, 'collating-element'):
                code = line[:_certain_comment_start(line, chars)]
                if 'collating-element' not in code:
                    continue
                if line.endswith(ec):
                    raise Unsettled('a collating-element line continued at '
                                    'the escape character')
                m = _element_re(ec).fullmatch(code)
                if m:
                    # glibc keeps the first (check_duplicate,
                    # ld-collate.c:419-455@2.39), unless an ifdef leaves it
                    # out: not settled here.
                    if m.group(1) in got:
                        raise Unsettled(f"collating-element {m.group(1)} is "
                                        f"defined twice")
                    got[m.group(1)] = _from_parts(m.group(2), ec)
                elif code.lstrip(_WS).startswith('collating-element'):
                    raise Unsettled(f"a collating-element line this tool "
                                    f"does not read: {code.strip()[:60]!r}")
        except Unsettled as e:
            got = e
        _ELEMS[(blk, chars)] = got
    if isinstance(got, Unsettled):
        raise got
    return got


def _from_parts(s, ec):
    """A from string as glibc reads it (get_string, linereader.c:773-1000
    @2.39): ('a', character) for a raw ASCII character or one after the
    escape, added as it is (:850-854); ('u', cp) for a `<U....>` name, read
    through the charmap and transliterated when it lacks it; ('n', name)
    for any other name. A raw non-ASCII character is Unsettled: glibc 2.36
    and later read it as a code point through the charmap, earlier ones as
    its bytes."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == ec:
            if i + 1 >= n or ord(s[i + 1]) > 0x7f:
                raise Unsettled('a from string with an escape this tool does '
                                'not read')
            out.append(('a', s[i + 1]))
            i += 2
        elif c == '<':
            j, name = i + 1, []
            while j < n and s[j] != '>':
                if s[j] == ec:
                    j += 1
                    if j >= n:
                        break
                name.append(s[j])
                j += 1
            if j >= n:
                raise Unsettled('a from string with an unterminated name')
            m = _UHEX.fullmatch(''.join(name))
            out.append(('u', int(m.group(1), 16)) if m
                       else ('n', ''.join(name)))
            i = j + 1
        elif ord(c) > 0x7f:
            raise Unsettled('a from string with a raw non-ASCII character')
        else:
            out.append(('a', c))
            i += 1
    return tuple(out)


def _quick_cps(scan):
    """Every code point the block's collating-element lines name, as a
    superset (comments included), and -1 for a line where a name has an
    escape inside or the line is continued: what decides whether a build's
    charmap lacks one, before anything is read strictly."""
    blk = scan.block
    got = _QUICK.get((blk, scan.text))
    if got is None:
        cps = set()
        for _, line in _lines_with(blk, 'collating-element'):
            cps |= {int(h, 16) for h in _UNAME.findall(line)}
            if not line.isascii():
                cps |= {ord(c) for c in line if ord(c) > 0x7f}
            if _ESC_UNAME.search(line) or line.endswith(('/', '\\')):
                cps.add(-1)
        if (_ctype_reading(scan.text) is None or scan.odd_keyword
                or _escape_alone(scan.text, '/')
                or _escape_alone(scan.text, '\\')
                or blk.count('collating-element') != _bare(blk).count(
                    'collating-element')):
            cps.add(-1)
        got = _QUICK[(blk, scan.text)] = frozenset(cps)
    return got


def _element_key(parts, cm, run, defining):
    """(the element's bytes in charmap cm, or None when glibc does not
    build it; the code points it transliterated)."""
    out, used = [], []
    for kind, v in parts:
        if kind == 'a':
            out.append('/x%02x' % ord(v))
        elif kind == 'n':
            # read_charmap reads only <U....> names: no charmap has it, and
            # glibc drops the string (linereader.c:946-953@2.39).
            return None, used
        else:
            k = key(cm, v)
            if k is None:
                used.append(v)
                ans = run.lookup(defining, v)
                if ans is None:
                    return None, used
                k = ''.join(key(cm, c) for c in ans)
            out.append(k)
    return ''.join(out), used


def _not_utf8_above(text, ct):
    """Does the text hold a byte that is not UTF-8 (U+FFFD once decoded)
    above its LC_CTYPE? Before 2.36 such a byte is part of a word or a
    string, and only 0xFF (TEXTS_WITH_0XFF) changes anything; from 2.36 an
    invalid byte ends a string as illegal, and LC_IDENTIFICATION's string
    fields go on reading the rest of the line as statements, an END among
    them (linereader.c:857, ld-identification.c:303-317@2.39; round 8 of
    the 13.7 review). No locale file at 2.36..2.42 holds one."""
    return '\ufffd' in text[:ct.offset]


def _not_utf8_above_text(text):
    ct = ctype_of(text) if text is not None else None
    return ct is not None and not ct.odd and _not_utf8_above(text, ct)


def _holds_raw(text):
    """Does the text's LC_CTYPE, read by _read_ctype, hold a raw non-ASCII
    character outside a comment in a translit block?"""
    ct = ctype_of(text) if text is not None else None
    return ct is not None and not ct.odd and _raw_code(ct)


def _raw_collate(text):
    """Does the text's LC_COLLATE block hold a raw non-ASCII character
    outside a certain comment, on any line (a from string can go on past a
    continued line)? Before UTF8_READER a string keeps those bytes as they
    are, so `from "gʻ"` is g and two bytes, nothing transliterated; from
    2.36 it is U+02BB, transliterated where the charmap lacks it
    (linereader.c:648-670@2.35; round 7 of the 13.7 review, finding 2)."""
    got = _RAWFROM.get(text)
    if got is None:
        sc = _scan(text)
        chars = _ctype_reading(text)
        got = False
        if sc.has_block and not sc.block.isascii():
            joined = False
            for line in sc.block.split('\n'):
                # A line the one above runs into is read whole: a string
                # goes on across the join and get_string knows no comment
                # (linereader.c:642@2.35; round 8, finding 2).
                code = line if joined else line[:_certain_comment_start(
                    line, chars)]
                if not code.isascii():
                    got = True
                    break
                # A line joined to the one above is no comment line, even
                # if it opens with the comment character (round 9).
                joined = chars is not None and line.endswith(chars[1]) \
                    and (joined or not line.lstrip(_WS).startswith(chars[0]))
        _RAWFROM[text] = got
    return got


def _readers_differ(old, new):
    """Could the two tags' localedef read the same bytes differently? Only
    across UTF8_READER: one side holds it and the other does not, or git
    places one side and not the other. Both refs were resolved before step
    2 runs, so git places neither side only when this repository does not
    hold UTF8_READER at all: then neither tag holds it, both read bytes the
    old way, and an unchanged file reads the same at both. TranslitRun
    still refuses a raw table at such a side (Side.reads_utf8 is None).
    Two Sides given one ask_utf8 are read by one localedef, as steps 6 and
    7 read a machine and its tag (node_charmaps), and git is not asked."""
    if old.ask_utf8 is not None and old.ask_utf8 is new.ask_utf8:
        return False
    return old.reads_utf8() is not new.reads_utf8()


def _plain(text):
    """Is the text's LC_CTYPE, if any, read by _read_ctype? What
    _named_files gives for it is then exactly what it reads."""
    if text is None:
        return True
    ct = ctype_of(text)
    return ct is None or not ct.odd


def _named_files(text):
    """What the text's LC_CTYPE copies and includes, as _read_ctype reads
    it; for a text it does not read, every name a `copy` or `include` word
    names anywhere, as it is or with no escape: a superset. Once per
    text."""
    got = _NAMED.get(text)
    if got is None:
        ct = ctype_of(text)
        if ct is None:
            got = _NAMED[text] = frozenset()
            return got
        if not ct.odd:
            got = _NAMED[text] = frozenset(
                ([ct.copy] if ct.copy else []) + list(ct.includes))
            return got
        names = set()
        for v in (text, _bare(text)):
            for word in ('copy', 'include'):
                i = v.find(word)
                while i >= 0:
                    m = _REACH.match(v, i)
                    if m:
                        # An escape keeps the character after it: both
                        # spellings, as a superset.
                        name = m.group(1) if m.group(1) is not None \
                            else m.group(2)
                        for nm in (name, re.sub(r'[/\\](.)', r'\1', name)):
                            names.add(g.decode_symbolic(nm))
                    i = v.find(word, i + 1)
        got = _NAMED[text] = frozenset(names)
    return got


def _ctype_reach(side, names):
    """Every file whose LC_CTYPE a run that starts from `names` can read,
    as a superset: each one and every file its LC_CTYPE names, recursively.
    A name not at this tag is kept in the set: absent at one tag and present
    at the other it compares as a difference, and absent at both it
    compares as the same absence, which glibc reads alike at both tags
    (find_locale fails with record_error, localedef.c, unchanged 2.28 to
    2.42)."""
    memo = side.__dict__.setdefault('_reach', {})
    out = set()
    for name in names:
        got = memo.get(name)
        if got is None:
            got, stack = set(), [name]
            while stack:
                n = stack.pop()
                if n not in got:
                    got.add(n)
                    text = side.texts.get(n)
                    if text is not None:
                        stack.extend(_named_files(text))
            memo[name] = got
        out |= got
    return out


def _ctype_signature(text):
    """What of a text the transliteration tables read: its LC_CTYPE
    section, comment and escape characters, and whether LC_CTYPE comes
    before LC_COLLATE. The whole text when the section is not simple."""
    if text is None:
        return None
    ct = ctype_of(text)
    if ct is None:
        return None
    if ct.odd:
        return text
    col = 0 if text.startswith('LC_COLLATE') else text.find('\nLC_COLLATE')
    return ct.section, ct.chars, 0 <= col < ct.offset


def ctype_moved(old, new):
    """With no charmap changed between the tags: could a transliteration
    differ? Only through a file whose LC_CTYPE is read differently. The
    Sides hold the texts of the files that changed (CharmapReads); a file
    added, deleted or renamed counts, and so does a changed one whose
    _ctype_signature differs, or whose two texts are equal only because
    bytes that are not UTF-8 decode as U+FFFD on both. Files that did not
    change but read differently across UTF8_READER are compare()'s to
    catch (_readers_differ). A change inside LC_COLLATE is step 2's own
    finding, and step 3 lists every locale that copies it; a changed
    file that defines collating elements still runs point 6, which counts
    the builds that read it as listed anyway."""
    if set(old.present) != set(new.present):
        return True
    for n, a in old.texts.items():
        b = new.texts.get(n)
        if a is b:
            continue
        if a == b and '\ufffd' in a:
            # Two blobs that differ only in bytes decoded as U+FFFD: the
            # texts cannot say what glibc reads (round 5, finding 7).
            return True
        if b is None or _ctype_signature(a) != _ctype_signature(b):
            return True
        # Elements it defines changed too: point 6 runs, so that its
        # builds are counted as listed, never as built the same.
        if ('collating-element' in _bare(a) or 'collating-element' in
                _bare(b)) and _scan(a).block != _scan(b).block:
            return True
    return False


def _names_ctype(text):
    """Does the text name LC_CTYPE anywhere? Once per text: the search
    runs through iso14651_t1_common, 3.4 MB, for every build that copies
    it."""
    if text is None:
        return False
    # Before the cache, as in ctype_of: two 0xFF can make glibc read an
    # LC_CTYPE the text does not show (round 8).
    if text in g.TEXTS_WITH_0XFF:
        return True
    got = _HAS_CTYPE.get(text)
    if got is None:
        got = _HAS_CTYPE[text] = ('LC_CTYPE' in _bare(text)
                                  or _ctype_reading(text) is None)
    return got


def _defines_elements(side, name):
    """Could `name`'s LC_COLLATE define a collating element at this tag:
    its block names the keyword, spelled plainly or with escapes, or the
    file has a section this does not read (Scan.odd_keyword)?"""
    text = side.texts[name]
    got = _DEFINES.get(text)
    if got is None:
        sc = side.scan(name)
        # A section this does not read (a second LC_COLLATE, the keyword
        # after blanks) can define one too: the strict reader refuses it.
        got = _DEFINES[text] = bool(sc.odd_keyword or sc.has_block and (
            'collating-element' in sc.block
            or 'collating-element' in _bare(sc.block)))
    return got


Translit = collections.namedtuple('Translit', 'source entry charmaps changed '
                                              'chars via reason')
Translit.__doc__ = """One build the transliteration check lists: changed,
how many of its elements are built differently; chars: the code points
they transliterate; via: the files that define them; reason: why it could
not be compared, or None."""

TranslitReport = collections.namedtuple('TranslitReport',
                                        'elements builds unmoved found '
                                        'changed', defaults=(None,))
TranslitReport.__doc__ = """elements, builds: how many collating elements of
how many builds are built out of a transliteration at either tag and were
compared; unmoved: how many builds that may build one were not compared,
because nothing they read changed; found: [Translit]; changed: how many
elements are built differently, counted before compare() takes out the
builds step 3 lists anyway, so the printed total is never short."""


def _translit_plan(old, new, common):
    """([(old Build, new Build, [files defining elements] per tag, {code
    points they name} per tag)], [Translit that could not be planned]):
    the builds whose LC_COLLATE closure holds a file that defines
    collating elements and has LC_CTYPE at either tag. Texts only; no
    charmap is read."""
    def has_ctype(d):
        return any(_names_ctype(side.texts.get(d)) for side in (old, new))
    plan, found = [], []
    for a, b in common:
        per, quick = [], []
        try:
            for side, bld in ((old, a), (new, b)):
                ds = [d for d in side.closure(bld.source)
                      if _defines_elements(side, d) and has_ctype(d)]
                per.append(ds)
                quick.append(frozenset().union(
                    *(_quick_cps(side.scan(d)) for d in ds)))
        except Unsettled as e:
            found.append(Translit(b.source, b.entry, (a.charmap, b.charmap),
                                  0, set(), [], str(e)))
            continue
        if quick[0] or quick[1]:
            plan.append((a, b, per, quick))
    return plan, found


def _plan_charmaps(old, new, plan):
    return {old.tag: {a.charmap for a, *_ in plan},
            new.tag: {b.charmap for _, b, *_ in plan}}


def translit_charmaps(old, new, builds_old, builds_new):
    """{tag: {charmap}}: what point 6 will read, from the texts alone, so
    that step 2 can read it while it does the rest of its work. Leaves its
    memos for compare(fresh=False)."""
    reset()
    common, _ = common_builds(old, new, builds_old, builds_new)
    return _plan_charmaps(old, new, _translit_plan(old, new, common)[0])


def _ff_gate(old, new, common):
    """A TranslitReport listing every build in `common`, or None: None
    unless a locale file or charmap either Side holds was read from bytes
    with 0xFF."""
    if not g.TEXTS_WITH_0XFF:
        return None
    held = sorted({f"{n} at {side.tag}" for side in (old, new)
                   for texts in (side.texts, side.charmap_texts)
                   for n, t in texts.items() if t in g.TEXTS_WITH_0XFF})
    if not held:
        return None
    reason = (f"{', '.join(held[:3])}{' and others' if len(held) > 3 else ''}"
              f" holds a 0xFF byte, which glibc before 2.36 reads as the end "
              f"of a string, a section or the file: no build's "
              f"transliterations are compared")
    return TranslitReport(0, 0, 0, [
        Translit(b.source, b.entry, (a.charmap, b.charmap), 0, set(), [],
                 reason) for a, b in common], 0)


def compare_translit(old, new, common, ks, bad, read_charmaps):
    """The collating elements the builds in `common` build out of
    transliterations, compared between the tags.

    A file in a build's LC_COLLATE closure that defines elements, and has
    LC_CTYPE at either tag (a file with none never transliterates, M1 h),
    is read for the code points its elements name; the build's charmap at
    each tag, which `read_charmaps({tag: {charmap}})` reads first, says
    whether one is missing and so transliterated. Those builds are compared
    when the charmap pair's K is not empty (H1, M2), a file their LC_CTYPE
    can read differs between the tags (H2), or an unchanged file holds what
    the two tags' readers read differently (raw characters, bytes that are
    not UTF-8): anything else gives glibc the same input, read alike, at
    both. Compared means every such element's bytes, or its absence, at
    each tag.
    """
    # One gate for the byte that ends a string, a section or the file in
    # glibc before 2.36 (read_blobs_at records it): with one in any locale
    # file or charmap the run holds, no build is compared, so that no path
    # of the plan, its caches or its shortcuts can pass over it (rounds 8
    # to 10 of the 13.7 review each found another). No file at 2.12..2.42
    # holds one. Asked again once the plan's charmaps are read.
    gate = _ff_gate(old, new, common)
    if gate:
        return gate
    plan, found = _translit_plan(old, new, common)
    if plan:
        read_charmaps(_plan_charmaps(old, new, plan))
        gate = _ff_gate(old, new, common)
        if gate:
            return gate
    elements = builds = unmoved = 0
    for a, b, per, quick in plan:
        p = (a.charmap, b.charmap)
        try:
            if p in bad:
                raise Unsettled(bad[p])
            cms = (old.charmap(a.charmap), new.charmap(b.charmap))
            if not any(c < 0 or key(cm, c) is None
                       for cm, q in zip(cms, quick) for c in q):
                continue
            # What cannot be read is refused first: the shortcut below
            # assumes each file's LC_CTYPE reads as it looks (round 3 of
            # the reviewer).
            for side, bld, ds in ((old, a, per[0]), (new, b, per[1])):
                for n in [bld.source] + ds:
                    text = side.texts.get(n)
                    if text is None:
                        continue
                    if side.scan(n).odd_keyword:
                        raise Unsettled(f"{n} at {side.tag}: "
                                        f"{side.scan(n).odd_keyword}")
                    ct = ctype_of(text)
                    if ct is not None and ct.odd:
                        raise Unsettled(f"{n} at {side.tag}: {ct.odd}")
            reach = (_ctype_reach(old, [a.source] + per[0] + per[1])
                     | _ctype_reach(new, [b.source] + per[0] + per[1]))
            k = ks.get(p)
            defs = set(per[0]) | set(per[1])
            if not (k is not None and k.size) and all(
                    old.texts.get(f) == new.texts.get(f)
                    and _plain(old.texts.get(f))
                    # The same raw characters read as other code points
                    # across UTF8_READER (round 6, finding 3).
                    and not ((_holds_raw(old.texts.get(f))
                              or _not_utf8_above_text(old.texts.get(f))
                              or (f in defs
                                  and _raw_collate(old.texts.get(f))))
                             and _readers_differ(old, new))
                    for f in reach):
                unmoved += 1
                continue
            elems = []
            for side, ds in ((old, per[0]), (new, per[1])):
                got = {}
                for d in ds:
                    try:
                        if side.scan(d).odd_keyword:
                            raise Unsettled(side.scan(d).odd_keyword)
                        ct = ctype_of(side.texts[d])
                        if ct is not None and ct.odd:
                            raise Unsettled(ct.odd)
                        for el, parts in elements_of(side.scan(d)).items():
                            if any(kind == 'u' for kind, _ in parts):
                                got[(d, el)] = parts
                    except Unsettled as e:
                        raise Unsettled(f"{d} at {side.tag}: {e}")
                elems.append(got)
            # An element that needs a transliteration at either tag and is
            # not the same element at both: the file that defines it changed
            # inside LC_COLLATE, which step 2 lists and step 3 follows.
            # Listed with that reason, so that compare() counts it as
            # listed anyway, and a `!!` would show if it ever were not.
            def needs(parts):
                return any(kind == 'u' and (key(cms[0], v) is None
                                            or key(cms[1], v) is None)
                           for kind, v in parts)
            moved_elements = {de for side_ in elems for de, parts in
                              side_.items() if needs(parts)
                              and elems[0].get(de) != elems[1].get(de)}
            if moved_elements:
                raise Unsettled(f"collating elements that differ between "
                                f"the tags in "
                                f"{', '.join(sorted({d for d, _ in moved_elements}))}")
            todo = [(d, el, parts) for (d, el), parts in sorted(
                        elems[0].items())
                    if elems[1].get((d, el)) == parts
                    and any(kind == 'u' and (key(cms[0], v) is None
                                             or key(cms[1], v) is None)
                            for kind, v in parts)]
            if not todo:
                continue
            runs = (TranslitRun(old, cms[0], a.source),
                    TranslitRun(new, cms[1], b.source))
            changed, chars, via = 0, set(), []
            for d, el, parts in todo:
                ko = _element_key(parts, cms[0], runs[0], d)
                kn = _element_key(parts, cms[1], runs[1], d)
                if ko[0] != kn[0]:
                    changed += 1
                    chars |= set(ko[1]) | set(kn[1])
                    if d not in via:
                        via.append(d)
            # Counted only once every element was built at both tags: a
            # build that could not be is listed with its reason, never
            # among those compared.
            elements += len(todo)
            builds += 1
            if changed:
                found.append(Translit(b.source, b.entry, p, changed, chars,
                                      via, None))
        except Unsettled as e:
            found.append(Translit(b.source, b.entry, p, 0, set(), [],
                                  str(e)))
    return TranslitReport(elements, builds, unmoved, found,
                          sum(f.changed for f in found if not f.reason))


def _clear_translit_memos():
    for memo in (_CTYPES, _LOWER, _HEADS, _NAMED, _ELEMS, _QUICK, _DEFINES,
                 _HAS_CTYPE, _REPMAP, _READING, _BARE, _UNESC, _RAW,
                 _RAWFROM):
        memo.clear()


# ------------------------------------------------------------- the check

Found = collections.namedtuple('Found', 'source entry charmaps chars via '
                                        'reason')
Found.__doc__ = """One listing. entry is the SUPPORTED entry, or None for a
source found by point 5; charmaps (old, new); chars: the members of K its
run names; via: the files that name them; reason: why it could not be
settled, or None."""

Report = collections.namedtuple(
    'Report', 'not_run builds charmaps changed sizes found skipped '
              'not_compared unsearched translit translit_skipped',
    defaults=(None, 0))
Report.__doc__ = """not_run: None, or why nothing was compared. builds: how
many builds were compared; charmaps: those they use; changed: [(old, new)]
whose blobs differ; sizes: {pair: (size of K, added, removed), or None when
the pair could not be compared}; found: [Found] listed for step 3;
skipped: [Found] whose source step 3 lists anyway; not_compared: the
SUPPORTED entries at both tags whose source is not at both; unsearched:
[(charmap, reason)] changed charmaps where point 5 could not run;
translit: the TranslitReport of point 6, None when no charmap changed, no
LC_CTYPE is read differently (ctype_moved) and both tags read bytes alike
(_readers_differ), its found without the builds step 3 lists
anyway, or `found` lists; translit_skipped: how many of those."""


def reset():
    """Forget what the per-text memos hold. They are kept for one step-2
    run: compare clears them unless the caller already did."""
    _SCANS.clear()
    _BLOCKS.clear()
    _clear_translit_memos()


def common_builds(old, new, builds_old, builds_new):
    """(common, not_compared): the [(old Build, new Build)] compared, and
    the entries of the pairs whose source is not at both tags."""
    # A build is a source in a charmap, whatever its entry is spelt: an
    # entry renamed between the tags (az_AZ.UTF-8 to az_AZ) is one build.
    # An entry that keeps its name and changes charmap is compared old
    # charmap against new. A build at one tag only is searched by point 5,
    # in step 2 (steps 6 to 8 leave point 5 out, search_unbuilt=False).
    old_by, new_by = {}, {}
    for by, builds in ((old_by, builds_old), (new_by, builds_new)):
        for b in builds:
            by.setdefault((b.source, b.charmap), b)
    old_entries = {b.entry: b for b in builds_old}
    pairs_ = [(old_by[k_], b) for k_, b in new_by.items() if k_ in old_by]
    pairs_ += [(old_entries[b.entry], b) for b in new_by.values()
               if b.entry in old_entries
               and old_entries[b.entry].charmap != b.charmap
               and old_entries[b.entry].source == b.source
               and (b.source, b.charmap) not in old_by
               and (b.source, old_entries[b.entry].charmap) not in new_by]
    common, not_compared = [], []
    for a, b in pairs_:
        if a.source in old.present and b.source in new.present:
            common.append((a, b))
        else:
            not_compared.append(b.entry)
    return common, not_compared


def compare(old, new, builds_old, builds_new, already_listed,
            read_more=lambda charmaps, locales=False: None, fresh=True,
            search_unbuilt=True):
    """Points 1 to 6 of the module docstring over two Sides.

    `already_listed()` returns the sources step 3 lists anyway; it is
    called only when something is found. `read_more({tag: {charmap}},
    locales)` reads those charmaps into the Sides, and with `locales` every
    locale file they do not hold (point 6). `fresh=False` keeps the memos
    of a caller that has used them on these Sides (translit_charmaps).
    `search_unbuilt=False` leaves out point 5, for two Sides whose builds
    are measured rather than read from SUPPORTED: a machine's builds are
    the locales its `locale -a` lists (node_charmaps), and one it does not
    build is not there for PostgreSQL to use.
    """
    if fresh:
        reset()
    common, not_compared = common_builds(old, new, builds_old, builds_new)
    used = sorted({a.charmap for a, _ in common}
                  | {b.charmap for _, b in common})
    missing = sorted({f"{a.charmap} at {old.tag}" for a, _ in common
                      if a.charmap not in old.charmap_oids}
                     | {f"{b.charmap} at {new.tag}" for _, b in common
                        if b.charmap not in new.charmap_oids})
    if missing:
        return Report(f"{g.SUPPORTED} names charmaps that are not there: "
                      f"{', '.join(missing)}", len(common), used, [], {}, [],
                      [], not_compared, [], None, 0)

    pairs = sorted({(a.charmap, b.charmap) for a, b in common})
    changed = [p for p in pairs
               if old.charmap_oids[p[0]] != new.charmap_oids[p[1]]]
    ks, bad = {}, {}
    for p in changed:
        try:
            co, cn = old.charmap(p[0]), new.charmap(p[1])
        except Unsettled as e:
            bad[p] = str(e)
            continue
        if co.code_set_name != cn.code_set_name:
            bad[p] = (f"the charmap's code_set_name is {co.code_set_name} at "
                      f"{old.tag} and {cn.code_set_name} at {new.tag}, and "
                      f"glibc refuses to load a category whose codeset does "
                      f"not match the locale's")
            continue
        ks[p] = K(delta(co, cn), co, cn)
    sizes = {p: ((ks[p].size, ks[p].added, ks[p].removed) if p in ks
                 else None) for p in changed}

    per_file = {}

    def file_result(side, n, p):
        """What one file contributes to any run in charmap pair p, done once
        per text and pair: (reason it cannot be read, or None; the members
        of K it names outside certain comments; its line ranges)."""
        text = side.texts[n]
        key_ = (id(text), p, side.tag)
        got = per_file.get(key_)
        if got is None:
            k, scan, reason, hits, lines = ks[p], side.scan(n), None, set(), ()
            if scan.absolute:
                reason = (f"{n} at {side.tag} uses a `...` range, which "
                          f"walks the charmap's bytes")
            elif scan.odd_keyword or (k.size and scan.odd):
                # A section this does not read can hold a `...` range,
                # which can move with K empty.
                reason = f"{n} at {side.tag}: {scan.odd}"
            elif k.size and scan.has_block:
                named = k.named(scan.names())
                if named and confirmed_hit(scan, k):
                    hits = named
                for lo, hi in scan.inline:
                    hits = hits | k.within(lo, hi)
                lines = scan.line_ranges
            got = per_file[key_] = (reason, frozenset(hits), lines)
        return got

    def evaluate(src_old, src_new, p):
        """(chars, via, reason) for one source pair in one charmap pair."""
        if p in bad:
            return set(), [], bad[p]
        k = ks[p]
        chars, via = set(), []
        try:
            for side, src in ((old, src_old), (new, src_new)):
                cl = side.closure(src)
                for n in cl:
                    reason, got, lines = file_result(side, n, p)
                    if reason:
                        raise Unsettled(reason)
                    for off in lines:
                        iv = range_interval(side, cl, n, off)
                        if iv is None:
                            raise Unsettled(
                                f"{n} at {side.tag}: a `..` range whose "
                                f"start depends on what the run defined "
                                f"before it")
                        got = got | k.within(*iv)
                    if got:
                        chars |= got
                        if n not in via:
                            via.append(n)
        except Unsettled as e:
            return chars, via, str(e)
        return chars, via, None

    found = []
    for a, b in common:
        p = (a.charmap, b.charmap)
        if p in ks or p in bad:
            chars, via, reason = evaluate(a.source, b.source, p)
            if chars or reason:
                found.append(Found(b.source, b.entry, p, chars, via, reason))

    built_in = collections.defaultdict(set)
    for a, b in common:
        if a.charmap == b.charmap:
            built_in[b.charmap].add(b.source)
    sources, unsearched = None, []
    for p in changed if search_unbuilt else ():
        if p[0] != p[1]:
            continue
        if p in bad:
            try:
                cs = new.charmap(p[1]).code_set_name.upper()
            except Unsettled:
                cs = None
            if cs not in CLIENT_ONLY_CODESETS:
                unsearched.append((p[1], bad[p]))
            continue
        # With K empty only a `...` range can still move (file_result checks
        # it first), and evaluate() finds that too.
        if new.charmap(p[1]).code_set_name.upper() in CLIENT_ONLY_CODESETS:
            continue
        if sources is None:
            sources = sorted(n for n in set(old.texts) & set(new.texts)
                             if old.scan(n).has_block
                             or new.scan(n).has_block
                             or old.scan(n).odd_keyword
                             or new.scan(n).odd_keyword)
        for src in sources:
            if src not in built_in[p[1]]:
                chars, via, reason = evaluate(src, src, p)
                if chars or reason:
                    found.append(Found(src, None, p, chars, via, reason))

    translit = None
    if common and (changed or ctype_moved(old, new)
                   # Unchanged files read differently across UTF8_READER
                   # (round 6, finding 3), asked of git here when nothing
                   # else runs point 6: the files that did not change are
                   # not read yet, so no plan can be made first.
                   or _readers_differ(old, new)):
        if not changed:
            read_more({}, locales=True)
        translit = compare_translit(old, new, common, ks, bad, read_more)

    skipped, tskipped = [], 0
    if found or (translit and translit.found):
        listed = already_listed()
        skipped = [f for f in found if f.source in listed]
        found = [f for f in found if f.source not in listed]
        if translit:
            entries = {f.entry for f in found if f.entry}
            kept = [f for f in translit.found if f.source not in listed
                    and f.entry not in entries]
            tskipped = len(translit.found) - len(kept)
            translit = translit._replace(found=kept)
    return Report(None, len(common), used, changed, sizes, found, skipped,
                  not_compared, unsearched, translit, tskipped)


def listed_sources(report):
    """The source names the report lists for step 3, sorted."""
    return sorted({f.source for f in report.found}
                  | {f.source for f in (report.translit.found
                                        if report.translit else ())})
