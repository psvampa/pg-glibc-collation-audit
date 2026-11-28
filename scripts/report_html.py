#!/usr/bin/env python3
"""The summary HTML report of one run, report.<pair>.html, read from the
run's data file alone (report.<pair>.json, through report_data.load, the
strict reader) and written beside it. The text the run prints is not
changed.

A summary, not a copy of the text: the counts at the top, the locales
added and removed, the locales whose sort order changed (each group step
11 found, with its characters drawn, what step 11 could not settle for it
and whether an SQL file checks its order), what changed in the sort rules
of the locale sources (step 2: its totals and groups in the words it
prints them), the other
changes the run found, what the run did not verify, how it compared and
how to confirm in PostgreSQL, each section open. Each warning of the
text is on the page once, as its card: of class "not checked" under Not
verified, of class "change" in the section WARN_HOME gives its kind, or
under "Other changes the run found". So that a check can hold the page
to the data file
(tests/_harness.report_page_held):
  - every word of the page but a section's <h2>, the <title> and the
    <style> is in exactly one part (data-part, part() below), and the
    check holds the page's parts, their elements and their words to the
    ones the data calls for;
  - a value of the data file shown in the body is marked with its place
    there (data-p="step11/counts/changed": '/' a key, '#' an index), but
    where a part says it in words the check rebuilds whole from the data
    (a group, step 2's parts, some entries of Not verified); a closed
    word of the file shown in the page's own words also carries the word
    (data-v); a name a map is keyed by carries the map (data-k);
  - a value the page works out of the file is marked data-d with its
    rule;
  - each group of values sits under a label (data-l, the label's words
    marked data-t), so the words beside a value can be held too;
  - a sentence the page says whole is marked data-f with its name;
  - a list that holds nothing says so where the list would be ("(none)",
    "(no SQL file was written)", or step 2's "Rules differ: none."),
    marked data-e with what the list is; the locale files gone and new
    say "(none among the files read)" where step 2 left entries of a
    machine's sources unread, and say under the list how many; the
    locales whose sort order changed say "(none among the locales
    measured)" where a locale is not known to be unchanged (step 11
    could not measure it, glibc refused it, or it is unchanged only as far
    as step 11 measured it) and step 11 found no group, and name each such
    locale under the list, groups or none;
  - a block the run did not do or could not know shows its state in
    capitals, with its reason where the data file gives one, never a 0 or
    an empty list. (A locale step 2 found new and could not spell is a
    state of that locale, shown in lower case beside its name.)
  - a group of locales whose sort order changed is a part of kind
    grp#<its place among step 11's groups>; the check holds its text
    whole, rebuilt from that group's data; so are step 2's totals, what
    they count as not compared, each of its groups of locales whose rules
    differ and its entries not read (parts of kind s2:..., with their
    place).
The page's <title> repeats the two sides unmarked.

usage: report_html.py REPORT_JSON
"""

import html
import os
import re
import sys
import unicodedata

import filter_lc_collate_changes as flc
import locale_order
import report_data

esc = html.escape


def mark(path, value, tag='span', cls=''):
    """A value of the data file, marked with its place."""
    c = f' class="{cls}"' if cls else ''
    shown = f'{value:,}' if isinstance(value, int) else str(value)
    return f'<{tag}{c} data-p="{esc(path)}">{esc(shown)}</{tag}>'


def made(rule, value):
    """A value the page works out of the file, marked with its rule."""
    shown = f'{value:,}' if isinstance(value, int) else str(value)
    return f'<span data-d="{esc(rule)}">{esc(shown)}</span>'


def label(key, words, body, tag='div', cls='', head='span', after=False):
    """A group of values under its label: `words` (HTML) marked data-t,
    before the values, or after them (`after`)."""
    c = f' class="{cls}"' if cls else ''
    w = f'<{head} data-t="">{words}</{head}>'
    return (
        f'<{tag}{c} data-l="{esc(key)}">'
        + (body + w if after else w + body)
        + f'</{tag}>'
    )


def fixed(key, words, tag='span', cls=''):
    """A sentence the page says whole (data-f), named so the check can
    require it where the data calls for it and hold it to its own words."""
    c = f' class="{cls}"' if cls else ''
    return f'<{tag}{c} data-f="{esc(key)}">{words}</{tag}>'


def empty(place, words='(none)'):
    """A list of the data file that holds nothing, said where it would be."""
    return f'<p class="none" data-e="{esc(place)}">{esc(words)}</p>'


def part(pid, h):
    """H with its first element marked as one part of the page
    (data-part="<kind>" or "<kind>#<i>"): every word of the page but a
    section's <h2>, the <title> and the <style> is in exactly one part,
    and the check holds the page's parts to the ones the data calls for
    (tests/_harness.parts_called_for)."""
    i = h.index('>')
    return h[:i] + f' data-part="{esc(pid)}"' + h[i:]


def not_done(word, path, why_html):
    """The box of a record the run did not do, inside the part that
    shows it: its state in capitals and its reason, where the caller
    gives one."""
    return (
        f'<div class="nd"><b data-p="{esc(path)}/state">{esc(word.upper())}</b>'
        f' {why_html}</div>'
    )


def why_of(path, block):
    w = block.get('why')
    if w is None:
        return ''
    if isinstance(w, list):
        return (
            '<ul>'
            + ''.join(
                f'<li>{mark(f"{path}/why#{i}", x)}</li>'
                for i, x in enumerate(w)
            )
            + '</ul>'
        )
    return '&mdash; ' + mark(f'{path}/why', w)


def items(r):
    """Every warning of the run, in step order, each once: two items whose
    text is the same are one, as in the text's warnings block."""
    seen, out = set(), []
    for st in ('step0', 'step1', 'step2', 'step11'):
        b = r['warnings'][st]
        for i, w in enumerate(b.get('items', [])):
            if w['text'] in seen:
                continue
            seen.add(w['text'])
            out.append((f'warnings/{st}/items#{i}', w))
    return out


def card(path, w):
    lines = w['text'].split('\n')
    body = '\n'.join(
        [lines[0][3:]]
        + [ln[3:] if ln.startswith('   ') else ln for ln in lines[1:]]
    )
    return (
        f'<details class="card" data-w="{esc(path)}"'
        f' data-part="card:{esc(path)}"><summary>'
        f'{mark(path + "/title", w["title"])}</summary>'
        f'<pre data-p="{esc(path)}/text">{esc(body)}</pre></details>'
    )


# The section a warning of class "change" is shown in, as its card, by
# its kind; any other kind of that class in "Other changes the run found"
# ('other'), so that none is left off the page.
WARN_HOME = {
    'not_on_new_machine': 'added',
    'glibc_refused': 'added',
    'collation_version_same': 'how',
    'c_utf8_differs': 'step2',
    'charmap_distro_buildable': 'step2',
}


def homed(r, home):
    """The cards of the warnings of class "change" whose section is HOME,
    in step order, each once."""
    return ''.join(card(p, w) for p, w in items(r)
                   if w['class'] == 'change'
                   and WARN_HOME.get(w['kind'], 'other') == home)


# The warnings of class "not checked" that already say, under Not
# verified, that a record the page reads is not done (covered(): where
# its data names that record, or by its kind alone where one card says it
# for the whole run): the record then needs no entry of its own there.
# Where the card is not on the page (its block not reported, or the
# warning absent), the record has its own.
COVERED_BY = {
    'compared/tags': ('signatures_unchecked',),
    'compared/collation_version': ('collation_version_unknown',),
    'compared/note:fallback': ('machine_falls_back_to_tags',),
    'step11/verdicts': ('not_measured',),
    'step2/removed/source': ('sides_read_as_tags',),
    "step2/removed/source:tags, because of a machine's file": (
        'machine_falls_back_to_tags',),
    'run/nothing_compared': ('same_commit', 'same_tree',
                             'same_tree_machines'),
    'step2/rules:not compared': ('same_commit', 'same_tree',
                                 'same_tree_machines'),
    'step2/rules_characters:not compared': ('same_commit', 'same_tree',
                                            'same_tree_machines'),
}
# Records not done because the machines' files were not given, which is
# when step 11 does not run: said by step 11's own entry where step 11 did
# not run.
UNDER_STEP11 = ('run/machines', 'compared/note:not run', 'warnings/step0',
                'warnings/step11')
# What each record the page reads is called in its own entry of Not
# verified.
RECORD_WORDS = {
    'step11/counts': "Step 11's counts",
    'step11/findings': 'Which locales sort differently (step 11)',
    'step11/characters': "Step 11's file of every character found",
    'step11/sql': "Step 11's SQL files",
    'step2/removed': "Step 2's list of the locale files gone",
    'step2/rules': "Step 2's sort rules of the locale sources",
    'step2/rules/differ': 'The characters of the changed rules of',
    'step2/rules_characters': "Step 2's file of the characters of the "
                              'changed rules',
    'compared': 'How this was compared',
    'compared/note': "Step 0's note on what each machine was compared as",
    'compared/machines': 'The charmaps of the',
    'compared/latest': 'Which build each latest-elN names',
    'compared/latest/resolved': 'The date of the build of',
    'compared/tags': 'The signature of',
    'compared/collation_version': 'The collation version PostgreSQL '
                                  'records',
    'run/machines': "The machines' files",
    'warnings/step0': "The warnings of the machines' files",
    'warnings/step11': 'The warnings of step 11',
}


