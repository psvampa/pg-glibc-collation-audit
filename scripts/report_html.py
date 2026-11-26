#!/usr/bin/env python3
"""The summary HTML report of one run, report.<pair>.html, read from the
run's data file alone (report.<pair>.json, through report_data.load, the
strict reader) and written beside it. The text the run prints is not
changed.

A summary, not a copy of the text: the counts at the top, the locales
added and removed, what the run did not verify, how it compared and how
to confirm in PostgreSQL. So that a check can hold the page to the data
file (tests/_harness.report_page_held):
  - a value of the data file shown in the body is marked with its place
    there (data-p="step11/counts/changed": '/' a key, '#' an index); a
    closed word of the file shown in the page's own words also carries
    the word (data-v); a name a map is keyed by carries the map (data-k);
  - a value the page works out of the file is marked data-d with its
    rule;
  - each group of values sits under a label (data-l, the label's words
    marked data-t), so the words beside a value can be held too;
  - a sentence the page says whole is marked data-f with its name;
  - a list that holds nothing says so where the list would be ("(none)",
    or "(no SQL file was written)"), marked data-e with what the list is;
  - a block the run did not do or could not know shows its state in
    capitals, with its reason where the data file gives one, never a 0 or
    an empty list. (A locale step 2 found new and could not spell is a
    state of that locale, shown in lower case beside its name.)
The page's <title> repeats the two sides unmarked.

usage: report_html.py REPORT_JSON
"""

import html
import os
import sys

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


def not_done(word, path, why_html):
    """A part the run did not do: its state in capitals and its reason."""
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
        f'<details class="card" data-w="{esc(path)}"><summary>'
        f'{mark(path + "/title", w["title"])}</summary>'
        f'<pre data-p="{esc(path)}/text">{esc(body)}</pre></details>'
    )


def placed(r, kind):
    """The cards of one kind of warning, wherever the page places them."""
    return ''.join(card(p, w) for p, w in items(r) if w['kind'] == kind)


def not_verified(r):
    """The entries of the Not verified section: each warnings block not
    reported, step 11 when it did not run or was not reported, and each
    warning of class "not checked"."""
    out = []
    for st in ('step0', 'step1', 'step2', 'step11'):
        b = r['warnings'][st]
        if b['state'] == 'not reported':
            out.append(('block', st, b))
    s11 = r['step11']
    if s11['state'] != 'reported':
        out.append(('step11', None, s11))
    for path, w in items(r):
        if w['class'] == 'not checked':
            out.append(('card', path, w))
    return out


# What each warnings block is called on the page.
BLOCK_WORDS = {
    'step0': "the machines' files",
    'step1': 'the two sides',
    'step2': 'step 2',
    'step11': 'step 11',
}


def nv_html(entry):
    kind, path, x = entry
    if kind == 'card':
        return card(path, x)
    if kind == 'block':
        p = f'warnings/{path}'
        return (
            f'<div class="card nd-card"><b>The warnings of '
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
        '<div class="card nd-card"><b>How each machine sorts every '
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
    return label(
        key,
        words,
        f'<b>{body}</b>',
        cls=('tile ' + cls).strip(),
        after=cls != 'nm',
    )


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
                label(
                    'far',
                    'of them only as far as measured:',
                    mark(p + '/unchanged_as_far_as_measured', far),
                    cls='tile-note',
                )
                if far
                else ''
            ),
            tile(
                'not compared',
                'locales not compared',
                made('not_measured+refused', nc),
                'warn' if nc else '',
            )
            + label(
                'not measured count',
                'not measured:',
                mark(p + '/not_measured', c['not_measured']),
                cls='tile-note',
            )
            + label(
                'refused',
                'refused by glibc:',
                mark(p + '/refused', c['refused']),
                cls='tile-note',
            ),
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
    return label(key, words, body, head='h4')


