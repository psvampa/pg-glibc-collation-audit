"""What localedef reads for every locale of one tree.

A tree is every locale source of one glibc tag, or of one directory copied
from a machine, read for one glibc build. For each locale glibc_reader.run()
emulates `localedef -i <locale>` (reader_cache keeps the answers), and this
module answers from those runs and from nothing else (backlog 15.2):

  - which locales have an LC_COLLATE of their own, and what it copies;
  - which files' LC_COLLATE localedef reads to build each locale, following
    every `copy` as glibc follows it, with the state glibc carries from file
    to file (one run per locale, never a graph pieced together from each
    file read alone: backlog 15.1 measured that shortcut wrong);
  - where a range (.., ..., ....) or codepoint_collation was read (no
    step asks it; the tests hold the reader to localedef with it), and
    which lines of each file steer other lines (step 2, backlog 13.36);
  - which characters each line of each file names, in every run, as far
    as the reader is sure of that run (glibc_reader.Run.named_lines; step
    2, backlog 13.29);
  - which runs this reader could not settle, or that localedef does not
    finish, and why. Such a locale is never reported as clear: the steps
    list it, with `!!`.

Step 2 reads this. The files step 2's check of character sets and
transliterations (collation_keys) reads are still read with its own reader.
"""
import collections
import difflib
import os
import re

import glibc_locale_data as g
import glibc_reader as gr
import reader_cache as rc

# A build id names a RHEL major as `.el8`, `.el9` or `.el10`
# (glibc-2.34-168.el9_6.14). Red Hat's patches add to the keyword table
# (glibc_reader.RHEL_BUILDS); the tables, and whether lr_getc predates
# 2.36, come from the tag the machine is audited against.
_RHEL_RE = re.compile(r'\.el(8|9|10)(?![0-9])')


def _name(b):
    return b.decode('utf-8', 'surrogateescape')


def _key(s):
    return s.encode('utf-8', 'surrogateescape')


def _pre236(repo, tag):
    """Does `tag` predate glibc's lr_getc fix? Refused when git cannot
    say: lr_getc then reads 0x1A and 0xFF one way or the other, and a guess
    is an answer this tool does not have."""
    pre = g.descends_from(repo, g.LR_GETC_FIX, tag)
    if pre is None:
        g.die(f"git cannot tell whether {tag} comes before or after glibc's "
              f"lr_getc fix ({g.LR_GETC_FIX[:12]}), which decides how "
              f"localedef reads the bytes 0x1A and 0xFF; refusing to guess.")
    return not pre


_GPERF = ('locale/programs/locfile-kw.gperf',
          'locale/programs/charmap-kw.gperf')


def builds_at(repo, tags, pre236=None):
    """{tag: Build} for each tag, upstream (no distro patch), their keyword
    tables read in ONE git process: two `git show` per tag cost about 0.1 s
    of every run of step 2, which asks for two tags (backlog 15.2 B3,
    measured on the Mac). `pre236` is {tag: True/False} when the caller has
    already asked git whether each tag predates the lr_getc fix; a tag it
    does not name is asked here, and refused when git cannot say."""
    return _builds(repo, [(t, None) for t in dict.fromkeys(tags)],
                   pre236 or {})


def build_at(repo, tag, build_id=None):
    """The Build glibc at `tag` reads with, plus what Red Hat's patches add
    when `build_id` names a RHEL major. Refuses when git cannot say whether
    `tag` predates glibc's lr_getc fix (_pre236)."""
    return _builds(repo, [(tag, build_id)], {})[tag]


def _builds(repo, specs, pre236):
    pre = {}
    for tag, _ in specs:
        known = pre236.get(tag)
        pre[tag] = known if known is not None else _pre236(repo, tag)
    got = g.read_blobs_at(repo, [(tag, list(_GPERF)) for tag, _ in specs],
                          raw=True)
    out = {}
    for (tag, build_id), (contents, missing) in zip(specs, got):
        if missing:
            g.die(f"cannot read glibc's keyword tables at {tag}: "
                  f"{', '.join(sorted(missing))} could not be read")
        try:
            lk, ck = (gr.Build.parse_gperf(contents[path].decode('ascii'))
                      for path in _GPERF)
        except (gr.ReaderError, UnicodeDecodeError) as exc:
            g.die(f"cannot read glibc's keyword tables at {tag}: {exc}")
        label = tag
        m = _RHEL_RE.search(build_id or '')
        if m:
            # Red Hat's patches add to the keyword table, as Build.at adds
            # extra_locfile.
            for k, v in gr.RHEL_BUILDS['rhel' + m.group(1)][2].items():
                lk[k.encode('ascii') if isinstance(k, str) else k] = v
            label = f'{tag} + rhel{m.group(1)}'
        out[tag] = gr.Build(pre[tag], True, lk, ck, label)
    return out


