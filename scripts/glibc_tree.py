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
  - where a range (.., ..., ....) or codepoint_collation was read;
  - which runs this reader could not settle, or that localedef does not
    finish, and why. Such a locale is never reported as clear: the steps
    list it, with `!!`.

Steps 1, 3, 4 and 9/10 read this. Steps 2 and 6 to 8 still read the files
with glibc_locale_data's own readers (backlog 15.2 part B3, and 15.5).
"""
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


def build_at(repo, tag, build_id=None):
    """The Build glibc at `tag` reads with, plus what Red Hat's patches add
    when `build_id` names a RHEL major. Refuses when git cannot say whether
    `tag` predates glibc's lr_getc fix: lr_getc then reads 0x1A and 0xFF
    one way or the other, and a guess is an answer this tool does not
    have."""
    pre = g.descends_from(repo, g.LR_GETC_FIX, tag)
    if pre is None:
        g.die(f"git cannot tell whether {tag} comes before or after glibc's "
              f"lr_getc fix ({g.LR_GETC_FIX[:12]}), which decides how "
              f"localedef reads the bytes 0x1A and 0xFF; refusing to guess.")
    extra = None
    label = tag
    m = _RHEL_RE.search(build_id or '')
    if m:
        extra = gr.RHEL_BUILDS['rhel' + m.group(1)][2]
        label = f'{tag} + rhel{m.group(1)}'
    try:
        return gr.Build.at(repo, tag, not pre, extra_locfile=extra,
                           label=label)
    except gr.ReaderError as exc:
        g.die(f"cannot read glibc's keyword tables at {tag}: {exc}")


class Tree(object):
    """The runs of every locale of one tree. Names are str, as the steps
    print them; the files are the bytes glibc reads."""

    def __init__(self, label, files, build, decode):
        self.label = label
        self.build = build
        self._files = files
        self._decode = decode
        self._unresolved = None
        self._runs = {_name(k): v for k, v in rc.tree_runs(files,
                                                           build).items()}
        self.names = sorted(self._runs)

    @classmethod
    def at_tag(cls, repo, tag):
        """Every locale file at `tag`, read for glibc at `tag`."""
        paths = g.list_locale_files(repo, tag)
        contents, missing = g.read_blobs(repo, tag, paths, raw=True)
        g.require_read(tag, paths, missing, 'the locale files')
        files = {_key(os.path.basename(p)): v for p, v in contents.items()}
        return cls(tag, files, build_at(repo, tag), g.decoded)

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
        fatal, reads, unsettled, collate = self._runs[name]
        out = []
        for fr in reads:
            for sec in fr[3]:
                category, fname, content, copy, mid = (
                    sec[0], sec[1], sec[5], sec[6], sec[10])
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
            fatal, reads, unsettled, collate = self._runs[name]
            cut = [(fr[0], fr[5]) for fr in reads if fr[5] is not None]
            if fatal is not None:
                kind, what = fatal[0], fatal[1]
                if kind == 'missing':
                    out[name] = (f"localedef cannot open "
                                 f"{_name(what) if what else '?'}")
                else:
                    out[name] = f"localedef stops ({kind})"
            elif unsettled:
                f, line, why = unsettled[0]
                out[name] = f"{f}:{line}: {why}"
            elif cut:
                f, line = cut[0]
                out[name] = (f"localedef stops reading {_name(f)} at line "
                             f"{line}, before the end of the file")
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
        """[(line, text)] where `name`'s own file gave a range to the
        LC_COLLATE localedef builds for `name`."""
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
            counts, _, codepoint = collate
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
