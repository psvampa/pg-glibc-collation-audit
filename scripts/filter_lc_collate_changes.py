#!/usr/bin/env python3
"""
Narrow the list of changed locale files down to those whose change falls
INSIDE the LC_COLLATE...END LC_COLLATE block, or changes the comment or escape
characters that block is read with, or whose file holds something that can
stop glibc reading that block as written, discarding changes in LC_TIME,
LC_MONETARY, comments, etc. When the two tags' localedef differ, a file they
read differently is listed whether or not it changed, and so is one the reader
cannot settle.

It also compares, between the two tags, the charmaps the SUPPORTED builds
use and the collating elements they build out of transliterations
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
import collections
import os
import re
import sys
import textwrap
import threading

import collation_keys as ck
import glibc_locale_data as g
import glibc_tree
import reader_cache as rc

_FILE_HDR_RE = re.compile(r'^diff --git a/(\S+) b/(\S+)$', re.M)
_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', re.M)
_BOTH_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', re.M)
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


def _cannot_tell():
    return None


def classify_change(old_text, new_text, old, new, ranges, diff,
                    early=(_cannot_tell, _cannot_tell)):
    """How does one content-changed file relate to LC_COLLATE?

    `old` and `new` are the file's glibc_tree.Facts at the two tags: where
    localedef reads its LC_COLLATE and with which comment and escape
    characters, from what glibc_reader read (backlog 15.2 B3); `new` is
    None when the new side was not read. The texts are the lines those
    sections are cut from, and what lc_collate_unread searches. `ranges`
    are the old side's hunks (parse_diff) and `diff` the file's diff
    sections, -U0 (diff_sections), which place each section's lines on
    the other side (_line_map).

    Returns one of:
      'unsettled'      -- the reader could not settle how localedef reads
                          the file at one of the tags (Facts.unresolved);
                          asked first, because nothing below can be
                          answered for it
      'collate'        -- a hunk falls inside an LC_COLLATE section of the
                          old file, or localedef reads another number of
                          LC_COLLATE sections in the new one (each is read
                          into the same tables, ld-collate.c:1499@2.39), or
                          sections of other text: the same number, one of
                          them another, as when a line above swallows the
                          one section and a new one below is read instead
                          (round 4 of the 15.2 B3 review)
      'gained-collate' -- localedef read no LC_COLLATE section in the old
                          file and reads one in the new
      'reading'        -- as many sections, and a section both sides read
                          is read with another comment or escape character
                          (_read_otherwise)
      'unread'         -- no hunk falls inside the old sections, but
                          localedef reads no LC_COLLATE section in the new
                          file; or the block and its characters did not
                          change, and something in the file can stop glibc
                          reading the block as written on either side
                          (glibc_locale_data.lc_collate_unread, backlog
                          13.18); `early` is that function's question for
                          the old side and the new, and a caller that cannot
                          ask it gets "cannot tell", which counts as yes
      'other'          -- has a section, and neither it nor the characters
                          it is read with changed, and lc_collate_unread
                          finds nothing in the file that can stop glibc
                          reading it
      'no-collate'     -- no LC_COLLATE section on either side, so no sort
                          rules of its own; a transliteration it holds can
                          still change a collating element another locale
                          builds, which point 6 of collation_keys compares

    The sections are where glibc's own reader puts them, so an indented
    `LC_COLLATE` (backlog 2.21) and an `END LC_COLLATE` joined by the escape
    to the rule above it, which glibc reads as a weight and not as the end
    (13.25), are read as glibc reads them.

    `reading` is its own answer for the reason `gained-collate` is: the
    directives sit outside the block, so no hunk lands inside it, and the
    same block read with other characters is other rules (backlog 13.7). A
    new side nobody passed in cannot clear that, so a file with a block and
    no new side is 'reading' too; main() reads every new side.

    `gained-collate` is its own answer rather than being filed under
    "no LC_COLLATE block". A file that acquires one changes its sort order by
    definition -- it had none and now has rules -- so folding it in with the
    files that never had one would hide a real collation change behind a count.
    It has never happened in glibc between 2.17 and 2.42, which is exactly why
    it needs a name: an unguarded path that nothing exercises is one nobody
    notices when it finally fires.

    Pure so that each answer can be tested by handing it Facts, rather than
    by fabricating a glibc clone in which it occurs.
    """
    if old.unresolved or (new is not None and new.unresolved):
        return 'unsettled'
    if not old.collate:
        if new is not None and new.collate:
            return 'gained-collate'
        return 'no-collate'
    if any(hunk_touches_block(s, ln, start,
                              end if end is not None else float('inf'))
           for start, end, _, _, _ in old.collate for s, ln in ranges):
        return 'collate'
    if new is not None and not new.collate:
        return 'unread'
    if new is not None and _sections(old) != _sections(new):
        return 'collate'
    if new is None or (new_text is not None and _read_otherwise(
            old, old_text, new, new_text, diff)):
        return 'reading'
    # After 'reading': the characters a section is read with are its text
    # too, and a change to them has its own answer.
    if new_text is not None and _sections_differ(old, old_text, new,
                                                 new_text):
        return 'collate'
    # A new text nobody read cannot be asked, and "could not look" keeps
    # the file listed; main() reads every new side.
    if (g.lc_collate_unread(old_text, early[0]) or new_text is None
            or g.lc_collate_unread(new_text, early[1])):
        return 'unread'
    return 'other'


def _sections(facts):
    return len({(start, end) for start, end, _, _, _ in facts.collate})


def _sections_differ(old, old_text, new, new_text):
    """Does a run both sides have read other lines as this file's
    LC_COLLATE sections? As _read_otherwise compares the characters: a
    run on one side only is another locale copying the file
    or no longer copying it, not a change to what the file says."""
    def texts(facts, text):
        lines, out = text.split('\n'), {}
        for start, end, _, _, run in facts.collate:
            out.setdefault(run, []).append(lines[start - 1:end])
        return out
    a, b = texts(old, old_text), texts(new, new_text)
    return any(a[run] != b[run] for run in a.keys() & b.keys())


def _sections_moved(old, old_text, new, new_text, diff):
    """Does a run both sides have read, as this file's LC_COLLATE, lines
    the diff did not change and that it did not read before, or no longer
    read lines it did? The lines a hunk changed are left out on both
    sides: a rule edited inside the same section is the changed lines'
    business (changed_characters), and comparing whole texts named it as
    a section read in place of another (doc-sweep of 15.2 B3). `diff` is
    the file's diff sections, -U0."""
    to_new, added = _line_map(diff)
    for run in _common_runs(old, new):
        moved = {to_new(n) for n in _run_lines(old, old_text, run)} - {None}
        kept = set(_run_lines(new, new_text, run)) - added
        if moved != kept:
            return True
    return False