class Tree(object):
    """The runs of every locale of one tree. Names are str, as the steps
    print them; the files are the bytes glibc reads."""

    def __init__(self, label, files, build, decode, cache_dir=None):
        self.label = label
        self.build = build
        self._files = files
        self._decode = decode
        self._cache_dir = cache_dir
        self._unresolved = None
        self._runs = {_name(k): v for k, v in rc.tree_runs(
            files, build, cache_dir).items()}
        self.names = sorted(self._runs)

    @classmethod
    def at_tag(cls, repo, tag, build=None, cache_dir=None):
        """Every locale file at `tag`, read for glibc at `tag`, or for
        `build` when the caller has it already."""
        paths = g.list_locale_files(repo, tag)
        contents, missing = g.read_blobs(repo, tag, paths, raw=True)
        g.require_read(tag, paths, missing, 'the locale files')
        files = {_key(os.path.basename(p)): v for p, v in contents.items()}
        return cls(tag, files, build or build_at(repo, tag), g.decoded,
                   cache_dir)

    # -- one run ------------------------------------------------------------

    def _collate_sections(self, name):
        """(file, copies) of every LC_COLLATE section run(name) read for
        its content, in reading order; copies are str names, the opening
        copy first."""
        fatal, reads, unsettled, collate, _ = self._runs[name]
        out = []
        for fr in reads:
            for sec in fr[2]:
                category, fname, content, copy, mid = (
                    sec[0], sec[1], sec[4], sec[5], sec[6])
                if category != 'LC_COLLATE' or not content:
                    continue
                cps = []
                if copy is not None and copy[0] is not None:
                    cps.append(_name(copy[0]))
                cps += [_name(n) for n, _ in mid if n is not None]
                out.append((_name(fname), cps))
        return out

    def defines(self, name):
        """Did localedef read an LC_COLLATE section in `name`'s own file
        when building `name`?"""
        return any(f == name for f, _ in self._collate_sections(name))

    def copies(self, name):
        """The `copy` names of `name`'s own LC_COLLATE, as glibc read them
        when building `name`, in order."""
        return [c for f, cps in self._collate_sections(name) if f == name
                for c in cps]

    def roots(self, name, roots):
        """The members of `roots` that building `name` reaches through
        `copy`, not counting those reached only through another member,
        walked over the copies run(name) read rather than over each file
        read alone."""
        if name in roots:
            return []
        edges = {}
        for f, cps in self._collate_sections(name):
            edges.setdefault(f, []).extend(cps)
        seen, stack, hits = set(), list(edges.get(name, ())), set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur in roots:
                hits.add(cur)
                continue
            stack.extend(edges.get(cur, ()))
        return sorted(hits)

    def unresolved(self):
        """{name: why} for every locale whose run localedef does not finish
        or this reader could not settle; for one where localedef stops
        reading a file before its end, which may be before an LC_COLLATE
        (FileRead.cut: the file then reads as having none, and the rest of
        it is not looked at); and for one whose own LC_COLLATE stores
        nothing (a `copy` of a file without LC_COLLATE): never cleared,
        listed with `!!`. The missing files are missing()."""
        if self._unresolved is not None:
            return self._unresolved
        out = {}
        for name in self.names:
            fatal, reads, unsettled, collate, _ = self._runs[name]
            cut = [(fr[0], fr[3]) for fr in reads if fr[3] is not None]
            if fatal is not None:
                kind, what = fatal[0], fatal[1]
                if kind == 'missing':
                    file = '?' if what is None else g.shown(_name(what))
                    out[name] = f"localedef cannot open {file}"
                else:
                    out[name] = f"localedef stops ({kind})"
            elif unsettled:
                f, line, why = unsettled[0]
                out[name] = f"{g.shown(f)}:{line}: {why}"
            elif cut:
                f, line = cut[0]
                out[name] = (f"localedef stops reading "
                             f"{g.shown(_name(f))} at line {line}, before "
                             f"the end of the file")
            elif collate is None and self.defines(name):
                out[name] = "localedef stores no LC_COLLATE for it"
        self._unresolved = out
        return out

    def missing(self):
        """{name: [the file localedef cannot open]} for every run that
        stops there, the locale itself excepted."""
        out = {}
        for name in self.names:
            fatal = self._runs[name][0]
            if fatal is not None and fatal[0] == 'missing' \
                    and fatal[1] is not None and _name(fatal[1]) != name:
                out[name] = [_name(fatal[1])]
        return out

    def _collate(self, name):
        return self._runs[name][3]

    def ranges(self, name):
        """[(line, text)] where `name`'s own file has a range in a
        statement of the LC_COLLATE localedef builds for `name`, ranges
        glibc refuses included."""
        collate = self._collate(name)
        own = [ln for f, ln in (collate[1] if collate else ()) if f == name]
        if not own:
            return []
        lines = self._decode(self._files[_key(name)]).split('\n')
        return [(ln, lines[ln - 1].strip() if 0 < ln <= len(lines) else '')
                for ln in own]

    def byte_order(self):
        """({names that declare byte order}, {name: the locale it copies}).

        glibc builds byte order when the LC_COLLATE it builds holds one
        statement, codepoint_collation, and nothing else (ld-collate.c:
        2691-2693 and collate_finish@2.39); a copy of such a locale, and
        nothing else, shares its data (backlog 6.9). Declared when the
        statement is the locale's own; by copy otherwise. A run that is not
        settled is neither. By copy also needs the locale's own LC_COLLATE
        to be that one `copy` and the copied locale to be byte order itself,
        so the name printed is the one that carries the order."""
        bad = self.unresolved()
        declared, copier = set(), {}
        for name in self.names:
            collate = self._collate(name)
            if name in bad or collate is None:
                continue
            counts, _, codepoint = collate[:3]
            if sum(n for _, n in counts) != 1 or len(codepoint) != 1:
                continue
            if codepoint[0][0] == name:
                declared.add(name)
            elif len(self.copies(name)) == 1:
                copier[name] = self.copies(name)[0]
        by_copy, grew = {}, True
        while grew:
            grew = False
            for name, target in copier.items():
                if name not in by_copy and (target in declared
                                            or target in by_copy):
                    by_copy[name] = target
                    grew = True
        return declared, by_copy


