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

Copyright-year, license-URL and comment-only hunks are filtered out, and the
number filtered is always reported so the filter cannot hide anything
silently. Use --all to see every hunk unfiltered. This tool deliberately
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
]

# Parsing and serialisation: a change here can alter the compiled LC_COLLATE
# tables without touching the weight logic itself.
TIER2 = [
    'locale/programs/linereader.c',   # tokeniser -- how `..` is even parsed
    'locale/programs/locfile.c',      # writes the compiled category files
    'locale/programs/locfile.h',
    'locale/programs/3level.h',       # the weight table representation
]

_HUNK_SPLIT_RE = re.compile(r'^(@@ .*?@@.*)$', re.M)
_CODE_CHARS = set(';{}=()')


def is_noise_line(line):
    """True if a changed line cannot affect behaviour.

    Comment text, copyright years and license URLs only. Anything carrying C
    punctuation is treated as code.
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
    return not (_CODE_CHARS & set(body))


def split_hunks(diff_text):
    """[(header, body_lines)] for one file's diff."""
    parts = _HUNK_SPLIT_RE.split(diff_text)
    hunks = []
    for i in range(1, len(parts), 2):
        body = [ln for ln in parts[i + 1].split('\n')
                if ln[:1] in ('+', '-') and not ln.startswith(('+++', '---'))]
        hunks.append((parts[i], body))
    return hunks


def report_file(repo, path, rng, show_all):
    diff_text = g.run_git(['diff', rng, '--', path], repo,
                          allow_fail=True).stdout.decode('utf-8', 'replace')
    if not diff_text.strip():
        return 0
    hunks = split_hunks(diff_text)
    kept, filtered = [], 0
    for header, body in hunks:
        if show_all or any(not is_noise_line(ln) for ln in body):
            kept.append((header, body))
        else:
            filtered += 1

    if not kept:
        print(f"  {path}: no substantive change "
              f"({filtered} comment/copyright hunk(s) filtered)")
        return 0

    note = f", {filtered} comment/copyright hunk(s) filtered" if filtered else ""
    print(f"  {path}: {len(kept)} substantive hunk(s){note}")
    for header, body in kept:
        print(f"      {header.strip()}")
        for ln in body:
            marker = '  ' if is_noise_line(ln) else '>>'
            print(f"      {marker} {ln}")
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
    if total == 0:
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
