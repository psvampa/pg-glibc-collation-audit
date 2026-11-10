"""scripts/build_sources.py: one glibc build's localedata/ and locale/,
rebuilt from its source RPM in the order rpm recorded, and checked against
the build's binary packages.

The known answers come from real rpm, not from this code. For the three
builds of the fixtures, tests/build_sources/<build>/ holds what the SRPM
holds that the rebuild reads (glibc.spec, the patches that touch
localedata/ or locale/, the Sources %prep runs, the patch-git.lua the
spec runs where the build has one, and srpm.json: every file's
name, the recorded ones' sha256, and the PATCH and SOURCE lists and rpm
version of the SRPM's own header), the header of the build's
glibc-locale-source and glibc-common binary packages (no payload), and what
`rpmbuild -bp --nodeps` did with the whole SRPM on Rocky 8, 9 and 10 on
2026-11-03: the order it applied every patch in (rpm-order.txt) and the md5
and executable bit of every file of the tree it left (rpm-tree.md5,
rpm-tree.exec). specs/ holds the spec of each build OTHER_SPECS names;
variants/ what real rpm did with ten inputs that a reading of the spec
in place of rpm's record accepted with a tree other than rpm's, and the
files those inputs add. A patch the fixture does not record is handed in
empty: it touches neither folder, which the tree's equality with rpm's
proves for the three builds.
"""
import functools
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import shutil
import time
import unittest
from unittest import mock

from _harness import (COMPARED_BANNER, GLIBC_CLONE, SCRIPTS_DIR, TESTS_DIR,
                      STEP2_C, SUMMARY_STEP2,
                      Step2Report, after_compared, before_compared,
                      compared_text, deleted_steps_named, flat,
                      summary_block, glibc_shaped, needs_clone,
                      assert_no_deleted_block, nothing_compared_lines,
                      summary_warnings, assert_collation_version,
                      assert_no_route_looks, pg_version_of,
                      EXPECTED_SHA)

import build_sources as bs
import filter_lc_collate_changes as step2
import glibc_locale_data as g

DATA = os.path.join(TESTS_DIR, 'build_sources')

# What build_sources raises as Unread, by message: a build this cannot read
# with nothing found wrong. Every other refusal is Refused, and a test of a
# refusal asserts which of the two it is (assertRefusedAs).
UNREAD = ('this does not know in', ' (looked in ', 'with no metadata, so its',
          'is not there any more', 'could not read ')


def assertRefusedAs(test, exc, why):
    """`exc` is Unread when its message is one of UNREAD, else exactly
    Refused; and it says `why`."""
    text = str(exc)
    test.assertIn(why, text)
    unread = any(u in text for u in UNREAD)
    test.assertIs(type(exc), bs.Unread if unread else bs.Refused, text)
EXTRACT = os.path.join(TESTS_DIR, 'locale_order')
FIXTURES = (('glibc-2.28-251.el8_10.40', 8), ('glibc-2.34-275.el9_8', 9),
            ('glibc-2.39-128.el10_2', 10))
EL9 = 'glibc-2.34-275.el9_8'
# One spec of each RHEL among the 25 older builds Rocky Linux publishes
# with metadata; together they hold the two Lua blocks and ten macro names
# that no spec above holds.
OLDER_SPECS = ('glibc-2.28-127.el8.2', 'glibc-2.34-100.el9_4.4',
               'glibc-2.39-37.el10')
OTHER_SPECS = ('glibc-2.28-251.el8_10.48', 'glibc-2.34-168.el9_6.24',
               'glibc-2.34-283.el9_8', 'glibc-2.39-145.el10_2') + OLDER_SPECS


def recorded(build):
    """(info, header, files) as read_srpm would give them for that build:
    the header's PATCH, SOURCE and RPMVERSION, the recorded files as they
    are, every other file of the SRPM empty."""
    d = os.path.join(DATA, build)
    with open(os.path.join(d, 'srpm.json')) as fh:
        m = json.load(fh)
    files = {n: b'' for n in m['files']}
    for n, sha in m['recorded'].items():
        with open(os.path.join(d, n), 'rb') as fh:
            files[n] = fh.read()
        if hashlib.sha256(files[n]).hexdigest() != sha:
            raise AssertionError('%s/%s is not the file the SRPM holds'
                                 % (build, n))
    info = {k: m[k] for k in ('name', 'version', 'release', 'srpm_sha256',
                              'payload_compressor')}
    hdr = {bs._PATCH: list(m['header_patch']),
           bs._SOURCE: list(m['header_source']),
           bs._RPMVERSION: m['rpm_version']}
    return info, hdr, files


def rpm_tree(build):
    md5, ex = {}, set()
    with open(os.path.join(DATA, build, 'rpm-tree.md5')) as fh:
        for ln in fh:
            h, p = ln.rstrip('\n').split('  ', 1)
            md5[p] = h
    with open(os.path.join(DATA, build, 'rpm-tree.exec')) as fh:
        ex = set(fh.read().split())
    return md5, ex


def rpm_order(build):
    with open(os.path.join(DATA, build, 'rpm-order.txt')) as fh:
        return fh.read().split()


def header_only(build, pkg):
    """A recorded binary package's header as a Binary with no payload."""
    path = os.path.join(DATA, build, pkg + '.head')
    with open(path, 'rb') as f:
        f.read(96)
        bs._header(f, pad=True)
        hdr, _ = bs._header(f, pad=False)
    return bs.Binary(path, None, hdr, None, 'header only')


def other_spec(build):
    with gzip.open(os.path.join(DATA, 'specs', build + '.spec.gz')) as fh:
        return fh.read().decode('utf-8')


@functools.lru_cache(maxsize=None)
def fixture(build):
    """(info, header, files, base, alias, tree, provenance) of a fixture
    build rebuilt from the clone's tag and the recorded files."""
    info, hdr, files = recorded(build)
    base, alias = bs.scope_from_git(GLIBC_CLONE, bs.identity(info)[2])
    tree, prov = bs.rebuild(info, hdr, files, base)
    return info, hdr, files, base, alias, tree, prov


def unknowns(text, files, hdr, info, tree):
    return bs.known_only(text, files, bs.rpm_record(info, hdr, files), tree)


@needs_clone
class RebuildsTheFixtureBuildsAsRpmDid(unittest.TestCase):
    """The three builds, from the clone's tag and the recorded files, against
    what rpmbuild -bp left, what the machines hold and what the binary
    packages record."""

    def test_the_tree_is_the_one_rpm_left(self):
        for build, _ in FIXTURES:
            with self.subTest(build=build):
                tree = fixture(build)[5]
                md5, ex = rpm_tree(build)
                mine = {p: hashlib.md5(v[0]).hexdigest()
                        for p, v in tree.items()}
                self.assertEqual(len(mine), len(md5))
                self.assertEqual(sorted(p for p in mine if mine[p] != md5.get(p)),
                                 [])
                self.assertEqual({p for p, v in tree.items() if v[1]}, ex)

    def test_locales_and_charmaps_are_the_machines(self):
        counts = {}
        for build, n in FIXTURES:
            tree = fixture(build)[5]
            seen = 0
            with tarfile.open(os.path.join(EXTRACT, 'rhel%d.extract.tar.gz'
                                           % n)) as t:
                for m in t:
                    if m.name.startswith('locales/'):
                        p, data = 'localedata/' + m.name, \
                            t.extractfile(m).read()
                    elif m.name.startswith('charmaps/'):
                        p = 'localedata/' + m.name[:-len('.gz')]
                        data = gzip.decompress(t.extractfile(m).read())
                    else:
                        continue
                    with self.subTest(build=build, path=p):
                        self.assertIn(p, tree)
                        self.assertEqual(tree[p][0], data)
                    seen += 1
            counts[build] = seen
        self.assertEqual(counts, {'glibc-2.28-251.el8_10.40': 588,
                                  'glibc-2.34-275.el9_8': 589,
                                  'glibc-2.39-128.el10_2': 599})

    def test_the_headers_patch_list_reversed_is_rpms_order(self):
        """The header was written by rpmbuild -bs on the build host; the
        order by rpmbuild -bp on Rocky 8, 9 and 10: two records of rpm's,
        position by position."""
        for build, _ in FIXTURES:
            with self.subTest(build=build):
                info, hdr, files = recorded(build)
                order = bs.rpm_record(info, hdr, files).order
                rpm = rpm_order(build)
                self.assertEqual(len(order), len(rpm))
                self.assertEqual([i for i, (a, b) in enumerate(zip(order, rpm))
                                  if a != b], [])

    def test_el8_declares_a_patch_above_lower_numbers(self):
        """The case that tells declaration order from number order: rpm
        applies glibc-rh1638523-1 (Patch52) before -2..-6 (Patch47..51)."""
        info, hdr, files = recorded('glibc-2.28-251.el8_10.40')
        order = bs.rpm_record(info, hdr, files).order
        self.assertLess(order.index('glibc-rh1638523-1.patch'),
                        order.index('glibc-rh1638523-2.patch'))

    def test_the_binary_packages_record_this_tree(self):
        """The sha256 of each locale source in glibc-locale-source's header,
        and of locale.alias in glibc-common's, against the rebuilt tree and
        the tag."""
        counts = {}
        for build, _ in FIXTURES:
            with self.subTest(build=build):
                info, _, _, _, alias, tree, _ = fixture(build)
                ls = header_only(build, 'glibc-locale-source')
                common = header_only(build, 'glibc-common')
                bs._of_this_build(ls, 'glibc-locale-source', info)
                bs._of_this_build(common, 'glibc-common', info)
                line = bs.check_locales(tree, ls)
                counts[build] = int(line.split(': ')[1].split(',')[0])
                self.assertIn('the sha256 glibc-common-', bs.check_alias(
                    alias, common))
        self.assertEqual(counts, {'glibc-2.28-251.el8_10.40': 355,
                                  'glibc-2.34-275.el9_8': 356,
                                  'glibc-2.39-128.el10_2': 366})

    def test_a_locale_that_differs_from_the_package(self):
        info, _, _, _, alias, tree, _ = fixture(EL9)
        ls = header_only(EL9, 'glibc-locale-source')
        bad = dict(tree)
        bad['localedata/locales/de_DE'] = (tree['localedata/locales/de_DE'][0]
                                           + b'\n', False)
        with self.assertRaises(bs.Refused) as cm:
            bs.check_locales(bad, ls)
        self.assertIn('locale sources: 1 differ from glibc-locale-source-'
                      '2.34-275.el9_8, e.g. de_DE', str(cm.exception))
        with self.assertRaises(bs.Refused) as cm:
            bs.check_alias(alias + b'#', header_only(EL9, 'glibc-common'))
        self.assertIn("installs another than the tag's", str(cm.exception))

    def test_known_only_knows_the_three_builds(self):
        for build, _ in FIXTURES:
            with self.subTest(build=build):
                info, hdr, files, _, _, tree, _ = fixture(build)
                self.assertEqual(unknowns(bs.spec_text(files), files, hdr,
                                          info, tree), [])

    def test_the_result_names_the_build_as_build_parts_reads_it(self):
        import locale_order as lo
        prov = fixture('glibc-2.28-251.el8_10.40')[6]
        self.assertEqual(prov['build'], 'glibc-2.28-251.el8_10.40.src')
        self.assertEqual(lo.build_parts(prov['build']),
                         ('2.28', '251.el8_10.40'))
        self.assertEqual(lo.version_tuple(prov['version']), (2, 28))
        # The whole item: the wording before step 9.3 ("patches to code
        # outside localedata/ and locale/") shares the strcmp clause and
        # read as if the code under locale/ were checked.
        self.assertEqual(prov['not_checked'][0],
                         "the compiler that built the binary packages, and "
                         "the build's patches to code, such as those to the "
                         "strcmp that strcoll calls")

    def test_patches_to_the_collation_code_are_listed(self):
        """Per build, every recorded patch that names a file of the
        collation code or the four wrappers, in the order rpm applied them:
        the same list build() gives from the whole SRPM, where no other patch
        names one (measured on the three)."""
        want = {
            'glibc-2.28-251.el8_10.40': [
                ('glibc-rh827510.patch', ['locale/loadlocale.c']),
                ('glibc-rh1691528-1.patch', ['locale/programs/ld-collate.c']),
                ('glibc-rh1691528-2.patch', ['locale/programs/ld-collate.c']),
                ('glibc-RHEL-72011-2.patch', ['locale/localeinfo.h']),
                ('glibc-RHEL-72011-3.patch', ['locale/localeinfo.h'])],
            'glibc-2.34-275.el9_8': [
                ('glibc-rh827510.patch', ['locale/loadlocale.c']),
                ('glibc-c-utf8-locale-1.patch', [
                    'locale/C-collate-seq.c', 'locale/C-collate.c',
                    'locale/programs/ld-collate.c',
                    'locale/programs/locfile-kw.gperf']),
                ('glibc-rh2149102.patch', ['locale/C-collate-seq.c']),
                ('glibc-RHEL-72017-2.patch', ['locale/localeinfo.h']),
                ('glibc-RHEL-72017-3.patch', ['locale/localeinfo.h'])],
            'glibc-2.39-128.el10_2': [
                ('glibc-rh827510.patch', ['locale/loadlocale.c']),
                ('glibc-upstream-2.39-221.patch', ['locale/localeinfo.h']),
                ('glibc-upstream-2.39-258.patch', ['locale/localeinfo.h'])]}
        for build, _ in FIXTURES:
            with self.subTest(build=build):
                got = [(c['patch'], c['paths']) for c in
                       fixture(build)[6]['collation_code_patches']]
                self.assertEqual(got, want[build])

    def test_the_four_wrappers_are_in_the_three_tags(self):
        for build, _ in FIXTURES:
            tag = bs.identity(fixture(build)[0])[2]
            for p in ('string/strcoll.c', 'string/strxfrm.c',
                      'wcsmbs/wcscoll.c', 'wcsmbs/wcsxfrm.c'):
                with self.subTest(tag=tag, path=p):
                    self.assertIn(p, bs.COLLATION_CODE)
                    bs.g.run_git(['cat-file', '-e', '%s:%s' % (tag, p)],
                                 GLIBC_CLONE)


@needs_clone
class TheCataloguesAreTheRealSpecs(unittest.TestCase):
    """Each entry of the three catalogues of known_only comes from a real
    spec of the seven builds or of the older ones (OLDER_SPECS), each real
    spec without its entries is refused naming them, and nothing else is in
    them."""

    def heads(self):
        out = {}
        for build, _ in FIXTURES:
            out[build] = bs.spec_text(fixture(build)[2])
        for build in OTHER_SPECS:
            out[build] = other_spec(build)
        return out

    @staticmethod
    def entries(text):
        """(Lua block shas, names defined before the end of %prep)."""
        lines = text.split('\n')
        head = '\n'.join(lines[:bs.prep_span(lines)[1]])
        return ({hashlib.sha256(b.encode()).hexdigest()
                 for _, _, b in bs._lua_blocks(text)},
                {m.group(3) for m in bs._DEFINES.finditer(head)})

    def test_the_lua_blocks_and_the_macro_names(self):
        lua, names = set(), set()
        for build, text in self.heads().items():
            got = self.entries(text)
            lua |= got[0]
            names |= got[1]
        self.assertEqual(lua, set(bs.LUA_CATALOGUE))
        self.assertEqual(names, set(bs.MACRO_NAMES))
        self.assertEqual((len(lua), len(names)), (17, 46))

    def test_the_prep_sequences(self):
        """168.el9_6.24's sequence is in no fixture's spec; its %prep runs
        the same parse-SUPPORTED.py as 275.el9_8 (sha256 aa72e727)."""
        seqs = set()
        for build, _ in FIXTURES:
            info, hdr, files, _, _, tree, _ = fixture(build)
            text = bs.spec_text(files)
            lines = text.split('\n')
            first, end = bs.prep_span(lines)
            seqs.add(bs.prep_keys(bs._Spec(text, files, tree),
                                  lines[first:end]))
        files, tree = fixture(EL9)[2], fixture(EL9)[5]
        text = other_spec('glibc-2.34-168.el9_6.24')
        lines = text.split('\n')
        first, end = bs.prep_span(lines)
        seqs.add(bs.prep_keys(bs._Spec(text, files, tree), lines[first:end]))
        self.assertEqual(seqs, set(bs.PREP_SEQUENCES))
        self.assertEqual(len(seqs), 4)

    def test_a_real_spec_without_its_entries_is_refused(self):
        for cat, why in (('LUA_CATALOGUE', 'a %{lua: block this does not '
                                           'know'),
                         ('MACRO_NAMES', 'which no real spec does'),
                         ('PREP_SEQUENCES', '%prep is not one the real '
                                            'specs have')):
            for build, _ in FIXTURES:
                with self.subTest(catalogue=cat, build=build):
                    info, hdr, files, _, _, tree, _ = fixture(build)
                    with mock.patch.object(bs, cat, frozenset()):
                        got = unknowns(bs.spec_text(files), files, hdr, info,
                                       tree)
                    self.assertTrue(got)
                    self.assertTrue(all(why in w for _, w in got), got)

    def test_each_entry_only_an_older_spec_holds_is_needed(self):
        """Each older spec, read with the files and tree of the fixture of
        its glibc version, is accepted whole; each Lua block or macro name
        that only the older specs hold, removed alone, is named alone, in
        each older spec that holds it."""
        fx = {build.split('-')[1]: build for build, _ in FIXTURES}
        heads = self.heads()
        seen = [set(), set()]
        for build, text in heads.items():
            if build not in OLDER_SPECS:
                for i, got in enumerate(self.entries(text)):
                    seen[i] |= got
        only = [set(), set()]
        for build in OLDER_SPECS:
            info, hdr, files, _, _, tree, _ = fixture(fx[build.split('-')[1]])
            text = heads[build]
            with self.subTest(build=build):
                self.assertEqual(unknowns(text, files, hdr, info, tree), [])
            for i, (cat, word) in enumerate((
                    ('LUA_CATALOGUE', lambda e: '(%s)' % e[:12]),
                    ('MACRO_NAMES', lambda e: 'defines %%%s,' % e))):
                for entry in self.entries(text)[i] - seen[i]:
                    only[i].add(entry)
                    with self.subTest(build=build, entry=entry), \
                            mock.patch.object(bs, cat,
                                              getattr(bs, cat) - {entry}):
                        got = unknowns(text, files, hdr, info, tree)
                        self.assertEqual(len(got), 1, got)
                        self.assertIn(word(entry), got[0][1])
        self.assertEqual((len(only[0]), len(only[1])), (2, 10))


# -- ten inputs, against real rpm --------------------------------------------

with open(os.path.join(DATA, 'variants', 'rpm-answers.json')) as _fh:
    RPM_ANSWERS = json.load(_fh)


def _variant_lines(name):
    with gzip.open(os.path.join(DATA, 'variants', name)) as fh:
        return fh.read().decode().split()


@needs_clone
class RefusesTenInputsRpmBuiltOtherwise(unittest.TestCase):
    """Ten inputs run through real rpmbuild -bp on Rocky 9 (rpm 4.16.1.3)
    against 275.el9_8, which a reading of the spec in place of rpm's record
    accepted with a tree other than rpm's; V0 is the unmodified spec, and every
    variant's tree is compared with V0's. Each is refused with its reason,
    or gives rpm's own order and tree; a %patchlist under %ifarch, one more
    input of the kind with no rpm answer recorded, is refused too. A patch
    dropped in the source folder is one patch-git emits last, so it heads
    the header's list."""

    PG = '%{lua:patchgit.patches()}\n'

    def base(self):
        info, hdr, files, base, _, _, _ = fixture(EL9)
        return info, {k: list(v) if isinstance(v, list) else v
                      for k, v in hdr.items()}, dict(files), base

    def spec(self, files, text):
        files['glibc.spec'] = text.encode('utf-8')

    def with_patch(self, hdr, files, name):
        with open(os.path.join(DATA, 'variants', name), 'rb') as fh:
            files[name] = fh.read()
        hdr[bs._PATCH] = [name] + hdr[bs._PATCH]

    def refused(self, info, hdr, files, base, why):
        with self.assertRaises(bs.Refused) as cm:
            bs.rebuild(info, hdr, files, base)
        assertRefusedAs(self, cm.exception, why)

    def test_v1_a_comment_ending_in_a_backslash(self):
        """rpm removed de_DE: the shell ends a comment at its newline."""
        self.assertEqual(RPM_ANSWERS['V1']['rpm_tree_changes'],
                         {'localedata/locales/de_DE': None})
        info, hdr, files, base = self.base()
        a = bs._AUTOSETUP + '\n'
        self.spec(files, bs.spec_text(files).replace(
            a, a + '# drop de_DE, rebuilt below \\\nrm -f '
            'localedata/locales/de_DE\n', 1))
        self.refused(info, hdr, files, base,
                     '%prep is not one the real specs have: line 2 of its '
                     'body, rm -f localedata/locales/de_DE')

    def test_v2_a_patch_declared_through_a_conditional_macro(self):
        """rpm wrote the patch at position 393 of its list and applied it
        there; the tree is V0's. The header list is the one rpmbuild -bs
        wrote on Rocky 9 for this spec."""
        info, hdr, files, base = self.base()
        self.spec(files, bs.spec_text(files).replace(
            self.PG, '%{?rhel:Patch9001: glibc-RHEL-72017-4.patch}\n' +
            self.PG, 1))
        hdr[bs._PATCH] = _variant_lines('V2.header-patch.txt.gz')
        rpm = _variant_lines('V2.order.txt.gz')
        self.assertEqual(rpm.index('glibc-RHEL-72017-4.patch'), 392)
        self.assertEqual(bs.rpm_record(info, hdr, files).order, rpm)
        tree, _ = bs.rebuild(info, hdr, files, base)
        md5, _ = rpm_tree(EL9)
        self.assertEqual({p: hashlib.md5(v[0]).hexdigest()
                          for p, v in tree.items()}, md5)
        self.assertEqual(RPM_ANSWERS['V2']['rpm_tree_changes'], {})

    def test_v3_lua_redefines_the_patch_flags(self):
        """rpm kept 27 *.rpmkeep backups in the tree."""
        self.assertEqual(len(RPM_ANSWERS['V3']['rpm_tree_changes']), 27)
        info, hdr, files, base = self.base()
        self.spec(files, bs.spec_text(files).replace(
            self.PG, '%{lua: rpm.define("_default_patch_flags '
            '--no-backup-if-mismatch -b -z .rpmkeep")}\n' + self.PG, 1))
        self.refused(info, hdr, files, base,
                     'a %{lua: block this does not know')

    def test_v4_the_prep_epilogue_redefined(self):
        """rpm ran it and changed SUPPORTED."""
        self.assertEqual(list(RPM_ANSWERS['V4']['rpm_tree_changes']),
                         ['localedata/SUPPORTED'])
        info, hdr, files, base = self.base()
        self.spec(files, bs.spec_text(files).replace(
            self.PG, "%global __spec_prep_post sed -i '/^de_DE/d' "
            "localedata/SUPPORTED; exit 0\n" + self.PG, 1))
        self.refused(info, hdr, files, base,
                     'defines %__spec_prep_post, which no real spec does')

    def test_v5_a_deletion_with_an_epoch_date(self):
        """GNU patch removed de_DE (diff -Nrup's +++ dated 1969-12-31)."""
        self.assertEqual(RPM_ANSWERS['V5']['rpm_tree_changes'],
                         {'localedata/locales/de_DE': None})
        info, hdr, files, base = self.base()
        self.with_patch(hdr, files, 'zz-epoch-delete.patch')
        self.refused(info, hdr, files, base, 'a plain diff that deletes')

    def test_v6_a_doubled_slash(self):
        """GNU patch collapses a//localedata and changed de_DE."""
        info, hdr, files, base = self.base()
        self.assertEqual(RPM_ANSWERS['V6']['rpm_tree_changes'],
                         RPM_ANSWERS['V11']['rpm_tree_changes'])
        self.with_patch(hdr, files, 'zz-double-slash.patch')
        self.refused(info, hdr, files, base,
                     'zz-double-slash.patch: a section on [] this does not '
                     'apply')

    def test_v7_a_patch_that_creates_an_orig_file(self):
        """%prep's find removed it: rpm's tree is V0's."""
        self.assertEqual(RPM_ANSWERS['V7']['rpm_tree_changes'], {})
        info, hdr, files, base = self.base()
        self.with_patch(hdr, files, 'zz-orig.patch')
        with self.assertRaises(bs.Refused) as cm:
            bs.rebuild(info, hdr, files, base)
        self.assertIn('the tree holds localedata/locales/de_DE.orig, which '
                      'the find of %prep deletes', str(cm.exception))

    def test_v9_a_diff_with_orig_prefixes_through_a_macro(self):
        """rpm wrote x.diff at position 393 and applied it: de_DE changed.
        The header list is the one rpmbuild -bs wrote on Rocky 9."""
        info, hdr, files, base = self.base()
        self.spec(files, bs.spec_text(files).replace(
            self.PG, '%{?rhel:Patch9000: x.diff}\n' + self.PG, 1))
        with open(os.path.join(DATA, 'variants', 'x.diff'), 'rb') as fh:
            files['x.diff'] = fh.read()
        hdr[bs._PATCH] = _variant_lines('V9.header-patch.txt.gz')
        rpm = _variant_lines('V9.order.txt.gz')
        self.assertEqual(list(RPM_ANSWERS['V9']['rpm_tree_changes']),
                         ['localedata/locales/de_DE'])
        self.assertEqual(rpm.index('x.diff'), 392)
        self.assertEqual(bs.rpm_record(info, hdr, files).order, rpm)
        self.refused(info, hdr, files, base,
                     'x.diff: a section on [] this does not apply')

    def test_v10_a_prep_line_dropped(self):
        """rpm left SUPPORTED.spec and SUPPORTED.glibc in localedata/."""
        self.assertEqual(sorted(RPM_ANSWERS['V10']['rpm_tree_changes']),
                         ['localedata/SUPPORTED.glibc',
                          'localedata/SUPPORTED.spec'])
        info, hdr, files, base = self.base()
        line = 'rm localedata/SUPPORTED.spec localedata/SUPPORTED.glibc\n'
        text = bs.spec_text(files)
        self.assertEqual(text.count(line), 1)
        self.spec(files, text.replace(line, ''))
        self.refused(info, hdr, files, base,
                     '%prep is not one the real specs have: line 11 of its '
                     'body, cp ')

    def test_v11_an_indented_diff_quoted_in_a_message(self):
        """GNU patch strips the indent and changed de_DE."""
        self.assertEqual(list(RPM_ANSWERS['V11']['rpm_tree_changes']),
                         ['localedata/locales/de_DE'])
        info, hdr, files, base = self.base()
        self.with_patch(hdr, files, 'zz-quoted.patch')
        self.refused(info, hdr, files, base,
                     'zz-quoted.patch: line 6 reads to GNU patch as a header '
                     'on a path the result reads')

    def test_a_patchlist_under_ifarch(self):
        """rpm 4.16 reads a %patchlist and the %ifarch inside it, so the arch
        that wrote the SRPM decides whether the patch is in it."""
        info, hdr, files, base = self.base()
        files['zz-arch.patch'] = b''
        hdr[bs._PATCH] = ['zz-arch.patch'] + hdr[bs._PATCH]
        self.spec(files, bs.spec_text(files).replace(
            self.PG, self.PG + '%patchlist\n%ifarch ppc64le\nzz-arch.patch\n'
            '%endif\n', 1))
        self.refused(info, hdr, files, base,
                     'a %patchlist, which no real spec has')


