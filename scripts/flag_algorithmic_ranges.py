#!/usr/bin/env python3
"""
Find locales whose LC_COLLATE relies on range-expansion (ellipsis) syntax
instead of listing every character's collation weight explicitly, then close
that set over the `copy` graph. Every other locale is added too, except one
glibc builds in byte order -- codepoint_collation alone, or only a copy of a
byte-order locale -- for the default weight described below.

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
Han weights and is reached by most of the locales that define LC_COLLATE.
Matching only a line-leading ellipsis cleared zh_CN, cmn_TW,
iso14651_t1_pinyin and cns11643_stroke -- a false "unaffected" for the exact
class of locale this script exists to catch.

Use diff_collation_code.py to check whether the collation code actually
changed for the version pair you care about. If it did, every locale printed
here needs an empirical sort-order test regardless of what the source diff
says.

Every other locale is printed too, except one glibc builds in byte order:
codepoint_collation alone, or only a copy of a byte-order locale
(glibc_locale_data.byte_order_locales). A character a locale does not list
takes the UNDEFINED weight, which localedef's code assigns and the file does
not hold, so even a locale with no range depends on localedef (backlog 1.6).

It also scans a DIRECTORY of locale sources -- a copy of a node's
/usr/share/i18n/locales/ -- instead of a tag, which reaches a locale the distro
BACKPORTS, and `C` is the case that matters: it exists at no
upstream tag before 2.35, and RHEL8 builds it from six ellipsis ranges, so
C.UTF-8's order there depends on how localedef expands them. Measured on
glibc-2.28-251.el8_10.40, this mode flags it; on glibc-2.34-275.el9_8 and
glibc-2.39-128.el10_2 the same file declares codepoint_collation and this mode
reports it as byte order by construction. Either way it is this mode saying so
about the node's own file, which the tag scan structurally cannot. See
docs/results.md.

Usage:
  python3 flag_algorithmic_ranges.py <tag> [--repo <path>]
  python3 flag_algorithmic_ranges.py --locales-dir <path> --build-id <nvr>
          [--supported-tag <tag>] [--expect-files N] [--min-files N]

Example:
  python3 flag_algorithmic_ranges.py glibc-2.34
  python3 flag_algorithmic_ranges.py --locales-dir ./el8-locales \
      --build-id glibc-2.28-251.el8_10.40
"""
import argparse
import os
import sys

import diff_distro_locales as dd
import glibc_locale_data as g
from filter_lc_collate_changes import KNOWN_BACKPORTED

TAG_OUT = 'step4_exposed_locales.txt'


def load_from_tag(repo, tag):
    """(texts, supported, label, out_name) for every locale file at `tag`."""
    g.check_refs(repo, tag)
    paths = g.list_locale_files(repo, tag)
    contents, missing = g.read_blobs(repo, tag, paths)
    if missing:
        g.die(f"{len(missing)} file(s) listed at {tag} could not be read: "
              f"{', '.join(sorted(missing)[:5])}")
    texts = {os.path.basename(path): text for path, text in contents.items()}
    return texts, g.supported_map(repo, tag), tag, TAG_OUT


