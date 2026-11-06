#!/usr/bin/env python3
"""
Confirm in PostgreSQL what step 11 measured: write one SQL file per
database encoding, and read what the two servers answered.

Step 11 (locale_order.py --compare) finds, for each locale whose sort order
changed, pairs of strings the two machines' glibc sort the other way round.
`locale_order.py --compare ... --sql-to PREFIX` hands those pairs here, and
write() turns them into PREFIX.<ENCODING>.sql, one file per database
encoding (UTF8, LATIN1, LATIN9...), each holding the pairs a database of that
encoding can store, with the bytes step 11 measured.

The same file runs unchanged on both servers, in a database of its
encoding, and only reads (SELECT, one SET for the session, and psql
commands):

    psql -X -d DATABASE -f FILE > old.out 2>&1     (on the old server)
    psql -X -d DATABASE -f FILE > new.out 2>&1     (on the new server)

Each row says whether the server answered like the OLD machine, like the
NEW machine or like NEITHER machine; a collation that cannot be used (not
there, or there without its locale, as on a replica built from another
machine) prints CANNOT BE USED and the run goes on. Then:

    python3 confirm_sql.py read FILE old.out new.out

reads the two outputs against the SQL file, never against what the outputs
say they expected, and refuses -- naming why -- unless every test of the
file is answered exactly once in each output, both outputs come from this
file, every collation could be used, psql gave no error outside a
collation's own check, no row answered like neither machine, the old output
answers every test like the old machine and the new one like the new
machine, and, where PostgreSQL reports a glibc version, it is that of each
machine's file; where it reports none, that is said. It repeats
PostgreSQL's warnings without refusing. It never says that
anything is unchanged: what the file does not test is listed in it, and
repeated.
"""
import argparse
import hashlib
import re
import sys

FORMAT = 'pg-glibc-audit confirmation/1'
# The file's id is the sha256 of its text with the id written as this.
PLACEHOLDER = '0' * 64
# A name is written into the SQL only when it needs no quoting beyond the
# double quotes of an identifier and the single quotes of a literal or of
# psql's \echo, which reads a backslash as an escape.
SAFE_NAME = re.compile(r'[A-Za-z0-9_.@=+,-]+')
WORDS = {'both': 'measured on both machines',
         'old': 'measured on the old machine, the new one by the characters '
                'between',
         'new': 'measured on the new machine, the old one by the characters '
                'between'}
LIKE = {'old': 'like the OLD machine', 'new': 'like the NEW machine',
        None: 'like NEITHER machine'}
SETTINGS = ('lc_messages', 'lc_monetary', 'lc_numeric', 'lc_time')
# glibc's rule for the encoding part of a locale name (_nl_normalize_codeset,
# intl/l10nflist.c): letters in lower case, digits kept, anything else
# dropped, and "iso" in front of a part of digits only; what a database
# stores (en_US.UTF-8) and what `locale -a` prints (en_US.utf8) then match.
NORM = ("CASE WHEN {x} ~ '^[^@.]*[.]' THEN substring({x} from '^[^@.]*') "
        "|| '.' || (SELECT CASE WHEN c ~ '^[0-9]+$' THEN 'iso' || c ELSE c "
        "END FROM (SELECT lower(regexp_replace(substring({x} from "
        "'^[^@.]*[.]([^@]*)'), '[^A-Za-z0-9]', '', 'g')) AS c) AS s) || "
        "coalesce(substring({x} from '@.*$'), '') ELSE {x} END")


def die(msg):
    sys.stderr.write(f'confirm_sql.py: {msg}\n')
    sys.exit(2)


def literal(text):
    """text as a SQL string literal; only ASCII is ever written."""
    if not text.isascii():
        raise ValueError(f'not ASCII: {text!r}')
    return "'" + text.replace("'", "''") + "'"


