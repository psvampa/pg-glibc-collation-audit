---
name: collation-testing
description: How work and testing are done in pg-glibc-collation-audit. Carries the rules Pablo set: never modify anything in plan mode; anydbver on dell3 is the ONLY way to deploy or destroy an environment; if that is not sufficient, ask for an alternative instead of improvising; ephemeral environments get destroyed after use, but the three persistent fixtures collaudit8/collaudit9/collaudit10 (RHEL 8/9/10, PostgreSQL 18, all langpacks) are preserved and re-deployed if missing; and whatever a session discovers to be wrong gets written back into this file so it is not rediscovered later. Load it also when a correction lands, a trap bites, or a documented claim turns out stale, so the lesson is recorded rather than repeated. Load BEFORE any task that would test, measure, verify, confirm or reproduce a collation result on real nodes, deploy or connect to a test host, or move a verdict in docs/results.md — and before running anything at all while plan mode is active. Also carries the measured traps that make a comparison agree while proving nothing. NOT needed for editing documentation, running the local unittest suite, or source-only audit work that touches no remote host.
---

# Testing and working rules for pg-glibc-collation-audit

Rules set by Pablo on 2026-09-06, after I broke three of them. They are not
style preferences; each one exists because breaking it produced a wrong or
unusable result. Rule 5 is why this list can grow without being told to.

## 1. Never modify anything in plan mode

Plan mode is read-only. No exception, no reason, no "just to plan faster".

- **Allowed:** reading files, `grep`/`find`/`ls`, `--help`, `docker ps`,
  `docker inspect`, `git log`/`show`, `anydbver list`, `anydbver namespace list`.
- **Forbidden, local and remote alike:** `docker run` — `--rm` does *not* make
  it read-only, it still starts a container — `docker exec` that changes state,
  `anydbver deploy`, package installs, writing any file, any `git` write.

**An order that needs execution is not a conflict.** If Pablo asks for
something requiring execution while plan mode is on, put it in the plan and
call ExitPlanMode. That is the default behaviour in every project; plan mode
changes nothing about it. Do not present his instructions as being in tension
with each other, and do not narrate resolving them. He decides; ask permission
and move on.

*What went wrong:* told I had already exceeded my limits, I ran
`docker run --rm dbcanvas-systemd:oraclelinux-{8,9,10}` on his host to check
glibc versions — three containers started, purely to plan faster. "NO. FRENA!"

## 2. anydbver on dell3 is the only way to deploy or destroy anything

**This is the fundamental rule for all work and testing.** Whenever an
environment has to be created or torn down, it is anydbver, on `dell3`. No
exceptions, no other tool, no other host.

- Binary on dell3: `/home/pablo.svampa/.local/bin/anydbver`.
- **Source, read it when unsure:**
  `/Users/pablosvampa/ClaudeProjects/src/anydbver` — locally checked out.
  `instructions.md` there is the reference; it is where the `os:` syntax below
  is documented.
- `dell3` is reachable from `~/.ssh/config` through jump host `highram02`.
- Containers are named `<namespace>-<user>-node0`, e.g.
  `collaudit9-pablo-svampa-node0`.

Syntax that matters here:

```sh
anydbver --namespace=<ns> deploy os:el9 pg:18   # OS + software, one node
anydbver --namespace=<ns> destroy               # tear that namespace down
anydbver --namespace=<ns> exec node0 -- <cmd>   # run a command in the node
anydbver namespace list                         # what exists
```

OS keywords for this project: `el8`, `el9`, `el10` (RHEL/Rocky family). Others
exist (`jammy`, `focal`, `noble`, `bookworm`, `sles15`) and are not used here.

**Never improvise infrastructure.** Do not pick images, hosts or containers
because they happen to be present on the host. Running `docker images` and
choosing what looks suitable is exactly the mistake rule 3 exists to prevent.

**Never touch an environment you did not create.** No `exec`, no writes, no
reuse, however convenient. `collaudit8` and `collaudit9` are Pablo's
namespaces — leave them alone and deploy your own.

*What went wrong:* I built a measurement on `dbcanvas-systemd` images I found
via `docker images`, nobody having suggested them, and ran `docker exec` inside
his 34-hour-old `collaudit9` container, leaving a file in it. "Porque estás
usando dbcanvas? yo jamás te autoricé o sugerí ir por ahí."

