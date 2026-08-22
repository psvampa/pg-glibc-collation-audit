#!/usr/bin/env bash
# Usage: ./audit-locale-diff.sh <old_tag> <new_tag>
# Example: ./audit-locale-diff.sh glibc-2.28 glibc-2.34
#
# For any pair of glibc tags, lists which locales under localedata/locales/
# had any content change, and whether the master collation table
# (iso14651_t1_common, inherited by almost every locale) changed.
set -euo pipefail
OLD=$1
NEW=$2

if [ ! -d glibc ]; then
  git clone --filter=blob:none --no-checkout https://github.com/bminor/glibc.git
fi
cd glibc

git diff --name-only "$OLD..$NEW" -- localedata/locales/ | sort > /tmp/changed_locales.txt

echo "Total locale files with content changes: $(wc -l < /tmp/changed_locales.txt)"
echo "---"
echo "Did the master collation table change? (affects nearly everything via inheritance)"
git diff --stat "$OLD..$NEW" -- localedata/locales/iso14651_t1_common
echo "---"
echo "Full list in /tmp/changed_locales.txt"