def echo(text):
    """A psql \\echo of text, which holds no quote or backslash."""
    if not text.isascii() or "'" in text or '\\' in text:
        raise ValueError(f'cannot echo {text!r}')
    return f"\\echo '{text}'"


def comment(text):
    """A SQL comment of text, kept ASCII: what a report line names (a
    character, a locale name) can be anything."""
    return '-- ' + text.replace('\n', ' ').encode(
        'ascii', 'backslashreplace').decode('ascii')


# --------------------------------------------------------------- write ----

def sql_text(encoding, old, new, groups, not_checked=(), elsewhere=(),
             gone=()):
    """(text, id, tests) of the SQL file for a database of encoding.

    old, new: {'label': how step 11 names the machine, 'version': the
    glibc version PostgreSQL reports there ('2.28'), or None}.
    groups: [{'locales': names measured alike, 'collations': the names a
    database of this encoding knows them by, 'tests': [(a, b, old sign,
    new sign, 'both'|'old'|'new')], 'untested': [(count or None, why)],
    'unstorable': pairs whose strings this encoding cannot store}].
    not_checked: [(locales, why)] of changed locales no file tests.
    elsewhere: the other encodings a file was written for.
    gone: the locale names of the old machine the new one lacks.
    A test pair whose two machines agree is a bug of the caller: it
    raises, since a row that cannot answer unlike one machine proves
    nothing."""
    out = []
    w = out.append
    tests = 0
    w(comment(f'{FORMAT}: step 11 of audit.sh, confirmed in PostgreSQL'))
    w(comment(f'id: {PLACEHOLDER}'))
    w(comment(f'encoding: {encoding}'))
    for side, d in (('old', old), ('new', new)):
        w(comment(f'{side}: {d["label"]}|{d["version"] or "unknown"}'))
    w(comment(f'Run this file unchanged on both servers, in a database of '
              f'encoding {encoding}; it only reads:'))
    w(comment('  psql -X -d DATABASE -f FILE > old.out 2>&1   (the old '
              'server)'))
    w(comment('  psql -X -d DATABASE -f FILE > new.out 2>&1   (the new '
              'server)'))
    w(comment('then read the two outputs, not by diff:'))
    w(comment('  python3 scripts/confirm_sql.py read FILE old.out new.out'))
    for g in groups:
        for count, why in g['untested']:
            w(comment('not tested: ' + ', '.join(g['locales']) + ': '
                      + (f'{count:,} ' if count is not None else '') + why))
        if g['unstorable']:
            w(comment('not tested: ' + ', '.join(g['locales'])
                      + f': {g["unstorable"]:,} pair(s) whose strings a '
                      f'{encoding} database cannot store'))
    for locales, why in not_checked:
        w(comment('not checked in PostgreSQL by this file: '
                  + ', '.join(locales) + ': ' + why))
    if elsewhere:
        w(comment('the locales of other encodings are in the files written '
                  'beside this one: ' + ', '.join(elsewhere)))
    w(comment('not tested: character rules (LC_CTYPE)'))
    w('\\set QUIET on')
    # Each message with its SQLSTATE, which the reader goes by: the words
    # around it are translated, by the client's LANG (DETALLE, LINEA) and
    # by the server's lc_messages (FEHLER).
    w('\\set VERBOSITY verbose')
    w('\\set ON_ERROR_STOP on')
    w('\\set ECHO none')
    w('\\pset pager off')
    w('\\pset format unaligned')
    w('\\pset tuples_only on')
    w('\\pset footer off')
    w('\\pset expanded off')
    w('\\timing off')
    w('SET client_min_messages = warning;')
    w(echo(f'pg-glibc-audit confirmation|{PLACEHOLDER}|{encoding}'))
    w("SELECT 'database encoding|' || getdatabaseencoding();")
    w("SELECT 'server|' || current_setting('server_version');")
    # In a database of another encoding a collation of the same name can be
    # another locale (sv_FI is ISO-8859-1 in LATIN1): nothing is compared.
    w(f"SELECT getdatabaseencoding() = {literal(encoding)} AS encoding_ok "
      "\\gset")
    w('\\if :encoding_ok')
    w('\\else')
    w('\\quit')
    w('\\endif')
    number = 0
    seen = set()
    for g in groups:
        rows = []
        for a, b, so, sn, measured in g['tests']:
            if so == sn or so not in '<>' or sn not in '<>':
                raise ValueError(f'a test pair the two machines do not answer '
                                 f'apart: {so} {sn}')
            number += 1
            rows.append(f"({number}, '{a.hex()}', '{b.hex()}', '{so}', "
                        f"'{sn}', '{measured}')")
        if not rows:
            continue
        for name in g['collations']:
            if name in seen or not SAFE_NAME.fullmatch(name):
                raise ValueError(f'collation name {name!r} twice or unsafe')
            seen.add(name)
            var = f'c{len(seen)}'
            ident = '"' + name + '"'
            w(comment(f'collation {name}: ' + ', '.join(g['locales'])))
            w(f'\\unset {var}')
            w('\\set ON_ERROR_STOP off')
            w(f"SELECT ('a' < 'b' COLLATE {ident}) IS NOT NULL AS {var} \\gset")
            w('\\set ON_ERROR_STOP on')
            w(f'\\if :{{?{var}}}')
            w(f"SELECT 'collation|{name}|usable|glibc ' || coalesce((SELECT "
              f"pg_collation_actual_version(c.oid) FROM pg_collation c WHERE "
              f"c.collname = {literal(name)} AND c.collencoding IN (-1, "
              f"(SELECT encoding FROM pg_database WHERE datname = "
              f"current_database())) AND pg_collation_is_visible(c.oid)), "
              f"'NULL');")
            w(f"SELECT 'test|' || id || '|{name}|' || r || '|old ' || o || "
              f"', new ' || n || '|' || CASE m WHEN 'both' THEN "
              f"{literal(WORDS['both'])} WHEN 'old' THEN "
              f"{literal(WORDS['old'])} ELSE {literal(WORDS['new'])} END || "
              f"'|' || CASE WHEN r = o THEN {literal(LIKE['old'])} WHEN r = n "
              f"THEN {literal(LIKE['new'])} ELSE {literal(LIKE[None])} END")
            # COLLATE on each comparison: carried out of a subquery it
            # becomes implicit, and conflicts with b's default.
            w(f"FROM (SELECT id, o, n, m, CASE WHEN a < b COLLATE {ident} THEN "
              f"'<' WHEN a > b COLLATE {ident} THEN '>' ELSE '=' END AS r")
            w('      FROM (VALUES')
            w(',\n'.join(rows))
            w('      ) AS v(id, ah, bh, o, n, m)')
            w(f"      CROSS JOIN LATERAL (SELECT convert_from(decode(ah, "
              f"'hex'), {literal(encoding)}) AS a, convert_from(decode(bh, "
              f"'hex'), "
              f"{literal(encoding)}) AS b) AS s) AS t")
            w('ORDER BY id;')
            w('\\else')
            w(echo(f'collation|{name}|CANNOT BE USED'))
            w('\\endif')
            tests += len(rows)
    if gone:
        names = ', '.join(f'({literal(x)})' for x in gone)
        w(comment('the databases, libc collations and lc_* settings that use '
                  'a locale name of the old machine the new one lacks: a '
                  'list, not a test'))
        w("SELECT DISTINCT 'gone|' || u.kind || '|' || u.obj || '|' || "
          "u.value FROM (")
        w("  SELECT 'database', datname, datcollate FROM pg_database")
        w("  UNION ALL SELECT 'database', datname, datctype FROM pg_database")
        w("  UNION ALL SELECT 'collation', collname, collcollate FROM "
          "pg_collation WHERE collprovider = 'c'")
        w("  UNION ALL SELECT 'collation', collname, collctype FROM "
          "pg_collation WHERE collprovider = 'c'")
        w("  UNION ALL SELECT 'setting', name, setting FROM pg_settings WHERE "
          "name IN (" + ', '.join(literal(x) for x in SETTINGS) + ")")
        w(f') AS u(kind, obj, value) JOIN (VALUES {names}) AS g(name)')
        w(f'  ON {NORM.format(x="u.value")} = {NORM.format(x="g.name")}')
        w('ORDER BY 1;')
    w(echo(f'end|{PLACEHOLDER}|{tests} tests'))
    text = '\n'.join(out) + '\n'
    ident = hashlib.sha256(text.encode('ascii')).hexdigest()
    return text.replace(PLACEHOLDER, ident), ident, tests