def load_from_dir(opts, repo):
    """(texts, supported, label, out_name) for a directory of locale sources.

    The truncation guard is more load-bearing here than anywhere else in the
    tool. A node without glibc-locale-source installed presents an EMPTY
    directory, and an empty scan prints "no locale uses ellipsis ranges" --
    the most reassuring sentence this step can produce, from the step whose
    entire job is refusing to clear a locale.
    """
    root = opts.locales_dir
    if not os.path.isdir(root):
        g.die(f"--locales-dir {root} is not a directory")
    names, skipped = dd.node_entries(root)
    if skipped:
        print(f"Skipped {len(skipped)} directory entr(ies), named so a "
              f"shrunken corpus cannot pass unnoticed:")
        for entry, why in skipped:
            print(f"  {entry}  ({why})")
        print()
    problem = dd.corpus_problem(len(names), expect_files=opts.expect_files,
                                floor=opts.min_files)
    if problem:
        g.die(problem)
    texts = {}
    for name in names:
        with open(os.path.join(root, name), 'rb') as fh:
            # surrogateescape, not errors='replace': a lossy decode can make
            # two different files look identical, the reason
            # classify_distro_diff compares bytes.
            texts[name] = fh.read().decode('utf-8', 'surrogateescape')
    supported = {}
    if opts.supported_tag:
        g.check_refs(repo, opts.supported_tag)
        supported = g.supported_map(repo, opts.supported_tag)
        # The floor above lets a copy that lost a third of its files through
        # with a clean scan (backlog 1.19: 250 of glibc-2.34's 355, exit 0, no
        # `!!`). The tag is the only reference this mode has.
        tag_names = [os.path.basename(p)
                     for p in g.list_locale_files(repo, opts.supported_tag)]
        if g.report_missing_from_copy(names, tag_names, opts.supported_tag,
                                      root, skipped):
            print()
    else:
        # Absent is not empty: without a tag nothing here can notice a copy
        # that lost files, and a scan that did not look must not read like
        # one that looked and found everything.
        g.warn(f"The files in {root} were not checked against any tag's "
               f"list, so a copy that lost files is not detected here. Pass "
               f"--supported-tag to check it.", split_words=False)
        print()
    slug = g.pair_slug(opts.build_id, opts.build_id).split('..')[0]
    return (texts, supported, f'{opts.build_id} ({root})',
            f'step4_exposed_locales.{slug}.txt')


def report_codepoint(texts):
    """Name the locales that cannot be moved by an expansion change at all.

    Reported rather than left unnamed: since backlog 1.6 these are the only
    locales this step does not list. `codepoint_collation` alone is a
    positive statement -- byte order by construction -- and it is the
    difference between upstream's C from glibc 2.35 on and the ellipsis-based
    copy RHEL8 ships. A locale that copies a byte-order locale and nothing
    else sorts the same way (backlog 6.9), directly or through a chain of such
    copies, and is named on a line of its own, because it declares nothing.
    A reader who cannot see which one is in front of them cannot tell a
    cleared locale from an unexamined one.

    The keyword beside anything else is not here: glibc does not build byte
    order from it, or this code cannot tell that it does
    (glibc_locale_data.declares_byte_order), so such a locale stays listed.

    Returns (every name above, {name: the locale it copies}).
    """
    declared, by_copy = g.byte_order_locales(texts)
    if declared:
        print(f"\nDeclare codepoint_collation, so no expansion change can "
              f"move them: {', '.join(sorted(declared))}")
    if by_copy:
        named = ', '.join(f'{n} (copies {t})'
                          for n, t in sorted(by_copy.items()))
        print(f"\nCopy a byte-order locale and nothing else, so glibc builds "
              f"them in byte order too: {named}")
    return sorted(declared | set(by_copy)), by_copy


def listed_names(names, aliases):
    """`names`, and every alias glibc's locale.alias gives one of them.

    The list holds locales, not spellings, for the reason step 3 gives
    (backlog 13.1): SUPPORTED's spellings missed sv_SE.iso885915, swedish and
    the others the real machines carry, and a name that cannot be found in
    the list reads as a name that was cleared. A spelling reaches its locale
    by glibc_locale_data.locale_source; an alias does not, so it is listed.
    """
    return set(names) | set(g.aliases_of(set(names), aliases))


