-- The repair, on the glibc 2.34 node, in the order a DBA would actually do it.
-- Run after 02-probe.sql has recorded state B.
--
-- THIS SCRIPT MODIFIES DATA.  It rebuilds indexes, deletes duplicate rows,
-- moves rows between partitions, rewrites a table and quarantines rows that
-- no longer satisfy their CHECK.  It is written for the database
-- 01-build.sql builds and refuses to run on any other; see the guard below.
--
-- Two steps fail on purpose and those failures are the evidence, so each of
-- them, and only each of them, turns ON_ERROR_STOP off around itself.
\pset pager off
\set ON_ERROR_STOP on
SET client_min_messages = warning;

-- Load the helpers rather than check that something by that name exists: the
-- name is not the body. A node can carry an older try_sql/check_index from a
-- previous run, and a name check passes over it. CREATE OR REPLACE makes this
-- idempotent, and \ir resolves next to this file, so the two travel together.
\ir 01b-helpers.sql

-- The guard.  Without it this script silences the version-mismatch warning in
-- step 9 on a database it repaired nothing in, and exits 0 -- a clean result
-- produced by not having looked.
DO $guard$
DECLARE missing text;
BEGIN
  SELECT string_agg(t, ', ' ORDER BY t) INTO missing
    FROM unnest(ARRAY['s1_words','s2_uniq','s3_parent','s3_child','s4_class',
                      's4_expr','s4_lower','s5a_partial','s5b_brin','s5c_gist',
                      's6_excl','s7_part','s7_part_lo','s7_part_hi','s8_check',
                      's9_gen','s9_mv','c0_control',
                      -- every index this script reindexes, by name
                      's1_idx','s2_uniq_pkey','s3_parent_pkey','s3_child_pw',
                      's4_lower_idx','s4_expr_idx','s5a_idx','s5b_idx',
                      's5c_idx','s6_excl_w_excl','s9_mv_idx','c0_idx']) AS t
   WHERE to_regclass(t) IS NULL;
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'not the database 01-build.sql built: % absent', missing;
  END IF;
  IF to_regclass('s8_check') IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM pg_constraint
        WHERE conrelid = 's8_check'::regclass AND conname = 's8_check_w_check') THEN
    RAISE EXCEPTION 'constraint s8_check_w_check absent: step 5 would not be a repair';
  END IF;
  IF to_regclass('s8_quarantine') IS NOT NULL
     OR to_regclass('s4_quarantine') IS NOT NULL THEN
    RAISE EXCEPTION 'a previous run already quarantined rows here: the counts '
                    'this script prints would include that run''s rows';
  END IF;
END $guard$;

\echo '#### step 0: the wrong move, shown first because it is the common one ####'
\echo '-- REFRESH VERSION silences the warning.  It repairs nothing.'
ALTER COLLATION pg_catalog."sv_SE.utf8" REFRESH VERSION;
SELECT collname, collversion, pg_collation_actual_version(oid) AS os_provides
FROM pg_collation WHERE collname = 'sv_SE.utf8';
SET enable_seqscan = off;
SELECT count(*) AS still_not_found_via_index FROM s1_words WHERE w = 'waa000';
RESET enable_seqscan;
SELECT check_index('s1_idx') AS s1_idx_after_refresh_version;

\echo '#### step 1: what production did in the meantime ####'
\echo '-- one ordinary INSERT, accepted because the unique index cannot see the'
\echo '-- row that is already there.  This is the damage that outlives REINDEX.'
INSERT INTO s2_uniq VALUES ('waa000', 'inserted by the application after the migration');
SET enable_seqscan = on; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_indexonlyscan = off;
SELECT count(*) AS copies_of_waa000_now FROM s2_uniq WHERE w = 'waa000';
RESET enable_seqscan; RESET enable_indexscan; RESET enable_bitmapscan; RESET enable_indexonlyscan;

