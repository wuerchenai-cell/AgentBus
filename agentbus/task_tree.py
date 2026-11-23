"""Project owned task forest, projected from the existing file bus.

Source: user task forest decision / Codex, 2026-10-08. The structure file
contains goals, dependencies, checkpoints and message bindings. Completion,
ownership and execution evidence are always read from Bus records. No model,
private agent memory, network request or automatic wakeup is used here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from .bus import Bus, BusError, Message, now_iso
from .config import Config, load
from . import claim_fencing, heartbeat, jobs
from .gates import GateKeeper
from .operation_lock import task_lock
from .task_assignment import ordinary_task_workers


class TaskTreeError(ValueError):
    pass


_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_CONVERSATION = re.compile(r"cv-[0-9a-f]{24}\Z", re.ASCII)
_NODE_KEYS = {"id", "title", "parent_id", "goal", "handoff", "task_id",
              "job_id", "dependencies", "history_task_ids", "expected_artifacts",
              "result_to", "required_capabilities", "section_no", "handoff_history",
              "progress_percent", "admin_override", "cost_tier", "token_budget",
              "tokens_consumed", "cost"}
_HANDOFF_HISTORY_KEYS = {"task_id", "agent", "conversation_id", "timestamp",
                         "reason", "owner_confirmed_inactive"}
_SECTION = re.compile(r"(?=.{1,128}\Z)(?:0|[1-9][0-9]{0,8})(?:\.[1-9][0-9]{0,8}){0,63}\Z", re.ASCII)
_PRODUCT_JOB = "j-b7ed91331128d934d22268834e306bd4"
_PRODUCT_TASKS = (
    ("WB-SKILL", "skills", "统一技能中心", "m-1791457584489-f05b6b"),
    ("WB-PLAN", "skills", "持续计划与短状态", "m-1791457584492-83bbee"),
    ("TR-DESKTOP", "desktop", "桌面外壳与动态团队", "m-1791457584495-f44d5b"),
    ("TR-RESCUE", "desktop", "入组与异常救援", "m-1791457584497-b68c42"),
    ("OC-RELIABILITY", "reliability", "心跳、防撞与接力恢复", "m-1791457584499-8893c2"),
    ("OC-EVIDENCE", "reliability", "交付体检与证据履历", "m-1791457584502-3c65ce"),
    ("AG-EXT", "members", "通用成员适配器", "m-1791457584504-8c2e3f"),
    ("AG-MEMORY", "members", "项目记忆与接手包", "m-1791457584507-f6eac3"),
)


def _id(value, label="node id"):
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise TaskTreeError(f"invalid {label}")
    return value


def _inherited_dependencies(nodes, node):
    """A branch's prerequisites gate all descendants, not merely its label."""
    dependencies = []
    while node is not None:
        dependencies.extend(node.get("dependencies", []))
        node = nodes.get(node.get("parent_id"))
    return list(dict.fromkeys(dependencies))


