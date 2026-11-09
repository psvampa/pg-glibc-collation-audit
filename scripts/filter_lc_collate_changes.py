#!/usr/bin/env python3
"""
Step 2: compare the sort rules localedef builds for every locale on the two
sides, every `copy` followed (glibc_tree.compare_rules), and list, for the
summary, the locales whose rules differ or could not be compared.

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
use (for two machines' own files, the builds measured on each machine) and
the collating elements they build out of transliterations
(collation_keys.py); what that check lists goes to that list too.

Writes, each named for the pair: step2_changed_collate.<pair>.txt (the
names listed, which the summary prints), step2_not_compared.<pair>.txt
(those of them it could not compare, which the summary sets apart),
step2_keyed_locales.<pair>.txt (what the character set check lists),
step2_removed_locales.<pair>.txt (files of the old side not on the new
one), step2_rules_characters.<pair>.tsv (each name whose rules differ, and
the characters named) and step2_rules_by_name.<pair>.json (what was
compared, what became of the character set check, and, for two machines'
files whose sources it compared, the rules of every locale name measured
on either machine, which audit.sh sets beside step 11's verdict:
rules_by_name).

Usage:
  python3 filter_lc_collate_changes.py <old_tag> <new_tag> [--repo <path>]
                                       [--allow-reverse]
  each a glibc tag, or a build named as `rpm -q glibc` names it
  without the architecture (glibc-2.34-283.el9_8),
  fetched and read by its commit, or, under audit.sh given each machine's
  file, read from that machine's file (glibc_locale_data.machine_side)

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
# live in node_charmaps.py, which also reads a machine's charmaps for
# this step and for step 11. Named here as well: this step and its
# tests use these names.
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
                   words=None, builds=None):
    """Run collation_keys.compare over the two sides, print what it found,
    and return the source names it lists for the summary. `already` is the
    set of source names the comparison lists already (their rules differ
    or could not be compared), which the check does not list again.
    `builds` is [old, new] when the caller has them from the machines'
    measurements (machine_builds), in place of SUPPORTED's.

    Returns (source names, why): why is the reason the check said NOT RUN,
    on each of its routes, else None, so that a caller never reads the
    empty list of a check that did not run as one that found nothing."""
    old, new = side_old.tag, side_new.tag
    # The charmaps read ahead of time (CharmapReads.prefetch) are back by
    # now: the verdicts above took far longer than one `git cat-file`.
    # Joined first, so that a git that failed says so here, whatever path
    # the check takes.
    reads._join(side_old, side_new)
    if builds is not None:
        builds_old, builds_new = builds
    else:
        try:
            builds_old = (ck.supported_builds(sup[old], old)
                          if sup[old] is not None else None)
            builds_new = (ck.supported_builds(sup[new], new)
                          if sup[new] is not None else None)
        except ck.Unsettled as e:
            return _charmaps_not_run(str(e)), str(e)
    if builds_old is None or builds_new is None:
        where = ' and '.join(t for t in (old, new) if sup[t] is None)
        why = f"{g.SUPPORTED} does not exist at {where}"
        return _charmaps_not_run(why), why
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
    # Point 5 (a source no build uses, searched in a changed charmap) only
    # for builds read from SUPPORTED: a machine's builds are what was
    # measured on it, and a locale it does not build is not there for a
    # database to use (collation_keys.compare, search_unbuilt).
    report = ck.compare(side_old, side_new, builds_old, builds_new,
                        already_listed, read_more, fresh=False,
                        search_unbuilt=builds is None)
    words = words or STEP2_WORDS
    print_charmap_report(report, old, new, words)
    # The two NOT RUN print_charmap_report says of the report itself.
    why = (report.not_run if report.not_run
           else _said(words, 'none_compared', old, new)
           if not report.builds else None)
    return ck.listed_sources(report), why


def _measured_names(sd):
    """The locale names measured on the machine a side reads (its file's
    locale_order.out, glibc_locale_data.machine_side), sorted; refused
    when the measurement cannot be read or lists none."""
    import json
    path = os.path.join(sd.prov['machine'], 'locale_order.out')
    try:
        with open(path, encoding='ascii') as fh:
            locales = json.load(fh).get('locales')
    except (OSError, ValueError, AttributeError) as e:
        g.die(f"cannot read the measurement {path}: {e}")
    if not isinstance(locales, dict) or not locales:
        g.die(f"the measurement {path} lists no locale")
    return sorted(locales)


def _measured(sd, aliases):
    """{source name: [names measured on that machine]} for a side read
    from a machine's own files (glibc_locale_data.machine_side): what
    SUPPORTED says for a tag or a build, which a machine's file does not
    hold. Each name goes to the locale file it is built from, through the
    side's locale.alias, as measured_builds maps it."""
    out = collections.defaultdict(list)
    for name in _measured_names(sd):
        out[g.locale_source(aliases.get(name, name))].append(name)
    return dict(out)


