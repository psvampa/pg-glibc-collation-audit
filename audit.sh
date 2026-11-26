#!/usr/bin/env bash
#
# Run the whole glibc collation audit for one version pair.
#
# The steps exist because the method asks distinct questions, not because a
# user should have to type one command per step. This runs them in order and
# ends with a consolidated summary, whose first list is step 2's.
#
# Usage:
#   ./audit.sh <old> <new>
#              [--old-node FILE --new-node FILE]
#
# Each side is a glibc tag, or a build named as `rpm -q glibc` names it
# without the architecture (glibc-2.34-275.el9_8): each step that reads that
# side then reads the build's own localedata/ and locale/, rebuilt from its
# source RPM, in place of the tag. A side can also be latest-elN, for a machine
# not installed yet: the newest build of RHEL N that Rocky Linux's mirror
# lists, resolved once, and named, before anything else reads the sides.
# Given each machine's file (--old-node, --new-node),
# the run reads that machine's own locale sources and charmaps, as the
# build the file names, in place of its tag; a file without the sources, or
# naming a build that is not of RHEL 8, 9 or 10, makes both sides read their
# tags when that side was given as its tag, and the run says so; given as
# the build itself, the run is refused.
#
# The order of the two tags is not a formality: every step assumes the second
# one is the newer. Given them the other way round the run used to go to the
# end at exit 0 with a plausible summary -- step 2 calling a deleted locale a
# harmless addition. A reversed pair is
# refused now. One commit spelt two ways -- a tag and its own sha -- is run,
# and the run says that nothing was compared.
#
# Direction is the ONLY property of the pair that is constrained. How far apart
# the two releases are is not checked anywhere: an upgrade that skips a release
# is one pair of tags, not two runs added up. Measured on one triple --
# glibc-2.28 against glibc-2.39 reports the union of what 2.28..2.34 and
# 2.34..2.39 report, C.UTF-8 included, except that a locale renamed on the way
# (aa_ER@saaho) is listed only as removed. One triple is not every triple.
#
# The --*-node options are optional, and go together. Each is the one file
# `scripts/locale_order.py --extract` wrote on that machine, holding its
# measurement (how its own glibc sorts every locale), its locale sources, its
# charmaps and its glibc build. A file without the sources or the charmaps
# says why, and the run repeats it where their checks would have answered.
# Step 11 compares the two measurements, so it answers from each machine's
# glibc rather than from locale sources, compares each locale's compiled sort
# table, names the characters and other strings that moved (outside UTF-8,
# by the old machine's charmaps), and checks what it measured against each
# machine's charmaps. When step 2 compares the two machines' own locale
# sources, each locale of the old machine step 11 names is set beside step
# 2's rules for it; otherwise the run says what step 2 compared.
#
# Example (the names are examples -- `ldd --version` on each node gives the
# tag, `rpm -q glibc` the build):
#   ./audit.sh glibc-2.28 glibc-2.34
#   ./audit.sh glibc-2.28 glibc-2.34 --old-node old.tar --new-node new.tar
#   ./audit.sh glibc-2.28-251.el8_10.40 glibc-2.34-275.el9_8
#   ./audit.sh glibc-2.34-275.el9_8 latest-el10
set -euo pipefail

usage() {
  echo "usage: $0 <old> <new>" >&2
  echo "         [--old-node FILE --new-node FILE]" >&2
  echo "       e.g. $0 glibc-2.28 glibc-2.34" >&2
  echo "            $0 glibc-2.28-251.el8_10.40 glibc-2.34-275.el9_8" >&2
  echo "            $0 glibc-2.34-275.el9_8 latest-el10" >&2
  echo "       each side is a glibc tag, glibc-<version> (run \`ldd --version\` on" >&2
  echo "       the node), or a build as \`rpm -q glibc\` names it without the" >&2
  echo "       architecture, rebuilt from its source RPM, or latest-elN for a" >&2
  echo "       machine not installed yet: the newest build of RHEL N on Rocky" >&2
  echo "       Linux's mirror, named before anything is read." >&2
  echo "       OLD first, NEW second: a reversed pair is refused, not" >&2
  echo "       answered. Distance is NOT checked: RHEL8 to RHEL10 is one" >&2
  echo "       pair, glibc-2.28 glibc-2.39, not two runs added up. What" >&2
  echo "       that reports was measured with glibc-2.34 in the middle" >&2
  echo "       (docs/scope.md). Every --* option takes a value." >&2
  echo >&2
  echo "       The --*-node options are OPTIONAL and go together. Each is the" >&2
  echo "       one file scripts/locale_order.py --extract wrote on that" >&2
  echo "       machine, with its measurement, its build, its locale sources and" >&2
  echo "       its charmaps (a file without them says why). The measurement is" >&2
  echo "       how the machine's own glibc sorts every locale, which step 11" >&2
  echo "       compares: it sees what a comparison of files cannot, a locale" >&2
  echo "       whose files did not change can still sort differently. Step 2" >&2
  echo "       reads each machine's own locale sources and charmaps, as the" >&2
  echo "       build its file names, in place of the tag (the side given is" >&2
  echo "       that build or its tag), and downloads nothing. A file without" >&2
  echo "       the sources, or naming a build that is not of RHEL 8, 9 or 10," >&2
  echo "       makes both sides read their tags when that side was given as" >&2
  echo "       its tag, and the run says so; given as the build itself, the" >&2
  echo "       run is refused. When step 2 compares the two machines' own" >&2
  echo "       locale sources, each locale of the old machine step 11 names is" >&2
  echo "       set beside step 2's rules for it; otherwise the run says what" >&2
  echo "       step 2 compared." >&2
  exit 2
}

[ $# -ge 2 ] || usage
OLD=$1
NEW=$2
shift 2

# Every option here takes a value, and `shift 2` with only one argument left
# exits 1 under `set -e` printing NOTHING -- measured with an option as the
# last word on the line. An empty value is refused for the same reason it is
# not accepted from a file: `--old-node ""` leaves OLD_NODE empty, which is
# indistinguishable from not having passed the machine's file at all.
needs_value() {
  [ -n "${2:-}" ] || { echo "error: $1 needs a value" >&2; usage; }
}
# OLD_ORDER and NEW_ORDER are not options: they are the measurements laid
# out of the machines' files, below.
OLD_ORDER=""; NEW_ORDER=""; OLD_NODE=""; NEW_NODE=""
while [ $# -gt 0 ]; do
  case $1 in
    --old-node)        needs_value "$@"; OLD_NODE=$2;    shift 2 ;;
    --new-node)        needs_value "$@"; NEW_NODE=$2;    shift 2 ;;
    *) echo "error: unknown argument '$1'" >&2; usage ;;
  esac
