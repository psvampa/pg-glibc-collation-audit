"""Layer 0: is the source being audited the source glibc released?

Everything else in this suite asserts what the tool concludes. This asserts what
it concluded it FROM. The audit reads a third-party mirror
(github.com/bminor/glibc) and addresses it by tag, and a tag is a mutable
pointer -- nothing else here notices if it moves.

Ordering matters: if a tag no longer resolves to its pinned commit, every
number in test_known_answers.py is suspect, and reading a count mismatch
there as a code regression would be wrong.

One class is about the suite instead: HasTagsAnswersTheQuestionAsked checks
the harness's answer to which tags the clone has, which decides whether
every clone-gated class of the suite runs or skips.
"""
import subprocess
import unittest

from _harness import (EXPECTED_SHA, FLOOR_NEW, FLOOR_OLD, GLIBC_CLONE, MID,
                       NEW, OLD, SIGNATURES_UNCHECKED, flat, has_tags,
                       needs_clone)

import glibc_locale_data as g


@needs_clone
class HasTagsAnswersTheQuestionAsked(unittest.TestCase):
    """has_tags remembers its answers for the life of a process. A
    remembered answer must be the one to the question asked: a tag the
    clone lacks is missing after a tag it has was asked about, alone and
    beside it, or a class whose tags are gone runs instead of skipping."""

    def test_a_missing_tag_stays_missing_after_a_present_one(self):
        missing = 'glibc-0.0-no-such-tag'
        self.assertTrue(has_tags(OLD))
        self.assertFalse(has_tags(missing))
        self.assertFalse(has_tags(OLD, missing))
        self.assertFalse(has_tags(missing, OLD))
        self.assertTrue(has_tags(OLD))
        # A question not asked before: a False remembered for one list of
        # tags must not answer another.
        self.assertTrue(has_tags(NEW, OLD))


@needs_clone
class TagsResolveToPinnedCommits(unittest.TestCase):
    def test_each_tag_points_where_it_did_when_the_results_were_published(self):
        for tag, expected in sorted(EXPECTED_SHA.items()):
            with self.subTest(tag=tag):
                got = subprocess.run(
                    ['git', 'rev-parse', f'{tag}^{{commit}}'],
                    cwd=GLIBC_CLONE, capture_output=True, text=True).stdout.strip()
                self.assertEqual(
                    got, expected,
                    f"\n{tag} now resolves to {got}, not {expected}.\n"
                    f"The tag MOVED, or this clone is serving different content.\n"
                    f"Every number in test_known_answers.py was derived from "
                    f"{expected}; do NOT read a mismatch there as a code "
                    f"regression until this is explained.")

    def test_the_pinned_tags_are_the_ones_the_suite_uses(self):
        """Guards against pinning one tag and testing another. The two floor
        tags carry no published verdict, but test_known_answers asserts
        numbers derived from them, so they are pinned on the same terms."""
        self.assertEqual(sorted(EXPECTED_SHA),
                         sorted([OLD, MID, NEW, FLOOR_OLD, FLOOR_NEW]))


