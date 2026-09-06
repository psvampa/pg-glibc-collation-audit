-- Probe: does C.UTF-8 sort the same way on these two nodes?
--
-- Run this SAME script, UNEDITED, on both nodes (old glibc / new glibc), then
-- diff the two outputs. Unlike collation_confirmation_template.sql there are no
-- placeholders to fill in: the corpus below is derived from the rule that
-- changes, not chosen per site.
--
-- Why C.UTF-8 gets its own file:
--
--   * The source-diff audit cannot see it. localedata/locales/C exists
--     upstream only from glibc 2.35, while RHEL8 and RHEL9 both ship a
--     BACKPORTED copy -- a file in neither tag, which no tag-to-tag diff can
--     compare. scripts/diff_node_locales.py compares the two nodes' own copies
--     and settles the DATA; it cannot settle the ORDER, because the backported
--     file defines its collation with ellipsis ranges and localedef computes
--     those weights when the locale is built.
--
--   * PostgreSQL cannot warn either. Under the libc provider,
--     get_collation_actual_version() returns NULL for "C", for "POSIX" and for
--     anything whose name STARTS WITH "C." -- the pg_strncasecmp("C.", ...)
--     test, in every branch from PG 14 on (src/backend/utils/adt/pg_locale.c
--     through PG 17, pg_locale_libc.c from PG 18). So collversion and
--     datcollversion stay NULL and no mismatch check can ever fire.
--
--   * It is usually the DATABASE collation. initdb inherits the locale from
--     the environment and minimal container images ship LANG=C.UTF-8, which
--     makes every `text` column without an explicit COLLATE depend on it.
--
--   * It has already moved twice: between RHEL8 and RHEL9 (measured -- U+007F
--     sorted AFTER U+FFFF under glibc 2.28 and before it under 2.34), and
--     WITHIN RHEL8, in glibc-2.28-93.el8 (RHEL 8.2, RHSA-2020:1828, Red Hat
--     bug 1361965), which rewrote those ellipsis expressions so that code
--     points above U+10000 gained weights at all. "Same RHEL major" is not a
--     control.
--
-- THE POSITIVE CONTROL IS INVERTED HERE. Everywhere else in this project,
-- agreement with LC_ALL=C means the locale was never generated and the
-- comparison proves nothing. For C.UTF-8, agreement with byte order is the
-- CORRECT answer -- it is what the fix produces, and what upstream's
-- codepoint_collation guarantees from glibc 2.35 on. Query 2 spells out how to
-- read it, including the third case where agreement means neither.
--
-- Notes before you run it:
--   * DROPS AND RECREATES a table named c_utf8_probe. Point it at a scratch
--     database.
--   * Needs the collation to be named C.utf8 in pg_collation. If it is not,
--     run SELECT pg_import_system_collations('pg_catalog'); as superuser --
--     AFTER restarting PostgreSQL, or it imports the pre-restart set and
--     reports success (measured: 72 collations versus 1006).
--   * Needs PostgreSQL 15+ for pg_collation_actual_version() and
--     datlocprovider. On 13/14 delete queries 4 and 5.
--   * Needs standard_conforming_strings on (the default) for the U&'\+xxxxxx'
--     literals below. Checked, not assumed.
\set ON_ERROR_STOP on

\echo '=== 0. node identity: a result is bound to the build it ran on ==='
\! rpm -q glibc 2>/dev/null || echo 'rpm not available -- record the glibc build by hand'
\! ldd --version | head -1
\! locale -a | grep -ix 'c.utf8\|c.utf-8' || echo '!! C.utf8 is NOT in locale -a on this node'

\echo '=== 0b. guards: without these, both nodes can agree for the wrong reason ==='
SHOW server_encoding;
SHOW standard_conforming_strings;

-- SQL_ASCII compares bytes for everything, so every query below would agree on
-- any two nodes and mean nothing.
DO $$
BEGIN
  IF current_setting('server_encoding') <> 'UTF8' THEN
    RAISE EXCEPTION
      'server_encoding is %, not UTF8. Every comparison in this probe would '
      'collapse to byte order and two nodes would agree while proving nothing.',
      current_setting('server_encoding');
  END IF;
  IF current_setting('standard_conforming_strings') <> 'on' THEN
    RAISE EXCEPTION
      'standard_conforming_strings is off; the U&''\+xxxxxx'' literals below '
      'would not mean what they say.';
  END IF;
END $$;