# -- what known_only refuses in a real spec -----------------------------------

@needs_clone
class KnowsOnlyTheRealSpecs(unittest.TestCase):

    def got(self, edit, tree_edit=None):
        info, hdr, files, _, _, tree, _ = fixture(EL9)
        files = dict(files)
        text = edit(bs.spec_text(files))
        files['glibc.spec'] = text.encode()
        if tree_edit:
            tree = tree_edit(dict(tree))
        return unknowns(text, files, hdr, info, tree)

    def one(self, edit, why, tree_edit=None):
        got = self.got(edit, tree_edit)
        self.assertEqual(len(got), 1, got)
        self.assertIn(why, got[0][1])

    def before_prep(self, line):
        return lambda t: t.replace('\n%prep\n', '\n' + line + '\n%prep\n', 1)

    def after_autosetup(self, line):
        a = bs._AUTOSETUP + '\n'
        return lambda t: t.replace(a, a + line + '\n', 1)

    def test_the_real_spec(self):
        self.assertEqual(self.got(lambda t: t), [])

    def test_what_runs_after_prep_while_rpm_reads_the_spec(self):
        """rpm runs these while it reads the spec, before %prep's shell, so
        after %prep too: a block that is not one of the real specs', and any
        %(, %{load: or %include."""
        for line, why in (("%{lua: posix.setenv('LANG', 'C')}",
                           'a %{lua: block this does not know'),
                          ('%(echo x)', 'while rpm reads the spec: %('),
                          ('%{load:/etc/x}', 'while rpm reads the spec: '
                                             '%{load:'),
                          ('%include other.spec', 'while rpm reads the spec: '
                                                  '%include')):
            with self.subTest(line=line):
                self.one(lambda t: t.replace('\n%build\n', '\n%build\n' + line
                                             + '\n', 1), why)

    def test_what_runs_while_rpm_reads_the_spec(self):
        for line, why in (('%(echo x)', '%('),
                          ('%{load:/etc/x}', '%{load:'),
                          ('%include other.spec', '%include')):
            with self.subTest(line=line):
                self.one(self.before_prep(line),
                         'runs or reads something while rpm reads the spec: '
                         + why)

    def test_a_lua_file_that_changes_macros_or_the_patches(self):
        """patch-git.lua is a Source the spec runs with dofile; it changes
        in every build, so it is read for what it may not do. The real one
        of this build is recorded, and passes (so do the five real ones of
        the seven builds, measured)."""
        info, hdr, files, _, _, tree, _ = fixture(EL9)
        self.assertIn('patch-git.lua', hdr[bs._SOURCE])
        self.assertGreater(len(files['patch-git.lua']), 60000)
        text = bs.spec_text(files)
        self.assertEqual(unknowns(text, files, hdr, info, tree), [])
        for body, why in (
                (b'rpm.define("_default_patch_fuzz 2")', 'rpm.define'),
                (b'rpm.undefine("x")', 'rpm.undefine'),
                (b'rpm.load(sourcedir .. "/x.mac")', 'rpm.load'),
                (b'macros._default_patch_fuzz = 2', 'macros.'),
                (b'macros["x"] = 1', 'macros['),
                (b'print("%global x 1")', '%global'),
                (b'print("%{define:x 1}")', '%{define'),
                (b"rpm.expand('%{load:' .. f .. '}')", '%{load:'),
                (b'print("%(touch x)")', '%('),
                (b'posix.setenv("POSIXLY_CORRECT", "1")', 'posix.setenv'),
                (b'posix.putenv("A=1")', 'posix.putenv'),
                (b'posix.unsetenv("LANG")', 'posix.unsetenv'),
                (b'table.insert(patches, "x.patch")', 'table.insert(patches'),
                (b'patch_nums[1] = 9', 'patch_nums[1] ='),
                (b'patches = {}', 'patches =')):
            with self.subTest(why=why):
                fs = dict(files, **{'patch-git.lua': files['patch-git.lua'] +
                                    b'\n' + body + b'\n'})
                got = unknowns(text, fs, hdr, info, tree)
                self.assertEqual(got, [('patch-git.lua', 'changes macros, the '
                                        'environment or the patches while rpm '
                                        'reads the spec: ' + why)])

    def test_a_patch_declared_after_prep(self):
        """%autosetup walks the patches when rpm parses %prep, so a later
        declaration is in the header and was never applied."""
        for text, why in (('%patchlist\nzz.patch', '%patchlist'),
                          ('%PatchList\nzz.patch', '%PatchList'),
                          ('%package late\nPatch9999: zz.patch', '%package'),
                          ('Patch9999: zz.patch', 'Patch9999:'),
                          ('%sourcelist\nzz.tar', '%sourcelist')):
            with self.subTest(why=why):
                got = self.got(lambda t: t.replace('\n%build\n', '\n%build\n'
                                                   + text + '\n', 1))
                self.assertTrue(got)
                self.assertTrue(any('a patch or a package declared after '
                                    '%prep: ' + why == w for _, w in got), got)

    def test_a_patchlist_in_any_case(self):
        for word in ('%patchlist', '%PatchList', '%PATCHLIST'):
            with self.subTest(word=word):
                self.one(self.before_prep(word + '\n%ifarch ppc64le\nzz.patch'
                                          '\n%endif'),
                         'a %' + word[1:] + ', which no real spec has')

    def test_every_way_to_define_a_macro(self):
        for line in ('%global zz 1', '%define zz 1', '%undefine zz',
                     '%{?rhel:%global zz 1}', '%{define:zz 1}',
                     '%{global:zz 1}', '%{undefine:zz}'):
            with self.subTest(line=line):
                self.one(self.before_prep(line),
                         'defines %zz, which no real spec does')

    def test_a_known_macro_given_a_second_value(self):
        self.one(self.before_prep('%global glibcsrcdir glibc-2.34'),
                 'does not unpack glibc-2.34.tar.xz the way the real specs')

    def test_a_macro_prep_uses_with_more_than_one_value(self):
        """The value %prep reads would depend on the arch."""
        def edit(t):
            t = t.replace('\n%prep\n', '\n%ifarch x86_64\n%global '
                          '_libdir /usr/lib64\n%endif\n%prep\n', 1)
            return self.after_autosetup('touch %{_libdir}')(t)
        got = self.got(edit)
        self.assertTrue(any('%_libdir has more than one value' in w
                            for _, w in got), got)

    def test_a_patch_line_under_a_conditional(self):
        self.one(self.before_prep('%if 0\nPatch9999: x.patch\n%endif'),
                 'a Patch line under a conditional')

    def test_a_prep_step_this_does_not_know(self):
        for line, why in (
                ('make -C localedata/unicode-gen install',
                 'line 2 of its body, make -C'),
                ('%patch1 -p1', 'macro %patch1, which this does not expand'),
                ('cp %{SOURCE9999} .', '%SOURCE9999 is not declared'),
                ('echo %{_libdir}', 'macro %_libdir, which this does not '
                                    'expand'),
                ('# %{_libdir}', 'macro %_libdir'),
                ("%{lua: print('rm -f localedata/locales/de_DE')}",
                 'a %{lua: block this does not know')):
            with self.subTest(line=line):
                got = self.got(self.after_autosetup(line))
                self.assertTrue(got)
                self.assertTrue(any(why in w for _, w in got), got)

    def test_a_known_line_whose_lua_or_source_changed(self):
        info, hdr, files, _, _, tree, _ = fixture(EL9)
        text = bs.spec_text(files)
        lua = text.replace('function print_locale_pairs()',
                           'function print_locale_pairs()\n  print("x")', 1)
        self.assertNotEqual(lua, text)
        got = self.got(lambda t: lua)
        self.assertTrue(any('a %{lua: block this does not know' in w
                            for _, w in got), got)
        src = dict(files)
        src['parse-SUPPORTED.py'] = files['parse-SUPPORTED.py'] + b'\n'
        got = unknowns(text, src, hdr, info, tree)
        self.assertEqual(len(got), 1, got)
        self.assertIn('%prep is not one the real specs have: line 9 of its '
                      'body, python3 @SRC:sha256=', got[0][1])

    def test_a_spec_without_a_whole_prep(self):
        for edit, why in (
                (lambda t: t.replace('\n%prep\n', '\n%prepx\n', 1),
                 'glibc.spec has 0 %prep sections'),
                (lambda t: t + '\n%prep\n', 'glibc.spec has 2 %prep sections'),
                (lambda t: t[:t.index('\n%prep\n') + 7] + 'x\n',
                 'nothing ends %prep')):
            with self.subTest(why=why):
                self.assertEqual(self.got(edit), [('glibc.spec', why)])

    def test_what_the_prep_lines_need_of_the_tree(self):
        self.one(lambda t: t, 'no locale/programs/*-kw.h for the touch',
                 lambda tr: {p: v for p, v in tr.items()
                             if not p.endswith('-kw.h')})

    def test_another_source0(self):
        self.one(lambda t: t.replace(bs._SOURCE0, bs._SOURCE0 + '.sig', 1),
                 'does not unpack glibc-2.34.tar.xz the way the real specs')

    def test_the_tarball_recorded_as_a_patch(self):
        info, hdr, files, _, _, tree, _ = fixture(EL9)
        hdr = dict(hdr)
        tar = 'glibc-2.34.tar.xz'
        hdr[bs._SOURCE] = [x for x in hdr[bs._SOURCE] if x != tar]
        hdr[bs._PATCH] = hdr[bs._PATCH] + [tar]
        got = unknowns(bs.spec_text(files), files, hdr, info, tree)
        self.assertEqual(len(got), 1, got)
        self.assertIn('does not unpack glibc-2.34.tar.xz', got[0][1])

    def test_a_doubled_percent_in_a_comment(self):
        """rpm reads %% as one %, in a comment too (the real specs write
        `# %%prep` for that reason)."""
        self.assertEqual(self.got(self.after_autosetup('# %%prep')), [])

    def test_a_macro_defined_inside_another_line(self):
        def edit(t):
            t = t.replace('\n%prep\n', '\n%{?rhel:%global glibc_release_url '
                          'x}\n%prep\n', 1)
            return self.after_autosetup('echo %{glibc_release_url}')(t)
        got = self.got(edit)
        self.assertTrue(any('%glibc_release_url has more than one value' in w
                            for _, w in got), got)


# -- what rpm recorded --------------------------------------------------------

class RecordsWhatRpmRecorded(unittest.TestCase):

    INFO = {'name': 'glibc', 'version': '2.39', 'release': '1.el10'}

    def hdr(self, patches=('b.patch', 'a.patch'), sources=('glibc-2.39.tar.xz',),
            ver='4.19.1.1'):
        return {bs._PATCH: list(patches), bs._SOURCE: list(sources),
                bs._RPMVERSION: ver}

    def files(self, *extra):
        names = ['glibc.spec', 'glibc-2.39.tar.xz', 'a.patch', 'b.patch']
        return {n: b'' for n in names + list(extra)}

    def refused(self, hdr, files, why):
        with self.assertRaises(bs.Refused) as cm:
            bs.rpm_record(self.INFO, hdr, files)
        self.assertIn(why, str(cm.exception))

    def test_the_order_is_the_list_reversed(self):
        r = bs.rpm_record(self.INFO, self.hdr(), self.files())
        self.assertEqual(r, bs.Record(['a.patch', 'b.patch'],
                                      ['glibc-2.39.tar.xz'], '4.19.1.1',
                                      'glibc-2.39'))

    def test_the_payload_is_what_the_header_records(self):
        self.refused(self.hdr(), self.files('c.patch'),
                     '1 file(s) more (c.patch), 0 fewer (-)')
        files = self.files()
        del files['a.patch']
        self.refused(self.hdr(), files, '0 file(s) more (-), 1 fewer (a.patch)')

    def test_what_the_header_cannot_mean(self):
        self.refused(self.hdr(patches=('a.patch', 'a.patch')), self.files(),
                     'names a.patch twice')
        self.refused(self.hdr(patches=('d/a.patch',)), self.files(),
                     'names a path')
        self.refused(self.hdr(sources=()), self.files(), 'records no Source')
        self.refused({bs._SOURCE: ['x'], bs._PATCH: 'a.patch'}, self.files(),
                     'records the patches unreadably')

    def test_an_rpm_this_does_not_know(self):
        self.refused(self.hdr(ver='4.20.0'), self.files(),
                     "written by rpm '4.20.0'")


# -- where a hunk lands: GNU patch 2.7.6, measured ---------------------------

def _case(lines, hunk):
    tree = {'localedata/x': (''.join(l + '\n' for l in lines).encode(), False)}
    data = ('diff --git a/localedata/x b/localedata/x\n'
            '--- a/localedata/x\n+++ b/localedata/x\n' + hunk).encode()
    return tree, data


def _where(tree):
    return [i + 1 for i, l in enumerate(tree['localedata/x'][0].split(b'\n'))
            if l in (b'Y', b'H1', b'H2', b'NEW')]


class PlacesAHunkWhereGnuPatchDoes(unittest.TestCase):
    """Each case was run through /usr/bin/patch -p1 -s --fuzz=0 (2.7.6, with
    rpm's flags) on Rocky 8, 9 and 10 on 2026-11-03; the lines are where
    the new text landed, 1-based."""

    def place(self, lines, hunk):
        tree, data = _case(lines, hunk)
        bs.apply_patch(tree, 'p.patch', data)
        return _where(tree)

    def test_a_tie_goes_after_before_before(self):
        f = ['u%d' % i for i in range(20)]
        f[7:10] = ['c', 'x', 'c']
        f[11:14] = ['c', 'x', 'c']
        self.assertEqual(self.place(f, '@@ -10,3 +10,3 @@\n c\n-x\n+Y\n c\n'),
                         [13])

    def test_a_hunk_never_lands_above_the_one_before(self):
        f = ['u%d' % i for i in range(70)]
        f[14:17] = ['d', 'z', 'd']
        f[50:53] = ['d', 'z', 'd']
        self.assertEqual(self.place(
            f, '@@ -20,3 +20,3 @@\n u19\n-u20\n+H1\n u21\n'
               '@@ -30,3 +30,3 @@\n d\n-z\n+H2\n d\n'), [21, 52])

    def test_the_bound_is_the_last_line_the_hunk_before_changed(self):
        """GNU never looks above the line after the last one the hunk
        before froze (min_where, patch.c:1142): the hunk before's trailing
        context is not frozen and can be matched again, its changed line
        cannot. Measured with GNU patch 2.7.6 on Rocky 8, 2026-11-03."""
        f = ['u%d' % i for i in range(60)]
        f[21:24] = ['k', 'm', 'k']
        f[45:48] = ['k', 'm', 'k']
        self.assertEqual(self.place(
            f, '@@ -20,3 +20,3 @@\n u19\n-u20\n+H1\n k\n'
               '@@ -32,3 +32,3 @@\n k\n-m\n+H2\n k\n'), [21, 23])
        f = ['u%d' % i for i in range(60)]
        f[21:23] = ['k', 'm']
        f[45:48] = ['NA', 'k', 'm']
        tree, data = _case(f, '@@ -20,3 +20,3 @@\n u19\n-u20\n+NA\n k\n'
                              '@@ -30,3 +30,3 @@\n NA\n-k\n+H2\n m\n')
        bs.apply_patch(tree, 'p.patch', data)
        self.assertEqual([i + 1 for i, l in enumerate(
            tree['localedata/x'][0].split(b'\n')) if l in (b'NA', b'H2')],
            [21, 46, 47])

    def test_no_context_after_means_the_end_of_the_file(self):
        f = ['u%d' % i for i in range(30)]
        f[10:13] = ['p', 'q', 'r']
        f[27:30] = ['p', 'q', 'r']
        self.assertEqual(self.place(f, '@@ -11,3 +11,4 @@\n p\n q\n r\n+NEW\n'),
                         [31])

    def test_no_context_before_means_the_start_of_the_file(self):
        f = ['u%d' % i for i in range(30)]
        f[0:3] = ['s', 't', 'v']
        f[5:8] = ['s', 't', 'v']
        self.assertEqual(self.place(f, '@@ -1,3 +1,4 @@\n+NEW\n s\n t\n v\n'),
                         [1])

    def test_an_anchored_hunk_that_moved_does_not_apply(self):
        cases = (
            (['u%d' % i for i in range(10)] + ['p', 'q', 'r', 't1', 't2'],
             '@@ -11,3 +11,4 @@\n p\n q\n r\n+NEW\n'),
            (['g1', 'g2', 's', 't', 'v'] + ['u%d' % i for i in range(20)],
             '@@ -1,3 +1,4 @@\n+NEW\n s\n t\n v\n'))
        for lines, hunk in cases:
            with self.subTest(hunk=hunk):
                tree, data = _case(lines, hunk)
                with self.assertRaises(bs.Refused) as cm:
                    bs.apply_patch(tree, 'p.patch', data)
                self.assertIn('does not apply to localedata/x', str(cm.exception))

    def test_a_context_line_that_differs_does_not_apply(self):
        """fuzz 0: rpm passes --fuzz=0 on el8, el9 and el10."""
        f = ['u%d' % i for i in range(10)]
        tree, data = _case(f, '@@ -4,3 +4,3 @@\n u3\n-u4\n+Y\n uX\n')
        with self.assertRaises(bs.Refused) as cm:
            bs.apply_patch(tree, 'p.patch', data)
        self.assertIn('does not apply', str(cm.exception))

    def test_a_zero_context_hunk_past_the_end(self):
        """E6: GNU patch appended it at the end of the file."""
        tree, data = _case(['a', 'b', 'c'], '@@ -9,0 +10,1 @@\n+Z\n')
        bs.apply_patch(tree, 'p.patch', data)
        self.assertEqual(tree['localedata/x'][0], b'a\nb\nc\nZ\n')


# -- how a patch is read, and what is refused --------------------------------

D = '2026-06-03 12:12:56.929422368 -0400'


class ReadsPatchesAsGnuPatchDoes(unittest.TestCase):
    """The shapes the seven real builds use on localedata/ and locale/ are
    applied; GNU patch's answer on the others (E1, E2, E3 and E5, measured
    with GNU patch 2.7.6 and rpm's flags on Rocky 9) is a tree this does not
    rebuild, so they are refused."""

    def unknown(self, data, why, name='p.patch'):
        got = bs.patch_unknowns(name, data)
        self.assertTrue(any(why in w for _, w in got), got)

    def test_the_known_shapes_apply(self):
        cases = {
            b'diff --git a/locale/x b/locale/x\nindex 1..2 100644\n'
            b'--- a/locale/x\n+++ b/locale/x\n@@ -1 +1 @@\n-a\n+b\n':
                {'locale/x': (b'b\n', False)},
            b'diff --git a/locale/n b/locale/n\nnew file mode 100755\n'
            b'index 0000000..1111111\n--- /dev/null\n+++ b/locale/n\n'
            b'@@ -0,0 +1 @@\n+x\n':
                {'locale/x': (b'a\n', False), 'locale/n': (b'x\n', True)},
            ('--- a/locale/x\t%s\n+++ b/locale/x\t%s\n@@ -1 +1 @@\n-a\n+b\n'
             % (D, D)).encode():
                {'locale/x': (b'b\n', False)},
            ('--- a/locale/n\t1970-01-01 00:00:00.000000000 +0000\n'
             '+++ b/locale/n\t%s\n@@ -0,0 +1 @@\n+x\n' % D).encode():
                {'locale/x': (b'a\n', False), 'locale/n': (b'x\n', False)},
            b'diff --git a/locale/e b/locale/e\nnew file mode 100644\n'
            b'index 0000000000000000..e69de29bb2d1d643\n':
                {'locale/x': (b'a\n', False), 'locale/e': (b'', False)}}
        for data, want in cases.items():
            with self.subTest(data=data[:40]):
                self.assertEqual(bs.patch_unknowns('p.patch', data), [])
                tree = {'locale/x': (b'a\n', False)}
                bs.apply_patch(tree, 'p.patch', data)
                self.assertEqual(tree, want)

    def test_gnus_e1_deletions(self):
        """E1a and E1b: GNU deleted the file (the +++ dated at the epoch);
        E1c: it left it empty."""
        for plus in ('1969-12-31 19:00:00.000000000 -0500',
                     '1970-01-01 00:00:00.000000000 +0000'):
            self.unknown(('--- a/localedata/x\t%s\n+++ b/localedata/x\t%s\n'
                          '@@ -1,3 +0,0 @@\n-a\n-b\n-c\n' % (D, plus)).encode(),
                         'a plain diff that deletes')
        self.unknown(b'--- a/localedata/x\n+++ b/localedata/x\n'
                     b'@@ -1,3 +0,0 @@\n-a\n-b\n-c\n',
                     'a plain diff without dates')
        self.unknown(b'diff --git a/locale/x b/locale/x\ndeleted file mode '
                     b'100644\nindex 1..0\n--- a/locale/x\n+++ /dev/null\n'
                     b'@@ -1 +0,0 @@\n-a\n', 'the git lines deleted file mode')

    def test_gnus_e2_e3_names_it_cleans(self):
        """GNU collapsed a// and a/./ and changed the file."""
        for pre in (b'a//', b'a/./'):
            data = (b'diff --git ' + pre + b'localedata/x ' + pre +
                    b'localedata/x\n--- ' + pre + b'localedata/x\n+++ ' +
                    pre.replace(b'a', b'b') + b'localedata/x\n'
                    b'@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n')
            with self.subTest(pre=pre):
                self.unknown(data, 'this does not know')
                with self.assertRaises(bs.Refused):
                    bs.apply_patch({'localedata/x': (b'a\nb\nc\n', False)},
                                   'p.patch', data)

    def test_gnus_e5_an_indented_quoted_diff(self):
        data = (b'Subject: x\n\n    This quotes:\n\n    --- a/localedata/x\n'
                b'    +++ b/localedata/x\n    @@ -1,3 +1,3 @@\n     a\n'
                b'    -b\n    +B\n     c\n\n---\ndiff --git a/y b/y\n'
                b'new file mode 100644\n--- /dev/null\n+++ b/y\n'
                b'@@ -0,0 +1 @@\n+y\n')
        self.unknown(data, 'line 5 reads to GNU patch as a header')

    def test_every_header_gnu_reads(self):
        """intuit_diff_type's starts, after blanks, tabs, X's and RFC 934's
        `- ` (pch.c:518, :549-830, :703-705@2.7.6)."""
        for head in (b'--- a/locale/x', b'+++ b/locale/x', b'*** a/locale/x',
                     b'Index: locale/x', b'  diff --git a/locale/x b/locale/y',
                     b'Prereq: locale/x', b'\tX --- a/locale/x',
                     b'- - --- a/locale/x', b'--- a/intl/locale.alias',
                     b'--- a/string/strcoll_l.c', b'+++ b/wcsmbs/wcsxfrm.c'):
            with self.subTest(head=head):
                self.unknown(b'Subject: x\n\n' + head + b'\n',
                             'line 3 reads to GNU patch as a header')

    def test_lines_inside_a_hunk_are_not_headers(self):
        data = (b'diff --git a/elf/x b/elf/x\n--- a/elf/x\n+++ b/elf/x\n'
                b'@@ -1,2 +1,2 @@\n --- a/locale/x\n-a\n+b\n')
        self.assertEqual(bs.patch_unknowns('p.patch', data), [])

    def test_shapes_this_does_not_take(self):
        cases = {
            b'diff --git a/locale/x b/locale/x\nold mode 100644\n'
            b'new mode 100755\n': 'the git lines old mode',
            b'diff --git a/locale/x b/locale/y\nsimilarity index 100%\n'
            b'rename from locale/x\nrename to locale/y\n':
                'a git header naming two paths',
            ('--- a/intl/x\t%s\n+++ b/locale/x\t%s\n@@ -0,0 +1 @@\n+b\n'
             % (D, D)).encode(): 'a ---/+++ pair this does not read',
            b'diff --git a/locale/x b/locale/x c/y\nnew file mode 100644\n':
                'a git header naming two paths or none',
            ('--- "a/locale/x"\t%s\n+++ "b/locale/x"\t%s\n@@ -0,0 +1 @@\n+b\n'
             % (D, D)).encode(): 'this does not know',
            ('--- a/locale/../x\t%s\n+++ b/locale/../x\t%s\n@@ -0,0 +1 @@\n'
             '+b\n' % (D, D)).encode(): 'this does not know',
            b'diff --git a/locale/x b/locale/x\nGIT binary patch\n':
                'the git lines GIT binary patch',
            b'diff --git a/locale/x b/locale/x\nindex 1..2\n':
                'a git section with no diff',
            b'diff --git a/locale/e b/locale/e\nnew file mode 100644\n'
            b'index 0000000..1111111\n': 'a git section with no diff',
            b'diff --git a/locale/x b/locale/x\n--- a/locale/y\n'
            b'+++ b/locale/x\n@@ -1 +1 @@\n-a\n+b\n':
                'do not name locale/x as git writes',
            ('--- a/locale/x\t1970-01-01 00:00:00.000000000 +0000\n'
             '+++ b/locale/x\t%s\n@@ -1 +1 @@\n-a\n+b\n' % D).encode():
                'a plain diff from the epoch that is not one creating hunk'}
        for data, why in cases.items():
            with self.subTest(why=why, data=data[:30]):
                self.unknown(data, why)

    def test_a_broken_hunk(self):
        for data, why in (
                (b'diff --git a/locale/x b/locale/x\n--- a/locale/x\n'
                 b'+++ b/locale/x\n@@ -1,2 +1,2 @@\n a\n*b\n', 'bad hunk line'),
                (b'diff --git a/locale/x b/locale/x\n--- a/locale/x\n'
                 b'+++ b/locale/x\n@@ -1,3 +1,3 @@\n a\n', 'hunk cut short')):
            with self.subTest(why=why):
                self.unknown(data, why)
                with self.assertRaises(bs.Refused) as cm:
                    bs.apply_patch({'locale/x': (b'a\nb\n', False)}, 'p.patch',
                                   data)
                self.assertIn(why, str(cm.exception))

    def test_a_hunk_header_outside_a_section(self):
        self.unknown(b'some text\n@@ -1 +1 @@\n-a\n+b\n',
                     'hunk headers this does not read')

    def test_an_index_line_beside_a_section_on_the_folders(self):
        self.unknown(b'Index: elf/x\n' + _case([], '@@ -0,0 +1 @@\n+b\n')[1],
                     'an Index: line beside')

    def test_creating_a_file_that_exists(self):
        """el10's rpm passes -f, and GNU patch then writes over it."""
        tree = {'locale/x': (b'a\n', False)}
        with self.assertRaises(bs.Refused) as cm:
            bs.apply_patch(tree, 'p.patch',
                           b'diff --git a/locale/x b/locale/x\nnew file mode '
                           b'100644\n--- /dev/null\n+++ b/locale/x\n'
                           b'@@ -0,0 +1 @@\n+b\n')
        self.assertIn('creates locale/x, which exists', str(cm.exception))

    def test_changing_a_file_that_is_not_there(self):
        with self.assertRaises(bs.Refused) as cm:
            bs.apply_patch({}, 'p.patch', _case([], '@@ -1 +1 @@\n-a\n+b\n')[1])
        self.assertIn('changes localedata/x, which is not there',
                      str(cm.exception))

    def test_out_of_scope_sections_are_left_alone(self):
        tree = {'locale/x': (b'a\n', False)}
        data = (b'diff --git a/elf/a b/elf/b\nsimilarity index 100%\n'
                b'rename from elf/a\nrename to elf/b\n'
                b'--- a/string/x\n+++ b/string/x\n@@ -1 +1 @@\n-q\n+r\n')
        self.assertEqual(bs.patch_unknowns('p.patch', data), [])
        self.assertEqual(bs.apply_patch(tree, 'p.patch', data), ([], []))
        self.assertEqual(tree, {'locale/x': (b'a\n', False)})

    def test_a_patch_to_strcolls_code_is_named(self):
        data = (b'diff --git a/string/strcoll_l.c b/string/strcoll_l.c\n'
                b'--- a/string/strcoll_l.c\n+++ b/string/strcoll_l.c\n'
                b'@@ -1 +1 @@\n-a\n+b\n')
        self.assertEqual(bs.apply_patch({}, 'p.patch', data),
                         ([], ['string/strcoll_l.c']))
        self.unknown(data.replace(b'a/string', b'a//string'),
                     'this does not know')

    def test_what_rpm_would_unpack(self):
        """rpmFileIsCompressed's magics at 4.14.3, 4.16.1.3 and 4.19.1.1;
        a file under 13 bytes is plain to it."""
        body = b'--- a/locale/x\n+++ b/locale/x\n@@ -1 +1 @@\n-a\n+b\n'
        cases = ((gzip.compress(body), 'gzip'), (lzma.compress(body), 'xz'),
                 (b'BZh91AY&SY' + body, 'bzip2'), (b'PK\x03\x04' + body, 'zip'),
                 (b'PK00' + body, 'zip'), (b'\x28\xb5\x2f\xfd' + body, 'zstd'),
                 (b'\x28\x85\x2f' + body, 'zstd'), (b'LZIP' + body, 'lzip'),
                 (b'LRZI' + body, 'lrzip'), (b'\x1f\x9d' + body, 'compress'),
                 (b'\x1f\x1e' + body, 'pack'), (b'\x1f\xa0' + body, 'lzh'),
                 (b'\x1f\x9e' + body, 'gzip'),
                 (b'7z\xbc\xaf\x27\x1c' + body, '7zip'))
        for data, why in cases:
            with self.subTest(why=why, start=data[:4]):
                self.unknown(data, 'rpm would unpack it as')
                self.assertIn(why, bs.rpm_compression('p.patch', data))
        self.assertEqual(bs.rpm_compression('p.lzma', body), 'lzma')
        self.assertEqual(bs.rpm_compression('p.gem', body), 'gem')
        self.assertIsNone(bs.rpm_compression('p.patch', b'BZh'))
        self.assertIsNone(bs.rpm_compression('p.patch', body))