done
# The Python steps need 3.7 (str.isascii, subprocess's capture_output); an
# older python3 would stop the first of them with a traceback. Each refusal
# below has its own message: a version that cannot be read is neither old
# nor new.
PY_VERSION=$(python3 -c \
  'import sys; print("%d.%d" % sys.version_info[:2])') || {
  echo "error: python3 did not run here; audit.sh needs python3 3.7" \
    "or newer" >&2
  exit 2; }
case $PY_VERSION in
  3.[7-9]|3.[1-9][0-9]|[4-9].*) ;;
  [0-2].*|3.[0-6])
    echo "error: audit.sh needs python3 3.7 or newer; python3 here is" \
      "$PY_VERSION" >&2
    exit 2 ;;
  *) echo "error: could not read python3's version ('$PY_VERSION')" >&2
     exit 2 ;;
esac
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPTS="$HERE/scripts"
# Inherit the user's setting. Never invent a private directory. The step scripts resolve this same variable
# independently.
OUT_DIR=${PG_GLIBC_AUDIT_OUT:-/tmp/pg-glibc-collation-audit}
export PG_GLIBC_AUDIT_OUT="$OUT_DIR"

# What each side is: a ref of the glibc clone (a tag, or a commit given by
# its id or a branch), or a build named as `rpm -q glibc` names it
# (glibc-2.34-275.el9_8), by the rule every step applies. The summary picks
# its blocks by it: a block about what upstream source cannot show is false
# of a build, whose own sources the steps read. latest-elN is a build too,
# and nothing here asks the network what it is: that is done once, below,
# after the machines' files are checked.
# What a run leaves when it ends, however it ends (but by SIGKILL, or by
# SIGQUIT, Ctrl-\, which can end bash without running this function): its
# temporary files gone, and, where step 11 had compared the two
# measurements and a later step stopped the run before step 11's report was
# printed, where that report is (16.144): it is worked out first (below),
# and printed only after 'What was compared' and step 2. One function, set
# once, so that no trap replaces another; it changes no exit code.
#
# A run that stops before it writes in $OUT_DIR anything that outlives it
# (the machines' files are laid out there under a temporary name, which
# this function removes) -- refused for a machine's file cut short, the two
# files the other way round, a side that is not the build its file names,
# or cut short itself -- leaves whatever is there as it was: an earlier
# run's files, of whichever pair, or the user's own. It removes none of
# them, and, when this function runs, says that none is its own (16.211).
# UNTOUCHED_OUT is cleared at the first file it removes or writes there,
# under the pair's name, and no run ends well before that. The exit code is
# not asked: bash 3.2 hands this function 0 after a SIGTERM or a SIGHUP.
#
# Until then a SIGTERM or a SIGHUP sent to this script alone (kill <pid>,
# pkill -f audit.sh) reaches no step it runs, and bash used to end at once
# and run this function while the step went on: --unpack laid its side out
# again in NODE_TMP after it had been removed, and the run said it had left
# nothing there (16.339); resolve wrote RESOLVED again. So bash takes both
# signals until UNTOUCHED_OUT is cleared, runs the trap once the step under
# way has ended (measured: about 0.3 s for --unpack), and ends by the same
# signal. latest-elN, which waits on the mirror, is stopped instead, below.
# A signal to the whole group (Ctrl-C, a closed terminal) ends the step too,
# as before; one ignored when the run started (nohup) stays ignored.
UNTOUCHED_OUT=1
RESOLVED=""; NODE_TMP=""; ORDER_UNPRINTED=""; STEP0_WARNINGS=""
STEP0_RECORD=""; LATEST_RECORD=""
on_exit() {
  [ -z "$RESOLVED" ] || rm -f "$RESOLVED"
  [ -z "$STEP0_WARNINGS" ] || rm -f "$STEP0_WARNINGS"
  [ -z "$STEP0_RECORD" ] || rm -f "$STEP0_RECORD"
  [ -z "$LATEST_RECORD" ] || rm -f "$LATEST_RECORD"
  [ -z "$NODE_TMP" ] || rm -rf "$NODE_TMP"
  if [ -n "$UNTOUCHED_OUT" ]; then
    echo "       This run left nothing in $OUT_DIR;" >&2
    echo "       none of the files there is from this run." >&2
  fi
  if [ -n "$ORDER_UNPRINTED" ]; then
    echo "note: step 11 compared the two measurements before the run stopped;" >&2
    echo "      its report, not printed above, is in $ORDER_UNPRINTED" >&2
  fi
}
trap on_exit EXIT
trap 'trap - TERM; kill -TERM $$' TERM
trap 'trap - HUP; kill -HUP $$' HUP
# Set by run_step for each step of WARNING_STEPS alone, and for the calls
# of step 0 and step 11; never the reader's, which would have every other
# call write its `!!` there too.
unset PG_GLIBC_AUDIT_WARNINGS
# The sides as the user typed them, for the run's data file when one is
# latest-elN (report_data's --old-asked and --new-asked).
ASKED_OLD=$OLD; ASKED_NEW=$NEW
RESOLVED=$(mktemp "${TMPDIR:-/tmp}/pg-glibc-resolve.XXXXXX")
python3 "$SCRIPTS/glibc_locale_data.py" resolve --to "$RESOLVED" "$OLD" "$NEW"
# A build typed as `rpm -q glibc` prints it, with the architecture, is read
# without it (glibc_locale_data.without_arch).
OLD=$(sed -n 1p "$RESOLVED" | cut -f1)
NEW=$(sed -n 2p "$RESOLVED" | cut -f1)
OLD_KIND=$(sed -n 1p "$RESOLVED" | cut -f2)
NEW_KIND=$(sed -n 2p "$RESOLVED" | cut -f2)
OLD_LATEST=$(sed -n 1p "$RESOLVED" | cut -f3)
NEW_LATEST=$(sed -n 2p "$RESOLVED" | cut -f3)
for kind in "$OLD_KIND" "$NEW_KIND"; do
  case $kind in
    tag|build) ;;
    *)
      echo "error: the sides came back as '$OLD_KIND' and '$NEW_KIND', not tag" >&2
      echo "       or build. Not continuing." >&2
      exit 1
      ;;
  esac
done
# The route the pair takes (glibc_locale_data.pair_noun): tags (two refs of
# the clone, a commit included), builds, or sides (a ref and a build). A
# sentence names the sides by WORD instead, which the order check gives
# below. Set again below once each machine's file has said what its side is.
set_noun() {
  if [ "$OLD_KIND" = tag ] && [ "$NEW_KIND" = tag ]; then
    NOUN=tags
  elif [ "$OLD_KIND" = build ] && [ "$NEW_KIND" = build ]; then
    NOUN=builds
  else
    NOUN=sides
  fi
}
set_noun

