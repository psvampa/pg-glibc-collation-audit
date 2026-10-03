# Results

## The answer

| Locale | RHEL8 → RHEL9<br>glibc 2.28 → 2.34 | RHEL9 → RHEL10<br>glibc 2.34 → 2.39 | Caught by |
|---|---|---|---|
| `sv_SE`, `sv_FI`, `sv_FI@euro` | 🔴 **Changed** | 🟢 No difference | steps 1–3 — `sv_FI` only via `copy`; the second pair is cleared by reading step 5, as for `ko_KR` |
| `or_IN` | 🔴 **Changed** | 🟢 No difference | steps 1–3; the second pair is cleared by reading step 5, as for `ko_KR` |
| `ko_KR` | 🔴 **Changed** | 🟢 No difference | **step 5** — its `LC_COLLATE` is unchanged in *both* pairs |
| `C.UTF-8` | 🔴 **Changed** | 🟢 No difference | **step 2 warns** and cannot settle it <sup>†</sup> — the node-to-node check settles the data, step 11 or `sql/c_utf8_probe.sql` the order |
| `th_TH` | 🟢 No difference | 🔴 **Changed** | steps 1–3; the first pair is cleared by step 11's measurement |
| `ber_DZ`, `kab_DZ` | 🟢 No difference | 🟢 No difference | steps 1–3 flagged it; inspection found a role swap; the first pair is cleared by step 11's measurement |
| CJK range U+4E00–U+9FA5 in `iso14651_t1`,<br>the base table a locale inherits unless it<br>defines its own order | 🟢 No difference | 🟢 No difference | step 4 flagged it; step 5 says a diff can't clear it |
| `zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`,<br>`cns11643_stroke` | 🟢 No difference | 🟢 No difference | step 4 flagged them via `iso14651_t1_common`; cleared by measurement |
| everything else — `en_US`, `de_DE`,<br>`fr_FR`, … | 🟢 No difference | 🟢 No difference | step 4 flags them and step 5 finds code changes in both pairs: step 11's measurement clears the first, as far as it can measure, and reading step 5 the second, as for `ko_KR` |

