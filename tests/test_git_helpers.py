"""Layer 2: the git-backed helpers, against the real clone.

This layer exists for one class of bug: code that cannot tell "there is nothing
here" from "I could not look". Both print the same thing, and in this tool the
thing they print is a clean verdict.

The failures are provoked with invalid tags and paths. No network is touched and
the clone is never modified.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _harness import (GLIBC_CLONE, MID, NEW, OLD, SCRIPTS_DIR, flat,
                      needs_clone)

import glibc_locale_data as g
import diff_collation_code as d

BAD = 'no-such-tag-4c1f'


def in_subprocess(code):
    """Run code that is expected to call die(); return (exit, output).

    die() calls sys.exit, so these have to run out of process to be observed
    without tearing down the test runner.
    """
    boot = ("import sys; sys.path.insert(0, %r); "
            "import glibc_locale_data as g, diff_collation_code as d; "
            "repo = %r\n" % (os.path.dirname(g.__file__), GLIBC_CLONE))
    p = subprocess.run([sys.executable, '-c', boot + code],
                       capture_output=True, cwd=os.path.dirname(g.__file__))
    return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')


@needs_clone
class GitFailureIsNotSilence(unittest.TestCase):
    """"A failed git was indistinguishable from 'this file did not change'".

    On a --filter=blob:none clone, fetches really do fail:
        fatal: could not fetch <oid> from promisor remote
    An invalid revision range produces the same shape -- non-zero exit, empty
    stdout -- without needing the network to be down.
    """

    def test_report_file_aborts_instead_of_reporting_no_change(self):
        """It used to return 0, i.e. 'no substantive change', for a file whose
        diff it never read."""
        rc, out = in_subprocess(
            "d.report_file(repo, 'locale/programs/ld-collate.c',"
            " '%s1..%s2', False)" % (BAD, BAD))
        self.assertNotEqual(rc, 0, 'a failed git diff was swallowed')
        self.assertIn('fatal', out.lower())

    def test_check_paths_aborts_instead_of_calling_a_real_file_harmless(self):
        """It used to return outside=['locale/programs/ld-collate.c'], printed
        as 'nothing to read, and nothing to miss', for a file that exists."""
        rc, out = in_subprocess(
            "print(d.check_paths(repo, ['locale/programs/ld-collate.c'],"
            " '%s1', '%s2'))" % (BAD, BAD))
        self.assertNotEqual(rc, 0, 'a failed ls-tree was swallowed')
        self.assertNotIn("outside=['locale/programs/ld-collate.c']", out)

    def test_a_diff_that_really_is_empty_still_reports_no_change(self):
        """The other direction: aborting must not turn 'unchanged' into an
        error. coll-lookup.h has only a copyright change over this pair."""
        n = d.report_file(GLIBC_CLONE, 'locale/coll-lookup.h',
                          f'{MID}..{NEW}', False, quiet_when_clean=True)
        self.assertEqual(n, 0)


@needs_clone
class AbsentAtBothIsTwoFacts(unittest.TestCase):
    """"A tracked path absent from BOTH tags was called harmless."

    check_paths files every such path under "nothing to read, and nothing to
    miss". That is true of a file not yet written when the range begins, and
    false of one renamed away before the older tag -- for which the audit reads
    nothing, `git diff` reports no error, and the step goes on to print its
    clean sentence. Both shapes are in the real clone: locale/C-collate-seq.c
    arrives in glibc 2.35, and locale/xlocale.h was deleted before 2.28.
    """

    def test_a_path_deleted_before_the_old_tag_is_not_harmless(self):
        renamed, unborn, never = d.absent_at_both(
            GLIBC_CLONE, ['locale/xlocale.h', 'locale/C-collate-seq.c'],
            OLD, MID)
        self.assertEqual(renamed, ['locale/xlocale.h'])
        self.assertEqual(unborn, ['locale/C-collate-seq.c'])
        self.assertEqual(never, [])

    def test_a_path_that_lived_and_died_inside_the_range_is_not_unborn(self):
        """Asking only the OLDER tag files a path added and removed inside the
        range under "not yet written, nothing to miss".
        posix/spawnattr_tcgetpgrp.c is added by 342cc934a3 and removed by
        6289d28d3c, both between 2.34 and 2.39."""
        renamed, unborn, never = d.absent_at_both(
            GLIBC_CLONE, ['posix/spawnattr_tcgetpgrp.c'], MID, NEW)
        self.assertEqual((renamed, unborn, never),
                         (['posix/spawnattr_tcgetpgrp.c'], [], []))

    def test_a_path_that_lived_on_a_merged_side_branch_is_not_unborn(self):
        """`git log -- <path>` with default history simplification does not
        report a path that was added and deleted on a branch that was later
        merged: the merge has the same tree as its first parent for that path,
        so simplification prunes the side. --full-history keeps it. Fabricated,
        because glibc has no path of this shape in the audited pairs."""
        tmp = tempfile.mkdtemp(prefix='pg-glibc-sidebranch-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        with open(os.path.join(tmp, 'keep'), 'w') as fh:
            fh.write('base\n')
        git(tmp, 'init', '-q', env=env)
        git(tmp, 'add', '.', env=env)
        git(tmp, 'commit', '-q', '-m', 'base', env=env)
        git(tmp, 'checkout', '-q', '-b', 'side', env=env)
        with open(os.path.join(tmp, 'gone.c'), 'w') as fh:
            fh.write('int f (void) { return 0; }\n')
        git(tmp, 'add', '.', env=env)
        git(tmp, 'commit', '-q', '-m', 'add gone.c', env=env)
        git(tmp, 'rm', '-q', 'gone.c', env=env)
        git(tmp, 'commit', '-q', '-m', 'remove gone.c', env=env)
        git(tmp, 'checkout', '-q', '-', env=env)
        git(tmp, 'merge', '-q', '--no-ff', '-m', 'merge side', 'side', env=env)
        git(tmp, 'tag', 'old', env=env)
        with open(os.path.join(tmp, 'keep'), 'a') as fh:
            fh.write('later\n')
        git(tmp, 'add', '.', env=env)
        git(tmp, 'commit', '-q', '-m', 'later', env=env)
        git(tmp, 'tag', 'new', env=env)

        simplified = subprocess.run(
            ['git', '-C', tmp, 'log', '-1', '--format=%H', 'old', '--',
             'gone.c'], capture_output=True, env=env)
        if simplified.stdout.strip():
            self.skipTest('this git does not simplify the side branch away; '
                          'the case this test guards is not reproducible here')
        renamed, unborn, never = d.absent_at_both(tmp, ['gone.c'], 'old', 'new')
        self.assertEqual((renamed, unborn, never), (['gone.c'], [], []),
                         'a file that existed and was removed read as '
                         '"not yet written"')

    def test_a_path_no_ref_ever_carried_is_not_read_as_not_yet_written(self):
        """A path NO ref in the clone has ever had is not a file waiting to
        be written: it is a name in the curated lists that matches nothing,
        and those lists are the ceiling of what step 5 reads. Measured with
        `ld-collate.c` spelt `ld-colate.c`: the pair reported 6 substantive
        hunks instead of 24, the Bug 22668 hunks gone, and the only mention
        was a note saying there was nothing to miss."""
        renamed, unborn, never = d.absent_at_both(
            GLIBC_CLONE, ['locale/programs/ld-colate.c'], OLD, MID)
        self.assertEqual((renamed, unborn, never),
                         ([], [], ['locale/programs/ld-colate.c']))

    def test_a_failed_git_log_is_not_read_as_never_existed(self):
        """The reassuring half is "did not exist yet": an error must not land
        there."""
        rc, out = in_subprocess(
            "print(d.absent_at_both(repo, ['locale/xlocale.h'], '%s', '%s'))"
            % (BAD, BAD))
        self.assertNotEqual(rc, 0, 'a failed git log was swallowed')
        self.assertNotIn("(['locale/xlocale.h'], [], [])", out)


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

    def test_absent_at_both_refuses_a_shallow_clone(self):
        rc, out = in_subprocess(
            "print(d.absent_at_both(%r, ['locale/xlocale.h'], '%s', '%s'))"
            % (self.repo, OLD, OLD))
        self.assertNotEqual(rc, 0, 'a shallow clone was read as history')
        self.assertIn('shallow', out)
        self.assertNotIn("([], ['locale/xlocale.h'], [])", out)

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
            "print(d.absent_at_both(repo, ['locale/xlocale.h'], '%s', '%s'))"
            % (OLD, MID))
        self.assertNotEqual(rc, 0, 'an unusable answer was read as "not shallow"')
        self.assertIn('neither true nor false', out)

    def test_the_full_clone_still_answers(self):
        """Control: the refusal must not fire on the clone this tool uses."""
        renamed, unborn, never = d.absent_at_both(
            GLIBC_CLONE, ['locale/xlocale.h'], OLD, MID)
        self.assertEqual((renamed, unborn, never),
                         (['locale/xlocale.h'], [], []))


@needs_clone
class AHijackedDiffIsNotNoChange(unittest.TestCase):
    """`diff.external` in a config this run does not control, or
    GIT_EXTERNAL_DIFF in the environment, replaces the unified diff with
    whatever that program prints. With /usr/bin/true, every tracked file read
    as "no substantive change" and the step printed its clean sentence."""

    def test_an_external_diff_driver_does_not_change_the_count(self):
        env = dict(os.environ, GIT_EXTERNAL_DIFF='/usr/bin/true',
                   GIT_NO_LAZY_FETCH='1')
        p = subprocess.run([sys.executable, 'diff_collation_code.py', OLD, MID],
                           cwd=SCRIPTS_DIR, env=env, capture_output=True)
        out = (p.stdout + p.stderr).decode('utf-8', 'replace')
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('24 substantive hunk(s) found', out)

    def test_a_textconv_driver_does_not_change_the_count(self):
        """--no-ext-diff does NOT disable `diff.<driver>.textconv`, and a
        textconv that empties both sides leaves an EMPTY diff -- which the
        hunk-less guard below never sees either, because there is no output
        at all. Reached through the user's core.attributesFile, which no
        override this run makes can pin."""
        tmp = tempfile.mkdtemp(prefix='pg-glibc-textconv-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        attrs = os.path.join(tmp, 'attributes')
        with open(attrs, 'w', encoding='utf-8') as fh:
            fh.write('* diff=nul\n')
        cfg = os.path.join(tmp, 'gitconfig')
        with open(cfg, 'w', encoding='utf-8') as fh:
            fh.write('[core]\n\tattributesFile = %s\n'
                     '[diff "nul"]\n\ttextconv = /usr/bin/true\n' % attrs)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=cfg, GIT_NO_LAZY_FETCH='1')

        # The fixture must bite, or this test guards nothing: without the
        # flag, the same config has to empty the diff.
        bare = subprocess.run(
            ['git', '-C', GLIBC_CLONE, 'diff', '--no-ext-diff',
             f'{OLD}..{MID}', '--', 'locale/programs/ld-collate.c'],
            env=env, capture_output=True)
        if bare.stdout.strip():
            self.skipTest('textconv is not applied by this git; nothing to '
                          'guard against here')

        p = subprocess.run([sys.executable, 'diff_collation_code.py', OLD, MID],
                           cwd=SCRIPTS_DIR, env=env, capture_output=True)
        out = (p.stdout + p.stderr).decode('utf-8', 'replace')
        self.assertEqual(p.returncode, 0, out)
        self.assertIn('24 substantive hunk(s) found', out)

    def hostile(self, body):
        """A GIT_CONFIG_GLOBAL holding `body`, plus a check that it bites."""
        tmp = tempfile.mkdtemp(prefix='pg-glibc-hostile-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = os.path.join(tmp, 'gitconfig')
        with open(cfg, 'w', encoding='utf-8') as fh:
            fh.write(body)
        return dict(os.environ, GIT_CONFIG_GLOBAL=cfg, GIT_NO_LAZY_FETCH='1')

    def step5(self, env, old, new):
        p = subprocess.run([sys.executable, 'diff_collation_code.py', old, new],
                           cwd=SCRIPTS_DIR, env=env, capture_output=True)
        return p.returncode, (p.stdout + p.stderr).decode('utf-8', 'replace')

    def test_a_coloured_diff_does_not_read_as_all_comment(self):
        """`color.diff` beats the `color.ui=false` override (more specific
        wins). With the hunk headers left plain they still match, every body
        line begins with an escape, each hunk comes back empty and therefore
        all-noise, and step 5 printed its clean sentence over the pair that
        carries Bug 22668."""
        env = self.hostile('[color]\n\tdiff = always\n'
                           '[color "diff"]\n\tfrag = normal\n')
        raw = subprocess.run(
            ['git', '-C', GLIBC_CLONE, '-c', 'color.ui=false', 'diff',
             f'{OLD}..{MID}', '--', 'locale/programs/ld-collate.c'],
            env=env, capture_output=True)
        if b'\x1b[' not in raw.stdout:
            self.skipTest('this git does not colour a piped diff here')
        rc, out = self.step5(env, OLD, MID)
        self.assertEqual(rc, 0, out)
        self.assertIn('24 substantive hunk(s) found', out)

    def test_the_context_count_is_not_the_readers_to_choose(self):
        """The classifier reads the context lines, so the count of them is
        part of the measurement. Both routes: `diff.context` in config, and
        GIT_DIFF_OPTS, which git applies AFTER the command line -- so `-U3` on
        the argv does not win and run_git drops the variable instead."""
        for name, env in (
                ('diff.context', self.hostile('[diff]\n\tcontext = 0\n')),
                # interHunkContext=50 merges two nearby changes into one hunk:
                # 31 instead of 52, with the same >> lines. Nothing hidden,
                # the same drift in a published number.
                ('diff.interHunkContext',
                 self.hostile('[diff]\n\tinterHunkContext = 50\n')),
                ('GIT_DIFF_OPTS', dict(os.environ, GIT_DIFF_OPTS='-u0',
                                       GIT_NO_LAZY_FETCH='1'))):
            with self.subTest(route=name):
                rc, out = self.step5(env, MID, NEW)
                self.assertEqual(rc, 0, out)
                self.assertIn('52 substantive hunk(s) found', out)

    def test_the_diff_algorithm_is_not_the_readers_to_choose(self):
        """patience and histogram pair the same changed lines into different
        hunks: the hunk count holds at 52 over 2.34..2.39 but the `>>` lines
        go 733 -> 731, so the count alone would not notice. Nothing is
        hidden -- every changed line still carries its marker -- but a
        published number must not move with a reader's config."""
        for algo in ('patience', 'histogram'):
            with self.subTest(algorithm=algo):
                env = self.hostile('[diff]\n\talgorithm = %s\n' % algo)
                rc, out = self.step5(env, MID, NEW)
                self.assertEqual(rc, 0, out)
                self.assertIn('52 substantive hunk(s) found', out)
                marked = [ln for ln in out.split('\n')
                          if ln.startswith('      >> ')]
                self.assertEqual(len(marked), 733)

    def test_output_with_no_hunk_in_it_is_not_read_as_unchanged(self):
        """"Binary files ... differ", or any diff this parser does not
        understand: the file DID change and nothing read the change."""
        rc, out = in_subprocess(
            "class R:\n"
            "    stdout = b'Binary files a/x and b/x differ\\n'\n"
            "g.run_git = lambda *a, **k: R()\n"
            "d.report_file(repo, 'locale/programs/ld-collate.c', 'a..b', False)")
        self.assertNotEqual(rc, 0, 'a diff with no hunk was read as no change')
        self.assertIn('no hunk', out)


