"""Layer 1: the algorithmic core, with no git and no glibc clone.

Every case here freezes a failure this tool actually shipped. The CHANGELOG
entry each one guards is quoted in its docstring, because a test whose purpose
is forgotten is a test somebody deletes during a refactor.
"""
import os
import shutil
import tempfile
import unittest

import _harness  # also puts scripts/ on sys.path

import glibc_locale_data as g
import diff_collation_code as d
import filter_lc_collate_changes as f
import diff_distro_locales as dd


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

    def test_a_longer_word_ending_in_the_keyword_is_not_the_keyword(self):
        self.assertEqual(
            g.classify_collation_style(collate('no_codepoint_collation')),
            'explicit')

    def test_codepoint_outranks_an_ellipsis_in_the_same_block(self):
        """glibc: the keyword "in any part of any LC_COLLATE immediately
        discards all collation information", so it cannot be outvoted by a
        range sitting beside it."""
        both = collate('codepoint_collation', '<U0000>', '..', '<U10FFFF>')
        self.assertEqual(g.classify_collation_style(both), 'codepoint')

    def test_copy_only_and_explicit_are_distinguished(self):
        self.assertEqual(g.classify_collation_style(collate('copy "iso14651_t1"')),
                         'copy-only')
        self.assertEqual(
            g.classify_collation_style(collate('<U0041> <U0041>;IGNORE')),
            'explicit')

    def test_no_block_is_none(self):
        self.assertEqual(g.classify_collation_style('LC_TIME\nEND LC_TIME\n'),
                         'none')


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
        locales reported on glibc-2.12..2.17 where there are 278."""
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