-- The collation under test must be the LIBC one. PostgreSQL 17 added a
-- BUILTIN provider whose C.UTF-8 (pg_c_utf8) is version-independent by
-- design: measuring that would make both nodes agree for a reason that has
-- nothing to do with glibc. It is also the recommended mitigation -- see
-- docs/scope.md -- which is exactly why it must not be mistaken for the thing
-- being measured.
DO $$
DECLARE prov "char";
BEGIN
  SELECT collprovider INTO prov
  FROM pg_collation
  WHERE collname = 'C.utf8'
    AND collnamespace = 'pg_catalog'::regnamespace;
  IF prov IS NULL THEN
    RAISE EXCEPTION
      'no collation named C.utf8 in pg_catalog. Restart PostgreSQL, then run '
      'SELECT pg_import_system_collations(''pg_catalog''); as superuser.';
  END IF;
  IF prov <> 'c' THEN
    RAISE EXCEPTION
      'the collation named C.utf8 has provider %, not the libc provider ''c''. '
      'This probe measures glibc; a builtin collation cannot change with it.',
      prov;
  END IF;
END $$;

\echo '=== 0c. the C-ish collations on this node, and their versions ==='
-- Expect every libc row to carry a NULL collversion. That is the blind spot,
-- asserted rather than described: see query 4.
SELECT collname, collprovider, collversion,
       pg_collation_actual_version(oid) AS actual
FROM pg_collation
WHERE collname IN ('C', 'POSIX', 'C.utf8', 'C.UTF-8', 'pg_c_utf8')
  AND collnamespace = 'pg_catalog'::regnamespace
ORDER BY collname;

-- THE CORPUS. Not a sample: every value here is an endpoint of a range the
-- backported localedata/locales/C actually declares, or a UTF-8 length
-- boundary. Bug 22668 is "LC_COLLATE: Fix last character ellipsis handling",
-- so the first and last code point of each declared range are where an
-- expansion change shows up. The backport declares:
--
--     order_start forward
--     <U0000>..<UFFFF>
--     <U00010000>..<U0001FFFF>
--     ... one range per plane ...
--     <U00100000>..<U0010FFFF>
--     UNDEFINED
--     order_end
--
-- Surrogates U+D800..U+DFFF are deliberately absent: they are not valid UTF-8
-- and PostgreSQL rejects them. The U+xFFFF noncharacters ARE valid UTF-8 and
-- were sorted successfully on both nodes when this was first measured.
DROP TABLE IF EXISTS c_utf8_probe;
CREATE TABLE c_utf8_probe (
  cp   text PRIMARY KEY,
  kind text NOT NULL,
  w    text COLLATE "C.utf8" NOT NULL
);

INSERT INTO c_utf8_probe (cp, kind, w) VALUES
  ('U+0001',   'UTF-8 1-byte',        U&'\+000001'),
  ('U+0041',   'ASCII anchor A',      U&'\+000041'),
  ('U+005A',   'ASCII anchor Z',      U&'\+00005A'),
  ('U+0061',   'ASCII anchor a',      U&'\+000061'),
  ('U+007F',   'UTF-8 1-byte last',   U&'\+00007F'),
  ('U+0080',   'UTF-8 2-byte first',  U&'\+000080'),
  ('U+07FF',   'UTF-8 2-byte last',   U&'\+0007FF'),
  ('U+0800',   'UTF-8 3-byte first',  U&'\+000800'),
  ('U+FFFF',   'range 1 end / BMP',   U&'\+00FFFF'),
  ('U+10000',  'range 2 start',       U&'\+010000'),
  ('U+1FFFF',  'range 2 end',         U&'\+01FFFF'),
  ('U+20000',  'range 3 start',       U&'\+020000'),
  ('U+2FFFF',  'range 3 end',         U&'\+02FFFF'),
  ('U+30000',  'range 4 start',       U&'\+030000'),
  ('U+3FFFF',  'range 4 end',         U&'\+03FFFF'),
  ('U+40000',  'range 5 start',       U&'\+040000'),
  ('U+4FFFF',  'range 5 end',         U&'\+04FFFF'),
  ('U+50000',  'range 6 start',       U&'\+050000'),
  ('U+5FFFF',  'range 6 end',         U&'\+05FFFF'),
  ('U+60000',  'range 7 start',       U&'\+060000'),
  ('U+6FFFF',  'range 7 end',         U&'\+06FFFF'),
  ('U+70000',  'range 8 start',       U&'\+070000'),
  ('U+7FFFF',  'range 8 end',         U&'\+07FFFF'),
  ('U+80000',  'range 9 start',       U&'\+080000'),
  ('U+8FFFF',  'range 9 end',         U&'\+08FFFF'),
  ('U+90000',  'range 10 start',      U&'\+090000'),
  ('U+9FFFF',  'range 10 end',        U&'\+09FFFF'),
  ('U+A0000',  'range 11 start',      U&'\+0A0000'),
  ('U+AFFFF',  'range 11 end',        U&'\+0AFFFF'),
  ('U+B0000',  'range 12 start',      U&'\+0B0000'),
  ('U+BFFFF',  'range 12 end',        U&'\+0BFFFF'),
  ('U+C0000',  'range 13 start',      U&'\+0C0000'),
  ('U+CFFFF',  'range 13 end',        U&'\+0CFFFF'),
  ('U+D0000',  'range 14 start',      U&'\+0D0000'),
  ('U+DFFFF',  'range 14 end',        U&'\+0DFFFF'),
  ('U+E0000',  'range 15 start',      U&'\+0E0000'),
  ('U+EFFFF',  'range 15 end',        U&'\+0EFFFF'),
  ('U+F0000',  'range 16 start',      U&'\+0F0000'),
  ('U+FFFFF',  'range 16 end',        U&'\+0FFFFF'),
  ('U+100000', 'range 17 start',      U&'\+100000'),
  ('U+10FFFF', 'range 17 end / last', U&'\+10FFFF');

