-- Stored generated columns a glibc collation change can break.  Run it in every
-- database.
--
-- WARNING: this script decides from the collation of each column a generated
-- column reads, not from its expression.  A collation the expression takes
-- from anywhere else can go unseen: a COLLATE clause, or the database's
-- default collation on text taken from a column that has none, such as
-- doc->>'name' on a jsonb column.  A generated column built either way can be
-- missing from this list, or listed only under the database's LC_CTYPE.  Text
-- search over such a column, like to_tsvector() on a jsonb column, is seen only
-- when the column stores the tsvector itself.  Check those by hand.
--
-- PostgreSQL requires the expression of a generated column to be immutable, and
-- the catalogue marks text comparison immutable.  glibc is what turns that into a
-- lie.  The value was computed once, written to disk, and the same expression now
-- returns something else.  No REINDEX repairs this.  Recompute the column with
-- ALTER TABLE ... ALTER COLUMN ... SET EXPRESSION AS (...), which rewrites the
-- table.
--
-- Only stored columns are reported.  A virtual generated column is computed when
-- it is read, so it always answers with the current rules.
--
-- A column is reported when its expression resolves at least one operator or
-- function with a collation and reads a column whose collation takes its sort
-- order or its characters from glibc.  When the database's LC_CTYPE is not C or
-- POSIX, whatever its provider, any text column counts, even one under C, and
-- every stored tsvector is listed whatever it was computed from, because text
-- search reads that LC_CTYPE.  A column listed only for that reason shows
-- database LC_CTYPE and the locale in the collations column.
--
-- It reports more than it has to, the same way the CHECK constraint script does.
-- An expression such as length(w) resolves with a collation and never asks it
-- anything, and it is listed.  Recomputing a column rewrites the table, so check
-- the expression before running it.

WITH db AS (
  -- pg_database.datlocprovider exists from PostgreSQL 15 on, so the row is read
  -- as json and a missing key comes back null.  On 14 a database collation is
  -- always libc, which is what the fallback says.
  SELECT coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') AS prov,
         d.datcollate AS coll, d.datctype AS ctype
    FROM pg_database d WHERE d.datname = current_database()
),
libc AS (
  -- The collations libc provides, and which half of each one's rules comes from
  -- glibc.  sorts is LC_COLLATE, the order.  chars is LC_CTYPE, what counts as
  -- a letter and what lower() and upper() return.  A collation can take the two
  -- from different locales, and only C and POSIX leave glibc out of it; C.UTF-8
  -- moves like any other.  ucs_basic is declared C in both halves with the libc
  -- provider up to PostgreSQL 16, and so is any collation someone creates that
  -- way, so neither moves.  The halves decide, not the name.  A collation
  -- called "C" in another schema can take its LC_CTYPE from en_US.
  --
  -- The default collation is glibc's only when the database itself is on libc.
  -- pg_collation names that row 'default', so a filter on the name cannot see
  -- it, and its halves are the database's.
  --
  -- label is the name the output prints, qualified by its schema when the
  -- search path would not find it, so a "C" from another schema does not pass
  -- for the real one.
  SELECT c.oid, c.collprovider = 'd' AS is_default,
         CASE WHEN pg_collation_is_visible(c.oid) THEN c.collname::text
              ELSE coalesce(n.nspname::text, '?') || '.' || c.collname END AS label,
         coalesce(CASE WHEN c.collprovider = 'd' THEN db.coll ELSE c.collcollate END, '')
           NOT IN ('C','POSIX') AS sorts,
         coalesce(CASE WHEN c.collprovider = 'd' THEN db.ctype ELSE c.collctype END, '')
           NOT IN ('C','POSIX') AS chars
    FROM pg_collation c
    LEFT JOIN pg_namespace n ON n.oid = c.collnamespace
    CROSS JOIN db
   WHERE c.collprovider = 'c' OR (c.collprovider = 'd' AND db.prov = 'c')
),
deps AS (
  -- The columns a generated column is computed from.  PostgreSQL 15 and later
  -- record them as dependencies of its expression in pg_attrdef.  12 to 14
  -- recorded them on the generated column itself (postgres commit cb02fcb4c95,
  -- never back-patched), and reading only the first shape there finds nothing
  -- but the column's own collation.
  SELECT ad.adrelid AS relid, ad.adnum AS attnum, d.refobjid, d.refobjsubid
    FROM pg_attrdef ad
    JOIN pg_depend d ON d.classid = 'pg_attrdef'::regclass AND d.objid = ad.oid
   WHERE d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
  UNION
  SELECT d.objid, d.objsubid, d.refobjid, d.refobjsubid
    FROM pg_depend d
   WHERE d.classid = 'pg_class'::regclass AND d.objsubid > 0
     AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
),
reads AS (
  -- The collatable columns each stored generated column reads, when its
  -- expression resolves at least one operator or function with a collation.
  -- An expression where every one of them is zero never consults a collation.
  SELECT att.attrelid, att.attnum, src.attcollation AS colloid
    FROM pg_attribute att
    JOIN pg_attrdef ad ON ad.adrelid = att.attrelid AND ad.adnum = att.attnum
    JOIN deps d ON d.relid = att.attrelid AND d.attnum = att.attnum
    JOIN pg_attribute src ON src.attrelid = d.refobjid AND src.attnum = d.refobjsubid
   WHERE att.attgenerated = 's'
     AND src.attcollation <> 0
     AND ad.adbin ~ 'inputcollid [1-9]'
),
hits AS (
  -- The expression is computed, so both halves of the collation count.
  SELECT r.attrelid, r.attnum, 1 AS rank, l.label AS label
    FROM reads r JOIN libc l ON l.oid = r.colloid
   WHERE l.sorts OR l.chars
  UNION
  -- The database's LC_CTYPE, which to_tsvector() reads whatever the collation
  -- of the column.  Telling it from lower() would mean reading the expression.
  SELECT r.attrelid, r.attnum, 2, 'database LC_CTYPE ' || db.ctype
    FROM reads r CROSS JOIN db
   WHERE db.ctype NOT IN ('C','POSIX')
  UNION
  -- A stored tsvector is text search, whatever it was computed from, even
  -- to_tsvector() over a jsonb column, which reads no collation at all.  A
  -- domain over tsvector, an array of tsvector, or a domain over such an array
  -- counts too.
  SELECT att.attrelid, att.attnum, 2, 'database LC_CTYPE ' || db.ctype
    FROM pg_attribute att CROSS JOIN db
   WHERE att.attgenerated = 's'
     AND att.atttypid IN (SELECT t.oid FROM pg_type t
                            LEFT JOIN pg_type bt ON bt.oid = t.typbasetype
                           WHERE 'tsvector'::regtype
                                 IN (t.oid, t.typelem, bt.oid, bt.typelem))
     AND db.ctype NOT IN ('C','POSIX')
),
best AS (
  -- An object found through a collation is not listed a second time under the
  -- database's LC_CTYPE.
  SELECT h.*, min(h.rank) OVER (PARTITION BY h.attrelid, h.attnum) AS best_rank
    FROM hits h
)
SELECT att.attrelid::regclass::text AS relation,
       att.attname AS column_name,
       string_agg(DISTINCT b.label, ', ') AS collations,
       pg_get_expr(ad.adbin, ad.adrelid) AS expression
  FROM best b
  JOIN pg_attribute att ON att.attrelid = b.attrelid AND att.attnum = b.attnum
  JOIN pg_attrdef ad ON ad.adrelid = b.attrelid AND ad.adnum = b.attnum
 WHERE b.rank = b.best_rank
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
