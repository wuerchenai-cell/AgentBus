# -*- coding: utf-8 -*-
"""diagnostics.py: 脱敏诊断包导出、敏感信息清洗与统一错误引导实现。"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.contracts import TriggerErrorCode, normalize_project_key
from agentbus.trigger.journal import DeliveryJournal, DeliveryStatus
from agentbus.trigger.service import TriggerDeliveryService


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


SECRET_PATTERNS = [
    (re.compile(r'''(["']?(?:api[_-]?key|secret|token|password|authorization|cookie|access[_-]?token|refresh[_-]?token)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^,\s}]+)''', re.IGNORECASE), r'\1"[REDACTED]"'),
    (re.compile(r"Basic\s+[A-Za-z0-9+/=]+", re.IGNORECASE), "Basic [REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"(api_key|apikey|secret|token|password|authorization)=['\"]?[A-Za-z0-9_\-\.]+['\"]?", re.IGNORECASE), r"\1=[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}", re.IGNORECASE), "[REDACTED]"),
]


def sanitize_text(text: str) -> str:
    """脱敏字符串中的认证秘钥、Token 与密码。"""
    res = text
    for pattern, repl in SECRET_PATTERNS:
        res = pattern.sub(repl, res)
    return res


def sanitize_data(value):
    """Redact credential fields recursively before rendering diagnostic JSON."""
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if re.search(r'password|secret|token|authorization|cookie|api.?key', str(k), re.I)
                         else sanitize_data(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_data(v) for v in value]
    return sanitize_text(value) if isinstance(value, str) else value


ERROR_RESOLUTION_MAP = {
    TriggerErrorCode.PRODUCT_UNRESOLVED.value: {
        "user_action": "请确认软件完整名称或选择可执行文件路径",
        "program_action": "允许文件协作，不自动选连接器",
    },
    TriggerErrorCode.CONNECTOR_UNSUPPORTED.value: {
        "user_action": "此软件版本暂无官方适配连接器，可导出评估包",
        "program_action": "导出接入评估包，不执行未知命令",
    },
    TriggerErrorCode.INSTALL_RELOAD_REQUIRED.value: {
        "user_action": "连接组件已安装，需在宿主软件中重新加载一次",
        "program_action": "等待新鲜实例握手，禁止重复安装",
    },
    TriggerErrorCode.WORKFLOW_CONFLICT.value: {
        "user_action": "项目工作流摘要冲突，需执行现有工作流修复",
        "program_action": "不创建测试任务与邀请",
    },
    TriggerErrorCode.INVITE_EXPIRED.value: {
        "user_action": "登记邀请已过期，请点击重新生成邀请",
        "program_action": "保留原绑定不变",
    },
    TriggerErrorCode.TARGET_UNKNOWN.value: {
        "user_action": "目标会话未核验或不存在，请打开对应项目聊天",
        "program_action": "不新建空会话，保持未连接",
    },
    TriggerErrorCode.TARGET_MISSING.value: {
        "user_action": "找不到绑定的聊天会话，请重新绑定",
        "program_action": "不自动猜测目标",
    },
    TriggerErrorCode.TARGET_CHANGED.value: {
        "user_action": "当前前台聊天与登记目标不同，请切回绑定聊天",
        "program_action": "可见路线暂停发送，防止误投",
    },
    TriggerErrorCode.PROJECT_MISMATCH.value: {
        "user_action": "聊天或连接器所属项目与当前工作区不匹配",
        "program_action": "零发送，严格项目隔离",
    },
    TriggerErrorCode.BOOTSTRAP_REQUIRED.value: {
        "user_action": "请在绑定的聊天窗口发送一次准备消息以激活运行时",
        "program_action": "等待运行时就绪，不无限重试",
    },
    TriggerErrorCode.AUTH_REQUIRED.value: {
        "user_action": "宿主工具需要授权，请在桌面弹窗中允许连接",
        "program_action": "使用官方授权流程，不提取凭据",
    },
    TriggerErrorCode.HOST_DENIED.value: {
        "user_action": "宿主拒绝了连接请求或权限受限",
        "program_action": "停止当前投递",
    },
    TriggerErrorCode.BUSY.value: {
        "user_action": "Agent 当前正在处理其他请求，请稍候",
        "program_action": "排队等待或通知组长合并回执",
    },
    TriggerErrorCode.APPROVAL_REQUIRED.value: {
        "user_action": "宿主正在等待人工审批，请在宿主中处理",
        "program_action": "保持等待，不自动越权批准",
    },
    TriggerErrorCode.BUSY_UNKNOWN.value: {
        "user_action": "无法确认宿主当前是否空闲",
        "program_action": "不自动连续发送",
    },
    TriggerErrorCode.SEND_UNCERTAIN.value: {
        "user_action": "消息可能已投递但未收到明确确认，请先核实回执",
        "program_action": "禁止盲目重发，先进行断线调和",
    },
    TriggerErrorCode.VERSION_CHANGED.value: {
        "user_action": "软件或连接器已更新，需重新核验连接能力",
        "program_action": "暂停受影响能力，保留历史证据",
    },
    TriggerErrorCode.STOPPED.value: {
        "user_action": "当前项目投递已被暂停或停止",
        "program_action": "停止新请求投递，保留在途任务",
    },
    TriggerErrorCode.SCOPE_EXPIRED.value: {
        "user_action": "本次协作范围或时间预算已耗尽",
        "program_action": "停止新投递，等待人工确认扩预算",
    },
    TriggerErrorCode.INSTANCE_CONFLICT.value: {
        "user_action": "检测到多个冲突的接收实例，请保留唯一活跃窗口",
        "program_action": "不随机选取，要求正常恢复",
    },
}


def get_error_resolution_guide(error_code: str) -> Dict[str, Any]:
    """获取错误码对应的脱敏解释与行动建议。"""
    info = ERROR_RESOLUTION_MAP.get(error_code, {
        "user_action": "遇到未分类错误，请检查日志或导出诊断包",
        "program_action": "暂停自动投递",
    })
    return {
        "code": error_code,
        "user_action": info["user_action"],
        "program_action": info["program_action"],
    }


@dataclass
class DiagnosticBundle:
    """脱敏诊断包。"""
    schema_version: int = 1
    project_key: str = ""
    exported_at: str = ""
    policy_state: Dict[str, Any] = field(default_factory=dict)
    requests_summary: Dict[str, int] = field(default_factory=dict)
    installed_connectors: List[str] = field(default_factory=list)
    sanitized_logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def export_diagnostic_bundle(workspace: Union[str, Path]) -> DiagnosticBundle:
    """导出脱敏诊断包（严格清洗凭据与私人聊天全文）。"""
    ws = Path(workspace)
    norm_ws = normalize_project_key(ws)

    service = TriggerDeliveryService(ws)
    policy = service.load_policy()

    journal = DeliveryJournal(ws)
    counts = {}
    for st in DeliveryStatus:
        counts[st.value] = len(journal.list_by_status(st.value))

    # Export only bounded structured operational errors, never arbitrary chat logs.
    sanitized_logs = []
    triggers_dir = ws / ".agentbus" / "triggers"
    for log_candidate in list((triggers_dir / 'errors').glob('*.json'))[:100]:
        try:
            if log_candidate.stat().st_size > 16384:
                continue
            data = json.loads(log_candidate.read_text(encoding='utf-8'))
            safe = {key:data.get(key) for key in ('status','member_id','error_type','observed_at')}
            sanitized_logs.append(json.dumps(sanitize_data(safe), ensure_ascii=False))
        except (OSError, ValueError, TypeError):
            sanitized_logs.append('{"status":"error","error_type":"UnreadableDiagnosticRecord"}')

    installed = []
    from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
    connector = CodeArtsHookConnector()
    hook = ws / '.codearts/hooks/agentbus.js'
    if hook.is_file() and connector._host_asset.is_file():
        import hashlib
        if hashlib.sha256(hook.read_bytes()).digest() == hashlib.sha256(connector._host_asset.read_bytes()).digest():
            installed.append('agentbus.codearts-hook')

    return DiagnosticBundle(
        schema_version=1,
        project_key=norm_ws,
        exported_at=_now_iso(),
        policy_state=policy.to_dict(),
        requests_summary=counts,
        installed_connectors=installed,
        sanitized_logs=sanitized_logs,
    )
