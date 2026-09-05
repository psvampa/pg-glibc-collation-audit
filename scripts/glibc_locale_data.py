#!/usr/bin/env python3
"""
Shared helpers for reading glibc locale data out of a git clone.

Every script in this directory needs the same handful of primitives: locate the
glibc clone, read blobs at a tag, find the LC_COLLATE block, and walk the
`copy` graph. They used to each spawn one `git show` per locale file (~350
subprocesses) with a 30s timeout and an ignored return code, so a slow blob
fetch on a `--filter=blob:none` clone silently dropped files from the analysis
and the locale was reported unaffected. Everything here fails loudly instead,
and blobs are read in a single `git cat-file --batch` stream.

Also runnable directly, for the shell script:
  python3 glibc_locale_data.py fanin <tag> [changed_files.txt]
"""
import os
import re
import subprocess
import sys

LOCALES_DIR = 'localedata/locales'

# Every ellipsis form glibc's localedef accepts. A range like
# `<UAC00>`/`..`/`<UD7A3>` is expanded algorithmically by localedef at build
# time, so the weights are NOT in the locale source and a source diff cannot
# prove the locale's order is unchanged.
#
# The forms come from locale/programs/linereader.c, which has already consumed
# one `.` before it compares the rest: `..`, `...`, `....`, `..(2)..` and
# `....(2)....`. The count is always a literal 2, never an arbitrary number.
#
# This must NOT be anchored to the start of the line. The dominant form in
# glibc is inline -- `collating-symbol <SAC00>..<SD7A3>` in iso14651_t1_common
# carries the constructed Hangul and Han weights -- and anchoring it missed
# every one of them, which cleared zh_CN and its pinyin siblings.
#
# Longest alternatives first, plus dot boundaries, so one run of dots yields
# exactly one match and `....` is never read as two `..`.
ELLIPSIS_RE = re.compile(
    r'(?<!\.)('
    r'\.\.\.\.\(2\)\.\.\.\.'
    r'|\.\.\.\.'
    r'|\.\.\.'
    r'|\.\.\(2\)\.\.'
    r'|\.\.'
    r')(?!\.)'
)

# Locale files declare their comment character; every one in glibc uses `%`.
# Prose in a comment ("2.28..2.34", "and so on ...") is not a range, so
# comments are stripped before looking for one.
_COMMENT_CHAR_RE = re.compile(r'^\s*comment_char\s+(\S)', re.M)

_COLLATE_BLOCK_RE = re.compile(r'\nLC_COLLATE\b(.*?)\nEND LC_COLLATE', re.S)
_COPY_RE = re.compile(r'^\s*copy\s+"([^"]+)"', re.M)


