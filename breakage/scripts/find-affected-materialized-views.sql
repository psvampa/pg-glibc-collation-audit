-- Materialized views a glibc collation change can break.  Run it in every
-- database.
--
-- WARNING: this script decides from the collation of each column a
-- materialized view's query names, not from what the query does.  A collation
-- the query takes from anywhere else can go unseen: a COLLATE clause, or the
-- database's default collation on text taken from a column that has none, such
-- as doc->>'name' on a jsonb column.  So can text the query reads without
-- naming a column, from VALUES, from a function or from a whole row.  A
-- materialized view built any of these ways can be missing from this list, or
-- listed only under the database's LC_CTYPE.  Text search over such a column,
-- like to_tsvector() on a jsonb column, is seen only when the view keeps the
-- tsvector as a column.  Check those by hand.
--
-- A materialized view holds its own copy of the rows, in the order the query
-- produced them under the old rules, with its own indexes.  Reindexing those
-- indexes leaves the stored rows as they are.  Run REFRESH MATERIALIZED VIEW.
--
-- This one over-reports.  Every materialized view whose query names a column
-- with a collation that takes its sort order or its characters from glibc is
-- listed, including one that never orders, compares, groups or lowers text and
-- is therefore still correct.  When the database's LC_CTYPE is not C or POSIX,
-- whatever its provider, so is every view whose query names any text column,
-- even one under C, and every view with a tsvector column, because text search
-- reads that LC_CTYPE.  Telling those apart would mean reading the query, and a
-- REFRESH costs little next to the risk of skipping one.  A view listed only
-- for the database's LC_CTYPE shows database LC_CTYPE and the locale in the
-- collations column.

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
read_all AS (
  -- The columns each materialized view's query names, and where from.
  SELECT r.ev_class AS matview, d.refobjid AS source, src.attcollation AS colloid
    FROM pg_rewrite r
    JOIN pg_class cl ON cl.oid = r.ev_class AND cl.relkind = 'm'
    JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass
                    AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
    JOIN pg_attribute src ON src.attrelid = d.refobjid AND src.attnum = d.refobjsubid
),
reads AS (
  SELECT * FROM read_all WHERE colloid <> 0
),
hits AS (
  -- The query can order, compare or lower what it reads, so both halves of the
  -- collation count.
  SELECT r.matview, r.source, 1 AS rank, l.label AS label
    FROM reads r JOIN libc l ON l.oid = r.colloid
   WHERE l.sorts OR l.chars
  UNION
  -- The database's LC_CTYPE, which to_tsvector() reads whatever the collation
  -- of the column.
  SELECT r.matview, r.source, 2, 'database LC_CTYPE ' || db.ctype
    FROM reads r CROSS JOIN db
   WHERE db.ctype NOT IN ('C','POSIX')
  UNION
  -- A stored tsvector is text search, whatever it was computed from, even
  -- to_tsvector() over a jsonb column, which reads no collation at all.  It is
  -- found from the view's own columns, so a view over VALUES or over a whole
  -- row, which names no column, is not missed.  A domain over tsvector, an
  -- array of tsvector, or a domain over such an array counts too.  For a view
  -- listed only this way, reads_from lists every relation it depends on.
  SELECT cl.oid, d.refobjid, 2, 'database LC_CTYPE ' || db.ctype
    FROM pg_class cl
    JOIN pg_rewrite r ON r.ev_class = cl.oid
    LEFT JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
                         AND d.refclassid = 'pg_class'::regclass
                         AND d.refobjid <> cl.oid
    CROSS JOIN db
   WHERE cl.relkind = 'm' AND db.ctype NOT IN ('C','POSIX')
     AND EXISTS (SELECT 1 FROM pg_attribute a
                  WHERE a.attrelid = cl.oid AND a.attnum > 0
                    AND NOT a.attisdropped
                    AND a.atttypid IN (SELECT t.oid FROM pg_type t
                                         LEFT JOIN pg_type bt ON bt.oid = t.typbasetype
                                        WHERE 'tsvector'::regtype
                                              IN (t.oid, t.typelem, bt.oid, bt.typelem)))
),
best AS (
  -- An object found through a collation is not listed a second time under the
  -- database's LC_CTYPE.
  SELECT h.*, min(h.rank) OVER (PARTITION BY h.matview) AS best_rank
    FROM hits h
)
SELECT b.matview::regclass::text AS relation,
       string_agg(DISTINCT b.label, ', ') AS collations,
       string_agg(DISTINCT b.source::regclass::text, ', ') AS reads_from,
       pg_size_pretty(pg_total_relation_size(b.matview)) AS size
  FROM best b
 WHERE b.rank = b.best_rank
 GROUP BY b.matview
 ORDER BY 1;
