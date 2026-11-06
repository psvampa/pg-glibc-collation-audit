"""scripts/confirm_sql.py and `locale_order.py --compare --sql-to`.

The SQL files the comparison writes from the three recorded measurements
(tests/locale_order/rhel*.extract.tar.gz), and the reader of two servers'
outputs, on outputs PostgreSQL really printed for those very files
(tests/confirm_sql/, gzip), each run with the file on stdin as postgres,
stdout and stderr merged by the server's own shell (`psql -X -d DB -f -
2>&1`), nothing written in the database:

  rhel8-to-rhel9.UTF8.{el8,el9}.out        collaudit8 (glibc-2.28-251.el8_10.40)
                                           and collaudit9 (2.34-275.el9_8),
                                           PostgreSQL 18.6, database postgres
  rhel9-to-rhel10.UTF8.{el9,el10}.out      collaudit9 and collaudit10
                                           (2.39-128.el10_2), PostgreSQL 18.6
  rhel8-to-rhel9.UTF8.node{0,1}.pg1N.out   collaudittest node0 (el8, the same
                                           build as collaudit8) and node1 (el9,
                                           a replica of node0 never repaired,
                                           whose collations were made on 2.28:
                                           PostgreSQL warns), PostgreSQL 14.24,
                                           15.19, 16.15 and 17.11
  rhel8-to-rhel9.LATIN{1,9}.{el8,el9}.out  an environment of this change's own,
                                           el8 with PostgreSQL 18.6 and el9 with
                                           Percona's 17.11, glibc brought to the
                                           fixtures' builds by dnf downgrade,
                                           in databases LATIN1 and LATIN9
  rhel8-to-rhel9.UTF8.wrongdb.el8.out      the UTF8 file in that LATIN1 database
  rhel8-to-rhel9.UTF8.nolangpack.el8.out   that el8 with every langpack but
                                           English removed and PostgreSQL
                                           restarted: the collations stay in the
                                           catalog, their locales are gone
  rhel8-to-rhel9.UTF8.es.el9.out           that el9 with PostgreSQL's
                                           translations installed, the database
                                           and its collations marked as made on
                                           2.28 (as a replica's are), run with
                                           `-f "/tmp/confirm 8.1.sql"`, LANG and
                                           lc_messages es_ES: PostgreSQL warns
                                           per collation in Spanish, and on
                                           connecting in English

Besides step 11's pairs, the UTF8 files test the strings of several
characters that a locale's compiled sort table names as changed (a pair
from the two tables) and, under one collation, every answer of the
characters whose rules (LC_CTYPE) change.

The SQL files under tests/confirm_sql/ are the ones the comparison writes
today (WrittenFromTheRecordedMeasurements checks it byte for byte): an
output names the file it answers by its id, a hash of the file, so a change
to what the files say needs the outputs recorded again.

A refusal is asserted by its message, and every case of a refusal is a
real output with the one thing the case names changed.
"""
import gzip
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import _harness  # also puts scripts/ on sys.path

import confirm_sql as c
import locale_order as m

DATA = os.path.join(_harness.TESTS_DIR, 'confirm_sql')
MEASURED = os.path.join(_harness.TESTS_DIR, 'locale_order')
SCRIPT = os.path.join(_harness.SCRIPTS_DIR, 'confirm_sql.py')
LOCALE_ORDER = os.path.join(_harness.SCRIPTS_DIR, 'locale_order.py')
PAIRS = {'89': 'rhel8-to-rhel9', '910': 'rhel9-to-rhel10'}

_TMP = tempfile.mkdtemp(prefix='confirm_sql_tests_')


def tearDownModule():
    shutil.rmtree(_TMP, True)


def recorded(name):
    """A file of tests/confirm_sql/, ungzipped once into a folder of this
    module: its path."""
    path = os.path.join(_TMP, name)
    if not os.path.exists(path):
        with gzip.open(os.path.join(DATA, name + '.gz'), 'rb') as src, \
                open(path, 'wb') as f:
            shutil.copyfileobj(src, f)
    return path


def measurement(n):
    """The measurement inside tests/locale_order/rhelN.extract.tar.gz."""
    path = os.path.join(_TMP, f'rhel{n}.out')
    if not os.path.exists(path):
        with tarfile.open(os.path.join(MEASURED, f'rhel{n}.extract.tar.gz'),
                          'r:gz') as t:
            member = t.getmember('locale_order.out')
            with t.extractfile(member) as src, open(path, 'wb') as f:
                shutil.copyfileobj(src, f)
    return path


def sql(pair, encoding):
    return recorded(f'{PAIRS[pair]}.{encoding}.sql')


def out(pair, encoding, where):
    return recorded(f'{PAIRS[pair]}.{encoding}.{where}.out')


