# Known limitations

Five things this method structurally cannot see. The first is invisible to the
tag diff and covered three other ways, all of them now measured; the second can
change your answer; the third is a hard kill condition; the fourth is a gap in
coverage with no demonstrated impact; the fifth is the manual step in an
otherwise mechanical method.

1. [`C.UTF-8` is invisible to a tag diff](#cutf-8-is-invisible-to-a-tag-diff)
2. [Upstream tags are not your distro's glibc](#upstream-tags-are-not-your-distros-glibc)
3. [Below glibc 2.24 the method breaks silently](#below-glibc-224-the-method-breaks-silently)
4. [Character repertoire changes are not audited](#character-repertoire-changes-are-not-audited)
5. [Step 5 reports, it does not decide](#step-5-reports-it-does-not-decide)

## `C.UTF-8` is invisible to a tag diff

Its source file, `localedata/locales/C`, only exists upstream from glibc
**2.35**. RHEL8 and RHEL9 predate that and **backport** the file; RHEL10 is
glibc 2.39 and simply has upstream's. So the file is on all three nodes, it is
in neither tag of the RHEL8→RHEL9 pair, and `C.UTF-8`'s order **does** change
between those two. Comparing upstream 2.28 and 2.34 cannot see a
file that is in neither, and no choice of tags fixes that.

What closes it is that the file is absent from both tags and **present on both
nodes**. Three checks reach it there — two read the file, the third measures the
order it produces — and this section says what each one does and does not prove.
None of them makes steps 1-5 able to see it.

### What was measured

Confirmed on real nodes, sorting U+10FFFF, U+FFFF, U+07FF and U+007F under
`C.utf8`:

```
glibc 2.28:  FFFF, 10FFFF, 007F, 07FF     <- not codepoint order
glibc 2.34:  007F, 07FF, FFFF, 10FFFF     <- correct
```

Re-measured 2026-09-06 on `glibc-2.28-251.el8_10.40` and
`glibc-2.34-275.el9_8`, both PostgreSQL 18.6, and confirmed by direct
`strcoll` as well as `sort`: U+007F sorts *after* U+FFFF at 2.28 and *before*
it at 2.34.

Widened the same day by [`sql/c_utf8_probe.sql`](../sql/c_utf8_probe.sql),
whose corpus is derived from the RHEL8 file itself: the first and last code
point of every range it declares, the first and last of every plane it declares
**no** range for, and the UTF-8 length boundaries. 41 values, asserted as 41
before anything is compared:

| Node | `C.utf8` equals byte order? | Positions differing |
|---|---|---|
| `glibc-2.28-251.el8_10.40` | **no** | **40 of 41** |
| `glibc-2.34-275.el9_8` | yes | 0 |
| `glibc-2.39-128.el10_2` | yes | 0 |

On RHEL8 ASCII sorts at position 31 — behind the last code point of every
declared range and behind all 22 values from the planes no range declares. Four
values still sort after it, and this project does not explain why: the *first*
code point of four declared ranges. The RHEL9 and RHEL10 outputs are
byte-identical to each other.
Full output in
[`examples/c-utf8-probe-rhel8-vs-rhel9.txt`](../examples/c-utf8-probe-rhel8-vs-rhel9.txt)
and
[`examples/c-utf8-probe-rhel9-vs-rhel10.txt`](../examples/c-utf8-probe-rhel9-vs-rhel10.txt).

This does not affect `COLLATE "C"`, which is byte order and immutable, but it
does affect indexes built on `C.UTF-8`.

### Why it changed, from the source this time

Measured the same day by copying `/usr/share/i18n/locales/C` off all three
nodes — the file no tag has:

| Node | its `LC_COLLATE` |
|---|---|
| `glibc-2.28-251.el8_10.40` | six **ellipsis ranges**: `<U0000>..<UFFFF>`, planes 1, 2, 14, 15, 16, then `UNDEFINED` |
| `glibc-2.34-275.el9_8` | the single keyword **`codepoint_collation`** |
| `glibc-2.39-128.el10_2` | byte-identical to RHEL9's |

That is the whole explanation, and it is mechanical rather than anecdotal:

* An ellipsis range does not carry weights. `localedef` computes them at build
  time, which is why [step 4](method.md) flags every locale that uses one and
  why a data diff can never clear it. glibc 2.34 took commit
  [`82292c99b2`](https://sourceware.org/bugzilla/show_bug.cgi?id=22668)
  ("LC_COLLATE: Fix last character ellipsis handling", Bug 22668) — the same
  commit that reorders `ko_KR`.
* Planes **3 through 13 have no range at all** in the RHEL8 file, so every code
  point in them falls to `UNDEFINED`. That is the defect
  [Red Hat bug 1361965](https://bugzilla.redhat.com/show_bug.cgi?id=1361965)
  describes, and it is why the RHEL8 order is not merely shifted but scrambled.
* `codepoint_collation` is glibc's own keyword for "discard all collation
  information and use `strcmp`". RHEL9 backported it. From there on `C.UTF-8`
  is byte order **by construction** and no expansion change can move it —
  which is why the RHEL9→RHEL10 result above is not luck.

Note what the [positive control](glossary.md) looks like here, because it
inverts: at 2.34 `C.utf8` produces *exactly* byte order, so agreeing with
`LC_ALL=C` is the corrected behaviour rather than the usual sign that a locale
was never generated. At 2.28 it is the disagreement that shows the bug.

There is a third reading of that agreement, and PostgreSQL cannot rule it out:
`varstr_cmp` and the sortsupport comparator both break a `strcoll` tie with
`strcmp`, so a build whose above-BMP weights are all *tied* is
indistinguishable through SQL from one with correct byte order. The probe
settles it outside PostgreSQL, with `strxfrm` sort keys, which cannot hide a
tie. Measured: RHEL9's and RHEL10's keys **are** the UTF-8 bytes, so the
agreement is real; RHEL8's are computed weights bearing no relation to the code
point.

### It also changed *inside* one RHEL major

`glibc-2.28-93.el8` (RHEL 8.2,
[RHSA-2020:1828](https://access.redhat.com/errata/RHSA-2020:1828), Red Hat bug
1361965) rewrote those ellipsis expressions; code points above U+10000 gained
weights and the compiled locale grew by 5.3 MiB. The node says so itself:

```
$ rpm -q --changelog glibc | grep -i collat
- Fix C.UTF-8 locale source ellipsis expressions (#1361965)
...
```

So an index built on `C.UTF-8` under RHEL 8.1 and used under RHEL 8.2 is in the
same position as one carried across a major upgrade. **"Same RHEL major" is not
a control.**

That is entirely outside the source method: both sides of such an upgrade are
upstream glibc 2.28, so the tag pair is `glibc-2.28..glibc-2.28` and steps 1-5
have nothing to compare. `audit.sh` now says so when both tags match, because
otherwise every step reports a clean everything. The node-to-node comparison
and the probe are the whole answer there.

Run that changelog grep on both nodes — and know what it is worth. It found the
line above on RHEL8, and found **nothing** on RHEL9 and RHEL10, whose
changelogs carry 158 and 112 entries and mention collation in none of them. It
is a cheap signal, not a check.

### Why this is the configuration to watch

It is the default almost everywhere `initdb` runs in a container: a `libc`
provider with `C.UTF-8` as the database collation means every text column
without an explicit `COLLATE` is exposed to a glibc upgrade, while the
source-diff audit is structurally blind to that locale.

The mitigation, if this is what you are worried about, is the `builtin`
provider — see [scope.md](scope.md).

### PostgreSQL is blind to it too

Worth knowing before you rely on a `collversion` mismatch as your warning.
Under the `libc` provider, `get_collation_actual_version()` returns NULL for
`C`, for `POSIX` and for **anything whose name starts with `C.`** — the
`pg_strncasecmp("C.", ...)` test, present in every branch from PG 14 on
(`src/backend/utils/adt/pg_locale.c` through PG 17, `pg_locale_libc.c` from
PG 18).

So `collversion` and `datcollversion` stay NULL for `C.UTF-8`, the mismatch
check in the SQL template can never fire for it, and neither can PostgreSQL's
own warning on a version bump. Nothing covers the source half and nothing
covers the catalog half, so for `C.UTF-8` the empirical comparison is not
optional.

### What the tool does about it

Steps 1-5 still cannot see the file, and step 2 still says so: it emits a named
warning whenever `localedata/locales/C` is absent at the old tag — which is
every pair where this bites — saying that the locale very likely exists on your
old system, that its order can change, that PostgreSQL will not warn, and that
the clean result in [results.md](results.md) says nothing about it.

Three checks do read it, none of them a step of the method, all of them needing
something the clone does not have:

| Check | What it proves | What it does not |
|---|---|---|
| [`scripts/diff_node_locales.py`](../scripts/diff_node_locales.py) — compares the two **nodes'** locale sources to each other, no tag in the middle | whether the two nodes carry the same collation **data**, and which findings exist at neither tag | the **order**. An ellipsis range's weights are computed by `localedef`; Bug 22668 reordered `ko_KR` from a byte-identical file |
| `flag_algorithmic_ranges.py --locales-dir` — step 4 over a node's own directory | that the node's `C` is ellipsis-based (RHEL8) or byte-order-by-construction (RHEL9, RHEL10). With step 5's Bug 22668 hunk this *derives* the measured inversion instead of only observing it | anything about a build whose `localedef` differs from both tags' |
| [`sql/c_utf8_probe.sql`](../sql/c_utf8_probe.sql) — run unedited on both nodes, then `diff` | the order, on the builds actually installed, with the inverted control made mechanical and the tie case ruled out | nothing else: it is one locale, on two nodes |

Both node-reading checks refuse to report rather than report a clean zero
instead: each has an absolute floor on how many files it will accept as a real
copy, because a truncated directory reports nothing wrong. The node-to-node one
additionally refuses two directories that resolve to the same path, since
comparing a tree with itself is flawless and meaningless.

The second one is **not** wired into `audit.sh` — the wrapper always runs step 4
against the new tag, so run the directory mode by hand, once per node:

```sh
python3 scripts/flag_algorithmic_ranges.py \
    --locales-dir ./el8-locales --build-id glibc-2.28-251.el8_10.40 \
    --supported-tag glibc-2.28
```

`--supported-tag` is optional and maps source file names to the
[generated names](glossary.md) `locale -a` shows; a node ships no `SUPPORTED`
file of its own. Note that passing it is the one thing here that needs the
glibc clone — without it the scan reads nothing but the directory, and `locale
-a` on the node is your mapping.

`audit.sh` runs the first as step 8 when given both nodes' directories, and
when not given them **says so in the summary** rather than omitting the
section:

```
-- Node-to-node locale data: NOT RUN
     Pass --old-locales-dir and --new-locales-dir with their build ids.
     Without it nothing above says anything about C.UTF-8 ...
```

That is the same rule as the step 2 warning, one level up: a summary that
prints nothing about `C.UTF-8` and one that has cleared it look identical on a
terminal, and that is how this locale gets missed.

## Upstream tags are not your distro's glibc

RHEL8 ships `glibc-2.28-251.el8_10.40` with hundreds of backports. A
backported collation change would be invisible to a `glibc-2.28..glibc-2.34`
diff, which is the main reason not to skip
[the confirmation step on real nodes](confirming-on-a-real-system.md).

### Where each pair stands

| Pair | Backports measured? | What covers the gap |
|---|---|---|
| `RHEL8 -> RHEL9` | **yes**, the numbers below | the measurement itself |
| `RHEL9 -> RHEL10` | **yes**, since 2026-09-06 | the measurement itself, plus the empirical confirmation in [`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt) |

Both audited pairs are now covered analytically as well as empirically.

### Measured, on all three OS versions

`scripts/diff_distro_locales.py` answers this. It compares every file in a
node's `/usr/share/i18n/locales/` (package `glibc-locale-source`) against the
same file at the upstream tag, and reports whether any difference lands inside
the `LC_COLLATE` block:

```sh
python3 scripts/diff_distro_locales.py glibc-2.28 \
    --locales-dir ./el8-locales --build-id glibc-2.28-251.el8_10.40
```

| Node build | Upstream tag | Compared | Differing | Inside `LC_COLLATE` | Absent upstream |
|---|---|---|---|---|---|
| `glibc-2.28-251.el8_10.40` | `glibc-2.28` | 353 | 73 | **0** | `C`, `en_US@ampm` |
| `glibc-2.34-275.el9_8` | `glibc-2.34` | 355 | 2 | **0** | `C` |
| `glibc-2.39-128.el10_2` | `glibc-2.39` | 366 | 3 | **0** | none |

The backports on every side land in other categories, so for every locale this
method can audit, the tag diff is reading the same collation data the nodes
run. The three files differing at `el10` are `bg_BG`, `hr_HR` and `ssy_ER`.

The `C` in that last column is the whole of the first limitation, and it is
answered by comparing the two **nodes** to each other instead of each node
against a tag — measured, 2026-09-06:

```sh
python3 scripts/diff_node_locales.py \
    --old-locales-dir ./el8-locales --old-build-id glibc-2.28-251.el8_10.40 \
    --new-locales-dir ./el9-locales --new-build-id glibc-2.34-275.el9_8 \
    --old-tag glibc-2.28 --new-tag glibc-2.34
```

| Node pair | Compared | Inside `LC_COLLATE` | Of those, at neither tag |
|---|---|---|---|
| `-251.el8_10.40` → `-275.el9_8` | 354 | 3: `C`, `or_IN`, `sv_SE` | **1: `C`** |
| `-275.el9_8` → `-128.el10_2` | 355 | 3: `ber_DZ`, `kab_DZ`, `th_TH` | 0 |

`or_IN`/`sv_SE` and `ber_DZ`/`kab_DZ`/`th_TH` are exactly what step 2 reports
for those tag pairs, reached by a different algorithm — which is what makes the
mode's answer about `C` worth citing. It also found two locales no step
reports, because neither is a change to a locale but a change to the *set* of
them: `en_US@ampm` is **removed** at RHEL9, and `aa_ER@saaho` at RHEL10. An
index on a collation that no longer exists does not sort differently; it does
not sort.

**Compared is not the node's file count.** An earlier version of this page said
"73 of 355", which conflated the two: upstream `glibc-2.28` has 353 locale
files, and 353 is what could be compared — the node's other two exist upstream
nowhere, and are the row's "absent" column rather than part of its denominator.

The first two rows reproduce the earlier hand measurement exactly, re-run on
independently provisioned nodes. The third is new: it needed a RHEL10 node,
which is why that pair's row above used to say "no".

**The two absent-upstream files are not equally blind**, and the script says
which is which by reading the node's own copy:

- `en_US@ampm` is a pure `copy` of `iso14651_t1`, which *was* compared and is
  identical. It has no collation of its own, so nothing is hidden. It is Red
  Hat-only — in no upstream tag from 2.28 to 2.41 — and until now nothing in
  the tool mentioned it at all.
- `C` carries its own tailoring, so *this* comparison cannot audit it: there
  is no upstream file to compare it against. That is the `C.UTF-8` limitation
  above, now confirmed mechanically rather than asserted — and audited from
  source anyway, by comparing the two nodes' copies to each other instead of
  each one to a tag.

### What that measurement does not cover

The exception is `C.UTF-8`, above: it is a backported collation change on
exactly this pair, and it is invisible to *this* comparison for the same reason
it is invisible to the audit — the file it comes from exists at neither tag, so
there is nothing to compare it *against*. This measurement is a statement about
the locales the method covers, and it does not rescue the one it does not. The
node-to-node comparison beside the table above is what does.

A comparison that always answered "identical" would produce those same zeros,
so the check carries a [positive control](glossary.md) against locales whose
answer is known from step 2. Run against the `el8` node's sources:

| Locale | vs `glibc-2.28` | vs `glibc-2.34` |
|---|---|---|
| `sv_SE` | identical | **different** |
| `ko_KR` | identical | identical |
| `or_IN` | **different** | **different** |

`sv_SE` and `ko_KR` are what step 2 predicts. `or_IN` is the one to read
carefully, and it corrects what this page used to claim: the file is **not**
identical to `glibc-2.28`. It differs by a single line, in
`LC_IDENTIFICATION`, where the distro renamed the language:

```
-language    "Oriya"
+language    "Odia"
```

Its `LC_COLLATE` block is byte-identical, which is why `or_IN` sits among the
73 differing files while contributing nothing to the zero above. That is the
distinction this whole section rests on — a locale file changing is not a
locale's sort order changing — and the positive control happens to demonstrate
it.

The claim is bounded by what was compared: **these** package builds against
these tags. It is not a general result that distro backports never touch
collation, and a different build of the same distro release is a different
measurement. Say which build a result was taken on.

**Automated, and self-checking without a node.** The script is exercised on
every push by comparing two upstream tags against each other and asserting it
reaches the same answer as step 2, which gets there by a completely different
route — diff hunks overlapped against the old side's line numbers, versus
whole-block equality. Only the transport off a real node stays un-exercised in
CI, the same position `sql/collation_confirmation_template.sql` is in.

`./audit.sh` runs it too, per side, for whichever of `--old-locales-dir` and
`--new-locales-dir` you give it: steps 1 to 5 read the clone alone, and this
needs a node's files. Give it both and it additionally compares the two nodes to
each other, which is a different question — see the first limitation.

## Below glibc 2.24 the method breaks silently

This project audits RHEL8 → RHEL9 and RHEL9 → RHEL10, both comfortably above
that floor. The limit matters anyway, because nothing enforces it: point the
tool at an older pair and it answers confidently and wrongly rather than
refusing.

**There is no guard for this.**

### Why it fails, and by how much

In glibc 2.23 and earlier, the three [collation templates](glossary.md)
(`iso14651_t1`, `iso14651_t1_common`, `iso14651_t1_pinyin`) begin with
`LC_COLLATE` on the very first byte of the file, and the block reader does
not recognise them there. Steps 3 and 4 walk the [`copy` graph](glossary.md)
at the **new** tag, so when that tag is old the graph loses its three roots
and the inheritance closure collapses — silently, in the reassuring
direction.

Measured on `glibc-2.12 -> glibc-2.17` — a pair far below the floor, run only
to show what the failure looks like, not because it is audited: step 2
correctly finds `iso14651_t1_common` changed, then step 3 reports **11**
affected locales where there are **278**, and step 4 reports **2** exposed
where there are **279**. The 267 names it drops include `en_US`, `de_DE`,
`fr_FR`, `es_ES`, `it_IT`, `nl_NL`, `pt_BR`, `ru_RU`, `sv_SE`, `zh_CN` and
`zh_TW`. What actually changes over that pair is 109 Tibetan code points
gaining a collation weight in `iso14651_t1_common`.

## Character repertoire changes are not audited

`localedata/charmaps/` is an input to `localedef` — it decides which
characters exist to be given a weight — and no step looks at it.
`charmaps/UTF-8` gains 1632 code points over 2.28..2.34, 1658 over
2.34..2.39 and 5235 over 2.39..2.41.

Measured rather than assumed: 99 of those newly added code points, mixed with
pre-existing reference characters and sorted on real RHEL8 and RHEL9 nodes,
come out in **identical order** under `en_US`, `sv_SE`, `de_DE`, `ar_SA` and
`zh_CN`. Adding a character to the repertoire gives that character a weight;
it does not move the characters that already had one.

So this is a gap in coverage with no demonstrated impact — but it is a gap,
and only data containing those characters could ever be affected by it.

## Step 5 reports, it does not decide

It cannot tell a weight-changing commit from a harmless one. Over
2.28..2.34 it surfaces both the ellipsis fix (which reorders `ko_KR`) and a
hash-table sizing change (which reorders nothing). **Read the
[hunks](glossary.md) it prints.**

This is the one part of the method that is not mechanical, and the one part
that requires reading C. If nobody on hand will do that, treat every locale
step 4 flags as unresolved and
[confirm it empirically](confirming-on-a-real-system.md) instead — that path
needs no source reading and is the stronger evidence anyway.

---

[Documentation index](README.md) · [Scope](scope.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Glossary](glossary.md)
