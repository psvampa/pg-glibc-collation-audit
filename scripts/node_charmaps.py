#!/usr/bin/env python3
"""
Reading a machine's own /usr/share/i18n/charmaps/ and its measurement, for
two steps. Step 2, given two machines' own files, takes from measured_builds
which locales each machine builds with which charmap (in place of the tag's
SUPPORTED), and reads the charmaps of its machine commits through
CharmapsDir; it also prints the character set check's report (backlog 13.7)
with the functions below. Step 11 reads the same folder through CharmapsDir
to check the strings it measured against it (locale_order.charmap_check).

A distro can change a charmap without changing a locale file: Red Hat gives
RHEL 8 and RHEL 9 the GB18030 of glibc 2.39, and RHEL 8 seven IBM charmaps of
2.34 (glibc-RHEL-67806.patch, glibc-rh1961109.patch). localedef resolves
every character an LC_COLLATE block names through the charmap, so the tags
alone cannot see such a change. The machine's copy is what its locales were
built from: glibc.spec builds them in the patched tree with localedata's
`make install-locales` or `install-locale-files` (el8 :2109-2110, el9
:1911-1912, el10 :1448-1449), which runs localedef with `-f
charmaps/<charset>`, and glibc-locale-source installs that tree's charmaps
(%files locale-source). Applying the specs' patches reproduces the installed
files byte for byte (13.7 handoff, PR D).

Which builds a machine has comes from its measurement (locale_order.py),
traced from each locale name its `locale -a` lists, never from SUPPORTED:

  - the charmap is the file whose <code_set_name> is the codeset measured
    under that name (nl_langinfo(CODESET)). localedef stores the charmap's
    code_set_name as the locale's codeset (ld-ctype.c:445@2.28), or the
    charmap's file name when it declares none (charmap.c:200-203@2.28). A
    codeset no file declares, or that two files could declare, is not
    mapped, and the name is printed under `!!` with the reason. A charmap
    whose prolog this does not read whole, and that is not its tag's own
    file, stops the check (NOT RUN): which codeset glibc stores from it is
    not known;
  - the source is locale_source of the name, after intl/locale.alias at the
    tag the machine's glibc is built from: the archive adds every alias whose
    locale it holds (locarchive.c:1195-1300@2.28), and RHEL builds with
    `--alias-file=../intl/locale.alias`. Measured on RHEL 8, 9 and 10: of
    their 867, 869 and 885 names, every one but C and POSIX maps to the build
    a simulation of the archive from the specs gives it (13.7 handoff, PR D);
  - C and POSIX are glibc's built-in locale, which `locale -a` always lists
    (locale.c:432-435@2.28) and no charmap builds.

A locale the machine does not build is not there for PostgreSQL to use: it
imports collations from `locale -a` (collationcmds.c:868@REL_18_6), and
CREATE DATABASE refuses a locale that does not exist (dbcommands.c:1067). So
point 5 of collation_keys, the search for locales a distro could build in a
changed charmap, is left out for machines.

Stated, not checked: each machine's localedef reads its files as the
localedef of its tag does. True of RHEL 8, 9 and 10, whose reader files
(linereader.c, locfile.c, ld-ctype.c, repertoire.c) are their tags' after
the specs' patches; RHEL 9 lacks b15538d77c as glibc-2.34 does. So the
comparison asks git about the tag (reads_utf8, below), as step 2 does for
two tags.
"""
import bz2
import collections
import gzip
import hashlib
import json
import os
import re
import textwrap
import zlib

import collation_keys as ck
import glibc_locale_data as g


# glibc's built-in locale: `locale -a` always lists both (locale.c:432-435@2.28)
# and setlocale gives the built-in one for either name (findlocale.c:133-138).
BUILTIN = ('C', 'POSIX')

