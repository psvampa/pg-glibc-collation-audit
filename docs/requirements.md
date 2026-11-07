# Requirements and setup

What each run needs, and three setup traps that give a wrong answer without
saying so.

## For the source audit (steps 1 and 2)

`git`, `python3` 3.7 or newer (stdlib only), `bash`. Nothing else, and no
PostgreSQL.

Step 1 clones glibc from `github.com/bminor/glibc`, so it needs outbound
network the first time. The clone is `--filter=blob:none --no-checkout` and
still comes to roughly **370 MB** on disk. Later runs reuse it.

A side named as a build (`glibc-2.34-275.el9_8`), or the build a machine's
file names (`--old-node`, `--new-node`), needs outbound network to
Rocky Linux's mirror the first time that build is read, and a side given
as `latest-el<N>` on every run, to read which build is the newest
(`PG_GLIBC_BUILD_MIRROR` names another), and its files are kept in
`scripts/build-sources-cache/` (`PG_GLIBC_BUILD_SOURCES_CACHE` names
another). Checking the charmaps of an el9 or el10 package needs Python 3.14
or a `zstd` program; without one the run says the charmaps were not
checked.

Steps 1 and 2 work out how `localedef` reads every locale file of the
versions they look at; step 2 looks at both. The first run for a version
does that with one process per processor. `PG_GLIBC_READER_JOBS=1` keeps it
to one process. The answers are kept in `scripts/glibc-reader-cache/`, and
later runs read them instead.

Step 1 also prints the commit id and GPG signature state behind each tag
before diffing anything, and an invalid signature aborts the run. Without
`gpg`, or without the signer's key, the signature is reported as not checked
and the run continues.

## For the extended run (1.b)

### On each machine

- **Python 3.6 or newer.** On RHEL8 `python3` comes in a package of its own
  and may be missing; `/usr/libexec/platform-python`, which `dnf` itself runs
  on, is always there and is Python 3.6. Measured on RHEL 8.10 on EC2, where
  `python3` was not installed.
- **`rpm`.** The extraction reads the glibc build with `rpm -q glibc`, and
  `audit.sh` refuses a file that names none, so the machine has to be one
  that installs glibc from RPM.
- **`glibc-locale-source` at the exact build the machine runs**, for steps 6
  to 8, and for step 11's check of what it measured against the machine's
  character sets (trap 3, below). Without it the machine still gives a file,
  with the measurement and the build, and the summary says which checks did
  not run and why.
- **The language packs of the locales you care about.** The measurement
  covers every locale `locale -a` lists and nothing else. A locale a database
  uses is installed already. On a stock RHEL image on EC2 that was English
  alone, 40 locales on RHEL 8.10 and 38 on RHEL 9.8.
- **No PostgreSQL and no root.** Measured as an ordinary user, the order is
  the same. Run as root, or as the user PostgreSQL runs as where /proc does
  not hide other users' processes (`hidepid`), the measurement also sees
  whether PostgreSQL runs the C library it measured; otherwise it says that
  was not checked.

`sudo` over `ssh` needs a terminal to ask for the password, which is what
`ssh -t` gives it. Without `-t` it stops at once with `sudo: a terminal is
required to read the password`, and nothing is installed.

The measurement cannot take `-t`, because the script reaches the machine on
its standard input. So the README runs it with `sudo -n`, which never asks:
without a rule that lets you run as `postgres` with no password it stops at
once with `sudo: a password is required`, and on a machine with no
`postgres` user it says the user is unknown. Either way it writes an
empty file, which `audit.sh` refuses. Run the line without
`sudo -n -u postgres` then.

### Memory and time

The script starts one measuring process per CPU, fewer when half the free
memory does not hold them, but always one, which can take up to 850 MiB, and
never more than the machine's cgroup allows. It also runs at low priority.

How long it takes depends on how many locales there are and how many are
measured at once. On the smallest EC2 machine each locale took about ten
seconds. The script prints how far it has got every tenth of the way.

