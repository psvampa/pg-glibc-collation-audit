#!/usr/bin/env bash
# Usage: ./audit-locale-diff.sh <old_tag> <new_tag>
# Example: ./audit-locale-diff.sh glibc-2.28 glibc-2.34
#
# Step 1 of the audit: for any pair of glibc tags, list which locale files
# under localedata/locales/ changed, and report the blast radius of each change
# through the LC_COLLATE `copy` graph.
#
# This used to hardcode iso14651_t1_common as "the master collation table" and
# signal "unchanged" by printing nothing -- indistinguishable from an error --
# and it missed that iso14651_t1 (a different file) is the one 328 locales
# actually inherit from. The fan-in is now computed from the copy graph.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <old_tag> <new_tag>" >&2
  exit 2
fi
OLD=$1
NEW=$2
HERE=$(cd "$(dirname "$0")" && pwd)

if [ ! -d "$HERE/glibc" ]; then
  echo "Cloning glibc (shallow, blobs on demand)..."
  git clone --filter=blob:none --no-checkout \
    https://github.com/bminor/glibc.git "$HERE/glibc"
fi
cd "$HERE/glibc"

# A clone left over from an earlier run simply does not have newer tags, and
# `git diff` against a missing ref is a confusing failure at best.
missing=""
for ref in "$OLD" "$NEW"; do
  git rev-parse --verify --quiet "${ref}^{commit}" >/dev/null || missing="$missing $ref"
done
if [ -n "$missing" ]; then
  echo "Refs not present locally:$missing -- fetching tags..."
  git fetch --tags --quiet
  for ref in "$OLD" "$NEW"; do
    if ! git rev-parse --verify --quiet "${ref}^{commit}" >/dev/null; then
      echo "error: unknown git ref '$ref' even after fetching tags" >&2
      exit 2
    fi
  done
fi

OUT_DIR=${PG_GLIBC_AUDIT_OUT:-/tmp/pg-glibc-collation-audit}
mkdir -p "$OUT_DIR"
CHANGED="$OUT_DIR/changed_locales.txt"

git diff --name-only "$OLD..$NEW" -- localedata/locales/ | sort > "$CHANGED"
echo "Total locale files with content changes: $(wc -l < "$CHANGED" | tr -d ' ')"
echo "Full list in $CHANGED"
echo "---"

# Explicit verdict per collation template. `git diff --quiet` exits non-zero
# when there are differences, so a real error can no longer read as
# "unchanged" the way empty `--stat` output did.
echo "Collation templates -- did they change?"
for tmpl in iso14651_t1 iso14651_t1_common iso14651_t1_pinyin; do
  if ! git cat-file -e "$NEW:localedata/locales/$tmpl" 2>/dev/null; then
    printf '  %-22s ABSENT at %s\n' "$tmpl" "$NEW"
  elif git diff --quiet "$OLD..$NEW" -- "localedata/locales/$tmpl"; then
    printf '  %-22s UNCHANGED\n' "$tmpl"
  else
    printf '  %-22s CHANGED  <== affects everything that inherits it\n' "$tmpl"
  fi
done
echo "---"

python3 "$HERE/glibc_locale_data.py" fanin "$NEW" "$CHANGED"
echo "---"
echo "Next: python3 filter_lc_collate_changes.py $OLD $NEW"
