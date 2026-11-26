#!/usr/bin/env python3
"""The run's data file, report.<pair>.json, assembled at the end of audit.sh
from the files the steps already wrote, beside the text, which it does not
change.

Every section is always there. What a run did not do is a state with its
reason, never an absent key or a null, so that a reader cannot take "this
was not looked at" for "nothing was found". What 'What was compared'
says of the tags, the machines, the builds rebuilt from their packages
and the glibc versions, and what is printed above it (step 0's note, or
the line of each latest-elN), is data: step1_compared.<pair>.json and,
when the machines' files are given, step0.<pair>.json, or, when a side
was typed as latest-elN, latest.<pair>.json, read into compared and tied
to other facts of the run (_compared, _latest). What step 2 prints of its
rules is data:
step2_rules.<pair>.json, read into step2.rules and tied to the other
facts of the run (_rules); and so is what it prints of its character
set check: step2_character_sets.<pair>.json, read into
step2.character_sets (_character_sets), and of the collating elements
built out of transliterations: step2_transliteration.<pair>.json, read
into step2.transliteration (_transliteration).
The `!!` notices of steps 0, 1, 2 and 11
are data: a file beside each step's log holds them (step 0's copied
there by audit.sh, step 11's step11.<pair>.warnings.jsonl), and KINDS
gives each kind its list and its short title. Step 11's counts are
data too, and so is each group of locales it found changed, with what
moved in it and its sort table, each check beside the order, and what
it prints of the SQL files it wrote, each file's number of tests and
each entry no file checks, with its reason and what it is about:
step11.<pair>.data.json,
beside the `!!` of its warnings file.

load() is the strict reader: a key missing or added, or a state outside
its list, is an error, never a default.

usage: report_data.py --out-dir DIR --pair SLUG --old OLD --new NEW
         --route tags|builds|sides --order-words "ORDER NOTHING OLD NEW WORD"
         [--old-node FILE --new-node FILE] [--tag-side old|new]
         [--old-asked SIDE --new-asked SIDE] [--step11]
"""
import argparse
import glob
import json
import os
import re
import sys

FORMAT = 'pg-glibc-audit-report/1'

ROUTES = ('tags', 'builds', 'sides')
ORDERS = ('same', 'forward', 'undetermined')
NOTHING = ('commit', 'tree', 'none')
CLASSES = ('tag', 'commit', 'build')
WORDS = ('tags', 'builds', 'sides')
# Where the locale files Removed compares come from, and whether what the
# distros add or drop is checked (audit.sh, the Removed block).
REMOVED_SOURCES = {
    'machine sources': 'checked against no package',
    'build packages': 'checked',
    'old side is a tag': 'not checked',
    'new side is a tag': 'not checked',
    'tags': 'not checked',
    "tags, because of a machine's file": 'not checked',
}
DISTRO_FILES = ('checked', 'checked against no package', 'not checked')
# Step 2's character set check, as step 2 writes it (locale_order).
CHARMAPS_STATES = ('compared', 'not run', 'one commit', 'one tree',
                   'not read')
# The states step 2 writes with no reason (write_rules_by_name).
NO_WHY = ('compared', 'one commit', 'one tree')
VERDICTS = ('changed', 'firm', 'far', 'unmeasured', 'refused', 'only old',
            'only new')
STEP11_NOT_DONE = (
    'how each machine sorts every locale',
    'the decimals of money',
    'which C library PostgreSQL runs on either machine',
    'that each machine accepts every locale and converts its text',
    "what each machine's character rules (LC_CTYPE) answer",
)
CHARACTER_COLUMNS = ['locales', 'finding', 'characters', 'how', 'test',
                     'before', 'now', 'measured']
RULES_CHARACTER_COLUMNS = ['name', 'state', 'every rule can change',
                           'characters']
class ReportError(Exception):
    pass


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _lines(path):
    """The lines of a list a step wrote, one item each, every one ended by
    a newline: a file whose last line has none was cut short."""
    text = _read(path)
    if text and not text.endswith('\n'):
        raise ReportError(f'{path} does not end with a newline: cut short')
    return text.split('\n')[:-1] if text else []


def _file(out_dir, name):
    return os.path.join(out_dir, name)


def not_reported(path):
    return {'state': 'not reported',
            'why': f'the step wrote no {os.path.basename(path)}'}


# ---------------------------------------------------------------- run

def shown_path(path):
    """A path as given, its bytes that are not UTF-8 written as \\xNN
    (a Latin-1 file name on a Linux machine): JSON holds text only."""
    return os.fsencode(path).decode('utf-8', 'backslashreplace')


def run_section(a):
    words = a.order_words.split()
    if len(words) != 5:
        raise ReportError(f'--order-words {a.order_words!r} is not five words')
    order, nothing, old_class, new_class, word = words
    for value, allowed, what in ((order, ORDERS, 'order'),
                                 (nothing, NOTHING, 'nothing compared'),
                                 (old_class, CLASSES, 'old class'),
                                 (new_class, CLASSES, 'new class'),
                                 (word, WORDS, 'word'),
                                 (a.route, ROUTES, 'route')):
        if value not in allowed:
            raise ReportError(f'{what} {value!r} is none of {allowed}')
    if bool(a.old_node) != bool(a.new_node):
        raise ReportError('one machine file given without the other')
    machines = ({'state': 'given', 'old': shown_path(a.old_node),
                 'new': shown_path(a.new_node)}
                if a.old_node else
                {'state': 'not given', 'why': 'no --old-node and --new-node'})
    return {'route': a.route, 'order': order, 'nothing_compared': nothing,
            'old_class': old_class, 'new_class': new_class, 'word': word,
            'machines': machines}


# ---------------------------------------------------------------- step 2

def removed_source(a, run):
    if a.route == 'builds':
        return 'machine sources' if a.old_node else 'build packages'
    if a.route == 'sides':
        if a.tag_side not in ('old', 'new'):
            raise ReportError(f'--tag-side {a.tag_side!r} on the sides route')
        return f'{a.tag_side} side is a tag'
    return "tags, because of a machine's file" if a.old_node else 'tags'


REMOVED_ROW = re.compile(r'(.+) \((deleted|renamed to (.+))\)')


def removed_section(a, run, path):
    if not os.path.isfile(path):
        return not_reported(path)
    lines = _lines(path)
    if not lines or not lines[0].startswith('# '):
        raise ReportError(f'{path} has no "# " header line')
    rows = []
    for line in lines[1:]:
        if not line:
            raise ReportError(f'{path} has an empty line')
        m = REMOVED_ROW.fullmatch(line)
        if not m:
            raise ReportError(f'{path}: a line of no known shape: {line!r}')
        rows.append({'name': m[1],
                     'how': 'deleted' if m[2] == 'deleted' else 'renamed',
                     'to': m[3] if m[3] is not None else '-'})
    source = removed_source(a, run)
    return {'state': 'reported', 'between': run['word'], 'source': source,
            'distro_files': REMOVED_SOURCES[source], 'rows': rows}


def rules_by_name(path):
    if not os.path.isfile(path):
        return not_reported(path)
    data = json.loads(_read(path))
    keys = {'charmaps', 'compared', 'locales', 'machines', 'measurements',
            'names', 'nothing', 'step', 'what'}
    if not isinstance(data, dict) or set(data) != keys or data['step'] != 2:
        raise ReportError(f'{path} is not what step 2 writes')
    cm = data['charmaps']
    if not isinstance(cm, dict) or cm.get('state') not in CHARMAPS_STATES:
        raise ReportError(f'{path}: charmaps {cm!r} is none of '
                          f'{CHARMAPS_STATES}')
    locales = data['locales']
    if locales is None:
        locales = {'state': 'not applicable',
                   'why': "step 2 names them only for two machines' own "
                          'locale sources'}
    else:
        # step 2 writes no key and no source (None) for a locale built
        # into glibc.
        for name, v in locales.items():
            if not (isinstance(v, list) and len(v) == 5):
                raise ReportError(f'{path}: the rules of {name!r} are {v!r}')
        locales = {'state': 'reported', 'by_name': {
            name: dict(zip(('key', 'rules', 'why', 'charmap', 'source'),
                           ['none' if x is None else x for x in v]))
            for name, v in sorted(locales.items())}}
    return {
        'state': 'reported',
        'compared': data['compared'],
        'names': data['names'],
        'machines': bool(data['machines']),
        'nothing': ({'state': 'something compared'} if data['nothing'] is None
                    else {'state': data['nothing'], 'what': data['what']}),
        'measurements': (data['measurements'] if data['measurements']
                         else {'state': 'not applicable',
                               'why': 'no machine measurement'}),
        'charmaps': ({'state': cm['state']} if cm['state'] in NO_WHY
                     else {'state': cm['state'], 'why': cm['why']}),
        'locales': locales,
    }


def tsv_rows(path, columns, header_prefix=''):
    """(header comment lines, rows, trailer comment lines) of a TSV a step
    wrote: comments, then the column line, then rows of exactly that many
    cells, then comments."""
    lines = _lines(path)
    head, at = [], 0
    while at < len(lines) and lines[at].startswith('# ') and \
            lines[at] != header_prefix + '\t'.join(columns):
        head.append(lines[at][2:])
        at += 1
    if at >= len(lines) or lines[at] != header_prefix + '\t'.join(columns):
        raise ReportError(f'{path} has no column line {columns!r}')
    at += 1
    rows = []
    while at < len(lines) and not lines[at].startswith('# '):
        cells = lines[at].split('\t')
        if len(cells) != len(columns):
            raise ReportError(f'{path} line {at + 1}: {len(cells)} cells, '
                              f'not {len(columns)}')
        rows.append(cells)
        at += 1
    tail = []
    for line in lines[at:]:
        if not line.startswith('# '):
            raise ReportError(f'{path}: a row after the notes: {line!r}')
        tail.append(line[2:])
    return head, rows, tail


def nothing_compared(run, by_name):
    """None when step 2 compared something; else why it compared nothing,
    in step 2's own words (step2_rules_by_name), which are what its lists
    were written from; the order check's answer only where step 2 wrote
    none."""
    if by_name['state'] == 'reported':
        if by_name['nothing']['state'] == 'something compared':
            return None
        return by_name['nothing']['what']
    if run['nothing_compared'] == 'none':
        return None
    return (f"the two {run['word']} are one commit"
            if run['nothing_compared'] == 'commit' else
            f"the two {run['word']} are two commits of one source tree")


# What step2_rules says (filter_lc_collate_changes.write_rules).
RULES_STATES = ('compared', 'not compared')
CHARACTER_STATES = ('listed', 'counted', 'none named', 'not known')
ADDED_STATES = ('none', 'plain', 'notice')
MISSING_STATES = ('not in SUPPORTED', 'not measured on that machine',
                  'in the C.UTF-8 notice')
RULES_KEYS = ['added', 'differ', 'not_ascii', 'removed', 'state', 'step',
              'totals', 'unbuilt', 'unread']
TOTALS_KEYS = ['names', 'same', 'differ', 'not_compared']


def step2_rules(path, nothing):
    """Step 2's record of what it prints of its rules, read as written,
    its state 'compared' given as 'reported';
    load() checks it. 'not compared' only where step 2 compared nothing,
    with the same reason."""
    if not os.path.isfile(path):
        return not_reported(path)
    lines = _lines(path)
    if len(lines) != 1:
        raise ReportError(f'{path} is not one line')
    data = json.loads(lines[0])
    if not isinstance(data, dict) or data.get('step') != 2 or \
            data.get('state') not in RULES_STATES:
        raise ReportError(f'{path} is not what step 2 writes')
    if data['state'] == 'not compared':
        if set(data) != {'step', 'state', 'why'}:
            raise ReportError(f'{path} has the keys {sorted(data)}')
        if nothing is None or data['why'] != nothing:
            raise ReportError(f'{path}: not compared, {data["why"]!r}, '
                              f'where step 2 says {nothing!r}')
        return {'state': 'not compared', 'why': data['why']}
    if sorted(data) != RULES_KEYS:
        raise ReportError(f'{path} has the keys {sorted(data)}')
    if nothing is not None:
        raise ReportError(f'{path}: compared, yet step 2 compared nothing: '
                          f'{nothing}')
    out = {k: v for k, v in data.items() if k != 'step'}
    out['state'] = 'reported'
    return out


# What step2_character_sets says (in node_charmaps.charmap_record's shape
# where the check ran or stopped, else what became of it:
# write_character_sets), its 'compared' given as 'reported'.
CS_STATES = ('reported', 'not run', 'one commit', 'one tree', 'not read')
CS_KEYS = {'compared': ['anyway', 'basis', 'builds', 'changed', 'charmaps',
                        'distro', 'listed', 'not_compared', 'state', 'step',
                        'unsearched', 'unsettled'],
           'not run': ['not_compared', 'state', 'step', 'why'],
           'one commit': ['state', 'step'], 'one tree': ['state', 'step'],
           'not read': ['state', 'step', 'why']}
CS_BASES = ('SUPPORTED', 'measured')
CS_PAIR_STATES = ('compared', 'not compared')
CS_ANYWAY_STATES = ('named', 'not compared')
# What the check settled of a file it found something of, strongest first
# (collation_keys.LISTED_STATES): step 2's rules by name carry it for each
# measured name built from that file.
LISTED_STATES = ('changed', 'transliteration', 'not compared',
                 'transliteration not compared')


def step2_character_sets(path):
    """Step 2's record of its character set check, read as written, its
    state 'compared' given as 'reported'; load() checks it."""
    if not os.path.isfile(path):
        return not_reported(path)
    lines = _lines(path)
    if len(lines) != 1:
        raise ReportError(f'{path} is not one line')
    data = json.loads(lines[0])
    if not isinstance(data, dict) or data.get('step') != 2 or \
            data.get('state') not in CS_KEYS:
        raise ReportError(f'{path} is not what step 2 writes')
    if sorted(data) != CS_KEYS[data['state']]:
        raise ReportError(f'{path} has the keys {sorted(data)}')
    out = {k: v for k, v in data.items() if k != 'step'}
    if out['state'] == 'compared':
        out['state'] = 'reported'
    return out


# What step2_transliteration says (node_charmaps.translit_record where the
# character set check compared builds, else what became of that check:
# write_transliteration), its 'compared' given as 'reported'; the line it
# opens with (node_charmaps.TRANSLIT_LINES) and the counts each line says.
TR_KEYS = {'compared': ['line', 'listed', 'skipped', 'state', 'step',
                        'unsettled'],
           'no change': ['state', 'step'],
           'not run': ['state', 'step', 'why'],
           'one commit': ['state', 'step'], 'one tree': ['state', 'step'],
           'not read': ['state', 'step', 'why']}
TR_STATES = ('reported', 'no change', 'not run', 'one commit', 'one tree',
             'not read')
TR_SKIPPED_STATES = ('built differently', 'not compared')
TR_LINES = {'elements': ['elements', 'builds', 'changed'],
            'unmoved': ['unmoved'], 'none': [], 'listed already': [],
            'no line': []}


def step2_transliteration(path):
    """Step 2's record of its transliterations, read as written, its state
    'compared' given as 'reported'; load() checks it."""
    if not os.path.isfile(path):
        return not_reported(path)
    lines = _lines(path)
    if len(lines) != 1:
        raise ReportError(f'{path} is not one line')
    data = json.loads(lines[0])
    if not isinstance(data, dict) or data.get('step') != 2 or \
            data.get('state') not in TR_KEYS:
        raise ReportError(f'{path} is not what step 2 writes')
    keys = TR_KEYS[data['state']]
    if data['state'] == 'compared':
        if data.get('line') not in TR_LINES:
            raise ReportError(f'{path} has the line {data.get("line")!r}')
        keys = sorted(keys + TR_LINES[data['line']])
    if sorted(data) != keys:
        raise ReportError(f'{path} has the keys {sorted(data)}')
    out = {k: v for k, v in data.items() if k != 'step'}
    if out['state'] == 'compared':
        out['state'] = 'reported'
    return out


def step2_section(a, run):
    d = a.out_dir
    by_name = rules_by_name(_file(d, f'step2_rules_by_name.{a.pair}.json'))
    nothing = nothing_compared(run, by_name)
    listed = _file(d, f'step2_changed_collate.{a.pair}.txt')
    uncompared = _file(d, f'step2_not_compared.{a.pair}.txt')
    if os.path.isfile(listed) and os.path.isfile(uncompared):
        names, skipped = _lines(listed), _lines(uncompared)
        missing = [n for n in skipped if n not in names]
        if missing:
            raise ReportError(f'{uncompared} names {missing!r}, which '
                              f'{listed} does not list')
        if nothing is not None:
            # Nothing compared: the lists are empty because nothing was
            # looked at, never because nothing differs.
            if names:
                raise ReportError(f'{listed} names {names!r}, yet step 2 '
                                  f'compared nothing: {nothing}')
            listed_s = {'state': 'not compared', 'why': nothing}
        else:
            listed_s = {'state': 'reported',
                        'compared': [n for n in names if n not in skipped],
                        'not_compared': skipped}
    else:
        listed_s = not_reported(listed if not os.path.isfile(listed)
                                else uncompared)
    chars = _file(d, f'step2_rules_characters.{a.pair}.tsv')
    if os.path.isfile(chars):
        _, rows, tail = tsv_rows(chars, RULES_CHARACTER_COLUMNS, '# ')
        if tail:
            raise ReportError(f'{chars}: notes after the rows')
        if nothing is not None:
            if rows:
                raise ReportError(f'{chars} has rows, yet step 2 compared '
                                  f'nothing: {nothing}')
            chars_s = {'state': 'not compared', 'why': nothing}
        else:
            chars_s = {'state': 'reported',
                       'columns': RULES_CHARACTER_COLUMNS, 'rows': rows}
    else:
        chars_s = not_reported(chars)
    return {
        'listed': listed_s,
        'removed': removed_section(
            a, run, _file(d, f'step2_removed_locales.{a.pair}.txt')),
        'rules_by_name': by_name,
        'rules_characters': chars_s,
        'rules': step2_rules(_file(d, f'step2_rules.{a.pair}.json'),
                             nothing),
        'character_sets': step2_character_sets(
            _file(d, f'step2_character_sets.{a.pair}.json')),
        'transliteration': step2_transliteration(
            _file(d, f'step2_transliteration.{a.pair}.json')),
    }


# ---------------------------------------------------------------- step 11

END = re.compile(r'end: (\d+) locale group\(s\), (\d+) row\(s\)')


def step11_section(a, by_name):
    d = a.out_dir
    if not a.step11:
        return {'state': 'not run', 'why': "no machines' files",
                'not_done': list(STEP11_NOT_DONE)}
    summary = _file(d, f'measured_order_summary.{a.pair}.txt')
    if not os.path.isfile(summary) or not os.path.getsize(summary):
        return not_reported(summary)
    vpath = _file(d, f'measured_order_verdicts.{a.pair}.json')
    if not os.path.isfile(vpath):
        return not_reported(vpath)
    v = json.loads(_read(vpath))
    if not isinstance(v, dict) or set(v) != {'locales', 'measurements',
                                             'step'} or v['step'] != 11:
        raise ReportError(f'{vpath} is not what step 11 writes')
    verdicts = {}
    for name, got in sorted(v['locales'].items()):
        if not (isinstance(got, list) and len(got) == 2
                and got[0] in VERDICTS):
            raise ReportError(f'{vpath}: {name} has the verdict {got!r}')
        verdicts[name] = {'verdict': got[0], 'words': got[1]}
    dpath = _file(d, f'step11.{a.pair}.data.json')
    counts = (step11_counts(dpath) if os.path.isfile(dpath)
              else not_reported(dpath))
    findings = (step11_findings(dpath) if os.path.isfile(dpath)
                else not_reported(dpath))
    checks = (step11_checks(dpath) if os.path.isfile(dpath)
              else not_reported(dpath))
    cpath = _file(d, f'measured_order_characters.{a.pair}.tsv')
    if os.path.isfile(cpath):
        head, rows, tail = tsv_rows(cpath, CHARACTER_COLUMNS)
        m = END.fullmatch(tail[-1]) if tail else None
        if not m or int(m[2]) != len(rows) or \
                int(m[1]) != len({r[0] for r in rows}):
            raise ReportError(f'{cpath} does not end with the count of its '
                              'rows and groups: cut short')
        characters = {'state': 'reported', 'columns': CHARACTER_COLUMNS,
                      'rows': rows}
    else:
        characters = not_reported(cpath)
    sql = sorted(glob.glob(glob.escape(
        _file(d, f'measured_order_confirm.{a.pair}')) + '.*.sql'))
    sql_files = []
    for p in sql:
        enc = os.path.basename(p)[len(f'measured_order_confirm.{a.pair}.'):-4]
        if not re.fullmatch(r'[A-Z0-9_]+', enc):
            continue        # another pair's, whose name extends this one
        sql_files.append({'encoding': enc, 'file': os.path.basename(p)})
    sql = (step11_sql(dpath, sql_files) if os.path.isfile(dpath)
           else not_reported(dpath))
    cross = _file(d, f'measured_order_rules.{a.pair}.txt')
    cross_log = _file(d, f'measured_order_rules.{a.pair}.log')
    written = os.path.isfile(cross) and os.path.getsize(cross)
    # Where step 2 compared nothing, or not the machines' own sources,
    # locale_order.rules_beside says so in its file (exit 0): the file is
    # there, and no rule is beside any verdict.
    not_beside = None
    if by_name['state'] == 'reported':
        if by_name['nothing']['state'] != 'something compared':
            not_beside = ('step 2 compared nothing, '
                          + by_name['nothing']['what'])
        elif not by_name['machines']:
            a_, b_ = by_name['names']
            not_beside = (f"step 2 compared the {by_name['compared']} {a_} "
                          f"and {b_}, not the machines' own locale sources")
    if written and not_beside:
        beside = {'state': 'not set beside', 'why': not_beside,
                  'file': os.path.basename(cross)}
    elif written:
        beside = {'state': 'set beside', 'file': os.path.basename(cross)}
    else:
        why = (_lines(cross_log) if os.path.isfile(cross_log) else [])
        beside = {'state': 'not reported',
                  'why': why or [f'no {os.path.basename(cross_log)}']}
    return {
        'state': 'reported',
        'measurements': v['measurements'],
        'verdicts': verdicts,
        'characters': characters,
        'sql': sql,
        'rules_beside': beside,
        'counts': counts,
        'findings': findings,
        'checks': checks,
    }


# The counts step 11 writes (locale_order.step11_counts), beside the state.
COUNT_KEYS = ['changed', 'unchanged', 'unchanged_as_far_as_measured',
              'not_measured', 'refused', 'only_old', 'only_new']


def step11_data(path):
    """The file step 11 wrote to PATH (locale_order.write_counts): its
    counts, its findings, its checks and what it says of the SQL files."""
    try:
        got = json.loads(_read(path))
    except ValueError as e:
        raise ReportError(f'{path} is not what step 11 writes: {e}')
    if not (isinstance(got, dict)
            and set(got) == {'step', 'counts', 'findings', 'checks', 'sql'}
            and got['step'] == 11 and isinstance(got['counts'], dict)
            and 'state' not in got['counts']
            and isinstance(got['findings'], dict)
            and got['findings'].get('state') == 'reported'
            and isinstance(got['checks'], dict)
            and 'state' not in got['checks']
            and isinstance(got['sql'], dict)):
        raise ReportError(f'{path} is not what step 11 writes')
    return got


def step11_counts(path):
    """The counts step 11 wrote to PATH, as a block of the report: load()
    checks every one of them."""
    return {'state': 'reported', **step11_data(path)['counts']}


def step11_findings(path):
    """The findings step 11 wrote to PATH (locale_order.change_groups,
    and compare()'s tables_only), as a block of the report: load() checks
    every one of them."""
    return step11_data(path)['findings']


def step11_sql(path, on_disk):
    """What step 11 wrote to PATH of its SQL files (locale_order.sql_data),
    as a block of the report: each file it printed, with its number of
    tests and its name in the run's folder, and each entry no file
    checks, with its reason, what it is about (SQL_ABOUT) and, where the
    report prints it, what else PostgreSQL calls those locales.
    ON_DISK is [{'encoding', 'file'}], the files the folder holds by
    encoding. audit.sh always asks for the files, so a record of a run not
    asked for them is refused, and so are files printed that the folder
    does not hold, or held and not printed, or in another order than the
    folder's, and each entry not whole
    (_sql, which load() runs too)."""
    sql = step11_data(path)['sql']
    if sql.get('state') != 'written':
        raise ReportError(f'{path}: step 11 says the SQL files are '
                          f'{sql.get("state")!r}, but audit.sh asks for them')
    if not isinstance(sql.get('files'), list) or not all(
            isinstance(f, dict) for f in sql['files']):
        raise ReportError(f'{path}: sql.files is {sql.get("files")!r}')
    printed = [f.get('encoding') for f in sql['files']]
    held = [f['encoding'] for f in on_disk]
    if printed != held:
        raise ReportError(f'{path}: step 11 printed the SQL files of '
                          f'{printed}, the folder holds those of {held}')
    if set(sql) != {'state', 'files', 'not_checked'} or any(
            set(f) != {'encoding', 'tests'} for f in sql['files']):
        raise ReportError(f'{path}: sql is {sql!r}')
    out = {'state': 'reported',
           'files': [{'encoding': d['encoding'], 'file': d['file'],
                      'tests': f['tests']}
                     for f, d in zip(sql['files'], on_disk)],
           'not_checked': sql['not_checked']}
    _sql(f'{path}: sql', out)
    return out


def step11_checks(path):
    """The checks step 11 wrote to PATH (locale_order.write_counts), as a
    block of the report: load() checks every one of them."""
    return {'state': 'reported', **step11_data(path)['checks']}


# What C or POSIX measured on a machine (locale_order.CONTROL_STATES) and
# what a machine's PostgreSQL processes come to (locale_order.LIBC_RESULTS).
CONTROL_STATES = ('byte order', 'not byte order', 'not in the file',
                  'not measured')
LIBC_RESULTS = ('clean', 'no process found', 'flagged')
# What each result's line says beside it (locale_order.libc_data), and
# the reasons a process found by its name was not checked
# (locale_order.LIBC_NAMED).
LIBC_BESIDE = {'clean': ('processes', 'looked_for'),
               'no process found': ('looked_for',), 'flagged': ()}
LIBC_NAMED_REASONS = ('could not be read', 'show no C library')
# The kinds of `!!` the three checks print (locale_order.control_texts,
# money_texts, libc_texts), which _checks holds to them.
CHECK_KINDS = ('control_failed', 'control_not_checked', 'money_earlier_format',
               'money_changed', 'money_not_checked', 'libc_earlier_format',
               'glibc_replaced', 'libc_stale', 'libc_other',
               'libc_not_checked', 'libc_no_process')


# What an entry no SQL file checks is about (locale_order.confirm_groups):
# the order of one group of step 11's findings, the character rules
# (LC_CTYPE) of some locales or of every one, or locales step 11 could
# not measure, glibc refuses or the new machine lacks.
SQL_ABOUT = ('order', 'ctype', 'unknown')


def _sql_groups_held(where, entries, groups):
    """Each entry no SQL file checks that is about a group's order names
    one group of step 11's findings, as the SQL files write its names
    (locale_order.ascii_name), and no group has two."""
    import locale_order
    named = [[locale_order.ascii_name(x) for x in g['locales']]
             for g in groups]
    seen = set()
    for i, x in enumerate(entries):
        if x['about'] != 'order':
            continue
        if x['names'] not in named:
            raise ReportError(f'{where}[{i}] is about the order of '
                              f'{x["names"]!r}, which is no group of '
                              'step 11')
        k = named.index(x['names'])
        if k in seen:
            raise ReportError(f'{where}[{i}] is a second entry of group {k}')
        seen.add(k)


