"""Every localedef run of a tree of locale sources, read once and kept.

glibc_reader.run() emulates one whole localedef run, the way glibc reads
every file it reaches, and that costs about 0.2 s per locale: almost every
locale reaches iso14651_t1_common, the largest file of the tree. Steps 1,
3, 4, 9 and 10 ask it about every locale of a tag or of a node's directory
(glibc_tree), and the
test suite runs them many times over the same trees, so the answers are kept
on disk, in scripts/glibc-reader-cache/ next to the glibc clone, outside git
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
steps read from them (summary(), below): how many statements each file
gave, and where a range or codepoint_collation was read. The answers are
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
    """run()'s answer as plain values: (fatal, reads, unsettled, collate).

    fatal      None, or (kind, name, where, detail)
    reads      one (name, mask, directives, sections, stray, cut) per file
               read, in order; mask is sorted; cut is FileRead.cut; each
               section is
               (category, fname, start, end, end_word, content, copy,
                copy_kind, skipped, returned, mid_copies), end_word being
               (tok, val) or None
    unsettled  (file, line, why), in order
    collate    None when Run.collate is (no LC_COLLATE stored, a fatal
               run, or one stopped where glibc reads outside its buffer),
               else (counts, ranges, codepoint):
                 counts     (file, how many statements it gave), sorted
                 ranges     (file, line) of every token .., ... or ....
                            in a statement, sorted, each line once: the
                            three ellipses ld-collate.c reads as a range
                            (the `(2)` forms are charmap syntax)
                 codepoint  (file, line) of each codepoint_collation
                            statement, in reading order
    """
    fatal = None if r.fatal is None else (
        r.fatal.kind, r.fatal.name, r.fatal.where, r.fatal.detail)
    reads = []
    for fr in r.reads:
        secs = tuple(
            (s.category, s.fname, s.start, s.end,
             None if s.end_word is None else (s.end_word.tok,
                                              s.end_word.val),
             s.content, s.copy, s.copy_kind, s.skipped, s.returned,
             tuple(s.mid_copies))
            for s in fr.sections)
        reads.append((fr.name, tuple(sorted(fr.mask)),
                      tuple(fr.directives), secs, tuple(fr.stray), fr.cut))
    collate = None
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
                   tuple(codepoint))
    return _canon((fatal, tuple(reads), tuple(r.unsettled), collate))


_RANGES = frozenset(('ellipsis2', 'ellipsis3', 'ellipsis4'))


def _canon(x):
    """Lists to tuples, all the way down, so a fresh answer and a kept one
    compare equal."""
    if isinstance(x, (list, tuple)):
        return tuple(_canon(y) for y in x)
    return x


# -- one tree ----------------------------------------------------------------

_WORK = {}


def _one(name):
    """run() of one locale, and every name it asked for with what it got."""
    files, ids, build = _WORK['files'], _WORK['ids'], _WORK['build']
    asked = {}

    def resolve(n):
        data = files.get(n)
        asked[n] = None if data is None else ids[n]
        return data
    return name, asked, summary(gr.run(name, resolve, build))


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


def _tree_entry(path, files):
    """{name: summary} kept for exactly this tree, or None."""
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
    return out


def _check(s):
    """A kept summary, if it has the shape summary() writes; else raises."""
    if not (isinstance(s, tuple) and len(s) == 4):
        raise ValueError('not a run summary')
    fatal, reads, unsettled, collate = s
    if fatal is not None and not (isinstance(fatal, tuple)
                                  and len(fatal) == 4):
        raise ValueError('bad fatal')
    if not isinstance(reads, tuple) or not all(
            isinstance(fr, tuple) and len(fr) == 6
            and isinstance(fr[0], bytes) and isinstance(fr[3], tuple)
            and (fr[5] is None or isinstance(fr[5], int))
            and all(isinstance(sec, tuple) and len(sec) == 11
                    for sec in fr[3])
            for fr in reads):
        raise ValueError('bad reads')
    if not isinstance(unsettled, tuple) or not all(
            isinstance(u, tuple) and len(u) == 3 for u in unsettled):
        raise ValueError('bad unsettled')
    if collate is not None and not (
            isinstance(collate, tuple) and len(collate) == 3
            and all(isinstance(part, tuple) and all(
                isinstance(x, tuple) and len(x) == 2
                and isinstance(x[0], str) and isinstance(x[1], int)
                for x in part) for part in collate)):
        raise ValueError('bad collate')
    return s


def _run_entry(path, name, files, ids):
    """(summary or None, every candidate kept under this path).

    A candidate is `name`'s only if it asked for `name` itself and got the
    bytes it has here: run() opens its -i file before anything else, so
    every run of `name` recorded it, and an entry that did not, whatever
    else it matches (an empty list of names matches every tree), is not
    this run."""
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
                for n, v in deps.items()):
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
    got = _tree_entry(tree_path, files)
    if got is not None:
        return got
    lock = _lock(os.path.join(d, 'tree-%s.lock' % tree)) if writable else None
    try:
        # Another process may have finished the same tree while this one
        # waited.
        got = _tree_entry(tree_path, files) if lock is not None else None
        if got is not None:
            return got
        out, todo, kept = {}, [], {}
        for name in sorted(files):
            # The whole name, hashed: two names may share any prefix.
            path = os.path.join(d, 'run-%s-%s.json' % (
                hashlib.sha1(name).hexdigest(), ids[name]))
            s, cands = _run_entry(path, name, files, ids)
            if s is not None:
                out[name] = s
            else:
                todo.append(name)
                kept[name] = (path, cands)
        for name, asked, s in _compute(todo, files, ids, build):
            out[name] = s
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