# -- the rules of each locale on two sides -------------------------------------

SAME, DIFFERS, NOT_COMPARED = 'same', 'differs', 'not compared'

# compare_rules' answer for one name.
#
#   state       SAME, DIFFERS or NOT_COMPARED.
#   why         NOT_COMPARED: why, naming the side or sides. DIFFERS: None
#               when `chars` holds the characters, else why they are not
#               known. SAME: None.
#   chars       DIFFERS: the code points the counted lines name (changed,
#               or read otherwise; _changed), sorted, or None when they
#               cannot be told; () when the counted lines name none.
#               SAME: (). NOT_COMPARED: None.
#   every_rule  DIFFERS: True when a counted line steers other lines (an
#               instruction, glibc_reader.Collate.instructions, or the
#               first or last line of an LC_COLLATE section), or a line
#               that did not change
#               is inside LC_COLLATE on one side only: rules then move on
#               lines that did not change, and `chars` are not all the
#               characters that move (backlog 13.36). False otherwise.
#
# No field has a default: a Rules made without one fails where it is made.
Rules = collections.namedtuple('Rules', 'state why chars every_rule')


def compare_rules(old_tree, new_tree, aliases):
    """{name: Rules} for every locale of two sides: every file of either
    tree, and every alias of either side's locale.alias whose locale has a
    file on one side at least.

    `aliases` is (old, new), each side's {alias: locale} as
    glibc_locale_data.locale_aliases reads it (swedish: sv_SE.ISO-8859-1);
    the locale's file is glibc_locale_data.locale_source of it. An alias
    spelled as its own file's name is that file, and is not listed again.
    A dict is required for each side: a locale.alias that could not be
    read is the caller's to refuse, never an empty map, which would drop
    swedish and read as nothing to compare.

    Each side's rules are the statements its localedef run accepted into
    the LC_COLLATE it built, following every `copy` (reader_cache.rules):
    compared without file names or line numbers, so rules that moved to
    another file the locale copies, or a renamed file, are SAME. What is
    checked first, and answered NOT_COMPARED, naming the side:
      - no file for the name on a side, or an alias on one side only, or
        an alias whose locale has no file on that side;
      - a run the reader did not settle on either side (Tree.unresolved:
        localedef does not finish, a place the reader cannot settle, a
        file cut short, an LC_COLLATE that stores nothing). The reader's
        doubt is never read past;
      - an LC_COLLATE stored on one side only: localedef builds rules on
        one side and none on the other.
    Two runs that both store no LC_COLLATE are SAME: neither builds rules.

    DIFFERS names the characters on the lines of the files whose
    LC_COLLATE the run read that changed, or that did not change and the
    two runs read otherwise (_changed): a line of the old file by what the
    old run's reader named on it, and a line of the new file by what the
    new run named (glibc_reader.Run.named_lines, kept as
    reader_cache.char_maps). A file whose LC_COLLATE only one run read
    counts whole, on that side. every_rule says when such a line steers
    others. Not known (chars None, with why) when a counted line is one
    the reader cannot name, a character map or a statement map is not
    kept, or no line counts at all.

    Not compared here: the code that compiles and compares the rules
    (localedef's and strcoll's), and the charmap. glibc_reader reads no
    charmap file and takes each <U....> as named, so a code point one
    side's charmap lacks is not seen here; step 2's check of character
    sets (collation_keys) reads the charmaps."""
    old_al, new_al = aliases
    if not isinstance(old_al, dict) or not isinstance(new_al, dict):
        raise TypeError("compare_rules needs each side's locale.alias as a "
                        "dict")
    sides = ((old_tree, old_al, 'old'), (new_tree, new_al, 'new'))
    out = {}
    for name in sorted(set(old_tree.names) | set(new_tree.names)):
        missing = [w for t, _, w in sides if name not in t._runs]
        if missing:
            out[name] = Rules(NOT_COMPARED, f"no file {g.shown(name)} on the "
                              f"{missing[0]} side", None, False)
        else:
            out[name] = _compare_one(old_tree, name, new_tree, name)
    alias_names = set()
    for tree, al, _ in sides:
        for a, target in al.items():
            src = g.locale_source(target)
            if a != src and a not in out and (src in old_tree._runs
                                              or src in new_tree._runs):
                alias_names.add(a)
    for a in sorted(alias_names):
        got, why = [], None
        for tree, al, side in sides:
            if a not in al:
                why = f"an alias in the {_other(side)} side's locale.alias only"
                break
            src = g.locale_source(al[a])
            if src not in tree._runs:
                why = (f"locale.alias names {g.shown(al[a])}, which has no "
                       f"file on the {side} side")
                break
            got.append(src)
        out[a] = Rules(NOT_COMPARED, why, None, False) if why else \
            _compare_one(old_tree, got[0], new_tree, got[1])
    return out