# The sources of step 2's Removed list that leave what a distro adds or
# drops not checked, and the side(s) read as a tag (report_data
# REMOVED_SOURCES); "tags, because of a machine's file" by its card.
SOURCE_TAG_SIDES = {'old side is a tag': ('old',),
                    'new side is a tag': ('new',), 'tags': ('old', 'new'),
                    "tags, because of a machine's file": ()}


def nd_records(r):
    """Each record the page reads that is not done and is said under Not
    verified in an entry of its own (nd_entry) unless an entry there
    already says it, in the order of Not verified, as (place, state). Not
    here: step 11 and the warnings blocks not reported (their entries
    above), what a group of step 11 leaves unknown (its NOT SETTLED in
    part), and what has no entry of its own there (a new locale's
    spelling, a probe of a group, a locale unchanged only as far as step
    11 measured it, which its tile counts and far_line names)."""
    out = []
    s11, s2, c = r['step11'], r['step2'], r['compared']
    if s11['state'] == 'reported':
        for k in ('counts', 'findings', 'characters', 'sql'):
            if s11[k]['state'] != 'reported':
                out.append((f'step11/{k}', s11[k]['state']))
    if s11['state'] == 'reported':
        for k, v in s11['verdicts'].items():
            if v['verdict'] == 'unmeasured':
                out.append((f'step11/verdicts/{k}', 'unmeasured'))
    if s2['removed']['state'] != 'reported':
        out.append(('step2/removed', s2['removed']['state']))
    elif s2['removed']['source'] in SOURCE_TAG_SIDES:
        out.append(('step2/removed/source', s2['removed']['source']))
    ru = s2['rules']
    if ru['state'] != 'reported':
        out.append(('step2/rules', ru['state']))
    else:
        for i, d in enumerate(ru['differ']):
            if d['characters']['state'] == 'not known':
                out.append((f'step2/rules/differ#{i}/characters',
                            'not known'))
    if s2['rules_characters']['state'] not in ('reported',):
        out.append(('step2/rules_characters',
                    s2['rules_characters']['state']))
    if r['run']['nothing_compared'] != 'none':
        out.append(('run/nothing_compared', r['run']['nothing_compared']))
    m = r['run']['machines']
    if m['state'] != 'given':
        out.append(('run/machines', m['state']))
    for st in ('step0', 'step11'):
        if r['warnings'][st]['state'] == 'not run':
            out.append((f'warnings/{st}', 'not run'))
    if c['state'] != 'reported':
        out.append(('compared', c['state']))
        return out
    if c['note']['state'] in ('fallback', 'not reported', 'not run'):
        out.append(('compared/note', c['note']['state']))
    for i, x in enumerate(c['machines']):
        if x['charmaps']['state'] != 'counted':
            out.append((f'compared/machines#{i}/charmaps',
                        x['charmaps']['state']))
    if c['latest']['state'] == 'not reported':
        out.append(('compared/latest', 'not reported'))
    for i, x in enumerate(c['latest'].get('resolved', [])):
        if x['when']['state'] != 'dated':
            out.append((f'compared/latest/resolved#{i}/when',
                        x['when']['state']))
    for i, t in enumerate(c['tags']):
        if t['status'] != 'good':
            out.append((f'compared/tags#{i}', t['status']))
    if c['collation_version']['state'] == 'unknown':
        out.append(('compared/collation_version', 'unknown'))
    return out


def covered(r, place, state, cards):
    """Whether the not-done record at PLACE is already said under Not
    verified by an entry whose data names it: step 11's entry where step
    11 did not run (no machines' files); or a card of class "not checked"
    of CARDS (each one's kind and data) that names that record: by its
    data, or by its kind alone where the card speaks for the whole run
    (each kind's rule below)."""
    base = re.sub(r'#\d+.*', '', place)
    if base.startswith('step11/verdicts/'):
        base = 'step11/verdicts'
    if base in UNDER_STEP11 or f'{base}:{state}' in UNDER_STEP11:
        return r['step11']['state'] == 'not run'
    kinds = COVERED_BY.get(f'{base}:{state}') or COVERED_BY.get(base) or ()
    for kind, d in cards:
        if kind not in kinds:
            continue
        if base == 'step11/verdicts':
            name = place.split('/')[2]
            if name in [x['name'] for x in d['locales']]:
                return True
        elif base == 'step2/removed/source' and kind == 'sides_read_as_tags':
            pair = r['pair']
            want = [pair[s] for s in SOURCE_TAG_SIDES[state]]
            if all(x in d['refs'] for x in want):
                return True
        elif base == 'compared/tags':
            x = at(r, place)
            if {'ref': x['ref'], 'status': x['status']} in d.get('refs', []):
                return True
        elif base == 'compared/collation_version':
            if d.get('sides') == r['compared']['collation_version']['sides']:
                return True
        else:
            return True
    return False


def not_verified(r):
    """The entries of the Not verified section: each warnings block not
    reported, step 11 when it did not run or was not reported, each other
    record the page reads that is not done and that no entry here
    already says (nd_records, covered), each group of step 11 it could
    not settle in part (unsettled, "NOT SETTLED in part"), each warning
    of class "not checked", and each entry no SQL file of step 11 checks
    (16.413)."""
    out = []
    for st in ('step0', 'step1', 'step2', 'step11'):
        b = r['warnings'][st]
        if b['state'] == 'not reported':
            out.append(('block', st, b))
    s11 = r['step11']
    if s11['state'] != 'reported':
        out.append(('step11', None, s11))
    cards = [(w['kind'], w['data']) for _, w in items(r)
             if w['class'] == 'not checked']
    for place, state in nd_records(r):
        if not covered(r, place, state, cards):
            out.append(('nd', place, state))
    if s11['state'] == 'reported' and \
            s11['findings']['state'] == 'reported':
        for i, g in enumerate(s11['findings']['groups']):
            if unsettled(g):
                out.append(('settled', i, g))
    for path, w in items(r):
        if w['class'] == 'not checked':
            out.append(('card', path, w))
    if s11['state'] == 'reported' and s11['sql']['state'] == 'reported':
        for i, g in enumerate(s11['sql']['not_checked']):
            out.append(('sql', f'step11/sql/not_checked#{i}', g))
    return out


# What each warnings block is called on the page.
BLOCK_WORDS = {
    'step0': "the machines' files",
    'step1': 'the two sides',
    'step2': 'step 2',
    'step11': 'step 11',
}


def at(r, place):
    """The value at PLACE of the report ('/' a key, '#' an index)."""
    v = r
    for p in place.split('/'):
        k, _, i = p.partition('#')
        v = v[k] if k else v
        v = v[int(i)] if i else v
    return v


def nd_entry(r, place, state):
    """A record not done in its own entry of Not verified: what it is
    and, in capitals, what that leaves (its state, or NOT MEASURED, NOT
    CHECKED, NOTHING COMPARED, NOT VERIFIED), with its reason where the
    data gives one, or where the page says it."""
    base = re.sub(r'#\d+.*', '', place)
    if base.startswith('step11/verdicts/'):
        name = place.split('/')[2]
        return part('nv:nd:' + place, (
            '<div class="card nd-card"><b>Whether this locale sorts '
            'differently (step 11): <span class="loc">' + esc(name)
            + '</span></b> <div class="nd"><b data-p="' + esc(place)
            + '/verdict" data-v="unmeasured">NOT MEASURED</b> &mdash; '
            + mark(place + '/words', at(r, place)['words']) + '</div></div>'))
    if base == 'step2/removed/source':
        return part('nv:nd:' + place, (
            '<div class="card nd-card"><b>What each distro adds to its '
            'locale files or drops</b> <div class="nd"><b>NOT CHECKED</b> '
            '&mdash; see Gone at, under Locales added and removed.</div>'
            '</div>'))
    if base == 'run/nothing_compared':
        return part('nv:nd:' + place, (
            '<div class="card nd-card"><b>What changed between the two '
            'sides</b> <div class="nd"><b>NOTHING COMPARED</b> &mdash; see '
            'the top of this page.</div></div>'))
    what = esc(RECORD_WORDS[base])
    x = at(r, place)
    if base == 'compared/tags':
        what += ' ' + mark(place + '/ref', x['ref'])
        st = (f'<b data-p="{place}/status" data-v="{esc(x["status"])}">NOT '
              'VERIFIED</b> &mdash; ' + mark(place + '/detail', x['detail']))
    elif base == 'compared/collation_version':
        st = not_done(state, place, '&mdash; no glibc version for ' + ', '.join(
            mark(f'{place}/sides#{i}', s) for i, s in enumerate(x['sides'])))
    elif base == 'compared/latest/resolved':
        e = at(r, place[:-len('/when')])
        what += ' ' + mark(place[:-len('/when')] + '/name', e['name'])
        st = not_done(state, place, '')
    elif base == 'compared/machines':
        what += ' ' + ('old' if place.startswith('compared/machines#0')
                       else 'new') + ' machine'
        st = not_done(state, place, why_of(place, x))
    elif base == 'step2/rules/differ':
        d = at(r, place[:-len('/characters')])
        what += ' ' + esc(', '.join(d['names']))
        st = not_done(state, place, why_of(place, x))
    else:
        st = not_done(state, place, why_of(place, x))
    return part('nv:nd:' + place,
                f'<div class="card nd-card"><b>{what}</b> {st}</div>')


