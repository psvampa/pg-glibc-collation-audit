# Extra B: the planner's statistics

The histogram ANALYZE stores is a list ordered by the collation of the day it ran, and the
planner binary-searches it. After the glibc change that list is no longer sorted for the
server reading it, so the row estimate is nonsense. ANALYZE rebuilds it.

## The estimate against the real rows

```text
Node0 - glibc 2.28 (RHEL8)                                              | Node1 - glibc 2.34 (RHEL9)
------------------------------------------------------------------------+---------------------------------------------------------------------------------------
postgres=# -- What the planner estimates for this predicate
postgres=# EXPLAIN SELECT count(*) FROM s5a_partial WHERE w < 'vz';
                              QUERY PLAN                                |                                       QUERY PLAN
----------------------------------------------------------------------- | --------------------------------------------------------------------------------------
 Aggregate  (cost=545.43..545.44 rows=1 width=8)                        |  Aggregate  (cost=4.32..4.33 rows=1 width=8)
   ->  Seq Scan on s5a_partial  (cost=0.00..497.00 rows=19374 width=0)  |    ->  Index Only Scan using s5a_idx on s5a_partial  (cost=0.29..4.32 rows=2 width=0)
         Filter: (w < 'vz'::text)                                       | (2 rows)
(3 rows)                                                                |
                                                                        |
postgres=# -- How many rows actually match, counted without any index
postgres=# BEGIN;
BEGIN                                                                   | BEGIN
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                                     | SET
SET                                                                     | SET
SET                                                                     | SET
postgres=*# SELECT count(*) AS actual_rows FROM s5a_partial WHERE w < 'vz';
 actual_rows                                                            |  actual_rows
-------------                                                           | -------------
       19232                                                            |         9616
(1 row)                                                                 | (1 row)
                                                                        |
postgres=*# ROLLBACK;
ROLLBACK                                                                | ROLLBACK
postgres=# -- The stored statistics themselves, byte for byte
postgres=# SELECT md5(histogram_bounds::text) AS histogram_hash FROM pg_stats WHERE tablename = 's5a_partial' AND attname = 'w';
          histogram_hash                                                |           histogram_hash
----------------------------------                                      | ----------------------------------
 b58dffbbbb4439fbddc01a2234fb3020                                       |  b58dffbbbb4439fbddc01a2234fb3020
(1 row)                                                                 | (1 row)
                                                                        |
postgres=# SELECT (histogram_bounds::text::text[])[1:4] AS first_four_bounds FROM pg_stats WHERE tablename = 's5a_partial' AND attname = 'w';
       first_four_bounds                                                |        first_four_bounds
-------------------------------                                         | -------------------------------
 {vaa000,Wag001,Wam004,Wat005}                                          |  {vaa000,Wag001,Wam004,Wat005}
(1 row)                                                                 | (1 row)
```

## ANALYZE, inside a transaction that is rolled back

```text
Node0 - glibc 2.28 (RHEL8)                                              | Node1 - glibc 2.34 (RHEL9)
------------------------------------------------------------------------+--------------------------------------------------------------------------------------------
postgres=# -- ANALYZE inside a transaction, so the statistics can be rolled back
postgres=# BEGIN;
BEGIN                                                                   | BEGIN
postgres=*# ANALYZE s5a_partial;
ANALYZE                                                                 | ANALYZE
postgres=*# EXPLAIN SELECT count(*) FROM s5a_partial WHERE w < 'vz';
                              QUERY PLAN                                |                                         QUERY PLAN
----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------
 Aggregate  (cost=545.43..545.44 rows=1 width=8)                        |  Aggregate  (cost=390.54..390.55 rows=1 width=8)
   ->  Seq Scan on s5a_partial  (cost=0.00..497.00 rows=19374 width=0)  |    ->  Index Only Scan using s5a_idx on s5a_partial  (cost=0.29..366.11 rows=9774 width=0)
         Filter: (w < 'vz'::text)                                       | (2 rows)
(3 rows)                                                                |
                                                                        |
postgres=*# ROLLBACK;
ROLLBACK                                                                | ROLLBACK
postgres=# -- back to the statistics the node had before
postgres=# EXPLAIN SELECT count(*) FROM s5a_partial WHERE w < 'vz';
                              QUERY PLAN                                |                                       QUERY PLAN
----------------------------------------------------------------------- | --------------------------------------------------------------------------------------
 Aggregate  (cost=545.43..545.44 rows=1 width=8)                        |  Aggregate  (cost=4.32..4.33 rows=1 width=8)
   ->  Seq Scan on s5a_partial  (cost=0.00..497.00 rows=19374 width=0)  |    ->  Index Only Scan using s5a_idx on s5a_partial  (cost=0.29..4.31 rows=2 width=0)
         Filter: (w < 'vz'::text)                                       | (2 rows)
(3 rows)                                                                |
```

## The script

Exactly what was fed to `psql` on each node:

```sql
-- What the planner estimates for this predicate
EXPLAIN SELECT count(*) FROM s5a_partial WHERE w < 'vz';
-- How many rows actually match, counted without any index
BEGIN;
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS actual_rows FROM s5a_partial WHERE w < 'vz';
ROLLBACK;
-- The stored statistics themselves, byte for byte
SELECT md5(histogram_bounds::text) AS histogram_hash FROM pg_stats WHERE tablename = 's5a_partial' AND attname = 'w';
SELECT (histogram_bounds::text::text[])[1:4] AS first_four_bounds FROM pg_stats WHERE tablename = 's5a_partial' AND attname = 'w';
```

```sql
-- ANALYZE inside a transaction, so the statistics can be rolled back
BEGIN;
ANALYZE s5a_partial;
EXPLAIN SELECT count(*) FROM s5a_partial WHERE w < 'vz';
ROLLBACK;
-- back to the statistics the node had before
EXPLAIN SELECT count(*) FROM s5a_partial WHERE w < 'vz';
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
