"""Portable Windows onefile builder. Source: Codex, 2026-10-03.

Run in an explicit build venv after installing requirements-build.txt.
No packages are installed by this tool. No model or authentication is called.
"""
from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import hashlib
import json
import os
import subprocess
import sys
import shutil
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_PROJECT_ROOT_ENV = 'AGENTBUS_TEST_PROJECT_ROOT'


def build_offline_test_env(project_root: Path | None, base_env=None, *, source_root: Path | None = None) -> dict[str, str]:
    """Prepare a clean environment for tests run from an exported snapshot.

    Host-integrity tests need an explicit project root; portable source-package
    builds must not inherit a stale path from the caller's environment.
    """
    env = dict(os.environ if base_env is None else base_env)
    env.pop(TEST_PROJECT_ROOT_ENV, None)
    env.pop("AGENTBUS_TEST_SOURCE_ROOT", None)
    if source_root is not None:
        env["AGENTBUS_TEST_SOURCE_ROOT"] = str(Path(source_root).resolve())
    if project_root is None:
        return env
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir() or not (root / '.agentbus' / 'config.json').is_file():
        raise ValueError(f'--project-root must contain .agentbus/config.json: {root}')
    env[TEST_PROJECT_ROOT_ENV] = str(root)
    return env


def validate_web_build_environment() -> None:
    """Fail before freezing if the pinned Windows desktop runtime is missing."""
    if sys.version_info[:2] != (3, 12):
        raise ValueError('Build the WebView2 desktop with Python 3.12 and requirements-build.txt.')
    for name, expected in (('pywebview', '6.2.1'), ('pythonnet', '3.0.5'), ('clr-loader', '0.2.8')):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError('Missing WebView2 build dependency: ' + name) from exc
        if actual != expected:
            raise ValueError(f'WebView2 build dependency {name} must be {expected}, found {actual}.')


def validate_share_assets(root: Path) -> None:
    from tools.export_source import LICENSE_NAMES, _license_hashes, _read, _source_path
    root = Path(root)
    assets, _ = _source_path(root, 'share_assets', directory=True)
    licenses, _ = _source_path(root, 'share_assets/licenses', directory=True)
    if {p.name for p in assets.iterdir()} != {'开始使用.md', 'THIRD_PARTY_NOTICES.md', 'licenses'}:
        raise ValueError('Unexpected share_assets entries; use a clean release source copy.')
    if {p.name for p in licenses.iterdir()} != LICENSE_NAMES | {'provenance.json'}:
        raise ValueError('Unexpected license resources; release asset allowlist needs review.')
    for name in ['开始使用.md', 'THIRD_PARTY_NOTICES.md']:
        _read(root, 'share_assets/' + name)
    hashes = _license_hashes(_read(root, 'share_assets/licenses/provenance.json'))
    for relative, expected in hashes.items():
        if hashlib.sha256(_read(root, 'share_assets/' + relative)).hexdigest() != expected:
            raise ValueError('Release license hash mismatch: ' + relative)


