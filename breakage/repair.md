# The repair, in order

The order matters more than the individual commands. This is the real run on the
glibc 2.34 node, the one that turns [state B into state C](environment.md#the-three-states),
captured with `psql -X -e -f scripts/04-repair.sql 2>&1`, so the commands are echoed and
stderr is merged. Two steps fail on purpose and those failures are the evidence.

One helper appears in step 0, defined in [`scripts/01b-helpers.sql`](scripts/01b-helpers.sql).
`check_index(ix)` calls `bt_index_check(ix)` and returns the error text instead of
aborting the transaction, so the answer lands on stdout where it can be diffed between
states:

```sql
CREATE OR REPLACE FUNCTION check_index(ix regclass) RETURNS text AS $$
BEGIN
  PERFORM bt_index_check(ix);
  RETURN 'ok';
EXCEPTION
  WHEN index_corrupted OR data_corrupted THEN
    RETURN 'CORRUPT: ' || SQLERRM;
  WHEN others THEN
    -- not a B-tree, not an index, no amcheck: all of these used to read
    -- CORRUPT, on both nodes alike. A name that does not resolve at all never
    -- reaches here -- the regclass cast raises in the caller, which stops the
    -- run under ON_ERROR_STOP.
    RETURN 'NOT ASKED: ' || SQLERRM;
END $$ LANGUAGE plpgsql;
```

`CORRUPT` is an answer about the index. `NOT ASKED` is the other thing that can happen —
the table is not there, amcheck is not installed — and it is a separate word on purpose:
reported as `CORRUPT`, a missing extension would read the same on both nodes and the two
sides would agree while proving nothing.

## Step 0: the wrong move, shown first because it is the common one

It rewrites the version recorded in the catalogue, and that is all it does. The index
still answers zero for a row the table holds, and amcheck still calls it corrupt. Whoever
runs this first loses the only signal they had.

```text
-- REFRESH VERSION silences the warning.  It repairs nothing.
ALTER COLLATION pg_catalog."sv_SE.utf8" REFRESH VERSION;
ALTER COLLATION
SELECT collname, collversion, pg_collation_actual_version(oid) AS os_provides
FROM pg_collation WHERE collname = 'sv_SE.utf8';
  collname  | collversion | os_provides 
------------+-------------+-------------
 sv_SE.utf8 | 2.34        | 2.34
(1 row)

SET enable_seqscan = off;
SET
SELECT count(*) AS still_not_found_via_index FROM s1_words WHERE w = 'waa000';
 still_not_found_via_index 
---------------------------
                         0
(1 row)

RESET enable_seqscan;
RESET
SELECT check_index('s1_idx') AS s1_idx_after_refresh_version;
               s1_idx_after_refresh_version                
-----------------------------------------------------------
 CORRUPT: item order invariant violated for index "s1_idx"
(1 row)
```

## Step 1: what production did in the meantime

The migration is done and the application is writing again. A single INSERT is enough to
leave behind damage that no REINDEX can undo.

```text
-- one ordinary INSERT, accepted because the unique index cannot see the
-- row that is already there.  This is the damage that outlives REINDEX.
INSERT INTO s2_uniq VALUES ('waa000', 'inserted by the application after the migration');
INSERT 0 1
SET enable_seqscan = on;
SET
SET enable_indexscan = off;
SET
SET enable_bitmapscan = off;
SET
SET enable_indexonlyscan = off;
SET
SELECT count(*) AS copies_of_waa000_now FROM s2_uniq WHERE w = 'waa000';
 copies_of_waa000_now 
----------------------
                    2
(1 row)

RESET enable_seqscan;
RESET
RESET enable_indexscan;
RESET
RESET enable_bitmapscan;
RESET
RESET enable_indexonlyscan;
RESET
```

## Step 2: REINDEX, and the index that refuses to be rebuilt

Eleven indexes rebuild without complaint and the primary key refuses. The WARNING in the
middle belongs to en_US.utf8, a collation whose order did not move at all.

```text
REINDEX INDEX s1_idx;
REINDEX
REINDEX INDEX s3_parent_pkey;
REINDEX
REINDEX INDEX s3_child_pw;
REINDEX
REINDEX INDEX s4_lower_idx;
REINDEX
REINDEX INDEX s4_expr_idx;
REINDEX
REINDEX INDEX s5a_idx;
REINDEX
REINDEX INDEX s5b_idx;
REINDEX
REINDEX INDEX s5c_idx;
REINDEX
REINDEX INDEX s6_excl_w_excl;
REINDEX
REINDEX INDEX s9_mv_idx;
REINDEX
REINDEX INDEX c0_idx;
psql:/tmp/run.sql:37: WARNING:  collation "en_US.utf8" has version mismatch
DETAIL:  The collation in the database was created using version 2.28, but the operating system provides version 2.34.
HINT:  Rebuild all objects affected by this collation and run ALTER COLLATION pg_catalog."en_US.utf8" REFRESH VERSION, or build PostgreSQL with the right library version.
REINDEX
-- this one fails, and names the duplicate that step 1 let in
REINDEX INDEX s2_uniq_pkey;
psql:/tmp/run.sql:39: ERROR:  could not create unique index "s2_uniq_pkey"
DETAIL:  Key (w)=(waa000) is duplicated.
```

## Step 3: find and remove the duplicates REINDEX refuses to rebuild

The duplicates have to be found with a sequential scan, because the index that would find
them is the broken one. Which copy survives is a decision for a person. Only then does the
primary key rebuild.

```text
SET enable_seqscan = on;
SET
SET enable_indexscan = off;
SET
SET enable_bitmapscan = off;
SET
SET enable_indexonlyscan = off;
SET
SELECT w, count(*) AS copies FROM s2_uniq GROUP BY w HAVING count(*) > 1 ORDER BY w;
   w    | copies 
--------+--------
 waa000 |      2
(1 row)

DELETE FROM s2_uniq a USING s2_uniq b
 WHERE a.w = b.w AND a.ctid > b.ctid;
DELETE 1
RESET enable_seqscan;
RESET
RESET enable_indexscan;
RESET
RESET enable_bitmapscan;
RESET
RESET enable_indexonlyscan;
RESET
REINDEX INDEX s2_uniq_pkey;
REINDEX
```

## Step 4: the partitioned table, which no REINDEX can help

No REINDEX reaches this. The rows are physically in the wrong partition, and they have to
be deleted from the child and reinserted into the parent so tuple routing places them
again.

```text
SELECT count(*) AS rows_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';
 rows_in_the_wrong_partition 
-----------------------------
                        9619
(1 row)

WITH moved AS (
  DELETE FROM s7_part_lo WHERE w >= 'vz' RETURNING w, note
)
INSERT INTO s7_part SELECT w, note FROM moved;
INSERT 0 9619
SELECT tableoid::regclass AS partition, count(*) AS rows FROM s7_part GROUP BY 1 ORDER BY 1;
 partition  | rows  
------------+-------
 s7_part_lo |  9619
 s7_part_hi | 10388
(2 rows)

SELECT count(*) AS rows_still_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';
 rows_still_in_the_wrong_partition 
-----------------------------------
                                 0
(1 row)
```

## Step 5: the CHECK constraint nobody revalidated

A CHECK is never re-evaluated on its own, so nothing here fails until it is asked to.
Where the 9,616 offending rows end up is a business decision, not a technical one. They
are moved to a quarantine table so the run can continue.

```text
SELECT count(*) AS rows_violating_the_check FROM s8_check WHERE NOT (w < 'vz');
 rows_violating_the_check 
--------------------------
                     9616
(1 row)

-- dropping and re-adding it is how you find out, and it fails
ALTER TABLE s8_check DROP CONSTRAINT s8_check_w_check;
ALTER TABLE
ALTER TABLE s8_check ADD CONSTRAINT s8_check_w_check CHECK (w < 'vz') NOT VALID;
ALTER TABLE
ALTER TABLE s8_check VALIDATE CONSTRAINT s8_check_w_check;
psql:/tmp/run.sql:63: ERROR:  check constraint "s8_check_w_check" of relation "s8_check" is violated by some row
-- the rows have to go somewhere before the constraint can be trusted
CREATE TABLE IF NOT EXISTS s8_quarantine (w text COLLATE "sv_SE.utf8");
CREATE TABLE
WITH bad AS (DELETE FROM s8_check WHERE NOT (w < 'vz') RETURNING w)
INSERT INTO s8_quarantine SELECT w FROM bad;
INSERT 0 9616
SELECT count(*) AS quarantined FROM s8_quarantine;
 quarantined 
-------------
        9616
(1 row)

ALTER TABLE s8_check VALIDATE CONSTRAINT s8_check_w_check;
ALTER TABLE
```

## Step 6: the stored generated column, recomputed

The stored generated column holds a value computed under rules that no longer exist. SET
EXPRESSION rewrites the table and takes the stale count to zero.

```text
SELECT count(*) AS stale_rows_before FROM s9_gen
 WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
 stale_rows_before 
-------------------
              9616
(1 row)

ALTER TABLE s9_gen ALTER COLUMN bucket
  SET EXPRESSION AS (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
ALTER TABLE
SELECT count(*) AS stale_rows_after FROM s9_gen
 WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
 stale_rows_after 
------------------
                0
(1 row)
```

## Step 7: the materialized view, refreshed

The materialized view keeps its own copy of the rows, in its own order, with its own
indexes, so it needs a refresh of its own.

```text
REFRESH MATERIALIZED VIEW s9_mv;
REFRESH MATERIALIZED VIEW
```

**This step has to come after step 2, and that is not a matter of taste.** A refresh
re-runs the view's own query, which is the one [case 9](cases/09-generated-column-matview.md)
publishes: on the migrated node its plan is `Index Only Scan using s1_idx`, with or without
a `LIMIT`. Run before that index is rebuilt, the refresh reads the order glibc 2.28 wrote
and stores it again. It prints `REFRESH MATERIALIZED VIEW`, raises nothing, and leaves the
view exactly as wrong as it was.

## Step 8: the CHECK that depends on LC_CTYPE, not LC_COLLATE

The CHECK that depends on LC_CTYPE rather than LC_COLLATE. Nothing warned about this one,
and the row it now rejects is a row the database already holds.

```text
SELECT count(*) AS rows_violating_the_class_check FROM s4_class WHERE code ~ '[[:alpha:]]';
 rows_violating_the_class_check 
--------------------------------
                              1
(1 row)

CREATE TABLE IF NOT EXISTS s4_quarantine (code text COLLATE "sv_SE.utf8");
CREATE TABLE
WITH bad AS (DELETE FROM s4_class WHERE code ~ '[[:alpha:]]' RETURNING code)
INSERT INTO s4_quarantine SELECT code FROM bad;
INSERT 0 1
SELECT count(*) AS quarantined FROM s4_quarantine;
 quarantined 
-------------
           1
(1 row)
```

## Step 9: only now, record the new collation version

Only now is it safe to record the new version, and to run ANALYZE so the planner stops
reading a histogram sorted under the old rules.

```text
ALTER COLLATION pg_catalog."en_US.utf8" REFRESH VERSION;
ALTER COLLATION
ALTER DATABASE postgres REFRESH COLLATION VERSION;
ALTER DATABASE
ANALYZE;
ANALYZE
```

## The script

[`scripts/04-repair.sql`](scripts/04-repair.sql), and the helper it loads,
[`scripts/01b-helpers.sql`](scripts/01b-helpers.sql). The transcript above was captured
before the file gained its guards: the object check that refuses to run on a database
`01-build.sql` did not build or that a previous run already quarantined rows in, the
helpers loaded rather than assumed, `ON_ERROR_STOP` left on except around the two steps
that fail on purpose, and the database name read from the connection instead of being
written as `postgres`. Not one repair statement changed, so the run above is what this
script still does; a reader running it today also sees the guard.
