"""Layer 2: the git-backed helpers, against the real clone.

This layer exists for one class of bug: code that cannot tell "there is nothing
here" from "I could not look". Both print the same thing, and in this tool the
thing they print is a clean verdict.

The failures are provoked with invalid tags and paths. No network is touched and
the clone is never modified.
"""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _harness import (FLOOR_NEW, FLOOR_OLD, GLIBC_CLONE, MID, NEW, OLD,
                      SCRIPTS_DIR, Step2Report, deleted_steps_named, flat,
                      glibc_shaped, needs_clone, needs_floor_pair)

import glibc_locale_data as g

BAD = 'no-such-tag-4c1f'


def in_subprocess(code):
    """Run code that is expected to call die(); return (exit, output).

    die() calls sys.exit, so these have to run out of process to be observed
    without tearing down the test runner.
    """
    boot = ("import sys; sys.path.insert(0, %r); "
            "import glibc_locale_data as g; "
            "repo = %r\n" % (os.path.dirname(g.__file__), GLIBC_CLONE))
    p = subprocess.run([sys.executable, '-c', boot + code],
                       capture_output=True, cwd=os.path.dirname(g.__file__))
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


@needs_clone
class DescendsFrom(unittest.TestCase):
    """Whether a tag's localedef reads its input as UTF-8 is a question of
    ancestry, and a commit git cannot place is not a no."""

    def test_the_utf8_reader_by_tag(self):
        import collation_keys as ck
        for tag, want in (('glibc-2.35', False), ('glibc-2.36', True),
                          ('glibc-2.39', True)):
            with self.subTest(tag):
                self.assertIs(g.descends_from(GLIBC_CLONE, ck.UTF8_READER,
                                              tag), want)
        self.assertIsNone(g.descends_from(GLIBC_CLONE, '0' * 40,
                                          'glibc-2.39'))
        # Step 2 asks through reads_utf8, which refuses a shallow clone
        # whenever git cannot say yes (AShallowCloneCannotAnswerThis): the
        # full one answers.
        import filter_lc_collate_changes as f
        self.assertIs(f.reads_utf8(GLIBC_CLONE, 'glibc-2.39'), True)
        self.assertIs(f.reads_utf8(GLIBC_CLONE, 'glibc-2.34'), False)

    def test_the_lr_getc_fix_by_tag(self):
        """Backlog 13.18: before glibc 2.36, lr_getc reads 0x1A and 0xFF
        as the end of the file."""
        import filter_lc_collate_changes as f
        for tag, want in (('glibc-2.28', True), ('glibc-2.34', True),
                          ('glibc-2.35', True), ('glibc-2.36', False),
                          ('glibc-2.39', False)):
            with self.subTest(tag):
                self.assertIs(f.predates_lr_getc_fix(GLIBC_CLONE, tag)(),
                              want)


@needs_clone
class AShallowCloneCannotAnswerThis(unittest.TestCase):
    """`git log` on a shallow clone exits 0 with empty output for any path
    whose last commit is beyond the boundary -- and empty is the half of
    absent_at_both's answer that means "nothing to miss". Found by the
    false-negative reviewer on this PR: on a depth-1 clone, locale/xlocale.h
    (deleted before 2.28) came back not-yet-born and step 5 printed its clean
    sentence."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-shallow-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'glibc')
        # file://, not a path: a LOCAL clone ignores --depth and hardlinks the
        # whole object store, so a path-cloned "shallow" repository is not
        # shallow at all and the test would pass against a repository that
        # cannot reproduce the defect.
        p = subprocess.run(['git', 'clone', '--quiet', '--depth', '1',
                            '--branch', OLD, '--no-checkout',
                            'file://' + os.path.abspath(GLIBC_CLONE),
                            self.repo], capture_output=True)
        if p.returncode != 0:
            self.skipTest('could not make a shallow clone: '
                          + p.stderr.decode('utf-8', 'replace')[:200])
        shallow = subprocess.run(['git', '-C', self.repo, 'rev-parse',
                                  '--is-shallow-repository'],
                                 capture_output=True)
        self.assertEqual(shallow.stdout.strip(), b'true',
                         'the fixture clone is not shallow')

    def test_two_predates_are_not_trusted_on_a_shallow_clone(self):
        """Round 2 of the 13.18 review, finding 6: on a shallow clone git
        can answer no for a tag that holds LR_GETC_FIX, and two such
        answers would skip the files that did not change."""
        ask = "(lambda: True, lambda: True), ('%s', '%s')" % (OLD, MID)
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "print('ANSWER', f.lr_getc_sides(%r, %s))" % (self.repo, ask))
        self.assertNotEqual(rc, 0, 'a shallow clone answered for both tags')
        self.assertIn('shallow', out)
        self.assertNotIn('ANSWER', out)
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "print('ANSWER', f.lr_getc_sides(repo, %s))" % ask)
        self.assertEqual(rc, 0, out)
        self.assertIn("ANSWER {'%s': True, '%s': True}" % (OLD, MID), out,
                      'the control: the full clone')

    def test_step_2_refuses_it_when_git_cannot_say_yes(self):
        """On a depth-1 clone git places neither tag relative to
        UTF8_READER, whatever they hold, and two such sides would read as
        one reader (round 7 of the 13.7 review): anything but a yes is
        refused there."""
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "print('ANSWER', f.reads_utf8(%r, '%s'))" % (self.repo, OLD))
        self.assertNotEqual(rc, 0, 'a shallow clone answered about UTF-8')
        self.assertIn('shallow', out)
        self.assertIn('reads its input as UTF-8', flat(out))
        self.assertNotIn('ANSWER', out)
        # A no can be wrong on a shallow clone too (one that holds the
        # commit and cuts the path to it): refused as well.
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "g.descends_from = lambda *a: False\n"
            "print('ANSWER', f.reads_utf8(%r, '%s'))" % (self.repo, OLD))
        self.assertNotEqual(rc, 0, 'a shallow clone answered no about UTF-8')
        self.assertIn('reads its input as UTF-8', flat(out))
        self.assertNotIn('ANSWER', out)


    def test_an_answer_that_is_neither_true_nor_false_is_not_read_as_deep(self):
        """`--is-shallow-repository` dates from git 2.15; an older `rev-parse`
        echoes an option it does not know and exits 0. That is not `true`, so
        a `== b'true'` guard would be off with nothing said."""
        rc, out = in_subprocess(
            "class R:\n"
            "    stdout = b'--is-shallow-repository\\n'\n"
            "real = g.run_git\n"
            "g.run_git = lambda a, *rest, **k: (R() if a[:1] == ['rev-parse']\n"
            "                                   else real(a, *rest, **k))\n"
            "g.require_full_history(repo, 'step 2', 'a test')\n"
            "print('ANSWER')")
        self.assertNotEqual(rc, 0, 'an unusable answer was read as "not shallow"')
        self.assertIn('neither true nor false', out)
        self.assertNotIn('ANSWER', out)

    def test_the_full_clone_still_answers(self):
        """Control: the refusal must not fire on the clone this tool uses."""
        self.assertIsNone(g.require_full_history(GLIBC_CLONE, 'step 2',
                                                 'a test'))


@needs_clone
class UnreadableBlobsAreNotDropped(unittest.TestCase):
    """read_blobs_strict: paths that came out of the tree at this tag must all
    be readable. Dropping one shrinks the copy graph or the include walk, and a
    smaller graph yields a shorter affected-locale list."""

    def test_aborts_and_names_the_path_it_could_not_read(self):
        rc, out = in_subprocess(
            "g.read_blobs_strict(repo, '%s',"
            " ['localedata/locales/no-such-locale'], 'the copy graph')" % NEW)
        self.assertNotEqual(rc, 0)
        self.assertIn('no-such-locale', out)
        self.assertIn('the copy graph', out, 'error does not say what broke')

    def test_valid_paths_are_returned(self):
        got = g.read_blobs_strict(GLIBC_CLONE, NEW,
                                  ['localedata/locales/sv_SE'], 'a test')
        self.assertIn('localedata/locales/sv_SE', got)
        self.assertIn('LC_COLLATE', got['localedata/locales/sv_SE'])

    # The two tests below assert the CALL SITES, not the helper. Mutation
    # testing showed why: reverting the copy graph's read (build_copy_graph,
    # since replaced by glibc_tree.Tree.at_tag) to `contents, _ =
    # read_blobs(...)` left the whole suite green, because every path it reads
    # comes from ls-tree at the same tag and so `missing` is empty in practice.
    # Testing the helper alone guards nothing. Injecting a missing blob is what
    # makes the guard observable.

    def test_the_tree_aborts_on_an_unreadable_blob(self):
        """A dropped blob silently shrinks the copy chains the steps
        walk (glibc_tree), and a smaller set yields a shorter affected-locale
        list -- the reassuring direction."""
        rc, out = in_subprocess(
            "import glibc_tree\n"
            "real = g.read_blobs\n"
            "def fake(repo, tag, paths, raw=False):\n"
            "    c, _ = real(repo, tag, paths, raw=raw)\n"
            "    dropped = sorted(c)[0]\n"
            "    del c[dropped]\n"
            "    return c, {dropped}\n"
            "g.read_blobs = fake\n"
            "glibc_tree.Tree.at_tag(repo, %r)\n" % NEW)
        self.assertNotEqual(rc, 0, 'an unreadable locale was dropped silently')
        self.assertIn('could not be read while building the locale files',
                      flat(out))


@needs_clone
class OneBatchForSeveralTags(unittest.TestCase):
    """read_blobs_at: step 2 reads at two tags in one `cat-file --batch`.
    Each group must get its own answers: a body landing in the wrong group
    would judge a file on the other tag's text, and a `missing` landing in
    the wrong group would refuse the wrong read or none."""

    SV = 'localedata/locales/sv_SE'
    NONE = 'localedata/locales/no-such-locale'

    def test_each_group_gets_its_own_answers(self):
        (old, old_missing), (mid, mid_missing), (empty, none) = g.read_blobs_at(
            GLIBC_CLONE, [(OLD, [self.SV, self.NONE]), (MID, [self.SV]),
                          (MID, [])])
        self.assertEqual(old, g.read_blobs(GLIBC_CLONE, OLD, [self.SV])[0])
        self.assertEqual(mid, g.read_blobs(GLIBC_CLONE, MID, [self.SV])[0])
        self.assertNotEqual(old[self.SV], mid[self.SV],
                            'sv_SE no longer differs between the two tags; '
                            'this test cannot tell the groups apart')
        self.assertEqual((old_missing, mid_missing), ({self.NONE}, set()))
        self.assertEqual((empty, none), ({}, set()))

    def test_a_stream_cut_inside_a_body_or_too_long_is_refused(self):
        """A header promises a size, and a slice past the end of the stream
        is shorter rather than an error: a body cut short was taken as the
        whole file. Output left over after the last answer means the
        answers may not line up with the requests (false-negative-reviewer).
        """
        for stdout, said in ((b'abc blob 5000\nshort\n', 'truncated'),
                             (b'abc blob 2\nok\nextra\n', 'more than')):
            with self.subTest(said=said):
                rc, out = in_subprocess(
                    "class R:\n"
                    "    returncode = 0\n"
                    "    stdout = %r\n"
                    "    stderr = b''\n"
                    "g.subprocess.run = lambda *a, **k: R()\n"
                    "g.read_blobs_at(repo, [(%r, ['x'])])" % (stdout, OLD))
                self.assertNotEqual(rc, 0, out)
                self.assertIn(said, out)


@needs_clone
class SupportedMap(unittest.TestCase):
    """"supported_map() returned an empty map when SUPPORTED was missing",
    which made every locale print as 'not built by default'."""

    def test_aborts_when_supported_is_absent(self):
        rc, out = in_subprocess("g.supported_map(repo, '%s')" % BAD)
        self.assertNotEqual(rc, 0)
        # The refusal itself, not any exit: until step 9.3 the helper
        # imported a module that no longer exists, and that import error
        # passed a test reading only the exit code.
        self.assertIn('localedata/SUPPORTED does not exist at %s' % BAD,
                      flat(out))

    def test_maps_source_names_to_generated_names(self):
        got = g.supported_map(GLIBC_CLONE, NEW)
        self.assertIn('sv_SE.utf8', got['sv_SE'])
        self.assertIn('C.utf8', got['C'])


@needs_clone
class LocaleAliases(unittest.TestCase):
    """Backlog 13.1: the names glibc's locale.alias gives a locale (swedish)
    are names a database can use, and a missing file must stop the run rather
    than read as "no aliases", which would drop swedish from the list."""

    def test_aborts_when_locale_alias_is_absent(self):
        rc, out = in_subprocess("g.locale_aliases(repo, '%s')" % BAD)
        self.assertNotEqual(rc, 0)
        self.assertIn('intl/locale.alias does not exist', out)

    def test_aborts_when_locale_alias_holds_no_alias(self):
        """A file that is there and holds nothing but comments is a reader or
        a corpus problem, not "no aliases"."""
        repo = tempfile.mkdtemp(prefix='pg-glibc-empty-alias-')
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        os.makedirs(os.path.join(repo, 'intl'))
        with open(os.path.join(repo, 'intl', 'locale.alias'), 'w') as fh:
            fh.write('# only a comment\n\n')
        git = ['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t',
               '-c', 'commit.gpgsign=false']
        subprocess.run(['git', 'init', '-q', repo], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['commit', '-q', '-m', 'x'], check=True)
        boot = ("import sys; sys.path.insert(0, %r); "
                "import glibc_locale_data as g\n" % os.path.dirname(g.__file__))
        p = subprocess.run([sys.executable, '-c',
                            boot + 'g.locale_aliases(%r, "HEAD")' % repo],
                           capture_output=True)
        out = (p.stdout + p.stderr).decode('utf-8', 'replace')
        self.assertNotEqual(p.returncode, 0, out)
        self.assertIn('holds no alias at all', out)

    def test_reads_the_tag_s_aliases_and_no_comment(self):
        got = g.locale_aliases(GLIBC_CLONE, NEW)
        self.assertEqual(got['swedish'], 'sv_SE.ISO-8859-1')
        self.assertEqual(got['thai'], 'th_TH.TIS-620')
        self.assertEqual(len(got), 45)
        self.assertEqual([a for a in got if a.startswith('#')], [])


@needs_clone
class CloneDetection(unittest.TestCase):
    """"Nothing ran on a clean machine": the clone is made --no-checkout, so
    localedata/locales never appears on disk and a filesystem test says no."""

    def test_accepts_the_real_clone(self):
        self.assertTrue(g._is_glibc_clone(GLIBC_CLONE))

    def test_rejects_a_directory_that_is_not_a_repo(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertFalse(g._is_glibc_clone(tmp))
        finally:
            shutil.rmtree(tmp)

    def test_rejects_an_unrelated_git_repo(self):
        tmp = tempfile.mkdtemp()
        try:
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            self.assertFalse(g._is_glibc_clone(tmp))
        finally:
            shutil.rmtree(tmp)

    def test_rejects_a_path_that_does_not_exist(self):
        self.assertFalse(g._is_glibc_clone('/no/such/path/4c1f'))


def git(repo, *args, env=None):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True,
                   env=env)


def make_glibc_shaped_repo(root, n_files, rename=False, dates=None,
                           base=MID):
    """A git repository that passes _is_glibc_clone: localedata/locales/ with
    `n_files` locale sources, the `i18n` they copy, an `iso14651_t1` with
    rules for a fixture's LC_COLLATE to copy (a copy of a file without
    LC_COLLATE stores no order, which the reader reports as unresolved),
    and a SUPPORTED, committed and tagged `t1`. With `rename`, a second commit `t2` renames
    the block-less file `x` to `y` AND gives it an LC_COLLATE block -- the
    case no upstream pair has produced.

    `t1` sits on the real tag `base` (_harness.glibc_shaped), so that step
    2 can place it before or after glibc's lr_getc fix and read its
    keyword tables; base=None gives a repository of its own, which git
    cannot place, for the tests that refuse before anything is read and
    for the one that is about that refusal.

    `dates` is (t1, t2) as git date strings, for the pair-order tests: no
    pinned tag pair is reversed, so the reversed pair has to be fabricated.
    """
    loc = os.path.join(root, 'localedata', 'locales')
    os.makedirs(loc)
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    body = ''.join(f'% filler line {i}\n' for i in range(40))
    for i in range(n_files):
        with open(os.path.join(loc, f'loc_{i:03d}'), 'w') as fh:
            fh.write(f'comment_char %\nescape_char /\n{body}LC_CTYPE\n'
                     f'copy "i18n"\nEND LC_CTYPE\n')
    with open(os.path.join(loc, 'x'), 'w') as fh:
        fh.write(f'comment_char %\nescape_char /\n{body}LC_CTYPE\n'
                 f'copy "i18n"\nEND LC_CTYPE\n')
    with open(os.path.join(loc, 'i18n'), 'w') as fh:
        fh.write('comment_char %\nescape_char /\nLC_CTYPE\nupper <U0041>\n'
                 'END LC_CTYPE\n')
    with open(os.path.join(loc, 'iso14651_t1'), 'w') as fh:
        fh.write(_rules('<U0041>'))
    with open(os.path.join(root, 'localedata', 'SUPPORTED'), 'w') as fh:
        fh.write('x.UTF-8/UTF-8 \\\ny.UTF-8/UTF-8 \\\n')
    at = lambda when: (env if when is None
                       else dict(env, GIT_AUTHOR_DATE=when,
                                 GIT_COMMITTER_DATE=when))
    env1 = at(dates[0] if dates else None)
    env2 = at(dates[1] if dates else None)
    if base is None:
        git(root, 'init', '-q', env=env1)
    else:
        glibc_shaped(root, base)
    # See make_release_line_repo: the identity belongs to the fixture, so that
    # a test adding a commit to it does not depend on the machine's config.
    git(root, 'config', 'user.name', 't', env=env1)
    git(root, 'config', 'user.email', 't@t', env=env1)
    git(root, 'add', '.', env=env1)
    git(root, 'commit', '-q', '-m', 't1', env=env1)
    git(root, 'tag', 't1', env=env1)
    if rename:
        git(root, 'mv', os.path.join(loc, 'x'), os.path.join(loc, 'y'), env=env2)
        with open(os.path.join(loc, 'y'), 'a') as fh:
            fh.write('LC_COLLATE\norder_start forward\n<U0041>\norder_end\n'
                     'END LC_COLLATE\n')
        git(root, 'add', '.', env=env2)
        git(root, 'commit', '-q', '-m', 't2', env=env2)
        git(root, 'tag', 't2', env=env2)
    return root


def run_script(script, *args, env_extra=None):
    """A step as a subprocess, uncached, optionally with extra environment."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    cmd = ([sys.executable, os.path.join(SCRIPTS_DIR, script)]
           if script.endswith('.py') else [os.path.join(SCRIPTS_DIR, script)])
    p = subprocess.run(cmd + list(args), cwd=SCRIPTS_DIR, env=env,
                       capture_output=True)
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


