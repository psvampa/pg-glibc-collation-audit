"""scripts/glibc_reader.py, a reader of glibc's locale sources that follows
localedef, held to glibc itself. Steps 1 to 4 and 9/10 read through it
(scripts/glibc_tree.py).

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
15.2), and run() agrees with the line readers steps 6 to 8 still use on
the LC_COLLATE bounds and copies of every locale file of the five tags,
which the cache makes cheap after the first time. Last, glibc_tree: the
shapes those line readers got wrong, through steps 1 to 4, what step 2
asks of each file (file_facts) and how it is kept, and its answers on the
five tags against theirs, every difference named.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
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
# An 0xFF inside a statement: what each handler reads after it is the
# statement grammars' (block 15, 15.3 to 15.4); until then the reader says
# so (false-negative-reviewer, round 3 of 15.2 B2).
_MID_STATEMENT = {'s62', 's64', 's65', 's66'}
EXPECTED_UNSETTLED = {
    'rhel8': {'b18', 'b19', 's51'} | _LATER | _UNDEFINED | _MID_STATEMENT,
    'rhel9': {'b18', 'b19', 's42', 's51'} | _LATER | _UNDEFINED
    | _MID_STATEMENT,
    'rhel10': {'s42', 's67', 's68'} | _LATER | _UNDEFINED | _MID_STATEMENT,
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
    (ld-collate.c:1849-1853@2.28), and one with codepoint_collation read
    writes no table of elements at all, whatever else it holds
    (ld-collate.c:1575, :2123@2.39), so no marker."""
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
    defined, placed, state, order, codepoint = set(), set(), 0, False, False
    for _, _, toks in r.collate.statements:
        t = toks[0]
        if t.tok == 'kw' and t.kwtok == 'tok_order_start':
            state, order = 1, True
        elif t.tok == 'kw' and t.kwtok == 'tok_codepoint_collation':
            order = codepoint = True
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
            'markers': [] if codepoint else sorted(
                m for m in case.markers if ('mk-' + m).encode() in placed)}


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

    def test_after_END_and_an_0xFF_localedef_reads_the_order_on(self):
        """What the markers cannot show when codepoint_collation is read:
        on el9 the table s54 writes is not s44's, the keyword alone, so
        glibc read the order after `END <0xFF>` and did not build byte
        order. On el10 the 0xFF is a byte and the section ended at END.
        On el8, s53 holds both markers placed after it (false-negative-
        reviewer, round 2 of 15.2 B2; measured 2026-10-14)."""
        got = {n: load_oracle(os.path.join(ORACLE_DIR, n + '.json'))['cases']
               for n in ('rhel8', 'rhel9', 'rhel10')}

        def md5(n, cid):
            return got[n][cid]['c']['collate']['md5']
        self.assertNotEqual(md5('rhel9', 's44'), md5('rhel9', 's54'))
        self.assertEqual(md5('rhel10', 's44'), md5('rhel10', 's54'))
        self.assertEqual(['a', 'b'],
                         got['rhel8']['s53']['c']['collate']['markers'])

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

    def test_kept_tree_says_whether_an_entry_exists(self):
        """What the fill asks before it reads a tree: blob ids alone, the
        same tree, the same build."""
        files = _small_tree()
        ids = {n: rc.blob_id(d) for n, d in files.items()}
        self.assertFalse(rc.kept_tree(ids, self.b39, cache_dir=self.dir))
        self.kept(files)
        self.assertTrue(rc.kept_tree(ids, self.b39, cache_dir=self.dir))
        self.assertFalse(rc.kept_tree(ids, self.b34, cache_dir=self.dir))
        ids[b'c'] = rc.blob_id(b'other')
        self.assertFalse(rc.kept_tree(ids, self.b39, cache_dir=self.dir))

    def test_a_collate_kept_in_another_shape_is_computed_again(self):
        """The shape before backlog 15.2 part B2 kept (files, count); read as
        today's, its count would be a list of ranges. Refused, computed."""
        files = _small_tree()
        want = self.fresh(files)
        self.kept(files)
        for dirpath, _, names in os.walk(self.dir):
            for n in names:
                if not n.endswith('.json'):
                    continue
                path = os.path.join(dirpath, n)
                with open(path) as fh:
                    got = json.load(fh)
                old = rc._enc((None, (), (), (('b',), 4)))
                if isinstance(got, dict):
                    got = {k: old for k in got}
                else:
                    got = [[deps, old] for deps, _ in got]
                with open(path, 'w') as fh:
                    json.dump(got, fh)
        self.assertEqual(want, self.kept(files))
        self.assertEqual(sorted(files), sorted(self.calls))

    def test_the_kept_collate_says_where_ranges_and_codepoint_are(self):
        """What steps 4, 9 and 10 read from a run: per file, how many
        statements, which lines hold a range ld-collate.c reads as one
        (`..`, `...`, `....`; not the charmap's `(2)` forms, not a `#`
        comment), and where codepoint_collation was read."""
        files = {
            b'a': (b'LC_COLLATE\ncopy "b"\n<U0041>\n# <U0042>..<U0043>\n'
                   b'<U0044>..<U0045>\n<U0046> ...\n<U0047>..(2)..<U0048>\n'
                   b'END LC_COLLATE\n'),
            b'b': b'LC_COLLATE\ncodepoint_collation\nEND LC_COLLATE\n'}
        fatal, _, unsettled, collate, _ = self.kept(files)[b'a']
        self.assertIsNone(fatal)
        counts, ranges, codepoint = collate
        self.assertEqual((('a', 4), ('b', 1)), counts)
        self.assertEqual((('a', 5), ('a', 6)), ranges)
        self.assertEqual((('b', 2),), codepoint)
        self.assertEqual(self.fresh(files)[b'a'], self.kept(files)[b'a'])
        # A statement joined over three lines by the escape: the range is on
        # the line that holds it, which is the one a reader is shown.
        files = {b'c': (b'LC_COLLATE\norder_start forward\n<U0041> \\\n'
                        b'..\\\n <U0045>\norder_end\nEND LC_COLLATE\n')}
        self.assertEqual((('c', 4),), self.kept(files)[b'c'][3][1])

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
    """run() against the line readers steps 6 to 8 use, over every locale
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
            fatal, reads, uns, _, _ = runs[name]
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



# The shapes the line readers steps 1, 3 and 4 used before backlog 15.2 got
# wrong, each with what localedef reads (backlog items in the names). Every
# one copies or ranges like a real locale, so a step that reads it wrong
# moves a real answer.
_HEAD = b'comment_char %\nescape_char /\n'
_SHAPES = {
    # 13.22: a `copy` split by the escape at the end of a line; lr_next
    # joins the two lines (linereader.c:162-171@2.39).
    'zz_SP': _HEAD + b'LC_COLLATE\nco/\npy "iso14651_t1"\nEND LC_COLLATE\n',
    # 13.26: the escape inside the word; get_ident keeps the character after
    # it (linereader.c:583-592@2.39).
    'zz_SW': _HEAD + b'LC_COLLATE\nc/opy "iso14651_t1"\nEND LC_COLLATE\n',
    # 13.27: the escape inside the string of a copy that opens LC_COLLATE,
    # read before collate_startup turns string translation off
    # (ld-collate.c:1524@2.39), so glibc drops it.
    'zz_SS': _HEAD + b'LC_COLLATE\ncopy "iso14651/_t1"\nEND LC_COLLATE\n',
    # 2.21: the section keywords indented; lr_token skips blanks first.
    'zz_IN': _HEAD + b'  LC_COLLATE\ncopy "iso14651_t1"\n  END LC_COLLATE\n',
    # 13.8: no comment_char, so `#` is the comment and `%` is not
    # (linereader.c:79@2.39): a range only in a `#` comment is no range, and
    # one followed by `%` text is one.
    'zz_HC': (b'LC_COLLATE\n# <U0041>..<U0045> IGNORE\norder_start forward\n'
              b'<U0041> IGNORE\norder_end\nEND LC_COLLATE\n'),
    'zz_PC': (b'LC_COLLATE\norder_start forward\n<U0041>\n'
              b'<U0042>..<U0045> % not a comment here\n<U0046>\norder_end\n'
              b'END LC_COLLATE\n'),
}
_COPIERS = ('zz_IN', 'zz_SP', 'zz_SS', 'zz_SW')


def _tree_with(tag, extra, build=None):
    """glibc_tree.Tree of every locale file at `tag` plus `extra`."""
    import glibc_tree
    names, data = _tag_files(tag)
    data = dict(data)
    for n, b in extra.items():
        data[n.encode('ascii')] = b
    pre = g.descends_from(GLIBC_CLONE, g.LR_GETC_FIX, tag)
    build = build or gr.Build.at(GLIBC_CLONE, tag, not pre)
    return glibc_tree.Tree(tag, data, build, g.decoded)


@needs_clone
class TreeReadsWhatLocaledefReads(unittest.TestCase):
    """scripts/glibc_tree.py answers from run() and nothing else: the
    shapes the old line readers got wrong come out the way localedef reads
    them, and a run it cannot settle, or that localedef does not finish, is
    named rather than read as clean (backlog 15.2)."""

    @classmethod
    def setUpClass(cls):
        cls.tree = _tree_with(NEW, _SHAPES)

    def test_each_copy_shape_is_the_copy_glibc_reads(self):
        for name in _COPIERS:
            with self.subTest(name):
                self.assertTrue(self.tree.defines(name))
                self.assertEqual(['iso14651_t1'], self.tree.copies(name))
                self.assertIn('iso14651_t1', self.tree.reach(name))
                self.assertEqual(['iso14651_t1'],
                                 self.tree.roots(name, {'iso14651_t1'}))

    def test_the_old_readers_really_got_these_wrong(self):
        """Control: each shape is one the line readers missed, so the test
        above cannot pass by testing what they already got right."""
        for name in ('zz_SP', 'zz_SW', 'zz_SS'):
            with self.subTest(name):
                text = _SHAPES[name].decode('ascii')
                self.assertNotEqual(['iso14651_t1'], g.copy_targets(text))
        self.assertIsNone(g.collate_block(_SHAPES['zz_IN'].decode('ascii')))
        # 13.8 is the other direction: `%` taken for the comment made the
        # `#` comment a range. Over-reporting, but not what glibc reads.
        for n in ('zz_HC', 'zz_PC'):
            text = _SHAPES[n].decode('ascii')
            self.assertTrue(g.ellipsis_hits(g.collate_text(text),
                                            g.comment_char(text)), n)

    def test_a_range_is_what_localedef_reads_as_one(self):
        self.assertEqual([], self.tree.ranges('zz_HC'))
        self.assertEqual([(4, '<U0042>..<U0045> % not a comment here')],
                         self.tree.ranges('zz_PC'))
        self.assertEqual('explicit', self.tree.style('zz_HC'))
        self.assertEqual('ellipsis', self.tree.style('zz_PC'))

    def test_a_run_that_does_not_finish_or_settle_is_named(self):
        tree = _tree_with(NEW, {
            'zz_CA': _HEAD + b'LC_COLLATE\ncopy "zz_CB"\nEND LC_COLLATE\n',
            'zz_CB': _HEAD + b'LC_COLLATE\ncopy "zz_CA"\nEND LC_COLLATE\n',
            'zz_MI': _HEAD + b'LC_COLLATE\ncopy "zz_NONE"\nEND LC_COLLATE\n',
            # A copy of a file without LC_COLLATE: glibc returns there and
            # stores none (ld-collate.c:2657-2658@2.39).
            'zz_NR': _HEAD + b'LC_COLLATE\ncopy "zz_NOC"\nEND LC_COLLATE\n',
            'zz_NOC': _HEAD + b'LC_CTYPE\nEND LC_CTYPE\n',
            'zz_RS': _HEAD + (b'LC_COLLATE\ncopy "iso14651_t1"\n'
                              b'reorder-sections-after <LATIN> <X>\n'
                              b'END LC_COLLATE\n')})
        why = tree.unresolved()
        self.assertEqual('localedef stops (circular)', why['zz_CA'])
        self.assertEqual('localedef stops (circular)', why['zz_CB'])
        self.assertEqual('localedef cannot open zz_NONE', why['zz_MI'])
        self.assertEqual({'zz_MI': ['zz_NONE']}, tree.missing())
        self.assertEqual('localedef stores no LC_COLLATE for it',
                         why['zz_NR'])
        self.assertIn('reorder-sections-after in LC_COLLATE', why['zz_RS'])
        for name in why:
            self.assertEqual('unread', tree.style(name))
        self.assertEqual({'zz_CA', 'zz_CB', 'zz_MI', 'zz_NR', 'zz_RS'},
                         set(why), 'every tag file reads clean')

    def test_byte_order_follows_the_build(self):
        """codepoint_collation is a keyword from glibc 2.35 and on RHEL 9
        (glibc_reader.RHEL_BUILDS). Without it the word is a syntax error,
        and byte order is not what glibc builds."""
        import glibc_tree
        c = {'C': _harness.upstream_c().encode('utf-8')}
        plain = _tree_with(MID, c, glibc_tree.build_at(GLIBC_CLONE, MID))
        rhel9 = _tree_with(MID, c, glibc_tree.build_at(
            GLIBC_CLONE, MID, 'glibc-2.34-275.el9_8'))
        self.assertEqual((set(), {}), plain.byte_order())
        self.assertEqual(({'C'}, {}), rhel9.byte_order())
        self.assertEqual('codepoint', rhel9.style('C'))

    def test_a_file_glibc_stops_reading_is_not_read_as_having_no_order(self):
        """Before 2.36 lr_getc gives the top level an EOF at a 0x1A, and at
        an 0xFF through a signed char, and glibc reads nothing more of the
        file (case b02, measured on el8 and el9). The file then reads as
        having no LC_COLLATE although it has one below: named, never put on
        no list (false-negative-reviewer, round 1 of B2). From 2.36 the byte
        is a byte, and the same file reads to its end."""
        import glibc_tree
        c = _harness.upstream_c().encode('utf-8')
        rhel9 = glibc_tree.build_at(GLIBC_CLONE, MID, 'glibc-2.34-1.el9')
        for byte in (b'\x1a', b'\xff'):
            with self.subTest(byte):
                tree = _tree_with(MID, {'C': byte + b'\n' + c,
                                        'zz_B': (_HEAD + b'LC_CTYPE\ncopy '
                                                 b'"i18n"\nEND LC_CTYPE\n'
                                                 + byte + b'\nLC_COLLATE\n'
                                                 b'copy "iso14651_t1"\n'
                                                 b'END LC_COLLATE\n')}, rhel9)
                why = tree.unresolved()
                for name, line in (('C', 1), ('zz_B', 6)):
                    self.assertEqual(
                        'localedef stops reading %s at line %d, before the '
                        'end of the file' % (name, line), why.get(name))
                    self.assertEqual('unread', tree.style(name))
                self.assertEqual((set(), {}), tree.byte_order())
        tree = _tree_with(NEW, {'C': b'\x1a\n' + c})
        self.assertNotIn('C', tree.unresolved())
        self.assertEqual(({'C'}, {}), tree.byte_order())
        # A line holding only the escape character stops every build,
        # 2.39 included (b14, b15): the buffer is empty, nothing on the
        # line is left, and only the lines below it are.
        esc = b'comment_char %\nescape_char /\n/\n'
        for tag, build in ((MID, rhel9), (NEW, None)):
            with self.subTest(tag):
                tree = _tree_with(tag, {'C': esc + c}, build)
                self.assertEqual('localedef stops reading C at line 3, '
                                 'before the end of the file',
                                 tree.unresolved().get('C'))

    def test_a_cut_in_a_copied_file_is_named(self):
        """zz_X is cut above a range; zz_Y copies zz_X. glibc reads zz_X up
        to the cut in both runs, so both are named, not read as a settled
        copy (false-negative-reviewer, round 2 of B2)."""
        import glibc_tree
        tree = _tree_with(MID, {
            'zz_X': _HEAD + (b'LC_COLLATE\ncopy "iso14651_t1"\n\x1a\n'
                             b'order_start forward\n<U0041>..<U0045>\n'
                             b'order_end\nEND LC_COLLATE\n'),
            'zz_Y': _HEAD + b'LC_COLLATE\ncopy "zz_X"\nEND LC_COLLATE\n'},
            glibc_tree.build_at(GLIBC_CLONE, MID, 'x.el9'))
        why = tree.unresolved()
        for name in ('zz_X', 'zz_Y'):
            self.assertEqual('localedef stops reading zz_X at line 5, before '
                             'the end of the file', why.get(name), name)

    def test_codepoint_then_END_and_an_0xFF_is_not_byte_order(self):
        """Before 2.36 an 0xFF after END is an EOF lr_getc consumes, and the
        section reads on (case s54): the order after it joins the keyword,
        and glibc does not build byte order (false-negative-reviewer, round
        2 of B2)."""
        import glibc_tree
        c = (b'LC_COLLATE\ncodepoint_collation\nEND \xff\n'
             b'order_start forward\n<U0041>\n<U0042>\norder_end\n'
             b'END LC_COLLATE\n')
        tree = _tree_with(MID, {'C': c},
                          glibc_tree.build_at(GLIBC_CLONE, MID, 'x.el9'))
        self.assertEqual((set(), {}), tree.byte_order())
        self.assertEqual('codepoint-not-alone', tree.style('C'))

    def test_an_0xFF_inside_a_statement_is_not_read_as_settled(self):
        """An 0xFF that lr_getc consumes ends a statement whose handler may
        leave the rest of its line, and LC_CTYPE then reads on after it
        (case s62): the LC_COLLATE below is read at the top level. The
        reader does not settle which handler does what yet, so zz_A is
        named, not read as defining no LC_COLLATE and dropped
        (false-negative-reviewer, round 3 of B2)."""
        import glibc_tree
        tree = _tree_with(MID, {
            'zz_A': _HEAD + (b'LC_CTYPE\nfoo \xff\nLC_COLLATE\n'
                             b'copy "iso14651_t1"\nEND LC_COLLATE\n'
                             b'END LC_CTYPE\n')},
            glibc_tree.build_at(GLIBC_CLONE, MID, 'x.el9'))
        self.assertIn('a byte read as the end of the file inside a '
                      'statement', tree.unresolved()['zz_A'])

    def test_translit_start_then_an_0xFF_reads_LC_CTYPE_on(self):
        """translit_start's loop ends at the consumed EOF and LC_CTYPE goes
        on (case s67): the LC_COLLATE word and codepoint_collation after it
        are LC_CTYPE's syntax errors, and the real LC_COLLATE copies
        iso14651_t1. Read as byte order before the fix (false-negative-
        reviewer, round 3 of B2)."""
        import glibc_tree
        tree = _tree_with(MID, {
            'C': (b'LC_CTYPE\ntranslit_start\n\xff\nLC_COLLATE\n'
                  b'codepoint_collation\nEND LC_CTYPE\nLC_COLLATE\n'
                  b'copy "iso14651_t1"\nEND LC_COLLATE\n')},
            glibc_tree.build_at(GLIBC_CLONE, MID, 'x.el9'))
        self.assertEqual((set(), {}), tree.byte_order())
        self.assertEqual(['iso14651_t1'], tree.copies('C'))
        self.assertEqual('copy-only', tree.style('C'))

    def test_an_unsettled_run_is_not_byte_order(self):
        """A run with an unsettled place is not read as byte order, even
        when its one stored statement is codepoint_collation."""
        import glibc_tree
        c = _harness.upstream_c().encode('utf-8')
        tree = _tree_with(MID, {'C': b'repertoiremap mnemonic.ds\n' + c},
                          glibc_tree.build_at(GLIBC_CLONE, MID, 'x.el9'))
        self.assertIn('a repertoiremap directive', tree.unresolved()['C'])
        self.assertEqual((set(), {}), tree.byte_order())

    def test_a_copy_is_byte_order_only_through_byte_order_locales(self):
        """zz_A copies an empty LC_COLLATE, then C: the one statement it
        stores is C's codepoint_collation, but its copy is not a byte-order
        locale, and localedef refuses the empty one (ld-collate.c:3763-3770
        @2.39). zz_K copies zz_J, which copies C: byte order, named by the
        locale it copies."""
        import glibc_tree
        c = _harness.upstream_c().encode('utf-8')
        tree = _tree_with(MID, {
            'C': c,
            'zz_E': _HEAD + b'LC_COLLATE\nEND LC_COLLATE\n',
            'zz_A': _HEAD + (b'LC_COLLATE\ncopy "zz_E"\ncopy "C"\n'
                             b'END LC_COLLATE\n'),
            'zz_J': _HEAD + b'LC_COLLATE\ncopy "C"\nEND LC_COLLATE\n',
            'zz_K': _HEAD + b'LC_COLLATE\ncopy "zz_J"\nEND LC_COLLATE\n',
            # One copy, of zz_A's shape: one stored statement, still not
            # byte order, because what it copies is not.
            'zz_M': _HEAD + b'LC_COLLATE\ncopy "zz_A"\nEND LC_COLLATE\n'},
            glibc_tree.build_at(GLIBC_CLONE, MID, 'x.el9'))
        declared, by_copy = tree.byte_order()
        self.assertEqual({'C'}, declared)
        self.assertEqual({'zz_J': 'C', 'zz_K': 'zz_J'}, by_copy)
        self.assertEqual(['zz_A'], tree.copies('zz_M'))
        self.assertEqual(1, sum(n for _, n in tree._collate('zz_M')[0]))

    def test_a_build_id_names_a_rhel_major_or_nothing(self):
        import glibc_tree
        kw = 'codepoint_collation'.encode('ascii')
        for build_id, has in (('glibc-2.34-275.el9_8', True),
                              ('fake.el9', True),
                              ('glibc-2.28-251.el8_10.40', False),
                              ('glibc-2.34-1.el90', False),
                              ('fake', False), (None, False)):
            with self.subTest(build_id):
                b = glibc_tree.build_at(GLIBC_CLONE, MID, build_id)
                self.assertEqual(has, kw in b.locfile_kw)
                self.assertTrue(b.pre236)

    def test_a_ref_git_cannot_place_is_refused(self):
        """Before or after the lr_getc fix decides how 0x1A and 0xFF read;
        a repository without glibc's history cannot say, and a guess is not
        an answer (glibc_tree.build_at)."""
        import glibc_tree
        tmp = tempfile.mkdtemp(prefix='pg-glibc-tree-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_COMMITTER_NAME='t',
                   GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_EMAIL='t@example.invalid')
        subprocess.run(['git', 'init', '-q', tmp], check=True)
        subprocess.run(['git', '-C', tmp, 'commit', '-q', '--allow-empty',
                        '-m', 'x'], check=True, env=env)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), \
                self.assertRaises(SystemExit) as cm:
            glibc_tree.build_at(tmp, 'HEAD')
        self.assertEqual(2, cm.exception.code)
        self.assertIn("lr_getc fix", _harness.flat(buf.getvalue()))
        self.assertIn('refusing to guess', _harness.flat(buf.getvalue()))


@needs_clone
class StepsReadWhatLocaledefReads(unittest.TestCase):
    """The same shapes through steps 1, 3 and 4, on glibc-2.39 plus the
    files (a commit git can place, _harness.glibc_commit). Before backlog
    15.2 step 3 left all four copiers out of the set a change to
    iso14651_t1 affects: a reassuring answer about four locales that
    inherit its order."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-steps-')
        cls.repo, cls.ref = _harness.glibc_commit(NEW, _SHAPES, cls.tmp)
        cls.out = os.path.join(cls.tmp, 'out')
        os.mkdir(cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def step(self, script, *args, cwd=None):
        env = dict(os.environ, PG_GLIBC_AUDIT_OUT=self.out)
        p = subprocess.run([sys.executable, os.path.join(
            _harness.SCRIPTS_DIR, script)] + list(args),
            cwd=cwd or _harness.SCRIPTS_DIR, env=env, capture_output=True)
        out = (p.stdout + p.stderr).decode('utf-8', 'replace')
        self.assertEqual(0, p.returncode, out)
        return out

    def test_step_3_lists_every_copier_of_a_changed_template(self):
        out = self.step('resolve_copy_closure.py', self.ref, 'iso14651_t1',
                        '--repo', self.repo)
        full = re.search(r'^Full affected set \(\d+ locale source file\(s\)'
                         r'\): (.+)$', out, re.M)
        self.assertIsNotNone(full, out)
        for name in _COPIERS:
            self.assertIn(name, full.group(1).split(', '))
            self.assertIn('  %s: copy "iso14651_t1" -> reaches iso14651_t1'
                          % name, out)

    def test_step_4_reads_ranges_and_copies_as_localedef_does(self):
        base = self.step('flag_algorithmic_ranges.py', NEW)
        out = self.step('flag_algorithmic_ranges.py', self.ref,
                        '--repo', self.repo)
        via = r'^  via iso14651_t1: (\d+) locale'
        self.assertEqual(int(re.search(via, base, re.M).group(1)) + 4,
                         int(re.search(via, out, re.M).group(1)), out)
        flagged = out.split('ranges: ', 1)[1].split('\n\n', 1)[0]
        self.assertIn('  zz_PC\n', flagged)
        self.assertNotIn('zz_HC', flagged)

    def test_step_3_names_a_changed_locale_it_could_not_read(self):
        """zz_X's LC_CTYPE copies a file the tree lacks, so localedef stops
        before zz_X's LC_COLLATE. Step 3 must not tell its reader that zz_X
        has none and nothing can inherit from it: zz_X is named under the
        `!!`, and zz_Y, which copies it, is listed (false-negative-reviewer,
        round 1 of B2)."""
        tmp = tempfile.mkdtemp(prefix='pg-glibc-steps-x-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo, ref = _harness.glibc_commit(NEW, {
            'zz_X': _HEAD + (b'LC_CTYPE\ncopy "zz_nope"\nEND LC_CTYPE\n'
                             b'LC_COLLATE\ncopy "iso14651_t1"\n'
                             b'END LC_COLLATE\n'),
            'zz_Y': _HEAD + b'LC_COLLATE\ncopy "zz_X"\nEND LC_COLLATE\n'},
            tmp)
        out = self.step('resolve_copy_closure.py', ref, 'zz_X',
                        '--repo', repo)
        self.assertNotIn('have no LC_COLLATE block', out)
        self.assertIn('\n     zz_X: localedef cannot open zz_nope\n', out)
        self.assertIn('  zz_Y: copy "zz_X" -> reaches zz_X', out)

    def test_step_1_counts_every_copier(self):
        here = self.step('glibc_locale_data.py', 'fanin', self.ref,
                         cwd=self.repo)
        base = self.step('glibc_locale_data.py', 'fanin', NEW)
        count = r'^\s+(\d+) locales inherit from iso14651_t1$'
        self.assertEqual(int(re.search(count, base, re.M).group(1)) + 4,
                         int(re.search(count, here, re.M).group(1)), here)