@needs_clone
class TheCleanSentenceNeedsSomethingRead(unittest.TestCase):
    """"Step 5 printed its clean sentence over a walk that read nothing."

    Unreachable with any tag in use: it needs glibc to have moved ld-collate.c
    or strcoll_l.c before BOTH tags of a pair, so the branches are driven by
    injection, on the real clone.
    """

    def run_injected(self, assignments, *tags):
        return in_subprocess(
            "%s\nsys.argv = ['x']\nd.main(%r)\n" % (assignments, list(tags)))

    def test_a_collapsed_include_walk_is_not_a_clean_result(self):
        """Entry points that resolve to nothing: the walk reaches 0 files,
        every tier is empty, and the step used to exit 0 saying there was no
        substantive collation code change."""
        rc, out = self.run_injected(
            "d.ENTRY_POINTS = ['locale/programs/no-such-entry.c']\n"
            "d.TIER1 = []\nd.TIER2 = []", OLD, MID)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('No substantive collation code change', out)
        self.assertIn('!!', out)
        self.assertIn('the include walk reached no file', flat(out))

    def test_a_path_renamed_away_before_both_tags_is_not_a_clean_result(self):
        """A same-tag range makes every diff empty, so the only thing left to
        decide the verdict is the tracked path that is in neither tree."""
        rc, out = self.run_injected(
            "d.TIER1 = ['locale/xlocale.h']\nd.TIER2 = []", OLD, OLD)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('No substantive collation code change', out)
        self.assertIn('locale/xlocale.h: ABSENT at glibc-2.28 and glibc-2.28',
                      flat(out))
        self.assertIn('are in NEITHER tree', flat(out))

    def test_a_vanished_path_is_still_a_blocker(self):
        """The oldest of the four reasons, and the one no test drove: the
        reversed pair that exercises the `!!` block finds 52 hunks, so it
        never reaches the verdict where `blockers` is read. Remove the
        `vanished` entry from the list and this fails."""
        rc, out = self.run_injected(
            "d.check_paths = lambda *a: (['locale/xlocale.h'], [])",
            OLD, OLD)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('No substantive collation code change', out)
        self.assertIn('1 tracked path(s) vanished before glibc-2.28',
                      flat(out))

    def test_a_misspelt_tracked_path_is_not_a_clean_result(self):
        """The whole of finding "a path that never existed is filed under
        nothing to miss": with a name that matches nothing, the tier is read
        as empty and the step used to say so in a note."""
        rc, out = self.run_injected(
            "d.TIER1 = ['locale/programs/ld-colate.c']\nd.TIER2 = []",
            OLD, OLD)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('No substantive collation code change', out)
        self.assertIn('exist at no ref in this clone', flat(out))
        self.assertIn('ld-colate.c: no ref in this clone has ever had it',
                      flat(out))

    def test_a_path_not_yet_written_still_gives_a_clean_result(self):
        """The control: same shape, benign cause. C-collate-seq.c arrives in
        2.35, so over a 2.28 range there is genuinely nothing to read -- and a
        guard that fires here would make every audited pair unresolved.

        The four blocker notices are what must be absent, named one by one.
        This used to assert that NO `!!` was printed at all, which stopped
        being the right assertion when the step started saying that one commit
        compared with itself compares nothing -- and this class passes the same
        tag twice on purpose, to isolate the injected condition."""
        rc, out = self.run_injected(
            "d.TIER1 = ['locale/C-collate-seq.c']\nd.TIER2 = []", OLD, OLD)
        self.assertEqual(rc, 0, out)
        self.assertIn('No substantive collation code change', out)
        for blocker in ('vanished before', 'exist at no ref in this clone',
                        'GONE at', 'include walk reached'):
            self.assertNotIn(blocker, flat(out))


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
    # testing showed why: reverting build_copy_graph to `contents, _ =
    # read_blobs(...)` left the whole suite green, because every path it reads
    # comes from ls-tree at the same tag and so `missing` is empty in practice.
    # Testing the helper alone guards nothing. Injecting a missing blob is what
    # makes the guard observable.

    def test_build_copy_graph_aborts_on_an_unreadable_blob(self):
        """A dropped blob silently shrinks the copy graph, and a smaller graph
        yields a shorter affected-locale list -- the reassuring direction."""
        rc, out = in_subprocess(
            "real = g.read_blobs\n"
            "def fake(repo, tag, paths):\n"
            "    c, _ = real(repo, tag, paths)\n"
            "    dropped = sorted(c)[0]\n"
            "    del c[dropped]\n"
            "    return c, {dropped}\n"
            "g.read_blobs = fake\n"
            "g.build_copy_graph(repo, %r)\n" % NEW)
        self.assertNotEqual(rc, 0, 'an unreadable locale was dropped silently')
        self.assertIn('copy graph', out)

    def test_the_include_walk_aborts_on_an_unreadable_blob(self):
        rc, out = in_subprocess(
            "real = g.read_blobs\n"
            "def fake(repo, tag, paths):\n"
            "    c, _ = real(repo, tag, paths)\n"
            "    dropped = sorted(c)[0]\n"
            "    del c[dropped]\n"
            "    return c, {dropped}\n"
            "g.read_blobs = fake\n"
            "d.reachable_from_entry_points(repo, %r)\n" % NEW)
        self.assertNotEqual(rc, 0, 'an unreadable header shrank the walk')
        self.assertIn('include walk', out)