# One machine's file compares nothing.
if { [ -n "$OLD_NODE" ] && [ -z "$NEW_NODE" ]; } ||
   { [ -z "$OLD_NODE" ] && [ -n "$NEW_NODE" ]; }; then
  echo "error: --old-node and --new-node go together: one machine's file" >&2
  echo "       alone compares nothing" >&2
  exit 2
fi

# Each machine's file is checked and laid out here, before anything reads the
# sides, because it names the build that machine runs, and the run reads
# that build from the machine's own locale sources, which the file holds
# (glibc_locale_data.machine_sides; nothing is downloaded): the side typed
# must be that build or its tag. A file that holds no locale sources, or
# names a build that is not of RHEL 8, 9 or 10, makes both sides read their
# tags when that side was given as its tag (given as the build itself, the
# run is refused), and NODE_NOTE says so; the warnings block repeats it from the step0 log
# written below. Laid out under a name of its own, and moved under the
# pair's name once the pair is named.
OLD_TAG=$OLD; NEW_TAG=$NEW; NODE_NOTE=""
if [ -n "$OLD_NODE" ]; then
  mkdir -p "$OUT_DIR"
  # Removed by on_exit, above.
  NODE_TMP=$(mktemp -d "$OUT_DIR/node_files.XXXXXX")
  for side in old new; do
    if [ "$side" = old ]; then file=$OLD_NODE; else file=$NEW_NODE; fi
    if ! python3 "$SCRIPTS/locale_order.py" --unpack "$file" "$NODE_TMP/$side"; then
      echo "error: the $side node's file could not be used (above). Not continuing." >&2
      exit 2
    fi
  done
  # Its `!!` as data too (glibc_locale_data.WARNINGS_ENV), moved under the
  # pair's name beside step0's log, below; removed by on_exit.
  STEP0_WARNINGS=$(mktemp "${TMPDIR:-/tmp}/pg-glibc-step0.XXXXXX")
  # Its note as data (glibc_locale_data.STEP0_RECORD_ENV), moved beside
  # it the same way.
  STEP0_RECORD=$(mktemp "${TMPDIR:-/tmp}/pg-glibc-step0-note.XXXXXX")
  NODE_NOTE=$(PG_GLIBC_AUDIT_WARNINGS="$STEP0_WARNINGS" \
    PG_GLIBC_AUDIT_STEP0_RECORD="$STEP0_RECORD" \
    python3 "$SCRIPTS/glibc_locale_data.py" machines --to "$RESOLVED" \
    "$OLD" "$NEW" "$(cat "$NODE_TMP/old/build-id")" "$(cat "$NODE_TMP/new/build-id")" \
    "$NODE_TMP/old" "$NODE_TMP/new")
  [ -z "$NODE_NOTE" ] || printf '%s\n' "$NODE_NOTE"
  OLD=$(sed -n 1p "$RESOLVED" | cut -f1); NEW=$(sed -n 2p "$RESOLVED" | cut -f1)
  OLD_KIND=$(sed -n 1p "$RESOLVED" | cut -f2); NEW_KIND=$(sed -n 2p "$RESOLVED" | cut -f2)
  OLD_TAG=$(sed -n 1p "$RESOLVED" | cut -f3); NEW_TAG=$(sed -n 2p "$RESOLVED" | cut -f3)
  set_noun
fi

# latest-elN, the newest build of RHEL N that Rocky Linux's mirror lists,
# replaced by that build's name, once for the whole run and before PAIR, so
# that every file, step and sentence below reads the name and the run is the
# one that name would give. The line saying which build it took is printed
# here and nowhere else. A side that cannot be resolved stops the run.
#
# The mirror can take as long as it likes to answer, and a kill must not wait
# for it: latest runs in the background, and a signal stops it, waits for it
# and ends the run by that signal. A step in the background ignores SIGINT
# and SIGQUIT (Ctrl-C, Ctrl-\), so those are taken here too. The traps are
# set before the step starts, and stop_latest takes the step from `jobs -p`
# when LATEST is not set yet: it lists the step from the moment bash starts
# it, and nothing before (this is the only step run in the background). Not
# $!: under set -u it is an error until a step has run in the background,
# and so is ${!:-} (measured on bash 3.2), and the run would end with 0.
if [ -n "$OLD_LATEST$NEW_LATEST" ]; then
  LATEST=""
  stop_latest() {
    trap - "$1"
    local pid=${LATEST:-$(jobs -p)}
    if [ -n "$pid" ]; then
      kill -TERM "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    kill -"$1" $$
  }
  trap 'stop_latest TERM' TERM
  trap 'stop_latest HUP' HUP
  trap 'stop_latest INT' INT
  trap 'stop_latest QUIT' QUIT
  # What latest prints, as data (glibc_locale_data.LATEST_RECORD_ENV), for
  # the run's data file.
  LATEST_RECORD=$(mktemp "${TMPDIR:-/tmp}/pg-glibc-latest.XXXXXX")
  PG_GLIBC_AUDIT_LATEST_RECORD="$LATEST_RECORD" \
    python3 "$SCRIPTS/glibc_locale_data.py" latest \
    --to "$RESOLVED" "$OLD" "$NEW" &
  LATEST=$!
  wait "$LATEST"
  LATEST=""
  trap 'trap - TERM; kill -TERM $$' TERM
  trap 'trap - HUP; kill -HUP $$' HUP
  trap - INT QUIT
  OLD=$(sed -n 1p "$RESOLVED"); NEW=$(sed -n 2p "$RESOLVED")
  if [ -z "$OLD" ] || [ -z "$NEW" ]; then
    echo "error: latest-elN came back as no build name. Not continuing." >&2
    exit 1
  fi
fi
# With a tag on one side only, that tag, which side it is, and the build on
# the other: set only now, so that the build is named as latest-elN resolved.
TAG_SIDE=""; TAG_NAME=""; BUILD_NAME=""
if [ "$NOUN" = sides ]; then
  if [ "$OLD_KIND" = tag ]; then
    TAG_SIDE=old; TAG_NAME=$OLD; BUILD_NAME=$NEW
  else
    TAG_SIDE=new; TAG_NAME=$NEW; BUILD_NAME=$OLD
  fi
fi

# Python block-buffers stdout when it is not a tty. Without this, the `note:`
# lines on stderr overtake the steps' `!!` warnings.
export PYTHONUNBUFFERED=1

# Every name below that a Python step also writes is spelled by the function
# that step uses, never by a second spelling here. bash's own
# ${x//[^A-Za-z0-9_.@+-]/_} replaces each byte of a character outside ASCII
# under LC_ALL=C, where pair_slug replaces the character once, and the summary
# then looked for a file the step had written under another name.
slug() { python3 "$SCRIPTS/glibc_locale_data.py" pair-slug "$1" "$2"; }
PAIR=$(slug "$OLD" "$NEW")

