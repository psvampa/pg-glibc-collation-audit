"""One reader of glibc's locale sources, following localedef step by step.

The tool reads glibc's locale files to decide what changed between two
versions. That reading used to be written by hand in several modules, each a
little differently from the others and from glibc; every fix corrected one copy
and the next review found its twin (backlog block 15). Steps 1 to 4 and
9/10 read through this module (glibc_tree.py); steps 6 to 8, step 2's
lc_collate_unread check and its check of character sets and
transliterations (collation_keys) still read with readers of their own.

It reads the way localedef reads, for one glibc BUILD, in layers:

  1. Bytes. The file is read raw, never decoded. Before glibc 2.36, lr_getc
     returns EOF for a 0x1A byte without consuming it, and every byte >= 0x80
     comes back through a `char` (signed on x86_64), so 0xFF also reads as
     EOF (linereader.h:138@glibc-2.34). From 2.36 every byte is returned
     masked (linereader.h:137@glibc-2.39). A Build says which.
  2. Tokens. lr_token and its helpers in linereader.c, with the build's own
     keyword table read from locfile-kw.gperf and charmap-kw.gperf at the
     build's ref in the glibc clone, plus a distro's additions (RHEL 9 adds
     codepoint_collation, glibc-c-utf8-locale-1.patch).
  3. Structure. locfile_read's loop (locfile.c:107-263@2.39): the
     comment_char and escape_char directives, the category headers, each
     section to its END, `copy` in each of its three forms, and localedef's
     queue of files (localedef.c:258-282, 612-701@2.39): which categories
     each file is read for, when a copied file is read, and the fatal
     outcomes (a file it cannot open, a circular copy, a file copied twice
     into LC_COLLATE).

Inside a section, every statement is read as its first token plus
the rest of its logical line, which glibc drops with lr_ignore_rest. That is
what most statement handlers end with, but not all: some leave the rest of
the line to be read as further statements, and some read past the end of the
line. Where this reader sees a shape on which that model can differ from the
handler glibc runs, it says so in Run.unsettled rather than answer as if it
had read it. In LC_COLLATE it follows collate_read's state as well: a
statement glibc refuses in the state it comes in changes nothing
(Collate.refused); a collating-element's `from` string is read translated,
as collate_read reads it; and the characters each line names are the ones
in its tokens (Collate.characters), as far as the reader is sure of the
run (Run.named_lines).

The token layer is the one backlog 15.0 compared token by token with
glibc's linereader.c compiled at glibc-2.28, 2.34 and 2.39, on every locale
file and charmap of the three tags and on edge cases: no difference but the
leading `-` named below. This module's copy of it gives the same tokens as
that prototype on every file of the three tags (backlog 15.1). The
structure layer is held to localedef itself: tests/reader_oracle/ records what
localedef did on the RHEL 8, 9 and 10 fixtures for a generated set of cases,
and tests/test_glibc_reader.py compares this module with it.

Not modelled, on purpose: a `-` at the very start of a buffer makes get_ident
read lr->buf[-1] (linereader.c:578@2.39, after lr_ungetn at :329), which is
undefined behaviour, and the run is reported unsettled there
(GlibcUndefined): the byte glibc reads is the same on every line, since
getdelim reuses the buffer, and no byte of the line stands for it. So is a
word that is a keyword up to a NUL byte: gperf's lookup can read past the
keyword's text there, and what it finds is the binary's (GlibcUndefined
again). A translated string holding a character the charmap lacks makes
get_string try to transliterate it, which can load the file's own LC_CTYPE
in the middle of the run (linereader.c:640-660@2.39). Charmaps are read in a
later step of block 15; until then, where the reader sees an answer depend
on whether the charmap has a character, it reports that place unsettled.
Repertoire maps are not read either: a `repertoiremap` line that names a
map, and an LC_COLLATE read with the map a translit include named, are
reported unsettled. glibc keeps that map with the file the include queues,
when the include is what creates its entry, and hands it on to every file
that file's copies create, and theirs in turn (an include passes its own);
the reader follows it the same way (_Entry.repertoire).

Written for Python 3.6, the oldest the fixtures run, with the standard
library only.
"""
import bisect
import re
import subprocess

# ---------------------------------------------------------------------------
# Layers 1 and 2: bytes and tokens (linereader.c and linereader.h).

EOF_ = -1
_SPACE = frozenset(b' \t\n\v\f\r')
_HEX = frozenset(b'0123456789abcdefABCDEF')
_MASK64 = (1 << 64) - 1
# {(escape char, pre236): the bytes that end LR._symname_in_buffer's search}
_SYMNAME_STOP = {}


class ReaderError(Exception):
    """The reader could not build what it was asked for (a ref or a keyword
    table the clone does not hold). Never an answer about a locale."""


class GlibcUndefined(Exception):
    """glibc reads outside an object for this input, so what it reads is
    undefined and can decide an answer; run() reports the place unsettled
    with `why`. Two ways:

    - get_ident takes lr->buf[lr->idx - 1] with idx 0 when lr_ungetn could
      not step back over a `-` (linereader.c:329 and :578@2.39), as at the
      start of a file or of a line continued from one ending in `-` and the
      escape (two `define`/`ifdef` names glibc reads alike);
    - a word that is a keyword K up to a NUL byte: gperf's lookup compares
      `*str == *s && !strncmp (str + 1, s + 1, len - 1) && s[len] == '\\0'`
      (locfile-kw.h:614@2.39, :620@2.28), and where the word's hash lands
      on K's slot, s[len] is past K's literal: the byte there is wherever
      the binary put the next string. On the three fixtures some such
      words meet a NUL there and are read as K, others are not
      (tests/reader_oracle/cases.py, t30 to t33), and another build can
      lay its strings out otherwise. The charmap table's hash sends no
      such word to its keyword's slot (glibc-2.28, 2.34 and 2.39, and the
      fixtures' binaries), so a word read with it is not one of these."""

    def __init__(self, fname, line,
                 why='glibc reads outside its buffer here'):
        Exception.__init__(self, '%s:%d' % (fname, line))
        self.fname = fname
        self.line = line
        self.why = why


_PAST_KEYWORD = ('a word that is a keyword up to a NUL byte, where glibc\'s '
                 'keyword lookup can read past the keyword')


class GlibcHang(Exception):
    """localedef never returns for this input, one of two ways.

    get_symname has no EOF test (the do ... while ending at
    linereader.c:528@2.39, :493@2.34): when lr_getc keeps returning EOF
    without consuming anything -- the end of the input, a line holding only
    the escape character, or a 0x1A byte before 2.36 -- glibc appends
    (char) EOF to a growing buffer forever, and lr_token never returns.
    Or lr_token returns, but the same token every time (_repeats), inside
    skip_category, whose loop waits for END or EOF."""


