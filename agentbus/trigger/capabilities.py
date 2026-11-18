# -*- coding: utf-8 -*-
"""capabilities.py: 能力推导、三态证据体系与对外等级评估实现。"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class CapabilityTier(str, enum.Enum):
    FILE_ONLY = "file_only"
    BINDING_ONLY = "binding_only"
    SINGLE_SEND_VERIFIED = "single_send_verified"
    AUTOMATIC_LIMITED = "automatic_limited"
    AUTOMATIC_EXACT = "automatic_exact"


CANONICAL_CAPABILITIES = [
    "can_identify_target",
    "can_check_project",
    "can_submit_existing",
    "can_route_exact_id",
    "can_detect_busy",
    "host_queue_verified",
    "atomic_target_submit",
    "can_report_native_start",
    "can_report_native_finish",
    "can_reconcile",
    "can_cancel_owned_turn",
    "cold_start_verified",
    "requires_visible_target",
    "requires_manual_bootstrap",
]


@dataclass
class CapabilityEvidence:
    """某项能力的具体实测证据记录。"""
    name: str
    value: bool
    basis: str = "tested"  # declared / static / runtime / tested
    host_version: str = "1.0.0"
    connector_version: str = "1.0.0"
    tested_at: Optional[str] = None
    scope: Optional[str] = None
    evidence_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CapabilityEvidence":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class EffectiveCapabilities:
    """推导后的有效能力集合与对外等级。"""
    overall_tier: str = CapabilityTier.FILE_ONLY.value
    capabilities: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def is_capable(self, name: str) -> bool:
        cap = self.capabilities.get(name, {})
        # Only true if value is True and basis is not stale_version
        return cap.get("value") is True and cap.get("basis") in {"tested", "runtime"}

    def get_basis(self, name: str) -> str:
        return self.capabilities.get(name, {}).get("basis", "unknown")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def derive_capabilities(
    declared: Dict[str, bool],
    runtime_host_version: str,
    connector_version: str,
    evidence_list: List[CapabilityEvidence],
) -> EffectiveCapabilities:
    """根据清单声明、当前运行时版本与实测证据推导有效能力与安全分级。"""
    eff_caps: Dict[str, Dict[str, Any]] = {}

    evidence_by_name = {ev.name: ev for ev in evidence_list}

    for name in CANONICAL_CAPABILITIES:
        dec_val = declared.get(name, False)
        ev = evidence_by_name.get(name)

        if ev is None:
            # Only declaration exists
            eff_caps[name] = {
                "value": dec_val,
                "basis": "declared" if name in declared else "unknown",
                "evidence_ref": None,
            }
        else:
            # Check version drift
            if ev.host_version != runtime_host_version:
                eff_caps[name] = {
                    "value": False,
                    "basis": "stale_version",
                    "reason": f"Tested on host {ev.host_version}, currently running {runtime_host_version}",
                    "evidence_ref": ev.evidence_ref,
                }
            else:
                eff_caps[name] = {
                    "value": ev.value,
                    "basis": ev.basis,
                    "evidence_ref": ev.evidence_ref,
                }

    # Evaluate overall tier based on strict tested/runtime capabilities
    def _is_tested_true(c_name: str) -> bool:
        info = eff_caps.get(c_name, {})
        return info.get("value") is True and info.get("basis") in {"tested", "runtime"}

    # Tier rules:
    if not _is_tested_true("can_identify_target"):
        overall_tier = CapabilityTier.FILE_ONLY.value
    elif not _is_tested_true("can_check_project") or not _is_tested_true("can_submit_existing"):
        overall_tier = CapabilityTier.BINDING_ONLY.value
    elif not (
        _is_tested_true("can_route_exact_id")
        and _is_tested_true("can_detect_busy")
        and _is_tested_true("atomic_target_submit")
    ):
        overall_tier = CapabilityTier.SINGLE_SEND_VERIFIED.value
    elif not (
        _is_tested_true("host_queue_verified")
        and _is_tested_true("can_reconcile")
    ):
        overall_tier = CapabilityTier.AUTOMATIC_LIMITED.value
    else:
        overall_tier = CapabilityTier.AUTOMATIC_EXACT.value

    return EffectiveCapabilities(
        overall_tier=overall_tier,
        capabilities=eff_caps,
    )
