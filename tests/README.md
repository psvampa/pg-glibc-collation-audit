# Tests

```sh
python3 -m unittest discover -s tests -t tests          # everything, ~1 min
python3 -m unittest discover -s tests -t tests -q -k pure_functions   # no clone needed
```

Stdlib `unittest`, no dependencies — [docs/requirements.md](../docs/requirements.md) promises `python3` (stdlib only),
and breaking that would itself be a regression.

Every test freezes a failure this tool actually shipped; the CHANGELOG entry it
guards is quoted in its docstring. A test whose purpose is forgotten is a test
somebody deletes during a refactor.

## Layers

| file | needs the glibc clone | what it covers |
|---|---|---|
| `test_provenance.py` | yes | **that the tags resolve to the pinned commits.** If these fail, every number in `test_known_answers.py` is suspect and a mismatch there must not be read as a code regression |
| `test_distro_diff.py` | yes | **the distro-versus-upstream comparison.** Checked against step 2's answer on both pairs, reached by a different algorithm; the node-side transport stays un-exercised, like the SQL template |
| `test_wrapper.py` | yes | **audit.sh end to end.** The wrapper removes a manual handoff, and automating a handoff is how the stale-result bug comes back; most of these tests are its failure modes, not its happy path |
| `test_pure_functions.py` | no | the algorithmic core: ellipsis matching, the `copy` graph, generated locale names, hunk/block overlap, the comment filter |
| `test_git_helpers.py` | yes | the silent-failure class — code that cannot tell "nothing here" from "could not look" |
| `test_known_answers.py` | yes | the five steps end to end on both pairs, against the results [docs/results.md](../docs/results.md) publishes |

Without a clone at `scripts/glibc`, every layer marked "yes" **skips with a
reason** and `test_pure_functions.py` still runs. A skip is never a pass: read
what it says. CI clones fresh and fails on any skip, so a layer that skips
there is a red build, not a quiet gap.

## What this suite does NOT cover

Stated here because "the tests pass" must not be read as "the audit is correct".
That misreading is the exact reassuring-direction failure this repo exists to
prevent.

- **`sql/collation_confirmation_template.sql` is untested.** It needs a live
  PostgreSQL on two operating systems. It is half the method and has no
  automated coverage at all.
- **The empirical confirmation on real nodes is irreplaceable.** These tests
  check the reasoning applied to glibc's source. They say nothing about the sort
  order a given machine actually produces.
- **Distro backports are invisible here**, by definition: they are not in the
  upstream tags the suite reads. See
  [docs/limitations.md](../docs/limitations.md).
- **The pinned numbers are for glibc 2.28, 2.34 and 2.39 only.** Audit a
  different pair and this suite says nothing about that result.
- **Three of audit.sh's guards are unreachable, so nothing tests them.** The
  argv name validation, the up-front `rm -f` of the files the summary reads,
  and the "step 2 wrote no file" check are all defence against a future
  refactor: as the wrapper stands, step 2 always rewrites its list for the
  pair being audited and `set -e` already ends the run if a step fails, so no
  test can drive them. They are labelled as such in `audit.sh`. Reverting any
  of the three leaves the suite green — which is the honest statement, not a
  claim of coverage.
- **`C.UTF-8` is asserted to be *warned about*, not to be correct.** No test can
  settle it from source; that is the point of the warning.
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

Before trusting a new test, break the thing it claims to guard and confirm it
fails. The suite was built that way, and it caught a real gap: the first version
of the `read_blobs_strict` test checked the helper while nothing asserted that
`build_copy_graph` *called* it, so reverting that fix left every test green.
