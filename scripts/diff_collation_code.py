#!/usr/bin/env python3
"""
Diff the glibc CODE that turns locale data into collation weights, between two
tags.

Steps 1-3 of this audit compare locale data files. That is not the whole
input: identical locale data compiled by a different localedef, or compared by
a different strcoll, can sort differently. Between glibc 2.28 (RHEL8) and 2.34
(RHEL9) that is exactly what happened -- commit 82292c99b2 ("LC_COLLATE: Fix
last character ellipsis handling", Bug 22668) changed how localedef expands
ellipsis ranges, and ko_KR's sort order changed on RHEL9 even though
localedata/locales/ko_KR is byte-identical between the two tags. A data-only
diff reports ko_KR as unaffected. It is not.

Run this together with flag_algorithmic_ranges.py: that script says which
locales depend on code-computed weights, this one says whether the code that
computes them changed.

Comment, attribution and licence hunks are filtered out, and the number
filtered is always reported so the filter cannot hide anything silently. The
filter only drops what it can PROVE is prose -- a preprocessor directive, a
label or a bare declarator counts as code, because a hunk that is dropped
here is a hunk nobody reads. Use --all to see every hunk unfiltered. This tool deliberately
reports for human judgement rather than declaring a verdict: over
2.28..2.34 it surfaces both the ellipsis fix (which reorders ko_KR) and a
hash-table sizing change (which does not), and only a human can tell them
apart.

Usage:
  python3 diff_collation_code.py <old_tag> <new_tag> [--repo <path>] [--all]

Example:
  python3 diff_collation_code.py glibc-2.28 glibc-2.34
"""
import argparse
import os
import re
import sys

import glibc_locale_data as g

# The code that turns locale data into weights reaches this audit two ways.
#
# TIER1/TIER2 below are CURATED: a human ranking of what to read first. They are
# no longer the ceiling of what gets read -- reachable_from_entry_points() walks
# glibc's own #include graph and everything it finds is reported too, under
# TIER 3. Before this, a file absent from these lists was indistinguishable from
# a file that did not change, and `locale/programs/linereader.h` (lr_getc) and
# `locale/elem-hash.h` (elem_hash) both changed over 2.34..2.39 unseen.
#
# So do NOT add a path here just because it looks collation-related: if the
# include walk already reaches it, listing it here only freezes by hand what the
# walk derives. Add a path only when the walk STRUCTURALLY cannot reach it, and
# say which blind spot it falls into. There are two, both marked below:
#
#   (macro)  reached by a macro-computed #include, which a regex cannot resolve
#   (TU)     a separately compiled translation unit with no header of its own,
#            linked rather than included
ENTRY_POINTS = [
    'locale/programs/ld-collate.c',   # localedef: the collation compiler
    'string/strcoll_l.c',             # runtime comparison
    'string/strxfrm_l.c',             # runtime sort-key generation
    'wcsmbs/wcscoll_l.c',             # the wide-char variants, which #define
    'wcsmbs/wcsxfrm_l.c',             # their way into the two above
    'locale/loadlocale.c',            # reads the compiled tables back in
]

# Weight assignment and comparison: a change here can reorder any locale.
TIER1 = [
    'locale/programs/ld-collate.c',   # localedef: ellipsis expansion, weights,
                                      # sections, reorder-after
    'string/strcoll_l.c',             # runtime comparison
    'string/strxfrm_l.c',             # runtime sort-key generation
    # The wide-char comparison and sort-key wrappers: a handful of #defines
    # (`STRCMP __wcscmp`, `WEIGHT_H "../locale/weightwc.h"`) and then
    # `#include <string/strcoll_l.c>`. A change here moves wcscoll/wcsxfrm
    # behaviour and nothing else. They are ENTRY_POINTS, and the walk subtracts
    # its entry points from what it reports -- so until they were listed here
    # they were checked for existence and never diffed. Measured over
    # 2.28..2.39: copyright and URL lines only, so no verdict moved.
    'wcsmbs/wcscoll_l.c',
    'wcsmbs/wcsxfrm_l.c',
    # (macro) strcoll_l.c reaches these as `#include WEIGHT_H`, where WEIGHT_H is
    # defined by whoever includes IT -- weight.h for the narrow build, weightwc.h
    # for the wide one. Nothing resolves that without a preprocessor, and
    # locale/weight.h does change over 2.34..2.39.
    'locale/weight.h',
    'locale/weightwc.h',
    'locale/coll-lookup.h',
    # (TU) __collidx_table_lookup, compiled and linked, included by nobody.
    'locale/coll-lookup.c',
    # (TU) installs the _NL_COLLATE_* pointers when a locale is loaded.
    'locale/lc-collate.c',
    # The C locale's own collation sequence. ld-collate.c does
    # `#include "C-collate-seq.c"`, so tracking only ld-collate.c showed the
    # include line and none of the 100 lines of weights behind it. Both change
    # over 2.34..2.39 -- the RHEL9-to-RHEL10 pair.
    # (TU) C-collate.c has no header; the walk cannot reach it.
    'locale/C-collate.c',
    'locale/C-collate-seq.c',
]

