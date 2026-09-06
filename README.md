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
and across every locale in the tree — 355 at glibc 2.34, 366 at 2.39: if the
rules that define a locale's sort order did not change, the order cannot have
changed.

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

Needs `git`, `python3` (stdlib only) and `bash` — no PostgreSQL for the audit
itself, and no other dependency.

```sh
git clone https://github.com/psvampa/pg-glibc-collation-audit.git
cd pg-glibc-collation-audit
```

The first run also clones glibc, which needs network and disk:
[docs/requirements.md](docs/requirements.md).

### Pick the two glibc versions

Run `ldd --version` on the old and the new node. Those two numbers are the
tags you pass, as `glibc-<version>` — glibc 2.28 and 2.34 become `glibc-2.28`
and `glibc-2.34`.

**The audited pairs are RHEL8 → RHEL9 and RHEL9 → RHEL10** — the two adjacent
upgrades this project publishes results for ([docs/scope.md](docs/scope.md)).
Other distros work the same way above glibc 2.24; below it the method breaks
silently and nothing stops it, so read
[docs/limitations.md](docs/limitations.md#below-glibc-224-the-method-breaks-silently)
first.

### Run the audit

One command. Substitute your own two tags — these are the RHEL8-to-RHEL9
pair, as an example:

```sh
./audit.sh glibc-2.28 glibc-2.34
```

It runs the five steps in order, hands each step's result to the next so you
never retype a locale name, and ends with a consolidated summary.

Real output from both pairs, start to finish, is in
[`examples/`](examples/) — read that before running anything if you want to
know what you are getting.

<details>
<summary><strong>Reading the output, and confirming on a real system</strong> — what the run prints, and the empirical half of the method</summary>

### Read the output

The run ends with an `AUDIT SUMMARY` block. One thing to know before you read
it: **`!!` marks a warning that the clean-looking result above it does not
cover something**, and the summary repeats every one of them, because a
warning that scrolled past 400 lines ago has not been delivered.

The rest of the output format — the `>>` code markers, where long result
lists are written, and why step 5 hands you C diffs instead of a verdict —
is in [docs/method.md](docs/method.md#reading-the-output).

### Confirm on a real system

A source diff is an argument, not a proof of what actually runs in production,
and it says nothing about your distro's backports.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

The template is
[`sql/collation_confirmation_template.sql`](sql/collation_confirmation_template.sql).

Run it on both the old and the new OS, for every locale steps 1 to 3 flagged
and — if step 5 found a [substantive code change](docs/glossary.md) — for
every locale step 4 flagged too.

Two things about it fail in the reassuring direction: three traps make the
comparison agree with itself while proving nothing
([docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md)),
and it needs PostgreSQL 15 or newer with langpacks installed in the right
order ([docs/requirements.md](docs/requirements.md)). It also reports more
than indexes — text partition keys among them, which no `REINDEX` fixes.

</details>

## How it works

If neither the locale's rules nor the code that compiles them changed, its
sort order cannot have changed. That is a proof, not a sample.

Five steps, all run by `./audit.sh`: what changed (1), which of those changes
fall inside `LC_COLLATE` (2), which locales inherit them through `copy` (3),
which locales a data diff can never clear (4), and whether the code that
computes weights changed (5). Steps 3 and 5 together give the complete set of
affected locale identifiers.

Beyond those five, an optional check compares a real node's locale sources
against upstream — the only way to see your distro's backports.

The five steps in detail, the decision procedure they add up to, and how to
read what the run prints: [docs/method.md](docs/method.md).

## Results for the two RHEL pairs

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 — `sv_FI` only via `copy` |
| `or_IN` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference <sup>†</sup> | **step 2 warns**, but cannot settle it; both verdicts are empirical |
| `th_TH` | ⚪ Unaffected | 🔴 **Changed** | steps 1–3 |
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
from this tool before 2026-09-07, check [CHANGELOG.md](CHANGELOG.md) first —
three verdicts have moved since, `th_TH` as recently as 2026-09-06.

## Scope

Sort order (`LC_COLLATE`) only, and in PostgreSQL terms the **`libc` provider**
only. Nothing about `LC_CTYPE` (`upper()`, `lower()`, pattern matching), ICU,
or the `builtin` provider — and the two pairs named above.

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
python3 -m unittest discover -s tests -t tests   # about a minute
```

Every test freezes a failure this tool actually shipped, and CI runs the
suite on a fresh glibc clone. [`tests/README.md`](tests/README.md) says what
it covers and, more usefully, what it does not.
