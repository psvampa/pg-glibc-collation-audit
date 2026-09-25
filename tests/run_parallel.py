#!/usr/bin/env python3
"""The suite `unittest discover -s tests -t tests` runs, spread over processes.

The same tests out of the same files, one process per TestCase class instead
of one process for all of them. Measured on 2026-09-21, 14-core darwin:
208 s serial, 47 s here.

Why by class and not by test method, also measured that day: by class over 8
workers, 47 s; by method, 68 s -- every extra process pays the interpreter
start again and loses the `run_step` memo in `_harness.py`, which the tests of
one class share. Over 14 workers, 46 s with every shard slowing down. Eight is
the knee, so that is the default.

The counts are the whole point. A parallel runner that loses a shard prints a
green summary over tests nobody ran, which is this repository's defect class
exactly (`references/detection-code-invariants.md`, section A: a clean result
where "could not look" and "nothing there" are different facts). So it is red
whenever it cannot show that the suite ran:

  * discovery counts every class separately and each shard is held to ITS
    number, not to the total: a class that ran fewer tests than it holds is
    FAILED and named, which a sum cannot do, because another shard reporting
    more makes the total agree;
  * the total is checked as well, so a shard that reported nothing is FAILED;
  * a shard that exits 0 without saying how many tests it ran is FAILED;
  * a shard that exits non-zero is FAILED even when the counts add up;
  * the verdict is read from the stream `unittest` writes it to, so what the
    tests print to stdout cannot supply it, and what they print to stderr
    cannot outrank the summary that ends the run, which is why the last match
    wins -- measured: 2 of 37 shards carry test-written text on stderr;
  * a class discovery counted that no shard reported on, and a class reported
    on twice, are both FAILED;
  * a run with nothing in it is FAILED where the verdict is printed, not only
    where the list is built;
  * discovering no test class at all, or a start directory that does not
    import, is FAILED;
  * a shard that never finishes is FAILED, by name, at SHARD_TIMEOUT;
  * a class named on the command line that matches nothing is FAILED, rather
    than a green run of the empty selection.

The last two lines are the ones `unittest` prints -- `Ran N tests in Ts`, then
`OK`, `OK (skipped=N)` or `FAILED (...)` -- because `.claude/hooks/git-gate.sh`
greps for exactly those and treats `skipped=` as a red, because a skip is
never a pass. Which classes skipped is printed above them,
so a skip is never just a number either.

`.github/workflows/tests.yml` runs the serial command on every pull request
and every push to `main`, on purpose: this runner is not the last word on a
change that gets merged. (It said "never the only thing that has run the
suite" until the CI triggers stopped firing on a branch with no pull request
open.)

Usage:

    python3 tests/run_parallel.py                 # the whole suite
    python3 tests/run_parallel.py -j 4            # four processes
    python3 tests/run_parallel.py test_wrapper    # one module, or one class
"""
import argparse
import collections
import os
import re
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Measured, not chosen: see the module docstring.
DEFAULT_WORKERS = 8

# The slowest class takes about 35 s. A shard past this is hung, not slow, and
# a hung shard that is waited on for ever is a suite that never says anything.
SHARD_TIMEOUT = 600

# `unittest` writes its summary to stderr, and the two lines of it are
# adjacent. Both halves of that matter, and both were measured on a runner
# that had neither: a test that prints `Ran 99 tests in 0.1s` and `OK` on
# stdout was read as its own shard's verdict, and a shard whose real tail said
# `OK (skipped=1)` reported a clean OK with the skip gone -- "a skip is never a
# pass" defeated by one line of test output. So the count
# and the status are matched as one pattern, in the stream unittest owns, and
# the LAST one wins: the summary of the run is the one that ends it.
_SUMMARY = re.compile(
    r'^Ran (\d+) tests? in [^\n]*\n\s*\n(OK|FAILED)(?: \(([^)]*)\))?[^\S\n]*$',
    re.M)
_COUNT = re.compile(r'([a-z][a-z ]*[a-z])=(\d+)')

# `output` is everything the shard printed, for the reader. `report` is the
# stream `unittest` writes its own summary to, and the only one parsed.
Shard = collections.namedtuple('Shard', 'name returncode seconds output report')


class DiscoveryError(Exception):
    """Discovery could not produce a list of classes to run.

    Its own exception type because the answer to it is never "run what was
    found": an import error or an empty discovery means the parent's expected
    count is not the suite's size, and every count below it would then be
    compared against a number that is already wrong.
    """