# Parsing and serialisation: a change here can alter the compiled LC_COLLATE
# tables without touching the weight logic itself.
TIER2 = [
    'locale/programs/linereader.c',   # tokeniser -- how `..` is even parsed
    'locale/programs/locfile.c',      # writes the compiled category files
    'locale/programs/locfile.h',
    'locale/programs/3level.h',       # the weight table representation
    'locale/loadlocale.c',            # reads the compiled tables back in
    'locale/localeinfo.h',            # the structs those tables live in
    # The keyword table is generated from this by gperf. The generated
    # locfile-kw.h IS reachable, but it is a machine-built hash table -- 20
    # substantive hunks over 2.34..2.39, none of them readable. This is the
    # source those hunks mean, one line per keyword.
    'locale/programs/locfile-kw.gperf',
]

# Where the include walk descends. Following #include anywhere pulls in all of
# libc -- measured at 265 files and 243 substantive hunks over 2.34..2.39,
# dominated by stdio.h, unistd.h and sys/cdefs.h, none of which can move a
# collation weight. Bounded to locale/, the same walk yields 28 files and finds
# exactly the collation code.
_WALK_PREFIX = 'locale/'



_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)

# Where an #include is looked up: the including file's own directory first, then
# glibc's include roots. Enough to resolve everything under locale/; anything
# unresolved is by construction outside _WALK_PREFIX and would be dropped anyway.
_INCLUDE_ROOTS = ['', 'include/', 'locale/', 'locale/programs/', 'string/',
                  'wcsmbs/']

_HUNK_SPLIT_RE = re.compile(r'^(@@ .*?@@.*)$', re.M)
_ATTRIBUTION_RE = re.compile(r'^(Contributed by|Written by)\b')
_STAR_COMMENT_RE = re.compile(r'^\*(\s|$|/)')


def is_noise_line(line):
    """True if a changed line is provably comment or licence text.

    The test is deliberately one-sided: a line is noise only when it can be
    SHOWN to be a comment, an attribution or a licence reference. Everything
    else -- preprocessor directives, labels, bare declarators, a lone `else`
    -- is code.

    The rule used to run the other way: noise unless the line carried one of
    `;{}=()`. That silently swallowed real changes, and because a hunk is
    dropped only when every line is noise, whole hunks disappeared under the
    heading "no substantive change":

      * `+#include "C-collate-seq.c"` in ld-collate.c   (2.34 -> 2.41)
      * `-#define NO_FINALIZE` / `+#define NO_ADD_LOCALE` (2.17 -> 2.28)
      * `-# define STRCMP strcmp` in strxfrm_l.c        (2.17 -> 2.28)

    and surviving hunks showed lines like `case tok_codepoint_collation:`
    without the `>>` marker a reader is told to scan for. In a tool whose
    zero-hunk verdict is "every locale whose data file is unchanged is
    genuinely unaffected", guessing wrong in that direction is the one
    failure that matters.
    """
    body = line[1:].strip()
    if not body:
        return True
    if body.startswith(('/*', '//')):
        return True
    # A leading `*` is a comment continuation only when it stands alone:
    # followed by a space, the end of the line, or the `/` that closes the
    # block. `*wp = '\0';`, `*endp++ = '/';` and `**argv` all start with `*`
    # and are code. The old test was `startswith('*')`, which marked every
    # one of them as noise -- ten such lines sat unmarked in the two
    # published examples, and a hunk made only of them would have been
    # dropped whole under "comment/licence hunk(s) filtered".
    if _STAR_COMMENT_RE.match(body):
        return True
    # Continuation line that closes a block comment.
    if body.endswith('*/') and '/*' not in body:
        return True
    if 'Copyright (C)' in body or 'gnu.org/licenses' in body \
       or 'fsf.org' in body:
        return True
    # "Contributed by Ulrich Drepper <drepper@gnu.org>, 1995." -- a bare
    # continuation of the file's header comment, with no leading `*`.
    return bool(_ATTRIBUTION_RE.match(body))


