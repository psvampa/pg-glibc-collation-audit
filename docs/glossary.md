# Glossary

Terms the rest of these documents use. Every definition here is stated
somewhere in the docs already; this page exists so you do not have to find
the file that happens to define it first.

**`LC_COLLATE` block** — the section of a locale source file, from
`LC_COLLATE` to `END LC_COLLATE`, that defines sort order. The only part of a
locale file that can move it. A locale file often changes without this block
being touched: most of the 283 files that differ between glibc 2.28 and 2.34
changed only `LC_TIME` or `LC_MONETARY`.

**collation template** — one of the three shared locale files most locales
inherit their sort rules from: `iso14651_t1`, `iso14651_t1_common` and
`iso14651_t1_pinyin`. A change to one of these is a change to every locale
that inherits it.

**`copy` graph** — the inheritance graph formed by `copy "other_locale"`
directives. A locale with no tailoring of its own just copies another's
rules, so its real sort order changes whenever the copied locale's does —
with no change to its own file, and so no appearance in a file diff. Step 3
walks this graph.

**blast radius** — how many locales inherit a given file through the `copy`
graph. Step 1 prints it, so a one-line change to a template that 328 locales
inherit cannot read as one line out of 283.

**`localedef`** — glibc's locale compiler. It turns locale source files into
the binary locale the system loads. It is what expands ellipsis ranges and
what normalises codeset spelling, so it is one of the two inputs to sort
order; the locale data is the other.

**build id** — the exact glibc package a machine runs, as `rpm -q glibc`
prints it, such as `glibc-2.28-251.el8_10.40`, with or without the
architecture. Two builds of one glibc version can sort differently, because a
distro patch can sit between them, as one did inside RHEL8 at
`glibc-2.28-93.el8`. Every result here is bound to the build it was taken on.

**ellipsis range** (also *algorithmic range*, *range expansion*) —
range-expansion syntax, `<UAC00>` / `..` / `<UD7A3>`, used instead of an
explicit weight per character. `localedef` expands it at build time, so those
weights are **not** in the locale file. If the expansion logic changes, every
character in the range can get a different weight with zero change to the
locale's own source. Step 4 lists the locales that use these, and every
other locale as well except one whose `LC_COLLATE` is `codepoint_collation`
alone, or only a copy of a byte-order locale, because every character a
locale does not list takes a default weight that a rule in `localedef`'s
code picks, not the file. Steps 1 to 3 cannot clear any of them on data
alone.

**locale, in the Reindex and step 4 lists** — the locale a collation is built
from (`sv_SE`), not one way of writing it. `locale -a`, `pg_collation` and a
database's own locale write one locale many ways (`sv_SE.utf8`,
`sv_SE.iso885915`, `sv_SE.UTF-8`), and glibc builds all of them from one file,
named as the locale is without the part from the dot up to any `@`. Both lists
also hold the aliases glibc's `locale.alias` gives these locales (`swedish`).
`sql/collation_confirmation_template.sql` reads collations the same way. Its
inventories show the locale of each collation they list, and its version query
lists the collations each name in its list reaches.

**hunk** — one contiguous changed block in a `git diff`. Step 5 reports how
many it found and prints them. "Read the hunks" means reading those blocks
of C. Step 5 marks each changed line it counts as code with `>>`.

**`!!`** — a warning printed where a clean-looking result does not cover
something. The summary repeats each one.

**tier** (step 5 only) — one of the three groups of glibc source paths step 5
diffs. Tiers 1 and 2 are curated lists. Tier 3 is *derived*, by walking
glibc's own `#include` graph from the collation entry points, so it grows as
glibc changes and catches files a hand-written list would miss.

**substantive change** (step 5 only) — a hunk that can move a weight, as
opposed to a comment, a licence header, a format-string fix or a type
replacement. Step 5 cannot tell these apart for you; deciding is a manual
step, and it is the one part of the method that requires reading C. See
[limitations.md](limitations.md#step-5-reports-it-does-not-decide).

**role swap** — two locale files exchanging which one holds the ruleset and
which one merely copies it. Both files show a large diff while the effective
sort order does not move. `ber_DZ` and `kab_DZ` over glibc 2.34..2.39 are
this case, not a rule change.

**`codepoint_collation`** — a glibc `LC_COLLATE` keyword that makes a
locale compare strings with strcmp/wcscmp, which is byte order, but only when
it is alone in `LC_COLLATE`. A comment in glibc's own `C` says it works
"in any part of any LC_COLLATE"; glibc's code does not do that, so beside a
`copy` or any sort rule the audit does not clear it. A locale that declares it
alone, or only copies a byte-order locale, is byte order **by construction**,
so no
change to how `localedef` expands ranges can move it. Upstream's `C` declares
it from glibc 2.35. RHEL9 backports that file, RHEL10 is glibc 2.39 and has it
upstream, and RHEL8 ships Red Hat's own ellipsis-based file. It is the whole
reason `C.UTF-8` changed across RHEL8→RHEL9 and cannot change across
RHEL9→RHEL10.

**node-to-node comparison** — comparing two nodes' own locale sources against
each other, with no upstream tag in the middle
(`scripts/diff_node_locales.py`, step 8 of `audit.sh`). Distinct from the
node-versus-tag check, which asks "is the audit reading what this node runs?";
this asks "did what the two nodes run actually change?"

**the file from each machine** (a *node file*) — the one file
`scripts/locale_order.py --extract` writes on a machine, and `audit.sh` reads
with `--old-node` and `--new-node`. What it holds is in
[commands.md](commands.md#the-file-from-each-machine).

**measured order** (step 11) — how a machine's own glibc sorts, asked of the
machine rather than read from source. For every locale `locale -a` lists, it
records where each character the locale can hold sorts, and how each two
neighbours in that order are told apart. It uses the call PostgreSQL makes
under a `libc` collation, so it sees what a source comparison cannot, such as
`ko_KR` between glibc 2.28 and 2.34.

**level** — in most locales glibc compares two strings in stages, the levels
of ISO 14651: first by letter, then by accent, then by case. Step 11 records
at which of them each two neighbours are told apart: as different letters,
like an accent, by case, by something smaller than case, or not at all, when
only the bytes decide. A character glibc ignores at some level counts for
nothing there.

**contraction** — a rule that sorts a combination of letters as one, such as
`ch` sorted as a single letter after `h` in Czech. It belongs to the
combination, not to any one character, so step 11, which measures characters,
cannot see it change.

**inverted positive control** — the one place the rule below runs backwards.
For `C.UTF-8`, agreeing with `LC_ALL=C` byte order is the *corrected*
behaviour, not the usual sign that a locale was not applied. Reading it the
normal way turns the fix into a false alarm and the bug into a clean result.

**positive control** — a check deliberately run against cases whose answer is
already known, to prove the check is capable of returning a non-null answer
at all. A comparison that always reported "identical" would look the same as
one that correctly found no differences.

---

[Documentation index](README.md)
