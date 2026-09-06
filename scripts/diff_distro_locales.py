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


def collate_text(text):
    """The LC_COLLATE block as text, or None.

    Sliced from collate_bounds' line numbers rather than taken from
    collate_block: that regex requires a newline before LC_COLLATE, so a file
    beginning with LC_COLLATE at byte 0 returns None from it. glibc 2.23 and
    earlier write the three master templates exactly that way, so using
    collate_block here would silently file iso14651_t1_common -- the highest
    fan-in file in the corpus -- under "no block on either side".
    """
    bounds = g.collate_bounds(text)
    if bounds is None:
        return None
    start, end = bounds
    return '\n'.join(text.split('\n')[start - 1:end])


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
        if opts.expect_files is not None and len(both) != opts.expect_files:
            g.die(f"compared {len(both)} file(s), expected "
                  f"{opts.expect_files}. Refusing to report: a partial copy of "
                  f"the node's locales yields a clean-looking zero.")
        if len(both) < len(up_names) // 2:
            g.die(f"only {len(both)} of {len(up_names)} upstream file(s) are "
                  f"present in --locales-dir. That is too few to be a real "
                  f"copy; a partial copy reports 0 differences inside "
                  f"LC_COLLATE, which is indistinguishable from a clean run.")

        buckets = {'identical': [], 'collate': [], 'other': [], 'no-collate': []}
        side = {}
        for n in both:
            with open(os.path.join(opts.locales_dir, n), 'rb') as fh:
                node_bytes = fh.read()
            with open(os.path.join(up_root, n), 'rb') as fh:
                up_bytes = fh.read()
            verdict = classify_distro_diff(node_bytes, up_bytes)
            buckets[verdict].append(n)
            if verdict == 'collate':
                nb = collate_text(node_bytes.decode('utf-8', 'surrogateescape'))
                ub = collate_text(up_bytes.decode('utf-8', 'surrogateescape'))
                side[n] = ('node only' if ub is None else
                           'upstream only' if nb is None else 'both, differing')

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
                nb = collate_text(open(os.path.join(opts.locales_dir, n), 'rb')
                                  .read().decode('utf-8', 'surrogateescape'))
                ub = collate_text(open(os.path.join(up_root, n), 'rb')
                                  .read().decode('utf-8', 'surrogateescape'))
                import difflib
                for line in list(difflib.unified_diff(
                        (ub or '').split('\n'), (nb or '').split('\n'),
                        f'{opts.tag}', 'node', lineterm='', n=1))[:24]:
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
