#!/usr/bin/env python3
"""Auditable, dependency-free installer; user run data always stays outside the bundle."""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

REPO = 'guasi18587278913/douyin-breakout-finder'
NAME = 'douyin-breakout-finder'
TOP = {'SKILL.md', 'README.md', 'project.json', 'install.py'}
DIRS = {'scripts', 'assets', 'agents', 'references', 'docs'}


def selected(path):
    parts = Path(path).parts
    return bool(parts) and '__pycache__' not in parts and not path.endswith('.pyc') and (
        (len(parts) == 1 and parts[0] in TOP) or (len(parts) > 1 and parts[0] in DIRS))


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != 'https' or u.hostname not in {'github.com', 'api.github.com', 'codeload.github.com', 'raw.githubusercontent.com'}:
            raise ValueError('下载被重定向到未知站点，已停止。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit=16 * 1024 * 1024):
    opener = urllib.request.build_opener(GitHubRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': NAME + '-installer', 'Accept': 'application/vnd.github+json'})
    with opener.open(request, timeout=30) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('下载大小超出限制。')
    return raw


def latest():
    data = json.loads(fetch('https://api.github.com/repos/' + REPO + '/releases/latest', 1024 * 1024))
    tag = data.get('tag_name', '')
    if data.get('draft') or data.get('prerelease') or not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('未找到有效正式版本。')
    return tag


def download(ref, destination):
    if not re.fullmatch(r'v\d+\.\d+\.\d+|[a-f0-9]{40}|main', ref):
        raise ValueError('版本应为 vX.Y.Z、main 或完整提交编号。')
    raw = fetch('https://api.github.com/repos/' + REPO + '/zipball/' + ref)
    total = 0
    seen = set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            parts = item.filename.split('/')
            if len(parts) < 2 or any(p in ('', '.', '..') for p in parts) or '\\' in item.filename or ':' in item.filename:
                raise ValueError('压缩包含有不安全路径。')
            relative = '/'.join(parts[1:])
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('发布包不应包含软链接。')
            if not selected(relative):
                continue
            total += item.file_size
            if total > 32 * 1024 * 1024 or relative in seen:
                raise ValueError('发布包大小或内容异常。')
            seen.add(relative)
            out = destination / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(archive.read(item))
    if not (destination / 'SKILL.md').is_file() or not (destination / 'project.json').is_file():
        raise ValueError('发布包缺少必要文件。')


def hashes(folder):
    result = {}
    paths = [folder / name for name in TOP if (folder / name).exists()]
    for name in DIRS:
        subfolder = folder / name
        if subfolder.is_symlink():
            raise ValueError('发布目录含有软链接，请使用普通目录。')
        if subfolder.exists():
            paths.extend(subfolder.rglob('*'))
    for path in sorted(paths):
        if not selected(path.relative_to(folder).as_posix()):
            continue
        if path.is_symlink():
            raise ValueError('安装目录含有软链接，请使用普通目录。')
        if path.is_file() and selected(path.relative_to(folder).as_posix()):
            result[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def install(source, target, ref=None, backup_root=None):
    source, target = source.resolve(), target.absolute()
    if target.is_symlink():
        raise ValueError('目标是软链接，未覆盖。请选择新目录。')
    if source == target.resolve() or source in target.resolve().parents or target.resolve() in source.parents:
        raise ValueError('源目录与目标目录不能嵌套。')
    meta = json.loads((source / 'project.json').read_text(encoding='utf-8'))
    if meta.get('name') != NAME or not (source / 'SKILL.md').is_file():
        raise ValueError('这不是有效的技能发布包。')
    if target.exists():
        marker = target / '.installation.json'
        if not marker.is_file():
            raise ValueError('目标已有文件且不由安装器管理，未覆盖。请备份旧安装或换一个目录。')
        old = json.loads(marker.read_text(encoding='utf-8'))
        if old.get('repository') != REPO or hashes(target) != old.get('files'):
            raise ValueError('检测到本地修改，已保留原文件。请先备份或合并修改，再更新。')
        extras = [p for p in target.rglob('*') if p.is_file() and p.name != '.installation.json' and '__pycache__' not in p.parts and not selected(p.relative_to(target).as_posix())]
        if extras:
            raise ValueError('安装目录含有额外文件，未覆盖。请先移出个人数据。')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + NAME + '-stage-', dir=target.parent))
    backup = None
    try:
        for relative in hashes(source):
            out = stage / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, out)
        marker = dict(repository=REPO, version=meta['version'], ref=ref, files=hashes(stage))
        (stage / '.installation.json').write_text(json.dumps(marker, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if target.exists():
            backup_root = Path(backup_root) if backup_root else Path.home() / '.local' / 'share' / NAME / 'installation-backups'
            identity = hashlib.sha256(str(target).encode()).hexdigest()[:8]
            backup = backup_root / (target.name + '-' + identity + '-' + uuid.uuid4().hex[:8])
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
        try:
            stage.rename(target)
        except Exception:
            if backup:
                shutil.move(str(backup), str(target))
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'installed': str(target), 'version': meta['version'], 'source_ref': ref,
            'backup': str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser(description='安装或更新抖音小号爆款 Skill，无需 GitHub Key。')
    parser.add_argument('--client', choices=['codex', 'claude', 'dsh', 'workbuddy', 'agents'], default='codex')
    parser.add_argument('--target', help='客户端要求的自定义技能目录（包括技能名）。')
    parser.add_argument('--source', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--download', action='store_true', help='从公开 GitHub 下载；默认正式发布版。')
    parser.add_argument('--ref', help='安装指定版本或完整提交。')
    parser.add_argument('--check', action='store_true', help='仅检查新版本，不修改安装。')
    parser.add_argument('--update', action='store_true', help='更新到 GitHub 最新正式版，保留旧版本备份。')
    args = parser.parse_args()
    roots = {'codex': '.agents/skills', 'agents': '.agents/skills', 'claude': '.claude/skills',
             'dsh': '.dsh/skills', 'workbuddy': '.agents/skills'}
    target = Path(args.target).expanduser() if args.target else Path.home() / roots[args.client] / NAME
    if args.client == 'dsh' and not args.target and os.environ.get('DSH_HOME'):
        target = Path(os.environ['DSH_HOME']).expanduser() / 'skills' / NAME
    try:
        if args.check:
            tag = latest()
            marker = target / '.installation.json'
            installed = json.loads(marker.read_text(encoding='utf-8')).get('version') if marker.is_file() else None
            print(json.dumps({'installed': installed, 'latest': tag, 'update_available': installed != tag[1:]}, ensure_ascii=False))
            return 0
        if args.download or args.update:
            ref = args.ref or latest()
            with tempfile.TemporaryDirectory(prefix=NAME + '-download-') as temp:
                source = Path(temp)
                download(ref, source)
                result = install(source, target, ref)
        else:
            result = install(Path(args.source), target, args.ref)
        if args.client == 'workbuddy':
            result['note'] = '文件已写入通用目录；请在 WorkBuddy 技能管理中添加或导入该目录。实际加载取决于客户端版本，尚未做客户端实测。'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, FileNotFoundError)) else type(error).__name__ + '：安装未完成，请检查网络或目录权限。'
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
