# pg-glibc-collation-audit

Deterministic audit for one question: **between two glibc versions (e.g. two
RHEL major releases), which specific locales change their sort order** — and
therefore which PostgreSQL B-tree indexes on `text`/`varchar`/`char`/`citext`
columns need a `REINDEX` after an OS upgrade or a physical migration?

Background: [PostgreSQL wiki — Locale data changes](https://wiki.postgresql.org/wiki/Locale_data_changes).
`pg_collation.collversion` only stores glibc's version string — it warns on
*any* version bump, whether or not your data would actually sort
differently, and stays silent if a distro patches collation data without
moving the reported version. It's a version comparison, not a check of the
real sort rules. This tool answers the real question directly, from glibc's
own source.

## The method

If the source file that defines a locale's collation rules did not change
between two glibc releases, that locale's sort order **cannot** have
changed. That's deterministic, not sampled — no need to guess or
brute-force-test every string.

1. **`scripts/audit-locale-diff.sh <old_tag> <new_tag>`** — clones glibc
   (shallow, blobs on demand) and checks the master collation table
   (`iso14651_t1_common`, inherited by most locales) first. If it changed,
   stop: essentially every locale is affected. If not, lists every locale
   file with *any* change (mostly noise: `LC_TIME`, `LC_MONETARY`, comments).
2. **`scripts/filter_lc_collate_changes.py <old_tag>`** — narrows that list
   to files whose change falls **inside** the `LC_COLLATE...END LC_COLLATE`
   block, the only part that can move sort order.
3. **`scripts/resolve_copy_closure.py <tag> <flagged_file1> [...]`** —
   closes a gap in step 2: a locale with no tailoring of its own, that just
   does `copy "some_other_locale"`, never shows up in a source diff (its
   file didn't change) even though its real sort order changes whenever the
   locale it copies does. This walks the full `copy` graph and adds every
   locale that inherits from a directly-changed one.

The output of step 3 is the real, complete set of affected locale
identifiers for that version pair.

## Confirming on a real system

A source diff is an argument, not a proof of what actually runs in
production. `sql/collation_confirmation_template.sql` is a template to run
on both the old and new OS/glibc, side by side: import system collations,
build a real B-tree index on a column using the flagged locale, and compare
`ORDER BY` output between the two.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

Check `locale -a` before comparing — if a locale isn't generated on the box,
`sort`/PostgreSQL silently fall back to `C`, and two boxes both missing it
will agree with each other while proving nothing.

## Worked example: RHEL8 → RHEL9 (glibc 2.28 → 2.34)

```sh
cd scripts
./audit-locale-diff.sh glibc-2.28 glibc-2.34
git diff -U0 glibc-2.28..glibc-2.34 -- localedata/locales/ > /tmp/localedata_full.diff  # inside glibc/, cloned by the previous step
cd glibc && python3 ../filter_lc_collate_changes.py glibc-2.28
python3 ../resolve_copy_closure.py glibc-2.34 or_IN sv_SE
```

Result: **`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro`** change sort order between
glibc 2.28 and 2.34 — `sv_FI` only via inheritance from `sv_SE`, never
flagged by a plain file diff. Every other locale (`en_US`, `de_DE`, `fr_FR`,
...) is unaffected, confirmed both by the empty master-table diff and by
running real `sort`/PostgreSQL tests on RHEL8 and RHEL9 nodes.

Full output and the PostgreSQL confirmation script for this pair:
[`examples/rhel8-to-rhel9-audit-output.txt`](examples/rhel8-to-rhel9-audit-output.txt),
[`examples/rhel8-to-rhel9.sql`](examples/rhel8-to-rhel9.sql).

## Scope

This audits **sort order (`LC_COLLATE`) only** — it says nothing about
`LC_CTYPE` behavior (`upper()`, `lower()`, character classification, pattern
matching), and nothing about ICU collations, which version their CLDR data
independently of the OS. It compares upstream glibc release tags; a vendor's
patched build isn't automatically covered by the same result.

## Requirements

`git`, `python3` (stdlib only), `bash`. The confirmation step needs a real
PostgreSQL instance with the relevant `glibc-langpack-*` packages installed
on each OS under test.

## Background

Written to accompany a Percona blog post on glibc collation risk across
RHEL OS upgrades. Link: TBD once published.
