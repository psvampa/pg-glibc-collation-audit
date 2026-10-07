#!/usr/bin/env python3
"""
Given the locale files with a REAL (non-inherited) LC_COLLATE change between
two glibc tags (output of filter_lc_collate_changes.py), find every OTHER
locale that inherits its LC_COLLATE via `copy "<name>"` -- directly or
transitively.

This closes a gap in filter_lc_collate_changes.py: that script only flags
files whose OWN LC_COLLATE block changed. A locale that just does
`copy "sv_SE"` and adds no tailoring of its own never shows up in a source
diff (its file didn't change), but its actual sort order changes whenever
sv_SE's does.

Two things this gets right that are easy to get wrong:

  * Arguments may be bare names or full paths. Step 2 prints paths
    (localedata/locales/sv_SE); comparing those against bare names matches
    nothing and reports "0 additionally affected" without any error, which is
    exactly the wrong answer in the reassuring direction. Names are
    normalised, and unknown names are a hard error rather than a silent miss.
  * A locale can carry more than one `copy`. om_ET copies both am_ET and
    om_KE; following only the first hides any change to the second.

Usage:
  python3 resolve_copy_closure.py <tag> <locale> [<locale> ...] [--repo <path>]

Example:
  python3 resolve_copy_closure.py glibc-2.34 or_IN sv_SE
"""
import argparse
import os
import sys

import glibc_locale_data as g


def main(argv):
    ap = argparse.ArgumentParser(
        description="Close a set of changed locales over the LC_COLLATE `copy` graph.")
    ap.add_argument('tag', help="glibc tag whose copy graph to walk")
    ap.add_argument('locales', nargs='+',
                    help="changed locales, as bare names or paths")
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    opts = ap.parse_args(argv)

    repo = g.find_repo(opts.repo)
    g.check_refs(repo, opts.tag)

    changed = {os.path.basename(name.strip()) for name in opts.locales
               if name.strip()}
    graph = g.build_copy_graph(repo, opts.tag)

    known = {os.path.basename(p) for p in g.list_locale_files(repo, opts.tag)}
    unknown = sorted(changed - known)
    if unknown:
        g.die(f"not locale file(s) at {opts.tag}: {', '.join(unknown)}\n"
              f"       (a typo here would otherwise look like "
              f"'nothing else is affected')")
    no_collate = sorted(changed - set(graph))
    if no_collate:
        print(f"note: {', '.join(no_collate)} have no LC_COLLATE block at "
              f"{opts.tag}; nothing can inherit collation from them.",
              file=sys.stderr)

    inherited = g.inherited_from(graph, changed)
    supported = g.supported_map(repo, opts.tag)

    # Which tag's copy graph this is. audit.sh must pass the NEW tag: the old
    # one may lack an inheritance the upgrade adds, and the list below would
    # read just as complete without it. Printed so a test can see the tag.
    print(f"Copy chains read at {opts.tag}")
    print(f"Directly changed (own LC_COLLATE diff): "
          f"{', '.join(sorted(changed))}")
    print(f"Additionally affected via copy-chain inheritance: {len(inherited)}")
    for loc, roots in sorted(inherited.items()):
        shown = ', '.join(f'copy "{t}"' for t in graph.get(loc, [])) or '?'
        print(f"  {loc}: {shown} -> reaches {', '.join(roots)}")

    affected = sorted(changed | set(inherited))
    print()
    print(f"Full affected set ({len(affected)} locale source file(s)): "
          f"{', '.join(affected)}")

    # Locales, not spellings. This used to print the names SUPPORTED generates
    # (sv_SE, sv_SE.utf8) as "the names `locale -a` and pg_collation show",
    # and on the real machines the same locales are also sv_SE.iso88591 and
    # sv_SE.iso885915 (the archive adds `<name>.<codeset>`, RHEL adds the
    # ISO-8859-15 builds), swedish (locale.alias) and, as a database's
    # locale, whatever spelling initdb was given (sv_SE.UTF-8). Measured on
    # RHEL8 -> RHEL9: names that changed order were in no line printed here
    # (backlog 13.1). No list of spellings can be complete; the locale
    # each one is built from is one name, and glibc_locale_data.locale_source
    # is glibc's rule for getting from a spelling to it. The aliases are the
    # names that rule cannot reach, so they are listed with their locale.
    all_aliases = g.locale_aliases(repo, opts.tag)
    aliases = g.aliases_of(set(affected), all_aliases)
    not_ascii = g.non_ascii_alias_targets(set(affected), all_aliases)
    unbuilt = [loc for loc in affected if not supported.get(loc)]
    print()
    print("These are locales, not spellings: a collation or a database uses "
          "one when its\nlocale, without the part from the dot up to any @, "
          "is one of them\n(sv_SE.UTF-8, sv_SE.utf8 and sv_SE.iso885915 are "
          "all sv_SE).")
    if aliases:
        print(f"Also named by glibc's locale.alias: "
              f"{', '.join(f'{a} ({s})' for a, s in sorted(aliases.items()))}")
    if not_ascii:
        print(f"Not listed: an alias of {', '.join(not_ascii)} whose name is "
              f"not ASCII; PostgreSQL never imports such a name as a "
              f"collation.")
    if unbuilt:
        print(f"Not listed in localedata/SUPPORTED (not built by default, so "
              f"normally absent from `locale -a`): {', '.join(unbuilt)}")
    # Written unconditionally: audit.sh reads the result of THIS run, and
    # inferring an empty result from a missing file cannot distinguish "no
    # inheritance to add" from "the step never ran". The announce stays gated
    # on the list being longer than what was already printed inline, so
    # terminal output for a hand-run audit is unchanged.
    path = g.write_list('step3_affected_locales.txt',
                        sorted(set(affected) | set(aliases)))
    if len(affected) > len(changed):
        print(f"\nFull list also written to {path}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
