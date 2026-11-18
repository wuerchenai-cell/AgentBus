#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read explicit build inputs without importing AgentBus or probing accounts.

Default: static AST + selected environment metadata only. Environment packages
are not a frozen SBOM. --run-smoke explicitly permits one entry.py --help call;
it never calls doctor. Source: Codex dependency audit repair, 2026-11-16.
"""
from __future__ import annotations

import argparse
import ast
import csv
from email.parser import Parser
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).absolute().parent.parent
OPTIONAL_ROOTS = ("openai_codex",)
OPTIONAL_TRANSITIVE = ("pydantic", "pydantic_core", "packaging")
BASE_ENTRY_POINTS = {name: "agentbus." + name for name in
                     ("cli", "web_desktop", "gui", "__main__", "bus", "task_tree", "web_bridge")}
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 100000
PRIVATE_COMPONENTS = {".workbuddy", "不要访问", "do not access"}


class AuditError(ValueError):
    """Missing, unsafe, malformed or changed explicit audit input."""


def _reject_private(path):
    if any(part.casefold() in PRIVATE_COMPONENTS
           for part in os.fspath(path).replace("\\", "/").split("/")):
        raise AuditError("Private or user-forbidden directories cannot be audited")


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def checked_path(path, *, directory=False):
    supplied = Path(path)
    _reject_private(supplied)
    result = Path(os.path.abspath(os.fspath(supplied)))
    _reject_private(result)
    for node in reversed((result, *result.parents)):
        try:
            info = node.lstat()
        except OSError as exc:
            raise AuditError("Missing or unreadable explicit audit input") from exc
        if _is_link(info):
            raise AuditError("Links and reparse points are not permitted audit inputs")
        expected_dir = node != result or directory
        if not (stat.S_ISDIR(info.st_mode) if expected_dir else stat.S_ISREG(info.st_mode)):
            raise AuditError("Audit input has the wrong file type")
    return result


def _read(path):
    path = checked_path(path)
    before = path.stat()
    if before.st_size > MAX_TEXT_BYTES:
        raise AuditError("Text audit input exceeds size limit")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise AuditError("Audit input changed before opening")
        data = stream.read(MAX_TEXT_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = checked_path(path).stat()
    if (len(data) > MAX_TEXT_BYTES or after.st_size != len(data)
            or before.st_size != len(data) or before.st_mtime_ns != after.st_mtime_ns
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
        raise AuditError("Audit input changed during reading")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AuditError("Text audit input must be UTF-8") from exc


def _files(directory):
    """Bounded no-link walk, excluding private names before inspecting metadata."""
    directory = checked_path(directory, directory=True)
    stack = [directory]
    visited = 0
    while stack:
        current = stack.pop()
        checked_path(current, directory=True)
        with os.scandir(current) as entries:
            names = sorted(entries, key=lambda x: x.name)
        for entry in names:
            visited += 1
            if visited > MAX_ENTRIES:
                raise AuditError("Audit directory exceeds entry limit")
            if (entry.name.casefold() in PRIVATE_COMPONENTS or entry.name.startswith(".")
                    or entry.name == "__pycache__"):
                continue
            # Windows DirEntry.stat can report zero file IDs for distinct files.
            # Full-path stat supplies the identity needed for hardlink counting.
            info = os.stat(entry.path, follow_symlinks=False)
            if _is_link(info):
                raise AuditError("Links and reparse points found in audit tree")
            path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                yield path, info


def _package_files(root):
    return sorted(path for path, _ in _files(root) if path.suffix == ".py")


def _dir_size(path):
    seen = set()
    total = 0
    for _, info in _files(path):
        identity = (info.st_dev, info.st_ino)
        if identity not in seen:
            seen.add(identity)
            total += info.st_size
    return total


def _module_imports(path, module=None, *, is_package=False, known_modules=()):
    try:
        tree = ast.parse(_read(path), filename=str(path))
    except SyntaxError as exc:
        raise AuditError(f"Source syntax error at line {exc.lineno}") from exc
    module = module or "agentbus." + Path(path).stem
    package = module if is_package else module.rpartition(".")[0]
    found = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                components = package.split(".")
                if node.level > len(components):
                    raise AuditError("Relative import escapes package")
                prefix = components[:len(components) - node.level + 1]
                base = ".".join(prefix + ([base] if base else []))
            names.append(base)
            names.extend(base + "." + alias.name for alias in node.names if alias.name != "*")
        for name in names:
            if name in known_modules or name.split(".")[0] in OPTIONAL_ROOTS:
                found.add(name.split(".")[0] if name.split(".")[0] in OPTIONAL_ROOTS else name)
    return found


def import_graph(source_root=None):
    root = checked_path(source_root if source_root is not None else ROOT, directory=True)
    package = root / "agentbus"
    modules = {}
    for path in _package_files(package):
        relative = path.relative_to(root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    graph = {}
    for module, path in modules.items():
        imports = _module_imports(path, module, is_package=path.name == "__init__.py",
                                  known_modules=modules)
        parent = module.rpartition(".")[0]
        while parent:
            if parent in modules:
                imports.add(parent)
            parent = parent.rpartition(".")[0]
        graph[module] = sorted(imports)
    return graph


def optional_reachable_from(graph, entry):
    seen, stack = set(), [entry]
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return seen


def optional_users(graph):
    direct = {module for module, deps in graph.items() if "openai_codex" in deps}
    users = {module for module in graph if "openai_codex" in optional_reachable_from(graph, module)}
    return {"direct": sorted(direct), "transitive": sorted(users - direct), "all": sorted(users)}


def entry_point_exposure(graph):
    users = set(optional_users(graph)["all"])
    result = {}
    for name, module in BASE_ENTRY_POINTS.items():
        hits = sorted(optional_reachable_from(graph, module) & users)
        result[name] = {"module": module, "optional_modules": hits,
                        "static_possible_exposure": bool(hits), "entry_present": module in graph}
    return result


def requirements_pins(source_root=None):
    root = checked_path(source_root if source_root is not None else ROOT, directory=True)
    pins = []
    for line in _read(root / "requirements-build.txt").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([^\s;]+)", line.strip())
        if match:
            pins.append({"name": match.group(1), "version": match.group(2)})
    return pins


def _site(venv):
    if venv is None:
        return None
    root = checked_path(venv, directory=True)
    # Explicit Windows build target; never search ancestors or the user's home.
    return checked_path(root / "Lib/site-packages", directory=True)


def _distributions(site):
    if site is None:
        return []
    result = []
    with os.scandir(site) as entries:
        names = sorted(entries, key=lambda x: x.name)
    if len(names) > MAX_ENTRIES:
        raise AuditError("Too many site-package entries")
    for entry in names:
        if not entry.name.endswith(".dist-info"):
            continue
        directory = checked_path(entry.path, directory=True)
        metadata = Parser().parsestr(_read(directory / "METADATA"))
        result.append((directory, metadata))
    return result


def site_packages_measurements(venv=None):
    site = _site(venv)
    if site is None:
        return []
    targets = (*OPTIONAL_ROOTS, *OPTIONAL_TRANSITIVE)
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    metadata = {normalize(item.get("Name", "")): item for _, item in _distributions(site)}
    rows = []
    for name in targets:
        path = site / name
        if not os.path.lexists(path):
            continue
        checked_path(path, directory=True)
        item = metadata.get(normalize(name))
        rows.append({"package": name, "bytes": _dir_size(path),
                     "version": item.get("Version") if item else None,
                     "license": (item.get("License-Expression") or item.get("License")) if item else None,
                     "classification": "build_environment_package_not_frozen_proof"})
    return rows


def cli_bin_measurement(venv=None):
    site = _site(venv)
    if site is None:
        return {"available": False}
    selected = [(directory, item) for directory, item in _distributions(site)
                if re.sub(r"[-_.]+", "-", item.get("Name", "")).lower() == "openai-codex-cli-bin"]
    if not selected:
        return {"available": False}
    if len(selected) != 1:
        raise AuditError("Ambiguous Codex runtime distributions")
    directory, item = selected[0]
    largest, identities, total = [], set(), 0
    for row in csv.reader(io.StringIO(_read(directory / "RECORD"))):
        if not row:
            continue
        relative = row[0]
        if (not relative or relative.startswith(("/", "\\")) or ":" in relative
                or "\\" in relative or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise AuditError("Wheel RECORD path escapes explicit site-packages")
        path = checked_path(site / relative)
        if not path.is_relative_to(site):
            raise AuditError("Wheel RECORD path escapes explicit site-packages")
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        if identity not in identities:
            identities.add(identity)
            total += info.st_size
            largest.append({"path": relative, "bytes": info.st_size})
    return {"available": True, "version": item.get("Version"), "entries": len(largest),
            "bytes": total, "largest": sorted(largest, key=lambda x: (-x["bytes"], x["path"]))[:5],
            "classification": "build_environment_files_not_frozen_proof"}


def blocked_import_smoke(source_root=None):
    """Legacy API retained as explicit --help smoke; no account doctor/probes."""
    root = checked_path(source_root if source_root is not None else ROOT, directory=True)
    entry = checked_path(root / "entry.py")
    start = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", str(entry), "--help"],
                              cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "kind": "explicit_cli_help", "reason": type(exc).__name__}
    return {"status": "passed" if proc.returncode == 0 else "failed", "kind": "explicit_cli_help",
            "exit_code": proc.returncode, "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            "output_captured_not_exported": True}


def audit(source_root=None, *, venv=None, executable=None, run_smoke=False):
    root = checked_path(source_root if source_root is not None else ROOT, directory=True)
    # Reject private strings in all caller-supplied inputs before metadata reads.
    for path in (venv, executable):
        if path is not None:
            _reject_private(path)
    site = _site(venv)
    exe = checked_path(executable) if executable is not None else None
    graph = import_graph(root)
    return {"schema_version": 2, "read_only": True, "source_root": str(root),
            "is_frozen_sbom": False, "runtime_imports_observed": False,
            "static_analysis_limit": "Includes imports in optional function bodies; possible reachability is not runtime execution",
            "requirements": {"file": "requirements-build.txt", "pins": requirements_pins(root)},
            "import_graph": {"modules": len(graph), "optional_users": optional_users(graph),
                             "entry_point_exposure": entry_point_exposure(graph)},
            "sizes": {"venv_measured": str(site) if site else None, "venv_candidates": [],
                      "site_packages": site_packages_measurements(venv), "cli_bin": cli_bin_measurement(venv),
                      "formal_exe_bytes": exe.stat().st_size if exe else None,
                      "byte_measurement": "logical file lengths; hardlinks deduplicated within each package, not NTFS allocation"},
            "smoke": blocked_import_smoke(root) if run_smoke else {"status": "not_run", "reason": "requires --run-smoke"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--venv", type=Path, help="Explicit Windows build venv; optional, never auto-discovered")
    parser.add_argument("--exe", type=Path, help="Explicit formal executable; metadata only, never launched")
    parser.add_argument("--run-smoke", action="store_true", help="Explicitly run source entry.py --help once, never doctor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit(args.source_root, venv=args.venv, executable=args.exe, run_smoke=args.run_smoke)
    except (AuditError, OSError) as exc:
        print(f"[audit refused] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Static dependency audit; environment metadata is not a frozen SBOM.")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["smoke"]["status"] == "failed" else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
