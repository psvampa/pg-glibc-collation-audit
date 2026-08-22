#!/usr/bin/env python3
"""
A partir de un diff unificado (-U0) de localedata/locales/ entre dos tags de glibc,
determina cuales de los archivos cambiados tienen el cambio DENTRO de la seccion
LC_COLLATE...END LC_COLLATE (la unica que afecta el orden de sort), descartando
cambios en LC_TIME, LC_MONETARY, comentarios, etc.

Uso:
  cd glibc
  git diff -U0 <tag_viejo>..<tag_nuevo> -- localedata/locales/ > /tmp/localedata_full.diff
  python3 filter_lc_collate_changes.py <tag_viejo>
"""
import re, subprocess, sys

old_tag = sys.argv[1] if len(sys.argv) > 1 else "glibc-2.28"
diff_text = open('/tmp/localedata_full.diff', encoding='utf-8', errors='replace').read()

file_blocks = re.split(r'^diff --git a/(localedata/locales/\S+) b/\S+$', diff_text, flags=re.M)
files = {}
for i in range(1, len(file_blocks), 2):
    fname = file_blocks[i]
    content = file_blocks[i + 1]
    hunks = re.findall(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', content, flags=re.M)
    ranges = []
    for start, length in hunks:
        start = int(start)
        length = int(length) if length else 1
        ranges.append((start, start + length))
    files[fname] = ranges

changed_lc_collate = []
for fname, ranges in files.items():
    try:
        old = subprocess.run(['git', 'show', f'{old_tag}:{fname}'],
                              capture_output=True, timeout=30).stdout.decode('utf-8', errors='replace')
    except Exception:
        continue
    lines = old.split('\n')
    lc_start = lc_end = None
    for idx, line in enumerate(lines, start=1):
        if line.startswith('LC_COLLATE'):
            lc_start = idx
        elif line.startswith('END LC_COLLATE'):
            lc_end = idx
            break
    if lc_start is None:
        continue
    if lc_end is None:
        lc_end = len(lines)
    overlap = any(not (end < lc_start or start > lc_end) for start, end in ranges)
    if overlap:
        changed_lc_collate.append(fname)

print(f"Archivos chequeados: {len(files)}")
print(f"Archivos con cambios dentro de LC_COLLATE: {len(changed_lc_collate)}")
for fn in sorted(changed_lc_collate):
    print(fn)
