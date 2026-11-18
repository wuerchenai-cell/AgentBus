#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.watchtower —— 唤醒者（灯塔）。

为什么必须有这个东西：
    2026-09-26 的三方协商里，Trae / ZCode / WorkBuddy 独立得出了**同一个结论**：
    「我没有后台循环，必须有人叫醒我。」

    也就是说——**消息格式再完美也没用，因为没人会去读它**。
    真正的缺口不是协议，是"唤醒者"。
    而且唤醒者**不能是它们三个中的任何一个**（三个都醒不来），
    所以只能是一个**不属于任何 agent 的普通常驻程序**：就是它。

它做什么：
    1. 事件驱动地盯着所有 agent 的信箱（内核事件，空闲零开销，不轮询）；
    2. 谁的箱子里有活，就**通知人**（控制台 + 可选 Windows 气泡）；
    3. 同时直接给出**"该对哪个 agent 说哪句话"**——复制粘贴就能把人叫醒。

它刻意不做的事：
    · 不读任务正文（那是 agent 的活）
    · 不替 agent 干活
    · 不做任何决策
    它只是个"门铃"。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .bus import IS_WINDOWS, Bus, Message, WinDirWatcher
from .config import Config
from .director import DIRECTOR_INBOX, HUMAN_INBOX
from .routing import resolve_message_result_to

WATCH_NAME = "watchtower"

_TOAST_PS = r"""
param([string]$TextFile, [string]$TitleFile)
try {
  Add-Type -AssemblyName System.Windows.Forms | Out-Null
  Add-Type -AssemblyName System.Drawing | Out-Null
  $text  = Get-Content -LiteralPath $TextFile  -Encoding UTF8 -Raw
  $title = Get-Content -LiteralPath $TitleFile -Encoding UTF8 -Raw
  $n = New-Object System.Windows.Forms.NotifyIcon
  $n.Icon = [System.Drawing.SystemIcons]::Information
  $n.BalloonTipTitle = $title
  $n.BalloonTipText  = $text
  $n.Visible = $true
  $n.ShowBalloonTip(15000)
  Start-Sleep -Seconds 6
  $n.Dispose()
} catch { }
"""


