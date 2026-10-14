"""scripts/glibc_reader.py, a reader of glibc's locale sources that follows
localedef, held to glibc itself. No step of the audit uses it yet.

The tool's reading of glibc's files used to be copied by hand into several
modules, and every copy read a little differently from glibc (backlog block
15). glibc_reader.py follows localedef instead, and this module holds it to
what localedef did:

  reader_oracle/rhel8.json   glibc-2.28-251.el8_10.40 (collaudit8)
  reader_oracle/rhel9.json   glibc-2.34-275.el9_8     (collaudit9)
  reader_oracle/rhel10.json  glibc-2.39-128.el10_2    (collaudit10)

are localedef's answers to the cases in reader_oracle/cases.py, recorded on
2026-10-13 with reader_oracle/measure.py on the three fixtures. The
comparison needs no machine; an answer that is missing, damaged or recorded
for other cases is a failure, never a skip. When cases.py changes, the
corpus digest stops matching and the three files have to be measured again.

The reader also says where it cannot answer (Run.unsettled): for the shapes
a later step of block 15 reads. Which cases those are
is pinned below, so a reader that stops answering anywhere else goes red,
and a step that settles one has to move it here.

Below the oracle: what a Build keeps between runs and scripts/reader_cache.py
keeps on disk give exactly what the reader computes without them (backlog
15.2), and run() agrees with the line readers the steps use today on the
LC_COLLATE bounds and copies of every locale file of the five tags, which
the cache makes cheap after the first time.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import _harness  # also puts scripts/ on sys.path
from _harness import (FLOOR_NEW, FLOOR_OLD, GLIBC_CLONE, MID, NEW, OLD,
                      needs_clone, needs_floor_pair)

import glibc_locale_data as g
import glibc_reader as gr
import reader_cache as rc

ORACLE_DIR = os.path.join(_harness.TESTS_DIR, 'reader_oracle')
if ORACLE_DIR not in sys.path:
    sys.path.insert(0, ORACLE_DIR)
import cases as oracle_cases  # noqa: E402
import measure as oracle_measure  # noqa: E402

# The 92-byte LC_COLLATE localedef writes when it stored none
# (ld-collate.c:2120-2146@2.39); measured on the three builds (backlog 15.0).
ABSENT_MD5 = 'dd8d0e53b63929a04b1c90ef6b95d684'
_OPEN_RE = re.compile(r"cannot open locale definition file `(.*)'")

# The upstream version and RHEL release each answer file must come from.
ORACLE_BUILDS = {'rhel8': ('2.28', '.el8'), 'rhel9': ('2.34', '.el9'),
                 'rhel10': ('2.39', '.el10')}

# Cases the reader leaves unsettled, each until the step of block 15 that
# reads the grammar it depends on (15.4: the grammars that read past the end
# of a line, LC_TIME's fields and translit's entries among them). b18 and
# b19 are settled on el10 because a 0x1A or 0xFF there is an ordinary byte
# from glibc 2.36 on, and so is s51, where a 0x1A after a number makes
# lr_token give the number again forever before 2.36.
# c44 and c50 wait for the charmap reader (15.6), which says whether a
# string has a character the charmap lacks; c45 and c46, like c34, for the
# translit entry grammar (15.4). s41, s43 and s45 for the LC_COLLATE and
# LC_CTYPE statement grammars (15.3, 15.4): a range before order_start,
# and statements written into a struct the file shares while read for
# another category; s42 too on el9 and el10, where codepoint_collation is a
# keyword. s46 too: a number that ends the file is read again on every
# lr_token, so whether glibc returns depends on what the statement's
# handler does with the rest of its line (15.4); s47, s48 and, before
# 2.36, s50, where the loop is glibc's own, are a hang, measured.
_LATER = {'s18', 's27', 's30', 's31', 'c34', 'c44', 'c45', 'c46', 'c50',
          's41', 's43', 's45', 's46'}
# For good: glibc reads outside its buffer there (GlibcUndefined), t16 at
# the start of the file and s52 at the start of a continued line.
_UNDEFINED = {'t16', 's52'}
EXPECTED_UNSETTLED = {
    'rhel8': {'b18', 'b19', 's51'} | _LATER | _UNDEFINED,
    'rhel9': {'b18', 'b19', 's42', 's51'} | _LATER | _UNDEFINED,
    'rhel10': {'s42'} | _LATER | _UNDEFINED,
}
def load_oracle(path):
    """The answers in `path`, checked whole: format, seal, the corpus they
    answer, and both runs of every case. Raises ValueError otherwise."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get('format') != oracle_measure.FORMAT:
        raise ValueError('%s: not a %s file' % (path, oracle_measure.FORMAT))
    if data.get('content') != oracle_measure.content_digest(data):
        raise ValueError('%s: the seal does not match the content' % path)
    if data.get('corpus') != oracle_cases.corpus_digest():
        raise ValueError('%s: recorded for other cases than cases.py makes '
                         'now; measure again' % path)
    ids = [c.id for c in oracle_cases.generate()]
    got = data.get('cases')
    if not isinstance(got, dict) or sorted(got) != sorted(ids):
        raise ValueError('%s: the cases answered are not the cases asked'
                         % path)
    for cid in ids:
        if sorted(got[cid]) != ['c', 'nc']:
            raise ValueError('%s: case %s lacks a run' % (path, cid))
    return data


