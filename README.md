# pg-glibc-collation-audit

Deterministic audit for one question: **between two glibc versions (e.g. two
RHEL major releases), which specific locales change their sort order**, and
therefore which PostgreSQL B-tree indexes on `text`/`varchar`/`char`/`citext`
columns need a `REINDEX` after an OS upgrade or a physical migration?

Background: [PostgreSQL wiki, Locale data changes](https://wiki.postgresql.org/wiki/Locale_data_changes).
`pg_collation.collversion` only stores glibc's version string, so it warns on
*any* version bump, whether or not your data would actually sort
differently, and stays silent if a distro patches collation data without
moving the reported version. It's a version comparison, not a check of the
real sort rules. This tool answers the real question directly, from glibc's
own source.

## The method

If the source file that defines a locale's collation rules did not change
between two glibc releases, that locale's sort order **cannot** have
changed, for the vast majority of locales. That's deterministic, not
sampled: no need to guess or brute-force-test every string. One category of
locale needs an extra check (step 4 below), and this method identifies
exactly which ones.

1. **`scripts/audit-locale-diff.sh <old_tag> <new_tag>`** clones glibc
   (shallow, blobs on demand) and checks the master collation table
   (`iso14651_t1_common`, inherited by most locales) first. If it changed,
   stop: essentially every locale is affected. If not, lists every locale
   file with *any* change (mostly noise: `LC_TIME`, `LC_MONETARY`, comments).
2. **`scripts/filter_lc_collate_changes.py <old_tag>`** narrows that list
   to files whose change falls **inside** the `LC_COLLATE...END LC_COLLATE`
   block, the only part that can move sort order.
3. **`scripts/resolve_copy_closure.py <tag> <flagged_file1> [...]`** closes
   a gap in step 2: a locale with no tailoring of its own, that just does
   `copy "some_other_locale"`, never shows up in a source diff (its file
   didn't change) even though its real sort order changes whenever the
   locale it copies does. This walks the full `copy` graph and adds every
   locale that inherits from a directly-changed one.
4. **`scripts/flag_algorithmic_ranges.py <tag>`** finds locales whose
   `LC_COLLATE` uses range-expansion syntax (a line like `<UAC00>`, a line
   `..`, a line `<UD7A3>`) instead of an explicit per-character weight. That
   range is expanded algorithmically by glibc's locale compiler
   (`localedef`) at build time, not stored in the locale file itself. If the
   expansion logic changes between two glibc releases, every character in
   that range can get a different weight with zero change to the locale's
   own source, so steps 1 to 3 alone cannot prove that locale is safe. As of
   glibc 2.34, exactly one locale in the entire corpus does this: `ko_KR`
   (all 11,172 precomposed Hangul syllables). Any locale this step flags
   needs an empirical test (step below) before you trust a "not flagged"
   result from steps 1 to 3, even if its own file is byte-identical between
   the two tags.

The output of steps 3 and 4 together is the real, complete set of affected
locale identifiers for that version pair, plus the short list that needs
empirical confirmation regardless of what the diff says.

## Confirming on a real system

A source diff is an argument, not a proof of what actually runs in
production. `sql/collation_confirmation_template.sql` is a template to run
on both the old and new OS/glibc, side by side: import system collations,
build a real B-tree index on a column using the flagged locale, and compare
`ORDER BY` output between the two. Run this for every locale steps 1 to 3
flagged, and also for every locale step 4 flagged, regardless of whether
step 4's locale showed up in steps 1 to 3.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

Check `locale -a` before comparing: if a locale isn't generated on the box,
`sort`/PostgreSQL silently fall back to `C`, and two boxes both missing it
will agree with each other while proving nothing.

## Worked example: RHEL8 to RHEL9 (glibc 2.28 to 2.34)

```sh
cd scripts
./audit-locale-diff.sh glibc-2.28 glibc-2.34
git diff -U0 glibc-2.28..glibc-2.34 -- localedata/locales/ > /tmp/localedata_full.diff  # inside glibc/, cloned by the previous step
cd glibc && python3 ../filter_lc_collate_changes.py glibc-2.28
python3 ../resolve_copy_closure.py glibc-2.34 or_IN sv_SE
python3 ../flag_algorithmic_ranges.py glibc-2.34
```

Result: **`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro`** change sort order between
glibc 2.28 and 2.34; `sv_FI` only via inheritance from `sv_SE`, never
flagged by a plain file diff. `ko_KR` is flagged by step 4 as needing
empirical confirmation regardless of its (unchanged) file diff. Every other
locale (`en_US`, `de_DE`, `fr_FR`, ...) is unaffected, confirmed both by the
empty master-table diff and by running real `sort`/PostgreSQL tests on
RHEL8 and RHEL9 nodes. We ran the empirical test for `ko_KR` too (everyday
Korean text and the specific Hangul range touched by a related glibc bug
fix) and found no observable difference on this version pair, though a
third-party broader test using a much larger string corpus reports one, so
treat `ko_KR` specifically as unresolved rather than cleared.

Full output and the PostgreSQL confirmation script for this pair:
[`examples/rhel8-to-rhel9-audit-output.txt`](examples/rhel8-to-rhel9-audit-output.txt),
[`examples/rhel8-to-rhel9.sql`](examples/rhel8-to-rhel9.sql).

## Worked example: RHEL9 to RHEL10 (glibc 2.34 to 2.39)

Same steps, different pair. Result: **`ber_DZ`, `kab_DZ`, `th_TH`** flagged
by steps 1 to 3, `ko_KR` flagged again by step 4. On inspection, `ber_DZ`
and `kab_DZ` turned out to be a role swap (the same collation ruleset,
relocated to the other file), not an actual rule change, confirmed by an
empirical test showing no observable difference. `th_TH` is a real
rewrite; our own empirical sample showed no difference but was narrow, so
treat it as a candidate for a broader check on real Thai data, not as
cleared. Full output: [`examples/rhel9-to-rhel10-audit-output.txt`](examples/rhel9-to-rhel10-audit-output.txt).

## Tested on

- **RHEL8 to RHEL9** (glibc 2.28 to 2.34) and **RHEL9 to RHEL10** (glibc
  2.34 to 2.39): full method run, confirmed against real nodes running
  PostgreSQL, not just the source diff.
- **RHEL7 to RHEL8** (glibc 2.17 to 2.28, the large jump that rewrote the
  master table): source-diff steps run and documented, but not confirmed
  against real RHEL7 nodes (RHEL7's `systemd` doesn't boot under a
  cgroups-v2-only container host, a limitation of our test environment,
  not of the method).

## Known limitation: `ko_KR`

As of glibc 2.39 (the newest tag checked), `ko_KR` is still the only locale
flagged by step 4, on every version pair we've run. We cannot currently
resolve whether its sort order actually changes between any two glibc
versions; only that a file diff alone can never answer that question for
this specific locale, because its collation weights are computed by
`localedef`, not stored in its source file. If you run `ko_KR` and are
migrating across a glibc boundary, treat it as unresolved and run your own
empirical test against representative data, not as cleared by this tool.

## Relationship to ardentperf/glibc-unicode-sorting

This tool is a complement to [ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
not a replacement for it. Different method, different blind spots:

- **ardentperf sorts ~25 million real strings and checksums the result.**
  Broad, sampled, empirical. It can catch a real behavior change from
  *anywhere* in glibc, including changes this tool cannot see by
  construction, like the `localedata/charmaps/UTF-8` update or the
  `ko_KR` range-expansion case above. It found a `ko_KR` difference
  between RHEL8 and RHEL9 that this tool cannot confirm or deny.
- **This tool diffs glibc's locale source and proves the negative.** It
  isn't sampled, so it covers all ~355 locales, not the roughly nine
  languages ardentperf's fixed test set covers. Running the RHEL9-to-RHEL10
  audit found real `LC_COLLATE` changes in `ber_DZ`, `kab_DZ`, and `th_TH`
  (Berber, Kabyle, and Thai), none of which are in ardentperf's tested
  language list, so none of them would show up there one way or the other.

If your locale is one of the roughly nine languages ardentperf tests,
check both: their result plus this tool's result gives you sampled
evidence and a deterministic proof for whatever this tool can prove. If
your locale isn't in their list, this tool is the only one of the two that
says anything about it at all, except for the `ko_KR`-style edge case
where only a broader sampled test like theirs can settle it.

## Scope

This audits **sort order (`LC_COLLATE`) only**. It says nothing about
`LC_CTYPE` behavior (`upper()`, `lower()`, character classification, pattern
matching), and nothing about ICU collations, which version their CLDR data
independently of the OS. It compares upstream glibc release tags, so a
vendor's patched build isn't automatically covered by the same result. And
even within `LC_COLLATE`, step 4 above is the one documented exception to
"a clean diff proves nothing changed": any locale relying on range
expansion needs an empirical check no matter what the diff says.

## Requirements

`git`, `python3` (stdlib only), `bash`. The confirmation step needs a real
PostgreSQL instance with the relevant `glibc-langpack-*` packages installed
on each OS under test.
