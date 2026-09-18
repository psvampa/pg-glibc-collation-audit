# Case 5: partial, BRIN and GiST indexes

No access method is immune, and each one fails in its own way. The partial index returns
rows that no longer satisfy its own predicate, GiST loses part of the range, and BRIN loses
rows only for some predicates, which makes it the hardest to diagnose.

## Partial index

```text
Node0 - glibc 2.28 (RHEL8)                           | Node1 - glibc 2.34 (RHEL9)
-----------------------------------------------------+-----------------------------------------------------
postgres=# BEGIN;
BEGIN                                                | BEGIN
postgres=*# -- Force the index path, so both nodes are compared the same way
postgres=*# SET LOCAL enable_seqscan = off;
SET                                                  | SET
postgres=*# -- Query using partial index (WHERE w < 'vz')
postgres=*# SELECT count(*) AS via_index FROM s5a_partial WHERE w < 'vz';
 via_index                                           |  via_index
-----------                                          | -----------
     19232                                           |      19232
(1 row)                                              | (1 row)
                                                     |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s5a_partial WHERE w < 'vz';
                     QUERY PLAN                      |                      QUERY PLAN
---------------------------------------------------- | ----------------------------------------------------
 Aggregate                                           |  Aggregate
   ->  Index Only Scan using s5a_idx on s5a_partial  |    ->  Index Only Scan using s5a_idx on s5a_partial
(2 rows)                                             | (2 rows)
                                                     |
postgres=*# ROLLBACK;
ROLLBACK                                             | ROLLBACK
postgres=# BEGIN;
BEGIN                                                | BEGIN
postgres=*# -- Disable indexes
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                  | SET
SET                                                  | SET
SET                                                  | SET
postgres=*# -- Query without using partial index (WHERE w < 'vz')
postgres=*# SELECT count(*) AS via_table FROM s5a_partial WHERE w < 'vz';
 via_table                                           |  via_table
-----------                                          | -----------
     19232                                           |       9616
(1 row)                                              | (1 row)
                                                     |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s5a_partial WHERE w < 'vz';
            QUERY PLAN                               |             QUERY PLAN
----------------------------------                   | ----------------------------------
 Aggregate                                           |  Aggregate
   ->  Seq Scan on s5a_partial                       |    ->  Seq Scan on s5a_partial
         Filter: (w < 'vz'::text)                    |          Filter: (w < 'vz'::text)
(3 rows)                                             | (3 rows)
                                                     |
postgres=*# ROLLBACK;
ROLLBACK                                             | ROLLBACK
```

## GiST

```text
Node0 - glibc 2.28 (RHEL8)                                     | Node1 - glibc 2.34 (RHEL9)
---------------------------------------------------------------+---------------------------------------------------------------
postgres=# -- Query using GiST index
postgres=# SELECT count(*) AS via_index FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
 via_index                                                     |  via_index
-----------                                                    | -----------
       772                                                     |        145
(1 row)                                                        | (1 row)
                                                               |
postgres=# EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
                          QUERY PLAN                           |                           QUERY PLAN
-------------------------------------------------------------- | --------------------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Index Only Scan using s5c_idx on s5c_gist               |    ->  Index Only Scan using s5c_idx on s5c_gist
         Index Cond: ((w >= 'wa'::text) AND (w < 'wb'::text))  |          Index Cond: ((w >= 'wa'::text) AND (w < 'wb'::text))
(3 rows)                                                       | (3 rows)
                                                               |
postgres=# BEGIN;
BEGIN                                                          | BEGIN
postgres=*# -- Disable indexes
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                            | SET
SET                                                            | SET
SET                                                            | SET
postgres=*# -- Same query without using the index
postgres=*# SELECT count(*) AS via_table FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
 via_table                                                     |  via_table
-----------                                                    | -----------
       772                                                     |        386
(1 row)                                                        | (1 row)
                                                               |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
                        QUERY PLAN                             |                         QUERY PLAN
----------------------------------------------------------     | ----------------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Seq Scan on s5c_gist                                    |    ->  Seq Scan on s5c_gist
         Filter: ((w >= 'wa'::text) AND (w < 'wb'::text))      |          Filter: ((w >= 'wa'::text) AND (w < 'wb'::text))
(3 rows)                                                       | (3 rows)
                                                               |
postgres=*# ROLLBACK;
ROLLBACK                                                       | ROLLBACK
```

