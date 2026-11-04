#!/usr/bin/env bash
# Usage: ./audit-locale-diff.sh <old> <new> [--allow-reverse]
# Example: ./audit-locale-diff.sh glibc-2.28 glibc-2.34
#          ./audit-locale-diff.sh glibc-2.28-251.el8_10.40 glibc-2.34-275.el9_8
#
# Step 1 of the audit: for a pair of glibc tags, or of builds named as
# `rpm -q glibc` names them without the architecture, OLD first, list which
# locale files under localedata/locales/ changed, and report the blast radius
# of each change through the LC_COLLATE `copy` graph. A pair given the other
# way round is refused here rather than reported on, unless --allow-reverse
# says it is deliberate.
#
# This used to hardcode iso14651_t1_common as "the master collation table" and
# signal "unchanged" by printing nothing -- indistinguishable from an error --
# and it missed that iso14651_t1 (a different file) is the one hundreds of
# locales actually inherit from. The fan-in is now computed from what
# localedef reads to build each locale (glibc_tree), every `copy` followed.
set -euo pipefail

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  echo "usage: $0 <old> <new> [--allow-reverse]" >&2
  exit 2
fi
OLD=$1
NEW=$2
ALLOW=${3:-}
if [ -n "$ALLOW" ] && [ "$ALLOW" != --allow-reverse ]; then
  echo "error: unknown argument '$ALLOW'" >&2
  exit 2
fi
HERE=$(cd "$(dirname "$0")" && pwd)

# The two sides, before a single finding is printed: glibc_locale_data.py's
# step1_sides, the one place step 1 asks what each argument is. A tag of
# the clone is read by its name; a build named as `rpm -q glibc` names it
# without the architecture (glibc-2.34-283.el9_8) is fetched, rebuilt from
# its source RPM and made a commit of the clone again, and read by that
# commit, never by a name. It also does what this script used to do here:
# clone glibc when it is not there (full history, blobs on demand: step 5
# asks `git log` about paths absent at both refs), fetch tags once when a
# tag is missing, refuse a reversed pair unless --allow-reverse, print the
# provenance -- the clone is a third-party mirror and a tag is a mutable
# pointer -- and hold both sides to the corpus floor, so that an empty diff
# cannot read as "nothing changed". With two tags every line is printed as
# it was. The two refs come back in a file this run makes, a line each.
REFS=$(mktemp "${TMPDIR:-/tmp}/pg-glibc-sides.XXXXXX")
trap 'rm -f "$REFS"' EXIT
python3 "$HERE/glibc_locale_data.py" sides ${ALLOW:+"$ALLOW"} \
  --refs-file "$REFS" "$OLD" "$NEW"
OLD_REF=$(sed -n 1p "$REFS")
NEW_REF=$(sed -n 2p "$REFS")
if [ -z "$OLD_REF" ] || [ -z "$NEW_REF" ]; then
  echo "error: the sides of $OLD and $NEW were not written to $REFS" >&2
  exit 2
fi
cd "$HERE/glibc"

OUT_DIR=${PG_GLIBC_AUDIT_OUT:-/tmp/pg-glibc-collation-audit}
mkdir -p "$OUT_DIR"
CHANGED="$OUT_DIR/changed_locales.txt"

# Rename detection pinned on, its limit lifted, and colour off, so the count
# does not depend on the user's git config: with diff.renames=false OR a low
# diff.renameLimit the one rename over 2.34..2.39 is listed as two paths and
# the published 318 reads 319. --find-renames alone does not override the
# limit -- measured: renameLimit=1 skipped detection with only a warning.
# diff.algorithm and diff.indentHeuristic are pinned too, as in the Python
# steps' GIT_CONFIG_OVERRIDES. A list of names does not depend on them today;
# pinned, it cannot start to.
git -c color.ui=false -c diff.renames=true -c diff.renameLimit=0 \
  -c diff.algorithm=myers -c diff.indentHeuristic=true \
  diff --find-renames --name-only "$OLD_REF..$NEW_REF" -- localedata/locales/ \
  | sort > "$CHANGED"
# --name-only lists additions and deletions too, so this is not a count of
# modified files. Step 2 separates them.
echo "Locale files added, modified or deleted: $(wc -l < "$CHANGED" | tr -d ' ')"
echo "Full list in $CHANGED"
echo "---"

# Explicit verdict per collation template. `git diff --quiet` exits non-zero
# when there are differences, so a real error can no longer read as
# "unchanged" the way empty `--stat` output did.
#
# --no-ext-diff, because `--quiet` normally ignores an external helper but
# obeys `diff.trustExitCode` / GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE, and then the
# helper's exit code IS the verdict. Measured on localedata/locales/sv_SE over
# 2.28..2.34: plain, exit 1 (changed); with GIT_EXTERNAL_DIFF=/usr/bin/true and
# that variable set, exit 0 -- printed as UNCHANGED for a template every locale
# inherits from.
#
# Existence is asked with `ls-tree`, not `cat-file -e`: on a
# `--filter=blob:none` clone the latter must fetch the blob to answer, and
# calls a file that exists ABSENT whenever that fetch cannot happen.
echo "Collation templates -- did they change?"
for tmpl in iso14651_t1 iso14651_t1_common iso14651_t1_pinyin; do
  if [ -z "$(git ls-tree --name-only "$NEW_REF" -- "localedata/locales/$tmpl")" ]; then
    printf '  %-22s ABSENT at %s\n' "$tmpl" "$NEW"
  elif git diff --quiet --no-ext-diff "$OLD_REF..$NEW_REF" \
       -- "localedata/locales/$tmpl"; then
    printf '  %-22s UNCHANGED\n' "$tmpl"
  else
    printf '  %-22s CHANGED  <== affects everything that inherits it\n' "$tmpl"
  fi
done
echo "---"

# A build's commit is printed by the build's name.
if [ "$NEW_REF" = "$NEW" ]; then
  python3 "$HERE/glibc_locale_data.py" fanin "$NEW" "$CHANGED"
else
  python3 "$HERE/glibc_locale_data.py" fanin "$NEW_REF" "$CHANGED" --as "$NEW"
fi
echo "---"
# Under audit.sh this hint names a step that has already run. Only the hint is
# suppressed; every verdict above is printed either way.
# By the names given: each step after this one reads a build by its name.
if [ "${PG_GLIBC_AUDIT_WRAPPED:-}" != "1" ]; then
  echo "Next: python3 filter_lc_collate_changes.py $OLD $NEW"
fi
