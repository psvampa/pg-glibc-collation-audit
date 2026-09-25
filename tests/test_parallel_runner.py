"""Layer 8: the parallel runner's own guards.

`tests/run_parallel.py` spreads the suite over one process per TestCase class.
Everything it adds over `unittest discover` is a way of losing tests quietly:
a shard that dies before saying anything, a shard that runs fewer tests than
its class holds, a module that does not import, a name that selects nothing, a
shard that hangs, a run of nothing at all -- and, the one that is easiest to
miss, a test whose own output is read as its shard's verdict. Each of those
ends as a green summary over tests nobody ran unless something refuses, which
is this repository's defect class in a new place.

So each test below reverts one guard in shape and asserts the red. Three
classes assert a green instead -- a skip that must be reported, an end-to-end
run that must pass, and the control at the bottom -- because a runner that
failed on everything would satisfy all the others and guard nothing.

Six of the guards here were written after `false-negative-reviewer` measured
them missing on a version of this file that had passed its own tests and the
whole suite: the per-class counts, the verdict read from the stream `unittest`
owns rather than from everything the shard printed, the refusal to report a
green run of nothing, the `skipped=` a red run used to drop, a class no shard
reported on, and a class two shards reported on. The seventh pass measured
that the stream separation -- advertised in the runner's docstring -- had no
test that reddened when it was reverted; `test_a_shard_that_dies_before_its
_summary_is_red` is that test.

No glibc clone needed: the shard results are fabricated, and the cases that
run a real process use a module written into a temporary directory or the
fastest module in the suite.
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

import run_parallel as rp
from _harness import TESTS_DIR


def shard(name, ran=1, status='OK', extra='', rc=0, body='', stdout=''):
    """A shard result shaped the way a real one comes back.

    `body` and the summary go where `unittest` puts them, on stderr; `stdout`
    is what the tests themselves printed, which the verdict must ignore.
    """
    report = '%s%s\nRan %d tests in 0.5s\n\n%s%s\n' % (
        body, '-' * 70, ran, status, (' (%s)' % extra) if extra else '')
    return rp.Shard(name, rc, 0.5, stdout + report, report)


def report(shards, expected):
    code, lines = rp.aggregate(shards, expected, 1.0, 2)
    return code, '\n'.join(lines)


class ALostShardIsNotAPass(unittest.TestCase):
    """The failure a parallel runner has and a serial one cannot: a class
    that ran less than it holds, in a run where every shard that did run
    passed. Without the counts, that prints OK."""

    def test_a_class_that_ran_fewer_tests_than_it_holds_is_red(self):
        code, text = report([shard('a', ran=4), shard('b', ran=4)],
                            {'a': 5, 'b': 4})
        self.assertEqual(code, 1, text)
        self.assertIn('discovery counted 5 tests in it and it reported 4',
                      text)
        self.assertIn('unaccounted=1', text)

    def test_a_loss_in_one_class_hidden_by_a_gain_in_another_is_red(self):
        """Why the counts are held per class and not summed: 4 + 4 is 8 either
        way, so the total says everything ran. Measured as a hole in the first
        version of this runner, by false-negative-reviewer."""
        code, text = report([shard('a', ran=4), shard('b', ran=4)],
                            {'a': 5, 'b': 3})
        self.assertEqual(code, 1, text)
        self.assertIn('a: discovery counted 5 tests in it and it reported 4',
                      text)
        self.assertNotIn('unaccounted', text)

    def test_a_class_discovery_never_counted_is_red(self):
        """The mirror: a shard nobody asked for means the list that was run
        and the list that was counted are not the same list."""
        code, text = report([shard('a', ran=4), shard('c', ran=2)], {'a': 4})
        self.assertEqual(code, 1, text)
        self.assertIn('c: ran 2 tests in a class discovery never counted',
                      text)

    def test_a_class_no_shard_reported_on_is_red_and_named(self):
        """The symmetric side of the uncounted class, and the one the sum
        cannot reach on its own: two shards for the same class make the total
        agree while a third class was never run."""
        code, text = report([shard('a', ran=4), shard('a', ran=4)],
                            {'a': 4, 'b': 4})
        self.assertEqual(code, 1, text)
        self.assertIn('b: discovery counted 4 tests in it and no shard '
                      'reported on it at all', text)
        self.assertIn('a: reported on by more than one shard', text)

    def test_a_run_that_expected_nothing_keeps_the_shards_failures(self):
        """A red for an unreadable reason is the defect class one direction
        over: this used to return early with "nothing ran" and throw away two
        real failures and their output."""
        code, text = report([shard('a', ran=4, status='FAILED',
                                   extra='failures=2', rc=1,
                                   body='AssertionError: 3 != 4\n')], {})
        self.assertEqual(code, 1, text)
        self.assertIn('AssertionError: 3 != 4', text)
        self.assertIn('failures=2', text)

    def test_more_tests_than_discovery_counted_is_red_too(self):
        """A count that is merely different from the expected one is a count
        nobody can act on."""
        code, text = report([shard('a', ran=9)], {'a': 8})
        self.assertEqual(code, 1, text)
        self.assertIn('unaccounted=-1', text)

    def test_the_status_line_stays_greppable_when_it_is_the_only_problem(self):
        """.claude/hooks/git-gate.sh reads the push/PR verdict off lines
        matching ^(Ran |OK|FAILED). A red that does not start a line with
        FAILED is a red the gate reports as a pass."""
        _, text = report([shard('a', ran=4)], {'a': 10})
        self.assertRegex(text, r'(?m)^FAILED \(')
        self.assertNotRegex(text, r'(?m)^OK')


class AShardCannotWriteItsOwnVerdict(unittest.TestCase):
    """`unittest` writes its summary to stderr; the tests write to stdout.
    Parsing both, and parsing the count and the status independently, let a
    test supply its shard's result -- measured on the first version of this
    runner: a class printing `Ran 99 tests` and `OK` reported 99 tests, and a
    shard whose real tail was `OK (skipped=1)` reported a clean OK with the
    skip gone. A skip is never a pass; nothing else
    reads this runner's output, so the printed `skipped=` is the only guard
    there is."""

    HIJACK = 'Ran 99 tests in 0.1s\n\nOK\n'

    def test_a_summary_printed_by_a_test_is_not_the_verdict(self):
        one = shard('a', ran=2, extra='skipped=1', stdout=self.HIJACK)
        self.assertEqual(rp.parse_shard(one.report), (2, 'OK', {'skipped': 1}))
        code, text = report([one], {'a': 2})
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r'(?m)^OK \(skipped=1\)$')
        self.assertRegex(text, r'(?m)^Ran 2 tests in ')

    def test_a_shard_that_printed_only_a_fake_summary_is_red(self):
        """The case the stream separation is load-bearing for: the shard died
        before `unittest` summarised anything, and the only summary-shaped
        text in the process's output is the one a test wrote. Read from
        everything the shard printed, that is a green shard of 99 tests."""
        one = rp.Shard('a', 0, 0.1, self.HIJACK, '')
        self.assertIsNone(rp.parse_shard(one.report))
        code, text = report([one], {'a': 2})
        self.assertEqual(code, 1, text)
        self.assertIn('without reporting how many tests it ran', text)

    def test_a_shard_that_dies_before_its_summary_is_red(self):
        """The case the stream separation is load-bearing for, through a real
        process: the class prints a summary-shaped block and then calls
        `os._exit(0)`, so `unittest` never writes one. Read from everything
        the shard printed, that is a green shard of 99 tests -- and reverting
        the separation used to leave this whole layer green, which is how a
        guard the runner's own docstring advertises came to be unmeasured
        (found by false-negative-reviewer on the fix, not by its author)."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'test_dies.py'), 'w') as fh:
                fh.write(textwrap.dedent('''
                    import os
                    import sys
                    import unittest

                    class K(unittest.TestCase):
                        def test_a_prints_and_exits(self):
                            print("Ran 2 tests in 0.1s")
                            print()
                            print("OK")
                            sys.stdout.flush()
                            os._exit(0)

                        def test_b_never_runs(self):
                            pass
                '''))
            result = rp.run_unit('test_dies.K', tests_dir=tmp)
        self.assertIn('Ran 2 tests', result.output)
        self.assertIsNone(rp.parse_shard(result.report))
        code, text = report([result._replace(name='test_dies.K')],
                            {'test_dies.K': 2})
        self.assertEqual(code, 1, text)
        self.assertIn('without reporting how many tests it ran', text)

    def test_a_status_line_not_under_its_own_count_is_not_a_verdict(self):
        """The count and the status are one pattern, so a status that belongs
        to something else cannot be paired with a count that belongs to the
        run."""
        self.assertIsNone(rp.parse_shard(
            'Ran 3 tests in 0.1s\n\nsomething else happened\n\nOK\n'))

    def test_the_last_summary_in_the_stream_is_the_run(self):
        self.assertEqual(
            rp.parse_shard('Ran 9 tests in 1s\n\nOK\n'
                           'Ran 2 tests in 1s\n\nFAILED (failures=1)\n'),
            (2, 'FAILED', {'failures': 1}))

    def test_a_real_process_printing_a_summary_does_not_move_the_count(self):
        """The fabricated shards above cannot catch a runner that reads the
        wrong stream off a real process, because they never run one."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'test_noisy.py'), 'w') as fh:
                fh.write(textwrap.dedent('''
                    import unittest

                    class K(unittest.TestCase):
                        def test_prints_a_summary(self):
                            print("Ran 99 tests in 0.1s")
                            print()
                            print("OK")

                        @unittest.skip('deliberate')
                        def test_skipped(self):
                            pass
                '''))
            result = rp.run_unit('test_noisy.K', tests_dir=tmp)
        self.assertEqual(result.returncode, 0, result.output)
        self.assertIn('Ran 99 tests', result.output)
        self.assertEqual(rp.parse_shard(result.report),
                         (2, 'OK', {'skipped': 1}))
        code, text = report([result._replace(name='test_noisy.K')],
                            {'test_noisy.K': 2})
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r'(?m)^OK \(skipped=1\)$')


class AShardThatSaidNothingIsNotZeroTests(unittest.TestCase):
    """"Could not read this shard" and "this shard ran no tests" are
    different facts (detection-code invariants, section A). Parsing that
    returns 0 for an unreadable shard turns a crash into a count, and a count
    is something the rest of the report is willing to reason about."""

    def test_an_empty_shard_output_is_red_for_the_right_reason(self):
        code, text = report([rp.Shard('a', 0, 0.1, '', '')], {'a': 3})
        self.assertEqual(code, 1, text)
        self.assertIn('a: exited 0 without reporting how many tests it ran',
                      text)

    def test_a_ran_line_with_no_status_under_it_is_red(self):
        """A shard killed between its count and its verdict."""
        broken = rp.Shard('a', 0, 0.1, 'Ran 3 tests in 0.1s\n',
                          'Ran 3 tests in 0.1s\n')
        code, text = report([broken], {'a': 3})
        self.assertEqual(code, 1, text)
        self.assertIsNone(rp.parse_shard(broken.report))

    def test_a_status_line_with_no_count_over_it_is_red(self):
        self.assertIsNone(rp.parse_shard('OK\n'))


class ARunOfNothingIsNotAPass(unittest.TestCase):
    """`discover_units` and `select_units` both refuse an empty selection, and
    the reporter used to trust them: `aggregate([], 0, ...)` returned OK. A
    verdict computed upstream and not re-checked where it is printed means the
    refusal lives only as long as both of those guards do."""

    def test_no_shards_at_all_is_red(self):
        code, text = report([], {})
        self.assertEqual(code, 1, text)
        self.assertIn('no shard ran at all', text)
        self.assertRegex(text, r'(?m)^FAILED \(')

    def test_shards_with_nothing_to_run_is_red(self):
        code, text = report([shard('a', ran=0)], {'a': 0})
        self.assertEqual(code, 1, text)
        self.assertIn('nothing was expected to run', text)


class ANonZeroShardIsRedWhateverItPrinted(unittest.TestCase):
    """Exit status and printed verdict are two claims, and a shard that
    crashes after printing OK -- an error in tearDownModule, a segfault in a
    subprocess it spawned -- makes them disagree. The reassuring one is not
    the one to believe."""

    def test_exit_one_under_an_ok_line_is_red(self):
        code, text = report([shard('a', ran=4, rc=1)], {'a': 4})
        self.assertEqual(code, 1, text)
        self.assertIn('exit 1', text)

    def test_a_failed_shard_is_red_and_counted(self):
        code, text = report([shard('a', ran=4, status='FAILED',
                                   extra='failures=2', rc=1)], {'a': 4})
        self.assertEqual(code, 1, text)
        self.assertIn('failures=2', text)

    def test_a_red_run_still_says_how_many_it_skipped(self):
        """Failing and skipping are different facts and a reader greps for
        the second one: it says which layers never ran at all."""
        _, text = report([shard('a', ran=4, status='FAILED',
                                extra='failures=1, skipped=2', rc=1)],
                         {'a': 4})
        self.assertRegex(text, r'(?m)^FAILED \(.*skipped=2')


class AFailedShardCarriesItsOutput(unittest.TestCase):
    """A red that does not say what failed sends the reader back to run the
    whole suite serially to find out, which is the time this runner exists to
    save."""

    def test_the_traceback_reaches_the_report(self):
        body = 'FAIL: test_x (test_mod.Klass)\nAssertionError: 3 != 4\n'
        code, text = report([shard('test_mod.Klass', ran=4, status='FAILED',
                                   extra='failures=1', rc=1, body=body)],
                            {'test_mod.Klass': 4})
        self.assertEqual(code, 1, text)
        self.assertIn('AssertionError: 3 != 4', text)
        self.assertIn('test_mod.Klass', text)

    def test_a_long_shard_output_says_how_much_it_cut(self):
        body = ''.join('line %d\n' % i for i in range(200))
        _, text = report([shard('a', ran=1, status='FAILED', extra='errors=1',
                                rc=1, body=body)], {'a': 1})
        self.assertIn('line 199', text)
        self.assertRegex(text,
                         r'\[\d+ earlier lines of this shard not shown\]')


class ASkipReachesTheStatusLine(unittest.TestCase):
    """CI and the gate both fail on `skipped=`, because a skip is never a
    pass. They read it off the status line, so a runner that counts skips
    and does not print them there turns every skipped layer into a pass."""

    def test_the_status_line_carries_the_skip_count(self):
        code, text = report([shard('a', ran=4, extra='skipped=2'),
                             shard('b', ran=3)], {'a': 4, 'b': 3})
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r'(?m)^OK \(skipped=2\)$')

    def test_which_class_skipped_is_named_not_only_how_many(self):
        """A count says something was not covered; only the name says what."""
        _, text = report([shard('test_mod.Klass', ran=4, extra='skipped=2')],
                         {'test_mod.Klass': 4})
        self.assertIn('skipped: test_mod.Klass (2 of its 4)', text)


class DiscoveryIsTheNumberEverythingElseIsComparedTo(unittest.TestCase):
    """If the parent's expected counts are short, every shard total matches a
    number that is already wrong and the count guard passes over the loss."""

    def test_it_counts_what_unittest_discover_counts(self):
        loaded = unittest.defaultTestLoader.discover(
            TESTS_DIR, top_level_dir=TESTS_DIR).countTestCases()
        self.assertEqual(sum(n for _, n in rp.discover_units()), loaded)

    def test_a_module_that_does_not_import_is_not_a_pass(self):
        """unittest turns an import error into a _FailedTest placeholder whose
        name cannot be run by this runner. Counted as an ordinary class, it is
        one shard that always fails; ignored, it is a module silently dropped
        from a green run."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'test_broken.py'), 'w') as fh:
                fh.write('import a_module_that_is_not_installed\n')
            with self.assertRaises(rp.DiscoveryError) as caught:
                rp.discover_units(tests_dir=tmp)
        self.assertIn('did not import', str(caught.exception))

    def test_discovering_nothing_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rp.DiscoveryError) as caught:
                rp.discover_units(tests_dir=tmp)
        self.assertIn('no test class', str(caught.exception))

    def test_a_start_directory_that_does_not_import_refuses_in_kind(self):
        """unittest raises ImportError for this one. Every other refusal here
        prints a `FAILED (...)` line, and one that leaves a traceback instead
        is one a reader -- or a hook grepping for it -- cannot classify."""
        with self.assertRaises(rp.DiscoveryError) as caught:
            rp.discover_units(tests_dir='/no/such/directory/4c1f')
        self.assertIn('could not discover', str(caught.exception))


