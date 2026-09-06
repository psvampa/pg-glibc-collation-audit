# Scope

## What it audits

**Sort order (`LC_COLLATE`) only.** It says nothing about `LC_CTYPE`
behavior: `upper()`, `lower()`, character classification, pattern matching.

**The `libc` provider only.** It is about glibc, so in PostgreSQL terms that
is the only provider it applies to. ICU collations version their CLDR data
independently of the OS.

**Two upgrade pairs: RHEL8 → RHEL9 and RHEL9 → RHEL10.** Those are what this
project audits and publishes results for. RHEL7 is out of scope — it is years
past end of life, and documenting it bought nothing.

The method itself works on any pair where both sides are glibc 2.24 or newer,
so equivalents on other distros (Ubuntu 18.04+, Debian 9+, SLES 15+) behave the
same. Below 2.24 it breaks silently and there is no guard. See
[limitations.md](limitations.md#below-glibc-224-the-method-breaks-silently).

## The `builtin` provider is the way out

The `builtin` provider (PG 17+) implements `C`, `C.UTF-8` and
`PG_UNICODE_FAST` inside PostgreSQL, in code-point order, with no call into
glibc. So:

```sh
initdb --locale-provider=builtin --locale=C.UTF-8
```

is immune to everything this tool looks for, and is the mitigation if
[the `C.UTF-8` limitation](limitations.md#cutf-8-is-invisible-to-a-tag-diff)
is what you are worried about.

Note that `initdb` still defaults to `libc` in PG 16, 17 and 18, so you have
to ask for `builtin`.

---

[Documentation index](README.md) · [Known limitations](limitations.md) ·
[The method](method.md)
