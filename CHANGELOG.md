# Changelog

Findings live in the [README](README.md). This file records what this tool
used to get wrong, so a reader can tell whether a result they saved earlier
is still trustworthy.

## 2026-09-05 (ninth entry)

### Two silent paths closed, and one of them by deleting code

The last two of the six. Neither could move a verdict — stated plainly so they
are not read as more than they are. They are unguarded paths, closed because a
silent path is the defect class this repo exists to hunt, not because a result
was wrong.

**A run of five or more dots was not read as an ellipsis range.** `ELLIPSIS_RE`
spelled out the five tokens `locale/programs/linereader.c` accepts, with dot
boundaries at each end. glibc reads `.....` as `....` plus a stray `.` — a real
range — while the pattern matched no alternative and every starting offset then
failed the guards, so the line read as "no ellipsis here". In the one step whose
job is to refuse to clear a locale, that is the wrong direction.

The fix is **smaller than what it replaced**. All five tokens are runs of two or
more dots, and the pattern only has to answer yes or no, so `(?<!\.)\.{2,}`
covers every one and also covers a run longer than any named token. Measured
identical to the old pattern across glibc 2.28, 2.34, 2.39 and 2.42 — every dot
run in the corpus is length 2, so the alternatives were doing no work. The
comment recording the five forms and where they come from is kept: that is the
part that cost something to learn.

**Files with no `LC_COLLATE` block were counted and never named**, and nothing
checked whether the new side had gained one. A file that acquires a block
acquires a sort order, so folding that case in with the files that never had one
would hide a real collation change behind a number. It now has its own verdict,
lands in the changed list, and gets called out.

Measured before writing the guard: across **five pairs from glibc 2.17 to
2.42**, no file has ever gained or lost an `LC_COLLATE` block, and the bucket is
always the same `translit_*` and `i18n_ctype` transliteration tables. That is
precisely why it needed a test rather than a comment — an unguarded path nothing
exercises is one nobody notices when it finally fires. It is now tested by
injection, by handing the classifier two strings.

**Mutation testing caught the same mistake as last time**, in a new place.
Removing the line that folds a gained block into the changed list left all 108
tests green: the verdict was computed correctly and then thrown away, because
only the classifier was under test and nothing checked what the report *did*
with its answer. Deciding what a file is and deciding what to do about it are
different mistakes, so they are now different functions —
`classify_change()` and `partition_verdicts()` — with tests on each. Five mutants
are caught where four were before.

Output is unchanged except for one new line naming the files with no block.
That was the acceptance criterion: every other step is byte-identical, and the
published counts hold at 2 and 3 files touching `LC_COLLATE`, 4 locales with
ellipsis ranges, and 25 and 53 substantive hunks.

## 2026-09-05 (eighth entry)

### CI, and one thing it has to refuse to do

`.github/workflows/tests.yml` runs the full suite on every push and pull
request, against a **freshly cloned** glibc.

Fresh rather than cached, and that is only defensible because of the previous
entry: with each tag pinned to its commit id, a moved tag fails as *a moved
tag*. Without that pin, cloning fresh would turn any upstream change into a red
build indistinguishable from a regression here -- and an ambiguous red build
gets ignored, which is the same failure as a switched-off test suite.

The job **fails if any test was skipped**. The git-backed layers skip by design
when there is no glibc clone, which is correct on a contributor's laptop and
wrong in CI: a broken clone step would otherwise leave the job green with only
the pure-function layer having run. Green-because-nothing-ran is precisely the
reassuring-direction failure this repo exists to catch, and it would have been
easy to ship.

Provenance is printed as its own step, so the commit ids the run actually read
stay in the log.

No badge, deliberately. "Tests passing" would be read as "this audit is
correct", and it is not: the SQL template, the empirical confirmation on real
nodes and distro backports have no automated coverage at all. See
`tests/README.md`.

## 2026-09-05 (seventh entry)

### The audit never checked that it was reading the glibc glibc released

Every result in this repo is stated against `glibc-2.28`, `glibc-2.34` and
`glibc-2.39`. Two things that was quietly trusting, and neither was checked:

- **`github.com/bminor/glibc` is a third-party mirror** of sourceware. The tool
  clones it and diffs whatever it serves.
- **A git tag is a mutable pointer.** If a tag were ever re-cut, the audit would
  read different source under the same name and say nothing.

Nothing here was wrong — the tags resolve to the same commits they always did.
This closes the gap between "the numbers are right" and "the numbers are right
*about the source glibc published*", which are not the same claim.

**Step 1 now prints provenance before it diffs anything**: the commit id behind
each tag and the state of its GPG signature. The glibc release tags are signed.
An **invalid** signature aborts the run. An **unverifiable** one — no `gpg`
installed, or no key for that signer — is reported as unchecked and the audit
continues, because refusing to run on a stock container buys no truth. The
commit id is printed either way, so a reader who cannot verify a signature can
still compare it against a source they trust.

That distinction is the whole reason `verify_tag()` returns a status rather than
a boolean: `git verify-tag` exits 1 for "gpg is missing", "I do not have that
key" and "this signature is forged" alike. Collapsing them loses the only
difference that matters, and a tool that reports "unchecked" the same way it
reports "forged" trains people to ignore both.

**The test suite pins the three tags to their commit ids.** Without it, a moved
tag would surface as `the count is 51, expected 53` — indistinguishable from a
regression in this repo's own code. Now it surfaces as "the tag moved", and the
failure message says outright not to read the known-answer mismatches as a code
regression until that is explained.

**Mutation testing again earned its place**, and again by failing. Three
mutants passed the suite on the first attempt: `verify_tag` reporting `good`
when it could not verify at all, a forged signature not aborting, and `BADSIG`
downgraded to "unchecked". None of them could be caught by a machine that has
no forged glibc tag to hand, so the classifier is now driven with faked git
output. Seven tests were added and all three mutants are caught.

Honest limit: a commit id is a SHA-1. For this purpose that is solid — forging a
commit with a chosen id is not a practical attack — but it is a weaker guarantee
than the signature, which is why signature reporting exists alongside it rather
than instead of it.

## 2026-09-05 (sixth entry)

### There were no tests. There are now: 85, and each one guards a shipped bug

Not a fix — coverage for everything above. This file documents more than twenty
failures, nearly all of the same family: the tool answered "did not change" when
it had not looked. Several were reintroduced once already. Nothing prevented
that.

`tests/`, stdlib `unittest`, no dependencies, ~16 seconds. Three layers:
pure functions (no clone needed), the git-backed helpers, and the five steps end
to end on both pairs against the results the README publishes. Without a glibc
clone the last two skip with a reason and the first still runs.

**Assertions pin behaviour, not wording** — counts and names parsed out of the
output, not whole-text comparison. The printed prose changed three times in a
single session; a suite that fails on a reworded sentence gets switched off, and
a switched-off suite guards nothing.

**Each test cites the CHANGELOG entry it freezes**, in its docstring. The ones
worth naming: the four ellipsis forms that went unmatched (which cleared
`zh_CN`), `copy` files with two targets, the three code hunks the comment filter
swallowed, the generated-name spelling, `check_paths` telling "unchanged" from
"not there", and the Bug 22668 commit that makes `ko_KR` change.

**The suite was validated by breaking things on purpose.** Eight mutants, each
reverting one fix; all eight are caught. That step earned its keep immediately:
reverting `build_copy_graph` to discard the `missing` set left **every test
green**. The test checked `read_blobs_strict` in isolation while nothing asserted
that `build_copy_graph` *called* it — and because that path's blobs always come
from `ls-tree` at the same tag, the difference is invisible without injecting a
missing blob. Two tests now do exactly that. A test that does not fail when its
subject breaks is not coverage, and only mutation showed which ones those were.

`tests/README.md` states what is **not** covered, deliberately and in the file
itself: the SQL template (needs live PostgreSQL on two operating systems, and it
is half the method), the empirical confirmation on real nodes, distro backports,
and any glibc pair other than 2.28/2.34/2.39. "The tests pass" must not be read
as "the audit is correct" — that misreading is the reassuring-direction failure
this repo exists to prevent.

## 2026-09-05 (fifth entry)