# A charmap's name as a file name: what the step can open without a path
# separator or a name the shell or a list line would split.
_SAFE = re.compile(r'[A-Za-z0-9_.,:+-]+')
# The escape characters _loose_names tries: the default and every printable
# ASCII character. `<escape_char>` takes any one-character word
# (charmap.c:443-462@2.28), control and 8-bit bytes too, which this does not
# try; a loose file is read only when it is its tag's own (measured_builds).
_ESCAPES = ['\\'] + [chr(c) for c in range(0x21, 0x7f) if chr(c) != '\\']
_CHARMAP_LINE = re.compile(r'^[ \t\r\f\v]*CHARMAP[ \t\r\f\v]*$', re.M)


class Unusable(Exception):
    """The machine's files cannot be compared at all: the check is NOT
    RUN, with this as the reason."""


class CharmapsDir:
    """A machine's charmaps folder, as --unpack lays it out: NAME, NAME.gz or
    NAME.bz2, one of them per charmap, as charmap_open looks for them
    (charmap-dir.c@2.28). Anything else -- a folder, a link, two forms of
    one name, a name this does not open -- is Unusable: the table of
    codesets below has to hold every file, or a build could be mapped to
    another file that declares its codeset."""

    def __init__(self, root):
        self.root = root
        try:
            entries = sorted(os.listdir(root))
        except OSError as e:
            raise Unusable(f"{root} cannot be listed: {e}")
        self.files = {}
        odd = []
        for entry in entries:
            path = os.path.join(root, entry)
            name = entry
            for ext in ('.gz', '.bz2'):
                if entry.endswith(ext):
                    name = entry[:-len(ext)]
            if os.path.islink(path) or not os.path.isfile(path):
                odd.append(f"{entry} is not a file")
            elif not _SAFE.fullmatch(name):
                odd.append(f"{entry!r} is not a charmap name this reads")
            elif name in self.files:
                odd.append(f"{self.files[name]} and {entry} are one charmap")
            else:
                self.files[name] = entry
        if odd:
            raise Unusable(f"{root} holds what this does not read as a "
                           f"charmap: {'; '.join(odd[:3])}"
                           f"{' and more' if len(odd) > 3 else ''}")
        if not self.files:
            raise Unusable(f"{root} holds no charmap")
        self._raw = {}

    def _bytes(self, name):
        try:
            with open(os.path.join(self.root, self.files[name]), 'rb') as fh:
                return fh.read()
        except OSError as e:
            raise Unusable(f"{self.files[name]} cannot be read: {e}")

    def raw(self, name):
        """The charmap's bytes, uncompressed."""
        got = self._raw.get(name)
        if got is None:
            data, entry = self._bytes(name), self.files[name]
            try:
                if entry.endswith('.gz'):
                    data = gzip.decompress(data)
                elif entry.endswith('.bz2'):
                    data = bz2.decompress(data)
            except (OSError, EOFError, ValueError, zlib.error) as e:
                raise Unusable(f"{entry} cannot be uncompressed: {e}")
            got = self._raw[name] = data
        return got

    def _head(self, name):
        """The text up to its first CHARMAP line, or all of it: what
        parse_charmap reads as the prolog (state 1, charmap.c:310-480@2.28).
        Only the start of a compressed file is uncompressed when the line is
        there."""
        entry = self.files[name]
        if entry.endswith('.gz') and name not in self._raw:
            data = self._bytes(name)
            try:
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                text = d.decompress(data, 16384).decode('utf-8', 'replace')
            except zlib.error as e:
                raise Unusable(f"{entry} cannot be uncompressed: {e}")
            m = _CHARMAP_LINE.search(text)
            if m and m.end() < len(text):
                return text[:m.end()]
        text = self.raw(name).decode('utf-8', 'replace')
        m = _CHARMAP_LINE.search(text)
        return text[:m.end()] if m else text

    def oid(self, name):
        """git's blob id of the uncompressed charmap, which a tag's ls-tree
        gives for its own."""
        data = self.raw(name)
        return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()

    def text(self, name):
        return g.decoded(self.raw(name)) if name in self.files else None

    def codesets(self):
        """({codeset: [charmap]} for the files whose prolog read_prolog reads
        whole, {charmap: {codesets}} for the others.

        A prolog read whole gives the codeset glibc stores: its
        <code_set_name>, or the file's name when it has none. A prolog it
        does not read whole -- such as EBCDIC-PT, with no CHARMAP line, or
        MAC-CENTRALEUROPE, with a `<comment>` line glibc skips as an
        invalid definition and goes on -- is "loose": its candidates are
        what _loose_names makes of it and the file's own name, all kept, so
        that a build is not mapped while one of those files could have
        built it. measured_builds accepts a loose file only when it is the
        tag's own."""
        certain, loose = collections.defaultdict(list), {}
        for name in self.files:
            head = self._head(name)
            try:
                seen = ck.read_prolog(head, name)[0]
            except ck.Unsettled:
                text = self.raw(name).decode('utf-8', 'replace')
                loose[name] = _loose_names(text) | {name}
                continue
            certain[seen.get('code_set_name', name)].append(name)
        return certain, loose


