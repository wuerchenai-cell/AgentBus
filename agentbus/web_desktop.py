"""AgentBus local WebView2 desktop shell.

Source: Codex, 2026-10-09. GUI imports stay lazy so CLI/MCP need no webview.
The frontend is packaged local content; only the allowlisted call API is exposed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urldefrag

WEB_ASSETS = ('index.html', 'app.css', 'app.js')
APP_ICON = Path('agentbus/assets/agentbus.ico')
WEBVIEW2_INSTALL_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/#download-section'
WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'


class DesktopStartupError(RuntimeError):
    """An actionable desktop dependency or local-resource failure."""


def _public_path(path: Path | str) -> Path:
    supplied = Path(path).expanduser()
    private = {'.workbuddy', 'do not access', '不要访问'}
    if any(part.casefold() in private for part in supplied.parts):
        raise DesktopStartupError('A private directory cannot be accessed.')
    resolved = supplied.resolve()
    if any(part.casefold() in private for part in resolved.parts):
        raise DesktopStartupError('A private directory cannot be accessed.')
    return resolved


def asset_directory(workspace: Path | str | None = None) -> Path:
    """Frozen releases use embedded assets; source development permits overrides."""
    extraction = getattr(sys, '_MEIPASS', None)
    if workspace and not (extraction or getattr(sys, 'frozen', False)):
        ws = Path(workspace).resolve()
        for candidate in (ws / '.agentbus' / 'web_assets',
                          ws / '成果' / 'agentbus' / 'pilots' / 'product-convergence-20261008' / 'integration' / 'agentbus' / 'web_assets'):
            if candidate.is_dir() and all((candidate / name).is_file() for name in WEB_ASSETS):
                return candidate
    assets = ((Path(extraction) / 'agentbus/web_assets') if extraction
              else Path(__file__).resolve().parent / 'web_assets')
    missing = [name for name in WEB_ASSETS if not (assets / name).is_file()]
    if missing:
        raise DesktopStartupError('AgentBus Web resources are missing: ' + ', '.join(missing))
    return assets


def app_icon_path() -> Path:
    """Resolve the same multi-size icon embedded into the native window and EXE."""
    extraction = getattr(sys, '_MEIPASS', None)
    icon = ((Path(extraction) / APP_ICON) if extraction
            else Path(__file__).resolve().parent.parent / APP_ICON)
    if not icon.is_file():
        raise DesktopStartupError('AgentBus application icon is missing: ' + APP_ICON.as_posix())
    return icon


def select_workspace(explicit: str | Path | None = None, *, cwd: Path | None = None) -> Path | None:
    """Use an explicit folder, current project, or valid registered project.

    Never traverse ancestors or guess a workspace from the EXE location.
    No remembered folder becomes active unless it still has project config.
    """
    if explicit:
        chosen = _public_path(explicit)
        if not chosen.is_dir():
            raise DesktopStartupError('Workspace directory does not exist: ' + str(chosen))
        return chosen
    current = _public_path(cwd or Path.cwd())
    if (current / '.agentbus/config.json').is_file():
        return current
    from .gui_state import load_last_workspace
    registered = load_last_workspace()
    if registered:
        try:
            chosen = _public_path(registered)
            if (chosen / '.agentbus/config.json').is_file():
                return chosen
        except (DesktopStartupError, OSError):
            pass  # 资源清理类：窗口选择取消/失败即返回，由调用方处理。
    return None


def _version_key(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', value):
        return ()
    parts = tuple(map(int, value.split('.')))
    return parts if any(parts) else ()


def _runtime_registry_versions() -> list[dict]:
    """Read only Microsoft's two public Runtime product registrations.

    Microsoft distribution guidance: /microsoft-edge/webview2/concepts/distribution.
    No browser profile, account, authentication, or identity configuration is read.
    """
    import winreg
    result = []
    for hive, label in ((winreg.HKEY_LOCAL_MACHINE, 'HKLM'), (winreg.HKEY_CURRENT_USER, 'HKCU')):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            path = 'SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\' + WEBVIEW2_GUID
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                    version, _ = winreg.QueryValueEx(key, 'pv')
                if _version_key(version):
                    result.append({'version': version, 'source': label + '\\' + path})
            except OSError:
                continue
    return result


def _runtime_directory_versions() -> list[dict]:
    """Inspect version-directory names and executable presence in public installs."""
    bases = [os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')]
    local = os.environ.get('LOCALAPPDATA')
    bases.append(local)
    result = []
    for base in dict.fromkeys(value for value in bases if value):
        directory = Path(base) / 'Microsoft/EdgeWebView/Application'
        try:
            for child in directory.iterdir():
                if _version_key(child.name) and (child / 'msedgewebview2.exe').is_file():
                    result.append({'version': child.name, 'source': str(child)})
        except OSError:
            continue
    return result


def runtime_diagnostics(*, platform_name: str | None = None) -> dict:
    platform_name = platform_name or sys.platform
    versions = []
    if platform_name == 'win32':
        versions = _runtime_registry_versions() + _runtime_directory_versions()
    valid = sorted((item for item in versions if _version_key(item.get('version'))),
                   key=lambda item: _version_key(item['version']), reverse=True)
    try:
        package = importlib.metadata.version('pywebview')
    except importlib.metadata.PackageNotFoundError:
        package = None
    return {'platform': platform_name, 'python_version': sys.version.split()[0],
            'frozen': bool(getattr(sys, 'frozen', False)), 'pywebview_version': package,
            'requested_renderer': 'edgechromium', 'webview2_installed': bool(valid),
            'webview2_version': valid[0]['version'] if valid else None,
            'runtime_evidence': valid, 'install_url': WEBVIEW2_INSTALL_URL,
            'guidance': '请安装 Microsoft Edge WebView2 Evergreen Runtime，然后重新打开 AgentBus。'}


def show_startup_error(message: str) -> None:
    """Visible native error for interactive startup; no alternate GUI backend."""
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(message), 'AgentBus 启动失败', 0x10)


def _write_report(path: Path | None, value: dict) -> None:
    if path is None:
        return
    path = _public_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _PageAPI:
    """Private facade: pywebview may recursively expose public attributes."""
    def __init__(self, bridge):
        self._bridge = bridge
        self._enabled = False

    def call(self, action, payload=None):
        if not self._enabled:
            return {'ok': False, 'code': 'DesktopNotReady',
                    'error': '本地桌面尚未就绪，或窗口已关闭。'}
        return self._bridge.call(action, payload)


def _bind_workspace_drop(window, bridge, report: dict | None = None) -> bool:
    """Bind pywebview's native file-drop event to the validated workspace switch."""
    try:
        from webview.dom import DOMEventHandler
        events = window.dom.document.events
    except Exception as exc:
        if report is not None:
            report['workspace_drop_available'] = False
            report['workspace_drop_error'] = str(exc)[:240]
        return False

    def on_drop(event):
        transfer = event.get('domTransfer') or event.get('dataTransfer') or {}
        files = transfer.get('files', []) if isinstance(transfer, dict) else []
        if not isinstance(files, (list, tuple)) or len(files) != 1:
            window.evaluate_js('window.AgentBusUI?.workspaceDropRejected?.()')
            return
        dropped = files[0]
        raw_path = dropped.get('pywebviewFullPath') if isinstance(dropped, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            window.evaluate_js('window.AgentBusUI?.workspaceDropRejected?.()')
            return
        result = bridge.call('set_workspace', {'workspace': raw_path})
        if not result.get('ok'):
            if report is not None:
                report['workspace_drop_error'] = result.get('error', 'workspace rejected')[:240]
            window.evaluate_js('window.AgentBusUI?.workspaceDropRejected?.()')
            return
        if report is not None:
            report['workspace_drop_count'] = report.get('workspace_drop_count', 0) + 1
        window.evaluate_js('window.AgentBusUI?.workspaceDropped?.()')

    try:
        prevent = lambda _event: None
        events.dragenter += DOMEventHandler(prevent, True, True)
        events.dragover += DOMEventHandler(prevent, True, True, debounce=80)
        events.drop += DOMEventHandler(on_drop, True, True)
    except Exception as exc:
        if report is not None:
            report['workspace_drop_available'] = False
            report['workspace_drop_error'] = str(exc)[:240]
        return False
    if report is not None:
        report['workspace_drop_available'] = True
    return True


def _guard_navigation(window, state: dict) -> None:
    """Cancel navigation away from the packaged page before remote code loads.

    This uses the pinned pywebview 6.2.1 Windows native WebView2 control in its
    documented before_show event. New windows open outside this JS API surface.
    """
    expected = urldefrag(window.real_url)[0]
    control = window.native.browser.webview

    def navigation_starting(sender, args):
        if urldefrag(str(args.Uri))[0] != expected:
            args.Cancel = True
            state['blocked_navigation_count'] += 1

    control.NavigationStarting += navigation_starting
    state['navigation_guard_installed'] = True
    state['_navigation_handler'] = navigation_starting  # retain the .NET delegate


def _bridge_summary(response: dict) -> dict:
    data = response.get('data') if isinstance(response, dict) else None
    data = data if isinstance(data, dict) else {}
    tree = data.get('tree', {})
    nodes = tree.get('nodes', {}) if isinstance(tree, dict) else {}
    if not nodes:
        nodes = data.get('nodes', {})
    forest = data.get('forest', {})
    counts = {name: len(forest.get(name, [])) if isinstance(forest, dict)
              and isinstance(forest.get(name, []), list) else 0
              for name in ('roots', 'branches', 'leaves')}
    return {'ok': bool(response.get('ok')) if isinstance(response, dict) else False,
            'node_count': sum(counts.values()) if 'forest' in data else
                          len(nodes) if isinstance(nodes, (dict, list)) else 0,
            'root_count': counts['roots'], 'branch_count': counts['branches'],
            'leaf_count': counts['leaves'],
            'data_keys': sorted(data), 'error': response.get('error') if isinstance(response, dict) else None}


def _inspect_window(window, report: dict, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    try:
        if not window.events.loaded.wait(timeout_s):
            raise DesktopStartupError('WebView2 window load timed out.')
        window.evaluate_js("""window.__agentbusDesktopSmoke = {done:false};
          Promise.resolve(window.pywebview.api.call('snapshot', {})).then(function(value) {
            window.__agentbusDesktopSmoke = {done:true, response:value};
          }).catch(function(error) {
            window.__agentbusDesktopSmoke = {done:true, error:String(error)};
          }); true;""")
        while time.monotonic() < deadline:
            observed = window.evaluate_js("""({
              readyState:document.readyState,
              bridgeAvailable:!!(window.pywebview && window.pywebview.api && window.pywebview.api.call),
              domNodeCount:document.querySelectorAll('*').length,
              title:document.title,
              bodyText:document.body.innerText.slice(0,800),
              frontend:window.AgentBusUI && window.AgentBusUI.inspect ? window.AgentBusUI.inspect() : null,
              snapshot:window.__agentbusDesktopSmoke,
              jsError:window.__lastJsError || null,
              userAgent:navigator.userAgent
            })""")
            # The report stores counts, readiness and a bounded DOM excerpt,
            # never a copy of the complete project snapshot or claim tickets.
            report['dom'] = {key: value for key, value in observed.items() if key != 'snapshot'}
            smoke = observed.get('snapshot') or {}
            frontend = observed.get('frontend') or {}
            if smoke.get('done'):
                report['bridge_snapshot'] = _bridge_summary(smoke.get('response', {}))
            if smoke.get('done') and frontend.get('ready'):
                report['success'] = bool(observed['readyState'] == 'complete'
                                         and observed['bridgeAvailable']
                                         and observed['domNodeCount'] > 0
                                         and report['bridge_snapshot']['ok']
                                         and report.get('renderer') == 'edgechromium')
                if not report['success']:
                    report['error'] = 'Window loaded but DOM/bridge/renderer checks failed.'
                return
            time.sleep(0.1)
        raise DesktopStartupError('Frontend readiness or bridge snapshot timed out.')
    except Exception as exc:
        report.update(success=False, error=str(exc))
    finally:
        window.destroy()


def _launch(workspace: Path | None, *, smoke: bool, timeout_s: float, report: dict) -> int:
    diagnostic = runtime_diagnostics()
    report['runtime'] = diagnostic
    if diagnostic['platform'] != 'win32':
        raise DesktopStartupError('AgentBus Web desktop currently requires Windows and WebView2.')
    if not diagnostic['webview2_installed']:
        raise DesktopStartupError(diagnostic['guidance'] + '\n' + WEBVIEW2_INSTALL_URL)
    assets = asset_directory(workspace)
    try:
        import webview
    except Exception as exc:
        raise DesktopStartupError('WebView2 desktop dependencies could not load. '
                                  'Use Python 3.12 and requirements-build.txt.\n' + str(exc)) from exc
    from .web_bridge import WebBridge
    bridge = WebBridge(workspace=str(workspace) if workspace else None,
                        remember_workspace=True)
    webview.settings['REMOTE_DEBUGGING_PORT'] = None
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
    page_api = _PageAPI(bridge)
    window = webview.create_window('AgentBus · 项目森林', url=str(assets / 'index.html'),
                                  js_api=page_api, width=1440, height=920,
                                  min_size=(1000, 650), background_color='#f5f2eb')
    if window is None:
        raise DesktopStartupError('WebView2 did not create an AgentBus window.')
    bridge.bind_window(window)
    state = {'blocked_navigation_count': 0, 'navigation_guard_installed': False}
    cleanup_done = threading.Event()

    def initialized(renderer):
        report['renderer'] = renderer
        if renderer != 'edgechromium':
            report['error'] = 'WebView2 renderer unavailable; alternate renderer was rejected.'
            return False

    def closed():
        try:
            page_api._enabled = False
            report['window_closed'] = True
            bridge.bind_window(None)
            report['bridge_unbound'] = True
            state.pop('_navigation_handler', None)
        finally:
            cleanup_done.set()

    def before_show():
        _guard_navigation(window, state)
        page_api._enabled = True

    def bind_workspace_drop(*_args):
        if state.get('workspace_drop_bound'):
            return
        state['workspace_drop_bound'] = _bind_workspace_drop(window, bridge, report)

    window.events.initialized += initialized
    window.events.loaded += bind_workspace_drop
    window.events.before_show += before_show
    window.events.closed += closed
    report['asset_directory'] = str(assets)
    report['workspace'] = str(workspace) if workspace else None
    webview.start(func=(lambda: _inspect_window(window, report, timeout_s)) if smoke else None,
                  gui='edgechromium', debug=False, private_mode=True, http_server=False,
                  icon=str(app_icon_path()))
    # The initialized event refuses a fallback before the native window opens.
    if report.get('renderer') != 'edgechromium':
        bridge.bind_window(None)
        raise DesktopStartupError((report.get('error') or 'WebView2 initialization failed.') + '\n'
                                  + diagnostic['guidance'] + '\n' + WEBVIEW2_INSTALL_URL)
    cleanup_done.wait(5)
    report['navigation_guard_installed'] = state['navigation_guard_installed']
    report['blocked_navigation_count'] = state['blocked_navigation_count']
    if not state['navigation_guard_installed']:
        raise DesktopStartupError('The local page navigation guard could not be installed.')
    if not smoke:
        report['success'] = True
    return 0 if report.get('success') else 1


def _bounded_smoke(args) -> tuple[int, dict]:
    """Own and reap only this smoke process and its WebView2 descendants."""
    from tools.smoke_frozen import _WindowsJob
    with tempfile.TemporaryDirectory(prefix='agentbus-web-smoke-') as directory:
        child_report = Path(directory) / 'window.json'
        command = ([sys.executable] if getattr(sys, 'frozen', False) else
                   [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve().parents[1] / 'entry.py')])
        command += ['gui', '--_smoke-worker', '--timeout', str(args.timeout), '--report', str(child_report)]
        if args.workspace:
            command += ['--workspace', args.workspace]
        job = _WindowsJob()
        process = None
        timed_out = False
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       # CREATE_SUSPENDED is a Win32 flag absent
                                       # from Python's public subprocess constants.
                                       creationflags=0x4 | subprocess.CREATE_NO_WINDOW)
            job.attach_and_resume(process)
            try:
                stdout, stderr = process.communicate(timeout=args.timeout + 10)
            except subprocess.TimeoutExpired:
                timed_out = True
                job.close()
                stdout, stderr = process.communicate(timeout=5)
            if child_report.is_file():
                report = json.loads(child_report.read_text(encoding='utf-8'))
            else:
                report = {'success': False, 'runtime': runtime_diagnostics(),
                          'error': 'WebView2 smoke did not produce a window report.'}
            report.update(process_exit_code=process.returncode, process_reaped=process.poll() is not None,
                          timed_out=timed_out, cleanup='Windows Job Object owns smoke descendants')
            if timed_out or process.returncode != 0:
                report['success'] = False
                report['error'] = ('WebView2 smoke exceeded its process deadline.' if timed_out
                                   else report.get('error') or stderr.decode('utf-8', 'replace')[-1800:])
            return (0 if report.get('success') else 1), report
        finally:
            job.close()
            if process is not None and process.poll() is None:
                # A failed Job assignment leaves only our suspended root. It
                # has not run or spawned descendants; terminate that known PID.
                process.kill()
                process.wait(timeout=5)
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='AgentBus WebView2 桌面')
    parser.add_argument('--workspace', help='明确选择项目目录')
    parser.add_argument('--diagnose', action='store_true', help='只报告 WebView2 运行环境')
    parser.add_argument('--smoke', action='store_true', help='加载真实 WebView2、检查 DOM/桥接并关闭')
    parser.add_argument('--report', type=Path, help='UTF-8 JSON 检查报告')
    parser.add_argument('--timeout', type=float, default=25, help='窗口检查秒数（1 至 60）')
    parser.add_argument('--_smoke-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 60:
        parser.error('--timeout must be between 1 and 60 seconds')
    report = {'format': 'agentbus.web-desktop-smoke' if args.smoke or args._smoke_worker
              else 'agentbus.web-desktop', 'schema_version': 1,
              'created_at': datetime.now(timezone.utc).isoformat(), 'success': False}
    try:
        if args.diagnose:
            report.update(runtime_diagnostics())
            report['success'] = report['webview2_installed'] and report['platform'] == 'win32'
            code = 0 if report['success'] else 1
        elif args.smoke:
            code, report = _bounded_smoke(args)
        else:
            code = _launch(select_workspace(args.workspace), smoke=args._smoke_worker,
                           timeout_s=args.timeout, report=report)
    except Exception as exc:
        report.update(success=False, error=str(exc))
        code = 1
        if not (args.smoke or args._smoke_worker or args.diagnose):
            show_startup_error(str(exc))
    _write_report(args.report, report)
    if args.smoke or args.diagnose:
        try:
            print(json.dumps(report, ensure_ascii=False))
        except UnicodeEncodeError:
            print(json.dumps(report, ensure_ascii=True))
    elif not report.get('success'):
        print(report.get('error', 'Desktop startup failed.'), file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
