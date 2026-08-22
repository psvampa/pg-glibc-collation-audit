#!/usr/bin/env python3
"""
Given the set of locale files with a REAL (non-inherited) LC_COLLATE change
between two glibc tags (output of filter_lc_collate_changes.py), find every
OTHER locale file that inherits its LC_COLLATE via `copy "<name>"` from one
of those changed files -- directly or transitively.

This closes a gap in filter_lc_collate_changes.py: that script only flags
files whose OWN LC_COLLATE block changed. A locale that just does
`copy "sv_SE"` and adds no tailoring of its own will never show up in a
source diff (its file didn't change), but its actual sort order changes
whenever sv_SE's does.

Usage:
  python3 resolve_copy_closure.py <tag> <changed_file1> [<changed_file2> ...]

Example:
  python3 resolve_copy_closure.py glibc-2.34 or_IN sv_SE
"""
import re, subprocess, sys

tag = sys.argv[1]
changed_direct = set(sys.argv[2:])

files = subprocess.run(
    ['git', 'ls-tree', '-r', '--name-only', tag, '--', 'localedata/locales/'],
    capture_output=True, timeout=30
).stdout.decode('utf-8', errors='replace').splitlines()

copy_of = {}
for f in files:
    name = f.split('/')[-1]
    content = subprocess.run(['git', 'show', f'{tag}:{f}'],
                              capture_output=True, timeout=30).stdout.decode('utf-8', errors='replace')
    m = re.search(r'\nLC_COLLATE\b(.*?)\nEND LC_COLLATE', content, re.S)
    if not m:
        continue
    cm = re.search(r'copy\s+"([^"]+)"', m.group(1))
    if cm:
        copy_of[name] = cm.group(1)

def resolve_chain(name, seen=None):
    if seen is None:
        seen = []
    if name in seen:
        return seen
    seen = seen + [name]
    if name in copy_of:
        return resolve_chain(copy_of[name], seen)
    return seen

affected_by_inheritance = {}
for loc in copy_of:
    if loc in changed_direct:
        continue
    chain = resolve_chain(loc)
    hit = next((c for c in chain[1:] if c in changed_direct), None)
    if hit:
        affected_by_inheritance[loc] = hit

print(f"Directly changed (own LC_COLLATE diff): {sorted(changed_direct)}")
print(f"Additionally affected via copy-chain inheritance: {len(affected_by_inheritance)}")
for loc, via in sorted(affected_by_inheritance.items()):
    print(f"  {loc} -> copy \"{via}\"")
print()
print(f"Full affected set (locale identifiers): {sorted(changed_direct | set(affected_by_inheritance))}")
