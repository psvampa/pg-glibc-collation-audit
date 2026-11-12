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
| `test_provenance.py` | mostly | the tags resolve to the pinned commits; if one no longer does, every number in `test_known_answers.py` is suspect |
| `test_known_answers.py` | yes | step 2 end to end on the pinned pairs |
| `test_wrapper.py` | yes | `audit.sh` end to end, mostly its failure modes: node files, missing sources, refused files; and the outputs of commands 1 and 1 extended in `examples/`, compared whole with what it prints |
| `test_git_helpers.py` | mostly | code that cannot tell "nothing here" from "could not look"; some of its classes build a small repository of their own |
| `test_glibc_reader.py` | mostly | `scripts/glibc_reader.py`, a reader of glibc's locale files that follows `localedef`, against what `localedef` did on the three RHEL builds (`reader_oracle/`); `scripts/reader_cache.py`, which keeps its answers; and `scripts/glibc_tree.py`, through which step 2 reads them |
| `test_build_sources.py` | mostly | `scripts/build_sources.py`, which rebuilds a RHEL glibc build's `localedata/` and `locale/` from its source RPM in the order rpm recorded in its header, against what `rpmbuild -bp` did with three builds (`build_sources/`), what those machines hold, and what the builds' binary packages record; and how it fetches a build's three files by name, on a mirror the tests make; and `audit.sh` end to end on builds and on machines' files |
| `test_pure_functions.py` | no | the algorithmic core: ellipsis matching, the `copy` graph, locale names and their aliases |
| `test_locale_order.py` | no | step 11: the comparison on three real machines' measurements, the file each machine writes, and the progress lines |
| `test_confirm_sql.py` | no | the SQL files step 11 writes for PostgreSQL (`--sql-to`), and `scripts/confirm_sql.py read` on outputs PostgreSQL printed for them (`confirm_sql/`) |
| `test_parallel_runner.py` | no | guards against losing tests quietly, in the parallel runner and in a test file run on its own |
| `test_published_claims.py` | no | the figures, links and paths the documentation publishes |

Without a clone at `scripts/glibc`, every layer marked "yes" skips with a
reason; `test_pure_functions.py`, `test_parallel_runner.py`,
`test_locale_order.py`, `test_confirm_sql.py`, `test_published_claims.py`
and the classes of
`test_git_helpers.py`, `test_glibc_reader.py`, `test_build_sources.py` and
`test_provenance.py` that do not need it still run. A skip is never a pass,
and CI, which clones glibc fresh, fails on any skip.

## What this suite does NOT cover

"The tests pass" does not mean "the audit is correct".

- The SQL files, `sql/collation_confirmation_template.sql` and
  `sql/c_utf8_probe.sql`, are never run, because that needs PostgreSQL on
  two machines. Neither are the files step 11 writes: the suite reads
  outputs PostgreSQL printed for them, recorded by hand (`confirm_sql/`),
  and so it does for the probe (its two examples, and `c_utf8_probe/`).
- A real machine: what runs on one -- the half of `scripts/locale_order.py`
  that measures, and `tests/reader_oracle/measure.py run`, which compiles
  the reader's cases with `localedef` -- needs Linux with glibc. So does
  `rpmbuild -bp`, whose patch order and tree for three builds
  `build_sources/` records. The suite replays what they recorded, and the
  transport over `ssh` was measured by hand.
- A whole binary package: `build_sources/` keeps only the headers of the
  three builds' `glibc-locale-source` and `glibc-common`, so the charmaps in
  a real package's payload were compared by hand, on seven builds; the
  suite compares them on packages it makes.
- A real mirror: no test reads one. Fetching the seven builds from Rocky
  Linux's mirror was measured by hand; the suite fetches from a mirror it
  makes, in the layouts found on Rocky's mirror.
- Distro backports in what `audit.sh` compares, beyond three builds: with a
  build named, it is tested on the three fixture builds, rebuilt from
  `tests/build_sources/`, and on builds the suite makes; on no other real
  build.
- Any pair of glibc versions other than the ones the suite pins.
- Some guards of `audit.sh` that cannot be reached as it stands, so no test
  drives them. Each is labelled "Unreachable" in the code.

## Adding a test

Pin behaviour, not wording: read counts and names out of the output. Assert on
wrapped output through `flat()` from `_harness.py`. Before trusting a new test,
break what it guards and confirm that it fails.
