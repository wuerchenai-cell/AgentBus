"""Public capability cards, Codex 2026-10-05. No host auth or session import."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime
from pathlib import Path

from .lease import LeaseManager

MAX_BYTES = 65536
FIELDS = {"schema_version", "member_id", "display_name", "host_version", "model_id",
          "route", "tools", "specialties", "limitations", "samples", "source", "updated_at"}
SAMPLE_FIELDS = {"project_id", "task_id", "result_id", "capability_family", "outcome",
                 "verified", "artifact_sha256", "transport", "observed_at", "version",
                 "model", "route"}
SAMPLE_REQUIRED = {"project_id", "task_id", "capability_family", "outcome", "verified", "observed_at"}
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1,10)),
            *(f"lpt{i}" for i in range(1,10))}
PRIVATE_DIRECTORIES = {".workbuddy", ".codex", ".codebuddy", ".zcode", ".opencode", ".trae"}


def _identity(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}",value) or value in RESERVED:
        raise ValueError("INVALID_CARD: invalid member ID")
    return value


def _safe(path):
    path=Path(os.path.abspath(path))
    if any(part.lower() in PRIVATE_DIRECTORIES for part in path.parts):
        raise ValueError("UNSAFE_PATH: private host directory")
    for node in [path,*path.parents]:
        try: mode=node.lstat()
        except FileNotFoundError: continue
        except (NotADirectoryError,PermissionError) as exc:
            raise ValueError("UNSAFE_PATH: inaccessible path component") from exc
        if stat.S_ISLNK(mode.st_mode) or getattr(mode,"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0x400):
            raise ValueError("UNSAFE_PATH: link or reparse point")
    return path


def _card(card):
    if (not isinstance(card,dict) or set(card)!=FIELDS
            or type(card.get("schema_version")) is not int or card["schema_version"]!=1):
        raise ValueError("INVALID_CARD: public field contract")
    _identity(card.get("member_id"))
    for key in FIELDS-{"schema_version","tools","specialties","limitations","samples"}:
        _text(card[key],key,nullable=key in {"host_version","model_id"})
    _timestamp(card["updated_at"],"updated_at")
    for key in ("tools","specialties","limitations"):
        if not isinstance(card[key],list) or not all(isinstance(v,str) and v.strip() for v in card[key]):
            raise ValueError("INVALID_CARD: list field "+key)
    if not isinstance(card["samples"],list):
        raise ValueError("INVALID_CARD: samples")
    for sample in card["samples"]:
        if (not isinstance(sample,dict) or not SAMPLE_REQUIRED<=set(sample)
                or not set(sample)<=SAMPLE_FIELDS):
            raise ValueError("INVALID_CARD: non-public sample field")
        if type(sample["verified"]) is not bool:
            raise ValueError("INVALID_CARD: verified must be boolean")
        for key,value in sample.items():
            if key=="verified":
                continue
            _text(value,"sample."+key,nullable=key not in SAMPLE_REQUIRED)
        if sample["outcome"] not in {"done","failed"}:
            raise ValueError("INVALID_CARD: outcome must be done or failed")
        _timestamp(sample["observed_at"],"sample.observed_at")
        digest=sample.get("artifact_sha256")
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}",digest):
            raise ValueError("INVALID_CARD: sample artifact SHA256")
    try: raw=(json.dumps(card,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n").encode("utf-8")
    except (TypeError,ValueError,UnicodeError) as exc: raise ValueError("INVALID_CARD: invalid JSON") from exc
    if len(raw)>MAX_BYTES: raise ValueError("INVALID_CARD: card exceeds 64 KiB")
    return raw


def _text(value,field,*,nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value,str) or not value.strip():
        raise ValueError("INVALID_CARD: text field "+field)


def _timestamp(value,field):
    try:
        parsed=datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError,ValueError) as exc:
        raise ValueError("INVALID_CARD: timestamp "+field) from exc


def _unique_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:
            raise ValueError("INVALID_CARD: duplicate JSON key")
        value[key]=item
    return value


def _read(path):
    path=_safe(path)
    if not path.exists(): return None
    if not path.is_file(): raise ValueError("UNSAFE_PATH: card must be a regular file")
    with path.open("rb") as stream: raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES: raise ValueError("INVALID_CARD: oversized stored card")
    return raw


def _write(root,card,expected,owner,lease_root):
    raw=_card(card); root=_safe(root); target=_safe(root/(card["member_id"]+".json"))
    if expected!="MISSING" and (not isinstance(expected,str) or not re.fullmatch(r"[0-9a-f]{64}",expected)):
        raise ValueError("CAS_MISMATCH: expected raw SHA256 required")
    _safe(lease_root); _safe(Path(lease_root)/"leases"/".mutation.lock")
    manager=LeaseManager(lease_root)
    _safe(manager._file(str(target)))
    lease=manager.acquire(str(target),owner,ttl_s=60,note="public member card CAS")
    if lease is None: raise ValueError("LEASE_BUSY: public card owned")
    temp=None
    try:
        previous=_read(target)
        actual="MISSING" if previous is None else hashlib.sha256(previous).hexdigest()
        if actual!=expected: raise ValueError("CAS_MISMATCH: card changed")
        root.mkdir(parents=True,exist_ok=True); _safe(root); _safe(target)
        temp=root/("."+uuid.uuid4().hex+".tmp")
        with temp.open("xb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        _safe(target); os.replace(temp,target)
        return {"member_id":card["member_id"],"path":str(target),"sha256":hashlib.sha256(raw).hexdigest(),"state":"saved_public_history"}
    finally:
        if temp is not None: temp.unlink(missing_ok=True)
        manager.release(lease)


class MemberLibrary:
    def __init__(self,root:Path):
        self.root=_safe(root)
        if self.root.exists() and not self.root.is_dir():
            raise ValueError("UNSAFE_PATH: library root must be a directory")

    def save(self,card:dict,expected_sha256:str)->dict:
        return _write(self.root,card,expected_sha256,"member-library",self.root/".agentbus")

    def get(self,member_id:str)->dict|None:
        raw=_read(self.root/(_identity(member_id)+".json"))
        if raw is None: return None
        try: card=json.loads(raw.decode("utf-8"),object_pairs_hook=_unique_object)
        except (UnicodeError,ValueError) as exc: raise ValueError("INVALID_CARD: corrupt stored card") from exc
        _card(card)
        if card["member_id"]!=member_id: raise ValueError("INVALID_CARD: stored identity mismatch")
        return card


def store_project_card(cfg,caller:str,card:dict,expected_sha256:str)->dict:
    _card(card); workers=cfg.data.get("workers",{})
    if (not isinstance(caller,str) or not isinstance(workers,dict) or any(
            not isinstance(workers.get(member),dict) or workers[member].get("enabled") is not True
            for member in (caller,card["member_id"]))):
        raise ValueError("UNKNOWN_MEMBER: caller and member must be registered and enabled")
    root=_safe(cfg.workspace)/".agentbus"
    return _write(root/"member-cards",card,expected_sha256,caller,root)