@needs_clone
class TagSignatures(unittest.TestCase):
    """The release tags are GPG-signed. Verification needs the maintainers'
    keys, which a stock machine does not have -- so these assert that the
    signature EXISTS and that the tool reports its state honestly, not that it
    verifies here."""

    def test_every_pinned_tag_is_a_signed_annotated_tag(self):
        for tag in sorted(EXPECTED_SHA):
            with self.subTest(tag=tag):
                status, _ = g.verify_tag(GLIBC_CLONE, tag)
                self.assertNotIn(status, ('unsigned', 'not-a-tag'),
                                 f'{tag} lost its signature')

    def test_an_unverifiable_signature_is_never_reported_as_good(self):
        """The distinction the whole helper exists for: `git verify-tag` exits
        1 for "no gpg", "no key" and "forged" alike. Reporting the first two as
        'good' would be a lie; reporting them as 'bad' would train people to
        ignore a real one."""
        status, detail = g.verify_tag(GLIBC_CLONE, NEW)
        self.assertIn(status, ('good', 'no-key', 'no-gpg', 'bad'))
        if status != 'good':
            self.assertTrue(detail, 'an unchecked signature must say why')

    def test_a_lightweight_ref_is_reported_as_having_nothing_to_verify(self):
        status, _ = g.verify_tag(GLIBC_CLONE, 'HEAD')
        self.assertEqual(status, 'not-a-tag')

    def test_a_bad_signature_aborts_the_audit(self):
        """The one status that must stop the run. Mutation testing found this
        unguarded: removing the abort left the whole suite green, because no
        tag available here actually has a forged signature."""
        import contextlib
        import io as _io
        real = g.verify_tag
        g.verify_tag = lambda repo, tag: ('bad', 'INVALID signature')
        try:
            # Redirected: this prints a scary INVALID line by design, and a
            # test must not leave that in the runner's output where it reads
            # like a real finding.
            with contextlib.redirect_stdout(_io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    g.report_tag_provenance(GLIBC_CLONE, NEW)
            self.assertNotEqual(caught.exception.code, 0)
        finally:
            g.verify_tag = real

    def test_an_unchecked_signature_does_not_abort(self):
        """The other direction: refusing to run without gpg would make the tool
        unusable on a stock container for no gain in truth."""
        real = g.verify_tag
        g.verify_tag = lambda repo, tag: ('no-gpg', 'gpg is not installed')
        try:
            import contextlib
            import io as _io
            with contextlib.redirect_stdout(_io.StringIO()):
                g.report_tag_provenance(GLIBC_CLONE, NEW)   # must not raise
        finally:
            g.verify_tag = real

    def test_a_signature_not_checked_is_a_warning(self):
        """16.154: every status but good and bad is a `!!` block, which
        the summary's warnings repeat: UNCHECKED (no gpg, no key) and
        UNSIGNED (no signature, a commit) alike, each tag named once; good
        prints none. Faked per status, in process: which one a machine
        gives depends on its gpg and keys."""
        import contextlib
        import io as _io
        real = g.verify_tag
        try:
            for status in ('no-gpg', 'no-key', 'unverifiable', 'revoked-key',
                           'expired-key', 'expired-sig', 'untrusted',
                           'unsigned', 'not-a-tag'):
                with self.subTest(status=status):
                    g.verify_tag = lambda repo, tag, s=status: (s, 'why')
                    buf = _io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        g.report_tag_provenance(GLIBC_CLONE, OLD, NEW, OLD)
                    lines = buf.getvalue().splitlines()
                    mark = ('UNSIGNED' if status in ('unsigned', 'not-a-tag')
                            else 'UNCHECKED')
                    self.assertTrue(any(ln.startswith(f'  {mark} {OLD} = ')
                                        for ln in lines), lines)
                    at =[i for i, ln in enumerate(lines)
                          if ln.startswith('!!')]
                    self.assertEqual(len(at), 1, lines)
                    block = [lines[at[0]]]
                    for ln in lines[at[0] + 1:]:
                        if not ln.startswith('   '):
                            break
                        block.append(ln)
                    self.assertTrue(flat(' '.join(block)).startswith(
                        f'{SIGNATURES_UNCHECKED}{OLD}, {NEW} (why is on '
                        f"each one's line under 'Tag provenance'"), block)
                    self.assertIn('sourceware.org/git/glibc.git',
                                  flat(' '.join(block)))
            g.verify_tag = lambda repo, tag: ('good', 'signed')
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                g.report_tag_provenance(GLIBC_CLONE, OLD, NEW)
            self.assertNotIn('!!', buf.getvalue())
        finally:
            g.verify_tag = real

    def test_provenance_output_names_the_commit_actually_read(self):
        """A reader who cannot verify a signature can still compare the commit
        id against a source they trust. That only works if it is printed."""
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            g.report_tag_provenance(GLIBC_CLONE, OLD, NEW)
        out = buf.getvalue()
        for tag in (OLD, NEW):
            self.assertIn(tag, out)
            self.assertIn(EXPECTED_SHA[tag][:12], out)


# What `git verify-tag --raw` really wrote to stderr, and its exit code, on
# Rocky 9.3 (git 2.52.0, gnupg2 2.3.3), 2026-11-12: glibc-2.39 and 2.12 with
# the GNU keyring (ftp.gnu.org/gnu/gnu-keyring.gpg) imported; the others on
# tags signed in the node with keys made for the case. el8 and el10 gave
# the same keywords and exit codes.
EL9_GLIBC_239_EXPKEYSIG = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] KEY_CONSIDERED 35B17DF5752577CA0C541CEB94BFDF4484AD142F 0\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] SIG_ID vu6pBlRb6bwvRtugD4rQcCyVPkY 2024-01-31 1706662099\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] KEY_CONSIDERED 35B17DF5752577CA0C541CEB94BFDF4484AD142F 0\n'
    b'[GNUPG:] EXPKEYSIG DC2B16215ED5412A Andreas K. Huettel (at work) '
    b'<mail@akhuettel.de>\n'
    b'[GNUPG:] NOTATION_NAME issuer-fpr@notations.openpgp.fifthhorseman.net\n'
    b'[GNUPG:] NOTATION_FLAGS 0 1\n'
    b'[GNUPG:] NOTATION_DATA FD19E6D31B192EE4DC63EAD3DC2B16215ED5412A\n'
    b'[GNUPG:] VALIDSIG FD19E6D31B192EE4DC63EAD3DC2B16215ED5412A 2024-01-31 '
    b'1706662099 0 4 0 1 10 00 35B17DF5752577CA0C541CEB94BFDF4484AD142F\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499647\n'
    b'[GNUPG:] KEYEXPIRED 1775499585\n'
    b'[GNUPG:] KEY_CONSIDERED 35B17DF5752577CA0C541CEB94BFDF4484AD142F 0\n'
))