def add_unrelated_commit(repo, when, name='unrelated', parent=None):
    """An orphan root committed at `when`: a commit that is an ancestor of
    nothing and a descendant of nothing, with no glibc tag reachable from it,
    so neither question pair_order asks can order it. No pinned tag pair has
    that shape.

    `--orphan` keeps the index, so the commit still carries whatever corpus
    the repository had; the marker file is only there to make it non-empty,
    and it is a new path so that this works in either fixture.

    With `parent`, a glibc tag, the commit is the current tree on top of
    that tag instead: still an ancestor and a descendant of none of the
    fixture's commits, and both describe as that one release, so pair_order
    cannot order it either -- but git can place it before or after glibc's
    lr_getc fix, which step 2 needs before it reads anything."""
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
               GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    if parent is not None:
        sha = subprocess.run(
            ['git', 'commit-tree', 'HEAD^{tree}', '-p', f'{parent}^{{commit}}',
             '-m', name], cwd=repo, env=env, check=True,
            capture_output=True, text=True).stdout.strip()
        git(repo, 'tag', name, sha, env=env)
        return name
    git(repo, 'checkout', '-q', '--orphan', name, env=env)
    with open(os.path.join(repo, f'{name}-marker'), 'w') as fh:
        fh.write('a commit with no history behind it\n')
    git(repo, 'add', '.', env=env)
    git(repo, 'commit', '-q', '-m', name, env=env)
    git(repo, 'tag', name, env=env)
    return name


def make_release_line_repo(root, dates=None):
    """A repository shaped like glibc's branches, which is what breaks the
    obvious answers.

        A --- B(glibc-2.28) --- S(glibc-2.28.9000) --- C(glibc-2.34)  <- master
                     \\
                      D(backport)                    <- release/2.28/master

    S is the "open master for the next release" snapshot tag, one commit after
    the release, which is what `describe` returns for every master commit
    between two releases.

    D is committed AFTER C by default: that is the real shape --
    `origin/release/2.28/master` carries commits dated years after
    `glibc-2.34` -- and it is why a commit date cannot decide the order. D is a
    2.28 and C a 2.34, so only the release behind each one puts them in
    order.
    """
    dates = dates or ('2018-08-01T00:00:00+0000', '2021-08-02T00:00:00+0000',
                      '2025-12-18T00:00:00+0000')
    def at(when):
        return dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                    GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
                    GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    os.makedirs(root, exist_ok=True)
    def commit(msg, when):
        with open(os.path.join(root, 'f'), 'a') as fh:
            fh.write(msg + '\n')
        git(root, 'add', '.', env=at(when))
        git(root, 'commit', '-q', '-m', msg, env=at(when))
    git(root, 'init', '-q', env=at(dates[0]))
    # Written into THIS repository's config, not taken from the machine's: the
    # tests that add a commit to this fixture call git without an environment,
    # and a runner with no global user.name -- CI -- then fails the commit with
    # "unable to auto-detect email address", exit 128. Measured there, after a
    # green run on a laptop that happened to have one.
    git(root, 'config', 'user.name', 't', env=at(dates[0]))
    git(root, 'config', 'user.email', 't@t', env=at(dates[0]))
    commit('base', dates[0])
    commit('two-twenty-eight', dates[0])
    git(root, 'tag', 'glibc-2.28', env=at(dates[0]))
    commit('open master for the next release', dates[0])
    git(root, 'tag', 'glibc-2.28.9000', env=at(dates[0]))
    commit('two-thirty-four', dates[1])
    git(root, 'tag', 'glibc-2.34', env=at(dates[1]))
    git(root, 'checkout', '-q', '-b', 'release/2.28/master', 'glibc-2.28',
        env=at(dates[2]))
    commit('a backport, four years later', dates[2])
    git(root, 'tag', 'backport', env=at(dates[2]))
    git(root, 'checkout', '-q', 'glibc-2.34', env=at(dates[2]))
    return root


class PairOrderIsMeasuredNotAssumed(unittest.TestCase):
    """"Nothing stopped a reversed pair": `./audit.sh glibc-2.34 glibc-2.28`
    ran to the end at exit 0 with a plausible summary -- step 2 reporting the
    same two files that touched LC_COLLATE and step 5 a hunk count, no `!!`
    anywhere -- while step 4 scanned the older tag and step 2 called a locale
    deleted in the real upgrade a harmless addition.

    Fabricated repositories, because the shapes that matter are not in the
    pinned tags: no pair of them is reversed, none is a release branch whose
    commits are dated after the next release, and a git probe that fails to
    answer exists in no repository at all.

    The first version of this guard compared commit DATES first. These tests
    are written against the shapes that broke it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-order-repo-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def linear(self, dates=None):
        """t1 then t2 on one line of history, tagged."""
        return make_glibc_shaped_repo(self.tmp, n_files=3, rename=True,
                                      dates=dates, base=None)

    def test_ancestry_settles_the_ordinary_pair(self):
        repo = self.linear()
        self.assertEqual(g.pair_order(repo, 't1', 't2')[0], 'forward')
        status, detail = g.pair_order(repo, 't2', 't1')
        self.assertEqual(status, 'reversed', detail)
        self.assertIn('is an ancestor of', detail)

    def test_a_commit_date_does_not_decide_anything(self):
        """The descendant committed with the EARLIER date. Ancestry says
        forward and that is the answer: history, not the clock."""
        repo = self.linear(dates=('2021-01-01T00:00:00+0000',
                                  '2020-01-01T00:00:00+0000'))
        self.assertEqual(g.pair_order(repo, 't1', 't2')[0], 'forward')
        self.assertEqual(g.pair_order(repo, 't2', 't1')[0], 'reversed')

    def test_one_commit_spelt_two_ways_is_the_same_pair(self):
        """The 1.12 defect at its root: `[ "$OLD" = "$NEW" ]` compared TEXT,
        so a tag and the sha it resolves to -- the sha the run's own
        provenance line prints -- read as two different tags."""
        repo = self.linear()
        sha = subprocess.run(['git', '-C', repo, 'rev-parse', 't2^{commit}'],
                             capture_output=True).stdout.decode().strip()
        status, detail = g.pair_order(repo, 't2', sha)
        self.assertEqual(status, 'same', detail)
        self.assertEqual(g.pair_order(repo, sha, 't2')[0], 'same')

    def test_a_release_branch_commit_is_ordered_by_what_it_descends_from(self):
        """The defect the first version of this guard shipped with, on the
        shape that produced it. `origin/release/2.28/master` is dated after
        `glibc-2.34` and is an ancestor of nothing on master, so "the newer
        commit date" and "is it an ancestor" BOTH answered "not reversed" --
        and `audit.sh glibc-2.34 <a 2.28 backport>` ran the whole audit
        backwards at exit 0. What orders them is the release behind each
        one."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'rel'))
        status, detail = g.pair_order(repo, 'glibc-2.34', 'backport')
        self.assertEqual(status, 'reversed', detail)
        self.assertIn('the newest glibc tag behind backport is glibc-2.28',
                      detail)
        # And the correct order is not refused: the first version called this
        # reversed too, on the same evidence.
        self.assertEqual(g.pair_order(repo, 'backport', 'glibc-2.34')[0],
                         'forward')
        # A tag against a later commit on its own release branch is forward,
        # and ancestry alone settles that one.
        self.assertEqual(g.pair_order(repo, 'glibc-2.28', 'backport')[0],
                         'forward')

    def test_release_lineages_are_compared_as_numbers(self):
        """`glibc-2.4` is a much OLDER release than `glibc-2.34`, and every
        string comparison gets that backwards -- `'glibc-2.4' > 'glibc-2.34'`
        is true in Python, in sort(1) and in test(1)."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'nums'))
        base = subprocess.run(['git', '-C', repo, 'rev-parse',
                               'glibc-2.28~1'],
                              capture_output=True).stdout.decode().strip()
        git(repo, 'checkout', '-q', '-b', 'old-line', base)
        with open(os.path.join(repo, 'h'), 'w') as fh:
            fh.write('an ancient release line\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', 'two-four')
        git(repo, 'tag', 'glibc-2.4')

        self.assertGreater('glibc-2.4', 'glibc-2.34',
                           'the string comparison this replaces')
        self.assertEqual(g.nearest_glibc_tag(repo, 'glibc-2.4')[1], (2, 4))
        status, detail = g.pair_order(repo, 'glibc-2.34', 'glibc-2.4')
        self.assertEqual(status, 'reversed', detail)
        self.assertIn('the newest glibc tag behind glibc-2.4 is glibc-2.4 '
                      '(release 2.4)', detail)
        self.assertEqual(g.pair_order(repo, 'glibc-2.4', 'glibc-2.34')[0],
                         'forward')

    def test_only_the_release_counts_not_what_follows_it(self):
        """A tag's third component cannot order two lines off one release, and
        both kinds of third component were measured getting it wrong.

        A development snapshot: `glibc-2.28.9000` is the tag one commit after
        `glibc-2.28` that opens master for 2.29, and `describe` returns it for
        every master commit up to that release. Read as a version of its own it
        ranked a master commit above a 2.28 backport branch, and
        `origin/release/2.28/master -> glibc-2.28.9000` answered forward --
        step 2 reading that branch's own commits backwards without a word.

        A point release, the same shape from the other side: master after 2.12
        describes as `glibc-2.12`, while `release/2.12/master`'s tip describes
        as `glibc-2.12.2`, so the branch outranked master and
        `glibc-2.13~20 -> origin/release/2.12/master` answered forward while
        the reverse was refused. Both measured on the pinned clone.

        The release is the first two components. What comes after says where
        inside or after that release a commit sits, which ancestry has already
        settled for any pair on one line."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'snap'))
        git(repo, 'tag', 'glibc-2.28.2', 'backport')
        for rev in ('glibc-2.28.9000', 'glibc-2.28.2', 'backport'):
            with self.subTest(rev=rev):
                self.assertEqual(g.nearest_glibc_tag(repo, rev)[1], (2, 28))
        for old, new in (('backport', 'glibc-2.28.9000'),
                         ('glibc-2.28.9000', 'backport')):
            with self.subTest(pair=f'{old}..{new}'):
                status, detail = g.pair_order(repo, old, new)
                self.assertEqual(status, 'undetermined', detail)
                self.assertIn('(release 2.28)', detail)
        # Ancestry still ranks the snapshot against the releases around it,
        # and it is asked before any of this.
        self.assertEqual(g.pair_order(repo, 'glibc-2.28.9000',
                                      'glibc-2.34')[0], 'forward')

    def test_a_tag_name_it_cannot_read_is_not_read_as_a_release(self):
        """`describe` answered, and the answer is a name this tool does not
        parse. That is a different fact from "no glibc tag is reachable", and
        the detail line has to say which: over a ref whose own name is a tag,
        "nothing reachable" would be a false sentence."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'odd'))
        git(repo, 'checkout', '-q', '-b', 'odd-line', 'glibc-2.28')
        with open(os.path.join(repo, 'k'), 'w') as fh:
            fh.write('an oddly named tag\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', 'oddly tagged')
        git(repo, 'tag', 'glibc-2x-tps')
        name, release = g.nearest_glibc_tag(repo, 'glibc-2x-tps')
        self.assertEqual(name, 'glibc-2x-tps')
        self.assertIsNone(release)
        status, detail = g.pair_order(repo, 'backport', 'glibc-2x-tps')
        self.assertEqual(status, 'undetermined', detail)
        self.assertIn('does not read as a release', detail)
        # The other state's sentence, which would be false here: a tag WAS
        # found behind this ref -- it is the ref itself.
        self.assertNotIn('found no glibc tag behind glibc-2x-tps', detail)

    def test_two_branches_off_one_release_are_undetermined(self):
        """Divergent, and behind the same release tag: nothing here can order
        them, and "could not tell" is not "in order"."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'two'))
        git(repo, 'checkout', '-q', '-b', 'other', 'glibc-2.28')
        with open(os.path.join(repo, 'g'), 'w') as fh:
            fh.write('another backport\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', 'another backport')
        git(repo, 'tag', 'other-backport')
        status, detail = g.pair_order(repo, 'backport', 'other-backport')
        self.assertEqual(status, 'undetermined', detail)
        self.assertIn('neither is an ancestor of the other', detail)
        self.assertIn('behind other-backport is glibc-2.28', detail)

    def test_a_commit_with_no_release_tag_behind_it_is_undetermined(self):
        """An orphan root: an ancestor of nothing, a descendant of nothing,
        and no glibc tag reachable from it. `describe` fails, and that failure
        must not become "in order".

        On a repository that HAS release tags, so that the side with none is
        this commit and not the corpus -- in a repository where no tag matches
        the glob at all, git answers the same way for every ref and the test
        would pass without discriminating anything. The other side is named in
        the detail, which is what shows it did."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'noname'))
        add_unrelated_commit(repo, '2020-01-01T00:00:00+0000')
        status, detail = g.pair_order(repo, 'glibc-2.34', 'unrelated')
        self.assertEqual(status, 'undetermined', detail)
        self.assertIn('git describe found no glibc tag behind unrelated',
                      detail)
        self.assertIn('behind glibc-2.34 is glibc-2.34 (release 2.34)',
                      detail)

    def test_a_failed_ancestry_probe_is_not_read_as_in_order(self):
        """The reassuring half is "not reversed", and `--is-ancestor` exits 1
        for "no" and 128 for "I could not answer". Injected, because no
        repository makes git fail here: the probe is the one call in this
        helper's two allow_fail calls whose failure ABORTS -- the other one,
        in nearest_glibc_tag, leaves the pair undetermined instead -- and
        allow_fail is what turns a failure into a clean verdict everywhere
        else in this tool."""
        repo = self.linear()
        rc, out = in_subprocess(
            "repo = %r\n"
            "real = g.run_git\n"
            "class R:\n"
            "    returncode = 128\n"
            "    stdout = b''\n"
            "    stderr = b'fatal: bad object'\n"
            "def fake(args, repo, allow_fail=False):\n"
            "    if args[0] == 'merge-base':\n"
            "        return R()\n"
            "    return real(args, repo, allow_fail)\n"
            "g.run_git = fake\n"
            "print(g.pair_order(repo, 't1', 't2'))" % repo)
        self.assertNotEqual(rc, 0, 'a failed ancestry probe was swallowed')
        self.assertNotIn("'forward'", out)
        self.assertIn('not an answer', out)

    def test_a_failed_describe_leaves_the_pair_undetermined(self):
        """The other probe. `describe` failing means "I could not find the
        lineage", which is not "the pair is in order": injected on the
        release-branch pair, whose whole answer comes from `describe`."""
        repo = make_release_line_repo(os.path.join(self.tmp, 'nodesc'))
        rc, out = in_subprocess(
            "repo = %r\n"
            "real = g.run_git\n"
            "class R:\n"
            "    returncode = 128\n"
            "    stdout = b''\n"
            "    stderr = b'fatal: No names found, cannot describe anything.'\n"
            "def fake(args, repo, allow_fail=False):\n"
            "    if args[0] == 'describe':\n"
            "        return R()\n"
            "    return real(args, repo, allow_fail)\n"
            "g.run_git = fake\n"
            "print(g.pair_order(repo, 'glibc-2.34', 'backport')[0])" % repo)
        self.assertEqual(rc, 0, out)
        self.assertIn('undetermined', out)
        self.assertNotIn('forward', out)


@needs_clone
class TheRealReleaseBranchIsOrderedCorrectly(unittest.TestCase):
    """The fabricated shapes above, against the clone this tool actually
    reads. `origin/release/2.28/master` is where a RHEL8 build's upstream
    commits live, and a bare sha is an input this tool invites: the provenance
    line prints one for every tag it reads."""

    REF = 'origin/release/2.28/master'
    SNAPSHOT = 'glibc-2.28.9000'
    # The point-release side of the same shape, out of the audited range: this
    # branch's tip describes as glibc-2.12.2 and master after 2.12 as
    # glibc-2.12, which is what used to rank the branch above master.
    POINT_REF = 'origin/release/2.12/master'
    POINT_MASTER = 'glibc-2.13~20'

    def setUp(self):
        for ref in (self.REF, self.SNAPSHOT, self.POINT_REF,
                    self.POINT_MASTER):
            p = subprocess.run(['git', '-C', GLIBC_CLONE, 'rev-parse',
                                '--verify', '--quiet', ref + '^{commit}'],
                               capture_output=True)
            if p.returncode != 0:
                self.skipTest(f'{ref} is not in the clone; both come with a '
                              f'plain `git clone` of the mirror and '
                              f'`fetch --tags`, which is what '
                              f'./audit.sh and CI both do')

    def test_a_backport_commit_is_older_than_the_next_release(self):
        status, detail = g.pair_order(GLIBC_CLONE, MID, self.REF)
        self.assertEqual(status, 'reversed', detail)
        self.assertIn(f'the newest glibc tag behind {self.REF} is glibc-2.28',
                      detail)

    def test_and_the_correct_order_is_not_refused(self):
        self.assertEqual(g.pair_order(GLIBC_CLONE, self.REF, MID)[0],
                         'forward')

    def test_a_master_line_and_a_backport_branch_are_not_ordered(self):
        """The real pairs the fabricated release-identity test stands for: a
        backport branch and a master commit off the same release are two lines,
        and nothing here can say which is upstream of the other. Both the
        snapshot spelling (2.28) and the point-release spelling (2.12) are
        measured, because each of them ordered this pair once."""
        for old, new in ((self.REF, self.SNAPSHOT), (self.SNAPSHOT, self.REF),
                         (self.POINT_MASTER, self.POINT_REF),
                         (self.POINT_REF, self.POINT_MASTER)):
            with self.subTest(pair=f'{old}..{new}'):
                status, detail = g.pair_order(GLIBC_CLONE, old, new)
                self.assertEqual(status, 'undetermined', detail)

    def test_the_audited_pairs_are_still_forward(self):
        """The control that matters most: the two audited pairs must be
        untouched by any of this. The floor pair is covered end to end by
        test_known_answers, which now runs every step through this guard."""
        for old, new in ((OLD, MID), (MID, NEW)):
            with self.subTest(pair=f'{old}..{new}'):
                self.assertEqual(g.pair_order(GLIBC_CLONE, old, new)[0],
                                 'forward')


@needs_clone
class TheStepsRefuseAReversedPair(unittest.TestCase):
    """The call site, not only the classifier: what each state DOES is a
    different mistake from what each state IS. Step 2 on the fabricated
    repository, at the corpus floor so that only the order guard can fire --
    a refusal that came from the floor instead would pass this test while
    guarding nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-order-step-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-order-out-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(
            self.tmp, n_files=g.MIN_LOCALE_FILES, rename=True,
            dates=('2020-01-01T00:00:00+0000', '2021-01-01T00:00:00+0000'))

    def step2(self, *args):
        return run_script('filter_lc_collate_changes.py', *args,
                          '--repo', self.repo,
                          env_extra={'PG_GLIBC_AUDIT_OUT': self.out_dir})

    def test_step_2_refuses_the_reversed_pair_and_reports_nothing(self):
        rc, out = self.step2('t2', 't1')
        self.assertEqual(rc, 2, out)
        self.assertIn('REVERSED', out)
        self.assertIn('Swap the arguments', flat(out))
        self.assertNotIn('below the floor', out)
        self.assertNotIn('The sort rules localedef builds', out)
        self.assertNotIn('Listed for Reindex', out)

    def test_the_forward_control_still_runs(self):
        """x, renamed y at t2: compared by name, y has no file at t1 and
        x none at t2."""
        rc, out = self.step2('t1', 't2')
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(r.added, {'y': 'y.utf8; renamed from x'})
        self.assertEqual(r.removed, {'x': '(renamed to y)'})

    def test_the_same_commit_twice_says_nothing_was_compared(self):
        """A hand-run step used to end in its clean sentence, rc 0 and no
        `!!`, for a pair that is one commit compared with itself -- a clean
        verdict over a comparison that never happened. The wrapper said so and
        the steps did not; the notice lives in the shared helper now."""
        rc, out = self.step2('t1', 't1')
        self.assertEqual(rc, 0, out)
        self.assertIn('!! ', out)
        self.assertIn('t1 and t1 are the same commit', flat(out))
        self.assertIn('Step 2 compares nothing: it would compare upstream '
                      'source against itself. A run on the tags says '
                      'nothing about an intra-major upgrade', flat(out))
        self.assertEqual(Step2Report(out).nothing,
                         'the two tags are one commit.')
        # The notice names no step deleted in branch 9 (it named steps 4
        # and 5 until step 9.3).
        self.assertEqual(deleted_steps_named(out), [])

    def test_an_undetermined_direction_is_said_out_loud_and_runs(self):
        """The fourth state at the call site. Not an error -- nothing below is
        wrong on its account -- but silence here reads as "the direction was
        checked", and this is the one branch where the tool cannot tell which
        way round the pair is. The shim in test_wrapper stands in for the
        wrapper's summary block and cannot reach this warning: it replaces the
        very subcommand that prints it."""
        add_unrelated_commit(self.repo, '2020-01-01T00:00:00+0000',
                             parent=MID)
        rc, out = self.step2('t1', 'unrelated')
        self.assertEqual(rc, 0, out)
        self.assertIn('!! The DIRECTION of this pair could not be', out)
        self.assertIn('neither is an ancestor of the other', flat(out))

    def test_allow_reverse_runs_but_says_the_pair_is_reversed(self):
        """A deliberate reversed run is what the suite itself does on
        2.39 -> 2.34. It must not look like an audit of an upgrade."""
        rc, out = self.step2('t2', 't1', '--allow-reverse')
        self.assertEqual(rc, 0, out)
        self.assertIn('!! REVERSED PAIR', out)
        self.assertIn('Not an audit of an upgrade', flat(out))


