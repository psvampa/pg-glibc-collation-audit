"""Layer 1: the algorithmic core, with no git and no glibc clone.

Every case here freezes a failure this tool actually shipped, and its docstring
says which, because a test whose purpose is forgotten is a test somebody
deletes during a refactor.
"""
import bz2
import contextlib
import gzip
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import _harness  # also puts scripts/ on sys.path
from _harness import flat

import glibc_locale_data as g
import diff_collation_code as d
import filter_lc_collate_changes as f
import diff_distro_locales as dd
import diff_node_locales as nl
import flag_algorithmic_ranges as fa
import collation_keys as ck
import node_charmaps as nc


def collate(*body):
    """A minimal locale file with an LC_COLLATE block, as glibc writes them."""
    return '\n'.join(['comment_char %', 'escape_char /', '',
                      'LC_COLLATE', *body, 'END LC_COLLATE', ''])


class Ellipsis(unittest.TestCase):
    """"flag_algorithmic_ranges.py matched one ellipsis form of four" and
    "Step 4 cleared zh_CN and three siblings that it should have flagged"."""

    def test_all_five_forms_glibc_accepts(self):
        # linereader.c consumes one '.' before comparing the rest, so these are
        # the five tokens: tok_ellipsis2/3/4 and the two (2) variants.
        for form in ('..', '...', '....', '..(2)..', '....(2)....'):
            with self.subTest(form=form):
                hits = g.ellipsis_hits(f'<U4E00>{form}<U9FA5>')
                self.assertEqual(len(hits), 1, f'{form} not matched')

    def test_inline_form_is_matched(self):
        """The inline form, as in iso14651_t1_common.

        Anchoring the pattern to the start of a line missed every one of these.
        """
        block = 'collating-symbol <SAC00>..<SD7A3>  % Hangul syllables'
        self.assertEqual(len(g.ellipsis_hits(block)), 1)

    def test_line_leading_form_is_matched(self):
        self.assertEqual(len(g.ellipsis_hits('.. ..;IGNORE;IGNORE;IGNORE')), 1)

    def test_prose_in_a_comment_is_not_a_range(self):
        """"2.28..2.34" in a comment is not an ellipsis range."""
        self.assertEqual(g.ellipsis_hits('% changed over 2.28..2.34'), [])
        self.assertEqual(g.ellipsis_hits('% and so on ...'), [])

    def test_code_before_a_comment_still_counts(self):
        block = '<UAC00>..<UD7A3>  % see 1.0...2.0 for prose'
        self.assertEqual(len(g.ellipsis_hits(block)), 1)

    def test_a_run_of_dots_yields_exactly_one_hit(self):
        """'....' must not read as two '..'."""
        self.assertEqual(len(g.ellipsis_hits('<A>....<B>')), 1)

    def test_honours_a_declared_comment_char(self):
        self.assertEqual(g.ellipsis_hits('# 2.28..2.34', comment_char='#'), [])

    def test_a_run_longer_than_any_named_token_is_matched(self):
        """This used to be a documented gap, and is now covered.

        glibc tokenises '.....' as tok_ellipsis4 plus a stray '.', so the line
        does use a range. Spelling out the five named forms matched none of
        them, and the dot-boundary guards then rejected every starting offset,
        so the line read as "no ellipsis here" -- a false clean in the one step
        whose job is to refuse to clear a locale.
        """
        for run in ('.....', '......', '.......'):
            with self.subTest(run=run):
                self.assertEqual(len(g.ellipsis_hits(f'<A>{run}<B>')), 1)

    def test_a_long_run_around_the_count_form_is_matched(self):
        self.assertEqual(len(g.ellipsis_hits('<A>.....(2).....<B>')), 1)


class ClassifyChange(unittest.TestCase):
    """Which of the four things a content-changed file can be.

    The bucket for "no LC_COLLATE block" used to be counted and never listed,
    and nothing checked whether the NEW side had gained one. A file that gains
    a block changes its sort order by definition.
    """

    OLD = collate('copy "iso14651_t1"')          # block on lines 4..6
    NO_BLOCK = 'comment_char %\nLC_CTYPE\ntranslit_start\ntranslit_end\nEND LC_CTYPE\n'

    def test_a_hunk_inside_the_block_is_a_collation_change(self):
        self.assertEqual(f.classify_change(self.OLD, None, [(5, 1)]), 'collate')

    def test_a_hunk_outside_the_block_is_not(self):
        """Read with the same characters on both sides. Until backlog 13.7
        this passed no new side and a hunk on line 1, which is the
        `comment_char` line: the test held the defect in place."""
        new = self.OLD + 'LC_IDENTIFICATION\nEND LC_IDENTIFICATION\n'
        self.assertEqual(f.classify_change(self.OLD, new, [(8, 2)]), 'other')

    def test_a_changed_comment_char_is_a_rules_change(self):
        """The block is the same text and is read differently: another
        comment character keeps or drops other lines (backlog 13.7, measured
        on the RHEL 8, 9 and 10 builds)."""
        new = self.OLD.replace('comment_char %', 'comment_char #', 1)
        self.assertEqual(f.classify_change(self.OLD, new, [(1, 1)]),
                         'reading')

    def test_a_changed_escape_char_is_a_rules_change(self):
        """`/` to the default `\\`, by dropping the directive. Writing
        `escape_char \\` instead ends the line in the escape in force, which
        glibc joins to the next line, and the reader refuses that shape."""
        new = self.OLD.replace('escape_char /\n', '', 1)
        self.assertEqual(g.reading_chars(new), ('%', '\\'))
        self.assertEqual(f.classify_change(self.OLD, new, [(2, 1)]),
                         'reading')

    def test_a_directive_dropped_for_the_default_is_a_rules_change(self):
        """No directive is glibc's `#`, not this module's `%`: a file that
        stops declaring `comment_char %` is read with another character."""
        new = self.OLD.replace('comment_char %\n', '', 1)
        self.assertEqual(f.classify_change(self.OLD, new, [(1, 1)]),
                         'reading')

    def test_characters_the_reader_cannot_settle_do_not_clear(self):
        """A directive after a category is a shape _glibc_chars refuses."""
        new = self.OLD + 'comment_char #\n'
        self.assertEqual(f.classify_change(self.OLD, new, [(8, 1)]),
                         'reading')

    def test_characters_unsettled_on_both_sides_do_not_clear(self):
        """Two answers of "cannot tell" are not one answer that agrees."""
        old = self.OLD + 'comment_char #\n'
        new = old + '% a note\n'
        self.assertIsNone(g.reading_chars(old))
        self.assertIsNone(g.reading_chars(new))
        self.assertEqual(f.classify_change(old, new, [(9, 1)]), 'reading')

    def test_a_block_with_no_new_side_is_not_cleared(self):
        """main() reads every new side; a caller that passes none cannot
        settle the characters, so the block is not called unchanged."""
        self.assertEqual(f.classify_change(self.OLD, None, [(8, 1)]),
                         'reading')

    def test_no_block_on_either_side_is_no_collate(self):
        self.assertEqual(
            f.classify_change(self.NO_BLOCK, self.NO_BLOCK, [(2, 1)]),
            'no-collate')

    def test_gaining_a_block_is_its_own_verdict(self):
        """Injected, because it has never happened in glibc between 2.17 and
        2.42 -- which is exactly why it needs a test rather than a comment."""
        self.assertEqual(
            f.classify_change(self.NO_BLOCK, self.OLD, [(2, 1)]),
            'gained-collate')

    def test_a_gained_block_is_not_filed_under_no_collate(self):
        """The regression this guards: folding it into the silent bucket hides
        a real collation change behind a count."""
        self.assertNotEqual(
            f.classify_change(self.NO_BLOCK, self.OLD, []), 'no-collate')

    def test_losing_a_block_is_still_judged_by_the_old_bounds(self):
        """The old side is what an existing index was built against."""
        self.assertEqual(
            f.classify_change(self.OLD, self.NO_BLOCK, [(5, 1)]), 'collate')

    def test_a_missing_new_side_does_not_crash(self):
        """main() reads every new side; a caller may pass none."""
        self.assertEqual(f.classify_change(self.NO_BLOCK, None, []),
                         'no-collate')

    # LC_CTYPE's END on line 6, LC_COLLATE opening on line 7.
    BELOW_CTYPE = ('comment_char %\nescape_char /\n\nLC_CTYPE\ncopy "i18n"\n'
                   'END LC_CTYPE{end}\nLC_COLLATE\ncopy "iso14651_t1"\n'
                   'END LC_COLLATE\n')

    def test_an_end_that_swallows_lc_collate_is_a_rules_change(self):
        """Backlog 13.18: the END line ends in the escape, lr_ignore_rest
        drops the LC_COLLATE line below it, and glibc builds the file with
        no LC_COLLATE. The block itself is the same text."""
        old = self.BELOW_CTYPE.format(end='')
        new = self.BELOW_CTYPE.format(end=' % a note /')
        self.assertEqual(f.classify_change(old, new, [(6, 1)]), 'unread')
        self.assertEqual(f.classify_change(new, old, [(6, 1)]), 'unread')

    def test_the_control_a_note_without_the_escape_is_not(self):
        old = self.BELOW_CTYPE.format(end='')
        new = self.BELOW_CTYPE.format(end=' % a note')
        self.assertEqual(f.classify_change(old, new, [(6, 1)]), 'other')

    def test_early_is_asked_of_each_side_for_its_own_text(self):
        """A 0x1A above LC_COLLATE ends the file only for a localedef
        before LR_GETC_FIX. The old side holds it here, so only the old
        side's answer can clear it."""
        old = self.BELOW_CTYPE.format(end='\n\x1a')
        new = self.BELOW_CTYPE.format(end='')
        yes, no = (lambda: True), (lambda: False)
        self.assertEqual(f.classify_change(old, new, [(7, 1)], (yes, no)),
                         'unread')
        self.assertEqual(f.classify_change(old, new, [(7, 1)], (no, yes)),
                         'other')
        self.assertEqual(f.classify_change(old, new, [(7, 1)]), 'unread',
                         'a caller that cannot ask must not clear it')
        # And the new side, holding the byte, asks the new side's question.
        self.assertEqual(f.classify_change(new, old, [(6, 0)], (yes, no)),
                         'other')
        self.assertEqual(f.classify_change(new, old, [(6, 0)], (no, yes)),
                         'unread')

    def test_an_end_commented_out_above_lc_collate_is_a_rules_change(self):
        """Review of 13.18, finding 1: LC_CTYPE is never closed, and glibc
        reads LC_COLLATE inside it."""
        old = self.BELOW_CTYPE.format(end='')
        new = old.replace('END LC_CTYPE', '% END LC_CTYPE', 1)
        self.assertEqual(f.classify_change(old, new, [(6, 1)]), 'unread')


class RenamedFileOnTheNewSide(unittest.TestCase):
    """"Step 2 aborted on a rename whose old side had no LC_COLLATE block", and
    could not see such a file gain one: the new side was read and looked up
    under the OLD path, which does not exist at the new tag. Latent -- the one
    rename in the audited pairs has a block on both sides -- so it is tested
    by injection, like gained-collate itself."""

    OLD = {'localedata/locales/x': 'LC_CTYPE\ncopy "i18n"\nEND LC_CTYPE\n',
           'localedata/locales/k': collate('order_start forward', '<U0041>',
                                          'order_end')}
    RENAMED = {'localedata/locales/x': 'localedata/locales/y'}

    def test_a_renamed_file_is_read_under_its_new_name(self):
        to_read = f.new_side_paths(list(self.OLD), self.RENAMED)
        self.assertEqual(to_read['localedata/locales/x'],
                         'localedata/locales/y')

    def test_a_file_with_an_old_block_is_read_too(self):
        """Its block can be read with other characters at the new tag
        (backlog 13.7). It used to be left out, and judged on its old side
        alone."""
        to_read = f.new_side_paths(list(self.OLD), {})
        self.assertEqual(to_read, {p: p for p in self.OLD})

    def test_a_renamed_file_that_gained_a_block_is_gained_collate(self):
        """Restore the lookup under the old path and this reads 'no-collate':
        the new text is there, filed under a name nobody asks for."""
        new = {'localedata/locales/y': collate('order_start forward',
                                               '<U0042>', 'order_end')}
        verdicts = dict(f.judge(list(self.OLD), self.OLD, new, {},
                                self.RENAMED))
        self.assertEqual(verdicts['localedata/locales/x'], 'gained-collate')

    def test_an_unrenamed_file_still_finds_its_own_new_text(self):
        new = {'localedata/locales/x': collate('order_start forward',
                                               '<U0042>', 'order_end')}
        verdicts = dict(f.judge(['localedata/locales/x'], self.OLD, new, {},
                                {}))
        self.assertEqual(verdicts['localedata/locales/x'], 'gained-collate')


class CorpusFloorIsShared(unittest.TestCase):
    """The node modes and the tag modes refuse the same size of corpus. Two
    constants would drift the way two copies of a count do."""

    def test_one_floor_for_tags_and_nodes(self):
        """Moving the floor in glibc_locale_data moves the node modes' floor
        with it. This used to be an assertIs between the two constants, which
        passed with a second 200 typed by hand, because CPython keeps one
        object per small integer."""
        for floor in (7, 4321):
            with self.subTest(floor=floor):
                with mock.patch.object(g, 'MIN_LOCALE_FILES', floor):
                    spec = importlib.util.spec_from_file_location(
                        'dd_fresh', dd.__file__)
                    fresh = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(fresh)
                self.assertEqual(fresh.DEFAULT_MIN_FILES, floor)

    def test_the_node_modes_refuse_below_the_shared_floor(self):
        """The rest of the path. The floor each node mode applies is the
        default of its --min-files, and audit.sh passes no --min-files, so a
        literal typed there would keep that mode at the old floor after the
        shared one moved."""
        base = tempfile.mkdtemp(prefix='pg-glibc-floor-')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        for side in ('old', 'new'):
            os.mkdir(os.path.join(base, side))
            for i in range(3):
                with open(os.path.join(base, side, f'x{i}'), 'w') as fh:
                    fh.write('LC_CTYPE\nEND LC_CTYPE\n')
        old, new = os.path.join(base, 'old'), os.path.join(base, 'new')
        modes = (
            ('diff_node_locales', nl.main,
             ['--old-locales-dir', old, '--old-build-id', 'o',
              '--new-locales-dir', new, '--new-build-id', 'n']),
            ('flag_algorithmic_ranges', fa.main,
             ['--locales-dir', new, '--build-id', 'n']),
        )
        # Both directions: a floor moved down and left behind by a mode is a
        # drift too. OUT_DIR is base, so a mode that stopped refusing writes
        # its lists there and not into the shared /tmp directory.
        for name, main, argv in modes:
            for floor in (4, 4321):
                with self.subTest(mode=name, floor=floor):
                    err = io.StringIO()
                    with mock.patch.object(g, 'MIN_LOCALE_FILES', floor), \
                            mock.patch.object(dd, 'DEFAULT_MIN_FILES', floor), \
                            mock.patch.object(g, 'OUT_DIR', base), \
                            contextlib.redirect_stderr(err), \
                            contextlib.redirect_stdout(io.StringIO()), \
                            self.assertRaises(SystemExit) as cm:
                        main(argv)
                    self.assertEqual(cm.exception.code, 2)
                    self.assertIn(f'below the floor of {floor}.',
                                  flat(err.getvalue()))

    def test_the_floor_is_below_every_pinned_tag_and_measured_node(self):
        """286 is glibc-2.12, the smallest tag the suite pins; 355 the
        smallest measured node."""
        self.assertLess(g.MIN_LOCALE_FILES, 286)
        self.assertGreater(g.MIN_LOCALE_FILES, 3)


class PartitionVerdicts(unittest.TestCase):
    """What the report DOES with each verdict.

    Split from ClassifyChange on purpose: mutation testing showed that testing
    the classifier alone left the acting-on-it step unguarded -- the verdict was
    computed correctly and then dropped, with every test still green.
    """

    def test_a_gained_block_counts_as_a_collation_change(self):
        changed, gained, _, _, no_collate = f.partition_verdicts(
            [('p', 'gained-collate')])
        self.assertIn('p', changed, 'a gained block was not counted as changed')
        self.assertIn('p', gained)
        self.assertNotIn('p', no_collate)

    def test_a_block_read_otherwise_counts_as_a_collation_change(self):
        changed, _, reading, unchanged, _ = f.partition_verdicts(
            [('p', 'reading')])
        self.assertIn('p', changed, 'a block read otherwise was not counted')
        self.assertIn('p', reading)
        self.assertNotIn('p', unchanged)

    def test_each_verdict_lands_in_its_list(self):
        lists = f.partition_verdicts(
            [('a', 'collate'), ('b', 'other'), ('c', 'no-collate')])
        self.assertEqual(lists, (['a'], [], [], ['b'], ['c']))

    def test_a_block_glibc_may_not_read_counts_as_a_collation_change(self):
        """Backlog 13.18. Its `!!` is built by unread_above, from every
        changed file: here only the count and step 3's list are decided."""
        lists = f.partition_verdicts([('p', 'unread')])
        self.assertEqual(lists, (['p'], [], [], [], []))

    def test_an_unknown_verdict_is_refused_not_cleared(self):
        """It used to fall into `unchanged`, the bucket a new answer must not
        land in by default."""
        with self.assertRaises(ValueError):
            f.partition_verdicts([('p', 'something-new')])

    def test_order_is_preserved(self):
        changed, _, _, _, _ = f.partition_verdicts(
            [('z', 'collate'), ('a', 'collate')])
        self.assertEqual(changed, ['z', 'a'])


class CopyGraph(unittest.TestCase):
    """"resolve_copy_closure.py followed only the first copy per file" and
    "inherited_from() reported a single inherited root"."""

    def test_every_copy_is_returned_not_just_the_first(self):
        """om_ET really does copy both am_ET and om_KE (verified at 2.39)."""
        text = collate('copy "am_ET"', 'copy "om_KE"')
        self.assertEqual(g.copy_targets(text), ['am_ET', 'om_KE'])

    def test_copy_outside_the_collate_block_is_ignored(self):
        text = ('LC_TIME\ncopy "en_US"\nEND LC_TIME\n'
                + collate('copy "iso14651_t1"'))
        self.assertEqual(g.copy_targets(text), ['iso14651_t1'])

    def test_a_copy_with_no_blank_before_the_string_is_followed(self):
        """Backlog 13.17: get_ident ends the word at the `"`
        (linereader.c:580@2.39, the same at 2.28 and 2.34), so glibc reads
        `copy"x"` as a copy of x, and the locale stayed out of step 3's
        closure."""
        self.assertEqual(g.copy_targets(collate('copy"iso14651_t1"')),
                         ['iso14651_t1'])
        self.assertEqual(g.copy_targets(collate('copy\v"iso14651_t1"')),
                         ['iso14651_t1'])

    def test_inherited_from_reports_every_root_reached(self):
        """Not whichever root a depth-first walk happened to hit first."""
        graph = {'root_a': [], 'root_b': [], 'mid': ['root_a', 'root_b'],
                 'leaf': ['mid']}
        got = g.inherited_from(graph, {'root_a', 'root_b'})
        self.assertEqual(got['leaf'], ['root_a', 'root_b'])
        self.assertEqual(got['mid'], ['root_a', 'root_b'])

    def test_a_cycle_terminates(self):
        graph = {'a': ['b'], 'b': ['a'], 'root': [], 'c': ['a', 'root']}
        got = g.inherited_from(graph, {'root'})
        self.assertEqual(sorted(got), ['c'])

    def test_a_root_is_not_listed_as_inheriting_from_itself(self):
        graph = {'root': [], 'child': ['root']}
        self.assertNotIn('root', g.inherited_from(graph, {'root'}))

    def test_transitive_inheritance_is_followed(self):
        graph = {'root': [], 'a': ['root'], 'b': ['a'], 'c': ['b']}
        self.assertEqual(sorted(g.inherited_from(graph, {'root'})),
                         ['a', 'b', 'c'])


class GeneratedNames(unittest.TestCase):
    """"locale -a does not spell locales the way the audit printed them".

    Printing the SUPPORTED spelling sends people to COLLATE "sv_SE.UTF-8",
    which fails with `collation ... does not exist`.
    """

    def test_codeset_is_lowercased_and_punctuation_dropped(self):
        self.assertEqual(g.normalize_locale_name('sv_SE.UTF-8'), 'sv_SE.utf8')

    def test_modifier_is_preserved_after_the_codeset(self):
        self.assertEqual(g.normalize_locale_name('ca_ES.UTF-8@valencia'),
                         'ca_ES.utf8@valencia')

    def test_a_name_with_no_codeset_is_unchanged(self):
        self.assertEqual(g.normalize_locale_name('sv_FI@euro'), 'sv_FI@euro')
        self.assertEqual(g.normalize_locale_name('or_IN'), 'or_IN')

    def test_an_all_digit_codeset_gains_the_iso_prefix(self):
        self.assertEqual(g.normalize_locale_name('sv_SE.ISO-8859-1'),
                         'sv_SE.iso88591')
        self.assertEqual(g.normalize_locale_name('ja_JP.EUC-JP'),
                         'ja_JP.eucjp')


class CollateBounds(unittest.TestCase):
    """Where the LC_COLLATE block starts and ends -- step 2 judges every hunk
    against these line numbers."""

    def test_finds_the_block(self):
        text = 'LC_TIME\nx\nEND LC_TIME\nLC_COLLATE\ncopy "a"\nEND LC_COLLATE\n'
        self.assertEqual(g.collate_bounds(text), (4, 6))

    def test_an_unterminated_block_runs_to_end_of_file(self):
        """Conservative on purpose: a change is more likely to be flagged."""
        text = 'LC_TIME\nEND LC_TIME\nLC_COLLATE\ncopy "a"\n'
        start, end = g.collate_bounds(text)
        self.assertEqual(start, 3)
        self.assertEqual(end, len(text.split('\n')))

    def test_no_block_returns_none(self):
        self.assertIsNone(g.collate_bounds('LC_TIME\nx\nEND LC_TIME\n'))

    def test_block_on_the_very_first_line_is_found_by_both(self):
        """The glibc <= 2.23 shape, and the whole of the old 2.24 version
        floor. collate_bounds always saw it; collate_block used a regex needing
        a preceding newline and did not, which dropped the three master
        templates from the copy graph. There is no asymmetry left to record --
        both see it, and this asserts they agree."""
        text = 'LC_COLLATE\ncopy "a"\nEND LC_COLLATE\n'
        self.assertEqual(g.collate_bounds(text), (1, 3))
        self.assertEqual(g.collate_block(text), '\ncopy "a"')

    def test_an_unterminated_block_reaches_collate_block_too(self):
        """collate_bounds runs an unterminated block to the end of the file;
        collate_block now inherits that instead of returning None. The
        conservative direction: the locale stays in the copy graph."""
        text = 'LC_COLLATE\ncopy "a"\n'
        self.assertEqual(g.collate_block(text), '\ncopy "a"\n')


class HunkOverlap(unittest.TestCase):
    """Does a hunk fall inside LC_COLLATE? Step 2's whole verdict rests here."""

    # Block occupies old-side lines 10..20 inclusive.
    LC = (10, 20)

    def test_modification_inside_the_block(self):
        self.assertTrue(f.hunk_touches_block(12, 3, *self.LC))

    def test_modification_entirely_before_the_block(self):
        self.assertFalse(f.hunk_touches_block(1, 5, *self.LC))

    def test_modification_entirely_after_the_block(self):
        self.assertFalse(f.hunk_touches_block(25, 2, *self.LC))

    def test_modification_straddling_the_start(self):
        self.assertTrue(f.hunk_touches_block(8, 5, *self.LC))

    def test_pure_insertion_just_inside_the_end_counts(self):
        """`@@ -19,0` adds lines after old line 19, still inside the block."""
        self.assertTrue(f.hunk_touches_block(19, 0, *self.LC))

    def test_pure_insertion_after_end_lc_collate_does_not_count(self):
        """`@@ -20,0` appends after the END LC_COLLATE line -- outside.

        Without the length==0 special case this read as a collation change.
        """
        self.assertFalse(f.hunk_touches_block(20, 0, *self.LC))

    def test_pure_insertion_just_before_the_block_does_not_count(self):
        self.assertFalse(f.hunk_touches_block(9, 0, *self.LC))


class ChangedCharacters(unittest.TestCase):
    """"Step 2 named the file and stopped": the characters
    the confirmation template needs as test values were in the diff step 2
    had already read, and reaching them meant a `git diff` by hand. These pin
    how the changed lines are read, by injection, because the corpus never
    produces most of these shapes.

    The new block sits three lines lower than the old one, so a line placed
    against the wrong side's block lands outside it.
    """

    # LC_COLLATE is old lines 6..9 and new lines 9..12.
    OLD = ('comment_char %\nescape_char /\nLC_CTYPE\n<U0043>\nEND LC_CTYPE\n'
           'LC_COLLATE\n<U0041> <a>\n<U0042> <b>\nEND LC_COLLATE\n')
    NEW = ('comment_char %\nescape_char /\nLC_CTYPE\n<U0043>\n<U0045>\n'
           '<U0046>\n<U0047>\nEND LC_CTYPE\n'
           'LC_COLLATE\n<U0041> <a>\n<U0044> <b>\nEND LC_COLLATE\n')

    def chars(self, section, old=OLD, new=NEW):
        return f.changed_characters([section], old, new)

    def test_a_removed_rule_is_read_against_the_old_block(self):
        """Old line 8 is inside the old block and outside the new one."""
        self.assertEqual(self.chars('\n@@ -8 +10,0 @@\n-<U0042> <b>\n'), ['B'])

    def test_an_added_rule_is_read_against_the_new_block(self):
        """New line 11 is inside the new block and outside the old one."""
        self.assertEqual(self.chars('\n@@ -8,0 +11 @@\n+<U0044> <b>\n'), ['D'])

    def test_lines_outside_the_block_name_nothing(self):
        self.assertEqual(self.chars('\n@@ -4 +4,4 @@\n-<U0043>\n+<U0043>\n'
                                    '+<U0045>\n+<U0046>\n+<U0047>\n'), [])

    def test_a_hunk_crossing_the_block_edge_counts_only_its_lines_inside(self):
        """ber_DZ over 2.34..2.39: one hunk deletes from before LC_COLLATE to
        past its end. The character named before the block is not a rule."""
        section = ('\n@@ -4,5 +4,0 @@\n-<U0043>\n-END LC_CTYPE\n-LC_COLLATE\n'
                   '-<U0041> <a>\n-<U0042> <b>\n')
        self.assertEqual(self.chars(section), ['A', 'B'])

    def test_context_lines_move_both_sides(self):
        """Step 2 reads git's -U0 diff, with --inter-hunk-context=0 pinned in
        DIFF_FLAGS, so no run hands this branch a context line. It is held for
        a diff that has them: uncounted, both changed lines below are numbered
        as if they sat before the block, and nothing is found."""
        section = ('\n@@ -5,3 +8,3 @@\n END LC_CTYPE\n LC_COLLATE\n'
                   '-<U0041> <a>\n+<U0048> <a>\n')
        self.assertEqual(self.chars(section), ['A', 'H'])

    def test_a_removed_line_opening_with_two_dashes_is_a_rule(self):
        """After the first hunk header, `---` is a removed line whose text
        opens with `--`, not a file name."""
        section = '\n--- a/x\n+++ b/x\n@@ -8 +11 @@\n---<U0046>\n+<U0044> <b>\n'
        self.assertEqual(self.chars(section), ['F', 'D'])

    def test_a_comment_names_no_rule(self):
        """A comment names characters too, and a changed comment is not a
        changed rule."""
        section = '\n@@ -7 +10 @@\n-<U0041> <a>\n+<U0041> <a> % <U005A>\n'
        self.assertEqual(self.chars(section), ['A'])

    def test_the_file_s_own_comment_char_is_used(self):
        old = 'comment_char #\nLC_COLLATE\n<U0041> <a>\nEND LC_COLLATE\n'
        new = 'comment_char #\nLC_COLLATE\n<U0041> <a> # <U005A>\nEND LC_COLLATE\n'
        section = '\n@@ -3 +3 @@\n-<U0041> <a>\n+<U0041> <a> # <U005A>\n'
        self.assertEqual(self.chars(section, old, new), ['A'])

    def test_the_comment_char_is_looked_up_once_per_side(self):
        """Called once per changed line, the lookup scans the whole file each
        time: measured 17 minutes on cns11643_stroke when the last attempt at
        this did it that way."""
        section = '\n@@ -8 +11 @@\n' + '-<U0042> <b>\n' * 10 + '+<U0044> <b>\n'
        with mock.patch.object(g, 'comment_char',
                               wraps=g.comment_char) as looked_up:
            self.chars(section)
        self.assertEqual(looked_up.call_count, 2)

    def test_a_gained_block_is_read_from_the_new_side(self):
        old = 'comment_char %\nLC_CTYPE\nEND LC_CTYPE\n'
        new = old + 'LC_COLLATE\n<U0041> <a>\nEND LC_COLLATE\n'
        section = '\n@@ -3,0 +4,3 @@\n+LC_COLLATE\n+<U0041> <a>\n+END LC_COLLATE\n'
        self.assertEqual(self.chars(section, old, new), ['A'])

    def test_a_gained_block_that_names_no_character_is_empty(self):
        """The caller turns this into the warning, so a character named on a
        changed line outside the new block must not fill the list and
        suppress it. Not reachable in any audited pair: no file has gained a
        block there at all."""
        old = 'comment_char %\nLC_CTYPE\n<U0043>\nEND LC_CTYPE\n'
        new = ('comment_char %\nLC_CTYPE\n<U0044>\nEND LC_CTYPE\n'
               'LC_COLLATE\ncopy "iso14651_t1"\nEND LC_COLLATE\n')
        section = ('\n@@ -3 +3 @@\n-<U0043>\n+<U0044>\n'
                   '@@ -4,0 +5,3 @@\n+LC_COLLATE\n+copy "iso14651_t1"\n'
                   '+END LC_COLLATE\n')
        self.assertEqual(self.chars(section, old, new), [])


