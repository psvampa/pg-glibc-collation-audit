# Case 4: LC_CTYPE, which PostgreSQL does not version

`LC_COLLATE` decides sort order and `LC_CTYPE` decides what counts as a letter and what
`lower()` returns. Only the first one has a version number. A sweep of the whole Unicode
catalogue found 6,525 characters that answer differently between the two builds.

## Two characters that changed

```text
Node0 - glibc 2.28 (RHEL8)             | Node1 - glibc 2.34 (RHEL9)
---------------------------------------+---------------------------------------
postgres=# -- Characters that differ between glibc versions.
postgres=# SELECT lower(E'Ʂ') AS lower_of_u_a7c5, E'ࢾ' ~ '[[:alpha:]]' AS u_08be_is_a_letter;
 lower_of_u_a7c5 | u_08be_is_a_letter  |  lower_of_u_a7c5 | u_08be_is_a_letter
-----------------+-------------------- | -----------------+--------------------
 Ʂ               | f                   |  ʂ               | t
(1 row)                                | (1 row)
```

## A functional index on lower(login)

```text
Node0 - glibc 2.28 (RHEL8)                                     | Node1 - glibc 2.34 (RHEL9)
---------------------------------------------------------------+---------------------------------------------------------------
postgres=# -- A query over functional index
postgres=# SELECT count(*) AS via_index FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
 via_index                                                     |  via_index
-----------                                                    | -----------
         1                                                     |          0
(1 row)                                                        | (1 row)
                                                               |
postgres=# EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
                          QUERY PLAN                           |                           QUERY PLAN
-------------------------------------------------------------- | --------------------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Bitmap Heap Scan on s4_lower                            |    ->  Bitmap Heap Scan on s4_lower
         Recheck Cond: (lower(login) = 'userꟅname'::text)      |          Recheck Cond: (lower(login) = 'userʂname'::text)
         ->  Bitmap Index Scan on s4_lower_idx                 |          ->  Bitmap Index Scan on s4_lower_idx
               Index Cond: (lower(login) = 'userꟅname'::text)  |                Index Cond: (lower(login) = 'userʂname'::text)
(5 rows)                                                       | (5 rows)
                                                               |
postgres=# BEGIN;
BEGIN                                                          | BEGIN
postgres=*# SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
SET                                                            | SET
SET                                                            | SET
SET                                                            | SET
postgres=*# -- Same query over table (without index)
postgres=*# SELECT count(*) AS via_table FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
 via_table                                                     |  via_table
-----------                                                    | -----------
         1                                                     |          1
(1 row)                                                        | (1 row)
                                                               |
postgres=*# EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
                     QUERY PLAN                                |                      QUERY PLAN
----------------------------------------------------           | ----------------------------------------------------
 Aggregate                                                     |  Aggregate
   ->  Seq Scan on s4_lower                                    |    ->  Seq Scan on s4_lower
         Filter: (lower(login) = 'userꟅname'::text)            |          Filter: (lower(login) = 'userʂname'::text)
(3 rows)                                                       | (3 rows)
                                                               |
postgres=*# ROLLBACK;
ROLLBACK                                                       | ROLLBACK
```

## A CHECK constraint on character class

```text
Node0 - glibc 2.28 (RHEL8)                                    | Node1 - glibc 2.34 (RHEL9)
--------------------------------------------------------------+----------------------------------------------------------------------------------------
postgres=# \d s4_class
             Table "public.s4_class"                          |              Table "public.s4_class"
 Column | Type | Collation  | Nullable | Default              |  Column | Type | Collation  | Nullable | Default
--------+------+------------+----------+---------             | --------+------+------------+----------+---------
 code   | text | sv_SE.utf8 |          |                      |  code   | text | sv_SE.utf8 |          |
Check constraints:                                            | Check constraints:
    "s4_class_code_check" CHECK (code !~ '[[:alpha:]]'::text) |     "s4_class_code_check" CHECK (code !~ '[[:alpha:]]'::text)
                                                              |
postgres=# -- Verify existing tuple
postgres=# SELECT code FROM s4_class WHERE code LIKE '7%';
 code                                                         |  code
------                                                        | ------
 7ࢾ9                                                          |  7ࢾ9
(1 row)                                                       | (1 row)
                                                              |
postgres=# BEGIN;
BEGIN                                                         | BEGIN
postgres=*# -- Insert a new tuple with same value. CHECK constraint validates the character
postgres=*# INSERT INTO s4_class SELECT code FROM s4_class WHERE code LIKE '7%';
INSERT 0 1                                                    | ERROR:  new row for relation "s4_class" violates check constraint "s4_class_code_check"
postgres=*# ROLLBACK;                                         | DETAIL:  Failing row contains (7ࢾ9).
ROLLBACK                                                      | postgres=!# ROLLBACK;
                                                              | ROLLBACK
```

## The script

Exactly what was fed to `psql` on each node:

```sql
-- Characters that differ between glibc versions.
SELECT lower(E'Ʂ') AS lower_of_u_a7c5, E'ࢾ' ~ '[[:alpha:]]' AS u_08be_is_a_letter;
```

```sql
-- A query over functional index
SELECT count(*) AS via_index FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
EXPLAIN (COSTS OFF) SELECT count(*) AS via_index FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
BEGIN;
SET LOCAL enable_indexscan = off;SET LOCAL enable_bitmapscan = off;SET LOCAL enable_indexonlyscan = off;
-- Same query over table (without index)
SELECT count(*) AS via_table FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
EXPLAIN (COSTS OFF) SELECT count(*) AS via_table FROM s4_lower WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
ROLLBACK;
```

```sql
\d s4_class
-- Verify existing tuple
SELECT code FROM s4_class WHERE code LIKE '7%';
BEGIN;
-- Insert a new tuple with same value. CHECK constraint validates the character
INSERT INTO s4_class SELECT code FROM s4_class WHERE code LIKE '7%';
ROLLBACK;
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