def _other(side):
    return 'new' if side == 'old' else 'old'


def _compare_one(old_tree, old_name, new_tree, new_name):
    doubt = [(side, t.unresolved().get(n)) for t, n, side in (
        (old_tree, old_name, 'old'), (new_tree, new_name, 'new'))]
    doubt = [f"{side} side: {why}" for side, why in doubt if why is not None]
    if doubt:
        return Rules(NOT_COMPARED, '; '.join(doubt), None, False)
    oc, nc = old_tree._collate(old_name), new_tree._collate(new_name)
    if oc is None and nc is None:
        return Rules(SAME, None, (), False)
    if oc is None or nc is None:
        return Rules(NOT_COMPARED, f"LC_COLLATE stored on the "
                     f"{'new' if oc is None else 'old'} side only", None,
                     False)
    if oc[4] == nc[4]:
        return Rules(SAME, None, (), False)
    chars, every, why = _changed(old_tree, old_name, new_tree, new_name)
    return Rules(DIFFERS, why, chars, every)


def _read_collate(tree, name):
    """({file: [(start, end)]} of the LC_COLLATE sections run(name) read
    for their content, {file: [(first, last)]} of its instructions,
    {file: map sha1})."""
    _, reads, _, collate, named = tree._runs[name]
    bounds = {}
    for fr in reads:
        for sec in fr[2]:
            if sec[0] == 'LC_COLLATE' and sec[4]:
                bounds.setdefault(_name(sec[1]), []).append((sec[2], sec[3]))
    steer = {f: list(zip(flat[0::2], flat[1::2])) for f, flat in collate[3]}
    return bounds, steer, dict(named[0])