class ReadsATagOfTheClone(unittest.TestCase):

    def test_a_link_under_the_folders(self):
        out = (b'100644 blob ' + b'1' * 40 + b'\tlocale/a\0'
               b'120000 blob ' + b'2' * 40 + b'\tlocale/b\0')
        fake = mock.Mock(stdout=out)
        with mock.patch.object(bs.g, 'run_git', return_value=fake):
            with self.assertRaises(bs.Refused) as cm:
                bs.scope_from_git('/nowhere', 'glibc-2.39')
        self.assertIn('locale/b', str(cm.exception))
        self.assertIn('not a regular file', str(cm.exception))


# -- rpm files made by the tests ---------------------------------------------

def _hdr(entries):
    """An rpm header from (tag, type, value): type 6 a string, 8 a string
    array (value a list), 4 an int32 array (value a list)."""
    store, index = bytearray(), b''
    for tag, typ, value in entries:
        if typ == 4:
            while len(store) % 4:
                store.append(0)
            off = len(store)
            store.extend(struct.pack('>%dI' % len(value), *value))
            cnt = len(value)
        elif typ == 6:
            off = len(store)
            store.extend(value.encode() + b'\0')
            cnt = 1
        else:
            off = len(store)
            for v in value:
                store.extend(v.encode() + b'\0')
            cnt = len(value)
        index += struct.pack('>IIII', tag, typ, off, cnt)
    return (b'\x8e\xad\xe8\x01\x00\x00\x00\x00' +
            struct.pack('>II', len(entries), len(store)) + index + bytes(store))


def _cpio(files, nlink=1, mode=0o100644, twice=None, dirs=()):
    out = bytearray()

    def pad():
        while len(out) % 4:
            out.append(0)
    pairs = [(n, b'', 0o040755) for n in dirs] + \
        [(n, d, mode) for n, d in files.items()] + \
        ([(twice, files[twice], mode)] if twice else [])
    for name, data, m in pairs + [('TRAILER!!!', b'', 0)]:
        n = name.encode() + b'\0'
        fld = [0, m, 0, 0, nlink, 0, len(data), 0, 0, 0, 0, len(n), 0]
        out += b'070701' + b''.join(b'%08X' % x for x in fld)
        out += n
        pad()
        out += data
        pad()
    return bytes(out)


def _rpm(path, main_entries, payload, lead_type, header_sha=None):
    main = _hdr(main_entries)
    sig = _hdr([(273, 6, header_sha or hashlib.sha256(main).hexdigest())])
    sig += b'\0' * ((8 - len(sig) % 8) % 8)
    lead = b'\xed\xab\xee\xdb\x03\x00\x00' + bytes([lead_type]) + b'\0' * 88
    with open(path, 'wb') as fh:
        fh.write(lead + sig + main + payload)


def make_srpm(path, files, compressor='gzip', payload_sha=None,
              header_sha=None, name='glibc', lead_type=1, fmt='cpio',
              raw=None, patches=None, sources=None, rpm_version='4.19.1.1',
              **cpio):
    payload = gzip.compress(raw if raw is not None else _cpio(files, **cpio))
    if patches is None:
        patches = sorted((n for n in files if n.endswith('.patch')),
                         reverse=True)
    if sources is None:
        sources = sorted(n for n in files
                         if n != 'glibc.spec' and n not in patches)
    _rpm(path, [(1000, 6, name), (1001, 6, '2.39'), (1002, 6, '1.el10'),
                (1018, 8, sources), (1019, 8, patches),
                (1064, 6, rpm_version), (1124, 6, fmt), (1125, 6, compressor),
                (5092, 8, [payload_sha or
                           hashlib.sha256(payload).hexdigest()]),
                (5093, 4, [8])], payload, lead_type, header_sha)


LS = '/usr/share/i18n/locales/'
CM = '/usr/share/i18n/charmaps/'
ALIAS = b'# locale.alias of the tag\n'


def make_binary(path, name, files, compressor='xz', version='2.39',
                release='1.el10', sourcerpm='glibc-2.39-1.el10.src.rpm',
                rpm_version='4.19.1.1', algo=8, digests=None,
                payload_sha=None, alt_sha=None, header_sha=None, cut=0,
                files_in_header=None):
    """A binary package installing `files` ({installed path: bytes}); the
    header lists files_in_header (default the same) with their sha256."""
    listed = files if files_in_header is None else files_in_header
    data = _cpio({'.' + p: b for p, b in files.items()},
                 dirs=['./usr/share/i18n'])
    comp = {'xz': lzma.compress, 'gzip': gzip.compress,
            'zstd': lambda d: b'\x28\xb5\x2f\xfd' + d,
            'zstd-bad': lambda d: b'not zstd' + d}
    payload = comp[compressor](data)
    paths = sorted(listed)
    dirs = sorted(set(os.path.dirname(p) + '/' for p in paths))
    dig = [hashlib.sha256(listed[p]).hexdigest() for p in paths]
    if digests:
        dig = [digests.get(p, d) for p, d in zip(paths, dig)]
    entries = [(1000, 6, name), (1001, 6, version), (1002, 6, release),
               (1035, 8, dig), (1044, 6, sourcerpm), (1064, 6, rpm_version),
               (1116, 4, [dirs.index(os.path.dirname(p) + '/')
                          for p in paths]),
               (1117, 8, [os.path.basename(p) for p in paths]),
               (1118, 8, dirs), (1124, 6, 'cpio'),
               (1125, 6, compressor.replace('-bad', '')),
               (5011, 4, [algo]),
               (5092, 8, [payload_sha or hashlib.sha256(payload).hexdigest()]),
               (5093, 4, [8])]
    if compressor.startswith('zstd'):
        entries.append((5097, 8, [alt_sha or hashlib.sha256(data).hexdigest()]))
    _rpm(path, entries, payload[:len(payload) - cut], 0, header_sha)
    return path


def locale_source(tree):
    out = {}
    for p, (data, _) in tree.items():
        if p.startswith('localedata/locales/'):
            out[LS + p[len('localedata/locales/'):]] = data
        elif p.startswith('localedata/charmaps/'):
            out[CM + p[len('localedata/charmaps/'):] + '.gz'] = \
                gzip.compress(data, mtime=0)
    return out


TREE = {'localedata/locales/xx': (b'a\nB\nc\n', False),
        'localedata/charmaps/CM': (b'<code_set_name> CM\n', False),
        'locale/programs/run.sh': (b'x\n', True)}
INFO = {'name': 'glibc', 'version': '2.39', 'release': '1.el10'}


class VerifiesAgainstTheBinaries(unittest.TestCase):
    """verify_against_binaries and its four parts on packages the test
    makes; the real headers are in RebuildsTheFixtureBuildsAsRpmDid."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='build_sources_bin_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def pkg(self, name='glibc-locale-source', files=None, **kw):
        if files is None:
            files = (locale_source(TREE) if name == 'glibc-locale-source'
                     else {'/usr/share/locale/locale.alias': ALIAS})
        return bs.read_binary(make_binary(os.path.join(self.tmp, name + '.rpm'),
                                          name, files, **kw))

    def verify(self, tree=TREE, alias=ALIAS, **binaries):
        b = {'glibc-locale-source': self.pkg(), 'glibc-common':
             self.pkg('glibc-common')}
        b.update(binaries)
        return bs.verify_against_binaries(tree, alias, INFO, b)

    def refused(self, why, **kw):
        with self.assertRaises(bs.Refused) as cm:
            self.verify(**kw)
        self.assertIn(why, str(cm.exception))

    def test_what_is_checked(self):
        self.assertEqual(self.verify(), [
            'locale sources: 1, each the sha256 glibc-locale-source-2.39-'
            '1.el10 records',
            'charmaps: 1, each the one glibc-locale-source-2.39-1.el10 '
            'installs',
            'locale.alias: the sha256 glibc-common-2.39-1.el10 records'])

    def test_a_package_missing(self):
        for name in ('glibc-locale-source', 'glibc-common'):
            with self.subTest(name=name):
                self.refused('no %s package of glibc-2.39-1.el10' % name,
                             **{name: None})

    def test_a_package_of_another_build(self):
        cases = ((dict(release='2.el10'), 'is glibc-locale-source-2.39-2.el10'),
                 (dict(sourcerpm='glibc-2.39-2.el10.src.rpm'),
                  "built from 'glibc-2.39-2.el10.src.rpm'"),
                 (dict(rpm_version='4.20.0'), "built by rpm '4.20.0'"))
        for kw, why in cases:
            with self.subTest(why=why):
                self.refused(why, **{'glibc-locale-source': self.pkg(**kw)})
        self.refused("is 'glibc-common', not glibc-locale-source",
                     **{'glibc-locale-source': self.pkg('glibc-common')})

    def test_a_package_rpm_would_not_read(self):
        path = os.path.join(self.tmp, 'p.rpm')
        cases = ((dict(header_sha='0' * 64), "the header's SHA256"),
                 (dict(cut=5), "the payload's SHA256"),
                 (dict(payload_sha='0' * 64), "the payload's SHA256"),
                 (dict(algo=1), 'file digests are not sha256'))
        for kw, why in cases:
            with self.subTest(why=why):
                with self.assertRaises(bs.Refused) as cm:
                    b = bs.read_binary(make_binary(path, 'glibc-locale-source',
                                                   locale_source(TREE), **kw))
                    bs.check_locales(TREE, b)
                self.assertIn(why, str(cm.exception))
        _rpm(path, [(1000, 6, 'x')], b'', 1)
        with self.assertRaises(bs.Refused) as cm:
            bs.read_binary(path)
        self.assertIn('is not a binary rpm', str(cm.exception))

    def test_locale_sources_that_differ(self):
        ls = locale_source(TREE)
        cases = ((dict(digests={LS + 'xx': '0' * 64}),
                  'locale sources: 1 differ from glibc-locale-source-2.39-'
                  '1.el10, e.g. xx'),
                 (dict(digests={LS + 'xx': ''}), 'which is not a plain file'),
                 (dict(files_in_header=dict(ls, **{LS + 'yy': b'y'})),
                  'installs 1 that the rebuilt tree lacks (yy)'),
                 (dict(files_in_header={p: b for p, b in ls.items()
                                        if p != LS + 'xx'}),
                  'the tree has 1 it does not install (xx)'))
        for kw, why in cases:
            with self.subTest(why=why):
                self.refused(why, **{'glibc-locale-source': self.pkg(**kw)})

    def test_charmaps_that_differ(self):
        ls = locale_source(TREE)
        cases = ((dict(ls, **{CM + 'CM.gz': gzip.compress(b'other')}),
                  'charmaps: 1 differ from'),
                 (dict(ls, **{CM + 'DD.gz': gzip.compress(b'd')}),
                  'installs 1 that the rebuilt tree lacks (DD)'),
                 ({p: b for p, b in ls.items() if not p.startswith(CM)},
                  'the tree has 1 it does not install (CM)'),
                 (dict(ls, **{CM + 'EE': b'e'}), 'which is not a gzipped'),
                 (dict(ls, **{CM + 'EE.gz': b'not gzip'}),
                  'does not decompress'))
        for files, why in cases:
            with self.subTest(why=why):
                self.refused(why, **{'glibc-locale-source': self.pkg(
                    files=files, files_in_header=ls)})

    def test_another_locale_alias(self):
        self.refused("installs another than the tag's", alias=b'other')
        self.refused("installs another than the tag's", alias=None)
        self.refused('installs 0 of it', **{'glibc-common': self.pkg(
            'glibc-common', files={'/usr/share/locale/x': b''})})

    def test_without_a_zstd_reader(self):
        """Without a zstd reader the charmaps are named not checked,
        the rest is checked, and the payload's digest still holds."""
        with mock.patch.object(bs, '_unzstd', return_value=None):
            ls = self.pkg(compressor='zstd')
            self.assertIsNone(ls.files)
            got = self.verify(**{'glibc-locale-source': ls})
            self.assertEqual(got[1], '!! charmaps NOT checked against '
                             'glibc-locale-source-2.39-1.el10: ' + bs.NO_ZSTD)
            self.assertTrue(got[0].startswith('locale sources: 1,'))
            with self.assertRaises(bs.Refused) as cm:
                self.pkg(compressor='zstd', cut=3)
            self.assertIn("the payload's SHA256", str(cm.exception))

    def test_with_a_zstd_reader(self):
        def unz(data):
            return data[4:]
        with mock.patch.object(bs, '_unzstd', side_effect=unz):
            ls = self.pkg(compressor='zstd')
            self.assertEqual(self.verify(**{'glibc-locale-source': ls})[1],
                             'charmaps: 1, each the one glibc-locale-source-'
                             '2.39-1.el10 installs')
            with self.assertRaises(bs.Refused) as cm:
                self.pkg(compressor='zstd', alt_sha='0' * 64)
            self.assertIn("decompressed payload's SHA256", str(cm.exception))

    def test_a_payload_that_is_not_what_its_header_says(self):
        with mock.patch.object(bs, '_unzstd', return_value=None):
            with self.assertRaises(bs.Refused) as cm:
                self.pkg(compressor='zstd-bad')
            self.assertIn('is not a zstd frame', str(cm.exception))
        with self.assertRaises(bs.Refused) as cm:
            self.pkg(compressor='gzip')
        self.assertIn("payload compressed with 'gzip'", str(cm.exception))

    def test_the_zstd_program_when_python_has_no_reader(self):
        """Python before 3.14 has no compression.zstd; then the zstd
        program, if there is one, else nothing."""
        prog = os.path.join(self.tmp, 'zstd')
        with open(prog, 'w') as fh:
            fh.write('#!/bin/sh\n[ "$1" = -dc ] || exit 9\nexec cat\n')
        os.chmod(prog, 0o755)
        bad = os.path.join(self.tmp, 'zstd-bad')
        with open(bad, 'w') as fh:
            fh.write('#!/bin/sh\necho broken >&2\nexit 1\n')
        os.chmod(bad, 0o755)
        with mock.patch.dict('sys.modules', {'compression': None}):
            with mock.patch.object(bs.shutil, 'which', return_value=None):
                self.assertIsNone(bs._unzstd(b'data'))
            with mock.patch.object(bs.shutil, 'which', return_value=prog):
                self.assertEqual(bs._unzstd(b'data'), b'data')
            with mock.patch.object(bs.shutil, 'which', return_value=bad):
                with self.assertRaises(bs.Refused) as cm:
                    bs._unzstd(b'data')
                self.assertIn('zstd could not read a payload: broken',
                              str(cm.exception))

    def test_the_cache_key_follows_the_list_of_strcoll_s_code(self):
        """The list (ENTRY_POINTS, TIER1, TIER2 and the four wrappers) is
        in build_sources.py since step 9.1, and the key hashes that file:
        a kept tree is made again when the list changes, and not when
        another script does. A key change
        rebuilds a build already fetched from its kept SRPM and downloads
        nothing (measured with the network refused: 1.9 s for
        glibc-2.34-275.el9_8, handoff-9-2026-11-06/paso-9.1); a build
        looked up in the mirror's metadata again reads primary.xml again
        once, since the metadata is kept under the key too."""
        tmp = tempfile.mkdtemp(prefix='pg-glibc-key-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in os.listdir(SCRIPTS_DIR):
            if name.endswith('.py'):
                shutil.copy(os.path.join(SCRIPTS_DIR, name), tmp)

        def key():
            p = subprocess.run(
                [sys.executable, '-B', '-c', 'import sys; sys.path.insert('
                 '0, %r); import build_sources; print(build_sources._key())'
                 % tmp], capture_output=True, timeout=120, check=True)
            return p.stdout.decode('ascii').strip()

        def edit(name, old, new):
            path = os.path.join(tmp, name)
            with open(path) as fh:
                text = fh.read()
            self.assertEqual(1, text.count(old), (name, old))
            with open(path, 'w') as fh:
                fh.write(text.replace(old, new))
        base = key()
        self.assertEqual(bs._key(), base)
        edit('glibc_locale_data.py', "SUPPORTED = 'localedata/SUPPORTED'\n",
             "SUPPORTED = 'localedata/SUPPORTED'\n# another line\n")
        self.assertEqual(base, key())
        edit('build_sources.py', "    'locale/programs/ld-ctype.c',\n]",
             "    'locale/programs/ld-ctype.c',\n    'locale/x.c',\n]")
        self.assertNotEqual(base, key())


# -- opening a source RPM ----------------------------------------------------

SPEC = """%define glibcsrcdir glibc-2.39
Name: glibc
Source0: %{{?glibc_release_url}}%{{glibcsrcdir}}.tar.xz
Patch1: one.patch
{extra}
%description
x
%prep
%autosetup -n %{{glibcsrcdir}} -p1
touch locale/programs/*-kw.h

%build
"""


SMALL_TREE = (('glibc-2.39/localedata/locales/xx', b'a\nb\nc\n'),
              ('glibc-2.39/localedata/charmaps/CM', b'<code_set_name> CM\n'),
              ('glibc-2.39/locale/programs/run.sh', b'x\n'),
              ('glibc-2.39/locale/programs/locfile-kw.h', b'k\n'),
              ('glibc-2.39/intl/locale.alias', ALIAS),
              ('glibc-2.39/elf/z', b'q\n'))


def small_tarball(members=SMALL_TREE):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:xz') as t:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ONE = (b'diff --git a/localedata/locales/xx b/localedata/locales/xx\n'
       b'--- a/localedata/locales/xx\n+++ b/localedata/locales/xx\n'
       b'@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n')


def small_srpm_files(extra=''):
    return {'glibc.spec': SPEC.format(extra=extra).encode(),
            'glibc-2.39.tar.xz': small_tarball(), 'one.patch': ONE}


SMALL_KEYS = (bs._AUTOSETUP, 'touch locale/programs/*-kw.h')
BUILT = {'localedata/locales/xx': (b'a\nB\nc\n', False),
         'localedata/charmaps/CM': (b'<code_set_name> CM\n', False)}