@needs_clone
class MissingIsNotUnchanged(unittest.TestCase):
    """"Step 5 could not tell 'unchanged' from 'not there'".

    git diff over a path absent at both tags is empty and exits 0 -- and so is
    the truth. A path that vanished between the tags reads the same way and is
    not the truth.
    """

    def test_a_path_that_vanished_is_reported_as_vanished(self):
        # gen-translit.pl exists at 2.28 and is deleted by 2.34.
        vanished, outside = d.check_paths(GLIBC_CLONE,
                                          ['locale/gen-translit.pl'], OLD, MID)
        self.assertEqual(vanished, ['locale/gen-translit.pl'])
        self.assertEqual(outside, [])

    def test_a_path_absent_at_both_tags_is_reported_as_outside(self):
        # C-collate-seq.c arrives upstream after 2.34.
        vanished, outside = d.check_paths(GLIBC_CLONE,
                                          ['locale/C-collate-seq.c'], OLD, MID)
        self.assertEqual(vanished, [])
        self.assertEqual(outside, ['locale/C-collate-seq.c'])

    def test_a_path_present_at_both_tags_is_neither(self):
        vanished, outside = d.check_paths(
            GLIBC_CLONE, ['locale/programs/ld-collate.c'], OLD, MID)
        self.assertEqual((vanished, outside), ([], []))


