-- Self-test for the five find-affected scripts.  Not for a production server.
-- It creates four databases, three on PostgreSQL 14, which has no ICU
-- databases, and drops them again.
--
-- Run it from this directory, as a superuser, on a test server whose operating
-- system has the en_US.utf8 and sv_SE.utf8 locales and whose PostgreSQL has the
-- pg_trgm and citext extensions and, from version 15 on, was built with ICU:
--
--   cd breakage/tests
--   psql -X -v ON_ERROR_STOP=1 -d postgres -f find-affected-selftest.sql
--
-- It builds the objects its expectation tables name, in throwaway databases,
-- runs the five scripts exactly as a reader would, and compares what they
-- report with what they should report.  Every check prints ok or FAIL.  The
-- run goes through every database, drops them, and ends with PASS,
-- or with a FAIL line that counts the failures and an error, so psql exits
-- non-zero.  A premise that does not hold stops the run where it is and
-- leaves the databases behind; the next run drops them first.  They are all
-- named fa_selftest_*.
--
-- Before it measures anything it checks that each database and collation does
-- what its name claims on this server, because a locale that is missing or
-- falls back to C makes every script agree with every expectation while
-- proving nothing.

\set ON_ERROR_STOP 1
\set QUIET 1
\pset tuples_only on
\pset format unaligned

SELECT current_setting('server_version_num')::int >= 150000 AS icu_databases,
       current_setting('server_version') AS server_version
\gset

-- The five scripts, read from disk the way a reader pastes them.  A file that
-- is not there leaves its variable empty, and the first check below says so.
\set fa_indexes            `cat ../scripts/find-affected-indexes.sql`
\set fa_check_constraints  `cat ../scripts/find-affected-check-constraints.sql`
\set fa_generated_columns  `cat ../scripts/find-affected-generated-columns.sql`
\set fa_materialized_views `cat ../scripts/find-affected-materialized-views.sql`
\set fa_range_partitions   `cat ../scripts/find-affected-range-partitions.sql`

-- Helpers created in each throwaway database.  st_assert stops the run when a
-- premise does not hold.  st_results compares what the five scripts reported
-- with what they should report, one line per object, and fails an expectation
-- whose object does not exist, since "not reported" would otherwise pass for a
-- name nobody created.
SELECT $helpers$
CREATE PROCEDURE st_assert(ok boolean, what text)
LANGUAGE plpgsql AS $$
BEGIN
  IF ok IS NOT TRUE THEN
    RAISE EXCEPTION 'setup check failed, nothing was measured: %', what;
  END IF;
END $$;

CREATE FUNCTION st_exists(script text, object text) RETURNS boolean
LANGUAGE sql AS $$
  SELECT CASE script
    WHEN 'indexes' THEN EXISTS (SELECT 1 FROM pg_class
                                 WHERE oid = to_regclass(object) AND relkind IN ('i', 'I'))
    WHEN 'check-constraints' THEN EXISTS (SELECT 1 FROM pg_constraint
                                           WHERE conname = object AND contype = 'c')
    WHEN 'generated-columns' THEN EXISTS (SELECT 1 FROM pg_attribute
                                           WHERE attrelid = to_regclass(split_part(object, '.', 1))
                                             AND attname = split_part(object, '.', 2)
                                             AND attgenerated = 's')
    WHEN 'materialized-views' THEN EXISTS (SELECT 1 FROM pg_class
                                            WHERE oid = to_regclass(object) AND relkind = 'm')
    WHEN 'range-partitions' THEN EXISTS (SELECT 1 FROM pg_class
                                          WHERE oid = to_regclass(object) AND relkind = 'p')
    ELSE false
  END
$$;

CREATE FUNCTION st_results() RETURNS TABLE (failed boolean, line text)
LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
    WITH got AS (
      SELECT 'indexes' AS script, index_name AS object, collations
        FROM got_indexes
      UNION ALL
      SELECT 'check-constraints', constraint_name, collations
        FROM got_check_constraints
      UNION ALL
      SELECT 'generated-columns', relation || '.' || column_name, collations
        FROM got_generated_columns
      UNION ALL
      SELECT 'materialized-views', relation, collations
        FROM got_materialized_views
      UNION ALL
      SELECT 'range-partitions', relation, collations
        FROM got_range_partitions
    ),
    checks AS (
      SELECT coalesce(w.script, g.script) AS script,
             coalesce(w.object, g.object) AS object,
             w.why,
             CASE
               WHEN w.object IS NOT NULL AND NOT st_exists(w.script, w.object)
                 THEN 'the object this check names does not exist'
               WHEN w.object IS NULL
                 THEN 'reported, and no expectation names it'
               WHEN w.reported AND g.object IS NULL
                 THEN 'not reported'
               WHEN NOT w.reported AND g.object IS NOT NULL
                 THEN 'reported, and should not be'
               WHEN w.reported AND g.collations IS DISTINCT FROM w.label
                 THEN 'reported under "' || g.collations || '", expected "' || w.label || '"'
             END AS problem
        FROM want w
        FULL JOIN got g ON g.script = w.script AND g.object = w.object
    )
    SELECT c.problem IS NOT NULL,
           CASE WHEN c.problem IS NULL THEN 'ok    ' ELSE 'FAIL  ' END
           || current_database() || ' ' || c.script || ': ' || c.object
           || coalesce(': ' || c.problem, '')
           || '  (' || coalesce(c.why, 'no expectation') || ')'
      FROM checks c;
