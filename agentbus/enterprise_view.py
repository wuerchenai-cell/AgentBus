# -*- coding: utf-8 -*-
"""enterprise_view：产品化工作台首页纯视图（workbench_view.build_overview 的超集包装）。

来源：Trae（平级成员），2026-10-05，实施任务 m-1791203447665-03fed1。
依据：IMPLEMENTATION_PLAN.md §3 接口 + DESIGN_RESOLUTIONS.md C1–C4（Codex 采用取舍）。
边界：
- 纯函数、零 I/O：不读文件、不 import Tk、不起进程/模型、不访问网络。
  config/health/jobs/capabilities 全部由调用方注入。
- C1：直接调用 agentbus.workbench_view.build_overview，保留其全部原返回键与取值，
  状态与主动作来自同一真源；本模块只在其上增加 goal/cards/responsibilities 等，不另创状态机。
- C2：goal 按 selected_job_id 或 created_ts/id 最新未 finalize 稳定选择；交付卡是
  "已验证任务交付"代理量；checkpoint 只在 caller 匹配行标作业来源；成员折叠区不展示技术字段。
- C3：只读成员是一等参与者，缺写工具不产生 blocked/failed；blocked 只来自真实任务/暂停/读取错误。
- C4：有界输入（作业数/公开消息估计）超限标 records_incomplete，不据此判空/离线。
"""
from __future__ import annotations

from agentbus import workbench_view

MAX_JOBS = 20
MAX_PUBLIC_MESSAGES = 2000

CARD_IDS = ("tasks", "deliverables", "ready", "blocked")
CARD_LABELS = {
    "tasks": "任务",
    "deliverables": "已验证任务交付",
    "ready": "待接手",
    "blocked": "阻塞",
}

STAGE_LABELS = {
    "queued": "排队",
    "working": "作业进行中",
    "waiting_handoff": "待人工接手",
    "completed": "已完成",
    "stopped": "已停止",
    "blocked": "阻塞",
    "awaiting_summary": "待验收",
}

GOAL_UNCONFIRMED = "目标未确认"

_MEMBER_NEXT_STEP = {
    "阻塞": "查看阻塞原因",
    "有任务待接手": "接手任务",
    "已认领，运行未确认": "回原会话报告",
    "暂停新派发": "等待恢复派发",
    "已停用": "启用或更换成员",
    "状态未知": "核对公开记录",
    "最近任务失败": "复核失败回执",
}


def _finite_str(value):
    return isinstance(value, str) and value.strip() != ""


def _job_key(job):
    ts = job.get("created_ts")
    if type(ts) not in (int, float):
        ts = 0.0
    return (float(ts), str(job.get("created_at") or ""), str(job.get("id") or ""))


def _is_finalized(job):
    fin = job.get("finalization")
    if isinstance(fin, dict) and fin.get("committed") is True:
        return True
    return job.get("state") == "completed"


def _job_tasks(job):
    tasks = job.get("tasks") if isinstance(job, dict) else None
    if not isinstance(tasks, list):
        return []
    return [item for item in tasks if isinstance(item, dict)]


def _is_done(task):
    return task.get("status") == "done" or task.get("location") == "done"


def _bound_jobs(jobs):
    """有界化输入；返回 (jobs, incomplete, reason)。非 list 原样透传由 base 容错。"""
    if not isinstance(jobs, list):
        return [], True, "公开作业记录缺失或无效"
    valid = [job for job in jobs if isinstance(job, dict)]
    incomplete = False
    reasons = []
    message_estimate = 0
    for job in valid:
        message_estimate += 1 + len(_job_tasks(job))
        cps = job.get("checkpoints")
        if isinstance(cps, list):
            message_estimate += len(cps)
    if message_estimate > MAX_PUBLIC_MESSAGES:
        incomplete = True
        reasons.append("公开消息约 %d 条超过上限 %d" % (message_estimate, MAX_PUBLIC_MESSAGES))
    bounded = valid
    if len(valid) > MAX_JOBS:
        bounded = sorted(valid, key=_job_key, reverse=True)[:MAX_JOBS]
        incomplete = True
        reasons.append("作业数 %d 超过上限 %d，仅展示最近 %d 个"
                       % (len(valid), MAX_JOBS, MAX_JOBS))
    return bounded, incomplete, "；".join(reasons)


def _select_job(config, jobs):
    selected_id = config.get("selected_job_id") if isinstance(config, dict) else None
    candidates = ([job for job in jobs
                   if isinstance(job, dict) and _finite_str(job.get("id"))]
                  if isinstance(jobs, list) else [])
    if _finite_str(selected_id):
        for job in candidates:
            if job.get("id") == selected_id:
                return job, {"selected_by": "explicit", "job_id": selected_id,
                             "created_at": job.get("created_at"),
                             "reason": "调用方指定 selected_job_id"}
        return None, {"selected_by": "explicit_missing", "job_id": selected_id,
                      "created_at": None, "reason": "指定作业不在有界公开记录中"}
    if not candidates:
        return None, {"selected_by": "none", "job_id": None, "created_at": None,
                      "reason": "无公开作业记录"}
    unfinalized = [job for job in candidates if not _is_finalized(job)]
    pool = unfinalized or candidates
    chosen = max(pool, key=_job_key)
    return chosen, {"selected_by": "latest_unfinalized" if unfinalized else "latest",
                    "job_id": chosen.get("id"), "created_at": chosen.get("created_at"),
                    "reason": "按 created_ts/id 确定性选择最新未 finalize 作业"}


