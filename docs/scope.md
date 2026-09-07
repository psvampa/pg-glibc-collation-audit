# Scope

## What it audits

**Sort order (`LC_COLLATE`) only.** It says nothing about `LC_CTYPE`
behavior — `upper()`, `lower()`, `initcap()`, character classification, pattern
matching — and nothing about `LC_NUMERIC`, `LC_TIME`, `LC_MONETARY` or
`LC_MESSAGES`.

`LC_CTYPE` is the exclusion that can still cost you an index: a functional index
on `lower(email)` breaks on a glibc upgrade the same way a `COLLATE` index does,
and PostgreSQL has no `collversion` equivalent for ctype to warn you. It is
unmeasured here in **both** directions — see
[limitations.md](limitations.md#lc_ctype-is-not-audited-at-all).

**The `libc` provider only.** It is about glibc, so in PostgreSQL terms that
is the only provider it applies to. ICU collations version their CLDR data
independently of the OS.

**Two upgrade pairs: RHEL8 → RHEL9 and RHEL9 → RHEL10.** Those are what this
project audits and publishes results for. RHEL7 is out of scope — it is years
past end of life, and documenting it bought nothing.

The method itself works on any pair of upstream tags, so equivalents on other
distros (Ubuntu 18.04+, Debian 9+, SLES 15+) behave the same. There used to be
a floor at glibc 2.24 — below it the three collation templates were dropped from
the `copy` graph and the answer collapsed silently — and that was a bug rather
than a limit of the method. It is fixed, and one pair below the old floor
(`glibc-2.12 -> glibc-2.17`) has been measured end to end; that is one pair, not
a claim about every older glibc. See
[limitations.md](limitations.md#below-glibc-224-the-method-rests-on-one-measured-pair).

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