END $$;

CREATE TEMP TABLE want (script text, object text, reported boolean, label text, why text);
$helpers$ AS helpers
\gset

-- The check that the five scripts were read, repeated in every database.
SELECT $run$
CALL st_assert(length(:'fa_indexes') > 500,
                 'find-affected-indexes.sql was not read; run this from breakage/tests');
CALL st_assert(length(:'fa_check_constraints') > 500,
                 'find-affected-check-constraints.sql was not read');
CALL st_assert(length(:'fa_generated_columns') > 500,
                 'find-affected-generated-columns.sql was not read');
CALL st_assert(length(:'fa_materialized_views') > 500,
                 'find-affected-materialized-views.sql was not read');
CALL st_assert(length(:'fa_range_partitions') > 500,
                 'find-affected-range-partitions.sql was not read');
$run$ AS read_checks
\gset

SET client_min_messages = warning;
DROP DATABASE IF EXISTS fa_selftest_c;
DROP DATABASE IF EXISTS fa_selftest_c_en;
DROP DATABASE IF EXISTS fa_selftest_en;
DROP DATABASE IF EXISTS fa_selftest_icu;
RESET client_min_messages;
\if :icu_databases
CREATE DATABASE fa_selftest_c TEMPLATE template0 ENCODING 'UTF8'
  LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'C';
CREATE DATABASE fa_selftest_c_en TEMPLATE template0 ENCODING 'UTF8'
  LOCALE_PROVIDER libc LC_COLLATE 'C' LC_CTYPE 'en_US.utf8';
CREATE DATABASE fa_selftest_en TEMPLATE template0 ENCODING 'UTF8'
  LOCALE_PROVIDER libc LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8';
CREATE DATABASE fa_selftest_icu TEMPLATE template0 ENCODING 'UTF8'
  LOCALE_PROVIDER icu ICU_LOCALE 'en-US' LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8';
\else
CREATE DATABASE fa_selftest_c TEMPLATE template0 ENCODING 'UTF8'
  LC_COLLATE 'C' LC_CTYPE 'C';
CREATE DATABASE fa_selftest_c_en TEMPLATE template0 ENCODING 'UTF8'
  LC_COLLATE 'C' LC_CTYPE 'en_US.utf8';
CREATE DATABASE fa_selftest_en TEMPLATE template0 ENCODING 'UTF8'
  LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8';
\endif


-- ===========================================================================
-- fa_selftest_c: LC_COLLATE and LC_CTYPE both C, so neither the default
-- collation nor the database's LC_CTYPE moves.  Everything reported here is
-- reported through a collation a column carries.
-- ===========================================================================
\connect fa_selftest_c
:helpers
:read_checks

SELECT datcollate = 'C' AND datctype = 'C'
       AND coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') = 'c' AS db_ok
  FROM pg_database d WHERE datname = current_database() \gset
CALL st_assert(:'db_ok', 'fa_selftest_c is a libc database with C in both halves');

-- Created here rather than imported, so the test does not depend on what
-- pg_import_system_collations left in template0.
CREATE COLLATION st_sv (provider = libc, locale = 'sv_SE.utf8');
CREATE COLLATION st_split_c_en (provider = libc, lc_collate = 'C', lc_ctype = 'en_US.utf8');
CREATE COLLATION st_split_en_c (provider = libc, lc_collate = 'en_US.utf8', lc_ctype = 'C');
CREATE SCHEMA st;
CREATE COLLATION st."C" (provider = libc, lc_collate = 'C', lc_ctype = 'en_US.utf8');

CALL st_assert(('a' COLLATE st_sv) < ('B' COLLATE st_sv)
                 AND lower(E'\u00C5' COLLATE st_sv) = E'\u00E5',
                 'st_sv sorts and lowers as sv_SE');
CALL st_assert(('a' COLLATE st_split_c_en) > ('B' COLLATE st_split_c_en)
                 AND lower(E'\u00C5' COLLATE st_split_c_en) = E'\u00E5',
                 'st_split_c_en sorts as C and lowers as en_US');
CALL st_assert(('a' COLLATE st_split_en_c) < ('B' COLLATE st_split_en_c)
                 AND lower(E'\u00C5' COLLATE st_split_en_c) = E'\u00C5',
                 'st_split_en_c sorts as en_US and lowers as C');
