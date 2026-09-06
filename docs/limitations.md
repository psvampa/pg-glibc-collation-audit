# Known limitations

Five things this method structurally cannot see. The first two are the ones
that can change your answer; the third is a hard kill condition; the last is
the manual step in an otherwise mechanical method.

1. [`C.UTF-8` cannot be audited by this method](#cutf-8-cannot-be-audited-by-this-method)
2. [Upstream tags are not your distro's glibc](#upstream-tags-are-not-your-distros-glibc)
3. [The destination must be glibc 2.24 or newer](#the-destination-must-be-glibc-224-or-newer)
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

This does not affect `COLLATE "C"`, which is byte order and immutable, but it
does affect indexes built on `C.UTF-8`. Test that one empirically.

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

RHEL8 ships `glibc-2.28-251.el8` with hundreds of backports. A backported
collation change would be invisible to a `glibc-2.28..glibc-2.34` diff, which
is the main reason not to skip
[the confirmation step on real nodes](confirming-on-a-real-system.md).

### Where each pair stands

| Pair | Backports measured? | What covers the gap |
|---|---|---|
| `RHEL8 -> RHEL9` | **yes**, the numbers below | the measurement itself |
| `RHEL9 -> RHEL10` | no — it needs a RHEL10 node, and the measurement below was taken on Rocky 8 / Rocky 9 only | the empirical confirmation in [`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt), run on RHEL9 / RHEL10 nodes |
| `RHEL7 -> RHEL8` | no | **nothing** — the pair was never confirmed on real nodes either |

So for `RHEL9 -> RHEL10` the cover is empirical, not analytical: it rests on
observed sort order on real nodes rather than on knowing the distro's
collation data matches the tag. And on `RHEL7 -> RHEL8` a backported
collation change would still pass unnoticed today.

### Measured for RHEL8 → RHEL9

For the flagship pair this is measured rather than left open. Comparing every
distro locale source in `/usr/share/i18n/locales/` (package
`glibc-locale-source`) against the same file at the upstream tag:
`glibc-2.28-251.el8_10.40` differs from `glibc-2.28` in **73 of 355** files,
and `glibc-2.34-275.el9_8` differs from `glibc-2.34` in **2 of 356**. In
**none** of them does the difference fall inside the `LC_COLLATE` block.

The backports on both sides land in other categories, so for every locale
this method can audit, the tag diff is reading the same collation data the
nodes run.

### What that measurement does not cover

The exception is `C.UTF-8`, above: it is a backported collation change on
exactly this pair, and it is invisible here for the same reason it is
invisible to the audit — the file it comes from exists at neither tag, so
there is nothing to compare it against. This measurement is a statement about
the locales the method covers, and it does not rescue the one it does not.

A comparison that always answered "identical" would produce that same zero,
so the check carries a [positive control](glossary.md) against locales whose
answer is known from step 2: the same method marks `sv_SE` and `or_IN` as
different from `glibc-2.34` and identical to `glibc-2.28`, and `ko_KR`
identical to both.

The claim is bounded by what was compared — these two package versions
against these two tags — and is not a general result that distro backports
never touch collation.

## The destination must be glibc 2.24 or newer

RHEL 8+, Ubuntu 18.04+, Debian 9+, SLES 15+. Note the direction: auditing
*from* an older system is fine, so `RHEL 7 -> RHEL 8` is correct. What is out
of scope is auditing *towards* RHEL 7 or older.

**There is no guard for this: run the tool outside the supported range and it
answers confidently and wrongly.**

### Why it fails, and by how much

In glibc 2.23 and earlier, the three [collation templates](glossary.md)
(`iso14651_t1`, `iso14651_t1_common`, `iso14651_t1_pinyin`) begin with
`LC_COLLATE` on the very first byte of the file, and the block reader does
not recognise them there. Steps 3 and 4 walk the [`copy` graph](glossary.md)
at the **new** tag, so when that tag is old the graph loses its three roots
and the inheritance closure collapses — silently, in the reassuring
direction.

Measured on `glibc-2.12 -> glibc-2.17`, a RHEL 6 to RHEL 7 audit: step 2
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
