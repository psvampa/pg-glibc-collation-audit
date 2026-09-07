#!/usr/bin/env python3
"""
Compare TWO NODES' locale sources against each other, with no upstream tag in
the middle.

diff_distro_locales.py answers "is the audit reading what this node runs?" --
one node against the tag the audit diffed, where the node is authoritative and
upstream is only the reference. This answers a different question: "did what
the two nodes run actually change?" Both sides are authoritative and the
verdict is about the delta between them.

Why that is worth a separate comparison: a locale the DISTRO backports exists
in neither tag, so no tag-to-tag diff can see it however the tags are chosen.
`localedata/locales/C` is the case that matters -- upstream has it only from
glibc 2.35, RHEL8 and RHEL9 predate that and backport it, and C.UTF-8's order
demonstrably differs between them. That file is on both nodes and in neither
tag, so comparing the nodes to each other is the only source-level evidence
about it that exists. (RHEL10 is glibc 2.39 and has upstream's copy, which is
why the RHEL9 -> RHEL10 pair is not blind in the same way.)

What this does NOT prove: that the ORDER is unchanged. Wherever a locale defines
its collation with ellipsis ranges -- as RHEL8's C does, with six of them --
localedef computes those weights at build time, and Bug 22668 reordered ko_KR
from a byte-identical data file. Data equality clears the data half only. See
the closing warnings, and sql/c_utf8_probe.sql for the half this cannot answer.

It takes two directories rather than reaching into two nodes, so the transport
is the caller's problem and the comparison is testable without a node. It does
not take them on trust: two equally truncated directories agree perfectly, and
so does one directory compared with itself.

Usage:
  python3 diff_node_locales.py --old-locales-dir DIR --old-build-id NVR \\
                               --new-locales-dir DIR --new-build-id NVR \\
                               [--old-tag TAG --new-tag TAG] \\
                               [--expect-files N] [--min-files N] [--repo PATH]

Example:
  python3 diff_node_locales.py \\
      --old-locales-dir ./el8-locales --old-build-id glibc-2.28-251.el8_10.40 \\
      --new-locales-dir ./el9-locales --new-build-id glibc-2.34-275.el9_8 \\
      --old-tag glibc-2.28 --new-tag glibc-2.34
"""
import argparse
import collections
import os
import sys

import diff_distro_locales as dd
import glibc_locale_data as g
# One definition, imported rather than copied. A locale listed as backported in
# one script and not the other is a locale that gets reported by one of them and
# silently dropped by the other -- and this whole script exists because of the
# one entry in that dict.
from filter_lc_collate_changes import KNOWN_BACKPORTED

Side = collections.namedtuple(
    'Side', 'label root build_id names skipped manifest')

STYLE_TEXT = {
    'ellipsis': 'ellipsis ranges -- weights computed by localedef',
    'codepoint': 'codepoint_collation -- byte order by construction',
    'copy-only': 'pure copy; inherits its order',
    'explicit': 'weights spelled out in the file',
    'none': 'no LC_COLLATE block',
}


def resolve_side(label, root, build_id):
    """One side of the comparison, with everything needed to report on it.

    Every skipped directory entry is carried along rather than dropped: an
    unreported skip shrinks the corpus, and a smaller corpus produces a
    cleaner-looking answer.
    """
    if not os.path.isdir(root):
        g.die(f"--{label}-locales-dir {root} is not a directory")
    names, skipped = dd.node_entries(root)
    return Side(label=label, root=root, build_id=build_id, names=names,
                skipped=skipped, manifest=dd.tree_manifest(root, names))


def resolve_sides(opts):
    """Both sides, refusing the comparisons that cannot mean anything."""
    old = resolve_side('old', opts.old_locales_dir, opts.old_build_id)
    new = resolve_side('new', opts.new_locales_dir, opts.new_build_id)
    if dd.same_tree(old.root, new.root):
        g.die(f"--old-locales-dir and --new-locales-dir are the same "
              f"directory ({os.path.realpath(old.root)}). That comparison "
              f"reports every file identical, which is the most reassuring "
              f"result this tool can print and means nothing.")
    return old, new


def read_texts(root, names):
    """{name: decoded text} for `names` under `root`.

    surrogateescape, not errors='replace': hundreds of these files carry
    non-ASCII, and a lossy decode makes two different files compare equal --
    the reason classify_distro_diff compares bytes.
    """
    out = {}
    for name in names:
        with open(os.path.join(root, name), 'rb') as fh:
            out[name] = fh.read().decode('utf-8', 'surrogateescape')
    return out


def style_of(text):
    """(style, hit count) for one locale's LC_COLLATE."""
    style = g.classify_collation_style(text)
    block = g.collate_text(text)
    hits = g.ellipsis_hits(block, g.comment_char(text)) if block else []
    return style, len(hits)


