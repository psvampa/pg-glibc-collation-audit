# Example output

Real output, kept so you can see what a run prints before you run one.

## The two audited pairs

The two upgrades this project publishes results for, in two shapes.

### Command 1, as the README gives it

`./audit.sh <old> <new>` and nothing else. This is what you get from a
checkout alone, and it is where the two `NOT RUN` blocks appear, each saying
what it did not cover rather than leaving the section out.

| File | |
|---|---|
| [`rhel8-to-rhel9-tags-only.txt`](rhel8-to-rhel9-tags-only.txt) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10-tags-only.txt`](rhel9-to-rhel10-tags-only.txt) | glibc 2.34 to 2.39 |

Raw output, start to finish, with nothing trimmed or annotated.

### The extended run, and the evidence behind the verdicts

Older and richer files, and not a transcript of one command. Each opens with
the five steps run one script at a time, carries the measurements taken on
real nodes for that pair, and closes with `./audit.sh` given both machines'
locale files — steps 6 to 10 and the full summary.

| File | |
|---|---|
| [`rhel8-to-rhel9-audit-output.txt`](rhel8-to-rhel9-audit-output.txt) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10-audit-output.txt`](rhel9-to-rhel10-audit-output.txt) | glibc 2.34 to 2.39 |

Read these for what was measured and why a verdict stands. Read the two above
for what the command prints.

## Two pairs that are not audited results

Kept as evidence about the method, not as verdicts. Each says so in its first
lines.

| File | Why it is here |
|---|---|
| [`skipping-a-release-2.28-to-2.39.txt`](skipping-a-release-2.28-to-2.39.txt) | the direct jump over a version this project has audited on its own, so the two readings can be compared |
| [`below-the-floor-2.12-to-2.17.txt`](below-the-floor-2.12-to-2.17.txt) | a pair below the old glibc 2.24 floor, which is what showed that floor was a bug |

## Command 2 — the `C.UTF-8` probe

The probe run unedited on both machines, the two outputs diffed. This is the
locale command 1 cannot reach when its file is in neither version.

| File | |
|---|---|
| [`c-utf8-probe-rhel8-vs-rhel9.txt`](c-utf8-probe-rhel8-vs-rhel9.txt) | RHEL8 against RHEL9 |
| [`c-utf8-probe-rhel9-vs-rhel10.txt`](c-utf8-probe-rhel9-vs-rhel10.txt) | RHEL9 against RHEL10 |

## Command 3 — the confirmation template, filled in

The template with its placeholders replaced for one pair, ready to run on both
machines and diff.

| File | |
|---|---|
| [`rhel8-to-rhel9.sql`](rhel8-to-rhel9.sql) | glibc 2.28 to 2.34 |
| [`rhel9-to-rhel10.sql`](rhel9-to-rhel10.sql) | glibc 2.34 to 2.39 |

---

[Back to the README](../README.md) · [What each command does](../docs/commands.md)