def predict(case, build):
    """What the reader says localedef -c does with `case`: an outcome and,
    when LC_COLLATE was read, the markers it holds. A marker is in the
    output when the statement defining it and the order line placing it
    were both read into the output's LC_COLLATE, in that order, the order
    line between order_start and order_end; a struct with neither an
    order_start nor codepoint_collation read makes collate_finish give up
    (ld-collate.c:1849-1853@2.28)."""
    files = {k.encode('latin-1'): v for k, v in case.files.items()}
    r = gr.run(case.main.encode('latin-1'), files.get, build)
    if r.unsettled:
        return {'outcome': 'unsettled'}
    if r.fatal is not None:
        if r.fatal.kind == 'missing':
            return {'outcome': 'missing',
                    'name': r.fatal.name.decode('latin-1')}
        return {'outcome': r.fatal.kind}
    if r.collate is None:
        return {'outcome': 'absent'}
    defined, placed, state, order = set(), set(), 0, False
    for _, _, toks in r.collate.statements:
        t = toks[0]
        if t.tok == 'kw' and t.kwtok == 'tok_order_start':
            state, order = 1, True
        elif t.tok == 'kw' and t.kwtok == 'tok_codepoint_collation':
            order = True
        elif t.tok == 'kw' and t.kwtok == 'tok_order_end':
            state = 2
        elif t.tok == 'kw' and t.kwtok == 'tok_collating_element' \
                and len(toks) > 1 and toks[1].tok == 'bsymbol':
            defined.add(toks[1].val)
        elif t.tok == 'bsymbol' and state == 1 and t.val in defined:
            placed.add(t.val)
    if not order:
        return {'outcome': 'giving-up'}
    return {'outcome': 'read',
            'markers': sorted(m for m in case.markers
                              if ('mk-' + m).encode() in placed)}


def observed(run):
    """What localedef did on one run, in predict()'s terms."""
    if run['timed_out']:
        return {'outcome': 'hang'}
    rc = run['rc']
    text = '\n'.join(run['messages'])
    if rc < 0 or rc >= 128:
        return {'outcome': 'crash'}
    if run['collate'] is None:
        m = _OPEN_RE.search(text)
        if rc == 4 and m:
            return {'outcome': 'missing', 'name': m.group(1)}
        if rc == 5 and 'circular dependencies' in text:
            return {'outcome': 'circular'}
        if rc == 5 and 'a second time' in text:
            return {'outcome': 'duplicate'}
        if rc == 1 and 'giving up' in text:
            return {'outcome': 'giving-up'}
        return {'outcome': 'unrecognised', 'rc': rc}
    if run['collate']['md5'] == ABSENT_MD5:
        return {'outcome': 'absent'}
    return {'outcome': 'read', 'markers': run['collate']['markers']}


class OracleFiles(unittest.TestCase):
    """The three answer files are whole, sealed, and answer today's cases.

    Without this a file edited by hand, cut short, or recorded for an older
    cases.py would be compared as if it were localedef's answer."""

    def test_each_file_loads_whole(self):
        for name, (version, release) in sorted(ORACLE_BUILDS.items()):
            with self.subTest(name):
                data = load_oracle(os.path.join(ORACLE_DIR, name + '.json'))
                rpm = data['build']['rpm']
                self.assertEqual(len(rpm), 2, rpm)
                self.assertTrue(rpm[0].startswith('glibc-%s-' % version)
                                and release in rpm[0], rpm)
                self.assertTrue(rpm[1].startswith('glibc-common-%s-'
                                                  % version), rpm)
                # The reader takes `char` as signed for these builds.
                self.assertEqual(data['build']['machine'], 'x86_64')

    def _damaged(self, change):
        with open(os.path.join(ORACLE_DIR, 'rhel9.json')) as f:
            data = json.load(f)
        change(data)
        fd, tmp = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        self.addCleanup(os.remove, tmp)
        return tmp

    def test_a_damaged_file_is_refused(self):
        def edit(d):
            d['cases']['c01']['c']['rc'] = 0

        def edit_resealed_other_corpus(d):
            d['corpus'] = '0' * 64
            d['content'] = oracle_measure.content_digest(d)

        def drop_case(d):
            del d['cases']['c01']
            d['content'] = oracle_measure.content_digest(d)

        def drop_run(d):
            del d['cases']['c01']['nc']
            d['content'] = oracle_measure.content_digest(d)

        def other_format(d):
            d['format'] = 'reader_oracle/0'
            d['content'] = oracle_measure.content_digest(d)
        for change, words in ((edit, 'seal'),
                              (edit_resealed_other_corpus, 'other cases'),
                              (drop_case, 'not the cases asked'),
                              (drop_run, 'lacks a run'),
                              (other_format, 'not a')):
            with self.subTest(change.__name__):
                with self.assertRaisesRegex(ValueError, words):
                    load_oracle(self._damaged(change))


