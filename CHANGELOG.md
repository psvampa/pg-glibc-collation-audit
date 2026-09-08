# Changelog

Findings live in [docs/results.md](docs/results.md). This file records what this tool
used to get wrong, so a reader can tell whether a result they saved earlier
is still trustworthy.

## 2026-09-08 (twenty-fifth entry)

Which pair you typed is now established before anything is compared: which of
the two tags is the newer commit, and whether the two are one commit spelt two
ways. **No verdict moves and no published number changes** -- `audit.sh`'s
output on the three measured pairs is byte-identical to the commit before this
one.

### What it used to get wrong

**A reversed pair produced a plausible clean audit.** `./audit.sh glibc-2.34
glibc-2.28` ran to the end at exit 0. Nothing in the tool compared the order
of the two tags: the wrapper tested `[ "$OLD" = "$NEW" ]` and
`check_refs` tested only that both refs resolve. Measured on that pair, the
output is the shape of the published `2.28..2.34` run -- step 2 reporting two
files that touch `LC_COLLATE` (`or_IN`, `sv_SE`), step 5 a substantive hunk
count, no `!!` anywhere. What is wrong with it does not appear in the output
at all:

- Step 4 scans the tag it is handed. Reversed it scans the older one, so a
  locale that only exists in the newer tag is not asked the question step 4
  exists to ask. `ckb_IQ` and `mnw_MM` are absent at `glibc-2.28` and both
  `copy "iso14651_t1"` at `glibc-2.34` (read from the pinned clone), so
  reversed they leave the exposed set in silence.
- Step 2 swaps its reassuring bucket for its noisy one. A locale DELETED in the
  real upgrade -- where any index using it fails outright -- is reported as
  "Added ... not analysed".
- Step 3 closes over the older tag's `copy` graph, and step 1's fan-in is
  computed there too.

**The same-tag notice compared text, not commits.** `./audit.sh glibc-2.39
ef321e23c20eebc6d6fb4044425c00e6df27b05f` -- the tag and the commit sha the
run's own provenance line prints for it -- is one commit compared with itself,
and neither the `!!` block nor the summary's "nothing was compared" section
fired: rc 0, and a summary reading "none -- no locale's `LC_COLLATE` changed".
That is the sixteenth entry's false negative reopened by spelling. Same-tag
runs matter because an intra-major upgrade (RHEL 8.1 to 8.2) is two builds of
one upstream release, and `C.UTF-8`'s order moved across exactly such a bump.

**An option without a value exited 1 without a message.** `./audit.sh
glibc-2.28 glibc-2.34 --new-locales-dir` died in `shift 2` under `set -e`,
printing nothing at all -- and the node checks that option exists to enable
are the only ones that can see `C.UTF-8`.

### What changed

`glibc_locale_data.pair_order()` answers the question with a STATUS, not a
bool, because the four cases are not two: `same` (both refs resolve to one
commit, however each is spelt), `forward`, `reversed`, and `undetermined`.
Folding that fourth state into `forward` would be a clean result produced by
not having looked, which is this repository's whole subject.

The direction is git's own answer, asked in this order: is one commit an
ancestor of the other, and if they are on different branches, which glibc
RELEASE does each one descend from (`git describe --tags --abbrev=0 --match
'glibc-[0-9]*'`, read as major and minor), compared as numbers -- `glibc-2.4`
is older than `glibc-2.34`, which no string comparison gets right. Only when
neither question orders the pair is it `undetermined`.

**The release is the first two components and nothing after them**, and both
kinds of suffix were measured getting that wrong while this change was being
reviewed. `glibc-2.28.9000` is the tag one commit after `glibc-2.28` that opens
master for 2.29, and `describe` returns it for every master commit up to 2.29:
read as a version of its own it ranked a master commit above a 2.28 backport
branch, so `origin/release/2.28/master -> glibc-2.28.9000` answered `forward`.
The point releases do it from the other side: master after 2.12 describes as
`glibc-2.12` while `release/2.12/master`'s tip describes as `glibc-2.12.2`, so
`glibc-2.13~20 -> origin/release/2.12/master` answered `forward` and the
reverse was refused. Two lines off one release are now `undetermined`, which is
the honest answer; between two commits on ONE line ancestry has already
answered before any of this is asked.

**Commit dates decide nothing, and the first version of this fix is why.** It
compared `git log -1 --format=%ct` first and treated it as decisive. glibc's
release branches invert that: `origin/release/2.28/master` carries commits
dated years after `glibc-2.34`, and it is an ancestor of nothing on master, so
both of that version's signals answered "not reversed". Measured on the pinned
clone before this shipped: `order glibc-2.34 origin/release/2.28/master` said
`forward`, which is `audit.sh` running the whole audit backwards at exit 0 over
the closest upstream object to what a RHEL8 node actually runs -- and the same
pair in the CORRECT order was refused. Both now answer right. A bare sha is an
input this tool invites, since the provenance line prints one for every tag it
reads.

`git merge-base --is-ancestor` exits 1 for "no" and 128 for "I could not
answer", and the second must not read as the first: anything but 0 or 1 aborts.
That is one of the two `allow_fail` calls here; the other is `describe`, whose
failure leaves the pair undetermined rather than aborting, because "I could not
find the lineage" is not a reason to refuse a pair ancestry may yet order --
but it is never read as ordered either. `nearest_glibc_tag` returns three
things for the same reason: a tag with its release, a tag whose NAME it does
not read as a release (`glibc-2x-tps` in shape -- no tag in the mirror looks
like that today, the odd 2.16-era spellings included), and no name at all. The
detail line says which happened, because "no glibc tag behind it" printed over
a ref whose own name is a tag is a false sentence. It claims no cause for the
third: `git describe` exits 128 for a commit no tag describes, a glob that
matches nothing and a rev the clone cannot read, and what it prints for each is
git's own prose -- three different sentences on the pinned clone with git
2.50.1, and one sentence for all of them in a repository where no tag matches
the glob at all. Parsing that would be a guess, so the sentence says what
describe answered and nothing about why.

