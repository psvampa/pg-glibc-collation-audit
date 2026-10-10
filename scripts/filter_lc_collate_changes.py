#!/usr/bin/env python3
"""
Narrow the list of changed locale files down to those whose change falls
INSIDE the LC_COLLATE...END LC_COLLATE block, or changes the comment or escape
characters that block is read with, discarding changes in LC_TIME,
LC_MONETARY, comments, etc.

It also compares the charmaps the SUPPORTED builds use between the two tags
(collation_keys.py), and writes the builds that check lists to
step2_keyed_locales.<pair>.txt, which step 3 takes after --keyed.

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
                                       [--diff-file <path>] [--allow-reverse]

Example:
  python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34
"""
import argparse
import os
import re
import sys
import textwrap

import collation_keys as ck
import glibc_locale_data as g

_FILE_HDR_RE = re.compile(r'^diff --git a/(\S+) b/(\S+)$', re.M)
_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', re.M)
# The same header, read one line at a time and keeping the new side's start
# too, so each changed line can be placed on its own side of the diff.
_HUNK_LINE_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')

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
# and only C.UTF-8 is measured (RHEL8 and RHEL9 nodes, see
# docs/results.md). Do not add a locale here on the strength of a guess.
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
      'reading'        -- the block did not change, but the comment or escape
                          character it is read with did, or cannot be settled
      'other'          -- has a block, and neither the block nor the comment
                          and escape characters it is read with changed
      'no-collate'     -- no block on either side; cannot affect sort order

    `reading` is its own answer for the reason `gained-collate` is: the
    directives sit outside the block, so no hunk lands inside it, and the
    same block read with other characters is other rules (backlog 13.7). A
    new side nobody passed in cannot clear that, so a file with a block and
    no new text is 'reading' too; main() reads every new side.

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
    if new_text is None or g.reading_changed(old_text, new_text):
        return 'reading'
    return 'other'


def partition_verdicts(verdicts):
    """Group {path: verdict} into the four lists the report prints.

    Separate from classify_change() because deciding what a file IS and
    deciding what to DO about it are different mistakes. Mutation testing found
    that the hard way: with only the classifier under test, dropping the line
    that folds `gained-collate` into the changed list left the whole suite
    green -- the verdict was computed correctly and then thrown away.

    Returns (changed, gained, reading, unchanged, no_collate). `gained` and
    `reading` each appear BOTH in their own list, so the report can call
    them out, and inside `changed`: a file that acquires an LC_COLLATE block
    acquires a sort order, and one whose block is read with other characters
    has other rules.
    """
    changed, gained, reading, unchanged, no_collate = [], [], [], [], []
    for path, verdict in verdicts:
        if verdict == 'collate':
            changed.append(path)
        elif verdict == 'gained-collate':
            gained.append(path)
            changed.append(path)
        elif verdict == 'reading':
            reading.append(path)
            changed.append(path)
        elif verdict == 'no-collate':
            no_collate.append(path)
        elif verdict == 'other':
            unchanged.append(path)
        else:
            # Not a default bucket: a verdict this does not know landed in
            # `unchanged` before, which is how a new answer is cleared unread.
            raise ValueError(f"unknown verdict {verdict!r} for {path}")
    return changed, gained, reading, unchanged, no_collate


def new_side_paths(content_changed, renamed_to):
    """Which paths to read at the NEW tag, and under which name.

    Every content-changed file. One with no LC_COLLATE block on the old side
    could have gained one, and one with a block could be read with other
    comment or escape characters while the block itself is unchanged
    (classify_change, 'reading'). Until backlog 13.7 only the first kind was
    read, so the second was judged on the old side alone. A renamed file
    lives under its NEW name there: reading the old name aborted the whole
    step with "could not read" on a legitimate rename, and looking the
    verdict up under the old name made `gained-collate` undetectable for any
    renamed file. Neither has happened in an audited pair -- the one rename,
    aa_ER@saaho to ssy_ER over 2.34..2.39, has a block on the old side --
    which is exactly why it is a function with a test rather than two lookups
    in main().

    Returns {old_path: new_path} for the files to read.
    """
    return {p: renamed_to.get(p, p) for p in content_changed}


def judge(content_changed, old_contents, new_contents, hunks, renamed_to):
    """[(old_path, verdict)] for every content-changed file.

    `new_contents` is keyed by the path at the NEW tag, as read_blobs_at
    returns it; the lookup goes through `renamed_to` so a renamed file finds
    its own new text.
    """
    return [(path, classify_change(old_contents[path],
                                   new_contents.get(renamed_to.get(path, path)),
                                   hunks.get(path, [])))
            for path in content_changed]


def read_otherwise(changed, reading, old_contents, new_contents, renamed_to):
    """The changed files read with other characters at the new tag, sorted.

    Not only the 'reading' verdict. A hunk inside the block returns
    'collate' before the characters are compared, and the characters
    printed for such a file name only its changed lines, while every rule in
    it can read differently -- the list would read as complete. A file with
    an old block and no new text counts too: nothing settled its characters.
    """
    out = []
    for path in changed:
        old = old_contents[path]
        new = new_contents.get(renamed_to.get(path, path))
        if path in reading or (g.collate_bounds(old) is not None
                               and (new is None
                                    or g.reading_changed(old, new))):
            out.append(path)
    return sorted(out)


