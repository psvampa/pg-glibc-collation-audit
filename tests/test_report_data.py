"""scripts/report_data.py: the run's data file, report.<pair>.json, assembled
from the files the steps wrote, and load(), its strict reader; and
scripts/report_html.py, the summary page written beside it (TheSummaryPage).

What each audit.sh run of the suite that reaches its summary is checked
against is in _harness.check_report. This module holds the files of each
kind of run read back, and what no run reaches: a file a step did not
write (each one said as 'not reported', never as an empty list), a file
cut short or two files that do not agree, and a report that load() must
refuse (a key missing or added, a state outside its list, a null, another
format, facts that do not hang together). The `!!` of steps 0, 1, 2 and
11: one of every kind with its title, and the lines of a step's warnings
file that are refused."""
import copy
import html
import json
import os
import re
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
    step2_rules_by_name (tags, something compared, check compared), and
    step2_rules follows them: compared nothing where by_name says so, else
    or_IN's group, the removed rows, and SUPPORTED not applicable on two
    machines."""
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
    rows = [{'name': m[1], 'renamed_to': m[3] or '-'} for m in (
        rd.REMOVED_ROW.fullmatch(x) for x in removed.split('\n') if x)]
    write(d, f'step2_rules.{PAIR}.json', json.dumps(
        {'step': 2, 'state': 'not compared', 'why': data['what']}
        if data['nothing'] else
        {'step': 2, 'state': 'compared',
         'totals': {'names': 11 + len(rows), 'same': 10, 'differ': 1,
                    'not_compared': len(rows)},
         'differ': [{'names': ['or_IN'], 'every_rule': True,
                     'characters': {'state': 'counted', 'count': 2}}],
         'added': {'state': 'none', 'rows': []}, 'removed': rows,
         'unread': [],
         'unbuilt': ({'state': 'not applicable',
                      'why': "a machine's file holds no SUPPORTED"}
                     if data['machines'] else
                     {'state': 'listed', 'names': []}),
         'not_ascii': []}) + '\n')
    cm = data['charmaps']
    write(d, f'step2_character_sets.{PAIR}.json', json.dumps(
        {'step': 2, 'state': 'compared',
         'basis': 'measured' if data['machines'] else 'SUPPORTED',
         'builds': 1, 'charmaps': ['UTF-8'], 'changed': [],
         'listed': [],
         'anyway': [], 'unsettled': [], 'distro': [],
         'unsearched': [], 'not_compared': []}
        if cm['state'] == 'compared' else
        dict({k: v for k, v in cm.items() if v is not None}, step=2,
             **({'not_compared': []} if cm['state'] == 'not run'
                else {}))) + '\n')
    write(d, f'step2_transliteration.{PAIR}.json', json.dumps(
        {'step': 2, 'state': 'compared', 'line': 'none', 'listed': [],
         'unsettled': [], 'skipped': []}
        if cm['state'] == 'compared' else
        dict({k: v for k, v in cm.items() if v is not None}, step=2))
        + '\n')


MACHINES = dict(
    compared='builds', machines=True, measurements=['a' * 64, 'b' * 64],
    locales={'C': [None, 'built in', 'built into glibc, no locale file', '-',
                   None],
             'sv_SE': ['sv_SE', 'differs', '', '-', 'sv_SE']})


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
          'sv_SE\tsort table changed\t-\tits compiled sort table '
          'changed\t-\t-\t-\t-\n'
          'sv_SE\tmoved\tU+00E5\t-\tx\t>\t<\ton both machines\n'
          'sv_SE,sv_SE.utf8\tmoved\tU+00E4\t-\tx\t>\t<\ton both machines\n'
          '# Control: held.\n'
          '# end: 2 locale group(s), 3 row(s)\n')
    write(d, f'measured_order_confirm.{PAIR}.LATIN1.sql', 'select 1;\n')
    write(d, f'measured_order_confirm.{PAIR}.UTF8.sql', 'select 1;\n')
    # Another pair's, whose new side's name extends this one's (16.109).
    write(d, f'measured_order_confirm.{PAIR}.1.UTF8.sql', 'select 1;\n')
    write(d, f'measured_order_rules.{PAIR}.txt', '     Each locale...\n')
    write(d, f'step11.{PAIR}.data.json',
          json.dumps({'step': 11, 'counts': COUNTS,
                      'findings': FINDINGS, 'checks': CHECKS,
                      'sql': SQL}) + '\n')


# What step 11 says of step11_files' SQL files (locale_order.sql_data):
# two files, and two entries no file checks: locales the new machine
# lacks ('unknown'), with what else PostgreSQL calls them, and the
# character rules of every locale ('ctype'), without.
SQL = {'state': 'written',
       'files': [{'encoding': 'LATIN1', 'tests': 4},
                 {'encoding': 'UTF8', 'tests': 38761}],
       'not_checked': [
           {'names': ['en_US.utf8@ampm', 'en_US@ampm'],
            'why': 'it is not on the new machine', 'about': 'unknown',
            'also': 'database locale en_US.UTF-8@ampm'},
           {'names': ['the character rules (LC_CTYPE) of every locale'],
            'why': 'not compared', 'about': 'ctype'}]}


# The findings of step11_files' changed locale, sv_SE, as
# locale_order.change_groups writes them: one of each kind, and a pair
# named by its bytes.
FINDINGS = {'state': 'reported', 'groups': [{
    'locales': {'sv_SE': {
        'codeset': {'old': 'ISO-8859-1', 'new': 'ISO-8859-1'},
        'postgres': {'alias': 'none', 'spelling': 'none',
                     'reach': 'imported'}}},
    'printed_under': 0, 'found_by': 'step 11',
    'findings': [
        {'kind': 'moved', 'count': 1, 'characters': 1, 'other_strings': 0,
         'runs': [['U+00E5', 'U+00E5']], 'listed': 'each'},
        {'kind': 'reversed', 'count': 2, 'characters': 2,
         'other_strings': 0, 'all': False, 'runs': [['U+00E4', 'U+00E5']]},
        {'kind': 'probes changed',
         'before': [{'level': 'letters', 'state': 'pair',
                     'pair': ['U+0061', 'U+0062'], 'from_the_end': False},
                    {'level': 'accents', 'state': 'pair',
                     'pair': ['U+0061', 'U+00E1'], 'from_the_end': True},
                    {'level': 'case', 'state': 'pair',
                     'pair': ['U+0061', 'U+0041'], 'from_the_end': False}],
         'now': [{'level': 'letters', 'state': 'pair',
                  'pair': ['U+0061', 'U+0062'], 'from_the_end': False},
                 {'level': 'accents', 'state': 'not measured'},
                 {'level': 'case', 'state': 'pair',
                  'pair': ['U+0061', 'U+0041'], 'from_the_end': False}]},
        {'kind': 'left out', 'compared': ['letters', 'case'],
         'left_out': ['accents']},
        {'kind': 'told apart', 'count': 2, 'compared': ['letters', 'case'],
         'probes': [{'level': 'letters', 'state': 'pair',
                     'pair': ['U+0061', 'U+0062'], 'from_the_end': False},
                    {'level': 'accents', 'state': 'pair',
                     'pair': ['U+0061', 'U+00E1'], 'from_the_end': True},
                    {'level': 'case', 'state': 'pair',
                     'pair': ['U+0061', 'U+0041'], 'from_the_end': False}],
         'listed': 'each',
         'pairs': [{'strings': ['U+00E4', 'U+00E5'],
                    'before': 'accents or case', 'now': 'letters'},
                   {'strings': ['U+00E5', 'U+00E4'], 'before': 'not at all',
                    'now': 'less than case'}],
         'chains': [{'runs': [['U+00E4', 'U+00E5']],
                     'before': 'accents or case', 'now': 'letters',
                     'measured': 'both',
                     'test': {'state': 'pair', 'level': 'letters',
                              'before': '>', 'now': '<'}},
                    {'runs': [['U+00E5', 'U+00E5'], ['U+00E4', 'U+00E4']],
                     'before': 'not at all', 'now': 'less than case',
                     'measured': 'new', 'test': {'state': 'none'}}]},
        {'kind': 'did not move', 'count': 1, 'characters': 1,
         'other_strings': 0, 'runs': [['U+00E4', 'U+00E4']],
         'listed': 'runs'},
        {'kind': 'undecided', 'count': 1, 'pairs': [['U+0061', 'bytes 80']]}],
    'names_bytes': True, 'sort_table': {
        'state': 'changed', 'step11': 'changed',
        'reason': 'no levels on one machine'}}], 'tables_only': []}


# The counts of step11_files' two locales, C unchanged (firm, built into
# glibc) and sv_SE changed, as locale_order.write_counts writes them.
COUNTS = {'machines': {'old': {'label': 'glibc-2.28-251.el8', 'locales': 2},
                       'new': {'label': 'glibc-2.34-100.el9', 'locales': 2}},
          'changed': 1, 'unchanged': 1, 'unchanged_as_far_as_measured': 0,
          'not_measured': 0, 'refused': 0, 'only_old': 0, 'only_new': 0}


# The checks of step11_files' two machines, as locale_order.write_counts
# writes them.
_LIBC = {'state': 'checked', 'replaced': False, 'stale': 0, 'other': 0,
         'processes': 1, 'looked_for': ['postgres', 'postmaster'],
         'not_checked': {'machine': [], 'named': [], 'unnamed': 0},
         'result': 'clean'}
CHECKS = {'control': {s: {n: {'state': 'byte order'} for n in ('C', 'POSIX')}
                      for s in ('old', 'new')},
          'money': {'state': 'compared', 'compared': 2, 'changed': [],
                    'not_checked': []},
          'postgres_libc': {'old': _LIBC, 'new': _LIBC, 'note': True},
          'postgres_use': {'state': 'checked', 'rows': [],
                           'lines': {'C': 'one byte', 'sv_SE': 'converts'},
                           'added': {}, 'back': [],
                           'groups': [], 'back_groups': [],
                           'back_changed': [], 'part_unknown': [],
                           'back_not_checked': [], 'converts': 1,
                           'one_byte': 1, 'left_out': [], 'elsewhere': 0},
          'ctype': {'state': 'compared',
                    'lines': {'C': 'built in', 'sv_SE': 'utf8'},
                    'character_groups': [], 'byte_groups': [],
                    'euc': {'ascii_alone': [], 'character_groups': [],
                            'not_checked': []},
                    'utf8': {'names': []}},
          'charmaps': {'state': 'checked', 'checked': 1, 'by_table': 0,
                       'builtin': ['C']}}


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
    # Step 11's.
    'tag_names_no_release': (
        {'side': 'old', 'tag': 'abc'},
        'Whether the old machine runs the glibc of abc'),
    'version_names_no_release': (
        {'side': 'new', 'version': 'glibc 2.34.9000', 'tag': 'glibc-2.34'},
        'Whether glibc 2.34.9000, measured on the new machine, is the glibc '
        'of glibc-2.34'),
    'same_build': ({}, 'Two machines on the same glibc build, not an '
                   'upgrade'),
    'machines_order_undetermined': (
        {'version': 'glibc 2.34'},
        'Which machine is the older one (both run glibc 2.34)'),
    'tables_disagree': (
        {'names': ['a', 'b'], 'more': 3},
        '5 locales changed whose sort tables find no change: the machines '
        'compare with different code'),
    'tables_not_compared': (
        {'groups': [{'why': 'w', 'names': ['a'], 'more': 0},
                    {'why': 'v', 'names': ['b', 'c'], 'more': 1}]},
        'Sort tables of 4 locales not compared'),
    'partly_measured': (
        {'locales': [{'name': 'a', 'count': 2}, {'name': 'b', 'count': 1}]},
        '2 locales unchanged only as far as measured: some neighbours not '
        'measured at every level'),
    'measured_in_part': (
        {'groups': [{'names': ['a', 'b'], 'side': 'old', 'charmap': 'X',
                     'count': 4, 'examples': ['bytes 80']},
                    {'names': ['b'], 'side': 'new', 'charmap': 'Y',
                     'count': 1, 'examples': ['bytes 81']}]},
        '2 locales measured in part: their character set has sequences not '
        'measured'),
    'charmaps_not_checked': (
        {'side': 'new', 'named_by_bytes': False,
         'groups': [{'names': ['a', 'b'], 'why': 'w'},
                    {'names': ['c'], 'why': 'v'}]},
        'The character sets of 3 locales of the new machine'),
    'charmaps_machine_not_checked': (
        {'side': 'either', 'why': 'w', 'named_by_bytes': True},
        'The character sets of either machine'),
    'glibc_refused': (
        {'locales': [{'name': 'a', 'side': 'old',
                      'categories': ['LC_COLLATE']},
                     {'name': 'a', 'side': 'new',
                      'categories': ['LC_CTYPE']},
                     {'name': 'b', 'side': 'both',
                      'categories': ['LC_COLLATE', 'LC_CTYPE']}]},
        '2 locales glibc refuses: a database that uses one does not open'),
    'not_measured': (
        {'locales': [{'name': 'a', 'reason': 'error', 'side': 'old',
                      'why': 'w'},
                     {'name': 'b', 'reason': 'different strings',
                      'side': 'both', 'why': 'v'}]},
        '2 locales that could not be measured'),
    'not_on_new_machine': (
        {'locales': ['a', 'b']},
        '2 locales not on the new machine: a database that uses one cannot '
        'use it there'),
    'closing': ({'state': 'tables'},
                'Whether the two machines compare strings with the same glibc '
                'code'),
    'control_failed': (
        {'side': 'new', 'names': ['C', 'POSIX']},
        'What was measured on the new machine: the control failed (C and '
        'POSIX did not measure as byte order)'),
    'control_not_checked': (
        {'absent': [{'name': 'C', 'side': 'old'}],
         'unmeasured': [{'name': 'POSIX', 'side': 'new', 'error': 'e'}]},
        'The control of the measurement on both machines: C and POSIX as '
        'byte order'),
    'money_earlier_format': (
        {'side': 'both'},
        'Decimals of money on both machines (a file of an earlier '
        'version)'),
    'money_changed': (
        {'locales': [{'name': 'a', 'old': 2, 'new': 3},
                     {'name': 'b', 'old': 0, 'new': 2}]},
        'Decimals of money change in 2 locales'),
    'money_not_checked': (
        {'locales': [{'name': 'a', 'side': 'old', 'why': 'w'},
                     {'name': 'a', 'side': 'new', 'why': 'v'}]},
        'Decimals of money of 1 locale'),
    'libc_earlier_format': (
        {'side': 'old'},
        'Which C library PostgreSQL runs on the old machine (a file of an '
        'earlier version)'),
    'glibc_replaced': (
        {'side': 'new'},
        'What the new machine runs now: its glibc was replaced while it '
        'was measured'),
    'libc_stale': (
        {'side': 'new', 'processes': 2},
        '2 PostgreSQL processes of the new machine still run the glibc '
        'replaced on disk'),
    'libc_other': (
        {'side': 'old', 'processes': 3},
        '3 PostgreSQL processes of the old machine run another C library '
        'than the one measured'),
    'libc_not_checked': (
        {'side': 'old', 'machine': ['m'],
         'named': [{'reason': 'could not be read', 'count': 1,
                    'processes': 2, 'looked_for': ['postgres']}],
         'unnamed': 0},
        'Which C library every PostgreSQL process of the old machine runs'),
    'libc_no_process': (
        {'side': 'new', 'looked_for': ['postgres', 'postmaster']},
        'Which C library PostgreSQL runs on the new machine: no process '
        'named postgres or postmaster found'),
    # Step 11's lines on upper(), lower() and ILIKE.
    'use_earlier_format': (
        {'side': 'old'},
        'upper(), lower() and ILIKE on the old machine (a file of an earlier '
        'version)'),
    'use_error_new': (
        {'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP', 'japanese']},
                    {'encoding': 'EUC-KR', 'names': ['ko_KR']}]},
        'upper(), lower() and ILIKE give an error on the new machine in 3 '
        'locales: its glibc cannot convert their encoding'),
    'use_error_both': (
        {'groups': [{'encoding': 'EUC-TW', 'names': ['zh_TW.euctw']}]},
        'upper(), lower() and ILIKE give an error on both machines in 1 '
        'locale, as before the upgrade'),
    'use_error_moved': (
        {'side': 'new', 'from': 'EUC-JP', 'to': 'BIG5', 'why': [],
         'groups': [{'encoding': 'BIG5', 'names': ['zh_TW']}]},
        'upper(), lower() and ILIKE give an error on the new machine in 1 '
        'locale whose encoding changed from EUC-JP to BIG5'),
    'use_error_any_db': (
        {'side': 'both', 'why': 'w',
         'groups': [{'encoding': 'TIS-620', 'names': ['th_TH', 'thai']}]},
        'upper(), lower() and ILIKE give an error on both machines in 2 '
        'locales that a database of any encoding may use'),
    'use_error_one_side': (
        {'side': 'new', 'reason': 'refused', 'why': [],
         'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP']}]},
        'upper(), lower() and ILIKE give an error on the new machine in 1 '
        'locale that glibc refused on the old machine'),
    'use_error_partial': (
        {'entries': [{'parts': [{'encoding': 'EUC-JP', 'alike': 'one side',
                                 'sides': [{'side': 'old', 'count': 4726}]}],
                      'unknown_on': ['new'], 'names': ['ja_JP']}]},
        'upper(), lower() and ILIKE give an error on part of the text in 1 '
        'locale'),
    'use_wrong_result': (
        {'entries': [{'parts': [{'encoding': 'GB2312', 'alike': 'same',
                                 'sides': [{'side': 'old', 'count': 38},
                                           {'side': 'new', 'count': 38}]}],
                      'lower': [{'side': 'both', 'count': 12}],
                      'names': ['zh_CN', 'zh_SG']}]},
        'upper(), lower() and ILIKE give a wrong result on part of the text '
        'in 2 locales'),
    'use_back_changed': (
        {'entries': [{'encoding': 'EUC-KR', 'count': 2,
                      'names': ['ko_KR']}]},
        'upper(), lower() and ILIKE give another result on part of the text '
        'in 1 locale'),
    'use_partial_unknown': (
        {'places': [{'side': 'new', 'groups': [
            {'encoding': 'EUC-JP', 'names': ['ja_JP']}]}]},
        'Whether upper(), lower() and ILIKE give an error on part of the '
        'text in 1 locale (a file of an earlier version)'),
    'use_back_not_checked': (
        {'side': 'both', 'utf8': 1,
         'encodings': [{'encoding': 'EUC-JP', 'count': 2},
                       {'encoding': 'UTF-8', 'count': 1}]},
        "Whether the result of upper(), lower() and ILIKE converts back to "
        "the database's bytes in 3 locales (a file of an earlier version)"),
    'use_not_checked': (
        {'reasons': [{'why': 'w', 'groups': [
            {'encoding': 'EUC-JP', 'names': ['ja_JP']}]}]},
        'upper(), lower() and ILIKE in 1 locale'),
    # Step 11's lines on the character rules and on a database of another
    # encoding.
    'ctype_earlier_format': (
        {'side': 'new'},
        'Character rules (LC_CTYPE) on the new machine (a file of an '
        'earlier version)'),
    'ctype_characters_changed': (
        {'locales': 486, 'names': [], 'count': 6525, 'classified': [
            {'character': 'U+0282', 'change': 'gains the capital'}]},
        'Character rules (LC_CTYPE) change for 6525 characters in 486 '
        'locales that a UTF8 database can use'),
    'ctype_bytes_changed': (
        {'locales': 2, 'names': ['tr_TR', 'tr_TR.iso88599'],
         'bytes': [{'byte': 'FD', 'change': 'letter -> punctuation'}]},
        'Character rules (LC_CTYPE) change for 1 byte in 2 locales'),
    'ctype_not_checked': (
        {'locales': [{'name': 'a', 'side': 'old', 'why': 'w'},
                     {'name': 'a', 'side': 'new', 'why': 'v'}]},
        'Character rules (LC_CTYPE) in 1 locale'),
    'euc_sequences_changed': (
        {'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP', 'japanese']}], 'sequences': [
            {'bytes': 'A1 C1', 'old': 'U+301C', 'new': 'U+FF5E'}]},
        'In an EUC database glibc converts 1 byte sequence to another '
        'character in 2 locales'),
    'euc_characters_changed': (
        {'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP', 'japanese']}], 'count': 3, 'classified': []},
        'Character rules (LC_CTYPE) in an EUC database change for 3 '
        'characters in 2 locales'),
    'euc_same': (
        {'other': False, 'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP', 'japanese']}]},
        'Character rules (LC_CTYPE) in an EUC database in 2 locales: the '
        'same for the text glibc converts on both machines'),
    'euc_ascii_same': (
        {'side': 'new', 'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP', 'japanese']}]},
        "Character rules (LC_CTYPE) in an EUC database in 2 locales: only "
        "ASCII compared, as the new machine's glibc cannot convert their "
        "encoding"),
    'euc_not_checked': (
        {'reasons': [{'why': 'W', 'side': 'both', 'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP', 'japanese']}]}]},
        'Character rules (LC_CTYPE) in an EUC database in 2 locales'),
    'utf8_changed': (
        {'locales': 3, 'sequences': [
            {'bytes': 'EF BF BD', 'old': 'U+FFFD', 'new': 'U+0041'},
            {'bytes': 'C2 80', 'old': 'U+0080', 'new': 'U+0041'}]},
        'In a UTF8 database glibc converts 2 byte sequences to another '
        'character in 3 locales'),
    'utf8_one_side': (
        {'side': 'old', 'why': [], 'locales': 1, 'sequences': [
            {'bytes': 'C2 80', 'old': 'U+0080', 'new': 'U+0041'}]},
        'In a UTF8 database, whether 1 byte sequence glibc reads as another '
        'character on the old machine changed, in 1 locale'),
    'other_encoding': (
        {'groups': [{'encoding': 'TIS-620', 'names': ['th_TH', 'thai']}],
         'same': ['th_TH'], 'told': True,
         'unknown': [{'side': 'old', 'groups': [
             {'encoding': 'TIS-620', 'names': ['thai']}]}],
         'errors': [{'encoding': 'TIS-620', 'alike': 'within', 'sides': [
             {'side': 'old', 'count': 0}, {'side': 'new', 'count': 2}]}],
         'whole': [], 'other': [{'encoding': 'TIS-620', 'count': 1}]},
        '2 locales whose encoding PostgreSQL does not know, in a database '
        'of another encoding'),
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
             old_node='', new_node='', tag_side='', step11=False,
             old_asked='', new_asked='')
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
        if a.old_asked:
            argv += ['--old-asked', a.old_asked, '--new-asked', a.new_asked]
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
        self.assertEqual(r['step2']['character_sets']['state'],
                         'reported')
        self.assertEqual(r['compared']['state'], 'not reported')
        self.assertEqual(r['step2']['transliteration'], {
            'state': 'reported', 'line': 'none', 'listed': [],
            'unsettled': [], 'skipped': []})
        self.assertEqual(r['step2']['rules']['totals'], {
            'names': 13, 'same': 10, 'differ': 1, 'not_compared': 2})
        self.assertEqual(r['warnings']['step11'], {
            'state': 'not run', 'why': "no machines' files"})
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
        self.assertEqual(len(s11['characters']['rows']), 3)
        self.assertEqual(s11['sql'], {
            'state': 'reported',
            'files': [{'encoding': 'LATIN1',
                       'file': f'measured_order_confirm.{PAIR}.LATIN1.sql',
                       'tests': 4},
                      {'encoding': 'UTF8',
                       'file': f'measured_order_confirm.{PAIR}.UTF8.sql',
                       'tests': 38761}],
            'not_checked': SQL['not_checked']})
        self.assertEqual(s11['rules_beside']['state'], 'set beside')
        self.assertEqual(s11['counts'], {'state': 'reported', **COUNTS})
        self.assertEqual(s11['findings'], FINDINGS)
        self.assertEqual(r['step2']['rules_by_name']['locales'], {
            'state': 'reported', 'by_name': {
                'C': {'key': 'none', 'rules': 'built in',
                      'why': 'built into glibc, no locale file',
                      'charmap': '-', 'source': 'none'},
                'sv_SE': {'key': 'sv_SE', 'rules': 'differs', 'why': '',
                          'charmap': '-', 'source': 'sv_SE'}}})
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
    """The `!!` of steps 0, 1, 2 and 11, each read from the file its
    step wrote beside its log, with its list and its title."""

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
        one as a literal: glibc_locale_data's warn() (each once), step
        2's _warn_block and _bang (node_charmaps,
        filter_lc_collate_changes), and step 11's (step11_kinds, which
        names those that are not a literal); a call without one would be
        a block with no data."""
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
        kinds += [('locale_order.py', k) for k in self.step11_kinds()]
        self.assertEqual(sorted({k for _, k in kinds}), sorted(rd.KINDS))

    def step11_kinds(self):
        """The kinds step 11 writes (locale_order.py), each a literal: the
        kind of every Bang, but the two that take their item's
        (`d['kind']`), check_lines' and use_lines' that take their text's
        (`d['text'].kind`), and kept's, which takes its text's
        (`text.kind`); and the kinds those items carry, under the
        key 'kind' of the charmaps' items and as the next to last member
        of the warnings compare() appends to `unchecked` and `warnings`."""
        import ast
        src = os.path.join(os.path.dirname(rd.__file__), 'locale_order.py')
        with open(src, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        kinds, passed = [], []
        charmaps = {id(x) for d in ast.walk(tree)
                    if isinstance(d, ast.FunctionDef)
                    and d.name == 'charmap_findings' for x in ast.walk(d)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'Bang':
                k = n.args[1]
                if isinstance(k, ast.Constant):
                    kinds.append(k.value)
                else:
                    passed.append(ast.unparse(k))
            elif isinstance(n, ast.Dict) and id(n) in charmaps:
                for key, v in zip(n.keys, n.values):
                    if isinstance(key, ast.Constant) and key.value == 'kind':
                        self.assertIsInstance(v, ast.Constant, n.lineno)
                        kinds.append(v.value)
            elif (isinstance(n, ast.Call)
                  and getattr(n.func, 'attr', '') == 'append'
                  and getattr(n.func.value, 'id', '') in ('unchecked',
                                                          'warnings')):
                t = n.args[0]
                self.assertIsInstance(t, ast.Tuple, n.lineno)
                self.assertIsInstance(t.elts[-2], ast.Constant, n.lineno)
                kinds.append(t.elts[-2].value)
        # warning_lines, unchecked_charmaps_lines; check_lines, use_lines;
        # kept, which ctype_lines and other_encoding_lines print through.
        self.assertEqual(sorted(passed), ["d['kind']", "d['kind']",
                                          "d['text'].kind", "d['text'].kind",
                                          'text.kind'])
        return kinds

    def test_the_lists(self):
        step2_files(self.d)
        warnings_file(self.d, 1, list(SAMPLES))
        items = self.written()['warnings']['step1']['items']
        self.assertEqual([i['kind'] for i in items if i['class'] == 'change'],
                         ['collation_version_same', 'c_utf8_differs',
                          'charmap_distro_buildable', 'glibc_refused',
                          'not_on_new_machine', 'money_changed', 'libc_stale',
                          'libc_other', 'use_error_new', 'use_error_both',
                          'use_error_moved', 'use_error_any_db',
                          'use_error_one_side', 'use_error_partial',
                          'use_wrong_result', 'use_back_changed',
                          'ctype_characters_changed', 'ctype_bytes_changed',
                          'euc_sequences_changed', 'euc_characters_changed',
                          'utf8_changed'])

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
        # Step 11's checks: one machine, the other or both; one process.
        self.assertEqual(t('control_not_checked', {
            'absent': [], 'unmeasured': [
                {'name': 'C', 'side': 'old', 'error': 'e'},
                {'name': 'POSIX', 'side': 'old', 'error': 'e'}]}),
            'The control of the measurement on the old machine: C and POSIX '
            'as byte order')
        self.assertEqual(t('money_earlier_format', {'side': 'new'}),
                         'Decimals of money on the new machine (a file of an '
                         'earlier version)')
        self.assertEqual(t('libc_earlier_format', {'side': 'both'}),
                         'Which C library PostgreSQL runs on both machines (a '
                         'file of an earlier version)')
        self.assertEqual(t('libc_stale', {'side': 'old', 'processes': 1}),
                         '1 PostgreSQL process of the old machine still runs '
                         'the glibc replaced on disk')
        self.assertEqual(t('libc_other', {'side': 'new', 'processes': 1}),
                         '1 PostgreSQL process of the new machine runs '
                         'another C library than the one measured')
        # upper(), lower() and ILIKE: each reason of one machine, both
        # machines, and a locale named in two places counted once.
        g = [{'encoding': 'EUC-JP', 'names': ['a', 'b']}]
        for reason, side, said in (
                ('refused', 'old', 'that glibc refused on the new machine'),
                ('absent', 'new', 'the old machine does not have'),
                ('unknown', 'new', 'not known on the old machine')):
            self.assertEqual(t('use_error_one_side', {
                'side': side, 'reason': reason, 'why': [], 'groups': g}),
                f'upper(), lower() and ILIKE give an error on the {side} '
                f'machine in 2 locales {said}')
        self.assertEqual(t('use_error_moved', {
            'side': 'both', 'from': 'X', 'to': 'Y', 'why': [], 'groups': g}),
            'upper(), lower() and ILIKE give an error on both machines in 2 '
            'locales whose encoding changed from X to Y')
        self.assertEqual(t('use_earlier_format', {'side': 'both'}),
                         'upper(), lower() and ILIKE on both machines (a file '
                         'of an earlier version)')
        self.assertEqual(t('use_not_checked', {'reasons': [
            {'why': 'w', 'groups': g}, {'why': 'v', 'groups': g}]}),
            'upper(), lower() and ILIKE in 2 locales')

    def test_what_a_step_cannot_write_is_refused(self):
        """Values each right alone and wrong together: the control's
        lists holding no locale, one twice or one in both; a decimals of
        money that does not change; processes not checked for no reason
        (review of 24.1c-2, round 1); the lines on upper(), lower() and
        ILIKE (use_impossible); and the lines on the character rules and
        on another encoding (ctype_impossible)."""
        c = {'name': 'C', 'side': 'old'}
        e = dict(c, error='e')
        for kind, data in (
                ('control_not_checked', {'absent': [], 'unmeasured': []}),
                ('control_not_checked', {'absent': [c, c], 'unmeasured': []}),
                ('control_not_checked', {'absent': [c], 'unmeasured': [e]}),
                ('money_changed', {'locales': [
                    {'name': 'a', 'old': 2, 'new': 3},
                    {'name': 'b', 'old': 2, 'new': 2}]}),
                ('libc_not_checked', {'side': 'old', 'machine': [],
                                      'named': [], 'unnamed': 0})) + \
                self.use_impossible() + self.ctype_impossible():
            with self.subTest(kind=kind, data=data), \
                    self.assertRaisesRegex(rd.ReportError,
                                           'cannot be written'):
                rd.warning_item('w', kind, data, '!! x')
        for kind in ('control_not_checked', 'money_changed',
                     'libc_not_checked') + tuple(
                         k for k in rd.KINDS if k.split('_')[0] in (
                             'use', 'ctype', 'euc', 'utf8', 'other')):
            rd.warning_item('w', kind, SAMPLES[kind][0], '!! x')

    def use_impossible(self):
        """The lines on upper(), lower() and ILIKE: one locale twice, an
        encoding that does not change, a reason where none is given or
        none where one is, a locale the old machine lacks named on the old
        one, what two machines refuse told apart against their counts, two
        parts of one encoding or of one machine, counts through lower() of
        a machine the
        parts do not name (two of one machine, one of the old machine
        where both refuse, one of both or two where one refuses),
        more of UTF-8 than in all."""
        def lack(alike, *counts, enc='E'):
            return {'encoding': enc, 'alike': alike, 'sides': [
                {'side': s, 'count': n} for s, n in zip(('old', 'new'),
                                                        counts)]}
        two = [{'encoding': 'E', 'names': ['a']},
               {'encoding': 'F', 'names': ['a']}]
        g = [{'encoding': 'E', 'names': ['a']}]
        out = [(k, {'groups': two}) for k in ('use_error_new',
                                              'use_error_both')]
        out += [
            ('use_error_moved', {'side': 'new', 'from': 'E', 'to': 'E',
                                 'why': [], 'groups': g}),
            ('use_error_one_side', {'side': 'new', 'reason': 'refused',
                                    'why': ['w'], 'groups': g}),
            ('use_error_one_side', {'side': 'new', 'reason': 'unknown',
                                    'why': [], 'groups': g}),
            ('use_error_one_side', {'side': 'old', 'reason': 'absent',
                                    'why': [], 'groups': g}),
            ('use_back_not_checked', {'side': 'old', 'utf8': 3, 'encodings': [
                {'encoding': 'UTF-8', 'count': 2}]})]
        for parts in ([lack('same', 2, 3)], [lack('mixed', 2, 2)],
                      [lack('one side', 2, 2)], [lack('within', 2)],
                      [lack('one side', 2), lack('one side', 3)],
                      [lack('one side', 2), lack('one side', 3, enc='F')]):
            out.append(('use_error_partial', {'entries': [
                {'parts': parts, 'unknown_on': [], 'names': ['a']}]}))
        for lower, part in (
                ([{'side': 'old', 'count': 1}, {'side': 'old', 'count': 1}],
                 lack('within', 2, 3)),
                ([{'side': 'old', 'count': 1}], lack('within', 2, 3)),
                ([{'side': 'both', 'count': 1}], lack('one side', 2)),
                ([{'side': 'old', 'count': 1}, {'side': 'new', 'count': 1}],
                 lack('one side', 2))):
            out.append(('use_wrong_result', {'entries': [
                {'parts': [part], 'lower': lower, 'names': ['a']}]}))
        out.append(('use_back_changed', {'entries': [
            {'encoding': 'E', 'count': 1, 'names': ['a']},
            {'encoding': 'F', 'count': 1, 'names': ['a']}]}))
        return tuple(out)

    def ctype_impossible(self):
        """The lines on the character rules and on a database of another
        encoding: more characters the old machine classified than change,
        one twice; names that are not as many as the locales counted, or
        one twice; a byte or a byte sequence twice, or a sequence both
        machines convert alike; a locale given a side twice, or both and
        one; a locale twice; on another encoding, a locale twice, one of
        the same sort table not among them, what only a machine that told
        says where none did, an encoding twice among the errors and those
        that convert every byte, one that converts otherwise not among
        them, what two machines refuse told apart against their counts,
        and a locale not known that is not among them."""
        cl = {'character': 'U+0041', 'change': 'x'}
        seq = {'bytes': 'A1', 'old': 'U+0041', 'new': 'U+0042'}
        g = [{'encoding': 'E', 'names': ['a']}]
        two = [{'encoding': 'E', 'names': ['a']},
               {'encoding': 'F', 'names': ['a']}]
        other = {'groups': [{'encoding': 'E', 'names': ['a', 'b']}],
                 'same': [], 'told': True, 'unknown': [], 'errors': [],
                 'whole': [], 'other': []}
        lack = {'encoding': 'E', 'alike': 'same', 'sides': [
            {'side': 'old', 'count': 1}, {'side': 'new', 'count': 1}]}
        return (
            ('ctype_characters_changed', {'locales': 1, 'names': [],
                                          'count': 1, 'classified': [cl, cl]}),
            ('ctype_characters_changed', {'locales': 2, 'names': ['a', 'b'],
                                          'count': 2, 'classified': [cl, cl]}),
            ('ctype_characters_changed', {'locales': 2, 'names': ['a'],
                                          'count': 1, 'classified': []}),
            ('ctype_characters_changed', {'locales': 2, 'names': ['a', 'a'],
                                          'count': 1, 'classified': []}),
            ('ctype_bytes_changed', {'locales': 1, 'names': [], 'bytes': [
                {'byte': 'E0', 'change': 'x'}, {'byte': 'E0', 'change': 'y'}]}),
            ('ctype_not_checked', {'locales': [
                {'name': 'a', 'side': 'old', 'why': 'w'},
                {'name': 'a', 'side': 'old', 'why': 'v'}]}),
            ('ctype_not_checked', {'locales': [
                {'name': 'a', 'side': 'both', 'why': 'w'},
                {'name': 'a', 'side': 'new', 'why': 'v'}]}),
            ('euc_sequences_changed', {'groups': g, 'sequences': [seq, seq]}),
            ('euc_sequences_changed', {'groups': g, 'sequences': [
                dict(seq, new='U+0041')]}),
            ('euc_sequences_changed', {'groups': two, 'sequences': [seq]}),
            ('euc_characters_changed', {'groups': g, 'count': 1,
                                        'classified': [cl, cl]}),
            ('euc_same', {'other': False, 'groups': two}),
            ('euc_ascii_same', {'side': 'old', 'groups': two}),
            ('euc_not_checked', {'reasons': [
                {'why': 'w', 'side': 'old', 'groups': g},
                {'why': 'v', 'side': 'old', 'groups': g}]}),
            ('utf8_changed', {'locales': 1, 'sequences': [seq, seq]}),
            ('utf8_changed', {'locales': 1, 'sequences': [
                dict(seq, new='U+0041')]}),
            ('utf8_one_side', {'side': 'old', 'why': [], 'locales': 1,
                               'sequences': [seq, seq]}),
            ('other_encoding', dict(other, groups=two)),
            ('other_encoding', dict(other, same=['c'])),
            ('other_encoding', dict(other, told=False, unknown=[
                {'side': 'old', 'groups': g}])),
            ('other_encoding', dict(other, errors=[lack], whole=[
                {'side': 'both', 'encodings': ['E']}])),
            ('other_encoding', dict(other, other=[
                {'encoding': 'E', 'count': 1}])),
            ('other_encoding', dict(other, errors=[
                dict(lack, alike='within')])),
            ('other_encoding', dict(other, unknown=[
                {'side': 'old', 'groups': [{'encoding': 'E',
                                            'names': ['c']}]}])),
        )

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

    def test_step11_with_the_machines(self):
        """Step 11's file, read as steps 0 to 2's are once step 11 ran:
        none is not reported, one holds its closing, and each `!!` it
        holds comes with its list and title; without step 11 it is not
        read."""
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        over = dict(ON_MACHINES)
        self.assertEqual(self.written(**over)['warnings']['step11'], {
            'state': 'not reported',
            'why': f'the step wrote no step11.{PAIR}.warnings.jsonl'})
        warnings_file(self.d, 11, ['closing'])
        self.assertEqual([i['kind'] for i in self.written(
            **over)['warnings']['step11']['items']], ['closing'])
        kinds = ['same_build', 'other_encoding', 'closing']
        warnings_file(self.d, 11, kinds)
        items = self.written(**over)['warnings']['step11']['items']
        self.assertEqual([(i['kind'], i['class'], i['title']) for i in items],
                         [(k, rd.KINDS[k][0], SAMPLES[k][1]) for k in kinds])

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
                           ('step2_rules_characters', 'rules_characters'),
                           ('step2_rules', 'rules')):
            with self.subTest(name=name):
                step2_files(self.d)
                ext = ('.json' if name.endswith(('by_name', 'rules')) else
                       '.tsv' if name.endswith('characters') else '.txt')
                os.remove(os.path.join(self.d, f'{name}.{PAIR}{ext}'))
                got = self.written()['step2'][part]
                self.assertEqual(got['state'], 'not reported')
                self.assertIn(name, got['why'])

    def test_each_step11_file(self):
        for name, ext, part in (
                ('measured_order_summary', '.txt', None),
                ('measured_order_verdicts', '.json', None),
                ('measured_order_characters', '.tsv', 'characters'),
                ('step11', '.data.json', ('counts', 'findings', 'checks'))):
            with self.subTest(name=name):
                step2_files(self.d, **MACHINES)
                step11_files(self.d)
                os.remove(os.path.join(self.d, f'{name}.{PAIR}{ext}'))
                s11 = self.written(
                    route='builds', old_node='/a', new_node='/b',
                    order_words='forward none build build builds',
                    step11=True)['step11']
                for got in ([s11[x] for x in part]
                            if isinstance(part, tuple)
                            else [s11[part] if part else s11]):
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
        for cut in ('# end: 2 locale group(s), 3 row(s)\n',
                    '# end: 2 locale group(s), 3 row(s)'):
            with self.subTest(cut=cut):
                step2_files(self.d, **MACHINES)
                step11_files(self.d)
                p = os.path.join(self.d,
                                 f'measured_order_characters.{PAIR}.tsv')
                text = open(p, encoding='utf-8').read()
                write(self.d, os.path.basename(p), text.replace(cut, ''))
                self.refused(**ON_MACHINES)
        for wrong in ('# end: 2 locale group(s), 4 row(s)\n',
                      '# end: 1 locale group(s), 3 row(s)\n'):
            with self.subTest(wrong=wrong):
                step2_files(self.d, **MACHINES)
                step11_files(self.d)
                p = os.path.join(self.d,
                                 f'measured_order_characters.{PAIR}.tsv')
                text = open(p, encoding='utf-8').read()
                write(self.d, os.path.basename(p), text.replace(
                    '# end: 2 locale group(s), 3 row(s)\n', wrong))
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
        warnings_file(self.d, 11, ['same_build', 'other_encoding',
                                   'closing'])
        self.good = self.written(**ON_MACHINES)

    def refuses(self, change):
        r = copy.deepcopy(self.good)
        change(r)
        p = os.path.join(self.d, 'changed.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        with self.assertRaises(rd.ReportError):
            rd.load(p)

    def test_findings_that_do_not_hang_together(self):
        """Step 11's findings, each written wrong alone, refused for the
        reason named: a count that is not its characters and other
        strings, or not what its runs or pairs hold; a run backwards, out
        of order or naming no string; a kind unknown, out of order, or
        where what moved says it cannot be; a pair in bytes the group
        says it names none of; probe pairs, levels left out and pairs told
        apart differently written wrong, each check and each half of a check of
        two parts with its own case; a change its sort table alone found
        with findings; a
        locale in two groups or in none, groups out of order, a heading
        skipped; and a value outside its list."""
        def group(r):
            return r['step11']['findings']['groups'][0]

        def found(r, kind):
            return next(x for x in group(r)['findings']
                        if x['kind'] == kind)

        def drop(r, kind):
            group(r)['findings'].remove(found(r, kind))

        def swap(r):
            f = group(r)['findings']
            f[1], f[2] = f[2], f[1]

        def two(r):
            g = copy.deepcopy(group(r))
            g['locales']['zz'] = g['locales']['sv_SE']
            r['step11']['findings']['groups'].append(g)
        def probe(r, side, i):
            return found(r, 'probes changed')[side][i]

        def probe_side(r, side):
            return found(r, 'probes changed')[side]

        def untested_bytes(r, k):
            # No pairs changed, the accents with no pair, and a byte in
            # the pair of level K of told apart (letters is tested).
            drop(r, 'probes changed')
            drop(r, 'left out')
            p = found(r, 'told apart')['probes']
            p[1] = {'level': 'accents', 'state': 'none'}
            p[k]['pair'] = ['U+0061', 'bytes FF']

        def chain(r, i):
            return found(r, 'told apart')['chains'][i]

        def no_bytes(r):
            found(r, 'undecided')['pairs'] = [['U+0061', 'U+0062']]
            group(r)['names_bytes'] = False
        cases = [
            ('a level missing', lambda r: found(r, 'probes changed')[
                'now'].pop(), 'is not three levels'),
            ('a level misnamed', lambda r: probe(r, 'now', 2).update(
                level='accents'), '.now[2].level is'),
            ('a probe state', lambda r: probe(r, 'now', 1).update(
                state='unknown'), '.now[1].state is'),
            ('a pair of one', lambda r: probe(r, 'before', 0).update(
                pair=['U+0061']), '.before[0].pair is'),
            ('from the end', lambda r: probe(r, 'before', 1).update(
                from_the_end='yes'), '.before[1].from_the_end is'),
            ('a pair where none is', lambda r: probe(r, 'now', 1).update(
                pair=['U+0061', 'U+0062']), 'has the keys'),
            ('a level unknown', lambda r: found(r, 'left out').update(
                left_out=['tone']), 'is not a list of levels'),
            ('levels out of order', lambda r: found(r, 'left out').update(
                compared=['case', 'letters']), 'is not a list of levels'),
            ('a level twice', lambda r: found(r, 'left out').update(
                compared=['letters', 'letters']), 'is not a list of levels'),
            ('nothing left out', lambda r: found(r, 'left out').update(
                left_out=[]), 'leaves out []'),
            ('left out and compared', lambda r: found(r, 'left out').update(
                left_out=['accents', 'case']),
             "leaves out ['accents', 'case']"),
            ('compared otherwise', lambda r: found(r, 'left out').update(
                compared=['letters']), 'levels compared are not those'),
            ('no pair told apart', lambda r: found(r, 'told apart').update(
                count=0), '.count is 0'),
            ('listed otherwise', lambda r: found(r, 'told apart').update(
                listed='runs'), '.listed is'),
            ('no chain', lambda r: found(r, 'told apart').update(chains=[]),
             '.chains is []'),
            ('a chain of one', lambda r: chain(r, 1).update(
                runs=[['U+00E4', 'U+00E4']]), 'is not a chain'),
            ('a state the levels cannot give', lambda r: chain(r, 0).update(
                now='accents'), '.chains[0].now is'),
            ('alike before and now', lambda r: chain(r, 1).update(
                now='not at all'), 'alike before and now'),
            ('a machine', lambda r: chain(r, 1).update(measured='either'),
             '.chains[1].measured is'),
            ('a test at a level not compared', lambda r: chain(r, 0)[
                'test'].update(level='accents'),
             '.chains[0].test.level is'),
            ('a test answered alike', lambda r: chain(r, 0)['test'].update(
                now='>'), 'answered alike'),
            ('a test with a pair', lambda r: chain(r, 0)['test'].update(
                pair=['U+0061', 'U+0062']), 'has the keys'),
            ('told apart with no probe pairs', lambda r: found(
                r, 'told apart').pop('probes'), 'has the keys'),
            ('a level compared with no pair', lambda r: (
                found(r, 'told apart')['probes'][2].update(state='none'),
                found(r, 'told apart')['probes'][2].pop('pair'),
                found(r, 'told apart')['probes'][2].pop('from_the_end')),
             'a level with no probe pair'),
            ('compared otherwise than the pairs', lambda r: (
                drop(r, 'probes changed'), drop(r, 'left out')),
             "its probe pairs ['letters', 'accents', 'case']"),
            ('other probe pairs', lambda r: found(r, 'told apart')[
                'probes'][0].update(pair=['U+0061', 'U+0063']),
             'other probe pairs'),
            ('pairs changed and nothing left out', lambda r: (
                drop(r, 'left out'),
                [(x[1].update(state='none'), x[1].pop('pair'),
                  x[1].pop('from_the_end'))
                 for x in (found(r, 'told apart')['probes'],
                           found(r, 'probes changed')['before'])]),
             'no level is left out'),
            ('changed pairs compared', lambda r: (
                probe(r, 'now', 0).update(pair=['U+0061', 'U+0063']),
                found(r, 'left out').update(left_out=['accents']),
                found(r, 'told apart')['probes'][0].update(
                    pair=['U+0061', 'U+0062'])),
             'letters is compared with pairs that changed'),
            ('a level compared the pairs differ on', lambda r: (
                probe(r, 'now', 2).update(from_the_end=True)),
             'case is compared with pairs that changed'),
            ('a level no machine measured, not left out', lambda r: (
                drop(r, 'told apart'), drop(r, 'did not move'),
                probe_side(r, 'before').__setitem__(
                    1, {'level': 'accents', 'state': 'not measured'}),
                found(r, 'left out').update(compared=['letters'],
                                            left_out=['case'])),
             'accents is compared with pairs that changed'),
            ('a level compared with no pair on either machine',
             lambda r: (drop(r, 'told apart'), drop(r, 'did not move'),
                        [(x.update(state='none'), x.pop('pair'),
                          x.pop('from_the_end'))
                         for x in (probe(r, 'before', 2),
                                   probe(r, 'now', 2))]),
             'case is compared with no pair'),
            ('listed each beyond the limit', lambda r: found(
                r, 'told apart').update(count=rd.LIST_LIMIT + 1),
             'is listed \'each\' with 21 pairs'),
            ('too many within the limit', lambda r: found(
                r, 'told apart').update(listed='too many', pairs=[]),
             "is listed 'too many' with 2 pairs"),
            ('a test state', lambda r: chain(r, 1)['test'].update(
                state='maybe'), '.chains[1].test.state is'),
            ('pairs miscounted by the chains', lambda r: found(
                r, 'told apart').update(count=3), 'its chains hold 2'),
            ('a pair listed otherwise', lambda r: found(r, 'told apart')[
                'pairs'][1].update(now='letters'),
             'lists its pairs otherwise'),
            ('pairs not listed', lambda r: found(r, 'told apart').update(
                pairs=[]), 'lists its pairs otherwise'),
            ('did not move otherwise', lambda r: found(
                r, 'did not move').update(runs=[['U+00E1', 'U+00E1']]),
             'what did not move is not'),
            ('did not move missing', lambda r: drop(r, 'did not move'),
             'what did not move is not'),
            ('a probe in bytes and no note', lambda r: (
                no_bytes(r), probe(r, 'now', 1).update(
                    state='pair', pair=['U+0061', 'bytes FF'],
                    from_the_end=True)), 'says it names none'),
            ('a test pair in bytes and no note', lambda r: (
                no_bytes(r), untested_bytes(r, 0)), 'says it names none'),
            ('a probe of told apart in bytes and no note', lambda r: (
                no_bytes(r), [x[1].update(pair=['U+0061', 'bytes FF'])
                              for x in (found(r, 'told apart')['probes'],
                                        probe_side(r, 'before'))]),
             'says it names none'),
            ('a count', lambda r: found(r, 'moved').update(count=2),
             'are not its characters and other strings'),
            ('runs short', lambda r: found(r, 'moved').update(
                count=2, characters=2), 'its runs hold 1'),
            ('a run backwards', lambda r: found(r, 'reversed').update(
                runs=[['U+00E5', 'U+00E4']]), 'is not a run'),
            ('a run of two kinds', lambda r: found(r, 'reversed').update(
                runs=[['U+0041', 'bytes 80']]), 'is not a run'),
            ('a run of two lengths', lambda r: found(r, 'reversed').update(
                runs=[['bytes 80', 'bytes 80 81']]), 'is not a run'),
            ('a count below zero', lambda r: found(r, 'moved').update(
                characters=-1, other_strings=2),
             'are not its characters and other strings'),
            ('runs out of order', lambda r: found(r, 'did not move').update(
                count=2, characters=2,
                runs=[['U+00E4', 'U+00E4'], ['U+00E1', 'U+00E1']]),
             'is not after the run before it'),
            ('no string', lambda r: found(r, 'moved').update(
                runs=[['X+00E5', 'X+00E5']]), 'is not a string step 11'),
            ('an unknown kind', lambda r: found(r, 'reversed').update(
                kind='sorted'), 'none of'),
            ('kinds out of order', swap, 'not in the order step 11'),
            ('moved and nothing sorts differently',
             lambda r: drop(r, 'reversed'), 'with 1 strings moved'),
            ('did not move with no pairs', lambda r: drop(r, 'told apart'),
             'with 1 strings moved'),
            ('pairs miscounted', lambda r: found(r, 'undecided').update(
                count=2), 'lists otherwise'),
            ('a pair of one', lambda r: found(r, 'undecided').update(
                pairs=[['U+0061']]), 'pairs[0] is'),
            ('listed as none', lambda r: found(r, 'moved').update(
                listed='none'), "is listed 'none' with 1 strings"),
            ('bytes and no note', lambda r: group(r).update(
                names_bytes=False), 'says it names none'),
            ('a table change with findings', lambda r: group(r).update(
                found_by='its sort table alone', names_bytes=False),
             'its sort table alone'),
            ('a table change naming bytes', lambda r: group(r).update(
                found_by='its sort table alone', findings=[]),
             'its sort table alone'),
            ('in two groups', two, 'is in two groups'),
            ('in no group', lambda r: group(r).update(locales={
                'sv_FI': group(r)['locales']['sv_SE']}),
             'otherwise than the verdicts'),
            ('groups out of order', lambda r: r['step11']['findings'][
                'groups'].append(copy.deepcopy(group(r))),
             'is not after the group before it'),
            ('a heading skipped', lambda r: group(r).update(printed_under=1),
             'the headings are [1]'),
            ('a reach', lambda r: group(r)['locales']['sv_SE']['postgres']
             .update(reach='nowhere'), 'none of'),
            ('no codeset', lambda r: group(r)['locales']['sv_SE']['codeset']
             .update(old=''), "locales['sv_SE'] is"),
            ('the sort table', lambda r: group(r).update(sort_table={
                'state': 'reported', 'why': 'x'}), 'sort_table.state is'),
            ('a key less', lambda r: group(r).pop('names_bytes'),
             'has the keys'),
        ]
        # A byte in the pair of a level no chain is tested at, the pairs
        # unchanged, is named nowhere (as th_TH's accents pair in TIS-620
        # on RHEL 8 and 9, FF 5F): read.
        r = copy.deepcopy(self.good)
        no_bytes(r)
        untested_bytes(r, 2)
        p = os.path.join(self.d, 'untested.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        self.assertEqual(rd.load(p), r)
        # The string of bytes E5 moved, and U+00E5, the same number, did
        # not: spans of strings of two kinds are kept apart.
        r = copy.deepcopy(self.good)
        found(r, 'moved').update(characters=0, other_strings=1,
                                 runs=[['bytes E5', 'bytes E5']])
        found(r, 'did not move').update(count=2, characters=2,
                                        runs=[['U+00E4', 'U+00E5']])
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        self.assertEqual(rd.load(p), r)
        for name, change, msg in cases:
            with self.subTest(case=name):
                r = copy.deepcopy(self.good)
                change(r)
                p = os.path.join(self.d, 'changed.json')
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(r, f)
                with self.assertRaisesRegex(rd.ReportError, re.escape(msg)):
                    rd.load(p)

    def sort_table(self, r, table, rows=(), found_by='step 11'):
        """R with its one group's sort table TABLE, found by FOUND_BY,
        and ROWS of the characters file under the line of its table
        (where the table changed): (characters, kind) each."""
        g = r['step11']['findings']['groups'][0]
        g['sort_table'] = table
        if found_by != 'step 11':
            g.update(found_by=found_by, findings=[], names_bytes=False)
        old = r['step11']['characters']['rows']
        rest = [x for x in old if x[1] != 'sort table changed']
        head = []
        if isinstance(table, dict) and table.get('state') == 'changed':
            head = [['sv_SE', 'sort table changed', '-',
                     'its compiled sort table changed', '-', '-', '-', '-']]
            head += [['sv_SE', 'sort table changed', chars,
                      rd.NAMED_HOW[kind], '-', '-', '-', 'compiled table']
                     for chars, kind in rows]
        r['step11']['characters']['rows'] = head + rest
        return r

    def tables_warned(self, r, disagree=(), not_compared=(), more=0):
        """R with the tables' `!!` naming DISAGREE and NOT_COMPARED, each
        list cutting MORE names it does not print."""
        items = r['warnings']['step11']['items']
        p = os.path.join(self.d, 'warned.jsonl')
        if disagree:
            items.append(rd.warning_item(p, 'tables_disagree', {
                'names': list(disagree), 'more': more}, '!! x'))
        if not_compared:
            items.append(rd.warning_item(p, 'tables_not_compared', {
                'groups': [{'why': 'x', 'names': list(not_compared),
                            'more': more}]}, '!! x'))
        return r

    def test_sort_tables_that_do_not_hang_together(self):
        """A group's sort table (locale_order.table_data), its rows in the
        characters file and the tables' `!!`, each written wrong alone,
        refused for the reason named, each check and each half of a check
        of two parts with its own case; and each state the tool writes
        read, those no real run prints among them."""
        named = {'state': 'changed', 'step11': 'changed',
                 'reason': 'items differ',
                 'naming': {'state': 'named',
                            'counts': {'marked': 2, 'old': 0, 'new': 1,
                                       'flip': 1},
                            'more': 0, 'others': True}}
        rows = [('U+0061..U+0062', 'marked'), ('bytes 80', 'new'),
                ('U+0063', 'flip')]
        many = [(f'U+{0x100 + 2 * k:04X}', 'marked') for k in range(21)]

        def table(**over):
            t = copy.deepcopy(named)
            t.update({k: v for k, v in over.items() if k != 'naming'})
            if 'naming' in over:
                t['naming'] = over['naming']
            return t

        def naming(**over):
            return dict(copy.deepcopy(named['naming']), **over)

        def counts(**over):
            return naming(counts=dict(named['naming']['counts'], **over))

        bare = {'state': 'changed', 'step11': 'changed',
                'reason': 'no levels on one machine'}
        reads = [
            ('named', lambda r: self.sort_table(r, named, rows)),
            ('more than the report lists', lambda r: self.sort_table(
                r, table(naming=naming(counts={'marked': 21, 'old': 0,
                                               'new': 0, 'flip': 0},
                                       more=1, others=False)), many)),
            ('one code point twice', lambda r: self.sort_table(
                r, table(naming=counts(marked=3)),
                [('U+0061..U+0062', 'marked'), ('U+0062', 'marked')]
                + rows[1:])),
            ('no levels', lambda r: self.sort_table(r, bare)),
            ('no line', lambda r: self.tables_warned(self.sort_table(
                r, {'state': 'no line'}), not_compared=['sv_SE'])),
            ('no line, a list cut', lambda r: self.tables_warned(
                self.sort_table(r, {'state': 'no line'}), disagree=['xx'],
                more=1)),
            ('the record', lambda r: self.tables_warned(self.sort_table(
                r, table(naming={'state': 'record'})), disagree=['sv_SE'])),
            ('the numbering, step 11 the same', lambda r: self.sort_table(
                r, table(step11='same', naming={'state': 'numbering'}),
                found_by='its sort table alone')),
            ('unread, not measured', lambda r: self.sort_table(
                r, table(step11='unmeasured', naming={
                    'state': 'unread', 'reasons': ['on the old machine, x']}),
                found_by='its sort table alone')),
            ('levels, not measured', lambda r: self.sort_table(
                r, table(step11='unmeasured', naming={
                    'state': 'levels', 'old': 4, 'new': 3}),
                found_by='its sort table alone')),
            ('no levels, not measured', lambda r: self.sort_table(
                r, dict(bare, step11='unmeasured'),
                found_by='its sort table alone')),
        ]
        for name, change in reads:
            with self.subTest(reads=name):
                r = change(copy.deepcopy(self.good))
                if name.endswith(', not measured'):
                    r['step11']['checks']['charmaps']['checked'] = 0
                p = os.path.join(self.d, 'read.json')
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(r, f)
                self.assertEqual(rd.load(p), r)

        def wrong(t, rws=rows, found_by='step 11'):
            return lambda r: self.sort_table(r, t, rws, found_by)
        cases = [
            # _sort_table.
            ('not an object', wrong([]), 'sort_table is []'),
            ('a state', wrong({'state': 'same'}), '.sort_table.state is'),
            ('no line, a key more', wrong({'state': 'no line', 'x': 1}),
             'has the keys'),
            ('no line of the table alone', wrong(
                {'state': 'no line'}, found_by='its sort table alone'),
             'no line of a table that alone'),
            ('step 11 unknown', wrong(table(step11='moved')),
             '.sort_table.step11 is'),
            ('a reason', wrong(table(reason='other')),
             '.sort_table.reason is'),
            ('step 11 said the same of its own change', wrong(
                table(step11='same')), "said 'same' of a change found by "
             "step 11"),
            ('step 11 said changed of the table alone', wrong(
                table(), found_by='its sort table alone'),
             "said 'changed' of a change found by its sort table alone"),
            ('no levels, naming', wrong(dict(bare, naming=named['naming'])),
             'has the keys'),
            ('items differ, no naming', wrong(dict(
                bare, reason='items differ')), 'has the keys'),
            ('naming not an object', wrong(table(naming=[])),
             '.naming is []'),
            ('a naming state', wrong(table(naming={'state': 'some'})),
             '.naming.state is'),
            ('the record, a key more', wrong(
                table(naming={'state': 'record', 'more': 0}), ()),
             'has the keys'),
            ('no reason', wrong(table(naming={'state': 'unread',
                                              'reasons': []}), ()),
             '.reasons is []'),
            ('an empty reason', wrong(table(naming={'state': 'unread',
                                                    'reasons': ['']}), ()),
             ".reasons is ['']"),
            ('reasons not a list', wrong(table(naming={'state': 'unread',
                                                       'reasons': 'x'}), ()),
             ".reasons is 'x'"),
            ('the same levels', wrong(table(naming={
                'state': 'levels', 'old': 3, 'new': 3}), ()),
             'has the levels 3 and 3'),
            ('no levels counted', wrong(table(naming={
                'state': 'levels', 'old': 0, 'new': 3}), ()),
             'has the levels 0 and 3'),
            ('a count missing', wrong(table(naming=naming(counts={
                'marked': 2, 'old': 0, 'new': 1}))), 'has the keys'),
            ('a count not a number', wrong(table(naming=counts(new='1'))),
             '.naming is'),
            ('more not a number', wrong(table(naming=naming(more=-1))),
             '.naming is'),
            ('others not a bool', wrong(table(naming=naming(others=1))),
             '.naming is'),
            ('nothing named but flips', wrong(table(naming=counts(
                marked=0, new=0, flip=1)), [('U+0063', 'flip')]),
             'names no sequence marked or on one side only'),
            ('flips beside none marked', wrong(table(naming=counts(
                marked=0)), rows[1:]), 'flip(s) beside none marked'),
            # _table_rows_held.
            ('no rows', lambda r: self.sort_table(r, named, rows).__setitem__(
                'step11', dict(r['step11'], characters=dict(
                    r['step11']['characters'], rows=[
                        x for x in r['step11']['characters']['rows']
                        if x[1] != 'sort table changed']))),
             'the sort table rows are of []'),
            ('rows of no changed table', lambda r: self.tables_warned(
                self.sort_table(r, named, rows), not_compared=['sv_SE'])
             ['step11']['findings']['groups'][0].update(
                 sort_table={'state': 'no line'}),
             "the sort table rows are of ['sv_SE'], the changed sort "
             "tables of []"),
            ('another label', lambda r: [x.__setitem__(0, 'sv_SE,sv_FI')
                                         for x in self.sort_table(
                                             r, named, rows)['step11'][
                                             'characters']['rows']
                                         if x[1] == 'sort table changed'],
             "the sort table rows are of ['sv_SE,sv_FI']"),
            ('two runs of one label', lambda r: self.sort_table(
                r, named, rows)['step11']['characters']['rows'].insert(
                    2, ['sv_SE', 'moved', 'U+00E5', '-', 'x', '>', '<',
                        'on both machines']),
             "the sort table rows are of ['sv_SE', 'sv_SE']"),
            ('a line with characters', lambda r: self.sort_table(
                r, named, rows)['step11']['characters']['rows'][0]
             .__setitem__(2, 'U+0061'), 'do not open with its line'),
            ('a line with no words', lambda r: self.sort_table(
                r, named, rows)['step11']['characters']['rows'][0]
             .__setitem__(3, '-'), 'do not open with its line'),
            ('a line with a test', lambda r: self.sort_table(
                r, named, rows)['step11']['characters']['rows'][0]
             .__setitem__(4, 'x'), 'do not open with its line'),
            ('names where it names none', wrong(bare, rows[:1]),
             'name 1 sequence(s), its table none'),
            ('names beside the record', lambda r: self.tables_warned(
                self.sort_table(r, table(naming={'state': 'record'}),
                                rows[:1]), disagree=['sv_SE']),
             'name 1 sequence(s), its table none'),
            ('a how unknown', lambda r: self.sort_table(
                r, named, rows)['step11']['characters']['rows'][1]
             .__setitem__(3, 'moved'), "'U+0061..U+0062', 'moved'"),
            ('not measured by the table', lambda r: self.sort_table(
                r, named, rows)['step11']['characters']['rows'][1]
             .__setitem__(7, 'on both machines'), "'on both machines']"),
            ('a kind in two runs', wrong(table(naming=counts(marked=3)), [
                ('U+0061..U+0062', 'marked'), ('bytes 80', 'new'),
                ('U+0064', 'marked'), ('U+0063', 'flip')]),
             'the marked names are not in one run'),
            ('kinds out of order', wrong(named, [rows[1], rows[0], rows[2]]),
             "the kinds are in the order ['new', 'marked', 'flip']"),
            ('a name before the one before it', wrong(named, [
                ('U+0063..U+0064', 'marked'), ('U+0061', 'marked')]
                + rows[1:]), "'U+0061' is not after the name before it"),
            ('bytes before a character', wrong(table(naming=counts(new=2)), [
                rows[0], ('bytes 80', 'new'), ('U+0041', 'new'), rows[2]]),
             "'U+0041' is not after"),
            ('a count otherwise', wrong(table(naming=counts(marked=3))),
             "the data counts {'marked': 3"),
            ('a flip counted otherwise', wrong(table(naming=counts(flip=2))),
             "the data counts {'marked': 2, 'old': 0, 'new': 1, 'flip': 2}"),
            ('more otherwise', wrong(table(naming=naming(more=1))),
             'and the data leaves 1 out of the report'),
            ('more left out', wrong(table(naming=naming(counts={
                'marked': 21, 'old': 0, 'new': 0, 'flip': 0}, others=False)),
                many), 'and the data leaves 0 out of the report'),
            ('bytes not said', wrong(table(naming=naming(others=False))),
             'a name in bytes is True, the data says False'),
            ('bytes said of none, counted', wrong(table(naming=naming(
                counts={'marked': 2, 'old': 0, 'new': 0, 'flip': 2})),
                [rows[0], ('U+0063..U+0064', 'flip')]),
             'a name in bytes is False, the data says True'),
            ('a range backwards', wrong(table(naming=counts(new=2)), [
                rows[0], ('bytes 81..80', 'new'), rows[2]]),
             "'bytes 81..80' is not a range of strings of one length"),
            ('a range of two lengths', wrong(table(naming=counts(new=2)), [
                rows[0], ('bytes 80..80 00', 'new'), rows[2]]),
             'is not a range of strings of one length'),
            ('a name of nothing', wrong(named, [rows[0], ('80', 'new'),
                                                rows[2]]),
             "'80' is not a name of a sequence"),
            # _tables_held.
            ('no line in no list', wrong({'state': 'no line'}),
             "'sv_SE' is said otherwise by the tables' warnings"),
            ('no line, another not compared', lambda r: self.tables_warned(
                self.sort_table(r, {'state': 'no line'}),
                not_compared=['xx']), "'sv_SE' is said otherwise"),
            ('no line, another in disagree', lambda r: self.tables_warned(
                self.sort_table(r, {'state': 'no line'}), disagree=['xx']),
             "'sv_SE' is said otherwise"),
            ('the record, another in disagree', lambda r: self.tables_warned(
                self.sort_table(r, table(naming={'state': 'record'}), ()),
                disagree=['xx']), "'sv_SE' is said otherwise"),
            ('the record, the other list cut', lambda r: self.tables_warned(
                self.sort_table(r, table(naming={'state': 'record'}), ()),
                not_compared=['xx'], more=1), "'sv_SE' is said otherwise"),
            ('the record not in disagree', wrong(
                table(naming={'state': 'record'}), ()),
             "'sv_SE' is said otherwise"),
            ('the numbering not compared too', lambda r: self.tables_warned(
                self.sort_table(r, table(naming={'state': 'numbering'})),
                disagree=['sv_SE'], not_compared=['sv_SE']),
             "'sv_SE' is said otherwise"),
            ('a change named and in disagree', lambda r: self.tables_warned(
                self.sort_table(r, named, rows), disagree=['sv_SE']),
             "'sv_SE' is said otherwise"),
            ('a change named and not compared', lambda r: self.tables_warned(
                self.sort_table(r, named, rows), not_compared=['sv_SE']),
             "'sv_SE' is said otherwise"),
            ('the record of the table alone in disagree',
             lambda r: self.tables_warned(self.sort_table(
                 r, table(step11='same', naming={'state': 'record'}),
                 found_by='its sort table alone'), disagree=['sv_SE']),
             "'sv_SE' is said otherwise"),
        ]
        for name, change, msg in cases:
            with self.subTest(case=name):
                r = copy.deepcopy(self.good)
                change(r)
                p = os.path.join(self.d, 'changed.json')
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(r, f)
                with self.assertRaisesRegex(rd.ReportError, re.escape(msg)):
                    rd.load(p)

    def test_verdicts_closing_and_character_sets_that_do_not_hang_together(
            self):
        """Step 11's verdicts' words, its closing, the locales its tables
        alone decided and its line on the character sets, each written
        wrong alone in the reader's example, refused for the reason named:
        what load() runs each check on."""
        cm = lambda r: r['step11']['checks']['charmaps']  # noqa: E731

        def closing(state):
            def change(r):
                for i in r['warnings']['step11']['items']:
                    if i['kind'] == 'closing':
                        i['data']['state'] = state
                        i['title'] = rd.CLOSING_TITLES[state]
            return change

        def refused_bang(r):
            r['warnings']['step11']['items'].append(rd.warning_item(
                os.path.join(self.d, 'w.jsonl'), 'glibc_refused', {
                    'locales': [{'name': 'sv_SE', 'side': 'old',
                                 'categories': ['LC_COLLATE']}]}, '!! x'))
        cases = [
            ('a state', lambda r: cm(r).update(state='x'), '.state is'),
            ('a key less', lambda r: cm(r).pop('by_table'), 'has the keys'),
            ('a count', lambda r: cm(r).update(checked=-1), 'charmaps is {'),
            ('C and POSIX out of order', lambda r: cm(r).update(
                builtin=['POSIX', 'C']), 'charmaps is {'),
            ('not needed with no table', lambda r: cm(r).update(
                state='not needed', by_table=0) or cm(r).pop('checked'),
             'not needed with no locale its sort table decided'),
            ('a count the verdicts do not give', lambda r: cm(r).update(
                checked=2), 'the verdicts and groups of the same run say'),
            ('not checked beside no `!!`', lambda r: r['step11'][
                'checks'].update(charmaps={'state': 'not checked'}),
             "is 'not checked' beside 0 `!!`"),
            ('the closing', closing('plain'),
             "step 11 closes ['plain'], its verdicts 'tables'"),
            ('words of another verdict', lambda r: r['step11']['verdicts'][
                'sv_SE'].update(words='built into glibc'),
             "['sv_SE'] is 'changed' in the words"),
            ('C firm by a table', lambda r: r['step11']['verdicts'][
                'C'].update(words=rd.VERDICT_WORDS['firm', 'sort table']),
             "['C'] is firm 'sort table'"),
            ('refused by a `!!` and not by its verdict', refused_bang,
             "refuse {}, the `!!` of glibc refusing {'sv_SE': 'old'}"),
            ('decided by its table alone and changed', lambda r: r[
                'step11']['findings'].update(tables_only=['sv_SE']),
             "tables_only names ['sv_SE']"),
            ('tables alone, a list', lambda r: r['step11'][
                'findings'].update(tables_only='C'), 'tables_only is'),
            ('tables alone, a name twice', lambda r: r['step11'][
                'findings'].update(tables_only=['C', 'C']),
             'tables_only is'),
        ]
        for name, change, msg in cases:
            with self.subTest(case=name):
                r = copy.deepcopy(self.good)
                change(r)
                p = os.path.join(self.d, 'changed.json')
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(r, f)
                with self.assertRaisesRegex(rd.ReportError, re.escape(msg)):
                    rd.load(p)

    def test_each_tie_of_the_closing_and_the_character_sets(self):
        """Each check of _verdict_basis, _verdicts_held, _closing_held,
        _tables_only_held and _charmaps_held, and each half of one of two
        parts, with a case only it refuses, on the verdicts and `!!` the
        tool writes; and the states no run of the suite reaches read: C
        or POSIX alone aside, a locale of no levels step 11 could not
        measure, C step 11 could not measure, two machines refusing a
        locale for different categories."""
        W = rd.VERDICT_WORDS

        def basis(**v):
            return rd._verdict_basis('v', {
                n: {'verdict': a, 'words': W[a, b]}
                for n, (a, b) in v.items()})

        def warned(*items):
            return {'state': 'reported', 'items': [
                {'kind': k, 'data': d} for k, d in items]}

        def control(old='byte order', new='byte order', name='C'):
            out = {s: {n: {'state': 'byte order'} for n in rd.BUILTIN}
                   for s in ('old', 'new')}
            out['old'][name]['state'], out['new'][name]['state'] = old, new
            return out
        unseen = [{'locales': {'a': {}}, 'sort_table': {
            'state': 'changed', 'step11': 'unmeasured'}}]
        firm = basis(C=('firm', 'built in'), a=('firm', 'sort table'))
        checked = {'state': 'checked', 'checked': 1, 'by_table': 0,
                   'builtin': []}
        cases = [
            # (case, call, the refusal it raises, or None where it reads)
            ('words of no verdict', lambda: rd._verdict_basis('v', {
                'a': {'verdict': 'firm', 'words': 'unchanged'}}),
             "['a'] is 'firm' in the words 'unchanged'"),
            ('words of another verdict', lambda: rd._verdict_basis('v', {
                'a': {'verdict': 'far', 'words': W['changed', '-']}}),
             "['a'] is 'far' in the words"),
            ('C not built in', lambda: basis(C=('firm', 'no levels')),
             "['C'] is firm 'no levels'"),
            ('built in, not C', lambda: basis(a=('firm', 'built in')),
             "['a'] is firm 'built in'"),
            ('no levels step 11 could not measure', lambda: basis(
                a=('firm', 'no levels, not measured')), None),
            ('refused on the old machine, the `!!` the new', lambda:
             rd._verdicts_held('v', {'a': {'verdict': 'refused',
                                          'words': W['refused', 'old']}},
                               warned(('glibc_refused', {'locales': [
                                   {'name': 'a', 'side': 'new'}]}))),
             "refuse {'a': 'old'}, the `!!` of glibc refusing {'a': 'new'}"),
            ('refused with no `!!`', lambda: rd._verdicts_held(
                'v', {'a': {'verdict': 'refused',
                            'words': W['refused', 'both']}}, warned()),
             "refuse {'a': 'both'}, the `!!` of glibc refusing {}"),
            ('refused on both, for different categories', lambda:
             rd._verdicts_held('v', {'a': {'verdict': 'refused',
                                          'words': W['refused', 'both']}},
                               warned(('glibc_refused', {'locales': [
                                   {'name': 'a', 'side': 'old'},
                                   {'name': 'a', 'side': 'new'}]}))), None),
            ('tables, a locale only as far as measured', lambda:
             rd._closing_held('w', basis(a=('firm', 'sort table'),
                                         b=('far', '-')),
                              warned(('closing', {'state': 'tables'}))),
             "closes ['tables'], its verdicts 'mixed'"),
            ('mixed, every one firm', lambda: rd._closing_held(
                'w', firm, warned(('closing', {'state': 'mixed'}))),
             "closes ['mixed'], its verdicts 'tables'"),
            ('mixed, none firm by its table', lambda: rd._closing_held(
                'w', basis(a=('firm', 'no levels'), b=('far', '-')),
                warned(('closing', {'state': 'mixed'}))),
             "closes ['mixed'], its verdicts 'plain'"),
            ('plain, one firm by its table', lambda: rd._closing_held(
                'w', basis(a=('firm', 'sort table, not measured'),
                           b=('far', '-')),
                warned(('closing', {'state': 'plain'}))),
             "closes ['plain'], its verdicts 'mixed'"),
            ('no locale unchanged', lambda: rd._closing_held(
                'w', basis(a=('changed', '-')),
                warned(('closing', {'state': 'plain'}))), None),
            ('two closings', lambda: rd._closing_held(
                'w', firm, warned(('closing', {'state': 'tables'}),
                                  ('closing', {'state': 'tables'}))),
             "closes ['tables', 'tables']"),
            # A locale of no levels step 11 found changed is in the tables'
            # `!!` of disagreeing, and one with an identical table can stay
            # firm beside it (table_verdicts): read.
            ('firm by its table, the tables disagreeing', lambda:
             rd._closing_held('w', basis(a=('firm', 'sort table'),
                                         b=('changed', '-')), warned(
                ('tables_disagree', {'names': ['b'], 'more': 0}),
                ('closing', {'state': 'tables'}))), None),
            ('tables alone, one not firm', lambda: rd._tables_only_held(
                't', ['b'], basis(b=('far', '-')), control()),
             "names ['b']"),
            ('tables alone, C not firm', lambda: rd._tables_only_held(
                't', ['C'], basis(C=('changed', '-')),
                control(old='not measured')), "names ['C']"),
            ('tables alone, its words measured', lambda:
             rd._tables_only_held('t', ['a'], firm, control()),
             "names ['a'], the verdicts say step 11 could not measure []"),
            ('tables alone, words not measured, not named', lambda:
             rd._tables_only_held('t', [], basis(
                 a=('firm', 'sort table, not measured')), control()),
             "could not measure ['a']"),
            ('C not measured on a machine, not named', lambda:
             rd._tables_only_held('t', [], firm, control(
                 new='not measured')),
             'leaves out C, which a machine could not measure'),
            ('C named, byte order on both', lambda: rd._tables_only_held(
                't', ['C'], firm, control()),
             'names C, measured as byte order on both machines'),
            ('C named, not measured on a machine', lambda:
             rd._tables_only_held('t', ['C'], firm, control(
                 old='not measured')), None),
            ('the line, not an object', lambda: rd._charmaps_shape(
                'c', ['checked']), "c is ['checked']"),
            ('the line, a count not a number', lambda: rd._charmaps_shape(
                'c', dict(checked, checked=True)), 'c is {'),
            ('the line, C and POSIX out of order', lambda:
             rd._charmaps_shape('c', dict(checked, builtin=['POSIX', 'C'])),
             'c is {'),
            ('the line, POSIX alone', lambda: rd._charmaps_shape(
                'c', dict(checked, builtin=['POSIX'])), None),
            ('checked beside a `!!`', lambda: rd._charmaps_held(
                'c', checked, [], basis(a=('changed', '-')),
                warned(('measured_in_part', {}))),
             "is 'checked' beside 1 `!!`"),
            ('not needed, a `!!` printed instead', lambda:
             rd._charmaps_held('c', {'state': 'not checked'}, [], firm,
                               warned(('charmaps_machine_not_checked', {
                                   'named_by_bytes': False}))),
             'is not checked where it was not needed'),
            ('a `!!` naming by bytes', lambda: rd._charmaps_held(
                'c', {'state': 'not checked'}, [], firm,
                warned(('charmaps_not_checked', {'named_by_bytes': True}))),
             None),
            ('a changed one step 11 did not measure, checked', lambda:
             rd._charmaps_held('c', dict(checked, checked=2), unseen,
                               basis(a=('changed', '-'), b=('far', '-')),
                               warned()),
             "'checked': 1"),
            ('one only as far as measured, not checked', lambda:
             rd._charmaps_held('c', dict(checked, checked=0), [],
                               basis(b=('far', '-')), warned()),
             "'checked': 1"),
            ('by its table, not counted', lambda: rd._charmaps_held(
                'c', checked, [], basis(a=('changed', '-'),
                                        b=('firm', 'sort table')),
                warned()), "'by_table': 1"),
            ('C firm, not aside', lambda: rd._charmaps_held(
                'c', checked, [], basis(a=('changed', '-'),
                                        C=('firm', 'built in')),
                warned()), "'builtin': ['C']"),
            ('C checked, not aside', lambda: rd._charmaps_held(
                'c', dict(checked, checked=0), [],
                basis(C=('changed', '-')), warned()), "'builtin': ['C']"),
            ('checked, nothing to check', lambda: rd._charmaps_held(
                'c', dict(checked, checked=0, by_table=1), [], firm,
                warned()), "'state': 'not needed'"),
            ('not needed, one to check', lambda: rd._charmaps_held(
                'c', {'state': 'not needed', 'by_table': 1, 'builtin': []},
                [], basis(a=('firm', 'sort table'), b=('changed', '-')),
                warned()), "'state': 'checked'"),
            ('C alone aside', lambda: rd._charmaps_held(
                'c', dict(checked, builtin=['C']), [],
                basis(C=('firm', 'built in'), a=('changed', '-')),
                warned()), None),
            ('POSIX alone aside, not needed', lambda: rd._charmaps_held(
                'c', {'state': 'not needed', 'by_table': 1,
                      'builtin': ['POSIX']}, [],
                basis(POSIX=('firm', 'built in'), a=('firm', 'sort table')),
                warned()), None),
            ('no `!!` read', lambda: rd._charmaps_held(
                'c', {'state': 'not checked'}, [], firm,
                {'state': 'not reported', 'why': 'x'}), None),
        ]
        for name, call, msg in cases:
            with self.subTest(case=name):
                if msg is None:
                    call()
                    continue
                with self.assertRaisesRegex(rd.ReportError, re.escape(msg)):
                    call()

    def test_the_closing_and_character_set_constants_are_locale_orders(
            self):
        """The states of the line on the character sets, the verdicts'
        words and the closings that the reader allows are those
        locale_order writes by, and C and
        POSIX are node_charmaps' built-in locales; locale_order refuses
        to print outside them: one it added and the reader did not would
        refuse a real run."""
        import locale_order as lo
        import node_charmaps as nc
        for name in ('CHARMAPS_LINE', 'VERDICT_WORDS'):
            with self.subTest(name=name):
                self.assertEqual(getattr(rd, name), getattr(lo, name))
        self.assertEqual(rd.BUILTIN, tuple(nc.BUILTIN))
        self.assertEqual(set(rd.CLOSING_TITLES), set(lo.CLOSINGS))
        for d in ({'state': 'not checked'}, {'state': 'x'},
                  {'state': 'checked', 'checked': 1, 'by_table': 0,
                   'builtin': ['POSIX', 'C']}):
            with self.subTest(charmaps=d), self.assertRaises(ValueError):
                lo.charmaps_text(d)
        for args in (('firm',), ('firm', 'x'), ('refused', 'either')):
            with self.subTest(words=args), self.assertRaises(ValueError):
                lo.verdict_words(*args)
        with self.assertRaises(ValueError):
            lo.run_findings({}, {}, [], [], [], [], closing='x')

    def test_the_sort_table_constants_are_locale_orders(self):
        """What step 11 said, the reasons, the states of what a table
        names, the words of each kind of name and the escapes of a label
        the reader allows are those locale_order writes by; and
        locale_order refuses to write a sort table outside them: one it
        added and the reader did not would refuse a real run."""
        import locale_order as lo
        for name in ('TABLE_STEP11', 'TABLE_REASONS', 'TABLE_NAMED',
                     'NAMED_HOW', 'TSV_ESCAPES'):
            with self.subTest(name=name):
                self.assertEqual(getattr(rd, name), getattr(lo, name))
        for args in (({'state': 'same'}, ()),
                     ({'state': 'named', 'counts': {}}, ()),
                     ({'state': 'record'}, (('marked', 'a', 'U+0061'),))):
            with self.subTest(named=args), self.assertRaises(ValueError):
                lo.table_named(*args)
        record = lo.table_named({'state': 'record'})
        for args in ((None, 'moved', record), (lo.NO_LEVELS_ONE, 'same',
                                                record),
                     (None, 'same', None), ('other', 'same', None)):
            with self.subTest(data=args[:2]), self.assertRaises(ValueError):
                lo.table_data(*args)
        self.assertEqual(lo.table_data(None, 'same', record),
                         {'state': 'changed', 'step11': 'same',
                          'reason': 'items differ',
                          'naming': {'state': 'record'}})

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
                     ('step2', 'rules')):
            with self.subTest(path=path):
                def change(r):
                    x = r
                    for k in path:
                        x = x[k]
                    x['state'] = 'clean'
                self.refuses(change)

    def test_counts_that_do_not_add_up(self):
        """Step 11's counts, each written wrong alone: every count and
        each machine's number of locales, by one more, against the sums
        of its line of totals and the verdicts beside them; a count that
        is not a count; a machine with no name; old and new swapped
        where they differ."""
        for k in rd.COUNT_KEYS:
            with self.subTest(count=k):
                self.refuses(lambda r: r['step11']['counts'].update(
                    {k: r['step11']['counts'][k] + 1}))
        for side in ('old', 'new'):
            with self.subTest(side=side):
                self.refuses(lambda r: r['step11']['counts']['machines']
                             [side].update(locales=3))
                self.refuses(lambda r: r['step11']['counts']['machines']
                             [side].update(label=''))
        # Both machines one more: the new machine's count still follows
        # from the old one's, and the verdicts are kept; only the old
        # machine's sum is wrong.
        self.refuses(lambda r: [r['step11']['counts']['machines'][s].update(
            locales=3) for s in ('old', 'new')])
        self.refuses(lambda r: r['step11']['counts'].update(changed=True))
        self.refuses(lambda r: r['step11']['counts'].update(changed=-1))

        def right(r):
            # One locale only on the new machine, with step 2's rules
            # for it: the new machine holds three.
            c = r['step11']['counts']
            r['step11']['verdicts']['de_DE'] = {
                'verdict': 'only new', 'words': 'only on the new machine'}
            by_name = r['step2']['rules_by_name']['locales']['by_name']
            by_name['de_DE'] = dict(by_name['sv_SE'])
            c['only_new'] = 1
            c['machines']['new']['locales'] = 3

        def swapped(r):
            # The same, the two machines swapped.
            right(r)
            m = r['step11']['counts']['machines']
            m['old'], m['new'] = m['new'], m['old']
        self.refuses(swapped)
        r = copy.deepcopy(self.good)
        right(r)
        p = os.path.join(self.d, 'right.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        self.assertEqual(rd.load(p), r)

    def test_the_counts_file_is_read_whole(self):
        """A data file step 11 did not write as it writes it, or one an
        audit.sh run cannot have left (SQL files not asked for, or not
        the folder's files in the folder's order): refused, never read
        as counts, findings, checks or SQL files."""
        whole = {'step': 11, 'counts': COUNTS, 'findings': FINDINGS,
                 'checks': CHECKS, 'sql': SQL}
        for text in ('{"step": 11}\n', '{"step": 2, "counts": {}}\n',
                     '{"step": 11, "counts": {"state": "reported"}}\n',
                     '{"step": 11, "counts": []}\n', '{"step": 11, "co',
                     json.dumps({'step': 11, 'counts': COUNTS}),
                     json.dumps({**whole, 'findings': []}),
                     json.dumps({**whole, 'findings': NOT_YET}),
                     json.dumps({k: v for k, v in whole.items()
                                 if k != 'checks'}),
                     json.dumps({**whole, 'checks': []}),
                     json.dumps({**whole, 'checks': {'state': 'reported',
                                                     **CHECKS}}),
                     json.dumps({**whole, 'more': 1}),
                     json.dumps({k: v for k, v in whole.items()
                                 if k != 'sql'}),
                     json.dumps({**whole, 'sql': []}),
                     # audit.sh always asks for the SQL files.
                     json.dumps({**whole, 'sql': {'state': 'not asked'}}),
                     # Files printed that the folder does not hold, held
                     # and not printed, or in another order.
                     json.dumps({**whole, 'sql': dict(
                         SQL, files=SQL['files'][1:])}),
                     json.dumps({**whole, 'sql': dict(
                         SQL, files=SQL['files'] + [
                             {'encoding': 'EUC_JP', 'tests': 1}])}),
                     json.dumps({**whole, 'sql': dict(
                         SQL, files=SQL['files'][::-1])}),
                     # Each entry whole (report_data._sql).
                     json.dumps({**whole, 'sql': dict(SQL, files=[
                         SQL['files'][0], {'encoding': 'UTF8', 'tests': 0}])}),
                     json.dumps({**whole, 'sql': dict(SQL, files=[
                         SQL['files'][0], {'encoding': 'UTF8'}])}),
                     json.dumps({**whole, 'sql': dict(SQL, not_checked=[
                         dict(SQL['not_checked'][0], names=[])])}),
                     json.dumps({**whole, 'sql': dict(SQL, not_checked=[
                         dict(SQL['not_checked'][0], why='')])}),
                     json.dumps({**whole, 'sql': dict(SQL, not_checked=[
                         dict(SQL['not_checked'][0], also='')])}),
                     json.dumps({**whole, 'sql': dict(SQL, not_checked=[
                         dict(SQL['not_checked'][0], also=None)])}),
                     # What an entry is about, left out or none of the
                     # three.
                     json.dumps({**whole, 'sql': dict(SQL, not_checked=[
                         {k: v for k, v in SQL['not_checked'][0].items()
                          if k != 'about'}])}),
                     json.dumps({**whole, 'sql': dict(SQL, not_checked=[
                         dict(SQL['not_checked'][0], about='sort')])}),
                     json.dumps({**whole, 'sql': {
                         k: v for k, v in SQL.items()
                         if k != 'not_checked'}})):
            with self.subTest(text=text):
                write(self.d, f'step11.{PAIR}.data.json', text)
                with self.assertRaises(rd.ReportError):
                    rd.build(args(self.d, **ON_MACHINES))

    def test_checks_that_do_not_hang_together(self):
        """Step 11's checks beside the order, each written wrong alone,
        refused for the reason named: every key, state and count of the
        control, the decimals of money and the PostgreSQL processes, each
        half of a check of two parts with its own case; the money compared
        against the counts; each machine's keys as its result picks them;
        a result or the note the data do not decide; and a `!!` of the
        three checks the data do not print, or one they print that step
        11 did not write."""
        def ctl(r, side='old', name='C'):
            return r['step11']['checks']['control'][side][name]

        def money(r):
            return r['step11']['checks']['money']

        def libc(r, side='old'):
            return r['step11']['checks']['postgres_libc'][side]

        def named(**over):
            return dict({'reason': 'could not be read', 'count': 1,
                         'processes': 2, 'looked_for': ['postgres']}, **over)

        def flagged(r, **over):
            p = libc(r)
            del p['processes'], p['looked_for']
            p.update(result='flagged', **over)

        keys, nc = 'has the keys', r'old\.not_checked is'
        typed = r'postgres_libc\.old is \{'
        cases = [
            # The control.
            (keys, lambda r: r['step11']['checks']['control'].pop('new')),
            (keys, lambda r: r['step11']['checks']['control']['old'].update(
                LANG={'state': 'byte order'})),
            (r'C\.state is', lambda r: ctl(r).update(state='same')),
            (keys, lambda r: ctl(r).update(state='not measured')),
            (keys, lambda r: ctl(r).update(error='x')),
            (r'C\.error is', lambda r: ctl(r).update(state='not measured',
                                                     error='')),
            (r"control\.old\.C is 'byte",
             lambda r: r['step11']['checks']['control']['old'].update(
                 C='byte order')),
            # The decimals of money.
            (r'compared is -1', lambda r: money(r).update(compared=-1)),
            # True equals the 1 the counts give: the type alone refuses it.
            (r'compared is True$', lambda r: money(r).update(
                compared=True,
                not_checked=[{'name': 'C', 'side': 'old', 'why': 'w'}])),
            (r'locale\(s\) on both machines', lambda r: money(r).update(
                compared=1)),
            (r'locale\(s\) on both machines', lambda r: money(r).update(
                not_checked=[{'name': 'C', 'side': 'old', 'why': 'w'}])),
            (r'money\.changed is', lambda r: money(r).update(changed=[
                {'name': 'C', 'old': 2, 'new': 2}])),
            (r'money\.changed is', lambda r: money(r).update(changed=[
                {'name': 'C', 'old': 2, 'new': 11}])),
            (r'money\.changed is', lambda r: money(r).update(changed=[
                {'name': 'C', 'old': 2}])),
            ('twice', lambda r: money(r).update(changed=[
                {'name': 'C', 'old': 2, 'new': 3},
                {'name': 'C', 'old': 2, 'new': 3}])),
            ('twice', lambda r: money(r).update(
                compared=1, changed=[{'name': 'C', 'old': 2, 'new': 3}],
                not_checked=[{'name': 'C', 'side': 'old', 'why': 'w'}])),
            ('twice', lambda r: money(r).update(compared=1, not_checked=[
                {'name': 'C', 'side': 'old', 'why': 'w'},
                {'name': 'C', 'side': 'old', 'why': 'v'}])),
            (r'money\.not_checked is', lambda r: money(r).update(
                compared=1, not_checked=[{'name': 'C', 'side': 'both',
                                          'why': 'w'}])),
            (r'money\.not_checked is', lambda r: money(r).update(
                compared=1, not_checked=[{'name': 'C', 'side': 'old',
                                          'why': ''}])),
            (r'money\.state is', lambda r: money(r).update(state='same')),
            (r'money\.side is', lambda r: r['step11']['checks'].update(money={
                'state': 'earlier format', 'side': 'either'})),
            (keys, lambda r: r['step11']['checks'].update(money={
                'state': 'earlier format', 'side': 'old', 'compared': 2})),
            # The PostgreSQL processes.
            (r'result is', lambda r: libc(r).update(result='fine')),
            (keys, lambda r: libc(r).pop('processes')),
            (keys, lambda r: libc(r).pop('looked_for')),
            (keys, lambda r: libc(r).update(result='no process found')),
            (keys, lambda r: libc(r).update(result='flagged', stale=1)),
            (typed, lambda r: libc(r).update(processes=0)),
            (typed, lambda r: libc(r).update(looked_for=[])),
            (typed, lambda r: libc(r).update(replaced=1)),
            (typed, lambda r: libc(r).update(stale=-1)),
            (typed, lambda r: libc(r).update(other=True)),
            (nc, lambda r: flagged(r, not_checked={
                'machine': ['a', 'b'], 'named': [], 'unnamed': 0})),
            (nc, lambda r: flagged(r, not_checked={
                'machine': [''], 'named': [], 'unnamed': 0})),
            (nc, lambda r: flagged(r, not_checked={
                'machine': [], 'named': [named(reason='gone')],
                'unnamed': 0})),
            (nc, lambda r: flagged(r, not_checked={
                'machine': [], 'named': [named(), named()], 'unnamed': 0})),
            (nc, lambda r: flagged(r, not_checked={
                'machine': [], 'named': [named(count=0)], 'unnamed': 0})),
            (nc, lambda r: flagged(r, not_checked={
                'machine': [], 'named': [], 'unnamed': -1})),
            (keys, lambda r: flagged(r, not_checked={
                'machine': [], 'named': []})),
            ('two sets', lambda r: flagged(r, not_checked={
                'machine': [], 'unnamed': 0, 'named': [
                    named(), named(reason='show no C library',
                                   processes=3)]})),
            ('two sets', lambda r: flagged(r, not_checked={
                'machine': [], 'unnamed': 0, 'named': [
                    named(), named(reason='show no C library',
                                   looked_for=['postmaster'])]})),
            ('more processes', lambda r: flagged(r, stale=2, not_checked={
                'machine': [], 'named': [named()], 'unnamed': 0})),
            ('more processes', lambda r: flagged(r, other=2, not_checked={
                'machine': [], 'named': [named()], 'unnamed': 0})),
            ('flag nothing', lambda r: flagged(r)),
            ('flag it', lambda r: libc(r).update(replaced=True)),
            ('flag it', lambda r: libc(r).update(not_checked={
                'machine': [], 'named': [], 'unnamed': 1})),
            ('note is False',
             lambda r: r['step11']['checks']['postgres_libc'].update(
                 note=False)),
            ('note is True',
             lambda r: r['step11']['checks']['postgres_libc'].update(
                 old={'state': 'earlier format'},
                 new={'state': 'earlier format'})),
            (keys, lambda r: r['step11']['checks']['postgres_libc'].update(
                old={'state': 'earlier format', 'result': 'clean'})),
            # The `!!` of the three checks, against the data.
            ('other `!!`', lambda r: r['warnings']['step11']['items'].append(
                {'kind': 'money_changed', 'class': 'change',
                 'title': rd.KINDS['money_changed'][2](
                     {'locales': [{'name': 'C', 'old': 2, 'new': 3}]}),
                 'data': {'locales': [{'name': 'C', 'old': 2, 'new': 3}]},
                 'text': '!! money'})),
            ('other `!!`', lambda r: ctl(r).update(state='not byte order')),
        ]
        for why, change in cases:
            with self.subTest(why=why, line=change.__code__.co_firstlineno):
                r = copy.deepcopy(self.good)
                change(r)
                p = os.path.join(self.d, 'changed.json')
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(r, f)
                with self.assertRaisesRegex(rd.ReportError, why):
                    rd.load(p)

    def test_the_checks_constants_are_locale_orders(self):
        """The states, results and reasons the reader allows are those
        locale_order writes: one it added and the reader did not would
        refuse a real run."""
        import locale_order as lo
        self.assertEqual(rd.CONTROL_STATES, lo.CONTROL_STATES)
        self.assertEqual(rd.LIBC_RESULTS, lo.LIBC_RESULTS)
        self.assertEqual(rd.LIBC_NAMED_REASONS,
                         tuple(reason for _, reason in lo.LIBC_NAMED))

    def test_the_check_kinds_are_those_the_checks_print(self):
        """CHECK_KINDS, the `!!` _checks holds to the data, are every kind
        the three checks print (control_texts, money_texts, libc_texts),
        each a literal: one they printed and CHECK_KINDS lacked would pass
        the cross-check unseen on both sides."""
        import ast
        src = os.path.join(os.path.dirname(rd.__file__), 'locale_order.py')
        with open(src, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        kinds = []
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and fn.name in (
                    'control_texts', 'money_texts', 'libc_texts'):
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Call)
                            and getattr(n.func, 'id', None) == 'Bang'):
                        self.assertIsInstance(n.args[1], ast.Constant)
                        kinds.append(n.args[1].value)
        self.assertEqual(sorted(kinds), sorted(rd.CHECK_KINDS))

    @staticmethod
    def use_rich(r):
        """step11_files' run made to name sv_SE in every row and line of
        upper(), lower() and ILIKE, each with the `!!` that names it: an
        error of the new machine with a reason not checked, sequences
        refused, characters not turned back, one turned back otherwise, a
        file that tried one character on the old machine and one before
        the back part on the new: what _use ties, every part present."""
        def bang(kind, data):
            r['warnings']['step11']['items'].append(
                {'kind': kind, 'class': rd.KINDS[kind][0],
                 'title': rd.KINDS[kind][2](data), 'data': data,
                 'text': f'!! {kind}'})

        def utf8():
            # A list of its own for each line, as each line writes it.
            return [{'encoding': 'UTF-8', 'names': ['sv_SE']}]
        r['step11']['checks']['postgres_use'] = {
            'state': 'checked',
            'rows': [{'name': 'sv_SE', 'kind': 'not checked', 'why': 'w'},
                     {'name': 'sv_SE', 'kind': 'error', 'rows': [
                         {'char': 0xE5, 'bytes': 'c3a5', 'before': 'converts',
                          'now': 'error'}]}],
            'lines': {'C': 'one byte', 'sv_SE': 'error'}, 'added': {},
            'back': [],
            'groups': [{'names': ['sv_SE'], 'encoding': 'UTF-8', 'sequences': [
                {'before': 'error', 'now': 'error', 'bytes': ['c3a5']},
                {'before': 'error', 'now': 'not measured', 'bytes': ['c3a6']}]}],
            'back_groups': [{'names': ['sv_SE'], 'encoding': 'UTF-8',
                             'characters': [
                {'char': 0xC5, 'through': [{'fn': 'lower', 'to': 0xE5}],
                 'before': 'not turned back', 'now': 'turned back'},
                {'char': 0xE5, 'through': [{'fn': 'upper', 'to': 0xC5}],
                 'before': 'not turned back', 'now': 'turned back'}]}],
            'back_changed': [{'name': 'sv_SE', 'characters': [
                {'char': 0x41, 'old': {'state': 'bytes', 'bytes': '41'},
                 'new': {'state': 'no bytes'}}]}],
            'part_unknown': [{'name': 'sv_SE', 'side': 'old'}],
            'back_not_checked': [{'name': 'sv_SE', 'utf8': True,
                                  'side': 'new'}],
            'converts': 0, 'one_byte': 1, 'left_out': [], 'elsewhere': 0}
        bang('use_error_new', {'groups': utf8()})
        bang('use_error_partial', {'entries': [{
            'parts': [{'encoding': 'UTF-8', 'alike': 'within',
                       'sides': [{'side': 'old', 'count': 2},
                                 {'side': 'new', 'count': 1}]}],
            'unknown_on': [], 'names': ['sv_SE']}]})
        bang('use_wrong_result', {'entries': [{
            'parts': [{'encoding': 'UTF-8', 'alike': 'one side',
                       'sides': [{'side': 'old', 'count': 2}]}],
            'lower': [{'side': 'old', 'count': 1}], 'names': ['sv_SE']}]})
        bang('use_back_changed', {'entries': [
            {'encoding': 'UTF-8', 'count': 1, 'names': ['sv_SE']}]})
        bang('use_partial_unknown', {'places': [{'side': 'old',
                                                 'groups': utf8()}]})
        bang('use_back_not_checked', {'side': 'new', 'utf8': 1, 'encodings': [
            {'encoding': 'UTF-8', 'count': 1}]})
        bang('use_not_checked', {'reasons': [{'why': 'w', 'groups': utf8()}]})

    def test_use_that_does_not_hang_together(self):
        """Step 11's data on upper(), lower() and ILIKE, each written
        wrong alone over a run that names a locale in every row and line,
        refused for the reason named: every key and state; the line of
        each locale against the comparison's locales, and the counts of
        the line on what converts against those lines; each row against
        the `!!` that names it, the states of an error row as its line
        gives them, each half of a check of two parts with its own case.
        The run itself is read whole."""
        def use(r):
            return r['step11']['checks']['postgres_use']

        def bang(r, kind):
            return next(i['data'] for i in r['warnings']['step11']['items']
                        if i['kind'] == kind)

        def retitle(r):
            for i in r['warnings']['step11']['items']:
                if i['kind'].startswith('use_'):
                    i['title'] = rd.KINDS[i['kind']][2](i['data'])

        def error_row(r):
            return use(r)['rows'][1]['rows'][0]

        def as_kind(r, kind, data, row):
            # sv_SE's error under another `!!` (or the line on what
            # converts back), with the row given.
            items = r['warnings']['step11']['items']
            items[:] = [i for i in items if i['kind'] != 'use_error_new']
            if kind == 'back':
                use(r)['back'] = [{'encoding': 'UTF-8', 'names': ['sv_SE']}]
            else:
                data = dict(data, groups=[{'encoding': 'UTF-8',
                                           'names': ['sv_SE']}])
                items.append({'kind': kind, 'class': rd.KINDS[kind][0],
                              'title': rd.KINDS[kind][2](data),
                              'data': data, 'text': f'!! {kind}'})
            use(r)['rows'][1]['rows'] = row

        def r_(before, now, b='c3a5', **k):
            return dict({'bytes': b, 'before': before, 'now': now}, **k)
        keys, rows = 'has the keys', r'its line'
        cases = [
            # Every key and state.
            (r'postgres_use\.state is', lambda r: use(r).update(state='same')),
            (keys, lambda r: use(r).pop('lines')),
            (keys, lambda r: use(r).update(earlier='old')),
            (r'converts is -1', lambda r: use(r).update(converts=-1)),
            (r'one_byte is True', lambda r: use(r).update(one_byte=True)),
            ('not by encoding', lambda r: use(r).update(left_out=[
                {'encoding': 'B', 'names': ['x']},
                {'encoding': 'A', 'names': ['y']}])),
            ('not by encoding', lambda r: use(r).update(back=[
                {'encoding': 'A', 'names': ['y', 'x']}])),
            (r'earlier is', lambda r: r['step11']['checks'].update(
                postgres_use={**{k: v for k, v in use(r).items() if k not in (
                    'converts', 'one_byte', 'left_out', 'elsewhere')},
                    'state': 'partly', 'earlier': 'both'})),
            (r'lines is', lambda r: use(r)['lines'].update(C='fine')),
            (r'added is', lambda r: use(r).update(added=[])),
            ('a line twice', lambda r: use(r)['added'].update(C='none')),
            (r'kind is', lambda r: use(r)['rows'][0].update(kind='odd')),
            (r'rows\[1\]\.rows is', lambda r: use(r)['rows'][1]['rows'].extend(
                [r_('error', 'error'), r_('error', 'converts')])),
            (r'rows\[1\]\.rows is', lambda r: error_row(r).update(
                before='converts', now='converts')),
            (r'rows\[1\]\.rows is', lambda r: error_row(r).update(
                bytes='C3A5')),
            (r'rows\[1\]\.rows is', lambda r: error_row(r).update(char=True)),
            (r'rows\[1\]\.rows is', lambda r: error_row(r).update(odd=1)),
            (r'rows\[0\]\.why is', lambda r: use(r)['rows'][0].update(why='')),
            (keys, lambda r: use(r)['rows'][0].update(rows=[])),
            ('again', lambda r: use(r)['rows'].append(
                {'name': 'sv_SE', 'kind': 'not checked', 'why': 'v'})),
            (r'groups\[0\] is', lambda r: use(r)['groups'][0]['sequences'][0]
             .update(bytes=['c3a6', 'c3a5'])),
            (r'groups\[0\] is', lambda r: use(r)['groups'][0]['sequences'][0]
             .update(now='lost')),
            ('not grouped by state', lambda r: use(r)['groups'][0].update(
                sequences=use(r)['groups'][0]['sequences'][::-1])),
            ('not grouped by state', lambda r: use(r)['groups'][0][
                'sequences'][1].update(bytes=['c3a5'])),
            ('not grouped by state', lambda r: use(r)['groups'][0][
                'sequences'][0].update(before='converts', now='converts')),
            (r'back_groups\[0\] is', lambda r: use(r)['back_groups'][0][
                'characters'][0].update(before='turned back')),
            (r'back_groups\[0\] is', lambda r: use(r)['back_groups'][0][
                'characters'][0]['through'][0].update(fn='title')),
            (r'back_changed is', lambda r: use(r)['back_changed'][0][
                'characters'][0].update(new={'state': 'refused'})),
            (r'back_changed is', lambda r: use(r)['back_changed'][0][
                'characters'][0].update(new={'state': 'bytes',
                                             'bytes': '41'})),
            (r"part_unknown is \[\{'name'", lambda r: use(r)[
                'part_unknown'][0].update(side='either')),
            ('not by encoding', lambda r: use(r).update(left_out=[
                {'encoding': 'A', 'names': ['x']},
                {'encoding': 'B', 'names': ['x']}])),
            (r'left_out is \[\{', lambda r: use(r).update(left_out=[
                {'encoding': 'A', 'names': []}])),
            (r'groups is \{\}', lambda r: use(r).update(groups={})),
            (keys, lambda r: use(r)['rows'][1].update(why='w')),
            ("names '' again", lambda r: use(r)['rows'][0].update(name='')),
            (r'back_not_checked is', lambda r: use(r)['back_not_checked'][0]
             .update(utf8=1)),
            # The lines, against the comparison and the counts.
            (r'locale\(s\) on both machines and', lambda r: use(r)[
                'lines'].update(X='none')),
            (r'locale\(s\) on both machines and', lambda r: use(r)[
                'added'].update(X='none')),
            (r'rows \(errors\) is .* the lines say', lambda r: use(r)[
                'lines'].update(sv_SE='unknown')),
            ('not checked, and the lines', lambda r: use(r)['lines'].update(
                C='unknown', sv_SE='error')),
            ('not checked, and the lines', lambda r: use(r)['rows'].append(
                {'name': 'C', 'kind': 'not checked', 'why': 'w'})),
            ('leaves out', lambda r: use(r)['lines'].update(
                C='back unknown')),
            ('a locale whose line is not an error', lambda r: use(r).update(
                back=[{'encoding': 'UTF-8', 'names': ['C']}])),
            (r'converts is 1, the lines', lambda r: use(r).update(converts=1)),
            (r'one_byte is 0, the lines', lambda r: use(r).update(one_byte=0)),
            (r'elsewhere is 1, the lines', lambda r: use(r).update(
                elsewhere=1)),
            (r'left_out is \[', lambda r: use(r).update(left_out=[
                {'encoding': 'BIG5', 'names': ['C']}])),
            # Each row against the `!!` that names it.
            ('printed the earlier format', lambda r: (
                r['warnings']['step11']['items'].append(
                    {'kind': 'use_earlier_format', 'class': 'not checked',
                     'title': rd.KINDS['use_earlier_format'][2](
                         {'side': 'old'}),
                     'data': {'side': 'old'}, 'text': '!! x'}))),
            (r'rows \(errors\) is .* the `!!` say', lambda r: bang(
                r, 'use_error_new')['groups'][0].update(names=['C'])),
            (rows, lambda r: error_row(r).update(before='error')),
            (rows, lambda r: as_kind(r, 'use_error_both', {},
                                     [r_('converts', 'error')])),
            (rows, lambda r: as_kind(r, 'back', {}, [r_('converts', 'error')])),
            (rows, lambda r: as_kind(r, 'use_error_one_side',
                                     {'side': 'new', 'reason': 'refused',
                                      'why': []},
                                     [r_('error', 'error')])),
            (rows, lambda r: as_kind(r, 'use_error_one_side',
                                     {'side': 'new', 'reason': 'refused',
                                      'why': []},
                                     [r_('error', 'not measured')])),
            (rows, lambda r: as_kind(r, 'use_error_one_side',
                                     {'side': 'old', 'reason': 'refused',
                                      'why': []},
                                     [r_('error', 'not measured'),
                                      r_('error', 'not measured', 'c3a6')])),
            (rows, lambda r: as_kind(r, 'use_error_one_side',
                                     {'side': 'new', 'reason': 'refused',
                                      'why': []},
                                     [r_('converts', 'error')])),
            (rows, lambda r: as_kind(r, 'use_error_one_side',
                                     {'side': 'old', 'reason': 'refused',
                                      'why': []},
                                     [r_('error', 'converts')])),
            (rows, lambda r: as_kind(r, 'use_error_one_side',
                                     {'side': 'new', 'reason': 'absent',
                                      'why': []},
                                     [r_('converts', 'error')])),
            (rows, lambda r: as_kind(r, 'use_error_any_db',
                                     {'side': 'new', 'why': 'w'},
                                     [r_('error', 'converts')])),
            (rows, lambda r: as_kind(r, 'use_error_any_db',
                                     {'side': 'new', 'why': 'w'},
                                     [r_('not measured', 'error'),
                                      r_('error', 'error', 'c3a6')])),
            (rows, lambda r: as_kind(r, 'use_error_moved',
                                     {'side': 'both', 'from': 'A', 'to': 'B',
                                      'why': []},
                                     [r_('not measured', 'error')])),
            (r'rows \(not checked\)', lambda r: use(r)['rows'][0].update(
                why='v')),
            (r'groups \(names\)', lambda r: bang(r, 'use_error_partial')[
                'entries'][0].update(names=['C'])),
            ('do not give sv_SE', lambda r: use(r)['groups'][0].update(
                encoding='EUC-JP')),
            (r'groups\[0\] \(sv_SE\)', lambda r: bang(r, 'use_error_partial')[
                'entries'][0]['parts'][0]['sides'][0].update(count=3)),
            (r'groups\[0\] \(sv_SE\)', lambda r: use(r)['groups'][0].update(
                sequences=[{'before': 'error', 'now': 'error',
                            'bytes': ['c3a5', 'c3a6']}])),
            ('refuse the same sequences', lambda r: (
                use(r)['groups'][0].update(sequences=[
                    {'before': 'converts', 'now': 'error', 'bytes': ['c3a6']},
                    {'before': 'error', 'now': 'converts', 'bytes': ['c3a5']}]),
                bang(r, 'use_error_partial')['entries'][0]['parts'][0].update(
                    alike='same', sides=[{'side': 'old', 'count': 1},
                                         {'side': 'new', 'count': 1}]))),
            (r'back_groups \(names\)', lambda r: bang(r, 'use_wrong_result')[
                'entries'][0].update(names=['C'])),
            ('do not give sv_SE', lambda r: use(r)['back_groups'][0].update(
                encoding='EUC-JP')),
            (r'back_groups\[0\] \(sv_SE\)', lambda r: use(r)['back_groups'][0][
                'characters'][0].update(before='turned back',
                                        now='not turned back')),
            (r'through lower\(\)', lambda r: use(r)['back_groups'][0][
                'characters'][0]['through'][0].update(fn='upper')),
            (r'through lower\(\)', lambda r: (
                use(r)['back_groups'][0]['characters'][0].update(
                    now='not turned back'),
                use(r)['back_groups'][0]['characters'][1].update(
                    now='not turned back'),
                bang(r, 'use_wrong_result')['entries'][0].update(
                    parts=[{'encoding': 'UTF-8', 'alike': 'same', 'sides': [
                        {'side': 'old', 'count': 2},
                        {'side': 'new', 'count': 2}]}],
                    lower=[{'side': 'old', 'count': 1},
                           {'side': 'new', 'count': 0}]))),
            (r'back_changed is .* the `!!`', lambda r: bang(
                r, 'use_back_changed')['entries'][0].update(count=2)),
            (r'part_unknown is .* the `!!`', lambda r: use(r)['part_unknown'][0]
             .update(side='new')),
            (r'back_not_checked is .* the `!!`', lambda r: bang(
                r, 'use_back_not_checked').update(utf8=0)),
            (r'back_not_checked is .* the `!!`', lambda r: bang(
                r, 'use_back_not_checked').update(side='both')),
            (r'back_not_checked is .* the `!!`', lambda r: bang(
                r, 'use_back_not_checked')['encodings'][0].update(count=2,
                                                                 encoding='B')),
        ]
        good = copy.deepcopy(self.good)
        self.use_rich(good)
        p = os.path.join(self.d, 'rich.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(good, f)
        self.assertEqual(rd.load(p), good)
        # C in the line on what converts, and in the line of a file that
        # tried one character, which takes its place there: read whole,
        # not counted as converting.
        apart = copy.deepcopy(good)
        use(apart)['lines']['C'] = 'converts'
        use(apart).update(one_byte=0)
        use(apart)['part_unknown'].append({'name': 'C', 'side': 'old'})
        bang(apart, 'use_partial_unknown')['places'][0]['groups'][0][
            'names'].insert(0, 'C')
        retitle(apart)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(apart, f)
        self.assertEqual(rd.load(p), apart)
        # sv_SE's error on the new machine alone, where what the old one
        # does is not known because its file tried one character: the old
        # machine's column converts (UNSAID), read whole.
        unsaid = copy.deepcopy(good)
        as_kind(unsaid, 'use_error_one_side',
                {'side': 'new', 'reason': 'unknown', 'why': ['w']},
                [r_('converts', 'error')])
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(unsaid, f)
        self.assertEqual(rd.load(p), unsaid)
        for why, change in cases:
            with self.subTest(why=why, line=change.__code__.co_firstlineno):
                r = copy.deepcopy(good)
                change(r)
                retitle(r)
                p = os.path.join(self.d, 'changed.json')
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(r, f)
                with self.assertRaisesRegex(rd.ReportError, why):
                    rd.load(p)

    def test_the_use_constants_are_locale_orders(self):
        """The states, kinds and lines the reader allows for upper(),
        lower() and ILIKE are those locale_order writes, and the `!!` whose
        locales get an error row each are every such kind use_texts
        prints: one it added and the reader did not would refuse a real
        run, or leave its rows untied."""
        import ast
        import locale_order as lo
        for name in ('USE_STATES', 'USE_LOCALE_KINDS', 'USE_LINES',
                     'SEQUENCE_STATES', 'BACK_STATES'):
            with self.subTest(name=name):
                self.assertEqual(getattr(rd, name), getattr(lo, name))
        src = os.path.join(os.path.dirname(rd.__file__), 'locale_order.py')
        with open(src, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        kinds = []
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and fn.name == 'use_texts':
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Call)
                            and getattr(n.func, 'id', None) == 'Bang'):
                        self.assertIsInstance(n.args[1], ast.Constant)
                        kinds.append(n.args[1].value)
        self.assertEqual(sorted(k for k in kinds if k.startswith('use_error_')
                                and k != 'use_error_partial'),
                         sorted(rd.USE_ERROR_KINDS))

    def ctype_rich(self):
        """(data, counts, warnings) of step 11's character rules over ten
        locales, every line given one at least: a group of characters naming its locale
        (two changes of one character each and one of two characters), a
        group of bytes of every locale compared, a locale not checked;
        three of EUC: one whose characters change, compared for ASCII
        alone, one the same, one not checked on each machine (a reason
        kept as it is, one capitalized); and the locale of UTF-8 under the
        rows of the line on UTF-8's converter; each with the `!!` a run
        prints of it."""
        lines = {'C': 'built in', 'POSIX': 'built in', 'de_DE': 'utf8',
                 'sv_SE': 'utf8', 'ja_JP': 'euc', 'th_TH': 'other encoding',
                 'xx_XX': 'refused', 'yy_YY': 'not checked', 'ko_KR': 'euc',
                 'zh_TW': 'euc'}
        letters = ['alpha', 'alnum', 'print', 'graph']
        data = {'state': 'compared', 'lines': lines, 'character_groups': [
            {'names': ['sv_SE'], 'changes': [
                {'chars': ['U+0282'], 'classified': True,
                 'upper': {'old': 'U+0282', 'new': 'U+A7C5'}},
                {'chars': ['U+08BE', 'U+08BF'], 'classified': False,
                 'classes': {'old': [], 'new': letters}},
                {'chars': ['U+1E9E'], 'classified': True,
                 'upper': {'old': 'U+1E9E', 'new': 'U+00DF'},
                 'lower': {'old': 'U+00DF', 'new': 'U+1E9E'}}]}],
            'byte_groups': [
            {'names': ['de_DE', 'ja_JP', 'ko_KR', 'sv_SE', 'th_TH',
                       'zh_TW'], 'changes': [
                {'bytes': ['A0', 'AD'], 'classes': {'old': ['print'],
                                                    'new': []}},
                {'bytes': ['B5'], 'upper': {'old': 'B5', 'new': 'CC'},
                 'classes': {'old': ['print'], 'new': letters}}]}],
            'euc': {'character_groups': [
                {'names': ['ja_JP'], 'changes': [
                    {'chars': ['U+3000', 'U+3001'], 'classified': False,
                     'classes': {'old': [],
                                 'new': ['punct', 'print', 'graph']}}]}],
                'ascii_alone': [{'side': 'new', 'groups': [
                    {'encoding': 'EUC-JP', 'names': ['ja_JP']}]}],
                'not_checked': [
                    {'name': 'zh_TW', 'side': 'old',
                     'why': "glibc's converter of EUC-TW could not be "
                            'measured: x'},
                    {'name': 'zh_TW', 'side': 'new',
                     'why': 'the converter part holds no EUC-TW'}]},
            'utf8': {'names': ['sv_SE']}}
        counts = {'state': 'reported',
                  'machines': {'old': {'label': 'a', 'locales': 11},
                               'new': {'label': 'b', 'locales': 10}},
                  'changed': 0, 'unchanged': 8,
                  'unchanged_as_far_as_measured': 0, 'not_measured': 1,
                  'refused': 1, 'only_old': 1, 'only_new': 0}
        items = [
            ('ctype_characters_changed', {
                'locales': 1, 'names': ['sv_SE'], 'count': 4,
                'classified': [{'character': 'U+0282', 'change': 'x'},
                               {'character': 'U+1E9E', 'change': 'y'}]}),
            ('ctype_bytes_changed', {
                'locales': 6, 'names': [], 'bytes': [
                    {'byte': 'A0', 'change': 'z'},
                    {'byte': 'AD', 'change': 'z'},
                    {'byte': 'B5', 'change': 'z'}]}),
            ('ctype_not_checked', {'locales': [
                {'name': 'yy_YY', 'side': 'new', 'why': 'w'}]}),
            ('euc_characters_changed', {
                'groups': [{'encoding': 'EUC-JP', 'names': ['ja_JP']}],
                'count': 2, 'classified': []}),
            ('euc_same', {'other': True, 'groups': [
                {'encoding': 'EUC-KR', 'names': ['ko_KR']}]}),
            ('euc_not_checked', {'reasons': [
                {'why': "glibc's converter of EUC-TW could not be "
                        'measured: x', 'side': 'old',
                 'groups': [{'encoding': 'EUC-TW', 'names': ['zh_TW']}]},
                {'why': 'The converter part holds no EUC-TW', 'side': 'new',
                 'groups': [{'encoding': 'EUC-TW', 'names': ['zh_TW']}]}]}),
            ('utf8_changed', {'locales': 1, 'sequences': [
                {'bytes': 'C3 A9', 'old': 'U+00E9', 'new': 'U+00EA'}]})]
        warned = {'state': 'reported',
                  'items': [{'kind': k, 'data': d} for k, d in items]}
        return data, counts, warned

    def test_ctype_that_does_not_hang_together(self):
        """Step 11's data on the character rules (_ctype), each written
        wrong alone over data that puts a locale
        on every line, refused for the reason named: every key and
        state; each group's locales and changes; the lines against the
        counts and C and POSIX; each group against its `!!`, the locales
        not checked and those of EUC against theirs, each half of a check
        of two parts with its own case. The data itself is read whole,
        and so is a change two characters share and a locale of EUC a
        UTF8 database can use too."""
        good = self.ctype_rich()

        def chars(r):
            return r[0]['character_groups'][0]

        def byts(r):
            return r[0]['byte_groups'][0]

        def bang(r, kind):
            return next(i['data'] for i in r[2]['items'] if i['kind'] == kind)

        def drop(r, kind):
            r[2]['items'][:] = [i for i in r[2]['items'] if i['kind'] != kind]

        def add(r, kind, data):
            r[2]['items'].append({'kind': kind, 'data': data})

        def euc(r):
            return r[0]['euc']
        keys, group, ours = 'has the keys', r'names is', 'the `!!` say'
        cases = [
            # Every key and state.
            (r'ctype\.state is', lambda r: r[0].update(state='same')),
            (keys, lambda r: r[0].update(state='earlier format')),
            (keys, lambda r: r[0].pop('byte_groups')),
            (r'lines is', lambda r: r[0]['lines'].update(C='odd')),
            (r'lines is', lambda r: r[0].update(lines=[])),
            (r'character_groups is', lambda r: r[0].update(
                character_groups={})),
            (group, lambda r: byts(r).update(names=['th_TH', 'de_DE',
                                                    'ja_JP', 'sv_SE'])),
            (keys, lambda r: r[0].pop('euc')),
            (keys, lambda r: r[0].pop('utf8')),
            (keys, lambda r: euc(r).pop('ascii_alone')),
            (keys, lambda r: r[0]['utf8'].update(odd=1)),
            (group, lambda r: byts(r).update(names=['de_DE', 'de_DE'])),
            (group, lambda r: byts(r).update(names=[])),
            (group, lambda r: chars(r).update(names=['th_TH'])),
            (group, lambda r: byts(r).update(names=['C'])),
            (r'changes is', lambda r: chars(r).update(changes=[])),
            (keys, lambda r: chars(r).update(odd=1)),
            (keys, lambda r: chars(r)['changes'][0].pop('classified')),
            (keys, lambda r: chars(r)['changes'][0].update(odd=1)),
            (keys, lambda r: byts(r)['changes'][0].update(classified=True)),
            (r'changes\[0\] is', lambda r: chars(r)['changes'][0].pop(
                'upper')),
            (r'changes\[0\] is', lambda r: chars(r)['changes'][0].update(
                chars=[])),
            (r'changes\[0\] is', lambda r: chars(r)['changes'][0].update(
                chars=['U+12'])),
            (r'changes\[0\] is', lambda r: chars(r)['changes'][0].update(
                chars='U+0282')),
            (r'changes\[0\] is', lambda r: chars(r)['changes'][0].update(
                classified=1)),
            (r'changes\[1\] is', lambda r: byts(r)['changes'][1].update(
                bytes=['b5'])),
            (r'upper is', lambda r: chars(r)['changes'][0]['upper'].update(
                new='U+0282')),
            (r'upper is', lambda r: chars(r)['changes'][0]['upper'].update(
                new='A7C5')),
            (r'upper is', lambda r: byts(r)['changes'][1]['upper'].update(
                new='U+00CC')),
            (keys, lambda r: chars(r)['changes'][0]['upper'].pop('new')),
            (r'classes is', lambda r: chars(r)['changes'][1]['classes'].update(
                new=['print', 'alpha'])),
            (r'classes is', lambda r: chars(r)['changes'][1]['classes'].update(
                new=['alpha', 'alpha'])),
            (r'classes is', lambda r: chars(r)['changes'][1]['classes'].update(
                new=['letter'])),
            (r'classes is', lambda r: chars(r)['changes'][1]['classes'].update(
                new=[])),
            (r'classes is', lambda r: chars(r)['changes'][1]['classes'].update(
                old=None)),
            ('not in order', lambda r: chars(r)['changes'][1].update(
                chars=['U+08BF', 'U+08BE'])),
            ('not in order', lambda r: chars(r).update(
                changes=chars(r)['changes'][::-1])),
            ('not in order', lambda r: chars(r)['changes'][2].update(
                chars=['U+08BE'])),
            ('not in order', lambda r: byts(r)['changes'][0].update(
                bytes=['A0', 'A0'])),
            ('twice or out of order', lambda r: r[0]['byte_groups'].append(
                {'names': ['th_TH'], 'changes': [
                    {'bytes': ['41'], 'classes': {'old': [],
                                                  'new': ['print']}}]})),
            ('twice or out of order', lambda r: (
                byts(r).update(names=['th_TH']),
                r[0]['byte_groups'].append(
                    {'names': ['de_DE'], 'changes': [
                        {'bytes': ['41'], 'classes': {
                            'old': [], 'new': ['print']}}]}))),
            # The lines, against the counts and C and POSIX.
            (r'gives 11 locale\(s\) a line', lambda r: r[0]['lines'].update(
                zz_ZZ='other encoding')),
            (r'gives 10 locale\(s\) a line, 0 refused', lambda r: r[0][
                'lines'].update(xx_XX='other encoding')),
            ('built in', lambda r: r[0]['lines'].update(
                POSIX='other encoding')),
            ('built in', lambda r: r[0]['lines'].update(yy_YY='built in')),
            # Each group against its `!!`.
            ('printed the earlier format', lambda r: add(
                r, 'ctype_earlier_format', {'side': 'old'})),
            (r'character_groups are .* ' + ours, lambda r: bang(
                r, 'ctype_characters_changed').update(count=3)),
            (r'character_groups are .* ' + ours, lambda r: bang(
                r, 'ctype_characters_changed').update(names=[], locales=1)),
            (r'character_groups are .* ' + ours, lambda r: chars(r)[
                'changes'][1].update(classified=True)),
            (r'character_groups are .* ' + ours, lambda r: drop(
                r, 'ctype_characters_changed')),
            (r'byte_groups are .* ' + ours, lambda r: bang(
                r, 'ctype_bytes_changed')['bytes'].pop()),
            (r'byte_groups are .* ' + ours, lambda r: bang(
                r, 'ctype_bytes_changed').update(names=[
                    'de_DE', 'ja_JP', 'sv_SE', 'th_TH'])),
            (r'byte_groups are .* ' + ours, lambda r: add(
                r, 'ctype_bytes_changed', bang(r, 'ctype_bytes_changed'))),
            # The locales not checked and those of EUC against theirs.
            (r'lines \(not checked\)', lambda r: r[0]['lines'].update(
                yy_YY='refused', xx_XX='not checked')),
            (r'lines \(not checked\)', lambda r: bang(
                r, 'ctype_not_checked')['locales'].append(
                    {'name': 'th_TH', 'side': 'old', 'why': 'w'})),
            ('give EUC', lambda r: r[0]['lines'].update(
                ko_KR='other encoding')),
            ('give EUC', lambda r: r[0]['lines'].update(th_TH='euc')),
            ('give EUC', lambda r: bang(r, 'euc_same')['groups'].append(
                {'encoding': 'TIS-620', 'names': ['th_TH']})),
            ('give EUC', lambda r: bang(r, 'euc_same')['groups'].append(
                {'encoding': 'X', 'names': ['C']})),
            # The groups of EUC, read as those of characters and held to
            # their `!!`.
            (r'euc\.character_groups\[0\]\.names is', lambda r: euc(r)[
                'character_groups'][0].update(names=['th_TH'])),
            (r'euc\.character_groups\[0\]\.changes\[0\] is', lambda r: euc(
                r)['character_groups'][0]['changes'][0].update(chars=[])),
            (r'euc\.character_groups are .* ' + ours, lambda r: bang(
                r, 'euc_characters_changed').update(count=1)),
            (r'euc\.character_groups are .* ' + ours, lambda r: euc(r)[
                'character_groups'][0]['changes'][0].update(classified=True)),
            (r'euc\.character_groups are .* ' + ours, lambda r: bang(
                r, 'euc_characters_changed').update(classified=[
                    {'character': 'U+3000', 'change': 'x'}])),
            (r'euc\.character_groups are .* ' + ours, lambda r: bang(
                r, 'euc_characters_changed')['groups'][0]['names'].append(
                    'ko_KR')),
            # Compared for ASCII alone: its shape, and each half of its
            # tie to the lines on EUC.
            (r'ascii_alone is', lambda r: euc(r)['ascii_alone'][0].update(
                side='odd')),
            (r'ascii_alone is', lambda r: euc(r)['ascii_alone'].append(
                {'side': 'new', 'groups': [
                    {'encoding': 'EUC-KR', 'names': ['ko_KR']}]})),
            (r'ascii_alone names', lambda r: euc(r)['ascii_alone'].append(
                {'side': 'old', 'groups': [
                    {'encoding': 'EUC-JP', 'names': ['ja_JP']}]})),
            (r'ascii_alone names', lambda r: euc(r)['ascii_alone'][0][
                'groups'].append({'encoding': 'TIS-620',
                                  'names': ['th_TH']})),
            (r'euc\.ascii_alone names .* change', lambda r: euc(r)[
                'ascii_alone'][0]['groups'].append(
                    {'encoding': 'EUC-KR', 'names': ['ko_KR']})),
            (r'euc\.ascii_alone names .* change', lambda r: add(
                r, 'euc_ascii_same', {'side': 'new', 'groups': [
                    {'encoding': 'EUC-JP', 'names': ['ja_JP']}]})),
            # The locales of EUC not checked: their shape, and against
            # their `!!`, the reason as a sentence starts with it.
            (r'not_checked is', lambda r: euc(r)['not_checked'][0].update(
                side='odd')),
            (r'not_checked is', lambda r: euc(r)['not_checked'][0].update(
                odd=1)),
            (r"ascii_alone names \['ja_JP', 'th_TH'\]$", lambda r: (
                r[2].update(state='not reported', why=['x']),
                euc(r)['ascii_alone'][0]['groups'].append(
                    {'encoding': 'TIS-620', 'names': ['th_TH']}))),
            (r'not_checked is', lambda r: euc(r)['not_checked'][0].update(
                side='both')),
            (r'not_checked is', lambda r: euc(r)['not_checked'].append(
                {'name': 'ko_KR', 'side': 'old', 'why': 'w'})),
            (r'not_checked is', lambda r: euc(r)['not_checked'].insert(
                0, {'name': 'th_TH', 'side': 'old', 'why': 'w'})),
            (r'euc\.not_checked are', lambda r: bang(
                r, 'euc_not_checked')['reasons'][0].update(side='new')),
            (r'euc\.not_checked are', lambda r: bang(
                r, 'euc_not_checked')['reasons'][0].update(
                    why="Glibc's converter of EUC-TW could not be "
                        'measured: x')),
            (r'euc\.not_checked are', lambda r: bang(
                r, 'euc_not_checked')['reasons'][1].update(
                    why='the converter part holds no EUC-TW')),
            (r'euc\.not_checked are', lambda r: bang(
                r, 'euc_not_checked')['reasons'][1]['groups'].append(
                    {'encoding': 'EUC-KR', 'names': ['ko_KR']})),
            (r'euc\.not_checked are', lambda r: euc(r)['not_checked'].pop()),
            # The locales of UTF-8 under the rows of its converter's line.
            (r'utf8\.names is', lambda r: r[0]['utf8'].update(
                names=['sv_SE', 'de_DE'])),
            (r'utf8\.names is', lambda r: r[0]['utf8'].update(
                names=['sv_SE', 'sv_SE'])),
            (r'utf8\.names is', lambda r: r[0]['utf8'].update(
                names=['th_TH'])),
            (r'utf8\.names are', lambda r: r[0]['utf8'].update(
                names=['de_DE', 'sv_SE'])),
            (r'utf8\.names are', lambda r: r[0]['utf8'].update(names=[])),
            (r'utf8\.names are', lambda r: drop(r, 'utf8_changed')),
        ]

        def read(r):
            rd._ctype('step11.checks.ctype', r[0], r[1], r[2])
        read(copy.deepcopy(good))
        # Two characters that share one change of capital, and a locale of
        # EUC a UTF8 database can use too: read whole.
        shared = copy.deepcopy(good)
        chars(shared)['changes'][0].update(
            chars=['U+0282', 'U+0283'], upper={'old': 'U+0282',
                                              'new': 'U+A7C5'})
        bang(shared, 'ctype_characters_changed').update(count=5, classified=[
            {'character': 'U+0282', 'change': 'x'},
            {'character': 'U+0283', 'change': 'x'},
            {'character': 'U+1E9E', 'change': 'y'}])
        add(shared, 'euc_ascii_same', {'side': 'new', 'groups': [
            {'encoding': 'EUC-JP', 'names': ['de_DE']}]})
        read(shared)
        # A file of an earlier format, whose line on UTF-8's converter is
        # one machine's: its locales read, held to their number alone.
        early = ({'state': 'earlier format', 'utf8': {'names': ['sv_SE']}},
                 good[1], {'state': 'reported', 'items': [
                     {'kind': 'ctype_earlier_format', 'data': {'side': 'old'}},
                     {'kind': 'utf8_one_side', 'data': {
                         'side': 'new', 'why': [], 'locales': 1,
                         'sequences': [{'bytes': 'C3 A9', 'old': 'U+00E9',
                                        'new': 'U+00EA'}]}}]})
        read(early)
        early[0]['utf8']['names'] = []
        with self.assertRaisesRegex(rd.ReportError, r'utf8\.names are'):
            read(early)
        # Counts or warnings step 11 did not report: the shape alone.
        for k in (1, 2):
            alone = list(copy.deepcopy(good))
            alone[k] = {'state': 'not reported', 'why': ['x']}
            alone[0]['lines']['zz_ZZ'] = 'refused'
            if k == 1:
                read(alone)
            else:
                with self.assertRaisesRegex(rd.ReportError, 'a line'):
                    read(alone)
        for why, change in cases:
            with self.subTest(why=why, line=change.__code__.co_firstlineno):
                r = copy.deepcopy(good)
                change(r)
                with self.assertRaisesRegex(rd.ReportError, why):
                    read(r)

    def test_the_ctype_lines_are_those_of_upper_and_lower(self):
        """The locales the data on the character rules gives a line are
        those the data on upper(), lower() and ILIKE gives one, both of
        the locales on both machines: a locale one names and the other
        does not is refused, through load()."""
        for where in ('ctype', 'postgres_use'):
            with self.subTest(where=where):
                def change(r, where=where):
                    lines = r['step11']['checks'][where]['lines']
                    lines['de_DE'] = lines.pop('sv_SE')
                with self.assertRaisesRegex(rd.ReportError,
                                            'name other locales'):
                    self.refuses_by(change)

    def refuses_by(self, change):
        r = copy.deepcopy(self.good)
        change(r)
        p = os.path.join(self.d, 'changed.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        rd.load(p)

    def test_the_euc_side_is_that_of_upper_and_lower(self):
        """The machine whose converter of EUC does not open, for each
        locale of EUC compared (_euc_ascii_sides): as the lines on EUC
        say it, compared for ASCII alone or the same for ASCII, and as the
        lines on upper(), lower() and ILIKE say it, an error on the new
        machine, on the old one or on both; each wrong alone, in either
        direction, is refused; a locale of another encoding in those lines
        is not one of them; and the check runs through load()."""
        def run(alone, same, new, back, both, other=()):
            c = {'ctype': {'state': 'compared', 'euc': {'ascii_alone': [
                {'side': s, 'groups': [{'encoding': 'E', 'names': n}]}
                for s, n in alone]}},
                 'postgres_use': {'state': 'checked', 'back': [
                     {'encoding': 'E', 'names': back}] if back else []}}
            items = [{'kind': 'euc_ascii_same', 'data': {
                'side': s, 'groups': [{'encoding': 'E', 'names': n}]}}
                for s, n in same]
            items += [{'kind': k, 'data': {'groups': [
                {'encoding': 'E', 'names': n}]}}
                for k, n in (('use_error_new', new), ('use_error_both', both))
                if n]
            items += [{'kind': 'euc_same', 'data': {'other': False, 'groups': [
                {'encoding': 'E', 'names': list(other)}]}}] if other else []
            rd._euc_ascii_sides('x', c, {'state': 'reported', 'items': items})
        good = dict(alone=[('new', ['a'])], same=[('both', ['b']),
                                                  ('old', ['c'])],
                    new=['a', 'th_TH'], back=['c'], both=['b'], other=['d'])
        run(**good)
        for why, over in (
                ('a side', dict(alone=[('old', ['a'])])),
                ('a side', dict(same=[('new', ['b']), ('old', ['c'])])),
                ('missing on EUC', dict(same=[('both', ['b'])],
                                        other=['c', 'd'])),
                ('missing on upper', dict(back=[])),
                ('a locale of EUC the same', dict(new=['a', 'd'])),
                ('a locale of EUC changed', dict(both=['b', 'a']))):
            with self.subTest(why=why):
                with self.assertRaisesRegex(rd.ReportError,
                                            'compare ASCII alone on'):
                    run(**dict(good, **over))

        def change(r):
            d = {'side': 'new', 'groups': [{'encoding': 'UTF-8',
                                            'names': ['sv_SE']}]}
            r['warnings']['step11']['items'].append(
                {'kind': 'euc_ascii_same',
                 'class': rd.KINDS['euc_ascii_same'][0],
                 'title': rd.KINDS['euc_ascii_same'][2](d), 'data': d,
                 'text': '!! x'})
        with self.assertRaisesRegex(rd.ReportError, 'compare ASCII alone on'):
            self.refuses_by(change)

    def test_the_ctype_constants_are_locale_orders(self):
        """The states, lines, classes and sides the reader allows for
        the character rules, the lines each kind of group compares, and
        the words a reason keeps as they are where it starts a sentence
        (with _sentence against sentence), are those locale_order writes
        and prints by; and locale_order refuses to write a state, line,
        class or side outside them: one it added and the reader did not
        would refuse a real run."""
        import locale_order as lo
        for name in ('CTYPE_DATA_STATES', 'CTYPE_LINES', 'CTYPE_CLASSES',
                     'CTYPE_COMPARED', 'CTYPE_SIDES', 'SENTENCE_AS_IS'):
            with self.subTest(name=name):
                self.assertEqual(getattr(rd, name), getattr(lo, name))
        data = self.ctype_rich()[0]
        self.assertIs(lo.ctype_held(data), data)
        # The reader's sentence of a reason is locale_order's.
        for why in ("glibc's converter", 'iconv_open said', 'its encoding',
                    'Its encoding', 'glibcx', 'g', ''):
            with self.subTest(why=why):
                self.assertEqual(rd._sentence(why), lo.sentence(why))
        for change in (lambda d: d.update(state='same'),
                       lambda d: d['lines'].update(C='odd'),
                       lambda d: d['byte_groups'][0]['changes'][0][
                           'classes'].update(new=['letter']),
                       lambda d: d['euc']['character_groups'][0]['changes'][
                           0]['classes'].update(new=['letter']),
                       lambda d: d['euc']['ascii_alone'][0].update(
                           side='either'),
                       lambda d: d['euc']['not_checked'][0].update(
                           side='either')):
            d = copy.deepcopy(data)
            change(d)
            with self.assertRaisesRegex(ValueError, 'outside their'):
                lo.ctype_held(d)

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
            for part in ('listed', 'rules_characters', 'rules'):
                r['step2'][part] = {'state': 'not compared', 'why': 'x'}
            r['step2']['character_sets'] = {'state': 'one commit'}
            r['step2']['transliteration'] = {'state': 'one commit'}
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
        self.refuses(lambda r: r['warnings'].update(step2=NOT_YET))
        step11 = lambda r: r['warnings']['step11']['items'][1]
        self.refuses(lambda r: step11(r).update(title='Nothing changed'))
        self.refuses(lambda r: step11(r)['data'].update(told='yes'))
        self.refuses(lambda r: r['warnings'].update(step11=NOT_YET))

    def test_step11_warnings_not_run_exactly_when_step11_did_not_run(self):
        """Step 11's warnings are not run where step 11 is, and only
        there: refused by that check, not by another."""
        def no_warnings(r):
            r['warnings']['step11'] = {'state': 'not run', 'why': 'x'}

        def no_step11(r):
            r['step11'] = {'state': 'not run', 'why': "no machines' files",
                           'not_done': list(rd.STEP11_NOT_DONE)}
        for change in (no_warnings, no_step11):
            r = copy.deepcopy(self.good)
            change(r)
            p = os.path.join(self.d, 'changed.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(r, f)
            with self.subTest(change=change.__name__), \
                    self.assertRaisesRegex(rd.ReportError,
                                           'warnings.step11 is not run'):
                rd.load(p)

    def test_step0_not_run_with_the_machines_given(self):
        def change(r):
            r['warnings']['step0'] = {'state': 'not run', 'why': 'x'}
        self.refuses(change)

    def test_not_run_says_what_it_leaves(self):
        def change(r):
            r['step11'] = {'state': 'not run', 'why': 'x', 'not_done': []}
        self.refuses(change)



class StepTwosRulesHangTogether(_Folder):
    """step2.rules (step 2's step2_rules), each fact written wrong alone,
    refused by load() for the reason named, on two machines and on two
    tags; and the controls that a right change is read."""

    def read(self, r):
        p = os.path.join(self.d, 'changed.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        return rd.load(p)

    def check(self, good, cases, controls=()):
        self.assertEqual(self.read(copy.deepcopy(good)), good)
        for why, change in cases:
            with self.subTest(why=why, line=change.__code__.co_firstlineno):
                r = copy.deepcopy(good)
                change(r)
                with self.assertRaisesRegex(rd.ReportError, why):
                    self.read(r)
        for change in controls:
            with self.subTest(control=change.__code__.co_firstlineno):
                r = copy.deepcopy(good)
                change(r)
                self.assertEqual(self.read(r), r)

    def machines(self):
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        warnings_file(self.d, 0, [])
        warnings_file(self.d, 1, [])
        warnings_file(self.d, 2, [])
        warnings_file(self.d, 11, ['same_build', 'other_encoding',
                                   'closing'])
        return self.written(**ON_MACHINES)

    def test_on_two_machines(self):
        good = self.machines()

        def R(r):
            return r['step2']['rules']

        def more(r, **k):
            for key, n in k.items():
                R(r)['totals'][key] += n
                R(r)['totals']['names'] += n

        def added(r, spellings, renamed_from='xx', name='yy'):
            R(r)['added'] = {'state': 'plain', 'rows': [
                {'name': name, 'spellings': spellings,
                 'renamed_from': renamed_from}]}
            more(r, not_compared=1)

        def group(r, **k):
            R(r)['differ'][0].update(k)

        def skipped(r, why, other=None):
            # de_IT skipped on both machines, as step 1 says it.
            items = r['warnings']['step1']['items']
            for machine in ('glibc-2.28', other or 'glibc-2.34'):
                items.append(rd.warning_item(
                    'x', 'machine_entries_skipped',
                    {'machine': machine,
                     'entries': [{'entry': 'de_IT', 'why': 'symlink'}]},
                    '!! machine_entries_skipped'))
            R(r)['unread'].append({'name': 'de_IT', 'why': why,
                                   'counted': False})

        def unsafe_and_bare(r):
            entries = [{'entry': 'aa_DJ', 'why': 'symlink'},
                       {'entry': ascii('de_DE~'), 'why': 'unsafe name'}]
            r['warnings']['step1']['items'].append(rd.warning_item(
                'x', 'machine_entries_skipped',
                {'machine': 'glibc-2.28', 'entries': entries},
                '!! machine_entries_skipped'))
            R(r)['unread'] += [
                {'name': 'aa_DJ', 'counted': False,
                 'why': ['symlink on the old machine']},
                {'name': ascii('de_DE~'), 'counted': False,
                 'why': ['unsafe name on the old machine']}]

        cases = [
            ('totals do not add up',
             lambda r: R(r)['totals'].update(names=99)),
            ('names in its groups', lambda r: more(r, differ=1)),
            ('the characters file',
             lambda r: group(r, names=['zz_ZZ'])),
            ('its row', lambda r: group(
                r, characters={'state': 'counted', 'count': 3})),
            ('its row', lambda r: group(
                r, every_rule=False,
                characters={'state': 'listed', 'codepoints': [0, 1]})),
            (r'differ\[0\]: ', lambda r: group(
                r, characters={'state': 'listed', 'codepoints': [0, 1]})),
            (r'differ\[0\]: ', lambda r: group(
                r, every_rule=False)),
            ('names one locale twice', lambda r: R(r)['differ'].append(
                copy.deepcopy(R(r)['differ'][0]))),
            ('the Removed file', lambda r: (R(r)['removed'].pop(),
                                            more(r, not_compared=-1))),
            ('the Removed file',
             lambda r: R(r)['removed'][1].update(renamed_to='zz')),
            ('which Removed gives', lambda r: added(
                r, {'state': 'not measured on that machine'}, 'qq')),
            ('added.state is', lambda r: (
                added(r, {'state': 'not measured on that machine'}),
                R(r)['added'].update(state='notice'))),
            ('is no measured name of', lambda r: added(r, ['zz_ZZ'])),
            ('with machines=True', lambda r: added(
                r, {'state': 'not in SUPPORTED'})),
            ('step 1', lambda r: (R(r)['unread'].append(
                {'name': 'zz_U', 'why': ['symlink on the new machine'],
                 'counted': False}))),
            ('counted, yet not read on both machines',
             lambda r: R(r)['unread'].append(
                 {'name': 'zz_U', 'counted': True,
                  'why': ['symlink on the old machine',
                          'symlink on the new machine']})),
            ('not_compared', lambda r: more(r, not_compared=1)),
            ('unbuilt is', lambda r: R(r).update(
                unbuilt={'state': 'listed', 'names': []})),
            ('printed bare', lambda r: R(r)['not_ascii'].append('zz_ZZ')),
            ('where step 2 compared something', lambda r: r['step2'].update(
                rules={'state': 'not compared', 'why': 'x'})),
            ('added files give as from', lambda r: added(
                r, {'state': 'not measured on that machine'}, '-')),
            ('step 1', lambda r: skipped(r, ['symlink on the new machine',
                                             'symlink on the old machine'])),
            ('skipped entries of', lambda r: skipped(
                r, ['symlink on the old machine',
                    'symlink on the new machine'], other='zz_machine')),
            # sv_SE is measured, and step 11 calls it on the new machine.
            ('measured on the new machine under it', lambda r: added(
                r, {'state': 'not measured on that machine'}, '-', 'sv_SE')),
            ('yet a `!!`', lambda r: r['warnings']['step2']['items'].append(
                rd.warning_item('x', 'added_not_at_old',
                                {'at': 'glibc-2.28', 'named': 'tag',
                                 'locales': ['zz']}, '!! added_not_at_old'))),
            ('in the list', lambda r: (
                r['step2']['listed']['compared'].append('zz Z'),
                R(r)['not_ascii'].append(ascii('zz Z')))),
        ]
        self.check(good, cases, controls=[
            # A file only the new side has, renamed from a removed one,
            # that no name measured on the new machine is built from.
            lambda r: added(r, {'state': 'not measured on that machine'}),
            # The names measured on the new machine under the file.
            lambda r: added(r, ['sv_SE'], '-', 'sv_SE'),
            # An entry skipped on both machines: its reasons old first.
            lambda r: skipped(r, ['symlink on the old machine',
                                  'symlink on the new machine']),
            # A link and an editor's backup on the old machine: step 2
            # sorts them by their own names, step 1 quotes the backup's.
            lambda r: unsafe_and_bare(r),
        ])

    def test_on_two_tags(self):
        step2_files(self.d)
        warnings_file(self.d, 1, [])
        with open(os.path.join(self.d, f'step2_rules.{PAIR}.json'),
                  encoding='utf-8') as f:
            rules = json.load(f)
        rules['added'] = {'state': 'notice', 'rows': [
            {'name': 'zz_NEW', 'spellings': {'state': 'not in SUPPORTED'},
             'renamed_from': '-'}]}
        rules['totals']['not_compared'] += 1
        rules['totals']['names'] += 1
        write(self.d, f'step2_rules.{PAIR}.json', json.dumps(rules) + '\n')
        write(self.d, f'step2.{PAIR}.warnings.jsonl', json.dumps(
            {'kind': 'added_not_at_old', 'text': '!! added_not_at_old',
             'data': {'at': 'glibc-2.28', 'named': 'tag',
                      'locales': ['zz_NEW']}}) + '\n')
        good = self.written()

        def items(r):
            return r['warnings']['step2']['items']

        def warning(kind, data):
            return rd.warning_item('x', kind, data, f'!! {kind}')

        def c_added(r):
            rules = r['step2']['rules']
            rules['added']['rows'].append(
                {'name': 'C.UTF-8', 'renamed_from': '-',
                 'spellings': {'state': rd.MISSING_STATES[2]}})
            rules['totals']['not_compared'] += 1
            rules['totals']['names'] += 1

        def notice_names(r, names):
            items(r)[0]['data']['locales'] = names
            items(r)[0]['title'] = rd.KINDS['added_not_at_old'][2](
                items(r)[0]['data'])

        cases = [
            ('the `!!`', lambda r: notice_names(r, ['zz_OTHER'])),
            ('the `!!`', lambda r: items(r).clear()),
            ('added.state is', lambda r: r['step2']['rules']['added'].update(
                state='plain')),
            ('rules not known names', lambda r: items(r).append(warning(
                'added_rules_unknown', {'old': 'glibc-2.28',
                                        'new': 'glibc-2.34',
                                        'locales': [{'name': 'zz_X',
                                                     'why': 'w'}]}))),
            ('not among the added files', lambda r: items(r).append(warning(
                'c_utf8_no_source', {'sides': ['glibc-2.28']}))),
            ('not among the removed files', lambda r: items(r).append(
                warning('c_utf8_no_source', {'sides': ['glibc-2.34']}))),
            ('with no machines', lambda r: r['step2']['rules'][
                'unread'].append({'name': 'zz_U', 'counted': False,
                                  'why': ['symlink on the new machine']})),
            ('C.UTF-8 has no file at the old', lambda r: c_added(r)),
            ('with the added state', lambda r: (c_added(r), r['step2'][
                'rules']['added']['rows'][-1].update(
                    spellings={'state': 'not in SUPPORTED'}), items(r).append(
                warning('c_utf8_no_source', {'sides': ['glibc-2.28']})))),
            ('not all in the list', lambda r: r['step2']['rules'][
                'unbuilt']['names'].append('zz_ZZ')),
            ('neither in the list', lambda r: (
                r['step2']['rules']['differ'].append(
                    {'names': ['zz_A'], 'every_rule': False,
                     'characters': {'state': 'none named'}}),
                r['step2']['rules']['totals'].update(
                    differ=2, names=r['step2']['rules']['totals']['names']
                    + 1),
                r['step2']['rules_characters']['rows'].append(
                    ['zz_A', 'differs', 'no', '']))),
        ]
        self.check(good, cases, controls=[
            lambda r: items(r).append(warning(
                'added_rules_unknown', {'old': 'glibc-2.28',
                                        'new': 'glibc-2.34',
                                        'locales': [{'name': 'zz_NEW',
                                                     'why': 'w'}]})),
            # Both tags without C: C.UTF-8 is neither added nor removed.
            lambda r: items(r).append(warning(
                'c_utf8_no_source', {'sides': ['glibc-2.28',
                                               'glibc-2.34']})),
            # C at the new tag alone: added, and the `!!` says so.
            lambda r: (c_added(r), items(r).append(warning(
                'c_utf8_no_source', {'sides': ['glibc-2.28']}))),
        ])

    def test_where_nothing_was_compared(self):
        what = nothing_files(self.d)
        good = self.written()
        self.assertEqual(good['step2']['rules'],
                         {'state': 'not compared', 'why': what})
        self.check(good, [
            ('is not', lambda r: r['step2']['rules'].update(why='x')),
        ])

    def test_the_constants_are_step_2s(self):
        """The states the reader takes are the ones step 2 writes."""
        import filter_lc_collate_changes as f
        self.assertEqual(rd.CHARACTER_STATES, f.CHARACTER_STATES)
        self.assertEqual(rd.MISSING_STATES,
                         tuple(f.MISSING_STATES.values()) + (f.C_NOTICE,))
        self.assertEqual(rd.ADDED_STATES, f.ADDED_STATES)


class StepTwosTransliterationHangsTogether(_Folder):
    """step2.transliteration (step 2's step2_transliteration), each fact
    written wrong alone, refused by load() for the reason named, on two
    tags, on two machines and where the character set check did not run;
    and the controls that a right change is read. On two machines also
    step 2's rules by name, each name's mark held to the rows of both
    checks of its file, and their five fields, read as step 2 writes
    them."""

    read = StepTwosRulesHangTogether.read
    check = StepTwosRulesHangTogether.check

    def files(self, **by_name):
        """The check compared 3 builds in GB18030 and UTF-8, of which UTF-8
        changed; the
        transliterations: 5 elements of 2 builds, sv_SE listed with 3
        built differently, C not compared, and its `!!`."""
        step2_files(self.d, **by_name)
        machines = by_name.get('machines')
        write(self.d, f'step2_character_sets.{PAIR}.json', json.dumps(
            {'step': 2, 'state': 'compared',
             'basis': 'measured' if machines else 'SUPPORTED', 'builds': 3,
             'charmaps': ['GB18030', 'UTF-8'],
             'changed': [{'old': 'UTF-8', 'new': 'UTF-8',
                          'state': 'compared', 'added': 2, 'removed': 0,
                          'other': 0}],
             'listed': [], 'anyway': [], 'unsettled': [], 'distro': [],
             'unsearched': [], 'not_compared': []}) + '\n')
        sv, c = (('sv_SE', 'C.utf8') if machines
                 else ('sv_SE.UTF-8', 'C.UTF-8'))
        write(self.d, f'step2_transliteration.{PAIR}.json', json.dumps(
            {'step': 2, 'state': 'compared', 'line': 'elements',
             'elements': 5, 'builds': 2, 'changed': 3, 'skipped': [],
             'listed': [{'entry': sv, 'source': 'sv_SE', 'old': 'UTF-8',
                         'new': 'UTF-8', 'count': 3, 'codepoints': [699],
                         'via': ['sv_SE']}],
             'unsettled': [{'entry': c, 'source': 'C', 'old': 'UTF-8',
                            'new': 'UTF-8', 'why': 'bad'}]}) + '\n')
        write(self.d, f'step2.{PAIR}.warnings.jsonl', json.dumps(
            {'kind': 'translit_unsettled', 'text': '!! translit_unsettled',
             'data': {'builds': [{'build': c, 'charmap': 'UTF-8',
                                  'why': 'bad'}]}}) + '\n')

    @staticmethod
    def T(r):
        return r['step2']['transliteration']

    def test_on_two_tags(self):
        self.files()
        warnings_file(self.d, 1, [])
        good = self.written()
        T = self.T
        self.assertEqual(T(good)['line'], 'elements')

        def at(part, i=0):
            return lambda r, **k: T(r)[part][i].update(**k)

        def line(name, **more):
            def change(r):
                for k in ('elements', 'builds', 'changed', 'unmoved'):
                    T(r).pop(k, None)
                T(r).update(line=name, **more)
            return change
        cases = [
            ("'not run' where the character set check compared",
             lambda r: r['step2'].update(transliteration={
                 'state': 'not run', 'why': 'x'})),
            ("'not reported', step2.character_sets", lambda r: r[
                'step2'].update(transliteration=rd.not_reported('x'))),
            ('with 1 charmap', lambda r: r['step2'].update(
                transliteration={'state': 'no change'})),
            ('has the keys', lambda r: T(r).update(unmoved=1)),
            ('line is', lambda r: T(r).update(line='other')),
            ('element.*of .* build', lambda r: T(r).update(builds=6)),
            ('element.*of .* build', lambda r: T(r).update(builds=4)),
            ('element.*of .* build', lambda r: T(r).update(changed=6)),
            ('its list', lambda r: T(r).update(changed=2)),
            ('its list', lambda r: T(r).update(changed=4)),
            ('builds is', lambda r: T(r).update(builds=0)),
            ('built differently with the line', line('unmoved',
                                                     unmoved=1)),
            ('unmoved is', lambda r: (T(r).update(listed=[]),
                                      line('unmoved', unmoved=4)(r))),
            ("the line 'none'", lambda r: (T(r).update(listed=[]),
                                           line('none')(r))),
            ("the line 'listed already'", lambda r: (
                T(r).update(listed=[], unsettled=[]),
                r['warnings']['step2']['items'].clear(),
                line('listed already')(r))),
            ('no line, and no build', lambda r: (
                T(r).update(listed=[], unsettled=[]),
                r['warnings']['step2']['items'].clear(),
                line('no line')(r))),
            ('skipped is not a list', lambda r: T(r).update(skipped=1)),
            # Each bound alone.
            ('element.*of .* build', lambda r: (
                at('listed')(r, count=1), T(r).update(elements=1, changed=1))),
            ('element.*of .* build', lambda r: (
                T(r).update(builds=1, changed=5), T(r)['skipped'].append(
                    {'entry': 'or_IN.UTF-8', 'source': 'or_IN',
                     'old': 'UTF-8', 'new': 'UTF-8',
                     'state': 'built differently', 'count': 2}))),
            ('left to the list, of 3 build', lambda r: (
                line('listed already')(r),
                T(r).update(listed=[], unsettled=[], skipped=[
                    {'entry': e, 'source': 'or_IN', 'old': 'UTF-8',
                     'new': 'UTF-8', 'state': 'not compared', 'why': 'w'}
                    for e in ('or_IN', 'or_IN.UTF-8', 'or_IN.GB18030',
                              'or_IN.ISO-8859-1')]),
                r['warnings']['step2']['items'].clear())),
            ('the `!!`', lambda r: (
                r['step2']['character_sets'].update(changed=[]),
                r['step2'].update(transliteration={'state': 'no change'}))),
            ('listed by nothing else', lambda r: (
                T(r).update(unsettled=[]),
                r['warnings']['step2']['items'].clear(),
                T(r)['skipped'].append(
                    {'entry': 'sv_SE.UTF-8', 'source': 'sv_SE',
                     'old': 'UTF-8', 'new': 'UTF-8', 'state': 'not compared',
                     'why': 'w'}))),
            ('its list and those left', lambda r: T(r)['skipped'].append(
                {'entry': 'or_IN.UTF-8', 'source': 'or_IN', 'old': 'UTF-8',
                 'new': 'UTF-8', 'state': 'built differently', 'count': 2})),
            ('element.*of .* build', lambda r: T(r)['skipped'].append(
                {'entry': 'or_IN.UTF-8', 'source': 'or_IN', 'old': 'UTF-8',
                 'new': 'UTF-8', 'state': 'not compared', 'why': 'w'})),
            ('built differently with the line', lambda r: (
                T(r).update(listed=[], changed=0),
                T(r)['skipped'].append(
                    {'entry': 'or_IN.UTF-8', 'source': 'or_IN',
                     'old': 'UTF-8', 'new': 'UTF-8',
                     'state': 'built differently', 'count': 3}),
                line('unmoved', unmoved=1)(r))),
            ('skipped.*count is', lambda r: T(r)['skipped'].append(
                {'entry': 'or_IN.UTF-8', 'source': 'or_IN', 'old': 'UTF-8',
                 'new': 'UTF-8', 'state': 'built differently', 'count': 0})),
            ('unsettled .*the `!!`', lambda r: at('unsettled')(
                r, old='GB18030', new='GB18030')),
            ('unsettled .*the `!!`', lambda r: at('unsettled')(r, why='x')),
            ('built from', lambda r: at('listed')(r, source='zz')),
            ('built from', lambda r: at('unsettled')(r, entry='zz.UTF-8')),
            ('not among the charmaps', lambda r: at('listed')(
                r, old='KOI8-R')),
            ('count is', lambda r: at('listed')(r, count=0)),
            ('codepoints is', lambda r: at('listed')(r, codepoints=[2, 1])),
            ('via is', lambda r: at('listed')(r, via=[])),
            ('among the names whose rules', lambda r: at('listed')(
                r, entry='or_IN.UTF-8', source='or_IN')),
            ("not in step 2's list", lambda r: at('listed')(
                r, entry='zz_ZZ.UTF-8', source='zz_ZZ')),
            ('which the character set check lists', lambda r: r['step2'][
                'character_sets']['listed'].append(
                    {'entry': 'sv_SE.UTF-8', 'source': 'sv_SE',
                     'old': 'UTF-8', 'new': 'UTF-8', 'codepoints': [65],
                     'via': ['sv_SE']})),
        ]
        controls = [
            # Some built differently in builds step 2 lists already.
            lambda r: (T(r).update(changed=5), T(r)['skipped'].append(
                {'entry': 'or_IN.UTF-8', 'source': 'or_IN', 'old': 'UTF-8',
                 'new': 'UTF-8', 'state': 'built differently', 'count': 2})),
        ]
        self.check(good, cases, controls)

    def test_on_two_machines(self):
        locales = dict(MACHINES['locales'],
                       sv_SE=['sv_SE', 'same', '', 'transliteration', 'sv_SE'])
        locales['C.utf8'] = ['C', 'same', '', 'transliteration not compared',
                             'C']
        self.files(**dict(MACHINES, locales=locales))
        step11_files(self.d)
        warnings_file(self.d, 0, [])
        warnings_file(self.d, 1, [])
        warnings_file(self.d, 11, ['same_build', 'other_encoding',
                                   'closing'])
        good = self.written(**ON_MACHINES)
        bn = lambda r: r['step2']['rules_by_name']['locales']['by_name']
        cases = [
            ("rules by name", lambda r: bn(r)['sv_SE'].update(charmap='-')),
            ("rules by name", lambda r: bn(r)['sv_SE'].update(
                charmap='transliteration not compared')),
            ("rules by name", lambda r: bn(r)['C.utf8'].update(charmap='-')),
            # What its rows found, no more and no less.
            ('charmap is', lambda r: bn(r)['sv_SE'].update(
                charmap='changed')),
            ('charmap is', lambda r: bn(r)['C.utf8'].update(
                charmap='not compared')),
            ('built into glibc', lambda r: bn(r)['C'].update(
                charmap='transliteration not compared')),
            ('built into glibc', lambda r: bn(r)['C'].update(source='C')),
            ('built into glibc', lambda r: bn(r)['C'].update(key='C')),
            # A name compare_rules does not have goes to its file.
            ('the key', lambda r: bn(r)['C.utf8'].update(source='D')),
            ('source is', lambda r: bn(r)['sv_SE'].update(source='none')),
            ('source is', lambda r: bn(r)['sv_SE'].update(source=5)),
            # A name of a file the check lists, not marked.
            ('charmap is', lambda r: bn(r).update(zz=dict(
                self.zz('-'), source='sv_SE'))),
            # Not compared, with no row behind it and no `!!` naming it.
            ('charmap is', lambda r: bn(r).update(zz=self.zz('not compared'))),
            # Named by the `!!` of names not traced, yet not marked.
            ('charmap is', lambda r: (
                bn(r).update(zz=self.zz('-')), self.untraced(r, 'zz'))),
            # The strongest of two rows of one file: transliteration over
            # not compared (collation_keys.LISTED_STATES).
            ('charmap is', lambda r: self.two_rows(r, 'not compared')),
            # With step 2's `!!` not reported, a mark with no row is still
            # refused where it is not 'not compared'.
            ('charmap is', lambda r: (
                r['warnings'].update(step2=rd.not_reported('x')),
                bn(r).update(zz=self.zz('changed')))),
        ]
        controls = [
            # An alias of a file the check lists carries what it found.
            lambda r: bn(r).update(swedish=dict(bn(r)['sv_SE'],
                                                key='swedish')),
            # The key first: sv_SE's own rows, whatever its source holds.
            lambda r: bn(r)['sv_SE'].update(source='C'),
            # A name the `!!` says was not traced is not compared.
            lambda r: (bn(r).update(zz=self.zz('not compared')),
                       self.untraced(r, 'zz')),
            lambda r: self.two_rows(r, 'transliteration'),
            # Of C's file, a charmap not settled (Cx) and transliterations
            # not compared (C.utf8): the stronger marks both.
            self.c_unsettled,
            # or_IN's transliterations, left to step 2's list: its one row.
            self.or_in_skipped,
            # With step 2's `!!` not reported, whether a name was traced is
            # not known: its 'not compared' is not checked.
            lambda r: (r['warnings'].update(step2=rd.not_reported('x')),
                       bn(r).update(zz=self.zz('not compared'))),
        ]
        self.check(good, cases, controls)

    @staticmethod
    def zz(charmap):
        """A name measured on a machine, built from a file of its own."""
        return {'key': 'zz', 'rules': 'same', 'why': '', 'charmap': charmap,
                'source': 'zz'}

    @staticmethod
    def untraced(r, name):
        r['warnings']['step2']['items'].append(rd.warning_item(
            'x', 'names_untraced',
            {'machine': 'glibc-2.28', 'names': [{'name': name, 'why': 'w'}]},
            '!! names_untraced'))

    @staticmethod
    def two_rows(r, charmap):
        """sv_SE.utf8, a second build of sv_SE, whose charmap the check
        could not settle, beside sv_SE's transliteration; the name is
        marked CHARMAP."""
        r['step2']['character_sets']['unsettled'].append(
            {'entry': 'sv_SE.utf8', 'source': 'sv_SE', 'old': 'UTF-8',
             'new': 'UTF-8', 'why': 'w'})
        r['warnings']['step2']['items'].append(rd.warning_item(
            'x', 'charmap_builds_unsettled',
            {'builds': [{'build': 'sv_SE.utf8', 'charmap': 'UTF-8',
                         'why': 'w'}]}, '!! charmap_builds_unsettled'))
        r['step2']['rules_by_name']['locales']['by_name']['sv_SE.utf8'] = {
            'key': 'sv_SE', 'rules': 'same', 'why': '', 'charmap': charmap,
            'source': 'sv_SE'}

    @staticmethod
    def c_unsettled(r):
        r['step2']['character_sets']['unsettled'].append(
            {'entry': 'Cx', 'source': 'C', 'old': 'UTF-8', 'new': 'UTF-8',
             'why': 'w'})
        r['warnings']['step2']['items'].append(rd.warning_item(
            'x', 'charmap_builds_unsettled',
            {'builds': [{'build': 'Cx', 'charmap': 'UTF-8', 'why': 'w'}]},
            '!! charmap_builds_unsettled'))
        bn = r['step2']['rules_by_name']['locales']['by_name']
        bn['Cx'] = dict(bn['C.utf8'], charmap='not compared')
        bn['C.utf8']['charmap'] = 'not compared'

    @staticmethod
    def or_in_skipped(r):
        r['step2']['character_sets']['builds'] = 4
        r['step2']['transliteration']['skipped'].append(
            {'entry': 'or_IN', 'source': 'or_IN', 'old': 'UTF-8',
             'new': 'UTF-8', 'state': 'not compared', 'why': 'w'})
        r['step2']['rules_by_name']['locales']['by_name']['or_IN'] = {
            'key': 'or_IN', 'rules': 'differs', 'why': '',
            'charmap': 'transliteration not compared', 'source': 'or_IN'}

    def test_each_name_has_five_fields(self):
        """step2_rules_by_name read as step 2 writes it: a name with four
        fields, or six, is refused, never cut by zip; and the strongest
        of what the check found is in collation_keys' order."""
        import collation_keys as ck
        self.assertEqual(rd.LISTED_STATES, ck.LISTED_STATES)
        for v in (['sv_SE', 'same', '', '-'],
                  ['sv_SE', 'same', '', '-', 'sv_SE', 'x']):
            with self.subTest(v=v):
                step2_files(self.d, **dict(MACHINES, locales={'sv_SE': v}))
                with self.assertRaisesRegex(rd.ReportError,
                                            'the rules of .sv_SE. are'):
                    rd.rules_by_name(os.path.join(
                        self.d, f'step2_rules_by_name.{PAIR}.json'))

    def test_where_the_check_did_not_run(self):
        step2_files(self.d, charmaps={'state': 'not run', 'why': 'no file'})
        warnings_file(self.d, 1, [])
        write(self.d, f'step2.{PAIR}.warnings.jsonl', json.dumps(
            {'kind': 'charmaps_not_run', 'text': '!! charmaps_not_run',
             'data': {'why': 'no file'}}) + '\n')
        good = self.written()
        self.assertEqual(self.T(good), {'state': 'not run', 'why': 'no file'})
        T = self.T
        cases = [
            ('step2.character_sets', lambda r: T(r).update(why='other')),
            ('step2.character_sets', lambda r: r['step2'].update(
                transliteration={'state': 'no change'})),
            ('with no build compared', lambda r: r['warnings']['step2'][
                'items'].append(rd.warning_item(
                    'x', 'translit_unsettled',
                    {'builds': [{'build': 'a', 'charmap': 'b', 'why': 'c'}]},
                    '!! translit_unsettled'))),
        ]
        self.check(good, cases)

    def test_the_states_are_the_records(self):
        """The states, lines and keys the reader takes are those
        translit_record writes, for every one it writes (each of its five
        lines, a build left to the list built differently and one not
        compared, nothing changed, the check not run for its two
        reasons), and, where the check compared nothing, step 2's own
        states of it (locale_order.CHARMAPS_STATES)."""
        import collation_keys as ck
        import locale_order as lo
        import node_charmaps as nc
        P = ('UTF-8', 'UTF-8')
        a = ck.Translit('aa', 'aa.UTF-8', P, 2, {65}, ['aa'], None)
        u = ck.Translit('bb', 'bb.UTF-8', P, 0, set(), [], 'w')
        report = ck.Report(None, 3, ['UTF-8'], [P], {P: (1, 1, 0)}, [], [],
                           [], [], ck.TranslitReport(4, 2, 0, [a], 2),
                           [a, u])
        seen = set()
        for t in (ck.TranslitReport(4, 2, 0, [a], 2),
                  ck.TranslitReport(0, 0, 1, [], 0),
                  ck.TranslitReport(0, 0, 0, [], 0),
                  ck.TranslitReport(0, 0, 0, [u], 0)):
            for skipped in ([], [a, u]):
                rec = nc.translit_record(report._replace(
                    translit=t, translit_skipped=skipped), 't1', 't2')
                seen.add(rec['line'])
                self.assertEqual(
                    sorted(list(rec) + ['step']),
                    sorted(rd.TR_KEYS['compared'] + rd.TR_LINES[rec['line']]))
                if skipped:
                    self.assertEqual(
                        [x['state'] for x in rec['skipped']],
                        list(rd.TR_SKIPPED_STATES))
        self.assertEqual(seen, set(nc.TRANSLIT_LINES))
        self.assertEqual(tuple(rd.TR_LINES), nc.TRANSLIT_LINES)
        rec = nc.translit_record(report._replace(translit=None), 't1', 't2')
        self.assertEqual(sorted(list(rec) + ['step']),
                         sorted(rd.TR_KEYS['no change']))
        for r in (report._replace(not_run='w'), report._replace(builds=0)):
            got = nc.translit_record(r, 't1', 't2')
            self.assertEqual(sorted(list(got) + ['step']),
                             sorted(rd.TR_KEYS['not run']))
            self.assertEqual(got['state'], 'not run')
        self.assertEqual(set(rd.TR_KEYS) - {'no change'},
                         set(lo.CHARMAPS_STATES))
        self.assertEqual(
            set(rd.TR_STATES),
            {'reported' if s == 'compared' else s for s in rd.TR_KEYS})


class StepTwosCharacterSetsHangTogether(_Folder):
    """step2.character_sets (step 2's step2_character_sets), each fact
    written wrong alone, refused by load() for the reason named, on two
    tags, on two machines and where the check did not run; and the
    controls that a right change is read."""

    read = StepTwosRulesHangTogether.read
    check = StepTwosRulesHangTogether.check

    def cs(self, **over):
        """A record of the check that compared: UTF-8 changed and
        compared, GB18030 changed and not compared; sv_SE listed, or_IN
        (whose rules differ) listed anyway, C not settled in GB18030."""
        rec = {'step': 2, 'state': 'compared', 'basis': 'SUPPORTED',
               'builds': 3, 'charmaps': ['GB18030', 'UTF-8'],
               'changed': [
                   {'old': 'UTF-8', 'new': 'UTF-8', 'state': 'compared',
                    'added': 2, 'removed': 0, 'other': 0},
                   {'old': 'GB18030', 'new': 'GB18030',
                    'state': 'not compared'}],
               'listed': [{'entry': 'sv_SE.UTF-8', 'source': 'sv_SE',
                           'old': 'UTF-8', 'new': 'UTF-8',
                           'codepoints': [65], 'via': ['sv_SE']}],
               'anyway': [{'entry': 'or_IN', 'source': 'or_IN',
                           'old': 'UTF-8', 'new': 'UTF-8', 'state': 'named',
                           'count': 2, 'via': ['or_IN']}],
               'unsettled': [{'entry': 'C.GB18030', 'source': 'C',
                              'old': 'GB18030', 'new': 'GB18030',
                              'why': 'bad gb'}],
               'distro': [], 'unsearched': [], 'not_compared': []}
        rec.update(over)
        write(self.d, f'step2_character_sets.{PAIR}.json',
              json.dumps(rec) + '\n')
        write(self.d, f'step2.{PAIR}.warnings.jsonl', json.dumps(
            {'kind': 'charmap_builds_unsettled',
             'text': '!! charmap_builds_unsettled',
             'data': {'builds': [{'build': 'C.GB18030',
                                  'charmap': 'GB18030', 'why': 'bad gb'}]}})
            + '\n')

    @staticmethod
    def C(r):
        return r['step2']['character_sets']

    @staticmethod
    def warning(r, kind, data):
        r['warnings']['step2']['items'].append(
            rd.warning_item('x', kind, data, f'!! {kind}'))

    def test_on_two_tags(self):
        step2_files(self.d)
        warnings_file(self.d, 1, [])
        self.cs()
        good = self.written()
        self.assertEqual(self.C(good)['state'], 'reported')
        C, warning = self.C, self.warning

        def at(part, i=0):
            return lambda r, **k: C(r)[part][i].update(**k)

        def distro(r):
            C(r)['distro'].append(
                {'entry': '-', 'source': 'sv_SE', 'old': 'UTF-8',
                 'new': 'UTF-8', 'codepoints': [66], 'via': ['sv_SE']})
        cases = [
            ("rules by name", lambda r: C(r).update(
                state='not run', why='x', not_compared=[])),
            ('basis is', lambda r: C(r).update(basis='measured')),
            ('the charmaps', lambda r: C(r).update(
                charmaps=['UTF-8', 'GB18030'])),
            ('not among the charmaps', lambda r: at('changed')(
                r, old='KOI8-R', new='KOI8-R')),
            ('the pair twice', lambda r: C(r)['changed'].append(
                dict(C(r)['changed'][0]))),
            ('is no pair that changed', lambda r: at('listed')(
                r, new='GB18030')),
            ('not compared, its builds', lambda r: C(r)['changed'].__setitem__(
                0, {'old': 'UTF-8', 'new': 'UTF-8', 'state': 'not compared'})),
            ('not compared, its builds', lambda r: C(r)['anyway'].append(
                {'entry': 'or_IN.GB18030', 'source': 'or_IN',
                 'old': 'GB18030', 'new': 'GB18030', 'state': 'not compared',
                 'why': 'another reason'})),
            ('unsettled .*the `!!`', lambda r: at('unsettled')(r, why='x')),
            ('built from', lambda r: at('unsettled')(r, source='zz')),
            ('built from', lambda r: at('listed')(r, source='zz')),
            ('is not among the names whose rules', lambda r: at('anyway')(
                r, entry='sv_SE', source='sv_SE')),
            ('is among the names whose rules', lambda r: at('listed')(
                r, entry='or_IN', source='or_IN')),
            ("not in step 2's list", lambda r: at('listed')(
                r, entry='zz_ZZ', source='zz_ZZ')),
            (r'listed\[0\]: entry', lambda r: at('listed')(r, entry='-')),
            ('codepoints is', lambda r: at('listed')(r, codepoints=[66, 65])),
            ('count is', lambda r: at('anyway')(r, count=0)),
            ('distro .*the `!!`', distro),
            ('unsearched .*the `!!`', lambda r: C(r)['unsearched'].append(
                {'charmap': 'UTF-8', 'why': 'w'})),
            ('unsearched', lambda r: (
                C(r)['unsearched'].append({'charmap': 'KOI8-R', 'why': 'w'}),
                warning(r, 'charmap_search_not_run',
                        {'charmaps': [{'charmap': 'KOI8-R', 'why': 'w'}]}))),
            # UTF-8 was compared: the search ran for it.
            ('its pair', lambda r: (
                C(r)['unsearched'].append({'charmap': 'UTF-8', 'why': 'w'}),
                warning(r, 'charmap_search_not_run',
                        {'charmaps': [{'charmap': 'UTF-8', 'why': 'w'}]}))),
            # KOI8-R compared, its one build not compared for the same
            # reason as the search.
            ('its pair', lambda r: (
                C(r).update(charmaps=['GB18030', 'KOI8-R', 'UTF-8']),
                C(r)['changed'].append(
                    {'old': 'KOI8-R', 'new': 'KOI8-R', 'state': 'compared',
                     'added': 1, 'removed': 0, 'other': 0}),
                C(r)['anyway'].append(
                    {'entry': 'or_IN.KOI8-R', 'source': 'or_IN',
                     'old': 'KOI8-R', 'new': 'KOI8-R',
                     'state': 'not compared', 'why': 'w'}),
                C(r)['unsearched'].append({'charmap': 'KOI8-R', 'why': 'w'}),
                warning(r, 'charmap_search_not_run',
                        {'charmaps': [{'charmap': 'KOI8-R', 'why': 'w'}]}))),
            # GB18030, for a reason its builds do not give.
            ('its pair', lambda r: (
                C(r)['unsearched'].append({'charmap': 'GB18030',
                                           'why': 'other'}),
                warning(r, 'charmap_search_not_run',
                        {'charmaps': [{'charmap': 'GB18030',
                                       'why': 'other'}]}))),
            # A distro row of a pair whose charmap changes name.
            (r'distro\[0\]: UTF-8 -> GB18030', lambda r: (
                C(r)['changed'].append(
                    {'old': 'UTF-8', 'new': 'GB18030', 'state': 'compared',
                     'added': 1, 'removed': 0, 'other': 0}),
                C(r)['distro'].append(
                    {'entry': '-', 'source': 'sv_SE', 'old': 'UTF-8',
                     'new': 'GB18030', 'codepoints': [66],
                     'via': ['sv_SE']}),
                warning(r, 'charmap_distro_buildable',
                        {'locales': [{'source': 'sv_SE',
                                      'charmap': 'GB18030'}]}))),
            # More characters than changed in the pair.
            ('more than changed', lambda r: at('changed')(r, added=1)),
            ('more than changed', lambda r: at('anyway')(r, count=3)),
            ('not_compared .*the `!!`', lambda r: C(r)['not_compared'].append(
                'zz.UTF-8')),
            ('the `!!` of the check not run', lambda r: warning(
                r, 'charmaps_not_run', {'why': 'x'})),
            ('the `!!` of entries not compared', lambda r: (
                C(r)['not_compared'].append('zz.UTF-8'),
                warning(r, 'charmap_sources_missing',
                        {'entries': ['zz.UTF-8'], 'basis': 'measured'}))),
            # Step 2's rules by name says not run, the check's record
            # compared.
            ("rules by name", lambda r: r['step2']['rules_by_name'].update(
                charmaps={'state': 'not run', 'why': 'x'})),
            # No rules by name: the run's own answer that step 2 compared
            # something.
            ('where step 2 compared something', lambda r: (
                r['step2'].update(rules_by_name={'state': 'not reported',
                                                 'why': 'x'}),
                C(r).clear(), C(r).update(state='one commit'))),
            # GB18030, not compared, with no build and no `!!` left.
            ('not compared, its builds', lambda r: (
                C(r)['unsettled'].clear(),
                r['warnings']['step2']['items'].clear())),
        ]
        self.check(good, cases, controls=[
            # A locale a distro can build, with its `!!`.
            lambda r: (distro(r), warning(
                r, 'charmap_distro_buildable',
                {'locales': [{'source': 'sv_SE', 'charmap': 'UTF-8'}]})),
            # The search not run for GB18030, not compared, for the
            # reason its builds give.
            lambda r: (C(r)['unsearched'].append(
                {'charmap': 'GB18030', 'why': 'bad gb'}), warning(
                r, 'charmap_search_not_run',
                {'charmaps': [{'charmap': 'GB18030', 'why': 'bad gb'}]})),
            # Entries not compared, with their `!!`.
            lambda r: (C(r)['not_compared'].append('zz.UTF-8'), warning(
                r, 'charmap_sources_missing',
                {'entries': ['zz.UTF-8'], 'basis': 'SUPPORTED'})),
            # A source no SUPPORTED entry builds in the charmap, listed
            # anyway and not settled by the check.
            lambda r: C(r)['anyway'].append(
                {'entry': '-', 'source': 'or_IN', 'old': 'UTF-8',
                 'new': 'UTF-8', 'state': 'not compared', 'why': 'w'}),
        ])

    def test_on_two_machines(self):
        step2_files(self.d, **dict(MACHINES, locales={
            'C': [None, 'built in', 'built into glibc, no locale file', '-',
                  None],
            'sv_SE': ['sv_SE', 'differs', '', 'changed', 'sv_SE']}))
        step11_files(self.d)
        for step in (0, 1):
            warnings_file(self.d, step, [])
        warnings_file(self.d, 11, ['same_build', 'other_encoding',
                                   'closing'])
        anyway = {'entry': 'sv_SE', 'source': 'sv_SE', 'old': 'UTF-8',
                  'new': 'UTF-8', 'state': 'named', 'count': 2,
                  'via': ['sv_SE']}
        self.cs(basis='measured', changed=[
            {'old': 'UTF-8', 'new': 'UTF-8', 'state': 'compared',
             'added': 2, 'removed': 0, 'other': 0}],
                listed=[], anyway=[anyway], unsettled=[])
        write(self.d, f'step2.{PAIR}.warnings.jsonl', '')
        # sv_SE's rules differ: the rules' record names it.
        with open(os.path.join(self.d, f'step2_rules.{PAIR}.json'),
                  encoding='utf-8') as f:
            rules = json.load(f)
        rules['differ'][0]['names'].append('sv_SE')
        rules['totals']['differ'] += 1
        rules['totals']['names'] += 1
        write(self.d, f'step2_rules.{PAIR}.json', json.dumps(rules) + '\n')
        with open(os.path.join(self.d, f'step2_rules_characters.{PAIR}.tsv'),
                  'a', encoding='utf-8') as f:
            f.write('sv_SE\tdiffers\tyes\tU+0000 U+0001\n')
        good = self.written(**ON_MACHINES)
        C = self.C
        rb = lambda r: r['step2']['rules_by_name']['locales']['by_name']  # noqa: E731
        self.check(good, [
            ('basis is', lambda r: C(r).update(basis='SUPPORTED')),
            ("in step 2's rules by name", lambda r: rb(r)['sv_SE'].update(
                charmap='-')),
            ("in step 2's rules by name", lambda r: rb(r)['sv_SE'].update(
                charmap='not compared')),
            ("in step 2's rules by name", lambda r: C(r)['anyway'][0].update(
                entry='zz_ZZ')),
            # A locale a distro can build, which two machines never
            # search for.
            ('point 5', lambda r: (C(r)['distro'].append(
                {'entry': '-', 'source': 'sv_SE', 'old': 'UTF-8',
                 'new': 'UTF-8', 'codepoints': [65], 'via': ['sv_SE']}),
                self.warning(r, 'charmap_distro_buildable',
                             {'locales': [{'source': 'sv_SE',
                                           'charmap': 'UTF-8'}]}))),
        ], controls=[
            # A build listed anyway that was not settled: its file is
            # 'not compared' in step 2's rules by name, or stronger.
            lambda r: (C(r)['anyway'].__setitem__(0, dict(
                {k: v for k, v in anyway.items()
                 if k not in ('count', 'via')},
                state='not compared', why='w')),
                rb(r)['sv_SE'].update(charmap='not compared')),
        ])

    def test_where_the_check_did_not_run(self):
        step2_files(self.d, charmaps={'state': 'not run', 'why': 'no builds'})
        warnings_file(self.d, 1, [])
        write(self.d, f'step2_character_sets.{PAIR}.json', json.dumps(
            {'step': 2, 'state': 'not run', 'why': 'no builds',
             'not_compared': ['zz.UTF-8']}) + '\n')
        write(self.d, f'step2.{PAIR}.warnings.jsonl', ''.join(
            json.dumps({'kind': k, 'text': f'!! {k}', 'data': d}) + '\n'
            for k, d in (('charmaps_not_run', {'why': 'no builds'}),
                         ('charmap_sources_missing',
                          {'entries': ['zz.UTF-8'], 'basis': 'SUPPORTED'}))))
        good = self.written()
        self.assertEqual(self.C(good), {'state': 'not run', 'why': 'no builds',
                                        'not_compared': ['zz.UTF-8']})
        self.check(good, [
            ("rules by name", lambda r: self.C(r).update(why='x')),
            ('not_compared .*the `!!`', lambda r: self.C(r).update(
                not_compared=[])),
            ('the `!!` of the check not run',
             lambda r: r['warnings']['step2']['items'].pop(0)),
        ])

    def test_a_reason_that_names_a_build(self):
        """A NOT RUN whose reason names a build side by its commit (step
        2 reads a build by its commit) is written by the build's name in
        both records, as the screen prints it, and the two agree
        (false-negative-reviewer, final review of 24.3b-1)."""
        import filter_lc_collate_changes as f
        import glibc_locale_data as g
        sha, name = 'a' * 40, 'glibc-2.34-100.el9'
        why = f"{g.SUPPORTED} does not exist at {sha}"
        with mock.patch.dict(g._NAMES, {sha: name}), \
                mock.patch.object(g, 'OUT_DIR', self.d):
            f.write_rules_by_name('p', 'builds', ('o', 'n'), False,
                                  {'state': 'not run', 'why': why})
            f.write_character_sets('p', {'state': 'not run', 'why': why,
                                         'not_compared': []})
        rb = rd.rules_by_name(os.path.join(self.d,
                                           'step2_rules_by_name.p.json'))
        cs = rd.step2_character_sets(
            os.path.join(self.d, 'step2_character_sets.p.json'))
        named = why.replace(sha, name)
        self.assertEqual((rb['charmaps']['why'], cs['why']), (named, named))
        rd._character_sets('x', {'character_sets': cs, 'rules_by_name': rb},
                           True, {'step2': {'state': 'reported',
                                            'items': [{'kind':
                                                       'charmaps_not_run',
                                                       'data': {'why':
                                                                named}}]}})

    def test_the_states_are_the_records(self):
        """The states and keys the reader takes are those charmap_record
        writes, for every state it writes (a pair compared and one not, a
        build listed anyway with its characters and one not compared, the
        check run and not run, for its two reasons), and, where nothing
        was compared, step 2's own states of the check
        (locale_order.CHARMAPS_STATES)."""
        import collation_keys as ck
        import locale_order as lo
        import node_charmaps as nc
        P, Q = ('UTF-8', 'UTF-8'), ('GB18030', 'GB18030')
        report = ck.Report(
            None, 2, ['GB18030', 'UTF-8'], [P, Q], {P: (1, 1, 0), Q: None},
            [], [ck.Found('aa', 'aa.UTF-8', P, {65}, ['aa'], None),
                 ck.Found('bb', 'bb.GB18030', Q, set(), [], 'w')],
            [], [], None, [])
        rec = nc.charmap_record(report, 't1', 't2')
        self.assertEqual(sorted(x['state'] for x in rec['changed']),
                         sorted(rd.CS_PAIR_STATES))
        self.assertEqual(sorted(x['state'] for x in rec['anyway']),
                         sorted(rd.CS_ANYWAY_STATES))
        self.assertEqual(sorted(list(rec) + ['step']),
                         sorted(rd.CS_KEYS['compared']))
        self.assertIn(rec['basis'], rd.CS_BASES)
        for r in (report._replace(not_run='w'), report._replace(builds=0)):
            got = nc.charmap_record(r, 't1', 't2')
            self.assertEqual(sorted(list(got) + ['step']),
                             sorted(rd.CS_KEYS['not run']))
            self.assertEqual(got['state'], 'not run')
        self.assertEqual(
            tuple('reported' if s == 'compared' else s
                  for s in lo.CHARMAPS_STATES), rd.CS_STATES)
        self.assertEqual(set(rd.CS_KEYS), set(lo.CHARMAPS_STATES))


B8, B9 = 'glibc-2.28-251.el8_10.40', 'glibc-2.34-275.el9_8'
DETAIL = 'gpg is not installed; the signature was NOT checked'


def _items(*pairs):
    return {'state': 'reported', 'items': [
        {'kind': k, 'data': d, 'text': f'!! {k}'} for k, d in pairs]}


# A state no section of the data file has.
NOT_YET = {'state': 'not yet reported', 'why': 'x'}


def compared_run(machines=False):
    """The parts of a report _compared reads, as a real run of 2.28 ->
    2.34 has them: on the tags, or on the two machines' files."""
    sig = ('signatures_unchecked', {'refs': [
        {'ref': 'glibc-2.28', 'status': 'no-gpg'},
        {'ref': 'glibc-2.34', 'status': 'no-gpg'}]})
    tags = [{'ref': 'glibc-2.28', 'commit': '3c03baca37fd',
             'status': 'no-gpg', 'detail': DETAIL},
            {'ref': 'glibc-2.34', 'commit': 'ae37d06c7d12',
             'status': 'no-gpg', 'detail': DETAIL}]
    version = {'state': 'differ', 'old': '2.28', 'new': '2.34'}
    if not machines:
        return {
            'pair': {'old': 'glibc-2.28', 'new': 'glibc-2.34', 'slug': PAIR},
            'run': {'route': 'tags', 'old_class': 'tag', 'new_class': 'tag',
                    'nothing_compared': 'none',
                    'machines': {'state': 'not given', 'why': 'x'}},
            'warnings': {'step0': {'state': 'not run', 'why': 'x'},
                         'step1': _items(sig, ('sides_read_as_tags', {
                             'refs': ['glibc-2.28', 'glibc-2.34'],
                             'classes': ['tag', 'tag'], 'ask': 'build'}),
                             ('code_not_compared', {'step11': False}))},
            'compared': {'state': 'reported', 'tags': tags, 'machines': [],
                         'builds': {'state': 'none'},
                         'collation_version': version,
                         'latest': {'state': 'not asked'},
                         'note': {'state': 'not run', 'why': 'x'}}}
    machine = lambda b, sha, n: {
        'build': b, 'commit': sha[:12], 'tag': b[:10], 'word': b + '.x86_64',
        'measurement_sha256': sha, 'locales': n,
        'charmaps': {'state': 'counted', 'count': 233}}
    return {
        'pair': {'old': B8, 'new': B9, 'slug': PAIR},
        'run': {'route': 'builds', 'old_class': 'build',
                'nothing_compared': 'none',
                'new_class': 'build',
                'machines': {'state': 'given', 'old': '/a', 'new': '/b'}},
        'warnings': {'step0': _items(),
                     'step1': _items(sig, ('machine_not_checked', {}),
                                     ('code_not_compared', {'step11': True}))},
        'compared': {'state': 'reported', 'tags': tags,
                     'machines': [machine(B8, 'a' * 64, 355),
                                  machine(B9, 'b' * 64, 356)],
                     'builds': {'state': 'none'},
                     'collation_version': version,
                     'latest': {'state': 'not asked'},
                     'note': {'state': 'printed', 'builds': [B8, B9],
                              'given': ['glibc-2.28', 'glibc-2.34']}}}


R8, R9 = 'glibc-2.28-251.el8_10.40', 'glibc-2.34-275.el9_8'
ROCKY = 'https://dl.rockylinux.org/'


def build_entry(b, sha, listed=True):
    """A rebuilt build as glibc_locale_data.build_entry gives it, its
    files on Rocky's mirror (its SRPM in vault 9.3's listing when not
    `listed`)."""
    import build_sources as bs
    ver, rel = b[len('glibc-'):].split('-', 1)
    n = rel.split('.el')[1][:1] if '.el' in rel else '9'
    tree = 'vault/rocky/9.3' if not listed else f'pub/rocky/{n}'
    return {'build': b, 'commit': sha[:12], 'tag': 'glibc-' + ver,
            'files': [
                {'package': 'srpm', 'listed': listed, 'sha256': sha,
                 'url': f'{ROCKY}{tree}/BaseOS/source/tree/Packages/g/'
                        f'{b}.src.rpm'},
                {'package': 'glibc-locale-source', 'listed': True,
                 'sha256': 'c' * 64,
                 'url': f'{ROCKY}{tree}/AppStream/x86_64/os/Packages/g/'
                        f'glibc-locale-source-{ver}-{rel}.x86_64.rpm'},
                {'package': 'glibc-common', 'listed': True,
                 'sha256': 'd' * 64,
                 'url': f'{ROCKY}{tree}/BaseOS/x86_64/os/Packages/g/'
                        f'glibc-common-{ver}-{rel}.x86_64.rpm'}],
            'checked': [bs.TARBALL_IS_TAG % ('glibc-' + ver),
                        f'locale sources: 356, each the sha256 '
                        f'glibc-locale-source-{ver}-{rel} records'],
            'patches': {'applied': 21, 'of': 1109},
            'charmaps_unchecked': []}


def builds_run(old=R8, new=R9, latest=None):
    """The parts of a report _compared reads, as a real run of two builds
    rebuilt from their packages has them; `latest` the sides as typed,
    each latest-elN resolved to its side."""
    sig = ('signatures_unchecked', {'refs': [
        {'ref': 'glibc-2.28', 'status': 'no-gpg'},
        {'ref': 'glibc-2.34', 'status': 'no-gpg'}]})
    tags = [{'ref': 'glibc-2.28', 'commit': '3c03baca37fd',
             'status': 'no-gpg', 'detail': DETAIL},
            {'ref': 'glibc-2.34', 'commit': 'ae37d06c7d12',
             'status': 'no-gpg', 'detail': DETAIL}]
    r = {
        'pair': {'old': old, 'new': new, 'slug': PAIR},
        'run': {'route': 'builds', 'old_class': 'build',
                'new_class': 'build', 'nothing_compared': 'none',
                'machines': {'state': 'not given', 'why': 'x'}},
        'warnings': {'step0': {'state': 'not run', 'why': 'x'},
                     'step1': _items(sig, ('build_not_checked', {
                         'metadata': True, 'unlisted': []}),
                         ('code_not_compared', {'step11': False}))},
        'compared': {'state': 'reported', 'tags': tags, 'machines': [],
                     'builds': {'state': 'rebuilt', 'entries': [
                         build_entry(old, 'a' * 64),
                         build_entry(new, 'b' * 64)]},
                     'collation_version': {'state': 'differ', 'old': '2.28',
                                           'new': '2.34'},
                     'latest': {'state': 'not asked'},
                     'note': {'state': 'not run', 'why': 'x'}}}
    if latest:
        r['compared']['latest'] = {
            'state': 'reported', 'asked': list(latest),
            'resolved': [{'name': t, 'build': b,
                          'folder': f'{ROCKY}pub/rocky/{t[-1]}',
                          'when': {'state': 'dated',
                                   'utc': '2026-11-21 06:02 UTC'}}
                         for t, b in dict(
                             (t, b) for t, b in zip(latest, (old, new))
                             if t.startswith('latest-')).items()]}
    return r


class WhatWasComparedHangsTogether(unittest.TestCase):
    """compared in load(): the facts report_data._compared ties to others
    of the same run, a case for each tie that refuses one wrong value, and
    the forms of a real run no run of the suite prints."""

    def ok(self, r):
        rd._compared(r)

    def refused(self, r, message):
        with self.assertRaisesRegex(rd.ReportError, message):
            rd._compared(r)

    def test_a_real_run_of_each_kind(self):
        for machines in (False, True):
            with self.subTest(machines=machines):
                self.ok(compared_run(machines))

    def test_forms_no_run_of_the_suite_prints(self):
        """A good signature (gpg and the key there), a glibc version not
        read, two machines on one build, glibc of 32 and 64 bits on one
        machine, both sides given as their builds."""
        r = compared_run()
        r['compared']['tags'][0]['status'] = 'good'
        r['warnings']['step1']['items'][0]['data']['refs'].pop(0)
        self.ok(r)
        r = compared_run()
        r['compared']['tags'][1]['status'] = 'good'
        r['compared']['tags'][0]['status'] = 'good'
        del r['warnings']['step1']['items'][0]
        self.ok(r)
        r = compared_run()
        r['compared']['collation_version'] = {'state': 'unknown',
                                              'sides': ['glibc-2.28']}
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_unknown',
             'data': {'sides': ['glibc-2.28']}, 'text': '!! x'})
        self.ok(r)
        r = compared_run(True)
        r['pair']['new'] = B8
        r['compared']['machines'][1] = dict(r['compared']['machines'][0])
        r['compared']['tags'].pop()
        r['warnings']['step1']['items'][0]['data']['refs'].pop()
        r['compared']['collation_version'] = {'state': 'same',
                                              'version': '2.28'}
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same',
             'data': {'version': '2.28'}, 'text': '!! x'})
        r['compared']['note']['builds'] = [B8, B8]
        r['compared']['note']['given'] = ['glibc-2.28', 'glibc-2.28']
        self.ok(r)
        r = compared_run(True)
        r['compared']['machines'][1]['word'] = (B9 + '.x86_64 ' + B9
                                                + '.i686')
        self.ok(r)
        r = compared_run(True)
        r['compared']['note'] = {'state': 'not printed'}
        self.ok(r)

    def test_the_tags(self):
        def tags(f):
            r = compared_run()
            f(r['compared']['tags'])
            return r
        self.refused(tags(lambda t: t[0].update(ref='glibc-2.29')),
                     "not the sides'")
        self.refused(tags(lambda t: t.reverse()), "not the sides'")
        self.refused(tags(lambda t: t.pop()), "not the sides'")
        self.refused(tags(lambda t: t.append(dict(t[0]))), "not the sides'")
        self.refused(tags(lambda t: t[0].update(commit='3c03baca37f')),
                     r'compared\.tags\[0\] is')
        self.refused(tags(lambda t: t[0].update(status='bad')),
                     r'tags\[0\]\.status')
        self.refused(tags(lambda t: t[0].update(x=1)), 'has the keys')
        # A build side is read on its version's tag.
        r = compared_run(True)
        r['compared']['tags'][0]['ref'] = 'glibc-2.29'
        self.refused(r, "not the sides'")

    def test_the_signatures_not_checked_are_the_warnings(self):
        for status in ('good', 'no-key'):
            with self.subTest(status=status):
                r = compared_run()
                r['compared']['tags'][0]['status'] = status
                self.refused(r, 'the `!!` of signatures')
        # The `!!` is one block for every tag.
        r = compared_run()
        items = r['warnings']['step1']['items']
        items.insert(1, dict(items[0]))
        self.refused(r, 'the `!!` of signatures')

    def test_the_machines(self):
        def machines(f):
            r = compared_run(True)
            f(r['compared']['machines'])
            return r
        self.refused(machines(lambda m: m.__setitem__(0, dict(m[1]))),
                     'are not the build sides')
        self.refused(machines(lambda m: m[0].update(
            build='glibc-2.39-1.el10', tag='glibc-2.39',
            word='glibc-2.39-1.el10.x86_64')), 'are not the build sides')
        self.refused(machines(lambda m: m[0].update(tag='glibc-2.29')),
                     'is not that of')
        self.refused(machines(lambda m: m[0].update(word=B9 + '.x86_64')),
                     'word')
        self.refused(machines(lambda m: m[1].update(
            word=B9 + '.x86_64 glibc-2.34-283.el9_8.i686')), 'word')
        self.refused(machines(lambda m: m[0].update(word='glibc')), 'word')
        self.refused(machines(lambda m: m.pop()), 'are not the build sides')
        self.refused(machines(lambda m: m.reverse()),
                     'are not the build sides')
        self.refused(machines(lambda m: m.clear()),
                     'what no step reads of a machine')
        self.refused(machines(lambda m: m[0].update(
            measurement_sha256='x')), r'machines\[0\] is')
        self.refused(machines(lambda m: m[0].update(locales='355')),
                     r'machines\[0\] is')
        self.refused(machines(lambda m: m[0].update(
            charmaps={'state': 'none'})), 'charmaps has the keys')
        self.refused(machines(lambda m: m[0]['charmaps'].update(
            count='233')), 'charmaps is')
        self.refused(machines(lambda m: m[0].update(
            charmaps={'state': 'none', 'why': ['x']})), 'charmaps is')
        r = compared_run(True)
        r['warnings']['step1']['items'].append(
            {'kind': 'machine_entries_skipped',
             'data': {'machine': 'glibc-2.39-1.el10',
                      'entries': [{'entry': 'x', 'why': 'y'}]},
             'text': '!! x'})
        self.refused(r, 'entries skipped')
        # A build side that is neither a machine nor a rebuilt build.
        r = compared_run(True)
        r['compared']['machines'] = []
        r['warnings']['step1']['items'].pop(1)
        self.refused(r, 'neither machines nor rebuilt builds')

    def test_the_builds(self):
        self.ok(builds_run())

        def builds(f):
            r = builds_run()
            f(r['compared']['builds']['entries'])
            return r
        self.refused(builds(lambda e: e.reverse()), 'are not the build sides')
        self.refused(builds(lambda e: e.pop()), 'are not the build sides')
        self.refused(builds(lambda e: e.append(dict(e[0]))),
                     'are not the build sides')
        self.refused(builds(lambda e: e.clear()), 'compared.builds is')
        self.refused(builds(lambda e: e[0].update(tag='glibc-2.29')),
                     'is not that of')
        self.refused(builds(lambda e: e[1]['checked'].__setitem__(
            0, 'the SRPM\'s tarball is glibc-2.28 in localedata/ and '
               'locale/')), 'the tarball')
        self.refused(builds(lambda e: e[0]['checked'].clear()), 'is')
        self.refused(builds(lambda e: e[0].update(commit='4ad85b6ccbe')),
                     r'entries\[0\] is')
        self.refused(builds(lambda e: e[0]['patches'].update(applied=1110,
                                                             of=1109)),
                     'patches is')
        self.refused(builds(lambda e: e[0]['patches'].update(of=None)),
                     'patches is')
        self.refused(builds(lambda e: e[0]['files'].reverse()), 'files are')
        self.refused(builds(lambda e: e[0]['files'].pop()), 'files are')
        self.refused(builds(lambda e: e[0]['files'][0].update(sha256='x')),
                     r'files\[0\] is')
        self.refused(builds(lambda e: e[0]['files'][1].update(listed=False)),
                     r'files\[1\] is')
        self.refused(builds(lambda e: e[0]['files'][0].update(listed='no')),
                     r'files\[0\] is')
        self.refused(builds(lambda e: e[0]['files'][0].update(x=1)),
                     'has the keys')
        # What is not checked for builds, as its `!!` says it.
        r = builds_run()
        for e in r['compared']['builds']['entries']:
            e['files'] = []
        self.refused(r, 'what is not checked for builds')
        r = builds_run()
        r['compared']['builds']['entries'][1]['files'][0]['listed'] = False
        self.refused(r, 'what is not checked for builds')
        r = builds_run()
        r['warnings']['step1']['items'][1]['data']['unlisted'].append(
            {'build': R9, 'folder': ROCKY + 'vault/rocky/9.3'})
        self.refused(r, 'what is not checked for builds')
        r = builds_run(new='glibc-2.34-83.el9.12')
        r['compared']['builds']['entries'][1] = build_entry(
            'glibc-2.34-83.el9.12', 'b' * 64, listed=False)
        r['warnings']['step1']['items'][1]['data']['unlisted'].append(
            {'build': 'glibc-2.34-83.el9.12',
             'folder': ROCKY + 'vault/rocky/9.2'})
        self.refused(r, 'what is not checked for builds')
        r = builds_run()
        r['warnings']['step1']['items'].pop(1)
        self.refused(r, 'what is not checked for builds')
        r = builds_run()
        items = r['warnings']['step1']['items']
        items.insert(1, dict(items[1]))
        self.refused(r, 'what is not checked for builds')
        # The `!!` of charmaps not checked are the builds', in order.
        U = {'package': 'glibc-locale-source-2.34-275.el9_8',
             'why': 'no zstd reader'}

        def charmaps(warned, unchecked):
            r = builds_run()
            r['warnings']['step1']['items'] += [
                {'kind': 'build_charmaps_unchecked',
                 'data': dict(U, build=x), 'text': '!! x'} for x in warned]
            for i in unchecked:
                r['compared']['builds']['entries'][i][
                    'charmaps_unchecked'].append(dict(U))
            return r
        self.ok(charmaps([R9], [1]))
        self.ok(charmaps([R8, R9], [0, 1]))
        for warned, unchecked in (([R9], []), ([], [1]), ([R8], [1]),
                                  ([R9, R8], [0, 1]), ([R9, R9], [1]),
                                  (['glibc-2.39-1.el10'], [1])):
            with self.subTest(warned=warned, unchecked=unchecked):
                self.refused(charmaps(warned, unchecked),
                             'charmaps not checked')
        r = charmaps([R9], [1])
        r['compared']['builds']['entries'][1]['charmaps_unchecked'][0][
            'why'] = 'x'
        self.refused(r, 'charmaps not checked')
        r = charmaps([R9], [1])
        r['compared']['builds']['entries'][1]['charmaps_unchecked'][0][
            'x'] = 1
        self.refused(r, 'charmaps_unchecked is')
        r = compared_run()
        r['compared']['builds'] = {'state': 'rebuilt', 'entries': []}
        self.refused(r, 'compared.builds is')
        r = compared_run(True)
        r['compared']['builds'] = builds_run(B8, B9)['compared']['builds']
        r['warnings']['step1']['items'].append(
            {'kind': 'build_not_checked',
             'data': {'metadata': True, 'unlisted': []}, 'text': '!! x'})
        self.refused(r, 'are not the build sides')

    def test_builds_no_run_of_the_suite_prints(self):
        """A build whose SRPM no metadata lists (vault 9.3), its folder in
        the `!!`; one build as both sides, its charmaps not checked on
        each (an el9 build with no zstd reader, a `!!` a side); a Side
        made with no file fetched."""
        v = 'glibc-2.34-83.el9.12'
        r = builds_run(new=v)
        r['compared']['builds']['entries'][1] = build_entry(v, 'b' * 64,
                                                            listed=False)
        r['warnings']['step1']['items'][1]['data']['unlisted'] = [
            {'build': v, 'folder': ROCKY + 'vault/rocky/9.3'}]
        self.ok(r)
        r = builds_run(new=R8)
        r['compared']['builds']['entries'][1] = build_entry(R8, 'a' * 64)
        r['compared']['tags'].pop()
        r['warnings']['step1']['items'][0]['data']['refs'].pop()
        r['compared']['collation_version'] = {'state': 'same',
                                              'version': '2.28'}
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same', 'data': {'version': '2.28'},
             'text': '!! x'})
        for e in r['compared']['builds']['entries']:
            e['charmaps_unchecked'] = [{'package': 'p', 'why': 'w'}]
        r['warnings']['step1']['items'] += [
            {'kind': 'build_charmaps_unchecked',
             'data': {'build': R8, 'package': 'p', 'why': 'w'},
             'text': '!! x'}] * 2
        self.ok(r)
        r = builds_run()
        for e in r['compared']['builds']['entries']:
            e['files'] = []
        r['warnings']['step1']['items'][1]['data']['metadata'] = False
        self.ok(r)

    def test_latest(self):
        """Each latest-elN typed, once, resolved to its side's build; the
        date as latest_entry writes it from the metadata's timestamp."""
        import build_sources as bs
        import glibc_locale_data as g
        when = g.latest_entry('latest-el9', bs.Latest(
            R9, ROCKY, 'pub/rocky/9', '1762000000'))['when']
        self.assertEqual(when['state'], 'dated')
        self.assertTrue(rd.LATEST_WHEN.fullmatch(when['utc']), when)
        for typed in (('latest-el8', 'latest-el9'), (R8, 'latest-el9'),
                      ('latest-el8', R9), (R8 + '.x86_64', 'latest-el9')):
            with self.subTest(typed=typed):
                self.ok(builds_run(latest=typed))
        r = builds_run(new=R8, latest=('latest-el8', 'latest-el8'))
        r['compared']['builds']['entries'][1] = build_entry(R8, 'a' * 64)
        r['compared']['tags'].pop()
        r['warnings']['step1']['items'][0]['data']['refs'].pop()
        r['compared']['collation_version'] = {'state': 'same',
                                              'version': '2.28'}
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same', 'data': {'version': '2.28'},
             'text': '!! x'})
        self.assertEqual(len(r['compared']['latest']['resolved']), 1)
        self.ok(r)
        r = builds_run(latest=('latest-el8', 'latest-el9'))
        r['compared']['latest']['resolved'][0]['when'] = {'state': 'no date'}
        self.ok(r)
        # One N typed two ways: one entry, named as first typed.
        r = builds_run(new=R8, latest=('latest-el8', 'latest-el08'))
        r['compared']['builds']['entries'][1] = build_entry(R8, 'a' * 64)
        r['compared']['tags'].pop()
        r['warnings']['step1']['items'][0]['data']['refs'].pop()
        r['compared']['collation_version'] = {'state': 'same',
                                              'version': '2.28'}
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same', 'data': {'version': '2.28'},
             'text': '!! x'})
        r['compared']['latest']['resolved'].pop()
        self.ok(r)
        r['compared']['latest']['resolved'][0]['name'] = 'latest-el08'
        self.refused(r, 'the sides typed')

        def latest(f, typed=('latest-el8', 'latest-el9')):
            r = builds_run(latest=typed)
            f(r['compared']['latest'])
            return r
        self.refused(latest(lambda x: x['resolved'].reverse()),
                     'the sides typed')
        self.refused(latest(lambda x: x['resolved'].pop()), 'the sides typed')
        self.refused(latest(lambda x: x['resolved'][1].update(
            build='glibc-2.34-283.el9_8')), 'the sides typed')
        self.refused(latest(lambda x: x['resolved'][1].update(
            folder=ROCKY + 'pub/rocky/8')), r'resolved\[1\] is')
        self.refused(latest(lambda x: x['resolved'][1].update(
            name='latest-el10')), r'resolved\[1\] is')
        self.refused(latest(lambda x: x['resolved'][0]['when'].update(
            utc='2026-11-21')), 'when is')
        self.refused(latest(lambda x: x.update(asked=['latest-el8'])),
                     'asked is')
        self.refused(latest(lambda x: x.update(asked=[R8, R9])),
                     'no side typed')
        self.refused(latest(lambda x: x.update(asked=['glibc-2.28', R9]),
                            typed=(R8, 'latest-el9')), 'typed, the side')
        self.refused(latest(lambda x: x.update(asked=[
            'glibc-2.28-251.el8_10.41.x86_64', R9]),
            typed=(R8, 'latest-el9')), 'typed, the side')
        self.refused(latest(lambda x: x.update(state='not asked')),
                     'has the keys')
        # One N typed for two builds of that RHEL.
        r8b = 'glibc-2.28-251.el8_10.41'
        r = builds_run(new=r8b, latest=('latest-el8', 'latest-el8'))
        r['compared']['builds']['entries'][1] = build_entry(r8b, 'b' * 64)
        r['compared']['tags'].pop()
        r['warnings']['step1']['items'][0]['data']['refs'].pop()
        r['compared']['collation_version'] = {'state': 'same',
                                              'version': '2.28'}
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same', 'data': {'version': '2.28'},
             'text': '!! x'})
        r['compared']['latest']['resolved'] = [
            dict(r['compared']['latest']['resolved'][0], build=R8)]
        self.refused(r, "'latest-el8' is")
        # A latest-elN whose build is of another RHEL than N.
        r = builds_run(latest=(R8, 'latest-el8'))
        r['compared']['latest']['resolved'][0].update(
            folder=ROCKY + 'pub/rocky/8')
        self.refused(r, r'resolved\[0\] is')
        # A latest-elN resolved to a side no rebuilt build is (a machine's).
        r = compared_run(True)
        r['compared']['latest'] = {
            'state': 'reported', 'asked': ['latest-el8', B9],
            'resolved': [{'name': 'latest-el8', 'build': B8,
                          'folder': ROCKY + 'pub/rocky/8',
                          'when': {'state': 'no date'}}]}
        self.refused(r, 'not rebuilt')

    def test_the_glibc_versions(self):
        def version(v, *warned):
            r = compared_run(True)
            r['compared']['collation_version'] = v
            r['warnings']['step1']['items'] += [
                {'kind': k, 'data': d, 'text': '!! x'} for k, d in warned]
            return r
        self.refused(version({'state': 'differ', 'old': '2.29',
                              'new': '2.34'}), 'the build')
        self.refused(version({'state': 'differ', 'old': '2.34',
                              'new': '2.34'}), 'collation_version is')
        r = compared_run()
        r['compared']['collation_version']['old'] = 2.28
        self.refused(r, 'collation_version is')
        self.refused(version({'state': 'same', 'version': '2.34'},
                             ('collation_version_same', {'version': '2.34'})),
                     'the build')
        r = compared_run()
        r['compared']['collation_version'] = {'state': 'same',
                                              'version': '2.34'}
        self.refused(r, 'its `!!`')
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same', 'data': {'version': '2.28'},
             'text': '!! x'})
        self.refused(r, 'its `!!`')
        r = compared_run()
        r['compared']['collation_version'] = {'state': 'unknown',
                                              'sides': ['x']}
        self.refused(r, 'its `!!`')
        r = compared_run()
        r['warnings']['step1']['items'].append(
            {'kind': 'collation_version_same', 'data': {'version': '2.34'},
             'text': '!! x'})
        self.refused(r, 'its `!!`')

    def test_step_0s_note(self):
        def note(n, machines=True):
            r = compared_run(machines)
            r['compared']['note'] = n
            return r
        self.refused(note({'state': 'not run', 'why': 'x'}), 'machines')
        self.refused(note({'state': 'printed', 'builds': [B8, B9],
                           'given': ['glibc-2.28', 'glibc-2.34']}, False),
                     'machines')
        self.refused(note({'state': 'fallback'}),
                     'machines that read their tags')
        # A note of builds on a run whose sides are tags, where step 0's
        # `!!` (which a fallback writes) is not there to say why.
        r = compared_run()
        r['run']['machines'] = {'state': 'given', 'old': '/a', 'new': '/b'}
        r['warnings']['step0'] = {'state': 'not reported', 'why': 'x'}
        r['compared']['note'] = {'state': 'not printed'}
        self.refused(r, 'on sides')
        self.refused(note({'state': 'printed', 'builds': [B9, B8],
                           'given': ['glibc-2.28', 'glibc-2.34']}),
                     'note is')
        self.refused(note({'state': 'printed', 'builds': [B8, B9],
                           'given': ['glibc-2.34', 'glibc-2.28']}),
                     'note is')
        self.refused(note({'state': 'printed', 'builds': [B8, B9],
                           'given': [B8, B9]}), 'note is')
        r = compared_run(True)
        r['warnings']['step0'] = _items(('machine_falls_back_to_tags', {
            'unread': [{'side': 'old', 'build': B8, 'why': 'w'}],
            'tags': ['glibc-2.28', 'glibc-2.34']}))
        self.refused(r, 'machines that read their tags')


