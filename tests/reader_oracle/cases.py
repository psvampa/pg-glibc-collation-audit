"""The cases localedef answers for scripts/glibc_reader.py (backlog 15.1).

Each case is one `localedef -i <main> -f UTF-8` run on a small locale built
for one question about how glibc reads a file: its bytes, its comment and
escape directives, its tokens, where its sections end, and what its `copy`
statements load. measure.py runs every case on a RHEL machine and records
what localedef did; tests/test_glibc_reader.py holds the reader to it.

What a case observes: the exit status, whether the time limit stopped
localedef, the messages that name an outcome (a file it cannot open, a
circular copy, a file loaded a second time, "giving up"), and the
LC_COLLATE file written -- the 92 bytes glibc writes when it stored no
LC_COLLATE, or a table whose bytes hold the `from` string of every
collating element that glibc both defined and placed in the order. Most
cases define such marker elements, `<mk-X>` with a four-byte `from` string
that is in no other part of the file, at the places whose reading is the
question.

The files are self-contained: every name a case means to be found starts
with `rd-`, which no locale glibc ships does, so no shipped locale can
answer for one. Nothing here may change once recorded without measuring
again: corpus_digest() is in every recorded file and the test compares it.

Standard library only, Python 3.6.
"""
import hashlib
import json

FAMILIES = ('bytes', 'directives', 'tokens', 'sections', 'copy')


def marker_text(name):
    """The four bytes of marker `name`'s `from` string."""
    return ('Zq' + name + 'ww')[:4].encode('ascii')


def _ucs(text):
    return b''.join(b'<U%04X>' % c for c in text)


def D(m):
    """The line defining marker m."""
    return b'collating-element <mk-%s> from "%s"\n' % (
        m.encode(), _ucs(marker_text(m)))


def O(m):
    """The order line placing marker m."""
    return b'<mk-%s>\n' % m.encode()


ORDER_HEAD = b'order_start forward\n<U0062>\n<U0061>\n'
ORDER_TAIL = b'UNDEFINED\norder_end\n'
HEAD = b'LC_COLLATE\n'
END = b'END LC_COLLATE\n'


def body(*ms):
    """Definitions, then the order placing them: an LC_COLLATE body."""
    return b''.join(D(m) for m in ms) + ORDER_HEAD + \
        b''.join(O(m) for m in ms) + ORDER_TAIL


def coll(*ms):
    return HEAD + body(*ms) + END


IDENT = b'LC_IDENTIFICATION\ntitle "t"\nEND LC_IDENTIFICATION\n'
NUMERIC = b'LC_NUMERIC\ndecimal_point "."\nEND LC_NUMERIC\n'
TIME7 = b'LC_TIME\nabday "1";"2";"3";"4";"5";"6";"7"\nEND LC_TIME\n'


class Case(object):
    def __init__(self, cid, family, files, main=None, markers=None,
                 note=''):
        self.id = cid
        self.family = family
        self.main = main or 'rd-' + cid
        self.files = {}
        for name, data in files.items():
            self.files[name.replace('@', 'rd-' + cid)] = data.replace(
                b'@', b'rd-' + cid.encode())
        if self.main not in self.files:
            raise ValueError('%s: no main file' % cid)
        found = set()
        for data in self.files.values():
            for line in data.split(b'\n'):
                if line.startswith(b'collating-element <mk-'):
                    found.add(line.split(b'<mk-')[1].split(b'>')[0].decode())
        self.markers = {m: marker_text(m).decode() for m in
                        sorted(found if markers is None else markers)}
        self.note = note


