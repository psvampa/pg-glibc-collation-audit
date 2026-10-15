"""One reader of glibc's locale sources, following localedef step by step.

The tool reads glibc's locale files to decide what changed between two
versions. That reading used to be written by hand in several modules, each a
little differently from the others and from glibc; every fix corrected one copy
and the next review found its twin (backlog block 15). This module is the one
reader the steps are moved onto, one PR at a time: steps 1 to 4 and 9/10
read through it (glibc_tree.py); steps 6 to 8 not yet, nor step 2's
lc_collate_unread check and its check of character sets and
transliterations (collation_keys).

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

Inside a section, every statement is read, for now, as its first token plus
the rest of its logical line, which glibc drops with lr_ignore_rest. That is
what most statement handlers end with, but not all: some leave the rest of
the line to be read as further statements, and some read past the end of the
line. Where this reader sees a shape on which that model can differ from the
handler glibc runs, it says so in Run.unsettled rather than answer as if it
had read it. The statement grammars come in later steps of block 15.

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
getdelim reuses the buffer, and no byte of the line stands for it. Keyword
lookup is exact, where
gperf's strncmp could in theory match an identifier holding a NUL byte. A
translated string holding a character the charmap lacks makes get_string
try to transliterate it, which can load the file's own LC_CTYPE in the
middle of the run (linereader.c:640-660@2.39). Charmaps are read in a later
step of block 15; until then, where the reader sees an answer depend on
whether the charmap has a character, it reports that place unsettled.

Written for Python 3.6, the oldest the fixtures run, with the standard
library only.
"""
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
    """glibc reads outside its buffer for this input: get_ident takes
    lr->buf[lr->idx - 1] with idx 0 when lr_ungetn could not step back over
    a `-` (linereader.c:329 and :578@2.39), as at the start of a file or
    of a line continued from one ending in `-` and the escape. What it
    reads is undefined, and can decide an answer (two `define`/`ifdef`
    names glibc reads alike), so run() reports the place unsettled."""

    def __init__(self, fname, line):
        Exception.__init__(self, '%s:%d' % (fname, line))
        self.fname = fname
        self.line = line


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


class Fatal(object):
    """A localedef run that ends without output: `kind` is 'missing' (a file
    it cannot open, exit 4), 'circular' or 'duplicate' (exit 5), 'hang'
    (localedef never returns: GlibcHang, or a cycle of `copy` names), or
    'crash' (a NULL dereference or a failed assertion). Each is measured on
    the RHEL 8, 9 and 10 builds by a case in tests/reader_oracle/, except
    the translit include with a NULL name, read from the source, and
    get_symname's GlibcHang, whose loop was measured against glibc's
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
    Each statement is (file name, line, tokens of its logical line)."""

    def __init__(self):
        self.statements = []
        self.else_action = 'none'     # else_none / else_ignore / else_seen
        self.order_started = False


class Section(object):
    """One category section as glibc read it.

    start      line of the category keyword
    end        line of the `END` that closed it, or None when the file ended
    end_word   the token read after that END (None at EOF)
    content    False when the file was read for other categories only
               (ignore_content), so nothing of it was stored
    copy       (name, line) of a `copy` that opens the section
    copy_kind  'load' (LC_CTYPE, LC_COLLATE), 'queue' (the other ten), or
               None
    skipped    the opening `copy` had no string: glibc skipped to the
               first END token and left the word after it to the top level
               (ld-collate.c:2621-2643@2.39, ld-ctype.c:2103-2125)
    returned   the opening `copy` named a file without this category: glibc
               returned there, leaving the rest of the section to the top
               level (ld-collate.c:2657-2658@2.39, ld-ctype.c:2139-2140)
    mid_copies [(name, line)] of `copy` statements inside LC_COLLATE
    """

    def __init__(self, category, fname, start, content):
        self.category = category
        self.fname = fname
        self.start = start
        self.end = None
        self.end_word = None
        self.content = content
        self.copy = None
        self.copy_kind = None
        self.skipped = False
        self.returned = False
        self.mid_copies = []

    def __repr__(self):
        return 'Section(%s %s:%s-%s)' % (self.category, self.fname,
                                         self.start, self.end)


class FileRead(object):
    """One call of locfile_read: the file, the categories it was read for,
    the directives it obeyed, its sections, the lines its top level
    dropped as syntax errors, and `cut`: the line where lr_getc gave the top
    level an EOF with the file not read to its end, or None. glibc reads
    nothing of the file after it (locfile.c:115@2.34). Three bytes do it: a
    line holding only the escape character (cases b14, b15, every build),
    and before 2.36 a 0x1A (b02) or an 0xFF through a signed char (b03)."""

    def __init__(self, name, mask):
        self.name = name
        self.mask = frozenset(mask)
        self.directives = []      # (line, 'comment_char'|'escape_char', byte)
        self.sections = []
        self.stray = []           # lines the top level dropped
        self.cut = None


