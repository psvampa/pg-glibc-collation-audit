#!/usr/bin/env python3
"""
Steps 6 to 8's comparison of the character sets a machine builds its locales
with, and of the collating elements they build out of transliterations
(backlog 13.7): collation_keys, the check step 2 makes between two tags, over
a machine's own /usr/share/i18n/charmaps/ and locale sources. Steps 6 and 7
compare a machine with the tag its glibc is built from, step 8 the two
machines.

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
changed charmap, is left out here.

Stated, not checked: each machine's localedef reads its files as the
localedef of its tag does. True of RHEL 8, 9 and 10, whose reader files
(linereader.c, locfile.c, ld-ctype.c, repertoire.c) are their tags' after
the specs' patches; RHEL 9 lacks b15538d77c as glibc-2.34 does. So the
comparison asks git about the tag (filter_lc_collate_changes.reads_utf8), as
step 2 does.

What this does not do: compare a copied folder (--locales-dir alone), which
holds no charmaps and no measurement. Those runs keep their `!!` saying the
character sets are not compared.
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
import filter_lc_collate_changes as f
import glibc_locale_data as g

# Where glibc-locale-source installs them on RHEL, for the printed lines.
MACHINE_DIR = '/usr/share/i18n/charmaps/'

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
        files = certain.get(codeset, [])
        maybe = sorted(c for c, names in loose.items() if codeset in names)
        if len(files) != 1 or maybe:
            unmapped.append((name, (
                f"its encoding {codeset} is declared by "
                f"{', '.join(files + maybe)}") if files or maybe else
                f"no charmap on the machine declares its encoding {codeset}"))
            continue
        source = g.locale_source(aliases.get(name, name))
        by_build[(source, files[0])].append(name)
    builds = [ck.Build(_entry(names, source), source, charmap)
              for (source, charmap), names in sorted(by_build.items())]
    return Measured(builds, unmapped, len(locales))


def read_texts(root, names, by_bytes):
    """{name: text} for the locale files under `root`, decoded as
    read_blobs_at decodes a blob (g.decoded). Files with equal bytes, here or
    on the other side, get the one text object `by_bytes` keeps:
    collation_keys passes over a file whose two texts are one object (as
    step 2's shared texts are), and reads it otherwise."""
    out = {}
    for name in names:
        with open(os.path.join(root, name), 'rb') as fh:
            raw = fh.read()
        text = by_bytes.get(raw)
        if text is None:
            text = by_bytes[raw] = g.decoded(raw)
        out[name] = text
    return out


class _Texts(dict):
    """A Side's charmap texts, read when first asked for."""

    def __init__(self, load):
        super().__init__()
        self._load = load

    def get(self, name, default=None):
        if name not in self:
            text = self._load(name)
            if text is None:
                return default
            self[name] = text
        return self[name]


def _read_more(sides):
    """compare's read_more: the charmaps it names, read now, so that its
    0xFF gate sees them (collation_keys._ff_gate); every locale file is
    read already."""
    def read_more(charmaps, locales=False):
        for side in sides:
            for name in sorted(charmaps.get(side.tag, ())):
                if name in side.charmap_oids:
                    side.charmap_texts.get(name)
    return read_more


def _labels(old, new):
    """Two names for two Sides; collation_keys keys its plan by them."""
    return (old, new) if old != new else (f"{old} (old)", f"{new} (new)")


Result = collections.namedtuple('Result', 'report old new measured')
Result.__doc__ = """report: the collation_keys Report, or None with
not_run; old, new: the Sides' names; measured: [Measured] per machine."""


def against_tag(repo, tag, tag_root, tag_names, root, names, build_id,
                charmaps_dir, measurement, listed, step):
    """Steps 6 and 7: the machine's builds compared with themselves at
    `tag`, the tag's locale files and charmaps against the machine's.
    Unusable when the machine's files cannot be compared.

    A locale file the tag lacks (C on RHEL 8 and 9, en_US@ampm on RHEL 8) is
    read from the machine on both sides. No tag diff reads it, which the
    step says above it, and what is asked here is whether the machine's
    charmaps change what its rules resolve to: the same rules against the
    tag's charmap and against the machine's answer that, where leaving the
    build out would answer nothing."""
    cms = CharmapsDir(charmaps_dir)
    tag_oids = _tag_charmaps(repo, tag)
    m = measured_builds(measurement, build_id, cms,
                        g.locale_aliases(repo, tag), tag, tag_oids)
    # A build in a charmap the tag lacks is the distro's own: no tag diff
    # reads what it does to the characters the build's rules name.
    absent = [b for b in m.builds if b.charmap not in tag_oids]
    builds = [b for b in m.builds if b.charmap in tag_oids]
    by_bytes = {}
    old_label, new_label = _labels(tag, build_id)
    used = sorted({b.charmap for b in builds})
    oids = {c: cms.oid(c) for c in used}
    differ = [c for c in used if oids[c] != tag_oids[c]]
    paths = [f'{ck.CHARMAPS_DIR}/{c}' for c in differ]
    (got, missing), = g.read_blobs_at(repo, [(tag, paths)])
    g.require_read(tag, paths, missing, 'the character set check')
    tag_texts = {p.rsplit('/', 1)[1]: t for p, t in got.items()}

    def tag_charmap(c):
        # A charmap with the machine's bytes is the machine's text, one
        # object on both Sides.
        if c not in tag_oids:
            return None
        if c in tag_texts:
            return tag_texts[c]
        if tag_oids[c] == oids.get(c):
            return new.charmap_texts.get(c)
        p = f'{ck.CHARMAPS_DIR}/{c}'
        (one, gone), = g.read_blobs_at(repo, [(tag, [p])])
        g.require_read(tag, [p], gone, 'the character set check')
        return one[p]
    node_texts = read_texts(root, names, by_bytes)
    tag_texts_all = read_texts(tag_root, tag_names, by_bytes)
    for n in names:
        tag_texts_all.setdefault(n, node_texts[n])
    old = ck.Side(old_label, set(tag_texts_all), tag_texts_all, tag_oids,
                  _Texts(tag_charmap))
    new = ck.Side(new_label, set(names), node_texts, oids, _Texts(cms.text))
    old.ask_utf8 = new.ask_utf8 = _asked_once(repo, tag, step)
    report = ck.compare(old, new, builds, builds, lambda: listed,
                        _read_more((old, new)), search_unbuilt=False)
    report = _with_absent(report, absent, tag, listed)
    return Result(report, old_label, new_label, [m])


def _tag_charmaps(repo, tag):
    """{charmap: blob id} at `tag`, from one `git ls-tree`."""
    _, ids = g.list_tree_ids(repo, tag, [ck.CHARMAPS_DIR + '/'])
    return f._named_under(ids, ck.CHARMAPS_DIR)


def _asked_once(repo, tag, step):
    """Side.ask_utf8 for the Sides read with `tag`'s localedef: git asked
    once, whichever Side asks first."""
    box = []

    def ask():
        if not box:
            box.append(f.reads_utf8(repo, tag, step))
        return box[0]
    return ask


def _with_absent(report, absent, tag, listed):
    if not absent:
        return report
    extra = [ck.Found(b.source, b.entry, (b.charmap, b.charmap), set(), [],
                      f"{tag} has no charmap {b.charmap}") for b in absent]
    return report._replace(
        found=report.found + [x for x in extra if x.source not in listed],
        skipped=report.skipped + [x for x in extra if x.source in listed])


def between_machines(repo, old_tag, new_tag, old_side, new_side,
                     old_charmaps, new_charmaps, old_measurement,
                     new_measurement, listed):
    """Step 8: the builds both machines have, compared machine against
    machine. old_side and new_side are diff_node_locales Sides."""
    cms, meas = [], []
    for side, cdir, mpath, tag in ((old_side, old_charmaps, old_measurement,
                                    old_tag),
                                   (new_side, new_charmaps, new_measurement,
                                    new_tag)):
        cm = CharmapsDir(cdir)
        cms.append(cm)
        meas.append(measured_builds(mpath, side.build_id, cm,
                                    g.locale_aliases(repo, tag), tag,
                                    _tag_charmaps(repo, tag)))
    labels = _labels(old_side.build_id, new_side.build_id)
    by_bytes, oids = {}, []
    for cm, m in zip(cms, meas):
        oids.append({c: cm.oid(c) for c in sorted({b.charmap
                                                    for b in m.builds})})

    def new_text(c):
        # A charmap with the same bytes on both machines is one text
        # object on both Sides.
        if c in oids[0] and oids[0][c] == oids[1].get(c):
            return old.charmap_texts.get(c)
        return cms[1].text(c)
    old = ck.Side(labels[0], set(old_side.names),
                  read_texts(old_side.root, old_side.names, by_bytes),
                  oids[0], _Texts(cms[0].text))
    new = ck.Side(labels[1], set(new_side.names),
                  read_texts(new_side.root, new_side.names, by_bytes),
                  oids[1], _Texts(new_text))
    if old_tag == new_tag:
        old.ask_utf8 = new.ask_utf8 = _asked_once(repo, old_tag, 'step 8')
    else:
        old.ask_utf8 = _asked_once(repo, old_tag, 'step 8')
        new.ask_utf8 = _asked_once(repo, new_tag, 'step 8')
    report = ck.compare(old, new, meas[0].builds, meas[1].builds,
                        lambda: listed, _read_more((old, new)),
                        search_unbuilt=False)
    return Result(report, labels[0], labels[1], meas)


STEP67_WORDS = f.Words(
    subject="the character sets {new} builds its locales with, against "
            "{old}'s",
    label=MACHINE_DIR,
    builds="on this machine",
    identical="are identical on this machine and at {old}",
    between="between this machine and {old}",
    listed_for="listed for the summary",
    lists_anyway="this step lists above",
    none_compared="No locale build on this machine could be compared.",
    not_compared="{n} locale build{s} on this machine {is_are} not "
                 "compared: {its_their} source file is not in the machine's "
                 "locale sources.",
    changed_between="between {old} and this machine",
    at_new="on this machine")

STEP8_WORDS = f.Words(
    subject="the character sets the two machines build their locales with",
    label=MACHINE_DIR,
    builds="on both machines",
    identical="are identical on {old} and {new}",
    between="between {old} and {new}",
    listed_for="listed for the summary",
    lists_anyway="this step lists above",
    none_compared="No locale build on both machines could be compared.",
    not_compared="{n} locale build{s} on both machines {is_are} not "
                 "compared: {its_their} source file is not on both.",
    changed_between="between the two machines",
    at_new="on {new}")


def not_run(reason, words, old, new):
    """The check's NOT RUN, in the step's words."""
    f._charmaps_not_run(reason, f._said(words, 'subject', old, new),
                        words.label, split_words=False)


def print_result(result, words, machines):
    """The check's lines in a step's output: what each machine's names come
    to, then collation_keys' report. `machines` names each measured
    machine, in the order of result.measured."""
    print()
    for name, m in zip(machines, result.measured):
        print(textwrap.fill(
            f"Locale names measured on {name}: {m.names}, built as "
            f"{len(m.builds)} locale build(s) (C and POSIX are glibc's "
            f"built-in locale, built from no charmap).", width=78))
        if m.unmapped:
            f._warn_block(f"{len(m.unmapped)} locale name(s) on {name} could "
                          f"not be traced to the charmap they are built "
                          f"with, so their character set is not compared:",
                          [f"{n}: {why}" for n, why in m.unmapped])
    f.print_charmap_report(result.report, result.old, result.new, words)


_PLAIN_NAME = re.compile(r'[A-Za-z0-9_@.+-]+')


def listed(result):
    """What the summary reads, sorted: the locales the check lists, those
    whose builds it could not compare, and the names it could not trace to a
    build, so that the summary never says there is nothing to add over a
    locale nobody looked at. A name that is not a plain locale name is
    written quoted, as diff_node_locales.shown writes it, so that none
    reads as the list's header. None when the check did not run, or
    compared no build and has nothing to name: its NOT RUN is printed
    above, and the step then writes no list. A machine whose every build
    uses a charmap its tag lacks compares none and names them all."""
    r = result.report
    if r.not_run:
        return None
    # A build not compared is named by its locale, as the builds listed
    # are; a name traced to no build has no locale but itself.
    source = {b.entry: b.source for m in result.measured for b in m.builds}
    names = set(ck.listed_sources(r))
    names |= {source.get(e, e) for e in r.not_compared}
    for m in result.measured:
        names |= {n for n, _ in m.unmapped}
    if not r.builds and not names:
        return None
    return sorted(n if _PLAIN_NAME.fullmatch(n) else repr(n) for n in names)