def _comment_open_after(was_open, text):
    """Is a block comment open after this line, given that it was `was_open`?

    Called for changed and context lines alike, which is the point: a comment
    that opens on a changed line often closes on a context one.

    A scanner, not `rfind`, because `/*` inside a `//` comment or a string
    literal opens nothing -- and a state wrongly left OPEN marks the code
    after it as prose, which is the direction that hides a hunk. `x = f
    ("/*");` and `// see /* below` both did that before, and with context
    lines now read there are three times as many lines that can do it.

    Inside a block comment nothing else is special (a quote in prose is
    ordinary text), so that state is checked first. An unterminated quote --
    an apostrophe in a `//` comment, a line cut mid-string -- swallows the
    rest of the LINE only, and can then miss a `/*`: the state stays closed
    and the next lines are marked as code, which is the safe direction.
    """
    open_now, quote, i, n = was_open, None, 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if open_now:
            if two == '*/':
                open_now, i = False, i + 2
                continue
            i += 1
        elif quote:
            if text[i] == '\\':
                i += 2
                continue
            if text[i] == quote:
                quote = None
            i += 1
        elif two == '/*':
            open_now, i = True, i + 2
        elif two == '//':
            break
        elif text[i] in ('"', "'"):
            quote, i = text[i], i + 1
        else:
            i += 1
    return open_now


def classify_body(body):
    """[(line, is_noise)] for one hunk, tracking block comments per side.

    is_noise_line() alone cannot see that

        +  /* Compare the file with the locale data files for the same category
        +     in other locales, and see if we can reuse it, to save disk space.

    is one comment: the second line neither opens with `*` nor closes with
    `*/`. Walking the hunk and carrying the open-comment state (separately for
    the + and - sides, which are two different versions of the file) keeps
    those continuations out of the `>>` markers a reader is told to scan.

    A preprocessor line is never swallowed by this, so a stray `/*` inside a
    string literal cannot hide a `#include` or `#define`.

    CONTEXT lines (prefix ' ') are read and not emitted. They exist in both
    versions of the file, so they advance BOTH sides' state. Without them the
    state machine was reading every third line of the comment it was tracking:
    a `/*` on a changed line whose `*/` sat on a context line left the side
    open for the rest of the hunk, and every changed line after it -- code
    included -- was marked noise. Measured over 2.34..2.39: eight lines of
    `charmap_find_value (charmap, ...)` in locale/programs/linereader.c
    printed without the `>>` marker docs/method.md tells the reader to scan
    for. The reverse also happened: a `/*` on a context line made its changed
    continuations print as code, and two hunks made only of comment counted as
    substantive (25 -> 24 and 53 -> 52).

    The state at the START of a hunk is still unknown, and is still assumed
    "outside a comment": that direction marks prose as code, never code as
    prose.
    """
    state = {'+': False, '-': False}
    out = []
    for line in body:
        side, text = line[:1], line[1:].strip()
        if side == ' ':
            # One line, both versions: it moves the + and the - side alike.
            state['+'] = _comment_open_after(state['+'], text)
            state['-'] = _comment_open_after(state['-'], text)
            continue
        noise = (state.get(side, False) and not text.startswith('#')) \
            or is_noise_line(line)
        state[side] = _comment_open_after(state.get(side, False), text)
        out.append((line, noise))
    return out