class Watchtower:
    def __init__(self, cfg: Config, bus: Bus | None = None):
        self.cfg = cfg
        self.bus = bus or Bus(cfg.workspace / ".agentbus")
        self.bus.init()
        self.tmp = cfg.workspace / ".agentbus" / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._warned_stale: set[str] = set()

    # ------------------------------------------------------------------ #
    def watched_agents(self) -> list[str]:
        """盯着所有 agent 的信箱；director 收结果、human 收待决事项也不例外。"""
        agents = list(self.cfg.enabled_workers().keys())
        for extra in (DIRECTOR_INBOX, HUMAN_INBOX):
            if (self.bus.agents_dir / extra).is_dir():
                agents.append(extra)
        # 去重且保序
        seen, out = set(), []
        for a in agents:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    def event_watchers(self) -> dict[str, WinDirWatcher | None]:
        """Reuse native directory events for mailboxes and durable progress.

        Source: external CLI checkpoint visibility regression, 2026-10-02.
        Ready changes deliver work; processing/jobs changes only update the
        user's observations. They never initiate a model or claim a task.
        """
        watchers: dict[str, WinDirWatcher | None] = {}
        try:
            for agent in self.watched_agents():
                watchers[agent] = self.bus.watcher(agent)
                key = 'processing:' + agent
                try:
                    watchers[key] = (WinDirWatcher(self.bus.dirs(agent)['processing'])
                                     if IS_WINDOWS else None)
                except OSError:
                    watchers[key] = None
            jobs = self.bus.root / 'jobs'
            jobs.mkdir(exist_ok=True)
            try:
                watchers['jobs'] = WinDirWatcher(jobs) if IS_WINDOWS else None
            except OSError:
                watchers['jobs'] = None
            return watchers
        except Exception:
            self.bus.close_watchers(watchers)
            raise

    def observation_signature(self) -> tuple:
        """Small file metadata snapshot, taken after directory events only."""
        folders = [('jobs', self.bus.root / 'jobs')]
        folders += [('processing:' + agent, self.bus.dirs(agent)['processing'])
                    for agent in self.watched_agents()]
        records = []
        for key, folder in folders:
            for path in sorted(folder.glob('*.json')):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # Atomic rename/finish can race this observation.
                records.append((key, path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(records)

    def scan(self) -> tuple[dict[str, list[Message]], list[tuple[str, str, str]]]:
        """返回 (可读消息, 坏件列表)。坏件必须报出来，不能静默丢弃。"""
        out: dict[str, list[Message]] = {}
        bad: list[tuple[str, str, str]] = []
        for a in self.watched_agents():
            msgs = []
            for p in self.bus.pending(a):
                try:
                    msgs.append(self.bus.read(p))
                except Exception as exc:  # noqa: BLE001
                    # 读不动的文件也要让人知道，否则任务会无声无息地消失
                    bad.append((a, p.name, str(exc)))
                    continue
            if msgs:
                out[a] = msgs
        return out, bad

    # ------------------------------------------------------------------ #
    def reply_box(self, message: Message) -> Path:
        """按这条消息自己的声明取结果信箱；不在这里推断协调者。"""
        decision = resolve_message_result_to(
            message, [*self.cfg.enabled_workers(), HUMAN_INBOX])
        if decision.recipient is None:
            raise ValueError(f"结果收件人未确定：{decision.error or decision.source}")
        return self.bus.dirs(decision.recipient)["ready"]

    def wake_line(self, agent: str, msgs: list[Message]) -> str:
        """给这个 agent 的"唤醒话术"：人只需要复制粘贴这一句。"""
        box = self.bus.dirs(agent)["ready"]
        titles = "；".join(m.title for m in msgs[:3])
        if len(msgs) > 3:
            titles += f" 等 {len(msgs)} 条"

        dispatch_msgs = [m for m in msgs if m.type in ("task", "goal", "review")]
        route_lines: list[str] = []
        for msg in dispatch_msgs:
            decision = resolve_message_result_to(
                msg, [*self.cfg.enabled_workers(), HUMAN_INBOX]
            )
            if decision.recipient is None:
                detail = f"（{decision.error}）" if decision.error else ""
                route_lines.append(
                    f"  {msg.id} 结果收件人未确定{detail}：暂停并询问用户，不要猜测收件人。"
                )
            else:
                recipient_box = self.bus.dirs(decision.recipient)["ready"]
                route_lines.append(
                    f"  {msg.id} 结果收件人：{decision.recipient}（{decision.source}）；"
                    f"结果消息投递到：{recipient_box}\\<新消息 ID>.json，"
                    f"reply_to={msg.id}"
                )
        route_text = "\n".join(route_lines)

        if agent in (DIRECTOR_INBOX, self.cfg.director):
            return (f"【汇总方有回执】{len(msgs)} 条 —— {titles}\n"
                    f"  读：{box}\n{route_text}\n"
                    f"  对汇总方（{agent}）说：\n"
                    f"    「去读 {box} 里所有 .json，把回执汇总成结论告诉我」")

        if agent == HUMAN_INBOX:
            return (f"【需要你决定】{len(msgs)} 条 —— {titles}\n"
                    f"  读：{box}\n{route_text}\n"
                    f"  （这里没有 agent 负责，需要你自己看并处置）")

        can_spawn = bool(self.cfg.data["workers"].get(agent, {}).get("can_spawn"))
        how = "可无头拉起" if can_spawn else "GUI，需人在界面里唤醒"
        return (f"【有活】{agent} 有 {len(msgs)} 条 —— {titles}   （{how}）\n"
                f"  任务在哪：{box}\n{route_text}\n"
                f"  对 {agent} 说这句：\n"
                f"    「去读 {box} 里发给你的 ready 消息，按 body 和 refs 完成；"
                "结果发给消息中声明的结果收件人，并用新消息 ID、reply_to=对应原任务 ID 关联。"
                f"结果不明确时先询问，不要猜」")

    # ------------------------------------------------------------------ #
    def notify(self, title: str, text: str, toast: bool = True) -> None:
        print("\a", end="", flush=True)  # 蜂鸣
        if not toast or os.name != "nt":
            return
        try:
            ps = self.tmp / "notify.ps1"
            if not ps.exists():
                ps.write_text(_TOAST_PS, encoding="utf-8")
            tf = self.tmp / "notify_text.txt"
            tf.write_text(text, encoding="utf-8")
            ttf = self.tmp / "notify_title.txt"
            ttf.write_text(title, encoding="utf-8")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(ps), str(tf), str(ttf)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass  # 通知失败不影响主功能

    # ------------------------------------------------------------------ #
    def report(self, pending: dict[str, list[Message]], toast: bool = True,
               bad: list[tuple[str, str, str]] | None = None) -> int:
        bad = bad or []
        total = sum(len(v) for v in pending.values())
        if not total and not bad:
            return 0
        print()
        print("=" * 72)
        print(f"[{time.strftime('%H:%M:%S')}] 有 {total} 条活等着被处理"
              + (f"，{len(bad)} 个文件读不动" if bad else ""))
        print("=" * 72)
        for agent, msgs in pending.items():
            print(self.wake_line(agent, msgs))
            print("-" * 72)
        for agent, fname, err in bad:
            print(f"⚠ 读不动的文件：{agent}/{fname}")
            print(f"   原因：{err}")
            print(f"   处理：把它改成合法 JSON，或直接让 {agent} 重写一份")
            print("-" * 72)
        brief = "；".join(f"{a}×{len(m)}" for a, m in pending.items()) or "无待办"
        if bad:
            brief += f"；读不动×{len(bad)}"
        self.notify("agentbus：有活要处理", brief, toast=toast)
        return total

    def _sweep_stale(self, agents: list[str]) -> list[str]:
        timeout_s = float(self.cfg.gates.get("heartbeat_timeout_s", 600))
        now = time.time()
        warnings: list[str] = []
        for agent in agents:
            for path in self.bus.processing(agent):
                age_s = now - path.stat().st_mtime
                if age_s > timeout_s and path.name not in self._warned_stale:
                    warning = (f"[watchtower] ⚠ agent {agent} 的 processing 消息 {path.name} "
                               f"已超时 {int(age_s)} 秒；只告警，不自动回收或重派，"
                               "请人工确认 agent 是否停止")
                    print(warning)
                    warnings.append(warning)
                    self._warned_stale.add(path.name)
        return warnings

    # ------------------------------------------------------------------ #
    def run(self, seconds: float = 0, toast: bool = True, once: bool = False,
            sweep_every: float = 60.0) -> int:
        agents = self.watched_agents()
        watchers: dict[str, Any] = {}
        if not once:
            for a in agents:
                watchers[a] = self.bus.watcher(a)
        event_driven = all(watchers.get(a) is not None for a in agents)
        total_reported = 0
        t0 = time.monotonic()
        last_sweep = 0.0
        try:
            # 先报一次现有存量
            pending, bad = self.scan()
            total_reported += self.report(pending, toast, bad)
            if once:
                return total_reported

            print(f"[watchtower] 已上线，盯着 {len(agents)} 个信箱：{'、'.join(agents)}")
            print("[watchtower] 事件驱动等待（空闲不轮询）；Ctrl+C 退出")
            while True:
                if seconds and (time.monotonic() - t0) > seconds:
                    print("[watchtower] 到达运行时限，退出")
                    break
                # CLI worker 超时可回收；GUI processing 只能告警，不能替人收回任务。
                if time.monotonic() - last_sweep > sweep_every:
                    self._sweep_stale(agents)
                    last_sweep = time.monotonic()

                who = self.bus.wait_for_change(agents, 1.0, watchers)
                if who is not None or not event_driven:
                    pending, bad = self.scan()
                    total_reported += self.report(pending, toast, bad)
        except KeyboardInterrupt:
            print("\n[watchtower] 收到中断，退出")
        finally:
            self.bus.close_watchers(watchers)
        return total_reported