def _sql(where, s):
    """step 11's SQL files (step11_sql): each file's encoding, its name in
    the run's folder, which is that encoding's, and its number of tests,
    one or more (a file is written only with a test); and each entry no
    file checks, its names, one or more, none empty, its reason, what it
    is about (SQL_ABOUT) and what else PostgreSQL calls those locales,
    words, where it says (the key left out where not: a report holds no
    null)."""
    _keys(where, s, ['state', 'files', 'not_checked'])
    if not isinstance(s['files'], list):
        raise ReportError(f'{where}.files is {s["files"]!r}')
    for i, f in enumerate(s['files']):
        w = f'{where}.files[{i}]'
        _keys(w, f, ['encoding', 'file', 'tests'])
        if not (_is_str(f['encoding'])
                and re.fullmatch(r'[A-Z0-9_]+', f['encoding'])):
            raise ReportError(f'{w}.encoding is {f["encoding"]!r}')
        if not (_is_str(f['file'])
                and f['file'].endswith(f'.{f["encoding"]}.sql')):
            raise ReportError(f'{w}.file is {f["file"]!r}')
        if not _is_positive(f['tests']):
            raise ReportError(f'{w}.tests is {f["tests"]!r}')
    if not isinstance(s['not_checked'], list):
        raise ReportError(f'{where}.not_checked is {s["not_checked"]!r}')
    for i, g in enumerate(s['not_checked']):
        w = f'{where}.not_checked[{i}]'
        _keys(w, g, ['names', 'why', 'about']
              + (['also'] if 'also' in g else []))
        if g['about'] not in SQL_ABOUT:
            raise ReportError(f'{w}.about is {g["about"]!r}')
        if not _is_strs(g['names']):
            raise ReportError(f'{w}.names is {g["names"]!r}')
        if not _is_str(g['why']):
            raise ReportError(f'{w}.why is {g["why"]!r}')
        if 'also' in g and not _is_str(g['also']):
            raise ReportError(f'{w}.also is {g["also"]!r}')


def _check_bangs(c):
    """[(kind, data)]: the `!!` the checks C print, as their text prints
    them from the same data, in no particular order."""
    out = []
    ctl = c['control']
    for side in ('old', 'new'):
        names = [n for n in ('C', 'POSIX')
                 if ctl[side][n]['state'] == 'not byte order']
        if names:
            out.append(('control_failed', {'side': side, 'names': names}))
    absent = [{'name': n, 'side': s} for s in ('old', 'new')
              for n in ('C', 'POSIX')
              if ctl[s][n]['state'] == 'not in the file']
    unmeasured = [{'name': n, 'side': s, 'error': ctl[s][n]['error']}
                  for s in ('old', 'new') for n in ('C', 'POSIX')
                  if ctl[s][n]['state'] == 'not measured']
    if absent or unmeasured:
        out.append(('control_not_checked',
                    {'absent': absent, 'unmeasured': unmeasured}))
    m = c['money']
    if m['state'] == 'earlier format':
        out.append(('money_earlier_format', {'side': m['side']}))
    else:
        if m['changed']:
            out.append(('money_changed', {'locales': m['changed']}))
        if m['not_checked']:
            out.append(('money_not_checked', {'locales': m['not_checked']}))
    lc = c['postgres_libc']
    earlier = [s for s in ('old', 'new') if lc[s]['state'] == 'earlier format']
    if earlier:
        out.append(('libc_earlier_format',
                    {'side': 'both' if len(earlier) == 2 else earlier[0]}))
    for s in ('old', 'new'):
        p = lc[s]
        if p['state'] == 'earlier format':
            continue
        if p['replaced']:
            out.append(('glibc_replaced', {'side': s}))
        if p['stale']:
            out.append(('libc_stale', {'side': s, 'processes': p['stale']}))
        if p['other']:
            out.append(('libc_other', {'side': s, 'processes': p['other']}))
        w = p['not_checked']
        if w['machine'] or w['named'] or w['unnamed']:
            out.append(('libc_not_checked', dict(w, side=s)))
        if p['result'] == 'no process found':
            out.append(('libc_no_process',
                        {'side': s, 'looked_for': p['looked_for']}))
    return out


def _checks(where, c, counts, warned):
    """step 11's checks beside the order (locale_order.control_data,
    money_data, libc_data): every key and state; the decimals of money
    compared, the locales on both machines less those not checked, as
    the counts of the same run count them; each machine's keys as its
    result picks them (LIBC_BESIDE), the processes found no fewer than
    those its reasons and its lines name; each result and the note as the
    data decide them; and the `!!` of step 11's warnings of the three
    checks exactly those the data print (_check_bangs), where step 11
    reported its warnings, and the money compared only where it reported
    its counts; upper(), lower() and ILIKE (use_texts), held by _use;
    the character rules (ctype_texts, euc_texts, utf8_texts), held by
    _ctype, of the locales _use gives a line; and, where step 11
    compared the character rules, reported its warnings and checked
    upper(), lower() and ILIKE, wholly or in part, the machine whose
    converter of EUC does not open, held by _euc_ascii_sides; and the
    line on the character sets, its state and keys (_charmaps_shape),
    which load() holds to the verdicts, the groups and the `!!` of the
    same run (_charmaps_held)."""
    _keys(where, c, ['state', 'control', 'money', 'postgres_libc',
                     'postgres_use', 'ctype', 'charmaps'])
    _charmaps_shape(f'{where}.charmaps', c['charmaps'])
    _use(f'{where}.postgres_use', c['postgres_use'], counts, warned)
    _ctype(f'{where}.ctype', c['ctype'], counts, warned)
    _euc_ascii_sides(where, c, warned)
    # The same locales, those on both machines, given a line by each.
    u, t = c['postgres_use'], c['ctype']
    if 'lines' in u and 'lines' in t and set(u['lines']) != set(t['lines']):
        raise ReportError(f'{where}.ctype.lines and postgres_use.lines name '
                          'other locales')
    w = f'{where}.control'
    _keys(w, c['control'], ['old', 'new'])
    for side in ('old', 'new'):
        _keys(f'{w}.{side}', c['control'][side], ['C', 'POSIX'])
        for name in ('C', 'POSIX'):
            x = c['control'][side][name]
            at = f'{w}.{side}.{name}'
            if not isinstance(x, dict):
                raise ReportError(f'{at} is {x!r}')
            _one_of(f'{at}.state', x.get('state'), CONTROL_STATES)
            if x['state'] == 'not measured':
                _keys(at, x, ['state', 'error'])
                if not _is_str(x['error']):
                    raise ReportError(f'{at}.error is {x["error"]!r}')
            else:
                _keys(at, x, ['state'])

    def money(w, m):
        _keys(w, m, ['state', 'compared', 'changed', 'not_checked'])
        if not _is_count(m['compared']):
            raise ReportError(f'{w}.compared is {m["compared"]!r}')
        if not _maybe(_objects(name=_is_str, old=_SCALE, new=_SCALE))(
                m['changed']) or any(x['old'] == x['new']
                                     for x in m['changed']):
            raise ReportError(f'{w}.changed is {m["changed"]!r}')
        if not _maybe(_objects(name=_is_str, side=_SIDE, why=_is_str))(
                m['not_checked']):
            raise ReportError(f'{w}.not_checked is {m["not_checked"]!r}')
        names = [x['name'] for x in m['changed']]
        pairs = [(x['name'], x['side']) for x in m['not_checked']]
        unread = {x['name'] for x in m['not_checked']}
        if len(set(names)) != len(names) or len(set(pairs)) != len(pairs) \
                or unread & set(names):
            raise ReportError(f'{w} names a locale twice')
        if counts['state'] == 'reported':
            both = (counts['machines']['old']['locales']
                    - counts['only_old'])
            if m['compared'] != both - len(unread):
                raise ReportError(
                    f'{w}.compared is {m["compared"]}, not the {both} '
                    f'locale(s) on both machines less the {len(unread)} not '
                    'checked')
    _stated(f'{where}.money', c['money'], ('compared', 'earlier format'),
            {'compared': money,
             'earlier format': lambda w, m: (
                 _keys(w, m, ['state', 'side']),
                 _one_of(f'{w}.side', m['side'], SIDES))})
    w = f'{where}.postgres_libc'
    lc = c['postgres_libc']
    _keys(w, lc, ['old', 'new', 'note'])
    named = _maybe(_objects(
        reason=_one_of_f(*LIBC_NAMED_REASONS),
        count=_is_positive, processes=_is_positive, looked_for=_is_strs))

    def libc(at, p):
        if not isinstance(p, dict):
            raise ReportError(f'{at} is {p!r}')
        _one_of(f'{at}.result', p.get('result'), LIBC_RESULTS)
        _keys(at, p, ['state', 'replaced', 'stale', 'other', 'not_checked',
                      'result', *LIBC_BESIDE[p['result']]])
        if not (_is_bool(p['replaced'])
                and _is_count(p['stale']) and _is_count(p['other'])
                and _is_positive(p.get('processes', 1))
                and _is_strs(p.get('looked_for', ['-']))):
            raise ReportError(f'{at} is {p!r}')
        n = p['not_checked']
        _keys(f'{at}.not_checked', n, ['machine', 'named', 'unnamed'])
        if not ((n['machine'] == [] or (_is_strs(n['machine'])
                                        and len(n['machine']) == 1))
                and named(n['named']) and _is_count(n['unnamed'])
                and len({x['reason'] for x in n['named']})
                == len(n['named'])):
            raise ReportError(f'{at}.not_checked is {n!r}')
        if len({(x['processes'], tuple(x['looked_for']))
                for x in n['named']}) > 1:
            raise ReportError(f'{at}.not_checked counts two sets of '
                              'processes')
        if n['named'] and p['stale'] + p['other'] + sum(
                x['count'] for x in n['named']) > n['named'][0]['processes']:
            raise ReportError(f'{at} names more processes than the '
                              f'{n["named"][0]["processes"]} it found')
        flagged = bool(p['stale'] or p['other'] or p['replaced']
                       or n['machine'] or n['named'] or n['unnamed'])
        if flagged != (p['result'] == 'flagged'):
            raise ReportError(f'{at}.result is {p["result"]!r}, and the data '
                              f'{"flag" if flagged else "flag nothing in"} it')
    for side in ('old', 'new'):
        _stated(f'{w}.{side}', lc[side], ('checked', 'earlier format'),
                {'checked': libc, 'earlier format': []})
    if lc['note'] is not any(lc[s]['state'] == 'checked'
                             for s in ('old', 'new')):
        raise ReportError(f'{w}.note is {lc["note"]!r}')
    if warned['state'] == 'reported':
        got = sorted(json.dumps([i['kind'], i['data']], sort_keys=True)
                     for i in warned['items'] if i['kind'] in CHECK_KINDS)
        want = sorted(json.dumps(list(x), sort_keys=True)
                      for x in _check_bangs(c))
        if got != want:
            raise ReportError(f'{where} print other `!!` than step 11 '
                              f'wrote: {want!r}, not {got!r}')


# What the line without `!!` on the character sets says
# (locale_order.CHARMAPS_LINE), the `!!` printed instead of it
# (charmap_findings), and glibc's built-in locales, which neither of its
# counts holds.
CHARMAPS_LINE = ('checked', 'not needed', 'not checked')
CHARMAPS_BANGS = ('charmaps_not_checked', 'charmaps_machine_not_checked',
                  'measured_in_part')
BUILTIN = ('C', 'POSIX')
# Each verdict's words (locale_order.VERDICT_WORDS), by the verdict and what
# decided it.
VERDICT_WORDS = {
    ('changed', '-'): 'the order changes',
    ('firm', 'built in'): 'built into glibc',
    ('firm', 'no levels'): 'unchanged, since it has no levels on either '
                           'machine and glibc compares bytes on both',
    ('firm', 'no levels, not measured'): 'unchanged, since it has no levels '
                                         'on either machine and glibc '
                                         'compares bytes on both; step 11 '
                                         'could not measure it',
    ('firm', 'sort table'): 'unchanged by its compiled sort table',
    ('firm', 'sort table, not measured'): 'unchanged by its compiled sort '
                                          'table, which step 11 could not '
                                          'measure',
    ('far', '-'): 'unchanged only as far as step 11 measured it',
    ('unmeasured', '-'): 'step 11 could not measure it',
    ('refused', 'both'): 'refused by glibc on both machines',
    ('refused', 'old'): 'refused by glibc on the old machine',
    ('refused', 'new'): 'refused by glibc on the new machine',
    ('only old', '-'): 'not on the new machine',
    ('only new', '-'): 'only on the new machine',
}
_WORDS = {w: k for k, w in VERDICT_WORDS.items()}


def _charmaps_shape(where, cm):
    """The line on the character sets (locale_order.charmaps_data): its
    state, and the keys and counts of each."""
    if not isinstance(cm, dict):
        raise ReportError(f'{where} is {cm!r}')
    _one_of(f'{where}.state', cm.get('state'), CHARMAPS_LINE)
    _keys(where, cm, {'checked': ['state', 'checked', 'by_table', 'builtin'],
                      'not needed': ['state', 'by_table', 'builtin'],
                      'not checked': ['state']}[cm['state']])
    if cm['state'] == 'not checked':
        return
    if not (_is_count(cm.get('checked', 0)) and _is_count(cm['by_table'])
            and cm['builtin'] in ([], ['C'], ['POSIX'], ['C', 'POSIX'])):
        raise ReportError(f'{where} is {cm!r}')
    if cm['state'] == 'not needed' and not cm['by_table']:
        raise ReportError(f'{where} is not needed with no locale its sort '
                          'table decided')


def _verdict_basis(where, verdicts):
    """{locale: (verdict, what decided it)} of step 11's verdicts, each
    from its words (VERDICT_WORDS): words that are not its verdict's are
    refused, and so is a firm C or POSIX that is not built in, or a firm
    locale built in that is neither."""
    out = {}
    for name, x in verdicts.items():
        got = _WORDS.get(x['words'])
        if got is None or got[0] != x['verdict']:
            raise ReportError(f'{where}[{name!r}] is {x["verdict"]!r} in the '
                              f'words {x["words"]!r}')
        if x['verdict'] == 'firm' and \
                (got[1] == 'built in') != (name in BUILTIN):
            raise ReportError(f'{where}[{name!r}] is firm '
                              f'{got[1]!r}')
        out[name] = got
    return out


def _verdicts_held(where, verdicts, warned):
    """Each verdict's words, a closed state (_verdict_basis), and the
    machines a refused one names, as glibc_refused, the `!!` of the same
    run, names them: both where both refused it, whatever each refused."""
    basis = _verdict_basis(where, verdicts)
    if warned['state'] != 'reported':
        return basis
    sides = {}
    for i in warned['items']:
        if i['kind'] == 'glibc_refused':
            for x in i['data']['locales']:
                sides.setdefault(x['name'], set()).update(
                    ('old', 'new') if x['side'] == 'both' else (x['side'],))
    want = {n: 'both' if len(v) == 2 else next(iter(v))
            for n, v in sides.items()}
    got = {n: b for n, (v, b) in basis.items() if v == 'refused'}
    if got != want:
        raise ReportError(f'{where} refuse {got!r}, the `!!` of glibc '
                          f'refusing {want!r}')
    return basis


def _tables_only_held(where, only, basis, control):
    """The locales step 11 could not measure that their sort table alone
    found unchanged (locale_order.table_verdicts' 'tables only'): each one
    firm, and, C and POSIX aside, exactly the firm ones whose words say
    step 11 could not measure them."""
    firm = {n for n, (v, b) in basis.items() if v == 'firm'}
    unread = {n for n, (v, b) in basis.items()
              if b.endswith(', not measured')}
    if not set(only) <= firm or set(only) - set(BUILTIN) != unread:
        raise ReportError(f'{where} names {sorted(only)[:5]}, the verdicts '
                          f'say step 11 could not measure '
                          f'{sorted(unread)[:5]}')
    # C and POSIX are built in, measured or not: their control says
    # whether step 11 measured them (control_data). One a machine could
    # not measure is not measured; one measured as byte order on both is.
    for name in BUILTIN:
        states = {control[s][name]['state'] for s in ('old', 'new')}
        if name in firm and 'not measured' in states and name not in only:
            raise ReportError(f'{where} leaves out {name}, which a machine '
                              'could not measure')
        if name in only and states == {'byte order'}:
            raise ReportError(f'{where} names {name}, measured as byte '
                              'order on both machines')


def _closing_held(where, basis, warned):
    """The closing step 11 printed, as the verdicts decide it
    (locale_order.closing_state): 'tables' where there is a locale
    unchanged and every one is firm, else 'mixed' where one is firm by its
    sort table, else 'plain'."""
    if warned['state'] != 'reported':
        return
    got = [i['data']['state'] for i in warned['items']
           if i['kind'] == 'closing']
    firm = [b for v, b in basis.values() if v == 'firm']
    far = any(v == 'far' for v, b in basis.values())
    want = ('tables' if firm and not far else
            'mixed' if any(b.startswith('sort table') for b in firm)
            else 'plain')
    if got != [want]:
        raise ReportError(f'{where}: step 11 closes {got!r}, its verdicts '
                          f'{want!r}')


def _charmaps_held(where, cm, groups, basis, warned):
    """The line on the character sets, against the verdicts of the same
    run (BASIS, _verdict_basis), its groups and its `!!`: not printed
    exactly where a `!!` on the character sets is; the locales its sort
    table decided, C and POSIX aside, those the firm verdicts count; the
    locales checked, the changed and unchanged ones step 11 measured that
    no table decided, those the verdicts and the groups count (a changed
    one step 11 could not measure, found by its table alone, is not
    checked); C and POSIX said aside where either is firm or checked; and
    'not needed' exactly where none was checked and the tables decided
    some, and no `!!` named a locale by its bytes."""
    bangs = [i for i in warned['items'] if i['kind'] in CHARMAPS_BANGS] \
        if warned['state'] == 'reported' else None
    if bangs is not None and bool(bangs) != (cm['state'] == 'not checked'):
        raise ReportError(f'{where} is {cm["state"]!r} beside '
                          f'{len(bangs)} `!!` on the character sets')
    firm = {n for n, (v, b) in basis.items() if v == 'firm'}
    unseen = {x for g in groups for x in g['locales']
              if g['sort_table'].get('step11') == 'unmeasured'}
    compared = {n for n, (v, b) in basis.items()
                if v == 'far' or (v == 'changed' and n not in unseen)}
    other = len(firm - set(BUILTIN))
    aside = [x for x in BUILTIN if x in firm or x in compared]
    named = bangs is not None and any(i['data'].get('named_by_bytes')
                                      for i in bangs)
    due = 'not needed' if not compared and other and not named else None
    if cm['state'] == 'not checked':
        if due and bangs is not None:
            raise ReportError(f'{where} is not checked where it was not '
                              'needed')
        return
    want = {'state': due or 'checked', 'by_table': other, 'builtin': aside}
    if not due:
        want['checked'] = len(compared - set(BUILTIN))
    if cm != want:
        raise ReportError(f'{where} is {cm!r}, the verdicts and groups of '
                          f'the same run say {want!r}')


# What step 11 could check of upper(), lower() and ILIKE, what a row of a
# locale is, the line each locale goes to, and what each machine does with a
# byte sequence and with a character on the way back
# (locale_order.USE_STATES, USE_LOCALE_KINDS, USE_LINES, SEQUENCE_STATES,
# BACK_STATES); the states of a sequence a row can print, one machine at
# least refusing it (an error row in parse_characters).
USE_STATES = ('checked', 'partly', 'not checked')
USE_LOCALE_KINDS = ('error', 'not checked')
USE_LINES = ('one byte', 'left out', 'error', 'unknown', 'elsewhere',
             'converts', 'back unknown', 'none')
SEQUENCE_STATES = ('converts', 'error', 'not measured')
BACK_STATES = ('not turned back', 'turned back', 'not read', 'not measured')
USE_PAIRS = (('converts', 'error'), ('error', 'converts'), ('error', 'error'),
             ('not measured', 'error'), ('error', 'not measured'))
# The `!!` of use_texts whose locales get an error row each; the row the
# first two give (use_records' groups 'changed' and 'both') and the line on
# what converts back ('back', which has no `!!`); and what the other
# machine's column of a row of one machine's error holds, by the reason
# of its line: a machine that refused the locale or does not have it
# measured nothing; one whose result is not known measured nothing, or
# converts where a file of an earlier format tried one character
# (UNSAID), and the reader does not tell those two apart.
USE_ERROR_KINDS = ('use_error_new', 'use_error_both', 'use_error_moved',
                   'use_error_any_db', 'use_error_one_side')
USE_ERROR_ROWS = {'use_error_new': ('converts', 'error'),
                  'use_error_both': ('error', 'error'),
                  'back': ('error', 'converts')}
USE_ONE_SIDE_OTHER = {'refused': ('not measured',),
                      'absent': ('not measured',),
                      'unknown': ('not measured', 'converts')}
USE_LISTS = ['rows', 'lines', 'added', 'back', 'groups', 'back_groups',
             'back_changed', 'part_unknown', 'back_not_checked']


def _hex(v):
    return (isinstance(v, str) and v != '' and len(v) % 2 == 0
            and all(c in '0123456789abcdef' for c in v))


def _sorted_groups(where, v):
    """by_codeset_data's groups, or none: each encoding once, in order,
    its names sorted, each name once over all."""
    if not _maybe(_GROUPS)(v):
        raise ReportError(f'{where} is {v!r}')
    encs = [g['encoding'] for g in v]
    names = [x for g in v for x in g['names']]
    if encs != sorted(set(encs)) or len(names) != len(set(names)) or any(
            g['names'] != sorted(g['names']) for g in v):
        raise ReportError(f'{where} is not by encoding: {v!r}')


def _back_side_ok(x):
    """What a machine gives back of a character both turn back otherwise
    (locale_order.back_side): bytes, or none; never a refusal."""
    return isinstance(x, dict) and (
        x == {'state': 'no bytes'}
        or (set(x) == {'state', 'bytes'} and x['state'] == 'bytes'
            and _hex(x['bytes'])))


def _use_shape(where, u):
    """Every key and state of step 11's data on upper(), lower() and
    ILIKE, as its state picks them."""
    lists = {'checked': USE_LISTS + ['converts', 'one_byte', 'left_out',
                                     'elsewhere'],
             'partly': USE_LISTS + ['earlier'], 'not checked': []}
    if not isinstance(u, dict):
        raise ReportError(f'{where} is {u!r}')
    _one_of(f'{where}.state', u.get('state'), USE_STATES)
    _keys(where, u, ['state', *lists[u['state']]])
    if u['state'] == 'not checked':
        return
    if u['state'] == 'checked':
        for k in ('converts', 'one_byte', 'elsewhere'):
            if not _is_count(u[k]):
                raise ReportError(f'{where}.{k} is {u[k]!r}')
        _sorted_groups(f'{where}.left_out', u['left_out'])
    else:
        _one_of(f'{where}.earlier', u['earlier'], ('old', 'new'))
    _sorted_groups(f'{where}.back', u['back'])
    for k in ('lines', 'added'):
        if not (isinstance(u[k], dict) and all(
                _is_str(n) and v in USE_LINES for n, v in u[k].items())):
            raise ReportError(f'{where}.{k} is {u[k]!r}')
    if set(u['lines']) & set(u['added']):
        raise ReportError(f'{where} gives a locale a line twice')
    for k in USE_LISTS:
        if not isinstance(u[k], (dict if k in ('lines', 'added') else list)):
            raise ReportError(f'{where}.{k} is {u[k]!r}')
    seen = set()
    for i, x in enumerate(u['rows']):
        at = f'{where}.rows[{i}]'
        if not isinstance(x, dict):
            raise ReportError(f'{at} is {x!r}')
        _one_of(f'{at}.kind', x.get('kind'), USE_LOCALE_KINDS)
        if x['kind'] == 'error':
            _keys(at, x, ['name', 'kind', 'rows'])
            if not (isinstance(x['rows'], list) and 1 <= len(x['rows']) <= 2
                    and all(isinstance(r, dict)
                            and set(r) - {'char'} == {'bytes', 'before', 'now'}
                            and _is_count(r.get('char', 0))
                            and _hex(r['bytes'])
                            and (r['before'], r['now']) in USE_PAIRS
                            for r in x['rows'])):
                raise ReportError(f'{at}.rows is {x["rows"]!r}')
        else:
            _keys(at, x, ['name', 'kind', 'why'])
            if not _is_str(x['why']):
                raise ReportError(f'{at}.why is {x["why"]!r}')
        if not _is_str(x['name']) or (x['name'], x['kind']) in seen:
            raise ReportError(f'{at} names {x.get("name")!r} again')
        seen.add((x['name'], x['kind']))
    seq = _objects(before=_one_of_f(*SEQUENCE_STATES),
                   now=_one_of_f(*SEQUENCE_STATES),
                   bytes=lambda v: isinstance(v, list) and bool(v)
                   and all(map(_hex, v)) and v == sorted(set(v)))
    for i, g in enumerate(u['groups']):
        at = f'{where}.groups[{i}]'
        _keys(at, g, ['names', 'encoding', 'sequences'])
        if not (_is_strs(g['names']) and _is_str(g['encoding'])
                and seq(g['sequences'])):
            raise ReportError(f'{at} is {g!r}')
        pairs = [(x['before'], x['now']) for x in g['sequences']]
        every = [b for x in g['sequences'] for b in x['bytes']]
        if (any(p not in USE_PAIRS for p in pairs)
                or pairs != sorted(set(pairs)) or len(every) != len(set(every))):
            raise ReportError(f'{at}.sequences is not grouped by state')
    through = _objects(fn=_one_of_f('upper', 'lower'), to=_is_count)
    for i, g in enumerate(u['back_groups']):
        at = f'{where}.back_groups[{i}]'
        _keys(at, g, ['names', 'encoding', 'characters'])
        if not (_is_strs(g['names']) and _is_str(g['encoding'])
                and _objects(char=_is_count, through=through,
                             before=_one_of_f(*BACK_STATES),
                             now=_one_of_f(*BACK_STATES))(g['characters'])
                and all('not turned back' in (x['before'], x['now'])
                        for x in g['characters'])):
            raise ReportError(f'{at} is {g!r}')
    if not _maybe(_objects(name=_is_str, characters=_objects(
            char=_is_count, old=_back_side_ok, new=_back_side_ok)))(
                u['back_changed']) or any(
                    c['old'] == c['new'] for x in u['back_changed']
                    for c in x['characters']):
        raise ReportError(f'{where}.back_changed is {u["back_changed"]!r}')
    if not _maybe(_objects(name=_is_str, side=_SIDES))(u['part_unknown']):
        raise ReportError(f'{where}.part_unknown is {u["part_unknown"]!r}')
    if not _maybe(_objects(name=_is_str, utf8=_is_bool, side=_SIDES))(
            u['back_not_checked']):
        raise ReportError(f'{where}.back_not_checked is '
                          f'{u["back_not_checked"]!r}')