class OpensASourceRpm(unittest.TestCase):
    """build() on a small SRPM and its two binary packages made by the
    test, with two of known_only's catalogues set to the small spec's
    own (it has no Lua): the real catalogues are tested on the real specs
    above."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='build_sources_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, 'cache')
        for name, value in (('PREP_SEQUENCES', frozenset([SMALL_KEYS])),
                            ('MACRO_NAMES', frozenset(['glibcsrcdir']))):
            p = mock.patch.object(bs, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.bins = {
            'glibc-locale-source': make_binary(
                os.path.join(self.tmp, 'ls.rpm'), 'glibc-locale-source',
                locale_source(BUILT)),
            'glibc-common': make_binary(
                os.path.join(self.tmp, 'common.rpm'), 'glibc-common',
                {'/usr/share/locale/locale.alias': ALIAS})}

    def srpm(self, files=None, **kw):
        path = os.path.join(self.tmp, 'g.src.rpm')
        make_srpm(path, files or small_srpm_files(), **kw)
        return path

    def kept(self):
        return [d for d in os.listdir(self.cache) if not d.startswith('.new-')] \
            if os.path.isdir(self.cache) else []

    def refused(self, path, why, bins=None):
        with self.assertRaises(bs.Refused) as cm:
            bs.build(path, bins or self.bins, cache=self.cache)
        self.assertIn(why, str(cm.exception))
        self.assertEqual(self.kept(), [])

    def test_builds_checks_and_keeps_the_tree(self):
        b = bs.build(self.srpm(), self.bins, cache=self.cache)
        self.assertEqual((b.build, b.version, b.tag),
                         ('glibc-2.39-1.el10.src', 'glibc 2.39', 'glibc-2.39'))
        with open(os.path.join(b.tree, 'localedata/locales/xx'), 'rb') as fh:
            self.assertEqual(fh.read(), b'a\nB\nc\n')
        self.assertTrue(os.access(os.path.join(b.tree, 'locale/programs/run.sh'),
                                  os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(b.tree, 'elf')))
        self.assertFalse(os.path.exists(os.path.join(b.tree, 'intl')))
        self.assertEqual(len(b.provenance['verified']), 3)
        self.assertEqual(b.provenance['order_rule'],
                         "the SRPM header's PATCH list, reversed")
        with mock.patch.object(bs, 'read_srpm', side_effect=AssertionError):
            self.assertEqual(bs.build(self.srpm(), self.bins,
                                      cache=self.cache).tree, b.tree)

    def test_the_binaries_are_part_of_what_is_kept(self):
        """Another glibc-common is another check: built again."""
        b = bs.build(self.srpm(), self.bins, cache=self.cache)
        other = dict(self.bins, **{'glibc-common': make_binary(
            os.path.join(self.tmp, 'common2.rpm'), 'glibc-common',
            {'/usr/share/locale/locale.alias': ALIAS,
             '/usr/share/locale/x': b''})})
        with mock.patch.object(bs, 'verify_against_binaries',
                               side_effect=bs.Refused('checked again')):
            with self.assertRaises(bs.Refused):
                bs.build(self.srpm(), other, cache=self.cache)
            self.assertEqual(bs.build(self.srpm(), self.bins,
                                      cache=self.cache).tree, b.tree)

    def test_a_tree_the_binaries_disagree_with_is_not_kept(self):
        files = small_srpm_files()
        files['one.patch'] = ONE.replace(b'+B', b'+Q')
        self.refused(self.srpm(files), 'locale sources: 1 differ from '
                     'glibc-locale-source-2.39-1.el10, e.g. xx')

    def test_no_binary_package(self):
        for name in ('glibc-locale-source', 'glibc-common'):
            with self.subTest(name=name):
                self.refused(self.srpm(), 'no %s package given' % name,
                             dict(self.bins, **{name: None}))

    def test_a_damaged_kept_tree_is_built_again(self):
        b = bs.build(self.srpm(), self.bins, cache=self.cache)
        os.remove(os.path.join(b.tree, 'localedata/locales/xx'))
        b2 = bs.build(self.srpm(), self.bins, cache=self.cache)
        self.assertTrue(os.path.exists(os.path.join(b2.tree,
                                                    'localedata/locales/xx')))

    def test_a_kept_tree_that_does_not_read_back(self):
        with mock.patch.object(bs, '_kept', return_value=None):
            self.refused(self.srpm(), 'does not read back whole')

    def test_a_wrong_payload_digest(self):
        self.refused(self.srpm(payload_sha='0' * 64), "payload's SHA256")

    def test_a_wrong_header_digest(self):
        self.refused(self.srpm(header_sha='0' * 64), "header's SHA256")

    def test_a_spec_that_unpacks_another_tree(self):
        files = small_srpm_files()
        files['glibc.spec'] = files['glibc.spec'].replace(b'glibc-2.39',
                                                          b'glibc-2.38', 1)
        self.refused(self.srpm(files), 'does not unpack glibc-2.39.tar.xz')

    def test_a_prep_that_does_not_open_with_rpms_autosetup(self):
        files = small_srpm_files()
        files['glibc.spec'] = files['glibc.spec'].replace(b'-p1', b'-p1 -S git')
        self.refused(self.srpm(files), '%prep: macro %autosetup')

    def test_a_compressor_python_cannot_read(self):
        self.refused(self.srpm(compressor='zstd'), "compressed with 'zstd'")

    def test_hardlinks_in_the_payload(self):
        self.refused(self.srpm(nlink=2), 'hardlinks')

    def test_a_link_in_the_payload(self):
        self.refused(self.srpm(mode=0o120777), 'not a regular file')

    def test_two_tarballs(self):
        files = small_srpm_files()
        files['other.tar.gz'] = gzip.compress(b'')
        self.refused(self.srpm(files), 'expected one tarball')

    def test_a_link_in_the_tarball(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:xz') as t:
            info = tarfile.TarInfo('glibc-2.39/localedata/locales/xx')
            info.type, info.linkname = tarfile.SYMTYPE, 'yy'
            t.addfile(info)
        files = small_srpm_files()
        files['glibc-2.39.tar.xz'] = buf.getvalue()
        self.refused(self.srpm(files), 'a link or a special file')

    def test_a_prep_step_that_rewrites_a_locale(self):
        files = small_srpm_files()
        files['glibc.spec'] = files['glibc.spec'].replace(
            b'-p1\n', b"-p1\nsed -i 's/a/b/' localedata/locales/xx\n")
        self.refused(self.srpm(files), "%prep is not one the real specs "
                     "have: line 2 of its body, sed -i 's/a/b/' "
                     "localedata/locales/xx")

    def test_what_is_not_a_glibc_source_rpm(self):
        cases = ((dict(name='bash'), "is 'bash', not glibc"),
                 (dict(lead_type=0), 'not a source rpm'),
                 (dict(fmt='drpm'), "payload format 'drpm'"),
                 (dict(raw=b'0707'), 'cut short'),
                 (dict(raw=b'x' * 120), 'not a newc cpio archive'),
                 (dict(twice='one.patch'), "holds 'one.patch' twice"),
                 (dict(rpm_version='4.11.3'), "written by rpm '4.11.3'"),
                 (dict(patches=['one.patch', 'two.patch']),
                  '1 fewer (two.patch)'))
        for kw, why in cases:
            with self.subTest(why=why):
                self.refused(self.srpm(**kw), why)

    def test_a_tarball_this_does_not_take(self):
        cases = ((SMALL_TREE + (('other/localedata/x', b''),),
                  'unexpected tarball member'),
                 ((('glibc-2.39/elf/z', b'q\n'),), 'holds no localedata/'))
        for members, why in cases:
            with self.subTest(why=why):
                files = small_srpm_files()
                files['glibc-2.39.tar.xz'] = small_tarball(members)
                self.refused(self.srpm(files), why)

    def test_a_patch_to_strcolls_code_is_listed(self):
        files = small_srpm_files()
        files['one.patch'] += (b'diff --git a/string/strcoll_l.c '
                               b'b/string/strcoll_l.c\n'
                               b'--- a/string/strcoll_l.c\n'
                               b'+++ b/string/strcoll_l.c\n'
                               b'@@ -1 +1 @@\n-a\n+b\n')
        b = bs.build(self.srpm(files), self.bins, cache=self.cache)
        self.assertEqual(b.provenance['collation_code_patches'],
                         [{'patch': 'one.patch',
                           'paths': ['string/strcoll_l.c']}])

    def test_a_patch_that_does_not_apply(self):
        files = small_srpm_files()
        files['one.patch'] = ONE.replace(b'-b', b'-Q')
        self.refused(self.srpm(files), 'does not apply')

    def test_a_patch_rpm_would_unpack(self):
        files = small_srpm_files()
        files['one.patch'] = lzma.compress(ONE)
        self.refused(self.srpm(files), 'rpm would unpack it as xz')


# -- a mirror made by the tests -----------------------------------------------

M = 'https://mirror.test/'
NSC, NSR = ('http://linux.duke.edu/metadata/common',
            'http://linux.duke.edu/metadata/repo')


def _row(name, arch, rel, data=b'x', ver='2.39', href=None, sourcerpm=None,
         sha=None, size=None):
    """One package of a primary.xml, as Rocky writes it."""
    fn = '%s-%s-%s.%s.rpm' % (name, ver, rel, arch)
    return {'name': name, 'arch': arch, 'ver': ver, 'rel': rel,
            'sha': sha or hashlib.sha256(data).hexdigest(),
            'size': len(data) if size is None else size,
            'href': href or 'Packages/g/' + fn,
            'sourcerpm': '' if arch == 'src' else
            (sourcerpm or 'glibc-%s-%s.src.rpm' % (ver, rel))}


def _primary(rows):
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<metadata xmlns="%s" '
           'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="%d">'
           % (NSC, len(rows) + 1),
           '<package type="rpm"><name>bash</name><arch>x86_64</arch>'
           '<version epoch="0" ver="5" rel="1"/></package>']
    for r in rows:
        out.append(
            '<package type="rpm"><name>%(name)s</name><arch>%(arch)s</arch>'
            '<version epoch="0" ver="%(ver)s" rel="%(rel)s"/>'
            '<checksum type="sha256" pkgid="YES">%(sha)s</checksum>'
            '<size package="%(size)s" installed="0" archive="0"/>'
            '<location href="%(href)s"/><format><rpm:sourcerpm>%(sourcerpm)s'
            '</rpm:sourcerpm></format></package>' % r)
    out.append('</metadata>\n')
    return gzip.compress(''.join(out).encode(), mtime=0)


def _repomd(primary, sha=None, kinds=('primary',)):
    """repomd.xml naming `primary`; primary_db always first, as in every
    real one."""
    data = ['<data type="primary_db"><checksum type="sha256">%s</checksum>'
            '<location href="repodata/db-primary.sqlite.xz"/></data>'
            % ('1' * 64)]
    for k in kinds:
        data.append('<data type="%s"><checksum type="sha256">%s</checksum>'
                    '<location href="repodata/%s-PRIMARY.xml.gz"/>'
                    '<timestamp>1762000000</timestamp></data>'
                    % (k, sha or hashlib.sha256(primary).hexdigest(), 'u'))
    return ('<?xml version="1.0"?>\n<repomd xmlns="%s"><revision>10'
            '</revision>%s</repomd>' % (NSR, ''.join(data))).encode()


class FakeMirror(object):
    """A mirror as a dict of URL to bytes, served in place of build_sources
    ._get: a URL it does not hold answers 404, one in `errors` raises what
    is there, and every URL asked is in `asked`."""

    def __init__(self, vault=()):
        self.files, self.errors, self.asked = {}, {}, []
        self.files[M + 'vault/rocky/'] = (
            '<a href="../">../</a>' + ''.join(
                '<a href="%s/">%s/</a>' % (v, v) for v in vault)).encode()

    def get(self, url):
        self.asked.append(url)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.files:
            raise bs._Absent(url)
        return self.files[url]

    def repo(self, tree, repo, rows, files=(), sha=None, kinds=('primary',)):
        base = M + tree + '/' + repo + '/'
        prim = _primary(rows)
        self.files[base + 'repodata/repomd.xml'] = _repomd(prim, sha, kinds)
        self.files[base + 'repodata/u-PRIMARY.xml.gz'] = prim
        for href, data in files:
            self.files[base + href] = data
        return base

    def copy(self, old, new, move=False):
        """Every URL under `old` also under `new` (and no longer under
        `old` with move): a build moved to the vault, or another mirror."""
        for url in [u for u in self.files if u.startswith(old)]:
            self.files[new + url[len(old):]] = self.files[url]
            if move:
                del self.files[url]


class FetchesFromAMirror(unittest.TestCase):
    """where_from, fetch, fetch_build and latest on a mirror the test makes,
    shaped as Rocky's was measured on 2026-11-03 (two package layouts,
    primary_db before primary, a vault folder with no metadata, 8.4-RC1/ and
    9.0-ISO/ in the vault's index, pub/rocky/8's SRPMs in the order its
    primary lists them). No test reads a real mirror: _get is the fake's for
    every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='build_sources_fetch_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, 'cache')
        self.mirror = FakeMirror(vault=('9.2', '9.3', '9.4', '8.4', '8.4-RC1',
                                        '9.0-ISO', '10.0'))
        p = mock.patch.object(bs, '_get', side_effect=self.mirror.get)
        p.start()
        self.addCleanup(p.stop)
        for name, value in (('PREP_SEQUENCES', frozenset([SMALL_KEYS])),
                            ('MACRO_NAMES', frozenset(['glibcsrcdir']))):
            p = mock.patch.object(bs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def refused(self, call, why):
        with self.assertRaises(bs.Refused) as cm:
            call()
        assertRefusedAs(self, cm.exception, why)

    def where(self, name):
        return bs.where_from(name, M, self.cache)

    def el10(self, srpm_rel='1.el10', binaries=None, listed='1.el10'):
        """pub/rocky/10 holding the small SRPM and its two binary packages,
        the SRPM listed under the release `listed`."""
        srpm = os.path.join(self.tmp, 'g.src.rpm')
        make_srpm(srpm, small_srpm_files())
        ls = make_binary(os.path.join(self.tmp, 'ls.rpm'),
                         'glibc-locale-source', locale_source(BUILT))
        common = make_binary(os.path.join(self.tmp, 'common.rpm'),
                             'glibc-common',
                             {'/usr/share/locale/locale.alias': ALIAS})
        data = {}
        for k, path in (('srpm', srpm), ('ls', ls), ('common', common)):
            with open(path, 'rb') as fh:
                data[k] = fh.read()
        s = _row('glibc', 'src', listed, data['srpm'])
        l_ = _row('glibc-locale-source', 'x86_64', listed, data['ls'])
        c = _row('glibc-common', 'x86_64', listed, data['common'])
        self.mirror.repo('pub/rocky/10', 'BaseOS/source/tree', [s],
                         [(s['href'], data['srpm'])])
        self.mirror.repo('pub/rocky/10', 'AppStream/x86_64/os', [l_],
                         [(l_['href'], data['ls'])])
        self.mirror.repo('pub/rocky/10', 'BaseOS/x86_64/os', [c],
                         [(c['href'], data['common'])])
        self.placed = {'srpm': ('BaseOS/source/tree', s),
                       'glibc-locale-source': ('AppStream/x86_64/os', l_),
                       'glibc-common': ('BaseOS/x86_64/os', c)}
        return srpm, {'glibc-locale-source': ls, 'glibc-common': common}

    def el8(self, rels, tree='pub/rocky/8', href=None):
        """Rows for each release in `rels`, in the order given, in the three
        repositories of `tree` (el8: both binaries in BaseOS)."""
        src, bins = [], []
        for rel in rels:
            src.append(_row('glibc', 'src', rel, ver='2.28', href=href and
                            href % ('glibc-2.28-%s.src' % rel)))
            for n in ('glibc-common', 'glibc-locale-source'):
                bins.append(_row(n, 'x86_64', rel, ver='2.28'))
        self.mirror.repo(tree, 'BaseOS/source/tree', src)
        self.mirror.repo(tree, 'BaseOS/x86_64/os', bins)
        self.mirror.repo(tree, 'AppStream/x86_64/os', [])
        return src, bins

    # The real order of pub/rocky/8's 17 releases in its primary.xml.
    EL8 = ['251.el8_10.%d' % n for n in (11, 13, 14, 16, 2, 22, 25, 27, 31,
                                          34, 37, 38, 4, 40, 43, 48, 5)]

    def test_fetches_the_three_files_and_builds_as_build_does(self):
        """The control: what fetch_build returns is what build() returns
        for the same three files."""
        srpm, bins = self.el10()
        got = bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        want = bs.build(srpm, bins, os.path.join(self.tmp, 'other'))
        self.assertEqual((got.build, got.tag), (want.build, want.tag))
        self.assertEqual(got.provenance['verified'],
                         want.provenance['verified'])
        files = got.provenance['fetched']['files']
        self.assertEqual(sorted(files), sorted(p[0] for p in bs.PACKAGES))
        self.assertEqual(files['glibc-locale-source']['repo'],
                         'AppStream/x86_64/os')
        self.assertEqual(got.provenance['fetched']['note'], bs.FETCHED_NOTE)

    def test_a_file_that_is_not_the_one_the_metadata_records(self):
        """Same size, other bytes; and a sha256 that agrees in its first
        eight digits only: nothing is kept, nothing is returned."""
        data = b'A' * 100
        url = M + 'pub/rocky/10/BaseOS/x86_64/os/Packages/g/f.rpm'
        sha = hashlib.sha256(data).hexdigest()
        cases = ((b'B' * 100, sha, 100, 'sha256'),
                 (data, sha[:8] + '0' * 56, 100, 'sha256'),
                 (data[:99], sha, 100, '99 bytes'))
        for served, want, size, why in cases:
            with self.subTest(why=why, want=want[:10]):
                self.mirror.files[url] = served
                entry = bs.Entry(url, want, size, 't', 'r', None)
                self.refused(lambda: bs.fetch(entry, self.cache), why)
                self.assertFalse(os.path.exists(
                    os.path.join(self.cache, 'downloads', want)))

    def test_a_primary_that_is_not_the_one_repomd_records(self):
        self.el10()
        base = M + 'pub/rocky/10/BaseOS/source/tree/'
        self.mirror.files[base + 'repodata/repomd.xml'] = _repomd(
            b'', sha=hashlib.sha256(b'another').hexdigest())
        self.refused(lambda: self.where('glibc-2.39-1.el10'),
                     'is not the primary.xml repomd.xml records')

    def test_repomd_must_name_one_primary(self):
        self.el10()
        base = M + 'pub/rocky/10/BaseOS/source/tree/'
        self.mirror.files[base + 'repodata/repomd.xml'] = _repomd(
            self.mirror.files[base + 'repodata/u-PRIMARY.xml.gz'],
            kinds=('primary', 'primary'))
        self.refused(lambda: self.where('glibc-2.39-1.el10'),
                     'names 2 primary.xml')

    def test_a_name_is_read_whole(self):
        """A release that begins another release's name is that build and
        no other: .4 beside .40 and .48, and glibc-2.39-46.el10_0.4, whose
        last word locale_order.build_parts would drop."""
        self.el8(self.EL8)
        got = self.where('glibc-2.28-251.el8_10.4')
        for k, e in got.items():
            self.assertIn('-2.28-251.el8_10.4.', e.url)
        rows = [_row('glibc', 'src', r) for r in ('46.el10_0', '46.el10_0.4')]
        self.mirror.repo('vault/rocky/10.0', 'BaseOS/source/tree', rows)
        self.mirror.repo('vault/rocky/10.0', 'BaseOS/x86_64/os', [
            _row('glibc-common', 'x86_64', r) for r in ('46.el10_0',
                                                        '46.el10_0.4')])
        self.mirror.repo('vault/rocky/10.0', 'AppStream/x86_64/os', [
            _row('glibc-locale-source', 'x86_64', r)
            for r in ('46.el10_0', '46.el10_0.4')])
        self.mirror.repo('pub/rocky/10', 'BaseOS/source/tree', [])
        got = self.where('glibc-2.39-46.el10_0.4')
        self.assertTrue(got['srpm'].url.endswith('glibc-2.39-46.el10_0.4.'
                                                 'src.rpm'))
        self.assertEqual(got['srpm'].tree, 'vault/rocky/10.0')

    def test_names_that_are_not_build_names(self):
        for name in ('glibc-2.34-283.el9_8.x86_64', 'glibc-2.34',
                     'glibc-2.34-283', 'bash-5.1-1.el9', '', None,
                     'glibc-2.34-283.el9_8 glibc-2.34-275.el9_8'):
            with self.subTest(name=name):
                self.refused(lambda: self.where(name),
                             'is not a glibc build name')
        self.assertFalse(self.mirror.asked)

    def test_the_build_fetched_must_be_the_one_asked_for(self):
        """The mirror lists the small SRPM, whose header says 1.el10, under
        another release."""
        self.el10(listed='1.el10_0.4')
        self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10_0.4', M,
                                            self.cache),
                     'asked for glibc-2.39-1.el10_0.4 and the SRPM fetched '
                     'is glibc-2.39-1.el10.src')

    def test_the_binaries_are_this_builds(self):
        """Another build's package listed first is passed over; a package
        named as this build but built from another SRPM is refused."""
        src, bins = self.el8(['251.el8_10.40', '251.el8_10.48'])
        got = self.where('glibc-2.28-251.el8_10.48')
        self.assertIn('glibc-common-2.28-251.el8_10.48.x86_64',
                      got['glibc-common'].url)
        bins[2]['sourcerpm'] = 'glibc-2.28-251.el8_10.40.src.rpm'
        self.mirror.repo('pub/rocky/8', 'BaseOS/x86_64/os', bins)
        self.refused(lambda: self.where('glibc-2.28-251.el8_10.48'),
                     "is built from 'glibc-2.28-251.el8_10.40.src.rpm'")

    def test_a_package_missing_or_listed_twice(self):
        src, bins = self.el8(['251.el8_10.48'])
        self.mirror.repo('pub/rocky/8', 'BaseOS/x86_64/os', bins[:1])
        self.refused(lambda: self.where('glibc-2.28-251.el8_10.48'),
                     'lists 0 glibc-locale-source.x86_64 of '
                     'glibc-2.28-251.el8_10.48')
        self.mirror.repo('pub/rocky/8', 'BaseOS/x86_64/os',
                         bins + bins[:1])
        self.refused(lambda: self.where('glibc-2.28-251.el8_10.48'),
                     'lists 2 glibc-common.x86_64')
        self.refused(lambda: self.where('glibc-2.28-251.el8_10.99'),
                     'glibc-2.28-251.el8_10.99 is not in')
        for field, value in (('sha', 'abc'), ('size', 'x'),
                             ('href', '../g.rpm')):
            with self.subTest(field=field):
                bad = [dict(b) for b in bins]
                bad[0][field] = value
                self.mirror.repo('pub/rocky/8', 'BaseOS/x86_64/os', bad)
                self.refused(lambda: self.where('glibc-2.28-251.el8_10.48'),
                             'has no sha256, size or path this reads')

    def test_a_folder_with_no_metadata(self):
        """vault 9.3's shape: SRPMs under Packages/g/, no repodata. Its
        build is refused by name; a build of a lower folder is still found
        past it."""
        for v in ('9', '9.4'):
            self.mirror.repo('pub/rocky/' + v if v == '9' else
                             'vault/rocky/' + v, 'BaseOS/source/tree', [])
        self.mirror.files[M + 'vault/rocky/9.3/BaseOS/source/tree/'
                          'Packages/g/'] = \
            b'<a href="glibc-2.34-83.el9.12.src.rpm">x</a>'
        self.refused(lambda: self.where('glibc-2.34-83.el9.12'),
                     'glibc-2.34-83.el9.12.src.rpm is published in '
                     'https://mirror.test/vault/rocky/9.3 with no metadata')
        rows = [_row('glibc', 'src', '60.el9', ver='2.34')]
        self.mirror.repo('vault/rocky/9.2', 'BaseOS/source/tree', rows)
        self.mirror.repo('vault/rocky/9.2', 'BaseOS/x86_64/os',
                         [_row('glibc-common', 'x86_64', '60.el9',
                               ver='2.34')])
        self.mirror.repo('vault/rocky/9.2', 'AppStream/x86_64/os',
                         [_row('glibc-locale-source', 'x86_64', '60.el9',
                               ver='2.34')])
        self.assertEqual(self.where('glibc-2.34-60.el9')['srpm'].tree,
                         'vault/rocky/9.2')

    def test_the_flat_layout_of_8_3_and_8_4(self):
        self.mirror.repo('pub/rocky/8', 'BaseOS/source/tree', [])
        self.el8(['151.el8'], tree='vault/rocky/8.4', href='Packages/%s.rpm')
        got = self.where('glibc-2.28-151.el8')
        self.assertEqual(got['srpm'].url, M + 'vault/rocky/8.4/BaseOS/source/'
                         'tree/Packages/glibc-2.28-151.el8.src.rpm')

    def test_release_candidates_and_iso_folders_are_not_trees(self):
        self.mirror.repo('pub/rocky/8', 'BaseOS/source/tree', [])
        self.mirror.repo('vault/rocky/8.4', 'BaseOS/source/tree', [])
        self.el8(['151.el8'], tree='vault/rocky/8.4-RC1')
        self.refused(lambda: self.where('glibc-2.28-151.el8'), 'is not in')
        self.assertFalse([u for u in self.mirror.asked
                          if 'RC1' in u or 'ISO' in u])

    def test_a_failure_is_not_an_absence(self):
        """A 503 or a timeout on the folder that holds the build refuses
        with what happened, never "not there"."""
        self.el10()
        url = M + 'pub/rocky/10/BaseOS/source/tree/repodata/repomd.xml'
        self.mirror.errors[url] = bs.Refused('%s: HTTP 503' % url)
        self.refused(lambda: self.where('glibc-2.39-1.el10'), 'HTTP 503')

    def test_nothing_is_built_after_a_failure(self):
        """Every refusal comes before build(): no tree from anything else,
        the upstream tag included."""
        self.el10()
        del self.mirror.files[M + 'pub/rocky/10/AppStream/x86_64/os/'
                              'repodata/repomd.xml']
        with mock.patch.object(bs, 'build', side_effect=AssertionError), \
                mock.patch.object(bs, 'scope_from_git',
                                  side_effect=AssertionError):
            self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', M,
                                                self.cache),
                         'AppStream/x86_64/os has no metadata to find')

    def test_the_latest_build_of_a_major(self):
        """In the real order of pub/rocky/8 the newest is .48: not the first
        row, not the last, not the greatest as text."""
        self.el8(self.EL8)
        got = bs.latest(8, M, self.cache)
        self.assertEqual(got.build, 'glibc-2.28-251.el8_10.48')
        self.assertEqual((got.tree, got.timestamp), ('pub/rocky/8',
                                                     '1762000000'))
        src = [_row('glibc', 'src', '1.el8', ver='2.28'),
               _row('glibc', 'src', '1.el8', ver='2.29'),
               _row('glibc', 'src', '9.el9', ver='2.34')]
        self.mirror.repo('pub/rocky/8', 'BaseOS/source/tree', src)
        self.refused(lambda: bs.latest(8, M, self.cache),
                     'lists glibc 2.28, 2.29 for el8')

    def test_rows_kept_whole_or_read_again(self):
        self.el8(['251.el8_10.48'])
        self.where('glibc-2.28-251.el8_10.48')
        kept = os.listdir(os.path.join(self.cache, 'metadata'))
        self.assertEqual(len(kept), 3)
        for name in kept:
            path = os.path.join(self.cache, 'metadata', name)
            with open(path) as fh:
                value = json.load(fh)
            value.pop('complete')
            with open(path, 'w') as fh:
                json.dump(value, fh)
        self.mirror.asked[:] = []
        self.where('glibc-2.28-251.el8_10.48')
        self.assertEqual(len([u for u in self.mirror.asked
                              if u.endswith('PRIMARY.xml.gz')]), 3)

    def test_once_fetched_no_network_and_a_damaged_file_again(self):
        self.el10()
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        self.mirror.asked[:] = []
        with mock.patch.object(bs, '_get', side_effect=AssertionError):
            bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        dl = os.path.join(self.cache, 'downloads')
        victim = sorted(os.listdir(dl))[0]
        with open(os.path.join(dl, victim), 'r+b') as fh:
            fh.write(b'\0')
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        self.assertEqual(len(self.mirror.asked), 1)
        self.assertIn(victim, [hashlib.sha256(d).hexdigest()
                               for u, d in self.mirror.files.items()
                               if u == self.mirror.asked[0]])

    def test_a_listed_file_that_is_not_there_is_unread(self):
        """The metadata lists the build, and its SRPM is gone from the
        mirror (a sync half done, a build moved): nothing was found wrong
        in what was read, so the build is Unread."""
        self.el10()
        repo, row = self.placed['srpm']
        del self.mirror.files[M + 'pub/rocky/10/' + repo + '/' + row['href']]
        self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', M,
                                            self.cache),
                     'is not there any more')

    def test_a_remembered_file_the_mirror_now_gives_another_sha256(self):
        """Each of the three files in turn: its kept copy and its URL gone,
        the metadata now names another sha256 for it."""
        for key in [p[0] for p in bs.PACKAGES]:
            with self.subTest(key=key):
                cache = os.path.join(self.tmp, 'c-' + key)
                self.el10()
                bs.fetch_build('glibc-2.39-1.el10', M, cache)
                shutil.rmtree(os.path.join(cache, 'downloads'))
                repo, row = self.placed[key]
                del self.mirror.files[M + 'pub/rocky/10/' + repo + '/' +
                                      row['href']]
                self.mirror.repo('pub/rocky/10', repo,
                                 [dict(row, sha='0' * 64)])
                self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', M,
                                                    cache),
                             'now gives %s of glibc-2.39-1.el10 another '
                             'sha256 than the one remembered from %s'
                             % (key, M))

    def test_a_remembered_build_that_moved_to_the_vault(self):
        """At a point release a build moves from pub/ to the vault with
        the same files: it is found there, and remembered there."""
        self.el10()
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        shutil.rmtree(os.path.join(self.cache, 'downloads'))
        self.mirror.copy(M + 'pub/rocky/10/', M + 'vault/rocky/10.0/',
                         move=True)
        self.mirror.repo('pub/rocky/10', 'BaseOS/source/tree', [])
        # A CDN can answer a removed object with 403 rather than 404: a
        # failure, which also sends the run back to the metadata.
        repo, row = self.placed['srpm']
        url = M + 'pub/rocky/10/' + repo + '/' + row['href']
        self.mirror.errors[url] = bs.Refused('%s: HTTP 403' % url)
        got = bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        self.assertEqual({f['tree'] for f in
                          got.provenance['fetched']['files'].values()},
                         {'vault/rocky/10.0'})
        with open(os.path.join(self.cache, 'builds',
                               'glibc-2.39-1.el10.json')) as fh:
            rec = json.load(fh)
        self.assertTrue(all('/vault/rocky/10.0/' in f['url']
                            for f in rec['files'].values()))

    def test_a_primary_repomd_names_that_is_not_there(self):
        """A mirror in the middle of a sync: every entry point refuses
        with what happened, never "not there"."""
        self.el10()
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        shutil.rmtree(os.path.join(self.cache, 'downloads'))
        shutil.rmtree(os.path.join(self.cache, 'metadata'))
        for u in [u for u in self.mirror.files
                  if u.endswith('PRIMARY.xml.gz')]:
            del self.mirror.files[u]
        why = 'is named by repomd.xml and is not there'
        self.refused(lambda: self.where('glibc-2.39-1.el10'), why)
        self.refused(lambda: bs.latest(10, M, self.cache), why)
        # The remembered path reads the metadata again when a kept URL
        # fails, as when a build moves to the vault.
        del self.mirror.files[[u for u in self.mirror.files
                               if '/Packages/g/glibc-common-' in u][0]]
        self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', M,
                                            self.cache), why)

    def test_a_vault_index_this_cannot_search(self):
        self.mirror.repo('pub/rocky/9', 'BaseOS/source/tree', [])
        index = M + 'vault/rocky/'
        self.mirror.files[index] = b'<a href="/vault/rocky/9.2/">9.2/</a>'
        self.refused(lambda: self.where('glibc-2.34-60.el9_2.7'),
                     'lists no release folder this reads')
        del self.mirror.files[index]
        self.refused(lambda: self.where('glibc-2.34-60.el9_2.7'),
                     'vault/rocky/ is not there')

    def test_a_listing_that_cannot_be_read(self):
        """The Packages/ listing of a folder with no metadata failing is a
        failure, not "not published there"."""
        for tree in ('pub/rocky/9', 'vault/rocky/9.4', 'vault/rocky/9.2'):
            self.mirror.repo(tree, 'BaseOS/source/tree', [])
        url = M + 'vault/rocky/9.3/BaseOS/source/tree/Packages/g/'
        self.mirror.errors[url] = bs.Refused('%s: HTTP 503' % url)
        self.refused(lambda: self.where('glibc-2.34-83.el9.12'), 'HTTP 503')

    def test_a_record_that_cannot_be_read_is_not_a_first_time(self):
        self.el10()
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        path = os.path.join(self.cache, 'builds', 'glibc-2.39-1.el10.json')
        with open(path) as fh:
            good = fh.read()
        rec = json.loads(good)
        rec['files']['another'] = rec['files']['srpm']
        for text, why in ((json.dumps(rec), 'does not hold what this '
                           'remembers'), ('', 'cannot be read')):
            with self.subTest(why=why):
                with open(path, 'w') as fh:
                    fh.write(text)
                self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', M,
                                                    self.cache),
                             path + ' ' + why)
        os.remove(path)
        os.mkdir(path)
        self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', M,
                                            self.cache),
                     path + ' cannot be read')
        os.rmdir(path)
        with open(path, 'w') as fh:
            fh.write(good)
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)

    def test_another_mirror_is_asked_again(self):
        """Another mirror named: its metadata is read, and it must give the
        sha256 the first mirror gave; then it is the one remembered."""
        other = 'https://other.test/'
        self.el10()
        bs.fetch_build('glibc-2.39-1.el10', M, self.cache)
        self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', other,
                                            self.cache),
                     other + 'vault/rocky/ is not there')
        self.mirror.copy(M, other)
        repo, row = self.placed['srpm']
        base = other + 'pub/rocky/10/' + repo + '/'
        prim = _primary([dict(row, sha='0' * 64)])
        self.mirror.files[base + 'repodata/repomd.xml'] = _repomd(prim)
        self.mirror.files[base + 'repodata/u-PRIMARY.xml.gz'] = prim
        self.refused(lambda: bs.fetch_build('glibc-2.39-1.el10', other,
                                            self.cache),
                     '%s now gives srpm of glibc-2.39-1.el10 another sha256 '
                     'than the one remembered from %s' % (other, M))
        self.mirror.copy(M, other)
        bs.fetch_build('glibc-2.39-1.el10', other, self.cache)
        with open(os.path.join(self.cache, 'builds',
                               'glibc-2.39-1.el10.json')) as fh:
            self.assertEqual(json.load(fh)['mirror'], other)


