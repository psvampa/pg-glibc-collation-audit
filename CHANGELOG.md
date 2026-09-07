# Changelog

Findings live in [docs/results.md](docs/results.md). This file records what this tool
used to get wrong, so a reader can tell whether a result they saved earlier
is still trustworthy.

## 2026-09-07 (nineteenth entry)

Three latent false negatives in step 5 and the summary that reads it, closed,
plus a shell error the summary printed on every clean run. **No verdict
moved**, and both published hunk counts hold at 25 and 53. What did change:
seven lines of the RHEL8→RHEL9 worked example now carry the `>>` marker the
documentation promised them, two files join step 5's first tier, and the
summary has a third state where it used to have two.

Every one of these was measured against the three tag pairs before it was
called latent. None fired on a published pair. All four are in the reassuring
direction, which is the direction this tool exists to distrust.

### What it used to get wrong

**The noise filter took C code for a comment.** `is_noise_line` called any
changed line opening with `*` a comment continuation. `*wp = '\0';`,
`*endp++ = '/';` and `*wch = result;` open with `*` and are code. Measured on
the real diffs: ten such lines across the two documented pairs, seven of them
printed in `examples/rhel8-to-rhel9-audit-output.txt` without the `>>` that
[docs/method.md](docs/method.md) and the README say marks every code change.
No whole hunk was lost: with the rule corrected, the hunk totals over
2.28..2.34, 2.34..2.39 and 2.12..2.17 do not move by one, so no dropped hunk
consisted only of such lines. But a hunk whose every changed line writes
through a pointer would have been dropped whole under "comment/licence hunk(s)
filtered", and step 5 would have gone on to say there was no substantive
change. That is the same defect class as the
first entry's inverted rule, in a new place.

**The summary contradicted step 5 when a tracked path vanished.** Step 5
already detected a path present at the old tag and absent at the new one, and
printed "NOT a clean result". But it printed it as prose, without the
"N substantive hunk(s) found" line, and `audit.sh` read its count from that
line with `HUNKS=${HUNKS:-0}`. Absent became zero, and zero is the branch that
prints "Step 5 found no substantive change, so a clean data diff is sufficient
even for the locales step 4 flagged". A renamed `ld-collate.c` would have been
reported correctly by the step and cleared by the summary 300 lines lower.
Structural, not observed: no tracked path has vanished in any pair audited.

**Two entry points were diffed by nobody.** `wcsmbs/wcscoll_l.c` and
`wcsmbs/wcsxfrm_l.c` were in `ENTRY_POINTS`, in neither curated tier, and the
include walk subtracts its entry points from what it reports. Their existence
was checked; their diff was never read. They are the wide-char comparison and
sort-key wrappers — a handful of `#define`s and then `#include` of the narrow
implementation — so a change there moves `wcscoll`/`wcsxfrm` behaviour and
nothing else sees it. Measured over 2.28..2.39: copyright and URL lines only,
so no verdict moves.

**The summary counted with a shell error.** `count_lines` was
`grep -c . FILE || echo 0`. `grep -c` prints `0` *and* exits 1 when nothing
matches, so the fallback printed a second `0`, `[ "0\n0" -gt 0 ]` failed with
`integer expression expected` on stderr, and the summary fell into the else
branch — the right one, by luck. Every run of a pair with no `LC_COLLATE`
change printed that error; `tests/test_wrapper.py` ran exactly such a pair and
did not look at stderr.

### What changed

- A leading `*` is comment text only when followed by a space, the end of the
  line, or the `/` that closes the block. `*identifier`, `*(`, `**p` are code.
- The vanished-path notice is a `!!` block with three-space continuation
  lines, the shape the summary collects and repeats verbatim at the bottom.
- The summary reads step 5 as one of three states: a hunk count; the exact
  clean sentence "No substantive collation code change."; or neither, which is
  **unresolved** — step 4's list stays open and the summary says why. The
  vanished-path variant deliberately never prints the clean sentence. A
  reworded script or a truncated log now lands in the unresolved branch, not
  the reassuring one.
- `wcscoll_l.c` and `wcsxfrm_l.c` are in TIER 1, with the reason recorded
  beside them. Both documented pairs gain two "no substantive change" lines.
  The floor pair `2.12 -> 2.17` goes from 63 to **65** hunks: each wrapper
  contributes the 2012 FSF postal-address change, a licence continuation the
  filter cannot prove is prose. Conservative, and now counted in
  `examples/below-the-floor-2.12-to-2.17.txt` and tied to a test.
- `count_lines`/`count_names` count with `awk`, one number, no fallback.
- Both worked examples regenerated. The only differences are the seven `>>`
  markers and the two new tier-1 lines — checked by rebuilding the abridged
  section mechanically from a fresh run and diffing.

### Two more tests that guarded nothing

The fifth and sixth found in this suite. `assertIn("means", out)` on the
wrapper's same-tag test: the word appears in every step's prose. And
`assertIn('C.UTF-8', out)` on the NOT RUN test: it passed on step 2's own
warning, not on the summary block it claimed to check. Both now assert the
full sentence, on the summary alone.

### What was verified

- Step 5 output before and after on `2.28..2.34`, `2.34..2.39` and
  `2.12..2.17`: the diff is exactly the marker changes and the two new tier-1
  lines (plus the two licence hunks on the floor pair). 25 and 53 hold.
- Reversed, `2.39 -> 2.34` loses `locale/C-collate-seq.c` for real, and the
  `!!` block is asserted on that run. The summary's unresolved branch cannot be
  reached with real tags — forward in time nothing has vanished, and the
  reversed pair still finds 53 hunks — so `test_wrapper.py` stands step 5 in
  with a `python3` shim on `PATH` that prints what the real script prints in
  that state, and asserts "sufficient" is gone and UNRESOLVED is there.