`require_pair_order()` decides what to DO about the status, which is a
different mistake and has its own tests at each call site: it refuses a
reversed pair with exit 2 unless `--allow-reverse` is passed, in which case it
prints a `!!` block saying every finding below has old and new the other way
round. It also carries the same-commit notice, which used to live in
`audit.sh` alone -- so a hand-run `diff_collation_code.py <tag> <tag>` ended in
"No substantive collation code change", rc 0, no `!!`: a clean verdict over a
comparison that never happened. Silent on `forward`, which is what keeps the
audited pairs' output unchanged.

Four entry points ask, because a guard routed around locally stays alive for
the next caller: step 1 (`audit-locale-diff.sh`, which now takes
`--allow-reverse` too and asks before it prints a single finding), step 2,
step 5, and the node-to-node comparison, whose two tags decide which node's
names each side's `SUPPORTED` maps. The wrapper does NOT take
`--allow-reverse`: a reversed whole audit answers no question of the method, so
it says to swap the arguments.

`audit.sh` gets the status from the same helper (`glibc_locale_data.py order
--quiet`), so `SAME_TAG` is now commit identity rather than string equality,
and an `undetermined` pair gets its own summary block, `-- Direction of the
pair: NOT ESTABLISHED`, next to the existing one-tag block. A word the wrapper
does not recognise stops the run rather than falling through to the quiet
branch. Every `--*` option now refuses a missing or empty value by name, with
the usage text and exit 2.

### What was verified

- `scripts/acceptance-diff.sh --base main`: **IDENTICAL** on `2.28..2.34`,
  `2.34..2.39` and `2.12..2.17` (544, 1291 and 1788 lines).
- The full suite ran green, no skips.
- Sixteen pairs measured on the pinned clone, each printing the answer this
  entry claims: the three audited and floor pairs forward; the reversed tag
  pair and `glibc-2.34 -> a release/2.28 commit` refused; `a release/2.28
  commit -> glibc-2.34`, `glibc-2.28 -> the release/2.28 tip`,
  `release/2.28 -> release/2.34` and `glibc-2.28.9000 -> glibc-2.34` forward;
  `origin/release/2.28/master` against `glibc-2.28.9000` undetermined in both
  directions, one direction of the same shape at 2.17, and the point-release
  shape at 2.12 undetermined in both directions; `glibc-2.0.5b -> glibc-2.28`
  forward, since a name the tool reads as release 2.0 orders like one;
  `glibc-2.39` against its own sha `same`.
- Mutation-checked, twenty-five mutants, each failing the test named for it:
  the ancestry return in each direction; the "not an answer" abort on a probe
  that exits 128; the release comparison dropped, and the same comparison made
  on the tag NAME instead of its numbers; the release read as the whole dotted
  version instead of its first two components; a tag whose name is not read as
  a release collapsed into the no-name state, and that state's sentence
  reworded into a claim about the lineage; the fall-through to
  `undetermined` turned into `forward`; the refusal itself; the same-commit
  notice; the `undetermined` warning; the wrapper's commit-identity
  `SAME_TAG`, its `NOT ESTABLISHED` block and its unrecognised-status branch;
  each of the four `needs_value` branches separately; step 1's call and step 1
  passing the flag on; step 5's call and step 5 honouring the flag; step 8's
  call; and, for a control this change had to reword, a path absent at both
  tags filed as "no ref ever had it" instead of "not yet written". Control: an
  edit to `pair_order`'s docstring alone leaves the suite green.
- One guard is deliberately redundant and no mutation can show it:
  `nearest_glibc_tag` checks `describe`'s exit status, and then that it printed
  a name at all, and both end in `None` -- so removing the first changes no
  answer. What is guarded is the outcome: such a pair is left `undetermined`,
  which has its own mutant above. `tests/README.md` records it.
- Three tests were found to be decoration -- green under a mutation that
  removed exactly what they claimed to guard -- and fixed. The wrapper's
  `undetermined` test drives a shim that replaces the very subcommand that
  warns, so it could not see the warning disappear -- a step-level test does
  now. Step 5's refusal and step 8's guard had no test at all: deleting either
  call left the whole suite green. And the option-value test covered one branch
  of four, so a mutation to any of the other three stayed green; it covers all
  four now, with a missing value and an empty one each.
- Read from the pinned clone with `GIT_NO_LAZY_FETCH=1`: `ckb_IQ` and `mnw_MM`
  absent at `glibc-2.28`, present at `glibc-2.34`, both `copy "iso14651_t1"`
  inside `LC_COLLATE`.
- By hand: `./audit.sh glibc-2.34 glibc-2.28` exits 2 with no `AUDIT SUMMARY`,
  and `./audit.sh glibc-2.39 ef321e23...` says "nothing was compared".
- `false-negative-reviewer` on the diff five times, each pass reading the
  corrections of the one before with a fresh context. First: the date-first
  ordering described above, step 5's refusal and step 8's guard with no test at
  all, and the silent `same` at the Python call sites. Second:
  development-snapshot tags read as releases. Third: the point releases doing
  the same thing from the other side, and a detail line that called a tag it
  could not parse unreachable. Fourth and fifth: nothing in the defect class --
  which is what "done" looks like -- and, each time, a sentence claiming more
  than the code knows, including a measurement of mine taken on the wrong
  corpus. Every finding carries the measurement that produced it, and every one
  has a test.
- `doc-sweep` four times -- among what it caught: a header comment claiming
  that one commit spelt two ways is refused when it is run, an "either signal
  is enough to refuse" that the rewrite above falsified, the count of measured
  pairs in this very list, a summary heading that said "one tag" over a tag and
  its own sha, and an `import time` left behind by the ordering this entry
  abandoned.

## 2026-09-08 (twenty-fourth entry)

Step 5's noise filter reads the diff's context lines, so a comment ends where
it really ends; a tracked path that is absent from both tags is asked whether
it ever existed; and a walk that reached nothing is no longer a clean result.
**No verdict moves** -- both audited pairs still have code to read, and the
same locales need the same tests. Two published hunk counts do move, by one
each: **25 -> 24** on `2.28..2.34` and **53 -> 52** on `2.34..2.39`. The floor
pair holds at 65.

### What it used to get wrong

