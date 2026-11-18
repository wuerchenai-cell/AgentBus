#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.gates —— 闸门。

没有闸门的多 agent 系统，最典型的死法是「两个 agent 互相派活，谁也停不下来」。
这里把四道闸门放在派活的必经之路上：
  1. 跳数上限  —— 一条任务链最多转手几次
  2. 重试上限  —— 单个任务最多重试几次
  3. 预算上限  —— 单个任务 / 整个目标的墙钟预算
  4. 任务总量  —— 一次目标最多拆出几个任务
每一次拒绝都会写审计，并把消息升级成 escalation（需要人来决定）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .bus import Message


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    escalate: bool = False      # 拒绝时是否升级给人类
    limit: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class GateKeeper:
    def __init__(self, gates: dict[str, Any]):
        self.max_hops = int(gates.get("max_hops", 4))
        self.max_retries = int(gates.get("max_retries", 2))
        self.max_tasks = int(gates.get("max_tasks", 20))
        self.task_timeout_s = int(gates.get("task_timeout_s", 1800))
        self.heartbeat_timeout_s = int(gates.get("heartbeat_timeout_s", 600))
        self.total_budget_s = int(gates.get("total_budget_s", 7200))
        self._started = time.monotonic()

    # ---- 逐条检查 ----
    def check_hop(self, msg: Message) -> Decision:
        if msg.hop > self.max_hops:
            return Decision(False, f"跳数 {msg.hop} 超过上限 {self.max_hops}",
                            escalate=True, limit="max_hops")
        return Decision(True)

    def check_retry(self, msg: Message) -> Decision:
        if msg.attempt > self.max_retries:
            return Decision(False, f"重试 {msg.attempt} 次超过上限 {self.max_retries}",
                            escalate=True, limit="max_retries")
        return Decision(True)

    def check_task_budget(self, msg: Message) -> Decision:
        if msg.deadline_ts and time.time() > msg.deadline_ts:
            return Decision(False, "该任务已超过自己的时间预算", escalate=True,
                            limit="task_deadline")
        return Decision(True)

    def check_total_budget(self) -> Decision:
        used = time.monotonic() - self._started
        if self.total_budget_s <= 0 or used > self.total_budget_s:
            return Decision(False, f"整个目标已用 {used:.0f}s，"
                                   f"超过总预算 {self.total_budget_s}s",
                            escalate=True, limit="total_budget")
        return Decision(True)

    def check_task_count(self, count: int) -> Decision:
        if count > self.max_tasks:
            return Decision(False, f"任务数 {count} 超过上限 {self.max_tasks}",
                            escalate=True, limit="max_tasks")
        return Decision(True)

    # ---- 组合 ----
    def check_dispatch(self, msg: Message, task_count: int = 0,
                       include_total_budget: bool = True) -> Decision:
        """派活前的总检查。任何一道不过就直接拒绝。"""
        checks = [self.check_hop(msg), self.check_retry(msg), self.check_task_budget(msg)]
        if include_total_budget:
            checks.insert(0, self.check_total_budget())
        for dec in checks:
            if not dec:
                return dec
        if task_count:
            return self.check_task_count(task_count)
        return Decision(True)

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def heartbeat_timeout(self) -> float:
        return float(self.heartbeat_timeout_s)
