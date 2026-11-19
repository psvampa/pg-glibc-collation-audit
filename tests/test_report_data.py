"""scripts/report_data.py: the run's data file, report.<pair>.json, assembled
from the files the steps wrote, and load(), its strict reader.

What each audit.sh run of the suite that reaches its summary is checked
against is in _harness.check_report. This module holds the files of each
kind of run read back, and what no run reaches: a file a step did not
write (each one said as 'not reported', never as an empty list), a file
cut short or two files that do not agree, and a report that load() must
refuse (a key missing or added, a state outside its list, a null, another
format, facts that do not hang together). The `!!` of steps 0, 1 and 2:
one of every kind with its title, and the lines of a step's warnings file
that are refused."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

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


# One `!!` of each kind as a step writes it (kind, data), with the title
# KINDS makes of it: every kind, so that a title no run of the suite
# reaches is still read here.
SAMPLES = {
    'same_commit': ({'old': 'glibc-2.34', 'new': 'glibc-2.34'},
                    'Nothing compared: glibc-2.34 and glibc-2.34 are the '
                    'same commit'),
    'same_tree_machines': ({'old': 'a', 'new': 'b', 'charmaps': True},
                           'Nothing compared: the two machines hold the same '
                           'locale sources'),
    'same_tree': ({'old': 'a', 'new': 'b'},
                  'Nothing compared: a and b are one source tree'),
    'reversed_allowed': ({'old': 'a', 'new': 'b', 'detail': 'd'},
                         'Old and new run the other way round, on request'),
    'direction_undetermined': ({'old': 'a', 'new': 'b', 'detail': 'd'},
                               'Which of a and b is the older one'),
    'signatures_unchecked': (
        {'refs': [{'ref': 'glibc-2.28', 'status': 'no-gpg'},
                  {'ref': 'glibc-2.34', 'status': 'no-gpg'}]},
        'Tag signatures of glibc-2.28, glibc-2.34 (gpg is not installed)'),
    'machine_falls_back_to_tags': (
        {'unread': [{'side': 'old', 'build': 'glibc-2.28-1.el8',
                     'why': 'w'}], 'tags': ['glibc-2.28', 'glibc-2.34']},
        "The old machine's build glibc-2.28-1.el8 could not be read; step 2 "
        "read the tags"),
    'sides_read_as_tags': (
        {'refs': ['glibc-2.28', 'glibc-2.34'], 'classes': ['tag', 'tag'],
         'ask': 'build'},
        "The distro's own locale files and patches (glibc-2.28 and "
        "glibc-2.34 are upstream tags)"),
    'code_not_compared': ({'step11': False},
                          "glibc's code that compiles and compares the rules"),
    'collation_version_unknown': (
        {'sides': ['abc']}, "Whether PostgreSQL warns about a collation's "
        "version (no glibc version for abc)"),
    'collation_version_same': (
        {'version': '2.28'},
        'PostgreSQL will not warn: both sides are glibc 2.28'),
    'machine_entries_skipped': (
        {'machine': 'm', 'entries': [{'entry': 'x', 'why': 'y'},
                                     {'entry': 'z', 'why': 'y'}]},
        "2 entries of m's files not read"),
    'machine_not_checked': (
        {}, "The machines' locale sources against the package they came "
        "from"),
    'build_charmaps_unchecked': (
        {'build': 'b', 'package': 'p', 'why': 'w'},
        'The character sets of b against its package'),
    'build_not_checked': (
        {'metadata': True, 'unlisted': []},
        "The builds' compiler, code patches and GPG signatures"),
    'added_not_at_old': (
        {'at': 'glibc-2.28', 'named': 'tag', 'locales': ['ab_CD', 'ef_GH']},
        'Whether 2 locales with no file at glibc-2.28 existed on the old '
        'system'),
    'added_rules_unknown': (
        {'old': 'a', 'new': 'b', 'locales': [{'name': 'x', 'why': 'w'},
                                             {'name': 'y', 'why': 'w'}]},
        'Sort rules of 2 locale files at b and not at a'),
    'c_utf8_no_source': (
        {'sides': ['glibc-2.28', 'glibc-2.34']},
        "C.UTF-8's sort rules (no source file at glibc-2.28 and "
        "glibc-2.34)"),
    'c_utf8_differs': ({}, "C.UTF-8's sort rules differ"),
    'rules_not_compared': (
        {'groups': [{'names': ['a', 'b'], 'why': 'w'},
                    {'names': ['c'], 'why': 'v'}]},
        'Sort rules of 3 locales, listed in the summary'),
    'names_untraced': (
        {'machine': 'm', 'names': [{'name': 'x', 'why': 'w'},
                                   {'name': 'y', 'why': 'w'}]},
        'Character set of 2 locale names measured on m'),
    'charmaps_not_run': (
        {'why': 'w'}, 'The character sets locales are built with, and the '
        'collating elements built out of transliterations'),
    'charmap_builds_unsettled': (
        {'builds': [{'build': 'x', 'charmap': 'UTF-8', 'why': 'w'},
                    {'build': 'y', 'charmap': 'UTF-8', 'why': 'w'}]},
        'What the character set does to 2 locale builds, listed in the '
        'summary'),
    'charmap_distro_buildable': (
        {'locales': [{'source': 'x', 'charmap': 'ISO-8859-15'},
                     {'source': 'y', 'charmap': 'ISO-8859-15'}]},
        '2 locales a distro can build in a changed character set, listed '
        'in the summary'),
    'charmap_search_not_run': (
        {'charmaps': [{'charmap': 'A', 'why': 'w'},
                      {'charmap': 'B', 'why': 'w'}]},
        'Locales a distro can build in 2 changed character sets'),
    'charmap_sources_missing': (
        {'entries': ['x', 'y'], 'basis': 'SUPPORTED'},
        'Character sets of 2 SUPPORTED entries whose source file is not on '
        'both sides'),
    'translit_unsettled': (
        {'builds': [{'build': 'x', 'charmap': 'UTF-8', 'why': 'w'},
                    {'build': 'y', 'charmap': 'UTF-8', 'why': 'w'}]},
        'Collating elements built out of transliterations, of 2 locale '
        'builds, listed in the summary'),
}


def warnings_file(d, step, kinds):
    write(d, f'step{step}.{PAIR}.warnings.jsonl', ''.join(
        json.dumps({'kind': k, 'data': SAMPLES[k][0], 'text': f'!! {k}'})
        + '\n' for k in kinds))


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
        for part in (r['compared'], r['step2']['totals'],
                     r['step2']['character_sets'],
                     r['warnings']['step11']):
            self.assertEqual(part['state'], 'not yet reported')
        self.assertEqual(r['warnings']['step0']['state'], 'not run')
        for n in (1, 2):
            self.assertEqual(r['warnings'][f'step{n}'], {
                'state': 'not reported',
                'why': f'the step wrote no step{n}.{PAIR}.warnings.jsonl'})

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


class Warnings(_Folder):
    """The `!!` of steps 0, 1 and 2, each read from the file its step
    wrote beside its log, with its list and its title."""

    def test_every_kind_and_its_title(self):
        step2_files(self.d)
        warnings_file(self.d, 1, list(SAMPLES))
        items = self.written()['warnings']['step1']['items']
        self.assertEqual([i['kind'] for i in items], list(SAMPLES))
        for i in items:
            with self.subTest(kind=i['kind']):
                self.assertEqual(i['title'], SAMPLES[i['kind']][1])
                self.assertEqual(i['data'], SAMPLES[i['kind']][0])
                self.assertEqual(i['text'], f"!! {i['kind']}")
        self.assertEqual(sorted(SAMPLES), sorted(rd.KINDS))

    def test_a_build_is_written_by_its_name(self):
        """A build's commit id in a block or its data is written as the
        build's name, as the screen shows it (glibc_locale_data.named):
        step 2 reads a build by its commit (review of 24.1b-2, round 1)."""
        import glibc_locale_data as g
        sha = 'a' * 40
        text = f'!! NOT RUN: ... KOI8-R at {sha}.'
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(g._NAMES, {sha: 'glibc-2.34-100.el9'}), \
                mock.patch.dict(os.environ, {
                    g.WARNINGS_ENV: os.path.join(tmp, 'w')}):
            g.record_warning('charmaps_not_run',
                             {'why': f'KOI8-R at {sha}'}, text)
            g.record_warning('charmap_builds_unsettled', {'builds': [
                {'build': 'x', 'charmap': 'UTF-8', 'why': f'at {sha}'}]},
                f'!! x (UTF-8): at {sha}')
            g.record_warning('c_utf8_no_source', {'sides': (sha,)},
                             f'!! {sha} holds no C')
            with open(os.path.join(tmp, 'w'), encoding='utf-8') as f:
                w, w2, w3 = (json.loads(ln) for ln in f.read().splitlines())
        self.assertEqual(w2['data']['builds'][0]['why'],
                         'at glibc-2.34-100.el9')
        self.assertEqual(w3['data'], {'sides': ['glibc-2.34-100.el9']})
        self.assertEqual(w, {'kind': 'charmaps_not_run',
                             'data': {'why': 'KOI8-R at glibc-2.34-100.el9'},
                             'text': '!! NOT RUN: ... KOI8-R at '
                                     'glibc-2.34-100.el9.'})

    def test_every_kind_written_is_a_kind_read(self):
        """The kinds the calls that print a `!!` write are KINDS, every
        one as a literal: glibc_locale_data's warn() (each once), and
        step 2's _warn_block and _bang (node_charmaps,
        filter_lc_collate_changes); a call without one would be a block
        with no data."""
        import ast
        where = {'warn': 1, '_warn_block': 2, '_bang': 0}
        kinds = []
        for name in ('glibc_locale_data.py', 'node_charmaps.py',
                     'filter_lc_collate_changes.py'):
            src = os.path.join(os.path.dirname(rd.__file__), name)
            with open(src, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            # _warn_block hands its own kind on to _bang.
            inner = {id(x) for d in ast.walk(tree)
                     if isinstance(d, ast.FunctionDef)
                     and d.name == '_warn_block' for x in ast.walk(d)}
            for n in ast.walk(tree):
                if id(n) in inner:
                    continue
                fn = n.func if isinstance(n, ast.Call) else None
                fn = getattr(fn, 'id', getattr(fn, 'attr', ''))
                if fn in where and not (name == 'glibc_locale_data.py'
                                        and fn != 'warn'):
                    i = where[fn]
                    self.assertTrue(len(n.args) > i and isinstance(
                        n.args[i], ast.Constant), f'{name}:{n.lineno}')
                    kinds.append((name, n.args[i].value))
        gld = [k for f, k in kinds if f == 'glibc_locale_data.py']
        self.assertEqual(len(gld), len(set(gld)))
        self.assertEqual(sorted({k for _, k in kinds}), sorted(rd.KINDS))

    def test_the_lists(self):
        step2_files(self.d)
        warnings_file(self.d, 1, list(SAMPLES))
        items = self.written()['warnings']['step1']['items']
        self.assertEqual([i['kind'] for i in items if i['class'] == 'change'],
                         ['collation_version_same', 'c_utf8_differs',
                          'charmap_distro_buildable'])

    def test_titles_by_the_data(self):
        """What the data changes in a title: two signature statuses and
        a commit among the refs, both machines unread, one class of side
        or two, one entry skipped."""
        t = lambda k, d: rd.KINDS[k][2](d)
        self.assertEqual(t('signatures_unchecked', {'refs': [
            {'ref': 'glibc-2.28', 'status': 'no-key'},
            {'ref': 'abc', 'status': 'not-a-tag'}]}),
            'Signatures of glibc-2.28, abc (not verified: no signing key, '
            'or gpg failed; a commit or a lightweight tag, which carries no '
            'signature)')
        self.assertEqual(t('machine_falls_back_to_tags', {
            'unread': [{'side': 'old', 'build': 'x', 'why': 'w'},
                       {'side': 'new', 'build': 'y', 'why': 'w'}],
            'tags': ['a', 'b']}),
            "Neither machine's build could be read; step 2 read the tags")
        self.assertEqual(t('sides_read_as_tags', {
            'refs': ['abc'], 'classes': ['commit'], 'ask': 'none'}),
            "The distro's own locale files and patches (abc is an upstream "
            "commit)")
        self.assertEqual(t('sides_read_as_tags', {
            'refs': ['glibc-2.28', 'abc'], 'classes': ['tag', 'commit'],
            'ask': 'build'}),
            "The distro's own locale files and patches (glibc-2.28 and abc "
            "are an upstream tag and an upstream commit)")
        self.assertEqual(t('machine_entries_skipped', {
            'machine': 'm', 'entries': [{'entry': 'x', 'why': 'y'}]}),
            "1 entry of m's files not read")
        # Step 2's, with one member, and the builds measured on two
        # machines.
        self.assertEqual(t('added_not_at_old', {
            'at': 'abc', 'named': 'commit', 'locales': ['x']}),
            'Whether 1 locale with no file at abc existed on the old system')
        self.assertEqual(t('c_utf8_no_source', {'sides': ['glibc-2.34']}),
                         "C.UTF-8's sort rules (no source file at "
                         "glibc-2.34)")
        self.assertEqual(t('rules_not_compared', {
            'groups': [{'names': ['a'], 'why': 'w'}]}),
            'Sort rules of 1 locale, listed in the summary')
        self.assertEqual(t('charmap_sources_missing', {
            'entries': ['x'], 'basis': 'measured'}),
            'Character sets of 1 locale build whose source file is not on '
            'both sides')
        self.assertEqual(t('charmap_sources_missing', {
            'entries': ['x'], 'basis': 'SUPPORTED'}),
            'Character sets of 1 SUPPORTED entry whose source file is not on '
            'both sides')
        self.assertEqual(t('charmap_search_not_run', {
            'charmaps': [{'charmap': 'A', 'why': 'w'}]}),
            'Locales a distro can build in 1 changed character set')

    def test_step0_with_the_machines(self):
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        over = dict(ON_MACHINES)
        self.assertEqual(self.written(**over)['warnings']['step0'], {
            'state': 'not reported',
            'why': f'the step wrote no step0.{PAIR}.warnings.jsonl'})
        warnings_file(self.d, 0, [])
        self.assertEqual(self.written(**over)['warnings']['step0'],
                         {'state': 'reported', 'items': []})
        warnings_file(self.d, 0, ['machine_falls_back_to_tags'])
        self.assertEqual(
            [i['kind'] for i in
             self.written(**over)['warnings']['step0']['items']],
            ['machine_falls_back_to_tags'])

    def test_a_step0_file_without_the_machines_is_not_read(self):
        step2_files(self.d)
        warnings_file(self.d, 0, ['same_tree'])
        self.assertEqual(self.written()['warnings']['step0']['state'],
                         'not run')

    def test_an_empty_file_is_no_warning(self):
        step2_files(self.d)
        warnings_file(self.d, 1, [])
        self.assertEqual(self.written()['warnings']['step1'],
                         {'state': 'reported', 'items': []})


class TheLogAgainstTheFile(_Folder):
    """_harness.check_warnings and check_summary_warnings, which every
    audit.sh run of the suite that reaches its summary passes through: the
    `!!` blocks of each step's log, read as audit.sh's warnings block
    reads them, are the step's warnings in the data file, one by one; and
    the summary's warnings block is those of steps 0, 1 and 2, each text
    once, in order."""

    LOG = ('Tag provenance:\n'
           '!! first block,\n'
           '   its second line\n'
           '     an item\n'
           'a line of no block\n'
           '!! second block\n')
    BLOCKS = ['!! first block,\n   its second line\n     an item',
              '!! second block']

    def report(self, texts, step0=None, step2=()):
        item = lambda t: {'kind': 'machine_not_checked', 'data': {},
                          'text': t}
        return {'warnings': {
            'step0': step0 or {'state': 'not run', 'why': 'x'},
            'step1': {'state': 'reported',
                      'items': [item(t) for t in texts]},
            'step2': {'state': 'reported',
                      'items': [item(t) for t in step2]}}}

    def check(self, texts, **more):
        write(self.d, f'step1.{PAIR}.log', self.LOG)
        _harness.check_warnings(self.report(texts, **more), self.d,
                                os.path.join(self.d, f'report.{PAIR}.json'))

    def test_the_blocks_as_audit_sh_reads_them(self):
        write(self.d, 'x.log', self.LOG)
        self.assertEqual(_harness.bang_blocks(os.path.join(self.d, 'x.log')),
                         self.BLOCKS)
        self.assertEqual(_harness.bang_blocks(os.path.join(self.d, 'no.log')),
                         [])

    def test_the_same_blocks_pass(self):
        self.check(self.BLOCKS)

    def test_each_difference_is_refused(self):
        for texts in (self.BLOCKS[:1], self.BLOCKS + ['!! third'],
                      self.BLOCKS[::-1], ['!! first block,', '!! second block'],
                      []):
            with self.subTest(texts=texts):
                with self.assertRaises(AssertionError):
                    self.check(texts)

    def test_each_data_value_is_in_its_block(self):
        """data_in_text: a name or reason not in the block, or old and new
        swapped, is refused; closed words and a name broken at a hyphen
        are not."""
        text = ('!! glibc-2.34-275.el9_8 and glibc-2.34-283.el9_8 are two '
                'commits of one\n   source tree; why: no zstd reader')
        ok = {'old': 'glibc-2.34-275.el9_8', 'new': 'glibc-2.34-283.el9_8'}
        _harness.data_in_text('same_tree', ok, text)
        _harness.data_in_text('x', {'why': 'no zstd reader', 'ask': 'none',
                                    'side': 'old'}, text)
        _harness.data_in_text('x', {'old': 'glibc-2.34-', 'new': 'glibc-2.34-'},
                              'glibc-2.34-\n   275')
        for kind, data in (
                ('same_tree', {'old': ok['new'], 'new': ok['old']}),
                ('same_tree', {'old': 'qqq', 'new': ok['new']}),
                ('same_tree', {'old': ok['new'], 'new': ok['new']}),
                ('direction_undetermined', ok),
                ('x', {'entries': [{'entry': 'de_IT', 'why': 'symlink'}]}),
                ('x', {'why': 'qqq'})):
            with self.subTest(data=data):
                with self.assertRaises(AssertionError):
                    _harness.data_in_text(kind, data, text)

    def test_a_step_not_reported_is_refused(self):
        write(self.d, f'step1.{PAIR}.log', self.LOG)
        r = self.report(self.BLOCKS)
        r['warnings']['step1'] = {'state': 'not reported', 'why': 'x'}
        with self.assertRaises(AssertionError):
            _harness.check_warnings(r, self.d, os.path.join(
                self.d, f'report.{PAIR}.json'))

    def test_step2_against_its_own_log(self):
        """Step 2 is held to its log like step 1: a block of its log the
        data file lacks, or one it adds, is refused."""
        write(self.d, f'step2.{PAIR}.log', 'Rules:\n!! two\n   lines\n')
        self.check(self.BLOCKS, step2=['!! two\n   lines'])
        for step2 in ([], ['!! two'], ['!! two\n   lines', '!! more']):
            with self.subTest(step2=step2):
                with self.assertRaises(AssertionError):
                    self.check(self.BLOCKS, step2=step2)

    SUMMARY = ('== AUDIT SUMMARY  a -> b\n'
               'Listed:\n'
               '!! not this one, above the block\n'
               '\n'
               '-- Warnings the clean results above do NOT cover\n'
               '{}'
               '(see docs/limitations.md)\n')

    def summary(self, step1, step2, printed):
        _harness.check_summary_warnings(
            self.report(step1, step2=step2),
            self.SUMMARY.format(''.join(b + '\n' for b in printed)), 'r')

    def test_the_summary_repeats_steps_0_to_2_once_each(self):
        """In order, a text two steps printed once; no warning, no block.
        A block missing, added, out of order or repeated is refused, and
        so is a block with no end or a line that is not of a block."""
        one, two = '!! one\n   wrapped', '!! two'
        self.summary([one], [two, one], [one, two])
        _harness.check_summary_warnings(self.report([]), 'x\n', 'r')
        for printed in ([one], [two, one], [one, two, one],
                        [one, two, '!! three'], []):
            with self.subTest(printed=printed):
                with self.assertRaises(AssertionError):
                    self.summary([one], [two, one], printed)
        with self.assertRaises(AssertionError):
            _harness.check_summary_warnings(
                self.report([]), self.SUMMARY.format(''), 'r')
        for out in (self.SUMMARY.format(one + '\n').replace(
                        '(see docs/limitations.md)\n', ''),
                    self.SUMMARY.format(one + '\nnot a block line\n')):
            with self.subTest(out=out):
                with self.assertRaises(AssertionError):
                    _harness.check_summary_warnings(self.report([one]), out,
                                                    'r')

    def test_step0_against_its_own_log(self):
        """Reported, step 0 is held to its log; with no log, to none."""
        step0 = {'state': 'reported', 'items': []}
        self.check(self.BLOCKS, step0=step0)
        write(self.d, f'step0.{PAIR}.log', '!! fell back\n')
        with self.assertRaises(AssertionError):
            self.check(self.BLOCKS, step0=step0)


class AWarningRefused(_Folder):
    """A line of a step's warnings file that is not a known kind with its
    data: refused, and no report written."""

    def refused(self, line):
        step2_files(self.d)
        write(self.d, f'step1.{PAIR}.warnings.jsonl', line)
        argv = ['--out-dir', self.d, '--pair', PAIR, '--old', 'glibc-2.28',
                '--new', 'glibc-2.34', '--route', 'tags',
                '--order-words', 'forward none tag tag tags']
        self.assertEqual(rd.main(argv), 1)
        self.assertEqual([n for n in os.listdir(self.d)
                          if n.startswith('report.')], [])

    def line(self, kind='same_tree', data=None, text='!! x', **more):
        w = {'kind': kind, 'data': SAMPLES['same_tree'][0] if data is None
             else data, 'text': text, **more}
        return json.dumps(w) + '\n'

    def test_the_good_line_is_read(self):
        step2_files(self.d)
        write(self.d, f'step1.{PAIR}.warnings.jsonl', self.line())
        self.assertEqual(len(self.written()['warnings']['step1']['items']), 1)

    def test_an_unknown_kind(self):
        self.refused(self.line(kind='same_thing'))

    def test_data_of_another_shape(self):
        self.refused(self.line(data={'old': 'a'}))
        self.refused(self.line(data={'old': 'a', 'new': 'b', 'x': 1}))
        self.refused(self.line(data={'old': 'a', 'new': ''}))
        self.refused(self.line(data={'old': 'a', 'new': 3}))
        self.refused(self.line(data=['a', 'b']))

    def test_each_value_check(self):
        """One bad value per kind whose values are more than text."""
        bad = {
            'same_tree_machines': {'old': 'a', 'new': 'b', 'charmaps': 'yes'},
            'signatures_unchecked': {'refs': [{'ref': 'x', 'status': 'good'}]},
            'machine_falls_back_to_tags': {
                'unread': [{'side': 'both', 'build': 'x', 'why': 'w'}],
                'tags': ['a', 'b']},
            'sides_read_as_tags': {'refs': ['a'], 'classes': ['build'],
                                   'ask': 'build'},
            'code_not_compared': {'step11': 1},
            'collation_version_unknown': {'sides': []},
            'machine_entries_skipped': {'machine': 'm', 'entries': []},
            'build_not_checked': {'metadata': True,
                                  'unlisted': [{'build': 'b'}]},
        }
        for kind, data in bad.items():
            with self.subTest(kind=kind):
                self.refused(self.line(kind=kind, data=data))

    def test_a_text_that_is_not_a_block(self):
        self.refused(self.line(text='Not checked'))

    def test_a_key_added_or_missing(self):
        self.refused(self.line(step=1))
        self.refused(json.dumps({'kind': 'same_tree',
                                 'data': SAMPLES['same_tree'][0]}) + '\n')

    def test_not_json_or_cut_short(self):
        self.refused('{"kind": \n')
        self.refused(self.line().rstrip('\n'))


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
        warnings_file(self.d, 0, [])
        warnings_file(self.d, 1, ['signatures_unchecked',
                                  'collation_version_same'])
        warnings_file(self.d, 2, ['c_utf8_differs', 'names_untraced'])
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
                     ('run', 'machines'), ('warnings', 'step0'),
                     ('warnings', 'step1'), ('warnings', 'step2'),
                     ('compared',),
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

    def test_a_warning_whose_list_or_title_is_not_its_kinds(self):
        """Each item is held to KINDS: its list, its title, its data."""
        item = lambda r: r['warnings']['step1']['items'][1]
        self.refuses(lambda r: item(r).update({'class': 'not checked'}))
        self.refuses(lambda r: item(r).update(title='Nothing changed'))
        self.refuses(lambda r: item(r).update(kind='same_commit'))
        self.refuses(lambda r: item(r)['data'].update(version=''))
        self.refuses(lambda r: item(r).pop('title'))
        step2 = lambda r: r['warnings']['step2']['items'][1]
        self.refuses(lambda r: step2(r).update(title='Nothing changed'))
        self.refuses(lambda r: step2(r)['data']['names'].pop())
        self.refuses(lambda r: r['warnings'].update(step2=rd.NOT_YET))

    def test_step0_not_run_with_the_machines_given(self):
        def change(r):
            r['warnings']['step0'] = {'state': 'not run', 'why': 'x'}
        self.refuses(change)

    def test_not_run_says_what_it_leaves(self):
        def change(r):
            r['step11'] = {'state': 'not run', 'why': 'x', 'not_done': []}
        self.refuses(change)


if __name__ == '__main__':
    unittest.main()