# A directive in LC_CTYPE after a `copy` of a file without LC_CTYPE: the
# copy returns and leaves the rest of the section to the top level
# (ld-ctype.c:2128-2139@2.34), which obeys `escape_char /` in zz_RF's own
# run; zz_RB copies zz_RF's LC_COLLATE alone, its run skips LC_CTYPE and
# never meets the directive (cases d25 and d26 of reader_oracle).
# False-negative-reviewer, 15.2 B3.
_RETURNS = {
    'zz_RF': (b'LC_CTYPE\ncopy "zz_RX"\nescape_char /\nEND LC_CTYPE\n'
              b'LC_COLLATE\ncopy "iso14651_t1"\nEND LC_COLLATE\n'),
    'zz_RB': b'LC_COLLATE\ncopy "zz_RF"\nEND LC_COLLATE\n',
    'zz_RX': b'LC_IDENTIFICATION\ntitle "t"\nEND LC_IDENTIFICATION\n',
}


# Step 2's shapes (backlog 15.2 B3), as pairs: the first version and one
# whose change lies where the line readers did not look.
# 13.25: case k07 of backlog 15.0, measured on RHEL 8, 9 and 10: the first
# `END LC_COLLATE` is joined by the escape to the line above, read as a
# weight ("syntax error"), and glibc reads on to the second.
_K07 = (b'LC_COLLATE\ncollating-element <mk-r> from "<U0072><U0078><U0072>"\n'
        b'order_start forward\n<U0062>\n<U0061> \\\nEND LC_COLLATE\n'
        b'<mk-r>\n{rule}UNDEFINED\norder_end\nEND LC_COLLATE\n')