class ASelectionThatMatchesNothingIsNotAPass(unittest.TestCase):
    """`run_parallel.py test_wrappre` is a typo, and the honest answer to it
    is not a green run of the other classes."""

    UNITS = [('test_mod.Klass', 3), ('test_other.Other', 2)]

    def test_an_unmatched_name_raises(self):
        with self.assertRaises(rp.DiscoveryError) as caught:
            rp.select_units(self.UNITS, ['test_nope'])
        self.assertIn('matches no test class', str(caught.exception))

    def test_a_module_name_selects_its_classes(self):
        self.assertEqual(rp.select_units(self.UNITS, ['test_mod']),
                         [('test_mod.Klass', 3)])

    def test_a_class_named_twice_is_run_once(self):
        picked = rp.select_units(self.UNITS, ['test_mod', 'test_mod.Klass'])
        self.assertEqual(picked, [('test_mod.Klass', 3)])


class AHungShardIsKilledAndCounted(unittest.TestCase):
    """A shard waited on for ever is a suite that never reports. The kill has
    to leave a result the aggregate can see, not a silence."""

    def test_the_timeout_returns_a_red_shard_that_names_itself(self):
        result = rp.run_unit('test_pure_functions', timeout=0.001)
        self.assertEqual(result.returncode, 124)
        self.assertIn('shard timeout', result.output)
        code, text = report([result], {'test_pure_functions': 1})
        self.assertEqual(code, 1, text)


