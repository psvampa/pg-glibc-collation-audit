-- Build the ten scenes.  Runs ONCE, on the glibc 2.28 primary (node0).
-- Everything below is created and populated under glibc 2.28 rules; the
-- standby on glibc 2.34 receives the same bytes by streaming replication.
--
-- Every text column carries an explicit COLLATE "sv_SE.utf8".  One control
-- table uses en_US.utf8, a locale this glibc pair does not change.
\pset pager off
SET client_min_messages = warning;
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS amcheck;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Dropped first: it depends on s1_words, so a rerun of this script fails at
-- scene 1 without it.
DROP MATERIALIZED VIEW IF EXISTS s9_mv;

-- The corpus: 20000 deterministic words over a mixed-radix generator, so the
-- v-words and the w-words interleave under glibc 2.28 (where W is a variant of
-- V) and separate under 2.34 (where W is its own letter, after V).  Mixed
-- radix means every word is distinct.  No randomness anywhere.
DROP TABLE IF EXISTS corpus CASCADE;
CREATE TABLE corpus (id int primary key, w text COLLATE "sv_SE.utf8");
INSERT INTO corpus
SELECT i,
       (ARRAY['v','V','w','W'])[1 + (i % 4)] ||
       chr(97 + (i / 4) % 26) ||
       chr(97 + (i / 104) % 26) ||
       lpad((i / 2704)::text, 3, '0')
FROM generate_series(0, 19999) i;

-- The seven readable words the article quotes.
DROP TABLE IF EXISTS readable CASCADE;
CREATE TABLE readable (w text COLLATE "sv_SE.utf8");
INSERT INTO readable VALUES ('va'),('Vasa'),('vind'),('vz'),('wa'),('Wasa'),('wind');

\echo '--- corpus built (20000 rows, all distinct, plus 7 readable words) ---'
SELECT (SELECT count(*) FROM corpus) AS corpus_rows,
       (SELECT count(DISTINCT w) FROM corpus) AS corpus_distinct,
       (SELECT count(*) FROM readable) AS readable_rows;

-- ---------------------------------------------------------------- scene 1
-- Plain B-tree index on a text column.
DROP TABLE IF EXISTS s1_words;
CREATE TABLE s1_words (id int, w text COLLATE "sv_SE.utf8");
INSERT INTO s1_words SELECT id, w FROM corpus;
INSERT INTO s1_words SELECT 100000 + row_number() OVER (ORDER BY w), w FROM readable;
CREATE INDEX s1_idx ON s1_words (w);

-- ---------------------------------------------------------------- scene 2
-- Unique index / primary key.
DROP TABLE IF EXISTS s2_uniq;
CREATE TABLE s2_uniq (w text COLLATE "sv_SE.utf8" PRIMARY KEY, note text);
INSERT INTO s2_uniq SELECT w, 'loaded under glibc 2.28' FROM corpus;

-- ---------------------------------------------------------------- scene 3
-- Foreign key, resolved through the parent's unique index and the child's.
DROP TABLE IF EXISTS s3_child;
DROP TABLE IF EXISTS s3_parent;
CREATE TABLE s3_parent (w text COLLATE "sv_SE.utf8" PRIMARY KEY);
INSERT INTO s3_parent SELECT w FROM corpus;
CREATE TABLE s3_child (id int PRIMARY KEY,
                       pw text COLLATE "sv_SE.utf8" REFERENCES s3_parent(w));
INSERT INTO s3_child SELECT id, w FROM corpus WHERE id < 200;
CREATE INDEX s3_child_pw ON s3_child (pw);

-- ---------------------------------------------------------------- scene 4
-- LC_CTYPE, not LC_COLLATE.  Measured with 03-ctype-sweep.sql: 6525 code
-- points answer differently on the two nodes.  Two of them are used here.
--   U+A7C5  lower() returns the character itself on 2.28, U+0282 on 2.34
--   U+08BE  is not [[:alpha:]] on 2.28 and is on 2.34
DROP TABLE IF EXISTS s4_lower;
CREATE TABLE s4_lower (id int, login text COLLATE "sv_SE.utf8");
INSERT INTO s4_lower SELECT id, w FROM corpus;
INSERT INTO s4_lower VALUES (900001, 'user' || E'Ʂ' || 'name');
CREATE INDEX s4_lower_idx ON s4_lower (lower(login));

DROP TABLE IF EXISTS s4_class;
CREATE TABLE s4_class (code text COLLATE "sv_SE.utf8"
                       CHECK (code !~ '[[:alpha:]]'));
INSERT INTO s4_class VALUES ('123-456'), ('7' || E'ࢾ' || '9');