# The states rules_by_name gives a measured name: compare_rules' three,
# and glibc's built-in locale, which no file builds.
BUILT_IN = 'built in'
RULES_STATES = (glibc_tree.SAME, glibc_tree.DIFFERS, glibc_tree.NOT_COMPARED,
                BUILT_IN)


def rules_by_name(sides, aliases, got, unread, keyed, unmapped):
    """{measured name: [key, state, why, charmap]}: step 2's rules for
    every locale name measured on either machine, for the cross with step
    11's verdict (locale_order.rules_beside), which takes it from here and
    never works it out again.

    key is the name compare_rules answered for: the name itself where
    compare_rules has it (a locale file or a locale.alias alias), else the
    file the name is built from through the side's locale.alias, as
    _measured maps it. C and POSIX are glibc's built-in locale whatever a
    machine holds (node_charmaps.BUILTIN), never the files C and POSIX:
    the file C is C.UTF-8's. An entry of a machine's sources that was not
    read is not compared, whatever compare_rules says of the name (it
    never saw the file); a name no file builds is not compared, "no locale
    file of that name" (a locale an admin built with localedef -i under a
    name of their own). charmap is 'changed' where the character set
    check lists the file the name is built from (KEYED holds files; an
    alias such as swedish is built from sv_SE, as step 2's list adds
    it), 'not compared' where it could not trace the name to its charmap
    (UNMAPPED), else '-'."""
    import node_charmaps as nc
    untraced = {n for who, names_ in unmapped or () for n, w in names_}
    keyed = set(keyed)
    out = {}
    for sd, al in zip(sides, aliases):
        for name in _measured_names(sd):
            if name in out:
                continue
            if name in nc.BUILTIN:
                out[name] = [None, BUILT_IN,
                             'built into glibc, no locale file', '-']
                continue
            source = g.locale_source(al.get(name, name))
            key = name if name in got else source
            if key in unread:
                state = glibc_tree.NOT_COMPARED
                why = ("not read among the machines' locale sources: "
                       + '; '.join(unread[key]))
            elif key in got:
                state, why = got[key].state, got[key].why or ''
            else:
                state, why = glibc_tree.NOT_COMPARED, \
                    'no locale file of that name'
            if state not in RULES_STATES:
                g.die(f"compare_rules answered {state!r} for {key}, which "
                      f"is none of {', '.join(RULES_STATES)}")
            charmap = ('changed' if key in keyed or source in keyed
                       else 'not compared' if name in untraced else '-')
            out[name] = [key, state, why, charmap]
    return out


def write_rules_by_name(slug, noun, names, machine, charmaps, sides=(),
                        nothing=None, what=None, locales=None):
    """Write step2_rules_by_name.<pair>.json, what step 2 compared and,
    for two machines' files, its rules for every measured name: the file
    locale_order.rules_beside reads beside step 11's verdict. Written on
    every route, so that a missing file means step 2 did not get there.
    measurements are the sha256 of each machine's measurement
    (prov['measurement_sha256']), which step 11's verdicts carry too, so
    that the two are never of two runs.

    charmaps is what became of the character set check on this route,
    {'state': one of locale_order.CHARMAPS_STATES, 'why': ...}, a status
    and never a boolean (2.1): 'compared' (the check ran; its own `!!`
    say what it could not settle), 'not run' (why: its reason), 'one
    commit' and 'one tree' (nothing to compare; the same tree holds the
    same charmaps), 'not read' (one tree of two machines' files that hold
    no charmaps this reads; why: each file's reason). A state outside the
    set, or a why that does not go with it, is refused here."""
    import json
    import locale_order as lo
    state, why = charmaps.get('state'), charmaps.get('why')
    if state not in lo.CHARMAPS_STATES:
        g.die(f"step 2's character set check ended in {state!r}, which is "
              f"none of {', '.join(lo.CHARMAPS_STATES)}")
    if (why is None) != (state in ('compared', 'one commit', 'one tree')) \
            or (state == 'not read' and not (isinstance(why, list) and why)):
        g.die(f"step 2's character set check ended in {state!r} with the "
              f"reason {why!r}, which does not go with it")
    data = {'step': 2, 'compared': noun, 'names': list(names),
            'machines': bool(machine), 'nothing': nothing, 'what': what,
            'measurements': [sd.prov['measurement_sha256'] for sd in sides]
            if machine else None,
            'charmaps': charmaps, 'locales': locales}
    g.write_list(f"step2_rules_by_name.{slug}.json",
                 [json.dumps(data, sort_keys=True)])


