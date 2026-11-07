-- pg-glibc-audit confirmation/1: step 11 of audit.sh, confirmed in PostgreSQL
-- id: 3010dbdb18659761439d6f187c81211502f2f489c6fa63ff156648053b0596f9
-- encoding: LATIN1
-- old: glibc-2.28-251.el8_10.40.x86_64|2.28
-- new: glibc-2.34-275.el9_8.x86_64|2.34
-- Run this file unchanged on both servers, in a database of encoding LATIN1; it only reads:
--   psql -X -d DATABASE -f FILE > old.out 2>&1   (the old server)
--   psql -X -d DATABASE -f FILE > new.out 2>&1   (the new server)
-- then read the two outputs, not by diff:
--   python3 scripts/confirm_sql.py read FILE old.out new.out
-- not tested: sv_FI, sv_FI.iso88591, sv_SE, sv_SE.iso88591, swedish: 4 sequence(s) its compiled sort table names as changed, which no test here takes one by one
-- not checked in PostgreSQL by this file: en_US.utf8@ampm, en_US@ampm: it is not on the new machine
-- the locales of other encodings are in the files written beside this one: LATIN9, UTF8
-- not tested: character rules (LC_CTYPE) in a database of any encoding but UTF8: no file tests them; step 11's report says what it found
\set QUIET on
\set VERBOSITY verbose
\set ON_ERROR_STOP on
\set ECHO none
\pset pager off
\pset format unaligned
\pset tuples_only on
\pset footer off
\pset expanded off
\timing off
SET client_min_messages = warning;
\echo 'pg-glibc-audit confirmation|3010dbdb18659761439d6f187c81211502f2f489c6fa63ff156648053b0596f9|LATIN1'
SELECT 'database encoding|' || getdatabaseencoding();
SELECT 'server|' || current_setting('server_version');
SELECT getdatabaseencoding() = 'LATIN1' AS encoding_ok \gset
\if :encoding_ok
\else
\quit
\endif
-- collation sv_FI: sv_FI, sv_FI.iso88591, sv_SE, sv_SE.iso88591, swedish
\unset c1
\set ON_ERROR_STOP off
SELECT ('a' < 'b' COLLATE "sv_FI") IS NOT NULL AS c1 \gset
\set ON_ERROR_STOP on
\if :{?c1}
SELECT 'collation|sv_FI|usable|glibc ' || coalesce((SELECT pg_collation_actual_version(c.oid) FROM pg_collation c WHERE c.collname = 'sv_FI' AND c.collencoding IN (-1, (SELECT encoding FROM pg_database WHERE datname = current_database())) AND pg_collation_is_visible(c.oid)), 'NULL');
SELECT 'test|' || id || '|sv_FI|' || r || '|old ' || o || ', new ' || n || '|' || CASE m WHEN 'both' THEN 'measured on both machines' WHEN 'old' THEN 'measured on the old machine, the new one by the characters between' WHEN 'new' THEN 'measured on the new machine, the old one by the characters between' WHEN 'table' THEN 'from both machines'' compiled sort tables' END || '|' || CASE WHEN r = o THEN 'like the OLD machine' WHEN r = n THEN 'like the NEW machine' ELSE 'like NEITHER machine' END
FROM (SELECT id, o, n, m, CASE WHEN a < b COLLATE "sv_FI" THEN '<' WHEN a > b COLLATE "sv_FI" THEN '>' ELSE '=' END AS r
      FROM (VALUES
(1, '56010162', '77010161', '>', '<', 'both')
      ) AS v(id, ah, bh, o, n, m)
      CROSS JOIN LATERAL (SELECT convert_from(decode(ah, 'hex'), 'LATIN1') AS a, convert_from(decode(bh, 'hex'), 'LATIN1') AS b) AS s) AS t
