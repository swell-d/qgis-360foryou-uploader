#!/usr/bin/env python3
"""Build the store-ready plugin zip.

Usage:  python scripts/package.py
Output: dist/uploader_360foryou.<version>.zip  (version read from metadata.txt)

The zip contains the package folder at its root, as plugins.qgis.org expects.
Excluded: __pycache__, *.pyc, *.ts translation sources, hidden files.
Translations (*.ts) are compiled to *.qm first when lrelease is on PATH.
"""
import configparser
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = 'uploader_360foryou'
MAX_SIZE = 20 * 1024 * 1024  # plugins.qgis.org package size limit
EXCLUDED_SUFFIXES = {'.pyc', '.ts'}
EXCLUDED_NAMES = {'__pycache__'}


def read_version():
    parser = configparser.ConfigParser()
    parser.read(ROOT / PACKAGE / 'metadata.txt', encoding='utf-8')
    return parser['general']['version']


def compile_translations():
    ts_files = sorted((ROOT / PACKAGE / 'i18n').glob('*.ts'))
    if not ts_files:
        return
    lrelease = (shutil.which('lrelease') or shutil.which('lrelease-qt5')
                or shutil.which('lrelease-qt6'))
    if not lrelease:
        print('warning: lrelease not found - .qm translations were not compiled')
        return
    for ts in ts_files:
        subprocess.run([lrelease, str(ts), '-qm', str(ts.with_suffix('.qm'))], check=True)
        print('compiled', ts.with_suffix('.qm').name)


def included(rel: Path) -> bool:
    if any(part in EXCLUDED_NAMES or part.startswith('.') for part in rel.parts):
        return False
    return rel.suffix not in EXCLUDED_SUFFIXES


def main():
    version = read_version()
    compile_translations()
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    out = dist / ('%s.%s.zip' % (PACKAGE, version))
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted((ROOT / PACKAGE).rglob('*')):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if not included(rel):
                continue
            z.write(path, rel.as_posix())
    size = out.stat().st_size
    if size > MAX_SIZE:
        out.unlink()
        sys.exit('error: %s exceeds the 20 MB store limit' % out.name)
    print('built %s (%.1f KB)' % (out, size / 1024))


if __name__ == '__main__':
    main()