def _line_map(diff):
    """(to_new, added) from a file's diff sections, -U0: to_new gives the
    new side's number of an old line the diff did not change, or None for
    one it removed or replaced; `added` holds the new side's lines a hunk
    wrote."""
    # findall gives '' for a length git leaves out, which means 1.
    hunks = sorted((int(a), int(b) if b else 1, int(c), int(d) if d else 1)
                   for text in diff for a, b, c, d in _BOTH_RE.findall(text))
    added = {n for _, _, c, d in hunks for n in range(c, c + d)}

    def to_new(line):
        shift = 0
        for a, b, c, d in hunks:
            if b and a <= line < a + b:
                return None
            if (b and a + b <= line) or (not b and a < line):
                shift += d - b
        return line + shift
    return to_new, added


def _run_lines(facts, text, run):
    """{line: (comment, escape)} for every line of the LC_COLLATE sections
    `run` reads in this file, with the pair it reads that section with."""
    last = text.count('\n') + 1
    return {n: (c, e) for start, end, c, e, r in facts.collate if r == run
            for n in range(start, (end if end is not None else last) + 1)}


def _common_runs(old, new):
    return ({r for _, _, _, _, r in old.collate}
            & {r for _, _, _, _, r in new.collate})


def _read_otherwise(old, old_text, new, new_text, diff):
    """Does a run both sides have read a line the diff did not change,
    inside this file's LC_COLLATE at both tags, with another comment or
    escape character? Line by line through _line_map, never the pairs as
    a whole: a section that goes with a pair of its own leaves the others
    read as before, and the `!!` would name the wrong cause (backlog 13.36
    (a)); a run that reads one section with another pair and gains a
    second one is still found, as are two runs that swap their pairs
    (round 2 of the 15.2 B3 review). A section of changed lines only is
    the changed lines' business: each is read with its own side's
    characters (changed_characters)."""
    to_new, _ = _line_map(diff)
    for run in _common_runs(old, new):
        after = _run_lines(new, new_text, run)
        for n, pair in _run_lines(old, old_text, run).items():
            m = to_new(n)
            if m is not None and m in after and after[m] != pair:
                return True
    return False


def partition_verdicts(verdicts):
    """Group {path: verdict} into the lists the report prints.

    Separate from classify_change() because deciding what a file IS and
    deciding what to DO about it are different mistakes. Mutation testing found
    that the hard way: with only the classifier under test, dropping the line
    that folds `gained-collate` into the changed list left the whole suite
    green -- the verdict was computed correctly and then thrown away.

    Returns (changed, gained, reading, unsettled, unchanged, no_collate).
    `gained`, `reading` and `unsettled` each appear BOTH in their own list,
    so the report can call them out, and inside `changed`: a file that
    acquires an LC_COLLATE block acquires a sort order, one whose block is
    read with other characters has other rules, and one the reader could
    not settle is never cleared. `unread` goes into `changed` only: its `!!`
    is built from every changed file (unread_above), not from this verdict,
    and names the files whose new side reads no LC_COLLATE section too.
    """
    changed, gained, reading, unsettled, unchanged, no_collate = (
        [], [], [], [], [], [])
    for path, verdict in verdicts:
        if verdict in ('collate', 'unread'):
            changed.append(path)
        elif verdict == 'unsettled':
            unsettled.append(path)
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
    return changed, gained, reading, unsettled, unchanged, no_collate


def new_side_paths(content_changed, renamed_to):
    """Which paths to read at the NEW tag, and under which name.

    Every content-changed file: which lines its LC_COLLATE sections hold
    and what can stop glibc reading them are asked of the new text as well
    (classify_change, unread_above). Until backlog 13.7 only a file with no
    block on the old side was read there, so the others were judged on the
    old side alone. A renamed file lives under its NEW name there: reading
    the old name aborted the whole step with "could not read" on a
    legitimate rename, and looking the
    verdict up under the old name made `gained-collate` undetectable for any
    renamed file. Neither has happened in an audited pair -- the one rename,
    aa_ER@saaho to ssy_ER over 2.34..2.39, has a block on the old side --
    which is exactly why it is a function with a test rather than two lookups
    in main().

    Returns {old_path: new_path} for the files to read.
    """
    return {p: renamed_to.get(p, p) for p in content_changed}


