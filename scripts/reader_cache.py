"""Every localedef run of a tree of locale sources, read once and kept.

glibc_reader.run() emulates one whole localedef run, the way glibc reads
every file it reaches, and that costs about 0.2 s per locale: almost every
locale reaches iso14651_t1_common, the largest file of the tree. Steps 1
to 4, 9 and 10 ask it about every locale of a tag or of a node's directory
(glibc_tree), step 2 of both tags, and the
test suite runs them many times over the same trees, so the answers are kept
on disk, in scripts/glibc-reader-cache/ next to this file, outside git
(backlog 15.2). Nothing here reads a locale file differently: a kept answer
is a run() answer, and it is used only where run() would give the same one.

Why a kept answer is the answer:
  - A run reads nothing but what resolve() hands it, by name, and resolve
    here looks only in the tree. Each kept run records every name it asked
    for and the git blob id of what it got, or None for a name the tree
    lacks, and is used only for a tree that gives each of those names the
    same blob id, or still lacks it.
  - The directory holds a hash of glibc_reader.py and of this file: a change
    to either starts from nothing.
  - Under it, a hash of the build (its keyword tables, pre236, signed_char):
    another build never shares an answer.
  - An entry that cannot be read, or reads as anything but the shape written
    here, is computed again, never taken as empty or as no run.

What is kept is run()'s answer without the tokens of each LC_COLLATE
statement: those are megabytes per locale. The kept collate holds what the
steps read from them (summary(), below). What each line names, which step
2 prints under a changed file, is kept apart, once per distinct map,
named by its sha1 (char_maps): a copy in each run would be 150 MB a
tree. The answers are
written as JSON, not pickle, so a damaged or planted entry can be wrong
data, never code; only its shape is checked.

PG_GLIBC_READER_CACHE names another directory; PG_GLIBC_READER_JOBS caps the
processes that compute what is missing (default: one per processor). A
directory that cannot be written costs speed only: the answers are computed
and returned all the same.
"""
import hashlib
import json
import os
import re
import shutil

import glibc_reader as gr

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(_HERE, 'glibc-reader-cache')
# A locale's own file reached through this many different sets of
# dependencies keeps only the most recent ones.
_CANDIDATES = 32
# Below this many runs to compute, one process does them.
_POOL_FROM = 8


def _version():
    h = hashlib.sha1(b'reader_cache 1\0')
    for f in ('glibc_reader.py', 'reader_cache.py'):
        with open(os.path.join(_HERE, f), 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()[:20]


VERSION = _version()


def blob_id(data):
    """git's id for these bytes as a blob, so a tag's file and a node's copy
    of it are one id."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def build_id(build):
    """A hash of everything about `build` that the reader reads."""
    h = hashlib.sha1(repr((
        'pre236', build.pre236, 'signed', build.signed_char,
        sorted(build.locfile_kw.items()),
        sorted(build.charmap_kw.items()))).encode('utf-8'))
    return h.hexdigest()[:20]


# -- what is kept -----------------------------------------------------------

def _enc(x):
    """JSON for run()'s answer: bytes become ['b', latin-1 text], tuples
    lists; anything else this module does not write raises."""
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, bytes):
        return ['b', x.decode('latin-1')]
    if isinstance(x, (list, tuple)):
        return ['l', [_enc(y) for y in x]]
    raise TypeError('cannot keep %r' % (type(x),))


def _dec(x):
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, list) and len(x) == 2 and x[0] == 'b' \
            and isinstance(x[1], str):
        return x[1].encode('latin-1')
    if isinstance(x, list) and len(x) == 2 and x[0] == 'l' \
            and isinstance(x[1], list):
        return tuple(_dec(y) for y in x[1])
    raise ValueError('not something this module writes')


def summary(r):
    """run()'s answer as plain values: (fatal, reads, unsettled, collate,
    meets, named). _summary's first half."""
    return _summary(r)[0]


def char_maps(r):
    """{file: (sha1, text)}: what Run.named_lines() tells of each file it
    names, as the JSON text kept for it, a flat list of the lines in
    order, each as its number, how many code points follow (-1 for a line
    the reader cannot say that of) and the code points, then, when the
    file has a cut, that line and -2; and the sha1 of that text, which
    names it. iso14651_t1_common's map is about 0.4 MB; kept once per
    distinct map, by what it holds, a tree's maps take 2.2 MB, where one
    per run would take 150 MB (measured at glibc-2.28, 2.34 and 2.39)."""
    return _maps(r.named_lines()[0])