@needs_clone
class IncludeWalk(unittest.TestCase):
    """"Step 5's file list was the ceiling of what it could see".

    linereader.h and elem-hash.h both changed over 2.34..2.39 and were in
    neither hand-written tier, so they were never read.
    """

    def test_the_walk_reaches_the_files_the_hand_lists_missed(self):
        got = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        for path in ('locale/programs/linereader.h', 'locale/elem-hash.h',
                     'locale/programs/locfile-token.h'):
            self.assertIn(path, got)

    def test_the_walk_picks_up_separately_compiled_units(self):
        """coll-lookup.c and simple-hash.c are linked, not included; they are
        reached as the sibling .c of a header the walk found."""
        got = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        self.assertIn('locale/coll-lookup.c', got)
        self.assertIn('locale/programs/simple-hash.c', got)

    def test_the_walk_stays_bounded(self):
        """Unbounded it reaches 265 files dominated by stdio.h and cdefs.h.
        The bound is what makes the output readable, so it is asserted."""
        got = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        self.assertLess(len(got), 60, 'the include walk escaped locale/')
        for path in got:
            self.assertTrue(path.startswith('locale/'), path)

    def test_hand_listed_paths_the_walk_cannot_reach_are_still_tracked(self):
        """weight.h arrives via `#include WEIGHT_H`, a macro no regex resolves,
        and it HAS a substantive change over 2.34..2.39. Replacing the curated
        lists with the walk would have lost it."""
        walk = d.reachable_from_entry_points(GLIBC_CLONE, NEW)
        for path in ('locale/weight.h', 'locale/weightwc.h',
                     'locale/lc-collate.c', 'locale/C-collate.c'):
            self.assertNotIn(path, walk)
            self.assertIn(path, d.TIER1)