def _cards(job):
    tasks = _job_tasks(job) if isinstance(job, dict) else []
    verified = bool(isinstance(job, dict) and job.get("artifacts_verified") is True)
    deliverable_required = [t for t in tasks if t.get("expected_artifacts")]
    done_deliverable = [t for t in deliverable_required if _is_done(t)]
    counts = {
        "tasks": len(tasks),
        "deliverables": len(done_deliverable) if verified else 0,
        "ready": sum(1 for t in tasks if t.get("location") == "ready"),
        "blocked": sum(1 for t in tasks
                       if t.get("location") == "blocked" or t.get("status") == "blocked"),
    }
    cards = [{"id": cid, "label": CARD_LABELS[cid], "count": counts[cid]} for cid in CARD_IDS]
    deliver = cards[1]
    deliver["pending_verify"] = bool(deliverable_required) and not verified
    deliver["proxy"] = "任务交付代理量（已验证任务数），非文件数"
    return cards


def _latest_checkpoint(job, member_id):
    if not isinstance(job, dict):
        return None
    cps = job.get("checkpoints")
    if not isinstance(cps, list):
        return None
    for item in reversed(cps):
        if isinstance(item, dict) and item.get("caller") == member_id:
            note = item.get("note")
            if _finite_str(note):
                return "%s（作业级续接记录 · %s · 来源 job %s）" % (
                    note.strip(), item.get("at") or "时间未记", job.get("id") or "未知")
    return None


def _member_task(job, member_id):
    tasks = [t for t in _job_tasks(job) if t.get("worker") == member_id]
    if not tasks:
        return None
    active = [t for t in tasks if not _is_done(t)]
    chosen = active[0] if active else tasks[-1]
    title = chosen.get("title") if _finite_str(chosen.get("title")) else chosen.get("id")
    return "%s（%s）" % (title, chosen.get("status") or chosen.get("location") or "未知")


def _write_capability(capabilities, member_id):
    entry = capabilities.get(member_id) if isinstance(capabilities, dict) else None
    if isinstance(entry, dict):
        if entry.get("readonly") is True:
            return "未验证（只读参与）"
        tools = entry.get("available_tools")
        if isinstance(tools, list) and tools and not any(
                isinstance(t, str) and ("write" in t.lower() or "edit" in t.lower())
                for t in tools):
            return "未验证（只读参与）"
    return "未验证"


def _responsibilities(base_members, job, capabilities):
    rows = []
    for item in base_members if isinstance(base_members, list) else []:
        mid = item.get("id")
        state = item.get("state_label")
        rows.append({
            "member_id": mid,
            "member": item.get("name"),
            "task": _member_task(job, mid),
            "checkpoint": _latest_checkpoint(job, mid),
            "state": state,
            "next_step": _MEMBER_NEXT_STEP.get(state, "无需动作"),
            "write_capability": _write_capability(capabilities, mid),
        })
    return rows


def _members_collapsed(base_members, coordinator):
    out = []
    for item in base_members if isinstance(base_members, list) else []:
        mid = item.get("id")
        out.append({
            "id": mid,
            "name": item.get("name"),
            "role": "负责人" if mid == coordinator else "成员",
            "state_label": item.get("state_label"),
            "observed_at": item.get("observed_at"),
            "evidence_count": len(item.get("evidence") or []),
        })
    return out


def build_project_view(config, health=None, jobs=None, capabilities=None):
    """workbench_view.build_overview 的超集：保留原键并新增目标/四卡/责任/成员折叠。

    纯函数、确定性、不抛异常；畸形输入降级为"目标未确认/状态未知"，不判空、不判离线。
    """
    jobs_bounded, incomplete, reason = _bound_jobs(jobs)
    if not isinstance(health, dict):
        incomplete = True
        reason = "；".join(filter(None, (reason, "成员健康记录缺失或无效")))
    base = workbench_view.build_overview(config, health, jobs_bounded)
    if incomplete and base.get("action_id") == "home":
        base = {
            **base,
            "headline": "任务记录需要检查",
            "next_action": "查看任务记录与诊断",
            "action_id": "tasks",
        }
    job, goal_source = _select_job(config, jobs_bounded)
    goal = GOAL_UNCONFIRMED
    if isinstance(job, dict) and _finite_str(job.get("goal")):
        goal = job["goal"].strip()
    stage = job.get("state") if isinstance(job, dict) else "unconfirmed"
    return {
        **base,
        "goal": goal,
        "goal_source": goal_source,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, "状态未确认"),
        "cards": _cards(job),
        "responsibilities": _responsibilities(base.get("members"), job, capabilities),
        "members_collapsed": _members_collapsed(base.get("members"), base.get("coordinator")),
        "records_incomplete": incomplete,
        "incomplete_reason": reason,
    }
