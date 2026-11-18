# -*- coding: utf-8 -*-
"""workbench_view：正式工作台首页纯函数摘要与证据状态。

来源：ZCode（平级成员），2026-10-05，任务 m-1791187019713-284e2c（job j-534407960b61037245f56fe225e91459）。
集成修正来源：Codex，2026-10-05；实际公开文件信箱计数、暂停/读失败及已启用成员校验。
ZCode 作者原件另行保留；本文件为集成副本。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：desktop-workbench-20261005/DESIGN.md 接口约定 + Codex 实施确认契约。

边界（写死，不可由调用方放宽）：
- 纯函数、零 I/O：不读文件、不 import Tk、不启动进程或模型、不访问网络；
  config/health/jobs 全部由调用方注入（health=onboarding.connection_summary 公开字典，
  config=cfg.data 加 workspace 字符串与 draft_dirty 布尔，jobs=list_jobs 摘要）。
- 不制造状态：health 存在、窗口/CLI 存在、配置已保存均不证明在线；
  认领只显示【已认领，运行未确认】，绝不输出执行中/在线/已连接；
  缺证默认【当前会话未确认】；空信箱不推导【等待中】。
- 输出纯 JSON 兼容数据，确定性输出（同输入同输出），不修改任何传入对象；
  畸形输入一律降级（未知字段/坏类型 → 状态未知或按缺失处理），不抛异常。
- 成员清单以 config.workers 为准；health 中不在 config 的名字不产生成员；
  name 缺失回退为 id。
- action_id 优先级：草稿/无团队 team > 暂停/读失败/阻塞 tasks > ready access > 已认领 tasks > 全空 home。
"""
from __future__ import annotations

NAV_IDS = ("home", "team", "tasks", "access", "settings", "logs")
ROUTE_LABEL = "文件协作"

ST_CONFIGURED = "已配置"
ST_DISABLED = "已停用"
ST_UNCONFIRMED = "当前会话未确认"
ST_READY = "有任务待接手"
ST_CLAIMED = "已认领，运行未确认"
ST_BLOCKED = "阻塞"
ST_UNKNOWN = "状态未知"
ST_PAUSED = "暂停新派发"
ST_RECENT_FAILURE = "最近任务失败"

_TONE = {
    ST_CONFIGURED: "neutral",
    ST_DISABLED: "neutral",
    ST_UNCONFIRMED: "info",
    ST_READY: "warn",
    ST_CLAIMED: "info",
    ST_BLOCKED: "warn",
    ST_UNKNOWN: "warn",
    ST_PAUSED: "warn",
    ST_RECENT_FAILURE: "warn",
}

_CLAIMED_STATUSES = ("claimed", "processing")
# The actual onboarding reader is bounded to 2,000 records. Keep this pure
# view independent of its producer while rejecting impossible/unbounded input.
_MAX_PUBLIC_COUNT = 1_000_000


def _is_finite_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _task_counts(jobs):
    """从 list_jobs 摘要容错提取每成员 claimed/blocked/ready 计数。"""
    counts = {}
    if not isinstance(jobs, list):
        return counts
    for job in jobs:
        if not isinstance(job, dict):
            continue
        tasks = job.get("tasks")
        if not isinstance(tasks, list):
            continue
        for item in tasks:
            if not isinstance(item, dict):
                continue
            worker = item.get("worker")
            status = item.get("status")
            if not _is_finite_str(worker):
                continue
            slot = counts.setdefault(worker, {"claimed": 0, "blocked": 0, "ready": 0})
            if status in _CLAIMED_STATUSES:
                slot["claimed"] += 1
            elif status == "blocked":
                slot["blocked"] += 1
            elif status == "pending":
                slot["ready"] += 1
    return counts


