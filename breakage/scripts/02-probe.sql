\echo '################################################################'
\echo '#  The probe.  Identical file, run in all three states:'
\echo '#    A  glibc 2.28, the node the data was written on'
\echo '#    B  glibc 2.34, the same data files, before any repair'
\echo '#    C  glibc 2.34, after the repair'
\echo '#'
\echo '#  Every scene asks the same thing twice: first the query an'
\echo '#  application would run, then the query that shows what is'
\echo '#  really in the table.  Nothing is modified: the inserts and'
\echo '#  deletes run inside a transaction that is rolled back, through'
\echo '#  a function that reports what the server answered.'
\echo '################################################################'
\pset pager off
-- Every failure this file expects is caught by try_sql() or check_index() and
-- reported on stdout. Anything else -- a missing table, helpers that were
-- never loaded -- would drop rows out of BOTH sides' output and leave them
-- agreeing while proving nothing, so it stops here instead.
\set ON_ERROR_STOP on
SET client_min_messages = warning;

-- Loaded, not assumed: a node can carry an older pair from a previous run, and
-- the older pair answered REJECTED where this one answers NOT ASKED.
\ir 01b-helpers.sql

\echo ''
\echo '===== environment ============================================='
SELECT version();
SELECT datcollate, datctype, datlocprovider, datcollversion
  FROM pg_database WHERE datname = current_database();
SELECT collname, collversion AS recorded_in_catalog,
       pg_collation_actual_version(oid) AS provided_by_the_os
  FROM pg_collation WHERE collname IN ('sv_SE.utf8','en_US.utf8') ORDER BY collname;

\echo ''
\echo '===== the change, in one query ================================'
\echo '-- seven Swedish words, sorted by the OS collation and by byte order.'
\echo '-- The sv_SE line is what moves between the two glibc versions.'
SELECT string_agg(w, ' ' ORDER BY w COLLATE "sv_SE.utf8") AS sv_se FROM readable;
SELECT string_agg(w, ' ' ORDER BY w COLLATE "C") AS byte_order FROM readable;

\echo ''
\echo '===== scene 1: a plain B-tree index ==========================='
\echo '-- The application asks for one row by its exact value.'
SELECT count(*) FROM s1_words WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s1_words WHERE w = 'waa000';
\echo '-- The same question with the index taken out of the picture.'
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) FROM s1_words WHERE w = 'waa000';
COMMIT;
\echo '-- A range instead of a single value: index first, then table.'
SELECT count(*) AS via_index FROM s1_words WHERE w >= 'wa' AND w < 'wb';
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS via_table FROM s1_words WHERE w >= 'wa' AND w < 'wb';
COMMIT;
\echo '-- What PostgreSQL itself says about that index.'
SELECT check_index('s1_idx') AS bt_index_check;

\echo ''
\echo '===== scene 2: a unique index ================================='
\echo '-- The value is already stored once.'
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS copies_in_the_table FROM s2_uniq WHERE w = 'waa000';
COMMIT;
\echo '-- Insert it a second time.  The primary key should refuse.'
BEGIN;
SELECT try_sql($$INSERT INTO s2_uniq VALUES ('waa000', 'a second copy')$$) AS server_said;
ROLLBACK;
SELECT check_index('s2_uniq_pkey') AS bt_index_check;

\echo ''
\echo '===== scene 3: a foreign key =================================='
\echo '-- The parent row exists.  Insert a child that points at it.'
BEGIN;
SELECT try_sql($$INSERT INTO s3_child VALUES (999001, 'waa000')$$) AS server_said;
ROLLBACK;
\echo '-- That parent has a child.  Deleting it should be refused.'
BEGIN;
SELECT try_sql($$DELETE FROM s3_parent WHERE w = 'waa000'$$) AS server_said;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS child_rows_for_that_parent
  FROM s3_child WHERE pw = 'waa000';
ROLLBACK;

\echo ''
\echo '===== scene 4: LC_CTYPE, which PostgreSQL does not version ===='
\echo '-- Two characters whose answers differ between the two glibc builds.'
SELECT lower(E'Ʂ') AS lower_of_u_a7c5,
       E'ࢾ' ~ '[[:alpha:]]' AS u_08be_is_a_letter;
\echo '-- A login containing the first one, looked up through the'
\echo '-- functional index on lower(login), then through the table.'
SELECT count(*) AS via_index FROM s4_lower
 WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS via_table FROM s4_lower
 WHERE lower(login) = lower('user' || E'Ʂ' || 'name');
COMMIT;
\echo '-- A CHECK constraint that says "this code contains no letters".'
SELECT count(*) AS stored_rows_that_now_violate_it
  FROM s4_class WHERE code ~ '[[:alpha:]]';
BEGIN;
SELECT try_sql($$INSERT INTO s4_class VALUES ('7' || E'ࢾ' || '9')$$) AS server_said;
ROLLBACK;
\echo '-- An index built on the result of a text comparison.'
SELECT count(*) AS via_index FROM s4_expr
 WHERE (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END) = 'late';
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS via_table FROM s4_expr
 WHERE (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END) = 'late';
