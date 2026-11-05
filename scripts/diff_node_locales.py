#!/usr/bin/env python3
"""
Compare TWO NODES' locale sources against each other, with no upstream tag in
the middle.

diff_distro_locales.py answers "is the audit reading what this node runs?" --
one node against the tag the audit diffed, where the node is authoritative and
upstream is only the reference. This answers a different question: "did what
the two nodes run actually change?" Both sides are authoritative and the
verdict is about the delta between them.

Why that is worth a separate comparison: a locale the DISTRO backports may be
missing from one tag of the pair or both, and then no tag-to-tag diff
compares it. `localedata/locales/C` is the case that matters -- upstream has it only from
glibc 2.35, RHEL8 and RHEL9 predate that and ship one anyway, and C.UTF-8's
order demonstrably differs between them. (RHEL10 is glibc 2.39 and has
upstream's copy, so on RHEL9 -> RHEL10 the new side is in the tag and RHEL9's
own copy still is not.)

What this does NOT prove: that the ORDER is unchanged. Wherever a locale defines
its collation with ellipsis ranges -- as RHEL8's C does, with six of them --
localedef computes those weights at build time, and Bug 22668 reordered ko_KR
from a byte-identical data file. Data equality clears the data half only. See
the closing warnings, and sql/c_utf8_probe.sql for the half this cannot answer.

It takes two directories rather than reaching into two nodes, so the transport
is the caller's problem and the comparison is testable without a node. It does
not take them on trust: two equally truncated directories agree perfectly, and
so does one directory compared with itself.

With both machines' charmaps and measurements (--old-charmaps-dir and the
rest, which audit.sh passes from files of locale_order.py --extract) and the
two tags, it also compares the character sets the two machines build their
locales with, and the collating elements they build out of
transliterations (node_charmaps). Without them it says those are not
compared.

Usage:
  python3 diff_node_locales.py --old-locales-dir DIR --old-build-id NVR \\
                               --new-locales-dir DIR --new-build-id NVR \\
                               [--old-tag TAG --new-tag TAG] \\
                               [--expect-files N] [--repo PATH]
                               [--allow-reverse]
                               [--old-charmaps-dir DIR --old-measurement FILE
                                --new-charmaps-dir DIR --new-measurement FILE
                                | --no-charmaps REASON]

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

Side = collections.namedtuple(
    'Side', 'label root build_id names skipped manifest')

STYLE_TEXT = {
    'ellipsis': 'ellipsis ranges -- weights computed by localedef',
    'codepoint': 'codepoint_collation -- byte order by construction',
    'codepoint-not-alone': 'names codepoint_collation, but could not be read '
                           'as the keyword alone -- NOT cleared',
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
    built = g.side(None, machine=root, build=build_id,
                   what=f'--{label}-locales-dir')
    return Side(label=label, root=root, build_id=build_id, names=built.paths,
                skipped=built.skipped,
                manifest=dd.tree_manifest(root, built.paths))


def resolve_sides(opts):
    """Both sides, refusing the comparisons that cannot mean anything, and
    the order of the two machines by their build ids
    (glibc_locale_data.typed_machines_order, which refuses them the other
    way round): (old, new, status, unread)."""
    old = resolve_side('old', opts.old_locales_dir, opts.old_build_id)
    new = resolve_side('new', opts.new_locales_dir, opts.new_build_id)
    if dd.same_tree(old.root, new.root):
        g.die(f"--old-locales-dir and --new-locales-dir are the same "
              f"directory ({os.path.realpath(old.root)}). That comparison "
              f"reports every file identical, which is the most reassuring "
              f"result this tool can print and means nothing.")
    status, unread = g.typed_machines_order(old.build_id, new.build_id,
                                            opts.allow_reverse)
    return old, new, status, unread


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
    localedef does with ranges and became byte order by construction. Either
    way round, it is a change in what the locale IS, not only in what its
    bytes say.
    """
    return g.classify_collation_style(old_text), \
        g.classify_collation_style(new_text)


def tag_basenames(repo, tag):
    """The file names under localedata/locales/ at `tag`.

    ls-tree, not cat-file -e: on a --filter=blob:none clone the latter must
    fetch the blob to answer, and reports a file that exists as absent whenever
    that fetch cannot happen.
    """
    return {os.path.basename(p) for p in g.list_locale_files(repo, tag)}