def _health_index(health):
    """返回 (observed_at, {name: entry})；health 畸形时返回 (None, {})。"""
    if not isinstance(health, dict):
        return None, {}
    observed = health.get("observed_at")
    if not isinstance(observed, str) or not observed.strip():
        observed = None
    entries = {}
    members = health.get("members")
    if isinstance(members, list):
        for item in members:
            if isinstance(item, dict) and _is_finite_str(item.get("name")):
                entries[item["name"]] = item
    return observed, entries


def _mailbox_state(entry):
    """Read connection_summary's public counts without inferring liveness.

    Counts are observations of files, not an execution or session signal.
    Invalid observations must not be interpreted as an empty mailbox.
    """
    counts = {"claimed": 0, "blocked": 0, "ready": 0}
    if not isinstance(entry, dict):
        return counts, "", False
    uncertain = False
    raw = entry.get("task_counts", {})
    if isinstance(raw, dict):
        for target, source in (("claimed", "processing"), ("blocked", "blocked"), ("ready", "ready")):
            value = raw.get(source, 0)
            if type(value) is int and 0 <= value <= _MAX_PUBLIC_COUNT:
                counts[target] = value
            else:
                uncertain = True
    else:
        uncertain = True
    unreadable = entry.get("unreadable_count", 0)
    if type(unreadable) is not int or unreadable < 0 or unreadable:
        uncertain = True
    indicator = entry.get("indicator")
    key = ""
    if isinstance(indicator, dict):
        proposed = indicator.get("key")
        if isinstance(proposed, str) and proposed in (
                "idle", "pending", "claimed", "blocked", "recent_failure", "paused", "attention"):
            key = proposed
            uncertain = uncertain or key == "attention"
        else:
            uncertain = True
    elif indicator is not None:
        uncertain = True
    return counts, key, uncertain