_STEP2 = {
    'zz_EJ': (_K07.replace(b'{rule}', b''),
              _K07.replace(b'{rule}', b'<U0063>\n')),
    # 2.21: the section keywords indented; lr_token skips blanks first.
    'zz_IN': tuple(_HEAD + b'  LC_COLLATE\norder_start forward\n<U0041>\n'
                   + rule + b'\norder_end\n  END LC_COLLATE\n'
                   for rule in (b'<U0042>', b'<U0043>')),
    # The escape_char moved to the top level: zz_RB's run reads zz_RF's
    # LC_COLLATE with `/` now, and with `\\` before.
    'zz_RF': (_RETURNS['zz_RF'],
              b'escape_char /\n' + _RETURNS['zz_RF'].replace(
                  b'escape_char /\n', b'')),
    'zz_RB': (_RETURNS['zz_RB'],) * 2,
    'zz_RX': (_RETURNS['zz_RX'],) * 2,
}


@needs_clone
class StepTwoReadsWhereLocaledefReads(unittest.TestCase):
    """Step 2 on two commits on top of glibc-2.39 (_harness.glibc_commit):
    where a change sits against LC_COLLATE is decided by the sections
    glibc_reader read (glibc_tree.file_facts), so a change the line
    readers put outside the block, or in a file they found no block in, is
    counted as one, with its characters named. zz_IN is read and judged
    'collate', and zz_RF 'reading'. zz_EJ's section is read to its second END, as glibc reads
    it, but the reader does not settle an END inside a statement, so the
    file is counted under the `!!` that says so: listed, never cleared."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-step2-')
        _, cls.old = _harness.glibc_commit(
            NEW, {n: v[0] for n, v in _STEP2.items()}, cls.tmp)
        cls.repo, cls.new = _harness.glibc_commit(
            cls.old, {n: v[1] for n, v in _STEP2.items()}, cls.tmp)
        cls.out = os.path.join(cls.tmp, 'out')
        os.mkdir(cls.out)
        p = subprocess.run(
            [sys.executable, os.path.join(_harness.SCRIPTS_DIR,
                                          'filter_lc_collate_changes.py'),
             cls.old, cls.new, '--repo', cls.repo],
            cwd=_harness.SCRIPTS_DIR, capture_output=True,
            env=dict(os.environ, PG_GLIBC_AUDIT_OUT=cls.out))
        cls.rc = p.returncode
        cls.text = (p.stdout + p.stderr).decode('utf-8', 'replace')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_change_counts(self):
        self.assertEqual(0, self.rc, self.text)
        self.assertIn('Of the 3 content-changed file(s): 3 touch LC_COLLATE, '
                      '0 do not, 0 have no LC_COLLATE block', self.text)
        with open(os.path.join(self.out, 'step2_changed_collate.%s.txt'
                               % g.pair_slug(self.old, self.new))) as fh:
            self.assertEqual(['zz_EJ', 'zz_IN', 'zz_RF'], fh.read().split())

    def test_a_section_a_copier_reads_otherwise_is_named(self):
        """False-negative-reviewer, 15.2 B3: zz_RF's own run reads its
        LC_COLLATE with `/` at both commits, and zz_RB's run, which copies
        it, with `\\` at the first. Taken from the own run alone, the
        characters did not change and zz_RF was 'other'."""
        flat = _harness.flat(self.text)
        self.assertIn('!! 1 file(s) are read at %s with other comment or '
                      'escape characters than at %s' % (self.new, self.old),
                      flat)
        self.assertIn('localedata/locales/zz_RF comment_char # and '
                      'escape_char \\ and comment_char # and escape_char / '
                      'at %s comment_char # and escape_char / at %s'
                      % (self.old, self.new), flat)

    def test_the_one_the_reader_cannot_settle_says_why(self):
        self.assertIn('!! 1 file(s) could not be read the way localedef '
                      'reads them', _harness.flat(self.text))
        for ref in (self.old, self.new):
            self.assertIn('localedata/locales/zz_EJ at %s: zz_EJ:5: `END` '
                          'after the first token of a statement in '
                          'LC_COLLATE' % ref, _harness.flat(self.text))
        self.assertNotIn('zz_IN at', _harness.flat(self.text))

    def test_the_changed_rules_name_their_characters(self):
        self.assertIn('  localedata/locales/zz_EJ\n      characters in the '
                      'changed rules (1): c (U+0063)', self.text)
        self.assertIn('  localedata/locales/zz_IN\n      characters in the '
                      'changed rules (2): B (U+0042)  C (U+0043)', self.text)

    def test_the_line_readers_really_put_these_outside(self):
        """Control: each change is one the line readers placed outside a
        block, so the tests above cannot pass by testing what they already
        got right."""
        ej = _STEP2['zz_EJ'][0].decode('ascii')
        self.assertEqual((1, 6), g.collate_bounds(ej))
        self.assertIsNone(g.collate_bounds(_STEP2['zz_IN'][0].decode('ascii')))


@needs_clone
class FileFactsReadLikeGlibc(unittest.TestCase):
    """glibc_tree.file_facts: what step 2 asks of each file, from run()
    and nothing else, and kept only where it is the same answer."""

    @classmethod
    def setUpClass(cls):
        import glibc_tree
        cls.gt = glibc_tree
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-facts-')
        files = {n: v[0] for n, v in _STEP2.items()}
        files['zz_HD'] = (b'comment_char %\nescape_char /\nLC_CTYPE\n'
                          b'copy "i18n"\nEND LC_CTYPE\ncomment_c/har #\n'
                          b'LC_COLLATE\ncopy "iso14651_t1"\nEND LC_COLLATE\n'
                          b'escape_char \\\n')
        files['zz_CP'] = _HEAD + b'LC_COLLATE\ncopy "zz_EJ"\nEND LC_COLLATE\n'
        files['zz_MI'] = _HEAD + b'LC_COLLATE\ncopy "zz_NONE"\nEND LC_COLLATE\n'
        files.update(_RETURNS)
        cls.repo, cls.ref = _harness.glibc_commit(NEW, files, cls.tmp)
        cls.paths, cls.ids = g.list_tree_ids(cls.repo, cls.ref, [])
        cls.build = glibc_tree.build_at(cls.repo, cls.ref)
        # The suite's own directory, where the runs of this tree, one file
        # or two from NEW, are kept already (_harness.fill_reader_cache): a
        # directory of its own computed the whole tree again, more than
        # 600 s with the other classes running. What is kept here is named
        # after this tree, which no other class reads; the facts a run
        # before this one kept are dropped, so that cls.facts is computed
        # and test_a_kept_answer_is_the_answer compares two answers, not one
        # kept answer with itself (round 5 of the 15.2 B3 review).
        cls.cache = None
        try:
            os.unlink(glibc_tree._facts_path(cls.paths, cls.ids, cls.build,
                                             cls.cache))
        except FileNotFoundError:
            pass
        cls.facts = cls.ask()

    @classmethod
    def ask(cls):
        return cls.gt.file_facts(cls.repo, cls.ref, cls.paths, cls.ids,
                                 cls.build, cache_dir=cls.cache)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_an_end_joined_by_the_escape_does_not_end_the_section(self):
        """Read by its own run and by zz_CP's, the same lines."""
        self.assertEqual([(1, 10, 0x23, 0x5c, 'zz_CP'),
                          (1, 10, 0x23, 0x5c, 'zz_EJ')],
                         self.facts['zz_EJ'].collate)

    def test_an_indented_section_is_a_section(self):
        self.assertEqual([(3, 8, 0x25, 0x2f, 'zz_IN')],
                         self.facts['zz_IN'].collate)

    def test_a_directive_spelt_across_the_escape_reads_the_section(self):
        """get_ident drops the escape inside a word: line 6 is
        comment_char #, and it reads LC_COLLATE on line 7. The escape_char
        under the section acts after it, on nothing of it."""
        hd = self.facts['zz_HD']
        self.assertEqual([(7, 9, 0x23, 0x2f, 'zz_HD')], hd.collate)
        self.assertEqual((0x23, 0x2f), hd.chars_at(8))
        self.assertIsNone(hd.chars_at(11))

    def test_a_section_read_only_through_a_copy_is_the_file_s(self):
        """zz_EJ's section is the same lines and characters whether its
        own run or zz_CP's reads it: one pair, two runs."""
        ej = self.facts['zz_EJ']
        self.assertEqual([(0x23, 0x5c)], ej.reading())
        self.assertEqual({'zz_CP': ((0x23, 0x5c),), 'zz_EJ': ((0x23, 0x5c),)},
                         ej.runs())
        self.assertEqual([(3, 5, 0x25, 0x2f, 'zz_CP')],
                         self.facts['zz_CP'].collate)

    def test_a_section_each_run_reads_with_its_own_characters(self):
        """zz_RF's own run obeys the escape_char a run copying its
        LC_COLLATE skips: the section is read with both pairs, and a line
        in it has no single answer."""
        rf = self.facts['zz_RF']
        self.assertIsNone(rf.unresolved)
        self.assertEqual([(5, 7, 0x23, 0x5c, 'zz_RB'),
                          (5, 7, 0x23, 0x2f, 'zz_RF')], rf.collate)
        self.assertEqual({'zz_RB': ((0x23, 0x5c),), 'zz_RF': ((0x23, 0x2f),)},
                         rf.runs())
        self.assertIsNone(rf.chars_at(6))

    def test_a_run_that_does_not_finish_or_settle_is_named(self):
        self.assertEqual('localedef cannot open zz_NONE',
                         self.facts['zz_MI'].unresolved)
        self.assertEqual('zz_EJ:5: `END` after the first token of a statement '
                         'in LC_COLLATE', self.facts['zz_EJ'].unresolved)
        self.assertIsNone(self.facts['zz_IN'].unresolved)

    def test_the_answer_is_the_tree_s(self):
        """file_facts keeps nothing but what Tree's runs give."""
        tree = self.gt.Tree.at_tag(self.repo, self.ref, self.build)
        bad = tree.unresolved()
        for name in tree.names:
            got = self.facts[name]
            self.assertEqual(bad.get(name), got.unresolved, name)
            for sec in tree._collate_sections(name):
                if sec[0] == name:
                    self.assertTrue(got.collate, name)

    def kept(self, facts):
        return {n: (f.collate, f.unresolved) for n, f in facts.items()}

    def test_a_kept_answer_is_the_answer(self):
        self.assertEqual(self.kept(self.facts), self.kept(self.ask()))

    def test_a_kept_answer_of_another_shape_is_computed_again(self):
        path = self.gt._facts_path(self.paths, self.ids, self.build,
                                   self.cache)
        self.ask()
        with open(path) as fh:
            kept = json.load(fh)
        for bad in ({}, [], {'zz_EJ': [[], None]},
                    dict(kept, zz_EJ=[[[1, 'x', 35, 92, 'zz_EJ']], None]),
                    dict(kept, zz_EJ=[[[1, 10, 35, 92]], None]),
                    dict(kept, zz_EJ=[[[1, 10, '#', 92, 'zz_EJ']], None]),
                    dict(kept, zz_EJ=[[[1, 10, 35, None, 'zz_EJ']], None]),
                    dict(kept, zz_EJ=[[[1, 10, 35, 92, 7]], None]),
                    dict(kept, zz_EJ=[[], 7]),
                    dict(kept, zz_EJ=[[], None, None])):
            with open(path, 'w') as fh:
                json.dump(bad, fh)
            self.assertEqual(self.kept(self.facts), self.kept(self.ask()),
                             bad)
        with open(path, 'w') as fh:
            fh.write('{not json')
        self.assertEqual(self.kept(self.facts), self.kept(self.ask()))

    def test_a_kept_answer_is_used(self):
        """The control of the one above: an entry of the right shape is
        what comes back, so that test measures a refusal and not a
        recomputation that happens anyway."""
        path = self.gt._facts_path(self.paths, self.ids, self.build,
                                   self.cache)
        self.ask()
        with open(path) as fh:
            kept = json.load(fh)
        try:
            with open(path, 'w') as fh:
                json.dump(dict(kept, zz_EJ=[[[2, 3, 35, 92, 'zz_EJ']], None]),
                          fh)
            self.assertEqual([(2, 3, 35, 92, 'zz_EJ')],
                             self.ask()['zz_EJ'].collate)
        finally:
            os.unlink(path)

    def test_a_change_to_glibc_tree_starts_from_nothing(self):
        """The entry is named after glibc_tree.py's own hash: what _facts
        computes changes with that file, as runs change with the reader."""
        path = self.gt._facts_path(self.paths, self.ids, self.build,
                                   self.cache)
        self.assertIn(self.gt._SELF, os.path.basename(path))
        with mock.patch.object(self.gt, '_SELF', 'x' * 12):
            self.assertNotEqual(path, self.gt._facts_path(
                self.paths, self.ids, self.build, self.cache))

    def test_the_runs_it_reads_are_kept_where_it_is_told(self):
        """The runs of the tree are asked of cache_dir, with the facts
        (round 3 of 15.2 B3). Stopped at the ask: computing the whole
        tree in a directory of its own is what setUpClass avoids."""
        told = os.path.join(self.tmp, 'told')
        asked = []

        class Asked(Exception):
            pass

        def stop(files, build, cache_dir=None):
            asked.append(cache_dir)
            raise Asked
        with mock.patch.object(rc, 'tree_runs', stop), \
                self.assertRaises(Asked):
            self.gt.file_facts(self.repo, self.ref, self.paths, self.ids,
                               self.build, cache_dir=told)
        self.assertEqual([told], asked)

    def test_two_files_with_one_name_are_refused(self):
        """Reached by injection: localedata/locales has no subdirectory at
        any tag, and one with a file of a name the top level has would
        give two answers one key (round 2 of the 15.2 B3 review)."""
        dup = self.paths[0].replace('/locales/', '/locales/sub/')
        ids = dict(self.ids, **{dup: self.ids[self.paths[0]]})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.gt.file_facts(self.repo, self.ref, self.paths + [dup], ids,
                               self.build,
                               cache_dir=os.path.join(self.tmp, 'dup'))
        self.assertIn('share a name', err.getvalue())

    def test_a_listing_that_is_not_the_tree_is_refused(self):
        """Reached by injection: main hands file_facts the listing of the
        tag it reads. A file the listing lacks would be one nobody asks
        about. The suite's own directory, as setUpClass: in a directory of
        its own the whole tree was computed before the refusal. No entry is
        kept for this listing: the refusal comes first."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.gt.file_facts(self.repo, self.ref, self.paths[1:], self.ids,
                               self.build, cache_dir=self.cache)
        self.assertIn('are not the ones listed there', err.getvalue())

    def test_keyword_tables_a_tag_lacks_are_refused(self):
        """A ref without locfile-kw.gperf or charmap-kw.gperf (a fabricated
        repository, a tree of another layout) is named and refused, never
        read with tables from somewhere else."""
        real = g.read_blobs_at

        def fake(repo, groups, raw=False):
            return [({}, set(paths)) if any(p.endswith('-kw.gperf')
                                             for p in paths)
                    else real(repo, [(t, paths)], raw=raw)[0]
                    for t, paths in groups]
        err = io.StringIO()
        with mock.patch.object(g, 'read_blobs_at', fake), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            self.gt.builds_at(self.repo, [self.ref])
        self.assertIn("cannot read glibc's keyword tables at %s: "
                      "locale/programs/charmap-kw.gperf, "
                      "locale/programs/locfile-kw.gperf could not be read"
                      % self.ref, _harness.flat(err.getvalue()))

    def test_another_tree_or_build_is_another_entry(self):
        path = self.gt._facts_path(self.paths, self.ids, self.build,
                                   self.cache)
        ids = dict(self.ids)
        ids[self.paths[0]] = '0' * 40
        self.assertNotEqual(path, self.gt._facts_path(
            self.paths, ids, self.build, self.cache))
        other = self.gt.build_at(self.repo, MID)
        self.assertNotEqual(path, self.gt._facts_path(
            self.paths, self.ids, other, self.cache))


@needs_clone
class AcrossTheTwoReaders(unittest.TestCase):
    """glibc_tree.across: which files the localedef of 2.34 and that of
    2.39 read differently, from whole runs, and how the answer is kept."""

    FILES = {
        b'zz_U': (b'comment_char %\nescape_char /\nLC_COLLATE\n'
                  b'define \xc3\xa9X\nifdef \xc3\xa9X\norder_start forward\n'
                  b'<U0061>\n<U0062>\norder_end\nelse\norder_start forward\n'
                  b'<U0062>\n<U0061>\norder_end\nendif\nEND LC_COLLATE\n'),
        # Not ASCII only in a comment, which lr_token drops before reading
        # it: a suspect read alike.
        b'zz_C': (b'comment_char %\nescape_char /\n% caf\xc3\xa9\n'
                  b'LC_COLLATE\norder_start forward\n<U0061>\norder_end\n'
                  b'END LC_COLLATE\n'),
        b'zz_A': (b'comment_char %\nescape_char /\nLC_COLLATE\n'
                  b'order_start forward\n<U0061>\norder_end\nEND LC_COLLATE\n'),
        # ASCII only: codepoint_collation is a keyword from 2.35 on, a word
        # glibc-2.34 does not know before it.
        b'zz_K': b'LC_COLLATE\ncodepoint_collation\nEND LC_COLLATE\n',
    }

    @classmethod
    def setUpClass(cls):
        import glibc_tree
        cls.gt = glibc_tree
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-across-')
        cls.b0 = glibc_tree.build_at(GLIBC_CLONE, MID)
        cls.b1 = glibc_tree.build_at(GLIBC_CLONE, NEW)
        cls.ids = {k.decode(): rc.blob_id(v) for k, v in cls.FILES.items()}
        cls.loads = []

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ask(self, cache, files=None):
        def load():
            self.loads.append(1)
            return dict(files or self.FILES)
        return self.gt.across(self.ids, load, self.b0, self.b1,
                              cache_dir=os.path.join(self.tmp, cache))

    def test_the_runs_it_reads_are_kept_where_it_is_told(self):
        """The runs of the tree go to cache_dir with the answer, not to
        the directory the environment names (round 3 of 15.2 B3)."""
        env = os.path.join(self.tmp, 'env')
        with mock.patch.dict(os.environ, {'PG_GLIBC_READER_CACHE': env}):
            self.ask('told')
        self.assertFalse(os.path.exists(env))
        self.assertTrue(os.path.isdir(os.path.join(
            self.tmp, 'told', rc.VERSION, rc.build_id(self.b0))))

    def test_what_the_two_readers_read_differently(self):
        """zz_U by its bytes, zz_K by a keyword one table has; zz_C's
        UTF-8 is in a comment glibc drops unread, and zz_A is ASCII."""
        self.assertEqual(['zz_K', 'zz_U'], self.ask('a'))

    def test_the_other_way_round_reads_the_same_differences(self):
        """With 2.39's reader first, the bytes are met by the lr_getc of
        2.36 and after."""
        self.assertEqual(['zz_K', 'zz_U'], self.gt.across(
            self.ids, lambda: dict(self.FILES), self.b1, self.b0,
            cache_dir=os.path.join(self.tmp, 'rev')))

    def test_a_0x1a_inside_a_name_is_met(self):
        """The fast path for <names> reads the buffer without lr_getc: a
        0x1A there is an ordinary byte from 2.36 and the end of the file
        before it."""
        files = dict(self.FILES)
        files[b'zz_N'] = (b'comment_char %\nescape_char /\nLC_COLLATE\n'
                          b'order_start forward\n<U00\x1a61>\norder_end\n'
                          b'END LC_COLLATE\n')
        ids = {k.decode(): rc.blob_id(v) for k, v in files.items()}
        got = self.gt.across(ids, lambda: dict(files), self.b1, self.b0,
                             cache_dir=os.path.join(self.tmp, 'name'))
        self.assertIn('zz_N', got)

    def test_a_keyword_a_name_spells_is_compared(self):
        """Both builds read <zz_named> as a name, and the second table
        makes it a keyword: no run records it, so every run is compared,
        and zz_N's differs."""
        table = dict(self.b1.locfile_kw)
        table[b'zz_named'] = ('tok_order_end', 1)
        b2 = gr.Build(self.b1.pre236, self.b1.signed_char, table,
                      self.b1.charmap_kw, 'zz')
        files = dict(self.FILES)
        files[b'zz_N'] = (b'comment_char %\nescape_char /\nLC_COLLATE\n'
                          b'order_start forward\n<U0061>\n<zz_named>\n'
                          b'<U0062>\norder_end\nEND LC_COLLATE\n')
        ids = {k.decode(): rc.blob_id(v) for k, v in files.items()}
        got = self.gt.across(ids, lambda: dict(files), self.b1, b2,
                             cache_dir=os.path.join(self.tmp, 'named'))
        self.assertIn('zz_N', got)

    def test_a_kept_lookahead_counts_for_every_run_that_uses_it(self):
        """The lookahead of a statement is read once per Build and kept:
        a second run that reuses it reads no byte of it, and must still
        record what the first met and looked up."""
        common = (b'comment_char %\nescape_char /\nLC_COLLATE\n'
                  b'order_start forward\n<U0061> IGNORE;\xc3\xa9\n'
                  b'order_end\nEND LC_COLLATE\n')
        files = {b'zz_S': common,
                 b'zz_A': b'LC_COLLATE\ncopy "zz_S"\nEND LC_COLLATE\n',
                 b'zz_B': b'LC_COLLATE\ncopy "zz_S"\nEND LC_COLLATE\n'}
        build = _twin(self.b1)
        first = gr.run(b'zz_A', files.get, build)
        second = gr.run(b'zz_B', files.get, build)
        fresh = gr.run(b'zz_B', files.get, _twin(self.b1))
        self.assertTrue(first.met)
        self.assertTrue(build.lookahead, 'nothing was kept to reuse')
        self.assertEqual((fresh.met, fresh.words),
                         (second.met, second.words))
        self.assertIn(b'IGNORE', second.words)

    def test_a_run_not_settled_is_listed_whatever_it_spells(self):
        """zz_R is not settled (a repertoiremap directive) and spells
        codepoint_collation across an escaped newline: no search of its
        bytes finds the word, and glibc reads it. Every run not settled is
        listed when the builds differ (round 3 of the 15.2 B3 review)."""
        files = {b'zz_A': self.FILES[b'zz_A'],
                 b'zz_R': (b'repertoiremap foo\nLC_COLLATE\n'
                           b'codepoint_coll\\\nation\nEND LC_COLLATE\n')}
        ids = {k.decode(): rc.blob_id(v) for k, v in files.items()}
        # Builds that differ in that word alone, so that the bytes cannot
        # be what lists it.
        table = dict(self.b0.locfile_kw)
        table[b'codepoint_collation'] = self.b1.locfile_kw[
            b'codepoint_collation']
        b35 = gr.Build(self.b0.pre236, self.b0.signed_char, table,
                       self.b0.charmap_kw, '2.34 + codepoint_collation')
        self.assertEqual(['zz_R'], self.gt.across(
            ids, lambda: dict(files), self.b0, b35,
            cache_dir=os.path.join(self.tmp, 'unsettled')))

    def test_two_tags_on_one_side_of_2_36_are_still_asked(self):
        """Keyword tables differ between tags that read bytes alike
        (codepoint_collation arrives in 2.35, alt_mon in 2.27): step 2
        asks glibc_tree.across of every pair whose Builds differ."""
        import filter_lc_collate_changes as f
        table = dict(self.b0.locfile_kw)
        table[b'codepoint_collation'] = self.b1.locfile_kw[
            b'codepoint_collation']
        b35 = gr.Build(self.b0.pre236, self.b0.signed_char, table,
                       self.b0.charmap_kw, '2.34 + codepoint_collation')
        tmp = os.path.join(self.tmp, 'side')
        os.mkdir(tmp)
        repo, ref = _harness.glibc_commit(MID, {'zz_K': self.FILES[b'zz_K']},
                                          tmp)
        paths, ids = g.list_tree_ids(repo, ref, [])
        self.assertEqual(['localedata/locales/zz_K'],
                         f.read_across(repo, ref, (self.b0, b35), paths, ids))
        self.assertEqual([], f.read_across(repo, ref, (self.b0, self.b0),
                                           paths, ids))

    def test_what_a_file_met_survives_a_lookahead_that_stops(self):
        """A statement whose lookahead reaches a place glibc reads outside
        its buffer (GlibcUndefined) leaves from the middle of the lookahead:
        the bytes the file met before it are the run's all the same."""
        files = {b'x': (b'LC_COLLATE\norder_start forward\n'
                        b'<U0041> \xc3\xa9\n<U0042> foo \\\n-a\n'
                        b'order_end\nEND LC_COLLATE\n')}
        for build in (self.b0, self.b1):
            r = gr.run(b'x', files.get, _twin(build))
            self.assertTrue(r.unsettled, build.label)
            self.assertTrue(r.met, build.label)
            self.assertIn(b'foo', r.words)

    def test_a_keyword_read_as_a_name_counts_every_run(self):
        """No run records the <names> it looks up (Run.words), so a table
        whose difference is one of those must have every run compared."""
        table = dict(self.b1.locfile_kw)
        table[b'zz_named'] = ('tok_x', 1)
        b2 = gr.Build(self.b1.pre236, self.b1.signed_char, table,
                      self.b1.charmap_kw, 'zz')
        words, named = self.gt.keyword_words(self.b1, b2)
        self.assertEqual(({b'zz_named'}, True), (words, named))
        words, named = self.gt.keyword_words(self.b0, self.b1)
        self.assertEqual(({b'codepoint_collation'}, False), (words, named))

    def test_one_build_twice_reads_nothing_differently(self):
        self.assertEqual([], self.gt.across(
            self.ids, lambda: dict(self.FILES), self.b0, self.b0,
            cache_dir=os.path.join(self.tmp, 'same')))

    def test_a_kept_answer_is_used_without_reading_a_file(self):
        self.ask('k')
        before = len(self.loads)
        path = self.gt._across_path(self.ids, self.b0, self.b1,
                                    os.path.join(self.tmp, 'k'))
        with open(path, 'w') as fh:
            json.dump(['zz_A'], fh)
        self.assertEqual(['zz_A'], self.ask('k'))
        self.assertEqual(before, len(self.loads))

    def test_a_kept_answer_of_another_shape_is_computed_again(self):
        self.ask('s')
        path = self.gt._across_path(self.ids, self.b0, self.b1,
                                    os.path.join(self.tmp, 's'))
        for bad in ({}, 'zz_U', [7], ['zz_NOT_IN_THE_TREE']):
            with open(path, 'w') as fh:
                json.dump(bad, fh)
            self.assertEqual(['zz_K', 'zz_U'], self.ask('s'), bad)

    def test_another_tree_or_build_is_another_entry(self):
        path = self.gt._across_path(self.ids, self.b0, self.b1, self.tmp)
        self.assertNotEqual(path, self.gt._across_path(
            dict(self.ids, zz_A='0' * 40), self.b0, self.b1, self.tmp))
        self.assertNotEqual(path, self.gt._across_path(
            self.ids, self.b1, self.b0, self.tmp))
        self.assertNotEqual(path, self.gt._across_path(
            self.ids, self.b0, self.b0, self.tmp))
        self.assertIn(self.gt._SELF, os.path.basename(path))

    def test_files_that_are_not_the_listing_are_refused(self):
        err = io.StringIO()
        other = dict(self.FILES)
        other[b'zz_A'] += b'\n'
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.ask('r', files=other)
        self.assertIn('not the ones listed', err.getvalue())