def split_hunks(diff_text):
    """[(header, body_lines)] for one file's diff, CONTEXT lines included.

    classify_body needs them to track where a block comment opens and closes;
    it drops them again after reading, so report_file still prints only the
    `+` and `-` lines.

    Where a hunk ends is asked structurally, not by pattern. Inside a hunk
    every content line begins with ' ', '+', '-' or the backslash of
    "No newline at end of file"; anything else is the start of the next file's `diff --git`
    header, so the body stops there. That is also what keeps a file header out
    of the body without matching on `+++`/`---`, which is what the previous
    filter did: `not ln.startswith(('+++', '---'))` also discarded a CHANGED
    line whose own content began with `++` or `--` (`+++i;`, `--argc;`,
    `-- a SQL comment`). None appears in the five pinned tags, so it was
    latent -- but a discarded line is one the noise filter never sees, and a
    hunk whose remaining lines are all comment is dropped whole.
    """
    parts = _HUNK_SPLIT_RE.split(diff_text)
    hunks = []
    for i in range(1, len(parts), 2):
        body = []
        for ln in parts[i + 1].split('\n'):
            if ln[:1] in ('+', '-', ' '):
                body.append(ln)
            elif ln == '' or ln[:1] == '\\':
                continue      # split artefact, or "\ No newline at end of file"
            elif ln.startswith('diff --git '):
                break         # the next file in a multi-file diff
            else:
                # Not content and not the next file: the shape this parser
                # assumes is wrong, and every line after it would be dropped
                # from a hunk that is then all-noise and filtered. Measured
                # with `color.diff=always`: every body line begins with an
                # escape, so each hunk came back empty and step 5 called the
                # pair clean.
                g.die(f"unexpected line in a diff hunk body, so the rest of "
                      f"the hunk was not read:\n{ln[:200]!r}", 2)
        hunks.append((parts[i], body))
    return hunks


def _tracked_files(repo, tag):
    """Every path in the tree at `tag`, as a set. One ls-tree, no blob fetch."""
    out = g.run_git(['ls-tree', '-r', '--name-only', tag], repo)
    return set(out.stdout.decode('utf-8', 'replace').split('\n')) - {''}


def reachable_from_entry_points(repo, tag):
    """Collation code reachable from ENTRY_POINTS by #include, at `tag`.

    The point of this is that TIER1/TIER2 stop being the ceiling. A hand list
    can only contain what somebody thought of; this walks what glibc actually
    includes, so a file that becomes part of the collation path in a future
    release is picked up without anyone editing a list.

    Two bounds keep it useful rather than merely complete:

      * The walk descends only into _WALK_PREFIX. Unbounded, it reaches 265
        files whose diffs are dominated by stdio.h and sys/cdefs.h -- correct,
        and unreadable.
      * For every header reached, the sibling .c is added if it exists. glibc
        links coll-lookup.c, simple-hash.c and locfile.c rather than including
        them, so an #include walk alone sees their declarations and never their
        code.

    What it CANNOT see is recorded in the TIER1/TIER2 comments: macro-computed
    includes (`#include WEIGHT_H`) and translation units with no header at all.
    Those are why the curated lists still exist.
    """
    tracked = _tracked_files(repo, tag)

    def resolve(inc, from_dir):
        for base in ([from_dir + '/'] if from_dir else []) + _INCLUDE_ROOTS:
            cand = os.path.normpath(base + inc) if base else inc
            if cand in tracked:
                return cand
        return None

    seen, frontier = set(), [p for p in ENTRY_POINTS if p in tracked]
    while frontier:
        batch = [p for p in frontier if p not in seen]
        seen.update(batch)
        # Strict: every path here came out of the tree at this same tag, so
        # an unreadable one is a failed read, not an absent file. Letting it
        # through would quietly shorten the walk, and a shorter walk reports
        # fewer changed files -- the reassuring direction.
        contents = g.read_blobs_strict(repo, tag, batch,
                                       'the collation include walk')
        frontier = []
        for path in batch:
            text = contents.get(path)
            if text is None:
                continue
            for inc in _INCLUDE_RE.findall(text):
                target = resolve(inc, os.path.dirname(path))
                if (target is not None and target not in seen
                        and target.startswith(_WALK_PREFIX)):
                    frontier.append(target)

    siblings = {p[:-2] + '.c' for p in seen if p.endswith('.h')}
    return (seen | (siblings & tracked)) - set(ENTRY_POINTS)