### A failed `git` was indistinguishable from "this file did not change"

The one family of bug in this tool that can invert a verdict with nobody
noticing. Everything else here is deterministic; this was not.

Not hypothetical — it happened on this clone, mid-session:

```
fatal: unable to access 'https://github.com/bminor/glibc.git/': Recv failure: Operation timed out
fatal: could not fetch 07100e5ff962c7582baa0157a73f1602bda04fa6 from promisor remote
```

The clone is `--filter=blob:none`, so every run without a reachable promisor
goes through that path.

**Two live faults, both in step 5.** `run_git(..., allow_fail=True)` suppresses
the abort, and git writes nothing to stdout when it fails, so:

- `report_file()` read an empty diff and returned 0 — the file was reported as
  having **no substantive change**, without a word of warning. Demonstrated
  with an invalid revision range: git exits 128 with 0 bytes of output, and the
  function returned 0.
- `check_paths()` got `False` for both tags and filed the path under *"exists
  at neither tag — nothing to read, and nothing to miss"*, printing it as
  harmless. For `locale/programs/ld-collate.c`, a file that exists and whose
  diff was never read.

**Two latent ones, stated without inflation.** `build_copy_graph()` and
`reachable_from_entry_points()` both discarded the `missing` set from
`read_blobs`, which would silently shrink the `copy` graph and the include
walk. Measured: their path lists come from `ls-tree` at the same tag, so
`missing` is empty at 2.28, 2.34 and 2.39 (353/355/366 paths, 0 absent).
**Neither could fire today.** They are fixed because an unguarded invariant is
cheap to close, not because a result was wrong.

While here: the comment added in the previous entry claiming `read_blobs`
"dies on an unreadable blob" was false — it returns the blob in a second value
that the same line discarded. A comment asserting a guarantee that does not
exist is worse than no comment.

**The fix removes a silencer rather than adding code.** `run_git` already
aborts with git's own stderr; three call sites were configured not to let it.
Verified before relying on it: `git ls-tree` exits 0 with empty output for a
path that is not in the tree and non-zero only on a real error, and `git diff`
without `--quiet` exits 0 whether or not there are differences. So in both, a
non-zero exit always means a real failure and never "there is nothing here" —
aborting introduces no false alarms.

A new `read_blobs_strict()` names the invariant for callers whose paths came
from the tree at that same tag, and `run_git` now documents that
`allow_fail=True` is for existence probes only, never for reading content.

**No output changed.** All five steps produce byte-identical output on both
pairs — that was the acceptance criterion, since this only changes what happens
when git fails. The worked examples are untouched for the same reason.

Left alone deliberately: `audit-locale-diff.sh` uses `git diff --quiet`, which
exits 128 on error, and the script reads that as `CHANGED`. Already the safe
direction.

## 2026-09-05 (fourth entry)

### Step 5's file list was the ceiling of what it could see

`diff_collation_code.py` chose what to diff from two hand-written lists,
`TIER1` and `TIER2`. A file absent from them was never read, and "not read"
produced the same output as "did not change" — a clean verdict.

Two files with real changes over **2.34..2.39 (RHEL9 → RHEL10)** were outside
both lists:

```
locale/programs/linereader.h   lr_getc, the tokeniser's character reader
locale/elem-hash.h             elem_hash, the collating-element hash
```

That pair is exactly where it hurts: the RHEL9-to-RHEL10 verdict **clears
`ko_KR`** on the argument that nothing changed ellipsis expansion, and that
argument is made by reading the hunks step 5 prints.

**What changed.** The lists stay, and stop being the ceiling. A new walk
follows glibc's own `#include` graph from the collation entry points
(`ld-collate.c`, `strcoll_l.c`, `strxfrm_l.c`, the wide-char variants,
`loadlocale.c`), and everything it reaches is reported under a new **TIER 3**.
The walk is derived from the source, so it picks up files that become part of
the collation path in future releases with nobody editing anything.

Two bounds keep it readable, both measured rather than guessed:

- It descends only into `locale/`. Unbounded, the same walk reaches 265 files
  and 243 substantive hunks over 2.34..2.39, dominated by `stdio.h`,
  `unistd.h` and `sys/cdefs.h` — correct, and unreadable. Bounded, it reaches
  28 files and finds the collation code.
