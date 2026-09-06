# Known limitations

Five things this method structurally cannot see. The first two can change
your answer; the third is a hard kill condition; the fourth is a gap in
coverage with no demonstrated impact; the fifth is the manual step in an
otherwise mechanical method.

1. [`C.UTF-8` cannot be audited by this method](#cutf-8-cannot-be-audited-by-this-method)
2. [Upstream tags are not your distro's glibc](#upstream-tags-are-not-your-distros-glibc)
3. [Below glibc 2.24 the method breaks silently](#below-glibc-224-the-method-breaks-silently)
4. [Character repertoire changes are not audited](#character-repertoire-changes-are-not-audited)
5. [Step 5 reports, it does not decide](#step-5-reports-it-does-not-decide)

## `C.UTF-8` cannot be audited by this method

Its source file, `localedata/locales/C`, only exists upstream from glibc
**2.35**, but RHEL8 and RHEL9 both ship a backported `C.UTF-8` — and it
**does** change between them (Bug 22668 again; the commit message calls out
`C.UTF-8` explicitly). Comparing upstream 2.28 and 2.34 cannot see a file
that is in neither.

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

This does not affect `COLLATE "C"`, which is byte order and immutable, but it
does affect indexes built on `C.UTF-8`. Test that one empirically.

Note what the [positive control](glossary.md) looks like here, because it
inverts: at 2.34 `C.utf8` produces *exactly* byte order, so agreeing with
`LC_ALL=C` is the corrected behaviour rather than the usual sign that a locale
was never generated. At 2.28 it is the disagreement that shows the bug.

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

The tool cannot fix that, but it no longer stays quiet about it. Step 2 emits
a named warning whenever `localedata/locales/C` is absent at the old tag —
which is every pair where this bites — saying that the locale very likely
exists on your old system, that its order can change, that PostgreSQL will
not warn, and that the clean result in [results.md](results.md) says nothing
about it.

Read this section as the reason for that warning, not as a substitute for it:
a step that prints nothing about `C.UTF-8` and a step that has cleared it
look identical on a terminal, and that is how this locale gets missed.

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
- `C` carries its own tailoring, so it is genuinely unauditable from source.
  That is the `C.UTF-8` limitation above, now confirmed mechanically rather
  than asserted.

### What that measurement does not cover

The exception is `C.UTF-8`, above: it is a backported collation change on
exactly this pair, and it is invisible here for the same reason it is
invisible to the audit — the file it comes from exists at neither tag, so
there is nothing to compare it against. This measurement is a statement about
the locales the method covers, and it does not rescue the one it does not.

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

`./audit.sh` runs it too, but only when given `--old-locales-dir` /
`--new-locales-dir`: steps 1 to 5 read the clone alone, and this needs a node's
files.

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