class AMissingRefIsNamed(unittest.TestCase):
    """check_refs: the refs are asked of git in one `cat-file --batch-check`,
    and when one does not resolve, tags are fetched once and the refs are
    asked again in one more; a ref still unresolved is named. A clone of a
    local repository, so the fetch needs no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-refs-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = os.path.join(self.tmp, 'origin')
        self.clone = os.path.join(self.tmp, 'clone')
        os.makedirs(self.origin)
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        git(self.origin, 'init', '-q', env=env)
        git(self.origin, 'commit', '-q', '--allow-empty', '-m', 't1', env=env)
        git(self.origin, 'tag', 't1', env=env)
        git(self.tmp, 'clone', '-q', self.origin, self.clone, env=env)
        self.env = env

    def sha(self, repo, ref):
        return subprocess.run(['git', 'rev-parse', f'{ref}^{{commit}}'],
                              cwd=repo, check=True, capture_output=True
                              ).stdout.decode().strip()

    def test_a_ref_nothing_resolves_is_named_after_the_fetch(self):
        rc, out = in_subprocess(
            "g.check_refs(%r, 't1', 'no-such-ref')" % self.clone)
        self.assertNotEqual(rc, 0, out)
        self.assertIn('no-such-ref not present', out)
        self.assertIn('unknown git ref(s) after fetching tags: no-such-ref',
                      out)

    def test_a_tag_the_fetch_brings_is_resolved(self):
        git(self.origin, 'commit', '-q', '--allow-empty', '-m', 'later',
            env=self.env)
        git(self.origin, 'tag', 'later', env=self.env)
        self.assertEqual(g.check_refs(self.clone, 't1', 'later'),
                         {'t1': self.sha(self.origin, 't1'),
                          'later': self.sha(self.origin, 'later')})

    def test_a_ref_with_a_newline_is_missing_not_two_requests(self):
        """Sent as is, `t1\nt1` would be two request lines for one ref, and
        the answers would no longer line up with the refs."""
        rc, out = in_subprocess(
            "g.check_refs(%r, 't1', 't1\\nt1')" % self.clone)
        self.assertNotEqual(rc, 0, out)
        self.assertIn('unknown git ref(s) after fetching tags: t1\nt1', out)

    def test_an_answer_that_cannot_be_matched_to_its_ref_dies(self):
        """A missing ref is fetched for and named, so an answer read wrong
        must not quietly become one: one line short, or a line of no known
        shape, dies."""
        for answer, said in ((b'missing-the-second-line\n', 'answered 1 '),
                             (b'x\nnot an answer\n', 'unreadable')):
            with self.subTest(answer=answer):
                rc, out = in_subprocess(
                    "import subprocess\n"
                    "class R:\n"
                    "    returncode = 0\n"
                    "    stdout = %r\n"
                    "    stderr = b''\n"
                    "g.subprocess.run = lambda *a, **k: R()\n"
                    "g.resolve_commits(%r, ['t1', 't2'])" % (answer, self.clone))
                self.assertNotEqual(rc, 0, out)
                self.assertIn(said, out)


@needs_clone
class TheSidesRefuseAReversedPairBeforeReportingAnything(unittest.TestCase):
    """The sides ('What was compared', glibc_locale_data.py sides) are the
    first entry point that takes a pair with a clone to ask: they do the
    cloning. Unguarded, a hand-run step 1 on glibc-2.34 glibc-2.28 printed
    findings computed at the tag that is actually the older one -- all of
    it about the other direction, and none of it saying so. Under audit.sh
    this is also what keeps a doomed run from printing anything before the
    refusal."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-sides-order-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.env = {'PG_GLIBC_AUDIT_OUT': self.out_dir}

    def test_it_refuses_and_reports_nothing(self):
        rc, out = run_script('glibc_locale_data.py', 'sides', MID, OLD,
                             env_extra=self.env)
        self.assertEqual(rc, 2, out)
        self.assertIn('This pair is REVERSED', flat(out))
        self.assertNotIn('provenance', out)
        self.assertNotIn('!!', out)

    def test_the_forward_control_is_untouched(self):
        """The guard refuses a direction, and says nothing otherwise: the
        `!!` of a forward pair of tags are what a tag does not hold, which
        run by hand asks for the builds (glibc_locale_data.tag_sides_text),
        and the code no step compares (CODE_NOT_COMPARED)."""
        rc, out = run_script('glibc_locale_data.py', 'sides', OLD, MID,
                             env_extra=self.env)
        self.assertEqual(rc, 0, out)
        self.assertIn('Tag provenance', out)
        self.assertEqual([ln for ln in out.splitlines() if '!!' in ln], [
            '!! Not checked for glibc-2.28 and glibc-2.34: they are upstream '
            'tags, which',
            '!! Not compared on any route: the code that compiles and '
            'compares the rules'])
        self.assertIn('name each side as its build instead', flat(out))
        self.assertNotIn('REVERSED', out)

    def test_a_third_argument_is_refused(self):
        """Two sides and nothing else; --allow-reverse, which step 1
        took, is refused like any other word."""
        for extra in ('--nonsense', '--allow-reverse'):
            with self.subTest(extra=extra):
                rc, out = run_script('glibc_locale_data.py', 'sides', MID,
                                     OLD, extra, env_extra=self.env)
                self.assertEqual(rc, 2, out)
                self.assertEqual(out, 'error: usage: glibc_locale_data.py '
                                      'sides <old> <new>\n')