<sup>†</sup> `C.UTF-8`'s source file is in neither tag for the first pair, so
both verdicts come from the machines themselves
([below](#cutf-8--from-the-nodes-own-files-because-no-tag-has-them)).

`C.UTF-8`'s order also changed *within* RHEL8, in `glibc-2.28-93.el8`
(RHEL 8.2). Staying on one RHEL major is not a control for this locale. The
evidence is [below](#cutf-8--from-the-nodes-own-files-because-no-tag-has-them).

The same table is in [the README](../README.md#results-for-the-two-rhel-pairs).
The sections below are the evidence behind each row.

## How to read the verdicts

- 🔴 **Changed** — sort order moves. Reindex.
- 🟡 **Unresolved** — flagged and *not* cleared. Treat as changed until
  tested.
- 🟢 **No difference** — the audit flagged it, but a targeted test or a
  mechanism argument shows the order does not move. Weaker than "unaffected".
- ⚪ **Unaffected** — neither the locale's `LC_COLLATE` nor the collation code
  changed. Note that a locale's *file* often changes without this being
  disturbed: most of the 283 files that differ between 2.28 and 2.34 changed
  only `LC_TIME` or `LC_MONETARY`. This is the deterministic verdict the
  method exists to produce.

## Worked example: RHEL8 to RHEL9 (glibc 2.28 to 2.34)

Full output and the PostgreSQL confirmation script for this pair:
[`examples/rhel8-to-rhel9-audit-output.txt`](../examples/rhel8-to-rhel9-audit-output.txt),
[`examples/rhel8-to-rhel9.sql`](../examples/rhel8-to-rhel9.sql).

### `or_IN` and the Swedish locales — from the data diff

**`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro`** change sort order between glibc
2.28 and 2.34. `sv_FI` comes in only via inheritance from `sv_SE` and is
never flagged by a plain file diff.

### `ko_KR` — from the code diff

**`ko_KR` also changes**, and its data file is byte-identical between the two
tags. Step 5 pins the cause to a single commit,
[`82292c99b2`](https://sourceware.org/bugzilla/show_bug.cgi?id=22668)
("LC_COLLATE: Fix last character ellipsis handling", Bug 22668), which landed
in glibc 2.34 and changed how `localedef` expands the ellipsis ranges that
`ko_KR` depends on.

Two independent checks agree: `localedata/locales/ko_KR` is unchanged across
both `2.28..2.34` and `2.31..2.36`, and
[ardentperf's](https://github.com/ardentperf/glibc-unicode-sorting)
checksums show `ko` changing on Debian exactly between 2.31 and 2.36 —
bracketing 2.34. This is the case a data-only audit gets wrong, which is why
step 5 exists.

#### The `ko_KR` mechanism, and its minimal test case

`ko_KR`'s `LC_COLLATE` is `<UAC00>` / `..` / `<UD7A3>` (the Hangul
syllables), immediately followed by the Hanja list starting at `<U4F3D>`.
Before the fix the cursor was left on `<UD7A2>` instead of `<UD7A3>` after
expanding the ellipsis, so the first Hanja was linked in *before* `<UD7A3>` —
leaving the last Hangul syllable dangling at the very end of the section:

```
$ printf '가\n힢\n힣\n伽\n佳\n' | LC_ALL=ko_KR.UTF-8 sort | tr '\n' ' '
glibc 2.28:  가 힢 伽 佳 힣      <- U+D7A3 dangling at the end
glibc 2.34:  가 힢 힣 伽 佳      <- U+D7A3 back in place
```

That is the whole difference: `힣` (U+D7A3) versus any Hanja. It is also why
a hand-picked sample of everyday Korean text shows nothing — U+D7A3 is the
last syllable of the Hangul block, and text essentially never reaches it.
How to turn a rule like this one into the three values the template tests
with is in
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#choosing-the-three-values).

### The CJK range — flagged by step 4, cleared by measurement

Step 4 also flags `iso14651_t1`'s CJK range (U+4E00..U+9FA5), inherited by
328 locales. Step 5 shows the ellipsis logic did change in this pair, so a
source diff cannot clear those locales either.

Empirically they are fine, and this one is explainable rather than merely
observed: the ellipsis is followed by an explicit `<U9FA5>` line, so the
stale cursor re-inserts U+9FA5 exactly where it already was. Predicted from
the source, then confirmed on real nodes — the range boundary
(`一 龤 龥 龦`) sorts identically under `en_US` and `zh_TW` on glibc 2.28 and
2.34.

Step 11 swept every character of `zh_TW.utf8`, `zh_HK.utf8` and
`zh_SG.utf8` on the three test machines and found no change in either pair.
What it cannot see is a rule for a combination of characters, and
`zh_TW.euctw`, whose encoding it cannot measure.

ardentperf reports a `glibc` engine and an `icu` engine, and only the first
bears on a `libc` collation. Between RHEL8 and RHEL9 every locale changes
under ICU — a full CLDR jump — while of the locales they test, only `ko` and
`C.UTF-8` change under glibc. A `zh` change read off those tables is an ICU
result and carries no `REINDEX` implication here. Their set is a fixed list
and holds no `sv` or `or_IN`, so it says nothing either way about two of the
locales this tool finds for that pair.

### `C.UTF-8` — from the nodes' own files, because no tag has them

Every other 🔴 row on this page was reached by steps 1-5. This one cannot be:
`localedata/locales/C` exists upstream only from glibc 2.35, so for a
`2.28..2.34` comparison it is in neither tag. RHEL8 and RHEL9 both ship it by
backport, so it is on both **nodes** — and comparing the nodes to each other is
what settles it.

Measured, and re-measured unchanged, on `glibc-2.28-251.el8_10.40` and
`glibc-2.34-275.el9_8`, both PostgreSQL 18.6:

| | RHEL8 | RHEL9 |
|---|---|---|
| its `LC_COLLATE` | six **ellipsis ranges** — planes 0, 1, 2, 14, 15, 16, then `UNDEFINED` | the single keyword **`codepoint_collation`** |
| steps 9 and 10, over the node's directory | flagged | byte order by construction |
| `C.utf8` equals byte order? | **no**, 40 of 41 probed code points in a different position | yes, 0 |
| `strxfrm` key for U+10000 | `ef85b5` — a computed weight | `f0908080` — the UTF-8 bytes themselves |
| database `datcollate` / `datcollversion` | `C.UTF-8` / NULL | `C.UTF-8` / NULL |

So it **changed**, and the mechanism is fully accounted for rather than merely
observed. An ellipsis range carries no weights — `localedef` computes them, and
glibc 2.34 took the Bug 22668 commit that changed exactly that expansion. On
top of it, planes 3 through 13 have no range at all in the RHEL8 file (Red Hat
bug 1361965), so those code points fall to `UNDEFINED`; that is why the RHEL8
order is scrambled rather than merely shifted. RHEL9 backported upstream's
`codepoint_collation`, which discards all collation information in favour of
`strcmp`. Step 11, which asks each node's glibc rather than reading the file,
measured the change too ([below](#what-step-11-measured)).

Two things make this row worth reading twice. The `datcollversion` was NULL on
both nodes — as it always is for a `C.*` name — so **nothing warned**, and the
databases' default collation was `C.UTF-8` on both, which is what an `initdb`
in a container gives you. And the [positive control](glossary.md) inverts here:
RHEL9's agreement with byte order is the *fix*, not the usual sign that a
locale was never generated.

**And it changed inside RHEL8 too.** `glibc-2.28-93.el8` (RHEL 8.2,
[RHSA-2020:1828](https://access.redhat.com/errata/RHSA-2020:1828), Red Hat bug
1361965) rewrote those ellipsis expressions so that the code points above
U+10000 gained weights at all.

Both sides of that upgrade are upstream glibc 2.28, so the tag pair is
`glibc-2.28..glibc-2.28` and steps 1-5 have nothing to compare. **Staying on
one RHEL major is not a control for this locale.**

Full output:
[`examples/c-utf8-probe-rhel8-vs-rhel9.txt`](../examples/c-utf8-probe-rhel8-vs-rhel9.txt).

### Everything else

Every other locale (`en_US`, `de_DE`, `fr_FR`, ...) sorts the same on both.
Step 4 flags them too, and step 5 finds code changes in this pair, so the
clean data diff does not clear them: real `sort`/PostgreSQL tests on RHEL8 and
RHEL9 nodes and step 11's measurement do.

## Worked example: RHEL9 to RHEL10 (glibc 2.34 to 2.39)

`C.UTF-8` **cannot** change across this pair, and that is a structural
statement rather than a measurement that happened to come out clean: both
nodes' `/usr/share/i18n/locales/C` is byte-identical and declares
`codepoint_collation`, so there is no expansion logic left for a `localedef`
change to move. The 41-code-point probe returns identical output on both
nodes, down to the sort keys —
[`examples/c-utf8-probe-rhel9-vs-rhel10.txt`](../examples/c-utf8-probe-rhel9-vs-rhel10.txt).

Full output:
[`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt).

### `ber_DZ`, `kab_DZ` and `th_TH` — from the data diff

**`ber_DZ`, `kab_DZ`, `th_TH`** flagged by steps 1 to 3, `ko_KR` flagged
again by step 4.

On inspection, `ber_DZ` and `kab_DZ` turned out to be a
[role swap](glossary.md) — the same collation ruleset, relocated to the other
file — not an actual rule change, confirmed by an empirical test showing no
observable difference.

`th_TH` is a real rewrite, and it **does** move sort order.

The diff deletes the `collating-element` definitions that paired each of the
five Thai leading vowels (U+0E40–U+0E44) with a consonant, and replaces them
with `copy "iso14651_t1"` plus CLDR tailoring. A leading vowel is written
before its consonant but pronounced after, so each pair used to be one
collating element sorting at the consonant's position.

Two code points in the consonant range never had such an element: U+0E24 (ฤ)
and U+0E26 (ฦ), the vowel-like letters — and that asymmetry is where the order
moves:

```
glibc 2.34:  ก เก ไก ไก่ ฤ ฦ ฮ เฤ เฦ      <- เฤ/เฦ dangle after the last consonant
glibc 2.39:  ก เก ไก ไก่ ฤ เฤ ฦ เฦ ฮ      <- each sorts beside its own consonant
```

Confirmed by direct `strcoll`, not only `ORDER BY`, so it is not a tie-break
artifact: `เฤ` > `ฮ` at 2.34 and `เฤ` < `ฮ` at 2.39, and likewise for `เฦ`.
With `ber_DZ`, `kab_DZ`, `ko_KR`, `en_US`, `de_DE` and `fr_FR` identical on
both nodes as controls. Reproduce with
[`examples/rhel9-to-rhel10.sql`](../examples/rhel9-to-rhel10.sql).

### `ko_KR` — how step 5 clears a step-4 locale

This pair also shows step 5 working in the other direction. `ko_KR` is
flagged by step 4 here too, but step 5 finds no change to ellipsis expansion
between 2.34 and 2.39.

That verdict rests on having **read** the two tier-3 hunks that bear on it
(`lr_getc` in `linereader.h`, `elem_hash` in `elem-hash.h`) and found that
neither moves a weight. The tier-1 changes in that range are `%Z`-to-`%z`
format fixes, integer type replacements, and a new opt-in
`codepoint_collation` keyword that no pre-existing locale uses.

So `ko_KR` is genuinely unaffected across RHEL9 to RHEL10, which steps 1 to 4
alone could never conclude. ardentperf's checksum for `ko` agrees, identical
between RHEL9 and RHEL10. Being able to clear a step-4 locale, rather than
only ever flagging it, is the point of step 5.

## What step 11 measured

Step 11 asks each machine's own glibc how it sorts every locale, rather than
reading source. Measured on the three test machines named under
[Tested on](#tested-on), with every language pack installed. The measurements
are kept whole in [`tests/locale_order/`](../tests/locale_order/), and the
suite checks against them which locales change.

**RHEL8 to RHEL9.** The locales that sort differently are the 🔴 rows of the
table: `C.utf8`, `ko_KR.utf8`, `or_IN` and `or_IN.utf8`, and the Swedish
locales, in UTF-8 and in the legacy encodings. In `ko_KR.utf8` only one
character moved, 힣 (U+D7A3), the last Hangul syllable
([the mechanism above](#the-ko_kr-mechanism-and-its-minimal-test-case)).
Every other locale it measured is unchanged, `ber_DZ`, `kab_DZ`, `zh_CN.utf8`
and `en_US.utf8` among them, though a few only as far as they could be
measured. The comparison names those, and also the few it could not measure
or that are missing from the new machine.

**RHEL9 to RHEL10.** Only `th_TH` sorts differently, in TIS-620 (`th_TH`,
`th_TH.tis620`, `thai`) and in `th_TH.utf8`. Every other locale it measured is
unchanged, `ber_DZ`, `kab_DZ` and `C.utf8` among them, though a few only as far
as they could be measured, `ko_KR.utf8` among those. The comparison names
them, and also the few it could not measure or that are missing from the new
machine.

No published verdict moved. What step 11 does not see is in
[limitations.md](limitations.md#step-11-measures-one-character-at-a-time).

## Tested on

| | RHEL8 | RHEL9 | RHEL10 |
|---|---|---|---|
| OS | Rocky Linux 8.9 | Rocky Linux 9.3 | Rocky Linux 10.1 |
| glibc | `glibc-2.28-251.el8_10.40` | `glibc-2.34-275.el9_8` | `glibc-2.39-128.el10_2` |
| PostgreSQL | 18.6 | 18.6 | 18.6 |

A build other than these can carry collation changes its distro backported,
which a comparison of the upstream tags does not see
([limitations.md](limitations.md#upstream-tags-are-not-your-distros-glibc)).

---

[Documentation index](README.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
