-- Indexes a glibc collation change can break.  Run it in every database.
--
-- It differs from the query on the PostgreSQL wiki in two ways.  It reaches the
-- collations that reach an index through its predicate or through the inputs of
-- an expression, and it resolves the default collation against the database
-- instead of against a name.

WITH db AS (
  -- pg_database.datlocprovider exists from PostgreSQL 15 on, so the row is read
  -- as json and a missing key comes back null.  On 14 a database collation is
  -- always libc, which is what the fallback says.
  SELECT coalesce(to_jsonb(d) ->> 'datlocprovider', 'c') AS prov,
         d.datcollate AS coll
    FROM pg_database d WHERE d.datname = current_database()
),
refs AS (
  -- 1. The collations of the key columns.  This is what pg_index.indcollation
  --    holds, and it is all the wiki query looks at.
  SELECT i.indexrelid, i.indcollation[g.n] AS colloid
    FROM pg_index i, generate_subscripts(i.indcollation, 1) g(n)
   WHERE i.indcollation[g.n] <> 0
  UNION
  -- 2. The collation of the columns an index with a predicate or an expression
  --    reads.  indcollation covers key columns only, so an index written as
  --    (id) WHERE w < 'vz' records a zero there and is missed without this.
  --    Restricting it to those indexes keeps a plain index on (w COLLATE "C")
  --    out, which branch 1 already answered, and an INCLUDEd column too, since
  --    an included column is stored and never ordered.
  SELECT d.objid, a.attcollation
    FROM pg_depend d
    JOIN pg_index i2 ON i2.indexrelid = d.objid
                    AND (i2.indpred IS NOT NULL OR i2.indexprs IS NOT NULL)
    JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
   WHERE d.classid = 'pg_class'::regclass
     AND d.refclassid = 'pg_class'::regclass
     AND d.refobjsubid > 0 AND a.attcollation <> 0
  UNION
  -- 3. Collations the index depends on directly, which is an explicit COLLATE.
  SELECT d.objid, d.refobjid
    FROM pg_depend d
   WHERE d.classid = 'pg_class'::regclass
     AND d.refclassid = 'pg_collation'::regclass
)
SELECT i.indrelid::regclass::text   AS table_name,
       r.indexrelid::regclass::text AS index_name,
       string_agg(DISTINCT c.collname, ', ') AS collations,
       pg_get_indexdef(r.indexrelid) AS definition
  FROM refs r
  JOIN pg_index i     ON i.indexrelid = r.indexrelid
  JOIN pg_collation c ON c.oid = r.colloid
  -- A hash index over a deterministic collation hashes the bytes, so a change
  -- in the sort rules leaves it alone.
  JOIN pg_class ic ON ic.oid = r.indexrelid
                  AND ic.relam <> (SELECT oid FROM pg_am WHERE amname = 'hash')
  CROSS JOIN db
 -- A libc collation other than C or POSIX is affected.  The default collation is
 -- affected only when the database itself is on libc and not on C or POSIX.
 -- pg_collation names that row 'default', so a filter on the name cannot see it.
  -- collcollate, and not just the name, because a collation can sort by code
  -- point under another name.  ucs_basic is declared LC_COLLATE = 'C' with the
  -- libc provider up to PostgreSQL 16, and so is any collation someone creates
  -- that way.  Neither one moves when glibc does.
  WHERE (c.collprovider = 'c' AND c.collname NOT IN ('C','POSIX')
                              AND coalesce(c.collcollate, '') NOT IN ('C','POSIX'))
    OR (c.collprovider = 'd' AND db.prov = 'c' AND db.coll NOT IN ('C','POSIX'))
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
