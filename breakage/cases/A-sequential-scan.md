# Extra A: a sequential scan

Not corruption. With every index disabled, the same range returns half the rows on the new
system, because the range itself means something else. The rows that leave are the ones
starting with V, which no longer sort between `wa` and `wb`. Nothing to repair here.

```text
Node0 - glibc 2.28 (RHEL8)                                              | Node1 - glibc 2.34 (RHEL9)
------------------------------------------------------------------------+------------------------------------------------------------------------
postgres=# BEGIN;
BEGIN                                                                   | BEGIN
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                                     | SET
SET                                                                     | SET
SET                                                                     | SET
postgres=*# SELECT count(*) AS total_in_range,
postgres-*#        count(*) FILTER (WHERE w ~ '^[Ww]') AS start_with_w, | postgres-*#        count(*) FILTER (WHERE w ~ '^[Ww]') AS start_with_w,
postgres-*#        count(*) FILTER (WHERE w ~ '^[Vv]') AS start_with_v  | postgres-*#        count(*) FILTER (WHERE w ~ '^[Vv]') AS start_with_v
postgres-*#   FROM s1_words WHERE w >= 'wa' AND w < 'wb';               | postgres-*#   FROM s1_words WHERE w >= 'wa' AND w < 'wb';
 total_in_range | start_with_w | start_with_v                           |  total_in_range | start_with_w | start_with_v
----------------+--------------+--------------                          | ----------------+--------------+--------------
            775 |          388 |          387                           |             388 |          388 |            0
(1 row)                                                                 | (1 row)
                                                                        |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS total_in_range,
postgres-*#        count(*) FILTER (WHERE w ~ '^[Ww]') AS start_with_w, | postgres-*#        count(*) FILTER (WHERE w ~ '^[Ww]') AS start_with_w,
postgres-*#        count(*) FILTER (WHERE w ~ '^[Vv]') AS start_with_v  | postgres-*#        count(*) FILTER (WHERE w ~ '^[Vv]') AS start_with_v
postgres-*#   FROM s1_words WHERE w >= 'wa' AND w < 'wb';               | postgres-*#   FROM s1_words WHERE w >= 'wa' AND w < 'wb';
                        QUERY PLAN                                      |                         QUERY PLAN
----------------------------------------------------------              | ----------------------------------------------------------
 Aggregate                                                              |  Aggregate
   ->  Seq Scan on s1_words                                             |    ->  Seq Scan on s1_words
         Filter: ((w >= 'wa'::text) AND (w < 'wb'::text))               |          Filter: ((w >= 'wa'::text) AND (w < 'wb'::text))
(3 rows)                                                                | (3 rows)
                                                                        |
postgres=*# ROLLBACK;
ROLLBACK                                                                | ROLLBACK
```

## The script

Exactly what was fed to `psql` on each node:

```sql
BEGIN;
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS total_in_range,
       count(*) FILTER (WHERE w ~ '^[Ww]') AS start_with_w,
       count(*) FILTER (WHERE w ~ '^[Vv]') AS start_with_v
  FROM s1_words WHERE w >= 'wa' AND w < 'wb';
EXPLAIN (COSTS OFF) SELECT count(*) AS total_in_range,
       count(*) FILTER (WHERE w ~ '^[Ww]') AS start_with_w,
       count(*) FILTER (WHERE w ~ '^[Vv]') AS start_with_v
  FROM s1_words WHERE w >= 'wa' AND w < 'wb';
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
