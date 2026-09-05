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
import re
import sys

import glibc_locale_data as g

# Weight assignment and comparison: a change here can reorder any locale.
TIER1 = [
    'locale/programs/ld-collate.c',   # localedef: ellipsis expansion, weights,
                                      # sections, reorder-after
    'string/strcoll_l.c',             # runtime comparison
    'string/strxfrm_l.c',             # runtime sort-key generation
    'locale/weight.h',
    'locale/weightwc.h',
    'locale/coll-lookup.h',
    # The C locale's own collation sequence. ld-collate.c does
    # `#include "C-collate-seq.c"`, so tracking only ld-collate.c showed the
    # include line and none of the 100 lines of weights behind it. Both change
    # over 2.34..2.39 -- the RHEL9-to-RHEL10 pair.
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
]

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
        out = g.run_git(['ls-tree', '--name-only', tag, '--', path],
                        repo, allow_fail=True)
        return out.returncode == 0 and bool(out.stdout.strip())

    vanished, outside = [], []
    for path in paths:
        at_old, at_new = present(old_tag, path), present(new_tag, path)
        if at_old and not at_new:
            vanished.append(path)
        elif not at_old and not at_new:
            outside.append(path)
    return vanished, outside


def report_file(repo, path, rng, show_all):
    diff_text = g.run_git(['diff', rng, '--', path], repo,
                          allow_fail=True).stdout.decode('utf-8', 'replace')
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
                    repo, allow_fail=True).stdout.decode('utf-8', 'replace')
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

    vanished, outside = check_paths(repo, TIER1 + TIER2,
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
