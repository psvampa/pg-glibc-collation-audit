# Confirming on a real system

A source diff is an argument, not a proof of what runs in production. This
page is how you measure the order on your own machines, and what it takes for
that measurement to mean something.

It covers commands 2 and 3 of [the README](../README.md#the-commands). Before
you run either, check the setup traps in [requirements.md](requirements.md).
Both need PostgreSQL 15 or newer, and a langpack installed in the wrong order
will hand you a clean result that means nothing.

## What the template does

[`sql/collation_confirmation_template.sql`](../sql/collation_confirmation_template.sql)
runs on both the old and the new machine, side by side. It imports system
collations, builds a real index on a column using the flagged locale, and
compares `ORDER BY` output between the two.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

## Which locales to run it for

Every locale steps 1 to 3 flagged, and — if step 5 found a
[substantive code change](glossary.md) — every locale step 4 flagged too,
whether or not it showed up in steps 1 to 3.

## Choosing the three values

The template compares three strings you supply, and choosing them is what
decides whether the run proves anything. They are not words in the language.
They are the characters the rule that changed moves.

Derive them from that rule. For a `localedef` change that means the boundaries
of the affected range. Step 2's diff names the file and the lines that
changed, and step 4 names the range.

The worked example is `ko_KR` between glibc 2.28 and 2.34, where the whole
difference is the last syllable of the Hangul block against any Hanja.
Everyday Korean text never reaches that syllable, so three plausible words
return a clean result on a locale whose order did change. That is the exact
failure this step exists to avoid. The characters, and what they print on each
build, are in
[results.md](results.md#the-ko_kr-mechanism-and-its-minimal-test-case).

A locale flagged by step 4 rather than by step 2 has no diff to read. There
the range itself is the guide, and its boundaries are the values to test.

## Three traps

Each of these makes a comparison agree with itself while proving nothing.

### The locale must actually be generated on both machines

Check `locale -a` first. If a locale is not generated, `sort` and PostgreSQL
silently fall back to `C`, and two machines both missing it agree with each
other perfectly. Use the [generated names](glossary.md) step 3 prints
(`sv_SE.utf8`), not the source file names.

### Feed both sides byte-identical input

glibc's `strcoll` reports some distinct strings as equal, and `sort(1)`
resolves those tied lines by input order. Two machines given
differently-ordered input can differ for reasons that have nothing to do with
glibc.

PostgreSQL is not exposed to this. `ORDER BY` on a libc collation breaks the
same ties with a byte comparison, so it is always a total, plan-independent
order.

### The database default is a separate question

It is also the answer for most columns. A `text` column with no explicit
`COLLATE` does not carry a libc collation. It carries a pointer resolved at
runtime from `pg_database`. In a typical database that is most text columns,
and in a container it is usually all of them, because `initdb` inherits the
locale from the environment and minimal images ship `LANG=C.UTF-8`.

The template asks `pg_database` first and says plainly whether the database is
exposed, then treats default-collated columns as in scope when it is.

`C.UTF-8` under the `libc` provider **is** exposed. PostgreSQL special-cases
only the literal strings `C` and `POSIX` to byte comparison, so libc `C.UTF-8`
goes through `strcoll` like any other locale. The `builtin` provider's
`C.UTF-8` (PG 17+) is a different implementation and is not exposed — see
[scope.md](scope.md).

## The `C.UTF-8` probe

[`sql/c_utf8_probe.sql`](../sql/c_utf8_probe.sql) is a separate file from the
template, and separate on purpose.

```sh
psql -X -f sql/c_utf8_probe.sql > this-node.out   # on each machine, then diff
```

Three things make it unlike the template.

- **It must not be edited.** The template is placeholder-driven and has to be;
  this one is fully determined, corpus included.
- **The positive control inverts here.** Everywhere else, agreement with
  `LC_ALL=C` means the locale was never generated and the comparison proves
  nothing. For `C.UTF-8`, agreement with byte order is the fix. Two
  contradictory rules in one file get read in the wrong order.
- **It is run even when the audit flagged nothing**, because nothing in steps
  1 to 5 can reach that locale at all.

It is also the one empirical check the langpack trap cannot fake, since
`C.utf8` exists on every machine whether or not any langpack is installed. It
still needs `pg_import_system_collations()` after a postmaster restart to
appear in `pg_collation`, and it refuses to run rather than fall back if it is
not there.

There is a third way to read "equals byte order = true" that PostgreSQL cannot
rule out, which is a build whose weights are all tied rather than correct. The
probe settles that itself, outside PostgreSQL, as query 6b.

## What else the template reports

Besides the index inventory, it reports **text partition keys**, every column
carrying an affected collation, and a manual-review list of `CHECK` and
`EXCLUDE` constraints.

The partition key matters most. If the collation changes, rows can belong in a
different partition than the one they are stored in, and no amount of
reindexing fixes that. The rows have to be moved. **A `REINDEX` list is not
the whole answer.**

---

[Documentation index](README.md) · [Requirements and setup](requirements.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