class ReadsAUrl(unittest.TestCase):
    """_get, the one place that reads a URL: what each answer becomes. The
    URLs here are file://, answered by a stand-in, or refused by urllib
    before any network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='build_sources_get_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_file_url(self):
        path = os.path.join(self.tmp, 'f')
        with open(path, 'wb') as fh:
            fh.write(b'bytes')
        self.assertEqual(bs._get('file://' + path), b'bytes')
        with self.assertRaises(bs.Refused):
            bs._get('file://' + path + '-missing')

    def test_what_each_answer_becomes(self):
        import urllib.error

        class Resp(io.BytesIO):
            def __init__(self, data, length):
                io.BytesIO.__init__(self, data)
                self.headers = {'Content-Length': length}

        def err(code):
            return urllib.error.HTTPError('u', code, 'x', {}, None)
        cases = ((err(404), bs._Absent, ''),
                 (err(503), bs.Refused, 'HTTP 503'),
                 (urllib.error.URLError('timed out'), bs.Unread, 'timed out'),
                 (Resp(b'abc', '10'), bs.Refused, 'cut short: 3 of 10'))
        for answer, exc, why in cases:
            with self.subTest(why=why or '404'):
                kw = {'return_value': answer} if isinstance(answer, Resp) \
                    else {'side_effect': answer}
                with mock.patch.object(bs.urllib.request, 'urlopen', **kw):
                    with self.assertRaises(exc) as cm:
                        bs._get('https://mirror.test/x')
                self.assertIs(type(cm.exception), exc)
                self.assertIn(why, str(cm.exception))
        with mock.patch.object(bs.urllib.request, 'urlopen',
                               return_value=Resp(b'abc', '3')):
            self.assertEqual(bs._get('https://mirror.test/x'), b'abc')
        # A mirror named without its scheme: urllib cannot open it. The
        # mirror named is wrong, which is a refusal, not "cannot be
        # reached" (Unread): audit.sh does not read the tags over it.
        with self.assertRaises(bs.Refused) as cm:
            bs._get('dl.rockylinux.org/vault/rocky/')
        self.assertIs(type(cm.exception), bs.Refused)
        self.assertIn('unknown url type', str(cm.exception))

    def test_the_suite_names_a_mirror_that_is_not_the_network(self):
        mirror = os.environ.get('PG_GLIBC_BUILD_MIRROR', '')
        self.assertTrue(mirror.startswith('file://'), mirror)
        self.assertEqual(bs._mirror(None), mirror.rstrip('/') + '/')


# -- a build as a commit of the clone, and as a side -------------------------

TAG = 'glibc-2.34'
B275, B283 = 'glibc-2.34-275.el9_8', 'glibc-2.34-283.el9_8'


def _git_out(repo, *args):
    return subprocess.run(['git', '-C', repo] + list(args), check=True,
                          capture_output=True).stdout.decode().strip()


def _linked(src, dst, change=None):
    """`src` at `dst` as hard links (a copy of a thousand files cost most of
    these classes' time), with `change` ({path: bytes}) written over it: each
    changed file unlinked first, so that the shared one never changes."""
    shutil.copytree(src, dst, copy_function=os.link)
    for path, data in (change or {}).items():
        full = os.path.join(dst, path)
        mode = os.stat(full).st_mode
        os.remove(full)
        with open(full, 'wb') as fh:
            fh.write(data)
        os.chmod(full, mode & 0o777)
    return dst


def _built(tree, name, tarball=True):
    """A Built for the build `name` whose tree is the folder `tree`: what
    fetch_build returns, without an SRPM."""
    prov = {'srpm_sha256': hashlib.sha256(name.encode()).hexdigest(),
            'verified': ['locale sources: 1, each the sha256 '
                         'glibc-locale-source records'],
            'applied': [], 'patches': 0, 'collation_code_patches': []}
    if tarball:
        prov['tarball'] = bs.TARBALL_IS_TAG % TAG
    return bs.Built(tree, name + '.src', 'glibc 2.34', TAG, prov)


@needs_clone
class ABuildIsACommitOfTheClone(unittest.TestCase):
    """build_commit: one parent, the upstream tag; the tag's tree with
    localedata/ and locale/ the build's; made again on every call, and its
    ref (build_ref, outside refs/tags) pointed at what was made, whatever
    was there. Each test works in a repository of its own that reads
    scripts/glibc's objects (glibc_shaped) and writes nothing there."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='build_commit_')
        base, _ = bs.scope_from_git(GLIBC_CLONE, TAG)
        cls.base = os.path.join(cls.tmp, 'base')
        for path, (data, exe) in base.items():
            full = os.path.join(cls.base, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as fh:
                fh.write(data)
            os.chmod(full, 0o755 if exe else 0o644)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bc_', dir=self.tmp)
        self.repo = os.path.join(self.dir, 'repo')
        glibc_shaped(self.repo, TAG)

    def tree(self, label, change=None):
        """A copy of the tag's localedata/ and locale/, with `change`
        ({path: bytes}) written over it."""
        return _linked(self.base, os.path.join(self.dir, label), change)

    def blob(self, commit, path):
        return _git_out(self.repo, 'rev-parse', '%s:%s' % (commit, path))

    def test_one_parent_the_tag_and_only_its_scope_changed(self):
        sv = 'localedata/locales/sv_SE'
        built = _built(self.tree('t', {sv: b'changed\n'}), B275)
        commit = bs.build_commit(self.repo, built)
        tag = _git_out(self.repo, 'rev-parse', TAG + '^{commit}')
        self.assertEqual(_git_out(self.repo, 'rev-list', '--parents', '-n',
                                  '1', commit).split(), [commit, tag])
        self.assertEqual(_git_out(self.repo, 'diff-tree', '-r',
                                  '--name-only', tag, commit), sv)
        self.assertEqual(self.blob(commit, sv), _git_out(
            self.repo, 'hash-object', os.path.join(built.tree, sv)))
        self.assertEqual(_git_out(self.repo, 'rev-parse',
                                  'refs/pg-glibc-audit/builds/' + B275),
                         commit)
        # describe reads refs/tags only: the build is not a release.
        self.assertEqual(_git_out(self.repo, 'describe', '--tags',
                                  '--abbrev=0', '--match', 'glibc-[0-9]*',
                                  commit), TAG)

    def test_a_ref_already_there_is_replaced_never_read(self):
        """An older run's commit of another tree, and a ref set by hand to
        the tag, both under the build's name: the commit made now is
        returned and the ref points at it."""
        ref = bs.build_ref(B275)
        older = bs.build_commit(self.repo, _built(
            self.tree('old', {'localedata/locales/sv_SE': b'old\n'}), B275))
        now = bs.build_commit(self.repo, _built(self.tree('now'), B275))
        self.assertNotEqual(now, older)
        self.assertEqual(_git_out(self.repo, 'rev-parse', ref), now)
        _git_out(self.repo, 'update-ref', ref, TAG + '^{commit}')
        self.assertEqual(bs.build_commit(self.repo, _built(
            self.tree('again'), B275)), now)
        self.assertEqual(_git_out(self.repo, 'rev-parse', ref), now)

    def test_another_tree_under_one_name_is_another_commit(self):
        sv = 'localedata/locales/sv_SE'
        one = bs.build_commit(self.repo, _built(self.tree('a'), B275))
        two = bs.build_commit(self.repo, _built(
            self.tree('b', {sv: b'another\n'}), B275))
        self.assertNotEqual(one, two)
        self.assertNotEqual(self.blob(one, sv), self.blob(two, sv))

    def test_two_builds_of_one_tree_are_two_commits_of_one_tree(self):
        """275 and 283 of el9 have one tree (measured): two commits, so the
        two are never "the same commit", and nothing_compared says 'tree'."""
        import glibc_locale_data as g
        a = bs.build_commit(self.repo, _built(self.tree('a'), B275))
        b = bs.build_commit(self.repo, _built(self.tree('b'), B283))
        self.assertNotEqual(a, b)
        self.assertEqual(g.nothing_compared(self.repo, a, b), 'tree')

    def test_the_readers_git_config_does_not_change_the_commit(self):
        """i18n.commitEncoding writes an encoding header into a commit, and
        a clean filter in the attributes rewrites every file it hashes;
        neither reaches the build's commit."""
        clean = bs.build_commit(self.repo, _built(self.tree('a'), B275))
        other = os.path.join(self.dir, 'hostile')
        glibc_shaped(other, TAG)
        _git_out(other, 'config', 'i18n.commitEncoding', 'ISO-8859-1')
        _git_out(other, 'config', 'filter.up.clean', 'tr a-z A-Z')
        with open(os.path.join(other, '.git', 'info', 'attributes'),
                  'w') as fh:
            fh.write('* filter=up\n')
        # The control: the config does change what a plain hash gives.
        sample = os.path.join(self.base, 'localedata/locales/sv_SE')
        self.assertNotEqual(
            _git_out(other, 'hash-object', '--path',
                     'localedata/locales/sv_SE', sample),
            _git_out(other, 'hash-object', '--no-filters', sample))
        self.assertEqual(bs.build_commit(other, _built(self.tree('b'),
                                                       B275)), clean)

    def test_a_builds_commit_is_never_read_as_a_tag(self):
        """Every step resolves its refs with check_refs, which refuses a
        commit build_commit made however it is spelt: the id the run prints,
        the ref under BUILD_REFS. Read as a tag, that commit is upstream's
        strcoll code with the build's patches nowhere."""
        import glibc_locale_data as g
        commit = bs.build_commit(self.repo, _built(self.tree('t'), B275))

        def refused(*refs):
            err = io.StringIO()
            with mock.patch('sys.stderr', err), \
                    self.assertRaises(SystemExit) as cm:
                g.check_refs(self.repo, *refs)
            self.assertEqual(cm.exception.code, 2, refs)
            return flat(err.getvalue())
        for spelt in (commit[:12], 'pg-glibc-audit/builds/' + B275):
            for refs in ((TAG, spelt), (spelt, TAG)):
                self.assertIn('%s is a commit this tool made for a distro '
                              'build' % spelt, refused(*refs))
        # After a fetch of tags too: glibc-2.39 is gone from this clone,
        # and its origin, scripts/glibc, gives it back.
        _git_out(self.repo, 'update-ref', '-d', 'refs/tags/glibc-2.39')
        _git_out(self.repo, 'remote', 'add', 'origin', GLIBC_CLONE)
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            said = refused(commit[:12], 'glibc-2.39')
        self.assertIn('%s is a commit this tool made for a distro build'
                      % commit[:12], said)
        self.assertIn('fetching tags', out.getvalue() + said)
        # The control: tags pass.
        self.assertEqual(set(g.check_refs(self.repo, TAG, 'glibc-2.28')),
                         {TAG, 'glibc-2.28'})

    def test_the_tarball_is_compared_with_the_tag(self):
        base, alias = bs.scope_from_git(GLIBC_CLONE, TAG)
        self.assertEqual(bs.tarball_is_tag(self.repo, TAG, base, alias),
                         bs.TARBALL_IS_TAG % TAG)
        changed = dict(base)
        p = 'localedata/locales/sv_SE'
        changed[p] = (b'x', changed[p][1])
        for args, why in (((changed, alias), p),
                          ((base, b'other\n'), 'intl/locale.alias')):
            with self.assertRaises(bs.Refused) as cm:
                bs.tarball_is_tag(self.repo, TAG, *args)
            self.assertIn(why, str(cm.exception))
        flipped = dict(base)
        flipped[p] = (base[p][0], not base[p][1])
        with self.assertRaises(bs.Refused):
            bs.tarball_is_tag(self.repo, TAG, flipped, alias)


@needs_clone
class FetchedWithTheCloneTheTarballIsTheTag(unittest.TestCase):
    """fetch_build with the clone compares the SRPM's tarball with the tag
    and refuses one that is not; a result kept without that comparison is
    not taken. The small SRPM of FetchesFromAMirror is glibc 2.39 with a
    tarball of a few files: not glibc-2.39's."""

    setUp = FetchesFromAMirror.setUp
    el10 = FetchesFromAMirror.el10

    def test_a_tarball_that_is_not_the_tag_is_refused(self):
        self.el10()
        name = 'glibc-2.39-1.el10'
        kept = bs.fetch_build(name, M, self.cache)
        self.assertNotIn('tarball', kept.provenance)
        with self.assertRaises(bs.Refused) as cm:
            bs.fetch_build(name, M, self.cache, repo=GLIBC_CLONE)
        self.assertIn("the SRPM's tarball is not glibc-2.39",
                      str(cm.exception))


def _fill_build_cache(cache, mirror, name, tree, warned=()):
    """build_sources' cache as fetch_build leaves it for `name`: the record
    of its three files, the files, and the kept tree with its provenance,
    so that fetch_build answers with no network and no SRPM."""
    ver, rel, _, _ = bs._ask(name)
    entries, shas = {}, {}
    for key in ('srpm', 'glibc-locale-source', 'glibc-common'):
        data = ('%s %s' % (key, name)).encode()
        sha = hashlib.sha256(data).hexdigest()
        os.makedirs(os.path.join(cache, 'downloads'), exist_ok=True)
        with open(os.path.join(cache, 'downloads', sha), 'wb') as fh:
            fh.write(data)
        shas[key] = sha
        entries[key] = {'url': mirror + key, 'sha256': sha,
                        'size': len(data), 'tree': 't', 'repo': 'r',
                        'timestamp': '1'}
    bs._write_json(os.path.join(cache, 'builds', name + '.json'),
                   {'mirror': mirror, 'files': entries})
    binaries = [shas['glibc-locale-source'], shas['glibc-common']]
    where = os.path.join(cache, hashlib.sha256(
        ' '.join([shas['srpm']] + binaries).encode()).hexdigest()[:32] +
        '-' + bs._key())
    _linked(tree, os.path.join(where, 'tree'))
    count = sum(len(fs) for _, _, fs in os.walk(os.path.join(where, 'tree')))
    prov = {'build': 'glibc-%s-%s.src' % (ver, rel),
            'version': 'glibc %s' % '.'.join(ver.split('.')[:2]),
            'tag': 'glibc-' + ver, 'srpm_sha256': shas['srpm'],
            'binaries_sha256': binaries, 'files': count,
            'verified': ['locale sources: %d, each the sha256 '
                         'glibc-locale-source-%s-%s records' % (count, ver,
                                                                rel)] +
            list(warned),
            'tarball': bs.TARBALL_IS_TAG % ('glibc-' + ver),
            'applied': [{'pos': 1, 'patch': 'a.patch', 'paths': ['x']}],
            'patches': 2,
            'collation_code_patches': [{'patch': 'a.patch',
                                        'paths': ['x']}]}
    with open(os.path.join(where, 'provenance.json'), 'w') as fh:
        json.dump(prov, fh)


@needs_clone
class ABuildIsASide(unittest.TestCase):
    """side(named=...) is a build, and the sides read one: fetched (here
    from a cache this test fills, so no network), made a commit of a clone
    of the test's own, listed and held to the floor; ordered by rpm; never
    replaced by its tag when it cannot be read."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='build_side_')
        base, _ = bs.scope_from_git(GLIBC_CLONE, TAG)
        cls.base = os.path.join(cls.tmp, 'base')
        for path, (data, exe) in base.items():
            full = os.path.join(cls.base, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as fh:
                fh.write(data)
            os.chmod(full, 0o755 if exe else 0o644)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        import glibc_locale_data as g
        self.g = g
        self.dir = tempfile.mkdtemp(prefix='bs_', dir=self.tmp)
        self.repo = os.path.join(self.dir, 'repo')
        glibc_shaped(self.repo, TAG)

    def fake_fetch(self, built=None, error=None):
        def fetch(name, repo):
            if error:
                raise bs.Refused(error)
            return built or _built(self.base, name)
        return mock.patch.object(self.g, '_fetch', side_effect=fetch)

    def test_named_is_a_build_and_build_is_a_machine(self):
        with self.fake_fetch():
            s = self.g.side(self.repo, named=B275)
        self.assertEqual((s.kind, s.build, s.version),
                         ('build', B275 + '.src', 'glibc 2.34'))
        self.assertEqual(s.ref, _git_out(self.repo, 'rev-parse',
                                         bs.build_ref(B275)))
        self.assertGreaterEqual(len(s.paths), self.g.MIN_LOCALE_FILES)
        self.assertEqual(self.g.label(s), B275)
        m = self.g.side(None, build=B275 + '.x86_64', version='glibc 2.34')
        self.assertEqual((m.kind, m.ref, m.paths), ('machine', None, None))
        # Both at once is neither: refused.
        err = io.StringIO()
        with self.fake_fetch(), mock.patch('sys.stderr', err), \
                self.assertRaises(SystemExit) as cm:
            self.g.side(self.repo, named=B275, build=B275 + '.x86_64')
        self.assertEqual(cm.exception.code, 2)
        self.assertIn('not both', err.getvalue())

    def test_two_builds_of_one_version_are_ordered_by_rpm(self):
        with self.fake_fetch():
            a = self.g.side(self.repo, named=B275)
            b = self.g.side(self.repo, named=B283)
        self.assertEqual(self.g.order(self.repo, a, b)[0], 'forward')
        self.assertEqual(self.g.order(self.repo, b, a)[0], 'reversed')
        self.assertEqual(self.g.nothing_compared(self.repo, a, b), 'tree')
        # The same two built from el8's .40 and .48: by rpm, not "same".
        s40 = self.g.Side('build', a.ref, None,
                          'glibc-2.28-251.el8_10.40.src', 'glibc 2.28',
                          a.paths, a.ids, (), a.prov)
        s48 = s40._replace(ref=b.ref, build='glibc-2.28-251.el8_10.48.src')
        self.assertEqual(self.g.order(self.repo, s40, s48)[0], 'forward')
        self.assertEqual(self.g.order(self.repo, s48, s40)[0], 'reversed')

    def test_a_build_that_cannot_be_read_is_refused_never_the_tag(self):
        for kw in ({'error': 'glibc-2.34-275.el9_8 is not in the mirror'},
                   {'built': _built(self.base, B275, tarball=False)}):
            err = io.StringIO()
            with self.fake_fetch(**kw), mock.patch('sys.stderr', err), \
                    self.assertRaises(SystemExit) as cm:
                self.g.side(self.repo, named=B275)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn('cannot read the build ' + B275, err.getvalue())
            self.assertNotEqual(
                subprocess.run(['git', '-C', self.repo, 'rev-parse',
                                '--verify', '--quiet', bs.build_ref(B275)],
                               capture_output=True).returncode, 0)

    def test_the_sides_read_a_build_by_its_commit(self):
        """glibc_locale_data.py sides on two builds, in a folder whose glibc
        is a repository of the test's own: the provenance of each build,
        read at the builds' commits. The new build changes one locale
        file."""
        here = os.path.join(self.dir, 'scripts')
        os.makedirs(here)
        for f in os.listdir(SCRIPTS_DIR):
            if f.endswith(('.py', '.sh')):
                os.symlink(os.path.join(SCRIPTS_DIR, f),
                           os.path.join(here, f))
        os.rename(self.repo, os.path.join(here, 'glibc'))
        cache = os.path.join(self.dir, 'cache')
        mirror = 'file:///nowhere/'
        sv = 'localedata/locales/sv_SE'
        with open(os.path.join(self.base, sv), 'rb') as fh:
            data = fh.read() + b'% a change of the build\n'
        changed = _linked(self.base, os.path.join(self.dir, 'changed'),
                          {sv: data})
        _fill_build_cache(cache, mirror, B275, self.base)
        _fill_build_cache(cache, mirror, B283, changed,
                          ['!! charmaps NOT checked against '
                           'glibc-locale-source: no zstd reader'])
        env = dict(os.environ, PG_GLIBC_BUILD_SOURCES_CACHE=cache,
                   PG_GLIBC_BUILD_MIRROR=mirror,
                   PG_GLIBC_AUDIT_OUT=os.path.join(self.dir, 'out'))
        sides = [sys.executable, os.path.join(here, 'glibc_locale_data.py'),
                 'sides']
        p = subprocess.run([*sides, B275, B283], capture_output=True,
                           env=env)
        out = p.stdout.decode()
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
        repo = os.path.join(here, 'glibc')
        commits = [_git_out(repo, 'rev-parse', bs.build_ref(b))
                   for b in (B275, B283)]
        for b, c in zip((B275, B283), commits):
            self.assertIn('  %s = %s, one parent: %s' % (b, c[:12], TAG),
                          out)
        self.assertEqual(out.count('    checked: ' + bs.TARBALL_IS_TAG % TAG),
                         2)
        self.assertIn("!! Not checked for these builds:", out)
        self.assertIn("checked above against the upstream tag",
                      flat(out))
        self.assertNotIn('DIRECTION', p.stderr.decode())
        # A `!!` a build's check gave reaches the run, naming the build.
        self.assertIn('!! %s: charmaps NOT checked against '
                      'glibc-locale-source: no zstd reader' % B283, flat(out))
        # The commit the sides made, given back as if it were a tag, on
        # either side: refused by its message, before any provenance.
        for pair in ((TAG, commits[1][:12]), (commits[1][:12], TAG)):
            with self.subTest(pair=pair):
                p = subprocess.run([*sides, *pair],
                                   capture_output=True, env=env)
                self.assertEqual(p.returncode, 2)
                self.assertIn('%s is a commit this tool made for a distro '
                              'build' % commits[1][:12],
                              flat(p.stderr.decode()))
                self.assertNotIn('provenance', p.stdout.decode())

    def test_a_clone_without_the_builds_tag_fetches_it(self):
        """A clone older than the build lacks the tag it is made on: the
        sides fetch tags for it as it does for a tag argument, instead of
        refusing in the middle of the build."""
        here = os.path.join(self.dir, 'scripts')
        os.makedirs(here)
        repo = os.path.join(here, 'glibc')
        os.rename(self.repo, repo)
        tag = _git_out(repo, 'rev-parse', TAG + '^{commit}')
        _git_out(repo, 'update-ref', '-d', 'refs/tags/' + TAG)
        _git_out(repo, 'update-ref', 'HEAD', 'glibc-2.28^{commit}')
        _git_out(repo, 'remote', 'add', 'origin', GLIBC_CLONE)
        out = io.StringIO()
        with self.fake_fetch(), mock.patch('sys.stdout', out):
            _, a, b = self.g.compared_sides(here, B275, B283)
        self.assertIn('Refs not present locally: %s -- fetching tags...'
                      % TAG, out.getvalue())
        self.assertEqual(_git_out(repo, 'rev-parse', TAG + '^{commit}'), tag)
        self.assertEqual((a.kind, b.kind), ('build', 'build'))

    def test_a_step_on_a_clone_without_the_builds_tag_fetches_it(self):
        """The same for a step run on its own: side() fetches the tag a
        build is made on, as the sides do, before making the commit."""
        tag = _git_out(self.repo, 'rev-parse', TAG + '^{commit}')
        _git_out(self.repo, 'update-ref', '-d', 'refs/tags/' + TAG)
        _git_out(self.repo, 'update-ref', 'HEAD', 'glibc-2.28^{commit}')
        _git_out(self.repo, 'remote', 'add', 'origin', GLIBC_CLONE)
        out = io.StringIO()
        with self.fake_fetch(), mock.patch('sys.stdout', out):
            s = self.g.side(self.repo, B275)
        self.assertIn('Refs not present locally: %s -- fetching tags...'
                      % TAG, out.getvalue())
        self.assertEqual(_git_out(self.repo, 'rev-parse', TAG + '^{commit}'),
                         tag)
        self.assertEqual((s.kind, s.prov['tag']), ('build', TAG))

    def recorded(self, lines):
        """PG_GLIBC_AUDIT_SIDES set to a file holding `lines`, as audit.sh
        sets it for its steps; None leaves the file out."""
        # A file of its own each time: subtests share self.dir.
        path = tempfile.mktemp(prefix='build_sides_', dir=self.dir)
        if lines is not None:
            with open(path, 'w') as fh:
                fh.write(''.join(ln + '\n' for ln in lines))
        return mock.patch.dict(os.environ, {self.g.SIDES_ENV: path})

    def test_a_step_takes_the_commit_the_sides_recorded(self):
        """Under audit.sh a step after the sides reads the build's commit
        from the file the sides wrote and does not make it again: with build_commit
        made to fail, the side still reads, at that commit."""
        with self.fake_fetch():
            made = self.g.side(self.repo, named=B275, make=True)
        with self.fake_fetch(), self.recorded([B275 + '\t' + made.ref]), \
                mock.patch.object(bs, 'build_commit',
                                  side_effect=AssertionError('made again')):
            s = self.g.side(self.repo, B275)
        self.assertEqual((s.kind, s.ref), ('build', made.ref))
        # The sides always make it, the file there or not.
        with self.fake_fetch(), self.recorded(None), \
                mock.patch.object(bs, 'build_commit',
                                  wraps=bs.build_commit) as bc:
            self.g.side(self.repo, B275, make=True)
        self.assertEqual(bc.call_count, 1)

    def test_a_recorded_commit_is_checked(self):
        """What the file names is a commit this tool made for this build,
        or the step dies naming the file: another build's commit (its
        message), a tag's (its committer), no line for the build, no
        file."""
        with self.fake_fetch():
            other = self.g.side(self.repo, named=B283, make=True).ref
        tag = _git_out(self.repo, 'rev-parse', TAG + '^{commit}')
        for lines, said in (
                ([B275 + '\t' + other], 'a commit this tool made for '
                                         'another build or another SRPM'),
                ([B275 + '\t' + tag], 'which this tool did not make for '
                                       'a build'),
                ([B283 + '\t' + other], 'names it 0 time(s)'),
                (None, 'and it cannot be read')):
            with self.subTest(said=said):
                err = io.StringIO()
                with self.fake_fetch(), self.recorded(lines), \
                        mock.patch('sys.stderr', err), \
                        self.assertRaises(SystemExit) as cm:
                    self.g.side(self.repo, B275)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn('cannot read the build %s' % B275,
                              flat(err.getvalue()))
                self.assertIn(said, flat(err.getvalue()))

    def test_the_sides_record_each_build_once(self):
        """One build given as both sides is one line, which the steps after
        the sides then find once."""
        with self.fake_fetch():
            s = self.g.side(self.repo, named=B275, make=True)
        with self.recorded([]):
            self.g.write_recorded([s, s])
            with open(os.environ[self.g.SIDES_ENV]) as fh:
                self.assertEqual(fh.read(), '%s\t%s\n' % (B275, s.ref))


def _fill_with_code(cache, mirror, name, tree, code=(), warned=()):
    """_fill_build_cache, with the build's patches to strcoll's code in its
    provenance as build() records them ((patch, paths) pairs): none by
    default, where _fill_build_cache's placeholder names a path outside
    localedata/ and locale/. `warned` is _fill_build_cache's."""
    _fill_build_cache(cache, mirror, name, tree, warned)
    for where in os.listdir(cache):
        path = os.path.join(cache, where, 'provenance.json')
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            prov = json.load(fh)
        if prov['build'] != name + '.src':
            continue
        prov['collation_code_patches'] = [
            {'patch': p, 'paths': list(paths)} for p, paths in code]
        with open(path, 'w') as fh:
            json.dump(prov, fh)
        return
    raise AssertionError('no kept tree for %s in %s' % (name, cache))


_HEX40 = re.compile(r'\b[0-9a-f]{40}\b')
ABSENT = 'glibc-2.34-286.el9_8'
LD = 'locale/programs/ld-collate.c'


class _StepsOnBuilds:
    """Steps 2 to 5, run on their own, read a build named as `rpm -q glibc`
    names it: fetched (from a cache this test fills, so no network), made a
    commit of a repository of the test's own, read by that commit, ordered
    by rpm, and printed by its name -- never its commit's id. Each build
    here is glibc-2.34's own scope with one change: B283 changes sv_SE,
    adds a locale file and changes a line of ld-collate.c; SAME holds
    B275's tree under another release."""

    SAME = 'glibc-2.34-284.el9_8'
    LR = 'glibc-2.34-285.el9_8'
    CLEAN = 'glibc-2.34-287.el9_8'
    OUT = 'glibc-2.34-288.el9_8'
    LRC = 'glibc-2.34-289.el9_8'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='build_step_')
        base, _ = bs.scope_from_git(GLIBC_CLONE, TAG)
        cls.base = os.path.join(cls.tmp, 'base')
        for path, (data, exe) in base.items():
            full = os.path.join(cls.base, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as fh:
                fh.write(data)
            os.chmod(full, 0o755 if exe else 0o644)
        sv = 'localedata/locales/sv_SE'
        with open(os.path.join(cls.base, sv), 'rb') as fh:
            sv_text = fh.read()
        with open(os.path.join(cls.base, LD), 'rb') as fh:
            ld = fh.read()
        cls.changed = _linked(cls.base, os.path.join(cls.tmp, 'changed'), {
            sv: sv_text + b'% a change of the build\n',
            LD: ld + b'int pg_glibc_audit_test_line;\n'})
        cls.clean = _linked(cls.base, os.path.join(cls.tmp, 'clean'), {
            sv: sv_text + b'% a change of the build\n'})
        added = os.path.join(cls.changed, 'localedata/locales/xx_YY')
        with open(added, 'wb') as fh:
            fh.write(sv_text)
        with open(os.path.join(cls.base, 'locale/programs/linereader.h'),
                  'rb') as fh:
            lr = fh.read()
        cls.reader = _linked(cls.base, os.path.join(cls.tmp, 'reader'), {
            'locale/programs/linereader.h': lr + b'/* a change */\n'})
        with open(os.path.join(cls.base, 'locale/programs/linereader.c'),
                  'rb') as fh:
            lrc = fh.read()
        cls.reader_c = _linked(cls.base, os.path.join(cls.tmp, 'reader_c'), {
            'locale/programs/linereader.c': lrc + b'/* a change */\n'})
        cls.repo = os.path.join(cls.tmp, 'repo')
        glibc_shaped(cls.repo, TAG)
        cls.cache = os.path.join(cls.tmp, 'cache')
        cls.mirror = 'file:///nowhere/'
        in_locale = [('glibc-rh827510.patch', ['locale/loadlocale.c'])]
        _fill_with_code(cls.cache, cls.mirror, B275, cls.base, in_locale)
        _fill_with_code(cls.cache, cls.mirror, B283, cls.changed,
                        in_locale + [('glibc-c-utf8-locale-1.patch', [LD])])
        _fill_with_code(cls.cache, cls.mirror, cls.SAME, cls.base,
                        [('glibc-example.patch', ['string/strcoll_l.c'])])
        _fill_with_code(cls.cache, cls.mirror, cls.LR, cls.reader)
        _fill_with_code(cls.cache, cls.mirror, cls.CLEAN, cls.clean,
                        in_locale)
        _fill_with_code(cls.cache, cls.mirror, cls.OUT, cls.changed,
                        in_locale + [('glibc-example.patch',
                                      ['wcsmbs/wcscoll.c'])])
        _fill_with_code(cls.cache, cls.mirror, cls.LRC, cls.reader_c)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def run_step(self, script, *args):
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        env = dict(os.environ, PG_GLIBC_BUILD_SOURCES_CACHE=self.cache,
                   PG_GLIBC_BUILD_MIRROR=self.mirror, PG_GLIBC_AUDIT_OUT=out)
        p = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, script), *args,
             '--repo', self.repo], capture_output=True, env=env, cwd=self.tmp)
        return (p.returncode, p.stdout.decode('utf-8', 'replace'),
                p.stderr.decode('utf-8', 'replace'), out)

    def assertNoCommitId(self, *texts, out_dir=None):
        for text in texts:
            self.assertEqual(_HEX40.findall(text), [], text[-1500:])
        for name in os.listdir(out_dir) if out_dir else ():
            self.assertNotRegex(name, _HEX40)
            with open(os.path.join(out_dir, name), encoding='utf-8') as fh:
                self.assertEqual(_HEX40.findall(fh.read()), [], name)