# Where the sides ('What was compared', below) record the commit made for
# each build, and the steps after them take it from
# (glibc_locale_data.SIDES_ENV), instead of each making it
# again: removed below with every other file this run reads, and exported to
# this run's steps only.
SIDES_FILE="$OUT_DIR/build_sides.$PAIR.txt"
export PG_GLIBC_AUDIT_SIDES="$SIDES_FILE"
# Where each machine's file is laid out, for the build it runs
# (glibc_locale_data.MACHINES_ENV): a step that reads that build reads the
# machine's own locale sources from there. Written below with the layout,
# only for a side read as its build; removed with every other file this run
# reads.
MACHINES_FILE="$OUT_DIR/machine_files.$PAIR.txt"
# What the `!!` for a side read as a tag asks for
# (glibc_locale_data.TAG_ASK_ENV): without the machines' files, a build;
# with a machine's file, nothing, as a tag is then read only when a file
# holds no locale sources or names a build that is not of RHEL 8, 9 or 10,
# which NODE_NOTE says.
if [ -n "$OLD_NODE" ]; then
  export PG_GLIBC_AUDIT_TAG_ASK=none
else
  export PG_GLIBC_AUDIT_TAG_ASK=build
fi
STEP2_LIST="$OUT_DIR/step2_changed_collate.$PAIR.txt"
STEP2_REMOVED="$OUT_DIR/step2_removed_locales.$PAIR.txt"
# Written by step 2 until step 12.4: what the character set check listed,
# which step 2's list holds. Nothing reads it; removed below, so that a
# list an earlier version left there is never taken for this run's.
OLD_STEP2_KEYED="$OUT_DIR/step2_keyed_locales.$PAIR.txt"
# The names of STEP2_LIST step 2 could not compare, each with its reason, or
# that of the locale it is an alias of, in a `!!` the warnings block repeats;
# the summary sets them apart (13.31, 17.2).
STEP2_NOT_COMPARED="$OUT_DIR/step2_not_compared.$PAIR.txt"
# Step 2's rules for every locale name measured on either machine, which
# the summary sets beside step 11's verdict (VERDICTS, below).
RULES_FILE="$OUT_DIR/step2_rules_by_name.$PAIR.json"
# Written by step 3 until step 9.2 merged it into step 2. Only removed
# below, so that a list an earlier version left there is never read.
OLD_STEP3_LIST="$OUT_DIR/step3_affected_locales.txt"
# Written by step 4 until step 9.3 deleted it, under names that carry no
# pair. Only removed below, so that a list an earlier version left there
# is never taken for this run's.
OLD_STEP4_LIST="$OUT_DIR/step4_exposed_locales.txt"
OLD_STEP4_BYTE_ORDER="$OUT_DIR/step4_byte_order_locales.txt"
# Written by step 1 until step 9.5 made it 'What was compared', under a name
# that carries no pair. Nothing reads it; removed below, so that a list an
# earlier version left there is never taken for this run's.
OLD_STEP1_LIST="$OUT_DIR/changed_locales.txt"

# The node files, laid out above (locale_order.py --unpack checks each one
# whole), are moved here under a name for the pair, like every other file
# this run reads: the measurement always, which step 11 compares; the
# sources and the charmaps when the file holds them, which the
# sides and step 2 read through MACHINES_FILE (a file without the sources
# was decided above, and NODE_NOTE says why).
#
# The charmaps also go to step 11, which checks the strings it measured
# against them and names, by the old machine's, the strings of a locale not
# in UTF-8; a file without them says why, and step 11 prints that reason
# under its NOT checked.
ORDER_CM=()
# From here the run removes and writes in $OUT_DIR under the pair's name
# (on_exit, above), and a signal ends it at once again.
UNTOUCHED_OUT=""
trap - TERM HUP
# Never one an earlier run left: removed before the layout writes it, and
# the variable is exported only when this run wrote the file (a value from
# the reader's environment would send the steps to another run's folders).
rm -f "$MACHINES_FILE"
unset PG_GLIBC_AUDIT_MACHINES
# Set below only once step 11 has compared the two measurements, for the
# steps' `!!` that name step 11 (glibc_locale_data.STEP11_ENV); never the
# reader's.
unset PG_GLIBC_AUDIT_STEP11
if [ -n "$OLD_NODE" ]; then
  mkdir -p "$OUT_DIR"
  for side in old new; do
    if [ "$side" = old ]; then file=$OLD_NODE; else file=$NEW_NODE; fi
    dir="$OUT_DIR/node_file.$PAIR.$side"
    rm -rf "$dir"
    mv "$NODE_TMP/$side" "$dir"
    if [ ! -d "$dir/locales" ] && [ ! -s "$dir/sources-not-included" ]; then
      # Unreachable as --unpack stands: it lays out one of the two or fails.
      echo "error: the $side node's file laid out neither its locale sources" >&2
      echo "       nor why they are missing. Not continuing." >&2
      exit 2
    fi
    if [ -d "$dir/charmaps" ]; then
      ORDER_CM+=("--$side-charmaps" "$dir/charmaps")
    elif [ -s "$dir/charmaps-not-included" ]; then
      cmreason=$(cat "$dir/charmaps-not-included")
      if [ -z "$cmreason" ]; then
        # Unreachable as --unpack stands: it refuses a reason with nothing
        # printable. An empty one here would drop the side's summary line.
        cmreason="the file gives no reason that can be printed"
      fi
      ORDER_CM+=("--$side-no-charmaps" "$file holds no charmaps, because $cmreason")
    else
      # Unreachable as --unpack stands: it lays out one of the two or fails.
      echo "error: the $side node's file laid out neither its charmaps nor" >&2
      echo "       why they are missing. Not continuing." >&2
      exit 2
    fi
    if [ "$side" = old ]; then kind=$OLD_KIND; b=$OLD; else kind=$NEW_KIND; b=$NEW; fi
    if [ "$kind" = build ]; then
      printf '%s\t%s\n' "$b" "$dir" >> "$MACHINES_FILE"
    fi
    if [ "$side" = old ]; then
      OLD_ORDER="$dir/locale_order.out"
    else
      NEW_ORDER="$dir/locale_order.out"
    fi
  done
  rmdir "$NODE_TMP"
  if [ -f "$MACHINES_FILE" ]; then
    export PG_GLIBC_AUDIT_MACHINES="$MACHINES_FILE"
  fi
fi

mkdir -p "$OUT_DIR"

