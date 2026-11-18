# -*- coding: utf-8 -*-
"""Task 3：跨宿主证据失效判定纯函数（修正版）。

来源：ZCode（平级成员），2026-10-05；原版任务 m-1791169024291-b8e3a2（candidate 原件保留），
本修正版任务 m-1791170402279-228fbd（job j-cc56fdf40e0c01743b077f4793e34447），目录 integration。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：IMPLEMENTATION_PLAN.md Task 3、DESIGN_RESOLUTIONS.md 第 1/4/5 节、修正任务正文三项缺陷。

相对原版的三项修正（先红后绿，红日志 test-red-fix-20261005.log）：
1. max_age_s 硬上限：大于 MAX_AGE_LIMIT_S（30 天）抛 ValueError，调用方不可放宽，
   只能更短；60 天证据 + 90 天参数反例钉死。
2. 身份字段与 source 强校验：七身份字段两侧都必须是非空、非纯空白字符串且精确相等；
   content_digest 两侧都必须是 64 位小写 hex；source 必须存在且非空非纯空白。
   任一不满足即降级（不异常），两侧同为空串不再判 verified。
3. 极大 int 不再 OverflowError：int（非 bool）恒按有限处理，float 才走 math.isfinite；
   坏数据一律降级为 unverified。

边界（写死，不可由调用方放宽）：
- 本模块不访问磁盘、不执行脚本、不安装、不读私有配置或凭据；纯函数只解释传入的 dict。
- 安装/发现/调用三态互不代证：evidence.kind 必须与 context.kind 精确相等，安装证据
  不能让发现/调用变绿；kind 词汇表由调用方定义，本函数只做精确匹配。
- 时间判定：observed_at_ts 必须为有限数值；未来时间、超过有效 max_age_s 降级；
  expires_at_ts 可缺省，存在时必须是有限数值且大于 now_ts，否则降级。
  恰好等于 max_age_s 的年龄视为仍有效（边界钉死在测试里），超过一秒即降级。
- 失效不删除历史：original_evidence 原样返回同一对象，输入零改动；
  成功与失败样本同等保留，供查错与统计重算。
- 单条证据 verified 只证明该次观察与当前上下文一致，不等同于所有聊天或会话都
  加载了同一资源；宿主缓存、全局发现目录与执行条件固定不在本函数能力范围内；
  当前独立 route 尚无固定载体，independent/cli 路线的证据不表示执行版本已固定。
- now_ts / max_age_s 非有限数值、max_age_s<=0 或超过 30 天硬上限属调用方编程错误，
  抛 ValueError；证据/上下文的数据质量问题一律降级为 unverified，不抛异常。
"""
from __future__ import annotations

import math
import re

# Codex internal independent audit integration correction, 2026-10-05:
# reject a caller's unrepresentable now_ts as ValueError, never OverflowError.

SCHEMA_VERSION = 1
STATE_VERIFIED = "verified"
STATE_UNVERIFIED = "unverified"
DEFAULT_MAX_AGE_S = 2592000  # 30 天默认
MAX_AGE_LIMIT_S = 2592000  # 30 天硬上限：传更大值拒绝，不可放宽

SCOPE_NOTE = (
    "单条证据 verified 只证明该次观察在 kind/host/host_version/machine_scope/"
    "project_scope/route/content_digest 七维与当前上下文精确一致且时间有效，"
    "不等同于所有聊天或会话都加载了同一资源；"
    "安装/发现/调用三态互不代证，历史样本不因降级被删除；"
    "当前独立 route 尚无固定载体，independent/cli 路线的证据不表示执行版本已固定。"
)

_IDENTITY_FIELDS = (
    "kind",
    "host",
    "host_version",
    "machine_scope",
    "project_scope",
    "route",
    "content_digest",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _is_finite_number(value) -> bool:
    # bool 先排除；任意精度 int 本身就是有限整数，直接放行（math.isfinite 会 OverflowError）；
    # 仅 float 需要 isfinite 检查 NaN/inf。
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _valid_identity(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def evaluate_evidence(
    evidence: dict | None,
    context: dict,
    *,
    now_ts: float,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> dict:
    """判定一条证据对当前上下文是否仍可视为 verified。

    返回 {schema_version, state, reason_codes, original_evidence, scope_note}；
    state=verified 当且仅当 reason_codes 为空。
    """
    if not _is_finite_number(now_ts):
        raise ValueError("now_ts 必须是有限数值")
    try:
        if not math.isfinite(now_ts):
            raise ValueError("now_ts 必须是有限数值")
    except OverflowError as exc:
        raise ValueError("now_ts 超出可支持的时间数值范围") from exc
    if not _is_finite_number(max_age_s) or max_age_s <= 0:
        raise ValueError("max_age_s 必须是正的有限数值")
    if max_age_s > MAX_AGE_LIMIT_S:
        raise ValueError("max_age_s 不可超过 30 天硬上限（2592000 秒）")

    reasons: list[str] = []
    original = evidence  # 原样保留（含失败样本），本函数绝不修改输入对象

    if evidence is None:
        reasons.append("EVIDENCE_MISSING")
    elif not isinstance(evidence, dict):
        reasons.append("EVIDENCE_MALFORMED")
    else:
        if evidence.get("outcome") != "passed":
            reasons.append("OUTCOME_NOT_PASSED")

        source = evidence.get("source")
        if not _valid_identity(source):
            reasons.append("SOURCE_MISSING")

        observed = evidence.get("observed_at_ts")
        if not _is_finite_number(observed):
            reasons.append("OBSERVED_AT_INVALID")
        elif observed > now_ts:
            reasons.append("OBSERVED_AT_FUTURE")
        elif observed < (now_ts - max_age_s):
            # 直接比较而非计算差值：极大 int 参与减法会在 float 转换时 OverflowError
            reasons.append("EVIDENCE_TOO_OLD")

        if evidence.get("expires_at_ts") is not None:
            expires = evidence.get("expires_at_ts")
            if not _is_finite_number(expires):
                reasons.append("EXPIRES_AT_INVALID")
            elif expires <= now_ts:
                reasons.append("EVIDENCE_EXPIRED")

        ctx = context if isinstance(context, dict) else {}
        for field in _IDENTITY_FIELDS:
            ev_value = evidence.get(field)
            ctx_value = ctx.get(field)
            ok = (
                _valid_identity(ev_value)
                and _valid_identity(ctx_value)
                and ev_value == ctx_value
            )
            if ok and field == "content_digest":
                # 两侧都必须是 64 位小写 hex，即使字符串相等
                ok = bool(_HEX64.fullmatch(ev_value)) and bool(_HEX64.fullmatch(ctx_value))
            if not ok:
                reasons.append(field.upper() + "_MISMATCH")

    return {
        "schema_version": SCHEMA_VERSION,
        "state": STATE_VERIFIED if not reasons else STATE_UNVERIFIED,
        "reason_codes": reasons,
        "original_evidence": original,
        "scope_note": SCOPE_NOTE,
    }