def _maps(files):
    out = {}
    for fname, (lines, cut) in files.items():
        flat = []
        for n in sorted(lines):
            cps = lines[n]
            if cps is None:
                flat += (n, -1)
            else:
                flat.append(n)
                flat.append(len(cps))
                flat.extend(cps)
        if cut is not None:
            flat += (cut, -2)
        text = json.dumps(flat, separators=(',', ':'))
        out[fname] = (hashlib.sha1(text.encode('ascii')).hexdigest(), text)
    return out


def _summary(r):
    """(summary, char_maps(r)).

    summary is (fatal, reads, unsettled, collate, meets, named).

    fatal      None, or (kind, name, where, detail)
    reads      one (name, directives, sections, cut) per file read, in
               order; cut is FileRead.cut; each section is
               (category, fname, start, end, content, copy, mid_copies)
    unsettled  (file, line, why), in order
    collate    None when Run.collate is (no LC_COLLATE stored, a fatal
               run, or one stopped where glibc reads outside an object),
               else (counts, ranges, codepoint, instructions):
                 counts     (file, how many statements it gave), sorted
                 ranges     (file, line) of every token .., ... or ....
                            in a statement, sorted, each line once: the
                            three ellipses ld-collate.c reads as a range
                            (the `(2)` forms are charmap syntax)
                 codepoint  (file, line) of each codepoint_collation
                            statement, in reading order
                 instructions
                            (file, (first, last, first, last, ...)) of
                            the lines Collate.instructions holds, sorted by
                            file, the stretches of one file joined where
                            they touch (join_spans) and flat: as (file,
                            first, last) rows, iso14651_t1_common's
                            stretches in every run that reads it made a
                            tree's answers much larger. What step 2 asks
                            of a changed line (backlog 13.36)
    meets      (Run.met, Run.words sorted): what another build could read
               differently in this run (glibc_tree.across)
    named      (files, rest): Run.named_lines() as kept. files is (file,
               sha1) of each file's char_maps, sorted: the map itself is
               kept apart, once (char_map), and step 2 reads the
               characters each changed line names from it (backlog
               13.29); rest is True when no other file's lines can be
               named either
    """
    fatal = None if r.fatal is None else (
        r.fatal.kind, r.fatal.name, r.fatal.where, r.fatal.detail)
    reads = []
    for fr in r.reads:
        secs = tuple(
            (s.category, s.fname, s.start, s.end, s.content, s.copy,
             tuple(s.mid_copies))
            for s in fr.sections)
        reads.append((fr.name, tuple(fr.directives), secs, fr.cut))
    files, rest = r.named_lines()
    collate, maps = None, _maps(files)
    if r.collate is not None:
        counts, ranges, codepoint = {}, set(), []
        for fname, line, toks in r.collate.statements:
            counts[fname] = counts.get(fname, 0) + 1
            for t in toks:
                if t.tok in _RANGES:
                    ranges.add((fname, t.line))
            if toks and toks[0].tok == 'kw' \
                    and toks[0].kwtok == 'tok_codepoint_collation':
                codepoint.append((fname, line))
        collate = (tuple(sorted(counts.items())), tuple(sorted(ranges)),
                   tuple(codepoint), _instructions(r.collate.instructions))
    named = (tuple(sorted((f, d) for f, (d, _) in maps.items())), rest)
    return _canon((fatal, tuple(reads), tuple(r.unsettled), collate,
                   (r.met, tuple(sorted(r.words))), named)), maps


_RANGES = frozenset(('ellipsis2', 'ellipsis3', 'ellipsis4'))