class _OracleCompare(object):
    """The reader against localedef on one build, case by case."""
    build_name = None

    @classmethod
    def setUpClass(cls):
        cls.build = gr.rhel_build(GLIBC_CLONE, cls.build_name)
        cls.data = load_oracle(os.path.join(ORACLE_DIR,
                                            cls.build_name + '.json'))
        cls.cases = oracle_cases.generate()

    def test_every_settled_case_agrees_with_localedef(self):
        wrong = []
        for case in self.cases:
            p = predict(case, self.build)
            if p['outcome'] == 'unsettled':
                continue
            o = observed(self.data['cases'][case.id]['c'])
            if p != o:
                wrong.append('%s (%s): reader %s, localedef %s'
                             % (case.id, case.note, p, o))
        self.assertEqual([], wrong)

    def test_the_reader_leaves_unsettled_only_the_known_cases(self):
        got = {c.id for c in self.cases
               if predict(c, self.build)['outcome'] == 'unsettled'}
        self.assertEqual(EXPECTED_UNSETTLED[self.build_name], got)

    def test_every_answer_is_one_the_comparison_recognises(self):
        # An outcome observed() cannot name would compare unequal to every
        # prediction and read as a reader defect; name it instead.
        odd = [cid for cid, runs in self.data['cases'].items()
               if observed(runs['c'])['outcome'] == 'unrecognised']
        self.assertEqual([], odd)


@needs_clone
class OracleRhel8(_OracleCompare, unittest.TestCase):
    build_name = 'rhel8'


@needs_clone
class OracleRhel9(_OracleCompare, unittest.TestCase):
    build_name = 'rhel9'


@needs_clone
class OracleRhel10(_OracleCompare, unittest.TestCase):
    build_name = 'rhel10'