class Build(object):
    """What the reader needs to know about one glibc build.

    pre236       lr_getc of glibc before 2.36 (0x1A reads as EOF, a byte
                 >= 0x80 goes through `char`), and no UTF-8 decoding of the
                 input. The caller decides it; for an upstream ref,
                 glibc_locale_data.LR_GETC_FIX says.
    signed_char  `char` is signed: x86_64, which the three fixtures are.
                 aarch64, s390x and ppc64le Linux have unsigned char.
    locfile_kw, charmap_kw
                 {keyword bytes: (glibc token name, symname_or_ident)}, the
                 build's gperf tables.
    """

    def __init__(self, pre236, signed_char, locfile_kw, charmap_kw, label='?'):
        self.pre236 = bool(pre236)
        self.signed_char = bool(signed_char)
        self.locfile_kw = locfile_kw
        self.charmap_kw = charmap_kw
        self.label = label
        # What the reader has already worked out for this build, kept for
        # as long as the Build lives. Each is a pure function of its key,
        # and the key holds every input of it, so a kept answer is the
        # answer the reader would compute again (see LR and
        # _Runner.statement). A run of every locale of a tree reads
        # iso14651_t1_common once per locale; this is what makes the
        # second reading cheap (backlog 15.2: 0.45 s to 0.22 s per locale
        # at glibc-2.39, every Run of glibc-2.12..2.39 identical).
        self.lines_of = {}        # file bytes -> its physical lines
        self.joins_of = {}        # (file bytes, escape) -> joined lines
        self.lookahead = {}       # statement lookahead, _Runner.statement

    @property
    def utf8(self):
        return not self.pre236

    def char_value(self, b):
        """The C `char` value of byte b (0..255)."""
        b &= 0xff
        return b - 256 if (self.signed_char and b >= 0x80) else b

    def byte_wide(self, b):
        """The wide value glibc before 2.36 stores for a raw or escaped byte
        b in a translated string: ADDWC ((uint32_t) ch), with ch the lr_getc
        result (linereader.c:661-668@2.34)."""
        return self.char_value(b) & 0xffffffff

    @staticmethod
    def parse_gperf(text):
        """{keyword: (token name, symname_or_ident)} from a gperf file's
        keyword section. Refuses a line it cannot read, so a table is never
        silently short."""
        lines = text.split('\n')
        seps = [i for i, line in enumerate(lines) if line == '%%']
        if len(seps) != 1:
            raise ReaderError('expected one %%%% line in the gperf file, '
                              'found %d' % len(seps))
        out = {}
        for line in '\n'.join(lines[seps[0] + 1:]).strip().split('\n'):
            f = [x.strip() for x in line.split(',')]
            if len(f) != 3 or not f[1].startswith('tok_') \
                    or f[2] not in ('0', '1'):
                raise ReaderError('unexpected gperf line %r' % line)
            out[f[0].encode('ascii')] = (f[1], int(f[2]))
        if not out:
            raise ReaderError('empty gperf keyword section')
        return out

    @classmethod
    def at(cls, repo, ref, pre236, signed_char=True, extra_locfile=None,
           label=None):
        """The build of glibc at `ref` in the clone at `repo`, its keyword
        tables read from the ref itself. extra_locfile adds what a distro
        patch adds: {keyword: (token name, symname_or_ident)}."""
        def show(path):
            p = subprocess.Popen(['git', '-C', repo, 'show',
                                  '%s:%s' % (ref, path)],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            out, err = p.communicate()
            if p.returncode != 0:
                raise ReaderError('git show %s:%s failed in %s: %s' % (
                    ref, path, repo, err.decode('utf-8', 'replace').strip()))
            return out.decode('ascii')
        lk = cls.parse_gperf(show('locale/programs/locfile-kw.gperf'))
        ck = cls.parse_gperf(show('locale/programs/charmap-kw.gperf'))
        for k, v in (extra_locfile or {}).items():
            lk[k.encode('ascii') if isinstance(k, str) else k] = v
        return cls(pre236, signed_char, lk, ck, label or ref)


# The three RHEL builds the fixtures run, as this reader needs them: the
# upstream tag each is built from, whether its lr_getc predates 2.36, and
# what Red Hat's patches add to the keyword table. Two of those facts are
# measured: on 2026-10-12 a 0x1A or 0xFF byte above LC_COLLATE stopped
# localedef on el8 and el9 and not on el10, and codepoint_collation was a
# keyword on el9 and el10 and not on el8 (backlog 15.0), and
# tests/reader_oracle/ checks both on every run. The rest of el8's table is
# taken as upstream 2.28's, not compared.
RHEL_BUILDS = {
    'rhel8': ('glibc-2.28', True, {}),
    'rhel9': ('glibc-2.34', True,
              {'codepoint_collation': ('tok_codepoint_collation', 0)}),
    'rhel10': ('glibc-2.39', False, {}),
}


def rhel_build(repo, name):
    """The Build for one of RHEL_BUILDS, read from the clone at `repo`."""
    tag, pre236, extra = RHEL_BUILDS[name]
    return Build.at(repo, tag, pre236, signed_char=True,
                    extra_locfile=extra, label=name)


class Tok(object):
    """One token: tok is 'eof', 'eol', 'kw', 'ident', 'bsymbol', 'ucs4',
    'charcode', 'number', 'string', 'error', a punctuation name or an
    ellipsis name. A keyword carries its glibc token name in kwtok."""
    __slots__ = ('tok', 'val', 'line', 'items', 'raw', 'closed', 'kwtok')

    def __init__(self, tok, val=None, line=0):
        self.tok = tok
        self.val = val
        self.line = line
        self.items = None
        self.raw = None
        self.closed = True
        self.kwtok = None

    def __repr__(self):
        return 'Tok(%s,%r,L%d)' % (self.tok, self.val, self.line)


# Every field of LR, and how _Runner.statement's kept lookahead covers it.
# A field missing here is one the key may lack: tests/test_glibc_reader.py
# fails when LR's fields and these differ.
LR_KEY_FIELDS = {
    'data': 'in the key', 'table': 'in the key',
    'pi': 'in the key (snapshot)', 'buf': 'in the key (snapshot)',
    'bufact': 'in the key (snapshot)', 'idx': 'in the key (snapshot)',
    'lineno': 'in the key (snapshot)', '_cc': 'in the key',
    '_ec': 'in the key', 'translate_strings': 'in the key',
    'return_widestr': 'in the key',
    'build': 'the kept answers live on it',
    'fname': 'only in the text of a GlibcHang, which the lookahead catches, '
             'or of a GlibcUndefined, which leaves before anything is kept',
    'utf8': 'from build', 'kw': 'from build and table',
    'getc': 'from build', 'phys': 'from data', 'empty': 'from data',
    'met': 'an answer, not an input: kept with each lookahead and added '
           'back when one is used',
    'words': 'an answer, not an input: kept with each lookahead and added '
             'back when one is used',
}


class LR(object):
    """glibc's struct linereader over one file's bytes: lr_create, lr_next,
    lr_getc, lr_ungetc, lr_ungetn, lr_ignore_rest and lr_token."""

    def __init__(self, data, fname='?', build=None, table='locfile'):
        if not isinstance(build, Build):
            raise TypeError('LR needs a Build')
        self.fname = fname
        self.build = build
        self.data = data
        self.table = table
        self.utf8 = build.utf8
        self.kw = build.locfile_kw if table == 'locfile' else build.charmap_kw
        if build.pre236:
            self.getc = (self._getc_pre236_s if build.signed_char
                         else self._getc_pre236_u)
        else:
            self.getc = self._getc_post236
        # The physical lines, each with its newline, as getdelim returns them.
        # Only read, never changed, so files with the same bytes share them.
        self.phys = build.lines_of.get(data)
        if self.phys is None:
            self.phys = []
            start = 0
            while start < len(data):
                j = data.find(b'\n', start)
                if j < 0:
                    self.phys.append(data[start:])
                    break
                self.phys.append(data[start:j + 1])
                start = j + 1
            build.lines_of[data] = self.phys
        self.pi = 0
        self._cc = 0x23          # comment_char '#' (linereader.c:79@2.39)
        self._ec = 0x5c          # escape_char '\' (:80)
        self.translate_strings = 1
        self.return_widestr = 0
        self.lineno = 1
        # What another build could read differently (Run.met, Run.words).
        self.met = False
        self.words = set()
        if not self.phys:        # getdelim < 0: lr_create returns NULL
            self.buf = bytearray()
            self.bufact = 0
            self.idx = 0
            self.empty = True
            return
        self.empty = False
        b = self.phys[0]
        self.pi = 1
        n = len(b)
        # lr_create tests a literal backslash, not escape_char (:95).
        if n > 1 and b[n - 2] == 0x5c and b[n - 1] == 10:
            n -= 2
        self.buf = bytearray(b[:n])
        self.bufact = n
        self.idx = 0

    # comment_char and escape_char are `char` (linereader.h:71-72@2.39):
    # assigning a byte stores its C char value, reading gives that value.
    @property
    def comment_char(self):
        return self._cc

    @comment_char.setter
    def comment_char(self, v):
        self._cc = self.build.char_value(v)

    @property
    def escape_char(self):
        return self._ec

    @escape_char.setter
    def escape_char(self, v):
        self._ec = self.build.char_value(v)

    def snapshot(self):
        return (self.pi, bytes(self.buf), self.bufact, self.idx, self.lineno)

    def restore(self, snap):
        self.pi, buf, self.bufact, self.idx, self.lineno = snap
        self.buf = bytearray(buf)

    def lr_next(self):
        if self.pi >= len(self.phys):
            return -1
        b = self.phys[self.pi]
        self.pi += 1
        self.lineno += 1
        n = len(b)
        # char == char here: a byte comparison (linereader.c:162@2.39).
        if n > 1 and b[n - 2] == (self._ec & 0xff) and b[n - 1] == 10:
            n -= 2
        self.buf = bytearray(b[:n])
        self.bufact = n
        self.idx = 0
        return 0

    def _getc_post236(self):
        if self.idx == self.bufact:
            if self.bufact != 0:
                if self.lr_next() < 0:
                    return EOF_
            if self.bufact == 0:
                return EOF_
        c = self.buf[self.idx]
        if c >= 0x80 or c == 0x1a:
            self.met = True
        self.idx += 1
        return c

    def _getc_pre236_s(self):
        if self.idx == self.bufact:
            if self.bufact != 0:
                if self.lr_next() < 0:
                    return EOF_
            if self.bufact == 0:
                return EOF_
        c = self.buf[self.idx]
        if c >= 0x80 or c == 0x1a:
            self.met = True
        if c == 0x1a:
            return EOF_
        self.idx += 1
        return c - 256 if c >= 0x80 else c

    def _getc_pre236_u(self):
        if self.idx == self.bufact:
            if self.bufact != 0:
                if self.lr_next() < 0:
                    return EOF_
            if self.bufact == 0:
                return EOF_
        c = self.buf[self.idx]
        if c >= 0x80 or c == 0x1a:
            self.met = True
        if c == 0x1a:
            return EOF_
        self.idx += 1
        return c

    def stuck(self):
        """True when lr_getc will return EOF forever without consuming."""
        if self.idx == self.bufact:
            return self.bufact == 0 or self.pi >= len(self.phys)
        return self.build.pre236 and self.buf[self.idx] == 0x1a

    def ungetn(self, n):
        if self.idx < n:
            return -1
        self.idx -= n
        return 0

    def ungetc(self, ch):
        if self.idx == 0:
            return -1
        if ch != EOF_:
            self.idx -= 1
            self.buf[self.idx] = ch & 0xff
        return 0

    def ignore_rest(self):
        """lr_ignore_rest (linereader.c:113-139@2.39): the verbose branch
        only prints; the effect is to drop the rest of the logical line,
        every continued physical line included."""
        while self.bufact > 0 and self.buf[self.bufact - 1] != 10:
            if self.lr_next() < 0:
                break
        self.idx = self.bufact

    def token(self):
        getc = self.getc
        while True:
            while True:
                ch = getc()
                if ch == EOF_:
                    return Tok('eof', line=self.lineno)
                if ch == 10:
                    return Tok('eol', line=self.lineno)
                if ch not in _SPACE:
                    break
            if ch != self._cc:
                break
            if self.buf[self.bufact - 1] != 10:
                self.idx = self.bufact
                continue
            self.ignore_rest()
            return Tok('eol', line=self.lineno)
        line = self.lineno
        if ch == self._ec:
            return self.toplvl_escape(line)
        if ch == 0x2e:  # '.'
            rest = bytes(self.buf[self.idx:self.idx + 10])
            if rest.startswith(b'...(2)....'):
                for _ in range(10):
                    getc()
                return Tok('ellipsis4_2', line=line)
            if rest.startswith(b'...'):
                getc(); getc(); getc()
                return Tok('ellipsis4', line=line)
            if rest.startswith(b'..'):
                getc(); getc()
                return Tok('ellipsis3', line=line)
            if rest.startswith(b'.(2)..'):
                for _ in range(6):
                    getc()
                return Tok('ellipsis2_2', line=line)
            if rest.startswith(b'.'):
                getc()
                return Tok('ellipsis2', line=line)
        if ch == 0x3c:  # '<'
            return self.symname(line)
        if 0x30 <= ch <= 0x39:
            v = ch - 48
            while True:
                ch = getc()
                if 0x30 <= ch <= 0x39:
                    # tok_number is an unsigned long (linereader.h:47@2.39).
                    v = (v * 10 + ch - 48) & _MASK64
                else:
                    break
            self.ungetn(1)
            return Tok('number', v, line)
        if ch == 0x3b:
            return Tok('semicolon', line=line)
        if ch == 0x2c:
            return Tok('comma', line=line)
        if ch == 0x28:
            return Tok('open_brace', line=line)
        if ch == 0x29:
            return Tok('close_brace', line=line)
        if ch == 0x22:
            return self.string(line)
        if ch == 0x2d:  # '-'
            ch2 = getc()
            if ch2 == 0x31:
                return Tok('minus1', line=line)
            self.ungetn(2)
        elif ch >= 0x80 and self.utf8:   # case 0x80 ... 0xff (2.36+)
            wch = self.utf8_decode(ch)
            if wch is None:
                return Tok('error', line=line)
            return Tok('ucs4', wch, line)
        return self.ident(line)

    def utf8_decode(self, ch1):
        if ch1 < 0xc2:
            return None
        ch2 = self.getc()
        if ch2 < 0 or (ch2 & 0xc0) != 0x80:
            return None
        if ch1 <= 0xdf:
            r = ((ch1 & 0x1f) << 6) | (ch2 & 0x3f)
            return r if r >= 0x80 else None
        ch3 = self.getc()
        if ch3 < 0 or (ch3 & 0xc0) != 0x80 or ch1 < 0xe0:
            return None
        if ch1 <= 0xef:
            r = ((ch1 & 0x0f) << 12) | ((ch2 & 0x3f) << 6) | (ch3 & 0x3f)
            return r if r >= 0x800 else None
        ch4 = self.getc()
        if ch4 < 0 or (ch4 & 0xc0) != 0x80 or ch1 < 0xf0 or ch1 > 0xf4:
            return None
        r = (((ch1 & 7) << 18) | ((ch2 & 0x3f) << 12) | ((ch3 & 0x3f) << 6)
             | (ch4 & 0x3f))
        return r if r >= 0x10000 else None

    def toplvl_escape(self, line):
        getc = self.getc
        nbytes = []
        while True:
            base = 8
            ch = getc()
            if ch == 0x64:    # 'd'
                base = 10
                ch = getc()
            elif ch == 0x78:  # 'x'
                base = 16
                ch = getc()
            if base == 16:
                ok = ch in _HEX
            else:
                ok = 0x30 <= ch < 0x30 + base
            if not ok:
                while ch != EOF_ and ch not in _SPACE:
                    ch = getc()
                return Tok('error', line=line)
            byte = int(chr(ch), 16)
            ch = getc()
            if base == 16:
                ok = ch in _HEX
            else:
                ok = 0x30 <= ch < 0x30 + base
            if not ok:
                while ch != EOF_ and ch not in _SPACE:
                    ch = getc()
                return Tok('error', line=line)
            byte = byte * base + int(chr(ch), 16)
            ch = getc()
            if base != 16 and 0x30 <= ch <= 0x39:
                byte = byte * base + ch - 48
                ch = getc()
            nbytes.append(byte & 0xff)
            if not (ch == self._ec and len(nbytes) < 16):
                break
        self.ungetn(1)
        return Tok('charcode', bytes(nbytes), line)

    def symname(self, line):
        # ch is what ended get_symname's loop, `>` or the newline. It is not
        # always the last byte appended: when the escape character is `>`,
        # the loop appends the byte after it and stops on the `>` itself.
        got = self._symname_in_buffer()
        b, ch = got if got is not None else self._symname_slow()
        if len(b) and b[0] == 0x55 and len(b) in (6, 10):
            if all(c in _HEX for c in b[1:-1]):
                return Tok('ucs4', int(bytes(b[1:-1]), 16), line)
        name = bytes(b[:-1])
        k = self.kw.get(name)
        if k is not None and k[1] == 1:
            t = Tok('kw', name.decode('ascii'), line)
            t.kwtok = k[0]
            return t
        t = Tok('bsymbol', name, line)
        if ch != 0x3e:
            t.closed = False
        return t

    def _symname_in_buffer(self):
        """(the bytes get_symname appends, the byte that ended its loop) when
        the name ends inside the line already in the buffer, at a `>` or the
        newline that is appended last; None when this cannot say, and
        _symname_slow reads it byte by byte.

        Between here and the first `>` or newline, the loop of get_symname
        (linereader.c:510-530@2.39) appends each byte lr_getc returns, as
        it is: no byte there is the escape character, and none is one
        lr_getc reads as EOF (0x1A before 2.36; 0xFF too, through a signed
        char, but its masked value is appended all the same, which is what
        is kept here). The escape character is searched for as the byte it
        is; it can be NUL or negative only as a C char value (a byte at or
        above 0x80 on a signed-char build), which no lr_getc value equals
        but EOF, and `>` or the newline as escape change what ends the
        name: those are left to _symname_slow.
        """
        ec = self._ec
        if ec <= 0 or ec in (0x3e, 10):
            return None
        key = (ec, self.build.pre236)
        stop = _SYMNAME_STOP.get(key)
        if stop is None:
            stop = _SYMNAME_STOP[key] = re.compile(
                b'[>\n' + re.escape(bytes([ec]))
                + (b'\x1a' if self.build.pre236 else b'') + b']')
        m = stop.search(self.buf, self.idx, self.bufact)
        if m is None or self.buf[m.start()] not in (0x3e, 10):
            return None
        end = m.start() + 1
        b = bytearray(self.buf[self.idx:end])
        if not b.isascii() or 0x1a in b:
            # What lr_getc would have flagged, byte by byte.
            self.met = True
        self.idx = end
        return b, b[-1]

    def _symname_slow(self):
        getc = self.getc
        ec = self._ec
        b = bytearray()
        while True:
            ch = getc()
            if ch == ec:
                c2 = getc()
                b.append(c2 & 0xff)
                if c2 == 10:
                    ch = 10
                elif c2 == EOF_ and self.stuck():
                    raise GlibcHang('%s:%d: symbolic name not closed before '
                                    'the end of the input'
                                    % (self.fname, self.lineno))
            else:
                b.append(ch & 0xff)
                if ch == EOF_ and self.stuck():
                    raise GlibcHang('%s:%d: symbolic name not closed before '
                                    'the end of the input'
                                    % (self.fname, self.lineno))
            if ch == 0x3e or ch == 10:
                return b, ch

    def ident(self, line):
        getc = self.getc
        ec = self._ec
        if self.idx == 0:
            raise GlibcUndefined(self.fname, line)
        b = bytearray([self.buf[self.idx - 1]])
        while True:
            ch = getc()
            if ch == EOF_ or ch in _SPACE or ch == 0x22 or ch == 0x3b or \
                    ch == 0x3c or ch == 0x2c:
                break
            if ch == ec:
                ch = getc()
                if ch == 10 or ch == EOF_:
                    break
            b.append(ch & 0xff)
        self.ungetc(ch)
        s = bytes(b)
        self.words.add(s)
        if self.table == 'locfile' and 0 in s:
            # The words gperf may take for a keyword that is their prefix
            # (GlibcUndefined): only one of kind 0 makes a keyword token.
            # The prefix is looked up too, so another build's table that
            # gives it another entry reads the run differently (Run.words).
            pre = s[:s.index(0)]
            self.words.add(pre)
            k = self.kw.get(pre)
            if k is not None and k[1] == 0:
                raise GlibcUndefined(self.fname, line, _PAST_KEYWORD)
        k = self.kw.get(s)
        if k is not None and k[1] == 0:
            t = Tok('kw', s.decode('ascii'), line)
            t.kwtok = k[0]
            return t
        return Tok('ident', s, line)

    def string(self, line):
        """A string token. .raw is the untranslated bytes (get_string with
        translate_strings == 0); .items is what the translating branch sees
        before the charmap: ('cp', codepoint) / ('name', bytes) /
        ('byte', b) / ('ucp', cp) / ('illegal', why)."""
        getc = self.getc
        ec = self._ec
        t = Tok('string', None, line)
        if not self.translate_strings:
            b = bytearray()
            while True:
                ch = getc()
                if ch == 0x22 or ch == 10 or ch == EOF_:
                    break
                b.append(ch & 0xff)
            eb = ec & 0xff
            # A single escape at the end is dropped (linereader.c:800-806).
            if b and b[-1] == eb and (len(b) == 1 or b[-2] != eb):
                del b[-1]
            t.raw = bytes(b)
            t.closed = ch == 0x22
            return t
        utf8 = self.utf8
        items = []
        while True:
            ch = getc()
            if ch == 0x22 or ch == 10 or ch == EOF_:
                break
            if ch != 0x3c:
                if ch == ec:
                    ch = getc()
                    if utf8 and ch >= 0x80:
                        items.append(('illegal', '8-bit escape'))
                        break
                    if ch == 10 or ch == EOF_:
                        break
                    items.append(('cp', ch) if 0 <= ch < 0x80
                                 else ('byte', ch & 0xff))
                elif 0 <= ch < 0x80:
                    items.append(('cp', ch))
                else:
                    if utf8:
                        w = self.utf8_decode(ch)
                        if w is None:
                            items.append(('illegal', 'bad utf8'))
                            break
                        items.append(('ucp', w))
                    else:
                        items.append(('byte', ch & 0xff))
                continue
            nb = bytearray()
            while True:
                ch = getc()
                if ch == 0x3e or ch == 10 or ch == EOF_:
                    break
                if ch == ec:
                    ch = getc()
                    if ch == 10 or ch == EOF_:
                        break
                nb.append(ch & 0xff)
            if ch == 10 or ch == EOF_:
                break
            if len(nb) == 0:
                items.append(('illegal', '<>'))
                continue
            if nb[0] == 0x55 and len(nb) in (5, 9) and \
                    all(c in _HEX for c in nb[1:]):
                items.append(('ucp', int(bytes(nb[1:]), 16)))
                continue
            items.append(('name', bytes(nb)))
        if ch == 10 or ch == EOF_:
            items.append(('illegal', 'unterminated'))
            t.closed = False
        t.items = items
        return t


# ---------------------------------------------------------------------------
# Layer 3: the structure of a file and localedef's queue of files.

CATEGORIES = ('LC_CTYPE', 'LC_NUMERIC', 'LC_TIME', 'LC_COLLATE',
              'LC_MONETARY', 'LC_MESSAGES', 'LC_PAPER', 'LC_NAME',
              'LC_ADDRESS', 'LC_TELEPHONE', 'LC_MEASUREMENT',
              'LC_IDENTIFICATION')
_CAT_OF_TOK = {'tok_' + c.lower(): c for c in CATEGORIES}


# Tokens that matter to the structure when a handler leaves them to be read
# as a statement (see _Runner.statement).
_TAIL_KEYWORDS = {
    None: frozenset(('tok_end',)),
    'LC_COLLATE': frozenset(('tok_end', 'tok_copy', 'tok_define',
                             'tok_undef', 'tok_ifdef', 'tok_ifndef',
                             'tok_elifdef', 'tok_elifndef', 'tok_else',
                             'tok_endif')),
    'LC_CTYPE': frozenset(('tok_end', 'tok_translit_start',
                           'tok_translit_end')),
    # STR_ELEM and INT_ELEM leave the rest of their line (ld-time.c:849-909
    # @2.39), and an array keyword there reads on past the line, an END
    # below included (false-negative-reviewer, measured on el8/el9/el10).
    'LC_TIME': frozenset(('tok_end', 'tok_abday', 'tok_day', 'tok_abmon',
                          'tok_mon', 'tok_am_pm', 'tok_alt_digits',
                          'tok_alt_mon', 'tok_ab_alt_mon')),
}

# STRARR_ELEM fields of LC_TIME and their most values (ld-time.c:801-808).
_TIME_ARRAYS = {'tok_abday': 7, 'tok_day': 7, 'tok_abmon': 12, 'tok_mon': 12,
                'tok_am_pm': 2, 'tok_alt_digits': 100, 'tok_alt_mon': 12,
                'tok_ab_alt_mon': 12}

_CONDITIONALS = frozenset(('tok_define', 'tok_undef', 'tok_ifdef',
                          'tok_ifndef', 'tok_elifdef', 'tok_elifndef',
                          'tok_else', 'tok_endif'))


def _kw(t, name):
    return t.tok == 'kw' and t.kwtok == name


_RANGE_TOKS = frozenset(('ellipsis2', 'ellipsis3', 'ellipsis4'))


def _own_entry(toks):
    """Does an LC_COLLATE statement open with a character and hold no
    range? Such a line steers no other line by what it says; whether
    another line names its character is _named_places' question. See
    Collate.instructions."""
    return toks[0].tok == 'ucs4' and not _ranges(toks)


def _in_order(toks):
    """Does the statement place something in the order, where glibc's
    cursor stands after it (insert_weights, ld-collate.c:692-945@2.34):
    a character, a symbol or element, UNDEFINED, a range?"""
    t = toks[0]
    return t.tok in ('ucs4', 'bsymbol') or t.tok in _RANGE_TOKS \
        or _kw(t, 'tok_undefined')


def _ranges(toks):
    return any(x.tok in _RANGE_TOKS for x in toks)


class Fatal(object):
    """A localedef run that ends without output: `kind` is 'missing' (a file
    it cannot open, exit 4), 'circular' or 'duplicate' (exit 5), 'hang'
    (localedef never returns: GlibcHang, or a cycle of `copy` names), or
    'crash' (a NULL dereference or a failed assertion). Each is measured on
    the RHEL 8, 9 and 10 builds by a case in tests/reader_oracle/ (the
    translit include with a NULL name by y009, y012 and the other y cases
    at that place that exit -11), except get_symname's GlibcHang, whose
    loop was measured against glibc's
    compiled line reader (backlog 15.0), not against localedef; the
    skip_category one is measured against localedef (s47, s48, s50).
    `name` is the file the message names, `where` the file and line being
    read."""

    def __init__(self, kind, name, where=None, detail=None):
        self.kind = kind
        self.name = name
        self.where = where
        self.detail = detail

    def __repr__(self):
        return 'Fatal(%s, %r)' % (self.kind, self.name)


class _Stop(Exception):
    def __init__(self, fatal):
        Exception.__init__(self, fatal.kind)
        self.fatal = fatal


class Collate(object):
    """One `struct locale_collate_t`: the statements read into it, from
    whichever files and sections glibc merged into it, in reading order.
    Each statement is (file name, line, tokens of its logical line).

    `instructions` holds (file name, first line, last line) of the lines
    that steer other lines: every statement read into it that does not
    open with a `<U....>` character (tok_ucs4, ld-collate.c:3475@2.34),
    UNDEFINED included, or that holds a range, which names characters on
    no line, and the entries a range statement (one that opens with the
    ellipsis, ld-collate.c:3723-3738@2.34; in the weights of any other
    order line it is a syntax error, :877-900) runs between, its end
    points: the last two entries placed in the order before it, the one
    glibc's cursor stands on when this reader is sure of it
    (handle_ellipsis, ld-collate.c:1072-1095@2.34; _Runner.instruction;
    an order_start or a script line places nothing, the entries a copied
    file places do) and the
    one before, in case glibc rejected the last (a character placed a
    second time, :1057-1063); after a reorder-after the cursor is its
    anchor, which _named_places marks; and every entry after it to the
    next one of the same file, whose collate_read closes the range; a
    conditional, and each line skip_to meets one on; a `copy`; a `define`
    before the opening copy; codepoint_collation; every physical line of
    the section that ends
    in the escape character, with the line it joins (_Runner.joined); and
    the lines that place a character another statement names as a
    reorder-after anchor or a weight (_named_places, after the run).
    Editing one moves rules on lines that did not change (a reorder-after
    moves the entries after it, an ifdef picks other lines, a line joined
    to the next swallows it), so step 2 cannot name the characters that
    move by reading the changed lines (backlog 13.36). `before`,
    `open_ranges`, `ranges` and `state` are the reading's state for a
    range's end points: the last two entries placed, the range each file
    has open, every range read, and each file's collate_read state.
    `refused` holds the index in `statements` of each statement glibc sent
    to err_label for the state it came in (_refused): it changed nothing."""

    def characters(self):
        """{file: {line: [code points] or None}}: the characters each line
        of each file names in a statement glibc accepted
        (statement_characters), each once, in reading order; None for a
        line this reader cannot say that of. A statement's characters go
        to every line it spans. A line no statement names a character on
        is not there, and neither is a refused one: err_label drops it
        unread (ld-collate.c:3958-3960@2.34). A statement spans the lines
        from its first token to where the reader stood after it (`last`,
        which _Runner.instruction records), a string continued over an
        escaped newline included. Step 2 prints them under each changed
        file (backlog 13.29)."""
        out = {}
        for i, (fname, line, toks) in enumerate(self.statements):
            if i in self.refused or (self.unsure_from is not None
                                     and i >= self.unsure_from):
                continue
            cps = statement_characters(toks)
            if cps == []:
                continue
            lines = out.setdefault(fname, {})
            end = max([self.last.get(i, line)] + [t.line for t in toks])
            for n in range(line, end + 1):
                have = lines.get(n, [])
                if cps is None or have is None:
                    lines[n] = None
                    continue
                have = list(have)
                for c in cps:
                    if c not in have:
                        have.append(c)
                lines[n] = have
        return out

    def __init__(self):
        self.statements = []
        self.instructions = []
        self.before = []              # the last two entries, (span, key)
        self.open_ranges = {}         # file -> its range waiting for an end
        self.ranges = []              # every range read: file, line, ends
        self.state = {}               # file -> collate_read's state
        self.else_action = 'none'     # else_none / else_ignore / else_seen
        self.order_started = False
        self.refused = set()          # statements glibc sent to err_label
        self.last = {}                # statement index -> its last line
        # The statements before this index are the ones read before
        # Run.unsure_at; None while the reader is sure.
        self.unsure_from = None


class Section(object):
    """One category section as glibc read it.

    start      line of the category keyword
    end        line of the `END` that closed it, or None when the file ended
    content    False when the file was read for other categories only
               (ignore_content), so nothing of it was stored
    copy       (name, line) of a `copy` that opens the section
    mid_copies [(name, line)] of `copy` statements inside LC_COLLATE
    """

    def __init__(self, category, fname, start, content):
        self.category = category
        self.fname = fname
        self.start = start
        self.end = None
        self.content = content
        self.copy = None
        self.mid_copies = []

    def __repr__(self):
        return 'Section(%s %s:%s-%s)' % (self.category, self.fname,
                                         self.start, self.end)


class FileRead(object):
    """One call of locfile_read: the file, the directives it obeyed, its
    sections, and `cut`: the line where lr_getc gave the top
    level an EOF with the file not read to its end, or None. glibc reads
    nothing of the file after it (locfile.c:115@2.34). Three bytes do it: a
    line holding only the escape character (cases b14, b15, every build),
    and before 2.36 a 0x1A (b02) or an 0xFF through a signed char (b03)."""

    def __init__(self, name):
        self.name = name
        self.directives = []      # (line, 'comment_char'|'escape_char', byte)
        self.sections = []
        self.cut = None


class _Entry(object):
    """localedef's `struct localedef_t`: one file in the read list."""

    def __init__(self, name, repertoire=None):
        self.name = name
        # The repertoire map's name the entry was created with
        # (add_to_readlist, localedef.c:628-629@2.39): a translit
        # include's, or that of the file whose copy created it, which
        # glibc takes at each read of the file (locfile.c:52, :58-59@2.39)
        # and passes to every entry that file's copies create; or None.
        self.repertoire = repertoire
        self.needed = set()
        self.avail = set()
        self.data = {}            # category -> stored data (categories[])
        self.copy_name = {}


class Run(object):
    """What one localedef run reads (see run()).

    fatal      None, or a Fatal: the run writes nothing.
    reads      [FileRead], in the order glibc read the files.
    collate    the Collate the output's LC_COLLATE is built from, or None
               when glibc stored none: localedef then warns "No definition
               for LC_COLLATE category found" and writes the 92-byte file
               that loads as no order at all (ld-collate.c:1566-1571 and
               :2120-2146@2.39). None too when the run is fatal, or stopped
               where glibc reads outside an object (GlibcUndefined), which
               is unsettled.
    unsettled  [(file, line, why)]: places where this reader's model of a
               statement can differ from the handler glibc runs, or where
               what localedef writes depends on -c. A caller
               that needs a settled answer refuses one with any of these.
    unsure_at  (file, line) of the first of them met while reading, a
               GlibcUndefined included, or None: what the reader read
               before it is what glibc reads, and nothing after it is
               known (named_lines). The ones _ranges_settled adds once the
               run has ended are not such a place: they are about which
               characters a range puts on no line, and every line of a
               range is an instruction already.
    began      every file whose LC_COLLATE the run began to read, in order.
    finished   the files of `began` whose LC_COLLATE read had ended at
               unsure_at.
    met        True when lr_getc read, or stopped at, a byte above 0x7F or
               a 0x1A, in any file of the run: the only bytes the builds on
               the two sides of glibc 2.36 read differently (lr_getc reads
               0x1A, and 0xFF through a signed char, as the end of the file
               before it; a token that opens with a byte above 0x7F is a
               UTF-8 character from it). A run that met none reads alike
               with both, step by step.
    words      every bare word looked up in a keyword table (get_ident): a
               build whose table gives one of them another entry reads the
               run differently. A <name> is looked up too, for the
               keywords read as one (k[1] == 1), and is not kept: there are
               tens of thousands per run, and no locfile table of
               glibc-2.12..2.39 has such a keyword; glibc_tree.across
               treats every run as one to compare when two tables differ in
               one.
    """

    def __init__(self):
        self.fatal = None
        self.reads = []
        self.collate = None
        self.unsettled = []
        self.unsure_at = None
        self.began = []
        self.finished = frozenset()
        self.met = False
        self.words = set()

    def named_lines(self):
        """What this run tells of the characters each line names, as
        ({file: (lines, cut)}, rest), by one rule: what the reader read
        before the first place it could not settle (`unsure_at`) is
        known, and nothing after it is.

        lines  Collate.characters() of the file: {line: [code points], or
               None for a line this reader cannot say that of}; a line
               not there names none. With a cut, the lines before it
               only: a place met inside _Runner.instruction comes after
               its statement was stored, and that statement's lines are
               not known either (review of the one-rule rewrite: the map
               held the cut's own line, read as no map at all).
        cut    None, or the first line from which the file's lines are
               not known: its LC_COLLATE read had not ended at that place,
               and glibc may read every line after it otherwise. Known up
               to the last line of its last statement before the place,
               and short of the place's own line when the place is in the
               file: codepoint_collation leaves the rest of its line to be
               read as statements (ld-collate.c:2691-2693@2.39).
        rest   True when a file not in the answer cannot be told either:
               after that place glibc may read any file, through a `copy`
               the reader did not read as glibc does. False when such a
               file names nothing in this run.

        A file whose read had ended at the place is known whole: an
        opening copy reads the copied file to its end first
        (iso14651_t1_common for the locales that copy am_ET at glibc-2.12
        and 2.17, which reorder-sections-after leaves unsettled). A run
        with no Collate (a fatal one, one that hangs, one stopped where
        glibc reads outside an object, one in which glibc stored no
        LC_COLLATE) gives no file: when the reader was sure throughout,
        localedef writes nothing (Fatal) or an LC_COLLATE with no rules
        (collate_finish and collate_output with no definition,
        ld-collate.c:1563-1568 and :2117-2131@2.34), so the run names
        nothing; otherwise glibc may never reach that end, and nothing is
        known. Rounds 1 to 4 of the 15.3 C2 review each found
        one more route by which a reading the reader was not sure of
        reached step 2 as a sure one; this is the one place that decides
        it, and the callers only keep and join its answer."""
        c, sure = self.collate, self.unsure_at is None
        if c is None:
            return {}, not sure
        named = c.characters()
        out = {f: (named.get(f, {}), None)
               for f in dict.fromkeys(self.began + list(named))}
        if sure:
            return out, False
        known = {}
        for i, (fname, line, toks) in enumerate(
                c.statements[:c.unsure_from]):
            end = max([c.last.get(i, line)] + [t.line for t in toks])
            known[fname] = max(known.get(fname, 0), end)
        at_file, at_line = self.unsure_at
        for f, (lines, _) in out.items():
            if f not in self.finished:
                cut = known.get(f, 0) + 1
                if f == at_file:
                    cut = min(cut, at_line)
                out[f] = ({n: v for n, v in lines.items() if n < cut}, cut)
        return out, True

    def sections(self, category, fname=None):
        return [s for r in self.reads for s in r.sections
                if s.category == category
                and (fname is None or s.fname == fname)]


def run(name, resolve, build, charmap='UTF-8'):
    """Emulate one `localedef -i NAME -f CHARMAP` run on the structure layer.

    `resolve(name)` gives the bytes of the file glibc would open for `name`
    (the -i argument or a `copy` string), or None when it would find none:
    the working directory, then I18NPATH, then LOCSRCDIR
    (locfile.c:57-105@2.39) are the caller's to search. Names are bytes, as
    glibc compares them (strcmp, localedef.c:617).

    Every file is read the way localedef reads it, in localedef's order and
    with the state localedef carries from file to file: a copy at the start
    of LC_COLLATE or LC_CTYPE is read at once, the other copies and translit
    includes after the -i file (localedef.c:266-282@2.39). There is no
    shortcut that reads a copied file on its own: one was tried in backlog
    15.1, and review found it wrong where glibc's state crosses from file
    to file.

    Only the UTF-8 charmap is modelled: a translated `copy` string is turned
    into a name the way that charmap turns it, and a name it cannot settle
    that way is reported unsettled. Returns a Run.
    """
    if charmap != 'UTF-8':
        raise ValueError('only the UTF-8 charmap is modelled')
    r = _Runner(resolve, build)
    main = _Entry(name)
    main.needed = set(CATEGORIES)
    r.entries.append(main)
    try:
        if r.locfile_read(main) != 0:
            raise _Stop(Fatal('missing', name))
        # localedef.c:266-282@2.39: read every queued file until each has
        # what it is needed for.
        while True:
            runp = None
            for e in r.entries:
                if not e.needed <= e.avail:
                    runp = e
                    break
            if runp is None:
                break
            if r.locfile_read(runp) != 0:
                raise _Stop(Fatal('missing', runp.name))
        r.finish_chains(main)
    except _Stop as stop:
        r.out.fatal = stop.fatal
    except GlibcHang as hang:
        r.out.fatal = Fatal('hang', None, detail=str(hang))
    except GlibcUndefined as undefined:
        # Nothing after this place can be read as glibc reads it.
        r.stop_being_sure(undefined.fname, undefined.line, undefined.why)
        return r.out
    if r.out.fatal is None:
        r.out.collate = main.data.get('LC_COLLATE')
        if r.out.collate is not None:
            _named_places(r.out.collate)
            _ranges_settled(r.out.collate, r.out)
    return r.out


def _c_name_match(arg, cur):
    """Whether undef and ifdef take the name `arg` for the defined name
    `cur`: strncmp (arg, cur, lenmb) == 0 && cur[lenmb] == '\\0', lenmb
    being len(arg) (ld-collate.c:3841-3843 and :3882-3884@2.39; :3819-3821
    and :3860-3862@2.34; :3816-3818 and :3857-3859@2.28). Both names keep
    their NUL bytes, and glibc stores `cur` with one NUL after it (:2607-2608
    and :3818-3819@2.39), so a NUL in either ends the comparison as C
    strings do (backlog 13.39). None where the test reads cur[lenmb] past
    that stored NUL: C leaves what it finds there undefined."""
    n, size = len(arg), len(cur)
    for i in range(n):
        c = cur[i] if i < size else 0
        if arg[i] != c:
            return False
        if c == 0:
            break
    if n < size:
        return cur[n] == 0
    if n == size:
        return True
    return None


# A `<U....>` name inside a weight string: insert_weights reads the names
# between `<` and `>` (ld-collate.c:800-829@2.34). Eight digits as well as
# four: a name of four is written as the one of eight it is looked up by.
_WEIGHT_NAME = re.compile(rb'<U([0-9A-Fa-f]{8}|[0-9A-Fa-f]{4})>')


def statement_characters(toks):
    """The code points glibc reads as characters in one LC_COLLATE
    statement's tokens, in reading order, or None when this reader cannot
    say which.

      - every <U....> name, and from 2.36 every raw UTF-8 character
        (tok_ucs4, lr_token);
      - in an untranslated string, a weight, each <U....> name
        (_WEIGHT_NAME); any other character of a weight string is the
        name of a symbol, one byte long (insert_weights, ld-collate.c:
        796-842@2.34), never a character;
      - in a translated string, the `from` of a collating-element
        (_lookahead), every character it holds: a byte below 0x80, an
        escaped one included, a <U....> name or, from 2.36, a UTF-8
        character. What a <U....> name gives the wide string is the code
        point written, before the charmap is asked (get_string,
        linereader.c:697-717@2.34); a code point the charmap lacks is then
        transliterated, which this reader does not model (charmaps come
        in backlog 15.6). Before 2.36 a byte above 0x7F is copied as it
        is, a byte and not a character (linereader.c:649-670@2.34;
        backlog 13.23): None. Under the UTF-8 charmap a symbolic name
        other than <U....> is not in the charmap (as copy_name).

    Every token of the logical line counts, a token after one that makes
    the handler drop the rest of the line included: this reader reads a
    statement as its first token and the rest of its line (the module's
    docstring), the wide side."""
    out = []
    for t in toks:
        if t.tok == 'ucs4':
            out.append(t.val)
        elif t.tok == 'string' and t.raw is not None:
            out.extend(int(h, 16) for h in _WEIGHT_NAME.findall(t.raw))
        elif t.tok == 'string':
            for kind, v in t.items:
                if kind in ('cp', 'ucp'):
                    out.append(v)
                elif kind == 'byte':
                    return None
    return out


# collate_read's state after each statement that changes it
# (ld-collate.c:2549-2558, 3091-3472@2.34), applied only when glibc
# accepts the statement (_refused).
_STATE_AFTER = {'tok_order_start': 1, 'tok_order_end': 2,
                'tok_reorder_after': 3, 'tok_reorder_end': 4,
                'tok_reorder_sections_after': 5,
                'tok_reorder_sections_end': 6}

# The states in which collate_read accepts each statement that changes the
# state, places something or defines an element; in any other it goes to
# err_label, which prints "syntax error" (not counted: lr_error,
# linereader.h:101-121@2.34), drops the rest of the line and keeps the
# state (ld-collate.c:3958-3960@2.34). The same at 2.28 and 2.39.
# collating-symbol, symbol-equivalence, coll_weight_max and section-symbol
# test the state too (:2833, :2979, :2697, :2720) and are not asked: this
# reader keeps no symbols, and they change no state and place nothing
# (what a symbol-equivalence leaves on its line is _Runner.statement's).

_ACCEPTED_IN = {
    'tok_collating_element': (0, 2),                 # :2767-2768
    'tok_order_start': (0, 1, 2),                    # :3100-3101
    'tok_order_end': (1,),                           # :3234-3235
    # reorder-after in state 0 only after an opening copy (:3273-3276)
    'tok_reorder_after': (0, 1, 2, 3),
    'tok_reorder_end': (3,),                         # :3373-3374
    'tok_reorder_sections_after': (1, 2, 3, 4),      # :3388-3409
    'tok_reorder_sections_end': (5,),                # :3468-3469
    'tok_undefined': (1,),                           # :3693-3694
    'ucs4': (1, 3),                                  # :3484-3488
    'bsymbol': (0, 1, 3, 5),                         # :3484-3485
    'ellipsis': (0, 1, 3),                           # :3731-3732
}


def _refused(toks, state, opening_copy):
    """Does collate_read send this LC_COLLATE statement to err_label in
    `state`? Only the statements of _ACCEPTED_IN are asked; a refused
    one changes the state, the cursor and the order not at all
    (backlog 15.3; the o cases of tests/reader_oracle, measured on el8,
    el9 and el10). A bsymbol in state 0 must also name a declared
    collating-symbol, or an element already placed that is not a
    character (:3518-3536); this reader does not keep the declarations
    and takes such a line as accepted. That is the cautious side: the
    line is an instruction either way (it does not open with a
    character), what it places is a symbol, never a character a range
    could end on, and the cursor after it is a place this reader is not
    sure of (_Runner.instruction)."""
    t = toks[0]
    if t.tok == 'kw':
        ok = _ACCEPTED_IN.get(t.kwtok)
        if ok is None:
            return False
        if t.kwtok == 'tok_reorder_after' and state == 0 \
                and not opening_copy:
            return True
        return state not in ok
    if t.tok in _RANGE_TOKS:
        return state not in _ACCEPTED_IN['ellipsis']
    ok = _ACCEPTED_IN.get(t.tok)
    return ok is not None and state not in ok


def _ranges_settled(coll, out):
    """For every range an entry closed, settled or not, count as an
    instruction every line of any file that places one of its end points
    or a character between them: such a line decides whether glibc fills
    the range and where, and it may be in another file than the range
    (round 5 of the 13.36 review). Then unsettle the range when one of
    its end points is named first by more than one statement of the run
    (glibc refuses a character placed twice, ld-collate.c:1056-1063@2.34,
    and handle_ellipsis then fills nothing, :1086-1090; a reorder block
    moves it, which this reader does not follow either), when no entry
    line put the cursor where it starts (the last line that moved it is
    not an entry between order_start and order_end that closes no range:
    _Runner.instruction), when it is not a `..`
    range (an absolute range, `...`, enters every character it fills in
    the sequence table, :1206-1223, and the order, :1241-1257, so a later
    line for one of them is refused, :1016 and :1056-1063, where this
    reader does not see it; a decimal one, `....`, reads the digits in
    base 10, :1322), or
    when an end point is not a character. Measured on glibc-2.12..2.39:
    the ranges of ko_KR and iso14651_t1 stay settled, and no line of any
    file is added."""
    placed, places = {}, []
    for i, (fname, line, toks) in enumerate(coll.statements):
        if i in coll.refused:
            continue
        if toks[0].tok in ('ucs4', 'bsymbol'):
            key = (toks[0].tok, toks[0].val)
            placed[key] = placed.get(key, 0) + 1
            places.append((key, (fname, line, max(t.line for t in toks))))
    for rng in coll.ranges:
        if rng['end'] is None:
            continue              # closed by no entry: unsettled already
        ends = (rng['start'], rng['end'])
        chars = None not in ends and all(k[0] == 'ucs4' for k in ends)
        lo, hi = sorted((ends[0][1], ends[1][1])) if chars else (1, 0)
        coll.instructions.extend(
            span for key, span in places
            if key in ends or (key[0] == 'ucs4' and lo <= key[1] <= hi))
        if ends[0] is None:
            out.unsettled.append((rng['file'], rng['line'],
                                  'a range that starts where no entry line '
                                  'put the cursor'))
        elif ends[1] is None or any(placed.get(k, 0) != 1 for k in ends):
            out.unsettled.append((rng['file'], rng['line'],
                                  'a range whose end points are not each '
                                  'placed once'))
        elif rng['kind'] != 'ellipsis2' or not chars:
            out.unsettled.append((rng['file'], rng['line'],
                                  'a range this reader does not fill'))


def _named_places(coll):
    """Add to coll.instructions the lines that place a character another
    statement names: as the anchor of a reorder-after, whose entries
    follow it (the cursor goes to that element, ld-collate.c:3269-3386
    @2.39), or as a weight, which is that element's rank (output_weight,
    ld-collate.c:1916@2.39). Moving such a line moves the rules that name
    it, which did not change (round 1 of the 13.36 review). Every line
    that places the character counts: a reorder block places it again
    (move_entry, ld-collate.c:3607-3633@2.39)."""
    place = {}
    for i, (fname, line, toks) in enumerate(coll.statements):
        if toks[0].tok == 'ucs4' and i not in coll.refused:
            place.setdefault(toks[0].val, []).append(
                (fname, line, max(t.line for t in toks)))
    for fname, line, toks in coll.statements:
        first = toks[0]
        if _kw(first, 'tok_reorder_after'):
            named = {t.val for t in toks[1:2] if t.tok == 'ucs4'}
        elif first.tok in ('ucs4', 'bsymbol') or first.tok in _RANGE_TOKS \
                or _kw(first, 'tok_undefined'):
            named = {t.val for t in toks[1:] if t.tok == 'ucs4'}
            for t in toks[1:]:
                if t.tok == 'string' and t.raw:
                    named |= {int(h, 16) for h in
                              _WEIGHT_NAME.findall(bytes(t.raw))}
            if first.tok == 'ucs4':
                named.discard(first.val)
        else:
            continue
        for cp in named:
            for span in place.get(cp, ()):
                if span[:2] != (fname, line):
                    coll.instructions.append(span)


# The ten categories whose <cat>_finish follows the `copy` chain
# (ld-time.c:143-153@2.39 and its nine siblings).
_QUEUED = ('LC_NUMERIC', 'LC_TIME', 'LC_MONETARY', 'LC_MESSAGES', 'LC_PAPER',
           'LC_NAME', 'LC_ADDRESS', 'LC_TELEPHONE', 'LC_MEASUREMENT',
           'LC_IDENTIFICATION')


def _repeats(lr, seen):
    """True when the line reader is back in a state it was in before:
    lr_token will give the same tokens again, forever.

    It can come back only where lr_getc returns EOF without reading
    anything, which it does at the end of the input and, before 2.36, at a
    0x1A byte (linereader.h:125-138@2.34); on an empty buffer too, but there
    lr_token gives `eof`, which both loops stop at first. lr_token
    then gives back what it read with lr_ungetn as if EOF had been a
    character -- after a number (linereader.c:288-301, the same at 2.28,
    2.34 and 2.39), after a `-`, after an escaped byte value -- and the
    next call reads the same again. Anywhere else every call moves on.
    Only a loop that never drops the rest of a line meets it: glibc's own
    (skip_category) never returns, and this reader's lookahead cannot
    settle the statement. `seen` holds the states one loop has met; the
    state is everything lr_token reads that these loops do not set."""
    if lr.pi < len(lr.phys) and not (lr.build.pre236 and 0x1a in lr.buf):
        return False
    state = (lr.pi, lr.idx, lr.bufact, bytes(lr.buf), lr.lineno)
    if state in seen:
        return True
    seen.add(state)
    return False


class _Runner(object):

    def __init__(self, resolve, build):
        self.resolve = resolve
        self.build = build
        self.entries = []
        self.out = Run()
        # `defined`: one static list for the whole run, every file and
        # section (ld-collate.c:276@2.39), most recent first.
        self.defined = []
        # For Run.named_lines: every LC_COLLATE struct of the run, and
        # the files whose LC_COLLATE read has ended.
        self.collates = []
        self.done = []

    def unsettled(self, lr, line, why):
        self.stop_being_sure(lr.fname, line, why)

    def stop_being_sure(self, fname, line, why):
        self.out.unsettled.append((fname, line, why))
        if self.out.unsure_at is None:
            self.out.unsure_at = (fname, line)
            self.out.finished = frozenset(self.done)
            for c in self.collates:
                c.unsure_from = len(c.statements)

    def new_collate(self):
        c = Collate()
        if self.out.unsure_at is not None:
            c.unsure_from = 0
        self.collates.append(c)
        return c

    # -- localedef.c ------------------------------------------------------

    def add_to_readlist(self, category, name, generate, copy_locale=None,
                        repertoire=None):
        """localedef.c:612-662@2.39. An entry keeps the repertoire it was
        created with; a later caller's is not looked at."""
        runp = None
        for e in self.entries:
            if e.name == name:
                runp = e
                break
        if runp is None:
            assert generate
            runp = _Entry(name, repertoire)
            self.entries.append(runp)
        if generate and category in runp.needed \
                and category not in runp.avail:
            raise _Stop(Fatal('circular', name))
        if copy_locale is not None:
            if runp.data.get(category) is not None:
                raise _Stop(Fatal('duplicate', name))
            runp.data[category] = copy_locale.data.get(category)
        runp.needed.add(category)
        return runp

    def load_locale(self, category, name, copy_locale=None,
                    repertoire=None):
        """localedef.c:685-701@2.39: queue and read now."""
        result = self.add_to_readlist(category, name, True, copy_locale,
                                      repertoire)
        if category not in result.avail and self.locfile_read(result) != 0:
            raise _Stop(Fatal('missing', name))
        return result

    def finish_chains(self, main):
        """<cat>_finish of the ten categories that copy through
        handle_copy: with no data of its own, the -i input follows its
        `copy` names from file to file until one has the category
        (ld-time.c:143-153@2.39, the same loop in the nine others), with no
        test for a cycle: a cycle never returns. Measured for LC_TIME on
        el8/el9/el10 (case c43)."""
        for cat in _QUEUED:
            if main.data.get(cat) is not None or cat not in main.copy_name:
                continue
            seen = {main.name}
            cur = main
            while True:
                n = cur.copy_name[cat]
                if n in seen:
                    raise _Stop(Fatal('hang', n, detail='a cycle of %s '
                                      'copies' % cat))
                seen.add(n)
                cur = None
                for x in self.entries:
                    if x.name == n:
                        cur = x
                        break
                if cur is None or cur.data.get(cat) is not None \
                        or cat not in cur.copy_name:
                    break

    # -- locfile.c --------------------------------------------------------

    def locfile_read(self, entry):
        """locfile.c:49-273@2.39. 1 when the file cannot be opened: no
        file, or an empty one (lr_create returns NULL, linereader.c:84-92)."""
        data = self.resolve(entry.name)
        if data is None:
            return 1
        lr = LR(data, entry.name.decode('utf-8', 'surrogateescape'),
                self.build)
        if lr.empty:
            return 1
        collate = 'LC_COLLATE' in entry.needed - entry.avail
        if collate:
            self.out.began.append(lr.fname)
        try:
            got = self._locfile_read(lr, entry)
            if collate:
                self.done.append(lr.fname)
            return got
        finally:
            # Also when the read ends in a _Stop, a GlibcHang or a
            # GlibcUndefined: what it met is the run's.
            self.out.met = self.out.met or lr.met
            self.out.words |= lr.words

    def _locfile_read(self, lr, entry):
        mask = entry.needed - entry.avail
        fr = FileRead(entry.name)
        self.out.reads.append(fr)
        not_here = set(CATEGORIES)
        while True:
            t = lr.token()
            if t.tok == 'eof':
                if lr.pi < len(lr.phys) or lr.idx < lr.bufact:
                    fr.cut = t.line
                break
            if t.tok == 'eol':
                continue
            kw = t.kwtok if t.tok == 'kw' else None
            if kw in ('tok_escape_char', 'tok_comment_char'):
                arg = lr.token()
                if arg.tok != 'ident':
                    lr.ignore_rest()              # "bad argument"
                    continue
                if len(arg.val) != 1:
                    lr.ignore_rest()
                    continue
                which = 'escape_char' if kw == 'tok_escape_char' \
                    else 'comment_char'
                setattr(lr, which, arg.val[0])
                fr.directives.append((t.line, which, arg.val[0]))
            elif kw == 'tok_repertoiremap':
                arg = lr.token()
                if arg.tok != 'ident':
                    lr.ignore_rest()
                    continue
                # It changes how symbolic names translate (locfile.c:150-168
                # @2.39), which this reader does not follow. No locale file
                # at glibc-2.12..2.39 has one.
                self.unsettled(lr, t.line, 'a repertoiremap directive')
            elif kw in _CAT_OF_TOK:
                cat = _CAT_OF_TOK[kw]
                sec = Section(cat, entry.name, t.line, cat in mask)
                fr.sections.append(sec)
                if cat == 'LC_COLLATE':
                    self.collate_read(lr, entry, sec)
                elif cat == 'LC_CTYPE':
                    self.ctype_read(lr, entry, sec)
                else:
                    self.other_read(lr, entry, sec)
                if cat in mask:
                    entry.avail.add(cat)
                not_here ^= {cat}
                continue
            else:
                # "syntax error: not inside a locale definition section"
                lr.ignore_rest()
                continue
            lr.ignore_rest()
        entry.avail |= not_here
        return 0

    def copy_name(self, lr, tok, line, report=True):
        """The name a `copy` string gives glibc, as bytes, or None for the
        NULL an illegal translated string leaves (get_string,
        linereader.c:772-1009@2.39). Under the UTF-8 charmap, a symbolic
        name other than <U....> is not in the charmap; a code point above
        U+007F is reported unsettled here and read as its UTF-8 bytes."""
        if tok.raw is not None:
            name = tok.raw
        else:
            out = bytearray()
            for kind, v in tok.items:
                if kind == 'illegal' or kind == 'name':
                    return None
                if kind == 'cp' or kind == 'byte':
                    out.append(v & 0xff)
                elif v < 0x80:          # 'ucp'
                    out.append(v)
                else:
                    if report:
                        self.unsettled(lr, line, 'a copy name holding a '
                                       'character above U+007F, which glibc '
                                       'looks up in the charmap')
                    try:
                        out += chr(v).encode('utf-8')
                    except (UnicodeEncodeError, ValueError):
                        return None
            name = bytes(out)
        # A C string: the name ends at its first NUL byte.
        return name.split(b'\0', 1)[0]

    def skip_category(self, lr, sec, end_tok):
        """The skip_category label of collate_read and ctype_read: read
        tokens to the first END or EOF. Its test is inverted, so on END it
        prints "premature end of file" and returns WITHOUT reading the word
        after END, which the top level then reads as a new statement
        (ld-collate.c:2624-2643@2.39, ld-ctype.c:2106-2125). On EOF it
        reads one more token, and unless that is EOF too drops the rest of
        its line: after an EOF lr_getc consumed (an 0xFF before 2.36) that
        is the line after it (cases s55, s57)."""
        seen = set()
        while True:
            t = lr.token()
            if t.tok == 'eof' or _kw(t, 'tok_end'):
                break
            if _repeats(lr, seen):
                raise GlibcHang('%s:%d: skip_category reads the same token '
                                'forever' % (lr.fname, t.line))
        if t.tok == 'eof':
            if lr.token().tok != 'eof':
                lr.ignore_rest()
            return
        sec.end = t.line
        return

    def statement(self, lr, t, sec, store):
        """One statement inside a section: its first token `t`, then the
        rest of its logical line, which this model drops as lr_ignore_rest
        does. The rest is tokenized first on a copy of the reader's state,
        to store the statement's tokens and to find the shapes on which the
        model can differ from the handler glibc runs:
          - a token after the first that matters to the structure if a
            handler leaves it to be read as a statement (END; in
            LC_COLLATE a copy or a conditional; in LC_CTYPE translit_start
            or translit_end). Several handlers do leave the rest of their
            line (numeric's decimal_point, an unknown word in LC_CTYPE,
            codepoint_collation ...);
          - tokens that run past the end of the logical line, as an
            unterminated string or <name does, swallowing its newline;
          - in LC_COLLATE, reorder-sections-after, whose handler can drop
            the lines after it, END included (ld-collate.c:3462-3474@2.39).
        """
        toks = [t]
        if lr.idx == lr.bufact and lr.bufact and lr.buf[-1] == 10:
            # The first token already read the newline: an `error` token
            # whose UTF-8 decoding took it, or an unterminated string or
            # <name. A syntax error then drops nothing more; any other
            # handler would read its arguments from the next line.
            if t.tok != 'error':
                self.unsettled(lr, t.line, 'a statement whose first token '
                               'runs past the end of its line')
            if store is not None:
                store.statements.append((lr.fname, t.line, toks))
                self.instruction(lr, store, t.line, toks, sec)
            return
        snap = lr.snapshot()
        # The lookahead reads only the line reader: its tokens, the reasons
        # and how far it got follow from the reader's state and nothing
        # else, and the state is put back after it. So it is kept per
        # state, every field LR has but the derived ones (fname is only in
        # the text of a GlibcHang, which is caught here, or of a
        # GlibcUndefined, which leaves before anything is kept; utf8, kw,
        # getc and phys follow from build, table and data). LR_KEY_FIELDS
        # names them, and a test fails when LR grows a field this key does
        # not cover.
        element = sec.category == 'LC_COLLATE' \
            and _kw(t, 'tok_collating_element')
        key = (lr.data, lr.table, snap, lr._cc, lr._ec, lr.translate_strings,
               lr.return_widestr, sec.category, element)
        kept = lr.build.lookahead.get(key)
        if kept is None:
            met, words = lr.met, lr.words
            lr.met, lr.words = False, set()
            try:
                kept = self._lookahead(lr, sec, element) \
                    + (lr.met, frozenset(lr.words))
            finally:
                # A GlibcUndefined leaves from here: what the file met
                # before the lookahead, and in it, is the run's all the same.
                lr.met, lr.words = met or lr.met, words | lr.words
            lr.build.lookahead[key] = kept
            lr.restore(snap)
        rest, reasons, scanned, met, words = kept
        # A kept lookahead read its bytes and words once, for every run
        # that uses it: each run counts them as its own.
        lr.met = lr.met or met
        lr.words |= words
        toks.extend(rest)
        for why in reasons:
            self.unsettled(lr, t.line, why)
        lr.ignore_rest()
        if scanned > lr.pi:
            self.unsettled(lr, t.line, 'a statement whose tokens run past '
                           'the end of its line')
        if sec.category == 'LC_COLLATE' \
                and _kw(t, 'tok_reorder_sections_after'):
            self.unsettled(lr, t.line, 'reorder-sections-after in '
                           'LC_COLLATE')
        if store is not None and t.tok == 'bsymbol' and rest \
                and store.state.get(lr.fname, 0) == 0:
            # Before any statement of this section read an argument, glibc
            # takes such a symbol as "bad symbol" and reads the rest of the
            # line as statements (arg == NULL, ld-collate.c:3496-3507
            # @2.34, :3516-3526@2.39); after one, as its weights, or drops
            # the line when the symbol is not declared or is a character
            # already placed (:3518-3536@2.34). This reader does not keep
            # which
            # (false-negative-reviewer, rounds 1 and 2 of 15.3 C1; cases
            # o40 to o42 of reader_oracle).
            self.unsettled(lr, t.line, 'a symbol before order_start with '
                           'more on its line')
        if store is not None and _kw(t, 'tok_symbol_equivalence') \
                and len(rest) > 2:
            # When its second name is no declared collating-symbol, or its
            # new name is defined already, symbol-equivalence `break`s
            # without dropping the rest of its line (sym_equiv_free,
            # ld-collate.c:3023-3038@2.34), which glibc then reads as
            # statements; otherwise it drops it (:3042). This reader keeps
            # no symbols (false-negative-reviewer, round 3 of 15.3 C1;
            # cases o47 to o52 of reader_oracle).
            self.unsettled(lr, t.line, 'symbol-equivalence with more on its '
                           'line')
        if not sec.content and sec.category == 'LC_COLLATE' and rest \
                and (_kw(t, 'tok_reorder_end')
                     or _kw(t, 'tok_reorder_sections_end')):
            # In a section read for another category these two `break`
            # without dropping the rest of their line (ld-collate.c:3370-3371,
            # :3465-3466@2.34), which glibc then reads as statements: an
            # ellipsis or codepoint_collation there writes into a struct
            # that is not this section's (false-negative-reviewer, round 2
            # of 15.3 C1; cases o43 to o45 of reader_oracle).
            self.unsettled(lr, t.line, '%s with more on its line, in an '
                           'LC_COLLATE read for another category' % t.val)
        if store is not None:
            store.statements.append((lr.fname, t.line, toks))
            self.instruction(lr, store, t.line, toks, sec)

    def instruction(self, lr, store, line, toks=None, sec=None):
        """Record lines `line` to where the reader stands as one of
        store's instructions (Collate.instructions), unless `toks` is a
        statement that opens with a character and holds no range; an
        entry next to a range is one whatever it names. Only an
        LC_COLLATE struct has them; any other store is left as it is.

        A range is settled in one shape only, the one glibc fills from
        two lines: opened between order_start and order_end, and closed
        there by the next entry of the same collate_read
        (ld-collate.c:3663-3674@2.34), with each end point placed once
        (_ranges_settled checks that after the run) and no statement of
        another file read while it is open; its start is the last entry
        placed between order_start and order_end that closes no range,
        where glibc's cursor stands. Every write of the cursor
        (ld-collate.c:679-687, 706-725, 1093-1488 and 3305-3355@2.34)
        leaves this reader either sure of the line the cursor then stands
        on or unsure, and a range that starts on a line it is unsure of
        is unsettled. Every other way glibc closes a range takes its end
        from no line or from the wrong one (order_end :3238,
        reorder-after :3265, a symbol in state 0 :3545, UNDEFINED :3696,
        END :3757, reorder-sections-after :3395), or never closes it;
        this reader follows none of them, so such a range is unsettled
        (rounds 3 and 4 of the 13.36 review). The state is collate_read's
        own (ld-collate.c:2558@2.34), kept per file in Collate.state."""
        if not isinstance(store, Collate):
            return
        span = (lr.fname, line, max(line, lr.lineno))
        if not toks or not _own_entry(toks):
            store.instructions.append(span)
        if not toks:
            return
        # Where the statement just appended ends: a string continued over
        # an escaped newline ends a line below the one its token starts on
        # (false-negative-reviewer, round 1 of 15.3 C2).
        store.last[len(store.statements) - 1] = span[2]
        f, first = lr.fname, toks[0]
        state = store.state.get(f, 0)
        if _refused(toks, state, sec is not None and sec.copy is not None):
            # err_label: the state, the cursor and the order stay as they
            # were. A keyword or a range is an instruction already (above):
            # edit it and glibc may accept it. A refused entry places
            # nothing, whatever character it names. `toks` is the statement
            # just appended (statement(), _collate_read).
            store.refused.add(len(store.statements) - 1)
            return
        # Another file's range still open: a statement read here can move
        # glibc's cursor, an entry or a range linked after the range's
        # placeholder (insert_weights, ld-collate.c:725@2.34) or a
        # reorder-after setting it to its anchor (:3305-3355), and
        # handle_ellipsis then unlinks the cursor as if it were the
        # placeholder (:1082; a copy reads its file into the same struct,
        # :2682), so that range is not filled from its lines (rounds 6 and
        # 7 of the 13.36 review). Every statement with tokens counts: an
        # entry, a range, UNDEFINED, a keyword; a copy or a conditional
        # line writes no cursor and comes here without tokens.
        for g in store.open_ranges:
            if g != f:
                self.unsettled(lr, line, 'a statement of another file read '
                               'inside an open range')
        if first.tok == 'kw' and first.kwtok in _STATE_AFTER:
            self.open_range_ends(lr, store, line)
            store.state[f] = _STATE_AFTER[first.kwtok]
            if first.kwtok == 'tok_reorder_after':
                # The cursor is now the anchor (ld-collate.c:3305-3355
                # @2.34), or NULL: on no entry line this reader names, so
                # a range that starts here is not settled (round 8).
                store.before = (store.before + [(span, None)])[-2:]
            return
        if not _in_order(toks):
            return
        key = (first.tok, first.val) if first.tok in ('ucs4', 'bsymbol') \
            else None
        if first.tok in _RANGE_TOKS:
            store.instructions.extend(s for s, _ in store.before)
            if state != 1 or f in store.open_ranges:
                self.unsettled(lr, line, 'a range outside order_start and '
                               'order_end, or after another')
            rng = {'file': f, 'line': line, 'end': None, 'kind': first.tok,
                   'start': store.before[-1][1] if store.before else None}
            store.ranges.append(rng)
            store.open_ranges[f] = rng
            return
        closes = False
        if store.open_ranges:
            store.instructions.append(span)
            rng = store.open_ranges.pop(f, None)
            if rng is not None:
                # Opened in state 1, which every statement that changes
                # the state closes first: the entry is in state 1 too.
                closes = True
                rng['end'] = key
                if key is None:
                    self.unsettled(lr, line, 'a range closed by UNDEFINED')
        if state in (1, 3) or (state == 0 and first.tok == 'bsymbol'):
            # Only an entry between order_start and order_end that closes
            # no range is a place this reader is sure glibc's cursor
            # stands on. In a reorder block the cursor can be NULL and the
            # entry dropped (ld-collate.c:3566-3570@2.34); after a range
            # the cursor is its last entry only from 2.34 (:1488, Bug
            # 22668), on its last filled character before (round 9).
            sure = state == 1 and not closes
            store.before = (store.before + [(span, key if sure
                                             else None)])[-2:]

    def open_range_ends(self, lr, store, line):
        """A range of this file still waiting for an entry is closed
        here, by something other than an entry: unsettled (instruction)."""
        if lr.fname in store.open_ranges:
            self.unsettled(lr, line, 'a range with no entry after it')
            del store.open_ranges[lr.fname]

    @staticmethod
    def joined(lr, sec, store):
        """Every physical line of `sec` that lr_next joins to the next,
        the escape character before its newline (linereader.c:162-171
        @2.39; the first line, lr_create, tests a literal backslash, :95),
        is one of store's instructions with the line it joins: edit one
        and an unchanged line is read as part of another, or on its own.
        comment_char and escape_char are read at the top level only, so
        one escape character holds for the whole section. Kept per file
        and escape on the Build, like its lines."""
        ec = lr._ec & 0xff
        key = (lr.data, ec)
        lines = lr.build.joins_of.get(key)
        if lines is None:
            lines = [i + 1 for i, b in enumerate(lr.phys)
                     if len(b) > 1 and b[-1] == 10
                     and b[-2] == (0x5c if i == 0 else ec)]
            lr.build.joins_of[key] = lines
        end = sec.end if sec.end is not None else len(lr.phys)
        for n in lines[bisect.bisect_left(lines, sec.start):]:
            if n > end:
                break
            store.instructions.append((lr.fname, n, n + 1))

    @staticmethod
    def _lookahead(lr, sec, element=False):
        """(tokens after the first, reasons, lr.pi reached) of the rest of a
        statement's logical line; leaves lr moved, statement() restores it.

        `element`: the statement opens with collating-element in
        LC_COLLATE, whose handler reads the token after `<name> from`
        with translate_strings and return_widestr set, and sets both back
        to 0 after it (ld-collate.c:2786-2793@2.34, the same at 2.28 and
        2.39): that string is read translated, as get_string's translating
        branch reads it (`.items`). In a state that refuses the statement,
        or a section read for another category, glibc drops the line
        without reading it, and its tokens change nothing (Collate.refused)."""
        rest, reasons = [], []
        loud = _TAIL_KEYWORDS.get(sec.category, _TAIL_KEYWORDS[None])
        seen = set()
        try:
            while True:
                if element and len(rest) == 2 and rest[0].tok == 'bsymbol' \
                        and _kw(rest[1], 'tok_from'):
                    lr.translate_strings = lr.return_widestr = 1
                    try:
                        x = lr.token()
                    finally:
                        lr.translate_strings = lr.return_widestr = 0
                else:
                    x = lr.token()
                if x.tok not in ('eol', 'eof') and _repeats(lr, seen):
                    reasons.append('a statement whose tokens the line '
                                   'reader gives again forever')
                    break
                if x.tok == 'eof' and (
                        lr.pi < len(lr.phys) or lr.idx < lr.bufact):
                    # A byte lr_getc reads as EOF (0x1A or 0xFF before
                    # 2.36) with the file going on after it: a handler that
                    # reads tokens stops there, one that calls
                    # lr_ignore_rest skips it (measured, case b19), and
                    # after an 0xFF, which lr_getc consumes, the statement
                    # loop reads on from the byte after it (cases s62-s66;
                    # false-negative-reviewer, round 3 of 15.2 B2).
                    reasons.append('a byte read as the end of the file '
                                   'inside a statement')
                if x.tok in ('eol', 'eof'):
                    break
                rest.append(x)
                if x.tok == 'kw' and x.kwtok in loud:
                    reasons.append('`%s` after the first token of a '
                                   'statement in %s' % (x.val, sec.category))
        except GlibcHang:
            reasons.append('a statement whose tokens do not end before the '
                           'end of the file')
        return tuple(rest), tuple(reasons), lr.pi

    def end_of_section(self, lr, t, sec):
        """`END` in statement position: the next token, then return
        (ld-collate.c:3760-3800, ld-numeric.c:316-327@2.39). Any word ends
        the section; a wrong one only draws a message. True when the
        section ended.

        An EOF in that place is `break`, not `return` (ld-collate.c:
        3772-3773@2.34, ld-numeric.c:320-321, ld-ctype.c:2823-2824, the
        same at 2.28 and 2.39): the statement loop goes on with the next
        token. When lr_getc consumed the EOF -- an 0xFF through a signed
        char before 2.36 -- that token is the line after it, and the
        section reads on to the next END; False then (cases s53, s54,
        s56). An EOF that is not consumed comes back and ends the loop."""
        arg = lr.token()
        if arg.tok == 'eof':
            if not lr.stuck():
                return False
            sec.end = t.line
            return True
        sec.end = t.line
        lr.ignore_rest()
        return True

    # -- ld-collate.c -----------------------------------------------------

    def collate_read(self, lr, entry, sec):
        """ld-collate.c:2554-3993@2.39 (2535 at 2.28, 2538 at 2.34),
        then, for the struct it was read into, the section's joined lines
        (joined), a range it leaves open (open_range_ends), and its own
        state, which the next collate_read starts again."""
        self._collate_read(lr, entry, sec)
        store = entry.data.get('LC_COLLATE') if sec.content else None
        if store is not None:
            self.joined(lr, sec, store)
            self.open_range_ends(lr, store, sec.end if sec.end is not None
                                 else len(lr.phys))
            store.state.pop(lr.fname, None)

    def collate_end(self, lr, line, store):
        """The checks of the END case, which run at every END in
        statement position, before its argument is read: also at an END
        whose argument is an EOF lr_getc consumed (an 0xFF before 2.36),
        after which the section reads on, and at one a skipped conditional
        branch meets (seen_end, ld-collate.c:3740-3770, :3891-3892@2.34;
        false-negative-reviewer, round 1 of 15.3 C1). In state 1 glibc
        closes a range still open there, with no entry (handle_ellipsis with
        NULL, :3751-3762); in states 0, 3 and 4 it leaves the range pending,
        a shape the reader holds unsettled already (instruction). Either
        way this reader closes it as unsettled. In a reorder block,
        "missing `reorder-end'" (or reorder-sections-end) is a counted
        error, and localedef without -c
        writes nothing (:3764-3769; cases o16, o22, o31 and o37 of
        reader_oracle, rc 4 on el8, el9 and el10)."""
        if store is None:
            return
        self.open_range_ends(lr, store, line)
        if store.state.get(lr.fname, 0) in (3, 5):
            self.unsettled(lr, line, 'END inside a reorder block, an '
                           'error that stops localedef without -c')

    def _collate_read(self, lr, entry, sec):
        ignore = not sec.content
        if not ignore and entry.repertoire is not None:
            # glibc reads the map named first (ld-collate.c:2577-2578
            # @2.39). A name it defines is refused as a collating-element
            # or collating-symbol ("already defined in repertoire",
            # check_duplicate, :433-438), and a symbol the charmap lacks is
            # looked up in it (:968): measured, case p001. This reader
            # reads no repertoire map, and the one named can be on the
            # machine's own path (repertoire.c:104-115). The entry can
            # have it from a copy, not only from an include: p017, p019.
            self.unsettled(lr, sec.start, 'an LC_COLLATE read with the '
                           'repertoire map a translit include named')
        lr.ignore_rest()
        # The lines before the first statement read into the struct, which
        # does not exist yet: the defines and the opening copy.
        ahead = []
        while True:
            t = lr.token()
            while t.tok == 'eol':
                t = lr.token()
            if not _kw(t, 'tok_define'):
                break
            if not ignore:
                arg = lr.token()
                if arg.tok == 'ident':
                    self.defined.insert(0, arg.val)
            lr.ignore_rest()
            ahead.append((lr.fname, t.line, lr.lineno))
        copy_locale = None
        if _kw(t, 'tok_copy'):
            s = lr.token()
            if s.tok != 'string':
                lr.ignore_rest()
                self.skip_category(lr, sec, 'tok_lc_collate')
                return
            name = self.copy_name(lr, s, t.line, not ignore)
            sec.copy = (name, t.line)
            if not ignore:
                if name is None:
                    raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                                      'load_locale with a NULL name'))
                # The reading file's map goes to the entry this creates
                # (ld-collate.c:2648-2649@2.39), as at :2702 and in the
                # copies of LC_CTYPE (ld-ctype.c:2130-2131) and of the
                # other categories (handle_copy, locfile.c:973-974).
                copy_locale = self.load_locale('LC_COLLATE', name,
                                               repertoire=entry.repertoire)
                if 'LC_COLLATE' not in copy_locale.avail \
                        and self.locfile_read(copy_locale) != 0:
                    self.skip_category(lr, sec, 'tok_lc_collate')
                    return
                if copy_locale.data.get('LC_COLLATE') is None:
                    return
            lr.ignore_rest()
            ahead.append((lr.fname, t.line, lr.lineno))
            t = lr.token()
        # collate_startup (ld-collate.c:1496-1528@2.39).
        if not ignore and entry.data.get('LC_COLLATE') is None:
            entry.data['LC_COLLATE'] = self.new_collate() \
                if copy_locale is None \
                else copy_locale.data.get('LC_COLLATE')
        lr.translate_strings = 0
        lr.return_widestr = 0
        store = None if ignore else entry.data.get('LC_COLLATE')
        if store is not None:
            store.instructions.extend(ahead)
        while True:
            if t.tok == 'eof':
                return
            if t.tok == 'eol':
                t = lr.token()
                continue
            if _kw(t, 'tok_end'):
                self.collate_end(lr, t.line, store)
                if self.end_of_section(lr, t, sec):
                    return
                t = lr.token()
                continue
            if _kw(t, 'tok_copy'):
                s = lr.token()
                if s.tok != 'string':
                    lr.ignore_rest()                  # err_label
                else:
                    name = self.copy_name(lr, s, t.line, not ignore)
                    sec.mid_copies.append((name, t.line))
                    if not ignore:
                        if name is None:
                            raise _Stop(Fatal('crash', None,
                                              (lr.fname, t.line),
                                              'load_locale with a NULL name'))
                        self.load_locale('LC_COLLATE', name, entry,
                                         entry.repertoire)
                    lr.ignore_rest()
                self.instruction(lr, store, t.line)
                t = lr.token()
                continue
            if ignore and (t.tok in ('ellipsis2', 'ellipsis3', 'ellipsis4')
                           or _kw(t, 'tok_codepoint_collation')):
                # The only LC_COLLATE handlers with no ignore_content test
                # (ld-collate.c:2691-2693, :3743-3756@2.39; the ellipses the
                # same at 2.28 and 2.34): they write into the struct, which
                # is NULL in a file read for another category -- a crash,
                # measured on el8/el9/el10 for the ellipsis and on el9/el10
                # for codepoint_collation (cases s33, s34) -- or the
                # copier's.
                if entry.data.get('LC_COLLATE') is None:
                    raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                                      '%s in an LC_COLLATE read for another '
                                      'category' % (t.val or t.tok)))
                else:
                    self.unsettled(lr, t.line, 'a statement written into '
                                   'a struct read for another category')
            if _kw(t, 'tok_codepoint_collation'):
                # `break` with nothing more read: the next token on the line
                # is a new statement (ld-collate.c:2691-2693@2.39).
                if store is not None:
                    store.statements.append((lr.fname, t.line, [t]))
                    self.instruction(lr, store, t.line, [t])
                t = lr.token()
                continue
            if t.tok == 'kw' and t.kwtok in _CONDITIONALS:
                if ignore:
                    lr.ignore_rest()
                    t = lr.token()
                    continue
                t = self.conditional(lr, t, sec, store)
                if t is None:
                    return
                continue
            if store is not None:
                if _kw(t, 'tok_order_start'):
                    store.order_started = True
                elif t.tok in ('ellipsis2', 'ellipsis3', 'ellipsis4') \
                        and not store.order_started:
                    # localedef crashes on a range before any order_start
                    # (measured on el8/el9/el10, case s41); this reader
                    # does not model why.
                    self.unsettled(lr, t.line, 'an ellipsis before '
                                   'order_start')
            self.statement(lr, t, sec, store)
            t = lr.token()

    def taken_for(self, lr, line, name, first):
        """For each name in `defined`, most recent first, whether glibc's
        test takes `name` for it (_c_name_match). With `first`, as the
        ifdef loop does, nothing after the first name taken is tested
        (:3880-3887@2.39). A test whose answer C leaves undefined makes
        the place unsettled and counts as not taken."""
        out = []
        for cur in self.defined:
            if out and first and out[-1]:
                out.append(False)
                continue
            m = _c_name_match(name, cur)
            if m is None:
                self.unsettled(lr, line, 'a name compared past the end of '
                               'a defined one, where glibc reads outside it')
            out.append(bool(m))
        return out

    def conditional(self, lr, t, sec, coll):
        """define, undef, ifdef, ifndef, elifdef, elifndef, else and endif
        in an LC_COLLATE read for its content (ld-collate.c:3804-3978@2.39,
        the same at 2.28 and 2.34). ifndef is not in the keyword table
        (locfile-kw.gperf:75-81), so its branch runs only after elifndef.
        Returns the next token to dispatch, or None when the section ended
        (on END or at the end of the file)."""
        kw = t.kwtok
        if kw in ('tok_define', 'tok_undef'):
            arg = lr.token()
            if arg.tok == 'ident':
                if kw == 'tok_define':
                    self.defined.insert(0, arg.val)
                else:
                    # "Remove _all_ occurrences of the symbol" (:3837)
                    self.defined = [
                        d for d, taken in zip(self.defined, self.taken_for(
                            lr, t.line, arg.val, False)) if not taken]
            coll.statements.append((lr.fname, t.line, [t, arg]))
            lr.ignore_rest()
            self.instruction(lr, coll, t.line)
            return lr.token()
        if kw == 'tok_endif':
            lr.ignore_rest()
            self.instruction(lr, coll, t.line)
            coll.else_action = 'none'
            return lr.token()
        if kw in ('tok_elifdef', 'tok_elifndef', 'tok_else'):
            lr.ignore_rest()
            self.instruction(lr, coll, t.line)
            if coll.else_action == 'ignore':
                stop = self.skip_to(lr, True, coll)
                return self.after_skip(lr, stop, sec, coll)
            if coll.else_action == 'seen':
                # assert (collate->else_action == else_none) fails
                # (ld-collate.c:3953@2.39): SIGABRT on el8/el9/el10, where
                # the else is in a file copied inside an else branch, which
                # shares the copier's struct (case c48).
                raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                                  'a second else: assertion failure'))
            return lr.token()
        # ifdef / ifndef, and found_ifdef after an elifdef or elifndef.
        at = t.line
        while True:
            arg = lr.token()
            if arg.tok != 'ident':
                lr.ignore_rest()                    # err_label
                self.instruction(lr, coll, at)
                return lr.token()
            lr.ignore_rest()
            self.instruction(lr, coll, at)
            if coll.else_action != 'none':
                # nested: not supported
                stop = self.skip_to(lr, True, coll)
                return self.after_skip(lr, stop, sec, coll)
            hit = any(self.taken_for(lr, at, arg.val, True))
            if (kw == 'tok_ifdef') == hit:
                coll.else_action = 'ignore'
                return lr.token()
            stop = self.skip_to(lr, False, coll)
            if _kw(stop, 'tok_else'):
                coll.else_action = 'seen'
                return lr.token()
            at = stop.line
            if _kw(stop, 'tok_elifdef'):
                kw = 'tok_ifdef'
                continue
            if _kw(stop, 'tok_elifndef'):
                kw = 'tok_ifndef'
                continue
            return self.after_skip(lr, stop, sec, coll)

    def after_skip(self, lr, stop, sec, coll):
        """What follows a skip_to that met the end of the file or END
        (seen_eof, seen_end); any other stop reads on."""
        if stop.tok == 'eof':
            return None
        if _kw(stop, 'tok_end'):
            # seen_end is the END case itself (ld-collate.c:3741@2.34).
            self.collate_end(lr, stop.line, coll)
            if self.end_of_section(lr, stop, sec):
                return None
        return lr.token()

    def skip_to(self, lr, to_endif, coll=None):
        """ld-collate.c:2514-2552@2.39: the first token of each logical
        line, until END, the end of the file, endif, or (when not looking
        for endif) else, elifdef or elifndef. Returns that token. Every
        conditional it meets is one of coll's instructions: where the skip
        stops decides which lines are read."""
        while True:
            t = lr.token()
            if t.tok == 'eof' or _kw(t, 'tok_end'):
                return t
            if t.tok == 'kw' and t.kwtok in _CONDITIONALS:
                self.instruction(lr, coll, t.line)
            if t.tok == 'kw' and t.kwtok in ('tok_ifdef', 'tok_ifndef'):
                inner = self.skip_to(lr, True, coll)
                if inner.tok == 'eof' or _kw(inner, 'tok_end'):
                    return inner
            elif _kw(t, 'tok_endif') or (not to_endif
                                          and _kw(t, 'tok_else')):
                lr.ignore_rest()
                self.instruction(lr, coll, t.line)
                return t
            elif not to_endif and t.tok == 'kw' and \
                    t.kwtok in ('tok_elifdef', 'tok_elifndef'):
                return t
            lr.ignore_rest()

    # -- ld-ctype.c -------------------------------------------------------

    def ctype_read(self, lr, entry, sec):
        """ld-ctype.c:2064-2150@2.39: the copy prologue, then the body."""
        ignore = not sec.content
        lr.ignore_rest()
        t = lr.token()
        while t.tok == 'eol':
            t = lr.token()
        copy_locale = None
        if _kw(t, 'tok_copy'):
            s = lr.token()
            if s.tok != 'string':
                lr.ignore_rest()
                self.skip_category(lr, sec, 'tok_lc_ctype')
                return
            name = self.copy_name(lr, s, t.line, not ignore)
            sec.copy = (name, t.line)
            if not ignore:
                if name is None:
                    raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                                      'load_locale with a NULL name'))
                copy_locale = self.load_locale('LC_CTYPE', name,
                                               repertoire=entry.repertoire)
                if 'LC_CTYPE' not in copy_locale.avail \
                        and self.locfile_read(copy_locale) != 0:
                    self.skip_category(lr, sec, 'tok_lc_ctype')
                    return
                if copy_locale.data.get('LC_CTYPE') is None:
                    return
            lr.ignore_rest()
            t = lr.token()
        # ctype_startup (ld-ctype.c:289-330@2.39) leaves the string mode.
        if not ignore and entry.data.get('LC_CTYPE') is None:
            entry.data['LC_CTYPE'] = object() if copy_locale is None \
                else copy_locale.data.get('LC_CTYPE')
        self.body(lr, t, sec, entry)

    # -- the ten categories that copy through handle_copy -----------------

    def other_read(self, lr, entry, sec):
        """numeric_read and its nine siblings (ld-numeric.c:149-341@2.39):
        a `copy` first goes to handle_copy (locfile.c:955-1010), which
        queues the file for later; otherwise <cat>_startup sets
        translated strings, and the body follows."""
        ignore = not sec.content
        lr.ignore_rest()
        t = lr.token()
        while t.tok == 'eol':
            t = lr.token()
        if _kw(t, 'tok_copy'):
            self.handle_copy(lr, entry, sec, ignore)
            return
        lr.translate_strings = 1
        lr.return_widestr = 1 if sec.category == 'LC_TIME' else 0
        if not ignore:
            entry.data[sec.category] = object()
        self.body(lr, t, sec, entry)

    def handle_copy(self, lr, entry, sec, ignore):
        s = lr.token()
        line = s.line
        if s.tok == 'string':
            name = self.copy_name(lr, s, line, not ignore)
            sec.copy = (name, line)
            if not ignore and name is not None:
                self.add_to_readlist(sec.category, name, True,
                                     repertoire=entry.repertoire)
                entry.copy_name[sec.category] = name
        lr.ignore_rest()
        while True:
            t = lr.token()
            if t.tok == 'eof' or _kw(t, 'tok_end'):
                break
            lr.ignore_rest()      # "no other keyword shall be specified"
        if t.tok == 'eof':
            return
        # handle_copy reads the word after END and drops the rest of the
        # line whatever it is, an EOF included (locfile.c:997-1010@2.34):
        # no statement loop to go back to.
        sec.end = t.line
        lr.token()
        lr.ignore_rest()

    def body(self, lr, t, sec, entry):
        """The statement loop of every reader but collate_read."""
        ignore = not sec.content
        while True:
            if t.tok == 'eof':
                return
            if t.tok == 'eol':
                t = lr.token()
                continue
            if _kw(t, 'tok_end'):
                if self.end_of_section(lr, t, sec):
                    return
                t = lr.token()
                continue
            if sec.category == 'LC_TIME' and t.tok == 'kw' \
                    and t.kwtok in _TIME_ARRAYS and not ignore:
                self.time_array(lr, t, _TIME_ARRAYS[t.kwtok])
            elif sec.category == 'LC_CTYPE' \
                    and _kw(t, 'tok_translit_start'):
                t = self.translit(lr, t, sec, entry)
                continue
            elif sec.category == 'LC_CTYPE' and ignore and t.tok == 'kw' \
                    and t.kwtok in ('tok_charclass', 'tok_charconv'):
                # No ignore_content test (ld-ctype.c:2177-2203@2.39, the
                # same at 2.28 and 2.34): when a name or a string follows,
                # ctype_class_new or ctype_map_new writes into the struct,
                # NULL in a file read for another category. Measured on
                # el8/el9/el10: a segmentation fault for both, and a
                # build when nothing or a number follows (cases s32,
                # s36-s40).
                snap = lr.snapshot()
                try:
                    nxt = lr.token()
                finally:
                    lr.restore(snap)
                if nxt.tok in ('ident', 'string'):
                    if entry.data.get('LC_CTYPE') is None:
                        raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                                          '%s in an LC_CTYPE read for '
                                          'another category' % t.val))
                    self.unsettled(lr, t.line, 'a statement written into a '
                                   'struct read for another category')
                self.statement(lr, t, sec, None)
            else:
                self.statement(lr, t, sec, None)
            t = lr.token()

    def time_array(self, lr, t, most):
        """STRARR_ELEM of time_read (ld-time.c:723-799@2.39, the same at
        2.28 and 2.34): up to `most` strings separated by `;`. A string
        followed by the end of its line does not end the list: the next
        line's first token is the next value, and one that is not a string
        -- END included -- is a syntax error that drops that line."""
        now = None
        cnt = 0
        while cnt < most:
            now = lr.token()
            if now.tok == 'eol':
                return
            if now.tok != 'string':
                lr.ignore_rest()                    # err_label
                return
            now = lr.token()
            if now.tok not in ('semicolon', 'eol'):
                break
            cnt += 1
        if now.tok == 'eol':
            return
        if now.tok == 'semicolon':
            now = lr.token()
            if now.tok == 'eol':
                return
        lr.ignore_rest()

    def translit(self, lr, t, sec, entry):
        """translit_start to translit_end (ld-ctype.c:2625-2786@2.39, the
        same at 2.28 and 2.34). END does not end the block, or the
        section, inside it. Returns the token after the block's end.

        Read for its content, an `include` queues its file for LC_CTYPE and
        the other entries are read as lines. An entry whose first token is
        not a value this reader can settle -- anything but a code point or
        a string with a value -- is reported unsettled: read_translit_entry
        then either leaves the rest of its line to be read as entries or,
        after a syntax error, reads on into the next line
        (ld-ctype.c:1855-1934).
        Read for another category, only the first token of each logical
        line is looked at (:2628-2643)."""
        if not sec.content:
            while True:
                lr.ignore_rest()
                now = lr.token()
                if now.tok == 'eof' or _kw(now, 'tok_translit_end'):
                    break
            # Both loops end at EOF too and `break`; the statement loop
            # then takes the next token (ld-ctype.c:2637-2643, :2782-2787,
            # :2841@2.34), which after an 0xFF lr_getc consumed is what
            # follows it, and the same EOF again otherwise (cases s67,
            # s68; false-negative-reviewer, round 3 of 15.2 B2).
            return lr.token()
        lr.ignore_rest()
        lr.translate_strings = 1
        lr.return_widestr = 1
        while True:
            now = lr.token()
            if now.tok == 'eof' or _kw(now, 'tok_translit_end'):
                break
            if now.tok == 'eol':
                continue
            if _kw(now, 'tok_include'):
                self.translit_include(lr, now, entry)
                continue
            if now.tok == 'kw' and now.kwtok in ('tok_default_missing',
                                                 'tok_translit_ignore'):
                self.statement(lr, now, sec, None)
                continue
            if now.tok not in ('ucs4', 'string'):
                self.unsettled(lr, now.line, 'a translit entry starting '
                               'with %s' % now.tok)
            elif now.tok == 'string' and (
                    not now.items or now.items[0][1] == 0
                    or any(k in ('illegal', 'name') or
                           (k == 'ucp' and v >= 0x80)
                           for k, v in now.items)):
                # read_widestring returns NULL for a string with no wide
                # value, or one that starts with U+0000, and
                # read_translit_entry then returns with the rest of the
                # line unread (ld-ctype.c:1836-1840, :1866@2.39): an
                # `include` there is queued (measured, el8/el9/el10). A
                # character above U+007F can be missing from the charmap,
                # which makes the string illegal; this reader does not read
                # charmaps yet.
                self.unsettled(lr, now.line, 'a translit entry starting '
                               'with a string glibc may read as having no '
                               'value')
            self.statement(lr, now, sec, None)
        lr.return_widestr = 0
        return lr.token()

    def translit_include(self, lr, t, entry):
        """`include "name";"repertoire"` (ld-ctype.c:2663-2716@2.39): the
        file is queued for LC_CTYPE and read after the main file."""
        name = lr.token()
        if name.tok not in ('string', 'ident'):
            lr.ignore_rest()
            return
        semi = lr.token()
        if semi.tok != 'semicolon':
            lr.ignore_rest()
            return
        rep_ = lr.token()
        if rep_.tok not in ('string', 'ident') or (
                rep_.tok == 'string' and
                self.copy_name(lr, rep_, t.line, False) is None):
            lr.ignore_rest()
            return
        if rep_.tok == 'string' and any(k == 'ucp' and v >= 0x80
                                        for k, v in rep_.items or ()):
            # A character the charmap lacks makes the string NULL and the
            # line a syntax error (measured, case c50); charmaps are read
            # in a later step of block 15.
            self.unsettled(lr, t.line, 'a translit include whose '
                           'repertoire name holds a character above U+007F')
        if name.tok == 'ident':
            # A word keeps its NUL bytes in the token, and glibc uses it as
            # a C string (locale_name = now->val.str.startmb,
            # ld-ctype.c:2680@2.39), so the name ends at its first NUL, as
            # in copy_name (backlog 13.44; cases c52, c54).
            target = name.val.split(b'\0', 1)[0]
        else:
            target = self.copy_name(lr, name, t.line)
        lr.ignore_rest()
        if target is None:
            # add_to_readlist strcmps a NULL name: a crash, measured (y009).
            raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                              'translit include with a NULL name'))
        # The repertoire name is a C string too, and an empty one is none
        # (ld-ctype.c:2692-2695@2.39). An include passes its own, not the
        # reading file's: the entry it creates keeps it (_Entry).
        rep = rep_.val if rep_.tok == 'ident' \
            else self.copy_name(lr, rep_, t.line, False)
        rep = rep.split(b'\0', 1)[0] or None
        self.add_to_readlist('LC_CTYPE', target, True, repertoire=rep)
