# -*- coding: utf-8 -*-
"""discovery.py: 软件发现、未知产品身份解析与路径边界安全检查。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from agentbus.trigger.contracts import TriggerContractError, TriggerErrorCode
from agentbus.trigger.registry import InstallationObservation

FORBIDDEN_DIR_FRAGMENTS = [".workbuddy", "credentials", ".ssh", ".aws", "id_rsa"]


@dataclass
class ProductResolutionResult:
    resolved: bool
    product_id: str
    product_name: str = ""
    vendor: str = ""
    channel: str = "stable"
    version: str = "1.0.0"
    error_code: Optional[str] = None
    human_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


KNOWN_PRODUCTS = {
    "codearts": {
        "product_name": "CodeArts Agent",
        "vendor": "Huawei",
        "channel": "stable",
        "default_version": "26.10.1",
    },
    "codex": {
        "product_name": "Codex Desktop",
        "vendor": "OpenAI",
        "channel": "stable",
        "default_version": "1.0.0",
    },
    "trae": {
        "product_name": "Trae IDE",
        "vendor": "ByteDance",
        "channel": "stable",
        "default_version": "0.0.5",
    },
    "workbuddy": {
        "product_name": "WorkBuddy",
        "vendor": "WorkBuddy",
        "channel": "stable",
        "default_version": "1.0.0",
    },
    "opencode": {
        "product_name": "OpenCode CLI (ACP)",
        "vendor": "SST / Anomaly",
        "channel": "stable",
        "default_version": "0.1.0",
    },
    "gemini": {
        "product_name": "Gemini CLI (ACP)",
        "vendor": "Google",
        "channel": "stable",
        "default_version": "0.1.0",
    },
    "cursor": {
        "product_name": "Cursor Agent (ACP)",
        "vendor": "Anysphere",
        "channel": "stable",
        "default_version": "0.45.0",
    },
    "cline": {
        "product_name": "Cline CLI (ACP)",
        "vendor": "Cline Bot",
        "channel": "stable",
        "default_version": "1.0.0",
    },
    "qwen-code": {
        "product_name": "Qwen Code CLI (ACP)",
        "vendor": "Alibaba Qwen",
        "channel": "stable",
        "default_version": "0.1.0",
    },
    "claude-acp": {
        "product_name": "Claude Code ACP",
        "vendor": "Anthropic / Zed ACP",
        "channel": "stable",
        "default_version": "0.1.0",
    },
    "acp": {
        "product_name": "Standard ACP Agent",
        "vendor": "ACP Standard",
        "channel": "stable",
        "default_version": "1.0.0",
    },
}


def _check_forbidden_paths(path_str: str) -> None:
    norm = path_str.replace("\\", "/").lower()
    for forbidden in FORBIDDEN_DIR_FRAGMENTS:
        if forbidden.lower() in norm:
            raise TriggerContractError(
                f"Forbidden path boundary access: '{forbidden}' is prohibited",
                TriggerErrorCode.HOST_DENIED
            )


def resolve_product_identity(name_or_exe: str) -> ProductResolutionResult:
    """根据软件名称或可执行文件名解析合法的产品身份（未知产品明确返回 PRODUCT_UNRESOLVED，不自动猜测）。"""
    raw = name_or_exe.lower().strip()
    if raw.endswith(".exe"):
        raw = raw[:-4]

    if raw in KNOWN_PRODUCTS:
        info = KNOWN_PRODUCTS[raw]
        return ProductResolutionResult(
            resolved=True,
            product_id=raw,
            product_name=info["product_name"],
            vendor=info["vendor"],
            channel=info["channel"],
            version=info["default_version"],
        )

    # Unknown or ambiguous products (e.g. astduio)
    return ProductResolutionResult(
        resolved=False,
        product_id=name_or_exe.strip(),
        error_code=TriggerErrorCode.PRODUCT_UNRESOLVED.value,
        human_message=f"Product identity '{name_or_exe}' cannot be resolved. Please confirm exact installation or use file collaboration mode.",
    )


def discover_installation_from_path(path: Union[str, Path]) -> InstallationObservation:
    """从具体路径安全发现软件安装元数据。"""
    p = Path(path)
    path_str = str(p.resolve())
    _check_forbidden_paths(path_str)

    stem = p.stem.lower()
    res = resolve_product_identity(stem)
    if not res.resolved:
        raise TriggerContractError(
            res.human_message or "Product unresolved",
            TriggerErrorCode.PRODUCT_UNRESOLVED
        )

    inst_key = f"inst_{res.product_id}_{abs(hash(path_str)) % 10000000:07d}"
    return InstallationObservation(
        installation_key=inst_key,
        product_id=res.product_id,
        product_name=res.product_name,
        vendor=res.vendor,
        channel=res.channel,
        version=res.version,
        executable_path=path_str,
        os="windows",
    )