@needs_clone
class TagModeCorpusFloor(unittest.TestCase):
    """"No corpus floor in tag mode": a tag whose tree holds a handful of
    locale files -- or none -- made step 2 print "0 changed", step 4 print
    "No locale uses ellipsis ranges here", and both exit 0. The node modes had
    refused such a directory from the start. The repository is fabricated,
    because no pinned tag is small enough to show it, and sits on a real tag
    (glibc_shaped), so the clone is needed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-tiny-repo-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_step_2_refuses_a_tag_below_the_floor(self):
        repo = make_glibc_shaped_repo(self.tmp, n_files=3, rename=True,
                                      base=None)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo)
        self.assertEqual(rc, 2, out)
        self.assertIn('below the floor', out)
        self.assertNotIn('The sort rules localedef builds', out)

    def test_the_sides_refuse_too(self):
        """The sides hold their tags to the floor in compared_sides, after
        the provenance; the corpus subcommand does the same for any tags it
        is given."""
        repo = make_glibc_shaped_repo(self.tmp, n_files=3, rename=True,
                                      base=None)
        here = os.path.join(self.tmp, 'here')
        os.mkdir(here)
        os.symlink(repo, os.path.join(here, 'glibc'))
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as cm:
            g.compared_sides(here, 't1', 't2')
        self.assertEqual(cm.exception.code, 2, err.getvalue())
        self.assertIn('below the floor', err.getvalue())
        self.assertIn('Tag provenance', out.getvalue())
        p = subprocess.run([sys.executable,
                            os.path.join(SCRIPTS_DIR, 'glibc_locale_data.py'),
                            'corpus', 't1'],
                           cwd=repo, capture_output=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(b'below the floor', p.stderr)

    def test_the_sides_refuse_a_new_tag_below_the_floor(self):
        """The other side: t1 holds a corpus above the floor, and t3, after it,
        loses ten files. Every other case of the sides has the short tag
        first."""
        root = os.path.join(self.tmp, 'new-short')
        os.mkdir(root)
        make_glibc_shaped_repo(root, n_files=g.MIN_LOCALE_FILES, base=None)
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        loc = os.path.join(root, 'localedata', 'locales')
        git(root, 'rm', '-q', *[os.path.join(loc, 'loc_%03d' % i)
                                for i in range(10)], env=env)
        git(root, 'commit', '-q', '-m', 't3', env=env)
        git(root, 'tag', 't3', env=env)
        here = os.path.join(self.tmp, 'here-new')
        os.mkdir(here)
        os.symlink(root, os.path.join(here, 'glibc'))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            g.compared_sides(here, 't1', 't3')
        self.assertEqual(cm.exception.code, 2, err.getvalue())
        self.assertIn('at t3, below the floor', flat(err.getvalue()))

    def test_a_corpus_at_the_floor_passes(self):
        """The control: the guard refuses size, not fabricated repositories."""
        repo = make_glibc_shaped_repo(self.tmp, n_files=g.MIN_LOCALE_FILES,
                                      rename=True)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo)
        self.assertEqual(rc, 0, out)


@needs_clone
class APureRenameGoesToRemoved(unittest.TestCase):
    """A file git reports as renamed with no change: step 2 reads git's
    `--find-renames` list for Removed, and a pure rename is in it with its
    new name, never a deletion and an unrelated addition. No audited pair
    has a pure rename (the one rename, over 2.34..2.39, changes the file
    too)."""

    def test_a_pure_rename_is_listed_with_its_new_name(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-pure-rename-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-pure-rename-out-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        repo = make_glibc_shaped_repo(tmp, n_files=g.MIN_LOCALE_FILES)
        loc = os.path.join(repo, 'localedata', 'locales')
        git(repo, 'mv', os.path.join(loc, 'x'), os.path.join(loc, 'y'))
        git(repo, 'commit', '-q', '-m', 't2')
        git(repo, 'tag', 't2')
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': out_dir})
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(r.removed, {'x': '(renamed to y)'})
        self.assertEqual(r.added, {'y': 'y.utf8; renamed from x'})
        self.assertEqual(read_list(os.path.join(
            out_dir, 'step2_removed_locales.t1..t2.txt')),
            ['# locale files at t1 and not at t2', 'x (renamed to y)'])


@needs_clone
class ALineAboveLcCollateThatStopsGlibc(unittest.TestCase):
    """Step 2 end to end on a fabricated repository, for what no locale
    file at glibc-2.12..2.42 does: a line above LC_COLLATE that stops glibc
    reading it at one tag (backlog 13.18), a section read in place of
    another, also in a renamed file or under a renamed copier, a file the
    two tags' localedef read differently.
    setUpClass builds the history, the commits each test compares and the
    files each one adds. It sits on glibc's own tag, before the lr_getc fix:
    step 2 places each tag and reads every file with glibc_reader."""

    BODY = ('comment_char %\nescape_char /\nLC_CTYPE\ncopy "i18n"\n'
            'END LC_CTYPE{end}\nLC_COLLATE\ncopy "iso14651_t1"\n'
            'END LC_COLLATE\n')

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-unread-repo-')
        cls.repo = make_glibc_shaped_repo(cls.tmp,
                                          n_files=g.MIN_LOCALE_FILES)
        cls.sha = {}
        for tag, end in (('base', ''), ('ctl', ' % a note'),
                         ('cont', ' % a note /')):
            cls.commit(tag, end)
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('sub', ' % a note\n% \x1a')
        cls.commit('sub2', ' % another note')
        git(cls.repo, 'checkout', '-q', 'sub')
        cls.commit('same', ' % a note\n% \x1a', other=True)
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('bare', ' % a note\n\x1a')
        cls.commit('bare2', ' % a note\n\x1a', other=True)
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('miss', ' % a note\nLC_CTYPE\ncopy "zz_none"\n'
                   'END LC_CTYPE\n\x1a')
        cls.commit('miss2', ' % a note\nLC_CTYPE\ncopy "zz_none"\n'
                   'END LC_CTYPE\n\x1a', other=True)
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('ifd', ' % a note', extra={'j': cls.IFDEF})
        cls.commit('ifd2', ' % a note', other=True)
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('cpd', ' % a note', extra=cls.COPIED)
        cls.commit('cpd2', ' % a note', other=True)
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('utf', ' % a note', extra={'u': cls.UTF8,
                                              'w': cls.UTF8_TIME % b'1'})
        cls.commit('utf2', ' % a note', extra={'w': cls.UTF8_TIME % b'2'})
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('mm', ' % a note', extra={'m': cls.SWALLOW % (b'/', b'')})
        cls.commit('mm2', ' % a note', extra={'m': cls.SWALLOW % (b'!', b' !')})
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('vv', ' % a note', extra={'v': cls.UTF8})
        cls.commit('vv2', ' % a note', extra={'v': cls.UTF8.replace(
            b'<U0062>\norder_end\nelse', b'<U0062>\n<U0063>\norder_end\nelse')})
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('sw', ' % a note', extra={'x': cls.SWAPPED % (b'', b'')})
        cls.commit('sw2', ' % a note', extra={'x': cls.SWAPPED % (
            b' % a /', cls.SWAPPED_IN)})
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('rn', ' % a note', extra={'r': cls.PAD + cls.SWAPPED % (
            b'', b'')})
        cls.commit('rn2', ' % a note', extra={'r': None, 'r2': cls.PAD
                                              + cls.SWAPPED % (b' % a /',
                                                               cls.SWAPPED_IN)})
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('cr', ' % a note', extra={
            'rf': cls.RF % b'', 'rb': cls.RB, 'rx': cls.RX})
        cls.commit('cr2', ' % a note', extra={
            'rf': cls.RF % b' # a \\', 'rb': None, 'rw': cls.RB})
        git(cls.repo, 'checkout', '-q', 'ctl')
        cls.commit('cq', ' % a note', extra={
            'rf': cls.RF % b'', 'rb': cls.RB, 'rx': cls.RX})
        cls.commit('cq2', ' % a note', extra={'rf': cls.RF % b' # a \\'})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # j: an 0xFF at the end of an ifdef name. lr_getc ends the word there
    # before 2.36, so ifdef asks for YES, which is defined, and the order is
    # a, b; from 2.36 the word holds the byte, and it is b, a (case b25 of
    # reader_oracle). Read by both tags' localedef, the same file gives as
    # many statements either way.
    IFDEF = (b'comment_char %\nescape_char /\nLC_CTYPE\ncopy "i18n"\n'
             b'END LC_CTYPE\nLC_COLLATE\ndefine YES\nifdef YES\xff\n'
             b'order_start forward\n<U0061>\n<U0062>\norder_end\nelse\n'
             b'order_start forward\n<U0062>\n<U0061>\norder_end\nendif\n'
             b'END LC_COLLATE\n')

    # zz_F's ifdef asks for a name zz_F does not define, either way: its own
    # run reads the same with both tags' localedef. zz_L defines YES and
    # then copies zz_F, and glibc keeps one list of defined names for the
    # whole run (ld-collate.c:273@2.34; cases c31 and b25 of
    # reader_oracle): only zz_L's run picks a branch by the byte.
    # False-negative-reviewer, round 2 of 15.2 B3.
    COPIED = {
        'zz_L': (b'comment_char %\nescape_char /\nLC_COLLATE\ndefine YES\n'
                 b'copy "zz_F"\nEND LC_COLLATE\n'),
        'zz_F': (b'comment_char %\nescape_char /\nLC_COLLATE\nifdef YES\xff\n'
                 b'order_start forward\n<U0061>\n<U0062>\norder_end\nelse\n'
                 b'order_start forward\n<U0062>\n<U0061>\norder_end\nendif\n'
                 b'END LC_COLLATE\n'),
    }

    # u and w: a define and an ifdef of a name that opens with UTF-8, which
    # glibc from 2.36 reads as a character (linereader.c:332-341@2.39) and
    # before it as a name (case b26 of reader_oracle). u does not change; w
    # changes in LC_TIME only. Round 2 of the 15.2 B3 review.
    UTF8 = (b'comment_char %\nescape_char /\nLC_COLLATE\n'
            b'define \xc3\xa9X\nifdef \xc3\xa9X\norder_start forward\n'
            b'<U0061>\n<U0062>\norder_end\nelse\norder_start forward\n'
            b'<U0062>\n<U0061>\norder_end\nendif\nEND LC_COLLATE\n')
    UTF8_TIME = UTF8.replace(b'%', b'%%') + (b'LC_TIME\nabday "%s";"2";"3";"4";'
                                            b'"5";"6";"7"\nEND LC_TIME\n')

    # m at mm2 sets the escape to `!` and ends END LC_CTYPE with it: lr_next
    # joins the LC_COLLATE line to it and glibc reads no LC_COLLATE. The
    # line search behind lc_collate_unread settles only `/` and `\` and does
    # not see it; glibc_reader does (doc-sweep of 15.2 B3).
    SWALLOW = (b'comment_char %%\nescape_char %s\nLC_CTYPE\ncopy "i18n"\n'
               b'END LC_CTYPE%s\nLC_COLLATE\ncopy "iso14651_t1"\n'
               b'END LC_COLLATE\n')

    # x at sw2 ends END LC_CTYPE with the escape, which swallows its
    # indented LC_COLLATE line, and appends a section of another order:
    # glibc reads one section on each side, and not the same one. The line
    # search does not see an indented header; round 4 of the 15.2 B3
    # review.
    SWAPPED = (b'comment_char %%\nescape_char /\n\nLC_CTYPE\ncopy "i18n"\n'
               b'END LC_CTYPE%s\n LC_COLLATE\norder_start forward\n<U0061>\n'
               b'<U0062>\norder_end\nEND LC_COLLATE\n%s')
    SWAPPED_IN = (b' LC_COLLATE\norder_start forward\n<U0062>\n<U0061>\n'
                  b'order_end\nEND LC_COLLATE\n')
    # r is x renamed to r2 at rn2: lines enough for git to see a rename.
    PAD = b''.join(b'# line %d\n' % i for i in range(30))
    # rf's own run obeys the escape_char inside its LC_CTYPE; rb copies its
    # LC_COLLATE, skips that section, and reads with the default `\`. At
    # cr2 the END line ends in `\`, which swallows the first section's
    # header for rb alone, and rb is renamed rw: one run under two names
    # (round 6 of the 15.2 B3 review).
    RF = (b'LC_CTYPE\ncopy "rx"\nescape_char /\nEND LC_CTYPE%s\n'
          b' LC_COLLATE\norder_start forward\n<U0061>\n<U0062>\norder_end\n'
          b'END LC_COLLATE\n LC_COLLATE\norder_start forward\n<U0063>\n'
          b'<U0064>\norder_end\nEND LC_COLLATE\n')
    RB = b'LC_COLLATE\ncopy "rf"\nEND LC_COLLATE\n'
    RX = b'LC_IDENTIFICATION\ntitle "t"\nEND LC_IDENTIFICATION\n'

    @classmethod
    def commit(cls, tag, end, other=False, extra=None):
        loc = os.path.join(cls.repo, 'localedata', 'locales')
        for name, data in (extra or {}).items():
            if data is None:
                os.unlink(os.path.join(loc, name))
                continue
            with open(os.path.join(loc, name), 'wb') as fh:
                fh.write(data)
        with open(os.path.join(loc, 'k'), 'w', encoding='utf-8') as fh:
            fh.write(cls.BODY.format(end=end))
        if other:
            with open(os.path.join(loc, 'loc_000'), 'a') as fh:
                fh.write('% changed\n')
        git(cls.repo, 'add', '.')
        git(cls.repo, 'commit', '-q', '-m', tag)
        git(cls.repo, 'tag', tag)
        cls.sha[tag] = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=cls.repo, check=True,
            capture_output=True, text=True).stdout.strip()

    def step2(self, old, new, fix=None):
        """(exit, Step2Report, the Reindex list written). `fix` moves
        LR_GETC_FIX to one of this history's commits, so that a tag can
        hold it."""
        out_dir = tempfile.mkdtemp(dir=self.tmp)
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import glibc_locale_data as g\n" % SCRIPTS_DIR)
        if fix:
            code += "g.LR_GETC_FIX = %r\n" % self.sha[fix]
        code += ("import filter_lc_collate_changes as f\n"
                 "sys.exit(f.main([%r, %r, '--repo', %r]))"
                 % (old, new, self.repo))
        p = subprocess.run([sys.executable, '-c', code], cwd=SCRIPTS_DIR,
                           env=dict(os.environ, PG_GLIBC_AUDIT_OUT=out_dir),
                           capture_output=True)
        out = (p.stdout + p.stderr).decode('utf-8', 'replace')
        self.assertEqual(p.returncode, 0, out)
        listed = read_list(os.path.join(
            out_dir, f'step2_changed_collate.{old}..{new}.txt'))
        r = Step2Report(out)
        self.assertEqual(r.reindex, listed)
        return r

    def test_an_end_that_swallows_lc_collate_is_listed(self):
        """k's END LC_CTYPE line ends in the escape at cont: lr_next joins
        the LC_COLLATE line to it, and glibc stores no LC_COLLATE for k on
        the new side. Not compared, and listed with a `!!`."""
        r = self.step2('ctl', 'cont')
        self.assertEqual(r.listed,
                         {'k': 'LC_COLLATE stored on the old side only'})
        self.assertEqual(r.differs, {})
        self.assertEqual(r.reindex, ['k'])

    def test_the_control_a_note_without_the_escape(self):
        r = self.step2('base', 'ctl')
        self.assertEqual((r.differs, r.listed, r.reindex), ({}, {}, []))

    def test_a_0x1a_on_a_comment_line_is_not_read_either_way(self):
        """k at sub holds a 0x1A on a comment line, above LC_COLLATE, and
        neither tag holds LR_GETC_FIX. glibc drops the rest of a comment
        line without lr_getc reading it (lr_ignore_rest), so both tags'
        localedef read k whole and alike: the same rules. Until step 9.2
        step 2 listed k here, from a search of the file's lines for the
        byte rather than from how glibc reads it."""
        for fix in (None, 'sub'):
            with self.subTest(fix=fix):
                r = self.step2('ctl', 'sub', fix=fix)
                self.assertEqual((r.differs, r.listed, r.reindex),
                                 ({}, {}, []))

    def test_each_tag_is_asked_about_its_own_localedef(self):
        """k at bare and bare2 holds a 0x1A on a line of its own above
        LC_COLLATE. With the fix at bare2, only the old tag's localedef
        stops there: k is listed, and the reason names the OLD side.
        Asked of the other tag, the reason would name the new one."""
        r = self.step2('bare', 'bare2', fix='bare2')
        self.assertEqual(r.listed, {'k': 'old side: localedef stops reading '
                                         'k at line 6, before the end of '
                                         'the file'})
        self.assertEqual(r.reindex, ['k'])

    def test_a_0x1a_counts_when_both_tags_predate_the_fix(self):
        """Neither tag holds LR_GETC_FIX: both localedef read the 0x1A on
        its own line as the end of the file, before LC_COLLATE. Both stop
        at the same place, and agreeing on a run that did not finish is
        not reading k: listed, the reason naming both sides."""
        r = self.step2('bare', 'bare2')
        self.assertEqual(sorted(r.listed), ['k'])
        self.assertTrue(r.listed['k'].startswith('both sides: '),
                        r.listed['k'])
        self.assertEqual(r.reindex, ['k'])

    def test_a_file_that_did_not_change_is_read_across_the_fix(self):
        """Review of 13.18, finding 3: k is the same bytes at both tags,
        and only the old tag's localedef stops at its 0x1A. Every locale
        is compared, changed file or not, so k is listed."""
        r = self.step2('bare', 'bare2', fix='bare2')
        self.assertEqual(sorted(r.listed), ['k'])
        self.assertEqual(r.totals[2], 0)

    def test_a_file_the_reader_cannot_settle_is_not_cleared(self):
        """A second LC_CTYPE in k copies a file there is none of, and a
        copy in LC_CTYPE is loaded where it is read: under both tags'
        Builds the run stops there, before the 0x1A, and the two runs
        agree. Agreeing on a run that did not finish is not reading the
        byte alike: listed."""
        r = self.step2('miss', 'miss2', fix='miss2')
        self.assertEqual(r.listed,
                         {'k': 'both sides: localedef cannot open zz_none'})
        self.assertEqual(r.reindex, ['k'])

    def test_a_byte_that_picks_another_branch_is_listed(self):
        """False-negative-reviewer, 15.2 B3: j did not change, and the two
        tags' localedef build opposite orders from it, out of the same
        number of statements: the runs are compared statement by
        statement, never by a count."""
        r = self.step2('ifd', 'ifd2', fix='ifd2')
        self.assertEqual(r.differs, {'j': (2, True)})
        self.assertEqual(r.reindex, ['j'])
        r = self.step2('ifd', 'ifd2', fix='ifd')
        self.assertEqual((r.differs, r.reindex), ({}, []),
                         'the control: one reader')

    def test_a_byte_only_a_copier_reads_otherwise_is_listed(self):
        """zz_L holds no such byte, and its order changes with the reader:
        it defines YES and copies zz_F, whose ifdef ends in an 0xFF, and
        glibc keeps one list of defined names for the whole run. zz_F's
        own run defines no YES and takes the same branch with either
        reader: the same rules. Every locale is compared, not only the
        files that hold the byte."""
        r = self.step2('cpd', 'cpd2', fix='cpd2')
        self.assertEqual(r.differs, {'zz_L': (2, True)})
        self.assertEqual(r.listed, {})
        self.assertEqual(r.reindex, ['zz_L'])

    def test_utf8_glibc_reads_from_2_36_is_read_across(self):
        """u did not change and w changed in LC_TIME alone; glibc before
        2.36 and after it build different orders from both: both differ,
        every rule able to change."""
        r = self.step2('utf', 'utf2', fix='utf2')
        self.assertEqual(r.differs, {'u': (2, True), 'w': (2, True)})
        self.assertEqual(r.reindex, ['u', 'w'])
        r = self.step2('utf', 'utf2', fix='utf')
        self.assertEqual((r.differs, r.reindex), ({}, []), 'the control')

    def test_a_new_side_with_no_lc_collate_is_named(self):
        """m's new side has no LC_COLLATE to glibc (the escape `!` ends its
        END LC_CTYPE line): not compared, listed, and the reason says
        which side stores it."""
        r = self.step2('mm', 'mm2')
        self.assertEqual(r.listed,
                         {'m': 'LC_COLLATE stored on the old side only'})
        self.assertEqual(r.reindex, ['m'])

    def test_one_section_read_for_another_is_listed(self):
        """x has one LC_COLLATE section to glibc on each side, and they
        are not the same lines: its rules differ, every rule able to
        change."""
        r = self.step2('sw', 'sw2')
        self.assertEqual(r.differs, {'x': (2, True)})
        self.assertEqual(r.reindex, ['x'])

    def test_a_renamed_file_is_not_compared_and_goes_to_removed(self):
        """r is renamed r2, and rb, which copies rf, is renamed rw. A
        locale is compared by its name: r and rb have no file on the new
        side and are listed under Removed, with the new name, and r2 and rw
        none on the old side. rf, whose own rules are the same, is not
        listed: until step 9.2 step 2 judged files, and listed rf for
        what its copier rb read (round 6 of the 15.2 B3 review)."""
        r = self.step2('rn', 'rn2')
        self.assertEqual(r.removed, {'r': '(renamed to r2)'})
        self.assertEqual(r.added, {'r2': '(not in SUPPORTED); renamed '
                                         'from r'})
        self.assertEqual(r.reindex, [])
        r = self.step2('cr', 'cr2')
        self.assertEqual(r.removed, {'rb': '(renamed to rw)'})
        self.assertEqual(r.added, {'rw': '(not in SUPPORTED); renamed '
                                         'from rb'})
        self.assertEqual((r.differs, r.listed, r.reindex), ({}, {}, []))

    def test_a_copier_that_reads_less_differs_and_its_source_does_not(self):
        """cr and cr2 without the rename: rf's own run obeys its escape_char
        `/` and reads both sections at cq and cq2, the same rules; rb,
        which copies rf's LC_COLLATE and reads with the default `\\`, loses
        the first section's header at cq2, where the END LC_CTYPE line
        ends in `\\`. Each locale is compared on its own run: rb differs,
        rf does not (round 6 of the 15.2 B3 review, by locale since step
        9.2)."""
        r = self.step2('cq', 'cq2')
        self.assertEqual(sorted(r.differs), ['rb'])
        self.assertEqual(r.listed, {})
        self.assertEqual(r.reindex, ['rb'])

    def test_a_change_inside_lc_collate_read_across_says_so(self):
        """v adds <U0063> to its first branch, and the two readers take
        different branches: the characters of the changed line are not all
        that moves, and the line under v says every rule can change. With
        one reader, the changed line's character alone (round 3 of the
        15.2 B3 review)."""
        r = self.step2('vv', 'vv2', fix='vv2')
        self.assertEqual(r.differs, {'v': (3, True)})
        r = self.step2('vv', 'vv2', fix='vv')
        self.assertEqual(r.differs, {'v': ([0x63], False)}, 'the control')

    def test_each_tag_is_placed_on_its_own_side_of_the_fix(self):
        """The answer lr_getc_sides hands to the Builds, in its direction:
        a tag that predates the fix is read with the old lr_getc and one
        that holds it with the new. Swapped, every test above still sees
        two different readers (false-negative-reviewer, 15.2 B3)."""
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import glibc_locale_data as g\n"
                "g.LR_GETC_FIX = %r\n"
                "import filter_lc_collate_changes as f, glibc_tree\n"
                "repo = %r\n"
                "early = (f.predates_lr_getc_fix(repo, 'sub'),\n"
                "         f.predates_lr_getc_fix(repo, 'same'))\n"
                "pre = f.lr_getc_sides(repo, early, ('sub', 'same'))\n"
                "b = glibc_tree.builds_at(repo, ['sub', 'same'], pre)\n"
                "print(pre['sub'], pre['same'], b['sub'].pre236, "
                "b['same'].pre236)\n" % (SCRIPTS_DIR, self.sha['same'],
                                          self.repo))
        p = subprocess.run([sys.executable, '-c', code], cwd=SCRIPTS_DIR,
                           capture_output=True, timeout=120)
        self.assertEqual(p.stdout.decode().split(),
                         ['True', 'False', 'True', 'False'],
                         p.stderr.decode())

    def test_a_byte_lr_getc_never_reads_is_not_listed(self):
        """The control of the one above, and a change from the byte search
        that came before the reader: on a comment line glibc drops the
        rest of the line without lr_getc reading it, so both tags read k
        alike, and the reader says so."""
        r = self.step2('sub', 'same', fix='same')
        self.assertEqual((r.differs, r.listed, r.reindex), ({}, {}, []))

    def test_the_control_both_tags_read_it_alike(self):
        """Both hold the fix: the same bytes are read the same way."""
        r = self.step2('bare', 'bare2', fix='bare')
        self.assertEqual((r.differs, r.listed, r.reindex), ({}, {}, []))

    def test_a_tag_git_cannot_place_is_refused(self):
        """Before the reader, a history that does not hold LR_GETC_FIX
        counted "cannot tell" as "predates" and went on. glibc_reader
        needs the answer to read 0x1A and 0xFF at all, and steps 1, 3 and
        4 refused such a tag already: so does step 2, before it judges
        anything."""
        tmp = tempfile.mkdtemp(dir=self.tmp)
        repo = make_glibc_shaped_repo(tmp, n_files=g.MIN_LOCALE_FILES,
                                      rename=True, base=None)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo, env_extra={
                                 'PG_GLIBC_AUDIT_OUT': tmp})
        self.assertEqual(rc, 2, out)
        self.assertIn("git cannot tell whether t1 comes before or after "
                      "glibc's lr_getc fix", flat(out))
        self.assertNotIn('The sort rules localedef builds', out)


def read_list(path):
    """Every non-blank line of a list a step wrote, its `#` header included."""
    with open(path, encoding='utf-8') as fh:
        return [ln.rstrip('\n') for ln in fh if ln.strip()]


@needs_clone
class Step2ListsWhatTheTagsRemove(unittest.TestCase):
    """With no node read, step 2 is the only step that knows a
    locale file is gone at the new tag, and it said so only in its own output.
    It now writes the list the summary relays. Fabricated, because none of
    the pairs this suite pins deletes a locale file. The clone's real
    deletions are older -- glibc-2.23..glibc-2.24 deletes iw_IL and pap_AN --
    on tags the suite does not pin; the rename is tested on the real
    2.34..2.39 below."""

    def test_a_deleted_and_a_renamed_file_are_listed_under_the_old_name(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-removed-repo-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-removed-out-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        repo = make_glibc_shaped_repo(tmp, n_files=g.MIN_LOCALE_FILES)
        loc = os.path.join(repo, 'localedata', 'locales')
        # Every file the fixture writes has the same body, and git pairs a
        # rename with whichever identical deleted file it meets first -- here
        # loc_000, measured. One line tells x apart.
        with open(os.path.join(loc, 'x'), 'a') as fh:
            fh.write('% x\n')
        git(repo, 'commit', '-q', '-am', 't1b')
        git(repo, 'tag', 't1b')
        git(repo, 'rm', '-q', os.path.join(loc, 'loc_000'))
        git(repo, 'mv', os.path.join(loc, 'x'), os.path.join(loc, 'y'))
        git(repo, 'commit', '-q', '-m', 't2')
        git(repo, 'tag', 't2')
        rc, out = run_script('filter_lc_collate_changes.py', 't1b', 't2',
                             '--repo', repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': out_dir})
        self.assertEqual(rc, 0, out)
        # The output says the same as the file.
        self.assertEqual(Step2Report(out).removed,
                         {'loc_000': '', 'x': '(renamed to y)'})
        self.assertEqual(
            read_list(os.path.join(out_dir, 'step2_removed_locales.t1b..t2.txt')),
            ['# locale files at t1b and not at t2', 'loc_000 (deleted)',
             'x (renamed to y)'])


@needs_clone
class Step2RemovalListOnThePublishedPairs(unittest.TestCase):
    """The same list on the tags the published results are stated against."""

    def removal_list(self, old, new):
        out_dir = tempfile.mkdtemp(prefix='pg-glibc-removed-pair-')
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        rc, out = run_script('filter_lc_collate_changes.py', old, new,
                             env_extra={'PG_GLIBC_AUDIT_OUT': out_dir})
        self.assertEqual(rc, 0, out)
        return read_list(os.path.join(out_dir,
                                      f'step2_removed_locales.{old}..{new}.txt'))

    def test_the_rename_over_2_34_to_2_39_is_listed(self):
        self.assertEqual(self.removal_list(MID, NEW),
                         [f'# locale files at {MID} and not at {NEW}',
                          'aa_ER@saaho (renamed to ssy_ER)'])

    def test_a_pair_that_removes_nothing_writes_the_header_alone(self):
        """Absent is not empty: 2.28 to 2.34 removes no file."""
        self.assertEqual(self.removal_list(OLD, MID),
                         [f'# locale files at {OLD} and not at {MID}'])


@needs_clone
class AGainedBlockThatNamesNoCharacter(unittest.TestCase):
    """A file that gains a block naming no character -- a bare `copy` --
    gained a sort order: it was once reported as "no character is named",
    which lost that. A locale whose LC_COLLATE is stored on one side only
    is not compared, and is listed for Reindex under the `!!`, with the
    side. Fabricated, because no file gains a block in a pinned pair."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-gained-copy-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-gained-copy-out-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(tmp, n_files=g.MIN_LOCALE_FILES)
        with open(os.path.join(self.repo, 'localedata', 'locales', 'x'),
                  'a') as fh:
            fh.write('LC_COLLATE\ncopy "iso14651_t1"\nEND LC_COLLATE\n')
        git(self.repo, 'commit', '-q', '-am', 't2')
        git(self.repo, 'tag', 't2')

    def test_it_is_listed_with_the_side_that_stores_it(self):
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', self.repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_dir})
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(r.listed,
                         {'x': 'LC_COLLATE stored on the new side only'})
        self.assertEqual(r.differs, {})
        self.assertEqual(r.reindex, ['x'])
        self.assertEqual(read_list(os.path.join(
            self.out_dir, 'step2_changed_collate.t1..t2.txt')), ['x'])