CALL st_assert(lower(E'\u00C5' COLLATE st."C") = E'\u00E5',
                 'st."C" lowers as en_US under the name C');
-- Text search in a C database treats every non-ASCII character as a letter,
-- so U+00D7 does not split the word.
CALL st_assert(length(to_tsvector('simple', E'ab\u00D7cd')) = 1,
                 'text search reads LC_CTYPE C here');

CREATE EXTENSION pg_trgm;
CREATE EXTENSION citext;

CREATE TABLE st_t (
  id      int,
  w_sv    text COLLATE st_sv,
  w_split text COLLATE st_split_c_en,
  w_en_c  text COLLATE st_split_en_c,
  w_c     text COLLATE "C",
  w_named text COLLATE st."C",
  w_ci    citext
);
CREATE INDEX st_ix_sv               ON st_t (w_sv);
CREATE INDEX st_ix_en_c             ON st_t (w_en_c);
CREATE INDEX st_ix_split            ON st_t (w_split);
CREATE INDEX st_ix_lower_split      ON st_t (lower(w_split));
CREATE INDEX st_ix_pred_split       ON st_t (id) WHERE w_split ~ '[[:alpha:]]';
CREATE INDEX st_ix_hash_lower_split ON st_t USING hash (lower(w_split));
CREATE INDEX st_ix_hash_sv          ON st_t USING hash (w_sv);
CREATE INDEX st_ix_hash_pred_sv     ON st_t USING hash (id) WHERE w_sv < 'vz';
CREATE INDEX st_ix_hash_pred_en_c   ON st_t USING hash (id) WHERE w_en_c < 'vz';
CREATE INDEX st_ix_c                ON st_t (w_c);
CREATE INDEX st_ix_lower_c          ON st_t (lower(w_c));
CREATE INDEX st_ix_named            ON st_t (lower(w_named));
CREATE INDEX st_ix_trgm_c           ON st_t USING gin (w_c gin_trgm_ops);
CREATE INDEX st_ix_citext           ON st_t (w_ci);
ALTER TABLE st_t ADD CONSTRAINT st_chk_split CHECK (w_split ~ '^[[:alpha:]]*$');
ALTER TABLE st_t ADD CONSTRAINT st_chk_sv    CHECK (w_sv < 'vz');
ALTER TABLE st_t ADD CONSTRAINT st_chk_c     CHECK (w_c < 'x');

CREATE TABLE st_g (
  w_sv    text COLLATE st_sv,
  w_split text COLLATE st_split_c_en,
  w_c     text COLLATE "C",
  st_gen_bool_sv boolean GENERATED ALWAYS AS (w_sv < 'vz') STORED,
  st_gen_split   boolean GENERATED ALWAYS AS (w_split ~ '[[:alpha:]]') STORED,
  st_gen_c       boolean GENERATED ALWAYS AS (w_c < 'x') STORED
);

CREATE TABLE st_j (id int, doc jsonb,
  st_gen_tsv_jsonb tsvector GENERATED ALWAYS AS (to_tsvector('simple', doc)) STORED);
CREATE INDEX st_ix_tsv_jsonb ON st_j USING gin (to_tsvector('simple', doc));
CREATE INDEX st_ix_jsonb ON st_j (doc);
CREATE MATERIALIZED VIEW st_mv_tsv_jsonb AS SELECT to_tsvector('simple', doc) AS v FROM st_j;
CREATE MATERIALIZED VIEW st_mv_tsv_values AS
  SELECT to_tsvector('simple', x) AS v FROM (VALUES ('ab cd')) s(x);
CREATE DOMAIN st_dtsv AS tsvector;
CREATE TABLE st_gd (doc jsonb,
  st_gen_dom_tsv st_dtsv GENERATED ALWAYS AS (to_tsvector('simple', doc)::st_dtsv) STORED);
CREATE MATERIALIZED VIEW st_mv_dom_tsv AS SELECT to_tsvector('simple', doc)::st_dtsv AS v FROM st_j;
CREATE MATERIALIZED VIEW st_mv_tsv_arr AS SELECT ARRAY[to_tsvector('simple', doc)] AS v FROM st_j;

CREATE MATERIALIZED VIEW st_mv_split AS SELECT lower(w_split) AS l FROM st_t;
CREATE MATERIALIZED VIEW st_mv_sv    AS SELECT w_sv FROM st_t;
CREATE MATERIALIZED VIEW st_mv_c     AS SELECT w_c FROM st_t;