\echo '#### step 2: REINDEX, and the index that refuses to be rebuilt ####'
REINDEX INDEX s1_idx;
REINDEX INDEX s3_parent_pkey;
REINDEX INDEX s3_child_pw;
REINDEX INDEX s4_lower_idx;
REINDEX INDEX s4_expr_idx;
REINDEX INDEX s5a_idx;
REINDEX INDEX s5b_idx;
REINDEX INDEX s5c_idx;
REINDEX INDEX s6_excl_w_excl;
REINDEX INDEX s9_mv_idx;
REINDEX INDEX c0_idx;
\echo '-- this one fails, and names the duplicate that step 1 let in'
\set ON_ERROR_STOP off
REINDEX INDEX s2_uniq_pkey;
\set ON_ERROR_STOP on

\echo '#### step 3: find and remove the duplicates REINDEX refuses to rebuild ####'
SET enable_seqscan = on; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_indexonlyscan = off;
SELECT w, count(*) AS copies FROM s2_uniq GROUP BY w HAVING count(*) > 1 ORDER BY w;
DELETE FROM s2_uniq a USING s2_uniq b
 WHERE a.w = b.w AND a.ctid > b.ctid;
RESET enable_seqscan; RESET enable_indexscan; RESET enable_bitmapscan; RESET enable_indexonlyscan;
REINDEX INDEX s2_uniq_pkey;

\echo '#### step 4: the partitioned table, which no REINDEX can help ####'
SELECT count(*) AS rows_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';
WITH moved AS (
  DELETE FROM s7_part_lo WHERE w >= 'vz' RETURNING w, note
)
INSERT INTO s7_part SELECT w, note FROM moved;
SELECT tableoid::regclass AS partition, count(*) AS rows FROM s7_part GROUP BY 1 ORDER BY 1;
SELECT count(*) AS rows_still_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';

\echo '#### step 5: the CHECK constraint nobody revalidated ####'
SELECT count(*) AS rows_violating_the_check FROM s8_check WHERE NOT (w < 'vz');
\echo '-- dropping and re-adding it is how you find out, and it fails'
ALTER TABLE s8_check DROP CONSTRAINT s8_check_w_check;
ALTER TABLE s8_check ADD CONSTRAINT s8_check_w_check CHECK (w < 'vz') NOT VALID;
\set ON_ERROR_STOP off
ALTER TABLE s8_check VALIDATE CONSTRAINT s8_check_w_check;
\set ON_ERROR_STOP on
\echo '-- the rows have to go somewhere before the constraint can be trusted'
CREATE TABLE IF NOT EXISTS s8_quarantine (w text COLLATE "sv_SE.utf8");
WITH bad AS (DELETE FROM s8_check WHERE NOT (w < 'vz') RETURNING w)
INSERT INTO s8_quarantine SELECT w FROM bad;
SELECT count(*) AS quarantined FROM s8_quarantine;
ALTER TABLE s8_check VALIDATE CONSTRAINT s8_check_w_check;

\echo '#### step 6: the stored generated column, recomputed ####'
SELECT count(*) AS stale_rows_before FROM s9_gen
 WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
-- SET EXPRESSION needs PostgreSQL 17.  Before that, drop the column and add
-- it back, which rewrites the table the same way.
ALTER TABLE s9_gen ALTER COLUMN bucket
  SET EXPRESSION AS (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
SELECT count(*) AS stale_rows_after FROM s9_gen
 WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);

\echo '#### step 7: the materialized view, refreshed ####'
REFRESH MATERIALIZED VIEW s9_mv;

\echo '#### step 8: the CHECK that depends on LC_CTYPE, not LC_COLLATE ####'
SELECT count(*) AS rows_violating_the_class_check FROM s4_class WHERE code ~ '[[:alpha:]]';
CREATE TABLE IF NOT EXISTS s4_quarantine (code text COLLATE "sv_SE.utf8");
WITH bad AS (DELETE FROM s4_class WHERE code ~ '[[:alpha:]]' RETURNING code)
INSERT INTO s4_quarantine SELECT code FROM bad;
SELECT count(*) AS quarantined FROM s4_quarantine;

\echo '#### step 9: only now, record the new collation version ####'
ALTER COLLATION pg_catalog."en_US.utf8" REFRESH VERSION;
ALTER DATABASE :"DBNAME" REFRESH COLLATION VERSION;
ANALYZE;