# Every file this script later READS must have been written by this run. A
# name that carries no pair (step 4's, until step 9.3) let a leftover from a
# different pair be summarised as if it were this pair's answer -- the exact bug
# filter_lc_collate_changes.py's docstring records having removed. Targeted
# removal only: $OUT_DIR is user-supplied and is not ours to wipe.
#
# The step logs count as files this run reads: the warnings block at the bottom
# globs every step*.$PAIR.log to repeat the `!!` notices. Without this, a log
# an earlier run left behind -- of the same pair, or of a step that no longer
# runs (steps 3 to 10, deleted in branch 9) -- would be reprinted as this
# run's finding. The direction can be conservative, which is how it once went
# unnoticed, but the statement is false and this file's rule is that every
# file it reads was written by this run.
rm -f "$STEP2_LIST" "$STEP2_NOT_COMPARED" "$STEP2_REMOVED" "$OLD_STEP2_KEYED" \
      "$OLD_STEP3_LIST" \
      "$OLD_STEP4_LIST" "$OLD_STEP4_BYTE_ORDER" "$OLD_STEP1_LIST" \
      "$SIDES_FILE" "$RULES_FILE" \
      "$OUT_DIR/step2_rules_characters.$PAIR.tsv" \
      "$OUT_DIR/step2_rules.$PAIR.json" \
      "$OUT_DIR/step2_character_sets.$PAIR.json" \
      "$OUT_DIR/step2_transliteration.$PAIR.json" \
      "$OUT_DIR/step0.$PAIR.json" "$OUT_DIR/step1_compared.$PAIR.json" \
      "$OUT_DIR/latest.$PAIR.json"
rm -f "$OUT_DIR"/step[0-9]*."$PAIR".log \
      "$OUT_DIR"/step[0-9]*."$PAIR".warnings.jsonl
# The run's data file (scripts/report_data.py), written at the end from the
# files this run's steps write: one left by an earlier run is never this
# run's.
REPORT_DATA="$OUT_DIR/report.$PAIR.json"
rm -f "$REPORT_DATA" "$REPORT_DATA.partial"
# And its summary page (scripts/report_html.py), made from it alone.
REPORT_HTML="$OUT_DIR/report.$PAIR.html"
rm -f "$REPORT_HTML" "$REPORT_HTML.partial"
# What the machines' files decided, for the warnings block below, which
# repeats the `!!` blocks of every step*.$PAIR.log.
[ -z "$NODE_NOTE" ] || printf '%s\n' "$NODE_NOTE" > "$OUT_DIR/step0.$PAIR.log"
# And the same `!!` as data, for the run's data file (report_data.py): the
# file is there whenever step 0 ran, empty when it printed none.
[ -z "$STEP0_WARNINGS" ] || cp "$STEP0_WARNINGS" "$OUT_DIR/step0.$PAIR.warnings.jsonl"
# And its note as data (glibc_locale_data.machine_note), whenever step 0
# ran, whether it printed one or not.
[ -z "$STEP0_RECORD" ] || cp "$STEP0_RECORD" "$OUT_DIR/step0.$PAIR.json"
# And what latest printed as data, whenever a side was latest-elN.
[ -z "$LATEST_RECORD" ] || cp "$LATEST_RECORD" "$OUT_DIR/latest.$PAIR.json"

# Step 11 is worked out before the sides are read, so that a measurement the
# comparison refuses -- measured by another copy of locale_order.py over other
# strings, say; --unpack has already refused one cut short or damaged --
# stops the run here instead of after them and step 2. Its report is printed
# where step 11 stands, below, and its summary at the end.
#
# Its log is not named step11.*.log on purpose: the warnings block at the end
# globs those to repeat their `!!` notices, and would repeat step 11's with
# the lists under them cut off. Its summary block carries each of them, names
# included.
ORDER_LOG="$OUT_DIR/measured_order.$PAIR.log"
# Its `!!` as data too (glibc_locale_data.WARNINGS_ENV), each block of its
# report as printed, lists included. Created empty before the comparison,
# so that one that printed none is told apart from one that never ran; an
# earlier run's is removed with the steps' warnings files, above.
STEP11_WARNINGS="$OUT_DIR/step11.$PAIR.warnings.jsonl"
# Its data (locale_order.STEP11_DATA_ENV),
# written once its report is printed; removed below, so that one an
# earlier run left is never this run's.
STEP11_DATA="$OUT_DIR/step11.$PAIR.data.json"
ORDER_SUMMARY="$OUT_DIR/measured_order_summary.$PAIR.txt"
# Every character, other string and pair step 11 found, and every sequence
# a changed sort table names; its report and summary name it.
ORDER_CHARACTERS="$OUT_DIR/measured_order_characters.$PAIR.tsv"
# One SQL file per database encoding, $ORDER_SQL.<ENCODING>.sql, with the
# tests step 11 wrote; its report names each one and how to read the
# two servers' outputs.
ORDER_SQL="$OUT_DIR/measured_order_confirm.$PAIR"
# Step 11's verdict for each locale, which the lines of CROSS set beside
# step 2's rules once step 2 has run; CROSS_LOG holds why, if they could
# not be.
VERDICTS="$OUT_DIR/measured_order_verdicts.$PAIR.json"
CROSS="$OUT_DIR/measured_order_rules.$PAIR.txt"
CROSS_LOG="$OUT_DIR/measured_order_rules.$PAIR.log"
rm -f "$ORDER_LOG" "$ORDER_SUMMARY" "$ORDER_CHARACTERS" \
      "$VERDICTS" "$CROSS" "$CROSS_LOG" "$STEP11_DATA" "$STEP11_DATA.partial"
# This pair's SQL files, by the rule step 11 writes them with: not
# "$ORDER_SQL".*.sql, which also took those of another pair of the same old
# side whose new side is this one's with ".N" after it, as builds are named
# (16.109).
python3 "$SCRIPTS/locale_order.py" --remove-sql "$ORDER_SQL"
if [ -n "$OLD_ORDER" ]; then
  : > "$STEP11_WARNINGS"
  if ! PG_GLIBC_AUDIT_WARNINGS="$STEP11_WARNINGS" \
       PG_GLIBC_AUDIT_STEP11_DATA="$STEP11_DATA" \
       python3 "$SCRIPTS/locale_order.py" --compare "$OLD_ORDER" "$NEW_ORDER" \
       --tags "$OLD_TAG" "$NEW_TAG" --summary-to "$ORDER_SUMMARY" \
       --characters-to "$ORDER_CHARACTERS" --sql-to "$ORDER_SQL" \
       --verdicts-to "$VERDICTS" \
       ${ORDER_CM[@]+"${ORDER_CM[@]}"} \
       > "$ORDER_LOG" 2>&1; then
    cat "$ORDER_LOG" >&2
    # The message above names the measurement as laid out; the reader
    # passed the two node files.
    echo "error: step 11 could not compare the measurements in $OLD_NODE" >&2
    echo "       and $NEW_NODE (above). Not continuing." >&2
    exit 2
  fi
  # Said by on_exit if the run stops before the report is printed below.
  ORDER_UNPRINTED=$ORDER_LOG
  export PG_GLIBC_AUDIT_STEP11=1