def join_spans(spans):
    """[(first, last)], sorted, the ones that overlap or touch joined
    into one: iso14651_t1_common's collating-symbol lines in a row are
    one."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _instructions(lines):
    by = {}
    for f, a, b in lines:
        by.setdefault(f, []).append((a, b))
    return tuple((f, tuple(n for span in join_spans(v) for n in span))
                 for f, v in sorted(by.items()))


def _canon(x):
    """Lists to tuples, all the way down, so a fresh answer and a kept one
    compare equal."""
    if isinstance(x, (list, tuple)):
        return tuple(_canon(y) for y in x)
    return x


# -- one tree ----------------------------------------------------------------

_WORK = {}


def _one(name):
    """run() of one locale, every name it asked for with what it got, and
    {sha1: text} of each character map this process has not given back
    in this _compute (_SENT): a worker returns iso14651_t1_common's once,
    not once per run."""
    files, ids, build = _WORK['files'], _WORK['ids'], _WORK['build']
    asked = {}

    def resolve(n):
        data = files.get(n)
        asked[n] = None if data is None else ids[n]
        return data
    s, maps = _summary(gr.run(name, resolve, build))
    new = {}
    for d, text in maps.values():
        if d not in _SENT:
            _SENT.add(d)
            new[d] = text
    return name, asked, s, new


# Character maps (char_maps) by sha1: the text of every map this process
# was given or read, and the ones a worker has given back already.
_TEXTS = {}
_SENT = set()
_MAPS = {}
_SHA1 = re.compile(r'[0-9a-f]{40}')


def _chars_dir(root):
    return os.path.join(root, VERSION, 'chars')


def _keep_maps(root, new, writable):
    """Keep each map of `new` ({sha1: text}) in this process, and on disk
    under the reader's version when the directory can be written: written
    when the file is not there or holds other text, so a damaged map is
    replaced by the runs that compute it again."""
    _TEXTS.update(new)
    if not writable or not new:
        return
    d = _chars_dir(root)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return
    for sha, text in new.items():
        path = os.path.join(d, sha + '.json')
        try:
            with open(path, encoding='ascii') as fh:
                same = fh.read() == text
        except (OSError, UnicodeDecodeError):
            same = False
        if not same:
            tmp = '%s.%d.tmp' % (path, os.getpid())
            try:
                with open(tmp, 'w', encoding='ascii') as fh:
                    fh.write(text)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _maps_kept(s, root):
    """Is every character map summary `s` names here, in this process or
    on disk? A run whose map is gone is computed again (tree_runs), never
    read as one that names no character."""
    return _kept_shas((sha for _, sha in s[5][0]), root)


def _kept_shas(shas, root):
    d = _chars_dir(root)
    return all(sha in _TEXTS or os.path.isfile(os.path.join(d, sha + '.json'))
               for sha in shas)


def maps_kept(shas, cache_dir=None):
    """Is every character map named in `shas` kept (in this process or on
    disk)? glibc_tree.file_facts computes its answer again when one is
    not, which computes the runs, and so the map, again."""
    root = cache_dir if cache_dir is not None else os.environ.get(
        'PG_GLIBC_READER_CACHE') or DEFAULT_DIR
    return _kept_shas(shas, root)


def char_map(sha, cache_dir=None):
    """{line: (code points) or None} of the character map named `sha`
    (char_maps), or None when it cannot be read whole: not kept, damaged
    (its text no longer has that sha1), or another shape. A line the map
    does not hold names no character; a line it holds as None is one the
    reader cannot say that of, and so is every line from the one under
    the key 'from', when there is one (the cut of Run.named_lines). A
    file that does not read whole is removed, so that the next tree_runs
    computes the runs that name it again (_maps_kept)."""
    got = _MAPS.get(sha)
    if got is not None:
        return got
    if not _SHA1.fullmatch(sha):
        return None
    text, path = _TEXTS.get(sha), None
    if text is None:
        root = cache_dir if cache_dir is not None else os.environ.get(
            'PG_GLIBC_READER_CACHE') or DEFAULT_DIR
        path = os.path.join(_chars_dir(root), sha + '.json')
        try:
            with open(path, 'rb') as fh:
                text = fh.read().decode('latin-1')
        except OSError:
            return None
    out = _parse_map(text, sha)
    if out is None:
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
        return None
    _MAPS[sha] = out
    return out


def _parse_map(text, sha):
    """char_map's answer from the kept text, or None."""
    if hashlib.sha1(text.encode('latin-1')).hexdigest() != sha:
        return None
    try:
        flat = json.loads(text)
    except ValueError:
        return None
    out, i = {}, 0
    if not isinstance(flat, list) or not all(
            isinstance(x, int) and not isinstance(x, bool) for x in flat):
        return None
    while i < len(flat):
        if i + 1 >= len(flat):
            return None
        n, k = flat[i], flat[i + 1]
        if n in out:
            return None
        if k == -2:
            if i + 2 != len(flat) or n < 1:
                return None
            out['from'] = n
            i += 2
            continue
        if k == -1:
            out[n] = None
            i += 2
            continue
        if k < 1 or i + 2 + k > len(flat):
            return None
        out[n] = tuple(flat[i + 2:i + 2 + k])
        i += 2 + k
    return out


