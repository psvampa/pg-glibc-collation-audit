# Known limitations

Seven blind spots. Each one is a way this tool can report a clean result while
something it cannot see has changed.

1. [`C.UTF-8` is invisible to a tag diff](#cutf-8-is-invisible-to-a-tag-diff)
2. [Upstream tags are not your distro's glibc](#upstream-tags-are-not-your-distros-glibc)
3. [Below glibc 2.24 the method rests on one measured pair](#below-glibc-224-the-method-rests-on-one-measured-pair)
4. [A copied folder's character sets are not compared](#a-copied-folders-character-sets-are-not-compared)
5. [Step 5 reports, it does not decide](#step-5-reports-it-does-not-decide)
6. [Step 11 measures one character at a time](#step-11-measures-one-character-at-a-time)
7. [`LC_CTYPE` is compared by step 11 alone](#lc_ctype-is-compared-by-step-11-alone)

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

## A copied folder's character sets are not compared

A locale's character set (`localedata/charmaps/`) decides the bytes of every
character its rules name, and an unchanged rule sorts differently when those
bytes change, or when the character appears or disappears. Step 2 compares,
between the two glibc versions, the character sets the locales in SUPPORTED
are built with, and lists the locales whose rules name a character the newer
version added, removed or gave other bytes. A distro can patch the character
sets it ships. Given the file from each machine, as in command 1.b, steps 6
and 7 compare each machine's own with the version its distro started from,
and step 8 compares the two machines'.
Given copied folders instead (`--old-locales-dir` and `--new-locales-dir`),
they are not compared, and those steps say so.

Step 2 also compares, for the same builds, the collating elements a locale
builds out of transliterations. localedef builds them for the characters a
character set lacks, so a change to a transliteration file, or to the
character set, can move them with no rule changing. Steps 6 to 8 compare
what a machine's own transliterations build from its file, and not from a
copied folder.

## Step 5 reports, it does not decide

Step 5 shows you the changes to the C code that computes sort weights. It
cannot tell a change that moves an order from one that moves nothing, so
somebody has to read them. This is the one part of the method that is not
mechanical.

If nobody on hand will read C, treat every locale step 4 flagged, and every
one it leaves out as built in byte order, as unresolved and measure them
instead. Step 11, in command 1.b, measures every locale it can
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
- the few byte sequences that PostgreSQL stores, for characters it converts
  from UTF-8, in a database of an EUC-JP, EUC-KR or TIS-620 locale, and that
  the strings measured do not hold. The run names them under the locales of
  those encodings, and its characters file lists every one;
- a locale whose encoding PostgreSQL does not know, in a database of another
  encoding, which PostgreSQL allows. The bytes it sorts there are not the
  ones measured, and the run names those locales, with what it measured of
  how they convert;
- the bytes a database holds that PostgreSQL did not convert from UTF-8, as
  from a client of the database's own encoding, and the bytes of a SQL_ASCII
  database, which a superuser can create with any locale. PostgreSQL stores
  them as the client sent them, and the run does not name them;
- a locale that is not installed, because it measures what `locale -a` lists
  and nothing else.

The run prints the first two beside its result every time, and the fourth
and fifth whenever it measured a locale they apply to. Command 3 sorts
strings you choose inside PostgreSQL, and reaches a rule for a combination of
letters when the strings contain it.

Not tried yet, so a failure there would be new: Python 3.14 on a real
machine, where only a simulation was run, a machine on cgroup v1, and ARM.

## `LC_CTYPE` is compared by step 11 alone

The other six sections are ways a sort-order change can be missed. This one
is a whole category only one step compares.

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

Step 11, given both machines' files, compares what glibc's character rules
answer and names the characters and bytes whose rules change. It says where it
could not look. In an `EUC_JP`, `EUC_KR`, `EUC_CN` or `EUC_TW` database it does
not compare the conversion of a result back to the database's bytes.
Nothing else here compares the character rules, neither a run without the two
files nor steps 2 and 6 to 8, which read the transliterations of `LC_CTYPE`
only for the collating elements they build, which are sort order. No verdict
this project publishes says anything about them in either direction, including
"unchanged".
A 🟢 on the results table is a statement about sort order and nothing else. If
a `lower()`-based index matters to you, run step 11 with both machines' files,
and confirm it the way this project confirms collation — on both machines,
naming the builds.

The remaining categories (`LC_NUMERIC`, `LC_TIME`, `LC_MONETARY`,
`LC_MESSAGES`) are out of scope. They move `to_char()` output and
message text rather than index order. The one exception is the number of
decimals of the `money` type, from `LC_MONETARY`, which changes what a stored
value means: step 11 compares it on both machines.

---

[Documentation index](README.md) · [Scope](scope.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Glossary](glossary.md)
