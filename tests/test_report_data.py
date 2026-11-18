"""scripts/report_data.py: the run's data file, report.<pair>.json, assembled
from the files the steps wrote, and load(), its strict reader.

What each audit.sh run of the suite that reaches its summary is checked
against is in _harness.check_report. This module holds the files of each
kind of run read back, and what no run reaches: a file a step did not
write (each one said as 'not reported', never as an empty list), a file
cut short or two files that do not agree, and a report that load() must
refuse (a key missing or added, a state outside its list, a null, another
format, facts that do not hang together)."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import _harness  # noqa: F401  (puts scripts/ on the path)
import report_data as rd

PAIR = 'glibc-2.28..glibc-2.34'


def write(d, name, text):
    with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
        f.write(text)


def step2_files(d, removed='en_US@ampm (deleted)\nxx (renamed to yy)\n',
                **by_name):
    """The files step 2 writes; by_name replaces fields of its
    step2_rules_by_name (tags, something compared, check compared)."""
    write(d, f'step2_changed_collate.{PAIR}.txt', 'C\nor_IN\nsv_SE\n')
    write(d, f'step2_not_compared.{PAIR}.txt', 'sv_SE\n')
    write(d, f'step2_removed_locales.{PAIR}.txt',
          '# locale files at glibc-2.28 and not at glibc-2.34\n' + removed)
    data = {
        'step': 2, 'compared': 'tags', 'names': ['glibc-2.28', 'glibc-2.34'],
        'machines': False, 'nothing': None, 'what': None,
        'measurements': None, 'charmaps': {'state': 'compared', 'why': None},
        'locales': None}
    data.update(by_name)
    write(d, f'step2_rules_by_name.{PAIR}.json', json.dumps(data) + '\n')
    write(d, f'step2_rules_characters.{PAIR}.tsv',
          '# name\tstate\tevery rule can change\tcharacters\n'
          'or_IN\tdiffers\tyes\tU+0000 U+0001\n')


MACHINES = dict(
    compared='builds', machines=True, measurements=['a' * 64, 'b' * 64],
    locales={'C': [None, 'built in', 'built into glibc, no locale file', '-'],
             'sv_SE': ['sv_SE', 'differs', '', '-']})


def nothing_files(d, nothing='commit'):
    """What step 2 writes when it compares nothing: empty lists."""
    what = 'the two tags are one commit'
    step2_files(d, removed='', nothing=nothing, what=what,
                charmaps={'state': 'one ' + nothing, 'why': None})
    write(d, f'step2_changed_collate.{PAIR}.txt', '')
    write(d, f'step2_not_compared.{PAIR}.txt', '')
    write(d, f'step2_rules_characters.{PAIR}.tsv',
          '# name\tstate\tevery rule can change\tcharacters\n')
    return what


def step11_files(d):
    write(d, f'measured_order_summary.{PAIR}.txt', 'summary\n')
    write(d, f'measured_order_verdicts.{PAIR}.json', json.dumps({
        'step': 11, 'measurements': ['a' * 64, 'b' * 64],
        'locales': {'C': ['firm', 'built into glibc'],
                    'sv_SE': ['changed', 'the order changes']}}) + '\n')
    write(d, f'measured_order_characters.{PAIR}.tsv',
          '# Step 11, x -> y: every character.\n'
          'locales\tfinding\tcharacters\thow\ttest\tbefore\tnow\tmeasured\n'
          'sv_SE\tmoved\tU+00E5\t-\tx\t>\t<\ton both machines\n'
          'sv_SE,sv_SE.utf8\tmoved\tU+00E4\t-\tx\t>\t<\ton both machines\n'
          '# Control: held.\n'
          '# end: 2 locale group(s), 2 row(s)\n')
    write(d, f'measured_order_confirm.{PAIR}.UTF8.sql', 'select 1;\n')
    # Another pair's, whose new side's name extends this one's (16.109).
    write(d, f'measured_order_confirm.{PAIR}.1.UTF8.sql', 'select 1;\n')
    write(d, f'measured_order_rules.{PAIR}.txt', '     Each locale...\n')


ON_MACHINES = dict(route='builds', old_node='/a', new_node='/b',
                   order_words='forward none build build builds',
                   step11=True)


def args(d, **over):
    a = dict(out_dir=d, pair=PAIR, old='glibc-2.28', new='glibc-2.34',
             route='tags', order_words='forward none tag tag tags',
             old_node='', new_node='', tag_side='', step11=False)
    a.update(over)
    return SimpleNamespace(**a)


class _Folder(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='report_data_')
        self.addCleanup(shutil.rmtree, self.d, True)

    def written(self, **over):
        """main() on the folder, then load() of what it wrote."""
        a = args(self.d, **over)
        argv = ['--out-dir', a.out_dir, '--pair', a.pair, '--old', a.old,
                '--new', a.new, '--route', a.route,
                '--order-words', a.order_words]
        if a.old_node:
            argv += ['--old-node', a.old_node, '--new-node', a.new_node]
        if a.tag_side:
            argv += ['--tag-side', a.tag_side]
        if a.step11:
            argv.append('--step11')
        self.assertEqual(rd.main(argv), 0)
        self.assertEqual(sorted(n for n in os.listdir(self.d)
                                if n.startswith('report.')),
                         [f'report.{PAIR}.json'])
        return rd.load(os.path.join(self.d, f'report.{PAIR}.json'))


class EveryFileWritten(_Folder):
    """The files of a run of each kind, read back as data."""

    def test_tags_without_machines(self):
        step2_files(self.d)
        r = self.written()
        self.assertEqual(r['step2']['listed'], {
            'state': 'reported', 'compared': ['C', 'or_IN'],
            'not_compared': ['sv_SE']})
        self.assertEqual(r['step2']['removed']['rows'], [
            {'name': 'en_US@ampm', 'how': 'deleted', 'to': '-'},
            {'name': 'xx', 'how': 'renamed', 'to': 'yy'}])
        self.assertEqual((r['step2']['removed']['source'],
                          r['step2']['removed']['distro_files']),
                         ('tags', 'not checked'))
        self.assertEqual(r['step2']['rules_characters']['rows'],
                         [['or_IN', 'differs', 'yes', 'U+0000 U+0001']])
        self.assertEqual(r['step11'], {
            'state': 'not run', 'why': "no machines' files",
            'not_done': list(rd.STEP11_NOT_DONE)})
        self.assertEqual(r['run']['machines']['state'], 'not given')
        for part in (r['compared'], r['warnings'], r['step2']['totals'],
                     r['step2']['character_sets']):
            self.assertEqual(part['state'], 'not yet reported')

    def test_machines(self):
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        r = self.written(route='builds', old_node='/m/old', new_node='/m/new',
                         order_words='forward none build build builds',
                         step11=True)
        s11 = r['step11']
        self.assertEqual(s11['state'], 'reported')
        self.assertEqual(s11['verdicts']['sv_SE'],
                         {'verdict': 'changed', 'words': 'the order changes'})
        self.assertEqual(len(s11['characters']['rows']), 2)
        self.assertEqual(s11['sql_files'], [
            {'encoding': 'UTF8',
             'file': f'measured_order_confirm.{PAIR}.UTF8.sql'}])
        self.assertEqual(s11['rules_beside']['state'], 'set beside')
        self.assertEqual(r['step2']['rules_by_name']['locales'], {
            'state': 'reported', 'by_name': {
                'C': {'key': 'none', 'rules': 'built in',
                      'why': 'built into glibc, no locale file',
                      'charmap': '-'},
                'sv_SE': {'key': 'sv_SE', 'rules': 'differs', 'why': '',
                          'charmap': '-'}}})
        self.assertEqual(r['step2']['removed']['source'], 'machine sources')
        self.assertEqual(r['run']['machines'],
                         {'state': 'given', 'old': '/m/old', 'new': '/m/new'})

    def test_each_state_of_the_character_set_check(self):
        """Step 2's character set check, run or not run, kept with its
        reason where it has one; 'compared' has none (one commit and one
        tree, which have none either, are test_nothing_compared's)."""
        for cm, want in (
                ({'state': 'not run', 'why': 'SUPPORTED does not exist'},
                 {'state': 'not run', 'why': 'SUPPORTED does not exist'}),
                ({'state': 'compared', 'why': None}, {'state': 'compared'})):
            with self.subTest(cm=cm):
                step2_files(self.d, charmaps=cm)
                self.assertEqual(
                    self.written()['step2']['rules_by_name']['charmaps'], want)

    def test_nothing_compared(self):
        """One commit or one tree: step 2's lists are 'not compared' with
        its own words, never an empty list read as nothing found; and step
        11's rules are not set beside, with why."""
        for nothing in ('commit', 'tree'):
            with self.subTest(nothing=nothing):
                what = nothing_files(self.d, nothing)
                step11_files(self.d)
                r = self.written(order_words=f'same {nothing} tag tag tags',
                                 step11=True)
                for part in ('listed', 'rules_characters'):
                    self.assertEqual(r['step2'][part],
                                     {'state': 'not compared', 'why': what})
                self.assertEqual(r['step11']['rules_beside'], {
                    'state': 'not set beside',
                    'why': 'step 2 compared nothing, ' + what,
                    'file': f'measured_order_rules.{PAIR}.txt'})

    def test_rules_not_set_beside_on_the_tags(self):
        """Step 2 compared the tags (a machine's file without sources):
        step 11's rules file is there and says no rule is beside."""
        step2_files(self.d)
        step11_files(self.d)
        r = self.written(old_node='/a', new_node='/b', step11=True)
        self.assertEqual(r['step11']['rules_beside'], {
            'state': 'not set beside',
            'why': "step 2 compared the tags glibc-2.28 and glibc-2.34, not "
                   "the machines' own locale sources",
            'file': f'measured_order_rules.{PAIR}.txt'})

    def test_a_machine_path_that_is_not_utf8(self):
        """A Latin-1 file name on a Linux machine: written escaped, and
        the run goes on."""
        step2_files(self.d)
        old = os.fsdecode(b'/home/dba/caf\xe9/rhel8.tar.gz')
        r = self.written(old_node=old, new_node='/b')
        self.assertEqual(r['run']['machines']['old'],
                         '/home/dba/caf\\xe9/rhel8.tar.gz')

    def test_where_removed_comes_from(self):
        """Each of the summary's Removed sources, from the route."""
        for over, want in (
                (dict(route='builds',
                      order_words='forward none build build builds'),
                 ('build packages', 'checked')),
                (dict(route='sides', tag_side='old',
                      order_words='forward none tag build sides'),
                 ('old side is a tag', 'not checked')),
                (dict(route='sides', tag_side='new',
                      order_words='forward none build tag sides'),
                 ('new side is a tag', 'not checked')),
                (dict(old_node='/a', new_node='/b'),
                 ("tags, because of a machine's file", 'not checked'))):
            with self.subTest(over=over):
                step2_files(self.d)
                r = self.written(**over)
                self.assertEqual((r['step2']['removed']['source'],
                                  r['step2']['removed']['distro_files']),
                                 want)


class AFileNotWritten(_Folder):
    """A file the step did not write is 'not reported', with its name;
    never an empty list, which would read as nothing found."""

    def test_each_step2_file(self):
        for name, part in (('step2_changed_collate', 'listed'),
                           ('step2_not_compared', 'listed'),
                           ('step2_removed_locales', 'removed'),
                           ('step2_rules_by_name', 'rules_by_name'),
                           ('step2_rules_characters', 'rules_characters')):
            with self.subTest(name=name):
                step2_files(self.d)
                ext = ('.json' if name.endswith('by_name') else
                       '.tsv' if name.endswith('characters') else '.txt')
                os.remove(os.path.join(self.d, f'{name}.{PAIR}{ext}'))
                got = self.written()['step2'][part]
                self.assertEqual(got['state'], 'not reported')
                self.assertIn(name, got['why'])

    def test_each_step11_file(self):
        for name, ext, part in (
                ('measured_order_summary', '.txt', None),
                ('measured_order_verdicts', '.json', None),
                ('measured_order_characters', '.tsv', 'characters')):
            with self.subTest(name=name):
                step2_files(self.d, **MACHINES)
                step11_files(self.d)
                os.remove(os.path.join(self.d, f'{name}.{PAIR}{ext}'))
                s11 = self.written(
                    route='builds', old_node='/a', new_node='/b',
                    order_words='forward none build build builds',
                    step11=True)['step11']
                got = s11[part] if part else s11
                self.assertEqual(got['state'], 'not reported')
                self.assertIn(name, got['why'])

    def test_rules_beside_not_set(self):
        """No rules file: why is the log's lines, as audit.sh prints them;
        no log either: its name."""
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        os.remove(os.path.join(self.d, f'measured_order_rules.{PAIR}.txt'))
        write(self.d, f'measured_order_rules.{PAIR}.log', 'error: x\nmore\n')
        over = dict(route='builds', old_node='/a', new_node='/b',
                    order_words='forward none build build builds',
                    step11=True)
        self.assertEqual(self.written(**over)['step11']['rules_beside'],
                         {'state': 'not reported', 'why': ['error: x', 'more']})
        os.remove(os.path.join(self.d, f'measured_order_rules.{PAIR}.log'))
        self.assertEqual(
            self.written(**over)['step11']['rules_beside'],
            {'state': 'not reported',
             'why': [f'no measured_order_rules.{PAIR}.log']})


class AFileCutShort(_Folder):
    """Files cut short or that do not agree (a list without its last
    newline, a name not compared that step 2 does not list, a row of
    another width, the characters file without its count or with a count
    its rows do not make, a list of names where step 2 compared nothing):
    refused, and nothing written under the report's name."""

    def refused(self, **over):
        a = args(self.d, **over)
        argv = ['--out-dir', a.out_dir, '--pair', a.pair, '--old', a.old,
                '--new', a.new, '--route', a.route,
                '--order-words', a.order_words]
        if a.old_node:
            argv += ['--old-node', a.old_node, '--new-node', a.new_node]
        if a.step11:
            argv.append('--step11')
        self.assertEqual(rd.main(argv), 1)
        self.assertEqual([n for n in os.listdir(self.d)
                          if n.startswith('report.')], [])

    def test_a_name_not_compared_that_is_not_listed(self):
        """Step 2 lists every name it could not compare: one that is not
        in its list is two files of two runs, never a name to drop."""
        step2_files(self.d)
        write(self.d, f'step2_not_compared.{PAIR}.txt', 'de_DE\n')
        self.refused()

    def test_names_where_nothing_was_compared(self):
        for name, text in (
                (f'step2_changed_collate.{PAIR}.txt', 'de_DE\n'),
                (f'step2_rules_characters.{PAIR}.tsv',
                 '# name\tstate\tevery rule can change\tcharacters\n'
                 'de_DE\tdiffers\tno\tU+0041\n')):
            with self.subTest(name=name):
                nothing_files(self.d)
                write(self.d, name, text)
                self.refused(order_words='same commit tag tag tags')

    def test_a_list_without_its_last_newline(self):
        step2_files(self.d, removed='en_US@ampm (deleted)\nxx (renamed to yy)')
        self.refused()

    def test_the_characters_file(self):
        for cut in ('# end: 2 locale group(s), 2 row(s)\n',
                    '# end: 2 locale group(s), 2 row(s)'):
            with self.subTest(cut=cut):
                step2_files(self.d, **MACHINES)
                step11_files(self.d)
                p = os.path.join(self.d,
                                 f'measured_order_characters.{PAIR}.tsv')
                text = open(p, encoding='utf-8').read()
                write(self.d, os.path.basename(p), text.replace(cut, ''))
                self.refused(**ON_MACHINES)
        for wrong in ('# end: 2 locale group(s), 3 row(s)\n',
                      '# end: 1 locale group(s), 2 row(s)\n'):
            with self.subTest(wrong=wrong):
                step2_files(self.d, **MACHINES)
                step11_files(self.d)
                p = os.path.join(self.d,
                                 f'measured_order_characters.{PAIR}.tsv')
                text = open(p, encoding='utf-8').read()
                write(self.d, os.path.basename(p), text.replace(
                    '# end: 2 locale group(s), 2 row(s)\n', wrong))
                self.refused(**ON_MACHINES)

    def test_a_row_of_another_width(self):
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        p = os.path.join(self.d, f'measured_order_characters.{PAIR}.tsv')
        text = open(p, encoding='utf-8').read()
        write(self.d, os.path.basename(p),
              text.replace('\ton both machines\n', '\n', 1))
        self.refused(**ON_MACHINES)


class TheReaderIsStrict(_Folder):
    """load() refuses whatever is not exactly the format: each case is a
    report that load() read whole, with one thing changed."""

    def setUp(self):
        super().setUp()
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        self.good = self.written(**ON_MACHINES)

    def refuses(self, change):
        r = copy.deepcopy(self.good)
        change(r)
        p = os.path.join(self.d, 'changed.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        with self.assertRaises(rd.ReportError):
            rd.load(p)

    def test_the_unchanged_report_is_read(self):
        p = os.path.join(self.d, 'same.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.good, f)
        self.assertEqual(rd.load(p), self.good)

    def test_another_format(self):
        self.refuses(lambda r: r.update(format='pg-glibc-audit-report/2'))

    def test_a_section_missing(self):
        for key in ('run', 'compared', 'step2', 'step11', 'warnings'):
            with self.subTest(key=key):
                self.refuses(lambda r: r.pop(key))

    def test_a_key_added(self):
        self.refuses(lambda r: r['step2'].update(extra=1))
        self.refuses(lambda r: r['step11']['verdicts']['C'].update(x='y'))

    def test_a_key_missing_inside(self):
        self.refuses(lambda r: r['step2']['listed'].pop('not_compared'))
        self.refuses(lambda r: r['step2']['removed']['rows'][0].pop('to'))
        self.refuses(lambda r: r['step11'].pop('rules_beside'))

    def test_a_state_outside_its_list(self):
        for path in (('step2', 'listed'), ('step2', 'removed'),
                     ('step2', 'rules_by_name'), ('step11',),
                     ('step11', 'rules_beside'), ('step11', 'characters'),
                     ('run', 'machines'), ('warnings',), ('compared',),
                     ('step2', 'totals')):
            with self.subTest(path=path):
                def change(r):
                    x = r
                    for k in path:
                        x = x[k]
                    x['state'] = 'clean'
                self.refuses(change)

    def test_a_value_outside_its_list(self):
        self.refuses(lambda r: r['run'].update(order='reversed'))
        self.refuses(lambda r: r['step11']['verdicts']['C'].update(
            verdict='same'))
        self.refuses(lambda r: r['step2']['removed']['rows'][0].update(
            how='gone'))
        self.refuses(lambda r: r['step2']['removed'].update(
            distro_files='checked'))

    def test_a_null(self):
        self.refuses(lambda r: r['step2']['listed'].update(compared=None))
        # Where no other check looks at the type: only the null rule.
        self.refuses(lambda r: r['step11']['verdicts']['C'].update(
            words=None))
        self.refuses(lambda r: r['pair'].update(slug=None))

    def test_facts_that_do_not_hang_together(self):
        """Each a report a run cannot write: rules set beside with a
        locale step 2 gave no rules for, or of other measurements; step
        2's per-locale rules and measurements kept where it read no
        machines; its lists reported where its record says it compared
        nothing; the check ended as one commit where something was
        compared."""
        self.refuses(lambda r: r['step2']['rules_by_name']['locales']
                     ['by_name'].pop('sv_SE'))
        self.refuses(lambda r: r['step2']['rules_by_name'].update(
            measurements=['c' * 64, 'b' * 64]))
        self.refuses(lambda r: r['step2']['rules_by_name'].update(
            nothing={'state': 'commit', 'what': 'x'}))
        self.refuses(lambda r: r['step2']['rules_by_name'].update(
            charmaps={'state': 'one commit'}))
        self.refuses(lambda r: r['step2']['rules_by_name'].update(
            machines=False))

    def test_each_rule_of_step2s_record_alone(self):
        """Each rule broken alone, the others kept, so that no case is
        refused by a rule other than the one it names."""
        unset = {'state': 'not reported', 'why': 'x'}
        na = {'state': 'not applicable', 'why': 'x'}

        def rules_without_machines(r):
            # Per-locale rules where step 2 did not read two machines.
            rb = r['step2']['rules_by_name']
            rb.update(machines=False, measurements=na)
            r['step11']['rules_beside'] = unset
        self.refuses(rules_without_machines)

        def machines_without_measurements(r):
            r['step2']['rules_by_name']['measurements'] = na
            r['step11']['rules_beside'] = unset
        self.refuses(machines_without_measurements)

        def lists_where_nothing_was_compared(r):
            rb = r['step2']['rules_by_name']
            rb.update(nothing={'state': 'commit', 'what': 'x'},
                      charmaps={'state': 'one commit'}, locales=na)
            r['step11']['rules_beside'] = unset
        self.refuses(lists_where_nothing_was_compared)

        def all_three_kept(r):
            # The control: the same report, each rule kept, is read.
            lists_where_nothing_was_compared(r)
            for part in ('listed', 'rules_characters'):
                r['step2'][part] = {'state': 'not compared', 'why': 'x'}
        r = copy.deepcopy(self.good)
        all_three_kept(r)
        p = os.path.join(self.d, 'kept.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        self.assertEqual(rd.load(p), r)

    def test_not_run_says_what_it_leaves(self):
        def change(r):
            r['step11'] = {'state': 'not run', 'why': 'x', 'not_done': []}
        self.refuses(change)


if __name__ == '__main__':
    unittest.main()
