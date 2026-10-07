"""Layer 1: the algorithmic core, with no git and no glibc clone.

Every case here freezes a failure this tool actually shipped, and its docstring
says which, because a test whose purpose is forgotten is a test somebody
deletes during a refactor.
"""
import contextlib
import importlib.util
import io
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
        """The form that carries the constructed Hangul and Han weights.

        Anchoring the pattern to the start of a line missed every one of these,
        which cleared zh_CN, cmn_TW, iso14651_t1_pinyin and cns11643_stroke --
        a false "unaffected" for the exact class of locale step 4 exists for.
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
        self.assertEqual(f.classify_change(self.OLD, None, [(1, 1)]), 'other')

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
        """main() only reads the new side for files that need it."""
        self.assertEqual(f.classify_change(self.NO_BLOCK, None, []),
                         'no-collate')


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
        to_read = f.new_side_paths(list(self.OLD), self.OLD, self.RENAMED)
        self.assertEqual(to_read, {'localedata/locales/x':
                                   'localedata/locales/y'})

    def test_a_file_with_an_old_block_is_not_read_again(self):
        to_read = f.new_side_paths(list(self.OLD), self.OLD, {})
        self.assertNotIn('localedata/locales/k', to_read)

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
        changed, gained, _, no_collate = f.partition_verdicts(
            [('p', 'gained-collate')])
        self.assertIn('p', changed, 'a gained block was not counted as changed')
        self.assertIn('p', gained)
        self.assertNotIn('p', no_collate)

    def test_each_verdict_lands_in_its_list(self):
        changed, gained, unchanged, no_collate = f.partition_verdicts(
            [('a', 'collate'), ('b', 'other'), ('c', 'no-collate')])
        self.assertEqual((changed, gained, unchanged, no_collate),
                         (['a'], [], ['b'], ['c']))

    def test_order_is_preserved(self):
        changed, _, _, _ = f.partition_verdicts(
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
        three lines ABOVE the keyword. A substring search reads that comment as
        a declaration -- and would then report an ellipsis-based backport as
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
        """C is in no tag, so at a tag it can be neither a root nor a target.
        Over a node's own corpus it is both."""
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


if __name__ == '__main__':
    unittest.main()
