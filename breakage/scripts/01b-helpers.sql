-- Two helpers so the probe's answers all land on stdout, where they can be
-- diffed.  Without them "was this accepted?" and "is this index well ordered?"
-- answer on stderr, which interleaves with stdout at an unpredictable point
-- and cannot be compared between two states.
SET client_min_messages = warning;
\set ON_ERROR_STOP on

-- amcheck is what check_index() calls. Without it every index would read
-- CORRUPT on both nodes, and the two sides would agree while proving nothing.
DO $guard$
BEGIN
  IF to_regprocedure('bt_index_check(regclass)') IS NULL THEN
    RAISE EXCEPTION 'amcheck is not installed: CREATE EXTENSION amcheck;';
  END IF;
END $guard$;

-- Both functions separate "the server answered" from "this could not be asked
-- at all", and they do it by naming the ANSWER, not by listing the ways an
-- absence can look. REJECTED and CORRUPT are the answers these scenes are
-- about; everything else -- a missing table, a typo in the statement, an index
-- that is not a B-tree -- is NOT ASKED. Listing the absences instead would
-- leave the reassuring word as the default: a malformed statement would print
-- REJECTED, which in four of these scenes is what a healthy constraint prints,
-- and it would print it identically on both nodes.
CREATE OR REPLACE FUNCTION try_sql(sql text) RETURNS text AS $$
BEGIN
  EXECUTE sql;
  RETURN 'ACCEPTED';
EXCEPTION
  -- class 23: unique, check, foreign key, exclusion, not-null, and the
  -- "no partition of relation found for row" that tuple routing raises.
  WHEN integrity_constraint_violation THEN
    RETURN 'REJECTED: ' || SQLERRM;
  WHEN others THEN
    RETURN 'NOT ASKED: ' || SQLERRM;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION check_index(ix regclass) RETURNS text AS $$
BEGIN
  PERFORM bt_index_check(ix);
  RETURN 'ok';
EXCEPTION
  WHEN index_corrupted OR data_corrupted THEN
    RETURN 'CORRUPT: ' || SQLERRM;
  WHEN others THEN
    -- not a B-tree, not an index, no amcheck: all of these used to read
    -- CORRUPT, on both nodes alike. A name that does not resolve at all never
    -- reaches here -- the regclass cast raises in the caller, which stops the
    -- run under ON_ERROR_STOP.
    RETURN 'NOT ASKED: ' || SQLERRM;
END $$ LANGUAGE plpgsql;