class WhatWasComparedAgainstTheText(unittest.TestCase):
    """_harness.compared_held on a run's text and data made here, with
    the machines' files given, for two notes no audit.sh run of the suite
    meets: 'not printed' (both sides typed as their builds), held; and
    'not reported', refused."""

    def text(self, r):
        import glibc_locale_data as g
        c, rule = r['compared'], '=' * 64
        return '\n'.join(
            ['', rule, '== WHAT WAS COMPARED', rule,
             "Machines' files, as given:", '  old  /a', '  new  /b']
            + g.tag_provenance_lines(c['tags'])
            + g.machine_provenance_lines(c['machines'])
            + g.collation_version_lines(c['collation_version'])
            + ['', rule, '== STEP 2  x', rule, ''])

    def test_the_note_not_printed_and_not_reported(self):
        r = compared_run(True)
        r['compared']['note'] = {'state': 'not printed'}
        _harness.compared_held(self.text(r), r, 'x')
        r['compared']['note'] = {'state': 'not reported', 'why': 'x'}
        with self.assertRaisesRegex(AssertionError, "step 0's note"):
            _harness.compared_held(self.text(r), r, 'x')


class BuildsAgainstTheText(unittest.TestCase):
    """_harness.compared_held on a run of two builds made here, for what
    no audit.sh run of the suite prints: a SRPM no metadata lists (vault
    9.3) and latest-elN whose metadata gives no date; held, and a line
    the data does not give, refused."""

    def text(self, r, before=None):
        import glibc_locale_data as g
        c, rule = r['compared'], '=' * 64
        if before is None:
            before = [x for e in c['latest'].get('resolved') or ()
                      for x in g.latest_lines(e)]
        return '\n'.join(
            before + ['', rule, '== WHAT WAS COMPARED', rule]
            + g.tag_provenance_lines(c['tags'])
            + g.build_provenance_lines(c['builds']['entries'])
            + g.collation_version_lines(c['collation_version'])
            + ['', rule, '== STEP 2  x', rule, ''])

    def test_held_and_refused(self):
        v = 'glibc-2.34-83.el9.12'
        r = builds_run(new=v, latest=('latest-el8', v))
        r['compared']['builds']['entries'][1] = build_entry(v, 'b' * 64,
                                                            listed=False)
        r['compared']['latest']['resolved'][0]['when'] = {'state': 'no date'}
        text = self.text(r)
        self.assertIn('no metadata gives this sha256', _harness.flat(text))
        self.assertIn('gives no date this reads', _harness.flat(text))
        _harness.compared_held(text, r, 'x')
        for before in ([], ['latest-el8 is x']):
            with self.subTest(before=before):
                with self.assertRaisesRegex(AssertionError, 'before'):
                    _harness.compared_held(self.text(r, before), r, 'x')
        bad = text.replace('no metadata gives this sha256', 'x', 1)
        with self.assertRaisesRegex(AssertionError, 'the block'):
            _harness.compared_held(bad, r, 'x')


