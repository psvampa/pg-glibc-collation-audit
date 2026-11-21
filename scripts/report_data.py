#!/usr/bin/env python3
"""The run's data file, report.<pair>.json, assembled at the end of audit.sh
from the files the steps already wrote, beside the text, which it does not
change.

Every section is always there. What a run did not do is a state with its
reason, never an absent key or a null, so that a reader cannot take "this
was not looked at" for "nothing was found". What the steps hold today
only inside a sentence (step 2's totals and character sets, the
provenance of each side, and each sort table of step 11's findings)
is 'not yet reported': a
later step of the work turns each one into data (branch 24 of the
plan).
The `!!` notices of steps 0, 1, 2 and 11
are data: a file beside each step's log holds them (step 0's copied
there by audit.sh, step 11's step11.<pair>.warnings.jsonl), and KINDS
gives each kind its list and its short title. Step 11's counts are
data too, and so is each group of locales it found changed, with what
moved in it: step11.<pair>.data.json.

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
    dpath = _file(d, f'step11.{a.pair}.data.json')
    counts = (step11_counts(dpath) if os.path.isfile(dpath)
              else not_reported(dpath))
    findings = (step11_findings(dpath) if os.path.isfile(dpath)
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
        'counts': counts,
        'findings': findings,
    }


# The counts step 11 writes (locale_order.step11_counts), beside the state.
COUNT_KEYS = ['changed', 'unchanged', 'unchanged_as_far_as_measured',
              'not_measured', 'refused', 'only_old', 'only_new']


def step11_data(path):
    """The file step 11 wrote to PATH (locale_order.write_counts): its
    counts and its findings."""
    try:
        got = json.loads(_read(path))
    except ValueError as e:
        raise ReportError(f'{path} is not what step 11 writes: {e}')
    if not (isinstance(got, dict)
            and set(got) == {'step', 'counts', 'findings'}
            and got['step'] == 11 and isinstance(got['counts'], dict)
            and 'state' not in got['counts']
            and isinstance(got['findings'], dict)
            and got['findings'].get('state') == 'reported'):
        raise ReportError(f'{path} is not what step 11 writes')
    return got


def step11_counts(path):
    """The counts step 11 wrote to PATH, as a block of the report: load()
    checks every one of them."""
    return {'state': 'reported', **step11_data(path)['counts']}


def step11_findings(path):
    """The findings step 11 wrote to PATH (locale_order.change_groups), as
    a block of the report: load() checks every one of them."""
    return step11_data(path)['findings']


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


def _findings(where, f, verdicts):
    """step 11's findings (locale_order.change_groups): each changed locale
    of VERDICTS in one group and none other, the groups sorted by their
    names, the headings they print under numbered from 0 with none
    skipped, and each group's findings in the order step 11 finds them."""
    _keys(where, f, ['state', 'groups'])
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
        _not_yet(f'{w}.sort_table', g['sort_table'])
        found = g['findings']
        if not isinstance(found, list):
            raise ReportError(f'{w}.findings is {found!r}')
        if g['found_by'] == 'its sort table alone':
            if found or g['names_bytes']:
                raise ReportError(f'{w}: step 11 found nothing in a change '
                                  'its sort table alone found')
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
    _stated('warnings.step11', w['step11'],
            ('reported', 'not run', 'not reported'),
            {**wr, 'not run': ['why']})

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
