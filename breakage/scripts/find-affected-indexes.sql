-- Indexes a glibc collation change can break.  Run it in every database.
--
-- It differs from the query on the PostgreSQL wiki in two ways.  It reaches the
-- collations that reach an index through its predicate or through the inputs of
-- an expression, and it resolves the default collation against the database
-- instead of against a name.

WITH db AS (
  SELECT datlocprovider AS prov, datcollate AS coll
    FROM pg_database WHERE datname = current_database()
),
refs AS (
  -- 1. The collations of the key columns.  This is what pg_index.indcollation
  --    holds, and it is all the wiki query looks at.
  SELECT i.indexrelid, i.indcollation[g.n] AS colloid
    FROM pg_index i, generate_subscripts(i.indcollation, 1) g(n)
   WHERE i.indcollation[g.n] <> 0
  UNION
  -- 2. The collation of every column the index references, whatever role that
  --    column plays.  indcollation covers key columns only, so an index written
  --    as (id) WHERE w < 'vz' records a zero there and is missed without this.
  SELECT d.objid, a.attcollation
    FROM pg_depend d
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
  CROSS JOIN db
 -- A libc collation other than C or POSIX is affected.  The default collation is
 -- affected only when the database itself is on libc and not on C or POSIX.
 -- pg_collation names that row 'default', so a filter on the name cannot see it.
 WHERE (c.collprovider = 'c' AND c.collname NOT IN ('C','POSIX'))
    OR (c.collprovider = 'd' AND db.prov = 'c' AND db.coll NOT IN ('C','POSIX'))
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