def machine_builds(repo, sides, aliases):
    """For two sides read from machines' own files: ([old builds, new
    builds], [(machine, unmapped names)], None) from each machine's
    measurement (node_charmaps.measured_builds), in place of SUPPORTED,
    which a machine's file does not hold; or (None, None, reason) when a
    machine's charmaps cannot be read, which makes the check NOT RUN."""
    import node_charmaps as nc
    builds, unmapped = [], []
    for sd, al in zip(sides, aliases):
        p = sd.prov
        if p['charmaps'] is None:
            return None, None, (f"The file of the machine that runs "
                                f"{g.label(sd)} holds no charmaps this reads, "
                                f"because {p['charmaps_why']}")
        try:
            m = nc.measured_builds(
                os.path.join(p['machine'], 'locale_order.out'), p['word'],
                nc.CharmapsDir(p['charmaps_dir']), al, p['tag'],
                nc._tag_charmaps(repo, p['tag']))
        except nc.Unusable as e:
            return None, None, (f"On the machine that runs {g.label(sd)}: "
                                f"{str(e).rstrip('.')}")
        builds.append(m.builds)
        if m.unmapped:
            unmapped.append((g.label(sd), m.unmapped))
    return builds, unmapped, None


def step2_words(noun, machines=False):
    """STEP2_WORDS for two tags, else the same words naming the two sides
    as `noun` says (glibc_locale_data.pair_noun); for two machines' own
    files, the builds are the ones measured on them (machine_builds), not
    SUPPORTED's."""
    if machines:
        return STEP2_WORDS._replace(
            builds="measured on both machines",
            none_compared="No locale build is measured on both machines "
                          "with its source file on both, so none was "
                          "compared.",
            not_compared="{n} locale build{s} measured on both machines "
                         "{is_are} not compared: {its_their} source file is "
                         "not on both.",
            changed_between="between the machines")
    if noun == 'tags':
        return STEP2_WORDS
    return STEP2_WORDS._replace(**{
        f: getattr(STEP2_WORDS, f).replace('both tags', f'both {noun}')
        .replace('between the tags', f'between the {noun}')
        for f in ('builds', 'none_compared', 'not_compared',
                  'changed_between')})


# What this step does not compare. The rules compare_rules reads are
# statements; what localedef compiles them into and how strcoll walks the
# result is code, which no step compares: a `!!` under "What was compared"
# says so once (glibc_locale_data.CODE_NOT_COMPARED), and this line points to
# it.
NOT_COMPARED_HERE = (
    "Not compared here: the code that compiles and compares the rules "
    "(localedef's and strcoll's), as said under 'What was compared'; the "
    "character sets are "
    "compared below.")