class ChangedCharactersCannotTell(unittest.TestCase):
    """What it cannot tell comes back empty, never partial: the caller prints
    the warning for an empty answer, and a partial one would print as if it
    were the whole list."""

    OLD, NEW = ChangedCharacters.OLD, ChangedCharacters.NEW
    SECTION = '\n@@ -8 +11 @@\n-<U0042> <b>\n+<U0044> <b>\n'

    def test_the_control_names_both(self):
        self.assertEqual(
            f.changed_characters([self.SECTION], self.OLD, self.NEW), ['B', 'D'])

    def test_an_unreadable_new_version(self):
        self.assertEqual(f.changed_characters([self.SECTION], self.OLD, None), [])

    def test_a_path_the_diff_carries_twice(self):
        self.assertEqual(f.changed_characters([self.SECTION, self.SECTION],
                                              self.OLD, self.NEW), [])

    def test_a_path_the_diff_does_not_carry(self):
        self.assertEqual(f.changed_characters([], self.OLD, self.NEW), [])

    def test_an_unparsable_hunk_header_after_a_good_one(self):
        section = self.SECTION + '@@ not a header @@\n-<U0041> <a>\n'
        self.assertEqual(f.changed_characters([section], self.OLD, self.NEW), [])


class PrintCharacters(unittest.TestCase):
    """How the list reads on a terminal."""

    def printed(self, chars):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f.print_characters(chars)
        return buf.getvalue()

    def test_a_character_a_terminal_would_not_show_is_its_code_point(self):
        """or_IN's change over 2.28..2.34 names U+0008, U+000F and U+001E."""
        out = self.printed(['W', '\x0f', ' '])
        self.assertIn('W (U+0057)', out)
        self.assertIn('U+000F', out)
        self.assertIn('U+0020', out)
        self.assertNotIn('\x0f', out)

    def test_a_long_list_is_wrapped_without_losing_or_splitting_an_entry(self):
        chars = [chr(0x0100 + i) for i in range(60)]   # Latin Extended-A
        out = self.printed(chars)
        self.assertIn('characters in the changed rules (60):', out)
        self.assertEqual(
            re.findall(r'(\S) \(U\+([0-9A-F]{4,6})\)', out),
            [(c, f'{ord(c):04X}') for c in chars])
        self.assertLessEqual(max(len(line) for line in out.splitlines()), 78)


class NoiseFilter(unittest.TestCase):
    """"Step 5 discarded real code as comment/copyright".

    The rule used to be: noise unless the line carries one of ;{}=(). That
    swallowed whole hunks under the heading "no substantive change". These are
    three of them.
    """

    def test_preprocessor_include_is_code(self):
        self.assertFalse(d.is_noise_line('+#include "C-collate-seq.c"'))

    def test_preprocessor_define_is_code(self):
        self.assertFalse(d.is_noise_line('-#define NO_FINALIZE'))
        self.assertFalse(d.is_noise_line('+#define NO_ADD_LOCALE'))

    def test_indented_preprocessor_define_is_code(self):
        self.assertFalse(d.is_noise_line('-# define STRCMP strcmp'))

    def test_a_label_is_code(self):
        self.assertFalse(d.is_noise_line('+  case tok_codepoint_collation:'))

    def test_a_bare_declarator_is_code(self):
        self.assertFalse(d.is_noise_line('+  bool codepoint_collation;'))

    def test_a_lone_else_is_code(self):
        self.assertFalse(d.is_noise_line('+  else'))

    def test_comments_and_licences_are_noise(self):
        for line in ('+/* Compare the file */', '+ * continuation',
                     '+// trailing', '+   end of comment */',
                     '-   Copyright (C) 2000-2018 Free Software Foundation, Inc.',
                     '+   <https://www.gnu.org/licenses/>.  */',
                     '-   Contributed by Ulrich Drepper <drepper@gnu.org>, 1995.',
                     '-   Written by Ulrich Drepper, 1995.',
                     '+'):
            with self.subTest(line=line):
                self.assertTrue(d.is_noise_line(line), line)

    def test_comment_continuation_without_a_leading_star_is_noise(self):
        """The second line here opens with neither '*' nor '/*', and closes
        with neither '*/'. Only carrying the open-comment state sees it."""
        body = ['+  /* Compare the file with the locale data files for the same',
                '+     category in other locales, to save disk space.  */',
                '+  int x = 1;']
        marked = d.classify_body(body)
        self.assertTrue(marked[0][1])
        self.assertTrue(marked[1][1], 'comment continuation read as code')
        self.assertFalse(marked[2][1])

    def test_open_comment_state_is_tracked_per_side(self):
        """+ and - are two different versions of the file; one side's open
        comment must not silence the other."""
        body = ['-  /* removed comment', '+  int kept = 1;', '-     tail */']
        marked = d.classify_body(body)
        self.assertFalse(marked[1][1], '+ line silenced by an open - comment')

    def test_a_preprocessor_line_is_never_swallowed_by_comment_state(self):
        body = ['+  /* an unterminated comment', '+#include <array_length.h>']
        marked = d.classify_body(body)
        self.assertFalse(marked[1][1])

    def test_a_pointer_dereference_is_code(self):
        """"The noise filter took C code for a comment": every line opening
        with `*` was noise, and `*wp = '\\0';` opens with `*`. Ten such lines
        sat unmarked in the two published examples."""
        for line in ("-      *wp = '\\0';", '-	  *wp++ = tolower (codeset[cnt]);',
                     "-  *endp++ = '/';", '+      *wch = result;',
                     '+  **argv = 0;', '+  *(p + 1) = 2;'):
            with self.subTest(line=line):
                self.assertFalse(d.is_noise_line(line), line)

    def test_a_star_comment_continuation_is_still_noise(self):
        for line in ('+ * continuation text', '+ *', '+ */',
                     '+   *  indented star, then text'):
            with self.subTest(line=line):
                self.assertTrue(d.is_noise_line(line), line)

    def test_a_comment_closed_on_a_context_line_does_not_hide_the_code(self):
        """"The noise filter read every third line of the comment it was
        tracking." `git diff` gives three lines of context; the filter kept
        only the +/- lines, so a comment that opened on a changed line and
        closed on a CONTEXT line stayed open for the rest of the hunk and
        every changed line after it was marked noise -- eight lines of
        charmap_find_value() in linereader.c over 2.34..2.39, printed without
        the `>>` the docs tell the reader to scan for. Discard the context
        lines again and this fails."""
        body = ['-  /* old', '+  /* new', '   rest */', '+  code();']
        marked = d.classify_body(body)
        self.assertEqual([ln for ln, _ in marked],
                         ['-  /* old', '+  /* new', '+  code();'],
                         'a context line was emitted as a changed line')
        self.assertFalse(marked[2][1],
                         'code after a comment that closed on a context line '
                         'was filtered as prose')

    def test_a_continuation_of_a_comment_opened_in_context_is_still_noise(self):
        """The control for the test above: reading the context lines must not
        turn into marking everything as code. Here the comment OPENS on a
        context line, so the changed line inside it is prose, and a hunk of
        only that is filtered."""
        body = ['   /* open', '+   more prose about the table', '   close */']
        marked = d.classify_body(body)
        self.assertEqual([ln for ln, _ in marked],
                         ['+   more prose about the table'])
        self.assertTrue(marked[0][1],
                        'a comment opened on a context line was marked code')

    def test_a_comment_marker_inside_a_string_or_line_comment_opens_nothing(self):
        """A state wrongly left OPEN marks the code after it as prose, which
        is the direction that hides a hunk -- and with the context lines now
        read there are three times as many lines that can do it. Replace the
        scanner with `rfind` and both of these mark `code ();` as noise."""
        for prefix in (' ', '+'):
            # Both call sites: the context line and the changed line advance
            # the state through different lines of classify_body, and a test
            # that only feeds one leaves the other free to go back to `rfind`.
            for opener in ('// see /* below', 'x = f ("/*");',
                           "c = '/'; /* real */"):
                with self.subTest(prefix=prefix, opener=opener):
                    body = [f'{prefix}  {opener}', '+  code ();']
                    marked = d.classify_body(body)
                    self.assertFalse(marked[-1][1], body)

    def test_a_real_comment_still_opens_after_a_string(self):
        """Control: masking the string must not swallow the comment that
        follows it."""
        marked = d.classify_body(['   f ("x");  /* opens here',
                                  '+  still prose'])
        self.assertTrue(marked[0][1])

    def test_a_string_continued_with_a_backslash_opens_nothing(self):
        """A string whose line ends in `\\` goes on on the next line, and the
        scanner started every line outside any string. A `/*` in the
        continuation then opened a comment that never closed, and every
        changed line after it was marked as prose -- code included. No line
        in the five pinned tags has that shape; this is the shape."""
        for prefix in (' ', '+'):
            # Both call sites, as above: the state is carried through a
            # context line and through a changed line by different code.
            with self.subTest(prefix=prefix):
                body = [f'{prefix}  msg = "one \\',
                        f'{prefix}  two /* three";',
                        '+  code ();']
                marked = d.classify_body(body)
                self.assertFalse(marked[-1][1], body)

    def test_a_line_that_starts_inside_a_continued_string_is_code(self):
        """Carrying the string was not enough: the verdict for the line it is
        carried INTO still came from is_noise_line, and `* new option\\n",
        stdout);` opens with the `*` of a comment continuation."""
        body = [' fputs ("Usage: \\',
                '-* old option\\n", stdout);',
                '+* new option\\n", stdout);']
        marked = d.classify_body(body)
        self.assertEqual([noise for _, noise in marked], [False, False])

    def test_a_quote_left_open_without_a_backslash_is_not_carried(self):
        """Only a `\\` at the end of the line continues a string. A quote left
        open for any other reason -- here the apostrophe in a comment that
        began before the hunk, which the scanner cannot know -- must end with
        its line, or the code after it is marked as prose."""
        body = [" do not reorder: this is the user's choice",
                '+  x = \'"\'; y = "/*";',
                '+  weight = next ();']
        marked = d.classify_body(body)
        self.assertFalse(marked[-1][1], body)

    def test_a_backslash_outside_a_string_carries_nothing(self):
        """Control: a macro continuation is not a string, so a real `/*` on
        the line after it still opens a comment."""
        marked = d.classify_body(['+#define X 1 \\',
                                  '+  /* opens here',
                                  '+  still prose'])
        self.assertTrue(marked[-1][1])

    def test_a_hunk_made_only_of_dereferences_is_kept(self):
        """The failure that mattered: a hunk is dropped only when EVERY line
        is noise, so a hunk whose changed lines are all `*p = x;` vanished
        whole under "comment/licence hunk(s) filtered" and step 5 went on to
        say there was no substantive change. Restore `startswith('*')` and
        this fails."""
        body = ["-      *wp = '\\0';", "+      *wp = 0;"]
        marked = d.classify_body(body)
        self.assertTrue(any(not noise for _, noise in marked),
                        'a hunk of pointer writes was filtered as comment')


class TrackedLists(unittest.TestCase):
    """The walk subtracts its entry points from what it reports, so an entry
    point that is in no tier is checked for existence and never diffed. That
    is false negative "two entry points of step 5 were diffed by nobody"; this
    is the assertion that keeps a sixth entry point from repeating it."""

    def test_every_entry_point_is_also_in_a_tier(self):
        self.assertLessEqual(set(d.ENTRY_POINTS), set(d.TIER1) | set(d.TIER2))


class DiffParsing(unittest.TestCase):
    DIFF = (
        'diff --git a/localedata/locales/sv_SE b/localedata/locales/sv_SE\n'
        'index 111..222 100644\n'
        '--- a/localedata/locales/sv_SE\n'
        '+++ b/localedata/locales/sv_SE\n'
        '@@ -100,2 +100,3 @@\n-old\n+new\n'
        '@@ -200 +201 @@\n-x\n+y\n'
        'diff --git a/localedata/locales/or_IN b/localedata/locales/or_IN\n'
        'index 333..444 100644\n'
        '--- a/localedata/locales/or_IN\n'
        '+++ b/localedata/locales/or_IN\n'
        '@@ -50,0 +51,2 @@\n+a\n+b\n')

    def test_hunks_are_grouped_by_file(self):
        got = f.parse_diff(self.DIFF)
        self.assertEqual(sorted(got), ['localedata/locales/or_IN',
                                       'localedata/locales/sv_SE'])

    def test_a_hunk_without_a_length_means_one_line(self):
        got = f.parse_diff(self.DIFF)
        self.assertIn((200, 1), got['localedata/locales/sv_SE'])

    def test_a_pure_insertion_keeps_its_zero_length(self):
        got = f.parse_diff(self.DIFF)
        self.assertEqual(got['localedata/locales/or_IN'], [(50, 0)])

    def test_a_binary_section_is_refused(self):
        """"Binary files ... differ" carries no hunk, so the file came back
        with no range at all and classify_change called it 'other': a change
        nothing read, reported as a change outside LC_COLLATE. The stale
        check does not see it, because the path IS in the result. Git writes
        it for every locale under one `-diff` in the reader's attributes."""
        diff = ('diff --git a/localedata/locales/sv_SE '
                'b/localedata/locales/sv_SE\n'
                'index 111..222 100644\n'
                'Binary files a/localedata/locales/sv_SE and '
                'b/localedata/locales/sv_SE differ\n')
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            f.parse_diff(diff)

    def test_an_unreadable_hunk_header_is_refused(self):
        """A `@@` line the header pattern does not read produced no range and
        said nothing. If it was the only hunk inside LC_COLLATE the file was
        not flagged, at exit 0. Measured by injection on f7fa3f9."""
        diff = self.DIFF.replace('@@ -200 +201 @@', '@@ -200 +201 garbled @@')
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            f.parse_diff(diff)

    def test_a_path_with_two_sections_keeps_the_ranges_of_both(self):
        """A locale replaced by a symlink is a deletion plus a creation, two
        sections under one path. Keeping only the last one kept `@@ -0,0` and
        lost the deletion's range over the whole file -- the block included --
        so the file was judged 'other'. No typechange in the corpus's
        history; a --diff-file that repeats a path has the same shape."""
        diff = ('diff --git a/localedata/locales/sv_SE '
                'b/localedata/locales/sv_SE\n'
                'deleted file mode 100644\n'
                '@@ -1,8 +0,0 @@\n-a\n'
                'diff --git a/localedata/locales/sv_SE '
                'b/localedata/locales/sv_SE\n'
                'new file mode 120000\n'
                '@@ -0,0 +1 @@\n+sv_FI\n')
        self.assertEqual(f.parse_diff(diff),
                         {'localedata/locales/sv_SE': [(1, 8), (0, 0)]})

    def test_a_pure_rename_still_parses_to_no_range(self):
        """Control: a rename with no content change has no hunk, and that is
        an answer, not a failure."""
        diff = ('diff --git a/localedata/locales/aa_ER@saaho '
                'b/localedata/locales/ssy_ER\n'
                'similarity index 100%\n'
                'rename from localedata/locales/aa_ER@saaho\n'
                'rename to localedata/locales/ssy_ER\n')
        self.assertEqual(f.parse_diff(diff),
                         {'localedata/locales/aa_ER@saaho': []})

    def test_split_hunks_keeps_content_lines_and_stops_at_the_next_file(self):
        """Context lines are kept -- classify_body tracks the open-comment
        state through them -- and the next file's `diff --git`/`---`/`+++`
        header is not one of them."""
        hunks = d.split_hunks(self.DIFF)
        self.assertEqual(len(hunks), 3)
        for _, body in hunks:
            for line in body:
                self.assertIn(line[:1], ('+', '-', ' '))
        joined = [ln for _, body in hunks for ln in body]
        for header in ('--- a/localedata/locales/or_IN',
                       '+++ b/localedata/locales/or_IN'):
            self.assertNotIn(header, joined,
                             'a file header leaked into a hunk body')

    def test_a_comment_closing_in_context_is_read_through_split_hunks(self):
        """The call site, not the helper: classify_body can only see a context
        line if split_hunks kept it. Tested separately because dropping them
        again in split_hunks leaves every classify_body test green -- they
        hand it a body of their own."""
        diff = ('diff --git a/locale/x.c b/locale/x.c\n'
                'index 111..222 100644\n'
                '--- a/locale/x.c\n'
                '+++ b/locale/x.c\n'
                '@@ -10,6 +10,6 @@ static void f (void)\n'
                '-  /* old comment\n'
                '+  /* new comment\n'
                '     still the comment  */\n'
                '+  new_code ();\n'
                '   return;\n')
        [(_, body)] = d.split_hunks(diff)
        self.assertIn('     still the comment  */', body,
                      'the context line was dropped before the filter saw it')
        marked = d.classify_body(body)
        self.assertEqual([ln for ln, _ in marked],
                         ['-  /* old comment', '+  /* new comment',
                          '+  new_code ();'])
        self.assertFalse(marked[2][1],
                         'the hunk would be filtered as comment/licence')

    def test_split_hunks_keeps_a_changed_line_that_starts_with_two_signs(self):
        """The old filter was `not startswith(('+++', '---'))`, which also
        discarded a changed line whose own content began with `++` or `--`.
        A discarded line is one the noise filter never sees, and a hunk whose
        remaining lines are all comment is dropped whole."""
        diff = ('diff --git a/locale/x.c b/locale/x.c\n'
                'index 111..222 100644\n'
                '--- a/locale/x.c\n'
                '+++ b/locale/x.c\n'
                '@@ -1,3 +1,3 @@\n'
                ' /* a comment */\n'
                '---argc;\n'
                '+++idx;\n')
        [(_, body)] = d.split_hunks(diff)
        self.assertIn('---argc;', body)
        self.assertIn('+++idx;', body)
        marked = d.classify_body(body)
        self.assertTrue(any(not noise for _, noise in marked),
                        'a hunk of real code was left with nothing to mark')

    def test_a_body_line_that_is_neither_content_nor_a_new_file_is_an_error(self):
        """Ending a body on anything unrecognised drops every line after it,
        and a hunk with nothing left is filtered as comment. Measured with
        `color.diff=always`: every body line began with an escape."""
        diff = ('@@ -1,2 +1,2 @@\n'
                '\x1b[32m+  code ();\x1b[m\n'
                ' ctx\n')
        with self.assertRaises(SystemExit):
            d.split_hunks(diff)

    def test_split_hunks_keeps_the_rest_of_a_hunk_after_a_no_newline_marker(self):
        r"""`\ No newline at end of file` sits between the two sides of a
        hunk. Stopping there would drop the `+` side."""
        diff = ('@@ -1 +1 @@\n'
                '-old\n'
                '\\ No newline at end of file\n'
                '+new\n')
        [(_, body)] = d.split_hunks(diff)
        self.assertEqual(body, ['-old', '+new'])


class OneDiffForBothQuestions(unittest.TestCase):
    """split_raw: step 2 asks git for its list of changed files and for the
    -U0 patch in one `--patch-with-raw` diff, where it used to run two. The
    list is what classifies every file as modified, added, deleted or
    renamed, so a line it cannot read must not become a modified file, and a
    patch with no list above it must not read as nothing changed."""

    RAW = (':100644 100644 1a2b3c4 5d6e7f8 M\tlocaledata/locales/sv_SE\n'
           ':100644 100644 0a1b2c3 4d5e6f7 R087\t'
           'localedata/locales/aa_ER@saaho\tlocaledata/locales/ssy_ER\n'
           ':000000 100644 0000000 9a8b7c6 A\tlocaledata/locales/ckb_IQ\n'
           '\n')

    def refused(self, text):
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            f.split_raw(text)

    def test_the_list_is_the_name_status_and_the_rest_is_the_patch(self):
        status, patch = f.split_raw(self.RAW + DiffParsing.DIFF)
        self.assertEqual(status, [
            'M\tlocaledata/locales/sv_SE',
            'R087\tlocaledata/locales/aa_ER@saaho\tlocaledata/locales/ssy_ER',
            'A\tlocaledata/locales/ckb_IQ'])
        self.assertEqual(patch, DiffParsing.DIFF)

    def test_nothing_changed_is_two_empty_answers(self):
        self.assertEqual(f.split_raw(''), ([], ''))

    def test_a_list_line_of_another_shape_is_refused(self):
        for bad in (':100644 100644 1a2b3c4 5d6e7f8 M localedata/locales/x\n',
                    ':100644 M\tlocaledata/locales/x\n'):
            with self.subTest(line=bad):
                self.refused(bad + '\n' + DiffParsing.DIFF)

    def test_a_list_cut_off_inside_a_line_is_refused(self):
        self.refused(':100644 100644 1a2b3c4 5d6e7f8 M\tlocaledata/locales/x')

    def test_a_patch_with_no_list_above_it_is_refused(self):
        self.refused(DiffParsing.DIFF)


class KnownBackportedFromTheListing(unittest.TestCase):
    """in_listing answers, from the recursive listing step 2 already read,
    whether a file is at that path or under it, where step 2 used to ask
    `git ls-tree <tag> -- localedata/locales/C`. Measured in a scratch
    repository with git 2.54: ls-tree names a directory by its own path, and
    the recursive listing shows it as the files under it, quoted when their
    names need it."""

    C = 'localedata/locales/C'

    def test_a_file_at_the_path_is_there(self):
        self.assertTrue(f.in_listing(self.C, ['localedata/locales/C',
                                              'localedata/locales/sv_SE']))

    def test_a_name_that_only_starts_the_same_is_not(self):
        self.assertFalse(f.in_listing(self.C, ['localedata/locales/C.UTF-8',
                                               'localedata/locales/CN']))

    def test_a_directory_is_there_through_the_files_under_it(self):
        self.assertTrue(f.in_listing(self.C, ['localedata/locales/C/x']))
        self.assertTrue(f.in_listing(self.C,
                                     ['"localedata/locales/C/\\303\\251"']))

    def test_a_name_with_a_line_separator_in_it_stays_one_name(self):
        """Under core.quotePath=false git prints U+2028 as it is, and
        splitlines() cut `x<U+2028>localedata/locales/C` in two, listing a C
        that is not there: step 2 dropped its C.UTF-8 note
        (false-negative-reviewer; measured with git 2.54)."""
        names = [f'{g.LOCALES_DIR}/loc_{i:03d}'
                 for i in range(g.MIN_LOCALE_FILES)]
        names.append(f'{g.LOCALES_DIR}/x\u2028{self.C}')

        class R:
            stdout = ('\n'.join(names) + '\n').encode()

        with mock.patch.object(g, 'run_git', lambda *a, **k: R()):
            listed = g.list_locale_files('/no/clone/here', 't')
        self.assertEqual(listed, names)
        self.assertFalse(f.in_listing(self.C, listed))


class LocaleSource(unittest.TestCase):
    """glibc's rule from a locale name to the file it is built from
    (localedata/Makefile builds locales/<name> with everything from the first
    dot up to any @ removed). Backlog 13.1: every spelling of a locale reaches
    one name."""

    def test_the_codeset_goes_and_the_modifier_stays(self):
        # The Makefile's sed takes everything from the first dot up to the
        # next @, or to the end. So a dot after the @ goes when it is the
        # first dot (glibc-2.12's SUPPORTED has tt_RU@iqtelif.UTF-8) and
        # stays when one came before it. No name has two dots today.
        for name, source in (('sv_SE.utf8', 'sv_SE'), ('sv_SE.UTF-8', 'sv_SE'),
                             ('sv_SE.iso885915', 'sv_SE'),
                             ('sv_FI.iso885915@euro', 'sv_FI@euro'),
                             ('ca_ES.UTF-8@valencia', 'ca_ES@valencia'),
                             ('C.utf8', 'C'), ('sv_SE', 'sv_SE'),
                             ('swedish', 'swedish'),
                             ('tt_RU@iqtelif.UTF-8', 'tt_RU@iqtelif'),
                             ('a.b.c', 'a'), ('sv_SE.utf8@x.y', 'sv_SE@x.y')):
            with self.subTest(name=name):
                self.assertEqual(g.locale_source(name), source)


class AliasesOf(unittest.TestCase):
    """Which aliases go on the list beside the locales they name."""

    ALIASES = {'swedish': 'sv_SE.ISO-8859-1', 'ko_KR': 'ko_KR.eucKR',
               'no_NO': 'nb_NO.ISO-8859-1', 'bokm\ufffdl': 'nb_NO.ISO-8859-1',
               'thai': 'th_TH.TIS-620'}

    def test_an_alias_is_listed_with_the_locale_it_is_built_from(self):
        """no_NO is the case the rule alone gets wrong: it is nb_NO."""
        self.assertEqual(g.aliases_of({'sv_SE', 'nb_NO', 'ko_KR'},
                                      self.ALIASES),
                         {'swedish': 'sv_SE', 'no_NO': 'nb_NO'})

    def test_an_alias_named_as_its_own_locale_is_not_added_again(self):
        self.assertNotIn('ko_KR', g.aliases_of({'ko_KR'}, self.ALIASES))

    def test_a_name_that_is_not_ascii_is_left_out_and_named(self):
        """glibc-2.17's locale.alias spells bokmal and francais in Latin-1
        bytes, which reach this module as U+FFFD."""
        self.assertNotIn('bokm\ufffdl', g.aliases_of({'nb_NO'}, self.ALIASES))
        self.assertEqual(g.non_ascii_alias_targets({'nb_NO'}, self.ALIASES),
                         ['nb_NO'])
        self.assertEqual(g.non_ascii_alias_targets({'sv_SE'}, self.ALIASES),
                         [])


class TheTemplateComparesByGlibcsRule(unittest.TestCase):
    """sql/collation_confirmation_template.sql reads each collation's locale,
    and the database's own, as the locale it is built from, and its version
    query matches the list's names by the same rule (backlog 13.1). Its
    regular expression must be glibc_locale_data.locale_source, so this
    reads it from the template and runs it over every collcollate PostgreSQL
    imported on the three machines (tests/locale_order/*.pg_collation.txt),
    plus the spellings a database's own locale takes. regexp_replace without
    flags replaces the first match only, hence count=1."""

    def test_the_template_s_regex_is_the_rule(self):
        path = os.path.join(_harness.REPO_ROOT, 'sql',
                            'collation_confirmation_template.sql')
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        found = re.findall(r"regexp_replace\([^;]*?'([^']+)', ''\)", text,
                           re.S)
        self.assertEqual(len(found), 3, found)
        self.assertEqual(len(set(found)), 1, found)
        # Read from what the collation loads, never from its name: "sv_SE"
        # is three collations, and CREATE COLLATION mine (locale = 'sv_SE.utf8')
        # is named mine.
        self.assertIn("THEN d.datcollate\n                           ELSE "
                      "c.collcollate END, '", text)
        # A server with standard_conforming_strings off reads a backslash in a
        # literal as an escape, and '\.' then matches any first character:
        # every name came back empty, measured on PostgreSQL 18.6.
        self.assertNotIn('\\', found[0])
        # glibc reads an alias in any case (a database created as Swedish
        # works and keeps that spelling, measured), so the version query
        # lowers both sides.
        # Both sides of the version query go through the rule, and lower()
        # runs under C: under a Turkish locale lower('I') is a dotless i and
        # or_IN matched nothing (measured on PostgreSQL 18.6).
        self.assertIn("lower(regexp_replace(c.collcollate, '", text)
        self.assertIn("lower(regexp_replace(n COLLATE \"C\", '", text)
        self.assertIn("FROM unnest(ARRAY['<LOCALE>']) AS n\nLEFT JOIN "
                      "pg_collation c", text)
        # The ISO_8859 spellings are where glibc's rule and PostgreSQL's
        # alias rule (`\.[A-Za-z0-9-]*`) part: glibc normalises the whole
        # codeset, so sv_SE.ISO_8859-1 loads sv_SE.iso88591.
        names = ['sv_SE.UTF-8', 'sv_FI.ISO-8859-15@euro', 'th_TH.TIS-620',
                 'sv_SE.ISO_8859-1', 'sv_FI.ISO_8859-15@euro']
        for machine in ('rhel8', 'rhel9', 'rhel10'):
            with open(os.path.join(_harness.TESTS_DIR, 'locale_order',
                                   f'{machine}.pg_collation.txt'),
                      encoding='utf-8') as fh:
                names += [ln.rstrip('\n').split('|')[2] for ln in fh if ln.strip()]
        self.assertGreater(len(names), 3000)
        wrong = [n for n in names
                 if re.sub(found[0], '', n, count=1) != g.locale_source(n)]
        self.assertEqual(wrong, [])

    @staticmethod
    def template():
        with open(os.path.join(_harness.REPO_ROOT, 'sql',
                               'collation_confirmation_template.sql'),
                  encoding='utf-8') as fh:
            return fh.read()

    @staticmethod
    def code(text):
        """`text` without its `--` comments, read as PostgreSQL's scanner
        reads them (src/backend/parser/scan.l, `comment`): from `--` to the
        end of the line, outside a quoted literal."""
        return re.sub(r"('(?:[^']|'')*')|--[^\n]*",
                      lambda m: m.group(1) or '', text)

    def statement(self, text, anchor):
        """The statement that starts at `anchor`, up to its `;`, without its
        comments. A `;` in a comment or a literal is not the statement's end:
        a cut that stopped there hid what follows from every assertion that
        something is absent. A missing or doubled anchor, or a missing `;`, is
        refused. What psql would not run at all is the next test's."""
        self.assertEqual(text.count(anchor), 1, anchor)
        body = self.code(text[text.index(anchor):])
        ends = [m.start() for m in re.finditer(r"'(?:[^']|'')*'|;", body)
                if m.group(0) == ';']
        self.assertTrue(ends, f'no ; after {anchor!r}')
        return body[:ends[0]]

    def test_psql_runs_every_statement(self):
        """A statement inside a block comment, or after a `\\q`, is never run:
        psql's scanner reads a block comment in an exclusive state where `;`
        and backslash commands are only text (src/fe_utils/psqlscan.l, `%x
        xc`, REL_18_6). Both machines then print nothing for it, the diff
        agrees, and every assertion here still reads the statement. So the
        template's code holds no block comment, no dollar quote this file
        cannot read, and no psql command but `\\echo`."""
        code = re.sub(r"'(?:[^']|'')*'", "''", self.code(self.template()))
        self.assertNotRegex(code, r'/\*|\$')
        self.assertEqual(set(re.findall(r'\\[A-Za-z]+', code)), {'\\echo'})

    def test_the_version_query_is_the_one_measured(self):
        """The version query, word for word, as it was run on PostgreSQL 18.6
        (backlog 13.1). It starts from the list, so a name that matches no
        collation still prints a row: a locale this database does not have,
        or <LOCALE> left unreplaced. Both sides go through glibc's rule, and
        lower() under C, so a spelling or an alias in another case matches
        and a Turkish database loses nothing. Listing what must not appear did
        not hold: a WHERE, a second JOIN, a LIMIT, a comma join to an empty
        set each emptied a row and passed in turn. A change to this statement
        is measured on PostgreSQL again and then written here."""
        self.assertEqual(
            flat(self.statement(self.template(), 'SELECT n AS listed')),
            "SELECT n AS listed, c.collname, c.collcollate, "
            "pg_encoding_to_char(c.collencoding) AS encoding, c.collversion "
            "FROM unnest(ARRAY['<LOCALE>']) AS n "
            "LEFT JOIN pg_collation c ON c.collprovider = 'c' "
            "AND lower(regexp_replace(c.collcollate, '[.][^@]*', '')) "
            "= lower(regexp_replace(n COLLATE \"C\", '[.][^@]*', '')) "
            "ORDER BY n, c.collname")

    def test_the_inventories_show_the_locale(self):
        """The three inventories print each object's locale beside its
        collation's name, read through the view from collcollate. The name
        cannot be compared with the list: CREATE COLLATION mine (locale =
        'sv_SE.utf8') shows as mine. The view is named five times: where it is
        made, the three inventories and the CHECK/EXCLUDE review list, which
        prints no collation. A new statement that reads it changes that
        count, and has to be added here."""
        text = self.template()
        self.assertEqual(
            len(re.findall(r'(?i)\bexposed_collation\b', self.code(text))), 5)
        self.assertEqual(text.count('x.effective_collation'), 3)
        for header in ("'--- indexes on non-C/POSIX libc collations ---'",
                       "'--- partitioned tables keyed on a non-C/POSIX libc "
                       "collation (REINDEX does NOT fix these) ---'",
                       "'--- columns using a non-C/POSIX libc collation ---'"):
            with self.subTest(header=header):
                stmt = self.statement(text, '\\echo ' + header)
                self.assertIn('\nFROM ', stmt)
                columns = flat(stmt.split('\nFROM ', 1)[0])
                self.assertIn('x.effective_collation', columns)
                self.assertRegex(columns, r'\bx\.locale\b')

    def test_the_view_is_the_one_measured(self):
        """The view the three inventories read, word for word, as it was run
        on PostgreSQL 18.6 (backlog 13.1).
        - C and POSIX are recognised by collcollate, as PostgreSQL does
          (pg_locale.c at REL_13_23 to REL_17_11, pg_locale_libc.c at
          REL_18_6). By name, the view kept ucs_basic, libc with collcollate
          C in 13 to 16, and dropped a collation named C that loads a real
          locale.
        - The second arm brings in every column with no COLLATE of its own,
          the database default, which the template calls MOST text columns.
        - `locale` is read from collcollate and datcollate, never collname.
        Listing what must not appear did not hold: a condition added beside
        the two arms emptied the inventories, four ways, and passed. A change
        to this statement is measured on PostgreSQL again and then written
        here."""
        self.assertEqual(
            flat(self.statement(
                self.template(),
                'CREATE OR REPLACE TEMP VIEW exposed_collation AS')),
            "CREATE OR REPLACE TEMP VIEW exposed_collation AS "
            "SELECT c.oid AS colloid, c.collname, "
            "CASE WHEN c.collprovider = 'd' "
            "THEN 'database default -> ' || d.datcollate "
            "ELSE c.collname END AS effective_collation, "
            "regexp_replace(CASE WHEN c.collprovider = 'd' THEN d.datcollate "
            "ELSE c.collcollate END, '[.][^@]*', '') AS locale "
            "FROM pg_collation c CROSS JOIN pg_database d "
            "WHERE d.datname = current_database() "
            "AND ( (c.collprovider = 'c' AND c.collcollate NOT IN "
            "('C', 'POSIX')) "
            "OR (c.collprovider = 'd' AND d.datlocprovider = 'c' "
            "AND d.datcollate NOT IN ('C', 'POSIX')) )")