def removal_verdicts(old, new, only_old, old_tag, old_tag_names):
    """(removed, undetermined): what the upgrade removes, and what this
    comparison cannot say either way.

    `removed` is every name only the old copy holds that the new copy does
    not hold in any form, at a tag or not: a file the old machine has and the
    new one does not is gone from the new machine, whoever wrote it.

    `undetermined` is (name, why) for everything that could be a removal and
    that this comparison can neither report as one nor rule out. "Held" means
    held as a file this step READ: an entry it skipped is neither present nor
    absent, and counting a new-side symlink as present printed "none" over a
    file step 7's `!!` named as not read (false-negative-reviewer, round 2).

      - a file of the old tag that neither copy holds. The distro may not ship
        it, or the old copy may have lost it, and a removal lost with the copy
        would leave the summary saying "none" above step 6's `!!` naming that
        very file. Checked against the tag, not against a list of what distros
        ship, so nothing here goes stale. Not checked without --old-tag, and
        the written list says so in its header.
      - an entry the old copy holds and this step did not read -- a symlink, a
        directory -- that the new copy does not hold as a file it read. A
        dotfile is left out: no locale's name starts with a dot, and Finder
        writes a .DS_Store into any directory it opens, so one in the old copy
        alone turned "none" into an undetermined line that meant nothing.
      - a file only the old copy read, that the new copy holds in a form this
        step did not read: gone if the symlink dangles, not if it resolves.

    Every name goes through shown(): a name node_entries skips as unsafe is
    written quoted, because one that began with `#` read as the list's header
    and vanished from the summary, and one with a newline in it would have
    been two entries.

    `undetermined` is empty on the three measured copies. None of them lacks
    a file of its tag, and none holds an entry node_entries skips.
    """
    old_skipped = dict(old.skipped)
    new_skipped = dict(new.skipped)
    old_held = set(old.names) | set(old_skipped)
    new_read = set(new.names)

    def new_side(name):
        if name in new_skipped:
            return f"on the new side but not read ({new_skipped[name]})"
        return "not on the new side"

    removed = [shown(n) for n in only_old
               if n not in new_skipped]
    undetermined = []
    if old_tag_names is not None:
        for name in sorted(old_tag_names - old_held - new_read):
            where = ("and in neither copy" if name not in new_skipped
                     else f"not in the old copy, and {new_side(name)}")
            undetermined.append((name, f"in {old_tag}, {where}"))
    for name in sorted(set(old_skipped) - new_read):
        if old_skipped[name] == 'dotfile':
            continue
        undetermined.append((shown(name), f"on the old side and not read "
                                          f"({old_skipped[name]}), and "
                                          f"{new_side(name)}"))
    for name in only_old:
        if name in new_skipped:
            undetermined.append((shown(name), f"read on the old side, and "
                                              f"{new_side(name)}"))
    return removed, undetermined


# `name` as a list line can carry it.
shown = dd.shown


def report_undetermined(undetermined):
    """Printed only when there is something in it, so a complete pair of
    copies prints what it printed before."""
    if undetermined:
        print(f"\nUndetermined ({len(undetermined)}) -- this comparison cannot "
              f"say whether the upgrade removes these:")
        for name, why in undetermined:
            print(f"  {name}: {why}")


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
        print("\nNo locale present on both nodes differs inside "
              "LC_COLLATE.")

    if invisible is not None and buckets['collate']:
        hidden = [n for n in buckets['collate'] if n in set(invisible)]
        print()
        if hidden:
            print(f"  {len(hidden)} of those are not at both tags, so no "
                  f"tag-to-tag diff compared them: {', '.join(hidden)}")
        else:
            print("  All of those are at both tags, so a tag-to-tag diff "
                  "compared them.")

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