-- Expression index whose expression depends on LC_COLLATE.
DROP TABLE IF EXISTS s4_expr;
CREATE TABLE s4_expr (id int, w text COLLATE "sv_SE.utf8");
INSERT INTO s4_expr SELECT id, w FROM corpus;
CREATE INDEX s4_expr_idx ON s4_expr ((CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END));

-- ---------------------------------------------------------------- scene 5
-- One table per index type, so EXPLAIN can only pick the index under test.
DROP TABLE IF EXISTS s5a_partial;
CREATE TABLE s5a_partial (id int, w text COLLATE "sv_SE.utf8", pad text);
INSERT INTO s5a_partial SELECT id, w, repeat('.', 60) FROM corpus;
CREATE INDEX s5a_idx ON s5a_partial (w) WHERE w < 'vz';

DROP TABLE IF EXISTS s5b_brin;
CREATE TABLE s5b_brin (id int, w text COLLATE "sv_SE.utf8", pad text);
-- inserted in sorted order, which is what makes BRIN minmax useful at all
INSERT INTO s5b_brin SELECT id, w, repeat('.', 60) FROM corpus ORDER BY w;
CREATE INDEX s5b_idx ON s5b_brin USING brin (w) WITH (pages_per_range = 4);

DROP TABLE IF EXISTS s5c_gist;
CREATE TABLE s5c_gist (id int, w text COLLATE "sv_SE.utf8", pad text);
INSERT INTO s5c_gist SELECT id, w, repeat('.', 60) FROM corpus;
CREATE INDEX s5c_idx ON s5c_gist USING gist (w);

-- ---------------------------------------------------------------- scene 6
-- EXCLUDE constraint (a GiST index underneath).
DROP TABLE IF EXISTS s6_excl;
CREATE TABLE s6_excl (w text COLLATE "sv_SE.utf8",
                      EXCLUDE USING gist (w WITH =));
INSERT INTO s6_excl SELECT w FROM corpus;

-- ---------------------------------------------------------------- scene 7
-- RANGE partitioning on a text key.  Boundary 'vz': under 2.28 every w-word
-- sorts before it, under 2.34 every w-word sorts after it.
DROP TABLE IF EXISTS s7_part;
CREATE TABLE s7_part (w text COLLATE "sv_SE.utf8", note text) PARTITION BY RANGE (w);
CREATE TABLE s7_part_lo PARTITION OF s7_part FOR VALUES FROM (MINVALUE) TO ('vz');
CREATE TABLE s7_part_hi PARTITION OF s7_part FOR VALUES FROM ('vz') TO (MAXVALUE);
INSERT INTO s7_part SELECT w, 'routed under glibc 2.28' FROM corpus;
INSERT INTO s7_part SELECT w, 'routed under glibc 2.28' FROM readable;

-- ---------------------------------------------------------------- scene 8
-- CHECK constraint.  Nothing revalidates it on an upgrade.
DROP TABLE IF EXISTS s8_check;
CREATE TABLE s8_check (w text COLLATE "sv_SE.utf8" CHECK (w < 'vz'));
-- only the rows the constraint accepted under glibc 2.28, which is exactly
-- what a real table would contain: everything that ever passed the CHECK.
INSERT INTO s8_check SELECT w FROM corpus WHERE w < 'vz';

-- ---------------------------------------------------------------- scene 9
-- Stored generated column, and a materialized view with its own index.
-- PostgreSQL accepts the expression because text comparison is marked
-- immutable.  glibc is what makes that a lie.
DROP TABLE IF EXISTS s9_gen;
CREATE TABLE s9_gen (
  w      text COLLATE "sv_SE.utf8",
  bucket text GENERATED ALWAYS AS (CASE WHEN w < 'vz' THEN 'early' ELSE 'late' END) STORED
);
INSERT INTO s9_gen (w) SELECT w FROM corpus;

DROP MATERIALIZED VIEW IF EXISTS s9_mv;
CREATE MATERIALIZED VIEW s9_mv AS
  SELECT row_number() OVER (ORDER BY w) AS pos, w FROM s1_words;
CREATE UNIQUE INDEX s9_mv_idx ON s9_mv (w);

-- --------------------------------------------------------------- control
-- Same shape under en_US.utf8, a locale this pair does not change.  If
-- anything in the control moves between the three states, the run is void.
DROP TABLE IF EXISTS c0_control;
CREATE TABLE c0_control (id int, w text COLLATE "en_US.utf8");
INSERT INTO c0_control SELECT id, w FROM corpus;
INSERT INTO c0_control SELECT 100000 + row_number() OVER (ORDER BY w), w FROM readable;
CREATE INDEX c0_idx ON c0_control (w);

ANALYZE;

\echo '--- objects built ---'
SELECT c.relname, c.relkind, pg_size_pretty(pg_relation_size(c.oid)) AS size
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r','i','m','p','I')
ORDER BY c.relname;
