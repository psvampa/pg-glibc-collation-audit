"""What localedef reads for every locale of one tree, in the terms the steps
ask about.

A tree is every locale source of one glibc tag, or of one directory copied
from a machine, read for one glibc build. For each locale glibc_reader.run()
emulates `localedef -i <locale>` (reader_cache keeps the answers), and this
module answers from those runs and from nothing else (backlog 15.2):

  - which locales have an LC_COLLATE of their own, and what it copies;
  - which files' LC_COLLATE localedef reads to build each locale, following
    every `copy` as glibc follows it, with the state glibc carries from file
    to file (one run per locale, never a graph pieced together from each
    file read alone: backlog 15.1 measured that shortcut wrong);
  - where a range (.., ..., ....) or codepoint_collation was read, and
    which lines of each file steer other lines (step 2, backlog 13.36);
  - which characters each line of each file names, in every run, as far
    as the reader is sure of that run (glibc_reader.Run.named_lines; step
    2, backlog 13.29);
  - which runs this reader could not settle, or that localedef does not
    finish, and why. Such a locale is never reported as clear: the steps
    list it, with `!!`.

Steps 1 to 4 and 9/10 read this. Steps 6 to 8, and step 2's
lc_collate_unread check and the files its check of character sets and
transliterations (collation_keys) reads, still read with their own readers.
"""
import hashlib
import json
import operator
import os
import re