def check_paths(repo, paths, old_tag, new_tag):
    """Classify tracked paths that do not exist at both tags.

    Returns (vanished, outside): paths present at `old_tag` but gone at
    `new_tag`, and paths present at neither.

    `vanished` is a blind spot outright. `outside` is TWO facts wearing one
    name, and absent_at_both() below separates them: a file not yet written
    when the range begins really has nothing to say about it, while a file
    renamed away BEFORE the older tag reads exactly the same here and is the
    same blind spot one range earlier. A file that appears only at the new tag
    is fine either way: the diff shows it added, in full. What `git diff`
    cannot tell you about is a file that was renamed out from under the audit,
    which reads exactly like "this file did not change".

    Existence is asked with `ls-tree`, not `cat-file -e`. On a
    `--filter=blob:none` clone `cat-file -e` must fetch the blob to answer,
    and calls a file that exists absent whenever that fetch cannot happen --
    offline, or against a dead promisor. `ls-tree` reads the tree, which such
    a clone always has.
    """
    def present(tag, path):
        # No allow_fail: `git ls-tree` exits 0 with empty output for a path
        # that is not in the tree, and non-zero only on a real error (a bad
        # tag, an unreadable object). Suppressing that turned an error into
        # `False` for BOTH tags, which check_paths then filed under "exists at
        # neither tag -- nothing to read, and nothing to miss" and printed as
        # harmless, for a file that exists and was never read.
        out = g.run_git(['ls-tree', '--name-only', tag, '--', path], repo)
        return bool(out.stdout.strip())

    vanished, outside = [], []
    for path in paths:
        at_old, at_new = present(old_tag, path), present(new_tag, path)
        if at_old and not at_new:
            vanished.append(path)
        elif not at_old and not at_new:
            outside.append(path)
    return vanished, outside


def absent_at_both(repo, paths, old_tag, new_tag):
    """Split paths absent at BOTH tags into (renamed, unborn, never).

    `check_paths` files them all under "nothing to read, and nothing to miss",
    which is true of only one of the two. A file that has no history at
    `old_tag` did not exist yet -- locale/C-collate-seq.c arrives in 2.35, so
    over 2.28..2.34 there is genuinely nothing to read. A file that HAS history
    there and is in neither tree was renamed away before the older tag, and
    that is the same blind spot `vanished` exists for, one range earlier: the
    include walk starts nowhere, TIER 3 comes back empty, and the step prints
    its clean sentence over a walk that read nothing.

    `git log -1 <tag> -- <path>` answers it without a blob: empty output means
    no commit in that tag's history ever touched the path. Measured over the
    three audited pairs: locale/C-collate-seq.c is the only path absent at both
    tags, and it comes back not-yet-born on 2.28..2.34 and 2.12..2.17.

    "Not yet born" is itself two facts, so it is asked once more against every
    ref in the clone: a path NO ref ever carried is not a file waiting to be
    written, it is a typo in the curated lists -- and the curated lists are
    the ceiling of what a tier gets read. Measured with ld-collate.c spelt
    `ld-colate.c` in ENTRY_POINTS and TIER1: 2.28..2.34 reported 6 substantive
    hunks instead of 24 and a coverage of 8 files instead of 27, with the Bug
    22668 hunks gone and no `!!` anywhere.
    """
    if paths:
        # `git log` on a shallow clone exits 0 with empty output for every path
        # whose last commit is beyond the boundary, and empty is the half of
        # this answer that means "nothing to miss". Measured on a depth-1 clone
        # of the same repository: locale/xlocale.h, deleted before 2.28, came
        # back not-yet-born and the step printed its clean sentence.
        #
        # Only `true` and `false` are answers. `--is-shallow-repository` dates
        # from git 2.15, and an older `rev-parse` ECHOES an option it does not
        # know and exits 0 -- which is not `true`, so the guard would be off
        # with nothing said. "Could not ask" is its own case.
        shallow = g.run_git(['rev-parse', '--is-shallow-repository'],
                            repo).stdout.strip()
        if shallow not in (b'true', b'false'):
            g.die(f"`git rev-parse --is-shallow-repository` answered "
                  f"{shallow!r}, which is neither true nor false, so whether "
                  f"this clone has the history step 5 needs is unknown "
                  f"(git older than 2.15?).", 2)
        if shallow == b'true':
            g.die("this glibc clone is shallow, so `git log` cannot say "
                  "whether a path absent at both tags was renamed away or had "
                  "not been written yet. Clone without --depth "
                  "(--filter=blob:none is fine).", 2)
    renamed, unborn, never = [], [], []
    for path in paths:
        # BOTH tags, and --full-history. Asking only the older one files a path
        # that was added and removed INSIDE the range under "not yet written":
        # measured with posix/spawnattr_tcgetpgrp.c over 2.34..2.39, added by
        # 342cc934a3 and removed by 6289d28d3c, which came back not-yet-born.
        # Default history simplification drops a path that lived and died on a
        # side branch that was later merged (reproduced in a fabricated repo);
        # --full-history keeps it, and leaves all three real answers unchanged.
        #
        # No allow_fail: `git log` exits 0 with empty output for a path with no
        # history, and non-zero only on a real error. Suppressing that would
        # file every path under "did not exist yet" -- the reassuring half.
        seen = any(
            g.run_git(['log', '-1', '--full-history', '--format=%H', tag,
                       '--', path], repo).stdout.strip()
            for tag in (old_tag, new_tag))
        if seen:
            renamed.append(path)
            continue
        anywhere = g.run_git(['log', '-1', '--full-history', '--format=%H',
                              '--all', '--', path], repo).stdout.strip()
        (unborn if anywhere else never).append(path)
    return renamed, unborn, never