@needs_clone
class AStepReadsABuild(_StepsOnBuilds, unittest.TestCase):
    """Step 2 on builds, and what every step refuses
    (_StepsOnBuilds)."""

    def test_step_2_reads_two_builds_and_names_them(self):
        rc, out, err, d = self.run_step('filter_lc_collate_changes.py',
                                        B275, B283)
        self.assertEqual(rc, 0, err[-2000:])
        self.assertNoCommitId(out, err, out_dir=d)
        self.assertIn('The sort rules localedef builds for each locale at %s '
                      'and at %s,' % (B275, B283), flat(out))
        # xx_YY exists only in the build.
        self.assertEqual(Step2Report(out).added,
                         {'xx_YY': '(not in SUPPORTED)'})
        # Ordered by rpm: two builds of one version, which git cannot order.
        self.assertNotIn('DIRECTION', out + err)
        self.assertIn('SUPPORTED at both builds', flat(out))
        self.assertNotIn('both tags', flat(out))
        # The lists audit.sh reads are named for the builds.
        self.assertEqual(sorted(os.listdir(d)), sorted(
            ['%s.%s..%s.txt' % (k, B275, B283) for k in (
                'step2_changed_collate', 'step2_keyed_locales',
                'step2_not_compared', 'step2_removed_locales')]
            + ['step2_rules_characters.%s..%s.tsv' % (B275, B283),
               'step2_rules_by_name.%s..%s.json' % (B275, B283)]))
        # An added file: the old build's own sources are the answer.
        self.assertIn("%s's own locale sources do not have these files, so "
                      "that build ships none of these locales." % B275,
                      flat(out))
        self.assertNotIn('distros backport', out)

    def test_a_tag_and_a_build_say_sides(self):
        rc, out, err, _ = self.run_step('filter_lc_collate_changes.py',
                                        TAG, B283)
        self.assertEqual(rc, 0, err[-2000:])
        self.assertNoCommitId(out, err)
        self.assertIn('SUPPORTED at both sides', flat(out))
        # The old side is the tag: its added-file sentence is the tag's.
        self.assertIn('distros backport', out)

    def test_two_builds_of_one_tree_compare_nothing(self):
        for script in ('filter_lc_collate_changes.py',):
            with self.subTest(script=script):
                rc, out, err, _ = self.run_step(script, B275, self.SAME)
                self.assertEqual(rc, 0, err[-2000:])
                text = flat(out + err)
                self.assertIn('%s and %s are two commits of one source tree'
                              % (B275, self.SAME), text)
                self.assertIn('Step 2 compares nothing: it would compare '
                              'that tree against itself. The two builds '
                              'hold the same locale sources and the same '
                              'code under locale/', text)
                self.assertNotIn("The distro's own builds", text)
                self.assertNotIn('DIRECTION', text)

    def test_one_build_twice_is_one_commit(self):
        rc, out, err, _ = self.run_step('filter_lc_collate_changes.py',
                                        B275, B275)
        self.assertEqual(rc, 0, err[-2000:])
        self.assertIn('%s and %s are the same commit. Step 2 '
                      'compares nothing: it would compare that commit '
                      'against itself.' % (B275, B275),
                      flat(out + err))
        self.assertIn('the two builds are one commit.', flat(out))

    def test_a_build_that_cannot_be_read_is_refused_never_its_tag(self):
        for script, args in (
                ('filter_lc_collate_changes.py', (B275, ABSENT)),
                ('filter_lc_collate_changes.py', (ABSENT, B275))):
            with self.subTest(script=script):
                rc, out, err, d = self.run_step(script, *args)
                self.assertEqual(rc, 2)
                self.assertIn('cannot read the build ' + ABSENT, err)
                self.assertEqual(out, '')
                self.assertEqual(os.listdir(d), [])

    def test_a_build_that_changes_the_line_reader_is_refused(self):
        for build, path in ((self.LR, 'locale/programs/linereader.h'),
                            (self.LRC, 'locale/programs/linereader.c')):
            with self.subTest(path=path):
                rc, out, err, _ = self.run_step(
                    'filter_lc_collate_changes.py', B275, build)
                self.assertEqual(rc, 2)
                self.assertIn("cannot read the build %s: it changes glibc's "
                              "line reader (%s)" % (build, path), flat(err))
                self.assertNotIn('The sort rules localedef builds', out)

    def test_a_builds_commit_given_as_a_tag_is_refused_by_every_step(self):
        rc, _, err, _ = self.run_step('filter_lc_collate_changes.py', TAG,
                                      B283)
        self.assertEqual(rc, 0, err[-2000:])
        commit = _git_out(self.repo, 'rev-parse', bs.build_ref(B283))
        for script, args in (
                ('filter_lc_collate_changes.py', (TAG, commit)),
                ('filter_lc_collate_changes.py', (TAG, commit[:12]))):
            with self.subTest(script=script):
                rc, out, err, _ = self.run_step(script, *args)
                self.assertEqual(rc, 2)
                self.assertIn('is a commit this tool made for a distro build',
                              flat(err))
                self.assertIn('Name the build itself '
                              '(glibc-2.34-283.el9_8) instead.', flat(err))

    def test_a_reversed_pair_of_builds_names_builds(self):
        rc, out, err, _ = self.run_step('filter_lc_collate_changes.py',
                                        B283, B275)
        self.assertEqual(rc, 2)
        self.assertIn('Every step takes <old_build> <new_build>', flat(err))
        self.assertIn('the old build glibc-2.34-283.el9_8', flat(err))
        self.assertNoCommitId(out, err)


@needs_clone
class AStepReadsTheFixtureBuilds(unittest.TestCase):
    """Step 2 on the three real builds of the fixtures, rebuilt from
    tests/build_sources/ and the clone (fixture()), read by audit.sh's
    known answers: over RHEL 8 -> 9 the builds hold C.UTF-8's file on both
    sides, so it is compared like any locale and differs, and en_US@ampm,
    which only el8 ships, is deleted; over RHEL 9 -> 10 C's rules are the
    same and no `!!` says C.UTF-8 may change (backlog 13.6)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='fixture_step_')
        cls.repo = os.path.join(cls.tmp, 'repo')
        glibc_shaped(cls.repo, TAG)
        cls.cache = os.path.join(cls.tmp, 'cache')
        cls.mirror = 'file:///nowhere/'
        for build, _ in FIXTURES:
            tree = fixture(build)[5]
            d = os.path.join(cls.tmp, build)
            for path, (data, exe) in tree.items():
                full = os.path.join(d, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'wb') as fh:
                    fh.write(data)
                os.chmod(full, 0o755 if exe else 0o644)
            _fill_with_code(cls.cache, cls.mirror, build, d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def step2(self, old, new):
        out = tempfile.mkdtemp(prefix='out_', dir=self.tmp)
        env = dict(os.environ, PG_GLIBC_BUILD_SOURCES_CACHE=self.cache,
                   PG_GLIBC_BUILD_MIRROR=self.mirror, PG_GLIBC_AUDIT_OUT=out)
        p = subprocess.run(
            [sys.executable,
             os.path.join(SCRIPTS_DIR, 'filter_lc_collate_changes.py'),
             old, new, '--repo', self.repo], capture_output=True, env=env,
            cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])

        def lines(kind):
            with open(os.path.join(out, 'step2_%s.%s..%s.txt'
                                   % (kind, old, new))) as fh:
                return [ln.strip() for ln in fh
                        if ln.strip() and not ln.startswith('#')]
        return p.stdout.decode(), lines

    def test_rhel8_to_rhel9(self):
        out, lines = self.step2(*(b for b, _ in FIXTURES[:2]))
        self.assertIn('C', lines('changed_collate'))
        self.assertIn('en_US@ampm (deleted)', lines('removed_locales'))
        # Each build holds its own C: compared, not added, and nothing said
        # of a tag.
        report = Step2Report(out)
        # Printed as C.UTF-8: glibc's file C is that locale (shown_name).
        self.assertIn(STEP2_C, report.differs)
        self.assertNotIn(STEP2_C, report.added)
        self.assertNotIn('Not checked for glibc-', out)

    def test_rhel9_to_rhel10(self):
        out, lines = self.step2(*(b for b, _ in FIXTURES[1:]))
        report = Step2Report(out)
        self.assertNotIn(STEP2_C, report.added)
        self.assertNotIn(STEP2_C, report.differs)
        self.assertNotIn('C.UTF-8', out)
        self.assertEqual(lines('changed_collate'), ['th_TH', 'thai'])


def without_the_tags_notice(text):
    """text without the `!!` for the sides read as tags, at any indent:
    it names latest-el8, latest-el9 and latest-el10 as forms a side can be
    typed in, which tests of what a run says it resolved must not count."""
    out, skip = [], None
    for line in text.split('\n'):
        stripped = line.lstrip(' ')
        if stripped.startswith('!! Not checked for glibc-'):
            skip = line[:len(line) - len(stripped)] + '   '
            continue
        if skip is not None and line.startswith(skip):
            continue
        skip = None
        out.append(line)
    return '\n'.join(out)

B40, B48 = 'glibc-2.28-251.el8_10.40', 'glibc-2.28-251.el8_10.48'
B128 = 'glibc-2.39-128.el10_2'
REINDEX = SUMMARY_STEP2
REMOVED = '-- Removed: locale files the old side has and the new side does not'
# Step 2's `!!` for C.UTF-8 (filter_lc_collate_changes.print_c_utf8), as the
# summary's warnings repeat it, whitespace collapsed.
C_NOT_COMPARED = ("!! {} holds no localedata/locales/C, the file C.UTF-8 is "
                  "built from, and a distro's build can ship its own, so "
                  "C.UTF-8's sort rules were not compared. ")
C_DIFFERS = ("!! C.UTF-8's sort rules differ, and it is listed in the "
             "summary. ")
MEASURED = '-- Measured order: NOT RUN'


# -- latest-elN: the newest build of a release, resolved once by audit.sh ----

class ResolvesTheLatestBuild(unittest.TestCase):
    """glibc_locale_data's `resolve` and `latest` subcommands on a mirror
    the test makes (FakeMirror, in place of build_sources._get), and the
    refusal of latest-elN by every step run on its own. In one process: no
    git and no network is reached, and a mutant that reaches either fails
    on the stand-in that raises."""

    EL8 = ('251.el8_10.2', '251.el8_10.40', '251.el8_10.5')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='latest_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, 'cache')
        self.mirror = FakeMirror()
        self.mirror.repo('pub/rocky/8', 'BaseOS/source/tree',
                         [_row('glibc', 'src', r, ver='2.28')
                          for r in self.EL8])
        self.mirror.repo('pub/rocky/9', 'BaseOS/source/tree',
                         [_row('glibc', 'src', '275.el9_8', ver='2.34'),
                          _row('glibc', 'src', '70.el9', ver='2.34')])
        for patch in (mock.patch.object(bs, '_get',
                                        side_effect=self.mirror.get),
                      mock.patch.dict(os.environ, {
                          'PG_GLIBC_BUILD_MIRROR': M,
                          'PG_GLIBC_BUILD_SOURCES_CACHE': self.cache,
                          'TZ': 'America/Sao_Paulo'})):
            patch.start()
            self.addCleanup(patch.stop)
        time.tzset()
        self.addCleanup(time.tzset)

    def main(self, *argv):
        """(exit code, stdout, stderr) of glibc_locale_data's _main."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'stdout', out), \
                mock.patch.object(sys, 'stderr', err):
            try:
                code = g._main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def latest(self, *names):
        to = os.path.join(self.tmp, 'latest.txt')
        code, out, err = self.main('latest', '--to', to, *names)
        lines = None
        if os.path.exists(to):
            with open(to) as fh:
                lines = fh.read().split('\n')
        return code, out, err, lines

    def line(self, name, build, major, when='2025-11-01 12:26 UTC'):
        return ('%s is %s: the newest glibc build in %spub/rocky/%d, by its '
                'metadata of %s.' % (name, build, M, major, when))

    def test_resolve_marks_latest_a_build_and_asks_no_mirror(self):
        to = os.path.join(self.tmp, 'resolve.txt')
        with mock.patch.object(bs, '_get', side_effect=AssertionError):
            code, out, err = self.main('resolve', '--to', to, 'latest-el8',
                                       'glibc-2.34')
        self.assertEqual((code, out, err), (0, '', ''))
        with open(to) as fh:
            self.assertEqual(fh.read(), 'latest-el8\tbuild\tlatest\n'
                             'glibc-2.34\ttag\n')

    def test_each_latest_is_its_newest_build_and_says_so(self):
        """el8's newest by rpm's order, .40 (not .5 as text); the line names
        the build, the mirror's tree and the metadata's time in UTC
        whatever the machine's time zone."""
        code, out, err, lines = self.latest('latest-el8', 'latest-el9')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(lines, [B40, B275, ''])
        self.assertEqual(flat(out), flat(
            self.line('latest-el8', B40, 8) + ' ' +
            self.line('latest-el9', B275, 9)))

    def test_a_tag_on_the_other_side_is_kept(self):
        code, out, err, lines = self.latest('glibc-2.28', 'latest-el9')
        self.assertEqual((code, lines), (0, ['glibc-2.28', B275, '']))
        self.assertEqual(flat(out), flat(self.line('latest-el9', B275, 9)))

    def test_one_release_on_both_sides_is_asked_once(self):
        code, out, err, lines = self.latest('latest-el9', 'latest-el9')
        self.assertEqual((code, lines), (0, [B275, B275, '']))
        self.assertEqual(flat(out), flat(self.line('latest-el9', B275, 9)))
        self.assertEqual(len([u for u in self.mirror.asked
                              if u.endswith('repomd.xml')]), 1)

    def test_metadata_with_no_date_says_so(self):
        base = M + 'pub/rocky/9/BaseOS/source/tree/repodata/'
        self.mirror.files[base + 'repomd.xml'] = re.sub(
            rb'<timestamp>[^<]*</timestamp>', b'',
            self.mirror.files[base + 'repomd.xml'])
        code, out, err, lines = self.latest('latest-el9', 'latest-el9')
        self.assertEqual(code, 0, err)
        self.assertEqual(flat(out), flat(
            'latest-el9 is %s: the newest glibc build in %spub/rocky/9, and '
            'its metadata gives no date this reads.' % (B275, M)))

    def test_a_release_the_mirror_does_not_publish_is_refused(self):
        """latest-el7: the mirror answers 404 for its metadata. Nothing is
        written, so audit.sh has no side to go on with."""
        code, out, err, lines = self.latest('latest-el7', 'latest-el9')
        self.assertEqual((code, out, lines), (2, '', None))
        self.assertIn('error: cannot resolve latest-el7: %spub/rocky/7 has no '
                      'source metadata' % M, flat(err))

    def test_a_mirror_that_fails_is_refused_never_read_as_a_tag(self):
        url = M + 'pub/rocky/9/BaseOS/source/tree/repodata/repomd.xml'
        self.mirror.errors[url] = bs.Refused('%s: HTTP 503' % url)
        code, out, err, lines = self.latest('latest-el8', 'latest-el9')
        self.assertEqual((code, out, lines), (2, '', None))
        self.assertIn('error: cannot resolve latest-el9: %s: HTTP 503' % url,
                      flat(err))

    REFUSED = ("latest-el9 is resolved only by audit.sh, once for the whole "
               "run, so that every step reads the same build: run audit.sh, "
               "or give the build's name it prints.")

    def refused_by_hand(self, call):
        """call() dies with the refusal before git is asked anything."""
        err = io.StringIO()
        with mock.patch.object(g, 'run_git', side_effect=AssertionError), \
                mock.patch.object(g.subprocess, 'run',
                                  side_effect=AssertionError), \
                mock.patch.object(sys, 'stderr', err), \
                self.assertRaises(SystemExit) as cm:
            call()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(flat(err.getvalue()), 'error: ' + self.REFUSED)

    def test_the_sides_by_hand_refuse_it(self):
        """compared_sides, before it clones or fetches anything."""
        self.refused_by_hand(lambda: g.compared_sides(self.tmp, 'glibc-2.34',
                                                      'latest-el9'))

    def test_a_step_reading_its_sides_refuses_it(self):
        """step_sides, the entry of step 2."""
        self.refused_by_hand(lambda: g.step_sides(self.tmp, 'latest-el9',
                                                  'glibc-2.39'))

    def test_a_step_checking_a_tag_refuses_it(self):
        """check_refs called directly, as provenance does."""
        with mock.patch.object(g, 'find_repo', return_value=self.tmp):
            self.refused_by_hand(lambda: g._main(['provenance',
                                                  'latest-el9']))

    def test_neither_the_url_nor_the_date_is_cut(self):
        """Whatever the mirror's length, the URL stays on one line, so it
        can be copied, and so does the date (on Rocky's real mirror the
        wrap once fell between "04:24" and "UTC")."""
        for n in range(40):
            mirror = 'https://m%s.test/' % ('x' * n)
            text = g.latest_line('latest-el9', bs.Latest(
                B275, mirror, 'pub/rocky/9', '1762000000'))
            with self.subTest(n=n):
                lines = text.split('\n')
                self.assertEqual(sum(mirror + 'pub/rocky/9,' in ln
                                     for ln in lines), 1, text)
                self.assertEqual(sum('2025-11-01 12:26 UTC.' in ln
                                     for ln in lines), 1, text)

    def test_the_rule_reads_only_latest_el_and_a_number(self):
        for name, major in (('latest-el8', 8), ('latest-el10', 10),
                            ('latest-el', None), ('latest-el9_8', None),
                            ('latest-el9 ', None), ('latest-el9\n', None),
                            ('xlatest-el9', None),
                            ('glibc-2.34', None), (B275, None)):
            with self.subTest(name=name):
                self.assertEqual(g.latest_major(name), major)

    def test_two_tags_never_import_build_sources(self):
        """resolve with two tags, in a fresh interpreter: the run on two
        tags is the run of before."""
        to = os.path.join(self.tmp, 'r.txt')
        p = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.argv = ["g", "resolve", "--to", %r, '
             '"glibc-2.28", "glibc-2.34"]; sys.path.insert(0, %r); '
             'import glibc_locale_data as g; g._main(sys.argv[1:]); '
             'print("build_sources" in sys.modules)' % (to, SCRIPTS_DIR)],
            capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout, p.stderr),
                         (0, 'False\n', ''))