def facts_of(facts, renamed_to, path):
    """(old Facts, new Facts or None) of a changed file: `facts` is the
    pair of {name: Facts} glibc_tree.file_facts gives for the two tags, and
    a renamed file is looked up under its name at the new tag. Every
    content-changed file is in the old tag's listing, so a name missing
    there dies rather than be judged on nothing.

    A Facts row names the run that read it, the locale built: a locale
    renamed between the tags is two names for one run, and every
    comparison by run (_read_otherwise, _sections_moved, _sections_differ)
    would find it on
    one side only and compare nothing for it. So every renamed run on the
    new side takes its old name, the file's own and any locale that copies
    it (rounds 5 and 6 of the 15.2 B3 review). git reports a rename only
    when the old name is gone at the new tag, so no run there already
    holds it. What each line names (Facts.chars) goes with them: it is
    the same file's, whatever the run is called; a run the reader
    stopped in (Facts.stopped) is renamed with the rest."""
    name = os.path.basename(path)
    if name not in facts[0]:
        g.die(f"{path} changed but is not among the locale files listed at "
              f"the old tag, so how glibc reads it was not looked at.")
    new = facts[1].get(os.path.basename(renamed_to.get(path, path)))
    back = {os.path.basename(n): os.path.basename(o)
            for o, n in renamed_to.items()}
    if new is not None and back:
        new = glibc_tree.Facts([r[:4] + (back.get(r[4], r[4]),)
                                for r in new.collate], new.unresolved,
                               new.instructions, new.chars,
                               [back.get(b, b) for b in new.stopped],
                               new.load)
    return facts[0][name], new


def judge(content_changed, old_contents, new_contents, hunks, sections,
          renamed_to, facts, early=(_cannot_tell, _cannot_tell)):
    """[(old_path, verdict)] for every content-changed file.

    `new_contents` is keyed by the path at the NEW tag, as read_blobs_at
    returns it; the lookup goes through `renamed_to` so a renamed file finds
    its own new text, and its own Facts (facts_of). `hunks` is parse_diff's
    answer and `sections` diff_sections'. `early` goes to classify_change
    as it is.
    """
    return [(path, classify_change(old_contents[path],
                                   new_contents.get(renamed_to.get(path, path)),
                                   *facts_of(facts, renamed_to, path),
                                   hunks.get(path, []),
                                   sections.get(path, []), early))
            for path in content_changed]


def predates_lr_getc_fix(repo, tag):
    """A zero-argument question: does `tag`'s localedef read 0x1A and 0xFF
    as the end of the file? True, False, or None when git cannot place
    glibc_locale_data.LR_GETC_FIX (a shallow clone, a fabricated
    repository). main asks it of both tags before anything is judged and
    refuses a None (glibc_tree.builds_at), and two "predates" on a shallow
    clone; lc_collate_unread asks it again, and would count None as yes.
    Asked of git at most once per tag."""
    box = []

    def ask():
        if not box:
            got = g.descends_from(repo, g.LR_GETC_FIX, tag)
            box.append(None if got is None else not got)
        return box[0]
    return ask


def lr_getc_sides(repo, early, tags):
    """{tag: predates LR_GETC_FIX} for the two tags, which decides how
    their localedef reads 0x1A and 0xFF. A tag git cannot place is left as
    None, and glibc_tree.builds_at refuses it before anything is read. Two
    "predates" on a shallow clone are refused here: git can answer no
    there for a tag that holds the fix (round 2 of the 13.18 review), as
    reads_utf8 refuses."""
    pre = {tag: ask() for tag, ask in zip(tags, early)}
    if all(v is True for v in pre.values()):
        g.require_full_history(
            repo, 'step 2', 'git cannot say which tag holds the commit from '
            'which localedef reads 0x1A and 0xFF as ordinary bytes')
    return pre


def unread_above(changed, old_contents, new_contents, renamed_to, early,
                 tags, facts=None, sections=None):
    """[(old_path, [why, ...])], sorted, for every changed file whose
    LC_COLLATE something in it can stop glibc reading as written at
    either tag (glibc_locale_data.lc_collate_unread), or that localedef
    reads an LC_COLLATE section in at the old tag and none in at the new
    one, or other lines as its sections (`facts`, the pair main has; the
    'unread' verdict of classify_change and the lines it reads as
    sections, which the line search behind lc_collate_unread does not
    always see, though not their number, backlog 13.36 (b); doc-sweep of
    15.2 B3; `sections`, diff_sections' answer, says which lines are
    the same lines). Not only the 'unread' verdict,
    for read_otherwise's reason: a file whose block also changed is
    'collate', and the characters printed for it name only its changed
    lines, while glibc may not read the block as written. `early` and
    `tags` are (old, new); a side with no text is not asked."""
    out = []
    for path in changed:
        texts = (old_contents[path],
                 new_contents.get(renamed_to.get(path, path)))
        why = [f"at {tag}: {reason}" for text, ask, tag
               in zip(texts, early, tags) if text is not None
               for reason in [g.lc_collate_unread(text, ask)] if reason]
        if facts is not None:
            old, new = facts_of(facts, renamed_to, path)
            if old.collate and new is not None and not new.collate \
                    and not any(w.startswith(f"at {tags[1]}:") for w in why):
                why.append(f"at {tags[1]}: localedef reads no LC_COLLATE "
                           f"section in it")
            elif old.collate and new is not None and new.collate \
                    and texts[1] is not None and sections is not None \
                    and _sections_moved(old, texts[0], new, texts[1],
                                        sections.get(path, [])):
                # The characters of the changed lines are not all that
                # moves: a section swallowed by a line above it loses
                # every rule, and those lines did not change (doc-sweep of
                # 15.2 B3). Another number of sections is not asked here:
                # a section of added or removed lines is the changed
                # lines' business, and a changed line that starts or ends
                # one is an instruction (instruction_changed; backlog
                # 13.36 (b)).
                why.append(f"at {tags[1]}: localedef reads other lines as "
                           f"its LC_COLLATE than at {tags[0]}")
        if why:
            out.append((path, why))
    return sorted(out)