EL9_REVKEYSIG = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEY_CONSIDERED C32E696CAF14454F5EFB855D4200FA5DBC3B6C80 0\n'
    b'[GNUPG:] SIG_ID M4oqlJBitDa9DzfJi3R91VZpumE 2026-11-12 1794522965\n'
    b'[GNUPG:] KEY_CONSIDERED C32E696CAF14454F5EFB855D4200FA5DBC3B6C80 0\n'
    b'[GNUPG:] REVKEYSIG 4200FA5DBC3B6C80 Revoked Key <rev@example.org>\n'
    b'[GNUPG:] VALIDSIG C32E696CAF14454F5EFB855D4200FA5DBC3B6C80 2026-11-12 '
    b'1794522965 0 4 0 1 8 00 C32E696CAF14454F5EFB855D4200FA5DBC3B6C80\n'
    b'[GNUPG:] KEYREVOKED\n'
    b'[GNUPG:] KEY_CONSIDERED C32E696CAF14454F5EFB855D4200FA5DBC3B6C80 0\n'
    b'[GNUPG:] TRUST_UNDEFINED 0 pgp\n'
))

EL9_EXPSIG = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEY_CONSIDERED 051E2E1040245FFF14CBBB406CD8334257E6FA0A 0\n'
    b'[GNUPG:] SIG_ID Y6C9Wft9ujEo9fz+ZzYdelN29Mw 2024-02-01 1706745600\n'
    b'[GNUPG:] KEY_CONSIDERED 051E2E1040245FFF14CBBB406CD8334257E6FA0A 0\n'
    b'[GNUPG:] EXPSIG 6CD8334257E6FA0A Sigexp Key <sigexp@example.org>\n'
    b'[GNUPG:] VALIDSIG 051E2E1040245FFF14CBBB406CD8334257E6FA0A 2024-02-01 '
    b'1706745600 1706832000 4 0 1 8 00 '
    b'051E2E1040245FFF14CBBB406CD8334257E6FA0A\n'
    b'[GNUPG:] TRUST_UNDEFINED 0 pgp\n'
))

EL9_GOODSIG_UNTRUSTED = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEY_CONSIDERED 49006F33144F944F1E1D732852141B257B602FE4 0\n'
    b'[GNUPG:] SIG_ID GhQd/o+QtelfqPbRZED0YZ31has 2026-11-12 1794522965\n'
    b'[GNUPG:] KEY_CONSIDERED 49006F33144F944F1E1D732852141B257B602FE4 0\n'
    b'[GNUPG:] GOODSIG 52141B257B602FE4 Good Key <good@example.org>\n'
    b'[GNUPG:] VALIDSIG 49006F33144F944F1E1D732852141B257B602FE4 2026-11-12 '
    b'1794522965 0 4 0 1 8 00 49006F33144F944F1E1D732852141B257B602FE4\n'
    b'[GNUPG:] TRUST_UNDEFINED 0 pgp\n'
))