fi

banner() {
  echo
  echo "================================================================"
  echo "== $1"
  echo "================================================================"
}

# Each step's output is teed so the summary can quote it back. `set -o pipefail`
# is what makes the tee'd status the step's own status rather than tee's.
# For a step of WARNING_STEPS, each `!!` it prints is also written as data
# to its warnings file (glibc_locale_data.WARNINGS_ENV), created empty
# first, so that a step that printed none is told apart from one that
# never ran. Exported in the left side of the pipe only, a subshell: no
# later command inherits it. Step 11 does not run through run_step: its
# call is given its own file, above.
WARNING_STEPS="1 2"
run_step() {
  local num=$1; shift
  local log="$OUT_DIR/step$num.$PAIR.log"
  local warnings="$OUT_DIR/step$num.$PAIR.warnings.jsonl"
  case " $WARNING_STEPS " in
    *" $num "*)
      : > "$warnings"
      { export PG_GLIBC_AUDIT_WARNINGS="$warnings"; "$@"; } 2>&1 | tee "$log"
      ;;
    *) "$@" 2>&1 | tee "$log" ;;
  esac
}

# What was compared: each side, where it was read from, where PostgreSQL
# warns about a collation's version between them, and what no step reads
# (glibc_locale_data.compared_sides), with each machine's file named
# as it was given. What it says of the tags, the machines, the rebuilt
# builds and the glibc versions is written as data too
# (glibc_locale_data.COMPARED_RECORD). Its log keeps the name step
# 1's had: the warnings block below repeats the `!!` blocks of every
# step*.$PAIR.log, and has to repeat these (CODE_NOT_COMPARED is the one
# line that covers ko_KR without the machines' files), after step0's,
# whose note is "the first warning below".
# Never captured in a variable: a refusal (a reversed pair, a tag git does
# not hold) is printed, and pipefail ends the run with it.
compared() {
  if [ -n "$OLD_NODE" ]; then
    echo "Machines' files, as given:"
    echo "  old  $OLD_NODE"
    echo "  new  $NEW_NODE"
  fi
  python3 "$SCRIPTS/glibc_locale_data.py" sides "$OLD" "$NEW"
}
banner "WHAT WAS COMPARED"
run_step 1 compared

# Which pair this actually is, asked of git -- and asked HERE because the
# sides are what clone the repository, verify both refs and record each
# build's commit, so nothing before them can ask git anything. The sides ask
# the same question first, and refuse there, so
# that no finding is printed for a pair that is about to be rejected; step 2
# asks it again for itself, and --quiet is what keeps this call from adding
# one more copy of its `!!` block. Questions in one answer:
#
#   reversed -- the sides have already exited 2 and this script stopped
#               with them. Reversed, step 2 would run to the end; the
#               direction was never compared at all.
#   same     -- one commit, however each side is spelt (the second word is
#               then `commit`). This used to be
#               `[ "$OLD" = "$NEW" ]`, a comparison of TEXT, so
#               `./audit.sh glibc-2.39 ef321e23...` -- the sha this run's own
#               provenance line prints -- skipped the notice entirely and
#               summarised "no locale's LC_COLLATE changed".
#
# The second word says whether the pair compares anything at all: `commit`
# (one commit), `tree` (two commits of one source tree, in whatever order;
# the clone holds such pairs, not only fabricated ones) or `none`. Then
# three more, read below. Words on stdout and nothing else, so a forward
# pair adds nothing to the output.
ORDER_WORDS=$(python3 "$SCRIPTS/glibc_locale_data.py" order --quiet \
  --repo "$SCRIPTS/glibc" "$OLD" "$NEW")
# Then three more: what each side is (tag, commit or build) and the word a
# sentence about both uses (tags, builds or sides), apart from NOUN, the
# route, which keeps a commit on the tags route (TAG_SIDE, Removed).
read -r ORDER NOTHING OLD_CLASS NEW_CLASS WORD EXTRA <<EOF
$ORDER_WORDS
EOF

# A minor-version upgrade inside one RHEL major is two builds of the SAME
# upstream release, so the steps that compare the two tags have nothing to
# compare. Step 2 reports that nothing was compared. That is not a clean result, and C.UTF-8 is the proof:
# its order changed between RHEL 8.1 and 8.2, both of them upstream glibc 2.28.
SAME_TAG=0
case $NOTHING in
  commit|tree) SAME_TAG=1 ;;
  none) ;;
  *)
    echo "error: the order check answered '$ORDER_WORDS', whose second word is none" >&2
    echo "       of commit, tree or none. Not continuing." >&2
    exit 1
    ;;
esac
case $ORDER in
  # The `!!` block for this case comes from the order check that every step
  # taking the pair runs -- the sides and step 2 each print it -- so it
  # lands in their logs, which is where the warnings block at the bottom finds
  # it and repeats it once. It used to be echoed here, and then only the
  # wrapper said it: a hand-run step printed its clean sentence over a
  # comparison that had not happened.
  same|forward) ;;
  # The steps printed their own `!!` block; the summary repeats it, because a
  # warning 400 lines up has not been delivered.
  undetermined) ;;
  # No state falls through to the quiet branch: an unrecognised word means the
  # check did not answer, and "the pair is in order" is the assumption that
  # runs the whole audit against the wrong tag.
  *)
    echo "error: the order check answered '$ORDER', which is none of the" >&2
    echo "       states this script handles (same, forward, undetermined;" >&2
    echo "       a reversed pair never gets here, the sides refuse it)." >&2
    echo "       Not continuing." >&2
    exit 1
    ;;
esac
# Each side's class, then the pair's word: one of the nine pairs
# pair_word can answer, and nothing after them.
case "$OLD_CLASS $NEW_CLASS $WORD/$EXTRA" in
  "tag tag tags/"|"build build builds/") ;;
  "tag build sides/"|"build tag sides/"|"commit build sides/") ;;
  "build commit sides/"|"tag commit sides/"|"commit tag sides/") ;;
  "commit commit sides/") ;;
  *)
    echo "error: the order check answered '$ORDER_WORDS', whose last three words" >&2
    echo "       are not each side's class (tag, commit, build) and the pair's" >&2
    echo "       word (tags, builds, sides). Not continuing." >&2
    exit 1
    ;;
esac

banner "STEP 2  Which locales' sort rules changed"
run_step 2 python3 "$SCRIPTS/filter_lc_collate_changes.py" \
  --repo "$SCRIPTS/glibc" "$OLD" "$NEW"