def read_across(repo, tag, builds, paths, ids):
    """Every locale at `tag` that the localedef of the two tags builds
    differently, sorted (glibc_tree.across): before glibc 2.36 lr_getc
    reads 0x1A, and 0xFF through a signed char, as the end of the file,
    from 2.36 a token that opens with a byte above 0x7F is a UTF-8
    character, and a word one tag's keyword table has and the other's has
    not (codepoint_collation from 2.35) is read otherwise. A run that can
    be read differently is read whole with both tags' Builds (`builds`, old
    and new): an 0xFF that ends an ifdef name picks the other branch with
    the same number of statements, so nothing less than every token is
    compared (false-negative-reviewer, 15.2 B3). Asked of every pair, not
    only one that crosses 2.36: two tags on one side of it can still have
    other keyword tables (round 3 of the same review). Step 2 judges the
    changed files by their diff and these by how they are read; main lists
    the ones that did not change and folds the changed ones in
    (fold_across). [] at once when the two Builds are the same. Two
    "predates" on a shallow clone, where git can answer no for a tag that
    holds LR_GETC_FIX, are refused before anything is read (lr_getc_sides).
    `paths` and `ids` are list_tree_ids' answer for `tag`: a kept answer is
    found by the blob ids alone."""
    if rc.build_id(builds[0]) == rc.build_id(builds[1]):
        return []

    def load():
        got, missing = g.read_blobs(repo, tag, paths, raw=True)
        g.require_read(tag, paths, missing, "the check for files the two "
                       "tags' localedef read differently")
        return {glibc_tree._key(os.path.basename(p)): v
                for p, v in got.items()}
    names = set(glibc_tree.across(
        {os.path.basename(p): ids[p] for p in paths}, load, *builds))
    return sorted(p for p in paths if os.path.basename(p) in names)


def fold_across(changed, unchanged, no_collate, across):
    """(changed, unchanged, no_collate, folded): a changed file the two
    tags' localedef read differently (read_across) is a change to its sort
    order whatever its diff touched, so one filed as not touching
    LC_COLLATE, or as having none, moves into `changed` and into `folded`,
    which names it. Separate from partition_verdicts because deciding what
    a file is and acting on it are different mistakes."""
    folded = sorted(p for p in unchanged + no_collate if p in across)
    return (changed + folded, [p for p in unchanged if p not in across],
            [p for p in no_collate if p not in across], folded)


def read_otherwise(changed, reading, facts, renamed_to, old_contents,
                   new_contents, sections):
    """The changed files read with other characters at the new tag, sorted.

    Not only the 'reading' verdict. A hunk inside the block returns
    'collate' before the characters are compared, and the characters
    printed for such a file name only its changed lines, while every rule in
    it can read differently -- the list would read as complete. A file with
    an LC_COLLATE section and no new side counts too: nothing settled its
    characters. The texts and `sections` (diff_sections) are what
    _read_otherwise places each line with.
    """
    out = []
    for path in changed:
        old, new = facts_of(facts, renamed_to, path)
        new_text = new_contents.get(renamed_to.get(path, path))
        if path in reading or (old.collate and (
                new is None or new_text is None or (
                    new.collate and _read_otherwise(
                        old, old_contents[path], new, new_text,
                        sections.get(path, []))))):
            out.append(path)
    return sorted(out)


def _byte(b):
    return chr(b) if 0x21 <= b < 0x7f else f'0x{b:02X}'


def describe_reading(facts, where):
    """`comment_char % and escape_char / at <where>`, for a report line:
    the characters localedef reads the file's LC_COLLATE with, one entry
    per distinct pair when it is read with more than one."""
    pairs = facts.reading()
    if not pairs:
        return f"no LC_COLLATE read at {where}"
    return ' and '.join(f"comment_char {_byte(c)} and escape_char {_byte(e)}"
                        for c, e in pairs) + f" at {where}"


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


def changed_characters(sections, old, new):
    """The characters named on the changed lines inside LC_COLLATE, in order.

    `old` and `new` are the file's glibc_tree.Facts at the two tags. A
    removed line is placed by its old line number against the old sections
    and an added line by its new line number against the new ones, so a
    hunk that crosses a section's edge contributes only the lines inside
    it, and a file that gained its section is read from the new one. What
    a line names is what glibc_reader read there, in every run of its tag
    that reads the file (Facts.characters): its tokens, each read with the
    comment and escape characters that run reads the line with, so a
    comment names nothing and a comment character inside a <name> or a
    string cuts nothing (backlog 13.29); a weight string's <U....> names;
    the characters of a collating-element's `from`, read translated as
    glibc reads it; nothing from a statement glibc refuses in its state,
    which it drops unread (glibc_reader.Collate.characters).

    Returns [] when the lines name no character and also when this cannot
    tell -- no new side, the path does not have exactly one section in the
    diff, a hunk header does not parse, a character map that cannot be
    read, a line the reader cannot say that of (Facts.characters). The
    caller never prints an empty list: the locale stays flagged, with the
    could-not-identify warning or a line saying every rule in it can
    change.
    """
    lines = _changed_lines(sections, old, new)
    if lines is None:
        return []
    seen, found = set(), []
    for facts, n in lines:
        cps = facts.characters(n)
        if cps is None:
            return []
        for code in cps:
            if code > 0x10FFFF or chr(code) in seen:
                continue
            seen.add(chr(code))
            found.append(chr(code))
    return found


def instruction_changed(sections, old, new):
    """Does a changed line inside LC_COLLATE decide how other lines are
    read or where other rules sort? Placed as changed_characters places
    it, a line is one when the reader read it, on its own side, as an
    instruction
    (glibc_tree.Facts.instruction: a reorder-after, an order_start, a
    conditional, a copy, a range, a symbol placed in the order ...), or
    when it starts or ends one of the sections: glibc starts each
    LC_COLLATE section reading from state 0 (`int state = 0`,
    ld-collate.c:2558@2.34), so a section split in two reads the lines
    after the split otherwise. Then rules move on lines that did not
    change, and the characters of the changed lines are not all the test
    values (backlog 13.36). False when the diff cannot be placed; the
    caller then prints that the characters are not known."""
    lines = _changed_lines(sections, old, new)
    return lines is not None and any(
        facts.instruction(n) or any(n in (start, end)
                                    for start, end, _, _, _ in facts.collate)
        for facts, n in lines)


