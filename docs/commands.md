# What each command does

Three commands. The first reads source and proves what cannot have changed,
and in its extended form also measures how each of your machines sorts. The
second and third measure, inside PostgreSQL, what the first leaves open.

**Commands are numbered here; steps are what a run prints.** A run of command
1 does its own work in up to eleven steps, five with the tags alone and six
more with a file from each machine, and those numbers are not these three.

## Command 1 — which locales the upgrade can affect

It runs in two ways, as in [the README](../README.md#the-commands). 1.a needs
the tags alone; 1.b adds one file from each of your machines, and with it six
more checks.

### 1.a — quick verification

`./audit.sh <old tag> <new tag>` compares the two glibc versions as their
maintainers published them, and needs nothing but this checkout. It runs five
checks in order and ends with one summary.

| The run's step | The question it answers |
|---|---|
| 1 | which locale files changed between the two versions, and how far the change reaches |
| 2 | which of those changes fall inside the part that defines sort order |
| 3 | which other locales inherit a change, because they copy from the file that changed |
| 4 | which locales a comparison of files can never clear, because part of their order is computed later: the weights of abbreviated ranges, and the default weight of each character they do not list |
| 5 | whether the code that computes those weights changed |

What you get is a list to worry about and a proof about everything else: if
neither a locale's rules nor the code that compiles them changed, its order
cannot have changed. What you do **not** get is confirmation that a locale on
the list really moved. A flagged locale can turn out not to move — `ber_DZ` and
`kab_DZ` were flagged and the change was a role swap that leaves the order
alone.

### 1.b — extended verification, against your two machines

The same command, given one file from each machine with `--old-node` and
`--new-node`. Each is the file `scripts/locale_order.py --extract` wrote on
that machine. The five checks above run either way; the two files add six
more.

| The run's step | The question it answers |
|---|---|
| 6 and 7 | do the distro's patches on that machine touch sort order? One step per side |
| 8 | does one machine's collation data differ from the other's? |
| 9 and 10 | the question of step 4, asked of that machine's own data. One step per side |
| 11 | how does each machine's own glibc sort the locales installed on it, and which characters moved between the two? |

Three of those answer questions the upstream comparison cannot reach at all:

- **What your distro changed on its own.** Steps 6 and 7 compare a machine
  against the version its distro started from.
- **A locale your distro adds**, which the upstream version it started from
  does not have. Step 8 compares one machine's copy of it with the other's,
  and it needs both machines. `C.UTF-8` is that locale on RHEL8 and RHEL9, and
  in a container it is usually the database collation.
- **How each machine really sorts.** Step 11 reads no source. It asks each
  machine's glibc to order every character the locale can hold, with the call
  PostgreSQL makes under a `libc` collation, and compares the two machines.
  That is how it sees `ko_KR`, whose file is identical at 2.28 and 2.34 while
  its order changed. What it does not measure, it prints beside its result.

Leave the files out and the summary says `NOT RUN` for steps 6 and 7, step
8, steps 9 and 10 and step 11, and `NOT CHECKED` for what the distro adds or
drops. They are not omitted, because a section that vanishes reads like a
section that found nothing.

A file without the locale sources still holds the measurement and the build,
because the extraction notes why the sources are missing and goes on. Step 11
runs. Every summary block that needed those sources says `NOT RUN`, or `NOT
CHECKED`, with the file's reason, such as `/usr/share/i18n/locales is empty`.

**Steps 6 to 10 settle data, not order.** The weights are computed when the
locale is built, so two machines can hold byte-identical files
and still sort differently. That is what step 11 measures, one character at a
time, and commands 2 and 3 measure inside PostgreSQL.

#### The file from each machine

The commands that write the two files are in
[the README](../README.md#the-commands), which holds the only copy of them.

Each file holds the measurement of step 11, a copy of the machine's
`/usr/share/i18n/locales`, and, written last, a list of both: the glibc build,
as `rpm -q glibc` prints it on that machine, and a checksum of every file. The
copy is the whole folder or none of it, and when it is none the list says why.

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
- was written by another version of `--extract`;
- holds an entry other than the one its list describes, such as an edited file
  or a name that would land outside the copy;
- names no glibc build, because `rpm -q glibc` printed none on that machine.

For the first three, extract again with this checkout's
`scripts/locale_order.py`. Once the copy is laid out, what was written is read
back against the list, so a disk that renames or drops a file stops the run as
well.

The two files go together, since one machine's file compares nothing, and
they do not mix with the options of the next section.

#### The same checks from separate pieces

`audit.sh` also takes the pieces of a node file one by one, for when you have
them already, such as a copy of the locale folder taken from an image:
`--old-locales-dir` and `--new-locales-dir`, each with its `--old-build-id` or
`--new-build-id`, and `--old-order` and `--new-order`, each the output of a
plain `python3 scripts/locale_order.py > old.out` on that machine. Take a
folder with `tar` over `ssh`, never `docker cp`, whose target `/tmp` in a
container is a separate mount, so the copy silently does nothing. `rpm -q
glibc` gives the build id, with or without the architecture (`...x86_64`).

A copy taken this way carries no checksum, so the checks below are yours to
make.

The build ids are required, because a result is bound to the build it was
taken on and nothing in a directory of locale files carries a version.

**Check each copy against the machine it came from** — compare the names,
`ls el8-locales` against `ls /usr/share/i18n/locales/` run there, not only the
counts. The run does not compare against the machine. It refuses a copy only
when it is too small to be one, below half the files of the published version
for steps 6 and 7 and below 200 for steps 8 to 10.

What never arrived is named, not refused. When a copy lacks a file its
published version has, steps 6 and 7 and steps 9 and 10 print a `!!` naming
each one, and the summary repeats it. The run cannot tell a file the machine
does not ship from one the copy lost, and the check above is what tells them
apart. Step 8 lists what one copy lacks under `Only on the old node` or `Only
on the new node`, where a failed transport reads exactly like a locale the
upgrade removed or added. Two more cases earn their own `!!`: a backported
locale such as `C` missing from one side (step 8), and a missing file that
other locales copy, such as `iso14651_t1` (steps 9 and 10).

**Which printed count to check against matters.** Steps 9 and 10 print the
copy's own count as `Files at <build id>`, and that is the number to set
against the machine's. The `Compared N file(s)` lines of steps 6 to 8 are
intersections — with the published version for steps 6 and 7, with the other
machine for step 8 — so they are never larger than `Files at`, and equality
there does not mean the copy is complete. A `Compared` line adds back up to
`Files at` only together with the `absent upstream` or `only on the ... node`
line printed beside it.

Supply one side only and that side is still checked against its published
version; the side you left out prints `NOT RUN` and names the flag that was
missing.

## Command 2 — whether `C.UTF-8`'s order changed

`sql/c_utf8_probe.sql`, run on each machine, the two outputs compared. It
needs PostgreSQL 15 or newer and no editing.

This is the one locale 1.a cannot reach when its file is in neither version;
1.b reaches it, in steps 8 and 11. PostgreSQL will not warn about it either.
It is needed when a database uses `C.UTF-8` — which in a container it usually
does, without anyone having chosen it.

Like step 11, this **measures the order**, but inside PostgreSQL. It sorts on
the machine as it actually runs and compares the two results.

Reading its output takes one warning, because for this locale the usual tell
is inverted. That, and how the probe differs from the template, are in
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#the-cutf-8-probe).

## Command 3 — confirming the order on your own builds

`sql/collation_confirmation_template.sql`, edited first, run on both machines.
Optional. Command 1's result stands on its own; this sorts strings you choose
inside PostgreSQL, which also reaches a rule for a combination of letters that
step 11 cannot see.

It does two separate jobs.

**It measures the order** of three strings you supply, under a locale you
name, on both machines. Choosing them decides whether the run proves
anything; how to choose them is in
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#choosing-the-three-values).

**It inventories your database**, which answers which objects of yours are
at stake. That half needs no editing and no confirmation; what it lists is in
[confirming-on-a-real-system.md](confirming-on-a-real-system.md#what-else-the-template-reports).

## What none of the three does

None of them touches your data or your indexes, and none of them decides for
you. What they do change is small and named: 1.b installs one package on each
machine, the locale sources of the build it already runs; commands 2 and 3
each create a test table of their own, and command 3 first imports the
system's collations into PostgreSQL's catalog. Command 1 hands you a list and
a proof, and in 1.b a measurement; commands 2 and 3 hand you measurements. The
decision to reindex is yours.