@needs_clone
class AChangedCommentCharIsARulesChange(unittest.TestCase):
    """Backlog 13.7, end to end on a fabricated repository. `loc_000` keeps
    the text of its LC_COLLATE block from t2 to t3 and is read with another
    comment character at t3: the line `<U0043> % a note` ends in a comment
    under `%` and not under `#`, so the rules differ, and step 2 must list
    the locale and name the character. Fabricated, because no directive
    changes inside a pinned pair. A comment character that no line of the
    block meets changes no rule, and step 2 says the same (the control)."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-reading-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-reading-out-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(tmp, n_files=g.MIN_LOCALE_FILES)
        self.path = os.path.join(self.repo, 'localedata', 'locales',
                                 'loc_000')
        with open(self.path, 'a') as fh:
            fh.write('LC_COLLATE\ncopy "iso14651_t1"\nreorder-after '
                     '<U0041>\n<U0042>\n<U0043> % a note\nreorder-end\n'
                     'END LC_COLLATE\n')
        # loc_001 copies the same rules with no line a comment character
        # reaches (test_the_control_a_comment_character_no_rule_meets).
        with open(os.path.join(self.repo, 'localedata', 'locales',
                               'loc_001'), 'a') as fh:
            fh.write('LC_COLLATE\ncopy "iso14651_t1"\nEND LC_COLLATE\n')
        git(self.repo, 'commit', '-q', '-am', 't2')
        git(self.repo, 'tag', 't2')

    def commit_t3(self, old, new, *more):
        with open(self.path) as fh:
            text = fh.read()
        for a, b in ((old, new),) + more:
            self.assertEqual(text.count(a), 1)
            text = text.replace(a, b)
        with open(self.path, 'w') as fh:
            fh.write(text)
        git(self.repo, 'commit', '-q', '-am', 't3')
        git(self.repo, 'tag', 't3')
        return run_script('filter_lc_collate_changes.py', 't2', 't3',
                          '--repo', self.repo,
                          env_extra={'PG_GLIBC_AUDIT_OUT': self.out_dir})

    def written(self):
        return read_list(os.path.join(self.out_dir,
                                      'step2_changed_collate.t2..t3.txt'))

    def test_the_locale_differs_and_names_the_character(self):
        rc, out = self.commit_t3('comment_char %', 'comment_char #')
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(r.differs, {'loc_000': ([0x43], False)})
        self.assertEqual(r.listed, {})
        self.assertEqual(self.written(), ['loc_000'])

    def test_a_rule_and_the_characters_changed_together(self):
        """A rule added in the same commit: its character is named beside
        the one the comment character reads otherwise."""
        rc, out = self.commit_t3(
            'comment_char %', 'comment_char #',
            ('<U0042>\n', '<U0042>\n<U0044>\n'))
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(sorted(r.differs), ['loc_000'])
        chars, _ = r.differs['loc_000']
        self.assertLessEqual({0x43, 0x44}, set(chars))
        self.assertEqual(self.written(), ['loc_000'])

    def test_a_directive_spelt_across_the_escape_is_read(self):
        """`comment_c/har #`: inside a word glibc drops the escape and keeps
        the character after it (get_ident, linereader.c:583-592@2.39), so
        this is a comment_char directive. The line search before the reader
        could not settle it and said so; glibc_reader reads it as glibc
        does, and the report names the character it sets."""
        rc, out = self.commit_t3('\nLC_COLLATE\n',
                                 '\ncomment_c/har #\nLC_COLLATE\n')
        self.assertEqual(rc, 0, out)
        self.assertEqual(Step2Report(out).differs,
                         {'loc_000': ([0x43], False)})
        self.assertEqual(self.written(), ['loc_000'])

    def test_the_control_a_comment_edit_is_not(self):
        """Same file, same place outside the block, a change that is not a
        directive: still "do not touch LC_COLLATE"."""
        rc, out = self.commit_t3('% filler line 0\n', '% filler line zero\n')
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual((r.differs, r.listed), ({}, {}))
        self.assertEqual(self.written(), [])

    def test_the_control_a_comment_character_no_rule_meets(self):
        """loc_001 copies the same rules with no line a comment character
        reaches: `%` to `#` there changes no rule."""
        path = os.path.join(self.repo, 'localedata', 'locales', 'loc_001')
        with open(path) as fh:
            text = fh.read()
        with open(path, 'w') as fh:
            fh.write(text.replace('comment_char %', 'comment_char #', 1))
        rc, out = self.commit_t3('% filler line 0\n', '% filler line zero\n')
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual((r.differs, r.listed), ({}, {}))


@needs_clone
class UserGitConfigCannotChangeTheAnswer(unittest.TestCase):
    """"git diff respects the user's config": diff.noprefix broke the header
    regex (loudly), a low diff.renameLimit or diff.renames=false turned the
    one rename over 2.34..2.39 into delete+add (silently: the added file lands
    in 'not analysed', and a count of changed files reads 319 instead of
    318), and
    color.ui=always put escape codes in the pipe. Measured, git 2.50. The
    steps now pin every one of those with -c; this runs them under the hostile
    config and asserts the output is the one the suite already trusts."""

    HOSTILE = ('[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n'
               '\trenames = false\n\trenameLimit = 1\n'
               '[color]\n\tui = always\n')

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-gitconfig-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.cfg = os.path.join(tmp, 'gitconfig')
        with open(self.cfg, 'w', encoding='utf-8') as fh:
            fh.write(self.HOSTILE)
        self.out_a = tempfile.mkdtemp(prefix='pg-glibc-cfg-a-')
        self.out_b = tempfile.mkdtemp(prefix='pg-glibc-cfg-b-')
        self.addCleanup(shutil.rmtree, self.out_a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.out_b, ignore_errors=True)

    def test_the_hostile_config_really_is_hostile(self):
        """Control: without the overrides the config changes the text."""
        p = subprocess.run(['git', 'diff', '--name-only', f'{MID}..{NEW}',
                            '--', 'localedata/locales/'],
                           cwd=GLIBC_CLONE, capture_output=True,
                           env=dict(os.environ, GIT_CONFIG_GLOBAL=self.cfg))
        names = p.stdout.decode().split()
        self.assertEqual(len(names), 319, 'renames=false no longer changes '
                         'the count; the mutation this class guards is gone')

    def test_step_2_is_identical_under_the_hostile_config(self):
        plain = run_script('filter_lc_collate_changes.py', MID, NEW,
                           env_extra={'PG_GLIBC_AUDIT_OUT': self.out_a})
        hostile = run_script('filter_lc_collate_changes.py', MID, NEW,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_b,
                                        'GIT_CONFIG_GLOBAL': self.cfg})
        self.assertEqual(plain[0], 0, plain[1])
        # The characters file is named under each run's own directory.
        self.assertEqual((hostile[0], hostile[1].replace(self.out_b,
                                                         self.out_a)), plain)
        self.assertEqual(Step2Report(hostile[1]).removed,
                         {'aa_ER@saaho': '(renamed to ssy_ER)'})



@needs_clone
class StepTwoReadsTheDiffGitWouldPrintByDefault(unittest.TestCase):
    """Step 5 pinned six diff flags in PR #30; step 2, which decides which
    locales changed their order, carried none of them. Measured on 56abe94,
    git 2.54, 2.28..2.34, whose right answer is or_IN and sv_SE:

      `localedata/locales/* -diff` in core.attributesFile
          every locale diffed as "Binary files ... differ", no hunk, verdict
          'other': 0 files inside LC_COLLATE, exit 0, and the full audit
          printed "Reindex: none" at exit 0.
      a textconv that prepends 300 lines
          git numbers the hunks on the converted text and step 2 places them
          on the raw file: sv_SE lost, exit 0.
      diff.interHunkContext=50, diff.algorithm=patience with
      indentHeuristic=false, color.diff=always
          2 -> 5 files flagged; a different list of changed characters;
          "could not identify which characters changed".

    Each case asserts that step 2 prints exactly what it prints under no
    config, and carries its own control: the same config must change what a
    bare `git diff` prints, or the case guards nothing. Since step 9.2 step
    2 reads no patch, only git's `--raw` list of what was deleted or
    renamed, which none of these change (measured with git 2.54; the
    rename settings that do are UserGitConfigCannotChangeTheAnswer's): the
    cases hold that its answer does not move whatever the patch would
    print."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-step2cfg-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def env_with(self, config, attributes=None):
        cfg = os.path.join(self.tmp, f'gitconfig{len(os.listdir(self.tmp))}')
        if attributes is not None:
            attrs = cfg + '.attributes'
            with open(attrs, 'w', encoding='utf-8') as fh:
                fh.write(attributes)
            config = '[core]\n\tattributesFile = %s\n' % attrs + config
        with open(cfg, 'w', encoding='utf-8') as fh:
            fh.write(config)
        return {'GIT_CONFIG_GLOBAL': cfg, 'GIT_NO_LAZY_FETCH': '1'}

    def bare_diff(self, env_extra):
        """The `-U0` half of step 2's `git diff`, with nothing pinned: no -c,
        no flag. Not
        GIT_CONFIG_OVERRIDES either, which now pins two of these settings
        itself and would make the control for them pass vacuously."""
        p = subprocess.run(['git', 'diff', '-U0',
                            '--find-renames', f'{OLD}..{MID}', '--',
                            'localedata/locales/'],
                           cwd=GLIBC_CLONE, capture_output=True,
                           env=dict(os.environ, **env_extra))
        return p.stdout

    def step2(self, env_extra, *extra):
        out = tempfile.mkdtemp(dir=self.tmp)
        rc, text = run_script('filter_lc_collate_changes.py', OLD, MID, *extra,
                              env_extra=dict(env_extra,
                                             PG_GLIBC_AUDIT_OUT=out))
        return rc, text.replace(out, '<out>')

    def test_step_2_is_identical_under_each_config(self):
        pad = os.path.join(self.tmp, 'pad.sh')
        with open(pad, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\nyes "" | head -n 300\ncat "$1"\n')
        os.chmod(pad, 0o755)
        cases = {
            'binary': self.env_with('', 'localedata/locales/* -diff\n'),
            'textconv': self.env_with('[diff "pad"]\n\ttextconv = %s\n' % pad,
                                      '* diff=pad\n'),
            'external': self.env_with('[diff]\n\texternal = /usr/bin/true\n'),
            'interHunkContext': self.env_with(
                '[diff]\n\tinterHunkContext = 50\n'),
            'algorithm': self.env_with('[diff]\n\talgorithm = patience\n'
                                       '\tindentHeuristic = false\n'),
            # Per driver, through the attributes file: this beats the
            # `-c diff.algorithm=myers` override, and only the flag holds it.
            'driverAlgorithm': self.env_with(
                '[diff "alg"]\n\talgorithm = patience\n', '* diff=alg\n'),
            # Replaces a/ b/ as noprefix does, and step 2 died
            # telling the reader to drop a --diff-file never passed.
            'prefix': self.env_with('[diff]\n\tsrcPrefix = x/\n'
                                    '\tdstPrefix = y/\n'),
            'color': self.env_with('[color]\n\tdiff = always\n'
                                   '[color "diff"]\n\tmeta = normal\n'
                                   '\tfrag = normal\n'),
        }
        clean = {'GIT_NO_LAZY_FETCH': '1'}
        plain = self.step2(clean)
        self.assertEqual(plain[0], 0, plain[1])
        self.assertEqual(sorted(Step2Report(plain[1]).differs),
                         ['or_IN', 'sv_FI', 'sv_FI@euro', 'sv_SE', 'swedish'])
        reference = self.bare_diff(clean)
        for name, env_extra in cases.items():
            with self.subTest(config=name):
                self.assertTrue(self.bare_diff(env_extra) != reference,
                                f'{name} no longer changes a bare diff; '
                                f'this case guards nothing')
                self.assertEqual(self.step2(env_extra), plain)

    def unreadable_locales_at(self, tag):
        """Step 2 with every locale file at `tag` coming back `missing`, as
        a partial clone with no promisor, or GIT_NO_LAZY_FETCH, gives them:
        the files are in the tag's own tree, so that is a read that failed,
        never a tree with no locale."""
        return in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "real = g.read_blobs_at\n"
            "def fake(r, groups, raw=False):\n"
            "    return [({}, set(paths)) if tag == %r and paths and all(\n"
            "            p.startswith(g.LOCALES_DIR + '/') for p in paths)\n"
            "            else real(r, [(tag, paths)], raw=raw)[0]\n"
            "            for tag, paths in groups]\n"
            "g.read_blobs_at = fake\n"
            "f.main([%r, %r, '--repo', repo])" % (tag, OLD, MID))

    def test_an_unreadable_changed_file_in_the_transliteration_batch(self):
        """The transliteration check reads both texts of every file git
        lists as changed, in the batch with SUPPORTED, after the trees read
        them once already: a file that comes back unread there is refused,
        never compared as absent, which would say its LC_CTYPE changed or
        did not on no evidence. The fake fails only that batch (the call
        that also reads SUPPORTED at the new tag)."""
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "real = g.read_blobs_at\n"
            "def fake(r, groups, raw=False):\n"
            "    batch = (%r, [g.SUPPORTED]) in [(t, list(p)) for t, p in\n"
            "                                      groups]\n"
            "    return [({}, set(paths)) if batch and tag == %r and paths\n"
            "            and all(p.startswith(g.LOCALES_DIR + '/')\n"
            "                    for p in paths)\n"
            "            else real(r, [(tag, paths)], raw=raw)[0]\n"
            "            for tag, paths in groups]\n"
            "g.read_blobs_at = fake\n"
            "f.main([%r, %r, '--repo', repo])" % (MID, OLD, OLD, MID))
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f'listed at {OLD} could not be read while building the '
                      f'transliteration check', flat(out))
        self.assertNotIn('Listed for Reindex', out)

    def test_an_unreadable_old_locale_file_is_refused(self):
        rc, out = self.unreadable_locales_at(OLD)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f'listed at {OLD} could not be read while building the '
                      f'locale files', flat(out))
        self.assertNotIn('The sort rules localedef builds', out)

    def test_an_unreadable_new_locale_file_is_refused(self):
        rc, out = self.unreadable_locales_at(MID)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f'listed at {MID} could not be read while building the '
                      f'locale files', flat(out))
        self.assertNotIn('The sort rules localedef builds', out)

    def test_an_unreadable_supported_is_refused(self):
        """SUPPORTED is read at both tags: the files with no old side
        need its names -- ckb_IQ and mnw_MM over 2.28..2.34 -- and the
        character set check its builds. It is in the tree, so a read that
        comes back empty failed. Read as empty, each such file would print
        "(not in SUPPORTED)", a false and reassuring line. The fake answers
        only at the new tag. A step that read SUPPORTED at the old tag for
        those files would get the real file, which lacks both names, and
        print that line at exit 0; with the tag unpinned this test passed
        over it (false-negative-reviewer). Since step 9.2 the first read of
        it is supported_map's, which refuses it as absent."""
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "real = g.read_blobs_at\n"
            "g.read_blobs_at = lambda r, groups, raw=False: [\n"
            "    ({}, set(paths)) if (tag, paths) == (%r, [g.SUPPORTED])\n"
            "    else real(r, [(tag, paths)], raw=raw)[0]\n"
            "    for tag, paths in groups]\n"
            "f.main([%r, %r, '--repo', repo])" % (MID, OLD, MID))
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f'localedata/SUPPORTED does not exist at {MID}',
                      flat(out))
        self.assertNotIn('(not in SUPPORTED)', out)
        self.assertNotIn('Listed for Reindex', out)

    def test_an_unreadable_charmap_is_refused(self):
        """UTF-8 differs between 2.28 and 2.34, so both copies are read. A
        copy that comes back empty is a read that failed, never a charmap
        that defines nothing, which would put every character it holds in
        K, or none. Only the batch's read fails: the transliteration check
        reads charmaps later (PR C), and a fake that failed that read too
        let this pass with the batch's refusal removed."""
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "real, failed = g.read_blobs_at, []\n"
            "def fake(r, groups, raw=False):\n"
            "    out = []\n"
            "    for tag, paths in groups:\n"
            "        if (tag == %r and paths and not failed and\n"
            "                paths[0].startswith('localedata/charmaps/')):\n"
            "            failed.append(paths)\n"
            "            out.append(({}, set(paths)))\n"
            "        else:\n"
            "            out.append(real(r, [(tag, paths)], raw=raw)[0])\n"
            "    return out\n"
            "g.read_blobs_at = fake\n"
            "f.main([%r, %r, '--repo', repo])" % (OLD, OLD, MID))
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f'listed at {OLD} could not be read while building the '
                      f'character set check: localedata/charmaps/', flat(out))
        self.assertNotIn('Character sets', out)

    def test_an_unreadable_charmap_read_ahead_is_refused(self):
        """The charmaps the transliteration check needs and the batch did
        not read -- ISO-8859-1 among them, for fi_FI and uz_UZ -- are read
        by a thread while step 2 judges its verdicts (CharmapReads.prefetch).
        A copy that comes back empty there is refused like any other read,
        never taken as a charmap that defines nothing: every element would
        then be transliterated, or none. Only the first read of it fails,
        which is the one made ahead: a check that dropped it there would read
        it again later, get it, and print a clean line."""
        rc, out = in_subprocess(
            "import filter_lc_collate_changes as f\n"
            "real, failed = g.read_blobs_at, []\n"
            "def fake(r, groups, raw=False):\n"
            "    out = []\n"
            "    for tag, paths in groups:\n"
            "        if (tag == %r and 'localedata/charmaps/ISO-8859-1' in\n"
            "                paths and not failed):\n"
            "            failed.append(paths)\n"
            "            out.append(({}, set(paths)))\n"
            "        else:\n"
            "            out.append(real(r, [(tag, paths)], raw=raw)[0])\n"
            "    return out\n"
            "g.read_blobs_at = fake\n"
            "f.main([%r, %r, '--repo', repo])" % (OLD, OLD, MID))
        self.assertNotEqual(rc, 0, out)
        self.assertIn(f'listed at {OLD} could not be read while building the '
                      f'character set check: localedata/charmaps/', flat(out))
        self.assertIn('ISO-8859-1', flat(out))
        self.assertNotIn('Transliteration', out)


