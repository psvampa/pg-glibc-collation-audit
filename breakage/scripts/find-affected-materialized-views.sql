-- Materialized views a glibc collation change can break.  Run it in every
-- database.
--
-- A materialized view holds its own copy of the rows, in the order the query
-- produced them under the old rules, with its own indexes.  Reindexing those
-- indexes leaves the stored rows as they are.  Run REFRESH MATERIALIZED VIEW.
--
-- This one over-reports.  Every materialized view that reads a column with a libc
-- collation is listed, including one that never orders, compares or groups text
-- and is therefore still correct.  Telling those apart would mean reading the
-- query, and a REFRESH costs little next to the risk of skipping one.

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
   WHERE (c.collprovider = 'c' AND c.collname NOT IN ('C','POSIX'))
      OR (c.collprovider = 'd' AND db.prov = 'c' AND db.coll NOT IN ('C','POSIX'))
)
SELECT r.ev_class::regclass::text AS relation,
       string_agg(DISTINCT a.collname, ', ') AS collations,
       string_agg(DISTINCT d.refobjid::regclass::text, ', ') AS reads_from,
       pg_size_pretty(pg_total_relation_size(r.ev_class)) AS size
  FROM pg_rewrite r
  JOIN pg_class cl ON cl.oid = r.ev_class AND cl.relkind = 'm'
  JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
                  AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
  JOIN pg_attribute src ON src.attrelid = d.refobjid AND src.attnum = d.refobjsubid
  JOIN affected a ON a.oid = src.attcollation
 GROUP BY 1, 4
 ORDER BY 1;
