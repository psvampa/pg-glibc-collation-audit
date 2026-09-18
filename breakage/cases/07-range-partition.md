# Case 7: a range-partitioned table

No index is broken here. The rows are physically in the wrong partition, because the bound
meant something else when they were written. The planner prunes the partition that holds
them and they become invisible.

```text
Node0 - glibc 2.28 (RHEL8)                     | Node1 - glibc 2.34 (RHEL9)
-----------------------------------------------+-----------------------------------------------
postgres=# -- Where the rows are physically stored
postgres=# SELECT tableoid::regclass AS partition, count(*) FROM s7_part GROUP BY 1 ORDER BY 1;
 partition  | count                            |  partition  | count
------------+-------                           | ------------+-------
 s7_part_lo | 19238                            |  s7_part_lo | 19238
 s7_part_hi |   769                            |  s7_part_hi |   769
(2 rows)                                       | (2 rows)
                                               |
postgres=# -- Rows in the low partition that no longer belong there
postgres=# SELECT count(*) AS rows_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';
 rows_in_the_wrong_partition                   |  rows_in_the_wrong_partition
-----------------------------                  | -----------------------------
                           0                   |                         9619
(1 row)                                        | (1 row)
                                               |
postgres=# -- One row, asked through the partitioned table
postgres=# SELECT count(*) FROM s7_part WHERE w = 'waa000';
 count                                         |  count
-------                                        | -------
     1                                         |      0
(1 row)                                        | (1 row)
                                               |
postgres=# EXPLAIN (COSTS OFF) SELECT count(*) FROM s7_part WHERE w = 'waa000';
              QUERY PLAN                       |               QUERY PLAN
--------------------------------------         | --------------------------------------
 Aggregate                                     |  Aggregate
   ->  Seq Scan on s7_part_lo s7_part          |    ->  Seq Scan on s7_part_hi s7_part
         Filter: (w = 'waa000'::text)          |          Filter: (w = 'waa000'::text)
(3 rows)                                       | (3 rows)
                                               |
postgres=# BEGIN;
BEGIN                                          | BEGIN
postgres=*# -- The same query with partition pruning turned off
postgres=*# SET LOCAL enable_partition_pruning = off;
SET                                            | SET
postgres=*# SELECT count(*) FROM s7_part WHERE w = 'waa000';
 count                                         |  count
-------                                        | -------
     1                                         |      1
(1 row)                                        | (1 row)
                                               |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) FROM s7_part WHERE w = 'waa000';
                  QUERY PLAN                   |                   QUERY PLAN
---------------------------------------------- | ----------------------------------------------
 Aggregate                                     |  Aggregate
   ->  Append                                  |    ->  Append
         ->  Seq Scan on s7_part_lo s7_part_1  |          ->  Seq Scan on s7_part_lo s7_part_1
               Filter: (w = 'waa000'::text)    |                Filter: (w = 'waa000'::text)
         ->  Seq Scan on s7_part_hi s7_part_2  |          ->  Seq Scan on s7_part_hi s7_part_2
               Filter: (w = 'waa000'::text)    |                Filter: (w = 'waa000'::text)
(6 rows)                                       | (6 rows)
                                               |
postgres=*# ROLLBACK;
ROLLBACK                                       | ROLLBACK
postgres=# BEGIN;
BEGIN                                          | BEGIN
postgres=*# -- Where a new copy of that same row would be routed today
postgres=*# INSERT INTO s7_part VALUES ('waa000', 'routed now');
INSERT 0 1                                     | INSERT 0 1
postgres=*# SELECT tableoid::regclass AS routed_to FROM s7_part WHERE note = 'routed now';
 routed_to                                     |  routed_to
------------                                   | ------------
 s7_part_lo                                    |  s7_part_hi
(1 row)                                        | (1 row)
                                               |
postgres=*# ROLLBACK;
ROLLBACK                                       | ROLLBACK
```

## The script

Exactly what was fed to `psql` on each node:

```sql
-- Where the rows are physically stored
SELECT tableoid::regclass AS partition, count(*) FROM s7_part GROUP BY 1 ORDER BY 1;
-- Rows in the low partition that no longer belong there
SELECT count(*) AS rows_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';
-- One row, asked through the partitioned table
SELECT count(*) FROM s7_part WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s7_part WHERE w = 'waa000';
BEGIN;
-- The same query with partition pruning turned off
SET LOCAL enable_partition_pruning = off;
SELECT count(*) FROM s7_part WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s7_part WHERE w = 'waa000';
ROLLBACK;
BEGIN;
-- Where a new copy of that same row would be routed today
INSERT INTO s7_part VALUES ('waa000', 'routed now');
SELECT tableoid::regclass AS routed_to FROM s7_part WHERE note = 'routed now';
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
