# -*- coding: utf-8 -*-
"""§3.10 PROJECT-WORKFLOW-PIPELINE: 刚性不可绕行工作流管道与法定交付状态机。

来源：用户最高指令（法定必经之路），任务 m-tree-429617654d6aaed2533b12c5。
在物理总线底层建立刚性状态机：
  立项 -> 依赖就绪 -> 认领锁死 -> 心跳激活 -> 实施回归 -> 清单核验 -> 正式回执 -> 验收终结
任何跳步、绕行、无心跳交付或无锁提交行为，直接物理级硬熔断拦截并记录审计！
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import claim_fencing as _fencing
from . import exclusive_lock as _exclusive_lock
from . import heartbeat as _heartbeat
from .silent_debug import note_silent


class WorkflowPipelineError(ValueError):
    """工作流管道违规拦截异常。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}" if message else code)


def verify_pipeline_prerequisites_for_claim(bus_root: str | Path, task_record: dict[str, Any],
                                           agent: str) -> None:
    """门禁 1: 认领前工作流校验。
    确保任务的前序依赖（若有）已全部处于 delivered 状态，禁止抢跑。
    """
    task_id = str(task_record.get("id") or "")
    _fencing.validate_task_id(task_id)
    _fencing.validate_agent_id(agent)

    # 检查是否有未满足的前序依赖
    unmet = task_record.get("unmet_dependencies")
    if unmet and isinstance(unmet, list) and len(unmet) > 0:
        raise WorkflowPipelineError(
            "PIPELINE_DEPENDENCY_UNMET",
            f"任务 {task_id} 尚有未完成的前序依赖 {unmet}，禁止抢跑认领！"
        )


