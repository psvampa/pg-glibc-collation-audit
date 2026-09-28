# Documentation

## Reading order

1. **[commands.md](commands.md)** — what each of the three commands does,
   and what it leaves to the next. Read this first if you are about to run
   one.
2. **[results.md](results.md)** — the verdict table, the evidence behind
   every row, and both worked examples.
3. **[confirming-on-a-real-system.md](confirming-on-a-real-system.md)** — how
   to confirm the order inside PostgreSQL on your own nodes. With step 11 of
   command 1.b, it is the evidence that covers a distro's backported code.
4. **[limitations.md](limitations.md)** — the seven things to know before you
   act on a clean result, `LC_CTYPE` among them.

## Every document

| Document | What's in it |
|---|---|
| [commands.md](commands.md) | What each of the three commands does, what command 1.b adds, the file it takes from each machine and when it refuses one, and the same checks from separate pieces |
| [results.md](results.md) | The evidence behind each verdict, both worked examples, and what was tested on which nodes |
| [confirming-on-a-real-system.md](confirming-on-a-real-system.md) | Running the SQL template and the `C.UTF-8` probe on both nodes, choosing values that prove something, and the three traps that make a comparison lie |
| [scope.md](scope.md) | What this audits — `LC_COLLATE` and the `libc` provider — and the `builtin` provider as a way out |
| [limitations.md](limitations.md) | The seven things to know before acting on a clean result — `C.UTF-8` among them, what step 11 does not see, and `LC_CTYPE`, which no step audits |
| [requirements.md](requirements.md) | What each run needs, on your machine and on each server, and the three setup traps |
| [glossary.md](glossary.md) | `copy` graph, blast radius, hunk, tier, ellipsis range, role swap, build id, measured order, level, contraction |
| [../examples/](../examples/README.md) | Real output of every command on both audited pairs, a confirmation SQL script for each, and the `C.UTF-8` probe output |
| [../tests/README.md](../tests/README.md) | What the test suite covers, and what it does not |

---

[Back to the README](../README.md)
