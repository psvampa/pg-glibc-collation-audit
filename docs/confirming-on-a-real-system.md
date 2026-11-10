# Confirming on a real system

A source diff is an argument, not a proof of what runs in production. This
page is how you measure the order inside PostgreSQL on your own machines, and
what it takes for that measurement to mean something. Step 11 of command 1.b
measures it too, through each machine's glibc and outside PostgreSQL
([commands.md](commands.md)).

It covers commands 2 and 3 of [the README](../README.md#the-commands). Before
you run either, check the setup traps in [requirements.md](requirements.md).
Command 2 needs PostgreSQL 15 or newer, and command 3 runs on PostgreSQL 14 or
newer.

## What the template does

[`sql/collation_confirmation_template.sql`](../sql/collation_confirmation_template.sql)
runs on both the old and the new machine, side by side. It imports system
collations, builds a real index on a column using the flagged locale, and
prints its `ORDER BY` output. You `diff` the two machines' outputs.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

## Which locales to run it for

Every locale in step 2's list. That is not every locale whose order
can change: no step compares `localedef`'s or `strcoll`'s
[code](limitations.md#no-step-compares-localedefs-or-strcolls-code), and
`ko_KR` changed that way between RHEL8 and RHEL9 with its rules the same.
Command 1.b measures every locale on both machines; without it, also run the
template for the locales your databases use. With command 1.b, add every
locale step 11 reports as changed, every one
it reports as not known to be unchanged, and every one it names in a line that
begins "!! NOT checked whether", "!! Measured in part" or "!! Not compared by
their sort tables". The lines that begin with `upper()`, `!! upper()`,
`Character rules` or `!! Character rules`, and those on the decimals of
`money`, are not about sort order, and the template does not confirm them. Nor
does it confirm the order of the locales named in the line that begins "!! Not
measured in a database of another encoding", even when step 11 reports them as
changed ([why](limitations.md#what-step-11-does-not-see)). Their UTF-8 forms,
such as `th_TH.utf8`, still go through it as above.

Step 2's full list holds `C.UTF-8` under its source name, `C`, and step 11
lists it as `C.utf8`. Whichever step names it,
[the probe](#the-cutf-8-probe) measures it instead of the template.

## Choosing the three values

The template compares three strings you supply, and choosing them is what
decides whether the run proves anything. They are not words in the language.
They are the characters the rule that changed moves.

Derive them from that rule. For a `localedef` change that means the boundaries
of the affected range. Under each locale whose rules differ, step 2 names the
characters the lines that changed name; or it says that which characters is
not known, and why, or that those lines name no character. When a changed line
decides how other lines are read or where other rules sort, it says that every
rule can change and, when it knows them, gives the count of the characters the
changed lines and the lines read otherwise name, with the list in the file it
names. For a locale it lists
because its character set changed, it prints which of the characters its rules
name the character set changed, or why it could not compare them. For a locale
it lists because a collating element, a combination of letters sorted as one,
is built differently out of a transliteration, it names the file that defines
the element and the characters transliterated. The strings must contain the
whole element. Step 11, in
command 1.b, names every character, other string and pair it found, in the
file its summary names.

The characters step 2 names are where to start, not proof that every one of
them moved. A
rule that was rewritten names everything it touches, weights included, so
three characters from the list that sort the same on both machines do not
clear the locale. And when step 2 says every rule can change, the list is not
all the values to test. Read the locale's rules, and those of the files it
copies.

The worked example is `ko_KR` between glibc 2.28 and 2.34, where the whole
difference is the last syllable of the Hangul block against any Hanja.
Everyday Korean text never reaches that syllable, so three plausible words
return a clean result on a locale whose order did change. That is the exact
failure this step exists to avoid. The characters, and what they print on each
build, are in
[results.md](results.md#the-ko_kr-mechanism-and-its-minimal-test-case).

For a locale no step names characters for, read its source and the files it
copies. Two kinds of character
can move there with no file changing: one inside an
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
`sv_SE.utf8`. Step 2's list names [locales, not
spellings](glossary.md). A name in `locale -a` belongs to the list when, without
the part from the dot up to any `@`, it is one of its names
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

PostgreSQL is not exposed to this when the table and its index are built
under the glibc that runs the query, as the template does. `ORDER BY` on a libc
collation breaks the same ties with a byte comparison, so its order is total
and the plan does not change it. A machine that keeps an index built under the
other glibc, such as a physical replica, a cluster carried over by
`pg_upgrade` or an OS upgraded in place, is different. There `ORDER BY` can
read the index and return the old order, where a sort returns the new one.

### The database default is a separate question

It is also the answer for most columns. A `text` column with no explicit
`COLLATE` does not carry a libc collation. It carries a pointer resolved at
runtime from `pg_database`. In a typical database that is most text columns,
and in a container it is usually all of them, because `initdb` inherits the
locale from the environment and minimal images ship `LANG=C.UTF-8`.

`C.UTF-8` under the `libc` provider **is** exposed. PostgreSQL special-cases
only the literal strings `C` and `POSIX` to byte comparison, so libc `C.UTF-8`
goes through `strcoll` like any other locale.

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
- **It is run even when the audit flagged nothing**, because step 2
  reads source files and never measures how your machine sorts `C.UTF-8`.

The probe refuses to run if `C.utf8` is not in `pg_collation`.

---

[Documentation index](README.md) · [Requirements and setup](requirements.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