def report_backported(texts, inherited=None, unresolved=None,
                      default_only=(), by_copy=None):
    """Declare, one by one, what this scan found for each locale the distros
    are known to BACKPORT. Returns {name: status}.

    `inherited` is step 4's own closure, {name: roots it reaches}. A locale
    whose file uses no ellipsis can still be exposed by copying one that does,
    and its style alone is then true of the FILE and false of the ORDER. The
    closure is already computed where this is called from; not passing it is
    a verdict computed and thrown away.
    `unresolved` is the other half of the same question: {name: copy targets
    this corpus does not contain}. A copy the walk could not follow is a locale
    whose order was never read, and saying only "copy-only, its order is
    whatever it inherits" makes that indistinguishable from a copy resolved to
    a file with nothing in it.
    `default_only` is the third: the locales no range reaches, which this step
    lists for localedef's default weight (backlog 1.6). Without it an explicit
    C read "no ellipsis range for localedef to expand" in the summary while the
    step's own list named it.

    `codepoint_collation` is byte order only alone
    (glibc_locale_data.declares_byte_order), and a file holding it alone
    copies nothing, so none of the three notes can reach it. Beside anything
    else this code does not call it byte order. Beside a copied template
    glibc gives none (`copy "iso14651_t1"` plus the keyword compiles into
    broken tables on RHEL9 and RHEL10, glibc study E5), and the other shapes
    are ones this code cannot read with certainty. The style is then
    'codepoint-not-alone', or 'ellipsis' when a range sits beside the
    keyword, and the notes apply to it as to any other file.
    `by_copy` is the fourth, and the one that clears: {name: the locale it
    copies}, for a locale whose LC_COLLATE copies a byte-order locale and
    nothing else (backlog 6.9). Its style says "copy-only", which is true of
    the file; without the note the status would leave the order unsaid while
    step 4 names the locale as byte order.

    The wrapper used to infer C's state from two greps -- is it in the ellipsis
    list, else does the codepoint line name it -- and a C that was neither
    printed nothing at all. Nothing is also what a run that never looked
    prints, so a locale present with explicit weights, a locale that only
    copies another, and a locale absent from the directory all arrived at the
    reader as silence. Absent, cleared and unexamined are three answers.

    Directory mode only. A git tag holds no distro backport by construction,
    and a heading here in tag mode would invite the reader to trust the tag
    scan on the one question it structurally cannot answer.
    """
    styles = {
        'ellipsis': 'ellipsis-based  <- localedef computes the weights, so '
                    'identical data is not identical order',
        'codepoint': 'codepoint_collation  <- byte order by construction',
        # Must not start with "codepoint_collation": audit.sh reads C's line
        # by how it starts and would print "byte order by construction".
        'codepoint-not-alone': 'names codepoint_collation, but could not be '
                               'read as the keyword alone in its LC_COLLATE  '
                               '<- the one form glibc builds in byte order, '
                               'so this is NOT cleared',
        'explicit': 'explicit weights  <- no ellipsis range for localedef to '
                    'expand',
        'copy-only': 'copy-only  <- its order is whatever it inherits; follow '
                     'the copy chain',
        'none': 'present, but defines no LC_COLLATE block',
    }
    inherited = inherited or {}
    unresolved = unresolved or {}
    by_copy = by_copy or {}
    found = {}
    print("\nDistro-backported locales, declared one by one -- absent, "
          "cleared and")
    print("unexamined are three different answers:")
    for name in sorted(KNOWN_BACKPORTED):
        text = texts.get(name)
        if text is None:
            status = 'ABSENT from this directory  <- not examined here'
        else:
            style = g.classify_collation_style(text)
            status = styles[style]
            if name in inherited:
                status += (f"; and it copies "
                           f"{', '.join(sorted(inherited[name]))}, which this "
                           f"step flagged -- so this locale IS exposed")
            if name in default_only:
                status += ("; and every character it does not list takes "
                           "localedef's default weight -- so this locale IS "
                           "exposed")
            if name in unresolved:
                status += (f"; and it copies "
                           f"{', '.join(sorted(unresolved[name]))}, which is "
                           f"NOT in this corpus -- what that carries was never "
                           f"read, so this locale is NOT cleared")
            if name in by_copy:
                status += (f"; and it copies {by_copy[name]} and nothing "
                           f"else, which glibc builds in byte order -- so "
                           f"this locale is byte order too")
        found[name] = status
        print(f"  {name} ({KNOWN_BACKPORTED[name]}): {status}")
    return found


def report_default_weight(names):
    """Name the locales that only localedef's default weight exposes.

    Every character a locale does not list takes the weight of UNDEFINED, and
    a file that declares no UNDEFINED gets one appended after everything else
    (glibc-2.39:locale/programs/ld-collate.c, collate_finish: "simply append
    UNDEFINED at the end"). That weight comes from the code, not the file, so
    a locale no ellipsis range reaches still depends on localedef -- the same
    dependency this step exists to name. It is the fallback that exposes a
    locale, not the keyword: ar_SA never writes UNDEFINED. Backlog 1.6.
    """
    if names:
        print(f"\nAdditionally exposed through localedef's default weight: "
              f"{len(names)}")
        print(f"      {', '.join(names[:12])}"
              f"{', ...' if len(names) > 12 else ''}")
        print("  No ellipsis range reaches these, but every character a locale "
              "does not list\n  gets the weight localedef assigns to UNDEFINED, "
              "in code, not from the file.")