def nv_html(r, entry):
    kind, path, x = entry
    if kind == 'card':
        return card(path, x)
    if kind == 'nd':
        return nd_entry(r, path, x)
    if kind == 'settled':
        return part(f'nv:settled#{path}', (
            '<div class="card nd-card"><b>NOT SETTLED in part</b>: '
            + esc(', '.join(x['locales'])) + ' &mdash; '
            + esc('; '.join(unsettled(x)))
            + '. <span class="hint">Open the group under Locales whose '
            'sort order changed.</span></div>'))
    if kind == 'sql':
        return part('nv:not checked#' + path.rsplit('#', 1)[1], label(
            'nv not checked',
            '<b>NOT VERIFIED in PostgreSQL</b>: no SQL file checks',
            ' ' + unchecked(path, x),
            cls='card nd-card',
        ))
    if kind == 'block':
        p = f'warnings/{path}'
        return (
            f'<div class="card nd-card" data-part="nv:block {path}">'
            '<b>The warnings of '
            + fixed('block ' + path, esc(BLOCK_WORDS[path]))
            + '</b> '
            + not_done(x['state'], p, why_of(p, x))
            + '<p class="hint">Whatever that step warned about is not '
            'on this page.</p></div>'
        )
    p = 'step11'
    nd = ''.join(
        f'<li>{mark(f"step11/not_done#{i}", t)}</li>'
        for i, t in enumerate(x.get('not_done', []))
    )
    return (
        '<div class="card nd-card" data-part="nv:step11"><b>How each '
        'machine sorts every '
        'locale (the measured order)</b> '
        + not_done(x['state'], p, why_of(p, x))
        + (
            fixed('nd-list', 'So none of these was checked:', 'p')
            + f'<ul>{nd}</ul>'
            if nd
            else ''
        )
        + '</div>'
    )


def tile(key, words, body, cls=''):
    """A count over its words; NOT MEASURED (cls 'nm') under them."""
    return part('tile:' + key, label(
        key,
        words,
        f'<b>{body}</b>',
        cls=('tile ' + cls).strip(),
        after=cls != 'nm',
    ))


def tiles(r):
    s11, c = r['step11'], r['step11'].get('counts')
    if s11['state'] != 'reported' or c['state'] != 'reported':
        block = s11 if s11['state'] != 'reported' else c
        path = 'step11' if s11['state'] != 'reported' else 'step11/counts'
        t = [
            tile(
                'not measured',
                'NOT MEASURED: how many locales sort '
                'differently, are gone or new',
                f'the measured order <span data-p="{esc(path)}/state">'
                f'{esc(block["state"].upper())}</span> {why_of(path, block)}',
                'nm',
            )
        ]
    else:
        p = 'step11/counts'
        far = c['unchanged_as_far_as_measured']
        nc = c['not_measured'] + c['refused']
        t = [
            tile(
                'changed',
                'locales sort differently',
                mark(p + '/changed', c['changed']),
                'bad',
            ),
            tile(
                'tile only old',
                'locales gone on the new machine',
                mark(p + '/only_old', c['only_old']),
                'bad' if c['only_old'] else '',
            ),
            tile(
                'tile only new',
                'locales new on the new machine',
                mark(p + '/only_new', c['only_new']),
            ),
            tile(
                'unchanged',
                'locales unchanged',
                mark(p + '/unchanged', c['unchanged']),
                'ok',
            )
            + (
                part('tile:far', label(
                    'far',
                    'of them only as far as measured:',
                    mark(p + '/unchanged_as_far_as_measured', far),
                    cls='tile-note',
                ))
                if far
                else ''
            ),
            tile(
                'not compared',
                'locales not compared',
                made('not_measured+refused', nc),
                'warn' if nc else '',
            )
            + part('tile:not measured count', label(
                'not measured count',
                'not measured:',
                mark(p + '/not_measured', c['not_measured']),
                cls='tile-note',
            ))
            + part('tile:refused', label(
                'refused',
                'refused by glibc:',
                mark(p + '/refused', c['refused']),
                cls='tile-note',
            )),
        ]
    t.append(
        tile(
            'not verified',
            'things NOT verified, listed below',
            made('not_verified', len(not_verified(r))),
            'warn',
        )
    )
    return '<div class="tiles">' + ''.join(t) + '</div>'


# What step 2's Removed list says of the files a distro adds or drops,
# by where its files come from (report_data.REMOVED_SOURCES), after what
# the text's Removed block prints for each (audit.sh), said for the page;
# {old} and {new} are the class of each side (run.old_class,
# run.new_class), {word} the pair's word (run.word, audit.sh's $WORD).
SOURCE_WORDS = {
    'machine sources': "Each machine's locale files are its own locale "
    'sources as its file holds them, checked against no package.',
    'build packages': "Each build's locale files are the ones its "
    'glibc-locale-source package installs, checked one by one.',
    'old side is a tag': 'The old side is an upstream {old}, so what the old '
    'distro adds to it is NOT CHECKED: a file only that distro ships, and '
    'the new side does not, is not listed. Name the old side as a build to '
    'check it.',
    'new side is a tag': 'The new side is an upstream {new}, so what the new '
    'distro adds to it or drops is NOT CHECKED: a file listed here may still '
    'ship there, and one it drops is not listed. Name the new side as a '
    'build to check it.',
    'tags': 'The {word} cannot show what your distro adds or drops: NOT '
    "CHECKED. Pass --old-node and --new-node, each machine's file, to "
    'check it.',
    "tags, because of a machine's file": 'The {word} cannot show what your '
    "distro adds or drops: NOT CHECKED. A machine's file made both sides "
    'read their tags; why is under Not verified.',
}
# A spelling state of a locale step 2 found new, in the page's words.
SPELLING_WORDS = {'in the C.UTF-8 notice': 'C.UTF-8: see Not verified'}


def verdict_list(r, verdict, key, words):
    v = r['step11']['verdicts']
    names = [k for k in v if v[k]['verdict'] == verdict]
    body = (
        '<ul class="names">'
        + ''.join(
            f'<li data-k="step11/verdicts" data-p="step11/verdicts/{esc(k)}/verdict"'
            f' data-v="{esc(verdict)}">{esc(k)}</li>'
            for k in names
        )
        + '</ul>'
        if names
        else empty('step11/verdicts:' + verdict)
    )
    return part('added:' + key, label(key, words, body, head='h4'))


def unread_note(r):
    """Where step 2 compared and left entries of a machine's locale
    sources unread, the line under Gone at and New at that says they are
    in neither list (filter_lc_collate_changes drops them from both); ''
    where it left none. Each list said empty then says "(none among the
    files read)" (unread_none)."""
    ru = r['step2']['rules']
    if ru['state'] != 'reported' or not ru['unread']:
        return ''
    n = len(ru['unread'])
    one = n == 1
    return (
        '<p class="hint">' + made('unread', n)
        + (' entry' if one else ' entries')
        + " of a machine's locale sources "
        + ('was' if one else 'were') + ' not read, so '
        + ('it is' if one else 'they are')
        + " in neither list: see Which locales' sort rules changed"
        ' (step 2).</p>'
    )


def unread_none(r, place):
    """A list of step 2 said empty: "(none among the files read)" where
    it left entries unread (unread_note), else "(none)"."""
    return empty(place, '(none among the files read)'
                 if unread_note(r) else '(none)')