import diff_distro_locales as dd
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

    @classmethod
    def at_dir(cls, root, names, repo, tag, build_id):
        """The files `names` of directory `root`, read for glibc at `tag`
        with what the RHEL major `build_id` names adds. A name the
        directory lacks is one localedef cannot open there."""
        files = {}
        for n in names:
            with open(os.path.join(root, n), 'rb') as fh:
                files[_key(n)] = fh.read()
        return cls(f'{build_id} ({root})', files,
                   build_at(repo, tag, build_id),
                   lambda b: b.decode('utf-8', 'surrogateescape'))

    # -- one run ------------------------------------------------------------

    def _collate_sections(self, name):
        """(file, copies) of every LC_COLLATE section run(name) read for
        its content, in reading order; copies are str names, the opening
        copy first."""
        fatal, reads, unsettled, collate, _, _ = self._runs[name]
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

    def reach(self, name):
        """Every other file whose LC_COLLATE localedef read to build
        `name`."""
        return {f for f, _ in self._collate_sections(name)} - {name}

    def roots(self, name, roots):
        """The members of `roots` that building `name` reaches through
        `copy`, not counting those reached only through another member:
        glibc_locale_data.inherited_from's rule, walked over the copies
        run(name) read rather than over each file read alone."""
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
            fatal, reads, unsettled, collate, _, _ = self._runs[name]
            cut = [(fr[0], fr[3]) for fr in reads if fr[3] is not None]
            if fatal is not None:
                kind, what = fatal[0], fatal[1]
                if kind == 'missing':
                    file = '?' if what is None else dd.shown(_name(what))
                    out[name] = f"localedef cannot open {file}"
                else:
                    out[name] = f"localedef stops ({kind})"
            elif unsettled:
                f, line, why = unsettled[0]
                out[name] = f"{dd.shown(f)}:{line}: {why}"
            elif cut:
                f, line = cut[0]
                out[name] = (f"localedef stops reading "
                             f"{dd.shown(_name(f))} at line {line}, before "
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

    def style(self, name):
        """glibc_locale_data.classify_collation_style's answer, from what
        localedef read: 'none', 'codepoint', 'ellipsis',
        'codepoint-not-alone', 'copy-only' or 'explicit'; or 'unread' when
        the run stopped or could not be settled, since what was not read
        has no shape to report (a `copy` that stops localedef hides every
        line after it, codepoint_collation included)."""
        if name in self.unresolved():
            return 'unread'
        if not self.defines(name):
            return 'none'
        declared, _ = self.byte_order()
        if name in declared:
            return 'codepoint'
        if self.ranges(name):
            return 'ellipsis'
        collate = self._collate(name)
        own = 0
        if collate is not None:
            if any(f == name for f, _ in collate[2]):
                return 'codepoint-not-alone'
            own = sum(n for f, n in collate[0] if f == name)
        if not own and self.copies(name):
            return 'copy-only'
        return 'explicit'


# -- what step 2 asks of each file -------------------------------------------

class Facts(object):
    """How localedef reads one file of a tree, for step 2.

    collate     [(start, end, comment, escape, run)]: every LC_COLLATE
                section of this file that localedef read for its content,
                once for each run of the tree that read it (`run` is the
                locale built), by its lines (end is None when the file
                ended first) and the comment and escape bytes that run's
                read of the file had obeyed when the section started. Runs
                can read one section with different characters: a run that
                reads the file for LC_COLLATE alone skips its other
                sections, and with them any directive inside one that its
                own run obeys (a `copy` that returns leaves the rest of the
                section to the top level, ld-ctype.c:2128-2139@2.34).
    unresolved  why the file's own run is not settled (Tree.unresolved), or
                None.
    instructions
                [(first, last)], sorted and joined: the lines of the file
                that steer other lines in some run that reads it
                (glibc_reader.Collate.instructions). A changed line among
                them can move rules on lines that did not change (backlog
                13.36).
    chars       the sha1 of every character map (reader_cache.char_maps)
                of this file in a run of the tree, sorted, each once:
                what glibc_reader.Run.named_lines tells of the file in
                that run. `load` reads one (reader_cache.char_map).
    stopped     the runs of the tree, by the locale built, that tell
                nothing of this file: Run.named_lines gives no map of it
                and says no other file can be told (its `rest`). Empty
                at glibc-2.28, 2.34 or 2.39, where no run is unsettled.
                chars and stopped have no default: a Facts made without
                them would name no character on any line, and facts_of,
                which makes one again when the pair renames a file, passes
                both.
    """

    __slots__ = ('collate', 'unresolved', 'instructions', 'chars', 'stopped',
                 'load')

    def __init__(self, collate, unresolved, instructions, chars, stopped,
                 load):
        self.collate = collate
        self.unresolved = unresolved
        self.instructions = instructions
        self.chars = tuple(chars)
        self.stopped = tuple(stopped)
        self.load = load

    def reading(self):
        """The (comment, escape) pairs LC_COLLATE is read with, each once,
        in the order of the sections."""
        return list(dict.fromkeys((c, e) for _, _, c, e, _ in self.collate))

    def inside(self, line):
        """Is `line` inside one of the LC_COLLATE sections?"""
        return any(start <= line and (end is None or line <= end)
                   for start, end, _, _, _ in self.collate)

    def instruction(self, line):
        """Is `line` on one of the instructions?"""
        return any(a <= line <= b for a, b in self.instructions)

    def characters(self, line):
        """The code points the statements on `line` name, in each run that
        reads it, in reading order and each once (glibc_reader.Collate.
        characters, through the maps in `chars`); [] when none does; None
        when this cannot be told: a run of the tree that tells nothing of
        the file (`stopped`), a map that cannot be read whole, a run in
        which the reader cannot say what the line names, or one that
        knows the file only up to an earlier line (the map's 'from', the
        cut of glibc_reader.Run.named_lines). Each run reads the line with
        its own comment and escape characters, so a line two runs read
        with two pairs is named by both readings."""
        if self.stopped:
            return None
        out = []
        for sha in self.chars:
            got = self.load(sha)
            if got is None:
                return None
            if 'from' in got and line >= got['from']:
                return None
            if line not in got:
                continue
            cps = got[line]
            if cps is None:
                return None
            for c in cps:
                if c not in out:
                    out.append(c)
        return out


def chars_before(directives, line):
    """(comment byte, escape byte) a file's read had in force at `line`:
    `#` and `\\` (lr_create, linereader.c:79-80@2.39), changed by each
    directive the read obeyed on an earlier line (locfile.c:123-149)."""
    cc, ec = 0x23, 0x5c
    for ln, which, byte in directives:
        if ln < line:
            if which == 'comment_char':
                cc = byte
            else:
                ec = byte
    return cc, ec


def _facts(tree, load):
    bad = tree.unresolved()
    collate = {n: set() for n in tree.names}
    steer = {n: set() for n in tree.names}
    chars = {n: set() for n in tree.names}
    stopped = {n: set() for n in tree.names}
    for name in tree.names:
        maps, rest = tree._runs[name][5]
        maps = dict(maps)
        for f in tree.names:
            if f in maps:
                chars[f].add(maps[f])
            elif rest:
                stopped[f].add(name)
    for name in tree.names:
        run = tree._runs[name]
        for fr in run[1]:
            for sec in fr[2]:
                f = _name(sec[1])
                if sec[0] == 'LC_COLLATE' and sec[4] and f in collate:
                    collate[f].add((sec[2], sec[3])
                                   + chars_before(fr[1], sec[2]) + (name,))
        for f, flat in (run[3][3] if run[3] is not None else ()):
            if f in steer:
                steer[f].update(zip(flat[0::2], flat[1::2]))
    return {n: Facts(sorted(collate[n], key=lambda r: (r[4], r[0])),
                     bad.get(n), rc.join_spans(steer[n]), sorted(chars[n]),
                     sorted(stopped[n]), load)
            for n in tree.names}


def _sources_hash(paths):
    h = hashlib.sha1()
    for path in paths:
        with open(path, 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


# What _facts computes is kept under the hash of the code that computes it,
# so a change to that code starts it from nothing as a change to the reader
# does: this file, and diff_distro_locales.py, whose shown() writes the
# names in the reasons Tree.unresolved gives and _facts keeps as text.
# .github/workflows/tests.yml keys its cache on the same files.
_SELF_SOURCES = [os.path.abspath(__file__), os.path.abspath(dd.__file__)]
_SELF = _sources_hash(_SELF_SOURCES)


def _facts_path(paths, ids, build, cache_dir):
    root = cache_dir if cache_dir is not None else os.environ.get(
        'PG_GLIBC_READER_CACHE') or rc.DEFAULT_DIR
    tree = hashlib.sha1(repr(sorted(
        (os.path.basename(p), ids[p]) for p in paths)).encode(
            'utf-8', 'surrogateescape')).hexdigest()
    return os.path.join(root, rc.VERSION, rc.build_id(build),
                        'facts-%s-%s.json' % (_SELF, tree))


def _load_facts(path, names, load):
    try:
        with open(path, encoding='utf-8') as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or set(got) != names:
        return None
    out = {}
    for n, v in got.items():
        if not (isinstance(v, list) and len(v) == 5):
            return None
        collate, why, steer, chars, stopped = v
        if not (isinstance(collate, list) and all(
                isinstance(r, list) and len(r) == 5
                and isinstance(r[0], int)
                and (r[1] is None or isinstance(r[1], int))
                and isinstance(r[2], int) and isinstance(r[3], int)
                and isinstance(r[4], str)
                for r in collate)
                and (why is None or isinstance(why, str))
                and isinstance(steer, list) and all(
                    isinstance(x, list) and len(x) == 2
                    and all(isinstance(y, int) for y in x) for x in steer)
                and isinstance(chars, list) and all(
                    isinstance(x, str) for x in chars)
                and isinstance(stopped, list) and all(
                    isinstance(x, str) for x in stopped)):
            return None
        out[n] = Facts([tuple(r) for r in collate], why,
                       [tuple(x) for x in steer], chars, stopped, load)
    return out


def file_facts(repo, tag, paths, ids, build, cache_dir=None):
    """{name: Facts} for every locale file at `tag` read for `build`.

    `paths` and `ids` are list_tree_ids' answer for the tag, which step 2
    has already, so a kept answer costs no file read: loading the whole
    tree's runs for two tags cost each run of step 2 about 0.5 s (backlog
    15.2 B3, measured). The answer
    is derived from those runs and nothing else, kept next to them under
    the reader's version, the build and _SELF (the hash of _SELF_SOURCES);
    an entry that cannot be read, reads as another shape or other names,
    or names a character map that is not kept (reader_cache.maps_kept), is
    computed again from the runs, which then compute that map again."""
    path = _facts_path(paths, ids, build, cache_dir)
    names = {os.path.basename(p) for p in paths}
    if len(names) != len(paths):
        # Facts, like the runs, are kept by the name glibc resolves, and
        # two paths with one name would answer for each other.
        g.die(f"two locale files at {tag} share a name "
              f"({len(paths) - len(names)} more paths than names); refusing "
              f"to answer for either.")

    def load(sha):
        return rc.char_map(sha, cache_dir)
    got = _load_facts(path, names, load)
    if got is not None and rc.maps_kept(
            {sha for f in got.values() for sha in f.chars}, cache_dir):
        return got
    tree = Tree.at_tag(repo, tag, build, cache_dir)
    if set(tree.names) != names:
        g.die(f"the locale files read at {tag} are not the ones listed "
              f"there; refusing to answer for either list.")
    got = _facts(tree, load)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return got
    rc._write_json(path, {n: [[list(r) for r in f.collate], f.unresolved,
                              [list(x) for x in f.instructions],
                              list(f.chars), list(f.stopped)]
                          for n, f in got.items()})
    return got


def same_run(files, name, b0, b1):
    """Does localedef read the run of `name` over `files` ({name: bytes})
    the same with Build b0 and with Build b1? run()'s summary, and every
    token of every LC_COLLATE statement, every field of each (Tok's
    slots): reader_cache keeps the summary alone, which holds how many
    statements each file gave, the words met and the characters each line
    names, and not every token of a statement. Computed, never kept,
    and compared token by token, stopping at the first difference: making a
    value of each run cost about 3.5 s a run where reading it costs 0.25 s
    (backlog 15.2 B3, measured on the Mac)."""
    r0 = gr.run(_key(name), files.get, b0)
    r1 = gr.run(_key(name), files.get, b1)
    if rc.summary(r0) != rc.summary(r1):
        return False
    if r0.collate is None or r1.collate is None:
        return r0.collate is None and r1.collate is None
    s0, s1 = r0.collate.statements, r1.collate.statements
    if len(s0) != len(s1):
        return False
    fields = operator.attrgetter(*gr.Tok.__slots__)
    for (f0, l0, t0), (f1, l1, t1) in zip(s0, s1):
        if f0 != f1 or l0 != l1 or len(t0) != len(t1):
            return False
        for x, y in zip(t0, t1):
            if fields(x) != fields(y):
                return False
    return True


# -- what two builds read differently in one tree ------------------------------

_ACROSS = {}


def _differs(name):
    files, b0, b1 = _ACROSS['files'], _ACROSS['b0'], _ACROSS['b1']
    return name, not same_run(files, name, b0, b1)


def _across_path(ids, b0, b1, cache_dir):
    root = cache_dir if cache_dir is not None else os.environ.get(
        'PG_GLIBC_READER_CACHE') or rc.DEFAULT_DIR
    tree = hashlib.sha1(repr(sorted(ids.items())).encode(
        'utf-8', 'surrogateescape')).hexdigest()
    return os.path.join(root, rc.VERSION, 'across-%s-%s-%s-%s.json' % (
        _SELF, rc.build_id(b0), rc.build_id(b1), tree))


def across(ids, load, b0, b1, cache_dir=None):
    """The locales of one tree that localedef builds differently with Build
    b0 and with Build b1, sorted. `ids` is {name: git blob id} for every
    file of the tree, from the listing, and `load()` gives {name (bytes):
    bytes} of the same files: it is called only when no answer is kept,
    so a kept one costs no file read.

    As this reader models them, two builds read a file differently only
    where they differ: before glibc 2.36 lr_getc reads 0x1A, and 0xFF
    through a signed char, as the end of the file (linereader.h:138@2.34),
    and from 2.36 a token that opens with a byte above 0x7F is a UTF-8
    character (Build.utf8); and a word one keyword table has and the other
    has not, or gives another token (codepoint_collation from 2.35). Every
    run records whether it met such a byte (Run.met) and which words it
    looked up (Run.words), so the runs of the tree with b0 -- kept
    already, as steps 1 to 4 read them -- say which can differ; a run that
    met nothing either way is read the same with b1, step by step. Each of
    the others is computed whole with both Builds (same_run: every token
    of every LC_COLLATE statement) and listed when the two differ. A run
    this reader did not settle is listed whenever the two builds' byte
    reading or keyword tables differ at all: glibc may read on where the
    reader stopped, and meet what they read differently in a spelling no
    search of the bytes would find (a word continued by the escape, round
    3 of the 15.2 B3 review). Where glibc reads outside an object
    (glibc_reader.GlibcUndefined) two binaries can differ even with the
    same tables; such a run is unsettled with each, and stays unresolved.

    Kept next to the reader's answers, under the reader's version, both
    builds, _SELF and the tree's blob ids; an entry that cannot be read or
    holds another shape is computed again."""
    path = _across_path(ids, b0, b1, cache_dir)
    names = set(ids)
    got = _kept_across(path, names)
    if got is not None:
        return got
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lock = rc._lock(path + '.lock')
    except OSError:
        lock = None
    try:
        # Another process may have finished the same pair while this one
        # waited.
        got = _kept_across(path, names) if lock is not None else None
        if got is not None:
            return got
        return _across(ids, load(), names, b0, b1, path, cache_dir)
    finally:
        if lock is not None:
            lock.close()


def _kept_across(path, names):
    try:
        with open(path, encoding='utf-8') as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(got, list) and all(isinstance(n, str) and n in names
                                     for n in got):
        return sorted(got)
    return None


def keyword_words(b0, b1):
    """(words, named): the words two Builds' keyword tables give different
    entries, as bytes (missing from one, or another token), and whether
    one of them is a keyword read as a <name> (k[1] == 1), which no run
    records (Run.words)."""
    out, named = set(), False
    for t0, t1 in ((b0.locfile_kw, b1.locfile_kw),
                   (b0.charmap_kw, b1.charmap_kw)):
        for k in set(t0) | set(t1):
            e0, e1 = t0.get(k), t1.get(k)
            if e0 != e1:
                out.add(k if isinstance(k, bytes) else k.encode('ascii'))
                named = named or any(e is not None and e[1] == 1
                                     for e in (e0, e1))
    return out, named


def _across(ids, files, names, b0, b1, path, cache_dir):
    if {_name(k) for k in files} != names or any(
            rc.blob_id(v) != ids[_name(k)] for k, v in files.items()):
        g.die("the locale files read are not the ones listed; refusing to "
              "compare how two versions of glibc read them.")
    words, named = keyword_words(b0, b1)
    bytes_differ = (b0.pre236 != b1.pre236 or b0.utf8 != b1.utf8
                    or b0.signed_char != b1.signed_char)
    tree = Tree('across', files, b0, g.decoded, cache_dir)
    bad = tree.unresolved()
    out, todo = set(), []
    for name in tree.names:
        met, looked = tree._runs[name][4]
        if name in bad:
            if bytes_differ or words or named:
                out.add(name)
        elif named or (bytes_differ and met) or words & set(looked):
            todo.append(name)
    out |= {n for n, d in _map_differs(todo, files, b0, b1) if d}
    rc._write_json(path, sorted(out))
    return sorted(out)


def _map_differs(names, files, b0, b1):
    _ACROSS.update(files=files, b0=b0, b1=b1)
    try:
        jobs = min(rc._jobs(), len(names))
        if jobs > 1 and len(names) >= rc._POOL_FROM:
            import multiprocessing
            try:
                ctx = multiprocessing.get_context('fork')
            except ValueError:
                ctx = None
            if ctx is not None:
                with ctx.Pool(jobs) as pool:
                    return pool.map(_differs, names, chunksize=2)
        return [_differs(n) for n in names]
    finally:
        _ACROSS.clear()
