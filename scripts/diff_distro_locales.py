#!/usr/bin/env python3
"""
Answer the question docs/limitations.md used to answer with a paragraph: do the
distro's patches to glibc's locale data touch sort order?

The audit reads glibc's UPSTREAM source. A node runs a distro build with
patches on top, so a backported collation change would be invisible to a
tag-to-tag diff. This compares a node's /usr/share/i18n/locales/ against the
same files at an upstream tag and reports how many differ, and -- the part that
matters -- how many differ INSIDE the LC_COLLATE block.

It takes a directory rather than reaching into a node, so the transport is the
caller's problem and the comparison is testable without one. It does NOT take
the directory on trust: a half-copied directory would report "12 compared, 0
inside LC_COLLATE", which is indistinguishable from a clean result. See
--expect-files.

The node is the authoritative side. Upstream is only the reference the audit
reads; the node is what the running system actually sorts with.

Usage:
  python3 diff_distro_locales.py <tag> --locales-dir <path> --build-id <nvr>
                                 [--repo <path>] [--expect-files N]
                                 [--node-label NAME]

Example:
  python3 diff_distro_locales.py glibc-2.28 \\
      --locales-dir ./el8-locales --build-id glibc-2.28-251.el8_10.40
"""
import argparse
import difflib
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap

import glibc_locale_data as g

# A directory entry becomes a path and a git argument. read_blobs' batch
# protocol desynchronises on an embedded newline, and audit.sh already made
# this decision for the same reason.
SAFE_NAME = re.compile(r'^[A-Za-z0-9_@.+-]+$')

# Several hundred files ship in glibc-locale-source; the three measured RHEL
# nodes carry 355, 356 and 366 files (docs/requirements.md). A floor well below
# all of them still catches a `docker cp` that landed three files, or a node
# without the package at all. Used where there is no upstream side to take
# half of -- see corpus_problem. One constant for nodes and tags alike, kept in
# glibc_locale_data where the tag modes apply it, so the two cannot drift.
DEFAULT_MIN_FILES = g.MIN_LOCALE_FILES


# Moved to glibc_locale_data once step 4's directory mode and
# diff_node_locales.py needed the same slicer. Re-exported under its old name:
# it is what this module's docstrings and tests call it.
collate_text = g.collate_text


def classify_distro_diff(node_bytes, upstream_bytes):
    """How does one node file relate to the same file upstream?

    Returns one of:
      'identical'      -- byte-identical; nothing to say
      'collate'        -- the LC_COLLATE block differs. THE finding: for this
                          locale the upstream diff is not reading what the node
                          runs
      'other'          -- the files differ but the block is byte-identical, so
                          the patch landed in LC_TIME, LC_IDENTIFICATION, a
                          comment, etc.
      'no-collate'     -- neither side has a block; no sort order to change

    A block present on one side and absent on the other is a block difference,
    so it returns 'collate' -- the caller reports which side, because "the node
    has rules upstream lacks" is the case the audit is blind to.

    Compares BYTES. read_blobs decodes with errors='replace', and hundreds of
    these files carry non-ASCII: one non-UTF-8 file would collapse to U+FFFD on
    both sides and compare equal. Decoding happens only to slice the block, with
    surrogateescape so it round-trips.

    Pure, so the byte-0 and whitespace cases can be tested by handing it two
    strings instead of fabricating a distro.
    """
    if node_bytes == upstream_bytes:
        return 'identical'
    node = node_bytes.decode('utf-8', 'surrogateescape')
    up = upstream_bytes.decode('utf-8', 'surrogateescape')
    nb, ub = collate_text(node), collate_text(up)
    if nb is None and ub is None:
        return 'no-collate'
    if nb != ub:
        return 'collate'
    return 'other'


def materialise_tag(repo, tag, dest):
    """Extract localedata/locales at `tag` into `dest`, byte-exact.

    git archive rather than read_blobs: the reference side has to be bytes for
    the comparison above to mean anything.
    """
    p = subprocess.run(['git', 'archive', tag, g.LOCALES_DIR],
                       cwd=repo, capture_output=True)
    if p.returncode != 0:
        g.die(f"git archive {tag} failed: "
              f"{p.stderr.decode('utf-8', 'replace').strip()}")
    with tempfile.TemporaryFile() as fh:
        fh.write(p.stdout)
        fh.seek(0)
        with tarfile.open(fileobj=fh, mode='r|') as tar:
            tar.extractall(dest)
    return os.path.join(dest, g.LOCALES_DIR)


def node_entries(root):
    """Depth-1 regular files with usable names, plus everything skipped.

    Every skipped entry is returned so it can be printed. An unreported skip
    shrinks the corpus, and a smaller corpus produces a cleaner answer.
    """
    names, skipped = [], []
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if entry.startswith('.'):
            skipped.append((entry, 'dotfile'))
        elif os.path.islink(path):
            skipped.append((entry, 'symlink'))
        elif os.path.isdir(path):
            skipped.append((entry, 'directory'))
        elif not os.path.isfile(path):
            skipped.append((entry, 'not a regular file'))
        elif not SAFE_NAME.match(entry):
            skipped.append((entry, 'unsafe name'))
        else:
            names.append(entry)
    return names, skipped