class StepFourWithoutRanges(unittest.TestCase):
    """The path of step 4 where nothing uses an ellipsis range writes its list
    too, and says what the other path says: the reading rule, and which
    aliases it leaves out because their name is not ASCII (backlog 13.1).
    Driven directly, because no pinned tag reaches it with such an alias."""

    def test_it_states_the_rule_and_the_aliases_it_leaves_out(self):
        texts = {'nb_NO': _harness.locale_file('copy "no_such_locale"'),
                 'xx_XX': _harness.locale_file('order_start forward',
                                               '<U0041>', 'order_end')}
        aliases = {'norwegian': 'nb_NO.ISO-8859-1',
                   'bokm\ufffdl': 'nb_NO.ISO-8859-1'}
        out = io.StringIO()
        with mock.patch.object(g, 'write_list', return_value='/x') as wl, \
                contextlib.redirect_stdout(out):
            fa.report(texts, {}, 'fake', 'list.txt', [], aliases=aliases)
        text = out.getvalue()
        self.assertIn('Locales, not spellings', text)
        self.assertIn('not listed: an alias of nb_NO whose name is not ASCII',
                      text)
        self.assertEqual(wl.call_args[0][1], ['nb_NO', 'norwegian', 'xx_XX'])


class ByteOrderListName(unittest.TestCase):
    """Step 4's second list is named after its full list and can never be
    that list (backlog 13.7)."""

    def test_named_after_the_full_list(self):
        self.assertEqual(fa.byte_order_list_name('step4_exposed_locales.txt'),
                         'step4_byte_order_locales.txt')
        self.assertEqual(
            fa.byte_order_list_name('step4_exposed_locales.b-1.txt'),
            'step4_byte_order_locales.b-1.txt')

    def test_never_the_full_list_itself(self):
        for name in ('list.txt', 'step4_byte_order_locales.txt', 'x'):
            with self.subTest(name=name):
                self.assertNotEqual(fa.byte_order_list_name(name), name)


class CommentChar(unittest.TestCase):
    def test_defaults_to_percent(self):
        self.assertEqual(g.comment_char('LC_COLLATE\n'), '%')

    def test_reads_a_declared_one(self):
        self.assertEqual(g.comment_char('comment_char #\nLC_COLLATE\n'), '#')


class DistroDiff(unittest.TestCase):
    """diff_distro_locales.py: the distro-versus-upstream classifier.

    Two of these guard silent-false-negative paths found while designing the
    script -- both would have reported a clean result on data that differs.
    """

    def test_identical_bytes_are_identical(self):
        b = collate('order_start forward', '<a>').encode()
        self.assertEqual(dd.classify_distro_diff(b, b), 'identical')

    def test_a_change_inside_the_block_is_a_collation_finding(self):
        up = collate('order_start forward', '<a>').encode()
        node = collate('order_start forward', '<b>').encode()
        self.assertEqual(dd.classify_distro_diff(node, up), 'collate')

    def test_a_change_outside_the_block_is_not(self):
        """The Oriya/Odia case: or_IN differs from upstream 2.28 by one line in
        LC_IDENTIFICATION, and must not read as a backported collation change."""
        up = collate('<a>') + 'language "Oriya"\n'
        node = collate('<a>') + 'language "Odia"\n'
        self.assertEqual(dd.classify_distro_diff(node.encode(), up.encode()),
                         'other')

    def test_LC_COLLATE_at_byte_zero_still_has_a_block(self):
        """glibc <= 2.23 writes the three master templates with LC_COLLATE on
        the first byte. collate_block's regex needs a preceding newline and
        returns None there, which would file iso14651_t1_common -- the highest
        fan-in file in the corpus -- under 'no block on either side'."""
        up = 'LC_COLLATE\n<a>\nEND LC_COLLATE\n'
        node = 'LC_COLLATE\n<b>\nEND LC_COLLATE\n'
        self.assertIsNotNone(dd.collate_text(up))
        self.assertEqual(dd.classify_distro_diff(node.encode(), up.encode()),
                         'collate')

    def test_whitespace_inside_the_block_still_counts(self):
        """Conservative on purpose: the script cannot tell a cosmetic patch
        from a meaningful one, so it reports and prints the diff."""
        up = collate('<a>').encode()
        node = collate('<a> ').encode()
        self.assertEqual(dd.classify_distro_diff(node, up), 'collate')

    def test_whitespace_at_the_block_edges_still_counts(self):
        """Guards against normalising the block before comparing -- a .strip()
        would erase a difference on the LC_COLLATE or END LC_COLLATE line
        itself, which is inside the block and therefore inside the answer."""
        up = 'LC_COLLATE\n<a>\nEND LC_COLLATE\n'
        node = 'LC_COLLATE\n<a>\nEND LC_COLLATE   \n'
        self.assertEqual(dd.classify_distro_diff(node.encode(), up.encode()),
                         'collate')

    def test_a_comment_inside_the_block_still_counts(self):
        up = collate('<a>').encode()
        node = collate('% distro note', '<a>').encode()
        self.assertEqual(dd.classify_distro_diff(node, up), 'collate')

    def test_block_on_one_side_only_is_a_collation_difference(self):
        with_block = collate('<a>').encode()
        without = b'comment_char %\nLC_TIME\nEND LC_TIME\n'
        self.assertEqual(dd.classify_distro_diff(with_block, without), 'collate')
        self.assertEqual(dd.classify_distro_diff(without, with_block), 'collate')

    def test_no_block_on_either_side_cannot_affect_sort_order(self):
        up = b'comment_char %\nLC_CTYPE\ntranslit_start\nEND LC_CTYPE\n'
        node = b'comment_char %\nLC_CTYPE\ntranslit_end\nEND LC_CTYPE\n'
        self.assertEqual(dd.classify_distro_diff(node, up), 'no-collate')

    def test_a_non_utf8_byte_is_not_swallowed(self):
        """read_blobs decodes with errors='replace'. Two files differing only
        in a byte that decodes to U+FFFD would compare EQUAL through it, which
        is a false negative in the reassuring direction. This compares bytes."""
        up = collate('<a>').encode() + b'\x80'
        node = collate('<a>').encode() + b'\x81'
        self.assertNotEqual(up, node)
        self.assertEqual(up.decode('utf-8', 'replace'),
                         node.decode('utf-8', 'replace'))   # the trap itself
        self.assertNotEqual(dd.classify_distro_diff(node, up), 'identical')


class ReadingCharsReadsLikeGlibc(unittest.TestCase):
    """The comment and escape characters a file is read with, found the way
    glibc's reader finds the directives, not by their spelling (review of
    backlog 13.7). get_ident keeps the character after an escape inside a
    word and lr_next joins a line ending in the escape to the next, so these
    are the directive to localedef, between two categories, where it acts."""

    BASE = collate('<a>')

    def hidden(self, line):
        return self.BASE.replace('\nLC_COLLATE\n', f'\n{line}LC_COLLATE\n', 1)

    def test_a_plain_file_settles(self):
        self.assertEqual(g.reading_chars(self.BASE), ('%', '/'))

    def test_a_directive_spelt_with_the_escape_does_not_settle(self):
        for line in ('comment_c/har #\n', 'comment_/\nchar #\n',
                     'escape_c/har \\\n'):
            with self.subTest(line=line):
                self.assertIsNone(g.reading_chars(self.hidden(line)))

    def test_a_continued_comment_line_drops_only_itself(self):
        """lr_token drops a physical line that opens with the comment
        character, and when that line is continued the logical line goes on
        with the next one: the directive below it is read. Without the drop
        the two would be read as one comment and the directive missed."""
        self.assertEqual(g._directive_words(['# note \\', 'comment_char %']),
                         ['comment_char'])
        self.assertEqual(g._directive_words(['# note', 'comment_char %']),
                         ['comment_char'])
        # lr_token skips white space before it looks for the comment
        # character, and that skip runs on into a continued line (round 2 of
        # false-negative-reviewer: either form, read as text, hid the
        # directive and left the characters settled and wrong).
        for lines in (['  # note \\', 'comment_char %'],
                      ['   \\', '# note \\', 'comment_char %']):
            with self.subTest(lines=lines):
                self.assertEqual(g._directive_words(lines), ['comment_char'])
        for line in ('  % note /\ncomment_c/har #\n',
                     '   /\n% note /\ncomment_c/har #\n'):
            with self.subTest(line=line):
                self.assertIsNone(g.reading_chars(self.hidden(line)))

    def test_the_control_a_commented_directive_settles(self):
        """Under `comment_char %` this line is a comment to glibc too."""
        self.assertEqual(g.reading_chars(self.hidden('% comment_char #\n')),
                         ('%', '/'))

    def test_a_hidden_directive_is_a_rules_change_in_step_2(self):
        new = self.hidden('comment_c/har #\n')
        # -U0 numbering: one line inserted after old line 3, outside the
        # block, which opens on line 4.
        self.assertEqual(f.classify_change(self.BASE, new, [(3, 0)]),
                         'reading')

    def test_and_in_steps_6_to_8(self):
        node = self.hidden('comment_c/har #\n')
        self.assertEqual(dd.classify_distro_diff(node.encode(),
                                                 self.BASE.encode()), 'collate')

    def test_an_unsettled_side_is_named_as_such(self):
        """describe_reading's other branch: the two lines must not claim the
        same settled characters under a header saying they differ."""
        node = self.hidden('comment_c/har #\n')
        lines = dd.collate_diff_lines(self.BASE, node, 'up', 'node')
        self.assertEqual(lines, [
            'read with comment_char % and escape_char / at up',
            'read with characters this tool cannot settle at node'])


class ReadOtherwise(unittest.TestCase):
    """Every changed file read with other characters, whatever its verdict
    (review of backlog 13.7): a hunk inside the block made the verdict
    'collate' before the characters were compared, and step 2 then listed
    only the changed lines' characters as if they were all that moved."""

    OLD = collate('<a>')

    def test_a_block_and_its_characters_changed_together(self):
        new = collate('<b>').replace('comment_char %', 'comment_char #', 1)
        self.assertEqual(
            f.read_otherwise(['p'], [], {'p': self.OLD}, {'p': new}, {}),
            ['p'])

    def test_the_reading_verdict_is_kept(self):
        self.assertEqual(
            f.read_otherwise(['p'], ['p'], {'p': self.OLD}, {'p': self.OLD},
                             {}), ['p'])

    def test_a_block_with_no_new_text_counts(self):
        self.assertEqual(f.read_otherwise(['p'], [], {'p': self.OLD}, {}, {}),
                         ['p'])

    def test_a_renamed_file_is_read_under_its_new_name(self):
        new = self.OLD.replace('comment_char %', 'comment_char #', 1)
        self.assertEqual(
            f.read_otherwise(['p'], [], {'p': self.OLD}, {'q': new},
                             {'p': 'q'}), ['p'])

    def test_the_control_same_characters_are_not_listed(self):
        self.assertEqual(
            f.read_otherwise(['p'], [], {'p': self.OLD},
                             {'p': collate('<b>')}, {}), [])

    def test_a_file_without_an_old_block_is_not_listed(self):
        """A gained block is its own `!!`; this one is about reading."""
        no_block = 'comment_char %\nLC_CTYPE\nEND LC_CTYPE\n'
        self.assertEqual(
            f.read_otherwise(['p'], [], {'p': no_block}, {'p': self.OLD}, {}),
            [])


class LcCollateUnread(unittest.TestCase):
    """Backlog 13.18: what in a file can stop glibc reading its LC_COLLATE
    block as written, read on linereader at 2.28, 2.34 and 2.39
    (lc_collate_unread's docstring has the lines). No locale file at
    glibc-2.12..2.42 holds any of these, so each is injected."""

    @staticmethod
    def text(*above, head=('comment_char %', 'escape_char /')):
        return '\n'.join([*head, '', 'LC_CTYPE', 'copy "i18n"',
                          'END LC_CTYPE', *above, 'LC_COLLATE',
                          'copy "iso14651_t1"', 'END LC_COLLATE', ''])

    @staticmethod
    def never():
        raise AssertionError('early() asked for a text without the byte')

    def test_the_control_a_plain_file_has_no_reason(self):
        self.assertIsNone(g.lc_collate_unread(self.text(), self.never))

    def test_a_line_ending_in_the_escape_joins_lc_collate(self):
        for text in (self.text().replace('END LC_CTYPE',
                                         'END LC_CTYPE % a note /', 1),
                     self.text('x /')):
            with self.subTest(text=text):
                self.assertIn('ends in the escape character',
                              g.lc_collate_unread(text, self.never))

    def test_the_control_a_comment_line_ending_in_the_escape(self):
        """lr_token drops a line opening with the comment character whole,
        continued or not (linereader.c:221-229@2.39): LC_COLLATE below it
        is read."""
        self.assertIsNone(g.lc_collate_unread(self.text('% a note /'),
                                              self.never))

    def test_the_escape_is_the_one_in_force(self):
        """`\\` is glibc's default; under `escape_char /` it is a plain
        character, and a line ending in it joins nothing."""
        text = self.text('x \\')
        self.assertIsNone(g.lc_collate_unread(text, self.never))
        default = self.text('x \\', head=('comment_char %',))
        self.assertIn('ends in the escape character',
                      g.lc_collate_unread(default, self.never))

    def test_unsettled_characters_count_both_escapes(self):
        """A directive after a category is a shape reading_chars refuses;
        the escape in force is then not known, and both count. So do both
        comment characters, and a line that opens with either and ends in
        an escape is joined, not dropped: `#` may be no comment at all."""
        text = self.text('comment_char #', '% a note \\')
        self.assertIsNone(g.reading_chars(text))
        self.assertIn('ends in the escape character',
                      g.lc_collate_unread(text, self.never))

    def test_a_line_of_only_the_escape_ends_the_file(self):
        """lr_next leaves an empty buffer and lr_getc answers EOF from then
        on, wherever above the line it sits."""
        text = self.text('/', '% after it')
        self.assertIn('holds only the escape character',
                      g.lc_collate_unread(text, self.never))

    def test_a_0x1a_above_ends_the_file_before_2_36(self):
        text = self.text('% \x1a')
        for early in (True, None):
            with self.subTest(early=early):
                self.assertIn('0x1A or 0xFF',
                              g.lc_collate_unread(text, lambda: early))
        self.assertIsNone(g.lc_collate_unread(text, lambda: False))

    def test_a_0xff_above_ends_the_file_before_2_36(self):
        """Found through TEXTS_WITH_0XFF: decoding made it a U+FFFD."""
        text = g.decoded(self.text('% \udcff').encode('utf-8',
                                                      'surrogateescape'))
        self.addCleanup(g.TEXTS_WITH_0XFF.discard, text)
        self.assertIn(text, g.TEXTS_WITH_0XFF)
        self.assertIn('0x1A or 0xFF', g.lc_collate_unread(text, lambda: True))
        self.assertIsNone(g.lc_collate_unread(text, lambda: False))

    def test_the_control_a_u_fffd_that_was_not_0xff(self):
        """Another byte that is not UTF-8 decodes to the same U+FFFD and
        ends nothing."""
        text = g.decoded(self.text('% \udcfe').encode('utf-8',
                                                      'surrogateescape'))
        self.assertNotIn(text, g.TEXTS_WITH_0XFF)
        self.assertIsNone(g.lc_collate_unread(text, self.never))

    def test_a_byte_inside_the_block_stops_it_before_2_36(self):
        """collate_read stops at the EOF lr_getc reads there
        (ld-collate.c:2662@2.34), and every rule after the byte is lost
        (review of 13.18, finding 2)."""
        text = self.text().replace('copy "iso14651_t1"',
                                   'copy "iso14651_t1"\n\x1a', 1)
        self.assertIn('0x1A or 0xFF', g.lc_collate_unread(text,
                                                          lambda: True))
        self.assertIsNone(g.lc_collate_unread(text, lambda: False))

    def test_the_control_a_byte_below_the_block(self):
        text = self.text() + '\x1a\n'
        self.assertIsNone(g.lc_collate_unread(text, self.never))
        self.assertFalse(g.byte_ends_it_early(text))

    def test_a_section_left_open_above_holds_lc_collate(self):
        """Review of 13.18, finding 1: a category reader takes LC_COLLATE
        as a syntax error inside it and closes on `END LC_COLLATE`
        (ld-identification.c:387-403, ld-ctype.c:2820-2835@2.39). The END
        is commented out, deleted, or swallowed by a `copy` line that ends
        in the escape (lr_ignore_rest, ld-ctype.c:2143@2.39)."""
        plain = self.text()
        for text in (plain.replace('END LC_CTYPE', '% END LC_CTYPE', 1),
                     plain.replace('END LC_CTYPE\n', '', 1),
                     plain.replace('copy "i18n"', 'copy "i18n" /', 1),
                     self.text('LC_IDENTIFICATION', 'title "x"')):
            with self.subTest(text=text):
                self.assertIn('has no END line glibc reads',
                              g.lc_collate_unread(text, self.never))

    def test_the_control_any_end_closes_the_section(self):
        """glibc closes on the END token whatever follows it, and warns."""
        plain = self.text()
        for text in (plain.replace('END LC_CTYPE', 'END LC_CTYP', 1),
                     plain.replace('END LC_CTYPE', 'END LC_CTYPE % note', 1),
                     plain.replace('END LC_CTYPE', '  END LC_CTYPE', 1)):
            with self.subTest(text=text):
                self.assertIsNone(g.lc_collate_unread(text, self.never))

    TRANSLIT = ('LC_CTYPE\ncopy "i18n"\ntranslit_start\n'
                'include "translit_combining";""\ntranslit_end\nEND LC_CTYPE')

    def test_a_translit_block_left_open_holds_lc_collate(self):
        """Round 2 of the 13.18 review, finding 1: inside translit_start,
        glibc reads entries until translit_end or the end of the file, END
        LC_CTYPE included (ld-ctype.c:2625-2657@2.39). The translit_end is
        commented out, deleted, or swallowed by an include line that ends
        in the escape."""
        plain = self.text().replace('LC_CTYPE\ncopy "i18n"\nEND LC_CTYPE',
                                    self.TRANSLIT, 1)
        self.assertIsNone(g.lc_collate_unread(plain, self.never),
                          'the control: a translit block closed')
        for text in (plain.replace('translit_end', '% translit_end', 1),
                     plain.replace('translit_end\n', '', 1),
                     plain.replace(';""\n', ';"" /\n', 1)):
            with self.subTest(text=text):
                self.assertIn('inside its translit_start',
                              g.lc_collate_unread(text, self.never))

    def test_the_directive_lines_are_read_before_their_escape(self):
        """Round 2, finding 2: glibc reads the `escape_char /` line with the
        escape in force before it, `\\`, so it is not joined to the
        LC_CTYPE line below even though it ends in `/`."""
        text = ('comment_char %\nescape_char /\nLC_CTYPE\ncopy "i18n"\n'
                'END LC_CTYPE\nLC_COLLATE\ncopy "iso14651_t1"\n'
                'END LC_COLLATE\n')
        self.assertIsNone(g.lc_collate_unread(text, self.never))
        self.assertIn('LC_CTYPE, opened above LC_COLLATE',
                      g.lc_collate_unread(text.replace('END LC_CTYPE',
                                                       '% END LC_CTYPE', 1),
                                          self.never))

    def test_the_first_word_is_read_as_get_ident_reads_it(self):
        """Round 2, finding 3: get_ident stops only at a blank, `"`, `;`,
        `<` or `,` (linereader.c:580-581@2.39), so `END.LC_CTYPE` is one
        word, not END, and LC_CTYPE stays open."""
        for end in ('END.LC_CTYPE', 'END-x', 'END%', 'END\u00e9'):
            with self.subTest(end=end):
                text = self.text().replace('END LC_CTYPE', end, 1)
                self.assertIn('has no END line glibc reads',
                              g.lc_collate_unread(text, self.never))
        for end in ('END;', 'END"x"', 'END<U0041>', 'END,'):
            with self.subTest(control=end):
                text = self.text().replace('END LC_CTYPE', end, 1)
                self.assertIsNone(g.lc_collate_unread(text, self.never))

    def test_an_lc_ctype_copy_with_no_string_is_a_reason(self):
        """Round 2, finding 4, measured on glibc-2.34-283.el9_8 and
        glibc-2.39-142.el10_2: `copy i18n` sends ctype_read to
        skip_category, and LC_COLLATE is not read."""
        plain = self.text()
        for line in ('copy i18n', 'copy <i18n>', 'copy'):
            with self.subTest(line=line):
                text = plain.replace('copy "i18n"', line, 1)
                self.assertIn('copy above LC_COLLATE with no string',
                              g.lc_collate_unread(text, self.never))
        self.assertIsNone(g.lc_collate_unread(
            plain.replace('copy "i18n"', 'copy  "i18n"', 1), self.never))

    def test_a_second_lc_collate_section_is_a_reason(self):
        """Round 2, finding 5: glibc reads every LC_COLLATE section into
        the same tables (collate_startup, ld-collate.c:1499@2.39), above
        the block or below it, spelled plainly, indented, or built across
        a line end."""
        plain = self.text()
        for text in (plain + 'LC_COLLATE\nEND LC_COLLATE\n',
                     plain + '  LC_COLLATE\nEND LC_COLLATE\n',
                     self.text('LC_COLLATE', 'END LC_COLLATE'),
                     self.text('LC_COLL/', 'ATE', 'END LC_COLLATE'),
                     plain + 'LC_COLL/\nATE\nEND LC_COLLATE\n',
                     plain + 'LC_COLL/ATE\nEND LC_COLLATE\n'):
            with self.subTest(text=text):
                self.assertIsNotNone(g.lc_collate_unread(text, self.never))

    def test_the_control_sections_below_the_block(self):
        """Sections after END LC_COLLATE that open nothing else: no
        reason (round 3 of the review)."""
        text = self.text() + 'LC_TIME\ncopy "en_US"\nEND LC_TIME\n'
        self.assertIsNone(g.lc_collate_unread(text, self.never))

    def test_a_word_built_with_the_escape_is_not_read(self):
        """get_ident keeps the character after an escape inside a word
        (linereader.c:580-589@2.39), so `EN/D` is END to glibc."""
        text = self.text().replace('END LC_CTYPE', 'EN/D LC_CTYPE', 1)
        self.assertIn('holds the escape character',
                      g.lc_collate_unread(text, self.never))

    def test_no_block_has_no_reason(self):
        self.assertIsNone(g.lc_collate_unread('LC_CTYPE /\nEND LC_CTYPE\n',
                                              self.never))


class UnreadAbove(unittest.TestCase):
    """The files step 2's `!!` names, whatever their verdict: a block that
    also changed is 'collate', and the characters printed under it name
    only its changed lines."""

    OLD = LcCollateUnread.text()
    SWALLOWS = OLD.replace('END LC_CTYPE', 'END LC_CTYPE /', 1)
    ASK = (lambda: None, lambda: None)

    def test_each_side_is_named_by_its_tag(self):
        got = f.unread_above(['p', 'q'], {'p': self.OLD, 'q': self.SWALLOWS},
                             {'p': self.SWALLOWS, 'q': self.OLD}, {},
                             self.ASK, ('t1', 't2'))
        self.assertEqual([(p, [w.split(':')[0] for w in why])
                          for p, why in got],
                         [('p', ['at t2']), ('q', ['at t1'])])

    def test_a_changed_block_is_named_too(self):
        new = self.SWALLOWS.replace('iso14651_t1', 'iso14651_t2')
        self.assertEqual(f.classify_change(self.OLD, new, [(8, 1)]),
                         'collate')
        got = f.unread_above(['p'], {'p': self.OLD}, {'p': new}, {},
                             self.ASK, ('t1', 't2'))
        self.assertEqual([p for p, _ in got], ['p'])

    def test_a_renamed_file_is_read_under_its_new_name(self):
        got = f.unread_above(['p'], {'p': self.OLD}, {'q': self.SWALLOWS},
                             {'p': 'q'}, self.ASK, ('t1', 't2'))
        self.assertEqual([p for p, _ in got], ['p'])

    def test_the_control_nothing_above_is_not_listed(self):
        self.assertEqual(f.unread_above(['p'], {'p': self.OLD},
                                        {'p': self.OLD}, {}, self.ASK,
                                        ('t1', 't2')), [])

    def test_a_side_with_no_text_is_not_asked(self):
        self.assertEqual(f.unread_above(['p'], {'p': self.OLD}, {}, {},
                                        self.ASK, ('t1', 't2')), [])


class DistroDiffReadingCharacters(unittest.TestCase):
    """Backlog 13.7: the comment and escape characters a block is read with
    are part of its rules, and they are declared outside it."""

    UP = collate('<a>')

    def test_the_same_block_read_otherwise_is_a_collation_finding(self):
        node = self.UP.replace('comment_char %', 'comment_char #', 1)
        self.assertEqual(dd.classify_distro_diff(node.encode(),
                                                 self.UP.encode()), 'collate')

    def test_the_diff_says_what_differs_when_the_block_does_not(self):
        """The block diff is empty for such a file; alone it would print
        nothing under a file reported as differing."""
        node = self.UP.replace('escape_char /\n', '', 1)
        lines = dd.collate_diff_lines(self.UP, node, 'up', 'node')
        self.assertEqual(lines, [
            'read with comment_char % and escape_char / at up',
            'read with comment_char % and escape_char \\ at node'])

    def test_the_side_is_named_read_differently(self):
        tmp = tempfile.mkdtemp(prefix='pg-glibc-reading-tree-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for side, text in (('a', self.UP),
                           ('b', self.UP.replace('comment_char %',
                                                 'comment_char #', 1))):
            os.mkdir(os.path.join(tmp, side))
            with open(os.path.join(tmp, side, 'xx'), 'w') as fh:
                fh.write(text)
        buckets, side, _ = dd.compare_trees(os.path.join(tmp, 'a'),
                                            os.path.join(tmp, 'b'), ['xx'])
        self.assertEqual(buckets['collate'], ['xx'])
        self.assertEqual(side['xx'], 'both, read differently')

    def test_the_control_identical_characters_add_nothing(self):
        node = collate('<b>')
        lines = dd.collate_diff_lines(self.UP, node, 'up', 'node')
        self.assertFalse([ln for ln in lines if ln.startswith('read with')])