def build_command(root: Path, build_dir: Path, dist_dir: Path, sdk_runtime: Path) -> list[str]:
    root, build_dir, dist_dir, sdk_runtime = map(Path, (root, build_dir, dist_dir, sdk_runtime))
    root = root.resolve()
    for output in [build_dir, dist_dir]:
        resolved = output.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError('Build outputs must be separate directories inside the source root.')
    from tools.export_source import APP_ICON_FILE, WEB_ASSET_FILES
    required = [root / 'entry.py', root / 'schemas/plan.schema.json',
                root / 'skills/agentbus-team/SKILL.md', root / 'share_assets/开始使用.md',
                sdk_runtime / 'bin/codex.exe', root / 'LICENSE', root / 'SOURCE_LICENSE_STATUS.md']
    required += [root / 'docs/public' / name
                 for name in ('README.md', 'RELEASE.md', 'ECOSYSTEM.md', 'SECURITY.md')]
    if any(not path.is_file() for path in required):
        raise ValueError('Source resources or packaged codex_cli_bin/bin/codex.exe are missing.')
    web_desktop = (root / 'agentbus/web_desktop.py').is_file()
    if web_desktop:
        if any(not (root / relative).is_file() for relative in WEB_ASSET_FILES):
            raise ValueError('Web desktop assets are missing from the release source.')
        if not (root / APP_ICON_FILE).is_file():
            raise ValueError('Web desktop application icon is missing: ' + APP_ICON_FILE)
    command = [sys.executable, '-m', 'PyInstaller', '--onefile', '--noconfirm',
               '--noconsole', '--name', 'AgentBus', '--paths', str(root),
               '--distpath', str(dist_dir.resolve()), '--workpath', str(build_dir.resolve() / 'work'),
               '--specpath', str(build_dir.resolve() / 'spec'),
               '--collect-all', 'openai_codex', '--hidden-import', 'codex_cli_bin',
               '--hidden-import', 'tools.export_source',
               '--copy-metadata', 'openai-codex', '--copy-metadata', 'openai-codex-cli-bin']
    if web_desktop:
        command += ['--icon', str(root / APP_ICON_FILE)]
        for module in ('webview', 'pythonnet', 'clr_loader'):
            command += ['--collect-all', module]
        command += ['--copy-metadata', 'pywebview', '--copy-metadata', 'pythonnet',
                    '--copy-metadata', 'clr-loader']
        from tools.export_source import OPTIONAL_WEB_ASSET_FILES
        web_assets = list(WEB_ASSET_FILES) + [f for f in OPTIONAL_WEB_ASSET_FILES if (root / f).is_file()]
        for relative in web_assets:
            command += ['--add-data', str(root / relative) + ';agentbus/web_assets']
        command += ['--add-data', str(root / APP_ICON_FILE) + ';agentbus/assets']
        for module in ('agentbus.web_desktop', 'agentbus.web_bridge', 'tools.smoke_frozen',
                       'agentbus.tree_templates', 'agentbus.task_assignment',
                       'agentbus.handover', 'agentbus.supervision'):
            command += ['--hidden-import', module]
    for source, destination in [(root / 'schemas', 'schemas'),
                                (root / 'skills/agentbus-team', 'skills/agentbus-team'),
                                (root / 'share_assets', 'share_assets'),
                                (sdk_runtime, 'codex_cli_bin'),
                                (root / 'agentbus/trigger/manifests', 'agentbus/trigger/manifests'),
                                (root / 'agentbus/host_assets', 'agentbus/host_assets')]:
        command += ['--add-data', str(source) + ';' + destination]
    for name in ('LICENSE', 'SOURCE_LICENSE_STATUS.md'):
        command += ['--add-data', str(root / name) + ';.']
    for name in ('README.md', 'RELEASE.md', 'ECOSYSTEM.md', 'SECURITY.md'):
        command += ['--add-data', str(root / 'docs/public' / name) + ';docs/public']
    for name in ['tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox',
                 'tkinter.scrolledtext', 'tkinter.simpledialog', 'agentbus.catalog',
                 'agentbus.detect', 'agentbus.gui', 'agentbus.gui_state', 'agentbus.watchtower',
                 'agentbus.mcp_server', 'agentbus.mcp_bridge', 'agentbus.codex_sdk', 'agentbus.acp',
                 'agentbus.lease', 'agentbus.trigger', 'agentbus.trigger.service',
                 'agentbus.trigger.contracts', 'agentbus.trigger.connectors.codex',
                 'agentbus.trigger.connectors.codearts', 'agentbus.trigger.connectors.acp',
                 'agentbus.trigger.connectors.universal']:
        command += ['--hidden-import', name]
    return command + [str(root / 'entry.py')]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--build-dir', default='.release-build')
    parser.add_argument('--dist-dir', default='dist-release')
    parser.add_argument('--project-root', type=Path,
                        help='Explicit host project root for host-integrity tests')
    parser.add_argument('--check-timeout', type=float, default=1200.0,
                        help='Timeout for tools/check_offline.py in seconds (default 1200s)')
    parser.add_argument('--skip-tests', action='store_true',
                        help='Skip running full check_offline.py if already verified by caller')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if os.name != 'nt':
            raise ValueError('The published desktop target is Windows; other platforms are not certified.')
        from tools.export_source import validate_source_root
        root = validate_source_root(args.root)
        if (root / 'agentbus/web_desktop.py').is_file():
            validate_web_build_environment()
        spec = importlib.util.find_spec('codex_cli_bin')
        if spec is None or not spec.submodule_search_locations:
            raise ValueError('Use a build venv with requirements-build.txt installed first.')
        runtime = Path(next(iter(spec.submodule_search_locations)))
        command = build_command(root, root / args.build_dir, root / args.dist_dir, runtime)
        if args.dry_run:
            print(json.dumps({'argv': command, 'cwd': str(root), 'model_calls': 0}))
            return 0
        # Build and test the exact allowlisted snapshot. This keeps stray files
        # and later edits in the developer checkout out of PyInstaller's input.
        from tools.export_source import export_source
        validate_share_assets(root)
        # §10.7 Release Gate: Formal source tree synchronization check
        from tools.check_formal_sync import audit_formal_sync
        manifest_file = root / "packaging/BUILD-MANIFEST.json"
        sync_res = audit_formal_sync(manifest_file, root_dir=root)
        if not sync_res.get("passed", False):
            raise ValueError(f"Release Gate failure: formal source tree is not synchronized ({sync_res.get('status_statement')})")

        # §10.19 Release Gate: Delivery self-proof and test truthfulness gate
        from tools.delivery_self_proof import verify_delivery_self_proof
        proof_delivery = {
            "artifacts": [
                {"path": "tests/test_delivery_self_proof.py"},
                {"path": "tests/test_broad_except_locator.py"},
                {"path": "tests/test_launcher_truth_check.py"}
            ],
            "body": "Release Gate delivery self-proof preflight"
        }
        proof_res = verify_delivery_self_proof(proof_delivery, root)
        if proof_res.get("verdict") not in ("pass", "pass_with_waiver"):
            raise ValueError(f"Release Gate failure: delivery self-proof gate rejected ({proof_res.get('reasons')})")
        build_dir = root / args.build_dir
        build_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='source-', dir=build_dir) as directory:
            temporary = Path(directory)
            package = temporary / 'input.zip'
            exported = export_source(root, package)
            snapshot = temporary / 'source'
            with zipfile.ZipFile(package) as archive:
                archive.extractall(snapshot)  # locally generated fixed whitelist
            if not args.skip_tests:
                if args.project_root is None:
                    raise ValueError('--project-root is required: portable tests cannot certify host release integrity')
                # Both scopes must pass. The host source is explicit, while the
                # portable run must not infer another project from its ancestors.
                subprocess.run([sys.executable, '-B', '-X', 'utf8', str(snapshot / 'tools/check_offline.py'), '--scope', 'host'],
                               cwd=snapshot, check=True, timeout=int(args.check_timeout),
                               env=build_offline_test_env(args.project_root, source_root=root))
                subprocess.run([sys.executable, '-B', '-X', 'utf8', str(snapshot / 'tools/check_offline.py'), '--scope', 'portable'],
                               cwd=snapshot, check=True, timeout=int(args.check_timeout),
                               env=build_offline_test_env(None))
            else:
                print("[build_release] Skipping check_offline.py per --skip-tests flag")
            command = build_command(snapshot, snapshot / '.build', snapshot / 'dist', runtime)
            subprocess.run(command, cwd=snapshot, check=True, timeout=1800)
            candidate = snapshot / 'dist/AgentBus.exe'
            subprocess.run([sys.executable, '-B', '-X', 'utf8', str(snapshot / 'tools/smoke_frozen.py'),
                            str(candidate)], cwd=snapshot, check=True, timeout=300)
            final_dir = root / args.dist_dir
            final_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix='.AgentBus-', suffix='.exe', dir=final_dir)
            os.close(fd)
            staged = Path(temp_name)
            try:
                shutil.copyfile(candidate, staged)
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if hashlib.sha256(staged.read_bytes()).hexdigest() != digest:
                    raise ValueError('Copied release binary hash differs from the verified candidate.')
                os.replace(staged, final_dir / 'AgentBus.exe')
            finally:
                staged.unlink(missing_ok=True)
            print(json.dumps({'exe': str(final_dir / 'AgentBus.exe'), 'sha256': digest,
                              'source_sha256': exported['sha256'], 'model_calls': 0}))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