**The filter that decides which hunks are code read every third line of the
comment it was tracking.** `git diff` prints three lines of context around each
change; `split_hunks` kept only the `+` and `-` lines, and `classify_body`
carried its open-comment state across that gap. A comment that opened on a
changed line and closed on a context line stayed open for the rest of the hunk,
and every changed line after it was marked as prose -- eight lines of
`charmap_find_value (charmap, &lrb.buf[startidx], ...)` in
`locale/programs/linereader.c` over `2.34..2.39`, printed with no `>>` on a
page that tells the reader to scan for exactly that marker. The reverse
happened too: a comment that opened on a context line made its changed
continuations print as code, and two hunks that are comment on both sides --
`localedef.c @@ -226,7 +232,8 @@` and `strcoll_l.c @@ -104,7 +103,7 @@` --
counted as substantive, which is where the two counts above come from.

Neither published pair lost a whole hunk to this, but the shape that would is
ordinary: `-/* old comment` / `+/* new comment` / ` still the comment */` /
`+  new_code ();` filters as "comment/licence hunk(s) filtered", and a step 5
with nothing left prints "No substantive collation code change" -- which
`audit.sh` reads as its clean verdict and turns into "a clean data diff is
sufficient even for the locales step 4 flagged". That is the false negative
this repository exists for, with the mechanism the first one had.

**A changed line whose own content began with `++` or `--` was thrown away.**
`split_hunks` dropped anything starting with `+++`/`---` to skip the file
headers -- and `+` plus `++idx;` spells `+++idx;`. The headers are not in a
hunk body to begin with, so the filter is gone: a body now ends where the next
file's `diff --git` line starts, which is structural, and `\ No newline at end
of file` no longer truncates the rest of a hunk either. No line in the five
pinned tags had this shape, so nothing published changes; a discarded line is
one the filter never sees, and a hunk whose survivors are all comment is
dropped whole.

**A tracked file that was in neither tag was called harmless.** `check_paths`
filed every such path under "nothing to read, and nothing to miss", which is
true of `locale/C-collate-seq.c` below glibc 2.35 and false of a file renamed
away before the older tag: for that one the audit reads nothing, `git diff`
reports no error, and the step goes on to its clean sentence. The two are now
separated by asking `git log` for the path's history -- none means it had not
been written yet (a note), some means it was renamed away (a `!!` warning, and
the clean sentence is refused). Which refs that question covers is itself two
of the corrections below. Over the three audited pairs
`locale/C-collate-seq.c` is the only path absent at both tags and it comes back
not-yet-born, so no output moves; `locale/xlocale.h`, deleted before 2.28, is
the other shape and is what the test uses.

**A `/*` inside a string literal or a `//` comment opened a block comment.**
The state was decided by the last `/*` or `*/` on the line, so `x = f ("/*");`
and `// see /* below` left it open, and every changed line after them was
marked as prose. A state wrongly OPEN is the direction that hides code, and
reading the context lines triples the number of lines that can do it, so the
line is scanned rather than searched: strings and `//` are skipped outside a
comment, and inside one nothing else is special. No line in the three pairs
has that shape, so no count moves.

**A hijacked `git diff` was "no substantive change" for every file.** The diff
is asked for with `--no-ext-diff --no-textconv --no-color -U3
--inter-hunk-context=0 --diff-algorithm=myers` now, one flag per way a config
this run does not control emptied it or moved the number it reports:

- `GIT_EXTERNAL_DIFF` in the environment -- which beats any config this run
  pins -- or `diff.external`. With `/usr/bin/true`, all 39 files step 5 diffs
  on `2.28..2.34` read as unchanged and the step printed its clean sentence at
  exit 0, with six hunks in `ld-collate.c` alone -- two of them substantive.
- A `diff.<driver>.textconv` reached through the user's `core.attributesFile`.
  `--no-ext-diff` does not disable textconv, and a textconv that empties both
  sides leaves an EMPTY diff -- so the hunk-less guard below never sees it
  either. Measured with `* diff=nul` and `textconv = /usr/bin/true`.
- `color.diff`, which beats the `color.ui=false` this tool already pinned --
  the more specific setting wins. With `color.diff.frag = normal` the hunk
  headers stay plain, so they still match, and every body line begins with an
  escape: each hunk came back EMPTY, empty is all-noise, and step 5 printed
  its clean sentence over the pair that carries Bug 22668. `split_hunks` now
  also refuses a body line that is neither diff content nor the next file's
  `diff --git`, instead of ending the hunk there quietly.
- `diff.interHunkContext`: at 50 two nearby changes merge into one hunk and
  the count reads 31 over `2.34..2.39` instead of 52, with the same 733 `>>`
  lines -- nothing hidden, the same drift. Pinned with
  `--inter-hunk-context=0`.
- `diff.algorithm`: `patience` and `histogram` pair the same changed lines
  into different hunks. The count holds at 52, so the count alone would not
  notice -- but the marked lines go 733 to 731. Pinned with
  `--diff-algorithm=myers`, and the test asserts the marked lines, not the
  count.
- `diff.context`: 0, 1 and 2 give 106, 76 and 61 hunks over `2.34..2.39`
  instead of 52. Not the reassuring direction -- but a published number must
  not move with a reader's config, and at zero context the comment tracking
  this entry is about is blind again. `GIT_DIFF_OPTS` is applied AFTER the
  command line, so `-U3` on the argv does not win it; `run_git` drops that
  variable for every step.

Separately, output that is not empty but holds no hunk -- `Binary files ...
differ`, or anything this parser does not understand -- is an error naming the
file, because the file DID change and nothing read the change.

**Step 1 took an external diff's word for whether a collation template
changed.** `git diff --quiet` ignores a `diff.external` helper -- unless
`diff.trustExitCode` or `GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE` is set, and then
the helper's exit code IS the verdict. Measured on `localedata/locales/sv_SE`
over `2.28..2.34`: exit 1 plain, exit 0 hijacked, which step 1 prints as
`UNCHANGED` for `iso14651_t1` and the two templates beside it -- the files
328 locales inherit from. The probe carries `--no-ext-diff` now. Both audited
pairs have all three templates unchanged, so no published verdict moves.