class CollationStyle(unittest.TestCase):
    """"Printed C.UTF-8 under 'cannot affect an existing index', then said
    nothing about it at all on the other pair" -- false negative #1, and the
    only one of the six the method could not see at all: the file is in
    neither tag. classify_collation_style is what turns the hand-written
    C.UTF-8 story into something the tool decides."""

    def test_the_backported_C_is_ellipsis_based(self):
        """RHEL8's C: six ellipsis ranges, so localedef computes every weight
        and a data diff can never clear it. Measured on collaudit8."""
        text = _harness.backported_c()
        self.assertEqual(g.classify_collation_style(text), 'ellipsis')
        self.assertEqual(len(g.ellipsis_hits(g.collate_text(text))), 6)

    def test_the_2_35_C_is_byte_order_by_construction(self):
        self.assertEqual(g.classify_collation_style(_harness.upstream_c()),
                         'codepoint')

    def test_the_word_in_a_comment_is_not_a_declaration(self):
        """glibc-2.39:localedata/locales/C names codepoint_collation in prose
        ABOVE the keyword. A substring search reads that comment as
        a declaration -- and would then report RHEL8's ellipsis-based C as
        byte order, clearing the one locale this exists to catch."""
        prose = collate(
            "% The keyword 'codepoint_collation' in any part of any LC_COLLATE",
            '% immediately discards all collation information.',
            '<U0000>', '..', '<U10FFFF>')
        self.assertEqual(g.classify_collation_style(prose), 'ellipsis')

    def test_a_declared_comment_char_is_honoured(self):
        text = '\n'.join(['comment_char #', '', 'LC_COLLATE',
                          '# codepoint_collation is only discussed here',
                          '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
                          'END LC_COLLATE', ''])
        self.assertEqual(g.classify_collation_style(text), 'explicit')

    def test_the_keyword_in_angle_brackets_is_a_symbol_not_a_declaration(self):
        """glibc's lexer reads `<name>` as a collating symbol and never as this
        keyword, so a file that names one this way declares nothing. Reading it
        as a declaration clears the locale outright -- the single most
        reassuring verdict this classifier has."""
        symbol = collate('collating-symbol <codepoint_collation>',
                         '<U0041> <U0041>;IGNORE;IGNORE;IGNORE')
        self.assertEqual(g.classify_collation_style(symbol), 'explicit')
        with_range = collate('collating-symbol <codepoint_collation>',
                             '<U0000>', '..', '<U10FFFF>')
        self.assertEqual(g.classify_collation_style(with_range), 'ellipsis')

    def test_a_longer_word_ending_in_the_keyword_is_not_the_keyword(self):
        self.assertEqual(
            g.classify_collation_style(collate('no_codepoint_collation')),
            'explicit')

    def test_an_ellipsis_beside_the_keyword_is_not_byte_order(self):
        """This test used to assert the opposite, on the strength of the
        comment in glibc's C ("in any part of any LC_COLLATE immediately
        discards all collation information"). Measured false (glibc study,
        E5): beside a sort rule the keyword gives broken tables, not byte
        order. Byte order is the keyword alone; beside a range the range is
        what the warnings name."""
        both = collate('codepoint_collation', '<U0000>', '..', '<U10FFFF>')
        self.assertEqual(g.classify_collation_style(both), 'ellipsis')
        self.assertFalse(g.declares_byte_order(both))

    def test_a_symbolic_copy_target_is_the_locale_it_spells(self):
        """`copy "<U0069><U0073><U006F>..."` is iso14651_t1 to localedef
        (locale/programs/linereader.c decodes `<U....>` wherever it reads a
        string), and was a name nothing matched here: ky_KG and uk_UA write it
        that way at glibc-2.12 and 2.17, so both dropped out of the
        iso14651_t1 closure and the floor pair reported them unaffected."""
        escaped = ('<U0069><U0073><U006F><U0031><U0034><U0036><U0035>'
                   '<U0031><U005F><U0074><U0031>')
        self.assertEqual(g.copy_targets(collate(f'copy "{escaped}"')),
                         ['iso14651_t1'])
        self.assertEqual(g.copy_targets(collate('copy "iso14651_t1"')),
                         ['iso14651_t1'])
        graph = g.copy_graph_from_texts(
            {'iso14651_t1': collate('<U0000>', '..', '<U10FFFF>'),
             'ky_KG': collate(f'copy "{escaped}"')})
        self.assertEqual(g.inherited_from(graph, {'iso14651_t1'}),
                         {'ky_KG': ['iso14651_t1']})

    def test_copy_only_and_explicit_are_distinguished(self):
        self.assertEqual(g.classify_collation_style(collate('copy "iso14651_t1"')),
                         'copy-only')
        self.assertEqual(
            g.classify_collation_style(collate('<U0041> <U0041>;IGNORE')),
            'explicit')

    def test_no_block_is_none(self):
        self.assertEqual(g.classify_collation_style('LC_TIME\nEND LC_TIME\n'),
                         'none')


ISO_COPY = 'copy "iso14651_t1"'
SORT_RULE = ('order_start forward', '<U0041> <U0041>;IGNORE;IGNORE;IGNORE',
             'order_end')


class ByteOrderByConstruction(unittest.TestCase):
    """Backlog 13.2: glibc builds byte order from `codepoint_collation` only
    when it is the whole of LC_COLLATE. The number of sort rules is one
    global for the localedef run and collate_output writes it before it
    looks at the keyword (ld-collate.c:273, :2120@2.39). Each shape below
    used to read as byte order here, which clears the locale outright. Those
    named E5 or E6 were built with localedef on the three fixtures (glibc
    study); the others are read from glibc's source, or refused because this
    code cannot read them with certainty, which only keeps a locale listed."""

    def not_byte_order(self, text):
        self.assertFalse(g.declares_byte_order(text))
        self.assertNotEqual(g.classify_collation_style(text), 'codepoint')
        self.assertEqual(g.byte_order_locales({'X': text}), (set(), {}))

    def test_the_keyword_alone_is_byte_order(self):
        """The shape of C from glibc 2.35 and of RHEL9's and RHEL10's,
        byte-identical (md5 850352c6). Indentation and blank lines are space
        to glibc's reader, and so are the defaults `#` and `\\` when the file
        declares no characters (linereader.c:79-80@2.39)."""
        self.assertTrue(g.declares_byte_order(_harness.upstream_c()))
        self.assertTrue(g.declares_byte_order(
            collate('', '   codepoint_collation  ', '')))
        bare = 'LC_COLLATE\n# a note\ncodepoint_collation\nEND LC_COLLATE\n'
        self.assertTrue(g.declares_byte_order(bare))
        self.assertEqual(g.byte_order_locales({'C': bare}), ({'C'}, {}))

    def test_nothing_read_during_one_scan_answers_after_it(self):
        """byte_order_locales reads each file once per call and keeps the
        answer only for that call. Kept past it, a reader that can no longer
        read the file still got the answer that clears the locale, which the
        first version of this did (false-negative-reviewer, 2026-10-09)."""
        bare = 'LC_COLLATE\ncodepoint_collation\nEND LC_COLLATE\n'
        self.assertEqual(g.byte_order_locales({'C': bare}), ({'C'}, {}))
        with mock.patch.object(g, '_glibc_chars', return_value=None):
            self.assertFalse(g.declares_byte_order(bare))
            self.assertEqual(g.byte_order_locales({'C': bare}), (set(), {}))

    def test_a_sort_rule_beside_the_keyword_is_not_byte_order(self):
        both = collate('codepoint_collation', *SORT_RULE)
        self.not_byte_order(both)
        self.assertEqual(g.classify_collation_style(both),
                         'codepoint-not-alone')

    def test_a_copy_beside_the_keyword_is_not_byte_order(self):
        """E5: broken tables on RHEL9 and RHEL10, garbage from strcoll and a
        crash; plain iso14651_t1 order on RHEL8. On the copy's own line (E6a)
        localedef drops the keyword as trailing garbage."""
        self.not_byte_order(collate(ISO_COPY, 'codepoint_collation'))
        self.not_byte_order(collate(f'{ISO_COPY} codepoint_collation'))
        # A comment after the copy is no error to glibc (lr_ignore_rest
        # stops at it), so this is E5's shape too. A line is a comment only
        # when it starts with the comment character.
        self.not_byte_order(collate(f'{ISO_COPY} % the template',
                                    'codepoint_collation'))
        self.assertEqual(
            g.classify_collation_style(collate(ISO_COPY, 'codepoint_collation')),
            'codepoint-not-alone')

    def test_the_keyword_in_a_skipped_branch_is_not_byte_order(self):
        """E6b: skip_to reads nothing in a branch not taken."""
        self.not_byte_order(collate(ISO_COPY, 'ifdef NEVER_DEFINED',
                                    'codepoint_collation', 'endif'))

    def test_the_keyword_glued_to_the_comment_char_is_not_byte_order(self):
        """E6c: `%` inside a word starts no comment, so the line is one
        unknown word and a syntax error."""
        self.not_byte_order(collate(ISO_COPY, 'codepoint_collation%x'))

    def test_a_continued_line_is_not_byte_order(self):
        """E6d: a line ending in the escape character takes the next one with
        it, and after a `copy` the keyword is then dropped as trailing
        garbage. After the header it would be read as the header's rest
        (from the source). After a comment glibc skips only that physical
        line (linereader.c:222-229@2.39) and would still read the keyword;
        refused anyway, because this code does not follow continued lines."""
        self.not_byte_order(collate(f'{ISO_COPY} /', 'codepoint_collation'))
        self.not_byte_order('\n'.join(['comment_char %', 'escape_char /', '',
                                       'LC_COLLATE /', 'codepoint_collation',
                                       'END LC_COLLATE', '']))
        self.not_byte_order(collate('% a note /', 'codepoint_collation'))

    def test_the_keyword_as_a_define_argument_is_not_byte_order(self):
        """E6e, before and after the copy."""
        self.not_byte_order(collate('define BYTE codepoint_collation',
                                    ISO_COPY))
        self.not_byte_order(collate(ISO_COPY,
                                    'define BYTE codepoint_collation'))

    def test_a_second_lc_collate_section_is_not_byte_order(self):
        """locfile.c:179-180@2.39 hands every LC_COLLATE section of the file
        to collate_read, so a sort rule in a second one is read in the same
        run. Indented, too: glibc's reader skips the space."""
        second = '\n'.join(['LC_COLLATE', *SORT_RULE, 'END LC_COLLATE', ''])
        self.not_byte_order(_harness.upstream_c() + second)
        self.not_byte_order(_harness.upstream_c() + '  ' + second)
        # A header with more on its word is no header to glibc, so the one
        # section it reads is the copy below (round 3 of
        # false-negative-reviewer: no test held the exact-header guard).
        self.not_byte_order('\n'.join(['comment_char %', 'escape_char /', '',
                                       'LC_COLLATEX', 'codepoint_collation',
                                       'END LC_COLLATE', '', 'LC_COLLATE',
                                       ISO_COPY, 'END LC_COLLATE', '']))

    def test_a_header_built_with_the_escape_character_is_not_byte_order(self):
        """Inside a word glibc takes the character after the escape as it is
        (get_ident, linereader.c:580-589@2.39), and a line ending in the
        escape joins the next one on, so these open a second LC_COLLATE that
        no search for the word sees. The copy in it then shares
        iso14651_t1's rules with the keyword: E5's broken tables. Found by
        false-negative-reviewer, which measured the tool declaring this C
        byte order, the first two spellings in round 1 and the two that start
        a line with the escape, or leave one behind, in round 2. lr_next
        removes only the last escape of a line. glibc's own C writes no word
        this way (2.35 to 2.42)."""
        up = _harness.upstream_c()
        for header in ('LC_COLL/ATE', 'LC_COL/\nLATE', '% a note /\nLC_COL/\nLATE',
                       'LC_COL/\n/LATE', 'LC_COLL//\nATE', 'LC_/COLLATE',
                       'LC/_COLLATE'):
            with self.subTest(header=header):
                hidden = f'{header}\n{ISO_COPY}\nEND LC_COLLATE\n\nLC_COLLATE\n'
                self.not_byte_order(up.replace('LC_COLLATE\n', hidden, 1))
        # The real C's comments write C/POSIX and glibc/locale: a comment is
        # skipped whole, by glibc and here.
        self.assertTrue(g.declares_byte_order(
            up.replace('% locale to use', '% C/POSIX locale to use')))

    def test_a_range_this_code_sees_wins_over_byte_order(self):
        """With no comment_char directive glibc's comment character is `#`
        and this code's ellipsis reader takes `%` (backlog 13.8), so a `#`
        comment holding `..` is a range to the reader and a comment to
        glibc. Before the two agreed, step 4 listed such a locale and named
        it byte order on the next line, and a C copying it read "IS exposed"
        and "byte order too" in one status. Found by false-negative-reviewer,
        round 2. The range wins, which can only keep a locale listed."""
        hashed = ('LC_COLLATE\n# U+0000..U+10FFFF sort by code point\n'
                  'codepoint_collation\nEND LC_COLLATE\n')
        self.assertTrue(g.scan_ellipsis({'zz_ZZ': hashed})[0])
        self.not_byte_order(hashed)
        self.assertEqual(g.classify_collation_style(hashed), 'ellipsis')
        copier = 'LC_COLLATE\n# planes 0..16\ncopy "C"\nEND LC_COLLATE\n'
        self.assertEqual(g.byte_order_locales(
            {'C': _harness.upstream_c(), 'X': copier, 'Y': collate('copy "zz_ZZ"'),
             'zz_ZZ': hashed}), ({'C'}, {}))
        # The same disagreement over the keyword: a C that only copies a
        # byte-order locale, under a `#` comment naming the keyword, read as
        # 'codepoint-not-alone' and as a copy in one status (round 3).
        named = ('LC_COLLATE\n# not codepoint_collation: a copy\n'
                 'copy "zz_ZZ"\nEND LC_COLLATE\n')
        self.assertEqual(g.classify_collation_style(named),
                         'codepoint-not-alone')
        self.assertEqual(g.byte_order_locales(
            {'C': named, 'zz_ZZ': _harness.upstream_c()}), ({'zz_ZZ'}, {}))

    def test_an_unterminated_block_is_not_byte_order(self):
        text = '\n'.join(['comment_char %', 'escape_char /', '',
                          'LC_COLLATE', 'codepoint_collation', ''])
        self.not_byte_order(text)

    def test_characters_read_without_certainty_are_not_byte_order(self):
        """The comment and escape characters are read with certainty only
        when the directives come first, with a value glibc's own files use.
        Otherwise localedef may read them differently from this code: a
        leading line continued into the directive swallows it, one inside a
        section is a syntax error there, and glibc refuses `<` as an argument
        (it reads as a symbol) and keeps `#`. Refused rather than guessed;
        that can only keep a locale listed, never clear one. Repeated
        directives at the top are read in order, as glibc does, unless one
        ends in the escape character in force: glibc then reads its argument
        from the next line (round 3 of false-negative-reviewer measured the
        tool reading `\\` where glibc reads `/`, and clearing C). A trailing
        space after the escape does not continue the line."""
        block = ['', 'LC_COLLATE', '% note', 'codepoint_collation',
                 'END LC_COLLATE', '']
        self.assertTrue(g.declares_byte_order(
            '\n'.join(['comment_char %', 'escape_char /', *block])))
        self.assertTrue(g.declares_byte_order(
            '\n'.join(['comment_char #', 'comment_char %', 'escape_char /',
                       *block])))
        self.assertFalse(g.declares_byte_order(
            '\n'.join(['comment_char %', 'comment_char #', 'escape_char /',
                       *block])))
        self.not_byte_order('\n'.join(['% a note first \\', 'comment_char %',
                                       'escape_char /', *block]))
        self.not_byte_order('\n'.join(['escape_char /', 'LC_CTYPE',
                                       'comment_char %', 'END LC_CTYPE',
                                       *block]))
        self.not_byte_order('\n'.join(['comment_char <', 'escape_char /', '',
                                       'LC_COLLATE', '< note',
                                       'codepoint_collation',
                                       'END LC_COLLATE', '']))
        hidden = ['', 'LC_COLL/ATE', 'order_start forward', '<U0041>',
                  'order_end', 'END LC_COLLATE', '']
        self.not_byte_order('\n'.join(['comment_char %', 'escape_char \\',
                                       '/', *block, *hidden]))
        self.not_byte_order('\n'.join(['comment_char %', 'escape_char /',
                                       'escape_char /', '\\', *block,
                                       *[h.replace('/', '\\') for h in hidden]]))
        self.assertTrue(g.declares_byte_order(
            '\n'.join(['comment_char %', 'escape_char \\ ', *block])))

    def test_unicode_space_is_not_space_to_glibc(self):
        """glibc's reader skips isspace in the C locale; U+00A0 is part of
        the word for it, while Python's str.strip() removes it."""
        self.not_byte_order(collate(' codepoint_collation'))

    def test_a_copy_of_a_byte_order_locale_is_byte_order(self):
        """Backlog 6.9. A copy and nothing else shares the copied locale's
        data (ld-collate.c:1518-1521@2.39). Measured on RHEL9 and RHEL10:
        `copy "C"`, a copy of that copy, and a copy of a file holding the
        keyword alone compile byte-identical to the installed C.UTF-8."""
        texts = {'C': _harness.upstream_c(),
                 'X': collate('copy "C"'),
                 'Y': collate('% via X', 'copy "X"'),
                 'Z': collate('copy "<U0043>"')}
        self.assertEqual(g.byte_order_locales(texts),
                         ({'C'}, {'X': 'C', 'Y': 'X', 'Z': 'C'}))
        self.assertEqual(g.classify_collation_style(texts['X']), 'copy-only')

    def test_a_copy_with_no_blank_before_the_string_is_byte_order(self):
        """Backlog 13.17: glibc reads `copy"C"` as `copy "C"`."""
        texts = {'C': _harness.upstream_c(), 'X': collate('copy"C"')}
        self.assertEqual(g.byte_order_locales(texts), ({'C'}, {'X': 'C'}))

    def test_a_copy_with_anything_else_is_not_byte_order(self):
        """Its own sort rule is read in the same run as the keyword, so the
        tables break as in E5 (backlog 6.9's refresh)."""
        texts = {'C': _harness.upstream_c(),
                 'X': collate('copy "C"', *SORT_RULE),
                 'Y': collate('copy "C"', ISO_COPY)}
        self.assertEqual(g.byte_order_locales(texts), ({'C'}, {}))

    def test_a_copy_that_cannot_resolve_is_not_byte_order(self):
        """A missing target is exit 4 and a cycle exit 5 in localedef, and
        a copy of RHEL8's C or of the keyword beside a copy is not byte
        order either."""
        texts = {'A': collate('copy "B"'), 'B': collate('copy "A"'),
                 'M': collate('copy "no_such_locale"'),
                 'C8': _harness.backported_c(),
                 'R': collate('copy "C8"'),
                 'N': collate(ISO_COPY, 'codepoint_collation'),
                 'S': collate('copy "N"')}
        self.assertEqual(g.byte_order_locales(texts), (set(), {}))


class ScanEllipsis(unittest.TestCase):
    """Shared by the tag scan and the node-directory scan, so the two cannot
    drift on comment_char handling."""

    def test_it_counts_only_files_with_a_block(self):
        texts = {'C': _harness.backported_c(),
                 'plain': collate('<U0041> <U0041>'),
                 'no_collate': 'LC_TIME\nEND LC_TIME\n'}
        flagged, with_collate = g.scan_ellipsis(texts)
        self.assertEqual(sorted(flagged), ['C'])
        self.assertEqual(with_collate, 2)

    def test_a_block_on_the_first_line_is_still_scanned(self):
        """A file opening with LC_COLLATE -- the glibc <=2.23 shape. In a
        directory scan the files are arbitrary distro files, so a blind spot
        here would CLEAR a template instead of flagging it. scan_ellipsis
        reached it via collate_text before collate_block was fixed; now both
        routes work, and this asserts the underlying one does too."""
        text = 'LC_COLLATE\n<U0000>\n..\n<U10FFFF>\nEND LC_COLLATE\n'
        self.assertIsNotNone(g.collate_block(text))
        flagged, with_collate = g.scan_ellipsis({'iso14651_t1_common': text})
        self.assertEqual(sorted(flagged), ['iso14651_t1_common'])
        self.assertEqual(with_collate, 1)


class CopyGraphFromTexts(unittest.TestCase):
    """"Discarded unreadable blobs, silently shrinking the copy graph" --
    false negative #3. The graph is now built by one pure function whether the
    corpus came from a tag or from a node's directory."""

    def test_a_graph_from_texts_matches_the_shape_build_copy_graph_returns(self):
        texts = {'a': collate('copy "b"'),
                 'b': collate('<U0041> <U0041>'),
                 'c': 'LC_TIME\nEND LC_TIME\n'}
        self.assertEqual(g.copy_graph_from_texts(texts), {'a': ['b'], 'b': []})

    def test_a_template_opening_with_lc_collate_is_a_root_not_a_dropout(self):
        """The glibc <=2.23 master templates open with LC_COLLATE at byte 0.
        While collate_block missed those, copy_graph_from_texts skipped them
        entirely, so the highest fan-in files in the corpus were not in the
        graph and everything inheriting from them looked unaffected -- 11
        locales reported on glibc-2.12..2.17 where there are 280."""
        texts = {'iso14651_t1_common': 'LC_COLLATE\n<U0041> <U0041>\n'
                                       'END LC_COLLATE\n',
                 'en_US': collate('copy "iso14651_t1_common"')}
        graph = g.copy_graph_from_texts(texts)
        self.assertIn('iso14651_t1_common', graph)
        self.assertEqual(g.inherited_from(graph, {'iso14651_t1_common'}),
                         {'en_US': ['iso14651_t1_common']})

    def test_a_node_only_file_participates_as_a_root(self):
        """A node-only file is in no tag, so at a tag it can be neither a root
        nor a target. Over a node's own corpus it is both."""
        texts = {'C': _harness.backported_c(), 'zz_MADEUP': collate('copy "C"')}
        graph = g.copy_graph_from_texts(texts)
        self.assertEqual(g.inherited_from(graph, {'C'}), {'zz_MADEUP': ['C']})


class NodeToNodeClassification(unittest.TestCase):
    """classify_distro_diff, reused unchanged for two nodes. Only the labels
    the caller puts on the sides change."""

    def test_a_block_on_one_node_only_is_a_collate_finding(self):
        with_block = collate('<U0041> <U0041>').encode()
        without = b'comment_char %\nLC_TIME\nEND LC_TIME\n'
        self.assertEqual(dd.classify_distro_diff(with_block, without), 'collate')
        self.assertEqual(dd.classify_distro_diff(without, with_block), 'collate')

    def test_it_is_symmetric(self):
        a = collate('<U0041> <U0041>').encode()
        b = collate('<U0042> <U0042>').encode()
        self.assertEqual(dd.classify_distro_diff(a, b),
                         dd.classify_distro_diff(b, a))

    def test_a_non_utf8_difference_INSIDE_the_block_is_a_collate_finding(self):
        """The existing bytes-not-text test puts the differing byte after END
        LC_COLLATE, so it can only assert "not identical". Inside the block the
        claim is stronger: a lossy decode would collapse both to U+FFFD, the
        blocks would compare equal, and the verdict would drop from 'collate'
        to 'other' -- a real sort-order change filed as a comment change."""
        base = collate('<U0041> <U0041>;IGNORE % X')
        a = base.replace('X', 'é').encode('latin-1')
        b = base.replace('X', 'ü').encode('latin-1')
        self.assertEqual(a.decode('utf-8', 'replace'),
                         b.decode('utf-8', 'replace'))     # the trap itself
        self.assertEqual(dd.classify_distro_diff(a, b), 'collate')


class CorpusGuard(unittest.TestCase):
    """"A half-copied directory would report '12 compared, 0 inside
    LC_COLLATE', which is indistinguishable from a clean result." Pure, so
    both truncation guards are checked with integers instead of a fabricated
    directory."""

    def test_expect_files_mismatch_is_refused(self):
        self.assertIsNotNone(dd.corpus_problem(350, expect_files=353))

    def test_a_short_corpus_against_an_upstream_reference_is_refused(self):
        self.assertIsNotNone(dd.corpus_problem(3, reference=353))

    def test_two_equally_truncated_sides_are_still_refused(self):
        """The trap node-to-node adds and node-vs-tag never had: with no
        upstream side there is nothing to take half of, three files intersect
        three files, and every one of them is identical. An absolute floor is
        the only thing standing between that and a flawless clean upgrade."""
        self.assertIsNone(dd.corpus_problem(3))
        self.assertIsNotNone(dd.corpus_problem(3, floor=dd.DEFAULT_MIN_FILES))

    def test_a_real_corpus_passes_every_guard(self):
        self.assertIsNone(dd.corpus_problem(353, expect_files=353,
                                            reference=355,
                                            floor=dd.DEFAULT_MIN_FILES))

    def test_the_message_says_why_it_refuses(self):
        for problem in (dd.corpus_problem(3, reference=353),
                        dd.corpus_problem(3, floor=200)):
            self.assertIn('indistinguishable from a clean run', problem)


