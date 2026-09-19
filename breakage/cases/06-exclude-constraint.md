# Case 6: an EXCLUDE constraint

An EXCLUDE constraint is an index underneath, so it fails the same way the unique index
does.

```text
Node0 - glibc 2.28 (RHEL8)                                                   | Node1 - glibc 2.34 (RHEL9)
-----------------------------------------------------------------------------+---------------------------------------------------
postgres=# \d s6_excl
             Table "public.s6_excl"                                          |              Table "public.s6_excl"
 Column | Type | Collation  | Nullable | Default                             |  Column | Type | Collation  | Nullable | Default
--------+------+------------+----------+---------                            | --------+------+------------+----------+---------
 w      | text | sv_SE.utf8 |          |                                     |  w      | text | sv_SE.utf8 |          |
Indexes:                                                                     | Indexes:
    "s6_excl_w_excl" EXCLUDE USING gist (w WITH =)                           |     "s6_excl_w_excl" EXCLUDE USING gist (w WITH =)
                                                                             |
postgres=# BEGIN;
BEGIN                                                                        | BEGIN
postgres=*# -- The value is already there. The exclusion constraint should reject a second copy.
postgres=*# INSERT INTO s6_excl VALUES ('waa000');
ERROR:  conflicting key value violates exclusion constraint "s6_excl_w_excl" | INSERT 0 1
DETAIL:  Key (w)=(waa000) conflicts with existing key (w)=(waa000).          | postgres=*# ROLLBACK;
postgres=!# ROLLBACK;                                                        | ROLLBACK
ROLLBACK                                                                     |
```

## The script

Exactly what was fed to `psql` on each node:

```sql
\d s6_excl
BEGIN;
-- The value is already there. The exclusion constraint should reject a second copy.
INSERT INTO s6_excl VALUES ('waa000');
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
