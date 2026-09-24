-- Partitioned tables a glibc collation change can break.  Run it in every
-- database.
--
-- WARNING: this script decides from the collation of each key and of each
-- column a key expression reads, not from what the expression does.  Text
-- taken from a column that has no collation, such as doc->>'name' on a jsonb
-- column, is compared under the database's default collation, and a key that
-- compares or lowers it and returns something other than text can be missing
-- from this list, and so can a key expression that runs text search over such
-- a column, like to_tsvector() on a jsonb column.  Check those by hand.
--
-- A row sits in the partition the old rules routed it to, and pruning now looks
-- for it somewhere else, so a query through the partitioned table can miss rows
-- that are still on disk.  No REINDEX repairs this.  The rows have to be deleted
-- from the child and reinserted into the parent so routing places them again.
--
-- A plain column in the key is reported only under range partitioning, and only
-- when its sort order comes from glibc.  List and hash partitioning compare it
-- for equality, which a deterministic collation answers byte by byte.  Two
-- kinds of key are exceptions.  A key under an operator class from an
-- extension, such as citext, is reported under any strategy when its collation
-- takes its characters from glibc, because such an operator class can read them
-- before it compares or hashes.  A jsonb key under range partitioning is
-- reported when the database's default collation sorts by glibc, because jsonb
-- orders the strings inside it by that collation.  A key expression is reported
-- under any strategy when a collation it is computed with takes its sort order
-- or its characters from glibc, because if lower() or a comparison inside it
-- answers differently, the row belongs to another partition.  When the
-- database's LC_CTYPE is not C or POSIX, whatever its provider, so is every key
-- expression that reads a text column, because text search reads that LC_CTYPE
-- whatever the collation, and every text key under an extension's operator
-- class, because citext lowers by the database's default collation whatever the
-- key's own.  A table listed only for that reason shows database LC_CTYPE and
-- the locale in the collations column.

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
keys AS (
  -- One row per column of each partition key.  partattrs holds 0 where the
  -- column is an expression.
  SELECT p.partrelid, p.partstrat, p.partattrs[g.n] AS attnum,
         p.partcollation[g.n] AS colloid, p.partclass[g.n] AS opclass
    FROM pg_partitioned_table p, generate_subscripts(p.partcollation, 1) g(n)
),
exprs AS (
  -- The collations an expression in a key is computed with.  PostgreSQL records
  -- the table columns an expression reads the way it records a plain key
  -- column, as internally dependent on the table, so the plain columns of a
  -- mixed key come along too.  An explicit COLLATE inside the expression is a
  -- dependency of the table on that collation.  And the expression's own
  -- collation is in partcollation.
  SELECT p.partrelid, a.attcollation AS colloid
    FROM pg_partitioned_table p
    JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = p.partrelid
                    AND d.objsubid > 0 AND d.refclassid = 'pg_class'::regclass
                    AND d.refobjid = p.partrelid AND d.refobjsubid = 0
                    AND d.deptype = 'i'
    JOIN pg_attribute a ON a.attrelid = p.partrelid AND a.attnum = d.objsubid
   WHERE p.partexprs IS NOT NULL AND a.attcollation <> 0
  UNION
  SELECT p.partrelid, d.refobjid
    FROM pg_partitioned_table p
    JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = p.partrelid
                    AND d.objsubid = 0 AND d.refclassid = 'pg_collation'::regclass
   WHERE p.partexprs IS NOT NULL
  UNION
  SELECT k.partrelid, k.colloid FROM keys k WHERE k.attnum = 0 AND k.colloid <> 0
),
hits AS (
  -- 1. A plain column in a range key.  Routing compares it under the key's
  --    collation, so the order counts and the characters do not.  List and
  --    hash partitioning compare a plain column for equality, which a
  --    deterministic collation answers byte by byte.
  SELECT k.partrelid, 1 AS rank, l.label AS label
    FROM keys k JOIN libc l ON l.oid = k.colloid
   WHERE k.partstrat = 'r' AND k.attnum <> 0 AND l.sorts
  UNION
  -- 1b. A key under an operator class that did not come with PostgreSQL, under
  --     any strategy, when its collation takes its characters from glibc.
  --     Such an operator class can read the characters before it compares,
  --     hashes or orders, so list and hash partitioning can depend on them.
  SELECT k.partrelid, 1, l.label
    FROM keys k JOIN libc l ON l.oid = k.colloid
   WHERE k.opclass >= 16384 AND l.chars
  UNION
  -- 1c. A jsonb key under range partitioning.  jsonb carries no collation of
  --     its own and orders the strings inside it by the database's default
  --     collation.  List and hash partitioning compare it for equality, which
  --     that collation answers byte by byte.
  SELECT k.partrelid, 1, l.label
    FROM keys k
    JOIN pg_opclass oc ON oc.oid = k.opclass
    JOIN libc l ON l.is_default
   WHERE k.partstrat = 'r' AND oc.opcintype = 'jsonb'::regtype AND l.sorts
  UNION
  -- 2. An expression in the key, under any strategy.  Its value is computed
  --    each time a row is routed and stored nowhere, so when lower() or a
  --    comparison inside it answers differently, the row belongs elsewhere.
  SELECT e.partrelid, 1, l.label
    FROM exprs e JOIN libc l ON l.oid = e.colloid
   WHERE l.sorts OR l.chars
  UNION
  -- 3. The database's LC_CTYPE, for an expression that reads text, since
  --    to_tsvector() reads it whatever the collation.
  SELECT e.partrelid, 2, 'database LC_CTYPE ' || db.ctype
    FROM exprs e CROSS JOIN db
   WHERE db.ctype NOT IN ('C','POSIX')
  UNION
  -- 3b. The database's LC_CTYPE, for a key under an extension's operator class,
  --     since citext lowers by the default collation whatever the key's own.
  SELECT k.partrelid, 2, 'database LC_CTYPE ' || db.ctype
    FROM keys k CROSS JOIN db
   WHERE k.opclass >= 16384 AND k.colloid <> 0
     AND db.ctype NOT IN ('C','POSIX')
),
best AS (
  -- An object found through a collation is not listed a second time under the
  -- database's LC_CTYPE.
  SELECT h.*, min(h.rank) OVER (PARTITION BY h.partrelid) AS best_rank
    FROM hits h
)
SELECT b.partrelid::regclass::text AS relation,
       string_agg(DISTINCT b.label, ', ') AS collations,
       pg_get_partkeydef(b.partrelid) AS partition_key,
       (SELECT count(*) FROM pg_inherits WHERE inhparent = b.partrelid) AS partitions
  FROM best b
 WHERE b.rank = b.best_rank
 GROUP BY b.partrelid
 ORDER BY 1;
