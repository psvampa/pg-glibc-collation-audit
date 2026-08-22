-- Worked example: glibc 2.28 (RHEL8) -> glibc 2.34 (RHEL9).
-- Run on both a RHEL8-family and a RHEL9-family node (Rocky/Alma/etc. are
-- binary-compatible for this purpose). Requires:
--   dnf install -y glibc-langpack-sv glibc-langpack-or glibc-langpack-en \
--                  glibc-langpack-de glibc-langpack-fr
--
-- Locales under test were chosen from the audit output for this exact pair
-- (see ../README.md "Worked example"):
--   affected:     sv_SE, sv_FI, sv_FI@euro, or_IN
--   not affected: en_US, de_DE, fr_FR (negative controls)

SELECT pg_import_system_collations('pg_catalog');
SELECT collname, collcollate, collversion
FROM pg_collation
WHERE collname IN ('sv_SE','sv_FI','or_IN','en_US','de_DE','fr_FR');

DROP TABLE IF EXISTS sv_test;
CREATE TABLE sv_test (w text COLLATE "sv_SE");
INSERT INTO sv_test VALUES ('va'),('Vasa'),('vind'),('wa'),('Wasa'),('wind');
\echo '--- sv_SE ORDER BY ---'
SELECT w FROM sv_test ORDER BY w;
CREATE INDEX ON sv_test (w);

DROP TABLE IF EXISTS svfi_test;
CREATE TABLE svfi_test (w text COLLATE "sv_FI");
INSERT INTO svfi_test VALUES ('va'),('Vasa'),('vind'),('wa'),('Wasa'),('wind');
\echo '--- sv_FI ORDER BY (inherits from sv_SE via copy, no tailoring of its own) ---'
SELECT w FROM svfi_test ORDER BY w;
CREATE INDEX ON svfi_test (w);

DROP TABLE IF EXISTS or_test;
CREATE TABLE or_test (w text COLLATE "or_IN");
INSERT INTO or_test VALUES (E'ହ'),(E'କ୍ଷ'),(E'ଔ'),(E'ଁ'),(E'ଂ'),(E'ଃ'),(E'କ');
\echo '--- or_IN ORDER BY ---'
SELECT w FROM or_test ORDER BY w;
CREATE INDEX ON or_test (w);

-- Negative controls: locales the audit says are NOT affected by this jump.
DROP TABLE IF EXISTS en_test;
CREATE TABLE en_test (w text COLLATE "en_US");
INSERT INTO en_test VALUES ('1-1'),('11');
\echo '--- en_US ORDER BY (PostgreSQL wiki smoke test) ---'
SELECT w FROM en_test ORDER BY w;
CREATE INDEX ON en_test (w);

\echo '--- indexes on non-C/POSIX collations ---'
SELECT i.indexrelid::regclass AS index_name,
       i.indrelid::regclass   AS table_name,
       c.collname
FROM pg_index i
CROSS JOIN LATERAL unnest(i.indcollation::oid[]) AS ic(oid)
JOIN pg_collation c ON c.oid = ic.oid
WHERE c.collprovider = 'c'
  AND c.collname NOT IN ('C', 'POSIX');

\echo '--- collversion mismatch check ---'
SELECT collname, collversion, pg_collation_actual_version(oid) AS actual
FROM pg_collation
WHERE collprovider = 'c'
  AND collversion IS DISTINCT FROM pg_collation_actual_version(oid);
