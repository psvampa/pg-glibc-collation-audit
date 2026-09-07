# Documentation

## Reading order

1. **[method.md](method.md)** — how the answer is produced, and why a clean
   diff counts as proof. Read this if you need to decide whether to trust it.
2. **[results.md](results.md)** — the evidence behind every row of the
   verdict table, and both worked examples. The table itself is in
   [the README](../README.md#results-for-the-two-rhel-pairs).
3. **[confirming-on-a-real-system.md](confirming-on-a-real-system.md)** — how
   to verify it on your own nodes, which is the only evidence that covers
   distro backports.
4. **[limitations.md](limitations.md)** — the six things to know before you
   act on a clean result, `LC_CTYPE` among them.

## Every document

| Document | What's in it |
|---|---|
| [method.md](method.md) | The five steps in detail, how to read what a run prints, and the decision procedure they add up to |
| [results.md](results.md) | The evidence behind each verdict, both worked examples, and what was tested on which nodes |
| [confirming-on-a-real-system.md](confirming-on-a-real-system.md) | Running the SQL template and the `C.UTF-8` probe on both nodes, comparing the nodes' locale files against upstream and against each other, and the four traps that make a comparison lie |
| [scope.md](scope.md) | What this audits — `LC_COLLATE` and the `libc` provider — and the `builtin` provider as a way out |
| [limitations.md](limitations.md) | The six things to know before acting on a clean result — `C.UTF-8` among them, and now covered three other ways; `LC_CTYPE`, which nothing here measures |
| [requirements.md](requirements.md) | Dependencies, the test suite, and the three setup traps on the confirmation side |
| [comparison-ardentperf.md](comparison-ardentperf.md) | How this relates to ardentperf/glibc-unicode-sorting, and how to read their tables |
| [glossary.md](glossary.md) | `copy` graph, blast radius, hunk, tier, ellipsis range, role swap |
| [../examples/](../examples/) | Real output from both pairs, a confirmation SQL script for each, the `C.UTF-8` probe output that is the only published evidence for that locale's order, and the below-the-floor pair that is not an audited result but shows what the old glibc 2.24 bug cost |
| [../CHANGELOG.md](../CHANGELOG.md) | What this tool used to get wrong, and when |
| [../tests/README.md](../tests/README.md) | What the test suite covers, and what it does not |

---

[Back to the README](../README.md)