**"Not yet written" was decided by asking one tag, with history
simplification on.** A path added and removed INSIDE the range has no history
at the older tag, so it was filed as not-yet-born and passed over in silence --
measured with `posix/spawnattr_tcgetpgrp.c` over `2.34..2.39`, added by
`342cc934a3` and removed by `6289d28d3c`. And `git log -- <path>` with default
simplification drops a path that lived and died on a side branch that was later
merged (reproduced in a fabricated repository). Both tags are asked now, with
`--full-history`; all three real answers are unchanged.

**A misspelt path in the curated lists read as "not yet written, nothing to
miss".** The lists that decide what step 5 reads are hand-written, and a name
in them that matches nothing has no history at either tag -- so the split
above filed it under the benign half and the run went on with a smaller
corpus. Measured with `ld-collate.c` spelt `ld-colate.c` in `ENTRY_POINTS` and
`TIER1`: `2.28..2.34` reported **6** substantive hunks instead of 24 and a
coverage of 8 files instead of 27, the Bug 22668 hunks gone, no `!!`
anywhere, exit 0. "Not yet written" is now asked once more against every ref
in the clone: a path no ref has ever carried is a fourth reason the clean
sentence is refused, and its own `!!` block.

**A shallow clone answered "not yet written" for a file that was renamed
away.** The absent-at-both split above asks `git log -1 <tag> -- <path>`,
and on a `--depth` clone that exits 0 with empty output for every path whose
last commit is beyond the boundary -- the half of the answer that means
nothing to miss. Measured on a depth-1 clone of this repository's own clone:
`locale/xlocale.h` came back not-yet-born and the step printed its clean
sentence. A shallow clone is now refused by name, and so is an answer that is
neither `true` nor `false`: `--is-shallow-repository` dates from git 2.15, and
an older `rev-parse` echoes an option it does not know and exits 0 -- which is
not `true`, so the guard would have been off with nothing said. The clone this
tool makes is `--filter=blob:none` and never shallow; a `--repo` pointing
elsewhere is the case.

**A collapsed include walk printed the clean sentence.** With
`ENTRY_POINTS` pointed at paths that do not exist -- what a rename before both
tags of a future pair produces -- the step reported "Coverage: 0 file(s)
reached by the include walk", "No substantive collation code change." and no
`!!` at all, at exit 0. A walk that reached nothing is now one of four reasons
the clean sentence is refused, printed as its own `!!` block and listed under
"This is NOT a clean result:". The reasons are collected in one list rather
than tested one at a time, so a fifth cannot leave the clean branch reachable
by accident. `audit.sh`'s unresolved branch and `docs/method.md` name all four.

### Smaller corrections

- `docs/method.md` and `audit-locale-diff.sh`'s own progress line called the
  clone "shallow". It is `--filter=blob:none --no-checkout`: partial, with the
  full history — which is what the absent-at-both split needs, and what the
  new refusal above insists on. The page described the clone its own step 5
  would now reject.
- `docs/requirements.md` said the suite pins "the three tags". It pins five;
  the floor pair has been pinned since the seventeenth entry.
- `docs/results.md` argued the RHEL9-to-RHEL10 `ko_KR` verdict from "the two
  tier-3 hunks". TIER 3 prints four over that pair; two of them bear on the
  verdict, which is what the example beside it already said.

### What was verified

- Step 5 before and after on the three pairs: the only differences are the two
  hunks that stopped counting, the eleven lines whose `>>` moved -- eight
  gained in `linereader.c`, three lost inside those two hunks -- and the
  reworded absent-at-both note. `24`, `52` and `65`, each tied to a test.
- Mutation-checked: twenty-five guards, twenty-five mutations, each failing
  the test named for it -- the context lines discarded in `split_hunks`; read but not
  advancing the state; the `+++`/`---` filter restored; `rfind` back in
  `_comment_open_after`, and separately back at the CHANGED-line call site,
  which the first version of that test did not cover; the renamed-away split
  dropped; the never-carried split dropped; each of the four blockers dropped
  in turn; `--no-ext-diff` and
  `--no-textconv`, `--no-color`, `-U3`, `--inter-hunk-context=0` and
  `--diff-algorithm=myers` dropped;
  `GIT_DIFF_OPTS` left in the environment; step 1's template probe taking the
  helper's word; `--full-history` dropped; the hunk-less `die` removed; the unexpected-body-line `die`
  turned back into a `break`; `absent_at_both` asking only the older tag; the
  shallow refusal removed; and its "neither true nor false" arm removed. Controls that must
  NOT fire: a comment that opens on a context line stays prose, a real comment
  after a string still opens, `locale/C-collate-seq.c` absent at both tags
  still gives a clean result on `2.28..2.28`, and the full clone is not
  refused as shallow.
- `scripts/acceptance-diff.sh --base main`: DIFFERS on all three pairs, and
  every difference is one of the three above. `2.28..2.34`: the count in three
  places, `localedef.c` 12 -> 11 substantive and 3 -> 4 filtered, and the
  comment-only hunk that no longer prints -- it carried a `>>` on a line of
  prose. `2.34..2.39`: the count in three places, `strcoll_l.c` 2 -> 1
  substantive, and the eight `charmap_find_value` lines that gain their `>>`.
  `2.12..2.17`: the reworded absent-at-both note alone; its 65 hunks are
  untouched.
- Both worked examples regenerated, and their three copies of each count are
  now tied to `docs/results.md` by a test.
- Every entry point is asserted to be in a tier as well: the walk subtracts its
  entry points from what it reports, which is how two of them went undiffed
  once already.
- `false-negative-reviewer` on the diff, six times, each pass reading the
  corrections of the one before. First: the `/*`-in-a-string, external-diff,
  shallow-clone and untested-vanished-blocker items above. Second: textconv,
  the changed-line call site whose test would have stayed green, and the
  shallow answer that is neither true nor false. Third: `color.diff`, the
  one-tag history question, `GIT_DIFF_OPTS` over `-U3`, history
  simplification, and an over-claim in `docs/method.md` about which way the
  filter errs. Fourth: step 1's template probe, `diff.interHunkContext`, and
  `--full-history` as a guard with no test. Fifth: the misspelt path above,
  and two records that did not match the runs behind them. Sixth: no false
  negative left in the class -- `diff.algorithm`, two negative assertions that
  could no longer fail, and a stale docstring. Every finding
  carries the measurement that produced it, and every one has a test.

