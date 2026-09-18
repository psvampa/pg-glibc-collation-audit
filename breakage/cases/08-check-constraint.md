# Case 8: a CHECK constraint

A CHECK is evaluated on insert and never evaluated again. Rows that stopped satisfying it
stay where they are, and the catalogue still marks the constraint as valid.

```text
Node0 - glibc 2.28 (RHEL8)                        | Node1 - glibc 2.34 (RHEL9)
--------------------------------------------------+-------------------------------------------------------------------------------------
postgres=# \d s8_check
             Table "public.s8_check"              |              Table "public.s8_check"
 Column | Type | Collation  | Nullable | Default  |  Column | Type | Collation  | Nullable | Default
--------+------+------------+----------+--------- | --------+------+------------+----------+---------
 w      | text | sv_SE.utf8 |          |          |  w      | text | sv_SE.utf8 |          |
Check constraints:                                | Check constraints:
    "s8_check_w_check" CHECK (w < 'vz'::text)     |     "s8_check_w_check" CHECK (w < 'vz'::text)
                                                  |
postgres=# -- Stored rows that violate their own CHECK constraint
postgres=# SELECT count(*) AS stored_rows_that_violate_it FROM s8_check WHERE NOT (w < 'vz');
 stored_rows_that_violate_it                      |  stored_rows_that_violate_it
-----------------------------                     | -----------------------------
                           0                      |                         9616
(1 row)                                           | (1 row)
                                                  |
postgres=# BEGIN;
BEGIN                                             | BEGIN
postgres=*# -- Insert a row identical to ones already stored
postgres=*# INSERT INTO s8_check VALUES ('waa000');
INSERT 0 1                                        | ERROR:  new row for relation "s8_check" violates check constraint "s8_check_w_check"
postgres=*# ROLLBACK;                             | DETAIL:  Failing row contains (waa000).
ROLLBACK                                          | postgres=!# ROLLBACK;
                                                  | ROLLBACK
```

## The script

Exactly what was fed to `psql` on each node:

```sql
\d s8_check
-- Stored rows that violate their own CHECK constraint
SELECT count(*) AS stored_rows_that_violate_it FROM s8_check WHERE NOT (w < 'vz');
BEGIN;
-- Insert a row identical to ones already stored
INSERT INTO s8_check VALUES ('waa000');
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