def _changed_lines(sections, old, new):
    """[(Facts, line)] for every changed line inside LC_COLLATE: a
    removed line by its old number against the old sections, an added one
    by its new number against the new. None when this cannot tell: no new
    side, the path does not have exactly one section in the diff, or a
    hunk header does not parse."""
    if new is None or len(sections) != 1:
        return None
    facts = {'-': old, '+': new}
    line_no = None
    out = []
    for line in sections[0].split('\n'):
        if line.startswith('@@'):
            m = _HUNK_LINE_RE.match(line)
            if not m:
                return None
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
        if facts[side].inside(n):
            out.append((facts[side], n))
    return out


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
        self._early = self._ahead = None
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
        got = self._sides(results, old_contents, new_contents, sup_new,
                          strict=True)
        self._early = None
        return got

    def prefetch(self, repo, results, old_contents, new_contents, sup_new):
        """Start reading, in a thread, the charmaps the transliteration
        check (collation_keys point 6) will read, while the step judges its
        verdicts: one `git cat-file`, about 20 ms on the Mac, that would
        otherwise be waited for. Only when the batch read every locale file
        and nothing came back unread; otherwise read_more reads them when
        they are needed, and sides() names whatever is missing, as before.
        The read goes through g.read_blobs_at like every other."""
        if not self.all_read:
            return
        got = self._sides(results, old_contents, new_contents, sup_new,
                          strict=False)
        if got is None:
            return
        side_old, side_new, sup = got
        try:
            builds = [ck.supported_builds(sup[t], t) for t in
                      (self.old, self.new) if sup[t] is not None]
        except ck.Unsettled:
            return
        if len(builds) != 2:
            return
        need = ck.translit_charmaps(side_old, side_new, *builds)
        groups = [(side.tag, [f'{ck.CHARMAPS_DIR}/{c}' for c in sorted(
                      need.get(side.tag, ())) if c in side.charmap_oids
                      and c not in side.charmap_texts])
                  for side in (side_old, side_new)]
        if not any(paths for _, paths in groups):
            return
        box = {}

        def work():
            try:
                box['read'] = g.read_blobs_at(repo, groups)
            except BaseException as e:
                box['error'] = e
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._early = got
        self._ahead = (thread, groups, box)

    def _join(self, side_old, side_new):
        """The prefetched charmaps into the Sides, or what the read
        raised (die's SystemExit included)."""
        ahead, self._ahead = self._ahead, None
        if ahead is None:
            return
        thread, groups, box = ahead
        thread.join()
        if 'error' in box:
            raise box['error']
        for (tag, paths), (contents, missing), side in zip(
                groups, box['read'], (side_old, side_new)):
            g.require_read(tag, paths, missing, 'the character set check')
            for path, text in contents.items():
                side.charmap_texts[path.rsplit('/', 1)[1]] = text

    def _sides(self, results, old_contents, new_contents, sup_new, strict):
        if self._early is not None:
            return self._early
        old, new = self.old, self.new
        sup = {}
        sup_old = results[0]
        for tag, (contents, missing), present in (
                (old, sup_old, g.SUPPORTED in self.ids[old]),
                (new, sup_new, g.SUPPORTED in self.ids[new])):
            if present:
                if not strict and missing:
                    return None
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
                if not strict and missing:
                    return None
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
        self._join(side_old, side_new)
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


def check_charmaps(reads, repo, side_old, side_new, sup, names, new_tree):
    """Run collation_keys.compare over the two tags, print what it found,
    and return the source names it lists for step 3. `new_tree()` gives the
    glibc_tree.Tree of the new tag, asked only if compare needs step 3's
    list."""
    old, new = side_old.tag, side_new.tag
    # The charmaps read ahead of time (CharmapReads.prefetch) are back by
    # now: the verdicts above took far longer than one `git cat-file`.
    # Joined first, so that a git that failed says so here, whatever path
    # the check takes.
    reads._join(side_old, side_new)
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
        # Step 3's own rule (resolve_copy_closure), on the same tree: the
        # names step 2 passes it, every locale whose run at the new tag
        # reaches one of them through `copy`, and every locale whose run
        # is not settled. Only when step 2 passes it a name: audit.sh runs
        # step 3 only then, or when this check lists one, so with no name
        # a locale whose run is not settled is listed here, never taken as
        # listed by a step that does not run.
        if not names:
            return set()
        tree, roots = new_tree(), set(names)
        return (roots | {n for n in tree.names if tree.roots(n, roots)}
                | set(tree.unresolved()))

    def read_more(charmaps, locales=False):
        # Point 6 of collation_keys: the charmaps of the builds whose
        # collating elements may be transliterated, and, when no charmap
        # changed, every locale file the batch did not read.
        names = sorted(set().union(*charmaps.values())) if charmaps else []
        if names or locales:
            reads.read_more(repo, side_old, side_new, names)

    for side in (side_old, side_new):
        # Asked when compare needs to know whether the two tags read bytes
        # alike (collation_keys._readers_differ).
        side.ask_utf8 = (lambda tag=side.tag: reads_utf8(repo, tag))
    report = ck.compare(side_old, side_new, builds_old, builds_new,
                        already_listed, read_more, fresh=False)
    print_charmap_report(report, old, new)
    return ck.listed_sources(report)