@needs_floor_pair
class TreeAgreesWithTheOldReadersOnTheTags(unittest.TestCase):
    """On the five tags, glibc_tree gives steps 1, 3 and 4 the answers the
    line readers gave them, except where those read differently from
    localedef, and those exceptions are named here: at 2.12 and 2.17 the
    ranges in iso14651_t1_common are inside `#` comments (backlog 13.8), and
    17 runs meet reorder-sections-after, which the reader does not settle
    yet (backlog 15.3). Anything else that moves is a difference nobody
    measured.

    Step 2's view (glibc_tree.file_facts, backlog 15.2 B3) is held to the
    line readers' collate_bounds and reading_chars the same way: on these
    tags they agree on every file whose run is settled."""

    KNOWN = {
        FLOOR_OLD: {'only_old_ranges': {'iso14651_t1_common'},
                    'lost_roots': {'iso14651_t1_pinyin', 'zh_CN'},
                    'unresolved': 17},
        FLOOR_NEW: {'only_old_ranges': {'iso14651_t1_common'},
                    'lost_roots': {'iso14651_t1_pinyin', 'zh_CN'},
                    'unresolved': 17},
    }

    def test_each_tag(self):
        for tag in (FLOOR_OLD, FLOOR_NEW, OLD, MID, NEW):
            with self.subTest(tag):
                self.check(tag)

    def check(self, tag):
        import glibc_tree
        known = self.KNOWN.get(tag, {})
        tree = glibc_tree.Tree.at_tag(GLIBC_CLONE, tag)
        _, data = _tag_files(tag)
        texts = {n.decode('utf-8'): g.decoded(b) for n, b in data.items()}
        self.assertEqual(sorted(texts), tree.names)
        self.assertEqual(
            {n for n, t in texts.items() if g.collate_block(t) is not None},
            {n for n in tree.names if tree.defines(n)})
        # What scan_ellipsis answered, from the pieces steps 6 to 8 still
        # use (it was deleted with the last caller, backlog 15.2 B3).
        old_ranges = {}
        for n, t in texts.items():
            block = g.collate_text(t)
            hits = (g.ellipsis_hits(block, g.comment_char(t))
                    if block is not None else [])
            if hits:
                old_ranges[n] = hits
        new_ranges = {n: [t for _, t in tree.ranges(n)] for n in tree.names
                      if tree.ranges(n)}
        self.assertEqual(known.get('only_old_ranges', set()),
                         set(old_ranges) - set(new_ranges))
        self.assertEqual(set(), set(new_ranges) - set(old_ranges))
        for n in new_ranges:
            self.assertEqual(old_ranges[n], new_ranges[n], n)
        graph = g.copy_graph_from_texts(texts)
        old_in = g.inherited_from(graph, set(old_ranges))
        new_in = {n: tree.roots(n, set(new_ranges)) for n in tree.names}
        new_in = {n: r for n, r in new_in.items() if r}
        self.assertEqual(known.get('lost_roots', set()),
                         {n for n in old_in if old_in[n] != new_in.get(n)})
        self.assertEqual(set(), set(new_in) - set(old_in))
        self.assertEqual(g.byte_order_locales(texts), tree.byte_order())
        self.assertEqual(known.get('unresolved', 0), len(tree.unresolved()))
        self.assertEqual({}, tree.missing())
        for name in graph:
            if name not in tree.unresolved():
                self.assertEqual(graph[name], tree.copies(name), name)
            reach = set()
            stack = list(graph[name])
            while stack:
                cur = stack.pop()
                if cur not in reach and cur != name:
                    reach.add(cur)
                    stack.extend(graph.get(cur, ()))
            if name not in tree.unresolved():
                self.assertEqual(reach, tree.reach(name), name)
        paths, ids = g.list_tree_ids(GLIBC_CLONE, tag, [])
        facts = glibc_tree.file_facts(GLIBC_CLONE, tag, paths, ids,
                                      tree.build)
        self.assertEqual(set(texts), set(facts))
        for name, text in texts.items():
            got = facts[name]
            self.assertEqual(tree.unresolved().get(name), got.unresolved)
            if got.unresolved:
                continue
            ranges = sorted({(s, e) for s, e, _, _, _ in got.collate})
            self.assertEqual(g.collate_bounds(text),
                             ranges[0] if ranges else None, name)
            self.assertLessEqual(len(ranges), 1, name)
            # Every run that reads the file reads it with one pair: no file
            # at these tags hides a directive from a run that copies it.
            self.assertLessEqual(len(got.reading()), 1, name)
            chars = g.reading_chars(text)
            if got.collate and chars is not None:
                self.assertEqual([tuple(ord(c) for c in chars)],
                                 got.reading(), name)

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