@needs_clone
class ReaderFailureModes(unittest.TestCase):
    """Each way the reader could give an answer for input it did not read.
    """

    @classmethod
    def setUpClass(cls):
        cls.b39 = gr.Build.at(GLIBC_CLONE, NEW, False)
        cls.b34 = gr.Build.at(GLIBC_CLONE, MID, True)

    def run_one(self, text, build=None, **files):
        allf = {b'm': text}
        allf.update({k.encode(): v for k, v in files.items()})
        return gr.run(b'm', allf.get, build or self.b39)

    def test_a_gperf_file_it_cannot_read_is_refused(self):
        good = 'head\n%%\nEND, tok_end, 0\n'
        self.assertEqual({b'END': ('tok_end', 0)}, gr.Build.parse_gperf(good))
        for bad in ('no separator\nEND, tok_end, 0\n',
                    'a\n%%\n%%\nEND, tok_end, 0\n',
                    'a\n%%\nEND, tok_end\n',
                    'a\n%%\nEND, end, 0\n',
                    'a\n%%\nEND, tok_end, 2\n',
                    'a\n%%\n\n'):
            with self.subTest(bad):
                with self.assertRaises(gr.ReaderError):
                    gr.Build.parse_gperf(bad)

    def test_a_ref_the_clone_does_not_hold_is_refused(self):
        with self.assertRaisesRegex(gr.ReaderError, 'git show'):
            gr.Build.at(GLIBC_CLONE, 'glibc-0.0-no-such-tag', False)

    def test_the_keyword_tables_are_the_tags_own(self):
        # codepoint_collation arrived in 2.35; RHEL 9 adds it by a patch.
        self.assertNotIn(b'codepoint_collation', self.b34.locfile_kw)
        self.assertIn(b'codepoint_collation', self.b39.locfile_kw)
        r9 = gr.rhel_build(GLIBC_CLONE, 'rhel9')
        self.assertEqual(('tok_codepoint_collation', 0),
                         r9.locfile_kw[b'codepoint_collation'])
        self.assertGreater(len(self.b34.locfile_kw), 170)

    def test_a_file_it_cannot_open_is_fatal(self):
        r = gr.run(b'nothing', {}.get, self.b39)
        self.assertEqual(('missing', b'nothing'), (r.fatal.kind, r.fatal.name))
        self.assertIsNone(r.collate)
        r = self.run_one(b'')
        self.assertEqual('missing', r.fatal.kind)

    def test_a_copy_of_a_missing_file_is_fatal(self):
        r = self.run_one(b'LC_COLLATE\ncopy "gone"\nEND LC_COLLATE\n')
        self.assertEqual(('missing', b'gone'), (r.fatal.kind, r.fatal.name))
        r = self.run_one(b'LC_TIME\ncopy "gone"\nEND LC_TIME\n')
        self.assertEqual(('missing', b'gone'), (r.fatal.kind, r.fatal.name))

    def test_a_copied_file_without_the_category_ends_the_section_early(self):
        text = b'LC_COLLATE\ncopy "t"\nEND LC_COLLATE\n'
        r = self.run_one(text, t=b'LC_TIME\nEND LC_TIME\n')
        self.assertTrue(r.sections('LC_COLLATE', b'm')[0].returned)
        self.assertIsNone(r.collate)
        r = self.run_one(text, t=b'LC_COLLATE\nEND LC_COLLATE\n')
        self.assertFalse(r.sections('LC_COLLATE', b'm')[0].returned)
        self.assertIsNotNone(r.collate)

    def test_an_unclosed_name_at_the_end_is_a_hang_not_an_answer(self):
        r = self.run_one(b'LC_COLLATE\n<U0061')
        self.assertEqual('hang', r.fatal.kind)
        self.assertIsNone(r.collate)

    def test_shapes_outside_the_model_are_unsettled(self):
        tail = 'after the first token of a statement in '
        shapes = (
            ('repertoiremap', b'repertoiremap x\n', 'repertoiremap'),
            ('END after a field', b'LC_NUMERIC\ndecimal_point "." '
             b'END LC_NUMERIC\n', tail + 'LC_NUMERIC'),
            ('END after an unknown word', b'LC_CTYPE\nfoo END LC_CTYPE\n',
             tail + 'LC_CTYPE'),
            ('translit_start after a word', b'LC_CTYPE\nfoo translit_start\n'
             b'END LC_CTYPE\n', tail + 'LC_CTYPE'),
            ('translit_end after a word', b'LC_CTYPE\nfoo translit_end\n'
             b'END LC_CTYPE\n', tail + 'LC_CTYPE'),
            ('copy in a tail', b'LC_COLLATE\nfoo copy "x"\nEND LC_COLLATE\n',
             tail + 'LC_COLLATE'),
            ('ifdef in a tail', b'LC_COLLATE\nfoo ifdef X\nEND LC_COLLATE\n',
             tail + 'LC_COLLATE'),
            ('an array after a field', b'LC_TIME\nd_fmt "x" abday "1"\n'
             b'END LC_TIME\n', tail + 'LC_TIME'),
            ('a run past the line', b'LC_COLLATE\n<U0061> "x\n<U0062>\n'
             b'END LC_COLLATE\n', 'run past the end of its line'),
            ('a first token past the line', b'LC_NUMERIC\n"x\n'
             b'END LC_NUMERIC\n', 'first token runs past'),
            ('tokens to the end of the file', b'LC_NUMERIC\n'
             b'decimal_point "." <U0041', 'do not end before the end'),
            ('reorder-sections-after', b'LC_COLLATE\n'
             b'reorder-sections-after <x>\nEND LC_COLLATE\n',
             'reorder-sections-after'),
            ('a translit entry', b'LC_CTYPE\ntranslit_start\nfoo bar\n'
             b'translit_end\nEND LC_CTYPE\n', 'translit entry starting'),
            ('an empty translit value', b'LC_CTYPE\ntranslit_start\n"" x\n'
             b'translit_end\nEND LC_CTYPE\n', 'having no value'),
            ('a non-ASCII copy name', b'LC_COLLATE\ncopy "<U00E9>"\n'
             b'END LC_COLLATE\n', 'above U+007F'),
        )
        for what, text, why in shapes:
            with self.subTest(what):
                r = self.run_one(text, **{'\u00e9': b'LC_COLLATE\n'
                                          b'END LC_COLLATE\n'})
                self.assertTrue(any(why in w for _, _, w in r.unsettled),
                                (what, r.unsettled))
        self.assertEqual([], self.run_one(b'LC_CTYPE\nEND LC_CTYPE\n'
                                          b'LC_COLLATE\nEND LC_COLLATE\n')
                         .unsettled)

    def test_codepoint_collation_read_for_another_category_is_a_crash(self):
        r = self.run_one(b'LC_TIME\ncopy "t"\nEND LC_TIME\n',
                         t=b'LC_COLLATE\ncodepoint_collation\n'
                           b'END LC_COLLATE\n')
        self.assertIsNotNone(r.fatal, 'read as if glibc built it')
        self.assertEqual('crash', r.fatal.kind)

    def test_an_ellipsis_into_a_copiers_struct_is_unsettled(self):
        # t is read for LC_COLLATE through the copy in the middle, which
        # gives it m's struct, then again for LC_TIME: its LC_COLLATE is
        # read with ignore_content and the ellipsis writes into m's struct
        # (ld-collate.c:3743-3756@2.39).
        r = self.run_one(b'LC_COLLATE\norder_start forward\ncopy "t"\n'
                         b'END LC_COLLATE\nLC_TIME\ncopy "t"\n'
                         b'END LC_TIME\n',
                         t=b'LC_COLLATE\n..\nEND LC_COLLATE\n'
                           b'LC_TIME\nEND LC_TIME\n')
        self.assertIsNone(r.fatal)
        self.assertTrue(any('a struct read for another category' in w
                            for _, _, w in r.unsettled), r.unsettled)

    def test_an_ellipsis_inside_an_order_section_is_settled(self):
        r = self.run_one(b'LC_COLLATE\norder_start forward\n<U0061>\n..\n'
                         b'<U0063>\norder_end\nEND LC_COLLATE\n')
        self.assertEqual([], r.unsettled)
        r = self.run_one(b'LC_COLLATE\n..\norder_start forward\n'
                         b'order_end\nEND LC_COLLATE\n')
        self.assertTrue(any('before order_start' in w
                            for _, _, w in r.unsettled), r.unsettled)

    def test_a_nested_conditional_is_skipped_whole(self):
        r = self.run_one(b'LC_COLLATE\nifdef NOPE\nifdef X\nelse\n'
                         b'copy "t"\nendif\nendif\norder_start forward\n'
                         b'END LC_COLLATE\n', t=b'LC_COLLATE\nEND LC_COLLATE\n')
        self.assertIsNone(r.fatal)
        self.assertEqual([], r.sections('LC_COLLATE', b'm')[0].mid_copies)

    def test_a_second_else_is_a_crash_not_an_answer(self):
        r = self.run_one(b'LC_COLLATE\nifdef X\nelse\nelse\n'
                         b'END LC_COLLATE\n')
        self.assertIsNotNone(r.fatal, 'a second else read as an answer')
        self.assertEqual('crash', r.fatal.kind)

    def test_a_byte_read_as_eof_inside_a_statement_is_unsettled(self):
        text = b'LC_NUMERIC\ndecimal_point "\x1a"\nEND LC_NUMERIC\n'
        self.assertTrue(self.run_one(text, self.b34).unsettled)
        self.assertEqual([], self.run_one(text, self.b39).unsettled)

    def test_reading_outside_glibcs_buffer_is_unsettled(self):
        # `-` ends one line in the escape, the next holds only the escape:
        # lr_ungetn cannot step back over the `-`, and get_ident reads
        # lr->buf[-1] of an empty buffer.
        r = self.run_one(b'LC_COLLATE\n-\\\n\\\nEND LC_COLLATE\n')
        self.assertIsNone(r.fatal)
        self.assertIn('glibc reads outside its buffer here',
                      [w for _, _, w in r.unsettled])
        # A whole LC_COLLATE read before the place is still not an answer.
        r = self.run_one(b'LC_COLLATE\norder_start forward\n<U0041>\n'
                         b'order_end\nEND LC_COLLATE\nLC_NUMERIC\n-x\n'
                         b'END LC_NUMERIC\n')
        self.assertIsNone(r.collate, 'a run stopped there read as one')
        self.assertIn('glibc reads outside its buffer here',
                      [w for _, _, w in r.unsettled])
        # Not only an empty buffer: two names glibc reads alike, the byte
        # before its buffer and `-y`, must not be told apart by the line.
        r = self.run_one(b'LC_COLLATE\ndefine \\\n-y\nifdef \\\n-y \\\n\n'
                         b'copy "t"\nendif\nEND LC_COLLATE\n',
                         t=b'LC_COLLATE\nEND LC_COLLATE\n')
        self.assertIn('glibc reads outside its buffer here',
                      [w for _, _, w in r.unsettled])

    def test_written_collate_lists_every_block_and_an_open_one(self):
        text = (b'LC_COLLATE\nEND LC_COLLATE\nx\nLC_COLLATE\n'
                b'order_start forward\n')
        self.assertEqual([(1, 2), (4, None)], gr.written_collate(text))
        self.assertEqual([], gr.written_collate(b'  LC_COLLATE\n'))