## BRIN

```text
Node0 - glibc 2.28 (RHEL8)                                                | Node1 - glibc 2.34 (RHEL9)
--------------------------------------------------------------------------+--------------------------------------------------------------------------
postgres=# -- Query using BRIN index
postgres=# SELECT count(*) AS via_index FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
 via_index                                                                |  via_index
-----------                                                               | -----------
        28                                                                |          0
(1 row)                                                                   | (1 row)
                                                                          |
postgres=# EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
                               QUERY PLAN                                 |                                QUERY PLAN
------------------------------------------------------------------------- | -------------------------------------------------------------------------
 Aggregate                                                                |  Aggregate
   ->  Bitmap Heap Scan on s5b_brin                                       |    ->  Bitmap Heap Scan on s5b_brin
         Recheck Cond: ((w >= 'vfx'::text) AND (w < 'vfxzzz'::text))      |          Recheck Cond: ((w >= 'vfx'::text) AND (w < 'vfxzzz'::text))
         ->  Bitmap Index Scan on s5b_idx                                 |          ->  Bitmap Index Scan on s5b_idx
               Index Cond: ((w >= 'vfx'::text) AND (w < 'vfxzzz'::text))  |                Index Cond: ((w >= 'vfx'::text) AND (w < 'vfxzzz'::text))
(5 rows)                                                                  | (5 rows)
                                                                          |
postgres=# BEGIN;
BEGIN                                                                     | BEGIN
postgres=*# -- Disable bitmap scan, the only access path to a BRIN index
postgres=*# SET LOCAL enable_bitmapscan = off;
SET                                                                       | SET
postgres=*# -- Same query without using the BRIN index
postgres=*# SELECT count(*) AS via_table FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
 via_table                                                                |  via_table
-----------                                                               | -----------
        28                                                                |         14
(1 row)                                                                   | (1 row)
                                                                          |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
                          QUERY PLAN                                      |                           QUERY PLAN
---------------------------------------------------------------           | ---------------------------------------------------------------
 Aggregate                                                                |  Aggregate
   ->  Seq Scan on s5b_brin                                               |    ->  Seq Scan on s5b_brin
         Filter: ((w >= 'vfx'::text) AND (w < 'vfxzzz'::text))            |          Filter: ((w >= 'vfx'::text) AND (w < 'vfxzzz'::text))
(3 rows)                                                                  | (3 rows)
                                                                          |
postgres=*# ROLLBACK;
ROLLBACK                                                                  | ROLLBACK
```

## The script

Exactly what was fed to `psql` on each node:

```sql
BEGIN;
-- Force the index path, so both nodes are compared the same way
SET LOCAL enable_seqscan = off;
-- Query using partial index (WHERE w < 'vz')
SELECT count(*) AS via_index FROM s5a_partial WHERE w < 'vz';
EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s5a_partial WHERE w < 'vz';
ROLLBACK;
BEGIN;
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Query without using partial index (WHERE w < 'vz')
SELECT count(*) AS via_table FROM s5a_partial WHERE w < 'vz';
EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s5a_partial WHERE w < 'vz';
ROLLBACK;
```

```sql
-- Query using GiST index
SELECT count(*) AS via_index FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
BEGIN;
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Same query without using the index
SELECT count(*) AS via_table FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
ROLLBACK;
```

```sql
-- Query using BRIN index
SELECT count(*) AS via_index FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
BEGIN;
-- Disable bitmap scan, the only access path to a BRIN index
SET LOCAL enable_bitmapscan = off;
-- Same query without using the BRIN index
SELECT count(*) AS via_table FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