def added_removed(r):
    out = [
        '<details class="sec" open><summary><h2>Locales added and removed'
        '</h2></summary><div class="secb">'
    ]
    s11 = r['step11']
    out.append('<h3 data-part="added:machines head">On the machines '
               '(what each machine measured)</h3>')
    if s11['state'] != 'reported':
        out.append(part('added:nd:step11', not_done(
            s11['state'], 'step11', why_of('step11', s11))))
        out.append(homed(r, 'added'))
    else:
        out.append(
            verdict_list(r, 'only old', 'only old', 'Not on the new machine')
        )
        out.append(homed(r, 'added'))
        out.append(
            verdict_list(r, 'only new', 'only new', 'Only on the new machine')
        )
    s2 = r['step2']
    rm, ru = s2['removed'], s2['rules']
    out.append(
        '<h3 data-part="added:sources head">Locale source files between the '
        + mark('run/word', r['run']['word'])
        + '</h3>'
    )
    new_at = mark('pair/new', r['pair']['new'])
    if rm['state'] != 'reported':
        out.append(part('added:gone',
            label(
                'gone',
                'Gone at ' + new_at,
                not_done(
                    rm['state'], 'step2/removed', why_of('step2/removed', rm)
                ),
                head='h4',
            ))
        )
    else:
        if rm['rows']:
            rows = []
            for i, x in enumerate(rm['rows']):
                p = f'step2/removed/rows#{i}'
                nm = (
                    mark(p + '/name', x['name'])
                    if x['name'] != 'C'
                    else f'<span data-p="{p}/name" data-as="C.UTF-8">C.UTF-8</span>'
                )
                how = 'deleted' if x['how'] == 'deleted' else 'renamed to'
                to = (
                    ''
                    if x['how'] == 'deleted'
                    else ' ' + mark(p + '/to', x['to'])
                )
                rows.append(
                    f'<li>{nm} <span class="how" data-p="{p}/how"'
                    f' data-v="{esc(x["how"])}">{how}</span>{to}</li>'
                )
            body = '<ul class="names">' + ''.join(rows) + '</ul>'
        else:
            body = unread_none(r, 'step2/removed/rows')
        words = SOURCE_WORDS[rm['source']].format(
            old=r['run']['old_class'],
            new=r['run']['new_class'],
            word=r['run']['word'],
        )
        body += (
            '<p class="hint" data-p="step2/removed/source" '
            f'data-v="{esc(rm["source"])}">{esc(words)}</p>'
        ) + unread_note(r)
        out.append(part('added:gone', label('gone', 'Gone at ' + new_at,
                                            body, head='h4')))
    if ru['state'] != 'reported':
        out.append(part('added:new',
            label(
                'new',
                'New at ' + new_at,
                not_done(
                    ru['state'], 'step2/rules', why_of('step2/rules', ru)
                ),
                head='h4',
            ))
        )
    else:
        rows = ru['added']['rows']
        if rows:
            li = []
            for i, x in enumerate(rows):
                p = f'step2/rules/added/rows#{i}'
                sp = x['spellings']
                if isinstance(sp, list):
                    spl = ', '.join(
                        mark(f'{p}/spellings#{j}', s) for j, s in enumerate(sp)
                    )
                    spl = f' <span class="hint">as {spl}</span>'
                else:
                    spl = (
                        f' <span class="hint" data-p="{p}/spellings/state"'
                        f' data-v="{esc(sp["state"])}">'
                        f'{esc(SPELLING_WORDS.get(sp["state"], sp["state"]))}'
                        '</span>'
                    )
                fr = (
                    ''
                    if x['renamed_from'] == '-'
                    else ' <span class="hint">renamed from '
                    + mark(p + '/renamed_from', x['renamed_from'])
                    + '</span>'
                )
                li.append(f'<li>{mark(p + "/name", x["name"])}{fr}{spl}</li>')
            body = '<ul class="names">' + ''.join(li) + '</ul>'
        else:
            body = unread_none(r, 'step2/rules/added/rows')
        body += unread_note(r)
        if ru['added']['state'] == 'notice':
            body += (
                '<p class="hint" data-p="step2/rules/added/state" '
                'data-v="notice">The old side is an upstream '
                + mark('run/old_class', r['run']['old_class'])
                + ': a file it does not have does not prove the locale '
                'was absent on the old system (see Not verified).</p>'
            )
        out.append(part('added:new', label('new', 'New at ' + new_at,
                                           body, head='h4')))
    out.append('</div></details>')
    return ''.join(out)


def machines(r, c):
    m = r['run']['machines']
    if m['state'] != 'given':
        return part('how:nd:run/machines', not_done(
            m['state'], 'run/machines', why_of('run/machines', m)))
    if c['note']['state'] == 'fallback':
        return part('how:nd:compared/note', not_done(
            'fallback',
            'compared/note',
            "&mdash; a machine's file made both sides read their upstream "
            'tags; why is under Not verified',
        ))
    out = []
    if c['note']['state'] == 'not reported':
        # What each machine was compared as is step 0's note: not known.
        out.append(part('how:nd:compared/note', not_done(
            'not reported', 'compared/note',
            why_of('compared/note', c['note']))))
    elif not c['machines']:
        return part('how:machines none', empty('compared/machines'))
    for i, x in enumerate(c['machines']):
        p = f'compared/machines#{i}'
        ch = x['charmaps']
        chs = (
            mark(p + '/charmaps/count', ch['count'])
            if ch['state'] == 'counted'
            else not_done(
                ch['state'], p + '/charmaps', why_of(p + '/charmaps', ch)
            )
        )
        what = (
            'its locale sources and charmaps'
            if ch['state'] == 'counted'
            else 'its locale sources'
        )
        out.append(part(f'how:machine#{i}',
            label(
                'machine ' + ('old' if i == 0 else 'new'),
                'old' if i == 0 else 'new',
                '<ul class="plain">'
                + label(
                    'word',
                    'rpm -q glibc:',
                    ' ' + mark(p + '/word', x['word']),
                    'li',
                )
                + label(
                    'locales',
                    'locale sources:',
                    ' ' + mark(p + '/locales', x['locales']),
                    'li',
                )
                + label('charmaps', 'charmaps:', ' ' + chs, 'li')
                + label(
                    'commit',
                    'compared as:',
                    f' {what} on '
                    + mark(p + '/tag', x['tag'])
                    + ', as commit <code>'
                    + mark(p + '/commit', x['commit'])
                    + '</code>',
                    'li',
                )
                + '</ul>',
                cls='side',
                head='b',
            ))
        )
    return ''.join(out)


def compared(r):
    c = r['compared']
    out = [
        '<details class="sec" open><summary><h2>How this was compared</h2>'
        '</summary><div class="secb">'
    ]
    if c['state'] != 'reported':
        out.append(part('how:nd:compared', not_done(
            c['state'], 'compared', why_of('compared', c))))
        out.append(homed(r, 'how'))
        out.append('</div></details>')
        return ''.join(out)
    out.append('<h4 data-part="how:machines head">Machines</h4>'
               + machines(r, c))
    b = c['builds']
    if b['state'] == 'rebuilt':
        li = []
        for i, x in enumerate(b['entries']):
            p = f'compared/builds/entries#{i}'
            li.append(part(f'how:build#{i}',
                label(
                    'build',
                    mark(p + '/build', x['build']) + ':',
                    ' '
                    + mark(p + '/tag', x['tag'])
                    + ' with localedata/ and locale/ from its source RPM ('
                    + mark(p + '/patches/applied', x['patches']['applied'])
                    + ' of its '
                    + mark(p + '/patches/of', x['patches']['of'])
                    + ' patches change them), as commit <code>'
                    + mark(p + '/commit', x['commit'])
                    + '</code>',
                    'li',
                ))
            )
        out.append(
            '<h4 data-part="how:builds head">Builds rebuilt from their '
            'packages</h4><ul class="plain">'
            + ''.join(li)
            + '</ul>'
        )
    la = c['latest']
    if la['state'] == 'reported':
        li = []
        for i, x in enumerate(la['resolved']):
            p = f'compared/latest/resolved#{i}'
            when = (
                ', by its metadata of ' + mark(p + '/when/utc', x['when']['utc'])
                if x['when']['state'] == 'dated'
                else ', and its metadata gives '
                f'<b data-p="{p}/when/state">NO DATE</b> this reads'
            )
            li.append(part(f'how:latest#{i}',
                label(
                    'latest',
                    mark(p + '/name', x['name']) + ' =',
                    ' '
                    + mark(p + '/build', x['build'])
                    + ', the newest glibc build in '
                    + mark(p + '/folder', x['folder'])
                    + when,
                    'li',
                ))
            )
        out.append(
            '<h4 data-part="how:latest head">latest-elN</h4>'
            '<ul class="plain">' + ''.join(li) + '</ul>'
        )
    elif la['state'] == 'not reported':
        out.append(
            '<h4 data-part="how:latest head">latest-elN</h4>'
            + part('how:nd:compared/latest', not_done(
                la['state'], 'compared/latest', why_of('compared/latest', la)
            ))
        )
    li = []
    for i, t in enumerate(c['tags']):
        p = f'compared/tags#{i}'
        li.append(part(f'how:tag#{i}',
            label(
                'tag',
                mark(p + '/ref', t['ref']) + ' =',
                ' <code>' + mark(p + '/commit', t['commit']) + '</code> '
                f'<span class="badge {"ok" if t["status"] == "good" else "warn"}"'
                f' data-p="{p}/status" data-v="{esc(t["status"])}">'
                + mark(p + '/detail', t['detail'])
                + '</span>',
                'li',
            ))
        )
    out.append(
        '<h4 data-part="how:tags head">Upstream tags</h4><ul class="plain">'
        + ''.join(li) + '</ul>'
    )
    cv = c['collation_version']
    out.append('<h4 data-part="how:cv head">Collation version PostgreSQL '
               'records</h4>')
    q = 'compared/collation_version'
    if cv['state'] == 'differ':
        out.append(
            '<p data-part="how:cv">'
            + label(
                'cv old',
                'old side:',
                ' ' + mark(q + '/old', cv['old']),
                'span',
            )
            + '; '
            + label(
                'cv new',
                'new side:',
                ' ' + mark(q + '/new', cv['new']),
                'span',
            )
            + '.</p>'
        )
    elif cv['state'] == 'same':
        out.append(
            '<p data-part="how:cv">'
            + label(
                'cv same',
                'both sides:',
                ' ' + mark(q + '/version', cv['version']),
                'span',
            )
            + '.</p>'
        )
    else:
        out.append(part('how:nd:compared/collation_version',
            not_done(
                cv['state'],
                q,
                '&mdash; no glibc version for '
                + ', '.join(
                    mark(f'{q}/sides#{i}', s)
                    for i, s in enumerate(cv['sides'])
                ),
            ))
        )
    out.append(homed(r, 'how'))
    out.append('</div></details>')
    return ''.join(out)