def discover_units(tests_dir=TESTS_DIR, names=None):
    """[(dotted class name, tests in it)], biggest first.

    Raises DiscoveryError when a module failed to import. unittest turns that
    into a `_FailedTest` placeholder whose failure is only raised when it is
    run by name, and its name is not importable, so this runner cannot run it:
    the alternative to refusing here is a summary that silently covers one
    module less than the suite has.

    An unimportable start directory raises the same way rather than reaching
    the caller as an ImportError traceback: every other refusal in this file
    prints a `FAILED (...)` line, and one that does not is one the reader --
    or a hook grepping for it -- cannot classify.
    """
    try:
        suite = unittest.defaultTestLoader.discover(tests_dir,
                                                    top_level_dir=tests_dir)
    except ImportError as exc:
        raise DiscoveryError('could not discover tests under %s: %s'
                             % (tests_dir, exc)) from exc
    counts = collections.Counter()
    broken = []
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(item)
            continue
        cls = item.__class__
        if cls.__name__ == '_FailedTest':
            broken.append('%s: %s' % (
                getattr(item, '_testMethodName', item.id()),
                getattr(item, '_exception', 'import failed')))
            continue
        counts['%s.%s' % (cls.__module__, cls.__qualname__)] += 1
    if broken:
        raise DiscoveryError('a test module did not import, so the suite is '
                             'not the size discovery reports: '
                             + '; '.join(broken))
    if not counts:
        raise DiscoveryError('discovered no test class under %s; an empty '
                             'selection is not a pass' % tests_dir)
    units = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return select_units(units, names) if names else units


def select_units(units, names):
    """The units whose dotted name is, or lives under, one of `names`.

    A name that matches nothing raises. Running the rest and reporting OK
    would answer a question nobody asked -- the caller named a class because
    that is the class they wanted covered.
    """
    chosen, seen = [], set()
    for name in names:
        hits = [u for u in units
                if u[0] == name or u[0].startswith(name + '.')]
        if not hits:
            raise DiscoveryError(
                '%r matches no test class; discovery found %d of them'
                % (name, len(units)))
        for unit in hits:
            if unit[0] not in seen:
                seen.add(unit[0])
                chosen.append(unit)
    return chosen


def run_unit(name, tests_dir=TESTS_DIR, timeout=SHARD_TIMEOUT):
    """One class in its own `python3 -m unittest -q` process.

    The two streams are kept apart. What the tests themselves print goes to
    stdout and is shown to the reader on a red; `unittest`'s own summary goes
    to stderr, and that is the only stream the verdict is read from.
    """
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        [tests_dir] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
    started = time.time()
    note = ''
    try:
        p = subprocess.run([sys.executable, '-m', 'unittest', '-q', name],
                           cwd=tests_dir, env=env, capture_output=True,
                           timeout=timeout)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        out, err = exc.stdout or b'', exc.stderr or b''
        note = '\nran past the %d s shard timeout and was killed\n' % timeout
        rc = 124
    out = out.decode('utf-8', 'replace')
    err = err.decode('utf-8', 'replace') + note
    return Shard(name, rc, time.time() - started, out + err, err)


def parse_shard(report):
    """(tests run, 'OK'|'FAILED', {failures: n, ...}), or None.

    `report` is the shard's stderr, not everything it printed, and the count
    and the status are matched as one adjacent pair rather than looked up
    separately. Both were measured as holes in the first version of this
    runner: a test printing summary-shaped lines on stdout supplied its own
    shard's verdict, and a shard whose real tail was `OK (skipped=1)` reported
    a clean `OK`. Of several summaries, the last one is the run's.

    None means the shard did not report a run at all. That is not zero tests;
    it is a shard whose result cannot be read, and the caller turns it into a
    red.
    """
    last = None
    for last in _SUMMARY.finditer(report):
        pass
    if last is None:
        return None
    counts = {k: int(v) for k, v in _COUNT.findall(last.group(3) or '')}
    return int(last.group(1)), last.group(2), counts


