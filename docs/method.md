# The method

If the source file that defines a locale's collation rules did not change
between two glibc releases, that locale's sort order **cannot** have
changed — provided the code that compiles and compares those rules did not
change either. That's deterministic, not sampled: no need to guess or
brute-force-test every string.

Five steps. **1 to 3** check the locale data: what changed, whether the
change was inside `LC_COLLATE`, and which other locales inherit it.
**Step 4** identifies the locales the data alone can never settle, because
their weights are computed by `localedef` rather than stored in the file.
**Step 5** diffs that code, which is what decides whether step 4's list
matters for your two versions.

## The five steps in detail

1. **`scripts/audit-locale-diff.sh <old_tag> <new_tag>`** clones glibc
   (shallow, blobs on demand) and, before diffing anything, prints where that
   content came from: the commit id behind each tag and the state of its GPG
   signature. The clone is a third-party mirror and a git tag is a mutable
   pointer, so "I audited glibc-2.39" is a weaker claim than it looks. An
   invalid signature aborts; an unverifiable one — no `gpg`, or no key for that
   signer — is reported as unchecked and the run continues, since refusing to
   run buys no truth. Then it lists every locale file with *any* change
   (mostly noise: `LC_TIME`, `LC_MONETARY`, comments), and gives an explicit
   `CHANGED`/`UNCHANGED` verdict for the collation templates. It also
   computes each file's blast radius from the `copy` graph, so a change to a
   template that 328 locales inherit cannot read as one line out of 283.
2. **`scripts/filter_lc_collate_changes.py <old_tag> <new_tag>`** narrows
   that list to files whose change falls **inside** the
   `LC_COLLATE...END LC_COLLATE` block, the only part that can move sort
   order. Files added, deleted or renamed between the two tags are reported
   separately rather than dropped, and so are the ones with no `LC_COLLATE`
   block on either side — named, not just counted, so a transliteration table
   cannot be confused with a locale skipped by mistake. An added file is only
   harmless if the locale did not exist on the old system, which an upstream
   diff cannot establish because distros backport; `C.UTF-8` gets a warning of
   its own whenever `localedata/locales/C` is missing at the old tag, which is
   both the pair where it is added upstream and the pair where it is in neither
   tag.
3. **`scripts/resolve_copy_closure.py <tag> <locale> [...]`** closes a gap in
   step 2: a locale with no tailoring of its own, that just does
   `copy "some_other_locale"`, never shows up in a source diff (its file
   didn't change) even though its real sort order changes whenever the
   locale it copies does. This walks the full `copy` graph — every `copy` in
   a file, not just the first — and adds every locale that inherits from a
   directly-changed one. It also maps the result through
   `localedata/SUPPORTED` to the generated names `locale -a` and
   `pg_collation` actually show. Note the spelling: `localedef` normalises
   the codeset when it builds the locale, so `SUPPORTED` says `sv_SE.UTF-8`
   while the installed locale, `locale -a` and `pg_collation` all say
   `sv_SE.utf8` — and `COLLATE "sv_SE.UTF-8"` does not exist.
4. **`scripts/flag_algorithmic_ranges.py <tag>`** finds locales whose
   `LC_COLLATE` uses range-expansion (ellipsis) syntax instead of an
   explicit per-character weight. Such a range is expanded algorithmically by
   glibc's locale compiler (`localedef`) at build time, not stored in the
   locale file itself. If the expansion logic changes between two releases,
   every character in the range can get a different weight with zero change
   to the locale's own source, so steps 1 to 3 alone cannot prove that
   locale is safe. The range may sit on its own line or, more often, inline
   on a `collating-symbol` line — both count. Four files do this as of glibc
   2.34: `ko_KR` (all 11,172 precomposed Hangul syllables), `iso14651_t1`
   (the CJK block U+4E00..U+9FA5), and `iso14651_t1_common` and `i18n`
   (the constructed Hangul and Han weight symbols). Between them they are
   inherited by 331 further locales — `en_US`, `de_DE`, `fr_FR`, `zh_CN`,
   `zh_TW`, `zh_HK`, `zh_SG` — so this step closes its own result over the
   `copy` graph too, reaching 335 of the 342 locales that define
   `LC_COLLATE`.
5. **`scripts/diff_collation_code.py <old_tag> <new_tag>`** diffs the glibc
   *code* that turns locale data into weights: `localedef`'s collation
   compiler and the runtime comparison functions. Steps 1 to 4 compare data;
   this compares the other half of the input. It is not hypothetical — the
   only sort-order-relevant change between glibc 2.28 and 2.34 lives here,
   not in `localedata/` (see the worked example). Comment and licence hunks
   are filtered out, with the filtered count always shown and `--all` to see
   everything. The filter only drops what it can prove is prose: a
   preprocessor directive, a label or a bare declarator counts as code,
   because a hunk dropped here is a hunk nobody reads. It also reports any
   tracked path that is absent at either tag, since `git diff` over a missing
   file is empty rather than an error.

   Its two curated tiers are **not** the ceiling of what it reads. A third tier
   is derived by walking glibc's own `#include` graph from the collation entry
   points — `ld-collate.c`, `strcoll_l.c`, `strxfrm_l.c`, the wide-char
   variants and `loadlocale.c` — bounded to `locale/`, plus the sibling `.c` of
   every header reached, for the units glibc links rather than includes. That
   list grows on its own as glibc changes, which a hand-written one cannot: it
   is what surfaced `linereader.h` and `elem-hash.h`, both changed over
   2.34..2.39 and in neither tier. The curated tiers stay because the walk
   structurally cannot follow a macro-computed include (`#include WEIGHT_H`,
   how `strcoll_l.c` reaches `locale/weight.h`, which does change over that
   pair) or reach a translation unit with no header of its own.

Steps 3 and 5 together give the real, complete set of affected locale
identifiers for that version pair. Step 4 says which locales a data diff can
never clear on its own; step 5 says whether that matters for your two
versions. If step 5 reports no substantive change, a clean data diff is
sufficient even for the locales step 4 flags. If it reports one, every
locale step 4 lists needs an empirical test regardless of its data diff.
