#!/usr/bin/env python3
"""
Find locales whose LC_COLLATE relies on range-expansion (ellipsis) syntax
instead of listing every character's collation weight explicitly, then close
that set over the `copy` graph.

Why this matters: a source diff (audit-locale-diff.sh +
filter_lc_collate_changes.py) proves that a locale's sort order didn't change
ONLY if every character's weight is spelled out in the data file itself. A
range such as

    <U4E00> <U4E00>;IGNORE;IGNORE;IGNORE
    .. ..;IGNORE;IGNORE;IGNORE
    <U9FA5> <U9FA5>;IGNORE;IGNORE;IGNORE

or, far more commonly, inline on one line:

    collating-symbol <SAC00>..<SD7A3>  % Hangul syllables (weights constructed)
    collating-symbol <RFB40>..<RFB41>  % first element of Han computed weights

is not expanded in the data file -- glibc's locale compiler (localedef)
expands it algorithmically at build time. If that expansion logic changes
between two glibc releases, every character in the range can get a different
weight with ZERO change to the locale's own source file, and a diff-based
audit would wrongly report "unaffected". This is not hypothetical: glibc 2.34
took commit 82292c99b2 ("LC_COLLATE: Fix last character ellipsis handling",
Bug 22668), which is why ko_KR sorts differently on RHEL9 than on RHEL8
despite localedata/locales/ko_KR being byte-identical between the two.

The inline form is the one that matters most and the one this script used to
miss: it lives in iso14651_t1_common, which carries the constructed Hangul and
Han weights and is reached by 333 of the 342 locales that define LC_COLLATE.
Matching only a line-leading ellipsis cleared zh_CN, cmn_TW,
iso14651_t1_pinyin and cns11643_stroke -- a false "unaffected" for the exact
class of locale this script exists to catch.

Use diff_collation_code.py to check whether the expansion logic actually
changed for the version pair you care about. If it did, every locale printed
here needs an empirical sort-order test regardless of what the source diff
says.

Usage:
  python3 flag_algorithmic_ranges.py <tag> [--repo <path>]

Example:
  python3 flag_algorithmic_ranges.py glibc-2.34
"""
import argparse
import os
import sys

import glibc_locale_data as g


def main(argv):
    ap = argparse.ArgumentParser(
        description="Flag locales whose LC_COLLATE uses algorithmic ellipsis "
                    "ranges, plus everything inheriting them.")
    ap.add_argument('tag', help="glibc tag to scan, e.g. glibc-2.34")
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    opts = ap.parse_args(argv)
    tag = opts.tag

    repo = g.find_repo(opts.repo)
    g.check_refs(repo, tag)

    paths = g.list_locale_files(repo, tag)
    contents, missing = g.read_blobs(repo, tag, paths)
    if missing:
        g.die(f"{len(missing)} file(s) listed at {tag} could not be read: "
              f"{', '.join(sorted(missing)[:5])}")

    flagged, with_collate = {}, 0
    for path, text in contents.items():
        block = g.collate_block(text)
        if block is None:
            continue
        with_collate += 1
        hits = g.ellipsis_hits(block, g.comment_char(text))
        if hits:
            flagged[os.path.basename(path)] = hits

    print(f"Files at {tag}: {len(contents)}, of which {with_collate} define "
          f"LC_COLLATE")
    print(f"Locales whose LC_COLLATE uses ellipsis (algorithmic) ranges: "
          f"{len(flagged)}")
    for name in sorted(flagged):
        print(f"  {name}")
        for hit in flagged[name][:3]:
            print(f"      {hit}")
        if len(flagged[name]) > 3:
            print(f"      ... and {len(flagged[name]) - 3} more")

    if not flagged:
        print("\nNo locale uses ellipsis ranges at this tag; steps 1-3 are "
              "sufficient.")
        return 0

    # A flagged template is only actionable together with everything that
    # inherits it: iso14651_t1 carries the Han range and is copied, directly or
    # transitively, by most of the corpus.
    graph = g.build_copy_graph(repo, tag)
    inherited = g.inherited_from(graph, set(flagged))
    supported = g.supported_map(repo, tag)

    print(f"\nAdditionally exposed via `copy` inheritance: {len(inherited)}")
    # A locale can reach more than one flagged template, so it can appear under
    # more than one heading here; the exposed set below counts it once.
    by_root = {}
    for loc, roots in inherited.items():
        for via in roots:
            by_root.setdefault(via, []).append(loc)
    for via in sorted(by_root):
        locs = sorted(by_root[via])
        print(f"  via {via}: {len(locs)} locale(s)")
        print(f"      {', '.join(locs[:12])}"
              f"{', ...' if len(locs) > 12 else ''}")

    exposed = sorted(set(flagged) | set(inherited))
    generated = sorted({n for loc in exposed for n in supported.get(loc, [])})
    unbuilt = [loc for loc in exposed if not supported.get(loc)]
    print(f"\nFull set needing empirical confirmation: {len(exposed)} locale "
          f"source file(s), {len(generated)} generated locale name(s) per "
          f"localedata/SUPPORTED")
    if generated:
        print(f"  e.g. {', '.join(generated[:8])}, ...")
    if unbuilt:
        print(f"  not in SUPPORTED (templates, not built by default): "
              f"{', '.join(unbuilt)}")
    # Same rule as step 3: never write a list narrower than what was reported.
    out_path = g.write_list('step4_exposed_locales.txt', generated or exposed)
    print(f"  full list: {out_path}")

    print()
    print("These cannot be cleared by a source diff alone. Run")
    print(f"  python3 diff_collation_code.py <old_tag> {tag}")
    print("to see whether localedef's expansion logic changed between your two")
    print("versions; if it did, test these empirically before trusting a")
    print("'not flagged' result from steps 1-3.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