def _use_lines(where, u, counts):
    """The line each locale goes to, against the comparison and the rest
    of the data: one line for each locale on both machines (the counts of
    the same run), and no more only on the new one than it counts; the
    counts of the line on what converts those of their lines, less, for
    what converts, the locales another line names in its place; the
    locales left out, with an error and not checked those of their lines;
    each of a file before the back part named by that line."""
    def tie(what, got, want):
        if got != want:
            raise ReportError(f'{where}.{what} is {got!r}, the lines say '
                              f'{want!r}')
    every = {**u['lines'], **u['added']}

    def of(line):
        return sorted(n for n, v in every.items() if v == line)
    if counts['state'] == 'reported':
        both = counts['machines']['old']['locales'] - counts['only_old']
        if len(u['lines']) != both or len(u['added']) > counts['only_new']:
            raise ReportError(
                f'{where} gives {len(u["lines"])} locale(s) on both machines '
                f'and {len(u["added"])} only on the new one a line, not the '
                f'{both} and at most {counts["only_new"]} the counts give')
    rows = [x['name'] for x in u['rows'] if x['kind'] == 'error']
    tie('rows (errors)', sorted(rows), of('error'))
    unread = {x['name'] for x in u['rows'] if x['kind'] == 'not checked'}
    if not set(of('unknown')) <= unread or not unread - set(of('unknown')) \
            <= set(rows):
        raise ReportError(f'{where}.rows names {sorted(unread)} not checked, '
                          f'and the lines {of("unknown")}')
    if not set(of('back unknown')) <= {x['name'] for x in u['back_not_checked']}:
        raise ReportError(f'{where}.back_not_checked leaves out '
                          f'{of("back unknown")}')
    if not {n for g in u['back'] for n in g['names']} <= set(of('error')):
        raise ReportError(f'{where}.back names a locale whose line is not '
                          'an error')
    if u['state'] == 'checked':
        apart = ({n for g in u['groups'] for n in g['names']}
                 | {n for g in u['back_groups'] for n in g['names']}
                 | {x['name'] for x in u['back_changed']}
                 | {x['name'] for x in u['part_unknown']})
        tie('converts', u['converts'],
            len(set(of('converts')) - apart))
        tie('one_byte', u['one_byte'], len(of('one byte')))
        tie('elsewhere', u['elsewhere'], len(of('elsewhere')))
        tie('left_out', sorted(n for g in u['left_out'] for n in g['names']),
            of('left out'))


def _use(where, u, counts, warned):
    """step 11's data on upper(), lower() and ILIKE (locale_order.use_texts):
    its state, the keys it picks, every row's states (_use_shape); the
    line of each locale held to the comparison's locales and the counts
    of the line on what converts to those lines (_use_lines); and, where
    step 11 reported its warnings, each row tied to the `!!` printed from
    the same run: the locales of an error row exactly those of the error
    lines and of the line on what converts back, each row's states as
    its line says what each machine does; the not-checked rows those of
    the line on what is not checked, with its reasons; each group of
    sequences the counts the line on part of the text gives each
    machine, and each group of characters those of the line on a wrong
    result, through lower() too; the characters that convert back
    otherwise as many as that line counts; the rows of a file that tried
    one character those of its line, side by side; and those of a file
    before the back part as many as its line counts, of UTF-8 too, on its
    machines."""
    _use_shape(where, u)
    if u['state'] != 'not checked':
        _use_lines(where, u, counts)
    if warned['state'] != 'reported':
        return
    bangs = {}
    for i in warned['items']:
        if i['kind'].startswith('use_'):
            bangs.setdefault(i['kind'], []).append(i['data'])
    early = [d['side'] for d in bangs.get('use_earlier_format', [])]
    want = ([] if u['state'] == 'checked' else
            ['both'] if u['state'] == 'not checked' else [u['earlier']])
    if early != want:
        raise ReportError(f'{where}.state is {u["state"]!r}, and step 11 '
                          f'printed the earlier format for {early!r}')
    if u['state'] == 'not checked':
        return

    def tie(what, got, want):
        if got != want:
            raise ReportError(f'{where}.{what} is {got!r}, the `!!` say '
                              f'{want!r}')
    # Each locale of an error row, with the `!!` (or the line on what
    # converts back) it is in and the machines that line names.
    line_of = {n: ('back', 'both', None) for g in u['back']
               for n in g['names']}
    for k in USE_ERROR_KINDS:
        for d in bangs.get(k, []):
            for n in _names(d):
                line_of[n] = (k, d.get('side', 'both'), d.get('reason'))
    rows = {x['name']: x['rows'] for x in u['rows'] if x['kind'] == 'error'}
    tie('rows (errors)', sorted(rows),
        sorted([n for k in USE_ERROR_KINDS for d in bangs.get(k, [])
                for n in _names(d)] + [n for g in u['back'] for n in g['names']]))
    col = {'old': 'before', 'new': 'now'}
    for n, got in rows.items():
        kind, side, reason = line_of[n]
        states = [(r['before'], r['now']) for r in got]
        if kind in USE_ERROR_ROWS:
            ok = states == [USE_ERROR_ROWS[kind]]
        elif kind == 'use_error_one_side':
            # One machine's error; the other as the line's reason says
            # (USE_ONE_SIDE_OTHER), never an error. A row of USE_PAIRS
            # whose other machine has no error has it on the machine named.
            ok = (len(got) == 1
                  and got[0][col['old' if side == 'new' else 'new']]
                  in USE_ONE_SIDE_OTHER[reason])
        else:
            # Two encodings, a row of each machine's error: each machine
            # the line names has its row.
            named = ['old', 'new'] if side == 'both' else [side]
            ok = (len(got) <= len(named) and all(
                any(r[col[s]] == 'error' for r in got) for s in named))
        if not ok:
            raise ReportError(f'{where}.rows: {n} has the rows {states}, '
                              f'which its line {kind} on {side} does not '
                              'give')
    # The line on what is not checked is printed only where both files
    # hold the part (an earlier one returns before it).
    if u['state'] == 'checked':
        tie('rows (not checked)',
            sorted((x['name'], x['why']) for x in u['rows']
                   if x['kind'] == 'not checked'),
            sorted((n, r['why']) for d in bangs.get('use_not_checked', [])
                   for r in d['reasons'] for g in r['groups']
                   for n in g['names']))
    entry = {n: e for d in bangs.get('use_error_partial', [])
             for e in d['entries'] for n in e['names']}
    tie('groups (names)', sorted(n for g in u['groups'] for n in g['names']),
        sorted(entry))
    for i, g in enumerate(u['groups']):
        for n in g['names']:
            part = [p for p in entry[n]['parts']
                    if p['encoding'] == g['encoding']]
            if len(part) != 1:
                raise ReportError(f'{where}.groups[{i}] is of '
                                  f'{g["encoding"]}, which the `!!` do '
                                  f'not give {n}')
            got = {x['side']: sum(len(s['bytes']) for s in g['sequences']
                                  if s[col[x['side']]] == 'error')
                   for x in part[0]['sides']}
            tie(f'groups[{i}] ({n})', got,
                {x['side']: x['count'] for x in part[0]['sides']})
            if part[0]['alike'] == 'same' and any(
                    (s['before'] == 'error') != (s['now'] == 'error')
                    for s in g['sequences']):
                raise ReportError(f'{where}.groups[{i}]: the `!!` say both '
                                  'machines refuse the same sequences')
    wrong = {n: e for d in bangs.get('use_wrong_result', [])
             for e in d['entries'] for n in e['names']}
    tie('back_groups (names)',
        sorted(n for g in u['back_groups'] for n in g['names']), sorted(wrong))
    lowered = {}
    for i, g in enumerate(u['back_groups']):
        for n in g['names']:
            part = [p for p in wrong[n]['parts']
                    if p['encoding'] == g['encoding']]
            if len(part) != 1:
                raise ReportError(f'{where}.back_groups[{i}] is of '
                                  f'{g["encoding"]}, which the `!!` do '
                                  f'not give {n}')
            tie(f'back_groups[{i}] ({n})',
                {x['side']: sum(c[col[x['side']]] == 'not turned back'
                                for c in g['characters'])
                 for x in part[0]['sides']},
                {x['side']: x['count'] for x in part[0]['sides']})
            for side in ('old', 'new'):
                lowered.setdefault(n, {}).setdefault(side, set()).update(
                    c['char'] for c in g['characters']
                    if c[col[side]] == 'not turned back'
                    and any(t['fn'] == 'lower' for t in c['through']))
    for n, e in wrong.items():
        sides = {x['side'] for p in e['parts'] for x in p['sides']}
        if len(e['lower']) == 1:
            tie(f'back_groups ({n}, through lower())',
                {len(lowered[n][s]) for s in sides}, {e['lower'][0]['count']})
        else:
            tie(f'back_groups ({n}, through lower())',
                {s: len(lowered[n][s]) for s in sides},
                {x['side']: x['count'] for x in e['lower']})
    tie('back_changed',
        sorted((x['name'], len(x['characters'])) for x in u['back_changed']),
        sorted((n, e['count']) for d in bangs.get('use_back_changed', [])
               for e in d['entries'] for n in e['names']))
    tie('part_unknown',
        sorted((x['name'], x['side']) for x in u['part_unknown']),
        sorted((n, p['side']) for d in bangs.get('use_partial_unknown', [])
               for p in d['places'] for g in p['groups'] for n in g['names']))
    b = u['back_not_checked']
    got = ([] if not b else [{
        'count': len(b), 'utf8': sum(x['utf8'] for x in b),
        'side': 'both' if (len({x['side'] for x in b}) > 1
                           or b[0]['side'] == 'both') else b[0]['side']}])
    tie('back_not_checked', got,
        [{'count': sum(x['count'] for x in d['encodings']),
          'utf8': d['utf8'], 'side': d['side']}
         for d in bangs.get('use_back_not_checked', [])])


# ---------------------------------------------------------------- warnings

# The two lists a `!!` goes to: what changed between the two sides or
# what PostgreSQL will not report between them, and what this run could
# not check.
WARNING_CLASSES = ('change', 'not checked')
WARNING_KEYS = ['kind', 'class', 'title', 'data', 'text']


def _is_str(v):
    return isinstance(v, str) and v != ''


def _is_bool(v):
    return isinstance(v, bool)


def _is_strs(v):
    return isinstance(v, list) and bool(v) and all(_is_str(x) for x in v)


def _is_count(v):
    """A whole number, 0 or more (a bool is not one)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_positive(v):
    return _is_count(v) and v > 0


def _objects(**spec):
    """A list, not empty, of objects with exactly spec's keys."""
    def check(v):
        return (isinstance(v, list) and bool(v)
                and all(isinstance(o, dict) and set(o) == set(spec)
                        and all(spec[k](o[k]) for k in spec) for o in v))
    return check


def _one_of_f(*allowed):
    return lambda v: v in allowed


# verify_tag's statuses but good and bad, each with what the title says.
SIGNATURE_WHY = {
    'no-gpg': 'gpg is not installed',
    'no-key': 'not verified: no signing key, or gpg failed',
    'unverifiable': 'gpg could not check the signature',
    'revoked-key': 'the signing key is revoked',
    'expired-key': 'the signing key has expired',
    'expired-sig': 'the signature has expired',
    'untrusted': 'the signing key is not trusted',
    'unsigned': 'not signed',
    'not-a-tag': 'a commit or a lightweight tag, which carries no signature',
}


def _signatures_title(d):
    refs = [r['ref'] for r in d['refs']]
    whys = list(dict.fromkeys(SIGNATURE_WHY[r['status']] for r in d['refs']))
    what = ('Signatures' if any(r['status'] == 'not-a-tag'
                                for r in d['refs']) else 'Tag signatures')
    return f"{what} of {', '.join(refs)} ({'; '.join(whys)})"


def _fallback_title(d):
    u = d['unread']
    if len(u) == 1:
        return (f"The {u[0]['side']} machine's build {u[0]['build']} could "
                f"not be read; step 2 read the tags")
    return "Neither machine's build could be read; step 2 read the tags"


def _tags_title(d):
    refs, classes = d['refs'], d['classes']
    one = len(refs) == 1
    if len(set(classes)) == 1:
        k = classes[0]
        what = f'is an upstream {k}' if one else f'are upstream {k}s'
    else:
        what = (f'are an upstream {classes[0]} and an upstream '
                f'{classes[1]}')
    return (f"The distro's own locale files and patches "
            f"({' and '.join(refs)} {what})")


