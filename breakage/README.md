# What a glibc collation change breaks inside PostgreSQL

Raw evidence for the cases described in the article. Every file holds the real `psql`
session, captured on two nodes that differ only in their operating system and therefore
in their glibc version, shown side by side.

The short version: a text index built under one glibc is a frozen copy of that version's
sort rules. A physical migration moves those files byte for byte. On the new system the
index is perfectly readable and quietly wrong, and so are several objects that are not
indexes at all.

| Case | What it shows | Is REINDEX enough? |
|---|---|---|
| [The change, in one query](cases/00-the-change.md) | sv_SE sorts differently | n/a |
| [Case 1: a B-tree index](cases/01-btree-index.md) | index finds nothing, table finds the row | REINDEX |
| [Case 2: a unique index or primary key](cases/02-unique-index.md) | a duplicate is accepted | REINDEX, after deleting the duplicates |
| [Case 3: a foreign key](cases/03-foreign-key.md) | both directions answer backwards | REINDEX the parent's index |
| [Case 4: LC_CTYPE](cases/04-lc-ctype.md) | lower() and character classes move, with no warning | partly, and nothing warns you |
| [Case 5: partial, BRIN and GiST](cases/05-partial-brin-gist.md) | each access method fails its own way | REINDEX |
| [Case 6: an EXCLUDE constraint](cases/06-exclude-constraint.md) | a conflicting value is accepted | REINDEX |
| [Case 7: a range-partitioned table](cases/07-range-partition.md) | 9,619 rows sit in the wrong partition | no, the rows must be moved |
| [Case 8: a CHECK constraint](cases/08-check-constraint.md) | 9,616 stored rows violate it, and it is still valid | no, it is never re-evaluated |
| [Case 9: generated column and materialized view](cases/09-generated-column-matview.md) | 9,616 stored values no longer match their definition | no, recompute |
| [Case 10: result order and keyset pagination](cases/10-result-order-keyset.md) | two answers in the same session | REINDEX |
| [Extra A: a sequential scan](cases/A-sequential-scan.md) | the same range returns half the rows | nothing to repair |
| [Extra B: the planner's statistics](cases/B-planner-statistics.md) | the row estimate collapses to 2 | ANALYZE |

## How to read these files

Left column is Node0, glibc 2.28 on Rocky Linux 8.9. Right column is Node1, glibc 2.34
on Rocky Linux 9.3, holding the data files Node0 wrote. Same PostgreSQL version, same
data, same queries.

Each file also carries the exact script that was fed to `psql`, so any of this can be
reproduced. See [environment.md](environment.md) for how the nodes were built.

PostgreSQL raises a collation version mismatch WARNING once per session on Node1. It is
omitted from the transcripts so the two columns line up, and it is shown on its own in
[environment.md](environment.md).

## Reproducing it

`scripts/01-build.sql` creates every object these cases use, on the old node, before
the migration. The corpus is 20,000 generated words starting with `v`, `V`, `w` or `W`,
plus seven readable Swedish ones, and every text column carries an explicit
`COLLATE "sv_SE.utf8"`.

Each case file carries the exact script that produced its session, so a case can be
re-run on its own once the schema exists.

`scripts/03-ctype-sweep.sql` and `scripts/03b-ctype-detail.sql` are what produced the
6,525 figure in [case 4](cases/04-lc-ctype.md). They sweep all 1,114,111 code points on
each node, first hashing by Unicode block and then listing the code points inside the
blocks that differ.

---

[Back to the README](../README.md)

