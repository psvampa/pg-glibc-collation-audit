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
#              [--old-locales-dir DIR --old-build-id NVR]
#              [--new-locales-dir DIR --new-build-id NVR]
#
# The order of the two tags is not a formality: every step assumes the second
# one is the newer. Given them the other way round the run used to go to the
# end at exit 0 with a plausible summary -- step 4 scanning the older tag, and
# step 2 calling a deleted locale a harmless addition. A reversed pair is
# refused now. One commit spelt two ways -- a tag and its own sha -- is run,
# and the run says that nothing was compared.
#
# The --*-locales-dir options are optional and read a node's own
# /usr/share/i18n/locales/. Each side you supply adds the
# distro-versus-upstream check for that side (step 6 for old, step 7 for new)
# and the ellipsis scan of that node's own data (step 9 for old, 10 for new);
# supplying BOTH additionally runs the node-to-node comparison (step 8), the
# only thing here that can see a locale the distro backports -- C.UTF-8 above
# all. See usage() below and docs/method.md.
#
# Example (the tags are examples -- run `ldd --version` on each node):
#   ./audit.sh glibc-2.28 glibc-2.34
#   ./audit.sh glibc-2.28 glibc-2.34 \
#     --old-locales-dir ./el8-locales --old-build-id glibc-2.28-251.el8_10.40 \
#     --new-locales-dir ./el9-locales --new-build-id glibc-2.34-275.el9_8
set -euo pipefail

usage() {
  echo "usage: $0 <old_tag> <new_tag>" >&2
  echo "         [--old-locales-dir DIR --old-build-id NVR]" >&2
  echo "         [--new-locales-dir DIR --new-build-id NVR]" >&2
  echo "       e.g. $0 glibc-2.28 glibc-2.34" >&2
  echo "       tags are glibc-<version>; run \`ldd --version\` on each node" >&2
  echo "       OLD first, NEW second: a reversed pair is refused, not" >&2
  echo "       answered. Every --* option takes a value." >&2
  echo >&2
  echo "       The --*-locales-dir options are OPTIONAL. Given a copy of a" >&2
  echo "       node's /usr/share/i18n/locales/, the run also checks whether" >&2
  echo "       the distro's patches touch LC_COLLATE -- the one thing an" >&2
  echo "       upstream tag diff structurally cannot see. Needs the node's" >&2
  echo "       build id too: a result is bound to the build it ran on." >&2
  echo >&2
  echo "       Either side on its own adds that check for that side (step 6" >&2
  echo "       for old, step 7 for new), and scans that node's own data for" >&2
  echo "       ellipsis ranges (step 9 for old, step 10 for new) -- which is" >&2
  echo "       the only way the question is asked of C itself, since step 4" >&2
  echo "       scans the new TAG and no tag holds that file." >&2
  echo >&2
  echo "       Supply BOTH and the run also compares the two nodes to each" >&2
  echo "       other (step 8). That is the only source-level evidence there is" >&2
  echo "       about C.UTF-8, whose file is in neither tag of the RHEL8->RHEL9" >&2
  echo "       pair." >&2
  exit 2
}

[ $# -ge 2 ] || usage
OLD=$1
NEW=$2
shift 2

# Every option here takes a value, and `shift 2` with only one argument left
# exits 1 under `set -e` printing NOTHING -- measured with
# `--new-locales-dir` as the last word on the line. An empty value is refused
# for the same reason it is not accepted from a file: `--old-locales-dir ""`
# leaves OLD_LOCALES empty, which is indistinguishable from not having asked
# for the node checks at all.
needs_value() {
  [ -n "${2:-}" ] || { echo "error: $1 needs a value" >&2; usage; }
}
OLD_LOCALES=""; OLD_BUILD=""; NEW_LOCALES=""; NEW_BUILD=""
while [ $# -gt 0 ]; do
  case $1 in
    --old-locales-dir) needs_value "$@"; OLD_LOCALES=$2; shift 2 ;;
    --old-build-id)    needs_value "$@"; OLD_BUILD=$2;   shift 2 ;;
    --new-locales-dir) needs_value "$@"; NEW_LOCALES=$2; shift 2 ;;
    --new-build-id)    needs_value "$@"; NEW_BUILD=$2;   shift 2 ;;
    *) echo "error: unknown argument '$1'" >&2; usage ;;
  esac
done

# A locales dir without its build id would produce a result that cannot be
# cited, so refuse the pair rather than silently dropping half of it.
if { [ -n "$OLD_LOCALES" ] && [ -z "$OLD_BUILD" ]; } ||
   { [ -n "$NEW_LOCALES" ] && [ -z "$NEW_BUILD" ]; }; then
  echo "error: --*-locales-dir requires the matching --*-build-id" >&2
  exit 2
