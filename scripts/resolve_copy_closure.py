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

    print(f"Directly changed (own LC_COLLATE diff): {sorted(changed)}")
    print(f"Additionally affected via copy-chain inheritance: {len(inherited)}")
    for loc, via in sorted(inherited.items()):
        shown = ', '.join(f'copy "{t}"' for t in graph.get(loc, [])) or '?'
        print(f"  {loc}: {shown} -> reaches {via}")

    affected = sorted(changed | set(inherited))
    print()
    print(f"Full affected set ({len(affected)} locale source file(s)): {affected}")

    # The audit works in source file names, but an admin and pg_collation see
    # generated names with codesets. Map one to the other.
    generated = sorted({n for loc in affected for n in supported.get(loc, [])})
    unbuilt = [loc for loc in affected if not supported.get(loc)]
    print()
    if generated:
        print(f"Generated locales per localedata/SUPPORTED -- these are the "
              f"names `locale -a` and pg_collation show ({len(generated)}):")
        for name in generated:
            print(f"  {name}")
    if unbuilt:
        print(f"Not listed in localedata/SUPPORTED (not built by default, so "
              f"normally absent from `locale -a`): {', '.join(unbuilt)}")
    if len(affected) > len(changed):
        path = g.write_list('step3_affected_locales.txt', generated or affected)
        print(f"\nFull list also written to {path}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
