# Results

## The answer

The verdict table lives in [the README](../README.md#results-for-the-two-rhel-pairs)
so there is only ever one copy of it to keep current. This page is the
evidence behind each row.

Two version pairs were run end to end. `ko_KR` is the row a data-only audit
gets wrong, and `C.UTF-8` the row no source diff can reach — it is not
*auditable* by this method, since its source file is in neither tag for the
first pair. Step 2 names it and says why the clean result says nothing about
it, rather than settling it; see
[limitations.md](limitations.md#cutf-8-cannot-be-audited-by-this-method). Its
RHEL9→RHEL10 verdict is ardentperf's checksum, not my own test.

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

If you saved a result from this tool before 2026-09-05, check
[CHANGELOG.md](../CHANGELOG.md) first. Two verdicts have moved since: `ko_KR`
was once reported unaffected for the RHEL8-to-RHEL9 pair and it changes, and
step 4 used to clear `zh_CN` and three siblings that it should have flagged.

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

### Everything else

Every other locale (`en_US`, `de_DE`, `fr_FR`, ...) is unaffected, confirmed
by the unchanged templates and by real `sort`/PostgreSQL tests on RHEL8 and
RHEL9 nodes.

`zh_CN` is not in that group: it reaches `iso14651_t1_common` through
`iso14651_t1_pinyin`, so step 4 flags it and the `sort` measurement — not the
clean data diff — is what clears it.

## Worked example: RHEL9 to RHEL10 (glibc 2.34 to 2.39)

`ber_DZ`, `kab_DZ` and `th_TH` are flagged; only `th_TH` stays unresolved.
`ko_KR` is flagged by step 4 and then cleared by step 5.

Full output:
[`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt).

### `ber_DZ`, `kab_DZ` and `th_TH` — from the data diff

Same steps, different pair. **`ber_DZ`, `kab_DZ`, `th_TH`** flagged by steps
1 to 3, `ko_KR` flagged again by step 4.

On inspection, `ber_DZ` and `kab_DZ` turned out to be a
[role swap](glossary.md) — the same collation ruleset, relocated to the other
file — not an actual rule change, confirmed by an empirical test showing no
observable difference.

`th_TH` is a real rewrite. My own empirical sample showed no difference but
was narrow, so treat it as a candidate for a broader check on real Thai data,
**not as cleared**.

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

- **RHEL8 to RHEL9** (glibc 2.28 to 2.34): full method run, plus empirical
  confirmation on side-by-side Rocky 8 / Rocky 9 nodes carrying
  `glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8` — the same package
  versions ardentperf tested — both running PostgreSQL 16.15. Every claim in
  the worked example is measured, not inferred; see
  [`examples/rhel8-to-rhel9-audit-output.txt`](../examples/rhel8-to-rhel9-audit-output.txt).
  The distro-versus-upstream backport check under
  [limitations.md](limitations.md#upstream-tags-are-not-your-distros-glibc)
  was measured on these same two nodes.
- **RHEL9 to RHEL10** (glibc 2.34 to 2.39): full method run, confirmed
  against real nodes running PostgreSQL, not just the source diff; see
  [`examples/rhel9-to-rhel10-audit-output.txt`](../examples/rhel9-to-rhel10-audit-output.txt).
  The empirical lines in that file predate step 5 and are marked as such.

## RHEL7 to RHEL8 — audited, not confirmed

glibc 2.17 to 2.28, the large jump that rewrote the master table. The
source-diff steps were run; the pair was **not** confirmed against real RHEL7
nodes, and no output file is kept for it. RHEL7's `systemd` doesn't boot
under a cgroups-v2-only container host — a limitation of my test environment,
not of the method.

The short version: all three collation templates changed, and 86 of the 310
content-changed locale files have the change inside `LC_COLLATE`. On this
pair essentially everything is affected and the audit is not the interesting
part. Note also that nothing covers the backport question here — see
[limitations.md](limitations.md#upstream-tags-are-not-your-distros-glibc).

---

[Documentation index](README.md) · [The method](method.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