class TaskForest:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # §7.31：工作区根在进程内是常量，解析一次即可。
        # 实测（150 叶 / 3 次快照的 cProfile）`nt._getfinalpathname` 占 31% 墙钟，
        # 而 `_path_check` 每调一次就要 realpath **路径 + 工作区**两遍。把工作区这一半
        # 提到构造期，语义完全不变，直接砍掉约一半 realpath 调用。
        self._workspace_root = cfg.workspace.resolve()
        self.root = self._workspace_root / ".agentbus"
        self.path = self.root / "task-tree.json"
        self.bus = Bus(self.root)
        self.observation_errors = []
        self._path_check(self.root)

    def _path_check(self, path):
        resolved = path.resolve()
        if (not resolved.is_relative_to(self._workspace_root)
                or any(part.casefold() in {".workbuddy", "不要访问", "do not access"}
                       for part in resolved.parts)):
            raise TaskTreeError("task forest path leaves the current project")
        return path

    def _read_json(self, path):
        self._path_check(path)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise TaskTreeError("project record exceeds task forest size limit")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise TaskTreeError(f"cannot read project record {path.name}") from exc

    def _job_records(self, identities=None):
        out = []
        self._path_check(self.root / "jobs")
        paths = sorted((self.root / "jobs").glob("j-*.json")) if identities is None else [
            self.root / "jobs" / (jobs._safe(identity, "job id") + ".json") for identity in sorted(identities)]
        for path in paths:
            if not path.is_file():
                self.observation_errors.append(f"missing bound job {path.stem}")
                continue
            try:
                value = self._read_json(path)
            except (OSError, TaskTreeError) as exc:
                self.observation_errors.append(f"unreadable job {path.stem}: {type(exc).__name__}")
                continue
            valid = (isinstance(value, dict) and value.get("schema_version") == 1
                     and value.get("id") == path.stem and isinstance(value.get("tasks"), list))
            if valid:
                workspace = value.get("workspace")
                valid = jobs.same_project(workspace, self._workspace_root, value.get("id"))
                valid = valid and all(isinstance(item, dict) and isinstance(item.get("message"), dict)
                                      and isinstance(item.get("id"), str)
                                      and item["message"].get("id") == item["id"] for item in value["tasks"])
                try:
                    valid = valid and all(math.isfinite(float(value.get(key, 0)))
                                          for key in ("deadline_ts", "created_ts"))
                except (ValueError, TypeError, OverflowError):
                    valid = False
            if not valid:
                self.observation_errors.append(f"invalid job record {path.name}; evidence preserved")
                continue
            out.append(value)
        return out

    @staticmethod
    def _normalize(tree):
        if not isinstance(tree, dict) or tree.get("schema_version", 1) != 1:
            raise TaskTreeError("task forest must be a version 1 structure")
        out = {"schema_version": 1}
        reserved = tree.get("section_registry", [])
        if (not isinstance(reserved, list) or len(reserved) > 12000
                or any(not isinstance(v, str) or not _SECTION.fullmatch(v) for v in reserved)):
            raise TaskTreeError("invalid section number registry")
        out["section_registry"] = list(dict.fromkeys(reserved))
        all_nodes = {}
        for kind in ("roots", "branches", "leaves"):
            values = tree.get(kind, [])
            if not isinstance(values, list) or len(values) > 1000:
                raise TaskTreeError(f"{kind} must be a bounded list")
            out[kind] = []
            for item in values:
                if not isinstance(item, dict) or set(item) - _NODE_KEYS:
                    raise TaskTreeError("structure nodes accept goals and bindings, never state/results/owner")
                node = copy.deepcopy(item)
                identity = _id(node.get("id"))
                if "section_no" in node and (not isinstance(node["section_no"], str)
                        or not _SECTION.fullmatch(node["section_no"])):
                    raise TaskTreeError("section_no must be a bounded chapter number")
                if identity in all_nodes:
                    raise TaskTreeError("duplicate task forest id")
                for key in ("title", "goal", "handoff"):
                    text = node.get(key, "")
                    if not isinstance(text, str) or len(text) > 100000:
                        raise TaskTreeError(f"{key} must be bounded text")
                    node[key] = text
                if "progress_percent" in node:
                    progress = node["progress_percent"]
                    if (kind != "leaves" or type(progress) is not int
                            or not 0 <= progress <= 100):
                        raise TaskTreeError("progress_percent must be an integer from 0 to 100 on a task leaf")
                node.setdefault("parent_id", None)
                if node["parent_id"] is not None:
                    _id(node["parent_id"], "parent id")
                for key in ("task_id", "job_id", "result_to"):
                    if node.get(key):
                        jobs._safe(node[key], key)
                for key in ("dependencies", "history_task_ids", "required_capabilities"):
                    values = node.get(key, [])
                    if not isinstance(values, list) or len(values) > 1000:
                        raise TaskTreeError(f"invalid {key}")
                    node[key] = list(dict.fromkeys(_id(v, key) for v in values))
                    if key == "history_task_ids":
                        for value in node[key]:
                            jobs._safe(value, "history task id")
                if "handoff_history" in node:
                    if kind != "leaves":
                        raise TaskTreeError("only task leaves can store handoff history")
                    history = node["handoff_history"]
                    if not isinstance(history, list) or len(history) > 100:
                        raise TaskTreeError("handoff history must be a bounded list of at most 100 records")
                    normalized_history = []
                    for record in history:
                        if not isinstance(record, dict) or set(record) != _HANDOFF_HISTORY_KEYS:
                            raise TaskTreeError("handoff history accepts only public conversation metadata")
                        item = copy.deepcopy(record)
                        jobs._safe(item.get("task_id"), "handoff task id")
                        if item["task_id"] not in node.get("history_task_ids", []):
                            raise TaskTreeError("handoff history task must exist in the leaf task history")
                        jobs._safe(item.get("agent"), "handoff member")
                        conversation_id = item.get("conversation_id")
                        if conversation_id and not _CONVERSATION.fullmatch(conversation_id):
                            raise TaskTreeError("invalid AgentBus conversation id in handoff history")
                        if not isinstance(conversation_id, str):
                            raise TaskTreeError("handoff conversation id must be text")
                        timestamp = item.get("timestamp")
                        reason = item.get("reason")
                        if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 80:
                            raise TaskTreeError("handoff timestamp must be bounded text")
                        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10000:
                            raise TaskTreeError("handoff reason must be bounded nonempty text")
                        if type(item.get("owner_confirmed_inactive")) is not bool:
                            raise TaskTreeError("handoff confirmation must be boolean")
                        normalized_history.append(item)
                    node["handoff_history"] = normalized_history
                if "expected_artifacts" in node:
                    node["expected_artifacts"] = jobs._expected_artifacts(node["expected_artifacts"])
                if "admin_override" in node:
                    override = node["admin_override"]
                    if not isinstance(override, dict) or not isinstance(override.get("reason"), str) or not override.get("reason", "").strip():
                        raise TaskTreeError("admin_override must be an object with nonempty reason")
                    mode = str(override.get("mode") or "pass").strip()
                    if mode not in ("pass", "reset"):
                        raise TaskTreeError("admin_override mode must be pass or reset")
                    norm_override = {
                        "admin": str(override.get("admin") or "admin"),
                        "reason": str(override["reason"]).strip(),
                        "timestamp": str(override.get("timestamp") or now_iso()),
                    }
                    if mode != "pass":
                        norm_override["mode"] = mode
                    if "reset_generation" in override:
                        norm_override["reset_generation"] = int(override["reset_generation"])
                    node["admin_override"] = norm_override
                if kind != "leaves" and node.get("task_id"):
                    raise TaskTreeError("only leaves can bind task messages")
                all_nodes[identity] = (kind, node)
                out[kind].append(node)
        task_ids = [node.get("task_id") for node in out["leaves"] if node.get("task_id")]
        if len(task_ids) != len(set(task_ids)):
            raise TaskTreeError("a task message can bind only one current leaf")
        for identity, (kind, node) in all_nodes.items():
            parent = all_nodes.get(node["parent_id"])
            if kind == "roots" and node["parent_id"] is not None:
                raise TaskTreeError("project roots cannot have parents")
            if kind != "roots" and (parent is None or parent[0] == "leaves"):
                raise TaskTreeError("task forest parent must be an existing root/branch")
            for dependency in node["dependencies"]:
                if dependency not in all_nodes or dependency == identity:
                    raise TaskTreeError("dependency must reference another forest node")
        def visit(identity, trail, completed, edge):
            if len(trail) > 64:
                raise TaskTreeError("task forest nesting exceeds 64 levels")
            if identity in trail:
                raise TaskTreeError(f"cyclic forest {edge}")
            if identity in completed:
                return
            node = all_nodes[identity][1]
            targets = node["dependencies"] if edge == "dependencies" else [node["parent_id"]]
            if edge == "readiness":
                targets = _inherited_dependencies({key: value[1] for key, value in all_nodes.items()}, node) + [identity for identity, (_, child) in all_nodes.items()
                                                  if child["parent_id"] == node["id"]]
            for target in targets:
                if target is not None:
                    visit(target, trail | {identity}, completed, edge)
            completed.add(identity)
        for edge in ("dependencies", "parents", "readiness"):
            completed = set()
            for identity in all_nodes:
                visit(identity, set(), completed, edge)
        assigned = [node["section_no"] for _, node in all_nodes.values() if "section_no" in node]
        if len(assigned) != len(set(assigned)):
            raise TaskTreeError("duplicate task forest section number")
        used = set(out["section_registry"]) | set(assigned)
        def allocate(node):
            if "section_no" in node:
                return node["section_no"]
            parent = all_nodes.get(node["parent_id"])
            if parent is None:
                if "0" not in used:
                    value = "0"
                else:
                    prefix = "0."
                    numbers = [int(v[len(prefix):]) for v in used if v.startswith(prefix)
                               and v[len(prefix):].isdigit()]
                    value = prefix + str(max(numbers, default=0) + 1)
            else:
                parent_no = allocate(parent[1])
                prefix = "" if parent_no == "0" else parent_no + "."
                numbers = [int(v[len(prefix):]) for v in used if v.startswith(prefix)
                           and v[len(prefix):].isdigit()]
                value = prefix + str(max(numbers, default=0) + 1)
            if not _SECTION.fullmatch(value):
                raise TaskTreeError("section number nesting exceeds its limit")
            used.add(value)
            node["section_no"] = value
            return value
        for _, node in all_nodes.values():
            allocate(node)
        for identity, (_, node) in all_nodes.items():
            if identity in used and node["section_no"] != identity:
                raise TaskTreeError("node id conflicts with an existing or retired chapter reference")
        out["section_registry"] = sorted(used, key=lambda value: tuple(map(int, value.split("."))))
        return out

    def _derived(self, records):
        if not records:
            return {"schema_version": 1, "roots": [], "branches": [], "leaves": []}
        live = [job for job in records if not job.get("finalization")
                and float(job.get("deadline_ts", 0)) > time.time()]
        selected = live or [max(records, key=lambda job: float(job.get("created_ts", 0)))]
        tree = {"schema_version": 1,
                "roots": [{"id": "project", "title": self.cfg.workspace.name,
                           "goal": selected[-1].get("goal", "项目目标"), "parent_id": None}],
                "branches": [], "leaves": []}
        for job in selected:
            branch_id = "job:" + job["id"]
            tree["branches"].append({"id": branch_id, "title": job.get("goal", "项目工作"),
                                     "goal": job.get("goal", ""), "job_id": job["id"],
                                     "parent_id": "project"})
            for item in job["tasks"]:
                tree["leaves"].append({"id": "task:" + item["id"], "title": item.get("title", "任务"),
                                       "goal": item.get("body", ""), "parent_id": branch_id,
                                       "job_id": job["id"], "task_id": item["id"]})
        return self._normalize(tree)

    def _structure(self):
        # Legacy structures project deterministic chapter references read-only;
        # the next explicit structure operation persists those references.
        if self.path.exists():
            original = self._read_json(self.path)
            tree = self._normalize(original)
            identities = {node["job_id"] for key in ("roots", "branches", "leaves")
                          for node in tree[key] if node.get("job_id")}
            records = self._job_records(identities)
            allocated = {item["id"]: item for job in records for item in job["tasks"]}
            for leaf in tree["leaves"]:
                job_id = self._job_id(tree, leaf)
                if not leaf.get("task_id") and job_id:
                    identity = self._allocation_id(job_id, leaf)
                    if identity in allocated:
                        leaf["task_id"], leaf["job_id"] = identity, job_id
            represented = {identity for node in tree["leaves"]
                           for identity in [node.get("task_id"), *node.get("history_task_ids", [])] if identity}
            for job in records:
                remaining = [item for item in job["tasks"] if item["id"] not in represented]
                if not remaining:
                    continue
                branch_id = "new:" + job["id"]
                if not any(node["id"] == branch_id for node in tree["branches"]):
                    root_id = next((node["id"] for node in tree["roots"] if node.get("job_id") == job["id"]),
                                   tree["roots"][0]["id"])
                    tree["branches"].append({"id": branch_id, "parent_id": root_id,
                                             "title": "新工作事项", "job_id": job["id"]})
                for item in remaining:
                    tree["leaves"].append({"id": "task:" + item["id"], "parent_id": branch_id,
                                           "title": item.get("title", "新任务"), "goal": item.get("body", "")[:1500],
                                           "job_id": job["id"], "task_id": item["id"]})
            tree = self._normalize(tree)
        else:
            tree = self._derived(self._job_records())
        return tree

    def _write(self, tree):
        tree = self._normalize(tree)
        self._path_check(self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x", encoding="utf-8") as stream:
                json.dump(tree, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _member(self, agent, *, ordinary_task=False):
        jobs._safe(agent, "agent")
        if agent not in self.cfg.executor_workers():
            raise TaskTreeError("member is not an enabled project executor")
        if ordinary_task and agent not in ordinary_task_workers(self.cfg):
            raise TaskTreeError(
                f"成员 {agent} 是本项目指定监督员，保留用于独立审核，不能领取普通任务。"
                f"请用 inbox --mine --caller {agent} 查看审核信箱。")

    def _refresh_config(self):
        """Read the adopted member/capability/gate configuration inside its lock."""
        current = load(self.cfg.workspace)
        root = current.workspace.resolve() / ".agentbus"
        self._path_check(root)
        self.cfg = current
        # Bus contains paths, not member/configuration caches. Preserve the
        # same client when its control root is unchanged; otherwise rebind it.
        if root != self.root:
            self.root, self.path, self.bus = root, root / "task-tree.json", Bus(root)

    def _eligible(self, leaf):
        required = set(leaf.get("required_capabilities", []))
        eligible = []
        for agent, worker in sorted(ordinary_task_workers(self.cfg).items()):
            capabilities = worker.get("capabilities", [])
            if isinstance(capabilities, dict):
                capabilities = [key for key, enabled in capabilities.items() if enabled]
            if not required or (isinstance(capabilities, list) and required.issubset(capabilities)):
                eligible.append(agent)
        return eligible

    def _receipts(self, msg, indexes=None):
        if not msg.result_to:
            return [], "任务缺少明确的成果回投地址。"
        self._path_check(self.bus.agents_dir / msg.result_to)
        if indexes is None:
            receipts = self.bus.find_results(msg.id, recipient=msg.result_to)
        else:
            recipient = msg.result_to.casefold()
            if recipient not in indexes:
                indexes[recipient] = self.bus.index_results(recipient)
            receipts = indexes[recipient].get(msg.id, [])
        if len(receipts) != 1:
            return receipts, "需要唯一一份正式成果回执。"
        receipt = receipts[0]
        error = jobs.receipt_binding_error(msg, receipt)
        if not error and (receipt.exit_code not in (None, 0) or receipt.status in ("failed", "blocked")):
            error = "正式回执报告失败或阻塞。"
        return receipts, error

    def _allowed(self, msg=None, job=None):
        if os.path.lexists(self.root / "STOP"):
            raise TaskTreeError("STOP 生效；保留当前任务与断点")
        if job:
            jobs._running_allowed(self.cfg, job)
            if job.get("finalization"):
                raise TaskTreeError("job has already been finalized")
        if msg:
            decision = GateKeeper(self.cfg.gates).check_dispatch(msg, include_total_budget=False)
            if not decision:
                raise TaskTreeError(decision.reason)

    _MAILBOX_LOCATIONS = ("done", "processing", "blocked", "ready")

    def _message_index(self):
        """§7.31：一次目录扫描建立 `identity -> (path, location, agent)` 索引。

        旧的 `_message` 是**每个叶子各自**遍历一遍成员目录 × 4 个位置，于是 150 叶
        快照要付出 O(叶子数 × 成员数 × 位置数) 次 `is_file` + `_path_check`（含 realpath）。
        改成"一次扫描服务全部叶子"后，总代价降到 O(邮件总数)。

        **与旧实现严格等价**：
        * 成员目录按 `iterdir()` 顺序、位置按 done→processing→blocked→ready 顺序；
        * 同一 identity 只记**首个存在的文件**——即使内容非法也照旧判 `unreadable`，
          不会退回后面的位置（旧实现同样是"一遇即停"）；
        * 仍对每个既有文件执行 `_path_check`（符号链接逃逸的守卫一点没放松）。

        **不是跨快照缓存**：索引在每次快照内新建、用完即弃，因此不存在"陈旧视图"问题。
        """
        self._path_check(self.bus.agents_dir)
        index = {}
        bases = list(self.bus.agents_dir.iterdir()) if self.bus.agents_dir.exists() else []
        for base in bases:
            if base.name.casefold() in {".workbuddy", "不要访问", "do not access"}:
                continue
            self._path_check(base)
            for location in self._MAILBOX_LOCATIONS:
                folder = base / location
                if not folder.is_dir():
                    continue
                self._path_check(folder)
                for path in folder.glob("*.json"):
                    identity = path.stem
                    if identity in index:
                        continue
                    if not path.is_file():      # 与旧实现的 `is_file()` 守卫一致
                        continue
                    self._path_check(path)
                    index[identity] = (path, location, base.name)
        return index

    def _message(self, identity, index=None):
        if not identity:
            return None, "unassigned"
        jobs._safe(identity, "task id")
        if index is None:
            index = self._message_index()
        hit = index.get(identity)
        if hit is None:
            return None, "missing"
        path, location, agent = hit
        try:
            message = self.bus.read(path)
        except (OSError, ValueError, BusError):
            return None, "unreadable"
        if message.id != identity or message.type != "task" or message.to != agent:
            return None, "unreadable"
        return message, location

    @staticmethod
    def _has_unresolved_audit(message):
        if message is None or message.status == "done":
            return False
        return (
            message.status in ("awaiting_audit", "rejected")
            or (message.supervision or {}).get("state") in
            ("awaiting_audit", "decision_recorded", "approved", "rejected")
        )

    def _pass_requires_audit(self, leaf, message, location, index=None):
        strict = self.cfg.data.get("supervision", {}).get("strict_supervision", False)
        if self._has_unresolved_audit(message):
            return True
        if strict and message is not None and location in ("ready", "processing"):
            return True
        if message is not None and location == "done" and message.status == "done":
            if (message.supervision or {}).get("state") == "approved":
                return False
        for identity in reversed(leaf.get("history_task_ids", [])):
            previous, previous_location = self._message(identity, index)
            if self._has_unresolved_audit(previous):
                return True
            if previous is not None and previous_location == "done" and previous.status == "done":
                if (previous.supervision or {}).get("state") == "approved":
                    break
        return False

    @staticmethod
    def _has_verified_delivery(node, error):
        return (
            node["state"] == "delivered"
            and node.get("receipt_state") == "valid"
            and node.get("artifact_state") in ("verified", "superseded", "not_required")
            and not error
        )

    @staticmethod
    def _aggregate(nodes):
        if not nodes:
            return "queued"
        states = {node["state"] for node in nodes}
        if states == {"delivered"}:
            return "delivered"
        if states <= {"delivered", "admin_passed"}:
            return "admin_passed"
        for state in ("stopped", "rejected", "awaiting_audit", "blocked", "missing",
                      "running", "claimed", "awaiting_receipt"):
            if state in states:
                return state
        return "queued"

    def snapshot(self):
        self.observation_errors = []
        tree = self._structure()
        identities = {node["job_id"] for key in ("roots", "branches", "leaves")
                      for node in tree[key] if node.get("job_id")}
        records = {job["id"]: job for job in self._job_records(identities if identities else None)}
        stopped = os.path.lexists(self.root / "STOP")
        nodes = {node["id"]: node for key in ("roots", "branches", "leaves") for node in tree[key]}
        receipt_indexes = {}
        from .artifact_versions import ArtifactVersions
        versions = ArtifactVersions(self.cfg, self.bus)
        routing_events = None
        message_index = self._message_index()      # 一次扫描服务本次快照的全部叶子
        for node in tree["leaves"]:
            eligible = self._eligible(node)
            msg, location = self._message(node.get("task_id"), message_index)
            node.update(state="queued", owner="", results="尚无正式成果回执。", artifacts=[],
                        task_id=node.get("task_id"), claimable=False, eligible_agents=[],
                        result_id=None, location=location, heartbeat=None, evidence_error="",
                        receipt_state="missing", artifact_state="not_checked",
                        dependency_ready=False,
                        conversation_id="", handoff_history=list(node.get("handoff_history", [])))
            error = ""
            if msg:
                node["owner"] = msg.claimed_by or msg.claim_agent or msg.to
                node["claim_epoch"] = msg.claim_epoch
                node["conversation_id"] = (msg.claim_conversation_id
                                            or (claim_fencing.conversation_id_for_claim(msg.claim_id)
                                                if msg.claim_id else ""))
                node["goal"] = node["goal"] or msg.body
                job = records.get(msg.job_id)
                item = next((item for item in (job or {}).get("tasks", []) if item["id"] == msg.id), None)
                if msg.job_id and item and not jobs._same_intent(msg, item["message"]):
                    if routing_events is None:
                        routing_events = self.bus.events()
                if msg.job_id and (not job or not item or not jobs._task_intent_matches(
                        msg, item["message"], routing_events or [])):
                    is_import = (
                        str(msg.job_id or "").startswith("import-")
                        or str(node.get("id") or "").startswith("import-")
                        or "import" in str(msg.job_id or "").lower()
                    )
                    artifacts_physically_verified = False
                    if location == "done" and msg.status == "done":
                        receipts_probe, r_err = self._receipts(msg, receipt_indexes)
                        if receipts_probe and not r_err:
                            art_err = jobs._artifact_error(
                                self.cfg,
                                item or {"id": msg.id, "expected_artifacts": msg.expected_artifacts},
                                receipts_probe[0],
                                versions=versions,
                            )
                            if not art_err:
                                artifacts_physically_verified = True
                    if is_import or artifacts_physically_verified:
                        pass
                    else:
                        error = "任务与原始作业意图不一致；请核对原记录。"
                if location == "done" and msg.status == "done":
                    receipts, receipt_error = self._receipts(msg, receipt_indexes)
                    artifact_error = ""
                    if len(receipts) == 1:
                        if receipt_error:
                            node["receipt_state"] = "invalid"
                        else:
                            receipt = receipts[0]
                            node.update(result_id=receipt.id, artifacts=list(receipt.artifacts),
                                        receipt_state="valid")
                            artifact_error, artifact_state, replacements = jobs._artifact_verdict(
                                self.cfg,
                                item or {"id": msg.id, "expected_artifacts": msg.expected_artifacts},
                                receipt,
                                versions=versions,
                            )
                            node["artifact_state"] = artifact_state
                            if replacements:
                                node["artifact_replacements"] = replacements
                    elif receipts:
                        node["receipt_state"] = "ambiguous"
                    error = error or receipt_error
                    if not error:
                        receipt = receipts[0]
                        node.update(state="delivered", results=(receipt.body or "正式回执已提交。")[:1500],
                                    artifacts=list(receipt.artifacts), result_id=receipt.id)
                        if artifact_error:
                            error = artifact_error
                            node["results"] += (f"\n\n正式回执 {receipt.id} 已保留。当前产物文件与回执清单不一致；"
                                                "无法判断是文件后来变化还是清单被修改，依赖此产物的任务暂不可用。")
                    else:
                        node["state"] = "awaiting_receipt"
                        if node["receipt_state"] == "valid":
                            if node["artifact_state"] == "mismatch":
                                node["results"] = (f"已收到回执 {node['result_id']}；原清单中的产物与当前文件不一致，"
                                                    "需要复核；当前未标记为交付。")
                            else:
                                node["results"] = (f"已收到回执 {node['result_id']}；任务关联或其他校验仍未通过，"
                                                    "当前未标记为交付。")
                        else:
                            node["results"] = "源任务已归档；正式成果回执仍缺失、重复或无效。"
                elif msg.status == "awaiting_audit":
                    node["state"] = "awaiting_audit"
                    node["results"] = "成果已提交，正在等待指定监督员独立审核。"
                    node["result_id"] = (msg.supervision or {}).get("pending_result_id")
                    node["artifacts"] = list((msg.supervision or {}).get("evidence", {})
                                             .get("artifact_verification", {}).get("artifacts", []))
                elif msg.status == "rejected":
                    node["state"] = "rejected"
                    node["results"] = msg.result or "独立审核未批准；保留成果等待修改后重新提交。"
                    node["claimable"] = False
                elif msg.status in ("failed", "blocked") or location == "blocked":
                    node["state"] = "blocked"
                    node["results"] = msg.result or "任务受阻；保留原有产物与历史。"
                elif location == "processing":
                    observed = heartbeat.state(self.root, msg.id, msg.to, task_record=msg.to_dict())
                    stale = heartbeat.is_stale(observed, self.cfg.gates.get("heartbeat_timeout_s", 600))
                    observed["stale"] = stale
                    node["heartbeat"] = observed
                    execution = observed.get("activity", "execution") == "execution"
                    node["state"] = "running" if not stale and execution else "claimed"
                    if stale:
                        node["results"] = "已认领；当前执行心跳未确认，原会话仍拥有任务。"
                    elif not execution:
                        node["results"] = "已记录认领或提交活动；执行进展仍未确认，原会话保留任务。"
                elif location == "ready":
                    node["state"] = "queued"
                    expired = bool(msg.deadline_ts and time.time() > msg.deadline_ts)
                    if expired:
                        node["eligible_agents"] = list(eligible) if eligible else ([msg.to] if msg.to in eligible else [])
                        node["claimable"] = bool(node["eligible_agents"])
                        node["results"] = "原指派已过初次窗口；已自动放开，任何合格工位均可认领接手。"
                    else:
                        if msg.to in eligible:
                            node["eligible_agents"] = [msg.to]
                            node["claimable"] = True
                else:
                    node["state"] = "missing"
                if node["state"] != "delivered":
                    try:
                        self._allowed(msg, job)
                    except (TaskTreeError, jobs.JobError) as exc:
                        if location == "ready" and ("预算" in str(exc) or "expired" in str(exc) or "超时" in str(exc)):
                            pass
                        else:
                            error = error or str(exc)
                if error and node["state"] not in ("delivered", "awaiting_receipt",
                                                      "awaiting_audit", "rejected"):
                    node["state"] = "blocked"
            elif node.get("task_id"):
                item = next((item for job in records.values() for item in job["tasks"]
                             if item["id"] == node["task_id"]), None)
                if item and location == "missing":
                    candidate = Message.from_dict(item["message"], strict=True)
                    try:
                        self._allowed(candidate, records.get(candidate.job_id))
                    except (TaskTreeError, jobs.JobError) as exc:
                        error = str(exc)
                    node["state"] = "queued" if not error else "blocked"
                    node["owner"] = candidate.to
                    node["results"] = "任务意图已分配；领取时恢复尚未完成的正式投递。"
                    node["eligible_agents"] = [candidate.to] if candidate.to in eligible else []
                    node["claimable"] = bool(node["eligible_agents"]) and not error
                else:
                    node["state"] = "missing"
                    error = "绑定任务不存在或不可读取；保留绑定等待恢复。"
            else:
                node["eligible_agents"] = eligible
                node["claimable"] = bool(eligible)
                old_id = (node.get("history_task_ids") or [None])[-1]
                previous, _ = self._message(old_id, message_index)
                job_id = self._job_id(tree, node)
                try:
                    self._allowed(previous, records.get(job_id))
                    if previous and previous.attempt + 1 > int(self.cfg.gates.get("max_retries", 2)):
                        raise TaskTreeError("接力会超过原任务重试预算。")
                except (TaskTreeError, jobs.JobError) as exc:
                    error = str(exc)
                    node["state"] = "blocked"
            if node.get("admin_override"):
                override = node["admin_override"]
                override_mode = override.get("mode", "pass")
                reason_text = override.get("reason", "管理员确认放行")
                admin_name = override.get("admin", "admin")
                admin_ts = override.get("timestamp", "")
                if (override_mode == "pass"
                        and not self._pass_requires_audit(node, msg, location, message_index)
                        and not self._has_verified_delivery(node, error)):
                    node.update(
                        state="admin_passed",
                        results=f"【管理员放行】{reason_text}（裁决人: {admin_name}, 时间: {admin_ts}）",
                        claimable=False
                    )
                elif (
                    override_mode == "reset"
                    and not node.get("task_id")
                    and override.get("reset_generation") == len(node.get("history_task_ids", []))
                ):
                    node.update(
                        state="queued",
                        results=f"【管理员重置重排】{reason_text}（裁决人: {admin_name}, 时间: {admin_ts}）",
                        evidence_error="",
                        eligible_agents=eligible,
                        claimable=bool(eligible) and not stopped
                    )
                    error = ""
            node["evidence_error"] = error
            if stopped and node["state"] not in ("delivered", "admin_passed"):
                node["state"] = "stopped"
            if error or stopped:
                node["claimable"] = False
            if not node["handoff"]:
                node["handoff"] = ("成果已回投；按产物继续集成与验收。" if node["state"] == "delivered"
                                   else "从本节点目标继续；完成后提交成果回执和下一步。")
        pending = list(tree["branches"]) + list(tree["roots"])
        while pending:
            progressed = False
            for node in pending[:]:
                children = [child for child in nodes.values() if child.get("parent_id") == node["id"]]
                if any("state" not in child for child in children):
                    continue
                node.update(state=self._aggregate(children), owner="", results="", task_id=None,
                            claimable=False, eligible_agents=[], artifacts=[])
                delivered = sum(child["state"] == "delivered" for child in children)
                node["results"] = f"{delivered}/{len(children)} 个下级节点已有正式成果回执；发布另需验收。"
                pending.remove(node)
                progressed = True
            if not progressed:
                raise TaskTreeError("cannot project task forest parent graph")
        children_by_parent = {identity: [] for identity in nodes}
        leaf_ids = {leaf["id"] for leaf in tree["leaves"]}
        for node in nodes.values():
            if node.get("parent_id") in children_by_parent:
                children_by_parent[node["parent_id"]].append(node)
        projected = set()
        def project_readiness(identity):
            if identity in projected:
                return nodes[identity]["state"]
            node = nodes[identity]
            inherited = _inherited_dependencies(nodes, node)
            for dependency in inherited:
                project_readiness(dependency)
            children = children_by_parent[identity]
            for child in children:
                project_readiness(child["id"])
            unmet = [dependency for dependency in inherited
                     if not nodes[dependency].get("dependency_ready", False)]
            node["effective_dependencies"] = inherited
            node["unmet_dependencies"] = unmet
            node["prerequisites_satisfied"] = not bool(unmet)

            # 详细列出未满足的前序依赖详情，增强可解释性
            unmet_details = []
            for dep_id in unmet:
                dep_node = nodes.get(dep_id, {})
                sec = dep_node.get("section_no", "")
                sec_prefix = f"§{sec} " if sec else ""
                unmet_details.append({
                    "id": dep_id,
                    "section_no": sec,
                    "title": dep_node.get("title", dep_id),
                    "state": dep_node.get("state", "unknown"),
                    "label": f"{sec_prefix}{dep_node.get('title', dep_id)}",
                })
            node["unmet_dependency_details"] = unmet_details

            if identity not in leaf_ids:
                node["state"] = self._aggregate(children)
                delivered = sum(child["state"] == "delivered" for child in children)
                node["results"] = f"{delivered}/{len(children)} 个下级节点已有正式成果回执；发布另需验收。"
                if unmet and node["state"] in {"queued", "delivered", "admin_passed"}:
                    node["state"] = "blocked"
            if unmet:
                node["claimable"] = False
                if node["state"] == "queued":
                    node["state"] = "blocked"
                    dep_names = "、".join(d["label"] for d in unmet_details[:3])
                    if len(unmet_details) > 3:
                        dep_names += f" 等 {len(unmet_details)} 项"
                    node["evidence_error"] = f"等待前序任务正式交付：{dep_names}。"
            if identity in leaf_ids:
                node["dependency_ready"] = (
                    not unmet and (
                        node["state"] == "admin_passed"
                        or self._has_verified_delivery(node, node.get("evidence_error"))
                    )
                )
                # 计算针对叶子节点的明确阻塞/不可认领原因（差哪一步）
                if unmet:
                    dep_names = "、".join(d["label"] for d in unmet_details[:2])
                    if len(unmet_details) > 2:
                        dep_names += f" 等共 {len(unmet_details)} 项前置任务"
                    node["blocked_reason"] = f"前序阻塞：等待 {dep_names} 交付并生成有效产物"
                elif not node.get("claimable"):
                    if node.get("state") in ("delivered", "admin_passed"):
                        node["blocked_reason"] = node.get("evidence_error") or None
                    elif node.get("state") == "stopped" or stopped:
                        node["blocked_reason"] = "派工已暂停"
                    elif node.get("evidence_error"):
                        node["blocked_reason"] = node["evidence_error"]
                    elif node.get("state") == "awaiting_audit" and not node.get("evidence_error"):
                        node["blocked_reason"] = "成果等待独立审核"
                    elif node.get("state") == "rejected":
                        node["blocked_reason"] = "成果审核未通过，请查看审核意见并修改后重新提交"
                    elif not node.get("eligible_agents"):
                        req = node.get("required_capabilities") or []
                        if req:
                            node["blocked_reason"] = f"前序已满足，但缺少匹配工位：本任务需要 [{', '.join(req)}] 技能，当前无匹配成员"
                        else:
                            node["blocked_reason"] = "前序已满足，但团队未配置可执行成员工位"
                    elif node.get("state") in ("claimed", "running"):
                        node["blocked_reason"] = f"已由 {node.get('owner', '成员')} 认领处理中"
                    else:
                        node["blocked_reason"] = "任务当前不可认领"
                else:
                    node["blocked_reason"] = None
            else:
                node["dependency_ready"] = (
                    node["state"] in ("delivered", "admin_passed")
                    and not unmet
                    and all(nodes[child["id"]].get("dependency_ready", False)
                            for child in children)
                )
                node["blocked_reason"] = "下级分支未完全交付" if unmet else None
            projected.add(identity)
            return node["state"]
        for identity in nodes:
            project_readiness(identity)
        # Token 消耗与成本预算聚合统计 (借鉴 multi-agent-collaboration)
        total_tokens = sum(int(l.get("tokens_consumed") or 0) for l in tree.get("leaves", []))
        total_cost = sum(float(l.get("cost") or 0.0) for l in tree.get("leaves", []))
        by_agent = {}
        for l in tree.get("leaves", []):
            owner = l.get("owner")
            tc = int(l.get("tokens_consumed") or 0)
            c = float(l.get("cost") or 0.0)
            if owner:
                stat = by_agent.setdefault(owner, {"tokens": 0, "cost": 0.0, "count": 0})
                stat["tokens"] += tc
                stat["cost"] = round(stat["cost"] + c, 4)
                if l.get("state") == "delivered":
                    stat["count"] += 1
        tree["token_summary"] = {
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "by_agent": by_agent,
        }
        tree.update(stopped=stopped, source="AgentBus tasks, receipts and heartbeat",
                    generated_at=now_iso(), auto_wakeup=False,
                    delivery_is_publication=False, observation_errors=self.observation_errors)
        return tree

    def replace_structure(self, tree):
        with task_lock(self.root, "task-forest", "structure"):
            previous = self._structure() if self.path.exists() else {"leaves": []}
            incoming_tree = copy.deepcopy(tree)
            if not isinstance(incoming_tree, dict):
                raise TaskTreeError("task forest must be a version 1 structure")
            known = {node["id"]: node.get("section_no") for key in ("roots", "branches", "leaves")
                     for node in previous.get(key, [])}
            for key in ("roots", "branches", "leaves"):
                for node in incoming_tree.get(key, []) if isinstance(incoming_tree.get(key, []), list) else []:
                    if isinstance(node, dict) and isinstance(node.get("id"), str) and node.get("id") in known:
                        old_no = known[node["id"]]
                        if old_no and node.get("section_no", old_no) != old_no:
                            raise TaskTreeError("existing task chapter numbers cannot be changed")
                        if old_no:
                            node["section_no"] = old_no
                    elif isinstance(node, dict) and node.get("section_no") in previous.get("section_registry", []):
                        raise TaskTreeError("retired task chapter numbers cannot be reused")
            registry = incoming_tree.get("section_registry", [])
            if (not isinstance(registry, list) or len(registry) > 12000
                    or any(not isinstance(v, str) or not _SECTION.fullmatch(v) for v in registry)):
                raise TaskTreeError("invalid section number registry")
            incoming_tree["section_registry"] = list(dict.fromkeys(previous.get("section_registry", []) + registry))
            normalized = self._normalize(incoming_tree)
            previous_leaves = {node["id"]: node for node in previous["leaves"] if node.get("task_id")}
            incoming = {node["id"]: node for node in normalized["leaves"]}
            for identity, old in previous_leaves.items():
                new = incoming.get(identity)
                if new is None or new.get("task_id") != old["task_id"]:
                    raise TaskTreeError("structure update must preserve existing task bindings; use handoff")
            self._write(normalized)
        return self.snapshot()

    def seed_leaf(self, leaf, *, branch_id=None, root_id="project", project_goal="项目目标"):
        with task_lock(self.root, "task-forest", "structure"):
            tree = self._structure()
            if not tree["roots"]:
                tree["roots"].append({"id": root_id, "parent_id": None,
                                      "title": self.cfg.workspace.name, "goal": project_goal})
            elif not any(node["id"] == root_id for node in tree["roots"]):
                if root_id == "project" and len(tree["roots"]) == 1:
                    root_id = tree["roots"][0]["id"]
                elif root_id == "project":
                    raise TaskTreeError("multiple project trees exist; choose an explicit root_id")
                else:
                    tree["roots"].append({"id": root_id, "parent_id": None,
                                          "title": self.cfg.workspace.name, "goal": project_goal})
            raw_branch = branch_id or leaf.get("parent_id") or root_id
            resolved_branch = None
            for key in ("roots", "branches"):
                for node in tree.get(key, []):
                    if node["id"] == raw_branch or node.get("section_no") == raw_branch:
                        resolved_branch = node["id"]
                        break
                if resolved_branch:
                    break
            branch_id = resolved_branch or raw_branch
            if branch_id != root_id and not any(node["id"] == branch_id for key in ("roots", "branches") for node in tree[key]):
                tree["branches"].append({"id": branch_id, "parent_id": root_id, "title": branch_id})

            leaf_id = leaf.get("id")
            if not leaf_id:
                ascii_slug = "".join(ch for ch in str(leaf.get("title", "")).upper()
                                     if (('A' <= ch <= 'Z') or ('0' <= ch <= '9') or ch in "-_") and ord(ch) < 128).strip("-_")
                if ascii_slug and len(ascii_slug) <= 24 and (('A' <= ascii_slug[0] <= 'Z') or ('0' <= ascii_slug[0] <= '9')):
                    candidate = ascii_slug
                else:
                    candidate = "TASK-" + uuid.uuid4().hex[:8].upper()
                existing_ids = {node["id"] for key in ("roots", "branches", "leaves") for node in tree.get(key, [])}
                suffix = 1
                leaf_id = candidate
                while leaf_id in existing_ids:
                    leaf_id = f"{candidate}-{suffix}"
                    suffix += 1

            deps = leaf.get("dependencies", [])
            if isinstance(deps, str):
                deps = [d.strip() for d in deps.split(",") if d.strip()]
            resolved_deps = []
            for dep in deps:
                matched = next((node["id"] for key in ("roots", "branches", "leaves") for node in tree.get(key, [])
                                if node["id"] == dep or node.get("section_no") == dep), None)
                resolved_deps.append(matched if matched else dep)

            cleaned_leaf = {
                "id": leaf_id,
                "parent_id": branch_id,
                "title": str(leaf.get("title") or leaf_id),
                "goal": str(leaf.get("goal") or leaf.get("title") or ""),
                "handoff": str(leaf.get("handoff") or ""),
                "dependencies": resolved_deps,
                "history_task_ids": list(leaf.get("history_task_ids", [])),
                "required_capabilities": list(leaf.get("required_capabilities", [])),
            }
            if leaf.get("task_id"):
                cleaned_leaf["task_id"] = leaf["task_id"]
            if leaf.get("job_id"):
                cleaned_leaf["job_id"] = leaf["job_id"]
            if leaf.get("expected_artifacts"):
                cleaned_leaf["expected_artifacts"] = leaf["expected_artifacts"]
            if leaf.get("section_no"):
                cleaned_leaf["section_no"] = leaf["section_no"]

            existing = next((node for node in tree["leaves"] if node["id"] == cleaned_leaf["id"]), None)
            if existing:
                raise TaskTreeError("leaf already exists; preserve its binding and checkpoint")
            if cleaned_leaf.get("section_no") in tree.get("section_registry", []):
                raise TaskTreeError("retired task chapter numbers cannot be reused")
            tree["leaves"].append(cleaned_leaf)
            self._write(tree)
        return self.snapshot()

    @staticmethod
    def _leaf(tree, leaf_id):
        _id(leaf_id)
        matches = [node for key in ("roots", "branches", "leaves") for node in tree.get(key, [])
                   if node["id"] == leaf_id or node.get("section_no") == leaf_id]
        if len(matches) != 1 or not any(matches[0] is leaf for leaf in tree["leaves"]):
            raise TaskTreeError("task forest leaf does not exist or its reference is ambiguous")
        return matches[0]

    def resolve_reference(self, reference):
        _id(reference, "task reference")
        tree = self._structure()
        matches = [node for key in ("roots", "branches", "leaves") for node in tree[key]
                   if node["id"] == reference or node.get("section_no") == reference]
        if len(matches) != 1:
            raise TaskTreeError("task reference does not exist or is ambiguous")
        return copy.deepcopy(matches[0])

    def resolve_leaf(self, identity):
        """Resolve a single leaf by ID or section number; fail on ambiguous or non-leaf references."""
        with task_lock(self.root, "task-forest", "structure"):
            return copy.deepcopy(self._leaf(self._structure(), identity))

    def update_leaf_topology(self, leaf_id: str, *, parent_id: str | None = None, dependencies: list[str] | None = None, title: str | None = None):
        """Update a leaf's parent branch group, dependencies, and/or title dynamically."""
        with task_lock(self.root, "task-forest", "structure"):
            tree = self._structure()
            leaf = self._leaf(tree, leaf_id)
            if title is not None:
                title_clean = str(title).strip()
                if not title_clean:
                    raise TaskTreeError("leaf title cannot be empty")
                leaf["title"] = title_clean
            if parent_id is not None:
                parent_id = str(parent_id).strip()
                valid_parents = {node["id"] for key in ("roots", "branches") for node in tree[key]}
                if parent_id not in valid_parents:
                    raise TaskTreeError(f"invalid parent branch: {parent_id}")
                leaf["parent_id"] = parent_id
            if dependencies is not None:
                all_ids = {node["id"] for key in ("roots", "branches", "leaves") for node in tree[key]}
                resolved_deps = []
                for dep in dependencies:
                    dep = str(dep).strip()
                    if dep in all_ids and dep != leaf["id"]:
                        resolved_deps.append(dep)
                leaf["dependencies"] = list(dict.fromkeys(resolved_deps))
            self._write(tree)
        return self.snapshot()



    @staticmethod
    def _job_id(tree, leaf):
        nodes = {node["id"]: node for key in ("roots", "branches", "leaves") for node in tree[key]}
        node = leaf
        while node:
            if node.get("job_id"):
                return node["job_id"]
            node = nodes.get(node.get("parent_id"))
        return None

    @staticmethod
    def _allocation_id(job_id, leaf):
        generation = len(leaf.get("history_task_ids", []))
        return "m-tree-" + hashlib.sha256((job_id + "\0" + leaf["id"] + "\0" + str(generation)).encode()).hexdigest()[:24]

    def _allocate(self, tree, leaf, agent, previous=None):
        if previous is None and leaf.get("history_task_ids"):
            previous, location = self._message(leaf["history_task_ids"][-1])
            if previous is None or location != "blocked" or claim_fencing.is_fenced(previous):
                raise TaskTreeError("continuation source must have a formally revoked claim")
        job_id = self._job_id(tree, leaf)
        if not job_id:
            nodes = {node["id"]: node for key in ("roots", "branches", "leaves") for node in tree[key]}
            root = leaf
            while root.get("parent_id") is not None:
                root = nodes[root["parent_id"]]
            request = "tree-" + hashlib.sha256(root["id"].encode()).hexdigest()[:24]
            job = jobs.start_job(self.cfg, self.cfg.director, root["goal"] or root["title"], request_id=request)
            job_id = job["id"]
            root["job_id"] = job_id
        job = jobs._load(self.cfg, job_id)
        self._allowed(job=job)
        identity = self._allocation_id(job_id, leaf)
        item = next((item for item in job["tasks"] if item["id"] == identity), None)
        if item:
            message = Message.from_dict(item["message"], strict=True)
            if message.to != agent:
                raise TaskTreeError("preallocated continuation belongs to another member")
        else:
            timeout_s = int(self.cfg.gates.get("task_timeout_s", 1800))
            if previous is not None:
                # A handoff is the same bounded unit of work. Reallocating it
                # must not grant a fresh task timeout to the successor.
                deadline = previous.deadline_ts
                if deadline is not None and job.get("deadline_ts") is not None:
                    deadline = min(deadline, job["deadline_ts"])
                next_attempt = previous.attempt + 1
            else:
                deadline = min(time.time() + timeout_s, job["deadline_ts"])
                next_attempt = 0
            from .artifact_versions import predecessor_refs
            dependency_refs = predecessor_refs(self.cfg, self.bus, tree, leaf)
            history = leaf.get("handoff_history", [])
            reason = history[-1]["reason"] if previous and history else ""
            checkpoint = leaf.get("handoff", "") if previous else ""
            message = self.bus.make(self.cfg.director, agent, "task", title=leaf["title"],
                                    body=previous.body if previous else leaf["goal"] or leaf["title"],
                                    handoff_note=checkpoint if checkpoint != reason else "",
                                    handoff_reason=reason,
                                    result_to=previous.result_to if previous else leaf.get("result_to") or job["coordinator"],
                                    reply_to=previous.id if previous else job["goal_id"],
                                    deadline_ts=deadline,
                                    budget_s=min(timeout_s, 1800, previous.budget_s) if previous is not None else min(timeout_s, 1800),
                                    expected_artifacts=previous.expected_artifacts if previous else leaf.get("expected_artifacts", []),
                                    refs=list(dict.fromkeys(([previous.id] + previous.refs if previous else [])
                                                            + dependency_refs)),
                                    attempt=next_attempt,
                                    hop=previous.hop if previous else 0, **jobs.job_binding(job))
            message.id = identity
            self._allowed(message, job)
            jobs.register_job_tasks(self.cfg, self.cfg.director, job_id, [message])
        leaf["task_id"], leaf["job_id"] = message.id, job_id
        self._write(tree)  # Binding before publication; the immutable job repairs crashes.
        self.bus.init()
        jobs._publish(self.bus, message.to_dict())
        if previous:
            self._audit_successor(leaf, message)
        return message

    def _audit_successor(self, leaf, message):
        if not leaf.get("history_task_ids"):
            return
        old_id = leaf["history_task_ids"][-1]
        if message.reply_to != old_id or old_id not in message.refs:
            raise TaskTreeError("continuation message does not bind its predecessor")
        if not any(event.get("event") == "task_tree_successor_bound"
                   and event.get("old_task_id") == old_id and event.get("task_id") == message.id
                   and event.get("leaf_id") == leaf["id"] and event.get("source") == "task_forest"
                   for event in self.bus.events()):
            self.bus.audit(event="task_tree_successor_bound", leaf_id=leaf["id"], job_id=message.job_id,
                           old_task_id=old_id, task_id=message.id, successor=message.to,
                           source="task_forest", reason=(leaf["handoff_history"][-1]["reason"] if leaf.get("handoff_history") else leaf["handoff"]))

    def claim_leaf(self, leaf_id, agent):
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            self._member(agent, ordinary_task=True)
            self.bus.check_work_slot(agent)
            view = self._leaf(self.snapshot(), leaf_id)
            self._allowed()
            if not view["claimable"] or agent not in view["eligible_agents"]:
                raise TaskTreeError(view["evidence_error"] or "leaf is owned, blocked or waiting for a dependency")
            tree = self._structure()
            leaf = self._leaf(tree, leaf_id)
            msg, location = self._message(leaf.get("task_id"))
            if msg is None and not leaf.get("task_id"):
                msg = self._allocate(tree, leaf, agent)
                location = "ready"
            elif msg is None and location == "missing":
                job_id = self._job_id(tree, leaf)
                job = jobs._load(self.cfg, job_id)
                item = next(item for item in job["tasks"] if item["id"] == leaf["task_id"])
                msg = Message.from_dict(item["message"], strict=True)
                self._allowed(msg, job)
                self._write(tree)
                self.bus.init()
                jobs._publish(self.bus, item["message"])
                location = "ready"
            if msg is None or location != "ready":
                raise TaskTreeError("original mailbox owns this task; use explicit handoff to change member")
            if msg.to != agent:
                if agent not in view["eligible_agents"]:
                    raise TaskTreeError("original mailbox owns this task; use explicit handoff to change member")
                if claim_fencing.record_epoch(msg) or msg.claimed_by or msg.status != "pending":
                    raise TaskTreeError("only never-claimed ready work may transfer on first pull")
                job = jobs.message_job(self.cfg, msg)
                self._allowed(job=job)
                old_path = self.bus.agents_dir / msg.to / "ready" / (msg.id + ".json")
                before = msg.to_dict()
                if not old_path.is_file() or self.bus.read(old_path).to_dict() != before:
                    raise TaskTreeError("ready task changed before transfer; preserve original evidence")
                msg.to = agent
                timeout_s = int(self.cfg.gates.get("task_timeout_s", 1800))
                timeout_s = min(timeout_s, msg.budget_s or timeout_s)
                msg.deadline_ts = min(time.time() + timeout_s, job["deadline_ts"]) if job else time.time() + timeout_s
                new_path = self.bus.agents_dir / agent / "ready" / (msg.id + ".json")
                new_path.parent.mkdir(parents=True, exist_ok=True)
                if new_path.exists():
                    raise TaskTreeError("transfer target already exists; preserve both records")
                committed = False
                try:
                    self.bus._write(new_path, msg)
                    self.bus.audit(event="task_transferred_on_claim", task_id=msg.id,
                                   from_agent=before["to"], to_agent=agent, source="task_forest",
                                   job_id=msg.job_id, leaf_id=leaf_id, claim_epoch_before=0,
                                   before_intent_sha256=jobs._routing_intent_sha256(before),
                                   after_intent_sha256=jobs._routing_intent_sha256(msg.to_dict()),
                                   deadline_ts=msg.deadline_ts)
                    old_path.unlink()
                    committed = True
                finally:
                    # Key write: roll back the new card on any failure, including
                    # unexpected exceptions, while the original ready card stays
                    # in its owner's mailbox. A crash between the two files remains
                    # fail-closed and requires explicit recovery; no second claim
                    # is granted.
                    if not committed and old_path.is_file():
                        try:
                            new_path.unlink(missing_ok=True)
                        except (PermissionError, FileNotFoundError):
                            # 资源清理类：删除失败时把残留卡改成非 *.json，
                            # 避免同一消息出现第二份可认领副本。
                            try:
                                new_path.replace(new_path.with_name(new_path.name + ".rollback-conflict"))
                            except (PermissionError, FileNotFoundError):
                                pass  # 资源清理类：两次清理都失败时保留残留，认领票据仍会拒绝二次领取。
            else:
                timeout_s = int(self.cfg.gates.get("task_timeout_s", 1800))
                if not msg.deadline_ts or time.time() > msg.deadline_ts:
                    msg.deadline_ts = time.time() + timeout_s
                    path = self.bus.agents_dir / agent / "ready" / (msg.id + ".json")
                    if path.exists():
                        try:
                            path.write_text(json.dumps(msg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                        except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
                            # Key write: an unrefreshed deadline must not be claimed as success.
                            raise TaskTreeError(
                                "无法刷新任务截止时间；认领已中止，原任务保持 ready 状态") from exc
            job = jobs.message_job(self.cfg, msg)
            self._allowed(msg, job)
            self._audit_successor(leaf, msg)
            return self.bus.claim(agent, self.bus.agents_dir / agent / "ready" / (msg.id + ".json"))

    @staticmethod
    def _build_leaf_dependency_graphs(snapshot):
        """构建完整叶子级依赖图（自动展开分支继承依赖 effective_dependencies、章节号及父分支下属叶子）。"""
        from collections import defaultdict, deque
        leaves = snapshot.get("leaves", [])
        leaf_map = {l["id"]: l for l in leaves if "id" in l}
        all_nodes = {}
        for k in ("roots", "branches", "leaves"):
            for n in snapshot.get(k, []):
                if "id" in n:
                    all_nodes[n["id"]] = n
        sec_to_id = {n["section_no"]: n["id"] for n in all_nodes.values() if n.get("section_no")}
        children_by_parent = defaultdict(list)
        for nid, n in all_nodes.items():
            pid = n.get("parent_id")
            if pid:
                children_by_parent[pid].append(nid)

        descendant_leaves_cache = {}
        def get_descendant_leaves(node_id):
            if node_id in descendant_leaves_cache:
                return descendant_leaves_cache[node_id]
            if node_id in leaf_map:
                res = {node_id}
            else:
                res = set()
                q = deque([node_id])
                seen = {node_id}
                while q:
                    curr = q.popleft()
                    for ch in children_by_parent.get(curr, []):
                        if ch not in seen:
                            seen.add(ch)
                            if ch in leaf_map:
                                res.add(ch)
                            else:
                                q.append(ch)
            descendant_leaves_cache[node_id] = res
            return res

        downstream_graph = defaultdict(set)
        upstream_graph = defaultdict(set)
        for l in leaves:
            lid = l["id"]
            raw_deps = l.get("effective_dependencies")
            if raw_deps is None:
                raw_deps = _inherited_dependencies(all_nodes, l)
            for dep in raw_deps:
                resolved_id = dep if dep in all_nodes else sec_to_id.get(dep)
                if not resolved_id:
                    continue
                for target_leaf_id in get_descendant_leaves(resolved_id):
                    if target_leaf_id != lid:
                        downstream_graph[target_leaf_id].add(lid)
                        upstream_graph[lid].add(target_leaf_id)
        return leaf_map, downstream_graph, upstream_graph

    def diagnose_deadlock(self, snapshot=None):
        """死锁诊断与根阻断节点提取 (Root Blocker Diagnostics)。

        当在途任务为 0、受阻任务 > 0 且无任何可认领任务时，判定为全流水线死锁。
        反向追溯拓扑 DAG（含分支继承依赖），找出自身前置依赖已全部交付但自身未交付的根阻断节点，
        并计算其各自直接/间接阻断的下游任务数量。
        """
        if snapshot is None:
            snapshot = self.snapshot()
        leaves = snapshot.get("leaves", [])
        if not leaves:
            return {"is_deadlocked": False, "root_blockers": []}

        in_flight = [l for l in leaves if l.get("state") in ("running", "claimed", "processing")
                     or (l.get("state") == "awaiting_audit" and not l.get("evidence_error"))]
        claimable = [l for l in leaves if l.get("claimable")]
        def ready(leaf):
            # Old snapshots lack dependency_ready; retain their prior behavior.
            return leaf.get("dependency_ready", leaf.get("state") == "delivered")

        unmet_leaves = [l for l in leaves if not ready(l)]

        is_deadlocked = (
            len(in_flight) == 0
            and len(claimable) == 0
            and len(unmet_leaves) > 0
            and not snapshot.get("stopped")
        )
        if not is_deadlocked:
            return {"is_deadlocked": False, "root_blockers": []}

        from collections import deque
        leaf_map, downstream_graph, upstream_graph = self._build_leaf_dependency_graphs(snapshot)

        def count_blocked_downstream(root_id):
            visited = set()
            queue = deque([root_id])
            while queue:
                curr = queue.popleft()
                for nxt in downstream_graph.get(curr, []):
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append(nxt)
            return sum(1 for vid in visited if not ready(leaf_map.get(vid, {})))

        root_blockers = []
        for l in unmet_leaves:
            lid = l["id"]
            upstreams = upstream_graph.get(lid, set())
            all_deps_delivered = all(ready(leaf_map.get(uid, {})) for uid in upstreams)
            if all_deps_delivered:
                blocked_count = count_blocked_downstream(lid)
                reason = (
                    l.get("evidence_error")
                    or l.get("blocked_reason")
                    or l.get("results")
                    or f"状态卡在 {l.get('state')}"
                )
                root_blockers.append({
                    "id": lid,
                    "section_no": l.get("section_no") or "",
                    "title": l.get("title") or lid,
                    "state": l.get("state"),
                    "evidence_error": l.get("evidence_error") or "",
                    "blocked_reason": reason,
                    "blocked_downstream_count": blocked_count,
                    "expected_artifacts": l.get("expected_artifacts") or []
                })

        root_blockers.sort(key=lambda x: -x["blocked_downstream_count"])
        return {
            "is_deadlocked": True,
            "root_blockers": root_blockers
        }

    def pass_leaf(self, leaf_id, reason, *, admin="admin", force=False):
        """记录解除依赖的管理员裁决，保留原始交付证据与审核要求。"""
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            if not isinstance(reason, str) or not reason.strip():
                raise TaskTreeError("放行必须提供明确的审核裁决理由 (--reason)")
            tree = self._structure()
            target = None
            for leaf in tree.get("leaves", []):
                if leaf.get("id") == leaf_id or leaf.get("section_no") == leaf_id:
                    target = leaf
                    break
            if not target:
                raise TaskTreeError(f"未找到指定的任务叶子节点: {leaf_id}")

            message_index = self._message_index()
            message, location = self._message(target.get("task_id"), message_index)
            if self._pass_requires_audit(target, message, location, message_index):
                raise TaskTreeError("该任务仍需独立审核，无法通过管理员放行替代审核。")
            if message is not None and location == "done":
                view = self._leaf(self.snapshot(), target["id"])
                if self._has_verified_delivery(view, view.get("evidence_error")):
                    raise TaskTreeError("该任务已有经核验的交付，无需管理员放行。")

            target["admin_override"] = {
                "admin": admin,
                "reason": reason.strip(),
                "timestamp": now_iso()
            }
            self._write(tree)

            self.bus.audit(
                event="leaf_passed_by_admin",
                leaf_id=target["id"],
                section_no=target.get("section_no"),
                admin=admin,
                reason=reason.strip(),
                source="admin_pass"
            )
            return target

    def reset_leaf(self, leaf_id, reason, *, admin="admin", owner_confirmed_inactive=False, expected_task_id=None, expected_owner=None, expected_claim_epoch=None):
        """安全重排：保留原作业期限、任务预算与重试限制。"""
        with task_lock(self.root, "task-forest", "structure"):
            if not isinstance(reason, str) or not reason.strip():
                raise TaskTreeError("重置节点必须提供明确的理由 (--reason)")
            tree = self._structure()
            target = None
            for leaf in tree.get("leaves", []):
                if leaf.get("id") == leaf_id or leaf.get("section_no") == leaf_id:
                    target = leaf
                    break
            if not target:
                raise TaskTreeError(f"未找到指定的任务叶子节点: {leaf_id}")

            job_id = self._job_id(tree, target)
            if job_id:
                try:
                    job_data = jobs._load(self.cfg, job_id)
                    deadline = job_data.get("deadline_ts")
                    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
                        raise TaskTreeError("原作业截止期限已到或无效；保留原记录并正式续接")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    if isinstance(exc, TaskTreeError):
                        raise
                    raise TaskTreeError("无法验证原作业期限；保留原任务记录") from exc

            old_task_id = target.get("task_id")
            if old_task_id:
                if len(target.get("handoff_history", [])) >= 100:
                    raise TaskTreeError("交接历史已达上限；保留原任务并先整理历史")
                if len(target.get("history_task_ids", [])) >= 1000 and old_task_id not in target.get("history_task_ids", []):
                    raise TaskTreeError("任务历史已达上限；保留原任务并先整理历史")
                # A first-hit index cannot establish unique ownership. Preserve
                # every copy if another mailbox/location has the same ID.
                copies = []
                if self.bus.agents_dir.exists():
                    for base in self.bus.agents_dir.iterdir():
                        if base.name.casefold() in {".workbuddy", "不要访问", "do not access"}:
                            continue
                        self._path_check(base)
                        for name in self._MAILBOX_LOCATIONS:
                            path = base / name / (old_task_id + ".json")
                            if os.path.lexists(path):
                                self._path_check(path)
                                copies.append(path)
                # The formal bus can reconcile a crash after blocked was
                # published but before the original active copy was removed.
                # Do not decide their validity here: the bus requires matching
                # audit intent, reason, and before/after hashes under its lock.
                same_member = len({path.parent.parent.name for path in copies}) == 1
                locations = {path.parent.name for path in copies}
                maybe_recovery = (len(copies) == 2 and same_member and
                                  locations in ({"blocked", "processing"}, {"blocked", "ready"}))
                if len(copies) != 1 and not maybe_recovery:
                    raise TaskTreeError("原任务记录缺失或存在多份冲突；保留当前节点，先核对")
                previous, location = self._message(old_task_id)
                if previous is None:
                    raise TaskTreeError("原任务记录缺失或无法读取；保留当前节点，先核对")
                review_state = (previous.supervision or {}).get("state")
                if previous.status in ("awaiting_audit", "rejected") or review_state in (
                        "awaiting_audit", "decision_recorded", "approved", "rejected"):
                    raise TaskTreeError("该任务仍有关联审核或驳回记录，请先走正式审核或重做流程")
                if location == "done":
                    raise TaskTreeError("已完成任务不能重置；请保留原有成果记录")
                if location == "processing" and owner_confirmed_inactive is not True:
                    raise TaskTreeError("请明确确认原执行成员已停止，再重置任务")
                if previous is not None and location in ("ready", "processing", "blocked"):
                    release = getattr(self.bus, "relinquish_claim", None)
                    if release is None:
                        raise TaskTreeError("缺少正规释放接口；原任务保留")
                    try:
                        with task_lock(self.root, previous.to, previous.id):
                            processing = self.bus.agents_dir / previous.to / "processing" / (previous.id + ".json")
                            self._path_check(processing)
                            if processing.is_file():
                                live = self.bus.read(processing)
                                if (expected_task_id != live.id or expected_owner != live.to or
                                        type(expected_claim_epoch) is not int or expected_claim_epoch != live.claim_epoch):
                                    raise TaskTreeError("原任务或认领状态已变更，请重新核对并确认")
                            release(previous.to, previous.id, reason=f"[管理员重置] {reason.strip()}",
                                    owner_confirmed_inactive=owner_confirmed_inactive is True)
                    except (FileNotFoundError, PermissionError, ValueError, KeyError, RuntimeError, BusError, TaskTreeError) as exc:
                        raise TaskTreeError(f"无法安全释放原任务：{exc}") from exc
                if previous is not None:
                    hist_ids = target.setdefault("history_task_ids", [])
                    if previous.id not in hist_ids:
                        hist_ids.append(previous.id)
                    prev_conv = (previous.claim_conversation_id
                                 or (claim_fencing.conversation_id_for_claim(previous.claim_id)
                                     if previous.claim_id else ""))
                    prev_agent = previous.claimed_by or previous.claim_agent or previous.to
                    target.setdefault("handoff_history", []).append({
                        "task_id": previous.id,
                        "agent": prev_agent,
                        "conversation_id": prev_conv,
                        "timestamp": now_iso(),
                        "reason": f"[管理员重置] {reason.strip()}",
                        "owner_confirmed_inactive": owner_confirmed_inactive is True,
                    })
                target.pop("task_id", None)

            target["admin_override"] = {
                "mode": "reset",
                "admin": admin,
                "reason": reason.strip(),
                "timestamp": now_iso(),
                "reset_generation": len(target.get("history_task_ids", [])),
            }
            # Record intent before changing the tree; if the final audit fails,
            # tell the operator the mutation already happened instead of
            # returning an ambiguous failure that invites a second reset.
            self.bus.audit(event="leaf_reset_intent", leaf_id=target["id"],
                           old_task_id=old_task_id, admin=admin, reason=reason.strip())
            self._write(tree)
            try:
                self.bus.audit(
                    event="leaf_reset_by_admin", leaf_id=target["id"],
                    section_no=target.get("section_no"), old_task_id=old_task_id,
                    admin=admin, reason=reason.strip(), source="admin_reset",
                )
            except OSError as exc:
                raise TaskTreeError("重置已生效，但完成记录写入失败；请刷新并检查审计记录，勿重复操作") from exc
            return target

    def claim_next(self, agent, tier: str = None):
        """Autonomous Pull: 智能体自主从就绪任务池中抢单认领下一个任务。

        硬性安全约束：
        1. 单工位在途上限（In-flight limit = 1）：若该 agent 当前已有 processing 任务，禁止重复抢单；
        2. 原子互斥：在 task_lock 下检索并原子认领，防止多 Agent 竞态并发认领同一任务；
        3. 依赖前置：严格要求 view["claimable"] 为 True 且 agent in view["eligible_agents"]；
        4. 拓扑序与成本优先级：优先认领与工位成本/复杂度偏好匹配、且能够解锁最多下游任务的关键节点。
        """
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            self._member(agent, ordinary_task=True)

            # 1. 检查在途任务上限 (In-flight limit = 1)
            processing = self.bus.active_work(agent)
            if processing:
                current_msg = processing[0]
                return {
                    "status": "already_has_processing",
                    "message": current_msg,
                    "leaf": None,
                    "detail": f"当前工位 {agent} 已有在途任务正在执行中（单工位在途上限为 1），请先完成交付后再认领新任务"
                }

            # 2. 检查就绪池
            snapshot = self.snapshot()
            if snapshot.get("stopped"):
                raise TaskTreeError("项目当前处于暂停状态（STOP），暂不能认领新任务")

            candidates = []
            for leaf in snapshot.get("leaves", []):
                if leaf.get("claimable") and agent in leaf.get("eligible_agents", []):
                    state = leaf.get("state")
                    if state in ("queued", "missing") or (state not in ("delivered", "awaiting_receipt", "running", "rejected", "claimed") and not leaf.get("task_id")):
                        candidates.append(leaf)

            if not candidates:
                diag = self.diagnose_deadlock(snapshot)
                detail = ("当前任务树公共池中暂无满足前置依赖的就绪任务。claim-next 只看任务树；"
                          f"别人直接投给你的任务请用 agentbus inbox --mine --caller {agent} 查看")
                if diag["is_deadlocked"]:
                    blockers_summary = "、".join(
                        f"§{b['section_no']} {b['title']}" for b in diag["root_blockers"][:2]
                    )
                    detail = (
                        f"流水线陷入等待死锁！无在途执行任务，共有 {len(diag['root_blockers'])} 个根节点阻断全盘（如：{blockers_summary}）"
                    )
                return {
                    "status": "no_available_tasks",
                    "message": None,
                    "leaf": None,
                    "diagnostics": diag,
                    "detail": detail
                }

            # 3. 启发式关键路径排序：优先认领能够解锁最多下游任务（含分支继承依赖）的关键节点，促进团队流水线并发最大化
            leaf_map, downstream_graph, _ = self._build_leaf_dependency_graphs(snapshot)

            def _candidate_weight(leaf_node):
                leaf_id = leaf_node["id"]
                blocking_count = sum(
                    1 for vid in downstream_graph.get(leaf_id, set())
                    if leaf_map.get(vid, {}).get("state") != "delivered"
                )
                sec = str(leaf_node.get("section_no") or "")
                sec_parts = [int(p) for p in sec.split(".") if p.isdigit()] or [999]
                explicit_pri = int(leaf_node.get("priority", 999))
                tier_pref = 0
                if tier:
                    leaf_tier = leaf_node.get("cost_tier", "standard")
                    tier_pref = -1 if leaf_tier == tier else 1
                return (tier_pref, -blocking_count, explicit_pri, sec_parts)

            candidates.sort(key=_candidate_weight)
            target_leaf = candidates[0]
            claimed_msg = self.claim_leaf(target_leaf["id"], agent)

            self.bus.audit(
                event="task_tree_autonomous_claimed",
                leaf_id=target_leaf["id"],
                job_id=claimed_msg.job_id,
                task_id=claimed_msg.id,
                agent=agent,
                source="task_forest"
            )

            return {
                "status": "claimed",
                "message": claimed_msg,
                "leaf": target_leaf,
                "detail": f"成功自主抢单认领任务 {claimed_msg.id} [{target_leaf.get('title')}]"
            }

    def dispatch_leaf(self, leaf_id, agent):

        """Publish a project leaf to one member's ready inbox without claiming it."""
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            self._member(agent)
            view = self._leaf(self.snapshot(), leaf_id)
            self._allowed()
            if not view["claimable"] or agent not in view["eligible_agents"]:
                raise TaskTreeError(view["evidence_error"] or "leaf is owned, blocked or waiting for a dependency")
            tree = self._structure()
            leaf = self._leaf(tree, leaf_id)
            msg, location = self._message(leaf.get("task_id"))
            published = False
            if msg is None and not leaf.get("task_id"):
                msg = self._allocate(tree, leaf, agent)
                published = True
            elif msg is None and location == "missing":
                job_id = self._job_id(tree, leaf)
                job = jobs._load(self.cfg, job_id)
                item = next((item for item in job["tasks"] if item["id"] == leaf["task_id"]), None)
                if item is None:
                    raise TaskTreeError("bound task intent is missing; preserve the leaf for recovery")
                msg = Message.from_dict(item["message"], strict=True)
                if msg.to != agent:
                    raise TaskTreeError("bound task is assigned to another member")
                self._allowed(msg, job)
                self._write(tree)
                self.bus.init()
                jobs._publish(self.bus, item["message"])
                published = True
            elif msg is not None and location == "ready":
                if msg.to != agent:
                    raise TaskTreeError("original mailbox owns this task; use explicit handoff to change member")
                return msg
            else:
                raise TaskTreeError("task is already owned or is not ready for dispatch")
            if msg.to != agent:
                raise TaskTreeError("original mailbox owns this task; use explicit handoff to change member")
            job = jobs.message_job(self.cfg, msg)
            self._allowed(msg, job)
            if published:
                self.bus.audit(event="task_tree_dispatched", leaf_id=leaf["id"], job_id=msg.job_id,
                               task_id=msg.id, recipient=agent, source="task_forest")
            return msg

    def checkpoint_leaf(self, leaf_id, agent, note, ticket=None, progress=None):
        if not isinstance(note, str) or not note.strip() or len(note) > 10000:
            raise TaskTreeError("checkpoint must be bounded nonempty text")
        if progress is not None and (type(progress) is not int or not 0 <= progress <= 100):
            raise TaskTreeError("checkpoint progress must be an integer from 0 to 100")
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            self._member(agent)
            tree = self._structure()
            leaf = self._leaf(tree, leaf_id)
            task_id = leaf.get("task_id")
            msg, location = self._message(task_id)
            if task_id:
                if msg is None:
                    raise TaskTreeError("assigned task is unavailable; checkpoint only its active processing record")
                if location != "processing":
                    raise TaskTreeError("formal task must be claimed and in processing before checkpoint")
                if msg.to != agent:
                    raise TaskTreeError("only the current leaf owner can checkpoint assigned work")
                self.bus.check_claim(agent, msg, ticket)
            # Unbound leaves keep the original tree-only handoff note behavior;
            # once a formal task_id exists, the claim ticket above is mandatory.
            leaf["handoff"] = note.strip()
            if progress is not None:
                leaf["progress_percent"] = progress
            self._write(tree)
            self.bus.init()
            self.bus.audit(event="task_tree_checkpoint", leaf_id=leaf["id"], task_id=leaf.get("task_id"),
                           agent=agent, note=note.strip())
        return self._leaf(self.snapshot(), leaf_id)

    def _release_stopped_assignment(self, previous, *, reason, expected_task_id,
                                    expected_owner, expected_claim_epoch):
        """Bind the stopped-owner assertion to the exact task and generation."""
        if (expected_task_id != previous.id or expected_owner != previous.to or
                type(expected_claim_epoch) is not int or expected_claim_epoch != previous.claim_epoch):
            raise TaskTreeError("原任务或认领状态已变更，请重新核对并确认")
        with task_lock(self.root, previous.to, previous.id):
            observed = False
            for location in ("processing", "ready", "blocked"):
                path = self.bus.agents_dir / previous.to / location / (previous.id + ".json")
                self._path_check(path)
                if path.is_file():
                    observed = True
                    live = self.bus.read(path)
                    if (live.id != expected_task_id or live.to != expected_owner or
                            live.claim_epoch != expected_claim_epoch):
                        raise TaskTreeError("原任务或认领状态已变更，请重新核对并确认")
            if not observed:
                raise TaskTreeError("原任务已不在活动信箱，请重新核对")
            return self.bus.relinquish_claim(previous.to, previous.id, reason=reason,
                                             owner_confirmed_inactive=True)

    def handoff_leaf(self, leaf_id, agent, ticket=None, reason="", successor=None,
                     owner_confirmed_inactive=False, expected_task_id=None,
                     expected_owner=None, expected_claim_epoch=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 10000:
            raise TaskTreeError("handoff needs a bounded checkpoint/reason")
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            self._member(agent)
            if successor is not None:
                self._member(successor)
            self._allowed()
            tree = self._structure()
            leaf = self._leaf(tree, leaf_id)
            previous, location = self._message(leaf.get("task_id"))
            if previous is None:
                raise TaskTreeError("handoff needs an existing assigned leaf")
            presented = claim_fencing.normalize(ticket)
            if presented is not None and presented.task_id != previous.id:
                raise TaskTreeError("original ticket belongs to another task; cannot release its successor")
            if location == "done":
                raise TaskTreeError("delivered history cannot be reassigned; add a new leaf")
            if location == "processing" and previous.to != agent and owner_confirmed_inactive is not True:
                raise TaskTreeError("only the owning executor can release its claim without inactive confirmation")
            if location == "processing" and owner_confirmed_inactive is not True:
                self.bus.check_claim(previous.to, previous, ticket)
            job = jobs.message_job(self.cfg, previous)
            self._allowed(job=job)
            # Validate the successor's retry budget before releasing any claim.
            if previous.attempt + 1 > int(self.cfg.gates.get("max_retries", 2)):
                if not (owner_confirmed_inactive and successor and successor != previous.to):
                    raise TaskTreeError("handoff would exceed the original retry limit")
            history = leaf.get("handoff_history", [])
            if len(history) >= 100:
                raise TaskTreeError("handoff history is full; preserve the existing record before another transfer")
            previous_conversation_id = (previous.claim_conversation_id
                                        or (claim_fencing.conversation_id_for_claim(previous.claim_id)
                                            if previous.claim_id else ""))
            previous_agent = previous.claimed_by or previous.claim_agent or previous.to
            release = getattr(self.bus, "relinquish_claim", None)
            if release is None:
                raise TaskTreeError("formal Bus claim release is unavailable; original claim is preserved")
            has_binding = any(value is not None for value in
                              (expected_task_id, expected_owner, expected_claim_epoch))
            if owner_confirmed_inactive is True and (location in ("processing", "blocked") or has_binding):
                self._release_stopped_assignment(previous, reason=reason.strip(),
                    expected_task_id=expected_task_id, expected_owner=expected_owner,
                    expected_claim_epoch=expected_claim_epoch)
            else:
                release(previous.to, previous.id, ticket=ticket, reason=reason.strip(),
                        owner_confirmed_inactive=owner_confirmed_inactive)
            task_history = leaf.setdefault("history_task_ids", [])
            if previous.id not in task_history:
                task_history.append(previous.id)
            leaf.setdefault("handoff_history", []).append({
                "task_id": previous.id,
                "agent": previous_agent,
                "conversation_id": previous_conversation_id,
                "timestamp": now_iso(),
                "reason": reason.strip(),
                "owner_confirmed_inactive": owner_confirmed_inactive is True,
            })
            leaf["handoff"] = leaf.get("handoff") or reason.strip()
            # An explicit successor becomes a routed task; otherwise leave this
            # project leaf open for whichever eligible member claims it next.
            leaf.pop("task_id", None)
            self._write(tree)
            if successor:
                message = self._allocate(tree, leaf, successor, previous=previous)
                self.bus.audit(event="task_tree_handoff", leaf_id=leaf["id"], old_task_id=previous.id,
                               task_id=message.id, agent=agent, successor=successor,
                               owner_confirmed_inactive=owner_confirmed_inactive)
            else:
                # Preserve continuation's original bounds in its immutable source
                # task, looked up again during next allocation.
                self.bus.audit(event="task_tree_handoff", leaf_id=leaf["id"], old_task_id=previous.id,
                               task_id=None, agent=agent, successor=None,
                               owner_confirmed_inactive=owner_confirmed_inactive)
        return self._leaf(self.snapshot(), leaf_id)

    def generate_takeover_capsule(self, leaf, agent) -> dict:
        """生成接管提示词胶囊（Takeover Capsule），包含前序断点与开箱即用自驱动提示词。"""
        leaf_id = leaf.get("id", "")
        title = leaf.get("title", "")
        section = leaf.get("section_no", "")
        goal = leaf.get("goal") or title
        artifacts = leaf.get("expected_artifacts") or []
        dependencies = leaf.get("dependencies") or []
        handoff_note = leaf.get("handoff") or "无特定断点备忘"
        history = leaf.get("handoff_history") or []
        task_id = leaf.get("task_id") or ""
        ws_path = str(self._workspace_root).replace('\\', '/')
        
        prompt_lines = [
            f"# AgentBus 任务接管与自驱动执行胶囊",
            "",
            f"你已被指定接手任务节点：[{section}] {title}（ID: `{leaf_id}`）。",
            f"请严格遵循 `AGENTS.md` 公共契约推进任务，不可擅自破坏拓扑结构与独占锁机制。",
            "",
            f"## 1. 任务背景与目标",
            f"- **工作区路径**：`{ws_path}`",
            f"- **任务 ID**：`{task_id}`",
            f"- **节点标题**：{title}",
            f"- **执行目标**：{goal}",
        ]
        if dependencies:
            prompt_lines.append(f"- **前置依赖**：{', '.join(dependencies)}")
        if artifacts:
            prompt_lines.append(f"- **预期产物**：{', '.join(artifacts)}")
        
        prompt_lines.extend([
            "",
            f"## 2. 前任断点与交接备忘",
            f"- **当前断点说明**：{handoff_note}",
        ])
        if history:
            last = history[-1]
            prompt_lines.append(f"- **换人原因**：{last['reason']}")
            prompt_lines.append(f"- **上一任执行者**：`{last.get('agent', '未知')}`（移交时间: {last.get('timestamp', '未知')}）")
        
        prompt_lines.extend([
            "",
            f"## 3. 标准执行流水线",
            f"1. **首先认领任务**：",
            f"   ```bash",
            f"   agentbus claim --workspace \"{ws_path}\" --agent {agent} --id {task_id}",
            f"   ```",
            f"2. **在任务声明的目录内推进工作**：",
            f"   - 继承上述前任断点继续编码与验证；",
            f"   - 严格杜绝修改未经租约声明的文件；",
            f"   - 认领输出里的 claim_epoch / claim_id 是本次票据，长任务心跳与交付都要带上：",
            f"     `agentbus heartbeat --workspace \"{ws_path}\" --agent {agent} --id {task_id} --claim-epoch <N> --claim-id <票据>`",
            f"3. **完成交付与回执**：",
            f"   - 把产物写到上面的预期产物路径，运行定向测试验证；",
            f"   - 编辑认领时生成的回执草稿（删除 AGENTBUS_DRAFT_UNFINISHED 一行并如实填写），然后提交：",
            f"     ```bash",
            f"     agentbus answer --workspace \"{ws_path}\" --agent {agent} --body-file <回执草稿路径> --auto-manifest",
            f"     ```",
            f"   - 系统自动核对产物字节数与 SHA256，无需手算。",
            f"4. **【刚性准则】自驱动工蜂连续工作循环（Autonomous Worker Loop）**：",
            f"   - 交付本任务后，**严禁直接停止工作或退出会话**！",
            f"   - 立即检查是否仍有分派给你的就绪任务：",
            f"     ```bash",
            f"     agentbus tree status --workspace \"{ws_path}\"",
            f"     ```",
            f"     并领取下一个就绪任务（同时看一眼信箱 `agentbus inbox --mine --caller {agent}`）：",
            f"     ```bash",
            f"     agentbus claim-next --workspace \"{ws_path}\" --agent {agent}",
            f"     ```",
            f"   - 若发现任何属于你的待办任务，必须**立即连续认领并执行**，循环往复，直到信箱与就绪队列彻底清空！",
            f"   - 唯有当所有归属于你的任务均已达到 `delivered`（或处于等待前序依赖的 `blocked` 状态）时，方可进行最终总结收工。",
        ])
        prompt_text = "\n".join(prompt_lines)
        return {
            "leaf_id": leaf_id,
            "section_no": section,
            "title": title,
            "agent": agent,
            "task_id": task_id,
            "handoff_note": handoff_note,
            "expected_artifacts": artifacts,
            "prompt": prompt_text,
        }

    def takeover_leaf(self, leaf_id, new_agent, reason="", force=False, *,
                      expected_task_id=None, expected_owner=None, expected_claim_epoch=None):
        """平滑安全接管任务：
        当原 Agent 离线、超时、报错挂起或因环境限制无法继续时，允许指定成员接管。
        自动完成旧认领释放、断点保留、独占锁迁移与新代次分配，并生成专属自驱动接手指令。
        """
        if type(force) is not bool:
            raise TaskTreeError('force must be a boolean stopped-owner confirmation')
        if not isinstance(reason, str) or len(reason) > 10000:
            raise TaskTreeError("takeover reason must be text within 10000 characters")
        reason = reason.strip() or f"由 {new_agent} 接管继续推进任务"
        with task_lock(self.root, "task-forest", "structure"):
            self._refresh_config()
            self._member(new_agent)
            self._allowed()
            tree = self._structure()
            leaf = self._leaf(tree, leaf_id)
            if leaf.get("status") == "delivered":
                raise TaskTreeError("已交付的节点不可接管")
            
            previous, location = self._message(leaf.get("task_id"))
            has_binding = any(value is not None for value in
                              (expected_task_id, expected_owner, expected_claim_epoch))
            if has_binding and (previous is None or previous.id != expected_task_id or
                    previous.to != expected_owner or type(expected_claim_epoch) is not int or
                    previous.claim_epoch != expected_claim_epoch):
                raise TaskTreeError("原任务或认领状态已变更，请重新核对并确认")
            if previous is not None:
                old_agent = previous.to
                if location == "done":
                    raise TaskTreeError("已完成任务不可接管")
                
                if location in ("processing", "blocked"):
                    if force is not True:
                        raise TaskTreeError(f'必须显式确认原成员 {old_agent} 的会话已停止；心跳过期、受阻或同成员都不是停止证明')
                    
                    release = getattr(self.bus, "relinquish_claim", None)
                    if release is None:
                        raise TaskTreeError("旧认领缺少正规释放接口，接管已中止")
                    try:
                        self._release_stopped_assignment(previous, reason=reason,
                            expected_task_id=expected_task_id, expected_owner=expected_owner,
                            expected_claim_epoch=expected_claim_epoch)
                    except Exception as exc:
                        # Any release failure must abort the takeover and keep the old
                        # claim; the contract is asserted by test_task_takeover.
                        raise TaskTreeError("旧认领释放失败，接管已中止，未派发新任务") from exc
                    
                    task_history = leaf.setdefault("history_task_ids", [])
                    if previous.id not in task_history:
                        task_history.append(previous.id)
                    
                    previous_conversation_id = (previous.claim_conversation_id
                                                or (claim_fencing.conversation_id_for_claim(previous.claim_id)
                                                    if previous.claim_id else ""))
                    takeover_reason = f"[接管: 由 {new_agent} 接替] {reason}"
                    leaf.setdefault("handoff_history", []).append({
                        "task_id": previous.id,
                        "agent": old_agent,
                        "conversation_id": previous_conversation_id,
                        "timestamp": now_iso(),
                        "reason": takeover_reason,
                        "owner_confirmed_inactive": True,
                    })
                    leaf["handoff"] = leaf.get("handoff") or reason
                    leaf.pop("task_id", None)
                    self._write(tree)
                    
                    message = self._allocate(tree, leaf, new_agent, previous=previous)
                    self.bus.audit(event="task_tree_takeover", leaf_id=leaf["id"],
                                   old_task_id=previous.id, task_id=message.id,
                                   old_agent=old_agent, new_agent=new_agent,
                                   reason=reason, source="task_forest")
                elif location == "ready":
                    if old_agent != new_agent:
                        with task_lock(self.root, previous.to, previous.id):
                            processing = self.bus.agents_dir / previous.to / "processing" / (previous.id + ".json")
                            self._path_check(processing)
                            if processing.exists():
                                raise TaskTreeError("任务已被认领，请重新核对并确认")
                            transferred = self.handoff_leaf(leaf['id'], new_agent,
                                reason=reason, successor=new_agent)
                        message = self.bus.find_message(transferred['task_id'])
                        if message is None:
                            raise TaskTreeError('正式转交已登记但新任务无法读取；保留接手历史供恢复')
                        self.bus.audit(event="task_tree_takeover", leaf_id=leaf["id"],
                                       old_task_id=previous.id, task_id=message.id,
                                       old_agent=old_agent, new_agent=new_agent,
                                       reason=reason, source="task_forest")
                    else:
                        message = previous
                else:
                    message = self._allocate(tree, leaf, new_agent, previous=previous)
            else:
                message = self._allocate(tree, leaf, new_agent)
                self.bus.audit(event="task_tree_takeover", leaf_id=leaf["id"],
                               task_id=message.id, old_agent="", new_agent=new_agent,
                               reason=reason, source="task_forest")
        
        snap_leaf = self._leaf(self.snapshot(), leaf_id)
        capsule = self.generate_takeover_capsule(snap_leaf, new_agent)
        return {
            "ok": True,
            "leaf": snap_leaf,
            "new_task_id": message.id,
            "agent": new_agent,
            "capsule": capsule
        }

    def ensure_product_forest(self):
        """Import the existing eight deliveries, keeping their actual evidence."""
        if self.path.exists():
            return self.snapshot()
        if not (self.root / "jobs" / (_PRODUCT_JOB + ".json")).is_file():
            return self.snapshot()
        records = {record["id"]: record for record in self._job_records({_PRODUCT_JOB})}
        if _PRODUCT_JOB not in records:
            raise TaskTreeError("current product convergence job is unreadable; original evidence preserved")
        job = records[_PRODUCT_JOB]
        tasks = {item["id"]: item for item in job["tasks"]}
        tree = {"schema_version": 1,
                "roots": [{"id": "project", "parent_id": None, "title": "AgentBus.exe 发布",
                           "goal": "交付可续接的任务树森林工作台与正式 AgentBus.exe", "job_id": _PRODUCT_JOB}],
                "branches": [], "leaves": []}
        for identity, title in (("desktop", "桌面 UI 看板树"), ("skills", "技能与计划树"),
                                ("reliability", "可靠性与证据树"), ("members", "成员与记忆树"),
                                ("release", "集成打包树")):
            tree["branches"].append({"id": identity, "parent_id": "project", "title": title})
        for identity, branch, title, task_id in _PRODUCT_TASKS:
            item = tasks.get(task_id)
            if not item:
                raise TaskTreeError("existing convergence job lacks a required delivery task")
            tree["leaves"].append({"id": identity, "parent_id": branch, "title": title,
                                   "goal": title + "；复用现有任务约定的实现、产物与验收范围。", "task_id": task_id,
                                   "job_id": _PRODUCT_JOB,
                                   "handoff": "核对该节点现有产物及正式回执，复用已完成实现后继续集成。"})
        tree["leaves"].extend([
            {"id": "CX-INTEGRATE", "parent_id": "release", "title": "任务树原型与源码集成",
             "goal": "归拢八项候选，完成任务树主视图、动态接力接口及基础回归。",
             "handoff": "在当前 integration 续接各模块；已有作者交付可按用户授权继续集成。"},
            {"id": "CX-RELEASE", "parent_id": "release", "title": "基础回归与正式 EXE",
             "goal": "完成当前候选的基础回归并构建包含任务树原型的 AgentBus.exe。",
             "dependencies": ["CX-INTEGRATE"],
             "handoff": "从集成成果继续构建，记录构建路径、摘要和实际验收范围。"},
        ])
        return self.replace_structure(tree)


def snapshot(cfg):
    return TaskForest(cfg).snapshot()


def claim_leaf(cfg, leaf_id, agent):
    return TaskForest(cfg).claim_leaf(leaf_id, agent)


def handoff_leaf(cfg, leaf_id, agent, ticket=None, reason="", successor=None,
                 owner_confirmed_inactive=False, expected_task_id=None,
                 expected_owner=None, expected_claim_epoch=None):
    return TaskForest(cfg).handoff_leaf(leaf_id, agent, ticket=ticket, reason=reason,
        successor=successor, owner_confirmed_inactive=owner_confirmed_inactive,
        expected_task_id=expected_task_id, expected_owner=expected_owner,
        expected_claim_epoch=expected_claim_epoch)


def checkpoint_leaf(cfg, leaf_id, agent, note, ticket=None, progress=None):
    return TaskForest(cfg).checkpoint_leaf(leaf_id, agent, note, ticket=ticket,
                                           progress=progress)


def replace_structure(cfg, tree):
    return TaskForest(cfg).replace_structure(tree)


def seed_leaf(cfg, leaf, **kwargs):
    return TaskForest(cfg).seed_leaf(leaf, **kwargs)


def ensure_product_forest(cfg):
    return TaskForest(cfg).ensure_product_forest()