def bang_blocks(out):
    """The `!!` blocks of a step's output as audit.sh's warnings block reads
    them: a line opening with `!!` and every line after it that opens with
    three blanks, each block flattened to one line."""
    blocks, cur = [], None
    for line in out.split('\n'):
        if line.startswith('!!'):
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.startswith('   '):
            cur.append(line)
        else:
            cur = None
    return [flat(' '.join(b)) for b in blocks]


def _cm(name, *body):
    return '\n'.join([f'<code_set_name> {name}', '<comment_char> %',
                      '<escape_char> /', '<mb_cur_min> 1', '<mb_cur_max> 4',
                      'CHARMAP', '<U0041> /x41', '<U0042> /x42', *body,
                      'END CHARMAP', ''])


def _rules(*lines):
    return '\n'.join(['comment_char %', 'escape_char /', 'LC_COLLATE',
                      'order_start forward', *lines, 'order_end',
                      'END LC_COLLATE', ''])


def make_charmap_repo(root):
    """A glibc-shaped repository for step 2's character set check (backlog
    13.7, PR B). t1: UTF-8 lacks U+00E9 and ISO-8859-15 gives it /xe9.
    t2: UTF-8 gains it, ISO-8859-15 moves it to /xea, and loc_c's rules
    change. From t2: t3 has no SUPPORTED, t4 no ISO-8859-15, t5 a UTF-8
    with an <include>, which this tool does not read, t6 builds loc_a in
    ISO-8859-15 instead of UTF-8 under the same entry, t7 changes a comment
    of UTF-8 and nothing else, t8 has an empty SUPPORTED."""
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    loc = os.path.join(root, 'localedata', 'locales')
    cms = os.path.join(root, 'localedata', 'charmaps')
    os.makedirs(loc)
    os.makedirs(cms)

    def put(path, text):
        with open(os.path.join(root, path), 'w', encoding='utf-8') as fh:
            fh.write(text)

    for i in range(g.MIN_LOCALE_FILES):
        put(f'localedata/locales/filler_{i:03d}', 'LC_CTYPE\nEND LC_CTYPE\n')
    put('localedata/locales/loc_a', _rules('<U00E9>', '<U0041>'))
    put('localedata/locales/loc_b', _rules('<U0041>'))
    put('localedata/locales/loc_c', _rules('<U00E9>'))
    put('localedata/locales/en_XX', _rules('<U00E9>'))
    put('localedata/locales/de_XX', _rules('<U0041>'))
    put('localedata/SUPPORTED', 'SUPPORTED-LOCALES=\\\n' + ''.join(
        f'{e} \\\n' for e in ('loc_a.UTF-8/UTF-8', 'loc_b.UTF-8/UTF-8',
                               'loc_c.UTF-8/UTF-8', 'en_XX.UTF-8/UTF-8',
                               'de_XX/ISO-8859-15')))
    put('localedata/charmaps/UTF-8', _cm('UTF-8'))
    put('localedata/charmaps/ISO-8859-15', _cm('ISO-8859-15',
                                               '<U00E9> /xe9'))
    glibc_shaped(root)
    git(root, 'config', 'user.name', 't', env=env)
    git(root, 'config', 'user.email', 't@t', env=env)

    def commit(tag):
        git(root, 'add', '-A', '.', env=env)
        git(root, 'commit', '-q', '-m', tag, env=env)
        git(root, 'tag', tag, env=env)

    commit('t1')
    put('localedata/charmaps/UTF-8', _cm('UTF-8', '<U00E9> /xc3/xa9'))
    put('localedata/charmaps/ISO-8859-15', _cm('ISO-8859-15',
                                               '<U00E9> /xea'))
    put('localedata/locales/loc_c', _rules('<U0042>', '<U00E9>'))
    commit('t2')
    for tag, change in (('t3', lambda: os.remove(
                            os.path.join(root, 'localedata', 'SUPPORTED'))),
                        ('t4', lambda: os.remove(
                            os.path.join(cms, 'ISO-8859-15'))),
                        ('t5', lambda: put('localedata/charmaps/UTF-8',
                                           '<include> X\n'
                                           + _cm('UTF-8',
                                                 '<U00E9> /xc3/xa9'))),
                        ('t6', lambda: put(
                            'localedata/SUPPORTED',
                            open(os.path.join(root, 'localedata',
                                              'SUPPORTED')).read().replace(
                                'loc_a.UTF-8/UTF-8', 'loc_a.UTF-8/ISO-8859-15'))),
                        ('t7', lambda: put('localedata/charmaps/UTF-8',
                                           _cm('UTF-8', '% a new comment',
                                               '<U00E9> /xc3/xa9'))),
                        ('t8', lambda: put('localedata/SUPPORTED', ''))):
        git(root, 'checkout', '-q', 't2', env=env)
        change()
        commit(tag)
    return root


