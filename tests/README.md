# Tests

```sh
python3 tests/run_parallel.py                           # one process per class
python3 -m unittest discover -s tests -t tests          # the same tests, one process
python3 -m unittest discover -s tests -t tests -q -k pure_functions   # no clone needed
```

Stdlib `unittest`, no dependencies — [docs/requirements.md](../docs/requirements.md) promises `python3` (stdlib only),
and breaking that would itself be a regression. [`run_parallel.py`](run_parallel.py)
is stdlib too, and is the faster of the first two on a machine with cores to
spare: it runs each `TestCase` class in its own `python3 -m unittest` process.

No runtime is written down here on purpose, and
[`test_published_claims.py`](test_published_claims.py) forbids one in any file
that publishes the command. A test count written down here went stale twice in
one day and was removed for the same reason; before that, two files disagreed
about how long one command took. Time it on your machine rather than trusting
a figure from someone else's.

What the parallel runner adds over the serial command is ways of losing tests
quietly, so it refuses instead: it holds every shard to the number of tests
discovery counted **in that class** — a sum would let a loss in one class hide
behind a gain in another — and checks the total too; a shard that exits
without saying what it ran is FAILED; the verdict is read only from the stream
`unittest` writes it to, so a test printing summary-shaped lines cannot supply
its own; a run of nothing, a class named on the command line that matches
nothing and a module that does not import are all FAILED; and a hung shard is
killed and named. CI runs the serial command on every pull request and every
push to `main`, so this runner is not the last word on a change that gets
merged.

Every test freezes a failure this tool shipped, or a way of losing one before
it ships, and its docstring says which; most of them quote the CHANGELOG entry
they guard. A test whose purpose is forgotten is a test somebody deletes
during a refactor.

## Layers

| file | needs the glibc clone | what it covers |
|---|---|---|
| `test_provenance.py` | yes | **that the tags resolve to the pinned commits.** If these fail, every number in `test_known_answers.py` is suspect and a mismatch there must not be read as a code regression |
| `test_distro_diff.py` | yes | **the distro-versus-upstream comparison.** Checked against step 2's answer on both pairs, reached by a different algorithm; the node-side transport stays un-exercised, like the SQL template |
| `test_wrapper.py` | yes | **audit.sh end to end.** The wrapper removes a manual handoff, and automating a handoff is how the stale-result bug comes back; most of these tests are its failure modes, not its happy path |
| `test_pure_functions.py` | no | the algorithmic core: ellipsis matching, the `copy` graph, generated locale names, hunk/block overlap, the comment filter |
| `test_git_helpers.py` | mostly | the silent-failure class — code that cannot tell "nothing here" from "could not look". Seven classes fabricate a tiny glibc-shaped repository instead and run without the clone: the corpus floor, a renamed file gaining an `LC_COLLATE` block, a pure rename that must still pass, a file gaining one that names no character, a deleted and a renamed file that step 2 must list as removed, and the two about the pair itself — which of the two tags is the newer commit (including a pair that no signal can order, and two probes made to fail) and what step 2 does about it |
| `test_known_answers.py` | yes | the five steps end to end on both pairs, against the results [docs/results.md](../docs/results.md) publishes — plus `glibc-2.12 -> glibc-2.17`, which is not an audited pair but is the one below the old glibc 2.24 floor, whose figures had no test until they had already gone stale once. Plus `glibc-2.28 -> glibc-2.39`, the release-skipping pair [docs/method.md](../docs/method.md) measures — asserted as set equality against the two audited pairs rather than as counts, because five names of which one is wrong is still five |
| `DISABLED_published_claims.py` | not run | **SWITCHED OFF 2026-09-23 for the documentation refactor**, by a rename that takes it out of unittest's `test*.py` discovery; the file's own docstring says why, what is unguarded meanwhile, and how to turn it back on. While it is off, nothing below this sentence is running. **The numbers and quotes the documentation publishes.** Two correction passes in one day found the same class of defect — a count, a position or a quoted line that no longer matched the tool or the measurement. This is that, mechanised: it cannot check prose and does not try. Also a table of canonical figures — a number restated across pages must be the same number on every page that states it in the shape the table names, and must still be stated in as many files as the row expects; the widest-spread of them is written in six files and was kept in agreement by re-reading and nothing else. Four of the six rows are also held to the saved run the figure came from, through four ties in all: three name the sentence the run prints it in and the section to read that sentence in -- a node built from a tag reprints the tag's figures word for word, lower down the same file, so there neither the number nor the sentence is an anchor on its own -- and one names a sentence that appears only once in its file and needs no section. Each of those four rows is then asked, every run, whether moving a single statement of its figure would make at least one file under it stop agreeing: a row where no single move breaks the tie cannot tell its figure from another statement of the same number in the same file, which is how the first of these shipped and what it cost two review rounds to find. Also that every internal link and anchor in the published Markdown resolves, and that every `docs/*.md` path `audit.sh`, `sql/` or `examples/` names exists -- a retitled heading used to break links in silence. And that nothing under `.claude/` is tracked, so the private working rules stay unpublished |
| `test_node_modes.py` | yes | **the two modes that read a node's own files.** A tag stands in for a node and the backported `C` is written out, because that file exists at no tag — which is the whole point. Includes the test that says the `C.UTF-8` limitation is closed on the data half |
| `test_parallel_runner.py` | no | **the parallel runner's own guards**, because a runner that loses a shard prints a green summary over tests nobody ran. Every guard in this list was reverted in a mutation and turned this layer red, and a control mutation left it green: discovery's count held per class and not as a sum, the total as well, an unreadable shard read as zero tests, a shard whose exit status contradicts its own `OK`, a verdict read from what the tests printed rather than from the stream `unittest` writes it to, the first summary taken instead of the last, a skip missing from the status line the gate greps on a green run and on a red one, a green report over a run of nothing, a module that did not import, a start directory that does not import, an empty discovery, a selection that matches nothing, a class nobody counted, a class no shard reported on at all, a class two shards reported on, a hung shard reported as a success, and a failed shard whose output never reaches the report |

