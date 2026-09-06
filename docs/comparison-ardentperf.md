# Relationship to ardentperf/glibc-unicode-sorting

This tool is a complement to [ardentperf/glibc-unicode-sorting](https://github.com/ardentperf/glibc-unicode-sorting),
not a replacement for it: they sort ~25 million real strings and checksum the
result across roughly nine languages, this diffs glibc's source across all
~355 locales. Check both where they overlap.

One thing to know before reading their tables: they report a `glibc` **and**
an `icu` engine. Between RHEL8 and RHEL9 every locale changes under ICU (60.3
to 67, a full CLDR jump) while only `ko` and `C.UTF-8` change under glibc, so
a `zh` change read off those tables is an ICU result and carries no `REINDEX`
implication for a libc collation.

## Where each method is blind, and how their tables read

- **ardentperf sorts ~25 million real strings and checksums the result.**
  Broad, empirical, and covering every Unicode code point. It can catch a
  real behavior change from *anywhere* in glibc, including a distro backport
  that no upstream diff would show. Its `ko_KR` finding between RHEL8 and
  RHEL9 is what prompted step 5 of this tool, which now root-causes it to
  Bug 22668.
- **This tool diffs glibc's locale source and proves the negative.** It
  isn't sampled, so it covers all ~355 locales, not the roughly nine
  languages ardentperf's fixed test set covers. Running the RHEL9-to-RHEL10
  audit found real `LC_COLLATE` changes in `ber_DZ`, `kab_DZ`, and `th_TH`
  (Berber, Kabyle, and Thai), none of which are in ardentperf's tested
  language list, so none of them would show up there one way or the other.

If your locale is one of the roughly nine languages ardentperf tests,
check both: their result plus this tool's result gives you empirical
evidence and a deterministic proof for whatever this tool can prove. If
your locale isn't in their list, this tool is the only one of the two that
says anything about it at all.

One thing worth knowing when reading their tables: they report both a
`glibc` and an `icu` engine. Between RHEL8 and RHEL9, **every** locale
changes under ICU (60.3 to 67, a full CLDR jump) while only `ko` and
`C.UTF-8` change under glibc. `zh_CN` in particular came out **unchanged**
under glibc for this pair when I measured it on RHEL8 and RHEL9 nodes, so a
`zh` change read off those tables is an ICU result, not a glibc one, and
carries no `REINDEX` implication for a libc collation.

  Note what that argument does *not* rest on. `zh_CN` and
  `iso14651_t1_pinyin` are both byte-identical from 2.28 through 2.42, but
  that proves nothing on its own: the chain ends at `iso14651_t1_common`,
  whose `collating-symbol <SAC00>..<SD7A3>` and `<RFB40>..<RFB41>` ranges are
  expanded by `localedef` at build time and are exactly what step 4 exists to
  flag. Step 4 does flag `zh_CN`; the evidence that clears it is the
  measurement, not the diff. Earlier versions of this tool missed the inline
  ellipsis form and cleared it from source alone — see
  [CHANGELOG.md](../CHANGELOG.md). Their set also contains no `sv` or
`or_IN`, the two locales this tool finds for the same pair, so the two
results do not overlap as much as they first appear.