def added_removed(r):
    out = [
        '<details class="sec" open><summary><h2>Locales added and removed'
        '</h2></summary><div class="secb">'
    ]
    s11 = r['step11']
    out.append('<h3>On the machines (what each machine measured)</h3>')
    if s11['state'] != 'reported':
        out.append(not_done(s11['state'], 'step11', why_of('step11', s11)))
        out.append(placed(r, 'not_on_new_machine'))
    else:
        out.append(
            verdict_list(r, 'only old', 'only old', 'Not on the new machine')
        )
        out.append(placed(r, 'not_on_new_machine'))
        out.append(
            verdict_list(r, 'only new', 'only new', 'Only on the new machine')
        )
    s2 = r['step2']
    rm, ru = s2['removed'], s2['rules']
    out.append(
        '<h3>Locale source files between the '
        + mark('run/word', r['run']['word'])
        + '</h3>'
    )
    new_at = mark('pair/new', r['pair']['new'])
    if rm['state'] != 'reported':
        out.append(
            label(
                'gone',
                'Gone at ' + new_at,
                not_done(
                    rm['state'], 'step2/removed', why_of('step2/removed', rm)
                ),
                head='h4',
            )
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
            body = empty('step2/removed/rows')
        words = SOURCE_WORDS[rm['source']].format(
            old=r['run']['old_class'],
            new=r['run']['new_class'],
            word=r['run']['word'],
        )
        body += (
            '<p class="hint" data-p="step2/removed/source" '
            f'data-v="{esc(rm["source"])}">{esc(words)}</p>'
        )
        out.append(label('gone', 'Gone at ' + new_at, body, head='h4'))
    if ru['state'] != 'reported':
        out.append(
            label(
                'new',
                'New at ' + new_at,
                not_done(
                    ru['state'], 'step2/rules', why_of('step2/rules', ru)
                ),
                head='h4',
            )
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
            body = empty('step2/rules/added/rows')
        if ru['added']['state'] == 'notice':
            body += (
                '<p class="hint" data-p="step2/rules/added/state" '
                'data-v="notice">The old side is an upstream '
                + mark('run/old_class', r['run']['old_class'])
                + ': a file it does not have does not prove the locale '
                'was absent on the old system (see Not verified).</p>'
            )
        out.append(label('new', 'New at ' + new_at, body, head='h4'))
    out.append('</div></details>')
    return ''.join(out)


def machines(r, c):
    m = r['run']['machines']
    if m['state'] != 'given':
        return not_done(m['state'], 'run/machines', why_of('run/machines', m))
    if c['note']['state'] == 'fallback':
        return not_done(
            'fallback',
            'compared/note',
            "&mdash; a machine's file made both sides read their upstream "
            'tags; why is under Not verified',
        )
    if not c['machines']:
        return empty('compared/machines')
    out = []
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
        out.append(
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
            )
        )
    return ''.join(out)


def compared(r):
    c = r['compared']
    out = [
        '<details class="sec"><summary><h2>How this was compared</h2>'
        '</summary><div class="secb">'
    ]
    if c['state'] != 'reported':
        out.append(not_done(c['state'], 'compared', why_of('compared', c)))
        out.append('</div></details>')
        return ''.join(out)
    out.append('<h4>Machines</h4>' + machines(r, c))
    b = c['builds']
    if b['state'] == 'rebuilt':
        li = []
        for i, x in enumerate(b['entries']):
            p = f'compared/builds/entries#{i}'
            li.append(
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
                )
            )
        out.append(
            '<h4>Builds rebuilt from their packages</h4><ul class="plain">'
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
            li.append(
                label(
                    'latest',
                    mark(p + '/name', x['name']) + ' =',
                    ' '
                    + mark(p + '/build', x['build'])
                    + ', the newest glibc build in '
                    + mark(p + '/folder', x['folder'])
                    + when,
                    'li',
                )
            )
        out.append(
            '<h4>latest-elN</h4><ul class="plain">' + ''.join(li) + '</ul>'
        )
    elif la['state'] == 'not reported':
        out.append(
            '<h4>latest-elN</h4>'
            + not_done(
                la['state'], 'compared/latest', why_of('compared/latest', la)
            )
        )
    li = []
    for i, t in enumerate(c['tags']):
        p = f'compared/tags#{i}'
        li.append(
            label(
                'tag',
                mark(p + '/ref', t['ref']) + ' =',
                ' <code>' + mark(p + '/commit', t['commit']) + '</code> '
                f'<span class="badge {"ok" if t["status"] == "good" else "warn"}"'
                f' data-p="{p}/status" data-v="{esc(t["status"])}">'
                + mark(p + '/detail', t['detail'])
                + '</span>',
                'li',
            )
        )
    out.append(
        '<h4>Upstream tags</h4><ul class="plain">' + ''.join(li) + '</ul>'
    )
    cv = c['collation_version']
    out.append('<h4>Collation version PostgreSQL records</h4>')
    q = 'compared/collation_version'
    if cv['state'] == 'differ':
        out.append(
            '<p>'
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
            '<p>'
            + label(
                'cv same',
                'both sides:',
                ' ' + mark(q + '/version', cv['version']),
                'span',
            )
            + '.</p>'
        )
    else:
        out.append(
            not_done(
                cv['state'],
                q,
                '&mdash; no glibc version for '
                + ', '.join(
                    mark(f'{q}/sides#{i}', s)
                    for i, s in enumerate(cv['sides'])
                ),
            )
        )
    out.append(placed(r, 'collation_version_same'))
    out.append('</div></details>')
    return ''.join(out)


def confirm(r):
    s11 = r['step11']
    out = [
        '<details class="sec"><summary><h2>Confirm in PostgreSQL</h2>'
        '</summary><div class="secb">'
    ]
    if s11['state'] != 'reported':
        out.append(not_done(s11['state'], 'step11', why_of('step11', s11)))
        out.append(
            '<p class="hint">The SQL files that confirm a change are '
            "written from the machines' measurements.</p>"
        )
    elif not s11['sql_files']:
        out.append(empty('step11/sql_files', '(no SQL file was written)'))
    else:
        out.append(
            '<p>Run each file unchanged on both servers, in a database of '
            'its encoding, then compare the two outputs:</p>'
            + fixed(
                'psql',
                'psql -X -d DATABASE -f FILE &gt; old.out 2&gt;&amp;1   '
                '# and new.out on the new server\npython3 '
                'scripts/confirm_sql.py read FILE old.out new.out',
                'pre',
            )
            + '<ul class="plain">'
        )
        for i, x in enumerate(s11['sql_files']):
            p = f'step11/sql_files#{i}'
            out.append(
                label(
                    'sql',
                    mark(p + '/encoding', x['encoding']) + ' database:',
                    ' <code>' + mark(p + '/file', x['file']) + '</code>',
                    'li',
                )
            )
        out.append('</ul>')
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
        f'<h1>What changes from {mark("pair/old", pair["old"])} to '
        f'{mark("pair/new", pair["new"])}</h1>'
    ]
    c = r['step11'].get('counts', {})
    if r['step11']['state'] == 'reported' and c.get('state') == 'reported':
        m = c['machines']
        head.append(
            '<p class="sub0" data-f="sub measured">'
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
        head.append(
            fixed(
                'sub unreported',
                "The two machines' files were given; what they measured is "
                'not reported (see below).',
                'p',
                'sub0',
            )
        )
    else:
        head.append(
            '<p class="sub0" data-f="sub sources">Compared as '
            + mark('run/word', run['word'])
            + " from their glibc sources; no machine's files given.</p>"
        )
    if run['nothing_compared'] != 'none':
        head.append(
            '<div class="banner">'
            + fixed('nothing', 'Nothing was compared', 'b')
            + ': the two sides are one '
            + mark('run/nothing_compared', run['nothing_compared'])
            + '.</div>'
        )
    head.append(
        fixed(
            'disc',
            'This report is a complement to testing on your '
            'own servers, not a guarantee. What it could not verify '
            'is listed under &ldquo;Not verified&rdquo;.',
            'p',
            'disc',
        )
    )
    nv = not_verified(r)
    body = [
        tiles(r),
        added_removed(r),
        '<details class="sec" open><summary><h2>Not verified</h2></summary>'
        '<div class="secb">'
        + fixed(
            'nv',
            'What this run could not check. None of it means '
            '&ldquo;no change&rdquo;.',
            'p',
            'hint',
        )
        + (''.join(nv_html(e) for e in nv) if nv else empty('not verified'))
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