ORDER BY id;
\else
\echo 'collation|sv_FI|CANNOT BE USED'
\endif
-- collation sv_FI.iso88591: sv_FI, sv_FI.iso88591, sv_SE, sv_SE.iso88591, swedish
\unset c2
\set ON_ERROR_STOP off
SELECT ('a' < 'b' COLLATE "sv_FI.iso88591") IS NOT NULL AS c2 \gset
\set ON_ERROR_STOP on
\if :{?c2}
SELECT 'collation|sv_FI.iso88591|usable|glibc ' || coalesce((SELECT pg_collation_actual_version(c.oid) FROM pg_collation c WHERE c.collname = 'sv_FI.iso88591' AND c.collencoding IN (-1, (SELECT encoding FROM pg_database WHERE datname = current_database())) AND pg_collation_is_visible(c.oid)), 'NULL');
SELECT 'test|' || id || '|sv_FI.iso88591|' || r || '|old ' || o || ', new ' || n || '|' || CASE m WHEN 'both' THEN 'measured on both machines' WHEN 'old' THEN 'measured on the old machine, the new one by the characters between' WHEN 'new' THEN 'measured on the new machine, the old one by the characters between' WHEN 'table' THEN 'from both machines'' compiled sort tables' END || '|' || CASE WHEN r = o THEN 'like the OLD machine' WHEN r = n THEN 'like the NEW machine' ELSE 'like NEITHER machine' END
FROM (SELECT id, o, n, m, CASE WHEN a < b COLLATE "sv_FI.iso88591" THEN '<' WHEN a > b COLLATE "sv_FI.iso88591" THEN '>' ELSE '=' END AS r
      FROM (VALUES
(1, '56010162', '77010161', '>', '<', 'both')
      ) AS v(id, ah, bh, o, n, m)
      CROSS JOIN LATERAL (SELECT convert_from(decode(ah, 'hex'), 'LATIN1') AS a, convert_from(decode(bh, 'hex'), 'LATIN1') AS b) AS s) AS t
ORDER BY id;
\else
\echo 'collation|sv_FI.iso88591|CANNOT BE USED'
\endif
-- collation sv_SE: sv_FI, sv_FI.iso88591, sv_SE, sv_SE.iso88591, swedish
\unset c3
\set ON_ERROR_STOP off
SELECT ('a' < 'b' COLLATE "sv_SE") IS NOT NULL AS c3 \gset
\set ON_ERROR_STOP on
\if :{?c3}
SELECT 'collation|sv_SE|usable|glibc ' || coalesce((SELECT pg_collation_actual_version(c.oid) FROM pg_collation c WHERE c.collname = 'sv_SE' AND c.collencoding IN (-1, (SELECT encoding FROM pg_database WHERE datname = current_database())) AND pg_collation_is_visible(c.oid)), 'NULL');
SELECT 'test|' || id || '|sv_SE|' || r || '|old ' || o || ', new ' || n || '|' || CASE m WHEN 'both' THEN 'measured on both machines' WHEN 'old' THEN 'measured on the old machine, the new one by the characters between' WHEN 'new' THEN 'measured on the new machine, the old one by the characters between' WHEN 'table' THEN 'from both machines'' compiled sort tables' END || '|' || CASE WHEN r = o THEN 'like the OLD machine' WHEN r = n THEN 'like the NEW machine' ELSE 'like NEITHER machine' END
FROM (SELECT id, o, n, m, CASE WHEN a < b COLLATE "sv_SE" THEN '<' WHEN a > b COLLATE "sv_SE" THEN '>' ELSE '=' END AS r
      FROM (VALUES
(1, '56010162', '77010161', '>', '<', 'both')
      ) AS v(id, ah, bh, o, n, m)
      CROSS JOIN LATERAL (SELECT convert_from(decode(ah, 'hex'), 'LATIN1') AS a, convert_from(decode(bh, 'hex'), 'LATIN1') AS b) AS s) AS t
ORDER BY id;
\else
\echo 'collation|sv_SE|CANNOT BE USED'
\endif
-- collation sv_SE.iso88591: sv_FI, sv_FI.iso88591, sv_SE, sv_SE.iso88591, swedish
\unset c4
\set ON_ERROR_STOP off
SELECT ('a' < 'b' COLLATE "sv_SE.iso88591") IS NOT NULL AS c4 \gset
\set ON_ERROR_STOP on
\if :{?c4}
SELECT 'collation|sv_SE.iso88591|usable|glibc ' || coalesce((SELECT pg_collation_actual_version(c.oid) FROM pg_collation c WHERE c.collname = 'sv_SE.iso88591' AND c.collencoding IN (-1, (SELECT encoding FROM pg_database WHERE datname = current_database())) AND pg_collation_is_visible(c.oid)), 'NULL');
SELECT 'test|' || id || '|sv_SE.iso88591|' || r || '|old ' || o || ', new ' || n || '|' || CASE m WHEN 'both' THEN 'measured on both machines' WHEN 'old' THEN 'measured on the old machine, the new one by the characters between' WHEN 'new' THEN 'measured on the new machine, the old one by the characters between' WHEN 'table' THEN 'from both machines'' compiled sort tables' END || '|' || CASE WHEN r = o THEN 'like the OLD machine' WHEN r = n THEN 'like the NEW machine' ELSE 'like NEITHER machine' END
FROM (SELECT id, o, n, m, CASE WHEN a < b COLLATE "sv_SE.iso88591" THEN '<' WHEN a > b COLLATE "sv_SE.iso88591" THEN '>' ELSE '=' END AS r
      FROM (VALUES
(1, '56010162', '77010161', '>', '<', 'both')
      ) AS v(id, ah, bh, o, n, m)
      CROSS JOIN LATERAL (SELECT convert_from(decode(ah, 'hex'), 'LATIN1') AS a, convert_from(decode(bh, 'hex'), 'LATIN1') AS b) AS s) AS t
