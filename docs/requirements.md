# Requirements and setup

For the source audit (steps 1 to 5): `git`, `python3` (stdlib only), `bash`.
Nothing else, and no PostgreSQL.

Step 1 prints the commit id behind each tag and the state of its GPG signature
before it diffs anything: the clone is a third-party mirror
(`github.com/bminor/glibc`) and a git tag is a mutable pointer, so "I audited
glibc-2.39" is a weaker claim than it looks. An invalid signature aborts; an
unverifiable one (no `gpg`, or no key for it) is reported and the audit
continues, because refusing to run buys no truth.

There is a test suite — `python3 -m unittest discover -s tests -t tests`, about
17 seconds. It pins the three tags to their commit ids, so a moved tag reports
itself as a moved tag instead of as a change in the results. It needs the glibc clone for two of its three layers and skips them
with a reason if it is absent. [`tests/README.md`](../tests/README.md) says what it
covers and, more usefully, what it does not.

For the confirmation step: a real PostgreSQL instance on each OS under test,
**version 15 or newer** — `sql/collation_confirmation_template.sql` reads
`pg_database.datlocprovider` and calls `pg_collation_actual_version()` and
`pg_database_collation_actual_version()`, all of which arrived in 15. The
template's header says what to drop to run it on 13 or 14.

Install the relevant `glibc-langpack-*` packages on each node first. If you
install them *after* `initdb`, the collations will not exist yet and
`CREATE TABLE ... COLLATE "sv_SE.utf8"` fails with `collation ... does not
exist` — which is why the template calls `pg_import_system_collations()`
before anything else.

**Restart PostgreSQL after installing a langpack, before re-running that
function.** Re-running it alone is not enough, and it fails quietly: the
function reports a plausible count and returns success while importing only
the locales that existed when the postmaster started. `pg_import_system_collations()`
takes its list from `locale -a` — a fresh subprocess, which sees the new
locales — but it then validates each one with `setlocale()` inside the
backend, and that resolves against the `locale-archive` the postmaster
already has mapped. Measured on Rocky 8 with PostgreSQL 16.15, installing
`glibc-all-langpacks` and calling the function without a restart imported
**72** collations and left `sv_SE.utf8` absent; `systemctl restart
postgresql-16` and the same call imported **931** more, for 1007 in total.

Minimal container images add a second trap ahead of that one: they ship
`/etc/rpm/macros.image-language-conf` containing `%_install_langs en_US`, so
`dnf install glibc-all-langpacks` reports success and installs nothing beyond
English — `locale -a` stayed at 59 entries. Remove that file and reinstall.