def _loose_names(text):
    """The names glibc 2.28's reader could store from a `<code_set_name>`
    in `text`. Not a whole model of that reader: round 3 of the review found
    five forms it misses (a comment on a continued line, a string ending in
    the escape, an escape outside _ESCAPES, a NUL, 0x1A or 0xFF ending the
    word, a line of only the escape). So measured_builds uses it only for a
    file that is its tag's own, and for every such file at 2.12..2.42 --
    EBCDIC-PT, MAC-CENTRALEUROPE and NF_Z_62-010_1973 -- the names it gives
    hold the one glibc stores (measured, 13.7 handoff). The whole file is
    searched: a line ending in the
    escape character swallows the CHARMAP line after it (lr_ignore_rest,
    linereader.c:128-136@2.28), and the prolog goes on. For every escape
    character the file could set, lines ending in it are joined (lr_next,
    :162-170), a `<...>` name is read keeping the character after each
    escape (get_symname, :478-488), and the argument after
    `<code_set_name>` is read as a word (get_ident, :552-563: up to a blank,
    `"`, `;`, `<` or `,`, keeping the character after each escape) or as a
    string, and as written, up to a blank or `"`."""
    out = set()
    for e in _ESCAPES:
        t = text.replace(e + '\n', '')
        at = t.find('<')
        while at >= 0:
            # Only a name that reads as starting with c can be the keyword:
            # c itself, or the escape and then c.
            if t[at + 1:at + 2] == 'c' or t[at + 1:at + 3] == e + 'c':
                sym, i = _read_name(t, at + 1, e, '>')
                if sym == 'code_set_name' and i < len(t) and t[i] == '>':
                    out |= _argument(t, i + 1, e)
            at = t.find('<', at + 1)
    return {n for n in out if n}


def _read_name(t, i, e, stops):
    """(the text from i up to a newline or a character in `stops`, with the
    character after each escape kept and the escape dropped, the index it
    stopped at)."""
    buf = []
    while i < len(t) and t[i] != '\n' and t[i] not in stops:
        if t[i] == e and i + 1 < len(t) and t[i + 1] != '\n':
            buf.append(t[i + 1])
            i += 2
        else:
            buf.append(t[i])
            i += 1
    return ''.join(buf), i


def _argument(t, i, e):
    """What a `<code_set_name>` ending at t[i] could take as its
    argument."""
    while i < len(t) and t[i] in ' \t\r\f\v':
        i += 1
    if i < len(t) and t[i] == '"':
        word, _ = _read_name(t, i + 1, e, '"')
        end = t.find('"', i + 1)
        return {word, t[i + 1:end] if end > 0 else ''}
    word, _ = _read_name(t, i, e, ' \t\r\f\v";<,')
    return {word, re.match(r'[^\s"]*', t[i:]).group(0)}


Measured = collections.namedtuple('Measured', 'builds unmapped names')
Measured.__doc__ = """builds: [collation_keys.Build], one per source and
charmap, named by the first of its names in the order of `_entry`;
unmapped: [(name, reason)] for the names traced to no build; names: how
many names the measurement lists, C and POSIX included."""


def _entry(names, source):
    """The name a build is printed under: the one spelled after its source
    (sv_SE rather than swedish), with no codeset if there is one (the
    SUPPORTED entry's own name), then the shortest."""
    own = [n for n in names if g.locale_source(n) == source] or names
    return min(own, key=lambda n: (n != source, len(n), n))