def _jobs():
    raw = os.environ.get('PG_GLIBC_READER_JOBS')
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return os.cpu_count() or 1


def _compute(names, files, ids, build):
    _WORK.update(files=files, ids=ids, build=build)
    _SENT.clear()
    try:
        jobs = min(_jobs(), len(names))
        if jobs > 1 and len(names) >= _POOL_FROM:
            import multiprocessing
            try:
                ctx = multiprocessing.get_context('fork')
            except ValueError:
                ctx = None
            if ctx is not None:
                with ctx.Pool(jobs) as pool:
                    return pool.map(_one, names, chunksize=4)
        return [_one(n) for n in names]
    finally:
        _WORK.clear()


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, separators=(',', ':'))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _tree_entry(path, files, root):
    """{name: summary} kept for exactly this tree, with every character
    map it names, or None."""
    got = _read_json(path)
    if not isinstance(got, dict) or len(got) != len(files):
        return None
    out = {}
    try:
        for k, v in got.items():
            name = k.encode('latin-1')
            if name not in files:
                return None
            out[name] = _check(_dec(v))
    except (ValueError, TypeError, UnicodeEncodeError):
        return None
    if not all(_maps_kept(s, root) for s in out.values()):
        return None
    return out


def _check(s):
    """A kept summary, if it has the shape summary() writes; else raises."""
    if not (isinstance(s, tuple) and len(s) == 6):
        raise ValueError('not a run summary')
    fatal, reads, unsettled, collate, meets, named = s
    if fatal is not None and not (isinstance(fatal, tuple)
                                  and len(fatal) == 4):
        raise ValueError('bad fatal')
    if not isinstance(reads, tuple) or not all(
            isinstance(fr, tuple) and len(fr) == 4
            and isinstance(fr[0], bytes) and isinstance(fr[2], tuple)
            and (fr[3] is None or isinstance(fr[3], int))
            and all(isinstance(sec, tuple) and len(sec) == 7
                    for sec in fr[2])
            for fr in reads):
        raise ValueError('bad reads')
    if not isinstance(unsettled, tuple) or not all(
            isinstance(u, tuple) and len(u) == 3 for u in unsettled):
        raise ValueError('bad unsettled')
    if collate is not None and not (
            isinstance(collate, tuple) and len(collate) == 4
            and all(isinstance(part, tuple) and all(
                isinstance(x, tuple) and len(x) == 2
                and isinstance(x[0], str) and isinstance(x[1], int)
                for x in part) for part in collate[:3])
            and isinstance(collate[3], tuple) and all(
                isinstance(x, tuple) and len(x) == 2
                and isinstance(x[0], str) and isinstance(x[1], tuple)
                and len(x[1]) % 2 == 0
                and all(isinstance(n, int) for n in x[1])
                for x in collate[3])):
        raise ValueError('bad collate')
    if not (isinstance(meets, tuple) and len(meets) == 2
            and isinstance(meets[0], bool) and isinstance(meets[1], tuple)
            and all(isinstance(w, bytes) for w in meets[1])):
        raise ValueError('bad meets')
    if not (isinstance(named, tuple) and len(named) == 2
            and isinstance(named[0], tuple) and all(
                isinstance(x, tuple) and len(x) == 2
                and isinstance(x[0], str) and isinstance(x[1], str)
                and _SHA1.fullmatch(x[1])
                for x in named[0])
            and isinstance(named[1], bool)):
        raise ValueError('bad named')
    return s


def _run_entry(path, name, files, ids, root):
    """(summary or None, every candidate kept under this path).

    A candidate is `name`'s only if it asked for `name` itself and got the
    bytes it has here: run() opens its -i file before anything else, so
    every run of `name` recorded it, and an entry that did not, whatever
    else it matches (an empty list of names matches every tree), is not
    this run. One whose character maps are not all kept is not used
    either (_maps_kept)."""
    got = _read_json(path)
    if not isinstance(got, list):
        return None, []
    cands = []
    for c in got:
        try:
            deps, s = c
            deps = {k.encode('latin-1'): v for k, v in deps.items()}
            if not all(v is None or isinstance(v, str)
                       for v in deps.values()):
                continue
            s = _check(_dec(s))
        except (ValueError, TypeError, AttributeError, UnicodeEncodeError):
            continue
        cands.append((deps, s))
    for deps, s in cands:
        if deps.get(name) == ids[name] and all(
                ids.get(n) == v if v is not None else n not in files
                for n, v in deps.items()) and _maps_kept(s, root):
            return s, cands
    return None, cands


