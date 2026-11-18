# -*- coding: utf-8 -*-
"""宿主技能适配器：把规范 SKILL.md 包落到各宿主的公开项目路径。

来源：WorkBuddy，2026-10-08；任务 m-1791457584489-f05b6b。
边界（写死）：本模块**只做纯路径计算**，不探测 home、不读宿主私有配置、不判断"是否已被加载"。
路径必须由调用方显式给出（项目根或公共根）；未知宿主一律走 generic 适配器并如实标注。
"""
from __future__ import annotations

import re

from .skill_registry import _norm_name, safe_relative_root

HOST_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)

SCOPE_PROJECT = "project"
SCOPE_GLOBAL = "global"
VALID_SCOPES = (SCOPE_PROJECT, SCOPE_GLOBAL)

GENERIC_HOST = "generic"

# 公开项目路径来自项目自身约定（AGENTBUS.md / AGENTS.md / 各宿主工作流目录），
# 不是从宿主私有配置里读出来的。documented=False 表示"尚无公开路径登记，按 generic 处理"。
HOST_ADAPTERS: dict[str, dict] = {
    "codex": {
        "project_relative": ".agentbus/workflows/codex",
        "documented": True, "generic": False,
        "note": "项目工作流：.agentbus/workflows/<host>/SKILL.md",
    },
    "workbuddy": {
        "project_relative": ".codebuddy/skills",
        "documented": True, "generic": False,
        "note": "项目技能目录：.codebuddy/skills/<skill>/SKILL.md",
    },
    "trae": {
        "project_relative": ".trae/skills",
        "documented": True, "generic": False,
        "note": "项目技能目录：.trae/skills/<skill>/SKILL.md",
    },
    "zcode": {
        "project_relative": ".zcode/skills",
        "documented": True, "generic": False,
        "note": "项目技能目录：.zcode/skills/<skill>/SKILL.md",
    },
    "opencode": {
        "project_relative": ".opencode/skills",
        "documented": True, "generic": False,
        "note": "项目技能目录：.opencode/skills/<skill>/SKILL.md",
    },
    "antigravity": {
        "project_relative": None,
        "documented": False, "generic": False,
        "note": "尚未登记公开项目技能路径；不得探测宿主私有目录，按 generic 落盘并标注未验证",
    },
}

GENERIC_ADAPTER: dict = {
    "project_relative": ".agentbus/skills",
    "documented": True, "generic": True,
    "note": "通用适配器：.agentbus/skills/<skill>/SKILL.md；任何未登记宿主与自定义成员都可用",
}


def list_hosts() -> tuple[str, ...]:
    """已登记适配器的宿主（不含 generic）；排序稳定，便于界面与测试。"""
    return tuple(sorted(HOST_ADAPTERS))


def host_public_paths() -> dict[str, str | None]:
    return {host: spec["project_relative"] for host, spec in sorted(HOST_ADAPTERS.items())}


def adapter_for(host: object) -> dict:
    """返回适配器规格的副本。未知宿主回落到 generic，并如实给出原因码。"""
    reasons: list[str] = []
    if not isinstance(host, str) or not HOST_ID.match(host):
        reasons.append("INVALID_HOST_ID")
        spec, host_id, used_generic = GENERIC_ADAPTER, GENERIC_HOST, True
    else:
        host_id = host
        spec = HOST_ADAPTERS.get(host)
        used_generic = False
        if spec is None:
            reasons.append("UNKNOWN_HOST_GENERIC")
            spec, used_generic = GENERIC_ADAPTER, True
        elif not spec.get("documented") or not spec.get("project_relative"):
            reasons.append("NO_PUBLIC_PATH_GENERIC")
            used_generic = True
    return {"host": host_id,
            "requested_host": host,
            "adapter": GENERIC_HOST if used_generic else host_id,
            "project_relative": spec["project_relative"],
            "documented": bool(spec.get("documented")),
            "generic": bool(spec.get("generic")) or used_generic,
            "used_generic": used_generic,
            "reason_codes": sorted(reasons),
            "note": spec["note"],
            "native_discovery_claimed": False,
            "private_probe": False}


def scope_note(scope: object) -> str:
    if scope == SCOPE_PROJECT:
        return "项目级：只影响当前项目目录，随项目文件一起交接，换机器需重新部署。"
    if scope == SCOPE_GLOBAL:
        return ("全局级：写入调用方显式给出的公共根（不探测用户主目录），"
                "影响该根下所有项目；仍不读取任何宿主私有配置。")
    raise ValueError(f"INVALID_SCOPE: scope 必须是 {VALID_SCOPES} 之一")


def resolve_target(host: object, *, skill_name: object, scope: object,
                   project_root: object = None, global_root: object = None) -> dict:
    """计算某宿主某作用域下该技能的目标目录；不创建目录、不写文件、不探测。"""
    if scope not in VALID_SCOPES:
        raise ValueError(f"INVALID_SCOPE: scope 必须是 {VALID_SCOPES} 之一")
    name = _norm_name(skill_name, label="skill_name")
    adapter = adapter_for(host)
    relative = adapter["project_relative"]
    if scope == SCOPE_PROJECT:
        if project_root is None:
            raise ValueError("INVALID_ROOT: 项目级部署必须显式给出 project_root")
        root = safe_relative_root(project_root, project_root, label="project_root")
        if not root.is_dir():
            raise ValueError(f"MISSING_ROOT: project_root 不是已存在目录：{project_root}")
    else:
        if global_root is None:
            raise ValueError("INVALID_ROOT: 全局级部署必须显式给出 global_root（公共根），不做探测")
        root = safe_relative_root(global_root, global_root, label="global_root")
        if not root.is_dir():
            raise ValueError(f"MISSING_ROOT: global_root 不是已存在目录：{global_root}")
    target_root = safe_relative_root(root, root / relative, label="target_root")
    target_dir = safe_relative_root(root, target_root / name, label="target_dir")
    return {"host": adapter["host"], "scope": scope, "route_hint": "file",
            "adapter": adapter["adapter"], "generic": adapter["generic"],
            "used_generic": adapter["used_generic"],
            "reason_codes": list(adapter["reason_codes"]),
            "skill_name": name, "relative": relative,
            "root": str(root), "target_root": str(target_root),
            "target_dir": str(target_dir),
            "exists": target_dir.is_dir(),
            "scope_note": scope_note(scope),
            "native_discovery_claimed": False,
            "note": adapter["note"]}
