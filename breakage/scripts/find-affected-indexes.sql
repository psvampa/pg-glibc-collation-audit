-- Indexes a glibc collation change can break.  Run it in every database.
--
-- WARNING: this script decides from the collation of each column an index
-- reads and of each key, not from what its expressions do.  Text taken from a
-- column that has no collation, such as doc->>'name' on a jsonb column, is
-- compared under the database's default collation, and an index whose
-- predicate compares or lowers it, or whose key does and returns something
-- other than text, can be missing from this list.  Text search over such a
-- column, like to_tsvector() on a jsonb column, is seen only when the tsvector
-- is the key itself.  Check those by hand.
--
-- Unlike the query on the PostgreSQL wiki, it reaches the collations that reach
-- an index through its predicate or through the inputs of an expression, it
-- resolves the default collation against the database instead of against a
-- name, and it counts LC_CTYPE as well as LC_COLLATE.  An index on lower(w) is
-- therefore listed when w sorts as C but takes its characters from en_US.  A
-- btree index on a jsonb key is listed when the database's default collation
-- sorts by glibc, because jsonb orders the strings inside it by that collation.
-- When the database's LC_CTYPE is not C or POSIX, whatever its provider, it
-- also lists every index that computes something from a text column or uses an
-- extension's operator class on one, because text search and pg_trgm read that
-- LC_CTYPE whatever the column's collation.  An index listed only for that
-- reason shows database LC_CTYPE and the locale in the collations column.

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
),
idx AS (
  -- What each index does with its text.  One with a predicate or an expression
  -- computes something from it, so the characters count as well as the order.
  -- An operator class that did not come with PostgreSQL, on a key that has a
  -- collation, can read the locale in any way it likes, whatever that
  -- collation is.  pg_trgm finds words by the database's LC_CTYPE, and citext
  -- lowers text by the database's default collation.  A key computed as a
  -- tsvector is text search, whatever it was computed from, even to_tsvector()
  -- over a jsonb column, which reads no collation at all.  A missing access
  -- method, which DROP ACCESS METHOD cannot leave without CASCADE, reads as
  -- neither hash nor btree.
  SELECT i.indexrelid,
         (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL) AS computes,
         coalesce(am.amname = 'hash', false) AS hash,
         EXISTS (SELECT 1 FROM generate_subscripts(i.indclass, 1) g(n)
                  WHERE i.indclass[g.n] >= 16384
                    AND i.indcollation[g.n] <> 0) AS extension_opclass,
         EXISTS (SELECT 1 FROM generate_subscripts(i.indclass, 1) g(n)
                   JOIN pg_opclass oc ON oc.oid = i.indclass[g.n]
                  WHERE i.indkey[g.n] = 0
                    AND oc.opcintype = 'tsvector'::regtype) AS computed_tsvector,
         coalesce(am.amname = 'btree', false)
           AND EXISTS (SELECT 1 FROM generate_subscripts(i.indclass, 1) g(n)
                         JOIN pg_opclass oc ON oc.oid = i.indclass[g.n]
                        WHERE oc.opcintype = 'jsonb'::regtype) AS jsonb_btree
    FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    LEFT JOIN pg_am am ON am.oid = ic.relam
),
hits AS (
  -- 1. The order.  A hash index over a deterministic collation hashes the
  --    bytes, so a change in the sort rules leaves a plain one alone.  A hash
  --    index with a predicate or an expression is decided by what it computes,
  --    like any other.
  SELECT r.indexrelid, 1 AS rank, l.label AS label
    FROM refs r
    JOIN idx x  ON x.indexrelid = r.indexrelid
    JOIN libc l ON l.oid = r.colloid
   WHERE l.sorts AND (NOT x.hash OR x.computes)
  UNION
  -- 2. The characters, where the index computes something from them or an
  --    extension's operator class reads them.  A plain key under a built-in
  --    operator class and a collation that sorts by code point is compared
  --    byte by byte and never lowered, whatever its LC_CTYPE says.
  SELECT r.indexrelid, 1, l.label
    FROM refs r
    JOIN idx x  ON x.indexrelid = r.indexrelid
    JOIN libc l ON l.oid = r.colloid
   WHERE l.chars AND (x.computes OR x.extension_opclass)
  UNION
  -- 3. The database's LC_CTYPE.  Text search and pg_trgm read it whatever the
  --    collation of the text, and whatever the database's provider, because
  --    PostgreSQL sets it from datctype in an ICU or builtin database too.
  --    Telling a call to to_tsvector() from one to lower() would mean reading
  --    the expression, so every index that computes something from a text
  --    column is listed.
  SELECT x.indexrelid, 2, 'database LC_CTYPE ' || db.ctype
    FROM idx x CROSS JOIN db
   WHERE db.ctype NOT IN ('C','POSIX')
     AND (x.extension_opclass OR x.computed_tsvector
          OR (x.computes AND EXISTS (SELECT 1 FROM refs r
                                      WHERE r.indexrelid = x.indexrelid)))
  UNION
  -- 4. A jsonb key in a btree index.  jsonb carries no collation of its own and
  --    orders the strings inside it by the database's default collation, so
  --    that collation's sort order decides.  GIN compares them under C and a
  --    hash index hashes their bytes, so neither is listed for it.
  SELECT x.indexrelid, 1, l.label
    FROM idx x JOIN libc l ON l.is_default
   WHERE x.jsonb_btree AND l.sorts
),
best AS (
  -- An index found through a collation is not listed a second time under the
  -- database's LC_CTYPE.  The repair is the same REINDEX.
  SELECT h.*, min(h.rank) OVER (PARTITION BY h.indexrelid) AS best_rank
    FROM hits h
)
SELECT i.indrelid::regclass::text   AS table_name,
       b.indexrelid::regclass::text AS index_name,
       string_agg(DISTINCT b.label, ', ') AS collations,
       pg_get_indexdef(b.indexrelid) AS definition
  FROM best b
  JOIN pg_index i ON i.indexrelid = b.indexrelid
 WHERE b.rank = b.best_rank
 GROUP BY 1, 2, 4
 ORDER BY 1, 2;
