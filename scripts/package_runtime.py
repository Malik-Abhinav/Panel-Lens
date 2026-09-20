#!/usr/bin/env python3
"""Release engineering only: build the runtime bundled in the ordinary-user app.

Uses pinned official standalone CPython and a fully hash-locked wheel set.
Users never run this script or pip. All output stays in ignored build/.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build/distribution'
RUNTIME = BUILD / 'runtime'
RESOURCES = BUILD / 'resources'
SOURCE = json.loads((ROOT / 'distribution/python-source.json').read_text())
parser = argparse.ArgumentParser()
parser.add_argument('--use-existing', action='store_true', help='Package the already assembled, version-checked runtime')
args = parser.parse_args()
if platform.system() != 'Darwin' or platform.machine() != 'arm64':
    raise SystemExit('Build on an Apple Silicon Mac, without Rosetta.')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


BUILD.mkdir(parents=True, exist_ok=True)
archive = BUILD / 'python.tar.gz'
if not archive.exists():
    urllib.request.urlretrieve(SOURCE['url'], archive)
assert digest(archive) == SOURCE['sha256'], 'Standalone Python checksum failed'
if not args.use_existing:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(RUNTIME, filter='data')
    subprocess.run([str(RUNTIME / 'python/bin/python3'), '-I', '-m', 'pip', 'install',
                    '--only-binary=:all:', '--require-hashes', '--no-cache-dir',
                    '-r', str(ROOT / 'distribution/runtime-requirements.lock')], check=True)
python = RUNTIME / 'python/bin/python3'
env = dict(os.environ, PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', PYTHONDONTWRITEBYTECODE='1')
expected = {}
for line in (ROOT / 'distribution/runtime-requirements.lock').read_text().splitlines():
    if line and not line.startswith('#'):
        name, version = line.split()[0].split('==')
        expected[name] = version
subprocess.run([str(python), '-I', '-c',
    'import importlib.metadata as m,json; expected=json.loads(' + repr(json.dumps(expected)) + '); '
    'assert all(m.version(k)==v for k,v in expected.items()), "Dependency version mismatch"; '
    'import torch,transformers,paddle,paddleocr; print("Pinned runtime imports passed")'], env=env, check=True)
ocr_sources = ROOT / '.cache/paddlex/official_models'
ocr_hashes = {}
for name in ('PP-OCRv5_mobile_det', 'korean_PP-OCRv5_mobile_rec'):
    source = ocr_sources / name
    if not source.is_dir():
        raise SystemExit(f'Missing release OCR asset: {source}. Prepare the official model before packaging.')
    destination = RUNTIME / 'ocr' / name
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.cache', '__pycache__'))
    for path in sorted(destination.rglob('*')):
        if path.is_file(): ocr_hashes[str(path.relative_to(RUNTIME))] = digest(path)
ocr_manifest = ROOT / 'distribution/ocr-assets.json'
if ocr_manifest.exists():
    assert json.loads(ocr_manifest.read_text()) == ocr_hashes, 'OCR asset hash mismatch'
else:
    ocr_manifest.write_text(json.dumps(ocr_hashes, indent=2)+'\n')
# Preserve all package dist-info licenses and Python notices. Console entry points
# have build-time absolute shebangs; no consumer uses them, so don't distribute them.
for path in (RUNTIME / 'python/bin').iterdir():
    if path.name not in {'python', 'python3', 'python3.12'}:
        path.unlink()
for path in RUNTIME.rglob('__pycache__'):
    if path.is_dir(): shutil.rmtree(path)
# Paddle's arm64 inference core links Apple's Accelerate. Its wheel also ships
# unused OpenBLAS/Fortran libraries that refer to non-existent Homebrew GCC.
# Exclude only those unused libraries, after proving no retained library links them.
unused = {'libblas.dylib', 'liblapack.dylib', 'libgfortran.5.dylib', 'libquadmath.0.dylib', 'libgcc_s.1.dylib'}
paddle_libs = RUNTIME / 'python/lib/python3.12/site-packages/paddle/libs'
for path in RUNTIME.rglob('*'):
    if path.is_file() and not path.is_symlink() and path.suffix in {'.so', '.dylib'} and path.name not in unused:
        linked = subprocess.check_output(['/usr/bin/otool', '-L', str(path)], text=True)
        assert not any('/' + name + ' ' in linked for name in unused), str(path)
for name in unused:
    (paddle_libs / name).unlink(missing_ok=True)
# Audit native linking: no Homebrew, pyenv, repository, or /usr/local dependencies.
for path in RUNTIME.rglob('*'):
    if path.is_file() and not path.is_symlink() and (path.suffix in {'.so', '.dylib'} or path.name == 'python3.12'):
        result = subprocess.run(['/usr/bin/otool', '-L', str(path)], capture_output=True, text=True, check=True)
        dependencies = [line for line in result.stdout.splitlines() if line.startswith("\t")]
        # Some wheels retain a Homebrew LC_ID_DYLIB, not a load dependency.
        if path.suffix == '.dylib' and dependencies and any(prefix in dependencies[0] for prefix in ('/opt/homebrew/', '/usr/local/')):
            subprocess.run(['/usr/bin/install_name_tool', '-id', '@rpath/' + path.name, str(path)], check=True)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], check=True)
            dependencies = dependencies[1:]
        assert not any(prefix in line for line in dependencies for prefix in ('/opt/homebrew/', '/usr/local/', '/Users/')), str(path)
# Signed release builds sign native runtime code before archiving. Ad-hoc/local
# builds deliberately remain clearly marked as not notarized distribution builds.
identity = os.environ.get('PANELLENS_SIGN_IDENTITY')
if identity:
    for path in RUNTIME.rglob('*'):
        if path.is_file() and not path.is_symlink() and (path.suffix in {'.so', '.dylib'} or path.name == 'python3.12'):
            subprocess.run(['/usr/bin/codesign', '--force', '--options', 'runtime', '--timestamp', '--sign', identity, str(path)], check=True)
RESOURCES.mkdir(parents=True, exist_ok=True)
output = RESOURCES / 'runtime.tar.gz'
with tarfile.open(output, 'w:gz', compresslevel=6) as package:
    for name in ('python', 'ocr'):
        package.add(RUNTIME / name, arcname=name)
installed = sum(p.stat().st_size for p in RUNTIME.rglob('*') if p.is_file() and not p.is_symlink())
manifest = {'version': '1.0.0', 'platform': 'macos-arm64', 'minimumMacOS': '14.0',
            'python': SOURCE['version'], 'signingIdentity': identity, 'archive': output.name, 'sha256': digest(output),
            'archiveBytes': output.stat().st_size, 'installedBytes': installed}
(RESOURCES / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
print(json.dumps(manifest, indent=2))
