#!/usr/bin/env bash
#
# Run the whole glibc collation audit for one version pair.
#
# The five steps exist because the method has five distinct questions, not
# because a user should have to type five commands. This runs them in order,
# hands step 2's result to step 3 so nobody has to retype locale names, and
# ends with a consolidated summary.
#
# The individual scripts keep working on their own -- see docs/method.md. Use
# them to re-run one step against a hand-picked locale list.
#
# Usage:
#   ./audit.sh <old_tag> <new_tag>
#
# Example (the tags are examples -- run `ldd --version` on each node):
#   ./audit.sh glibc-2.28 glibc-2.34
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <old_tag> <new_tag>" >&2
  echo "       e.g. $0 glibc-2.28 glibc-2.34" >&2
  echo "       tags are glibc-<version>; run \`ldd --version\` on each node" >&2
  exit 2
fi

OLD=$1
NEW=$2
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPTS="$HERE/scripts"

# Mirror audit-locale-diff.sh exactly, and inherit the user's setting. Never
# invent a private directory: the path is a documented knob, and the step
# scripts resolve this same variable independently.
OUT_DIR=${PG_GLIBC_AUDIT_OUT:-/tmp/pg-glibc-collation-audit}
export PG_GLIBC_AUDIT_OUT="$OUT_DIR"

# Python block-buffers stdout when it is not a tty. Without this, the `note:`
# lines on stderr overtake step 2's `!!` warning -- the single most important
# message the audit prints.
export PYTHONUNBUFFERED=1

# Tells the steps to drop their "run this next" hints, which name commands
# this script has already run. Findings, counts and warnings are unaffected.
export PG_GLIBC_AUDIT_WRAPPED=1

PAIR="${OLD//[^A-Za-z0-9_.@+-]/_}..${NEW//[^A-Za-z0-9_.@+-]/_}"
STEP2_LIST="$OUT_DIR/step2_changed_collate.$PAIR.txt"
STEP3_LIST="$OUT_DIR/step3_affected_locales.txt"
STEP4_LIST="$OUT_DIR/step4_exposed_locales.txt"

mkdir -p "$OUT_DIR"

# Every file this script later READS must have been written by this run. Step 3
# and step 4 use pair-agnostic names, so a leftover from a different pair would
# otherwise be summarised as if it were this pair's answer -- the exact bug
# filter_lc_collate_changes.py's docstring records having removed. Targeted
# removal only: $OUT_DIR is user-supplied and is not ours to wipe.
rm -f "$STEP2_LIST" "$STEP3_LIST" "$STEP4_LIST"

banner() {
  echo
  echo "================================================================"
  echo "== $1"
  echo "================================================================"
}

# Each step's output is teed so the summary can quote it back. `set -o pipefail`
# is what makes the tee'd status the step's own status rather than tee's.
run_step() {
  local num=$1; shift
  local log="$OUT_DIR/step$num.$PAIR.log"
  "$@" 2>&1 | tee "$log"
}

banner "STEP 1  What changed, and how far it reaches"
run_step 1 "$SCRIPTS/audit-locale-diff.sh" "$OLD" "$NEW"

banner "STEP 2  Which of those changes are inside LC_COLLATE"
run_step 2 python3 "$SCRIPTS/filter_lc_collate_changes.py" "$OLD" "$NEW"

# Absent is not empty. Step 2 writes this file whether or not it found
# anything, so a missing file means step 2 did not get that far -- an error to
# report, never an empty result to pass on as a clean audit.
#
# Unreachable today: under `set -e` a failing step 2 has already ended this
# script. Kept because it is the difference between a wrong answer and an
# error if step 2 ever stops writing, and the suite cannot reach it to prove
# that -- so treat it as untested defence, not as a checked guarantee.
if [ ! -f "$STEP2_LIST" ]; then
  echo "error: step 2 did not write $STEP2_LIST. Not continuing: an empty" >&2
  echo "       locale list would read as 'nothing changed'." >&2
  exit 1
fi