def style_transition(old_text, new_text):
    """(old style, new style) -- a verdict in its own right.

    'ellipsis' -> 'codepoint' means the locale stopped depending on what
    localedef does with ranges and became byte order by construction: upstream
    made exactly that change to C at glibc 2.35. Either way round, it is a
    change in what the locale IS, not only in what its bytes say.
    """
    return g.classify_collation_style(old_text), \
        g.classify_collation_style(new_text)


def in_neither_tag(repo, old_tag, new_tag, names):
    """Which of `names` exist at neither tag -- the structurally invisible set.

    ls-tree, not cat-file -e: on a --filter=blob:none clone the latter must
    fetch the blob to answer, and reports a file that exists as absent whenever
    that fetch cannot happen. filter_lc_collate_changes.py records the same
    reason.
    """
    present = set()
    for tag in (old_tag, new_tag):
        present |= {os.path.basename(p) for p in g.list_locale_files(repo, tag)}
    return sorted(n for n in names if n not in present)


def report_sides(old, new):
    print("Node-to-node comparison of locale SOURCES. Both sides are "
          "authoritative:")
    for side in (old, new):
        count, total, digest = side.manifest
        print(f"  {side.label}: {side.build_id}")
        print(f"       {side.root}")
        print(f"       {count} file(s), {total} byte(s), fingerprint {digest}")
    for side in (old, new):
        if side.skipped:
            print(f"\nSkipped {len(side.skipped)} entr(ies) on the {side.label} "
                  f"side, named so a shrunken corpus cannot pass unnoticed:")
            for entry, why in side.skipped:
                print(f"  {entry}  ({why})")


def report_buckets(old, new, buckets, side, texts, both, only_old, only_new,
                   invisible, inherited):
    print(f"\nCompared {len(both)} file(s) present on both nodes:")
    print(f"  identical:                  {len(buckets['identical'])}")
    print(f"  differ outside LC_COLLATE:  {len(buckets['other'])}")
    print(f"  differ INSIDE LC_COLLATE:   {len(buckets['collate'])}")
    print(f"  no LC_COLLATE either node:  {len(buckets['no-collate'])}")
    print(f"  only on the old node:       {len(only_old)}")
    print(f"  only on the new node:       {len(only_new)}")

    if buckets['collate']:
        print(f"\nDiffer INSIDE LC_COLLATE ({len(buckets['collate'])}) -- these "
              f"two nodes do not carry the same collation rules:")
        for name in buckets['collate']:
            old_text, new_text = texts[name]
            styles = style_transition(old_text, new_text)
            trans = (f", {styles[0]} -> {styles[1]}" if styles[0] != styles[1]
                     else f", {styles[0]} on both")
            print(f"  {name}  ({side[name]}{trans})")
            for line in dd.collate_diff_lines(old_text, new_text,
                                              old.build_id, new.build_id):
                print(f"      {line}")
    else:
        print(f"\nNothing differs inside LC_COLLATE. For every locale present "
              f"on both nodes, {old.build_id} and {new.build_id} carry the "
              f"same collation DATA -- see the warnings below for what that "
              f"does and does not settle.")

    if invisible is not None and buckets['collate']:
        hidden = [n for n in buckets['collate'] if n in set(invisible)]
        print()
        if hidden:
            print(f"  {len(hidden)} of those exist at NEITHER tag, so no "
                  f"tag-to-tag diff could see them: {', '.join(hidden)}")
        else:
            print("  All of those exist at one of the two tags, so the tag "
                  "diff could see them.")

    if buckets['collate']:
        # Through the NEW node's own copy graph: the order those files define
        # is inherited by whatever copies them, and a differing template is a
        # differing template for all of its dependants.
        dd.print_inheritance(inherited, new.build_id)

    if only_new:
        print(f"\nOnly on the new node ({len(only_new)}) -- locales this "
              f"upgrade adds:")
        for name in only_new:
            style, hits = style_of(read_texts(new.root, [name])[name])
            extra = f" ({hits} hit(s))" if style == 'ellipsis' else ''
            print(f"  {name}: {STYLE_TEXT[style]}{extra}")

    if only_old:
        print(f"\nOnly on the old node ({len(only_old)}) -- locales this "
              f"upgrade REMOVES. An index built on one of these does not just "
              f"sort differently on the new node; the collation is gone:")
        for name in only_old:
            style, _ = style_of(read_texts(old.root, [name])[name])
            print(f"  {name}: {STYLE_TEXT[style]}")