def tree_runs(files, build, cache_dir=None):
    """{name: summary(run(name))} for every name in `files`.

    `files` is {name: bytes} for one tree: every locale source of a tag or
    of a directory, by the name glibc resolves (`copy` strings and -i), and
    nothing else is visible to the runs.
    """
    root = cache_dir if cache_dir is not None else os.environ.get(
        'PG_GLIBC_READER_CACHE') or DEFAULT_DIR
    ids = {n: blob_id(d) for n, d in files.items()}
    d = os.path.join(root, VERSION, build_id(build))
    fresh_version = not os.path.isdir(os.path.join(root, VERSION))
    try:
        os.makedirs(d, exist_ok=True)
        writable = True
    except OSError:
        writable = False
    if writable and fresh_version:
        _prune(root)
    tree = hashlib.sha1(repr(sorted(ids.items())).encode('utf-8')).hexdigest()
    tree_path = os.path.join(d, 'tree-%s.json' % tree)
    got = _tree_entry(tree_path, files, root)
    if got is not None:
        return got
    lock = _lock(os.path.join(d, 'tree-%s.lock' % tree)) if writable else None
    try:
        # Another process may have finished the same tree while this one
        # waited.
        got = _tree_entry(tree_path, files, root) if lock is not None \
            else None
        if got is not None:
            return got
        out, todo, kept = {}, [], {}
        for name in sorted(files):
            # The whole name, hashed: two names may share any prefix.
            path = os.path.join(d, 'run-%s-%s.json' % (
                hashlib.sha1(name).hexdigest(), ids[name]))
            s, cands = _run_entry(path, name, files, ids, root)
            if s is not None:
                out[name] = s
            else:
                todo.append(name)
                kept[name] = (path, cands)
        for name, asked, s, maps in _compute(todo, files, ids, build):
            out[name] = s
            _keep_maps(root, maps, writable)
            if writable:
                path, cands = kept[name]
                fresh = [({k.decode('latin-1'): v for k, v in asked.items()},
                          _enc(s))]
                old = [({k.decode('latin-1'): v for k, v in deps.items()},
                        _enc(cs)) for deps, cs in cands]
                _write_json(path, (fresh + old)[:_CANDIDATES])
        if writable:
            _write_json(tree_path, {k.decode('latin-1'): _enc(v)
                                    for k, v in out.items()})
        return out
    finally:
        if lock is not None:
            lock.close()


def kept_tree(ids, build, cache_dir=None):
    """Is an answer kept for the tree whose files have these git blob ids
    ({name: id}), read for `build`? Asked without reading a file, from a
    `git ls-tree` alone: tests/_harness.fill_reader_cache skips a tree that
    has one. Says only that the entry exists: tree_runs still refuses one
    it cannot read, and computes it again."""
    root = cache_dir if cache_dir is not None else os.environ.get(
        'PG_GLIBC_READER_CACHE') or DEFAULT_DIR
    tree = hashlib.sha1(repr(sorted(ids.items())).encode('utf-8')).hexdigest()
    return os.path.isfile(os.path.join(root, VERSION, build_id(build),
                                       'tree-%s.json' % tree))


def _prune(root):
    """Remove what earlier versions of the reader kept: a version this
    code is not can never be read again. Only directories named like a
    VERSION are touched, and a failure leaves them."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for n in names:
        if n != VERSION and re.fullmatch(r'[0-9a-f]{20}', n):
            shutil.rmtree(os.path.join(root, n), ignore_errors=True)


def _lock(path):
    """An exclusive lock held until the returned file is closed, so two
    processes asked for one tree compute it once. None where there is no
    flock or the file cannot be opened: the tree is then computed without
    it, which costs time and changes no answer."""
    try:
        import fcntl
        fh = open(path, 'a')
    except (ImportError, OSError):
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError:
        fh.close()
        return None
    return fh