CREATE TABLE st_p_range_split       (w text COLLATE st_split_c_en) PARTITION BY RANGE (w);
CREATE TABLE st_p_range_lower_split (w text COLLATE st_split_c_en) PARTITION BY RANGE (lower(w));
CREATE TABLE st_p_list_lower_split  (w text COLLATE st_split_c_en) PARTITION BY LIST (lower(w));
CREATE TABLE st_p_hash_lower_split  (w text COLLATE st_split_c_en) PARTITION BY HASH (lower(w));
CREATE TABLE st_p_range_sv          (w text COLLATE st_sv) PARTITION BY RANGE (w);
CREATE TABLE st_p_list_sv           (w text COLLATE st_sv) PARTITION BY LIST (w);
CREATE TABLE st_p_range_c           (w text COLLATE "C") PARTITION BY RANGE (w);
CREATE TABLE st_p_range_lower_c     (w text COLLATE "C") PARTITION BY RANGE (lower(w));
CREATE TABLE st_p_list_citext       (w citext) PARTITION BY LIST (w);

INSERT INTO want VALUES
  ('indexes', 'st_ix_sv',               true,  'st_sv',         'a key sorted by sv_SE'),
  ('indexes', 'st_ix_en_c',             true,  'st_split_en_c', 'a key sorted by en_US, whatever its LC_CTYPE'),
  ('indexes', 'st_ix_split',            false, NULL,            'a plain key sorted as C is compared byte by byte'),
  ('indexes', 'st_ix_lower_split',      true,  'st_split_c_en', 'lower() reads the column''s LC_CTYPE'),
  ('indexes', 'st_ix_pred_split',       true,  'st_split_c_en', 'a character class in the predicate'),
  ('indexes', 'st_ix_hash_lower_split', true,  'st_split_c_en', 'a hash of lower() moves with LC_CTYPE'),
  ('indexes', 'st_ix_hash_sv',          false, NULL,            'a plain hash index hashes the bytes'),
  ('indexes', 'st_ix_hash_pred_sv',     true,  'st_sv',         'a hash index whose predicate compares under sv_SE'),
  ('indexes', 'st_ix_hash_pred_en_c',   true,  'st_split_en_c', 'the same under en_US order and C characters, so only the order finds it'),
  ('indexes', 'st_ix_c',                false, NULL,            'C in both halves'),
  ('indexes', 'st_ix_lower_c',          false, NULL,            'lower() under C in both halves'),
  ('indexes', 'st_ix_named',            true,  'st.C',          'named C, and its LC_CTYPE is en_US; printed with its schema'),
  ('indexes', 'st_ix_trgm_c',           false, NULL,            'pg_trgm, and the database''s LC_CTYPE is C'),
  ('indexes', 'st_ix_citext',           false, NULL,            'citext, and the database''s LC_CTYPE is C'),
  ('indexes', 'st_ix_tsv_jsonb',        false, NULL,            'text search over jsonb, and the database''s LC_CTYPE is C'),
  ('generated-columns', 'st_j.st_gen_tsv_jsonb', false, NULL,   'a tsvector from jsonb, and the database''s LC_CTYPE is C'),
  ('materialized-views', 'st_mv_tsv_jsonb', false, NULL,        'a tsvector from jsonb, and the database''s LC_CTYPE is C'),
  ('indexes', 'st_ix_jsonb',            false, NULL,            'a jsonb key, and the default collation sorts as C'),
  ('materialized-views', 'st_mv_tsv_values', false, NULL,       'a tsvector over VALUES, and the database''s LC_CTYPE is C'),
  ('materialized-views', 'st_mv_dom_tsv', false, NULL,          'a domain over tsvector, and the database''s LC_CTYPE is C'),
  ('materialized-views', 'st_mv_tsv_arr', false, NULL,          'an array of tsvector, and the database''s LC_CTYPE is C'),
  ('generated-columns', 'st_gd.st_gen_dom_tsv', false, NULL,    'a stored domain over tsvector, and the database''s LC_CTYPE is C'),
  ('range-partitions', 'st_p_list_citext', false, NULL,         'a citext list key, and the database''s LC_CTYPE is C'),
  ('check-constraints', 'st_chk_split', true,  'st_split_c_en', 'a character class reads LC_CTYPE'),
  ('check-constraints', 'st_chk_sv',    true,  'st_sv',         'a comparison under sv_SE'),
  ('check-constraints', 'st_chk_c',     false, NULL,            'C in both halves'),
  ('generated-columns', 'st_g.st_gen_bool_sv', true, 'st_sv',
   'a boolean computed from sv_SE; on 14 only the column''s own dependencies show it'),
  ('generated-columns', 'st_g.st_gen_split', true, 'st_split_c_en', 'a character class reads LC_CTYPE'),
  ('generated-columns', 'st_g.st_gen_c', false, NULL,           'C in both halves'),
  ('materialized-views', 'st_mv_split', true,  'st_split_c_en', 'reads a column whose LC_CTYPE moves'),
  ('materialized-views', 'st_mv_sv',    true,  'st_sv',         'reads a column sorted by sv_SE'),
  ('materialized-views', 'st_mv_c',     false, NULL,            'reads only C'),
  ('range-partitions', 'st_p_range_split',       false, NULL,   'a plain range key sorted as C'),
  ('range-partitions', 'st_p_range_lower_split', true, 'st_split_c_en', 'lower() in a range key'),
  ('range-partitions', 'st_p_list_lower_split',  true, 'st_split_c_en', 'lower() in a list key'),
  ('range-partitions', 'st_p_hash_lower_split',  true, 'st_split_c_en', 'lower() in a hash key'),
  ('range-partitions', 'st_p_range_sv',          true, 'st_sv', 'a plain range key sorted by sv_SE'),
  ('range-partitions', 'st_p_list_sv',           false, NULL,   'a plain list key is compared for equality'),
  ('range-partitions', 'st_p_range_c',           false, NULL,   'C in both halves'),
  ('range-partitions', 'st_p_range_lower_c',     false, NULL,   'lower() under C in both halves');