def main(argv):
    ap = argparse.ArgumentParser(
        description="Compare two nodes' locale sources against each other and "
                    "report whether any difference touches sort order.")
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
                                      "findings are not at both tags -- the "
                                      "ones a diff of these two tags does not "
                                      "compare -- and "
                                      "lets the list of what cannot be decided "
                                      "include a file of the old tag that "
                                      "neither copy holds.")
    ap.add_argument('--new-tag', help="the upstream tag for the new side")
    ap.add_argument('--allow-reverse', action='store_true',
                    help="run a pair whose new tag is the OLDER commit, or whose new machine runs the older build. Refused by default. Prints a `!!` block saying the direction is reversed.")
    ap.add_argument('--expect-files', type=int,
                    help="abort unless exactly this many files are compared")
    ap.add_argument('--repo', help="path to the glibc clone (autodetected); "
                                   "only needed with --old-tag/--new-tag")
    for label in ('old', 'new'):
        ap.add_argument(f'--{label}-charmaps-dir',
                        help=f"a copy of the {label} machine's "
                             f"/usr/share/i18n/charmaps/. With the other "
                             f"three and the two tags: compares the character "
                             f"sets the two machines build their locales with")
        ap.add_argument(f'--{label}-measurement',
                        help=f"the {label} machine's locale_order.py "
                             f"measurement, which names the locales it builds "
                             f"and their encodings")
    ap.add_argument('--no-charmaps', metavar='REASON',
                    help="why the machines' charmaps are not supplied: printed "
                         "as the reason that comparison did not run")
    opts = ap.parse_args(argv)

    if bool(opts.old_tag) != bool(opts.new_tag):
        g.die("--old-tag and --new-tag go together: one tag cannot say whether "
              "a file is at both.")
    four = (opts.old_charmaps_dir, opts.old_measurement,
            opts.new_charmaps_dir, opts.new_measurement)
    if any(four) and not all(four):
        g.die("--old-charmaps-dir, --old-measurement, --new-charmaps-dir and "
              "--new-measurement go together: the comparison needs both "
              "machines' charmaps and which locales each builds with them.")
    if all(four) and not opts.old_tag:
        g.die("comparing the two machines' charmaps needs --old-tag and "
              "--new-tag: each tag's glibc says how its machine's localedef "
              "reads the files, and which aliases its locales have.")
    if all(four) and opts.no_charmaps:
        g.die("--old-charmaps-dir and --no-charmaps cannot both be given.")
    with g.reading_chars_once():
        return _main(opts, all(four))


