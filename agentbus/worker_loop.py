"""Autonomous Worker Loop engine for AgentBus.

Provides continuous and resilient task execution loops for agents,
breaking free from single-turn IDE conversation pauses.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .task_tree import TaskForest, TaskTreeError
from . import claim_fencing, heartbeat
from .bus import Bus, BusError
from .operation_lock import task_lock

logger = logging.getLogger("agentbus.worker_loop")


class WorkerLoopError(Exception):
    """Raised when worker loop operations fail."""


class HeartbeatKeeper:
    """Background thread to keep heartbeat alive while task is in progress with strict session ticket fencing."""

    def __init__(self, agentbus_dir: Path, task_id: str, agent: str, ticket: Optional[Dict[str, Any]] = None, interval_s: float = 15.0):
        self.agentbus_dir = agentbus_dir
        self.task_id = task_id
        self.agent = agent
        self.ticket = ticket or {}
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            ticket = claim_fencing.normalize({"task_id": self.task_id, "agent": self.agent, **self.ticket})
        except claim_fencing.TicketError as exc:
            logger.warning("HeartbeatKeeper refused invalid ticket: %s", exc.code)
            return
        bus = Bus(self.agentbus_dir)
        while not self._stop_event.is_set():
            try:
                if (self.agentbus_dir / "STOP").exists():
                    break
                # Serialize with recovery and normal heartbeat writes, then validate
                # the original ticket against the current processing record.
                with task_lock(self.agentbus_dir, self.agent, self.task_id):
                    current = bus._active_record(self.agent, self.task_id)
                    if current is None:
                        break
                    bus.check_claim(self.agent, current, ticket)
                    hb_file = heartbeat.heartbeat_path(self.agentbus_dir, self.task_id, self.agent)
                    if hb_file.exists():
                        observed = heartbeat.state(self.agentbus_dir, self.task_id, self.agent,
                                                   task_record=current.to_dict())
                        if observed["source"] != heartbeat.SOURCE_RECORD:
                            logger.warning("HeartbeatKeeper preserves invalid heartbeat for %s", self.task_id)
                            break
                    bus.heartbeat(self.agent, current, ticket, note="worker_loop_active")
            except (OSError, ValueError, BusError) as exc:
                logger.warning("Heartbeat update failed for %s: %s", self.task_id, exc)
                break
            self._stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)


class WorkerLoop:
    """Autonomous Worker Loop that continuously scans and processes tasks for an agent."""

    def __init__(
        self,
        cfg: Config,
        agent: str,
        poll_interval: float = 2.0,
        max_tasks: Optional[int] = None,
        timeout_s: Optional[float] = None,
        auto_claim: bool = True,
        handler: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self.cfg = cfg
        self.agent = agent
        self.poll_interval = max(0.2, float(poll_interval))
        self.max_tasks = max_tasks
        self.timeout_s = timeout_s
        self.auto_claim = auto_claim
        self.handler = handler
        self.forest = TaskForest(cfg)
        self.processed_tasks: List[Dict[str, Any]] = []
        self._tickets = {}
        self._attempted = set()

        if agent not in cfg.enabled_workers():
            raise WorkerLoopError(f"Agent '{agent}' is not registered or enabled in config.")

    def scan_ready_tasks(self) -> List[Dict[str, Any]]:
        """Scan task tree for tasks that are currently claimable by or assigned to this agent."""
        snapshot = self.forest.snapshot()
        ready_tasks = []
        for leaf in snapshot.get("leaves", []):
            leaf_id = leaf.get("id")
            state = leaf.get("state")
            owner = leaf.get("owner")
            claimable = leaf.get("claimable", False)
            eligible = leaf.get("eligible_agents", [])

            # Case 1: Already claimed/processing by this agent
            if state in ("claimed", "processing", "running") and owner == self.agent:
                ready_tasks.append(leaf)
            # Case 2: In queue and claimable by this agent
            elif state == "queued" and claimable and self.agent in eligible:
                ready_tasks.append(leaf)

        return ready_tasks

    def execute_task(self, leaf: Dict[str, Any]) -> Dict[str, Any]:
        """Claim, keep alive, and execute a single leaf task."""
        leaf_id = leaf["id"]
        state = leaf.get("state")
        owner = leaf.get("owner")
        task_id = leaf.get("task_id")

        # §13.3: Pre-check STOP file before doing any execution or heartbeats
        stop_file = self.cfg.workspace / ".agentbus" / "STOP"
        if stop_file.exists():
            logger.warning("STOP file detected before executing task %s. Aborting.", leaf_id)
            record = {
                "leaf_id": leaf_id,
                "section_no": leaf.get("section_no", ""),
                "task_id": task_id or leaf_id,
                "success": False,
                "duration_s": 0.0,
                "error": "STOP_REQUESTED: STOP file detected before task execution",
                "result": {"status": "stopped", "leaf_id": leaf_id},
            }
            self.processed_tasks.append(record)
            return record

        # §13.3: Pre-check deadline / budget
        deadline_ts = leaf.get("deadline_ts")
        if deadline_ts and time.time() > float(deadline_ts):
            logger.warning("Deadline exceeded before executing task %s. Aborting.", leaf_id)
            record = {
                "leaf_id": leaf_id,
                "section_no": leaf.get("section_no", ""),
                "task_id": task_id or leaf_id,
                "success": False,
                "duration_s": 0.0,
                "error": "DEADLINE_EXCEEDED: task deadline reached before execution",
                "result": {"status": "timeout", "leaf_id": leaf_id},
            }
            self.processed_tasks.append(record)
            return record

        def refused(code, status):
            record = {"leaf_id": leaf_id, "section_no": leaf.get("section_no", ""),
                      "task_id": task_id or leaf_id, "success": False, "duration_s": 0.0,
                      "error": code, "result": {"status": status, "leaf_id": leaf_id}}
            self._attempted.add((leaf_id, task_id or leaf_id))
            self.processed_tasks.append(record)
            return record

        if self.handler is None:
            return refused("NO_HANDLER: configure an explicit handler before claiming work", "awaiting_handler")

        # Step 1: Claim if not claimed yet
        if state == "queued" and self.auto_claim:
            logger.info("Claiming leaf %s for %s", leaf_id, self.agent)
            msg = self.forest.claim_leaf(leaf_id, self.agent)
            if msg is None:
                return refused("CLAIM_FAILED: another session claimed the task", "refused")
            task_id = msg.id
            self._tickets[task_id] = claim_fencing.captured_ticket(msg)

        # Refresh leaf snapshot after claim
        try:
            leaf_snapshot = self.forest._leaf(self.forest.snapshot(), leaf_id)
        except (OSError, ValueError, TaskTreeError) as exc:
            return refused(f"SNAPSHOT_FAILED: {exc}", "refused")
        current_task_id = leaf_snapshot.get("task_id") or task_id or leaf_id
        ticket = self._tickets.get(current_task_id) or leaf.get("claim_ticket")
        if ticket is None:
            return refused("SESSION_TICKET_REQUIRED: this loop did not claim the active task", "refused")
        try:
            current = self.forest.bus._active_record(self.agent, current_task_id)
            if current is None:
                return refused("SESSION_TICKET_REQUIRED: no active processing task", "refused")
            ticket = self.forest.bus.check_claim(self.agent, current, ticket)
            if task_id and current_task_id != task_id:
                return refused("STALE_TICKET: leaf now points to a successor task", "refused")
            if current.deadline_ts and time.time() > current.deadline_ts:
                return refused("DEADLINE_EXCEEDED: current task budget expired", "timeout")
            if current.attempt > int(self.cfg.gates.get("max_retries", 2)):
                return refused("RETRY_LIMIT: current task exceeds retry budget", "refused")
            if stop_file.exists():
                return refused("STOP_REQUESTED: stop requested after claim", "stopped")
            self.forest.bus.heartbeat(self.agent, current, ticket, note="worker_loop_active")
        except (OSError, ValueError, BusError) as exc:
            return refused(f"TICKET_REFUSED: {exc}", "refused")
        leaf_snapshot["claim_ticket"] = ticket.to_dict()
        leaf_snapshot["deadline_ts"] = current.deadline_ts
        self._attempted.add((leaf_id, current_task_id))

        # Step 2: Start heartbeat keeper with claim ticket info
        ticket_info = ticket.to_dict()
        keeper = HeartbeatKeeper(
            agentbus_dir=self.cfg.workspace / ".agentbus",
            task_id=current_task_id,
            agent=self.agent,
            ticket=ticket_info,
            interval_s=10.0,
        )
        keeper.start()

        start_time = time.time()
        success = False
        handled = False
        phase = "handler"
        error_msg = ""
        handler_result = None

        try:
            # Step 3: Run custom handler if provided (no fake success without handler)
            if self.handler:
                handler_result = self.handler(leaf_snapshot)
                success = not (handler_result is False or
                               isinstance(handler_result, dict) and
                               (handler_result.get("success") is False or
                                handler_result.get("status") in ("failed", "error", "stopped", "timeout", "awaiting_handler")))
                if not success:
                    error_msg = "HANDLER_FAILED: handler reported unsuccessful execution"
                handled = success
                if handled:
                    if stop_file.exists():
                        success = False
                        error_msg = "STOP_REQUESTED: stopped during handler execution"
                        phase = "stopped"
                    elif current.deadline_ts and time.time() > current.deadline_ts:
                        success = False
                        error_msg = "DEADLINE_EXCEEDED: handler exceeded task budget"
                        phase = "timeout"
                    else:
                        delivered = self.forest._leaf(self.forest.snapshot(), leaf_id)
                        receipt = self.forest.bus.find_message(delivered.get("result_id")) if delivered.get("result_id") else None
                        success = (delivered.get("task_id") == current_task_id
                                   and delivered.get("state") == "delivered"
                                   and delivered.get("receipt_state") == "valid"
                                   and delivered.get("artifact_state") in ("verified", "superseded", "not_required")
                                   and not delivered.get("evidence_error")
                                   and receipt is not None
                                   and receipt.source_claim_epoch == ticket.claim_epoch
                                   and receipt.source_claim_id == ticket.claim_id
                                   and receipt.source_conversation_id == ticket.conversation_id)
                        phase = "delivered" if success else "awaiting_delivery"
                        if not success:
                            error_msg = "DELIVERY_UNVERIFIED: handler finished without a verified receipt for its original ticket"
            else:
                logger.warning("Task %s ready for agent %s. No custom handler injected; marked awaiting_handler.", leaf_id, self.agent)
                success = False
                error_msg = "NO_HANDLER: task is claimed but requires explicit handler or runner to execute"
                handler_result = {"status": "awaiting_handler", "leaf_id": leaf_id}
        except Exception as exc:
            success = False
            error_msg = str(exc)
            logger.error("Handler error executing %s: %s", leaf_id, exc, exc_info=True)
        finally:
            keeper.stop()

        duration = round(time.time() - start_time, 3)
        record = {
            "leaf_id": leaf_id,
            "section_no": leaf.get("section_no", ""),
            "task_id": current_task_id,
            "success": success,
            "handled": handled,
            "phase": phase,
            "duration_s": duration,
            "error": error_msg,
            "result": handler_result,
        }
        self.processed_tasks.append(record)
        return record

    def run(self, continuous: bool = False, max_idle_loops: int = 3) -> Dict[str, Any]:
        """Run the autonomous worker loop.

        Args:
            continuous: If True, keep polling until stopped or timeout.
                        If False, exit after emptying currently ready queue.
            max_idle_loops: In non-continuous mode, exit after this many consecutive empty checks.
        """
        start_time = time.time()
        idle_count = 0
        logger.info(
            "WorkerLoop started for agent '%s' (continuous=%s, interval=%.1fs)",
            self.agent,
            continuous,
            self.poll_interval,
        )

        while True:
            # Check STOP file
            stop_file = self.cfg.workspace / ".agentbus" / "STOP"
            if stop_file.exists():
                logger.warning("STOP file detected. WorkerLoop aborting.")
                break

            # Check timeout
            if self.timeout_s is not None and (time.time() - start_time) >= self.timeout_s:
                logger.info("WorkerLoop reached timeout_s (%.1fs). Exiting.", self.timeout_s)
                break

            # Check max tasks
            if self.max_tasks is not None and len(self.processed_tasks) >= self.max_tasks:
                logger.info("WorkerLoop reached max_tasks (%d). Exiting.", self.max_tasks)
                break

            # Scan ready tasks
            ready_tasks = [task for task in self.scan_ready_tasks()
                           if (task["id"], task.get("task_id") or task["id"]) not in self._attempted]
            if ready_tasks:
                idle_count = 0
                for task in ready_tasks:
                    # Enforce per-item STOP and deadline checks before starting next item
                    if stop_file.exists():
                        logger.warning("STOP file detected before item. Aborting batch.")
                        break
                    if self.timeout_s is not None and (time.time() - start_time) >= self.timeout_s:
                        logger.info("Timeout exceeded before item. Aborting batch.")
                        break
                    if self.max_tasks is not None and len(self.processed_tasks) >= self.max_tasks:
                        break
                    self.execute_task(task)
            else:
                idle_count += 1
                if not continuous and idle_count >= max_idle_loops:
                    logger.info("Queue is empty. Non-continuous loop complete.")
                    break

            time.sleep(self.poll_interval)

        total_duration = round(time.time() - start_time, 3)
        return {
            "agent": self.agent,
            "continuous": continuous,
            "total_processed": len(self.processed_tasks),
            "succeeded": sum(1 for t in self.processed_tasks if t["success"]),
            "handled": sum(1 for t in self.processed_tasks if t.get("handled")),
            "awaiting_delivery": sum(1 for t in self.processed_tasks if t.get("phase") == "awaiting_delivery"),
            "failed": sum(1 for t in self.processed_tasks if not t["success"] and t.get("phase") != "awaiting_delivery"),
            "tasks": self.processed_tasks,
            "duration_s": total_duration,
        }