EL9_GOODSIG_IMPORTED = (0, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEY_CONSIDERED 49006F33144F944F1E1D732852141B257B602FE4 0\n'
    b'[GNUPG:] SIG_ID GhQd/o+QtelfqPbRZED0YZ31has 2026-11-12 1794522965\n'
    b'[GNUPG:] KEY_CONSIDERED 49006F33144F944F1E1D732852141B257B602FE4 0\n'
    b'[GNUPG:] GOODSIG 52141B257B602FE4 Good Key <good@example.org>\n'
    b'[GNUPG:] VALIDSIG 49006F33144F944F1E1D732852141B257B602FE4 2026-11-12 '
    b'1794522965 0 4 0 1 8 00 49006F33144F944F1E1D732852141B257B602FE4\n'
    b'[GNUPG:] TRUST_UNDEFINED 0 pgp\n'
))

EL9_ERRSIG_MD5 = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEY_CONSIDERED 35437666D6B8E4AE90C07DD91B34913BA2388C3E 0\n'
    b'[GNUPG:] ERRSIG 1B34913BA2388C3E 1 1 00 1706745600 5 '
    b'35437666D6B8E4AE90C07DD91B34913BA2388C3E\n'
))

EL9_GLIBC_212_NO_PUBKEY = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] ERRSIG DA28C23A79FF4474 17 2 00 1272921158 9 -\n'
    b'[GNUPG:] NO_PUBKEY DA28C23A79FF4474\n'
))

EL9_BADSIG = (1, (
    b'[GNUPG:] NEWSIG\n'
    b'[GNUPG:] KEY_CONSIDERED 49006F33144F944F1E1D732852141B257B602FE4 0\n'
    b'[GNUPG:] KEY_CONSIDERED 49006F33144F944F1E1D732852141B257B602FE4 0\n'
    b'[GNUPG:] BADSIG 52141B257B602FE4 Good Key <good@example.org>\n'
))

EL9_NO_GPG = (1, (
    b'error: cannot run gpg: No such file or directory\n'
))


class VerifyTagClassification(unittest.TestCase):
    """verify_tag's whole job is telling apart the three things `git verify-tag`
    reports identically (it exits 1 for all of them). Driven with faked git
    output, because a machine cannot produce a forged glibc tag on demand.
    No clone needed: every git call is faked, and one that is not fails.

    Mutation testing added this class: making verify_tag return 'good' whenever
    gpg was missing left every other test passing.
    """

    def drive(self, verify_rc, verify_stderr, signed=True):
        real = g.run_git

        class Fake:
            def __init__(self, rc=0, out=b'', err=b''):
                self.returncode, self.stdout, self.stderr = rc, out, err

        body = (b'object ef321e\ntype commit\ntag glibc-2.39\n'
                b'tagger Andreas K. Huettel <x@example.org> 1706662000 +0100\n\n'
                + (b'-----BEGIN PGP SIGNATURE-----\n' if signed else b''))

        def fake(args, repo, allow_fail=False):
            if args == ['cat-file', '-t', NEW]:
                return Fake(out=b'tag\n')
            if args == ['cat-file', 'tag', NEW]:
                return Fake(out=body)
            if args == ['verify-tag', '--raw', NEW]:
                return Fake(rc=verify_rc, err=verify_stderr)
            raise AssertionError(f'verify_tag ran a git command this test '
                                 f'does not fake: {args}')

        g.run_git = fake
        try:
            return g.verify_tag('/no/glibc/clone/here', NEW)
        finally:
            g.run_git = real

    def test_missing_gpg_is_unchecked_never_good(self):
        status, detail = self.drive(1, b'error: cannot run gpg: No such file')
        self.assertEqual(status, 'no-gpg')
        self.assertIn('NOT checked', detail)

    def test_a_missing_public_key_is_unchecked_never_good(self):
        status, _ = self.drive(1, b'[GNUPG:] ERRSIG ABC123 1 8 00 1 9\n'
                                  b'[GNUPG:] NO_PUBKEY ABC123')
        self.assertEqual(status, 'no-key')

    def test_a_forged_signature_is_bad(self):
        status, _ = self.drive(1, b'[GNUPG:] BADSIG ABC123 Someone Else')
        self.assertEqual(status, 'bad')

    def test_a_valid_signature_is_good(self):
        status, detail = self.drive(0, b'[GNUPG:] GOODSIG ABC123 Andreas\n'
                                       b'[GNUPG:] VALIDSIG ABC123')
        self.assertEqual(status, 'good')
        self.assertIn('Andreas', detail)

    def test_an_expired_key_is_unchecked_never_good(self):
        """git exits 1 on EXPKEYSIG, and gpg gives VALIDSIG beside it: what
        glibc-2.28, 2.34 and 2.39 give with the GNU keyring. Read as good
        before, printed OK while git refused it."""
        status, detail = self.drive(*EL9_GLIBC_239_EXPKEYSIG)
        self.assertEqual(status, 'expired-key')
        self.assertIn('expired', detail)

    def test_a_revoked_key_is_unchecked_never_good_nor_bad(self):
        status, detail = self.drive(*EL9_REVKEYSIG)
        self.assertEqual(status, 'revoked-key')
        self.assertIn('revoked', detail)

    def test_an_expired_signature_is_unchecked_never_good(self):
        status, _ = self.drive(*EL9_EXPSIG)
        self.assertEqual(status, 'expired-sig')

    def test_a_good_signature_git_refuses_is_unchecked(self):
        """GOODSIG with exit 1: gpg.minTrustLevel=fully and a key of
        undefined trust. The same stderr with exit 0 is good: the exit code
        decides."""
        status, _ = self.drive(*EL9_GOODSIG_UNTRUSTED)
        self.assertEqual(status, 'untrusted')
        status, _ = self.drive(0, EL9_GOODSIG_UNTRUSTED[1])
        self.assertEqual(status, 'good')

    def test_a_signature_gpg_cannot_check_is_not_called_a_missing_key(self):
        """ERRSIG without NO_PUBKEY (an MD5 signature): the key is in the
        keyring, so "not in your keyring" would be false."""
        status, detail = self.drive(*EL9_ERRSIG_MD5)
        self.assertEqual(status, 'unverifiable')
        self.assertNotIn('keyring', detail)

    def test_real_stderr_of_the_old_cases(self):
        self.assertEqual(self.drive(*EL9_GOODSIG_IMPORTED)[0], 'good')
        self.assertEqual(self.drive(*EL9_GLIBC_212_NO_PUBKEY)[0], 'no-key')
        self.assertEqual(self.drive(*EL9_BADSIG)[0], 'bad')
        self.assertEqual(self.drive(*EL9_NO_GPG)[0], 'no-gpg')

    def test_an_unsigned_tag_is_reported_as_unsigned(self):
        status, _ = self.drive(0, b'', signed=False)
        self.assertEqual(status, 'unsigned')