- Mutation checks: restoring `startswith('*')` fails the hunk-of-dereferences
  test; restoring the old `count_lines` inside the real wrapper prints
  `integer expression expected` once on the same-tag pair; restoring
  `HUNKS=${HUNKS:-0}`'s default-to-clean makes the shim test fail.

## 2026-09-07 (eighteenth entry)

### Nothing checked that the documentation's links resolve

Every internal link in the published Markdown, and every `docs/*.md` path that
`audit.sh`, `sql/` and `examples/` name in prose, is now asserted by
`tests/test_published_claims.py` (`EveryLinkResolves`), so CI fails on a
retitled heading or a moved file. **No verdict moved and no output changed**:
every link resolves today, which was measured before the test was written.

### What it used to get wrong

The check existed as a Python snippet in a private working-rules file, run by
hand before a commit when somebody remembered. The seventeenth entry records two
broken links found by such a hand run, one created by retitling the very
section being documented; the eleventh records `examples/rhel8-to-rhel9.sql`
pointing at a README section that had moved to `docs/results.md`. Plain
relative file links -- the majority of the links in the docs -- were never
checked at all. A check that depends on somebody remembering is not a check,
which is the rule the seventeenth entry applied to the per-node ellipsis scan.

### What was verified

Mutation-checked, with a control: retitling one heading in
`docs/limitations.md` fails the test once per link to that heading; breaking
one relative link in `docs/README.md` fails it; making `audit.sh` name a page
that does not exist fails it; an unrelated prose edit stays green. Fenced code
blocks are ignored, so a `#` inside a shell snippet is not a heading, and
inline code is ignored when looking for links, so a link-shaped example inside
backticks is not one -- the first version of this entry tripped that itself.
Each of the three tests also asserts that it found something to check, so a
stale pattern fails instead of passing over nothing.

### Also: one copy of the whitespace-collapsing helper

`flat()`, the helper that makes an assertion on wrapped output mean something,
lived in `tests/test_node_modes.py` alone, so the other seven test files could
not use it without copying it -- and a copied helper is how two helpers drift,
which this repository has already paid for twice. It now lives in
`tests/_harness.py` and `test_node_modes.py` imports it. No test changed
behaviour; the suite is byte-for-byte the same set of assertions.

## 2026-09-06 (seventeenth entry)

The glibc 2.24 version floor was a bug in one function, not a structural limit.
It is fixed, one published number turned out to have gone stale, `LC_CTYPE`
becomes a stated limitation instead of a scope footnote, and a check that
needed somebody to remember it is now wired into the wrapper. **No verdict
moved, and no audited pair's output changed by a byte.**

### What it used to get wrong

`docs/limitations.md` said the method *"breaks silently"* below glibc 2.24 and
closed with *"There is no guard for this."* Both were accurate descriptions of
the symptom and neither located the cause, which was three lines of
`glibc_locale_data.py`.

`collate_block` was built on a regex requiring a newline before `LC_COLLATE`,
so it returned `None` for any file opening with `LC_COLLATE` at byte 0 — which
is exactly how glibc 2.23 and earlier write `iso14651_t1`,
`iso14651_t1_common` and `iso14651_t1_pinyin`, the three highest fan-in files
in the corpus. `copy_graph_from_texts` skips whatever it gets `None` for, so
those three never entered the `copy` graph and every locale inheriting from
them looked unaffected.

The same regex had already been worked around **twice, locally**: `collate_text`
avoided it, then `scan_ellipsis` avoided `collate_block` by calling
`collate_text`. Each fix solved its own caller and left the function broken for
the next. That is the transferable part of this entry — a bug routed around
rather than repaired stays alive for whoever calls it next.

### One published number had already gone stale

This file's own convention is that a saved result may not still be true, and
this is a case of it. `docs/limitations.md` said step 4 reported **2** exposed
locales on `glibc-2.12 -> glibc-2.17`. Re-measured, the pre-fix figure is
**277**: migrating `scan_ellipsis` to `collate_text` had already fixed the
flagging half, leaving only the closure half broken, and nobody revisited the
number. The step 3 figure of 11 was still exact.

### What changed

- **`scripts/glibc_locale_data.py`** — `collate_block` is now built on
  `collate_bounds`, which is line-based and always saw these files. The two
  remaining callers, `copy_targets` and `copy_graph_from_texts`, inherit the
  fix rather than having to know about the trap. The dead regex is gone. It
  also now accepts an unterminated block, running it to the end of the file,
  because `collate_bounds` does: the conservative direction, and it makes the
  two agree instead of disagreeing.
- **`docs/limitations.md`** — section 3 is retitled *"Below glibc 2.24 the
  method rests on one measured pair"* and carries the before/after table. It
  claims what was measured and no more: `2.12 -> 2.17` is one pair, not "any
  pair below the floor". No guard was added, because nothing measured justifies
  one.
- **`docs/limitations.md`** gains a sixth section, **`LC_CTYPE` is not audited
  at all**. It was previously a one-line scope exclusion in two files, which
  reads as "does not apply" rather than "can still cost you an index". A
  functional index on `lower(email)` breaks on a glibc upgrade the same way a
  `COLLATE` index does, and there is no `collversion` equivalent for ctype, so
  PostgreSQL leaves no version trail at all. Read from the tags:
  `UNICODE_VERSION` is 11.0.0 at `glibc-2.28`, 13.0.0 at `glibc-2.34` and
  15.1.0 at `glibc-2.39` — three Unicode versions across the same upgrades this
  project audits for collation. **It is unmeasured in both directions**, and
  the section says so; a green row on this project's table is a statement about
  sort order and nothing else. `LC_NUMERIC`, `LC_TIME`, `LC_MONETARY` and
  `LC_MESSAGES` are named as out of scope for the first time.
- **`audit.sh`** — `flag_algorithmic_ranges.py --locales-dir` runs as steps 9
  and 10, one per node, whenever the locale directories are supplied. It was
  documented as a manual step, run by hand once per node; a check that depends
  on somebody remembering is not a check. Given no directories the summary says
  `NOT RUN` and what that leaves uncovered, by the same absent-is-not-empty rule
  as step 8, and a test ties that heading to the wrapper.