COMMIT;

\echo ''
\echo '===== scene 5: partial, BRIN and GiST indexes ================='
\echo '-- Partial index, defined as WHERE w < vz.'
SELECT count(*) AS via_index FROM s5a_partial WHERE w < 'vz';
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS via_table FROM s5a_partial WHERE w < 'vz';
COMMIT;
\echo '-- BRIN.  A wide range survives the change; this narrow one does'
\echo '-- not.  It was found by testing all 2704 three-letter prefixes,'
\echo '-- 206 of which lose rows after the migration.'
SELECT count(*) AS via_index FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
BEGIN;
SET LOCAL enable_bitmapscan = off;
SELECT count(*) AS via_table FROM s5b_brin WHERE w >= 'vfx' AND w < 'vfxzzz';
COMMIT;
\echo '-- GiST.'
SELECT count(*) AS via_index FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS via_table FROM s5c_gist WHERE w >= 'wa' AND w < 'wb';
COMMIT;

\echo ''
\echo '===== scene 6: an EXCLUDE constraint =========================='
\echo '-- The value is already there; the constraint should reject it.'
BEGIN;
SELECT try_sql($$INSERT INTO s6_excl VALUES ('waa000')$$) AS server_said;
ROLLBACK;

\echo ''
\echo '===== scene 7: a range-partitioned table ======================'
\echo '-- Where the rows are physically stored.'
SELECT tableoid::regclass AS partition, count(*) FROM s7_part GROUP BY 1 ORDER BY 1;
\echo '-- Rows sitting in the low partition that no longer belong in it.'
SELECT count(*) AS rows_in_the_wrong_partition FROM s7_part_lo WHERE w >= 'vz';
\echo '-- One row, asked for through the partitioned table.'
SELECT count(*) FROM s7_part WHERE w = 'waa000';
EXPLAIN (COSTS OFF) SELECT count(*) FROM s7_part WHERE w = 'waa000';
\echo '-- The same query with partition pruning turned off.'
BEGIN;
SET LOCAL enable_partition_pruning = off;
SELECT count(*) FROM s7_part WHERE w = 'waa000';
COMMIT;
\echo '-- Where a new copy of that row would be routed today.'
BEGIN;
SELECT try_sql($$INSERT INTO s7_part VALUES ('waa000', 'routed now')$$) AS server_said;
SELECT tableoid::regclass AS routed_to FROM s7_part WHERE note = 'routed now';
ROLLBACK;

\echo ''
\echo '===== scene 8: a CHECK constraint ============================='
\echo '-- The constraint is CHECK (w < vz) and is marked valid.'
SELECT count(*) AS stored_rows FROM s8_check;
SELECT count(*) AS stored_rows_that_violate_it FROM s8_check WHERE NOT (w < 'vz');
\echo '-- Insert a row identical to ones already stored.'
BEGIN;
SELECT try_sql($$INSERT INTO s8_check VALUES ('waa000')$$) AS server_said;
ROLLBACK;

\echo ''
\echo '===== scene 9: a stored generated column and a matview ========'
\echo '-- The column is GENERATED ALWAYS AS (CASE WHEN w < vz ...) STORED.'
SELECT bucket, count(*) FROM s9_gen GROUP BY 1 ORDER BY 1;
\echo '-- The same expression, recomputed now.'
SELECT CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END AS recomputed, count(*)
  FROM s9_gen GROUP BY 1 ORDER BY 1;
SELECT count(*) AS rows_whose_stored_value_is_wrong FROM s9_gen
 WHERE bucket IS DISTINCT FROM (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END);
\echo '-- The materialized view stores its own order.'
SELECT pos, w FROM s9_mv ORDER BY pos LIMIT 6;
\echo '-- The same query, run against the table now.'
SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words ORDER BY 1 LIMIT 6;

\echo ''
\echo '===== scene 10: result order and keyset pagination ============'
\echo '-- ORDER BY answered by the index, which returns the stored order.'
SELECT w FROM s1_words ORDER BY w LIMIT 6;
EXPLAIN (COSTS OFF) SELECT w FROM s1_words ORDER BY w LIMIT 6;
\echo '-- The same ORDER BY answered by a sort, which uses the live rules.'
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT w FROM s1_words ORDER BY w LIMIT 6;
COMMIT;
\echo '-- Keyset pagination: the page after the word vind.'
SELECT w FROM s1_words WHERE w > 'vind' ORDER BY w LIMIT 5;

\echo ''
\echo '===== control: en_US, a locale this pair does not change ======'
\echo '-- Same corpus, same index, same node.  Nothing here may move.'
SELECT string_agg(w, ' ' ORDER BY w COLLATE "en_US.utf8") AS en_us FROM readable;
SELECT count(*) AS via_index FROM c0_control WHERE w = 'waa000';
BEGIN;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL enable_indexonlyscan = off;
SELECT count(*) AS via_table FROM c0_control WHERE w = 'waa000';
COMMIT;
SELECT check_index('c0_idx') AS bt_index_check;
