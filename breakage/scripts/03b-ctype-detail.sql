-- LC_CTYPE sweep, pass 2: every code point inside the 18 blocks whose md5
-- differed between the two nodes in 03-ctype-sweep.sql.  One line per code
-- point; diffing the two sides names the characters whose lower(), upper() or
-- character class moved between glibc 2.28 and 2.34.
\pset pager off
\pset format unaligned
\pset tuples_only on
\pset fieldsep '|'
SET client_min_messages = error;

SELECT i,
       lower(c COLLATE "sv_SE.utf8"),
       upper(c COLLATE "sv_SE.utf8"),
       ((c COLLATE "sv_SE.utf8") ~ '[[:alpha:]]')::int::text ||
       ((c COLLATE "sv_SE.utf8") ~ '[[:digit:]]')::int::text ||
       ((c COLLATE "sv_SE.utf8") ~ '[[:punct:]]')::int::text ||
       ((c COLLATE "sv_SE.utf8") ~ '[[:space:]]')::int::text
FROM (SELECT i, chr(i) AS c
      FROM generate_series(1, 1114111) i
      WHERE (i < 55296 OR i > 57343)
        AND (i / 4096) IN (0,1,2,3,4,9,10,16,17,19,22,24,27,30,31,42,48,49)) s
ORDER BY i;
