# The method

## Why a clean diff is proof

If the source file that defines a locale's collation rules did not change
between two glibc releases, that locale's sort order **cannot** have
changed — provided the code that compiles and compares those rules did not
change either. That's deterministic, not sampled: no need to guess or
brute-force-test every string.

Steps 1 to 3 check the locale data. Step 4 identifies the locales the data
alone can never settle. Step 5 checks the other half of the input, the code.
What you do with those five answers is [the decision
procedure](#the-decision-procedure) at the bottom of this page.

Unfamiliar terms — `copy` graph, blast radius, hunk, tier, ellipsis range —
are in the [glossary](glossary.md).

## The five steps in detail

`./audit.sh <old_tag> <new_tag>` runs all five in order and does the handoffs
for you. Each is also a standalone script, which is what you want to re-run
one step against a hand-picked locale list — the invocations below are those
standalone forms.

**Old first, new second.** The order is not a formality: every step assumes
the second tag is the newer one. Given the pair backwards, the run used to go
to the end at exit 0 with a plausible clean summary — step 4 scanning the
older tag, so locales added in the newer one drop out of its list, and step 2
reporting a locale *deleted* in the real upgrade as an addition it did not
analyse. The audit now asks git which of the two commits is newer, in step 1,
and refuses a reversed pair with exit 2 before printing a single finding.
*Newer* is git's own answer — is one commit an ancestor of the other — and for
two commits on different branches, the glibc release behind each one: a commit
on `release/2.28/master` is a 2.28, whatever its date. Only the release counts,
never what follows it — a point release (`glibc-2.12.2`) or the snapshot tag
that opens master for the next release (`glibc-2.28.9000`) says where inside or
after a release a commit sits, which cannot order two lines off that release.
Commit dates decide nothing, because a release branch carries commits dated
years after the next release. The standalone steps
that take a pair accept `--allow-reverse` for a deliberate backwards read
(each script's `--help`, or its usage line), and then print a `!!` block
saying so; the wrapper has no
such flag, because a reversed audit answers none of the questions on this
page.

The wrapper also runs three checks that are *not* among these five, all of them
needing files off a node rather than the clone — which is why none of them is a
sixth step. They answer different questions:

- **node against its tag** — `scripts/diff_distro_locales.py <tag>
  --locales-dir <path> --build-id <nvr>`, run as step 6 and/or 7 for whichever
  side you supply. Is the audit reading what that node runs? This is the only
  way to see your distro's own patching. A file that differs inside
  `LC_COLLATE` is reported with its reach through the node's `copy` graph.
- **node against node** — `scripts/diff_node_locales.py --old-locales-dir
  <path> --old-build-id <nvr> --new-locales-dir <path> --new-build-id <nvr>
  [--old-tag <tag> --new-tag <tag>]`, run as step 8 when both sides are
  supplied. Did what the two nodes run actually change? This is the only way to
  see a locale the distro **adds** — a file in neither tag, which no choice of
  tags can reach. `C.UTF-8` is that locale, and it is usually the database
  collation in a container. Every differing file is followed by its reach
  through the new node's own `copy` graph — the same closure step 3 takes —
  so a backport to a template reads as the hundreds of locales it moves, not
  as "1 locale(s) differ". The summary carries that count. When you do not
  supply the directories, the summary says `NOT RUN` rather than omitting the
  section.
- **step 4 over a node's own directory** — `scripts/flag_algorithmic_ranges.py
  --locales-dir <path> --build-id <nvr> --supported-tag <tag>`, run as step 9
  and/or 10 for whichever side you supply. Does that node's own locale data use
  ellipsis ranges? Step 4 proper scans the **new tag**, which cannot hold a file
  no tag has, so this is the only way the question is asked of `C` itself. It
  was a manual step until the seventeenth CHANGELOG entry; a check that depends
  on somebody remembering is not a check. Same rule as step 8 when the
  directories are absent: the summary says `NOT RUN`.

None of the three settles the resulting *order*: the weights for an ellipsis
range are computed when the locale is built. `sql/c_utf8_probe.sql` is what
does, for the one locale where nothing else can.

See
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#checking-the-distros-own-patches).

### Step 1 — `scripts/audit-locale-diff.sh <old_tag> <new_tag>`

Clones glibc (full history, blobs fetched on demand — step 5 needs the
history) and, before diffing anything, prints
where that content came from: the commit id behind each tag and the state of
its GPG signature. The clone is a third-party mirror and a git tag is a
mutable pointer, so "I audited glibc-2.39" is a weaker claim than it looks.
An invalid signature aborts; an unverifiable one — no `gpg`, or no key for
that signer — is reported as unchecked and the run continues, since refusing
to run buys no truth.

Then it lists every locale file with *any* change (mostly noise: `LC_TIME`,
`LC_MONETARY`, comments), and gives an explicit `CHANGED`/`UNCHANGED` verdict
for the [collation templates](glossary.md). It also computes each file's
[blast radius](glossary.md) from the [`copy` graph](glossary.md), so a change
to a template that 328 locales inherit cannot read as one line out of 283.

Before any of that it checks that both tags hold a real locale corpus — at
least 200 files under `localedata/locales/`, the same floor the node-reading
checks apply — and refuses otherwise. Steps 2, 3 and 4 apply the same check
on their own. Without it, a tag whose tree lacks that directory produced
"0 files changed", "no locale uses ellipsis ranges" and a zero exit: a clean
result from having compared nothing. The three `git diff` calls also pin
rename detection on, its limit lifted, and colour off, so a user's git
configuration cannot change a count or hide a rename.

### Step 2 — `scripts/filter_lc_collate_changes.py <old_tag> <new_tag>`

Narrows that list to files whose change falls **inside** the
`LC_COLLATE...END LC_COLLATE` block, the only part that can move sort order.

Files added, deleted or renamed between the two tags are reported separately
rather than dropped, and so are the ones with no `LC_COLLATE` block on either
side — named, not just counted, so a transliteration table cannot be confused
with a locale skipped by mistake.

An added file is only harmless if the locale did not exist on the old system,
and an upstream diff cannot establish that, because distros backport. So
`C.UTF-8` gets a warning of its own whenever `localedata/locales/C` is
missing at the old tag. That condition covers two different cases: the pair
where the file is added upstream, and the pair where it is in neither tag.
The warning is all this step can do; the node-to-node check and
[`sql/c_utf8_probe.sql`](../sql/c_utf8_probe.sql) are what settle it. See
[limitations.md](limitations.md#cutf-8-is-invisible-to-a-tag-diff).

### Step 3 — `scripts/resolve_copy_closure.py <tag> <locale> [...]`

Closes a gap in step 2. A locale with no tailoring of its own, that just does
`copy "some_other_locale"`, never shows up in a source diff — its file didn't
change — even though its real sort order changes whenever the locale it
copies does.

This walks the full [`copy` graph](glossary.md) — every `copy` in a file, not
just the first — and adds every locale that inherits from a directly-changed
one. It also maps the result through `localedata/SUPPORTED` to the
[generated names](glossary.md) `locale -a` and `pg_collation` actually show.
A file `SUPPORTED` does not name keeps its source name rather than dropping
out: the mapping is a translation, not a filter, and the written list is
never narrower than the set the step reported.

Note the spelling: `localedef` normalises the codeset when it builds the
locale, so `SUPPORTED` says `sv_SE.UTF-8` while the installed locale,
`locale -a` and `pg_collation` all say `sv_SE.utf8` — and
`COLLATE "sv_SE.UTF-8"` does not exist.

### Step 4 — `scripts/flag_algorithmic_ranges.py <tag>`, or `--locales-dir <path>`

Finds locales whose `LC_COLLATE` uses [range-expansion (ellipsis)
syntax](glossary.md) instead of an explicit per-character weight. Such a
range is expanded algorithmically by glibc's locale compiler (`localedef`) at
build time, not stored in the locale file itself.

If the expansion logic changes between two releases, every character in the
range can get a different weight with zero change to the locale's own source,
so steps 1 to 3 alone cannot prove that locale is safe. The range may sit on
its own line or, more often, inline on a `collating-symbol` line — both
count.

Point it at a node's `/usr/share/i18n/locales/` instead of a tag
(`--locales-dir` with `--build-id`) and it scans that node's own corpus, which
is the only way it sees a **backported** locale. Measured on
`glibc-2.28-251.el8_10.40`, that adds a fifth file: `C`, built from six
ellipsis ranges. It also names the locales that declare
`codepoint_collation` — byte order by construction, immune to any expansion
change — rather than leaving them in the unflagged majority, where cleared and
unexamined look the same.

Four files do this as of glibc 2.34: `ko_KR` (all 11,172 precomposed Hangul
syllables), `iso14651_t1` (the CJK block U+4E00..U+9FA5), and
`iso14651_t1_common` and `i18n` (the constructed Hangul and Han weight
symbols). Between them they are inherited by 331 further locales — `en_US`,
`de_DE`, `fr_FR`, `zh_CN`, `zh_TW`, `zh_HK`, `zh_SG` — so this step closes
its own result over the `copy` graph too, reaching 335 of the 342 locales
that define `LC_COLLATE`.

### Step 5 — `scripts/diff_collation_code.py <old_tag> <new_tag>`

Diffs the glibc *code* that turns locale data into weights: `localedef`'s
collation compiler and the runtime comparison functions. Steps 1 to 4 compare
data; this compares the other half of the input.

It is not hypothetical — the only sort-order-relevant change between glibc
2.28 and 2.34 lives here, not in `localedata/` (see
[the worked example](results.md)).

Comment and licence [hunks](glossary.md) are filtered out, with the filtered
count always shown and `--all` to see everything. The filter only drops what
it can prove is prose: a preprocessor directive, a label, a bare declarator
or a line that writes through a pointer (`*wp = '\0';`) counts as code,
because a hunk dropped here is a hunk nobody reads. It reads the diff's
context lines to know where a comment really ends, and marks prose as code
rather than the reverse when a hunk begins inside one. It also reports any
tracked path that is absent at either tag, since `git diff` over a missing
file is empty rather than an error — and a path present at the old tag and
gone at the new one is a `!!` warning, not a clean result: the summary then
leaves step 4's list **unresolved** instead of clearing it. A path absent at
*both* tags is asked further questions, because "absent at both" is three
different facts: with history at either tag it was renamed away before the
range or lived and died inside it (a `!!`, the same blind spot as a path that
vanishes); with no history at either tag but history somewhere in the clone it
had not been written yet (a note); and with no history at any ref it is a
misspelt name in the lists that decide what step 5 reads, which is a `!!` of
its own.

What step 5 does **not** do is decide. It cannot tell a weight-changing
commit from a harmless one — that judgement is yours, and it is the one part
of this method that requires reading C. See
[limitations.md](limitations.md#step-5-reports-it-does-not-decide).

#### Step 5's third tier is derived, not curated

Its two curated [tiers](glossary.md) are **not** the ceiling of what it
reads. A third tier is derived by walking glibc's own `#include` graph from
the collation entry points — `ld-collate.c`, `strcoll_l.c`, `strxfrm_l.c`,
the wide-char variants and `loadlocale.c` — bounded to `locale/`, plus the
sibling `.c` of every header reached, for the units glibc links rather than
includes.

That list grows on its own as glibc changes, which a hand-written one cannot:
it is what surfaced `linereader.h` and `elem-hash.h`, both changed over
2.34..2.39 and in neither curated tier.

The curated tiers stay because the walk structurally cannot follow a
macro-computed include (`#include WEIGHT_H`, how `strcoll_l.c` reaches
`locale/weight.h`, which does change over that pair) or reach a translation
unit with no header of its own. The walk also does not report its own entry
points, so the two wide-char wrappers (`wcscoll_l.c`, `wcsxfrm_l.c`) are
listed in tier 1 by hand: until they were, they were checked for existence
and never diffed.

## Reading the output

The run ends with an `AUDIT SUMMARY` block: what to reindex, what still needs
an empirical test, every warning repeated in full, and what the tool did
*not* decide for you. Long result lists are written to files under
`$PG_GLIBC_AUDIT_OUT` (default `/tmp/pg-glibc-collation-audit/`) and
referenced rather than inlined, so the summary stays readable.

Two markers carry the weight:

- **`!!`** is a warning that the clean-looking result above it does not cover
  something. The `C.UTF-8` warning in step 2 is one of these, and it fires on
  both documented pairs. The summary repeats every one of them verbatim,
  because a warning that scrolled past 400 lines ago has not been delivered —
  once each, though: three node-reading steps close with the same caveat, and
  printing it three times teaches the reader to skip the section.
- **`>>`** marks the actual code changes in step 5's [hunks](glossary.md).
  The filter that decides reads the diff's context lines too, so a comment
  that opens on a changed line and closes on an unchanged one stops where it
  really stops. Where it cannot know — a hunk that begins inside a comment,
  since the diff does not show where that comment opened — it assumes it does
  not, which marks prose as code rather than the reverse.

The summary also carries the node-to-node block, and this is the one place
where **absent is not empty**: if it says `NOT RUN`, nothing in the whole run
said anything about `C.UTF-8`. If both tags are the same commit — an
intra-major upgrade, RHEL 8.1 to 8.10 — the summary says that too, because
steps 1 to 5 then compare upstream source with itself and can only report
"nothing changed". *The same* is asked of git rather than of the two strings:
`glibc-2.39` and the commit sha the run's own provenance line prints for it
are one commit spelt two ways, and comparing the text let that pair through as
if it were two versions.

One case is left over: two commits on different branches off the same release
— a master commit against a backport branch — or with no glibc tag behind them
the clone can name. Nothing there settles the direction, and saying nothing
would read as having checked, so the summary says which it is:

```
-- Direction of the pair: NOT ESTABLISHED
```

Steps 1 to 4 give you lists. **Step 5 gives you C diffs and does not decide
for you** — it cannot tell a weight-changing commit from a harmless one. If
nobody will read those hunks, treat every locale step 4 flagged as unresolved
and [confirm it on real nodes](confirming-on-a-real-system.md) instead; that
path needs no source reading and is stronger evidence anyway.

## The decision procedure

This is what the five answers add up to.

- **Steps 3 and 5 together** give the real, complete set of affected locale
  identifiers for that version pair.
- **Step 4** says which locales a data diff can never clear on its own.
- **Step 5** says whether that matters for your two versions:
  - If step 5 reports no [substantive change](glossary.md), a clean data diff
    is sufficient even for the locales step 4 flags.
  - If it reports one, every locale step 4 lists needs
    [an empirical test](confirming-on-a-real-system.md) regardless of its
    data diff.
  - If it reports neither — a tracked file vanished between the tags or was
    renamed away before both of them, a tracked path exists at no ref in the
    clone at all, the include walk reached nothing, or the step did not
    finish — step 4's list is unresolved, and the summary says so rather than
    treating silence as clean.

---

[Documentation index](README.md) · [Results](results.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
