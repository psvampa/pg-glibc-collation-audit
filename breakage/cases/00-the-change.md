# The change, in one query

glibc 2.28 sorts Swedish `W` as a variant of `V`. glibc 2.34 makes it a letter of its own,
after `V`. Seven words are enough to see it, and that single movement is the origin of every
case that follows.

```text
Node0 - glibc 2.28 (RHEL8)                   | Node1 - glibc 2.34 (RHEL9)
---------------------------------------------+---------------------------------------------
[postgres@node0 ~]$ getconf GNU_LIBC_VERSION | [postgres@node1 ~]$ getconf GNU_LIBC_VERSION
glibc 2.28                                   | glibc 2.34
[postgres@node0 ~]$ psql --pset pager        | [postgres@node1 ~]$ psql --pset pager
psql (18.6)                                  | psql (18.6)
Type "help" for help.                        | Type "help" for help.
                                             |
postgres=# SELECT string_agg(w, ' ' ORDER BY w COLLATE "sv_SE.utf8") AS sv_se FROM readable;
            sv_se                            |             sv_se
------------------------------               | ------------------------------
 va wa Vasa Wasa vind wind vz                |  va Vasa vind vz wa Wasa wind
(1 row)                                      | (1 row)
                                             |
postgres=# SELECT string_agg(w, ' ' ORDER BY w COLLATE "C") AS byte_order FROM readable;
          byte_order                         |           byte_order
------------------------------               | ------------------------------
 Vasa Wasa va vind vz wa wind                |  Vasa Wasa va vind vz wa wind
(1 row)                                      | (1 row)
                                             |
postgres=#
```

---

Both columns are real `psql` output, captured on the two nodes described in
[environment.md](../environment.md). The version mismatch WARNING that PostgreSQL raises
once per session on Node1 is omitted, so the two columns line up. Commands are
identical on both nodes, so each one is printed once, full width, above the two answers.
