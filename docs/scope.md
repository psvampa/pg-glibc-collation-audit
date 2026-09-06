# Scope

This audits **sort order (`LC_COLLATE`) only**. It says nothing about
`LC_CTYPE` behavior (`upper()`, `lower()`, character classification, pattern
matching).

It is also about glibc, so in PostgreSQL terms it applies to the **`libc`
provider** and nothing else. ICU collations version their CLDR data
independently of the OS. The `builtin` provider (PG 17+) implements `C`,
`C.UTF-8` and `PG_UNICODE_FAST` inside PostgreSQL, in code-point order, with
no call into glibc — so `initdb --locale-provider=builtin --locale=C.UTF-8` is
immune to everything this tool looks for, and is the mitigation if the
`C.UTF-8` limitation in [limitations.md](limitations.md) is what you are
worried about. Note that `initdb` still defaults to `libc` in PG 16, 17 and
18, so you have to ask for `builtin`.

Within `LC_COLLATE`, steps 4 and 5 are what keep "a clean diff proves nothing
changed" honest: a locale relying on range expansion needs an empirical check
whenever step 5 reports a change in the expansion logic. See
[limitations.md](limitations.md) for what this method structurally cannot see.