@needs_clone
class SupportedMap(unittest.TestCase):
    """"supported_map() returned an empty map when SUPPORTED was missing",
    which made every locale print as 'not built by default'."""

    def test_aborts_when_supported_is_absent(self):
        rc, out = in_subprocess("g.supported_map(repo, '%s')" % BAD)
        self.assertNotEqual(rc, 0)

    def test_maps_source_names_to_generated_names(self):
        got = g.supported_map(GLIBC_CLONE, NEW)
        self.assertIn('sv_SE.utf8', got['sv_SE'])
        self.assertIn('C.utf8', got['C'])


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


def make_glibc_shaped_repo(root, n_files, rename=False, dates=None):
    """A git repository that passes _is_glibc_clone: localedata/locales/ with
    `n_files` locale sources and a SUPPORTED, committed and tagged `t1`. With
    `rename`, a second commit `t2` renames the block-less file `x` to `y` AND
    gives it an LC_COLLATE block -- the case no upstream pair has produced.

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
    with open(os.path.join(root, 'localedata', 'SUPPORTED'), 'w') as fh:
        fh.write('x.UTF-8/UTF-8 \\\ny.UTF-8/UTF-8 \\\n')
    at = lambda when: (env if when is None
                       else dict(env, GIT_AUTHOR_DATE=when,
                                 GIT_COMMITTER_DATE=when))
    env1 = at(dates[0] if dates else None)
    env2 = at(dates[1] if dates else None)
    git(root, 'init', '-q', env=env1)
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


def add_unrelated_commit(repo, when, name='unrelated'):
    """An orphan root committed at `when`: a commit that is an ancestor of
    nothing and a descendant of nothing, with no glibc tag reachable from it,
    so neither question pair_order asks can order it. No pinned tag pair has
    that shape.

    `--orphan` keeps the index, so the commit still carries whatever corpus
    the repository had; the marker file is only there to make it non-empty,
    and it is a new path so that this works in either fixture."""
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
               GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
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
    same two files that touch LC_COLLATE and step 5 a hunk count, no `!!`
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
                                      dates=dates)

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
                              f'audit-locale-diff.sh and CI both do')

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
        self.assertNotIn('Files with changes inside LC_COLLATE', out)

    def test_the_forward_control_still_runs(self):
        rc, out = self.step2('t1', 't2')
        self.assertEqual(rc, 0, out)
        self.assertIn('Files with changes inside LC_COLLATE: 1', out)

    def test_the_same_commit_twice_says_nothing_was_compared(self):
        """A hand-run step used to end in its clean sentence, rc 0 and no
        `!!`, for a pair that is one commit compared with itself -- a clean
        verdict over a comparison that never happened. The wrapper said so and
        the steps did not; the notice lives in the shared helper now."""
        rc, out = self.step2('t1', 't1')
        self.assertEqual(rc, 0, out)
        self.assertIn('!! ', out)
        self.assertIn('t1 and t1 are the same commit', flat(out))
        self.assertIn("they can only report 'nothing changed'", flat(out))

    def test_an_undetermined_direction_is_said_out_loud_and_runs(self):
        """The fourth state at the call site. Not an error -- nothing below is
        wrong on its account -- but silence here reads as "the direction was
        checked", and this is the one branch where the tool cannot tell which
        way round the pair is. The shim in test_wrapper stands in for the
        wrapper's summary block and cannot reach this warning: it replaces the
        very subcommand that prints it."""
        add_unrelated_commit(self.repo, '2020-01-01T00:00:00+0000')
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


@needs_clone
class Step1RefusesAReversedPairBeforeReportingAnything(unittest.TestCase):
    """Step 1 is the fourth entry point that takes a pair, and the first one
    with a clone to ask: it does the cloning. Unguarded, a hand-run
    `audit-locale-diff.sh glibc-2.34 glibc-2.28` printed a changed-file count,
    three template verdicts and a fan-in computed at the tag that is actually
    the older one -- all of it about the other direction, and none of it saying
    so. Under audit.sh this is also what keeps a doomed run from printing
    findings before the refusal."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-step1-order-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.env = {'PG_GLIBC_AUDIT_OUT': self.out_dir}

    def test_it_refuses_and_reports_nothing(self):
        rc, out = run_script('audit-locale-diff.sh', MID, OLD,
                             env_extra=self.env)
        self.assertEqual(rc, 2, out)
        self.assertIn('This pair is REVERSED', flat(out))
        self.assertNotIn('Locale files added, modified or deleted', out)
        self.assertNotIn('Collation templates', out)

    def test_allow_reverse_reports_and_says_the_pair_is_reversed(self):
        rc, out = run_script('audit-locale-diff.sh', MID, OLD,
                             '--allow-reverse', env_extra=self.env)
        self.assertEqual(rc, 0, out)
        self.assertIn('!! REVERSED PAIR', out)
        self.assertIn('Locale files added, modified or deleted', out)

    def test_the_forward_control_is_untouched(self):
        """The guard refuses a direction, and says nothing otherwise: the
        three audited pairs' output has to stay byte-identical."""
        rc, out = run_script('audit-locale-diff.sh', OLD, MID,
                             env_extra=self.env)
        self.assertEqual(rc, 0, out)
        self.assertNotIn('!!', out)

    def test_a_third_argument_that_is_not_the_flag_is_refused(self):
        rc, out = run_script('audit-locale-diff.sh', OLD, MID, '--nonsense',
                             env_extra=self.env)
        self.assertEqual(rc, 2, out)
        self.assertIn("unknown argument '--nonsense'", out)