def make_translit_repo(root):
    """A glibc-shaped repository for step 2's transliteration check
    (backlog 13.7, PR C). uz_XX defines the element g + U+02BB, which its
    ISO-8859-1 cannot hold, and copies i18n in LC_CTYPE, whose only answer
    for U+02BB is translit_neutral's U+2018, which ISO-8859-1 lacks too:
    at t1 the element is not built. t2 adds "<U0027>" after it in
    translit_neutral, so the element is g'; no charmap changes. From t1:
    t3 makes the same change and UTF-8 gains a character; t4 gives
    ISO-8859-1 U+2018, and nothing else changes (H1); t5 changes UTF-8
    alone. t6 puts a line holding the byte 0xFF above translit_neutral's
    LC_CTYPE."""
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    for d_ in ('localedata/locales', 'localedata/charmaps', 'intl'):
        os.makedirs(os.path.join(root, *d_.split('/')))

    def put(path, text):
        with open(os.path.join(root, path), 'w', encoding='utf-8') as fh:
            fh.write(text)
    head = 'comment_char %\nescape_char /\n'

    def neutral(*alts):
        return (head + 'LC_CTYPE\ntranslit_start\n<U02BB> '
                + ';'.join(alts) + '\ntranslit_end\nEND LC_CTYPE\n')
    ascii_ = [f'<U{c:04X}> /x{c:02x}' for c in range(0x20, 0x7f)]
    for i in range(g.MIN_LOCALE_FILES):
        put(f'localedata/locales/filler_{i:03d}', 'LC_CTYPE\nEND LC_CTYPE\n')
    put('localedata/locales/i18n_ctype',
        head + 'LC_CTYPE\nupper <U0041>\nEND LC_CTYPE\n')
    put('localedata/locales/i18n',
        head + 'LC_CTYPE\ncopy "i18n_ctype"\n\ntranslit_start\n'
        'include "translit_neutral";""\ntranslit_end\nEND LC_CTYPE\n')
    put('localedata/locales/translit_neutral', neutral('"<U2018>"'))
    put('localedata/locales/uz_XX',
        head + 'LC_CTYPE\ncopy "i18n"\nEND LC_CTYPE\n\nLC_COLLATE\n'
        'collating-element <g-tc> from "g<U02BB>"\norder_start forward\n'
        '<U0067>\n<g-tc>\n<U0068>\norder_end\nEND LC_COLLATE\n')
    put('localedata/locales/loc_u', _rules('<U0041>'))
    put('localedata/charmaps/ISO-8859-1',
        _cm('ISO-8859-1', *ascii_[2:]))
    put('localedata/charmaps/UTF-8', _cm('UTF-8'))
    # Step 2 names the aliases glibc gives a locale, and refuses a tree
    # without the file or with none in it.
    put('intl/locale.alias', 'uzbek uz_XX.ISO-8859-1\n')
    put('localedata/SUPPORTED', 'SUPPORTED-LOCALES=\\\nuz_XX/ISO-8859-1 '
        '\\\nloc_u.UTF-8/UTF-8 \\\n')
    glibc_shaped(root)
    git(root, 'config', 'user.name', 't', env=env)
    git(root, 'config', 'user.email', 't@t', env=env)

    def commit(tag):
        git(root, 'add', '-A', '.', env=env)
        git(root, 'commit', '-q', '-m', tag, env=env)
        git(root, 'tag', tag, env=env)
    commit('t1')
    put('localedata/locales/translit_neutral',
        neutral('"<U2018>"', '"<U0027>"'))
    commit('t2')
    for tag, changes in (
            ('t3', [('localedata/charmaps/UTF-8', _cm('UTF-8', '<U00E9> /xc3/xa9')),
                    ('localedata/locales/translit_neutral',
                     neutral('"<U2018>"', '"<U0027>"'))]),
            ('t4', [('localedata/charmaps/ISO-8859-1',
                     _cm('ISO-8859-1', *ascii_[2:], '<U2018> /x91'))]),
            ('t5', [('localedata/charmaps/UTF-8',
                     _cm('UTF-8', '<U00E9> /xc3/xa9'))])):
        git(root, 'checkout', '-q', 't1', env=env)
        for path, text in changes:
            put(path, text)
        commit(tag)
    git(root, 'checkout', '-q', 't1', env=env)
    with open(os.path.join(root, 'localedata/locales/translit_neutral'),
              'wb') as fh:
        fh.write(neutral('"<U2018>"').encode().replace(
            b'LC_CTYPE\n', b'\xff\nLC_CTYPE\n', 1))
    commit('t6')
    return root