def charmap_of(certain, loose, codeset):
    """(the charmap a locale of CODESET was built from, []) or (None, the
    files that could declare it: none, or several), from codesets()'s two
    tables. Exactly one file whose prolog is read whole declares it, and
    no loose file could: localedef stores the charmap's code_set_name as
    the locale's codeset (ld-ctype.c:445@2.28), so with two candidates
    the file is not known (IBM1162 declares IBM1133, and the two
    differ)."""
    files = certain.get(codeset, [])
    maybe = sorted(c for c, names in loose.items() if codeset in names)
    if len(files) == 1 and not maybe:
        return files[0], []
    return None, files + maybe


def measured_builds(path, build_id, charmaps, aliases, tag, tag_ids):
    """Measured for the machine whose measurement is at `path`, whose glibc
    is built from `tag`; `tag_ids` are that tag's charmaps' blob ids.

    The machine must hold every charmap of the tag, or the check is
    Unusable. A charmap whose prolog read_prolog does not read whole
    ("loose") is read by _loose_names only when it is the tag's own file.
    One that is not --
    added or changed on the machine -- is Unusable: what codeset glibc
    stores from it is not known, so no build can be traced to its charmap
    with certainty. Rounds 2 and 3 of the review each found spellings a
    model of glibc's reader missed (13.7 handoff, PR D); reading the whole
    reader as glibc does is backlog 13.19."""
    try:
        with open(path, encoding='ascii') as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise Unusable(f"the measurement {path} does not read: {e}")
    locales = data.get('locales') if isinstance(data, dict) else None
    if not isinstance(locales, dict) or not locales or not all(
            isinstance(r, dict) for r in locales.values()):
        raise Unusable(f"the measurement {path} lists no locale")
    if data.get('glibc_build') != build_id:
        raise Unusable(f"the measurement {path} was taken on "
                       f"{data.get('glibc_build')!r}, not on {build_id}")
    # A charmap of the tag the machine lacks: a build made with it, before it
    # went, would be traced to another file that declares its codeset, or
    # to none (round 4 of the review: IBM1133 removed, its builds traced to
    # IBM1162, which declares the same name).
    gone = sorted(set(tag_ids) - set(charmaps.files))
    if gone:
        raise Unusable(
            f"The machine lacks {len(gone)} of {tag}'s charmaps "
            f"({', '.join(gone[:3])}{' and others' if len(gone) > 3 else ''}"
            f"), and a build made with one could be traced to another file "
            f"that declares its codeset")
    certain, loose = charmaps.codesets()
    odd = sorted(charmaps.files[c] for c in loose
                 if tag_ids.get(c) != charmaps.oid(c))
    if odd:
        raise Unusable(
            f"{', '.join(odd)} on the machine {'is' if len(odd) == 1 else 'are'}"
            f" not {tag}'s and could not be read whole for the codeset "
            f"{'it declares' if len(odd) == 1 else 'they declare'}, so no "
            f"build can be traced to its charmap with certainty")
    by_build, unmapped = collections.defaultdict(list), []
    for name in sorted(locales):
        if name in BUILTIN:
            continue
        codeset = locales[name].get('codeset')
        if not isinstance(codeset, str) or not codeset:
            unmapped.append((name, "its encoding was not measured"))
            continue
        charmap, files = charmap_of(certain, loose, codeset)
        if charmap is None:
            unmapped.append((name, (
                f"its encoding {codeset} is declared by "
                f"{', '.join(files)}") if files else
                f"no charmap on the machine declares its encoding {codeset}"))
            continue
        source = g.locale_source(aliases.get(name, name))
        by_build[(source, charmap)].append(name)
    builds = [ck.Build(_entry(names, source), source, charmap)
              for (source, charmap), names in sorted(by_build.items())]
    return Measured(builds, unmapped, len(locales))


# -- the character set check's report, printed by step 2 -------------------

