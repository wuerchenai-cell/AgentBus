"""Mailbox Backlog Audit and Who-Waits-For-Whom Analyzer for AgentBus.

Source: Antigravity, 2026-10-11.
Audits backlog across all agent mailboxes, computes message ages, analyzes
Who-Waits-For-Whom dependency chains, and exposes structured health metrics.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def parse_message_age(ts_str: str | None, now_dt: datetime) -> float | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now_dt - dt).total_seconds() / 86400.0)
    except Exception:
        return None


def audit_mailboxes(root_dir: Path) -> dict:
    root_dir = root_dir.resolve()
    agents_dir = root_dir / ".agentbus/agents"
    if not agents_dir.is_dir():
        agents_dir = root_dir / "agents"

    now_dt = datetime.now(timezone.utc)
    res = {
        "schema_version": "1.0.0",
        "audited_at": now_dt.isoformat(),
        "agents_dir": str(agents_dir).replace('\\', '/'),
        "mailboxes": {},
        "who_waits_for_whom": {},
        "age_distribution": {
            ">7d": 0,
            "3-7d": 0,
            "1-3d": 0,
            "<24h": 0,
            "unknown": 0
        },
        "totals": {
            "ready": 0,
            "processing": 0,
            "done": 0,
            "blocked": 0,
            "agents_count": 0
        },
        "oldest_message": None,
        "newest_message": None
    }

    if not agents_dir.is_dir():
        return res

    wwfw = defaultdict(lambda: defaultdict(int))
    oldest_msg = None
    oldest_age = -1.0
    newest_msg = None
    newest_age = 999999.0

    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent = agent_dir.name
        res["totals"]["agents_count"] += 1

        ready_p = agent_dir / "ready"
        proc_p = agent_dir / "processing"
        done_p = agent_dir / "done"
        blk_p = agent_dir / "blocked"

        ready_files = list(ready_p.glob("*.json")) if ready_p.is_dir() else []
        proc_count = len(list(proc_p.glob("*.json"))) if proc_p.is_dir() else 0
        done_count = len(list(done_p.glob("*.json"))) if done_p.is_dir() else 0
        blk_count = len(list(blk_p.glob("*.json"))) if blk_p.is_dir() else 0

        res["totals"]["ready"] += len(ready_files)
        res["totals"]["processing"] += proc_count
        res["totals"]["done"] += done_count
        res["totals"]["blocked"] += blk_count

        type_counter = Counter()
        msgs_summary = []
        oldest_agent_ts = None
        newest_agent_ts = None

        for f in sorted(ready_files):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                data = {"type": "corrupt", "sender": "unknown"}

            msg_id = data.get("id", f.stem)
            msg_type = data.get("type", "unknown")
            sender = data.get("sender", "unknown")
            ts = data.get("ts")
            title = data.get("title", "")

            type_counter[msg_type] += 1
            wwfw[sender][agent] += 1

            age_days = parse_message_age(ts, now_dt)
            if age_days is not None:
                if age_days >= 7.0:
                    res["age_distribution"][">7d"] += 1
                elif age_days >= 3.0:
                    res["age_distribution"]["3-7d"] += 1
                elif age_days >= 1.0:
                    res["age_distribution"]["1-3d"] += 1
                else:
                    res["age_distribution"]["<24h"] += 1

                if age_days > oldest_age:
                    oldest_age = age_days
                    oldest_msg = {
                        "id": msg_id,
                        "recipient": agent,
                        "sender": sender,
                        "type": msg_type,
                        "ts": ts,
                        "title": title,
                        "age_days": round(age_days, 1)
                    }
                if age_days < newest_age:
                    newest_age = age_days
                    newest_msg = {
                        "id": msg_id,
                        "recipient": agent,
                        "sender": sender,
                        "type": msg_type,
                        "ts": ts,
                        "title": title,
                        "age_days": round(age_days, 1)
                    }
            else:
                res["age_distribution"]["unknown"] += 1

            if ts:
                if oldest_agent_ts is None or ts < oldest_agent_ts:
                    oldest_agent_ts = ts
                if newest_agent_ts is None or ts > newest_agent_ts:
                    newest_agent_ts = ts

            msgs_summary.append({
                "id": msg_id,
                "type": msg_type,
                "sender": sender,
                "ts": ts,
                "title": title[:60] if title else "",
                "age_days": round(age_days, 1) if age_days is not None else None
            })

        res["mailboxes"][agent] = {
            "ready_count": len(ready_files),
            "processing_count": proc_count,
            "done_count": done_count,
            "blocked_count": blk_count,
            "by_type": dict(type_counter),
            "oldest_ts": oldest_agent_ts,
            "newest_ts": newest_agent_ts,
            "messages": msgs_summary
        }

    # Format who_waits_for_whom
    formatted_wwfw = {}
    for s, rec_map in sorted(wwfw.items()):
        formatted_wwfw[s] = dict(rec_map)
    res["who_waits_for_whom"] = formatted_wwfw
    res["oldest_message"] = oldest_msg
    res["newest_message"] = newest_msg
    return res


def print_summary(data: dict):
    totals = data.get("totals", {})
    age_dist = data.get("age_distribution", {})
    print("=" * 70)
    print(" AgentBus 信箱积压体检审计与「谁等谁」依赖分析")
    print("=" * 70)
    print(f"审计时间: {data.get('audited_at')}")
    print(f"信箱目录: {data.get('agents_dir')}")
    print(f"成员总数: {totals.get('agents_count')}")
    print(f"消息总数: ready={totals.get('ready')}, processing={totals.get('processing')}, done={totals.get('done')}")
    print("-" * 70)
    print("【积压年龄分布】:")
    print(f"  > 7 天 (严重滞留) : {age_dist.get('>7d', 0)} 条")
    print(f"  3 - 7 天 (中度滞留): {age_dist.get('3-7d', 0)} 条")
    print(f"  1 - 3 天 (轻度滞留): {age_dist.get('1-3d', 0)} 条")
    print(f"  < 24 小时 (新鲜在途): {age_dist.get('<24h', 0)} 条")
    oldest = data.get("oldest_message")
    if oldest:
        print(f"  最老消息: {oldest['id']} ({oldest['sender']} -> {oldest['recipient']}, 类型: {oldest['type']}, 滞留: {oldest['age_days']} 天, 时间: {oldest['ts']})")
    print("-" * 70)
    print("【成员信箱状态】:")
    print(f"{'成员代号':15} | {'ready':6} | {'proc':6} | {'done':6} | {'类型细分'}")
    print("-" * 70)
    for agent, m in sorted(data.get("mailboxes", {}).items()):
        types_str = ", ".join(f"{k}:{v}" for k, v in m.get("by_type", {}).items()) or "(空)"
        print(f"{agent:15} | {m.get('ready_count', 0):6} | {m.get('processing_count', 0):6} | {m.get('done_count', 0):6} | {types_str}")
    print("-" * 70)
    print("【谁等谁（因果等待链）】:")
    for sender, r_map in sorted(data.get("who_waits_for_whom", {}).items()):
        targets = ", ".join(f"{rec}({cnt}条)" for rec, cnt in sorted(r_map.items()))
        print(f"  [{sender:12}] 正在等待处理 -> {targets}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    repo_root = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])
    parser.add_argument('--root', type=Path, default=repo_root)
    parser.add_argument('--output', type=Path, default=None)
    parser.add_argument('--json', action='store_true', help="Output raw JSON to stdout")
    args = parser.parse_args()

    data = audit_mailboxes(args.root)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
        print(f"[SUCCESS] Audit report written to {args.output}")

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_summary(data)


if __name__ == '__main__':
    main()
