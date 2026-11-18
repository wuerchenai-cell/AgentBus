"""Atomic formal publication; locked EXE and failed registration preserve old state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone, timedelta

def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    return digest.hexdigest()

def _stage(target):
    target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + target.name + '.', suffix='.pending', dir=target.parent)
    os.close(fd); return Path(name)

def atomic_copy(source, destination):
    stage = _stage(destination)
    try:
        shutil.copyfile(source, stage)
        if stage.stat().st_size != Path(source).stat().st_size or file_hash(stage) != file_hash(source):
            raise OSError('Staged executable differs from verified source')
        with stage.open('r+b') as stream: os.fsync(stream.fileno())
        os.replace(stage, destination)
    finally: stage.unlink(missing_ok=True)

def atomic_bytes(destination, data):
    stage = _stage(destination)
    try:
        with stage.open('wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(stage, destination)
    finally: stage.unlink(missing_ok=True)

def atomic_zip(root, formal, destination):
    # Same writer and public boundary as the frozen app's template export.
    from agentbus.sharing import build_portable_package
    return build_portable_package(formal, destination,
                                  asset_root=Path(root) / '成果/agentbus')

def _refresh(root, executable):
    from agentbus.config import load
    from agentbus.project_workflow import prepare_project_workflow
    prepare_project_workflow(load(root), frozen_executable=executable)

def _managed_files(root, discover_skills):
    from agentbus.public_files import PublicFiles
    public = PublicFiles(root)
    paths = {'.agentbus/workflow.json', 'AGENTBUS.md', 'PROJECT.md', 'STATE.md'}
    wf = root / '.agentbus/workflow.json'
    if wf.exists(): paths.update(json.loads(wf.read_text(encoding='utf-8')).get('files', {}))
    if discover_skills:
        from agentbus.config import load
        from agentbus.profiles import project_skill_paths
        paths.update(project_skill_paths(load(root)).values())
    return [public.checked(rel) for rel in sorted(paths)]

def _update_identity_docs(root, size, digest):
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    path = root / 'PROJECT.md'
    if path.is_file():
        old = path.read_text(encoding='utf-8')
        new = re.sub(r'（当前实测构建：成果/AgentBus\.exe，[\d,]+ 字节，SHA256: `[0-9a-f]{64}`）',
                     f'（当前实测构建：成果/AgentBus.exe，{size:,} 字节，SHA256: `{digest}`）', old)
        new = re.sub(r'截至 \d{4}-\d{2}-\d{2}(，正式程序身份以)', '截至 '+today+r'\1', new)
        if new != old: atomic_bytes(path, new.encode('utf-8'))
    path = root / 'STATE.md'
    if path.is_file():
        old = path.read_text(encoding='utf-8')
        heading = f'## {today} | 正式程序发布登记'
        block = f'{heading}\n\n- **文件大小**：`{size:,} 字节`（`{size}`）\n- **SHA256 摘要**：`{digest}`\n- 登记以 `.agentbus/workflow.json` 为准；以下旧日期记录保留原貌。\n\n'
        pattern = r'(?ms)^'+re.escape(heading)+r'\n.*?(?=^## |\Z)'
        if re.search(pattern, old): new = re.sub(pattern, lambda _: block, old, count=1)
        else:
            first, separator, rest = old.partition('\n')
            new = first+'\n\n'+block+rest.lstrip('\n')
        if new != old: atomic_bytes(path, new.encode('utf-8'))

def deploy(root, *, refresh_workflow=None):
    root = Path(root).resolve(); sys.path.insert(0, str(root / '成果/agentbus'))
    from agentbus.operation_lock import task_lock
    source = root / '成果/agentbus/dist-release/AgentBus.exe'
    formal = root / '成果/AgentBus.exe'; bundle = root / '成果/AgentBus-win-x64-green.zip'
    if not source.is_file(): raise FileNotFoundError(f'No verified build: {source}')
    with task_lock(root / '.agentbus', 'formal-release', 'all'):
        saved = {p: p.read_bytes() if p.exists() else None for p in _managed_files(root, refresh_workflow is None)}
        backups = {}; replaced = False; preserve_backups = False
        try:
            for path in (formal, bundle):
                if path.exists():
                    stage = _stage(path); backups[path] = stage; shutil.copyfile(path, stage)
            atomic_copy(source, formal); replaced = True
            digest = file_hash(formal); size = formal.stat().st_size
            (refresh_workflow or _refresh)(root, formal)
            wf = json.loads((root / '.agentbus/workflow.json').read_text(encoding='utf-8'))
            if wf.get('launcher', {}).get('sha256') != digest:
                raise ValueError('Workflow identifies a different executable')
            _update_identity_docs(root, size, digest)
            atomic_zip(root, formal, bundle)
        except BaseException as original_error:
            if replaced:
                errors = []
                for path, data in saved.items():
                    try:
                        if data is None: path.unlink(missing_ok=True)
                        else: atomic_bytes(path, data)
                    except OSError as exc: errors.append(exc)
                for path in (formal, bundle):
                    try:
                        if path in backups: os.replace(backups[path], path)
                        else: path.unlink(missing_ok=True)
                    except OSError as exc: errors.append(exc)
                if errors:
                    preserve_backups = True
                    raise BaseExceptionGroup('发布与回滚失败；请保留 .pending 备份以恢复，不能宣称发布成功',
                                             [original_error, *errors])
            raise
        finally:
            if not preserve_backups:
                for path in backups.values(): path.unlink(missing_ok=True)
        os.replace(source, source.with_name('AgentBus.exe.deprecated_old_version'))
        return {'path': str(formal), 'bytes': size, 'sha256': digest, 'zip': str(bundle)}

def main(argv=None):
    parser = argparse.ArgumentParser(description='正式程序原子发布与登记同步')
    parser.add_argument('--confirm-formal-release', action='store_true')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    if not args.confirm_formal_release:
        print('[安全阻断] 请先构建并验证，再传入 --confirm-formal-release。', file=sys.stderr); return 2
    try: print(json.dumps(deploy(args.root), ensure_ascii=False)); return 0
    except Exception as exc:
        print(f'[发布失败] {type(exc).__name__}: {exc}', file=sys.stderr); return 1

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
