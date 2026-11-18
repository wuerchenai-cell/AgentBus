#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.watchtower_gui —— 哨兵值守台（双击 exe 就是这个界面）。

设计目标就是用户说的那句：**"我打开就开始运行。"**
所以：打开即自动读取上次的工作空间 → 自动开始盯 → 有活立刻列出来，
每个条目旁边就有一个「复制唤醒话术」按钮，复制完直接粘给对应 agent 就行。

为什么需要这个界面（而不是纯命令行）：
    2026-09-26 三方协商的结论是「三家都没有后台循环，都必须被唤醒」。
    所以"人按一下"这一步在现阶段无法省略，那就必须把"按一下"的摩擦降到最低——
    低到只需要点一个按钮 + 粘一次。

它只做值守：盯信箱、报活、给话术、派活。不读任务正文、不替 agent 干活、不做决策。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__
from .bus import Bus
from .config import ConfigError, load
from .director import HUMAN_INBOX
from .routing import resolve_result_to_arg
from .watchtower import Watchtower

UI_STATE = Path.home() / ".agentbus_ui.json"


def _load_state() -> dict:
    try:
        return json.loads(UI_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        UI_STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass  # 资源清理类：UI 状态持久化失败不影响界面运行。


def _enable_dpi() -> None:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass  # 资源清理类：非 Windows 平台无 DPI API 时忽略。


class TowerApp:
    def __init__(self, root: tk.Tk, workspace: str | None = None):
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.wt: Watchtower | None = None
        self.wake_texts: dict[str, str] = {}

        root.title(f"Agent 哨兵 —— 三方互通值守台  v{__version__}")
        root.geometry("980x720")
        root.minsize(840, 600)

        st = _load_state()
        self.ws_var = tk.StringVar(value=workspace or st.get("workspace")
                                  or str(Path.cwd()))
        self.sender_var = tk.StringVar(value=st.get("sender", "workbuddy"))
        self.to_var = tk.StringVar(value="trae")
        self.result_to_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="未开始")
        self.busy_var = tk.StringVar(value="")

        self._build()
        # 打开就自动跑起来
        root.after(300, self._auto_start)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="工作空间").pack(side="left")
        ttk.Entry(top, textvariable=self.ws_var).pack(side="left", fill="x",
                                                     expand=True, padx=6)
        ttk.Button(top, text="选择…", command=self._pick).pack(side="left")
        self.btn_start = ttk.Button(top, text="开始盯着", command=self._toggle)
        self.btn_start.pack(side="left", padx=6)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10)
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        ttk.Label(bar, textvariable=self.busy_var, foreground="#b45309").pack(side="right")

        mid = ttk.LabelFrame(self.root, text=" 待处理（有活就出现在这里） ")
        mid.pack(fill="both", expand=True, **pad)
        self.list_canvas = tk.Canvas(mid, highlightthickness=0)
        self.list_inner = ttk.Frame(self.list_canvas)
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self._win = self.list_canvas.create_window((0, 0), window=self.list_inner,
                                                   anchor="nw")
        self.list_inner.bind(
            "<Configure>",
            lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind(
            "<Configure>",
            lambda e: self.list_canvas.itemconfig(self._win, width=e.width))
        self._render_empty()

        send = ttk.LabelFrame(self.root, text=" 派活（谁都能派，不限于主体） ")
        send.pack(fill="x", **pad)
        r1 = ttk.Frame(send)
        r1.pack(fill="x", padx=8, pady=6)
        ttk.Label(r1, text="从").pack(side="left")
        self.sender_box = ttk.Combobox(r1, textvariable=self.sender_var, width=12, values=[
            "workbuddy", "trae", "zcode", "codex"])
        self.sender_box.pack(side="left", padx=4)
        ttk.Label(r1, text="给").pack(side="left")
        self.to_box = ttk.Combobox(r1, textvariable=self.to_var, width=12, values=[
            "trae", "zcode", "workbuddy"])
        self.to_box.pack(side="left", padx=4)
        ttk.Label(r1, text="结果给").pack(side="left")
        self.result_to_box = ttk.Combobox(r1, textvariable=self.result_to_var,
                                          width=12, values=[])
        self.result_to_box.pack(side="left", padx=4)
        ttk.Button(r1, text="发出", command=self._post).pack(side="right")
        r2 = ttk.Frame(send)
        r2.pack(fill="x", padx=8, pady=(0, 8))
        self.intent = tk.Text(r2, height=3, wrap="word")
        self.intent.pack(fill="x")
        self.intent.insert("1.0", "在 成果/ 下创建 <文件名>，内容写三行：①你是谁 ②读到时间 "
                                  "③确认你没有后台循环、需要被唤醒。写完回报。")

        logf = ttk.LabelFrame(self.root, text=" 日志 ")
        logf.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(logf, height=8, wrap="word",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ #
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _render_empty(self) -> None:
        for w in self.list_inner.winfo_children():
            w.destroy()
        ttk.Label(self.list_inner, foreground="#6b7280",
                  text="（暂时没有待处理的活。agent 干完活、回执进来时，这里会自动刷新）"
                  ).pack(anchor="w", padx=10, pady=14)

    def _render_pending(self, pending: dict, bad: list) -> None:
        for w in self.list_inner.winfo_children():
            w.destroy()
        self.wake_texts.clear()
        if not pending and not bad:
            self._render_empty()
            return

        for agent, msgs in pending.items():
            box = ttk.Frame(self.list_inner, borderwidth=1, relief="solid")
            box.pack(fill="x", padx=8, pady=6)
            ttl = "；".join(m.title or m.body[:40] for m in msgs[:3])
            if len(msgs) > 3:
                ttl += f" 等 {len(msgs)} 条"
            head = ttk.Frame(box)
            head.pack(fill="x", padx=8, pady=(6, 2))
            ttk.Label(head, text=f"{agent}　{len(msgs)} 条",
                      font=("Segoe UI", 11, "bold")).pack(side="left")
            txt = self.wt.wake_line(agent, msgs) if self.wt else ""
            self.wake_texts[agent] = txt
            ttk.Button(head, text="复制唤醒话术",
                       command=lambda a=agent: self._copy(a)).pack(side="right")
            ttk.Label(box, text=ttl, wraplength=820, justify="left",
                      foreground="#374151").pack(anchor="w", padx=8, pady=(0, 8))

        for agent, fname, err in bad:
            box = ttk.Frame(self.list_inner, borderwidth=1, relief="solid")
            box.pack(fill="x", padx=8, pady=6)
            ttk.Label(box, text=f"⚠ 读不动的文件：{agent}/{fname}",
                      foreground="#b91c1c", font=("Segoe UI", 10, "bold")
                      ).pack(anchor="w", padx=8, pady=(6, 0))
            ttk.Label(box, text=err[:300], wraplength=820, justify="left",
                      foreground="#7f1d1d").pack(anchor="w", padx=8, pady=(0, 6))

    def _copy(self, agent: str) -> None:
        txt = self.wake_texts.get(agent, "")
        if not txt:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.root.update()
        self.busy_var.set(f"已复制 {agent} 的唤醒话术 —— 粘给它的对话框即可")
        self.root.after(6000, lambda: self.busy_var.set(""))
        self._log(f"[复制] {agent} 的唤醒话术已进剪贴板")

    # ------------------------------------------------------------------ #
    def _pick(self) -> None:
        d = filedialog.askdirectory(title="选一个工作空间")
        if d:
            self.ws_var.set(d)
            self._restart()

    def _auto_start(self) -> None:
        if not self.ws_var.get().strip():
            self.status_var.set("请先选一个工作空间")
            return
        self._start()

    def _toggle(self) -> None:
        if self.thread and self.thread.is_alive():
            self._stop()
        else:
            self._start()

    def _restart(self) -> None:
        self._stop()
        self.root.after(200, self._start)

    def _start(self) -> None:
        ws = Path(self.ws_var.get().strip())
        try:
            cfg = load(ws, create=False)
        except ConfigError as exc:
            if not ws.exists():
                messagebox.showerror("工作空间不存在", f"{ws}\n\n{exc}")
                return
            if not messagebox.askyesno("还没初始化",
                                       f"这个工作空间还没初始化。\n现在初始化吗？\n\n{ws}"):
                return
            cfg = load(ws, create=True)
            Bus(ws / ".agentbus").init()
        self.wt = Watchtower(cfg)
        participants = list(cfg.enabled_workers())
        self.sender_box.configure(values=participants)
        self.to_box.configure(values=[*participants, HUMAN_INBOX])
        self.result_to_box.configure(values=[*participants, HUMAN_INBOX])
        if self.sender_var.get() not in participants and participants:
            self.sender_var.set(participants[0])
        if self.to_var.get() not in participants and participants:
            self.to_var.set(participants[0])
        _save_state({"workspace": str(ws), "sender": self.sender_var.get()})
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.btn_start.configure(text="停止")
        self.status_var.set(f"● 正在盯着 {len(self.wt.watched_agents())} 个信箱"
                            f"（事件驱动，空闲不轮询）")
        self._log(f"[开始] 工作空间：{ws}")
        self._log(f"       盯着：{'、'.join(self.wt.watched_agents())}")

    def _stop(self) -> None:
        self.stop_flag.set()
        if self.wt:
            self.wt = None
        self.btn_start.configure(text="开始盯着")
        self.status_var.set("已停止")
        self._log("[停止] 值守已暂停")

    def _loop(self) -> None:
        """后台线程：事件驱动等待，变化就往队列里塞。"""
        wt = self.wt
        assert wt is not None
        agents = wt.watched_agents()
        watchers = {a: wt.bus.watcher(a) for a in agents}
        event_driven = all(watchers.get(a) is not None for a in agents)
        last_sweep = 0.0
        try:
            pending, bad = wt.scan()
            self.q.put(("data", pending, bad))
            while not self.stop_flag.is_set():
                now = time.monotonic()
                if now - last_sweep >= 60.0:
                    warnings = wt._sweep_stale(agents)
                    if warnings:
                        self.q.put(("stale", warnings))
                    last_sweep = now
                who = wt.bus.wait_for_change(agents, 1.0, watchers)
                if who is not None or not event_driven:
                    pending, bad = wt.scan()
                    self.q.put(("data", pending, bad))
                    total = sum(len(v) for v in pending.values())
                    if total:
                        wt.notify("agentbus：有活要处理",
                                  "；".join(f"{a}×{len(m)}" for a, m in pending.items()))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("error", str(exc)))
        finally:
            wt.bus.close_watchers(watchers)

    def _poll(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "data":
                    _, pending, bad = item
                    self._render_pending(pending, bad)
                    total = sum(len(v) for v in pending.values())
                    self.busy_var.set(f"当前有 {total} 条待处理"
                                      + (f"，{len(bad)} 个坏件" if bad else "")
                                      if total or bad else "")
                elif kind == "error":
                    self._log(f"[错误] {item[1]}")
                    messagebox.showerror("值守出错", item[1])
                elif kind == "stale":
                    for warning in item[1]:
                        self._log(warning)
                    self.busy_var.set("有 processing 任务超时，请人工确认 agent 是否停止")
        except queue.Empty:
            pass  # 资源清理类：队列空为正常轮询间隙。
        self.root.after(400, self._poll)

    # ------------------------------------------------------------------ #
    def _post(self) -> None:
        if self.wt is None:
            messagebox.showinfo("先开始盯着", "请先点「开始盯着」")
            return
        intent = self.intent.get("1.0", "end").strip()
        if len(intent) < 2:
            return
        bus = self.wt.bus
        frm, to = self.sender_var.get().strip(), self.to_var.get().strip()
        participants = [*self.wt.cfg.enabled_workers(), HUMAN_INBOX]
        lookup = {name.casefold(): name for name in participants}
        frm = lookup.get(frm.casefold())
        to = lookup.get(to.casefold())
        if frm is None or to is None:
            messagebox.showwarning("参与者不可用", "发送者和收件人必须是已启用参与者或 human。")
            return
        if to != HUMAN_INBOX and not self.wt.cfg.worker(to).get("executor_enabled", True):
            messagebox.showwarning("执行者未启用", f"{to} 未启用为执行者，不能接收任务。")
            return
        result_value = self.result_to_var.get().strip()
        if not result_value:
            messagebox.showwarning("缺结果收件人", "请明确选择任务完成后的结果收件人。")
            return
        try:
            result_to = resolve_result_to_arg(
                result_value, sender=frm, director=self.wt.cfg.director,
                participants=participants)
        except ValueError as exc:
            messagebox.showwarning("结果收件人不可用", str(exc))
            return
        msg = bus.make(sender=frm, to=to, type="task", title=intent[:60],
                       body=intent, result_to=result_to)
        bus.post(msg)
        self._log(f"[派活] {frm} → {to}　{msg.id}")
        self._log(f"       {intent[:80]}")
        self.busy_var.set(f"已派给 {to}")
        self.root.after(4000, lambda: self.busy_var.set(""))

    def _on_close(self) -> None:
        self.stop_flag.set()
        _save_state({"workspace": self.ws_var.get().strip(),
                     "sender": self.sender_var.get()})
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="agent-watchtower")
    ap.add_argument("--workspace", default=None)
    args = ap.parse_args(argv or [])

    _enable_dpi()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:  # noqa: BLE001
        pass  # 资源清理类：vista 主题不可用时沿用默认。
    app = TowerApp(root, workspace=args.workspace)
    root.after(400, app._poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