def report_backported(old, new, buckets, invisible):
    """Every KNOWN_BACKPORTED locale, whether or not it differs.

    Absent is not empty. A run that says nothing about C.UTF-8 and a run that
    cleared it look identical on a terminal, and that is how this locale gets
    missed -- it is the false negative this whole script was written for.

    Returns (computed, unexamined):

      computed    backported locales that are ellipsis-based on at least one
                  side. That -- not the fact of being backported -- is what
                  makes identical data insufficient, so the closing warning is
                  written from it rather than asserting the RHEL8 shape on
                  every run. RHEL9 and RHEL10 declare codepoint_collation, and
                  a warning that contradicts the output eight lines above it
                  is a warning nobody believes twice.
      unexamined  backported locales this run could NOT compare: absent from
                  both nodes, or present on only one. Kept separate because an
                  empty `computed` has two very different causes, and one of
                  them must never produce a reassuring closing line. Getting
                  that wrong is how "this comparison says nothing about
                  C.UTF-8" ends up printed directly above "the data comparison
                  is the whole story".
    """
    print(f"\nBackported locales, reported whether or not they differ "
          f"(these are why this comparison exists):")
    computed, unexamined = [], []
    for name in sorted(KNOWN_BACKPORTED):
        locale_name = KNOWN_BACKPORTED[name]
        on_old = name in old.names
        on_new = name in new.names
        if not on_old and not on_new:
            print(f"  {name} ({locale_name}): on NEITHER node. Either the "
                  f"distro does not ship it or glibc-locale-source is not "
                  f"installed -- this comparison says nothing about it.")
            unexamined.append(locale_name)
            continue
        if on_old != on_new:
            where = 'old' if on_old else 'new'
            print(f"  {name} ({locale_name}): present on the {where} node "
                  f"ONLY. The order it provides is not the same thing on both "
                  f"sides; test it empirically.")
            unexamined.append(locale_name)
            continue
        old_text = read_texts(old.root, [name])[name]
        new_text = read_texts(new.root, [name])[name]
        if name in buckets['collate']:
            verdict = 'LC_COLLATE DIFFERS'
        elif name in buckets['other']:
            verdict = 'differs, but outside LC_COLLATE'
        elif name in buckets['identical']:
            verdict = 'byte-identical'
        else:
            verdict = 'no LC_COLLATE block on either node'
        print(f"  {name} ({locale_name}): present on both nodes, {verdict}")
        for side, text in ((old, old_text), (new, new_text)):
            style, hits = style_of(text)
            extra = f" ({hits} hit(s))" if style == 'ellipsis' else ''
            print(f"      {side.build_id}: {STYLE_TEXT[style]}{extra}")
        if invisible is not None and name in set(invisible):
            print(f"      exists at neither tag: nothing in steps 1-5 can see "
                  f"this file at all")
        styles = (g.classify_collation_style(old_text),
                  g.classify_collation_style(new_text))
        if 'ellipsis' in styles:
            computed.append(locale_name)
            print(f"      an ellipsis range means localedef computes the "
                  f"weights, so identical data does NOT clear the order")
    return computed, unexamined


