# The environment

Two nodes, different operating systems, physical streaming replication between them.

| | Node0 | Node1 |
|---|---|---|
| OS | Rocky Linux 8.9 | Rocky Linux 9.3 |
| glibc | `glibc-2.28-251.el8_10.40` | `glibc-2.34-275.el9_8` |
| PostgreSQL | 18.6 (PGDG) | 18.6 (PGDG) |
| locales | 867 | 869 |

The data was loaded on Node0 and reached Node1 through physical replication. Node1 was
then promoted, so it holds exactly the index files that glibc 2.28 wrote, read by glibc
2.34. That is what makes the comparison valid. A dump and restore would have rebuilt
every index under the new rules and hidden all of it.

## The three states

`scripts/02-probe.sql` is one file, run unchanged in three states. Every case in this
folder is state A beside state B.

| State | Node | What it is |
|---|---|---|
| A | Node0 | glibc 2.28, the node the data was written on |
| B | Node1 | glibc 2.34, the same data files, before any repair |
| C | Node1 | glibc 2.34, after `scripts/04-repair.sql` has run |

**The right-hand column of every case is state B**, a promoted replica on which nothing
has been repaired yet. That is deliberate: it is the state a database is in on the
morning after the migration, and it is the only state in which the damage can be seen.
State C is [repair.md](repair.md).

The locale under test is `sv_SE.utf8`, which changed between these two glibc versions.
Every text column the cases sort or compare on carries an explicit
`COLLATE "sv_SE.utf8"`, except the control table below, which carries `en_US.utf8`.

## Controls

Three controls run alongside the cases, because a measurement like this fails quietly
when the environment is wrong.

- The same corpus sorted with `COLLATE "C"` must stay identical on both nodes. If it ever
  matched the `sv_SE` ordering, the locale was never generated and silently fell back to
  `C`, and the two nodes would agree with each other while proving nothing.
- The same table, corpus and index under `en_US.utf8`, a locale this glibc pair does not
  change. Nothing there is allowed to move, and nothing did, `bt_index_check()` included.
- Every query is asked twice, once through the index and once with index scans disabled,
  each with its own `EXPLAIN`, so the plan is evidence and not an assumption.

## The warning

After the promotion, the first statement of every session that touches the collation
raises this on Node1:

```
WARNING:  collation "sv_SE.utf8" has version mismatch
DETAIL:  The collation in the database was created using version 2.28, but the operating system provides version 2.34.
HINT:  Rebuild all objects affected by this collation and run ALTER COLLATION pg_catalog."sv_SE.utf8" REFRESH VERSION, or build PostgreSQL with the right library version.
```

It is omitted from the transcripts so the two columns line up. Two things are worth
knowing about it: the identical message is raised for `en_US.utf8`, whose ordering did
not move at all, because it compares glibc version strings and not behaviour. And there
is no equivalent warning for `LC_CTYPE`, so the 6,525 characters of case 4 leave no
trace anywhere.
