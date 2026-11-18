"""
Generic Member Adapter Contract (通用成员插件契约)
Source: Antigravity (TEAM-20261008-IMPLEMENT, task AG-EXT)
Date: 2026-10-08

架构原则：
1. 品牌中立：核心逻辑无任何硬编码品牌 if/else，所有厂商特例均作为声明式数据。
2. 极简接入：新成员仅需 ID、显示名、文件读写能力、可选唤醒适配器和公开技能路径即可加入。
3. 隔离约束：新成员加入不继承旧项目的权限、登录状态、当前在线状态或 processing 在途任务。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Set, Tuple
from .public_files import PublicFileError, validate_relative

# Windows 设备名及系统保留名
RESERVED_NAMES: Set[str] = {
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
    "human", "director"
}

# 合法唤醒适配器类型
VALID_WAKE_ADAPTERS = {"manual", "cli", "acp", "sdk", "none"}

# 默认文件读写能力
DEFAULT_FILE_CAPABILITIES = ("read_utf8", "write_utf8", "atomic_write")
READONLY_FILE_CAPABILITIES = ("read_utf8",)


class MemberAdapterError(ValueError):
    """成员适配器或契约异常"""
    pass


def validate_member_id(name: str) -> str:
    """验证成员标识：1-64位小写字母/数字/中划线，字母开头，非保留名"""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
        raise MemberAdapterError("成员标识需为1–64位小写字母/数字/短横线，以字母开头")
    if name.casefold() in RESERVED_NAMES:
        raise MemberAdapterError(f"成员标识不能是系统保留名或设备名: {name!r}")
    return name


@dataclass(frozen=True)
class MemberAdapter:
    """品牌中立的成员适配器数据定义"""
    member_id: str
    display_name: str
    file_capabilities: Tuple[str, ...] = DEFAULT_FILE_CAPABILITIES
    wake_adapter: str = "manual"
    project_skill_path: Optional[str] = None
    host_version: Optional[str] = None
    model_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_member_id(self.member_id)
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise MemberAdapterError("成员显示名不能为空")
        if len(self.display_name) > 80 or any(ord(c) < 32 for c in self.display_name):
            raise MemberAdapterError("成员显示名长度不能超过80字符")
        if (not isinstance(self.file_capabilities, (tuple, list))
                or len(self.file_capabilities) > 32
                or any(not isinstance(c, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', c)
                       for c in self.file_capabilities)):
            raise MemberAdapterError("文件能力必须是有界公共能力名数组")
        object.__setattr__(self, 'file_capabilities', tuple(self.file_capabilities))
        if not isinstance(self.wake_adapter, str) or self.wake_adapter not in VALID_WAKE_ADAPTERS:
            raise MemberAdapterError(f"不支持的唤醒适配器类型: {self.wake_adapter}")
        metadata_fields = {'vendor', 'documentation_url', 'notes', 'source', 'version'}
        if (not isinstance(self.metadata, dict) or set(self.metadata) - metadata_fields
                or any(not isinstance(value, str) or len(value) > 512
                       or any(ord(c) < 32 for c in value) for value in self.metadata.values())):
            raise MemberAdapterError('适配器元数据仅接受公开标量字段，不含权限、凭据或在途状态')
        object.__setattr__(self, 'metadata', MappingProxyType(dict(self.metadata)))
        for value in (self.host_version, self.model_id):
            if value is not None and (not isinstance(value, str) or not value.strip()
                                      or len(value) > 128 or any(ord(c) < 32 for c in value)):
                raise MemberAdapterError('宿主版本和模型ID必须是有界公共标签')
        if self.project_skill_path is not None:
            try:
                relative = validate_relative(self.project_skill_path)
                if relative.name != 'SKILL.md':
                    raise PublicFileError('skill entry must be SKILL.md')
                object.__setattr__(self, 'project_skill_path', relative.as_posix())
            except PublicFileError as exc:
                raise MemberAdapterError(str(exc)) from exc

    @property
    def writable(self) -> bool:
        """是否具备文件写能力"""
        return "write_utf8" in self.file_capabilities

    def default_skill_path(self) -> str:
        """计算成员默认技能路径（声明式约定）"""
        if self.project_skill_path:
            return self.project_skill_path
        return f".agentbus/workflows/{self.member_id}/SKILL.md"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为标准声明字典"""
        return {
            "member_id": self.member_id,
            "display_name": self.display_name,
            "file_capabilities": list(self.file_capabilities),
            "writable": self.writable,
            "wake_adapter": self.wake_adapter,
            "project_skill_path": self.default_skill_path(),
            "host_version": self.host_version,
            "model_id": self.model_id,
            "metadata": dict(self.metadata)
        }


