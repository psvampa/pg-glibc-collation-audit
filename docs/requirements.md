# Requirements and setup

The source audit needs almost nothing. The confirmation step on real nodes
has two traps that will silently give you a wrong answer, and they are the
reason this page exists.

## For the source audit (steps 1 to 5)

`git`, `python3` (stdlib only), `bash`. Nothing else, and no PostgreSQL.

Step 1 clones glibc from `github.com/bminor/glibc`, so it needs outbound
network the first time. The clone is `--filter=blob:none --no-checkout` and
still comes to roughly **370 MB** on disk. Later runs reuse it.

Step 1 also prints the commit id and GPG signature state behind each tag
before diffing anything, and an invalid signature aborts the run. Why that
matters, and what happens when `gpg` is unavailable, is in
[method.md](method.md) under step 1.

## The test suite

```sh
python3 -m unittest discover -s tests -t tests
```

About 17 seconds. It pins the three tags to their commit ids, so a moved tag
reports itself as a moved tag instead of as a change in the results. Two of
its three layers need the glibc clone and skip themselves, with a reason, if
it is absent. CI runs the whole suite on a fresh clone and fails on any skip.

[`tests/README.md`](../tests/README.md) says what it covers and, more
usefully, what it does not.

There is deliberately no CI badge: "tests passing" would be read as "the
audit is correct", and the SQL template and the empirical node confirmation
have no automated coverage at all.

## For the confirmation step

A real PostgreSQL instance on each OS under test, **version 15 or newer** —
[`sql/collation_confirmation_template.sql`](../sql/collation_confirmation_template.sql)
reads `pg_database.datlocprovider` and calls `pg_collation_actual_version()`
and `pg_database_collation_actual_version()`, all of which arrived in 15. The
template's header says what to drop to run it on 13 or 14.

### Install the langpacks before `initdb`

Install the relevant `glibc-langpack-*` packages on each node first. If you
install them *after* `initdb`, the collations will not exist yet and
`CREATE TABLE ... COLLATE "sv_SE.utf8"` fails with `collation ... does not
exist` — which is why the template calls `pg_import_system_collations()`
before anything else.

### Trap 1: restart PostgreSQL after installing a langpack

**Restart the server before re-running `pg_import_system_collations()`.**
Re-running it alone is not enough, and it fails quietly: the function reports
a plausible count and returns success while importing only the locales that
existed when the postmaster started.

Why: the function takes its list from `locale -a`, a fresh subprocess, which
does see the new locales. But it then validates each one with `setlocale()`
inside the backend, and that resolves against the `locale-archive` the
postmaster already has mapped.

Measured on Rocky 8 with PostgreSQL 16.15. Installing `glibc-all-langpacks`
and calling the function without a restart imported **72** collations and
left `sv_SE.utf8` absent. After `systemctl restart postgresql-16` the same
call imported **931** more, for 1007 in total.

### Trap 2: minimal container images install no langpacks at all

This one sits ahead of trap 1. Minimal images ship
`/etc/rpm/macros.image-language-conf` containing `%_install_langs en_US`, so
`dnf install glibc-all-langpacks` reports success and installs nothing beyond
English — `locale -a` stayed at 59 entries. **Remove that file and
reinstall.**

---

[Documentation index](README.md) ·
[Confirming on a real system](confirming-on-a-real-system.md) ·
[Method](method.md)