def aggregate(shards, expected, seconds, workers, tail=80):
    """(exit code, report lines) for a finished run.

    `expected` is {class name: tests discovery counted in it}, per class and
    not a total, because a sum hides a swap: one class running fewer tests
    than it holds is invisible the moment another reports more, and even
    alone a total can only say that one test is missing, never which class
    lost it. Discovery already produced the per-class number.

    Every way this can be wrong is a red, and each one names itself: an
    unreadable shard, a non-zero shard, a FAILED shard, a shard that ran a
    different number of tests than the class holds, a class discovery never
    counted, and a run of nothing at all -- which reaches here as an empty
    list and used to return OK, leaving the refusal to two guards upstream
    that nothing re-checked where the verdict is printed.
    """
    lines, problems = [], []
    totals = collections.Counter()
    ran = 0
    total = sum(expected.values())
    if not shards:
        return 1, ['Ran 0 tests in %.1fs (%d workers, 0 classes)'
                   % (seconds, workers),
                   'FAILED (no shard ran at all, and that is not a pass)']
    if total <= 0:
        # Additive, not an early return: the first version returned here and
        # threw away the shards' own failures, printing "nothing ran" over a
        # report that had two. Red for an unreadable reason is still the
        # defect class, one direction over.
        lines.append('--- nothing was expected to run, so no count below can '
                     'be checked against anything')
        problems.append((shards[0], 'discovery expected no tests at all'))
    missing = sorted(set(expected) - {shard.name for shard in shards})
    for name in missing:
        lines.append('--- %s: discovery counted %d tests in it and no shard '
                     'reported on it at all' % (name, expected[name]))
    repeated = [name for name, n in
                collections.Counter(s.name for s in shards).items() if n > 1]
    for name in sorted(repeated):
        lines.append('--- %s: reported on by more than one shard, so its '
                     'count cannot be checked' % name)
    for shard in shards:
        parsed = parse_shard(shard.report)
        if parsed is None:
            problems.append((shard, 'exited %d without reporting how many '
                                    'tests it ran' % shard.returncode))
            continue
        count, status, counts = parsed
        ran += count
        totals.update(counts)
        if shard.name not in expected:
            problems.append((shard, 'ran %d tests in a class discovery never '
                                    'counted' % count))
        elif count != expected[shard.name]:
            problems.append((shard, 'discovery counted %d tests in it and it '
                                    'reported %d'
                                    % (expected[shard.name], count)))
        if shard.returncode != 0 or status != 'OK':
            problems.append((shard, '%s (exit %d)' % (status,
                                                      shard.returncode)))
        if counts.get('skipped'):
            lines.append('  skipped: %s (%d of its %d)'
                         % (shard.name, counts['skipped'], count))
    for shard, why in problems:
        lines.append('--- %s: %s' % (shard.name, why))
        body = shard.output.rstrip().split('\n')
        if len(body) > tail:
            lines.append('    [%d earlier lines of this shard not shown]'
                         % (len(body) - tail))
            body = body[-tail:]
        lines.extend('    ' + line for line in body)
    lines.append('Ran %d tests in %.1fs (%d workers, %d classes)'
                 % (ran, seconds, workers, len(shards)))
    # `skipped` stays in the detail on this branch: a run that failed AND
    # skipped is red either way, but the count a reader greps for is the one
    # that says which layers never ran.
    detail = ['%s=%d' % (k, totals[k]) for k in sorted(totals) if totals[k]]
    if ran != total:
        detail.append('unaccounted=%d' % (total - ran))
        lines.append('  discovery counted %d tests and the shards reported '
                     '%d; a suite that cannot say it ran is not a suite that '
                     'passed' % (total, ran))
    if missing:
        detail.append('unreported classes=%d' % len(missing))
    if repeated:
        detail.append('classes reported twice=%d' % len(repeated))
    if problems or missing or repeated or ran != total:
        if not detail:
            detail = ['%d problem(s), see the lines above' % len(problems)]
        lines.append('FAILED (%s)' % ', '.join(detail))
        return 1, lines
    if totals.get('skipped'):
        lines.append('OK (skipped=%d)' % totals['skipped'])
    else:
        lines.append('OK')
    return 0, lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Run the tests/ suite one process per TestCase class.')
    ap.add_argument('names', nargs='*',
                    help='module or module.Class to run; default is all')
    ap.add_argument('-j', '--workers', type=int, default=DEFAULT_WORKERS,
                    help='processes to run at once (default %d)'
                         % DEFAULT_WORKERS)
    args = ap.parse_args(argv)
    try:
        units = discover_units(names=args.names or None)
    except DiscoveryError as exc:
        print('Ran 0 tests in 0.0s (nothing was run)')
        print('FAILED (%s)' % exc)
        return 2
    expected = dict(units)
    workers = max(1, min(args.workers, len(units)))
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        shards = list(pool.map(lambda u: run_unit(u[0]), units))
    code, lines = aggregate(shards, expected, time.time() - started, workers)
    print('\n'.join(lines))
    return code


if __name__ == '__main__':
    sys.exit(main())
