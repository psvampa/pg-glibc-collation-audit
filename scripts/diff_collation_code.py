#!/usr/bin/env python3
"""
Diff the glibc CODE that turns locale data into collation weights, between two
tags.

Steps 1-3 of this audit compare locale data files. That is not the whole
input: identical locale data compiled by a different localedef, or compared by
a different strcoll, can sort differently. Between glibc 2.28 (RHEL8) and 2.34
(RHEL9) that is exactly what happened -- commit 82292c99b2 ("LC_COLLATE: Fix
last character ellipsis handling", Bug 22668) changed how localedef expands
ellipsis ranges, and ko_KR's sort order changed on RHEL9 even though
localedata/locales/ko_KR is byte-identical between the two tags. A data-only
diff reports ko_KR as unaffected. It is not.

Run this together with flag_algorithmic_ranges.py: that script says which
locales depend on code-computed weights, this one says whether the code that
computes them changed.

Comment, attribution and licence hunks are filtered out, and the number
filtered is always reported so the filter cannot hide anything silently. The
filter only drops what it can PROVE is prose -- a preprocessor directive, a
label or a bare declarator counts as code, because a hunk that is dropped
here is a hunk nobody reads. Use --all to see every hunk unfiltered. This tool deliberately
reports for human judgement rather than declaring a verdict: over
2.28..2.34 it surfaces both the ellipsis fix (which reorders ko_KR) and a
hash-table sizing change (which does not), and only a human can tell them
apart.

Usage:
  python3 diff_collation_code.py <old_tag> <new_tag> [--repo <path>] [--all]

Example:
  python3 diff_collation_code.py glibc-2.28 glibc-2.34
"""
import argparse
import os
import re
import sys

import glibc_locale_data as g

# The code that turns locale data into weights reaches this audit two ways.
#
# TIER1/TIER2 below are CURATED: a human ranking of what to read first. They are
# no longer the ceiling of what gets read -- reachable_from_entry_points() walks
# glibc's own #include graph and everything it finds is reported too, under
# TIER 3. Before this, a file absent from these lists was indistinguishable from
# a file that did not change, and `locale/programs/linereader.h` (lr_getc) and
# `locale/elem-hash.h` (elem_hash) both changed over 2.34..2.39 unseen.
#
# So do NOT add a path here just because it looks collation-related: if the
# include walk already reaches it, listing it here only freezes by hand what the
# walk derives. Add a path only when the walk STRUCTURALLY cannot reach it, and
# say which blind spot it falls into. There are two, both marked below:
#
#   (macro)  reached by a macro-computed #include, which a regex cannot resolve
#   (TU)     a separately compiled translation unit with no header of its own,
#            linked rather than included
ENTRY_POINTS = [
    'locale/programs/ld-collate.c',   # localedef: the collation compiler
    'string/strcoll_l.c',             # runtime comparison
    'string/strxfrm_l.c',             # runtime sort-key generation
    'wcsmbs/wcscoll_l.c',             # the wide-char variants, which #define
    'wcsmbs/wcsxfrm_l.c',             # their way into the two above
    'locale/loadlocale.c',            # reads the compiled tables back in
]

# Weight assignment and comparison: a change here can reorder any locale.
TIER1 = [
    'locale/programs/ld-collate.c',   # localedef: ellipsis expansion, weights,
                                      # sections, reorder-after
    'string/strcoll_l.c',             # runtime comparison
    'string/strxfrm_l.c',             # runtime sort-key generation
    # (macro) strcoll_l.c reaches these as `#include WEIGHT_H`, where WEIGHT_H is
    # defined by whoever includes IT -- weight.h for the narrow build, weightwc.h
    # for the wide one. Nothing resolves that without a preprocessor, and
    # locale/weight.h does change over 2.34..2.39.
    'locale/weight.h',
    'locale/weightwc.h',
    'locale/coll-lookup.h',
    # (TU) __collidx_table_lookup, compiled and linked, included by nobody.
    'locale/coll-lookup.c',
    # (TU) installs the _NL_COLLATE_* pointers when a locale is loaded.
    'locale/lc-collate.c',
    # The C locale's own collation sequence. ld-collate.c does
    # `#include "C-collate-seq.c"`, so tracking only ld-collate.c showed the
    # include line and none of the 100 lines of weights behind it. Both change
    # over 2.34..2.39 -- the RHEL9-to-RHEL10 pair.
    # (TU) C-collate.c has no header; the walk cannot reach it.
    'locale/C-collate.c',
    'locale/C-collate-seq.c',
]