def split_raw(text):
    """([name-status line, ...], patch) from a `--patch-with-raw` diff.

    git writes one `:` line per changed file, a blank line, and the patch;
    with nothing changed, nothing. A `:` line is `:<old mode> <new mode>
    <old id> <new id> <status>` and a tab before the path or paths, so what
    follows its fourth space is the line `--name-status` prints for that
    file. Measured with git 2.54 on every pair the tests run, renames and
    the same commit twice included: those lines are the name-status, and the
    patch is the `-U0` diff, byte for byte. A `:` line of another shape dies:
    read as a modified file, it would be judged against hunks it has not got.
    So does a patch with no list above it, which would read as nothing
    changed.
    """
    status, pos = [], 0
    while text.startswith(':', pos):
        nl = text.find('\n', pos)
        line = text[pos:nl if nl >= 0 else len(text)]
        fields = line.split(' ', 4)
        if nl < 0 or len(fields) != 5 or '\t' not in fields[4]:
            g.die(f"unreadable line in git's list of changed files: {line!r}")
        status.append(fields[4])
        pos = nl + 1
    if status and text.startswith('\n', pos):
        pos += 1
    if not status and text:
        g.die(f"git's diff has no list of changed files above it, so nothing "
              f"says what changed: {text[:80]!r}")
    return status, text[pos:]


def in_listing(path, listed):
    """Would `git ls-tree <tag> -- <path>` name `path`? Answered from
    list_locale_files' recursive listing of the same tag, so asking costs no
    git: a file is in the listing itself, a directory as the files under it,
    quoted when their names need it. A tree with no file under it at any
    depth, which only plumbing can commit, is named by ls-tree and absent
    from the listing; the answer is then no, and the note about the path
    prints: the noisy direction. ls-tree rather than `cat-file -e` either
    way: on a --filter=blob:none clone the latter must fetch the blob to
    answer, and calls a file that exists absent whenever that fetch cannot
    happen.
    """
    return path in listed or any(p.startswith((path + '/', f'"{path}/'))
                                 for p in listed)


def parse_diff(diff_text):
    """{old_path: [(old_start, old_length), ...]} from a -U0 diff.

    Refuses what it cannot read instead of returning fewer ranges. A file with
    no range is judged 'other' -- has a block, nothing changed inside it -- so
    every range lost here is a change reported as outside LC_COLLATE, at exit
    0. Two shapes did that: "Binary files a/x and b/x differ", which git writes
    for every locale under one `-diff` in the reader's attributes (git_diff
    passes --text, so only a --diff-file can still carry it), and a `@@` line
    the header pattern does not read, which findall skipped in silence.
    """
    files = {}
    matches = list(_FILE_HDR_RE.finditer(diff_text))
    for i, m in enumerate(matches):
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        body = diff_text[m.end():body_end]
        ranges = []
        # A changed line starts with '-' or '+' and a context line with ' ',
        # so a line starting with '@@' is always a hunk header.
        for line in body.split('\n'):
            if line.startswith('Binary files '):
                g.die(f"{m.group(1)}: the diff shows it as binary, with no "
                      f"hunk, so the change was not read. Drop --diff-file "
                      f"and let this script generate it.")
            if line.startswith('@@'):
                h = _HUNK_RE.match(line)
                if not h:
                    g.die(f"{m.group(1)}: unreadable hunk header {line!r}, so "
                          f"the change under it was not read.")
                start, length = h.groups()
                ranges.append((int(start), int(length) if length else 1))
        # extend, not assign: a path can have two sections -- a file replaced
        # by a symlink is a deletion plus a creation, and a --diff-file can
        # repeat a path -- and keeping only the last one kept the creation's
        # `@@ -0,0` and lost every range the deletion had inside the block.
        files.setdefault(m.group(1), []).extend(ranges)
    return files


