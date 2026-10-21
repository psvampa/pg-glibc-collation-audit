"""The cases localedef answers for scripts/glibc_reader.py (backlog 15.1).

Each case is one `localedef -i <main> -f <charmap>` run, UTF-8 unless the
case names another, on a small locale built for one question about how
glibc reads a file: its bytes, its comment and escape directives, its
tokens, where its sections end, what its `copy` statements load, and how
collate_read reads LC_COLLATE statements. measure.py runs every case on a
RHEL machine and records what localedef did; tests/test_glibc_reader.py
holds the reader to it.

What a case observes: the exit status, whether the time limit stopped
localedef, the messages that name an outcome (a file it cannot open, a
circular copy, a file loaded a second time, "giving up"), and the
LC_COLLATE file written -- the 92 bytes glibc writes when it stored no
LC_COLLATE, or a table whose bytes hold the `from` string of every
collating element that glibc both defined and placed in the order. Most
cases define such marker elements, `<mk-X>` with a four-byte `from` string
that is in no other part of the file, at the places whose reading is the
question. A case can name a marker's bytes itself, as a Latin-1 string,
when the question is which bytes glibc made of the `from` string.

The files are self-contained: every name a case means to be found starts
with `rd-`, which no locale glibc ships does, so no shipped locale can
answer for one. Nothing here may change once recorded without measuring
again: corpus_digest() is in every recorded file and the test compares it.

Standard library only, Python 3.6.
"""
import hashlib
import json

