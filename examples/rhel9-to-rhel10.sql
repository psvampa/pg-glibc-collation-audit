-- Worked example: glibc 2.34 (RHEL9) -> glibc 2.39 (RHEL10).
--
-- Run on both a RHEL9-family and a RHEL10-family node (Rocky/Alma/Oracle are
-- binary-compatible for this purpose), and diff the two outputs. Feed both
-- sides this identical file: a comparison of differently-ordered input differs
-- for reasons that have nothing to do with glibc.
--
-- Measured on (state the build, not just the upstream version -- the distro
-- adds backports the upstream tag diff cannot see):
--   RHEL9  side: Rocky Linux 9.3,  glibc-2.34-83.el9.7,     PostgreSQL 18.6
--   RHEL10 side: Rocky Linux 10.1, glibc-2.39-58.el10_1.2,  PostgreSQL 18.6
-- Re-confirmed on the newer distro builds glibc-2.34-275.el9_8 and
-- glibc-2.39-128.el10_2: same result, so a backport between those builds does
-- not change it either.
--
-- Requires, on each node:
--   rm -f /etc/rpm/macros.image-language-conf   # or dnf installs English only
--   dnf install -y glibc-all-langpacks
--   systemctl restart postgresql-18             # BEFORE importing collations
--
-- Locales under test, from this pair's findings (see ../docs/results.md):
--   affected:     th_TH            -- real LC_COLLATE rewrite, verdict open
--                 ber_DZ, kab_DZ   -- flagged; believed a role swap
--   step 4 only:  ko_KR            -- flagged by step 4, cleared by step 5
--   not affected: en_US, de_DE, fr_FR (negative controls)

SELECT pg_import_system_collations('pg_catalog');

-- If a locale is missing here it is NOT generated on this node, and every
-- comparison below silently falls back to C. Two nodes both missing it agree
-- with each other while proving nothing.
\echo '--- collations under test: all must be present on BOTH nodes ---'
SELECT collname, collcollate, collversion
FROM pg_collation
WHERE collname IN ('th_TH.utf8','ber_DZ.utf8','kab_DZ.utf8','ko_KR.utf8',
                   'en_US.utf8','de_DE.utf8','fr_FR.utf8')
ORDER BY collname;

-- ---------------------------------------------------------------- th_TH ----
-- What changed: 2.34..2.39 deletes 220 `collating-element` definitions and
-- replaces them with `copy "iso14651_t1"` plus CLDR tailoring. Those 220 are
-- exactly the 5 Thai leading vowels (U+0E40..U+0E44) x 44 consonants.
--
-- A Thai leading vowel is written BEFORE its consonant but pronounced after,
-- so the old locale treated each pair as one collating element sorting at the
-- consonant's position. Removing them makes each character sort on its own.
--
-- Two code points in the consonant range never had such an element:
-- U+0E24 (ฤ) and U+0E26 (ฦ), the vowel-like letters. They are therefore the
-- asymmetry in the change, and the strings most likely to move.
DROP TABLE IF EXISTS th_test;
CREATE TABLE th_test (w text COLLATE "th_TH.utf8", note text);
INSERT INTO th_test VALUES
  (E'ก',   'U+0E01 bare consonant, first'),
  (E'ฮ',   'U+0E2E bare consonant, last'),
  (E'ฤ',   'U+0E24 vowel-like, never had an element'),
  (E'ฦ',   'U+0E26 vowel-like, never had an element'),
  (E'เก',  'U+0E40 U+0E01  leading vowel + consonant that HAD an element'),
  (E'ไก',  'U+0E44 U+0E01  same, different leading vowel'),
  (E'ไก่', 'U+0E44 U+0E01 U+0E48  a real word (chicken), with tone mark'),
  (E'เฤ',  'U+0E40 U+0E24  leading vowel + consonant that had NO element'),
  (E'เฦ',  'U+0E40 U+0E26  same');
\echo '--- th_TH ORDER BY (the open verdict for this pair) ---'
SELECT w, note FROM th_test ORDER BY w;
CREATE INDEX ON th_test (w);

-- Positive control: this order MUST differ from byte order. If the two agree,
-- the locale was not applied and every result above is meaningless.
\echo '--- th_TH positive control: must NOT equal C byte order ---'
SELECT (SELECT string_agg(w, ' ' ORDER BY w COLLATE "th_TH.utf8") FROM th_test)
       IS DISTINCT FROM
       (SELECT string_agg(w, ' ' ORDER BY w COLLATE "C") FROM th_test)
       AS th_TH_differs_from_C;

