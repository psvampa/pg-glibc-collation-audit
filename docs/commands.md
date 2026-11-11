# What each command does

Three commands. The first reads source and lists the locales the upgrade can
affect, and in its extended form also measures how each of your machines
sorts. The second and third measure, inside PostgreSQL, what the first leaves
open.

**Commands are numbered here; steps are what a run prints.** A run of command
1 says what it compared, then does its own work in steps numbered 2 and 11:
2 always, and 11 with each machine's file. Those numbers are not
these three.

## Command 1 — which locales the upgrade can affect

It runs in two ways, as in [the README](../README.md#the-commands). 1.a needs
no machine; 1.b adds one file from each of your machines, and with it more
checks.

### 1.a — quick verification

`./audit.sh <old> <new>`, given two tags, compares the two glibc versions as
their maintainers published them, and needs nothing but this checkout. It
says what it compares (*What was compared*: each side, where it was read
from, and where PostgreSQL warns about a collation's version between them),
runs step 2 and ends with one summary.

Either side can name a RHEL build instead of a tag, as `rpm -q glibc` prints
it without the architecture: `./audit.sh glibc-2.28-251.el8_10.40
glibc-2.34-275.el9_8`. The run then fetches that build's source RPM and two
of its packages from Rocky Linux's mirror, rebuilds its `localedata/` and
`locale/`, and reads them instead of the tag. For a machine not installed
yet, a side can be `latest-el10` (`latest-el<N>` for RHEL N): the newest
build Rocky Linux's mirror lists for that release, whose name the run prints
before anything else and reads from then on.

| The run's step | The question it answers |
|---|---|
| 2 | which locales' sort rules differ between the two versions, every `copy` followed, which locales' rules name a character their character set added, removed or gave other bytes, and which locales build a collating element differently out of a transliteration |

It does not compare the code that compiles and compares the rules,
`localedef`'s and `strcoll`'s; every run says so under *What was compared*
([limitations](limitations.md#no-step-compares-localedefs-or-strcolls-code)).

What you get is a list to worry about. What you do **not** get is
confirmation that a locale on the list really moved, nor that a locale off it
did not: `ko_KR` is off the list over RHEL8 to RHEL9, its rules unchanged,
and its order changed through `localedef`'s code.

### 1.b — extended verification, against your two machines

The same command, given one file from each machine with `--old-node` and
`--new-node`. Each is the file `scripts/locale_order.py --extract` wrote on
that machine. The run then reads each machine's own locale sources
and charmaps, which its file holds, as the build `rpm -q glibc` printed on
that machine, in place of the tag, as in 1.a: the side you give is that
build or its tag. Nothing is downloaded. When a file holds no locale
sources, or names a build that is not of RHEL 8, 9 or 10, and you gave its
tag, both sides read their tags and the run says so before anything else;
given as
the build itself, it is refused, as in 1.a. The two files add the step
below.

| The run's step | The question it answers |
|---|---|
| 11 | how does each machine's own glibc sort the locales installed on it, is each one's compiled sort table the same on both, and which characters and other strings moved between the two? |

With the files, the run answers questions the upstream comparison cannot
reach at all:

- **What your distro changed on its own, and a locale it adds** that the
  upstream version it started from does not have. Step 2 compares the
  two machines' own locale sources and charmaps, so such a patch or file is
  compared like any other. `C.UTF-8` is such a locale on RHEL8 and RHEL9, and
  in a container it is usually the database collation.
- **How each machine really sorts.** Step 11 reads no source. It asks each
  machine's glibc to order every character, and every lone byte, a database of
  the locale's encoding can hold, with the call
  PostgreSQL makes under a `libc` collation, and compares the two machines.
  That is how it sees `ko_KR`, whose file is identical at 2.28 and 2.34 while
  its order changed. It also compares each locale's compiled sort table: one
  that is the same on both machines settles the locale for any text, given
  the same glibc code on both. What it does not see, it prints beside its
  result.

Leave the files out and the summary says `NOT RUN`
for step 11; given a tag on either side, it also says `NOT CHECKED` for what
that side's distro changes. They are not omitted, because a section that
vanishes reads like a section that found nothing.

A file without the locale sources still holds the measurement and the build,
because the extraction notes why the sources are missing and goes on. Step 2
then reads the tags, or the run is refused when that side was given as the
build itself, as above; the warning that says so carries the file's
reason, such as `/usr/share/i18n/locales is empty`. A file without the
charmaps is still read for its sources: step 2 says `NOT RUN` for the
character sets, and step 11 says it did not check the strings it measured
against them, each with the reason.

**Step 2 compares rules, not order.** The weights are computed when the
locale is built, so two machines can hold byte-identical files
and still sort differently. That is what step 11 measures, and commands 2 and
3 measure inside PostgreSQL.

With the two files, the run sets step 2's rules beside step 11's order,
at the end of step 11 and under the measured order in the summary: each
locale of the old machine that step 11 names, and whether step 2 found its
rules changed. The line says what changed, never why. Where step 2 compared
the tags, or compared nothing, the run says so and no locale gets a line.

**What step 11 names in a locale whose sort table changed.** Beside what it
measured, the locale's line counts the sequences the table marks as changed,
those in one machine's table only, and those that compare differently against
a marked one, and names the first of them, "among them", never as a complete
list. A name is a character (`W (U+0057)`), a run of consecutive characters
(`U+0E01..U+0E3A`), a string of several (`U+0E40 U+0E01`), or, where glibc
reads no character, its bytes. Outside UTF-8, what glibc reads comes from the
old machine's charmap: without it, every name in such a locale is its bytes,
and the line that says the charmaps were not checked says so too. "And N
more" counts names, so a run counts as one. Where the line names nothing, it
says why: no levels on one machine (glibc then compares bytes there), a
change in bytes its record of the sequences does not keep or only in their
numbering, a different number of levels, or sequences that could not be
read.
The file the summary names holds every name, with how it was named.

#### The file from each machine

The commands that write the two files are in
[the README](../README.md#the-commands), which holds the only copy of them.

Each file holds the measurement of step 11, a copy of the machine's
`/usr/share/i18n/locales` and `/usr/share/i18n/charmaps`, and, written last,
a list of them: the glibc build, as `rpm -q glibc` prints it on that machine,
and a checksum of every file. Each copy is the whole folder or none of it, and
when it is none the list says why.

Beside every locale's order, the measurement holds the decimals each locale
gives the `money` type, whether glibc accepts the locale and can convert its
text, what glibc's character rules (`LC_CTYPE`) answer for every character and
every byte, and which PostgreSQL processes on the machine run another C
library than the one measured, because an update replaced it on disk or
because they run another file, as in a container. Step 11 compares the
decimals, those two answers and the character rules between the two machines,
and checks each machine's processes against
what was measured there.

The README's install line names the exact glibc build the machine runs, so
`dnf` installs the sources of that build and nothing else. A plain `dnf
install glibc-locale-source` installs the newest sources instead, and on a
machine that is behind it upgrades glibc itself to match. Measured on RHEL
8.10, where it planned to take glibc from `2.28-251.el8_10.34` to
`2.28-251.el8_10.40`, which changes the system you set out to audit.

`audit.sh` checks each file whole before it reads any of it, and stops, with
the reason, on a file that:

- is not whole: a run cut short, text the machine printed ahead of it such as
  a login message, or a second file appended to the first;
- was written in a format this checkout's `--extract` does not write;
- was measured by another copy of `scripts/locale_order.py`, over other
  strings or other byte sequences than this checkout's;
- holds an entry other than the one its list describes, such as an edited file
  or a name that would land outside the copy;
- names no glibc build, because `rpm -q glibc` printed none on that machine.

For the first four, extract again with this checkout's
`scripts/locale_order.py`. Once the copy is laid out, what was written is read
back against the list, so a disk that renames or drops a file stops the run as
well.

The two files go together, since one machine's file compares nothing.

#### A measurement you already have

`audit.sh` reads a machine's measurement only from the file `--extract`
writes. If what you kept of a machine you have since upgraded is the output
of a plain `python3 scripts/locale_order.py`, here `old-measurement.out`,
step 11 compares it by hand with the new machine's file, laid out with
`--unpack`:

```sh
python3 scripts/locale_order.py --unpack new.tar new
python3 scripts/locale_order.py --compare old-measurement.out \
    new/locale_order.out --tags glibc-2.28 glibc-2.34 \
    --new-charmaps new/charmaps --summary-to summary.txt --sql-to confirm
```

`--tags` names the two glibc versions, so that a measurement taken on
another glibc is refused; without it nothing checks that. `--sql-to` writes
the `confirm.<ENCODING>.sql` files command 3 runs on both machines. The old
measurement holds no charmaps, so step 11 checks only the new machine's,
names the strings of a locale not in UTF-8 by their bytes, and says so.
Step 2 is a run of its own: `./audit.sh` with each machine's build (1.a).

## Command 2 — whether `C.UTF-8`'s order changed

`sql/c_utf8_probe.sql`, run on each machine, the two outputs compared. It
needs no editing.

This is the one locale 1.a cannot reach when its file is in neither version;
1.b measures it, in step 11. PostgreSQL will not warn about it either.
It is needed when a database uses `C.UTF-8` — which in a container it usually
does, without anyone having chosen it.

Like step 11, this **measures the order**, but inside PostgreSQL. It sorts on
the machine as it actually runs and compares the two results.

Reading its output takes one warning, because for this locale the usual tell
is inverted. That, and how the probe differs from the template, are in
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#the-cutf-8-probe).

## Command 3 — confirming the order on your own builds

`sql/collation_confirmation_template.sql`, edited first, run on both machines.
Optional. This sorts strings you choose inside PostgreSQL.

It measures the order of three strings you supply, under a locale you
name, on both machines. Choosing them decides whether the run proves
anything; how to choose them is in
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#choosing-the-three-values).

**With the two machines' files, step 11 writes the SQL itself.** One file
per database encoding, `measured_order_confirm.<pair>.<ENCODING>.sql` in the
output folder, with the test pairs step 11 found that a database of that
encoding can store, in the bytes it measured, under every name such a
database knows the locale by. The UTF8 file also tests each string of
several characters that a locale's compiled sort table names as changed,
with a pair taken from the two machines' tables, and the characters whose
rules (LC_CTYPE) change: every answer of upper(), lower(), initcap() and the
character classes that differs between the two machines, under one locale of
each group step 11 found changed alike, the others counted. These files need no editing. Each only reads, so
it runs unchanged on both servers (`psql -X -d DATABASE -f FILE > old.out
2>&1`), and `scripts/confirm_sql.py read FILE old.out new.out` reads the two
outputs against the file. It confirms only when every test is answered once
in each output, like the old machine in the old one and like the new machine
in the new one. It refuses when a collation cannot be used, psql reports an
error outside a collation's own check, or the glibc version PostgreSQL
reports is not that machine's. It never says that a locale is unchanged:
what the file does not test is listed in it, and the reader repeats the
list.

## What none of the three does

None of them touches your data or your indexes, and none of them decides for
you. What they do change is small and named: 1.b installs one package on each
machine, the locale sources of the build it already runs; command 3
creates a test table of its own and, run as a superuser, first imports the
system's collations into PostgreSQL's catalog; command 2 and the SQL files
step 11 writes only read. Command 1 hands you a list,
and in 1.b a measurement; commands 2 and 3 hand you measurements. What to do
in your databases is yours to decide.
