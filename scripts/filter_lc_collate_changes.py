#!/usr/bin/env python3
"""
Narrow the list of changed locale files down to those whose change falls
INSIDE the LC_COLLATE...END LC_COLLATE block -- the only part that can move
sort order -- discarding changes in LC_TIME, LC_MONETARY, comments, etc.

This generates its own diff from the two tags. It used to read a hardcoded
/tmp/localedata_full.diff that no script produced and that nothing tied to the
tags being audited, so a leftover diff from an earlier run against different
versions would be analysed silently and reported as if it were the answer.

Files added in the new tag are reported separately rather than skipped. They
used to carry the blanket claim that they "cannot affect an existing index",
which is only true if the locale did not exist on the OLD system -- something
an upstream source diff cannot establish, because distros backport. `C.UTF-8`
is the case that makes this concrete: RHEL8 and RHEL9 both ship it, its order
does change, and upstream adds localedata/locales/C only at 2.35, so the tool
reported the single most dangerous locale as harmless.

Usage:
  python3 filter_lc_collate_changes.py <old_tag> <new_tag> [--repo <path>]
                                       [--diff-file <path>]

Example:
  python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34
"""
import argparse
import os
import re
import sys
import textwrap

import glibc_locale_data as g

_FILE_HDR_RE = re.compile(r'^diff --git a/(\S+) b/(\S+)$', re.M)
_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', re.M)

# Locale source files that arrive upstream at some tag but are ALREADY SHIPPED,
# backported, by the distros this audit targets. For these, "added upstream"
# does not mean "new on your system": the locale exists on the old node, has an
# order, and that order can change.
#
# This is hardcoded because it is not derivable. `localedata/SUPPORTED` cannot
# tell them apart -- measured at 2.34 -> 2.39, `C` is absent from SUPPORTED at
# the old tag and present at the new one, exactly like the genuinely new `tok`,
# `crh_RU` and `gbm_IN`. The difference lives in what the distro backports,
# which by definition is not in an upstream tag.
#
# Keep this minimal: every entry asserts something about what a distro ships,
# and only C.UTF-8 is measured (RHEL8 and RHEL9 nodes, see README "Known
# limitations"). Do not add a locale here on the strength of a guess.
KNOWN_BACKPORTED = {
    'C': 'C.UTF-8',
}


def hunk_touches_block(start, length, lc_start, lc_end):
    """Does an old-side hunk overlap the LC_COLLATE block?

    A pure insertion (`@@ -N,0 +M,K @@`) adds lines in the gap after old line
    N, so it is inside the block only when N is strictly before the closing
    END LC_COLLATE line -- otherwise text appended just after the block would
    be flagged as a collation change.
    """
    if length == 0:
        return lc_start <= start < lc_end
    lo, hi = start, start + length - 1
    return not (hi < lc_start or lo > lc_end)


def classify_change(old_text, new_text, ranges):
    """How does one content-changed file relate to LC_COLLATE?

    Returns one of:
      'collate'        -- a hunk falls inside the old LC_COLLATE block
      'gained-collate' -- the old version had no block and the new one does
      'other'          -- has a block, but nothing changed inside it
      'no-collate'     -- no block on either side; cannot affect sort order

    `gained-collate` is its own answer rather than being filed under
    "no LC_COLLATE block". A file that acquires one changes its sort order by
    definition -- it had none and now has rules -- so folding it in with the
    files that never had one would hide a real collation change behind a count.
    It has never happened in glibc between 2.17 and 2.42, which is exactly why
    it needs a name: an unguarded path that nothing exercises is one nobody
    notices when it finally fires.

    Pure so that `gained-collate` can be tested by handing it two strings,
    rather than by fabricating a glibc clone in which it occurs.
    """
    bounds = g.collate_bounds(old_text)
    if bounds is None:
        if new_text is not None and g.collate_bounds(new_text) is not None:
            return 'gained-collate'
        return 'no-collate'
    lc_start, lc_end = bounds
    if any(hunk_touches_block(s, ln, lc_start, lc_end) for s, ln in ranges):
        return 'collate'
    return 'other'


def partition_verdicts(verdicts):
    """Group {path: verdict} into the four lists the report prints.

    Separate from classify_change() because deciding what a file IS and
    deciding what to DO about it are different mistakes. Mutation testing found
    that the hard way: with only the classifier under test, dropping the line
    that folds `gained-collate` into the changed list left the whole suite
    green -- the verdict was computed correctly and then thrown away.

    Returns (changed, gained, unchanged, no_collate). `gained` appears BOTH in
    its own list, so the report can call it out, and inside `changed`, because
    a file that acquires an LC_COLLATE block acquires a sort order.
    """
    changed, gained, unchanged, no_collate = [], [], [], []
    for path, verdict in verdicts:
        if verdict == 'collate':
            changed.append(path)
        elif verdict == 'gained-collate':
            gained.append(path)
            changed.append(path)
        elif verdict == 'no-collate':
            no_collate.append(path)
        else:
            unchanged.append(path)
    return changed, gained, unchanged, no_collate


