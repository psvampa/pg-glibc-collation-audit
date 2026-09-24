-- CHECK constraints a glibc collation change can break.  Run it in every database.
--
-- WARNING: this script decides from the collation of each column a constraint
-- reads, not from its expression.  A collation the expression takes from
-- anywhere else can go unseen: a COLLATE clause, or the database's default
-- collation on text taken from a column that has none, such as doc->>'name' on
-- a jsonb column.  A constraint built either way can be missing from this
-- list, or listed only under the database's LC_CTYPE.  Text search over such a
-- column, like to_tsvector() on a jsonb column, is not seen either.  Check
-- those by hand.
--
-- A CHECK is evaluated when a row is written and never evaluated again, not on an
-- upgrade, not on a restart, not on a REINDEX.  Rows that stopped satisfying it
-- stay where they are and the catalogue still marks the constraint valid.  No
-- REINDEX repairs this.  Drop the constraint, add it back NOT VALID, and run
-- VALIDATE CONSTRAINT to make the server say which rows no longer satisfy it.
--
-- A constraint is reported when its expression resolves at least one operator
-- or function with a collation and it reads a column whose collation takes its
-- sort order or its characters from glibc.  When the database's LC_CTYPE is not
-- C or POSIX, whatever its provider, any text column counts, even one under C,
-- because to_tsvector() reads that LC_CTYPE whatever the column's collation.  A
-- constraint listed only for that reason shows database LC_CTYPE and the locale
-- in the collations column.
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
reads AS (
  -- The collatable columns each CHECK reads, when its expression resolves at
  -- least one operator or function with a collation.  The parsed expression
  -- carries the collation each of them was resolved with, and a constraint
  -- where every one is zero, such as CHECK (w IS NOT NULL), never consults one.
  SELECT con.oid AS conoid, att.attcollation AS colloid
    FROM pg_constraint con
    JOIN pg_depend d ON d.objid = con.oid AND d.classid = 'pg_constraint'::regclass
                    AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
    JOIN pg_attribute att ON att.attrelid = d.refobjid AND att.attnum = d.refobjsubid
   WHERE con.contype = 'c'
     AND att.attcollation <> 0
     AND con.conbin ~ 'inputcollid [1-9]'
),
hits AS (
  -- A CHECK computes its expression, so both halves of the collation count.
  SELECT r.conoid, 1 AS rank, l.label AS label
    FROM reads r JOIN libc l ON l.oid = r.colloid
   WHERE l.sorts OR l.chars
  UNION
  -- The database's LC_CTYPE, which to_tsvector() reads whatever the collation
  -- of the column.  Telling it from lower() would mean reading the expression.
  SELECT r.conoid, 2, 'database LC_CTYPE ' || db.ctype
    FROM reads r CROSS JOIN db
   WHERE db.ctype NOT IN ('C','POSIX')
),
best AS (
  -- An object found through a collation is not listed a second time under the
  -- database's LC_CTYPE.
  SELECT h.*, min(h.rank) OVER (PARTITION BY h.conoid) AS best_rank
    FROM hits h
)
SELECT con.conrelid::regclass::text AS relation,
       con.conname AS constraint_name,
       string_agg(DISTINCT b.label, ', ') AS collations,
       pg_get_constraintdef(con.oid) AS definition
  FROM best b
  JOIN pg_constraint con ON con.oid = b.conoid
 WHERE b.rank = b.best_rank
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
