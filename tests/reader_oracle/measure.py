#!/usr/bin/env python3
"""Record what localedef does on the cases of cases.py, on a RHEL machine.

  python3 tests/reader_oracle/measure.py pack > bundle.json     # here
  python3 measure.py run bundle.json > rhelN.json              # on the node

`run` needs this file, the bundle, the machine's own localedef and rpm
(which names the build); it is standard library Python 3.6, for /usr/libexec/platform-python
on RHEL 8. Every case is compiled twice, without and with -c, with
`--no-archive` into a scratch directory of its own under /tmp, which is
removed at the end; the system's /usr/lib/locale/locale-archive is checked
unchanged (size and mtime) before and after, and the run refuses to write a
result if it changed. Each run of localedef has a time limit.

What is recorded per run: the exit status (negative for a signal), whether
the time limit stopped it, the LC_COLLATE file written (size, md5, and which
of the case's marker strings its bytes hold), and localedef's messages. The
file is sealed like tests/locale_order/rhelN.out: `content` is a sha256 over
every other key, so a hand edit is caught on load.
"""
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys

FORMAT = 'reader_oracle/1'
BUNDLE_FORMAT = 'reader_oracle_bundle/1'
TIME_LIMIT = 60
ROOT = '/tmp/reader_oracle'


def content_digest(data):
    """The seal: sha256 of every key but `content`, serialised the same
    way on the machine and on load."""
    sealed = {k: v for k, v in data.items() if k != 'content'}
    text = json.dumps(sealed, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True)
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def pack():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import cases as cs
    cases = cs.generate()
    out = {'format': BUNDLE_FORMAT, 'corpus': cs.corpus_digest(cases),
           'cases': [{'id': c.id, 'main': c.main,
                      'markers': c.markers,
                      'files': {n: base64.b64encode(d).decode('ascii')
                                for n, d in sorted(c.files.items())}}
                     for c in cases]}
    json.dump(out, sys.stdout, sort_keys=True)
    sys.stdout.write('\n')


def _out(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    return p.communicate()[0].decode('utf-8', 'replace').strip()


def _archive():
    path = '/usr/lib/locale/locale-archive'
    try:
        st = os.stat(path)
    except OSError as e:
        return {'error': str(e)}
    return {'size': st.st_size, 'mtime': st.st_mtime}


def _build():
    localedef = shutil.which('localedef')
    if localedef is None:
        raise SystemExit('measure.py: no localedef on this machine')
    with open(localedef, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    pretty = ''
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    pretty = line.split('=', 1)[1].strip().strip('"')
    except OSError:
        pass
    return {'rpm': _out(['rpm', '-q', 'glibc', 'glibc-common']).split('\n'),
            'localedef_sha256': digest,
            'localedef_version': _out([localedef, '--version']).split('\n')[0],
            'machine': platform.machine(), 'os': pretty}


def _one(case, src, out, force):
    if os.path.exists(out):
        shutil.rmtree(out)
    cmd = ['localedef', '--no-archive', '-i', case['main'], '-f', 'UTF-8',
           out]
    if force:
        cmd.insert(1, '-c')
    env = dict(os.environ, LC_ALL='C', I18NPATH=src)
    p = subprocess.Popen(cmd, cwd=src, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    timed_out = False
    try:
        text = p.communicate(timeout=TIME_LIMIT)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        text = p.communicate()[0]
        timed_out = True
    obs = {'rc': p.returncode, 'timed_out': timed_out,
           'messages': [m[:300] for m in
                        text.decode('utf-8', 'replace').split('\n')[:40]
                        if m]}
    lc = os.path.join(out, 'LC_COLLATE')
    if os.path.isfile(lc):
        with open(lc, 'rb') as f:
            data = f.read()
        obs['collate'] = {
            'size': len(data), 'md5': hashlib.md5(data).hexdigest(),
            'markers': sorted(m for m, text in case['markers'].items()
                              if text.encode('ascii') in data)}
    else:
        obs['collate'] = None
    obs['written'] = sorted(os.listdir(out)) if os.path.isdir(out) else []
    return obs


def run(bundle_path):
    with open(bundle_path) as f:
        bundle = json.load(f)
    if bundle.get('format') != BUNDLE_FORMAT:
        raise SystemExit('measure.py: not a bundle')
    before = _archive()
    if os.path.exists(ROOT):
        shutil.rmtree(ROOT)
    src = os.path.join(ROOT, 'src')
    outs = os.path.join(ROOT, 'out')
    os.makedirs(src)
    os.makedirs(outs)
    results = {}
    try:
        for case in bundle['cases']:
            for name, b64 in case['files'].items():
                with open(os.path.join(src, name), 'wb') as f:
                    f.write(base64.b64decode(b64))
            results[case['id']] = {
                'nc': _one(case, src, os.path.join(outs, case['id'] + '.nc'),
                           False),
                'c': _one(case, src, os.path.join(outs, case['id'] + '.c'),
                          True)}
            for name in case['files']:
                os.remove(os.path.join(src, name))
    finally:
        shutil.rmtree(ROOT)
    after = _archive()
    if before != after:
        raise SystemExit('measure.py: locale-archive changed (%r -> %r); '
                         'no result written' % (before, after))
    data = {'format': FORMAT, 'corpus': bundle['corpus'], 'build': _build(),
            'archive': before, 'cases': results}
    data['content'] = content_digest(data)
    json.dump(data, sys.stdout, sort_keys=True, indent=1)
    sys.stdout.write('\n')


if __name__ == '__main__':
    if sys.argv[1:] == ['pack']:
        pack()
    elif len(sys.argv) == 3 and sys.argv[1] == 'run':
        run(sys.argv[2])
    else:
        raise SystemExit(__doc__)
