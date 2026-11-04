"""One glibc build's localedata/ and locale/, rebuilt from its source RPM and
checked against the build's binary packages.

A RHEL or Rocky glibc build is upstream glibc plus the distro's patches,
applied by rpmbuild's %prep. build() opens the build's SRPM, takes the inner
tarball's localedata/ and locale/, applies the patches that touch them the
way GNU patch does, and keeps the result in a cache. Steps 1 to 5 of
the audit, each run on its own, read a build named on its command line through
glibc_locale_data.side (build_side); audit.sh refuses one so far.

It does not work out from the spec what rpm did; it reads what rpm wrote
down, and checks against it:

  - rpm_record(): the order of the patches is the PATCH list rpm wrote into
    the SRPM's header, reversed. rpm puts each Patch it reads at the head of
    a list and writes the header from that list (rpm-4.14.3
    build/parsePreamble.c:264-265, build/parseSpec.c:566; 4.16.1.3 :336-337,
    :688; 4.19.1.1 :313-314, :690), whatever wrote the Patch line: the spec,
    a macro, Lua, patch-git. Measured against rpmbuild -bp on rpm 4.14.3,
    4.16.1.3 and 4.19.1.1.
  - verify_against_binaries(): the result is compared with what rpm built
    from the same sources: the sha256 of every locale source in the header
    of the build's glibc-locale-source, and locale.alias in glibc-common's
    header; and every charmap in glibc-locale-source's payload, when this
    Python (3.14 or later) or a zstd program can decompress it -- el9 and
    el10 packages are zstd -- else a `!!` line says the charmaps were not
    checked.
  - known_only(): for what neither record covers (localedata/SUPPORTED and
    locale/ come from the SRPM's tarball, whose bytes its digests stand
    for, and its patches), the spec, what %prep runs and the shape of every
    patch are accepted only as they are in the seven real builds this was
    measured on, anything else refused, naming it; a Lua Source the spec runs
    is read only as NOT_CHECKED says.

Whatever this cannot read the way rpm and GNU patch would, it refuses with
the reason (Refused), and a refusal is never kept in the cache: a tree
rebuilt from a guess would compare as that build's sources, and the tool
would report on a build nobody made. It never falls back to the upstream tag.

What it does not check is listed in every result: NOT_CHECKED, never a
recipe, and the `!!` lines of `verified`; and, for a fetched build,
provenance['fetched'].

fetch_build() takes a build's name instead of its files: where_from()
finds the SRPM and the two binary packages in a mirror's public metadata
(repomd.xml, then primary.xml), fetch() downloads each and compares it with
the sha256 the metadata records, and build() does the rest; given the
glibc clone, build() also compares the SRPM's tarball with the upstream tag
(tarball_is_tag). latest() names a major version's newest build.

build_commit() makes a build a commit of the clone, again on every call: one
parent, the upstream tag, and the tag's tree with localedata/ and locale/
the build's, under build_ref(), outside refs/tags.

PG_GLIBC_BUILD_SOURCES_CACHE names the cache directory (default
scripts/build-sources-cache/, outside git), and PG_GLIBC_BUILD_MIRROR the
mirror (default Rocky Linux's, ROCKY).
"""
import bz2
import collections
import fnmatch
import functools
import gzip
import hashlib
import http.client
import io
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import diff_collation_code as dcc
import glibc_locale_data as g

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(_HERE, 'build-sources-cache')

SCOPE = ('localedata/', 'locale/')

NOT_CHECKED = (
    'the compiler, and the code strcoll runs: the patches of this build '
    'that change that code are named with its patches, and what the build '
    'compiled is not read',
    "the SRPM's and the binary packages' GPG signatures (only rpm's own "
    'digests are checked)',
    'the host that built the binary packages is taken to read the spec as '
    'the host that built the SRPM did (its architecture, its rpm and '
    'redhat-rpm-config macros)',
    'the Lua rpm runs while it reads the spec beyond the blocks of the '
    'spec, which are pinned: a Lua Source such as patch-git.lua, which '
    'changes in every build, is read only for the ways of changing '
    "macros, the environment or rpm's table of patches the seven "
    'builds were measured without',
    'localedata/SUPPORTED and locale/ are in no binary package: they are the '
    "SRPM's tarball and its patches; the tarball is compared with the "
    "upstream tag only when the build is made with the clone (provenance "
    "'tarball')",
)

# provenance['tarball'] when build() was given the clone and the SRPM's
# tarball is the upstream tag in localedata/, locale/ and intl/locale.alias,
# byte for byte and executable bit: so in the seven real builds, measured
# 2026-11-03. Anything else is refused.
TARBALL_IS_TAG = "the SRPM's tarball is %s in localedata/ and locale/"

# The rpm versions the seven real builds were made with, SRPMs and binary
# packages alike, and whose sources these rules were read in.
KNOWN_RPM = ('4.14.3', '4.16.1.3', '4.19.1.1')


class Refused(Exception):
    """These sources cannot be rebuilt the way rpm would; the message says
    why."""


# -- rpm files ---------------------------------------------------------------

_NAME, _VERSION, _RELEASE = 1000, 1001, 1002
_SOURCE, _PATCH = 1018, 1019
_FILEDIGESTS, _SOURCERPM, _RPMVERSION = 1035, 1044, 1064
_DIRINDEXES, _BASENAMES, _DIRNAMES = 1116, 1117, 1118
_PAYLOADFORMAT, _PAYLOADCOMPRESSOR = 1124, 1125
_FILEDIGESTALGO = 5011
_PAYLOADDIGEST, _PAYLOADDIGESTALGO, _PAYLOADDIGESTALT = 5092, 5093, 5097
_SIG_SHA256 = 273
_HEADER_MAGIC = b'\x8e\xad\xe8\x01\x00\x00\x00\x00'
_OPENERS = {'gzip': gzip.GzipFile, 'xz': lzma.LZMAFile, 'lzma': lzma.LZMAFile,
            'bzip2': bz2.BZ2File}


def _header(f, pad):
    """One rpm header: {tag: value} and its bytes (lib/header.c@4.14.3)."""
    magic = f.read(16)
    if len(magic) != 16 or magic[:8] != _HEADER_MAGIC:
        raise Refused('not an rpm header (magic %r)' % magic[:8])
    nindex, hsize = struct.unpack('>II', magic[8:])
    if nindex > 65535 or hsize > 0x0fffffff:
        raise Refused('rpm header larger than rpm allows')
    index, store = f.read(16 * nindex), f.read(hsize)
    if len(index) != 16 * nindex or len(store) != hsize:
        raise Refused('rpm header cut short')
    tags = {}
    for i in range(nindex):
        tag, typ, off, cnt = struct.unpack('>IIII', index[16 * i:16 * i + 16])
        if off > hsize:
            raise Refused('rpm header entry outside the header')
        if typ in (6, 8, 9):            # STRING, STRING_ARRAY, I18NSTRING
            vals, p = [], off
            for _ in range(1 if typ == 6 else cnt):
                end = store.find(b'\0', p)
                if end < 0:
                    raise Refused('rpm header string without its end')
                vals.append(store[p:end].decode('utf-8', 'replace'))
                p = end + 1
            tags[tag] = vals[0] if typ == 6 else vals
        elif typ == 4:                  # INT32
            if off + 4 * cnt > hsize:
                raise Refused('rpm header entry outside the header')
            tags[tag] = list(struct.unpack('>%dI' % cnt,
                                           store[off:off + 4 * cnt]))
    if pad:
        f.read((8 - (16 * nindex + hsize) % 8) % 8)
    return tags, magic + index + store


def _read_rpm(path, kind):
    """(header, payload bytes) of an rpm whose lead says `kind` (1 source,
    0 binary), after the signature's sha256 of the header."""
    with open(path, 'rb') as f:
        lead = f.read(96)
        if lead[:4] != b'\xed\xab\xee\xdb':
            raise Refused('%s is not an rpm' % path)
        if struct.unpack('>H', lead[6:8])[0] != kind:
            raise Refused('%s is not a %s rpm'
                          % (path, 'source' if kind == 1 else 'binary'))
        sig, _ = _header(f, pad=True)
        hdr, blob = _header(f, pad=False)
        payload = f.read()
    if sig.get(_SIG_SHA256) != hashlib.sha256(blob).hexdigest():
        raise Refused("%s: the header's SHA256 is missing or wrong" % path)
    digest = hdr.get(_PAYLOADDIGEST)
    if hdr.get(_PAYLOADDIGESTALGO) != [8] or not digest or \
            digest[0] != hashlib.sha256(payload).hexdigest():
        raise Refused("%s: the payload's SHA256 is missing or wrong (cut "
                      'short, or another file)' % path)
    if hdr.get(_PAYLOADFORMAT) != 'cpio':
        raise Refused('%s: payload format %r'
                      % (path, hdr.get(_PAYLOADFORMAT)))
    return hdr, payload