def unchecked(p, g):
    """An entry no SQL file checks, at P: its names, its reason and, where
    the data gives it, what else PostgreSQL calls those locales."""
    return (
        ', '.join(mark(f'{p}/names#{j}', n) for j, n in enumerate(g['names']))
        + ' &mdash; ' + mark(p + '/why', g['why'])
        + ('<div class="hint">also, in PostgreSQL: '
           + mark(p + '/also', g['also']) + '</div>' if 'also' in g else '')
    )


def confirm(r):
    """The SQL files step 11 wrote, each with its number of tests, or
    "(no SQL file was written)" where none was; then each entry no file
    checks, NOT VERIFIED, with its reason and, where the data gives it,
    what else PostgreSQL calls those locales. Where step 11, or what it
    wrote of the SQL files, is not reported: its state and reason
    instead."""
    s11 = r['step11']
    out = [
        '<details class="sec" open><summary><h2>Confirm in PostgreSQL</h2>'
        '</summary><div class="secb">'
    ]
    if s11['state'] != 'reported':
        out.append(part('confirm:nd:step11', not_done(
            s11['state'], 'step11', why_of('step11', s11))))
        out.append(
            '<p class="hint" data-part="confirm:not run hint">The SQL files '
            'that confirm a change are '
            "written from the machines' measurements.</p>"
        )
    elif s11['sql']['state'] != 'reported':
        out.append(part('confirm:nd:step11/sql', not_done(
            s11['sql']['state'], 'step11/sql',
            why_of('step11/sql', s11['sql']))))
    elif not s11['sql']['files']:
        out.append(part('confirm:files none', empty(
            'step11/sql/files', '(no SQL file was written)')))
    else:
        out.append(
            '<p data-part="confirm:run each">Run each file unchanged on '
            'both servers, in a database of '
            'its encoding, then compare the two outputs:</p>'
            + part('confirm:psql', fixed(
                'psql',
                'psql -X -d DATABASE -f FILE &gt; old.out 2&gt;&amp;1   '
                '# and new.out on the new server\npython3 '
                'scripts/confirm_sql.py read FILE old.out new.out',
                'pre',
            ))
            + '<ul class="plain">'
        )
        for i, x in enumerate(s11['sql']['files']):
            p = f'step11/sql/files#{i}'
            out.append(part(f'confirm:sql#{i}',
                label(
                    'sql',
                    mark(p + '/encoding', x['encoding']) + ' database:',
                    ' <code>' + mark(p + '/file', x['file']) + '</code>'
                    ' &mdash; ' + mark(p + '/tests', x['tests']) + ' test(s)',
                    'li',
                ))
            )
        out.append('</ul>')
    if s11['state'] == 'reported' and s11['sql']['state'] == 'reported':
        for i, g in enumerate(s11['sql']['not_checked']):
            out.append(part(f'confirm:not checked#{i}',
                label(
                    'not checked',
                    '<b>NOT VERIFIED</b> by these files:',
                    ' ' + unchecked(f'step11/sql/not_checked#{i}', g),
                    cls='nd',
                ))
            )
    out.append('</div></details>')
    return ''.join(out)


# How many strings a list of a group draws before "and N more"; past it,
# a sample of the code points that draw as a character.
SAMPLE = 60
# How many pairs step 11 could not decide the page shows.
UNDECIDED_SHOWN = 10


def glyph(cp):
    """A code point drawn: a letter, number, punctuation or symbol as
    itself, a mark that combines over a dotted circle (U+25CC), anything
    else (a control, a space, a code point not assigned or past Unicode)
    as nothing."""
    if cp > 0x10FFFF:
        return ''
    cat = unicodedata.category(chr(cp))
    if cat[0] in 'LNPS':
        return chr(cp)
    if cat[0] == 'M':
        return '\u25cc' + chr(cp)
    return ''


def code_point(tok):
    """The code point a string of step 11 names, or None for one named by
    its bytes (report_data.STRING)."""
    m = report_data.STRING.fullmatch(tok)
    return int(m[1], 16) if m[1] else None


def cell(tok, named=False):
    """One string, drawn: the character where it draws, its code, and its
    Unicode name where NAMED and it has one; a string named by its bytes,
    as its bytes."""
    cp = code_point(tok)
    if cp is None:
        return f'<span class="ch"><code>{esc(tok)}</code></span>'
    return cp_cell(cp, tok, named)


def cp_cell(cp, tok, named=False):
    """Code point CP drawn as cell draws it, its code spelt TOK. Step 2
    gives its characters as whole numbers with no upper bound
    (report_data._rules), past what report_data.STRING reads."""
    g = glyph(cp)
    name = unicodedata.name(chr(cp), '') if cp <= 0x10FFFF else ''
    return (
        '<span class="ch">'
        + (f'<span class="g">{esc(g)}</span> ' if g else '')
        + f'<code>{esc(tok)}</code>'
        + (f' <span class="nm">{esc(name)}</span>' if named and name else '')
        + '</span>'
    )


def strings(runs, sample):
    """The strings of RUNS [first, last] in order, each as (first, last,
    how many strings it stands for): a code point alone, a run of strings
    named by their bytes whole, first..last; every one, or with SAMPLE
    only the first SAMPLE code points that draw."""
    out = []
    for a, b in runs:
        n = report_data._run_size('run', [a, b])
        ca = code_point(a)
        if ca is None:
            if not sample:
                out.append((a, b, n))
            continue
        for c in range(ca, ca + n):
            if len(out) >= SAMPLE:
                return out
            if not sample or glyph(c):
                out.append((f'U+{c:04X}', f'U+{c:04X}', 1))
    return out


def run_cell(a, b, named=False):
    """A string drawn (cell), or a run of strings named by their bytes,
    first..last."""
    if a == b:
        return cell(a, named)
    return (f'<span class="ch"><code>{esc(a)}..{esc(b[len("bytes "):])}'
            '</code></span>')


def drawn(r, runs, count, named=False):
    """A list of a group drawn: all of it up to SAMPLE strings, else a
    sample; then how many more and the file that holds them all."""
    sample = count > SAMPLE
    shown = strings(runs, sample)
    out = (
        '<div class="chars' + (' named' if named else '') + '">'
        + ' '.join(run_cell(a, b, named) for a, b, _ in shown)
        + '</div>'
    )
    if sample:
        out += (f'<p class="hint">A sample: the first {len(shown)} that '
                'draw as a character.</p>')
    more = count - sum(n for _, _, n in shown)
    if more:
        out += f'<p class="more">and {more:,} more' + all_in(r) + '</p>'
    return out


def characters_file(r):
    return 'measured_order_characters.' + r['pair']['slug'] + '.tsv'


def all_in(r):
    """Where every string of a group is listed: the characters file, where
    step 11 reported it."""
    if r['step11']['characters']['state'] != 'reported':
        return ''
    return f', all listed in <code>{esc(characters_file(r))}</code>'


def sql_of(r, g):
    """The entry no SQL file checks that is about this group's order, if
    any: the one whose names are its locales as the SQL files write them
    (locale_order.ascii_name; report_data.load holds that each such entry
    names one group)."""
    names = [locale_order.ascii_name(x) for x in g['locales']]
    return [(i, x) for i, x in enumerate(r['step11']['sql']['not_checked'])
            if x['about'] == 'order' and x['names'] == names]


def encodings(g):
    out = []
    for v in g['locales'].values():
        o, n = (x.upper() if x == 'unknown' else x
                for x in (v['codeset']['old'], v['codeset']['new']))
        w = n if o == n else f'{o} \u2192 {n}'
        if w not in out:
            out.append(w)
    return ', '.join(out)


def unsettled(g):
    """What step 11 could not settle for group G, in short words, in the
    order the group's own "Not settled for this group" says it: the
    pairs that tell levels apart told apart differently themselves, the
    levels not compared, the pairs it could not decide, what its
    compiled sort table changed where step 11 could not measure it, and
    which sequences that table changed where they cannot be named. Empty
    where it settled everything."""
    f = {x['kind']: x for x in g['findings']}
    out = []
    if 'probes changed' in f:
        out.append('the pairs that tell letters, accents and case apart '
                   'are told apart differently themselves')
    if 'left out' in f:
        out.append(' and '.join(f['left out']['left_out']) + ' NOT COMPARED')
    if 'undecided' in f:
        out.append(f"{f['undecided']['count']:,} pair(s) not decided")
    st = g['sort_table']
    if st['state'] == 'changed' and st['step11'] == 'unmeasured':
        out.append('what its compiled sort table changed NOT MEASURED by '
                   'step 11')
    if st.get('naming', {}).get('state') in ('unread', 'levels'):
        out.append('which sequences its compiled sort table changed NOT '
                   'NAMED')
    return out


