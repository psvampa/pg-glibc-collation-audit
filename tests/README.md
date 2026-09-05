# Tests

```sh
python3 -m unittest discover -s tests -t tests          # everything, ~16s
python3 -m unittest discover -s tests -t tests -q -k pure_functions   # no clone needed
```

Stdlib `unittest`, no dependencies — the README promises `python3` (stdlib only),
and breaking that would itself be a regression.

Every test freezes a failure this tool actually shipped; the CHANGELOG entry it
guards is quoted in its docstring. A test whose purpose is forgotten is a test
somebody deletes during a refactor.

## Layers

| file | needs the glibc clone | what it covers |
|---|---|---|
| `test_provenance.py` | yes | **that the tags resolve to the pinned commits.** If these fail, every number in `test_known_answers.py` is suspect and a mismatch there must not be read as a code regression |
| `test_pure_functions.py` | no | the algorithmic core: ellipsis matching, the `copy` graph, generated locale names, hunk/block overlap, the comment filter |
| `test_git_helpers.py` | yes | the silent-failure class — code that cannot tell "nothing here" from "could not look" |
| `test_known_answers.py` | yes | the five steps end to end on both pairs, against the results the README publishes |

Without a clone at `scripts/glibc`, the last two **skip with a reason** and the
first still runs. A skip is never a pass: read what it says.

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
  upstream tags the suite reads. See README, *Known limitations*.
- **The pinned numbers are for glibc 2.28, 2.34 and 2.39 only.** Audit a
  different pair and this suite says nothing about that result.
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