def parse_diff(diff_text):
    """{old_path: [(old_start, old_length), ...]} from a -U0 diff."""
    files = {}
    matches = list(_FILE_HDR_RE.finditer(diff_text))
    for i, m in enumerate(matches):
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        body = diff_text[m.end():body_end]
        ranges = [(int(s), int(ln) if ln else 1)
                  for s, ln in _HUNK_RE.findall(body)]
        files[m.group(1)] = ranges
    return files


def main(argv):
    ap = argparse.ArgumentParser(
        description="Filter changed locale files down to real LC_COLLATE changes.")
    ap.add_argument('old_tag')
    ap.add_argument('new_tag')
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    ap.add_argument('--diff-file',
                    help="use this -U0 diff instead of generating one "
                         "(for offline reruns; must match the two tags)")
    opts = ap.parse_args(argv)

    repo = g.find_repo(opts.repo)
    g.check_refs(repo, opts.old_tag, opts.new_tag)
    rng = f'{opts.old_tag}..{opts.new_tag}'
    pathspec = g.LOCALES_DIR + '/'

    # Classify every change first, so added/deleted/renamed files are reported
    # as such instead of vanishing into a `continue`.
    status = g.run_git(['diff', '--name-status', '--find-renames', rng,
                        '--', pathspec], repo).stdout.decode('utf-8', 'replace')
    modified, added, deleted, renamed = [], [], [], []
    for line in status.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        code = parts[0]
        if code.startswith('R'):
            renamed.append((parts[1], parts[2]))
        elif code == 'A':
            added.append(parts[1])
        elif code == 'D':
            deleted.append(parts[1])
        else:
            modified.append(parts[1])

    if opts.diff_file:
        with open(opts.diff_file, encoding='utf-8', errors='replace') as fh:
            diff_text = fh.read()
    else:
        diff_text = g.run_git(['diff', '-U0', '--find-renames', rng,
                               '--', pathspec],
                              repo).stdout.decode('utf-8', 'replace')
    hunks = parse_diff(diff_text)

    # The diff must actually cover the files git says changed. Without this, a
    # --diff-file from a different version pair yields "nothing touches
    # LC_COLLATE" with a zero exit -- the same silent false negative that
    # generating the diff internally was meant to remove.
    content_changed = modified + [old for old, _ in renamed]
    stale = [path for path in content_changed if path not in hunks]
    if stale:
        g.die(f"the diff does not cover {len(stale)} of the "
              f"{len(modified) + len(renamed)} file(s) git reports as changed "
              f"between {opts.old_tag} and {opts.new_tag}, e.g. "
              f"{', '.join(sorted(stale)[:3])}.\n"
              f"       It does not match these tags. Drop --diff-file and let "
              f"this script generate it.")

    # Content-changed files are judged against their OLD LC_COLLATE bounds.
    old_contents, old_missing = g.read_blobs(repo, opts.old_tag, content_changed)
    if old_missing:
        g.die(f"{len(old_missing)} file(s) reported as modified do not exist at "
              f"{opts.old_tag}: {', '.join(sorted(old_missing)[:5])}. The diff "
              f"and the tags disagree -- if you passed --diff-file, it does not "
              f"match these tags.")

    # The new side is needed only for the files with no block in the old one:
    # those are the only ones that could have gained a block. Reading just
    # those keeps this to one extra batch of a handful of blobs.
    without_old_block = [p for p in content_changed
                         if g.collate_bounds(old_contents[p]) is None]
    new_contents = g.read_blobs_strict(
        repo, opts.new_tag, without_old_block,
        'the check for files that gained an LC_COLLATE block'
    ) if without_old_block else {}

    verdicts = [(path, classify_change(old_contents[path],
                                       new_contents.get(path),
                                       hunks.get(path, [])))
                for path in content_changed]
    (changed_collate, gained_collate,
     unchanged_collate, no_collate_block) = partition_verdicts(verdicts)

    total = len(modified) + len(added) + len(deleted) + len(renamed)
    print(f"Locale files changed between {opts.old_tag} and {opts.new_tag}: {total}")
    print(f"  modified: {len(modified)}   added: {len(added)}   "
          f"deleted: {len(deleted)}   renamed: {len(renamed)}")
    print(f"Of the {len(content_changed)} content-changed file(s): "
          f"{len(changed_collate)} touch LC_COLLATE, "
          f"{len(unchanged_collate)} do not, "
          f"{len(no_collate_block)} have no LC_COLLATE block")
    if no_collate_block:
        # Named, not just counted. These are LC_CTYPE transliteration tables
        # and character-class data, which define no collation on either side --
        # but printing only a number leaves a reader unable to tell that from a
        # locale that was skipped by mistake.
        names = ', '.join(sorted(os.path.basename(p) for p in no_collate_block))
        print(f"  (no LC_COLLATE on either side, so no sort order to change: "
              f"{names})")
    if gained_collate:
        print(f"\n!! {len(gained_collate)} file(s) GAINED an LC_COLLATE block "
              f"at {opts.new_tag}. They had no")
        print(f"   sort order before and have one now, so they are counted as "
              f"changed above:")
        for path in sorted(gained_collate):
            print(f"     {path}")

    print(f"\nFiles with changes inside LC_COLLATE: {len(changed_collate)}")
    for path in sorted(changed_collate):
        print(f"  {path}")

    names = sorted(os.path.basename(p) for p in changed_collate)
    if names:
        print(f"\nLocale names for step 3:")
        print(f"  python3 resolve_copy_closure.py {opts.new_tag} {' '.join(names)}")

    # KNOWN_BACKPORTED locales the audit is structurally blind to on the OLD
    # side: no source file at the old tag means nothing to diff against,
    # whether or not the file shows up at the new one.
    #
    # Reported whenever the old side is missing, NOT only when the file happens
    # to be ADDED in this range. The silent case is the one that matters: over
    # 2.28 -> 2.34 (RHEL8 -> RHEL9) localedata/locales/C is in neither tag, so
    # nothing was printed at all -- for the pair where C.UTF-8 demonstrably
    # does change (Bug 22668).
    blind = []
    for name in sorted(KNOWN_BACKPORTED):
        path = f'{g.LOCALES_DIR}/{name}'
        # ls-tree, not cat-file -e: on a --filter=blob:none clone the latter
        # must fetch the blob to answer, and calls a file that exists absent
        # whenever that fetch cannot happen.
        at_old = g.run_git(['ls-tree', '--name-only', opts.old_tag, '--', path],
                           repo).stdout.strip()
        if at_old:
            continue          # present at the old tag: judged like any file
        at_new = g.run_git(['ls-tree', '--name-only', opts.new_tag, '--', path],
                           repo).stdout.strip()
        blind.append((path, KNOWN_BACKPORTED[name], bool(at_new)))

    for path, locale_name, at_new in blind:
        where = (f"is new UPSTREAM at {opts.new_tag}" if at_new
                 else f"exists at NEITHER {opts.old_tag} nor {opts.new_tag}")
        # Wrapped at runtime rather than hand-wrapped: tag and locale names
        # vary in length, and a ragged block reads like a formatting bug in the
        # one message the reader most needs to take seriously.
        print()
        print(textwrap.fill(
            f"{path} {where}, but {locale_name} is BACKPORTED by the distros "
            f"this audit targets -- so it very likely DOES exist on your old "
            f"system, with an order of its own, and that order can change. "
            f"There is no source file to diff on the old side, so this audit "
            f"is blind to it: a clean result above says nothing about "
            f"{locale_name}. PostgreSQL will not cover the gap either -- "
            f"collversion is NULL for every collation whose name starts with "
            f"'C.', so no version mismatch can ever fire. Compare "
            f"{locale_name} empirically on both nodes. See README, Known "
            f"limitations.",
            width=78, initial_indent='!! ', subsequent_indent='   '))

    blind_paths = {path for path, _, _ in blind}
    rest = [path for path in added if path not in blind_paths]
    if rest:
        supported = g.supported_map(repo, opts.new_tag)
        # Generated names, not source file names: `locale -a` and pg_collation
        # spell it sv_SE.utf8, and the reader is about to go grep for it.
        def generated(path):
            names = supported.get(os.path.basename(path), [])
            return ', '.join(names) if names else '(not in SUPPORTED)'

        print(f"\nAdded at {opts.new_tag} ({len(rest)}), not analysed for a "
              f"change of order. An added file")
        print(f"cannot affect an existing index ONLY IF the locale did not "
              f"exist on the old system.")
        print(f"An upstream source diff cannot establish that -- distros "
              f"backport. Confirm with")
        print(f"`locale -a` on the OLD node before treating these as out of "
              f"scope:")
        for path in sorted(rest):
            print(f"  {path} -> {generated(path)}")
    if deleted:
        print(f"\nDeleted at {opts.new_tag} ({len(deleted)}) -- any index using "
              f"one of these will fail to sort on the new system at all:")
        for path in sorted(deleted):
            print(f"  {path}")
    if renamed:
        print(f"\nRenamed ({len(renamed)}) -- judged against the old path's "
              f"LC_COLLATE:")
        for old, new in sorted(renamed):
            print(f"  {old} -> {new}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