class TheRunnerRunsWhatItSaysItRan(unittest.TestCase):
    """End to end against the serial command on the same module: same tests,
    same count, both green. The fabricated shards cannot catch a runner that
    shells out wrongly -- a bad cwd, a lost PYTHONPATH -- because they never
    shell out."""

    @staticmethod
    def _ran(output):
        found = re.search(r'^Ran (\d+) tests? in ', output, re.M)
        assert found, 'no count in:\n%s' % output
        return int(found.group(1))

    def test_one_module_in_parallel_matches_the_same_module_in_series(self):
        serial = subprocess.run(
            [sys.executable, '-m', 'unittest', '-q', 'test_pure_functions'],
            cwd=TESTS_DIR, capture_output=True, text=True)
        parallel = subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, 'run_parallel.py'),
             '-j', '4', 'test_pure_functions'],
            cwd=TESTS_DIR, capture_output=True, text=True)
        self.assertEqual(serial.returncode, 0, serial.stdout + serial.stderr)
        self.assertEqual(parallel.returncode, 0,
                         parallel.stdout + parallel.stderr)
        self.assertEqual(self._ran(parallel.stdout),
                         self._ran(serial.stdout + serial.stderr))
        self.assertRegex(parallel.stdout, r'(?m)^OK$')

    def test_a_named_class_that_does_not_exist_exits_non_zero(self):
        p = subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, 'run_parallel.py'),
             'test_pure_functions.NoSuchClass'],
            cwd=TESTS_DIR, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertRegex(p.stdout, r'(?m)^FAILED \(')


class AGreenRunIsStillGreen(unittest.TestCase):
    """The control. Every class above except the skip report and the two
    end-to-end runs asserts a red, so a runner that reported red on
    everything would pass almost all of them and be useless. Counted as a
    description rather than as a number, because a number here goes stale on
    the next class added -- which it already did, at "eight"."""

    def test_shards_that_all_passed_report_ok_and_nothing_else(self):
        code, text = report([shard('a', ran=4), shard('b', ran=6)],
                            {'a': 4, 'b': 6})
        self.assertEqual(code, 0, text)
        self.assertEqual(text.strip().split('\n')[-1], 'OK')
        self.assertNotIn('---', text)
        self.assertRegex(text, r'(?m)^Ran 10 tests in 1\.0s \(2 workers, '
                               r'2 classes\)$')


if __name__ == '__main__':
    unittest.main()