@needs_clone
class StepTwoComparesTransliterations(unittest.TestCase):
    """Step 2's transliteration check end to end on a repository built for
    it: the element uz_XX builds out of a transliteration, at both tags,
    and what it adds to Reindex. With no charmap changed, the check knows
    a locale file changed only from the texts step 2 reads of every file
    git lists as changed: given none, it said no LC_CTYPE changed over a
    changed translit_neutral (found writing step 9.2's tests)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-translit-')
        cls.repo = make_translit_repo(os.path.join(cls.tmp, 'repo'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def step2(self, old, new):
        out_dir = tempfile.mkdtemp(dir=self.tmp)
        rc, out = run_script('filter_lc_collate_changes.py', old, new,
                             '--repo', self.repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': out_dir})
        path = os.path.join(out_dir,
                            f'step2_keyed_locales.{g.pair_slug(old, new)}.txt')
        listed = None
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                listed = fh.read().split()
        return rc, out, listed

    def test_a_transliteration_file_that_changes_is_found(self):
        """No charmap and no LC_COLLATE changes: only translit_neutral,
        which no build's rules name. With nothing read beyond the changed
        files, the check reads what it needs (t2), and with every file read
        it reads the charmap ahead (t3)."""
        for new in ('t2', 't3'):
            with self.subTest(new):
                rc, out, listed = self.step2('t1', new)
                self.assertEqual(rc, 0, out)
                self.assertEqual(listed, ['uz_XX'])
                self.assertIn('Transliteration (LC_CTYPE): 1 collating '
                              f'element of 1 locale build depends on a '
                              f'transliteration; 1 is built differently at '
                              f'{new}.', flat(out))
                self.assertRegex(out, r'(?m)^  uz_XX \(ISO-8859-1\): 1 '
                                      r'element, defined in uz_XX\n      '
                                      r'transliterated characters \(1\): '
                                      '\u02bb \\(U\\+02BB\\)$')
                self.assertEqual(Step2Report(out).reindex, ['uz_XX'])

    def test_a_0xff_byte_is_seen_where_the_bytes_are_read(self):
        """read_blobs_at records the text it decoded from bytes that held
        0xFF, which errors='replace' makes one U+FFFD with every other
        invalid byte; step 2 then lists the build whose run reads it."""
        (got, _), = g.read_blobs_at(self.repo, [
            ('t6', ['localedata/locales/translit_neutral']),
        ])
        (plain, _), = g.read_blobs_at(self.repo, [
            ('t1', ['localedata/locales/translit_neutral']),
        ])
        self.assertIn(got['localedata/locales/translit_neutral'],
                      g.TEXTS_WITH_0XFF)
        self.assertNotIn(plain['localedata/locales/translit_neutral'],
                         g.TEXTS_WITH_0XFF)
        rc, out, listed = self.step2('t1', 't6')
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn('holds a 0xFF byte', flat(out))
        # Every build of the run, not only the one that reads the file: one
        # gate, so that no path of the plan passes over the byte. uz_XX is
        # the build that reads it, and its run stops at the byte on this
        # pre-2.36 tag: step 2 cannot compare it and lists it, so the check
        # counts it among the builds listed already.
        self.assertEqual(listed, ['loc_u'])
        self.assertIn('is listed already', flat(out))
        r = Step2Report(out)
        self.assertIn('uz_XX', r.listed)
        self.assertEqual(r.reindex,
                         ['i18n', 'loc_u', 'translit_neutral', 'uz_XX'])

    def test_a_charmap_that_gains_an_answer_is_found(self):
        """H1, as M2 measured it: ISO-8859-1 gains U+2018, which only the
        transliteration of U+02BB names. The character set search finds no
        rule that names it; the element is now g and that byte."""
        rc, out, listed = self.step2('t1', 't4')
        self.assertEqual(rc, 0, out)
        self.assertEqual(listed, ['uz_XX'])
        self.assertIn("No build's LC_COLLATE names one of those characters.",
                      out)
        self.assertIn('1 is built differently at t4.', flat(out))

    def test_nothing_built_differently_says_so(self):
        """t5 changes UTF-8 alone and uz_XX is built in ISO-8859-1:
        nothing it reads changed, and that is what is printed, not a
        comparison that was not made."""
        rc, out, listed = self.step2('t1', 't5')
        self.assertEqual(rc, 0, out)
        self.assertEqual(listed, [])
        self.assertIn('Transliteration (LC_CTYPE): 1 locale build may build '
                      'a collating element out of a transliteration, and '
                      'nothing it reads changed between the tags.', flat(out))
        self.assertNotIn('built differently', flat(out))


@needs_clone
class StepTwoNamesWhatItDidNotCompare(unittest.TestCase):
    """make_glibc_shaped_repo's SUPPORTED names x and y at both tags, and
    the rename leaves each source at one tag only: nothing is compared, and
    both facts are `!!` blocks, which audit.sh repeats in its summary."""

    def test_nothing_compared_is_said_twice_where_the_summary_reads(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-charmaps-none-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = make_glibc_shaped_repo(os.path.join(tmp, 'r'),
                                      n_files=g.MIN_LOCALE_FILES, rename=True)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': tmp})
        self.assertEqual(rc, 0, out)
        blocks = bang_blocks(out)
        self.assertTrue(any(b.startswith('!! NOT RUN: the comparison of the '
                                         'character sets') for b in blocks),
                        out)
        self.assertIn('!! 2 SUPPORTED entries at both tags are not compared: '
                      'their source file is not at both. x.UTF-8, y.UTF-8',
                      blocks)


@needs_clone
class StepTwoComparesCharmaps(unittest.TestCase):
    """Step 2 end to end on a repository built for it: what goes to
    Reindex through step2_keyed_locales.<pair>.txt, what it counts as
    listed anyway, and when it says NOT RUN instead of a clean line."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-charmaps-')
        cls.repo = make_charmap_repo(os.path.join(cls.tmp, 'repo'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def step2(self, old, new):
        out_dir = tempfile.mkdtemp(dir=self.tmp)
        rc, out = run_script('filter_lc_collate_changes.py', old, new,
                             '--repo', self.repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': out_dir})
        path = os.path.join(out_dir,
                            f'step2_keyed_locales.{g.pair_slug(old, new)}.txt')
        listed = None
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                listed = fh.read().split()
        return rc, out, listed

    def test_what_a_charmap_change_reaches_goes_to_reindex(self):
        """loc_a and en_XX name U+00E9, which UTF-8 gains: listed, each with
        the character. They are found through ISO-8859-15 too, which
        SUPPORTED never builds them in and which moves U+00E9 (H3). loc_c
        names it as well, and its own rules differ, so Reindex lists it
        anyway: counted, not repeated. loc_b and de_XX name only A."""
        rc, out, listed = self.step2('t1', 't2')
        self.assertEqual(rc, 0, out)
        self.assertEqual(listed, ['en_XX', 'loc_a'])
        self.assertRegex(out, r'(?m)^  loc_a\.UTF-8 \(UTF-8\): 1 character, '
                              r'named in loc_a\n      characters its character '
                              r'set changed \(1\): \xe9 \(U\+00E9\)$')
        self.assertIn('ISO-8859-15: 1 character given other bytes. UTF-8: 1 '
                      'character added.', flat(out))
        self.assertRegex(flat(out), r'Reindex lists anyway, whose '
                                    r'LC_COLLATE names one of those '
                                    r'characters or could not be compared '
                                    r'\(2\): loc_c\.UTF-8 \(UTF-8\)')
        self.assertRegex(flat(out), r'!! 2 locale\(s\) that SUPPORTED never '
                                    r'builds in a charmap that changed .*'
                                    r'en_XX \(ISO-8859-15\).* loc_a '
                                    r'\(ISO-8859-15\)')
        self.assertNotIn('loc_b', out)
        self.assertNotIn('de_XX', out)
        self.assertEqual(Step2Report(out).reindex, ['en_XX', 'loc_a', 'loc_c'])

    def test_a_missing_supported_or_charmap_is_not_run(self):
        """Absent is not empty: a tag without a charmap SUPPORTED names,
        or whose SUPPORTED lists no locale, gets a `!!` NOT RUN, never the
        clean line. One with no SUPPORTED at all is refused: step 2 needs
        its names for the files only the new side has (supported_map)."""
        rc, out, listed = self.step2('t2', 't3')
        self.assertEqual(rc, 2, out)
        self.assertIn(f'{g.SUPPORTED} does not exist at t3', flat(out))
        self.assertNotIn('Listed for Reindex', out)
        for new, why in (('t4', 'ISO-8859-15 at t4'),
                         ('t8', f'{g.SUPPORTED} at t8 lists no locale')):
            with self.subTest(new):
                rc, out, listed = self.step2('t2', new)
                self.assertEqual(rc, 0, out)
                self.assertIn('!! NOT RUN: the comparison of the character '
                              'sets', flat(out))
                self.assertIn(why, flat(out))
                # Whole, in the shape audit.sh repeats in its summary.
                self.assertTrue(any('NOT RUN: the comparison' in b and why in b
                                    for b in bang_blocks(out)), out)
                self.assertNotIn("No build's LC_COLLATE names", out)
                self.assertEqual(listed, [])

    def test_a_charmap_glibc_reads_another_way_lists_its_builds(self):
        """And says that the search for builds a distro could add in it did
        not run: en_XX's ISO-8859-15 build is found only by that search, and
        the same silence over UTF-8 would hide such a build."""
        rc, out, listed = self.step2('t2', 't5')
        self.assertEqual(rc, 0, out)
        self.assertEqual(listed, ['en_XX', 'loc_a', 'loc_b', 'loc_c'])
        blocks = bang_blocks(out)
        self.assertTrue(any(b.startswith('!! 4 locale build(s) could not be '
                                         'compared') and
                            'charmap UTF-8: line 1, before CHARMAP' in b
                            for b in blocks), out)
        self.assertTrue(any('did not run for these charmaps' in b and
                            'UTF-8: charmap UTF-8: line 1' in b
                            for b in blocks), out)

    def test_a_charmap_with_no_character_changed(self):
        """UTF-8 changes in a comment only: it was read, K is empty, and
        step 2 said so. It died on this, over five pairs of real tags,
        2.39..2.40 among them (false-negative-reviewer)."""
        rc, out, listed = self.step2('t2', 't7')
        self.assertEqual(rc, 0, out)
        self.assertIn('UTF-8: no character changed.', out)
        self.assertEqual(listed, [])

    def test_a_build_that_changes_charmap_is_compared_old_against_new(self):
        """loc_a.UTF-8 is built with UTF-8 at t2 and ISO-8859-15 at t6, and
        no charmap changed under its own name, so the batch read neither
        charmap, and both are read in one more batch (read_more). Their
        code_set_names differ, so the build is listed with that reason,
        which needs both texts: unread, the reason would say so."""
        rc, out, listed = self.step2('t2', 't6')
        self.assertEqual(rc, 0, out)
        self.assertEqual(listed, ['loc_a'])
        self.assertIn("loc_a.UTF-8 (ISO-8859-15): the charmap's code_set_name "
                      "is UTF-8 at t2 and ISO-8859-15 at t6", flat(out))

    def test_one_commit_twice_compares_nothing_and_says_so(self):
        rc, out, listed = self.step2('t2', 't2')
        self.assertEqual(rc, 0, out)
        self.assertIn('Character sets and transliterations: not compared, the '
                      'two tags are one commit.', out)
        self.assertEqual(listed, [])


def make_unsettled_charmap_repo(root):
    """loc_a names U+00E9 and reaches reorder-sections-after, which
    glibc_reader does not settle. u2 changes UTF-8 alone (it gains
    U+00E9); u3 makes the same change and changes loc_b's rules."""
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
    for d_ in ('localedata/locales', 'localedata/charmaps'):
        os.makedirs(os.path.join(root, *d_.split('/')))

    def put(path, text):
        with open(os.path.join(root, path), 'w', encoding='utf-8') as fh:
            fh.write(text)

    for i in range(g.MIN_LOCALE_FILES):
        put(f'localedata/locales/filler_{i:03d}', 'LC_CTYPE\nEND LC_CTYPE\n')
    put('localedata/locales/loc_a',
        'comment_char %\nescape_char /\nLC_COLLATE\norder_start forward\n'
        '<U00E9>\n<U0041>\norder_end\nreorder-sections-after <U0041>\n'
        'reorder-sections-end\nEND LC_COLLATE\n')
    put('localedata/locales/loc_b', _rules('<U0041>'))
    put('localedata/SUPPORTED', 'SUPPORTED-LOCALES=\\\nloc_a.UTF-8/UTF-8 \\\n'
                                'loc_b.UTF-8/UTF-8 \\\n')
    put('localedata/charmaps/UTF-8', _cm('UTF-8'))
    glibc_shaped(root)
    git(root, 'config', 'user.name', 't', env=env)
    git(root, 'config', 'user.email', 't@t', env=env)

    def commit(tag):
        git(root, 'add', '-A', '.', env=env)
        git(root, 'commit', '-q', '-m', tag, env=env)
        git(root, 'tag', tag, env=env)

    commit('u1')
    put('localedata/charmaps/UTF-8', _cm('UTF-8', '<U00E9> /xc3/xa9'))
    commit('u2')
    git(root, 'checkout', '-q', 'u1', env=env)
    put('localedata/charmaps/UTF-8', _cm('UTF-8', '<U00E9> /xc3/xa9'))
    put('localedata/locales/loc_b', _rules('<U0042>'))
    commit('u3')
    return root


@needs_clone
class StepTwoListsWhatItCannotCompare(unittest.TestCase):
    """A locale glibc_reader cannot settle is listed for Reindex by step 2
    itself, whatever else changed. Before step 9.2 step 3 listed it, and ran
    only when step 2 gave it a name: with none, step 2 counted loc_a as
    listed by step 3 anyway, wrote no list, and the summary said "Reindex:
    none" over a locale whose character set changed (review of the whole
    of block 15, 2026-10-30)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-unsettled-charmaps-')
        cls.repo = make_unsettled_charmap_repo(os.path.join(cls.tmp, 'repo'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    step2 = StepTwoComparesCharmaps.step2

    def test_with_nothing_else_changed_it_is_listed(self):
        """u2 changes UTF-8 alone: no locale's rules differ, and loc_a,
        which the reader cannot settle, is listed for Reindex with its
        reason; the character set check counts it as listed anyway."""
        rc, out, listed = self.step2('u1', 'u2')
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(r.differs, {})
        self.assertEqual(sorted(r.listed), ['loc_a'])
        self.assertEqual(r.reindex, ['loc_a'])
        self.assertEqual(listed, [])
        self.assertRegex(flat(out), r'Reindex lists anyway, whose '
                                    r'LC_COLLATE names one of those '
                                    r'characters or could not be compared '
                                    r'\(1\): loc_a\.UTF-8 \(UTF-8\)')

    def test_with_another_locale_changed_it_is_listed_too(self):
        """Control: with loc_b's change, loc_b differs and loc_a is still
        listed."""
        rc, out, listed = self.step2('u1', 'u3')
        self.assertEqual(rc, 0, out)
        r = Step2Report(out)
        self.assertEqual(sorted(r.differs), ['loc_b'])
        self.assertEqual(r.reindex, ['loc_a', 'loc_b'])
        self.assertEqual(listed, [])


def build_side(build, version):
    """A machine known by its measurement: what locale_order.compare
    hands glibc_locale_data.order."""
    return g.side(None, build=build, version=version)


class OneOrderForEverySide(unittest.TestCase):
    """glibc_locale_data.order is the one function that orders two sides:
    git for two tags (PairOrderIsMeasuredNotAssumed), rpm for two builds of
    one glibc, the release for a build against a tag. Two builds of one
    version are a real pair -- RHEL8 changed C.UTF-8's order at
    glibc-2.28-93 -- so rpm's own rule decides them, never their text."""

    V = 'glibc 2.34'
    OLD_B = 'glibc-2.34-275.el9_8.x86_64'
    NEW_B = 'glibc-2.34-283.el9_8.x86_64'

    def status(self, old, new, repo=None):
        return g.order(repo, old, new)[0]

    def test_two_builds_of_one_version_are_ordered_by_rpm(self):
        old = build_side(self.OLD_B, self.V)
        new = build_side(self.NEW_B, self.V)
        self.assertEqual(self.status(old, new), 'forward')
        self.assertEqual(self.status(new, old), 'reversed')
        # 283 against 1000: a text comparison would call this reversed.
        later = build_side('glibc-2.34-1000.el9_8.x86_64', self.V)
        self.assertEqual(self.status(new, later), 'forward')

    def test_the_same_build_is_the_same_side(self):
        """Equal by rpm across architecture words, and equal words that
        rpm cannot read."""
        two_arch = build_side(f'{self.NEW_B} glibc-2.34-283.el9_8.i686',
                              self.V)
        self.assertEqual(self.status(build_side(self.NEW_B, self.V), two_arch),
                         'same')
        odd = build_side('weird-build-string', self.V)
        self.assertEqual(self.status(odd, odd), 'same')

    def test_equal_words_are_one_build_whatever_the_version_says(self):
        """A machine known only by its measurement (step 11) has the build
        id it recorded, and the version is read from that id: a free-text
        id gives none. Two equal ids
        are still one build (backlog 16.62), where reading the version
        first called them undetermined."""
        self.assertEqual(self.status(build_side('same-build', None),
                                     build_side('same-build', None)), 'same')
        self.assertEqual(self.status(build_side('same-build', None),
                                     build_side('other-build', None)),
                         'undetermined')



    def test_a_build_word_without_its_architecture_is_not_read(self):
        """build_parts reads the last dotted word as the architecture only
        when it can be one: `.40` and `.el10_0` cannot. Reading `.40` so
        made el8_10.40 and el8_10.48 one build, 'same' in both directions
        (measured 2026-11-02)."""
        import locale_order as lo
        for word in ('glibc-2.28-251.el8_10.40', 'glibc-2.28-251.el8_10.48',
                     'glibc-2.39-46.el10_0'):
            self.assertIsNone(lo.build_parts(word), word)
        for arch in ('src', 'x86_64', 'i686', 'aarch64', 'noarch'):
            self.assertEqual(
                lo.build_parts('glibc-2.28-251.el8_10.40.' + arch),
                ('2.28', '251.el8_10.40'), arch)
        v = 'glibc 2.28'
        bare = [build_side('glibc-2.28-251.el8_10.%d' % n, v)
                for n in (40, 48)]
        self.assertEqual(self.status(*bare), 'undetermined')
        src = [build_side('glibc-2.28-251.el8_10.%d.src' % n, v)
               for n in (40, 48)]
        self.assertEqual(self.status(*src), 'forward')
        self.assertEqual(self.status(*reversed(src)), 'reversed')

    def test_a_build_that_cannot_be_read_is_undetermined(self):
        odd = build_side('weird-build-string', self.V)
        new = build_side(self.NEW_B, self.V)
        self.assertEqual(self.status(odd, new), 'undetermined')
        self.assertEqual(self.status(new, odd), 'undetermined')
        self.assertEqual(self.status(build_side(self.NEW_B, None), new),
                         'undetermined')
        # Two machines whose `rpm -q glibc` printed nothing: no build on
        # either side is not one build.
        self.assertEqual(self.status(build_side(None, self.V),
                                     build_side(None, self.V)), 'undetermined')
        self.assertEqual(self.status(build_side('', self.V),
                                     build_side('', self.V)), 'undetermined')

    def test_the_version_decides_before_the_build(self):
        """2.28's build 999 is still older than 2.34's 275."""
        old = build_side('glibc-2.28-999.el8_10.x86_64', 'glibc 2.28')
        new = build_side(self.OLD_B, self.V)
        self.assertEqual(self.status(old, new), 'forward')
        self.assertEqual(self.status(new, old), 'reversed')

    def test_a_build_against_a_tag_is_ordered_by_release(self):
        """A build carries its version, a tag its release (describe). One
        release on both sides is not placed: nothing here says whether the
        build is that tag or later."""
        tmp = tempfile.mkdtemp(prefix='pg-glibc-order-mixed-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = make_release_line_repo(tmp)
        b28 = build_side('glibc-2.28-251.el8_10.40.x86_64', 'glibc 2.28')
        b34 = build_side(self.OLD_B, self.V)
        self.assertEqual(self.status(b28, 'glibc-2.34', repo), 'forward')
        self.assertEqual(self.status('glibc-2.34', b28, repo), 'reversed')
        self.assertEqual(self.status(b34, 'glibc-2.28', repo), 'reversed')
        self.assertEqual(self.status('glibc-2.28', b34, repo), 'forward')
        self.assertEqual(self.status(b34, 'glibc-2.34', repo), 'undetermined')
        self.assertEqual(self.status('glibc-2.34', b34, repo), 'undetermined')
        # The backport describes as glibc-2.28: one release with b28.
        self.assertEqual(self.status('backport', b28, repo), 'undetermined')

    def test_pair_order_is_order(self):
        """pair_order, which every step asks, takes its answer from order:
        a planted answer reaches it."""
        with mock.patch.object(g, 'order', return_value=('reversed', 'x')):
            self.assertEqual(g.pair_order(None, 'a', 'b'), ('reversed', 'x'))


class NothingComparedIsNotTheOrder(unittest.TestCase):
    """Two commits of one source tree compare nothing, whatever their
    order: two builds of one glibc version with the same locale sources
    (measured 2026-11-02 for .40/.48, 275/283, 128/145) are in order by rpm,
    and step 2 would compare a tree against itself, which is nothing. The clone holds
    real ones too, on glibc's own branches. They keep the `!!`
    that one commit compared with itself gets (audit.sh's SAME_TAG), which
    the order alone would not give them: built here as a tag and an empty
    commit on top of it, the shape branch 7.4 gives two builds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-one-tree-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(self.tmp, n_files=3, rename=True,
                                           base=None)
        git(self.repo, 'checkout', '-q', 't1')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 't1b')
        git(self.repo, 'tag', 't1b')

    def word(self, *args):
        """What `glibc_locale_data.py order --quiet` prints for audit.sh:
        the direction, and what nothing_compared answered."""
        out = io.StringIO()
        with mock.patch.object(g, 'find_repo', return_value=self.repo), \
                contextlib.redirect_stdout(out):
            self.assertEqual(g._main(['order', '--quiet', *args]), 0)
        return out.getvalue().strip()

    def orphan(self):
        """t1's tree in a commit with no parent: one tree that git cannot
        order against t1, as a master commit and the same patch on a wip
        branch are in the clone."""
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        sha = subprocess.run(['git', 'commit-tree', 't1^{tree}', '-m', 'o'],
                             cwd=self.repo, env=env, check=True,
                             capture_output=True, text=True).stdout.strip()
        git(self.repo, 'tag', 'orphan', sha, env=env)
        return 'orphan'

    def test_one_tree_in_order_compares_nothing(self):
        self.assertEqual(g.order(self.repo, 't1', 't1b')[0], 'forward')
        self.assertEqual(g.nothing_compared(self.repo, 't1', 't1b'), 'tree')
        self.assertEqual(self.word('t1', 't1b'), 'forward tree')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, nothing = g.pair_check(self.repo, 't1', 't1b')
        self.assertEqual((status, nothing), ('forward', 'tree'))
        self.assertIn('!! t1 and t1b are two commits of one source tree.',
                      flat(out.getvalue()))
        self.assertIn('Step 2 compares nothing: it would compare that tree '
                      'against itself.', flat(out.getvalue()))
        self.assertIn('A run on the tags says nothing about an intra-major '
                      'upgrade', flat(out.getvalue()))

    def test_one_commit_still_says_so(self):
        self.assertEqual(g.nothing_compared(self.repo, 't1', 't1'), 'commit')
        self.assertEqual(self.word('t1', 't1'), 'same commit')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            g.pair_check(self.repo, 't1', 't1')
        self.assertIn('!! t1 and t1 are the same commit.', flat(out.getvalue()))

    def test_two_trees_compare_something(self):
        """Control: t2 renames a file, so it is another tree."""
        self.assertIsNone(g.nothing_compared(self.repo, 't1', 't2'))
        self.assertEqual(self.word('t1', 't2'), 'forward none')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(g.pair_check(self.repo, 't1', 't2'),
                             ('forward', None))
        self.assertNotIn('!!', out.getvalue())

    def test_one_tree_git_cannot_order_compares_nothing(self):
        """Both answers, apart: the direction is not known, and nothing is
        compared; audit.sh reads the second word for the latter."""
        o = self.orphan()
        self.assertEqual(self.word('t1', o), 'undetermined tree')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(g.pair_check(self.repo, 't1', o),
                             ('undetermined', 'tree'))
        text = flat(out.getvalue())
        self.assertIn(f'!! t1 and {o} are two commits of one source tree.',
                      text)
        self.assertIn('The DIRECTION of this pair could not be established',
                      text)

    def test_one_tree_reversed_on_request_compares_nothing(self):
        """--allow-reverse runs a reversed pair; of one tree, it still
        compares nothing, and says so beside the REVERSED notice
        (false-negative-reviewer, round 1 of 7.1: step 5 printed its clean
        verdict over 9b3aac58 -> 7423da21 of the clone)."""
        self.assertEqual(self.word('--allow-reverse', 't1b', 't1'),
                         'reversed tree')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(g.pair_check(self.repo, 't1b', 't1',
                                          allow_reverse=True),
                             ('reversed', 'tree'))
        text = flat(out.getvalue())
        self.assertIn('!! t1b and t1 are two commits of one source tree.', text)
        self.assertIn('REVERSED PAIR, allowed on request', text)

    def test_one_tree_reversed_is_refused(self):
        """The order is still asked: one tree given the wrong way round is
        refused like any reversed pair."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit):
            g.pair_check(self.repo, 't1b', 't1')
        self.assertIn('This pair is REVERSED', err.getvalue())


@needs_clone
class Step2AsksWhetherAnythingIsCompared(unittest.TestCase):
    """Step 2 used to decide "one commit" by comparing the two commit ids
    itself; it takes nothing_compared's answer now, so two commits of one
    tree skip the character set check's reads as one commit does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-step2-one-tree-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(self.tmp, n_files=g.MIN_LOCALE_FILES,
                                           rename=True)
        git(self.repo, 'checkout', '-q', 't1')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 't1b')
        git(self.repo, 'tag', 't1b')

    def groups(self, old, new):
        import filter_lc_collate_changes as f
        seen = []
        real = g.read_blobs_at

        def spy(repo, groups, **kw):
            groups = [(ref, list(paths)) for ref, paths in groups]
            seen.extend(groups)
            return real(repo, groups, **kw)
        out = io.StringIO()
        with mock.patch.object(g, 'read_blobs_at', spy), \
                mock.patch.object(g, 'OUT_DIR', self.tmp), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            f.main([old, new, '--repo', self.repo])
        self.out = flat(out.getvalue())
        return [(ref, path) for ref, paths in seen for path in paths]

    def test_one_tree_reads_no_supported(self):
        self.assertNotIn(('t1b', g.SUPPORTED), self.groups('t1', 't1b'))
        self.assertIn('Character sets and transliterations: not compared, '
                      'the two refs are two commits of one source tree.',
                      self.out)

    def test_one_commit_says_one_commit(self):
        self.groups('t1', 't1')
        self.assertIn('Character sets and transliterations: not compared, '
                      'the two tags are one commit.', self.out)

    def test_two_trees_read_it(self):
        """Control: the renamed pair is compared, and SUPPORTED is read."""
        self.assertIn(('t2', g.SUPPORTED), self.groups('t1', 't2'))
        self.assertNotIn('Character sets and transliterations: not compared',
                         self.out)


@needs_floor_pair
class OneFloorForEverySide(unittest.TestCase):
    """Every side of every step is refused below one floor
    (glibc_locale_data.below_floor, applied by side() and machine_side()),
    on the tag side and on the machine side. With the floor moved to 300,
    glibc-2.12 (286 locale files) is below it and glibc-2.17 (312) above,
    and a machine's file of 299 locale files is below it and one of 300
    above: each step is driven so that exactly one side is short, and must
    refuse that side. A step that kept a floor of its own on one side stays
    at 200 and runs on."""

    FLOOR = 300

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='pg-glibc-one-floor-')
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.dirs = {}
        for name, start, count in (('ok', 0, 300), ('okb', 0, 300),
                                   ('ok2', 150, 300), ('short', 0, 299)):
            root = os.path.join(cls.tmp, name)
            os.mkdir(root)
            for i in range(start, start + count):
                with open(os.path.join(root, f'loc_{i:03d}'), 'w') as fh:
                    fh.write('LC_CTYPE\nEND LC_CTYPE\n')
            cls.dirs[name] = root

    def refusal(self, main, argv):
        err = io.StringIO()
        with mock.patch.object(g, 'MIN_LOCALE_FILES', self.FLOOR), \
                mock.patch.object(g, 'OUT_DIR', self.tmp), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            main(argv)
        self.assertEqual(cm.exception.code, 2, err.getvalue())
        return flat(err.getvalue())

    def test_the_tag_side_of_every_step(self):
        import filter_lc_collate_changes as f
        old, new = FLOOR_OLD, FLOOR_NEW
        here = os.path.join(self.tmp, 'sides')
        if not os.path.isdir(here):
            os.mkdir(here)
            os.symlink(GLIBC_CLONE, os.path.join(here, 'glibc'))
        steps = (
            ('the sides', lambda argv: g.compared_sides(*argv),
             [here, old, new]),
            ('corpus subcommand', g._main, ['corpus', old, new]),
            ('2', f.main, [old, new]),
        )
        for name, main, argv in steps:
            with self.subTest(step=name):
                self.assertIn(f'only 286 file(s) under {g.LOCALES_DIR}/ at '
                              f'{old}, below the floor of {self.FLOOR}.',
                              self.refusal(main, argv))

    def test_the_machine_side_of_every_step(self):
        """A machine's file is read through its commit (machine_side), and
        the commit is listed and held to the floor like a tag: 299 locale
        files are refused, 300 are not. The sides and step 2 both reach a
        machine through this one function."""
        for name, count in (('short', 299), ('ok', 300)):
            folder = os.path.join(self.tmp, f'machine-{name}')
            if not os.path.isdir(folder):
                os.mkdir(folder)
                shutil.copytree(self.dirs[name],
                                os.path.join(folder, 'locales'))
                with open(os.path.join(folder, 'locale_order.out'),
                          'w') as fh:
                    fh.write(f'a measurement of {name}\n')
                with open(os.path.join(folder, 'build-id'), 'w') as fh:
                    fh.write('glibc-2.17-326.el7.x86_64\n')
            build = f'glibc-2.17-{300 + count}.el7'
            with self.subTest(files=count):
                if count < self.FLOOR:
                    self.assertIn(
                        f'only {count} file(s) under {g.LOCALES_DIR}/ at ',
                        self.refusal(lambda argv: g.machine_side(*argv),
                                     [GLIBC_CLONE, build, [folder]]))
                    continue
                with mock.patch.object(g, 'MIN_LOCALE_FILES', self.FLOOR):
                    sd, _ = g.machine_side(GLIBC_CLONE, build, [folder])
                self.assertEqual(len(sd.paths), count)


if __name__ == '__main__':
    unittest.main()
