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
rpm-tree.exec). specs/ holds the spec of the other four builds the
catalogues were made from; variants/ what real rpm did with ten inputs
that a reading of the spec in place of rpm's record accepted with a tree
other than rpm's, and the files those inputs add. A patch the fixture does not record
is handed in empty: it touches neither folder, which the tree's equality
with rpm's proves for the three builds.
"""
import functools
import gzip
import hashlib
import io
import json
import lzma
import os
import struct
import subprocess
import tarfile
import tempfile
import shutil
import unittest
from unittest import mock

from _harness import (GLIBC_CLONE, SCRIPTS_DIR, TESTS_DIR, flat,
                      glibc_shaped, needs_clone)

import build_sources as bs

DATA = os.path.join(TESTS_DIR, 'build_sources')
EXTRACT = os.path.join(TESTS_DIR, 'locale_order')
FIXTURES = (('glibc-2.28-251.el8_10.40', 8), ('glibc-2.34-275.el9_8', 9),
            ('glibc-2.39-128.el10_2', 10))
EL9 = 'glibc-2.34-275.el9_8'
OTHER_SPECS = ('glibc-2.28-251.el8_10.48', 'glibc-2.34-168.el9_6.24',
               'glibc-2.34-283.el9_8', 'glibc-2.39-145.el10_2')


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
        self.assertIn('the code strcoll runs', prov['not_checked'][0])

    def test_patches_to_the_collation_code_are_listed(self):
        """Per build, every recorded patch that names a file of step 5's
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
    spec of the seven builds, each real spec without its entries is
    refused naming them, and nothing else is in them."""

    def heads(self):
        out = {}
        for build, _ in FIXTURES:
            out[build] = bs.spec_text(fixture(build)[2])
        for build in OTHER_SPECS:
            out[build] = other_spec(build)
        return out

    def test_the_lua_blocks_and_the_macro_names(self):
        lua, names = set(), set()
        for build, text in self.heads().items():
            lines = text.split('\n')
            head = '\n'.join(lines[:bs.prep_span(lines)[1]])
            lua.update(hashlib.sha256(b.encode()).hexdigest()
                       for _, _, b in bs._lua_blocks(text))
            names.update(m.group(3) for m in bs._DEFINES.finditer(head))
        self.assertEqual(lua, set(bs.LUA_CATALOGUE))
        self.assertEqual(names, set(bs.MACRO_NAMES))
        self.assertEqual((len(lua), len(names)), (15, 36))

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
        self.assertIn(why, str(cm.exception))

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

    def test_the_cache_key_follows_diff_collation_code(self):
        other = os.path.join(self.tmp, 'other.py')
        with open(other, 'w') as fh:
            fh.write('# another list\n')
        before = bs._key()
        with mock.patch.object(bs.dcc, '__file__', other):
            self.assertNotEqual(bs._key(), before)


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
        self.assertIn(why, str(cm.exception))

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
                 (urllib.error.URLError('timed out'), bs.Refused, 'timed out'),
                 (Resp(b'abc', '10'), bs.Refused, 'cut short: 3 of 10'))
        for answer, exc, why in cases:
            with self.subTest(why=why or '404'):
                kw = {'return_value': answer} if isinstance(answer, Resp) \
                    else {'side_effect': answer}
                with mock.patch.object(bs.urllib.request, 'urlopen', **kw):
                    with self.assertRaises(exc) as cm:
                        bs._get('https://mirror.test/x')
                self.assertIn(why, str(cm.exception))
        with mock.patch.object(bs.urllib.request, 'urlopen',
                               return_value=Resp(b'abc', '3')):
            self.assertEqual(bs._get('https://mirror.test/x'), b'abc')
        # A mirror named without its scheme: urllib cannot open it.
        with self.assertRaises(bs.Refused) as cm:
            bs._get('dl.rockylinux.org/vault/rocky/')
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
        commit build_commit made however it is spelt: the id step 1 prints,
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
        # The controls: tags pass, and step 1's fan-in reads the build.
        self.assertEqual(set(g.check_refs(self.repo, TAG, 'glibc-2.28')),
                         {TAG, 'glibc-2.28'})
        self.assertEqual(g.check_refs(self.repo, commit, builds=True),
                         {commit: commit})

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
    """side(named=...) is a build, and step 1 reads one: fetched (here
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
                          a.paths, a.ids, ())
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

    def test_step_1_reads_a_build_by_its_commit(self):
        """audit-locale-diff.sh on two builds, in a folder whose glibc is a
        repository of the test's own: the provenance of each build, the
        diff and the fan-in read at the builds' commits, the fan-in named
        by the build. The new build changes one locale file."""
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
                   PG_GLIBC_BUILD_MIRROR=mirror, PG_GLIBC_AUDIT_WRAPPED='',
                   PG_GLIBC_AUDIT_OUT=os.path.join(self.dir, 'out'))
        step1 = os.path.join(here, 'audit-locale-diff.sh')
        p = subprocess.run(['bash', step1, B275, B283], capture_output=True,
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
        self.assertIn('Locale files added, modified or deleted: 1\n', out)
        self.assertIn('Collation templates by blast radius at %s ' % B283,
                      out)
        self.assertIn("!! Not checked for these builds:", out)
        self.assertIn("checked above against the upstream tag",
                      flat(out))
        self.assertNotIn('DIRECTION', p.stderr.decode())
        # A `!!` a build's check gave reaches the run, naming the build.
        self.assertIn('!! %s: charmaps NOT checked against '
                      'glibc-locale-source: no zstd reader' % B283, flat(out))
        # No hint names a step that reads two tags.
        self.assertNotIn('Next:', out)
        # The commit step 1 made, given back to it as if it were a tag, on
        # either side.
        for pair in ((TAG, commits[1][:12]), (commits[1][:12], TAG)):
            with self.subTest(pair=pair):
                p = subprocess.run(['bash', step1, *pair],
                                   capture_output=True, env=env)
                self.assertEqual(p.returncode, 2)
                self.assertIn('%s is a commit this tool made for a distro '
                              'build' % commits[1][:12],
                              flat(p.stderr.decode()))
                self.assertNotIn('Locale files added', p.stdout.decode())

    def test_a_clone_without_the_builds_tag_fetches_it(self):
        """A clone older than the build lacks the tag it is made on: step 1
        fetches tags for it as it does for a tag argument, instead of
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
            _, a, b = self.g.step1_sides(here, B275, B283)
        self.assertIn('Refs not present locally: %s -- fetching tags...'
                      % TAG, out.getvalue())
        self.assertEqual(_git_out(repo, 'rev-parse', TAG + '^{commit}'), tag)
        self.assertEqual((a.kind, b.kind), ('build', 'build'))

    def test_audit_sh_refuses_a_build_before_step_1(self):
        for pair in ((TAG, B275), (B275, 'glibc-2.39')):
            with self.subTest(pair=pair):
                p = subprocess.run(
                    ['bash', os.path.join(SCRIPTS_DIR, os.pardir,
                                          'audit.sh'), *pair],
                    capture_output=True,
                    env=dict(os.environ, PG_GLIBC_AUDIT_OUT=self.dir))
                self.assertEqual(p.returncode, 2)
                self.assertIn('%s is a build. audit.sh does not read a '
                              'build yet' % B275, flat(p.stderr.decode()))
                self.assertNotIn('STEP 1', p.stdout.decode())


if __name__ == '__main__':
    unittest.main()
