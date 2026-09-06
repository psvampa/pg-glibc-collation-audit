# Scope

## What it audits

**Sort order (`LC_COLLATE`) only.** It says nothing about `LC_CTYPE`
behavior: `upper()`, `lower()`, character classification, pattern matching.

**The `libc` provider only.** It is about glibc, so in PostgreSQL terms that
is the only provider it applies to. ICU collations version their CLDR data
independently of the OS.

**Upgrades landing on glibc 2.24 or newer** — RHEL 8+, Ubuntu 18.04+,
Debian 9+, SLES 15+. Note the direction: auditing *from* an older system is
fine, so `RHEL 7 -> RHEL 8` is correct. What is out of scope is auditing
*towards* RHEL 7 or older, and there is no guard — outside that range the
tool answers confidently and wrongly. See
[limitations.md](limitations.md#the-destination-must-be-glibc-224-or-newer).

## The `builtin` provider is the way out

The `builtin` provider (PG 17+) implements `C`, `C.UTF-8` and
`PG_UNICODE_FAST` inside PostgreSQL, in code-point order, with no call into
glibc. So:

```sh
initdb --locale-provider=builtin --locale=C.UTF-8
```

is immune to everything this tool looks for, and is the mitigation if
[the `C.UTF-8` limitation](limitations.md#cutf-8-cannot-be-audited-by-this-method)
is what you are worried about.

Note that `initdb` still defaults to `libc` in PG 16, 17 and 18, so you have
to ask for `builtin`.

---

[Documentation index](README.md) · [Known limitations](limitations.md) ·
[The method](method.md)