class WhatWasComparedRead(_Folder):
    """compared as the files of a run give it: the sides' record, step 0's
    note beside it on the machines' files, and each machine's measurement
    against the file the run laid out."""

    def files(self, sha_old=None):
        import hashlib
        step2_files(self.d)
        run = compared_run(True)
        for side, m in zip(('old', 'new'), run['compared']['machines']):
            data = f'measurement of {side}'.encode()
            os.makedirs(os.path.join(self.d, f'node_file.{PAIR}.{side}'),
                        exist_ok=True)
            write(self.d, f'node_file.{PAIR}.{side}/locale_order.out',
                  data.decode())
            m['measurement_sha256'] = hashlib.sha256(data).hexdigest()
        if sha_old:
            run['compared']['machines'][0]['measurement_sha256'] = sha_old
        write(self.d, f'build_sides.{PAIR}.txt', ''.join(
            f"{m['build']}\t{m['commit']}{'0' * 28}\n"
            for m in run['compared']['machines']))
        record = {k: run['compared'][k] for k in
                  ('tags', 'machines', 'builds', 'collation_version')}
        write(self.d, f'step1_compared.{PAIR}.json',
              json.dumps(dict(record, step=1)) + '\n')
        write(self.d, f'step0.{PAIR}.json',
              json.dumps(dict(run['compared']['note'], step=0)) + '\n')
        return run

    def section(self):
        return rd.compared_section(args(self.d, old=B8, new=B9,
                                        old_node='/a', new_node='/b'))

    def test_the_record_and_the_note(self):
        run = self.files()
        got = self.section()
        self.assertEqual(got, run['compared'])

    def test_a_measurement_that_is_not_the_files(self):
        self.files(sha_old='c' * 64)
        with self.assertRaisesRegex(rd.ReportError, 'measurement'):
            self.section()

    def test_a_commit_that_is_not_the_one_recorded(self):
        """Each machine's commit is the one the sides recorded for its
        build (build_sides.<pair>.txt); a build they recorded none for,
        or a file of another shape, refused."""
        for text in (f'{B8}\t{"9" * 40}\n{B9}\t{"b" * 40}\n',
                     f'{B9}\t{"b" * 40}\n',
                     f'{B8} {"a" * 40}\n{B9}\t{"b" * 40}\n'):
            with self.subTest(text=text):
                self.files()
                write(self.d, f'build_sides.{PAIR}.txt', text)
                with self.assertRaisesRegex(rd.ReportError, 'commit|a line'):
                    self.section()

    def builds(self, latest=True):
        """A run of two builds rebuilt from their packages, as its files
        give it, latest-elN on its new side."""
        step2_files(self.d)
        run = builds_run(latest=(R8, 'latest-el9') if latest else None)
        run['compared']['note']['why'] = 'no --old-node and --new-node'
        es = run['compared']['builds']['entries']
        write(self.d, f'build_sides.{PAIR}.txt', ''.join(
            f"{e['build']}\t{e['commit']}{'0' * 28}\n" for e in es))
        record = {k: run['compared'][k] for k in
                  ('tags', 'machines', 'builds', 'collation_version')}
        write(self.d, f'step1_compared.{PAIR}.json',
              json.dumps(dict(record, step=1)) + '\n')
        if latest:
            write(self.d, f'latest.{PAIR}.json', json.dumps(
                {'step': 'latest',
                 'resolved': run['compared']['latest']['resolved']}) + '\n')
        return run

    def builds_section(self, asked=(R8, 'latest-el9')):
        return rd.compared_section(args(
            self.d, old=R8, new=R9, route='builds',
            old_asked=asked[0] if asked else '',
            new_asked=asked[1] if asked else ''))

    def test_the_builds_and_latest(self):
        run = self.builds()
        self.assertEqual(self.builds_section(), run['compared'])
        os.remove(os.path.join(self.d, f'latest.{PAIR}.json'))
        self.assertEqual(self.builds_section()['latest']['state'],
                         'not reported')
        run = self.builds(latest=False)
        self.assertEqual(self.builds_section(None), run['compared'])
        for text, message in (
                (f'{R8}\t{"9" * 40}\n{R9}\t{"b" * 40}\n', 'a build'),
                (f'{R9}\t{"b" * 40}\n', 'a build')):
            with self.subTest(text=text):
                self.builds()
                write(self.d, f'build_sides.{PAIR}.txt', text)
                with self.assertRaisesRegex(rd.ReportError, message):
                    self.builds_section()
        good = json.dumps({'step': 'latest', 'resolved': []})
        for text, message in (
                ('{"step": 0, "resolved": []}\n', 'not what latest writes'),
                ('{"step": "latest", "resolved": [], "x": 1}\n',
                 'not what latest writes'),
                (good + '\n{}\n', 'not one line')):
            with self.subTest(text=text):
                self.builds()
                write(self.d, f'latest.{PAIR}.json', text)
                with self.assertRaisesRegex(rd.ReportError, message):
                    self.builds_section()

    def test_no_note_beside_the_machines(self):
        self.files()
        os.remove(os.path.join(self.d, f'step0.{PAIR}.json'))
        self.assertEqual(self.section()['note']['state'], 'not reported')

    def test_a_record_of_another_shape(self):
        for name, text, message in (
                (f'step1_compared.{PAIR}.json', '{"step": 1}\n',
                 'has the keys'),
                (f'step1_compared.{PAIR}.json', '{"step": 2}\n',
                 'not what step 1 writes'),
                (f'step1_compared.{PAIR}.json', '{}\n{}\n',
                 'not one line'),
                (f'step0.{PAIR}.json', '{"step": 1}\n',
                 'not what step 0 writes')):
            with self.subTest(name=name, text=text):
                self.files()
                write(self.d, name, text)
                with self.assertRaisesRegex(rd.ReportError, message):
                    self.section()


