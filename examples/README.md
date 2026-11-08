# Example output

Real output, kept so you can see what a run prints before you run one.

## The two audited pairs

The two upgrades this project publishes results for, in two shapes.

### Command 1

`./audit.sh <old> <new>` and nothing else. This is what you get from a
checkout alone, and it is where the summary says what it did not cover
(`NOT RUN`, `NOT CHECKED`) rather than leaving the section out.

| File | |
|---|---|
| [`rhel8-to-rhel9-tags-only.txt`](rhel8-to-rhel9-tags-only.txt) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10-tags-only.txt`](rhel9-to-rhel10-tags-only.txt) | glibc 2.34 to 2.39 |

Raw output, start to finish, with nothing trimmed or annotated.

### Command 1 extended

The same command given one file from each machine, the one
`scripts/locale_order.py --extract` wrote there, so steps 1 and 2 read each
machine's own locale sources and charmaps and step 11 measures both machines
instead of saying `NOT RUN`.

| File | |
|---|---|
| [`rhel8-to-rhel9-audit-output.txt`](rhel8-to-rhel9-audit-output.txt) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10-audit-output.txt`](rhel9-to-rhel10-audit-output.txt) | glibc 2.34 to 2.39 |

Raw output as well, taken on the builds the results page cites.

## Command 2 — the `C.UTF-8` probe

The probe run unedited on both machines. Each file carries one node's output
in full and then the `diff` against the other, so you can see what the probe
prints as well as where the two builds part. Command 1 never measures this
locale's order; Command 1 extended does, in step 11, and its result is in the
extended outputs above.

The RHEL9-against-RHEL10 diff is four lines, and all four are the node's own
glibc version. Everything the probe measured is byte-identical.

| File | |
|---|---|
| [`c-utf8-probe-rhel8-vs-rhel9.txt`](c-utf8-probe-rhel8-vs-rhel9.txt) | RHEL8 against RHEL9 |
| [`c-utf8-probe-rhel9-vs-rhel10.txt`](c-utf8-probe-rhel9-vs-rhel10.txt) | RHEL9 against RHEL10 |

## Command 3 — the SQL step 11 writes

Command 1 extended writes, in its output folder, one SQL file per database
encoding that tests in PostgreSQL the changes step 11 found
(`measured_order_confirm.<pair>.<ENCODING>.sql`). These are the files it
writes for the two pairs. Each runs unchanged on both servers; beside it are
the old and the new server's output, and what `scripts/confirm_sql.py read`
said of the two, the command above what it printed.

| SQL file | Old server | New server | Read |
|---|---|---|---|
| [`rhel8-to-rhel9.UTF8.sql`](rhel8-to-rhel9.UTF8.sql) | [`el8`](rhel8-to-rhel9.UTF8.el8.out) | [`el9`](rhel8-to-rhel9.UTF8.el9.out) | [`read`](rhel8-to-rhel9.UTF8.read.txt) |
| [`rhel8-to-rhel9.LATIN1.sql`](rhel8-to-rhel9.LATIN1.sql) | [`el8`](rhel8-to-rhel9.LATIN1.el8.out) | [`el9`](rhel8-to-rhel9.LATIN1.el9.out) | [`read`](rhel8-to-rhel9.LATIN1.read.txt) |
| [`rhel8-to-rhel9.LATIN9.sql`](rhel8-to-rhel9.LATIN9.sql) | [`el8`](rhel8-to-rhel9.LATIN9.el8.out) | [`el9`](rhel8-to-rhel9.LATIN9.el9.out) | [`read`](rhel8-to-rhel9.LATIN9.read.txt) |
| [`rhel9-to-rhel10.UTF8.sql`](rhel9-to-rhel10.UTF8.sql) | [`el9`](rhel9-to-rhel10.UTF8.el9.out) | [`el10`](rhel9-to-rhel10.UTF8.el10.out) | [`read`](rhel9-to-rhel10.UTF8.read.txt) |

The UTF8 outputs come from the test machines the results page cites, on
PostgreSQL 18.6. The LATIN1 and LATIN9 outputs come from a Rocky Linux 8
machine with PostgreSQL 18.6 and a Rocky Linux 9 machine with Percona's
PostgreSQL 17.11, running the test machines' glibc builds,
`glibc-2.28-251.el8_10.40` and `glibc-2.34-275.el9_8`. Raw output, nothing
trimmed.

Without the two machines' files, start from
[the template](../sql/collation_confirmation_template.sql), which sorts
strings you choose and reads each collation's locale.

---

[Back to the README](../README.md) · [What each command does](../docs/commands.md)