class _AuditOnBuilds:
    """audit.sh on builds named as `rpm -q glibc` names them, from a folder
    of the test's own: audit.sh and a COPY of scripts/ (a linked script
    imports from the folder its link points to, measured 2026-11-03), a
    glibc of its own (glibc_shaped), a build cache filled with no network
    (_fill_with_code) and the suite's reader cache, so that no run computes
    the reader from nothing. Each class runs its pairs once (RUNS) and its
    tests read the output. `builds()` returns {build: tree folder}."""

    RUNS = {}
    # {tree: [(version, release), ...]} of the glibc SRPMs the class's
    # mirror lists, on disk and read by file:// (latest-elN); none by
    # default, and then the mirror is a folder that is not there.
    LISTS = {}

    @classmethod
    def make_mirror(cls):
        if not cls.LISTS:
            return 'file:///nowhere/'
        root = os.path.join(cls.tmp, 'mirror')
        for tree, srpms in cls.LISTS.items():
            prim = _primary([_row('glibc', 'src', rel, ver=ver)
                             for ver, rel in srpms])
            d = os.path.join(root, tree, 'BaseOS/source/tree/repodata')
            os.makedirs(d)
            with open(os.path.join(d, 'repomd.xml'), 'wb') as fh:
                fh.write(_repomd(prim))
            with open(os.path.join(d, 'u-PRIMARY.xml.gz'), 'wb') as fh:
                fh.write(prim)
        # The build cache is filled with this URL too, slash and all:
        # fetch_build reads a kept build with no network only from the
        # mirror it was fetched from.
        return 'file://' + root + '/'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='audit_builds_')
        root = os.path.join(cls.tmp, 'audit')
        os.makedirs(os.path.join(root, 'scripts'))
        shutil.copy2(os.path.join(SCRIPTS_DIR, os.pardir, 'audit.sh'), root)
        for f in os.listdir(SCRIPTS_DIR):
            if f.endswith(('.py', '.sh')):
                shutil.copy2(os.path.join(SCRIPTS_DIR, f),
                             os.path.join(root, 'scripts', f))
        cls.repo = os.path.join(root, 'scripts', 'glibc')
        glibc_shaped(cls.repo, TAG)
        cls.cache = os.path.join(cls.tmp, 'cache')
        cls.mirror = cls.make_mirror()
        for build, spec in cls.builds().items():
            _fill_with_code(cls.cache, cls.mirror, build, *spec)
        env = dict(os.environ, PG_GLIBC_BUILD_SOURCES_CACHE=cls.cache,
                   PG_GLIBC_BUILD_MIRROR=cls.mirror,
                   PG_GLIBC_READER_CACHE=os.path.join(SCRIPTS_DIR,
                                                      'glibc-reader-cache'))
        env.pop('PG_GLIBC_AUDIT_SIDES', None)
        cls.out = {}
        for label, pair in cls.RUNS.items():
            out = os.path.join(cls.tmp, 'out', label)
            os.makedirs(out)
            p = subprocess.run(['bash', os.path.join(root, 'audit.sh'), *pair],
                               cwd=cls.tmp, capture_output=True,
                               env=dict(env, PG_GLIBC_AUDIT_OUT=out))
            cls.out[label] = (p.returncode,
                              (p.stdout + p.stderr).decode('utf-8',
                                                           'replace'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    @classmethod
    def tree(cls, label, build, change=None):
        """The fixture build's tree, or with `change` ({path: bytes})."""
        d = os.path.join(cls.tmp, 'trees', label)
        for path, (data, exe) in fixture(build)[5].items():
            full = os.path.join(d, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as fh:
                fh.write((change or {}).get(path, data))
            os.chmod(full, 0o755 if exe else 0o644)
        return d

    def run_of(self, label):
        rc, out = self.out[label]
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn('AUDIT SUMMARY', out)
        return out

    def summary(self, label):
        return self.run_of(label).split('AUDIT SUMMARY', 1)[1]

    def test_what_a_build_run_does_not_read_is_said_in_one_place(self):
        """The one place is the `!! Not checked for these builds` under
        'What was compared', repeated once under the summary's warnings; no
        other block of the summary restates it (four review rounds each
        found one more copy that disagreed). The same for the code no step
        compares, its `!! Not compared on any route` (glibc_locale_data.CODE_NOT_COMPARED),
        which names localedef and each build's patches to that code: step
        5 compared them until step 9.3, and the sentence that replaced it
        must not lose either. Read on every run of each class that reached
        the summary."""
        ran = [label for label, (rc, _) in self.out.items() if rc == 0]
        self.assertTrue(ran)
        for label in ran:
            out = self.run_of(label)
            summary = out.split('AUDIT SUMMARY', 1)[1]
            outside, head, warnings = summary.partition(
                '-- Warnings the clean results above do NOT cover')
            self.assertTrue(head, label)
            # Each run is one kind, by what 'What was compared' names:
            # builds rebuilt from their SRPM, machines read from their own
            # files (each with its one place), or tags (neither).
            compared = compared_text(out)
            kinds = [k for k, title in (('build', 'Build provenance'),
                                        ('machine', 'Machine provenance'))
                     if title in compared]
            self.assertLessEqual(len(kinds), 1, label)
            kind = kinds[0] if kinds else 'tag'
            for k, said in (('build', '!! Not checked for these builds:'),
                            ('machine', '!! Not checked for these machines:')):
                self.assertEqual(flat(warnings).count(said),
                                 1 if k == kind else 0, (label, k))
            self.assertEqual(flat(warnings).count(
                '!! Not compared on any route: the code that compiles and '
                "compares the rules (localedef's and strcoll's, each build's "
                'patches to it included).'), 1, label)
            # Inside the warnings too, the one place only: no other `!!`
            # says what is not read of a build.
            self.assertEqual(flat(warnings).count("localedef's and strcoll's"),
                             1, label)
            for word in ('compiler', 'strcmp'):
                self.assertEqual(flat(warnings).count(word),
                                 1 if kind == 'build' else 0, (label, word))
            if kind == 'build':
                self.assertIn('such as those to the strcmp that strcoll '
                              'calls', flat(warnings), label)
            self.assertNotIn('outside localedata/ and locale/',
                             flat(warnings), label)
            for word in ('strcmp', 'compiler', 'wcsmbs', 'strcoll'):
                self.assertNotIn(word, outside, label)
            self.assertEqual(deleted_steps_named(out), [], label)

    def test_what_postgresql_warns_about_is_said_once(self):
        """16.166 and 13.9 on every run of the class that reached the
        summary: builds of two versions and of one (RHEL 9.x 275 -> 283,
        8.10 .40 -> .48), a tag and a build of one release, the machines'
        own files."""
        ran = [label for label, (rc, _) in self.out.items() if rc == 0]
        self.assertTrue(ran)
        for label in ran:
            out = self.run_of(label)
            old, new = self.RUNS[label][:2]
            assert_collation_version(self, out, pg_version_of(old),
                                     pg_version_of(new), label)
            assert_no_route_looks(self, out, label)


@needs_clone
class AuditShReadsTheFixtureBuilds(_AuditOnBuilds, unittest.TestCase):
    """audit.sh's known answers on the three real builds of the fixtures:
    over RHEL 8 -> 9 C.UTF-8 changes and en_US@ampm is removed, and the
    summary says so without a tag's caveat; over RHEL 9 -> 10 nothing says
    C.UTF-8 may change or that the distros ship their own."""

    RUNS = {'89': (B40, B275), '910': (B275, B128),
            'latest': ('latest-el8', 'latest-el9')}
    LISTS = {'pub/rocky/8': [('2.28', '251.el8_10.40'),
                             ('2.28', '251.el8_10.5')],
             'pub/rocky/9': [('2.34', '275.el9_8')]}

    @classmethod
    def builds(cls):
        return {b: (cls.tree(b, b), ()) for b in (B40, B275, B128)}

    def test_a_recorded_machine_is_read_as_its_build(self):
        """Each recorded machine's file (tests/locale_order/rhel*.extract)
        made a commit as the sides make it (machine_commit): its
        localedata/locales/ and localedata/charmaps/ are the very trees of
        its build's commit, rebuilt from the SRPM, and the reader reads it
        with the same keyword tables (one reader-cache Build). So the
        machine route gives every answer the build route gives on these
        machines, with nothing downloaded."""
        import glibc_tree
        import reader_cache as rc
        for name, build, tag in (('rhel8', B40, 'glibc-2.28'),
                                 ('rhel9', B275, 'glibc-2.34'),
                                 ('rhel10', B128, 'glibc-2.39')):
            with self.subTest(machine=name):
                folder = os.path.join(self.tmp, 'machine', name)
                p = subprocess.run(
                    [sys.executable, os.path.join(SCRIPTS_DIR,
                                                  'locale_order.py'),
                     '--unpack', os.path.join(TESTS_DIR, 'locale_order',
                                              name + '.extract.tar.gz'),
                     folder], capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                commit = g.machine_commit(self.repo, tag, folder, build,
                                          '0' * 64)
                built = g.BUILD_REFS + build
                for path in ('localedata/locales', 'localedata/charmaps'):
                    # Each file's bytes, by name: the mode is not compared,
                    # since glibc's tarball holds bi_VU executable and a
                    # machine installs it as a plain file (measured on
                    # rhel8), and no step reads a mode.
                    blobs = []
                    for ref in (commit, built):
                        out = g.run_git(['ls-tree', '-z', f'{ref}:{path}'],
                                        self.repo).stdout.decode()
                        blobs.append({e.split('\t')[1]: e.split(' ')[2]
                                      .split('\t')[0]
                                      for e in out.split('\0') if e})
                    self.assertTrue(blobs[0], path)
                    self.assertEqual(blobs[0], blobs[1], path)
                self.assertEqual(
                    rc.build_id(glibc_tree.build_at(self.repo, commit, build)),
                    rc.build_id(glibc_tree.builds_at(self.repo, [built])[built]))

    def test_latest_is_the_run_its_builds_would_give(self):
        """latest-el8 latest-el9 is resolved before anything reads the
        sides: after the lines that say which build each stands for, the
        run is the run of B40 B275, line for line, but for its folder."""
        lines = self.run_of('latest').split('\n')
        cut = lines.index('')
        self.assertEqual(flat('\n'.join(lines[:cut])), flat(
            'latest-el8 is %s: the newest glibc build in %spub/rocky/8, by '
            'its metadata of 2025-11-01 12:26 UTC. latest-el9 is %s: the '
            'newest glibc build in %spub/rocky/9, by its metadata of '
            '2025-11-01 12:26 UTC.' % (B40, self.mirror, B275, self.mirror)))
        out = os.path.join(self.tmp, 'out')
        self.assertEqual(
            [ln.replace(os.path.join(out, 'latest'), 'OUT')
             for ln in lines[cut:]],
            [ln.replace(os.path.join(out, '89'), 'OUT')
             for ln in self.run_of('89').split('\n')])

    def test_rhel8_to_rhel9_reindexes_c_utf8(self):
        body = summary_block(self.run_of('89'), REINDEX)
        self.assertEqual(body[0], 'C.UTF-8')
        self.assertNotIn('C', body)
        self.assertIn("C.UTF-8 is glibc's locale file C, written C in the "
                      "full list; C.utf8 and C.UTF-8 are its spellings. A "
                      "collation or a database whose locale is exactly C or "
                      "POSIX is compared by PostgreSQL itself, without "
                      "glibc.", ' '.join(body))

    def test_rhel8_to_rhel9_removes_en_us_ampm_with_no_tag_caveat(self):
        body = summary_block(self.run_of('89'), REMOVED)
        self.assertEqual(body[:4], [
            "Each build's locale files are the ones its glibc-locale-source",
            "package installs, checked one by one ('What was compared').",
            'Between the builds, gone at %s:' % B275,
            'en_US@ampm (deleted)'])
        self.assertNotIn('NOT CHECKED', ' '.join(body))
        self.assertNotIn('--old-node', ' '.join(body))

    def test_the_summary_speaks_of_builds(self):
        """Between the two builds C.UTF-8's rules are compared, and differ:
        step 2 says PostgreSQL will not warn of it, once, and the summary
        repeats it; nothing says it was not compared."""
        out = self.run_of('89')
        summary = flat(out.split('AUDIT SUMMARY', 1)[1])
        self.assertEqual(summary.count(C_DIFFERS + step2.C_NO_VERSION), 1,
                         summary)
        self.assertNotIn('were not compared', summary)
        self.assertEqual(summary_block(out, MEASURED)[:2], [
            "Pass --old-node and --new-node, each machine's file written by",
            'scripts/locale_order.py --extract. Without them nothing above'])
        self.assertNotIn('the TAG', flat(out.split('AUDIT SUMMARY', 1)[1]))
        self.assertNotIn('upstream diff', flat(out.split('AUDIT SUMMARY', 1)[1]))

    def test_rhel9_to_rhel10_says_nothing_of_upstreams_c_utf8(self):
        out = self.run_of('910')
        self.assertNotIn('Not checked for glibc-', out)
        self.assertNotIn('localedata/locales/C ->', out)
        self.assertNotIn('ship their own', flat(out))
        self.assertNotIn('C.UTF-8', summary_block(out, REINDEX))

    def test_no_commit_id_is_printed_for_a_build(self):
        for label in self.RUNS:
            self.assertEqual(_HEX40.findall(self.run_of(label)), [], label)


ONE_TREE = 'glibc-2.34-283.el9_8'
OUTSIDE = 'glibc-2.34-284.el9_8'


@needs_clone
class AuditShOnBuildsOfOneVersion(_AuditOnBuilds, unittest.TestCase):
    """Two builds of one version, made by the test: 283 with 275's tree
    (one source tree, as the real two are), .48 with .40's tree and a
    comment changed in sv_SE, and 284 with a change of
    ld-collate.c and a patch to strcoll's code outside locale/."""

    # B48's check of its charmaps gave a `!!` (no zstd reader: a Python
    # before 3.14 without the zstd program, on an el10 payload), which
    # reaches the summary's warnings, naming the build.
    ZSTD = ('!! charmaps NOT checked against glibc-locale-source: no zstd '
            'reader')
    RUNS = {'tree': (B275, ONE_TREE), 'clean': (B40, B48),
            'outside': (B275, OUTSIDE), 'twice': (B275, B275)}

    @classmethod
    def builds(cls):
        sv = 'localedata/locales/sv_SE'
        ld = 'locale/programs/ld-collate.c'
        t275 = fixture(B275)[5]
        t40 = fixture(B40)[5]
        return {
            B275: (cls.tree(B275, B275), ()),
            ONE_TREE: (cls.tree(ONE_TREE, B275), ()),
            B40: (cls.tree(B40, B40), ()),
            B48: (cls.tree(B48, B40, {sv: t40[sv][0] + b'% a build\n'}), (),
                  [cls.ZSTD]),
            OUTSIDE: (cls.tree(OUTSIDE, B275, {
                ld: t275[ld][0] + b'int pg_glibc_audit_test_line;\n'}),
                [('glibc-example.patch', ['string/strcoll_l.c'])])}

    def test_one_source_tree_compares_nothing_and_says_builds(self):
        out = self.run_of('tree')
        self.assertIn('%s and %s are two commits of one source tree'
                      % (B275, ONE_TREE), flat(out))
        # The summary says it with the order check's `!!` alone, once,
        # which names the builds and what this tool does not read of one
        # (10.1 deleted the block that said it again in other words).
        self.assertEqual(summary_warnings(out).count(flat(
            "!! %s and %s are two commits of one source tree. Step 2 "
            "compares nothing: it would compare that tree against itself. "
            % (B275, ONE_TREE) + g._NOTHING_COMPARED_BUILDS.format(
                noun='builds', one='build'))), 1, out)
        self.assertEqual(summary_block(out, REINDEX),
                         nothing_compared_lines('builds'))
        # Step 2 names the two builds as builds, not as "refs" (16.174).
        step2 = out.split('== STEP 2', 1)[1].partition('== AUDIT SUMMARY')[0]
        self.assertEqual(Step2Report(step2).nothing,
                         'the two builds are two commits of one source tree.')
        assert_no_deleted_block(self, out)
        self.assertNotIn('DIRECTION', out)
        self.assertEqual(deleted_steps_named(out), [])

    def test_a_builds_own_warning_reaches_the_summary(self):
        """A `!!` a build's check gave, printed under 'What was compared'
        naming the build, is repeated among the summary's warnings."""
        out = self.run_of('clean')
        said = '!! %s: charmaps NOT checked against glibc-locale-source: ' \
               'no zstd reader' % B48
        self.assertEqual(flat(compared_text(out)).count(said), 1)
        warnings = self.summary('clean').split(
            '-- Warnings the clean results above do NOT cover', 1)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(flat(warnings[1]).count(said), 1)

    def test_one_build_given_twice_is_one_commit(self):
        """The sides record the build once, so the steps after it find it;
        the summary says one build was given twice, and its ellipsis block
        names no other side (false-negative-reviewer, round 1)."""
        out = self.run_of('twice')
        self.assertEqual(summary_warnings(out).count(
            '%s and %s are the same commit' % (B275, B275)), 1, out)
        self.assertEqual(summary_block(out, REINDEX),
                         nothing_compared_lines('builds'))
        assert_no_deleted_block(self, out)

    def test_a_patch_to_strcoll_outside_locale_is_named_and_not_compared(self):
        """A build's patch to strcoll's own code outside locale/: step 5
        refused its clean sentence over it until step 9.3 (StepFiveReadsABuild
        tested that). The provenance no longer counts such patches: the
        count said strcoll and left localedef's patches out (16.129). The
        summary's warnings say that each build's patches to that code are
        compared by no step; step 2, which reads only the rules, lists
        nothing."""
        out = self.run_of('outside')
        compared = flat(compared_text(out))
        self.assertIn('change localedata/ or locale/, applied in the order '
                      'rpm recorded', compared)
        self.assertNotIn("strcoll's code", compared)
        self.assertNotIn('glibc-example.patch', compared)
        summary = self.summary('outside')
        warnings = flat(summary.split(
            '-- Warnings the clean results above do NOT cover', 1)[1])
        self.assertIn("!! Not compared on any route: the code that compiles "
                      "and compares the rules (localedef's and strcoll's, "
                      "each build's patches to it included).", warnings)
        self.assertEqual(summary_block(summary, REINDEX), [
            'none -- of what step 2 compared, nothing differs; step 2, above,',
            'names what it did not compare'])


@needs_clone
class AuditShOnATagAndABuild(_AuditOnBuilds, unittest.TestCase):
    """A tag on one side and a build on the other: each sentence speaks of
    its side. And a build that cannot be read stops the run when the sides
    are read,
    never replaced by its tag. The new side of tag_build is given as
    latest-el9, which the class's mirror resolves to B275: every sentence
    names the build, none the word given; tag_typed names B275 and its
    summary is tag_build's."""

    RUNS = {'tag_build': (TAG, 'latest-el9'), 'tag_typed': (TAG, B275),
            'build_tag': (B40, TAG), 'absent': (B275, ABSENT),
            'sha_build': (EXPECTED_SHA[TAG], B275),
            'build_sha': (B40, EXPECTED_SHA[TAG])}
    LISTS = {'pub/rocky/9': [('2.34', '275.el9_8')]}

    @classmethod
    def builds(cls):
        return {b: (cls.tree(b, b), ()) for b in (B40, B275)}

    def test_an_old_tag_says_what_it_cannot_show(self):
        out = self.run_of('tag_build')
        self.assertTrue(out.startswith('latest-el9 is %s:' % B275), out[:300])
        self.assertNotIn('latest-el9',
                         without_the_tags_notice(out.split('\n\n', 1)[1]))
        self.assertEqual(' '.join(summary_block(out, REMOVED)),
                         '%s is an upstream tag, so what your old distro adds '
                         'to it is NOT CHECKED: a file only that distro ships, '
                         'and %s does not, is not listed. Name the old side '
                         'as a build to check it. Between the sides: none -- '
                         'no locale file at %s is gone at %s'
                         % (TAG, B275, TAG, B275))
        # The old side is the tag, which holds no file C (16.123).
        self.assertEqual(flat(out.split('AUDIT SUMMARY', 1)[1]).count(
            C_NOT_COMPARED.format(TAG) + step2.C_NO_VERSION), 1)
        # The order check's `!!` is all the summary says of the direction,
        # once, true at the end of the run, naming the kind of the sides.
        warnings = summary_warnings(out)
        self.assertEqual(warnings.count(
            '!! The DIRECTION of this pair could not be established'), 1, out)
        self.assertIn('Nothing in this run has checked it', warnings)
        self.assertIn('Confirm which side is the older one.', warnings)
        self.assertIn('nothing here places a build against a tag of its own '
                      'release', warnings)
        assert_no_deleted_block(self, out)

    def test_a_commit_and_a_build_name_the_commit(self):
        """16.183: the tag's commit given by its id is a commit, in Removed
        and in the order check's `!!`, on either side."""
        sha = EXPECTED_SHA[TAG]
        out = self.run_of('sha_build')
        self.assertEqual(' '.join(summary_block(out, REMOVED)),
                         '%s is an upstream commit, so what your old distro '
                         'adds to it is NOT CHECKED: a file only that distro '
                         'ships, and %s does not, is not listed. Name the old '
                         'side as a build to check it. Between the sides: '
                         'none -- no locale file at %s is gone at %s'
                         % (sha, B275, sha, B275))
        self.assertIn('nothing here places a build against a commit of its '
                      'own release', summary_warnings(out))
        # A 40-character id wraps the sentence over four lines, not three.
        said = ('%s is an upstream commit, so what your new distro adds to '
                'it or drops is NOT CHECKED: a file listed below may still '
                'ship there, and one it drops is not listed. Name the new '
                'side as a build to check it.' % sha)
        body = ' '.join(summary_block(self.run_of('build_sha'), REMOVED))
        self.assertEqual(body[:len(said)], said)

    def test_the_tag_side_alone_gets_the_tags_warning(self):
        """The old side is a tag and the new one a build: the `!!`
        names the tag alone and asks for its build, the summary repeats it,
        and C, which the build ships and the tag lacks, is not compared for
        want of an old file: the C.UTF-8 `!!` alone says so (16.123), and
        that a distro's build can ship its own. Nothing says its order is
        settled (7.7)."""
        out = self.run_of('tag_typed')
        notice = ("!! Not checked for %s: it is an upstream tag, which holds "
                  "none of a distro's own locale files or patches. Step 2 "
                  "reads only what that tag holds. If that machine runs "
                  "RHEL 8, 9 or 10, name that side as its build instead, as "
                  "rpm -q glibc prints it on the machine without the "
                  "architecture (glibc-2.34-283.el9_8), or as latest-el8, "
                  "latest-el9 or latest-el10." % TAG)
        self.assertEqual(flat(out).count(notice), 2, out[-3000:])
        self.assertNotIn('Not checked for %s and' % TAG, out)
        step2, end, _ = out.split('== STEP 2', 1)[1].partition(
            '== AUDIT SUMMARY')
        self.assertTrue(end, 'no summary ends step 2')
        # C is the one file only the build has, and the C.UTF-8 `!!` is
        # what says it, once: no list of added files is printed for it.
        # (`step2` is the step's text here, not the module.)
        import filter_lc_collate_changes as step2_mod
        report = Step2Report(step2)
        self.assertEqual(report.added, {STEP2_C: 'C.UTF-8 !!'})
        self.assertNotIn('Not compared, no file at %s (' % TAG, step2)
        self.assertEqual(flat(step2).count(
            C_NOT_COMPARED.format(TAG) + step2_mod.C_NO_VERSION), 1)
        self.assertNotIn(STEP2_C, report.differs)

    def test_a_build_then_a_tag_does_not_say_the_tag_removes_what_the_distro_ships(self):
        """16.78: over B40 -> TAG the new side is a tag, which cannot show
        what its distro ships, so a file the tag lacks is not said to fail
        on the new system for certain."""
        out = self.run_of('build_tag')
        step2, end, _ = out.split('== STEP 2', 1)[1].partition(
            '== AUDIT SUMMARY')
        self.assertTrue(end, 'no summary ends step 2')
        head = 'Not compared, no file at %s (' % TAG
        deleted = step2.split(head, 1)[1].split('\n\n', 1)[0]
        self.assertEqual(flat(deleted), flat(
            "2), listed under Removed in the summary: C.UTF-8 en_US@ampm "
            "Unless its distro ships the file anyway, which an upstream tag "
            "cannot "
            "show, on the new system a database whose locale is one of "
            "these does not connect, and a collation of one gives an error "
            "when it is used."))
        self.assertNotIn('On the new system, a database', step2)

    def test_a_tag_and_a_build_typed_is_the_tag_and_latest_run(self):
        """Every block of the summary of TAG B275 is tag_build's, four of
        which the test above reads."""
        typed = self.summary('tag_typed')
        self.assertNotIn('latest-el9', without_the_tags_notice(typed))
        self.assertEqual(typed, self.summary('tag_build').replace(
            os.path.join(self.tmp, 'out', 'tag_build'),
            os.path.join(self.tmp, 'out', 'tag_typed')))

    def test_a_new_tag_says_what_it_cannot_show(self):
        out = self.run_of('build_tag')
        body = summary_block(out, REMOVED)
        self.assertEqual(' '.join(body[:3]),
                         '%s is an upstream tag, so what your new distro adds '
                         'to it or drops is NOT CHECKED: a file listed below '
                         'may still ship there, and one it drops is not '
                         'listed. Name the new side as a build to check it.'
                         % TAG)
        self.assertIn('C.UTF-8 (deleted)', body)
        self.assertNotIn('C (deleted)', body)
        # The new side is the tag, which holds no file C (16.123): the
        # `!!` reads each side, not the old one alone.
        self.assertEqual(flat(out.split('AUDIT SUMMARY', 1)[1]).count(
            C_NOT_COMPARED.format(TAG) + step2.C_NO_VERSION), 1)

    def test_a_build_that_cannot_be_read_stops_the_run(self):
        rc, out = self.out['absent']
        self.assertEqual(rc, 2, out[-2000:])
        self.assertIn('cannot read the build ' + ABSENT, out)
        self.assertNotIn('STEP 2', out)
        self.assertNotIn('AUDIT SUMMARY', out)


class AuditShRefusesAMachinesFilesWithABuild(unittest.TestCase):
    """With a build on either side, every option that reads a machine's
    files, but --old-node and --new-node, is refused before the sides are
    read, its
    pair given or not, on the tag's side of a mixed pair too."""

    def refused(self, *args, mirror=None):
        out = tempfile.mkdtemp(prefix='audit_refuse_')
        self.addCleanup(shutil.rmtree, out, True)
        env = dict(os.environ, PG_GLIBC_AUDIT_OUT=out)
        if mirror:
            env['PG_GLIBC_BUILD_MIRROR'] = mirror
        p = subprocess.run(['bash', os.path.join(SCRIPTS_DIR, os.pardir,
                                                 'audit.sh'), *args],
                           capture_output=True, env=env)
        self.assertEqual(p.returncode, 2, p.stderr.decode()[-2000:])
        self.assertNotIn(COMPARED_BANNER, p.stdout.decode())
        self.assertNotIn('provenance', p.stdout.decode())
        self.assertEqual(os.listdir(out), [])
        return flat(p.stderr.decode())

    def test_each_option_is_refused(self):
        for args, build, said in (
                (('--old-order', 'x'), B40, '--old-order is refused'),
                (('--new-order', 'x'), B40, '--new-order is refused'),
                (('--old-order', 'a', '--new-order', 'b'), B40,
                 '--old-order and --new-order are refused')):
            with self.subTest(args=args):
                said_all = self.refused(B40, B275, *args)
                self.assertIn('error: %s is a build, and with a build '
                              "audit.sh reads a machine's files only from "
                              '--old-node and --new-node: %s. Give those '
                              "options with two tags, or each machine's file "
                              'with --old-node and --new-node.'
                              % (build, said), said_all)

    def test_each_machines_file_is_not_refused(self):
        """--old-node and --new-node are read with a build: what stops this
        run is the file, which is not there, not the option."""
        out = tempfile.mkdtemp(prefix='audit_refuse_')
        self.addCleanup(shutil.rmtree, out, True)
        p = subprocess.run(['bash', os.path.join(SCRIPTS_DIR, os.pardir,
                                                 'audit.sh'), B40, B275,
                            '--old-node', 'a', '--new-node', 'b'],
                           capture_output=True,
                           env=dict(os.environ, PG_GLIBC_AUDIT_OUT=out))
        err = flat(p.stderr.decode())
        self.assertEqual(p.returncode, 2, err[-2000:])
        self.assertNotIn('is a build, and with a build', err)
        self.assertIn("the old node's file could not be used", err)

    def test_the_tag_sides_options_are_refused_too(self):
        said = self.refused(TAG, B275, '--old-order', 'x')
        self.assertIn('%s is a build' % B275, said)
        self.assertIn('--old-order is refused', said)

    def test_latest_is_refused_with_them_before_the_mirror_is_asked(self):
        """latest-elN is a build: the options are refused first, with a
        mirror that cannot be read, so nothing asked the network."""
        said = self.refused('latest-el8', B275, '--old-order', 'x',
                            mirror='file:///nowhere/')
        self.assertIn('error: latest-el8 is a build, and with a build '
                      "audit.sh reads a machine's files only from --old-node "
                      'and --new-node: --old-order is refused.', said)
        self.assertNotIn('cannot resolve', said)

    def test_a_latest_that_cannot_be_resolved_stops_the_run(self):
        """A mirror that cannot be read: the run stops before the sides with
        the reason, and no side is read as a tag."""
        said = self.refused('latest-el8', 'latest-el9',
                            mirror='file:///nowhere/')
        self.assertIn('error: cannot resolve latest-el8: could not read '
                      'file:///nowhere/pub/rocky/8/BaseOS/source/tree/'
                      'repodata/repomd.xml', said)


# -- each machine's file names its build (7.6a) -------------------------------

W8, W9 = B40 + '.x86_64', B275 + '.x86_64'


class MachineSides(unittest.TestCase):
    """glibc_locale_data.machine_sides in this process, each rule on its own:
    what the run reads when audit.sh is given each machine's file.
    build_sources.fetch_build is replaced by one that records its calls and
    fails, so that a side read by downloading its build shows."""

    def sides(self, given, words, no_sources=(None, None)):
        calls = []

        def fake(name, *a, **k):
            calls.append(name)
            raise bs.Unread('this test downloads nothing')
        with mock.patch.object(bs, 'fetch_build', fake):
            got = g.machine_sides(given, words, no_sources)
        return got, calls

    def refused(self, given, words, no_sources=(None, None)):
        err = io.StringIO()
        with mock.patch('sys.stderr', err), \
                self.assertRaises(SystemExit) as cm:
            self.sides(given, words, no_sources)
        self.assertEqual(cm.exception.code, 2)
        return flat(err.getvalue())

    def test_the_builds_are_read_in_place_of_the_tags(self):
        """Each side is the build its machine runs, read from the machine's
        own files: nothing is downloaded."""
        (sides, lines), calls = self.sides(('glibc-2.28', 'glibc-2.34'),
                                           (W8, W9))
        self.assertEqual(sides, [(B40, 'build', 'glibc-2.28'),
                                 (B275, 'build', 'glibc-2.34')])
        self.assertEqual(calls, [])
        self.assertEqual(flat('\n'.join(lines)), flat(
            "Each machine's file names the build it runs: step 2 reads "
            "%s and %s, in place of glibc-2.28 and glibc-2.34, from each "
            "machine's own locale sources." % (B40, B275)))

    def test_the_builds_themselves_given_say_nothing_more(self):
        for given in ((B40, B275), (W8, W9), ('glibc-2.28', B275)):
            with self.subTest(given=given):
                (sides, lines), calls = self.sides(
                    tuple(g.without_arch(x) for x in given), (W8, W9))
                self.assertEqual([s[0] for s in sides], [B40, B275])
                self.assertEqual(lines != [], given[0] == 'glibc-2.28')
                self.assertEqual(calls, [])

    def test_two_words_of_one_build_are_that_build(self):
        (sides, _), _ = self.sides(('glibc-2.28', 'glibc-2.34'),
                                   (W8 + ' ' + B40 + '.i686', W9))
        self.assertEqual(sides[0], (B40, 'build', 'glibc-2.28'))

    def test_two_builds_on_one_machine_are_refused(self):
        said = self.refused(('glibc-2.28', 'glibc-2.34'),
                            (W8 + ' ' + B48 + '.i686', W9))
        self.assertIn('two builds, %s and %s' % (B40, B48), said)

    def test_a_word_that_is_no_package_is_refused(self):
        said = self.refused(('glibc-2.28', 'glibc-2.34'), (B40, W9))
        self.assertIn('does not read as a package and its architecture', said)

    def test_a_side_given_that_is_not_the_machines(self):
        for given, side, typed, tag, build in (
                (('glibc-2.34', 'glibc-2.34'), 'old', 'glibc-2.34',
                 'glibc-2.28', B40),
                (('glibc-2.28', B283), 'new', B283, 'glibc-2.34', B275),
                (('glibc-2.28', 'glibc-2.39'), 'new', 'glibc-2.39',
                 'glibc-2.34', B275)):
            with self.subTest(given=given):
                said = self.refused(given, (W8, W9))
                self.assertIn("the %s machine's file names %s, and the %s "
                              "side given is %s: give that build's tag, %s, "
                              "or the build itself." % (side, build, side,
                                                        typed, tag), said)

    def test_the_files_the_wrong_way_round(self):
        said = self.refused(('glibc-2.28', 'glibc-2.34'), (W9, W8))
        self.assertIn('pass the machine you upgrade FROM first', said)

    def test_latest_with_a_machines_file_is_refused(self):
        said = self.refused(('glibc-2.28', 'latest-el9'), (W8, W9))
        self.assertIn('latest-el9 stands for a machine not installed yet', said)

    def test_a_file_without_sources_makes_both_sides_tags(self):
        """One machine's file holds no locale sources (glibc-locale-source
        not installed): its build is not downloaded in their place, so both
        sides read their tags (a tag against a build of one release cannot
        be ordered), and one `!!` names the machine, the file's reason, and
        what the tags lack."""
        (sides, lines), calls = self.sides(
            ('glibc-2.28', 'glibc-2.34'), (W8, W9),
            (None, '/usr/share/i18n/locales does not exist'))
        self.assertEqual(calls, [])
        self.assertEqual(sides, [('glibc-2.28', 'tag', 'glibc-2.28'),
                                 ('glibc-2.34', 'tag', 'glibc-2.34')])
        said = flat('\n'.join(lines))
        self.assertEqual(said.count('!!'), 1)
        self.assertEqual(said, flat(
            "!! The new machine's file names %s, which this run cannot read: "
            "it holds no locale sources, because /usr/share/i18n/locales "
            "does not exist. So step 2 reads the tags glibc-2.28 and "
            "glibc-2.34, which hold neither build's patches." % B275))

    def test_two_files_without_sources_are_both_named(self):
        (sides, lines), _ = self.sides(('glibc-2.28', 'glibc-2.34'),
                                       (W8, W9), ('one', 'two'))
        self.assertEqual([s[1] for s in sides], ['tag', 'tag'])
        said = flat('\n'.join(lines))
        self.assertIn("The old machine's file names %s, which this run cannot "
                      "read: it holds no locale sources, because one; and the "
                      "new machine's file names %s, which this run cannot "
                      "read: it holds no locale sources, because two." %
                      (B40, B275), said)

    def test_a_build_typed_as_itself_without_sources_is_refused(self):
        """Typed by its name, with or without the architecture, a build whose
        machine's file holds no sources stops the run, as a build that
        cannot be read does without a machine's file; typed as its tag, the
        tags are read (above)."""
        for given, no in (((B40, 'glibc-2.34'), ('gone', None)),
                          (('glibc-2.28', W9), (None, 'gone'))):
            with self.subTest(given=given):
                said = self.refused(tuple(g.without_arch(x) for x in given),
                                    (W8, W9), no)
                self.assertIn('cannot read the build %s: it holds no locale '
                              'sources, because gone'
                              % (B40 if given[0] == B40 else B275), said)
        # A build of another distro typed as itself: refused for its name.
        alma = 'glibc-2.39-128.el10_2.alma.1'
        said = self.refused(('glibc-2.34', alma), (W9, alma + '.x86_64'))
        self.assertIn('cannot read the build %s: the name does not read as '
                      'a build of RHEL 8, 9 or 10' % alma, said)

    def test_a_name_that_is_no_rhel_build(self):
        """A build whose name Rocky's would not be (a suffix of another
        distro): its sources are not read as a RHEL build's, so the tags are
        read, and nothing is downloaded for either side."""
        alma = 'glibc-2.39-128.el10_2.alma.1'
        (sides, lines), calls = self.sides(
            ('glibc-2.34', 'glibc-2.39'), (W9, alma + '.x86_64'))
        self.assertEqual([s[1] for s in sides], ['tag', 'tag'])
        self.assertEqual(calls, [])
        self.assertIn(alma, flat('\n'.join(lines)))

    def test_a_major_whose_tables_are_not_held(self):
        """A RHEL 7 machine (the build reads as one, and glibc-2.17 is a tag
        of the clone): its localedef's keyword tables are not among those
        this tool holds (glibc_reader.RHEL_BUILDS), so its sources are not
        read with the tag's alone, and both sides read their tags with the
        `!!`; typed as the build itself, it is refused."""
        el7 = 'glibc-2.17-326.el7_9.3'
        (sides, lines), calls = self.sides(('glibc-2.17', 'glibc-2.28'),
                                           (el7 + '.x86_64', W8))
        self.assertEqual(sides, [('glibc-2.17', 'tag', 'glibc-2.17'),
                                 ('glibc-2.28', 'tag', 'glibc-2.28')])
        self.assertEqual(calls, [])
        self.assertIn("The old machine's file names %s, which this run "
                      "cannot read: the name does not read as a build of "
                      "RHEL 8, 9 or 10" % el7, flat('\n'.join(lines)))
        said = self.refused((el7, 'glibc-2.28'), (el7 + '.x86_64', W8))
        self.assertIn('cannot read the build %s: the name does not read as '
                      'a build of RHEL 8, 9 or 10' % el7, said)


class WithoutTheArchitecture(unittest.TestCase):
    """A build typed as `rpm -q glibc` prints it (backlog 16.79), and
    resolve, which audit.sh reads every side through."""

    def test_each_shape(self):
        for name, bare in ((W9, B275), (B275, B275), ('glibc-2.34', 'glibc-2.34'),
                           ('glibc-2.39-46.el10_0', 'glibc-2.39-46.el10_0'),
                           ('glibc-2.39-46.el10_0.4.x86_64',
                            'glibc-2.39-46.el10_0.4'),
                           (B40, B40), ('latest-el9', 'latest-el9')):
            with self.subTest(name=name):
                self.assertEqual(g.without_arch(name), bare)

    def test_resolve_writes_the_build_without_it(self):
        with tempfile.TemporaryDirectory() as d:
            to = os.path.join(d, 'r.txt')
            with mock.patch('sys.stdout', io.StringIO()):
                self.assertEqual(g._main(['resolve', '--to', to, W8, 'glibc-2.34']),
                                 0)
            with open(to) as fh:
                self.assertEqual(fh.read(), '%s\tbuild\nglibc-2.34\ttag\n' % B40)


MACHINES = "Each machine's file names the build it runs: step 2 reads"


@needs_clone
class AuditShReadsEachMachinesBuild(_AuditOnBuilds, unittest.TestCase):
    """audit.sh glibc-2.28 glibc-2.34 with each machine's file, which holds
    the fixture build's locale sources and charmaps: the run reads
    each machine's own files (C.UTF-8 joins step 2's list, as between
    the builds), and download nothing, so with the builds' cache empty and
    no mirror the run is the same; Removed reads step 2's list, under a
    sentence of machines; a file without sources makes both sides read
    their tags, with the `!!`."""

    RUNS = {}

    @classmethod
    def builds(cls):
        return {b: (cls.tree(b, b), ()) for b in (B40, B275)}

    # What a distro's patch inside LC_COLLATE looks like, the change
    # test_wrapper.WrapperNodePatchedInsideCollate makes: by injection,
    # since no fixture build differs from its tag in a compared locale.
    INSIDE = ('reorder-after <U0076>\n'
              '<U0077> <U0076>;<BASE>;<MIN>;IGNORE\n')

    @classmethod
    def machine_file(cls, build, measurement, inside=()):
        """A machine's file as --extract writes it, with locale_order's own
        writer: the real measurement of that machine (cut to a few
        locales), the build's own locale sources and its charmaps, gzipped
        as glibc-locale-source installs them in /usr/share/i18n/charmaps,
        which is what a machine of that build holds (measured equal on six
        builds, 7.2)."""
        import locale_order as lom
        base = tempfile.mkdtemp(prefix='audit_machine_file_')
        cls.made.append(base)
        root = os.path.join(base, 'locales')
        charmaps = os.path.join(base, 'charmaps')
        for path, (data, _) in fixture(build)[5].items():
            for prefix, into, gz in (('localedata/locales/', root, False),
                                     ('localedata/charmaps/', charmaps, True)):
                if path.startswith(prefix):
                    full = os.path.join(into, path[len(prefix):])
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    if gz:
                        with gzip.open(full + '.gz', 'wb') as fh:
                            fh.write(data)
                        continue
                    if path[len(prefix):] in inside:
                        at = re.search(rb'(?m)^LC_COLLATE\n', data)
                        assert at, path
                        data = (data[:at.end()] + cls.INSIDE.encode('ascii')
                                + data[at.end():])
                    with open(full, 'wb') as fh:
                        fh.write(data)
        with open(measurement, encoding='ascii') as fh:
            text = fh.read()
        data, _ = lom.archive(text, json.loads(text), root, charmaps)
        out = os.path.join(base, build + '.tar.gz')
        with open(out, 'wb') as fh:
            fh.write(data)
        return out

    @classmethod
    def odd_codeset(cls, measurement, name):
        """A copy of `measurement` whose `name` was measured in an encoding
        no charmap declares, resealed as test_locale_order.trimmed reseals
        a cut one."""
        import locale_order as lom
        with open(measurement, encoding='ascii') as fh:
            data = json.load(fh)
        # Both records of the encoding, as one glibc reads it for the order
        # and for how PostgreSQL uses the locale (locale_order.load holds
        # the two to each other).
        data['locales'][name]['codeset'] = 'X-ADMINS-OWN'
        data['postgres_use'][name]['codeset'] = 'X-ADMINS-OWN'
        data['content'] = lom.content_digest(data)
        base = tempfile.mkdtemp(prefix='audit_machine_odd_')
        cls.made.append(base)
        path = os.path.join(base, os.path.basename(measurement))
        with open(path, 'w', encoding='ascii') as fh:
            fh.write(json.dumps(data, sort_keys=True) + '\n')
        return path

    @classmethod
    def tearDownClass(cls):
        for d in cls.made:
            shutil.rmtree(d, True)
        # test_wrapper's helpers made the files without sources and the cut
        # measurements; its tearDownModule removes them, and never runs in
        # a process that runs this class alone.
        import test_wrapper
        test_wrapper.tearDownModule()
        super().tearDownClass()

    @classmethod
    def setUpClass(cls):
        from test_wrapper import node_file, cut_few
        from test_locale_order import RHEL8, RHEL9
        cls.made = []
        # The old machine's de_DE and sv_SE carry a change inside
        # LC_COLLATE (INSIDE), so that step 6's block names them; the other
        # locales are the build's, C among them.
        # The old machine's el_CY is measured in an encoding no charmap on
        # that machine declares, as a locale built with a charmap of the
        # admin's own would be: step 2 names it as not traced, never drops
        # it.
        old = cls.machine_file(B40, cls.odd_codeset(cut_few(RHEL8), 'el_CY'),
                               inside=('de_DE', 'sv_SE'))
        new = cls.machine_file(B275, cut_few(RHEL9))
        # Machines without glibc-locale-source: their files hold no
        # sources, and both sides read their tags.
        bare_old = node_file('old.tar', cut_few(RHEL8), 'glibc-2.28',
                             sources=False)
        bare_new = node_file('new.tar', cut_few(RHEL9), 'glibc-2.34',
                             sources=False)
        cls.RUNS = {'read': ('glibc-2.28', 'glibc-2.34', '--old-node', old,
                             '--new-node', new),

                    'old_bare': ('glibc-2.28', 'glibc-2.34', '--old-node',
                                 bare_old, '--new-node', new),
                    'new_bare': ('glibc-2.28', 'glibc-2.34', '--old-node',
                                 old, '--new-node', bare_new),
                    'bare': ('glibc-2.28', 'glibc-2.34', '--old-node',
                             bare_old, '--new-node', bare_new)}
        super().setUpClass()
        # The same files with no build to read: an empty cache, no mirror.
        out = os.path.join(cls.tmp, 'out', 'unread')
        os.makedirs(out)
        env = dict(os.environ, PG_GLIBC_AUDIT_OUT=out,
                   PG_GLIBC_BUILD_SOURCES_CACHE=os.path.join(cls.tmp, 'none'),
                   PG_GLIBC_BUILD_MIRROR='file:///nowhere/',
                   PG_GLIBC_READER_CACHE=os.path.join(SCRIPTS_DIR,
                                                      'glibc-reader-cache'))
        env.pop('PG_GLIBC_AUDIT_SIDES', None)
        p = subprocess.run(['bash', os.path.join(cls.tmp, 'audit', 'audit.sh'),
                            *cls.RUNS['read']], cwd=cls.tmp,
                           capture_output=True, env=env)
        # Kept apart from cls.out: the same run with nothing to download,
        # which test_nothing_is_downloaded compares with cls.out['read'].
        cls.unread = (p.returncode,
                      (p.stdout + p.stderr).decode('utf-8', 'replace'))

    def test_the_run_reads_each_machines_own_sources(self):
        """Each machine's file holds the build's locale sources and
        charmaps: the run reads them, as one commit of the clone per
        machine, and says so in the provenance under 'What was compared';
        nothing is rebuilt from an SRPM. C.UTF-8 joins step 2's list, as
        between the builds."""
        out = self.run_of('read')
        self.assertEqual(flat(before_compared(out)), flat(
            MACHINES + " %s and %s, in place of glibc-2.28 and glibc-2.34, "
            "from each machine's own locale sources. "
            '================================================================'
            % (B40, B275)))
        compared = compared_text(out)
        self.assertIn('Machine provenance', compared)
        self.assertNotIn('Build provenance', compared)
        self.assertNotIn('SRPM', compared)
        for build, word in ((B40, W8), (B275, W9)):
            with self.subTest(build=build):
                self.assertIn('rpm -q glibc   %s' % word, compared)
        self.assertIn('The sort rules localedef builds for each locale at %s '
                      'and at %s,' % (B40, B275), flat(out))
        self.assertIn('AUDIT SUMMARY  %s -> %s' % (B40, B275), out)
        self.assertIn('C.UTF-8', summary_block(out, REINDEX))
        # 16.169: what C.UTF-8 is, and that PostgreSQL compares exactly C
        # and POSIX without glibc, under the list that names it, on this
        # route too.
        self.assertIn('C.UTF-8 is glibc\'s locale file C, written C in the '
                      'full list; C.utf8 and C.UTF-8 are its spellings. A '
                      'collation or a database whose locale is exactly C or '
                      'POSIX is compared by PostgreSQL itself, without glibc.',
                      ' '.join(summary_block(out, REINDEX)))
        self.assertIn("This list is from each machine's own locale sources.",
                      flat(self.summary('read')))
        self.assertNotIn('This list is from the builds.',
                         flat(self.summary('read')))
        # Which locales each machine builds with UTF-8 comes from its
        # measurement, not from the tags' SUPPORTED, which lists no
        # C.UTF-8: the measured C.utf8 build is compared, by its measured
        # name.
        # Step 2 alone, cut at both ends: a missing banner would widen the
        # cut to the rest of the run in silence.
        self.assertEqual(out.count('== STEP 2'), 1)
        self.assertEqual(out.count('== STEP 11'), 1)
        step2 = out.split('== STEP 2', 1)[1].split('== STEP 11', 1)[0]
        # C, whose RHEL 9 file uses codepoint_collation, a keyword only the
        # build's table holds: read with that table (glibc_tree.build_at by
        # the build's name), its rules are compared and differ in 12
        # characters, as between the builds.
        self.assertIn('Rules differ', step2)
        self.assertIn('C.UTF-8 12 character(s) named by the lines',
                      flat(step2.split('Rules differ', 1)[1]))
        self.assertIn('C.utf8 (UTF-8): 1546 characters, named in C', step2)
        # The character set check names where its builds come from.
        self.assertIn('measured on both machines', flat(step2))
        self.assertNotIn('in SUPPORTED at both', flat(step2))
        # SUPPORTED's note is not printed of a machine, and a file only the
        # new machine has is spelt as measured there: the measurement
        # lists none of these.
        self.assertNotIn('Not listed in localedata/SUPPORTED', flat(step2))
        for name in ('ckb_IQ', 'mnw_MM'):
            with self.subTest(added=name):
                self.assertIn('  %s -> (not measured on that machine)\n'
                              % name, step2)
        # A name measured in an encoding no charmap declares is named.
        self.assertIn('1 locale name(s) measured on %s could not be traced '
                      'to the charmap they are built with, so their character '
                      'set is not compared: el_CY: no charmap on the machine '
                      'declares its encoding X-ADMINS-OWN' % B40, flat(step2))

    def test_nothing_is_downloaded(self):
        """The same files with no build to read (an empty build cache, no
        mirror): the run is the same run, line for line from 'What was
        compared' to the end, since a machine's own sources need no
        download."""
        rc, out = self.unread
        self.assertEqual(rc, 0, out[-3000:])
        read = self.run_of('read')

        def body(text, label):
            text = text.replace(os.path.join(self.tmp, 'out', label), 'OUT')
            return after_compared(text)
        self.assertEqual(body(out, 'unread'), body(read, 'read'))
        self.assertNotIn('which this run cannot read', out)

    def test_a_removed_locale_says_what_postgresql_does_with_it(self):
        """16.122: the new machine's sources lack en_US@ampm, and step 2
        says what PostgreSQL does on that machine with a database or a
        collation of it (postinit.c and pg_locale, REL_14_24, REL_18_6),
        not what an index does; and not the tag's caveat, which a
        machine does not need."""
        out = self.run_of('read')
        step2 = flat(out.split('== STEP 2', 1)[1].split(
            '== AUDIT SUMMARY', 1)[0])
        self.assertIn('en_US@ampm On the new system, a database whose '
                      'locale is one of these does not connect, and a '
                      'collation of one gives an error when it is used.',
                      step2)
        self.assertNotIn('Unless its distro ships the file', step2)

    def test_removed_reads_step_2_under_a_sentence_of_machines(self):
        """With machines, Removed is step 2's list between the two machine
        commits, under a sentence that says what a machine's files are:
        checked against no package, never "checked one by one" (that is a
        build's)."""
        summary = self.summary('read')
        body = flat(' '.join(summary_block(summary, REMOVED)))
        self.assertTrue(body.startswith(
            "Each machine's locale files are its own locale sources as its "
            "file holds them, checked against no package ('Not checked for "
            "these machines', below)."), body)
        self.assertNotIn('checked one by one', body)
        self.assertIn('en_US@ampm (deleted)', body)
        self.assertNotIn('NOT REPORTED', summary)
        self.assertNotIn('-- Distro patches', summary)
        self.assertNotIn('-- Node-to-node', summary)

    def test_a_machines_own_patch_read_in_its_build(self):
        """The old machine's de_DE and sv_SE differ from glibc-2.28 inside
        LC_COLLATE, a patch of that machine's own: step 2, which reads the
        machine's files and not the build's -- not "the tag diff", which a
        run on builds has not -- finds their rules changed against the new
        machine and lists them, with what copies them."""
        summary = self.summary('read')
        self.assertNotIn('tag diff is NOT reading', flat(summary))
        reindex = summary_block(summary, REINDEX)
        for name in ('de_DE', 'sv_SE', 'C.UTF-8'):
            with self.subTest(name=name):
                self.assertIn(name, reindex)

    def test_a_machine_without_its_sources(self):
        """The old machine's file holds no sources (glibc-locale-source not
        installed): no build is downloaded in its place, so the run reads
        both tags, and one `!!` before 'What was compared' says which
        machine and
        the file's reason; Removed points to it, and the tags' C.UTF-8 `!!`
        follows."""
        out = self.run_of('old_bare')
        before = flat(before_compared(out))
        self.assertTrue(before.startswith(
            "!! The old machine's file names %s, which this run cannot read: "
            "it holds no locale sources, because" % B40), before)
        self.assertIn('So step 2 reads the tags glibc-2.28 and '
                      "glibc-2.34, which hold neither build's patches.",
                      before)
        self.assertNotIn(B275 + ', which', before)
        self.assertIn('AUDIT SUMMARY  glibc-2.28 -> glibc-2.34', out)
        self.assertNotIn('Machine provenance', out)
        summary = self.summary('old_bare')
        self.assertIn("A machine's file made both sides read their tags; "
                      "why is the first warning below.",
                      flat(' '.join(summary_block(summary, REMOVED))))
        self.assertEqual(flat(summary).count(
            C_NOT_COMPARED.format('glibc-2.28 and glibc-2.34').replace(
                'holds', 'hold') + step2.C_NO_VERSION), 1)
        warned = flat(summary.split('-- Warnings the clean results above do '
                                    'NOT cover', 1)[1])
        self.assertIn("!! The old machine's file names %s, which this run "
                      "cannot read: it holds no locale sources" % B40, warned)
        self.assertNotIn('NOT REPORTED', summary)

    def test_two_machines_without_their_sources(self):
        out = self.run_of('bare')
        before = flat(before_compared(out))
        for side, build in (('old', B40), ('new', B275)):
            with self.subTest(side=side):
                # The first is the sentence's start, capitalised.
                self.assertIn(("the %s machine's file names %s, which this "
                               "run cannot read: it holds no locale sources"
                               % (side, build)).lower(), before.lower())
        summary = self.summary('bare')
        self.assertIn("A machine's file made both sides read their tags; "
                      "why is the first warning below.",
                      flat(' '.join(summary_block(summary, REMOVED))))


if __name__ == '__main__':
    unittest.main()
