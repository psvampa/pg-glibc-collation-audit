# Changelog

Findings live in the [README](README.md). This file records what this tool
used to get wrong, so a reader can tell whether a result they saved earlier
is still trustworthy.

## 2026-09-03

### `ko_KR` was reported unaffected between glibc 2.28 and 2.34. It changes.

If you ran this audit on the RHEL8-to-RHEL9 pair before this date and did not
reindex `ko_KR`, reindex it.

The audit compared only `localedata/`. The sole sort-order-relevant change in
that version pair is upstream commit `82292c99b2` ("LC_COLLATE: Fix last
character ellipsis handling", [Bug 22668](https://sourceware.org/bugzilla/show_bug.cgi?id=22668))
in `locale/programs/ld-collate.c`, which lands in glibc 2.34 and alters how
`localedef` expands ellipsis ranges. `ko_KR` depends on one, and its own
`LC_COLLATE` is unchanged across the two tags — so no data diff could ever
find it. Step 5 (`diff_collation_code.py`) now covers this.

The earlier empirical check for `ko_KR` came back negative because it used
everyday Korean text. The difference is confined to U+D7A3, the last syllable
of the Hangul block, which real text essentially never reaches. See the
README's RHEL8-to-RHEL9 worked example for the mechanism and a five-string
test that shows it.

### Other false negatives fixed

Each of these could turn a "safe" verdict into a wrong one. A result produced
before this date is only as good as whether it hit one:

- **`flag_algorithmic_ranges.py` matched one ellipsis form of four.** It saw
  `ko_KR`'s bare `..` but missed `iso14651_t1`'s `.. ..;IGNORE;IGNORE;IGNORE`
  — the CJK range inherited by 328 locales — and `...`, `..(N)..`,
  `...(N)...`. Its printed claim that exactly one locale in the corpus used
  range expansion was false.
- **`resolve_copy_closure.py` reported "0 additionally affected" silently**
  when fed step 2's output verbatim, because step 2 prints paths and it
  compared bare names. That dropped `sv_FI` from the RHEL8-to-RHEL9 result.
- **`resolve_copy_closure.py` followed only the first `copy` per file.**
  `om_ET` has two, so a change to `om_KE` never propagated to it.
- **`filter_lc_collate_changes.py` skipped files added in the new tag**
  while still counting them as checked, using the same code path that
  swallowed git failures.
- **`filter_lc_collate_changes.py` read a stale diff.** It expected a
  hand-generated `/tmp/localedata_full.diff` that nothing tied to the tags
  being audited, so a leftover file from an earlier run was analysed as if it
  were the answer.
- **All three Python steps dropped files silently on git failure**, using a
  30-second timeout per `git show` across ~350 calls on a partial clone.
- **`audit-locale-diff.sh` reported "unchanged" as empty output**, which is
  indistinguishable from an error, and treated `iso14651_t1_common` as the
  only high-fan-in template when `iso14651_t1` is the one 328 locales inherit.

### Documentation corrections

- **`zh_CN` was never affected under glibc** for the RHEL8-to-RHEL9 pair. It
  inherits `iso14651_t1_pinyin`, unchanged from glibc 2.28 through 2.42. A
  `zh` change read off ardentperf's tables is an ICU result — ICU 60.3 to 67
  moves every locale — and carries no `REINDEX` implication for a libc
  collation.
- **The `ORDER BY` tie-break in the SQL template was justified wrongly.**
  glibc ties are real (~10% of random pairs under `ko_KR`), but PostgreSQL
  breaks them itself with `strcmp` in both the row and sortsupport
  comparators, in every release from 13 to 18, and abbreviated keys cannot
  bypass it. The added `COLLATE "C"` is a no-op. Where tie order genuinely is
  unspecified is `sort(1)`, which resolves tied lines by input order.
- **The RHEL9-to-RHEL10 notes called `ko_KR`'s data file byte-identical.** The
  file does change in that pair, at lines 6109+, in `LC_MONETARY` and
  `LC_TIME`. Its `LC_COLLATE` block is what is unchanged — which is why step 2
  compares against the block rather than the file.

### Known limitations, now documented rather than implied

- **`C.UTF-8` cannot be audited by this method.** Its source file exists
  upstream only from glibc 2.35, yet RHEL8 and RHEL9 both ship a backported
  `C.UTF-8` and it does change between them.
- **Upstream tags are not a distro's glibc.** A backported collation change is
  invisible to a tag-to-tag diff; the confirmation step on real nodes is the
  only cover.
