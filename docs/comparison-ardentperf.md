# Relationship to ardentperf/glibc-unicode-sorting

This tool is a complement to
[ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
not a replacement for it: they sort ~25 million real strings and checksum the
result across ten locales — nine languages plus `C.UTF-8` — while this
diffs glibc's source across every locale in the tree (355 at glibc 2.34, 366
at 2.39). Check both where they overlap.

## Reading their tables: `glibc` vs `icu`

They report a `glibc` **and** an `icu` engine, and the difference matters.

Between RHEL8 and RHEL9 **every** locale changes under ICU (60.3 to 67, a
full CLDR jump) while only `ko` and `C.UTF-8` change under glibc. `zh_CN` in
particular came out **unchanged** under glibc for this pair when I measured
it on RHEL8 and RHEL9 nodes.

So a `zh` change read off those tables is an ICU result, not a glibc one, and
carries no `REINDEX` implication for a libc collation.

### What clears `zh_CN` is the measurement, not the diff

Worth being precise about what that argument does *not* rest on. `zh_CN` and
`iso14651_t1_pinyin` are both byte-identical from 2.28 through 2.42, but that
proves nothing on its own: the chain ends at `iso14651_t1_common`, whose
`collating-symbol <SAC00>..<SD7A3>` and `<RFB40>..<RFB41>` ranges are
expanded by `localedef` at build time and are exactly what step 4 exists to
flag.

Step 4 does flag `zh_CN`; the evidence that clears it is the measurement.
Earlier versions of this tool missed the inline ellipsis form and cleared it
from source alone — see [CHANGELOG.md](../CHANGELOG.md).

## Where each method is blind

- **ardentperf sorts ~25 million real strings and checksums the result.**
  Broad, empirical, and covering every Unicode code point. It can catch a
  real behavior change from *anywhere* in glibc, including one this tool's
  five steps cannot see — a backported change to the collation *code*, which
  is in neither upstream tag. Its `ko_KR` finding between RHEL8 and RHEL9 is
  what prompted step 5 of this tool, which now root-causes it to Bug 22668.
- **This tool diffs glibc's locale source and proves the negative.** It
  isn't sampled, so it covers every locale in the tree, not the nine
  languages plus `C.UTF-8` that ardentperf's fixed test set covers. Running
  the RHEL9-to-RHEL10 audit found real `LC_COLLATE` changes in `ber_DZ`,
  `kab_DZ`, and `th_TH` (Berber, Kabyle, and Thai), none of which are in
  ardentperf's tested language list, so none of them would show up there one
  way or the other.

  Since 2026-09-06 it also reads the *nodes'* own locale sources, which closes
  the backported-**data** half of the gap above: `scripts/diff_node_locales.py`
  compares the two nodes to each other, so a locale that is in no upstream tree
  at all — the backported `localedata/locales/C`, i.e. `C.UTF-8` — gets a source
  verdict for the first time. What stays outside it is a backported change to
  the code, which remains ardentperf's territory and the empirical check's.

Their set also contains no `sv` or `or_IN`, the two locales this tool finds
for the RHEL8-to-RHEL9 pair, and no `zh_TW`, `zh_HK` or `zh_SG`, so the two
results overlap less than they first appear.

Where they do overlap they overlap tightly: their RHEL8 and RHEL9 rows run
`glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8`, the same two package
builds [results.md](results.md#tested-on) reports this tool's empirical
confirmation on. Checked against their repository on 2026-09-06.

**One row no longer leans on them.** `C.UTF-8` for RHEL9→RHEL10 was 🟢 on their
checksum, this tool having nothing to say about a file in neither tag. It is now
measured here directly — both nodes' copies of that file are byte-identical and
declare `codepoint_collation`, and `sql/c_utf8_probe.sql` returns identical
output on both, down to the `strxfrm` sort keys. Their checksum and this
measurement agree, which is the useful part; the difference is that the verdict
no longer depends on a set this project does not control.

## Which one to use

If your locale is one of the nine languages ardentperf tests, check both:
their result plus this tool's result gives you empirical evidence and a
deterministic proof for whatever this tool can prove. If your locale isn't in
their list, this tool is the only one of the two that says anything about it
at all.

Where the two disagree, the measurement wins — a source diff cannot see a
distro backport.

---

[Documentation index](README.md) · [Known limitations](limitations.md) ·
[Results](results.md)
