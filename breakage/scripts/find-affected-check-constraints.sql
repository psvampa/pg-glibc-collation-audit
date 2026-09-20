-- CHECK constraints a glibc collation change can break.  Run it in every database.
--
-- A CHECK is evaluated when a row is written and never evaluated again, not on an
-- upgrade, not on a restart, not on a REINDEX.  Rows that stopped satisfying it
-- stay where they are and the catalogue still marks the constraint valid.  No
-- REINDEX repairs this.  Drop the constraint, add it back NOT VALID, and run
-- VALIDATE CONSTRAINT to make the server say which rows no longer satisfy it.
--
-- A constraint is reported when it reads a column carrying a libc collation and
-- its expression resolves at least one operator or function with a collation.
--
-- It reports more than it has to.  Equality and inequality over a deterministic
-- collation compare bytes, and a function such as length() never asks the locale
-- anything, yet CHECK (code <> 'x') and CHECK (length(w) < 50) are both listed.
-- Separating them would mean deciding which operators consult a collation, and
-- the first user-defined function that compares text inside would slip past that
-- list.  Revalidating a constraint that was fine costs a scan, missing one that
-- is broken costs more.  A CHECK on a domain is not reported at all, because it
-- depends on the type rather than on a column.

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
SELECT con.conrelid::regclass::text AS relation,
       con.conname AS constraint_name,
       string_agg(DISTINCT a.collname, ', ') AS collations,
       pg_get_constraintdef(con.oid) AS definition
  FROM pg_constraint con
  JOIN pg_depend d ON d.objid = con.oid AND d.classid = 'pg_constraint'::regclass
                  AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
  JOIN pg_attribute att ON att.attrelid = d.refobjid AND att.attnum = d.refobjsubid
  JOIN affected a ON a.oid = att.attcollation
 WHERE con.contype = 'c'
   -- The parsed expression carries the collation each operator and function was
   -- resolved with.  A constraint where every one of them is zero, such as
   -- CHECK (w IS NOT NULL), never consults a collation.
   AND con.conbin ~ 'inputcollid [1-9]'
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
