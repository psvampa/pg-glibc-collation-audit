# Documentation

| Document | What's in it |
|---|---|
| [method.md](method.md) | The five steps, in detail: what each one reads, and why it exists |
| [results.md](results.md) | The full verdict table, both worked examples, and what was tested on which nodes |
| [confirming-on-a-real-system.md](confirming-on-a-real-system.md) | Running the SQL template on both nodes, and the two traps that make a comparison lie |
| [scope.md](scope.md) | What this audits — `LC_COLLATE` and the `libc` provider — and what it does not |
| [limitations.md](limitations.md) | The four things it structurally cannot see, `C.UTF-8` among them |
| [requirements.md](requirements.md) | Dependencies, the test suite, and the langpack/`initdb` traps on the confirmation side |
| [comparison-ardentperf.md](comparison-ardentperf.md) | How this relates to ardentperf/glibc-unicode-sorting, and how to read their tables |
| [../CHANGELOG.md](../CHANGELOG.md) | What this tool used to get wrong — read it if you saved a result before 2026-09-05 |
| [../tests/README.md](../tests/README.md) | What the test suite covers, and what it does not |

Start with [method.md](method.md) if you want to know whether to trust the
answer, and [results.md](results.md) if you just want the answer.