def group_html(r, i, g):
    """One group of locales whose order changed (step11/findings/groups#I),
    held whole by the check: its names and encodings, what PostgreSQL
    also calls them, its compiled sort table, what moved, the pairs told
    apart differently, what did not move, what compares differently
    against another; and what step 11 could not settle for it
    (unsettled: then "NOT SETTLED in part" on its closed line too) and
    whether an SQL file checks its order."""
    f = {x['kind']: x for x in g['findings']}
    s11 = r['step11']
    sql = s11['sql']
    unchecked_here = sql_of(r, g) if sql['state'] == 'reported' else []
    head = []
    if g['found_by'] != 'step 11':
        head.append('found by its compiled sort table alone')
    mv = f.get('moved')
    if mv is not None:
        head.append(
            'no character moved' if not mv['count'] else
            f'{mv["characters"]:,} character(s)'
            + (f' and {mv["other_strings"]:,} other string(s)'
               if mv['other_strings'] else '') + ' moved')
    ta = f.get('told apart')
    if ta is not None:
        head.append(f'{ta["count"]:,} pair(s) told apart differently')
    summary = (
        '<span class="loc">' + esc(', '.join(g['locales'])) + '</span> '
        f'<span class="enc">({esc(encodings(g))})</span>'
        + (' <span class="badge warn">NOT VERIFIED in PostgreSQL</span>'
           if unchecked_here else '')
        + (' <span class="badge warn">NOT SETTLED in part</span>'
           if unsettled(g) else '')
        + ' <span class="what">' + esc('; '.join(head)) + '</span>'
    )
    body = []
    pg = []
    for v in g['locales'].values():
        q = v['postgres']
        for w in ((f'collation "{q["alias"]}"' if q['alias'] != 'none'
                   else None),
                  (f'database locale {q["spelling"]}'
                   if q['spelling'] != 'none' else None)):
            if w and w not in pg:
                pg.append(w)
    if pg:
        body.append('<p class="hint">Also, in PostgreSQL: '
                    + esc('; '.join(pg)) + '.</p>')
    st = g['sort_table']
    if st['state'] == 'changed':
        said = locale_order.table_change_lines(st)[2]
        body.append('<p class="hint">' + esc(said[0].upper() + said[1:])
                    + '.</p>')
    if mv is not None:
        if mv['count']:
            body.append(
                f'<h4>Moved: {mv["count"]:,}</h4>'
                '<p class="hint">They now sort in another place.</p>'
                + drawn(r, mv['runs'], mv['count'], mv['listed'] == 'each'))
        else:
            body.append('<p>No character moved.</p>')
    if ta is not None:
        body.append(f'<h4>Pairs told apart differently: {ta["count"]:,}</h4>')
        if ta['listed'] == 'each':
            body.append(
                '<table><tr><th>one</th><th>other</th><th>before</th>'
                '<th>now</th></tr>'
                + ''.join(
                    f'<tr><td>{cell(x["strings"][0], True)}</td>'
                    f'<td>{cell(x["strings"][1], True)}</td>'
                    f'<td>{esc(locale_order.RELATION_WORDS[x["before"]])}</td>'
                    f'<td>{esc(locale_order.RELATION_WORDS[x["now"]])}</td>'
                    '</tr>' for x in ta['pairs'])
                + '</table>')
        else:
            body.append('<p class="hint">Too many to list here'
                        + all_in(r) + '.</p>')
    dm = f.get('did not move')
    if dm is not None:
        body.append(
            '<details class="sub"><summary>Among them, '
            f'{dm["count"]:,} string(s) that did not move</summary>'
            + drawn(r, dm['runs'], dm['count']) + '</details>')
    rv = f.get('reversed')
    if rv is not None:
        body.append(
            '<details class="sub"><summary>'
            + ('all ' if rv['all'] else '')
            + f'{rv["characters"]:,} '
            f'character(s) and {rv["other_strings"]:,} other string(s) now '
            'compare differently against at least one other</summary>'
            + drawn(r, rv['runs'], rv['count']) + '</details>')
    gaps = []
    pc = f.get('probes changed')
    if pc is not None:
        def side(k):
            return (f'<tr><th>{k}</th>' + ''.join(
                '<td>' + (cell(x['pair'][0]) + ' / ' + cell(x['pair'][1])
                          if x['state'] == 'pair' else
                          f'<b>{esc(x["state"].upper())}</b>') + '</td>'
                for x in pc[k]) + '</tr>')
        gaps.append(
            'The pairs that tell letters, accents and case apart here are '
            'told apart differently themselves:<table><tr><th></th>'
            '<th>letters</th><th>accents</th><th>case</th></tr>'
            + side('before') + side('now') + '</table>')
    lo = f.get('left out')
    if lo is not None:
        gaps.append(('Pairs compared only by '
                     + esc(' and '.join(lo['compared'])) + '; '
                     if lo['compared'] else 'No pair can be compared; ')
                    + esc(' and '.join(lo['left_out']))
                    + ' <b>NOT COMPARED</b> here.')
    ud = f.get('undecided')
    if ud is not None:
        gaps.append(
            f'{ud["count"]:,} more pair(s) may be told apart differently; '
            'what was measured cannot decide:<ul class="pairs">'
            + ''.join(f'<li>{cell(a)} and {cell(b)}</li>'
                      for a, b in ud['pairs'][:UNDECIDED_SHOWN])
            + '</ul>'
            + (f'<p class="more">and {ud["count"] - UNDECIDED_SHOWN:,} more'
               + all_in(r) + '</p>'
               if ud['count'] > UNDECIDED_SHOWN else ''))
    if st['state'] == 'changed' and st['step11'] == 'unmeasured':
        gaps.append('What its compiled sort table changed: <b>NOT MEASURED</b> '
                    'by step 11.')
    if st.get('naming', {}).get('state') in ('unread', 'levels'):
        gaps.append('Which sequences its compiled sort table changed: '
                    '<b>NOT NAMED</b> (the line "Its compiled sort '
                    'table changed" says why).')
    if sql['state'] != 'reported':
        gaps.append('Whether an SQL file checks these locales: <b>NOT '
                    'REPORTED</b> (see Confirm in PostgreSQL).')
    for _, x in unchecked_here:
        gaps.append('<b>NOT VERIFIED in PostgreSQL</b>: no SQL file checks '
                    'these locales &mdash; ' + esc(x['why']) + '.')
    if gaps:
        body.append('<div class="gap"><b>Not settled for this group</b><ul>'
                    + ''.join(f'<li>{x}</li>' for x in gaps) + '</ul></div>')
    return (f'<details class="grp" data-part="grp#{i}"><summary>{summary}'
            '</summary>' + ''.join(body) + '</details>')


def unmeasured_line(r):
    """Under the locales whose sort order changed, with groups or
    without: each locale step 11 could not measure, named, in step 11's
    order, since it is not known to be unchanged; '' where step 11 gives
    no locale the verdict unmeasured."""
    un = [k for k, v in r['step11']['verdicts'].items()
          if v['verdict'] == 'unmeasured']
    if not un:
        return ''
    return part('changed:unmeasured', (
        '<p class="hint">Could not be measured, so NOT known to be '
        'unchanged: ' + ', '.join(
            '<span class="loc" data-k="step11/verdicts" data-p="'
            f'step11/verdicts/{esc(k)}/verdict" data-v="unmeasured">'
            f'{esc(k)}</span>' for k in un)
        + '. Why is under Not verified.</p>'))


def refused_line(r):
    """Under the locales whose sort order changed, with groups or
    without: each locale glibc refused, named, in step 11's order, since
    it is not known to be unchanged; its card, where step 11's warnings
    are reported, is under Locales added and removed (WARN_HOME). ''
    where glibc refused none."""
    re_ = [k for k, v in r['step11']['verdicts'].items()
           if v['verdict'] == 'refused']
    if not re_:
        return ''
    return part('changed:refused', (
        '<p class="hint">Refused by glibc, so NOT known to be unchanged: '
        + ', '.join(
            '<span class="loc" data-k="step11/verdicts" data-p="'
            f'step11/verdicts/{esc(k)}/verdict" data-v="refused">'
            f'{esc(k)}</span>' for k in re_)
        + '. See Locales added and removed.</p>'))


def far_line(r):
    """Under the locales whose sort order changed, with groups or
    without: each locale unchanged only as far as step 11 measured it,
    named, in step 11's order, in its verdict's own words, since it is not
    known to be unchanged; no cause, which differs from one to another
    (locale_order.verdicts_by_name: every unchanged one that is not
    firm); '' where there is none."""
    fa = [k for k, v in r['step11']['verdicts'].items()
          if v['verdict'] == 'far']
    if not fa:
        return ''
    return part('changed:far', (
        '<p class="hint">Unchanged only as far as step 11 measured, so NOT '
        'known to be unchanged: ' + ', '.join(
            '<span class="loc" data-k="step11/verdicts" data-p="'
            f'step11/verdicts/{esc(k)}/verdict" data-v="far">'
            f'{esc(k)}</span>' for k in fa)
        + '.</p>'))


