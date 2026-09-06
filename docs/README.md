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
4. **[limitations.md](limitations.md)** — the five things the method cannot
   see. Read before you act on a clean result.

## Every document

| Document | What's in it |
|---|---|
| [method.md](method.md) | The five steps in detail, and the decision procedure they add up to |
| [results.md](results.md) | The evidence behind each verdict, both worked examples, and what was tested on which nodes |
| [confirming-on-a-real-system.md](confirming-on-a-real-system.md) | Running the SQL template on both nodes, and the three traps that make a comparison lie |
| [scope.md](scope.md) | What this audits — `LC_COLLATE` and the `libc` provider — and the `builtin` provider as a way out |
| [limitations.md](limitations.md) | The five things it structurally cannot see, `C.UTF-8` among them |
| [requirements.md](requirements.md) | Dependencies, the test suite, and the three setup traps on the confirmation side |
| [comparison-ardentperf.md](comparison-ardentperf.md) | How this relates to ardentperf/glibc-unicode-sorting, and how to read their tables |
| [glossary.md](glossary.md) | `copy` graph, blast radius, hunk, tier, ellipsis range, role swap |
| [../examples/](../examples/) | Real output from both pairs, plus a confirmation SQL script for each |
| [../CHANGELOG.md](../CHANGELOG.md) | What this tool used to get wrong, and when |
| [../tests/README.md](../tests/README.md) | What the test suite covers, and what it does not |

---

[Back to the README](../README.md)
