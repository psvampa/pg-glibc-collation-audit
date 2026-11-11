-- Template: confirm in real PostgreSQL whether specific locales actually sort
-- differently, once the source-diff audit (scripts/) has told you which
-- locale identifiers to check on this OS/glibc version.
--
-- Run this SAME script on both nodes (old glibc / new glibc), as
-- docs/confirming-on-a-real-system.md shows, and diff the two outputs. It
-- stops at its first error, and only a run that finished ends in a line that
-- starts `=== done`.
--
-- Replace <LOCALE>/<value*> below with the locale(s) to run it for
-- (docs/confirming-on-a-real-system.md, "Which locales to run it for") and
-- with strings known (the characters step 2 or step 11 names) to hit the
-- specific rule that changed.
--
-- Notes before you run it:
--   * It DROPS AND RECREATES a table named collation_test in the current
--     schema. Point it at a scratch database.
--   * Run as a superuser, it first calls pg_import_system_collations(),
--     which WRITES TO pg_catalog. It is idempotent and only adds collations
--     already present in `locale -a`. Run as any other role, it skips that
--     call and uses the collations already there.
--   * RESTART PostgreSQL after installing glibc-all-langpacks, BEFORE running
--     this. The function takes its list from `locale -a` (a fresh subprocess,
--     which sees the new locales) but checks each one inside the backend, and
--     glibc opens the locale archive at most once per process. Every backend
--     inherits the postmaster's attempt, so without a restart the function
--     returns success while the locales that package added stay missing. A
--     single glibc-langpack-<xx> installs its locales outside the archive and
--     needs no restart.
--   * collversion and actual, in the collation listing below, are empty for
--     a collation whose locale is C, C.* or POSIX, which PostgreSQL gives no
--     version.
--   * Use the names docs/confirming-on-a-real-system.md says to run it for
--     ("Which locales to run it for"). Step 2's list holds locales,
--     not spellings (sv_SE, not sv_SE.utf8 or sv_SE.iso885915),
--     plus the aliases glibc's locale.alias gives them (swedish). The
--     collation listing below reads each collation's locale from
--     collcollate, so every spelling and every encoding of one locale counts
--     as that locale. Check
--     `locale -a` first, and in CREATE TABLE's COLLATE below use a name that
--     exists for this database's encoding. In a UTF8 database "sv_SE" is
--     sv_SE.utf8.
--   * ONE LOCALE INVERTS THE USUAL RULE: C.UTF-8. Agreement with LC_ALL=C is
--     the CORRECT answer there -- it is what upstream's codepoint_collation
--     guarantees from 2.35 on -- so reading
--     "it agrees with C, therefore it was not applied" is exactly
--     backwards for that one locale. It has its own script for that reason:
--     sql/c_utf8_probe.sql, and this one stops on it.
\set ON_ERROR_STOP on

-- Only a superuser may import. Any other role runs on the collations already
-- in pg_catalog, and a locale missing there stops the run at CREATE TABLE.
SELECT rolsuper AS is_super FROM pg_roles WHERE rolname = current_user \gset
\if :is_super
SELECT pg_import_system_collations('pg_catalog');
\endif