FAMILIES = ('bytes', 'directives', 'tokens', 'sections', 'copy',
            'statements')


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
                 note='', charmap='UTF-8'):
        self.id = cid
        self.family = family
        self.main = main or 'rd-' + cid
        self.charmap = charmap
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
        if isinstance(markers, dict):
            self.markers = dict(markers)
        else:
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
    # 0xFF inside an identifier: lr_getc ends the word there before 2.36
    # (signed char, linereader.h:138@2.34), so ifdef asks for YES, which
    # is defined; from 2.36 the word is YES and the byte. False-negative-
    # reviewer, 15.2 B3.
    add('b25', B, HEAD + b'define YES\n' + D('t') + D('v') + ORDER_HEAD
        + b'ifdef YES\xff\n' + O('t') + b'else\n' + O('v') + b'endif\n'
        + ORDER_TAIL + END, '0xFF at the end of an ifdef name')
    # A word that opens with a byte above 0x7F: from 2.36 lr_token reads a
    # UTF-8 character there (linereader.c:332-341@2.39), before it a name.
    # So `define` and `ifdef` read the same name before 2.36 and a
    # character after it. False-negative-reviewer, round 2 of 15.2 B3.
    add('b26', B, HEAD + b'define \xc3\xa9X\n' + D('t') + D('v') + ORDER_HEAD
        + b'ifdef \xc3\xa9X\n' + O('t') + b'else\n' + O('v') + b'endif\n'
        + ORDER_TAIL + END, 'define and ifdef of a name that opens with UTF-8')
    # b26 with each branch a whole order, under declared comment and escape
    # characters: the shape of file v in test_git_helpers'
    # ALineAboveLcCollateThatStopsGlibc (round 3 of 15.2 B3).
    add('b27', B, b'comment_char %\nescape_char /\n' + HEAD
        + b'define \xc3\xa9X\n' + D('t') + D('v') + b'ifdef \xc3\xa9X\n'
        + ORDER_HEAD + O('t') + ORDER_TAIL + b'else\n' + ORDER_HEAD + O('v')
        + ORDER_TAIL + b'endif\n' + END,
        'define and ifdef of a UTF-8 name, a whole order in each branch')
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
    # A directive in a section's text after a `copy` that returns (the
    # copied file has no such category): the file's own run reads it at
    # the top level, a run that reads the file for LC_COLLATE alone skips
    # the section and never sees it. The escape decides whether `define Y/`
    # and the next line are one `define YES`, and the ifdef picks t or v.
    # False-negative-reviewer, 15.2 B3.
    ret = (b'LC_CTYPE\ncopy "@-x"\nescape_char /\nEND LC_CTYPE\n' + HEAD
           + b'define Y/\nES\n' + D('t') + D('v') + ORDER_HEAD
           + b'ifdef YES\n' + O('t') + b'else\n' + O('v') + b'endif\n'
           + ORDER_TAIL + END)
    add('d25', C, {'@': HEAD + b'copy "@-f"\n' + END, '@-f': ret,
                   '@-x': IDENT},
        'escape_char after a returning LC_CTYPE copy, read through a copy '
        'of LC_COLLATE')
    add('d26', C, {'@': ret, '@-x': IDENT},
        'control for d25: the same file read as the main file')
    add('d27', C, {'@': b'LC_CTYPE\ncopy "@-x"\nEND LC_CTYPE\n'
                   + coll('a'), '@-x': IDENT},
        'an LC_CTYPE whose copy returns, and LC_COLLATE after it')

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
    # A keyword continued across an escaped newline: lr_next drops the
    # escape and the newline (linereader.c:162-171@2.39) and lr_getc reads
    # the next line on into the same word (linereader.h:125-138), so where the table has codepoint_collation (2.35, and RHEL 9 by a patch)
    # it is read and no element is stored. glibc_tree.across matched the
    # word in the file's text and missed this spelling (false-negative-
    # reviewer, round 3 of 15.2 B3); t29 is the control.
    cp = D('a') + ORDER_HEAD + O('a') + ORDER_TAIL + END
    add('t27', T, HEAD + b'codepoint_coll\\\nation\n' + cp,
        'codepoint_collation continued across the escape')
    add('t28', T, b'escape_char /\n' + HEAD + b'codepoint_coll/\nation\n' + cp,
        'codepoint_collation continued across a declared escape')
    add('t29', T, HEAD + b'codepoint_collation\n' + cp,
        'control for t27 and t28: the keyword on one line')

    # get_ident keeps a NUL byte in a word, and gperf's lookup takes a
    # keyword, a NUL and a tail that hashes to the keyword's slot up to its
    # last test, s[len] == '\0', which reads past the keyword's literal
    # (locfile-kw.h:614@2.39, :620@2.28): what it reads is the binary's.
    # ifdef<NUL>xxdm and undef<NUL>xxdm reach that test at 2.28, 2.34 and
    # 2.39 (the reader leaves them unsettled; backlog 13.39).
    add('t30', T, HEAD + D('t') + D('v') + ORDER_HEAD
        + b'ifdef\x00xxdm RD_NOPE\n' + O('t') + b'else\n' + O('v')
        + b'endif\n' + ORDER_TAIL + END,
        'ifdef, a NUL and a tail hashed to ifdef\'s slot')
    add('t31', T, HEAD + D('t') + b'define RD_X\nundef\x00xxdm RD_X\n'
        + D('v') + ORDER_HEAD + b'ifdef RD_X\n' + O('t') + b'else\n'
        + O('v') + b'endif\n' + ORDER_TAIL + END,
        'undef, a NUL and a tail hashed to undef\'s slot')
    # The same with tails whose s[len] is the NUL after the next literal in
    # the three fixtures' localedef (read in the binaries, then measured):
    # glibc reads ifdef and elifdef there. t30 and t31 are their controls.
    add('t32', T, HEAD + D('t') + D('v') + ORDER_HEAD
        + b'ifdef\x00xxixxxxxxxxxxo RD_NOPE\n' + O('t') + b'else\n'
        + O('v') + b'endif\n' + ORDER_TAIL + END,
        'ifdef, a NUL and a tail the fixtures take for ifdef')
    add('t33', T, HEAD + D('t') + b'define RD_Y\n' + D('u') + D('v')
        + ORDER_HEAD + b'ifdef RD_NOPE\n' + O('t')
        + b'elifdef\x00ixxxxxxxxxxxxo RD_Y\n' + O('u') + b'else\n'
        + O('v') + b'endif\n' + ORDER_TAIL + END,
        'elifdef, a NUL and a tail the fixtures take for elifdef')

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
    # get_ident reads lr->buf[-1] when lr_ungetn cannot step back over a
    # `-` that opens a continued line (linereader.c:329, :578@2.39).
    add('s52', S, {'@': IDENT + HEAD + b'define \\\n-y\nifdef \\\n-y \\\n\n'
                   b'copy "@-t"\nendif\n' + body('a') + END,
                   '@-t': coll('b')},
        'a define and an ifdef whose names open a continued line with `-`')
    # Before 2.36 an 0xFF reads as EOF through a signed char and IS
    # consumed. After END, an EOF is `break`, not `return`: the section
    # reads on (ld-collate.c:3772-3773@2.34, ld-numeric.c:320-321,
    # ld-ctype.c:2823-2824); skip_category reads one more token after it
    # and drops the rest of that line (ld-collate.c:2611-2624@2.34).
    # False-negative-reviewer, round 2 of 15.2 B2.
    add('s53', S, HEAD + D('a') + D('b') + b'END \xff\n' + ORDER_HEAD
        + O('a') + O('b') + ORDER_TAIL + END,
        'END then 0xFF in LC_COLLATE, the order after it')
    add('s54', S, HEAD + b'codepoint_collation\nEND \xff\n' + D('a')
        + ORDER_HEAD + O('a') + ORDER_TAIL + END,
        'codepoint_collation, END then 0xFF, an order after it')
    add('s55', S, IDENT + HEAD + b'copy\n\xff\nLC_COLLATE\n' + body('a')
        + END, 'skip_category of LC_COLLATE meeting 0xFF, then LC_COLLATE')
    add('s56', S, b'LC_NUMERIC\ndecimal_point "."\nEND \xff\n' + coll('a'),
        'END then 0xFF in LC_NUMERIC, LC_COLLATE after it')
    add('s57', S, b'LC_CTYPE\ncopy\n\xff\n' + coll('a'),
        'skip_category of LC_CTYPE meeting 0xFF, then LC_COLLATE')
    add('s58', S, HEAD + D('a') + D('b') + b'END \x1a\n' + ORDER_HEAD
        + O('a') + O('b') + ORDER_TAIL + END,
        'control for s53: 0x1A, which is not consumed')
    # The same EOF with more on its line: skip_category's extra token is
    # that word, and handle_copy drops the rest of the line after END; and
    # an END that ends a skipped conditional branch is the END case itself
    # (seen_end, ld-collate.c:3741@2.34).
    add('s59', S, IDENT + HEAD + b'copy\n\xff LC_COLLATE\n' + body('a')
        + END, 'skip_category of LC_COLLATE meeting 0xFF, LC_COLLATE after '
        'it on the same line')
    add('s60', S, {'@': b'LC_NUMERIC\ncopy "@-t"\nEND \xff LC_COLLATE\n'
                   + body('a') + END,
                   '@-t': b'LC_NUMERIC\ndecimal_point "."\nEND LC_NUMERIC\n'},
        'a copy in LC_NUMERIC, END then 0xFF, LC_COLLATE after it on the '
        'same line')
    add('s61', S, HEAD + D('a') + D('b') + b'ifdef NOPE\nEND \xff\n'
        + ORDER_HEAD + O('a') + O('b') + ORDER_TAIL + END,
        'END then 0xFF inside a skipped ifdef branch, the order after it')
    # An 0xFF inside a statement: many handlers leave the rest of their
    # line or stop at that EOF without lr_ignore_rest, and the statement
    # loop reads on from the byte after it (ld-ctype.c:2789@2.34,
    # ld-numeric.c:202-239, ld-collate.c:733-737 and :918-933). And
    # translit_start's loops, which end at EOF and `break`
    # (ld-ctype.c:2637-2643, :2782-2787). False-negative-reviewer, round 3
    # of 15.2 B2.
    cl = HEAD + b'copy "@-t"\n' + END
    add('s62', S, {'@': b'LC_CTYPE\nfoo \xff\n' + cl + b'END LC_CTYPE\n',
                   '@-t': coll('a')},
        'an unknown word then 0xFF in LC_CTYPE, LC_COLLATE after it')
    add('s63', S, {'@': b'LC_CTYPE\nfoo\n' + cl + b'END LC_CTYPE\n',
                   '@-t': coll('a')},
        'control for s62: no 0xFF, LC_CTYPE swallows LC_COLLATE')
    add('s64', S, {'@': b'LC_NUMERIC\ndecimal_point "." \xff\n' + cl
                   + b'END LC_NUMERIC\n', '@-t': coll('a')},
        'decimal_point then 0xFF in LC_NUMERIC, LC_COLLATE after it')
    add('s65', S, HEAD + D('a') + D('b') + ORDER_HEAD
        + b'<U0063> \xff END LC_COLLATE\n' + O('a') + O('b') + ORDER_TAIL
        + END, 'an order line, 0xFF, then END LC_COLLATE on the same line')
    add('s66', S, b'LC_CTYPE\nclass \xff END LC_CTYPE\n' + coll('a'),
        'class then 0xFF then END LC_CTYPE on the same line')
    add('s67', S, {'@': b'LC_CTYPE\ntranslit_start\n\xff\nLC_COLLATE\n'
                   b'codepoint_collation\nEND LC_CTYPE\n' + cl,
                   '@-t': coll('a')},
        'translit_start then 0xFF; LC_COLLATE words inside LC_CTYPE')
    add('s68', S, {'@': b'LC_CTYPE\ntranslit_start\n\xff\nLC_COLLATE\n'
                   b'copy "@-t"\nEND LC_CTYPE\nEND LC_COLLATE\n',
                   '@-t': coll('a')},
        'translit_start then 0xFF; a copy inside LC_CTYPE')
    add('s69', S, {'@': HEAD + b'copy "@-t"\n' + END,
                   '@-t': b'LC_CTYPE\ntranslit_start\n\xff\n' + coll('a')
                   + b'END LC_CTYPE\n' + coll('b')},
        'translit_start then 0xFF in an LC_CTYPE read for LC_COLLATE only')
    # The escape that ends the section above swallows an indented
    # LC_COLLATE line (b11), and a second section follows: one section is
    # read, and it is the second. Step 2 compared how many sections it read
    # (round 4 of the 15.2 B3 review).
    add('s70', S, b'comment_char %\nescape_char /\nLC_IDENTIFICATION\n'
        b'title "t"\nEND LC_IDENTIFICATION % a /\n ' + coll('a') + b' '
        + coll('b'), 'a swallowed LC_COLLATE line, then a second section')
    # An order line that ends in the escape joins the line below it (round
    # 1 of the 13.36 review): in a branch not taken, skip_to's
    # lr_ignore_rest takes the endif with it and mk-b is skipped; in the
    # order, it takes the order_end below it, and mk-b, after it, is still
    # placed. Measured on the three builds on 2026-10-16 and not kept: the
    # same join in a reorder block placed mk-b after mk-a where the control
    # drops it, and one taking `<mk-b>` as mk-a's weight crashed localedef
    # -c (signal 11, "symbol `mk-b' not defined"), a weight the reader does
    # not model.
    esc = b'comment_char %\nescape_char /\n'
    skip = (esc + HEAD + D('a') + D('b') + ORDER_HEAD + b'ifdef RD_NOPE\n'
            + b'<mk-a> /\nendif\n' + O('b') + ORDER_TAIL + END)
    add('s71', S, skip, 'an order line ending in the escape, in an ifdef '
        'branch not taken, joins the endif below it')
    add('s72', S, skip.replace(b'<mk-a> /\n', b'<mk-a>\n'),
        'control for s71: no escape')
    joined = (esc + HEAD + D('a') + D('b') + ORDER_HEAD + b'<mk-a> /\n'
              + b'order_end\n' + O('b') + END)
    add('s73', S, joined, 'an order line ending in the escape joins the '
        'order_end below it')
    add('s74', S, joined.replace(b'<mk-a> /\n', b'<mk-a>\n'),
        'control for s73: no escape')
    cond =(HEAD + D('t') + D('v') + ORDER_HEAD + b'ifdef YES\n' + O('t')
            + b'else\n' + O('v') + b'endif\n' + ORDER_TAIL + END)
    add('c31', P, {'@': HEAD + b'define YES\ncopy "@-t"\n' + END,
                   '@-t': cond},
        'a define in the main file decides an ifdef in the copied file')
    add('c32', P, {'@': HEAD + b'copy "@-t"\n' + END, '@-t': cond},
        'control for c31: no define')

    # -- statements: what collate_read accepts in each state (backlog 15.3)
    # A statement whose handler tests collate_read's state goes to
    # err_label in any other state, and the state stays as it was
    # (ld-collate.c:2697-3738, :3958-3960@2.34). Markers stand after the
    # statement glibc refuses, with a control where it is accepted.
    O_ = 'statements'
    ab = HEAD + D('a') + D('b') + ORDER_HEAD + O('a') + ORDER_TAIL
    after = b'reorder-after <U0061>\n'
    add('o01', O_, ab + after + b'<U0063>\nreorder-end\norder_start forward\n'
        + O('b') + b'order_end\n' + END,
        'after reorder-end, order_start and the entry after it are refused')
    add('o02', O_, ab + b'order_start forward\n' + O('b') + b'order_end\n'
        + END, 'control for o01: a second order_start after order_end')
    add('o03', O_, ab + after + O('b') + b'reorder-end\n' + END,
        'control for o04 to o06: an entry in a reorder block')
    add('o04', O_, ab + after + b'<U0063>\nreorder-end\n' + O('b') + END,
        'an entry after reorder-end is refused')
    add('o05', O_, ab + after + b'<U0063>\norder_end\n' + O('b')
        + b'reorder-end\n' + END,
        'order_end in a reorder block is refused; the block goes on')
    add('o06', O_, ab + after + b'order_start forward\n' + O('b')
        + b'reorder-end\n' + END,
        'order_start in a reorder block is refused; the block goes on')
    add('o07', O_, HEAD + D('a') + ORDER_HEAD + O('a') + D('b') + O('b')
        + ORDER_TAIL + END,
        'collating-element between order_start and order_end is refused')
    add('o08', O_, HEAD + D('a') + ORDER_HEAD + O('a') + ORDER_TAIL + D('b')
        + after + O('b') + b'reorder-end\n' + END,
        'control for o07 and o09: collating-element after order_end')
    add('o09', O_, HEAD + D('a') + ORDER_HEAD + O('a') + ORDER_TAIL + after
        + D('b') + O('b') + b'reorder-end\n' + END,
        'collating-element in a reorder block is refused')
    add('o10', O_, HEAD + D('a') + b'reorder-after <U0062>\n' + ORDER_HEAD
        + O('a') + ORDER_TAIL + END,
        'reorder-after before order_start, with no opening copy, is refused')
    add('o11', O_, {'@': HEAD + b'copy "@-t"\n' + D('a') + after + O('a')
                    + b'reorder-end\n' + END, '@-t': coll('t')},
        'control for o10: reorder-after after an opening copy')
    add('o12', O_, HEAD + D('a') + ORDER_HEAD + O('a') + ORDER_TAIL
        + b'..\n<U0045>\n' + END,
        'a range after order_end is refused, and the entry after it')
    add('o13', O_, HEAD + D('a') + O('a') + ORDER_HEAD + ORDER_TAIL + END,
        'an entry before order_start that names no collating-symbol')
    add('o14', O_, HEAD + D('a') + b'order_start forward\n<U0061>\n..\n'
        + after + O('a') + b'reorder-end\n' + END,
        'reorder-after with a range open: it closes the range with a stale '
        'argument')
    add('o15', O_, ab + after + b'<U0063>\nreorder-sections-after <x>\n'
        b'reorder-sections-end\n' + END,
        'reorder-sections-after in a reorder block: a counted error')
    add('o16', O_, ab + after + O('b') + END,
        'END in a reorder block: a counted error')
    add('o17', O_, HEAD + D('a') + D('b') + D('c') + ORDER_HEAD + O('a')
        + ORDER_TAIL + after + O('b') + b'reorder-after <U9999>\n' + O('c')
        + b'reorder-end\n' + END,
        'a second reorder-after in the block, whose anchor is not there')
    add('o18', O_, ab + after + b'<U0063>\nreorder-end\n' + after + O('b')
        + b'reorder-end\n' + END,
        'a second reorder block after reorder-end is refused')
    add('o19', O_, HEAD + D('a') + b'order_start forward\n<U0041>\n..\n'
        b'<U0045>\n' + O('a') + ORDER_TAIL + b'reorder-after <U0041>\n'
        b'<U0063>\nreorder-end\n<U0045>\n' + END,
        'the end of a range, again on a line glibc refuses')
    add('o36', O_, ab + after + O('b'),
        'control for o16: the file ends in a reorder block, with no END')
    # Round 1 of the false-negative review of 15.3 C1: the END case's
    # checks run at an END the section reads on after (an 0xFF before
    # 2.36), and at one a skipped branch meets; and a symbol read while no
    # statement has read an argument leaves the rest of its line.
    add('o37', O_, ab + after + O('b') + b'END \xff\nreorder-end\n' + END,
        'END then 0xFF in a reorder block, reorder-end after it')
    add('o38', O_, ab + after + O('b') + b'ifdef RD_NOPE\nEND \xff\n'
        b'endif\nreorder-end\n' + END,
        'END then 0xFF in a skipped branch of a reorder block')
    add('o39', O_, HEAD + D('a') + b'order_start forward\n<U0041>\n..\n'
        b'END \xff\n<U0045>\n' + O('a') + ORDER_TAIL + END,
        'END then 0xFF with a range open')
    add('o40', O_, HEAD + b'<rd-x> order_start forward\n<U0062>\n<U0061>\n'
        b'order_end\n' + D('a') + after + O('a') + b'reorder-end\n' + END,
        'a symbol first in the section, order_start after it on its line')
    add('o41', O_, HEAD + b'<rd-x>\norder_start forward\n<U0062>\n<U0061>\n'
        b'order_end\n' + D('a') + after + O('a') + b'reorder-end\n' + END,
        'control for o40: order_start on a line of its own')
    add('o42', O_, HEAD + b'define rdX\n<rd-x> order_start forward\n'
        b'<U0062>\n<U0061>\norder_end\n' + D('a') + after + O('a')
        + b'reorder-end\n' + END,
        'control for o40: a define before the symbol')
    # Round 2 of the same review: in a section read for another category,
    # reorder-end and reorder-sections-end leave the rest of their line to
    # be read as statements. s33 is the ellipsis alone on its line.
    for cid, line, note in (
            ('o43', b'reorder-end ..', 'reorder-end then an ellipsis'),
            ('o44', b'reorder-sections-end ..',
             'reorder-sections-end then an ellipsis'),
            ('o45', b'reorder-end', 'control for o43: reorder-end alone')):
        add(cid, O_, {'@': qt, '@-x': HEAD + line + b'\n' + END + TIME7},
            note + ', in a file read for LC_TIME')
    add('o46', O_, ab + after + O('b') + b'reorder-end ..\n' + END,
        'control for o43: reorder-end then an ellipsis, in a section read '
        'for itself, where glibc drops the rest of the line')
    # Round 3 of the same review: symbol-equivalence leaves the rest of its
    # line to be read as statements when its second name is no declared
    # collating-symbol or its new name is defined already
    # (ld-collate.c:3023-3038@2.34), and drops it on success (:3042).
    se = b'symbol-equivalence <rd-q> '
    ab0 = HEAD + D('a') + D('b')
    tail = O('a') + O('b') + ORDER_TAIL + END
    add('o47', O_, ab0 + se + b'<rd-nope> ' + ORDER_HEAD + tail,
        'symbol-equivalence to an undeclared symbol, order_start after it')
    add('o48', O_, ab0 + se + b'<rd-nope>\n' + ORDER_HEAD + tail,
        'control for o47: order_start on a line of its own')
    add('o49', O_, ab0 + se + b'<rd-nope> codepoint_collation\n'
        + ORDER_HEAD + tail,
        'symbol-equivalence to an undeclared symbol, codepoint_collation '
        'after it')
    add('o50', O_, ab0 + se + b'<rd-nope> ..\n' + ORDER_HEAD + tail,
        'symbol-equivalence to an undeclared symbol, a range after it')
    sym = b'collating-symbol <rd-s>\n'
    add('o51', O_, ab0 + sym + se + b'<rd-s>\n' + se + b'<rd-s> '
        + ORDER_HEAD + tail,
        'symbol-equivalence of a name defined already, order_start after it')
    add('o52', O_, ab0 + sym + se + b'<rd-s> ' + ORDER_HEAD + tail,
        'control for o51: a symbol-equivalence that succeeds drops the '
        'order_start after it')
    # Round 4 of the same review, the exits of collate_read that leave a
    # line unfinished: a reorder-after closing a range with a stale
    # argument (ld-collate.c:3264-3271@2.34), an 0xFF read as EOF inside
    # order_start's directions and inside a weight, a range in a reorder
    # block, and order_start with a range pending.
    add('o53', O_, ab0 + ORDER_HEAD + b'...\n' + after + O('a') + O('b')
        + b'reorder-end\n' + END,
        'reorder-after with an absolute range open')
    add('o54', O_, ab0 + ORDER_HEAD + b'order_end\n' + after + O('a')
        + O('b') + b'reorder-end\n' + END,
        'control for o53: order_end before the reorder block')
    add('o55', O_, ab0 + b'order_start forward \xff <mk-a>\n<U0062>\n'
        b'<U0061>\n' + O('b') + ORDER_TAIL + END,
        'order_start, then 0xFF, then an entry on its line')
    add('o56', O_, ab0 + ORDER_HEAD + b'<U0063> <U0063>\xff <mk-a>\n'
        + O('b') + ORDER_TAIL + END,
        'a weight ending in 0xFF, then an entry on its line')
    add('o57', O_, ab0 + ORDER_HEAD + O('a') + O('b') + b'order_end\n'
        + after + b'...\n<U0062>\nreorder-end\n' + END,
        'an absolute range in a reorder block')
    add('o58', O_, ab0 + ORDER_HEAD + b'...\norder_start forward\n' + O('a')
        + O('b') + ORDER_TAIL + END,
        'order_start with an absolute range pending')
    # The cases of the 13.36 review that collate_read's state decides,
    # measured again for backlog 15.3: no markers, an outcome each.
    for cid, files, note in (
            ('o20', {'@': HEAD + b'order_start forward\n<U0041>\n..\n'
                     b'copy "@-b"\n<U0045>\norder_end\n' + END,
                     '@-b': HEAD + b'order_start forward\n<U0061>\n<U0062>\n'
                     b'order_end\n' + END},
             'a copy in the middle of a range'),
            ('o21', {'@': HEAD + b'order_start forward\n<U0041>\n'
                     b'copy "@-b"\n..\n<U0045>\norder_end\n' + END,
                     '@-b': HEAD + b'order_start forward\n<U0061>\n'
                     b'order_end\nreorder-after <U9999>\n<U0063>\n'
                     b'reorder-end\n' + END},
             'a range after a copy whose reorder-after names nothing'),
            ('o22', {'@': HEAD + b'order_start forward\n<U0041>\norder_end\n'
                     b'reorder-after <U0041>\n<U0042>\norder_start forward\n'
                     b'<U0043>\n..\n<U0045>\n' + END},
             'order_start in a reorder block, then a range; END in the '
             'block'),
            ('o23', {'@': HEAD + b'<foo>\norder_start forward\n..\n<U0045>\n'
                     b'order_end\n' + END},
             'a symbol before order_start, a range right after it'),
            ('o24', {'@': HEAD + b'order_start forward\n..\n<U0045>\n'
                     b'order_end\n' + END},
             'a range right after order_start'),
            ('o25', {'@': HEAD + b'copy "@-y"\norder_start forward\n..\n'
                     b'<U0045>\norder_end\n' + END,
                     '@-y': HEAD + b'order_start forward\n<U0041>\n<U0042>\n'
                     b'<U0043>\norder_end\nreorder-after <U0041>\n'
                     b'reorder-end\n' + END},
             'a range right after order_start, after an opening copy'),
            ('o26', {'@': HEAD + b'order_start forward\n<U00010000>\n...\n'
                     b'<U00010010>\n<U00010020>\n<U00010005>\n<U00010006>\n'
                     b'...\n<U00010030>\norder_end\n' + END},
             'two absolute ranges, the second after a character the first '
             'filled'),
            ('o27', {'@': HEAD + b'order_start forward\n<U00010000>\n...\n'
                     b'<U00010010>\n<U00010028>\n<U00010005>\n<U00010006>\n'
                     b'...\n<U00010030>\norder_end\n' + END},
             'two absolute ranges, the second after a character the first '
             'filled'),
            ('o28', {'@': HEAD + b'copy "@-b"\norder_start forward\n'
                     b'<U0041>\n..\n<U0045>\n<U0031>\norder_end\n' + END,
                     '@-b': HEAD + b'order_start forward\n<U0045>\n<U0030>\n'
                     b'<U0032>\norder_end\n' + END},
             'a range whose end the opening copy placed'),
            ('o29', {'@': HEAD + b'copy "@-b"\norder_start forward\n'
                     b'<U0041>\n..\n<U0045>\n<U0031>\norder_end\n' + END,
                     '@-b': HEAD + b'order_start forward\n<U0030>\n<U0032>\n'
                     b'order_end\n' + END},
             'a range after an opening copy'),
            ('o30', {'@': HEAD + b'order_start forward\n<U0041>\norder_end\n'
                     b'reorder-after <U0041>\n<U0042>\nreorder-end\n'
                     b'order_start forward\n<U0043>\n..\n<U0045>\norder_end\n'
                     + END},
             'a range in an order_start after reorder-end'),
            ('o31', {'@': HEAD + b'order_start forward\n<U0041>\norder_end\n'
                     b'reorder-after <U0041>\n<U0042>\norder_end\n'
                     b'order_start forward\n<U0043>\n..\n<U0045>\norder_end\n'
                     + END},
             'order_end in a reorder block, then a range; END in the block')):
        add(cid, O_, files, note)
    # The `from` string of a collating-element is read translated
    # (ld-collate.c:2786-2793@2.34). A raw byte above 0x7F is copied as it
    # is before 2.36 and decoded as UTF-8 and looked up in the charmap from
    # 2.36 (backlog 13.23). The marker is the bytes the table holds when
    # the string is read as <U00E9> and the marker text, in that charmap.
    raw = b'"\xc3\xa9Zqew"'
    named = b'"<U00E9>' + _ucs(b'Zqew') + b'"'
    for cid, string, charmap, text, note in (
            ('o32', raw, 'UTF-8', '\xc3\xa9Zqew',
             'a raw UTF-8 character in a `from` string, under UTF-8'),
            ('o33', named, 'UTF-8', '\xc3\xa9Zqew', 'control for o32'),
            ('o34', raw, 'ISO-8859-1', '\xe9Zqew',
             'a raw UTF-8 character in a `from` string, under ISO-8859-1'),
            ('o35', named, 'ISO-8859-1', '\xe9Zqew', 'control for o34')):
        add(cid, O_, HEAD + b'collating-element <mk-e> from ' + string + b'\n'
            + ORDER_HEAD + O('e') + ORDER_TAIL + END, note,
            markers={'e': text}, charmap=charmap)

    # define, undef, ifdef, elifdef and elifndef (ld-collate.c:3804-3978
    # @2.39, the same text at 2.28 and 2.34; the prologue's define at
    # :2592-2614). glibc keeps a name with its NUL bytes and one NUL after
    # it, and takes a name for a defined one when strncmp (arg, cur,
    # lenmb) == 0 && cur[lenmb] == '\0' (:3841-3843, :3882-3884): C
    # strings, so a NUL ends the comparison (backlog 13.39). Where
    # cur[lenmb] is past that NUL, C leaves the answer undefined. ifndef
    # is not in the keyword table (locfile-kw.gperf:75-81), so glibc runs
    # its branch only through elifndef (backlog 13.43 b).
    def tv(kw, name):
        return (kw + b' ' + name + b'\n' + O('t') + b'else\n' + O('v')
                + b'endif\n')

    def tuv(second, name, first=b'ifdef RD_NOPE'):
        return (first + b'\n' + O('t') + second + b' ' + name + b'\n'
                + O('u') + b'else\n' + O('v') + b'endif\n')

    def pro(pre, mid, ms='tv'):
        # pre right after LC_COLLATE: the prologue's define loop
        return (HEAD + pre + b''.join(D(m) for m in ms) + ORDER_HEAD + mid
                + ORDER_TAIL + END)

    def bod(pre, mid, ms='tv'):
        # pre after the first collating-element: read by the body
        return (HEAD + D(ms[0]) + pre + b''.join(D(m) for m in ms[1:])
                + ORDER_HEAD + mid + ORDER_TAIL + END)

    N = b'\x00'
    for cid, files, note in (
            ('o59', pro(b'define A' + N + b'B\n', tv(b'ifdef', b'A')),
             'prologue define A<NUL>B, ifdef A'),
            ('o60', pro(b'define A' + N + b'B\n',
                        tv(b'ifdef', b'A' + N + b'C')),
             'prologue define A<NUL>B, ifdef A<NUL>C'),
            ('o61', pro(b'define A' + N + b'BX\n',
                        tv(b'ifdef', b'A' + N + b'C')),
             'control for o60: define A<NUL>BX, ifdef A<NUL>C'),
            ('o62', pro(b'define A' + N + b'B\nundef A\n',
                        tv(b'ifdef', b'A' + N + b'B')),
             'define A<NUL>B, undef A, ifdef A<NUL>B'),
            ('o63', bod(b'define A' + N + b'B\n', tv(b'ifdef', b'A')),
             'body define A<NUL>B, ifdef A'),
            ('o64', bod(b'define A' + N + b'BC\nundef A' + N + b'Q' + N
                        + b'\n', tv(b'ifdef', b'A' + N + b'BC')),
             'undef A<NUL>Q<NUL> removes A<NUL>BC'),
            ('o65', bod(b'define A' + N + b'B\n', tuv(b'elifdef', b'A'),
                        'tuv'),
             'elifdef A after define A<NUL>B'),
            ('o66', bod(b'define A' + N + b'B\n', tuv(b'elifndef', b'A'),
                        'tuv'),
             'elifndef A after define A<NUL>B'),
            ('o67', {'@': HEAD + b'define A' + N + b'B\ncopy "@-t"\n' + END,
                     '@-t': HEAD + D('t') + D('v') + ORDER_HEAD
                     + tv(b'ifdef', b'A') + ORDER_TAIL + END},
             'define A<NUL>B before the opening copy, ifdef A in the copy'),
            ('o68', bod(b'define ' + N + b'A\n', tv(b'ifdef', N + b'B')),
             'a name opening with NUL: define <NUL>A, ifdef <NUL>B'),
            ('o69', bod(b'define A' + N + b'B\n', tv(b'ifdef', b'A' + N)),
             'control: ifdef A<NUL> after define A<NUL>B'),
            ('o70', bod(b'define X\nundef X' + N + b'\n', tv(b'ifdef', b'X')),
             'undef X<NUL> after define X: past the stored NUL'),
            ('o71', bod(b'define X\n', tv(b'ifdef', b'X' + N)),
             'ifdef X<NUL> after define X: past the stored NUL'),
            ('o72', bod(b'define X\ndefine X' + N + N + b'\n',
                        tv(b'ifdef', b'X' + N)),
             'ifdef X<NUL>: the newer X<NUL><NUL> matches first'),
            ('o73', bod(b'', tv(b'ifndef', b'RD_X')),
             'ifndef is not a keyword'),
            ('o74', bod(b'define RD_Y\n', tuv(b'elifdef', b'RD_Y'), 'tuv'),
             'elifdef taken'),
            ('o75', bod(b'', tuv(b'elifdef', b'RD_Y'), 'tuv'),
             'elifdef not taken'),
            ('o76', bod(b'', tuv(b'elifndef', b'RD_Y'), 'tuv'),
             'elifndef taken'),
            ('o77', bod(b'define RD_Y\n', tuv(b'elifndef', b'RD_Y'), 'tuv'),
             'elifndef not taken'),
            ('o78', bod(b'define RD_X\nundef RD_X\n', tv(b'ifdef', b'RD_X')),
             'undef removes a define'),
            ('o79', bod(b'define RD_X\ndefine RD_X\nundef RD_X\n',
                        tv(b'ifdef', b'RD_X')),
             'undef removes every copy'),
            ('o80', bod(b'define RD_X\n',
                        tuv(b'elifdef', b'RD_X', b'ifdef RD_X'), 'tuv'),
             'elifdef after a taken ifdef is skipped'),
            ('o81', bod(b'define RD_X\ndefine RD_Y\n',
                        b'ifdef RD_NOPE\n' + O('t') + b'elifndef RD_X\n'
                        + O('u') + b'elifdef RD_Y\n' + O('w') + b'else\n'
                        + O('v') + b'endif\n', 'tuvw'),
             'elifndef not taken, then elifdef taken'),
            ('o82', bod(b'define RD_X\nifdef RD_NOPE\nundef RD_X\nendif\n',
                        tv(b'ifdef', b'RD_X')),
             'an undef in a skipped branch'),
            ('o83', bod(b'define RD_X\n', tv(b'ifdef', b'RD_X')),
             'control: ifdef of a defined name')):
        add(cid, O_, files, note)
    return out


def generate():
    """Every case, in a fixed order. Ids are unique."""
    cases = _cases()
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate case ids')
    return cases


def corpus_digest(cases=None):
    """sha256 over every case: its id, family, main name, markers,
    charmap and files."""
    cases = generate() if cases is None else cases
    h = hashlib.sha256(b'reader_oracle_cases/1\n')
    for c in cases:
        h.update(json.dumps([c.id, c.family, c.main, sorted(c.markers.items()),
                             c.charmap], sort_keys=True).encode('ascii'))
        for name in sorted(c.files):
            h.update(name.encode('ascii') + b'\0')
            h.update(hashlib.sha256(c.files[name]).digest())
    return h.hexdigest()
