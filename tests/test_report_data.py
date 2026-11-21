"""scripts/report_data.py: the run's data file, report.<pair>.json, assembled
from the files the steps wrote, and load(), its strict reader.

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
    write(d, f'step11.{PAIR}.data.json',
          json.dumps({'step': 11, 'counts': COUNTS,
                      'findings': FINDINGS, 'checks': CHECKS}) + '\n')


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
    'names_bytes': True, 'sort_table': rd.NOT_YET}]}


# The counts of step11_files' two locales, C unchanged (firm, built into
# glibc) and sv_SE changed, as locale_order.write_counts writes them.
COUNTS = {'machines': {'old': {'label': 'glibc-2.28-251.el8', 'locales': 2},
                       'new': {'label': 'glibc-2.34-100.el9', 'locales': 2}},
          'changed': 1, 'unchanged': 1, 'unchanged_as_far_as_measured': 0,
          'not_measured': 0, 'refused': 0, 'only_old': 0, 'only_new': 0}


# The checks of step11_files' two machines, as locale_order.write_counts
# writes them: C and POSIX byte order on both, the decimals of money of both
# locales the same, one PostgreSQL process on each running the C library
# measured there.
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
                           'one_byte': 1, 'left_out': [], 'elsewhere': 0}}


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
    'closing': ({'state': 'mixed'},
                'Whether the two machines compare strings with the same glibc '
                'code, and rules of several letters'),
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
                     r['step2']['character_sets']):
            self.assertEqual(part['state'], 'not yet reported')
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
        self.assertEqual(len(s11['characters']['rows']), 2)
        self.assertEqual(s11['sql_files'], [
            {'encoding': 'UTF8',
             'file': f'measured_order_confirm.{PAIR}.UTF8.sql'}])
        self.assertEqual(s11['rules_beside']['state'], 'set beside')
        self.assertEqual(s11['counts'], {'state': 'reported', **COUNTS})
        self.assertEqual(s11['findings'], FINDINGS)
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
        none is not reported, an empty one holds none, and each `!!` it
        holds comes with its list and title; without step 11 it is not
        read."""
        step2_files(self.d, **MACHINES)
        step11_files(self.d)
        over = dict(ON_MACHINES)
        self.assertEqual(self.written(**over)['warnings']['step11'], {
            'state': 'not reported',
            'why': f'the step wrote no step11.{PAIR}.warnings.jsonl'})
        warnings_file(self.d, 11, [])
        self.assertEqual(self.written(**over)['warnings']['step11'],
                         {'state': 'reported', 'items': []})
        kinds = ['ctype_characters_changed', 'other_encoding']
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
        warnings_file(self.d, 11, ['euc_ascii_same', 'other_encoding'])
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
                'state': 'reported', 'why': 'x'}), 'sort_table is'),
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
            r['step11']['verdicts']['de_DE'] = {'verdict': 'only new',
                                                'words': 'x'}
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
        """A file step 11 did not write as it writes it: refused, never
        read as counts, findings or checks."""
        whole = {'step': 11, 'counts': COUNTS, 'findings': FINDINGS,
                 'checks': CHECKS}
        for text in ('{"step": 11}\n', '{"step": 2, "counts": {}}\n',
                     '{"step": 11, "counts": {"state": "reported"}}\n',
                     '{"step": 11, "counts": []}\n', '{"step": 11, "co',
                     json.dumps({'step': 11, 'counts': COUNTS}),
                     json.dumps({**whole, 'findings': []}),
                     json.dumps({**whole, 'findings': rd.NOT_YET}),
                     json.dumps({k: v for k, v in whole.items()
                                 if k != 'checks'}),
                     json.dumps({**whole, 'checks': []}),
                     json.dumps({**whole, 'checks': {'state': 'reported',
                                                     **CHECKS}}),
                     json.dumps({**whole, 'more': 1})):
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
        step11 = lambda r: r['warnings']['step11']['items'][1]
        self.refuses(lambda r: step11(r).update(title='Nothing changed'))
        self.refuses(lambda r: step11(r)['data'].update(told='yes'))
        self.refuses(lambda r: r['warnings'].update(step11=rd.NOT_YET))

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


if __name__ == '__main__':
    unittest.main()