## 3. If anydbver on dell3 is not sufficient, ask

Not knowing how to do something with anydbver is a question for Pablo, never
licence to find another way.

**Read the source before asking.**
`/Users/pablosvampa/ClaudeProjects/src/anydbver/instructions.md` answers most
questions — the `os:` keyword was there all along, under "Choosing the OS".
Checking a local checkout is not improvising; picking an image off the host is.

Ask when the source does not answer it. This rule covers: a glibc version anydbver cannot deploy, a test needing
a real VM rather than a container, or anything needing software the tool does
not install. State what is missing and ask for the alternative.

## 4. Destroy what you created — except the three fixtures

For any **ad-hoc** environment: `anydbver --namespace=<ns> destroy` after the
measurement, **including when the test fails or is inconclusive.**

Full lifecycle for ad-hoc work:

1. Connect to `dell3`.
2. Verify what already exists — `anydbver namespace list`.
3. Deploy what is missing, under your own namespace.
4. Test.
5. Destroy the scenario.

**The exception is the three persistent fixtures below.** They are deliberately
kept between sessions, so do not tear them down after a test.

## 5. When something turns out wrong, learn it here

**A correction that only lives in a session transcript will be repeated.** When
this session discovers that something was wrong — a mistake I made, a trap that
bit, a claim in the docs that no longer holds, a rule Pablo had to state twice —
understand *why* it happened and write it into this file before the session
ends. Do not wait to be asked.

Understand before writing. The useful entry is the mechanism, not the symptom:
"`docker cp` into these containers silently does nothing because their `/tmp` is
a separate mount, so `sort` returns the checksum of empty input and two empty
sides agree perfectly" prevents a recurrence. "Be careful with docker cp" does
not.

How to write an entry:

- **Attach the concrete case**, so the rule reads as a consequence rather than a
  precept. Every rule above carries the incident that produced it; that is
  deliberate and worth keeping.
- **Edit the existing rule instead of appending a near-duplicate.** Two copies
  of a rule drift, and this repository has already been bitten by exactly that:
  a `~16s` / `~17s` disagreement about the test suite's runtime, and a "four
  things" count against a five-item list.
- **Correct and delete what turns out to be false.** Self-learning is not
  append-only. Two entries were stale and had to be fixed on 2026-09-06: a note
  claiming false negatives #3–#6 were still open when the CHANGELOG's ninth
  entry had closed all six, and this file's own claim that anydbver's OS
  selection was undocumented when `instructions.md` had it under "Choosing the
  OS" the whole time. A confidently stale note is worse than no note, because it
  gets acted on.
- **Keep it short.** A skill nobody can scan stops being read. Record the rule
  and the mechanism; the transcript keeps the narrative.

**Where it goes.** Procedures, rules and traps belong here, in the skill —
that is what gets loaded when the work matches. Facts that are not procedure go
to project memory instead: who Pablo is, how he wants to be worked with, where
a verdict currently stands, what is still open. If an entry would be acted on
rather than recalled, it belongs in this file.

*What went wrong:* several things in this session were documented in
`docs/requirements.md` and `docs/confirming-on-a-real-system.md` and still
caught me — the `%_install_langs` trap and the postmaster restart among them —
because knowing a fact is written down somewhere is not the same as having it
in front of you when you provision a node. Both are now in the provisioning
recipe below, with the commands, not as prose to remember.

## The three persistent test environments

Set up by Pablo's instruction on 2026-09-06. **Keep these; do not destroy them
after a test.** If one is missing when needed, deploy it again with the exact
command below.

| Namespace | OS keyword | Deployed as | glibc | PostgreSQL |
|---|---|---|---|---|
| `collaudit8` | `os:el8` | Rocky Linux 8.9 | `glibc-2.28-236.el8_9.7` | 18.6 |
| `collaudit9` | `os:el9` | Rocky Linux 9.3 | `glibc-2.34-83.el9.7` | 18.6 |
| `collaudit10` | `os:el10` | Rocky Linux 10.1 | `glibc-2.39-58.el10_1.2` | 18.6 |