# ---------------------------------------------------------------- read ----

class Sql:
    """What a file sql_text() wrote says: its id, encoding, the two
    machines, every collation with the line of its check and its tests'
    expected signs, and what it does not test."""

    def __init__(self, path):
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError as e:
            die(f'cannot read {path}: {e}')
        try:
            text = raw.decode('ascii')
        except UnicodeDecodeError:
            die(f'{path} is not a file confirm_sql.py wrote: it is not ASCII')
        lines = text.split('\n')
        m = re.fullmatch(r'-- id: ([0-9a-f]{64})', lines[1] if len(lines) > 1
                         else '')
        if not lines[0].startswith('-- ' + FORMAT) or not m:
            die(f'{path} is not a file confirm_sql.py wrote')
        self.ident = m.group(1)
        if hashlib.sha256(text.replace(self.ident, PLACEHOLDER)
                          .encode('ascii')).hexdigest() != self.ident:
            die(f'{path} is not the file confirm_sql.py wrote: its text does '
                'not match its id (cut short or edited)')
        self.path = path
        self.encoding = None
        self.machines = {}
        self.notes = []
        self.collations = {}    # name: (line of its check, {id: (old, new)})
        self.end = None
        current = None
        for number, line in enumerate(lines, 1):
            m = re.fullmatch(r'-- encoding: (\w+)', line)
            if m:
                self.encoding = m.group(1)
                continue
            m = re.fullmatch(r'-- (old|new): (.*)\|(.*)', line)
            if m:
                self.machines[m.group(1)] = (
                    m.group(2), None if m.group(3) == 'unknown' else m.group(3))
                continue
            if line.startswith(('-- not tested: ', '-- not checked in ',
                                '-- the locales of other encodings')):
                self.notes.append(line[3:])
                continue
            m = re.fullmatch(r"SELECT \('a' < 'b' COLLATE \"(.+)\"\) IS NOT "
                             r"NULL AS c\d+ \\gset", line)
            if m:
                current = {}
                self.collations[m.group(1)] = (number, current)
                continue
            m = re.fullmatch(r"\((\d+), '[0-9a-f]+', '[0-9a-f]+', '([<>])', "
                             r"'([<>])', '(both|old|new)'\),?", line)
            if m:
                current[int(m.group(1))] = (m.group(2), m.group(3))
                continue
            m = re.fullmatch(r"\\echo 'end\|[0-9a-f]{64}\|(\d+) tests'", line)
            if m:
                self.end = int(m.group(1))
        tests = sum(len(t) for line_, t in self.collations.values())
        if (self.encoding is None or set(self.machines) != {'old', 'new'}
                or self.end != tests):
            die(f'{path} is not a whole file confirm_sql.py wrote')
        self.tests = tests
        self.checks = {line_: name
                       for name, (line_, t) in self.collations.items()}