def _line_pairs(old_bytes, new_bytes):
    """(old lines, new lines, pairs): the lines of each file that changed,
    numbered from 1, and (old line, new line) of each line that did not.
    A side that is None counts whole and pairs nothing."""
    a = old_bytes.split(b'\n') if old_bytes is not None else []
    b = new_bytes.split(b'\n') if new_bytes is not None else []
    if old_bytes is None or new_bytes is None:
        return range(1, len(a) + 1), range(1, len(b) + 1), []
    old, new, pairs = [], [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal':
            pairs.extend(zip(range(i1 + 1, i2 + 1), range(j1 + 1, j2 + 1)))
        else:
            old.extend(range(i1 + 1, i2 + 1))
            new.extend(range(j1 + 1, j2 + 1))
    return old, new, pairs


class _Side(object):
    """What one run read of each file, line by line: whether a line is in
    an LC_COLLATE section it read, an instruction or a section's edge, and
    what the reader named on it."""

    def __init__(self, tree, name):
        self.tree = tree
        self.name = name
        self.bounds, self.steer, self.maps = _read_collate(tree, name)
        self._got = {}
        self._lines = {}
        self.line_maps = dict(tree._collate(name)[5])

    def statements(self, f):
        """{line: sha1} of the statements glibc accepted in `f` in this
        run, by the line each starts on (reader_cache.statement_map); {}
        for a file where none starts; None when the map is not kept."""
        if f not in self._lines:
            self._lines[f] = rc.statement_map(
                self.line_maps[f], self.tree._cache_dir) \
                if f in self.line_maps else {}
        return self._lines[f]

    def map(self, f):
        """The character map of `f` in this run, or None when not kept."""
        if f not in self._got:
            self._got[f] = rc.char_map(self.maps[f], self.tree._cache_dir) \
                if f in self.maps else None
        return self._got[f]

    def view(self, f, n):
        """(inside, steers, names) of line `n` of `f`: names is the code
        points the reader named there, () for none, None when it cannot
        say or the map is not kept."""
        inside = any(s <= n and (e is None or n <= e)
                     for s, e in self.bounds.get(f, ()))
        if not inside:
            return False, False, ()
        steers = any(a <= n <= b for a, b in self.steer.get(f, ())) or any(
            n in (s, e) for s, e in self.bounds[f])
        got = self.map(f)
        if got is None or ('from' in got and n >= got['from']):
            return True, steers, None
        return True, steers, got.get(n, ())


def _changed(old_tree, old_name, new_tree, new_name):
    """(chars, every_rule, why) of compare_rules' DIFFERS.

    A line counts when its bytes changed, and also when its bytes did not
    and the two runs read it otherwise: inside LC_COLLATE on one side only,
    an instruction or a section's edge on one side only, naming other
    characters, or starting other accepted statements (every token but its
    line, as reader_cache.rules compares them, read from each run's
    statement map). That is the one rule for every way an unchanged line
    is read otherwise (false-negative-reviewer, step 9.1, rounds 1 and 2):
    a keyword one build's table has (codepoint_collation in the RHEL 9
    build and not in glibc-2.34), a byte lr_getc reads otherwise across
    glibc 2.36, an escape_char or comment_char line above LC_COLLATE that
    changed. Each counted line's characters are named by the side that
    reads it; a counted line that steers others, or an unchanged line
    inside LC_COLLATE on one side only, sets every_rule."""
    sides = (_Side(old_tree, old_name), _Side(new_tree, new_name))
    files = sorted(set(sides[0].bounds) | set(sides[1].bounds))
    found, every, unknown, any_line = set(), False, None, False

    def count(side, f, n):
        nonlocal every, unknown, any_line
        inside, steers, names = side.view(f, n)
        if not inside:
            return
        any_line = True
        every = every or steers
        if names is None:
            unknown = unknown or (
                f"the reader cannot name what line {n} of {g.shown(f)} "
                f"names, or its character map is not kept")
            return
        found.update(names)
    for f in files:
        data = [s.tree._files.get(_key(f)) if f in s.bounds else None
                for s in sides]
        old_lines, new_lines, pairs = _line_pairs(*data)
        for side, lines in zip(sides, (old_lines, new_lines)):
            for n in lines:
                count(side, f, n)
        so, sn = sides[0].statements(f), sides[1].statements(f)
        if pairs and (so is None or sn is None):
            unknown = unknown or (f"the reader's statement map of "
                                  f"{g.shown(f)} is not kept")
            so, sn = so or {}, sn or {}
        for i, j in pairs:
            o, n = sides[0].view(f, i), sides[1].view(f, j)
            if o != n or so.get(i) != sn.get(j):
                every = every or o[0] != n[0]
                count(sides[0], f, i)
                count(sides[1], f, j)
    if not any_line:
        unknown = ("no line of LC_COLLATE changed, and none is read "
                   "otherwise line by line: the same lines are read "
                   "otherwise as a whole")
    if unknown:
        return None, every, unknown
    return tuple(sorted(found)), every, None