# 声明式内置适配器模板（仅作为初始已知元数据字典，非代码 if/else）
DECLARATIVE_BUILTIN_ADAPTERS: Dict[str, Dict[str, Any]] = {
    "codex": {
        "display_name": "OpenAI Codex",
        "file_capabilities": DEFAULT_FILE_CAPABILITIES,
        "wake_adapter": "cli",
        "project_skill_path": ".agentbus/workflows/codex/SKILL.md"
    },
    "workbuddy": {
        "display_name": "Tencent WorkBuddy",
        "file_capabilities": DEFAULT_FILE_CAPABILITIES,
        "wake_adapter": "manual",
        "project_skill_path": ".codebuddy/skills/agentbus-team/SKILL.md"
    },
    "trae": {
        "display_name": "ByteDance Trae",
        "file_capabilities": DEFAULT_FILE_CAPABILITIES,
        "wake_adapter": "manual",
        "project_skill_path": ".trae/skills/agentbus-team/SKILL.md"
    },
    "opencode": {
        "display_name": "OpenCode",
        "file_capabilities": DEFAULT_FILE_CAPABILITIES,
        "wake_adapter": "cli",
        "project_skill_path": ".opencode/skills/agentbus-team/SKILL.md"
    },
    "zcode": {
        "display_name": "ZCode",
        "file_capabilities": DEFAULT_FILE_CAPABILITIES,
        "wake_adapter": "manual",
        "project_skill_path": ".zcode/skills/agentbus-team/SKILL.md"
    },
    "antigravity": {
        "display_name": "Google Antigravity",
        "file_capabilities": DEFAULT_FILE_CAPABILITIES,
        "wake_adapter": "manual",
        "project_skill_path": ".agentbus/workflows/antigravity/SKILL.md"
    }
}


class MemberRegistry:
    """成员适配器动态注册表"""
    def __init__(self, include_builtin: bool = True):
        self._adapters: Dict[str, MemberAdapter] = {}
        # 预载入声明式适配器元数据
        for name, spec in (DECLARATIVE_BUILTIN_ADAPTERS.items() if include_builtin else []):
            self._adapters[name] = MemberAdapter(
                member_id=name,
                display_name=spec["display_name"],
                file_capabilities=spec["file_capabilities"],
                wake_adapter=spec["wake_adapter"],
                project_skill_path=spec["project_skill_path"]
            )

    def register(self, adapter: MemberAdapter, allow_overwrite: bool = False) -> None:
        """注册新成员适配器"""
        if not isinstance(adapter, MemberAdapter):
            raise MemberAdapterError('注册对象必须是合法成员适配器')
        if adapter.member_id in self._adapters and not allow_overwrite:
            raise MemberAdapterError(f"成员 {adapter.member_id!r} 已存在，不可重复注册覆盖")
        self._adapters[adapter.member_id] = adapter

    def get(self, member_id: str) -> Optional[MemberAdapter]:
        """获取成员适配器"""
        return self._adapters.get(member_id)

    def list_all(self) -> List[MemberAdapter]:
        """列出全部已注册适配器"""
        return list(self._adapters.values())

    def create_generic(self, member_id: str, display_name: str,
                       writable: bool = True, wake_adapter: str = "manual",
                       skill_path: Optional[str] = None,
                       host_version: Optional[str] = None,
                       model_id: Optional[str] = None) -> MemberAdapter:
        """快速构建并注册任意全新通用成员"""
        caps = DEFAULT_FILE_CAPABILITIES if writable else READONLY_FILE_CAPABILITIES
        adapter = MemberAdapter(
            member_id=member_id,
            display_name=display_name,
            file_capabilities=caps,
            wake_adapter=wake_adapter,
            project_skill_path=skill_path,
            host_version=host_version,
            model_id=model_id
        )
        self.register(adapter)
        return adapter


# 全局默认注册中心实例
GLOBAL_REGISTRY = MemberRegistry()