Without a clone at `scripts/glibc`, every layer marked "yes" **skips with a
reason**; `test_pure_functions.py`, `test_published_claims.py`,
`test_parallel_runner.py` and the
fabricated-repository classes of `test_git_helpers.py` still run. A skip is
never a pass: read what it says. CI clones fresh and fails on any skip, so a layer that skips
there is a red build, not a quiet gap.

## What this suite does NOT cover

Stated here because "the tests pass" must not be read as "the audit is correct".
That misreading is the exact reassuring-direction failure this repo exists to
prevent.

- **`sql/collation_confirmation_template.sql` and `sql/c_utf8_probe.sql` are
  untested.** They need a live PostgreSQL on two operating systems. Between
  them they are half the method and have no automated coverage at all. The
  probe is untestable in CI by construction: what it measures is the glibc a
  node has installed.
- **The empirical confirmation on real nodes is irreplaceable.** These tests
  check the reasoning applied to glibc's source. They say nothing about the sort
  order a given machine actually produces.
- **Distro backports are invisible here**, by definition: they are not in the
  upstream tags the suite reads. See
  [docs/limitations.md](../docs/limitations.md).
- **`nearest_glibc_tag`'s exit-status check cannot be mutation-tested.** It
  checks whether `git describe` succeeded, and then whether it printed a name
  at all; both paths return `None`, so deleting the first changes no answer
  and no test can fail. The outcome is what the suite pins: a pair whose
  release cannot be read is left `undetermined`, never ordered.
- **`--quiet` on the `order` subcommand is not asserted to be quiet.** Every
  step that takes the pair prints the `!!` block into its own log -- measured
  on a same-commit run: steps 1, 2 and 5 -- and `--quiet` keeps the wrapper's
  own call from adding one more copy. Nothing fails if it stops working, and
  the cost of that is repetition, not a wrong answer. The wrapper's
  undetermined branch is driven by a shim that replaces the subcommand
  outright, so no test is in a position to count the blocks.
- **The pinned numbers are for glibc 2.28, 2.34, 2.39 and the floor pair
  2.12/2.17 only.** Audit a different pair and this suite says nothing about
  that result. The floor pair carries no verdict of its own: it is pinned
  because the documentation quotes numbers from it, not because anyone should
  audit it.
- **One pair below the old glibc 2.24 floor is not every pair below it.** The
  bug that made the method collapse there is fixed and tested on `2.12 -> 2.17`.
  Nothing here says an older pair behaves the same, and steps 2 and 5 have been
  run below the floor exactly once.
- **Eight of audit.sh's guards are unreachable, so nothing tests them.** The
  argv name validation, the up-front `rm -f` of the files the summary reads,
  the "step 2 wrote no file" check, the `NOT DECLARED` branch of the
  steps 9/10 block, and the four `NOT REPORTED` branches -- the reach of the
  node-to-node block, the two sources of the Removed block, and the new
  copy's list -- are all defence against a future refactor: as the wrapper
  stands, step 2 always rewrites its lists for the pair being audited, steps
  7 and 8 always write their lists, steps 9 and 10 always declare a status
  for every backported locale they know of, and `set -e` already ends the
  run if a step fails, so no test can drive them. Seven carry that label in
  `audit.sh`; the up-front `rm -f` carries the run it exists to stop reading
  instead. Reverting any of the eight leaves the suite green — which is the
  honest statement, not a claim of coverage.
- **`C.UTF-8` is asserted to be *warned about* and to reach a DATA verdict —
  not to be correct.** `test_node_modes.py` checks that a backported file in
  neither tag gets a verdict, that its `ellipsis`/`codepoint_collation` shape
  is reported on both sides, and that it is named even when identical. What no
  test can settle is the ORDER: an ellipsis range's weights are computed by
  `localedef` when the locale is built, so that answer lives on a node and in
  `sql/c_utf8_probe.sql`.
- **Signatures are not verified here.** `test_provenance.py` asserts the release
  tags are still *signed* and that the tool reports an unverifiable signature as
  unchecked rather than good. Actually verifying one needs the glibc release
  managers' public keys, which a stock machine does not have. Pinned commit ids
  are the guarantee this suite does offer; a signature is the stronger one, and
  `scripts/audit-locale-diff.sh` prints its state on every run.

## Adding a test

Pin behaviour, not wording. Assertions read counts and names out of the output
rather than comparing whole text: the printed prose changes often, and a suite
that fails on a reworded sentence gets switched off.

Assert on wrapped output through `flat()` from `_harness.py`, never on the raw
text: `dd.warn` wraps at 78 columns, so a negative assertion on a phrase of more
than a few words passes whether the phrase is printed or not. Four tests have
been found guarding nothing that way.

Before trusting a new test, break the thing it claims to guard and confirm it
fails. The suite was built that way, and it caught a real gap: the first version
of the `read_blobs_strict` test checked the helper while nothing asserted that
`build_copy_graph` *called* it, so reverting that fix left every test green.
