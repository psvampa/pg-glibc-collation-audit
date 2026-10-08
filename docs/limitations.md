# Known limitations

Seven blind spots. Each one is a way this tool can report a clean result while
something it cannot see has changed.

1. [`C.UTF-8` is invisible to a tag diff](#cutf-8-is-invisible-to-a-tag-diff)
2. [Upstream tags are not your distro's glibc](#upstream-tags-are-not-your-distros-glibc)
3. [Below glibc 2.24 the method rests on one measured pair](#below-glibc-224-the-method-rests-on-one-measured-pair)
4. [Character repertoire changes are not audited](#character-repertoire-changes-are-not-audited)
5. [Step 5 reports, it does not decide](#step-5-reports-it-does-not-decide)
6. [Step 11 measures one character at a time](#step-11-measures-one-character-at-a-time)
7. [`LC_CTYPE` is not audited at all](#lc_ctype-is-not-audited-at-all)

## `C.UTF-8` is invisible to a tag diff

Its source file exists upstream only from glibc 2.35, and RHEL8 and RHEL9 ship
one anyway. Over RHEL8 to RHEL9 the file is therefore on both your machines and
in neither tag. Over RHEL9 to RHEL10 the new tag holds upstream's copy and the
old tag still has none. Either way the five steps that read upstream source
cannot see what your old machine runs.

It is worth knowing about because it is usually the default. Almost anywhere
`initdb` runs in a container the database collation is `C.UTF-8`, so every
text column without an explicit `COLLATE` sits on it. Its order did change
between RHEL8 and RHEL9.

PostgreSQL does not cover the gap either. It records no collation version for
any name beginning with `C.`, so no version mismatch can fire for this locale
and no warning will reach you.

What reaches it needs your machines rather than the clone. Give `audit.sh`
one file from each machine, as in command 1.b, and it compares the two
machines' `C.UTF-8` files (step 8) and measures how each machine sorts it
(step 11). [`sql/c_utf8_probe.sql`](../sql/c_utf8_probe.sql) measures the
order inside PostgreSQL, on the builds you actually run. What the file
comparison and the probe found is in [results.md](results.md).

## Upstream tags are not your distro's glibc

Your machines do not run upstream glibc. RHEL8 ships `glibc-2.28` carrying
hundreds of backported patches, and a collation change among them is invisible
to a comparison of the two upstream tags.

Give `audit.sh` one file from each machine and each side is also checked
against the version its distro started from, when its file holds the locale
sources, which reaches patches to the locale data. What no file comparison reaches is a backported change to glibc's
*code*, because the code is read between the two tags and nowhere else.

Step 11 reaches it, because it measures the glibc actually installed, patches
and all. For
[what step 11 does not see](#step-11-measures-one-character-at-a-time), and
for the order inside PostgreSQL, do not skip
[confirming on the real machines](confirming-on-a-real-system.md).

## Below glibc 2.24 the method rests on one measured pair

Exactly one pair older than glibc 2.24 has been run end to end. Nothing about
the method is known to break there and there is no version guard, but one pair
is not every pair.

If both your versions are that old, read the result as thinner evidence than
the two RHEL pairs carry, and confirm it on the machines.

## Character repertoire changes are not audited

`localedata/charmaps/` decides which characters exist to be given a sort
weight, and no step reads it. Every glibc release adds thousands of code
points to it.

Adding a character gives that character a weight rather than moving the
characters that already had one, and no impact from this has been found. It is
still a gap, and only data containing those newly added characters could ever
be touched by it.

## Step 5 reports, it does not decide

Step 5 shows you the changes to the C code that computes sort weights. It
cannot tell a change that moves a weight from one that moves nothing, so
somebody has to read them. This is the one part of the method that is not
mechanical.

If nobody on hand will read C, treat every locale step 4 flagged as unresolved
and measure it instead. Step 11, in command 1.b, measures every locale it can
on both machines, and
[confirming on the machines](confirming-on-a-real-system.md) measures inside
PostgreSQL. Either path needs no source reading and is the stronger evidence
anyway.

## Step 11 measures one character at a time

Step 11 asks each machine's glibc where every character a locale can hold
sorts, and how each two neighbours in that order are told apart. It does not
see:

- a rule for a particular combination of letters, such as a contraction (`ch`
  sorted as one letter). Two machines that agree on every character can still
  sort some longer strings apart;
- a change in how a character is told apart from its neighbour where glibc
  ignores that character at some level, which it reads less reliably;
- a locale whose encoding Python cannot convert, since the bytes PostgreSQL
  would compare cannot be built. On the three test machines that was four
  locales each, `hy_AM.armscii8`, `ka_GE`, `ka_GE.georgianps` and
  `zh_TW.euctw`, and the run names them as not known to be unchanged;
- a locale that is not installed, because it measures what `locale -a` lists
  and nothing else.

The run prints the first two beside its result every time. Command 3 sorts
strings you choose inside PostgreSQL, and reaches a rule for a combination of
letters when the strings contain it.

Not tried yet, so a failure there would be new: Python 3.14 on a real
machine, where only a simulation was run, a machine on cgroup v1, and ARM.

## `LC_CTYPE` is not audited at all

The other six sections are ways a sort-order change can be missed. This one
is a whole category nothing here looks at.

`LC_COLLATE` decides sort order. `LC_CTYPE` decides `upper()`, `lower()`,
`initcap()`, character classification and pattern matching, and under the
`libc` provider PostgreSQL takes it from glibc too. A functional index on
`lower(email)`, a unique index on `lower(username)` or a `CHECK` constraint
calling `upper()` breaks on a glibc upgrade for the same reason a `COLLATE`
index does. The function's output moves under an index built from the old
output.

PostgreSQL warns less here than it does for collation, not more. There is no
ctype version to compare, so a ctype change leaves no trail at all and there
is no mismatch for anything to detect.

No step of this tool reads it, and no verdict this project publishes says
anything about it in either direction, including "unchanged". A 🟢 on the
results table is a statement about sort order and nothing else. If a
`lower()`-based index matters to you, confirm it the way this project confirms
collation — on both machines, naming the builds.

The remaining categories (`LC_NUMERIC`, `LC_TIME`, `LC_MONETARY`,
`LC_MESSAGES`) are out of scope as well. They move `to_char()` output and
message text rather than index order, and no step reads them either.

---

[Documentation index](README.md) · [Scope](scope.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Glossary](glossary.md)