def print_characters(chars, indent='      ', width=78,
                     label='characters in the changed rules'):
    """`W (U+0057)  w (U+0077)`, wrapped, never splitting one character's
    entry. A character a terminal would not show -- a control character, a
    space -- is printed as its code point alone."""
    entries = [f'U+{c:X}' if isinstance(c, int)
               else f'{c} (U+{ord(c):04X})' if c.isprintable()
               and not c.isspace() else f'U+{ord(c):04X}' for c in chars]
    line = f'{indent}{label} ({len(chars)}):'
    sep = ' '
    for entry in entries:
        if len(line) + len(sep) + len(entry) > width:
            print(line)
            line = indent + entry
        else:
            line += sep + entry
        sep = '  '
    print(line)


def _named_under(ids, directory):
    """{file name: blob id} for the files directly under `directory` in a
    list_tree_ids map. A quoted path (a name git escapes) is left out: it
    cannot be read by that spelling, and a run that needs it finds it
    missing and says so (collation_keys.Side.closure)."""
    prefix = directory + '/'
    return {p[len(prefix):]: i for p, i in ids.items()
            if p.startswith(prefix) and '/' not in p[len(prefix):]}


def reads_utf8(repo, tag):
    """Does `tag` hold collation_keys.UTF8_READER: True, False, or None when
    git cannot place it. A shallow clone can answer no, or nothing, for a
    tag that holds it, and _readers_differ would read two such sides as one
    reader (round 7 of the 13.7 review): it is refused, as step 5 refused
    it. A yes is a path git found in the history it has, so it stands on
    any clone, and the check (a git process) is not paid for it. Step 2
    asks it of each tag, and of the tag a machine's glibc is built from."""
    got = g.descends_from(repo, ck.UTF8_READER, tag)
    if got is not True:
        g.require_full_history(
            repo, 'step 2', 'git cannot say which tag holds the commit from '
            'which localedef reads its input as UTF-8')
    return got


# What the character set check's lines say about the two sides of step 2.
# Each is formatted with old= and new=, the Sides' names.
Words = collections.namedtuple(
    'Words', 'subject label builds identical between listed_for '
             'lists_anyway none_compared not_compared changed_between at_new')
STEP2_WORDS = Words(
    subject="the character sets locales are built with",
    label="localedata/charmaps/",
    builds="in SUPPORTED at both tags",
    identical="are identical at {old} and {new}",
    between="between {old} and {new}",
    listed_for="listed in the summary",
    lists_anyway="the summary lists anyway",
    none_compared="No locale build is in SUPPORTED at both tags with its "
                  "source file at both, so none was compared.",
    not_compared="{n} SUPPORTED entr{y_ies} at both tags {is_are} not "
                 "compared: {its_their} source file is not at both.",
    changed_between="between the tags",
    at_new="at {new}")


def _said(words, field, old, new, **more):
    return getattr(words, field).format(old=old, new=new, **more)


def _charmaps_not_run(reason, subject=STEP2_WORDS.subject,
                      label=STEP2_WORDS.label):
    """The check's NOT RUN."""
    print()
    print(textwrap.fill(
        f"NOT RUN: the comparison of {subject} ({label}). {reason}. A rule "
        f"that names a character whose bytes changed in its charmap, and a "
        f"collating element built out of a transliteration, are not covered "
        f"by this result.",
        width=78, initial_indent='!! ', subsequent_indent='   '))


def _warn_block(head, items):
    """A `!!` block audit.sh repeats in its summary: the first line opens
    with `!!`, every other with three blanks or more (audit.sh's warnings
    block reads exactly that)."""
    print()
    print(textwrap.fill(head, width=78, initial_indent='!! ',
                        subsequent_indent='   '))
    for item in items:
        print(textwrap.fill(item, width=78, initial_indent='     ',
                            subsequent_indent='       '))


_CHANGED_IN_CHARMAP = 'characters its character set changed'


def _chars_phrase(f):
    via = ', '.join(f.via) if f.via else '?'
    n = len(f.chars)
    return f"{n} character{'s' if n != 1 else ''}, named in {via}"