# Absent is not empty. Step 2 writes these files whether or not it found
# anything, so a missing file means step 2 did not get that far -- an error to
# report, never an empty result to pass on as a clean audit.
#
# Unreachable today: under `set -e` a failing step 2 has already ended this
# script. Kept because it is the difference between a wrong answer and an
# error if step 2 ever stops writing, and the suite cannot reach it to prove
# that -- so treat it as untested defence, not as a checked guarantee.
if [ ! -f "$STEP2_LIST" ] || [ ! -f "$STEP2_NOT_COMPARED" ]; then
  echo "error: step 2 did not write both $STEP2_LIST" >&2
  echo "       and $STEP2_NOT_COMPARED." >&2
  echo "       Not continuing: an empty locale list would read as 'nothing" >&2
  echo "       changed'." >&2
  exit 1
fi

# Step 2's rules beside step 11's verdict, read from the two files this run
# wrote, never worked out again (locale_order.rules_beside). Lines that
# cannot be made are said where they would stand, with why: never step 11's
# answer alone as if they had been. Untested defence: both files are of
# the same measurements, so no run the suite makes reaches NOT REPORTED.
if [ -n "$OLD_ORDER" ]; then
  if ! python3 "$SCRIPTS/locale_order.py" --rules-beside "$VERDICTS" \
       "$RULES_FILE" "$CROSS" > "$CROSS_LOG" 2>&1; then
    rm -f "$CROSS"
  fi
fi
rules_beside() {
  if [ -s "$CROSS" ]; then
    cat "$CROSS"
  else
    echo "     NOT REPORTED -- step 2's rules could not be set beside each"
    echo "     locale of the old machine step 11 names:"
    sed 's/^/       /' "$CROSS_LOG"
  fi
}

# Step 2's list is the summary's first block. Every name in it was just
# written by step 2 for this pair (the absent check above), and it is
# printed, never passed on as an argument.

# Optional, and not part of the source audit: step 2 reads the clone and the
# builds, or each machine's own locale sources from its file, while step 11
# needs each machine's measurement. It runs only when you supply them. Every
# step keeps its number: in branch 9 step 1 became 'What was compared', and
# steps 3 to 10 were deleted.
if [ -n "$OLD_ORDER" ]; then
  banner "STEP 11  MEASURED ORDER  how each machine's own glibc sorts every locale"
  cat "$ORDER_LOG"
  ORDER_UNPRINTED=""
  echo
  rules_beside
fi

# ---------------------------------------------------------------- summary ----

# awk, not `grep -c ... || echo 0`: grep -c prints "0" AND exits 1 when
# nothing matches, so the fallback printed a second "0" and every clean run
# fed "0\n0" to `[ -gt ]`, which complained "integer expression expected" on
# stderr and fell into the else branch. The right branch, by luck.
count_lines() { [ -f "$1" ] || { echo 0; return; }; awk 'NF {n++} END {print n+0}' "$1"; }

# Same, minus the `#` provenance header step 2's removed list carries. Counting
# it would report one finding where there are none -- and "1 locale differs"
# is the wrong direction to be wrong in.
count_names() { [ -f "$1" ] || { echo 0; return; }; awk '!/^#/ && NF {n++} END {print n+0}' "$1"; }

banner "AUDIT SUMMARY  $OLD -> $NEW"

echo
echo "-- Listed by step 2"
# Each count is assigned before it is tested: a count that fails inside
# `[ ]` escapes set -e and falls through to "none".
LISTED=$(count_lines "$STEP2_LIST")
UNCOMPARED=$(count_lines "$STEP2_NOT_COMPARED")
HAS_C=$(awk '$0 == "C" {n++} END {print n+0}' "$STEP2_LIST")
if [ "$LISTED" -gt 0 ]; then
  # glibc's file C is C.UTF-8: PostgreSQL sorts a locale that is exactly C
  # without glibc. The list keeps C; the SQL template's listing matches it to
  # C and C.utf8 alike, and its sort stops on both and points to the probe.
  # The names step 2 could not compare come apart, under a line that says
  # why they are listed; their reasons are in the warnings, each in its own
  # step's words, an alias's under the locale it is built from (13.31, 17.2).
  awk -v skip="$STEP2_NOT_COMPARED" '
    BEGIN { while ((getline l < skip) > 0) out[l] = 1 }
    NF && !($0 in out)
  ' "$STEP2_LIST" | sed 's/^C$/C.UTF-8/; s/^/     /'
  if [ "$UNCOMPARED" -gt 0 ]; then
    echo "   Listed because step 2 could not compare them (the reason for each"
    echo "   one, or for the locale it is an alias of, is in the warnings below):"
    sed 's/^C$/C.UTF-8/; s/^/     /' "$STEP2_NOT_COMPARED"
  fi
  echo "   ($LISTED name(s); full list: $STEP2_LIST)"
  # Locales, not spellings (backlog 13.1): the names a machine and a database
  # use for one locale are many (sv_SE.utf8, sv_SE.iso885915, sv_SE.UTF-8),
  # and none of those lists can be complete. glibc's own rule takes each back
  # to the locale it is built from; the aliases that rule cannot reach are in
  # the list.
  echo "   These are locales, not spellings. A collation or a database uses one"
  echo "   when its locale, without the part from the dot up to any @, is in this"
  echo "   list (sv_SE.UTF-8, sv_SE.utf8 and sv_SE.iso885915 are all sv_SE). The"
  echo "   list also holds the aliases glibc's locale.alias gives these locales."
  echo "   sql/collation_confirmation_template.sql reads collations the same way."
  if [ "$HAS_C" -gt 0 ]; then
    echo "   C.UTF-8 is glibc's locale file C, written C in the full list; C.utf8"
    echo "   and C.UTF-8 are its spellings. A collation or a database whose locale"
    echo "   is exactly C or POSIX is compared by PostgreSQL itself, without glibc."
  fi
elif [ "$SAME_TAG" = 1 ]; then
  # One commit or one source tree: the order check's `!!`, which every step
  # taking the pair prints and the warnings block repeats once, says which,
  # and, for two machines' files, whether their charmaps were read: this
  # line never says it again in other words.
  echo "     none -- nothing was compared: the two $WORD are one commit or one"
  echo "     source tree (the warnings below say which, and what that leaves)"
else
  echo "     none -- of what step 2 compared, nothing differs; step 2, above,"
  echo "     names what it did not compare"
fi
# With the two measurements the summary holds a second answer to the same
# question, and this one comes first: it says where the other is, so that the
# tags' list -- or their "none" -- is not read as the whole answer. The
# machines' files give two builds, or two tags when a file holds no locale
# sources or names a build that is not of RHEL 8, 9 or 10 (machine_sides).
if [ -n "$OLD_ORDER" ]; then
  if [ "$NOUN" = builds ]; then
    echo "     This list is from each machine's own locale sources. What each"
    echo "     machine's own glibc says is under '-- Measured order', below."
  else
    echo "     This list is from the $NOUN. What each machine's own glibc says"
    echo "     is under '-- Measured order', below."
  fi