CREATE TEMP TABLE got_indexes            AS :fa_indexes
CREATE TEMP TABLE got_check_constraints  AS :fa_check_constraints
CREATE TEMP TABLE got_generated_columns  AS :fa_generated_columns
CREATE TEMP TABLE got_materialized_views AS :fa_materialized_views
CREATE TEMP TABLE got_range_partitions   AS :fa_range_partitions
SELECT line FROM st_results() ORDER BY failed DESC, line;
SELECT count(*) AS checks_c, count(*) FILTER (WHERE failed) AS fails_c
  FROM st_results() \gset


-- ===========================================================================
-- fa_selftest_c_en: LC_COLLATE C and LC_CTYPE en_US, the recipe chosen in the
-- hope that indexes never move.  Every index that lowers, classifies or
-- searches text still does.  The default collation sorts as C and lowers as
-- en_US, and text search reads en_US whatever the column carries.
-- ===========================================================================
\connect fa_selftest_c_en
:helpers
:read_checks

SELECT datcollate = 'C' AND datctype = 'en_US.utf8'
       AND coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') = 'c' AS db_ok
  FROM pg_database d WHERE datname = current_database() \gset
CALL st_assert(:'db_ok', 'fa_selftest_c_en is a libc database, C and en_US.utf8');
CALL st_assert('a' > 'B' AND lower(E'\u00C5') = E'\u00E5',
                 'the default collation sorts as C and lowers as en_US');
-- Under en_US, U+00D7 is not a letter and splits the word, even in a column
-- that sorts and lowers as C.
CALL st_assert(length(to_tsvector('simple', E'ab\u00D7cd' COLLATE "C")) = 2,
                 'text search reads the database''s LC_CTYPE, en_US, through COLLATE "C"');

CREATE EXTENSION pg_trgm;
CREATE EXTENSION citext;

CREATE TABLE st_t (id int, w_def text, w_c text COLLATE "C", w_ci citext);
CREATE INDEX st_ix_def       ON st_t (w_def);
CREATE INDEX st_ix_hash_def  ON st_t USING hash (w_def);
CREATE INDEX st_ix_c         ON st_t (w_c);
CREATE INDEX st_ix_lower_def ON st_t (lower(w_def));
CREATE INDEX st_ix_trgm_def  ON st_t USING gin (w_def gin_trgm_ops);
CREATE INDEX st_ix_trgm_c    ON st_t USING gin (w_c gin_trgm_ops);
CREATE INDEX st_ix_tsv_c     ON st_t USING gin (to_tsvector('simple', w_c));
CREATE INDEX st_ix_pred_tsv_c ON st_t (id)
  WHERE to_tsvector('simple', w_c) @@ to_tsquery('simple', 'x');
CREATE INDEX st_ix_citext    ON st_t (w_ci);
ALTER TABLE st_t ADD CONSTRAINT st_chk_def   CHECK (w_def ~ '^[[:alpha:]]*$');
ALTER TABLE st_t ADD CONSTRAINT st_chk_tsv_c CHECK (length(to_tsvector('simple', w_c)) < 1000);

CREATE TABLE st_g (
  w_def text,
  w_c   text COLLATE "C",
  st_gen_def   boolean  GENERATED ALWAYS AS (w_def ~ '[[:alpha:]]') STORED,
  st_gen_tsv_c tsvector GENERATED ALWAYS AS (to_tsvector('simple', w_c)) STORED
);

CREATE TABLE st_j (id int, doc jsonb,
  st_gen_tsv_jsonb tsvector GENERATED ALWAYS AS (to_tsvector('simple', doc)) STORED);
CREATE INDEX st_ix_tsv_jsonb  ON st_j USING gin (to_tsvector('simple', doc));
CREATE INDEX st_ix_tsv_stored ON st_j USING gin (st_gen_tsv_jsonb);
CREATE MATERIALIZED VIEW st_mv_tsv_jsonb AS SELECT to_tsvector('simple', doc) AS v FROM st_j;

CREATE MATERIALIZED VIEW st_mv_tsv_values AS
  SELECT to_tsvector('simple', x) AS v FROM (VALUES ('ab cd')) s(x);
