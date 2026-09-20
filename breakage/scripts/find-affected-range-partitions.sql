-- Range-partitioned tables a glibc collation change can break.  Run it in every
-- database.
--
-- A row sits in the partition the old rules routed it to, and pruning now looks
-- for it somewhere else, so a query through the partitioned table can miss rows
-- that are still on disk.  No REINDEX repairs this.  The rows have to be deleted
-- from the child and reinserted into the parent so routing places them again.
--
-- Only range partitioning is reported.  List and hash partitioning compare for
-- equality, which a deterministic collation answers byte by byte.

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
SELECT pc.partrelid::regclass::text AS relation,
       string_agg(DISTINCT a.collname, ', ') AS collations,
       pg_get_partkeydef(pc.partrelid) AS partition_key,
       (SELECT count(*) FROM pg_inherits WHERE inhparent = pc.partrelid) AS partitions
  FROM (SELECT p.partrelid, p.partcollation[g.n] AS colloid
          FROM pg_partitioned_table p, generate_subscripts(p.partcollation, 1) g(n)
         WHERE p.partstrat = 'r') pc
  JOIN affected a ON a.oid = pc.colloid
 GROUP BY 1, 3, 4
 ORDER BY 1;