def _print_chars(f, indent='      '):
    """f's characters, in code point order; one beyond U+10FFFF, which a
    `<U........>` name can spell, as its number."""
    cps = sorted(f.chars)
    print_characters([chr(c) if c < 0x110000 else c for c in cps],
                     indent=indent, label=_CHANGED_IN_CHARMAP)


def print_charmap_report(report, old, new, words=STEP2_WORDS):
    """The character set check's lines in step 2's output."""
    w = lambda field, **more: _said(words, field, old, new, **more)
    if report.not_run:
        _charmaps_not_run(report.not_run, w('subject'), w('label'))
        return
    print()
    changed = report.changed
    if not report.builds:
        _warn_block(f"NOT RUN: the comparison of {w('subject')}, and of the "
                    f"collating elements they build out of "
                    f"transliterations. {w('none_compared')}", [])
    elif not changed:
        print(textwrap.fill(
            f"Character sets ({w('label')}): the "
            f"{len(report.charmaps)} that the {report.builds} locale builds "
            f"{w('builds')} use {w('identical')}.", width=78))
    else:
        def size(p):
            name = p[1] if p[0] == p[1] else f"{p[0]} -> {p[1]}"
            if report.sizes[p] is None:
                return f"{name}: could not be compared (see below)."
            n, added, removed = report.sizes[p]
            other = n - added - removed
            parts = [f"{added} added"] * bool(added) + \
                [f"{removed} removed"] * bool(removed) + \
                [f"{other} given other bytes"] * bool(other)
            if not parts:
                # The file changed and no character's bytes did: a comment,
                # a WIDTH line, the order of two lines.
                return f"{name}: no character changed."
            parts[0] = parts[0].replace(
                ' ', ' character ' if parts[0].startswith('1 ')
                else ' characters ', 1)
            return f"{name}: {', '.join(parts)}."
        n = len(changed)
        print(textwrap.fill(
            f"Character sets ({w('label')}): {n} of the "
            f"{len(report.charmaps)} that the {report.builds} locale builds "
            f"{w('builds')} use {'differs' if n == 1 else 'differ'}"
            f" {w('between')}.", width=78))
        print(textwrap.fill(' '.join(size(p) for p in changed), width=78))
        if not report.found and not report.skipped:
            print(f"No {'compared ' if report.not_compared else ''}build's "
                  f"LC_COLLATE names one of those characters.")
    plain = [f for f in report.found if f.entry and not f.reason]
    if plain:
        print(textwrap.fill(
            f"Locale builds whose LC_COLLATE names one of those characters "
            f"({len(plain)}), {w('listed_for')}:", width=78))
        for f in plain:
            print(f"  {f.entry} ({f.charmaps[1]}): {_chars_phrase(f)}")
            _print_chars(f)
    if report.skipped:
        print(textwrap.fill(
            f"Locale builds {w('lists_anyway')}, whose LC_COLLATE names one "
            f"of those characters or could not be compared "
            f"({len(report.skipped)}):", width=78))
        for f in report.skipped:
            what = f.entry or f"{f.source} (not in SUPPORTED in this charmap)"
            print(f"  {what} ({f.charmaps[1]}): "
                  + (f"could not be compared: {f.reason}" if f.reason
                     else _chars_phrase(f)))
    unsettled = [f for f in report.found if f.reason]
    if unsettled:
        _warn_block(f"{len(unsettled)} locale build(s) could not be compared "
                    f"for what their character set does to the characters "
                    f"their LC_COLLATE names, so they are "
                    f"{w('listed_for')}:",
                    [f"{f.entry or f.source} ({f.charmaps[1]}): {f.reason}"
                     for f in unsettled])
    distro = [f for f in report.found if not f.entry and not f.reason]
    if distro:
        print()
        print(textwrap.fill(
            f"!! {len(distro)} locale(s) that SUPPORTED never builds in a "
            f"charmap that changed name one of the characters it changed. A "
            f"distro can build them in it (Red Hat builds en_US in "
            f"ISO-8859-15), so they are {w('listed_for')}:",
            width=78, subsequent_indent='   '))
        for f in distro:
            print(f"     {f.source} ({f.charmaps[1]}): {_chars_phrase(f)}")
            _print_chars(f, indent='       ')
    if report.unsearched:
        _warn_block("The search for locales a distro can build in a changed "
                    "charmap, which SUPPORTED does not build in it, did not "
                    "run for these charmaps, so no such locale is listed for "
                    "them:", [f"{name}: {why}"
                              for name, why in report.unsearched])
    print_translit_report(report, new, words, old)
    if report.not_compared:
        n = len(report.not_compared)
        one = n == 1
        _warn_block(w('not_compared', n=n, y_ies='y' if one else 'ies',
                      s='' if one else 's', is_are='is' if one else 'are',
                      its_their='its' if one else 'their'),
                    [', '.join(report.not_compared)])