CREATE DOMAIN st_dtsv AS tsvector;
CREATE TABLE st_gd (doc jsonb,
  st_gen_dom_tsv st_dtsv GENERATED ALWAYS AS (to_tsvector('simple', doc)::st_dtsv) STORED);
CREATE MATERIALIZED VIEW st_mv_dom_tsv AS SELECT to_tsvector('simple', doc)::st_dtsv AS v FROM st_j;
CREATE MATERIALIZED VIEW st_mv_tsv_arr AS SELECT ARRAY[to_tsvector('simple', doc)] AS v FROM st_j;

CREATE MATERIALIZED VIEW st_mv_def   AS SELECT upper(w_def) AS u FROM st_t;
CREATE MATERIALIZED VIEW st_mv_tsv_c AS SELECT to_tsvector('simple', w_c) AS v FROM st_t;

CREATE TABLE st_p_range_def       (w text) PARTITION BY RANGE (w);
CREATE TABLE st_p_range_lower_def (w text) PARTITION BY RANGE (lower(w));
CREATE TABLE st_p_list_tsv_c      (w text COLLATE "C")
  PARTITION BY LIST (length(to_tsvector('simple', w)));
CREATE TABLE st_p_list_citext     (w citext) PARTITION BY LIST (w);
CREATE TABLE st_p_hash_citext     (w citext) PARTITION BY HASH (w);
CREATE TABLE st_p_range_citext_c  (w citext COLLATE "C") PARTITION BY RANGE (w);

INSERT INTO want VALUES
  ('indexes', 'st_ix_def',       false, NULL,      'a plain key under the default collation, which sorts as C'),
  ('indexes', 'st_ix_hash_def',  false, NULL,      'a plain hash index hashes the bytes'),
  ('indexes', 'st_ix_c',         false, NULL,      'a plain key under C'),
  ('indexes', 'st_ix_lower_def', true,  'default', 'lower() reads the default collation''s LC_CTYPE'),
  ('indexes', 'st_ix_trgm_def',  true,  'default', 'pg_trgm on a column whose LC_CTYPE moves'),
  ('indexes', 'st_ix_trgm_c',    true,  'database LC_CTYPE en_US.utf8', 'pg_trgm reads the database''s LC_CTYPE through COLLATE "C"'),
  ('indexes', 'st_ix_tsv_c',     true,  'database LC_CTYPE en_US.utf8', 'text search reads the database''s LC_CTYPE through COLLATE "C"'),
  ('indexes', 'st_ix_pred_tsv_c', true, 'database LC_CTYPE en_US.utf8', 'text search in a predicate, through COLLATE "C"'),
  ('indexes', 'st_ix_citext',    true,  'default', 'citext lowers by the default collation, whose LC_CTYPE moves'),
  ('indexes', 'st_ix_tsv_jsonb', true,  'database LC_CTYPE en_US.utf8', 'text search over a jsonb column, which has no collation'),
  ('indexes', 'st_ix_tsv_stored', false, NULL,     'an index over a stored tsvector keeps its lexemes; the column is what moves'),
  ('generated-columns', 'st_j.st_gen_tsv_jsonb', true, 'database LC_CTYPE en_US.utf8', 'a stored tsvector computed from jsonb'),
  ('materialized-views', 'st_mv_tsv_jsonb', true, 'database LC_CTYPE en_US.utf8', 'a tsvector computed from jsonb'),
  ('check-constraints', 'st_chk_def',   true, 'default', 'a character class under the default collation'),
  ('check-constraints', 'st_chk_tsv_c', true, 'database LC_CTYPE en_US.utf8', 'text search through COLLATE "C"'),
  ('generated-columns', 'st_g.st_gen_def',   true, 'default', 'a character class under the default collation'),
  ('generated-columns', 'st_g.st_gen_tsv_c', true, 'database LC_CTYPE en_US.utf8', 'a stored tsvector computed from a C column'),
  ('materialized-views', 'st_mv_def',   true, 'default', 'upper() under the default collation'),
  ('materialized-views', 'st_mv_tsv_c', true, 'database LC_CTYPE en_US.utf8', 'text search through COLLATE "C"'),
  ('range-partitions', 'st_p_range_def',       false, NULL,     'a plain range key under the default collation, which sorts as C'),
  ('range-partitions', 'st_p_range_lower_def', true, 'default', 'lower() in a range key'),
  ('range-partitions', 'st_p_list_tsv_c',      true, 'database LC_CTYPE en_US.utf8', 'text search in a list key, through COLLATE "C"'),
  ('range-partitions', 'st_p_list_citext',     true, 'default', 'citext lowers before it compares, so a list key reads LC_CTYPE'),
  ('range-partitions', 'st_p_hash_citext',     true, 'default', 'citext lowers before it hashes, so a hash key reads LC_CTYPE'),
  ('range-partitions', 'st_p_range_citext_c',  true, 'database LC_CTYPE en_US.utf8', 'citext lowers by the default collation, whatever the key''s own'),
  ('materialized-views', 'st_mv_tsv_values',   true, 'database LC_CTYPE en_US.utf8', 'a tsvector over VALUES, which reads no column'),
  ('materialized-views', 'st_mv_dom_tsv',      true, 'database LC_CTYPE en_US.utf8', 'a domain over tsvector is a tsvector column'),
  ('materialized-views', 'st_mv_tsv_arr',      true, 'database LC_CTYPE en_US.utf8', 'an array of tsvector is text search too'),
  ('generated-columns', 'st_gd.st_gen_dom_tsv', true, 'database LC_CTYPE en_US.utf8', 'a stored domain over tsvector');