- A plain directory sweep was rejected for the same reason: it adds 72 and 77
  hunks on the two pairs, including `ld-monetary.c`, `iso-639.def`, `md5.c`
  and `Makefile`, and `locfile-kw.h` alone contributes 20 hunks of a
  gperf-generated hash table. The readable source of those 20 is one line in
  `locfile-kw.gperf`, which is now tracked instead.

**The lists were not replaced, and could not be.** The walk cannot follow a
macro-computed include (`#include WEIGHT_H`, how `strcoll_l.c` reaches
`weight.h`) and cannot reach a translation unit with no header of its own
(`lc-collate.c`, `C-collate.c`, and `coll-lookup.c`, now added). `locale/weight.h`
is one of these **and has a substantive change over 2.34..2.39** — replacing
the lists with the walk would have lost it. Each hand-listed path now carries
a one-line note saying which blind spot puts it there.

**Reported hunk counts rise**: 2.28..2.34 from 8 to 25, and 2.34..2.39 from 48
to 53. Both worked examples are regenerated.

**No verdict moved.** The two newly visible hunks were read, and neither moves
a weight: `linereader.h` is commit 19d4944459, "locale: Fix signed char bug in
lr_getc", which changes which bytes are read out of a source file, not how a
range is expanded — and no glibc locale file contains the `\32` sentinel it
drops. `elem-hash.h` is commit 535e935a28, a tree-wide
`{u}int_fast` → `{u}int32_t` sweep on a length argument, leaving the hash
value unchanged. So `ko_KR` is still cleared for RHEL9 → RHEL10. What changed
is the standing of that verdict: it used to rest on not having looked at these
files, and now rests on having read them.

Also fixed here: if one of the entry points is ever renamed away, the walk
collapses to nothing, which would read exactly like a clean result. The
existing `check_paths` vanished/absent check now covers `ENTRY_POINTS` too, so
a collapsed walk blocks the clean verdict instead of producing one.

## 2026-09-05 (third entry)

### Step 2 called `C.UTF-8` harmless, then said nothing about it at all

Both wrong, in the reassuring direction, for the locale with the widest
exposure of any in this audit.

`localedata/locales/C` arrives upstream only at glibc 2.35. Step 2 classified
it as an added file and printed the whole added-file group under this text:

> *Added at glibc-2.39 (11), not analysed for a change of order — they had no
> previous order to change. **They cannot affect an existing index**, but do
> check them if you plan to start using them.*

RHEL8 and RHEL9 both ship a backported `C.UTF-8`. The locale **does** exist on
the old node, **does** have an order, and that order **does** change (Bug
22668). So on `RHEL9 -> RHEL10` the tool actively told you the opposite of the
truth, and on `RHEL8 -> RHEL9` — where the file is in neither tag — it said
nothing whatsoever. Silence is the worse of the two: the pair where `C.UTF-8`
demonstrably changes is the pair that produced no mention of it.

This matters more than the locale count suggests. `C.UTF-8` is what `initdb`
picks up in most container images, which makes it the database default, which
makes every `text` column without an explicit `COLLATE` ride on it. And
PostgreSQL cannot warn you either: `get_collation_actual_version()` returns
NULL for anything whose name starts with `C.`, so `collversion` and
`datcollversion` stay NULL and no mismatch can fire.

**What changed.** The blanket claim is gone. An added file cannot affect an
existing index *only if* the locale did not exist on the old system, and an
upstream source diff cannot establish that — distros backport. The added list
now states that condition, names `locale -a` on the **old** node as the way to
settle it, and prints each file's generated name so there is something to grep
for. Separately, a named warning fires for `C.UTF-8` whenever
`localedata/locales/C` is absent at the **old** tag — which covers both the
"added in this range" case and the silent "in neither tag" case.