def diff_sections(diff_text):
    """{old_path: [section, ...]}: the text under each file's diff header.

    A list, because git can show one path in two sections -- a file replaced
    by a symlink is a deletion and a creation -- and then neither section
    alone is that file's change.
    """
    sections = {}
    matches = list(_FILE_HDR_RE.finditer(diff_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        sections.setdefault(m.group(1), []).append(diff_text[m.end():end])
    return sections


def changed_characters(sections, old_text, new_text):
    """The characters named on the changed lines inside LC_COLLATE, in order.

    A removed line is placed by its old line number against the old block and
    an added line by its new line number against the new block, so a hunk that
    crosses the block's edge contributes only the lines inside it, and a file
    that gained its block is read from the new one. Each line's comment is cut
    before the `<U....>` escapes are read: a comment names characters too, and
    a changed comment is not a changed rule.

    Returns [] when the lines name no character and also when this cannot
    tell -- the new text could not be read, the path does not have exactly one
    section in the diff, a hunk header does not parse. The caller prints the
    same thing for all of them: the locale stays flagged, and the reader is
    told the characters are unknown, never that there are none.
    """
    if new_text is None or len(sections) != 1:
        return []
    bounds = {'-': g.collate_bounds(old_text), '+': g.collate_bounds(new_text)}
    # Once per side, not once per line: comment_char searches the whole file,
    # and called per line it took 17 minutes on cns11643_stroke.
    cchar = {'-': g.comment_char(old_text), '+': g.comment_char(new_text)}
    line_no = None
    seen, found = set(), []
    for line in sections[0].split('\n'):
        if line.startswith('@@'):
            m = _HUNK_LINE_RE.match(line)
            if not m:
                return []
            line_no = {'-': int(m.group(1)), '+': int(m.group(2))}
            continue
        # Before the first hunk header come the ---/+++ file names; after it,
        # a line opening with --- is a removed line whose text opens with --.
        if line_no is None:
            continue
        side = line[:1]
        if side == ' ':               # context: the -U0 diff read here has none
            line_no['-'] += 1
            line_no['+'] += 1
            continue
        if side not in ('-', '+'):
            continue
        n = line_no[side]
        line_no[side] += 1
        block = bounds[side]
        if block is None or not block[0] <= n <= block[1]:
            continue
        for hexcp in g._UCHAR_RE.findall(line[1:].split(cchar[side])[0]):
            code = int(hexcp, 16)
            if code > 0x10FFFF or chr(code) in seen:
                continue
            seen.add(chr(code))
            found.append(chr(code))
    return found


def print_characters(chars, indent='      ', width=78,
                     label='characters in the changed rules'):
    """`W (U+0057)  w (U+0077)`, wrapped, never splitting one character's
    entry. A character a terminal would not show -- a control character, a
    space -- is printed as its code point alone."""
    entries = [f'U+{c:X}' if isinstance(c, int)
               else f'{c} (U+{ord(c):04X})' if c.isprintable()
               and not c.isspace() else f'U+{ord(c):04X}' for c in chars]
    line = f'{indent}{label} ({len(chars)}):'
    sep = ' '
    for entry in entries:
        if len(line) + len(sep) + len(entry) > width:
            print(line)
            line = indent + entry
        else:
            line += sep + entry
        sep = '  '
    print(line)


def _named_under(ids, directory):
    """{file name: blob id} for the files directly under `directory` in a
    list_tree_ids map. A quoted path (a name git escapes) is left out: it
    cannot be read by that spelling, and a run that needs it finds it
    missing and says so (collation_keys.Side.closure)."""
    prefix = directory + '/'
    return {p[len(prefix):]: i for p, i in ids.items()
            if p.startswith(prefix) and '/' not in p[len(prefix):]}


class CharmapReads:
    """What step 2's one batch reads for the character set check, and the
    texts that come back (collation_keys).

    Always SUPPORTED at both tags. When a charmap differs between the tags
    -- by blob id, in the listing already made -- also those charmaps at
    both tags and every locale file not read for the verdicts: the search
    reads every LC_COLLATE block a build can reach. A locale file with the
    same blob at both tags is read once, at the new tag, and the old side
    shares the text.
    """

    def __init__(self, old, new, listed, ids, content_changed, new_paths):
        self.old, self.new, self.ids = old, new, ids
        self.cm = {t: _named_under(ids[t], ck.CHARMAPS_DIR) for t in (old, new)}
        self.loc = {t: _named_under(ids[t], g.LOCALES_DIR) for t in (old, new)}
        differ = sorted(c for c in self.cm[old].keys() & self.cm[new].keys()
                        if self.cm[old][c] != self.cm[new][c])
        self.groups = [(old, [g.SUPPORTED])]
        self.charmap_paths = [f'{ck.CHARMAPS_DIR}/{c}' for c in differ]
        self.all_read = bool(differ)
        if differ:
            self.groups += [(old, self.charmap_paths),
                            (new, self.charmap_paths)]
            have_new, have_old = set(new_paths), set(content_changed)
            self.groups.append((new, [
                f'{g.LOCALES_DIR}/{n}' for n in sorted(self.loc[new])
                if f'{g.LOCALES_DIR}/{n}' not in have_new]))
            self.groups.append((old, [
                f'{g.LOCALES_DIR}/{n}' for n, i in sorted(self.loc[old].items())
                if f'{g.LOCALES_DIR}/{n}' not in have_old
                and self.loc[new].get(n) != i]))

    def sides(self, repo, results, old_contents, new_contents, sup_new):
        """(old Side, new Side, SUPPORTED texts {tag: text or None}) once the
        batch is back. `results` holds this object's groups, in order. A
        file in the tree that comes back unread dies, as every read of a
        listed file does (require_read); one not in the tree is None."""
        old, new = self.old, self.new
        sup = {}
        sup_old = results[0]
        for tag, (contents, missing), present in (
                (old, sup_old, g.SUPPORTED in self.ids[old]),
                (new, sup_new, g.SUPPORTED in self.ids[new])):
            if present:
                g.require_read(tag, [g.SUPPORTED], missing,
                               'the character set check')
            sup[tag] = contents.get(g.SUPPORTED)
        cm_texts = {old: {}, new: {}}
        texts = {old: {}, new: {}}
        for path, text in old_contents.items():
            texts[old][os.path.basename(path)] = text
        for path, text in new_contents.items():
            texts[new][os.path.basename(path)] = text
        if self.all_read:
            for (tag, paths), (contents, missing) in zip(self.groups[1:],
                                                         results[1:]):
                g.require_read(tag, paths, missing,
                               'the character set check')
                for path, text in contents.items():
                    if path.startswith(ck.CHARMAPS_DIR + '/'):
                        cm_texts[tag][path.rsplit('/', 1)[1]] = text
                    else:
                        texts[tag][path.rsplit('/', 1)[1]] = text
            for n, i in self.loc[old].items():
                if n not in texts[old] and self.loc[new].get(n) == i:
                    texts[old][n] = texts[new][n]
        mk = lambda tag: ck.Side(tag, set(self.loc[tag]), texts[tag],
                                 self.cm[tag], cm_texts[tag])
        return mk(old), mk(new), sup

    def read_more(self, repo, side_old, side_new, charmaps):
        """Read `charmaps` at both tags where they are, and, if the batch
        read no locale file beyond the verdicts', every one, in one more
        `git cat-file --batch`."""
        old, new = self.old, self.new
        want = {old: [], new: []}
        for tag, side in ((old, side_old), (new, side_new)):
            want[tag] += [f'{ck.CHARMAPS_DIR}/{c}' for c in charmaps
                          if c in side.charmap_oids
                          and c not in side.charmap_texts]
            if not self.all_read:
                want[tag] += [f'{g.LOCALES_DIR}/{n}' for n in sorted(
                    self.loc[tag]) if n not in side.texts]
        groups = [(old, want[old]), (new, want[new])]
        for (tag, paths), (contents, missing), side in zip(
                groups, g.read_blobs_at(repo, groups), (side_old, side_new)):
            g.require_read(tag, paths, missing, 'the character set check')
            for path, text in contents.items():
                if path.startswith(ck.CHARMAPS_DIR + '/'):
                    side.charmap_texts[path.rsplit('/', 1)[1]] = text
                else:
                    side.texts[path.rsplit('/', 1)[1]] = text
        self.all_read = True


def check_charmaps(reads, repo, side_old, side_new, sup, names):
    """Run collation_keys.compare over the two tags, print what it found,
    and return the source names it lists for step 3."""
    old, new = side_old.tag, side_new.tag
    try:
        builds_old = (ck.supported_builds(sup[old], old)
                      if sup[old] is not None else None)
        builds_new = (ck.supported_builds(sup[new], new)
                      if sup[new] is not None else None)
    except ck.Unsettled as e:
        return _charmaps_not_run(str(e))
    if builds_old is None or builds_new is None:
        where = ' and '.join(t for t in (old, new) if sup[t] is None)
        return _charmaps_not_run(f"{g.SUPPORTED} does not exist at {where}")
    by_entry = {b.entry: b.charmap for b in builds_old}
    cross = {(by_entry[b.entry], b.charmap) for b in builds_new
             if b.entry in by_entry and by_entry[b.entry] != b.charmap}
    if cross:
        # A build whose entry stays and whose charmap does not. The batch
        # read only the charmaps whose blob changed under one name, so its
        # two are read here, in one more batch, with every locale file the
        # batch did not read if it read none.
        reads.read_more(repo, side_old, side_new,
                        sorted({a for a, _ in cross} | {b for _, b in cross}))

    def already_listed():
        # Step 3's own rule (resolve_copy_closure): the names step 2 passes
        # it, and every locale that copies one of them at the new tag.
        graph = g.copy_graph_from_texts(side_new.texts)
        return set(names) | set(g.inherited_from(graph, set(names)))

    report = ck.compare(side_old, side_new, builds_old, builds_new,
                        already_listed)
    print_charmap_report(report, old, new)
    return ck.listed_sources(report)


def _charmaps_not_run(reason):
    print()
    print(textwrap.fill(
        f"NOT RUN: the comparison of the character sets locales are built "
        f"with (localedata/charmaps/). {reason}. A rule that names a "
        f"character whose bytes changed in its charmap is not covered by "
        f"this result.",
        width=78, initial_indent='!! ', subsequent_indent='   '))
    return []


def _warn_block(head, items):
    """A `!!` block audit.sh repeats in its summary: the first line opens
    with `!!`, every other with three blanks or more (audit.sh's warnings
    block reads exactly that)."""
    print()
    print(textwrap.fill(head, width=78, initial_indent='!! ',
                        subsequent_indent='   '))
    for item in items:
        print(textwrap.fill(item, width=78, initial_indent='     ',
                            subsequent_indent='       '))


_CHANGED_IN_CHARMAP = 'characters its character set changed'


def _chars_phrase(f):
    via = ', '.join(f.via) if f.via else '?'
    n = len(f.chars)
    return f"{n} character{'s' if n != 1 else ''}, named in {via}"


def _print_chars(f, indent='      '):
    """f's characters, in code point order; one beyond U+10FFFF, which a
    `<U........>` name can spell, as its number."""
    cps = sorted(f.chars)
    print_characters([chr(c) if c < 0x110000 else c for c in cps],
                     indent=indent, label=_CHANGED_IN_CHARMAP)


def print_charmap_report(report, old, new):
    """The character set check's lines in step 2's output."""
    if report.not_run:
        _charmaps_not_run(report.not_run)
        return
    print()
    changed = report.changed
    if not report.builds:
        _warn_block("NOT RUN: the comparison of the character sets locales "
                    "are built with. No locale build is in SUPPORTED at both "
                    "tags with its source file at both, so none was "
                    "compared.", [])
    elif not changed:
        print(textwrap.fill(
            f"Character sets (localedata/charmaps/): the "
            f"{len(report.charmaps)} that the {report.builds} locale builds "
            f"in SUPPORTED at both tags use are identical at {old} and "
            f"{new}.", width=78))
    else:
        def size(p):
            name = p[1] if p[0] == p[1] else f"{p[0]} -> {p[1]}"
            if report.sizes[p] is None:
                return f"{name}: could not be compared (see below)."
            n, added, removed = report.sizes[p]
            other = n - added - removed
            parts = [f"{added} added"] * bool(added) + \
                [f"{removed} removed"] * bool(removed) + \
                [f"{other} given other bytes"] * bool(other)
            if not parts:
                # The file changed and no character's bytes did: a comment,
                # a WIDTH line, the order of two lines.
                return f"{name}: no character changed."
            parts[0] = parts[0].replace(
                ' ', ' character ' if parts[0].startswith('1 ')
                else ' characters ', 1)
            return f"{name}: {', '.join(parts)}."
        n = len(changed)
        print(textwrap.fill(
            f"Character sets (localedata/charmaps/): {n} of the "
            f"{len(report.charmaps)} that the {report.builds} locale builds "
            f"in SUPPORTED at both tags use {'differs' if n == 1 else 'differ'}"
            f" between {old} and {new}.", width=78))
        print(textwrap.fill(' '.join(size(p) for p in changed), width=78))
        if not report.found and not report.skipped:
            print(f"No {'compared ' if report.not_compared else ''}build's "
                  f"LC_COLLATE names one of those characters.")
    plain = [f for f in report.found if f.entry and not f.reason]
    if plain:
        print(textwrap.fill(
            f"Locale builds whose LC_COLLATE names one of those characters "
            f"({len(plain)}), listed for step 3:", width=78))
        for f in plain:
            print(f"  {f.entry} ({f.charmaps[1]}): {_chars_phrase(f)}")
            _print_chars(f)
    if report.skipped:
        print(textwrap.fill(
            f"Locale builds step 3 lists anyway, whose LC_COLLATE names one "
            f"of those characters or could not be compared "
            f"({len(report.skipped)}):", width=78))
        for f in report.skipped:
            what = f.entry or f"{f.source} (not in SUPPORTED in this charmap)"
            print(f"  {what} ({f.charmaps[1]}): "
                  + (f"could not be compared: {f.reason}" if f.reason
                     else _chars_phrase(f)))
    unsettled = [f for f in report.found if f.reason]
    if unsettled:
        _warn_block(f"{len(unsettled)} locale build(s) could not be compared "
                    f"for what their character set does to the characters "
                    f"their LC_COLLATE names, so they are listed for step 3:",
                    [f"{f.entry or f.source} ({f.charmaps[1]}): {f.reason}"
                     for f in unsettled])
    distro = [f for f in report.found if not f.entry and not f.reason]
    if distro:
        print()
        print(textwrap.fill(
            f"!! {len(distro)} locale(s) that SUPPORTED never builds in a "
            f"charmap that changed name one of the characters it changed. A "
            f"distro can build them in it (Red Hat builds en_US in "
            f"ISO-8859-15), so they are listed for step 3:",
            width=78, subsequent_indent='   '))
        for f in distro:
            print(f"     {f.source} ({f.charmaps[1]}): {_chars_phrase(f)}")
            _print_chars(f, indent='       ')
    if report.unsearched:
        _warn_block("The search for locales a distro can build in a changed "
                    "charmap, which SUPPORTED does not build in it, did not "
                    "run for these charmaps, so no such locale is listed for "
                    "them:", [f"{name}: {why}"
                              for name, why in report.unsearched])
    if report.not_compared:
        n = len(report.not_compared)
        _warn_block(f"{n} SUPPORTED entr{'y' if n == 1 else 'ies'} at both "
                    f"tags {'is' if n == 1 else 'are'} not compared: "
                    f"{'its' if n == 1 else 'their'} source file is not at "
                    f"both.", [', '.join(report.not_compared)])


def main(argv):
    ap = argparse.ArgumentParser(
        description="Filter changed locale files down to real LC_COLLATE "
                    "changes, and compare the charmaps the SUPPORTED builds "
                    "use.")
    ap.add_argument('old_tag')
    ap.add_argument('new_tag')
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    ap.add_argument('--allow-reverse', action='store_true',
                    help="run a pair whose new tag is the OLDER commit. Refused by default: reversed, every step still prints a plausible clean result. Prints a `!!` block saying the direction is reversed.")
    ap.add_argument('--diff-file',
                    help="a -U0 diff to check against the one git gives for "
                         "the two tags: refused unless it places every change "
                         "where git's does. The run still needs the clone, and "
                         "reads git's diff")
    opts = ap.parse_args(argv)

    # Each text's comment and escape characters, read once for this run:
    # classify_change and read_otherwise ask for the same texts, and the
    # character set check asks again. Confined to this step and this run,
    # and restored on the way out: steps 6/7 call the same reader, and a
    # saving there belongs to their own time gate (backlog 13.7, PR D).
    real_reading_chars, memo = g.reading_chars, {}

    def reading_chars(text):
        got = memo.get(text, memo)
        if got is memo:
            got = memo[text] = real_reading_chars(text)
        return got
    g.reading_chars = reading_chars
    try:
        return _main(opts)
    finally:
        g.reading_chars = real_reading_chars


def _main(opts):
    repo = g.find_repo(opts.repo)
    commits = g.check_refs(repo, opts.old_tag, opts.new_tag)

    # Which of the two is newer, asked of git rather than assumed. Reversed,
    # this step swaps its reassuring bucket for its noisy one: a locale
    # DELETED in the real upgrade is reported as "Added ... not analysed".
    g.require_pair_order(repo, opts.old_tag, opts.new_tag,
                         allow_reverse=opts.allow_reverse, commits=commits)
    rng = f'{opts.old_tag}..{opts.new_tag}'
    pathspec = g.LOCALES_DIR + '/'

    # The corpus floor. `git diff` over a pathspec that matches nothing at
    # either tag is empty and exits 0, and this step then reports "0 changed"
    # -- the node-reading modes refuse a directory that small, and a tag
    # deserves the same refusal. list_locale_files dies below the floor. The
    # lists are kept: they answer the KNOWN_BACKPORTED check below.
    # The same listing gives the blob ids the character set check compares,
    # for charmaps and SUPPORTED too, so it costs no git process.
    listed, ids = {}, {}
    for tag in (opts.old_tag, opts.new_tag):
        if tag not in listed:
            listed[tag], ids[tag] = g.list_tree_ids(
                repo, tag, [ck.CHARMAPS_DIR + '/', g.SUPPORTED])
    same_commit = commits[opts.old_tag] == commits[opts.new_tag]

    # One diff for both questions. Its --raw half is git's list of what
    # changed and how, and its -U0 half is the text every verdict reads.
    status, generated = split_raw(g.git_diff(
        repo, ['--patch-with-raw', '-U0', '--find-renames', rng,
               '--', pathspec]))

    # Classify every change first, so added/deleted/renamed files are reported
    # as such instead of vanishing into a `continue`.
    modified, added, deleted, renamed = [], [], [], []
    for line in status:
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
    renamed_to = {old_path: new_path for old_path, new_path in renamed}

    if opts.diff_file:
        with open(opts.diff_file, encoding='utf-8', errors='replace') as fh:
            diff_text = fh.read()
    else:
        diff_text = generated
    hunks = parse_diff(diff_text)

    # A --diff-file is read only if it places every change where git does.
    # The checks below catch a file with a path missing or with no hunk; a
    # file with FEWER hunks for a path, or hunks numbered on other text,
    # passed them all: one taken under a textconv that prepends lines, and
    # one with sv_SE's hunks inside LC_COLLATE cut, both lost sv_SE at exit 0.
    if opts.diff_file:
        expected = parse_diff(generated)
        differ = sorted(path for path in set(hunks) | set(expected)
                        if hunks.get(path) != expected.get(path))
        if differ:
            g.die(f"--diff-file places the changes of {len(differ)} file(s) "
                  f"differently from the diff git gives for {rng}, e.g. "
                  f"{', '.join(differ[:3])}.\n"
                  f"       It does not match these tags. Drop --diff-file and "
                  f"let this script generate it.")
        # The ranges are all the verdict reads, and they now match; the lines
        # under them are what the characters are read from, and a file with
        # git's headers and other lines printed another locale's characters
        # at exit 0. From here on, git's diff is the one read.
        diff_text = generated

    # The diff must actually cover the files git says changed. A --diff-file
    # that does not was refused above; what is left is git's own diff and
    # git's own list of changed files disagreeing, which a config this run
    # does not pin could do -- diff.srcPrefix did, before it was pinned.
    content_changed = modified + [old for old, _ in renamed]
    stale = [path for path in content_changed if path not in hunks]
    if stale:
        g.die(f"the diff does not cover {len(stale)} of the "
              f"{len(modified) + len(renamed)} file(s) git reports as changed "
              f"between {opts.old_tag} and {opts.new_tag}, e.g. "
              f"{', '.join(sorted(stale)[:3])}.\n"
              f"       git's diff and its list of changed files disagree, so "
              f"the diff was not read the way this script expects.")

    # KNOWN_BACKPORTED locales a tag-to-tag diff is structurally blind to on
    # the OLD side: no source file at the old tag means nothing to diff
    # against, whether or not the file shows up at the new one.
    #
    # Reported whenever the old side is missing, NOT only when the file happens
    # to be ADDED in this range. The silent case is the one that matters: over
    # 2.28 -> 2.34 (RHEL8 -> RHEL9) localedata/locales/C is in neither tag, so
    # nothing was printed at all -- for the pair where C.UTF-8 demonstrably
    # does change. Decided here and printed further down; the added files it
    # leaves are the ones that need SUPPORTED, read in the batch below.
    blind = []
    for name in sorted(KNOWN_BACKPORTED):
        path = f'{g.LOCALES_DIR}/{name}'
        if in_listing(path, listed[opts.old_tag]):
            continue          # present at the old tag: judged like any file
        blind.append((path, KNOWN_BACKPORTED[name],
                      in_listing(path, listed[opts.new_tag])))
    blind_paths = {path for path, _, _ in blind}
    rest = [path for path in added if path not in blind_paths]

    # Every file text this step reads, in one batch: the old side of every
    # content-changed file, its new side under the name it has at the new tag
    # (see new_side_paths) -- a file without a block could have gained one,
    # and one with a block could be read with other comment or escape
    # characters -- SUPPORTED, which an added file needs for its names and
    # the character set check at both tags, and what that check reads
    # (CharmapReads).
    to_read = new_side_paths(content_changed, renamed_to)
    new_paths = sorted(set(to_read.values()))
    groups = [(opts.old_tag, content_changed), (opts.new_tag, new_paths)]
    if rest or not same_commit:
        groups.append((opts.new_tag, [g.SUPPORTED]))
    charmap_reads = None
    if not same_commit:
        charmap_reads = CharmapReads(opts.old_tag, opts.new_tag, listed, ids,
                                     content_changed, new_paths)
        groups += charmap_reads.groups
    read = g.read_blobs_at(repo, groups)
    (old_contents, old_missing), (new_contents, new_missing) = read[:2]

    # Content-changed files are judged against their OLD LC_COLLATE bounds.
    # These paths come from git's own list of what changed, so every one of
    # them exists at the old tag, and a blob that comes back missing is a read
    # that failed -- a partial clone that cannot fetch. The message used to
    # blame a --diff-file that did not match the tags, which it cannot be.
    g.require_read(opts.old_tag, content_changed, old_missing,
                   'the LC_COLLATE bounds of the files that changed')

    # The general form of "Binary files": git reports a file as changed and
    # the diff holds no hunk for it. That is an answer only when both versions
    # are the same bytes -- a pure rename, a mode change -- and git says which
    # blob each tag holds without reading either. Anything else is a change
    # nothing read, and it would be judged 'other'.
    no_hunk = [path for path in content_changed if not hunks[path]]
    if no_hunk:
        specs = [spec for path in no_hunk
                 for spec in (f'{opts.old_tag}:{path}',
                              f'{opts.new_tag}:{renamed_to.get(path, path)}')]
        oids = g.run_git(['rev-parse', *specs], repo).stdout.decode().split()
        if len(oids) != len(specs):
            g.die(f"`git rev-parse` answered {len(oids)} of {len(specs)} "
                  f"blob ids for the files with no hunk in the diff.")
        unread = [path for path, before, after
                  in zip(no_hunk, oids[0::2], oids[1::2]) if before != after]
        if unread:
            g.die(f"{len(unread)} file(s) git reports as changed have no hunk "
                  f"in the diff, so the change was not read: "
                  f"{', '.join(sorted(unread)[:5])}"
                  f"{', ...' if len(unread) > 5 else ''}.")

    # Refused only now, after the check above, so a run that has both
    # failures still names the one it named before the reads were batched.
    g.require_read(opts.new_tag, new_paths, new_missing,
                   'the check for files that gained an LC_COLLATE block or '
                   'are read with other comment or escape characters')

    verdicts = judge(content_changed, old_contents, new_contents, hunks,
                     renamed_to)
    (changed_collate, gained_collate, reading_files,
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
    reread = read_otherwise(changed_collate, reading_files, old_contents,
                            new_contents, renamed_to)
    if reread:
        print(f"\n!! {len(reread)} file(s) are read at {opts.new_tag} with "
              f"other comment or escape")
        print(f"   characters than at {opts.old_tag}, or with characters "
              f"this tool cannot settle, so")
        print(f"   any rule in them can read differently. They are counted as "
              f"changed above:")
        for path in reread:
            new_text = new_contents.get(renamed_to.get(path, path))
            print(f"     {path}")
            print(f"       {g.describe_reading(old_contents[path], opts.old_tag)}")
            print(f"       " + (g.describe_reading(new_text, opts.new_tag)
                                if new_text is not None else
                                f"not read at {opts.new_tag}"))

    # Under each file, the characters its changed rules name: they are what
    # the confirmation template needs as test values. Indented six spaces and
    # with no blank line, so the list keeps parsing as one path per line. The
    # new side is the one already read, strictly, for the verdicts above.
    sections = diff_sections(diff_text)
    print(f"\nFiles with changes inside LC_COLLATE: {len(changed_collate)}")
    for path in sorted(changed_collate):
        print(f"  {path}")
        chars = changed_characters(sections.get(path, []), old_contents[path],
                                   new_contents.get(renamed_to.get(path, path)))
        if path in reread:
            if chars:
                print_characters(chars)
            print(textwrap.fill(
                ("those are the changed lines; " if chars else "")
                + "the block is read with other characters (see the `!!` "
                "above), so any rule in it can read differently",
                width=78, initial_indent='      ', subsequent_indent='      '))
        elif chars:
            print_characters(chars)
        else:
            print(textwrap.fill(
                "could not identify which characters changed, but this "
                "locale must be considered suspicious",
                width=78, initial_indent='      ', subsequent_indent='      '))

    # Step 3 walks the copy graph at the NEW tag, so it can only be given
    # names that exist there. A renamed file is judged against its OLD path
    # (content_changed, above), and that path is gone at the new tag -- handing
    # it to step 3 unchanged is an exit-2 abort on a legitimate finding. Map it
    # to the new name instead of dropping it: the ruleset moved, it did not
    # disappear, and the locale exposed at the new tag is the new one.
    for_step3, translated = [], []
    for path in changed_collate:
        landed = renamed_to.get(path)
        if landed:
            translated.append((path, landed))
        for_step3.append(os.path.basename(landed or path))
    names = sorted(set(for_step3))

    if translated:
        print(f"\n{len(translated)} of those file(s) were renamed, so step 3 "
              f"gets the name that exists at {opts.new_tag}:")
        for old_path, new_path in sorted(translated):
            print(f"  {os.path.basename(old_path)} -> "
                  f"{os.path.basename(new_path)}")

    # The character sets the locales are built with (collation_keys): a rule
    # identical at both tags sorts differently when its charmap gives a
    # character it names other bytes. What it lists goes to step 3 in a list
    # of its own, so step 3 does not call it an LC_COLLATE change.
    if charmap_reads is None:
        print("\nCharacter sets: not compared, the two tags are one commit.")
        keyed = []
    else:
        side_old, side_new, sup = charmap_reads.sides(
            repo, read[3:], old_contents, new_contents, read[2])
        keyed = check_charmaps(charmap_reads, repo, side_old, side_new, sup,
                               names)

    # Written whether or not it is empty, and named after the pair. audit.sh
    # reads this instead of the user retyping it, and an empty file for THIS
    # pair is a different fact from a leftover file for another one -- the
    # confusion this script's docstring exists to record.
    g.write_list(f"step2_changed_collate.{g.pair_slug(opts.old_tag, opts.new_tag)}.txt",
                 names)
    # Written every run too, for the same reason: the builds the character
    # set check lists, which step 3 reads with --keyed.
    g.write_list(f"step2_keyed_locales.{g.pair_slug(opts.old_tag, opts.new_tag)}.txt",
                 keyed)

    # Every file of the old tag that is not at the new one, which is what the
    # summary relays as removed when no node was read: deleted, or renamed
    # away. A renamed file's ruleset moved, but the NAME a database refers to
    # is gone, so it is listed under the old name with where it went. Written
    # whether or not it is empty, for the same reason as the list above.
    removed = sorted(
        [(os.path.basename(p), 'deleted') for p in deleted]
        + [(os.path.basename(old), f'renamed to {os.path.basename(new)}')
           for old, new in renamed])
    g.write_list(f"step2_removed_locales.{g.pair_slug(opts.old_tag, opts.new_tag)}.txt",
                 [f"# locale files at {opts.old_tag} and not at {opts.new_tag}"]
                 + [f"{name} ({how})" for name, how in removed])

    if (names or keyed) and not g.wrapped():
        print(f"\nLocale names for step 3:")
        print(f"  python3 resolve_copy_closure.py {opts.new_tag} "
              + ' '.join(names + (['--keyed'] + keyed if keyed else [])))

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
            f"The old tag has no source file for it, so a comparison of the "
            f"two tags cannot see what your old system runs, and a clean "
            f"result from steps 1 to 5 says nothing about the {locale_name} "
            f"it runs. PostgreSQL will not cover the gap either -- "
            f"collversion is NULL for every collation whose name starts with "
            f"'C.', so no version mismatch can ever fire. Compare "
            f"{locale_name} empirically on both nodes. See "
            f"docs/limitations.md.",
            width=78, initial_indent='!! ', subsequent_indent='   '))

    if rest:
        supported = g.parse_supported(read[2][0].get(g.SUPPORTED),
                                      opts.new_tag)
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
