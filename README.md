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

## Quickstart

Needs `git`, `python3` (stdlib only) and `bash`. Nothing else, and no
PostgreSQL. The RHEL8-to-RHEL9 pair, end to end:

```sh
cd scripts
./audit-locale-diff.sh               glibc-2.28 glibc-2.34   # step 1
python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34   # step 2
python3 resolve_copy_closure.py      glibc-2.34 or_IN sv_SE  # step 3
python3 flag_algorithmic_ranges.py   glibc-2.34              # step 4
python3 diff_collation_code.py       glibc-2.28 glibc-2.34   # step 5
```

Each script finds the glibc clone on its own and generates whatever diff it
needs from the two tags, so there is no intermediate file to keep in sync and
no working directory to get wrong.

## What the five steps do

If the source file that defines a locale's collation rules did not change
between two glibc releases, that locale's sort order **cannot** have changed —
provided the code that compiles and compares those rules did not change either.
That's deterministic, not sampled.

1. **`audit-locale-diff.sh`** — clones glibc, prints the commit id and GPG
   signature state behind each tag, then gives an explicit
   `CHANGED`/`UNCHANGED` verdict for the collation templates, with each file's
   blast radius over the `copy` graph.
2. **`filter_lc_collate_changes.py`** — narrows that to files whose change
   falls *inside* the `LC_COLLATE` block, the only part that can move sort
   order. Added, deleted and renamed files are reported separately, not
   dropped.
3. **`resolve_copy_closure.py`** — adds the locales that inherit a changed one
   through `copy` and so never appear in a file diff, mapped to the names
   `locale -a` and `pg_collation` actually show (`sv_SE.utf8`, not
   `sv_SE.UTF-8`).
4. **`flag_algorithmic_ranges.py`** — lists the locales steps 1 to 3 can never
   clear from data alone, because their weights are computed by `localedef`
   from an ellipsis range rather than stored in the locale file.
5. **`diff_collation_code.py`** — diffs the glibc *code* that turns locale data
   into weights. The only sort-order-relevant change between glibc 2.28 and
   2.34 lives here, not in `localedata/`, and this step is what decides whether
   step 4's list matters for your pair.

Steps 3 and 5 together give the real, complete set of affected locale
identifiers. What each step reads and why: [docs/method.md](docs/method.md).

## Results for the two RHEL pairs

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 — `sv_FI` only via `copy` |
| `or_IN` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference <sup>†</sup> | **step 2 warns**, but cannot settle it; both verdicts are empirical |
| `th_TH` | ⚪ Unaffected | 🟡 **Unresolved** | steps 1–3 |
| `ber_DZ`, `kab_DZ` | ⚪ Unaffected | 🟢 No difference | steps 1–3 flagged it; inspection found a role swap |
| CJK range U+4E00–U+9FA5 in `iso14651_t1`,<br>inherited by 328 locales | 🟢 No difference | 🟢 No difference | step 4 flagged it; step 5 says a diff can't clear it |
| `zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`,<br>`cns11643_stroke` | 🟢 No difference | 🟢 No difference | step 4 flagged them via `iso14651_t1_common`; cleared by measurement |
| everything else — `en_US`, `de_DE`,<br>`fr_FR`, … | ⚪ Unaffected | ⚪ Unaffected | `LC_COLLATE` and code both unchanged |

🔴 reindex · 🟡 flagged and *not* cleared, treat as changed until tested ·
🟢 flagged, but a targeted test or mechanism argument shows the order does not
move · ⚪ neither the locale's `LC_COLLATE` nor the collation code changed.
`ko_KR` is the row a data-only audit gets wrong, and `C.UTF-8` the row no
source diff can reach.

Footnotes, both worked examples and the nodes each claim was measured on:
[docs/results.md](docs/results.md). If you saved a result from this tool before
2026-09-05, check [CHANGELOG.md](CHANGELOG.md) first — two verdicts have moved
since.

## Confirming on a real system

A source diff is an argument, not a proof of what actually runs in production,
and it says nothing about your distro's backports.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

Run it on both the old and the new OS, for every locale steps 1 to 3 flagged
and — if step 5 found a substantive code change — for every locale step 4
flagged too. Besides the index inventory it reports text partition keys, which
no `REINDEX` fixes: the rows have to be moved.

Two traps that make such a comparison agree while proving nothing, and what
else the template checks:
[docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md).
It needs PostgreSQL 15 or newer and some langpack care on each node —
[docs/requirements.md](docs/requirements.md).

## Scope

Sort order (`LC_COLLATE`) only, and in PostgreSQL terms the **`libc` provider**
only. Nothing about `LC_CTYPE` (`upper()`, `lower()`, pattern matching), ICU,
or the `builtin` provider. The migration's *destination* must be glibc 2.24 or
newer — RHEL 8+, Ubuntu 18.04+, Debian 9+, SLES 15+; run it outside that range
and it answers confidently and wrongly.

Full scope, including the `builtin` provider as a mitigation:
[docs/scope.md](docs/scope.md). The four things this method structurally cannot
see, `C.UTF-8` among them: [docs/limitations.md](docs/limitations.md).

## Documentation

- [docs/method.md](docs/method.md) — the five steps in detail
- [docs/results.md](docs/results.md) — verdict table, worked examples, tested-on
- [docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md) — the empirical check
- [docs/scope.md](docs/scope.md) — what it audits, and what it does not
- [docs/limitations.md](docs/limitations.md) — the four things it cannot see
- [docs/requirements.md](docs/requirements.md) — dependencies, test suite, setup traps
- [docs/comparison-ardentperf.md](docs/comparison-ardentperf.md) — how this relates to [ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting)
- [CHANGELOG.md](CHANGELOG.md) — what this tool used to get wrong
