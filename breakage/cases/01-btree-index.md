# Case 1: a B-tree index

The index holds the order glibc 2.28 wrote. Looking up a value under the 2.34 rules walks
the tree to where the word belongs today, which is not where it is stored. The row is in the
table and the index cannot find it.

```text
Node0 - glibc 2.28 (RHEL8)                                     | Node1 - glibc 2.34 (RHEL9)
---------------------------------------------------------------+----------------------------------------------------------------------------------------------------------------------------------
postgres=# -- Query using index
postgres=# SELECT count(*) FROM s1_words WHERE w = 'waa000';
 count                                                         |  count
-------                                                        | -------
     1                                                         |      0
(1 row)                                                        | (1 row)
                                                               |
postgres=# EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w = 'waa000';
                   QUERY PLAN                                  |                    QUERY PLAN
------------------------------------------------               | ------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Index Only Scan using s1_idx on s1_words                |    ->  Index Only Scan using s1_idx on s1_words
         Index Cond: (w = 'waa000'::text)                      |          Index Cond: (w = 'waa000'::text)
(3 rows)                                                       | (3 rows)
                                                               |
postgres=# SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
 count                                                         |  count
-------                                                        | -------
   775                                                         |      0
(1 row)                                                        | (1 row)
                                                               |
postgres=# EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
                          QUERY PLAN                           |                           QUERY PLAN
-------------------------------------------------------------- | --------------------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Index Only Scan using s1_idx on s1_words                |    ->  Index Only Scan using s1_idx on s1_words
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
postgres=*# -- Same queries without using index
postgres=*# SELECT count(*) FROM s1_words WHERE w = 'waa000';
 count                                                         |  count
-------                                                        | -------
     1                                                         |      1
(1 row)                                                        | (1 row)
                                                               |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w = 'waa000';
              QUERY PLAN                                       |               QUERY PLAN
--------------------------------------                         | --------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Seq Scan on s1_words                                    |    ->  Seq Scan on s1_words
         Filter: (w = 'waa000'::text)                          |          Filter: (w = 'waa000'::text)
(3 rows)                                                       | (3 rows)
                                                               |
postgres=*# SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
 count                                                         |  count
-------                                                        | -------
   775                                                         |    388
(1 row)                                                        | (1 row)
                                                               |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
                        QUERY PLAN                             |                         QUERY PLAN
----------------------------------------------------------     | ----------------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Seq Scan on s1_words                                    |    ->  Seq Scan on s1_words
         Filter: ((w >= 'wa'::text) AND (w < 'wb'::text))      |          Filter: ((w >= 'wa'::text) AND (w < 'wb'::text))
(3 rows)                                                       | (3 rows)
                                                               |
postgres=*# ROLLBACK;
ROLLBACK                                                       | ROLLBACK
postgres=# -- amcheck on the index
postgres=# SELECT bt_index_check('s1_idx'::regclass);
 bt_index_check                                                | ERROR:  item order invariant violated for index "s1_idx"
----------------                                               | DETAIL:  Lower index tid=(3,4) (points to index tid=(5,1)) higher index tid=(3,5) (points to index tid=(6,1)) page lsn=0/A4AF298.
                                                               |
(1 row)                                                        |
```

## The script

Exactly what was fed to `psql` on each node:

```sql
-- Query using index
SELECT count(*) FROM s1_words WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w = 'waa000';
SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
BEGIN;
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Same queries without using index
SELECT count(*) FROM s1_words WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w = 'waa000';
SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w >= 'wa' AND w < 'wb';
ROLLBACK;
-- amcheck on the index
SELECT bt_index_check('s1_idx'::regclass);
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