class _Entry(object):
    """localedef's `struct localedef_t`: one file in the read list."""

    def __init__(self, name):
        self.name = name
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
               where glibc reads outside its buffer, which is unsettled.
    unsettled  [(file, line, why)]: places where this reader's model of a
               statement can differ from the handler glibc runs. A caller
               that needs a settled answer refuses one with any of these.
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
        self.met = False
        self.words = set()

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
        r.out.unsettled.append((undefined.fname, undefined.line,
                                'glibc reads outside its buffer here'))
        return r.out
    if r.out.fatal is None:
        r.out.collate = main.data.get('LC_COLLATE')
    return r.out


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

    def unsettled(self, lr, line, why):
        self.out.unsettled.append((lr.fname, line, why))

    # -- localedef.c ------------------------------------------------------

    def add_to_readlist(self, category, name, generate, copy_locale=None):
        """localedef.c:612-662@2.39."""
        runp = None
        for e in self.entries:
            if e.name == name:
                runp = e
                break
        if runp is None:
            assert generate
            runp = _Entry(name)
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

    def load_locale(self, category, name, copy_locale=None):
        """localedef.c:685-701@2.39: queue and read now."""
        result = self.add_to_readlist(category, name, True, copy_locale)
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
        try:
            return self._locfile_read(lr, entry)
        finally:
            # Also when the read ends in a _Stop, a GlibcHang or a
            # GlibcUndefined: what it met is the run's.
            self.out.met = self.out.met or lr.met
            self.out.words |= lr.words

    def _locfile_read(self, lr, entry):
        mask = entry.needed - entry.avail
        fr = FileRead(entry.name, mask)
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
                    fr.stray.append(t.line)       # "bad argument"
                    lr.ignore_rest()
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
                fr.stray.append(t.line)
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
        sec.skipped = True
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
        key = (lr.data, lr.table, snap, lr._cc, lr._ec, lr.translate_strings,
               lr.return_widestr, sec.category)
        kept = lr.build.lookahead.get(key)
        if kept is None:
            met, words = lr.met, lr.words
            lr.met, lr.words = False, set()
            try:
                kept = self._lookahead(lr, sec) + (lr.met, frozenset(lr.words))
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
        if store is not None:
            store.statements.append((lr.fname, t.line, toks))

    @staticmethod
    def _lookahead(lr, sec):
        """(tokens after the first, reasons, lr.pi reached) of the rest of a
        statement's logical line; leaves lr moved, statement() restores it."""
        rest, reasons = [], []
        loud = _TAIL_KEYWORDS.get(sec.category, _TAIL_KEYWORDS[None])
        seen = set()
        try:
            while True:
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
        sec.end_word = arg
        lr.ignore_rest()
        return True

    # -- ld-collate.c -----------------------------------------------------

    def collate_read(self, lr, entry, sec):
        """ld-collate.c:2554-3993@2.39 (2535 at 2.28, 2538 at 2.34)."""
        ignore = not sec.content
        lr.ignore_rest()
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
        copy_locale = None
        if _kw(t, 'tok_copy'):
            s = lr.token()
            sec.copy_kind = 'load'
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
                copy_locale = self.load_locale('LC_COLLATE', name)
                if 'LC_COLLATE' not in copy_locale.avail \
                        and self.locfile_read(copy_locale) != 0:
                    self.skip_category(lr, sec, 'tok_lc_collate')
                    return
                if copy_locale.data.get('LC_COLLATE') is None:
                    sec.returned = True
                    return
            lr.ignore_rest()
            t = lr.token()
        # collate_startup (ld-collate.c:1496-1528@2.39).
        if not ignore and entry.data.get('LC_COLLATE') is None:
            entry.data['LC_COLLATE'] = Collate() if copy_locale is None \
                else copy_locale.data.get('LC_COLLATE')
        lr.translate_strings = 0
        lr.return_widestr = 0
        store = None if ignore else entry.data.get('LC_COLLATE')
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
                        self.load_locale('LC_COLLATE', name, entry)
                    lr.ignore_rest()
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
                    # (measured on el8/el9/el10, case s41); why is the
                    # statement grammar's to say (block 15, step 15.3).
                    self.unsettled(lr, t.line, 'an ellipsis before '
                                   'order_start')
            self.statement(lr, t, sec, store)
            t = lr.token()

    def conditional(self, lr, t, sec, coll):
        """define, undef, ifdef, ifndef, elifdef, elifndef, else and endif
        in an LC_COLLATE read for its content (ld-collate.c:3804-3978@2.39,
        the same at 2.28 and 2.34). Returns the next token to dispatch, or
        None when the section ended (on END or at the end of the file)."""
        kw = t.kwtok
        if kw in ('tok_define', 'tok_undef'):
            arg = lr.token()
            if arg.tok == 'ident':
                if kw == 'tok_define':
                    self.defined.insert(0, arg.val)
                else:
                    self.defined = [d for d in self.defined if d != arg.val]
            coll.statements.append((lr.fname, t.line, [t, arg]))
            lr.ignore_rest()
            return lr.token()
        if kw == 'tok_endif':
            lr.ignore_rest()
            coll.else_action = 'none'
            return lr.token()
        if kw in ('tok_elifdef', 'tok_elifndef', 'tok_else'):
            lr.ignore_rest()
            if coll.else_action == 'ignore':
                stop = self.skip_to(lr, True)
                return self.after_skip(lr, stop, sec)
            if coll.else_action == 'seen':
                # assert (collate->else_action == else_none) fails
                # (ld-collate.c:3953@2.39): SIGABRT on el8/el9/el10, where
                # the else is in a file copied inside an else branch, which
                # shares the copier's struct (case c48).
                raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                                  'a second else: assertion failure'))
            return lr.token()
        # ifdef / ifndef, and found_ifdef after an elifdef or elifndef.
        while True:
            arg = lr.token()
            if arg.tok != 'ident':
                lr.ignore_rest()                    # err_label
                return lr.token()
            lr.ignore_rest()
            if coll.else_action != 'none':
                stop = self.skip_to(lr, True)       # nested: not supported
                return self.after_skip(lr, stop, sec)
            hit = arg.val in self.defined
            if (kw == 'tok_ifdef') == hit:
                coll.else_action = 'ignore'
                return lr.token()
            stop = self.skip_to(lr, False)
            if _kw(stop, 'tok_else'):
                coll.else_action = 'seen'
                return lr.token()
            if _kw(stop, 'tok_elifdef'):
                kw = 'tok_ifdef'
                continue
            if _kw(stop, 'tok_elifndef'):
                kw = 'tok_ifndef'
                continue
            return self.after_skip(lr, stop, sec)

    def after_skip(self, lr, stop, sec):
        """What follows a skip_to that met the end of the file or END
        (seen_eof, seen_end); any other stop reads on."""
        if stop.tok == 'eof':
            return None
        if _kw(stop, 'tok_end'):
            # seen_end is the END case itself (ld-collate.c:3741@2.34).
            if self.end_of_section(lr, stop, sec):
                return None
        return lr.token()

    def skip_to(self, lr, to_endif):
        """ld-collate.c:2514-2552@2.39: the first token of each logical
        line, until END, the end of the file, endif, or (when not looking
        for endif) else, elifdef or elifndef. Returns that token."""
        while True:
            t = lr.token()
            if t.tok == 'eof' or _kw(t, 'tok_end'):
                return t
            if t.tok == 'kw' and t.kwtok in ('tok_ifdef', 'tok_ifndef'):
                inner = self.skip_to(lr, True)
                if inner.tok == 'eof' or _kw(inner, 'tok_end'):
                    return inner
            elif _kw(t, 'tok_endif') or (not to_endif
                                          and _kw(t, 'tok_else')):
                lr.ignore_rest()
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
            sec.copy_kind = 'load'
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
                copy_locale = self.load_locale('LC_CTYPE', name)
                if 'LC_CTYPE' not in copy_locale.avail \
                        and self.locfile_read(copy_locale) != 0:
                    self.skip_category(lr, sec, 'tok_lc_ctype')
                    return
                if copy_locale.data.get('LC_CTYPE') is None:
                    sec.returned = True
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
        sec.copy_kind = 'queue'
        s = lr.token()
        line = s.line
        if s.tok == 'string':
            name = self.copy_name(lr, s, line, not ignore)
            sec.copy = (name, line)
            if not ignore and name is not None:
                self.add_to_readlist(sec.category, name, True)
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
        arg = lr.token()
        if arg.tok != 'eof':
            sec.end_word = arg
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
            target = name.val
        else:
            target = self.copy_name(lr, name, t.line)
        lr.ignore_rest()
        if target is None:
            # add_to_readlist would strcmp a NULL name (INFERRED crash).
            raise _Stop(Fatal('crash', None, (lr.fname, t.line),
                              'translit include with a NULL name'))
        self.add_to_readlist('LC_CTYPE', target, True)


def written_collate(data):
    """[(start, end)] of every LC_COLLATE block as the TEXT holds it: a line
    that starts with `LC_COLLATE`, to the next line that starts with
    `END LC_COLLATE` (end None when there is none). 1-based lines.

    This is where the block is written, which is what the tool compares
    between two versions; it is not a claim about what glibc reads. Run
    says that, and the two answers are kept apart because a file can hold a
    block glibc never reads (backlog 13.18).
    """
    out = []
    start = None
    for idx, line in enumerate(data.split(b'\n'), 1):
        if start is None:
            if line.startswith(b'LC_COLLATE'):
                start = idx
        elif line.startswith(b'END LC_COLLATE'):
            out.append((start, idx))
            start = None
    if start is not None:
        out.append((start, None))
    return out