ORDER BY id;
\else
\echo 'collation|sv_SE.iso88591|CANNOT BE USED'
\endif
-- collation swedish: sv_FI, sv_FI.iso88591, sv_SE, sv_SE.iso88591, swedish
\unset c5
\set ON_ERROR_STOP off
SELECT ('a' < 'b' COLLATE "swedish") IS NOT NULL AS c5 \gset
\set ON_ERROR_STOP on
\if :{?c5}
SELECT 'collation|swedish|usable|glibc ' || coalesce((SELECT pg_collation_actual_version(c.oid) FROM pg_collation c WHERE c.collname = 'swedish' AND c.collencoding IN (-1, (SELECT encoding FROM pg_database WHERE datname = current_database())) AND pg_collation_is_visible(c.oid)), 'NULL');
SELECT 'test|' || id || '|swedish|' || r || '|old ' || o || ', new ' || n || '|' || CASE m WHEN 'both' THEN 'measured on both machines' WHEN 'old' THEN 'measured on the old machine, the new one by the characters between' WHEN 'new' THEN 'measured on the new machine, the old one by the characters between' WHEN 'table' THEN 'from both machines'' compiled sort tables' END || '|' || CASE WHEN r = o THEN 'like the OLD machine' WHEN r = n THEN 'like the NEW machine' ELSE 'like NEITHER machine' END
FROM (SELECT id, o, n, m, CASE WHEN a < b COLLATE "swedish" THEN '<' WHEN a > b COLLATE "swedish" THEN '>' ELSE '=' END AS r
      FROM (VALUES
(1, '56010162', '77010161', '>', '<', 'both')
      ) AS v(id, ah, bh, o, n, m)
      CROSS JOIN LATERAL (SELECT convert_from(decode(ah, 'hex'), 'LATIN1') AS a, convert_from(decode(bh, 'hex'), 'LATIN1') AS b) AS s) AS t
ORDER BY id;
\else
\echo 'collation|swedish|CANNOT BE USED'
\endif
-- the databases, libc collations and lc_* settings that use a locale name of the old machine the new one lacks: a list, not a test
SELECT DISTINCT 'gone|' || u.kind || '|' || u.obj || '|' || u.value FROM (
  SELECT 'database', datname, datcollate FROM pg_database
  UNION ALL SELECT 'database', datname, datctype FROM pg_database
  UNION ALL SELECT 'collation', collname, collcollate FROM pg_collation WHERE collprovider = 'c'
  UNION ALL SELECT 'collation', collname, collctype FROM pg_collation WHERE collprovider = 'c'
  UNION ALL SELECT 'setting', name, setting FROM pg_settings WHERE name IN ('lc_messages', 'lc_monetary', 'lc_numeric', 'lc_time')
) AS u(kind, obj, value) JOIN (VALUES ('en_US.utf8@ampm'), ('en_US@ampm')) AS g(name)
  ON CASE WHEN u.value ~ '^[^@.]*[.]' THEN substring(u.value from '^[^@.]*') || '.' || (SELECT CASE WHEN c ~ '^[0-9]+$' THEN 'iso' || c ELSE c END FROM (SELECT lower(regexp_replace(substring(u.value from '^[^@.]*[.]([^@]*)'), '[^A-Za-z0-9]', '', 'g')) AS c) AS s) || coalesce(substring(u.value from '@.*$'), '') ELSE u.value END = CASE WHEN g.name ~ '^[^@.]*[.]' THEN substring(g.name from '^[^@.]*') || '.' || (SELECT CASE WHEN c ~ '^[0-9]+$' THEN 'iso' || c ELSE c END FROM (SELECT lower(regexp_replace(substring(g.name from '^[^@.]*[.]([^@]*)'), '[^A-Za-z0-9]', '', 'g')) AS c) AS s) || coalesce(substring(g.name from '@.*$'), '') ELSE g.name END
ORDER BY 1;
\echo 'end|3010dbdb18659761439d6f187c81211502f2f489c6fa63ff156648053b0596f9|5 tests'
