# -*- coding: utf-8 -*-
"""capability_card：公开能力档案校验与解释性推荐纯函数。

来源：ZCode（平级成员），2026-10-05，任务 m-1791203447665-e38761（job j-6912b1425b1305708d134dd4b25aa6c2）。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。
边界：纯函数零 I/O，不启动模型；required tools/permission 硬边界；样例只辅助推荐；
普通授权任务可界派发并在 reasons 注明未验证（不全局 E0 拒绝）；品牌不进评分
（改 member_id 不改变推荐）；版本/模型漂移进 pending_retest 且历史保留；
不复制技能包七维/30 天规则；拒绝 command/credentials/auth/permission_inherited 字段。
Codex 集成硬化，2026-10-05：严格公共字段与类型；证据按当前项目/版本/模型/路线及实际时间限定。
原作者文件保留不改。max_evidence_age_s 仅任务显式要求时生效，不自造全局失效时限。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

FORBIDDEN_FIELDS = ("command", "credentials", "auth", "permission_inherited")
_SAMPLE_REQUIRED = ("project_id", "task_id", "capability_family", "outcome", "verified", "observed_at")
_OUTCOMES = ("done", "failed")
_CARD_FIELDS = {"schema_version", "member_id", "display_name", "host_version", "model_id",
                "route", "tools", "specialties", "limitations", "samples", "source", "updated_at"}
_SAMPLE_FIELDS = set(_SAMPLE_REQUIRED) | {"result_id", "artifact_sha256", "transport", "version", "model", "route"}
_RESERVED = {"con", "prn", "aux", "nul", *("com"+str(i) for i in range(1,10)),
             *("lpt"+str(i) for i in range(1,10))}
_UNKNOWN = {"unknown", "unverified", "n/a", "none", "未知", "未验证"}


def _err(msg):
    raise ValueError(msg)


def _str_list(value, field):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        _err(field + " 必须是字符串列表")
    return list(value)


def _text(value, field, *, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value,str) or not value.strip():
        _err(field + " 必须是非空字符串")
    return value


def _time(value, field):
    _text(value,field)
    try:
        parsed=datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed
    except ValueError as exc:
        raise ValueError(field + " 必须是带时区的 ISO 日期") from exc


def _known(value):
    return isinstance(value,str) and bool(value.strip()) and value.strip().casefold() not in _UNKNOWN


def validate_card(card):
    """校验并规范化公开能力档案；字段格式错误抛 ValueError。"""
    if not isinstance(card, dict):
        _err("card 必须是 dict")
    if set(card)!=_CARD_FIELDS:
        _err("档案必须严格使用公共字段")
    member_id = card.get("member_id")
    if (not isinstance(member_id,str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}",member_id)
            or member_id in _RESERVED):
        _err("member_id 必须是合法公共成员 ID")
    if type(card.get("schema_version")) is not int or card["schema_version"]!=1:
        _err("schema_version 必须是整数 1")
    for field in ("display_name","source","route","updated_at"):
        _text(card[field],field)
    for field in ("host_version","model_id"):
        _text(card[field],field,nullable=True)
    _time(card["updated_at"],"updated_at")
    samples_in = card.get("samples", [])
    if not isinstance(samples_in, list):
        _err("samples 必须是列表")
    samples = []
    for s in samples_in:
        if not isinstance(s, dict):
            _err("sample 必须是 dict")
        if not set(s)<=_SAMPLE_FIELDS:
            _err("sample 只能使用公共字段")
        for key in _SAMPLE_REQUIRED:
            if s.get(key) is None:
                _err("sample 缺少必填字段: " + key)
        for key in _SAMPLE_REQUIRED:
            if key!="verified":
                _text(s[key],"sample."+key)
        _time(s["observed_at"],"sample.observed_at")
        if s["outcome"] not in _OUTCOMES:
            _err("sample.outcome 必须是 done/failed")
        if type(s["verified"]) is not bool:
            _err("sample.verified 必须是布尔")
        opt = {k: s.get(k) for k in ("result_id", "artifact_sha256", "transport", "version", "model", "route")}
        for key,value in opt.items():
            _text(value,"sample."+key,nullable=True)
        digest=opt["artifact_sha256"]
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}",digest):
            _err("sample.artifact_sha256 必须是 SHA256 或明确空值")
        samples.append({
            "project_id": s["project_id"], "task_id": s["task_id"],
            "capability_family": s["capability_family"], "outcome": s["outcome"],
            "verified": s["verified"], "observed_at": s["observed_at"], **opt,
        })
    return {
        "schema_version": 1,
        "member_id": member_id,
        "display_name": card.get("display_name") or "",
        "host_version": card.get("host_version"),
        "model_id": card.get("model_id"),
        "route": card.get("route"),
        "tools": _str_list(card.get("tools", []), "tools"),
        "specialties": _str_list(card.get("specialties", []), "specialties"),
        "limitations": _str_list(card.get("limitations", []), "limitations"),
        "source": card.get("source"),
        "updated_at": card.get("updated_at"),
        "samples": samples,
    }


def assess_member(card, requirements, context, now):
    """按硬边界与样例证据给出解释性推荐；输出纯 JSON，输入零改动。"""
    normalized = validate_card(card)
    if not isinstance(requirements, dict):
        _err("requirements 必须是 dict")
    if not isinstance(context, dict):
        _err("context 必须是 dict")
    current_time=_time(now,"now")
    family = requirements.get("capability_family")
    if not isinstance(family, str) or not family.strip():
        _err("requirements.capability_family 必须是非空字符串")
    if "tools" not in requirements:
        _err("requirements.tools 必须显式给出（可为空列表）")
    req_tools = _str_list(requirements["tools"], "requirements.tools")
    permission = context.get("permission_allowed")
    if type(permission) is not bool:
        _err("context.permission_allowed 必须是布尔")
    available = _str_list(context.get("available_tools"),"context.available_tools")
    _text(context.get("project_id"),"context.project_id")
    for field in ("host_version","model_id","route"):
        _text(context.get(field),"context."+field,nullable=True)
    require_current=requirements.get("require_current_sample",False)
    if type(require_current) is not bool:
        _err("requirements.require_current_sample 必须是布尔")
    max_age=requirements.get("max_evidence_age_s")
    if max_age is not None and (type(max_age) not in (int,float) or not math.isfinite(max_age) or max_age<=0):
        _err("requirements.max_evidence_age_s 必须是有限正数")

    reasons = []
    eligible = True
    bounded = False
    write_family = "write" in family.casefold()
    if not permission:
        eligible = False
        reasons.append("PERMISSION_NOT_ALLOWED")
    missing = [t for t in req_tools if t not in available]
    for t in missing:
        eligible = False
        reasons.append("TOOL_MISSING:" + t)

    family_samples = [s for s in normalized["samples"] if s["capability_family"] == family]
    family_samples.sort(key=lambda s: (_time(s["observed_at"],"sample.observed_at"),s["task_id"],s.get("result_id") or ""))
    past_samples=[s for s in family_samples if _time(s["observed_at"],"sample.observed_at")<=current_time]
    latest = past_samples[-1] if past_samples else None
    samples_seen = [{"project_id": s["project_id"], "task_id": s["task_id"],
                     "outcome": s["outcome"], "verified": s["verified"]} for s in family_samples]

    pending = []
    valid_current=[]
    for s in family_samples:
        age=(current_time-_time(s["observed_at"],"sample.observed_at")).total_seconds()
        if age<0:
            reasons.append("FUTURE_OBSERVATION_IGNORED:"+s["task_id"])
            continue
        reasons.append("EVIDENCE_AGE_SECONDS:"+s["task_id"]+":"+str(round(age,3)))
        if s["outcome"] != "done" or not s["verified"]:
            continue
        fresh=max_age is None or age<=max_age
        if not fresh:
            reasons.append("EXPIRED_EVIDENCE_PENDING_RETEST:"+s["task_id"])
        dimensions=(("version","host_version"),("model","model_id"),("route","route"))
        known=all(_known(s.get(sample_field)) and _known(context.get(context_field))
                  and _known(normalized.get(context_field)) for sample_field,context_field in dimensions)
        matches=known and all(s[sample_field]==context[context_field]==normalized[context_field]
                              for sample_field,context_field in dimensions)
        if not known:
            reasons.append("UNKNOWN_VERSION_MODEL_OR_ROUTE_PENDING_RETEST:"+s["task_id"])
        elif not matches:
            reasons.append("VERSION_MODEL_OR_ROUTE_DRIFT_PENDING_RETEST:"+s["task_id"])
        same_project=s["project_id"]==context["project_id"]
        if not same_project:
            reasons.append("CROSS_PROJECT_HISTORY_REFERENCE_ONLY:"+s["task_id"])
        referenced=_known(s.get("result_id")) and _known(s.get("transport"))
        if not referenced:
            reasons.append("SAMPLE_RESULT_REFERENCE_UNVERIFIED:"+s["task_id"])
        if write_family and s.get("artifact_sha256") is None:
            referenced=False
            reasons.append("WRITE_ARTIFACT_DIGEST_UNVERIFIED:"+s["task_id"])
        if not fresh or not matches:
            if family not in pending:
                pending.append(family)
        if fresh and matches and same_project and referenced:
            valid_current.append(s)

    if require_current:
        if not valid_current:
            eligible = False
            reasons.append("REQUIRE_CURRENT_SAMPLE_UNMET")

    has_current_verified_done = latest is not None and latest in valid_current

    if write_family and not has_current_verified_done:
        bounded = True
        reasons.append("BOUNDED_SCOPE_REQUIRED_UNVERIFIED_WRITE")

    if not eligible:
        rec = "not_recommended"
    elif not family_samples:
        reasons.append("NO_SAMPLE_SELF_REPORT_ONLY_BOUNDED_DISPATCH_OK")
        rec = "probe_first"
    elif latest is not None and latest["outcome"] == "failed":
        reasons.append("RECENT_FAILURE_HISTORY_KEPT")
        rec = "probe_first"
    elif has_current_verified_done:
        reasons.append("SAMPLE_VERIFIED")
        rec = "recommended"
        pending=[]
    elif latest is not None and latest["outcome"]=="done" and latest["verified"]:
        rec = "recommend_with_caution"
    else:
        reasons.append("NO_VALID_CURRENT_SAMPLE_BOUNDED_DISPATCH_OK")
        rec = "probe_first"

    return {
        "schema_version": 1,
        "member_id": normalized["member_id"],
        "eligible": eligible,
        "recommendation": rec,
        "reasons": reasons,
        "pending_retest": pending,
        "bounded_scope_required": bounded,
        "samples_seen": samples_seen,
    }
