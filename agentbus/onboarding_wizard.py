# -*- coding: utf-8 -*-
"""入组向导纯模型与动作合同（品牌中立）。

来源：Trae（平级成员），2026-10-08；任务 m-1791457584497-b68c42（TR-RESCUE），
作业 j-b7ed91331128d934d22268834e306bd4；计划快照 r-cdef881858336918798c18bb8f096f25。

边界（写死）：
- 纯函数：不读文件、不写文件、不启进程、不调模型、不访问网络、不手写总线 JSON、不认证身份。
- 从任意成员 ID 开始；对协调者与新成员各生成一份自足指令；给出一个有界能力样例；
  默认文件路线，可选适配器仅作说明，绝不改变默认路线。
- 不硬编码任何品牌名：输出文本与标签只含通用角色（协调者/成员）与路线措辞。
- 「已保存/已复制/已发送」都不等于执行；本模块只产出计划与指令，不产出执行证据。

集成说明：正式包内应由 `profiles.validate_agent_id` 承担成员 ID 校验，
由 `invitation_copy.build_instructions` 承担两份指令措辞；本候选模块自带等价实现以便独立测试。
"""
from __future__ import annotations

import re

__all__ = [
    "WIZARD_STEPS",
    "ROUTE_FILE",
    "ROUTE_ADAPTER",
    "ROUTE_LABELS",
    "ADAPTER_KINDS",
    "DEFAULT_ACCEPTANCE",
    "READONLY_ACCEPTANCE",
    "SAMPLE_BUDGET_S",
    "validate_member_id",
    "describe_adapter",
    "build_wizard",
]

ROUTE_FILE = "file"
ROUTE_ADAPTER = "adapter"
ROUTE_LABELS = {
    ROUTE_FILE: "桌面文件路线（默认）",
    ROUTE_ADAPTER: "可选适配器路线（需已真实验收后才可用于正式派工）",
}
ADAPTER_KINDS = ("acp", "cli", "sdk")

WIZARD_STEPS = (
    ("project", "选择项目", "确认项目目录、负责人与当前小组"),
    ("member", "登记新成员", "填写成员 ID、显示名与接入路线"),
    ("save", "保存并检查工作流", "保存成员，检查公开工作流冲突（不静默回滚）"),
    ("sample", "创建有界接入样例", "由程序创建唯一接入样例任务，禁止手写总线 JSON"),
    ("instructions", "显示两份指令", "生成给协调者与给新成员的自足指令"),
)

DEFAULT_ACCEPTANCE = (
    "读取一份短 UTF-8 输入文件，回报其项目相对路径与 path/bytes/sha256",
    "在指定输出路径写出结果，回报 path/bytes/sha256",
    "自述当前可用工具与限制；未实测项必须写「未验证」",
)
READONLY_ACCEPTANCE = (
    "读取一份短 UTF-8 输入文件，回报其项目相对路径与 path/bytes/sha256",
    "给出只读分析结论并自述可用工具与限制；写能力保持「未验证」",
)

SAMPLE_BUDGET_S = 1800
REQUIRED_KEYS = (
    "workspace",
    "member_id",
    "display_name",
    "coordinator",
    "task_id",
    "inbox_path",
    "input_path",
    "output_path",
)
_DEVICE_NAMES = {"con", "prn", "aux", "nul", "clock$",
                 *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}
_SYSTEM_MAILBOXES = {"human", "director"}