def changed_order(r):
    """The locales whose sort order changed, one group of locales measured
    alike at a time, in step 11's order, then each locale step 11 could
    not measure (unmeasured_line), each glibc refused (refused_line) and
    each unchanged only as far as step 11 measured it (far_line); or why
    the run does not know: step 11 not run or not reported, its findings
    not reported."""
    s11 = r['step11']
    out = ['<details class="sec" open><summary><h2>Locales whose sort order '
           'changed</h2></summary><div class="secb">']
    if s11['state'] != 'reported':
        out.append(part('changed:nd:step11', not_done(
            s11['state'], 'step11', why_of('step11', s11))))
        out.append(part('changed:not run hint', fixed(
            'changed not run', 'Only the measurements of the '
                         'two machines show which locales sort differently. '
                         "Which locales' sort rules changed is in the next "
                         'section.', 'p', 'hint')))
    elif s11['findings']['state'] != 'reported':
        fi = s11['findings']
        out.append(part('changed:nd:step11/findings', not_done(
            fi['state'], 'step11/findings', why_of('step11/findings', fi))))
    elif not s11['findings']['groups']:
        # A locale step 11 could not measure, glibc refused, or measured
        # unchanged only in part, is not known to be unchanged: the list is
        # empty only among the locales measured, and the lines under it
        # name each one.
        lines = unmeasured_line(r) + refused_line(r) + far_line(r)
        out.append(part('changed:none', empty(
            'step11/findings/groups', '(none among the locales measured)'
            if lines else '(none)')))
        out.append(lines)
    else:
        out.append(part('changed:hint', fixed(
            'changed', 'Indexes, ORDER BY, comparisons and range partitions '
            'on text in these locales can give other results after the '
            'upgrade. Open a group for its characters.', 'p', 'hint')))
        ch = s11['characters']
        out.append(part('changed:characters file', label(
            'characters file',
            'Every character, other string and pair found:',
            ' <code>' + made('characters file', characters_file(r))
            + '</code>' if ch['state'] == 'reported' else ' '
            + not_done(ch['state'], 'step11/characters',
                       why_of('step11/characters', ch)),
            # A <p> cannot hold the box of a file not reported.
            tag='p' if ch['state'] == 'reported' else 'div', cls='hint')))
        out += [group_html(r, i, g)
                for i, g in enumerate(s11['findings']['groups'])]
        out.append(unmeasured_line(r) + refused_line(r) + far_line(r))
    out.append('</div></details>')
    return ''.join(out)


def rules_file(r):
    return 'step2_rules_characters.' + r['pair']['slug'] + '.tsv'


def rules_row(r, i, d):
    """One group of step 2's locales whose rules differ, in the words step
    2 prints it (filter_lc_collate_changes.print_differs): its names, and
    the characters of its changed rules: drawn where step 2 lists them
    (past SAMPLE, a sample of the first that draw, then how many more);
    their count where every rule can change; or the words for none named
    or not known, with the reason; then, where every rule can change,
    step 2's sentence saying so. Where the characters file is not
    reported, the row does not name it: the label above says its state."""
    c = d['characters']
    listed = r['step2']['rules_characters']['state'] == 'reported'
    if c['state'] == 'listed':
        cps = c['codepoints']
        sample = len(cps) > SAMPLE
        shown = [x for x in cps if glyph(x)][:SAMPLE] if sample else cps
        what = (f'<div>{esc(flc.LISTED_WORDS)} ({len(cps):,}):</div>'
                '<div class="chars named">'
                + ' '.join(cp_cell(x, f'U+{x:04X}', True) for x in shown)
                + '</div>')
        if sample:
            what += (f'<p class="hint">A sample: the first {len(shown)} that '
                     'draw as a character.</p>')
        if len(cps) > len(shown):
            what += (f'<p class="more">and {len(cps) - len(shown):,} more'
                     + (f', all listed in <code>{esc(rules_file(r))}</code>'
                        if listed else '') + '</p>')
    elif c['state'] == 'counted':
        what = (f'{c["count"]:,} {esc(flc.COUNTED_WORDS)}'
                + (f'; listed in <code>{esc(rules_file(r))}</code>'
                   if listed else ''))
    elif c['state'] == 'none named':
        what = esc(flc.NONE_NAMED_WORDS)
    else:
        what = f'{esc(flc.NOT_KNOWN_WORDS)} {esc(c["why"])}'
    if d['every_rule']:
        what += f'<p class="hint">{esc(flc.EVERY_RULE_WORDS)}</p>'
    return (f'<tr data-part="s2:differ#{i}"><td>{esc(", ".join(d["names"]))}'
            f'</td><td>{what}</td></tr>')


def sources(r):
    """What step 2 found of the sort rules in each locale's sources, under
    the text's title for it, in the words it prints them: its totals (and,
    in the page's own words, what their "not compared" counts), what it
    does not compare and that of its comparison of the character sets
    only its warnings are on this page, each group of locales whose rules
    differ with the characters of the changed rules, and the entries of a
    machine's sources it did not read, each with its reason; or its state
    and reason where it compared nothing or its record is not reported;
    and the cards of its warnings of class "change". Its totals, what they
    count as not compared, its groups and its entries not read are parts
    of kind s2:... with their place, each said whole."""
    ru = r['step2']['rules']
    out = ['<details class="sec" open><summary><h2>Which locales&#x27; '
           'sort rules changed (step 2)</h2></summary><div class="secb">']
    if ru['state'] != 'reported':
        out.append(part('s2:nd:step2/rules', not_done(
            ru['state'], 'step2/rules', why_of('step2/rules', ru))))
        out.append(homed(r, 'step2'))
        out.append('</div></details>')
        return ''.join(out)
    t = ru['totals']
    out.append('<p data-part="s2:totals">' + esc(flc.TOTALS_WORDS.format(
        **{k: f'{v:,}' for k, v in t.items()})) + '</p>')
    nc = not_compared_parts(r)
    if nc:
        out.append('<p class="hint" data-part="s2:not compared">'
                   + esc(nc) + '</p>')
    out.append(part('s2:not here', fixed(
        'step2 not here', esc(flc.CODE_NOT_COMPARED_HERE)
                     + "; of step 2's comparison of the character sets, "
                     'only its warnings are on this page.', 'p',
                     'hint')))
    if ru['differ']:
        rc = r['step2']['rules_characters']
        out.append(part('s2:rules file', label(
            'rules file', 'Every character of the changed rules:',
            ' <code>' + made('rules file', rules_file(r)) + '</code>'
            if rc['state'] == 'reported' else ' '
            + not_done(rc['state'], 'step2/rules_characters',
                       why_of('step2/rules_characters', rc)),
            tag='p' if rc['state'] == 'reported' else 'div', cls='hint')))
        out.append('<table>'
                   + part('s2:heads', fixed(
                       'step2 heads', '<th>locales whose rules differ'
                       '</th> <th>' + esc(flc.LISTED_WORDS) + '</th>', 'tr'))
                   + ''.join(rules_row(r, i, d)
                             for i, d in enumerate(ru['differ']))
                   + '</table>')
    else:
        out.append(part('s2:differ none', empty('step2/rules/differ',
                                                flc.NONE_DIFFER_WORDS)))
    if ru['unread']:
        out.append(
            '<div class="gap" data-part="s2:unread">'
            f'<b>{esc(flc.UNREAD_WORDS)} '
            f'({len(ru["unread"]):,}):</b><ul>' + ''.join(
                f'<li>{esc(u["name"])} ({esc("; ".join(u["why"]))})</li>'
                for u in ru['unread']) + '</ul></div>')
    out.append(homed(r, 'step2'))
    out.append('</div></details>')
    return ''.join(out)


def not_compared_parts(r):
    """What step 2's "N not compared" counts, worked out of its rules: the
    files only the new side has, those only the old side has, the entries
    of a machine's sources it did not read and counted, and the rest, the
    names its warnings say it did not compare, each with its reason: a sum
    report_data.load holds where those warnings are reported; where they
    are not, the rest is said as named in warnings NOT REPORTED, never
    worked out as a number of names. '' where it counts none."""
    ru = r['step2']['rules']
    n = ru['totals']['not_compared']
    if not n:
        return ''
    pair = r['pair']
    new, old = len(ru['added']['rows']), len(ru['removed'])
    unread = sum(1 for u in ru['unread'] if u['counted'])
    rest = n - new - old - unread
    told = r['warnings']['step2']['state'] == 'reported'
    # report_data.load refuses such a run first (its tie of the totals to
    # step 2's warnings, where they are reported): this stop is never
    # reached by a run load() accepts.
    if told and rest < 0:
        raise report_data.ReportError(
            f'step2.rules.totals.not_compared {n}: {new} only at the new '
            f'side, {old} only at the old, {unread} not read')
    parts = [f'{new:,} only at {pair["new"]}' if new else '',
             f'{old:,} only at {pair["old"]}' if old else '',
             f"{unread:,} not read among a machine's sources" if unread
             else '',
             (f'{rest:,} not compared, each with its reason under Not '
              'verified' if told else 'any other, named in the warnings of '
              'step 2, which are NOT REPORTED') if rest > 0 else '']
    return f'Of the {n:,} not compared: ' + '; '.join(
        x for x in parts if x) + '.'