- **`audit.sh` no longer inherits a previous run's warnings.** Found while
  verifying the above, and older than any of it: the warnings block globs every
  `step*.$PAIR.log` in the output directory, and the up-front `rm -f` cleared
  the three result lists but not the logs. So a run given both nodes'
  directories left its notices behind, and the next run of the same pair —
  given no directories — reprinted them as its own, including *"C.UTF-8: built
  from ellipsis ranges on at least one of these nodes"* from a run that read no
  node. The direction was conservative, which is why it survived, but the
  statement was false and the wrapper's own rule is that every file it reads was
  written by this run. The logs for the pair are now cleared with the lists, and
  a test drives it.
- **`tests/_harness.py`** pins `glibc-2.12` and `glibc-2.17` alongside the
  three audited tags, and `tests/test_known_answers.py` gains
  `BelowTheOldVersionFloor`, which asserts the figures above against those
  pinned commits. The numbers this entry publishes had no automated coverage
  when they were written — which is precisely how the previous ones rotted, so
  they are covered now. The floor pair gets its own skip decorator: a
  contributor's clone need not carry two tags no published result depends on,
  while CI fetches every tag and fails on any skip.
- **`examples/below-the-floor-2.12-to-2.17.txt`** — both runs side by side,
  clearly marked as not an audited pair and not a verdict. Its table is tied to
  the same asserted numbers, so a saved example cannot drift from the code
  either.

### What was verified

- `./audit.sh glibc-2.28 glibc-2.34` and `./audit.sh glibc-2.34 glibc-2.39`
  produce **byte-identical** output before and after. From 2.24 on no locale
  file opens with `LC_COLLATE`, so the fix adds reach and moves nothing
  published.
- `glibc-2.12 -> glibc-2.17` end to end: step 3 goes from 11 affected locale
  source files to **278**, step 4 from 277 to **279** (404 to **408** generated
  names). Steps 2 and 5 raised nothing new below the old floor.
- Reverting `collate_block` to the regex fails four tests, including a new one
  asserting a byte-0 template is a root of the graph rather than a dropout.
  Two tests that recorded the old behaviour as a deliberate asymmetry now
  record that there is none.

## 2026-09-06 (sixteenth entry)

`C.UTF-8` stops being a locale this tool can only warn about. **No verdict
moved**, but the basis of one did, and one new fact invalidates a conclusion
people were reasonably drawing.

### What it used to say, and why that was as far as it could go

`docs/limitations.md` opened with *"`C.UTF-8` cannot be audited by this
method"*, and the README footnote said it was *"not auditable by this method at
all"*. Both were true of the **tag diff** and were being read as true of the
project. `localedata/locales/C` exists upstream only from glibc 2.35, RHEL8 and
RHEL9 both ship a backported copy, so for the pair that matters the file is in
neither tag — and no choice of tags fixes that. Step 2 named the locale and
said the clean result meant nothing for it. That was honest and it was all the
five steps could do.

What was missed is that the file is absent from both tags and **present on both
nodes**. Comparing the nodes to each other needs no tag at all.

### What changed

- **`scripts/diff_node_locales.py`** — a new comparison, and step 8 of
  `audit.sh` when both nodes' locale sources are supplied. Node against node,
  no tag in the middle. It reports every known backported locale whether or not
  it differs, names which findings exist at neither tag, and reports locales
  present on only one node. A separate script rather than a flag on
  `diff_distro_locales.py`: that one asks "is the audit reading what this node
  runs?", where the node is authoritative and upstream is the reference; this
  asks "did what the two nodes run actually change?", where both sides are
  authoritative and the verdict is the delta.
- **`flag_algorithmic_ranges.py --locales-dir`** — step 4 over a node's own
  directory, which is the only way it sees a backported locale. It also names
  the locales declaring `codepoint_collation` instead of leaving them among the
  unflagged, because cleared and unexamined look identical on a terminal.
- **`sql/c_utf8_probe.sql`** — the empirical half, and it takes no editing. Its
  corpus is derived from the RHEL8 file itself rather than sampled: the first
  and last code point of every range it declares, the first and last of every
  plane it declares **no** range for, and the UTF-8 length boundaries. 41
  values, asserted as 41 before anything is compared. It carries the **inverted positive control** — for `C.UTF-8`,
  agreeing with byte order is the fix, not the usual sign that a locale was
  never generated — and a `strxfrm` check for the third reading, where tied
  weights are rescued by PostgreSQL's own `strcmp` tie-break.
- The `docs/limitations.md` heading is now *"`C.UTF-8` is invisible to a tag
  diff"*, with all six links to the old anchor updated.

### Measured, 2026-09-06

On the three fixtures — `glibc-2.28-251.el8_10.40`, `glibc-2.34-275.el9_8` and
`glibc-2.39-128.el10_2`, Rocky Linux 8.9 / 9.3 / 10.1, all PostgreSQL 18.6.
`/usr/share/i18n/locales/C` exists on all three: 355, 356 and 366 files copied
off, counts asserted on both sides of the transport.

| | RHEL8 | RHEL9 | RHEL10 |
|---|---|---|---|
| its `LC_COLLATE` | six **ellipsis ranges**, planes 0/1/2/14/15/16, then `UNDEFINED` | `codepoint_collation` | byte-identical to RHEL9's |
| step 4 over the node's files | flagged (5th file) | byte order by construction | byte order by construction |
| `C.utf8` equals byte order? | **no** — 40 of 41 positions differ | yes | yes |
| `strxfrm` key, U+10000 | `ef85b5` (a weight) | `f0908080` (the UTF-8 bytes) | `f0908080` |

