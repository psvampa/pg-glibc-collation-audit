#!/usr/bin/env python3
"""
Find locales whose LC_COLLATE section relies on range-expansion syntax
(a line "<UXXXX>", a line "..", a line "<UYYYY>") instead of listing every
character's collation weight explicitly.

Why this matters: a plain source diff (audit-locale-diff.sh +
filter_lc_collate_changes.py) proves that a locale's sort order didn't
change ONLY if every character's weight is spelled out in the data file
itself. A range like "<UAC00> .. <UD7A3>" (all 11,172 precomposed Hangul
syllables, used only by ko_KR as of glibc 2.34) is NOT expanded in the data
file -- glibc's locale compiler (localedef) expands it algorithmically at
build time. If that expansion logic changes between two glibc releases,
every character in the range can get a different weight with ZERO change to
the locale's own source file, and the diff-based audit would wrongly report
"unaffected".

A locale flagged by this script needs an empirical sort-order test on both
glibc versions before you trust a "not flagged by the diff" result -- the
diff alone is not sufficient for it, unlike every other locale in glibc.

Usage:
  python3 flag_algorithmic_ranges.py <tag>

Example:
  python3 flag_algorithmic_ranges.py glibc-2.34
"""
import re, subprocess, sys

tag = sys.argv[1] if len(sys.argv) > 1 else "glibc-2.34"

files = subprocess.run(
    ['git', 'ls-tree', '-r', '--name-only', tag, '--', 'localedata/locales/'],
    capture_output=True, timeout=30
).stdout.decode('utf-8', errors='replace').splitlines()

flagged = []
for f in files:
    name = f.split('/')[-1]
    content = subprocess.run(['git', 'show', f'{tag}:{f}'],
                              capture_output=True, timeout=30).stdout.decode('utf-8', errors='replace')
    m = re.search(r'\nLC_COLLATE\b(.*?)\nEND LC_COLLATE', content, re.S)
    if not m:
        continue
    block = m.group(1)
    if re.search(r'^\.\.$', block, re.M):
        flagged.append(name)

print(f"Locales at {tag} whose LC_COLLATE uses range-expansion syntax (algorithmic, not explicit): {len(flagged)}")
for name in sorted(flagged):
    print(f"  {name}")
print()
print("These need an empirical sort-order test on both glibc versions before trusting")
print("a 'not flagged by the diff' result -- the source diff alone cannot prove their")
print("order is unchanged, because the actual weights are computed by localedef, not")
print("stored in the locale file.")
