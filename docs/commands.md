# What each command does

Three commands. The first reads source and proves what cannot have changed;
the second and third measure what a source read cannot settle.

**Commands are numbered here; steps are what a run prints.** A run of command
1 does its own work in ten numbered steps, and those numbers are not these
three. Where this page needs them it says "the run's step 4", and
[method.md](method.md) is where each of them is explained.

## Command 1 — which locales the upgrade can affect

`./audit.sh <old tag> <new tag>` compares the two glibc versions as their
maintainers published them, and needs nothing but this checkout. It runs five
checks in order and ends with one summary.

| The run's step | The question it answers |
|---|---|
| 1 | which locale files changed between the two versions, and how far the change reaches |
| 2 | which of those changes fall inside the part that defines sort order |
| 3 | which other locales inherit a change, because they copy from the file that changed |
| 4 | which locales a comparison of files can never clear, because they define their order with abbreviated ranges whose real weights are computed later |
| 5 | whether the code that computes those weights changed |

The run's steps 3 and 5 together give the complete set of affected locale
names.

What you get is a list to worry about and a proof about everything else: if
neither a locale's rules nor the code that compiles them changed, its order
cannot have changed. What you do **not** get is confirmation that a locale on
the list really moved. A flagged locale can turn out unaffected — `ber_DZ` and
`kab_DZ` were flagged and the change was a role swap that leaves the order
alone.

### Command 1 extended — the same run against your two machines

The same command, with each machine's own locale files and build ids added.
The five checks above run either way; supplying the files adds five more,
numbered 6 to 10 in the output.

| The run's step | The question it answers |
|---|---|
| 6 and 7 | do the distro's patches on that machine touch sort order? One step per side |
| 8 | does one machine's collation data differ from the other's? |
| 9 and 10 | does that machine's own data use the abbreviated ranges a file comparison can never clear? One step per side |

Two of those answer questions the upstream comparison cannot reach at all:

- **What your distro changed on its own.** Steps 6 and 7 compare a machine
  against the version its distro started from.
- **A locale your distro adds**, which is in no upstream version, so no choice
  of versions can reach it. Step 8 is the only thing that sees it, and it
  needs both machines. `C.UTF-8` is that locale, and in a container it is
  usually the database collation.

Leave the files out and steps 6 to 10 print `NOT RUN`. They are not omitted,
because a section that vanishes reads like a section that found nothing.

**This still settles data, not order.** The weights are computed when the
locale is built on the machine, so two machines can hold byte-identical files
and still sort differently. That is what commands 2 and 3 are for.

#### Taking the copy, and checking it

The commands that produce the two directories are in
[the README](../README.md#the-commands), which holds the only copy of them.
In a container `docker exec` replaces `ssh` in those commands, and `rpm -q
glibc` prints the architecture as well (`...x86_64`) — either form is a usable
build id.

Installing `glibc-locale-source` **upgrades glibc**, because the two packages
are version-locked. Read the build id after installing it, not before.

The build ids are required, because a result is bound to the build it was
taken on and nothing in a directory of locale files carries a version.

**Check each copy against the machine it came from** — `ls el8-locales | wc -l`
against `ls /usr/share/i18n/locales/` run there. The run does not do this for
you, and a copy that lands most of its files is not refused: the floor is
absolute, and only a directory too small to be a real copy at all is rejected.

What never arrived then comes back as an ordinary finding. The run's steps 6
and 7 list it under `Absent on the node`, step 8 under `Only on the old node`
or `Only on the new node`, where a failed transport reads exactly like a
locale the upgrade removed or added. Two cases do earn a `!!`: a backported
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

This is the one locale command 1 cannot reach when its file is in neither
version, and PostgreSQL will not warn about it either. It is needed when a
database uses `C.UTF-8` — which in a container it usually does, without anyone
having chosen it.

Unlike command 1, this **measures the order**. It sorts on the machine as it
actually runs and compares the two results.

One warning about reading it: for this locale the usual tell is inverted.
Agreeing with byte order is the corrected state here, not the sign that the
locale was never generated.

## Command 3 — confirming the order on your own builds

`sql/collation_confirmation_template.sql`, edited first, run on both machines.
Optional: command 1's result stands on its own, and this is what turns that
argument into a measurement.

It does two separate jobs.

**It measures the order** of three strings you supply, under a locale you
name, on both machines. Choosing those three strings is what decides whether
the run proves anything: they have to be the characters the changed
rule moves, not words in the language. The run's step 2 lists, under each
locale it flags, the characters its changed rules name.

**It inventories your database** — which indexes, partitioned tables, columns
and constraints use a collation that is exposed. That half needs no editing
and no confirmation; it answers which objects of yours are at stake.

## What none of the three does

None of them changes anything, and none of them decides for you. Command 1
hands you a list and a proof, commands 2 and 3 hand you measurements. The
decision to reindex is yours.