def _n(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _skipped_title(d):
    n = len(d['entries'])
    return (f"{n} entr{'y' if n == 1 else 'ies'} of {d['machine']}'s files "
            f"not read")


_PAIR = {'old': _is_str, 'new': _is_str}
_SIDE = _one_of_f('old', 'new')


def _locales(names):
    """'1 locale' or 'N locales', of the names in `names`."""
    return _n(len(names), 'locale', 'locales')


def _maybe(check):
    """check, or an empty list."""
    return lambda v: v == [] or check(v)


# Step 11's sides in one word (both machines too), the two locales of
# its control, and the decimals cash.c keeps (money_scale).
_SIDES = _one_of_f('old', 'new', 'both')
_CONTROL = _one_of_f('C', 'POSIX')


def _SCALE(v):
    return _is_count(v) and v <= 10


def _machines(sides):
    """'both machines' or 'the old machine', of the sides named."""
    sides = set(sides)
    if sides == {'both'} or sides == {'old', 'new'}:
        return 'both machines'
    return f"the {sides.pop()} machine"


# Step 11's lines on upper(), lower() and ILIKE (use_texts): the locales
# of a line by encoding (by_codeset_data), a reason given or not (a list of
# none or one), what each machine refuses of an encoding (lacking_data),
# and the count through lower() (one, or one a machine).
_GROUPS = _objects(encoding=_is_str, names=_is_strs)
_ALIKE = ('same', 'as many', 'mixed', 'within', 'one side')
_LACKING = _objects(encoding=_is_str, alike=_one_of_f(*_ALIKE),
                    sides=_objects(side=_SIDE, count=_is_positive))
_LOWER = _objects(side=_SIDES, count=_is_count)
_UNKNOWN_ON = _one_of_f([], ['new'], ['old'], ['new', 'old'])
_FN = 'upper(), lower() and ILIKE'


def _why(v):
    """A reason given, or none: a list of one, or empty."""
    return v == [] or (_is_strs(v) and len(v) == 1)


def _names(d):
    """Every name a line of use_texts lists, in its groups, entries,
    places or reasons."""
    if 'groups' in d:
        return [x for g in d['groups'] for x in g['names']]
    if 'entries' in d:
        return [x for e in d['entries'] for x in e['names']]
    if 'places' in d:
        return [x for p in d['places'] for g in p['groups']
                for x in g['names']]
    return [x for r in d['reasons'] for g in r['groups'] for x in g['names']]


def _use_locales(d):
    return _locales(set(_names(d)))


def _locales_n(n):
    return _n(n, 'locale', 'locales')


def _distinct(d):
    """Each name once in the line: one locale goes in one place."""
    names = _names(d)
    return len(names) == len(set(names))


def _part(p):
    """What one machine or two refuse of an encoding, as lacking_data
    writes it: one side alone, or old and new told apart by alike."""
    sides = [x['side'] for x in p['sides']]
    counts = [x['count'] for x in p['sides']]
    if p['alike'] == 'one side':
        return len(sides) == 1
    if sides != ['old', 'new']:
        return False
    return (counts[0] == counts[1]) == (p['alike'] in ('same', 'as many'))


def _parts(e):
    """One part, or two of one machine each and another encoding (the
    new machine's first, as use_texts sorts them)."""
    ps = e['parts']
    if not all(map(_part, ps)):
        return False
    if len(ps) == 1:
        return True
    return (len(ps) == 2 and all(p['alike'] == 'one side' for p in ps)
            and [p['sides'][0]['side'] for p in ps] == ['new', 'old']
            and ps[0]['encoding'] != ps[1]['encoding'])


def _lower(e):
    """One count through lower(), of the machines the entry's parts name
    (both, or the one), or the old machine's and the new one's where the
    parts name both: one count's side is in no text, so it is held to the
    parts here."""
    sides = [x['side'] for x in e['lower']]
    named = sorted({x['side'] for p in e['parts'] for x in p['sides']})
    if len(sides) == 1:
        return sides[0] == ('both' if len(named) == 2 else named[0])
    return sides == ['old', 'new'] and len(named) == 2


def _one_side_title(d):
    other = 'old' if d['side'] == 'new' else 'new'
    said = {'refused': f"that glibc refused on the {other} machine",
            'absent': "the old machine does not have",
            'unknown': f"not known on the {other} machine"}[d['reason']]
    return (f"{_FN} give an error on the {d['side']} machine in "
            f"{_use_locales(d)} {said}")


# Step 11's lines on the character rules (ctype_texts, euc_texts,
# utf8_texts) and on a database of another encoding (other_encoding_text):
# a character as 'U+0282', a byte as 'E0', a byte sequence as 'AD E2'; the
# characters of a change with what the line says of each; how a line names
# its locales (ctype_group_data: how many, and each by name, or none where
# they are all those compared).
_C = 'Character rules (LC_CTYPE)'
_EUC_DB = f'{_C} in an EUC database'


def _is_cp(v):
    return isinstance(v, str) and re.fullmatch(r'U\+[0-9A-F]{4,8}', v) is not None


def _is_byte(v):
    return isinstance(v, str) and re.fullmatch(r'[0-9A-F]{2}', v) is not None


def _is_bytes(v):
    return (isinstance(v, str)
            and re.fullmatch(r'[0-9A-F]{2}( [0-9A-F]{2})*', v) is not None)


_CLASSIFIED = _maybe(_objects(character=_is_cp, change=_is_str))
_SEQUENCES = _objects(bytes=_is_bytes, old=_is_cp, new=_is_cp)


# What step 11 could compare of the character rules, the line each
# locale goes to, and the classes a character or byte is in
# (locale_order.CTYPE_DATA_STATES, CTYPE_LINES, CTYPE_CLASSES, in that order).
CTYPE_DATA_STATES = ('compared', 'earlier format')
CTYPE_LINES = ('utf8', 'other encoding', 'euc', 'built in', 'refused',
               'not checked')
CTYPE_CLASSES = ('alpha', 'upper', 'lower', 'digit', 'space', 'punct',
                 'alnum', 'print', 'graph', 'xdigit')
# The lines each kind of group compares (locale_order.CTYPE_COMPARED): the
# characters where a UTF8 database can use the locale, the bytes in every
# locale compared.
CTYPE_COMPARED = {'character_groups': ('utf8',),
                  'byte_groups': ('utf8', 'other encoding', 'euc')}


def _ctype_change(at, x, unit, value):
    """One change of a group (locale_order.ctype_grouped): its characters
    or bytes, at least one, classified for characters; each of upper,
    lower and classes that changed, one at least, old not new; the
    classes each once, in CTYPE_CLASSES' order."""
    head = ['chars', 'classified'] if unit == 'char' else ['bytes']
    rest = [k for k in ('upper', 'lower', 'classes') if k in x]
    _keys(at, x, head + rest)
    us = x[unit + 's']
    if not rest or not (isinstance(us, list) and us
                        and all(map(value, us))) or (
            unit == 'char' and not _is_bool(x['classified'])):
        raise ReportError(f'{at} is {x!r}')
    for k in rest:
        v = x[k]
        _keys(f'{at}.{k}', v, ['old', 'new'])
        if k == 'classes':
            ok = all(isinstance(s, list)
                     and all(c in CTYPE_CLASSES for c in s)
                     and len(set(s)) == len(s)
                     and s == sorted(s, key=CTYPE_CLASSES.index)
                     for s in (v['old'], v['new']))
        else:
            ok = value(v['old']) and value(v['new'])
        if not ok or v['old'] == v['new']:
            raise ReportError(f'{at}.{k} is {v!r}')


def _ctype(where, c, counts, warned):
    """step 11's data on the character rules (locale_order.ctype_texts):
    its state; where compared, the line of
    each locale on both machines (where step 11 reported its counts, as
    many as they give, those glibc refuses as many as they count; C and
    POSIX built in where glibc does not refuse them and no other), each
    group's locales
    once, in order, of the lines its kind compares, the groups in the
    order printed and each change as _ctype_change reads it; in an EUC
    database, as _ctype_euc reads them; the locales of UTF-8 under the
    rows of the line on UTF-8's converter, as _ctype_utf8 reads them;
    and, where step 11 reported its warnings, the earlier format exactly
    where its `!!` is, each group exactly the `!!` printed of it (its
    locales, all those compared or each by name, how many change, which
    the old machine classified, which bytes), the locales not checked
    exactly those its `!!` names, those of EUC those the lines on EUC
    name that a UTF8 database cannot use, each group of EUC exactly its
    `!!` (its locales, how many change, which the old machine
    classified), those compared for ASCII alone among those the lines on
    EUC say change and none of those they say are the same for ASCII,
    each locale and side of EUC not checked exactly its `!!`'s with its
    reason as a sentence starts with it (_sentence), and as many locales
    of UTF-8 as the line on UTF-8's converter counts. Which locales a
    UTF8 database can use no other fact of the run says: where no group
    of characters names them all, their number is held only as part of
    those compared; nor which are of UTF-8 on both machines: held only
    by their number and their line."""
    if not isinstance(c, dict):
        raise ReportError(f'{where} is {c!r}')
    _one_of(f'{where}.state', c.get('state'), CTYPE_DATA_STATES)
    if c['state'] == 'earlier format':
        _keys(where, c, ['state', 'utf8'])
    else:
        _keys(where, c, ['state', 'lines', 'character_groups',
                         'byte_groups', 'euc', 'utf8'])
        lines = c['lines']
        if not (isinstance(lines, dict) and all(
                _is_str(n) and v in CTYPE_LINES for n, v in lines.items())):
            raise ReportError(f'{where}.lines is {lines!r}')
        for key, unit, value in (('character_groups', 'char', _is_cp),
                                 ('byte_groups', 'byte', _is_byte)):
            _ctype_groups(f'{where}.{key}', c[key], lines,
                          CTYPE_COMPARED[key], unit, value)
        _ctype_euc(f'{where}.euc', c['euc'], lines)
        builtin = {n for n, v in lines.items() if v == 'built in'}
        if builtin != {n for n in ('C', 'POSIX')
                       if lines.get(n, 'refused') != 'refused'}:
            raise ReportError(f'{where}.lines give {sorted(builtin)} built '
                              'in, which are C and POSIX where glibc does '
                              'not refuse them')
        if counts['state'] == 'reported':
            both = counts['machines']['old']['locales'] - counts['only_old']
            refused = sum(1 for v in lines.values() if v == 'refused')
            if len(lines) != both or refused != counts['refused']:
                raise ReportError(
                    f'{where} gives {len(lines)} locale(s) a line, '
                    f'{refused} refused, not the {both} on both machines '
                    f'and the {counts["refused"]} refused the counts give')
    _ctype_utf8(f'{where}.utf8', c['utf8'], c.get('lines'))
    if warned['state'] != 'reported':
        return
    bangs = {}
    for i in warned['items']:
        bangs.setdefault(i['kind'], []).append(i['data'])
    # The locales of UTF-8 the rows of the converter's line are under:
    # as many as that line counts, and none where it is not printed.
    said = bangs.get('utf8_changed', []) + bangs.get('utf8_one_side', [])
    if len(c['utf8']['names']) != sum(d['locales'] for d in said):
        raise ReportError(f'{where}.utf8.names are {c["utf8"]["names"]!r}, '
                          f'the `!!` count {[d["locales"] for d in said]}')
    early = len(bangs.get('ctype_earlier_format', []))
    if early != (c['state'] == 'earlier format'):
        raise ReportError(f'{where}.state is {c["state"]!r}, and step 11 '
                          f'printed the earlier format {early} time(s)')
    if c['state'] == 'earlier format':
        return

    def tie(what, got, want):
        got = sorted(json.dumps(x, sort_keys=True) for x in got)
        want = sorted(json.dumps(x, sort_keys=True) for x in want)
        if got != want:
            raise ReportError(f'{where}.{what} are {want!r}, the `!!` say '
                              f'{got!r}')
    lines = c['lines']
    compared = {k: sum(1 for v in lines.values() if v in kinds)
                for k, kinds in CTYPE_COMPARED.items()}

    def group(g, key):
        return {'locales': len(g['names']),
                'names': ([] if len(g['names']) == compared[key]
                          else g['names'])}

    def units(g, unit, only=lambda x: True):
        return sorted((u for x in g['changes'] if only(x)
                       for u in x[unit + 's']),
                      key=lambda u: int(u[2:] if unit == 'char' else u, 16))
    tie('character_groups',
        [dict({k: d[k] for k in ('locales', 'names', 'count')},
              classified=[x['character'] for x in d['classified']])
         for d in bangs.get('ctype_characters_changed', [])],
        [dict(group(g, 'character_groups'),
              count=len(units(g, 'char')),
              classified=units(g, 'char', lambda x: x['classified']))
         for g in c['character_groups']])
    tie('byte_groups',
        [dict({k: d[k] for k in ('locales', 'names')},
              bytes=[x['byte'] for x in d['bytes']])
         for d in bangs.get('ctype_bytes_changed', [])],
        [dict(group(g, 'byte_groups'), bytes=units(g, 'byte'))
         for g in c['byte_groups']])
    tie('lines (not checked)',
        [sorted({x['name'] for d in bangs.get('ctype_not_checked', [])
                 for x in d['locales']})],
        [sorted(n for n, v in lines.items() if v == 'not checked')])
    # The locales of EUC the line of the other encodings points at (its
    # 'or, in EUC'): exactly those the lines on EUC name, less those a
    # UTF8 database can use too. One of them on any other line, or one on
    # this line they do not name, is refused.
    euc = set()
    for k in ('euc_sequences_changed', 'euc_characters_changed', 'euc_same',
              'euc_ascii_same'):
        for d in bangs.get(k, []):
            euc |= {n for g in d['groups'] for n in g['names']}
    for d in bangs.get('euc_not_checked', []):
        euc |= {n for r in d['reasons'] for g in r['groups']
                for n in g['names']}

    def of(*kinds):
        return {n for n, v in lines.items() if v in kinds}
    if of('euc') != euc - of('utf8'):
        raise ReportError(f'{where}.lines give EUC to {sorted(of("euc"))}, '
                          f'and the lines on EUC name {sorted(euc)}')
    e = c['euc']
    tie('euc.character_groups',
        [{'names': sorted(n for g in d['groups'] for n in g['names']),
          'count': d['count'],
          'classified': [x['character'] for x in d['classified']]}
         for d in bangs.get('euc_characters_changed', [])],
        [{'names': g['names'], 'count': len(units(g, 'char')),
          'classified': units(g, 'char', lambda x: x['classified'])}
         for g in e['character_groups']])
    # Compared for ASCII alone where the rest of the text changes: each
    # locale among those a line on EUC says change, and none of those it
    # says are the same for ASCII.
    alone = {n for x in e['ascii_alone'] for g in x['groups']
             for n in g['names']}
    changed = {n for k in ('euc_sequences_changed', 'euc_characters_changed')
               for d in bangs.get(k, []) for g in d['groups']
               for n in g['names']}
    same = {n for d in bangs.get('euc_ascii_same', []) for g in d['groups']
            for n in g['names']}
    if not alone <= changed or alone & same:
        raise ReportError(f'{where}.euc.ascii_alone names {sorted(alone)}, '
                          f'the lines on EUC change {sorted(changed)} and '
                          f'are the same for ASCII in {sorted(same)}')
    # Each locale and side not checked, with its reason: the `!!`'s, as
    # a sentence starts with it.
    told = {(n, r['side']): r['why'] for d in bangs.get('euc_not_checked', [])
            for r in d['reasons'] for g in r['groups'] for n in g['names']}
    held = {(x['name'], x['side']): x['why'] for x in e['not_checked']}
    if set(told) != set(held) or any(told[k] != _sentence(w)
                                     for k, w in held.items()):
        raise ReportError(f'{where}.euc.not_checked are {held!r}, the `!!` '
                          f'say {told!r}')


def _euc_ascii_sides(where, c, warned):
    """The machine whose converter of an EUC encoding does not open, for
    each locale of EUC compared: as the lines on EUC say it (compared for
    ASCII alone, or the same for ASCII) and as the lines on upper(),
    lower() and ILIKE say it (an error on the new machine, on the old one,
    on both), the same."""
    t, u = c['ctype'], c['postgres_use']
    if (warned['state'] != 'reported' or t['state'] != 'compared'
            or u['state'] == 'not checked'):
        return
    bangs = {}
    for i in warned['items']:
        bangs.setdefault(i['kind'], []).append(i['data'])

    def names(*kinds):
        return {n for k in kinds for d in bangs.get(k, [])
                for n in _names(d)}
    said = {}
    for x in t['euc']['ascii_alone'] + bangs.get('euc_ascii_same', []):
        for g in x['groups']:
            for n in g['names']:
                said[n] = x['side']
    compared = (names('euc_sequences_changed', 'euc_characters_changed',
                      'euc_same', 'euc_ascii_same') | set(said))
    use = {}
    for side, got in (('new', names('use_error_new')),
                      ('old', {n for g in u.get('back', [])
                               for n in g['names']}),
                      ('both', names('use_error_both'))):
        for n in got & compared:
            use[n] = side
    if said != use:
        raise ReportError(f'{where}.ctype.euc: the lines on EUC compare '
                          f'ASCII alone on {said!r}, the lines on upper(), '
                          f'lower() and ILIKE give an error on {use!r}')


def _ctype_groups(where, gs, lines, kinds, unit, value):
    """Groups of locales whose characters (unit 'char') or bytes ('byte')
    change alike: each group's locales once, in order, of the lines
    KINDS, the groups in the order printed, each change as _ctype_change
    reads it, each character or byte once in its group."""
    if not isinstance(gs, list):
        raise ReportError(f'{where} is {gs!r}')
    seen = []
    for i, g in enumerate(gs):
        at = f'{where}[{i}]'
        _keys(at, g, ['names', 'changes'])
        if not (_is_strs(g['names'])
                and g['names'] == sorted(set(g['names']))
                and all(lines.get(n) in kinds for n in g['names'])):
            raise ReportError(f'{at}.names is {g["names"]!r}')
        if not (isinstance(g['changes'], list) and g['changes']):
            raise ReportError(f'{at}.changes is {g["changes"]!r}')
        for j, x in enumerate(g['changes']):
            _ctype_change(f'{at}.changes[{j}]', x, unit, value)
        # Each character or byte once in the group, each entry's
        # in order, the entries in the order of their first.
        num = ((lambda u: int(u[2:], 16)) if unit == 'char'
               else (lambda u: int(u, 16)))
        each = [[num(u) for u in x[unit + 's']] for x in g['changes']]
        every = [u for e in each for u in e]
        if (len(every) != len(set(every))
                or any(e != sorted(e) for e in each)
                or [e[0] for e in each] != sorted(e[0] for e in each)):
            raise ReportError(f'{at}.changes are not in order, or '
                              'name a character or byte twice')
        seen.append(g['names'])
    every = [n for names in seen for n in names]
    if len(every) != len(set(every)) or seen != sorted(seen):
        raise ReportError(f'{where} names a locale twice or out of order')


# The lines a locale on EUC can be on: one a UTF8 database can use too, or
# not (locale_order.euc_texts takes every locale of EUC compared); the
# sides its lines name (locale_order.CTYPE_SIDES).
CTYPE_EUC_LINES = ('utf8', 'euc')
CTYPE_SIDES = ('both', 'old', 'new')
# The words a reason keeps as they are where it starts a sentence
# (locale_order.sentence).
SENTENCE_AS_IS = ('glibc', 'iconv_open')


def _sentence(why):
    """A reason as the line on EUC not checked starts a sentence with it
    (locale_order.sentence): capitalized, but for SENTENCE_AS_IS."""
    return (why if why.startswith(SENTENCE_AS_IS)
            else why[:1].upper() + why[1:])


def _ctype_euc(where, e, lines):
    """step 11's data on the character rules in an EUC database
    (locale_order.euc_texts): the groups whose characters change, as
    _ctype_groups reads them; the locales compared for ASCII alone, by
    side, each side once and each locale once; each locale and side not
    checked with its reason, once, in order of name; every locale of
    them compared on a line of CTYPE_EUC_LINES."""
    _keys(where, e, ['character_groups', 'ascii_alone', 'not_checked'])
    _ctype_groups(f'{where}.character_groups', e['character_groups'], lines,
                  CTYPE_EUC_LINES, 'char', _is_cp)
    a = e['ascii_alone']
    if not (_maybe(_objects(side=_one_of_f(*CTYPE_SIDES),
                            groups=_GROUPS))(a)
            and len({x['side'] for x in a}) == len(a)):
        raise ReportError(f'{where}.ascii_alone is {a!r}')
    names = [n for x in a for g in x['groups'] for n in g['names']]
    if len(names) != len(set(names)) or any(
            lines.get(n) not in CTYPE_EUC_LINES for n in names):
        raise ReportError(f'{where}.ascii_alone names {names!r}')
    nc = e['not_checked']
    if not (_maybe(_objects(name=_is_str, side=_one_of_f(*CTYPE_SIDES),
                            why=_is_str))(nc)
            and _side_sets((x['name'], x['side']) for x in nc)
            and [x['name'] for x in nc] == sorted(x['name'] for x in nc)
            and all(lines.get(x['name']) in CTYPE_EUC_LINES for x in nc)):
        raise ReportError(f'{where}.not_checked is {nc!r}')


def _ctype_utf8(where, u, lines):
    """The locales of UTF-8 the rows of the line on UTF-8's converter are
    under (locale_order.utf8_texts): each once, in order, none where no
    line is printed; where the rules were compared, each on the line a
    UTF8 database can use, or on the one of those not checked."""
    _keys(where, u, ['names'])
    n = u['names']
    if not (n == [] or (_is_strs(n) and n == sorted(set(n)))) or (
            lines is not None and any(lines.get(x) not in ('utf8',
                                                           'not checked')
                                      for x in n)):
        raise ReportError(f'{where}.names is {n!r}')


def _named(d):
    """Each locale named once, or none where they are all those compared,
    as many as the line counts."""
    return (d['names'] == [] or (len(d['names']) == d['locales']
                                 and len(set(d['names'])) == d['locales']))


def _changed(d):
    """No more characters the old machine classified than change, each
    once."""
    cs = [x['character'] for x in d['classified']]
    return len(cs) <= d['count'] and len(cs) == len(set(cs))


def _once(xs, key):
    """Each entry's key once."""
    vs = [x[key] for x in xs]
    return len(vs) == len(set(vs))


def _side_sets(pairs):
    """The sides each name is given (name, side): one of both, old or new,
    or old and new, each once (a reason of each machine, or one of both)."""
    by = {}
    for name, side in pairs:
        by.setdefault(name, []).append(side)
    return all(sorted(v) in (['both'], ['old'], ['new'], ['new', 'old'])
               for v in by.values())


def _other_encoding(d):
    """other_encoding_text's data: each locale once; those of the same
    sort table among them; what only a machine that told says (told)
    empty where none did; each encoding once among the errors and the
    converters that convert every byte, and one that converts otherwise
    among them."""
    names = _names(d)
    if len(names) != len(set(names)) or not set(d['same']) <= set(names):
        return False
    if not d['told'] and (d['unknown'] or d['errors'] or d['whole']
                          or d['other']):
        return False
    unknown = [x for u in d['unknown'] for g in u['groups'] for x in g['names']]
    if len(unknown) != len(set(unknown)) or not set(unknown) <= set(names):
        return False
    if not all(map(_part, d['errors'])):
        return False
    measured = ([e['encoding'] for e in d['errors']]
                + [c for w in d['whole'] for c in w['encodings']])
    return (len(measured) == len(set(measured))
            and _once(d['other'], 'encoding')
            and {x['encoding'] for x in d['other']} <= set(measured))


_ASCII_ONLY = {'both': "neither machine's glibc can convert their encoding",
               'old': "the old machine's glibc could not convert their "
                      "encoding",
               'new': "the new machine's glibc cannot convert their "
                      "encoding"}


def _processes(d):
    return _n(d['processes'], 'PostgreSQL process', 'PostgreSQL processes')


def _run(d):
    return 'runs' if d['processes'] == 1 else 'run'


def _listed(d):
    """How many names a list cut at the step's limit holds: those it
    prints and those it leaves out."""
    return len(d['names']) + d['more']


# Step 11's closing, by the state locale_order.py writes it under.
CLOSING_TITLES = {
    'plain': "Rules that show only in strings of several letters, and "
             "characters glibc ignores",
    'tables': "Whether the two machines compare strings with the same glibc "
              "code",
    'mixed': "Whether the two machines compare strings with the same glibc "
             "code, and rules of several letters",
}
# Every kind of `!!` the steps write as data: its list, the exact keys of
# its data with a check for each value, and its short title, made from the
# data alone. A kind not here, or data of another shape, is refused.
KINDS = {
    'same_commit': (
        'not checked', _PAIR,
        lambda d: f"Nothing compared: {d['old']} and {d['new']} are the "
                  f"same commit"),
    'same_tree_machines': (
        'not checked', {**_PAIR, 'charmaps': _is_bool},
        lambda d: "Nothing compared: the two machines hold the same locale "
                  "sources"),
    'same_tree': (
        'not checked', _PAIR,
        lambda d: f"Nothing compared: {d['old']} and {d['new']} are one "
                  f"source tree"),
    'reversed_allowed': (
        'not checked', {**_PAIR, 'detail': _is_str},
        lambda d: "Old and new run the other way round, on request"),
    'direction_undetermined': (
        'not checked', {**_PAIR, 'detail': _is_str},
        lambda d: f"Which of {d['old']} and {d['new']} is the older one"),
    'signatures_unchecked': (
        'not checked',
        {'refs': _objects(ref=_is_str,
                          status=_one_of_f(*SIGNATURE_WHY))},
        _signatures_title),
    'machine_falls_back_to_tags': (
        'not checked',
        {'unread': _objects(side=_one_of_f('old', 'new'), build=_is_str,
                            why=_is_str),
         'tags': lambda v: _is_strs(v) and len(v) == 2},
        _fallback_title),
    'sides_read_as_tags': (
        'not checked',
        {'refs': _is_strs,
         'classes': lambda v: _is_strs(v) and set(v) <= {'tag', 'commit'},
         'ask': _one_of_f('build', 'none')},
        _tags_title),
    'code_not_compared': (
        'not checked', {'step11': _is_bool},
        lambda d: "glibc's code that compiles and compares the rules"),
    'collation_version_unknown': (
        'not checked', {'sides': _is_strs},
        lambda d: f"Whether PostgreSQL warns about a collation's version "
                  f"(no glibc version for {', '.join(d['sides'])})"),
    'collation_version_same': (
        'change', {'version': _is_str},
        lambda d: f"PostgreSQL will not warn: both sides are glibc "
                  f"{d['version']}"),
    'machine_entries_skipped': (
        'not checked',
        {'machine': _is_str,
         'entries': _objects(entry=_is_str, why=_is_str)},
        _skipped_title),
    'machine_not_checked': (
        'not checked', {},
        lambda d: "The machines' locale sources against the package they "
                  "came from"),
    'build_charmaps_unchecked': (
        'not checked',
        {'build': _is_str, 'package': _is_str, 'why': _is_str},
        lambda d: f"The character sets of {d['build']} against its "
                  f"package"),
    'build_not_checked': (
        'not checked',
        {'metadata': _is_bool,
         'unlisted': lambda v: v == [] or _objects(build=_is_str,
                                                   folder=_is_str)(v)},
        lambda d: "The builds' compiler, code patches and GPG signatures"),
    # Step 2's (filter_lc_collate_changes, node_charmaps).
    'added_not_at_old': (
        'not checked',
        {'at': _is_str, 'named': _one_of_f('tag', 'commit'),
         'locales': _is_strs},
        lambda d: f"Whether {_n(len(d['locales']), 'locale', 'locales')} "
                  f"with no file at {d['at']} existed on the old system"),
    'added_rules_unknown': (
        'not checked',
        {**_PAIR, 'locales': _objects(name=_is_str, why=_is_str)},
        lambda d: f"Sort rules of "
                  f"{_n(len(d['locales']), 'locale file', 'locale files')} "
                  f"at {d['new']} and not at {d['old']}"),
    'c_utf8_no_source': (
        'not checked', {'sides': _is_strs},
        lambda d: f"C.UTF-8's sort rules (no source file at "
                  f"{' and '.join(d['sides'])})"),
    'c_utf8_differs': (
        'change', {},
        lambda d: "C.UTF-8's sort rules differ"),
    'rules_not_compared': (
        'not checked',
        {'groups': _objects(names=_is_strs, why=_is_str)},
        lambda d: "Sort rules of " + _n(
            sum(len(x['names']) for x in d['groups']), 'locale', 'locales')
        + ", listed in the summary"),
    'names_untraced': (
        'not checked',
        {'machine': _is_str, 'names': _objects(name=_is_str, why=_is_str)},
        lambda d: f"Character set of "
                  f"{_n(len(d['names']), 'locale name', 'locale names')} "
                  f"measured on {d['machine']}"),
    'charmaps_not_run': (
        'not checked', {'why': _is_str},
        lambda d: "The character sets locales are built with, and the "
                  "collating elements built out of transliterations"),
    'charmap_builds_unsettled': (
        'not checked',
        {'builds': _objects(build=_is_str, charmap=_is_str, why=_is_str)},
        lambda d: f"What the character set does to "
                  f"{_n(len(d['builds']), 'locale build', 'locale builds')}"
                  f", listed in the summary"),
    'charmap_distro_buildable': (
        'change',
        {'locales': _objects(source=_is_str, charmap=_is_str)},
        lambda d: f"{_n(len(d['locales']), 'locale', 'locales')} a distro "
                  f"can build in a changed character set, listed in the "
                  f"summary"),
    'charmap_search_not_run': (
        'not checked',
        {'charmaps': _objects(charmap=_is_str, why=_is_str)},
        lambda d: "Locales a distro can build in " + _n(
            len(d['charmaps']), 'changed character set',
            'changed character sets')),
    'charmap_sources_missing': (
        'not checked',
        {'entries': _is_strs, 'basis': _one_of_f('SUPPORTED', 'measured')},
        lambda d: f"Character sets of "
                  + (_n(len(d['entries']), 'SUPPORTED entry',
                        'SUPPORTED entries') if d['basis'] == 'SUPPORTED'
                     else _n(len(d['entries']), 'locale build',
                             'locale builds'))
                  + " whose source file is not on both sides"),
    'translit_unsettled': (
        'not checked',
        {'builds': _objects(build=_is_str, charmap=_is_str, why=_is_str)},
        lambda d: f"Collating elements built out of transliterations, of "
                  f"{_n(len(d['builds']), 'locale build', 'locale builds')}"
                  f", listed in the summary"),
    # Step 11's (locale_order.py, its report's head, its lists of locales
    # and its closing).
    'tag_names_no_release': (
        'not checked', {'side': _SIDE, 'tag': _is_str},
        lambda d: f"Whether the {d['side']} machine runs the glibc of "
                  f"{d['tag']}"),
    'version_names_no_release': (
        'not checked', {'side': _SIDE, 'version': _is_str, 'tag': _is_str},
        lambda d: f"Whether {d['version']}, measured on the {d['side']} "
                  f"machine, is the glibc of {d['tag']}"),
    'same_build': (
        'not checked', {},
        lambda d: "Two machines on the same glibc build, not an upgrade"),
    'machines_order_undetermined': (
        'not checked', {'version': _is_str},
        lambda d: f"Which machine is the older one (both run "
                  f"{d['version']})"),
    'tables_disagree': (
        'not checked', {'names': _is_strs, 'more': _is_count},
        lambda d: f"{_n(_listed(d), 'locale', 'locales')} changed whose "
                  f"sort tables find no change: the machines compare with "
                  f"different code"),
    'tables_not_compared': (
        'not checked',
        {'groups': _objects(why=_is_str, names=_is_strs, more=_is_count)},
        lambda d: f"Sort tables of "
                  f"{_n(sum(map(_listed, d['groups'])), 'locale', 'locales')}"
                  f" not compared"),
    'partly_measured': (
        'not checked', {'locales': _objects(name=_is_str, count=_is_positive)},
        lambda d: f"{_n(len(d['locales']), 'locale', 'locales')} unchanged "
                  f"only as far as measured: some neighbours not measured at "
                  f"every level"),
    'measured_in_part': (
        'not checked',
        {'groups': _objects(names=_is_strs, side=_SIDE, charmap=_is_str,
                            count=_is_positive, examples=_is_strs)},
        lambda d: f"{_locales({x for g in d['groups'] for x in g['names']})}"
                  f" measured in part: their character set has sequences "
                  f"not measured"),
    'charmaps_not_checked': (
        'not checked',
        {'side': _SIDE, 'named_by_bytes': _is_bool,
         'groups': _objects(names=_is_strs, why=_is_str)},
        lambda d: f"The character sets of "
                  f"{_locales([x for g in d['groups'] for x in g['names']])}"
                  f" of the {d['side']} machine"),
    'charmaps_machine_not_checked': (
        'not checked',
        {'side': _one_of_f('old', 'new', 'either'), 'why': _is_str,
         'named_by_bytes': _is_bool},
        lambda d: "The character sets of " + (
            'either machine' if d['side'] == 'either'
            else f"the {d['side']} machine")),
    'glibc_refused': (
        'change',
        {'locales': _objects(name=_is_str,
                             side=_one_of_f('both', 'old', 'new'),
                             categories=_is_strs)},
        lambda d: f"{_locales({x['name'] for x in d['locales']})} glibc "
                  f"refuses: a database that uses one does not open"),
    'not_measured': (
        'not checked',
        {'locales': _objects(name=_is_str,
                             reason=_one_of_f('different strings', 'error'),
                             side=_one_of_f('both', 'old', 'new'),
                             why=_is_str)},
        lambda d: f"{_locales({x['name'] for x in d['locales']})} that "
                  f"could not be measured"),
    'not_on_new_machine': (
        'change', {'locales': _is_strs},
        lambda d: f"{_n(len(d['locales']), 'locale', 'locales')} not on the "
                  f"new machine: a database that uses one cannot use it "
                  f"there"),
    'closing': (
        'not checked', {'state': _one_of_f(*CLOSING_TITLES)},
        lambda d: CLOSING_TITLES[d['state']]),
    # Step 11's checks beside the order (control_texts, money_texts,
    # libc_texts).
    'control_failed': (
        'not checked',
        {'side': _SIDE,
         'names': lambda v: v in (['C'], ['POSIX'], ['C', 'POSIX'])},
        lambda d: f"What was measured on the {d['side']} machine: the "
                  f"control failed ({' and '.join(d['names'])} did not "
                  f"measure as byte order)"),
    'control_not_checked': (
        'not checked',
        {'absent': _maybe(_objects(name=_CONTROL, side=_SIDE)),
         'unmeasured': _maybe(_objects(name=_CONTROL, side=_SIDE,
                                       error=_is_str))},
        lambda d: "The control of the measurement on " + _machines(
            x['side'] for x in d['absent'] + d['unmeasured'])
        + ": C and POSIX as byte order"),
    'money_earlier_format': (
        'not checked', {'side': _SIDES},
        lambda d: f"Decimals of money on {_machines([d['side']])} (a file "
                  f"of an earlier version)"),
    'money_changed': (
        'change',
        {'locales': _objects(name=_is_str, old=_SCALE, new=_SCALE)},
        lambda d: f"Decimals of money change in "
                  f"{_locales({x['name'] for x in d['locales']})}"),
    'money_not_checked': (
        'not checked',
        {'locales': _objects(name=_is_str, side=_SIDE, why=_is_str)},
        lambda d: f"Decimals of money of "
                  f"{_locales({x['name'] for x in d['locales']})}"),
    'libc_earlier_format': (
        'not checked', {'side': _SIDES},
        lambda d: f"Which C library PostgreSQL runs on "
                  f"{_machines([d['side']])} (a file of an earlier version)"),
    'glibc_replaced': (
        'not checked', {'side': _SIDE},
        lambda d: f"What the {d['side']} machine runs now: its glibc was "
                  f"replaced while it was measured"),
    'libc_stale': (
        'change', {'side': _SIDE, 'processes': _is_positive},
        lambda d: f"{_processes(d)} of the {d['side']} machine still "
                  f"{_run(d)} the glibc replaced on disk"),
    'libc_other': (
        'change', {'side': _SIDE, 'processes': _is_positive},
        lambda d: f"{_processes(d)} of the {d['side']} machine {_run(d)} "
                  f"another C library than the one measured"),
    'libc_not_checked': (
        'not checked',
        {'side': _SIDE,
         'machine': lambda v: v == [] or (_is_strs(v) and len(v) == 1),
         'named': _maybe(_objects(
             reason=_one_of_f('could not be read', 'show no C library'),
             count=_is_positive, processes=_is_positive,
             looked_for=_is_strs)),
         'unnamed': _is_count},
        lambda d: f"Which C library every PostgreSQL process of the "
                  f"{d['side']} machine runs"),
    'libc_no_process': (
        'not checked', {'side': _SIDE, 'looked_for': _is_strs},
        lambda d: f"Which C library PostgreSQL runs on the {d['side']} "
                  f"machine: no process named {' or '.join(d['looked_for'])} "
                  f"found"),
    # Step 11's lines on upper(), lower() and ILIKE (use_texts).
    'use_earlier_format': (
        'not checked', {'side': _SIDES},
        lambda d: f"{_FN} on {_machines([d['side']])} (a file of an earlier "
                  f"version)"),
    'use_error_new': (
        'change', {'groups': _GROUPS},
        lambda d: f"{_FN} give an error on the new machine in "
                  f"{_use_locales(d)}: its glibc cannot convert their "
                  f"encoding"),
    'use_error_both': (
        'change', {'groups': _GROUPS},
        lambda d: f"{_FN} give an error on both machines in "
                  f"{_use_locales(d)}, as before the upgrade"),
    'use_error_moved': (
        'change',
        {'side': _one_of_f('both', 'new'), 'from': _is_str, 'to': _is_str,
         'why': _why, 'groups': _GROUPS},
        lambda d: f"{_FN} give an error on {_machines([d['side']])} in "
                  f"{_use_locales(d)} whose encoding changed from "
                  f"{d['from']} to {d['to']}"),
    'use_error_any_db': (
        'change', {'side': _SIDES, 'why': _is_str, 'groups': _GROUPS},
        lambda d: f"{_FN} give an error on {_machines([d['side']])} in "
                  f"{_use_locales(d)} that a database of any encoding may "
                  f"use"),
    'use_error_one_side': (
        'change',
        {'side': _SIDE, 'reason': _one_of_f('refused', 'absent', 'unknown'),
         'why': _why, 'groups': _GROUPS},
        _one_side_title),
    'use_error_partial': (
        'change',
        {'entries': _objects(parts=_LACKING, unknown_on=_UNKNOWN_ON,
                             names=_is_strs)},
        lambda d: f"{_FN} give an error on part of the text in "
                  f"{_use_locales(d)}"),
    'use_wrong_result': (
        'change',
        {'entries': _objects(parts=_LACKING, lower=_LOWER, names=_is_strs)},
        lambda d: f"{_FN} give a wrong result on part of the text in "
                  f"{_use_locales(d)}"),
    'use_back_changed': (
        'change',
        {'entries': _objects(encoding=_is_str, count=_is_positive,
                             names=_is_strs)},
        lambda d: f"{_FN} give another result on part of the text in "
                  f"{_use_locales(d)}"),
    'use_partial_unknown': (
        'not checked', {'places': _objects(side=_SIDES, groups=_GROUPS)},
        lambda d: f"Whether {_FN} give an error on part of the text in "
                  f"{_use_locales(d)} (a file of an earlier version)"),
    'use_back_not_checked': (
        'not checked',
        {'side': _SIDES, 'utf8': _is_count,
         'encodings': _objects(encoding=_is_str, count=_is_positive)},
        lambda d: f"Whether the result of {_FN} converts back to the "
                  f"database's bytes in "
                  f"{_locales_n(sum(x['count'] for x in d['encodings']))} "
                  f"(a file of an earlier version)"),
    'use_not_checked': (
        'not checked', {'reasons': _objects(why=_is_str, groups=_GROUPS)},
        lambda d: f"{_FN} in {_use_locales(d)}"),
    # Step 11's lines on the character rules (ctype_texts, euc_texts,
    # utf8_texts) and on a database of another encoding
    # (other_encoding_text).
    'ctype_earlier_format': (
        'not checked', {'side': _SIDES},
        lambda d: f"{_C} on {_machines([d['side']])} (a file of an earlier "
                  f"version)"),
    'ctype_characters_changed': (
        'change',
        {'locales': _is_positive, 'names': _maybe(_is_strs),
         'count': _is_positive, 'classified': _CLASSIFIED},
        lambda d: f"{_C} change for "
                  f"{_n(d['count'], 'character', 'characters')} in "
                  f"{_locales_n(d['locales'])} that a UTF8 database can use"),
    'ctype_bytes_changed': (
        'change',
        {'locales': _is_positive, 'names': _maybe(_is_strs),
         'bytes': _objects(byte=_is_byte, change=_is_str)},
        lambda d: f"{_C} change for {_n(len(d['bytes']), 'byte', 'bytes')} "
                  f"in {_locales_n(d['locales'])}"),
    'ctype_not_checked': (
        'not checked',
        {'locales': _objects(name=_is_str, side=_SIDES, why=_is_str)},
        lambda d: f"{_C} in {_locales({x['name'] for x in d['locales']})}"),
    'euc_sequences_changed': (
        'change', {'groups': _GROUPS, 'sequences': _SEQUENCES},
        lambda d: f"In an EUC database glibc converts "
                  f"{_n(len(d['sequences']), 'byte sequence', 'byte sequences')}"
                  f" to another character in {_use_locales(d)}"),
    'euc_characters_changed': (
        'change',
        {'groups': _GROUPS, 'count': _is_positive, 'classified': _CLASSIFIED},
        lambda d: f"{_EUC_DB} change for "
                  f"{_n(d['count'], 'character', 'characters')} in "
                  f"{_use_locales(d)}"),
    'euc_same': (
        'not checked', {'other': _is_bool, 'groups': _GROUPS},
        lambda d: f"{_EUC_DB} in {_use_locales(d)}: the same for the text "
                  f"glibc converts on both machines"),
    'euc_ascii_same': (
        'not checked', {'side': _SIDES, 'groups': _GROUPS},
        lambda d: f"{_EUC_DB} in {_use_locales(d)}: only ASCII compared, as "
                  f"{_ASCII_ONLY[d['side']]}"),
    'euc_not_checked': (
        'not checked',
        {'reasons': _objects(why=_is_str, side=_SIDES, groups=_GROUPS)},
        lambda d: f"{_EUC_DB} in {_use_locales(d)}"),
    'utf8_changed': (
        'change', {'locales': _is_positive, 'sequences': _SEQUENCES},
        lambda d: f"In a UTF8 database glibc converts "
                  f"{_n(len(d['sequences']), 'byte sequence', 'byte sequences')}"
                  f" to another character in {_locales_n(d['locales'])}"),
    'utf8_one_side': (
        'not checked',
        {'side': _SIDE, 'why': _why, 'locales': _is_positive,
         'sequences': _SEQUENCES},
        lambda d: f"In a UTF8 database, whether "
                  f"{_n(len(d['sequences']), 'byte sequence', 'byte sequences')}"
                  f" glibc reads as another character on the {d['side']} "
                  f"machine changed, in {_locales_n(d['locales'])}"),
    'other_encoding': (
        'not checked',
        {'groups': _GROUPS, 'same': _maybe(_is_strs), 'told': _is_bool,
         'unknown': _maybe(_objects(side=_SIDES, groups=_GROUPS)),
         'errors': _maybe(_objects(
             encoding=_is_str, alike=_one_of_f(*_ALIKE),
             sides=_objects(side=_SIDE, count=_is_count))),
         'whole': _maybe(_objects(side=_SIDES, encodings=_is_strs)),
         'other': _maybe(_objects(encoding=_is_str, count=_is_positive))},
        lambda d: f"{_use_locales(d)} whose encoding PostgreSQL does not "
                  f"know, in a database of another encoding"),
}


def _control_entries(d):
    """The control's locales, each machine's C and POSIX named once at
    most, in one list or the other, and one at least."""
    said = [(x['side'], x['name']) for x in d['absent'] + d['unmeasured']]
    return bool(said) and len(said) == len(set(said))


# What a kind's values say together, beside each one's check in KINDS:
# what the step cannot write is refused.
WHOLE = {
    'control_not_checked': _control_entries,
    'money_changed': lambda d: all(x['old'] != x['new']
                                   for x in d['locales']),
    'libc_not_checked': lambda d: bool(d['machine'] or d['named']
                                       or d['unnamed']),
    'use_error_new': _distinct,
    'use_error_both': _distinct,
    'use_error_moved': lambda d: _distinct(d) and d['from'] != d['to'],
    'use_error_any_db': _distinct,
    # A reason only where what the other machine does is not known; a
    # locale the old machine does not have, only on the new one.
    'use_error_one_side': lambda d: (
        _distinct(d) and bool(d['why']) == (d['reason'] == 'unknown')
        and (d['reason'] != 'absent' or d['side'] == 'new')),
    'use_error_partial': lambda d: _distinct(d) and all(
        map(_parts, d['entries'])),
    'use_wrong_result': lambda d: _distinct(d) and all(
        _parts(e) and _lower(e) for e in d['entries']),
    'use_back_changed': _distinct,
    'use_partial_unknown': _distinct,
    'use_back_not_checked': lambda d: d['utf8'] <= sum(
        x['count'] for x in d['encodings']),
    'ctype_characters_changed': lambda d: _named(d) and _changed(d),
    'ctype_bytes_changed': lambda d: _named(d) and _once(d['bytes'], 'byte'),
    'ctype_not_checked': lambda d: _side_sets(
        (x['name'], x['side']) for x in d['locales']),
    # Two machines that convert a sequence to the same character are no
    # change.
    'euc_sequences_changed': lambda d: _distinct(d) and _once(
        d['sequences'], 'bytes') and all(x['old'] != x['new']
                                         for x in d['sequences']),
    'euc_characters_changed': lambda d: _distinct(d) and _changed(d),
    'euc_same': _distinct,
    'euc_ascii_same': _distinct,
    'euc_not_checked': lambda d: _side_sets(
        (x, r['side']) for r in d['reasons'] for g in r['groups']
        for x in g['names']),
    'utf8_changed': lambda d: _once(d['sequences'], 'bytes') and all(
        x['old'] != x['new'] for x in d['sequences']),
    'utf8_one_side': lambda d: _once(d['sequences'], 'bytes'),
    'other_encoding': _other_encoding,
}


def warning_item(where, kind, data, text):
    """The item of a `!!` written as (kind, data, text): checked against
    KINDS, with its list and its title."""
    if kind not in KINDS:
        raise ReportError(f'{where}: a warning of unknown kind {kind!r}')
    cls, spec, title = KINDS[kind]
    if not isinstance(data, dict) or set(data) != set(spec):
        raise ReportError(f'{where}: {kind} data {data!r} has not the keys '
                          f'{sorted(spec)}')
    for k, ok in spec.items():
        if not ok(data[k]):
            raise ReportError(f'{where}: {kind} data {k} is {data[k]!r}')
    if kind in WHOLE and not WHOLE[kind](data):
        raise ReportError(f'{where}: {kind} data {data!r} cannot be written')
    if not (isinstance(text, str) and text.startswith('!! ')):
        raise ReportError(f'{where}: {kind} text {text!r} is not a !! block')
    return {'kind': kind, 'class': cls, 'title': title(data), 'data': data,
            'text': text}


def step_warnings(path):
    """The `!!` a step wrote beside its log (glibc_locale_data.
    record_warning), one JSON object a line; no file is 'not reported'."""
    if not os.path.isfile(path):
        return not_reported(path)
    items = []
    for i, line in enumerate(_lines(path), 1):
        where = f'{path}:{i}'
        try:
            w = json.loads(line)
        except ValueError as e:
            raise ReportError(f'{where}: not JSON: {e}')
        if not isinstance(w, dict) or set(w) != {'kind', 'data', 'text'}:
            raise ReportError(f'{where}: not a warning: {line!r}')
        items.append(warning_item(where, w['kind'], w['data'], w['text']))
    return {'state': 'reported', 'items': items}


def warnings_section(a):
    """Steps 0, 1, 2 and 11 from their files (step 0 runs only for the
    machines' files, step 11 only with --step11)."""
    f = lambda n: _file(a.out_dir, f'step{n}.{a.pair}.warnings.jsonl')
    step0 = (step_warnings(f(0)) if a.old_node else
             {'state': 'not run', 'why': 'no --old-node and --new-node'})
    step11 = (step_warnings(f(11)) if a.step11 else
              {'state': 'not run', 'why': "no machines' files"})
    return {'step0': step0, 'step1': step_warnings(f(1)),
            'step2': step_warnings(f(2)), 'step11': step11}


# ---------------------------------------------------------------- compared

# What 'What was compared' says (glibc_locale_data.COMPARED_RECORD), and
# step 0's note (STEP0_RECORD_ENV).
COMPARED_KEYS = ['builds', 'collation_version', 'machines', 'step', 'tags']
NOTE_STATES = ('printed', 'not printed', 'fallback')


def _one_json(path, step):
    lines = _lines(path)
    if len(lines) != 1:
        raise ReportError(f'{path} is not one line')
    data = json.loads(lines[0])
    if not isinstance(data, dict) or data.get('step') != step:
        raise ReportError(f'{path} is not what step {step} writes')
    return {k: v for k, v in data.items() if k != 'step'}


def compared_section(a):
    """What 'What was compared' printed, read from its record, with step
    0's note (or 'not run' without the machines' files, 'not reported'
    without its file); each machine's commit held to the one the sides
    recorded for its build, and its measurement sha256 to the file this
    run laid out for the side its build names: the old side's where both
    sides are one build, whose file the sides read for both."""
    import hashlib
    path = _file(a.out_dir, f'step1_compared.{a.pair}.json')
    if not os.path.isfile(path):
        return not_reported(path)
    data = _one_json(path, 1)
    if sorted(list(data) + ['step']) != COMPARED_KEYS:
        raise ReportError(f'{path} has the keys {sorted(data)}')
    out = dict(data, state='reported')
    if a.old_node:
        p0 = _file(a.out_dir, f'step0.{a.pair}.json')
        out['note'] = (_one_json(p0, 0) if os.path.isfile(p0)
                       else not_reported(p0))
    else:
        out['note'] = {'state': 'not run',
                       'why': 'no --old-node and --new-node'}
    if a.old_asked:
        pl = _file(a.out_dir, f'latest.{a.pair}.json')
        if os.path.isfile(pl):
            lines = _lines(pl)
            if len(lines) != 1:
                raise ReportError(f'{pl} is not one line')
            got = json.loads(lines[0])
            if not isinstance(got, dict) or got.get('step') != 'latest' or \
                    sorted(got) != ['resolved', 'step']:
                raise ReportError(f'{pl} is not what latest writes')
            out['latest'] = {'state': 'reported',
                             'asked': [a.old_asked, a.new_asked],
                             'resolved': got['resolved']}
        else:
            out['latest'] = not_reported(pl)
    else:
        out['latest'] = {'state': 'not asked'}
    machines = out['machines'] if isinstance(out['machines'], list) else []
    b = out['builds']
    rebuilt = (b.get('entries') if isinstance(b, dict) and
               isinstance(b.get('entries'), list) else [])
    recorded = {}
    if machines or rebuilt:
        # The commit the sides recorded for each build, the steps after
        # them read (glibc_locale_data.write_recorded): `<build>\t<commit>`.
        p1 = _file(a.out_dir, f'build_sides.{a.pair}.txt')
        for line in _lines(p1):
            build, sep, commit = line.partition('\t')
            if not sep or build in recorded:
                raise ReportError(f'{p1}: a line {line!r}')
            recorded[build] = commit
    for e in rebuilt:
        if not isinstance(e, dict) or \
                e.get('commit') != recorded.get(e.get('build'), '')[:12]:
            raise ReportError(f'{path}: a build {e!r}, the sides recorded '
                              f'{recorded!r}')
    for e in machines:
        if e.get('commit') != recorded.get(e.get('build'), '')[:12]:
            raise ReportError(f'{path}: {e.get("build")!r} commit '
                              f'{e.get("commit")!r}, the sides recorded '
                              f'{recorded.get(e.get("build"))!r}')
        side = ('old' if e.get('build') == a.old else
                'new' if e.get('build') == a.new else None)
        if side is None:
            raise ReportError(f'{path}: a machine {e.get("build")!r} that is '
                              f'neither side')
        f = _file(a.out_dir, f'node_file.{a.pair}.{side}/locale_order.out')
        with open(f, 'rb') as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        if e.get('measurement_sha256') != sha:
            raise ReportError(f'{path}: {e.get("build")!r} measurement '
                              f'{e.get("measurement_sha256")!r}, {f} is '
                              f'{sha}')
    return out


# ---------------------------------------------------------------- whole

def build(a):
    run = run_section(a)
    step2 = step2_section(a, run)
    return {
        'format': FORMAT,
        'pair': {'old': a.old, 'new': a.new, 'slug': a.pair},
        'run': run,
        'compared': compared_section(a),
        'step2': step2,
        'step11': step11_section(a, step2['rules_by_name']),
        'warnings': warnings_section(a),
    }


# ---------------------------------------------------------------- reader

def _no_null(where, obj):
    """No value anywhere is null: what was not done is a state."""
    if obj is None:
        raise ReportError(f'{where} is null')
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_null(f'{where}.{k}', v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_null(f'{where}[{i}]', v)


def _keys(where, obj, keys):
    if not isinstance(obj, dict):
        raise ReportError(f'{where} is not an object: {obj!r}')
    if set(obj) != set(keys):
        raise ReportError(f'{where} has the keys {sorted(obj)}, not '
                          f'{sorted(keys)}')


def _one_of(where, value, allowed):
    if value not in allowed:
        raise ReportError(f'{where} is {value!r}, none of {list(allowed)}')


def _strs(where, value):
    if not (isinstance(value, list) and all(isinstance(x, str)
                                            for x in value)):
        raise ReportError(f'{where} is not a list of strings: {value!r}')


def _rows(where, value, width):
    if not (isinstance(value, list) and all(
            isinstance(r, list) and len(r) == width
            and all(isinstance(c, str) for c in r) for r in value)):
        raise ReportError(f'{where} is not rows of {width} strings')


def _stated(where, obj, states, check):
    """A block whose 'state' picks its other keys: check[state] is the
    list of keys beside 'state', or a function that checks the block."""
    if not isinstance(obj, dict) or 'state' not in obj:
        raise ReportError(f'{where} has no state: {obj!r}')
    _one_of(f'{where}.state', obj['state'], states)
    rule = check[obj['state']]
    if callable(rule):
        rule(where, obj)
    else:
        _keys(where, obj, ['state', *rule])
        for k in rule:
            if k == 'why' and not isinstance(obj[k], (str, list)):
                raise ReportError(f'{where}.why is {obj[k]!r}')


def _warnings_of(w, step, kind):
    o = w[step]
    if o['state'] != 'reported':
        return None
    return [it['data'] for it in o['items'] if it['kind'] == kind]


def _rules(where, s2, some, run, pair, w, step11):
    """step2.rules: its shape, then each fact tied to the others of the
    same run: the totals add up and count the groups, the characters
    file's rows and the not-compared files and names; each group is its
    rows; the files only the new side has are the `!!` of an old tag or
    commit side or a plain block, their renames are the Removed rows';
    the printed Removed is the Removed file; the entries not read are
    step 1's `!!`; the two notes go with the list."""
    o = s2['rules']
    _stated(where, o, ('reported', 'not compared', 'not reported'),
            {'reported': ['totals', 'differ', 'added', 'removed', 'unread',
                          'unbuilt', 'not_ascii'],
             'not compared': ['why'], 'not reported': ['why']})
    if o['state'] == 'not reported':
        return
    if (o['state'] == 'reported') != some:
        raise ReportError(f'{where} is {o["state"]!r} where step 2 compared '
                          f'{"something" if some else "nothing"}')
    rb = s2['rules_by_name']
    if o['state'] == 'not compared':
        if rb['state'] == 'reported' and o['why'] != rb['nothing']['what']:
            raise ReportError(f'{where}.why {o["why"]!r} is not '
                              f'{rb["nothing"]["what"]!r}')
        return
    t = o['totals']
    _keys(f'{where}.totals', t, TOTALS_KEYS)
    for k in TOTALS_KEYS:
        if not _is_count(t[k]):
            raise ReportError(f'{where}.totals.{k} is {t[k]!r}')
    if t['names'] != t['same'] + t['differ'] + t['not_compared']:
        raise ReportError(f'{where}.totals do not add up: {t!r}')
    # The groups whose rules differ.
    names = []
    if not isinstance(o['differ'], list):
        raise ReportError(f'{where}.differ is not a list')
    for i, x in enumerate(o['differ']):
        at = f'{where}.differ[{i}]'
        _keys(at, x, ['names', 'every_rule', 'characters'])
        _strs(f'{at}.names', x['names'])
        if not x['names'] or not _is_bool(x['every_rule']):
            raise ReportError(f'{at} is {x!r}')
        c = x['characters']
        _stated(f'{at}.characters', c, CHARACTER_STATES,
                {'listed': ['codepoints'], 'counted': ['count'],
                 'none named': [], 'not known': ['why']})
        if c['state'] == 'listed' and (x['every_rule'] or not c['codepoints']
                                       or not all(_is_count(v) for v in
                                                  c['codepoints'])):
            raise ReportError(f'{at}: {x!r}')
        if c['state'] == 'counted' and (not x['every_rule']
                                        or not _is_positive(c['count'])):
            raise ReportError(f'{at}: {x!r}')
        if c['state'] == 'not known' and not _is_str(c['why']):
            raise ReportError(f'{at}: {x!r}')
        names += x['names']
    if len(set(names)) != len(names):
        raise ReportError(f'{where}.differ names one locale twice')
    if t['differ'] != len(names):
        raise ReportError(f'{where}.totals.differ {t["differ"]} and '
                          f'{len(names)} names in its groups')
    rc = s2['rules_characters']
    if rc['state'] == 'reported':
        rows = {r_[0]: r_ for r_ in rc['rows']}
        if len(rows) != len(rc['rows']) or set(rows) != set(names):
            raise ReportError(f'{where}.differ names {sorted(names)!r}, the '
                              f'characters file {sorted(rows)!r}')
        for x in o['differ']:
            c = x['characters']
            for n in x['names']:
                _, state, every, chars = rows[n]
                if state != 'differs' or every != ('yes' if x['every_rule']
                                                   else 'no'):
                    raise ReportError(f'{where}: {n} is {x!r}, its row '
                                      f'{rows[n]!r}')
                if c['state'] == 'not known':
                    ok = chars == f'not known: {c["why"]}'
                elif c['state'] == 'none named':
                    ok = chars == ''
                else:
                    cps = chars.split(' ') if chars else []
                    ok = all(re.fullmatch(r'U\+[0-9A-F]{4,}', v) for v in cps)
                    ok = ok and (len(cps) == c['count']
                                 if c['state'] == 'counted' else
                                 [int(v[2:], 16) for v in cps]
                                 == c['codepoints'])
                if not ok:
                    raise ReportError(f'{where}: {n} is {x!r}, its row '
                                      f'{rows[n]!r}')
    # The files only the new side has.
    a = o['added']
    _keys(f'{where}.added', a, ['state', 'rows'])
    _one_of(f'{where}.added.state', a['state'], ADDED_STATES)
    machines = rb['state'] == 'reported' and rb['machines']
    added = []
    for i, x in enumerate(a['rows']):
        at = f'{where}.added.rows[{i}]'
        _keys(at, x, ['name', 'spellings', 'renamed_from'])
        if not _is_str(x['name']) or not _is_str(x['renamed_from']):
            raise ReportError(f'{at} is {x!r}')
        sp = x['spellings']
        c_notice = (x['name'] == 'C.UTF-8' and a['state'] == 'notice')
        if c_notice != (sp == {'state': 'in the C.UTF-8 notice'}):
            raise ReportError(f'{at}.spellings is {sp!r} with the added '
                              f'state {a["state"]!r}')
        if isinstance(sp, dict):
            _stated(f'{at}.spellings', sp, MISSING_STATES,
                    {m: [] for m in MISSING_STATES})
            if not c_notice and rb['state'] == 'reported' and \
                    sp['state'] != ('not measured on that machine'
                                    if machines else 'not in SUPPORTED'):
                raise ReportError(f'{at}.spellings is {sp!r} with '
                                  f'machines={machines}')
        elif not (isinstance(sp, list) and sp and all(map(_is_str, sp))):
            raise ReportError(f'{at}.spellings is {sp!r}')
        elif machines:
            # Each a name measured on the new machine, which step 2's
            # rules by name hold under this file, or under itself where
            # compare_rules has it (a locale.alias alias).
            bn = rb['locales']['by_name']
            for n in sp:
                if n not in bn or bn[n]['key'] not in (x['name'], n):
                    raise ReportError(f'{at}: {n} is no measured name of '
                                      f'{x["name"]}: {bn.get(n)!r}')
        if machines and not c_notice and step11.get('verdicts') is not None:
            # The names measured on the new machine under this file: step
            # 2's rules by name hold them under it, and step 11 calls none
            # of them only old (a custom name on the old machine is).
            bn = rb['locales']['by_name']
            under = sorted(n for n, v in bn.items() if v['key'] == x['name'])
            v11 = step11['verdicts']
            if all(n in v11 for n in under):
                want = [n for n in under if v11[n]['verdict'] != 'only old']
                got_ = sorted(sp) if isinstance(sp, list) else []
                if got_ != want:
                    raise ReportError(f'{at}.spellings {sp!r}, measured on '
                                      f'the new machine under it: {want!r}')
        added.append(x['name'])
    tag_old = run['old_class'] != 'build'
    want = 'none' if not added else 'notice' if tag_old else 'plain'
    if a['state'] != want:
        raise ReportError(f'{where}.added.state is {a["state"]!r} with '
                          f'{len(added)} file(s) and the old side a '
                          f'{run["old_class"]}')
    notice = _warnings_of(w, 'step2', 'added_not_at_old')
    if notice is not None:
        rest = [n for n in added if n != 'C.UTF-8']
        if a['state'] == 'notice' and rest:
            if len(notice) != 1 or notice[0]['locales'] != rest or \
                    notice[0]['at'] != pair['old']:
                raise ReportError(f'{where}.added {rest!r}, the `!!` '
                                  f'{notice!r}')
        elif notice:
            raise ReportError(f'{where}.added is {a["state"]!r}, yet a `!!` '
                              f'names files not at the old side: {notice!r}')
        if a['state'] == 'notice' and 'C.UTF-8' in added:
            c = _warnings_of(w, 'step2', 'c_utf8_no_source')
            if not (c and pair['old'] in c[0]['sides']):
                raise ReportError(f'{where}: C.UTF-8 has no file at the old '
                                  f'tag, and no `!!` says so: {c!r}')
    unknown = _warnings_of(w, 'step2', 'added_rules_unknown')
    for d in unknown or ():
        stray = [x['name'] for x in d['locales'] if x['name'] not in added]
        if stray:
            raise ReportError(f'{where}: the `!!` of rules not known names '
                              f'{stray!r}, no file only the new side has')
    # The files only the old side has, as printed: the Removed file's rows.
    rm = o['removed']
    if not isinstance(rm, list):
        raise ReportError(f'{where}.removed is not a list')
    for i, x in enumerate(rm):
        _keys(f'{where}.removed[{i}]', x, ['name', 'renamed_to'])
        if not (_is_str(x['name']) and _is_str(x['renamed_to'])):
            raise ReportError(f'{where}.removed[{i}] is {x!r}')
    if s2['removed']['state'] == 'reported':
        rows = [{'name': x['name'] if x['name'] != 'C' else 'C.UTF-8',
                 'renamed_to': x['to'] if x['to'] != 'C' else 'C.UTF-8'}
                for x in s2['removed']['rows']]
        if rm != rows:
            raise ReportError(f'{where}.removed {rm!r}, the Removed file '
                              f'{rows!r}')
    # The C.UTF-8 `!!` of one tag side without the file C: C.UTF-8 is a
    # file only the other side has, unless that side is a build, which
    # can lack it too.
    c = _warnings_of(w, 'step2', 'c_utf8_no_source')
    for d in c or ():
        lack = d['sides']
        if pair['old'] in lack and pair['new'] not in lack and \
                run['new_class'] != 'build' and 'C.UTF-8' not in added:
            raise ReportError(f'{where}: no C at {pair["old"]} alone, and '
                              f'C.UTF-8 is not among the added files')
        if pair['new'] in lack and pair['old'] not in lack and \
                run['old_class'] != 'build' and \
                'C.UTF-8' not in [x['name'] for x in rm]:
            raise ReportError(f'{where}: no C at {pair["new"]} alone, and '
                              f'C.UTF-8 is not among the removed files')
    # A rename is a removed row and an added file, both ways.
    gone = {x['name']: x['renamed_to'] for x in rm}
    came = {x['name']: x['renamed_from'] for x in a['rows']}
    for n, frm in came.items():
        if frm != '-' and gone.get(frm) != n:
            raise ReportError(f'{where}: {n} renamed from {frm}, which '
                              f'Removed gives as {gone.get(frm)!r}')
    for n, to in gone.items():
        if to != '-' and to in came and came[to] != n:
            raise ReportError(f'{where}: {n} renamed to {to}, which the '
                              f'added files give as from {came[to]!r}')
    # The entries of a machine's sources not read.
    un = o['unread']
    if not isinstance(un, list):
        raise ReportError(f'{where}.unread is not a list')
    for i, x in enumerate(un):
        _keys(f'{where}.unread[{i}]', x, ['name', 'why', 'counted'])
        if not (_is_str(x['name']) and _is_strs(x['why']) and x['why']
                and _is_bool(x['counted'])):
            raise ReportError(f'{where}.unread[{i}] is {x!r}')
        if x['counted'] and len(x['why']) != 1:
            raise ReportError(f'{where}.unread[{i}]: counted, yet not read '
                              f'on both machines: {x!r}')
    skipped = _warnings_of(w, 'step1', 'machine_entries_skipped')
    if not machines and un:
        raise ReportError(f'{where}.unread {un!r} with no machines')
    if machines and skipped is not None:
        which = {pair['old']: 'old', pair['new']: 'new'}
        want = {}
        for d in sorted(skipped, key=lambda d: d['machine'] != pair['old']):
            if d['machine'] not in which:
                raise ReportError(f'{where}: a `!!` of skipped entries of '
                                  f'{d["machine"]!r}')
            for e in d['entries']:
                if not e['entry'].startswith('charmaps/'):
                    want.setdefault(e['entry'], []).append(
                        f"{e['why']} on the {which[d['machine']]} machine")
        # Without order: both steps name an entry through shown() (step
        # 2's C as C.UTF-8, mapped below), which quotes a name it cannot
        # print bare (de_DE~), but step 2 sorts by the entry's own name,
        # and a quoted name sorts apart.
        want = sorted([n if n != 'C' else 'C.UTF-8', v]
                      for n, v in want.items())
        if sorted([x['name'], x['why']] for x in un) != want:
            raise ReportError(f'{where}.unread {un!r}, step 1 {want!r}')
    # The not-compared count: the files only one side has and the names
    # of the `!!` of names not compared.
    listed_w = _warnings_of(w, 'step2', 'rules_not_compared')
    if listed_w is not None:
        n_listed = sum(len(g_['names']) for d in listed_w
                       for g_ in d['groups'])
        n_unread = sum(1 for x in un if x['counted'])
        if t['not_compared'] != len(added) + len(rm) + n_listed + n_unread:
            raise ReportError(f'{where}.totals.not_compared '
                              f'{t["not_compared"]}: {len(added)} added, '
                              f'{len(rm)} removed, {n_listed} listed, '
                              f'{n_unread} not read')
    # The notes under the list.
    import glibc_locale_data as g
    ub = o['unbuilt']
    _stated(f'{where}.unbuilt', ub, ('listed', 'not applicable'),
            {'listed': ['names'], 'not applicable': ['why']})
    if (ub['state'] == 'not applicable') != machines:
        raise ReportError(f'{where}.unbuilt is {ub["state"]!r} with '
                          f'machines={machines}')
    ls = s2['listed']
    if ls['state'] == 'reported':
        shown = {g.shown(n) if n != 'C' else 'C.UTF-8'
                 for n in ls['compared'] + ls['not_compared']}
        if ub['state'] == 'listed':
            _strs(f'{where}.unbuilt.names', ub['names'])
            if not set(ub['names']) <= shown:
                raise ReportError(f'{where}.unbuilt {ub["names"]!r} not all '
                                  f'in the list')
        _strs(f'{where}.not_ascii', o['not_ascii'])
        bare = [n for n in o['not_ascii'] if g.SAFE_NAME.fullmatch(n)]
        if bare:
            raise ReportError(f'{where}.not_ascii {bare!r} printed bare')
        if set(o['not_ascii']) & shown:
            raise ReportError(f'{where}.not_ascii {o["not_ascii"]!r} in the '
                              f'list')
        for n in names + [m for d in listed_w or () for g_ in d['groups']
                          for m in g_['names']]:
            if n not in shown and n not in o['not_ascii']:
                raise ReportError(f'{where}: {n} is neither in the list nor '
                                  f'among the names not ASCII')


def _character_sets(where, s2, some, w):
    """step2.character_sets: its shape, then each fact tied to the others
    of the same run: its state and reason are step2_rules_by_name's; NOT
    RUN is the `!!` that says so; every build it names is of a pair that
    changed, and a pair not compared names no character; the builds it
    could not settle, those a distro can build, the charmaps the search
    skipped and the entries not compared are the `!!` of each; the builds
    it lists are in step 2's list and not among the names whose rules
    differ or were not compared, those the summary lists anyway are among
    them; on two machines each build is a measured name whose rules by
    name carry what the check found."""
    import glibc_locale_data as g
    o = s2['character_sets']
    if o['state'] == 'not reported':
        _keys(where, o, ['state', 'why'])
        return
    _one_of(f'{where}.state', o['state'], CS_STATES)
    rb = s2['rules_by_name']
    if rb['state'] == 'reported':
        cm = rb['charmaps']
        if (cm['state'] == 'compared') != (o['state'] == 'reported') or \
                (o['state'] != 'reported' and (cm['state'] != o['state']
                                               or cm.get('why')
                                               != o.get('why'))):
            raise ReportError(f'{where} is {o["state"]!r} '
                              f'({o.get("why")!r}), step 2\'s rules by name '
                              f'{cm!r}')
    if (o['state'] in ('reported', 'not run')) != some:
        raise ReportError(f'{where} is {o["state"]!r} where step 2 compared '
                          f'{"something" if some else "nothing"}')
    if o['state'] in ('one commit', 'one tree'):
        _keys(where, o, ['state'])
        return
    if o['state'] == 'not read':
        _keys(where, o, ['state', 'why'])
        _strs(f'{where}.why', o['why'])
        return
    _strs(f'{where}.not_compared', o['not_compared'])
    not_run = _warnings_of(w, 'step2', 'charmaps_not_run')
    missing = _warnings_of(w, 'step2', 'charmap_sources_missing')
    if not_run is not None:
        want = [{'why': o['why']}] if o['state'] == 'not run' else []
        if not_run != want:
            raise ReportError(f'{where} is {o["state"]!r}, the `!!` of the '
                              f'check not run {not_run!r}')
    if missing is not None:
        got = [d['entries'] for d in missing]
        if got != ([o['not_compared']] if o['not_compared'] else []):
            raise ReportError(f'{where}.not_compared {o["not_compared"]!r}, '
                              f'the `!!` {got!r}')
    if o['state'] == 'not run':
        _keys(where, o, ['state', 'why', 'not_compared'])
        if not _is_str(o['why']):
            raise ReportError(f'{where}.why is {o["why"]!r}')
        return
    _one_of(f'{where}.basis', o['basis'], CS_BASES)
    machines = rb['state'] == 'reported' and rb['machines']
    if rb['state'] == 'reported' and (o['basis'] == 'measured') != machines:
        raise ReportError(f'{where}.basis is {o["basis"]!r} with '
                          f'machines={machines}')
    for d in missing or ():
        if d['basis'] != o['basis']:
            raise ReportError(f'{where}.basis {o["basis"]!r}, the `!!` of '
                              f'entries not compared {d["basis"]!r}')
    _strs(f'{where}.charmaps', o['charmaps'])
    if not (_is_positive(o['builds']) and o['charmaps'] and
            o['charmaps'] == sorted(set(o['charmaps']))):
        raise ReportError(f'{where}: {o["builds"]!r} builds, the charmaps '
                          f'{o["charmaps"]!r}')
    used = set(o['charmaps'])
    # The pairs that changed.
    pairs = {}
    if not isinstance(o['changed'], list):
        raise ReportError(f'{where}.changed is not a list')
    for i, x in enumerate(o['changed']):
        at = f'{where}.changed[{i}]'
        _stated(at, x, CS_PAIR_STATES,
                {'compared': ['old', 'new', 'added', 'removed', 'other'],
                 'not compared': ['old', 'new']})
        if not (_is_str(x['old']) and _is_str(x['new'])):
            raise ReportError(f'{at} is {x!r}')
        if x['state'] == 'compared' and not all(
                _is_count(x[k]) for k in ('added', 'removed', 'other')):
            raise ReportError(f'{at} is {x!r}')
        if (x['old'], x['new']) in pairs:
            raise ReportError(f'{at}: the pair twice')
        if not {x['old'], x['new']} <= used:
            raise ReportError(f'{at}: {x["old"]} -> {x["new"]}, not among '
                              f'the charmaps the builds use')
        pairs[(x['old'], x['new'])] = x['state']
    # Every build it names.
    keys = ['entry', 'source', 'old', 'new']
    shapes = {'listed': keys + ['codepoints', 'via'],
              'unsettled': keys + ['why'],
              'distro': keys + ['codepoints', 'via']}
    rows = []
    for part in ('listed', 'anyway', 'unsettled', 'distro'):
        if not isinstance(o[part], list):
            raise ReportError(f'{where}.{part} is not a list')
        for i, x in enumerate(o[part]):
            at = f'{where}.{part}[{i}]'
            if part == 'anyway':
                _stated(at, x, CS_ANYWAY_STATES,
                        {'named': keys + ['count', 'via'],
                         'not compared': keys + ['why']})
            else:
                _keys(at, x, shapes[part])
            if not all(_is_str(x[k]) for k in keys):
                raise ReportError(f'{at} is {x!r}')
            if (x['old'], x['new']) not in pairs:
                raise ReportError(f'{at}: {x["old"]} -> {x["new"]} is no '
                                  f'pair that changed')
            if 'via' in x:
                _strs(f'{at}.via', x['via'])
            if 'codepoints' in x and not (
                    x['codepoints'] and all(map(_is_count, x['codepoints']))
                    and x['codepoints'] == sorted(set(x['codepoints']))):
                raise ReportError(f'{at}.codepoints is {x["codepoints"]!r}')
            if 'count' in x and not _is_positive(x['count']):
                raise ReportError(f'{at}.count is {x["count"]!r}')
            # Point 5 searches a charmap whose name stays.
            if part == 'distro' and x['old'] != x['new']:
                raise ReportError(f'{at}: {x["old"]} -> {x["new"]}')
            if 'why' in x and not _is_str(x['why']):
                raise ReportError(f'{at}.why is {x["why"]!r}')
            if (x['entry'] == '-') != (part == 'distro') and \
                    part != 'unsettled' and part != 'anyway':
                raise ReportError(f'{at}: entry {x["entry"]!r}')
            # A SUPPORTED entry builds from the file its name names
            # (collation_keys.supported_builds).
            if o['basis'] == 'SUPPORTED' and x['entry'] != '-' and \
                    x['source'] != g.locale_source(x['entry']):
                raise ReportError(f'{at}: {x["entry"]} built from '
                                  f'{x["source"]}')
            rows.append((part, x))
    # A build names characters of its pair's changes (collation_keys.K):
    # no more than changed in it.
    size = {(x['old'], x['new']): x['added'] + x['removed'] + x['other']
            for x in o['changed'] if x['state'] == 'compared'}
    for part, x in rows:
        n = len(x['codepoints']) if 'codepoints' in x else x.get('count', 0)
        if n > size.get((x['old'], x['new']), n):
            raise ReportError(f'{where}.{part}: {x["entry"]} names {n} '
                              f'character(s), more than changed in '
                              f'{x["old"]} -> {x["new"]}')
    # A pair not compared names no character: each of its builds says why,
    # one and the same reason (a pair has a build at least: compare() takes
    # its pairs from the builds compared).
    for p, state in pairs.items():
        mine = [(part, x) for part, x in rows if (x['old'], x['new']) == p]
        if state == 'not compared':
            whys = {x.get('why') for part, x in mine}
            if None in whys or len(whys) != 1:
                raise ReportError(f'{where}: {p[0]} -> {p[1]} not compared, '
                                  f'its builds {mine!r}')
    # The `!!` of each.
    for part, kind, want in (
            ('unsettled', 'charmap_builds_unsettled',
             lambda xs: {'builds': [
                 {'build': x['entry'] if x['entry'] != '-' else x['source'],
                  'charmap': x['new'], 'why': x['why']} for x in xs]}),
            ('distro', 'charmap_distro_buildable',
             lambda xs: {'locales': [{'source': x['source'],
                                      'charmap': x['new']} for x in xs]}),
            ('unsearched', 'charmap_search_not_run',
             lambda xs: {'charmaps': xs})):
        got = _warnings_of(w, 'step2', kind)
        if got is not None and got != ([want(o[part])] if o[part] else []):
            raise ReportError(f'{where}.{part} {o[part]!r}, the `!!` {got!r}')
    # The search is skipped for a charmap whose name stays and that could
    # not be compared (collation_keys.compare), for that same reason.
    for i, x in enumerate(o['unsearched']):
        _keys(f'{where}.unsearched[{i}]', x, ['charmap', 'why'])
        if not (_is_str(x['charmap']) and _is_str(x['why'])):
            raise ReportError(f'{where}.unsearched[{i}] is {x!r}')
        p = (x['charmap'], x['charmap'])
        whys = {y.get('why') for part, y in rows
                if (y['old'], y['new']) == p}
        if pairs.get(p) != 'not compared' or whys != {x['why']}:
            raise ReportError(f'{where}.unsearched[{i}]: {x!r}, its pair '
                              f'{pairs.get(p)!r} with the reasons '
                              f'{sorted(map(str, whys))!r}')
    if machines and (o['distro'] or o['unsearched']):
        raise ReportError(f'{where}: the search of point 5 on two machines')
    # Against step 2's list: what the summary lists anyway is a name whose
    # rules differ or were not compared; what the check lists is neither,
    # and is in the list.
    shown = lambda n: 'C.UTF-8' if n == 'C' else g.shown(n)
    ru, ls = s2['rules'], s2['listed']
    if ru['state'] == 'reported':
        lw = _warnings_of(w, 'step2', 'rules_not_compared')
        already = {n for x in ru['differ'] for n in x['names']}
        already |= {n for d in lw or () for g_ in d['groups']
                    for n in g_['names']}
        for part, x in rows:
            if (shown(x['source']) in already) != (part == 'anyway'):
                raise ReportError(
                    f'{where}.{part}: {x["source"]} '
                    f'{"is not" if part == "anyway" else "is"} among the '
                    f'names whose rules differ or were not compared')
    if ls['state'] == 'reported':
        names = {shown(n) for n in ls['compared'] + ls['not_compared']}
        names |= set(ru.get('not_ascii', []))
        for part, x in rows:
            if part != 'anyway' and shown(x['source']) not in names:
                raise ReportError(f'{where}.{part}: {x["source"]} is not in '
                                  f'step 2\'s list')
    # On two machines, each build is a name measured on the new one, whose
    # rules by name carry what the check found of its file.
    if machines and rb['locales']['state'] == 'reported':
        bn = rb['locales']['by_name']
        for part, x in rows:
            v = bn.get(x['entry'], {}).get('charmap')
            ok = (v == 'changed' if part == 'listed' or
                  (part == 'anyway' and x['state'] == 'named') else
                  v in ('changed', 'transliteration', 'not compared'))
            if not ok:
                raise ReportError(f'{where}.{part}: {x["entry"]} is '
                                  f'{v!r} in step 2\'s rules by name')


def _transliteration(where, s2, w):
    """step2.transliteration: its shape, then each fact tied to the others
    of the same run: where the character set check compared no build, the
    same state and reason as step2.character_sets; 'no change' only where
    no charmap changed; the line it opens with is the one its counts and
    lists give; the elements it counts are no fewer than the builds and
    than those built differently, which are those its list counts where
    it leaves none to step 2's list; each build is of the charmaps the
    check read, not one the character set check lists, in step 2's list
    and not among the names whose rules differ or were not compared, and
    on two machines a measured name whose rules by name carry it; the
    builds it could not compare are its `!!`."""
    import glibc_locale_data as g
    o, cs = s2['transliteration'], s2['character_sets']
    if (o['state'] == 'not reported') != (cs['state'] == 'not reported'):
        raise ReportError(f'{where} is {o["state"]!r}, step2.character_sets '
                          f'{cs["state"]!r}')
    if o['state'] == 'not reported':
        _keys(where, o, ['state', 'why'])
        return
    _one_of(f'{where}.state', o['state'], TR_STATES)
    unsettled_w = _warnings_of(w, 'step2', 'translit_unsettled')
    if cs['state'] != 'reported':
        # The character set check compared no build: what it says covers
        # the transliterations too.
        want = {k: cs[k] for k in ('state', 'why') if k in cs}
        if o != want:
            raise ReportError(f'{where} is {o!r}, step2.character_sets '
                              f'{want!r}')
        if unsettled_w:
            raise ReportError(f'{where}: the `!!` {unsettled_w!r} with no '
                              f'build compared')
        return
    if o['state'] not in ('reported', 'no change'):
        raise ReportError(f'{where} is {o["state"]!r} where the character '
                          f'set check compared {cs["builds"]} build(s)')
    if o['state'] == 'no change':
        _keys(where, o, ['state'])
        # Point 6 runs on every pair a charmap changed in
        # (collation_keys.compare).
        if cs['changed']:
            raise ReportError(f'{where} is {o["state"]!r} with '
                              f'{len(cs["changed"])} charmap(s) changed')
        if unsettled_w:
            raise ReportError(f'{where}: the `!!` {unsettled_w!r}')
        return
    line = o.get('line')
    _one_of(f'{where}.line', line, TR_LINES)
    _keys(where, o, ['state', 'line', 'listed', 'unsettled', 'skipped']
          + TR_LINES[line])
    keys = ['entry', 'source', 'old', 'new']
    rows = []
    for part, more in (('listed', ['count', 'codepoints', 'via']),
                       ('unsettled', ['why']), ('skipped', None)):
        if not isinstance(o[part], list):
            raise ReportError(f'{where}.{part} is not a list')
        for i, x in enumerate(o[part]):
            at = f'{where}.{part}[{i}]'
            if more is None:
                _stated(at, x, TR_SKIPPED_STATES,
                        {'built differently': keys + ['count'],
                         'not compared': keys + ['why']})
                if x['state'] == 'built differently' and \
                        not _is_positive(x['count']):
                    raise ReportError(f'{at}.count is {x["count"]!r}')
            else:
                _keys(at, x, keys + more)
            if not all(_is_str(x[k]) for k in keys):
                raise ReportError(f'{at} is {x!r}')
            if not {x['old'], x['new']} <= set(cs['charmaps']):
                raise ReportError(f'{at}: {x["old"]} -> {x["new"]}, not '
                                  f'among the charmaps the builds use')
            if part == 'skipped':
                pass
            elif part == 'listed':
                if not _is_positive(x['count']):
                    raise ReportError(f'{at}.count is {x["count"]!r}')
                if not (isinstance(x['codepoints'], list)
                        and all(map(_is_count, x['codepoints']))
                        and x['codepoints'] == sorted(set(x['codepoints']))):
                    raise ReportError(f'{at}.codepoints is '
                                      f'{x["codepoints"]!r}')
                if not _is_strs(x['via']):
                    raise ReportError(f'{at}.via is {x["via"]!r}')
            if 'why' in x and not _is_str(x['why']):
                raise ReportError(f'{at}.why is {x["why"]!r}')
            # A SUPPORTED entry builds from the file its name names
            # (collation_keys.supported_builds).
            if cs['basis'] == 'SUPPORTED' and \
                    x['source'] != g.locale_source(x['entry']):
                raise ReportError(f'{at}: {x["entry"]} built from '
                                  f'{x["source"]}')
            rows.append((part, x))
    # The line it opens with, from what it found (print_translit_report).
    listed, unsettled, skipped = o['listed'], o['unsettled'], o['skipped']
    # A build compared and built differently is listed, or left to step
    # 2's list; one not compared says why, either way; every other build
    # counted is a build compared (collation_keys.compare_translit: each
    # build of the check ends in one of these, once).
    sk_built = [x for x in skipped if x['state'] == 'built differently']
    sk_not = [x for x in skipped if x['state'] == 'not compared']
    if len(listed) + len(unsettled) + len(skipped) > cs['builds']:
        raise ReportError(f'{where}: {len(listed)} listed, {len(unsettled)} '
                          f'not compared and {len(skipped)} left to the '
                          f'list, of {cs["builds"]} build(s) compared')
    if line == 'elements':
        for k in ('elements', 'builds'):
            if not _is_positive(o[k]):
                raise ReportError(f'{where}.{k} is {o[k]!r}')
        if not _is_count(o['changed']):
            raise ReportError(f'{where}.changed is {o["changed"]!r}')
        # A build is counted with each element it built at both sides
        # (collation_keys.compare_translit), and every build it lists was
        # counted.
        if not (o['builds'] <= o['elements'] and o['changed'] <= o['elements']
                and o['builds'] + len(unsettled) + len(sk_not)
                <= cs['builds']
                and len(listed) + len(sk_built) <= o['builds']):
            raise ReportError(f'{where}: {o["elements"]} element(s) of '
                              f'{o["builds"]} build(s), {o["changed"]} '
                              f'built differently, {len(listed)} listed, '
                              f'{cs["builds"]} compared')
        # Counted before the builds step 2 lists anyway are taken out.
        n = sum(x['count'] for x in listed + sk_built)
        if o['changed'] != n:
            raise ReportError(f'{where}: {o["changed"]} built differently, '
                              f'its list and those left to the list {n}')
    else:
        if listed or sk_built:
            raise ReportError(f'{where}: {len(listed + sk_built)} build(s) '
                              f'built differently with the line {line!r}')
        if line == 'unmoved':
            if not (_is_positive(o['unmoved'])
                    and o['unmoved'] + len(unsettled) + len(sk_not)
                    <= cs['builds']):
                raise ReportError(f'{where}.unmoved is {o["unmoved"]!r} of '
                                  f'{cs["builds"]} build(s)')
        elif line == 'no line':
            if not unsettled:
                raise ReportError(f'{where}: no line, and no build it could '
                                  f'not compare')
        elif unsettled or (line == 'listed already') != bool(skipped):
            raise ReportError(f'{where}: the line {line!r} with '
                              f'{len(unsettled)} build(s) not compared and '
                              f'{len(skipped)} left to the list')
    # The `!!` of the builds it could not compare.
    if unsettled_w is not None:
        want = [{'builds': [{'build': x['entry'], 'charmap': x['new'],
                             'why': x['why']} for x in unsettled]}] \
            if unsettled else []
        if unsettled_w != want:
            raise ReportError(f'{where}.unsettled {unsettled!r}, the `!!` '
                              f'{unsettled_w!r}')
    # Not a build the character set check lists (collation_keys.left_out).
    theirs = {x['entry'] for k in ('listed', 'unsettled', 'distro')
              for x in cs[k] if x['entry'] != '-'}
    for part, x in rows:
        if part != 'skipped' and x['entry'] in theirs:
            raise ReportError(f'{where}.{part}: {x["entry"]}, which the '
                              f'character set check lists')
    # Against step 2's list: not among the names whose rules differ or
    # were not compared, and in the list.
    shown = lambda n: 'C.UTF-8' if n == 'C' else g.shown(n)
    ru, ls = s2['rules'], s2['listed']
    if ru['state'] == 'reported':
        lw = _warnings_of(w, 'step2', 'rules_not_compared')
        already = {n for x in ru['differ'] for n in x['names']}
        already |= {n for d in lw or () for g_ in d['groups']
                    for n in g_['names']}
        for part, x in rows:
            # What it leaves to the list is a name whose rules differ or
            # were not compared, or a build the character set check lists
            # (collation_keys.left_out); what it lists is neither.
            if part == 'skipped':
                if shown(x['source']) not in already and \
                        x['entry'] not in theirs:
                    raise ReportError(f'{where}.skipped: {x["entry"]} is '
                                      f'listed by nothing else')
            elif shown(x['source']) in already:
                raise ReportError(f'{where}.{part}: {x["source"]} is among '
                                  f'the names whose rules differ or were not '
                                  f'compared')
    if ls['state'] == 'reported':
        names = {shown(n) for n in ls['compared'] + ls['not_compared']}
        names |= set(ru.get('not_ascii', []))
        for part, x in rows:
            if shown(x['source']) not in names:
                raise ReportError(f'{where}.{part}: {x["source"]} is not in '
                                  f'step 2\'s list')
    # On two machines, each build is a name measured on the new one, whose
    # rules by name carry what the check found of its file, the strongest
    # of it (collation_keys.listed_sources).
    rb = s2['rules_by_name']
    if rb['state'] == 'reported' and rb['machines'] and \
            rb['locales']['state'] == 'reported':
        bn = rb['locales']['by_name']
        for part, x in rows:
            v = bn.get(x['entry'], {}).get('charmap')
            built = part == 'listed' or \
                x.get('state') == 'built differently'
            ok = (v in ('changed', 'transliteration') if built else
                  v in ('changed', 'transliteration', 'not compared',
                        'transliteration not compared'))
            if not ok:
                raise ReportError(f'{where}.{part}: {x["entry"]} is '
                                  f'{v!r} in step 2\'s rules by name')


def _charmap_by_name(where, s2, w):
    """On two machines, what step 2's rules by name carry of its character
    set check for each measured name, held to the check's rows of the same
    run in both directions (rules_by_name, collation_keys.listed_sources):
    each file the check found something of takes the strongest of it, a
    row with a reason being not compared (of a charmap, or of its
    transliterations); a name takes that of its key,
    else of its source; one of neither is not compared where a machine
    could not trace it to its charmap (step 2's `!!` names_untraced), else
    '-'. So a name marked with no row behind it, and a row whose names are
    not marked, are refused alike. C and POSIX are built in: no key, no
    source, no character set. Where the key is not the name itself it is
    the source (a name compare_rules does not have goes to its file).
    Where step 2's `!!` are not reported, a name not compared with no row
    behind it is not checked."""
    rb = s2['rules_by_name']
    if rb['state'] != 'reported' or rb['locales']['state'] != 'reported':
        return
    found = {}
    rows = []
    cs, tr = s2['character_sets'], s2['transliteration']
    if cs['state'] == 'reported':
        rows += [(x, 'not compared' if 'why' in x else 'changed')
                 for part in ('listed', 'anyway', 'unsettled', 'distro')
                 for x in cs[part]]
    if tr['state'] == 'reported':
        rows += [(x, 'transliteration not compared' if 'why' in x
                  else 'transliteration')
                 for part in ('listed', 'unsettled', 'skipped')
                 for x in tr[part]]
    for x, state in rows:
        s = x['source']
        if s not in found or \
                LISTED_STATES.index(state) < LISTED_STATES.index(found[s]):
            found[s] = state
    untraced = _warnings_of(w, 'step2', 'names_untraced')
    if untraced is not None:
        untraced = {x['name'] for d in untraced for x in d['names']}
    for name, x in sorted(rb['locales']['by_name'].items()):
        at = f'{where}.by_name[{name!r}]'
        if x['rules'] == 'built in':
            if (x['key'], x['source'], x['charmap']) != ('none', 'none', '-'):
                raise ReportError(f'{at} is {x!r}, built into glibc')
            continue
        if not (_is_str(x['source']) and x['source'] != 'none'):
            raise ReportError(f'{at}.source is {x["source"]!r}')
        if x['key'] != name and x['key'] != x['source']:
            raise ReportError(f'{at}: the key {x["key"]!r}, the source '
                              f'{x["source"]!r}')
        want = found.get(x['key']) or found.get(x['source'])
        if want is None:
            if untraced is None:
                if x['charmap'] == 'not compared':
                    continue
                want = '-'
            else:
                want = 'not compared' if name in untraced else '-'
        if x['charmap'] != want:
            raise ReportError(f'{at}.charmap is {x["charmap"]!r}, the '
                              f'character set check of its file '
                              f'{want!r}')


TAG_STATUSES = ('good',) + tuple(SIGNATURE_WHY)
HEX12 = re.compile(r'[0-9a-f]{12}')
HEX64 = re.compile(r'[0-9a-f]{64}')


def _build_tag(where, name):
    import glibc_locale_data as g
    parsed = g.build_name(name)
    if parsed is None:
        raise ReportError(f'{where}: {name!r} is no build name')
    return 'glibc-' + parsed[0], parsed[0]


LATEST_WHEN = re.compile(r'\d{4}-\d\d-\d\d \d\d:\d\d UTC')


def _latest(x, pair, sides, es):
    """compared.latest: 'not asked' when no side was typed as latest-elN
    (audit.sh passes the sides as typed only then), else each N typed as
    latest-elN, once, in the order of the sides, named as first typed
    and resolved to the build of its side; a side typed otherwise is the
    side without its architecture; each resolved build is a build of
    that RHEL major rebuilt in this run, in a folder pub/rocky/N; the
    date of its metadata as printed. That it is the newest build there,
    the mirror and the date are tied here to nothing."""
    import glibc_locale_data as g
    _stated('compared.latest', x, ('reported', 'not asked', 'not reported'),
            {'reported': ['asked', 'resolved'], 'not asked': [],
             'not reported': ['why']})
    if x['state'] != 'reported':
        return
    asked = x['asked']
    if not (_is_strs(asked) and len(asked) == 2):
        raise ReportError(f'compared.latest.asked is {asked!r}')
    want = {}
    for typed, (name, _) in zip(asked, sides):
        n = g.latest_major(typed)
        if n is None:
            if g.without_arch(typed) != name:
                raise ReportError(f'compared.latest: {typed!r} typed, the '
                                  f'side is {name!r}')
        elif want.setdefault(n, (typed, name))[1] != name:
            raise ReportError(f'compared.latest: {typed!r} is {name!r}')
    if not want:
        raise ReportError(f'compared.latest {x!r}: no side typed latest-elN')
    res = x['resolved']
    if not isinstance(res, list):
        raise ReportError('compared.latest.resolved is not a list')
    for i, e in enumerate(res):
        at = f'compared.latest.resolved[{i}]'
        _keys(at, e, ['name', 'build', 'folder', 'when'])
        _stated(f'{at}.when', e['when'], ('dated', 'no date'),
                {'dated': ['utc'], 'no date': []})
        if e['when']['state'] == 'dated' and not (
                isinstance(e['when']['utc'], str)
                and LATEST_WHEN.fullmatch(e['when']['utc'])):
            raise ReportError(f'{at}.when is {e["when"]!r}')
        n = g.latest_major(e['name'])
        bn = g.build_name(e['build']) if _is_str(e['build']) else None
        if n is None or bn is None or bn[2] != n or \
                not _is_str(e['folder']) or \
                not e['folder'].endswith(f'pub/rocky/{n}'):
            raise ReportError(f'{at} is {e!r}')
    if [(e['name'], e['build']) for e in res] != list(want.values()):
        raise ReportError(f'compared.latest resolved {res!r}, the sides '
                          f'typed {asked!r} are {want!r}')
    if not {b for _, b in want.values()} <= {e['build'] for e in es}:
        raise ReportError(f'compared.latest {list(want.values())!r} not '
                          f'rebuilt')


def _compared(r):
    """compared: its shape, then these facts tied to others of the same
    run: the tags are the sides' (a build's by its version); their
    statuses that are not good are the `!!` of signatures; each machine is
    a side of the pair, on its tag; the machines are there exactly when
    their `!!` is, the rebuilt builds exactly when theirs is; each build
    rebuilt from its packages is a side that is no machine, in order, on
    its tag, its first "checked:" line the tarball against that tag, and
    what its `!!` say of it (whether any file was fetched, the SRPMs no
    metadata lists, each under the folder its `!!` names, the charmaps
    not checked) is its own; the glibc version is each build's
    and its `!!`; step 0's note is the pair's; latest-elN as _latest
    says. A tag's commit, a signature's detail, a tag's glibc version, the
    counts of locale sources and charmaps, why a machine's file holds no
    charmaps, the architecture of `rpm -q glibc`, a build's "checked:"
    lines after the first and its counts of patches, the sha256 of each
    file and the URL of each file metadata lists are tied here to
    nothing; _harness.compared_held holds them to the printed text."""
    c, run, w, pair = r['compared'], r['run'], r['warnings'], r['pair']
    _stated('compared', c, ('reported', 'not reported'),
            {'reported': ['tags', 'machines', 'builds', 'collation_version',
                          'note', 'latest'],
             'not reported': ['why']})
    if c['state'] == 'not reported':
        return
    sides = [(pair['old'], run['old_class']), (pair['new'], run['new_class'])]
    # The tags, once each, in the order of the sides.
    want = []
    for name, cls in sides:
        t = _build_tag('compared', name)[0] if cls == 'build' else name
        if t not in want:
            want.append(t)
    tags = c['tags']
    if not isinstance(tags, list):
        raise ReportError('compared.tags is not a list')
    for i, t in enumerate(tags):
        at = f'compared.tags[{i}]'
        _keys(at, t, ['ref', 'commit', 'status', 'detail'])
        _one_of(f'{at}.status', t['status'], TAG_STATUSES)
        if not (_is_str(t['ref']) and _is_str(t['detail'])
                and isinstance(t['commit'], str)
                and HEX12.fullmatch(t['commit'])):
            raise ReportError(f'{at} is {t!r}')
    if [t['ref'] for t in tags] != want:
        raise ReportError(f'compared.tags {[t["ref"] for t in tags]!r} are '
                          f'not the sides\' {want!r}')
    sig = _warnings_of(w, 'step1', 'signatures_unchecked')
    if sig is not None:
        got = [{'ref': t['ref'], 'status': t['status']} for t in tags
               if t['status'] != 'good']
        if len(sig) > 1 or got != (sig[0]['refs'] if sig else []):
            raise ReportError(f'compared.tags not good {got!r}, the `!!` of '
                              f'signatures {sig!r}')
    # The machines.
    ms = c['machines']
    if not isinstance(ms, list):
        raise ReportError('compared.machines is not a list')
    builds = [name for name, cls in sides if cls == 'build']
    for i, m in enumerate(ms):
        at = f'compared.machines[{i}]'
        _keys(at, m, ['build', 'commit', 'tag', 'word',
                      'measurement_sha256', 'locales', 'charmaps'])
        if m['tag'] != _build_tag(at, m['build'])[0]:
            raise ReportError(f'{at}.tag {m["tag"]!r} is not that of '
                              f'{m["build"]!r}')
        if not (isinstance(m['commit'], str) and HEX12.fullmatch(m['commit'])
                and isinstance(m['measurement_sha256'], str)
                and HEX64.fullmatch(m['measurement_sha256'])
                and _is_count(m['locales']) and _is_str(m['word'])):
            raise ReportError(f'{at} is {m!r}')
        # Each word `rpm -q glibc` printed, by the rule machine_build reads
        # them with: two words on a machine with glibc of 32 and 64 bits.
        import locale_order as lo
        parts = [lo.build_parts(x) for x in m['word'].split()]
        if not parts or None in parts or \
                {'glibc-%s-%s' % p for p in parts} != {m['build']}:
            raise ReportError(f'{at}.word {m["word"]!r} is not '
                              f'{m["build"]!r}')
        _stated(f'{at}.charmaps', m['charmaps'], ('counted', 'none'),
                {'counted': ['count'], 'none': ['why']})
        if m['charmaps']['state'] == 'counted' and \
                not _is_count(m['charmaps']['count']):
            raise ReportError(f'{at}.charmaps is {m["charmaps"]!r}')
        if m['charmaps']['state'] == 'none' and \
                not _is_str(m['charmaps']['why']):
            raise ReportError(f'{at}.charmaps is {m["charmaps"]!r}')
    if [m['build'] for m in ms] not in ([], builds):
        raise ReportError(f'compared.machines {[m["build"] for m in ms]!r} '
                          f'are not the build sides {builds!r}')
    said = _warnings_of(w, 'step1', 'machine_not_checked')
    if said is not None and bool(said) != bool(ms):
        raise ReportError(f'compared.machines {len(ms)}, the `!!` of what no '
                          f'step reads of a machine {len(said)} time(s)')
    skipped = _warnings_of(w, 'step1', 'machine_entries_skipped') or []
    if not {s['machine'] for s in skipped} <= {m['build'] for m in ms}:
        raise ReportError(f'entries skipped of {skipped!r}, machines '
                          f'{[m["build"] for m in ms]!r}')
    # The builds rebuilt from their packages (24.3c-2).
    b = c['builds']
    _stated('compared.builds', b, ('none', 'rebuilt'),
            {'none': [], 'rebuilt': ['entries']})
    es = b.get('entries', [])
    if not isinstance(es, list) or (b['state'] == 'rebuilt') != bool(es):
        raise ReportError(f'compared.builds is {b!r}')
    unlisted = []
    for i, e in enumerate(es):
        at = f'compared.builds.entries[{i}]'
        _keys(at, e, ['build', 'commit', 'tag', 'files', 'checked',
                      'patches', 'charmaps_unchecked'])
        cu = e['charmaps_unchecked']
        if not isinstance(cu, list) or not all(
                isinstance(u, dict) and sorted(u) == ['package', 'why']
                and _is_str(u['package']) and _is_str(u['why'])
                for u in cu):
            raise ReportError(f'{at}.charmaps_unchecked is {cu!r}')
        if not (_is_str(e['build']) and isinstance(e['commit'], str)
                and HEX12.fullmatch(e['commit'])
                and isinstance(e['files'], list) and _is_strs(e['checked'])
                and e['checked']):
            raise ReportError(f'{at} is {e!r}')
        if e['tag'] != _build_tag(at, e['build'])[0]:
            raise ReportError(f'{at}.tag {e["tag"]!r} is not that of '
                              f'{e["build"]!r}')
        # The first "checked:" line is the tarball's, against that tag.
        import build_sources as bs
        if e['checked'][0] != bs.TARBALL_IS_TAG % e['tag']:
            raise ReportError(f'{at}.checked[0] {e["checked"][0]!r} is not '
                              f'the tarball against {e["tag"]!r}')
        _keys(f'{at}.patches', e['patches'], ['applied', 'of'])
        if not (_is_count(e['patches']['applied'])
                and _is_count(e['patches']['of'])
                and e['patches']['applied'] <= e['patches']['of']):
            raise ReportError(f'{at}.patches is {e["patches"]!r}')
        # The three files fetched, or none (a Side made by hand).
        import glibc_locale_data as g
        if [f.get('package') if isinstance(f, dict) else None
                for f in e['files']] not in (
                    [], [k for k, _ in g.BUILD_FILES]):
            raise ReportError(f'{at}.files are {e["files"]!r}')
        for j, f in enumerate(e['files']):
            _keys(f'{at}.files[{j}]', f, ['package', 'url', 'sha256',
                                          'listed'])
            if not (_is_str(f['url']) and isinstance(f['sha256'], str)
                    and HEX64.fullmatch(f['sha256'])
                    and isinstance(f['listed'], bool)
                    and (f['listed'] or f['package'] == 'srpm')):
                raise ReportError(f'{at}.files[{j}] is {f!r}')
            if not f['listed']:
                unlisted.append((e['build'], f['url']))
    if [e['build'] for e in es] not in (
            [], [n for n in builds if n not in {m['build'] for m in ms}]):
        raise ReportError(f'compared.builds {[e["build"] for e in es]!r} '
                          f'are not the build sides {builds!r}')
    said = _warnings_of(w, 'step1', 'build_not_checked')
    if said is not None and bool(said) != (b['state'] != 'none'):
        raise ReportError(f'compared.builds {b!r}, the `!!` of what is not '
                          f'checked for builds {len(said)} time(s)')
    if said:
        d = said[0]
        got = [(u['build'], u['folder']) for u in d['unlisted']]
        if len(said) > 1 or \
                d['metadata'] != any(e['files'] for e in es) or \
                [x for x, _ in got] != [x for x, _ in unlisted] or \
                not all(url.startswith(folder + '/')
                        for (_, folder), (_, url) in zip(got, unlisted)):
            raise ReportError(f'compared.builds {es!r}, the `!!` of what is '
                              f'not checked for builds {said!r}')
    warned = _warnings_of(w, 'step1', 'build_charmaps_unchecked')
    want = [dict(u, build=e['build']) for e in es
            for u in e['charmaps_unchecked']]
    if warned is not None and warned != want:
        raise ReportError(f'charmaps not checked {warned!r}, of the builds '
                          f'{want!r}')
    if [n for n in builds if n not in {m['build'] for m in ms}] and \
            b['state'] == 'none':
        raise ReportError(f'compared: build sides {builds!r} that are '
                          f'neither machines nor rebuilt builds')
    # The glibc version of each side.
    v = c['collation_version']
    _stated('compared.collation_version', v, ('differ', 'same', 'unknown'),
            {'differ': ['old', 'new'], 'same': ['version'],
             'unknown': ['sides']})
    versions = {'differ': [v.get('old'), v.get('new')],
                'same': [v.get('version')] * 2}.get(v['state'])
    if versions is not None:
        if not all(_is_str(x) for x in versions) or \
                (v['state'] == 'differ' and versions[0] == versions[1]):
            raise ReportError(f'compared.collation_version is {v!r}')
        for (name, cls), have in zip(sides, versions):
            if cls == 'build' and _build_tag('compared', name)[1] != have:
                raise ReportError(f'compared.collation_version {v!r}, the '
                                  f'build {name!r}')
    elif not _is_strs(v['sides']) or not v['sides']:
        raise ReportError(f'compared.collation_version is {v!r}')
    same = _warnings_of(w, 'step1', 'collation_version_same')
    unknown = _warnings_of(w, 'step1', 'collation_version_unknown')
    if same is not None:
        want = {'differ': ([], []), 'same': ([{'version': v.get('version')}],
                                             []),
                'unknown': ([], [{'sides': v.get('sides')}])}[v['state']]
        if (same, unknown) != want:
            raise ReportError(f'compared.collation_version {v!r}, its `!!` '
                              f'{same!r} {unknown!r}')
    # Step 0's note.
    n = c['note']
    _stated('compared.note', n, NOTE_STATES + ('not run', 'not reported'),
            {'printed': ['builds', 'given'], 'not printed': [],
             'fallback': [], 'not run': ['why'], 'not reported': ['why']})
    if (n['state'] == 'not run') != (run['machines']['state'] == 'not given'):
        raise ReportError(f'compared.note {n!r}, machines '
                          f'{run["machines"]["state"]!r}')
    fell = _warnings_of(w, 'step0', 'machine_falls_back_to_tags')
    if fell is not None and (n['state'] == 'fallback') != bool(fell):
        raise ReportError(f'compared.note {n!r}, the `!!` of machines that '
                          f'read their tags {fell!r}')
    if n['state'] in ('printed', 'not printed') and \
            builds != [pair['old'], pair['new']]:
        raise ReportError(f'compared.note {n!r} on sides {sides!r}')
    _latest(c['latest'], pair, sides, es)
    if n['state'] == 'printed':
        if n['builds'] != builds or not _is_strs(n['given']) or \
                len(n['given']) != 2 or n['given'] == n['builds'] or \
                not all(x in (b_, _build_tag('compared.note', b_)[0])
                        for x, b_ in zip(n['given'], builds)):
            raise ReportError(f'compared.note is {n!r}')


# What step 11 finds in a changed locale, in the order it finds it
# (locale_order.LOCALE_KINDS), each written as data; the levels a pair is
# told apart at (locale_order.LEVEL_NAMES); the machine a chain of
# neighbours is measured on (locale_order.MEASURED).
FINDING_KINDS = ('moved', 'reversed', 'probes changed', 'left out',
                 'told apart', 'did not move', 'undecided')
LEVELS = ('letters', 'accents', 'case')
SIDES = ('both', 'old', 'new')
# How many pairs told apart differently the report lists one by one
# (locale_order.LIST_LIMIT).
LIST_LIMIT = 20


def relations(compared):
    """The states a relation can be in over the levels COMPARED
    (locale_order.relation_state): not at all; at the first compared level
    that tells the two apart, reached from just after the compared level
    before it; less than the weakest compared level; told apart where
    none is compared."""
    ks = [LEVELS.index(x) for x in compared]
    out, before = {'not at all'}, -1
    for k in ks:
        out.add(' or '.join(LEVELS[before + 1:k + 1]))
        before = k
    out.add(f'less than {LEVELS[ks[-1]]}' if ks else 'told apart')
    return out


def _levels(where, x):
    """A list of level names, in LEVELS's order, each once."""
    if not (isinstance(x, list) and all(v in LEVELS for v in x)
            and x == sorted(set(x), key=LEVELS.index)):
        raise ReportError(f'{where} is not a list of levels: {x!r}')
    return x


def _probes(where, side):
    """One machine's probe pairs (locale_order.probe_data)."""
    if not (isinstance(side, list) and len(side) == 3):
        raise ReportError(f'{where} is not three levels: {side!r}')
    for i, e in enumerate(side):
        w = f'{where}[{i}]'
        if not isinstance(e, dict):
            raise ReportError(f'{w} is {e!r}')
        _one_of(f'{w}.state', e.get('state'), ('pair', 'none', 'not measured'))
        if e['state'] == 'pair':
            _keys(w, e, ['level', 'state', 'pair', 'from_the_end'])
            if not (isinstance(e['pair'], list) and len(e['pair']) == 2):
                raise ReportError(f'{w}.pair is {e["pair"]!r}')
            for t in e['pair']:
                _string(f'{w}.pair', t)
            if not _is_bool(e['from_the_end']):
                raise ReportError(f'{w}.from_the_end is {e["from_the_end"]!r}')
        else:
            _keys(w, e, ['level', 'state'])
        if e['level'] != LEVELS[i]:
            raise ReportError(f'{w}.level is {e["level"]!r}')


def _expand(where, runs):
    """The strings of RUNS, in their order, as _string gives them."""
    out = []
    for i, run in enumerate(runs):
        n = _run_size(f'{where}[{i}]', run)
        a = _string(where, run[0])
        out += [a[:-1] + (a[-1] + j,) for j in range(n)]
    return out


def _spans(where, runs):
    """The strings of RUNS as spans (kind, first, last) of _string's
    numbers, in order, each span as long as it can be: two sets of strings
    are equal exactly when their spans are."""
    got = []
    for i, run in enumerate(runs):
        n = _run_size(f'{where}[{i}]', run)
        a = _string(where, run[0])
        got.append((a[:-1], a[-1], a[-1] + n - 1))
    out = []
    for kind, lo, hi in sorted(got):
        if out and out[-1][0] == kind and lo <= out[-1][2] + 1:
            out[-1] = (kind, out[-1][1], max(out[-1][2], hi))
        else:
            out.append((kind, lo, hi))
    return out


def _minus(a, b):
    """The spans of A's strings that are not B's (both from _spans)."""
    out, j = [], 0
    for kind, lo, hi in a:
        while j < len(b) and (b[j][0], b[j][2]) < (kind, lo):
            j += 1
        k, at = j, lo
        while k < len(b) and b[k][0] == kind and b[k][1] <= hi:
            if b[k][1] > at:
                out.append((kind, at, b[k][1] - 1))
            at = max(at, b[k][2] + 1)
            k += 1
        if at <= hi:
            out.append((kind, at, hi))
    return out


def _told_apart(where, x):
    _keys(where, x, ['kind', 'count', 'compared', 'probes', 'listed',
                     'pairs', 'chains'])
    compared = _levels(f'{where}.compared', x['compared'])
    _probes(f'{where}.probes', x['probes'])
    paired = [e['level'] for e in x['probes'] if e['state'] == 'pair']
    if not set(compared) <= set(paired):
        raise ReportError(f'{where} compares {compared!r}, a level with no '
                          'probe pair')
    allowed = relations(compared)
    if not _is_positive(x['count']):
        raise ReportError(f'{where}.count is {x["count"]!r}')
    _one_of(f'{where}.listed', x['listed'], ('each', 'too many'))
    if (x['listed'] == 'each') != (x['count'] <= LIST_LIMIT):
        raise ReportError(f'{where} is listed {x["listed"]!r} with '
                          f'{x["count"]} pairs')
    if not (isinstance(x['chains'], list) and x['chains']):
        raise ReportError(f'{where}.chains is {x["chains"]!r}')
    held, chained = 0, []
    for i, c in enumerate(x['chains']):
        w = f'{where}.chains[{i}]'
        _keys(w, c, ['runs', 'before', 'now', 'measured', 'test'])
        if not (isinstance(c['runs'], list) and c['runs']):
            raise ReportError(f'{w}.runs is {c["runs"]!r}')
        # Counted by its runs, every one checked; named string by string
        # only where the report lists the pairs (at most LIST_LIMIT).
        size = sum(_run_size(f'{w}.runs[{i}]', run)
                   for i, run in enumerate(c['runs']))
        if size < 2:
            raise ReportError(f'{w} is not a chain: {c["runs"]!r}')
        held += size - 1
        _one_of(f'{w}.before', c['before'], sorted(allowed))
        _one_of(f'{w}.now', c['now'], sorted(allowed))
        if c['before'] == c['now']:
            raise ReportError(f'{w} is told apart alike before and now')
        _one_of(f'{w}.measured', c['measured'], SIDES)
        t = c['test']
        if not isinstance(t, dict):
            raise ReportError(f'{w}.test is {t!r}')
        _one_of(f'{w}.test.state', t.get('state'), ('pair', 'none'))
        if t['state'] == 'pair':
            _keys(f'{w}.test', t, ['state', 'level', 'before', 'now'])
            _one_of(f'{w}.test.level', t['level'], compared)
            _one_of(f'{w}.test.before', t['before'], ('<', '>'))
            _one_of(f'{w}.test.now', t['now'], ('<', '>'))
            if t['before'] == t['now']:
                raise ReportError(f'{w}.test is answered alike before and '
                                  'now')
        else:
            _keys(f'{w}.test', t, ['state'])
        if x['listed'] == 'each' and held <= LIST_LIMIT:
            strings = _expand(f'{w}.runs', c['runs'])
            chained += [[a, b, c['before'], c['now']]
                        for a, b in zip(strings, strings[1:])]
    if held != x['count']:
        raise ReportError(f'{where} counts {x["count"]} pairs and its chains '
                          f'hold {held}')
    if not isinstance(x['pairs'], list):
        raise ReportError(f'{where}.pairs is {x["pairs"]!r}')
    listed = []
    for i, p in enumerate(x['pairs']):
        w = f'{where}.pairs[{i}]'
        _keys(w, p, ['strings', 'before', 'now'])
        if not (isinstance(p['strings'], list) and len(p['strings']) == 2):
            raise ReportError(f'{w}.strings is {p["strings"]!r}')
        listed.append([_string(w, s) for s in p['strings']]
                      + [p['before'], p['now']])
    if listed != (chained if x['listed'] == 'each' else []):
        raise ReportError(f'{where} lists its pairs otherwise than its '
                          'chains')
FOUND_BY = ('step 11', 'its sort table alone')
REACHES = ('imported', 'anywhere', 'superuser')
STRING = re.compile(r'U\+([0-9A-F]{4,6})|bytes ([0-9A-F]{2}(?: [0-9A-F]{2})*)')


def _string(where, tok):
    """A string as step 11's data name it, as (0, code point) or (1,
    length, its bytes as a number)."""
    m = STRING.fullmatch(tok) if isinstance(tok, str) else None
    if not m:
        raise ReportError(f'{where} is not a string step 11 names: {tok!r}')
    if m[1]:
        return (0, int(m[1], 16))
    b = bytes.fromhex(m[2])
    return (1, len(b), int.from_bytes(b, 'big'))


def _run_size(where, run):
    """How many strings a run [first, last] holds: two strings of one
    kind and one length, the first not after the last."""
    if not (isinstance(run, list) and len(run) == 2):
        raise ReportError(f'{where} is not a run: {run!r}')
    a, b = (_string(where, x) for x in run)
    if a[:-1] != b[:-1] or a[-1] > b[-1]:
        raise ReportError(f'{where} is not a run: {run!r}')
    return b[-1] - a[-1] + 1


def _finding(where, x):
    """One finding of a changed locale (locale_order.change_data)."""
    if not isinstance(x, dict):
        raise ReportError(f'{where} is not an object: {x!r}')
    _one_of(f'{where}.kind', x.get('kind'), FINDING_KINDS)
    kind = x['kind']
    if kind == 'probes changed':
        _keys(where, x, ['kind', 'before', 'now'])
        _probes(f'{where}.before', x['before'])
        _probes(f'{where}.now', x['now'])
        return
    if kind == 'left out':
        _keys(where, x, ['kind', 'compared', 'left_out'])
        a = _levels(f'{where}.compared', x['compared'])
        b = _levels(f'{where}.left_out', x['left_out'])
        if not b or set(a) & set(b):
            raise ReportError(f'{where} leaves out {b!r} and compares {a!r}')
        return
    if kind == 'told apart':
        _told_apart(where, x)
        return
    if kind == 'undecided':
        _keys(where, x, ['kind', 'count', 'pairs'])
        if not (_is_positive(x['count']) and isinstance(x['pairs'], list)
                and len(x['pairs']) == x['count']):
            raise ReportError(f'{where} counts {x["count"]!r} pairs and '
                              'lists otherwise')
        for i, pair in enumerate(x['pairs']):
            if not (isinstance(pair, list) and len(pair) == 2):
                raise ReportError(f'{where}.pairs[{i}] is {pair!r}')
            for t in pair:
                _string(f'{where}.pairs[{i}]', t)
        return
    more = {'moved': ['listed'], 'reversed': ['all'],
            'did not move': ['listed']}[kind]
    _keys(where, x, ['kind', 'count', 'characters', 'other_strings',
                     'runs', *more])
    if not all(_is_count(x[k]) for k in ('count', 'characters',
                                          'other_strings')) or \
            x['count'] != x['characters'] + x['other_strings']:
        raise ReportError(f'{where}: {x["count"]!r} strings are not its '
                          'characters and other strings')
    if not isinstance(x['runs'], list):
        raise ReportError(f'{where}.runs is {x["runs"]!r}')
    held, last = 0, None
    for i, run in enumerate(x['runs']):
        held += _run_size(f'{where}.runs[{i}]', run)
        first = _string(f'{where}.runs[{i}]', run[0])
        if last is not None and first <= last:
            raise ReportError(f'{where}.runs[{i}] is not after the run '
                              'before it')
        last = _string(f'{where}.runs[{i}]', run[1])
    if held != x['count']:
        raise ReportError(f'{where} counts {x["count"]} strings and its '
                          f'runs hold {held}')
    if kind == 'moved':
        _one_of(f'{where}.listed', x['listed'],
                ('none', 'each', 'runs', 'too many'))
        if (x['listed'] == 'none') != (x['count'] == 0):
            raise ReportError(f'{where} is listed {x["listed"]!r} with '
                              f'{x["count"]} strings')
    elif kind == 'did not move':
        _one_of(f'{where}.listed', x['listed'], ('runs', 'too many'))
    if kind != 'moved' and not x['count']:
        raise ReportError(f'{where} holds no string')
    if kind == 'reversed' and not _is_bool(x['all']):
        raise ReportError(f'{where}.all is {x["all"]!r}')


# What a group's sort_table says (locale_order.table_data, table_named):
# what step 11 said of a locale whose table changed, why the table changed,
# and what it names or why it names nothing; how the characters file names
# each kind of sequence a table names (locale_order.NAMED_HOW); and how it
# writes a locale in a label (locale_order.TSV_ESCAPES).
TABLE_STEP11 = ('changed', 'same', 'unmeasured')
TABLE_REASONS = ('no levels on one machine', 'items differ')
TABLE_NAMED = ('named', 'record', 'numbering', 'unread', 'levels')
NAMED_HOW = {'marked': 'marked by the table as changed',
             'old': 'only in the old machine\'s table',
             'new': 'only in the new machine\'s table',
             'flip': 'compares differently against one marked as changed'}
TSV_ESCAPES = {9: '\\t', 10: '\\n', 13: '\\r', 44: '\\x2c', 92: '\\\\'}
# A name a sort table gives (locale_order.table_naming): a code point, a
# run of them, a string of several, or bytes, one string or a range of
# strings of one length.
TABLE_ITEM = re.compile(r'(U\+[0-9A-F]{4,6})(?:\.\.(U\+[0-9A-F]{4,6})'
                        r'|(?: U\+[0-9A-F]{4,6})+)?'
                        r'|(bytes [0-9A-F]{2}(?: [0-9A-F]{2})*)'
                        r'(?:\.\.([0-9A-F]{2}(?: [0-9A-F]{2})*))?')


def _sort_table(where, t, found_by):
    """A group's sort_table (locale_order.table_data): 'no line' where
    its table printed none, which only a group step 11 found can be; else
    'changed', what step 11 said (found by the table alone, FOUND_BY,
    exactly where step 11 did not say 'changed'), why, and, where the
    items differ, what the table names: counts of which some sequence is
    marked or on one side only, and a flip only beside one marked
    (locale_order.table_sequences)."""
    if not isinstance(t, dict):
        raise ReportError(f'{where} is {t!r}')
    _one_of(f'{where}.state', t.get('state'), ('no line', 'changed'))
    if t['state'] == 'no line':
        _keys(where, t, ['state'])
        if found_by != 'step 11':
            raise ReportError(f'{where}: no line of a table that alone found '
                              'the change')
        return
    _one_of(f'{where}.step11', t.get('step11'), TABLE_STEP11)
    _one_of(f'{where}.reason', t.get('reason'), TABLE_REASONS)
    if (found_by == 'step 11') != (t['step11'] == 'changed'):
        raise ReportError(f'{where}: step 11 said {t["step11"]!r} of a change '
                          f'found by {found_by}')
    if t['reason'] == 'no levels on one machine':
        _keys(where, t, ['state', 'step11', 'reason'])
        return
    _keys(where, t, ['state', 'step11', 'reason', 'naming'])
    nm, w = t['naming'], f'{where}.naming'
    if not isinstance(nm, dict):
        raise ReportError(f'{w} is {nm!r}')
    _one_of(f'{w}.state', nm.get('state'), TABLE_NAMED)
    if nm['state'] in ('record', 'numbering'):
        _keys(w, nm, ['state'])
    elif nm['state'] == 'unread':
        _keys(w, nm, ['state', 'reasons'])
        if not _is_strs(nm['reasons']):
            raise ReportError(f'{w}.reasons is {nm["reasons"]!r}')
    elif nm['state'] == 'levels':
        _keys(w, nm, ['state', 'old', 'new'])
        if not (_is_positive(nm['old']) and _is_positive(nm['new'])
                and nm['old'] != nm['new']):
            raise ReportError(f'{w} has the levels {nm["old"]!r} and '
                              f'{nm["new"]!r}')
    else:
        _keys(w, nm, ['state', 'counts', 'more', 'others'])
        _keys(f'{w}.counts', nm['counts'], list(NAMED_HOW))
        c = nm['counts']
        if not (all(_is_count(v) for v in c.values())
                and _is_count(nm['more']) and _is_bool(nm['others'])):
            raise ReportError(f'{w} is {nm!r}')
        if not (c['marked'] or c['old'] or c['new']):
            raise ReportError(f'{w} names no sequence marked or on one side '
                              'only')
        if c['flip'] and not c['marked']:
            raise ReportError(f'{w} names {c["flip"]} flip(s) beside none '
                              'marked')


def _label(locales):
    """The characters file's label of a group of LOCALES, as
    locale_order.compare writes it: each name as shown() prints it, with
    TSV_ESCAPES, in order, joined by commas."""
    return ','.join(x.encode('utf-8', 'surrogateescape').decode(
        'utf-8', 'replace').translate(TSV_ESCAPES) for x in sorted(locales))


def _table_item(where, tok):
    """((first, last), size) of a name a sort table gives: its first and
    last sequence as table_naming sorts them, code points first (0, points)
    and then bytes (1, length, value), and how many sequences it holds: a
    run of code points or a range of byte strings of one length, by value
    (locale_order.byte_runs), else one."""
    m = TABLE_ITEM.fullmatch(tok) if isinstance(tok, str) else None
    if not m:
        raise ReportError(f'{where}: {tok!r} is not a name of a sequence')
    if m[3]:
        a = bytes.fromhex(m[3][6:])
        b = bytes.fromhex(m[4]) if m[4] else a
        x, y = int.from_bytes(a, 'big'), int.from_bytes(b, 'big')
        if m[4] and (len(a) != len(b) or y <= x):
            raise ReportError(f'{where}: {tok!r} is not a range of strings '
                              'of one length')
        return ((1, len(a), x), (1, len(b), y)), y - x + 1
    if m[2]:
        return ((0, (int(m[1][2:], 16),)), (0, (int(m[2][2:], 16),))), \
            _run_size(where, [m[1], m[2]])
    cps = tuple(int(x, 16) for x in re.findall(r'U\+([0-9A-F]+)', tok))
    return ((0, cps), (0, cps)), 1


def _table_rows_held(where, groups, rows):
    """Each group's sort_table against the characters file's ROWS: one
    run of 'sort table changed' rows per group whose table changed, under
    that group's label (_label), in the groups' order, and no other; its
    first row the line ('-'), and, where the table names sequences, one
    row per name after it: the kinds in NAMED_HOW's order, each kind's
    names in table_naming's order (one may repeat the code point of the one
    before: two byte strings of one character, as ISIRI-3342, GB18030,
    ARMSCII-8 and EUC-TW have at glibc-2.39), their sequences adding up to
    each count, one named by its bytes exactly where the data says so, and
    as many as the report lists and leaves out (more); none where the table
    names nothing."""
    # A run is the rows of one label one after the other: a label met
    # again after other rows is a second run of it.
    runs, prev = [], None
    for r in rows:
        if r[1] != 'sort table changed':
            prev = None
            continue
        if r[0] != prev:
            runs.append((r[0], []))
        prev = r[0]
        runs[-1][1].append(r)
    changed = [g for g in groups if g['sort_table']['state'] == 'changed']
    got = [lb for lb, _ in runs]
    want = [_label(g['locales']) for g in changed]
    if got != want:
        raise ReportError(f'{where}: the sort table rows are of {got[:5]}, '
                          f'the changed sort tables of {want[:5]}')
    how = {v: k for k, v in NAMED_HOW.items()}
    for g, (label, rs) in zip(changed, runs):
        nm = g['sort_table'].get('naming', {})
        w = f'{where}: the sort table rows of {label!r}'
        head, named = rs[0], rs[1:]
        if head[2] != '-' or head[3] == '-' or head[4:] != ['-'] * 4:
            raise ReportError(f'{w} do not open with its line: {head!r}')
        if nm.get('state') != 'named':
            if named:
                raise ReportError(f'{w} name {len(named)} sequence(s), its '
                                  'table none')
            continue
        counts = dict.fromkeys(NAMED_HOW, 0)
        kinds, in_bytes, last = [], False, None
        for r in named:
            if r[3] not in how or r[4:] != ['-', '-', '-', 'compiled table']:
                raise ReportError(f'{w}: {r!r}')
            kind = how[r[3]]
            if not kinds or kinds[-1] != kind:
                if kind in kinds:
                    raise ReportError(f'{w}: the {kind} names are not in one '
                                      'run')
                kinds.append(kind)
                last = None
            (first, end), size = _table_item(w, r[2])
            if last is not None and first < last:
                raise ReportError(f'{w}: {r[2]!r} is not after the name '
                                  'before it')
            last = end
            counts[kind] += size
            in_bytes = in_bytes or r[2].startswith('bytes ')
        if kinds != [x for x in NAMED_HOW if x in kinds]:
            raise ReportError(f'{w}: the kinds are in the order {kinds!r}')
        if counts != nm['counts']:
            raise ReportError(f'{w} hold {counts!r}, the data counts '
                              f'{nm["counts"]!r}')
        if nm['more'] != max(len(named) - LIST_LIMIT, 0):
            raise ReportError(f'{w}: {len(named)} name(s), and the data '
                              f'leaves {nm["more"]} out of the report')
        if in_bytes != nm['others']:
            raise ReportError(f'{w}: a name in bytes is {in_bytes}, the data '
                              f'says {nm["others"]}')


def _tables_held(where, groups, warned):
    """Each group's sort_table against the tables' `!!` of the same run
    (WARNED, step 11's warnings; locale_order.table_verdicts): a locale of
    a group whose table printed no line is in tables_disagree or
    tables_not_compared; one whose changed table names nothing by its
    record or numbering, step 11 having found the change, is in
    tables_disagree and not in tables_not_compared; no other changed
    locale is in either. Where a list is cut (more), a name it does not
    print is not looked for in that list; the other is read whole."""
    if warned['state'] != 'reported':
        return
    dis, nc, dis_cut, nc_cut = set(), set(), False, False
    for it in warned['items']:
        if it['kind'] == 'tables_disagree':
            dis.update(it['data']['names'])
            dis_cut = dis_cut or it['data']['more'] > 0
        elif it['kind'] == 'tables_not_compared':
            for x in it['data']['groups']:
                nc.update(x['names'])
                nc_cut = nc_cut or x['more'] > 0
    for i, g in enumerate(groups):
        t, w = g['sort_table'], f'{where}.groups[{i}].sort_table'
        nm = t.get('naming', {}).get('state')
        for name in sorted(g['locales']):
            if t['state'] == 'no line':
                ok = name in dis or name in nc or dis_cut or nc_cut
            elif t['step11'] == 'changed' and nm in ('record', 'numbering'):
                ok = (name in dis or dis_cut) and name not in nc
            else:
                ok = name not in dis and name not in nc
            if not ok:
                raise ReportError(f'{w}: {name!r} is said otherwise by the '
                                  "tables' warnings")


def _findings(where, f, verdicts):
    """step 11's findings (locale_order.change_groups, and compare()'s
    tables_only): each changed locale
    of VERDICTS in one group and none other, the groups sorted by their
    names, the headings they print under numbered from 0 with none
    skipped, each group's findings in the order step 11 finds them, and
    each group's sort table (_sort_table); and the locales the tables
    alone found unchanged (tables_only), each once, which load() holds to
    the verdicts and the control of the same run (_tables_only_held)."""
    _keys(where, f, ['state', 'groups', 'tables_only'])
    if not (_is_strs(f['tables_only']) or f['tables_only'] == []) or \
            len(set(f['tables_only'])) != len(f['tables_only']):
        raise ReportError(f'{where}.tables_only is {f["tables_only"]!r}')
    if not isinstance(f['groups'], list):
        raise ReportError(f'{where}.groups is {f["groups"]!r}')
    seen, printed, before = set(), set(), None
    for i, g in enumerate(f['groups']):
        w = f'{where}.groups[{i}]'
        _keys(w, g, ['locales', 'printed_under', 'found_by', 'findings',
                     'names_bytes', 'sort_table'])
        if not (isinstance(g['locales'], dict) and g['locales']):
            raise ReportError(f'{w}.locales is {g["locales"]!r}')
        names = sorted(g['locales'])
        if before is not None and names <= before:
            raise ReportError(f'{w} is not after the group before it')
        before = names
        for name, e in g['locales'].items():
            lw = f'{w}.locales[{name!r}]'
            if name in seen:
                raise ReportError(f'{lw} is in two groups')
            seen.add(name)
            _keys(lw, e, ['codeset', 'postgres'])
            _keys(f'{lw}.codeset', e['codeset'], ['old', 'new'])
            _keys(f'{lw}.postgres', e['postgres'],
                  ['alias', 'spelling', 'reach'])
            if not all(_is_str(v) for v in (*e['codeset'].values(),
                                            e['postgres']['alias'],
                                            e['postgres']['spelling'])):
                raise ReportError(f'{lw} is {e!r}')
            _one_of(f'{lw}.postgres.reach', e['postgres']['reach'], REACHES)
        if not _is_count(g['printed_under']):
            raise ReportError(f'{w}.printed_under is {g["printed_under"]!r}')
        printed.add(g['printed_under'])
        _one_of(f'{w}.found_by', g['found_by'], FOUND_BY)
        if not _is_bool(g['names_bytes']):
            raise ReportError(f'{w}.names_bytes is {g["names_bytes"]!r}')
        found = g['findings']
        if not isinstance(found, list):
            raise ReportError(f'{w}.findings is {found!r}')
        if g['found_by'] == 'its sort table alone':
            if found or g['names_bytes']:
                raise ReportError(f'{w}: step 11 found nothing in a change '
                                  'its sort table alone found')
        _sort_table(f'{w}.sort_table', g['sort_table'], g['found_by'])
        if g['found_by'] == 'its sort table alone':
            continue
        for k, x in enumerate(found):
            _finding(f'{w}.findings[{k}]', x)
        kinds = [x['kind'] for x in found]
        if kinds != sorted(set(kinds), key=FINDING_KINDS.index) or \
                kinds[:1] != ['moved']:
            raise ReportError(f'{w}.findings are {kinds!r}, not in the '
                              'order step 11 finds them')
        moved = found[0]['count']
        if ('reversed' in kinds) != bool(moved) or \
                ('did not move' in kinds and not (
                    moved and 'told apart' in kinds)):
            raise ReportError(f'{w}.findings are {kinds!r} with '
                              f'{moved} strings moved')
        fk = {x['kind']: x for x in found}
        _levels_held(w, fk)
        if any(x.get('other_strings') for x in found) or any(
                t.startswith('bytes ')
                for t in _tokens(_named_strings(found))):
            if not g['names_bytes']:
                raise ReportError(f'{w} counts a string by its bytes and '
                                  'says it names none')
        if 'told apart' in fk and moved:
            chain = _spans(w, [r for c in fk['told apart']['chains']
                               for r in c['runs']])
            dn = fk.get('did not move')
            if _minus(chain, _spans(w, found[0]['runs'])) != \
                    _spans(w, dn['runs'] if dn else []):
                raise ReportError(f'{w}: what did not move is not the strings '
                                  'of the chains that did not move')
    changed = {n for n, v in verdicts.items() if v['verdict'] == 'changed'}
    if seen != changed:
        raise ReportError(f'{where} groups {sorted(seen ^ changed)[:5]} '
                          'otherwise than the verdicts')
    if printed != set(range(len(printed))):
        raise ReportError(f'{where}: the headings are {sorted(printed)}')


def _levels_held(w, fk):
    """The levels a group's findings compare and leave out, against its
    probe pairs: told apart compares the levels its pairs (the old
    machine's) measure but those left out, and takes those pairs from the
    probes changed's 'before'; where the pairs changed and a level is
    left out, a level whose pairs differ or that one machine did not
    measure is among those left out, and a level compared has a pair (the
    pairs changed without a level left out only where nothing told apart
    was looked at: locale_findings). Only what bytes decide is asked: two
    byte strings that one charmap names alike leave a level whose names
    read alike left out, which nothing here refuses."""
    pc, lo, ta = (fk.get(k) for k in ('probes changed', 'left out',
                                      'told apart'))
    out = lo['left_out'] if lo else []
    if ta:
        want = [e['level'] for e in ta['probes']
                if e['state'] == 'pair' and e['level'] not in out]
        if ta['compared'] != want:
            raise ReportError(f'{w}: told apart compares {ta["compared"]!r}, '
                              f'its probe pairs {want!r}')
        if lo and lo['compared'] != ta['compared']:
            raise ReportError(f'{w}: the levels compared are not those '
                              'told apart says')
        if pc and ta['probes'] != pc['before']:
            raise ReportError(f'{w}: told apart takes other probe pairs '
                              'than the old machine measured')
        if pc and not lo:
            raise ReportError(f'{w}: the probe pairs changed and no level '
                              'is left out')
    if pc and lo:
        for a, b in zip(pc['before'], pc['now']):
            k = a['level']
            if (a != b or 'not measured' in (a['state'], b['state'])) and \
                    k not in out:
                raise ReportError(f'{w}: {k} is compared with pairs that '
                                  'changed')
            # A level compared whose pairs differ is refused above.
            if k in lo['compared'] and a['state'] != 'pair':
                raise ReportError(f'{w}: {k} is compared with no pair')


def _named_strings(found):
    """FOUND as the outputs name its strings: told apart's probe pairs
    only at the levels a chain is tested at, the only ones told apart's
    outputs name (locale_order.names_bytes). A pair of another level, such
    as th_TH's accents pair in TIS-620 on RHEL 8 and 9 (FF 5F), is named
    by told apart nowhere; probes changed, kept whole, names it where the
    pairs changed."""
    out = []
    for x in found:
        if x['kind'] == 'told apart':
            tested = {c['test'].get('level') for c in x['chains']}
            x = dict(x, probes=[e for e in x['probes']
                                if e['level'] in tested])
        out.append(x)
    return out


def _tokens(x):
    """Every string value under X."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, list):
        for v in x:
            yield from _tokens(v)
    elif isinstance(x, dict):
        for v in x.values():
            yield from _tokens(v)


def load(path):
    """The report at PATH, checked whole: every key, and every state from
    its list. Raises ReportError on anything else."""
    with open(path, encoding='utf-8') as f:
        r = json.load(f)
    _no_null('report', r)
    _keys('report', r, ['format', 'pair', 'run', 'compared', 'step2',
                        'step11', 'warnings'])
    if r['format'] != FORMAT:
        raise ReportError(f'format {r["format"]!r}, not {FORMAT!r}')
    _keys('pair', r['pair'], ['old', 'new', 'slug'])
    run = r['run']
    _keys('run', run, ['route', 'order', 'nothing_compared', 'old_class',
                       'new_class', 'word', 'machines'])
    for k, allowed in (('route', ROUTES), ('order', ORDERS),
                       ('nothing_compared', NOTHING), ('old_class', CLASSES),
                       ('new_class', CLASSES), ('word', WORDS)):
        _one_of(f'run.{k}', run[k], allowed)
    _stated('run.machines', run['machines'], ('given', 'not given'),
            {'given': ['old', 'new'], 'not given': ['why']})
    w = r['warnings']
    _keys('warnings', w, ['step0', 'step1', 'step2', 'step11'])

    def items(where, o):
        _keys(where, o, ['state', 'items'])
        if not isinstance(o['items'], list):
            raise ReportError(f'{where}.items is not a list')
        for i, it in enumerate(o['items']):
            _keys(f'{where}.items[{i}]', it, WARNING_KEYS)
            if warning_item(f'{where}.items[{i}]', it['kind'], it['data'],
                            it['text']) != it:
                raise ReportError(f'{where}.items[{i}]: its list or title '
                                  f'is not its kind\'s: {it!r}')
    wr = {'reported': items, 'not reported': ['why']}
    _stated('warnings.step0', w['step0'],
            ('reported', 'not run', 'not reported'),
            {**wr, 'not run': ['why']})
    if (w['step0']['state'] == 'not run') != (run['machines']['state']
                                               == 'not given'):
        raise ReportError('warnings.step0 is not run exactly when no '
                          'machine files were given')
    _stated('warnings.step1', w['step1'], ('reported', 'not reported'), wr)
    _stated('warnings.step2', w['step2'], ('reported', 'not reported'), wr)
    _stated('warnings.step11', w['step11'],
            ('reported', 'not run', 'not reported'),
            {**wr, 'not run': ['why']})
    _compared(r)

    s2 = r['step2']
    _keys('step2', s2, ['listed', 'removed', 'rules_by_name',
                        'rules_characters', 'rules', 'character_sets',
                        'transliteration'])
    nr = {'not reported': ['why']}

    def listed(where, o):
        _keys(where, o, ['state', 'compared', 'not_compared'])
        _strs(f'{where}.compared', o['compared'])
        _strs(f'{where}.not_compared', o['not_compared'])
    _stated('step2.listed', s2['listed'],
            ('reported', 'not compared', 'not reported'),
            {'reported': listed, 'not compared': ['why'], **nr})
    # Nothing compared is never a list read as nothing found: step 2's
    # own record of it decides, the order check's where step 2 wrote none.
    rb = s2['rules_by_name']
    some = (rb['nothing']['state'] == 'something compared'
            if rb['state'] == 'reported' else
            run['nothing_compared'] == 'none')
    for part in ('listed', 'rules_characters'):
        if s2[part]['state'] == 'reported' and not some:
            raise ReportError(f'step2.{part} is reported, yet step 2 '
                              'compared nothing')
        if s2[part]['state'] == 'not compared' and some:
            raise ReportError(f'step2.{part} is not compared, yet step 2 '
                              'compared something')

    def removed(where, o):
        _keys(where, o, ['state', 'between', 'source', 'distro_files',
                         'rows'])
        _one_of(f'{where}.between', o['between'], WORDS)
        _one_of(f'{where}.source', o['source'], REMOVED_SOURCES)
        if o['distro_files'] != REMOVED_SOURCES[o['source']]:
            raise ReportError(f'{where}.distro_files is {o["distro_files"]!r}'
                              f' for the source {o["source"]!r}')
        for i, row in enumerate(o['rows']):
            _keys(f'{where}.rows[{i}]', row, ['name', 'how', 'to'])
            _one_of(f'{where}.rows[{i}].how', row['how'],
                    ('deleted', 'renamed'))
            if (row['how'] == 'renamed') == (row['to'] == '-'):
                raise ReportError(f'{where}.rows[{i}]: {row!r}')
    _stated('step2.removed', s2['removed'], ('reported', 'not reported'),
            {'reported': removed, **nr})

    def by_name(where, o):
        _keys(where, o, ['state', 'compared', 'names', 'machines', 'nothing',
                         'measurements', 'charmaps', 'locales'])
        _one_of(f'{where}.compared', o['compared'], WORDS)
        _strs(f'{where}.names', o['names'])
        if not isinstance(o['machines'], bool):
            raise ReportError(f'{where}.machines is {o["machines"]!r}')
        _stated(f'{where}.nothing', o['nothing'],
                ('something compared', 'commit', 'tree'),
                {'something compared': [], 'commit': ['what'],
                 'tree': ['what']})
        if not isinstance(o['measurements'], list):
            _stated(f'{where}.measurements', o['measurements'],
                    ('not applicable',), {'not applicable': ['why']})
        _stated(f'{where}.charmaps', o['charmaps'], CHARMAPS_STATES,
                {s: [] if s in NO_WHY else ['why']
                 for s in CHARMAPS_STATES})

        def locs(w, lo):
            _keys(w, lo, ['state', 'by_name'])
            for name, x in lo['by_name'].items():
                _keys(f'{w}.by_name[{name!r}]', x,
                      ['key', 'rules', 'why', 'charmap', 'source'])
        _stated(f'{where}.locales', o['locales'],
                ('reported', 'not applicable'),
                {'reported': locs, 'not applicable': ['why']})
    _stated('step2.rules_by_name', s2['rules_by_name'],
            ('reported', 'not reported'), {'reported': by_name, **nr})

    def rchars(where, o):
        _keys(where, o, ['state', 'columns', 'rows'])
        if o['columns'] != RULES_CHARACTER_COLUMNS:
            raise ReportError(f'{where}.columns is {o["columns"]!r}')
        _rows(f'{where}.rows', o['rows'], len(RULES_CHARACTER_COLUMNS))
    _stated('step2.rules_characters', s2['rules_characters'],
            ('reported', 'not compared', 'not reported'),
            {'reported': rchars, 'not compared': ['why'], **nr})
    if rb['state'] == 'reported':
        # Step 2's own rule (write_rules_by_name, rules_beside): one commit,
        # one tree or not read only where it compared nothing.
        if (rb['charmaps']['state'] in ('one commit', 'one tree',
                                        'not read')) == some:
            raise ReportError(f"step2.rules_by_name.charmaps is "
                              f"{rb['charmaps']['state']!r} where step 2 "
                              f"compared {'something' if some else 'nothing'}")
        if (rb['locales']['state'] == 'reported') != \
                (rb['machines'] and some):
            raise ReportError('step2.rules_by_name.locales is '
                              f"{rb['locales']['state']!r} with machines="
                              f"{rb['machines']} and something compared="
                              f'{some}')
        if isinstance(rb['measurements'], list) != rb['machines']:
            raise ReportError('step2.rules_by_name.measurements does not go '
                              'with machines')
    _rules('step2.rules', s2, some, run, r['pair'], w, r['step11'])
    _character_sets('step2.character_sets', s2, some, w)
    _transliteration('step2.transliteration', s2, w)
    _charmap_by_name('step2.rules_by_name.locales', s2, w)

    def s11_not_run(where, o):
        _keys(where, o, ['state', 'why', 'not_done'])
        if o['not_done'] != list(STEP11_NOT_DONE):
            raise ReportError(f'{where}.not_done is {o["not_done"]!r}')

    def s11(where, o):
        _keys(where, o, ['state', 'measurements', 'verdicts', 'characters',
                         'sql', 'rules_beside', 'counts', 'findings',
                         'checks'])
        _strs(f'{where}.measurements', o['measurements'])
        for name, x in o['verdicts'].items():
            _keys(f'{where}.verdicts[{name!r}]', x, ['verdict', 'words'])
            _one_of(f'{where}.verdicts[{name!r}].verdict', x['verdict'],
                    VERDICTS)

        def chars(w, c):
            _keys(w, c, ['state', 'columns', 'rows'])
            if c['columns'] != CHARACTER_COLUMNS:
                raise ReportError(f'{w}.columns is {c["columns"]!r}')
            _rows(f'{w}.rows', c['rows'], len(CHARACTER_COLUMNS))
        _stated(f'{where}.characters', o['characters'],
                ('reported', 'not reported'), {'reported': chars, **nr})
        _stated(f'{where}.sql', o['sql'], ('reported', 'not reported'),
                {'reported': _sql, **nr})
        _stated(f'{where}.rules_beside', o['rules_beside'],
                ('set beside', 'not set beside', 'not reported'),
                {'set beside': ['file'], 'not set beside': ['why', 'file'],
                 **nr})
        # Two of the things locale_order.rules_beside refuses, held here
        # too: the rules of every locale step 11 names, of the same two
        # measurements.
        if o['rules_beside']['state'] == 'set beside':
            if rb['state'] != 'reported' or \
                    rb['locales']['state'] != 'reported':
                raise ReportError(f'{where}.rules_beside is set beside with '
                                  'no rules by name from step 2')
            gone = sorted(set(o['verdicts'])
                          - set(rb['locales']['by_name']))
            if gone:
                raise ReportError(f'step 2 gives no rules for {gone[:5]}, '
                                  'which step 11 names')
            if rb['measurements'] != o['measurements']:
                raise ReportError('step 2 and step 11 are of different '
                                  'measurements')
        def counts(w, c):
            # Every count, each machine's, and that they add up as the
            # report's line of totals says and as the verdicts above
            # count them: a count that does not add up, or that the
            # verdicts of the same run count otherwise, is refused here.
            _keys(w, c, ['state', 'machines', *COUNT_KEYS])
            _keys(f'{w}.machines', c['machines'], ['old', 'new'])
            for side, m in c['machines'].items():
                _keys(f'{w}.machines.{side}', m, ['label', 'locales'])
                if not (_is_str(m['label']) and _is_count(m['locales'])):
                    raise ReportError(f'{w}.machines.{side} is {m!r}')
            for k in COUNT_KEYS:
                if not _is_count(c[k]):
                    raise ReportError(f'{w}.{k} is {c[k]!r}')
            old = c['machines']['old']['locales']
            new = c['machines']['new']['locales']
            if old != (c['changed'] + c['unchanged'] + c['not_measured']
                       + c['refused'] + c['only_old']):
                raise ReportError(f"{w}: the old machine's {old} locale(s) "
                                  'are not the sum of its counts')
            if new != old - c['only_old'] + c['only_new']:
                raise ReportError(f"{w}: the new machine's {new} locale(s) "
                                  'are not the old ones less those only on '
                                  'the old and more those only on the new')
            tally = {v: 0 for v in VERDICTS}
            for x in o['verdicts'].values():
                tally[x['verdict']] += 1
            told = {'changed': tally['changed'],
                    'unchanged': tally['firm'] + tally['far'],
                    'unchanged_as_far_as_measured': tally['far'],
                    'not_measured': tally['unmeasured'],
                    'refused': tally['refused'],
                    'only_old': tally['only old'],
                    'only_new': tally['only new']}
            wrong = {k: (c[k], v) for k, v in told.items() if c[k] != v}
            if wrong:
                raise ReportError(f'{w} and {where}.verdicts count '
                                  f'otherwise: {wrong}')
        _stated(f'{where}.counts', o['counts'], ('reported', 'not reported'),
                {'reported': counts, **nr})
        _stated(f'{where}.findings', o['findings'],
                ('reported', 'not reported'),
                {'reported': lambda w, f: _findings(w, f, o['verdicts']),
                 **nr})
        if o['findings']['state'] == 'reported' and \
                o['characters']['state'] == 'reported':
            _table_rows_held(f'{where}.characters', o['findings']['groups'],
                             o['characters']['rows'])
        if o['findings']['state'] == 'reported':
            _tables_held(f'{where}.findings', o['findings']['groups'],
                         r['warnings']['step11'])
        _stated(f'{where}.checks', o['checks'],
                ('reported', 'not reported'),
                {'reported': lambda w, c: _checks(
                    w, c, o['counts'], r['warnings']['step11']),
                 **nr})
        basis = _verdicts_held(f'{where}.verdicts', o['verdicts'],
                               r['warnings']['step11'])
        _closing_held(where, basis, r['warnings']['step11'])
        if o['findings']['state'] == 'reported' and \
                o['checks']['state'] == 'reported':
            _tables_only_held(f'{where}.findings.tables_only',
                              o['findings']['tables_only'], basis,
                              o['checks']['control'])
        if o['findings']['state'] == 'reported' and \
                o['sql']['state'] == 'reported':
            _sql_groups_held(f'{where}.sql.not_checked',
                             o['sql']['not_checked'], o['findings']['groups'])
        if o['findings']['state'] == 'reported' and \
                o['checks']['state'] == 'reported':
            _charmaps_held(f'{where}.checks.charmaps',
                           o['checks']['charmaps'], o['findings']['groups'],
                           basis, r['warnings']['step11'])
    _stated('step11', r['step11'], ('not run', 'reported', 'not reported'),
            {'not run': s11_not_run, 'reported': s11, **nr})
    if ((r['warnings']['step11']['state'] == 'not run')
            != (r['step11']['state'] == 'not run')):
        raise ReportError('warnings.step11 is not run exactly when step 11 '
                          'did not run')
    return r


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--pair', required=True)
    ap.add_argument('--old', required=True)
    ap.add_argument('--new', required=True)
    ap.add_argument('--route', required=True)
    ap.add_argument('--order-words', required=True)
    ap.add_argument('--old-node', default='')
    ap.add_argument('--new-node', default='')
    ap.add_argument('--tag-side', default='')
    ap.add_argument('--old-asked', default='')
    ap.add_argument('--new-asked', default='')
    ap.add_argument('--step11', action='store_true')
    a = ap.parse_args(argv)
    path = _file(a.out_dir, f'report.{a.pair}.json')
    partial = path + '.partial'
    try:
        report = build(a)
        with open(partial, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
            f.write('\n')
        load(partial)
        os.replace(partial, path)
    except (OSError, ValueError, ReportError) as e:
        try:
            os.remove(partial)
        except OSError:
            pass
        print(f'error: cannot write {path}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
