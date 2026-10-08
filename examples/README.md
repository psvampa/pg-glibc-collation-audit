# Example output

Real output, kept so you can see what a run prints before you run one.

## The two audited pairs

The two upgrades this project publishes results for, in two shapes.

### Command 1

`./audit.sh <old> <new>` and nothing else. This is what you get from a
checkout alone, and it is where the `NOT RUN` blocks appear, each saying
what it did not cover rather than leaving the section out.

| File | |
|---|---|
| [`rhel8-to-rhel9-tags-only.txt`](rhel8-to-rhel9-tags-only.txt) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10-tags-only.txt`](rhel9-to-rhel10-tags-only.txt) | glibc 2.34 to 2.39 |

Raw output, start to finish, with nothing trimmed or annotated.

### Command 1 extended

The same command given one file from each machine, the one
`scripts/locale_order.py --extract` wrote there, so the six node steps run
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

## Command 3 — the worked-example scripts

These are the PostgreSQL scripts [docs/results.md](../docs/results.md) cites
for each pair. Run one on both machines and diff the outputs. They pick
collations by name. For a new confirmation, start from
[the template](../sql/collation_confirmation_template.sql), which reads each
collation's locale instead.

| File | |
|---|---|
| [`rhel8-to-rhel9.sql`](rhel8-to-rhel9.sql) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10.sql`](rhel9-to-rhel10.sql) | glibc 2.34 to 2.39 |

---

[Back to the README](../README.md) · [What each command does](../docs/commands.md)