def validate_member_id(member_id) -> str:
    """成员 ID 是便携的单一目录分量，不是标签或路径。"""
    if (not isinstance(member_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", member_id)
            or member_id in _DEVICE_NAMES):
        raise ValueError("成员 ID 需为 1–64 位小写字母/数字/短横线，以字母开头；不能是路径或系统设备名")
    if member_id in _SYSTEM_MAILBOXES:
        raise ValueError("系统信箱名不能注册为小组成员")
    return member_id


def _require_text(request: dict, key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少必需字段或为空: %s" % key)
    return value.strip()


def _text_list(value, fallback) -> list:
    if value is None:
        return list(fallback)
    if isinstance(value, str):
        return [value.strip()] if value.strip() else list(fallback)
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or list(fallback)
    return list(fallback)


def describe_adapter(adapter) -> dict:
    """可选适配器说明：不影响默认文件路线，未验收不得用于正式派工。"""
    if not isinstance(adapter, dict) or not adapter:
        return {
            "available": False,
            "kind": None,
            "label": "无适配器（保持默认文件路线）",
            "verified": False,
            "usable_for_dispatch": False,
            "note": "适配器只是可选接入，不是成员加入条件。",
        }
    kind = adapter.get("kind")
    kind = kind.strip() if isinstance(kind, str) and kind.strip() else "未声明"
    verified = adapter.get("verified") is True
    return {
        "available": True,
        "kind": kind,
        "label": "可选适配器：%s" % kind,
        "verified": verified,
        "usable_for_dispatch": verified,
        "note": ("适配器已验收，可按需启用。" if verified else
                 "适配器未验收：仅作说明，正式派工仍走默认文件路线。"),
    }


def _steps(request: dict, instructions: dict) -> list:
    done = {
        "project": bool(_finite(request.get("workspace"))),
        "member": bool(_finite(request.get("member_id")) and _finite(request.get("display_name"))),
        "save": request.get("saved") is True,
        "sample": bool(_finite(request.get("task_id"))),
        "instructions": bool(instructions.get("leader") and instructions.get("newcomer")),
    }
    return [{"id": sid, "label": label, "detail": detail, "done": done[sid]}
            for sid, label, detail in WIZARD_STEPS]


def _finite(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _instructions(values: dict, acceptance: list, readonly: bool, route_label: str,
                  adapter: dict, sample: dict) -> dict:
    acceptance_block = "\n".join("  %d) %s" % (i + 1, item)
                                 for i, item in enumerate(acceptance))
    mode_line = ("接入方式：只读参与（当前无写工具）；写能力保持未验证，只读完成判据同样计为完成。"
                 if readonly else
                 "接入方式：%s。" % route_label)
    adapter_line = ("可选适配器：%s" % adapter["label"] if adapter["available"] else adapter["label"])

    leader = """【入组向导 · 给协调者】把新成员加入本项目小组。
项目目录（绝对路径）：{workspace}
你的身份：协调者 {coordinator}
新成员身份：{display_name}（成员 ID：{member_id}）
本次接入样例任务的真实 task ID：{task_id}
新成员信箱：{inbox_path}
样例输入：{input_path}
样例输出：{output_path}

{adapter_line}

请依次做四件事：
1) 只登记并勾选 {member_id}，不改动其他成员、不更换协调者。
2) 由程序按登记接口创建本次唯一接入样例任务（task ID 必须是上面这一个）；
   禁止手写总线 JSON，禁止自行伪造认领或回执。重复操作复用同一 task ID，不生成重复任务。
3) 把「给新成员」的指令原文交给用户，请用户原样粘贴到 {member_id} 在本项目的对话里发送；
   不要转述、不要改写、不要增删路径。
4) 收到回执后按下述位置与判据独立校验，结论写回记录并附来源与时间。

完成判据（逐项核对，缺项写「未验证」，不得据推测放行）：
{acceptance_block}
产物一律以原始 path/bytes/sha256 清单核对。
样例预算：{budget_s} 秒；到期记录真实待办，不延期、不伪装完成。""".format(
        acceptance_block=acceptance_block, budget_s=sample["budget_s"],
        adapter_line=adapter_line, **values)

    newcomer = """【入组向导 · 给新成员】你是本项目小组的平级成员。
项目目录（绝对路径）：{workspace}
你的成员 ID：{member_id}（显示名：{display_name}）
本次任务的真实 task ID：{task_id}
结果收件人（显式）：{coordinator}
你的信箱：{inbox_path}
样例输入：{input_path}
样例输出：{output_path}

{mode_line}
{adapter_line}

请按下面要求完成，并把结论回给收件人：
1) 先认领任务：优先使用登记接口读取任务、写产物、answer/report 回执；
   禁止手写总线 JSON 伪造认领与回执。
2) 若没有程序工具：用可用的文件工具生成产物，并在聊天里完整回报；由有权限的成员忠实转录，
   转录必须标注「聊天转录，非原生投递」。
3) 验收标准：
{acceptance_block}
4) 产物一律回报原始 path/bytes/sha256；未实测的能力写「未验证」，不要夸大。
5) 按当前宿主权限工作；登录或安装需求由用户在宿主内处理。本项目不代理认证、不读取凭据。
6) 认领不等于执行：只有真实回执与产物校验才算完成。""".format(
        acceptance_block=acceptance_block, mode_line=mode_line,
        adapter_line=adapter_line, **values)

    return {"leader": leader, "newcomer": newcomer}


def build_wizard(request: dict) -> dict:
    """从任意成员 ID 开始，产出入组向导纯模型。

    缺必需字段、成员 ID 非法或路线非法 → ValueError；不读文件、不执行、不写总线 JSON。
    """
    if not isinstance(request, dict):
        raise ValueError("request 必须是字典")

    values = {key: _require_text(request, key) for key in REQUIRED_KEYS}
    validate_member_id(values["member_id"])
    validate_member_id(values["coordinator"])

    writable = request.get("writable")
    writable = writable if isinstance(writable, bool) else True
    readonly = writable is False

    route = request.get("route")
    route = route.strip() if isinstance(route, str) and route.strip() else ROUTE_FILE
    if route not in (ROUTE_FILE, ROUTE_ADAPTER):
        raise ValueError("route 只能是 %s 或 %s" % (ROUTE_FILE, ROUTE_ADAPTER))

    adapter = describe_adapter(request.get("adapter"))
    fallback = READONLY_ACCEPTANCE if readonly else DEFAULT_ACCEPTANCE
    acceptance = _text_list(request.get("acceptance"), fallback)

    budget_s = request.get("budget_s")
    budget_s = int(budget_s) if isinstance(budget_s, (int, float)) and budget_s > 0 else SAMPLE_BUDGET_S
    now_ts = request.get("now_ts")
    now_ts = float(now_ts) if isinstance(now_ts, (int, float)) else None
    deadline_ts = (now_ts + budget_s) if now_ts is not None else None

    capability_family = "readonly-review" if readonly else "file-write"
    sample = {
        "task_id": values["task_id"],
        "inbox_path": values["inbox_path"],
        "input_path": values["input_path"],
        "output_path": values["output_path"],
        "acceptance": list(acceptance),
        "writable": writable,
        "capability_family": capability_family,
        "budget_s": budget_s,
        "deadline_ts": deadline_ts,
        "expected_artifacts": [] if readonly else [values["output_path"]],
    }

    instructions = _instructions(values, acceptance, readonly,
                                 ROUTE_LABELS[route], adapter, sample)

    return {
        "ok": True,
        "member_id": values["member_id"],
        "display_name": values["display_name"],
        "coordinator": values["coordinator"],
        "workspace": values["workspace"],
        "task_id": values["task_id"],
        "route": route,
        "route_label": ROUTE_LABELS[route],
        "adapter": adapter,
        "readonly": readonly,
        "acceptance": list(acceptance),
        "sample": sample,
        "steps": _steps(request, instructions),
        "instructions": instructions,
        "brand_neutral": True,
        "execution_verified": False,
        "model_calls": 0,
    }
