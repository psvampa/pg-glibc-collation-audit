# Case 2: a unique index or primary key

Uniqueness is enforced by searching the index before inserting. When that search cannot find
the row that is already there, the insert is accepted and the constraint stops being true.

```text
Node0 - glibc 2.28 (RHEL8)                                            | Node1 - glibc 2.34 (RHEL9)
----------------------------------------------------------------------+--------------------------------------------------------------------------------------------------------------------------------------
postgres=# BEGIN;
BEGIN                                                                 | BEGIN
postgres=*# -- Search using index
postgres=*# SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
 copies_in_the_table                                                  |  copies_in_the_table
---------------------                                                 | ---------------------
                   1                                                  |                    0
(1 row)                                                               | (1 row)
                                                                      |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
                     QUERY PLAN                                       |                      QUERY PLAN
-----------------------------------------------------                 | -----------------------------------------------------
 Aggregate                                                            |  Aggregate
   ->  Index Only Scan using s2_uniq_pkey on s2_uniq                  |    ->  Index Only Scan using s2_uniq_pkey on s2_uniq
         Index Cond: (w = 'waa000'::text)                             |          Index Cond: (w = 'waa000'::text)
(3 rows)                                                              | (3 rows)
                                                                      |
postgres=*# -- Disable indexes
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                                   | SET
SET                                                                   | SET
SET                                                                   | SET
postgres=*# -- Search without using index
postgres=*# SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
 copies_in_the_table                                                  |  copies_in_the_table
---------------------                                                 | ---------------------
                   1                                                  |                    1
(1 row)                                                               | (1 row)
                                                                      |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
              QUERY PLAN                                              |               QUERY PLAN
--------------------------------------                                | --------------------------------------
 Aggregate                                                            |  Aggregate
   ->  Seq Scan on s2_uniq                                            |    ->  Seq Scan on s2_uniq
         Filter: (w = 'waa000'::text)                                 |          Filter: (w = 'waa000'::text)
(3 rows)                                                              | (3 rows)
                                                                      |
postgres=*# COMMIT;
COMMIT                                                                | COMMIT
postgres=# -- Insert a second copy of the same value
postgres=# BEGIN;
BEGIN                                                                 | BEGIN
postgres=*# INSERT INTO s2_uniq VALUES ('waa000', 'a second copy');
ERROR:  duplicate key value violates unique constraint "s2_uniq_pkey" | INSERT 0 1
DETAIL:  Key (w)=(waa000) already exists.                             | postgres=*# ROLLBACK;
postgres=!# ROLLBACK;                                                 | ROLLBACK
ROLLBACK                                                              |
postgres=# -- amcheck on the PK
postgres=# SELECT bt_index_check('s2_uniq_pkey'::regclass);
 bt_index_check                                                       | ERROR:  item order invariant violated for index "s2_uniq_pkey"
----------------                                                      | DETAIL:  Lower index tid=(3,10) (points to index tid=(19,1)) higher index tid=(3,11) (points to index tid=(69,1)) page lsn=0/A7CC990.
                                                                      |
(1 row)                                                               |
```

## The script

Exactly what was fed to `psql` on each node:

```sql
BEGIN;
-- Search using index
SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
-- Disable indexes
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Search without using index
SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
COMMIT;
-- Insert a second copy of the same value
BEGIN;
INSERT INTO s2_uniq VALUES ('waa000', 'a second copy');
ROLLBACK;
-- amcheck on the PK
SELECT bt_index_check('s2_uniq_pkey'::regclass);
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