def report_file(repo, path, rng, show_all, quiet_when_clean=False):
    """Print one file's substantive hunks; return how many there were.

    `quiet_when_clean` suppresses the "no substantive change" line. TIER 1 and
    TIER 2 are curated and short, so naming every file that was checked is the
    point. TIER 3 is derived and mostly clean -- 14 of 20 files over
    2.34..2.39 -- and a line each buries the two hunks that matter. The count
    is reported in the coverage line instead, so nothing goes unaccounted for.
    """
    # No allow_fail: `git diff` without --quiet exits 0 whether or not there
    # are differences, so a non-zero exit is always a real error. With it
    # suppressed, a failed diff gave empty stdout and returned 0 here -- the
    # file was reported as having no substantive change, without a word.
    # Six flags, six ways a config this run does not control turns every
    # diff into "no substantive change", or moves the number it reports:
    #
    #   --no-ext-diff  `diff.external`, or GIT_EXTERNAL_DIFF in the environment
    #                  (which beats config, so pinning config alone is not
    #                  enough). Measured: GIT_EXTERNAL_DIFF=/usr/bin/true made
    #                  all 39 files this step diffs over 2.28..2.34 read as
    #                  unchanged, with 6 hunks in ld-collate.c alone.
    #   --no-textconv  a `diff.<driver>.textconv` reached through the user's
    #                  core.attributesFile. --no-ext-diff does NOT disable it,
    #                  and a textconv that empties both sides leaves an EMPTY
    #                  diff -- so the guard below never sees it either.
    #   --no-color     `color.diff` beats the `color.ui=false` in
    #                  GIT_CONFIG_OVERRIDES (more specific wins). Measured with
    #                  `color.diff=always` and `color.diff.frag=normal`: the
    #                  hunk headers stay plain so they still match, every body
    #                  line starts with an escape, each hunk comes back empty
    #                  and therefore all-noise, and step 5 printed its clean
    #                  sentence over the pair that carries Bug 22668.
    #   -U3            how many context lines the classifier gets to read.
    #                  diff.context=0/1/2 gives 106/76/61 hunks over 2.34..2.39
    #                  instead of 52: not the reassuring direction, but a
    #                  published number must not move with a user's config, and
    #                  at zero context the comment tracking is blind again.
    #                  GIT_DIFF_OPTS would beat this flag, so run_git drops it.
    #   --inter-hunk-context=0
    #                  how far apart two changes must be to stay two hunks.
    #                  diff.interHunkContext=50 merges them: 31 hunks over
    #                  2.34..2.39 instead of 52, with the same 733 `>>` lines.
    #                  Nothing hidden, but the same published-number drift.
    #   --diff-algorithm=myers
    #                  patience and histogram pair the same changed lines into
    #                  different hunks: 52 hunks either way over 2.34..2.39,
    #                  but 731 `>>` lines instead of 733. Every changed line
    #                  still carries its marker, so again nothing is hidden --
    #                  and again a published number must not move with a
    #                  reader's config.
    diff_text = g.run_git(['diff', '--no-ext-diff', '--no-textconv',
                           '--no-color', '-U3', '--inter-hunk-context=0',
                           '--diff-algorithm=myers', rng, '--', path],
                          repo).stdout.decode('utf-8', 'replace')
    if not diff_text.strip():
        return 0
    hunks = split_hunks(diff_text)
    if not hunks:
        # Output that is not empty and holds no hunk: "Binary files ... differ",
        # or a diff this parser does not understand. Either way the file DID
        # change and nothing here read the change, which is not the same fact
        # as "no substantive change".
        g.die(f"{path}: `git diff` returned output with no hunk in it, so the "
              f"change was not read:\n{diff_text[:400]}", 2)
    kept, filtered = [], 0
    for header, body in hunks:
        marked = classify_body(body)
        if show_all or any(not noise for _, noise in marked):
            kept.append((header, marked))
        else:
            filtered += 1

    if not kept:
        if not quiet_when_clean:
            print(f"  {path}: no substantive change "
                  f"({filtered} comment/licence hunk(s) filtered)")
        return 0

    note = f", {filtered} comment/licence hunk(s) filtered" if filtered else ""
    print(f"  {path}: {len(kept)} substantive hunk(s){note}")
    for header, marked in kept:
        print(f"      {header.strip()}")
        for ln, noise in marked:
            print(f"      {'  ' if noise else '>>'} {ln}")
    log = g.run_git(['log', '--oneline', '--no-merges', rng, '--', path],
                    repo).stdout.decode('utf-8', 'replace')
    if log.strip():
        print("      commits:")
        for line in log.strip().split('\n'):
            print(f"        {line}")
    return len(kept)


