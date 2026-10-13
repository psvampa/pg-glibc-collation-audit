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
"""
import json
import os
import re
import sys
import tempfile
import unittest

import _harness  # also puts scripts/ on sys.path
from _harness import GLIBC_CLONE, MID, NEW, needs_clone

import glibc_reader as gr

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
# from glibc 2.36 on.
# c44 and c50 wait for the charmap reader (15.6), which says whether a
# string has a character the charmap lacks; c45 and c46, like c34, for the
# translit entry grammar (15.4). s41, s43 and s45 for the LC_COLLATE and
# LC_CTYPE statement grammars (15.3, 15.4): a range before order_start,
# and statements written into a struct the file shares while read for
# another category; s42 too on el9 and el10, where codepoint_collation is a
# keyword.
_LATER = {'s18', 's27', 's30', 's31', 'c34', 'c44', 'c45', 'c46', 'c50',
          's41', 's43', 's45'}
EXPECTED_UNSETTLED = {
    'rhel8': {'b18', 'b19'} | _LATER,
    'rhel9': {'b18', 'b19', 's42'} | _LATER,
    'rhel10': {'s42'} | _LATER,
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

    def test_written_collate_lists_every_block_and_an_open_one(self):
        text = (b'LC_COLLATE\nEND LC_COLLATE\nx\nLC_COLLATE\n'
                b'order_start forward\n')
        self.assertEqual([(1, 2), (4, None)], gr.written_collate(text))
        self.assertEqual([], gr.written_collate(b'  LC_COLLATE\n'))


if __name__ == '__main__':
    unittest.main()
