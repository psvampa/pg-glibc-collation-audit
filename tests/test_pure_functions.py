"""Layer 1: the algorithmic core, with no git and no glibc clone.

Every case here freezes a failure this tool actually shipped. The CHANGELOG
entry each one guards is quoted in its docstring, because a test whose purpose
is forgotten is a test somebody deletes during a refactor.
"""
import unittest

import _harness  # noqa: F401  -- puts scripts/ on sys.path

import glibc_locale_data as g
import diff_collation_code as d
import filter_lc_collate_changes as f


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

    def test_block_on_the_very_first_line_is_found_by_bounds(self):
        """The glibc <= 2.23 shape. collate_bounds sees it (line-based);
        collate_block does not (it requires a preceding newline). That gap is
        the documented 2.24 version floor -- asserted here so the asymmetry is
        a recorded decision rather than a latent surprise."""
        text = 'LC_COLLATE\ncopy "a"\nEND LC_COLLATE\n'
        self.assertEqual(g.collate_bounds(text), (1, 3))
        self.assertIsNone(g.collate_block(text))


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


class NoiseFilter(unittest.TestCase):
    """"Step 5 discarded real code as comment/copyright".

    The rule used to be: noise unless the line carries one of ;{}=(). That
    swallowed whole hunks under the heading "no substantive change". These are
    the three the CHANGELOG names.
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

    def test_split_hunks_keeps_only_added_and_removed_lines(self):
        hunks = d.split_hunks(self.DIFF)
        self.assertEqual(len(hunks), 3)
        for _, body in hunks:
            for line in body:
                self.assertIn(line[:1], ('+', '-'))
                self.assertFalse(line.startswith(('+++', '---')))


class CommentChar(unittest.TestCase):
    def test_defaults_to_percent(self):
        self.assertEqual(g.comment_char('LC_COLLATE\n'), '%')

    def test_reads_a_declared_one(self):
        self.assertEqual(g.comment_char('comment_char #\nLC_COLLATE\n'), '#')


if __name__ == '__main__':
    unittest.main()
