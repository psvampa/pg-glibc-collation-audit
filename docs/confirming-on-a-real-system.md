# Confirming on a real system

A source diff is an argument, not a proof of what actually runs in
production — and it says nothing about your distro's backports.
`sql/collation_confirmation_template.sql` is a template to run on both the
old and new OS/glibc, side by side: import system collations, build a real
index on a column using the flagged locale, and compare `ORDER BY` output
between the two. Run it for every locale steps 1 to 3 flagged, and — if step
5 found a substantive code change — for every locale step 4 flagged too,
regardless of whether it showed up in steps 1 to 3.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

Check `locale -a` before comparing: if a locale isn't generated on the box,
`sort`/PostgreSQL silently fall back to `C`, and two boxes both missing it
will agree with each other while proving nothing. Use the generated names
step 3 prints (`sv_SE.utf8`), not the source file names.

## What the template checks, and two traps

- **Feed both sides byte-identical input.** glibc's `strcoll` really does
  report distinct strings as equal — measured on RHEL8/RHEL9, about 0.1% of
  random string pairs under `sv_SE`/`en_US`/`de_DE`, and about 10% under
  `ko_KR`. `sort(1)` resolves those tied lines by **input order**, so two
  nodes given differently-ordered input can differ for reasons that have
  nothing to do with glibc. PostgreSQL is not exposed to this: `varstr_cmp`
  and the sortsupport comparator both break strcoll ties with `strcmp`
  (`src/backend/utils/adt/varlena.c`, unchanged in substance from PG 13 to
  18), abbreviated keys cannot bypass it — a zero from the abbreviated
  comparator means "indeterminate", not "equal", and forces the full
  comparator — and nondeterministic collations, the one case where the
  tie-break is skipped, are rejected for every provider except ICU. So
  `ORDER BY` on a libc collation is always a total, plan-independent order.
  The `COLLATE "C"` in the template is a no-op kept to state intent.
- **The database default is the answer for most columns, and it is a
  separate question.** A `text` column with no explicit `COLLATE` does not
  carry a libc collation — it carries OID 100, `default`, a pointer resolved
  at runtime from `pg_database`. In a typical database that is most text
  columns; in a container it is usually all of them, because `initdb`
  inherits the locale from the environment and minimal images ship
  `LANG=C.UTF-8`. The template asks `pg_database` first and says plainly
  whether the database is exposed, then treats default-collated columns as in
  scope when it is. `C.UTF-8` under the `libc` provider **is** exposed:
  PostgreSQL special-cases only the literal strings `C` and `POSIX` to byte
  comparison, so libc `C.UTF-8` goes through `strcoll` like any other locale.
  The `builtin` provider's `C.UTF-8` (PG 17+) is a different implementation
  and is not exposed.
- Besides the index inventory, it reports **text partition keys**, every
  column carrying an affected collation, and a manual-review list of
  `CHECK`/`EXCLUDE` constraints. A partition key matters most: if the
  collation changes, rows can belong in a different partition than the one
  they are stored in, and no amount of reindexing fixes that — the rows have
  to be moved. A `REINDEX` list is not the whole answer.
