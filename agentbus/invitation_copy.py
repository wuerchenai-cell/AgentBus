# -*- coding: utf-8 -*-
"""邀请两份指令与证据状态：纯函数实现。

来源：WorkBuddy（平级成员），2026-10-05，任务 m-1791203447665-dc76f8。
采纳计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…cc3b5，
以及 DESIGN_RESOLUTIONS 中「正式评审回应：WorkBuddy」第 1–4 项。

边界（写死）：
- 纯函数：不读文件、不写文件、不启进程、不调模型、不手写总线 JSON、不认证身份。
- 默认 `acceptance` 与 `transport` 不得为空；只读成员走只读完成判据，写能力保持未验证。
- 状态独立派生，互不作为前置：合法回执可以没有「已复制」与「自述」；
  failed 回执只证明通道有效，**不得**让能力通过；转录样本不得冒充原生回执。
- 「已认领」不产生任何运行中证据：文案只出现「运行未确认」，不出现「正在执行」或「在线」。
"""
from __future__ import annotations

import re
from datetime import datetime

__all__ = [
    "DEFAULT_ACCEPTANCE",
    "READONLY_ACCEPTANCE",
    "TRANSPORT_DEFAULT",
    "REQUIRED_CONTEXT_KEYS",
    "STATE_KEYS",
    "build_instructions",
    "describe_invitation",
]

REQUIRED_CONTEXT_KEYS = (
    "workspace",
    "member",
    "display_name",
    "coordinator",
    "task_id",
    "inbox_path",
    "input_path",
    "output_path",
)

DEFAULT_ACCEPTANCE = (
    "读一份短 UTF-8 输入文件，并回报其相对路径与 path/bytes/sha256",
    "在指定输出路径写出结果，并回报 path/bytes/sha256",
    "自述当前可用工具与限制；未实测项必须写明「未验证」",
)

READONLY_ACCEPTANCE = (
    "读一份短 UTF-8 输入文件，并回报其相对路径与 path/bytes/sha256",
    "给出只读分析结论，并自述可用工具与限制；写能力保持「未验证」",
)

TRANSPORT_DEFAULT = (
    "文件产物 ＋ 聊天完整回报 ＋ 由有权限成员忠实转录（非原生投递）"
)

STATE_KEYS = (
    "copied",
    "sent",
    "self_reported",
    "receipt_channel_verified",
    "capability_passed",
)

_NATIVE_TRANSPORTS = ("native", "program", "registration")


def _require_text(context: dict, key: str) -> str:
    value = context.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("context 缺少必需字段或为空: %s" % key)
    return value.strip()


def _text_list(value, fallback) -> list:
    if value is None:
        return list(fallback)
    if isinstance(value, str):
        return [value] if value.strip() else list(fallback)
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or list(fallback)
    return list(fallback)


def _bool_or_none(value):
    return value if isinstance(value, bool) else None