class TheSummaryPage(_Folder):
    """scripts/report_html.py: the summary page of a report, held to it by
    _harness.report_page_held, the check that every audit.sh run of the
    suite reaching its summary passes through check_report. Here, states
    of a report that no run of the suite prints, each a report load()
    accepts: written by report_data.main() from this module's files of a
    run, then one part changed and read back through load(). And pages
    broken one way at a time, each refused, beside a change of style the
    check does not hold; and report_html.main()."""

    def loaded(self, r):
        """`r` written as the run's data file and read back by load()."""
        path = os.path.join(self.d, f'report.{PAIR}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        return rd.load(path)

    def fresh(self):
        """The folder emptied of the files of the case before."""
        for name in os.listdir(self.d):
            os.remove(os.path.join(self.d, name))

    def on_machines(self):
        """Two machines' files, each machine on its build, and what the
        sides recorded of them."""
        self.fresh()
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        r = self.written(route='builds', old=B8, new=B9, old_node='/m/old',
                         new_node='/m/new',
                         order_words='forward none build build builds',
                         step11=True)
        r['compared'] = compared_run(True)['compared']
        return self.loaded(r)

    def on_machines_unreported(self):
        """Two machines' files, step 11's summary not reported, and its
        `!!` of a locale not on the new machine, which the page places
        among the locales added and removed."""
        r = self.on_machines()
        r['step11'] = {'state': 'not reported', 'why': 'no summary file'}
        r['warnings']['step11'] = {'state': 'reported', 'items': [
            rd.warning_item('x', 'not_on_new_machine',
                            {'locales': ['xx_YY']},
                            '!! Not on the new machine: xx_YY')]}
        return self.loaded(r)

    def on_tags(self, **files):
        """Two tags, and what the sides recorded of them."""
        self.fresh()
        step2_files(self.d, **files)
        r = self.written()
        r['compared'] = compared_run()['compared']
        return self.loaded(r)

    def on_tags_added(self):
        """Two tags, and the locale file step 2 removed renamed to one it
        found new, spelt two ways."""
        r = self.on_tags()
        r['step2']['rules']['added'] = {'state': 'notice', 'rows': [
            {'name': 'yy', 'renamed_from': 'xx',
             'spellings': ['yy', 'yy.utf8']}]}
        return self.loaded(r)

    def on_builds(self):
        """Two builds rebuilt from their packages, the new one typed as
        latest-el9."""
        self.fresh()
        step2_files(self.d)
        r = self.written(route='builds', old=R8, new=R9,
                         order_words='forward none build build builds')
        r['compared'] = builds_run(latest=(R8, 'latest-el9'))['compared']
        return self.loaded(r)

    def on_one_commit(self):
        """Two tags that are one commit: step 2 compared nothing, and the
        sides and step 2 print the same `!!`."""
        self.fresh()
        nothing_files(self.d, 'commit')
        warnings_file(self.d, 1, ['same_commit', 'code_not_compared'])
        warnings_file(self.d, 2, ['same_commit'])
        return self.written(order_words='same commit tag tag tags')

    def page(self, r):
        """The page of `r` (read back by load() first), where the check
        looks for it, held; its text, the tags taken out."""
        import report_html
        r = self.loaded(r)
        path = os.path.join(self.d, f'report.{PAIR}.json')
        with open(path[:-5] + '.html', 'w', encoding='utf-8') as f:
            f.write(report_html.render(r))
        _harness.report_page_held(path, r)
        with open(path[:-5] + '.html', encoding='utf-8') as f:
            page = f.read()
        os.remove(path[:-5] + '.html')
        page = re.sub(r'<style>.*?</style>', '', page, flags=re.S)
        text = html.unescape(re.sub(r'<[^>]+>', ' ', page))
        text = re.sub(r'\s+', ' ', text.replace('\u2014', '-'))
        return re.sub(r'\( ', '(', re.sub(r' ([.,;:)])', r'\1', text))

    def test_the_counts_and_not_measured(self):
        """On the machines, step 11's counts under their words; on the
        tags, NOT MEASURED with the reason, and no count of step 11."""
        text = self.page(self.on_machines())
        self.assertIn('1 locales sort differently', text)
        self.assertIn('1 locales unchanged 0 locales not compared', text)
        self.assertNotIn('NOT MEASURED: how many', text)
        text = self.page(self.on_tags())
        self.assertIn('NOT MEASURED: how many locales sort differently, '
                      "are gone or new the measured order NOT RUN - no "
                      "machines' files", text)
        self.assertNotIn('locales sort differently 0', text)
        self.assertIn('en_US@ampm deleted', text)
        self.assertIn('xx renamed to yy', text)

    def test_parts_not_done(self):
        """A part the run did not do or could not know, in the states no
        run of the suite prints, shown with its state in capitals and its
        reason where the data file gives one; and lists that hold
        nothing, said where they would be."""
        cases = [
            ('on_machines', lambda r: r.__setitem__('step11', {
                'state': 'not reported', 'why': 'no summary file'}),
             ['NOT MEASURED', 'NOT REPORTED - no summary file']),
            ('on_machines', lambda r: r['step11'].__setitem__('counts', {
                'state': 'not reported', 'why': 'no data file'}),
             ['NOT MEASURED', 'NOT REPORTED - no data file']),
            ('on_machines', lambda r: r['compared']['machines'][0]
             .__setitem__('charmaps', {'state': 'none',
                                       'why': 'none in the file'}),
             ['charmaps: NONE - none in the file',
              'compared as: its locale sources on glibc-2.28,']),
            ('on_machines', lambda r: r['step11']['sql'].__setitem__(
                'files', []),
             ['(no SQL file was written) NOT VERIFIED by these files: '
              'en_US.utf8@ampm, en_US@ampm - it is not on the new machine']),
            ('on_machines', lambda r: r['step11'].__setitem__('sql', {
                'state': 'not reported', 'why': 'no data file'}),
             ['Confirm in PostgreSQL NOT REPORTED - no data file']),
            ('on_machines_unreported', lambda r: None,
             ['NOT MEASURED', 'NOT REPORTED - no summary file',
              'NOT REPORTED - no summary file 1 locale not on the new '
              'machine: a database that uses one cannot use it there Not on '
              'the new machine: xx_YY Locale source files between']),
            ('on_tags', lambda r: r['warnings'].__setitem__('step2', {
                'state': 'not reported', 'why': 'no warnings file'}),
             ['The warnings of step 2 NOT REPORTED - no warnings file']),
            ('on_tags', lambda r: r.__setitem__('compared', {
                'state': 'not reported', 'why': 'no record'}),
             ['NOT REPORTED - no record']),
            ('on_tags', lambda r: r['step2'].__setitem__('removed', {
                'state': 'not reported', 'why': 'no list'}),
             ['Gone at glibc-2.34 NOT REPORTED - no list']),
            ('on_tags', lambda r: r['step2'].__setitem__('rules', {
                'state': 'not reported', 'why': 'no rules'}),
             ['New at glibc-2.34 NOT REPORTED - no rules']),
            ('on_tags', lambda r: r['compared'].__setitem__(
                'collation_version', {'state': 'unknown',
                                      'sides': ['glibc-2.34']}),
             ['UNKNOWN - no glibc version for glibc-2.34']),
            ('on_tags', lambda r: r['compared'].__setitem__('latest', {
                'state': 'not reported', 'why': 'no latest record'}),
             ['latest-elN NOT REPORTED - no latest record']),
            ('on_builds', lambda r: r['compared']['latest']['resolved'][0]
             .__setitem__('when', {'state': 'no date'}),
             ['and its metadata gives NO DATE this reads'])]
        for n, (base, change, want) in enumerate(cases):
            with self.subTest(n=n):
                r = getattr(self, base)()
                change(r)
                text = self.page(r)
                for w in want:
                    self.assertIn(w, text)
        text = self.page(self.on_tags(removed=''))
        self.assertIn('Gone at glibc-2.34 (none)', text)
        self.fresh()
        step2_files(self.d)
        r = self.written(order_words='forward none tag commit sides')
        r['compared'] = compared_run()['compared']
        text = self.page(r)
        self.assertIn('The sides cannot show what your distro adds or drops',
                      text)
        text = self.page(self.on_tags_added())
        self.assertIn('New at glibc-2.34 yy renamed from xx as yy, yy.utf8 '
                      'The old side is an upstream tag: a file it does not '
                      'have does not prove the locale was absent on the old '
                      'system (see Not verified).', text)
        text = self.page(self.on_machines())
        self.assertIn('Not on the new machine (none)', text)
        self.assertIn('Only on the new machine (none)', text)

    def test_one_commit(self):
        """Nothing compared: said at the top; step 2's new files NOT
        COMPARED with its reason; the `!!` the sides and step 2 print
        alike, one card, counted once, as the text's warnings block
        prints it once."""
        text = self.page(self.on_one_commit())
        self.assertIn('Nothing was compared: the two sides are one commit',
                      text)
        self.assertIn('New at glibc-2.34 NOT COMPARED - the two tags are '
                      'one commit', text)
        self.assertEqual(text.count('same_commit'), 1)  # its text, once
        self.assertIn('3 things NOT verified', text)

    def test_unchanged_only_as_far_as_measured(self):
        """A locale unchanged only as far as step 11 measured it: counted
        with the unchanged, and said apart under its own words."""
        r = self.on_machines()
        r['step11']['verdicts']['C'] = {
            'verdict': 'far',
            'words': 'unchanged only as far as step 11 measured it'}
        r['step11']['counts']['unchanged_as_far_as_measured'] = 1
        text = self.page(r)
        self.assertIn('1 locales unchanged of them only as far as '
                      'measured: 1', text)  # the count, then its words

    def test_the_groups_whose_order_changed(self):
        """Each group of locales whose order changed, in its section, said
        whole: its names and encoding, its sort table, what moved drawn
        with names, the pairs told apart differently, what did not move,
        what compares differently against another, and what it leaves
        unsettled; a list too long for the page as a sample, how many
        more and the file that has them all."""
        r = self.on_machines()
        text = self.page(r)
        self.assertIn(
            'Locales whose sort order changed Indexes, ORDER BY, comparisons '
            'and range partitions on text in these locales can give other '
            'results after the upgrade. Open a group for its characters. '
            'Every character, other string and pair found: '
            f'measured_order_characters.{PAIR}.tsv sv_SE (ISO-8859-1) 1 '
            'character(s) moved; 2 pair(s) told apart differently Its '
            'compiled sort table changed: on one machine it has no levels, so '
            'glibc compares bytes there. Moved: 1 They now sort in another '
            'place. \u00e5 U+00E5 LATIN SMALL LETTER A WITH RING ABOVE Pairs '
            'told apart differently: 2 one other before now \u00e4 U+00E4 '
            'LATIN SMALL LETTER A WITH DIAERESIS \u00e5 U+00E5 LATIN SMALL '
            'LETTER A WITH RING ABOVE like an accent or by case as different '
            'letters', text)
        self.assertIn(
            'Among them, 1 string(s) that did not move \u00e4 U+00E4 2 '
            'character(s) and 0 other string(s) now compare differently '
            'against at least one other \u00e4 U+00E4 \u00e5 U+00E5 Not '
            'settled for this group The pairs that tell letters, accents and '
            'case apart here are told apart differently themselves: letters '
            'accents case before a U+0061 / b U+0062 a U+0061 / \u00e1 U+00E1 '
            'a U+0061 / A U+0041 now a U+0061 / b U+0062 NOT MEASURED a '
            'U+0061 / A U+0041 Pairs compared only by letters and case; '
            'accents NOT COMPARED here. 1 more pair(s) may be told apart '
            'differently; what was measured cannot decide: a U+0061 and '
            'bytes 80 Not verified', text)
        self.assertIn('6 things NOT verified', text)
        # A list longer than the page draws: a sample of what draws, how
        # many more and where; and more undecided pairs than it shows.
        f = {x['kind']: x for x in r['step11']['findings']['groups'][0]
             ['findings']}
        f['reversed'].update(count=256, characters=256,
                             runs=[['U+0000', 'U+00FF']])
        f['undecided'].update(count=12, pairs=[
            [f'U+{0x41 + i:04X}', f'U+{0x42 + i:04X}'] for i in range(12)])
        text = self.page(r)
        self.assertIn(
            'now compare differently against at least one other ! U+0021',
            text)
        self.assertIn(
            '[ U+005B \\ U+005C A sample: the first 60 that draw as a '
            'character. '
            'and 196 more, all listed in measured_order_characters.'
            f'{PAIR}.tsv Not settled', text)
        self.assertIn('J U+004A and K U+004B and 2 more, all listed in '
                      f'measured_order_characters.{PAIR}.tsv Not verified',
                      text)
        # The characters file not reported: said once, and no list points
        # to it.
        r['step11']['characters'] = {'state': 'not reported',
                                     'why': 'no characters file'}
        text = self.page(r)
        self.assertIn('Every character, other string and pair found: NOT '
                      'REPORTED - no characters file sv_SE', text)
        self.assertIn('and 196 more Not settled', text)
        self.assertNotIn('all listed in', text)

    def test_what_no_sql_file_checks(self):
        """16.413: an entry no SQL file checks is in Not verified, counted
        there; a group whose locales it names says NOT VERIFIED in
        PostgreSQL beside its names and why among what it leaves
        unsettled. SQL files not reported: each group says it does not
        know."""
        r = self.on_machines()
        r['step11']['sql']['not_checked'].append({
            'names': ['sv_SE'], 'why': 'PostgreSQL imports no collation of '
            'its encoding, ISO-8859-1', 'about': 'order'})
        text = self.page(r)
        self.assertIn('sv_SE (ISO-8859-1) NOT VERIFIED in PostgreSQL 1 '
                      'character(s) moved', text)
        self.assertIn('NOT VERIFIED in PostgreSQL: no SQL file checks these '
                      'locales - PostgreSQL imports no collation of its '
                      'encoding, ISO-8859-1. Not verified', text)
        self.assertIn('NOT VERIFIED in PostgreSQL: no SQL file checks sv_SE '
                      '- PostgreSQL imports no collation of its encoding, '
                      'ISO-8859-1 How this was compared', text)
        self.assertIn('7 things NOT verified', text)
        self.assertEqual(text.count('NOT VERIFIED in PostgreSQL'), 5)
        r['step11']['sql'] = {'state': 'not reported', 'why': 'no data file'}
        text = self.page(r)
        self.assertIn('Whether an SQL file checks these locales: NOT '
                      'REPORTED (see Confirm in PostgreSQL). Not verified',
                      text)
        self.assertNotIn('NOT VERIFIED in PostgreSQL', text)
        self.assertIn('4 things NOT verified', text)

    def test_an_entry_of_a_group_names_it(self):
        """load(): an entry no SQL file checks that is about a group's
        order names one group of step 11, as the SQL files write its
        names, and a group has one such entry at most; an entry about the
        character rules (LC_CTYPE) of the same locales is not that group's
        (16.413): the group is not marked, the entry is counted in Not
        verified."""
        r = self.on_machines()
        x = {'names': ['sv_SE'], 'why': 'w', 'about': 'order'}
        for entries, why in (
                ([dict(x, names=['xx_YY'])], 'is no group of step 11'),
                ([x, dict(x, why='v')], 'second entry of group 0')):
            with self.subTest(why=why):
                r['step11']['sql']['not_checked'] = entries
                with self.assertRaisesRegex(rd.ReportError, why):
                    self.loaded(r)
        r['step11']['sql']['not_checked'] = [dict(x, about='ctype')]
        text = self.page(r)
        self.assertNotIn('NOT VERIFIED in PostgreSQL 1', text)
        self.assertNotIn('no SQL file checks these locales', text)
        self.assertIn('NOT VERIFIED in PostgreSQL: no SQL file checks sv_SE '
                      '- w How this', text)
        self.assertIn('5 things NOT verified', text)

    def test_a_short_list_of_bytes_and_every_string(self):
        """A run of strings named by their bytes in a list the page shows
        whole, drawn first..last; "all" said where every string compares
        differently, as the text says it; no level compared, said so."""
        r = self.on_machines()
        f = {x['kind']: x for x in r['step11']['findings']['groups'][0]
             ['findings']}
        f['reversed'].update(count=5, characters=2, other_strings=3,
                             all=True, runs=[['U+00E4', 'U+00E5'],
                                             ['bytes 80', 'bytes 82']])
        text = self.page(r)
        self.assertIn('all 2 character(s) and 3 other string(s) now compare '
                      'differently against at least one other \u00e4 U+00E4 '
                      '\u00e5 U+00E5 bytes 80..82 Not settled', text)
        g = r['step11']['findings']['groups'][0]
        g['findings'] = [x for x in g['findings']
                         if x['kind'] not in ('told apart', 'did not move')]
        f['left out'].update(compared=[],
                             left_out=['letters', 'accents', 'case'])
        self.assertIn('No pair can be compared; letters and accents and case '
                      'NOT COMPARED here.', self.page(r))

    def test_groups_not_known_or_none(self):
        """Step 11 not run, its findings not reported: the section says so,
        with the reason, and shows no group; no group changed: "(none)";
        an encoding a machine did not give, in capitals, and one that
        changed, old to new."""
        text = self.page(self.on_tags())
        self.assertIn("Locales whose sort order changed NOT RUN - no "
                      "machines' files Only the measurements of the two "
                      "machines show which locales sort differently. Not "
                      "verified", text)
        r = self.on_machines()
        r['step11']['findings'] = {'state': 'not reported',
                                   'why': 'no data file'}
        text = self.page(r)
        self.assertIn('Locales whose sort order changed NOT REPORTED - no '
                      'data file Not verified', text)
        r = self.on_machines()
        r['step11']['findings']['groups'] = []
        r['step11']['verdicts']['sv_SE'] = {
            'verdict': 'firm', 'words': 'unchanged by its compiled sort table'}
        r['step11']['counts'].update(changed=0, unchanged=2)
        r['step11']['characters']['rows'] = []
        r['step11']['checks']['charmaps'] = {
            'state': 'not needed', 'by_table': 1, 'builtin': ['C']}
        text = self.page(r)
        self.assertIn('Locales whose sort order changed (none) Not verified',
                      text)
        r = self.on_machines()
        cs = r['step11']['findings']['groups'][0]['locales']['sv_SE'][
            'codeset']
        cs['old'] = 'unknown'
        self.assertIn('sv_SE (UNKNOWN \u2192 ISO-8859-1) 1', self.page(r))
        cs['old'] = 'ISO-8859-15'
        self.assertIn('sv_SE (ISO-8859-15 \u2192 ISO-8859-1) 1', self.page(r))

    def test_a_page_broken_is_refused(self):
        """One break at a time, each a way a page can say what the data
        does not: a count, a member of a list, words over the wrong
        values, values of an entry in another order, a card, a warning's
        text, a state, a sentence the data calls for or the sentence of
        another route, words the page never says, 0 for what was not
        measured, a list emptied without saying so. And a change of style,
        which the check does not hold and passes."""
        import report_html
        path = os.path.join(self.d, f'report.{PAIR}.json')
        cases = (
            ('on_machines', r'(data-p="step11/counts/changed">)(\d+)',
             lambda m: m.group(1) + str(int(m.group(2)) + 1), 'shows'),
            ('on_machines', r'(data-p="compared/machines#0/locales">)(\d+)',
             lambda m: m.group(1) + str(int(m.group(2)) + 1), 'shows'),
            ('on_machines', r'Not on the new machine',
             lambda m: 'Only on the new machine', 'label'),
            ('on_machines', r'locales sort differently',
             lambda m: 'locales unchanged', 'label'),
            ('on_machines', r'<b data-t="">old</b>',
             lambda m: '<b data-t="">new</b>', 'label'),
            ('on_machines', r'(data-d="not_verified">)(\d+)',
             lambda m: m.group(1) + str(int(m.group(2)) + 1), 'worked out'),
            ('on_machines', r'data-l="cv old"><span data-t="">old side:'
             r'(.*?)data-l="cv new"><span data-t="">new side:',
             lambda m: 'data-l="cv new"><span data-t="">new side:'
             + m.group(1) + 'data-l="cv old"><span data-t="">old side:',
             'holds'),
            ('on_machines', r'<div class="tile bad" data-l="changed">.*?'
             r'</div>', lambda m: '', 'calls for'),
            ('on_machines_unreported', r'<details class="card" data-w='
             r'"warnings/step11/items#0">.*?</details>', lambda m: '',
             'placed'),
            ('on_machines', r'class="badge warn"',
             lambda m: 'class="badge ok"', 'shown as'),
            ('on_tags', r'<div class="tile nm".*?</b></div>',
             lambda m: '<div class="tile"><b>0</b></div>', 'outside'),
            ('on_tags', r'(<b data-p="step11/state">)NOT RUN(</b>)',
             lambda m: m.group(1) + '(none)' + m.group(2), 'shows'),
            ('on_tags', r'<div class="nd"><b data-p="step11/state">NOT RUN'
             r'</b>[^<]*<span[^>]*>[^<]*</span></div>(<h3>)',
             lambda m: m.group(1), 'not shown once'),
            ('on_tags', r'Gone at', lambda m: 'New at', 'label'),
            ('on_tags', r'data-v="renamed">renamed to',
             lambda m: 'data-v="renamed">deleted', 'shown as'),
            ('on_tags', r'<li><span data-p="step2/removed/rows#0/name">'
             r'.*?</li>', lambda m: '', 'removed rows'),
            ('on_tags', r'<p class="hint" data-p="step2/removed/'
             r'source"[^>]*>[^<]*</p>', lambda m: '', 'come from'),
            ('on_tags', r'<li><span data-p="step11/not_done#4">.*?</li>',
             lambda m: '', 'did not do'),
            ('on_tags', r'(<div class="nd"><b data-p="step11/state">)',
             lambda m: '<p>Every other locale sorts as before.</p>'
             + m.group(1), 'does not know'),
            ('on_tags', r'<p class="sub0" data-f="sub '
             r'sources">.*?</p>', lambda m: '<p class="sub0" '
             'data-f="sub unreported">The two machines\' files were given; '
             'what they measured is not reported (see below).</p>',
             'fixed sentences'),
            ('on_tags', r' <span data-p="step2/removed/rows#1/to">yy</span>',
             lambda m: '', 'where it went'),
            ('on_machines', r'(<div class="tile ok" data-l="unchanged"><b>'
             r'<span data-p="step11/counts/)unchanged(">)1',
             lambda m: m.group(1) + 'unchanged_as_far_as_measured'
             + m.group(2) + '0', 'holds'),
            ('on_builds', r'(<span data-p="compared/builds/entries#0/patches/)'
             r'applied(">)(\d+)(</span> of its <span data-p="compared/builds/'
             r'entries#0/patches/)of(">)([\d,]+)',
             lambda m: m.group(1) + 'of' + m.group(2) + m.group(6) + m.group(4)
             + 'applied' + m.group(5) + m.group(3), 'order'),
            ('on_builds', r'(<span data-p="compared/latest/resolved#0/)build'
             r'(">)([^<]*)(</span>, the newest glibc build in <span data-p="'
             r'compared/latest/resolved#0/)folder(">)([^<]*)',
             lambda m: m.group(1) + 'folder' + m.group(2) + m.group(6)
             + m.group(4) + 'build' + m.group(5) + m.group(3),
             'latest-elN said'),
            ('on_tags', r'<p class="none" data-e="step2/rules/added/'
             r'rows">\(none\)</p>', lambda m: '', 'empty lists'),
            ('on_tags', r'(<h2>How this was compared)',
             lambda m: m.group(1) + ' (2 sides)', 'outside'),
            ('on_tags', r'<p class="disc" data-f="disc">'
             r'.*?</p>',
             lambda m: '', 'fixed sentences'),
            ('on_one_commit', r'<div class="banner">.*?</div>',
             lambda m: '', 'fixed sentences'),
            ('on_one_commit', r'<details class="card" data-w="[^"]*">'
             r'.*?</details>', lambda m: '', 'Not verified'),
            ('on_one_commit', r'(<pre data-p="warnings/step1/items#0/'
             r'text">)', lambda m: m.group(1) + 'x', 'shows'),
            ('on_tags_added', r', <span data-p="step2/rules/added/rows#0/'
             r'spellings#1">yy.utf8</span>', lambda m: '', 'spelt'),
            ('on_tags', r'not a guarantee', lambda m: 'a guarantee',
             'fixed sentence'),
            ('on_tags', r'(data-p="compared/tags#0/ref">glibc-2.28</span>)'
             r' =', lambda m: m.group(1) + ' = verified, no change:',
             'says'),
            ('on_machines', r'read FILE old.out new.out',
             lambda m: 'read FILE new.out old.out', 'fixed sentence'),
            ('on_machines', r'(data-e="step11/verdicts:only old">)\(none\)',
             lambda m: m.group(1) + 'no change', 'empty, said'),
            ('on_tags', r'(data-f="block step2">)step 2',
             lambda m: m.group(1) + 'step 11', 'fixed sentence'),
            ('on_builds', r', by its metadata of', lambda m: ', and its '
             'metadata gives', 'latest-elN said'),
            # 16.408: each file's tests and each entry no file checks,
            # held to its value, its words and its order.
            ('on_machines', r'(data-p="step11/sql/files#1/tests">)38,761',
             lambda m: m.group(1) + '4', 'shows'),
            ('on_machines', r' test\(s\)', lambda m: ' tests', 'does not know|SQL file said'),
            ('on_machines', r'<b>NOT VERIFIED</b>', lambda m: '<b>NOTED</b>',
             'says'),
            ('on_machines', r', <span data-p="step11/sql/not_checked#0/'
             r'names#1">en_US@ampm</span>', lambda m: '', 'holds'),
            ('on_machines', r'<div class="hint">also, in PostgreSQL: <span '
             r'data-p="step11/sql/not_checked#0/also">[^<]*</span></div>',
             lambda m: '', 'holds'),
            ('on_machines', r'<div class="nd" data-l="not checked">.*?'
             r'</div>(?=<div class="nd" data-l="not checked">)',
             lambda m: '', 'labels'),
            # Each entry once and in the data's order: the two entries,
            # and the two files, swapped.
            ('on_machines', r'(<div class="nd" data-l="not checked">.*?'
             r'</div></div>)(<div class="nd" data-l="not checked">.*?'
             r'</div>)(?=</div></details>)',
             lambda m: m.group(2) + m.group(1), 'shows the entries'),
            ('on_machines', r'(<li data-l="sql">.*?</li>)'
             r'(<li data-l="sql">.*?</li>)',
             lambda m: m.group(2) + m.group(1), 'shows the entries'),
            # 24.4-2a: a group said otherwise, out of its order, out of its
            # section; an entry no SQL file checks left out of Not verified.
            ('on_machines', r'U\+00E5</code> <span class="nm">',
             lambda m: 'U+00E4</code> <span class="nm">', 'group 0 said'),
            ('on_machines', r'data-grp="0"', lambda m: 'data-grp="1"',
             'groups shown'),
            ('on_machines', r'<details class="grp" data-grp="0">.*?</details>'
             r'(?=</div></details>)', lambda m: '', 'groups shown'),
            ('on_machines', r'(<details class="grp" data-grp="0">.*?'
             r'</details>)(?=</div></details>)(.*?<h2>Not verified</h2>'
             r'</summary><div class="secb">)',
             lambda m: m.group(2) + m.group(1), 'shown under'),
            ('on_machines', r'<div class="card nd-card" data-l="nv not '
             r'checked">.*?</div></div>', lambda m: '', 'labels'),
            ('on_machines', r'(data-d="characters file">)measured',
             lambda m: m.group(1) + 'Measured', 'characters file'),
            ('on_tags', r'font-size:22px', lambda m: 'font-size:23px',
             None))
        for n, (base, rx, repl, why) in enumerate(cases):
            with self.subTest(n=n, rx=rx):
                r = getattr(self, base)()
                page = report_html.render(r)
                broken, k = re.subn(rx, repl, page, count=1, flags=re.S)
                self.assertEqual(k, 1)
                with open(path[:-5] + '.html', 'w', encoding='utf-8') as f:
                    f.write(broken)
                if why is None:
                    _harness.report_page_held(path, r)
                else:
                    with self.assertRaisesRegex(AssertionError, why):
                        _harness.report_page_held(path, r)

    def test_written_beside_the_data_file(self):
        """main(): the page beside the data file it reads, nothing
        printed; a data file load() refuses gives exit 1, why on stderr,
        and no page nor .partial; a wrong call, exit 2."""
        import contextlib
        import io
        import report_html
        step2_files(self.d)
        self.written()
        path = os.path.join(self.d, f'report.{PAIR}.json')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(report_html.main([path]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertEqual(sorted(n for n in os.listdir(self.d)
                                if n.startswith('report.')),
                         [f'report.{PAIR}.html', f'report.{PAIR}.json'])
        _harness.report_page_held(path, rd.load(path))
        os.remove(path[:-5] + '.html')
        with open(path, encoding='utf-8') as f:
            r = json.load(f)
        r['step11']['state'] = 'not yet run'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(r, f)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(report_html.main([path]), 1)
        self.assertIn('error: cannot write', err.getvalue())
        self.assertEqual(sorted(n for n in os.listdir(self.d)
                                if n.startswith('report.')),
                         [f'report.{PAIR}.json'])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(report_html.main([]), 2)
            self.assertEqual(report_html.main([path, path]), 2)


if __name__ == '__main__':
    unittest.main()