-- -------------------------------------------------------- ber_DZ/kab_DZ ----
-- Steps 1-3 flag both. Inspection suggested a role swap: the same ruleset
-- relocated to the other file, which shows as a large diff while the effective
-- order does not move. These strings exercise the Berber-specific letters.
DROP TABLE IF EXISTS ber_test;
CREATE TABLE ber_test (w text COLLATE "ber_DZ.utf8");
INSERT INTO ber_test VALUES
  ('azul'),('ɛemmar'),('ɣur'),('ḍaɛef'),('ḥader'),('ṭṭaqa'),('ẓẓay'),
  ('ccna'),('tamurt'),('yir');
\echo '--- ber_DZ ORDER BY ---'
SELECT w FROM ber_test ORDER BY w;
CREATE INDEX ON ber_test (w);

DROP TABLE IF EXISTS kab_test;
CREATE TABLE kab_test (w text COLLATE "kab_DZ.utf8");
INSERT INTO kab_test VALUES
  ('azul'),('ɛemmar'),('ɣur'),('ḍaɛef'),('ḥader'),('ṭṭaqa'),('ẓẓay'),
  ('ccna'),('tamurt'),('yir');
\echo '--- kab_DZ ORDER BY (same input as ber_DZ, by design) ---'
SELECT w FROM kab_test ORDER BY w;
CREATE INDEX ON kab_test (w);

-- ---------------------------------------------------------------- ko_KR ----
-- Step 4 flags ko_KR on this pair too, because its LC_COLLATE relies on
-- ellipsis ranges that localedef expands at build time. Step 5 found no change
-- to that expansion between 2.34 and 2.39, so this is a control: it should be
-- identical. The strings are the Hangul block boundary, which is where the
-- 2.28->2.34 bug lived -- see ../docs/results.md.
DROP TABLE IF EXISTS ko_test;
CREATE TABLE ko_test (w text COLLATE "ko_KR.utf8");
INSERT INTO ko_test VALUES
  (E'가'),(E'힢'),(E'힣'),(E'伽'),(E'佳'),(E'한');
\echo '--- ko_KR ORDER BY (U+D7A3 against Hanja; expected identical) ---'
SELECT w FROM ko_test ORDER BY w;
CREATE INDEX ON ko_test (w);

-- ----------------------------------------------------- negative controls ----
-- These must be identical on both nodes. If one of them differs, the setup is
-- wrong and nothing else on this page can be trusted.
DROP TABLE IF EXISTS en_test;
CREATE TABLE en_test (w text COLLATE "en_US.utf8");
INSERT INTO en_test VALUES ('1-1'),('11'),('a'),('A'),('b');
\echo '--- en_US ORDER BY (PostgreSQL wiki smoke test) ---'
SELECT w FROM en_test ORDER BY w;
CREATE INDEX ON en_test (w);

DROP TABLE IF EXISTS de_test;
CREATE TABLE de_test (w text COLLATE "de_DE.utf8");
INSERT INTO de_test VALUES ('Straße'),('Strasse'),('Stras'),('äpfel'),('apfel'),('zebra');
\echo '--- de_DE ORDER BY ---'
SELECT w FROM de_test ORDER BY w;
CREATE INDEX ON de_test (w);

DROP TABLE IF EXISTS fr_test;
CREATE TABLE fr_test (w text COLLATE "fr_FR.utf8");
INSERT INTO fr_test VALUES ('cote'),('côte'),('coté'),('côté'),('zone');
\echo '--- fr_FR ORDER BY (accent ordering) ---'
SELECT w FROM fr_test ORDER BY w;
CREATE INDEX ON fr_test (w);

-- ------------------------------------------------------------ inventory ----
\echo '--- indexes on non-C/POSIX libc collations ---'
SELECT i.indexrelid::regclass AS index_name,
       i.indrelid::regclass   AS table_name,
       c.collname
FROM pg_index i
CROSS JOIN LATERAL unnest(i.indcollation::oid[]) AS ic(oid)
JOIN pg_collation c ON c.oid = ic.oid
WHERE c.collprovider = 'c'
  AND c.collname NOT IN ('C', 'POSIX')
ORDER BY 1;

\echo '--- collversion mismatch check ---'
SELECT collname, collversion, pg_collation_actual_version(oid) AS actual
FROM pg_collation
WHERE collprovider = 'c'
  AND collversion IS DISTINCT FROM pg_collation_actual_version(oid);