**What did not change.** No verdict moved: the affected-locale sets for both
pairs are the same as before (`or_IN`, `sv_SE`, `sv_FI`, `sv_FI@euro` for
`RHEL8 -> RHEL9`; `ber_DZ`, `kab_DZ`, `th_TH` for `RHEL9 -> RHEL10`), and the
counts of files touching `LC_COLLATE` are unchanged at 2 and 3. If you saved a
result from this tool earlier, its locale list is still right — what was
missing is the `C.UTF-8` caveat beside it.

**What was tried and does not work.** Deriving the distinction from
`localedata/SUPPORTED`. Measured over `2.34 -> 2.39`: `C` is absent from
SUPPORTED at the old tag and present at the new one — behaving exactly like
the genuinely new `tok`, `crh_RU` and `gbm_IN`. Upstream tags do not contain
the information, because the difference is what a distro backports. The list
of such locales is therefore hardcoded, deliberately minimal, and carries only
`C.UTF-8`, which is the only entry measured on real nodes.

## 2026-09-05 (second entry)

### Declared a version floor: the migration's destination must be glibc 2.24+

Not a fix — a documented limit. If you audited a pair whose **new** tag is
glibc 2.23 or older, the result was wrong in the reassuring direction and
still is; the tool now says so in Known limitations instead of pretending
otherwise.

Steps 3 and 4 walk the `copy` graph at the new tag. In glibc <= 2.23 the three
master templates (`iso14651_t1`, `iso14651_t1_common`, `iso14651_t1_pinyin`)
begin with `LC_COLLATE` on the very first byte of the file, and the block
reader requires a preceding newline, so it decides those files define no
collation at all. The graph loses its three roots and the closure collapses.

Measured on `glibc-2.12 -> glibc-2.17` (RHEL 6 to RHEL 7):

| | reported | correct |
|---|---|---|
| step 3, affected locales | **11** | **278** |
| step 4, exposed locales | **2** | **279** |

The 267 names dropped by step 3 include `en_US`, `de_DE`, `fr_FR`, `es_ES`,
`it_IT`, `nl_NL`, `pt_BR`, `ru_RU`, `sv_SE`, `zh_CN` and `zh_TW`. What changes
over that pair is 109 Tibetan code points gaining a collation weight in
`iso14651_t1_common` — every one of those 278 locales inherits it.

Direction matters and is easy to misread: auditing *from* an old system is
fine (`RHEL 7 -> RHEL 8` is correct, its new tag is 2.28). Only auditing
*towards* RHEL 7 or older is out of scope. There is no guard in the code; run
it outside the range and it answers confidently and wrongly.

### Step 5 was not reading the C locale's collation data

`TIER1` gains `locale/C-collate.c` and `locale/C-collate-seq.c`. The second is
`#include`d by `ld-collate.c:2098`, which was already tracked — so the audit
printed the `#include` line and never read the 100 lines of collation sequence
behind it. Both change over 2.34..2.39, the RHEL9-to-RHEL10 pair this repo
ships as a worked example.

`TIER2` gains `locale/loadlocale.c` and `locale/localeinfo.h`, which are how
the compiled `LC_COLLATE` tables are read back and what structures they live
in. Both have substantive changes in every pair measured.

Reported hunk counts rise accordingly: 2.28..2.34 goes from 4 to 8, and
2.34..2.39 from 38 to 48. Both worked examples are regenerated.

### `git cat-file -e` reported files that exist as absent

The existence checks in `audit-locale-diff.sh` and in step 5 used
`git cat-file -e`. On a `--filter=blob:none` clone that has to fetch the blob
to answer, and calls a file that exists ABSENT whenever the fetch cannot
happen — offline, or against a dead promisor. Confirmed with
`manual/intro.texi` at glibc-2.17: `ls-tree` lists it, `cat-file -e` denies it.
Both now ask `git ls-tree`, which reads the tree such a clone always has.

Step 5's absence report is also narrower now. It used to warn for any path
missing at either tag, which fired on `locale/C-collate-seq.c` for
2.28..2.34 — a file that simply did not exist yet — and withheld the clean
verdict over it. It now distinguishes a path that vanished before the new tag
(a rename the audit cannot see: a real blind spot, verdict withheld) from one
absent at both tags (nothing to read and nothing to miss: a note).

### Character repertoire changes are documented as unaudited

