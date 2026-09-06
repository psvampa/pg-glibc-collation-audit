# Results

## The answer

The verdict table lives in [the README](../README.md#results-for-the-two-rhel-pairs)
so there is only ever one copy of it to keep current. This page is the
evidence behind each row.

Two version pairs were run end to end. `ko_KR` is the row a data-only audit
gets wrong, and `C.UTF-8` the row no *tag* diff can reach — its source file is
in neither tag for the first pair, so steps 1-5 are blind to it and step 2
names it rather than settling it. It is settled by reading the file where it
does exist, on the nodes themselves: both its verdicts were measured directly
on 2026-09-06, replacing ardentperf's checksum as the basis for the
RHEL9→RHEL10 column. See
[limitations.md](limitations.md#cutf-8-is-invisible-to-a-tag-diff) and the
worked-example section below.

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

## If you saved an earlier result

If you saved a result from this tool before 2026-09-07, check
[CHANGELOG.md](../CHANGELOG.md) first. Three verdicts have moved since:

- **`th_TH` for the RHEL9-to-RHEL10 pair** was reported 🟡 Unresolved until
  2026-09-06 and it **changes** — indexes on it need a `REINDEX` across that
  upgrade. This is the most recent move, and the one most likely to affect a
  result you are still holding.
- **`ko_KR` for the RHEL8-to-RHEL9 pair** was once reported unaffected and it
  changes.
- **`zh_CN` and three siblings** used to be cleared by step 4, which should
  have flagged them.

No verdict moved on 2026-09-06 when `C.UTF-8` was measured directly for the
first time, but two things did.

**A conclusion this page never contradicted turns out to be wrong:** *"we are
staying on RHEL8, so `C.UTF-8` is fine"*. Its order also changed *within* RHEL8,
in `glibc-2.28-93.el8` (RHEL 8.2) — see the `C.UTF-8` section of the
RHEL8-to-RHEL9 worked example below. If you decided not to reindex because you
were not crossing a major, that decision was made on incomplete information.

And the *basis* of its RHEL9→RHEL10 🟢 moved: it was ardentperf's checksum and
is now a byte comparison of both nodes' own locale sources plus a 41-code-point
probe on each.

No verdict moved on 2026-09-05, but six ways of reaching one silently did, so
a run from that day prints things an earlier one did not: a `C.UTF-8` warning
on both pairs, the files with no `LC_COLLATE` block named rather than
counted, and a third tier in step 5 that raises its [hunk](glossary.md)
counts from 8 to 25 and from 48 to 53. A saved result whose locale lists
match is still right; what it was missing is the caveats beside them.

## Worked example: RHEL8 to RHEL9 (glibc 2.28 to 2.34)

`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro` and `ko_KR` change. Everything else
that this method can settle is clear; `zh_CN` and its pinyin siblings are
flagged by step 4 and cleared only by measurement.

Full output and the PostgreSQL confirmation script for this pair:
[`examples/rhel8-to-rhel9-audit-output.txt`](../examples/rhel8-to-rhel9-audit-output.txt),
[`examples/rhel8-to-rhel9.sql`](../examples/rhel8-to-rhel9.sql).

### The full run

```sh
cd scripts
./audit-locale-diff.sh glibc-2.28 glibc-2.34
python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34
python3 resolve_copy_closure.py glibc-2.34 or_IN sv_SE
python3 flag_algorithmic_ranges.py glibc-2.34
python3 diff_collation_code.py glibc-2.28 glibc-2.34
```

### `or_IN` and the Swedish locales — from the data diff

**`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro`** change sort order between glibc
2.28 and 2.34. As [generated locales](glossary.md): `or_IN`, `sv_SE`,
`sv_SE.utf8`, `sv_FI`, `sv_FI.utf8`, `sv_FI@euro`. `sv_FI` comes in only via
inheritance from `sv_SE` and is never flagged by a plain file diff.

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
25-million-string checksums show `ko` changing on Debian exactly between 2.31
and 2.36 — bracketing 2.34. This is the case a data-only audit gets wrong,
which is why step 5 exists.

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
Derive test strings from the rule that changed; for a `localedef` change that
means the boundaries of the affected range.

### The CJK range — flagged by step 4, cleared by measurement

Step 4 also flags `iso14651_t1`'s CJK range (U+4E00..U+9FA5), inherited by
328 locales. Step 5 shows the ellipsis logic did change in this pair, so a
source diff cannot clear those locales either.

Empirically they are fine, and this one is explainable rather than merely
observed: the ellipsis is followed by an explicit `<U9FA5>` line, so the
stale cursor re-inserts U+9FA5 exactly where it already was. Predicted from
the source, then confirmed on real nodes — the range boundary
(`一 龤 龥 龦`) sorts identically under `en_US` and `zh_TW` on glibc 2.28 and
2.34 — and independently corroborated by ardentperf, whose corpus covers
every Unicode code point and whose `en`, `de` and `fr` checksums are
identical across RHEL8 and RHEL9.

**The caveat that remains:** my own test covers the range *boundary*, which
is where this particular bug lives, not a broad CJK corpus, and
`zh_TW`/`zh_HK`/`zh_SG` are absent from ardentperf's set. For those three the
evidence is a mechanism argument plus a targeted test, not a broad empirical
sweep. They are 🟢 on weaker evidence than the other 🟢 rows.

### `C.UTF-8` — from the nodes' own files, because no tag has them

Every other 🔴 row on this page was reached by steps 1-5. This one cannot be:
`localedata/locales/C` exists upstream only from glibc 2.35, so for a
`2.28..2.34` comparison it is in neither tag. RHEL8 and RHEL9 both ship it by
backport, so it is on both **nodes** — and comparing the nodes to each other is
what settles it.

Measured 2026-09-06 on `glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8`,
both PostgreSQL 18.6:

| | RHEL8 | RHEL9 |
|---|---|---|
| its `LC_COLLATE` | six **ellipsis ranges** — planes 0, 1, 2, 14, 15, 16, then `UNDEFINED` | the single keyword **`codepoint_collation`** |
| step 4 over the node's directory | flagged | byte order by construction |
| `C.utf8` equals byte order? | **no**, 40 of 41 probed code points in a different position | yes, 0 |
| `strxfrm` key for U+10000 | `ef85b5` — a computed weight | `f0908080` — the UTF-8 bytes themselves |
| database `datcollate` / `datcollversion` | `C.UTF-8` / NULL | `C.UTF-8` / NULL |

So it **changed**, and the mechanism is fully accounted for rather than merely
observed. An ellipsis range carries no weights — `localedef` computes them, and
glibc 2.34 took the Bug 22668 commit that changed exactly that expansion. On
top of it, planes 3 through 13 have no range at all in the RHEL8 file (Red Hat
bug 1361965), so those code points fall to `UNDEFINED`; that is why the RHEL8
order is scrambled rather than merely shifted, with ASCII landing at position
31. RHEL9 backported upstream's `codepoint_collation`, which discards all
collation information in favour of `strcmp`.

Two things make this row worth reading twice. The `datcollversion` was NULL on
both nodes — as it always is for a `C.*` name — so **nothing warned**, and the
databases' default collation was `C.UTF-8` on both, which is what an `initdb`
in a container gives you. And the [positive control](glossary.md) inverts here:
RHEL9's agreement with byte order is the *fix*, not the usual sign that a
locale was never generated. The `strxfrm` row is what rules out the third
reading, where tied weights are rescued by PostgreSQL's own `strcmp`
tie-break.

**And it changed inside RHEL8 too.** `glibc-2.28-93.el8` (RHEL 8.2,
[RHSA-2020:1828](https://access.redhat.com/errata/RHSA-2020:1828), Red Hat bug
1361965) rewrote those ellipsis expressions so that the code points above
U+10000 gained weights at all; the compiled locale grew 5.3 MiB. The node says
so itself — `rpm -q --changelog glibc | grep -i collat` prints *"Fix C.UTF-8
locale source ellipsis expressions (#1361965)"* on RHEL8, and nothing at all on
RHEL9 or RHEL10, out of changelogs of 158 and 112 entries.

Both sides of that upgrade are upstream glibc 2.28, so the tag pair is
`glibc-2.28..glibc-2.28` and steps 1-5 have nothing to compare. This table is
keyed on two major upgrades and cannot express it, so read it here: **staying
on one RHEL major is not a control for this locale.**

Full output:
[`examples/c-utf8-probe-rhel8-vs-rhel9.txt`](../examples/c-utf8-probe-rhel8-vs-rhel9.txt).

### Everything else

Every other locale (`en_US`, `de_DE`, `fr_FR`, ...) is unaffected, confirmed
by the unchanged templates and by real `sort`/PostgreSQL tests on RHEL8 and
RHEL9 nodes.

`zh_CN` is not in that group: it reaches `iso14651_t1_common` through
`iso14651_t1_pinyin`, so step 4 flags it and the `sort` measurement — not the
clean data diff — is what clears it.

## Worked example: RHEL9 to RHEL10 (glibc 2.34 to 2.39)

`ber_DZ`, `kab_DZ` and `th_TH` are flagged; `th_TH` changes sort order.
`ko_KR` is flagged by step 4 and then cleared by step 5. `C.UTF-8` **cannot**
change across this pair, and that is a structural statement rather than a
measurement that happened to come out clean: both nodes'
`/usr/share/i18n/locales/C` is byte-identical and declares
`codepoint_collation`, so there is no expansion logic left for a `localedef`
change to move. The 41-code-point probe returns identical output on both
nodes, down to the sort keys —
[`examples/c-utf8-probe-rhel9-vs-rhel10.txt`](../examples/c-utf8-probe-rhel9-vs-rhel10.txt).

Full output:
[`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt).

### `ber_DZ`, `kab_DZ` and `th_TH` — from the data diff

Same steps, different pair. **`ber_DZ`, `kab_DZ`, `th_TH`** flagged by steps
1 to 3, `ko_KR` flagged again by step 4.

On inspection, `ber_DZ` and `kab_DZ` turned out to be a
[role swap](glossary.md) — the same collation ruleset, relocated to the other
file — not an actual rule change, confirmed by an empirical test showing no
observable difference.

`th_TH` is a real rewrite, and it **does** move sort order — measured on
2026-09-06, after an earlier narrow sample had left it unresolved.

The diff deletes 220 `collating-element` definitions and replaces them with
`copy "iso14651_t1"` plus CLDR tailoring. Those 220 are exactly the five Thai
leading vowels (U+0E40–U+0E44) against 44 consonants. A leading vowel is
written before its consonant but pronounced after, so each pair used to be one
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
Measured on Rocky Linux 9.3 (`glibc-2.34-83.el9.7`) and Rocky Linux 10.1
(`glibc-2.39-58.el10_1.2`), both PostgreSQL 18.6, and re-confirmed unchanged on
the newer builds `glibc-2.34-275.el9_8` and `glibc-2.39-128.el10_2` — so no
backport between those builds moves it. With `ber_DZ`, `kab_DZ`,
`ko_KR`, `en_US`, `de_DE` and `fr_FR` identical on both nodes as controls.
Reproduce with [`examples/rhel9-to-rhel10.sql`](../examples/rhel9-to-rhel10.sql).

### `ko_KR` — how step 5 clears a step-4 locale

This pair also shows step 5 working in the other direction. `ko_KR` is
flagged by step 4 here too, but step 5 finds no change to ellipsis expansion
between 2.34 and 2.39.

That verdict rests on having **read** the two tier-3 hunks (`lr_getc` in
`linereader.h`, `elem_hash` in `elem-hash.h`) and found that neither moves a
weight. Earlier versions of the tool never printed those hunks at all, so the
verdict used to rest on their absence; now it rests on their content. The
tier-1 changes in that range are `%Z`-to-`%z` format fixes, integer type
replacements, and a new opt-in `codepoint_collation` keyword that no
pre-existing locale uses.

So `ko_KR` is genuinely unaffected across RHEL9 to RHEL10, which steps 1 to 4
alone could never conclude. ardentperf's checksums agree: all ten of their
locales, `ko` included, are identical between RHEL9 and RHEL10. Being able to
clear a step-4 locale, rather than only ever flagging it, is the point of
step 5.

## Tested on

Both pairs are now confirmed on **PostgreSQL 18.6**, so no verdict on this
page rests on a different PostgreSQL from any other.

- **RHEL8 to RHEL9** (glibc 2.28 to 2.34): full method run, plus empirical
  confirmation on side-by-side Rocky Linux 8.9 / Rocky Linux 9.3 nodes
  carrying `glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8` — the same
  package builds ardentperf tested — both running **PostgreSQL 18.6**,
  re-measured 2026-09-06. Every claim in the worked example is measured, not
  inferred; see
  [`examples/rhel8-to-rhel9-audit-output.txt`](../examples/rhel8-to-rhel9-audit-output.txt).
  The distro-versus-upstream backport check under
  [limitations.md](limitations.md#upstream-tags-are-not-your-distros-glibc)
  was measured on these same two builds, and so were the node-to-node
  comparison and the `C.UTF-8` probe added on 2026-09-06. Those two are
  separate measurements from the ones above even though the builds match:
  they read files (`/usr/share/i18n/locales/`) that the earlier runs never
  looked at.

  This block was first measured on PostgreSQL 16.15, on the same OS and the
  same glibc builds. Every sort-order result reproduced unchanged on 18.6,
  which is what a glibc-level finding should do — PostgreSQL calls `strcoll`,
  it does not implement the order.
- **RHEL9 to RHEL10** (glibc 2.34 to 2.39): full method run, confirmed
  against real nodes running **PostgreSQL 18.6**, not just the source diff;
  see
  [`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt).
  The `ber_DZ`, `kab_DZ`, `ko_KR` and `en_US` lines in that file predate step
  5 and are marked as such; the `th_TH` line was re-measured on 2026-09-06,
  after step 5 existed, and is what moved that verdict. The node-to-node
  comparison and the `C.UTF-8` probe were measured on `glibc-2.34-275.el9_8`
  and `glibc-2.39-128.el10_2`, Rocky Linux 9.3 and 10.1, PostgreSQL 18.6, on
  2026-09-06.

---

[Documentation index](README.md) · [The method](method.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
