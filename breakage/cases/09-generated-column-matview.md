# Case 9: a stored generated column and a materialized view

The expression of a generated column must be immutable, and text comparison is immutable
according to the catalogue. The value was computed once, stored, and today the same
expression returns something else.

A materialized view is the same problem one level up: it holds its own copy of the rows,
in the order they had when it was built.

```text
Node0 - glibc 2.28 (RHEL8)                                          | Node1 - glibc 2.34 (RHEL9)
--------------------------------------------------------------------+----------------------------------------------------------------
postgres=# -- The value stored in the generated column
postgres=# SELECT bucket, count(*) FROM s9_gen GROUP BY 1 ORDER BY 1;
 bucket | count                                                     |  bucket | count
--------+-------                                                    | --------+-------
 early  | 19232                                                     |  early  | 19232
 late   |   768                                                     |  late   |   768
(2 rows)                                                            | (2 rows)
                                                                    |
postgres=# -- The same expression, recomputed now
postgres=# SELECT CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END AS recomputed, count(*) FROM s9_gen GROUP BY 1 ORDER BY 1;
 recomputed | count                                                 |  recomputed | count
------------+-------                                                | ------------+-------
 early      | 19232                                                 |  early      |  9616
 late       |   768                                                 |  late       | 10384
(2 rows)                                                            | (2 rows)
                                                                    |
postgres=# -- Rows whose stored value no longer matches its own definition
postgres=# SELECT count(*) AS rows_whose_stored_value_is_wrong FROM s9_gen WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
 rows_whose_stored_value_is_wrong                                   |  rows_whose_stored_value_is_wrong
----------------------------------                                  | ----------------------------------
                                0                                   |                              9616
(1 row)                                                             | (1 row)
                                                                    |
postgres=# -- The order the materialized view holds, recomputed from the table
postgres=# SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;
 pos |   w                                                          |  pos |   w
-----+--------                                                      | -----+--------
   1 | va                                                           |    1 | va
   2 | wa                                                           |    2 | wa
   3 | vaa000                                                       |    3 | vaa000
   4 | Vaa000                                                       |    4 | Vaa000
   5 | waa000                                                       |    5 | waa000
   6 | Waa000                                                       |    6 | Waa000
(6 rows)                                                            | (6 rows)
                                                                    |
postgres=# -- Which plan answered that
postgres=# EXPLAIN (COSTS OFF) SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;
                            QUERY PLAN                              |                             QUERY PLAN
------------------------------------------------------------------- | -------------------------------------------------------------------
 Limit                                                              |  Limit
   ->  Sort                                                         |    ->  Sort
         Sort Key: (row_number() OVER w1)                           |          Sort Key: (row_number() OVER w1)
         ->  WindowAgg                                              |          ->  WindowAgg
               Window: w1 AS (ORDER BY w ROWS UNBOUNDED PRECEDING)  |                Window: w1 AS (ORDER BY w ROWS UNBOUNDED PRECEDING)
               ->  Index Only Scan using s1_idx on s1_words         |                ->  Index Only Scan using s1_idx on s1_words
(6 rows)                                                            | (6 rows)
                                                                    |
postgres=# BEGIN;
BEGIN                                                               | BEGIN
                                                                    |
postgres=*# -- Disable indexes
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                                 | SET
SET                                                                 | SET
SET                                                                 | SET
                                                                    |
postgres=*# -- Same query without using the index
postgres=*# SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;
 pos |   w                                                          |  pos |   w
-----+--------                                                      | -----+--------
   1 | va                                                           |    1 | va
   2 | wa                                                           |    2 | vaa000
   3 | vaa000                                                       |    3 | Vaa000
   4 | Vaa000                                                       |    4 | vaa001
   5 | waa000                                                       |    5 | Vaa001
   6 | Waa000                                                       |    6 | vaa002
(6 rows)                                                            | (6 rows)
                                                                    |
postgres=*# ROLLBACK;
ROLLBACK                                                            | ROLLBACK
                                                                    |
```

The materialized view's order is asked twice on purpose. The first answer is the same on
both nodes, which reads as "nothing moved". `EXPLAIN` shows why: the plan ends in
`Index Only Scan using s1_idx`, so PostgreSQL never sorted by `w` -- it returned the order
the index recorded under glibc 2.28. Asked again with the index taken away, Node0 answers
the same as before and Node1 does not.

## The script

Exactly what was fed to `psql` on each node:

```sql
-- The value stored in the generated column
SELECT bucket, count(*) FROM s9_gen GROUP BY 1 ORDER BY 1;
-- The same expression, recomputed now
SELECT CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END AS recomputed, count(*) FROM s9_gen GROUP BY 1 ORDER BY 1;
-- Rows whose stored value no longer matches its own definition
SELECT count(*) AS rows_whose_stored_value_is_wrong FROM s9_gen WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
-- The order the materialized view holds, recomputed from the table
SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;
-- Which plan answered that
EXPLAIN (COSTS OFF) SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;
BEGIN;
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Same query without using the index
SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
