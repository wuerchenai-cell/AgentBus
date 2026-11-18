# -*- coding: utf-8 -*-
"""Task 4：正式展示文案与纯展示层。

来源：WorkBuddy（平级成员），2026-10-05。
- 初次实现：任务 m-1791169024293-907d25（job j-b2ea92e011777b6bcfa403cc91d2b68d）。
- 修正实现：任务 m-1791170402282-71e0df（job j-cc56fdf40e0c01743b077f4793e34447），
  按 Codex 独立复核的 5 项缺陷做最小修复；初次 candidate 与初次回执保留不动。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：IMPLEMENTATION_PLAN.md Task 4、DESIGN_RESOLUTIONS.md 第 1/4/5 节、Global Constraints。

边界（写死，不可由调用方放宽）：
- **纯展示层**：不访问磁盘、不读文件、不执行脚本、不安装、不读私有配置或凭据。
- 展示层不产生权限：`sync_clickable`、`write_permitted`、`delete_permitted` 恒为 False；
  即使上游误传 `write_permitted=True` 也不据此画出可点击同步。
- 安装事实、原生发现、实际调用三态互不代证，初值均为未验证。
- 内容校验：只有取得**可信的目录内容摘要**（64 位十六进制）才显示“内容已校验”；
  无效源、缺摘要、摘要形状不对一律显示“内容未校验”，不得凭 action 无条件宣称已校验。
- 执行版本：**当前无固定执行载体**，上游 `execution_fixed` 只算声明线索、不构成验收，
  一律显示“已记录版本，实际加载未验证”。
- 完成态：必须同时满足“任务成功 + 结果成功 + 产物核对通过”，失败结果不得标绿。
- 等待态：空信箱与旧回执不证明正在等待；没有当前有效的等待载体证据时显示需人工接手。
- 目录内容摘要只证明路径与原始字节一致，不覆盖 POSIX 可执行位、Windows ACL、
  宿主解释条件；执行条件一律显示未验证。
- 本模块只做文案与状态呈现，不构成任何实现完成声明。
"""
from __future__ import annotations

import re
import math

from .skill_preview import _manifest_ok

# Codex internal independent audit integration corrections, 2026-10-05.
# This pure view is not a verification entry point. It consumes trusted caller
# results: content.source_verified (produced by build_sync_preview),
# wait_evidence.verified plus caller observation now_ts, and artifacts_verified.
# Literal True means the caller already verified the relevant evidence; user
# entered declarations do not prove filesystem content or live host execution.

SCHEMA_VERSION = 1

ACTION_CREATE = "create"
ACTION_UNCHANGED = "unchanged"
ACTION_UPDATE = "update"
ACTION_CONFLICT = "conflict"

STAGE_INSPECT_PREVIEW = "inspect-preview"

READONLY_STAGE_NOTE = (
    "当前为只读阶段：只能选择技能并检查与预览；写入需后续受控同步另行授权，"
    "本阶段不提供可点击的同步动作。"
)

CONTENT_CHECKED_NOTE = "内容已校验（目录内容摘要，仅路径与原始字节）；执行条件未验证"
CONTENT_UNVERIFIED_NOTE = (
    "内容未校验：未取得可信的目录内容摘要（源不可信或缺少摘要）；执行条件未验证"
)
EXECUTION_CARRIER_NOTE = (
    "当前无固定执行载体：上游执行固定声明（flags）不构成验收，执行版本一律按实际加载未验证显示"
)

