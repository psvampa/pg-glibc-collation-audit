"""Layer 8: guards against losing tests quietly.

`tests/run_parallel.py` spreads the suite over one process per TestCase class.
Everything it adds over `unittest discover` is a way of losing tests quietly:
a shard that dies before saying anything, a shard that runs fewer tests than
its class holds, a module that does not import, a name that selects nothing, a
shard that hangs, a run of nothing at all -- and, the one that is easiest to
miss, a test whose own output is read as its shard's verdict. Each of those
ends as a green summary over tests nobody ran unless something refuses, which
is this repository's defect class in a new place.

So most tests below revert one guard in shape and assert the red. Some
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
run a real process use a module written into a temporary directory, the
fastest module in the suite, or a one-line `python3 -c`.

The suite also loses something quietly when a test writes where a real audit
reads: `TheSuiteDoesNotWriteWhereARealAuditReads` guards the folder of its
own each test process gets and the runner's check on the default one.
"""
import ast
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import run_parallel as rp
from _harness import (DEFAULT_OUT_DIR, REPO_ROOT, SCRIPTS_DIR, SUITE_OUT_DIR,
                      TESTS_DIR)


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
    """"Could not read this shard" and "this shard ran no tests" are different
    facts. Parsing that returns 0 for an unreadable shard turns a crash into a
    count, and a count is something the rest of the report is willing to reason
    about."""

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

    def test_a_module_run_on_its_own_runs_all_of_it(self):
        """`python3 tests/test_x.py` defines only what sits above its
        `unittest.main()`, runs that, and ends in OK. With the call halfway
        down, the classes below it were skipped in silence. So every module
        ends with exactly that block, and has no other."""
        # From the directory, not from discover_units(): under `-k` the loader
        # it shares is filtered, and this would read only the modules the
        # pattern selects.
        modules = sorted(name[:-3] for name in os.listdir(TESTS_DIR)
                         if name.startswith('test_') and name.endswith('.py'))
        self.assertGreater(len(modules), 1, 'no test modules found at all')
        block = ast.dump(ast.parse(
            "if __name__ == '__main__':\n    unittest.main()\n").body[0])
        for module in modules:
            with self.subTest(module=module):
                with open(os.path.join(TESTS_DIR, module + '.py'),
                          encoding='utf-8') as fh:
                    body = ast.parse(fh.read()).body
                mains = [i for i, node in enumerate(body)
                         if isinstance(node, ast.If) and
                         any(isinstance(n, ast.Name) and n.id == '__name__'
                             for n in ast.walk(node.test))]
                self.assertEqual(mains, [len(body) - 1],
                                 f'{module}.py does not end with its main '
                                 f'block, so a run of that file alone skips '
                                 f'what comes after it')
                self.assertEqual(ast.dump(body[-1]), block,
                                 f'{module}.py ends with a main block that '
                                 f'does not run the whole module')


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



class TheReaderFillSaysWhenItDidNotFinish(unittest.TestCase):
    """run_parallel.fill_reader computes the reader's answers before the
    classes start (backlog 15.2). It changes how long the run takes, never
    an answer, so a fill that fails or runs out of time is said and the
    run goes on; it is never read as done."""

    def harness(self, body):
        d = tempfile.mkdtemp(prefix='pg-glibc-fill-')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, '_harness.py'), 'w') as fh:
            fh.write('import os, sys, time\n'
                     'def fill_reader_cache():\n    ' + body + '\n')
        return d

    def test_a_fill_that_finishes_is_none(self):
        self.assertIsNone(rp.fill_reader(self.harness('pass')))

    def test_a_fill_that_fails_says_how(self):
        why = rp.fill_reader(self.harness(
            "sys.stderr.write('boom\\n'); sys.exit(3)"))
        self.assertEqual('exit 3: boom', why)

    def test_a_fill_that_runs_out_of_time_says_so(self):
        why = rp.fill_reader(self.harness('time.sleep(30)'), timeout=1)
        self.assertEqual('it ran past 1 s', why)

    def test_the_fill_gets_every_processor_unless_told_otherwise(self):
        d = self.harness("print(os.environ['PG_GLIBC_READER_JOBS']); "
                         "sys.exit(1)")
        with mock.patch.dict(os.environ):
            os.environ.pop('PG_GLIBC_READER_JOBS', None)
            self.assertEqual('exit 1: %d' % (os.cpu_count() or 1),
                             rp.fill_reader(d))
            os.environ['PG_GLIBC_READER_JOBS'] = '2'
            self.assertEqual('exit 1: 2', rp.fill_reader(d))

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


