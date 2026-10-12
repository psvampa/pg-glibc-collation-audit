#!/usr/bin/env python3
"""
Step 2's comparison of the character sets locales are built with, between two
glibc tags (backlog 13.7).

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

What it cannot settle it lists with the reason, under a `!!` unless step 3
lists the locale anyway: for example a charmap of a shape this reader does
not model, a range whose start depends on what the run defined before it, a
`...` range or a form other than `..` and `....`, a second LC_COLLATE
section, a repertoiremap line, an escape_char this does not read with, a
range on a continued line, a copy target that is missing or copies in a
cycle, a code_set_name that differs between the two tags (glibc then refuses
to load the category, findlocale.c:256-296@2.39). A SUPPORTED this reads differently
than glibc, or a charmap it names that is not there, is NOT RUN.

Not covered here: a character the charmap gains that no rule names, which
takes the UNDEFINED weight -- step 4 lists every locale that relies on
UNDEFINED; transliteration, which builds collating elements out of other
characters (backlog 13.7, PR C); an LC_COLLATE keyword built out of escape
characters (`LC_COLL/ATE`), which steps 2 and 3 do not see either, and an
`escape_char` directive spelled that way.
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
                    and not _CODE_SET_NAME.fullmatch(arg))):
            raise Unsettled(f"charmap {name}: <{key_}> {arg}")
        seen[key_] = arg
        if key_ == 'comment_char':
            cc = arg
        elif key_ == 'escape_char':
            ec = arg
    if 'code_set_name' not in seen:
        raise Unsettled(f"charmap {name}: no <code_set_name>")
    mb_max = int(seen.get('mb_cur_max', '1'))
    mb_min = int(seen.get('mb_cur_min', str(mb_max)))
    if mb_min > mb_max:
        raise Unsettled(f"charmap {name}: <mb_cur_min> {mb_min} is over "
                        f"<mb_cur_max> {mb_max}")

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
# not only blanks and tabs.
_COPY_LINE = re.compile(r'[ \t\r\f\v]*copy[ \t\r\f\v]+"([^"\n]*)"')
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


# ------------------------------------------------------------- the check

Found = collections.namedtuple('Found', 'source entry charmaps chars via '
                                        'reason')
Found.__doc__ = """One listing. entry is the SUPPORTED entry, or None for a
source found by point 5; charmaps (old, new); chars: the members of K its
run names; via: the files that name them; reason: why it could not be
settled, or None."""

Report = collections.namedtuple(
    'Report', 'not_run builds charmaps changed sizes found skipped '
              'not_compared unsearched')
Report.__doc__ = """not_run: None, or why nothing was compared. builds: how
many builds were compared; charmaps: those they use; changed: [(old, new)]
whose blobs differ; sizes: {pair: (size of K, added, removed), or None when
the pair could not be compared}; found: [Found] listed for step 3;
skipped: [Found] whose source step 3 lists anyway; not_compared: the
SUPPORTED entries at both tags whose source is not at both; unsearched:
[(charmap, reason)] changed charmaps where point 5 could not run."""


def compare(old, new, builds_old, builds_new, already_listed):
    """Points 1 to 5 of the module docstring over two Sides.

    `already_listed()` returns the sources step 3 lists anyway; it is
    called only when something is found.
    """
    _SCANS.clear()
    _BLOCKS.clear()
    # A build is a source in a charmap, whatever its entry is spelt: an
    # entry renamed between the tags (az_AZ.UTF-8 to az_AZ) is one build.
    # An entry that keeps its name and changes charmap is compared old
    # charmap against new. A build at one tag only is searched by point 5.
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
    used = sorted({a.charmap for a, _ in common}
                  | {b.charmap for _, b in common})
    missing = sorted({f"{a.charmap} at {old.tag}" for a, _ in common
                      if a.charmap not in old.charmap_oids}
                     | {f"{b.charmap} at {new.tag}" for _, b in common
                        if b.charmap not in new.charmap_oids})
    if missing:
        return Report(f"{g.SUPPORTED} names charmaps that are not there: "
                      f"{', '.join(missing)}", len(common), used, [], {}, [],
                      [], not_compared, [])

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
    for p in changed:
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

    skipped = []
    if found:
        listed = already_listed()
        skipped = [f for f in found if f.source in listed]
        found = [f for f in found if f.source not in listed]
    return Report(None, len(common), used, changed, sizes, found, skipped,
                  not_compared, unsearched)


def listed_sources(report):
    """The source names the report lists for step 3, sorted."""
    return sorted({f.source for f in report.found})