All three come from the same vendor and image lineage, which is the control
that matters: the only intended variable between two nodes is glibc's version.
Note the distro package builds differ from the ones `docs/results.md` cites
(`glibc-2.28-251.el8_10.40`, `glibc-2.34-275.el9_8`, measured on earlier
nodes) — the upstream 2.28/2.34/2.39 lineage is the same, but say which build
a measurement was taken on rather than assuming they match.

```sh
anydbver --namespace=collaudit8  deploy os:el8  pg:18
anydbver --namespace=collaudit9  deploy os:el9  pg:18
anydbver --namespace=collaudit10 deploy os:el10 pg:18
```

Each carries **PostgreSQL 18** and **all glibc langpacks**, so any locale in
the audit can be tested without re-provisioning. `collaudit9` serves both
documented pairs: it is the new side of RHEL8→RHEL9 and the old side of
RHEL9→RHEL10.

Langpacks and collations are **not** part of the deploy spec. After deploying,
run this on each node — every step is load-bearing:

```sh
# 1. the trap: all three images ship %_install_langs en_US, so without this
#    dnf reports success and installs English only
rm -f /etc/rpm/macros.image-language-conf
dnf install -y glibc-all-langpacks          # -> 867 / 869 / 885 locales

# 2. restart BEFORE importing, or the import reports success while seeing
#    only the locales that existed when the postmaster started
systemctl restart postgresql-18
psql -c "SELECT pg_import_system_collations('pg_catalog')"   # 3 -> ~1006-1024
```

Verified on 2026-09-06: all ten locales the audit touches — `sv_SE.utf8`,
`sv_FI.utf8`, `or_IN`, `th_TH.utf8`, `ber_DZ.utf8`, `kab_DZ.utf8`,
`ko_KR.utf8` plus the `en_US`/`de_DE`/`fr_FR` controls — exist as `libc`
collations on all three nodes.

## Making a measurement that is worth citing

This repository exists to catch reassuring false negatives, so the same
standard applies to its own measurements.

**A measurement from a dirty or unauthorised environment is void.** Say so and
re-run it clean. Never cite it, and never let it move a published verdict.

**Both sides must differ only in glibc.** Same image lineage, same vendor, both
freshly deployed. One dirty side and one side from another vendor's image is
not a control.

Four traps, each one measured rather than theorised:

- **`docker cp` into these containers can silently do nothing** — their `/tmp`
  is a separate mount, so the file never lands. `sort` then returns the
  checksum of empty input, and *two empty sides agree perfectly*. Pipe input
  with `docker exec -i` instead, and **assert the line count on both sides**
  before comparing anything.
- **Minimal images install no langpacks.** They ship
  `/etc/rpm/macros.image-language-conf` containing `%_install_langs en_US`, so
  `dnf install glibc-langpack-*` reports success and installs only English.
  Remove that file first, then verify with `locale -a`.
- **Feed byte-identical input to both sides.** `strcoll` reports distinct
  strings as equal, and `sort(1)` breaks those ties by *input order*, so
  differently-ordered input differs for reasons unrelated to glibc.
- **Keep a positive control.** Confirm the locale's order differs from `LC_ALL=C`
  byte order; otherwise an ungenerated locale silently fell back to `C` and the
  two sides will agree while proving nothing.

When a difference does appear, confirm it with a direct `locale.strcoll`
comparison on specific string pairs, not only a `sort` checksum — that rules
out the tie-break artifact above.

**Derive the corpus from the rule that changed**, and prefer exhaustive over
sampled. For `th_TH` over 2.34..2.39 the 220 deleted `collating-element` lines
are exactly 5 Thai leading vowels (U+0E40..U+0E44) × 44 consonants, so the
complete corpus over the changed rule is a few hundred strings — not a sample.
A narrow sample is what left that verdict unresolved in the first place.

## Related

- `docs/confirming-on-a-real-system.md` — the three traps, and what the SQL
  template checks.
- `docs/requirements.md` — the langpack ordering and postmaster-restart traps.
- `sql/collation_confirmation_template.sql` — the PostgreSQL-level confirmation.
- `docs/results.md` — the verdicts any measurement here would move.