# A message of the server, as psql prints it under VERBOSITY verbose: the
# file and line it ran, a severity in the server's language, the SQLSTATE,
# the text. Class 01 is a warning (00 a notice); every other, an error.
MESSAGE = re.compile(r'(?:psql:(.*?):(\d+): )?([^\s:]+):  ([0-9A-Z]{5}): (.*)')
RECORDS = ('pg-glibc-audit confirmation', 'database encoding', 'server',
           'collation', 'test', 'gone', 'end')


class Out:
    """One server's output of an Sql: what it answered, and every reason it
    cannot be read as an answer to that file (problems)."""

    def __init__(self, path, sql, side):
        self.path, self.side = path, side
        self.problems = []
        self.warnings = []
        self.server = None
        self.encoding = None
        self.collations = {}     # name: ('usable', version) or ('cannot',)
        self.errors = {}         # name: [lines of its check's error]
        self.answers = {}        # (name, id): result
        self.gone = []
        self.before = []         # what psql printed before the file ran
        self.headers = 0
        self.ends = 0
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError as e:
            self.problems.append(f'cannot be read: {e}')
            return
        if not raw:
            self.problems.append('is empty')
            return
        text = raw.decode('utf-8', errors='replace')
        lines = text.split('\n')
        if lines and lines[-1] == '':
            lines.pop()
        message = None          # the message the lines under it belong to
        unknown = []
        for line in lines:
            fields = line.split('|')
            head = fields[0]
            if len(fields) > 1 and head in RECORDS:
                message = None
            else:
                m = MESSAGE.fullmatch(line)
                if m:
                    message = [line]
                    number, state = m.group(2), m.group(4)
                    if state[:2] in ('00', '01'):
                        self.warnings.append(message)
                    elif number is not None and int(number) in sql.checks:
                        self.errors.setdefault(sql.checks[int(number)],
                                               []).append(message)
                    else:
                        self.problems.append(
                            f'psql reported an error outside the check of a '
                            f'collation: {line}')
                    continue
                if message is not None and not line.startswith('psql:'):
                    # DETAIL, HINT, LINE and its caret, LOCATION, in any
                    # language: under the message, never a line of psql's
                    # own, which starts with its name.
                    message.append(line)
                    continue
                message = None
                if self.headers == 0 and not line.startswith('psql:'):
                    # Before the file's first line: what psql says on
                    # connecting (a database's collation version, PostgreSQL
                    # 15 on). Nothing of the file has run.
                    self.before.append(line)
                    continue
            if head == 'pg-glibc-audit confirmation' and len(fields) == 3:
                self.headers += 1
                if fields[1] != sql.ident:
                    self.problems.append(
                        f'is the output of another SQL file (id '
                        f'{fields[1][:12]}..., not {sql.ident[:12]}... of '
                        f'{sql.path})')
            elif head == 'database encoding' and len(fields) == 2:
                self.encoding = fields[1]
            elif head == 'server' and len(fields) == 2:
                self.server = fields[1]
            elif head == 'collation' and len(fields) in (3, 4):
                name = fields[1]
                if name in self.collations:
                    self.problems.append(f'collation {name} is reported twice')
                if len(fields) == 3 and fields[2] == 'CANNOT BE USED':
                    self.collations[name] = ('cannot',)
                elif (len(fields) == 4 and fields[2] == 'usable'
                      and fields[3].startswith('glibc ')):
                    version = fields[3][len('glibc '):]
                    self.collations[name] = ('usable',
                                             None if version == 'NULL'
                                             else version)
                else:
                    unknown.append(line)
            elif head == 'test' and len(fields) == 7 and fields[1].isdigit() \
                    and fields[3] in ('<', '>', '='):
                key = (fields[2], int(fields[1]))
                if key in self.answers:
                    self.problems.append(f'test {key[1]} of collation '
                                         f'{key[0]} is answered twice')
                self.answers[key] = fields[3]
            elif head == 'gone' and len(fields) == 4:
                self.gone.append(f'{fields[1]} {fields[2]}: {fields[3]}')
            elif head == 'end' and len(fields) == 3:
                self.ends += 1
                if fields[1] != sql.ident:
                    self.problems.append('ends as the output of another '
                                         'SQL file')
            else:
                unknown.append(line)
        for line in unknown[:5]:
            self.problems.append(f'holds a line this SQL file does not print: '
                                 f'{line[:120]}')
        if len(unknown) > 5:
            self.problems.append(f'and {len(unknown) - 5} more such line(s)')
        if self.headers == 0:
            self.problems.append(
                f'is not an output of {sql.path}: it lacks the line that '
                'names the file (did psql connect? run with -X and -f FILE)')
        elif self.headers > 1:
            self.problems.append('holds the output of more than one run')
        if self.headers and self.encoding != sql.encoding:
            # The file stops there: what it did not get to is that.
            self.problems = [p for p in self.problems
                             if p.startswith('is the output of another')] + [
                f'comes from a database of encoding '
                f'{self.encoding or "unknown"}; this file is for a '
                f'{sql.encoding} database, and stops in any other']
            return
        if self.headers == 1 and self.ends == 0:
            self.problems.append('is cut short: it lacks the last line')
        for name, (line_, tests) in sql.collations.items():
            got = self.collations.get(name)
            if got is None:
                if self.headers:
                    self.problems.append(f'says nothing of collation {name}')
                continue
            if got[0] == 'cannot':
                why = [' / '.join(x) for x in self.errors.get(name, [])]
                self.problems.append(
                    f'collation {name} CANNOT BE USED'
                    + (': ' + '; '.join(why) if why else ''))
                continue
            if name in self.errors:
                self.problems.append(
                    f'collation {name} is reported usable after an error in '
                    'its check: ' + ' / '.join(self.errors[name][0]))
            missing = [i for i in tests if (name, i) not in self.answers]
            if missing:
                self.problems.append(
                    f'collation {name}: test(s) {ranges(missing)} missing')
        extra = [k for k in self.answers
                 if k[0] not in sql.collations or k[1] not in
                 sql.collations[k[0]][1]]
        for name in sorted(set(self.collations) - set(sql.collations)):
            self.problems.append(f'reports collation {name}, which this file '
                                 'does not check')
        if extra:
            self.problems.append(f'answers {len(extra)} test(s) this file does '
                                 'not hold')

    def likeness(self, sql):
        """{('old'|'new'|None): [(name, id)]}: whom each answer is like."""
        out = {'old': [], 'new': [], None: []}
        for name, (line_, tests) in sql.collations.items():
            for i, (o, n) in tests.items():
                r = self.answers.get((name, i))
                if r is None:
                    continue
                out['old' if r == o else 'new' if r == n else None].append(
                    (name, i))
        return out


