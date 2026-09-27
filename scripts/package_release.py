"""Build a clean, checksummed source archive and validate the separate CSV submission."""

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['README.md', 'compose.yaml', 'Dockerfile', '.env.example', '.gitignore', '.dockerignore',
              'requirements.txt', 'requirements-dev.txt']
DIRECTORIES = ['backend', 'ndtp', 'shared', 'ml_service', 'ml_assets', 'Frontend-To-the-Moon', 'assets', 'examples', 'scripts', 'tests', 'docs']
EXCLUDED = {'node_modules', '__pycache__', '.git', 'dist', '.venv', 'artifacts', 'development-backup'}


def selected_files():
    files = [ROOT/name for name in ROOT_FILES]
    for name in DIRECTORIES:
        for path in (ROOT/name).rglob('*'):
            if not path.is_file() or any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
                continue
            if path.name in {'.env', 'AGENTS.md', 'HANDOFF.md'} or path.suffix in {'.pyc', '.zip', '.tsbuildinfo'}:
                continue
            if path.suffix == '.csv' and name == 'ml_assets':
                continue
            files.append(path)
    return sorted(files)


def package():
    output = ROOT/'submission'
    output.mkdir(exist_ok=True)
    source = ROOT/'ml_assets/catboost/submission.csv'
    target = output/'submission.csv'
    if source.exists():
        shutil.copyfile(source, target)
    with target.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        if reader.fieldnames != ['sample_id', 'prediction']:
            raise ValueError('Submission must have sample_id;prediction columns')
        rows = list(reader)
    with (ROOT/'dataset/validate/points.csv').open(encoding='utf-8-sig', newline='') as stream:
        expected = {row['sample_id'] for row in csv.DictReader(stream)}
    actual = [row['sample_id'] for row in rows]
    if len(set(actual)) != len(actual) or set(actual) != expected or not all(math.isfinite(float(row['prediction'])) for row in rows):
        raise ValueError('Submission IDs or predictions are invalid')
    files = sorted([*selected_files(), target])
    required = [ROOT/'assets/geometry.json', ROOT/'docs/code/index.html', ROOT/'docs/README.md',
                ROOT/'ml_assets/catboost/ml_module/model.cbm', ROOT/'ml_assets/catboost_classificator/risk_module/risk_model.cbm']
    if not all(path in files for path in required):
        raise ValueError('Missing runtime or documentation artifact')
    manifest = {'prepared_at': datetime.now(timezone.utc).isoformat(), 'submission_rows': len(rows),
        'submission_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'external_inputs': ['dataset/ndtp-telemetry-emulator.tar', 'dataset/validate/traffic.csv', 'dataset/validate/schedule_plan.csv'],
        'publication_status': 'prepared_locally_not_submitted',
        'files': {str(path.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    archive = output/'to-the-moon-source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package_file:
        for path in files:
            package_file.write(path, path.relative_to(ROOT))
        package_file.writestr('RELEASE-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest['archive_bytes'] = archive.stat().st_size
    (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in manifest.items() if key != 'files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    package()
