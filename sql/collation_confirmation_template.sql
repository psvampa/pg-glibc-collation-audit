-- Template: confirm in real PostgreSQL whether specific locales actually sort
-- differently, once the source-diff audit (scripts/) has told you which
-- locale identifiers to check on this OS/glibc version.
--
-- Run this SAME script on both nodes (old glibc / new glibc), then diff the
-- two outputs with `diff`.
--
-- Replace <LOCALE>/<value*> below with the locale(s) flagged by the audit and
-- with strings known (from reading the diff, see the step 2 and step 4 output)
-- to hit the specific rule that changed.
--
-- Notes before you run it:
--   * It DROPS AND RECREATES a table named collation_test in the current
--     schema. Point it at a scratch database.
--   * The first statement, pg_import_system_collations(), WRITES TO
--     pg_catalog and needs superuser. It is idempotent and only adds
--     collations already present in `locale -a`, but it is a catalog change:
--     if you are running the inventory queries against production, run them
--     without that line and accept that a langpack installed after initdb
--     will be missing from pg_collation.
--   * RESTART PostgreSQL after installing a langpack, BEFORE running this.
--     The function takes its list from `locale -a` (a fresh subprocess, which
--     sees the new locales) but validates each one with setlocale() in the
--     backend, which resolves against the locale-archive the postmaster
--     already mapped. Without a restart it returns success with a plausible
--     count and silently imports only the old set: measured on Rocky 8 /
--     PG 16.15, 72 collations imported and sv_SE.utf8 still absent, versus
--     1007 after a restart.
--   * Needs PostgreSQL 15 or newer: it reads pg_database.datlocprovider and
--     calls pg_collation_actual_version(). On 13/14, drop the datlocprovider
--     conditions (no database can use a non-libc provider there) and delete
--     the actual-version block.
--   * Use the generated locale names step 3 prints (sv_SE.utf8), and check
--     `locale -a` first: if a locale is not generated on the box,
--     sort/PostgreSQL silently fall back to C, and two boxes both missing it
--     will agree with each other while proving nothing. Note the spelling:
--     localedef normalises the codeset when it builds the locale, so
--     localedata/SUPPORTED says sv_SE.UTF-8 while `locale -a` and
--     pg_collation both say sv_SE.utf8. COLLATE "sv_SE.UTF-8" does not
--     exist.

SELECT pg_import_system_collations('pg_catalog');

-- READ THIS FIRST. It changes how every inventory below reads.
--
-- A column declared `text` with no explicit COLLATE does not get a libc
-- collation. It gets OID 100, `default`, whose collprovider is 'd' -- a
-- pointer resolved at runtime from pg_database. In a typical database that is
-- MOST text columns, and in a container it is usually all of them, because
-- initdb inherits the locale from the environment and minimal images ship
-- LANG=C.UTF-8.
--
-- So the question "is this database exposed to a glibc collation change?" is
-- answered here, once, and not per column. If datlocprovider is 'c' and
-- datcollate is anything other than C or POSIX, every default-collated column
-- in the database is exposed -- C.UTF-8 very much included: PostgreSQL
-- special-cases only the literal strings "C" and "POSIX" to byte comparison
-- (src/backend/utils/adt/pg_locale_libc.c), so libc C.UTF-8 goes through
-- strcoll like any other locale. The `builtin` provider's C.UTF-8 (PG 17+) is
-- a different thing and is not exposed.
\echo '--- is this database exposed at all? ---'
SELECT d.datname,
       d.datlocprovider AS provider,
       d.datcollate,
       CASE
         WHEN d.datlocprovider <> 'c'
           THEN 'not libc -- a glibc change does not affect the default'
         WHEN d.datcollate IN ('C', 'POSIX')
           THEN 'byte order -- immune'
         ELSE 'EXPOSED -- every column with no explicit COLLATE uses this'
       END AS default_collation_status
FROM pg_database d
WHERE d.datname = current_database();

-- One definition of "exposed", used by every inventory below: an explicit
-- libc collation other than C/POSIX, OR the database default when that
-- resolves to one. Temporary and session-scoped -- it disappears when you
-- disconnect and changes nothing in the database.
CREATE OR REPLACE TEMP VIEW exposed_collation AS
SELECT c.oid AS colloid,
       c.collname,
       CASE WHEN c.collprovider = 'd'
            THEN 'database default -> ' || d.datcollate
            ELSE c.collname
       END AS effective_collation