# Parsing and serialisation: a change here can alter the compiled LC_COLLATE
# tables without touching the weight logic itself.
TIER2 = [
    'locale/programs/linereader.c',   # tokeniser -- how `..` is even parsed
    'locale/programs/locfile.c',      # writes the compiled category files
    'locale/programs/locfile.h',
    'locale/programs/3level.h',       # the weight table representation
    'locale/loadlocale.c',            # reads the compiled tables back in
    'locale/localeinfo.h',            # the structs those tables live in
    # The keyword table is generated from this by gperf. The generated
    # locfile-kw.h IS reachable, but it is a machine-built hash table -- 20
    # substantive hunks over 2.34..2.39, none of them readable. This is the
    # source those hunks mean, one line per keyword.
    'locale/programs/locfile-kw.gperf',
]

# Where the include walk descends. Following #include anywhere pulls in all of
# libc -- measured at 265 files and 243 substantive hunks over 2.34..2.39,
# dominated by stdio.h, unistd.h and sys/cdefs.h, none of which can move a
# collation weight. Bounded to locale/, the same walk yields 28 files and finds
# exactly the collation code.
_WALK_PREFIX = 'locale/'

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)

# Where an #include is looked up: the including file's own directory first, then
# glibc's include roots. Enough to resolve everything under locale/; anything
# unresolved is by construction outside _WALK_PREFIX and would be dropped anyway.
_INCLUDE_ROOTS = ['', 'include/', 'locale/', 'locale/programs/', 'string/',
                  'wcsmbs/']

_HUNK_SPLIT_RE = re.compile(r'^(@@ .*?@@.*)$', re.M)
_ATTRIBUTION_RE = re.compile(r'^(Contributed by|Written by)\b')


def is_noise_line(line):
    """True if a changed line is provably comment or licence text.

    The test is deliberately one-sided: a line is noise only when it can be
    SHOWN to be a comment, an attribution or a licence reference. Everything
    else -- preprocessor directives, labels, bare declarators, a lone `else`
    -- is code.

    The rule used to run the other way: noise unless the line carried one of
    `;{}=()`. That silently swallowed real changes, and because a hunk is
    dropped only when every line is noise, whole hunks disappeared under the
    heading "no substantive change":

      * `+#include "C-collate-seq.c"` in ld-collate.c   (2.34 -> 2.41)
      * `-#define NO_FINALIZE` / `+#define NO_ADD_LOCALE` (2.17 -> 2.28)
      * `-# define STRCMP strcmp` in strxfrm_l.c        (2.17 -> 2.28)

    and surviving hunks showed lines like `case tok_codepoint_collation:`
    without the `>>` marker a reader is told to scan for. In a tool whose
    zero-hunk verdict is "every locale whose data file is unchanged is
    genuinely unaffected", guessing wrong in that direction is the one
    failure that matters.
    """
    body = line[1:].strip()
    if not body:
        return True
    if body.startswith(('/*', '*', '//')):
        return True
    # Continuation line that closes a block comment.
    if body.endswith('*/') and '/*' not in body:
        return True
    if 'Copyright (C)' in body or 'gnu.org/licenses' in body \
       or 'fsf.org' in body:
        return True
    # "Contributed by Ulrich Drepper <drepper@gnu.org>, 1995." -- a bare
    # continuation of the file's header comment, with no leading `*`.
    return bool(_ATTRIBUTION_RE.match(body))