class MissingFromCopy(unittest.TestCase):
    """A copy that lost files above the half-of-the-tag refusal reported
    "Nothing differs" and listed the rest with no `!!` (backlog 1.15). The
    helper the node-reading steps share, driven with names
    instead of a directory."""

    def report(self, copy, tag, skipped=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            missing = g.report_missing_from_copy(copy, tag, 'glibc-2.34',
                                                 './el9-locales', skipped)
        return missing, buf.getvalue()

    def test_every_missing_file_is_named_under_a_warning(self):
        missing, out = self.report(['a', 'b'], ['a', 'b', 'th_TH', 'c'])
        self.assertEqual(missing, ['c', 'th_TH'])
        self.assertTrue(out.startswith('!! 2 file(s) that glibc-2.34 has'),
                        out)
        self.assertIn('are missing from ./el9-locales: c, th_TH.', _harness.flat(out))
        # Every line is part of the block the wrapper repeats: `!!` first,
        # three-space continuation after, or the tail never reaches it.
        for line in out.splitlines()[1:]:
            self.assertTrue(line.startswith('   '), line)

    def test_it_concludes_nothing_about_why(self):
        """The first attempt at this fix told the reader the missing files
        were locales the node does not ship -- measured false at 250 of 355
        with a count the reader had asserted."""
        _, out = self.report([], ['a'])
        self.assertIn('cannot tell whether the machine does not ship them or '
                      'the copy lost them', _harness.flat(out))

    def test_a_file_held_but_not_read_is_named_apart(self):
        """A tag file the copy holds as a symlink is not missing, and was not
        read. Reported as missing, the count the reader was told to compare
        matched (355 entries against 355) and cleared a th_TH never read.
        Found by false-negative-reviewer on this fix."""
        missing, out = self.report(['a'], ['a', 'th_TH'],
                                   [('th_TH', 'symlink'), ('.x', 'dotfile')])
        self.assertEqual(missing, ['th_TH'])
        text = _harness.flat(out)
        self.assertIn('are in ./el9-locales but were not read: '
                      'th_TH (symlink).', text)
        self.assertNotIn('are missing from', text)
        self.assertNotIn('.x', text)
        self.assertIn('Copy those again as regular files.', text)

    def test_missing_and_not_read_together_are_both_named(self):
        """The case no other test combines: a copy that lost files AND holds
        a tag file as a symlink. Saying only the missing ones left th_TH in
        step 6's plain "Skipped" list, which the summary does not collect
        (false-negative-reviewer, second round)."""
        missing, out = self.report(['a'], ['a', 'b', 'th_TH'],
                                   [('th_TH', 'symlink')])
        self.assertEqual(missing, ['b', 'th_TH'])
        text = _harness.flat(out)
        self.assertIn('are missing from ./el9-locales: b.', text)
        self.assertIn('but were not read: th_TH (symlink).', text)
        self.assertIn('Check on the machine whether', text)
        self.assertIn('Copy those again as regular files.', text)

    def test_it_advises_names_not_a_count(self):
        """`ls | wc -l` counts what the steps skip, so it can match while a
        file went unread; the advice names the files instead."""
        _, out = self.report([], ['a'])
        self.assertNotIn('wc -l', _harness.flat(out))
        self.assertIn('Check on the machine whether /usr/share/i18n/locales/ '
                      'has them.', _harness.flat(out))

    def test_a_complete_copy_prints_nothing(self):
        self.assertEqual(self.report(['a', 'b'], ['a', 'b']), ([], ''))

    def test_files_the_tag_lacks_are_not_missing(self):
        """The distro's own C is in the copy and in no 2.34 tag."""
        self.assertEqual(self.report(['a', 'C'], ['a']), ([], ''))


class SameTreeAndManifest(unittest.TestCase):
    """Comparing a directory with itself, or two copies of one tar, reports
    100% identical -- the most reassuring output the tool can print."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='pg-glibc-sametree-')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def _dir(self, name, files=()):
        path = os.path.join(self.base, name)
        os.mkdir(path)
        for fname, body in files:
            with open(os.path.join(path, fname), 'w', encoding='utf-8') as fh:
                fh.write(body)
        return path

    def test_the_same_directory_under_two_names_is_detected(self):
        real = self._dir('real')
        link = os.path.join(self.base, 'link')
        os.symlink(real, link)
        self.assertTrue(dd.same_tree(real, link))
        self.assertTrue(dd.same_tree(real, os.path.join(real, '.')))
        self.assertFalse(dd.same_tree(real, self._dir('other')))

    def test_two_identical_trees_share_a_fingerprint(self):
        files = (('C', _harness.backported_c()), ('en_US', collate('copy "x"')))
        a, b = self._dir('a', files), self._dir('b', files)
        self.assertEqual(dd.tree_manifest(a, ['C', 'en_US'])[2],
                         dd.tree_manifest(b, ['C', 'en_US'])[2])

    def test_a_different_size_changes_the_fingerprint(self):
        a = self._dir('a', (('C', _harness.backported_c()),))
        b = self._dir('b', (('C', _harness.upstream_c()),))
        self.assertNotEqual(dd.tree_manifest(a, ['C'])[2],
                            dd.tree_manifest(b, ['C'])[2])



# --- step 2's character set check (backlog 13.7, PR B) ----------------------

_PROLOG = ('<code_set_name> TEST', '<comment_char> %', '<escape_char> /',
           '<mb_cur_min> 1', '<mb_cur_max> 4')


def charmap(*body, prolog=_PROLOG, tail=()):
    """A charmap as glibc's files are written: prolog, body, END CHARMAP."""
    return '\n'.join([*prolog, 'CHARMAP', *body, 'END CHARMAP', *tail, ''])


class CharmapReader(unittest.TestCase):
    """read_charmap models charmap.c strictly: what it reads it reads as
    glibc does, and any other shape is Unsettled, which keeps every build in
    that charmap listed. The prototype it replaced counted only data lines at
    column 0, so an indented line glibc reads was skipped in silence."""

    def test_singles_and_ranges_get_glibc_s_bytes(self):
        cm = ck.read_charmap(charmap('<U0041>     /x41         A',
                                     '<U0100>..<U0102> /xc4/x80 <A>'), 'T')
        self.assertEqual(ck.key(cm, 0x41), '/x41')
        self.assertEqual(ck.key(cm, 0x101), '/xc4/x81')
        self.assertIsNone(ck.key(cm, 0x103))
        self.assertEqual(cm.code_set_name, 'TEST')

    def test_the_first_definition_wins_whatever_the_spelling(self):
        """charmap.c stores <U> names as U and eight digits, and the hash
        keeps the first (charmap.c:983-984)."""
        cm = ck.read_charmap(charmap('<U00000041> /x41', '<U0041> /x42'), 'T')
        self.assertEqual(ck.key(cm, 0x41), '/x41')

    def test_an_indented_data_line_is_read(self):
        cm = ck.read_charmap(charmap('<U0041> /x41', '   <U0042> /x42'), 'T')
        self.assertEqual(ck.key(cm, 0x42), '/x42')

    def test_a_comment_line_ending_in_the_escape_drops_only_itself(self):
        """linereader.c:216-231@2.39, measured in M3 of the 13.7 handoff."""
        cm = ck.read_charmap(charmap('% a comment /', '<U0042> /x42'), 'T')
        self.assertEqual(ck.key(cm, 0x42), '/x42')

    def test_width_sections_after_the_body_are_skipped(self):
        cm = ck.read_charmap(charmap('<U0041> /x41',
                                     tail=('WIDTH', '<U0041> 1', 'END WIDTH',
                                           'WIDTH_DEFAULT 1')), 'T')
        self.assertEqual(ck.key(cm, 0x41), '/x41')

    def test_body_lines_glibc_reads_another_way_are_unsettled(self):
        bad = {'one hex digit': ['<U0041> /x4 A'],
               # /x4 then /x42 would read as one byte if one digit passed:
               # the byte count alone does not catch it.
               'one hex digit, then a byte': ['<U0041> /x4/x42 A'],
               'decimal byte': ['<U0041> /d65'],
               'seventeen bytes': ['<U0041> ' + '/x41' * 17],
               'more bytes than mb_cur_max': ['<U0041> /x41/x42/x43/x44/x45'],
               'absolute range': ['<U0041>...<U0043> /x41'],
               'decimal range': ['<U0041>....<U0043> /x41'],
               'step range': ['<U0041>..(2)..<U0043> /x41'],
               'a name that is not <U>': ['<ch> /x41'],
               'lower-case u': ['<u0041> /x41'],
               'an escape inside the name': ['<U00/41> /x41'],
               'garbage after the bytes': ['<U0041> /x414'],
               'a data line continued': ['<U0041> /x41 A /', '<U0042> /x42'],
               'a range that ends below its start': ['<U0045>..<U0041> /x45'],
               'a range past one byte': ['<U0000>..<U0002> /xfe'],
               'overlapping ranges': ['<U0100>..<U0105> /xc4/x80',
                                      '<U0104>..<U0108> /xc5/x80'],
               'a single inside a range': ['<U0100>..<U0105> /xc4/x80',
                                           '<U0103> /xc6/x80'],
               'END of another section': ['<U0041> /x41', 'END WIDTH'],
               'a line of no shape': ['<U0041> /x41', 'nonsense']}
        for why, body in bad.items():
            with self.subTest(why):
                with self.assertRaises(ck.Unsettled):
                    ck.read_charmap(charmap(*body), 'T')

    def test_prologs_and_ends_it_does_not_model_are_unsettled(self):
        bad = {'include': _PROLOG + ('<include> X',),
               'repertoiremap (H10)': _PROLOG + ('<repertoiremap> X',),
               'escape_char |': ('<code_set_name> T', '<escape_char> |'),
               'comment_char !': ('<code_set_name> T', '<comment_char> !'),
               'mb_cur_max twice': _PROLOG + ('<mb_cur_max> 4',),
               'mb_cur_min over mb_cur_max': ('<code_set_name> T',
                                              '<mb_cur_min> 4',
                                              '<mb_cur_max> 2'),
               'no code_set_name': ('<comment_char> %', '<escape_char> /')}
        # A word glibc reads as a keyword is no name: it stores none
        # (round 4 of the PR D review).
        for word in ('CHARMAP', 'END', 'WIDTH', 'WIDTH_VARIABLE',
                     'WIDTH_DEFAULT'):
            bad[f'<code_set_name> {word}'] = (f'<code_set_name> {word}',
                                              ) + _PROLOG[1:]
        for why, prolog in bad.items():
            with self.subTest(why):
                with self.assertRaises(ck.Unsettled):
                    ck.read_charmap(charmap('<U0041> /x41', prolog=prolog),
                                    'T')
        with self.subTest('no END CHARMAP: a truncated file'):
            with self.assertRaises(ck.Unsettled):
                ck.read_charmap('\n'.join(_PROLOG + ('CHARMAP',
                                                      '<U0041> /x41')), 'T')
        with self.subTest('END of something else, and no END CHARMAP'):
            with self.assertRaises(ck.Unsettled):
                ck.read_charmap('\n'.join(_PROLOG + ('CHARMAP', '<U0041> /x41',
                                                      'END WIDTH', '')), 'T')
        with self.subTest('the file ends inside WIDTH'):
            with self.assertRaises(ck.Unsettled):
                ck.read_charmap(charmap('<U0041> /x41',
                                        tail=('WIDTH', '<U0041> 1')), 'T')


class CharmapDelta(unittest.TestCase):
    """K: the code points whose bytes differ, are added or removed. The
    time-gate prototype counted U+20086 and U+20088 of GB18030 as changed
    (2.34 -> 2.39: 38 against 30), because they moved from single lines to
    a range with the same bytes."""

    def k(self, old, new):
        return ck.delta(ck.read_charmap(charmap(*old), 'T'),
                        ck.read_charmap(charmap(*new), 'T'))

    def test_a_move_between_a_single_and_a_range_is_no_change(self):
        singles = ['<U0100> /xc4/x80', '<U0101> /xc4/x81', '<U0102> /xc4/x82']
        ranged = ['<U0100>..<U0102> /xc4/x80']
        self.assertEqual(self.k(singles, ranged), set())
        self.assertEqual(self.k(ranged, singles), set())

    def test_added_removed_and_other_bytes(self):
        old = ['<U0041> /x41', '<U0042> /x42', '<U0044> /x44']
        new = ['<U0041> /x61', '<U0043> /x43']
        self.assertEqual(self.k(old, new), {0x41, 0x42, 0x43, 0x44})
        kk = ck.K(self.k(old, new), ck.read_charmap(charmap(*old), 'T'),
                  ck.read_charmap(charmap(*new), 'T'))
        self.assertEqual((kk.size, kk.added, kk.removed), (4, 1, 2))

    def test_swapped_duplicates_change_the_key(self):
        self.assertEqual(self.k(['<U0041> /x41', '<U0041> /x42'],
                                ['<U0041> /x42', '<U0041> /x41']), {0x41})

    def test_a_range_that_grows_adds_its_new_end(self):
        self.assertEqual(self.k(['<U0100>..<U0104> /xc4/x80'],
                                ['<U0100>..<U0105> /xc4/x80']), {0x105})

    def test_a_range_with_other_first_bytes_changes_every_member(self):
        self.assertEqual(self.k(['<U0100>..<U0102> /xc4/x80'],
                                ['<U0100>..<U0102> /xc4/x81']),
                         {0x100, 0x101, 0x102})


def locale_text(*collate_body, head=('comment_char %', 'escape_char /')):
    return '\n'.join([*head, '', 'LC_COLLATE', *collate_body,
                      'END LC_COLLATE', ''])


class LcCollateSearch(unittest.TestCase):
    """What the search reads in an LC_COLLATE block. It must be a superset
    of what glibc resolves through the charmap: what it misses, a charmap
    change can move unseen."""

    def test_every_spelling_of_a_name_is_found(self):
        scan = ck.Scan(locale_text('copy "x"', '<U00E9> IGNORE',
                                   '<U000000E8> IGNORE', '<U00/E7> IGNORE',
                                   '\u00f1 IGNORE'))
        names = scan.names()
        self.assertLessEqual({'00E9', '000000E8', '000000E7'}, names.spell)
        self.assertIn(0xF1, names.raw)
        self.assertIn(ord('x'), names.raw)      # the copy string (H6)
        self.assertEqual(scan.copies, ('x',))

    def test_a_comment_starts_only_where_a_token_does(self):
        cs = ck._certain_comment_start
        pct = ('%', '/')
        self.assertEqual(cs('foo%bar <U4E00>', pct), 15)
        self.assertEqual(cs('<U0041> % <U4E00>', pct), 8)
        self.assertEqual(cs('"a%b" <U4E00>', pct), 13)
        self.assertEqual(cs('<U0041>%<U4E00>', pct), 7)
        self.assertEqual(cs('<U0041> % x', None), 11)

    def test_only_the_file_s_escape_character_escapes(self):
        """With escape_char /, a backslash in a string is a plain character
        and `/"` an escaped quote, so the string runs on past the `%`
        (get_string, linereader.c:773ff@2.39). Reading both as escapes cut
        the line at the `%` and dropped the <U00E9> after it."""
        line = 'collating-element <e-x> from "<U0041>\\/" % <U00E9>"'
        self.assertEqual(ck._certain_comment_start(line, ('%', '/')),
                         len(line))
        self.assertEqual(
            ck._certain_comment_start('from "<U0041>\\" % x', ('%', '/')), 16)

    def test_a_hit_only_in_a_comment_is_dropped_and_one_in_code_kept(self):
        k = ck.K(frozenset({0xE9}))
        only = ck.Scan(locale_text('% <U00E9> is not named here',
                                   '<U0041> IGNORE'))
        code = ck.Scan(locale_text('<U00E9> IGNORE % <U00E9>'))
        word = ck.Scan(locale_text('foo%bar <U00E9>'))
        continued = ck.Scan(locale_text('<U0041> IGNORE /', '% <U00E9>'))
        self.assertFalse(ck.confirmed_hit(only, k))
        self.assertTrue(ck.confirmed_hit(code, k))
        self.assertTrue(ck.confirmed_hit(word, k))
        self.assertTrue(ck.confirmed_hit(continued, k))

    def test_every_spelling_is_read_where_it_stands(self):
        """The upper-case spelling in a comment does not clear a lower-case
        or escaped one in code: glibc reads both as the code point
        (get_symname, linereader.c:502-546@2.39). Only the upper-case one
        was looked for, found in the comment, and the hit dropped."""
        k = ck.K(frozenset({0xE9}))
        for spelt in ('<U00e9> <U00e9>', '<U00/E9> <U00/E9>'):
            with self.subTest(spelt):
                scan = ck.Scan(locale_text('% <U00E9> is e-acute', spelt))
                self.assertTrue(ck.confirmed_hit(scan, k))

    def test_an_escape_before_the_u_is_read_too(self):
        """`</U00E9>` is U+00E9: get_symname keeps the character after the
        escape, whichever it is (linereader.c:516-524@2.39). The pattern
        wanted an escape after the U, so the name was not read, and a
        comment spelling it plainly cleared the hit (false-negative-
        reviewer, round 2)."""
        k = ck.K(frozenset({0xE9}))
        alone = ck.Scan(locale_text('</U00E9> IGNORE'))
        self.assertIn(0xE9, k.named(alone.names()))
        commented = ck.Scan(locale_text('% <U00E9> e-acute',
                                        '</U00E9> IGNORE'))
        self.assertTrue(ck.confirmed_hit(commented, k))

    def test_a_raw_character_in_code_keeps_the_hit(self):
        k = ck.K(frozenset({0xE9}))
        scan = ck.Scan(locale_text('% <U00E9> e-acute', '\u00e9 IGNORE'))
        self.assertTrue(ck.confirmed_hit(scan, k))

    def test_a_member_spelt_by_a_copy_string_keeps_the_hit(self):
        """The copy string is translated through the charmap (H6); a member
        spelt there keeps the hit even when its plain spelling sits in a
        comment."""
        k = ck.K(frozenset({ord('x')}))
        scan = ck.Scan(locale_text('copy "x"', '% <U0078>'))
        self.assertTrue(ck.confirmed_hit(scan, k))

    def test_an_escape_character_it_does_not_read_with_is_odd(self):
        """glibc takes any one-character word as escape_char
        (locfile.c:123-149@2.39); every search here knows only `/` and
        `\\`, so `<U00!E9>` under `escape_char !` would go unread."""
        odd = ck.Scan(locale_text('<U00!E9> IGNORE',
                                  head=('comment_char %', 'escape_char !')))
        self.assertIsNotNone(odd.odd)
        self.assertIsNone(ck.Scan(locale_text('<U00E9> IGNORE')).odd)
        # A directive line ending in the escape in force reads its argument
        # from the next line (lr_next; locfile.c:126): `\\` by default, `/`
        # once `escape_char /` was read (false-negative-reviewer, round 3).
        for head in (('escape_char \\', 'X'),
                     ('escape_char /', 'escape_char /', 'X')):
            with self.subTest(head):
                self.assertIsNotNone(ck.Scan(locale_text('<U00XE9> IGNORE',
                                                         head=head)).odd)

    def test_a_file_read_with_unsettled_characters_keeps_its_hit(self):
        """glibc_locale_data.reading_chars gives None for a comment_char
        line below the first rule: nothing is then a certain comment."""
        text = locale_text('% <U00E9>', head=('LC_CTYPE', 'END LC_CTYPE',
                                              'comment_char %'))
        self.assertIsNone(ck._reading(text))
        # The range readers take reading_chars, not the transliteration
        # reader: one name for both once gave them the other (round 6 of
        # the 13.7 review). `LC_TI\ME` opens LC_TIME, so the directives
        # below it are not read: reading_chars says so, the other does not.
        shadow = ('LC_TI\\ME\ncomment_char %\nescape_char /\nEND LC_TIME\n'
                  'LC_COLLATE\norder_start forward\n<U0041>\norder_end\n'
                  'END LC_COLLATE\n')
        self.assertIsNone(ck._reading(shadow))
        self.assertTrue(ck.confirmed_hit(ck.Scan(text), ck.K(frozenset({0xE9}))))

    def test_a_name_split_at_an_escaped_line_end_is_read(self):
        """lr_next joins `<U00/` and `E9>` into <U00E9>
        (linereader.c:152-171@2.39)."""
        for esc in ('/', '\\'):
            with self.subTest(esc):
                scan = ck.Scan(locale_text(f'<U00{esc}', 'E9> <U0041>'))
                self.assertIn('00E9', scan.names().spell)

    def test_a_range_split_at_an_escaped_line_end_is_odd(self):
        """`./` then `.` is `..` to glibc (lr_next): a range on a continued
        line cannot be placed. Kept even when the same text stands as a
        plain line above, which a set of lines lost (false-negative-
        reviewer, round 2)."""
        for body in (('<U00E0>', './', '.', '<U00EF>'),
                     ('<U0041>', '..', '<U0045>', '<U00E0>', './', '.',
                      '<U00EF>')):
            with self.subTest(body):
                self.assertIsNotNone(ck.Scan(locale_text(*body)).odd)

    def test_a_copy_split_at_an_escaped_line_end_is_followed(self):
        scan = ck.Scan(locale_text('co/', 'py "tmpl"'))
        self.assertEqual(scan.copies, ('tmpl',))

    def test_a_copy_after_a_form_feed_is_followed(self):
        """glibc_locale_data.copy_targets follows it (`\\s*`), and so does
        glibc; the closure did not (false-negative-reviewer, round 3)."""
        self.assertEqual(ck.Scan(locale_text('\fcopy "tmpl"')).copies,
                         ('tmpl',))

    def test_a_copy_with_no_blank_before_the_string_is_followed(self):
        """Backlog 13.17: get_ident ends `copy` at the `"`."""
        for line in ('copy"tmpl"', 'copy\v"tmpl"'):
            with self.subTest(line=line):
                self.assertEqual(ck.Scan(locale_text(line)).copies,
                                 ('tmpl',))

    def test_the_range_readers_ask_reading_chars(self):
        """Scan's ranges and range_interval, for the range's own file and
        for another file of the run, take the comment and escape
        characters from _reading (glibc_locale_data.reading_chars), never
        from the transliteration reader: one name for both once gave them
        the other (round 6 of the 13.7 review)."""
        def refuse(text):
            raise AssertionError('a range reader asked _ctype_reading')
        texts = {name: locale_text('order_start forward', lo, '..', hi,
                                   'order_end')
                 for name, lo, hi in (('loc', '<U0041>', '<U0045>'),
                                      ('two', '<U0050>', '<U0055>'))}
        real = ck._ctype_reading
        ck._ctype_reading = refuse
        try:
            ck.reset()
            side = ck.Side('t', set(texts), texts, {}, {})
            off = side.scan('loc').line_ranges[-1]
            self.assertEqual(ck.range_interval(side, ('loc', 'two'), 'loc',
                                               off), (0x41, 0x45))
        finally:
            ck._ctype_reading = real

    def test_a_line_range_settles_only_between_two_order_lines(self):
        """H4: glibc starts a `..` range at its cursor, which an order line
        naming something already defined does not move."""
        def interval(*body, others=()):
            texts = {'loc': locale_text('order_start forward', *body,
                                        'order_end')}
            texts.update(others)
            side = ck.Side('t', set(texts), texts, {}, {})
            scan = side.scan('loc')
            off = scan.line_ranges[-1]          # the last range in the block
            return ck.range_interval(side, ('loc',) + tuple(others), 'loc',
                                     off)
        self.assertEqual(interval('<U0041>', '..', '<U0045>'), (0x41, 0x45))
        self.assertIsNone(interval('<U0041>', '<U0042>', '<U0041>', '..',
                                   '<U0045>'))
        self.assertIsNone(interval('<U0041>', '..'))     # order_end below
        self.assertIsNone(interval('<SYM>', '..', '<U0045>'))
        self.assertIsNone(interval('<U0041>', '..', '<U0045>',
                                   others={'base': locale_text('<U0041>')}))
        # The line above is a weight of a continued line, which leaves the
        # cursor at <U0030>; the same with the start defined by an escaped
        # name, by a raw character, or by an earlier range of the run.
        self.assertIsNone(interval('<U0030> <U0030>;/', '<U0041>', '..',
                                   '<U0050>'))
        # The line above runs on into the `..` line: one statement, no
        # range of its own.
        self.assertIsNone(interval('<U0041>/', '..', '<U0050>'))
        # A head after a form feed is a head: lr_token skips every isspace.
        self.assertIsNone(interval('<U0030>', '\f<U0041> IGNORE', '<U0039>',
                                   '<U0041>', '..', '<U00FF>'))
        self.assertIsNone(interval('<U00/41>', '<U0035>', '<U0041>', '..',
                                   '<U0050>'))
        self.assertIsNone(interval('\u00e0', '<U0035>', '<U00E0>', '..',
                                   '<U00F0>'))
        self.assertIsNone(interval('<U0040>', '..', '<U0042>', '<U0035>',
                                   '<U0041>', '..', '<U0050>'))
        # Defined by a range that opens a line, by a name split across a
        # continued line, by a raw character in another file of the run.
        self.assertIsNone(interval('<U0020>..<U0035> IGNORE', '<U0030>',
                                   '..', '<U0050>'))
        self.assertIsNone(interval('<U00/', 'E0>', '<U0035>', '<U00E0>',
                                   '..', '<U00F0>'))
        self.assertIsNone(interval('<U00E0>', '..', '<U00F0>',
                                   others={'base': locale_text(
                                       '\u00e0 IGNORE')}))

    def test_range_forms(self):
        decimal = ck.Scan(locale_text('<U0041>....<U0050> IGNORE'))
        self.assertEqual(decimal.inline, ((0x41, 0x50),))    # H5
        self.assertIsNone(decimal.odd)
        self.assertTrue(ck.Scan(locale_text('<U0041>', '...', '<U0050>'))
                        .absolute)                           # H8
        self.assertIsNotNone(ck.Scan(locale_text('<U0041>', '..(2)..',
                                                 '<U0050>')).odd)
        weights = ck.Scan(locale_text('<U4E00> <U4E00>;IGNORE',
                                      '.. ..;IGNORE', '<U9FA5> <U9FA5>;IGNORE'))
        self.assertIsNone(weights.odd)
        self.assertEqual(len(weights.line_ranges), 1)
        symbols = ck.Scan(locale_text('collating-symbol <S0041>..<S005A>'))
        self.assertEqual((symbols.odd, symbols.inline), (None, ()))

    def test_a_repertoiremap_is_odd_and_a_comment_naming_one_is_not(self):
        """H10: with a repertoire map loaded, a name the charmap lacks
        resolves through it (ld-collate.c:966-971@2.39). Unused upstream;
        sgs_LT names it in a comment."""
        self.assertIsNotNone(ck.Scan('repertoiremap mnemonic\n'
                                     + locale_text('<U0041>')).odd)
        self.assertIsNone(ck.Scan('% repertoiremap mnemonic\n'
                                  + locale_text('<U0041>')).odd)

    def test_a_section_the_search_does_not_read_is_odd(self):
        """H11: glibc reads every LC_COLLATE section and the keyword after
        blanks; an argument naming the category is neither."""
        two = locale_text('<U0041>') + 'LC_COLLATE\n<U0042>\nEND LC_COLLATE\n'
        self.assertIsNotNone(ck.Scan(two).odd)
        indented = 'comment_char %\n  LC_COLLATE\n<U0041>\nEND LC_COLLATE\n'
        self.assertIsNotNone(ck.Scan(indented).odd)
        ident = locale_text('<U0041>') + (
            'LC_IDENTIFICATION\ncategory "i18n:2012";LC_COLLATE\n'
            'END LC_IDENTIFICATION\n')
        self.assertIsNone(ck.Scan(ident).odd)


def sides(old_texts, new_texts, old_cm, new_cm):
    """Two Sides from {name: text} and {charmap: text}; a charmap's blob id
    is its text, so identical texts compare as one blob."""
    def side(tag, texts, cms):
        return ck.Side(tag, set(texts), dict(texts),
                       {n: f'id:{hash(t)}' for n, t in cms.items()},
                       dict(cms))
    return side('t1', old_texts, old_cm), side('t2', new_texts, new_cm)


class CompareBuilds(unittest.TestCase):
    """compare() over two Sides built in memory: which builds it lists,
    counts, or cannot settle."""

    UTF8_OLD = charmap('<U0041> /x41', '<U0042> /x42')
    UTF8_NEW = charmap('<U0041> /x41', '<U0042> /x42', '<U00E9> /xc3/xa9')

    def builds(self, *lines):
        return ck.supported_builds('SUPPORTED-LOCALES=\\\n' + ''.join(
            f'{ln} \\\n' for ln in lines), 't')

    def run_compare(self, texts, old_cm, new_cm, supported, listed=(),
                    new_texts=None):
        old, new = sides(texts, new_texts or texts, old_cm, new_cm)
        b = self.builds(*supported)
        return ck.compare(old, new, b, b, lambda: set(listed))

    def test_a_build_whose_rules_name_a_changed_character_is_listed(self):
        r = self.run_compare({'loc_a': locale_text('<U00E9> IGNORE'),
                              'loc_b': locale_text('<U0041> IGNORE')},
                             {'UTF-8': self.UTF8_OLD}, {'UTF-8': self.UTF8_NEW},
                             ['loc_a.UTF-8/UTF-8', 'loc_b.UTF-8/UTF-8'])
        self.assertEqual([(f.source, f.chars, f.reason) for f in r.found],
                         [('loc_a', {0xE9}, None)])
        self.assertEqual(ck.listed_sources(r), ['loc_a'])

    def test_a_copier_is_listed_through_the_file_it_copies(self):
        r = self.run_compare({'loc_c': locale_text('copy "tmpl"'),
                              'tmpl': locale_text('<U00E9> IGNORE')},
                             {'UTF-8': self.UTF8_OLD}, {'UTF-8': self.UTF8_NEW},
                             ['loc_c.UTF-8/UTF-8'])
        # tmpl itself is listed too, by point 5: no SUPPORTED entry builds
        # it, and a file with rules is a locale a distro can build (ab_GE at
        # glibc-2.39 is one, outside SUPPORTED).
        self.assertEqual([(f.source, f.entry, f.via) for f in r.found],
                         [('loc_c', 'loc_c.UTF-8', ['tmpl']),
                          ('tmpl', None, ['tmpl'])])

    def test_a_line_range_spanning_a_changed_character_is_a_hit(self):
        r = self.run_compare({'loc': locale_text('order_start forward',
                                                 '<U00E0>', '..', '<U00EF>',
                                                 'order_end')},
                             {'UTF-8': self.UTF8_OLD}, {'UTF-8': self.UTF8_NEW},
                             ['loc.UTF-8/UTF-8'])
        self.assertEqual([f.chars for f in r.found], [{0xE9}])

    def test_an_identical_blob_is_not_read(self):
        r = self.run_compare({'loc_a': locale_text('<U00E9> IGNORE')},
                             {'UTF-8': self.UTF8_NEW}, {'UTF-8': self.UTF8_NEW},
                             ['loc_a.UTF-8/UTF-8'])
        self.assertEqual((r.changed, r.found), ([], []))

    def test_a_build_step_3_lists_anyway_is_counted_not_repeated(self):
        r = self.run_compare({'loc_a': locale_text('<U00E9> IGNORE')},
                             {'UTF-8': self.UTF8_OLD}, {'UTF-8': self.UTF8_NEW},
                             ['loc_a.UTF-8/UTF-8'], listed={'loc_a'})
        self.assertEqual(([f.source for f in r.skipped], r.found),
                         (['loc_a'], []))

    def test_a_source_a_distro_can_build_in_a_changed_charmap(self):
        """H3: Red Hat builds en_US in ISO-8859-15, which SUPPORTED never
        does. A source with no build in a changed charmap whose rules name a
        changed character is listed, with no SUPPORTED entry; not in a
        charmap PostgreSQL never uses on a server (GB18030)."""
        old15 = charmap('<U0041> /x41', '<U00E9> /xe9',
                        prolog=('<code_set_name> ISO-8859-15',) + _PROLOG[1:])
        new15 = charmap('<U0041> /x41', '<U00E9> /xea',
                        prolog=('<code_set_name> ISO-8859-15',) + _PROLOG[1:])
        texts = {'en_XX': locale_text('<U00E9> IGNORE'),
                 'de_XX': locale_text('<U0041> IGNORE')}
        supported = ['en_XX.UTF-8/UTF-8', 'de_XX/CS']
        r = self.run_compare(texts, {'UTF-8': self.UTF8_NEW, 'CS': old15},
                             {'UTF-8': self.UTF8_NEW, 'CS': new15}, supported)
        self.assertEqual([(f.source, f.entry, f.charmaps[1]) for f in r.found],
                         [('en_XX', None, 'CS')])
        gb_old = old15.replace('ISO-8859-15', 'GB18030')
        gb_new = new15.replace('ISO-8859-15', 'GB18030')
        r = self.run_compare(texts, {'UTF-8': self.UTF8_NEW, 'CS': gb_old},
                             {'UTF-8': self.UTF8_NEW, 'CS': gb_new}, supported)
        self.assertEqual(r.found, [])

    def test_an_entry_renamed_between_the_tags_is_one_build(self):
        """A build is a source in a charmap: az_AZ.UTF-8 at one tag and
        az_AZ at the next (2.12..2.17) is the same build. Matched by entry
        it was compared by neither path."""
        old, new = sides({'xx_YY': locale_text('<U00E9> IGNORE')},
                         {'xx_YY': locale_text('<U00E9> IGNORE')},
                         {'UTF-8': self.UTF8_OLD}, {'UTF-8': self.UTF8_NEW})
        r = ck.compare(old, new, self.builds('xx_YY.UTF-8/UTF-8'),
                       self.builds('xx_YY/UTF-8'), set)
        self.assertEqual([(f.source, f.entry) for f in r.found],
                         [('xx_YY', 'xx_YY')])

    def test_a_build_only_at_one_tag_is_searched_as_a_distro_s(self):
        """At the old tag only a distro could build loc in CS, H3's own
        case, so a build SUPPORTED adds at the new tag does not exempt it
        from the search."""
        cs_old = charmap('<U0041> /x41', '<U00E9> /xe9',
                         prolog=('<code_set_name> CS',) + _PROLOG[1:])
        cs_new = cs_old.replace('/xe9', '/xea')
        texts = {'loc': locale_text('<U00E9> IGNORE'),
                 'de_XX': locale_text('<U0041> IGNORE')}
        old, new = sides(texts, texts, {'UTF-8': self.UTF8_NEW, 'CS': cs_old},
                         {'UTF-8': self.UTF8_NEW, 'CS': cs_new})
        r = ck.compare(old, new,
                       self.builds('loc.UTF-8/UTF-8', 'de_XX/CS'),
                       self.builds('loc.UTF-8/UTF-8', 'de_XX/CS', 'loc/CS'),
                       set)
        self.assertEqual([(f.source, f.entry, f.charmaps[1])
                          for f in r.found], [('loc', None, 'CS')])

    def test_what_it_cannot_settle_is_listed_with_its_reason(self):
        cases = {
            'a charmap it does not read':
                ({'loc': locale_text('<U0041> IGNORE')},
                 charmap('<U0041> /x41', prolog=_PROLOG + ('<include> X',))),
            'a code_set_name that changes':
                ({'loc': locale_text('<U0041> IGNORE')},
                 charmap('<U0041> /x41',
                         prolog=('<code_set_name> OTHER',) + _PROLOG[1:])),
            'a copy target that is not there':
                ({'loc': locale_text('copy "gone"')}, self.UTF8_NEW),
            'copies in a cycle':
                ({'loc': locale_text('copy "loop"'),
                  'loop': locale_text('copy "loc"')}, self.UTF8_NEW),
            'a second section':
                ({'loc': locale_text('<U0041> IGNORE')
                  + 'LC_COLLATE\n<U0042>\nEND LC_COLLATE\n'}, self.UTF8_NEW),
        }
        for why, (texts, new_cm) in cases.items():
            with self.subTest(why):
                r = self.run_compare(texts, {'UTF-8': self.UTF8_OLD},
                                     {'UTF-8': new_cm}, ['loc.UTF-8/UTF-8'])
                got = {f.source: f.reason for f in r.found if f.entry}
                self.assertEqual(list(got), ['loc'])
                self.assertIsNotNone(got['loc'])

    def test_an_absolute_range_is_unsettled_even_when_k_is_empty(self):
        """H8: `...` walks the charmap's byte table, which a blob that
        changes only in its comments can still reorder (the first name of
        a byte sequence)."""
        r = self.run_compare({'loc': locale_text('<U0041>', '...', '<U0042>')},
                             {'UTF-8': self.UTF8_OLD},
                             {'UTF-8': self.UTF8_OLD.replace(
                                 'CHARMAP', '% changed\nCHARMAP', 1)},
                             ['loc.UTF-8/UTF-8'])
        self.assertEqual(r.sizes, {('UTF-8', 'UTF-8'): (0, 0, 0)})
        self.assertEqual([f.source for f in r.found], ['loc'])
        self.assertIn('`...`', r.found[0].reason)

    def test_a_distro_build_with_an_absolute_range_and_k_empty(self):
        """Point 5 ran only when K had members, and `...` can move with K
        empty: a source no SUPPORTED entry builds in the charmap, with `...`
        in its rules, was never evaluated (false-negative-reviewer, round
        2)."""
        r = self.run_compare({'loc': locale_text('<U0041> IGNORE'),
                              'other': locale_text('<U0041>', '...',
                                                   '<U0042>')},
                             {'UTF-8': self.UTF8_OLD},
                             {'UTF-8': self.UTF8_OLD.replace(
                                 'CHARMAP', '% changed\nCHARMAP', 1)},
                             ['loc.UTF-8/UTF-8'])
        self.assertEqual([(f.source, f.entry) for f in r.found],
                         [('other', None)])
        self.assertIn('`...`', r.found[0].reason)

    def test_a_keyword_the_search_does_not_read_with_k_empty(self):
        """With K empty only a `...` range can move, and glibc reads every
        LC_COLLATE section: a `...` in a second section or under a keyword
        after blanks was dropped with every other odd reason when K was
        empty, for SUPPORTED builds and for point 5 (false-negative-
        reviewer, round 3)."""
        second = (locale_text('<U0041> IGNORE')
                  + 'LC_COLLATE\n<U0041>\n...\n<U0042>\nEND LC_COLLATE\n')
        r = self.run_compare({'loc': second}, {'UTF-8': self.UTF8_OLD},
                             {'UTF-8': self.UTF8_OLD.replace(
                                 'CHARMAP', '% changed\nCHARMAP', 1)},
                             ['loc.UTF-8/UTF-8'])
        self.assertEqual([(f.source, f.entry) for f in r.found],
                         [('loc', 'loc.UTF-8')])
        self.assertIsNotNone(r.found[0].reason)

    def test_point_5_reads_a_file_whose_keyword_has_blanks_before_it(self):
        indented = ('comment_char %\nescape_char /\n  LC_COLLATE\n'
                    '<U00E9> IGNORE\nEND LC_COLLATE\n')
        r = self.run_compare({'loc': locale_text('<U0041> IGNORE'),
                              'ind': indented},
                             {'UTF-8': self.UTF8_OLD}, {'UTF-8': self.UTF8_NEW},
                             ['loc.UTF-8/UTF-8'])
        self.assertEqual([(f.source, f.entry) for f in r.found],
                         [('ind', None)])
        self.assertIsNotNone(r.found[0].reason)

    def test_a_charmap_supported_names_that_is_not_there_is_not_run(self):
        old, new = sides({'loc': locale_text('<U0041>')},
                         {'loc': locale_text('<U0041>')},
                         {'UTF-8': self.UTF8_OLD}, {})
        b = self.builds('loc.UTF-8/UTF-8')
        r = ck.compare(old, new, b, b, set)
        self.assertIn('UTF-8 at t2', r.not_run)

    def test_supported_lines_it_does_not_read_are_unsettled(self):
        for line in ('a/b/c', 'a b/c', '/c', 'a/'):
            with self.subTest(line):
                with self.assertRaises(ck.Unsettled):
                    self.builds(line)
        self.assertEqual([b.entry for b in self.builds('sv_SE.UTF-8/UTF-8')],
                         ['sv_SE.UTF-8'])



class CharmapReportLines(unittest.TestCase):
    """Lines print_charmap_report writes that no real pair reaches."""

    def lines(self, **fields):
        base = dict(not_run=None, builds=2, charmaps=['UTF-8'],
                    changed=[('UTF-8', 'UTF-8')],
                    sizes={('UTF-8', 'UTF-8'): (1, 1, 0)}, found=[],
                    skipped=[], not_compared=[], unsearched=[])
        base.update(fields)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f.print_charmap_report(ck.Report(**base), 't1', 't2')
        return buf.getvalue()

    def test_the_clean_line_says_compared_when_entries_were_not(self):
        out = self.lines(not_compared=['ghost.UTF-8'])
        self.assertIn("No compared build's LC_COLLATE names one of those "
                      "characters.", out)
        self.assertIn('!! 1 SUPPORTED entry at both tags is not compared',
                      out)
        self.assertIn("No build's LC_COLLATE", self.lines())

    def test_a_code_point_beyond_unicode_is_printed_as_a_number(self):
        found = ck.Found('loc', 'loc.UTF-8', ('UTF-8', 'UTF-8'),
                         {0x41, 0x110000}, ['loc'], None)
        out = self.lines(found=[found])
        self.assertIn('2 characters, named in loc', out)
        self.assertIn('A (U+0041)  U+110000', out)



# --- step 2's transliteration check (backlog 13.7, PR C) --------------------

def _ascii_charmap(*extra):
    """ISO-8859-1's shape: ASCII, no U+02BB and no U+2018."""
    return charmap(*[f'<U{c:04X}> /x{c:02x}' for c in range(0x20, 0x7f)],
                   *extra)


def ctype_text(ctype=None, collate=None, ctype_first=True):
    """A locale file with an LC_CTYPE body and an LC_COLLATE body, in that
    order or the other."""
    head = 'comment_char %\nescape_char /\n'
    c = f'LC_CTYPE\n{ctype}\nEND LC_CTYPE\n' if ctype is not None else ''
    o = (f'LC_COLLATE\n{collate}\nEND LC_COLLATE\n' if collate is not None
         else '')
    return head + (c + o if ctype_first else o + c)


def translit(*lines):
    return 'translit_start\n' + '\n'.join(lines) + '\ntranslit_end'


# i18n as glibc writes it: `copy "i18n_ctype"`, then an include whose only
# answer for U+02BB is U+2018, which the charmap lacks (M1 c and d).
I18N = {
    'i18n_ctype': ctype_text('upper <U0041>'),
    'i18n': ctype_text('copy "i18n_ctype"\n\n'
                       + translit('include "translit_neutral";""')),
    'translit_neutral': ctype_text(translit('<U02BB> "<U2018>"')),
    'Z': ctype_text(translit('<U02BB> "<U0060>"')),
}
ELEMENT = ('collating-element <gtc> from "g<U02BB>"\norder_start forward\n'
           '<gtc>\norder_end')
GRAVE = '<U02BB> "<U0060>"'
APOS = '<U02BB> "<U0027>"'


class TranslitTable(unittest.TestCase):
    """TranslitRun against M1 of the 13.7 handoff: the element
    `g` + U+02BB, which ISO-8859-1 cannot hold, built with the table glibc
    has when it reads the from string, on RHEL 8, 9 and 10. D defines the
    element; M is the build's source. Every expected key is the measured
    one (`.claude/glibc-study/m137/pr2-rediseno/nodes/README.md`)."""

    CASES = {
        # M's LC_CTYPE writes into i18n's table before its LC_COLLATE copies D.
        'a': ({'D': ctype_text('copy "i18n"', ELEMENT),
               'M': ctype_text('copy "i18n"\n' + translit(GRAVE),
                               'copy "D"')}, 'D', '/x67/x60'),
        # The same, M's LC_COLLATE first: M's entry is read too late.
        'b': ({'D': ctype_text('copy "i18n"', ELEMENT),
               'M': ctype_text('copy "i18n"\n' + translit(GRAVE), 'copy "D"',
                               ctype_first=False)}, 'D', None),
        'c': ({'D': ctype_text('copy "i18n"', ELEMENT),
               'M': ctype_text('copy "i18n"', 'copy "D"')}, 'D', None),
        # A table without `copy` is M's own.
        'e': ({'D': ctype_text('copy "i18n"', ELEMENT),
               'M': ctype_text(translit(GRAVE), 'copy "D"')}, 'D', None),
        # An include M adds goes to the end of the shared table.
        'g': ({'D': ctype_text('copy "i18n"', ELEMENT),
               'M': ctype_text('copy "i18n"\n' + translit('include "Z";""'),
                               'copy "D"')}, 'D', '/x67/x60'),
        # D with no LC_CTYPE never transliterates.
        'h': ({'D': ctype_text(None, ELEMENT),
               'M': ctype_text('copy "i18n"\n' + translit(GRAVE),
                               'copy "D"')}, 'D', None),
        # D's own entry is read at the lookup, after M's, and wins.
        'i': ({'D': ctype_text('copy "i18n"\n' + translit(APOS), ELEMENT),
               'M': ctype_text('copy "i18n"\n' + translit(GRAVE),
                               'copy "D"')}, 'D', '/x67/x27'),
        # A file reached by `copy` in LC_COLLATE is read for LC_COLLATE only.
        'j': ({'D': ctype_text('copy "i18n"', ELEMENT),
               'X': ctype_text('copy "i18n"\n' + translit(GRAVE), 'copy "D"'),
               'M': ctype_text('copy "i18n"', 'copy "X"')}, 'D', None),
        # M copied D's LC_CTYPE first, so M's own entry is the newer one.
        'k': ({'Down': ctype_text('copy "i18n"\n' + translit(APOS), ELEMENT),
               'M': ctype_text('copy "Down"\n' + translit(GRAVE),
                               'copy "Down"')}, 'Down', '/x67/x60'),
        'l': ({'Down': ctype_text('copy "i18n"\n' + translit(APOS), ELEMENT),
               'M': ctype_text('copy "Down"\n' + translit(GRAVE),
                               'copy "Down"', ctype_first=False)},
              'Down', '/x67/x27'),
        # N3c: the later of two lines wins; the first alternative the
        # charmap has; an entry with none leaves the answer to the includes.
        'later line': ({'D': ctype_text('copy "i18n"\n' + translit(APOS, GRAVE),
                                        ELEMENT),
                        'M': ctype_text('copy "i18n"', 'copy "D"')},
                       'D', '/x67/x60'),
        'first available': ({'D': ctype_text(
            'copy "i18n"\n' + translit('<U02BB> "<U2018>";"<U0060>"'),
            ELEMENT), 'M': ctype_text('copy "i18n"', 'copy "D"')},
            'D', '/x67/x60'),
        # A bare <U....> is not checked when it is read, only when it is
        # looked up (find_translit2): the next alternative answers.
        'first available bare': ({'D': ctype_text(
            'copy "i18n"\n' + translit('<U02BB> <U2018>;<U0060>'), ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')}, 'D', '/x67/x60'),
        'include answers': ({'D': ctype_text(
            'copy "i18n"\n' + translit('include "Z";""', '<U02BB> "<U2018>"'),
            ELEMENT), 'M': ctype_text('copy "i18n"', 'copy "D"')},
            'D', '/x67/x60'),
        # A trailing `;` is an empty alternative: the character is dropped.
        'empty alternative': ({'D': ctype_text(
            'copy "i18n"\n' + translit('<U02BB> "<U2018>";'), ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')}, 'D', '/x67'),
        # The newest entry has no alternative the charmap holds: the
        # older entries of the table are not asked, only the includes.
        'newest without answer': ({'D': ctype_text(
            'copy "i18n"\n' + translit('<U02BB> "<U2018>"'), ELEMENT),
            'M': ctype_text('copy "i18n"\n' + translit(GRAVE), 'copy "D"')},
            'D', None),
        # A line glibc cannot read is no entry: the older line answers.
        'dropped newest': ({'D': ctype_text(
            'copy "i18n"\n' + translit(GRAVE, '<U02BB> ;"<U0027>"'), ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')}, 'D', '/x67/x60'),
    }

    def key(self, files, defining):
        texts = dict(I18N, **files)
        ck.reset()
        cm = ck.read_charmap(_ascii_charmap(), 'TEST')
        side = ck.Side('t', set(texts), texts, {'TEST': '1'}, {})
        run = ck.TranslitRun(side, cm, 'M')
        parts = ck.elements_of(side.scan(defining))['<gtc>']
        return ck._element_key(parts, cm, run, defining)[0]

    def test_every_measured_case(self):
        for name, (files, defining, want) in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(self.key(files, defining), want)

    def unsettled(self, files, defining='D'):
        with self.assertRaises(ck.Unsettled) as cm:
            self.key(files, defining)
        return str(cm.exception)

    def test_translit_ignore_is_not_settled(self):
        """H12: find_translit2 never moves past a translit_ignore range that
        does not answer, so glibc does not finish."""
        why = self.unsettled({'D': ctype_text(
            'copy "i18n"\n' + translit('translit_ignore <U0300>'), ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')})
        self.assertIn('translit_ignore', why)

    def test_a_cycle_is_not_settled(self):
        """H12 too: a copy cycle, and an include that leads back to the
        table being searched, never come back with an answer."""
        self.assertIn('cycle', self.unsettled({
            'D': ctype_text('copy "E"', ELEMENT),
            'E': ctype_text('copy "D"'),
            'M': ctype_text('copy "i18n"', 'copy "D"')}))
        self.assertIn('leads back', self.unsettled({
            'D': ctype_text('copy "i18n"\n' + translit('include "i18n";""'),
                            ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')}))

    def test_a_source_spelled_with_escapes_is_refused(self):
        """M's LC_CTYPE head and END built with the escape: glibc reads
        it before M's LC_COLLATE; the run is refused, never built without
        it."""
        m = ctype_text('copy "i18n"\n' + translit(GRAVE), 'copy "D"') \
            .replace('LC_CTYPE', 'LC_C/TYPE')
        self.assertIn('LC_CTYPE', self.unsettled(
            {'D': ctype_text('copy "i18n"', ELEMENT), 'M': m}))

    def test_lookups_in_two_files_are_not_settled(self):
        files = {'D': ctype_text('copy "i18n"', ELEMENT),
                 'M': ctype_text('copy "i18n"', 'copy "D"')}
        texts = dict(I18N, **files)
        ck.reset()
        cm = ck.read_charmap(_ascii_charmap(), 'TEST')
        run = ck.TranslitRun(ck.Side('t', set(texts), texts, {}, {}), cm, 'M')
        run.lookup('D', 0x2BB)
        with self.assertRaises(ck.Unsettled):
            run.lookup('M', 0x2BB)

    def test_a_missing_include_is_not_settled(self):
        self.assertIn('is not there', self.unsettled({
            'D': ctype_text('copy "i18n"\n' + translit('include "nope";""'),
                            ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')}))


class TranslitLines(unittest.TestCase):
    """How a translit line is read: its head, and its alternatives as
    read_translit_entry and get_string take them (ld-ctype.c:1786-1917,
    linereader.c:773-1000@2.39)."""

    CHARS = ('%', '/')

    def alts(self, rest):
        return ck._alternatives(rest, self.CHARS)

    def test_alternatives(self):
        for rest, want in (
                (' "<U2018>";"<U0027>" % a comment', [[0x2018], [0x27]]),
                (' <U0068><U0065>', [[0x68, 0x65]]),
                (' "<U2018>";', [[0x2018], []]),
                (' "<U2018>";;"<U0027>"', [[0x2018], [], [0x27]]),
                (' ""', [None]),
                (' <foo>;"x"', [None, [0x78]]),
                (' "<foo>";"x"', [None, [0x78]]),
                (' "a/"b"', [[0x61, 0x22, 0x62]]),
                (' ;"x"', None),
                ('', None),
                ('  % nothing but a comment', None)):
            with self.subTest(rest):
                self.assertEqual(self.alts(rest), want)

    def test_a_string_the_charmap_cannot_hold_is_dropped_as_glibc_reads(self):
        """get_string with no locale drops a string naming a `<U....>` the
        charmap lacks (linereader.c:897-899@2.39), and what the alternative
        had collected stays in glibc's buffer for the next one: Unsettled.
        Alone in its alternative it is just dropped."""
        has = lambda c: c != 0x2018
        self.assertEqual(ck._alternatives(' "<U2018>";"<U0060>"', self.CHARS,
                                          has), [None, [0x60]])
        with self.assertRaises(ck.Unsettled):
            ck._alternatives(' <U0067>"<U2018>";"<U0060>"', self.CHARS, has)

    def test_what_is_not_read_is_unsettled(self):
        for rest in (' /x27', ' "<foo>" "x";"y"', ' "‘"', ' "x',
                     ' default_missing', ' 39'):
            with self.subTest(rest), self.assertRaises(ck.Unsettled):
                self.alts(rest)

    def ct(self, *lines):
        text = ctype_text(translit(*lines))
        return text, ck.ctype_of(text)

    def heads(self, *lines, cp=0x2BB):
        text, ct = self.ct(*lines)
        self.assertIsNone(ct.odd)
        return [rest for _, rest in ck._translit_lines(text, ct, cp)]

    def test_heads(self):
        """A `<U....>` head in either case of hex and either length; a
        quoted head followed by strings, a comment and another code point
        are not entries for U+02BB."""
        self.assertEqual(self.heads('<U02bb> "a"', '<U000002BB> "b"',
                                    '"<U02BB>" "d"', '% <U02BB> "e"',
                                    '<U02BC> "f"'),
                         [' "a"', ' "b"'])

    def test_a_head_spelled_another_way_is_refused(self):
        """`<u02bb>` is a symbol whose entry glibc drops, leaving the rest
        of the line read as an entry of its own; `<U02/BB>` names U+02BB
        through the escape; a raw head is read as the code point from 2.36
        on and not before. The first two make the block unread, the third
        refuses the lookup of that code point only."""
        for line in ('<u02bb> "c"', '<U02/BB> "a"', '"" <U02BB> "a"',
                     '"" include "Z";""'):
            with self.subTest(line):
                self.assertIsNotNone(self.ct(line)[1].odd)
        text, ct = self.ct('ʻ "a"', '<U02BC> "b"')
        self.assertIsNone(ct.odd)
        with self.assertRaises(ck.Unsettled):
            ck._translit_lines(text, ct, 0x2BB)
        self.assertEqual(len(ck._translit_lines(text, ct, 0x2BC)), 1)


class TranslitCtype(unittest.TestCase):
    """ctype_of reads LC_CTYPE as ctype_read does for the forms every
    LC_CTYPE from 2.12 to 2.42 uses, and refuses every other: the four
    rounds of false-negative-reviewer on PR C each found a spelling the
    narrower checks before it read differently from glibc."""

    def test_copy_includes_and_blocks(self):
        ct = ck.ctype_of(ctype_text('copy "i18n"\n\n' + translit(
            'include "a";""', 'include  "<U0062>";""', '<U0041> "a"')
            + '\n' + translit('include "c";""')))
        self.assertIsNone(ct.odd)
        self.assertEqual((ct.copy, ct.includes), ('i18n', ('a', 'b', 'c')))
        self.assertEqual(len(ct.blocks), 2)
        self.assertIsNone(ck.ctype_of(ctype_text(None, ELEMENT)))
        ident = ('comment_char %\nLC_IDENTIFICATION\ncategory '
                 '"i18n:2012";LC_CTYPE\nEND LC_IDENTIFICATION\n')
        self.assertIsNone(ck.ctype_of(ident))

    def test_spellings_glibc_reads_as_copy_and_include(self):
        """get_ident ends a word at `"` and keeps the character after the
        escape inside it (linereader.c:569-589@2.39): `copy"X"` and
        `co/py "X"` are copies, `include"X";""` an include."""
        for body in ('copy"i18n"', 'co/py "i18n"'):
            with self.subTest(body):
                ct = ck.ctype_of(ctype_text(body))
                self.assertEqual((ct.odd, ct.copy), (None, 'i18n'))
                self.assertIn('i18n', ck._named_files(ctype_text(body)))
        ct = ck.ctype_of(ctype_text(translit('include"Z";""')))
        self.assertEqual((ct.odd, ct.includes), (None, ('Z',)))
        # Spellings it does not take are refused, and still followed by
        # the search for names, as a superset.
        for body in (translit('in/clude "i18n";""'), translit('inc/\nlude '
                                                              '"i18n";""')):
            with self.subTest(body):
                self.assertIsNotNone(ck.ctype_of(ctype_text(body)).odd)
                self.assertIn('i18n', ck._named_files(ctype_text(body)))

    def test_what_is_odd(self):
        body = translit('<U02BB> "a"')
        good = ctype_text(body)
        self.assertIsNone(ck.ctype_of(good).odd)
        for name, text in (
                ('second section', good + 'LC_CTYPE\nEND LC_CTYPE\n'),
                ('indented keyword', 'comment_char %\n  LC_CTYPE\n'
                 'copy "i18n"\nEND LC_CTYPE\n'),
                ('indented beside one', good + '  LC_CTYPE\nEND LC_CTYPE\n'),
                ('no END', 'comment_char %\nLC_CTYPE\ncopy "i18n"\n'),
                ('copy not first', ctype_text('upper <U0041>\ncopy "i18n"')),
                ('another word for copy', ctype_text('copying "x"')),
                ('repertoire include', ctype_text(translit(
                    'include "a";"rep"'))),
                ('include outside', ctype_text('include "a";""')),
                ('unquoted include', ctype_text(translit('include a;""'))),
                ('escaped include', ctype_text(translit(
                    'include "trans/lit";""'))),
                ('translit_ignore', ctype_text(translit(
                    'translit_ignore <U0300>'))),
                ('escaped translit_ignore', ctype_text(translit(
                    'translit_ig/nore <U0300>'))),
                ('continued keyword', ctype_text('upper <U0041>;/\n' + body)),
                ('continued entry', ctype_text(translit('<U0041> "a";/',
                                                        '"b"'))),
                ('unclosed block', ctype_text('translit_start\n<U0041> "a"')),
                ('joined block', ctype_text('copy "i18n"\ntranslit_st/\nart\n'
                                            '<U02BB> "<U0060>"\ntranslit_e/\n'
                                            'nd')),
                ('balanced', ctype_text(translit('translit_ignore/x',
                                                 'translit_ignore/y')
                                        + '\ntranslit_st/\nart\n<U02BB> "a"\n'
                                          'translit_e/\nnd')),
                ('head continued', good.replace('LC_CTYPE\n',
                                                'LC_CTYPE /\n', 1)),
                ('above continued', good.replace(
                    'LC_CTYPE\n', 'END LC_IDENTIFICATION/\nLC_CTYPE\n', 1)),
                ('past a comment', good.replace(
                    'LC_CTYPE\n', 'END LC_IDENTIFICATION/\n% note /\n'
                    'LC_CTYPE\n', 1)),
                ('escaped head', good.replace('LC_CTYPE\n', 'LC_CT/YPE\n',
                                              1)),
                ('escaped head and end', good.replace('LC_CTYPE',
                                                      'LC_CT/YPE')),
                ('indented END', ctype_text('  END LC_CTYPE\n' + body)),
                ('bare END', ctype_text('END\n' + body)),
                ('END of another', ctype_text('END LC_COLLATE\n' + body)),
                ('escaped END', ctype_text('EN/D LC_CTYPE\n' + body)),
                ('END escaped name', ctype_text('END LC_C/TYPE\n' + body)),
                ('END after a word', ctype_text('xyz END LC_CTYPE\n' + body)),
                ('line above END', ctype_text(body + '\nupper <U0041>/')),
                ('other comment above END', ctype_text(body + '\n#x/')),
                ('directive above END', ctype_text(body + '\nescape_char /')),
                ('second, escaped', good + 'LC_C/TYPE\n' + body
                 + '\nEND LC_C/TYPE\n'),
                ('escape alone', ctype_text('/\n' + body)),
                ('escape alone above', good.replace('LC_CTYPE\n',
                                                    '/\nLC_CTYPE\n', 1)),
                # Joined to a blank line, so only the escape-alone check
                # sees it.
                ('escape alone, then a blank line', ctype_text(
                    'copy "i18n"\n/\n\n' + body)),
                # The comment takes the next physical line with it.
                ('head with a comment continued', good.replace(
                    'LC_CTYPE\n', 'LC_CTYPE % note /\n', 1)),
                ('keyword after a known statement', ctype_text(
                    'upper <U0041> END LC_CTYPE\n' + body)),
                ('copy naming a keyword', ctype_text('copy "x END"')),
                ('keyword joined after the section', good
                 + 'foo /\nLC_CTYPE\n'),
                ('escape before a second keyword', good + '/ LC_CTYPE\n'
                 + body + '\nEND LC_CTYPE\n'),
                # Round 5. A comment does not stop the join: the next line
                # joins the entry, or lr_ignore_rest drops it.
                ('block comment continued', ctype_text(translit(
                    '<U02BB> "<U0060>" % note /', '<U02BB> "<U0027>"'))),
                ('include comment continued', ctype_text(translit(
                    'include "Z";"" % note /', '<U02BB> "<U0027>"'))),
                ('marker comment continued', ctype_text(
                    'translit_start % note /\n<U02BB> "a"\ntranslit_end')),
                ('END comment continued', good.replace(
                    'END LC_CTYPE\n', 'END LC_CTYPE % note /\n')),
                ('inside another section', good.replace(
                    'LC_CTYPE\n', 'LC_IDENTIFICATION\nLC_CTYPE\n', 1)
                 + 'END LC_IDENTIFICATION\n'),
                ('the END above joined', good.replace(
                    'LC_CTYPE\n', 'LC_IDENTIFICATION\nx /\n'
                    'END LC_IDENTIFICATION\nLC_CTYPE\n', 1)),
                ('a word above that only starts like END', good.replace(
                    'LC_CTYPE\n', 'LC_IDENTIFICATION\nENDING\n'
                    'LC_CTYPE\n', 1)),
                ('two dropped strings, then ;', ctype_text(translit(
                    '"" "";""', 'include "Z";""'))),
                ('the default escape alone', good.replace(
                    'escape_char /\n', '\\\n\nescape_char /\n', 1)),
                ('bytes that are not UTF-8', ctype_text(translit(
                    '<U02BB> "\ufffd"'))),
                # A syntax error on `foo` drops the head with it.
                ('head joined to a plain line above', good.replace(
                    'LC_CTYPE\n', 'foo /\nLC_CTYPE\n', 1)),
                # `#` is no comment where comment_char is `%`.
                ('head joined to a # line above', good.replace(
                    'LC_CTYPE\n', '#x /\nLC_CTYPE\n', 1)),
                # get_ident keeps the character after the escape: glibc
                # opens the section, and LC_CTYPE is inside it.
                ('a section above opened with the escape', good.replace(
                    'LC_CTYPE\n', 'LC_IDENT/IFICATION\nLC_CTYPE\n', 1)),
                ('an opening above in another form', good.replace(
                    'LC_CTYPE\n', 'LC_IDENTIFICATION x\n'
                    'END LC_IDENTIFICATION\nLC_CTYPE\n', 1)),
                # Round 6. The END above is indented; the line that joins
                # it is still the one above its line.
                ('an indented END above joined', good.replace(
                    'LC_CTYPE\n', 'LC_IDENTIFICATION\nx /\n'
                    '  END LC_IDENTIFICATION\nLC_CTYPE\n', 1)),
                ('a tab-indented END above joined', good.replace(
                    'LC_CTYPE\n', 'LC_IDENTIFICATION\nx /\n'
                    '\tEND LC_IDENTIFICATION\nLC_CTYPE\n', 1)),
                # 0x1A ended the file for glibc before 2.36, wherever it
                # stands above the end of LC_CTYPE.
                ('0x1A in a statement', ctype_text(
                    'upper <U0041>\x1a\n' + body)),
                ('0x1A alone above', good.replace(
                    'LC_CTYPE\n', '\x1a\nLC_CTYPE\n', 1)),
                ('0x1A in a comment above', good.replace(
                    'escape_char /\n', 'escape_char /\n% \x1a\n', 1)),
                ('0x1A in a block', ctype_text(translit(
                    '<U02BB> "a" % \x1a'))),
                ('unknown statement', ctype_text('frobnicate <U0041>\n'
                                                 + body)),
                ('a comment run into the block', ctype_text(
                    'upper <U0041>/\n% note /\n' + body)),
                ('repertoiremap', good.replace(
                    'escape_char /\n', 'escape_char /\nrepertoiremap x\n')),
                ('escaped repertoiremap', good.replace(
                    'escape_char /\n', 'escape_char /\nrepertoire/map x\n'))):
            with self.subTest(name):
                ct = ck.ctype_of(text)
                self.assertIsNotNone(ct)
                self.assertIsNotNone(ct.odd)
        # A section above that ends above (with its END indented or not),
        # an entry headed by a string followed by one string (scn_IT at
        # 2.41), and a 0x1A below LC_CTYPE, which glibc meets only after
        # reading it, are read.
        for text in (good.replace('LC_CTYPE\n', 'LC_IDENTIFICATION\n'
                                  'title "x"\nEND LC_IDENTIFICATION\n\n'
                                  'LC_CTYPE\n', 1),
                     good.replace('LC_CTYPE\n', 'LC_IDENTIFICATION\n'
                                  'title "x"\n  END LC_IDENTIFICATION\n'
                                  'LC_CTYPE\n', 1),
                     good + 'LC_TIME\n% \x1a\nEND LC_TIME\n',
                     # Bytes that are not UTF-8 in a comment (with the
                     # comment character in force there) or in a string
                     # are skipped or end only the string.
                     good.replace('escape_char /\n',
                                  'escape_char /\n% caf\ufffd\n', 1),
                     '# caf\ufffd\n' + good,
                     good.replace('LC_CTYPE\n', 'LC_IDENTIFICATION\ntitle '
                                  '"caf\ufffd"\nEND LC_IDENTIFICATION\n'
                                  'LC_CTYPE\n', 1),
                     ctype_text(translit('"a" "b"', '<U02BB> "a"'))):
            with self.subTest(text[:60]):
                self.assertIsNone(ck.ctype_of(text).odd)
        # A comment run into the next line drops only itself, and a
        # directive's argument does not join: neither is refused.
        for text in (good.replace('LC_CTYPE\n', '% a comment /\nLC_CTYPE\n',
                                  1),
                     'comment_char %\nescape_char /\nLC_CTYPE\ncopy "i18n"\n'
                     'END LC_CTYPE\n'):
            with self.subTest(text[:40]):
                self.assertIsNone(ck.ctype_of(text).odd)
        self.assertEqual(ck._joined('a/\n% c /\nb\n% d\nc', '%', '/'),
                         'ab\n\nc')

    def test_a_0xff_byte_is_odd_wherever_it_stands(self):
        """A text read from bytes that held 0xFF (read_blobs_at records it)
        is odd wherever the byte stands, a file with no LC_CTYPE in sight
        included: before 2.36 it ends a string, a section or the file, so
        what glibc reads cannot be told from the text, where it and every
        other byte that is not UTF-8 are one U+FFFD (round 8 of the 13.7
        review). The same texts not so recorded are read."""
        body = translit('<U02BB> "a"')
        good = ctype_text(body)
        texts = {
            'top level': good.replace('LC_CTYPE\n', '\ufffd\nLC_CTYPE\n', 1),
            'in a string above': good.replace(
                'LC_CTYPE\n', 'LC_IDENTIFICATION\ntitle "\ufffd\ufffd '
                'x"\nEND LC_IDENTIFICATION\nLC_CTYPE\n', 1),
            'in a comment': good.replace('escape_char /\n',
                                         'escape_char /\n% \ufffd\n', 1),
            'below LC_CTYPE': good + 'LC_TIME\n% \ufffd\nEND LC_TIME\n',
            'no LC_CTYPE in sight': ctype_text(None, ELEMENT).replace(
                'escape_char /\n', 'escape_char /\ntitle "\ufffd'
                'LC_CTYPE"\n', 1),
            # Two 0xFF end the string and the section before 2.36, so the
            # escape_char in it is read at the top level, and LC_C!TYPE is
            # then LC_CTYPE: nothing but the record says the file names it.
            'an escape set inside a string': (
                'comment_char %\nescape_char /\nLC_IDENTIFICATION\n'
                'title "\ufffd\ufffd escape_char !"\nEND LC_IDENTIFICATION\n'
                'LC_C!TYPE\ncopy "i18n"\nEND LC_C!TYPE\nLC_COLLATE\n'
                + ELEMENT + '\nEND LC_COLLATE\n')}
        for name, text in texts.items():
            with self.subTest(name):
                ck.reset()
                self.assertTrue(ck.ctype_of(text) is None
                                or ck.ctype_of(text).odd is None)
                g.TEXTS_WITH_0XFF.add(text)
                try:
                    ck.reset()
                    self.assertIsNotNone(ck.ctype_of(text))
                    self.assertIn('0xFF', ck.ctype_of(text).odd)
                    self.assertTrue(ck._names_ctype(text))
                finally:
                    g.TEXTS_WITH_0XFF.discard(text)
                    ck.reset()

    def test_a_0xff_record_wins_over_an_earlier_look(self):
        """The caches are keyed by text value, and a text with the same
        value, decoded from another invalid byte, can be looked at before
        the one read from 0xFF is recorded (ctype_moved runs before the
        unchanged files are read): with no reset in between, the record
        still decides (round 9 of the 13.7 review)."""
        text = ('comment_char %\nescape_char /\nLC_IDENTIFICATION\n'
                'title "\ufffd\ufffd x"\nEND LC_IDENTIFICATION\n'
                'LC_C!TYPE\ncopy "i18n"\nEND LC_C!TYPE\n')
        ck.reset()
        self.assertIsNone(ck.ctype_of(text))
        self.assertFalse(ck._names_ctype(text))
        g.TEXTS_WITH_0XFF.add(text)
        try:
            self.assertIn('0xFF', ck.ctype_of(text).odd)
            self.assertTrue(ck._names_ctype(text))
        finally:
            g.TEXTS_WITH_0XFF.discard(text)
            ck.reset()

    def test_reading_characters(self):
        """`%` or `#` and `/` or `\\`, set by plain directive lines before
        the first section: what every file from 2.12 to 2.42 does. glibc
        takes any one character, reads a directive's argument from the next
        line when its line ends in the escape in force, and reads
        directives only at the top level (locfile.c:107-150@2.39)."""
        base = ctype_text(translit('<U02BB> "a"'))
        self.assertEqual(ck._ctype_reading(base), ('%', '/'))
        self.assertEqual(ck._ctype_reading(base.replace('escape_char /\n', '')
                                     .replace('comment_char %\n', '')),
                         ('#', '\\'))
        # escape_char above comment_char, as af_ZA writes it at 2.12: a
        # directive line ending in the escape joins nothing.
        self.assertEqual(ck._ctype_reading('escape_char /\ncomment_char %\n'
                                     + base.split('\n', 2)[2]), ('%', '/'))
        for name, text in (
                ('escape !', base.replace('escape_char /', 'escape_char !')),
                ('comment !', base.replace('comment_char %', 'comment_char !')),
                ('escaped word', base.replace('escape_char /',
                                              'escape_ch/ar !')),
                ('comment char /', base.replace('comment_char %',
                                                'comment_char /')),
                ('argument below', base.replace('escape_char /',
                                                'escape_char \\\n!')),
                ('word split', base.replace('escape_char /',
                                            'escape_\\\nchar !')),
                ('escape_char \\ alone', base.replace('escape_char /',
                                                      'escape_char \\')),
                # A valid argument, so that only the position refuses it.
                ('a valid directive after a section', base.replace(
                    'escape_char /\n', 'escape_char /\nLC_IDENTIFICATION\n'
                    'END LC_IDENTIFICATION\ncomment_char #\n', 1)),
                ('directive continued from above', base.replace(
                    'escape_char /\n', 'escape_char /\nupper /\n'
                    'comment_char #\n', 1)),
                # The syntax error on `x` drops the logical line, both
                # continued lines with it (round 5, finding 6).
                ('directive past a continued comment', base.replace(
                    'escape_char /\n', 'x \\\n% c \\\nescape_char /\n', 1)),
                ('after a section', 'comment_char %\nLC_IDENTIFICATION\n'
                 'END LC_IDENTIFICATION\nescape_char !\n'
                 + base.split('\n', 2)[2])):
            with self.subTest(name):
                self.assertIsNone(ck._ctype_reading(text))
                self.assertTrue(ck._names_ctype(text))
                self.assertIsNotNone(ck.ctype_of(text).odd)
        bang = 'comment_char %\nescape_char !\nLC_C!TYPE\nEND LC_C!TYPE\n'
        self.assertTrue(ck._names_ctype(bang))
        self.assertIsNotNone(ck.ctype_of(bang).odd)
        self.assertIn(-1, ck._quick_cps(ck.Scan(ctype_text(None, ELEMENT)
                      .replace('escape_char /', 'escape_char !'))))
        # Scan's own refusal knows escape_char only: comment_char is this
        # check's alone.
        bang = ck.Scan(ctype_text(None, ELEMENT).replace('comment_char %',
                                                          'comment_char !'))
        self.assertIsNone(bang.odd_keyword)
        self.assertIn(-1, ck._quick_cps(bang))
        odd = ctype_text(None, 'collating-element <x> from "g<U02!BB>"') \
            .replace('escape_char /', 'escape_char \\\n!')
        self.assertIn(-1, ck._quick_cps(ck.Scan(odd)))

    def test_the_escape_free_text_of_a_large_file(self):
        """Over 1 MB _bare goes by position (iso14651_t1_common, 3.4 MB,
        holds four escapes): the same text as for a small one."""
        tail = 'collating-ele/\nment <x> from "a\\"b"'
        for text in (tail, 'x' * (1 << 20) + tail):
            with self.subTest(len(text)):
                ck.reset()
                self.assertTrue(ck._bare(text).endswith(
                    'collating-element <x> from "a"b"'))

    def test_elements(self):
        scan = ck.Scan(ctype_text(None, 'collating-element <D-Z<> from '
                                  '"<U0044><U017D>"\n'
                                  'collating-element <x> from "a/"<U0042>"'))
        self.assertEqual(ck.elements_of(scan),
                         {'<D-Z<>': (('u', 0x44), ('u', 0x17D)),
                          '<x>': (('a', 'a'), ('a', '"'), ('u', 0x42))})
        with self.assertRaises(ck.Unsettled):
            ck.reset()
            ck.elements_of(ck.Scan(ctype_text(None, 'collating-element <x> '
                                              'from "a"\ncollating-element '
                                              '<x> from "b"')))
        # The keyword built with the escape (round 2 of the reviewer).
        for line in ('collating-ele/ment <x> from "g<U02BB>"',
                     'collating-ele/\nment <x> from "g<U02BB>"'):
            with self.subTest(line):
                ck.reset()
                sc = ck.Scan(ctype_text('copy "i18n"', line))
                side = ck.Side('t', {'D'}, {'D': sc.text}, {}, {})
                self.assertTrue(ck._defines_elements(side, 'D'))
                self.assertIn(-1, ck._quick_cps(sc))
                with self.assertRaises(ck.Unsettled):
                    ck.elements_of(sc)
        # One block, read with two escape characters: two answers.
        ck.reset()
        body = 'collating-element <x> from "a/"<U02BB>"'
        slash = ctype_text(None, body)
        back = slash.replace('escape_char /', 'escape_char \\')
        self.assertEqual(ck.elements_of(ck.Scan(slash)),
                         {'<x>': (('a', 'a'), ('a', '"'), ('u', 0x2BB))})
        with self.assertRaises(ck.Unsettled):
            ck.elements_of(ck.Scan(back))
        for line in ('collating-element <x> from "a" junk',
                     'collating-element <x> from "ʻ"',
                     'collating-element <x> from /\n"a"'):
            with self.subTest(line), self.assertRaises(ck.Unsettled):
                ck.reset()
                ck.elements_of(ck.Scan(ctype_text(None, line)))


class TranslitCompare(unittest.TestCase):
    """compare()'s point 6 over two Sides: when it compares, what it lists,
    and what it does not settle."""

    SUP = 'SUPPORTED-LOCALES=\\\nM/TEST \\\n'

    def run_compare(self, old_texts, new_texts, old_cm=None, new_cm=None,
                    listed=(), ask_utf8=None):
        old_cm = old_cm or _ascii_charmap()
        new_cm = new_cm or old_cm
        old, new = sides(dict(I18N, **old_texts), dict(I18N, **new_texts),
                         {'TEST': old_cm}, {'TEST': new_cm})
        if isinstance(ask_utf8, tuple):
            old.ask_utf8, new.ask_utf8 = ask_utf8
        else:
            old.ask_utf8 = new.ask_utf8 = ask_utf8
        b = ck.supported_builds(self.SUP, 't')
        return ck.compare(old, new, b, b, lambda: set(listed))

    BASE = {'D': ctype_text('copy "i18n"', ELEMENT),
            'M': ctype_text('copy "i18n"', 'copy "D"')}

    def test_a_charmap_change_alone_is_found(self):
        """H1, measured as M2: the charmap gains U+2018, which D's
        transliteration names and no LC_COLLATE does. The character set
        search finds nothing; point 6 does."""
        files = {'D': ctype_text('copy "i18n"\n'
                                 + translit('<U02BB> "<U2018>";"<U0027>"'),
                                 ELEMENT),
                 'M': ctype_text('copy "i18n"', 'copy "D"')}
        r = self.run_compare(files, files,
                             new_cm=_ascii_charmap('<U2018> /x91'))
        self.assertEqual(r.found, [])
        self.assertEqual([(f.source, f.changed, f.chars, f.via, f.reason)
                          for f in r.translit.found],
                         [('M', 1, {0x2BB}, ['D'], None)])
        self.assertEqual(ck.listed_sources(r), ['M'])

    def test_a_change_in_the_source_s_lc_ctype_is_found(self):
        """H2: neither D nor anything its LC_CTYPE reaches changes; M's
        LC_CTYPE, read before its LC_COLLATE, gains the entry."""
        new = dict(self.BASE, M=ctype_text('copy "i18n"\n' + translit(GRAVE),
                                           'copy "D"'))
        r = self.run_compare(self.BASE, new)
        self.assertEqual([(f.source, f.changed) for f in r.translit.found],
                         [('M', 1)])

    def test_a_change_in_an_include_is_found(self):
        new = dict(self.BASE)
        new['translit_neutral'] = ctype_text(translit(APOS))
        r = self.run_compare(self.BASE, new)
        self.assertEqual([(f.source, f.changed) for f in r.translit.found],
                         [('M', 1)])

    def test_d_gaining_lc_ctype_is_found(self):
        old = dict(self.BASE, D=ctype_text(None, ELEMENT))
        new = dict(self.BASE, D=ctype_text('copy "i18n"\n' + translit(APOS),
                                           ELEMENT))
        r = self.run_compare(old, new)
        self.assertEqual([(f.source, f.changed) for f in r.translit.found],
                         [('M', 1)])

    def test_nothing_that_changed_is_not_compared(self):
        """With no charmap changed, no LC_CTYPE read differently: no point
        6 at all. With a charmap changed and nothing the run reads, the
        build is counted as not compared, never as compared and equal."""
        r = self.run_compare(self.BASE, dict(self.BASE))
        self.assertIsNone(r.translit)
        r = self.run_compare(self.BASE, dict(self.BASE),
                             new_cm=_ascii_charmap('<U00E9> /xe9'))
        self.assertIsNotNone(r.translit)
        self.assertEqual((r.translit.elements, r.translit.unmoved), (1, 0))
        unrelated = dict(self.BASE, other=ctype_text('upper <U0042>'))
        r = self.run_compare(dict(self.BASE, other=ctype_text('upper <U0041>')),
                             unrelated)
        self.assertEqual((r.translit.elements, r.translit.unmoved,
                          r.translit.found), (0, 1, []))

    def test_a_raw_character_in_a_from_string_is_listed(self):
        """The quick search takes a raw non-ASCII character as the code
        point it is, so the build is planned and the strict read refuses
        the string; missed there, the build would never be compared."""
        raw = ELEMENT.replace('g<U02BB>', 'gʻ')
        old = dict(self.BASE, D=ctype_text('copy "i18n"', raw))
        new = dict(old, M=ctype_text('copy "i18n"\n' + translit(GRAVE),
                                     'copy "D"'))
        r = self.run_compare(old, new)
        self.assertEqual([(f.source, f.changed) for f in r.translit.found],
                         [('M', 0)])
        self.assertIn('raw non-ASCII', r.translit.found[0].reason)
        self.assertEqual(ck.listed_sources(r), ['M'])

    def test_a_raw_character_is_read_only_where_localedef_reads_utf8(self):
        """Before UTF8_READER (2.36) a raw head is bytes that make glibc
        swallow the next line: the build is compared only where git says
        the tag holds the commit, and listed where it does not or cannot
        say. A raw character in a comment is skipped by every glibc."""
        def files(entry, comment=''):
            return dict(self.BASE, D=ctype_text(
                'copy "i18n"\n' + translit('é "e"' + comment, entry),
                ELEMENT))
        old, new = files(APOS), files(GRAVE)
        r = self.run_compare(old, new, ask_utf8=lambda: True)
        self.assertEqual([(f.source, f.changed, f.reason)
                          for f in r.translit.found], [('M', 1, None)])
        for ask, why in ((lambda: False, 'does not hold it'),
                         (lambda: None, 'could not tell'),
                         (None, 'could not tell')):
            with self.subTest(why):
                r = self.run_compare(old, new, ask_utf8=ask)
                self.assertEqual([(f.source, f.changed)
                                  for f in r.translit.found], [('M', 0)])
                self.assertIn(why, r.translit.found[0].reason)
                self.assertIn('raw non-ASCII', r.translit.found[0].reason)
        def commented(entry):
            return dict(self.BASE, D=ctype_text(
                'copy "i18n"\n' + translit(entry + ' % ‘ -> x'), ELEMENT))
        r = self.run_compare(commented(APOS), commented(GRAVE))
        self.assertEqual([(f.source, f.changed, f.reason)
                          for f in r.translit.found], [('M', 1, None)])

    def test_an_unchanged_raw_table_is_compared_across_the_utf8_reader(self):
        """The same raw head reads as other code points on either side of
        UTF8_READER, so a file that did not change is compared there: with
        nothing else changed (compare runs point 6 for it), and with point 6
        running for another reason (the shortcut does not count it as
        unchanged). Two sides that read alike, or that git places neither
        way, leave it alone."""
        raw = dict(self.BASE, translit_neutral=ctype_text(translit(
            'é "e"', '<U02BB> "<U2018>"')))
        yes, no, unknown = (lambda: True), (lambda: False), (lambda: None)
        moved = dict(raw, other=ctype_text('upper <U0042>'))
        for name, new in (('nothing else changed', raw),
                          ('another file moved', moved)):
            old = dict(raw, other=ctype_text('upper <U0041>')) \
                if new is moved else raw
            for asks, why in (((no, yes), 'does not hold it'),
                              ((unknown, yes), 'could not tell')):
                with self.subTest(name, why=why):
                    r = self.run_compare(old, new, ask_utf8=asks)
                    self.assertEqual([(f.source, f.changed)
                                      for f in r.translit.found], [('M', 0)])
                    self.assertIn(why, r.translit.found[0].reason)
            for asks in ((yes, yes), (unknown, unknown)):
                with self.subTest(name, asks=asks):
                    r = self.run_compare(old, new, ask_utf8=asks)
                    if new is raw:
                        self.assertIsNone(r.translit)
                    else:
                        self.assertEqual((r.translit.found,
                                          r.translit.unmoved), ([], 1))

    def test_an_unchanged_raw_from_string_is_compared_across_the_reader(self):
        """A raw character in a from string is bytes before UTF8_READER and
        a code point from it, so an unchanged defining file is compared
        where the readers differ: on the element line, or on the line a
        continued from string goes on to. In a comment it is skipped."""
        yes, no = (lambda: True), (lambda: False)
        for name, el in (
                ('on the line', ELEMENT.replace('g<U02BB>', 'gʻ')),
                ('on a continued line', ELEMENT.replace(
                    'from "g<U02BB>"', 'from /\n"gʻ"')),
                # The string goes on across the join; get_string knows no
                # comment (round 8 of the review).
                ('continued past a %', ELEMENT.replace(
                    'from "g<U02BB>"', 'from "g/\n%ʻ"')),
                # A line joined to the one above is no comment line, so
                # the join goes on past it (round 9).
                ('three lines, the middle one opening with %',
                 ELEMENT.replace('from "g<U02BB>"', 'from "g/\n%/\n%ʻ"'))):
            files = dict(self.BASE, D=ctype_text('copy "i18n"', el))
            with self.subTest(name):
                r = self.run_compare(files, files, ask_utf8=(no, yes))
                self.assertEqual([(f.source, f.changed)
                                  for f in r.translit.found], [('M', 0)])
                self.assertIsNone(self.run_compare(
                    files, files, ask_utf8=(yes, yes)).translit)
        files = dict(self.BASE, D=ctype_text('copy "i18n"', ELEMENT.replace(
            'from "g<U02BB>"', 'from "g<U02BB>" % ʻ')))
        r = self.run_compare(files, files, ask_utf8=(no, yes))
        self.assertEqual((r.translit.found, r.translit.unmoved), ([], 1))

    def test_a_byte_that_is_not_utf8_above_counts_from_2_36(self):
        """From 2.36 a byte that is not UTF-8 ends a string as illegal, and
        LC_IDENTIFICATION's string fields go on reading the rest of the line
        as statements; before, it is a byte of the string, and only 0xFF
        (recorded apart) changes anything. So a build whose source has one
        above LC_CTYPE is listed where a tag reads UTF-8 or git cannot
        say, and compared where neither does; unchanged, it is compared
        where the readers differ."""
        yes, no, unknown = (lambda: True), (lambda: False), (lambda: None)
        def m(entry):
            return ('comment_char %\nescape_char /\nLC_IDENTIFICATION\n'
                    'title "caf\ufffd"\nEND LC_IDENTIFICATION\n'
                    + 'LC_CTYPE\ncopy "i18n"\n' + translit(entry)
                    + '\nEND LC_CTYPE\nLC_COLLATE\ncopy "D"\nEND LC_COLLATE\n')
        old, new = dict(self.BASE, M=m(GRAVE)), dict(self.BASE, M=m(APOS))
        r = self.run_compare(old, new, ask_utf8=(no, no))
        self.assertEqual([(f.source, f.changed, f.reason)
                          for f in r.translit.found], [('M', 1, None)])
        for asks in ((yes, yes), (unknown, unknown), (no, yes)):
            with self.subTest(asks=asks):
                r = self.run_compare(old, new, ask_utf8=asks)
                self.assertEqual([(f.source, f.changed)
                                  for f in r.translit.found], [('M', 0)])
                self.assertIn('not UTF-8', r.translit.found[0].reason)
        r = self.run_compare(old, old, ask_utf8=(no, yes))
        self.assertEqual([(f.source, f.changed)
                          for f in r.translit.found], [('M', 0)])
        self.assertIsNone(self.run_compare(old, old,
                                           ask_utf8=(no, no)).translit)

    def test_a_0xff_byte_anywhere_in_the_run_lists_every_build(self):
        """One gate for the byte read_blobs_at records: with one in any
        locale file or charmap the run holds, every build is listed, so no
        path of the plan can pass over it -- an unrelated file; the build's
        own source, whose two 0xFF let an escape_char inside a string act
        and spell `<U02!BB>` (round 10 of the 13.7 review); a charmap read
        only once the plan is made. Not recorded, the same run compares."""
        new_neutral = ctype_text(translit('<U02BB> "<U2018>"', APOS))
        zz = ctype_text('upper <U0041>') + '% \ufffd\n'
        esc = ('comment_char %\nescape_char /\nLC_IDENTIFICATION\n'
               'title "\ufffd\ufffd escape_char !"\nEND LC_IDENTIFICATION\n'
               'LC_CTYPE\ncopy "i18n"\nEND LC_CTYPE\nLC_COLLATE\n'
               + ELEMENT.replace('<U02BB>', '<U02!BB>') + '\nEND LC_COLLATE\n')
        for name, old, recorded in (
                ('an unrelated file', dict(self.BASE, zz=zz), zz),
                ('the source itself', dict(self.BASE, M=esc), esc)):
            new = dict(old, translit_neutral=new_neutral)
            with self.subTest(name):
                ck.reset()
                self.assertNotIn('0xFF', str(self.run_compare(old, new)))
                g.TEXTS_WITH_0XFF.add(recorded)
                try:
                    r = self.run_compare(old, new)
                    self.assertEqual([(f.source, f.changed)
                                      for f in r.translit.found], [('M', 0)])
                    self.assertIn('0xFF', r.translit.found[0].reason)
                finally:
                    g.TEXTS_WITH_0XFF.discard(recorded)
                    ck.reset()
        # A charmap the Sides do not hold until the plan asks for it.
        cm = _ascii_charmap()
        old, new = sides(dict(I18N, **self.BASE),
                         dict(I18N, **self.BASE, translit_neutral=new_neutral),
                         {'TEST': cm}, {'TEST': cm})
        for side in (old, new):
            del side.charmap_texts['TEST']

        def read_more(charmaps, locales=False):
            # Only what the plan asks for: a read of locale files alone
            # brings no charmap.
            for side in (old, new):
                if 'TEST' in charmaps.get(side.tag, ()):
                    side.charmap_texts['TEST'] = cm
        b = ck.supported_builds(self.SUP, 't')
        g.TEXTS_WITH_0XFF.add(cm)
        try:
            ck.reset()
            r = ck.compare(old, new, b, b, lambda: set(), read_more)
            self.assertEqual([(f.source, f.changed)
                              for f in r.translit.found], [('M', 0)])
            self.assertIn('TEST at t1', r.translit.found[0].reason)
        finally:
            g.TEXTS_WITH_0XFF.discard(cm)
            ck.reset()

    def test_a_raw_table_is_read_with_its_own_characters(self):
        """One LC_CTYPE section, two files: under escape `/` the raw é is
        in a comment, under the default `\\` the backslash escapes the
        quote and é is in the string. The first look must not answer for
        the other file (round 10 of the 13.7 review)."""
        section = ('LC_CTYPE\ntranslit_start\n<U02BB> "x\\" % é"\n'
                   'translit_end\nEND LC_CTYPE\n')
        f = dict(I18N)
        for n, head in (('1', 'comment_char %\nescape_char /\n'),
                        ('2', 'comment_char %\n')):
            f['T' + n] = head + section
            f['D' + n] = ctype_text('copy "i18n"\n' + translit(
                f'include "T{n}";""'), ELEMENT.replace('<gtc>', f'<gt{n}>'))
            f['M' + n] = ctype_text('copy "i18n"', f'copy "D{n}"')
        sup = 'SUPPORTED-LOCALES=\\\nM1/TEST \\\nM2/TEST \\\n'
        cm = _ascii_charmap('<U00E9> /xe9')
        old, new = sides(f, dict(f), {'TEST': cm}, {'TEST': cm})
        old.ask_utf8, new.ask_utf8 = (lambda: False), (lambda: True)
        b = ck.supported_builds(sup, 't')
        ck.reset()
        r = ck.compare(old, new, b, b, lambda: set())
        self.assertEqual([(x.source, x.changed) for x in r.translit.found],
                         [('M2', 0)])
        self.assertIn('raw non-ASCII', r.translit.found[0].reason)
        self.assertEqual(r.translit.unmoved, 1)

    def test_which_readers_differ(self):
        """Across UTF8_READER, or where git places one side and not the
        other; two sides git places neither way are one reader (the
        repository does not hold the commit; a shallow clone is refused
        before this is asked)."""
        def side(ans):
            s = ck.Side('t', set(), {}, {}, {})
            s.ask_utf8 = lambda: ans
            return s
        for a, b, want in ((True, True, False), (False, False, False),
                           (None, None, False), (True, False, True),
                           (False, True, True), (True, None, True),
                           (None, False, True)):
            with self.subTest(a=a, b=b):
                self.assertIs(ck._readers_differ(side(a), side(b)), want)

    def test_what_cannot_be_settled_is_listed(self):
        bad = dict(self.BASE, D=ctype_text(
            'copy "i18n"\n' + translit('translit_ignore <U0300>'), ELEMENT))
        r = self.run_compare(bad, dict(bad, M=ctype_text(
            'copy "i18n"\n' + translit(GRAVE), 'copy "D"')))
        self.assertEqual(len(r.translit.found), 1)
        self.assertIn('translit_ignore', r.translit.found[0].reason)
        self.assertEqual(ck.listed_sources(r), ['M'])
        # Not counted among the compared, which the line above the `!!`
        # would otherwise call "none is built differently".
        self.assertEqual((r.translit.elements, r.translit.builds), (0, 0))

    def test_a_continued_element_line_is_not_settled(self):
        """The from string is on the next line, which glibc joins: the
        quick search sees no code point on the line it reads, and must
        still send the file to the strict reader, which refuses it."""
        d = ctype_text('copy "i18n"', 'collating-element <x> from /\n'
                       '"g<U02BB>"\norder_start forward\n<x>\norder_end')
        old = dict(self.BASE, D=d)
        new = dict(old, M=ctype_text('copy "i18n"\n' + translit(GRAVE),
                                     'copy "D"'))
        r = self.run_compare(old, new)
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertIn('continued', r.translit.found[0].reason)

    def test_elements_that_differ_between_the_tags_are_listed(self):
        """Round 2 of the reviewer, on 2.27..2.28: fi_FI gains its
        elements, so they are not compared, and the line said "no compared
        locale build builds a collating element out of a transliteration".
        Now each such build is listed with that reason: counted as listed
        anyway when step 3 lists it, a `!!` when it does not."""
        new = dict(self.BASE, D=ctype_text(
            'copy "i18n"', ELEMENT.replace(
                'order_start', 'collating-element <y> from "h<U02BB>"\n'
                'order_start')))
        r = self.run_compare(self.BASE, new, listed=['M'])
        self.assertEqual((r.translit.found, r.translit_skipped,
                          r.translit.elements), ([], 1, 0))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f.print_translit_report(r, 't2')
        self.assertEqual(flat(buf.getvalue()), flat(
            'Transliteration (LC_CTYPE): the 1 locale build that may build a '
            'collating element out of a transliteration is listed already, '
            'so none was compared.'))
        r = self.run_compare(self.BASE, new)
        self.assertIn('differ between the tags', r.translit.found[0].reason)

    def test_a_build_step_3_lists_anyway_is_counted(self):
        """Counted, and its changed elements still in the printed total:
        the line read "none is built differently" over it (reviewer of PR
        C)."""
        new = dict(self.BASE, M=ctype_text('copy "i18n"\n' + translit(GRAVE),
                                           'copy "D"'))
        r = self.run_compare(self.BASE, new, listed=['M'])
        self.assertEqual((r.translit.found, r.translit_skipped), ([], 1))
        self.assertEqual(ck.listed_sources(r), [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f.print_translit_report(r, 't2')
        self.assertIn('1 is built differently at t2.', flat(buf.getvalue()))

    def test_a_second_lc_collate_in_the_defining_file_is_refused(self):
        """glibc reads every LC_COLLATE section of a file (H11); an element
        defined in a second one is not in elements_of's block."""
        d = (ctype_text('copy "i18n"', ELEMENT)
             + 'LC_COLLATE\ncollating-element <y> from "h<U02BB>"\n'
               'END LC_COLLATE\n')
        old = dict(self.BASE, D=d)
        new = dict(old, M=ctype_text('copy "i18n"\n' + translit(GRAVE),
                                     'copy "D"'))
        r = self.run_compare(old, new)
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertIn('second LC_COLLATE', r.translit.found[0].reason)

    def test_elements_only_in_a_second_section_are_refused(self):
        """The first block defines none, the second one does: the file
        is still planned, and refused."""
        d = (ctype_text('copy "i18n"', 'order_start forward\n<U0041>\n'
                        'order_end')
             + 'LC_COLLATE\ncollating-element <y> from "h<U02BB>"\n'
               'END LC_COLLATE\n')
        old = dict(self.BASE, D=d)
        new = dict(old, M=ctype_text('copy "i18n"\n' + translit(GRAVE),
                                     'copy "D"'))
        r = self.run_compare(old, new)
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertIsNotNone(r.translit.found[0].reason)

    def test_what_cannot_be_read_is_refused_before_the_shortcut(self):
        """Round 3 of the reviewer: M's LC_COLLATE keyword after blanks
        (glibc reads it), D includes Z2, Z2 changes, no charmap change.
        The shortcut "nothing it reads changed" ran first and said so."""
        m = ctype_text('copy "i18n"', 'copy "D"').replace(
            '\nLC_COLLATE', '\n  LC_COLLATE')
        old = dict(self.BASE, M=m, Z2=ctype_text(translit(GRAVE)),
                   D=ctype_text('copy "i18n"\n' + translit('include "Z2";""'),
                                ELEMENT))
        new = dict(old, Z2=ctype_text(translit(APOS)))
        r = self.run_compare(old, new)
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertIsNotNone(r.translit.found[0].reason)
        self.assertEqual(r.translit.unmoved, 0)

    def test_an_include_read_with_another_escape_stops_the_shortcut(self):
        """Z, identical at both tags, is read with `!` and includes Z2
        as `in!clude`, which the search for names cannot see; Z2 changes.
        Z is not read as it looks, so nothing is taken as unchanged."""
        z = ctype_text(translit('in!clude "Z2";""')).replace(
            'escape_char /', 'escape_char !')
        old = dict(self.BASE, Z=z, Z2=ctype_text(translit(GRAVE)),
                   D=ctype_text('copy "i18n"\n' + translit('include "Z";""'),
                                ELEMENT))
        new = dict(old, Z2=ctype_text(translit(APOS)))
        r = self.run_compare(old, new)
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertEqual(r.translit.unmoved, 0)

    def test_a_source_with_an_escaped_head_is_refused(self):
        """`LC_C/TYPE` before LC_COLLATE: glibc reads M's LC_CTYPE
        first; the run was built without it."""
        m = ctype_text('copy "i18n"\n' + translit(GRAVE), 'copy "D"').replace(
            'LC_CTYPE', 'LC_C/TYPE')
        r = self.run_compare(dict(self.BASE, M=m), dict(self.BASE, M=m),
                             new_cm=_ascii_charmap('<U00E9> /xe9'))
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertIsNotNone(r.translit.found[0].reason)

    def test_a_copy_spelled_without_a_blank_is_followed(self):
        """`copy"i18n"` is a copy to glibc: a change in what it reaches
        is compared."""
        old = dict(self.BASE, D=ctype_text('copy"i18n"', ELEMENT))
        new = dict(old, translit_neutral=ctype_text(translit(APOS)))
        r = self.run_compare(old, new)
        self.assertEqual([(f.source, f.changed) for f in r.translit.found],
                         [('M', 1)])

    def test_a_table_read_with_another_escape_is_refused(self):
        """translit_neutral, reached through i18n, is read with `!` as its
        escape: refused, never compared as if `/` were."""
        odd = ctype_text(translit(APOS)).replace('escape_char /',
                                                 'escape_char !')
        new = dict(self.BASE, translit_neutral=odd)
        r = self.run_compare(self.BASE, new)
        self.assertEqual([f.source for f in r.translit.found], ['M'])
        self.assertIsNotNone(r.translit.found[0].reason)


class CtypeMoved(unittest.TestCase):
    """ctype_moved decides that nothing needs reading: every way an
    LC_CTYPE can be read differently must say yes."""

    def moved(self, old, new, old_present=None, new_present=None):
        o = ck.Side('t1', set(old_present or old), dict(old), {}, {})
        n = ck.Side('t2', set(new_present or new), dict(new), {}, {})
        return ck.ctype_moved(o, n)

    def test_each_way(self):
        base = ctype_text('copy "i18n"', 'copy "x"')
        for name, new in (
                ('LC_CTYPE', ctype_text('copy "i18n"\n' + translit(GRAVE),
                                        'copy "x"')),
                ('order', ctype_text('copy "i18n"', 'copy "x"',
                                     ctype_first=False)),
                ('comment char', base.replace('comment_char %',
                                              'comment_char #')),
                ('repertoiremap', 'repertoiremap x\n' + base)):
            with self.subTest(name):
                self.assertTrue(self.moved({'a': base}, {'a': new}))
        self.assertTrue(self.moved({'a': base}, {'a': base},
                                   new_present={'a', 'b'}))
        # An escaped head: its section is compared as the whole text.
        esc = base.replace('LC_CTYPE', 'LC_C/TYPE')
        self.assertTrue(self.moved({'a': esc}, {'a': esc.replace(
            'copy "i18n"', 'copy "i18n"\n' + translit(GRAVE))}))
        self.assertFalse(self.moved({'a': base}, {'a': ctype_text(
            'copy "i18n"', 'copy "y"')}))
        # Two blobs whose bytes differ only where they decode as U+FFFD:
        # equal texts, two objects (round 5, finding 7).
        a = base + '% \ufffd\n'
        b = ''.join([base, '% \ufffd\n'])
        self.assertIsNot(a, b)
        self.assertTrue(self.moved({'a': a}, {'a': b}))


class TranslitReportLines(unittest.TestCase):
    """Lines print_translit_report writes."""

    def lines(self, translit, skipped=0, builds=2):
        r = ck.Report(None, builds, ['TEST'], [], {}, [], [], [], [],
                      translit, skipped)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f.print_translit_report(r, 't2')
        return buf.getvalue()

    def test_each_line(self):
        self.assertIn('24 collating elements of 3 locale builds depend on a '
                      'transliteration; none is built differently at t2.',
                      flat(self.lines(ck.TranslitReport(24, 3, 0, []))))
        self.assertIn('no character set and no locale file\'s LC_CTYPE '
                      'changed', flat(self.lines(None)))
        found = ck.Translit('uz_XX', 'uz_XX', ('TEST', 'TEST'), 4, {0x2BB},
                            ['uz_XX'], None)
        out = self.lines(ck.TranslitReport(4, 1, 0, [found]))
        self.assertIn('4 are built differently at t2.', flat(out))
        self.assertIn('  uz_XX (TEST): 4 elements, defined in uz_XX', out)
        self.assertIn('ʻ (U+02BB)', out)
        why = ck.Translit('fi_XX', 'fi_XX', ('TEST', 'TEST'), 0, set(), [],
                          'a reason')
        out = self.lines(ck.TranslitReport(0, 0, 0, [why]))
        self.assertTrue(any(b.startswith('!! 1 locale build(s) could not be '
                                         'compared for the collating '
                                         'elements') and 'fi_XX (TEST): a '
                            'reason' in b for b in _bang_blocks(out)), out)
        self.assertEqual(self.lines(None, builds=0), '')



def _cm_file(root, entry, text):
    """A charmap file as a machine installs it: plain, .gz or .bz2."""
    data = text.encode('utf-8')
    if entry.endswith('.gz'):
        data = gzip.compress(data, mtime=0)
    elif entry.endswith('.bz2'):
        data = bz2.compress(data)
    with open(os.path.join(root, entry), 'wb') as fh:
        fh.write(data)


def _named(code_set_name, *body):
    return charmap(*(body or ('<U0041> /x41',)),
                   prolog=(f'<code_set_name> {code_set_name}',) + _PROLOG[1:])


class MachineCharmapsDir(unittest.TestCase):
    """A machine's /usr/share/i18n/charmaps/ as node_charmaps reads it: one
    file per charmap, and the codeset glibc stores for each, or the folder
    is refused whole (the table of codesets has to hold every file)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='pg-glibc-cmdir-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def cms(self, files):
        for entry, text in files.items():
            _cm_file(self.root, entry, text)
        return nc.CharmapsDir(self.root)

    def test_plain_gzip_and_bzip2_are_read_uncompressed(self):
        text = _named('A')
        c = self.cms({'A': text, 'B.gz': text, 'C.bz2': text})
        self.assertEqual(sorted(c.files), ['A', 'B', 'C'])
        for name in 'ABC':
            self.assertEqual(c.raw(name), text.encode())
        # git's id of the uncompressed bytes, whatever the file's form.
        self.assertEqual({c.oid(n) for n in 'ABC'}, {c.oid('A')})
        self.assertEqual(c.text('B'), text)
        self.assertIsNone(c.text('missing'))

    def test_what_is_not_one_charmap_file_is_refused(self):
        os.mkdir(os.path.join(self.root, 'sub'))
        with self.assertRaisesRegex(nc.Unusable, 'sub is not a file'):
            nc.CharmapsDir(self.root)
        os.rmdir(os.path.join(self.root, 'sub'))
        _cm_file(self.root, 'X', _named('X'))
        os.symlink('X', os.path.join(self.root, 'Y'))
        with self.assertRaisesRegex(nc.Unusable, 'Y is not a file'):
            nc.CharmapsDir(self.root)
        os.remove(os.path.join(self.root, 'Y'))
        _cm_file(self.root, 'X.gz', _named('X'))
        with self.assertRaisesRegex(nc.Unusable, 'X and X.gz are one charmap'):
            nc.CharmapsDir(self.root)
        os.remove(os.path.join(self.root, 'X.gz'))
        _cm_file(self.root, 'a b', _named('X'))
        with self.assertRaisesRegex(nc.Unusable, 'not a charmap name'):
            nc.CharmapsDir(self.root)

    def test_an_empty_or_missing_folder_is_refused(self):
        with self.assertRaisesRegex(nc.Unusable, 'holds no charmap'):
            nc.CharmapsDir(self.root)
        with self.assertRaisesRegex(nc.Unusable, 'cannot be listed'):
            nc.CharmapsDir(os.path.join(self.root, 'none'))

    def test_a_file_that_does_not_uncompress_is_refused(self):
        with open(os.path.join(self.root, 'Z.gz'), 'wb') as fh:
            fh.write(b'not gzip at all')
        c = nc.CharmapsDir(self.root)
        with self.assertRaisesRegex(nc.Unusable, 'cannot be uncompressed'):
            c.raw('Z')
        with self.assertRaisesRegex(nc.Unusable, 'cannot be uncompressed'):
            c.codesets()

    def test_the_codeset_glibc_stores_for_each_file(self):
        """A prolog read whole gives its <code_set_name>, or the file's own
        name when it has none (charmap.c:200-203@2.28). One read loosely --
        an invalid definition glibc skips, no CHARMAP line -- could give any
        name a <code_set_name> line spells, or the file's."""
        c = self.cms({
            'UTF-8.gz': _named('UTF-8'),
            'NONAME': charmap('<U0041> /x41', prolog=_PROLOG[1:]),
            'ODD.gz': '<code_set_name> ODDNAME\n<comment> %\n'
                      + charmap('<U0041> /x41', prolog=_PROLOG[1:]),
            'NOLINE': '<U0041> /x41\n<code_set_name> LATE\n',
        })
        certain, loose = c.codesets()
        self.assertEqual(dict(certain), {'UTF-8': ['UTF-8'],
                                         'NONAME': ['NONAME']})
        # A superset (_loose_names): what glibc reads and the file's name
        # are in it.
        self.assertEqual(sorted(loose), ['NOLINE', 'ODD'])
        self.assertLessEqual({'ODDNAME', 'ODD'}, loose['ODD'])
        self.assertLessEqual({'LATE', 'NOLINE'}, loose['NOLINE'])
        self.assertNotIn('UTF-8', loose['ODD'] | loose['NOLINE'])

    def test_a_prolog_longer_than_the_first_read_is_read_whole(self):
        """Only the start of a .gz file is uncompressed when its CHARMAP
        line is there; a longer prolog is read whole, not cut."""
        # `#` is the comment character until <comment_char> changes it
        # (lr_create, linereader.c:79-80@2.39).
        long = '\n'.join(['# a comment line of some length'] * 2000)
        c = self.cms({'L.gz': long + '\n' + _named('LONGNAME')})
        self.assertEqual(dict(c.codesets()[0]), {'LONGNAME': ['L']})


class MachineBuilds(unittest.TestCase):
    """measured_builds: the builds of a machine, one per source and
    charmap, traced from each name `locale -a` listed by the codeset
    measured under it; a name it cannot trace is named, never dropped."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='pg-glibc-builds-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cmroot = os.path.join(self.root, 'cm')
        os.mkdir(self.cmroot)

    def cms(self, files):
        for entry, text in files.items():
            _cm_file(self.cmroot, entry, text)
        return nc.CharmapsDir(self.cmroot)

    @staticmethod
    def built(path, c, aliases, tag_ids=None):
        """measured_builds with every charmap its tag's own, unless
        `tag_ids` says otherwise."""
        if tag_ids is None:
            tag_ids = {n: c.oid(n) for n in c.files}
        return nc.measured_builds(path, 'b-1', c, aliases, 't', tag_ids)

    def measurement(self, locales, build='b-1', raw=None):
        path = os.path.join(self.root, 'm.json')
        with open(path, 'w', encoding='ascii') as fh:
            fh.write(raw if raw is not None else json.dumps(
                {'glibc_build': build,
                 'locales': {n: ({'codeset': c} if c else {})
                             for n, c in locales.items()}}))
        return path

    def test_names_become_one_build_per_source_and_charmap(self):
        c = self.cms({'UTF-8.gz': _named('UTF-8'),
                      'ISO-8859-1.gz': _named('ISO-8859-1')})
        m = self.built(self.measurement({
            'de_DE': 'ISO-8859-1', 'de_DE.iso88591': 'ISO-8859-1',
            'deutsch': 'ISO-8859-1', 'de_DE.utf8': 'UTF-8',
            'C': 'ANSI_X3.4-1968', 'POSIX': 'ANSI_X3.4-1968'}), c, {'deutsch': 'de_DE.ISO-8859-1'})
        self.assertEqual(m.builds, [ck.Build('de_DE', 'de_DE', 'ISO-8859-1'),
                                    ck.Build('de_DE.utf8', 'de_DE', 'UTF-8')])
        self.assertEqual((m.unmapped, m.names), ([], 6))

    def test_a_build_is_named_after_its_own_locale_not_an_alias(self):
        """With no name spelled as its source alone, the shortest of those
        spelled after it, not a shorter alias (french)."""
        c = self.cms({'ISO-8859-1': _named('ISO-8859-1')})
        m = self.built(self.measurement({
            'fr_FR.iso88591': 'ISO-8859-1', 'french': 'ISO-8859-1'}), c, {'french': 'fr_FR.ISO-8859-1'})
        self.assertEqual(m.builds,
                         [ck.Build('fr_FR.iso88591', 'fr_FR', 'ISO-8859-1')])

    def test_an_alias_is_traced_to_the_locale_it_names(self):
        """bokmal is nb_NO.ISO-8859-1 in intl/locale.alias; locale_source
        alone would make it a source named bokmal."""
        c = self.cms({'ISO-8859-1': _named('ISO-8859-1')})
        m = self.built(self.measurement({'bokmal': 'ISO-8859-1'}), c,
                       {'bokmal': 'nb_NO.ISO-8859-1'})
        self.assertEqual(m.builds, [ck.Build('bokmal', 'nb_NO', 'ISO-8859-1')])

    def test_a_name_traced_to_no_single_charmap_is_named(self):
        c = self.cms({'A': _named('X'), 'B': _named('X'), 'Z': _named('Z'),
                      'ODD': '<code_set_name> Y\n<comment> %\n'
                             + charmap('<U0041> /x41', prolog=_PROLOG[1:])})
        m = self.built(self.measurement({
            'n1': None, 'n2': 'Q', 'n3': 'X', 'n4': 'Y', 'n5': 'ODD',
            'n6': 'Z'}), c, {})
        self.assertEqual(m.unmapped, [
            ('n1', 'its encoding was not measured'),
            ('n2', 'no charmap on the machine declares its encoding Q'),
            ('n3', 'its encoding X is declared by A, B'),
            ('n4', 'its encoding Y is declared by ODD'),
            ('n5', 'its encoding ODD is declared by ODD')])
        self.assertEqual(m.builds, [ck.Build('n6', 'n6', 'Z')])

    def test_a_loose_name_is_read_as_glibc_reads_it(self):
        """A second charmap glibc reads as declaring FOO-BAR, spelled so
        that only glibc's reader makes FOO-BAR of it: an escape inside the
        word or the keyword, a word joined across lines, a stop character
        after it, the argument on the next line, a CHARMAP line swallowed by
        the line above it. With that file its tag's own, the build is not
        mapped while that file could have built it (false-negative-reviewer,
        round 2 of PR D)."""
        head = '<comment_char> %\n<escape_char> /\n'
        body = 'CHARMAP\n<U0041> /x41\nEND CHARMAP\n'
        for spelling in ('<code_set_name> FOO/-BAR\n',
                         '<code_set_name> FOO-/\nBAR\n',
                         '<code_set_name> FOO-BAR,X\n',
                         '<code_set_name> FOO-BAR;X\n',
                         '<code_set_n/ame> FOO-BAR\n',
                         '</code_set_name> FOO-BAR\n',
                         '<code_set_name> /\nFOO-BAR\n',
                         '<code_set_name> "FOO-BAR"\n',
                         '<mb_cur_max> 1 /\nCHARMAP\n'
                         '<code_set_name> FOO-BAR\n'):
            with self.subTest(spelling=spelling):
                root = tempfile.mkdtemp(prefix='pg-glibc-loose-')
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                _cm_file(root, 'AAA', _named('FOO-BAR'))
                _cm_file(root, 'LLL.gz', head + spelling + body)
                c = nc.CharmapsDir(root)
                self.assertIn('LLL', c.codesets()[1])
                m = self.built(self.measurement({'x': 'FOO-BAR'}), c, {})
                self.assertEqual(m.unmapped, [
                    ('x', 'its encoding FOO-BAR is declared by AAA, LLL')])

    def test_a_charmap_of_the_tag_the_machine_lacks_stops_the_check(self):
        """With IBM1133 gone, a build made with it would be traced to
        IBM1162, which declares the same codeset (round 4 of PR D)."""
        c = self.cms({'IBM1162': _named('IBM1133')})
        with self.assertRaisesRegex(
                nc.Unusable, "The machine lacks 1 of t's charmaps "
                             r"\(IBM1133\)"):
            self.built(self.measurement({'lo_LA.ibm1133': 'IBM1133'}), c, {},
                       {'IBM1133': 'id1', 'IBM1162': c.oid('IBM1162')})

    def test_a_charmap_that_names_a_keyword_is_held_to_its_tag(self):
        """`<code_set_name> END`: glibc stores the file's name, not END.
        Read as loose, a file that is not the tag's stops the check."""
        c = self.cms({'SAMI-WS2': _named('WIN-SAMI-2'),
                      'WIN-SAMI-2': _named('END')})
        self.assertIn('WIN-SAMI-2', c.codesets()[1])
        with self.assertRaisesRegex(nc.Unusable, 'WIN-SAMI-2 on the machine '
                                                 "is not t's"):
            self.built(self.measurement({'se_NO.winsami2': 'WIN-SAMI-2'}), c,
                       {}, {'SAMI-WS2': c.oid('SAMI-WS2')})

    def test_a_loose_charmap_not_the_tag_s_stops_the_check(self):
        """A loose charmap added or changed on the machine: what codeset
        glibc stores from it is not known (round 3 of PR D found forms no
        model of the reader here covers), so no build is traced."""
        loose = '<comment> %\n' + _named('FOO-BAR')
        for name, ids in (('added', lambda c: {'AAA': c.oid('AAA')}),
                          ('changed', lambda c: {'AAA': c.oid('AAA'),
                                                 'LLL': 'another blob'})):
            with self.subTest(name):
                root = tempfile.mkdtemp(prefix='pg-glibc-loose-')
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                _cm_file(root, 'AAA', _named('X'))
                _cm_file(root, 'LLL.gz', loose)
                c = nc.CharmapsDir(root)
                with self.assertRaisesRegex(
                        nc.Unusable, "LLL.gz on the machine is not t's and "
                                     "could not be read whole"):
                    self.built(self.measurement({'x': 'X'}), c, {}, ids(c))

    def test_a_measurement_that_cannot_stand_for_the_machine_is_refused(self):
        c = self.cms({'A': _named('A')})
        for raw, why in (('not json', 'does not read'),
                         ('{"glibc_build": "b-1"}', 'lists no locale'),
                         ('{"glibc_build": "b-1", "locales": {"x": 1}}',
                          'lists no locale')):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(nc.Unusable, why):
                    self.built(self.measurement({}, raw=raw), c, {})
        with self.assertRaisesRegex(nc.Unusable, "taken on 'b-2', not on b-1"):
            self.built(self.measurement({'x': 'A'}, build='b-2'), c, {})

    def test_equal_bytes_are_one_text_and_0xff_is_recorded(self):
        for side in ('o', 'n'):
            os.mkdir(os.path.join(self.root, side))
            for name, body in (('same', b'same bytes\n'),
                               ('ff', b'x\xffy\n')):
                with open(os.path.join(self.root, side, name), 'wb') as fh:
                    fh.write(body)
        by_bytes = {}
        o = nc.read_texts(os.path.join(self.root, 'o'), ['same', 'ff'],
                          by_bytes)
        n = nc.read_texts(os.path.join(self.root, 'n'), ['same', 'ff'],
                          by_bytes)
        self.assertIs(o['same'], n['same'])
        self.assertIn(o['ff'], g.TEXTS_WITH_0XFF)
        self.assertNotIn(o['same'], g.TEXTS_WITH_0XFF)


class MachineListed(unittest.TestCase):
    """What the summary reads of a machine check: never a list that reads
    as nothing to add over a build or a name nobody compared."""

    def report(self, **kw):
        base = dict(not_run=None, builds=2, charmaps=[], changed=[],
                    sizes={}, found=[], skipped=[], not_compared=[],
                    unsearched=[], translit=None, translit_skipped=0)
        base.update(kw)
        return ck.Report(**base)

    def test_what_was_listed_not_compared_or_not_traced(self):
        found = ck.Found('loc_a', 'loc_a.utf8', ('UTF-8', 'UTF-8'), {0xE9},
                         ['loc_a'], None)
        r = nc.Result(self.report(found=[found],
                                  not_compared=['x.utf8', 'y_YY.utf8']),
                      'o', 'n', [nc.Measured(
                          [ck.Build('x.utf8', 'x', 'UTF-8')],
                          [('#odd', 'why'), ('ok_name', 'why')], 3)])
        # x.utf8 by its locale; y_YY.utf8, which no build names, as itself.
        self.assertEqual(nc.listed(r), sorted(['loc_a', 'x', 'y_YY.utf8',
                                               "'#odd'", 'ok_name']))

    def test_nothing_compared_writes_no_list(self):
        for kw in ({'not_run': 'a reason'}, {'builds': 0}):
            with self.subTest(**kw):
                self.assertIsNone(nc.listed(nc.Result(
                    self.report(**kw), 'o', 'n', [])))

    def test_no_build_compared_still_lists_what_it_names(self):
        """Every build in a charmap the tag lacks: none compared, all of
        them named, and the summary must name them too."""
        absent = ck.Found('de_DE', 'de_DE.x', ('X', 'X'), set(), [],
                          'the tag has no charmap X')
        self.assertEqual(nc.listed(nc.Result(
            self.report(builds=0, found=[absent]), 'o', 'n', [])), ['de_DE'])


class MachineReadsWhatItIsAskedFor(unittest.TestCase):
    """compare's read_more, for a machine: the charmaps it names are read
    then, so that the 0xFF gate that follows sees them
    (collation_keys._ff_gate), not later, when a charmap is first used."""

    def test_the_charmaps_named_are_read_now(self):
        reads = []

        def load(name):
            reads.append(name)
            return 'text of ' + name
        a = ck.Side('a', set(), {}, {'X': '1', 'Y': '2'}, nc._Texts(load))
        b = ck.Side('b', set(), {}, {'X': '1'}, nc._Texts(load))
        nc._read_more((a, b))({'a': {'X', 'Y'}, 'b': {'X', 'Z'}})
        self.assertEqual((sorted(a.charmap_texts), sorted(b.charmap_texts)),
                         (['X', 'Y'], ['X']))
        self.assertEqual(sorted(reads), ['X', 'X', 'Y'])


class ChecksWithoutTheUnbuiltSearch(unittest.TestCase):
    """compare(search_unbuilt=False), for machines: their builds are
    measured, and a locale a machine does not build is not there for
    PostgreSQL to use."""

    def test_point_5_runs_only_when_asked(self):
        old15 = _named('CS', '<U0041> /x41', '<U00E9> /xe9')
        new15 = _named('CS', '<U0041> /x41', '<U00E9> /xea')
        texts = {'en_XX': locale_text('<U00E9> IGNORE'),
                 'de_XX': locale_text('<U0041> IGNORE')}
        b = ck.supported_builds('SUPPORTED-LOCALES=\\\nde_XX/CS \\\n', 't')
        for unbuilt, expect in ((True, ['en_XX']), (False, [])):
            with self.subTest(search_unbuilt=unbuilt):
                old, new = sides(texts, texts, {'CS': old15}, {'CS': new15})
                r = ck.compare(old, new, b, b, set,
                               search_unbuilt=unbuilt)
                self.assertEqual([f.source for f in r.found], expect)


class ReadersShared(unittest.TestCase):
    """Two Sides given one ask_utf8 are read by one localedef, as steps 6
    and 7 read a machine and its tag; git is not asked."""

    def test_one_reader_is_not_asked_and_two_are(self):
        calls = []

        def ask():
            calls.append(1)
            return True
        a, b = (ck.Side(t, set(), {}, {}, {}) for t in ('a', 'b'))
        a.ask_utf8 = b.ask_utf8 = ask
        self.assertFalse(ck._readers_differ(a, b))
        self.assertEqual(calls, [])
        a, b = (ck.Side(t, set(), {}, {}, {}) for t in ('a', 'b'))
        a.ask_utf8, b.ask_utf8 = (lambda: True), (lambda: False)
        self.assertTrue(ck._readers_differ(a, b))


class ReadingCharsOnce(unittest.TestCase):
    """g.reading_chars_once: each text read once inside the block, and the
    reader put back on the way out, an exception included."""

    def test_a_text_is_read_once_and_the_reader_restored(self):
        real = g.reading_chars
        with mock.patch.object(g, '_glibc_chars',
                               wraps=g._glibc_chars) as spy:
            with g.reading_chars_once():
                text = locale_text('<U0041> IGNORE')
                self.assertEqual(g.reading_chars(text),
                                 g.reading_chars(text))
                # _collate_body asks through the same memo.
                g._collate_body(text)
                self.assertEqual(spy.call_count, 1)
            self.assertIs(g.reading_chars, real)
            with self.assertRaises(KeyError):
                with g.reading_chars_once():
                    raise KeyError('x')
            self.assertIs(g.reading_chars, real)


class DecodedTexts(unittest.TestCase):

    def test_a_0xff_byte_is_recorded(self):
        self.assertIn(g.decoded(b'a\xffb'), g.TEXTS_WITH_0XFF)
        self.assertEqual(g.decoded(b'a\xc3\xa9'), 'a\u00e9')
        self.assertNotIn(g.decoded(b'plain text 13.7'), g.TEXTS_WITH_0XFF)


class MachineNotRun(unittest.TestCase):
    """A machine's NOT RUN keeps the node file's path whole, so it can be
    copied; step 2's wraps as it always did."""

    def test_a_path_with_hyphens_is_not_cut(self):
        path = ('/very-long-folder-name/with-many-hyphens-in-it/'
                'session-2026-09-25-locale-order/node.tar')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nc.not_run(f'{path} holds no charmaps', nc.STEP67_WORDS,
                       'glibc-2.28', 'glibc-2.28-251.el8_10.40.x86_64')
        self.assertIn(path, out.getvalue().split())
        self.assertIn('NOT RUN: the comparison of the character sets '
                      'glibc-2.28-251.el8_10.40.x86_64 builds its locales '
                      "with, against glibc-2.28's (/usr/share/i18n/charmaps/).",
                      flat(out.getvalue()))

def _bang_blocks(out):
    blocks, cur = [], None
    for line in out.split('\n'):
        if line.startswith('!!'):
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.startswith('   '):
            cur.append(line)
        else:
            cur = None
    return [' '.join(x.strip() for x in b) for b in blocks]


if __name__ == '__main__':
    unittest.main()