FROM pg_collation c
CROSS JOIN pg_database d
WHERE d.datname = current_database()
  AND ( (c.collprovider = 'c' AND c.collname NOT IN ('C', 'POSIX'))
     OR (c.collprovider = 'd' AND d.datlocprovider = 'c'
         AND d.datcollate NOT IN ('C', 'POSIX')) );

-- Confirms which glibc version this collation was imported against.
SELECT collname, collcollate, collversion
FROM pg_collation
WHERE collname IN ('<LOCALE>');  -- e.g. 'sv_SE.utf8','sv_FI.utf8','or_IN'

DROP TABLE IF EXISTS collation_test;
CREATE TABLE collation_test (w text COLLATE "<LOCALE>");
INSERT INTO collation_test VALUES ('<value1>'), ('<value2>'), ('<value3>');
CREATE INDEX ON collation_test (w);

-- The comparison itself. glibc's strcoll DOES report distinct strings as equal
-- (measured on RHEL8/RHEL9: ~0.1% of random pairs under sv_SE/en_US/de_DE,
-- ~10% under ko_KR), so ties are real and common. PostgreSQL breaks them
-- itself, by bytes, in src/backend/utils/adt/varlena.c:
--
--     result = pg_strcoll(...);
--     /* Break tie if necessary. */
--     if (result == 0 && pg_locale_deterministic(...))
--         result = strcmp(...);
--
-- present in both the row comparator and the sortsupport comparator, in every
-- release from 13 to 18. Abbreviated keys do not bypass it: returning 0 from
-- the abbreviated comparator means "indeterminate", not "equal", and forces
-- the full comparator (src/include/utils/sortsupport.h).
--
-- Nondeterministic collations, where the tie-break is skipped, are rejected
-- for every provider except ICU (src/backend/commands/collationcmds.c), so for
-- a libc collation -- all this script looks at -- ORDER BY is always a total,
-- plan-independent order. The trailing `COLLATE "C"` is therefore a NO-OP,
-- kept only to state the intent.
--
-- Where tie order really is unspecified: `sort(1)`, which resolves
-- collation-tied lines by INPUT order, so two nodes fed differently-ordered
-- input can differ for reasons that have nothing to do with glibc. Feed both
-- sides byte-identical input.
--
-- The position column makes the diff point straight at the first divergence.
\echo '--- ORDER BY under <LOCALE> ---'
SELECT ROW_NUMBER() OVER (ORDER BY w COLLATE "<LOCALE>", w COLLATE "C") AS pos,
       w
FROM collation_test
ORDER BY pos;

-- What is actually exposed: every index on a libc-provided, non-C/POSIX
-- collation. Run this on a real instance before a migration to know what you
-- would need to reindex if a locale turns out to be affected. The access
-- method is reported because a REINDEX is the fix for btree/gist ordering,
-- while hash and other AMs have different exposure.
\echo '--- indexes on non-C/POSIX libc collations ---'
SELECT i.indexrelid::regclass AS index_name,
       i.indrelid::regclass   AS table_name,
       am.amname              AS access_method,
       x.effective_collation
FROM pg_index i
JOIN pg_class ir ON ir.oid = i.indexrelid
JOIN pg_am am ON am.oid = ir.relam
CROSS JOIN LATERAL unnest(i.indcollation::oid[]) AS ic(oid)
JOIN exposed_collation x ON x.colloid = ic.oid
WHERE ir.relnamespace NOT IN ('pg_catalog'::regnamespace,
                              'information_schema'::regnamespace)
-- The expressions are repeated rather than ordering by the output aliases:
-- ORDER BY on a bare regclass sorts by OID, not by name, and `alias::text` is
-- an expression, so its names resolve against the FROM clause and not against
-- the SELECT list.
ORDER BY i.indrelid::regclass::text, i.indexrelid::regclass::text;

-- The severe one, and the reason a REINDEX list is not the whole answer: a
-- text partition key is evaluated with a collation. If the collation changes,
-- rows can belong in a different partition than the one they are physically
-- stored in, and NO amount of reindexing fixes that -- the rows have to be
-- moved. Check this before you migrate, not after.
\echo '--- partitioned tables keyed on a non-C/POSIX libc collation (REINDEX does NOT fix these) ---'
SELECT pt.partrelid::regclass AS partitioned_table,
       x.effective_collation,
       pg_get_partkeydef(pt.partrelid) AS partition_key
