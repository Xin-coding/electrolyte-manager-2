"""Package verified build files and public documentation, never experiment data."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from em2 import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=SOURCE).strip():
        raise SystemExit('Packaging requires a clean checkout')
    build = json.loads(args.build_manifest.read_text(encoding='utf-8'))
    if build['version'] != __version__:
        raise SystemExit('Built version does not match source')
    changed = subprocess.check_output(['git', 'diff', '--name-only', build['source_commit'], 'HEAD', '--', 'em2', 'pyproject.toml', 'tools/build_release.py'], cwd=SOURCE).strip()
    if changed:
        raise SystemExit('Application changed since the verified build')
    app = Path(build['app'])
    files = {p.relative_to(app).as_posix(): p for p in app.rglob('*') if p.is_file()}
    if files.keys() != build['files'].keys():
        raise SystemExit('Build file inventory changed')
    for name, path in files.items():
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != build['files'][name]:
            raise SystemExit('Build file was replaced: '+name)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, text=True).strip()
    manifest = {'version': __version__, 'release_channel': 'stable', 'source_commit': build['source_commit'],
                'documentation_commit': head, 'built_utc': build['built_utc'],
                'packaged_utc': datetime.now(timezone.utc).isoformat(),
                'exe_sha256': build['files']['ElectrolyteManager.exe'],
                'dependencies': {name: metadata.version(name) for name in ('PySide6', 'pyqtgraph', 'pyserial', 'numpy')},
                'validation': 'See release notes and docs/VALIDATION.md. M80 hardware acceptance is read-only; other devices have software regression coverage. Earlier endurance, gravimetric and physical-fault acceptance remains incomplete.'}
    manifest_path = output/'release.json'
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = output/f'ElectrolyteManager-{__version__}-Windows-x64.zip'
    prefix = 'Electrolyte-Manager-3.0/'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, path in files.items():
            z.write(path, prefix+'app/'+name)
        for name in ('PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'pyqtgraph', 'pyserial', 'numpy', 'pyinstaller', 'packaging'):
            dist = metadata.distribution(name)
            for file in dist.files or []:
                if file.name.lower().startswith(('license', 'copying', 'authors')):
                    z.write(dist.locate_file(file), prefix+'licenses/'+name+'/'+str(file).replace('..', '_').replace('\\', '/'))
        python_license = Path(sys.base_prefix)/'LICENSE.txt'
        if python_license.exists():
            z.write(python_license, prefix+'licenses/Python-LICENSE.txt')
        for name in ('LGPL-3.0.txt', 'GPL-3.0.txt'):
            path = SOURCE.parent/'licenses'/name
            if not path.exists():
                raise SystemExit('Missing public license: '+str(path))
            z.write(path, prefix+'licenses/'+name)
        for path in (SOURCE/'README.md', SOURCE/'LICENSE', manifest_path):
            z.write(path, prefix+path.name)
        for path in (SOURCE/'docs').glob('*.md'):
            z.write(path, prefix+'docs/'+path.name)
        z.writestr(prefix+'data/.keep', '')
        z.writestr(prefix+'exports/.keep', '')
    with zipfile.ZipFile(archive) as z:
        bad = [name for name in z.namelist() if name.lower().endswith(('.sqlite', '.sqlite3', '.log', '.key', '.pem')) or '/data/' in name and not name.endswith('.keep')]
        if bad or z.testzip():
            raise SystemExit('Package integrity or private content check failed')
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n', encoding='ascii')
    report = {**manifest, 'archive': str(archive), 'sha256': digest, 'private_content_check': 'passed'}
    (output/'package-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