def _cases():
    out = []

    def add(cid, family, files, note, **kw):
        if not isinstance(files, dict):
            files = {'@': files}
        out.append(Case(cid, family, files, note=note, **kw))

    # -- bytes: lr_getc, lr_next and lr_create (linereader.c/.h) ----------
    B = 'bytes'
    add('b01', B, IDENT + coll('a'), 'control: one marker read')
    add('b02', B, IDENT + b'\x1a\n' + coll('a'),
        '0x1A on its own line above LC_COLLATE')
    add('b03', B, IDENT + b'\xff\n' + coll('a'),
        '0xFF on its own line above LC_COLLATE')
    add('b04', B, HEAD + D('a') + D('b') + ORDER_HEAD + O('a') + b'\x1a\n'
        + O('b') + ORDER_TAIL + END, '0x1A between two order lines')
    add('b05', B, HEAD + D('a') + D('b') + ORDER_HEAD + O('a') + b'\xff\n'
        + O('b') + ORDER_TAIL + END, '0xFF between two order lines')
    add('b06', B, b'comment_char %\n% a comment \x1a here\n' + coll('a'),
        '0x1A inside a comment line above LC_COLLATE')
    add('b07', B, coll('a') + b'\x1a\n', '0x1A after END LC_COLLATE')
    add('b08', B, b'LC_COLLATE \x1a\n' + body('a') + END,
        '0x1A on the LC_COLLATE line, after the keyword')
    add('b09', B, HEAD + body('a') + b'\x1aEND LC_COLLATE\n',
        '0x1A at the start of the END line')
    add('b10', B, IDENT + coll('a')[:-1], 'no newline at the end of the file')
    add('b11', B, b'LC_IDENTIFICATION\ntitle "t"\nEND LC_IDENTIFICATION \\\n'
        + coll('a'), 'END line of the section above continued onto LC_COLLATE')
    add('b12', B, b'LC_IDENTIFICATION\ntitle "t"\nEND LC_IDENTIFICATION \\ \n'
        + coll('a'), 'escape then a blank then the newline: not a continuation')
    add('b13', B, b'LC_IDENTIFICATION\r\ntitle "t"\r\nEND LC_IDENTIFICATION'
        b' \\\r\n' + coll('a'), 'escape then CR LF: not a continuation')
    add('b14', B, b'\\\n' + coll('a'),
        'a first line holding only the escape character')
    add('b15', B, IDENT + b'\\\n' + coll('a'),
        'a later line holding only the escape character')
    add('b16', B, (IDENT + coll('a')).replace(b'\n', b'\r\n'),
        'CR LF line ends throughout')
    add('b17', B, IDENT + b'\x00\n' + coll('a'), 'a NUL byte alone on a line')
    add('b18', B, b'LC_IDENTIFICATION\ntitle "t\xff"\nEND LC_IDENTIFICATION\n'
        + coll('a'), '0xFF inside a string in the section above')
    add('b19', B, b'LC_IDENTIFICATION\ntitle "t\x1a"\nEND LC_IDENTIFICATION\n'
        + coll('a'), '0x1A inside a string in the section above')
    add('b20', B, {'@': IDENT + HEAD + b'copy "@-t"\n' + END,
                   '@-t': b'\x1a\n' + coll('t')},
        '0x1A above LC_COLLATE in the file LC_COLLATE copies')
    add('b21', B, {'@': HEAD + b'copy "@-t"\n' + END, '@-t': b''},
        'LC_COLLATE copies an empty file')
    add('b22', B, b'% caf\xc3\xa9\n' + IDENT + coll('a'),
        'valid UTF-8 in a line glibc drops as a syntax error')
    add('b23', B, b'caf\xe9 x\n' + IDENT + coll('a'),
        'a byte that is not UTF-8 at the top level')
    add('b24', B, HEAD + D('a') + D('b') + ORDER_HEAD + O('a') + b'\\\n'
        + O('b') + ORDER_TAIL + END,
        'a line holding only the escape character inside LC_COLLATE')

    # -- directives: comment_char and escape_char (locfile.c:123-149) ------
    C = 'directives'
    add('d01', C, b'comment_char %\n% END LC_COLLATE\n' + coll('a'),
        'comment_char % makes a % line a comment')
    add('d02', C, b'comment_char L\n' + coll('a'),
        'comment_char L: the LC_COLLATE line is a comment')
    add('d03', C, b'comment_char E\n' + HEAD + body('a') + END + coll('b'),
        'comment_char E: END lines are comments')
    add('d04', C, b'comment_char LL\n' + coll('a'),
        'a two-character argument is refused')
    add('d05', C, b'comment_char "L"\n' + coll('a'),
        'a string argument is refused')
    add('d06', C, b'comment_char\n' + coll('a'), 'no argument')
    add('d07', C, b'comment_char \\\nL\n' + coll('a'),
        'the argument continued onto the next line')
    add('d08', C, b'comment_\\\nchar L\n' + coll('a'),
        'the keyword split across a continuation')
    add('d09', C, b'comm\\ent_char L\n' + coll('a'),
        'an escape inside the keyword')
    add('d10', C, b'LC_IDENTIFICATION\ncomment_char L\nEND LC_IDENTIFICATION\n'
        + coll('a'), 'a directive inside a section is not obeyed')
    add('d11', C, coll('a') + b'comment_char L\n' + coll('b'),
        'a directive between two LC_COLLATE sections')
    add('d12', C, b'escape_char /\nLC_IDENTIFICATION\ntitle "t"\n'
        b'END LC_IDENTIFICATION /\n' + coll('a'),
        'escape_char /: a line ending in / is continued')
    add('d13', C, b'escape_char /\n' + IDENT[:-1] + b' \\\n' + coll('a'),
        'escape_char /: a line ending in \\ is not continued')
    add('d14', C, b'escape_char /\nLC_COLL/ATE\n' + body('a') + END,
        'the escape character inside a word keeps the next character')
    add('d15', C, b'escape_char /\nLC_COLL/\nATE\n' + body('a') + END,
        'a word continued across a line by the escape character')
    add('d16', C, b'comment_char %\nescape_char %\n' + IDENT[:-1] + b' %\n'
        + coll('a'), 'the same character for both')
    add('d17', C, b'escape_char \xa7\n' + IDENT[:-1] + b' \xa7\n' + coll('a'),
        'a one-byte escape character above 0x7F')
    add('d18', C, b'comment_char \xa7\n\xa7LC_IDENTIFICATION\n' + coll('a'),
        'a one-byte comment character above 0x7F')
    add('d19', C, {'@': HEAD + b'copy "@-t"\n' + END,
                   '@-t': b'comment_char L\n' + coll('t')},
        'a directive in a copied file applies to that file')
    add('d21', C, b'comment_char %\n%LC_IDENTIFICATION \\\n' + coll('a'),
        'a comment line ending in the escape drops only its own line')
    add('d22', C, b'comment_char %\nLC_IDENTIFICATION\n% x \\\n'
        b'END LC_IDENTIFICATION\n' + coll('a'),
        'a continued comment line inside a section above')
    add('d23', C, b'escape_char x y\n' + coll('a'),
        'trailing garbage after the directive argument')
    add('d24', C, b'repertoiremap\n' + coll('a'),
        'repertoiremap with no argument')

    # -- tokens: how a line becomes keywords (lr_token) --------------------
    T = 'tokens'
    add('t01', T, b'LC_COLLATE junk\n' + body('a') + END,
        'trailing garbage on the LC_COLLATE line')
    add('t02', T, b'LC_COLLATEX\n' + body('a') + END, 'a longer word')
    add('t03', T, b'lc_collate\n' + body('a') + END, 'lower case')
    add('t04', T, b'LC_COLLATE;\n' + body('a') + END, 'a word ended by ;')
    add('t05', T, b'LC_COLLATE"x"\n' + body('a') + END,
        'a word ended by a string')
    add('t06', T, b'<LC_COLLATE>\n' + body('a') + END, 'a symbolic name')
    add('t07', T, b'"LC_COLLATE"\n' + body('a') + END, 'a string')
    add('t08', T, HEAD + body('a') + b'END<LC_COLLATE>\n' + coll('b'),
        'END followed by a symbolic name')
    add('t09', T, HEAD + body('a') + b'END\t LC_COLLATE  # x\n' + coll('b'),
        'blanks and a comment on the END line')
    add('t10', T, HEAD + body('a') + b'END\n' + coll('b'),
        'END alone, then a new LC_COLLATE section')
    add('t11', T, HEAD + body('a') + b'END\nLC_COLLATE\n' + D('b') + END,
        'END alone, then LC_COLLATE: a second section')
    add('t12', T, HEAD + D('a') + b'END.LC_COLLATE\n' + ORDER_HEAD + O('a')
        + ORDER_TAIL + END, 'END.LC_COLLATE is one word')
    add('t13', T, b'LC_COLL\\ATE\n' + body('a') + END,
        'an escape inside the LC_COLLATE word')
    add('t14', T, HEAD + body('a') + b'E\\ND LC_COLLATE\n' + coll('b'),
        'an escape inside the END word')
    add('t15', T, b'1LC_COLLATE\n' + body('a') + END,
        'a number glued to the word')
    add('t16', T, b'-LC_COLLATE\n' + body('a') + END, 'a leading minus')
    add('t17', T, b'\\x41 LC_COLLATE\n' + body('a') + END,
        'a character code before the word')
    add('t18', T, b'x;LC_COLLATE\n' + body('a') + END,
        'the word after ; on a top-level line')
    add('t19', T, b'  \t LC_COLLATE\n' + body('a') + END,
        'blanks before the word')
    add('t20', T, HEAD + body('a') + b'  END LC_COLLATE\n' + coll('b'),
        'blanks before END')
    add('t21', T, HEAD + body('a') + b'END LC_COLLATE junk\n' + coll('b'),
        'trailing garbage after END LC_COLLATE')
    add('t22', T, HEAD + body('a') + b'END LC_CTYPE\n' + coll('b'),
        'END with the wrong category')
    add('t23', T, HEAD + body('a') + b'END "LC_COLLATE"\n' + coll('b'),
        'END with a string')
    add('t24', T, b'LC_COLLATE % x\n' + body('a') + END,
        'a % after the word under the default comment character')
    add('t25', T, b'LC_COLLATE # x\n' + body('a') + END,
        'a comment after the word')
    add('t26', T, b'LC_COLLATE\\\n' + body('a') + END,
        'the LC_COLLATE line continued onto the next')

    # -- sections: where each category ends (locfile.c, ld-*.c) ------------
    S = 'sections'
    add('s01', S, NUMERIC + IDENT + TIME7 + coll('a'), 'control: three sections')
    add('s02', S, b'LC_IDENTIFICATION\ntitle "t"\n' + coll('a'),
        'the section above has no END')
    add('s03', S, b'LC_IDENTIFICATION\ntitle "t"\n# END LC_IDENTIFICATION\n'
        + coll('a'), 'the END above is commented out')
    add('s04', S, b'LC_IDENTIFICATION\ntitle "t"\nEND.LC_IDENTIFICATION\n'
        + coll('a'), 'END.LC_IDENTIFICATION does not end it')
    add('s05', S, b'LC_IDENTIFICATION\ntitle "t"\nEND LC_NUMERIC\n'
        + coll('a'), 'END with the wrong category ends it')
    add('s06', S, b'LC_IDENTIFICATION\ntitle "t"\nEND\n' + coll('a'),
        'END alone ends it')
    add('s07', S, IDENT + HEAD + body('a') + END + HEAD + body('b') + END,
        'two LC_COLLATE sections')
    add('s08', S, HEAD + END + coll('a'),
        'an empty LC_COLLATE section, then a full one')
    add('s09', S, IDENT + HEAD + body('a'), 'LC_COLLATE without END')
    add('s10', S, b'stray words\n' + IDENT + b'"x"\n' + coll('a'),
        'top-level lines outside any section')
    add('s11', S, b'LC_CTYPE\ncopy i18n\nEND LC_CTYPE\n' + coll('a'),
        'an LC_CTYPE copy with no string: skip_category')
    add('s12', S, HEAD + b'copy x\n' + END + coll('a'),
        'an LC_COLLATE copy with no string, then another LC_COLLATE')
    add('s13', S, HEAD + b'copy x\n' + END + D('a') + ORDER_HEAD + O('a')
        + ORDER_TAIL + END,
        'an LC_COLLATE copy with no string, then a body with no header')
    add('s14', S, b'LC_TIME\nabday "1";"2"\nEND LC_TIME\n' + coll('a'),
        'an LC_TIME array with too few values before END')
    add('s15', S, b'LC_TIME\nabday "1";"2";"3";"4";"5";"6";"7"\n'
        b'END LC_TIME\n' + coll('a'), 'control for s14: a full array')
    add('s16', S, b'LC_TIME\nabday "1";"2"\n\nEND LC_TIME\n' + coll('a'),
        'a short array ended by a blank line')
    add('s17', S, b'LC_TIME\nam_pm "1";\nEND LC_TIME\n' + coll('a'),
        'an array ending in ;')
    add('s18', S, b'LC_CTYPE\ntranslit_start\nEND LC_CTYPE\n' + coll('a'),
        'translit_start with no translit_end')
    add('s19', S, b'LC_CTYPE\ntranslit_start\ntranslit_end\nEND LC_CTYPE\n'
        + coll('a'), 'control for s18')
    add('s20', S, b'LC_IDENTIFICATION junk\ntitle "t"\nEND LC_IDENTIFICATION\n'
        + coll('a'), 'trailing garbage after a header')
    add('s21', S, b'LC_IDENTIFICATION\n' + HEAD + D('a')
        + b'END LC_IDENTIFICATION\n' + ORDER_HEAD + O('a') + ORDER_TAIL + END
        + coll('b'), 'LC_COLLATE inside a section that is still open')
    add('s22', S, b'LC_NUMERIC\ncopy "@-n"\nEND LC_NUMERIC\n' + coll('a'),
        'a queued copy of a missing file')
    add('s23', S, {'@': b'LC_NUMERIC\ncopy "@-n"\nEND LC_NUMERIC\n'
                   + coll('a'),
                   '@-n': NUMERIC},
        'control for s22')
    add('s24', S, {'@': b'LC_NUMERIC\ncopy "@-n"\nx\nEND LC_NUMERIC\n'
                   + coll('a'), '@-n': NUMERIC},
        'another line after a queued copy')
    add('s25', S, {'@': b'LC_NUMERIC\ncopy "@-n"\n' + coll('a'),
                   '@-n': NUMERIC}, 'a queued copy with no END')
    add('s26', S, b'LC_TIME\nabday "1";"2";"3";"4";"5";"6";"7";\n'
        b'END LC_TIME\n' + coll('a'), 'a full array ending in ;')
    add('s27', S, b'LC_CTYPE\ntranslit_start\nEND LC_CTYPE\ntranslit_end\n'
        b'END LC_CTYPE\n' + coll('a'), 'END inside translit')
    add('s28', S, HEAD + D('a') + END,
        'LC_COLLATE read with no order_start: collate_finish gives up')
    add('s29', S, b'LC_TIME\nd_fmt "x"\nabday "1";"2";"3";"4";"5";"6";"7"\n'
        b'END LC_TIME\n' + coll('a'), 'control for s30 and s31')
    add('s30', S, b'LC_TIME\nd_fmt "x" abday "1"\nEND LC_TIME\n' + coll('a'),
        'an array keyword left after a string field')
    add('s31', S, b'LC_TIME\nfirst_weekday 1 abday "1"\nEND LC_TIME\n'
        + coll('a'), 'an array keyword left after a number field')

    # -- copy: the three forms and localedef's queue ----------------------
    P = 'copy'
    add('c01', P, {'@': HEAD + b'copy "@-t"\n' + END, '@-t': coll('t')},
        'copy at the start of LC_COLLATE')
    add('c02', P, {'@': HEAD + b'copy @-t\n' + END + coll('a'),
                   '@-t': coll('t')}, 'unquoted copy')
    add('c03', P, {'@': HEAD + b'copy "@-missing"\n' + END},
        'copy of a missing file')
    add('c04', P, {'@': HEAD + b'copy "@-t"\n' + END + IDENT,
                   '@-t': IDENT}, 'copy of a file without LC_COLLATE')
    add('c05', P, {'@': HEAD + b'copy "<U0072><U0064>-c05-t"\n' + END,
                   '@-t': coll('t')}, 'symbolic name at the start')
    add('c06', P, {'@': HEAD + D('a') + b'copy "@-t"\n' + ORDER_HEAD + O('a')
                   + ORDER_TAIL + END, '@-t': coll('t')},
        'copy in the middle of LC_COLLATE')
    add('c07', P, {'@': HEAD + D('a') + b'copy "<U0072>d-c07-t"\n'
                   + ORDER_HEAD + O('a') + ORDER_TAIL + END,
                   '@-t': coll('t')},
        'a symbolic name in the middle is not decoded')
    add('c08', P, {'@': HEAD + D('a') + b'copy "@-t"\ncopy "@-t"\n'
                   + ORDER_HEAD + O('a') + ORDER_TAIL + END,
                   '@-t': coll('t')}, 'the same file copied twice')
    add('c09', P, {'@': HEAD + b'copy "@"\n' + END}, 'a file copying itself')
    add('c10', P, {'@': HEAD + b'copy "@-t"\n' + END,
                   '@-t': HEAD + b'copy "@-u"\n' + END, '@-u': coll('u')},
        'a chain of two copies')
    add('c11', P, {'@': HEAD + b'copy "@-t"\n' + END,
                   '@-t': HEAD + b'copy "@-u"\n' + END, '@-u': IDENT},
        'the end of the chain has no LC_COLLATE')
    add('c12', P, {'@': HEAD + b'copy"@-t"\n' + END, '@-t': coll('t')},
        'no blank before the string')
    add('c13', P, {'@': HEAD + b'copy "@-t" junk\n' + END,
                   '@-t': coll('t')}, 'trailing garbage after the string')
    add('c14', P, {'@': HEAD + b'copy \\\n"@-t"\n' + END, '@-t': coll('t')},
        'the string on a continued line')
    add('c15', P, {'@': HEAD + b'copy "@-t\n' + END, '@-t': coll('t')},
        'an unterminated string at the start')
    add('c16', P, {'@': HEAD + D('a') + b'copy "@-t\n' + ORDER_HEAD + O('a')
                   + ORDER_TAIL + END, '@-t': coll('t')},
        'an unterminated string in the middle')
    add('c17', P, {'@': HEAD + b'copy ""\n' + END}, 'an empty name')
    add('c18', P, {'@': b'LC_IDENTIFICATION\ncopy "@-missing"\n'
                   b'END LC_IDENTIFICATION\n' + coll('a')},
        'a queued copy of a missing file in another category')
    add('c19', P, {'@': b'LC_IDENTIFICATION\ncopy "@-t"\n'
                   b'END LC_IDENTIFICATION\n' + coll('a'),
                   '@-t': IDENT + HEAD + b'copy "@-missing"\n' + END},
        'a queued file whose LC_COLLATE copies a missing file')
    add('c20', P, {'@': b'LC_CTYPE\ncopy "@-t"\nx y\nEND LC_CTYPE\n'
                   + coll('a'), '@-t': IDENT},
        'LC_CTYPE copies a file without LC_CTYPE')
    add('c21', P, {'@': b'LC_CTYPE\ntranslit_start\ninclude "@-missing";""\n'
                   b'translit_end\nEND LC_CTYPE\n' + coll('a')},
        'a translit include of a missing file')
    add('c22', P, {'@': HEAD + D('a') + b'copy "@-t"\n' + ORDER_HEAD + O('a')
                   + ORDER_TAIL + END,
                   '@-t': HEAD + b'copy "@-u"\n' + END, '@-u': coll('u')},
        'a file copied in the middle opens with its own copy')
    add('c23', P, {'@': HEAD + b'define X\ncopy "@-t"\n' + END,
                   '@-t': coll('t')}, 'define lines before the copy')
    add('c24', P, {'@': HEAD + b'copy "@-t\x00x"\n' + END, '@-t': coll('t')},
        'a NUL byte inside the name')
    add('c25', P, {'@': coll('a') + HEAD + b'copy "@-t"\n' + END,
                   '@-t': coll('t')}, 'a second LC_COLLATE that opens with copy')
    add('c26', P, {'@': HEAD + b'copy "@-t"\n' + END,
                   '@-t': IDENT + b'\x1a\n' + coll('t')},
        '0x1A above the copied file\'s LC_COLLATE')
    add('c27', P, {'@': HEAD + D('a') + b'ifdef NOPE\ncopy "@-t"\nendif\n'
                   + ORDER_HEAD + O('a') + ORDER_TAIL + END,
                   '@-t': coll('t')}, 'a copy inside a false conditional')
    add('c28', P, {'@': HEAD + b'define YES\n' + D('a')
                   + b'ifdef YES\ncopy "@-t"\nelse\ncopy "@-u"\nendif\n'
                   + ORDER_HEAD + O('a') + ORDER_TAIL + END,
                   '@-t': coll('t'), '@-u': coll('u')},
        'a copy inside a true conditional, another in its else')
    add('c29', P, {'@': HEAD + b'ifdef NOPE\n' + body('a') + END
                   + coll('b')}, 'END inside a false conditional')
    add('c30', P, {'@': b'LC_CTYPE\ncopy "@-t"\nEND LC_CTYPE\n' + coll('a'),
                   '@-t': b'LC_CTYPE\nEND LC_CTYPE\n' + coll('t')},
        'an LC_CTYPE copy reads only the copied file\'s LC_CTYPE')
    add('c33', P, {'@': b'LC_CTYPE\ntranslit_start\n<U0041> "" include '
                   b'"@-gone";""\ntranslit_end\nEND LC_CTYPE\n' + coll('a')},
        'control for c34: an include after an entry\'s values')
    add('c34', P, {'@': b'LC_CTYPE\ntranslit_start\n"" include "@-gone";""\n'
                   b'translit_end\nEND LC_CTYPE\n' + coll('a')},
        'an include after a translit entry whose first value is empty')
    add('c35', P, {'@': HEAD + b'copy "@-t"\ncopy "@-u"\n' + END,
                   '@-t': HEAD + b'copy "@-u"\n' + END, '@-u': coll('u')},
        'a file copied in the middle that a copied file already loaded')
    tl = b'LC_CTYPE\ntranslit_start\n%s\ntranslit_end\nEND LC_CTYPE\n'
    add('c36', P, {'@': HEAD + b'copy "@-t"\nifdef X\ncopy "@-u"\nendif\n'
                   + END, '@-t': HEAD + b'define X\ncopy "@-u"\n' + END,
                   '@-u': coll('u')},
        'a define in a copied file decides an ifdef in the copier')
    add('c37', P, {'@': HEAD + b'copy "@-t"\nifdef X\ncopy "@-u"\nendif\n'
                   + END, '@-t': HEAD + b'copy "@-u"\n' + END,
                   '@-u': coll('u')}, 'control for c36: no define')
    add('c38', P, {'@': HEAD + b'define X\ncopy "@-t"\ncopy "@-u"\n' + END,
                   '@-t': HEAD + b'ifdef X\ncopy "@-u"\nendif\n' + END,
                   '@-u': coll('u')},
        'the copier\'s define decides an ifdef that loads a file twice')
    add('c39', P, {'@': HEAD + b'copy "@-t"\ncopy "@-u"\n' + END,
                   '@-t': HEAD + b'ifdef X\ncopy "@-u"\nendif\n' + END,
                   '@-u': coll('u')}, 'control for c38: no define')
    add('c40', P, {'@': tl % b'include "@-x";""\ninclude "@-y";""' + coll('a'),
                   '@-x': tl % b'include "@-y";""',
                   '@-y': b'LC_CTYPE\nEND LC_CTYPE\n'},
        'a queued file includes a file the main file queued too')
    add('c41', P, {'@': tl % b'include "@-x";""\ninclude "@-y";""' + coll('a'),
                   '@-x': b'LC_CTYPE\nEND LC_CTYPE\n',
                   '@-y': b'LC_CTYPE\nEND LC_CTYPE\n'}, 'control for c40')
    add('c42', P, {'@': tl % b'include "@-x";""' + coll('a'),
                   '@-x': tl % b'include "@";""'},
        'two files including each other')
    add('c43', P, {'@': b'LC_TIME\ncopy "@-x"\nEND LC_TIME\n' + coll('a'),
                   '@-x': b'LC_TIME\ncopy "@"\nEND LC_TIME\n'},
        'two files copying each other\'s LC_TIME')
    add('c44', P, {'@': tl % b'"<U0378>" include "@-gone";""' + coll('a')},
        'a translit value not in the charmap, then an include')
    add('c45', P, {'@': tl % b'"<U0000>" include "@-gone";""' + coll('a')},
        'a translit value starting with U+0000, then an include')
    add('c46', P, {'@': tl % b'"\x00" include "@-gone";""' + coll('a')},
        'a translit value that is a NUL byte, then an include')
    add('c47', P, {'@': tl % b'"<U0041>" include "@-gone";""' + coll('a')},
        'control for c44 to c46: a value, then an include')
    add('c48', P, {'@': HEAD + b'ifdef NOPE\nelse\ncopy "@-t"\nendif\n'
                   + body('a') + END, '@-t': HEAD + b'else\n' + END},
        'a file copied inside an else branch meets an else of its own')
    add('c49', P, {'@': HEAD + b'ifdef NOPE\nelse\ncopy "@-t"\nendif\n'
                   + body('a') + END, '@-t': HEAD + END}, 'control for c48')
    add('c50', P, {'@': tl % b'include "@-gone";"<U0378>"' + coll('a')},
        'a translit include whose repertoire name is not in the charmap')
    add('c51', P, {'@': tl % b'include "@-gone";"<U0041>"' + coll('a')},
        'control for c50')
    qt = b'LC_TIME\ncopy "@-x"\nEND LC_TIME\n' + coll('a')
    add('s32', S, {'@': qt, '@-x': b'LC_CTYPE\ncharclass foo\nEND LC_CTYPE\n'
                   + TIME7}, 'charclass in a file read for LC_TIME')
    add('s33', S, {'@': qt, '@-x': HEAD + b'..\n' + END + TIME7},
        'an ellipsis in a file read for LC_TIME')
    add('s34', S, {'@': qt, '@-x': HEAD + b'codepoint_collation\n' + END
                   + TIME7}, 'codepoint_collation in a file read for LC_TIME')
    add('s35', S, {'@': qt, '@-x': b'LC_CTYPE\nfoo\nEND LC_CTYPE\n' + HEAD
                   + b'<U0061>\n' + END + TIME7},
        'control for s32 to s34')
    for cid, line, note in (
            ('s36', b'charclass', 'charclass alone'),
            ('s37', b'charclass 5', 'charclass and a number'),
            ('s38', b'charconv', 'charconv alone'),
            ('s39', b'charconv foo', 'charconv and a name'),
            ('s40', b'charclass "foo"', 'charclass and a string')):
        add(cid, S, {'@': qt, '@-x': b'LC_CTYPE\n' + line + b'\nEND LC_CTYPE\n'
                     + TIME7}, note + ' in a file read for LC_TIME')
    add('s41', S, HEAD + D('a') + b'..\n' + ORDER_HEAD + O('a') + ORDER_TAIL
        + END, 'an ellipsis before order_start')
    twice = b'LC_TIME\ncopy "@-t"\nEND LC_TIME\n'
    add('s42', S, {'@': HEAD + b'copy "@-t"\n' + END + twice,
                   '@-t': HEAD + b'codepoint_collation\n' + END + TIME7},
        'codepoint_collation in a file read again for LC_TIME')
    add('s43', S, {'@': b'LC_CTYPE\ncopy "@-t"\nEND LC_CTYPE\n' + coll('a')
                   + twice,
                   '@-t': b'LC_CTYPE\ncharclass foo\nEND LC_CTYPE\n' + TIME7},
        'charclass in a file read again for LC_TIME')
    add('s44', S, {'@': HEAD + b'copy "@-t"\n' + END + twice,
                   '@-t': HEAD + b'codepoint_collation\n' + END},
        'control for s42: the copied file has no LC_TIME')
    add('s45', S, {'@': HEAD + b'order_start forward\ncopy "@-t"\n' + END
                   + twice, '@-t': HEAD + b'..\n' + END + TIME7},
        'an ellipsis in a file copied in the middle, read again for LC_TIME')
    # A number that ends the file with no newline is read again on every
    # lr_token (lr_ungetn after EOF, linereader.c:288-301@2.39).
    add('s46', S, IDENT + coll('a') + b'LC_NUMERIC\nX 1',
        'a statement ending the file in a number, no newline')
    add('s47', S, IDENT + coll('a') + b'LC_CTYPE\ncopy\n1',
        'skip_category of LC_CTYPE meeting a number at the end, no newline')
    add('s48', S, IDENT + HEAD + b'copy\n1',
        'skip_category of LC_COLLATE meeting a number at the end, no newline')
    add('s49', S, IDENT + HEAD + b'copy\n1\n', 'control for s48: a newline')
    # Before 2.36 a 0x1A reads as EOF without being consumed, so the same
    # holds in the middle of a file (linereader.h:138@2.34).
    add('s50', S, IDENT + HEAD + b'copy\n7\x1a\nEND LC_COLLATE\n',
        'skip_category of LC_COLLATE meeting a number, then 0x1A')
    add('s51', S, IDENT + HEAD + D('a') + ORDER_HEAD + b'7\x1a\n' + O('a')
        + ORDER_TAIL + END, 'a statement of a number, then 0x1A')
    cond = (HEAD + D('t') + D('v') + ORDER_HEAD + b'ifdef YES\n' + O('t')
            + b'else\n' + O('v') + b'endif\n' + ORDER_TAIL + END)
    add('c31', P, {'@': HEAD + b'define YES\ncopy "@-t"\n' + END,
                   '@-t': cond},
        'a define in the main file decides an ifdef in the copied file')
    add('c32', P, {'@': HEAD + b'copy "@-t"\n' + END, '@-t': cond},
        'control for c31: no define')
    return out


def generate():
    """Every case, in a fixed order. Ids are unique."""
    cases = _cases()
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate case ids')
    return cases


def corpus_digest(cases=None):
    """sha256 over every case: its id, main name, files and markers."""
    cases = generate() if cases is None else cases
    h = hashlib.sha256(b'reader_oracle_cases/1\n')
    for c in cases:
        h.update(json.dumps([c.id, c.family, c.main, sorted(c.markers.items())],
                            sort_keys=True).encode('ascii'))
        for name in sorted(c.files):
            h.update(name.encode('ascii') + b'\0')
            h.update(hashlib.sha256(c.files[name]).digest())
    return h.hexdigest()