def _tok(t):
    return tuple(getattr(t, s) for s in gr.Tok.__slots__)


def _twin(build):
    """The same build with nothing kept yet."""
    return gr.Build(build.pre236, build.signed_char, build.locfile_kw,
                    build.charmap_kw, build.label)


@needs_clone
class ReaderKeepsOnlyWhatItWouldCompute(unittest.TestCase):
    """What a Build keeps (the statement lookahead, a file's lines) and the
    fast path for <names> give exactly what the reader computes without
    them (backlog 15.2). Every Run of glibc-2.12..2.39 was compared whole
    with the reader before them; these hold the parts no real file
    exercises."""

    @classmethod
    def setUpClass(cls):
        cls.b39 = gr.Build.at(GLIBC_CLONE, NEW, False)
        cls.b34 = gr.Build.at(GLIBC_CLONE, MID, True)

    def test_every_field_of_the_line_reader_is_accounted_for(self):
        for data in (b'x\n', b''):
            lr = gr.LR(data, 'f', self.b39)
            self.assertEqual(set(gr.LR_KEY_FIELDS), set(vars(lr)),
                             'LR has a field the kept lookahead does not '
                             'account for; add it to the key in '
                             '_Runner.statement and to LR_KEY_FIELDS')

    @staticmethod
    def lookahead(build, data, table='locfile', category='LC_COLLATE',
                  cc=None, ec=None, ts=1, rest=None):
        """The statement _Runner.statement stores for the first line of
        `data` and the reasons it gives, with the line reader set up as
        asked."""
        r = gr._Runner({}.get, build)
        lr = gr.LR(data, 'f', build, table=table)
        if cc is not None:
            lr.comment_char = cc
        if ec is not None:
            lr.escape_char = ec
        lr.translate_strings = ts
        t = lr.token()
        if rest is not None:
            assert len(rest) == lr.bufact - lr.idx, 'not the same length'
            lr.buf[lr.idx:] = rest
        store = gr.Collate()
        r.statement(lr, t, gr.Section(category, b'f', 1, True), store)
        toks = [_tok(x) for x in store.statements[-1][2]]
        return toks, [w for _, _, w in r.out.unsettled], (lr.pi, lr.idx)

    def test_a_kept_lookahead_is_used_only_for_the_same_state(self):
        # One variation per part of the key that a real run can change.
        # Each must change the answer, or the case proves nothing, and
        # after the base state was kept, the variation must still get the
        # answer a build that kept nothing gives.
        base = dict(data=b'<U0041> %x copy "ab" /x41\\\n+\n')
        lr = gr.LR(base['data'], 'f', _twin(self.b39))
        lr.token()
        rest = b' ' + b'z' * (lr.bufact - lr.idx - 1)
        variants = {
            'comment_char': dict(cc=0x25),
            'escape_char': dict(ec=0x2f),
            'translate_strings': dict(ts=0),
            'category': dict(category='LC_NUMERIC'),
            'table': dict(table='charmap'),
            # The line in the buffer is the same; the one it continues
            # into, read from the file, is not.
            'data': dict(data=b'<U0041> %x copy "ab" /x41\\\n-\n'),
            # Same length, so only the bytes in the buffer differ.
            'buffer': dict(rest=rest),
        }
        for what, change in variants.items():
            with self.subTest(what):
                for build in (self.b39, self.b34):
                    args = dict(base, **change)
                    kept = _twin(build)
                    first = self.lookahead(kept, **base)
                    again = self.lookahead(kept, **args)
                    alone = self.lookahead(_twin(build), **args)
                    self.assertNotEqual(first, alone,
                                        'the variation changes nothing')
                    self.assertEqual(alone, again)
                    self.assertEqual(first, self.lookahead(kept, **base))

    def test_files_of_one_length_keep_their_own_lines(self):
        shared = _twin(self.b39)
        files = {b'm': b'LC_COLLATE\ncopy "aa"\nEND LC_COLLATE\n',
                 b'n': b'LC_COLLATE\ncopy "bb"\nEND LC_COLLATE\n'}
        self.assertEqual(len(files[b'm']), len(files[b'n']))
        for name, want in ((b'm', b'aa'), (b'n', b'bb')):
            r = gr.run(name, files.get, shared)
            self.assertEqual((want, 2), r.sections('LC_COLLATE', name)[0].copy)

    def test_a_name_says_whether_a_greater_than_sign_closed_it(self):
        # A line ending in the escape is joined to the next (lr_next), so
        # the third name is closed on the line below.
        # With `>` as the escape, the byte after it is appended and the
        # loop stops on the `>` (linereader.c:510-530@2.39): closed.
        for data, ec, closed in ((b'<abc>\n', None, True),
                                 (b'<abc\n', None, False),
                                 (b'<abc\\\n>\n', None, True),
                                 (b'<abc\\\nd\n', None, False),
                                 (b'<abc> x\n', 0x3e, True),
                                 (b'<abc\n', 0x3e, False)):
            for fast in (True, False):
                with self.subTest(data=data, ec=ec, fast=fast), \
                        mock.patch.object(
                            gr.LR, '_symname_in_buffer',
                            gr.LR._symname_in_buffer if fast
                            else (lambda self: None)):
                    lr = gr.LR(data, 'f', _twin(self.b39))
                    if ec is not None:
                        lr.escape_char = ec
                    t = lr.token()
                    self.assertEqual(('bsymbol', closed), (t.tok, t.closed))

    def test_the_name_fast_path_reads_what_the_slow_one_reads(self):
        unsigned = gr.Build(True, False, self.b34.locfile_kw,
                            self.b34.charmap_kw, 'pre236-unsigned')
        lines = [b'<U0041>', b'<abc> x', b'<a/>b>', b'<a\\>b>', b'<a\x1ab>',
                 b'<a\xffb>', b'<a\xc3\xa9>', b'<abc', b'<abc\\\ndef>',
                 b'<abc/\ndef>', b'<>', b'<U00000041>', b'<a>>', b'<a\n>',
                 b'<code_set_name>', b'<a', b'<a\\', b'<a/']
        escapes = (None, 0x2f, 0x3e, 0x0a, 0x1a, 0xe9, 0x41)

        def tokens(build, data, ec):
            lr = gr.LR(data, 'f', build)
            if ec is not None:
                lr.escape_char = ec
            out = []
            try:
                while True:
                    t = lr.token()
                    out.append(_tok(t))
                    if t.tok == 'eof':
                        return out
            except gr.GlibcHang:
                return out + ['hang']

        for build in (self.b39, self.b34, unsigned):
            for line in lines:
                for ec, data in ((e, d) for e in escapes
                                 for d in (line + b'\n', line)):
                    with self.subTest(build=build.label, data=data, ec=ec):
                        fast = tokens(_twin(build), data, ec)
                        with mock.patch.object(
                                gr.LR, '_symname_in_buffer',
                                lambda self: None):
                            slow = tokens(_twin(build), data, ec)
                        self.assertEqual(slow, fast)


