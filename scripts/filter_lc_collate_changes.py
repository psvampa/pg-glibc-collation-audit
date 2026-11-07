#!/usr/bin/env python3
"""
Step 2: compare the sort rules localedef builds for every locale on the two
sides, every `copy` followed (glibc_tree.compare_rules), and list for
Reindex the locales whose rules differ or could not be compared.

Each name is a locale file of either side or an alias of either side's
intl/locale.alias. Its answer is one of three: the same rules, rules that
differ (with the characters the lines that count name), or not compared,
with the reason. A locale whose rules this step could not compare -- the
reader cannot settle how localedef builds it, or LC_COLLATE is stored on one
side only -- is listed with a `!!`, never cleared. A name with no file on the
old side is not compared and is named, with what the old machine may still
have (distros backport: C.UTF-8 is in RHEL 8 and 9 and in no tag before
2.35); one with no file on the new side is what the summary's Removed lists.

When the two sides are one commit, or two commits of one source tree,
nothing is compared and this step says so before anything else: the
comparison of a tree with itself answers "the same" for every locale.

It also compares, between the two sides, the charmaps the SUPPORTED builds
use and the collating elements they build out of transliterations
(collation_keys.py); what that check lists goes to Reindex too.

Writes, each named for the pair: step2_changed_collate.<pair>.txt (Reindex:
the names listed), step2_keyed_locales.<pair>.txt (what the character set
check lists), step2_removed_locales.<pair>.txt (files of the old side not on
the new one) and step2_rules_characters.<pair>.tsv (each name whose rules
differ, and the characters named).

Usage:
  python3 filter_lc_collate_changes.py <old_tag> <new_tag> [--repo <path>]
                                       [--allow-reverse]
  each a glibc tag, or a build named as `rpm -q glibc` names it
  without the architecture (glibc-2.34-283.el9_8),
  fetched and read by its commit

Example:
  python3 filter_lc_collate_changes.py glibc-2.28 glibc-2.34
"""
import argparse
import collections
import os
import sys
import textwrap
import threading

import collation_keys as ck
import glibc_locale_data as g
import glibc_tree

# The character set check's report, and the helpers it prints with,
# live in node_charmaps.py, which outlives the steps that print them
# from here (step 9.1). Named here as well: this step and its tests
# use these names.
from node_charmaps import (
    print_characters, _named_under, reads_utf8, Words, STEP2_WORDS,
    _said, _charmaps_not_run, _warn_block, _CHANGED_IN_CHARMAP,
    _chars_phrase, _print_chars, print_charmap_report,
    print_translit_report)

WIDTH = 78


def predates_lr_getc_fix(repo, tag):
    """A zero-argument question: does `tag`'s localedef read 0x1A and 0xFF
    as the end of the file? True, False, or None when git cannot place
    glibc_locale_data.LR_GETC_FIX (a shallow clone, a fabricated
    repository). main asks it of both tags before anything is compared
    and refuses a None (glibc_tree.builds_at), and two "predates" on a
    shallow clone. Asked of git at most once per tag."""
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