def compare_trees(root_a, root_b, names, label_a='a', label_b='b'):
    """classify_distro_diff over two directories of locale sources.

    Returns (buckets, side, texts):
      buckets  {verdict: [name]} using classify_distro_diff's four verdicts
      side     for every 'collate' name, which side carries a block --
               '<label_a> only' / '<label_b> only' / 'both, differing'
      texts    the decoded pair for those same names, so the caller can show
               the diff without opening and decoding both files a second time,
               which is what the printing loop used to do

    The labels are the caller's because the two questions are differently
    shaped: here it is a node against an upstream tag, in diff_node_locales.py
    it is one build against another.
    """
    buckets = {'identical': [], 'collate': [], 'other': [], 'no-collate': []}
    side, texts = {}, {}
    for name in names:
        with open(os.path.join(root_a, name), 'rb') as fh:
            raw_a = fh.read()
        with open(os.path.join(root_b, name), 'rb') as fh:
            raw_b = fh.read()
        verdict = classify_distro_diff(raw_a, raw_b)
        buckets[verdict].append(name)
        if verdict == 'collate':
            text_a = raw_a.decode('utf-8', 'surrogateescape')
            text_b = raw_b.decode('utf-8', 'surrogateescape')
            block_a, block_b = collate_text(text_a), collate_text(text_b)
            side[name] = (f'{label_a} only' if block_b is None else
                          f'{label_b} only' if block_a is None else
                          'both, differing')
            texts[name] = (text_a, text_b)
    return buckets, side, texts


def collate_diff_lines(text_a, text_b, label_a, label_b, limit=24):
    """A truncated unified diff of two files' LC_COLLATE blocks."""
    block_a = collate_text(text_a) or ''
    block_b = collate_text(text_b) or ''
    return list(difflib.unified_diff(block_a.split('\n'), block_b.split('\n'),
                                     label_a, label_b, lineterm='',
                                     n=1))[:limit]


def corpus_problem(compared, expect_files=None, reference=None, floor=None,
                   what='--locales-dir'):
    """Why this corpus must not be reported on, or None if it is usable.

    Pure -- counts in, a message or None out -- so the guard that decides
    whether a whole result is publishable is testable without fabricating a
    directory. Every branch exists because its failure mode reports ZERO
    differences inside LC_COLLATE, which is indistinguishable from a clean run.

    `reference` is an upstream file count, for a node compared against a tag.
    `floor` is an absolute minimum, for a comparison with no reference side at
    all: two equally truncated directories agree perfectly, and half of three
    is one.
    """
    if expect_files is not None and compared != expect_files:
        return (f"compared {compared} file(s), expected {expect_files}. "
                f"Refusing to report: a partial copy of the node's locales "
                f"yields a clean-looking zero.")
    if reference is not None and compared < reference // 2:
        return (f"only {compared} of {reference} upstream file(s) are present "
                f"in {what}. That is too few to be a real copy; a partial copy "
                f"reports 0 differences inside LC_COLLATE, which is "
                f"indistinguishable from a clean run.")
    if floor is not None and compared < floor:
        return (f"only {compared} file(s) to compare in {what}, below the floor "
                f"of {floor}. glibc-locale-source ships several hundred; this "
                f"is a truncated copy or a node without the package. Refusing "
                f"to report: too few files reports 0 differences inside "
                f"LC_COLLATE, which is indistinguishable from a clean run.")
    return None


def tree_manifest(root, names):
    """(file count, total bytes, short sha256) over the sorted name/size list.

    A fingerprint per side. Two sides whose fingerprints match while their
    build ids differ means one directory was copied twice, or one tar was
    extracted over the other -- a transport error that otherwise prints as
    100% identical, the most reassuring output this tool can produce.
    """
    digest = hashlib.sha256()
    total = 0
    for name in sorted(names):
        size = os.path.getsize(os.path.join(root, name))
        total += size
        digest.update(f'{name}:{size}\n'.encode())
    return len(names), total, digest.hexdigest()[:12]


def same_tree(path_a, path_b):
    """Do these two arguments name the same directory?

    Comparing a directory with itself yields a flawless clean result, so it is
    checked rather than trusted. Symlinks resolved: the recorded transport is a
    tar unpacked into a temp dir, and a stale symlink between two of them is
    exactly how this happens by accident.
    """
    return os.path.realpath(path_a) == os.path.realpath(path_b)


def warn(text):
    print(textwrap.fill(text, width=78,
                        initial_indent='!! ', subsequent_indent='   '))