def report(texts, supported, label, out_name, next_hint,
           supported_tag=None, node_dir=False, aliases=None):
    aliases = aliases or {}
    flagged, with_collate = g.scan_ellipsis(texts)

    print(f"Files at {label}: {len(texts)}, of which {with_collate} define "
          f"LC_COLLATE")
    # The file-count floor asks whether enough files were read; this asks
    # whether any of them turned out to be a locale. Not one collation block
    # out of a full corpus means the reader is wrong, not that the corpus has
    # no collation -- and everything below would then print the cleanest
    # result this step has. Measured at the five pinned tags: 274 of 286, 300
    # of 312, 340 of 353, 342 of 355 and 352 of 366.
    if not with_collate:
        g.die(f"{len(texts)} file(s) were read at {label} and not one defines "
              f"LC_COLLATE. That is a reader or a corpus problem, not a "
              f"collation result: refusing to report 'no locale uses ellipsis "
              f"ranges' over it.")
    print(f"Locales whose LC_COLLATE uses ellipsis (algorithmic) ranges: "
          f"{len(flagged)}")
    for name in sorted(flagged):
        print(f"  {name}")
        for hit in flagged[name][:3]:
            print(f"      {hit}")
        if len(flagged[name]) > 3:
            print(f"      ... and {len(flagged[name]) - 3} more")

    immune, by_copy = report_codepoint(texts)

    # Built before anything can return, because a `copy` target this corpus
    # does not contain is a locale whose order was NOT read, and
    # `inherited_from` treats an unknown target as a leaf -- so "resolved, and
    # what it copies is clear" and "could not resolve it at all" reach the
    # reader as the same sentence. Measured 0 dangling targets at glibc-2.28,
    # 2.34 and 2.39 and on the three RHEL fixtures (47/47/48 distinct targets),
    # so this fires only on a directory that is not the closed source a node
    # built from -- which is the input the ABSENT wording already contemplates.
    graph = g.copy_graph_from_texts(texts)
    dangling = {t for ts in graph.values() for t in ts} - set(graph)
    unresolved = g.inherited_from(graph, dangling) if dangling else {}
    if dangling:
        print(f"\n!! {len(dangling)} `copy` target(s) are absent from this "
              f"corpus, so what they carry was never read:")
        print(f"     {', '.join(sorted(dangling))}")
        # Named, not only counted: "N locale(s) reach one" is a verdict
        # computed and never attached to a name.
        reaching = sorted(unresolved)
        # "in the list below" was written for a reader looking at this step.
        # audit.sh relays `!!` blocks into the summary, where there is no
        # below, so the block names the file the step writes instead.
        print(f"   {len(reaching)} locale(s) reach one. They are NOT cleared, "
              f"and they are in this step's full list, named at the end of it:")
        print(f"     {', '.join(reaching[:12])}"
              f"{', ...' if len(reaching) > 12 else ''}")

    # A flagged template is only actionable together with everything that
    # inherits it: iso14651_t1 carries the Han range and is copied, directly or
    # transitively, by most of the corpus.
    inherited = g.inherited_from(graph, set(flagged)) if flagged else {}
    # What neither an ellipsis nor a copy of one reaches still sorts every
    # character it does not list by localedef's default weight, so it is
    # exposed too. Only a locale glibc builds in byte order is free of it
    # (`immune`: codepoint_collation alone, or a copy of one and nothing
    # else), and a locale whose copy target is absent is already named in the
    # `!!` block above.
    default_only = sorted(set(graph) - set(flagged) - set(inherited)
                          - set(immune) - set(unresolved))

    if not flagged:
        report_default_weight(default_only)
        if default_only:
            print("\nNo locale uses ellipsis ranges here, but the "
                  f"{len(default_only) + len(unresolved)} locale(s) above are "
                  "NOT cleared by steps 1-3.")
        elif unresolved:
            print("\nNothing that could be READ here uses an ellipsis range, "
                  "but the absent copy\ntargets above leave "
                  f"{len(unresolved)} locale(s) unresolved: steps 1-3 are NOT "
                  f"sufficient for those.")
        else:
            print("\nNo locale uses ellipsis ranges here; steps 1-3 are "
                  "sufficient.")
        # Written whether or not anything was found: same reason as step 3.
        # audit.sh must be able to tell "nothing exposed" from "step 4 did not
        # run". Announced too -- the `!!` block above promises a list, and this
        # path used to write it and never say where, so the promise pointed at
        # nothing and the twelve names it prints were all a reader could get.
        names = set(unresolved) | set(default_only)
        print("  Locales, not spellings. One is in use wherever a locale name, "
              "without the part\n  from the dot up to any @, is one of them; "
              "the list adds the aliases\n  glibc's locale.alias gives them.")
        not_ascii = g.non_ascii_alias_targets(names, aliases)
        if not_ascii:
            print(f"  not listed: an alias of {', '.join(not_ascii)} whose "
                  f"name is not ASCII; PostgreSQL never imports such a name "
                  f"as a collation")
        listed = sorted(listed_names(names, aliases))
        out_path = g.write_list(out_name, listed)
        print(f"  full list ({len(listed)} name(s)): {out_path}")
        # Declared on this path too. A directory where nothing uses an
        # ellipsis is where this step comes closest to reassuring, and it is
        # exactly where the summary must still be able to say what the node's
        # C is -- returning here without a status made the wrapper print
        # "NOT DECLARED" over a scan that had looked and found an answer.
        if node_dir:
            report_backported(texts, {}, unresolved, default_only, by_copy)
        return 0

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

    report_default_weight(default_only)

    exposed = sorted(set(flagged) | set(inherited) | set(default_only))
    # N counts the unresolved locales too: they are on the list, and the `!!`
    # block above says why. Counted as what they add: an alias can share its
    # name with a locale already in the set, and "N locales and M aliases"
    # must add up to the full list printed below.
    to_confirm = set(exposed) | set(unresolved)
    alias_names = sorted(set(g.aliases_of(set(exposed) | set(unresolved),
                                          aliases))
                         - set(exposed) - set(unresolved))
    not_ascii = g.non_ascii_alias_targets(set(exposed) | set(unresolved),
                                          aliases)
    unbuilt = [loc for loc in exposed if not supported.get(loc)]
    if supported:
        print(f"\nFull set needing empirical confirmation: {len(to_confirm)} "
              f"locale(s), and {len(alias_names)} more\nname(s), the aliases "
              f"glibc's locale.alias gives them. Locales, not spellings.\nOne "
              f"is in use wherever a locale name, without the part from the "
              f"dot up to\nany @, is one of them.")
        print(f"  e.g. {', '.join(exposed[:8])}, ...")
        if not_ascii:
            print(f"  not listed: an alias of {', '.join(not_ascii)} whose "
                  f"name is not ASCII; PostgreSQL never imports such a name "
                  f"as a collation")
        if unbuilt:
            if node_dir:
                # Measured on the three Rocky 8/9/10 fixtures, 2026-09-07:
                # none ships /usr/share/i18n/SUPPORTED and glibc-locale-source
                # installs none, so this mapping can only come from a tag --
                # and the tag does not decide what the node built. The RHEL8
                # fixture (glibc-2.28-251.el8_10.40) builds 867 locales, C.utf8
                # among them, and audit.sh maps that node through glibc-2.28,
                # whose SUPPORTED does not list C at all.
                print(f"  not in {supported_tag}'s SUPPORTED -- the node's "
                      f"`locale -a` is the authority on whether these are "
                      f"built: {', '.join(unbuilt)}")
            else:
                print(f"  not in SUPPORTED (not built by default): "
                      f"{', '.join(unbuilt)}")
    else:
        # No tag: nothing to read locale.alias or SUPPORTED from, and the
        # node's own `locale -a` is the authority on which of these are built.
        print(f"\nFull set needing empirical confirmation: {len(to_confirm)} "
              f"locale(s). Locales, not spellings; no tag was given, so the "
              f"aliases glibc's locale.alias gives them are not added -- pass "
              f"--supported-tag to add them.")
        print(f"  e.g. {', '.join(exposed[:8])}, ...")
    # Same rule as step 3: every exposed and every unresolved locale, and
    # their aliases. A list of SUPPORTED's spellings once dropped every exposed
    # locale SUPPORTED does not name, so the file the sentence above calls the
    # full list was NARROWER than what was reported. On a node that omission
    # was C -- the collation initdb picks -- and a name absent from the list
    # reads as a name cleared.
    listed = sorted(set(exposed) | set(unresolved) | set(alias_names))
    out_path = g.write_list(out_name, listed)
    print(f"  full list ({len(listed)} name(s)): {out_path}")

    print()
    print("These cannot be cleared by a source diff alone.")
    for line in next_hint:
        print(line)

    # Last, not before the sentence above: "These" names the exposed set, and
    # a declaration wedged in between put "C (C.UTF-8): codepoint_collation"
    # directly above it, where a reader takes C for one of "these" and tests a
    # locale glibc settled by construction. Noise is a cost like any other.
    if node_dir:
        report_backported(texts, inherited, unresolved, default_only,
                          by_copy)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        description="Flag locales whose LC_COLLATE uses algorithmic ellipsis "
                    "ranges, plus everything inheriting them. Every other "
                    "locale is flagged too, except one glibc builds in byte "
                    "order (codepoint_collation alone, or only a copy of a "
                    "byte-order locale), because a character a locale "
                    "does not list takes localedef's default weight.")
    ap.add_argument('tag', nargs='?',
                    help="glibc tag to scan, e.g. glibc-2.34")
    ap.add_argument('--locales-dir',
                    help="scan a copy of a node's /usr/share/i18n/locales/ "
                         "instead of a tag, which reaches a locale the distro "
                         "backports, such as C (C.UTF-8).")
    ap.add_argument('--build-id',
                    help="with --locales-dir: the node's glibc build, from "
                         "`rpm -q glibc`. Required, because a result is bound "
                         "to the build it was taken on and nothing in the "
                         "directory carries a version.")
    ap.add_argument('--supported-tag',
                    help="with --locales-dir: a tag whose localedata/SUPPORTED "
                         "says which locales are built by default and whose "
                         "intl/locale.alias names their aliases (swedish for "
                         "sv_SE), which the written list adds. A node ships "
                         "no SUPPORTED -- measured on Rocky 8/9/10, none has "
                         "/usr/share/i18n/SUPPORTED and glibc-locale-source "
                         "installs none -- so both are the tag's, and the "
                         "tag does not know what the node built. Also the "
                         "list the directory is checked against: every file "
                         "of the tag it lacks is named under a `!!`.")
    ap.add_argument('--expect-files', type=int,
                    help="with --locales-dir: abort unless exactly this many "
                         "files are read")
    ap.add_argument('--min-files', type=int, default=dd.DEFAULT_MIN_FILES,
                    help=f"with --locales-dir: abort below this many files "
                         f"(default {dd.DEFAULT_MIN_FILES})")
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    opts = ap.parse_args(argv)

    if bool(opts.tag) == bool(opts.locales_dir):
        ap.error("give either a tag or --locales-dir, not both and not "
                 "neither")
    if opts.locales_dir and not opts.build_id:
        ap.error("--locales-dir requires --build-id: a result that does not "
                 "say which build it was taken on cannot be cited")

    if opts.tag:
        repo = g.find_repo(opts.repo)
        texts, supported, label, out_name = load_from_tag(repo, opts.tag)
        if g.wrapped():
            hint = ["Step 5 below decides whether that matters for this pair."]
        else:
            hint = [f"Run",
                    f"  python3 diff_collation_code.py <old_tag> {opts.tag}",
                    f"to see whether localedef's collation code changed "
                    f"between your two",
                    f"versions; if it did, test these empirically before "
                    f"trusting a",
                    f"'not flagged' result from steps 1-3."]
    else:
        repo = g.find_repo(opts.repo) if opts.supported_tag else None
        texts, supported, label, out_name = load_from_dir(opts, repo)
        hint = ["This is one node's own data. Whether the WEIGHTS localedef "
                "computes for these",
                "differ between two nodes is a question about localedef, not "
                "about this",
                "directory: diff_collation_code.py for the two upstream tags, "
                "and",
                "docs/confirming-on-a-real-system.md for the glibc each node "
                "actually runs."]

    alias_tag = opts.tag or opts.supported_tag
    aliases = g.locale_aliases(repo, alias_tag) if alias_tag else {}
    return report(texts, supported, label, out_name, hint,
                  supported_tag=opts.supported_tag or opts.tag,
                  node_dir=bool(opts.locales_dir), aliases=aliases)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