def _cpio(f, regular_only=True):
    """The payload's files, (name, bytes), from a newc archive. rpm writes a
    file shared by hardlinks once, under its last name, and empty under the
    others (lib/rpmfi.c:1994-2052@4.14.3), so a link count above one is
    refused, never read as an empty file. With regular_only False, entries
    that are not regular files (a binary package's directories) are
    skipped; with it True they are refused."""
    pos = [0]

    def rd(n):
        b = f.read(n)
        if len(b) != n:
            raise Refused('the payload is cut short')
        pos[0] += n
        return b

    def align():
        rd((4 - pos[0] % 4) % 4)

    while True:
        h = rd(110)
        if h[:6] not in (b'070701', b'070702'):
            raise Refused('payload is not a newc cpio archive (%r)' % h[:6])
        fld = [int(h[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        mode, nlink, size, namesize = fld[1], fld[4], fld[6], fld[11]
        name = rd(namesize)[:-1].decode('utf-8', 'replace')
        align()
        data = rd(size)
        align()
        if name == 'TRAILER!!!':
            return
        if name.startswith('./'):
            name = name[2:]
        if (mode & 0o170000) != 0o100000:
            if regular_only:
                raise Refused('payload file %r is not a regular file' % name)
            continue
        if nlink != 1:
            raise Refused('payload file %r is one of %d hardlinks'
                          % (name, nlink))
        yield name, data


def read_srpm(path):
    """(info, header, {name: bytes}) of a source RPM, after rpm's own two
    digests."""
    hdr, payload = _read_rpm(path, 1)
    comp = hdr.get(_PAYLOADCOMPRESSOR)
    if comp not in _OPENERS:
        raise Refused('SRPM payload compressed with %r, which Python cannot '
                      'read without a module outside its standard library'
                      % comp)
    files = {}
    for name, data in _cpio(_OPENERS[comp](fileobj=io.BytesIO(payload))):
        if '/' in name or name in ('', '.', '..'):
            raise Refused('unexpected path in the SRPM: %r' % name)
        if name in files:
            raise Refused('the SRPM holds %r twice' % name)
        files[name] = data
    info = {'name': hdr.get(_NAME), 'version': hdr.get(_VERSION),
            'release': hdr.get(_RELEASE), 'payload_compressor': comp,
            'srpm_sha256': _sha256_file(path)}
    return info, hdr, files


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def identity(info):
    """(build, version, tag) as the rest of the tool reads them. The build
    ends in `.src`: locale_order.build_parts reads the last dotted word as
    the architecture only when it starts with a letter and is not an `elN`
    mark, so a name without one cannot be ordered. The header's ARCH is
    the build host's, never written here."""
    name, ver, rel = info.get('name'), info.get('version'), info.get('release')
    if name != 'glibc':
        raise Refused('the SRPM is %r, not glibc' % name)
    m = re.match(r'^(\d+)\.(\d+)(?:\.\d+)?$', ver or '')
    if not m or not re.match(r'^[^\s/-]+$', rel or ''):
        raise Refused('unreadable version-release %r-%r' % (ver, rel))
    return ('glibc-%s-%s.src' % (ver, rel), 'glibc %s.%s' % m.groups(),
            'glibc-' + ver)


# -- what rpm recorded in the SRPM -------------------------------------------

Record = collections.namedtuple('Record', 'order sources rpm_version tag')


def rpm_record(info, hdr, files):
    """The patches in the order %autosetup applied them, the Sources, and
    the rpm that wrote the SRPM, all from the SRPM's header. The payload
    holds exactly the Sources, the Patches and glibc.spec: rpmbuild -bs
    packs what it parsed and nothing else (build/files.c
    processSourceFiles), so anything else means the header and the files
    disagree."""
    sources = hdr.get(_SOURCE)
    patches = hdr.get(_PATCH, [])
    if not isinstance(sources, list) or not sources:
        raise Refused('the SRPM header records no Source')
    if not isinstance(patches, list):
        raise Refused('the SRPM header records the patches unreadably')
    named = sources + patches + ['glibc.spec']
    if any('/' in n or n in ('', '.', '..') for n in named):
        raise Refused('the SRPM header names a path: %r'
                      % [n for n in named if '/' in n or not n.strip('.')][0])
    seen = collections.Counter(named)
    twice = sorted(n for n, c in seen.items() if c > 1)
    if twice:
        raise Refused('the SRPM header names %s twice' % twice[0])
    extra = sorted(set(files) - set(named))
    missing = sorted(set(named) - set(files))
    if extra or missing:
        raise Refused('the SRPM payload is not what its header records: '
                      '%d file(s) more (%s), %d fewer (%s)'
                      % (len(extra), ', '.join(extra[:3]) or '-',
                         len(missing), ', '.join(missing[:3]) or '-'))
    ver = hdr.get(_RPMVERSION)
    if ver not in KNOWN_RPM:
        raise Refused('the SRPM was written by rpm %r; this knows %s'
                      % (ver, ', '.join(KNOWN_RPM)))
    return Record(list(reversed(patches)), list(sources), ver,
                  identity(info)[2])


# -- what rpm built from it: the binary packages ------------------------------

Binary = collections.namedtuple('Binary', 'path sha256 header files why')

_ZSTD_FRAME = b'\x28\xb5\x2f\xfd'


def _unzstd(data):
    """The decompressed bytes, or None when this Python has no zstd and no
    zstd program is installed."""
    try:
        from compression import zstd    # Python 3.14
        return zstd.decompress(data)
    except ImportError:
        pass
    prog = shutil.which('zstd')
    if not prog:
        return None
    r = subprocess.run([prog, '-dc'], input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if r.returncode:
        raise Refused('zstd could not read a payload: %s'
                      % r.stderr.decode('utf-8', 'replace').strip())
    return r.stdout


NO_ZSTD = 'no zstd reader (Python before 3.14, and no zstd program)'


def read_binary(path):
    """A binary package: its header and, when it can be decompressed, its
    files. A payload that cannot be decompressed here (zstd with no reader)
    gives files None and the reason; its digest is checked all the same,
    since PAYLOADDIGEST is taken over the compressed bytes."""
    hdr, payload = _read_rpm(path, 0)
    comp = hdr.get(_PAYLOADCOMPRESSOR)
    if comp == 'xz':
        data = lzma.decompress(payload)
    elif comp == 'zstd':
        if not payload.startswith(_ZSTD_FRAME):
            raise Refused('%s: its zstd payload is not a zstd frame' % path)
        data = _unzstd(payload)
    else:
        raise Refused('%s: payload compressed with %r; the binary packages '
                      'this was measured on use xz or zstd' % (path, comp))
    if data is None:
        return Binary(path, _sha256_file(path), hdr, None, NO_ZSTD)
    alt = hdr.get(_PAYLOADDIGESTALT)
    if alt is not None and alt[0] != hashlib.sha256(data).hexdigest():
        raise Refused("%s: the decompressed payload's SHA256 is wrong" % path)
    files = {}
    for name, body in _cpio(io.BytesIO(data), regular_only=False):
        files['/' + name] = body
    return Binary(path, _sha256_file(path), hdr, files, None)


def _nvr(hdr):
    return '%s-%s-%s' % (hdr.get(_NAME), hdr.get(_VERSION), hdr.get(_RELEASE))


def _of_this_build(b, name, info):
    """The package named `name` of this build, built by an rpm this knows."""
    h = b.header
    srpm = 'glibc-%s-%s.src.rpm' % (info['version'], info['release'])
    if h.get(_NAME) != name:
        raise Refused('%s is %r, not %s' % (b.path, h.get(_NAME), name))
    if (h.get(_VERSION), h.get(_RELEASE)) != (info['version'],
                                              info['release']) \
            or h.get(_SOURCERPM) != srpm:
        raise Refused('%s is %s, built from %r, not from %s'
                      % (b.path, _nvr(h), h.get(_SOURCERPM), srpm))
    if h.get(_RPMVERSION) not in KNOWN_RPM:
        raise Refused('%s was built by rpm %r; this knows %s'
                      % (b.path, h.get(_RPMVERSION), ', '.join(KNOWN_RPM)))


def _installed(b):
    """{installed path: sha256 hex} of a binary package's header. An entry
    with no digest (a directory, a link) maps to ''."""
    h = b.header
    if h.get(_FILEDIGESTALGO) != [8]:
        raise Refused('%s: file digests are not sha256 (%r)'
                      % (b.path, h.get(_FILEDIGESTALGO)))
    idx, base, dirs = (h.get(_DIRINDEXES), h.get(_BASENAMES),
                       h.get(_DIRNAMES))
    digests = h.get(_FILEDIGESTS)
    if not (isinstance(base, list) and isinstance(dirs, list) and
            isinstance(idx, list) and isinstance(digests, list)) or \
            not len(idx) == len(base) == len(digests) or \
            any(i >= len(dirs) for i in idx):
        raise Refused("%s: its header's file list is not readable" % b.path)
    return {dirs[i] + n: d for i, n, d in zip(idx, base, digests)}


def _under(installed, folder):
    out = {}
    for p, d in installed.items():
        if p.startswith(folder):
            rest = p[len(folder):]
            if '/' in rest or not d:
                raise Refused('%s holds %s, which is not a plain file'
                              % (folder, p))
            out[rest] = d
    return out


def _same_names(what, pkg, mine, nvr):
    only_pkg, only_tree = sorted(set(pkg) - set(mine)), \
        sorted(set(mine) - set(pkg))
    if only_pkg or only_tree:
        raise Refused('%s: %s installs %d that the rebuilt tree lacks (%s), '
                      'and the tree has %d it does not install (%s)'
                      % (what, nvr, len(only_pkg), ', '.join(only_pkg[:3]) or
                         '-', len(only_tree), ', '.join(only_tree[:3]) or '-'))


def check_locales(tree, b):
    """Every locale source glibc-locale-source installs is the rebuilt
    tree's, by name and sha256, and the other way round: the build installs
    `locales/*` (localedata/Makefile:28@2.28, :28@2.34, :29@2.39)."""
    nvr = _nvr(b.header)
    pkg = _under(_installed(b), '/usr/share/i18n/locales/')
    mine = {p[len('localedata/locales/'):]: hashlib.sha256(v[0]).hexdigest()
            for p, v in tree.items() if p.startswith('localedata/locales/')}
    _same_names('locale sources', pkg, mine, nvr)
    bad = sorted(n for n in pkg if pkg[n] != mine[n])
    if bad:
        raise Refused('locale sources: %d differ from %s, e.g. %s'
                      % (len(bad), nvr, bad[0]))
    return 'locale sources: %d, each the sha256 %s records' % (len(pkg), nvr)


def check_charmaps(tree, b):
    """Every charmap glibc-locale-source installs, decompressed, is the
    rebuilt tree's, and the other way round. Without a reader for the
    payload, the charmaps are named as not checked."""
    nvr = _nvr(b.header)
    if b.files is None:
        return '!! charmaps NOT checked against %s: %s' % (nvr, b.why)
    folder = '/usr/share/i18n/charmaps/'
    pkg = {}
    for p, body in b.files.items():
        if not p.startswith(folder):
            continue
        rest = p[len(folder):]
        if '/' in rest or not rest.endswith('.gz'):
            raise Refused('charmaps: %s installs %s, which is not a gzipped '
                          'charmap' % (nvr, p))
        try:
            pkg[rest[:-len('.gz')]] = gzip.decompress(body)
        except (OSError, EOFError) as e:
            raise Refused('charmaps: %s in %s does not decompress: %s'
                          % (p, nvr, e))
    mine = {p[len('localedata/charmaps/'):]: v[0] for p, v in tree.items()
            if p.startswith('localedata/charmaps/')}
    _same_names('charmaps', pkg, mine, nvr)
    bad = sorted(n for n in pkg if pkg[n] != mine[n])
    if bad:
        raise Refused('charmaps: %d differ from %s, e.g. %s'
                      % (len(bad), nvr, bad[0]))
    return 'charmaps: %d, each the one %s installs' % (len(pkg), nvr)


def check_alias(tag_alias, b):
    """The locale.alias glibc-common installs is the upstream tag's, the one
    a side reads (glibc_locale_data.locale_aliases)."""
    nvr = _nvr(b.header)
    got = [d for p, d in _installed(b).items()
           if p == '/usr/share/locale/locale.alias']
    if len(got) != 1 or not got[0]:
        raise Refused('locale.alias: %s installs %d of it'
                      % (nvr, len(got)))
    if tag_alias is None or hashlib.sha256(tag_alias).hexdigest() != got[0]:
        raise Refused("locale.alias: %s installs another than the tag's, "
                      'which is the one a side reads' % nvr)
    return 'locale.alias: the sha256 %s records' % nvr


def verify_against_binaries(tree, tag_alias, info, binaries):
    """The rebuilt tree against the build's binary packages: what rpm itself
    built from the same SRPM. binaries is {'glibc-locale-source': Binary,
    'glibc-common': Binary}; a missing one is refused (it is not published,
    or was not fetched). Returns what was checked, a line each; a line
    starting `!!` names what could not be."""
    for name in ('glibc-locale-source', 'glibc-common'):
        if binaries.get(name) is None:
            raise Refused('no %s package of glibc-%s-%s to check the rebuilt '
                          'sources against' % (name, info.get('version'),
                                               info.get('release')))
        _of_this_build(binaries[name], name, info)
    ls, common = binaries['glibc-locale-source'], binaries['glibc-common']
    return [check_locales(tree, ls), check_charmaps(tree, ls),
            check_alias(tag_alias, common)]


# -- the spec ----------------------------------------------------------------

# The spec's last section, the package's history, is left out: tests/
# test_wrapper keeps its name out of scripts/, and %prep ends before it in
# every spec.
_SECTION = re.compile(r'^%(prep|build|install|check|files|package|'
                      r'description|pre|post|preun|postun|posttrans|pretrans|'
                      r'transfiletriggerin|filetriggerin|triggerin|'
                      r'triggerun|generate_buildrequires)(\s|$)')
_COND_OPEN = re.compile(r'^%if(n?arch|n?os)?(\s|$)')
_AUTOSETUP = '%autosetup -n %{glibcsrcdir} -p1'
_SOURCE0 = 'Source0: %{?glibc_release_url}%{glibcsrcdir}.tar.xz'


def spec_text(files):
    raw = files.get('glibc.spec')
    if raw is None:
        raise Refused('the SRPM has no glibc.spec')
    specs = [n for n in files if n.endswith('.spec')]
    if specs != ['glibc.spec']:
        raise Refused('expected glibc.spec alone, found %r' % sorted(specs))
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise Refused('glibc.spec is not UTF-8')


def prep_span(lines):
    """(first, end) indexes of %prep's body in the spec's lines: from the
    line after `%prep` to the next section. Refused when there is no %prep
    or nothing ends it."""
    starts = [i for i, ln in enumerate(lines) if re.match(r'^%prep(\s|$)', ln)]
    if len(starts) != 1:
        raise Refused('glibc.spec has %d %%prep sections' % len(starts))
    for j in range(starts[0] + 1, len(lines)):
        if _SECTION.match(lines[j]):
            return starts[0] + 1, j
    raise Refused('nothing ends %prep')


def _lua_blocks(text):
    """(start, end, body) of every %{lua: ...} in text, braces balanced."""
    i = 0
    while True:
        i = text.find('%{lua:', i)
        if i < 0:
            return
        depth, j = 0, i + 1
        while j < len(text):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth:
            raise Refused('a %{lua: block without its end in glibc.spec')
        yield i, j + 1, text[i + 6:j]
        i = j + 1


class _Spec(object):
    """What prep_keys reads of the spec: its macros, which of them have more
    than one value (defined twice, under a conditional, over several lines,
    or inside another line), and its Sources."""

    def __init__(self, text, files, tree):
        self.files, self.tree = files, tree
        self.macros, self.unsure, self.sources = {}, set(), {}
        depth = 0
        for ln in text.split('\n'):
            s = ln.strip()
            if _COND_OPEN.match(s):
                depth += 1
            elif re.match(r'^%endif(\s|$)', s):
                depth -= 1
            m = re.match(r'^%(?:global|define)\s+(\w+)(?:\s+(.*?))?\s*$', s)
            if m:
                name, value = m.group(1), m.group(2) or ''
                if name in self.macros or depth or value.endswith('\\'):
                    self.unsure.add(name)
                self.macros[name] = value
            for m in _DEFINES.finditer(s[1:]):
                self.unsure.add(m.group(3))
            m = re.match(r'^Source(\d*)\s*:\s*(\S+)', s, re.I)
            if m:
                self.sources[int(m.group(1) or 0)] = \
                    os.path.basename(m.group(2))

    def source(self, name):
        raw = self.files.get(name)
        if raw is None:
            raise Refused('Source %s is not in the SRPM' % name)
        sup = self.tree.get('localedata/SUPPORTED')
        if sup is not None and sup[0] == raw:
            return '@SRC:=localedata/SUPPORTED'
        return '@SRC:sha256=' + hashlib.sha256(raw).hexdigest()

    def expand(self, s, depth=0):
        if depth > 8:
            raise Refused('macros nested too deep')
        out, last = '', 0
        for a, b, body in _lua_blocks(s):
            out += s[last:a] + '@LUA:' + \
                hashlib.sha256(body.encode('utf-8')).hexdigest()
            last = b
        s = out + s[last:]

        def rep(m):
            if m.group(0) == '%%':
                return '%'
            name = (m.group(1) or m.group(2)).lstrip('?!')
            n = re.match(r'^SOURCE(\d+)$', name, re.I)
            if n:
                if int(n.group(1)) not in self.sources:
                    raise Refused('%%%s is not declared' % name)
                return self.source(self.sources[int(n.group(1))])
            if name == '_sourcedir':
                return '@SD:'
            if name in self.unsure:
                raise Refused('%%%s has more than one value in glibc.spec'
                              % name)
            if name in self.macros:
                return self.expand(self.macros[name], depth + 1)
            raise Refused('macro %%%s, which this does not expand' % name)
        return re.sub(r'%%|%\{([^{}:]+)\}|%([A-Za-z_]\w*)', rep, s)


def prep_keys(spec, body):
    """%prep's body lines as the shell runs them: each line expanded the way
    rpm expands it (a macro whose value holds a newline makes more lines),
    then comments dropped one physical line at a time (a comment ends at
    its newline, even after a backslash), then lines ending in a backslash
    joined, each key with its blanks collapsed. The first line is rpm's
    %autosetup, which is kept as written."""
    shell = []
    for ln in body:
        if ' '.join(ln.split()) == _AUTOSETUP:
            shell.append(_AUTOSETUP)
        else:
            shell.extend(spec.expand(ln).split('\n'))
    keys, buf = [], None
    for s in shell:
        if buf is None and (not s.strip() or s.lstrip().startswith('#')):
            continue
        if s.endswith('\\'):
            buf = (buf or '') + s[:-1] + ' '
            continue
        keys.append(' '.join(((buf or '') + s).split()))
        buf = None
    if buf is not None:
        keys.append(' '.join(buf.split()))
    return tuple(keys)


# -- what is known: the seven real builds -------------------------------------

# Every %{lua: block of the specs of the seven builds this was measured on
# (el8_10.40, el8_10.48, 168.el9_6.24, 275.el9_8, 283.el9_8, 128.el10_2,
# 145.el10_2), by the sha256 of its body.
LUA_CATALOGUE = frozenset((
    '052f2341c249ca8edef05fc099930d1d21296a600756b8a59f668e7089825a40',
    '0b04e359900e4c4e959765c2d2102dcfc53f32655a4f27fe96020afa351abd94',
    '3125e9b3a410876afda001be8fc4afc2719916d7643557699d055ad9dc55f77c',
    '360e1b756c9a8723df2eac0f287e1a32cf43e4b77493b5d23304b79498cb4c90',
    '3b62e2b92acd2038b5748040d60104cc1b1e57efcb8aa4d34535bc4a9ace0bee',
    '49093902285baf19b84b4e32734d9386a3f624e24cbe9880348931f25a9245af',
    '5466bba4969221b736900901a43afe9c574344141c5199018d7f387c708a63bc',
    'a4ec7462eb9b3f89dcb2181d9fcec137ea3f10b1d24bee78f7732af7a84f9aca',
    'a88b8d25cc2fb5d94e5ea51b4be4e16205aabb259e62d0fad0dfcabb8b84e320',
    'ab7915cadf6137360cd506455f07ee89ceb3daf937a2d8bca88b676edf3bc639',
    'b8335ec2e9edce072684f263a6c51a23b94d196e56a1de809ef10200c5ae5c05',
    'dd70ed296135f004e5f6892b583c2cf2753db9f60d1785b3b0f537f6036f97e7',
    'df4a5453cda04fd457bcc166bee92118c3992d2e4e23191ad2062fef8717de45',
    'ef89f1d6b842d73fc261ef0035c0bbd67c61b8a2df88dd0efd48d3d8a34d71bf',
    'f212168e84f12b1bde9b4c25652b9ffe9b43b031d7cc58c75e425cef23030e00',
))

# Every macro those specs define before the end of %prep, by any spelling.
MACRO_NAMES = frozenset((
    '__brp_ldconfig',
    '__filter_GLIBC_PRIVATE',
    '__provides_exclude',
    '_annotated_build',
    '_auto_set_build_flags',
    '_binaries_in_noarch_packages_terminate_build',
    '_no_recompute_build_ids',
    '_unique_build_ids',
    'biarcharches',
    'buildpower10',
    'buildpower9',
    'comment',
    'enablekernel',
    'glibc_has_libmvec',
    'glibc_has_libnldbl',
    'glibc_ldso',
    'glibc_ldso_alternate',
    'glibc_post_funcs',
    'glibc_rtld_early_cflags',
    'glibc_ship_tracelibs_in_utils',
    'glibcrelease',
    'glibcsrcdir',
    'glibcversion',
    'headers_package_name',
    'locale_rx',
    'man_pages_version',
    'need_headers_package',
    'pie_arches',
    'sysroot_dist',
    'sysroot_package_arch',
    'sysroot_prefix',
    'target',
    'with_benchtests',
    'with_docs',
    'with_valgrind',
    'with_werror',
))

# Their %prep bodies, as prep_keys reads them.
PREP_SEQUENCES = frozenset((
    (
        '%autosetup -n %{glibcsrcdir} -p1',
        'chmod +x benchtests/scripts/*.py scripts/pylint',
        'find . -type f -size 0 -o -name "*.orig" -exec rm -f {} \\;',
        'touch `find . -name configure`',
        'touch locale/programs/*-kw.h',
        'diff -u @SRC:=localedata/SUPPORTED localedata/SUPPORTED',
    ),
    (
        '%autosetup -n %{glibcsrcdir} -p1',
        'chmod +x benchtests/scripts/*.py scripts/pylint',
        'find . -type f -size 0 -o -name "*.orig" -exec rm -f {} \\;',
        'touch `find . -name configure`',
        'touch locale/programs/*-kw.h',
        'set +x',
        "echo '@LUA:df4a5453cda04fd457bcc166bee92118c3992d2e4e23191ad2062fef8"
        "717de45' > localedata/SUPPORTED.spec",
        'set -x',
        'python3 @SRC:sha256=aa72e727c7e92b923f24e9271efe7e9a3b3f52950a393b9e'
        'fc779fe2ae39cd9d localedata/SUPPORTED > localedata/SUPPORTED.glibc',
        'diff -u --label "spec file" localedata/SUPPORTED.spec --label "glibc'
        ' localedata/SUPPORTED" localedata/SUPPORTED.glibc',
        'rm localedata/SUPPORTED.spec localedata/SUPPORTED.glibc',
    ),
    (
        '%autosetup -n %{glibcsrcdir} -p1',
        'chmod +x benchtests/scripts/*.py scripts/pylint',
        'find . -type f -size 0 -o -name "*.orig" -exec rm -f {} \\;',
        'touch `find . -name configure`',
        'touch locale/programs/*-kw.h',
        'set +x',
        "echo '@LUA:df4a5453cda04fd457bcc166bee92118c3992d2e4e23191ad2062fef8"
        "717de45' > localedata/SUPPORTED.spec",
        'set -x',
        'python3 @SRC:sha256=aa72e727c7e92b923f24e9271efe7e9a3b3f52950a393b9e'
        'fc779fe2ae39cd9d localedata/SUPPORTED > localedata/SUPPORTED.glibc',
        'diff -u --label "spec file" localedata/SUPPORTED.spec --label "glibc'
        ' localedata/SUPPORTED" localedata/SUPPORTED.glibc',
        'rm localedata/SUPPORTED.spec localedata/SUPPORTED.glibc',
        'cp @SRC:sha256=0e7104192ee401d67b8b8b24d122bf879b28b8af5194a45b34884'
        '153a3be4ed2 .',
        'chmod +x verify-ld-so-abi.sh',
        'cp @SD:/*.baseline .',
    ),
    (
        '%autosetup -n %{glibcsrcdir} -p1',
        'chmod +x benchtests/scripts/*.py scripts/pylint',
        'find . -type f -size 0 -o -name "*.orig" -exec rm -f {} \\;',
        'touch `find . -name configure`',
        'touch locale/programs/*-kw.h',
        'set +x',
        "echo '@LUA:df4a5453cda04fd457bcc166bee92118c3992d2e4e23191ad2062fef8"
        "717de45' > localedata/SUPPORTED.spec",
        'set -x',
        'python3 @SRC:sha256=aa72e727c7e92b923f24e9271efe7e9a3b3f52950a393b9e'
        'fc779fe2ae39cd9d localedata/SUPPORTED > localedata/SUPPORTED.glibc',
        'diff -u --label "spec file" localedata/SUPPORTED.spec --label "glibc'
        ' localedata/SUPPORTED" localedata/SUPPORTED.glibc',
        'rm localedata/SUPPORTED.spec localedata/SUPPORTED.glibc',
        'cp @SRC:sha256=6f3476de938d9676fc37f274bb98b85389defafef861421fcc5bd'
        '5d61936e500 .',
        'chmod +x verify-ld-so-abi.sh',
        'cp @SD:/*.baseline .',
    ),
))

_DEFINES = re.compile(r'%(?:\{(global|define|undefine):\s*|'
                      r'(global|define|undefine)\s+)(\w+)')
# What a Lua file may not do while rpm reads the spec: define, undefine or
# load macros (rpmio/rpmlua.c at the three rpm versions: rpm.define,
# rpm.undefine, rpm.load, the macros table of 4.19, an expansion of
# %global/%define/%undefine/%{load:}), run %( ), set the environment the
# %prep shell and GNU patch inherit (posix.setenv, putenv, unsetenv), or
# write rpm's own table of patches, which %autopatch walks.
_LUA_DEFINES = re.compile(
    r'\brpm\.(define|undefine|load)\b|\bmacros\s*[.\[]|'
    r'%\{?(global|define|undefine)\b|%\{load:|%\(|'
    r'\bposix\.(setenv|putenv|unsetenv)\b|'
    r'\btable\.\w+\(\s*(patches|patch_nums)\b|'
    r'\b(patches|patch_nums)\s*(\[[^\]]*\])?\s*=(?!=)')
# rpm reads a section's name in any case (build/parseSpec.c isPart,
# rstrncasecmp, at the three versions).
_LATE = re.compile(r'^[ \t]*(%(patchlist|sourcelist|package)(\s|$)|'
                   r'Patch\d*\s*:)', re.M | re.I)


def known_only(text, files, record, tree):
    """[(where, what)] of everything in the SRPM that neither rpm's record
    nor the binary packages stand for and that is not as it is in the seven
    real builds; empty when everything is known. tree is the patched
    {path: (bytes, exec)} of localedata/ and locale/."""
    out = []
    lines = text.split('\n')
    try:
        first, end = prep_span(lines)
    except Refused as e:
        return [('glibc.spec', str(e))]
    head = '\n'.join(lines[:end])

    def at(offset):
        return 'glibc.spec:%d' % (text.count('\n', 0, offset) + 1)
    # rpm runs every block, and every %( %{load: %include, while it reads
    # the spec, before %prep's shell runs: those after %prep too.
    for a, _, body in _lua_blocks(text):
        sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
        if sha not in LUA_CATALOGUE:
            out.append((at(a), 'a %%{lua: block this does not know (%s)'
                        % sha[:12]))
    for m in re.finditer(r'%\(|%\{load:|^[ \t]*%include(\s|$)', text, re.M):
        out.append((at(m.start()), 'runs or reads something while rpm reads '
                    'the spec: %s' % m.group(0).strip()))
    # rpm 4.16 and later read patches from %patchlist, under its %ifarch
    # too (build/parseSpec.c:79@4.16.1.3): the arch the SRPM was written on
    # would decide them, as a Patch line under a conditional would.
    for m in re.finditer(r'^[ \t]*%(patchlist|sourcelist)(\s|$)', head,
                         re.M | re.I):
        out.append((at(m.start()), 'a %%%s, which no real spec has'
                    % m.group(1)))
    # %autosetup's walk over the patches is expanded when rpm parses %prep
    # (build/parsePrep.c, the three versions), so a patch a later
    # %patchlist, %package preamble or Patch line adds is in the header and
    # was never applied.
    tail = '\n'.join(lines[end:])
    for m in _LATE.finditer(tail):
        out.append(('glibc.spec:%d' % (end + tail.count('\n', 0, m.start())
                                       + 1),
                    'a patch or a package declared after %%prep: %s'
                    % m.group(1).strip()))
    # A Lua file the spec runs (patch-git.lua, through dofile) changes from
    # build to build, so it cannot be pinned; it is read for _LUA_DEFINES,
    # none of which the five real ones holds. What else it does is in
    # NOT_CHECKED.
    for name in record.sources:
        if name.endswith('.lua'):
            m = _LUA_DEFINES.search(files[name].decode('utf-8', 'replace'))
            if m:
                out.append((name, 'changes macros, the environment or the '
                            'patches while rpm reads the spec: %s'
                            % m.group(0)))
    for m in _DEFINES.finditer(head):
        if m.group(3) not in MACRO_NAMES:
            out.append((at(m.start()), 'defines %%%s, which no real spec '
                        'does' % m.group(3)))
    depth = 0
    for i, ln in enumerate(lines[:end], 1):
        s = ln.strip()
        if _COND_OPEN.match(s):
            depth += 1
        elif re.match(r'^%endif(\s|$)', s):
            depth -= 1
        elif depth and re.match(r'^Patch\d*\s*:', s, re.I):
            out.append(('glibc.spec:%d' % i, 'a Patch line under a '
                        'conditional'))
    srcdir = re.findall(r'^%(?:define|global)\s+glibcsrcdir\s+(\S+)\s*$',
                        head, re.M)
    tag = record.tag
    if srcdir != [tag] or _SOURCE0 not in [ln.strip() for ln in lines[:end]] \
            or tag + '.tar.xz' not in record.sources:
        out.append(('glibc.spec', 'does not unpack %s.tar.xz the way the real '
                    'specs do (glibcsrcdir %r)' % (tag, srcdir)))
    try:
        keys = prep_keys(_Spec(text, files, tree), lines[first:end])
    except Refused as e:
        out.append(('glibc.spec:%d' % first, '%%prep: %s' % e))
    else:
        if keys not in PREP_SEQUENCES:
            out.append(('glibc.spec:%d' % first, '%%prep is not one the real '
                        'specs have: %s' % _first_unknown(keys)))
    orig = sorted(p for p in tree if p.endswith('.orig'))
    if orig:
        out.append(('%prep', 'the tree holds %s, which the find of %%prep '
                    'deletes' % orig[0]))
    if not any(fnmatch.fnmatchcase(p, 'locale/programs/*-kw.h') for p in tree):
        out.append(('%prep', 'no locale/programs/*-kw.h for the touch of '
                    '%prep'))
    for name in record.order:
        out.extend(patch_unknowns(name, files[name]))
    return out


def _first_unknown(keys):
    """The first line of keys that no known sequence has at that place."""
    best = None
    for seq in PREP_SEQUENCES:
        n = 0
        while n < min(len(seq), len(keys)) and seq[n] == keys[n]:
            n += 1
        if best is None or n > best:
            best = n
    best = best or 0
    if best < len(keys):
        return 'line %d of its body, %s' % (best + 1, keys[best])
    return 'it ends after line %d of its body' % len(keys)


# -- the patches --------------------------------------------------------------

_HUNK = re.compile(rb'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
# The lines git writes between `diff --git` and `---` (git diff-generate-
# patch, "extended header lines"); anything else ends the header.
_GIT_HEADER = (b'old mode ', b'new mode ', b'deleted file mode ',
               b'new file mode ', b'copy from ', b'copy to ', b'rename from ',
               b'rename to ', b'similarity index ', b'dissimilarity index ',
               b'index ', b'Binary files ', b'GIT binary patch')
# Lines GNU patch 2.7.6 reads as the start of a diff's header, wherever they
# are outside a hunk (intuit_diff_type, src/pch.c:549-830), after it skips
# blanks, tabs and X's (:518) and, before `---`, RFC 934's `- ` (:703-705).
_HEADER_SHAPED = (b'--- ', b'+++ ', b'*** ', b'Index:', b'diff --git ',
                  b'Prereq:')
_ALIAS = 'intl/locale.alias'
# The code strcoll and strxfrm run: what step 5 reads as glibc's collation
# code, and the four wrappers that call into it and are in no list there.
COLLATION_CODE = frozenset(dcc.ENTRY_POINTS + dcc.TIER1 + dcc.TIER2 + [
    'string/strcoll.c', 'string/strxfrm.c', 'wcsmbs/wcscoll.c',
    'wcsmbs/wcsxfrm.c'])
_EPOCH = re.compile(rb'^(1969-12-31|1970-01-01) ')


def in_scope(path):
    return path.startswith(SCOPE)


def watched(path):
    """A path the result reads or reports: localedata/ and locale/ (the
    tree), intl/locale.alias (a side reads it), strcoll's code (listed)."""
    return in_scope(path) or path == _ALIAS or path in COLLATION_CODE


def _names_in_scope(line):
    return any(w.encode() in line for w in SCOPE)


def _names_watched(line):
    return _names_in_scope(line) or _ALIAS.encode() in line or \
        any(c.encode() in line for c in COLLATION_CODE)


def rpm_compression(name, data):
    """What rpm would unpack the patch as before applying it, or None.
    rpmFileIsCompressed reads 13 bytes and calls a shorter file plain
    (rpmio/rpmfileutil.c:341-378@4.14.3, :178-215@4.16.1.3, the same at
    4.19.1.1); 4.14.3 spells zstd 28 85 2f, the later ones 28 b5 2f, and
    either is refused here."""
    m = data[:13]
    if len(m) < 13:
        return None
    if m.startswith(b'BZh'):
        return 'bzip2'
    if m.startswith((b'PK\x03\x04', b'PK00')):
        return 'zip'
    if m.startswith(b'\xfd7zXZ\x00'):
        return 'xz'
    if m.startswith((b'\x28\x85\x2f', b'\x28\xb5\x2f')):
        return 'zstd'
    if m.startswith(b'LZIP'):
        return 'lzip'
    if m.startswith(b'LRZI'):
        return 'lrzip'
    if m[0] == 0o37 and m[1] in (0o213, 0o236, 0o036, 0o240, 0o235):
        return 'gzip, pack, lzh or compress'
    if m.startswith(b'7z\xbc\xaf\x27\x1c'):
        return '7zip'
    if name.endswith(('.lzma', '.gem')):
        return name.rsplit('.', 1)[1]
    return None


def _lines(data):
    ls = data.split(b'\n')
    return [l + b'\n' for l in ls[:-1]] + ([ls[-1]] if ls[-1] else [])


def _header_shaped(line):
    """The line as GNU patch tests it for a header, or None."""
    s = line.lstrip(b' \tX')
    while s.startswith(b'- '):
        s = s[2:]
    return s if s.startswith(_HEADER_SHAPED) else None


def _name(field, prefix):
    """(path, timestamp) of a ---/+++ field whose name is prefix + a clean
    relative path, else None: a quoted name, a doubled or leading slash, a
    `.` or `..` part are not the shapes measured."""
    raw, _, ts = field.rstrip(b'\r\n').partition(b'\t')
    if raw == b'/dev/null':
        return '/dev/null', ts
    if not raw.startswith(prefix):
        return None
    p = raw[len(prefix):]
    parts = p.split(b'/')
    if not p or any(x in (b'', b'.', b'..') for x in parts) or b'"' in p:
        return None
    try:
        return p.decode('utf-8'), ts
    except UnicodeDecodeError:
        return None


def _shape(git, old, new, hunks):
    """(shape, path, why) of a section, the shape one of those the seven
    real builds use on localedata/ and locale/, else None and why:
      git          diff --git a/P b/P, index, --- a/P, +++ b/P
      git-create   ... new file mode, --- /dev/null, +++ b/P
      plain        --- a/P <date>, +++ b/P <date>
      plain-create --- a/P <1969-12-31 or 1970-01-01 date>, +++ b/P,
                   one hunk @@ -0,0
      git-empty    diff --git a/P b/P, new file mode, index 0..e69de29,
                   no diff (el8's glibc-rh1817513-51.patch)"""
    if git:
        m = re.match(rb'^diff --git a/(\S+) b/(\S+)\r?\n?$', git[0])
        if not m or m.group(1) != m.group(2):
            return None, None, 'a git header naming two paths or none'
        p = _name(b'a/' + m.group(1), b'a/')
        if p is None or p[0] == '/dev/null':
            return None, None, 'a git path not in the shapes measured'
        path = p[0]
        rest = [x for x in git[1:] if not x.startswith(b'index ')]
        create = len(rest) == 1 and re.match(
            rb'^new file mode 100(644|755)\r?\n?$', rest[0]) is not None
        if rest and not create:
            return None, path, 'the git lines %s' % ', '.join(
                x.rstrip().decode('utf-8', 'replace') for x in rest[:2])
        if old is None and new is None:
            if create and any(x.startswith(b'index 0000000') and
                              b'..e69de29' in x for x in git):
                return 'git-empty', path, None
            return None, path, 'a git section with no diff'
        if old is None or new is None:
            return None, path, 'a ---/+++ pair this does not read'
        want_old = ('/dev/null',) if create else (path,)
        if old[0] not in want_old or new[0] != path or old[1] or new[1]:
            return None, path, ('its ---/+++ do not name %s as git writes'
                                % path)
        return ('git-create' if create else 'git'), path, None
    if old is None or new is None or old[0] == '/dev/null' or \
            new[0] == '/dev/null' or old[0] != new[0]:
        return None, (new or old or (None,))[0], 'a ---/+++ pair this does ' \
            'not read'
    path = old[0]
    if not old[1] or not new[1]:
        return None, path, 'a plain diff without dates'
    if _EPOCH.match(new[1]):
        return None, path, 'a plain diff that deletes'
    if _EPOCH.match(old[1]):
        if len(hunks) == 1 and hunks[0][0] == 0 and not hunks[0][1]:
            return 'plain-create', path, None
        return None, path, 'a plain diff from the epoch that is not one ' \
            'creating hunk'
    return 'plain', path, None


def parse_patch(data):
    """The sections of a patch file, as GNU patch would find them at column
    0: {shape, path, why, hunks, git, broken, lines, body}, each hunk (old
    start, old lines, new lines, leading context, trailing context); lines
    and body are the indexes of the section's header lines and of its hunk
    lines."""
    lines, files, i = _lines(data), [], 0

    def take_hunks(i):
        hunks, broken, body = [], None, []
        while i < len(lines) and lines[i].startswith(b'@@ ') and not broken:
            m = _HUNK.match(lines[i])
            if not m:
                return hunks, 'bad hunk header %r' % lines[i][:60], i, body
            body.append(i)
            start = int(m.group(1))
            oc = int(m.group(2)) if m.group(2) is not None else 1
            nc = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            ol, nl, ops = [], [], []
            while (len(ol) < oc or len(nl) < nc) and i < len(lines):
                t = lines[i]
                c, rest = t[:1], t[1:]
                if t == b'\n':          # an empty context line, its blank cut
                    c, rest = b' ', b'\n'
                if c not in (b' ', b'-', b'+'):
                    broken = 'bad hunk line %r' % t[:60]
                    break
                body.append(i)
                ops.append(c)
                if c in (b' ', b'-'):
                    ol.append(rest)
                if c in (b' ', b'+'):
                    nl.append(rest)
                i += 1
                if i < len(lines) and lines[i].startswith(b'\\'):
                    body.append(i)
                    if c in (b' ', b'-'):
                        ol[-1] = ol[-1].rstrip(b'\n')
                    if c in (b' ', b'+'):
                        nl[-1] = nl[-1].rstrip(b'\n')
                    i += 1
            if not broken and (len(ol) != oc or len(nl) != nc):
                broken = 'hunk cut short'
            pre = 0
            while pre < len(ops) and ops[pre] == b' ':
                pre += 1
            suf = 0
            while suf < len(ops) - pre and ops[len(ops) - 1 - suf] == b' ':
                suf += 1
            hunks.append((start, ol, nl, pre, suf))
        return hunks, broken, i, body

    def triple(i):
        return (i + 2 < len(lines) and lines[i].startswith(b'--- ') and
                lines[i + 1].startswith(b'+++ ') and
                lines[i + 2].startswith(b'@@ '))

    def section(git, idx, i):
        old = new = None
        hunks, broken, body = [], None, []
        if triple(i):
            old = _name(lines[i][4:], b'a/')
            new = _name(lines[i + 1][4:], b'b/')
            idx = idx + [i, i + 1]
            hunks, broken, i, body = take_hunks(i + 2)
        shape, path, why = _shape(git, old, new, hunks)
        files.append({'shape': shape, 'path': path, 'why': why,
                      'hunks': hunks, 'git': git, 'broken': broken,
                      'lines': idx, 'body': body,
                      'names': (old, new)})
        return i

    while i < len(lines):
        ln = lines[i]
        if ln.startswith(b'diff --git '):
            git, idx = [ln], [i]
            i += 1
            while i < len(lines) and lines[i].startswith(_GIT_HEADER):
                git.append(lines[i])
                idx.append(i)
                i += 1
            i = section(git, idx, i)
            continue
        if triple(i):
            i = section([], [], i)
            continue
        i += 1
    return files


def _section_paths(s):
    """Every path a section names, as GNU patch strips -p1."""
    out = set()
    if s['path']:
        out.add(s['path'])
    for n in s['names']:
        if n and n[0] != '/dev/null':
            out.add(n[0])
    for g0 in s['git'][:1]:
        for x in g0.split()[2:4]:
            out.add(x[2:].decode('utf-8', 'replace'))
    return out


def _touches(s, lines, path_is, line_names):
    """A section is on a path when a path it names is, or when one of its
    header lines names one: a name this cannot read (`a//localedata/x`,
    `glibc-2.34.orig/localedata/x`) still reaches the file under GNU patch's
    -p1. apply_patch asks it of the folders, patch_unknowns of every path
    the result reads."""
    return any(path_is(p) for p in _section_paths(s)) or \
        any(line_names(lines[i]) for i in s['lines'])


def _touches_watched(s, lines):
    return _touches(s, lines, watched, _names_watched)


def patch_unknowns(name, data):
    """[(name, what)] for one patch: rpm would unpack it, or GNU patch would
    read from it a section on a path the result reads that is not one of
    the shapes of the seven real builds (parse_patch), or a header-shaped
    line naming such a path that no such section holds (an indented,
    quoted or otherwise spelled diff GNU would still apply)."""
    kind = rpm_compression(name, data)
    if kind:
        return [(name, 'rpm would unpack it as %s before applying it' % kind)]
    out = []
    sections = parse_patch(data)
    lines = _lines(data)
    claimed, body = set(), set()
    for s in sections:
        claimed.update(s['lines'])
        body.update(s['body'])
        if not _touches_watched(s, lines):
            continue
        if s['shape'] is None:
            out.append((name, 'a section on %s this does not know: %s'
                        % (s['path'] or sorted(_section_paths(s)), s['why'])))
        elif s['broken']:
            out.append((name, '%s: %s' % (s['path'], s['broken'])))
    index = False
    for i, ln in enumerate(lines):
        if i in body:
            continue
        h = _header_shaped(ln)
        if h is None:
            continue
        if h.startswith(b'Index:'):
            index = True
        if i not in claimed and _names_watched(h):
            out.append((name, 'line %d reads to GNU patch as a header on a '
                        'path the result reads: %r' % (i + 1, h[:80])))
    if index and any(_touches_watched(s, lines) for s in sections):
        out.append((name, 'an Index: line beside a section on a path the '
                    'result reads'))
    hunks = data.count(b'\n@@ -') + data.startswith(b'@@ -')
    if hunks != sum(len(s['hunks']) for s in sections):
        out.append((name, 'hunk headers this does not read as unified diff'))
    return out


def _matches(out, ol, pos):
    return 0 <= pos <= len(out) - len(ol) and out[pos:pos + len(ol)] == ol


def locate(out, ol, start, first_guess, frozen, prefix, suffix):
    """Where GNU patch 2.7.6 puts a hunk at fuzz 0 (locate_hunk,
    src/patch.c:1142-1214), 1-based, or 0. The search runs from first_guess
    outwards, after before before (+offset first), never above the line
    after the last one the previous hunk froze; a hunk with less context
    before than after can only match at the file's start, and only if it
    says it starts there, one with less after only at the file's end."""
    pat = len(ol)
    if not pat:
        return first_guess
    context = suffix if prefix < suffix else prefix
    prefix_fuzz, suffix_fuzz = prefix - context, suffix - context
    max_where = len(out) - (pat - suffix_fuzz) + 1
    min_where = frozen + 1
    max_pos = max_where - first_guess
    max_neg = first_guess - min_where
    max_offset = max(max_pos, max_neg)
    if first_guess <= max_neg:
        max_neg = first_guess - 1
    if prefix_fuzz < 0 and start <= 1:
        offset = 1 - first_guess
        if frozen <= prefix and offset <= max_pos and \
                _matches(out, ol, first_guess + offset - 1):
            return first_guess + offset
        return 0
    if suffix_fuzz < 0:
        offset = first_guess - (len(out) - pat + 1)
        if offset <= max_neg and _matches(out, ol, first_guess - offset - 1):
            return first_guess - offset
        return 0
    offset = (first_guess - max_where if max_pos < 0
              else first_guess - min_where if max_neg < 0 else 0)
    while offset <= max_offset:
        if offset <= max_pos and _matches(out, ol, first_guess + offset - 1):
            return first_guess + offset
        if offset <= max_neg and _matches(out, ol, first_guess - offset - 1):
            return first_guess - offset
        offset += 1
    return 0


def apply_hunks(lines, hunks, where):
    """The lines after every hunk, placed as GNU patch places them."""
    out, adj, in_off, frozen = list(lines), 0, 0, 0
    for start, ol, nl, pre, suf in hunks:
        nominal = (start - 1 if ol else start) + adj
        pos = locate(out, ol, start, nominal + in_off + 1, frozen, pre, suf)
        if not pos:
            raise Refused('a hunk at -%d does not apply to %s'
                          % (start, where))
        pos -= 1
        out[pos:pos + len(ol)] = nl
        in_off = pos - nominal
        adj += len(nl) - len(ol)
        frozen = pos + len(nl) - suf
    return out


def apply_patch(tree, name, data):
    """Apply the localedata/ and locale/ sections of one patch file to tree
    ({path: (bytes, exec)}). Returns (paths changed, strcoll's code paths
    the patch names). A section on those folders whose shape is not known
    is refused; patch_unknowns says why in detail."""
    changed, code = [], set()
    lines = _lines(data)
    for s in parse_patch(data):
        paths = _section_paths(s)
        code.update(p for p in paths if p in COLLATION_CODE)
        if not _touches(s, lines, in_scope, _names_in_scope):
            continue
        if s['shape'] is None or s['broken'] or not in_scope(s['path'] or ''):
            raise Refused('%s: a section on %s this does not apply: %s'
                          % (name, sorted(paths), s['why'] or s['broken'] or
                             'it crosses into localedata/ or locale/'))
        p = s['path']
        if s['shape'] in ('git-create', 'plain-create', 'git-empty'):
            # Without -f GNU patch takes an existing file as a patch already
            # applied and fails the build; rpm 4.19 (el10) passes -f, and the
            # hunk is then put into the file as it is (util.c:1047-1064,
            # pch.c:200-223@2.7.6). Neither leaves a tree to read.
            if p in tree:
                raise Refused('%s: creates %s, which exists' % (name, p))
            cur = []
            exe = any(x.startswith(b'new file mode 100755') for x in s['git'])
        else:
            if p not in tree:
                raise Refused('%s: changes %s, which is not there' % (name, p))
            cur, exe = _lines(tree[p][0]), tree[p][1]
        new = apply_hunks(cur, s['hunks'], '%s (%s)' % (p, name))
        tree[p] = (b''.join(new), exe)
        changed.append(p)
    return changed, sorted(code)


# -- the upstream tree -------------------------------------------------------

def scope_from_tarball(files, srcdir):
    """({path: (bytes, exec)} of localedata/ and locale/, intl/locale.alias)
    of the SRPM's one tarball, whose top folder is srcdir."""
    tars = [n for n in files if re.search(r'\.tar\.(xz|gz|bz2)$', n)]
    if len(tars) != 1:
        raise Refused('expected one tarball in the SRPM, found %r'
                      % sorted(tars))
    name = tars[0]
    if not name.startswith(srcdir + '.tar.'):
        raise Refused('the tarball %s is not %s' % (name, srcdir))
    tree, alias = {}, None
    with tarfile.open(fileobj=io.BytesIO(files[name]),
                      mode='r:' + name.rsplit('.', 1)[1]) as t:
        for m in t:
            parts = m.name.split('/')
            if parts[0] != srcdir or '..' in parts or m.name.startswith('/'):
                raise Refused('unexpected tarball member %r' % m.name)
            rel = '/'.join(parts[1:])
            if m.isdir() or not (in_scope(rel) or rel == _ALIAS):
                continue
            if not m.isfile():
                raise Refused('%s in the tarball is a link or a special file'
                              % rel)
            if rel == _ALIAS:
                alias = t.extractfile(m).read()
                continue
            tree[rel] = (t.extractfile(m).read(), bool(m.mode & 0o100))
    if not tree:
        raise Refused('the tarball holds no localedata/ or locale/')
    return tree, alias


def scope_from_git(repo, tag):
    """The same, from a tag of the clone: measured equal to the SRPM's
    tarball in paths, bytes and executable bit for the seven builds."""
    out = g.run_git(['ls-tree', '-r', '-z', tag, '--', 'localedata/',
                     'locale/', _ALIAS], repo).stdout
    modes = {}
    for ent in out.split(b'\0'):
        if not ent:
            continue
        meta, _, raw = ent.partition(b'\t')
        mode, path = meta.split(b' ')[0], raw.decode('utf-8', 'replace')
        if mode not in (b'100644', b'100755'):
            raise Refused('%s at %s is not a regular file' % (path, tag))
        modes[path] = mode == b'100755'
    paths = sorted(modes)
    blobs, missing = g.read_blobs(repo, tag, paths, raw=True)
    g.require_read(tag, paths, missing, 'the localedata/ and locale/ of a '
                   'build')
    alias = blobs.get(_ALIAS)
    return {p: (blobs[p], modes[p]) for p in modes if p != _ALIAS}, alias


def tarball_is_tag(repo, tag, base, alias):
    """TARBALL_IS_TAG % tag when the SRPM's tarball (scope_from_tarball's
    tree and alias) is the upstream tag `tag` of the clone in localedata/,
    locale/ and intl/locale.alias, bytes and executable bit alike; else
    Refused, naming the first paths that differ. The steps compare the
    build's commit with the tag, so a tarball that is not the tag would make
    its own differences read as the distro's patches."""
    upstream, up_alias = scope_from_git(repo, tag)
    differ = sorted(p for p in set(base) | set(upstream)
                    if base.get(p) != upstream.get(p))
    if alias != up_alias:
        differ.append(_ALIAS)
    if differ:
        raise Refused("the SRPM's tarball is not %s in localedata/ and "
                      "locale/: %d path(s) differ, the first %s"
                      % (tag, len(differ), ', '.join(differ[:5])))
    return TARBALL_IS_TAG % tag


# -- putting it together -----------------------------------------------------

def rebuild(info, hdr, files, base):
    """(tree, provenance) of one build: base (the upstream tree) with its
    patches applied in the order rpm recorded, and everything neither rpm's
    record nor the binary packages stand for checked by known_only. files
    is the SRPM's {name: bytes}."""
    build, version, tag = identity(info)
    text = spec_text(files)
    record = rpm_record(info, hdr, files)
    tree = dict(base)
    applied, collation = [], []
    for pos, name in enumerate(record.order, 1):
        changed, code = apply_patch(tree, name, files[name])
        if changed:
            applied.append({'pos': pos, 'patch': name, 'paths': changed})
        if code:
            collation.append({'patch': name, 'paths': code})
    unknown = known_only(text, files, record, tree)
    if unknown:
        raise Refused('%d thing(s) this does not know in %s; the first: '
                      '%s: %s' % ((len(unknown), build) + unknown[0]))
    return tree, {
        'build': build, 'version': version, 'tag': tag,
        'srpm_sha256': info.get('srpm_sha256'),
        'payload_compressor': info.get('payload_compressor'),
        'srpm_rpm_version': record.rpm_version,
        'order_rule': "the SRPM header's PATCH list, reversed",
        'patches': len(record.order), 'applied': applied,
        'collation_code_patches': collation,
        'not_checked': list(NOT_CHECKED)}


Built = collections.namedtuple('Built', 'tree build version tag provenance')


def _key():
    """What a kept tree depends on besides its inputs: this module, and the
    list of strcoll's code it takes from diff_collation_code."""
    h = hashlib.sha256()
    for f in (os.path.abspath(__file__), dcc.__file__):
        with open(f.replace('.pyc', '.py'), 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()[:20]


def build(srpm, binaries, cache=None, repo=None):
    """The build's rebuilt localedata/ and locale/, from the SRPM at `srpm`,
    checked against the build's binary packages ({'glibc-locale-source':
    path, 'glibc-common': path}) and kept under `cache`
    (PG_GLIBC_BUILD_SOURCES_CACHE, else scripts/build-sources-cache).
    Raises Refused, and keeps nothing, when the sources cannot be rebuilt
    the way rpm would or do not match what rpm built.

    With `repo`, the glibc clone, the SRPM's tarball is also compared with
    the upstream tag in localedata/, locale/ and intl/locale.alias and
    refused unless they are the same (tarball_is_tag); provenance['tarball']
    then says so. A result kept without that check is made again when the
    clone is given, so the cache never skips it."""
    cache = cache or os.environ.get('PG_GLIBC_BUILD_SOURCES_CACHE') or \
        DEFAULT_CACHE
    names = ('glibc-locale-source', 'glibc-common')
    missing = [n for n in names if not (binaries or {}).get(n)]
    if missing:
        raise Refused('no %s package given to check the rebuilt sources '
                      'against' % missing[0])
    sha = _sha256_file(srpm)
    shas = [_sha256_file(binaries[n]) for n in names]
    where = os.path.join(cache, hashlib.sha256(
        ' '.join([sha] + shas).encode()).hexdigest()[:32] + '-' + _key())
    kept = _kept(where, sha, shas)
    if kept and (repo is None or kept.provenance.get('tarball')):
        return kept
    info, hdr, files = read_srpm(srpm)
    _, _, tag = identity(info)
    base, alias = scope_from_tarball(files, tag)
    checked = tarball_is_tag(repo, tag, base, alias) if repo else None
    tree, prov = rebuild(info, hdr, files, base)
    if checked:
        prov['tarball'] = checked
    prov['verified'] = verify_against_binaries(
        tree, alias, info, {n: read_binary(binaries[n]) for n in names})
    prov['binaries_sha256'] = shas
    os.makedirs(cache, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='.new-', dir=cache)
    try:
        for p, (data, exe) in tree.items():
            full = os.path.join(tmp, 'tree', p)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as fh:
                fh.write(data)
            os.chmod(full, 0o755 if exe else 0o644)
        prov['files'] = len(tree)
        with open(os.path.join(tmp, 'provenance.json'), 'w') as fh:
            json.dump(prov, fh, indent=1, sort_keys=True)
        if os.path.exists(where):
            shutil.rmtree(where)
        os.rename(tmp, where)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    kept = _kept(where, sha, shas)
    if kept is None:
        shutil.rmtree(where, ignore_errors=True)
        raise Refused('the tree just kept at %s does not read back whole'
                      % where)
    return kept


def _kept(where, sha, shas):
    """The kept result at `where`, or None when there is none or it is not
    whole: a damaged entry is built again, never read as a smaller tree."""
    try:
        with open(os.path.join(where, 'provenance.json')) as fh:
            prov = json.load(fh)
        root = os.path.join(where, 'tree')
        count = sum(len(fs) for _, _, fs in os.walk(root))
    except (OSError, ValueError):
        return None
    if prov.get('srpm_sha256') != sha or prov.get('files') != count or \
            prov.get('binaries_sha256') != shas or not prov.get('verified'):
        return None
    return Built(root, prov['build'], prov['version'], prov['tag'], prov)


# -- fetching a build from a mirror ------------------------------------------
#
# Measured on Rocky Linux's mirror on 2026-11-03, pub/rocky/{8,9,10} and
# every vault/rocky/<major>.<minor>, 66 glibc builds: the SRPM is in
# BaseOS/source/tree, glibc-locale-source in BaseOS on el8 and AppStream on
# el9 and el10, glibc-common in BaseOS; every row sha256, every binary's
# sourcerpm its build's SRPM; package paths come in two layouts (Packages/g/
# and, on 8.3 and 8.4, Packages/), so a URL is always the metadata's href,
# never made from the name; vault 9.3 and 9.5 publish the SRPMs with no
# metadata, and those builds are refused.

ROCKY = 'https://dl.rockylinux.org/'
TIMEOUT = 120
_SOURCE_REPO = 'BaseOS/source/tree'
# (key, rpm name, arch, the repositories it can be in): one row per file.
PACKAGES = (('srpm', 'glibc', 'src', (_SOURCE_REPO,)),
            ('glibc-locale-source', 'glibc-locale-source', 'x86_64',
             ('BaseOS/x86_64/os', 'AppStream/x86_64/os')),
            ('glibc-common', 'glibc-common', 'x86_64', ('BaseOS/x86_64/os',)))
_NAMES = frozenset(p[1] for p in PACKAGES)
_RMD = '{http://linux.duke.edu/metadata/repo}'
_PRI = '{http://linux.duke.edu/metadata/common}'
_RPMNS = '{http://linux.duke.edu/metadata/rpm}'
_SHA = re.compile(r'^[0-9a-f]{64}$')
FETCHED_NOTE = ("each file's sha256 is the one the mirror's metadata "
                "records; the metadata's GPG signature is not checked")

Entry = collections.namedtuple('Entry', 'url sha256 size tree repo timestamp')
Latest = collections.namedtuple('Latest', 'build mirror tree timestamp')


class _Absent(Exception):
    """The mirror answered 404: the one answer read as "not there"."""


def _get(url):
    """The bytes at url. A 404 raises _Absent; every other failure (another
    status, a timeout, a body shorter than its Content-Length, a URL urllib
    cannot open, such as a mirror named without its scheme) is Refused,
    never read as "not there"."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            data = r.read()
            length = r.headers.get('Content-Length')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _Absent(url)
        raise Refused('%s: HTTP %d' % (url, e.code))
    except (urllib.error.URLError, OSError, http.client.HTTPException,
            ValueError) as e:
        raise Refused('could not read %s: %s'
                      % (url, getattr(e, 'reason', e)))
    if length is not None and length.strip().isdigit() and \
            int(length) != len(data):
        raise Refused('%s was cut short: %d of %s bytes'
                      % (url, len(data), length.strip()))
    return data


def _mirror(mirror):
    m = mirror or os.environ.get('PG_GLIBC_BUILD_MIRROR') or ROCKY
    return m if m.endswith('/') else m + '/'


def _cache(cache):
    return cache or os.environ.get('PG_GLIBC_BUILD_SOURCES_CACHE') or \
        DEFAULT_CACHE


_el = g.release_el


def _ask(name):
    """glibc_locale_data.build_name(name), refused when the name does not
    read as a build."""
    got = g.build_name(name)
    if got is None:
        raise Refused('%r is not a glibc build name such as '
                      'glibc-2.34-283.el9_8' % (name,))
    return got


def _write_json(path, value):
    """Write value to path whole or not at all."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.new-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(value, fh, indent=1, sort_keys=True)
        os.rename(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _unpack(data, url):
    """primary.xml's bytes, decompressed by what they are, not by the name
    (vault 9.0-9.2 name it <uuid>-PRIMARY.xml.gz)."""
    if data[:2] == b'\x1f\x8b':
        return gzip.GzipFile(fileobj=io.BytesIO(data))
    if data[:6] == b'\xfd7zXZ\x00':
        return lzma.LZMAFile(io.BytesIO(data))
    if data.lstrip()[:1] == b'<':
        return io.BytesIO(data)
    raise Refused('%s is compressed in a way this does not read' % url)


def _text(el, tag):
    x = el.find(tag)
    return x.text if x is not None else None


def _parse_primary(data, url):
    """The rows of glibc, glibc-locale-source and glibc-common in one
    primary.xml, read a package at a time: the whole of el8's BaseOS one
    (60 MB packed) takes 4.3 GiB to hold at once."""
    rows = []
    try:
        for _, el in ET.iterparse(_unpack(data, url)):
            if el.tag != _PRI + 'package':
                continue
            name = _text(el, _PRI + 'name')
            if name in _NAMES:
                v, ck = el.find(_PRI + 'version'), el.find(_PRI + 'checksum')
                loc, size = el.find(_PRI + 'location'), el.find(_PRI + 'size')
                fmt = el.find(_PRI + 'format')
                rows.append({
                    'name': name, 'arch': _text(el, _PRI + 'arch'),
                    'ver': v.get('ver') if v is not None else None,
                    'rel': v.get('rel') if v is not None else None,
                    'cktype': ck.get('type') if ck is not None else None,
                    'sha256': ck.text if ck is not None else None,
                    'href': loc.get('href') if loc is not None else None,
                    'size': size.get('package') if size is not None else None,
                    'sourcerpm': _text(fmt, _RPMNS + 'sourcerpm')
                    if fmt is not None else None})
            el.clear()
    except (ET.ParseError, OSError, EOFError, lzma.LZMAError) as e:
        raise Refused('%s does not read as a primary.xml: %s' % (url, e))
    return rows


def _rows(mirror, tree, repo, cache):
    """{'rows', 'timestamp'} of one repository's primary.xml, or None when
    the repository has no repodata/repomd.xml (404). The primary is checked
    against the sha256 repomd.xml records, and its rows are kept by that
    sha256 and this module's version, written whole or not at all."""
    base = mirror + tree + '/' + repo + '/'
    try:
        md = _get(base + 'repodata/repomd.xml')
    except _Absent:
        return None
    try:
        root = ET.fromstring(md)
    except ET.ParseError as e:
        raise Refused('%srepodata/repomd.xml does not read as XML: %s'
                      % (base, e))
    prim = [d for d in root.findall(_RMD + 'data')
            if d.get('type') == 'primary']
    if len(prim) != 1:
        raise Refused('%srepodata/repomd.xml names %d primary.xml'
                      % (base, len(prim)))
    ck, loc = prim[0].find(_RMD + 'checksum'), prim[0].find(_RMD + 'location')
    href = loc.get('href') if loc is not None else None
    sha = ck.text if ck is not None else None
    if ck is None or ck.get('type') != 'sha256' or not _SHA.match(sha or '') \
            or not _safe_href(href):
        raise Refused('%srepodata/repomd.xml does not give primary.xml a '
                      'sha256 and a path this reads' % base)
    where = os.path.join(cache, 'metadata', '%s-%s.json' % (sha, _key()))
    kept = _read_json(where)
    if isinstance(kept, dict) and kept.get('complete') is True and \
            kept.get('primary_sha256') == sha and \
            isinstance(kept.get('rows'), list):
        return kept
    try:
        data = _get(base + href)
    except _Absent:
        raise Refused('%s%s is named by repomd.xml and is not there (a '
                      'mirror in the middle of a sync?)' % (base, href))
    if hashlib.sha256(data).hexdigest() != sha:
        raise Refused('%s%s is not the primary.xml repomd.xml records '
                      '(sha256 %s)' % (base, href, sha))
    out = {'rows': _parse_primary(data, base + href), 'primary_sha256': sha,
           'timestamp': _text(prim[0], _RMD + 'timestamp'), 'complete': True}
    _write_json(where, out)
    return out


def _safe_href(href):
    return bool(href) and not href.startswith('/') and '://' not in href \
        and '..' not in href.split('/')


def _trees(mirror, major, minor):
    """Where a build of `major` can be: pub/rocky/<major>, then the vault's
    <major>.<n> folders, the release's own minor first. Only folders named
    exactly <major>.<n>: the vault also lists 8.4-RC1/ and 9.0-ISO/."""
    try:
        index = _get(mirror + 'vault/rocky/').decode('utf-8', 'replace')
    except _Absent:
        raise Refused('%svault/rocky/ is not there' % mirror)
    if not re.search(r'href="\d+\.\d+/"', index):
        raise Refused('%svault/rocky/ lists no release folder this reads, '
                      'so the vault cannot be searched' % mirror)
    minors = sorted({int(n) for n in re.findall(r'href="%d\.(\d+)/"' % major,
                                                 index)}, reverse=True)
    if minor in minors:
        minors.remove(minor)
        minors.insert(0, minor)
    return ['pub/rocky/%d' % major] + \
        ['vault/rocky/%d.%d' % (major, n) for n in minors]


def _is(row, name, arch, ver, rel):
    return (row.get('name'), row.get('arch'), row.get('ver'),
            row.get('rel')) == (name, arch, ver, rel)


def where_from(name, mirror=None, cache=None):
    """{key of PACKAGES: Entry} for the build `name`: the one place that
    decides where each of its three files comes from and with which sha256.
    The tree is the first whose source metadata lists exactly this SRPM; the
    two binary packages come from that same tree, one row each, built from
    it. A tree with no metadata is passed over while looking, and named if
    the build turns out to be published there."""
    ver, rel, major, minor = _ask(name)
    mirror, cache = _mirror(mirror), _cache(cache)
    unread, looked = [], []
    srpm = 'glibc-%s-%s.src.rpm' % (ver, rel)
    for tree in _trees(mirror, major, minor):
        src = _rows(mirror, tree, _SOURCE_REPO, cache)
        if src is None:
            unread.append(tree)
            continue
        looked.append(tree)
        if not any(_is(r, 'glibc', 'src', ver, rel) for r in src['rows']):
            continue
        out = {}
        for key, rpm, arch, repos in PACKAGES:
            hits = []
            for repo in repos:
                got = src if repo == _SOURCE_REPO else \
                    _rows(mirror, tree, repo, cache)
                if got is None:
                    raise Refused('%s%s/%s has no metadata to find %s of '
                                  'glibc-%s-%s in' % (mirror, tree, repo, rpm,
                                                      ver, rel))
                hits += [(repo, got, r) for r in got['rows']
                         if _is(r, rpm, arch, ver, rel)]
            if len(hits) != 1:
                raise Refused('%s%s lists %d %s.%s of glibc-%s-%s'
                              % (mirror, tree, len(hits), rpm, arch, ver, rel))
            repo, got, r = hits[0]
            if key != 'srpm' and r.get('sourcerpm') != srpm:
                raise Refused('%s%s/%s: %s-%s-%s.%s is built from %r, not %s'
                              % (mirror, tree, repo, rpm, ver, rel, arch,
                                 r.get('sourcerpm'), srpm))
            size = r.get('size')
            if r.get('cktype') != 'sha256' or not _SHA.match(
                    r.get('sha256') or '') or not _safe_href(r.get('href')) \
                    or not (size or '').isdigit():
                raise Refused('%s%s/%s: %s-%s-%s.%s has no sha256, size or '
                              'path this reads' % (mirror, tree, repo, rpm,
                                                   ver, rel, arch))
            out[key] = Entry(mirror + tree + '/' + repo + '/' + r['href'],
                             r['sha256'], int(size), tree, repo,
                             got.get('timestamp'))
        return out
    for tree in unread:
        for sub in ('Packages/g/', 'Packages/'):
            try:
                listing = _get(mirror + tree + '/' + _SOURCE_REPO + '/' + sub)
            except _Absent:
                continue
            if ('"%s"' % srpm).encode() in listing:
                raise Refused('%s is published in %s%s with no metadata, so '
                              'its sha256 cannot be checked'
                              % (srpm, mirror, tree))
    raise Refused('glibc-%s-%s is not in %s (looked in %s%s)'
                  % (ver, rel, mirror, ', '.join(looked) or 'nothing',
                     '; no metadata in ' + ', '.join(unread)
                     if unread else ''))


def fetch(entry, cache=None):
    """The path of entry's file in the cache, downloaded if it is not there
    and compared with the size and sha256 the metadata records; a kept file
    that no longer has that sha256 is downloaded again."""
    folder = os.path.join(_cache(cache), 'downloads')
    path = os.path.join(folder, entry.sha256)
    if os.path.exists(path):
        if os.path.getsize(path) == entry.size and \
                _sha256_file(path) == entry.sha256:
            return path
        os.remove(path)
    data = _get(entry.url)
    got = hashlib.sha256(data).hexdigest()
    if len(data) != entry.size or got != entry.sha256:
        raise Refused('%s is not the file the metadata records: %d bytes, '
                      'sha256 %s; the metadata says %d bytes, %s'
                      % (entry.url, len(data), got, entry.size, entry.sha256))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.new-', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        os.rename(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return path


def _remembered(cache, name):
    return os.path.join(cache, 'builds', name + '.json')


def _recall(cache, name, keys):
    """(mirror, {key: Entry}) remembered for `name`, or None when it was
    never fetched. A record that is there and cannot be read whole is
    refused, never taken for a first time: that would accept whatever the
    mirror gives now in place of what it gave then."""
    path = _remembered(cache, name)
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Refused('%s cannot be read (%s); remove it to fetch %s from '
                      'the metadata again' % (path, e, name))
    try:
        files = rec['files']
        if sorted(files) != sorted(keys):
            raise KeyError(sorted(files))
        return rec['mirror'], {k: Entry(**files[k]) for k in keys}
    except (KeyError, TypeError) as e:
        raise Refused('%s does not hold what this remembers of a build (%s); '
                      'remove it to fetch %s from the metadata again'
                      % (path, e, name))


def fetch_build(name, mirror=None, cache=None, repo=None):
    """build() of the build named `name` (glibc-2.34-283.el9_8), its three
    files fetched from the mirror, and `repo` handed on to it. The first
    time, where_from() reads the mirror's metadata and the sha256 it gives
    each file are remembered;
    afterwards the kept files are checked against them with no network. A
    file that is missing or no longer has its sha256 is downloaded again
    from the remembered URL; the metadata is read again only when that
    fails or another mirror is named, and it must then give the same
    sha256. Refused unless the build rebuilt is exactly the one asked
    for."""
    ver, rel, _, _ = _ask(name)
    cache, mirror = _cache(cache), _mirror(mirror)
    keys = [p[0] for p in PACKAGES]
    got = _recall(cache, name, keys)
    entries, paths = None, {}
    if got:
        kept_mirror, kept = got
        try:
            if kept_mirror == mirror:
                paths = {k: fetch(kept[k], cache) for k in keys}
                entries = kept
        except (Refused, _Absent):
            paths = {}
        if not paths:
            fresh = where_from(name, mirror, cache)
            moved = [k for k in keys if fresh[k].sha256 != kept[k].sha256]
            if moved:
                raise Refused('%s now gives %s of %s another sha256 than the '
                              'one remembered from %s'
                              % (mirror, moved[0], name, kept_mirror))
            entries = fresh
    if not paths:
        try:
            entries = entries or where_from(name, mirror, cache)
            paths = {k: fetch(entries[k], cache) for k in keys}
        except _Absent as e:
            raise Refused('%s is not there any more' % e)
        _write_json(_remembered(cache, name),
                    {'mirror': mirror,
                     'files': {k: entries[k]._asdict() for k in keys}})
    built = build(paths['srpm'], {k: paths[k] for k in keys if k != 'srpm'},
                  cache, repo)
    if built.build != 'glibc-%s-%s.src' % (ver, rel):
        raise Refused('asked for %s and the SRPM fetched is %s'
                      % (name, built.build))
    built.provenance['fetched'] = {
        'note': FETCHED_NOTE,
        'files': {k: entries[k]._asdict() for k in keys}}
    return built


def latest(major, mirror=None, cache=None):
    """The newest glibc build of RHEL `major` (8, 9, 10) the mirror's
    pub/rocky/<major> lists, by rpm's own order of releases (as text,
    el8_10.5 would beat el8_10.48), read from the metadata every time."""
    mirror, cache = _mirror(mirror), _cache(cache)
    tree = 'pub/rocky/%d' % major
    got = _rows(mirror, tree, _SOURCE_REPO, cache)
    if got is None:
        raise Refused('%s%s has no source metadata' % (mirror, tree))
    srpms = [r for r in got['rows'] if r.get('name') == 'glibc' and
             r.get('arch') == 'src' and r.get('rel') and
             (_el(r['rel']) or (None,))[0] == major]
    if not srpms:
        raise Refused('%s%s lists no glibc SRPM of el%d' % (mirror, tree,
                                                             major))
    versions = sorted({r.get('ver') for r in srpms})
    if len(versions) != 1:
        raise Refused('%s%s lists glibc %s for el%d: which is the newest is '
                      'not decided by the release' % (mirror, tree,
                                                      ', '.join(versions),
                                                      major))
    import locale_order
    best = max((r['rel'] for r in srpms),
               key=functools.cmp_to_key(locale_order.rpm_vercmp))
    return Latest('glibc-%s-%s' % (versions[0], best), mirror, tree,
                  got.get('timestamp'))


# -- a build as a commit of the clone ----------------------------------------

# The identity and date of every build commit: fixed, so that the same build
# gives the same commit on every run and every machine.
_COMMIT_ENV = {'GIT_AUTHOR_NAME': 'pg-glibc-audit',
               'GIT_AUTHOR_EMAIL': 'pg-glibc-audit@invalid',
               'GIT_COMMITTER_NAME': 'pg-glibc-audit',
               'GIT_COMMITTER_EMAIL': g.BUILD_EMAIL,
               'GIT_AUTHOR_DATE': '1970-01-01T00:00:00Z',
               'GIT_COMMITTER_DATE': '1970-01-01T00:00:00Z'}


def build_ref(build):
    """The one name of a build's commit in the clone: under
    glibc_locale_data.BUILD_REFS, outside refs/tags, so `git describe`
    never reads a build for a release, a bare name never finds it, and step
    5 leaves it out of its search of the whole history (measured
    2026-11-03 on a copy of the clone)."""
    return g.BUILD_REFS + build


def _git(repo, args, data=None):
    """git with the reader's configuration held off (GIT_CONFIG_OVERRIDES,
    and i18n.commitEncoding, which writes an encoding header into a commit:
    measured, another commit id), the fixed identity, and `data` on stdin.
    Refused on any failure."""
    env = {k: v for k, v in os.environ.items() if k != 'GIT_DIFF_OPTS'}
    env.update(_COMMIT_ENV)
    p = subprocess.run(['git', *g.GIT_CONFIG_OVERRIDES,
                        '-c', 'i18n.commitEncoding=UTF-8', *args],
                       cwd=repo, input=data, capture_output=True, env=env)
    if p.returncode:
        raise Refused('`git %s` failed in %s: %s'
                      % (' '.join(args[:2]), repo,
                         p.stderr.decode('utf-8', 'replace').strip()))
    return p.stdout


def _mktree(repo, entries, missing=False):
    """A tree from {name: (kind, mode, id) | ('tree', {...})}, made with
    `git mktree`, which checks that every entry exists unless `missing`. A
    ('tree', {...}) entry is a directory made here, below this one."""
    lines = []
    for name in sorted(entries):
        e = entries[name]
        if isinstance(e[1], dict):
            e = ('tree', '040000', _mktree(repo, e[1]))
        lines.append(b'%s %s %s\t%s' % (
            e[1].encode(), e[0].encode(), e[2].encode(),
            name.encode('utf-8', 'surrogateescape')))
    args = ['mktree', '-z'] + (['--missing'] if missing else [])
    return _git(repo, args, b''.join(ln + b'\0' for ln in lines)) \
        .decode().strip()


def build_commit(repo, built):
    """The commit of `built` in the clone `repo`, made again on every call,
    and build_ref(built's name) pointed at it.

    Its one parent is the upstream tag (built.tag); its tree is the tag's,
    with localedata/ and locale/ replaced by the build's: each file hashed
    into the clone as its bytes are (--no-filters, so no attribute of the
    reader's changes them), each directory made with mktree. The root tree
    copies the tag's other entries as ls-tree lists them, and is made with
    --missing because those are the tag's own: on a --filter=blob:none
    clone its top-level files are not there to check, and none is read
    (measured 2026-11-03: the same commit, no object fetched). The message
    names the build and its SRPM, so two builds of one tree are two commits.

    Whatever ref of that name was there before -- an older run's, or one
    put there by hand -- is never read: the ref is set to the commit made
    now. What it made is checked before the ref is set: one parent, the
    tag; nothing outside localedata/ and locale/ different from the tag."""
    files = []
    for top, _, names in os.walk(built.tree):
        for n in names:
            full = os.path.join(top, n)
            files.append((os.path.relpath(full, built.tree), full))
    files.sort()
    ids = _git(repo, ['hash-object', '-w', '--no-filters', '--stdin-paths'],
               ''.join(f + '\n' for _, f in files).encode()).decode().split()
    if len(ids) != len(files):
        raise Refused('`git hash-object` gave %d id(s) for %d file(s) of %s'
                      % (len(ids), len(files), built.build))
    scope = {}
    for (rel, full), oid in zip(files, ids):
        parts = rel.split('/')
        if parts[0] not in ('localedata', 'locale'):
            raise Refused('%s of %s is outside localedata/ and locale/'
                          % (rel, built.build))
        d = scope
        for part in parts[:-1]:
            d = d.setdefault(part, ('tree', {}))[1]
        exe = os.stat(full).st_mode & 0o100
        d[parts[-1]] = ('blob', '100755' if exe else '100644', oid)
    root = {}
    for ent in _git(repo, ['ls-tree', '-z', built.tag + '^{tree}']) \
            .split(b'\0'):
        if not ent:
            continue
        meta, _, name = ent.partition(b'\t')
        mode, kind, oid = meta.decode().split(' ')
        root[name.decode('utf-8', 'surrogateescape')] = (kind, mode, oid)
    for name in ('localedata', 'locale'):
        if name not in scope:
            raise Refused('%s of %s holds no %s/' % (built.tree, built.build,
                                                     name))
        root[name] = scope[name]
    tree = _mktree(repo, root, missing=True)
    parent = _git(repo, ['rev-parse', '--verify',
                         built.tag + '^{commit}']).decode().strip()
    message = ('pg-glibc-audit: %s with localedata/ and locale/ of %s\n\n'
               'SRPM sha256 %s\n' % (built.tag, built.build,
                                      built.provenance.get('srpm_sha256')))
    commit = _git(repo, ['commit-tree', '--no-gpg-sign', tree, '-p', parent],
                  message.encode()).decode().strip()
    parents = _git(repo, ['rev-list', '--parents', '-n', '1', commit]) \
        .decode().split()
    if parents != [commit, parent]:
        raise Refused('the commit made for %s has parents %r, not %s alone'
                      % (built.build, parents[1:], built.tag))
    outside = _git(repo, ['diff-tree', '-r', '--name-only', parent, commit,
                          '--', '.', ':(exclude)localedata',
                          ':(exclude)locale']).decode().split('\n')
    outside = [p for p in outside if p]
    if outside:
        raise Refused('the commit made for %s differs from %s outside '
                      'localedata/ and locale/: %s'
                      % (built.build, built.tag, ', '.join(outside[:5])))
    _git(repo, ['update-ref', build_ref(asked_name(built)), commit])
    return commit


def asked_name(built):
    """The build's name as it is asked for, glibc-2.34-283.el9_8: Built.build
    without the `.src` identity() adds."""
    b = built.build
    return b[:-len('.src')] if b.endswith('.src') else b