## 2026-09-07 (twenty-third entry)

The list every step writes is the set that step reported, the node scan
declares what it found for a distro-backported locale instead of leaving the
summary to infer it from silence, and a `copy` target spelled in glibc's
symbolic notation is the locale it spells. **No verdict moves** -- the two
audited pairs print the same names, and the pair below the floor, which carries
no verdict, gains two: `ky_KG` and `uk_UA`, cleared until now.

### What it used to get wrong

**The only list this tool publishes as the "full list" left `C.UTF-8` out of
it.** `examples/rhel8-to-rhel9-audit-output.txt`, step 9 over
`glibc-2.28-251.el8_10.40`, reported 334 exposed source files, flagged `C` as
ellipsis-based at the top of that same step's output, and wrote a file of 475
names with no `C` in it. A reader who built the empirical set from that file -- which the line above
it calls the full list -- tested everything except the locale this project's
first false negative was about. The mechanism was one expression,
`write_list(out_name, generated or exposed)`: mapping the exposed set through
the tag's `localedata/SUPPORTED` is a translation, and every name the tag could
not translate fell out of the result instead of passing through. `C` is in
neither of this pair's tags' `SUPPORTED`; upstream added
`localedata/locales/C` at 2.35, and by 2.39 `SUPPORTED` carries
`C.UTF-8/UTF-8`. The same expression sat in `resolve_copy_closure.py`,
where step 3 dropped `el_GR@euro`, `i18n`, `iso14651_t1`, `iso14651_t1_common`
and `iso14651_t1_pinyin` from the list the summary counts on the floor pair.

**A `copy` target written in symbolic notation was a locale nothing matched.**
`localedata/locales/ky_KG` and `uk_UA` say
`copy "<U0069><U0073><U006F><U0031><U0034><U0036><U0035><U0031><U005F><U0074><U0031>"`,
which is `iso14651_t1` to `localedef` -- `locale/programs/linereader.c` decodes
`<U....>` wherever it reads a string. The copy graph kept the escaped spelling,
found no such key, and `inherited_from` treats an unknown target as a leaf, so
both locales looked like files that copy nothing reachable. `iso14651_t1` is
ellipsis-based at every tag of the floor pair, so both were exposed and both
were reported clear: step 3's closure said 278 where it is 280, and step 4's
exposed set 279 where it is 281. Two files in the corpus do this and both are
below the old version floor, so no audited pair and no published verdict is
affected -- but "no audited pair" is where the last one was found too. Caught
by the guard added in the same change, which reported one unresolvable `copy`
target on that pair and named it.

**A `copy` the walk could not follow ended in the same sentence as a copy
resolved to a clear file.** `inherited_from` returns a leaf for a target the
corpus does not contain, so "its order is whatever it inherits" was printed
over a locale whose order had not been read at all. Step 4 now names every
dangling target, names what reaches one, and puts those locales in its list
under the spelling `locale -a` shows -- and names the file it wrote them to,
which that path did not do, so the block promised a list and pointed at
nothing. On the no-ellipsis path it no longer says "steps 1-3 are sufficient"
over them. A corpus out of which not one file
defines `LC_COLLATE` is refused outright, for the same reason: that is a reader
problem, and every sentence after it would be the cleanest this step prints.
Zero dangling targets at 2.28, 2.34 and 2.39 and on the three RHEL fixtures
once the symbolic spellings decode, so this is the net under a corpus that is
not the closed source a node built from.

**A fact about the tag was printed as a fact about the node.** Those names were
labelled "not in SUPPORTED (templates, not built by default)" in the scan of a
node's own `/usr/share/i18n/locales/`, where the tag's `SUPPORTED` decides
nothing. Measured 2026-09-07 on the three fixtures: no Rocky 8, 9 or 10 node
has `/usr/share/i18n/SUPPORTED` and `glibc-locale-source` installs none, so the
mapping can only come from a tag -- and `collaudit8`
(`glibc-2.28-251.el8_10.40`) builds 867 locales with `C.utf8` among them, which
`glibc-2.28`'s `SUPPORTED` does not list. The tool called a locale the node
builds, and runs its databases on, a template that is not built.

**The summary said nothing about `C` when `C` was neither ellipsis-based nor
`codepoint_collation`.** `audit.sh` decided that line with two greps -- is `C`
in step 9's ellipsis list, else does its codepoint line name it -- and printed
no line at all when both answers were no. Nothing is also what it prints when
the step never looked. A `C` present with explicit weights, a `C` that only
copies another locale, and a locale directory with no `C` in it all reached the
reader as silence, and with only one `--old-locales-dir` no other block filled
the gap. The sixteenth entry recorded the same shape: cleared and unexamined
must not look alike.

### What changed

- `flag_algorithmic_ranges.py` and `resolve_copy_closure.py` write
  `sorted(set(generated) | set(unbuilt))`: the written list is never narrower
  than the set the step printed. The step-4 lists go from 478 to 483 names on
  `2.28 -> 2.34`, 488 to 494 on `2.34 -> 2.39`, 408 to 416 on the floor pair;
  step 3's floor list from 406 to 414 -- the union adds the five names
  SUPPORTED does not list, and the symbolic decode adds `ky_KG`, `uk_UA`
  and `uk_UA.utf8`. The node scans go from 475 to 482 on
  `glibc-2.28-251.el8_10.40` -- `C` among the seven added -- 478 to 483 on
  `glibc-2.34-275.el9_8` and 488 to 494 on `glibc-2.39-128.el10_2`.
- The summary line under "Needs an empirical test" now reads "N name(s) to
  confirm: generated names, and source names for the locales SUPPORTED does
  not list", because "N generated name(s)" is no longer what the file holds.
- In directory mode the label names the tag it came from and says who the
  authority is: "not in glibc-2.28's SUPPORTED -- the node's `locale -a` is the
  authority on whether these are built". Tag mode keeps the old wording, where
  it is true.