class TheSuiteDoesNotWriteWhereARealAuditReads(unittest.TestCase):
    """Backlog 13.37 and 2.5. Tests that launched a step without `out_dir=`
    wrote its lists to the folder a real audit.sh reads them back from
    between its steps; one full run on main, measured on 2026-10-18,
    rewrote 25 files there, the step 2 list of both published pairs among
    them. An audit.sh running at the same time could read the test's list
    instead of its own. `_harness` now gives every test process a folder of
    its own, and the runner fails a run that changes the default one. The
    last tests also hold main()'s call to the reader fill, which that check
    watches: once, before the classes, and said when it did not finish."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pg-glibc-audit-watch-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_test_process_sends_the_steps_to_a_folder_of_its_own(self):
        out = os.environ.get('PG_GLIBC_AUDIT_OUT')
        self.assertEqual(out, SUITE_OUT_DIR)
        self.assertTrue(os.path.isdir(out), out)
        self.assertNotEqual(os.path.realpath(out),
                            os.path.realpath(DEFAULT_OUT_DIR))

    def test_a_folder_inherited_from_the_shell_is_replaced_too(self):
        """Whoever runs the suite may have exported PG_GLIBC_AUDIT_OUT for
        their own audits, so keeping a value already set would send the
        suite back to a real audit's folder."""
        p = subprocess.run(
            [sys.executable, '-c',
             'import _harness, os; print(os.environ["PG_GLIBC_AUDIT_OUT"])'],
            cwd=TESTS_DIR, capture_output=True, text=True,
            env=dict(os.environ, PG_GLIBC_AUDIT_OUT=DEFAULT_OUT_DIR))
        self.assertEqual(p.returncode, 0, p.stderr)
        got = p.stdout.strip()
        self.assertTrue(got, p.stderr)
        self.assertNotEqual(os.path.realpath(got),
                            os.path.realpath(DEFAULT_OUT_DIR))

    def test_a_step_launched_with_no_out_dir_writes_in_that_folder(self):
        """The call site, the way the three writers measured on 2026-10-18
        launched their steps: a plain subprocess that inherits the
        environment, no folder named."""
        name = 'suite-out-dir-probe.%d.txt' % os.getpid()
        p = subprocess.run(
            [sys.executable, '-c',
             'import glibc_locale_data as g; print(g.write_list(%r, ["x"]))'
             % name],
            cwd=SCRIPTS_DIR, capture_output=True, text=True)
        stray = os.path.join(DEFAULT_OUT_DIR, name)
        if os.path.lexists(stray):
            # Only the file this test named, so a red run leaves the folder
            # of a real audit as it found it.
            os.remove(stray)
        self.assertEqual(p.returncode, 0, p.stderr)
        written = p.stdout.strip()
        self.assertEqual(os.path.dirname(written), SUITE_OUT_DIR, written)
        self.assertTrue(os.path.isfile(written), written)

    def test_every_default_in_the_tool_is_the_folder_the_runner_watches(self):
        """The runner watches DEFAULT_OUT_DIR. If the tool's default moved
        and this name did not, the check would watch a folder nothing writes
        to and pass for ever. Every mention of the variable has to be a
        read whose default this test can see, or an export of the folder
        the file settled on; anything else is refused, because a second
        read in a shape the patterns skip would be the one that wins."""
        shapes = (re.compile(r'\$\{PG_GLIBC_AUDIT_OUT:-([^}]*)\}'),
                  re.compile(r"environ\.get\(\s*'PG_GLIBC_AUDIT_OUT',\s*"
                             r"'([^']*)'\s*\)"))
        export = re.compile(r'export PG_GLIBC_AUDIT_OUT="\$OUT_DIR"')
        readers = {}
        candidates = [os.path.join(REPO_ROOT, 'audit.sh')] + [
            os.path.join(SCRIPTS_DIR, n) for n in sorted(os.listdir(SCRIPTS_DIR))
            if n.endswith(('.py', '.sh'))]
        for path in candidates:
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            if 'PG_GLIBC_AUDIT_OUT' not in text:
                continue
            name = os.path.relpath(path, REPO_ROOT)
            reads = [m for shape in shapes for m in shape.finditer(text)]
            known = [m.span() for m in reads + list(export.finditer(text))]
            for use in re.finditer('PG_GLIBC_AUDIT_OUT', text):
                self.assertTrue(
                    any(a <= use.start() < b for a, b in known),
                    '%s:%d mentions PG_GLIBC_AUDIT_OUT in a shape this '
                    'test cannot read'
                    % (name, text.count('\n', 0, use.start()) + 1))
            self.assertTrue(reads, '%s reads PG_GLIBC_AUDIT_OUT and no '
                                   'default could be read from it' % name)
            readers[name] = {m.group(1) for m in reads}
        self.assertEqual(sorted(readers), ['audit.sh',
                                           'scripts/audit-locale-diff.sh',
                                           'scripts/glibc_locale_data.py'])
        for name, defaults in readers.items():
            self.assertEqual(defaults, {DEFAULT_OUT_DIR}, name)
        # The spelling is not the folder: code after the read can still move
        # it. The Python reader is asked for the folder it settled on, with
        # the variable unset (importing it writes nothing), and each shell
        # reader may set OUT_DIR on the read line only.
        env = dict(os.environ)
        env.pop('PG_GLIBC_AUDIT_OUT')
        p = subprocess.run(
            [sys.executable, '-c',
             'import glibc_locale_data as g; print(g.OUT_DIR)'],
            cwd=SCRIPTS_DIR, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), DEFAULT_OUT_DIR)
        for name in ('audit.sh', 'scripts/audit-locale-diff.sh'):
            with open(os.path.join(REPO_ROOT, name), encoding='utf-8') as fh:
                sets = [line for line in fh
                        if re.search(r'\bOUT_DIR=', line)]
            self.assertEqual(len(sets), 1, '%s sets OUT_DIR on %d lines: %s'
                             % (name, len(sets), sets))
            self.assertIn('${PG_GLIBC_AUDIT_OUT:-', sets[0], name)

    def test_a_watched_folder_names_what_was_added_rewritten_or_removed(self):
        keep, gone = (os.path.join(self.tmp, n) for n in ('keep', 'gone'))
        for path in (keep, gone):
            with open(path, 'w') as fh:
                fh.write('x\n')
        before = rp.watch(self.tmp)
        st = os.stat(keep)
        os.utime(keep, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        os.remove(gone)
        with open(os.path.join(self.tmp, 'new'), 'w') as fh:
            fh.write('x\n')
        self.assertEqual(rp.changed(before, rp.watch(self.tmp)),
                         ['.', 'gone', 'keep', 'new'])

    def test_a_file_come_and_gone_behind_a_linked_folder_is_a_change(self):
        target = os.path.join(self.tmp, 'target')
        os.mkdir(target)
        link = os.path.join(self.tmp, 'link')
        os.symlink(target, link)
        before = rp.watch(link)
        st = os.stat(target)
        tmp = os.path.join(target, 'brief')
        with open(tmp, 'w') as fh:
            fh.write('x\n')
        os.remove(tmp)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        self.assertEqual(rp.changed(before, rp.watch(link)), ['.'])

    def test_an_absent_folder_that_appears_is_a_change(self):
        root = os.path.join(self.tmp, 'out')
        before = rp.watch(root)
        self.assertEqual(before, {})
        os.mkdir(root)
        self.assertEqual(rp.changed(before, rp.watch(root)), ['.'])

    def test_a_folder_that_cannot_be_read_raises(self):
        sub = os.path.join(self.tmp, 'sub')
        os.mkdir(sub)
        os.chmod(sub, 0)
        self.addCleanup(os.chmod, sub, 0o700)
        with self.assertRaises(PermissionError):
            rp.watch(self.tmp)

    def test_a_change_is_red_and_names_the_path_and_both_causes(self):
        code, lines = rp.aggregate([shard('a', ran=4)], {'a': 4}, 1.0, 2,
                                   touched=['step2_changed_collate.x..y.txt'])
        text = '\n'.join(lines)
        self.assertEqual(code, 1, text)
        self.assertIn('    step2_changed_collate.x..y.txt', text)
        self.assertIn('either a test wrote there or an audit.sh ran at the '
                      'same time', ' '.join(text.split()))
        self.assertRegex(text.split('\n')[-1],
                         r'^FAILED \(default output folder changed=1\)$')

    def test_a_folder_that_could_not_be_read_is_red(self):
        code, lines = rp.aggregate([shard('a', ran=4)], {'a': 4}, 1.0, 2,
                                   unwatched='before the run: denied')
        text = '\n'.join(lines)
        self.assertEqual(code, 1, text)
        self.assertIn('before the run: denied', text)
        self.assertRegex(text.split('\n')[-1],
                         r'^FAILED \(default output folder unreadable=1\)$')

    def _main(self, run_unit, watch=rp.watch, folder=None, fill=lambda: None,
              units=(('k', 1),)):
        # main([]) is a run of the whole suite, so it fills the reader's
        # answers first; the real fill_reader computes them for the suite,
        # work no test here is about. main() also sets PG_GLIBC_READER_JOBS
        # for its shards, and the tests after these must not inherit it.
        out = io.StringIO()
        with mock.patch.object(rp, 'DEFAULT_OUT_DIR', folder or self.tmp), \
                mock.patch.object(rp, 'discover_units',
                                  lambda names=None: list(units)), \
                mock.patch.object(rp, 'run_unit', run_unit), \
                mock.patch.object(rp, 'watch', watch), \
                mock.patch.object(rp, 'fill_reader', fill), \
                mock.patch.dict(os.environ), \
                contextlib.redirect_stdout(out):
            code = rp.main([])
        return code, out.getvalue()

    def test_main_fails_a_run_whose_shard_wrote_in_the_watched_folder(self):
        def writes(name):
            with open(os.path.join(self.tmp, 'step2_list.txt'), 'w') as fh:
                fh.write('x\n')
            return shard(name)
        code, text = self._main(writes)
        self.assertEqual(code, 1, text)
        self.assertIn('    step2_list.txt', text)
        self.assertRegex(text, r'(?m)^FAILED \(default output folder '
                               r'changed=2\)$')

    def test_main_fails_a_run_that_created_the_folder(self):
        """The case every CI run is: a fresh runner has no default folder
        before the suite, so "before" is {} and still has to be compared."""
        folder = os.path.join(self.tmp, 'out')

        def creates(name):
            os.mkdir(folder)
            with open(os.path.join(folder, 'step2_list.txt'), 'w') as fh:
                fh.write('x\n')
            return shard(name)
        code, text = self._main(creates, folder=folder)
        self.assertEqual(code, 1, text)
        self.assertIn('    step2_list.txt', text)
        self.assertRegex(text, r'(?m)^FAILED \(default output folder '
                               r'changed=2\)$')

    def test_main_fails_a_run_when_the_folder_cannot_be_read(self):
        def denied(root):
            raise PermissionError(13, 'denied', root)
        code, text = self._main(shard, watch=denied)
        self.assertEqual(code, 1, text)
        self.assertIn('before the run:', text)
        self.assertIn('after the run:', text)
        self.assertRegex(text, r'(?m)^FAILED \(default output folder '
                               r'unreadable=1\)$')

    def test_main_fails_a_run_when_the_folder_turns_unreadable(self):
        calls, real_watch = [], rp.watch

        def readable_once(root):
            calls.append(root)
            if len(calls) > 1:
                raise PermissionError(13, 'denied', root)
            return real_watch(root)
        code, text = self._main(shard, watch=readable_once)
        self.assertEqual(code, 1, text)
        self.assertNotIn('before the run:', text)
        self.assertIn('after the run:', text)
        self.assertRegex(text, r'(?m)^FAILED \(default output folder '
                               r'unreadable=1\)$')

    def test_main_passes_a_run_that_left_the_folder_alone(self):
        """The control for the three above: a runner that failed every run
        would pass all of them."""
        code, text = self._main(shard)
        self.assertEqual(code, 0, text)
        self.assertEqual(text.strip().split('\n')[-1], 'OK')

    def test_main_fills_the_reader_once_before_the_classes(self):
        """Two classes, so once per run and once per class differ; and a fill
        that takes a moment, so one started beside the classes ends after
        one of them has begun."""
        order = []

        def fill():
            order.append('fill')
            time.sleep(0.05)
            order.append('filled')

        def runs(name):
            order.append(name)
            return shard(name)
        code, text = self._main(runs, fill=fill, units=(('a', 1), ('b', 1)))
        self.assertEqual(code, 0, text)
        self.assertEqual(order[:2], ['fill', 'filled'], order)
        self.assertEqual(sorted(order[2:]), ['a', 'b'], order)

    def test_main_says_so_when_the_fill_did_not_finish_and_runs_on(self):
        """The fill changes how long the run takes, never an answer, so a
        fill that failed is said above the verdict and the run goes on."""
        code, text = self._main(shard, fill=lambda: 'exit 3: boom')
        self.assertEqual(code, 0, text)
        self.assertIn("note: the reader's answers were not filled before the "
                      "classes started (exit 3: boom); each class computes "
                      "what it needs.", ' '.join(text.split()))
        self.assertEqual(text.strip().split('\n')[-1], 'OK')

    def test_main_fails_a_run_whose_fill_wrote_in_the_watched_folder(self):
        """The folder's first snapshot is taken before the fill, so the fill
        is watched like the classes are."""
        def fill():
            with open(os.path.join(self.tmp, 'filled.txt'), 'w') as fh:
                fh.write('x\n')
        code, text = self._main(shard, fill=fill)
        self.assertEqual(code, 1, text)
        self.assertIn('    filled.txt', text)
        self.assertRegex(text, r'(?m)^FAILED \(default output folder '
                               r'changed=2\)$')

    def test_a_fill_that_failed_does_not_stop_the_watch(self):
        """Both of the fill's signals in one run: the note, and the red of
        what it wrote before it failed."""
        def fill():
            with open(os.path.join(self.tmp, 'filled.txt'), 'w') as fh:
                fh.write('x\n')
            return 'exit 3: boom'
        code, text = self._main(shard, fill=fill)
        self.assertEqual(code, 1, text)
        self.assertIn('(exit 3: boom)', text)
        self.assertIn('    filled.txt', text)
        self.assertRegex(text, r'(?m)^FAILED \(default output folder '
                               r'changed=2\)$')


class AGreenRunIsStillGreen(unittest.TestCase):
    """The control. Most tests above assert a red, so a runner that reported
    red on everything would pass almost all of them and be useless. Counted
    as a description rather than as a number, because a number here goes
    stale on the next class added -- which it already did, at "eight"."""

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
