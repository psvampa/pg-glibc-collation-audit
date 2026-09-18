-- LC_CTYPE sweep: every Unicode code point, under the sv_SE.utf8 collation.
--
-- Runs identically on both nodes (read-only, so it also works on a standby).
-- Output is one md5 per 4096-code-point block; diffing the two sides names
-- the blocks whose lower()/upper()/character-class answers moved between
-- glibc 2.28 and 2.34.  Pass 2 (generated from the diff) lists the exact
-- code points inside a block that moved.
--
-- Surrogates (U+D800..U+DFFF) are excluded: chr() rejects them in UTF8.
\pset pager off
\pset footer off
SET client_min_messages = warning;

\echo '--- node identity ---'
SELECT current_setting('server_version') AS pg_version,
       datcollate, datctype, datlocprovider
FROM pg_database WHERE datname = current_database();

\echo '--- ctype block hashes, collation sv_SE.utf8 ---'
SELECT (i / 4096) AS block,
       md5(string_agg(lower(c COLLATE "sv_SE.utf8") || '>' ||
                      upper(c COLLATE "sv_SE.utf8") || '>' ||
                      ((c COLLATE "sv_SE.utf8") ~ '[[:alpha:]]')::text ||
                      ((c COLLATE "sv_SE.utf8") ~ '[[:digit:]]')::text ||
                      ((c COLLATE "sv_SE.utf8") ~ '[[:punct:]]')::text ||
                      ((c COLLATE "sv_SE.utf8") ~ '[[:space:]]')::text,
                      ',' ORDER BY i)) AS block_md5
FROM (SELECT i, chr(i) AS c
      FROM generate_series(1, 1114111) i
      WHERE i < 55296 OR i > 57343) s
GROUP BY 1
ORDER BY 1;