def mutant(path, change, name):
    """A copy of the output at path with change(text) applied, which must
    change it."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    new = change(text)
    if new == text:
        raise AssertionError(f'the change for {name} changed nothing')
    dest = os.path.join(_TMP, name)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(new)
    return dest


def read(sql_path, old, new):
    """(exit status, stdout, stderr) of `confirm_sql.py read`."""
    p = subprocess.run([sys.executable, SCRIPT, 'read', sql_path, old, new],
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def refusals(stdout):
    """The reasons under REFUSED, one per line, or None when not refused."""
    lines = stdout.split('\n')
    if not any(x.startswith('REFUSED: ') for x in lines):
        return None
    start = next(i for i, x in enumerate(lines) if x.startswith('REFUSED: '))
    out = []
    for line in lines[start + 1:]:
        if not line.startswith('  '):
            break
        out.append(line[2:])
    return out


def confirmed(stdout):
    """(tests, total) of the Confirmed line, or None."""
    m_ = re.search(r'^Confirmed in PostgreSQL: (\d+) of (\d+) tests ', stdout,
                   re.M)
    return (int(m_.group(1)), int(m_.group(2))) if m_ else None


# --- the files the comparison writes ----------------------------------------

class WrittenFromTheRecordedMeasurements(unittest.TestCase):
    """--sql-to on the two RHEL pairs: which files, what each tests, under
    which names, what each says it does not test; counts and names parsed
    from the files, and the files the recorded outputs answer."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(dir=_TMP)
        cls.runs = {}
        for pair, (a, b) in (('89', (8, 9)), ('910', (9, 10))):
            prefix = os.path.join(cls.dir, f'confirm.{pair}')
            # The characters file of the same run, for the 8 -> 9 check below.
            p = subprocess.run(
                [sys.executable, LOCALE_ORDER, '--compare', measurement(a),
                 measurement(b), '--sql-to', prefix, '--characters-to',
                 os.path.join(cls.dir, f'chars.{pair}.tsv')],
                capture_output=True, text=True,
                env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            cls.runs[pair] = (p.returncode, p.stdout, p.stderr, prefix)

    def written(self, pair):
        rc, stdout, stderr, prefix = self.runs[pair]
        self.assertEqual((rc, stderr), (0, ''))
        base = os.path.basename(prefix)
        return {re.fullmatch(re.escape(base) + r'\.(\w+)\.sql', x).group(1):
                os.path.join(self.dir, x) for x in os.listdir(self.dir)
                if x.startswith(base + '.')}

    def test_one_file_per_database_encoding(self):
        self.assertEqual(sorted(self.written('89')),
                         ['LATIN1', 'LATIN9', 'UTF8'])
        self.assertEqual(sorted(self.written('910')), ['UTF8'])

    def test_the_report_names_each_file_with_its_count(self):
        for pair in ('89', '910'):
            rc, stdout, stderr, prefix = self.runs[pair]
            named = {path: (enc, int(n.replace(',', '')))
                     for enc, n, path in re.findall(
                         r'^  (\w+) database, ([\d,]+) test\(s\):\n'
                         r'    (\S+)$', stdout, re.M)}
            files = self.written(pair)
            self.assertEqual(sorted(named), sorted(files.values()))
            for path, (enc, count) in named.items():
                self.assertEqual(files[enc], path)
                self.assertEqual(c.Sql(path).tests, count)
            self.assertIn('  python3 scripts/confirm_sql.py read FILE old.out '
                          'new.out', stdout.split('\n'))

    def test_what_each_file_tests(self):
        """Distinct pairs per file, and the names each runs them under:
        every name `locale -a` gives the group and the alias PostgreSQL
        makes for it in that encoding."""
        got = {}
        for pair in ('89', '910'):
            for enc, path in self.written(pair).items():
                s = c.Sql(path)
                got[pair, enc] = {name: len(tests) for name, (line, tests)
                                  in s.collations.items()}
        self.assertEqual(got, {
            ('89', 'UTF8'): {'C.utf8': 1502, 'ko_KR': 1, 'ko_KR.utf8': 1,
                             'or_IN': 5859, 'or_IN.utf8': 5859, 'sv_FI': 2,
                             'sv_FI.utf8': 2, 'sv_SE': 2, 'sv_SE.utf8': 2,
                             'en_US.utf8': 25531},
            ('89', 'LATIN1'): {'sv_FI': 1, 'sv_FI.iso88591': 1, 'sv_SE': 1,
                               'sv_SE.iso88591': 1, 'swedish': 1},
            ('89', 'LATIN9'): {'sv_FI.iso885915@euro': 1, 'sv_FI@euro': 1,
                               'sv_SE': 1, 'sv_SE.iso885915': 1},
            ('910', 'UTF8'): {'th_TH': 4530, 'th_TH.utf8': 4530,
                              'en_US.utf8': 23814}})

    def notes(self, pair, enc):
        return c.Sql(self.written(pair)[enc]).notes

    def test_pairs_a_database_cannot_store_are_counted(self):
        self.assertIn('not tested: C.utf8: 215 pair(s) whose strings a UTF8 '
                      'database cannot store', self.notes('89', 'UTF8'))

    def test_a_change_without_a_pair_is_named_with_why(self):
        notes = self.notes('89', 'UTF8')
        for name, count, why in (
                ('C.utf8', '2,721', 'pair(s) of neighbours the measurement '
                                    'cannot decide'),
                ('or_IN, or_IN.utf8', '1,055', 'chain(s) of neighbours told '
                 'apart differently at no level a measured pair compares'),
                ('or_IN, or_IN.utf8', '8,406', 'pair(s) of neighbours the '
                 'measurement cannot decide')):
            with self.subTest(name=name, why=why):
                self.assertIn(f'not tested: {name}: {count} {why}', notes)

    def test_a_locale_postgresql_does_not_import_is_named_in_every_file(self):
        rc, stdout, stderr, prefix = self.runs['910']
        line = ('not checked in PostgreSQL by this file: th_TH, th_TH.tis620, '
                'thai: PostgreSQL imports no collation of its encoding, '
                'TIS-620')
        self.assertIn(line, self.notes('910', 'UTF8'))
        self.assertIn('Not checked in PostgreSQL by these files: th_TH, '
                      'th_TH.tis620, thai:', stdout)

    def test_a_locale_not_on_the_new_machine_is_named_in_every_file(self):
        for enc in ('UTF8', 'LATIN1', 'LATIN9'):
            with self.subTest(encoding=enc):
                self.assertIn('not checked in PostgreSQL by this file: '
                              'en_US.utf8@ampm, en_US@ampm: it is not on the '
                              'new machine', self.notes('89', enc))

    def test_the_names_that_disappear_are_a_list_not_a_test(self):
        for pair, names in (('89', ["('en_US.utf8@ampm'), ('en_US@ampm')"]),
                            ('910', ["('aa_ER.utf8@saaho'), ('aa_ER@saaho')"])):
            with open(self.written(pair)['UTF8']) as f:
                text = f.read()
            self.assertIn(f'JOIN (VALUES {names[0]}) AS g(name)', text)

    def test_every_pair_is_one_the_characters_file_names(self):
        """The pairs of the UTF8 file of 8 -> 9, against the test column
        of the characters file the same comparison writes, read the other
        way: tokens to bytes, the two parts joined as step 11 measured
        them. The file's strings carry a space where the measurement had
        SEPARATOR, so the file is never what the SQL is made from."""
        chars = os.path.join(self.dir, 'chars.89.tsv')
        self.assertEqual(self.runs['89'][0], 0)

        def to_bytes(text):
            parts, tokens = [], text.split(' ')
            i = 0
            while i < len(tokens):
                if tokens[i].startswith('U+'):
                    parts.append(chr(int(tokens[i][2:], 16)).encode('utf-8'))
                    i += 1
                else:
                    self.assertEqual(tokens[i], 'bytes')
                    j = i + 1
                    while j < len(tokens) and re.fullmatch('[0-9A-F]{2}',
                                                           tokens[j]):
                        j += 1
                    parts.append(bytes.fromhex(''.join(tokens[i + 1:j])))
                    i = j
            return m.SEPARATOR.join(parts)

        from_file, unstorable = set(), 0
        with open(chars, encoding='utf-8') as f:
            for line in f:
                cols = line.rstrip('\n').split('\t')
                if len(cols) != 8 or cols[1] not in ('moved',
                                                     'told apart differently'):
                    continue
                if cols[4] == '-' or 'UTF-8' not in self.codeset_of(cols[0]):
                    continue
                a, b = (to_bytes(x) for x in cols[4].split(' / '))
                try:
                    a.decode('utf-8'), b.decode('utf-8')
                except UnicodeDecodeError:
                    unstorable += 1
                    continue
                from_file.add((cols[0], a.hex(), b.hex(), cols[5], cols[6]))
        in_sql = set()
        with open(self.written('89')['UTF8']) as f:
            group = None
            for line in f:
                m_ = re.match(r'-- collation (\S+): (.*)$', line)
                if m_:
                    group = m_.group(2).replace(', ', ',')
                m_ = re.match(r"\(\d+, '([0-9a-f]+)', '([0-9a-f]+)', "
                              r"'([<>])', '([<>])', '(both|old|new)'", line)
                if m_:
                    in_sql.add((group,) + m_.groups()[:4])
        self.assertEqual(len(in_sql), 6493)
        self.assertEqual(unstorable, 215)
        self.assertEqual(in_sql, from_file)

    def rows_of(self, pair):
        """{(collation, kind): [the VALUES lines]} of the UTF8 file, kind
        'table' for a table's pair, 'ctype' for an LC_CTYPE row."""
        got = {}
        with open(self.written(pair)['UTF8']) as f:
            name = None
            for line in f:
                m_ = re.match(r'-- collation (\S+): ', line)
                if m_:
                    name = m_.group(1)
                if re.match(r"\(\d+, '[0-9a-f]+', '[0-9a-f]+', '[<>]', "
                            r"'[<>]', 'table'\)", line):
                    got.setdefault((name, 'table'), []).append(line)
                elif c.CTYPE_ROW.fullmatch(line.rstrip('\n')):
                    got.setdefault((name, 'ctype'), []).append(line)
        return got

    def characters_file(self, pair):
        """The rows of the characters file of the same comparison."""
        self.assertEqual(self.runs[pair][0], 0)
        with open(os.path.join(self.dir, f'chars.{pair}.tsv'),
                  encoding='utf-8') as f:
            return [line.rstrip('\n').split('\t') for line in f
                    if not line.startswith('#')]

    def test_a_table_names_strings_of_several_characters_with_a_pair(self):
        """Each string of several characters a changed table names gets one
        pair, the string against a single character or its own first
        character followed by one, under every name of its group; the
        characters file, read apart, says which strings those are."""
        for pair, group, names in (
                ('89', 'or_IN,or_IN.utf8', ('or_IN', 'or_IN.utf8')),
                ('910', 'th_TH.utf8', ('th_TH', 'th_TH.utf8'))):
            several = set()
            for cols in self.characters_file(pair):
                if (cols[0] == group and cols[1] == 'sort table changed'
                        and re.fullmatch(r'U\+\w+( U\+\w+)+', cols[2])):
                    several.add(''.join(chr(int(x[2:], 16)) for x in
                                        cols[2].split(' ')).encode('utf-8'))
            rows = self.rows_of(pair)
            for name in names:
                with self.subTest(pair=pair, name=name):
                    got = [re.match(r"\(\d+, '(\w+)', '(\w+)'", x).groups()
                           for x in rows[name, 'table']]
                    strings = [bytes.fromhex(a) for a, b in got]
                    self.assertEqual(len(strings), 871)
                    self.assertEqual(set(strings), several)
                    for a, b in got:
                        a_, b_ = (bytes.fromhex(x).decode() for x in (a, b))
                        self.assertTrue(len(b_) == 1 or (
                            len(b_) == 2 and b_[0] == a_[0]), (a_, b_))

    def test_every_other_sequence_a_table_names_is_counted(self):
        """The count of what no test takes, against the characters file:
        every sequence its rows name (a run U+x..U+y is that many), less
        the pairs of the table in the file."""
        for pair, group, label, name in (
                ('89', 'or_IN,or_IN.utf8', 'or_IN, or_IN.utf8', 'or_IN'),
                ('89', 'sv_FI.utf8,sv_SE.utf8', 'sv_FI.utf8, sv_SE.utf8',
                 None),
                ('89', 'ko_KR.utf8', 'ko_KR.utf8', None),
                ('910', 'th_TH.utf8', 'th_TH.utf8', 'th_TH.utf8')):
            named = 0
            for cols in self.characters_file(pair):
                if cols[0] != group or cols[1] != 'sort table changed' \
                        or cols[2] == '-':
                    continue
                run = re.fullmatch(r'U\+(\w+)\.\.U\+(\w+)', cols[2])
                named += (int(run.group(2), 16) - int(run.group(1), 16) + 1
                          if run else 1)
            tested = len(self.rows_of(pair).get((name, 'table'), ()))
            with self.subTest(group=group):
                self.assertIn(f'not tested: {label}: {named - tested:,} '
                              'sequence(s) its compiled sort table names as '
                              'changed, which no test here takes one by one',
                              self.notes(pair, 'UTF8'))

    def test_the_characters_whose_rules_change_under_one_collation(self):
        """Every answer of upper(), lower() and the classes that differs,
        against the LC_CTYPE rows of the characters file read apart; under
        en_US.utf8 alone, the others of its group counted."""
        for pair in ('89', '910'):
            want = set()
            members = None
            for cols in self.characters_file(pair):
                if cols[1] == 'LC_CTYPE group':
                    members = cols[0].split(',')
                if cols[1] != 'LC_CTYPE changed':
                    continue
                cp = int(cols[2][2:], 16)
                for part in cols[3].split('; '):
                    what, change = part.split(': ', 1)
                    if what in ('upper', 'lower'):
                        want.add((cp, 'text', what))
                    else:
                        was, now = (set(x.split()) - {'none'}
                                    for x in change.split(' -> '))
                        want |= {(cp, 'class', k) for k in was ^ now}
            rows = self.rows_of(pair)
            with self.subTest(pair=pair):
                self.assertEqual([k for k in rows if k[1] == 'ctype'],
                                 [('en_US.utf8', 'ctype')])
                got = [c.CTYPE_ROW.fullmatch(x.rstrip('\n')).groups()
                       for x in rows['en_US.utf8', 'ctype']]
                self.assertEqual(
                    {(int(cp, 16), kind, f) for i, kind, f, cp, o, n in got
                     if f != 'initcap'}, want)
                self.assertTrue(all(o != n for i, kind, f, cp, o, n in got))
                self.assertIn(
                    f'not tested: character rules (LC_CTYPE) of '
                    f'{len(members) - 1:,} more locale(s), not run one by '
                    f'one: step 11 found in each the changes it found in '
                    f'en_US.utf8, which this file tests (the LC_CTYPE group '
                    f'of step 11\'s characters file names them)',
                    self.notes(pair, 'UTF8'))

    CODESETS = None

    def codeset_of(self, label):
        if self.CODESETS is None:
            data = m.load(measurement(8), m.corpus_digest())[0]
            type(self).CODESETS = {n: r.get('codeset') or ''
                                   for n, r in data['locales'].items()}
        return {self.CODESETS[n] for n in label.split(',')}

    def test_the_recorded_sql_files_are_the_ones_written_today(self):
        """The outputs under tests/confirm_sql/ answer these files; a file
        written otherwise today has outputs nobody recorded."""
        for pair in ('89', '910'):
            for enc, path in self.written(pair).items():
                with self.subTest(pair=pair, encoding=enc):
                    with open(path, 'rb') as a, open(sql(pair, enc), 'rb') as b:
                        self.assertEqual(a.read(), b.read())


class StaleFiles(unittest.TestCase):
    """A file an earlier run left under the prefix is gone whatever this
    run does, so that it never reads as this run's."""

    def test_a_refused_run_leaves_no_earlier_file(self):
        d = tempfile.mkdtemp(dir=_TMP)
        prefix = os.path.join(d, 'x')
        for name in ('x.LATIN2.sql', 'x.UTF8.sql.partial', 'y.UTF8.sql'):
            with open(os.path.join(d, name), 'w') as f:
                f.write('old')
        p = subprocess.run([sys.executable, LOCALE_ORDER, '--compare',
                            measurement(8), measurement(8), '--sql-to',
                            prefix], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('the two files hold the same measurement', p.stderr)
        self.assertEqual(os.listdir(d), ['y.UTF8.sql'])

    def test_sql_to_goes_with_compare(self):
        p = subprocess.run([sys.executable, LOCALE_ORDER, '--sql-to', 'x'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('--sql-to', p.stderr)
        self.assertIn('go with --compare', p.stderr)


# --- what the writer refuses to write ---------------------------------------

class Writer(unittest.TestCase):
    MACHINES = ({'label': 'old build', 'version': '2.28'},
                {'label': 'new build', 'version': '2.34'})

    def group(self, **kw):
        g = {'locales': ['xx_YY.utf8'], 'collations': ['xx_YY.utf8'],
             'tests': [(b'a', b'b', '<', '>', 'both')], 'untested': [],
             'unstorable': 0}
        g.update(kw)
        return g

    def test_a_pair_both_machines_answer_alike_is_a_bug(self):
        with self.assertRaisesRegex(ValueError, 'do not answer apart'):
            c.sql_text('UTF8', *self.MACHINES,
                       [self.group(tests=[(b'a', b'b', '<', '<', 'both')])])

    def test_a_name_that_needs_quoting_is_never_written(self):
        for name in ('x"y', "x'y", 'x\\y', 'x|y', 'x y'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    c.sql_text('UTF8', *self.MACHINES,
                               [self.group(collations=[name])])

    def test_the_id_is_the_hash_of_the_file(self):
        text, ident, tests = c.sql_text('UTF8', *self.MACHINES, [self.group()])
        self.assertEqual(tests, 1)
        self.assertEqual(text.count(ident), 3)
        path = os.path.join(_TMP, 'one.sql')
        with open(path, 'w') as f:
            f.write(text)
        self.assertEqual(c.Sql(path).ident, ident)
        with open(path, 'w') as f:
            f.write(text.replace("'b'", "'c'", 1) if "'b'" in text
                    else text.replace('6162', '6163'))
        with self.assertRaises(SystemExit):
            c.Sql(path)

    def test_a_group_with_no_pair_a_database_stores_gets_no_file(self):
        """confirm_groups: no file reads "0 of 0"; the group is named."""
        old = {'glibc_version': 'glibc 2.28', 'glibc_build': 'o'}
        new = {'glibc_version': 'glibc 2.34', 'glibc_build': 'n'}
        files, not_checked = m.confirm_groups(
            [(['C.utf8'], 'UTF-8',
              [('test', (b'\x80',), (b'a',), '<', '>', 'both'),
               ('untested', 3, 'pair(s) of neighbours the measurement cannot '
                'decide')])], {'C.utf8': (None, 'C.UTF-8')}, old, new, [],
            (), {'groups': [], 'why': None, 'unknown': []})
        self.assertEqual(files, [])
        self.assertEqual(not_checked, [(['C.utf8'], 'none of its 1 test '
                                        'pair(s) can be stored in a UTF8 '
                                        'database; 3 pair(s) of neighbours '
                                        'the measurement cannot decide')])

    def ctype(self, *rows):
        return c.sql_text('UTF8', *self.MACHINES, [self.group(
            tests=[], collations=['en_US.utf8'], ctype=list(rows))])

    def test_an_lc_ctype_row_both_machines_answer_alike_is_a_bug(self):
        for row in ((0x282, 'text', 'upper', 'ca82', 'ca82'),
                    (0x282, 'class', 'alpha', 't', 't')):
            with self.subTest(row=row):
                with self.assertRaisesRegex(ValueError, 'do not answer apart'):
                    self.ctype(row)

    def test_an_lc_ctype_row_of_the_wrong_kind_is_a_bug(self):
        """A class answered as text, a function answered t or f, a name of
        neither: each would be asked as the other, and answer like neither
        machine on every server."""
        for row in ((0x282, 'text', 'alpha', 'ca82', 'ea9f85'),
                    (0x282, 'class', 'initcap', 't', 'f'),
                    (0x282, 'class', 'upper', 'ca82', 'ea9f85'),
                    (0x282, 'text', 'upper', 't', 'f'),
                    (0x282, 'text', 'title', 'ca82', 'ea9f85')):
            with self.subTest(row=row):
                with self.assertRaisesRegex(ValueError, 'do not answer apart'):
                    self.ctype(row)

    def test_lc_ctype_rows_only_in_a_utf8_file(self):
        with self.assertRaisesRegex(ValueError, 'do not answer apart'):
            c.sql_text('LATIN1', *self.MACHINES, [self.group(
                tests=[], collations=['sv_SE'],
                ctype=[(0xE9, 'text', 'upper', 'c389', 'c3a9')])])

    def test_a_class_and_a_function_of_one_name_are_asked_apart(self):
        """[[:upper:]] by a regular expression, upper() as text: the kind
        of each row, in the file, is what the query goes by."""
        text, ident, tests = self.ctype(
            (0xA7BA, 'class', 'upper', 'f', 't'),
            (0xA7BA, 'text', 'lower', 'ea9eba', 'ea9ebb'))
        self.assertEqual(tests, 2)
        self.assertIn("(1, 'class', 'upper', 'A7BA', 'ea9eba', 'f', 't')",
                      text)
        self.assertIn("CASE WHEN k = 'class' THEN CASE WHEN x COLLATE "
                      "\"en_US.utf8\" ~ ('^[[:' || f || ':]]$')", text)
        path = os.path.join(_TMP, 'kinds.sql')
        with open(path, 'w') as f:
            f.write(text)
        self.assertEqual(c.Sql(path).collations['en_US.utf8'][1],
                         {1: ('f', 't'), 2: ('ea9eba', 'ea9ebb')})

    def test_the_collation_an_lc_ctype_group_runs_under(self):
        for members, want in (
                (['C.utf8', 'aa_DJ.utf8', 'en_US.utf8'], 'en_US.utf8'),
                (['C.utf8', 'de_DE.utf8', 'aa_DJ.utf8'], 'aa_DJ.utf8'),
                (['C.utf8', 'POSIX', 'x"y.utf8'], None)):
            with self.subTest(members=members):
                self.assertEqual(m.ctype_collation(members), want)


class CtypeNotes(unittest.TestCase):
    """confirm_groups on what ctype_texts gives: each way LC_CTYPE is not
    tested is said in the UTF8 file, or named among what no file checks,
    which the report prints whether or not a file is written; ctype_tests
    is replaced by the rows each case needs."""
    OLD = {'glibc_version': 'glibc 2.28', 'glibc_build': 'o', 'ctype': None}
    NEW = {'glibc_version': 'glibc 2.34', 'glibc_build': 'n', 'ctype': None}
    SORT = (['xx_YY.utf8'], 'UTF-8',
            [('test', (b'a',), (b'b',), '<', '>', 'both')])
    ROW = (0x282, 'text', 'upper', 'ca82', 'ea9f85')

    def files(self, ctype, rows=(ROW,), skipped=0, sort=True):
        saved = m.ctype_tests
        m.ctype_tests = lambda *a: (list(rows), skipped)
        try:
            files, not_checked = m.confirm_groups(
                [self.SORT] if sort else [], {}, self.OLD, self.NEW, [],
                (), ctype)
        finally:
            m.ctype_tests = saved
        notes = []
        for enc, text, ident, tests in files:
            path = os.path.join(_TMP, f'ctype-notes.{enc}.sql')
            with open(path, 'w') as f:
                f.write(text)
            notes += c.Sql(path).notes
        return files, not_checked, notes

    def group(self, *names):
        return (list(names), {x: ('ok', 'nk') for x in names})

    def test_why_none_was_compared_is_said(self):
        why = ('the OLD file was written by an earlier version of '
               'locale_order.py, which did not measure this; run it again on '
               'the old machine')
        for sort in (True, False):
            with self.subTest(file=sort):
                files, not_checked, notes = self.files(
                    {'groups': [], 'why': why, 'unknown': []}, sort=sort)
                self.assertIn((['the character rules (LC_CTYPE) of every '
                                'locale'], why), not_checked)
                self.assertEqual(len(files), int(sort))
                if sort:
                    self.assertIn('not checked in PostgreSQL by this file: '
                                  'the character rules (LC_CTYPE) of every '
                                  f'locale: {why}', notes)
                self.assertIn('the character rules (LC_CTYPE) of every '
                              f'locale: {why}', '\n'.join(
                                  ' '.join(x.split()) for x in
                                  [' '.join(m.sql_lines('p', files,
                                                        not_checked))]))

    def test_an_earlier_file_gives_its_reason(self):
        """ctype_texts on a measurement that did not measure LC_CTYPE: the
        reason the SQL files get."""
        import types
        model = types.SimpleNamespace(machines=[], views={})
        found, rows, sql = m.ctype_texts({'held': []}, {'held': ['ctype']},
                                         [], set(), model)
        self.assertEqual(sql['groups'], [])
        self.assertRegex(sql['why'], r'^the OLD file was written by an '
                                     r'earlier version of locale_order\.py')

    def model(self, rule):
        """A stand-in for TextModel with one locale, ab_CD.utf8, whose
        table on each machine is rule(side); no encoding a UTF8 database
        or EUC reads, no converter."""
        import types
        machines = [types.SimpleNamespace(
            side=side, rule=lambda name, side=side: rule(side),
            forward=lambda codeset: types.SimpleNamespace(state='none'))
            for side in ('old', 'new')]
        return types.SimpleNamespace(
            machines=machines, views={'ab_CD.utf8': []},
            serves_utf8=lambda name: False,
            old=types.SimpleNamespace(data={}),
            new=types.SimpleNamespace(data={}))

    def test_ctype_texts_hands_on_what_it_could_not_compare(self):
        """A locale whose rules one machine could not measure is handed to
        the SQL files, which name it; and with no locale a UTF8 database can
        use compared, why is said."""
        held = {'held': ['ctype'], 'ctype': {}}
        found, rows, sql = m.ctype_texts(
            held, held, ['ab_CD.utf8'], set(),
            self.model(lambda side: (None, 'not measured') if side == 'new'
                       else (('ok', 'ob'), None)))
        self.assertEqual(sql['unknown'], ['ab_CD.utf8'])
        self.assertEqual(sql['groups'], [])
        self.assertEqual(sql['why'], 'no locale a UTF8 database can use was '
                                     'compared')

    def test_locales_not_compared_are_named(self):
        for sort in (True, False):
            with self.subTest(file=sort):
                files, not_checked, notes = self.files(
                    {'groups': [], 'why': None,
                     'unknown': ['ab_CD.utf8', 'ef_GH']}, sort=sort)
                self.assertIn((['ab_CD.utf8', 'ef_GH'], 'its character rules '
                               '(LC_CTYPE): step 11 could not compare them on '
                               'both machines'), not_checked)

    def test_code_points_no_utf8_text_holds_are_counted(self):
        files, not_checked, notes = self.files(
            {'groups': [self.group('en_US.utf8')], 'why': None,
             'unknown': []}, skipped=3)
        self.assertIn('not tested: character rules (LC_CTYPE) of 3 code '
                      'point(s) that no UTF8 text holds', notes)

    def test_a_group_with_no_name_to_run_under(self):
        files, not_checked, notes = self.files(
            {'groups': [self.group('C.utf8', 'POSIX')], 'why': None,
             'unknown': []})
        self.assertIn((['C.utf8', 'POSIX'], 'its character rules (LC_CTYPE): '
                       'every name of it is C.* or POSIX, which PostgreSQL '
                       'may answer itself, cannot be written into a SQL '
                       'file, or already runs sort tests in this file'),
                      not_checked)
        self.assertFalse(any('LC_CTYPE) of 1 more' in x for x in notes))

    def test_a_group_with_no_row_is_not_said_tested(self):
        files, not_checked, notes = self.files(
            {'groups': [self.group('en_US.utf8', 'fr_FR.utf8')], 'why': None,
             'unknown': []}, rows=())
        self.assertIn((['en_US.utf8', 'fr_FR.utf8'], 'its character rules '
                       '(LC_CTYPE): no change a UTF8 database can hold'),
                      not_checked)
        self.assertFalse(any('which this file tests' in x for x in notes))

    def test_the_other_locales_of_a_group_are_counted(self):
        files, not_checked, notes = self.files(
            {'groups': [self.group('en_US.utf8', 'fr_FR.utf8', 'de_DE')],
             'why': None, 'unknown': []})
        self.assertIn('not tested: character rules (LC_CTYPE) of 2 more '
                      'locale(s), not run one by one: step 11 found in each '
                      'the changes it found in en_US.utf8, which this file '
                      'tests (the LC_CTYPE group of step 11\'s characters file '
                      'names them)', notes)

    def test_rows_never_under_a_name_a_sort_group_checks(self):
        """A sort group checks its tests under every name it has, and those
        names' rules may be others: the LC_CTYPE rows go under a member no
        sort group checks, and with none left the group is named."""
        sort2 = (['ww_WW.utf8', 'xx_YY.utf8'], 'UTF-8',
                 [('test', (b'a',), (b'b',), '<', '>', 'both')])
        saved = m.ctype_tests
        m.ctype_tests = lambda *a: ([self.ROW], 0)
        try:
            files, not_checked = m.confirm_groups(
                [sort2], {}, self.OLD, self.NEW, [], (),
                {'groups': [self.group('xx_YY.utf8', 'zz_ZZ.utf8'),
                            self.group('ww_WW.utf8')],
                 'why': None, 'unknown': []})
        finally:
            m.ctype_tests = saved
        (enc, text, ident, tests), = files
        path = os.path.join(_TMP, 'own-name.sql')
        with open(path, 'w') as f:
            f.write(text)
        got = {name: sorted(t) for name, (line, t) in
               c.Sql(path).collations.items()}
        self.assertEqual(got, {'ww_WW.utf8': [1], 'xx_YY.utf8': [1],
                               'zz_ZZ.utf8': [2]})
        self.assertIn((['ww_WW.utf8'], 'its character rules (LC_CTYPE): every '
                       'name of it is C.* or POSIX, which PostgreSQL may '
                       'answer itself, cannot be written into a SQL file, or '
                       'already runs sort tests in this file'), not_checked)

    def test_initcap_reads_the_first_character_mapped(self):
        """PostgreSQL maps the first character by towupper and asks
        iswalnum of what it got (formatting.c:1955-1964@REL_14_19): a
        character not alphanumeric whose capital is, on one machine only,
        gives initcap() of it twice another second character there. On
        the two RHEL pairs the character itself and its capital give the
        same rows, so these tables are built for the case: towupper maps
        U+0100 to U+0101, which is alphanumeric on the old machine only."""
        tables = {'o': ({0x100: 0x101}, {}, dict.fromkeys(
                      m.CTYPE_CLASSES, []) | {'alnum': [[0x101, 0x101]]}),
                  'n': ({0x100: 0x101}, {}, dict.fromkeys(
                      m.CTYPE_CLASSES, []))}
        saved = m.ctype_wide_of, m.ctype_wide_changes
        m.ctype_wide_of = lambda part, key: tables[key]
        m.ctype_wide_changes = lambda *a: [(0x100, True, '', '')]
        try:
            rows, skipped = m.ctype_tests(None, 'o', None, 'n')
        finally:
            m.ctype_wide_of, m.ctype_wide_changes = saved
        self.assertEqual(rows, [(0x100, 'text', 'initcap',
                                 'āĀ'.encode().hex(),
                                 'āā'.encode().hex())])

    def test_what_a_database_stores(self):
        for piece, codeset, want in (
                (b'\xc3\xa9', 'UTF-8', True), (b'\x80', 'UTF-8', False),
                (b'\xf4\x90\x80\x80', 'UTF-8', False),
                (b'\xed\xa0\x80', 'UTF-8', False),
                (m.SEPARATOR, 'EUC-JP', True), (b'\x8e', 'EUC-JP', False),
                (b'\xa4\xa2', 'EUC-JP', True), (b'\xff', 'ISO-8859-1', True),
                (b'\xa4\xa2', 'BIG5', None)):
            with self.subTest(piece=piece, codeset=codeset):
                self.assertIs(m.stores(piece, codeset), want)


# --- the reader on real outputs ---------------------------------------------

class ReadsRealOutputs(unittest.TestCase):

    def test_rhel8_to_rhel9_on_postgresql_18(self):
        rc, stdout, stderr = read(sql('89', 'UTF8'), out('89', 'UTF8', 'el8'),
                                  out('89', 'UTF8', 'el9'))
        self.assertEqual((rc, refusals(stdout)), (0, None))
        self.assertEqual(confirmed(stdout), (38761, 38761))
        # C.utf8: PostgreSQL gives no version, said for each output.
        self.assertEqual(len(re.findall(r'gives no glibc version for collation '
                                        r'C\.utf8', stdout)), 2)

    def test_rhel9_to_rhel10_on_postgresql_18(self):
        rc, stdout, stderr = read(sql('910', 'UTF8'),
                                  out('910', 'UTF8', 'el9'),
                                  out('910', 'UTF8', 'el10'))
        self.assertEqual((rc, confirmed(stdout)), (0, (32874, 32874)))

    def test_rhel8_to_rhel9_on_postgresql_14_to_17_with_warnings(self):
        """node1's collations were made on 2.28: PostgreSQL warns once per
        collation, and each warning is repeated, never refused."""
        for pg in (14, 15, 16, 17):
            with self.subTest(pg=pg):
                rc, stdout, stderr = read(
                    sql('89', 'UTF8'), out('89', 'UTF8', f'node0.pg{pg}'),
                    out('89', 'UTF8', f'node1.pg{pg}'))
                self.assertEqual((rc, confirmed(stdout)), (0, (38761, 38761)))
                warned = re.findall(r'^!! .*node1\.pg\d+\.out: PostgreSQL '
                                    r'warned: psql:<stdin>:\d+: WARNING:  '
                                    r'01000: collation "([^"]+)" has version '
                                    r'mismatch', stdout, re.M)
                self.assertEqual(sorted(warned), [
                    'en_US.utf8', 'ko_KR', 'ko_KR.utf8', 'or_IN',
                    'or_IN.utf8', 'sv_FI', 'sv_FI.utf8', 'sv_SE',
                    'sv_SE.utf8'])

    def test_latin1_and_latin9(self):
        for enc, n in (('LATIN1', 5), ('LATIN9', 4)):
            with self.subTest(encoding=enc):
                rc, stdout, stderr = read(sql('89', enc), out('89', enc, 'el8'),
                                          out('89', enc, 'el9'))
                self.assertEqual((rc, confirmed(stdout)), (0, (n, n)))

    def test_the_names_that_disappear_are_listed(self):
        rc, stdout, stderr = read(sql('89', 'UTF8'), out('89', 'UTF8', 'el8'),
                                  out('89', 'UTF8', 'el9'))
        block = stdout.split('uses a locale name the new machine does not '
                             'have:\n')[1].split('\n!!')[0]
        self.assertEqual(block.split('\n'),
                         ['     collation en_US.utf8@ampm: en_US.utf8@ampm',
                          '     collation en_US@ampm: en_US@ampm'])

    def test_what_the_file_does_not_test_is_repeated(self):
        rc, stdout, stderr = read(sql('89', 'UTF8'), out('89', 'UTF8', 'el8'),
                                  out('89', 'UTF8', 'el9'))
        said = stdout.split('Not tested by it:\n')[1].rstrip('\n').split('\n')
        self.assertEqual([x.strip() for x in said],
                         c.Sql(sql('89', 'UTF8')).notes)
        self.assertNotIn('unchanged', stdout.replace(
            'does not say that anything else is unchanged', ''))

    def test_psql_and_postgresql_in_spanish_with_a_file(self):
        """The labels psql prints in the client's language (DETALLE,
        SUGERENCIA, UBICACION), the server's text in its own, a file path
        with a space: read by the SQLSTATE, confirmed, each warning and what
        psql said before the file ran repeated."""
        rc, stdout, stderr = read(sql('89', 'UTF8'), out('89', 'UTF8', 'el8'),
                                  out('89', 'UTF8', 'es.el9'))
        self.assertEqual((rc, confirmed(stdout)), (0, (38761, 38761)))
        warned = re.findall(r'^!! .*es\.el9\.out: PostgreSQL warned: '
                            r'psql:/tmp/confirm 8\.1\.sql:\d+: WARNING:  01000: '
                            r'el ordenamiento \(collation\) .([^ ]+). tiene una '
                            r'discordancia de versi.n / DETALLE:  ', stdout,
                            re.M)
        self.assertEqual(sorted(warned), [
            'en_US.utf8', 'ko_KR', 'ko_KR.utf8', 'or_IN', 'or_IN.utf8',
            'sv_FI', 'sv_FI.utf8', 'sv_SE', 'sv_SE.utf8'])
        self.assertEqual(len(re.findall(r'^!! .*es\.el9\.out: psql printed '
                                        r'before the file ran: WARNING:  '
                                        r'database "postgres" ', stdout,
                                        re.M)), 1)

    def test_a_database_of_another_encoding(self):
        rc, stdout, stderr = read(sql('89', 'UTF8'),
                                  out('89', 'UTF8', 'wrongdb.el8'),
                                  out('89', 'UTF8', 'el9'))
        self.assertEqual(rc, 1)
        self.assertIsNone(confirmed(stdout))
        self.assertEqual([x.split(': ', 1)[1] for x in refusals(stdout)],
                         ['comes from a database of encoding LATIN1; this '
                          'file is for a UTF8 database, and stops in any '
                          'other'])

    def test_a_collation_in_the_catalog_whose_locale_is_gone(self):
        """C.utf8 is in glibc-minimal-langpack and stays usable; the
        others' locales went with glibc-all-langpacks."""
        rc, stdout, stderr = read(sql('89', 'UTF8'),
                                  out('89', 'UTF8', 'nolangpack.el8'),
                                  out('89', 'UTF8', 'el9'))
        self.assertEqual(rc, 1)
        self.assertIsNone(confirmed(stdout))
        cannot = re.findall(r'collation (\S+) CANNOT BE USED: psql:<stdin>:'
                            r'\d+: ERROR:  22023: could not create locale ',
                            stdout)
        self.assertEqual(sorted(cannot), [
            'ko_KR', 'ko_KR.utf8', 'or_IN', 'or_IN.utf8', 'sv_FI',
            'sv_FI.utf8', 'sv_SE', 'sv_SE.utf8'])
        self.assertEqual(len(refusals(stdout)), 8)


# --- the reader's refusals, each a real output with one thing changed ------

def first_test(text):
    return re.search(r'^test\|\d+\|[^|]+\|[<>]\|.*$', text, re.M).group(0)


class Refuses(unittest.TestCase):
    """Each case is the 8 -> 9 UTF8 run of PostgreSQL 18 with the one thing
    the case names changed; the refusal is asserted by its message."""

    SQL = OLD = NEW = None

    @classmethod
    def setUpClass(cls):
        cls.SQL = sql('89', 'UTF8')
        cls.OLD = out('89', 'UTF8', 'el8')
        cls.NEW = out('89', 'UTF8', 'el9')

    def refused(self, old, new, pattern, count=1):
        rc, stdout, stderr = read(self.SQL, old, new)
        self.assertEqual(rc, 1, stdout)
        self.assertIsNone(confirmed(stdout))
        why = refusals(stdout)
        hits = [x for x in why if re.search(pattern, x)]
        self.assertEqual(len(hits), count, why)
        return why

    def test_cut_short(self):
        def cut(text):
            return text[:len(text) // 2].rsplit('\n', 1)[0] + '\n'
        why = self.refused(mutant(self.OLD, cut, 'cut.out'), self.NEW,
                           r'cut\.out: is cut short: it lacks the last line$')
        self.assertTrue(any(re.search(r'cut\.out: collation en_US\.utf8: '
                                      r'test\(s\) \d+ to \d+ missing$', x)
                            for x in why), why)

    def test_empty(self):
        self.refused(mutant(self.OLD, lambda t: '', 'empty.out'), self.NEW,
                     r'empty\.out: is empty$')

    def test_a_test_missing(self):
        def drop(text):
            line = re.search(r'^test\|4120\|or_IN\|.*\n', text, re.M).group(0)
            return text.replace(line, '')
        self.refused(mutant(self.OLD, drop, 'missing.out'), self.NEW,
                     r'collation or_IN: test\(s\) 4120 missing$')

    def test_a_test_twice(self):
        def twice(text):
            line = first_test(text)
            return text.replace(line, line + '\n' + line, 1)
        self.refused(mutant(self.OLD, twice, 'twice.out'), self.NEW,
                     r'test 1 of collation C\.utf8 is answered twice$')

    def test_a_collation_that_cannot_be_used(self):
        def cannot(text):
            text = re.sub(r'^collation\|sv_SE\|usable\|glibc 2\.28$',
                          'collation|sv_SE|CANNOT BE USED', text, flags=re.M)
            return re.sub(r'^test\|\d+\|sv_SE\|.*\n', '', text, flags=re.M)
        self.refused(mutant(self.OLD, cannot, 'cannot.out'), self.NEW,
                     r'collation sv_SE CANNOT BE USED$')

    def test_a_row_like_neither_machine(self):
        def neither(text):
            line = first_test(text)
            fields = line.split('|')
            fields[3] = '='
            return text.replace(line, '|'.join(fields), 1)
        self.refused(mutant(self.OLD, neither, 'neither.out'), self.NEW,
                     r'1 test\(s\) answer like NEITHER machine, first test 1 '
                     r'of collation C\.utf8$')

    def test_a_row_like_the_other_machine(self):
        def other(text):
            line = first_test(text)
            fields = line.split('|')
            fields[3] = '<' if fields[3] == '>' else '>'
            return text.replace(line, '|'.join(fields), 1)
        self.refused(mutant(self.OLD, other, 'other.out'), self.NEW,
                     r'38760 of 38761 test\(s\) answer like the OLD machine '
                     r'and 1 like the NEW machine')

    def test_the_two_outputs_given_the_other_way_round(self):
        self.refused(self.NEW, self.OLD, r'given the other way round$')

    def test_both_outputs_from_one_server(self):
        self.refused(self.OLD, self.OLD, r'^both outputs answer like the OLD '
                     r'machine: ')
        self.refused(self.NEW, self.NEW, r'^both outputs answer like the NEW '
                     r'machine: ')

    def test_the_output_of_another_sql_file(self):
        self.refused(out('910', 'UTF8', 'el9'), self.NEW,
                     r'is the output of another SQL file')

    def test_another_glibc_version(self):
        def version(text):
            return text.replace('|usable|glibc 2.28', '|usable|glibc 2.27', 1)
        self.refused(mutant(self.OLD, version, 'version.out'), self.NEW,
                     r"collation ko_KR reports glibc 2\.27, and the old "
                     r"machine's file was measured on 2\.28 ")

    def test_an_error_outside_a_check(self):
        def error(text):
            return text.replace(
                'server|18.6\n', 'server|18.6\npsql:<stdin>:12: ERROR:  '
                '57014: canceling statement due to user request\n', 1)
        self.refused(mutant(self.OLD, error, 'error.out'), self.NEW,
                     r'psql reported an error outside the check of a '
                     r'collation: psql:<stdin>:12: ERROR:  57014: canceling')

    def test_an_error_in_another_language(self):
        """The severity in the server's language (FEHLER): an error by its
        SQLSTATE all the same."""
        def error(text):
            return text.replace(
                'server|18.6\n', 'server|18.6\npsql:<stdin>:12: FEHLER:  '
                '57014: storniere Anfrage wegen Benutzeraufforderung\n', 1)
        self.refused(mutant(self.OLD, error, 'fehler.out'), self.NEW,
                     r'psql reported an error outside the check of a '
                     r'collation: psql:<stdin>:12: FEHLER:  57014: ')

    def test_a_line_the_file_does_not_print(self):
        def line(text):
            return text.replace('server|18.6\n',
                                'server|18.6\nOutput format is unaligned.\n',
                                1)
        self.refused(mutant(self.OLD, line, 'psqlrc.out'), self.NEW,
                     r'holds a line this SQL file does not print: Output '
                     r'format is unaligned\.$')

    def test_a_psql_line_under_a_warning_is_not_part_of_it(self):
        """What follows a message is its DETAIL, HINT or LOCATION in any
        language, never a line of psql's own."""
        def line(text):
            return text.replace(
                'server|18.6\n', 'server|18.6\npsql:<stdin>:12: WARNING:  '
                '01000: w\npsql:<stdin>:13: error: unexpected\n', 1)
        self.refused(mutant(self.OLD, line, 'psqlline.out'), self.NEW,
                     r'holds a line this SQL file does not print: '
                     r'psql:<stdin>:13: error: unexpected$')


    def test_no_connection(self):
        def failed(text):
            return ('psql: error: connection to server on socket '
                    '"/run/postgresql/.s.PGSQL.5432" failed: FATAL:  database '
                    '"x" does not exist\n')
        self.refused(mutant(self.OLD, failed, 'noconn.out'), self.NEW,
                     r'noconn\.out: is not an output of .*: it lacks the line '
                     r'that names the file')

    def test_two_runs_in_one_output(self):
        self.refused(mutant(self.OLD, lambda t: t + t, 'two-runs.out'),
                     self.NEW, r'two-runs\.out: holds the output of more than '
                     r'one run$')

    def test_a_collation_not_reported(self):
        def drop(text):
            return re.sub(r'^collation\|or_IN\.utf8\|.*\n', '', text,
                          flags=re.M)
        self.refused(mutant(self.OLD, drop, 'nocoll.out'), self.NEW,
                     r'nocoll\.out: says nothing of collation or_IN\.utf8$')

    def test_an_error_in_a_check_reported_usable(self):
        line = c.Sql(self.SQL).collations['sv_SE'][0]

        def error(text):
            return text.replace(
                'collation|sv_SE|usable', f'psql:<stdin>:{line}: ERROR:  '
                '22023: could not create locale "sv_SE.utf8": No such file or '
                'directory\ncollation|sv_SE|usable', 1)
        self.refused(mutant(self.OLD, error, 'usable-error.out'), self.NEW,
                     r'collation sv_SE is reported usable after an error in '
                     r'its check: ')

    def ctype_line(self, text):
        return re.search(r'^ctype\|\d+\|en_US\.utf8\|.*$', text,
                         re.M).group(0)

    def test_an_lc_ctype_row_missing(self):
        def drop(text):
            line = self.ctype_line(text)
            return text.replace(line + '\n', '', 1), line.split('|')[1]
        number = drop(open(self.OLD).read())[1]
        self.refused(mutant(self.OLD, lambda t: drop(t)[0],
                            'ctype-missing.out'),
                     self.NEW, rf'collation en_US\.utf8: test\(s\) {number} '
                     r'missing$')

    def test_an_lc_ctype_row_like_neither_machine(self):
        def neither(text):
            line = self.ctype_line(text)
            fields = line.split('|')
            fields[3] = 'f' if fields[3] not in ('t', 'f') else '00'
            return text.replace(line, '|'.join(fields), 1)
        self.refused(mutant(self.OLD, neither, 'ctype-neither.out'), self.NEW,
                     r'1 test\(s\) answer like NEITHER machine, first test '
                     r'\d+ of collation en_US\.utf8$')

    def test_an_lc_ctype_row_like_the_other_machine(self):
        def other(text):
            line = self.ctype_line(text)
            fields = line.split('|')
            fields[3] = re.fullmatch(r'old (\w+), new (\w+)',
                                     fields[4]).group(2)
            return text.replace(line, '|'.join(fields), 1)
        self.refused(mutant(self.OLD, other, 'ctype-other.out'), self.NEW,
                     r'38760 of 38761 test\(s\) answer like the OLD machine '
                     r'and 1 like the NEW machine')

    def test_a_table_pair_like_the_other_machine(self):
        """A pair of a string of several characters (or_IN's table), in
        the new output, answered as the old machine does."""
        def other(text):
            line = re.search(r'^test\|\d+\|or_IN\|[<>]\|old ([<>]), new '
                             r'[<>]\|from both machines\' compiled sort '
                             r'tables\|.*$', text, re.M)
            fields = line.group(0).split('|')
            fields[3] = line.group(1)
            return text.replace(line.group(0), '|'.join(fields), 1)
        self.refused(self.OLD, mutant(self.NEW, other, 'table-other.out'),
                     r'38760 of 38761 test\(s\) answer like the NEW machine '
                     r'and 1 like the OLD machine')

    def test_a_test_the_file_does_not_hold(self):
        def extra(text):
            line = first_test(text)
            fields = line.split('|')
            fields[1] = '99999'
            return text.replace(line, line + '\n' + '|'.join(fields), 1)
        self.refused(mutant(self.OLD, extra, 'extra.out'), self.NEW,
                     r'extra\.out: answers 1 test\(s\) this file does not '
                     r'hold$')


class NullVersion(unittest.TestCase):
    """A version PostgreSQL does not give is said, and not refused: what
    it cannot check, it names."""

    def test_null_versions_are_said(self):
        old = mutant(out('89', 'UTF8', 'el8'),
                     lambda t: t.replace('|usable|glibc 2.28', '|usable|glibc '
                                         'NULL'), 'null.out')
        rc, stdout, stderr = read(sql('89', 'UTF8'), old,
                                  out('89', 'UTF8', 'el9'))
        self.assertEqual((rc, confirmed(stdout)), (0, (38761, 38761)))
        said = re.findall(r'null\.out: PostgreSQL gives no glibc version for '
                          r'collation (\S+) ', stdout)
        self.assertEqual(len(said), 10)

    def test_one_version_on_both_machines_is_said(self):
        """Two builds of 2.28 (RHEL 8 changed its order inside 2.28): the
        file's header and both outputs as such a pair writes them, the id
        taken again; what PostgreSQL cannot tell apart is said."""
        with open(sql('89', 'UTF8')) as f:
            text = f.read()
        old_id = c.Sql(sql('89', 'UTF8')).ident
        text = text.replace('-- new: glibc-2.34-275.el9_8.x86_64|2.34',
                            '-- new: glibc-2.28-251.el8_10.48.x86_64|2.28')
        new_id = c.hashlib.sha256(text.replace(old_id, c.PLACEHOLDER)
                                  .encode('ascii')).hexdigest()
        path = os.path.join(_TMP, 'same-version.sql')
        with open(path, 'w') as f:
            f.write(text.replace(old_id, new_id))
        outs = [mutant(out('89', 'UTF8', side), lambda t: t.replace(
            old_id, new_id).replace('glibc 2.34', 'glibc 2.28'),
            f'same-version.{side}.out') for side in ('el8', 'el9')]
        rc, stdout, stderr = read(path, *outs)
        self.assertEqual((rc, confirmed(stdout)), (0, (38761, 38761)))
        self.assertIn('!! Both machines run glibc 2.28: two builds of one '
                      'version cannot be told apart from SQL', stdout)
        rc, stdout, stderr = read(sql('89', 'UTF8'), out('89', 'UTF8', 'el8'),
                                  out('89', 'UTF8', 'el9'))
        self.assertNotIn('two builds of one version', stdout)


if __name__ == '__main__':
    unittest.main()