CREATE TEMP TABLE got_indexes            AS :fa_indexes
CREATE TEMP TABLE got_check_constraints  AS :fa_check_constraints
CREATE TEMP TABLE got_generated_columns  AS :fa_generated_columns
CREATE TEMP TABLE got_materialized_views AS :fa_materialized_views
CREATE TEMP TABLE got_range_partitions   AS :fa_range_partitions
SELECT line FROM st_results() ORDER BY failed DESC, line;
SELECT count(*) AS checks_c_en, count(*) FILTER (WHERE failed) AS fails_c_en
  FROM st_results() \gset


-- ===========================================================================
-- fa_selftest_en: libc with en_US in both halves, so the default collation's
-- sort order moves.  jsonb carries no collation and orders its strings by that
-- default collation, which is what this database is for.
-- ===========================================================================
\connect fa_selftest_en
:helpers
:read_checks

SELECT datcollate = 'en_US.utf8' AND datctype = 'en_US.utf8'
       AND coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') = 'c' AS db_ok
  FROM pg_database d WHERE datname = current_database() \gset
CALL st_assert(:'db_ok', 'fa_selftest_en is a libc database with en_US.utf8 in both halves');
-- Under C, 'a' sorts after 'B', and under en_US before it.  The same must hold
-- inside jsonb, or jsonb is not ordering by the default collation here.
CALL st_assert('a' < 'B' AND '"a"'::jsonb < '"B"'::jsonb,
               'jsonb orders its strings by the default collation, en_US');

CREATE TABLE st_t (id int, w text, doc jsonb);
CREATE INDEX st_ix_text              ON st_t (w);
CREATE INDEX st_ix_jsonb             ON st_t (doc);
CREATE UNIQUE INDEX st_ix_jsonb_expr ON st_t ((doc->'id'));
CREATE INDEX st_ix_jsonb_gin         ON st_t USING gin (doc);
CREATE INDEX st_ix_jsonb_hash        ON st_t USING hash (doc);
CREATE TABLE st_p_range_jsonb      (doc jsonb) PARTITION BY RANGE (doc);
CREATE TABLE st_p_range_jsonb_expr (doc jsonb) PARTITION BY RANGE ((doc->'k'));
CREATE TABLE st_p_list_jsonb       (doc jsonb) PARTITION BY LIST (doc);

INSERT INTO want VALUES
  ('indexes', 'st_ix_text',       true,  'default', 'a text key sorted by en_US, the control'),
  ('indexes', 'st_ix_jsonb',      true,  'default', 'jsonb orders its strings by the default collation'),
  ('indexes', 'st_ix_jsonb_expr', true,  'default', 'a unique index on a jsonb expression'),
  ('indexes', 'st_ix_jsonb_gin',  false, NULL,      'GIN compares jsonb strings under C'),
  ('indexes', 'st_ix_jsonb_hash', false, NULL,      'a hash index hashes the bytes'),
  ('range-partitions', 'st_p_range_jsonb',      true,  'default', 'a jsonb range key'),
  ('range-partitions', 'st_p_range_jsonb_expr', true,  'default', 'a jsonb expression as a range key'),
  ('range-partitions', 'st_p_list_jsonb',       false, NULL,      'a list key is compared for equality');

CREATE TEMP TABLE got_indexes            AS :fa_indexes
CREATE TEMP TABLE got_check_constraints  AS :fa_check_constraints
CREATE TEMP TABLE got_generated_columns  AS :fa_generated_columns
CREATE TEMP TABLE got_materialized_views AS :fa_materialized_views
CREATE TEMP TABLE got_range_partitions   AS :fa_range_partitions
SELECT line FROM st_results() ORDER BY failed DESC, line;
SELECT count(*) AS checks_en, count(*) FILTER (WHERE failed) AS fails_en
  FROM st_results() \gset


-- ===========================================================================
-- fa_selftest_icu: ICU decides the default collation, and PostgreSQL still
-- sets the process LC_CTYPE from datctype, which text search and pg_trgm read.
-- LC_COLLATE is en_US too, so a script that took the default collation for a
-- libc one would report its plain keys.  There is no ICU database before
-- PostgreSQL 15.
-- ===========================================================================
\if :icu_databases
\connect fa_selftest_icu
:helpers
:read_checks