`localedata/charmaps/` is an input to `localedef` and no step looks at it.
`charmaps/UTF-8` gains 1632 code points over 2.28..2.34, 1658 over 2.34..2.39
and 5235 over 2.39..2.41. Measured before writing it down: 99 of those newly
added code points, mixed with pre-existing references and sorted on real RHEL8
and RHEL9 nodes, come out in identical order under `en_US`, `sv_SE`, `de_DE`,
`ar_SA` and `zh_CN`. A gap in coverage, not a demonstrated loss.

## 2026-09-05

### Step 4 cleared `zh_CN` and three siblings that it should have flagged

If you saved a `flag_algorithmic_ranges.py` result before this date, re-run
it. Its ellipsis matcher was anchored to the start of a line, so it only saw
a range that occupied a whole line — `ko_KR`'s bare `..` and the one in
`iso14651_t1`. The dominant form in glibc is inline:

    collating-symbol <SAC00>..<SD7A3>  % Hangul syllables (weights constructed)
    collating-symbol <RFB40>..<RFB41>  % first element of Han computed weights

Those lines live in `iso14651_t1_common`, which is reached by 333 of the 342
locales that define `LC_COLLATE` and which carries precisely the
`localedef`-constructed weights step 4 exists to flag. Missing them cleared
**`zh_CN`, `cmn_TW`, `iso14651_t1_pinyin`, `cns11643_stroke`** and
`iso14651_t1_common` itself: at glibc 2.34 the tool reported an exposed set
of 330 source files where it should have reported 335.

The contradiction was visible in the tool's own output — step 1 printed
`333 locales inherit from iso14651_t1_common` while step 4 claimed 330
exposed — and the README leaned on the gap, arguing that `zh_CN` was
unaffected because its file and `iso14651_t1_pinyin` are byte-identical from
2.28 through 2.42. That premise is true and beside the point: it is exactly
the reasoning step 4 exists to refute. The empirical `sort` measurement on
RHEL8/RHEL9 nodes still shows `zh_CN` unchanged for that pair, so the verdict
in the README stands — but it now rests on the measurement, not on a data
diff that could never have settled it.

The matcher now recognises every form `localedef` accepts (`..`, `...`,
`....`, `..(2)..`, `....(2)....`, per `locale/programs/linereader.c`)
anywhere on a line, outside comments. Two of those forms were also simply
wrong before: it looked for `...(N)...` and `..(N)..` with an arbitrary N,
where glibc accepts `....(2)....` and `..(2)..` with a literal 2.

### Step 5 discarded real code as "comment/copyright"

`diff_collation_code.py` classified a changed line as noise unless it
contained one of `; { } = ( )`. That covers most C statements and none of
its declarations, so preprocessor directives, labels and bare declarators
were all filed as comments. Because a hunk is dropped only when every line
in it is noise, whole hunks vanished under the heading "no substantive
change":

| Pair | File | Discarded |
|---|---|---|
| 2.34 → 2.41 | `locale/programs/ld-collate.c` | `+#include "C-collate-seq.c"` |
| 2.34 → 2.41 | `locale/programs/ld-collate.c` | `+#include <array_length.h>` |
| 2.17 → 2.28 | `locale/programs/ld-collate.c` | `-#define NO_FINALIZE` / `+#define NO_ADD_LOCALE` |
| 2.17 → 2.28 | `string/strxfrm_l.c` | `-# define STRCMP strcmp` |

and inside surviving hunks, lines such as `case tok_codepoint_collation:`
were shown without the `>>` marker readers are told to scan for. In a tool
whose zero-hunk output is the verdict "every locale whose data file is
unchanged is genuinely unaffected", that is the one direction of error that
matters.

The test now runs the other way: a line is noise only when it can be shown
to be comment, attribution or licence text, tracking open block comments
within each hunk so continuation lines are still recognised. Verified across
eight tag pairs from 2.17 to 2.41: no hunk containing a preprocessor
directive, label, declarator or keyword is dropped.

The reported counts for the two worked examples changed as a result:
2.34 → 2.39 now finds 38 substantive hunks where it reported 34.

### Nothing ran on a clean machine