`dnf` needs memory too. On a machine with 0.7 GB, the kernel killed it at
516 MB and the package was not installed; free some memory, or add swap, for
the install.

### The machine that compares

What the source audit needs, above. It reads the two files and needs no
access to the machines. Comparing on a RHEL8 machine itself is not tested:
`python3` may be missing there, and it is 3.6 when present.

## The test suite

What it covers, and what it does not, is in
[tests/README.md](../tests/README.md).

## For the confirmation step

A real PostgreSQL instance on each OS under test, **version 15 or newer** —
[`sql/collation_confirmation_template.sql`](../sql/collation_confirmation_template.sql)
reads `pg_database.datlocprovider` and `datcollversion` and calls
`pg_database_collation_actual_version()`, all of which arrived in 15.
`pg_collation_actual_version()` itself is older: it exists since PostgreSQL 10
and has reported a version for `libc` collations since 13, so the
named-collation mismatch check works on 13 and 14. The template's header says
what to drop to run it there.

### Install the langpacks before `initdb`

Install the relevant `glibc-langpack-*` packages on each node first. If you
install them *after* `initdb`, the collations will not exist yet and
`CREATE TABLE ... COLLATE "sv_SE.utf8"` fails with `collation ... does not
exist` — which is why the template calls `pg_import_system_collations()`
before anything else.

### Trap 1: restart PostgreSQL after installing glibc-all-langpacks

**Restart the server before re-running `pg_import_system_collations()`.**
Re-running it alone is not enough, and it fails quietly: the function returns
success while the locales that package added stay missing.

Why: the function takes its list from `locale -a`, a fresh subprocess, which
does see the new locales. But it then checks each one inside the backend, and
glibc opens the `locale-archive` at most once per process. Every backend
inherits the postmaster's attempt.

A single `glibc-langpack-<xx>` installs its locales outside the archive and
needs no restart.

### Trap 2: minimal container images install no langpacks at all

This one sits ahead of trap 1. Minimal images ship
`/etc/rpm/macros.image-language-conf` containing `%_install_langs en_US`, so
`dnf install glibc-all-langpacks` reports success and installs nothing beyond
English — `locale -a` stayed at 59 entries. **Remove that file and
reinstall.**

### Trap 3: installing packages can move your glibc build

`glibc-all-langpacks` and `glibc-locale-source` are version-locked to `glibc`
itself, so a plain `dnf install` pulls the newest build of all of them. On the
node above that upgraded glibc from `2.28-236.el8_9.7` to
`2.28-251.el8_10.40` as a side effect of installing langpacks, and on RHEL
8.10 on EC2 installing `glibc-locale-source` planned to take glibc from
`2.28-251.el8_10.34` to `2.28-251.el8_10.40`.

For the sources, name the exact build, as the README does:
`glibc-locale-source-$(rpm -q --qf "%{VERSION}-%{RELEASE}" glibc)`. `dnf` then
installs that build's sources and nothing else, and if the repository no
longer has them it says `No match for argument` and changes nothing. Measured
on RHEL 8.10 and 9.8 on EC2.

For language packs, re-check `rpm -q glibc` afterwards: a measurement is bound
to the build it ran on, and this is a way to change that build without meaning
to.

## Which nodes need `glibc-locale-source`

**Both of them**, for steps 6 to 8. Steps 6 and 7 hold a machine's locale
sources against the upstream version its distro started from; step 8 holds the
two machines' against each other. A machine without the package still gives a
file, and those checks then say `NOT RUN` with the reason. Step 11 runs
without it, and then says it did not check the strings it measured against
that machine's character sets, which a locale its sort table decides does not
need.

`sql/c_utf8_probe.sql` needs neither langpacks nor that package — `C.utf8`
exists on every node regardless — and it refuses to run if the collation is
missing.

---

[Documentation index](README.md) ·
[Confirming on a real system](confirming-on-a-real-system.md)
