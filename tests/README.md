# Tests

```sh
python3 tests/run_parallel.py                    # one process per class
python3 -m unittest discover -s tests -t tests   # the same tests, one process
```

Standard library only, like the rest of the tool. Every test freezes a failure
this tool shipped, or a way of losing one, and its docstring says which.

## Layers

| file | needs the glibc clone | what it covers |
|---|---|---|
| `test_provenance.py` | mostly | the tags resolve to the pinned commits; if this fails, every number in `test_known_answers.py` is suspect |
| `test_known_answers.py` | yes | steps 1 to 5 end to end on the pinned pairs |
| `test_wrapper.py` | yes | `audit.sh` end to end, mostly its failure modes: node files, missing sources, refused files |
| `test_node_modes.py` | yes | the steps that read a machine's own locale files |
| `test_distro_diff.py` | yes | the comparison of a machine's locale files against its upstream tag (steps 6 and 7) |
| `test_git_helpers.py` | mostly | code that cannot tell "nothing here" from "could not look"; some of its classes build a small repository of their own |
| `test_pure_functions.py` | no | the algorithmic core: ellipsis matching, the `copy` graph, locale names and their aliases, the comment filter |
| `test_locale_order.py` | no | step 11: the comparison on three real machines' measurements, the file each machine writes, and the progress lines |
| `test_parallel_runner.py` | no | guards against losing tests quietly, in the parallel runner and in a test file run on its own |
| `test_published_claims.py` | no | the figures, links and paths the documentation publishes |

Without a clone at `scripts/glibc`, every layer marked "yes" skips with a
reason; `test_pure_functions.py`, `test_parallel_runner.py`,
`test_locale_order.py`, `test_published_claims.py` and the classes of
`test_git_helpers.py` and `test_provenance.py` that do not need it still
run. A skip is never a pass, and CI, which clones glibc fresh, fails on any
skip.

## What this suite does NOT cover

"The tests pass" does not mean "the audit is correct".

- The SQL files, `sql/collation_confirmation_template.sql` and
  `sql/c_utf8_probe.sql`, are never run, because that needs PostgreSQL on
  two machines.
- A real machine: the half of `scripts/locale_order.py` that measures needs
  Linux with glibc, and the transport over `ssh` was measured by hand.
- Distro backports: they are not in the upstream tags the suite reads.
- Any pair of glibc versions other than the ones the suite pins.
- Some guards of `audit.sh` that cannot be reached as it stands, so no test
  drives them. Each is labelled "Unreachable" in the code.

## Adding a test

Pin behaviour, not wording: read counts and names out of the output. Assert on
wrapped output through `flat()` from `_harness.py`. Before trusting a new test,
break what it guards and confirm that it fails.
