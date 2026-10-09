"""Public source fingerprints; never walks run data or private configuration."""
import hashlib
import json
from pathlib import Path

TOP = ('AGENTS.md', 'SKILL.md', 'README.md', 'project.json', 'install.py')
DIRECTORIES = ('scripts', 'assets', 'agents', 'references', 'docs')


def source_manifest(root):
    root = Path(root).resolve()
    paths = [root / name for name in TOP if (root / name).exists()]
    for name in DIRECTORIES:
        folder = root / name
        if folder.is_symlink():
            raise ValueError('Source directories must not be symlinks.')
        if folder.exists():
            paths.extend(folder.rglob('*'))
    result = {}
    for path in sorted(paths):
        relative = path.relative_to(root)
        if '__pycache__' in relative.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError('Source files must not be symlinks.')
        if path.is_file():
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def source_identity(root):
    root = Path(root).resolve()
    files = source_manifest(root)
    raw = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'source_dir': str(root), 'sha256': hashlib.sha256(raw).hexdigest(), 'file_count': len(files)}