SELECT datcollate = 'en_US.utf8' AND datctype = 'en_US.utf8'
       AND coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') = 'i' AS db_ok
  FROM pg_database d WHERE datname = current_database() \gset
CALL st_assert(:'db_ok', 'fa_selftest_icu is an ICU database with LC_COLLATE and LC_CTYPE en_US.utf8');
CALL st_assert(length(to_tsvector('simple', E'ab\u00D7cd')) = 2,
                 'text search splits the word at U+00D7 in an ICU database, so it is not reading LC_CTYPE C');

CREATE EXTENSION pg_trgm;

CREATE TABLE st_t (id int, w_def text);
CREATE INDEX st_ix_def      ON st_t (w_def);
CREATE INDEX st_ix_trgm_def ON st_t USING gin (w_def gin_trgm_ops);
CREATE INDEX st_ix_tsv_def  ON st_t USING gin (to_tsvector('simple', w_def));
ALTER TABLE st_t ADD CONSTRAINT st_chk_def CHECK (w_def < 'x');
CREATE TABLE st_g (w_def text, st_gen_def boolean GENERATED ALWAYS AS (w_def < 'x') STORED);
CREATE MATERIALIZED VIEW st_mv_def AS SELECT w_def FROM st_t;
CREATE TABLE st_p_range_def (w text) PARTITION BY RANGE (w);
CREATE TABLE st_j (doc jsonb);
CREATE INDEX st_ix_jsonb ON st_j (doc);

INSERT INTO want VALUES
  ('indexes', 'st_ix_def',      false, NULL, 'ICU sorts the default collation, whatever LC_COLLATE says'),
  ('indexes', 'st_ix_jsonb',    false, NULL, 'jsonb orders by the default collation, and ICU decides it here'),
  ('indexes', 'st_ix_trgm_def', true,  'database LC_CTYPE en_US.utf8', 'pg_trgm reads the database''s LC_CTYPE'),
  ('indexes', 'st_ix_tsv_def',  true,  'database LC_CTYPE en_US.utf8', 'text search reads the database''s LC_CTYPE'),
  ('check-constraints', 'st_chk_def', true, 'database LC_CTYPE en_US.utf8',
   'ICU compares it; listed only because text search would read the database''s LC_CTYPE'),
  ('generated-columns', 'st_g.st_gen_def', true, 'database LC_CTYPE en_US.utf8',
   'ICU compares it; listed only because text search would read the database''s LC_CTYPE'),
  ('materialized-views', 'st_mv_def', true, 'database LC_CTYPE en_US.utf8',
   'ICU sorts it; listed only because text search would read the database''s LC_CTYPE'),
  ('range-partitions', 'st_p_range_def', false, NULL, 'ICU sorts the default collation, whatever LC_COLLATE says');

CREATE TEMP TABLE got_indexes            AS :fa_indexes
CREATE TEMP TABLE got_check_constraints  AS :fa_check_constraints
CREATE TEMP TABLE got_generated_columns  AS :fa_generated_columns
CREATE TEMP TABLE got_materialized_views AS :fa_materialized_views
CREATE TEMP TABLE got_range_partitions   AS :fa_range_partitions
SELECT line FROM st_results() ORDER BY failed DESC, line;
SELECT count(*) AS checks_icu, count(*) FILTER (WHERE failed) AS fails_icu
  FROM st_results() \gset
\else
\set checks_icu 0
\set fails_icu 0
\echo 'NOT RUN  fa_selftest_icu: PostgreSQL' :server_version 'has no ICU database provider (it arrived in 15)'
\endif


\connect postgres
DROP DATABASE fa_selftest_c;
DROP DATABASE fa_selftest_c_en;
DROP DATABASE fa_selftest_en;
SET client_min_messages = warning;
DROP DATABASE IF EXISTS fa_selftest_icu;
RESET client_min_messages;
-- The number of checks is pinned, so a section that stops running, or a case
-- deleted together with its object, cannot leave a shorter run that passes.
-- Change the two numbers when a case is added or removed.
SELECT :checks_c + :checks_c_en + :checks_en + :checks_icu AS checks_total,
       CASE WHEN :'icu_databases' THEN 85 ELSE 77 END AS checks_expected,
       :fails_c + :fails_c_en + :fails_en + :fails_icu AS fails_total
\gset
SELECT :fails_total > 0 OR :checks_total <> :checks_expected AS failed \gset
\if :failed
\echo 'FAIL  ' :fails_total 'failed,' :checks_total 'run,' :checks_expected 'expected, on PostgreSQL' :server_version
DO $$ BEGIN RAISE EXCEPTION 'find-affected-selftest failed; the FAIL lines above name each check'; END $$;
\else
\echo 'PASS  ' :checks_total 'checks on PostgreSQL' :server_version
\endif
