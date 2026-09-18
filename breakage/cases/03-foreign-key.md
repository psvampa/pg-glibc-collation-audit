# Case 3: a foreign key

A foreign key has no index of its own. It is resolved against the parent's unique index, so
it inherits that index's error in both directions.

## Inserting a child whose parent exists

```text
Node0 - glibc 2.28 (RHEL8) | Node1 - glibc 2.34 (RHEL9)
---------------------------+------------------------------------------------------------------------------------------------
postgres=# BEGIN;
BEGIN                      | BEGIN
postgres=*# -- Parent tuple exists. Inserting a child tuple pointing its parent.
postgres=*# INSERT INTO s3_child VALUES (999001, 'waa000');
INSERT 0 1                 | ERROR:  insert or update on table "s3_child" violates foreign key constraint "s3_child_pw_fkey"
postgres=*# ROLLBACK;      | DETAIL:  Key (pw)=(waa000) is not present in table "s3_parent".
ROLLBACK                   | postgres=!# ROLLBACK;
                           | ROLLBACK
```

## Deleting a parent that has children

```text
Node0 - glibc 2.28 (RHEL8)                                                                                           | Node1 - glibc 2.34 (RHEL9)
---------------------------------------------------------------------------------------------------------------------+----------------------------------------
postgres=# \set ON_ERROR_ROLLBACK on
postgres=# BEGIN;
BEGIN                                                                                                                | BEGIN
postgres=*# -- Parent tuple has child tuples. It should not be possible to delete.
postgres=*# DELETE FROM s3_parent WHERE w = 'waa000';
ERROR:  update or delete on table "s3_parent" violates foreign key constraint "s3_child_pw_fkey" on table "s3_child" | DELETE 0
DETAIL:  Key (w)=(waa000) is still referenced from table "s3_child".                                                 |
postgres=*# -- Disable indexes
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                                                                                  | SET
SET                                                                                                                  | SET
SET                                                                                                                  | SET
postgres=*# SELECT count(*) AS children_pointing_at_a_deleted_parent FROM s3_child WHERE pw = 'waa000';
 children_pointing_at_a_deleted_parent                                                                               |  children_pointing_at_a_deleted_parent
---------------------------------------                                                                              | ---------------------------------------
                                     1                                                                               |                                      1
(1 row)                                                                                                              | (1 row)
                                                                                                                     |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS children_pointing_at_a_deleted_parent FROM s3_child WHERE pw = 'waa000';
              QUERY PLAN                                                                                             |               QUERY PLAN
---------------------------------------                                                                              | ---------------------------------------
 Aggregate                                                                                                           |  Aggregate
   ->  Seq Scan on s3_child                                                                                          |    ->  Seq Scan on s3_child
         Filter: (pw = 'waa000'::text)                                                                               |          Filter: (pw = 'waa000'::text)
(3 rows)                                                                                                             | (3 rows)
                                                                                                                     |
postgres=*# ROLLBACK;
ROLLBACK                                                                                                             | ROLLBACK
```

## The script

Exactly what was fed to `psql` on each node:

```sql
BEGIN;
-- Parent tuple exists. Inserting a child tuple pointing its parent.
INSERT INTO s3_child VALUES (999001, 'waa000');
ROLLBACK;
```

```sql
\set ON_ERROR_ROLLBACK on
BEGIN;
-- Parent tuple has child tuples. It should not be possible to delete.
DELETE FROM s3_parent WHERE w = 'waa000';
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS children_pointing_at_a_deleted_parent FROM s3_child WHERE pw = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) AS children_pointing_at_a_deleted_parent FROM s3_child WHERE pw = 'waa000';
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