FLOW = (
    "1 选择技能（当前项目、成员与作用域）",
    "2 检查与预览（只读：清单、内容摘要、冲突与原因）",
    "3 后续受控同步（本阶段未开放，需租约内校验与备份，另行授权）",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 六条固定图例：每条给出成立条件与来源证据，禁止用泛化的“接入状态”代替。
LEGEND_ORDER = (
    "unconfigured",
    "manual_takeover",
    "running",
    "waiting",
    "blocked",
    "done",
)

LEGEND = (
    {
        "key": "unconfigured",
        "label": "待配置",
        "condition": "项目、成员、组长或技能尚未保存，或部署记录缺失导致维护者未知。",
        "evidence_source": "项目 .agentbus/config.json、技能部署记录与计划快照的缺失状态。",
        "never": "不得显示为泛化接入状态或可用。",
    },
    {
        "key": "manual_takeover",
        "label": "需人工接手",
        "condition": "任务已在成员信箱待认领，或该成员只有活动对话回合才能读取文件。",
        "evidence_source": ".agentbus/agents/<成员>/ready 中的消息文件与待接手原因。",
        "never": "不得显示为正在自动执行。",
    },
    {
        "key": "running",
        "label": "正在执行",
        "condition": "任务已被认领且在 processing，心跳未过期。",
        "evidence_source": ".agentbus/agents/<成员>/processing 的消息文件与 claimed_by/claimed_at。",
        "never": "不得显示为正在执行，除非存在未过期的 processing 记录与认领信息。",
    },
    {
        "key": "waiting",
        "label": "等待任务",
        "condition": (
            "存在当前有效的等待载体证据（例如有界文件事件等待仍在运行且未超时，"
            "或存在活动的会话回合）。仅信箱为空、或只有旧回执，都不成立；"
            "没有当前有效证据时应显示需人工接手。"
        ),
        "evidence_source": (
            "等待工具的运行时记录与观察时间、活动会话回合；空信箱与旧回执不作为证据。"
        ),
        "never": "不得显示为在线等待或泛化接入状态。",
    },
    {
        "key": "blocked",
        "label": "阻塞",
        "condition": "任务进入 blocked，或校验、冲突、证据失效未通过。",
        "evidence_source": "消息 reason_codes、冲突记录与失效证据的降级原因。",
        "never": "不得显示为等待或已完成；阻塞必须单独显示具体原因。",
    },
    {
        "key": "done",
        "label": "已完成",
        "condition": (
            "三件事同时成立才算完成：源任务状态为 done、结果回执为成功、"
            "产物清单已按 path/bytes/sha256 核对通过。缺任一项都不算完成。"
        ),
        "evidence_source": "done 消息文件、成功 result 回执，以及可核对的产物清单。",
        "never": "不得显示为已完成，除非任务成功、结果成功且产物核对通过；失败结果不得标绿。",
    },
)

_REASON_TEXT = {
    "WRITE_NOT_PERMITTED": "只读阶段不授予写入权",
    "DELETE_RIGHT_NOT_GRANTED": "同内容也不授予删除权",
    "OWNER_UNKNOWN": "没有可核的 AgentBus 部署记录，维护者未知",
    "OWNER_NOT_AGENTBUS": "目标由其他管理器或用户维护",
    "TARGET_MODIFIED": "目标内容已被用户或其他程序改动",
    "ACTIVE_TASK_PIN": "有活跃任务依赖该版本，拒绝原地更新",
    "TARGET_ABSENT": "目标尚不存在",
    "UNCHANGED": "源与目标内容摘要一致",
    "UPDATE_AVAILABLE": "存在可用的新版本",
    "INVALID_HOST": "宿主标识不合法",
    "INVALID_SCOPE": "作用域不合法",
    "INVALID_ROUTE": "路线不合法",
    "INVALID_SOURCE": "源技能包不可信",
    "INVALID_TARGET": "目标技能包不可信",
    "INVALID_RECORD": "部署记录不可信",
    "EXECUTION_FIX_UNVERIFIED": "执行版本固定未验证",
}


class SkillPresentationError(ValueError):
    """展示层输入不是预览对象时抛出；不静默伪造状态。"""


def _is_digest(value) -> bool:
    """只有 64 位十六进制摘要才算可信的内容摘要。"""
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _as_reason_list(value) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _reason_texts(codes) -> list:
    return [_REASON_TEXT.get(code, code) for code in codes]


def _label_tri_state(states: dict) -> tuple:
    installed = states.get("installed")
    if installed == "recorded":
        installation = "已记录安装（AgentBus 部署记录；不代表宿主已发现，也不代表已实际调用）"
    else:
        installation = "未验证（无安装记录）"

    discovery = ("宿主已发现（证据需附宿主、作用域、路线与日期）"
                 if states.get("discovered") == "verified"
                 else "未验证（无宿主发现证据）")

    invocation = ("调用通过（证据需附宿主、路线、内容摘要与日期）"
                  if states.get("called") == "verified"
                  else "未验证（无实际调用证据）")
    return installation, discovery, invocation


def _execution_label() -> str:
    """当前无固定执行载体：一律按实际加载未验证显示（与预览层恒 False 契约一致）。"""
    return "已记录版本，实际加载未验证"


def _waiting_hint(preview: dict) -> str:
    """空信箱与旧回执不证明正在等待。"""
    ev = preview.get("wait_evidence")
    def date_ok(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value) and value >= 0
        except OverflowError:
            return False
    now = preview.get("now_ts")
    observed = ev.get("observed_at_ts") if isinstance(ev, dict) else None
    expires = ev.get("expires_at_ts") if isinstance(ev, dict) else None
    if (isinstance(ev, dict) and ev.get("verified") is True
            and isinstance(ev.get("waiting_tool"), str) and ev["waiting_tool"].strip()
            and date_ok(now) and date_ok(observed) and date_ok(expires)
            and observed <= now < expires):
        return "等待中（有当前有效的等待载体证据：%s）" % ev["waiting_tool"]
    return ("需人工接手（没有当前有效的等待工具或活动会话证据；"
            "空信箱与旧回执都不证明正在等待）")


def _completion_label(preview: dict) -> str:
    """完成态需要 任务成功 + 结果成功 + 产物核对 三者齐备。"""
    status = preview.get("source_status")
    outcome = preview.get("result_outcome")
    arts = preview.get("artifacts")
    has_arts = preview.get("artifacts_verified") is True and _manifest_ok(arts)
    if status != "done":
        return "未完成：任务尚未以 done 状态归档"
    if outcome == "failed":
        return "未完成：结果失败，不得标绿（不满足成功结果条件）"
    if outcome != "success":
        return "未完成：缺少成功结果回执"
    if not has_arts:
        return "未完成：缺少产物清单核对"
    return "已完成（任务成功、结果成功、产物已核对）"


def present_preview(preview: dict) -> dict:
    """把 Task 2 的只读预览对象转成正式展示文案与状态图例。

    只读：不访问磁盘，不授予权限，不伪造发现、调用、等待或完成状态。
    """
    if not isinstance(preview, dict):
        raise SkillPresentationError("preview 必须是字典对象")

    action = preview.get("action")
    action = str(action) if isinstance(action, str) and action else "unknown"
    owner = preview.get("owner") or "unknown"
    scope = preview.get("scope") or "unknown"
    route = preview.get("route") or "unknown"
    reasons = _as_reason_list(preview.get("reason_codes"))
    texts = _reason_texts(reasons)
    reason_join = "；".join(texts)
    states = preview.get("states") if isinstance(preview.get("states"), dict) else {}
    content = preview.get("content") if isinstance(preview.get("content"), dict) else {}

    digest = content.get("source_digest")
    digest_ok = content.get("source_verified") is True and _is_digest(digest)

    installation, discovery, invocation = _label_tri_state(states)
    execution_version = _execution_label()

    if action == ACTION_CONFLICT:
        title = "冲突：需要你确认"
        summary = (
            f"存在冲突（{reason_join}）。不会覆盖任何文件：保留现有内容，"
            f"等待你确认维护者或处理冲突后再继续。当前维护者：{owner}。"
        )
        next_action = ("只读阶段：请先确认维护者或处理冲突，保留现有内容；"
                       "同步写入未开放，不能点击执行。")
    elif action in (ACTION_CREATE, ACTION_UPDATE):
        word = "新建" if action == ACTION_CREATE else "更新"
        title = f"候选：{word}（尚未同步）"
        summary = (f"检测到可{word}的候选，尚未同步。当前为只读阶段，"
                   f"不会写入任何文件；原因：{reason_join}。")
        next_action = "只读阶段：仅可检查与预览；同步写入未开放，不能点击执行。"
    elif action == ACTION_UNCHANGED:
        title = "内容一致"
        summary = ("源与目标内容摘要一致，无需写入；同内容也不授予删除权。"
                   f"原因：{reason_join}。")
        next_action = "只读阶段：无需写入；如需删除必须另行授权。"
    else:
        title = "需要确认：未识别的预览状态"
        summary = (f"预览状态无法识别，不会写入。原因：{reason_join}。"
                   f"请重新检查或人工确认。")
        next_action = "只读阶段：请重新检查与预览；同步写入未开放。"

    notes = [
        CONTENT_CHECKED_NOTE if digest_ok else CONTENT_UNVERIFIED_NOTE,
        EXECUTION_CARRIER_NOTE,
        READONLY_STAGE_NOTE,
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE_INSPECT_PREVIEW,
        "title": title,
        "summary": summary,
        "next_action": next_action,
        "flow": list(FLOW),
        "installation_label": installation,
        "discovery_label": discovery,
        "invocation_label": invocation,
        "execution_version_label": execution_version,
        "execution_fixed_accepted": False,
        "execution_fixed_note": EXECUTION_CARRIER_NOTE,
        "waiting_hint": _waiting_hint(preview),
        "completion_label": _completion_label(preview),
        "owner": owner,
        "scope": scope,
        "route": route,
        "action": action,
        "reason_texts": texts,
        "sync_clickable": False,
        "write_permitted": False,
        "delete_permitted": False,
        "content_checked": digest_ok,
        "content_digest": digest if digest_ok else None,
        "digest_covers": content.get("digest_covers", "paths_and_raw_bytes_only"),
        "execution_metadata_digest": content.get("execution_metadata_digest", "unknown"),
        "legend": [dict(entry) for entry in LEGEND],
        "notes": notes,
    }