fi
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

# Named after both builds, so a node-to-node result cannot be read as another
# pair's. Empty unless both sides were supplied, which is what gates step 8.
NODE_LIST=""; NODE_INHERITED=""
if [ -n "$OLD_BUILD" ] && [ -n "$NEW_BUILD" ]; then
  BUILDPAIR="${OLD_BUILD//[^A-Za-z0-9_.@+-]/_}..${NEW_BUILD//[^A-Za-z0-9_.@+-]/_}"
  NODE_LIST="$OUT_DIR/node_collate_diffs.$BUILDPAIR.txt"
  NODE_INHERITED="$OUT_DIR/node_collate_inherited.$BUILDPAIR.txt"
fi

mkdir -p "$OUT_DIR"

# Every file this script later READS must have been written by this run. Step 3
# and step 4 use pair-agnostic names, so a leftover from a different pair would
# otherwise be summarised as if it were this pair's answer -- the exact bug
# filter_lc_collate_changes.py's docstring records having removed. Targeted
# removal only: $OUT_DIR is user-supplied and is not ours to wipe.
#
# The step logs count as files this run reads: the warnings block at the bottom
# globs every step*.$PAIR.log to repeat the `!!` notices. Without this, a run
# given both nodes' directories leaves step 6/7/8/9/10 logs behind, and the NEXT
# run of the same pair -- given no directories at all -- reprints their node
# findings as its own, down to "C.UTF-8: built from ellipsis ranges on at least
# one of these nodes" when it read no node. The direction is conservative, which
# is why it went unnoticed, but the statement is false and this file's rule is
# that every file it reads was written by this run.
rm -f "$STEP2_LIST" "$STEP3_LIST" "$STEP4_LIST" ${NODE_LIST:+"$NODE_LIST"} \
      ${NODE_INHERITED:+"$NODE_INHERITED"}
rm -f "$OUT_DIR"/step[0-9]*."$PAIR".log

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

# Which pair this actually is, asked of git -- and asked HERE because step 1 is
# what clones the repository and verifies both refs, so nothing before it can
# ask git anything. Step 1 asks the same question first, and refuses there, so
# that no finding is printed for a pair that is about to be rejected; steps 2
# and 5 ask it again for themselves, and --quiet is what keeps this call from
# adding one more copy of their `!!` block. Two questions in one answer:
#
#   reversed -- step 1 has already exited 2 and this script stopped with it.
#               Reversed, all five steps run to the end and print a plausible
#               clean result; the direction was never compared at all.
#   same     -- one commit, however each side is spelt. This used to be
#               `[ "$OLD" = "$NEW" ]`, a comparison of TEXT, so
#               `./audit.sh glibc-2.39 ef321e23...` -- the sha this run's own
#               provenance line prints -- skipped the notice entirely and
#               summarised "no locale's LC_COLLATE changed".
#
# The status word is the only thing the subcommand puts on stdout, so a forward
# pair adds nothing to the output.
ORDER=$(python3 "$SCRIPTS/glibc_locale_data.py" order --quiet "$OLD" "$NEW")

# A minor-version upgrade inside one RHEL major is two builds of the SAME
# upstream release, so every step below has nothing to compare and reports a
# clean everything. That is not a clean result, and C.UTF-8 is the proof: its
# order changed between RHEL 8.1 and 8.2, both of them upstream glibc 2.28.
SAME_TAG=0
case $ORDER in
  # The `!!` block for this case comes from the order check that every step
  # taking the pair runs -- measured, steps 1, 2 and 5 each print it -- so it
  # lands in their logs, which is where the warnings block at the bottom finds
  # it and repeats it once. It used to be echoed here, and then only the
  # wrapper said it: a hand-run step printed its clean sentence over a
  # comparison that had not happened.
  same) SAME_TAG=1 ;;
  forward) ;;
  # The steps printed their own `!!` block; the summary repeats it, because a
  # warning 400 lines up has not been delivered.
  undetermined) ;;
  # No state falls through to the quiet branch: an unrecognised word means the
  # check did not answer, and "the pair is in order" is the assumption that
  # runs the whole audit against the wrong tag.
  *)
    echo "error: the order check answered '$ORDER', which is none of the" >&2
    echo "       states this script handles (same, forward, undetermined;" >&2
    echo "       a reversed pair never gets here, it exits 2 in step 1)." >&2
    echo "       Not continuing." >&2
    exit 1
    ;;
esac

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

