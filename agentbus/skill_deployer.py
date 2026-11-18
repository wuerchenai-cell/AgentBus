# -*- coding: utf-8 -*-
"""技能事务部署器：预览门禁 → 事务写入 → 失败回滚 → 用户改动保护 → 多宿主隔离。

来源：WorkBuddy，2026-10-08；任务 m-1791457584489-f05b6b。
复用（不重写）：skill_packages.inspect_package（只读清单）、skill_preview.build_sync_preview（只读门禁与
动作判定）、skill_install.{safe_install_path, installation_guard, transactional_write, _atomic_write,
_sha256}（路径安全/跨进程锁/只在失败时撤销自己写入）、skill_evidence.evaluate_evidence（发现/调用证据）。

关键语义（写死）：
- 预览永远不授予写权（``build_sync_preview`` 的 ``write_permitted`` 恒 False）；真实写入必须由调用方
  显式 ``confirm=True`` 触发，两者不可互相推导。
- 记录字段 ``before_content_digest`` 存"我们装上去的内容摘要"；目标被用户改动后摘要不符 →
  预览判 conflict(TARGET_MODIFIED)，部署拒绝写入，不静默覆盖。
- 回滚只撤销**本记录登记的托管文件**：本轮写入后又被用户改动的文件跳过并如实上报，绝不覆盖用户改动。
- 部署事实只证明"文件已按摘要写入"；原生发现与实际调用是独立证据，初值一律 unverified。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import host_skill_adapters as adapters
from .skill_evidence import evaluate_evidence
from .skill_install import (_atomic_write, _sha256, installation_guard,
                            safe_install_path, transactional_write)
from .skill_packages import SkillPackageError, inspect_package
from .skill_preview import ACTION_CREATE, ACTION_UNCHANGED, ACTION_UPDATE
from .skill_preview import build_sync_preview

SCHEMA_VERSION = 1
OWNER = "agentbus"
STATE_DIR_NAME = "skill-center"
RECORD_DIR = "records"
BACKUP_DIR = "backups"
WRITABLE_ACTIONS = (ACTION_CREATE, ACTION_UPDATE)
DEPLOYABLE_ROUTES = ("desktop", "cli", "independent")


class SkillDeployError(RuntimeError):
    """部署输入无效或事务不可继续。"""


def _read_package(path: Path) -> dict | None:
    """只读清单；缺 SKILL.md、损坏或不可读时返回 None（交给门禁判 conflict，不崩溃）。"""
    if not path.is_dir():
        return None
    try:
        return inspect_package(path.parent, path.name)
    except (SkillPackageError, OSError, ValueError):
        return None


def _source_package(source_dir: Path) -> dict:
    package = _read_package(source_dir)
    if package is None:
        raise SkillDeployError(f"INVALID_SOURCE: 源不是可读技能包（需根 SKILL.md）：{source_dir}")
    return package


def record_path(state_root: Path, *, name: str, host: str, scope: str, route: str) -> Path:
    key = _sha256(f"{name}\0{host}\0{scope}\0{route}".encode("utf-8"))[:16]
    return state_root / RECORD_DIR / f"{key}.json"


def _backup_dir(state_root: Path, *, name: str, host: str, scope: str) -> Path:
    return state_root / BACKUP_DIR / f"{name}--{host}--{scope}"


def _load_record(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise SkillDeployError('RECORD_TOO_LARGE: 技能记录超过有界大小')
        with path.open('rb') as stream:
            content = stream.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise SkillDeployError('RECORD_TOO_LARGE: 技能记录在读取期间增长')
        value = json.loads(content.decode('utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _record_for(record: dict | None, *, host: str, scope: str, route: str) -> dict | None:
    if not isinstance(record, dict):
        return None
    if (record.get("owner") == OWNER and record.get("host") == host
            and record.get("scope") == scope and record.get("route") == route):
        return record
    return None


def plan(host: object, *, source_dir: Path | str, project_root: Path | str = None,
         global_root: Path | str = None, scope: str = adapters.SCOPE_PROJECT,
         route: str = "desktop", state_root: Path | str = None,
         active_task_ids=()) -> dict:
    """只读计划：给出目标目录、动作、原因与三态，不写任何文件。"""
    if route not in DEPLOYABLE_ROUTES:
        raise ValueError(f"INVALID_ROUTE: route 必须是 {DEPLOYABLE_ROUTES} 之一")
    target = adapters.resolve_target(host, skill_name=Path(str(source_dir)).name,
                                     scope=scope, project_root=project_root,
                                     global_root=global_root)
    source = _source_package(Path(source_dir))
    state = Path(state_root) if state_root else Path(target["root"]) / ".agentbus" / STATE_DIR_NAME
    record_file = record_path(state, name=target["skill_name"], host=target["host"],
                              scope=scope, route=route)
    safe_install_path(state, record_file)
    record = _record_for(_load_record(record_file), host=target["host"], scope=scope, route=route)
    target_pkg = _read_package(Path(target["target_dir"]))
    preview = build_sync_preview(source, target_pkg, record, host=target["host"],
                                 scope=scope, route=route, active_task_ids=active_task_ids)
    invalid_target = (Path(target["target_dir"]).is_dir() and target_pkg is None)
    if invalid_target and preview["action"] != "conflict":
        preview = dict(preview, action="conflict",
                       reason_codes=sorted(set(preview["reason_codes"]) | {"TARGET_UNREADABLE"}))
    return {"schema_version": SCHEMA_VERSION, "host": target["host"], "scope": scope,
            "route": route, "skill_name": target["skill_name"],
            "source_dir": str(Path(source_dir)), "source_digest": source["content_digest"],
            "source_files": len(source["files"]),
            "target_dir": target["target_dir"], "target_exists": target["exists"],
            "target_digest": (target_pkg or {}).get("content_digest"),
            "action": preview["action"], "reason_codes": list(preview["reason_codes"]),
            "used_generic": target["used_generic"], "adapter_notes": target["reason_codes"],
            "record_path": str(record_file), "state_root": str(state),
            "preview": preview, "write_permitted": False, "delete_permitted": False,
            "native_discovery_claimed": False, "private_probe": False}


def _file_outputs(source_dir: Path, target_root: Path, package: dict) -> dict[Path, bytes]:
    outputs: dict[Path, bytes] = {}
    for entry in package["files"]:
        relative = Path(entry["path"])
        source_file = source_dir / relative
        data = source_file.read_bytes()
        if _sha256(data) != entry["sha256"] or len(data) != entry["bytes"]:
            raise SkillDeployError(f"SNAPSHOT_CHANGED: 源文件在计划后变化：{entry['path']}")
        destination = target_root / relative
        outputs[safe_install_path(target_root, destination)] = data
    if not outputs:
        raise SkillDeployError("INVALID_SOURCE: 源技能包为空")
    return outputs


def deploy(host: object, *, source_dir: Path | str, confirm: bool = False,
           project_root: Path | str = None, global_root: Path | str = None,
           scope: str = adapters.SCOPE_PROJECT, route: str = "desktop",
           state_root: Path | str = None, active_task_ids=(),
           deployed_at: float | None = None) -> dict:
    """执行一次事务部署；未显式 confirm 一律不写。"""
    planned = plan(host, source_dir=source_dir, project_root=project_root,
                   global_root=global_root, scope=scope, route=route,
                   state_root=state_root, active_task_ids=active_task_ids)
    if not confirm:
        return {"schema_version": SCHEMA_VERSION, "deployed": False,
                "action": planned["action"], "reason_codes": ["CONFIRMATION_REQUIRED"],
                "plan": planned, "native_discovery_claimed": False}
    if planned["action"] not in WRITABLE_ACTIONS:
        return {"schema_version": SCHEMA_VERSION, "deployed": False,
                "action": planned["action"],
                "reason_codes": sorted(set(planned["reason_codes"]) | {"NO_WRITE_FOR_ACTION"}),
                "plan": planned, "native_discovery_claimed": False}

    source_dir = Path(planned["source_dir"])
    target_dir = Path(planned["target_dir"])
    state = Path(planned["state_root"])
    package = _source_package(source_dir)
    backup_root = _backup_dir(state, name=planned["skill_name"], host=planned["host"],
                              scope=planned["scope"])
    previous: dict[str, bytes | None] = {}
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        outputs = _file_outputs(source_dir, target_dir, package)
        for relative in sorted(package["files"], key=lambda item: item["path"]):
            path = target_dir / Path(relative["path"])
            previous[relative["path"]] = path.read_bytes() if path.is_file() else None
        with installation_guard(state):
            for rel, data in previous.items():
                if data is not None:
                    backup_path = backup_root / Path(rel)
                    safe_install_path(state, backup_path)
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    backup_path.write_bytes(data)
            record = {
                "schema_version": SCHEMA_VERSION, "owner": OWNER,
                "skill_name": planned["skill_name"], "host": planned["host"],
                "scope": planned["scope"], "route": planned["route"],
                "before_content_digest": package["content_digest"],
                "managed_files": sorted(previous),
                # 每文件记录"我们装上去的字节摘要"：回滚时据此判定用户是否改过。
                "managed_digests": {entry["path"]: entry["sha256"] for entry in package["files"]},
                "backup_dir": str(backup_root),
                "source_dir": str(source_dir),
                "deployed_at": deployed_at,
            }
            record_file = safe_install_path(state, Path(planned['record_path']))
            # Source: Codex integration, 2026-10-08. Files and ownership record
            # are one transaction, so a record failure cannot leave unowned writes.
            outputs[record_file] = json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
            transactional_write(outputs)
    except Exception as exc:  # 事务失败：transactional_write 已回滚它自己写入的部分
        return {"schema_version": SCHEMA_VERSION, "deployed": False, "action": planned["action"],
                "reason_codes": ["TRANSACTION_FAILED"], "error": f"{type(exc).__name__}: {exc}",
                "plan": planned, "target_rolled_back": True,
                "native_discovery_claimed": False}
    verified = _read_package(target_dir)
    matched = bool(verified and verified["content_digest"] == package["content_digest"])
    return {"schema_version": SCHEMA_VERSION, "deployed": True,
            "action": planned["action"], "reason_codes": sorted(set(planned["reason_codes"])),
            "skill_name": planned["skill_name"], "host": planned["host"], "scope": planned["scope"],
            "target_dir": str(target_dir), "files_written": len(package["files"]),
            "content_digest": package["content_digest"], "verified_after_write": matched,
            "backup_dir": str(backup_root), "record_path": planned["record_path"],
            "native_discovery_claimed": False, "plan": planned}


def rollback(host: object, *, source_dir: Path | str, confirm: bool = False,
             project_root: Path | str = None, global_root: Path | str = None,
             scope: str = adapters.SCOPE_PROJECT, route: str = "desktop",
             state_root: Path | str = None) -> dict:
    """只撤销本记录托管的文件；用户改动过的文件跳过并上报。"""
    planned = plan(host, source_dir=source_dir, project_root=project_root,
                   global_root=global_root, scope=scope, route=route,
                   state_root=state_root)
    if not confirm:
        return {"schema_version": SCHEMA_VERSION, "rolled_back": False,
                "reason_codes": ["CONFIRMATION_REQUIRED"], "plan": planned}
    record = _record_for(_load_record(Path(planned["record_path"])),
                         host=planned["host"], scope=planned["scope"], route=planned["route"])
    if record is None:
        return {"schema_version": SCHEMA_VERSION, "rolled_back": False,
                "reason_codes": ["NO_MANAGED_RECORD"], "plan": planned}
    target_dir = Path(planned["target_dir"])
    state = Path(planned['state_root'])
    expected_backup = _backup_dir(state, name=planned['skill_name'], host=planned['host'], scope=planned['scope'])
    if (record.get('skill_name') != planned['skill_name']
            or record.get('schema_version') != SCHEMA_VERSION
            or not isinstance(record.get('backup_dir'), str)
            or Path(record['backup_dir']).absolute() != expected_backup.absolute()):
        raise SkillDeployError('INVALID_RECORD: 回滚备份或技能身份与登记上下文不匹配')
    backup_root = safe_install_path(state, expected_backup)
    ours = record.get("managed_digests") if isinstance(record.get("managed_digests"), dict) else {}
    managed = record.get('managed_files')
    if (not isinstance(managed, list) or not managed or len(managed) > 1024
            or any(not isinstance(rel, str) for rel in managed)
            or len(set(managed)) != len(managed) or set(managed) != set(ours)
            or any(not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                   for digest in ours.values())):
        raise SkillDeployError('INVALID_RECORD: 托管文件与原写入摘要必须一一对应')
    # Check the complete plan before any mutation, including backup links.
    for rel in managed:
        if (not rel or '\\' in rel or ':' in rel or Path(rel).is_absolute()
                or any(part in ('', '.', '..') for part in rel.split('/'))):
            raise SkillDeployError('INVALID_RECORD: 托管路径不安全')
        safe_install_path(target_dir, target_dir / rel)
        safe_install_path(backup_root, backup_root / rel)
    restored, removed, skipped = [], [], []
    with installation_guard(Path(planned["state_root"])):
        safe_install_path(state, Path(planned['record_path']))
        if _load_record(Path(planned['record_path'])) != record:
            raise SkillDeployError('RECORD_CHANGED: 回滚记录在预览后变化')
        for rel in record.get("managed_files") or []:
            path = target_dir / Path(rel)
            try:
                safe_install_path(target_dir, path)
            except Exception:
                skipped.append({"path": rel, "reason": "UNSAFE_PATH"})
                continue
            if not path.is_file():
                skipped.append({"path": rel, "reason": "ALREADY_ABSENT"})
                continue
            current = path.read_bytes()
            if _sha256(current) != ours[rel]:
                # 部署后被用户改动过：不覆盖用户改动，保留文件并如实上报。
                skipped.append({"path": rel, "reason": "USER_MODIFIED_SKIPPED"})
                continue
            backup_path = backup_root / Path(rel)
            if backup_path.is_file():
                _atomic_write(path, backup_path.read_bytes())
                restored.append(rel)
            else:
                path.unlink()
                removed.append(rel)
        if not any(item['reason'] != 'ALREADY_ABSENT' for item in skipped):
            Path(planned["record_path"]).unlink(missing_ok=True)
    return {"schema_version": SCHEMA_VERSION, "rolled_back": True,
            "complete": not any(item['reason'] != 'ALREADY_ABSENT' for item in skipped),
            "restored": sorted(restored), "removed": sorted(removed),
            "skipped": sorted(skipped, key=lambda item: item["path"]),
            "host": planned["host"], "scope": planned["scope"],
            "target_dir": str(target_dir)}


def deploy_many(items, *, confirm: bool = False, state_root: Path | str = None,
                deployed_at: float | None = None) -> dict:
    """多宿主部署：逐宿主独立事务，一个失败不影响其他宿主已写入的结果。"""
    results = []
    for item in items:
        host = item.get("host")
        try:
            result = deploy(host, source_dir=item["source_dir"], confirm=confirm,
                            project_root=item.get("project_root"),
                            global_root=item.get("global_root"),
                            scope=item.get("scope", adapters.SCOPE_PROJECT),
                            route=item.get("route", "desktop"),
                            state_root=state_root, deployed_at=deployed_at)
        except Exception as exc:
            result = {"schema_version": SCHEMA_VERSION, "deployed": False, "host": host,
                      "reason_codes": ["HOST_FAILED"],
                      "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
    deployed = [item for item in results if item.get("deployed")]
    unchanged = [item for item in results if item.get("action") == ACTION_UNCHANGED]
    failed = [item for item in results if not item.get("deployed")
              and item.get("action") != ACTION_UNCHANGED]
    return {"schema_version": SCHEMA_VERSION,
            "hosts_total": len(results), "deployed": len(deployed),
            "unchanged": len(unchanged), "failed": len(failed),
            "results": results,
            "isolated_failures": sorted(item.get("host") for item in failed if item.get("host"))}


def evidence_report(record: dict | None, *, host: str, scope: str, route: str,
                    discovery_evidence: dict | None = None,
                    call_evidence: dict | None = None, now_ts: float) -> dict:
    """三态分列：安装事实来自记录；发现与调用各自独立取证，互不代证。"""
    installed = _record_for(record, host=host, scope=scope, route=route) is not None
    context = {"host": host, "scope": scope, "route": route}
    discovery = evaluate_evidence(discovery_evidence, context, now_ts=now_ts)
    called = evaluate_evidence(call_evidence, context, now_ts=now_ts)
    return {"schema_version": SCHEMA_VERSION,
            "installed": "recorded" if installed else "unverified",
            "discovered": discovery["state"], "called": called["state"],
            "discovery_reason_codes": discovery["reason_codes"],
            "call_reason_codes": called["reason_codes"],
            "note": "文件已写入不等于宿主已发现，更不等于被实际调用；三态互不代证。"}