def _small_tree():
    return {
        b'a': b'LC_COLLATE\ncopy "b"\nEND LC_COLLATE\n',
        b'b': (b'LC_COLLATE\norder_start forward\n<U0041>\norder_end\n'
               b'END LC_COLLATE\n'),
        b'c': b'LC_CTYPE\nEND LC_CTYPE\n',
    }


@needs_clone
class ReaderCacheKeepsOnlyRunAnswers(unittest.TestCase):
    """scripts/reader_cache.py answers every name of a tree with what run()
    answers, and with nothing it kept for another tree, another build or
    another reader; what it cannot read it computes again."""

    @classmethod
    def setUpClass(cls):
        cls.b39 = gr.Build.at(GLIBC_CLONE, NEW, False)
        cls.b34 = gr.Build.at(GLIBC_CLONE, MID, True)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='pg-glibc-reader-cache-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {'PG_GLIBC_READER_JOBS': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        real = gr.run

        def counted(name, *a, **k):
            self.calls.append(name)
            return real(name, *a, **k)
        p = mock.patch.object(gr, 'run', counted)
        p.start()
        self.addCleanup(p.stop)

    def fresh(self, files, build=None):
        return {n: rc.summary(gr.run(n, files.get, _twin(build or self.b39)))
                for n in files}

    def kept(self, files, build=None, cache=None):
        del self.calls[:]
        return rc.tree_runs(files, build or self.b39,
                            cache_dir=self.dir if cache is None else cache)

    def test_a_kept_tree_is_the_fresh_answer(self):
        files = _small_tree()
        want = self.fresh(files)
        self.assertEqual(want, self.kept(files))
        self.assertEqual(sorted(files), sorted(self.calls))
        self.assertEqual(want, self.kept(files))
        self.assertEqual([], self.calls, 'a kept tree was read again')

    def test_a_changed_copy_target_is_read_again(self):
        files = _small_tree()
        self.kept(files)
        files[b'b'] = b'\n' + files[b'b']
        got = self.kept(files)
        self.assertEqual([b'a', b'b'], sorted(self.calls),
                         'only what reads b is read again')
        self.assertEqual(self.fresh(files), got)

    def test_a_name_the_tree_gains_is_read_again(self):
        files = _small_tree()
        files[b'a'] = b'LC_COLLATE\ncopy "x"\nEND LC_COLLATE\n'
        first = self.kept(files)
        self.assertEqual('missing', first[b'a'][0][0])
        files[b'x'] = files.pop(b'b')
        got = self.kept(files)
        self.assertIsNone(got[b'a'][0])
        self.assertEqual(self.fresh(files), got)

    def test_a_damaged_entry_is_computed_again(self):
        files = _small_tree()
        want = self.fresh(files)
        damage = [b'', b'{', b'[]', b'{}', b'null', b'"x"',
                  b'{"a": 1, "b": 2, "c": 3}',
                  b'[[{}, ["l", []]]]',
                  b'[[{"b": null}, ["l", [null, ["l", []], ["l", []], '
                  b'null]]]]']
        for bad in damage:
            with self.subTest(bad):
                self.kept(files)
                for dirpath, _, names in os.walk(self.dir):
                    for n in names:
                        if n.endswith('.json'):
                            with open(os.path.join(dirpath, n), 'wb') as fh:
                                fh.write(bad)
                self.assertEqual(want, self.kept(files))
                self.assertEqual(sorted(files), sorted(self.calls))

    def test_a_name_is_never_served_another_names_run(self):
        # Two names sharing a long prefix, and a kept entry that names no
        # file at all: neither is the run of `a`.
        files = _small_tree()
        long = {b'x' * 80 + b'_one': files[b'a'],
                b'x' * 80 + b'_two': b'LC_CTYPE\nEND LC_CTYPE\n'}
        files.update(long)
        self.kept(files)
        files[b'c'] = b'\n' + files[b'c']
        self.assertEqual(self.fresh(files), self.kept(files))
        planted = [[{}, rc._enc(self.fresh(files)[b'c'])]]
        for dirpath, _, names in os.walk(self.dir):
            for n in names:
                if n.endswith('.json'):
                    with open(os.path.join(dirpath, n), 'w') as fh:
                        json.dump(planted, fh)
        self.assertEqual(self.fresh(files), self.kept(files))
        self.assertEqual(sorted(files), sorted(self.calls))

    def test_another_build_shares_nothing(self):
        # The same keyword tables, and only lr_getc told apart: before 2.36
        # a 0x1A byte ends the file, so d has no LC_COLLATE there.
        files = _small_tree()
        files[b'd'] = b'\x1a\n' + files[b'b']
        after = self.b39
        before = gr.Build(True, after.signed_char, after.locfile_kw,
                          after.charmap_kw, 'pre236')
        self.assertNotEqual(self.fresh(files, after)[b'd'],
                            self.fresh(files, before)[b'd'])
        self.kept(files, after)
        self.assertEqual(self.fresh(files, before),
                         self.kept(files, before))
        self.assertEqual(sorted(files), sorted(self.calls))
        for other in (self.b34, gr.Build(False, False, after.locfile_kw,
                                         after.charmap_kw, 'unsigned')):
            self.assertNotEqual(rc.build_id(after), rc.build_id(other))

    def test_the_version_follows_the_readers_source(self):
        both = ('glibc_reader.py', 'reader_cache.py')
        for changed in both + (None,):
            copy = tempfile.mkdtemp(dir=self.dir)
            for f in both:
                shutil.copy(os.path.join(_harness.SCRIPTS_DIR, f), copy)
            if changed:
                with open(os.path.join(copy, changed), 'ab') as fh:
                    fh.write(b'\n')
            with self.subTest(changed), \
                    mock.patch.object(rc, '_HERE', copy):
                if changed:
                    self.assertNotEqual(rc.VERSION, rc._version())
                else:
                    self.assertEqual(rc.VERSION, rc._version())

    def test_another_reader_starts_from_nothing_and_drops_the_old(self):
        files = _small_tree()
        self.kept(files)
        old = rc.VERSION
        keep = os.path.join(self.dir, 'not-a-version')
        os.mkdir(keep)
        with mock.patch.object(rc, 'VERSION', 'f' * 20):
            self.assertEqual(self.fresh(files), self.kept(files))
            self.assertEqual(sorted(files), sorted(self.calls))
        self.assertFalse(os.path.exists(os.path.join(self.dir, old)))
        self.assertTrue(os.path.isdir(keep))

    def test_a_directory_it_cannot_write_still_answers(self):
        blocker = os.path.join(self.dir, 'file')
        with open(blocker, 'w') as fh:
            fh.write('x')
        files = _small_tree()
        self.assertEqual(self.fresh(files),
                         self.kept(files, cache=os.path.join(blocker, 'c')))

    def test_many_processes_answer_what_one_does(self):
        files = {b'l%02d' % i: b'LC_COLLATE\ncopy "b"\nEND LC_COLLATE\n'
                 for i in range(12)}
        files.update(_small_tree())
        with mock.patch.dict(os.environ, {'PG_GLIBC_READER_JOBS': '4'}):
            got = self.kept(files)
        self.assertEqual(self.fresh(files), got)