class TagModeCorpusFloor(unittest.TestCase):
    """"No corpus floor in tag mode": a tag whose tree holds a handful of
    locale files -- or none -- made step 2 print "0 changed", step 4 print
    "No locale uses ellipsis ranges here", and both exit 0. The node modes had
    refused such a directory from the start. No clone needed: the repository
    is fabricated, because no pinned tag is small enough to show it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-tiny-repo-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_step_4_refuses_a_tag_below_the_floor(self):
        repo = make_glibc_shaped_repo(self.tmp, n_files=3)
        rc, out = run_script('flag_algorithmic_ranges.py', 't1', '--repo', repo)
        self.assertEqual(rc, 2, out)
        self.assertIn(f'below the floor of {g.MIN_LOCALE_FILES}', out)
        self.assertNotIn('No locale uses ellipsis ranges', out)

    def test_step_2_refuses_a_tag_below_the_floor(self):
        repo = make_glibc_shaped_repo(self.tmp, n_files=3, rename=True)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo)
        self.assertEqual(rc, 2, out)
        self.assertIn('below the floor', out)
        self.assertNotIn('Files with changes inside LC_COLLATE', out)

    def test_the_shell_step_refuses_too(self):
        """Step 1 is shell; it asks glibc_locale_data.py, which dies."""
        repo = make_glibc_shaped_repo(self.tmp, n_files=3)
        p = subprocess.run([sys.executable,
                            os.path.join(SCRIPTS_DIR, 'glibc_locale_data.py'),
                            'corpus', 't1'],
                           cwd=repo, capture_output=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(b'below the floor', p.stderr)

    def test_a_corpus_at_the_floor_passes(self):
        """The control: the guard refuses size, not fabricated repositories."""
        repo = make_glibc_shaped_repo(self.tmp, n_files=g.MIN_LOCALE_FILES,
                                      rename=True)
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', repo)
        self.assertEqual(rc, 0, out)


class RenamedFileGainsABlock(unittest.TestCase):
    """"Step 2 aborted on a rename without an old LC_COLLATE block", end to end
    on a fabricated repository: `x` (no block) becomes `y` (with one). The old
    code died reading `x` at t2; with the read fixed but the lookup not, it
    reported y as 'no LC_COLLATE on either side'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-rename-repo-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_dir = tempfile.mkdtemp(prefix='pg-glibc-rename-out-')
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.repo = make_glibc_shaped_repo(self.tmp, n_files=g.MIN_LOCALE_FILES,
                                           rename=True)

    def test_the_renamed_file_is_reported_as_gained_collate(self):
        rc, out = run_script('filter_lc_collate_changes.py', 't1', 't2',
                             '--repo', self.repo,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_dir})
        self.assertEqual(rc, 0, out)
        self.assertIn('renamed: 1', out)
        self.assertIn('1 file(s) GAINED an LC_COLLATE block at t2', out)
        self.assertIn('Files with changes inside LC_COLLATE: 1', out)
        self.assertIn('x -> y', out)
        with open(os.path.join(self.out_dir, 'step2_changed_collate.t1..t2.txt'),
                  encoding='utf-8') as fh:
            self.assertEqual(fh.read().split(), ['y'],
                             'step 3 must get the name that exists at t2')