def other_changes(r):
    """The section of the warnings of class "change" no other section
    holds: each as its card; and, for each warnings block the run did not
    report or run, its state and reason, since its warnings cannot be
    here; "(none)" only where every block was reported and none is
    here."""
    out = ['<details class="sec" open><summary><h2>Other changes the run '
           'found</h2></summary><div class="secb">']
    nd = []
    for st in ('step0', 'step1', 'step2', 'step11'):
        b = r['warnings'][st]
        if b['state'] != 'reported':
            p = f'warnings/{st}'
            nd.append(f'<div class="nd-card" data-part="other:block {st}">'
                      'The warnings of '
                      + fixed('other ' + st, esc(BLOCK_WORDS[st]))
                      + ' ' + not_done(b['state'], p, why_of(p, b))
                      + '</div>')
    cards = homed(r, 'other')
    out += nd
    out.append(cards)
    if not nd and not cards:
        out.append(part('other:none', empty('other changes')))
    out.append('</div></details>')
    return ''.join(out)


CSS = '''
:root{--bg:#fff;--fg:#1d1d1f;--mute:#6b6b70;--line:#e3e3e6;--card:#f6f6f8;--bad:#b42318;--badbg:#fef3f2;--ok:#067647;--okbg:#ecfdf3;--warn:#b54708;--warnbg:#fffaeb}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#151517;--fg:#ececee;--mute:#a0a0a8;--line:#2e2e33;--card:#1e1e22;--bad:#ff8a80;--badbg:#2a1715;--ok:#6ce9a6;--okbg:#10241a;--warn:#fdb022;--warnbg:#2a210f}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,"Segoe UI",sans-serif;margin:0}
main{max-width:980px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:0}h3{font-size:15px;margin:16px 0 4px}h4{margin:12px 0 4px;font-size:14px}
.sub0{color:var(--mute);margin:0 0 6px}
.disc{font-size:13px;color:var(--mute);border-left:3px solid var(--line);padding-left:10px;margin:10px 0 18px}
.banner{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:10px 14px;margin:10px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:14px 0 22px}
.tile{background:var(--card);border-radius:10px;padding:12px}
.tile b{display:block;font-size:26px}.tile span{font-size:13px;color:var(--mute)}
.tile-note{font-size:12px;color:var(--mute);margin-top:4px}.tile-note span{font-size:12px}.tile-note b{display:inline;font-size:12px}
.side{margin:6px 0}.side ul{margin:2px 0}
.tile.nm span[data-t]{display:block;font-weight:600;color:var(--warn);font-size:15px}.tile.nm>b{font-size:13px;font-weight:normal;color:var(--mute)}
.tile.bad b{color:var(--bad)}.tile.ok b{color:var(--ok)}.tile.warn b{color:var(--warn)}
details.sec{border:1px solid var(--line);border-radius:10px;margin:12px 0}
details.sec>summary{padding:12px 14px;cursor:pointer;list-style:none;display:flex;gap:10px;align-items:baseline}
details.sec>summary::before{content:"\\25B8";color:var(--mute)}details.sec[open]>summary::before{content:"\\25BE"}
.secb{padding:0 14px 14px}
details.grp{border-top:1px solid var(--line);padding:8px 0}details.grp>summary{cursor:pointer}
details.sub{padding:4px 0}details.sub>summary{cursor:pointer;font-size:14px}
.loc{font-weight:600}.enc{font-size:12px;color:var(--mute)}.what{color:var(--bad);font-size:14px;margin-left:6px}
.chars{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0}.ch{display:inline-flex;align-items:baseline;gap:6px;background:var(--card);border-radius:6px;padding:2px 8px}
.chars.named .ch{flex-basis:100%;background:none;padding:2px 0}.g{font-size:20px;min-width:1.2em;text-align:center}
.nm{font-size:12px;color:var(--mute)}.more{font-size:13px;color:var(--mute)}
.gap{background:var(--warnbg);border-radius:8px;padding:8px 12px;margin-top:10px;font-size:14px}.gap ul{margin:4px 0 0;padding-left:18px}.gap b{color:var(--warn)}
ul.pairs{columns:2 240px;padding-left:18px}
details.card{border-top:1px solid var(--line);padding:8px 0}details.card>summary{cursor:pointer}
.nd-card{border-top:1px solid var(--line);padding:8px 0}
.nd{background:var(--warnbg);border-radius:8px;padding:6px 10px;margin:6px 0;font-size:14px}.nd b{color:var(--warn)}
.nd ul{margin:4px 0 0;padding-left:18px}
.hint{font-size:13px;color:var(--mute);margin:4px 0}.none{color:var(--mute);font-size:14px}
ul.names{columns:3 180px;padding-left:18px;margin:4px 0}ul.plain{padding-left:18px}
code{font:12px ui-monospace,Menlo,monospace;color:var(--mute)}
table{border-collapse:collapse;font-size:13px;margin:6px 0;width:100%}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
.badge{font-size:12px;padding:1px 8px;border-radius:10px}.badge.ok{background:var(--okbg);color:var(--ok)}.badge.warn{background:var(--warnbg);color:var(--warn)}
pre{white-space:pre-wrap;font:12px/1.45 ui-monospace,Menlo,monospace;background:var(--card);padding:10px;border-radius:8px;overflow-x:auto}
'''


def render(r):
    pair, run = r['pair'], r['run']
    head = [
        '<h1 data-part="h1">What changes from '
        f'{mark("pair/old", pair["old"])} to '
        f'{mark("pair/new", pair["new"])}</h1>'
    ]
    c = r['step11'].get('counts', {})
    if r['step11']['state'] == 'reported' and c.get('state') == 'reported':
        m = c['machines']
        head.append(
            '<p class="sub0" data-f="sub measured" data-part="sub">'
            + label(
                'measured old',
                'Old machine measured:',
                ' '
                + mark('step11/counts/machines/old/label', m['old']['label'])
                + ', '
                + mark(
                    'step11/counts/machines/old/locales', m['old']['locales']
                )
                + ' locales',
                'span',
            )
            + '; '
            + label(
                'measured new',
                'new machine measured:',
                ' '
                + mark('step11/counts/machines/new/label', m['new']['label'])
                + ', '
                + mark(
                    'step11/counts/machines/new/locales', m['new']['locales']
                )
                + ' locales',
                'span',
            )
            + '.</p>'
        )
    elif run['machines']['state'] == 'given':
        head.append(part('sub',
            fixed(
                'sub unreported',
                "The two machines' files were given; what they measured is "
                'not reported (see below).',
                'p',
                'sub0',
            ))
        )
    else:
        head.append(
            '<p class="sub0" data-f="sub sources" data-part="sub">Compared as '
            + mark('run/word', run['word'])
            + " from their glibc sources; no machine's files given.</p>"
        )
    if run['nothing_compared'] != 'none':
        head.append(
            '<div class="banner" data-part="nothing">'
            + fixed('nothing', 'Nothing was compared', 'b')
            + ': the two sides are one '
            + mark('run/nothing_compared', run['nothing_compared'])
            + '.</div>'
        )
    head.append(part('disc',
        fixed(
            'disc',
            'This report is a complement to testing on your '
            'own servers, not a guarantee. What it could not verify '
            'is listed under &ldquo;Not verified&rdquo;.',
            'p',
            'disc',
        ))
    )
    nv = not_verified(r)
    body = [
        tiles(r),
        added_removed(r),
        changed_order(r),
        sources(r),
        other_changes(r),
        '<details class="sec" open><summary><h2>Not verified</h2></summary>'
        '<div class="secb">'
        + part('nv:hint', fixed(
            'nv',
            'What this run could not check. None of it means '
            '&ldquo;no change&rdquo;.',
            'p',
            'hint',
        ))
        + (''.join(nv_html(r, e) for e in nv) if nv else
           part('nv:none', empty('not verified')))
        + '</div></details>',
        compared(r),
        confirm(r),
    ]
    title = f'{pair["old"]} to {pair["new"]}'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title><style>{CSS}</style></head><body><main>'
        + ''.join(head)
        + ''.join(body)
        + '</main></body></html>'
    )


def main(argv):
    if len(argv) != 1 or not argv[0].endswith('.json'):
        print('usage: report_html.py REPORT_JSON', file=sys.stderr)
        return 2
    path = argv[0]
    out = path[: -len('.json')] + '.html'
    part = out + '.partial'
    try:
        page = render(report_data.load(path))
        with open(part, 'w', encoding='utf-8') as f:
            f.write(page)
        os.replace(part, out)
    except (OSError, ValueError, report_data.ReportError) as e:
        if os.path.exists(part):
            os.remove(part)
        print(f'error: cannot write {out}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