`audit-locale-diff.sh` clones glibc with `--no-checkout`, so the clone has no
working tree. Every Python step located that clone by looking for a
`localedata/locales` **directory** on disk, which therefore never existed, and
all five steps died with `could not find the glibc clone [...] Run
scripts/audit-locale-diff.sh first` — on the very run that had just cloned it.
Repository detection now goes through git, so a clone with no working tree
works. Anyone who had a checked-out clone from an earlier version never saw
this.

### Step 5 could not tell "unchanged" from "not there"

`git diff` over a path that does not exist is empty and exits 0, so a
renamed or dropped file in `TIER1`/`TIER2` read exactly like "this file did
not change" and fed the no-change verdict. All ten paths do exist at 2.28,
2.34 and 2.41, so no result was wrong because of this — but a future glibc
rename would have been silent. Step 5 now reports `ABSENT at <tag>`, as
step 1 already did for the collation templates, and withholds the clean
verdict when any path is missing.

### `locale -a` does not spell locales the way the audit printed them

Step 3 printed the raw `localedata/SUPPORTED` entry — `sv_SE.UTF-8` — and
called it "the name `locale -a` and pg_collation show". `localedef`
normalises the codeset when it builds the locale, so the installed locale,
`locale -a` and `pg_collation` all say `sv_SE.utf8`, and
`COLLATE "sv_SE.UTF-8"` fails with `collation ... does not exist`. The README
used the correct spelling in one place and the wrong one in another. Both
scripts now print the normalised form.

### `pg_collation.collversion` can never flag `C.UTF-8`

Not a bug in this tool, but a blind spot the SQL template did not name.
Under the `libc` provider, `get_collation_actual_version()` returns NULL for
`C`, for `POSIX` and for anything whose name starts with `C.` — the
`pg_strncasecmp("C.", ...)` test, present in every branch from PG 14 on
(`src/backend/utils/adt/pg_locale.c` through PG 17, `pg_locale_libc.c` from
PG 18). So `collversion` and `datcollversion` stay
NULL for `C.UTF-8`, and the mismatch queries in the template — like
PostgreSQL's own upgrade warning — can never fire for it. That is the same
locale the README already flags as exposed and un-auditable from source, and
it is the container default. Both the template and the README now say so.

### `pg_import_system_collations()` under-imports without a server restart

New, and found only by running the template on real nodes. Install a langpack
after `initdb`, call `pg_import_system_collations()` as the template says, and
it returns success with a plausible count — while importing only the locales
that existed when the postmaster started. It reads `locale -a` in a fresh
subprocess, which does see the new locales, then validates each with
`setlocale()` in the backend, which resolves against the `locale-archive` the
postmaster already mapped.

Measured on Rocky 8 / PostgreSQL 16.15 with `glibc-all-langpacks` installed
after `initdb`:

| | collations imported | `sv_SE.utf8` present |
|---|---|---|
| `pg_import_system_collations()` alone | 72 | no |
| after `systemctl restart postgresql-16` | +931 (1007 total) | yes |

The README told you to re-run the function after adding a langpack, which is
not enough and fails in the reassuring direction. Both it and the template now
say to restart first. The README also notes the trap ahead of it: minimal
container images ship `/etc/rpm/macros.image-language-conf` with
`%_install_langs en_US`, so `dnf install glibc-all-langpacks` succeeds and
installs nothing but English.

### Verified on real nodes

Everything above was re-checked on freshly deployed Rocky 8 (glibc
2.28-251.el8_10.40) and Rocky 9 (glibc 2.34-275.el9_8) nodes, both running
PostgreSQL 16.15. Same script, byte-identical input, both sides:

| Locale | glibc 2.28 | glibc 2.34 | |
|---|---|---|---|
| `sv_SE.utf8` | `va wa Vasa Wasa vind wind` | `va Vasa vind wa Wasa wind` | changed |
| `or_IN` | `ଔ କ ହ କ୍ଷ ଂ ଃ ଁ` | `ଔ ଁ ଂ ଃ କ ହ କ୍ଷ` | changed |
| `ko_KR.utf8` | `가 힢 伽 佳 힣` | `가 힢 힣 伽 佳` | changed — data file identical, only step 5 finds it |
| `zh_CN.utf8` | `伽 假 一 龥` | `伽 假 一 龥` | unchanged — step 4 flags it, this clears it |
| `en_US.utf8` | `一 伽 假 龥` | `一 伽 假 龥` | unchanged |

