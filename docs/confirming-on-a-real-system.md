# Confirming on a real system

A source diff is an argument, not a proof of what runs in production. This
page is how you measure the order inside PostgreSQL on your own machines, and
what it takes for that measurement to mean something. Step 11 of command 1.b
measures it too, through each machine's glibc and outside PostgreSQL
([commands.md](commands.md)).

It covers commands 2 and 3 of [the README](../README.md#the-commands). Before
you run either, check the setup traps in [requirements.md](requirements.md).
Both need PostgreSQL 15 or newer.

## What the template does

[`sql/collation_confirmation_template.sql`](../sql/collation_confirmation_template.sql)
runs on both the old and the new machine, side by side. It imports system
collations, builds a real index on a column using the flagged locale, and
prints its `ORDER BY` output. You `diff` the two machines' outputs.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

## Which locales to run it for

Every locale steps 1 to 3 flagged, and every locale step 4 flagged that step 5
does not [clear](limitations.md#step-5-reports-it-does-not-decide), whether or
not it showed up in steps 1 to 3. When step 5 does not clear the code, add the
locales step 4 leaves out as built in byte order, which the summary names. With
command 1.b, add every locale step 11 reports as changed, and every one it
reports as not known to be unchanged.

Steps 9 and 10, in command 1.b, run step 4 again on each machine's own files,
so the same rule holds for every locale they flag. They list `C.UTF-8` by its
source name, `C`, and step 11 lists it as `C.utf8`. Whichever step names it,
[the probe](#the-cutf-8-probe) measures it instead of the template.

## Choosing the three values

The template compares three strings you supply, and choosing them is what
decides whether the run proves anything. They are not words in the language.
They are the characters the rule that changed moves.

Derive them from that rule. For a `localedef` change that means the boundaries
of the affected range. Under each locale it flags, step 2 lists the characters
its changed rules name, or says it could not identify them and that the locale
must be considered suspicious, or, for a file read with other comment or escape
characters, that any rule in it can read differently. For a locale it lists
because its character set changed, it prints which of the characters its rules
name the character set changed, or why it could not compare them. Step 11, in
command 1.b, names the characters that moved, when there are few enough to
list.

Step 2's list is where to start, not proof that every character on it moved. A
rule that was rewritten names everything it touches, weights included, so
three characters from the list that sort the same on both machines do not
clear the locale.

The worked example is `ko_KR` between glibc 2.28 and 2.34, where the whole
difference is the last syllable of the Hangul block against any Hanja.
Everyday Korean text never reaches that syllable, so three plausible words
return a clean result on a locale whose order did change. That is the exact
failure this step exists to avoid. The characters, and what they print on each
build, are in
[results.md](results.md#the-ko_kr-mechanism-and-its-minimal-test-case).

For a locale step 3 adds, the characters are the ones step 2 lists under the
file step 3 says it reaches. For a locale that only steps 4, 9 or 10 flagged,
where step 11 names no characters, read its source and the files it copies.
Two kinds of character can move there with no file changing: one inside an
[ellipsis range](glossary.md), best taken at the range's boundary, and one the
files do not list, which takes `localedef`'s default weight. Put one of each
kind the locale has in the three strings, and fill the rest with characters
the files do list.

## Three traps

Each of these makes a comparison agree with itself while proving nothing.

### The locale must actually be generated on both machines

Check `locale -a` first. If a locale is not generated, `sort` silently falls
back to `C`, and two machines both missing it agree with each other
perfectly. Check the exact name the test runs under, such as
`sv_SE.utf8`. The Reindex list and step 4's list name [locales, not
spellings](glossary.md). A name in `locale -a` belongs to a list when, without
the part from the dot up to any `@`, it is one of that list's names
(`sv_SE.utf8` and `sv_SE.iso885915` both become `sv_SE`).

Step 11, in command 1.b, catches half of this. It names every locale the old
machine has and the new one does not, as not known to be unchanged. A locale
missing on both it cannot see, because it measures only what `locale -a`
lists.

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
  `LC_ALL=C` means the locale was not applied and the comparison proves
  nothing. For `C.UTF-8`, agreement with byte order is the fix. Two
  contradictory rules in one file get read in the wrong order.
- **It is run even when the audit flagged nothing**, because steps 1 to 5
  read upstream's files, never your machine's `C.UTF-8`.

The probe refuses to run if `C.utf8` is not in `pg_collation`.

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
