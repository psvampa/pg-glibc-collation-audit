#!/usr/bin/env python3
"""
Narrow the list of changed locale files down to those whose change falls
INSIDE the LC_COLLATE...END LC_COLLATE block -- the only part that can move
sort order -- discarding changes in LC_TIME, LC_MONETARY, comments, etc.

This generates its own diff from the two tags. It used to read a hardcoded
/tmp/localedata_full.diff that no script produced and that nothing tied to the
tags being audited, so a leftover diff from an earlier run against different
versions would be analysed silently and reported as if it were the answer.

Files added in the new tag are reported separately rather than skipped: no
pre-existing index can depend on a locale that did not exist, but silently
dropping them made the "files checked" count untrue and hid genuine read
failures in the same code path.

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

import glibc_locale_data as g

_FILE_HDR_RE = re.compile(r'^diff --git a/(\S+) b/(\S+)$', re.M)
_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', re.M)


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

    changed_collate, no_collate_block, unchanged_collate = [], [], []
    for path in content_changed:
        bounds = g.collate_bounds(old_contents[path])
        if bounds is None:
            no_collate_block.append(path)
            continue
        lc_start, lc_end = bounds
        ranges = hunks.get(path, [])
        if any(hunk_touches_block(s, ln, lc_start, lc_end) for s, ln in ranges):
            changed_collate.append(path)
        else:
            unchanged_collate.append(path)

    total = len(modified) + len(added) + len(deleted) + len(renamed)
    print(f"Locale files changed between {opts.old_tag} and {opts.new_tag}: {total}")
    print(f"  modified: {len(modified)}   added: {len(added)}   "
          f"deleted: {len(deleted)}   renamed: {len(renamed)}")
    print(f"Of the {len(content_changed)} content-changed file(s): "
          f"{len(changed_collate)} touch LC_COLLATE, "
          f"{len(unchanged_collate)} do not, "
          f"{len(no_collate_block)} have no LC_COLLATE block")

    print(f"\nFiles with changes inside LC_COLLATE: {len(changed_collate)}")
    for path in sorted(changed_collate):
        print(f"  {path}")

    names = sorted(os.path.basename(p) for p in changed_collate)
    if names:
        print(f"\nLocale names for step 3:")
        print(f"  python3 resolve_copy_closure.py {opts.new_tag} {' '.join(names)}")

    if added:
        print(f"\nAdded at {opts.new_tag} ({len(added)}), not analysed for a "
              f"change of order -- they had no previous order to change. They "
              f"cannot affect an existing index, but do check them if you plan "
              f"to start using them:")
        for path in sorted(added):
            print(f"  {path}")
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