# Optional, and not a sixth step of the method: steps 1-5 read only the clone,
# while this needs a node's files. It runs only when you supply them.
if [ -n "$OLD_LOCALES" ]; then
  banner "DISTRO CHECK  do $OLD_BUILD's patches touch LC_COLLATE?"
  run_step 6 python3 "$SCRIPTS/diff_distro_locales.py" "$OLD" \
    --locales-dir "$OLD_LOCALES" --build-id "$OLD_BUILD" --node-label old
fi
if [ -n "$NEW_LOCALES" ]; then
  banner "DISTRO CHECK  do $NEW_BUILD's patches touch LC_COLLATE?"
  run_step 7 python3 "$SCRIPTS/diff_distro_locales.py" "$NEW" \
    --locales-dir "$NEW_LOCALES" --build-id "$NEW_BUILD" --node-label new
fi

# The only comparison that can see a locale the distro BACKPORTS: it takes both
# sides from the nodes, so a file in neither tag is still in both inputs.
if [ -n "$OLD_LOCALES" ] && [ -n "$NEW_LOCALES" ]; then
  banner "NODE TO NODE  does $OLD_BUILD's collation data differ from $NEW_BUILD's?"
  run_step 8 python3 "$SCRIPTS/diff_node_locales.py" \
    --old-locales-dir "$OLD_LOCALES" --old-build-id "$OLD_BUILD" \
    --new-locales-dir "$NEW_LOCALES" --new-build-id "$NEW_BUILD" \
    --old-tag "$OLD" --new-tag "$NEW"
fi

# Step 4 again, over each NODE's own locale directory instead of the new tag.
# Step 4 above scans the tag, which cannot hold a file no tag has -- C among
# them -- and an ellipsis range is precisely what a data diff can never clear,
# so the node-to-node comparison cannot settle it either. docs/limitations.md
# used to say "run this by hand, once per node"; a check that depends on
# somebody remembering is not a check.
if [ -n "$OLD_LOCALES" ]; then
  banner "NODE ELLIPSIS  does $OLD_BUILD's own locale data use ellipsis ranges?"
  run_step 9 python3 "$SCRIPTS/flag_algorithmic_ranges.py" \
    --locales-dir "$OLD_LOCALES" --build-id "$OLD_BUILD" --supported-tag "$OLD"
fi
if [ -n "$NEW_LOCALES" ]; then
  banner "NODE ELLIPSIS  does $NEW_BUILD's own locale data use ellipsis ranges?"
  run_step 10 python3 "$SCRIPTS/flag_algorithmic_ranges.py" \
    --locales-dir "$NEW_LOCALES" --build-id "$NEW_BUILD" --supported-tag "$NEW"
fi

# ---------------------------------------------------------------- summary ----

# awk, not `grep -c ... || echo 0`: grep -c prints "0" AND exits 1 when
# nothing matches, so the fallback printed a second "0" and every clean run
# fed "0\n0" to `[ -gt ]`, which complained "integer expression expected" on
# stderr and fell into the else branch. The right branch, by luck.
count_lines() { [ -f "$1" ] || { echo 0; return; }; awk 'NF {n++} END {print n+0}' "$1"; }

# Same, minus the `#` provenance header the node-to-node list carries. Counting
# it would report one finding where there are none -- and "1 locale differs"
# is the wrong direction to be wrong in.
count_names() { [ -f "$1" ] || { echo 0; return; }; awk '!/^#/ && NF {n++} END {print n+0}' "$1"; }

# Step 5 has three outcomes, not two. `hunks`: it printed a count. `clean`: it
# printed its clean sentence. `unresolved`: neither -- which is what it prints
# when a tracked path is present at the old tag and gone at the new one, and
# is also what a reworded script or a truncated log would look like. This used
# to be `HUNKS=${HUNKS:-0}`: anything that was not a count became zero, and
# zero is the reassuring branch. A vanished ld-collate.c would have been
# summarised as "a clean data diff is sufficient".
HUNKS=$(sed -n 's/^\([0-9][0-9]*\) substantive hunk(s) found.*/\1/p' \
        "$OUT_DIR/step5.$PAIR.log" | tail -1)
if [ -n "$HUNKS" ]; then
  STEP5=hunks
elif grep -q '^No substantive collation code change\.' "$OUT_DIR/step5.$PAIR.log"; then
  STEP5=clean
  HUNKS=0
else
  STEP5=unresolved
  HUNKS=0
