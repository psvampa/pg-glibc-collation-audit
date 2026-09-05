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
changed — provided the code that compiles and compares those rules did not
change either. That's deterministic, not sampled: no need to guess or
brute-force-test every string.

Five steps. **1 to 3** check the locale data: what changed, whether the
change was inside `LC_COLLATE`, and which other locales inherit it.
**Step 4** identifies the locales the data alone can never settle, because
their weights are computed by `localedef` rather than stored in the file.
**Step 5** diffs that code, which is what decides whether step 4's list
matters for your two versions.

<details>
<summary><strong>The five steps in detail</strong> — what each one does and why</summary>

1. **`scripts/audit-locale-diff.sh <old_tag> <new_tag>`** clones glibc
   (shallow, blobs on demand), lists every locale file with *any* change
   (mostly noise: `LC_TIME`, `LC_MONETARY`, comments), and gives an explicit
   `CHANGED`/`UNCHANGED` verdict for the collation templates. It also
   computes each file's blast radius from the `copy` graph, so a change to a
   template that 328 locales inherit cannot read as one line out of 283.
2. **`scripts/filter_lc_collate_changes.py <old_tag> <new_tag>`** narrows
   that list to files whose change falls **inside** the
   `LC_COLLATE...END LC_COLLATE` block, the only part that can move sort
   order. Files added, deleted or renamed between the two tags are reported
   separately rather than dropped.
