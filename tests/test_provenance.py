"""Layer 0: is the source being audited the source glibc released?

Everything else in this suite asserts what the tool concludes. This asserts what
it concluded it FROM. The audit reads a third-party mirror
(github.com/bminor/glibc) and addresses it by tag, and a tag is a mutable
pointer -- nothing else here notices if it moves.

Ordering matters: if these fail, every number in test_known_answers.py is
suspect, and reading a count mismatch there as a code regression would be wrong.
"""
import subprocess
import unittest

from _harness import EXPECTED_SHA, GLIBC_CLONE, MID, NEW, OLD, needs_clone

import glibc_locale_data as g


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

    def test_the_three_pinned_tags_are_the_ones_the_suite_uses(self):
        """Guards against pinning one tag and testing another."""
        self.assertEqual(sorted(EXPECTED_SHA), sorted([OLD, MID, NEW]))


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


if __name__ == '__main__':
    unittest.main()


@needs_clone
class VerifyTagClassification(unittest.TestCase):
    """verify_tag's whole job is telling apart the three things `git verify-tag`
    reports identically (it exits 1 for all of them). Driven with faked git
    output, because a machine cannot produce a forged glibc tag on demand.

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
            if args[:2] == ['cat-file', '-t']:
                return Fake(out=b'tag\n')
            if args[:2] == ['cat-file', 'tag']:
                return Fake(out=body)
            if args[0] == 'verify-tag':
                return Fake(rc=verify_rc, err=verify_stderr)
            return real(args, repo, allow_fail=allow_fail)

        g.run_git = fake
        try:
            return g.verify_tag(GLIBC_CLONE, NEW)
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

    def test_an_unsigned_tag_is_reported_as_unsigned(self):
        status, _ = self.drive(0, b'', signed=False)
        self.assertEqual(status, 'unsigned')