def ranges(ids):
    """'4120 to 6493, 7000': a sorted list of numbers as runs."""
    ids = sorted(ids)
    runs, start = [], ids[0]
    for a, b in zip(ids, ids[1:] + [None]):
        if b != a + 1:
            runs.append(str(start) if start == a else f'{start} to {a}')
            start = b
    return ', '.join(runs)


def read(sql_path, old_path, new_path):
    """Read the old and new servers' outputs of sql_path; print what they
    confirm, or every reason they confirm nothing. 0 when confirmed, 1
    when refused."""
    sql = Sql(sql_path)
    outs = [Out(old_path, sql, 'old'), Out(new_path, sql, 'new')]
    problems = []
    for out in outs:
        problems += [f'{out.path}: {p}' for p in out.problems]
    notes = []
    if all(not out.problems for out in outs):
        likes = [out.likeness(sql) for out in outs]
        for out, like in zip(outs, likes):
            if like[None]:
                problems.append(
                    f'{out.path}: {len(like[None])} test(s) answer like '
                    f'NEITHER machine, first test {like[None][0][1]} of '
                    f'collation {like[None][0][0]}')
        (old_like, new_like), total = likes, sql.tests
        if len(old_like['new']) == total and len(new_like['old']) == total:
            problems.append(f'{old_path} answers like the NEW machine and '
                            f'{new_path} like the OLD one: the two outputs '
                            'are given the other way round')
        elif len(old_like['old']) == total and len(new_like['old']) == total:
            problems.append('both outputs answer like the OLD machine: '
                            f'{new_path} does not come from the new server')
        elif len(old_like['new']) == total and len(new_like['new']) == total:
            problems.append('both outputs answer like the NEW machine: '
                            f'{old_path} does not come from the old server')
        else:
            for out, like in zip(outs, likes):
                if len(like[out.side]) != total:
                    other = 'new' if out.side == 'old' else 'old'
                    problems.append(
                        f'{out.path}: {len(like[out.side])} of {total} '
                        f'test(s) answer like the {out.side.upper()} machine '
                        f'and {len(like[other])} like the {other.upper()} '
                        'machine: not every test answers like its machine')
        for out in outs:
            label, wanted = sql.machines[out.side]
            for name, got in sorted(out.collations.items()):
                if got[0] != 'usable':
                    continue
                if got[1] is None:
                    notes.append(f'{out.path}: PostgreSQL gives no glibc '
                                 f'version for collation {name} (it gives none '
                                 'for C.* and POSIX, nor before PostgreSQL '
                                 '13), so its version is not checked')
                elif wanted is None:
                    notes.append(f'{out.path}: collation {name} reports glibc '
                                 f'{got[1]}; the {out.side} machine\'s version '
                                 'is not known, so it is not checked')
                elif got[1] != wanted:
                    problems.append(
                        f'{out.path}: collation {name} reports glibc {got[1]}, '
                        f'and the {out.side} machine\'s file was measured on '
                        f'{wanted} ({label})')
        if sql.machines['old'][1] == sql.machines['new'][1]:
            notes.append('Both machines run glibc '
                         f'{sql.machines["old"][1] or "of an unknown version"}:'
                         ' two builds of one version cannot be told apart '
                         'from SQL, so which server ran which build is not '
                         'checked.')
    print(f'SQL file: {sql.path} ({sql.encoding} database; '
          f'{sql.machines["old"][0]} -> {sql.machines["new"][0]})')
    for out in outs:
        if out.server:
            print(f'{out.side.upper()} output: {out.path}, PostgreSQL '
                  f'{out.server}, database encoding {out.encoding}')
    if problems:
        print(f'REFUSED: these outputs do not confirm {sql.path}:')
        for p in problems:
            print(f'  {p}')
        code = 1
    else:
        print(f'Confirmed in PostgreSQL: {sql.tests} of {sql.tests} tests '
              f'answer like the old machine in {old_path} and like the new '
              f'one in {new_path}.')
        code = 0
    for out in outs:
        if out.before:
            print(f'!! {out.path}: psql printed before the file ran: '
                  + ' / '.join(out.before))
        seen = set()
        for message in out.warnings:
            key = tuple(message)
            if key in seen:
                continue
            seen.add(key)
            print(f'!! {out.path}: PostgreSQL warned: ' + ' / '.join(message))
    for note in notes:
        print(f'!! {note}')
    for out in outs:
        if out.gone:
            print(f'!! {out.path}: uses a locale name the new machine does not '
                  'have:')
            for item in out.gone:
                print(f'     {item}')
    print('!! This file confirms the changes step 11 found a test pair for; '
          'it does not say that anything else is unchanged. Not tested by '
          'it:')
    for note in sql.notes:
        print(f'     {note}')
    return code


def main():
    ap = argparse.ArgumentParser(
        description='Read two servers\' outputs of a SQL file that '
                    'locale_order.py --compare --sql-to wrote.')
    sub = ap.add_subparsers(dest='command', required=True)
    r = sub.add_parser('read', help='read the old and the new server\'s '
                                    'outputs of SQL')
    r.add_argument('sql', metavar='SQL')
    r.add_argument('old', metavar='OLD.out')
    r.add_argument('new', metavar='NEW.out')
    args = ap.parse_args()
    return read(args.sql, args.old, args.new)


if __name__ == '__main__':
    sys.exit(main())
