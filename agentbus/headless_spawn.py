"""Headless Spawn Engine for AgentBus.

Orchestrates headless subprocess execution for CLI/SDK-capable agents,
turning adaptive task pipeline schedules into true autonomous workflows.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .silent_debug import note_silent
from .task_tree import TaskForest
from . import claim_fencing
from .bus import BusError

logger = logging.getLogger("agentbus.headless_spawn")


@dataclass
class SpawnResult:
    agent: str
    leaf_id: str
    task_id: str
    cmd: List[str]
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    log_path: str = ""
    started: bool = False
    success: bool = False
    error: str = ""
    process_success: bool = False
    phase: str = "preflight"


class HeadlessSpawnEngine:
    """Engine that automatically spawns headless agent processes to execute claimed tasks."""

    def __init__(self, cfg: Config, default_timeout_s: float = 300.0):
        self.cfg = cfg
        self.default_timeout_s = default_timeout_s
        self.forest = TaskForest(cfg)
        self.logs_dir = self.cfg.workspace / ".agentbus" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.active_processes: Dict[str, Dict[str, Any]] = {}
        self._active_lock = threading.RLock()
        self._tickets = {}

    def is_spawnable(self, agent: str) -> bool:
        """Check whether an agent is configured and allowed to be spawned."""
        worker = self.cfg.data.get("workers", {}).get(agent, {})
        return bool(worker.get("enabled", True) and worker.get("can_spawn", False) and worker.get("executor_enabled", True))

    def build_spawn_command(self, agent: str, leaf: Dict[str, Any], task_id: str) -> List[str]:
        """Construct the execution command line for the given agent and task."""
        worker = self.cfg.data.get("workers", {}).get(agent, {})
        ws = str(self.cfg.workspace)

        # Check explicit command list
        if "command" in worker and isinstance(worker["command"], list) and worker["command"]:
            base_cmd = list(worker["command"])
            return base_cmd + ["--workspace", ws, "--task-id", task_id]

        # Check explicit binary path
        if "path" in worker and worker["path"]:
            base_bin = str(worker["path"])
            return [base_bin, "exec", "--workspace", ws, "--task-id", task_id]

        # Check frozen mode
        workflow_file = self.cfg.workspace / ".agentbus" / "workflow.json"
        if workflow_file.exists():
            try:
                wf_data = json.loads(workflow_file.read_text(encoding="utf-8"))
                launcher = wf_data.get("launcher", {})
                if launcher.get("mode") == "frozen" and launcher.get("executable"):
                    exe_path = launcher["executable"]
                    if Path(exe_path).exists():
                        return [
                            str(exe_path),
                            "worker",
                            "--workspace",
                            ws,
                            "--agent",
                            agent,
                            "--max-tasks",
                            "1",
                        ]
            except Exception as exc:
                logger.warning("Registered frozen launcher could not be read; using the configured Python worker fallback: %s", exc)
                note_silent("headless_spawn.py:91", "functional", f"冻结入口配置不可读，回退到当前 Python worker：{exc}")
                pass  # 功能类：显式登记入口不可用时使用当前源码 worker，日志记录了回退原因。

        # Fallback to internal worker loop runner via current Python interpreter
        py_bin = sys.executable
        return [
            py_bin,
            "-m",
            "agentbus.cli",
            "worker",
            "--workspace",
            ws,
            "--agent",
            agent,
            "--max-tasks",
            "1",
        ]

    def _kill_process_tree(self, pid: int) -> None:
        """Reliably terminate a process and all its descendants to prevent orphan leaks."""
        if not pid or pid <= 0:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5.0,
                )
            else:
                import signal
                os.killpg(pid, signal.SIGKILL)
        except Exception as exc:
            logger.debug("Failed to kill process tree for PID %d: %s", pid, exc)

    @staticmethod
    def _windows_job():
        """Use the same proven suspended-root job supervisor as frozen smoke."""
        if sys.platform != "win32":
            return None
        from tools.smoke_frozen import _WindowsJob
        return _WindowsJob()

    def _stop_process(self, info):
        proc = info.get("proc")
        self._kill_process_tree(info.get("pid"))
        job = info.pop("job", None)
        if job:
            job.close()
        if proc and proc.poll() is None:
            proc.kill()
        if proc:
            proc.wait(timeout=5)

    def _monitor_process(self, task_id, info, result):
        proc = info["proc"]
        error = ""
        try:
            while proc.poll() is None:
                if info["cancel"].is_set() or (self.cfg.workspace / ".agentbus" / "STOP").exists():
                    error = "STOP_REQUESTED: supervised process stopped"
                    break
                if time.monotonic() >= info["expires"]:
                    error = "TIMEOUT: supervised process exhausted its time budget"
                    break
                current = self.forest.bus._active_record(info["agent"], task_id)
                if current is None:
                    # A normal receipt moves the task out of processing. The
                    # command may still be flushing logs before exiting.
                    if not self._delivery_verified(info["leaf_id"], task_id, info["ticket"]):
                        error = "STALE_TICKET: task is no longer active for this process"
                        break
                else:
                    self.forest.bus.check_claim(info["agent"], current, info["ticket"])
                info["cancel"].wait(.05)
            if error:
                self._stop_process(info)
                result.exit_code = -1
            else:
                result.exit_code = proc.wait(timeout=1)
                # Closing the job/group also reaps children on normal parent exit.
                self._stop_process(info)
            result.process_success = result.exit_code == 0
            result.success = result.process_success and self._delivery_verified(info["leaf_id"], task_id, info["ticket"])
            result.error = error or ("" if result.success else
                "DELIVERY_UNVERIFIED: process exited successfully without a verified task receipt" if result.process_success else
                f"Process exited with code {result.exit_code}")
        except (OSError, ValueError, BusError, claim_fencing.TicketError, subprocess.SubprocessError) as exc:
            result.error = f"SUPERVISION_FAILED: {exc}"
            result.success = False
            try:
                self._stop_process(info)
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                logger.error("Supervised process cleanup failed: %s", cleanup_error)
        finally:
            result.phase = "finished"
            result.duration_s = round(time.monotonic() - info["start_monotonic"], 3)
            with self._active_lock:
                if self.active_processes.get(task_id) is info:
                    self.active_processes.pop(task_id)

    def _delivery_verified(self, leaf_id, task_id, ticket):
        leaf = self.forest._leaf(self.forest.snapshot(), leaf_id)
        receipt = self.forest.bus.find_message(leaf.get("result_id")) if leaf.get("result_id") else None
        return (leaf.get("task_id") == task_id and leaf.get("state") == "delivered"
                and leaf.get("receipt_state") == "valid"
                and leaf.get("artifact_state") in ("verified", "superseded", "not_required")
                and not leaf.get("evidence_error") and receipt is not None
                and receipt.source_claim_epoch == ticket.claim_epoch
                and receipt.source_claim_id == ticket.claim_id
                and receipt.source_conversation_id == ticket.conversation_id)

    def spawn_leaf(
        self,
        leaf: Dict[str, Any],
        agent: str,
        timeout_s: Optional[float] = None,
        wait: bool = True,
    ) -> SpawnResult:
        """Claim (if necessary) and execute a leaf task via headless subprocess."""
        leaf_id = leaf.get("id", "")
        task_id = leaf.get("task_id", "")
        timeout = float(self.default_timeout_s if timeout_s is None else timeout_s)

        # §13.4: Strict adapter & permission gate before claim/Popen
        if not self.is_spawnable(agent):
            logger.warning("Agent '%s' is not spawnable or disabled. Refusing spawn.", agent)
            return SpawnResult(
                agent=agent,
                leaf_id=leaf_id,
                task_id=task_id,
                cmd=[],
                started=False,
                success=False,
                error=f"ADAPTER_GATE_REFUSAL: agent '{agent}' has can_spawn=false or is disabled",
            )

        # §13.3: Pre-check STOP file before spawn
        stop_file = self.cfg.workspace / ".agentbus" / "STOP"
        if stop_file.exists():
            logger.warning("STOP file detected before spawning %s on leaf %s. Aborting.", agent, leaf_id)
            return SpawnResult(
                agent=agent,
                leaf_id=leaf_id,
                task_id=task_id,
                cmd=[],
                started=False,
                success=False,
                error="STOP_REQUESTED: STOP file detected before process spawn",
            )

        # §13.3: Pre-check deadline
        deadline_ts = leaf.get("deadline_ts")
        if deadline_ts and time.time() > float(deadline_ts):
            logger.warning("Deadline exceeded before spawning %s on leaf %s. Aborting.", agent, leaf_id)
            return SpawnResult(
                agent=agent,
                leaf_id=leaf_id,
                task_id=task_id,
                cmd=[],
                started=False,
                success=False,
                error="DEADLINE_EXCEEDED: task deadline exceeded before process spawn",
            )

        worker = self.cfg.data.get("workers", {}).get(agent, {})
        command = worker.get("command")
        if (not (isinstance(command, list) and command and all(isinstance(arg, str) and arg for arg in command))
                and not worker.get("path")) or worker.get("communication_mode") == "file":
            return SpawnResult(agent, leaf_id, task_id, [], error=
                "ADAPTER_COMMAND_REQUIRED: configure an accepted explicit execution adapter or use the file handoff route")
        if not math.isfinite(timeout) or timeout <= 0:
            return SpawnResult(agent, leaf_id, task_id, [], error="INVALID_TIMEOUT: execution budget must be positive and finite")

        try:
            current_leaf = self.forest._leaf(self.forest.snapshot(), leaf_id)
            if task_id and current_leaf.get("task_id") != task_id:
                return SpawnResult(agent, leaf_id, task_id, [], error="STALE_TICKET: leaf now points to another task")
            leaf = current_leaf | ({"claim_ticket": leaf["claim_ticket"]} if leaf.get("claim_ticket") else {})
        except (OSError, ValueError, RuntimeError) as exc:
            return SpawnResult(agent, leaf_id, task_id, [], error=f"SNAPSHOT_FAILED: {exc}")

        # Claim if leaf is currently queued
        if leaf.get("state") == "queued":
            try:
                msg = self.forest.claim_leaf(leaf_id, agent)
                if msg is None:
                    raise BusError("claim already belongs to another session")
                task_id = msg.id
                self._tickets[task_id] = claim_fencing.captured_ticket(msg)
            except Exception as exc:
                logger.error("Failed to claim leaf %s for agent %s: %s", leaf_id, agent, exc)
                return SpawnResult(
                    agent=agent,
                    leaf_id=leaf_id,
                    task_id=task_id,
                    cmd=[],
                    started=False,
                    success=False,
                    error=f"Claim failed: {exc}",
                )

        if not task_id:
            task_id = leaf.get("task_id") or leaf_id

        ticket = self._tickets.get(task_id) or leaf.get("claim_ticket")
        if ticket is None:
            return SpawnResult(agent, leaf_id, task_id, [], error="SESSION_TICKET_REQUIRED: this engine did not claim the task")
        try:
            current = self.forest.bus._active_record(agent, task_id)
            if current is None:
                raise BusError("task is not active for this member")
            ticket = self.forest.bus.check_claim(agent, current, ticket)
            if current.attempt > int(self.cfg.gates.get("max_retries", 2)):
                return SpawnResult(agent, leaf_id, task_id, [], error="RETRY_LIMIT: current task exceeds retry budget")
            if current.deadline_ts:
                remaining = current.deadline_ts - time.time()
                if remaining <= 0:
                    return SpawnResult(agent, leaf_id, task_id, [], error="DEADLINE_EXCEEDED: current task budget expired")
                timeout = min(timeout, remaining)
            if current.budget_s > 0:
                timeout = min(timeout, current.budget_s)
        except (OSError, ValueError, BusError, claim_fencing.TicketError) as exc:
            return SpawnResult(agent, leaf_id, task_id, [], error=f"TICKET_REFUSED: {exc}")

        # §13.5: Deduplicate concurrent spawns for identical ticket/task_id
        with self._active_lock:
            active_info = self.active_processes.get(task_id)
            if not active_info:
                active_info = {"agent": agent, "leaf_id": leaf_id, "ticket": ticket,
                               "cancel": threading.Event(), "phase": "starting"}
                self.active_processes[task_id] = active_info
                reserved = True
            else:
                reserved = False
        if not reserved:
            logger.warning("Task %s already has active supervised process PID %s", task_id, active_info.get("pid"))
            return SpawnResult(
                agent=agent,
                leaf_id=leaf_id,
                task_id=task_id,
                cmd=[],
                pid=active_info.get("pid"),
                started=False,
                success=False,
                error="ALREADY_ACTIVE: a subprocess is already running for this task ticket",
            )

        cmd = self.build_spawn_command(agent, leaf, task_id)
        run_id = secrets.token_hex(12)
        log_file = self.logs_dir / f"spawn_{agent}_{leaf_id}_{run_id}.log"

        start_time = time.time()
        logger.info("Spawning subprocess for %s on leaf %s: %s", agent, leaf_id, " ".join(cmd))

        proc = None
        job = None
        try:
            if stop_file.exists():
                raise BusError("STOP_REQUESTED: stopped before Popen")
            job = self._windows_job()
            # Original claim capability crosses only this subprocess boundary;
            # an adapter never needs to reconstruct a ticket from current disk.
            child_env = os.environ.copy()
            child_env["AGENTBUS_CLAIM_TICKET"] = json.dumps(ticket.to_dict(), ensure_ascii=False)
            child_env["AGENTBUS_WORKSPACE"] = str(self.cfg.workspace)
            with open(log_file, "w", encoding="utf-8") as out:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.cfg.workspace),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=(subprocess.CREATE_NO_WINDOW | 4) if sys.platform == "win32" else 0,  # CREATE_SUSPENDED
                    start_new_session=sys.platform != "win32",
                    env=child_env,
                )
                active_info.update(pid=proc.pid, proc=proc, job=job)
                if job:
                    job.attach_and_resume(proc)

            pid = proc.pid
            active_info.update(start_time=start_time, start_monotonic=time.monotonic(),
                               timeout=timeout, expires=time.monotonic() + min(timeout,
                                   max(0.0, current.deadline_ts - time.time()) if current.deadline_ts else timeout),
                               log_file=str(log_file), phase="running")
            result = SpawnResult(agent, leaf_id, task_id, cmd, pid=pid, log_path=str(log_file),
                                 started=True, phase="running")

            if not wait:
                # §13.5: Asynchronous mode registers process supervision; does NOT falsely declare business success
                monitor = threading.Thread(target=self._monitor_process, args=(task_id, active_info, result), daemon=True)
                active_info["monitor"] = monitor
                monitor.start()
                return result

            # Wait with watchdog & whole process tree termination
            self._monitor_process(task_id, active_info, result)
            return result

        except Exception as exc:
            if proc:
                self._stop_process(active_info)
            elif job:
                job.close()
            with self._active_lock:
                self.active_processes.pop(task_id, None)
            logger.error("Failed to spawn process for %s on leaf %s: %s", agent, leaf_id, exc, exc_info=True)
            return SpawnResult(
                agent=agent,
                leaf_id=leaf_id,
                task_id=task_id,
                cmd=cmd,
                started=proc is not None,
                success=False,
                error=str(exc),
            )

    def terminate_all_active(self) -> None:
        """Terminate all currently supervised subprocesses and their descendants."""
        with self._active_lock:
            active = list(self.active_processes.items())
        for task_id, info in active:
            info["cancel"].set()
            pid = info.get("pid")
            if pid:
                logger.info("Terminating active process tree for task %s (PID %d)", task_id, pid)
                if not info.get("monitor"):
                    try:
                        self._stop_process(info)
                    except (OSError, subprocess.SubprocessError) as exc:
                        note_silent("headless_spawn.py:319", "cleanup", f"子进程等待回收失败，进程树已先请求终止：{exc}")
                        pass  # 资源清理类：进程树终止已请求，保留清理异常供调试环读取。
            monitor = info.get("monitor")
            if monitor and monitor is not threading.current_thread():
                monitor.join(timeout=6)

    def auto_spawn_ready(self, max_spawn: int = 1) -> List[SpawnResult]:
        """Automatically find queued, claimable leaves and spawn eligible headless workers."""
        snap = self.forest.snapshot()
        spawned = []
        if max_spawn <= 0:
            return spawned
        for leaf in snap.get("leaves", []):
            if leaf.get("state") == "queued" and leaf.get("claimable", False):
                candidates = leaf.get("eligible_agents") or []
                for agent in candidates:
                    if self.is_spawnable(agent):
                        res = self.spawn_leaf(leaf, agent, wait=True)
                        spawned.append(res)
                        if len(spawned) >= max_spawn:
                            return spawned
                        break
        return spawned