@needs_clone
class UserGitConfigCannotChangeTheAnswer(unittest.TestCase):
    """"git diff respects the user's config": diff.noprefix broke the header
    regex (loudly), a low diff.renameLimit or diff.renames=false turned the
    one rename over 2.34..2.39 into delete+add (silently: the added file lands
    in 'not analysed', and step 1's count reads 319 instead of 318), and
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
        self.assertEqual(hostile, plain)
        self.assertIn('renamed: 1', hostile[1])

    def test_the_template_verdict_does_not_trust_an_external_diff(self):
        """`git diff --quiet` ignores an external helper -- unless the config
        trusts its exit code, and then that exit code IS the verdict for the
        three templates every other locale inherits from. No audited pair can
        show it (all three are unchanged on both), so this asserts the two
        halves separately: the flag defeats the hijack on a file that really
        changed, and the script's probe carries the flag."""
        hijack = dict(os.environ,
                      GIT_EXTERNAL_DIFF='/usr/bin/true',
                      GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE='true',
                      GIT_NO_LAZY_FETCH='1')
        sv_SE = 'localedata/locales/sv_SE'
        hijacked = subprocess.run(
            ['git', 'diff', '--quiet', f'{OLD}..{MID}', '--', sv_SE],
            cwd=GLIBC_CLONE, capture_output=True, env=hijack)
        if hijacked.returncode != 0:
            self.skipTest('this git does not trust the helper exit code here')
        guarded = subprocess.run(
            ['git', 'diff', '--quiet', '--no-ext-diff', f'{OLD}..{MID}',
             '--', sv_SE],
            cwd=GLIBC_CLONE, capture_output=True, env=hijack)
        self.assertNotEqual(guarded.returncode, 0,
                            '--no-ext-diff did not defeat the hijack')
        src = open(os.path.join(SCRIPTS_DIR, 'audit-locale-diff.sh'),
                   encoding='utf-8').read()
        self.assertIn('git diff --quiet --no-ext-diff', src,
                      "step 1's template probe would take the helper's word")

    def test_step_1_is_identical_under_the_hostile_config(self):
        plain = run_script('audit-locale-diff.sh', MID, NEW,
                           env_extra={'PG_GLIBC_AUDIT_OUT': self.out_a})
        hostile = run_script('audit-locale-diff.sh', MID, NEW,
                             env_extra={'PG_GLIBC_AUDIT_OUT': self.out_b,
                                        'GIT_CONFIG_GLOBAL': self.cfg})
        self.assertEqual(plain[0], 0, plain[1])
        self.assertIn('Locale files added, modified or deleted: 318', plain[1])
        self.assertEqual(hostile[1].replace(self.out_b, self.out_a), plain[1])


if __name__ == '__main__':
    unittest.main()