`C.UTF-8` was confirmed on both counts at once. Its order **does** change —
sorting U+007F, U+07FF, U+FFFF and U+10FFFF under `COLLATE "C.utf8"` gives
`ffff, 10ffff, 7f, 7ff` on 2.28 and `7f, 7ff, ffff, 10ffff` on 2.34 — and
PostgreSQL stays **silent**: `collversion` and `pg_collation_actual_version()`
are both NULL for it on both nodes, where `sv_SE.utf8` correctly reports 2.28
and 2.34. Both mismatch queries in the template return zero rows for `C.*`.
The containers' own databases run `datlocprovider = 'c'`,
`datcollate = C.UTF-8` with a NULL `datcollversion`, so their default
collation reordered with no signal of any kind — the exact configuration the
README calls out.

The name finding was settled the same way: `pg_collation` has `sv_SE.utf8`
and no `sv_SE.UTF-8`, and `COLLATE "sv_SE.UTF-8"` fails with
`collation "sv_SE.UTF-8" for encoding "UTF8" does not exist`.

### Smaller corrections

- `supported_map()` returned an empty map when `localedata/SUPPORTED` was
  missing, which made every locale print as "not built by default, so
  normally absent from `locale -a`" and wrote an empty `step4` list under the
  heading "full list of generated names". It now fails loudly.
- `inherited_from()` reported a single inherited root, whichever a
  depth-first walk happened to reach first. The affected set was right; the
  `-> reaches X` explanation was not necessarily. It now reports every root.
- Step 4's `step4_exposed_locales.txt` silently omitted locales absent from
  `SUPPORTED`; it now follows the same rule as step 3 and names them.
- Step 4 reported "Locales checked: 355" counting every file, not the 342
  that define `LC_COLLATE`. Step 1 said "Total locale files with content
  changes" for a count that includes additions and deletions.
- The SQL template's temp view is now `CREATE OR REPLACE`, so re-running the
  script in one session works; inventory queries order by name rather than by
  OID; and the header warns that `pg_import_system_collations()` writes to
  `pg_catalog` and needs superuser.

## 2026-09-03 (second entry)

### The SQL template reported nothing for columns using the database default

If you ran `sql/collation_confirmation_template.sql` and it reported no
affected indexes, run it again. On a database whose default collation is a
libc locale other than `C`/`POSIX` — which is the norm, and is what `initdb`
produces in a container from `LANG=C.UTF-8` — it reported **zero** while real
indexes were exposed.

A `text` column declared with no explicit `COLLATE` does not carry a libc
collation. `pg_type` gives `text` a `typcollation` of `default`, so the column
gets OID 100, whose `collprovider` is `'d'` — a pointer resolved at runtime
from `pg_database` by `init_database_collation()`. Every inventory query in
the template filtered `collprovider = 'c'`, so all of those columns fell
outside it. In a typical database that is most text columns.

Measured on a real PostgreSQL 18 instance with 45 databases, all
`libc` + `C.UTF-8`: the template reported **0** exposed indexes where **119**
were exposed, across 19 databases.

The template now asks `pg_database` first, states plainly whether the
database is exposed at all, and treats default-collated columns as in scope
when it is. Every inventory row now names its effective collation, so a row
reads either `sv_SE.utf8` or `database default -> C.UTF-8`. Also added the
missing counterpart to the `collversion` check: `pg_collation` has no row for
the database default, so that mismatch is now read from
`pg_database.datcollversion` via `pg_database_collation_actual_version()`.

Worth stating explicitly, because it compounds the `C.UTF-8` limitation
below: for a `libc` + `C.UTF-8` database this tool used to be blind end to
end — the source diff cannot see `C.UTF-8` (no upstream file before glibc
2.35) and the SQL template could not see the columns using it. The SQL half
is fixed. The source half cannot be, so for that configuration the empirical
comparison is not optional.

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