FROM pg_partitioned_table pt
CROSS JOIN LATERAL unnest(pt.partcollation::oid[]) AS pc(oid)
JOIN exposed_collation x ON x.colloid = pc.oid
ORDER BY pt.partrelid::regclass::text;

-- Every column carrying such a collation, indexed or not. This is the full
-- surface: anything comparing these columns (a query plan, a constraint, a
-- generated column, a materialized view's stored order) inherits the change.
\echo '--- columns using a non-C/POSIX libc collation ---'
SELECT a.attrelid::regclass AS table_name,
       a.attname            AS column_name,
       t.typname            AS type,
       x.effective_collation
FROM pg_attribute a
JOIN pg_class k ON k.oid = a.attrelid
JOIN pg_type t ON t.oid = a.atttypid
JOIN exposed_collation x ON x.colloid = a.attcollation
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND k.relkind IN ('r', 'p', 'm', 'f')
  AND k.relnamespace NOT IN ('pg_catalog'::regnamespace,
                             'information_schema'::regnamespace)
ORDER BY a.attrelid::regclass::text, a.attname;

-- CHECK and EXCLUDE constraints on tables holding such columns. Unlike indexes
-- and partition keys, a constraint does not record its collations in a
-- catalog column, so this cannot be answered precisely -- it is a
-- MANUAL REVIEW LIST. Read each definition and decide whether it does a text
-- comparison (a range check like `code BETWEEN 'a' AND 'm'`, an ordering
-- assertion) that the collation change would alter. A CHECK constraint is not
-- re-validated on upgrade, so a violated one stays silently violated.
\echo '--- CHECK/EXCLUDE constraints to review by hand ---'
SELECT con.conrelid::regclass AS table_name,
       con.conname            AS constraint_name,
       CASE con.contype WHEN 'c' THEN 'CHECK' WHEN 'x' THEN 'EXCLUDE' END AS kind,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class kr ON kr.oid = con.conrelid
WHERE con.contype IN ('c', 'x')
  AND kr.relnamespace NOT IN ('pg_catalog'::regnamespace,
                              'information_schema'::regnamespace)
  AND EXISTS (
        SELECT 1
        FROM pg_attribute a
        JOIN exposed_collation x ON x.colloid = a.attcollation
        WHERE a.attrelid = con.conrelid
          AND a.attnum > 0
          AND NOT a.attisdropped)
ORDER BY con.conrelid::regclass::text, con.conname;

-- PostgreSQL's own signal: fires on ANY glibc version bump, whether or not
-- your data would actually sort differently, and stays silent if a distro
-- patches collation data without moving the reported version. Conservative in
-- one direction, blind in the other -- which is why this audit exists.
-- Requires PostgreSQL 15+.
--
-- THIRD blind spot, and the one that bites hardest here: neither this query
-- nor the one after it can ever report a C.* collation. Under the libc
-- provider, get_collation_actual_version() returns NULL for "C", for
-- "POSIX", and for anything whose name STARTS WITH "C." -- the
-- pg_strncasecmp("C.", ...) test, present in every branch from PG 14 on
-- (src/backend/utils/adt/pg_locale.c through PG 17, pg_locale_libc.c from
-- PG 18). So collversion stays NULL, the
-- IS DISTINCT FROM never fires, and libc C.UTF-8 -- which the note at the top
-- of this file explains IS exposed, and which is the default in most
-- containers -- is invisible to both. For C.UTF-8 the empirical ORDER BY
-- comparison above is the only signal you get.
\echo '--- collversion mismatch, named collations (PostgreSQL 15+) ---'
SELECT collname, collversion, pg_collation_actual_version(oid) AS actual
FROM pg_collation
WHERE collprovider = 'c'
  AND collversion IS DISTINCT FROM pg_collation_actual_version(oid);

-- The database default has its own version field, and pg_collation has no row
-- for it -- the query above cannot see it. This is the same blind spot the
-- inventories had: ask pg_database, not pg_collation. Same C.* caveat: a
-- database created with datcollate = C.UTF-8 under the libc provider carries
-- a NULL datcollversion and will never show up here.
\echo '--- collversion mismatch, the database default (PostgreSQL 15+) ---'
SELECT datname,
       datcollate,
       datcollversion,
       pg_database_collation_actual_version(oid) AS actual
FROM pg_database
WHERE datlocprovider = 'c'
  AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid);
