-- Template: confirm in real PostgreSQL whether specific locales actually sort
-- differently, once the source-diff audit (scripts/) has told you which
-- locale identifiers to check on this OS/glibc version.
--
-- Run this SAME script on both nodes (old glibc / new glibc), then diff the
-- two outputs by hand or with `diff`.
--
-- Replace <LOCALE>/<TABLE> below with the locale(s) flagged by the audit,
-- and the sample values with a handful of strings that are known (from
-- reading the diff, see scripts/filter_lc_collate_changes.py output) to hit
-- the specific rule that changed.

SELECT pg_import_system_collations('pg_catalog');

-- Confirms which glibc version this collation was imported against.
SELECT collname, collcollate, collversion
FROM pg_collation
WHERE collname IN ('<LOCALE>');  -- e.g. 'sv_SE','sv_FI','or_IN'

DROP TABLE IF EXISTS collation_test;
CREATE TABLE collation_test (w text COLLATE "<LOCALE>");
INSERT INTO collation_test VALUES ('<value1>'), ('<value2>'), ('<value3>');
\echo '--- ORDER BY under <LOCALE> ---'
SELECT w FROM collation_test ORDER BY w;
CREATE INDEX ON collation_test (w);

-- What is actually exposed: every btree index on a libc-provided,
-- non-C/POSIX collation. Run this on a real instance before a migration to
-- know what you'd need to reindex if a locale turns out to be affected.
\echo '--- indexes on non-C/POSIX collations ---'
SELECT i.indexrelid::regclass AS index_name,
       i.indrelid::regclass   AS table_name,
       c.collname
FROM pg_index i
CROSS JOIN LATERAL unnest(i.indcollation::oid[]) AS ic(oid)
JOIN pg_collation c ON c.oid = ic.oid
WHERE c.collprovider = 'c'
  AND c.collname NOT IN ('C', 'POSIX');

-- PostgreSQL's own signal: fires on ANY glibc version bump, whether or not
-- your data would actually sort differently. Conservative by design -- see
-- the article for why this alone is not enough to decide "safe" vs "not safe".
\echo '--- collversion mismatch check ---'
SELECT collname, collversion, pg_collation_actual_version(oid) AS actual
FROM pg_collation
WHERE collprovider = 'c'
  AND collversion IS DISTINCT FROM pg_collation_actual_version(oid);