def reads_utf8(repo, tag, step='step 2'):
    """Does `tag` hold collation_keys.UTF8_READER: True, False, or None when
    git cannot place it. A shallow clone can answer no, or nothing, for a
    tag that holds it, and _readers_differ would read two such sides as one
    reader (round 7 of the 13.7 review): it is refused, as step 5 refuses
    it. A yes is a path git found in the history it has, so it stands on
    any clone, and the check (a git process) is not paid for it. Steps 6
    to 8 ask it of the tag a machine's glibc is built from (node_charmaps),
    and name themselves in the refusal."""
    got = g.descends_from(repo, ck.UTF8_READER, tag)
    if got is not True:
        g.require_full_history(
            repo, step, 'git cannot say which tag holds the commit from '
            'which localedef reads its input as UTF-8')
    return got


# What the character set check's lines say about the two sides, which are
# two tags in step 2 and a machine and its tag, or two machines, in steps 6
# to 8 (node_charmaps). Each is formatted with old= and new=, the Sides'
# names.
Words = collections.namedtuple(
    'Words', 'subject label builds identical between listed_for '
             'lists_anyway none_compared not_compared changed_between at_new')
STEP2_WORDS = Words(
    subject="the character sets locales are built with",
    label="localedata/charmaps/",
    builds="in SUPPORTED at both tags",
    identical="are identical at {old} and {new}",
    between="between {old} and {new}",
    listed_for="listed for step 3",
    lists_anyway="step 3 lists anyway",
    none_compared="No locale build is in SUPPORTED at both tags with its "
                  "source file at both, so none was compared.",
    not_compared="{n} SUPPORTED entr{y_ies} at both tags {is_are} not "
                 "compared: {its_their} source file is not at both.",
    changed_between="between the tags",
    at_new="at {new}")


def _said(words, field, old, new, **more):
    return getattr(words, field).format(old=old, new=new, **more)


def _charmaps_not_run(reason, subject=STEP2_WORDS.subject,
                      label=STEP2_WORDS.label, split_words=True):
    """The check's NOT RUN. `split_words=False` keeps a path in the reason
    whole, as glibc_locale_data.warn does, so that it can be copied."""
    print()
    print(textwrap.fill(
        f"NOT RUN: the comparison of {subject} ({label}). {reason}. A rule "
        f"that names a character whose bytes changed in its charmap, and a "
        f"collating element built out of a transliteration, are not covered "
        f"by this result.",
        width=78, initial_indent='!! ', subsequent_indent='   ',
        break_on_hyphens=split_words, break_long_words=split_words))
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