def classify_body(body):
    """[(line, is_noise)] for one hunk, tracking block comments per side.

    is_noise_line() alone cannot see that

        +  /* Compare the file with the locale data files for the same category
        +     in other locales, and see if we can reuse it, to save disk space.

    is one comment: the second line neither opens with `*` nor closes with
    `*/`. Walking the hunk and carrying the open-comment state (separately for
    the + and - sides, which are two different versions of the file) keeps
    those continuations out of the `>>` markers a reader is told to scan.

    A preprocessor line is never swallowed by this, so a stray `/*` inside a
    string literal cannot hide a `#include` or `#define`.
    """
    state = {'+': False, '-': False}
    out = []
    for line in body:
        side, text = line[:1], line[1:].strip()
        noise = (state.get(side, False) and not text.startswith('#')) \
            or is_noise_line(line)
        last_open, last_close = text.rfind('/*'), text.rfind('*/')
        if last_open > last_close:
            state[side] = True
        elif last_close > last_open:
            state[side] = False
        out.append((line, noise))
    return out


def split_hunks(diff_text):
    """[(header, body_lines)] for one file's diff."""
    parts = _HUNK_SPLIT_RE.split(diff_text)
    hunks = []
    for i in range(1, len(parts), 2):
        body = [ln for ln in parts[i + 1].split('\n')
                if ln[:1] in ('+', '-') and not ln.startswith(('+++', '---'))]
        hunks.append((parts[i], body))
    return hunks


def _tracked_files(repo, tag):
    """Every path in the tree at `tag`, as a set. One ls-tree, no blob fetch."""
    out = g.run_git(['ls-tree', '-r', '--name-only', tag], repo)
    return set(out.stdout.decode('utf-8', 'replace').split('\n')) - {''}


def reachable_from_entry_points(repo, tag):
    """Collation code reachable from ENTRY_POINTS by #include, at `tag`.

    The point of this is that TIER1/TIER2 stop being the ceiling. A hand list
    can only contain what somebody thought of; this walks what glibc actually
    includes, so a file that becomes part of the collation path in a future
    release is picked up without anyone editing a list.

    Two bounds keep it useful rather than merely complete:

      * The walk descends only into _WALK_PREFIX. Unbounded, it reaches 265
        files whose diffs are dominated by stdio.h and sys/cdefs.h -- correct,
        and unreadable.
      * For every header reached, the sibling .c is added if it exists. glibc
        links coll-lookup.c, simple-hash.c and locfile.c rather than including
        them, so an #include walk alone sees their declarations and never their
        code.

    What it CANNOT see is recorded in the TIER1/TIER2 comments: macro-computed
    includes (`#include WEIGHT_H`) and translation units with no header at all.
    Those are why the curated lists still exist.
    """
    tracked = _tracked_files(repo, tag)

    def resolve(inc, from_dir):
        for base in ([from_dir + '/'] if from_dir else []) + _INCLUDE_ROOTS:
            cand = os.path.normpath(base + inc) if base else inc
            if cand in tracked:
                return cand
        return None

    seen, frontier = set(), [p for p in ENTRY_POINTS if p in tracked]
    while frontier:
        batch = [p for p in frontier if p not in seen]
        seen.update(batch)
        # Strict: every path here came out of the tree at this same tag, so
        # an unreadable one is a failed read, not an absent file. Letting it
        # through would quietly shorten the walk, and a shorter walk reports
        # fewer changed files -- the reassuring direction.
        contents = g.read_blobs_strict(repo, tag, batch,
                                       'the collation include walk')
        frontier = []
        for path in batch:
            text = contents.get(path)
            if text is None:
                continue
            for inc in _INCLUDE_RE.findall(text):
                target = resolve(inc, os.path.dirname(path))
                if (target is not None and target not in seen
                        and target.startswith(_WALK_PREFIX)):
                    frontier.append(target)

    siblings = {p[:-2] + '.c' for p in seen if p.endswith('.h')}
    return (seen | (siblings & tracked)) - set(ENTRY_POINTS)