def die(msg, code=2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _is_glibc_clone(path):
    """Is `path` a git clone of glibc?

    Asked of git, not of the filesystem. audit-locale-diff.sh clones with
    `--no-checkout`, so the clone has no working tree at all and a
    `localedata/locales` directory never appears on disk -- every step used to
    die with "run audit-locale-diff.sh first" on the very run that had just
    cloned it.
    """
    if not os.path.isdir(path):
        return False
    if run_git(['rev-parse', '--git-dir'], path, allow_fail=True).returncode != 0:
        return False
    if os.path.isdir(os.path.join(path, LOCALES_DIR)):
        return True
    # No working tree: ask the object store instead. `--filter=blob:none`
    # keeps every tree, so this needs no network.
    return run_git(['cat-file', '-e', f'HEAD:{LOCALES_DIR}'],
                   path, allow_fail=True).returncode == 0


def find_repo(explicit=None):
    """Locate the glibc clone.

    Accepts being run from the clone itself, from scripts/, or from the repo
    root, so that a wrong working directory can no longer produce an empty
    diff that later gets mistaken for "nothing changed".
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates += [os.getcwd(),
                       os.path.join(os.getcwd(), 'glibc'),
                       os.path.join(here, 'glibc'),
                       os.path.join(here, os.pardir, 'glibc')]
    for c in candidates:
        if _is_glibc_clone(c):
            return os.path.abspath(c)
    die("could not find the glibc clone (no git repository containing "
        "localedata/locales among the paths tried). Run "
        "scripts/audit-locale-diff.sh first, or pass "
        "--repo <path-to-glibc-clone>.")


def run_git(args, repo, allow_fail=False):
    """Run git, aborting on failure unless explicitly allowed."""
    p = subprocess.run(['git', *args], cwd=repo, capture_output=True)
    if p.returncode != 0 and not allow_fail:
        die(f"`git {' '.join(args)}` failed in {repo}:\n"
            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    return p


def check_refs(repo, *refs):
    """Verify every ref resolves, fetching tags once if some are missing.

    A stale clone from an earlier run simply does not have newer tags, and
    `git diff` against a missing tag is a confusing failure at best.
    """
    missing = [r for r in refs
               if run_git(['rev-parse', '--verify', '--quiet', f'{r}^{{commit}}'],
                          repo, allow_fail=True).returncode != 0]
    if not missing:
        return
    print(f"note: {', '.join(missing)} not present in {repo}, fetching tags...",
          file=sys.stderr)
    run_git(['fetch', '--tags', '--quiet'], repo)
    still = [r for r in missing
             if run_git(['rev-parse', '--verify', '--quiet', f'{r}^{{commit}}'],
                        repo, allow_fail=True).returncode != 0]
    if still:
        die(f"unknown git ref(s) after fetching tags: {', '.join(still)}")


def read_blobs(repo, tag, paths):
    """Read many blobs at `tag` in one pass.

    Returns (contents, missing): a {path: text} map and the set of paths that
    do not exist at that tag. Distinguishing "missing" from "failed" is what
    lets callers report a locale added in the new tag instead of silently
    skipping it.
    """
    paths = list(paths)
    if not paths:
        return {}, set()
    req = ''.join(f'{tag}:{p}\n' for p in paths).encode()
    p = subprocess.run(['git', 'cat-file', '--batch'], cwd=repo,
                       input=req, capture_output=True)
    if p.returncode != 0:
        die(f"`git cat-file --batch` failed in {repo}:\n"
            f"{p.stderr.decode('utf-8', 'replace').strip()}")
    out = p.stdout
    contents, missing, pos = {}, set(), 0
    for path in paths:
        nl = out.find(b'\n', pos)
        if nl < 0:
            die(f"truncated `git cat-file --batch` output while reading "
                f"{tag}:{path} (read {len(contents)} of {len(paths)} blobs)")
        header = out[pos:nl].decode('utf-8', 'replace')
        pos = nl + 1
        # "<oid> missing" / "<oid> ambiguous" carry no body.
        if header.endswith(' missing') or header.endswith(' ambiguous'):
            missing.add(path)
            continue
        try:
            size = int(header.split()[2])
        except (IndexError, ValueError):
            die(f"unparsable `git cat-file --batch` header for {tag}:{path}: "
                f"{header!r}")
        contents[path] = out[pos:pos + size].decode('utf-8', 'replace')
        pos += size + 1  # body plus its trailing newline
    return contents, missing


def list_locale_files(repo, tag):
    """Every file under localedata/locales/ at `tag`, as repo-relative paths."""
    out = run_git(['ls-tree', '-r', '--name-only', tag, '--', LOCALES_DIR + '/'],
                  repo).stdout.decode('utf-8', 'replace')
    return [ln for ln in out.splitlines() if ln.strip()]


def collate_block(text):
    """The body of the LC_COLLATE...END LC_COLLATE block, or None."""
    m = _COLLATE_BLOCK_RE.search(text)
    return m.group(1) if m else None


def collate_bounds(text):
    """1-based (start_line, end_line) of the LC_COLLATE block, or None.

    If the block is unterminated, end is the last line of the file, which keeps
    the overlap test conservative (a change is more likely to be flagged).
    """
    lines = text.split('\n')
    start = None
    for idx, line in enumerate(lines, start=1):
        if start is None:
            if line.startswith('LC_COLLATE'):
                start = idx
        elif line.startswith('END LC_COLLATE'):
            return start, idx
    return (start, len(lines)) if start is not None else None


def ellipsis_hits(block, comment_char='%'):
    """Lines of an LC_COLLATE block that use an algorithmic ellipsis range.

    Returns the original lines, stripped, so the caller can show what it found.
    """
    hits = []
    for line in block.split('\n'):
        if ELLIPSIS_RE.search(line.split(comment_char)[0]):
            hits.append(line.strip())
    return hits


def comment_char(text):
    """The locale file's comment character, `%` unless it says otherwise."""
    m = _COMMENT_CHAR_RE.search(text)
    return m.group(1) if m else '%'


def copy_targets(text):
    """Every `copy "..."` target inside LC_COLLATE, in order.

    All of them, not just the first: om_ET copies both am_ET and om_KE, and
    taking only the first hides any change to the second.
    """
    block = collate_block(text)
    return _COPY_RE.findall(block) if block else []


def build_copy_graph(repo, tag):
    """{locale_name: [copy targets]} for every locale with an LC_COLLATE block.

    Locales with no `copy` map to an empty list, so the graph doubles as the
    set of names that define collation at this tag.
    """
    paths = list_locale_files(repo, tag)
    contents, _ = read_blobs(repo, tag, paths)
    graph = {}
    for path, text in contents.items():
        if collate_block(text) is None:
            continue
        graph[os.path.basename(path)] = copy_targets(text)
    return graph


def inherited_from(graph, roots):
    """{locale: [roots it inherits from]} for every locale reaching `roots`.

    Walks all parents, not a single chain, and guards against cycles. Reports
    EVERY root reached, not the first one a depth-first walk happens to find:
    the traversal order is an artefact of `stack.pop()`, and attributing a
    locale to an arbitrary one of several roots makes the "reaches X"
    explanation untrue even when the affected set is right.
    """
    result = {}
    for name in graph:
        if name in roots:
            continue
        seen, stack, hits = set(), list(graph[name]), set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur in roots:
                hits.add(cur)
                continue
            stack.extend(graph.get(cur, ()))
        if hits:
            result[name] = sorted(hits)
    return result


def fan_in(graph):
    """{locale: number of other locales that inherit its LC_COLLATE}."""
    return {name: len(inherited_from(graph, {name})) for name in graph}


_CODESET_RE = re.compile(r'\.([^.@]+)(?=@|$)')


def normalize_locale_name(entry):
    """A SUPPORTED entry as `locale -a` and pg_collation actually spell it.

    localedef normalises the codeset when it builds the locale -- lowercase,
    punctuation dropped, `iso` prefixed to an all-digit codeset -- so
    `sv_SE.UTF-8` is installed, listed and imported into pg_collation as
    `sv_SE.utf8`. Printing the raw SUPPORTED spelling and calling it "the name
    locale -a shows" sends people to `COLLATE "sv_SE.UTF-8"`, which fails with
    `collation ... does not exist`.
    """
    def norm(m):
        cs = ''.join(c for c in m.group(1) if c.isalnum()).lower()
        return '.' + ('iso' + cs if cs.isdigit() else cs)
    return _CODESET_RE.sub(norm, entry)


def supported_map(repo, tag):
    """{source file name: [generated locale names]} from localedata/SUPPORTED.

    The audit works in source file names (sv_FI@euro), but `locale -a` and
    pg_collation show generated names with codesets (sv_FI.utf8, sv_FI). This
    is the mapping between them, in the spelling those tools use.
    """
    contents, missing = read_blobs(repo, tag, ['localedata/SUPPORTED'])
    if missing:
        # Returning {} here made every locale print as "not listed in
        # SUPPORTED, so normally absent from `locale -a`" -- a false and
        # reassuring claim -- and wrote an empty step4 list under the heading
        # "full list of generated names".
        die(f"localedata/SUPPORTED does not exist at {tag}; cannot map source "
            f"file names to the generated names `locale -a` shows.")
    out = {}
    for line in contents['localedata/SUPPORTED'].split('\n'):
        line = line.strip().rstrip('\\').strip()
        if not line or line.startswith('#') or '=' in line:
            continue
        entry = line.split('/')[0].strip()
        if not entry:
            continue
        source = _CODESET_RE.sub('', entry)
        generated = normalize_locale_name(entry)
        if generated not in out.setdefault(source, []):
            out[source].append(generated)
    return out


OUT_DIR = os.environ.get('PG_GLIBC_AUDIT_OUT', '/tmp/pg-glibc-collation-audit')


def write_list(name, items):
    """Write a long result list to OUT_DIR and return the path.

    Keeps terminal output scannable: the scripts print counts and a sample,
    and park the full several-hundred-entry lists here.
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, 'w', encoding='utf-8') as fh:
        for item in items:
            fh.write(f"{item}\n")
    return path


def _main(argv):
    if len(argv) >= 2 and argv[0] == 'fanin':
        repo = find_repo()
        tag = argv[1]
        check_refs(repo, tag)
        graph = build_copy_graph(repo, tag)
        counts = fan_in(graph)
        changed = None
        if len(argv) >= 3:
            with open(argv[2], encoding='utf-8') as fh:
                changed = {os.path.basename(ln.strip())
                           for ln in fh if ln.strip()}
        print(f"Collation templates by blast radius at {tag} "
              f"(locales inheriting via `copy`):")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:10]:
            if n == 0:
                break
            mark = ''
            if changed is not None and name in changed:
                mark = '  <== has some change in this version pair'
            print(f"  {n:4d} locales inherit from {name}{mark}")
        if changed is not None:
            hot = sorted(((counts.get(c, 0), c) for c in changed
                          if counts.get(c, 0) > 0), reverse=True)
            print()
            if not hot:
                print("No changed file is inherited from by any other locale.")
                return 0
            # These files changed SOMEWHERE -- usually LC_TIME or LC_MONETARY.
            # Step 2 decides which of them changed LC_COLLATE. Reported here
            # only so that, if step 2 does flag one, its reach is already
            # visible.
            print(f"Changed files that others inherit from ({len(hot)}) -- "
                  f"reach if the change turns out to be in LC_COLLATE.")
            print("NOT a collation verdict: most of these changed LC_TIME or "
                  "LC_MONETARY. Step 2 filters.")
            for n, name in hot[:8]:
                print(f"  {name}: {n} dependent locale(s)")
            if len(hot) > 8:
                rest = ', '.join(name for _, name in hot[8:])
                print(f"  ... and {len(hot) - 8} more with 1-2 dependents: "
                      f"{rest}")
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
