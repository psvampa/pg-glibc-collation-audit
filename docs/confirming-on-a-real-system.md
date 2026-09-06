# Confirming on a real system

A source diff is an argument, not a proof of what actually runs in
production — and it says nothing about your distro's backports. This is the
step that produces evidence rather than inference, and for `C.UTF-8` it is
[not optional](limitations.md#cutf-8-cannot-be-audited-by-this-method).

Before you run it, check the setup traps in
[requirements.md](requirements.md): it needs PostgreSQL 15 or newer, and a
langpack installed in the wrong order — or a container image that silently
installs none — will hand you a clean result that means nothing.

## What the template does

[`sql/collation_confirmation_template.sql`](../sql/collation_confirmation_template.sql)
is a template to run on both the old and new OS/glibc, side by side: import
system collations, build a real index on a column using the flagged locale,
and compare `ORDER BY` output between the two.

```sh
psql -f sql/collation_confirmation_template.sql   # edit placeholders first
```

## Which locales to run it for

Every locale steps 1 to 3 flagged, and — if step 5 found a
[substantive code change](glossary.md) — every locale step 4 flagged too,
regardless of whether it showed up in steps 1 to 3.

## Three traps

Each of these makes a comparison agree with itself while proving nothing.

### The locale must actually be generated on both boxes

Check `locale -a` first. If a locale is not generated, `sort` and PostgreSQL
silently fall back to `C` — and two boxes both missing it will agree with
each other perfectly. Use the [generated names](glossary.md) step 3 prints
(`sv_SE.utf8`), not the source file names.

### Feed both sides byte-identical input

glibc's `strcoll` really does report distinct strings as equal. Measured on
RHEL8/RHEL9: about 0.1% of random string pairs under
`sv_SE`/`en_US`/`de_DE`, and about 10% under `ko_KR`. `sort(1)` resolves
those tied lines by **input order**, so two nodes given differently-ordered
input can differ for reasons that have nothing to do with glibc.

PostgreSQL is not exposed to this — `ORDER BY` on a libc collation is always
a total, plan-independent order. Three things have to hold for that, and all
three do:

- `varstr_cmp` and the sortsupport comparator both break `strcoll` ties with
  `strcmp` (`src/backend/utils/adt/varlena.c`, unchanged in substance from
  PG 13 to 18).
- Abbreviated keys cannot bypass the tie-break. A zero from the abbreviated
  comparator means "indeterminate", not "equal", and forces the full
  comparator.
- Nondeterministic collations are the one case where the tie-break is
  skipped, and they are rejected for every provider except ICU.

The `COLLATE "C"` in the template is a no-op kept to state intent.

### The database default is a separate question

It is also the answer for most columns. A `text` column with no explicit `COLLATE` does not carry a libc collation.
It carries OID 100, `default` — a pointer resolved at runtime from
`pg_database`. In a typical database that is most text columns; in a
container it is usually all of them, because `initdb` inherits the locale
from the environment and minimal images ship `LANG=C.UTF-8`.

The template asks `pg_database` first and says plainly whether the database
is exposed, then treats default-collated columns as in scope when it is.

`C.UTF-8` under the `libc` provider **is** exposed: PostgreSQL special-cases
only the literal strings `C` and `POSIX` to byte comparison, so libc
`C.UTF-8` goes through `strcoll` like any other locale. The `builtin`
provider's `C.UTF-8` (PG 17+) is a different implementation and is not
exposed — see [scope.md](scope.md).

## Checking the distro's own patches

The template answers what the *running* system sorts. A second question sits
beside it: do the distro's patches to glibc differ from the upstream source the
audit reads at all? `scripts/diff_distro_locales.py` answers that, and unlike
the template it needs no database:

```sh
# on the node
dnf install -y glibc-locale-source

# copy the sources off it -- tar, NOT `docker cp`, whose target /tmp is a
# separate mount in these containers, so the copy silently does nothing and the
# comparison then reports a clean zero over an empty directory
docker exec <container> tar -cf - -C /usr/share/i18n/locales . | tar -xf - -C ./node-locales

python3 scripts/diff_distro_locales.py glibc-2.34 \
    --locales-dir ./node-locales --build-id "$(rpm -q glibc)"
```

`--build-id` is required: a result is bound to the build it was taken on, and
nothing in the directory carries a version. The script refuses a directory too
small to be a real copy, because a partial copy reports "0 differ inside
`LC_COLLATE`" and that is indistinguishable from a clean result.

`./audit.sh` runs the same check on both sides of a pair in one command, given
each node's sources and its build id:

```sh
./audit.sh glibc-2.28 glibc-2.34 \
  --old-locales-dir ./el8-locales --old-build-id glibc-2.28-251.el8_10.40 \
  --new-locales-dir ./el9-locales --new-build-id glibc-2.34-275.el9_8
```

It is optional there for the same reason it is a separate page here: steps 1
to 5 read the glibc clone alone, and this needs files off a real node. A
`--*-locales-dir` without its matching `--*-build-id` is refused rather than
half-used, because a result nobody can bind to a build cannot be cited.

This compares locale **data**. glibc's collation **code** is step 5's job, and
step 5 reads it between the two upstream tags — that is how Bug 22668, the
change that reorders `ko_KR`, was found. What neither covers is the distro
backporting a code change present in neither tag, and **that** is what the
empirical check on this page closes: it measures the glibc actually installed,
patches and all. The three layers cover each other; none of them is optional.

## What else the template reports

Besides the index inventory: **text partition keys**, every column carrying
an affected collation, and a manual-review list of `CHECK`/`EXCLUDE`
constraints.

The partition key matters most. If the collation changes, rows can belong in
a different partition than the one they are stored in, and no amount of
reindexing fixes that — the rows have to be moved. **A `REINDEX` list is not
the whole answer.**

---

[Documentation index](README.md) · [Requirements and setup](requirements.md) ·
[Known limitations](limitations.md) · [Glossary](glossary.md)
