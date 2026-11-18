# -*- coding: utf-8 -*-
"""registry.py: 连接器清单加载、格式校验与兼容匹配实现。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.contracts import TriggerContractError, TriggerErrorCode


@dataclass
class InstallationObservation:
    """发现的软件安装实例公开元数据。"""
    installation_key: str
    product_id: str
    product_name: str
    vendor: str
    channel: str = "stable"
    version: str = "1.0.0"
    executable_path: str = ""
    os: str = "windows"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstallationObservation":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ConnectorDescriptor:
    """连接器清单描述符。"""
    schema_version: int = 1
    connector_id: str = ""
    connector_version: str = "1.0.0"
    protocol_version: str = "agentbus-trigger/1"
    display_name: str = ""
    products: List[Dict[str, Any]] = field(default_factory=list)
    entry_kind: str = "hook"  # host_bridge, extension, hook, sidecar, session_client, manual
    execution_surface: str = "desktop_existing"
    transport: str = "file_channel"
    asset_refs: List[Dict[str, Any]] = field(default_factory=list)
    install_actions: List[Dict[str, Any]] = field(default_factory=list)
    activation: Dict[str, Any] = field(default_factory=dict)
    permission_requirements: List[str] = field(default_factory=list)
    declared_capabilities: Dict[str, bool] = field(default_factory=dict)
    docs_refs: List[str] = field(default_factory=list)
    maintenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectorDescriptor":
        if not isinstance(data, dict):
            raise TriggerContractError("Manifest data must be a dict")
        if data.get("schema_version") != 1:
            raise TriggerContractError(
                f"Unsupported manifest schema_version: {data.get('schema_version')}",
                TriggerErrorCode.CONNECTOR_UNSUPPORTED
            )
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


def load_manifest(manifest_path: Union[str, Path]) -> ConnectorDescriptor:
    """从本地文件读取并解析 ConnectorDescriptor。"""
    p = Path(manifest_path)
    if not p.is_file():
        raise TriggerContractError(f"Manifest file not found: {p}", TriggerErrorCode.CONNECTOR_UNSUPPORTED)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return ConnectorDescriptor.from_dict(data)
    except TriggerContractError:
        raise
    except Exception as exc:
        raise TriggerContractError(f"Failed to parse manifest: {exc}", TriggerErrorCode.CONNECTOR_UNSUPPORTED)


def match_connectors(
    installation: InstallationObservation,
    descriptors: List[ConnectorDescriptor]
) -> List[ConnectorDescriptor]:
    """根据安装身份匹配兼容的连接器清单。"""
    matches = []
    target_prod = installation.product_id.lower().strip()
    target_channel = installation.channel.lower().strip()
    target_os = installation.os.lower().strip()

    for desc in descriptors:
        for prod in desc.products:
            p_id = prod.get("product_id", "").lower().strip()
            channels = [c.lower().strip() for c in prod.get("channels", ["stable"])]
            oses = [o.lower().strip() for o in prod.get("os", ["windows"])]

            if p_id == target_prod:
                if target_channel in channels and (not oses or target_os in oses):
                    matches.append(desc)
                    break
    return matches


class ConnectorRegistry:
    """连接器注册表管理类。"""

    def __init__(self, manifests_dir: Optional[Union[str, Path]] = None):
        if manifests_dir is None:
            self.manifests_dir = Path(__file__).parent / "manifests"
        else:
            self.manifests_dir = Path(manifests_dir)
        self.descriptors: Dict[str, ConnectorDescriptor] = {}
        self.reload()

    def reload(self) -> None:
        self.descriptors.clear()
        if not self.manifests_dir.is_dir():
            return
        for mf in self.manifests_dir.glob("*.json"):
            try:
                desc = load_manifest(mf)
                self.descriptors[desc.connector_id] = desc
            except Exception:
                continue

    def get_connector(self, connector_id: str) -> Optional[ConnectorDescriptor]:
        return self.descriptors.get(connector_id)

    def list_connectors(self) -> List[ConnectorDescriptor]:
        return list(self.descriptors.values())

    def find_matching_connectors(self, installation: InstallationObservation) -> List[ConnectorDescriptor]:
        return match_connectors(installation, list(self.descriptors.values()))