- Steps 9 and 10 close with a status for every locale in `KNOWN_BACKPORTED`,
  one line each: `ellipsis-based`, `codepoint_collation`, `explicit weights`,
  `copy-only`, `present, but defines no LC_COLLATE block`, or `ABSENT from this
  directory`. `audit.sh` reads that declared line instead of grepping for two
  shapes, and prints a seventh state, `NOT DECLARED`, if the step wrote none.
  That last branch is unreachable as the step stands -- the declaration also
  runs on the path that returns early with "No locale uses ellipsis ranges
  here", which is where a first version of this change left the summary
  printing NOT DECLARED over a scan that had looked -- and it is labelled
  untested in `tests/README.md`, with the other three.
- `--help` for `--supported-tag` and `docs/limitations.md` say the node ships
  no `SUPPORTED` as a measurement rather than an assertion, and name what the
  mapping therefore cannot decide. `docs/method.md` says the mapping is a
  translation, not a filter.
- The declaration prints last, after the step's closing sentence. Wedged in
  above it, "These cannot be cleared by a source diff alone" -- which names the
  exposed set -- sat directly under `C (C.UTF-8): codepoint_collation` on the
  RHEL9 and RHEL10 nodes, and read as covering a locale glibc settles by
  construction.
- The declaration carries step 4's own `copy` closure, so a backported locale
  that uses no ellipsis but copies a template that does is declared exposed
  rather than by its own style, and `audit.sh` relays the declared text whole
  instead of appending a verdict of its own. `codepoint_collation` outranks the
  copy, because glibc discards inherited collation information when it sees
  that keyword.
- `copy_targets` decodes `<U....>` through the new `decode_symbolic`, so the
  graph is a claim about what `localedef` will build rather than about how the
  file spells it. The floor pair's published figures move with it: step 3's
  affected set 278 -> 280, step 4's exposed set 279 -> 281 and its generated
  names 408 -> 411, in `docs/limitations.md`'s before/after table, the worked
  example and the tests that pin them against the tool -- 280 and the step-3
  header's 409, 281, 411 and both written-list lengths, 414 and 416.
- `classify_collation_style` no longer reads `<codepoint_collation>` as the
  keyword. glibc's lexer takes `<name>` as a collating symbol, and the wrong
  reading clears a locale outright -- the single most reassuring verdict this
  classifier has, and one the new copy-exposure note also defers to.
- Two ties in `tests/test_published_claims.py`: every status
  `docs/limitations.md` lists is one `report_backported` actually returns, the
  statuses taken from the function rather than grepped out of the file -- a
  first version searched the whole source and passed on a renamed status,
  because the old wording still sat in a comment three lines above; and every
  `full list (N name(s))` an example publishes equals the two numbers printed
  above it, across all seven step-4 blocks in the three examples.

### What was verified

- The defect, on real node data: `flag_algorithmic_ranges.py --locales-dir`
  over `collaudit8`'s own `/usr/share/i18n/locales/` (355 files, count asserted
  on both ends of the transport) with `--supported-tag glibc-2.28` writes 482
  names, `C` among them; before this change, 475 and no `C`.
- Twenty-five mutations, twenty-five failing tests: the union reverted to
  `generated` in each of the two scripts, the node label restored to "templates, not built
  by default", the per-locale declaration removed, `audit.sh` returned to its
  two greps, the declaration dropped from the no-ellipsis path, a status
  renamed in the script, `NOT DECLARED` renamed in `audit.sh`, a status dropped
  from `docs/limitations.md`, a published list count moved by one, the `copy`
  closure not passed to the declaration, the exposure note appended
  unconditionally, the summary guessing instead of relaying, dangling targets
  followed into nothing, "steps 1-3 are sufficient" printed over an unread
  file, an angle-bracketed symbol read as the keyword, a symbolic `copy`
  target left undecoded, the written count put in the step-3 header, an
  unresolved locale written under its source name only, one counted and never
  named, a corpus with no collation block accepted, `codepoint_collation` not
  excluded from the unresolved note, and the no-ellipsis path both writing
  source names only and writing its list without naming it, and the announced
  path not being the written one. The step-3 test asserts both halves -- `cns11643_stroke` and `sv_SE.utf8` -- so a fix
  that traded one omission for the other fails too. Two controls: the tag scan
  must declare no backported status, so a fix that printed the block everywhere
  fails; and an unrelated sentence added to the documented list must fail
  nothing.
- The six declared states are driven by fixture, including the ones the summary
  used to pass over: a `C` with explicit weights, a `C` that only copies
  `iso14651_t1`, and a directory with no `C`. The copy-only shape goes through
  the wrapper, asserting that the summary names what the copy reaches and that
  it is reported as neither of the two settled states. A directory where
  nothing at all uses an ellipsis still declares its `C`.