-- A silently dropped code point is the same class of error as a truncated
-- locale directory: it makes the comparison narrower and the result cleaner.
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM c_utf8_probe;
  IF n <> 41 THEN
    RAISE EXCEPTION 'corpus is % row(s), expected 41. A value was rejected or '
                    'collapsed; the comparison below would be narrower than '
                    'the one this file documents.', n;
  END IF;
END $$;

\echo '=== 1. the order C.utf8 produces on this node (diff this) ==='
-- Single sort key. PostgreSQL breaks strcoll ties with strcmp for a
-- deterministic collation, so the printed order is total either way -- which
-- also means this query CANNOT see a tie. Query 6b is the disambiguator.
SELECT row_number() OVER (ORDER BY w COLLATE "C.utf8") AS pos, cp, kind
FROM c_utf8_probe
ORDER BY pos;

\echo '=== 2. does C.utf8 equal byte order? READ THE THREE CASES ==='
-- false -> the glibc 2.28-era bug. C.UTF-8 is NOT code point order; query 1
--          shows where it diverges. An index on this collation moves on
--          upgrade.
-- true  -> the corrected order: glibc >= 2.34, or upstream >= 2.35 where
--          codepoint_collation makes it byte order by construction. DO NOT
--          read this as "the locale fell back to C" -- for C.UTF-8 agreement
--          with byte order IS the right answer, the opposite of the rule the
--          rest of this project uses.
-- true  -> ALSO what you get on a build where the weights above U+10000 are
--          all TIED and PostgreSQL's byte tie-break supplied the order. That
--          is the shape of the intra-RHEL8 change. Query 6b tells the two
--          apart, and nothing inside PostgreSQL can.
WITH under_locale AS (
  SELECT cp, row_number() OVER (ORDER BY w COLLATE "C.utf8") AS pos
  FROM c_utf8_probe
), under_bytes AS (
  SELECT cp, row_number() OVER (ORDER BY w COLLATE "C") AS pos
  FROM c_utf8_probe
)
SELECT bool_and(l.pos = b.pos)                        AS c_utf8_equals_byte_order,
       count(*) FILTER (WHERE l.pos <> b.pos)         AS positions_differing
FROM under_locale l JOIN under_bytes b USING (cp);