def build_overview(config: dict, health: dict | None = None, jobs: list | None = None) -> dict:
    """由公开配置、连接摘要与作业摘要构建首页纯数据视图。

    返回 {project_title, coordinator, member_count, route_label, headline,
          next_action, action_id, members}；members 每项
    {id, name, state_label, tone, evidence, observed_at}。
    """
    if not isinstance(config, dict):
        config = {}
    workers = config.get("workers")
    worker_items = ([(mid, item) for mid, item in workers.items() if _is_finite_str(mid)]
                    if isinstance(workers, dict) else [])

    counts = _task_counts(jobs)
    observed, hindex = _health_index(health)

    draft = config.get("draft_dirty") is True
    director = config.get("director")
    enabled_ids = {mid for mid, item in worker_items
                   if isinstance(item, dict) and item.get("enabled") is True}
    has_team = _is_finite_str(director) and director in enabled_ids
    mailbox_states = {mid: _mailbox_state(hindex.get(mid)) for mid, _ in worker_items}
    paused = ((isinstance(health, dict) and health.get("paused") is True)
              or any(mailbox_states[mid][1] == "paused" for mid in enabled_ids))
    global_unknown = isinstance(health, dict) and (
        health.get("truncated") is True
        or any(field in health and type(health[field]) is not bool for field in ("paused", "truncated")))

    members_out = []
    totals = {"claimed": 0, "blocked": 0, "ready": 0}
    uncertain_members = 0
    recent_failures = 0
    for mid, entry in worker_items:
        entry = entry if isinstance(entry, dict) else {}
        enabled = entry.get("enabled") is True
        raw_name = entry.get("display_name") or entry.get("name")
        name = raw_name if _is_finite_str(raw_name) else mid
        evidence = ["config.workers.%s.enabled=%s" % (mid, "true" if enabled else "false")]
        job_counts = counts.get(mid, {"claimed": 0, "blocked": 0, "ready": 0})
        hentry = hindex.get(mid)
        mailbox_counts, indicator, unknown = mailbox_states[mid]
        # The same tasks can appear in a job and its mailbox. A per-member
        # maximum retains the larger observation without double-counting it.
        c = {key: max(job_counts[key], mailbox_counts[key]) for key in totals}
        if hentry is not None:
            evidence.append("health.members[%s].present（记录存在不证明当前会话）" % mid)
            if _is_finite_str(hentry.get("last_receipt_at")):
                evidence.append("历史回执：health.members[%s].last_receipt_at=%s（不证明当前在线）"
                                % (mid, hentry["last_receipt_at"]))
            if any(mailbox_counts.values()):
                evidence.append("文件信箱: ready=%d / processing=%d / blocked=%d"
                                % (mailbox_counts["ready"], mailbox_counts["claimed"], mailbox_counts["blocked"]))
        else:
            evidence.append("health 无该成员记录（缺失即未知，presence 不证明在线）")
        if not enabled:
            state = ST_DISABLED
        else:
            totals["claimed"] += c["claimed"]
            totals["blocked"] += c["blocked"]
            totals["ready"] += c["ready"]
            if any(job_counts.values()):
                evidence.append("jobs: ready=%d / claimed=%d / blocked=%d（认领只记录投递事实，运行未确认）"
                                % (job_counts["ready"], job_counts["claimed"], job_counts["blocked"]))
            if paused:
                evidence.append("项目暂停新派发；在途工作是否仍在运行需另行确认")
                state = ST_PAUSED
            elif global_unknown or unknown:
                uncertain_members += 1
                evidence.append("记录无法读取、截断或状态无效；不据此判断信箱为空")
                state = ST_UNKNOWN
            elif c["blocked"]:
                state = ST_BLOCKED
            elif c["ready"]:
                state = ST_READY
            elif c["claimed"]:
                state = ST_CLAIMED
            elif indicator == "recent_failure":
                recent_failures += 1
                evidence.append("历史记录显示最近任务失败；不自动重派")
                state = ST_RECENT_FAILURE
            else:
                state = ST_UNCONFIRMED
        members_out.append({
            "id": mid,
            "name": name,
            "state_label": state,
            "tone": _TONE.get(state, "warn"),
            "evidence": evidence,
            "observed_at": observed if (hentry is not None and observed) else None,
        })

    title = config.get("project_title")
    if not _is_finite_str(title):
        workspace = config.get("workspace")
        if _is_finite_str(workspace):
            tail = workspace.replace("/", "\\").rstrip("\\").split("\\")[-1]
            title = tail if _is_finite_str(tail) else "未选择项目"
        else:
            title = "未选择项目"

    if draft:
        headline = "团队有未保存修改"
        next_action = "配置并保存团队"
        action_id = "team"
    elif not has_team:
        headline = "团队未配置"
        next_action = "配置并保存团队"
        action_id = "team"
    elif paused:
        headline = "项目已暂停新派发"
        next_action = "查看任务与暂停原因"
        action_id = "tasks"
    elif uncertain_members:
        headline = "任务记录需要检查"
        next_action = "查看任务记录与诊断"
        action_id = "tasks"
    elif totals["blocked"]:
        headline = "有 %d 项任务阻塞" % totals["blocked"]
        next_action = "查看阻塞任务"
        action_id = "tasks"
    elif totals["ready"]:
        headline = "有 %d 项任务待接手" % totals["ready"]
        next_action = "到接入页获取说明并接手"
        action_id = "access"
    elif totals["claimed"]:
        headline = "有 %d 项任务已认领，运行未确认" % totals["claimed"]
        next_action = "查看任务进度"
        action_id = "tasks"
    elif recent_failures:
        headline = "最近任务失败，需复核"
        next_action = "查看失败任务与回执"
        action_id = "tasks"
    else:
        headline = "团队已配置，暂无待办"
        next_action = "填写目标并复制任务指令"
        action_id = "home"

    return {
        "project_title": title,
        "coordinator": director if has_team else None,
        "member_count": len(enabled_ids),
        "route_label": ROUTE_LABEL,
        "headline": headline,
        "next_action": next_action,
        "action_id": action_id,
        "members": members_out,
    }