So RHEL8 → RHEL9 **changed** — which the README already said, on an empirical
four-code-point test — and now the mechanism is accounted for from source: an
ellipsis range carries no weights, `localedef` computes them, glibc 2.34 took
the Bug 22668 commit that changed exactly that expansion, and on top of it
planes 3 through 13 have no range at all in the RHEL8 file, so those code
points fall to `UNDEFINED`. That is why RHEL8's order is scrambled rather than
shifted, with ASCII at position 31.

RHEL9 → RHEL10 stays 🟢, and **its basis moved**: it rested on ardentperf's
checksum and now rests on both nodes' own files being byte-identical and
byte order by construction, plus a 41-code-point probe returning identical
output on both. That is a structural statement, not a measurement that came out
clean.

The node-to-node comparison also reproduced step 2's answer on both pairs by a
different algorithm — `or_IN`/`sv_SE`, then `ber_DZ`/`kab_DZ`/`th_TH` — which
is what makes its answer about `C` worth citing. And it found two things no
step reports, because they are changes to the *set* of locales rather than to
one: `en_US@ampm` is removed at RHEL9, `aa_ER@saaho` at RHEL10.

### A new fact that invalidates a saved conclusion

**"We are staying on RHEL8, so `C.UTF-8` is fine" was wrong.** Its order also
changed *within* one major: `glibc-2.28-93.el8` (RHEL 8.2,
[RHSA-2020:1828](https://access.redhat.com/errata/RHSA-2020:1828), Red Hat bug
1361965) rewrote those ellipsis expressions and code points above U+10000
gained weights, growing the compiled locale by 5.3 MiB. The node's own
changelog says so: `rpm -q --changelog glibc | grep -i collat` prints *"Fix
C.UTF-8 locale source ellipsis expressions (#1361965)"* on RHEL8, and nothing
at all on RHEL9 and RHEL10 — from changelogs of 158 and 112 entries, which is
what that grep is worth.

Both sides of such an upgrade are upstream glibc 2.28, so the tag pair is
`glibc-2.28..glibc-2.28` and steps 1-5 have nothing to compare. `audit.sh` now
says so when both tags match, instead of reporting a clean everything.

### What this still does not close

The **order** on RHEL8/RHEL9 is not derivable from source, because `localedef`
computes those weights at build time. Node-to-node clears the data half only
and says so in a warning of its own. The compiled locale
(`/usr/lib/locale/locale-archive`) is still not compared, which would be the
strictly stronger check and would have caught that 5.3 MiB directly.

### Four things caught by measuring, and by re-reading what was written

All four are the failure mode this repository exists to prevent, found by
running and reading the thing rather than by planning it.

- The probe's `DROP TABLE IF EXISTS` emitted a notice **only on the first run
  on a node**, so running it twice on one node and once on the other made the
  two outputs differ by a line that has nothing to do with glibc. Notices are
  now suppressed; errors are not.
- The test fixture for the backported `C` was written from the Fedora patch's
  general shape and had one range per plane. The real RHEL8 file has six, and
  the planes it omits are the entire reason the order was wrong. The fixture is
  now the file copied off `collaudit8`.
- **The probe carried that same wrong shape, and was not fixed with the
  fixture.** Its header described one range per plane and its corpus rows were
  labelled `range 3 start` … `range 17 end`, so 22 of the 41 values claimed to
  be endpoints of ranges that do not exist — and those labels were printed in
  both published example outputs, three lines below prose stating the truth.
  The corpus itself was right and is unchanged: those 22 values are the first
  and last code point of every plane the file declares **no** range for, which
  is precisely why they have no weights. Only the labels and the rationale were
  wrong, and a wrong label on correct evidence is how the evidence gets
  misread. Both outputs were regenerated on all three nodes; every measured
  answer came back identical, which is what says this was a labelling fix and
  not a measurement change.
- **A claim in the regenerated example was false on its own data.** It said
  "the FIRST code point of each declared range sorts last (38-41)"; four of the
  five in the corpus do, and `U+E0000` sorts at 27 instead. Corrected to say
  what the output shows and to say plainly that it is not explained. No grep
  could have found that one — it needed reading the table under the sentence.

Two stale generalisations went with them: the step 4 docstring said "RHEL8's
**and RHEL9's** `C` is built from ellipsis ranges" (RHEL9's declares
`codepoint_collation`), and step 8's closing warning asserted the ellipsis
shape on every run, including the RHEL9 → RHEL10 run whose own output reported
`codepoint_collation` on both nodes a few lines above. That warning is now
written from what was actually read, and two tests pin it in both directions.

### A second correction pass, and what the first one broke

The pass above was itself re-read. It had introduced a **code regression** and
five new false statements, which is worth recording plainly: a correction pass
is not self-verifying, and this one was not.

The regression: making step 8's closing warning conditional split it into "is
ellipsis-based" and "is not", and the second branch also fired when the
backported locale could not be compared **at all** — absent from a node, or
present on only one. So a run against nodes without `glibc-locale-source`
printed *"this comparison says nothing about C.UTF-8"* and then closed with
*"the data comparison is the whole story"*. A reassurance printed over an
absence, which is the precise failure this script exists to remove, introduced
while fixing a warning that was merely wrong in shape. There are now three
branches — not-compared, ellipsis-based, neither — the first is checked first
and independently, and three tests pin it.

The five false statements, all introduced by the correction:

- *"RHEL8, RHEL9 and RHEL10 all ship a backported `C.UTF-8`"*, written into
  three files while fixing a narrower imprecision. RHEL10 is glibc 2.39 and
  simply has upstream's copy — as the repo's own step 2 output says
  (`new UPSTREAM at glibc-2.39`) and its own table says (el10's *absent
  upstream* column: `none`).
- *"ASCII sorts at position 31, behind every plane-14-to-16 noncharacter"* —
  false on the data regenerated in the same commit: two plane-15/16 values sort
  *after* ASCII, and none of the three named code points is a noncharacter.
  The identical failure mode the pass congratulated itself for catching.
- *"a file in neither tag"* and *"in no upstream tree at all"*, about the
  RHEL9→RHEL10 pair, whose new tag contains the file.
- Three mutually exclusive *"the only check that…"* claims for one locale.

Also fixed, and left behind rather than introduced: the pre-correction corpus
rationale was still published on the page that sends a reader to the probe; the
probe's "exhaustive" claim did not add up to 41 because `U+0000` is absent
(PostgreSQL `text` cannot hold a NUL) and nothing said so; and one command had
three different documented runtimes. Every test count is now gone from the
documentation rather than corrected — a number that cannot be stated cannot go
stale, and it had already gone stale twice in one day.

### A fourth test that guarded nothing

`dd.warn` wraps at 78 columns, so a phrase of more than a few words is split
across lines — which makes an `assertNotIn` on such a phrase **pass whether the
text is there or not**. Two of this pass's own new tests were vacuous that way
before the assertions were run through a whitespace-collapsing helper, and the
same defect turned out to be sitting in
`test_the_false_blanket_claim_is_gone`, from an earlier entry: it asserted
`'They cannot affect an existing index'` against output that prints *"An added
file"* / *"cannot affect an existing index ONLY IF…"* across two lines. It
would have passed if the false blanket claim came back. Now asserted on
collapsed output, and mutation-checked: restoring the claim fails the test.

That is the fourth test in this suite found to guard nothing. The three earlier
ones are in the tenth entry.

### Not decided here

Whether the summary's node-to-node block should be louder than a `NOT RUN`
line. It is the only thing standing between a clean-looking summary and a
reader concluding `C.UTF-8` was covered, and one line may not be enough.

### The checkable half is now a test

`tests/test_published_claims.py`, an eighth layer needing no glibc clone. It
asserts what a re-read was doing by hand: that the probe's header quotes
exactly the six ranges the real RHEL8 file declares (the fixture *is* that
file, so the two can be compared); that no corpus row is labelled after a range
that does not exist, and that every `(NO range)` plane really is one the header
omits; that the corpus arithmetic adds to 41; that every position and count the
docs state about the published output matches the published output; that the
RHEL9 output really is code point order, which nothing else checked; that the
`NOT RUN` heading the docs quote is still the one `audit.sh` prints; and that
no document states a test count.

Mutation-checked, including a control: seven deliberate corruptions each fail
it, and an unrelated prose edit does not. It would have caught the range-label
defect, both drifted counts and both stale positions on the day they were
written.

What it cannot do is read prose, and it does not pretend to. The judgement
calls — whether a sentence is true of the data under it, whether "the only X"
is still the only X — stay human.

Still not decided: whether the summary's node-to-node block should be louder
than a `NOT RUN` line.

## 2026-09-06 (fifteenth entry)

A documentation correctness sweep. **No source-level verdict moved**, and no
locale changed sides. What moved is which PostgreSQL the published empirical
results rest on, and a stale notice that was telling people the opposite of
the truth.

### The "if you saved an earlier result" notice was wrong about th_TH

**If you saved a RHEL9-to-RHEL10 result on 2026-09-05 or 2026-09-06, re-read
it.** `docs/results.md` and the README both carried a block saying *saved a
result before 2026-09-05 → two verdicts have moved*. `th_TH` moved from
🟡 Unresolved to 🔴 **Changed** on **2026-09-06**, in the thirteenth entry
below. That entry updated the verdict table, the worked example and this file,
and left the notice alone — so the one paragraph whose entire job is to tell
you your saved answer went stale was itself stale, in the reassuring
direction. It now names three verdicts and reads from on or before 2026-09-06.

Indexes on `th_TH` need a `REINDEX` across RHEL9 → RHEL10.

### Both pairs are now confirmed on the same PostgreSQL

The RHEL8-to-RHEL9 empirical confirmation was measured on PostgreSQL 16.15;
everything since the thirteenth entry was measured on PostgreSQL 18.6. Both
records were honest, neither said which era it came from, and the result read
as an error.

Re-measured 2026-09-06 on Rocky Linux 8.9 (`glibc-2.28-251.el8_10.40`) and
Rocky Linux 9.3 (`glibc-2.34-275.el9_8`), both **PostgreSQL 18.6**, both sides
fed byte-identical input built on the node rather than copied in, with line
counts and input checksums asserted equal first:

| Locale | glibc 2.28 | glibc 2.34 | |
|---|---|---|---|
| `sv_SE.utf8` | `va wa Vasa Wasa vind wind` | `va Vasa vind wa Wasa wind` | changed |
| `or_IN` | `ଔ କ ହ କ୍ଷ ଂ ଃ ଁ` | `ଔ ଁ ଂ ଃ କ ହ କ୍ଷ` | changed |
| `ko_KR.utf8` | `가 힢 伽 佳 힣` | `가 힢 힣 伽 佳` | changed |
| `C.utf8` | U+FFFF, U+10FFFF, U+007F, U+07FF | U+007F, U+07FF, U+FFFF, U+10FFFF | changed |
| `zh_CN.utf8` | `伽 假 一 龥` | identical | unchanged |
| `en_US`/`zh_TW` Han boundary | `龦 一 龤 龥` | identical | unchanged |
| `en_US`, `de_DE`, `fr_FR` | identical | identical | unchanged |

Every row reproduced its PostgreSQL 16.15 result exactly. That is the expected
outcome and worth stating rather than assuming: PostgreSQL calls `strcoll`, it
does not implement the order, so a glibc-level finding should survive a
PostgreSQL major version. Each change was confirmed by direct `strcoll` on a
specific pair, not only by a `sort` checksum, and every locale was confirmed
to sort differently from `LC_ALL=C` so none had silently fallen back.

On PostgreSQL 18.6, `collversion` and `pg_collation_actual_version()` read
2.28 and 2.34 respectively for `sv_SE.utf8`, `or_IN`, `ko_KR.utf8` and
`en_US.utf8`, and both return **NULL for `C.utf8` on both nodes** — the blind
spot in `docs/limitations.md`, now confirmed on PG 18 as well as PG 16.

### The restart trap, re-measured on PostgreSQL 18.6

Same numbers, new PostgreSQL, and the instruction no longer hardcodes a
version. On Rocky Linux 8.9 / `glibc-2.28-251.el8_10.40` / PostgreSQL 18.6,
with `glibc-all-langpacks` installed after `initdb`:

| | collations imported | `sv_SE.utf8` present |
|---|---|---|
| `pg_import_system_collations()` alone | 72 | no |
| after `systemctl restart postgresql-18` | +931 (1006 `libc` in total) | yes |

`72` and `+931` are exactly what PostgreSQL 16.15 gave, which is the point:
the mechanism is glibc's `locale-archive` mapping in the postmaster, not
anything PostgreSQL versions. The docs said `systemctl restart postgresql-16`
as an instruction, which is wrong on any other major version.

### Installing langpacks can move your glibc build

Found while measuring the above, and newly documented. `glibc-all-langpacks`
is version-locked to `glibc`, so `dnf` pulls the newest build of both: the
node went from `glibc-2.28-236.el8_9.7` to `glibc-2.28-251.el8_10.40` as a
side effect of installing langpacks. A measurement is bound to the build it
ran on, and this is a way to change that build without meaning to. Re-check
`rpm -q glibc` after installing langpacks.

### ardentperf covers ten locales, which is nine languages plus C.UTF-8

`docs/comparison-ardentperf.md` said "roughly nine languages" in three places
while `docs/results.md` said "all ten of their locales". Both were reaching
for the same fact from different sides. Checked against their repository on
2026-09-06: the set is `de`, `en`, `fr`, `ru`, `ar`, `es`, `ja`, `ko`, `zh`
and `C.UTF-8`. The docs now say it once, precisely.

Confirmed at the same time, because this repository cites it: their RHEL8 and
RHEL9 rows really do run `glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8`,
the same two builds as the confirmation above.

### Smaller corrections

- `docs/requirements.md` said "two of its three layers" need the glibc clone.
  The suite has **six** modules and five need it; only
  `test_pure_functions.py` runs without one.
- `docs/README.md` said `examples/` carried "the confirmation SQL for one"
  pair. It has carried one for each since the thirteenth entry.
- `docs/results.md` said the RHEL9-to-RHEL10 empirical lines all predate step
  5. The `th_TH` line does not — it was re-measured after, and is what moved
  that verdict.
- `examples/rhel9-to-rhel10.sql` still described `th_TH` as "verdict open" in
  the very file whose run closed it.
- `sql/collation_confirmation_template.sql` cited
  `src/backend/utils/adt/pg_locale_libc.c` for the `C`/`POSIX` special case
  without qualification. That file exists only from PG 18; the template
  supports PG 15+, where it is `pg_locale.c`.
- The README never mentioned the distro-versus-upstream check added in the
  twelfth entry, so the entry point described five steps and no distro layer.
  It now shows the `--old-locales-dir` / `--new-locales-dir` invocation.
- `docs/limitations.md` described "hundreds of backports" in
  `glibc-2.28-251.el8`, a build string no `rpm -q` prints, and its intro
  accounted for four of its five items.
- "all ~355 locales" is the glibc 2.34 file count. At 2.39 it is 366. Both
  pairs are published, so the docs now give both.

## 2026-09-06 (fourteenth entry)

### RHEL7 is out of scope

The scope is two pairs: **RHEL8 → RHEL9** and **RHEL9 → RHEL10**. The docs
stated it as a *floor* instead — "the destination must be glibc 2.24 or newer"
— which admits `RHEL7 → RHEL8` and led to it being documented as a third,
unconfirmed pair. RHEL7 has been end-of-life for years. Documenting it bought
nothing and the floor framing kept re-admitting it.

Removed: the `RHEL7 to RHEL8` section of `docs/results.md`, and its row in the
backport table.

Kept, reframed: the glibc 2.24 limit is still real, because nothing enforces it
— point the tool below it and it answers confidently and wrongly rather than
refusing. It is now "Below glibc 2.24 the method breaks silently", a property
of the method, rather than an invitation to audit a third pair. The measured
failure that justifies it (`glibc-2.12 -> glibc-2.17`: step 3 reports 11
affected locales where there are 278) stays, now labelled as a demonstration
rather than an audit.

No code changed. The references to glibc 2.17 in `scripts/` and `tests/` are
prose inside docstrings, not support for that version.

## 2026-09-06 (thirteenth entry)

### The backport check becomes a script, and corrects two of its own numbers

`docs/limitations.md` answered "do the distro's patches touch collation?" with
hand-measured numbers in prose. Nothing re-checked them when a build changed —
and builds change on their own: installing `glibc-locale-source` upgrades
`glibc`, because the two are version-locked, which moved all three test nodes
in the previous entry.

`scripts/diff_distro_locales.py` answers it on demand. It takes a copy of a
node's `/usr/share/i18n/locales/` and reports how many files differ from the
upstream tag and — the part that matters — how many differ inside `LC_COLLATE`.
It reproduces the published result on all three fixtures: 73, 2 and 3 files
differing, **0 inside `LC_COLLATE`** in each.

**A published denominator was wrong.** The page said "73 of 355" for the RHEL8
node. Upstream `glibc-2.28` has **353** locale files; 355 was the node's count.
The two files that make up the difference exist upstream nowhere, so they were
never comparable and belong in the "absent" column, not the ratio. The script
reports compared, differing, inside, no-block, absent-upstream and
absent-on-node as separate numbers that add up.

**One of those absent files turns out not to be a blind spot.** `en_US@ampm` is
Red Hat-only — in no upstream tag from 2.28 to 2.41 — and nothing in the tool
mentioned it. The script holds the node's copy, so it reads it: a pure `copy` of
`iso14651_t1`, which was compared and is identical. Nothing is hidden. `C` is
the opposite: it carries its own tailoring, so it is genuinely unauditable, and
that is now shown mechanically rather than asserted. *(Superseded on
2026-09-06 — see the sixteenth entry. It is unauditable against an upstream
tag, which is what this entry was about, and auditable against the other
node's copy of the same file, which is what that one added.)*

Two design choices were forced by verification rather than taste, and both were
silent-false-negative paths:

- **The block is sliced from `collate_bounds`, not taken from `collate_block`.**
  That regex requires a newline before `LC_COLLATE`, so on a file starting with
  `LC_COLLATE` at byte 0 it returns `None` — measured on `iso14651_t1_common` at
  `glibc-2.17`, the highest fan-in file in the corpus, on the one pair that has
  nothing else covering it. Using it would have filed that file under "no block
  on either side" and reported it as unable to affect sort order.
- **Bytes are compared, not decoded text.** `read_blobs` decodes with
  `errors='replace'`, and hundreds of these files carry non-ASCII: two files
  differing only in a byte that decodes to U+FFFD would compare equal. A test
  now asserts the trap exists and that the script does not fall into it.

The script is **self-checking without a node**, which is what stops it rotting
back into prose. `tests/test_distro_diff.py` materialises one upstream tag as a
stand-in node, compares it against the other, and asserts the same answer step 2
reaches by a completely different route — diff hunks overlapped against old-side
line numbers, versus whole-block equality. They agree exactly on both pairs. The
relation asserted is `step 2's content-changed count == differing + renames`,
because step 2 judges a renamed file by its old path; pair 1 has no rename and
matches outright, pair 2's single rename accounts for its whole delta. Asserting
raw equality would have failed confusingly.

`./audit.sh` runs it too, but only when given `--old-locales-dir` /
`--new-locales-dir` with their build ids. It is not a sixth step of the method:
steps 1 to 5 read the clone alone. The summary's warning glob widened from
`step[12345]` to `step[0-9]*` so the new warnings actually reach the summary —
that glob is precisely why a bolted-on step would otherwise have been dropped
in silence.

Two `!!` warnings it always prints, because a clean result here is easy to
over-read. The first names which layer covers what, rather than implying the
audit ignores glibc's code: this script compares locale **data**; the **code**
is step 5's job and is where Bug 22668 lives; the gap neither closes — a distro
backporting a code change present in neither tag — is what the empirical check
on real nodes exists for. The second: `charmaps/` is not compared, though
`glibc-locale-source` ships it.

Seven guards, seven mutations, each one turning a specific test red.

## 2026-09-06 (twelfth entry)

### The backport gap closes for the second pair

`docs/limitations.md` used to say the distro-versus-upstream backport check was
measured for `RHEL8 -> RHEL9` and **not** for `RHEL9 -> RHEL10`, because that
one needed a RHEL10 node. It has one now, so both documented pairs are covered
analytically as well as empirically.

Comparing every distro locale source in `/usr/share/i18n/locales/` against the
same file at the upstream tag, using the audit's own `LC_COLLATE` block parser
rather than a second implementation of it:

| Distro package | Upstream tag | Files differing | Inside `LC_COLLATE` |
|---|---|---|---|
| `glibc-2.28-251.el8_10.40` | `glibc-2.28` | 73 of 355 | **0** |
| `glibc-2.34-275.el9_8` | `glibc-2.34` | 2 of 356 | **0** |
| `glibc-2.39-128.el10_2` | `glibc-2.39` | 3 of 366 | **0** |

The first two reproduce the earlier numbers exactly, on independently
provisioned nodes. The third is new.

**A claim on this page was wrong and is corrected.** The positive control said
the same method "marks `sv_SE` and `or_IN` as different from `glibc-2.34` and
identical to `glibc-2.28`". `or_IN` is *not* identical to `glibc-2.28`: it
differs by one line in `LC_IDENTIFICATION`, where the distro renamed the
language from "Oriya" to "Odia". Its `LC_COLLATE` block is byte-identical,
which is why it sits among the 73 differing files while contributing nothing to
the zero. The conclusion never depended on it, but the sentence describing the
control did, and a locale file changing is not a locale's sort order changing —
the distinction the whole section rests on.

Two more things worth knowing, both found the hard way:

**Installing `glibc-locale-source` upgrades `glibc`.** The packages are
version-locked and `dnf` takes the newest build of both, so the test nodes moved
from `2.28-236.el8_9.7` / `2.34-83.el9.7` / `2.39-58.el10_1.2` to
`-251.el8_10.40` / `-275.el9_8` / `-128.el10_2` as a side effect of installing
the locale sources. Anything measured before that install is a measurement on
the older build and has to say so.

**`th_TH` still changes on the newer builds.** Re-checked after the upgrade,
because a distro backport between two builds of the same upstream version is
exactly what this section exists to worry about. `เฤ` > `ฮ` at
`2.34-275.el9_8` and `เฤ` < `ฮ` at `2.39-128.el10_2`, identical to the result on
the older pair. The verdict now rests on two build pairs rather than one.

**Still not automated.** The comparison remains a manual procedure recorded as
prose, so nothing re-checks it when a build changes — which, given the paragraph
above, is not hypothetical. `docs/limitations.md` now says how to reproduce it
and says plainly that it is manual.

## 2026-09-06 (eleventh entry)

### th_TH changes after all, and the second pair finally has a script

**A verdict moved. If you saved a RHEL9-to-RHEL10 result before today, `th_TH`
was reported 🟡 Unresolved and it is 🔴 Changed.** Indexes on `th_TH` need a
REINDEX across that upgrade.

The old verdict rested on an empirical sample that was too narrow to reach the
strings that move, and said so — `docs/results.md` asked for a broader check
and gave nobody a way to run one, because `examples/` had a confirmation script
for the first pair and none for the second. That asymmetry was backwards: the
pair with the open question was the one without a tool.

`examples/rhel9-to-rhel10.sql` is that tool, and running it is what moved the
verdict.

The strings are derived from the rule that changed rather than sampled, which
is the difference between this measurement and the one it replaces. The diff
deletes 220 `collating-element` definitions and adds `copy "iso14651_t1"` plus
CLDR tailoring; those 220 are exactly the five Thai leading vowels
(U+0E40..U+0E44) against 44 consonants. A leading vowel is written before its
consonant but pronounced after, so each pair used to be a single collating
element sorting at the consonant's position.

Two code points in the consonant range never had such an element — U+0E24 (ฤ)
and U+0E26 (ฦ), the vowel-like letters — and that asymmetry is exactly where
the order moves:

```
glibc 2.34:  ก เก ไก ไก่ ฤ ฦ ฮ เฤ เฦ      <- เฤ/เฦ dangle after the last consonant
glibc 2.39:  ก เก ไก ไก่ ฤ เฤ ฦ เฦ ฮ      <- each sorts beside its own consonant
```

Confirmed by direct `strcoll` rather than `ORDER BY` alone, so it cannot be a
tie-break artifact: `เฤ` > `ฮ` at 2.34 and `เฤ` < `ฮ` at 2.39, likewise `เฦ`.

Measured on freshly deployed Rocky Linux 9.3 (`glibc-2.34-83.el9.7`) and Rocky
Linux 10.1 (`glibc-2.39-58.el10_1.2`), and re-confirmed unchanged on the newer
builds `glibc-2.34-275.el9_8` and `glibc-2.39-128.el10_2`, so no backport
between those builds moves it — both PostgreSQL 18.6, same vendor, same
image lineage, so the only intended variable is glibc's version. `ber_DZ`,
`kab_DZ`, `ko_KR`, `en_US`, `de_DE` and `fr_FR` were identical on both nodes,
which is what makes the `th_TH` difference readable as a finding rather than as
a broken setup. A positive control confirmed the Thai order differs from `C`
byte order, so no side had silently fallen back to an ungenerated locale.

Note what did **not** change: no source-level verdict moved. Steps 1 to 3
flagged `th_TH` on this pair all along, and the audit was right to flag it. What
was wrong was the empirical note that followed, which read the absence of a
difference in a narrow sample as evidence there was none.

Two smaller fixes alongside: `examples/rhel8-to-rhel9.sql` pointed at a
"Worked example" section of the README that moved to `docs/results.md` in the
documentation split, and the `th_TH` paragraph in
`examples/rhel9-to-rhel10-audit-output.txt` now records the new measurement
instead of the superseded one.

## 2026-09-06 (tenth entry)

### One command, and the handoff that used to be manual

No verdict moved. Both documented pairs give exactly the sets
[docs/results.md](docs/results.md) states — `or_IN`, `sv_SE`, `sv_FI`,
`sv_FI@euro` for 2.28..2.34 and `ber_DZ`, `kab_DZ`, `th_TH` for 2.34..2.39 —
and the five scripts print byte-identical output when run by hand. What
changed is that you no longer have to run them by hand.

`./audit.sh <old_tag> <new_tag>` runs all five steps and ends with a summary:
what to reindex, what still needs an empirical test, every `!!` warning
repeated in full, and what was not decided for you. Previously the only thing
in the repo that ran all five steps was the test suite.

The handoff it removes was a real defect, not just friction. Step 2 printed
the locale names and the user retyped them into step 3. Step 2 now writes them
to `step2_changed_collate.<old>..<new>.txt` and the wrapper reads that. Two
things about the file matter more than the convenience:

- It is named for the version pair. Every other file in
  `$PG_GLIBC_AUDIT_OUT` is pair-agnostic, and this one becomes **argv** for a
  later step. A leftover list from a different pair, fed to step 3 in silence,
  is precisely the bug `filter_lc_collate_changes.py` was rewritten to remove
  when it stopped reading a hardcoded `/tmp` diff.
- It is written whether or not anything was found. Absent and empty are
  different facts: empty means this pair has no `LC_COLLATE` change, absent
  means step 2 never got there. Inferring the first from the second is how an
  error becomes a clean audit.

A latent bug surfaced while automating this. Step 2 judges a renamed file
against its **old** path, and step 3 walks the copy graph at the **new** tag,
where that path no longer exists — step 3 exits 2 on an unknown locale name.
Retyping hid it, because a human reading `aa_ER@saaho -> ssy_ER` would type
the name that exists. Step 2 now maps a renamed file to its new name and says
so. Neither documented pair hits this, so no recorded output changed; on a
pair where a renamed locale's `LC_COLLATE` did change, the old behaviour would
have aborted a valid audit.

The steps' "run this next" hints are suppressed under the wrapper, which is
the only difference between wrapped and hand-run output. That is deliberate:
manual output stays byte-identical, so `examples/` — which is partly
hand-abridged and not mechanically reproducible — remains valid.

Three of the wrapper's guards are unreachable and therefore untested: the argv
name validation, the up-front removal of the files the summary reads, and the
"step 2 wrote no file" check. Step 2 always rewrites its list for the pair
being audited, and `set -e` ends the run before the last one can fire.
Reverting any of the three leaves the suite green. They are kept as defence
against a future refactor and labelled as untested in both `audit.sh` and
`tests/README.md`, rather than covered by tests that would assert nothing —
the failure mode this project has already found three times.

`tests/test_wrapper.py` adds 15 tests, mostly failure modes: a bogus tag must
fail without printing a summary, an empty step 2 must skip step 3 rather than
call it with no arguments, a stale step-3 list must not be summarised, and
`sv_FI` must appear for 2.28..2.34 — it is reachable only through the new
tag's copy graph, so an old/new swap in the wrapper would otherwise produce a
plausible reversed audit at exit 0. The suite is now about 40 seconds, up from
17: wrapper tests run five real steps and cannot be memoised.

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