def split_raw(text):
    """([name-status line, ...], what follows the list) from a `--raw`
    diff.

    git writes one `:` line per changed file; with nothing changed,
    nothing. A `:` line is `:<old mode> <new mode> <old id> <new id>
    <status>` and a tab before the path or paths, so what follows its fourth
    space is the line `--name-status` prints for that file. Measured with
    git 2.54 on every pair the tests run, renames and the same commit twice
    included. Step 2 asks for `--raw` alone, so what follows the list is
    empty (measured on 2.34..2.39: 318 lines, nothing after them). A `:`
    line of another shape dies: a deletion or a rename it could not read
    would be missing from Removed. So does text with no list above it,
    which would read as nothing removed.
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


def check_charmaps(reads, repo, side_old, side_new, sup, already,
                   words=None):
    """Run collation_keys.compare over the two sides, print what it found,
    and return the source names it lists for Reindex. `already` is the set
    of source names the comparison lists already (their rules differ or
    could not be compared), which the check does not list again."""
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
        # What the comparison lists: compare_rules follows every `copy`,
        # so a locale that copies a changed one differs itself.
        return set(already)

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
    print_charmap_report(report, old, new, words or STEP2_WORDS)
    return ck.listed_sources(report)


def step2_words(noun):
    """STEP2_WORDS for two tags, else the same words naming the two sides
    as `noun` says (glibc_locale_data.pair_noun)."""
    if noun == 'tags':
        return STEP2_WORDS
    return STEP2_WORDS._replace(**{
        f: getattr(STEP2_WORDS, f).replace('both tags', f'both {noun}')
        .replace('between the tags', f'between the {noun}')
        for f in ('builds', 'none_compared', 'not_compared',
                  'changed_between')})


# What this step does not compare, said once. The rules compare_rules reads
# are statements; what localedef compiles them into and how strcoll walks
# the result is code, which step 5 reads.
NOT_COMPARED_HERE = (
    "Not compared here: the code that compiles and compares the rules "
    "(localedef's and strcoll's), which step 5 reads; the character sets "
    "are compared below.")


def _fill(text, first='      ', rest=None):
    return textwrap.fill(text, width=WIDTH, initial_indent=first,
                         subsequent_indent=first if rest is None else rest,
                         break_long_words=False, break_on_hyphens=False)


def shown_name(name):
    """A name as this step prints it: glibc's file C is C.UTF-8, a name a
    terminal cannot carry is escaped (glibc_locale_data.shown)."""
    return 'C.UTF-8' if name == 'C' else g.shown(name)


def sort_out(got, old_tree, new_tree):
    """(differs, added, removed, listed) from compare_rules' answer, each
    sorted: the names whose rules differ; the not-compared FILES that only
    the new side has; those only the old side has; and every other name
    not compared (the reader in doubt, LC_COLLATE stored on one side, an
    alias one side cannot resolve), which is listed for Reindex. Sorted by
    which side has the file, never by the words of `why`."""
    differs, added, removed, listed = [], [], [], []
    old_files, new_files = set(old_tree.names), set(new_tree.names)
    for name in sorted(got):
        r = got[name]
        if r.state == glibc_tree.DIFFERS:
            differs.append(name)
        elif r.state == glibc_tree.NOT_COMPARED:
            if name in new_files and name not in old_files:
                added.append(name)
            elif name in old_files and name not in new_files:
                removed.append(name)
            else:
                listed.append(name)
        elif r.state != glibc_tree.SAME:
            raise ValueError(f"compare_rules answered {r.state!r} for "
                             f"{g.shown(name)}, which is none of its states")
    return differs, added, removed, listed


def print_differs(got, differs, tsv_path):
    """Each group of names with the same answer, and what it names. Every
    rule can change: the count, and the characters in `tsv_path`; a
    count of characters is never printed where it is not known, and an
    empty list is said in words."""
    print()
    if not differs:
        print("Rules differ: none.")
        return
    print(f"Rules differ ({len(differs)}):")
    groups = collections.OrderedDict()
    for name in differs:
        r = got[name]
        groups.setdefault((r.chars, r.every_rule, r.why), []).append(name)
    for (chars, every, why), names in groups.items():
        print(_fill(', '.join(shown_name(n) for n in names), '  '))
        if chars is None:
            print(_fill(f"which characters is not known: {why}"))
        elif not chars:
            print(_fill("the lines that count name no character"))
        elif every:
            print(_fill(f"{len(chars)} character(s) named by the lines that "
                        f"changed or are read otherwise; listed in "
                        f"{tsv_path}"))
        else:
            print_characters([chr(c) if c < 0x110000 else c for c in chars])
        if every:
            print(_fill("a changed line decides how other lines are read or "
                        "where other rules sort, so every rule can change"))


def characters_rows(got, differs):
    rows = ["# name\tstate\tevery rule can change\tcharacters"]
    for name in differs:
        r = got[name]
        chars = (f"not known: {r.why}" if r.chars is None else
                 ' '.join(f'U+{c:04X}' for c in r.chars))
        rows.append(f"{g.shown(name)}\t{r.state}\t"
                    f"{'yes' if r.every_rule else 'no'}\t{chars}")
    return rows


def print_added(added, renamed_from, supported, old_side, old_name):
    if not added:
        return
    print()
    print(f"Not compared, no file at {old_name} ({len(added)}):")
    for name in added:
        spelt = supported.get(name)
        tail = ', '.join(spelt) if spelt else '(not in SUPPORTED)'
        if name in renamed_from:
            tail += f"; renamed from {g.shown(renamed_from[name])}"
        print(f"  {g.shown(name)} -> {tail}")
    if g._is_tag(old_side):
        print(_fill(
            f"A file added at the new side cannot affect an existing index "
            f"ONLY IF the locale did not exist on the old system. An "
            f"upstream tag cannot establish that -- distros backport. "
            f"Confirm with `locale -a` on the OLD machine, under the names "
            f"above, before treating these as out of scope.", '   '))
    else:
        # The old side is the build's own sources, distro patches and all,
        # verified against its glibc-locale-source.
        print(_fill(f"{old_name}'s own locale sources do not have these "
                    f"files, so that build ships none of these locales.",
                    '   '))


def print_removed(removed, renamed_to, new_side, new_name):
    if not removed:
        return
    print()
    print(f"Not compared, no file at {new_name} ({len(removed)}), listed "
          f"under Removed in the summary:")
    for name in removed:
        extra = (f" (renamed to {g.shown(renamed_to[name])})"
                 if name in renamed_to else '')
        print(f"  {g.shown(name)}{extra}")
    if g._is_tag(new_side):
        print(_fill("An index using one of these fails to sort on the new "
                    "system UNLESS its distro ships the file anyway, which "
                    "an upstream tag cannot show.", '   '))
    else:
        print(_fill("Any index using one of these will fail to sort on the "
                    "new system at all.", '   '))


def print_listed(got, listed):
    if not listed:
        return
    whys = collections.OrderedDict()
    for name in listed:
        why = got[name].why
        if why.startswith('old side: ') and '; new side: ' in why:
            a, b = why[len('old side: '):].split('; new side: ', 1)
            if a == b:
                why = 'both sides: ' + a
        whys.setdefault(why, []).append(name)
    _warn_block(
        f"Not compared, so listed for REINDEX ({len(listed)}): the reader "
        f"could not settle how localedef builds them, or LC_COLLATE is "
        f"stored on one side only, or an alias does not resolve on both "
        f"sides:",
        [f"{', '.join(shown_name(n) for n in names)}: {why}"
         for why, names in whys.items()])


def main(argv):
    ap = argparse.ArgumentParser(
        description="Compare the sort rules localedef builds for every "
                    "locale on the two sides, every copy followed, and the "
                    "charmaps the SUPPORTED builds use and the collating "
                    "elements they build out of transliterations.")
    side_help = ("a glibc tag, or a build named as `rpm -q glibc` names it "
                 "(glibc-2.34-283.el9_8)")
    ap.add_argument('old_tag', help=side_help)
    ap.add_argument('new_tag', help=side_help)
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    ap.add_argument('--allow-reverse', action='store_true',
                    help="run a pair whose new side is the OLDER one. "
                         "Refused by default. Prints a `!!` block saying the "
                         "direction is reversed.")
    opts = ap.parse_args(argv)
    with g.reading_chars_once():
        return _main(opts)


def _main(opts):
    repo = g.find_repo(opts.repo)
    # Each side a tag or a build (step_sides): from here on git reads a
    # build by its commit, and every line printed names it by its name.
    # The names are kept for the lists audit.sh reads by name.
    (old_side, new_side), commits = g.step_sides(repo, opts.old_tag,
                                                 opts.new_tag)
    old_name, new_name = opts.old_tag, opts.new_tag
    noun = g.pair_noun(old_side, new_side)
    old, new = g._ref(old_side), g._ref(new_side)
    slug = g.pair_slug(old_name, new_name)

    # Which of the two is newer, asked of git rather than assumed, and
    # whether the two compare anything at all.
    _, nothing = g.pair_check(repo, old_side, new_side,
                              allow_reverse=opts.allow_reverse,
                              commits=commits)

    # The corpus floor (list_tree_ids dies below it), and the blob ids the
    # character set check compares.
    listed_ids, ids = {}, {}
    for tag in (old, new):
        if tag not in listed_ids:
            listed_ids[tag], ids[tag] = g.list_tree_ids(
                repo, tag, [ck.CHARMAPS_DIR + '/', g.SUPPORTED])

    # git's list of the files that differ: what is gone, and where a file
    # was renamed to, for Removed; and the files whose content changed, whose
    # two texts the transliteration check compares (collation_keys: an
    # LC_CTYPE that changed builds a collating element otherwise). Through
    # git_diff, so the reader's git config cannot change it.
    status, _ = split_raw(g.git_diff(
        repo, ['--raw', '--find-renames', f'{old}..{new}', '--',
               g.LOCALES_DIR + '/']))
    deleted, renamed_to, content_changed, new_path = [], {}, [], {}
    for line in status:
        parts = line.split('\t')
        if parts[0].startswith('R'):
            renamed_to[os.path.basename(parts[1])] = os.path.basename(
                parts[2])
            content_changed.append(parts[1])
            new_path[parts[1]] = parts[2]
        elif parts[0] == 'D':
            deleted.append(os.path.basename(parts[1]))
        elif parts[0] != 'A':
            content_changed.append(parts[1])
            new_path[parts[1]] = parts[1]
    renamed_from = {b: a for a, b in renamed_to.items()}
    removed_rows = sorted(
        [(n, 'deleted') for n in deleted]
        + [(a, f'renamed to {b}') for a, b in renamed_to.items()])

    tsv = os.path.join(g.OUT_DIR, f"step2_rules_characters.{slug}.tsv")
    if nothing:
        # A tree compared with itself answers "the same" for every locale
        # the reader settles: nothing was compared, and that is said first.
        what = (f"the two {noun} are one commit" if nothing == 'commit'
                else "the two refs are two commits of one source tree")
        print(f"Rules: not compared, {what}.")
        print(f"Character sets and transliterations: not compared, {what}.")
        names, keyed = [], []
        g.write_list(f"step2_rules_characters.{slug}.tsv",
                     characters_rows({}, []))
    else:
        early = (predates_lr_getc_fix(repo, old),
                 predates_lr_getc_fix(repo, new))
        pre = lr_getc_sides(repo, early, (old, new))
        builds = glibc_tree.builds_at(repo, [old, new], pre)
        old_tree = glibc_tree.Tree.at_tag(repo, old, builds[old])
        new_tree = glibc_tree.Tree.at_tag(repo, new, builds[new])
        aliases = (g.locale_aliases(repo, old), g.locale_aliases(repo, new))
        supported = (g.supported_map(repo, old), g.supported_map(repo, new))
        got = glibc_tree.compare_rules(old_tree, new_tree, aliases)
        differs, added, removed, listed = sort_out(got, old_tree, new_tree)
        n_same = sum(1 for r in got.values() if r.state == glibc_tree.SAME)

        print(_fill(f"The sort rules localedef builds for each locale at "
                    f"{old_name} and at {new_name}, every `copy` followed, "
                    f"compared statement by statement:", ''))
        print(_fill(f"{len(got)} name(s), locale files and locale.alias "
                    f"aliases: {n_same} the same, {len(differs)} differ, "
                    f"{len(got) - n_same - len(differs)} not compared.",
                    '  '))
        print(_fill(NOT_COMPARED_HERE, '  '))
        print_differs(got, differs, tsv)
        g.write_list(f"step2_rules_characters.{slug}.tsv",
                     characters_rows(got, differs))
        print_added(added, renamed_from, supported[1], old_side, old_name)
        print_removed(removed, renamed_to, new_side, new_name)
        print_listed(got, listed)

        # The character set check, with what this step lists already. It
        # reads both texts of every content-changed file, in one batch with
        # SUPPORTED and what CharmapReads adds: given none, the
        # transliteration check said no LC_CTYPE changed over a changed
        # translit_neutral (found writing the 9.2 tests). A file of git's
        # list that comes back unread is refused, never read as absent.
        new_paths = sorted({new_path[p] for p in content_changed})
        charmap_reads = CharmapReads(old, new, listed_ids, ids,
                                     content_changed, new_paths)
        read = g.read_blobs_at(repo, [(old, content_changed),
                                      (new, new_paths), (new, [g.SUPPORTED])]
                               + charmap_reads.groups)
        (old_contents, old_missing), (new_contents, new_missing) = read[:2]
        g.require_read(old, content_changed, old_missing,
                       'the transliteration check')
        g.require_read(new, new_paths, new_missing,
                       'the transliteration check')
        charmap_reads.prefetch(repo, read[3:], old_contents, new_contents,
                               read[2])
        side_old, side_new, sup = charmap_reads.sides(
            repo, read[3:], old_contents, new_contents, read[2])
        sources = set(old_tree.names) | set(new_tree.names)
        already = {n for n in differs + listed if n in sources}
        keyed = check_charmaps(charmap_reads, repo, side_old, side_new, sup,
                               already, step2_words(noun))

        # The character set check names locale files; the aliases of each
        # side's locale.alias that are built from one go to Reindex with it
        # (swedish with sv_SE). compare_rules adds the aliases of what it
        # compares itself, but an alias of a keyed locale whose rules are the
        # same comes out "same" there (false-negative-reviewer, 9.2).
        keyed_aliases = set()
        for al in aliases:
            keyed_aliases |= set(g._aliases_reaching(set(keyed), al))
        names = sorted(set(differs) | set(listed) | set(keyed)
                       | keyed_aliases)
        not_ascii = [n for n in names if not n.isascii()]
        names = [n for n in names if n.isascii()]
        print()
        print(_fill(f"Listed for Reindex ({len(names)}): "
                    + (', '.join(shown_name(n) for n in names) or 'none'),
                    '', '  '))
        unbuilt = [n for n in names if n in sources
                   and n not in supported[0] and n not in supported[1]]
        if unbuilt:
            print(_fill("Not listed in localedata/SUPPORTED (not built by "
                        "default, so normally absent from `locale -a`): "
                        + ', '.join(g.shown(n) for n in unbuilt), '  '))
        if not_ascii:
            print(_fill("Not listed: alias(es) whose name is not ASCII; "
                        "PostgreSQL never imports such a name as a "
                        "collation: "
                        + ', '.join(g.shown(n) for n in not_ascii), '  '))

    # Written whether or not they are empty, and named after the pair: an
    # empty file for THIS pair is a different fact from a leftover one.
    g.write_list(f"step2_changed_collate.{slug}.txt", names)
    g.write_list(f"step2_keyed_locales.{slug}.txt", keyed)
    g.write_list(f"step2_removed_locales.{slug}.txt",
                 [f"# locale files at {old_name} and not at {new_name}"]
                 + [f"{name} ({how})" for name, how in removed_rows])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