def print_charmap_report(report, old, new, words=STEP2_WORDS):
    """The character set check's lines in step 2's output, and in steps 6
    to 8 with their own `words`."""
    w = lambda field, **more: _said(words, field, old, new, **more)
    if report.not_run:
        _charmaps_not_run(report.not_run, w('subject'), w('label'))
        return
    print()
    changed = report.changed
    if not report.builds:
        _warn_block(f"NOT RUN: the comparison of {w('subject')}, and of the "
                    f"collating elements they build out of "
                    f"transliterations. {w('none_compared')}", [])
    elif not changed:
        print(textwrap.fill(
            f"Character sets ({w('label')}): the "
            f"{len(report.charmaps)} that the {report.builds} locale builds "
            f"{w('builds')} use {w('identical')}.", width=78))
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
            f"Character sets ({w('label')}): {n} of the "
            f"{len(report.charmaps)} that the {report.builds} locale builds "
            f"{w('builds')} use {'differs' if n == 1 else 'differ'}"
            f" {w('between')}.", width=78))
        print(textwrap.fill(' '.join(size(p) for p in changed), width=78))
        if not report.found and not report.skipped:
            print(f"No {'compared ' if report.not_compared else ''}build's "
                  f"LC_COLLATE names one of those characters.")
    plain = [f for f in report.found if f.entry and not f.reason]
    if plain:
        print(textwrap.fill(
            f"Locale builds whose LC_COLLATE names one of those characters "
            f"({len(plain)}), {w('listed_for')}:", width=78))
        for f in plain:
            print(f"  {f.entry} ({f.charmaps[1]}): {_chars_phrase(f)}")
            _print_chars(f)
    if report.skipped:
        print(textwrap.fill(
            f"Locale builds {w('lists_anyway')}, whose LC_COLLATE names one "
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
                    f"their LC_COLLATE names, so they are "
                    f"{w('listed_for')}:",
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
    print_translit_report(report, new, words, old)
    if report.not_compared:
        n = len(report.not_compared)
        one = n == 1
        _warn_block(w('not_compared', n=n, y_ies='y' if one else 'ies',
                      s='' if one else 's', is_are='is' if one else 'are',
                      its_their='its' if one else 'their'),
                    [', '.join(report.not_compared)])


def print_translit_report(report, new, words=STEP2_WORDS, old=None):
    """Point 6 of collation_keys: the collating elements built out of
    transliterations."""
    w = lambda field: _said(words, field, old, new)
    t = report.translit
    if not report.builds:
        return
    if t is None:
        print(textwrap.fill(
            f"Transliteration (LC_CTYPE): no character set and no locale "
            f"file's LC_CTYPE changed {w('changed_between')}, so no "
            f"collating element is built differently.", width=78))
        return
    plain = [f for f in t.found if not f.reason]
    unsettled = [f for f in t.found if f.reason]
    if t.elements:
        n = t.changed if t.changed is not None else sum(
            f.changed for f in plain)
        line = (f"Transliteration (LC_CTYPE): {t.elements} collating "
                f"element{'s' if t.elements != 1 else ''} of {t.builds} "
                f"locale build{'s' if t.builds != 1 else ''} "
                f"depend{'' if t.elements != 1 else 's'} on a "
                f"transliteration; "
                + (f"{n or 'none'} {'is' if n < 2 else 'are'} built "
                   f"differently {w('at_new')}."))
    elif t.unmoved:
        u = t.unmoved
        line = (f"Transliteration (LC_CTYPE): {u} locale build"
                f"{'s' if u != 1 else ''} may build a collating element out "
                f"of a transliteration, and nothing "
                f"{'they read' if u != 1 else 'it reads'} changed "
                f"{w('changed_between')}.")
    elif not unsettled and not report.translit_skipped:
        line = ("Transliteration (LC_CTYPE): no compared locale build "
                "builds a collating element out of a transliteration.")
    elif not unsettled:
        k = report.translit_skipped
        line = (f"Transliteration (LC_CTYPE): the {k} locale build"
                f"{'s' if k != 1 else ''} that may build a collating "
                f"element out of a transliteration "
                f"{'are' if k != 1 else 'is'} listed already, so none was "
                f"compared.")
    else:
        line = None
    if line:
        print(textwrap.fill(line, width=78))
    if plain:
        print(textwrap.fill(
            f"Locale builds whose collating elements are built differently "
            f"({len(plain)}), {w('listed_for')}:", width=78))
        for f in plain:
            print(f"  {f.entry} ({f.charmaps[1]}): {f.changed} "
                  f"element{'s' if f.changed != 1 else ''}, defined in "
                  f"{', '.join(f.via)}")
            print_characters([chr(c) if c < 0x110000 else c
                              for c in sorted(f.chars)],
                             indent='      ', label='transliterated characters')
    if report.translit_skipped and (t.elements or t.unmoved or unsettled):
        n = report.translit_skipped
        print(textwrap.fill(
            f"{n} more locale build{'s' if n != 1 else ''} with collating "
            f"elements built differently, or not compared, "
            f"{'are' if n != 1 else 'is'} listed already.", width=78))
    if unsettled:
        _warn_block(f"{len(unsettled)} locale build(s) could not be compared "
                    f"for the collating elements they build out of "
                    f"transliterations, so they are {w('listed_for')}:",
                    [f"{f.entry} ({f.charmaps[1]}): {f.reason}"
                     for f in unsettled])


def main(argv):
    ap = argparse.ArgumentParser(
        description="Filter changed locale files down to those that can "
                    "change LC_COLLATE, add unchanged ones the two tags' "
                    "localedef read differently, and compare the charmaps "
                    "the SUPPORTED builds "
                    "use and the collating elements they build out of "
                    "transliterations.")
    ap.add_argument('old_tag')
    ap.add_argument('new_tag')
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    ap.add_argument('--allow-reverse', action='store_true',
                    help="run a pair whose new tag is the OLDER commit. Refused by default. Prints a `!!` block saying the direction is reversed.")
    ap.add_argument('--diff-file',
                    help="a -U0 diff to check against the one git gives for "
                         "the two tags: refused unless it places every change "
                         "where git's does. The run still needs the clone, and "
                         "reads git's diff")
    opts = ap.parse_args(argv)

    # Each text's comment and escape characters, read once for this run:
    # classify_change and unread_above ask for the same texts, and the
    # character set check asks again.
    with g.reading_chars_once():
        return _main(opts)


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
    # (see new_side_paths) -- which lines its LC_COLLATE sections hold and
    # what can stop glibc reading them are asked of it too -- SUPPORTED,
    # which an added file needs for its names and the character set check at
    # both tags, and what that check reads (CharmapReads).
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
                   'the check of every content-changed file at the new '
                   'tag')
    if charmap_reads is not None:
        charmap_reads.prefetch(repo, read[3:], old_contents, new_contents,
                               read[2])

    early = (predates_lr_getc_fix(repo, opts.old_tag),
             predates_lr_getc_fix(repo, opts.new_tag))
    # How localedef reads every locale file at each tag (glibc_tree): where
    # its LC_COLLATE sections are and with which comment and escape
    # characters, from the runs glibc_reader computed.
    pre = lr_getc_sides(repo, early, (opts.old_tag, opts.new_tag))
    builds = glibc_tree.builds_at(repo, [opts.old_tag, opts.new_tag], pre)
    facts = tuple(glibc_tree.file_facts(repo, tag, listed[tag], ids[tag],
                                        builds[tag])
                  for tag in (opts.old_tag, opts.new_tag))
    sections = diff_sections(diff_text)
    verdicts = judge(content_changed, old_contents, new_contents, hunks,
                     sections, renamed_to, facts, early)
    (changed_collate, gained_collate, reading_files, unsettled_files,
     unchanged_collate, no_collate_block) = partition_verdicts(verdicts)
    across = set(read_across(repo, opts.old_tag,
                             (builds[opts.old_tag], builds[opts.new_tag]),
                             listed[opts.old_tag], ids[opts.old_tag]))
    (changed_collate, unchanged_collate, no_collate_block,
     folded) = fold_across(changed_collate, unchanged_collate,
                           no_collate_block, across)

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
        # and character-class data, which define no collation rules on either
        # side; what their transliterations build is compared by the
        # character set check below (collation_keys, point 6). Printing only
        # a number would leave a reader unable to tell that from a
        # locale that was skipped by mistake.
        names = ', '.join(sorted(os.path.basename(p) for p in no_collate_block))
        print(f"  (no LC_COLLATE on either side, so no sort rules of their "
              f"own; for what their transliterations build, see the "
              f"transliteration lines below: "
              f"{names})")
    if gained_collate:
        print(f"\n!! {len(gained_collate)} file(s) GAINED an LC_COLLATE block "
              f"at {opts.new_tag}. They had no")
        print(f"   sort order before and have one now, so they are counted as "
              f"changed above:")
        for path in sorted(gained_collate):
            print(f"     {path}")
    if unsettled_files:
        lines = []
        for path in sorted(unsettled_files):
            for side, tag in zip(facts_of(facts, renamed_to, path),
                                 (opts.old_tag, opts.new_tag)):
                if side is not None and side.unresolved:
                    lines.append(f"{path} at {tag}: {side.unresolved}")
        _warn_block(
            f"{len(unsettled_files)} file(s) could not be read the way "
            f"localedef reads them, so whether their change reaches "
            f"LC_COLLATE is not known. They are counted as changed above:",
            lines)
    across_changed = sorted(p for p in content_changed if p in across)
    if across_changed:
        _warn_block(
            f"{len(across_changed)} file(s) that changed are also read "
            f"differently by the localedef of {opts.old_tag} and that of "
            f"{opts.new_tag} (glibc 2.36 reads 0x1A and 0xFF as bytes and "
            f"bytes above 0x7F as UTF-8, and the keyword tables can differ), "
            f"so any rule in them can change, not only the changed lines. "
            f"They are counted as changed above:", across_changed)
    reread = read_otherwise(changed_collate, reading_files, facts,
                            renamed_to, old_contents, new_contents, sections)
    if reread:
        print(f"\n!! {len(reread)} file(s) are read at {opts.new_tag} with "
              f"other comment or escape")
        print(f"   characters than at {opts.old_tag}, so any rule in them "
              f"can read differently.")
        print(f"   They are counted as changed above:")
        for path in reread:
            old, new = facts_of(facts, renamed_to, path)
            print(f"     {path}")
            print(f"       {describe_reading(old, opts.old_tag)}")
            print(f"       " + (describe_reading(new, opts.new_tag)
                                if new is not None else
                                f"not read at {opts.new_tag}"))
    unread = unread_above(changed_collate, old_contents, new_contents,
                          renamed_to, early, (opts.old_tag, opts.new_tag),
                          facts, sections)
    if unread:
        _warn_block(
            f"{len(unread)} file(s) hold something that can stop glibc "
            f"reading their LC_COLLATE as written, so their sort order can "
            f"change whatever their block says. They are counted as changed "
            f"above:",
            [f"{path} {why}" for path, whys in unread for why in whys])
    unread_paths = {path for path, _ in unread}
    judged = set(content_changed) | set(deleted)
    stopped = sorted(p for p in across if p not in judged)
    if stopped:
        _warn_block(
            f"{len(stopped)} file(s) that did not change are read differently "
            f"by the localedef of {opts.old_tag} and that of {opts.new_tag}, "
            f"or could not be settled by one of them: glibc before 2.36 reads "
            f"0x1A, and 0xFF, as the end of the file, and from 2.36 reads "
            f"bytes above 0x7F as UTF-8, and the two keyword tables can "
            f"differ. Every rule in their LC_COLLATE, or in one that copies "
            f"them, can change. They are added to step 3's list:",
            stopped)

    # Under each file, the characters its changed rules name: where the
    # confirmation template's test values start. Indented six spaces and
    # with no blank line, so the list keeps parsing as one path per line. The
    # new side is the one already read, strictly, for the verdicts above.
    print(f"\nFiles with changes inside LC_COLLATE: {len(changed_collate)}")
    for path in sorted(changed_collate):
        print(f"  {path}")
        chars = changed_characters(sections.get(path, []),
                                   *facts_of(facts, renamed_to, path))
        if path in reread:
            if chars:
                print_characters(chars)
            print(textwrap.fill(
                ("those are the changed lines; " if chars else "")
                + "the block is read with other characters (see the `!!` "
                "above that names it), so any rule in it can read "
                "differently",
                width=78, initial_indent='      ', subsequent_indent='      '))
        elif path in across_changed:
            if chars:
                print_characters(chars)
            print(textwrap.fill(
                ("those are the changed lines; " if chars else "")
                + "the two tags' localedef read this file differently (see "
                "the `!!` above that names it), so any rule in it can change",
                width=78, initial_indent='      ', subsequent_indent='      '))
        elif path in unread_paths:
            if chars:
                print_characters(chars)
            print(textwrap.fill(
                ("those are the changed lines; " if chars else "")
                + "glibc may not read the block as written (see the `!!` "
                "above that names it), so every rule in it can change",
                width=78, initial_indent='      ', subsequent_indent='      '))
        elif instruction_changed(sections.get(path, []),
                                 *facts_of(facts, renamed_to, path)):
            if chars:
                print_characters(chars)
            print(textwrap.fill(
                ("those are the changed lines; " if chars else "")
                + "a changed line decides how other lines are read or where "
                "other rules sort, so every rule in it can change",
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
    # Files that did not change are at the new tag under the same name.
    names = sorted(set(for_step3) | {os.path.basename(p) for p in stopped})

    if translated:
        print(f"\n{len(translated)} of those file(s) were renamed, so step 3 "
              f"gets the name that exists at {opts.new_tag}:")
        for old_path, new_path in sorted(translated):
            print(f"  {os.path.basename(old_path)} -> "
                  f"{os.path.basename(new_path)}")

    # The character sets the locales are built with (collation_keys): a rule
    # identical at both tags sorts differently when its charmap gives a
    # character it names other bytes, or when a collating element it defines
    # is built out of another transliteration. What it lists goes to step 3
    # in a list of its own, so step 3 does not call it an LC_COLLATE change.
    if charmap_reads is None:
        print("\nCharacter sets and transliterations: not compared, the two "
              "tags are one commit.")
        keyed = []
    else:
        side_old, side_new, sup = charmap_reads.sides(
            repo, read[3:], old_contents, new_contents, read[2])
        trees = []

        def new_tree():
            if not trees:
                trees.append(glibc_tree.Tree.at_tag(repo, opts.new_tag,
                                                    builds[opts.new_tag]))
            return trees[0]
        keyed = check_charmaps(charmap_reads, repo, side_old, side_new, sup,
                               names, new_tree)

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