3. **`scripts/resolve_copy_closure.py <tag> <locale> [...]`** closes a gap in
   step 2: a locale with no tailoring of its own, that just does
   `copy "some_other_locale"`, never shows up in a source diff (its file
   didn't change) even though its real sort order changes whenever the
   locale it copies does. This walks the full `copy` graph — every `copy` in
   a file, not just the first — and adds every locale that inherits from a
   directly-changed one. It also maps the result through
   `localedata/SUPPORTED` to the generated names `locale -a` and
   `pg_collation` actually show. Note the spelling: `localedef` normalises
   the codeset when it builds the locale, so `SUPPORTED` says `sv_SE.UTF-8`
   while the installed locale, `locale -a` and `pg_collation` all say
   `sv_SE.utf8` — and `COLLATE "sv_SE.UTF-8"` does not exist.
4. **`scripts/flag_algorithmic_ranges.py <tag>`** finds locales whose
   `LC_COLLATE` uses range-expansion (ellipsis) syntax instead of an
   explicit per-character weight. Such a range is expanded algorithmically by
   glibc's locale compiler (`localedef`) at build time, not stored in the
   locale file itself. If the expansion logic changes between two releases,
   every character in the range can get a different weight with zero change
   to the locale's own source, so steps 1 to 3 alone cannot prove that
   locale is safe. The range may sit on its own line or, more often, inline
   on a `collating-symbol` line — both count. Four files do this as of glibc
   2.34: `ko_KR` (all 11,172 precomposed Hangul syllables), `iso14651_t1`
   (the CJK block U+4E00..U+9FA5), and `iso14651_t1_common` and `i18n`
   (the constructed Hangul and Han weight symbols). Between them they are
   inherited by 331 further locales — `en_US`, `de_DE`, `fr_FR`, `zh_CN`,
   `zh_TW`, `zh_HK`, `zh_SG` — so this step closes its own result over the
   `copy` graph too, reaching 335 of the 342 locales that define
   `LC_COLLATE`.
5. **`scripts/diff_collation_code.py <old_tag> <new_tag>`** diffs the glibc
   *code* that turns locale data into weights: `localedef`'s collation
   compiler and the runtime comparison functions. Steps 1 to 4 compare data;
   this compares the other half of the input. It is not hypothetical — the
   only sort-order-relevant change between glibc 2.28 and 2.34 lives here,
   not in `localedata/` (see the worked example). Comment and licence hunks
   are filtered out, with the filtered count always shown and `--all` to see
   everything. The filter only drops what it can prove is prose: a
   preprocessor directive, a label or a bare declarator counts as code,
   because a hunk dropped here is a hunk nobody reads. It also reports any
   tracked path that is absent at either tag, since `git diff` over a missing
   file is empty rather than an error.

Steps 3 and 5 together give the real, complete set of affected locale
identifiers for that version pair. Step 4 says which locales a data diff can
never clear on its own; step 5 says whether that matters for your two
versions. If step 5 reports no substantive change, a clean data diff is
sufficient even for the locales step 4 flags. If it reports one, every
locale step 4 lists needs an empirical test regardless of its data diff.

</details>

## Confirming on a real system

A source diff is an argument, not a proof of what actually runs in
production — and it says nothing about your distro's backports.
`sql/collation_confirmation_template.sql` is a template to run on both the
old and new OS/glibc, side by side: import system collations, build a real
index on a column using the flagged locale, and compare `ORDER BY` output
between the two. Run it for every locale steps 1 to 3 flagged, and — if step
5 found a substantive code change — for every locale step 4 flagged too,
regardless of whether it showed up in steps 1 to 3.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

Check `locale -a` before comparing: if a locale isn't generated on the box,
`sort`/PostgreSQL silently fall back to `C`, and two boxes both missing it
will agree with each other while proving nothing. Use the generated names
step 3 prints (`sv_SE.utf8`), not the source file names.

<details>
<summary><strong>What the template checks, and two traps</strong></summary>

- **Feed both sides byte-identical input.** glibc's `strcoll` really does
  report distinct strings as equal — measured on RHEL8/RHEL9, about 0.1% of
  random string pairs under `sv_SE`/`en_US`/`de_DE`, and about 10% under
  `ko_KR`. `sort(1)` resolves those tied lines by **input order**, so two
  nodes given differently-ordered input can differ for reasons that have
  nothing to do with glibc. PostgreSQL is not exposed to this: `varstr_cmp`
  and the sortsupport comparator both break strcoll ties with `strcmp`
  (`src/backend/utils/adt/varlena.c`, unchanged in substance from PG 13 to
  18), abbreviated keys cannot bypass it — a zero from the abbreviated
  comparator means "indeterminate", not "equal", and forces the full
  comparator — and nondeterministic collations, the one case where the
  tie-break is skipped, are rejected for every provider except ICU. So
  `ORDER BY` on a libc collation is always a total, plan-independent order.
  The `COLLATE "C"` in the template is a no-op kept to state intent.
- **The database default is the answer for most columns, and it is a
  separate question.** A `text` column with no explicit `COLLATE` does not
  carry a libc collation — it carries OID 100, `default`, a pointer resolved
  at runtime from `pg_database`. In a typical database that is most text
  columns; in a container it is usually all of them, because `initdb`
  inherits the locale from the environment and minimal images ship
  `LANG=C.UTF-8`. The template asks `pg_database` first and says plainly
  whether the database is exposed, then treats default-collated columns as in
  scope when it is. `C.UTF-8` under the `libc` provider **is** exposed:
  PostgreSQL special-cases only the literal strings `C` and `POSIX` to byte
  comparison, so libc `C.UTF-8` goes through `strcoll` like any other locale.
  The `builtin` provider's `C.UTF-8` (PG 17+) is a different implementation
  and is not exposed.
- Besides the index inventory, it reports **text partition keys**, every
  column carrying an affected collation, and a manual-review list of
  `CHECK`/`EXCLUDE` constraints. A partition key matters most: if the
  collation changes, rows can belong in a different partition than the one
  they are stored in, and no amount of reindexing fixes that — the rows have
  to be moved. A `REINDEX` list is not the whole answer.

</details>

## Results

Two version pairs run end to end. The two middle columns are the answer;
**Caught by** is which step got there, and whether a data diff was enough.

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 — `sv_FI` only via `copy` |
| `or_IN` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference <sup>†</sup> | nothing — outside this method entirely |
| `th_TH` | ⚪ Unaffected | 🟡 **Unresolved** | steps 1–3 |
| `ber_DZ`, `kab_DZ` | ⚪ Unaffected | 🟢 No difference | steps 1–3 flagged it; inspection found a role swap |
| CJK range U+4E00–U+9FA5 in `iso14651_t1`,<br>inherited by 328 locales | 🟢 No difference | 🟢 No difference | step 4 flagged it; step 5 says a diff can't clear it |
| `zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`,<br>`cns11643_stroke` | 🟢 No difference | 🟢 No difference | step 4 flagged them via `iso14651_t1_common`; cleared by measurement |
| everything else — `en_US`, `de_DE`,<br>`fr_FR`, … | ⚪ Unaffected | ⚪ Unaffected | `LC_COLLATE` and code both unchanged |

If you saved a result from this tool before 2026-09-05, check
[CHANGELOG.md](CHANGELOG.md) first. Two verdicts have moved since: `ko_KR`
was once reported unaffected for the RHEL8-to-RHEL9 pair and it changes, and
step 4 used to clear `zh_CN` and three siblings that it should have flagged.

- 🔴 **Changed** — sort order moves. Reindex.
- 🟡 **Unresolved** — flagged and *not* cleared. Treat as changed until tested.
- 🟢 **No difference** — the audit flagged it, but a targeted test or a
  mechanism argument shows the order does not move. Weaker than "unaffected".
- ⚪ **Unaffected** — neither the locale's `LC_COLLATE` nor the collation code
  changed. Note that a locale's *file* often changes without this being
  disturbed: most of the 283 files that differ between 2.28 and 2.34 changed
  only `LC_TIME` or `LC_MONETARY`. This is the deterministic verdict the
  method exists to produce.

<sup>†</sup> `C.UTF-8` is not auditable by this method at all — see
[Known limitations](#known-limitations). Its RHEL9→RHEL10 verdict is
ardentperf's checksum, not my own test.

Note that `ko_KR` is the row a data-only audit gets wrong, and `C.UTF-8` the
row no source diff can reach.

### Worked example: RHEL8 to RHEL9 (glibc 2.28 to 2.34)

`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro` and `ko_KR` change. Everything else
that this method can settle is clear; `zh_CN` and its pinyin siblings are
flagged by step 4 and cleared only by measurement.

<details>
<summary><strong>The full run, and the evidence for each verdict</strong></summary>

```sh
cd scripts
./audit-locale-diff.sh glibc-2.28 glibc-2.34
python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34
python3 resolve_copy_closure.py glibc-2.34 or_IN sv_SE
python3 flag_algorithmic_ranges.py glibc-2.34
python3 diff_collation_code.py glibc-2.28 glibc-2.34
```

Each script finds the glibc clone on its own and generates whatever diff it
needs from the two tags, so there is no intermediate file to keep in sync and
no working directory to get wrong.

Result, from the data diff: **`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro`** change
sort order between glibc 2.28 and 2.34 (as generated locales: `or_IN`,
`sv_SE`, `sv_SE.utf8`, `sv_FI`, `sv_FI.utf8`, `sv_FI@euro`). `sv_FI` comes
in only via inheritance from `sv_SE` and is never flagged by a plain file
diff.

Result, from the code diff: **`ko_KR` also changes**, and its data file is
byte-identical between the two tags. Step 5 pins the cause to a single
commit, [`82292c99b2`](https://sourceware.org/bugzilla/show_bug.cgi?id=22668)
("LC_COLLATE: Fix last character ellipsis handling", Bug 22668), which landed
in glibc 2.34 and changed how `localedef` expands the ellipsis ranges that
`ko_KR` depends on. Two independent checks agree: `localedata/locales/ko_KR`
is unchanged across both `2.28..2.34` and `2.31..2.36`, and
[ardentperf's](https://github.com/ardentperf/glibc-unicode-sorting) 25-million-string
checksums show `ko` changing on Debian exactly between 2.31 and 2.36 —
bracketing 2.34. This is the case a data-only audit gets wrong, which is why
step 5 exists.

Step 4 also flags `iso14651_t1`'s CJK range (U+4E00..U+9FA5), inherited by 328
locales. Step 5 shows the ellipsis logic did change in this pair, so a source
diff cannot clear those locales either. Empirically they are fine, and this
one is explainable rather than merely observed: the ellipsis is followed by an
explicit `<U9FA5>` line, so the stale cursor re-inserts U+9FA5 exactly where
it already was. Predicted from the source, then confirmed on real nodes — the
range boundary (`一 龤 龥 龦`) sorts identically under `en_US` and `zh_TW` on
glibc 2.28 and 2.34 — and independently corroborated by ardentperf, whose
corpus covers every Unicode code point and whose `en`, `de` and `fr`
checksums are identical across RHEL8 and RHEL9.

The caveat that remains: my own test covers the range *boundary*, which is
where this particular bug lives, not a broad CJK corpus, and `zh_TW`/`zh_HK`/
`zh_SG` are absent from ardentperf's set. For those three the evidence is a
mechanism argument plus a targeted test, not a broad empirical sweep.

Every other locale (`en_US`, `de_DE`, `fr_FR`, ...) is unaffected, confirmed
by the unchanged templates and by real `sort`/PostgreSQL tests on RHEL8 and
RHEL9 nodes. `zh_CN` is not in that group: it reaches
`iso14651_t1_common` through `iso14651_t1_pinyin`, so step 4 flags it and the
`sort` measurement — not the clean data diff — is what clears it.

#### The `ko_KR` mechanism, and its minimal test case

`ko_KR`'s `LC_COLLATE` is `<UAC00>` / `..` / `<UD7A3>` (the Hangul syllables),
immediately followed by the Hanja list starting at `<U4F3D>`. Before the fix
the cursor was left on `<UD7A2>` instead of `<UD7A3>` after expanding the
ellipsis, so the first Hanja was linked in *before* `<UD7A3>` — leaving the
last Hangul syllable dangling at the very end of the section:

```
$ printf '가\n힢\n힣\n伽\n佳\n' | LC_ALL=ko_KR.UTF-8 sort | tr '\n' ' '
glibc 2.28:  가 힢 伽 佳 힣      <- U+D7A3 dangling at the end
glibc 2.34:  가 힢 힣 伽 佳      <- U+D7A3 back in place
```

That is the whole difference: `힣` (U+D7A3) versus any Hanja. It is also why a
hand-picked sample of everyday Korean text shows nothing — U+D7A3 is the last
syllable of the Hangul block, and text essentially never reaches it. Derive
test strings from the rule that changed; for a `localedef` change that means
the boundaries of the affected range.

Full output and the PostgreSQL confirmation script for this pair:
[`examples/rhel8-to-rhel9-audit-output.txt`](examples/rhel8-to-rhel9-audit-output.txt),
[`examples/rhel8-to-rhel9.sql`](examples/rhel8-to-rhel9.sql).

</details>

### Worked example: RHEL9 to RHEL10 (glibc 2.34 to 2.39)

`ber_DZ`, `kab_DZ` and `th_TH` are flagged; only `th_TH` stays unresolved.
`ko_KR` is flagged by step 4 and then cleared by step 5.

<details>
<summary><strong>The full run, and how step 5 clears a step-4 locale</strong></summary>

Same steps, different pair. Result: **`ber_DZ`, `kab_DZ`, `th_TH`** flagged
by steps 1 to 3, `ko_KR` flagged again by step 4. On inspection, `ber_DZ`
and `kab_DZ` turned out to be a role swap (the same collation ruleset,
relocated to the other file), not an actual rule change, confirmed by an
empirical test showing no observable difference. `th_TH` is a real
rewrite; my own empirical sample showed no difference but was narrow, so
treat it as a candidate for a broader check on real Thai data, not as
cleared.

This pair also shows step 5 working in the other direction. `ko_KR` is
flagged by step 4 here too, but step 5 finds no change to ellipsis expansion
between 2.34 and 2.39 — the tier-1 changes in that range are `%Z`-to-`%z`
format fixes, integer type replacements, and a new opt-in
`codepoint_collation` keyword that no pre-existing locale uses. So `ko_KR`
is genuinely unaffected across RHEL9 to RHEL10, which steps 1 to 4 alone
could never conclude. ardentperf's checksums agree: all ten of their locales,
`ko` included, are identical between RHEL9 and RHEL10. Being able to clear a
step-4 locale, rather than only ever flagging it, is the point of step 5.

Full output: [`examples/rhel9-to-rhel10-audit-output.txt`](examples/rhel9-to-rhel10-audit-output.txt).

</details>

## Tested on

- **RHEL8 to RHEL9** (glibc 2.28 to 2.34): full method run, plus empirical
  confirmation on side-by-side Rocky 8 / Rocky 9 nodes carrying
  `glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8` — the same package
  versions ardentperf tested — both running PostgreSQL 16.15. Every claim in
  the worked example is measured, not inferred; see
  [`examples/rhel8-to-rhel9-audit-output.txt`](examples/rhel8-to-rhel9-audit-output.txt).
  The distro-versus-upstream backport check under [Known
  limitations](#known-limitations) was measured on these same two nodes.
- **RHEL9 to RHEL10** (glibc 2.34 to 2.39): full method run, confirmed
  against real nodes running PostgreSQL, not just the source diff; see
  [`examples/rhel9-to-rhel10-audit-output.txt`](examples/rhel9-to-rhel10-audit-output.txt).
  The empirical lines in that file predate step 5 and are marked as such.
- **RHEL7 to RHEL8** (glibc 2.17 to 2.28, the large jump that rewrote the
  master table): source-diff steps run, but **not** confirmed against real
  RHEL7 nodes, and no output file is kept for it. RHEL7's `systemd` doesn't
  boot under a cgroups-v2-only container host — a limitation of my test
  environment, not of the method. The short version: all three collation
  templates changed, and 86 of the 310 content-changed locale files have the
  change inside `LC_COLLATE`, so on this pair essentially everything is
  affected and the audit is not the interesting part.

## Known limitations

- **`C.UTF-8` cannot be audited by this method.** Its source file,
  `localedata/locales/C`, only exists upstream from glibc **2.35**, but RHEL8
  and RHEL9 both ship a backported `C.UTF-8` — and it **does** change between
  them (Bug 22668 again; the commit message calls out `C.UTF-8` explicitly).
  Comparing upstream 2.28 and 2.34 cannot see a file that is in neither.
  Confirmed on real nodes, sorting U+10FFFF, U+FFFF, U+07FF and U+007F under
  `C.utf8`:

  ```
  glibc 2.28:  FFFF, 10FFFF, 007F, 07FF     <- not codepoint order
  glibc 2.34:  007F, 07FF, FFFF, 10FFFF     <- correct
  ```

  This does not affect `COLLATE "C"`, which is byte order and immutable, but
  it does affect indexes built on `C.UTF-8`. Test that one empirically.

  This is the configuration to watch, because it is the default almost
  everywhere `initdb` runs in a container: a `libc` provider with `C.UTF-8` as
  the database collation means every text column without an explicit `COLLATE`
  is exposed to a glibc upgrade, while the source-diff audit is structurally
  blind to that locale.

  PostgreSQL is blind to it too, which is worth knowing before you rely on a
  `collversion` mismatch as your warning. Under the `libc` provider,
  `get_collation_actual_version()` returns NULL for `C`, for `POSIX` and for
  **anything whose name starts with `C.`** — the `pg_strncasecmp("C.", ...)`
  test, present in every branch from PG 14 on
  (`src/backend/utils/adt/pg_locale.c` through PG 17,
  `pg_locale_libc.c` from PG 18). So `collversion` and `datcollversion`
  stay NULL for `C.UTF-8`, the mismatch check in the SQL template can never
  fire for it, and neither can PostgreSQL's own warning on a version bump.
  Nothing covers the source half and nothing covers the catalog half, so for
  `C.UTF-8` the empirical comparison is not optional.
- **Upstream tags are not your distro's glibc.** RHEL8 ships
  `glibc-2.28-251.el8` with hundreds of backports; a backported collation
  change would be invisible to a `glibc-2.28..glibc-2.34` diff, which is the
  main reason not to skip the confirmation step on real nodes.

  For the flagship `RHEL8 -> RHEL9` pair this is measured rather than left
  open. Comparing every distro locale source in `/usr/share/i18n/locales/`
  (package `glibc-locale-source`) against the same file at the upstream tag:
  `glibc-2.28-251.el8_10.40` differs from `glibc-2.28` in **73 of 355** files,
  and `glibc-2.34-275.el9_8` differs from `glibc-2.34` in **2 of 356**. In
  **none** of them does the difference fall inside the `LC_COLLATE` block. The
  backports on both sides land in other categories, so for every locale this
  method can audit, the tag diff is reading the same collation data the nodes
  run.

  The exception is the one named above: `C.UTF-8` is a backported collation
  change on exactly this pair, and it is invisible here for the same reason it
  is invisible to the audit — the file it comes from exists at neither tag, so
  there is nothing to compare it against. This measurement is a statement about
  the locales the method covers, and it does not rescue the one it does not.

  A comparison that always answered "identical" would produce that same zero,
  so the check carries a positive control against locales whose answer is known
  from step 2: the same method marks `sv_SE` and `or_IN` as different from
  `glibc-2.34` and identical to `glibc-2.28`, and `ko_KR` identical to both.

  The claim is bounded by what was compared — these two package versions
  against these two tags — and is not a general result that distro backports
  never touch collation. The other two pairs stand differently, and the
  difference is worth being explicit about:

  | Pair | Backports measured? | What covers the gap |
  |---|---|---|
  | `RHEL8 -> RHEL9` | **yes**, the numbers above | the measurement itself |
  | `RHEL9 -> RHEL10` | no — it needs a RHEL10 node, and the measurement above was taken on Rocky 8 / Rocky 9 only | the empirical confirmation in [`examples/rhel9-to-rhel10-audit-output.txt`](examples/rhel9-to-rhel10-audit-output.txt), run on RHEL9 / RHEL10 nodes |
  | `RHEL7 -> RHEL8` | no | **nothing** — the pair was never confirmed on real nodes either |

  So for `RHEL9 -> RHEL10` the cover is empirical, not analytical: it rests on
  observed sort order on real nodes rather than on knowing the distro's
  collation data matches the tag. And on `RHEL7 -> RHEL8` a backported
  collation change would still pass unnoticed today.
- **The migration's DESTINATION must be glibc 2.24 or newer** — RHEL 8+,
  Ubuntu 18.04+, Debian 9+, SLES 15+. Note the direction: auditing *from* an
  older system is fine, so `RHEL 7 -> RHEL 8` is correct. What is out of scope
  is auditing *towards* RHEL 7 or older.

  In glibc 2.23 and earlier, the three master templates (`iso14651_t1`,
  `iso14651_t1_common`, `iso14651_t1_pinyin`) begin with `LC_COLLATE` on the
  very first byte of the file, and the block reader does not recognise them
  there. Steps 3 and 4 walk the `copy` graph at the **new** tag, so when that
  tag is old the graph loses its three roots and the inheritance closure
  collapses — silently, in the reassuring direction.

  Measured on `glibc-2.12 -> glibc-2.17`, a RHEL 6 to RHEL 7 audit: step 2
  correctly finds `iso14651_t1_common` changed, then step 3 reports **11**
  affected locales where there are **278**, and step 4 reports **2** exposed
  where there are **279**. The 267 names it drops include `en_US`, `de_DE`,
  `fr_FR`, `es_ES`, `it_IT`, `nl_NL`, `pt_BR`, `ru_RU`, `sv_SE`, `zh_CN` and
  `zh_TW`. What actually changes over that pair is 109 Tibetan code points
  gaining a collation weight in `iso14651_t1_common`.

  There is no guard for this: run the tool outside the supported range and it
  answers confidently and wrongly.
- **Character repertoire changes are not audited.** `localedata/charmaps/`
  is an input to `localedef` — it decides which characters exist to be given a
  weight — and no step looks at it. `charmaps/UTF-8` gains 1632 code points
  over 2.28..2.34, 1658 over 2.34..2.39 and 5235 over 2.39..2.41.

  Measured rather than assumed: 99 of those newly added code points, mixed
  with pre-existing reference characters and sorted on real RHEL8 and RHEL9
  nodes, come out in **identical order** under `en_US`, `sv_SE`, `de_DE`,
  `ar_SA` and `zh_CN`. Adding a character to the repertoire gives that
  character a weight; it does not move the characters that already had one. So
  this is a gap in coverage with no demonstrated impact — but it is a gap, and
  only data containing those characters could ever be affected by it.
- **Step 5 reports, it does not decide.** It cannot tell a weight-changing
  commit from a harmless one — over 2.28..2.34 it surfaces both the ellipsis
  fix (which reorders `ko_KR`) and a hash-table sizing change (which reorders
  nothing). Read the hunks it prints.

## Relationship to ardentperf/glibc-unicode-sorting

This tool is a complement to [ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
not a replacement for it: they sort ~25 million real strings and checksum the
result across roughly nine languages, this diffs glibc's source across all
~355 locales. Check both where they overlap.

One thing to know before reading their tables: they report a `glibc` **and**
an `icu` engine. Between RHEL8 and RHEL9 every locale changes under ICU (60.3
to 67, a full CLDR jump) while only `ko` and `C.UTF-8` change under glibc, so
a `zh` change read off those tables is an ICU result and carries no `REINDEX`
implication for a libc collation.

<details>
<summary><strong>Where each method is blind, and how their tables read</strong></summary>

- **ardentperf sorts ~25 million real strings and checksums the result.**
  Broad, empirical, and covering every Unicode code point. It can catch a
  real behavior change from *anywhere* in glibc, including a distro backport
  that no upstream diff would show. Its `ko_KR` finding between RHEL8 and
  RHEL9 is what prompted step 5 of this tool, which now root-causes it to
  Bug 22668.
- **This tool diffs glibc's locale source and proves the negative.** It
  isn't sampled, so it covers all ~355 locales, not the roughly nine
  languages ardentperf's fixed test set covers. Running the RHEL9-to-RHEL10
  audit found real `LC_COLLATE` changes in `ber_DZ`, `kab_DZ`, and `th_TH`
  (Berber, Kabyle, and Thai), none of which are in ardentperf's tested
  language list, so none of them would show up there one way or the other.

If your locale is one of the roughly nine languages ardentperf tests,
check both: their result plus this tool's result gives you empirical
evidence and a deterministic proof for whatever this tool can prove. If
your locale isn't in their list, this tool is the only one of the two that
says anything about it at all.

One thing worth knowing when reading their tables: they report both a
`glibc` and an `icu` engine. Between RHEL8 and RHEL9, **every** locale
changes under ICU (60.3 to 67, a full CLDR jump) while only `ko` and
`C.UTF-8` change under glibc. `zh_CN` in particular came out **unchanged**
under glibc for this pair when I measured it on RHEL8 and RHEL9 nodes, so a
`zh` change read off those tables is an ICU result, not a glibc one, and
carries no `REINDEX` implication for a libc collation.

  Note what that argument does *not* rest on. `zh_CN` and
  `iso14651_t1_pinyin` are both byte-identical from 2.28 through 2.42, but
  that proves nothing on its own: the chain ends at `iso14651_t1_common`,
  whose `collating-symbol <SAC00>..<SD7A3>` and `<RFB40>..<RFB41>` ranges are
  expanded by `localedef` at build time and are exactly what step 4 exists to
  flag. Step 4 does flag `zh_CN`; the evidence that clears it is the
  measurement, not the diff. Earlier versions of this tool missed the inline
  ellipsis form and cleared it from source alone — see
  [CHANGELOG.md](CHANGELOG.md). Their set also contains no `sv` or
`or_IN`, the two locales this tool finds for the same pair, so the two
results do not overlap as much as they first appear.

</details>

## Scope

This audits **sort order (`LC_COLLATE`) only**. It says nothing about
`LC_CTYPE` behavior (`upper()`, `lower()`, character classification, pattern
matching).

It is also about glibc, so in PostgreSQL terms it applies to the **`libc`
provider** and nothing else. ICU collations version their CLDR data
independently of the OS. The `builtin` provider (PG 17+) implements `C`,
`C.UTF-8` and `PG_UNICODE_FAST` inside PostgreSQL, in code-point order, with
no call into glibc — so `initdb --locale-provider=builtin --locale=C.UTF-8`
is immune to everything this tool looks for, and is the mitigation if the
`C.UTF-8` limitation above is what you are worried about. Note that `initdb`
still defaults to `libc` in PG 16, 17 and 18, so you have to ask for
`builtin`.

Within `LC_COLLATE`, steps 4 and 5 are what keep
"a clean diff proves nothing changed" honest: a locale relying on range
expansion needs an empirical check whenever step 5 reports a change in the
expansion logic. See **Known limitations** above for what this method
structurally cannot see.

## Requirements

For the source audit (steps 1 to 5): `git`, `python3` (stdlib only), `bash`.
Nothing else, and no PostgreSQL.

Step 1 prints the commit id behind each tag and the state of its GPG signature
before it diffs anything: the clone is a third-party mirror
(`github.com/bminor/glibc`) and a git tag is a mutable pointer, so "I audited
glibc-2.39" is a weaker claim than it looks. An invalid signature aborts; an
unverifiable one (no `gpg`, or no key for it) is reported and the audit
continues, because refusing to run buys no truth.

There is a test suite — `python3 -m unittest discover -s tests -t tests`, about
17 seconds. It pins the three tags to their commit ids, so a moved tag reports
itself as a moved tag instead of as a change in the results. It needs the glibc clone for two of its three layers and skips them
with a reason if it is absent. [`tests/README.md`](tests/README.md) says what it
covers and, more usefully, what it does not.

For the confirmation step: a real PostgreSQL instance on each OS under test,
**version 15 or newer** — `sql/collation_confirmation_template.sql` reads
`pg_database.datlocprovider` and calls `pg_collation_actual_version()` and
`pg_database_collation_actual_version()`, all of which arrived in 15. The
template's header says what to drop to run it on 13 or 14.

Install the relevant `glibc-langpack-*` packages on each node first. If you
install them *after* `initdb`, the collations will not exist yet and
`CREATE TABLE ... COLLATE "sv_SE.utf8"` fails with `collation ... does not
exist` — which is why the template calls `pg_import_system_collations()`
before anything else.

**Restart PostgreSQL after installing a langpack, before re-running that
function.** Re-running it alone is not enough, and it fails quietly: the
function reports a plausible count and returns success while importing only
the locales that existed when the postmaster started. `pg_import_system_collations()`
takes its list from `locale -a` — a fresh subprocess, which sees the new
locales — but it then validates each one with `setlocale()` inside the
backend, and that resolves against the `locale-archive` the postmaster
already has mapped. Measured on Rocky 8 with PostgreSQL 16.15, installing
`glibc-all-langpacks` and calling the function without a restart imported
**72** collations and left `sv_SE.utf8` absent; `systemctl restart
postgresql-16` and the same call imported **931** more, for 1007 in total.

Minimal container images add a second trap ahead of that one: they ship
`/etc/rpm/macros.image-language-conf` containing `%_install_langs en_US`, so
`dnf install glibc-all-langpacks` reports success and installs nothing beyond
English — `locale -a` stayed at 59 entries. Remove that file and reinstall.
