#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.handshake —— 互通协议协商。

设计立场（很重要，别改）：
    协议**不该由某一个 agent 单方面设计**。谁最适合说"我怎么接收消息最方便"？
    是每个 agent 自己。所以这里只做三件事：
      1. emit()     生成一份中立、平级的《协商指令》，用户粘贴给每个 agent；
      2. collect()  把各 agent 的回复收集回来；
      3. summarize() 汇总成对照表，标出共识、分歧和需要用户拍板的点。

这份指令刻意保持中立：
    · 不预设谁是主体谁是执行者（它们是平级的同事）；
    · 不告诉对方"你应该怎么做"，只问"你现在能怎么做"；
    · 明确要求它只写一个回复文件，别动其他文件（避免协商本身造成冲突）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REQUEST_FILE = "REQUEST.md"
REPLY_SUFFIX = ".reply.md"

# 协商要问的问题。顺序就是设计协议的决策顺序。
QUESTIONS: list[tuple[str, str, str]] = [
    ("1", "我是谁、我的形态",
     "谁在运行你？是桌面应用里的一段对话，还是能在终端/脚本里被直接拉起的命令行？"
     "有没有命令行入口？请给出你的真实形态，不要猜别人。"),
    ("2", "我怎么接收外部消息最方便（请排序）",
     "可选：A. 我读某个文件；B. 有人把内容粘贴给我；C. 我自己调工具去拉（如 MCP 工具）；"
     "D. 有人通过命令行把内容喂给我；E. 其他。请排序并说明为什么，以及具体路径/命令名。"),
    ("3", "我怎么知道有新消息（最关键的一题）",
     "你是能自己持续发现新消息，还是必须有人跟你说话你才能动作？"
     "如果你没有后台循环，请直接说清楚——这一条决定整个协议能不能自动跑。"),
    ("4", "我怎么把结果给出去最方便",
     "可选：写某个文件 / 调工具回投 / 打印出来让人复制。请给具体路径或工具名。"),
    ("5", "等待时的开销",
     "你能常驻等待吗？还是每次被叫醒时才看一眼？如果不确定，请说\"不确定\"。"),
    ("6", "消息格式我建议这样",
     "给你想收的消息写一个最小字段清单（例如：谁发的、给谁、干什么、相关文件、怎么回执）。"
     "越短越好。"),
    ("7", "改文件前怎么避免撞车",
     "如果两个 agent 同时要改同一个文件，你希望怎么协调？"
     "（例如：先查一下有没有人在动、抢一个租约文件、或者约定分工不重叠）"),
    ("8", "我不做什么（红线）",
     "哪些事你明确不会做 / 不应该让你做（例如删除文件、发布、花钱的操作）。"),
    ("9", "一句话：你建议的最简可行协议",
     "用一句话说清\"消息怎么放、你怎么拿到、结果怎么放\"。"),
]

# 极简版：只问决定架构的 3+1 题，适合快速试水
SHORT_NUMBERS = ("1", "2", "3", "4")

ISSUE_NOTE = """\
> 说明（请先读这一段，再回答）
>
> 现在这台机器上同时有多个 AI 协作工具（例如 Codex、WorkBuddy、Trae、ZCode）。
> 我们的目标是：**让它们互相之间能传递工作，而不需要人在中间转述**。
>
> 你们是**平级的同事**，没有谁指挥谁。所以我不打算替你们定一套协议——
> 而是先问你们每个人：**按你现在的能力，怎么沟通对你最顺手？**
>
> 要求：
> 1. 只如实回答你自己的能力，**不要猜别人、不要替别人回答**。
> 2. 不确定的地方请直接写「不确定」，不要编。
> 3. **只写下面指定的那一个回复文件，不要修改工作空间里的其他任何文件。**
> 4. 回答要短。每一项两三句话，能给出路径/命令名就给出来。
"""


@dataclass
class Reply:
    agent: str
    path: Path
    sections: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str:
        for num, title, _ in QUESTIONS:
            if num == key:
                return self.sections.get(title, "")
        return ""


# --------------------------------------------------------------------------- #
def build_request(workspace: Path | str, agents: list[str] | None = None,
                  self_name: str | None = None, short: bool = False) -> str:
    ws = Path(workspace).resolve()
    hs = ws / ".agentbus" / "handshake"
    agents = agents or []
    who = f"（我现在找的是：{'、'.join(agents)}）" if agents else ""
    qs = [q for q in QUESTIONS if q[0] in SHORT_NUMBERS] if short else QUESTIONS
    lines = [
        "# 互通协议协商 —— 请描述你自己，别猜别人",
        "",
        ISSUE_NOTE.rstrip(),
        "",
        f"工作空间：`{ws}` {who}",
        "",
        "---",
        "",
        f"## 请按这 {len(qs)} 个问题回答",
        "",
    ]
    for num, title, hint in qs:
        lines += [f"### {num}. {title}", "", hint, "", "（在这里写你的回答）", ""]

    lines += [
        "---",
        "",
        "## 回答怎么交回来",
        "",
        "把上面的回答写进**这一个文件**（文件名用你自己的名字，例如 `codex`、`workbuddy`、`trae`、`zcode`）：",
        "",
        "```",
        str(hs / "<你的名字>.reply.md"),
        "```",
        "",
        "文件里请用与上面**完全相同的小节标题**（`## 1. 我是谁、我的形态` 这种一行一个），",
        "方便自动汇总。",
        "",
        "如果你**没有文件写入权限**，就把回答直接打印出来，让用户复制走即可 —— 不要为了写文件去改别的目录。",
        "",
        "## 回复模板（直接填）",
        "",
        "```markdown",
    ]
    for num, title, _ in qs:
        lines += [f"## {num}. {title}", "", "<回答>", ""]
    lines += ["```", ""]
    return "\n".join(lines)