def check_paths(repo, paths, old_tag, new_tag):
    """Classify tracked paths that do not exist at both tags.

    Returns (vanished, outside): paths present at `old_tag` but gone at
    `new_tag`, and paths present at neither.

    Only `vanished` is a blind spot. `git diff` over a path that exists at
    neither tag is empty and exits 0, but so is the truth -- the file has
    nothing to say about that range. A file that appears only at the new tag
    is likewise fine: the diff shows it added, in full. What `git diff` cannot
    tell you about is a file that was renamed out from under the audit, which
    reads exactly like "this file did not change".

    Existence is asked with `ls-tree`, not `cat-file -e`. On a
    `--filter=blob:none` clone `cat-file -e` must fetch the blob to answer,
    and calls a file that exists absent whenever that fetch cannot happen --
    offline, or against a dead promisor. `ls-tree` reads the tree, which such
    a clone always has.
    """
    def present(tag, path):
        # No allow_fail: `git ls-tree` exits 0 with empty output for a path
        # that is not in the tree, and non-zero only on a real error (a bad
        # tag, an unreadable object). Suppressing that turned an error into
        # `False` for BOTH tags, which check_paths then filed under "exists at
        # neither tag -- nothing to read, and nothing to miss" and printed as
        # harmless, for a file that exists and was never read.
        out = g.run_git(['ls-tree', '--name-only', tag, '--', path], repo)
        return bool(out.stdout.strip())

    vanished, outside = [], []
    for path in paths:
        at_old, at_new = present(old_tag, path), present(new_tag, path)
        if at_old and not at_new:
            vanished.append(path)
        elif not at_old and not at_new:
            outside.append(path)
    return vanished, outside


def report_file(repo, path, rng, show_all, quiet_when_clean=False):
    """Print one file's substantive hunks; return how many there were.

    `quiet_when_clean` suppresses the "no substantive change" line. TIER 1 and
    TIER 2 are curated and short, so naming every file that was checked is the
    point. TIER 3 is derived and mostly clean -- 14 of 20 files over
    2.34..2.39 -- and a line each buries the two hunks that matter. The count
    is reported in the coverage line instead, so nothing goes unaccounted for.
    """
    # No allow_fail: `git diff` without --quiet exits 0 whether or not there
    # are differences, so a non-zero exit is always a real error. With it
    # suppressed, a failed diff gave empty stdout and returned 0 here -- the
    # file was reported as having no substantive change, without a word.
    diff_text = g.run_git(['diff', rng, '--', path],
                          repo).stdout.decode('utf-8', 'replace')
    if not diff_text.strip():
        return 0
    hunks = split_hunks(diff_text)
    kept, filtered = [], 0
    for header, body in hunks:
        marked = classify_body(body)
        if show_all or any(not noise for _, noise in marked):
            kept.append((header, marked))
        else:
            filtered += 1

    if not kept:
        if not quiet_when_clean:
            print(f"  {path}: no substantive change "
                  f"({filtered} comment/licence hunk(s) filtered)")
        return 0

    note = f", {filtered} comment/licence hunk(s) filtered" if filtered else ""
    print(f"  {path}: {len(kept)} substantive hunk(s){note}")
    for header, marked in kept:
        print(f"      {header.strip()}")
        for ln, noise in marked:
            print(f"      {'  ' if noise else '>>'} {ln}")
    log = g.run_git(['log', '--oneline', '--no-merges', rng, '--', path],
                    repo).stdout.decode('utf-8', 'replace')
    if log.strip():
        print("      commits:")
        for line in log.strip().split('\n'):
            print(f"        {line}")
    return len(kept)