@needs_floor_pair
class ReaderAgreesOnEveryRealFile(unittest.TestCase):
    """run() against the line readers the steps use today, over every locale
    file of the five tags, each as the -i of a localedef run with its
    copies found in the same tag: the same LC_COLLATE bounds and the same
    copies, no fatal run, and nothing unsettled but the known grammar
    (backlog 15.1 measured this outside the suite, 9.4 minutes on the Mac;
    reader_cache keeps the runs, so after the first time it takes
    seconds)."""

    TAGS = (FLOOR_OLD, FLOOR_NEW, OLD, MID, NEW)
    # reorder-sections-after, read in 15.3; at 2.12 and 2.17 only.
    UNSETTLED = {FLOOR_OLD: {'reorder-sections-after in LC_COLLATE': 17},
                 FLOOR_NEW: {'reorder-sections-after in LC_COLLATE': 17}}

    def test_each_tag(self):
        for tag in self.TAGS:
            with self.subTest(tag):
                self.check(tag)

    def check(self, tag):
        names, data = _tag_files(tag)
        pre236 = g.descends_from(GLIBC_CLONE, g.LR_GETC_FIX, tag)
        self.assertIsNotNone(pre236, 'git cannot place %s' % tag)
        runs = rc.tree_runs(data, gr.Build.at(GLIBC_CLONE, tag, not pre236))
        self.assertEqual(sorted(data), sorted(runs))
        self.assertGreater(len(runs), 250)
        bad, unsettled = [], {}
        for name in names:
            fatal, reads, uns, _ = runs[name]
            if fatal is not None:
                bad.append((name, 'fatal', fatal))
            for _, _, why in uns:
                unsettled[why] = unsettled.get(why, 0) + 1
            secs = [s for fr in reads for s in fr[3]
                    if s[0] == 'LC_COLLATE' and s[1] == name]
            mine = (secs[0][2], secs[0][3]) if secs else None
            text = g.decoded(data[name])
            theirs = g.collate_bounds(text)
            if mine != theirs:
                bad.append((name, 'bounds', mine, theirs))
            cps = []
            for s in secs[:1]:
                if s[6]:
                    cps.append(s[6][0])
                cps += [n for n, _ in s[10]]
            cps = [c.decode('utf-8', 'replace') if c is not None else None
                   for c in cps]
            if cps != g.copy_targets(text):
                bad.append((name, 'copies', cps, g.copy_targets(text)))
        self.assertEqual([], bad)
        self.assertEqual(self.UNSETTLED.get(tag, {}), unsettled)


def _tag_files(tag):
    """(names in order, {name: bytes}) for localedata/locales at `tag`."""
    paths = g.list_locale_files(GLIBC_CLONE, tag)
    contents, missing = g.read_blobs(GLIBC_CLONE, tag, paths, raw=True)
    g.require_read(tag, paths, missing, 'the corpus')
    data = {os.path.basename(p).encode('utf-8', 'surrogateescape'): v
            for p, v in contents.items()}
    return sorted(data), data


if __name__ == '__main__':
    unittest.main()
