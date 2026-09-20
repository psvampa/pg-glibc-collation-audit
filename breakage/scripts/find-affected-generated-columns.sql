-- Stored generated columns a glibc collation change can break.  Run it in every
-- database.
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
-- It reports more than it has to, the same way the CHECK constraint script does.
-- An expression such as length(w) resolves with a collation and never asks it
-- anything, and it is listed.  Recomputing a column rewrites the table, so check
-- the expression before running it.

WITH db AS (
  -- pg_database.datlocprovider exists from PostgreSQL 15 on, so the row is read
  -- as json and a missing key comes back null.  On 14 a database collation is
  -- always libc, which is what the fallback says.
  SELECT coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') AS prov, d.datcollate AS coll
    FROM pg_database d WHERE d.datname = current_database()
),
affected AS (
  -- A libc collation other than C or POSIX is affected.  The default collation is
  -- affected only when the database itself is on libc and not on C or POSIX.
  -- pg_collation names that row 'default', so a filter on the name cannot see it.
  SELECT c.oid, c.collname FROM pg_collation c CROSS JOIN db
   -- collcollate, and not just the name, because a collation can sort by code
   -- point under another name.  ucs_basic is declared LC_COLLATE = 'C' with the
   -- libc provider up to PostgreSQL 16, and so is any collation someone creates
   -- that way.  Neither one moves when glibc does.
   WHERE (c.collprovider = 'c' AND c.collname NOT IN ('C','POSIX')
                               AND coalesce(c.collcollate, '') NOT IN ('C','POSIX'))
      OR (c.collprovider = 'd' AND db.prov = 'c' AND db.coll NOT IN ('C','POSIX'))
)
SELECT att.attrelid::regclass::text AS relation,
       att.attname AS column_name,
       string_agg(DISTINCT a.collname, ', ') AS collations,
       pg_get_expr(ad.adbin, ad.adrelid) AS expression
  FROM pg_attribute att
  JOIN pg_attrdef ad ON ad.adrelid = att.attrelid AND ad.adnum = att.attnum
  JOIN pg_depend d ON d.objid = ad.oid AND d.classid = 'pg_attrdef'::regclass
                  AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
  JOIN pg_attribute src ON src.attrelid = d.refobjid AND src.attnum = d.refobjsubid
  JOIN affected a ON a.oid = src.attcollation
 WHERE att.attgenerated = 's'
   -- The parsed expression carries the collation each operator and function was
   -- resolved with.  An expression where every one of them is zero never
   -- consults a collation.
   AND ad.adbin ~ 'inputcollid [1-9]'
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