@needs_clone
class ProvenanceReadsWhatItPrints(unittest.TestCase):
    """The two reads under 'Tag provenance' that used to run with
    allow_fail (2.11): the commit id printed beside a tag, and the tag
    object verify_tag reads its signature from. A git that fails on either
    stops the run with git's message; it no longer prints a tag with no id,
    or "the tag object carries no signature" over an object it could not
    read. Faked at subprocess.run, so that run_git's own handling of a
    failure is what runs; each fake answers the exact argv the code makes
    and raises on anything else. Neither failure is reachable in a real run
    (both refs are checked before), so this is the only place they run.
    """

    def run_faked(self, answers, call):
        import contextlib
        import io

        def fake_run(argv, **kw):
            for want, rc, out in answers:
                if argv == ['git', *g.GIT_CONFIG_OVERRIDES, *want]:
                    return subprocess.CompletedProcess(
                        argv, rc, out, b'fatal: could not read it')
            raise AssertionError(f'unexpected command: {argv}')

        real_run = g.subprocess.run
        g.subprocess.run = fake_run
        buf, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    call()
        finally:
            g.subprocess.run = real_run
        self.assertNotEqual(caught.exception.code, 0)
        return buf.getvalue(), err.getvalue()

    def test_a_commit_git_cannot_read_stops_the_run(self):
        real_verify = g.verify_tag
        g.verify_tag = lambda repo, tag: ('good', 'signed by someone')
        try:
            out, err = self.run_faked(
                [(['rev-parse', f'{NEW}^{{commit}}'], 128, b'')],
                lambda: g.report_tag_provenance('/no/glibc/clone/here', NEW))
        finally:
            g.verify_tag = real_verify
        self.assertNotIn(f'{NEW} = ', out)
        self.assertIn('rev-parse', err)

    def test_a_tag_object_git_cannot_read_stops_the_run(self):
        out, err = self.run_faked(
            [(['cat-file', '-t', NEW], 0, b'tag\n'),
             (['cat-file', 'tag', NEW], 128, b'')],
            lambda: g.verify_tag('/no/glibc/clone/here', NEW))
        self.assertIn('cat-file tag', flat(err))


if __name__ == '__main__':
    unittest.main()
