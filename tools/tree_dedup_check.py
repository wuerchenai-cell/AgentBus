#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree_dedup_check.py: 任务树只读查重与相似候选检索工具。

来源：§10.14 AUDIT-360-TREE-DEDUP-MECHANISM（P2）。
依据：避免多成员、多批次审计时由于信息孤岛导致同类问题重复立叶。

纪律：
1. 100% 只读：严格不修改、不覆写任务树文件。
2. 语义与关键词双重匹配：基于字符覆盖、词袋重合与子串检索，输出相似候选。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def tokenize(text: str) -> set[str]:
    """提取中英文短词与关键词集合。"""
    if not text:
        return set()
    # 提取英文单词及连续2-4个中文字符
    tokens = set()
    # 英文数字
    en_words = re.findall(r"[a-zA-Z0-9_-]{2,}", text.lower())
    tokens.update(en_words)
    # 中文字符切分 (2-gram / 3-gram)
    cn_text = re.sub(r"[^\u4e00-\u9fa5]", "", text)
    for n in (2, 3, 4):
        for i in range(len(cn_text) - n + 1):
            tokens.add(cn_text[i : i + n])
    return tokens


def score_leaf(query: str, query_tokens: set[str], leaf: dict) -> float:
    """计算查询与叶子节点的匹配相似度得分 (0.0 ~ 1.0)。"""
    title = leaf.get("title", "")
    goal = leaf.get("goal", "")
    full_text = f"{title} {goal}".lower()
    q_lower = query.lower().strip()

    # 1. 标题完全包含或字面高度重合
    if q_lower in title.lower():
        return 0.95
    if len(q_lower) >= 4 and q_lower in goal.lower():
        return 0.85

    # 2. 词袋 Jaccard 相似度
    leaf_tokens = tokenize(title + " " + goal[:300])
    if not query_tokens or not leaf_tokens:
        return 0.0

    intersection = query_tokens.intersection(leaf_tokens)
    if not intersection:
        return 0.0

    # 标题命中权重更高
    title_tokens = tokenize(title)
    title_hit = len(intersection.intersection(title_tokens))

    score = len(intersection) / float(len(query_tokens))
    if title_hit > 0:
        score = min(1.0, score + 0.3)

    return round(score, 3)


def check_dedup(tree_path: Path, query: str, top: int = 5) -> list[dict]:
    """查重检索核心逻辑。"""
    if not tree_path.exists():
        raise FileNotFoundError(f"找不到任务树文件: {tree_path}")

    data = json.loads(tree_path.read_text(encoding="utf-8"))
    leaves = data.get("leaves", [])
    query_tokens = tokenize(query)

    results = []
    for l in leaves:
        s = score_leaf(query, query_tokens, l)
        if s > 0.15:  # 过滤无关项
            results.append({
                "section_no": l.get("section_no", ""),
                "id": l.get("id", ""),
                "title": l.get("title", ""),
                "score": s,
                "goal_snippet": l.get("goal", "")[:120].replace("\n", " "),
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="任务树只读查重工具 (tree_dedup_check)")
    parser.add_argument("--query", required=True, help="待查重的新任务描述或关键词")
    parser.add_argument("--workspace", "--root", dest="root", default=".", help="工作区路径")
    parser.add_argument("--top", type=int, default=5, help="返回相似候选项上限")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args(argv)

    root = Path(args.root)
    tree_path = root / ".agentbus" / "task-tree.json"

    candidates = check_dedup(tree_path, args.query, top=args.top)

    if args.json:
        json.dump({"query": args.query, "candidates": candidates}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print("=" * 70)
    print(f"任务树查重检索: '{args.query}'")
    print("=" * 70)
    if not candidates:
        print("[OK] 未发现高相似既有任务叶子，可放心立叶。")
    else:
        print(f"[!] 发现 {len(candidates)} 个潜在重叠或语义相近的既有任务：\n")
        for i, c in enumerate(candidates, 1):
            print(f" {i}. [{c['section_no']}] {c['id']} (相似度得分: {c['score']})")
            print(f"    标题: {c['title']}")
            print(f"    摘要: {c['goal_snippet']}...")
            print()
        print("[提示] 若范围存在重叠，请遵循增量原则：仅覆盖新范围，并在 goal 中交叉引用前序任务。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