# What PostgreSQL does with C.UTF-8, said once, by this step: a libc
# collation or database whose locale is C, C.<anything> or POSIX gets no
# version (pg_locale.c:1708-1724@REL_13_23, which also skips POSIX.<any>;
# pg_locale.c:1671-1674@REL_14_19, :1697-1700@REL_16_10;
# pg_locale_libc.c:663-665@REL_18_RC1), so nothing in PostgreSQL warns when
# its order changes.
C_NO_VERSION = (
    "PostgreSQL records no collation version for a libc collation or "
    "database whose locale is C, C.<anything> or POSIX, so no version "
    "mismatch warns about C.UTF-8; sql/c_utf8_probe.sql, run on both "
    "machines, compares its order inside PostgreSQL.")


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
    alias one side cannot resolve), which is listed for the summary. Sorted
    by which side has the file, never by the words of `why`."""
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


def print_added(added, renamed_from, supported, old_side, old_name,
                missing='(not in SUPPORTED)', unread=None, new_name=None):
    """The not-compared files only the new side has. `unread` is {name:
    why} for those of them glibc_tree.Tree.unresolved names at `new_name`,
    each with its reason: said in a `!!` of its own, which the summary
    repeats, since nothing else says so (step 1's fan-in did until step
    9.5).

    With an upstream tag on the old side, whether the old system lacked
    them is not known, so the list is a `!!` the summary repeats (16.123);
    C is left to print_c_utf8's `!!`, which a tag without the file always
    prints, so that the fact is said once."""
    if not added:
        return
    lines = []
    for name in added:
        spelt = supported.get(name)
        tail = ', '.join(spelt) if spelt else missing
        if name in renamed_from:
            tail += f"; renamed from {g.shown(renamed_from[name])}"
        lines.append((name, f"{g.shown(name)} -> {tail}"))
    if g._is_tag(old_side):
        rest = [line for name, line in lines if name != 'C']
        if rest:
            _warn_block(
                f"Not compared, no file at {old_name} ({len(rest)}). A file "
                f"added at the new side cannot change what PostgreSQL "
                f"already stored ONLY IF the locale did not exist on the old "
                f"system, and an upstream tag cannot establish that -- "
                f"distros backport. Confirm with `locale -a` on the OLD "
                f"machine, under the names on each line, before treating "
                f"these as out of scope:", rest)
    else:
        print()
        print(f"Not compared, no file at {old_name} ({len(added)}):")
        for name, line in lines:
            print(f"  {line}")
        # The old side is the build's own sources, distro patches and all,
        # verified against its glibc-locale-source, or a machine's own
        # sources as its file holds them (glibc_locale_data.machine_side).
        print(_fill(f"{old_name}'s own locale sources do not have these "
                    f"files, so that build ships none of these locales.",
                    '   '))
    if unread:
        _warn_block(
            f"Sort rules not known, of the locale files at {new_name} and "
            f"not at {old_name} ({len(unread)}), for the reason on each "
            f"line:",
            [f"{g.shown(n)}: {unread[n]}" for n in added if n in unread])


def print_c_utf8(sides, differs):
    """The `!!` for C.UTF-8 (glibc's file C), from the trees, never from a
    list of what a distro ships: a tag side without the file, whose rules
    were then not compared (a distro build can ship its own: Red Hat's for
    RHEL 8, 9 and 10 do), or rules that differ. Nothing when C was compared
    and is the same. Never says its order changes where nothing compared
    it (#151: RHEL 9 and 10 ship one C). `sides` is ((side, tree, name),
    (side, tree, name)), old first."""
    lacking = [name for sd, tree, name in sides
               if g._is_tag(sd) and 'C' not in tree.names]
    if lacking:
        _warn_block(f"{' and '.join(lacking)} "
                    f"{'holds' if len(lacking) == 1 else 'hold'} no "
                    f"localedata/locales/C, the file C.UTF-8 is built from, "
                    f"and a distro's build can ship its own, so C.UTF-8's "
                    f"sort rules were not compared. {C_NO_VERSION}", [])
    elif 'C' in differs:
        _warn_block(f"C.UTF-8's sort rules differ, and it is listed in the "
                    f"summary. {C_NO_VERSION}", [])


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
        print(_fill("Unless its distro ships the file anyway, which an "
                    "upstream tag cannot show, on the new system a "
                    "database whose locale is one of these does not "
                    "connect, and a collation of one gives an error "
                    "when it is used.", '   '))
    else:
        print(_fill("On the new system, a database whose locale is one "
                    "of these does not connect, and a collation of one "
                    "gives an error when it is used.", '   '))


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
        f"Not compared, so listed in the summary ({len(listed)}): the "
        f"reader could not settle how localedef builds them, or "
        f"LC_COLLATE is stored on one side only, or an alias does not "
        f"resolve on both sides:",
        [f"{', '.join(shown_name(n) for n in names)}: {why}"
         for why, names in whys.items()])


def main(argv):
    ap = argparse.ArgumentParser(
        description="Compare the sort rules localedef builds for every "
                    "locale on the two sides, every copy followed, and the "
                    "charmaps the SUPPORTED builds use (or, for two "
                    "machines' own files, the builds measured on each) and "
                    "the collating elements they build out of "
                    "transliterations.")
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
    machine = g.is_machine(old_side) and g.is_machine(new_side)
    if nothing:
        # A tree compared with itself answers "the same" for every locale
        # the reader settles: nothing was compared, and that is said first.
        cm = machine and g.machines_charmaps(old_side, new_side)
        charmaps = {'state': 'one commit' if nothing == 'commit'
                    else 'one tree', 'why': None}
        what = (f"the two {noun} are one commit" if nothing == 'commit'
                else "the two machines' files hold the same locale sources "
                     "and charmaps" if cm
                else "the two machines' files hold the same locale sources"
                if machine
                else "the two refs are two commits of one source tree")
        print(f"Rules: not compared, {what}.")
        if machine and not cm and nothing != 'commit':
            # Never "the same charmaps": a file without them left them out
            # of its commit, and none was read. Each file's own reason,
            # which no other line of the run carries for one tree.
            print("Character sets and transliterations: not compared, a "
                  "machine's file holds no charmaps this reads.")
            said, whys = set(), []
            for sd in (old_side, new_side):
                if sd.prov.get('charmaps') is None and g.label(sd) not in said:
                    said.add(g.label(sd))
                    whys.append(f"The file of the machine that runs "
                                f"{g.label(sd)} holds none, because "
                                f"{sd.prov['charmaps_why']}.")
                    print(_fill(whys[-1], '  '))
            charmaps = {'state': 'not read', 'why': whys}
        else:
            print(f"Character sets and transliterations: not compared, "
                  f"{what}.")
        names, keyed, not_compared = [], [], []
        g.write_list(f"step2_rules_characters.{slug}.tsv",
                     characters_rows({}, []))
        write_rules_by_name(slug, noun, (old_name, new_name), machine,
                            charmaps, (old_side, new_side), nothing, what)
    else:
        early = (predates_lr_getc_fix(repo, old),
                 predates_lr_getc_fix(repo, new))
        pre = lr_getc_sides(repo, early, (old, new))
        builds = glibc_tree.builds_at(repo, [old, new], pre)
        # A machine's commit holds the tag's keyword tables, not its
        # build's: read with what the build's RHEL major adds
        # (glibc_tree.build_at).
        for sd in (old_side, new_side):
            if g.is_machine(sd):
                builds[g._ref(sd)] = glibc_tree.build_at(
                    repo, g._ref(sd), g._asked(sd.build))
        old_tree = glibc_tree.Tree.at_tag(repo, old, builds[old])
        new_tree = glibc_tree.Tree.at_tag(repo, new, builds[new])
        aliases = (g.locale_aliases(repo, old), g.locale_aliases(repo, new))
        # A machine's file holds no SUPPORTED: what it builds is what was
        # measured on it.
        supported = tuple(
            _measured(sd, al) if g.is_machine(sd) else g.supported_map(repo, t)
            for sd, al, t in ((old_side, aliases[0], old),
                              (new_side, aliases[1], new)))
        got = glibc_tree.compare_rules(old_tree, new_tree, aliases)
        differs, added, removed, listed = sort_out(got, old_tree, new_tree)
        # An entry of a machine's sources that node_entries skipped (a link, a
        # folder, a dotfile, a name it does not read) was not read
        # (glibc_locale_data.machine_side): neither added nor removed,
        # whatever the other side holds, but not compared, and listed.
        unread = {}
        for sd, which in ((old_side, 'old'), (new_side, 'new')):
            if g.is_machine(sd):
                for e, why in sd.prov['skipped']:
                    if not e.startswith('charmaps/'):
                        unread.setdefault(e, []).append(
                            f"{why} on the {which} machine")
        added = [n for n in added if n not in unread]
        removed = [n for n in removed if n not in unread]
        removed_rows = [r for r in removed_rows if r[0] not in unread]
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
        unresolved = new_tree.unresolved()
        print_added(added, renamed_from, supported[1], old_side, old_name,
                    '(not measured on that machine)' if machine
                    else '(not in SUPPORTED)',
                    {n: unresolved[n] for n in added if n in unresolved},
                    new_name)
        print_removed(removed, renamed_to, new_side, new_name)
        print_listed(got, listed)
        if unread:
            print()
            print(_fill(f"Not compared, not read among a machine's locale "
                        f"sources ({len(unread)}): "
                        + ', '.join(f"{g.shown(n)} ({'; '.join(w)})"
                                    for n, w in sorted(unread.items())),
                        '', '  '))
        print_c_utf8(((old_side, old_tree, old_name),
                      (new_side, new_tree, new_name)), differs)

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
        if not machine:
            # Reads ahead from SUPPORTED's builds, which a machine's file
            # does not hold.
            charmap_reads.prefetch(repo, read[3:], old_contents,
                                   new_contents, read[2])
        side_old, side_new, sup = charmap_reads.sides(
            repo, read[3:], old_contents, new_contents, read[2])
        sources = set(old_tree.names) | set(new_tree.names)
        already = {n for n in differs + listed if n in sources}
        if machine:
            measured, unmapped, why = machine_builds(
                repo, (old_side, new_side), aliases)
            for who, names_ in unmapped or ():
                _warn_block(f"{len(names_)} locale name(s) measured on {who} "
                            f"could not be traced to the charmap they are "
                            f"built with, so their character set is not "
                            f"compared:",
                            [f"{n}: {w}" for n, w in names_])
            keyed, cm_why = ((_charmaps_not_run(why), why) if why else
                             check_charmaps(charmap_reads, repo, side_old,
                                            side_new, sup, already,
                                            step2_words(noun, machines=True),
                                            builds=measured))
        else:
            keyed, cm_why = check_charmaps(charmap_reads, repo, side_old,
                                           side_new, sup, already,
                                           step2_words(noun))
        # Untested at this call site: on two machines' files the check's
        # own two NOT RUN (the report's, no build in common) were reached by
        # no file here; check_charmaps' test holds each reason, and
        # machine_builds' reason for a file without charmaps is asserted on
        # a real run (test_wrapper).
        write_rules_by_name(
            slug, noun, (old_name, new_name), machine,
            {'state': 'not run', 'why': cm_why} if cm_why
            else {'state': 'compared', 'why': None}, (old_side, new_side),
            locales=rules_by_name((old_side, new_side), aliases, got, unread,
                                  keyed, unmapped) if machine else None)

        # The character set check names locale files; the aliases of each
        # side's locale.alias that are built from one join the list with it
        # (swedish with sv_SE). compare_rules adds the aliases of what it
        # compares itself, but an alias of a keyed locale whose rules are the
        # same comes out "same" there (false-negative-reviewer, 9.2).
        keyed_aliases = set()
        for al in aliases:
            keyed_aliases |= set(g._aliases_reaching(set(keyed), al))
        names = sorted(set(differs) | set(listed) | set(keyed)
                       | keyed_aliases | set(unread))
        not_ascii = [n for n in names if not n.isascii()]
        names = [n for n in names if n.isascii()]
        # The names of the list step 2 could not compare, each said with
        # its reason in a `!!` (print_listed; glibc_locale_data's
        # report_machine_provenance for an entry not read), so that the
        # summary sets them apart from the rest of the list (13.31).
        not_compared = [n for n in names if n in set(listed) | set(unread)]
        print()
        print(_fill(f"Listed in the summary ({len(names)}): "
                    + (', '.join(shown_name(n) for n in names) or 'none'),
                    '', '  '))
        # SUPPORTED's note: a machine's file holds no SUPPORTED, and what a
        # machine builds is what was measured on it (step 11).
        unbuilt = [] if machine else [
            n for n in names if n in sources
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
    g.write_list(f"step2_not_compared.{slug}.txt", not_compared)
    g.write_list(f"step2_keyed_locales.{slug}.txt", keyed)
    g.write_list(f"step2_removed_locales.{slug}.txt",
                 [f"# locale files at {old_name} and not at {new_name}"]
                 + [f"{name} ({how})" for name, how in removed_rows])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