def main(argv):
    ap = argparse.ArgumentParser(
        description="Diff glibc's collation code between two tags.")
    ap.add_argument('old_tag')
    ap.add_argument('new_tag')
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    ap.add_argument('--all', action='store_true',
                    help="show every hunk, including comment-only ones")
    opts = ap.parse_args(argv)

    repo = g.find_repo(opts.repo)
    g.check_refs(repo, opts.old_tag, opts.new_tag)
    rng = f'{opts.old_tag}..{opts.new_tag}'

    print(f"Collation code changes between {opts.old_tag} and {opts.new_tag}")
    print()

    # Derived at BOTH tags and unioned: the walk at the new tag alone cannot
    # see a file that existed at the old one and was removed or renamed away,
    # which is the same "reads exactly like unchanged" failure check_paths()
    # exists for.
    derived = (reachable_from_entry_points(repo, opts.old_tag)
               | reachable_from_entry_points(repo, opts.new_tag))
    tiered = set(TIER1) | set(TIER2)
    tier3 = sorted(derived - tiered)

    # ENTRY_POINTS included: if one is renamed away the whole walk collapses to
    # nothing, and a collapsed walk reads exactly like a clean result.
    vanished, outside = check_paths(repo, ENTRY_POINTS + TIER1 + TIER2,
                                    opts.old_tag, opts.new_tag)
    if vanished:
        print(f"Tracked files present at {opts.old_tag} and GONE at "
              f"{opts.new_tag}. `git diff` over a")
        print("missing path is empty, not an error, so a rename reads exactly "
              "like")
        print('"unchanged":')
        for path in vanished:
            print(f"  {path}: ABSENT at {opts.new_tag}")
        print("  Find where each moved and add the new path to TIER1/TIER2 "
              "before trusting")
        print("  a no-change result.")
        print()
    if outside:
        print("Tracked files that exist at neither tag -- nothing to read, and "
              "nothing to")
        print("miss, for this version range:")
        for path in outside:
            print(f"  {path}")
        print()

    total = 0
    print("TIER 1 -- weight assignment and comparison")
    print("  (a change here can reorder any locale, including ones whose data "
          "file is unchanged)")
    for path in TIER1:
        total += report_file(repo, path, rng, opts.all)
    print()
    print("TIER 2 -- parsing and serialisation")
    print("  (a change here can alter the compiled tables without touching the "
          "weight logic)")
    tier2 = 0
    for path in TIER2:
        tier2 += report_file(repo, path, rng, opts.all)
    total += tier2

    print()
    print("TIER 3 -- reachable from the collation entry points, not classified")
    print("  (derived by walking #include from ld-collate.c, strcoll_l.c, "
          "strxfrm_l.c and the")
    print("   wide-char variants, bounded to locale/, plus the sibling .c of "
          "every header")
    print("   reached -- so this list grows on its own as glibc changes)")
    tier3_clean = 0
    for path in tier3:
        n = report_file(repo, path, rng, opts.all, quiet_when_clean=True)
        total += n
        if n == 0:
            tier3_clean += 1
    if tier3_clean:
        print(f"  ({tier3_clean} further file(s) reached and read, with no "
              f"substantive change)")

    print()
    print(f"Coverage: {len(derived)} file(s) reached by the include walk, "
          f"{len(tier3)} of them beyond")
    print(f"TIER 1/2. The walk cannot follow a macro-computed include "
          f"(`#include WEIGHT_H`) or")
    print(f"reach a translation unit with no header of its own -- "
          f"locale/weight.h, weightwc.h,")
    print(f"lc-collate.c and C-collate.c are in TIER 1 by hand for exactly "
          f"that reason.")

    print()
    if total == 0 and vanished:
        print(f"No substantive change in the files this audit could read -- "
              f"but {len(vanished)} tracked")
        print(f"path(s) vanished before {opts.new_tag}, so this is NOT a clean "
              f"result.")
        print("Resolve the paths listed above, then re-run.")
    elif total == 0:
        print("No substantive collation code change. Every locale whose data "
              "file is unchanged")
        print("is genuinely unaffected, including the algorithmic-range "
              "locales that")
        print("flag_algorithmic_ranges.py lists -- steps 1-3 are sufficient "
              "for this pair.")
    else:
        print(f"{total} substantive hunk(s) found. Locale data alone does not "
              f"settle this pair.")
        print("Lines marked >> are the code changes; read them and decide "
              "whether they can")
        print("move weights. If any can, every locale listed by")
        print(f"  python3 flag_algorithmic_ranges.py {opts.new_tag}")
        print("needs an empirical sort-order test, however clean its data diff "
              "is.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
