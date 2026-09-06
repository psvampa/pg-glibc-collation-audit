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
walks this graph. See [method.md](method.md).

**blast radius** — how many locales inherit a given file through the `copy`
graph. Step 1 prints it, so a one-line change to a template that 328 locales
inherit cannot read as one line out of 283.

**`localedef`** — glibc's locale compiler. It turns locale source files into
the binary locale the system loads. It is what expands ellipsis ranges and
what normalises codeset spelling, so it is one of the two inputs to sort
order; the locale data is the other.

**ellipsis range** (also *algorithmic range*, *range expansion*) —
range-expansion syntax, `<UAC00>` / `..` / `<UD7A3>`, used instead of an
explicit weight per character. `localedef` expands it at build time, so those
weights are **not** in the locale file. If the expansion logic changes, every
character in the range can get a different weight with zero change to the
locale's own source. This is what step 4 looks for and why steps 1 to 3
cannot clear those locales on data alone.

**generated locale name** — the name `locale -a` and `pg_collation` actually
show, which is not the source file name. `localedata/SUPPORTED` says
`sv_SE.UTF-8`; the installed locale, `locale -a` and `pg_collation` all say
`sv_SE.utf8`. `COLLATE "sv_SE.UTF-8"` does not exist. Step 3 prints the
generated names — use those.

**hunk** — one contiguous changed block in a `git diff`. Step 5 reports how
many it found and prints them. "Read the hunks" means reading those blocks
of C.

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

**positive control** — a check deliberately run against cases whose answer is
already known, to prove the check is capable of returning a non-null answer
at all. A comparison that always reported "identical" would look the same as
one that correctly found no differences.

---

[Documentation index](README.md)
