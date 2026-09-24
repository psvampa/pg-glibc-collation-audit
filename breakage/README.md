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

[The repair, in order](repair.md) is the run that turns the broken node back into a
working one, including the two steps that fail on purpose.

## Finding the affected objects in your own database

Five scripts in [`scripts/`](scripts/), each one standalone, each one reporting the objects
a change in glibc's sort order or character rules can break, including those that text
search, pg_trgm or citext tie to the database's LC_CTYPE. Run them in every database. Each
one opens with a
warning that it decides from the collations of the columns an object reads, not from its
expressions, so an object built on text taken from a column that has no collation, such as
`doc->>'name'` on a jsonb column, can be missing from its list.

Tested on PostgreSQL 14 through 18 with
[`tests/find-affected-selftest.sql`](tests/find-affected-selftest.sql), which builds a set of
objects in throwaway databases and checks what each script reports about them. It creates
and drops databases, so run it on a test server, never in production.

| Script | What it finds |
|---|---|
| [find-affected-indexes.sql](scripts/find-affected-indexes.sql) | indexes, including the partial and expression indexes the query on the PostgreSQL wiki does not reach, the trigram and text search indexes that follow the database's LC_CTYPE, and btree indexes on jsonb |
| [find-affected-check-constraints.sql](scripts/find-affected-check-constraints.sql) | CHECK constraints, which are never re-evaluated on their own |
| [find-affected-range-partitions.sql](scripts/find-affected-range-partitions.sql) | partitioned tables, by range on a text or jsonb column, and by any strategy on a citext column or on an expression over a text column |
| [find-affected-generated-columns.sql](scripts/find-affected-generated-columns.sql) | stored generated columns, whose value was computed once and written down |
| [find-affected-materialized-views.sql](scripts/find-affected-materialized-views.sql) | materialized views, which hold their own copy of the rows |

Only the first one is repaired by a REINDEX. Each script says in its own header what breaks,
why no REINDEX helps, and what it leaves out.

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

Four scripts, run in order, in the [three states](environment.md#the-three-states):

| Script | When | What it does |
|---|---|---|
| [01-build.sql](scripts/01-build.sql) | state A, once | creates every object these cases use, on the old node, before the migration |
| [01b-helpers.sql](scripts/01b-helpers.sql) | before each probe | two functions that put "was this accepted?" and "is this index well ordered?" on stdout, where they can be diffed |
| [02-probe.sql](scripts/02-probe.sql) | A, B and C, unchanged | asks every scene the same questions and modifies nothing: the inserts and deletes run inside transactions that are rolled back |
| [04-repair.sql](scripts/04-repair.sql) | state B, once | the repair run, in order. It is [repair.md](repair.md) |

The corpus is 20,000 generated words starting with `v`, `V`, `w` or `W`, plus seven
readable Swedish ones. Every text column the cases sort or compare on carries an explicit
`COLLATE "sv_SE.utf8"`, except the control table, which carries `en_US.utf8`. The `note`
and `pad` columns carry no collation of their own and nothing sorts on them.

Each case file carries the exact script that produced its session, so a case can be
re-run on its own once the schema exists. `02-probe.sql` is the consolidated form of
those per-case scripts, written to be run whole; it is not the text any single case
publishes.

`scripts/03-ctype-sweep.sql` and `scripts/03b-ctype-detail.sql` are what produced the
6,525 figure in [case 4](cases/04-lc-ctype.md). They sweep 1,112,063 code points on
each node, first hashing by Unicode block and then listing the code points inside the
blocks that differ. That is every code point but `U+0000` and the 2,048 surrogates,
which `chr()` rejects in UTF8.

---

[Back to the README](../README.md)

