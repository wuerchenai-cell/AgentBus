"""Local, immutable delivery provenance for dependent artifact revisions.

Source: Codex audit remediation, 2026-11-04. This is filesystem evidence,
not authentication against a user who can replace the entire control store.
Historical receipts are never rewritten. A replacement needs a dependency
binding captured in the original task, a committed receipt, and current bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid

from .config import ConfigError, load
from .workspace_files import WorkspaceFiles

_PREFIX = "agentbus-artifact-predecessor-v1:"
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SOURCE_KEYS = ("id", "sender", "to", "type", "body", "title", "reply_to", "result_to",
                "job_id", "plan_id", "plan_sha256", "expected_artifacts", "refs",
                "claim_epoch", "claim_id", "claim_agent", "claim_conversation_id")
_RESULT_KEYS = ("id", "ts", "sender", "to", "type", "title", "body", "reply_to", "result_to",
                "job_id", "plan_id", "plan_sha256", "artifacts", "exit_code", "duration_s",
                "source_claim_epoch", "source_claim_id", "source_conversation_id", "supervision")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _projection(message, keys):
    return {key: getattr(message, key) for key in keys}


def receipt_digest(message):
    # Receipt inbox lifecycle may change; its delivery contents must not.
    return _digest(_projection(message, _RESULT_KEYS))


def _safe_path(path, workspace):
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("artifact version evidence leaves its workspace")
    for node in (path, *path.parents):
        if node.name.casefold() in {".workbuddy", "不要访问", "do not access"}:
            raise ValueError("forbidden artifact version evidence path")
        if node.exists() or node.is_symlink():
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("artifact version evidence cannot use links")
        if node == workspace:
            break
    return path


def _predecessors(source):
    predecessors = []
    for ref in source.refs:
        if isinstance(ref, str) and ref.startswith(_PREFIX):
            row = json.loads(ref[len(_PREFIX):])
            if (not isinstance(row, dict) or set(row) != {"task_id", "result_id", "receipt_sha256"}
                    or not all(isinstance(row[key], str) for key in row)
                    or not _SAFE.fullmatch(row["task_id"]) or not _SAFE.fullmatch(row["result_id"])
                    or not re.fullmatch("[a-f0-9]{64}", row["receipt_sha256"])):
                raise ValueError("invalid artifact predecessor binding")
            if row["task_id"] == source.id or row in predecessors:
                raise ValueError("cyclic or duplicate artifact predecessor binding")
            predecessors.append(row)
    return predecessors


def predecessor_refs(cfg, bus, tree, leaf):
    """Capture real transitive dependencies before the task is published.

    The resulting refs are part of the job's original immutable task intent.
    Merely declaring the same output path on another branch grants no edge.
    """
    from . import jobs
    nodes = {node["id"]: node for key in ("roots", "branches", "leaves") for node in tree[key]}
    leaf_ids = {node["id"] for node in tree["leaves"]}
    parents = {}
    for node in nodes.values():
        parents.setdefault(node.get("parent_id"), []).append(node["id"])
    found, active = set(), set()

    def visit(identity):
        if identity in active:
            raise ValueError("cyclic artifact dependency")
        if identity in found:
            return
        active.add(identity)
        node = nodes[identity]
        current = node
        while current is not None:
            for dep in current.get("dependencies", []):
                visit(dep)
            current = nodes.get(current.get("parent_id"))
        if identity not in leaf_ids:
            for child in parents.get(identity, []):
                visit(child)
        active.remove(identity)
        found.add(identity)

    current = leaf
    while current is not None:
        for dependency in current.get("dependencies", []):
            visit(dependency)
        current = nodes.get(current.get("parent_id"))
    refs = []
    for identity in sorted(found & leaf_ids):
        task_id = nodes[identity].get("task_id")
        task, location = jobs._message(bus, task_id)
        if task is None or location != "done" or task.status != "done":
            continue  # No administrative pass can manufacture a version edge.
        results, error = jobs._receipts(bus, task)
        if error:
            continue
        result = results[0]
        row = {"task_id": task.id, "result_id": result.id, "receipt_sha256": receipt_digest(result)}
        refs.append(_PREFIX + json.dumps(row, sort_keys=True, separators=(",", ":")))
    return refs


def record_delivery(bus, source, result):
    """Called after posting the receipt, immediately before terminal finish.

    Failure prevents terminal completion. A pre-finish crash leaves provenance
    that readers refuse until the real source task is durably done.
    """
    from .bus import BusError
    from . import jobs
    if source.type != "task" or result.exit_code not in (None, 0) or not result.artifacts:
        return
    if bus.root.name != ".agentbus" or not (bus.root / "config.json").is_file():
        # Standalone legacy Bus instances have no project/output directory.
        # Version-bound tasks cannot silently downgrade to that compatibility mode.
        if any(isinstance(ref, str) and ref.startswith(_PREFIX) for ref in source.refs):
            raise BusError("artifact version binding requires its project configuration")
        return
    try:
        cfg = load(bus.root.parent)
        if cfg.workspace.resolve() != bus.root.parent.resolve():
            raise ValueError("artifact version workspace mismatch")
        results, error = jobs._receipts(bus, source)
        if error or len(results) != 1 or receipt_digest(results[0]) != receipt_digest(result):
            raise ValueError(error or "posted receipt differs from the delivery")
        if WorkspaceFiles(cfg.output_root).verify_artifacts(result.artifacts)["verdict"] != "match":
            raise ValueError("artifact bytes changed before version recording")
        if not _SAFE.fullmatch(source.id):
            raise ValueError("invalid artifact version task id")
        record = {"schema_version": 1, "workspace": str(cfg.workspace.resolve()),
                  "output_root": str(cfg.output_root.resolve()), "task_id": source.id,
                  "result_id": result.id, "source_sha256": _digest(_projection(source, _SOURCE_KEYS)),
                  "receipt_sha256": receipt_digest(result), "artifacts": result.artifacts,
                  "predecessors": _predecessors(source)}
        directory = _safe_path(bus.root / "artifact-versions", cfg.workspace)
        directory.mkdir(exist_ok=True)
        target = _safe_path(directory / (source.id + ".json"), cfg.workspace)
        if target.exists():
            if json.loads(target.read_text(encoding="utf-8")) != record:
                raise ValueError("existing artifact version evidence conflicts; preserve it for review")
            return
        staging = directory / ("." + source.id + "." + uuid.uuid4().hex + ".tmp")
        try:
            with staging.open("x", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(staging, target)  # Atomic create without replacing existing history.
            except FileExistsError:
                if json.loads(target.read_text(encoding="utf-8")) != record:
                    raise ValueError("concurrent artifact version evidence conflicts")
        finally:
            staging.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, KeyError, ConfigError) as exc:
        raise BusError(f"artifact version evidence could not be committed: {exc}") from exc


class ArtifactVersions:
    """One observation-scoped verifier; never writes or upgrades old evidence."""
    def __init__(self, cfg, bus=None):
        from .bus import Bus
        self.cfg = cfg
        self.bus = bus or Bus(cfg.workspace / ".agentbus")
        self.directory = self.bus.root / "artifact-versions"
        self.files = WorkspaceFiles(cfg.output_root)
        self._proofs = {}
        self._all_ids = None

    def _path(self, task_id):
        if not isinstance(task_id, str) or not _SAFE.fullmatch(task_id):
            raise ValueError("invalid artifact version task id")
        return _safe_path(self.directory / (task_id + ".json"), self.cfg.workspace)

    def _proof(self, task_id):
        from . import jobs
        if task_id in self._proofs:
            return self._proofs[task_id]
        self._proofs[task_id] = None
        try:
            path = self._path(task_id)
            if path.stat().st_size > 4 * 1024 * 1024:
                return None
            row = json.loads(path.read_text(encoding="utf-8"))
            source, location = jobs._message(self.bus, task_id)
            if source is None or location != "done" or source.status != "done" or source.type != "task":
                return None
            receipts, error = jobs._receipts(self.bus, source)
            if error:
                return None
            receipt = receipts[0]
            if (row.get("schema_version") != 1 or row.get("task_id") != task_id
                    or row.get("workspace") != str(self.cfg.workspace.resolve())
                    or row.get("output_root") != str(self.cfg.output_root.resolve())
                    or row.get("result_id") != receipt.id
                    or row.get("source_sha256") != _digest(_projection(source, _SOURCE_KEYS))
                    or row.get("receipt_sha256") != receipt_digest(receipt)
                    or row.get("artifacts") != receipt.artifacts
                    or row.get("predecessors") != _predecessors(source)):
                return None
            if source.job_id:
                job = jobs._load(self.cfg, source.job_id)
                item = next((r for r in job["tasks"] if r["id"] == task_id), None)
                if not item or not jobs._task_intent_matches(source, item["message"], self.bus.events()):
                    return None
            proof = (row, source, receipt)
            self._proofs[task_id] = proof
            return proof
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _ids(self):
        if self._all_ids is None:
            _safe_path(self.directory, self.cfg.workspace)
            self._all_ids = [path.stem for path in self.directory.glob("*.json")
                             if _SAFE.fullmatch(path.stem)]
        return self._all_ids

    @staticmethod
    def _entry(proof, key):
        row, source, receipt = proof
        declared = {os.path.normcase(WorkspaceFiles._relative(p)) for p in source.expected_artifacts}
        if key not in declared:
            return None
        entries = [entry for entry in receipt.artifacts
                   if os.path.normcase(WorkspaceFiles._relative(entry["path"])) == key]
        return entries[0] if len(entries) == 1 else None

    def _replacement(self, original, entry):
        key = os.path.normcase(WorkspaceFiles._relative(entry["path"]))
        start = original[0]["task_id"]
        if self._entry(original, key) is None:
            return None
        proofs = {identity: proof for identity in self._ids()
                  if (proof := self._proof(identity)) is not None and self._entry(proof, key) is not None}
        if start not in proofs:
            return None
        edges = {identity: [] for identity in proofs}
        for identity, (row, source, receipt) in proofs.items():
            for predecessor in row["predecessors"]:
                prior = proofs.get(predecessor["task_id"])
                if (prior and predecessor["result_id"] == prior[2].id
                        and predecessor["receipt_sha256"] == receipt_digest(prior[2])):
                    edges[predecessor["task_id"]].append(identity)
        reached, active = set(), set()

        def visit(identity):
            if identity in active:
                raise ValueError("cyclic artifact version chain")
            if identity in reached:
                return
            active.add(identity)
            for successor in edges[identity]:
                visit(successor)
            active.remove(identity)
            reached.add(identity)

        visit(start)
        heads = [identity for identity in reached if not edges[identity]]
        # Two independent successors of the same version are a conflict.
        if heads == [start]:
            return None
        if len(heads) != 1:
            raise ValueError("conflicting independent artifact successors")
        head = proofs[heads[0]]
        latest = self._entry(head, key)
        if self.files.verify_artifacts([latest])["verdict"] != "match":
            raise ValueError(f"latest recorded artifact version {head[1].id} differs from current bytes")
        return {"path": entry["path"], "task_id": head[1].id, "result_id": head[2].id,
                "historical_sha256": entry["sha256"], "current_sha256": latest["sha256"]}

    def verify(self, result):
        manifest = result.artifacts
        task_id = result.reply_to
        proof = None
        try:
            path = self._path(task_id)
            if path.exists():
                proof = self._proof(task_id)
                if proof is None or proof[2].id != result.id or receipt_digest(proof[2]) != receipt_digest(result):
                    return {"verdict": "mismatch", "state": "mismatch", "mismatches": [
                        {"code": "VERSION_EVIDENCE_INVALID", "path": "", "task_id": task_id}]}
            verdict = self.files.verify_artifacts(manifest)
            if any(row.get("code") != "CONTENT_MISMATCH" for row in verdict["mismatches"]):
                return {**verdict, "state": "mismatch"}
            replacements = []
            if proof:
                for entry in manifest:
                    replacement = self._replacement(proof, entry)
                    if replacement:
                        replacements.append(replacement)
            replaced = {os.path.normcase(WorkspaceFiles._relative(row["path"])) for row in replacements}
            if any(os.path.normcase(WorkspaceFiles._relative(row["path"])) not in replaced
                   for row in verdict["mismatches"]):
                return {**verdict, "state": "mismatch"}
            return {"verdict": "match", "state": "superseded" if replacements else "verified",
                    "mismatches": [], "replacements": replacements}
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return {"verdict": "mismatch", "state": "mismatch", "mismatches": [
                {"code": "VERSION_EVIDENCE_INVALID", "path": "", "reason": str(exc)}]}
