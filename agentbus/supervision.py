"""Strict, local supervision primitives for AgentBus task delivery.

This module verifies submitted artifacts and executes only check commands
already stored in owner-authored project configuration. It does not read agent
messages for commands, contact external services, or decide whether the work is
semantically correct. Placeholder scanning is a warning aid, never a quality
certificate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

from .workspace_files import MAX_ARTIFACT_BYTES, WorkspaceFileError, WorkspaceFiles

MAX_CHECKS = 16
MAX_ARGV_ITEMS = 64
MAX_ARG_LENGTH = 4096
MAX_ARGV_BYTES = 16384
MAX_CAPTURE_BYTES = 65536
MAX_MANIFEST_ITEMS = 128
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".rst", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".csv", ".log",
    ".sh", ".ps1", ".bat", ".cmd", ".sql", ".ipynb", ".make", ".mk",
})
_PLACEHOLDERS = (
    ("QUESTION_MARKS", re.compile(r"\?\?\?")),
    ("TODO", re.compile(r"\bTODO\b", re.IGNORECASE)),
    ("FIXME", re.compile(r"\bFIXME\b", re.IGNORECASE)),
    ("NOT_IMPLEMENTED", re.compile(r"\bNotImplemented(?:Error)?\b", re.IGNORECASE)),
)
_CHECK_ENV_ALLOWLIST = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT",
})

_FORBIDDEN_COMMANDS = frozenset({
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "csh", "tcsh", "ksh",
    "del", "erase", "rm", "rmdir", "shutdown", "curl", "wget", "format",
    "diskpart", "taskkill", "kill", "reg", "regedit",
})

_ALLOWED_COMMAND_BASENAMES = frozenset({
    "python", "python3", "py", "pytest", "pyinstaller",
    "node", "npm", "npx", "deno", "bun", "cargo", "go",
})


def _validate_check_program(program: str) -> None:
    if not isinstance(program, str) or not program.strip():
        _fail("check program cannot be empty")
    clean = program.strip().replace("\\", "/").rstrip("/")
    basename = clean.split("/")[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".sh", ".bin"):
        if basename.endswith(ext):
            basename = basename[:-len(ext)]
            break
    if basename in _FORBIDDEN_COMMANDS:
        _fail(f"forbidden check program: '{program}' is not permitted in strict supervision")
    if basename not in _ALLOWED_COMMAND_BASENAMES and not clean.endswith((".py", ".js", ".ts")):
        _fail(f"unauthorized check program: '{program}' is not in the allowed test runner allowlist")


class SupervisionError(ValueError):
    """Invalid supervision policy or unsafe verification input."""


def _fail(message: str) -> None:
    raise SupervisionError(message)


# CHECKS-1/2 (OpenCode adversarial audit, 2026-10-11): the argv[0] program name
# had no allowlist at all. Any executable could be configured as a "check",
# including `cmd /c del`, and it both ran and reported ok=True, so a check could
# destroy the very artifact it was supposed to verify. The allowlist below
# closes that. Absolute interpreter paths stay allowed so existing owner
# configuration keeps working.
_SHELL_INTERPRETERS = frozenset({
    "cmd", "cmd.exe", "command.com", "powershell", "powershell.exe",
    "pwsh", "pwsh.exe", "bash", "bash.exe", "sh", "sh.exe",
    "dash", "zsh", "ksh", "csh", "tcsh", "ash",
    "wscript", "wscript.exe", "cscript", "cscript.exe", "mshta", "mshta.exe",
    "rundll32", "rundll32.exe",
})

_DESTRUCTIVE_PRIMITIVES = frozenset({
    "del", "erase", "rm", "rmdir", "rd", "unlink", "shred", "srm",
    "shutdown", "restart", "reboot", "halt", "poweroff", "logoff",
    "kill", "killall", "pkill", "taskkill", "taskend", "tskill",
    "format", "fdisk", "mkfs", "mklabel", "sfdisk",
    "dd", "chmod", "chown", "chgrp", "attrib",
    "cacls", "icacls", "takeown",
    "reg", "regedit", "regedit.exe",
    "sc", "sc.exe", "net", "net1", "net.exe", "netsh", "wmic",
    "schtasks", "at", "bcdedit", "diskpart", "vssadmin", "wbadmin",
    "cipher", "compact", "recover", "subst", "mountvol",
    "curl", "curl.exe", "wget", "wget.exe", "certutil", "certutil.exe",
    "bitsadmin", "npx", "npx.cmd", "npm", "npm.cmd", "yarn", "yarn.cmd",
    "pnpm", "pnpm.cmd", "uv", "uv.exe", "uvx", "pip", "pip3",
    "pipx", "poetry", "conda", "gem", "bundle",
})

_ALLOWED_RUNNERS = frozenset({
    "python", "python3", "pythonw", "py", "pytest", "py.test",
    "node", "node.exe", "deno", "bun",
    "go", "go.exe", "cargo", "rustc", "java", "javac",
    "make", "gmake", "mingw32-make", "cmake", "ninja",
    "msbuild", "msbuild.exe", "dotnet", "dotnet.exe",
})

# 允许的绝对路径解析后，文件名（去掉 .exe）也必须落在 _ALLOWED_RUNNERS 内，
# 否则 C:\Windows\System32\cmd.exe 这种绝对路径仍可逃逸。
_RUNNER_SUFFIXES = ("", ".exe", ".com", ".bat", ".cmd")

# 项目内的测试脚本扩展名。只有项目相对路径可以走这一支；
# 绝对路径一律只认 _ALLOWED_RUNNERS，防止指向项目外的任意脚本。
# .ps1 / .bat / .cmd 刻意不在内：它们就是 shell，放进白名单等于重新开门。
_ALLOWED_SCRIPT_SUFFIXES = frozenset({".py", ".pyi", ".sh"})


def _runner_stem(value: str) -> str:
    lowered = str(value).strip().strip('"').strip("'").lower()
    for suffix in _RUNNER_SUFFIXES:
        if lowered.endswith(suffix) and suffix:
            lowered = lowered[: -len(suffix)]
            break
    return lowered


def _validated_program(argv: list[str], index: int) -> str:
    """Validate argv[0] against a closed allowlist.

    Returns the program exactly as configured, unchanged, so an absolute
    interpreter path and a relative workspace test script both keep working.

    Three shapes are accepted:
      1. a bare interpreter/toolkit name from _ALLOWED_RUNNERS;
      2. an absolute path whose basename is in _ALLOWED_RUNNERS;
      3. a project-relative path ending in an allowed test-script extension.
    """
    program = argv[0]
    raw = str(program).strip().strip('"').strip("'")
    if not raw:
        _fail(f"check {index + 1} argv[0] must not be empty")
    normalized = raw.replace("\\", "/")
    is_absolute = str(program)[:1] in ("/", "\\") or (
        len(str(program)) > 1 and str(program)[1] == ":")
    has_separator = "/" in normalized

    if not is_absolute and not has_separator:
        # Bare name. Interpreters and toolkits pass; shells and destructive
        # primitives are rejected by name.
        if normalized in _SHELL_INTERPRETERS:
            _fail(f"check {index + 1} argv[0] {normalized!r} is a shell interpreter and is forbidden")
        if normalized in _DESTRUCTIVE_PRIMITIVES:
            _fail(f"check {index + 1} argv[0] {normalized!r} is a destructive primitive and is forbidden")
        if _runner_stem(normalized) not in _ALLOWED_RUNNERS:
            # A single-segment project script such as `run` or `run.py` is a
            # controlled in-project invocation and stays allowed.
            if Path(normalized).suffix.lower() in _ALLOWED_SCRIPT_SUFFIXES or "." not in normalized:
                return program
            _fail(f"check {index + 1} argv[0] {normalized!r} is not an allowed test runner")
        return program

    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        _fail(f"check {index + 1} argv[0] may not traverse outside the project")
    basename = parts[-1] if parts else ""
    stem = _runner_stem(basename)
    if stem in _ALLOWED_RUNNERS:
        return program
    # A project-relative path to a test script is allowed only when it is not
    # absolute, because an absolute path could point at anything on the host.
    if not is_absolute and Path(basename).suffix.lower() in _ALLOWED_SCRIPT_SUFFIXES:
        return program
    _fail(f"check {index + 1} argv[0] {basename!r} is not an allowed test runner "
          f"or project-relative test script")
    return program


def _validated_checks(checks: Any, *, required: bool) -> list[dict]:
    if not isinstance(checks, list):
        _fail("checks must be a list of owner-configured argv records")
    if required and not checks:
        _fail("strict supervision requires at least one owner-configured check")
    if len(checks) > MAX_CHECKS:
        _fail(f"checks cannot exceed {MAX_CHECKS} entries")
    result = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or set(check) - {"argv", "timeout_seconds", "name"}:
            _fail(f"check {index + 1} must contain only argv, timeout_seconds and optional name")
        argv = check.get("argv")
        if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV_ITEMS:
            _fail(f"check {index + 1} argv must be a nonempty list of at most {MAX_ARGV_ITEMS} strings")
        if any(not isinstance(arg, str) or not arg.strip() or len(arg) > MAX_ARG_LENGTH
               or "\x00" in arg or any(ord(char) < 32 for char in arg)
               for arg in argv):
            _fail(f"check {index + 1} argv contains an empty, oversized or control-character argument")
        if sum(len(arg.encode("utf-8")) for arg in argv) > MAX_ARGV_BYTES:
            _fail(f"check {index + 1} argv exceeds the {MAX_ARGV_BYTES}-byte limit")
        # CHECKS-1/2: validate the program before it can ever be executed.
        program = _validated_program(argv, index)
        timeout = check.get("timeout_seconds")
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            _fail(f"check {index + 1} timeout_seconds must be an integer from 1 to 1800")
        name = check.get("name", f"check-{index + 1}")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            _fail(f"check {index + 1} name must be a nonempty string of at most 120 characters")
        result.append({"name": name.strip(), "argv": [program, *argv[1:]],
                       "timeout_seconds": timeout})
    return result

def validate_settings(supervision: dict, workers: dict) -> dict:
    """Validate strict-audit settings against the enabled project members.

    The required keys are ``strict_supervision``, ``designated_auditor`` and
    ``checks``. A strict auditor must be enabled and permitted to execute work.
    """
    if not isinstance(supervision, dict):
        _fail("supervision settings must be an object")
    if not isinstance(workers, dict):
        _fail("workers must be an object keyed by member id")
    strict = supervision.get("strict_supervision")
    if type(strict) is not bool:
        _fail("strict_supervision must be a boolean")

    auditor = supervision.get("designated_auditor", "")
    if not isinstance(auditor, str) or len(auditor) > 64:
        _fail("designated_auditor must be a member id string")
    auditor = auditor.strip().lower()
    checks = _validated_checks(supervision.get("checks", []), required=strict)

    if strict:
        if not auditor:
            _fail("strict supervision requires a designated auditor")
        member = next((value for key, value in workers.items()
                       if isinstance(key, str) and key.strip().lower() == auditor), None)
        if not isinstance(member, dict):
            _fail("designated auditor is not a configured worker")
        if member.get("enabled") is not True:
            _fail("designated auditor must be enabled")
        if member.get("executor_enabled") is not True:
            _fail("designated auditor must have executor permission")

    return {
        "strict_supervision": strict,
        "designated_auditor": auditor,
        "checks": checks,
    }


def _manifest_shape(artifacts: Any) -> list[dict]:
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_MANIFEST_ITEMS:
        _fail(f"artifacts must be a list containing 1 to {MAX_MANIFEST_ITEMS} entries")
    result = []
    for index, entry in enumerate(artifacts):
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            _fail(f"artifact {index + 1} must contain exactly path, bytes and sha256")
        result.append(dict(entry))
    return result


def _read_verified_bytes(files: WorkspaceFiles, rel: str) -> tuple[str, bytes]:
    """Read through the same no-link/path-snapshot checks as WorkspaceFiles."""
    canonical_rel, target, snapshot = files._path(rel)
    return canonical_rel, files._raw(target, MAX_ARTIFACT_BYTES, snapshot=snapshot)


def verify_artifacts(output_root: str | Path, artifacts: list[dict]) -> dict:
    """Independently check each artifact's path, size and SHA256 on disk.

    A successful byte check says only that the declared bytes are present. It
    does not establish that the artifact is correct or complete.
    """
    entries = _manifest_shape(artifacts)
    try:
        files = WorkspaceFiles(output_root)
        verification = files.verify_artifacts(entries)
    except (OSError, WorkspaceFileError, ValueError) as exc:
        return {"verdict": "fail", "artifacts": [],
                "issues": [{"code": getattr(exc, "code", "INVALID_ROOT"),
                            "path": "", "reason": str(exc)}]}

    if verification.get("verdict") != "match":
        issues = []
        for mismatch in verification.get("mismatches", []):
            issues.append({"code": mismatch.get("code", "CONTENT_MISMATCH"),
                            "path": mismatch.get("path", ""),
                            "reason": mismatch.get("reason", "declared artifact bytes do not match disk")})
        return {"verdict": "fail", "artifacts": [], "issues": issues}

    checked = []
    issues = []
    for entry in entries:
        rel = entry["path"]
        try:
            canonical_rel, raw = _read_verified_bytes(files, rel)
        except (OSError, WorkspaceFileError, SupervisionError) as exc:
            issues.append({"code": getattr(exc, "code", "UNREADABLE"),
                           "path": rel if isinstance(rel, str) else "",
                           "reason": str(exc)})
            continue
        text_candidate = Path(canonical_rel).suffix.lower() in TEXT_SUFFIXES
        content_kind = "binary"
        if text_candidate:
            try:
                raw.decode("utf-8-sig", errors="strict")
                content_kind = "text"
            except UnicodeDecodeError:
                # Binary or invalidly encoded data is classified, not decoded
                # with replacement and then treated as a valid text artifact.
                content_kind = "binary"
        checked.append({"path": canonical_rel, "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "content_kind": content_kind, "verified": True})

    return {"verdict": "pass" if not issues else "fail",
            "artifacts": checked, "issues": issues}


def scan_text_artifacts(output_root: str | Path, artifacts: list[dict]) -> list[dict]:
    """Report obvious text placeholders; findings are heuristic, not proof."""
    verification = verify_artifacts(output_root, artifacts)
    if verification["verdict"] != "pass":
        return [{"code": "ARTIFACT_VERIFICATION_FAILED",
                 "path": issue.get("path", ""), "line": 0,
                 "detail": issue.get("reason", issue.get("code", "verification failed")),
                 "is_quality_proof": False}
                for issue in verification["issues"]]

    files = WorkspaceFiles(output_root)
    findings = []
    for artifact in verification["artifacts"]:
        if artifact["content_kind"] != "text":
            continue
        rel = artifact["path"]
        try:
            _canonical_rel, raw = _read_verified_bytes(files, rel)
            text = raw.decode("utf-8-sig", errors="strict")
        except (OSError, WorkspaceFileError, UnicodeDecodeError, SupervisionError) as exc:
            findings.append({"code": "SCAN_UNAVAILABLE", "path": rel, "line": 0,
                             "detail": type(exc).__name__, "is_quality_proof": False})
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for code, pattern in _PLACEHOLDERS:
                if pattern.search(line):
                    findings.append({"code": code, "path": rel, "line": line_number,
                                     "detail": "possible placeholder; human review required",
                                     "is_quality_proof": False})
    return findings


def _capture_pipe(pipe, sink: bytearray, truncated: list[bool]) -> None:
    while True:
        chunk = pipe.read(8192)
        if not chunk:
            return
        available = MAX_CAPTURE_BYTES - len(sink)
        if available > 0:
            sink.extend(chunk[:available])
        if len(chunk) > max(available, 0):
            truncated[0] = True


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # Fixed operating-system command; task/result content is never passed
        # here and shell parsing remains disabled.
        try:
            subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, shell=False, timeout=10,
                           check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
    else:
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()


def _check_environment() -> dict[str, str]:
    """Return only the OS variables needed to launch bounded local checks.

    In particular, checks do not inherit API keys, access tokens, cloud
    credentials, or arbitrary user environment variables from the agent host.
    """
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in _CHECK_ENV_ALLOWLIST and isinstance(value, str)
    }
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return env


def run_allowlisted_checks(workspace: str | Path, checks: list[dict]) -> list[dict]:
    """Run only previously validated owner-configured argv commands.

    The caller must pass settings returned by ``validate_settings`` or the same
    persisted owner configuration. No task, receipt or agent text is parsed as
    a command. Each invocation has a fixed working directory, no stdin, no
    shell, bounded duration and bounded retained output.
    """
    normalized = _validated_checks(checks, required=True)
    try:
        root = WorkspaceFiles(workspace).workspace
        if not root.is_dir():
            _fail("workspace must be an existing directory")
    except (OSError, WorkspaceFileError) as exc:
        _fail(f"workspace cannot be inspected ({type(exc).__name__})")

    results = []
    for check in normalized:
        out, err = bytearray(), bytearray()
        out_truncated, err_truncated = [False], [False]
        started = time.monotonic()
        timed_out = False
        error = ""
        proc = None
        try:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                check["argv"], cwd=str(root), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                env=_check_environment(),
                creationflags=flags, start_new_session=(os.name != "nt"),
            )
            assert proc.stdout is not None and proc.stderr is not None
            readers = [
                threading.Thread(target=_capture_pipe, args=(proc.stdout, out, out_truncated), daemon=True),
                threading.Thread(target=_capture_pipe, args=(proc.stderr, err, err_truncated), daemon=True),
            ]
            for reader in readers:
                reader.start()
            try:
                proc.wait(timeout=check["timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
        except (OSError, ValueError) as exc:
            error = type(exc).__name__
            if proc is not None and proc.poll() is None:
                _kill_process_tree(proc)
                proc.wait(timeout=5)
        finally:
            if proc is not None:
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()

        exit_code = None if timed_out or error or proc is None else proc.returncode
        results.append({
            "name": check["name"], "argv": list(check["argv"]),
            "exit_code": exit_code, "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": err.decode("utf-8", errors="replace"),
            "stdout_truncated": out_truncated[0],
            "stderr_truncated": err_truncated[0],
            "error": error,
            "ok": exit_code == 0 and not timed_out and not error,
        })
    return results


def assess_check_for_audit(check: dict, result: dict) -> dict:
    """Decide whether a successful command is an auditable test run.

    A zero exit status alone is not acceptance evidence: inline snippets such
    as ``python -c "print('ok')"`` are opaque no-ops, and test discovery that
    ran zero tests must not satisfy strict supervision. This deliberately
    accepts only recognizable test-runner invocations with a nonzero test
    count. Other allowed commands may still run, but remain informational and
    cannot make a supervised result reviewable.
    """
    argv = check.get("argv", []) if isinstance(check, dict) else []
    if not isinstance(argv, list) or not argv:
        return {"audit_qualifying": False, "runner": "unknown",
                "tests_run": None, "qualification_reason": "missing command"}

    program = Path(str(argv[0]).replace("\\", "/")).name.lower()
    if program.endswith(".exe"):
        program = program[:-4]
    args = [str(value).strip().lower() for value in argv[1:]]
    output = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
    exit_code = result.get("exit_code")
    runner = "unknown"
    count = None

    if program in {"python", "python3", "pythonw", "py"}:
        if "-c" in args:
            runner = "python -c"
            return {"audit_qualifying": False, "runner": runner,
                    "tests_run": None,
                    "qualification_reason": "inline code is not an auditable test suite"}
        module_index = args.index("-m") if "-m" in args else -1
        module = args[module_index + 1] if module_index >= 0 and module_index + 1 < len(args) else ""
        if module == "unittest":
            runner = "python -m unittest"
            match = re.search(r"\bRan\s+(\d+)\s+tests?\b", output, re.IGNORECASE)
            total = int(match.group(1)) if match else None
            skipped_match = re.search(r"\bskipped\s*=\s*(\d+)\b", output,
                                      re.IGNORECASE)
            skipped = int(skipped_match.group(1)) if skipped_match else 0
            count = max(0, total - skipped) if total is not None else None
        elif module == "pytest":
            runner = "python -m pytest"
            match = re.search(r"\b(\d+)\s+passed\b", output, re.IGNORECASE)
            skipped_match = re.search(r"\b\d+\s+skipped\b", output,
                                      re.IGNORECASE)
            count = int(match.group(1)) if match else 0 if skipped_match else None
        else:
            return {"audit_qualifying": False, "runner": "python (not a test module)",
                    "tests_run": None,
                    "qualification_reason": "command is not a recognized test runner"}
    elif program in {"pytest", "py.test"}:
        runner = program
        match = re.search(r"\b(\d+)\s+passed\b", output, re.IGNORECASE)
        skipped_match = re.search(r"\b\d+\s+skipped\b", output,
                                  re.IGNORECASE)
        count = int(match.group(1)) if match else 0 if skipped_match else None
    else:
        return {"audit_qualifying": False, "runner": program,
                "tests_run": None,
                "qualification_reason": "command is not a recognized test runner"}

    qualifying = (exit_code == 0 and result.get("ok") is True
                  and result.get("timed_out") is not True
                  and not result.get("error")
                  and not result.get("stdout_truncated")
                  and not result.get("stderr_truncated")
                  and isinstance(count, int) and count > 0)
    if qualifying:
        reason = "recognized test runner completed at least one test"
    elif exit_code != 0:
        reason = "test runner returned a nonzero exit code"
    elif count == 0:
        reason = "test runner executed zero non-skipped tests"
    elif count is None:
        reason = "test count could not be confirmed from runner output"
    else:
        reason = "test runner output was incomplete or did not pass"
    return {"audit_qualifying": qualifying, "runner": runner,
            "tests_run": count, "qualification_reason": reason}