\echo '=== 3. the named pairs, so a diff says which side is which ==='
-- U+007F < U+FFFF is THE measured inversion: false on glibc 2.28, true on
-- 2.34. U+10000 < U+20000 is the above-BMP pair that covers the intra-RHEL8
-- change, where those code points had no weights at all before 8.2.
SELECT pair, holds FROM (
  SELECT 1 AS n, 'U+007F  < U+FFFF' AS pair,
         (SELECT w FROM c_utf8_probe WHERE cp = 'U+007F')
       < (SELECT w FROM c_utf8_probe WHERE cp = 'U+FFFF') AS holds
  UNION ALL
  SELECT 2, 'U+07FF  < U+FFFF',
         (SELECT w FROM c_utf8_probe WHERE cp = 'U+07FF')
       < (SELECT w FROM c_utf8_probe WHERE cp = 'U+FFFF')
  UNION ALL
  SELECT 3, 'U+FFFF  < U+10FFFF',
         (SELECT w FROM c_utf8_probe WHERE cp = 'U+FFFF')
       < (SELECT w FROM c_utf8_probe WHERE cp = 'U+10FFFF')
  UNION ALL
  SELECT 4, 'U+10000 < U+20000',
         (SELECT w FROM c_utf8_probe WHERE cp = 'U+10000')
       < (SELECT w FROM c_utf8_probe WHERE cp = 'U+20000')
  UNION ALL
  SELECT 5, 'U+FFFFF < U+100000',
         (SELECT w FROM c_utf8_probe WHERE cp = 'U+FFFFF')
       < (SELECT w FROM c_utf8_probe WHERE cp = 'U+100000')
) t ORDER BY n;

\echo '=== 4. collversion asserted as expected-NULL (PostgreSQL 15+) ==='
-- Not a mismatch check: a mismatch check can never fire for a C.* name, so
-- running one here would be theatre. This asserts the blind spot instead. If
-- both_null_as_expected ever comes back false, PostgreSQL changed its
-- pg_strncasecmp("C.", ...) special-case -- which is a finding worth
-- recording, and would mean C.UTF-8 finally warns for itself.
SELECT collname,
       collversion,
       pg_collation_actual_version(oid) AS actual,
       (collversion IS NULL AND pg_collation_actual_version(oid) IS NULL)
         AS both_null_as_expected
FROM pg_collation
WHERE collname = 'C.utf8'
  AND collprovider = 'c'
  AND collnamespace = 'pg_catalog'::regnamespace;

SELECT datname,
       datcollate,
       datcollversion,
       pg_database_collation_actual_version(oid) AS actual,
       (datcollversion IS NULL
        AND pg_database_collation_actual_version(oid) IS NULL)
         AS both_null_as_expected
FROM pg_database
WHERE datlocprovider = 'c'
  AND datcollate ~ '^[Cc]\.';

\echo '=== 5. is THIS database exposed through its default collation? ==='
-- The full index / partition-key / column / constraint inventory lives in
-- sql/collation_confirmation_template.sql. This is only the one-line answer
-- for the database default, because that is the way C.UTF-8 normally reaches
-- your data: a text column with no explicit COLLATE resolves to
-- pg_database.datcollate at runtime.
SELECT datname,
       datlocprovider,
       datcollate,
       CASE
         WHEN datlocprovider <> 'c' THEN 'not libc -- this probe does not apply'
         WHEN datcollate IN ('C', 'POSIX') THEN 'byte order, immutable'
         WHEN datcollate ~ '^[Cc]\.' THEN
           'EXPOSED via C.UTF-8, and no collversion can ever warn'
         ELSE 'EXPOSED via ' || datcollate || ' (collversion can warn)'
       END AS verdict
FROM pg_database
WHERE datname = current_database();

\echo '=== 6. the same order, read through a real btree index ==='
-- The question only matters because indexes store this order. Building one
-- makes that concrete rather than assumed.
CREATE INDEX c_utf8_probe_w_idx ON c_utf8_probe (w);
SET enable_seqscan = off;
SELECT row_number() OVER (ORDER BY w) AS pos, cp
FROM c_utf8_probe
ORDER BY pos;
RESET enable_seqscan;

\echo '=== 6b. tie detector: needs no PostgreSQL, and PostgreSQL cannot do it ==='
-- varstr_cmp and the sortsupport comparator both break a strcoll tie with
-- strcmp, and abbreviated keys cannot bypass it. So a build where every
-- above-BMP weight is TIED is indistinguishable, through SQL alone, from one
-- with correct byte order -- and that is precisely the shape of the
-- intra-RHEL8 change. Equal sort keys below mean every SQL answer above came
-- from PostgreSQL's byte tie-break, not from the locale.
\! python3 -c "import locale; locale.setlocale(locale.LC_COLLATE,'C.utf8'); k=[locale.strxfrm(chr(c)).encode().hex() for c in (0x10000,0x20000)]; print('U+10000 key:',k[0]); print('U+20000 key:',k[1]); print('TIED -- the locale gives these no distinct weights' if k[0]==k[1] else 'distinct weights')" 2>/dev/null || echo 'python3 not available; run the strxfrm check by hand (see docs/confirming-on-a-real-system.md)'

\echo '=== done. diff this output against the other node. ==='