def _main(opts, charmaps):

    old, new, status, unread = resolve_sides(opts)
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
                                floored=True,
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
    if status == 'same':
        ids = (f"Both --*-build-id values are {old.build_id}"
               if old.build_id == new.build_id else
               f"--old-build-id {old.build_id} and --new-build-id "
               f"{new.build_id} are one build")
        dd.warn(f"{ids}. This is a valid "
                f"control run and it proves nothing about a glibc upgrade: it "
                f"compares a build with itself.")
    elif status == 'reversed':
        # Only on request: typed_machines_order refuses it otherwise.
        dd.warn(f"REVERSED PAIR of machines, allowed on request: "
                f"{old.build_id} is newer than {new.build_id}. Every finding "
                f"below has old and new the other way round -- what reads as "
                f"added was removed in the real upgrade. Not an audit of an "
                f"upgrade.")
    elif status == 'undetermined':
        # Every typed id whose version reads is ordered (rpm's rule, the
        # `.src` of typed_machines_order), so an undetermined pair always
        # names at least one id here.
        why = (f"{' and '.join(unread)} "
               f"{'is' if len(unread) == 1 else 'are'} not read as the name "
               f"of a glibc build, as rpm -q glibc prints it, with or without "
               f"the architecture")
        dd.warn(f"Which of {old.build_id} and {new.build_id} is the newer "
                f"build could not be established: {why}. Nothing in step 8 "
                f"checked it: if the two machines are the wrong way round, "
                f"step 8 reads a locale the upgrade removes as one it adds. "
                f"Confirm which machine is the older one.")

    invisible = None
    old_tag_names = None
    if opts.old_tag:
        repo = g.find_repo(opts.repo)
        commits = g.check_refs(repo, opts.old_tag, opts.new_tag)
        # The two tags decide which side's SUPPORTED maps which node's names,
        # so a reversed pair labels the older node as the new one.
        g.require_pair_order(repo, opts.old_tag, opts.new_tag,
                             allow_reverse=opts.allow_reverse,
                             commits=commits)
        old_tag_names = tag_basenames(repo, opts.old_tag)
        # Not at both tags: steps 1 to 5 over two tags compare only a file
        # both hold; one the old tag lacks is "added", one the new tag lacks
        # "deleted", neither compared (filter_lc_collate_changes.py).
        both_tags = old_tag_names & tag_basenames(repo, opts.new_tag)
        invisible = sorted(n for n in old_set | new_set if n not in both_tags)

    buckets, side, texts = dd.compare_trees(old.root, new.root, both,
                                            label_a=old.build_id,
                                            label_b=new.build_id)
    inherited = dd.inherited_via_copy(read_texts(new.root, new.names),
                                      buckets['collate'])
    report_buckets(old, new, buckets, side, texts, both, only_old, only_new,
                   invisible, inherited)
    removed, undetermined = removal_verdicts(old, new, only_old, opts.old_tag,
                                             old_tag_names)
    report_undetermined(undetermined)

    slug = g.pair_slug(old.build_id, new.build_id)
    out = g.write_list(
        f"node_collate_diffs.{slug}.txt",
        [f"# {old.build_id} ({old.root}) vs {new.build_id} ({new.root})"]
        + buckets['collate'])
    # The character sets and transliterations, machine against machine. What
    # this step lists already -- inside LC_COLLATE, or through `copy` on the
    # new machine -- is counted, not repeated. Its list is written only when
    # the comparison ran: an absent list is a comparison that did not.
    if charmaps:
        import node_charmaps as nc
        words = nc.STEP8_WORDS
        try:
            result = nc.between_machines(
                repo, opts.old_tag, opts.new_tag, old, new,
                opts.old_charmaps_dir, opts.new_charmaps_dir,
                opts.old_measurement, opts.new_measurement,
                set(buckets['collate']) | set(inherited))
        except nc.Unusable as e:
            nc.not_run(str(e), words, old.build_id, new.build_id)
        else:
            nc.print_result(result, words, [old.build_id, new.build_id])
            items = nc.listed(result)
            if items is not None:
                keyed = g.write_list(
                    f"node_keyed_locales.{slug}.txt",
                    [f"# {old.build_id} ({old.root}) vs {new.build_id} "
                     f"({new.root}): locales whose character set or "
                     f"transliterations differ in what their LC_COLLATE "
                     f"names, or that were not compared"] + items)
                print(f"\nCharacter set result written to {keyed}")
    elif opts.no_charmaps:
        import node_charmaps as nc
        nc.not_run(opts.no_charmaps, nc.STEP8_WORDS, old.build_id,
                   new.build_id)
    print(f"\nFull result written to {out}")
    # The dependants, in their own file: the differing files ARE the finding
    # and the summary counts that list; this is its reach.
    g.write_list(f"node_collate_inherited.{slug}.txt",
                 [f"# locales inheriting LC_COLLATE, via copy at "
                  f"{new.build_id}, from: {', '.join(buckets['collate'])}"]
                 + sorted(inherited))
    # What the summary relays as removed, and what stops it saying "none".
    # Both written whether or not they are empty: the summary reads them, and
    # an absent list means this step never got here, not that nothing is
    # removed.
    g.write_list(f"node_removed_locales.{slug}.txt",
                 [f"# on {old.build_id} ({old.root}) and not on "
                  f"{new.build_id} ({new.root})"] + removed)
    checked = (f"checked against {opts.old_tag}" if opts.old_tag
               else "NOT checked against a tag (no --old-tag), so a file the "
                    "old copy lost is not in this list")
    g.write_list(f"node_removed_undetermined.{slug}.txt",
                 [f"# whether {old.build_id} -> {new.build_id} removes these "
                  f"is undetermined; old copy {checked}"]
                 + [f"{name}: {why}" for name, why in undetermined])

    print()
    dd.warn(f"This compares locale DATA. Identical data is NOT identical "
            f"ORDER: the weights for an ellipsis range are computed by "
            f"localedef when the locale is built, and that code differs "
            f"between two glibc builds. Bug 22668 (commit 82292c99b2) changed "
            f"exactly that and reordered ko_KR from a byte-identical file. So "
            f"a clean result here clears the data half and nothing else.")
    if not (charmaps or opts.no_charmaps):
        dd.warn(f"A machine's own localedata/charmaps/ is NOT compared, nor "
                f"what its own transliterations build. glibc-locale-source "
                f"ships both, and they are inputs to localedef, so a change "
                f"a distro makes to either is outside this result. See "
                f"docs/limitations.md.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