# Read without a subshell and without mapfile -- macOS ships bash 3.2, and
# requirements.md promises only "bash".
STEP3_ARGS=()
while IFS= read -r name; do
  [ -n "$name" ] || continue
  # This file lives under a world-writable /tmp by default and becomes argv
  # below. A pre-seeded line like `--repo /elsewhere` would silently point the
  # audit at different source, so validate before trusting.
  #
  # Also unreachable today, and for a better reason: step 2 rewrites this file
  # for THIS pair immediately above, so the only names that get here are the
  # basenames it just wrote. That property is what
  # test_step_2_rewrites_the_list_so_a_seed_cannot_survive pins; this check is
  # what stops the file being trusted if that ever changes.
  case $name in
    *[!A-Za-z0-9_.@+-]* | -*)
      echo "error: refusing to pass '$name' from $STEP2_LIST to step 3." >&2
      echo "       That is not a locale file name. Delete the file and re-run." >&2
      exit 1
      ;;
  esac
  STEP3_ARGS+=("$name")
done < "$STEP2_LIST"

if [ ${#STEP3_ARGS[@]} -gt 0 ]; then
  banner "STEP 3  Which locales inherit those changes"
  run_step 3 python3 "$SCRIPTS/resolve_copy_closure.py" "$NEW" "${STEP3_ARGS[@]}"
else
  banner "STEP 3  Skipped: no locale changed inside LC_COLLATE"
  echo "Nothing to close over the copy graph for this pair."
  echo "This is a real result, not a failure -- steps 4 and 5 still matter,"
  echo "because they cover what a data diff cannot settle."
  : > "$STEP3_LIST"
fi

banner "STEP 4  Which locales a data diff can never clear"
run_step 4 python3 "$SCRIPTS/flag_algorithmic_ranges.py" "$NEW"

banner "STEP 5  Did the code that computes weights change"
run_step 5 python3 "$SCRIPTS/diff_collation_code.py" "$OLD" "$NEW"

# ---------------------------------------------------------------- summary ----

count_lines() { [ -f "$1" ] && grep -c . "$1" || echo 0; }

HUNKS=$(sed -n 's/^\([0-9][0-9]*\) substantive hunk(s) found.*/\1/p' \
        "$OUT_DIR/step5.$PAIR.log" | tail -1)
HUNKS=${HUNKS:-0}

banner "AUDIT SUMMARY  $OLD -> $NEW"

echo
echo "-- Reindex: sort order changes, confirm then REINDEX"
if [ "$(count_lines "$STEP3_LIST")" -gt 0 ]; then
  sed 's/^/     /' "$STEP3_LIST"
  echo "   ($(count_lines "$STEP3_LIST") name(s); full list: $STEP3_LIST)"
else
  echo "     none -- no locale's LC_COLLATE changed between these two tags"
fi

echo
if [ "$HUNKS" -gt 0 ]; then
  echo "-- Needs an empirical test: step 5 found $HUNKS substantive hunk(s),"
  echo "   so a clean data diff CANNOT clear the locales step 4 flagged"
  echo "     $(count_lines "$STEP4_LIST") generated name(s)"
  echo "     full list: $STEP4_LIST"
else
  echo "-- Needs an empirical test: none on this evidence. Step 5 found no"
  echo "   substantive change, so a clean data diff is sufficient even for"
  echo "   the locales step 4 flagged."
fi

# Repeated verbatim. A warning that scrolled past 400 lines ago has not been
# delivered, and these are the cases where a clean result means nothing.
WARNINGS=$(awk '
  /^!!/            { inblock = 1; print; next }
  inblock && /^   / { print; next }
  inblock          { inblock = 0 }
' "$OUT_DIR"/step[12345]."$PAIR".log 2>/dev/null || true)

if [ -n "$WARNINGS" ]; then
  echo
  echo "-- Warnings the clean results above do NOT cover"
  # Whole block, not just the `!!` line. These warnings wrap onto indented
  # continuation lines, and the first line alone stops before the part that
  # says why the clean result does not cover the locale.
  printf '%s\n' "$WARNINGS" | sed 's/^/     /'
  echo "     (see docs/limitations.md)"
fi

echo
echo "-- Not decided for you"
if [ "$HUNKS" -gt 0 ]; then
  echo "     $HUNKS hunk(s) marked >> in step 5. Whether any of them moves a"
  echo "     weight is a judgement call that needs someone to read C."
  echo "     If nobody will, treat step 4's list as unresolved and confirm"
  echo "     empirically instead: docs/confirming-on-a-real-system.md"
else
  echo "     Nothing from step 5. Still confirm on real nodes before acting:"
  echo "     an upstream diff cannot see your distro's backports."
  echo "     docs/confirming-on-a-real-system.md"
fi
echo