def main(argv):
    ap = argparse.ArgumentParser(
        description="Diff glibc's collation code between two tags.")
    ap.add_argument('old_tag')
    ap.add_argument('new_tag')
    ap.add_argument('--repo', help="path to the glibc clone (autodetected)")
    ap.add_argument('--all', action='store_true',
                    help="show every hunk, including comment-only ones")
    opts = ap.parse_args(argv)

    repo = g.find_repo(opts.repo)
    g.check_refs(repo, opts.old_tag, opts.new_tag)
    rng = f'{opts.old_tag}..{opts.new_tag}'

    print(f"Collation code changes between {opts.old_tag} and {opts.new_tag}")
    print()

    # Derived at BOTH tags and unioned: the walk at the new tag alone cannot
    # see a file that existed at the old one and was removed or renamed away,
    # which is the same "reads exactly like unchanged" failure check_paths()
    # exists for.
    derived = (reachable_from_entry_points(repo, opts.old_tag)
               | reachable_from_entry_points(repo, opts.new_tag))
    tiered = set(TIER1) | set(TIER2)
    tier3 = sorted(derived - tiered)

    # ENTRY_POINTS included: if one is renamed away the whole walk collapses to
    # nothing, and a collapsed walk reads exactly like a clean result. Each
    # path once -- the wide-char wrappers are entry points AND in TIER1.
    tracked_paths = list(dict.fromkeys(ENTRY_POINTS + TIER1 + TIER2))
    vanished, outside = check_paths(repo, tracked_paths,
                                    opts.old_tag, opts.new_tag)
    if vanished:
        # `!!` plus three-space continuation lines: the shape audit.sh's
        # summary collects and repeats verbatim. Before this the notice was
        # plain prose, printed 300 lines above a summary that went on to say
        # "a clean data diff is sufficient".
        print(f"!! {len(vanished)} tracked path(s) present at {opts.old_tag} "
              f"and GONE at {opts.new_tag}. `git diff`")
        print("   over a missing path is empty, not an error, so a rename "
              "reads exactly like")
        print('   "unchanged":')
        for path in vanished:
            print(f"     {path}: ABSENT at {opts.new_tag}")
        print("   Find where each moved and add the new path to TIER1/TIER2 "
              "before trusting")
        print("   a no-change result.")
        print()
    # Absent at both tags is two different facts, and only one of them is
    # harmless. Splitting them is the whole of false negative "a tracked path
    # absent from both tags was called harmless".
    unreadable, outside, never = absent_at_both(repo, outside,
                                                opts.old_tag, opts.new_tag)
    if unreadable:
        print(f"!! {len(unreadable)} tracked path(s) have history in this "
              f"clone and are in NEITHER")
        print(f"   tree. Renamed or removed before {opts.old_tag}, or added "
              f"and removed inside")
        print("   the range: either way this audit read nothing for them and "
              "`git diff`")
        print("   reported no error, which is the same blind spot as a path "
              "that vanishes")
        print("   inside the range:")
        for path in unreadable:
            print(f"     {path}: ABSENT at {opts.old_tag} and "
                  f"{opts.new_tag}")
        print("   Find where each moved and update ENTRY_POINTS/TIER1/TIER2 "
              "before trusting")
        print("   a no-change result.")
        print()
    if never:
        print(f"!! {len(never)} tracked path(s) exist at no ref in this clone "
              f"at all. A path")
        print("   nothing ever carried is not a file waiting to be written, "
              "it is a name in")
        print("   ENTRY_POINTS/TIER1/TIER2 that matches nothing -- and those "
              "lists are the")
        print("   ceiling of what this step reads:")
        for path in never:
            print(f"     {path}: no ref in this clone has ever had it")
        print("   Correct the spelling, or the path if the file moved, before "
              "trusting a")
        print("   no-change result.")
        print()
    if not derived:
        print("!! The include walk reached 0 file(s). Every hunk this step "
              "reports below comes")
        print("   from the curated lists alone, and TIER 3 -- the part that "
              "grows on its own as")
        print("   glibc changes -- is empty because the walk collapsed, not "
              "because glibc has no")
        print("   other collation code.")
        print()
    if outside:
        print("Tracked files that exist at neither tag and have no history at "
              f"{opts.old_tag}")
        print("-- not yet written when this range begins, so nothing to read "
              "and nothing to")
        print("miss:")
        for path in outside:
            print(f"  {path}")
        print()

    total = 0
    print("TIER 1 -- weight assignment and comparison")
    print("  (a change here can reorder any locale, including ones whose data "
          "file is unchanged)")
    for path in TIER1:
        total += report_file(repo, path, rng, opts.all)
    print()
    print("TIER 2 -- parsing and serialisation")
    print("  (a change here can alter the compiled tables without touching the "
          "weight logic)")
    tier2 = 0
    for path in TIER2:
        tier2 += report_file(repo, path, rng, opts.all)
    total += tier2

    print()
    print("TIER 3 -- reachable from the collation entry points, not classified")
    print("  (derived by walking #include from ld-collate.c, strcoll_l.c, "
          "strxfrm_l.c and the")
    print("   wide-char variants, bounded to locale/, plus the sibling .c of "
          "every header")
    print("   reached -- so this list grows on its own as glibc changes)")
    tier3_clean = 0
    for path in tier3:
        n = report_file(repo, path, rng, opts.all, quiet_when_clean=True)
        total += n
        if n == 0:
            tier3_clean += 1
    if tier3_clean:
        print(f"  ({tier3_clean} further file(s) reached and read, with no "
              f"substantive change)")

    print()
    print(f"Coverage: {len(derived)} file(s) reached by the include walk, "
          f"{len(tier3)} of them beyond")
    print(f"TIER 1/2. The walk cannot follow a macro-computed include "
          f"(`#include WEIGHT_H`) or")
    print(f"reach a translation unit with no header of its own -- "
          f"locale/weight.h, weightwc.h,")
    print(f"lc-collate.c and C-collate.c are in TIER 1 by hand for exactly "
          f"that reason.")

    # Four reasons the clean sentence is refused, each printed as its own `!!`
    # block above. They are collected rather than tested one at a time so that
    # adding a fifth cannot leave the clean branch reachable by accident.
    blockers = []
    if vanished:
        blockers.append(f"{len(vanished)} tracked path(s) vanished before "
                        f"{opts.new_tag}")
    if unreadable:
        blockers.append(f"{len(unreadable)} path(s) this audit must read are "
                        f"absent from both tags")
    if never:
        blockers.append(f"{len(never)} tracked path(s) exist at no ref in "
                        f"this clone")
    if not derived:
        blockers.append("the include walk reached no file")

    print()
    if total == 0 and blockers:
        # Deliberately NOT the "No substantive collation code change" sentence:
        # audit.sh treats that exact sentence as the clean verdict, and this is
        # not one.
        print("No substantive change in the files this audit could read.")
        print("This is NOT a clean result:")
        for reason in blockers:
            print(f"  - {reason}")
        print("Resolve the paths listed above, then re-run.")
    elif total == 0:
        print("No substantive collation code change. Every locale whose data "
              "file is unchanged")
        print("is genuinely unaffected, including the algorithmic-range "
              "locales that")
        print("flag_algorithmic_ranges.py lists -- steps 1-3 are sufficient "
              "for this pair.")
    else:
        print(f"{total} substantive hunk(s) found. Locale data alone does not "
              f"settle this pair.")
        print("Lines marked >> are the code changes; read them and decide "
              "whether they can")
        if g.wrapped():
            print("move weights. If any can, every locale step 4 listed above "
                  "needs an")
            print("empirical sort-order test, however clean its data diff is.")
        else:
            print("move weights. If any can, every locale listed by")
            print(f"  python3 flag_algorithmic_ranges.py {opts.new_tag}")
            print("needs an empirical sort-order test, however clean its data "
                  "diff is.")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
