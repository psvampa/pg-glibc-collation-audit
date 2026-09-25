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
and across every locale in the tree. If the rules that define a locale's sort
order did not change, the order cannot have changed.

**This project does not set out to give a single, infallible answer.** It
gives you a few simple ways to compare the source of two glibc versions —
upstream, or the patched files a distro installs — to measure the resulting
order on your own nodes, and to try to identify which of your objects may be
affected.

Use it as a complement to what already exists, depending on what you need. One
example is an empirical method such as
[ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
which sorts real strings on real nodes, and there are others. Check the known
limitations in [docs/limitations.md](docs/limitations.md) before you act.

## How to use

### Prerequisites and Install

The prerequisites are in [docs/requirements.md](docs/requirements.md).

```sh
git clone https://github.com/psvampa/pg-glibc-collation-audit.git
cd pg-glibc-collation-audit
```

### Pick the two glibc versions

Run `ldd --version` on the old and the new node. Those two numbers are the
tags you pass, written as `glibc-<version>`. **Old first, new second** — a
reversed pair is refused rather than answered, because backwards every step
still prints a plausible clean result.

Any two versions are allowed, however far apart, and nothing in the tool looks
at the distance between them. That is the answer for a direct upgrade. If you
will run production on the version in between for any length of time, audit
each step, because a change made on the way and undone before the end is in
neither end's files.

<details>
<summary><strong>Worth reading before you trust a result</strong> — which pairs are measured, and the old glibc 2.24 floor</summary>

- which pairs this project publishes measured results for —
  [docs/scope.md](docs/scope.md)
- why a pair below glibc 2.24 rests on a single measured pair —
  [docs/limitations.md](docs/limitations.md#below-glibc-224-the-method-rests-on-one-measured-pair)

</details>

### The commands

In the order of what they cost you. The first needs nothing but this checkout;
the last needs PostgreSQL on both nodes. Each one says what it measures and
what it leaves to the next.

**1 — Verifying which locales the upgrade can affect**
*Needs this checkout. No node, no database.*

```sh
./audit.sh glibc-2.28 glibc-2.34
```

It runs the five steps in order, passes the locales step 2 found on to step 3
so you never retype a name, and ends with a consolidated summary. What each
step asks, and what the run leaves unsettled, is in
[docs/commands.md](docs/commands.md). What this exact command prints, on
both audited pairs, is in
[examples/README.md, under Command 1](examples/README.md#command-1) — worth
reading before you run anything.

> [!TIP]
> If you can get the locale files off both machines, run the longer form below
> instead — it adds five checks the tags alone cannot make.

<details>
<summary><strong>Alternatively — the same run, with each machine's own locale files</strong></summary>

*Needs the locale sources off both nodes. No database.*

What the five extra checks answer, how to take the copy and how to tell a good
one from a short one are in [docs/commands.md](docs/commands.md). What this
form prints is in
[examples/README.md, under Command 1 extended](examples/README.md#command-1-extended).

```sh
# on each node
dnf install -y glibc-locale-source

# copy the sources off both nodes -- tar, NOT `docker cp`, whose target /tmp is
# a separate mount in a container, so the copy silently does nothing
mkdir -p el8-locales el9-locales
ssh el8 tar -cf - -C /usr/share/i18n/locales . | tar -xf - -C el8-locales
ssh el9 tar -cf - -C /usr/share/i18n/locales . | tar -xf - -C el9-locales

# the build ids, read on the nodes themselves
ssh el8 rpm -q glibc
ssh el9 rpm -q glibc

./audit.sh glibc-2.28 glibc-2.34 \
  --old-locales-dir ./el8-locales --old-build-id glibc-2.28-251.el8_10.40 \
  --new-locales-dir ./el9-locales --new-build-id glibc-2.34-275.el9_8
```

</details>

**2 — Verifying whether `C.UTF-8`'s order changed**
*Only if a database uses `C.UTF-8` — in a container it usually does. Needs
PostgreSQL 15 or newer on both nodes.*

```sh
psql -X -f sql/c_utf8_probe.sql > el8.out      # on each node, then diff
diff el8.out el9.out
```

[`sql/c_utf8_probe.sql`](sql/c_utf8_probe.sql) takes no editing, and it is run
**even when the audit flagged nothing**. No step of the audit measures this
locale's order, and PostgreSQL will not warn about it either.

Reading its output takes one warning, because the usual tell is inverted for
this locale — agreeing with byte order is the *fix* here, not the sign that
nothing was generated. That, and what was measured, are in
[docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md#the-cutf-8-probe)
and
[docs/limitations.md](docs/limitations.md#cutf-8-is-invisible-to-a-tag-diff).
What it prints on both pairs is in
[examples/README.md, under Command 2](examples/README.md#command-2--the-cutf-8-probe).

**3 — Confirming the order on your own builds**
*Optional, and what turns a source argument into a measurement. Needs
PostgreSQL 15 or newer on both nodes, and editing the file first.*

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

A source diff is an argument, not a proof of what actually runs in
production. Run it on both the old and the new OS, for every locale the audit
flagged.

What this box leaves out is in
[docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md):

- which locales exactly to run it for
- which values to test with, the step that decides whether the run proves
  anything at all
- the three traps that make a comparison agree with itself while proving
  nothing
- what it reports beyond indexes, text partition keys among them, which no
  `REINDEX` fixes

What it prints, filled in for each pair, is in
[examples/README.md, under Command 3](examples/README.md#command-3--the-confirmation-template-filled-in).

## Results for the two RHEL pairs

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 — `sv_FI` only via `copy` |
| `or_IN` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference | **step 2 warns** and cannot settle it <sup>†</sup> — the node-to-node check settles the data, `sql/c_utf8_probe.sql` the order |
| `th_TH` | ⚪ Unaffected | 🔴 **Changed** | steps 1–3 |
| `ber_DZ`, `kab_DZ` | ⚪ Unaffected | 🟢 No difference | steps 1–3 flagged it; inspection found a role swap |
| CJK range U+4E00–U+9FA5 in `iso14651_t1`,<br>the base table a locale inherits unless it<br>defines its own order | 🟢 No difference | 🟢 No difference | step 4 flagged it; step 5 says a diff can't clear it |
| `zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`,<br>`cns11643_stroke` | 🟢 No difference | 🟢 No difference | step 4 flagged them via `iso14651_t1_common`; cleared by measurement |
| everything else — `en_US`, `de_DE`,<br>`fr_FR`, … | ⚪ Unaffected | ⚪ Unaffected | `LC_COLLATE` and code both unchanged |

🔴 reindex · 🟡 flagged and *not* cleared, treat as changed until tested ·
🟢 flagged, but a targeted test or mechanism argument shows the order does not
move · ⚪ neither the locale's `LC_COLLATE` nor the collation code changed.
`ko_KR` is the row a data-only audit gets wrong, and `C.UTF-8` the row no
*tag* diff can reach.

<sup>†</sup> `C.UTF-8`'s source file is in neither tag for the first pair, so
steps 1–5 cannot settle it. Both verdicts come from the nodes themselves. Why
each came out as it did, and the builds it was measured on, are in
[docs/results.md](docs/results.md), for
[RHEL8 → RHEL9](docs/results.md#cutf-8--from-the-nodes-own-files-because-no-tag-has-them)
and for
[RHEL9 → RHEL10](docs/results.md#worked-example-rhel9-to-rhel10-glibc-234-to-239).

**A table keyed on two major upgrades cannot say this, so it goes here:**
`C.UTF-8`'s order also changed *within* RHEL8, in `glibc-2.28-93.el8`
(RHEL 8.2). Staying on one RHEL major is not a control for this locale. The
evidence is in
[docs/results.md](docs/results.md#cutf-8--from-the-nodes-own-files-because-no-tag-has-them).

The evidence behind each row, both worked examples and the nodes each claim
was measured on: [docs/results.md](docs/results.md). If you saved a result
from an earlier version of this tool, run the current version again rather
than reuse it. Earlier versions printed clean results over checks they had not
made. [Three published verdicts have
moved](docs/results.md#if-you-saved-an-earlier-result), `th_TH` most recently.

## Scope

Sort order (`LC_COLLATE`) only, and in PostgreSQL terms the **`libc` provider**
only. Nothing about `LC_CTYPE` (`upper()`, `lower()`, pattern matching), ICU,
or the `builtin` provider — and the two pairs named above. `LC_CTYPE` is the
exclusion that can still cost you an index, and no step of this tool reads
it.

Full scope, including the `builtin` provider as a mitigation:
[docs/scope.md](docs/scope.md). The six things to know before acting on a clean
result — `C.UTF-8` among them:
[docs/limitations.md](docs/limitations.md).

## Documentation

**[docs/](docs/README.md)** is the index, with a suggested reading order. The
short version:

- [docs/commands.md](docs/commands.md) — what each command does, and what it leaves to the next
- [docs/results.md](docs/results.md) — the evidence behind each verdict, both worked examples, tested-on
- [docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md) — the empirical check
- [docs/limitations.md](docs/limitations.md) — the six things to know before acting on a clean result
- [docs/scope.md](docs/scope.md) — what it audits, and the `builtin` provider as a way out
- [docs/requirements.md](docs/requirements.md) — dependencies, test suite, setup traps
- [docs/glossary.md](docs/glossary.md) — `copy` graph, blast radius, hunk, tier, ellipsis range
- [examples/](examples/README.md) — real output of every command, and which file is which

## Tests

```sh
python3 tests/run_parallel.py                    # one process per class
python3 -m unittest discover -s tests -t tests   # the same tests, one process
```

Every test freezes a failure this tool shipped, or a way of losing one, and
CI runs the serial command on a fresh glibc clone.
[`tests/README.md`](tests/README.md) says what it covers and, more usefully,
what it does not.
