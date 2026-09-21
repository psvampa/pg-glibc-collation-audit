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

**It is not a single, infallible answer, and does not try to be.** The five
steps read upstream source, so three things sit outside them: the weights
`localedef` computes at build time, your distro's own patches to the locale
data, and a locale your distro adds, which is in no upstream tag at all. Three
optional checks read a real node's own files: two cover the second and third,
and the third says whether the node's own `C.UTF-8` is a locale the first
applies to. Nothing settles the first except measuring on the nodes.

Use it as one input among several, cross-checked against an empirical method
such as
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
and `glibc-2.34`. **Old first, new second**: a reversed pair is refused rather
than answered, because backwards every step still prints a plausible clean
result ([docs/method.md](docs/method.md#the-five-steps-in-detail)).

<details>
<summary><strong>Which pairs you may pass</strong> — the two audited pairs, skipping releases, and the old glibc 2.24 floor</summary>

**The audited pairs are RHEL8 → RHEL9 and RHEL9 → RHEL10** — the two upgrades
this project publishes measured results for ([docs/scope.md](docs/scope.md)).
That is what has been measured, not a restriction on the pair you may pass:
nothing in the tool looks at how far apart the two versions are, and the only
pair it refuses is a reversed one. In fact the two audited pairs already skip
releases — they are consecutive RHEL majors, not consecutive glibc releases,
and `2.28 -> 2.34` leaves out five upstream versions. **Skipping more is
fine**: `glibc-2.28` straight against `glibc-2.39` reports exactly what the
two steps between them report, name for name. Measured on that one triple, and
with the one case that needs two runs instead, in
[docs/method.md](docs/method.md#how-far-apart-the-two-tags-may-be).
Other distros work the same way. There used to be a hard floor at glibc 2.24,
below which the method answered confidently and wrongly; that was a bug and it
is fixed, though only one pair below it has been measured — see
[docs/limitations.md](docs/limitations.md#below-glibc-224-the-method-rests-on-one-measured-pair).

</details>

### The four commands

Four commands, in the order of what they cost you. The first needs nothing but
this checkout; the last needs PostgreSQL on both nodes. Each one says what it
measures and what it leaves to the next.

**1 — What changed between the two glibc versions**
*Needs this checkout. No node, no database.*

```sh
./audit.sh glibc-2.28 glibc-2.34
```

It runs the five steps in order, hands each step's result to the next so you
never retype a locale name, and ends with a consolidated summary.

Three things sit outside those five steps:

- **The weights `localedef` computes at build time.** Only a measurement on
  the nodes settles those — commands 3 and 4.
- **Your distro's own patches.** Command 2 reaches the ones that touch the
  files under `/usr/share/i18n/locales/`; `charmaps/` is not compared, and
  steps 6 to 8 say so. A backported change to the collation *code* is in
  neither tag and in neither command.
- **A locale your distro adds**, which is in no upstream tag at all.
  Command 2 is what sees it.

<details>
<summary><strong>Reading the output</strong> — the <code>!!</code> markers, and where the long lists are written</summary>

The run ends with an `AUDIT SUMMARY` block. One thing to know before you read
it: **`!!` marks a warning that the clean-looking result above it does not
cover something**, and the summary repeats every one of them, because a
warning that scrolled past 400 lines ago has not been delivered.

The rest of the output format — the `>>` code markers, where long result
lists are written, and why step 5 hands you C diffs instead of a verdict —
is in [docs/method.md](docs/method.md#reading-the-output).

Real output from both pairs, start to finish, is in
[`examples/`](examples/) — read that before running anything if you want to
know what you are getting.

</details>

**2 — What your distro patched, and what it added**
*Needs the locale sources off both nodes. No database.*

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

In a container, `docker exec el8` replaces `ssh el8`. `rpm -q glibc` prints
the architecture as well (`...x86_64`); either form is a usable build id, and
the build ids in this README drop it.

Check each copy against the node it came from: `ls el8-locales | wc -l`
against `ls /usr/share/i18n/locales/ | wc -l` run on the node. The run does
not do this for you: a copy missing a large share of its files is still not
refused, and at step 8 a file that never arrived reads as a locale the upgrade
removed or added.

**Give it both nodes' locale sources and it does more.** Each side you supply
adds a check that the node's own files match the tag the audit diffed — your
distro's patching, which no tag diff can see. Supply **both** and it also
compares the two nodes to each other, which is the only way to see whether a
locale your distro *adds* changed between them — `C.UTF-8` above all, since
its source file exists upstream only from glibc 2.35 and RHEL8 and RHEL9
predate that.

That settles whether the two nodes' collation *data* differs. What it cannot
settle is the resulting *order*, because the weights are computed when the
locale is built. That is command 3.

<details>
<summary><strong>Checking the copy, and the build ids</strong> — the counts each step prints, and what the summary says without the directories</summary>

Steps 9 and 10 print the copy's own count as `Files at <build id>` — that is
the number to set against the node's — and step 8 prints each copy's count,
byte total and fingerprint. The `Compared N file(s)` lines of steps 6 to 8 are
intersections — with the tag for steps 6 and 7, with the other node for step 8
— so they are never larger than `Files at`, and equality there does not mean
the copy is complete. A `Compared` line adds back up to `Files at` only
together with the `absent upstream` or `only on the ... node` line printed
beside it.

A copy that lands most of the files is not refused: the scripts refuse only a
directory too small to be a real copy at all. The files that never arrived are
mostly reported as ordinary findings — steps 6 and 7 list them under `Absent
on the node`, and step 8 under `Only on the old node` or `Only on the new
node`, where a failed transport reads as a locale the upgrade removed or
added. Two cases do earn a `!!`: a backported locale such as `C` missing from
one side (step 8), and a missing file that other locales `copy`, such as
`iso14651_t1` (steps 9 and 10).

The build ids are required: a result is bound to the build it was taken on, and
nothing in a directory of locale files carries a version. With neither
directory supplied the summary says so, in as many words, rather than leaving
the section out — the node-to-node comparison
([`scripts/diff_node_locales.py`](scripts/diff_node_locales.py)) and the
ellipsis scan of each node's own data (steps 9 and 10) print `NOT RUN`, and
`NOT RUN` must not read as a clean result.

Installing `glibc-locale-source` also **upgrades glibc**, because the two
packages are version-locked: read the build id after installing it, not
before. That trap and the langpack ordering are both in
[docs/requirements.md](docs/requirements.md); the traps that make two
directories agree while proving nothing are in
[docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md).

</details>

**3 — Whether `C.UTF-8`'s order changed**
*Needs PostgreSQL 15 or newer on both nodes.*

```sh
psql -X -f sql/c_utf8_probe.sql > el8.out      # on each node, then diff
diff el8.out el9.out
```

[`sql/c_utf8_probe.sql`](sql/c_utf8_probe.sql) takes no editing, and it is run
**even when the audit flagged nothing** — nothing in steps 1 to 5 can ever
flag this locale, whose source file is in neither tag of the RHEL8 → RHEL9
pair. It is usually the database collation in a container, and PostgreSQL
will not warn about it either.

<details>
<summary><strong>The positive control is inverted here</strong> — why agreeing with byte order is the fix, not the tell</summary>

Everywhere else in this project, a locale agreeing with `LC_ALL=C` byte order
means it was never generated and the comparison proves nothing. For `C.UTF-8`
that agreement is the **corrected** state: it is what RHEL9's build of glibc
2.34 produces from its backported file, and what `codepoint_collation`
guarantees upstream from 2.35 on. It is also what a build
whose above-BMP weights are all *tied* produces, because PostgreSQL breaks a
`strcoll` tie with `strcmp` — query 6b is what tells those two apart. Two
contradictory rules in one project get read in the wrong order.

What was measured, why it changed, and why it also changed *within* RHEL8:
[docs/limitations.md](docs/limitations.md#cutf-8-is-invisible-to-a-tag-diff).

</details>

**4 — Confirming the order on your own builds**
*Needs PostgreSQL 15 or newer on both nodes, and editing the file first.*

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

A source diff is an argument, not a proof of what actually runs in production.
Run
[`sql/collation_confirmation_template.sql`](sql/collation_confirmation_template.sql)
on both the old and the new OS, for every locale steps 1 to 3 flagged and — if
step 5 found a [substantive code change](docs/glossary.md) — for every locale
step 4 flagged too.

<details>
<summary><strong>What it reports, and the four traps</strong> — including the ones that make a comparison agree with itself</summary>

Two things about it fail in the reassuring direction: three traps on the SQL
side and a fourth on the file comparisons make a comparison agree with itself
while proving nothing
([docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md)),
and it needs PostgreSQL 15 or newer with langpacks installed in the right
order ([docs/requirements.md](docs/requirements.md)). It also reports more
than indexes — text partition keys among them, which no `REINDEX` fixes.

What those objects look like once they are already wrong is measured in
[breakage/](breakage/), on two real nodes, one case per object type.

</details>

## How it works

If neither the locale's rules nor the code that compiles them changed, its
sort order cannot have changed. That is a proof, not a sample.

Five steps, all run by `./audit.sh`: what changed (1), which of those changes
fall inside `LC_COLLATE` (2), which locales inherit them through `copy` (3),
which locales a data diff can never clear (4), and whether the code that
computes weights changed (5). Steps 3 and 5 together give the complete set of
affected locale identifiers.

Beyond those five, three optional checks read a real node's locale sources.
One compares a node against the upstream tag — the only way to see your
distro's backports. Another compares the two **nodes to each other**, which
is the only way to see whether a locale the distro adds — one upstream does
not have — changed between them: `C.UTF-8` is that locale, and it is usually
the database collation in a container. The third scans each node's own data
for ellipsis ranges — the
only way that question is asked of the node's `C` itself, since step 4 scans
the tag and no tag of the RHEL8 → RHEL9 pair holds that file.

The five steps in detail, the decision procedure they add up to, and how to
read what the run prints: [docs/method.md](docs/method.md).

## Results for the two RHEL pairs

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 — `sv_FI` only via `copy` |
| `or_IN` | 🔴 **Changed** | ⚪ Unaffected | steps 1–3 |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference | **step 2 warns** and cannot settle it <sup>†</sup> — the node-to-node check settles the data, `sql/c_utf8_probe.sql` the order |
| `th_TH` | ⚪ Unaffected | 🔴 **Changed** | steps 1–3 |
| `ber_DZ`, `kab_DZ` | ⚪ Unaffected | 🟢 No difference | steps 1–3 flagged it; inspection found a role swap |
| CJK range U+4E00–U+9FA5 in `iso14651_t1`,<br>inherited by 328 locales at 2.34, 338 at 2.39 | 🟢 No difference | 🟢 No difference | step 4 flagged it; step 5 says a diff can't clear it |
| `zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`,<br>`cns11643_stroke` | 🟢 No difference | 🟢 No difference | step 4 flagged them via `iso14651_t1_common`; cleared by measurement |
| everything else — `en_US`, `de_DE`,<br>`fr_FR`, … | ⚪ Unaffected | ⚪ Unaffected | `LC_COLLATE` and code both unchanged |

🔴 reindex · 🟡 flagged and *not* cleared, treat as changed until tested ·
🟢 flagged, but a targeted test or mechanism argument shows the order does not
move · ⚪ neither the locale's `LC_COLLATE` nor the collation code changed.
`ko_KR` is the row a data-only audit gets wrong, and `C.UTF-8` the row no
*tag* diff can reach.

<sup>†</sup> `C.UTF-8`'s source file is in neither tag for the first pair, so
steps 1–5 are blind to it and step 2 warns rather than settling it. It is
settled elsewhere: RHEL8 → RHEL9 **changed**, 40 of 41 probed code points in a
different position, because RHEL8 builds the locale from ellipsis ranges that
leave planes 3–13 undefined and RHEL9 backported upstream's
`codepoint_collation`; RHEL9 → RHEL10 **cannot** change, because both nodes'
copies of the file are byte-identical and byte order by construction. Both
measured 2026-09-06 on `glibc-2.28-251.el8_10.40`, `glibc-2.34-275.el9_8` and
`glibc-2.39-128.el10_2`, replacing ardentperf's checksum as the basis for the
second column. See
[docs/limitations.md](docs/limitations.md#cutf-8-is-invisible-to-a-tag-diff).

**A table keyed on two major upgrades cannot say this, so it goes here:**
`C.UTF-8`'s order also changed *within* RHEL8, in `glibc-2.28-93.el8`
(RHEL 8.2). Staying on one RHEL major is not a control for this locale.

The evidence behind each row, both worked examples and the nodes each claim
was measured on: [docs/results.md](docs/results.md). If you saved a result
from this tool on or before 2026-09-06, check [CHANGELOG.md](CHANGELOG.md) first —
three verdicts have moved, `th_TH` most recently, in the eleventh entry (dated
2026-09-06). Later that day `C.UTF-8` was measured directly: no verdict moved,
but the basis of its RHEL9→RHEL10 🟢 did.

## Scope

Sort order (`LC_COLLATE`) only, and in PostgreSQL terms the **`libc` provider**
only. Nothing about `LC_CTYPE` (`upper()`, `lower()`, pattern matching), ICU,
or the `builtin` provider — and the two pairs named above. `LC_CTYPE` is the
exclusion that can still cost you an index. No step of this tool reads it; one
measurement of what it does, on one pair of builds, is in
[breakage/cases/04-lc-ctype.md](breakage/cases/04-lc-ctype.md).

Full scope, including the `builtin` provider as a mitigation:
[docs/scope.md](docs/scope.md). The six things to know before acting on a clean
result — `C.UTF-8` among them, and now covered three other ways:
[docs/limitations.md](docs/limitations.md).

## Documentation

**[docs/](docs/README.md)** is the index, with a suggested reading order. The
short version:

- [docs/method.md](docs/method.md) — the five steps in detail, and the decision procedure
- [docs/results.md](docs/results.md) — the evidence behind each verdict, both worked examples, tested-on
- [docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md) — the empirical check
- [docs/limitations.md](docs/limitations.md) — the six things to know before acting on a clean result
- [docs/scope.md](docs/scope.md) — what it audits, and the `builtin` provider as a way out
- [docs/requirements.md](docs/requirements.md) — dependencies, test suite, setup traps
- [docs/glossary.md](docs/glossary.md) — `copy` graph, blast radius, hunk, tier, ellipsis range
- [docs/comparison-ardentperf.md](docs/comparison-ardentperf.md) — how this relates to [ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting)
- [breakage/](breakage/README.md) — what breaks inside PostgreSQL once a locale did change, measured on two nodes
- [examples/](examples/) — real output from both pairs
- [CHANGELOG.md](CHANGELOG.md) — what this tool used to get wrong

## Tests

```sh
python3 -m unittest discover -s tests -t tests   # about a minute and a half
```

Every test freezes a failure this tool actually shipped, and CI runs the
suite on a fresh glibc clone. [`tests/README.md`](tests/README.md) says what
it covers and, more usefully, what it does not.