fi

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
case $STEP5 in
  hunks)
    echo "-- Needs an empirical test: step 5 found $HUNKS substantive hunk(s),"
    echo "   so a clean data diff CANNOT clear the locales step 4 flagged"
    echo "     $(count_lines "$STEP4_LIST") name(s) to confirm: generated names, and"
    echo "     source names for the locales SUPPORTED does not list"
    echo "     full list: $STEP4_LIST" ;;
  clean)
    echo "-- Needs an empirical test: none on this evidence. Step 5 found no"
    echo "   substantive change, so a clean data diff is sufficient even for"
    echo "   the locales step 4 flagged." ;;
  *)
    echo "-- Needs an empirical test: step 5 did NOT reach a clean result, so"
    echo "   the locales step 4 flagged stay UNRESOLVED. Read step 5's output:"
    echo "   a tracked file vanished between the tags or was renamed away"
    echo "   before both of them, a tracked path exists at no ref in the"
    echo "   clone, the include walk reached nothing, or the step did not"
    echo "   finish."
    echo "     $(count_lines "$STEP4_LIST") name(s) to confirm: generated names, and"
    echo "     source names for the locales SUPPORTED does not list"
    echo "     full list: $STEP4_LIST" ;;
esac

echo
if [ -n "$NODE_LIST" ] && [ -f "$NODE_LIST" ]; then
  echo "-- Node-to-node locale data ($OLD_BUILD -> $NEW_BUILD)"
  NODE_DIFFS=$(count_names "$NODE_LIST")
  if [ "$NODE_DIFFS" -gt 0 ]; then
    echo "     $NODE_DIFFS locale(s) differ inside LC_COLLATE between the two"
    echo "     nodes' OWN sources; full list: $NODE_LIST"
    # Their reach. Read from the list step 8 wrote, not from its prose; an
    # absent list is reported as absent, because the step-5 summary once
    # turned a missing line into a reassuring zero.
    if [ -f "$NODE_INHERITED" ]; then
      echo "     plus $(count_names "$NODE_INHERITED") locale(s) that inherit one of those files'"
      echo "     LC_COLLATE via copy on $NEW_BUILD; full list: $NODE_INHERITED"
    else
      echo "     blast radius via copy: NOT REPORTED -- step 8 wrote no"
      echo "     inheritance list; read its output above"
    fi
  else
    echo "     no locale differs inside LC_COLLATE between the two nodes'"
    echo "     own sources. Data only -- the weights an ellipsis range expands"
    echo "     to are computed by localedef, not stored in these files."
  fi
  if grep -q '^  C (C\.UTF-8): present on both nodes, LC_COLLATE DIFFERS' \
       "$OUT_DIR/step8.$PAIR.log" 2>/dev/null; then
    echo "     C (C.UTF-8): DIFFERS  <- in neither tag; no other step sees it"
  fi
else
  # Absent is not empty. A summary that simply says nothing about C.UTF-8
  # reads exactly like one that cleared it, and that is how this locale gets
  # missed -- it is false negative #1 in a different costume.
  echo "-- Node-to-node locale data: NOT RUN"
  echo "     Pass --old-locales-dir and --new-locales-dir with their build"
  echo "     ids. Without it nothing above says anything about C.UTF-8: its"
  echo "     source file is in neither tag, and PostgreSQL reports collversion"
  echo "     as NULL for every C.* collation, so no mismatch can ever fire."
  echo "     Then run sql/c_utf8_probe.sql on both nodes."
fi