def _observed_delivery(evidence: dict):
    observer = evidence.get("observed_sent_by")
    observed_at = evidence.get("observed_at")
    if (not isinstance(observer, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", observer.strip())
            or not isinstance(observed_at, str)):
        return "", ""
    try:
        parsed = datetime.fromisoformat(observed_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "", ""
    except ValueError:
        return "", ""
    return observer.strip(), observed_at


def build_instructions(context: dict) -> dict:
    """生成「给负责人」与「给新成员」两份可执行自然语言指令。

    缺必需字段或值为空 → ValueError；不读文件、不执行、不写总线 JSON。
    """
    if not isinstance(context, dict):
        raise ValueError("context 必须是字典")

    values = {key: _require_text(context, key) for key in REQUIRED_CONTEXT_KEYS}

    writable = _bool_or_none(context.get("writable"))
    readonly = writable is False
    fallback = READONLY_ACCEPTANCE if readonly else DEFAULT_ACCEPTANCE
    acceptance = _text_list(context.get("acceptance"), fallback)

    transport = context.get("transport")
    transport = transport.strip() if isinstance(transport, str) and transport.strip() \
        else TRANSPORT_DEFAULT

    request_id = context.get("request_id")
    request_id = request_id.strip() if isinstance(request_id, str) and request_id.strip() \
        else "invite:%s" % values["member"]
    invite_record_path = ".agentbus/invites/%s.json" % request_id.replace(":", "-")

    acceptance_block = "\n".join("  %d) %s" % (i + 1, item)
                                 for i, item in enumerate(acceptance))

    mode_line = ("接入方式：只读参与（当前不具备写工具）；写能力未验证，"
                 "只读完成判据同样计为完成。" if readonly else
                 "接入方式：桌面文件路线（默认，无需 CLI 登录）。")

    leader = """【邀请协调】把新成员加入本项目小组。
项目目录（绝对路径）：{workspace}
你的身份：组长 {coordinator}
新成员身份：{display_name}（成员 ID：{member}）
本次邀请任务的真实 task ID：{task_id}
邀请记录（本记录由程序准备/提交，不由本指令落盘）：{invite_record_path}

请依次做四件事：
1) 只登记并勾选 {member}，不改动其他成员、不更换组长。
2) 由程序按登记接口创建本次接入样例任务（task ID 必须是上面这一个）；
   禁止手写总线 JSON，禁止自行伪造认领或回执。
3) 把「给新成员」的指令原文交给用户，请用户粘贴到 {member} 在本项目的对话里发送；
   不要转述、不要改写、不要增删路径。
4) 收到回执后按下列位置与判据独立校验；校验结论写回邀请记录，来源与时间齐全。

新成员要读的位置：{inbox_path}
样例输入：{input_path}
样例输出：{output_path}
显式结果收件人：{coordinator}
完成判据（逐项核对，缺项写「未验证」，不得据推测放行）：
{acceptance_block}
产物一律以原始 path/bytes/sha256 清单核对。

回投方式（默认，必须原样给出，不得空白）：
  {transport}
说明：非原生投递时，回执正文必须写明来源、时间、观察到的成员回复与运输方式；
转录只证明通道可达，不得据此断言原生通道通过或写能力通过。""".format(
        acceptance_block=acceptance_block, transport=transport, **values,
        invite_record_path=invite_record_path)

    newcomer = """【小组接入】你是本项目小组的平级成员。
项目目录（绝对路径）：{workspace}
你的成员 ID：{member}（显示名：{display_name}）
本次任务的真实 task ID：{task_id}
结果收件人（显式）：{coordinator}
回执 / 输入文件位置：{inbox_path}
样例输入：{input_path}
样例输出：{output_path}

{mode_line}

请按下面要求完成，并把结论回给收件人：
1) 优先使用登记接口读取任务、写产物、answer/report 回执；禁止手写总线 JSON 伪造认领与回执。
2) 若没有程序工具：用可用的文件工具生成产物，并在聊天里完整回报；由有权限的成员忠实转录。
   转录必须在正文标注「聊天转录，非原生投递」。
3) 验收标准：
{acceptance_block}
4) 产物一律回报原始 path/bytes/sha256；未实测的能力写「未验证」，不要夸大。
5) 不需要登录、不需要认证、不需要安装任何东西；本项目不代理认证、不读取凭据。

回投方式：{transport}""".format(
        acceptance_block=acceptance_block, transport=transport,
        mode_line=mode_line, **values)

    return {
        "leader": leader,
        "newcomer": newcomer,
        "member": values["member"],
        "display_name": values["display_name"],
        "coordinator": values["coordinator"],
        "task_id": values["task_id"],
        "acceptance": list(acceptance),
        "transport": transport,
        "readonly": readonly,
        "invite_record_path": invite_record_path,
        "request_id": request_id,
    }


def _has_text(evidence: dict, *keys) -> bool:
    for key in keys:
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value is True:
            return True
        if isinstance(value, (dict, list, tuple)) and len(value) > 0:
            return True
    return False


def describe_invitation(evidence: dict) -> dict:
    """按证据分别派生五个独立布尔；互不为前置，不越级。"""
    if not isinstance(evidence, dict):
        raise ValueError("evidence 必须是字典")

    task_id = evidence.get("task_id")
    task_id = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None

    copied = _has_text(evidence, "copied_at", "copied")
    confirmed_by = evidence.get("sent_confirmed_by")
    human_confirmed = confirmed_by == "user"
    observed_by, observed_at = _observed_delivery(evidence)
    sent = bool(human_confirmed or observed_by)
    self_reported = _has_text(evidence, "self_report", "self_reported")

    receipt_id = evidence.get("receipt_id")
    receipt_id = receipt_id.strip() if isinstance(receipt_id, str) else ""
    linked = evidence.get("linked_task_id")
    linked = linked.strip() if isinstance(linked, str) else ""
    receipt_channel_verified = bool(receipt_id and task_id and linked == task_id)

    sample = evidence.get("capability_sample")
    sample = sample if isinstance(sample, dict) else {}
    sample_outcome = str(sample.get("outcome") or "").lower()
    sample_transport = str(sample.get("transport") or "").lower()
    native_sample = sample_transport in _NATIVE_TRANSPORTS
    sample_family = str(sample.get("capability_family") or "").lower()
    artifact_digest = sample.get("artifact_sha256")
    artifact_verified = (sample.get("artifact_verified") is True
                         and isinstance(artifact_digest, str)
                         and re.fullmatch(r"[0-9a-f]{64}", artifact_digest) is not None)
    sample_evidence_complete = (sample_family == "readonly-review"
                                or (sample_family == "file-write" and artifact_verified))
    outcome = str(evidence.get("outcome") or "").lower()
    # Codex integration correction, 2026-10-05: transport is independent
    # of limited task capability; require the verified, linked completion.
    capability_passed = bool(receipt_channel_verified and sample_outcome == "passed"
                             and outcome == "done" and sample.get("verified") is True
                             and sample.get("acceptance_verified") is True
                             and sample.get("task_id") == task_id
                             and sample.get("result_id") == receipt_id
                             and sample.get("project_id") and sample_evidence_complete)

    labels = {
        "copied": ("已复制（%s）" % evidence.get("copied_at") if copied
                   else "未验证：没有复制事件记录"),
        "sent": ("已送达（来源：用户明确确认）" if human_confirmed else
                 ("已送达（授权观察：%s；时间：%s）" % (observed_by, observed_at) if observed_by else
                  "未验证：没有用户确认或授权观察")),
        "self_reported": ("成员已自报能力与限制" if self_reported
                          else "未验证：没有成员自报"),
        "receipt_channel_verified": ("回执通道已核验（回执 %s 关联 %s）" % (receipt_id, linked)
                                     if receipt_channel_verified
                                     else "未验证：没有可核验的关联回执"),
        "capability_passed": ("本次有限能力样本通过（运输方式 %s；不证明原生通道）" % (sample_transport or "未声明")
                              if capability_passed
                              else ("未验证：转录不证明原生通道，能力样本须关联完成回执及实际验收" if sample_transport
                                    and not native_sample
                                    else "未验证：没有通过的能力样本")),
    }

    claimed = evidence.get("claimed_by")
    claimed = claimed.strip() if isinstance(claimed, str) and claimed.strip() else ""

    if capability_passed:
        next_action = "按用户要求安排下一个有界任务"
    elif receipt_channel_verified and claimed:
        next_action = "已认领·运行未确认：请回原会话确认执行状态，并要求原生能力样本"
    elif receipt_channel_verified:
        next_action = "回执已收到；请独立校验本次有限能力样本"
    elif claimed:
        next_action = "已认领·运行未确认：请回原会话确认执行状态"
    elif sent:
        next_action = "等待该成员在聊天里确认收到本次 task ID"
    elif copied:
        next_action = "请把指令原文粘贴到该成员在本项目的对话里发送"
    else:
        next_action = "复制两份指令后交给用户送达"

    return {
        "copied": copied,
        "sent": sent,
        "self_reported": self_reported,
        "receipt_channel_verified": receipt_channel_verified,
        "capability_passed": capability_passed,
        "labels": labels,
        "next_action": next_action,
        "task_id": task_id,
        "escalates": False,
    }
