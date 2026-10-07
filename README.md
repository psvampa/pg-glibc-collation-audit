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
and across every locale in the tree: if neither a locale's rules nor the code
that compiles them changed, its order cannot have changed. Given one file from
each of your machines, it also measures how each machine's own glibc sorts the
locales installed on it, and names the characters that moved.

**This project does not set out to give a single, infallible answer.** It
gives you a few simple ways to compare the source of two glibc versions —
upstream, or the patched files a distro installs — to measure the resulting
order on your own nodes, and to try to identify which of your objects may be
affected.

Use it as a complement to what already exists, depending on what you need. One
example is an empirical method such as
[ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
which sorts real strings on real nodes, and there are others.

## How to use

### Prerequisites

**On the machine that runs `audit.sh`** — yours, or any other:

- `git`, `python3` (standard library only) and `bash`
- network the first time, to clone glibc (about 370 MB)
- `gpg`, optional, to check the signature of each glibc tag

**On each server, for the extended run of command 1:**

- `ssh` access, and `sudo` for one package install
- Python 3.6 or newer (on RHEL8, `/usr/libexec/platform-python`)
- `glibc-locale-source`, of the exact glibc build the server runs
- the language packs of the locales your databases use
- no PostgreSQL, and no root for the measurement

**For commands 2 and 3:**

- PostgreSQL 15 or newer on both servers
- for command 3, the language packs of the locales to test, installed before
  `initdb`

Why each one is needed, and the setup traps that give a wrong answer without
saying so, are in [docs/requirements.md](docs/requirements.md).

### Install

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

**1 — Which locales the upgrade can affect**

`audit.sh` compares the two glibc versions and lists the locales whose sort
order can change. It runs in two ways. 1.a needs only this checkout and reads
glibc's published source. 1.b adds one file from each of your machines, and
with it checks what your distro changed on its own and measures how each
machine's glibc actually sorts. Whenever you can run a command on both
machines, 1.b is the stronger answer.

**1.a — Quick verification.**
*Needs this checkout. No machine, no database.*

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

**1.b — Extended verification (recommended).**
*Needs a shell on both machines, and Python 3.6 or newer there. No database.
The script measures every locale installed on the machine and shows how far
it has got as it goes.*

What the six extra checks answer, what goes into each machine's file and what
the run says when it refuses one are in [docs/commands.md](docs/commands.md).
What this form prints is in
[examples/README.md, under Command 1 extended](examples/README.md#command-1-extended).

```sh
# 1. the locale sources of the exact glibc build each machine runs, an
#    official Red Hat package. Without the version, dnf installs the newest
#    sources and upgrades glibc itself when the machine is behind.
#    -t lets sudo ask for the password
ssh -t el8 'sudo dnf install -y glibc-locale-source-$(rpm -q --qf "%{VERSION}-%{RELEASE}" glibc)'
ssh -t el9 'sudo dnf install -y glibc-locale-source-$(rpm -q --qf "%{VERSION}-%{RELEASE}" glibc)'

# 2. measure each machine and pack the result into one file: the script
#    travels over the connection and the file comes back the same way.
#    On RHEL8 python3 may be missing; platform-python is always there
ssh el8 /usr/libexec/platform-python - --extract < scripts/locale_order.py > old.tar
ssh el9 python3 - --extract < scripts/locale_order.py > new.tar

# 3. compare, wherever this checkout is
./audit.sh glibc-2.28 glibc-2.34 --old-node old.tar --new-node new.tar
```

- `el8` and `el9` are placeholders for your two machines, the old one and the
  new one. Replace them, in every line above, with the names you reach those
  machines by over ssh, such as `user@host` or a name from `~/.ssh/config`.
- You can also copy `scripts/locale_order.py` to the machine, run
  `python3 locale_order.py --extract > old.tar` there and bring the file back.
- If the repository no longer has the sources of that exact build, dnf says
  "No match for argument" and changes nothing. A machine without the sources
  still gives a file, and the summary says which checks did not run and why.
- The files are named for their role, old and new, not for their version, so
  two builds of the same release cannot overwrite each other.

**2 — Verifying whether `C.UTF-8`'s order changed.**
*Only if a database uses `C.UTF-8` — in a container it usually does. Needs
PostgreSQL 15 or newer on both nodes.*

```sh
psql -X -f sql/c_utf8_probe.sql > el8.out      # on each node, then diff
diff el8.out el9.out
```

[`sql/c_utf8_probe.sql`](sql/c_utf8_probe.sql) takes no editing, and it is run
**even when the audit flagged nothing**. The run with the tags alone never
measures this locale's order; the extended run does, in step 11, on each
machine's own glibc. PostgreSQL will not warn about it either.

Reading its output takes one warning, because the usual tell is inverted for
this locale — agreeing with byte order is the *fix* here, not the sign that
nothing was generated. That, and what was measured, are in
[docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md#the-cutf-8-probe)
and
[docs/limitations.md](docs/limitations.md#cutf-8-is-invisible-to-a-tag-diff).
What it prints on both pairs is in
[examples/README.md, under Command 2](examples/README.md#command-2--the-cutf-8-probe).

**3 — Confirming the order on your own builds.**
*Optional. It sorts strings you choose inside PostgreSQL, and lists which of
your objects are at stake. Needs PostgreSQL 15 or newer on both nodes, and
editing the file first.*

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

A source diff is an argument, not a proof of what actually runs in
production. Step 11 measures, but one character at a time: a rule for a
combination of letters, such as a contraction, shows only in strings that
contain it. Run it on both the old and the new OS, for every locale the audit
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

A worked example for each pair is in
[examples/README.md, under Command 3](examples/README.md#command-3--the-worked-example-scripts).

## Results for the two RHEL pairs

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | 🟢 No difference | steps 1–3 — `sv_FI` only via `copy`; the second pair is cleared by step 11's measurement |
| `or_IN` | 🔴 **Changed** | 🟢 No difference | steps 1–3; the second pair is cleared by step 11's measurement |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs; the second pair is cleared by measurement |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference | **step 2 warns** and cannot settle it <sup>†</sup> — the node-to-node check settles the data, step 11 or `sql/c_utf8_probe.sql` the order |
| `th_TH` | 🟢 No difference | 🔴 **Changed** | steps 1–3; the first pair is cleared by step 11's measurement |
| `ber_DZ`, `kab_DZ` | 🟢 No difference | 🟢 No difference | steps 1–3 flagged it; inspection found a role swap; the first pair is cleared by step 11's measurement |
| CJK range U+4E00–U+9FA5 in `iso14651_t1`,<br>the base table a locale inherits unless it<br>defines its own order | 🟢 No difference | 🟢 No difference | step 4 flagged it; step 5 says a diff can't clear it |
| `zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`,<br>`cns11643_stroke` | 🟢 No difference | 🟢 No difference | step 4 flagged them via `iso14651_t1_common`; cleared by measurement |
| everything else — `en_US`, `de_DE`,<br>`fr_FR`, … | 🟢 No difference | 🟢 No difference | step 4 flags them and step 5 finds code changes in both pairs: step 11's measurement clears both, as far as it can measure |

🔴 reindex · 🟡 flagged and *not* cleared, treat as changed until tested ·
🟢 flagged, but a targeted test or mechanism argument shows the order does not
move.
`ko_KR` is the row a data-only audit gets wrong, and `C.UTF-8` the row no
*tag* diff can reach.

Step 11 measured the same on three test machines (Rocky Linux 8.9, 9.3 and
10.1): the locales marked 🔴 are the ones that sort differently, and every
other locale it could measure came out unchanged, one character at a time
([what that leaves out](docs/limitations.md#step-11-measures-one-character-at-a-time)).

<sup>†</sup> `C.UTF-8`'s source file is in neither tag for the first pair, so
both verdicts come from the machines themselves
([docs/results.md](docs/results.md#cutf-8--from-the-nodes-own-files-because-no-tag-has-them)).

`C.UTF-8`'s order also changed *within* RHEL8, in `glibc-2.28-93.el8`
(RHEL 8.2). Staying on one RHEL major is not a control for this locale. The
evidence is in
[docs/results.md](docs/results.md#cutf-8--from-the-nodes-own-files-because-no-tag-has-them).

## Scope

Sort order (`LC_COLLATE`) only, and in PostgreSQL terms the **`libc` provider**
only. Nothing about `LC_CTYPE` (`upper()`, `lower()`, pattern matching), ICU,
or the `builtin` provider — and the two pairs named above. `LC_CTYPE` is the
exclusion that can still cost you an index, and no step of this tool reads
it.

Full scope, including the `builtin` provider as a mitigation:
[docs/scope.md](docs/scope.md). The seven things to know before acting on a
clean result — `C.UTF-8` among them:
[docs/limitations.md](docs/limitations.md).

## Documentation

**[docs/](docs/README.md)** is the index, with a suggested reading order. The
short version:

- [docs/commands.md](docs/commands.md) — what each command does, and what it leaves to the next
- [docs/results.md](docs/results.md) — the evidence behind each verdict, both worked examples, tested-on
- [docs/confirming-on-a-real-system.md](docs/confirming-on-a-real-system.md) — the empirical check
- [docs/limitations.md](docs/limitations.md) — the seven things to know before acting on a clean result
- [docs/scope.md](docs/scope.md) — what it audits, and the `builtin` provider as a way out
- [docs/requirements.md](docs/requirements.md) — what each run needs, on your machine and on each server, and the three setup traps
- [docs/glossary.md](docs/glossary.md) — `copy` graph, blast radius, hunk, tier, ellipsis range, role swap, build id, measured order, level, contraction
- [examples/](examples/README.md) — real output of every command, and which file is which
- [tests/README.md](tests/README.md) — how to run the tests, what they cover, and what they do not
