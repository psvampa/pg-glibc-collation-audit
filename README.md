# pg-glibc-collation-audit

## Summary

Between two glibc versions — typically two RHEL major releases — **which
specific locales change their sort order**, and therefore which PostgreSQL
B-tree indexes on `text`/`varchar`/`char`/`citext` columns need a `REINDEX`
after an OS upgrade or a physical migration?

**PostgreSQL will not tell you.** `pg_collation.collversion` stores glibc's
version string, so it warns on *any* version bump whether or not your data
would sort differently, and stays silent when a distro patches collation data
without moving that string. It is a version comparison, not a check of the
real sort rules
([background](https://wiki.postgresql.org/wiki/Locale_data_changes)).

This tool answers the real question from glibc's own source, deterministically
and across all ~355 locales: if the rules that define a locale's sort order
did not change, the order cannot have changed.

**It is not a single, infallible answer, and does not try to be.** It reads
source, so it cannot see what a machine actually does — distro backports and
build-time-computed weights are outside what a diff can settle. Use it as one
input among several, cross-checked against an empirical method such as
[ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
which sorts ~25 million real strings on real nodes. Where the two overlap,
check both; where they disagree, the measurement wins. Confirm on your own
nodes before you act:
[docs/comparison-ardentperf.md](docs/comparison-ardentperf.md),
[docs/limitations.md](docs/limitations.md).

## How to use

### Install

Needs `git`, `python3` (stdlib only) and `bash`. Nothing else, and no
PostgreSQL for the audit itself.

```sh
git clone https://github.com/psvampa/pg-glibc-collation-audit.git
cd pg-glibc-collation-audit
```

Step 1 below clones glibc from `github.com/bminor/glibc`, so the first run
needs outbound network and about **370 MB** of disk. Later runs reuse that
clone.

### Pick the two glibc versions

Run `ldd --version` on the old and the new node. Those two numbers are the
tags you pass, as `glibc-<version>` — glibc 2.28 and 2.34 become `glibc-2.28`
and `glibc-2.34`.

**The version you are upgrading *to* must be glibc 2.24 or newer** — RHEL 8+,
Ubuntu 18.04+, Debian 9+, SLES 15+. Auditing *from* something older is fine,
so `RHEL 7 -> RHEL 8` is correct; auditing *towards* RHEL 7 or older is not,
and there is no guard for it — outside that range the tool answers
confidently and wrongly. See
[docs/limitations.md](docs/limitations.md#the-destination-must-be-glibc-224-or-newer).

### Run the five steps

The RHEL8-to-RHEL9 pair, end to end:

```sh
cd scripts
./audit-locale-diff.sh               glibc-2.28 glibc-2.34   # step 1
python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34   # step 2
python3 resolve_copy_closure.py      glibc-2.34 or_IN sv_SE  # step 3
python3 flag_algorithmic_ranges.py   glibc-2.34              # step 4
python3 diff_collation_code.py       glibc-2.28 glibc-2.34   # step 5
```

Step 3 takes the locale names step 2 printed, so substitute your own. Each
script finds the glibc clone on its own and generates whatever diff it needs
from the two tags, so there is no intermediate file to keep in sync and no
working directory to get wrong.

Real output from both pairs, start to finish, is in
[`examples/`](examples/) — read that before running anything if you want to
know what you are getting.

### Read the output

Each step ends with a `Next:` line naming the command that follows, so the
five runs chain. Long result lists are written to files under
`$PG_GLIBC_AUDIT_OUT` (default `/tmp/pg-glibc-collation-audit/`) and
referenced rather than inlined.

Two markers carry the weight:

- **`!!`** is a warning that the clean-looking result above it does not cover
  something. The `C.UTF-8` warning in step 2 is one of these, and it fires on
  both documented pairs.
- **`>>`** marks the actual code changes in step 5's hunks.

Steps 1 to 4 give you lists. **Step 5 gives you C diffs and does not decide
for you** — it cannot tell a weight-changing commit from a harmless one. If
nobody will read those hunks, treat every locale step 4 flagged as unresolved
and [confirm it on real nodes](docs/confirming-on-a-real-system.md) instead;
that path needs no source reading and is stronger evidence anyway.

### Confirm on a real system

A source diff is an argument, not a proof of what actually runs in production,
and it says nothing about your distro's backports.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

The template is
[`sql/collation_confirmation_template.sql`](sql/collation_confirmation_template.sql).

Run it on both the old and the new OS, for every locale steps 1 to 3 flagged
and — if step 5 found a [substantive code change](docs/glossary.md) — for every locale step 4
flagged too. Besides the index inventory it reports text partition keys, which
no `REINDEX` fixes: the rows have to be moved.

Three traps make such a comparison agree with itself while proving nothing —
chiefly a locale that is not generated on either box, which makes both fall
back to `C`. Those, and what else the template reports:
[docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md).
It needs PostgreSQL 15 or newer, and a langpack installed in the wrong order
will hand you a clean result that means nothing —
[docs/requirements.md](docs/requirements.md).

## How it works

If neither the locale's rules nor the code that compiles them changed, its
sort order cannot have changed. That is a proof, not a sample — the reasoning
is in [docs/method.md](docs/method.md).

1. **`audit-locale-diff.sh`** — clones glibc, prints the commit id and GPG
   signature state behind each tag, then gives an explicit
   `CHANGED`/`UNCHANGED` verdict for the collation templates, with each file's
   [blast radius](docs/glossary.md) over the [`copy` graph](docs/glossary.md).
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
   from an [ellipsis range](docs/glossary.md) rather than stored in the locale file.
5. **`diff_collation_code.py`** — diffs the glibc *code* that turns locale data
   into weights. The only sort-order-relevant change between glibc 2.28 and
   2.34 lives here, not in `localedata/`, and this step is what decides whether
   step 4's list matters for your pair.

Steps 3 and 5 together give the real, complete set of affected locale
identifiers.

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

<sup>†</sup> `C.UTF-8` is not auditable by this method at all — its source
file is in neither tag for the first pair. Step 2 warns about it rather than
settling it, and its RHEL9→RHEL10 verdict is ardentperf's checksum, not my
own test. See
[docs/limitations.md](docs/limitations.md#cutf-8-cannot-be-audited-by-this-method).

The evidence behind each row, both worked examples and the nodes each claim
was measured on: [docs/results.md](docs/results.md). If you saved a result
from this tool before 2026-09-05, check [CHANGELOG.md](CHANGELOG.md) first —
two verdicts have moved since.

## Scope

Sort order (`LC_COLLATE`) only, and in PostgreSQL terms the **`libc` provider**
only. Nothing about `LC_CTYPE` (`upper()`, `lower()`, pattern matching), ICU,
or the `builtin` provider — plus the glibc 2.24 floor above.

Full scope, including the `builtin` provider as a mitigation:
[docs/scope.md](docs/scope.md). The five things this method structurally cannot
see, `C.UTF-8` among them: [docs/limitations.md](docs/limitations.md).

## Documentation

**[docs/](docs/README.md)** is the index, with a suggested reading order. The
short version:

- [docs/method.md](docs/method.md) — the five steps in detail, and the decision procedure
- [docs/results.md](docs/results.md) — the evidence behind each verdict, both worked examples, tested-on
- [docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md) — the empirical check
- [docs/limitations.md](docs/limitations.md) — the five things it cannot see
- [docs/scope.md](docs/scope.md) — what it audits, and the `builtin` provider as a way out
- [docs/requirements.md](docs/requirements.md) — dependencies, test suite, setup traps
- [docs/glossary.md](docs/glossary.md) — `copy` graph, blast radius, hunk, tier, ellipsis range
- [docs/comparison-ardentperf.md](docs/comparison-ardentperf.md) — how this relates to [ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting)
- [examples/](examples/) — real output from both pairs
- [CHANGELOG.md](CHANGELOG.md) — what this tool used to get wrong

## Tests

```sh
python3 -m unittest discover -s tests -t tests   # about 17 seconds
```

Every test freezes a failure this tool actually shipped, and CI runs the
suite on a fresh glibc clone. [`tests/README.md`](tests/README.md) says what
it covers and, more usefully, what it does not.
