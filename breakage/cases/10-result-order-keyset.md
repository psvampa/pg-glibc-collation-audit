# Case 10: result order and keyset pagination

The same query, in the same session, returns two different answers depending on the plan. An
index scan returns the order the index was written in and a sort uses the live rules.

```text
Node0 - glibc 2.28 (RHEL8)                       | Node1 - glibc 2.34 (RHEL9)
-------------------------------------------------+-------------------------------------------------
postgres=# -- ORDER BY answered by the index
postgres=# SELECT w FROM s1_words ORDER BY w LIMIT 6;
   w                                             |    w
--------                                         | --------
 va                                              |  va
 wa                                              |  wa
 vaa000                                          |  vaa000
 Vaa000                                          |  Vaa000
 waa000                                          |  waa000
 Waa000                                          |  Waa000
(6 rows)                                         | (6 rows)
                                                 |
postgres=# EXPLAIN (COSTS OFF) SELECT w FROM s1_words ORDER BY w LIMIT 6;
                   QUERY PLAN                    |                    QUERY PLAN
------------------------------------------------ | ------------------------------------------------
 Limit                                           |  Limit
   ->  Index Only Scan using s1_idx on s1_words  |    ->  Index Only Scan using s1_idx on s1_words
(2 rows)                                         | (2 rows)
                                                 |
postgres=# BEGIN;
BEGIN                                            | BEGIN
postgres=*# -- Disable indexes
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                              | SET
SET                                              | SET
SET                                              | SET
postgres=*# -- The same ORDER BY answered by a sort
postgres=*# SELECT w FROM s1_words ORDER BY w LIMIT 6;
   w                                             |    w
--------                                         | --------
 va                                              |  va
 wa                                              |  vaa000
 vaa000                                          |  Vaa000
 Vaa000                                          |  vaa001
 waa000                                          |  Vaa001
 Waa000                                          |  vaa002
(6 rows)                                         | (6 rows)
                                                 |
postgres=*# EXPLAIN (COSTS OFF) SELECT w FROM s1_words ORDER BY w LIMIT 6;
                QUERY PLAN                       |                 QUERY PLAN
------------------------------------------       | ------------------------------------------
 Limit                                           |  Limit
   ->  Sort                                      |    ->  Sort
         Sort Key: w COLLATE "sv_SE.utf8"        |          Sort Key: w COLLATE "sv_SE.utf8"
         ->  Seq Scan on s1_words                |          ->  Seq Scan on s1_words
(4 rows)                                         | (4 rows)
                                                 |
postgres=*# ROLLBACK;
ROLLBACK                                         | ROLLBACK
postgres=# -- Keyset pagination: the page after the word vind
postgres=# SELECT w FROM s1_words WHERE w > 'vind' ORDER BY w LIMIT 5;
   w                                             |    w
--------                                         | --------
 wind                                            |  wfu001
 vio000                                          |  Wfu001
 Vio000                                          |  wfu002
 wio000                                          |  Wfu002
 Wio000                                          |  wfu003
(5 rows)                                         | (5 rows)
                                                 |
postgres=# EXPLAIN (COSTS OFF) SELECT w FROM s1_words WHERE w > 'vind' ORDER BY w LIMIT 5;
                   QUERY PLAN                    |                    QUERY PLAN
------------------------------------------------ | ------------------------------------------------
 Limit                                           |  Limit
   ->  Index Only Scan using s1_idx on s1_words  |    ->  Index Only Scan using s1_idx on s1_words
         Index Cond: (w > 'vind'::text)          |          Index Cond: (w > 'vind'::text)
(3 rows)                                         | (3 rows)
```

## The script

Exactly what was fed to `psql` on each node:

```sql
-- ORDER BY answered by the index
SELECT w FROM s1_words ORDER BY w LIMIT 6;
EXPLAIN (COSTS OFF) SELECT w FROM s1_words ORDER BY w LIMIT 6;
BEGIN;
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- The same ORDER BY answered by a sort
SELECT w FROM s1_words ORDER BY w LIMIT 6;
EXPLAIN (COSTS OFF) SELECT w FROM s1_words ORDER BY w LIMIT 6;
ROLLBACK;
-- Keyset pagination: the page after the word vind
SELECT w FROM s1_words WHERE w > 'vind' ORDER BY w LIMIT 5;
EXPLAIN (COSTS OFF) SELECT w FROM s1_words WHERE w > 'vind' ORDER BY w LIMIT 5;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