- `doc-sweep`, on the working tree: four false or imprecise passages, all
  fixed -- "`C` is in no tag's `SUPPORTED` at all" (it is in 2.39's, measured),
  "a sixth state" for a seventh, a positional claim about the example that did
  not hold, and a source comment pairing the RHEL8 node with a tag `audit.sh`
  never passes for it. Plus the block placement above. Everything else it
  checked reconciled. A second sweep of the finished tree caught five more,
  four of them figures this change itself moved and one an example line the
  tool never printed: the step-3 header counts generated names (409), not the
  written list (414), and the first patch put the list count in the header.
- `false-negative-reviewer`, on the diff: one finding, that the summary's
  fall-through branch called a `C` copying `iso14651_t1` "neither
  ellipsis-based nor codepoint_collation" -- true of the file, false of the
  order, on an input the suite itself constructs. Fixed above and re-reviewed.
  Everything else it drove came back honest, including eleven of its own
  mutations and the arithmetic of all seven example blocks.
- Acceptance: `DIFFERS` on all three pairs, in the lines listed above and
  nowhere else. Both worked examples were re-measured against runs taken
  2026-09-07 off `collaudit8`/`9`/`10`; every changed line in them appears
  verbatim in one of those runs.
- 298 tests, no skips.

## 2026-09-07 (twenty-second entry)

The two node-reading checks report the reach of what they find, the worked
examples finally show steps 6 to 10, and three branches of the summary that
no test had ever driven are driven. **No verdict moves.** Steps 1 to 5 print
exactly what they printed; steps 6 and 7 too on the three fixtures, since no
distro patch touches `LC_COLLATE` there. Step 8 gains one block.

### What it used to get wrong

**Node-to-node and the distro check did not close over the `copy` graph.**
Every other list this tool produces is closed over inheritance -- step 3
exists for nothing else, step 4 does it to its own result -- but the one check
that can see a distro backport at all reported the differing files and stopped.
A backport that edits `iso14651_t1` would have read as "1 locale(s) differ
inside LC_COLLATE", with the 328 to 338 locales that copy it nowhere, in the
step and in the summary. Not observed on the fixtures: no distro patch touches
`LC_COLLATE` on any of the three. Reproduced by injection, a comment inside
`iso14651_t1`'s block on a materialised tag: the old output named one file.

**The steps 9/10 summary block was quoted in `docs/limitations.md` and
published nowhere else.** No `examples/*.txt` carried a run of steps 6, 7, 9 or
10 -- only step 8, spliced in on 2026-09-06 -- and no test tied the quoted
block to anything. A reader could not check it, and nothing would have noticed
it going stale.

**Three summary branches had no test:** a run given only `--old-locales-dir`
(steps 6 and 9 run, 7, 8 and 10 do not), two nodes with nothing differing
inside `LC_COLLATE`, and the clean step 5 branch -- executed by the same-tag
test since the tenth entry and asserted by nobody. The steps 9/10 lines
`C (C.UTF-8): ellipsis-based` and `C (C.UTF-8): codepoint_collation` were
produced by the node-to-node wrapper test on every run and asserted by nobody
either.

### What changed

- `diff_node_locales.py` prints, under its differing-files list,
  "Additionally affected via `copy` inheritance at <new build>: N locale(s)"
  with the per-template breakdown step 4 uses, computed over the NEW node's
  own files, and writes the names to `node_collate_inherited.<builds>.txt`.
  `diff_distro_locales.py` prints the same block over the node's files. Roots
  are excluded, so a file that differs and copies another differing file is
  not counted twice. The closure itself is `inherited_via_copy()`, pure, in
  `diff_distro_locales.py`, on top of the tested `copy_graph_from_texts` and
  `inherited_from`.
- `audit.sh` reads that list and prints "plus N locale(s) that inherit one of
  those files' LC_COLLATE via copy" under the node-to-node count -- from the
  file, not from the step's prose, and an absent file is reported as
  `NOT REPORTED`, never as zero. The file is removed up front like every other
  file the summary reads.
- On the real fixtures, RHEL8→RHEL9: `sv_FI` and `sv_FI@euro` inherit from
  `sv_SE`, the same two step 3 finds from the tag side. RHEL9→RHEL10: zero.
- Both worked examples now carry steps 6, 7, 8, 9, 10 and the complete
  summary of a run given both nodes' locale sources -- taken 2026-09-07 off
  `collaudit8`/`9`/`10` (`glibc-2.28-251.el8_10.40`, `glibc-2.34-275.el9_8`,
  `glibc-2.39-128.el10_2`; 355, 356 and 366 files, counts asserted on both
  ends of the transport). Per-file diff bodies in step 8 are trimmed and
  marked; nothing else is.
- `tests/test_published_claims.py` ties the block `docs/limitations.md` quotes
  to the RHEL8→RHEL9 example line for line, requires every node-step banner in
  both examples, and checks the summary's "plus N" against step 8's N.

### What was verified

- Injection: a template edited inside its block on a materialised 2.39 tree
  makes node-to-node and the distro check both report more than 300 inherited
  locales, `en_US` among them, and the written list has exactly that many
  names. Two unedited tag trees report `sv_FI, sv_FI@euro` via `sv_SE`.
- Wrapper: one side only, two identical trees under different build ids
  (fingerprint warning fires, "no locale differs" branch printed, no shell
  error), the clean step 5 sentence on the same-tag pair, and both `C (C.UTF-8)`
  ellipsis-scan lines in the two-node run.
- Steps 1 to 7 byte-identical before and after on both audited pairs.

## 2026-09-07 (twenty-first entry)

Three guards on the input, none of which existed. **No verdict moves, and both
audited pairs' output is byte-identical before and after**, as is the floor
pair's. Every case below was measured on the clone or on a fabricated
repository before it was called latent; none has fired on an audited pair.

### What it used to get wrong

**No corpus floor in tag mode.** The node-reading modes have refused a
directory of fewer than 200 locale files since they were written, because two
truncated copies agree perfectly. The tag modes never checked. A tag whose tree
holds no `localedata/locales/` — a restructured checkout, a tag from before the
directory existed (glibc 2.0 has none, 2.2 has 148) — produced "Locale files
changed: 0" from step 1, "0 touch LC_COLLATE" from step 2, "No locale uses
ellipsis ranges here; steps 1-3 are sufficient" from step 4, and exit 0 from
all three. Reproduced on a fabricated three-file repository.

**Step 2 aborted on a rename whose old side had no `LC_COLLATE` block**, and
could not see such a file gain one. The new side was read, and looked up, under
the OLD path — which does not exist at the new tag once the file is renamed.
Reading it died with "could not read"; had it not, the verdict would have been
"no LC_COLLATE on either side" for a file that now has one. Latent: the only
rename in the audited pairs (`aa_ER@saaho` → `ssy_ER`, 2.34..2.39) has a block
on both sides. Reproduced on a fabricated repository where `x` becomes `y` and
gains a block: the old code exited 2.

**`git diff` obeyed the user's configuration.** Measured with git 2.50 on the
2.34..2.39 pair: `diff.noprefix=true` drops the `a/ b/` the header regex
expects (step 2 then dies, correctly, with "the diff does not cover");
`diff.renameLimit=1` skips rename detection with a warning on stderr, so the
renamed file becomes delete-plus-add and lands under "not analysed" —
silently; `diff.renames=false` does the same to step 1, whose published count
of 318 reads 319; `color.ui=always` writes escape codes into the pipe.
`--find-renames` on the command line does **not** lift a configured limit.

### What changed

- `list_locale_files()` dies below `MIN_LOCALE_FILES` (200, one constant now
  shared with the node modes' `DEFAULT_MIN_FILES`). Steps 2, 3 and 4 reach it
  on every run; step 1 asks for it explicitly through a new silent
  `glibc_locale_data.py corpus` subcommand, so its output stays byte-identical.
- Step 2 reads the new side under each file's new name and looks the verdict
  up the same way: `new_side_paths()` and `judge()`, both pure, both tested by
  injection.
- Every `git` the Python steps run carries `-c color.ui=false -c
  diff.noprefix=false -c diff.mnemonicPrefix=false -c diff.renames=true -c
  diff.renameLimit=0`; step 1's shell `git diff` carries the same. Command-line
  `-c` outranks every configuration source.

### What was verified

- Steps 1, 2 and 4 on 2.28..2.34, 2.34..2.39 and 2.12..2.17: byte-identical
  output before and after, output-directory path aside.
- Under a hostile `GIT_CONFIG_GLOBAL` (noprefix, mnemonicPrefix, renames off,
  renameLimit 1, colour always) steps 1 and 2 print exactly what they print
  without it, with a control asserting that the same config still changes a
  bare `git diff`'s count to 319.
- A fabricated three-file repository is refused by steps 1, 2 and 4 with the
  floor named; the same repository at 200 files passes, so the guard refuses
  size, not fabricated input. The rename case reports `gained-collate` and
  hands step 3 the new name.
- Mutation checks: with `--find-renames` alone in step 1 the hostile-config
  test fails at 319 (that was the first version of this fix); with the lookup
  under the old path restored, the rename test reads `no-collate`.

## 2026-09-07 (twentieth entry)

Documentation only. No code path changes and no verdict moves; nothing a run
prints is different. The passages below said something false or stale about
the tool; each is corrected, with what was checked.

### What the docs used to say, and what is true

- **"Two optional checks read a real node's own files"** (README, twice). There
  have been three since the seventeenth entry: the distro-versus-upstream
  comparison, the node-to-node comparison, and the ellipsis scan of each node's
  own data (steps 9 and 10). The third is the only one that asks the ellipsis
  question of the node's own `C`, since no tag of the RHEL8→RHEL9 pair holds
  that file. `docs/method.md` and `audit.sh` already said three.
- **"`diff_node_locales.py` is the only thing in the run that looks at that
  file"** (README). False since the same entry: steps 9 and 10 read `C` too.
- **PostgreSQL version attribution.** `docs/requirements.md` and both SQL
  headers said `pg_collation_actual_version()` "arrived in 15" / "needs 15+".
  Checked against the PostgreSQL catalogs on GitHub: the function exists since
  PostgreSQL 10 (`pg_proc.h`, OID 3448) and returns a version for `libc`
  collations from 13. What arrived in 15 is `pg_database.datlocprovider`,
  `datcollversion` and `pg_database_collation_actual_version()`. The 15+
  requirement stands; the reason did not, and the instruction "on 13/14 delete
  the actual-version block" deleted a query that works there. The headers now
  say which half to drop. Likewise, the `pg_strncasecmp("C.", ...)` test was
  described as "present in every branch from PG 14 on" as if PG 13 lacked the
  behaviour: PG 13 gets the same NULL by chopping the encoding suffix and
  comparing the rest to `c` (`copy_suffix`, `pg_locale.c`, REL_13_STABLE);
  the `pg_strncasecmp` form is 14+. Three files corrected.
- **CHANGELOG cross-references.** The fifteenth entry said `th_TH` moved "in
  the thirteenth entry below" and that `examples/` has carried a script for
  each pair "since the thirteenth entry". Both happened in the **eleventh**
  ("th_TH changes after all, and the second pair finally has a script").
- **"inherited by 328 locales"** in the README's CJK row, which covers both
  pairs. 328 is the blast radius at 2.34; at 2.39 it is 338
  (`examples/rhel9-to-rhel10-audit-output.txt`). The row now states both, and
  both are asserted against the clone in `tests/test_known_answers.py` — the
  328 was stated in five files and tied in none.
- **"four traps make the comparison agree with itself"** attributed all four to
  the SQL template. `docs/confirming-on-a-real-system.md` is explicit that the
  fourth, two truncated copies agreeing perfectly, belongs to the file
  comparisons. The README now says three and one.
- **`examples/rhel8-to-rhel9.sql`** lists `sv_FI@euro` as affected and creates
  no table for it. A comment now says why: it is an ISO-8859-15 locale, unusable
  as a collation in a UTF8 database, and it changes exactly as `sv_FI` does.
- **The "if you saved an earlier result" notice** dated the last verdict move
  "as recently as 2026-09-06" and then said "no verdict moved on 2026-09-06",
  which reads as a contradiction to anyone who saved a result that afternoon.
  It now names the eleventh entry as the move and says `C.UTF-8`'s direct
  measurement came later the same day and moved a basis, not a verdict.
- **A code comment** in `diff_distro_locales.py` called 353/355/366 "the three
  measured RHEL corpora". Those are the upstream file counts at the three tags;
  the nodes carry 355, 356 and 366 (`docs/requirements.md`).

### What was verified

- Every internal anchor resolves; no spelled-out count changed except
  "two" → "three" optional checks, grepped across every file.
- The PostgreSQL claims were checked by reading `pg_proc.h` at REL_10_STABLE,
  `pg_proc.dat` at REL_12 through REL_15, `pg_database.h` at REL_14 and REL_15,
  and `get_collation_actual_version()` in `pg_locale.c` at REL_13 and REL_14.
- The 328/338 figures are now a known-answer test against the pinned tags.
- No `\echo` label in either SQL file changed, so the published probe outputs
  still match what the probe prints.

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

### Also: the private working rules are pinned unpublished

The rules Claude follows in this repository were published once by accident
and unpublished in PR #18; since then `.claude/` is ignored whole, on the
strength of one line in `.gitignore` that nothing checked. Two tests now pin
it: the line is present, and `git ls-files .claude` is empty. Removing the
line fails the first; force-adding a file under `.claude/` fails the second.

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
🟡 Unresolved to 🔴 **Changed** on **2026-09-06**, in the eleventh entry
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
  pair. It has carried one for each since the eleventh entry.
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
