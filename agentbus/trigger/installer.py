# -*- coding: utf-8 -*-
"""installer.py: 宿主连接组件安装计划、原子执行、摘要核验与回滚撤销。"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.contracts import (
    TriggerContractError,
    TriggerErrorCode,
    normalize_project_key,
)
from agentbus.trigger.registry import ConnectorDescriptor, InstallationObservation


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write_json(target_path: Path, data: dict) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(target_path.parent),
        prefix=f".{target_path.stem}_",
        suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, str(target_path))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


@dataclass
class InstallPlan:
    plan_id: str
    installation_key: str
    connector_id: str
    project_key: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    reload_required: bool = False
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstallPlan":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def plan_install(
    installation: InstallationObservation,
    workspace: Union[str, Path],
    connector: ConnectorDescriptor,
) -> InstallPlan:
    """生成连接组件的安装计划（不产生副作用，仅列出改动）。"""
    norm_ws = normalize_project_key(workspace)
    plan_id = f"plan_{uuid.uuid4().hex[:12]}"
    reload_req = connector.activation.get("reload_required", False)

    actions = []
    for act in connector.install_actions:
        actions.append(dict(act))

    return InstallPlan(
        plan_id=plan_id,
        installation_key=installation.installation_key,
        connector_id=connector.connector_id,
        project_key=norm_ws,
        actions=actions,
        reload_required=reload_req,
        created_at=_now_iso(),
    )


def apply_install(
    workspace: Union[str, Path],
    plan: InstallPlan,
    *,
    authorization_ref: str = "",
) -> Dict[str, Any]:
    """执行安装计划（校验包摘要、原子写入并记录撤销日志）。"""
    ws = Path(workspace)
    norm_ws = normalize_project_key(ws)
    tx_id = f"inst_tx_{uuid.uuid4().hex[:12]}"
    tx_dir = ws / ".agentbus" / "triggers" / "installations"
    tx_dir.mkdir(parents=True, exist_ok=True)

    installed_files = []

    try:
        for action in plan.actions:
            act_id = action.get("action_id")
            if act_id == "copy_asset":
                src_path = Path(action["source_path"])
                target_rel = action["target_relpath"]
                expected_sha = action.get("sha256")

                if not src_path.is_file():
                    raise TriggerContractError(f"Source asset not found: {src_path}")

                content = src_path.read_bytes()
                actual_sha = hashlib.sha256(content).hexdigest()
                if expected_sha and actual_sha != expected_sha:
                    raise TriggerContractError(
                        f"Asset digest mismatch for {src_path}: expected {expected_sha}, got {actual_sha}",
                        TriggerErrorCode.HOST_DENIED
                    )

                dest_file = ws / target_rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(content)
                installed_files.append({
                    "relpath": target_rel,
                    "bytes": len(content),
                    "sha256": actual_sha,
                })
            else:
                # Other actions
                pass

        # Save transaction log
        tx_data = {
            "transaction_id": tx_id,
            "plan_id": plan.plan_id,
            "installation_key": plan.installation_key,
            "connector_id": plan.connector_id,
            "project_key": norm_ws,
            "installed_files": installed_files,
            "authorization_ref": authorization_ref,
            "installed_at": _now_iso(),
            "status": "applied",
        }
        _atomic_write_json(tx_dir / f"{tx_id}.json", tx_data)

        return {
            "success": True,
            "transaction_id": tx_id,
            "installed_files": installed_files,
            "reload_required": plan.reload_required,
        }

    except Exception:
        # Rollback partial writes if failed
        for item in installed_files:
            p = ws / item["relpath"]
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass  # 资源清理类：安装失败回滚临时文件清理异常忽略
        raise


def rollback_install(workspace: Union[str, Path], transaction_id: str) -> Dict[str, Any]:
    """撤销已安装的连接组件。"""
    ws = Path(workspace)
    tx_file = ws / ".agentbus" / "triggers" / "installations" / f"{transaction_id}.json"
    if not tx_file.is_file():
        raise TriggerContractError(f"Install transaction {transaction_id} not found")

    tx_data = json.loads(tx_file.read_text(encoding="utf-8"))
    rolled_back = []

    for item in tx_data.get("installed_files", []):
        rel = item["relpath"]
        p = ws / rel
        if p.is_file():
            p.unlink()
            rolled_back.append(rel)

    tx_data["status"] = "rolled_back"
    tx_data["rolled_back_at"] = _now_iso()
    _atomic_write_json(tx_file, tx_data)

    return {
        "success": True,
        "transaction_id": transaction_id,
        "rolled_back_files": rolled_back,
    }