def print_translit_report(report, new, words=STEP2_WORDS, old=None):
    """Point 6 of collation_keys: the collating elements built out of
    transliterations."""
    w = lambda field: _said(words, field, old, new)
    t = report.translit
    if not report.builds:
        return
    if t is None:
        print(textwrap.fill(
            f"Transliteration (LC_CTYPE): no character set and no locale "
            f"file's LC_CTYPE changed {w('changed_between')}, so no "
            f"collating element is built differently.", width=78))
        return
    plain = [f for f in t.found if not f.reason]
    unsettled = [f for f in t.found if f.reason]
    if t.elements:
        n = t.changed if t.changed is not None else sum(
            f.changed for f in plain)
        line = (f"Transliteration (LC_CTYPE): {t.elements} collating "
                f"element{'s' if t.elements != 1 else ''} of {t.builds} "
                f"locale build{'s' if t.builds != 1 else ''} "
                f"depend{'' if t.elements != 1 else 's'} on a "
                f"transliteration; "
                + (f"{n or 'none'} {'is' if n < 2 else 'are'} built "
                   f"differently {w('at_new')}."))
    elif t.unmoved:
        u = t.unmoved
        line = (f"Transliteration (LC_CTYPE): {u} locale build"
                f"{'s' if u != 1 else ''} may build a collating element out "
                f"of a transliteration, and nothing "
                f"{'they read' if u != 1 else 'it reads'} changed "
                f"{w('changed_between')}.")
    elif not unsettled and not report.translit_skipped:
        line = ("Transliteration (LC_CTYPE): no compared locale build "
                "builds a collating element out of a transliteration.")
    elif not unsettled:
        k = report.translit_skipped
        line = (f"Transliteration (LC_CTYPE): the {k} locale build"
                f"{'s' if k != 1 else ''} that may build a collating "
                f"element out of a transliteration "
                f"{'are' if k != 1 else 'is'} listed already, so none was "
                f"compared.")
    else:
        line = None
    if line:
        print(textwrap.fill(line, width=78))
    if plain:
        print(textwrap.fill(
            f"Locale builds whose collating elements are built differently "
            f"({len(plain)}), {w('listed_for')}:", width=78))
        for f in plain:
            print(f"  {f.entry} ({f.charmaps[1]}): {f.changed} "
                  f"element{'s' if f.changed != 1 else ''}, defined in "
                  f"{', '.join(f.via)}")
            print_characters([chr(c) if c < 0x110000 else c
                              for c in sorted(f.chars)],
                             indent='      ', label='transliterated characters')
    if report.translit_skipped and (t.elements or t.unmoved or unsettled):
        n = report.translit_skipped
        print(textwrap.fill(
            f"{n} more locale build{'s' if n != 1 else ''} with collating "
            f"elements built differently, or not compared, "
            f"{'are' if n != 1 else 'is'} listed already.", width=78))
    if unsettled:
        _warn_block(f"{len(unsettled)} locale build(s) could not be compared "
                    f"for the collating elements they build out of "
                    f"transliterations, so they are {w('listed_for')}:",
                    [f"{f.entry} ({f.charmaps[1]}): {f.reason}"
                     for f in unsettled])


def _tag_charmaps(repo, tag):
    """{charmap: blob id} at `tag`, from one `git ls-tree`."""
    _, ids = g.list_tree_ids(repo, tag, [ck.CHARMAPS_DIR + '/'])
    return _named_under(ids, ck.CHARMAPS_DIR)