-- Lists every collation built from a locale on the audit's list, in every
-- encoding, with the glibc version recorded when it was created or last
-- refreshed (collversion) and the one this server runs (actual). They differ
-- where glibc changed after that, as on an OS upgraded in place or a physical
-- replica. Equal versions prove nothing, because most ways of creating or
-- refreshing a collation under the new glibc record the new version. Both are
-- glibc's release number only, so a distro patch that keeps the number
-- changes neither. Put the names you are confirming in place of <LOCALE>. The
-- query starts from the list, so a name that matches no collation still
-- prints a row with nothing beside it. That happens for a locale this
-- database does not have, for a name left unreplaced, and for a locale whose
-- collations were never imported into pg_collation (the note on
-- pg_import_system_collations, above). A collation's locale, by glibc's own
-- rule, is its collcollate without the part from the dot up to any @
-- (localedata/Makefile builds every locale from that file). sv_SE.UTF-8,
-- sv_SE.utf8 and sv_SE.iso885915 are all sv_SE. Both sides go through the
-- same rule, so a spelling put on the list (sv_SE.utf8) matches too. The dot
-- is written [.] and not with a backslash, which a server with
-- standard_conforming_strings off reads as an escape. '\.' would then match
-- any first character and empty every name. glibc reads an alias in any case
-- (Swedish is swedish), so the comparison ignores case. lower() runs under
-- the C collation (catalog columns are C, and the list side says so), which
-- changes ASCII letters only. Under a Turkish locale's own rules lower('I')
-- is a dotless i, and before this query used C, or_IN, sv_FI and sv_FI@euro
-- matched nothing in a Turkish database (measured, PostgreSQL 18.6). With C
-- they all match. The rows are ordered under C too, so two databases with
-- different default collations print them in the same order, and by encoding,
-- which tells apart the collations that share one name.
SELECT n AS listed, c.collname, c.collcollate,
       pg_encoding_to_char(c.collencoding) AS encoding, c.collversion,
       pg_collation_actual_version(c.oid) AS actual
FROM unnest(ARRAY['<LOCALE>']) AS n
LEFT JOIN pg_collation c
  ON c.collprovider = 'c'
 AND lower(regexp_replace(c.collcollate, '[.][^@]*', ''))
   = lower(regexp_replace(n COLLATE "C", '[.][^@]*', ''))
ORDER BY n COLLATE "C", c.collname, encoding;
  -- e.g. ARRAY['sv_SE','sv_FI','sv_FI@euro','or_IN','swedish']

-- The NOTICE a missing table gives would print on a first run and not on the
-- next, and two nodes run a different number of times would differ by it.
SET client_min_messages = warning;
DROP TABLE IF EXISTS collation_test;
RESET client_min_messages;
CREATE TABLE collation_test (w text COLLATE "<LOCALE>");
INSERT INTO collation_test VALUES ('<value1>'), ('<value2>'), ('<value3>');
CREATE INDEX ON collation_test (w);

-- A run that sorted, but not through glibc's locale data, or not the strings
-- chosen, would diff as alike on both nodes. PostgreSQL compares C and POSIX
-- itself, and step 2's full list writes C.UTF-8 as C. C.UTF-8 reads the
-- opposite way and has its own script. The patterns hold no backslash, so
-- standard_conforming_strings does not change them.
DO $$
DECLARE prov "char"; loc text; cname text; left_over bigint;
BEGIN
  SELECT c.collprovider, c.collcollate, c.collname INTO prov, loc, cname
  FROM pg_attribute a JOIN pg_collation c ON c.oid = a.attcollation
  WHERE a.attrelid = 'collation_test'::regclass AND a.attname = 'w';
  IF prov IS DISTINCT FROM 'c' THEN
    RAISE EXCEPTION
      'collation % has provider %, not libc; this template confirms glibc''s '
      'order only.', cname, prov;
  END IF;
  IF loc IN ('C', 'POSIX') THEN
    RAISE EXCEPTION
      'collation % is locale %, which PostgreSQL compares without glibc. '
      'Step 2''s list writes C.UTF-8 as C; run sql/c_utf8_probe.sql for it.',
      cname, loc;
  END IF;
  IF loc ~* '^c[.]' THEN
    RAISE EXCEPTION
      'collation % is locale %, which reads the opposite way; run '
      'sql/c_utf8_probe.sql for it.', cname, loc;
  END IF;
  SELECT count(*) INTO left_over FROM collation_test
  WHERE w ~ '^<value[0-9]+>$';
  IF left_over > 0 THEN
    RAISE EXCEPTION
      '% value(s) are still the <valueN> placeholders; put the strings to '
      'compare in their place.', left_over;
  END IF;
END $$;

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
-- It orders by the column itself, so under the collation the check above read.
\echo '--- ORDER BY under <LOCALE> ---'
SELECT ROW_NUMBER() OVER (ORDER BY w, w COLLATE "C") AS pos,
       w
FROM collation_test
ORDER BY pos;
\echo '=== done. diff this output against the other node. ==='