def main(argv):
    ap = argparse.ArgumentParser(
        description="Compare two nodes' locale sources against each other and "
                    "report whether any difference falls inside LC_COLLATE.")
    for label in ('old', 'new'):
        ap.add_argument(f'--{label}-locales-dir', required=True,
                        help=f"a copy of the {label} node's "
                             f"/usr/share/i18n/locales/")
        ap.add_argument(f'--{label}-build-id', required=True,
                        help=f"the {label} node's glibc build, from "
                             f"`rpm -q glibc`. Required: a result is bound to "
                             f"the builds it was taken on, and nothing in "
                             f"either directory carries a version.")
    ap.add_argument('--old-tag', help="the upstream tag the audit used for the "
                                      "old side. With --new-tag, reports which "
                                      "findings exist at neither tag -- the "
                                      "ones no tag diff could ever see.")
    ap.add_argument('--new-tag', help="the upstream tag for the new side")
    ap.add_argument('--expect-files', type=int,
                    help="abort unless exactly this many files are compared")
    ap.add_argument('--min-files', type=int, default=dd.DEFAULT_MIN_FILES,
                    help=f"abort if fewer than this many files are compared "
                         f"(default {dd.DEFAULT_MIN_FILES})")
    ap.add_argument('--repo', help="path to the glibc clone (autodetected); "
                                   "only needed with --old-tag/--new-tag")
    opts = ap.parse_args(argv)

    if bool(opts.old_tag) != bool(opts.new_tag):
        g.die("--old-tag and --new-tag go together: one tag cannot say whether "
              "a file exists at neither.")

    old, new = resolve_sides(opts)
    report_sides(old, new)

    new_set = set(new.names)
    old_set = set(old.names)
    both = [n for n in old.names if n in new_set]
    only_old = [n for n in old.names if n not in new_set]
    only_new = [n for n in new.names if n not in old_set]

    # Both sides truncated is the trap this mode adds that node-vs-tag never
    # had: the intersection is small, every file in it is identical, and the
    # result reads as a clean upgrade.
    problem = dd.corpus_problem(len(both), expect_files=opts.expect_files,
                                floor=opts.min_files,
                                what='the two node directories')
    if problem:
        g.die(problem)

    if old.manifest[2] == new.manifest[2]:
        dd.warn(f"Both directories have the same fingerprint "
                f"({old.manifest[2]}): identical file names and sizes "
                f"throughout. Two different glibc builds do not normally "
                f"produce that. Check the transport before believing anything "
                f"below -- one tar extracted over the other yields a flawless "
                f"clean result.")
    if old.build_id == new.build_id:
        dd.warn(f"Both --*-build-id values are {old.build_id}. This is a valid "
                f"control run and it proves nothing about a glibc upgrade: it "
                f"compares a build with itself.")

    invisible = None
    if opts.old_tag:
        repo = g.find_repo(opts.repo)
        g.check_refs(repo, opts.old_tag, opts.new_tag)
        invisible = in_neither_tag(repo, opts.old_tag, opts.new_tag,
                                   sorted(old_set | new_set))

    buckets, side, texts = dd.compare_trees(old.root, new.root, both,
                                            label_a=old.build_id,
                                            label_b=new.build_id)
    inherited = dd.inherited_via_copy(read_texts(new.root, new.names),
                                      buckets['collate'])
    report_buckets(old, new, buckets, side, texts, both, only_old, only_new,
                   invisible, inherited)
    computed, unexamined = report_backported(old, new, buckets, invisible)

    slug = g.pair_slug(old.build_id, new.build_id)
    out = g.write_list(
        f"node_collate_diffs.{slug}.txt",
        [f"# {old.build_id} ({old.root}) vs {new.build_id} ({new.root})"]
        + buckets['collate'])
    print(f"\nFull result written to {out}")
    # The dependants, in their own file: the differing files ARE the finding
    # and the summary counts that list; this is its reach.
    g.write_list(f"node_collate_inherited.{slug}.txt",
                 [f"# locales inheriting LC_COLLATE, via copy at "
                  f"{new.build_id}, from: {', '.join(buckets['collate'])}"]
                 + sorted(inherited))

    print()
    dd.warn(f"This compares locale DATA. Identical data is NOT identical "
            f"ORDER: the weights for an ellipsis range are computed by "
            f"localedef when the locale is built, and that code differs "
            f"between two glibc builds. Bug 22668 (commit 82292c99b2) changed "
            f"exactly that and reordered ko_KR from a byte-identical file. So "
            f"a clean result here clears the data half and nothing else.")
    # Written from what was actually read. Asserting the ellipsis shape on
    # every run made this contradict the output a few lines above it whenever
    # both nodes declared codepoint_collation -- which is the RHEL9 -> RHEL10
    # case, and half of what this script is run for.
    #
    # `unexamined` is checked FIRST and separately. An empty `computed` means
    # either "nothing here is ellipsis-based" or "nothing here could be
    # compared", and only the first licenses a reassuring line. Collapsing them
    # printed "the data comparison is the whole story" directly under "this
    # comparison says nothing about C.UTF-8" -- a reassurance over an absence,
    # which is the false negative this script was written to remove.
    if unexamined:
        dd.warn(f"{', '.join(unexamined)}: NOT compared by this run -- absent "
                f"from a node, or present on only one. Nothing above says "
                f"anything about it, and that is not the same as clearing it. "
                f"Install glibc-locale-source on both nodes and re-run, and "
                f"measure the order with sql/c_utf8_probe.sql regardless: "
                f"PostgreSQL reports collversion as NULL for every C.* name, "
                f"so nothing else will warn you.")
    if computed:
        dd.warn(f"{', '.join(computed)}: built from ellipsis ranges on at "
                f"least one of these nodes, so the order is whatever localedef "
                f"computed and is NOT settled above. PostgreSQL also reports "
                f"collversion as NULL for every C.* name, so nothing warns "
                f"either. sql/c_utf8_probe.sql is the only thing that answers "
                f"the order.")
    elif not unexamined:
        dd.warn(f"No backported locale here is ellipsis-based, so for those "
                f"the data comparison is the whole story. Run "
                f"sql/c_utf8_probe.sql anyway if C.UTF-8 is your database "
                f"collation: it is the only check that measures the order "
                f"these builds actually produce, and PostgreSQL reports "
                f"collversion as NULL for every C.* name.")
    dd.warn(f"Cheap complement, on each node: "
            f"rpm -q --changelog glibc | grep -i collat")
    dd.warn(f"localedata/charmaps/ is NOT compared. glibc-locale-source ships "
            f"it and it is an input to localedef, so a repertoire change is "
            f"outside this result as well as outside the audit. See "
            f"docs/limitations.md.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