def main(argv):
    ap = argparse.ArgumentParser(
        description="Compare a node's locale sources against an upstream tag, "
                    "and report whether any difference falls inside "
                    "LC_COLLATE.")
    ap.add_argument('tag', help="upstream glibc tag, e.g. glibc-2.28")
    ap.add_argument('--locales-dir', required=True,
                    help="a copy of the node's /usr/share/i18n/locales/")
    ap.add_argument('--build-id', required=True,
                    help="the node's glibc build, from `rpm -q glibc`. Required: "
                         "a result is bound to the build it was taken on, and "
                         "nothing in the directory carries a version.")
    ap.add_argument('--node-label', default='node',
                    help="short name for the node, used in output filenames")
    ap.add_argument('--expect-files', type=int,
                    help="abort unless exactly this many files are compared")
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    opts = ap.parse_args(argv)

    if not os.path.isdir(opts.locales_dir):
        g.die(f"--locales-dir {opts.locales_dir} is not a directory")

    repo = g.find_repo(opts.repo)
    g.check_refs(repo, opts.tag)
    g.report_tag_provenance(repo, opts.tag)

    print(f"\nNode build under test: {opts.build_id}")
    print(f"Node locale sources:   {opts.locales_dir}")

    names, skipped = node_entries(opts.locales_dir)
    if skipped:
        print(f"\nSkipped {len(skipped)} directory entr(ies), named so a "
              f"shrunken corpus cannot pass unnoticed:")
        for entry, why in skipped:
            print(f"  {entry}  ({why})")

    with tempfile.TemporaryDirectory(prefix='pg-glibc-upstream-') as tmp:
        up_root = materialise_tag(repo, opts.tag, tmp)
        up_names = set(os.listdir(up_root))

        both = [n for n in names if n in up_names]
        absent_upstream = [n for n in names if n not in up_names]
        absent_on_node = sorted(up_names - set(names))

        # A truncated copy is the failure mode that looks like success.
        problem = corpus_problem(len(both), expect_files=opts.expect_files,
                                 reference=len(up_names))
        if problem:
            g.die(problem)

        buckets, side, texts = compare_trees(opts.locales_dir, up_root, both,
                                             label_a='node',
                                             label_b='upstream')

        differ = len(buckets['collate']) + len(buckets['other'])
        print(f"\nCompared {len(both)} file(s) against {opts.tag}:")
        print(f"  identical:                 {len(buckets['identical'])}")
        print(f"  differ outside LC_COLLATE: {len(buckets['other'])}")
        print(f"  differ INSIDE LC_COLLATE:  {len(buckets['collate'])}")
        print(f"  no LC_COLLATE either side: {len(buckets['no-collate'])}")
        print(f"  absent upstream:           {len(absent_upstream)}")
        print(f"  absent on the node:        {len(absent_on_node)}")
        print(f"  ({differ} of {len(both)} differ in some way)")

        if buckets['collate']:
            print(f"\nInside LC_COLLATE ({len(buckets['collate'])}) -- for these "
                  f"locales the upstream diff is NOT reading what the node runs:")
            for n in buckets['collate']:
                print(f"  {n}  ({side[n]})")
                node_text, up_text = texts[n]
                for line in collate_diff_lines(up_text, node_text,
                                               opts.tag, 'node'):
                    print(f"      {line}")
        else:
            print(f"\nNothing differs inside LC_COLLATE. For every locale "
                  f"compared, the tag diff is reading the same collation data "
                  f"{opts.build_id} runs.")

        if absent_upstream:
            print(f"\nAbsent upstream ({len(absent_upstream)}) -- backported "
                  f"locales this comparison is structurally blind to:")
            for n in absent_upstream:
                text = open(os.path.join(opts.locales_dir, n), 'rb').read() \
                    .decode('utf-8', 'surrogateescape')
                block = collate_text(text)
                if block is None:
                    verdict = "no LC_COLLATE block: no sort order of its own"
                else:
                    targets = g.copy_targets(text)
                    covered = [t for t in targets if t in up_names]
                    body = [l for l in block.split('\n')[1:-1]
                            if l.strip() and not l.strip().startswith('%')
                            and not l.strip().startswith('copy')]
                    if targets and not body:
                        verdict = (f"pure copy of {', '.join(targets)}"
                                   f"{' (compared above)' if covered else ''}")
                    else:
                        verdict = "HAS ITS OWN TAILORING -- unauditable here"
                print(f"  {n}: {verdict}")

        if absent_on_node:
            print(f"\nAbsent on the node ({len(absent_on_node)}): "
                  f"{', '.join(absent_on_node)}")

        out = g.write_list(
            f"backports_inside_collate."
            f"{g.pair_slug(opts.tag, opts.node_label)}.txt",
            [f"# {opts.build_id} vs {opts.tag}"] + buckets['collate'])
        print(f"\nFull result written to {out}")

    print()
    warn(f"This compares locale DATA. glibc's collation CODE is step 5's job "
         f"(diff_collation_code.py) -- that is where Bug 22668 lives, the "
         f"change that reorders ko_KR -- and step 5 reads it between the two "
         f"upstream tags. What neither step covers is a distro backporting a "
         f"CODE change that is in neither tag. That gap is closed by the "
         f"empirical check on real nodes, which measures the glibc actually "
         f"installed, patches and all: docs/confirming-on-a-real-system.md. "
         f"Cheap complement: rpm -q --changelog glibc | grep -i collat")
    warn(f"localedata/charmaps/ is NOT compared. glibc-locale-source ships it "
         f"and it is an input to localedef, so a repertoire change is outside "
         f"this result as well as outside the audit. See docs/limitations.md.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