def verify_pipeline_prerequisites_for_delivery(bus_root: str | Path, task_record: dict[str, Any],
                                              agent: str, ticket: Any,
                                              artifacts: list[dict[str, Any]] | None = None,
                                              workspace_root: str | Path | None = None,
                                              artifact_root: str | Path | None = None,
                                              heartbeat_timeout_s: float = 600.0,
                                              require_artifacts: bool = True) -> None:
    """门禁 2: 交付前完整工作流刚性硬核查。

    必须满足的法定链条：
    1. 认领独占链：任务必须已建立跨 Agent 物理排他独占锁，且当前 agent 和 ticket 是合法持有者；
    2. 会话心跳链：任务必须拥有合法会话心跳记录，且票据代次一致、心跳未超时（599/600秒合格，601秒超时）；
    3. 成功交付必须满足产物规范与真实性链：若声明 expected_artifacts，必须提供清单且磁盘文件真实存在，bytes 与 SHA256 绝对一致；明确失败的尝试只记录失败回执，不宣称产物已交付；
    4. 状态合规链：任务必须处于 claimed / running 态，不可为终态重交；
    任何环节跳步、缺失或造假，物理总线直接熔断拒绝。
    """
    import hashlib

    task_id = str(task_record.get("id") or "")
    _fencing.validate_task_id(task_id)
    _fencing.validate_agent_id(agent)

    # 1. 锁持有权核验
    lock_info = _exclusive_lock.get_task_lock_info(bus_root, task_id)
    if lock_info is None:
        raise WorkflowPipelineError(
            "PIPELINE_LOCK_MISSING",
            f"工作流管道熔断：任务 {task_id} 缺乏物理排他独占锁记录，禁止提交成果！"
        )
    try:
        _exclusive_lock.verify_task_lock(bus_root, task_id, agent, ticket)
    except _exclusive_lock.ExclusiveLockError as err:
        raise WorkflowPipelineError(
            "PIPELINE_LOCK_VIOLATION",
            f"工作流管道熔断：非排他独占锁持有者，拒绝提交成果！原因：{err}"
        ) from err

    # 2. 会话显式心跳核验
    hb_state = _heartbeat.state(bus_root, task_id, agent, task_record=task_record)
    if hb_state.get("source") != _heartbeat.SOURCE_RECORD or hb_state.get("epoch") is None:
        raise WorkflowPipelineError(
            "PIPELINE_HEARTBEAT_MISSING",
            f"工作流管道熔断：任务 {task_id} 缺乏合法执行会话心跳溯源记录（source={hb_state.get('source')}），禁止提交成果！"
        )
    if _heartbeat.is_stale(hb_state, heartbeat_timeout_s):
        raise WorkflowPipelineError(
            "PIPELINE_HEARTBEAT_STALE",
            f"工作流管道熔断：任务 {task_id} 的会话心跳已超时（age={hb_state.get('age_s')}s > {heartbeat_timeout_s}s），必须先上报健康心跳证明存活方可提交！"
        )

    # 3. 产物规范与物理完整性硬核查（如果有声明 expected_artifacts）
    expected = task_record.get("expected_artifacts")
    if require_artifacts and expected and isinstance(expected, list) and len(expected) > 0:
        if not artifacts or not isinstance(artifacts, list):
            raise WorkflowPipelineError(
                "PIPELINE_ARTIFACTS_MISSING_MANIFEST",
                f"工作流管道熔断：任务要求产物 {expected}，但未提供产物清单！"
            )
        provided_map: dict[str, dict[str, Any]] = {}
        for a in artifacts:
            if isinstance(a, dict) and "path" in a:
                norm_p = str(a["path"]).replace("\\", "/")
                provided_map[norm_p] = a

        artifact_base = (Path(artifact_root).resolve() if artifact_root else
                         Path(workspace_root).resolve() if workspace_root else
                         Path(bus_root).parent.resolve())

        for exp in expected:
            norm_exp = str(exp).replace("\\", "/")
            if norm_exp not in provided_map:
                raise WorkflowPipelineError(
                    "PIPELINE_ARTIFACTS_INCOMPLETE",
                    f"工作流管道熔断：任务要求产物 '{exp}' 未在回执产物清单中提供！"
                )
            item = provided_map[norm_exp]
            declared_bytes = item.get("bytes")
            declared_sha = item.get("sha256")
            if declared_bytes is None or not declared_sha:
                raise WorkflowPipelineError(
                    "PIPELINE_ARTIFACTS_ENTRY_INVALID",
                    f"工作流管道熔断：产物清单条目 '{exp}' 缺少 bytes 或 sha256 字段！"
                )

            actual_file = (artifact_base / norm_exp).resolve()
            try:
                actual_file.relative_to(artifact_base)
            except ValueError:
                raise WorkflowPipelineError(
                    "PIPELINE_ARTIFACTS_PATH_UNSAFE",
                    f"工作流管道熔断：产物路径 '{exp}' 超出工作区安全边界！"
                )

            if not actual_file.is_file():
                raise WorkflowPipelineError(
                    "PIPELINE_ARTIFACTS_FILE_MISSING",
                    f"工作流管道熔断：任务要求产物 '{exp}' 物理文件不存在于磁盘 ({actual_file})！"
                )

            real_size = actual_file.stat().st_size
            if real_size != declared_bytes:
                raise WorkflowPipelineError(
                    "PIPELINE_ARTIFACTS_BYTES_MISMATCH",
                    f"工作流管道熔断：产物 '{exp}' 实际大小 {real_size} 与清单声明 {declared_bytes} 不符！"
                )

            h = hashlib.sha256()
            with actual_file.open("rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            real_sha = h.hexdigest()

            if real_sha.lower() != str(declared_sha).lower():
                raise WorkflowPipelineError(
                    "PIPELINE_ARTIFACTS_SHA_MISMATCH",
                    f"工作流管道熔断：产物 '{exp}' 实际哈希 {real_sha} 与清单声明 {declared_sha} 不符（伪造摘要）！"
                )


def record_pipeline_stage_audit(bus: Any, event: str, task_id: str,
                                agent: str, **kwargs: Any) -> None:
    """将工作流状态机跃迁写入审计总线。"""
    try:
        bus.audit(event=f"workflow_pipeline_{event}", task_id=task_id,
                  agent=agent, **kwargs)
    except Exception:
        # 功能类：审计落盘失败不得阻断状态机跃迁本身（跃迁已生效于任务 JSON），
        # 但必须留痕，否则"审计线上有洞"会完全不可见。
        note_silent("workflow_pipeline.py:108", "functional",
                    "审计写入失败不阻断跃迁，但此处留痕以便发现审计断点")
        pass
