#!/usr/bin/env python3
"""The run's data file, report.<pair>.json, assembled at the end of audit.sh
from the files the steps already wrote, beside the text, which it does not
change.

Every section is always there. What a run did not do is a state with its
reason, never an absent key or a null, so that a reader cannot take "this
was not looked at" for "nothing was found". What the steps hold today
only inside a sentence (step 2's totals and character sets, step 11's
counts and findings, the provenance of each side) is 'not yet
reported': a later step of the work turns each one into data (branch
24 of the plan). The `!!` notices of steps 0, 1 and 2
are data: a file beside each step's log holds them (step 0's copied
there by audit.sh), and KINDS gives each kind its list and its short
title. Step 11 writes its file too, step11.<pair>.warnings.jsonl, with
the blocks whose printer gives no kind yet under 'step11_pending'; this
file reads it once none is.

load() is the strict reader: a key missing or added, or a state outside
its list, is an error, never a default.

usage: report_data.py --out-dir DIR --pair SLUG --old OLD --new NEW
         --route tags|builds|sides --order-words "ORDER NOTHING OLD NEW WORD"
         [--old-node FILE --new-node FILE] [--tag-side old|new]
         [--step11]
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
# What later steps of branch 24 turn into data.
NOT_YET = {'state': 'not yet reported',
           'why': 'today said only in the text of the run'}


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
        # step 2 writes no key (None) for a locale built into glibc.
        locales = {'state': 'reported', 'by_name': {
            name: dict(zip(('key', 'rules', 'why', 'charmap'),
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
        'totals': NOT_YET,
        'character_sets': NOT_YET,
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
        'sql_files': sql_files,
        'rules_beside': beside,
        'counts': NOT_YET,
        'findings': NOT_YET,
    }


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
# Every kind of `!!` the steps write as data, but step 11's provisional
# 'step11_pending', which this file does not read yet: its list, the exact
# keys of its data with a check for each value, and its short title, made
# from the data alone. A kind not here, or data of another shape, is
# refused.
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
    """Steps 0, 1 and 2 from their files (step 0 runs only for the
    machines' files); step 11 not yet, as some of its blocks have no
    kind yet ('step11_pending')."""
    f = lambda n: _file(a.out_dir, f'step{n}.{a.pair}.warnings.jsonl')
    step0 = (step_warnings(f(0)) if a.old_node else
             {'state': 'not run', 'why': 'no --old-node and --new-node'})
    return {'step0': step0, 'step1': step_warnings(f(1)),
            'step2': step_warnings(f(2)), 'step11': NOT_YET}


# ---------------------------------------------------------------- whole

def build(a):
    run = run_section(a)
    step2 = step2_section(a, run)
    return {
        'format': FORMAT,
        'pair': {'old': a.old, 'new': a.new, 'slug': a.pair},
        'run': run,
        'compared': NOT_YET,
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


def _not_yet(where, obj):
    _keys(where, obj, ['state', 'why'])
    if obj != NOT_YET:
        raise ReportError(f'{where} is {obj!r}')


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
    _not_yet('compared', r['compared'])
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
    _not_yet('warnings.step11', w['step11'])

    s2 = r['step2']
    _keys('step2', s2, ['listed', 'removed', 'rules_by_name',
                        'rules_characters', 'totals', 'character_sets'])
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
                      ['key', 'rules', 'why', 'charmap'])
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
    _not_yet('step2.totals', s2['totals'])
    _not_yet('step2.character_sets', s2['character_sets'])

    def s11_not_run(where, o):
        _keys(where, o, ['state', 'why', 'not_done'])
        if o['not_done'] != list(STEP11_NOT_DONE):
            raise ReportError(f'{where}.not_done is {o["not_done"]!r}')

    def s11(where, o):
        _keys(where, o, ['state', 'measurements', 'verdicts', 'characters',
                         'sql_files', 'rules_beside', 'counts', 'findings'])
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
        for i, f in enumerate(o['sql_files']):
            _keys(f'{where}.sql_files[{i}]', f, ['encoding', 'file'])
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
        _not_yet(f'{where}.counts', o['counts'])
        _not_yet(f'{where}.findings', o['findings'])
    _stated('step11', r['step11'], ('not run', 'reported', 'not reported'),
            {'not run': s11_not_run, 'reported': s11, **nr})
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