# Same rule one level down: the node-to-node comparison answers whether the two
# nodes carry the same collation DATA, and this answers whether that data is
# the kind localedef expands at build time. Identical data is not identical
# order when an ellipsis range computes the weights.
echo
if [ -n "$OLD_LOCALES" ] || [ -n "$NEW_LOCALES" ]; then
  for side in old new; do
    if [ "$side" = old ]; then dir=$OLD_LOCALES; build=$OLD_BUILD; n=9
    else dir=$NEW_LOCALES; build=$NEW_BUILD; n=10; fi
    [ -n "$dir" ] || continue
    log="$OUT_DIR/step$n.$PAIR.log"
    echo "-- Node's own locale data, ellipsis scan ($build)"
    sed -n 's/^Locales whose LC_COLLATE uses ellipsis (algorithmic) ranges: /     ellipsis-based locale(s): /p' \
      "$log" 2>/dev/null
    # Read the status step N DECLARED for C, rather than inferring one from
    # two greps. The old form asked "is C in the ellipsis list?", then "does
    # the codepoint line name it?", and printed NOTHING when both answers were
    # no -- which is also what it printed when the step never looked. A C that
    # is present with explicit weights, one that only copies another, and one
    # absent from the directory all reached the reader as silence, and silence
    # here reads as cleared. Five branches, and no state falls through.
    CSTAT=$(sed -n 's/^  C (C\.UTF-8): //p' "$log" 2>/dev/null | tail -1)
    case "$CSTAT" in
      ellipsis-based*)
        echo "     C (C.UTF-8): ellipsis-based  <- localedef computes its weights,"
        echo "     so identical data does NOT mean identical order" ;;
      codepoint_collation*)
        echo "     C (C.UTF-8): codepoint_collation  <- byte order by construction" ;;
      ABSENT*)
        echo "     C (C.UTF-8): ABSENT from this locale directory  <- not examined,"
        echo "     NOT cleared. If the node has C.UTF-8, the directory passed in"
        echo "     is not the one that node built it from" ;;
      '')
        # Unreachable as the step stands -- it declares a status for every
        # backported locale it knows of, on every run -- so no test drives
        # this branch; tests/README.md lists it with the other three. It is
        # here because the alternative to an unreachable branch is a reworded
        # script silently taking the reassuring one.
        echo "     C (C.UTF-8): NOT DECLARED  <- step $n printed no status line for"
        echo "     it. Read step $n above; do not read this as cleared" ;;
      *)
        # Relay what the step declared, whole. This branch used to add
        # "neither ellipsis-based nor codepoint_collation", which is true of
        # the FILE and false of the ORDER when that file copies a template
        # step 4 flagged: the step knows the difference and says so, and the
        # summary must not overwrite it with a guess of its own.
        echo "     C (C.UTF-8): $CSTAT" ;;
    esac
  done
else
  echo "-- Node's own ellipsis scan: NOT RUN"
  echo "     Pass --old-locales-dir and --new-locales-dir with their build"
  echo "     ids. Step 4 above scanned the tag, and no tag of this pair holds"
  echo "     localedata/locales/C, so nothing above says whether either node's"
  echo "     own C.UTF-8 is ellipsis-based -- which is the one thing a data"
  echo "     diff, including the node-to-node one, can never clear."
fi

if [ "$ORDER" = "undetermined" ]; then
  echo
  echo "-- Direction of the pair: NOT ESTABLISHED"
  echo "     Neither of $OLD and $NEW is an ancestor of the other, and the"
  echo "     glibc release behind each one does not put them in order either"
  echo "     -- so nothing here checked that $NEW is the newer of the two"
  echo "     (step 1 prints which tag it found behind each). If they are"
  echo "     the wrong way round, every list above is the wrong tag's: step 4"
  echo "     scanned $NEW, and step 2 reports a locale deleted in the upgrade"
  echo "     as a harmless addition. Confirm which build is older."
fi

if [ "$SAME_TAG" = "1" ]; then
  echo
  echo "-- One commit, compared with itself"
  echo "     $OLD -> $NEW. Everything above that says 'nothing changed' means"
  echo "     'nothing was compared'. For an intra-major upgrade the evidence is"
  echo "     the node-to-node check and sql/c_utf8_probe.sql, nothing else."
fi

# Repeated verbatim. A warning that scrolled past 400 lines ago has not been
# delivered, and these are the cases where a clean result means nothing.
#
# Deduplicated by whole block. Three steps read node files and each closes with
# the same charmaps caveat; printing it three times trains the reader to skip
# the section, which costs more than the repetition buys. Identical text only --
# two warnings that differ by one word are two warnings.
WARNINGS=$(awk '
  function flush() {
    if (cur != "") { if (!(cur in seen)) { seen[cur] = 1; printf "%s", cur } }
    cur = ""
  }
  /^!!/             { flush(); cur = $0 "\n"; inblock = 1; next }
  inblock && /^   / { cur = cur $0 "\n"; next }
  inblock           { flush(); inblock = 0 }
  END               { flush() }
' "$OUT_DIR"/step[0-9]*."$PAIR".log 2>/dev/null || true)

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
case $STEP5 in
  hunks)
    echo "     $HUNKS hunk(s) marked >> in step 5. Whether any of them moves a"
    echo "     weight is a judgement call that needs someone to read C."
    echo "     If nobody will, treat step 4's list as unresolved and confirm"
    echo "     empirically instead: docs/confirming-on-a-real-system.md" ;;
  clean)
    echo "     Nothing from step 5. Still confirm on real nodes before acting:"
    echo "     an upstream diff cannot see your distro's backports."
    echo "     docs/confirming-on-a-real-system.md" ;;
  *)
    echo "     Step 5 reached no clean result (see above). Until it does, step"
    echo "     4's list is unresolved: confirm empirically instead:"
    echo "     docs/confirming-on-a-real-system.md" ;;
esac
echo
