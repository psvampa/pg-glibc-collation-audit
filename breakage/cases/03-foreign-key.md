# Case 3: a foreign key

A foreign key has no index of its own. It is resolved against the parent's unique index,
so it inherits that index's error in both directions, and each direction fails in a
different way.

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

```sql
BEGIN;
-- Parent tuple exists. Inserting a child tuple pointing its parent.
INSERT INTO s3_child VALUES (999001, 'waa000');
ROLLBACK;
```

## Deleting a parent that has children

On Node0 the foreign key refuses, which is correct. On Node1 the statement reports
`DELETE 0` and removes nothing, because the lookup goes through the broken index and
does not find the row. The parent is still in the table, and so is its child. A
statement that should either delete the row or fail does neither, and says nothing.

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
postgres=*# -- Is that parent row still in the table?
postgres=*# SELECT count(*) AS parent_rows FROM s3_parent WHERE w = 'waa000';
 parent_rows                                                                                                         |  parent_rows
-------------                                                                                                        | -------------
           1                                                                                                         |            1
(1 row)                                                                                                              | (1 row)
                                                                                                                     |
postgres=*# -- And its child?
postgres=*# SELECT count(*) AS child_rows_for_that_parent FROM s3_child WHERE pw = 'waa000';
 child_rows_for_that_parent                                                                                          |  child_rows_for_that_parent
----------------------------                                                                                         | ----------------------------
                          1                                                                                          |                           1
(1 row)                                                                                                              | (1 row)
                                                                                                                     |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS child_rows_for_that_parent FROM s3_child WHERE pw = 'waa000';
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

```sql
\set ON_ERROR_ROLLBACK on
BEGIN;
-- Parent tuple has child tuples. It should not be possible to delete.
DELETE FROM s3_parent WHERE w = 'waa000';
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Is that parent row still in the table?
SELECT count(*) AS parent_rows FROM s3_parent WHERE w = 'waa000';
-- And its child?
SELECT count(*) AS child_rows_for_that_parent FROM s3_child WHERE pw = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) AS child_rows_for_that_parent FROM s3_child WHERE pw = 'waa000';
ROLLBACK;
```

## What does not happen

The obvious fear is that a parent gets deleted while its children stay behind. It does
not happen here. Locating the parent by `ctid`, which bypasses the index entirely, the
foreign key check runs and Node1 refuses the delete exactly like Node0. No orphan rows
were produced in this measurement.

```text
Node0 - glibc 2.28 (RHEL8)                                                                                           | Node1 - glibc 2.34 (RHEL9)
---------------------------------------------------------------------------------------------------------------------+---------------------------------------------------------------------------------------------------------------------
postgres=# -- Find the parent without the index, and keep its physical location
postgres=# BEGIN;
BEGIN                                                                                                                | BEGIN
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                                                                                  | SET
SET                                                                                                                  | SET
SET                                                                                                                  | SET
postgres=*# SELECT ctid AS pctid FROM s3_parent WHERE w = 'waa000' \gset
postgres=*# COMMIT;
COMMIT                                                                                                               | COMMIT
postgres=# -- Delete it by ctid, with indexes enabled. The foreign key check decides now.
postgres=# BEGIN;
BEGIN                                                                                                                | BEGIN
postgres=*# DELETE FROM s3_parent WHERE ctid = :'pctid';
ERROR:  update or delete on table "s3_parent" violates foreign key constraint "s3_child_pw_fkey" on table "s3_child" | ERROR:  update or delete on table "s3_parent" violates foreign key constraint "s3_child_pw_fkey" on table "s3_child"
DETAIL:  Key (w)=(waa000) is still referenced from table "s3_child".                                                 | DETAIL:  Key (w)=(waa000) is still referenced from table "s3_child".
postgres=!# ROLLBACK;
ROLLBACK                                                                                                             | ROLLBACK
```

```sql
-- Find the parent without the index, and keep its physical location
BEGIN;
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SELECT ctid AS pctid FROM s3_parent WHERE w = 'waa000' \gset
COMMIT;
-- Delete it by ctid, with indexes enabled. The foreign key check decides now.
BEGIN;
DELETE FROM s3_parent WHERE ctid = :'pctid';
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL
raises once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
