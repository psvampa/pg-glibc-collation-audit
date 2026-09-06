# Confirming on a real system

A source diff is an argument, not a proof of what actually runs in
production — and it says nothing about your distro's backports. This is the
step that produces evidence rather than inference, and for `C.UTF-8` it is
[not optional](limitations.md#cutf-8-is-invisible-to-a-tag-diff).

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

## Four traps

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

### Two truncated copies agree perfectly

This one belongs to the file comparisons below rather than to the SQL, and it
is the same `docker cp` trap seen from the other side: if the transport lands
three files instead of 355, the comparison reports "0 differ inside
`LC_COLLATE`" over an intersection of three identical files. Comparing a
directory with **itself** does even better — 100% identical, the most
reassuring output the tool can print.

So both file-reading scripts refuse rather than report: they check an absolute
floor on the number of files compared, they resolve both paths and abort if
they name the same directory, and they print a per-side fingerprint of file
names and sizes so two equal fingerprints under two different build ids are
visible. Assert the file count on both sides yourself as well: the scripts print each
side's count, and when you run them directly `--expect-files N` turns your
expectation into a refusal. `./audit.sh` does not take that option, so through
the wrapper the printed counts are the assertion — compare them against
`ls /usr/share/i18n/locales/ | wc -l` on each node.

## The `C.UTF-8` probe

`sql/c_utf8_probe.sql` is a separate file from the template, and separate on
purpose. Run it, unedited, on both nodes and `diff` the two outputs:

```sh
psql -X -f sql/c_utf8_probe.sql > this-node.out
```

Three reasons it is not a section of the template:

- The template is placeholder-driven and *must* be edited before use. This one
  is fully determined — its corpus is every endpoint of every range the
  backported locale actually declares, plus the UTF-8 length boundaries, 41
  values — and must **not** be edited.
- **The positive control inverts here.** Everywhere else, agreement with
  `LC_ALL=C` means the locale was never generated and the comparison proves
  nothing. For `C.UTF-8`, agreement with byte order is the *fix*: it is what
  glibc 2.34 produces and what `codepoint_collation` guarantees from 2.35 on.
  Two contradictory rules in one file get read in the wrong order.
- It must be run **even when the audit flagged nothing**, because nothing in
  steps 1 to 5 can ever flag it.

It is also the one empirical check the langpack trap cannot fake: `C.utf8`
exists on every node whether or not any langpack is installed. It still needs
`pg_import_system_collations()` after a postmaster restart to be in
`pg_collation`, and it refuses to run rather than fall back if it is not.

There is a third way to read "equals byte order = true", and PostgreSQL cannot
rule it out: `varstr_cmp` and the sortsupport comparator both break a `strcoll`
tie with `strcmp`, so a build whose weights are all *tied* is indistinguishable
through SQL from one with correct byte order. Settle it outside PostgreSQL, on
each node:

```sh
python3 -c "import locale; locale.setlocale(locale.LC_COLLATE,'C.utf8'); \
  print(locale.strxfrm(chr(0x10000)).encode().hex(), \
        locale.strxfrm(chr(0x20000)).encode().hex())"
```

Equal keys mean tied weights and every SQL answer above came from the byte
tie-break. Measured 2026-09-06: `f0908080 f0a08080` on RHEL9 and RHEL10 —
the UTF-8 bytes themselves, so the agreement is real — and `ef85b5 f0948b95`
on RHEL8, computed weights bearing no relation to the code point.

The probe runs this itself as query 6b when `python3` is available.

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

### And comparing the two nodes to each other

Everything above compares one node against an upstream tag, which cannot say
anything about a file that is in **no** tag. `scripts/diff_node_locales.py`
takes both sides from the nodes instead, so a backported locale is in both
inputs:

```sh
# tar the sources off BOTH nodes, same caveat as above
python3 scripts/diff_node_locales.py \
    --old-locales-dir ./el8-locales --old-build-id glibc-2.28-251.el8_10.40 \
    --new-locales-dir ./el9-locales --new-build-id glibc-2.34-275.el9_8 \
    --old-tag glibc-2.28 --new-tag glibc-2.34
```

The two tags are optional and worth passing: with them it names which findings
exist at neither, which is the set no tag diff could ever see. It reports every
known backported locale whether or not it differs — a run that says nothing
about `C.UTF-8` and one that cleared it must not look alike — and it reports
locales present on only one node, which is neither a change to a locale nor
something any step covers. Measured 2026-09-06: `en_US@ampm` is gone at RHEL9
and `aa_ER@saaho` at RHEL10.

`./audit.sh` runs it as step 8 when both directories are supplied.

**Identical data is not identical order.** The weights an ellipsis range
expands to are computed by `localedef` at build time, so two nodes can carry
byte-identical files and sort differently — Bug 22668 reordered `ko_KR` from
exactly that. A clean node-to-node result clears the data half and nothing
else.

This compares locale **data**. glibc's collation **code** is step 5's job, and
step 5 reads it between the two upstream tags — that is how Bug 22668, the
change that reorders `ko_KR`, was found. What neither covers is the distro
backporting a code change present in neither tag, and **that** is what the
empirical check on this page closes: it measures the glibc actually installed,
patches and all. The layers cover each other; none of them is optional.

The cheap complement, on each node — and know what it is worth:

```sh
rpm -q --changelog glibc | grep -i collat
```

On RHEL8 it prints `Fix C.UTF-8 locale source ellipsis expressions (#1361965)`,
which is the intra-major change
[limitations.md](limitations.md#cutf-8-is-invisible-to-a-tag-diff) describes.
On RHEL9 and RHEL10 it prints nothing at all, from changelogs of 158 and 112
entries. A signal, not a check.

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