fi

# What the upgrade removes. A locale that is gone is worse than one that sorts
# differently. Whatever is built on it has no collation at all on the new
# system. Step 2 says so in its own output, above, and a reader of the
# summary alone must not conclude that the upgrade removes nothing.
echo
echo "-- Removed: locale files the old side has and the new side does not"
# First, so it is never read as a footnote to a "none" below it. On RHEL8 ->
# RHEL9 the tags remove nothing and the upgrade removes en_US@ampm, a file
# only RHEL8 ships. A file the new distro drops is as invisible to the tags.
# A build's locale files are its distro's own, checked against the package
# that installs them (build_sources.check_locales); a machine's are its own
# locale sources as its file holds them, checked against no package
# (MACHINE_NOT_CHECKED): with two of either, what follows is what the
# upgrade removes. A tag on one side cannot say what that side's distro adds.
if [ "$NOUN" = builds ] && [ -n "$OLD_NODE" ]; then
  echo "     Each machine's locale files are its own locale sources as its file"
  echo "     holds them, checked against no package ('Not checked for these"
  echo "     machines', below)."
elif [ "$NOUN" = builds ]; then
  echo "     Each build's locale files are the ones its glibc-locale-source"
  echo "     package installs, checked one by one ('What was compared')."
elif [ "$NOUN" = sides ]; then
  if [ "$TAG_SIDE" = old ]; then
    text="$TAG_NAME is an upstream $OLD_CLASS, so what your old distro adds"
    text="$text to it is NOT CHECKED: a file only that distro ships, and"
    text="$text $NEW does not, is not listed. Name the old side as a build"
    text="$text to check it."
  else
    text="$TAG_NAME is an upstream $NEW_CLASS, so what your new distro adds"
    text="$text to it or drops is NOT CHECKED: a file listed below may still"
    text="$text ship there, and one it drops is not listed. Name the new side"
    text="$text as a build to check it."
  fi
  printf '%s\n' "$text" | fold -s -w 70 | sed 's/ *$//; s/^/     /'
else
  echo "     The $WORD cannot show what your distro adds or drops: NOT CHECKED."
  if [ -n "$OLD_NODE" ]; then
    # Both sides read their tags because of a machine's file
    # (machine_sides), whose reason the run printed first and the warnings
    # block repeats from the step0 log.
    echo "     A machine's file made both sides read their tags; why is the"
    echo "     first warning below."
  else
    echo "     Pass --old-node and --new-node, each machine's file, to check it."
  fi
fi
# Each count is assigned before it is tested. A failing count inside `[ ]`
# escapes set -e, and the test then falls through to the branch that prints
# "none".
if [ -f "$STEP2_REMOVED" ]; then
  GONE=$(count_names "$STEP2_REMOVED")
fi
if [ ! -f "$STEP2_REMOVED" ]; then
  # Unreachable as the step stands: step 2 writes this list on every run,
  # and a step 2 that failed has already ended this script under set -e.
  echo "     Between the $WORD: NOT REPORTED -- step 2 wrote no removal list."
  echo "     Read its output above; do not read this as nothing removed"
elif [ "$GONE" -gt 0 ]; then
  echo "     Between the $WORD, gone at $NEW:"
  awk '!/^#/ && NF' "$STEP2_REMOVED" | sed 's/^C /C.UTF-8 /; s/^/       /'
  echo "   ($GONE name(s); full list: $STEP2_REMOVED)"
else
  echo "     Between the $WORD: none -- no locale file at $OLD"
  echo "     is gone at $NEW"
fi

# What each machine's own glibc says, beside what the tags say. Below Removed,
# not above it: that section sits directly under step 2's list on purpose,
# and this block is long. Step 11 reads no locale source: over RHEL8 ->
# RHEL9 it names ko_KR.utf8, whose file is byte-identical in both tags, and
# eight Swedish locales in which no character moves. Absent is not empty:
# without the two files this says NOT RUN.
echo
if [ -n "$OLD_ORDER" ]; then
  echo "-- Measured order: each machine's own glibc"
  if [ -s "$ORDER_SUMMARY" ]; then
    cat "$ORDER_SUMMARY"
    echo "     Full report: step 11 above."
    rules_beside
  else
    # Unreachable as the step stands: step 11 writes its summary before its
    # report, and a step 11 that failed has already ended this script.
    echo "     NOT REPORTED -- step 11 wrote no summary. Read its output above;"
    echo "     do not read this as nothing changed"
  fi
else
  echo "-- Measured order: NOT RUN"
  echo "     Pass --old-node and --new-node, each machine's file written by"
  echo "     scripts/locale_order.py --extract. Without them nothing above"
  echo "     asked either machine's glibc how it sorts, and a locale whose files"
  echo "     did not change can still sort differently. Nor did anything compare"
  echo "     the decimals of money, check which C library PostgreSQL runs on"
  echo "     either machine, check that each machine accepts every locale and"
  echo "     converts its text, or compare what each machine's character rules"
  echo "     (LC_CTYPE) answer."
fi

# Repeated verbatim. A warning that scrolled past 400 lines ago has not been
# delivered, and these are the cases where a clean result means nothing.
#
# Deduplicated by whole block. The sides and step 2 each print the order
# check's `!!` for a pair they cannot order; printing it twice trains the
# reader to skip the section, which costs more than the repetition buys.
# Identical text only -- two warnings that differ by one word are two
# warnings.
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
  printf '%s\n' "$WARNINGS"
  echo "(see docs/limitations.md)"
fi

# The run's data file, beside the text, from the files the steps wrote and
# the words above that only this script holds. Prints nothing when it is
# written; when it cannot be, says why on stderr and the run fails, so that
# a run never ends with no data file and exit 0.
python3 "$SCRIPTS/report_data.py" --out-dir "$OUT_DIR" --pair "$PAIR" \
  --old "$OLD" --new "$NEW" --route "$NOUN" \
  --order-words "$ORDER $NOTHING $OLD_CLASS $NEW_CLASS $WORD" \
  ${OLD_NODE:+--old-node "$OLD_NODE" --new-node "$NEW_NODE"} \
  ${TAG_SIDE:+--tag-side "$TAG_SIDE"} \
  ${LATEST_RECORD:+--old-asked "$ASKED_OLD" --new-asked "$ASKED_NEW"} \
  ${OLD_ORDER:+--step11}
# The summary page, from the data file alone; prints nothing when it is
# written, and the run fails when it cannot be.
python3 "$SCRIPTS/report_html.py" "$REPORT_DATA"

echo