def emit(workspace: Path | str, agents: list[str] | None = None,
         short: bool = False) -> Path:
    ws = Path(workspace).resolve()
    hs = ws / ".agentbus" / "handshake"
    hs.mkdir(parents=True, exist_ok=True)
    target = hs / REQUEST_FILE
    target.write_text(build_request(ws, agents, short=short), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
def _parse_sections(text: str) -> dict[str, str]:
    """按 '## 数字. 标题' 切分。标题只比数字，避免标点差异导致解析失败。"""
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []

    def flush():
        if current is not None:
            body = "\n".join(buf).strip()
            if body:
                out[current] = body

    for line in text.splitlines():
        m = re.match(r"^\s*#{2,3}\s*(\d+)\s*[.、)]\s*(.+?)\s*$", line)
        if m:
            flush()
            num, title = m.group(1), m.group(2).strip()
            # 用"标准标题"归一化，容忍标点/空格差异
            std = next((t for n, t, _ in QUESTIONS if n == num), title)
            current = std
            buf = []
            continue
        if current is not None:
            buf.append(line)
    flush()
    return out


def collect(workspace: Path | str) -> list[Reply]:
    ws = Path(workspace).resolve()
    hs = ws / ".agentbus" / "handshake"
    replies: list[Reply] = []
    if not hs.is_dir():
        return replies
    for p in sorted(hs.glob(f"*{REPLY_SUFFIX}")):
        name = p.name[: -len(REPLY_SUFFIX)]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        replies.append(Reply(agent=name, path=p, sections=_parse_sections(text)))
    return replies


# --------------------------------------------------------------------------- #
def _verdict(row: dict[str, str]) -> tuple[bool, str]:
    """粗判：能自动跑 / 必须有人推一下。只做提示，不代替人判断。"""
    joined = " ".join(row.values())
    if re.search(r"不确定", joined):
        return (False, "答复含「不确定」，需追问")
    if re.search(r"没有?后台|必须有人|需要.*唤醒|我不能自己发现|被叫醒|每次.*激活|粘贴", joined):
        return (False, "看起来需要有人推一下（无后台自发现）")
    if re.search(r"监听|轮询|持续|常驻|自己发现|自动发现|文件变化", joined):
        return (True, "可能可以自己发现新消息")
    return (False, "信息不足，需追问")


def summarize(replies: list[Reply]) -> str:
    if not replies:
        return ("还没有收到任何回复。\n"
                "先执行：python -m agentbus handshake --emit 生成指令并粘贴给每个 agent，\n"
                "等它们各自写好 <名字>.reply.md 后，再执行 --collect。")

    names = [r.agent for r in replies]
    out: list[str] = []
    out.append("# 互通协议协商汇总")
    out.append("")
    out.append(f"已收到 {len(replies)} 份回复：{'、'.join(names)}")
    out.append("")

    # 1) 逐题对照
    for num, title, _ in QUESTIONS:
        out.append(f"## {num}. {title}")
        out.append("")
        out.append("| agent | 回答 |")
        out.append("|-------|------|")
        for r in replies:
            body = r.get(num).replace("\n", " ").replace("|", "/").strip()
            if len(body) > 260:
                body = body[:260] + "…"
            out.append(f"| {r.agent} | {body or '（未回答）'} |")
        out.append("")

    # 2) 关键题判定
    out.append("## 能不能自动跑起来（第 3 题判定）")
    out.append("")
    out.append("| agent | 判定 | 依据 |")
    out.append("|-------|------|------|")
    for r in replies:
        ok, why = _verdict({"q3": r.get("3")})
        out.append(f"| {r.agent} | {'可能可以' if ok else '需要有人推'} | {why} |")
    out.append("")

    # 3) 收集各自的"最简协议"提议
    out.append("## 各自建议的最简可行协议（第 9 题）")
    out.append("")
    for r in replies:
        out.append(f"- **{r.agent}**：{r.get('9') or '（未回答）'}")
    out.append("")

    # 4) 待用户拍板
    missing = [(r.agent, n) for r in replies
               for n, _, _ in QUESTIONS if not r.get(n)]
    if missing:
        out.append("## 未回答 / 需要追问")
        out.append("")
        for agent, n in missing:
            out.append(f"- {agent} 的第 {n} 题")
        out.append("")

    out.append("## 下一步")
    out.append("")
    out.append("1. 把上面的分歧点挑出来（尤其是「能不能自己发现新消息」），逐条向对应 agent 追问。")
    out.append("2. 共识部分直接写进工作空间根目录的 `AGENTS.md`，作为所有人共享的约定。")
    out.append("3. 只有**所有**参与方都确认过的条目，才写进协议正文；没确认的标成「待定」。")
    return "\n".join(out)


def write_summary(workspace: Path | str, text: str) -> Path:
    ws = Path(workspace).resolve()
    hs = ws / ".agentbus" / "handshake"
    hs.mkdir(parents=True, exist_ok=True)
    target = hs / "SUMMARY.md"
    target.write_text(text, encoding="utf-8")
    return target


def status(workspace: Path | str) -> dict:
    ws = Path(workspace).resolve()
    hs = ws / ".agentbus" / "handshake"
    got = [p.name[: -len(REPLY_SUFFIX)] for p in hs.glob(f"*{REPLY_SUFFIX}")] \
        if hs.is_dir() else []
    return {"request_emitted": (hs / REQUEST_FILE).exists(), "replies": sorted(got)}


def to_json(replies: list[Reply]) -> str:
    return json.dumps([{"agent": r.agent, "sections": r.sections} for r in replies],
                      ensure_ascii=False, indent=2)
