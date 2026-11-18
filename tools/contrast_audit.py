#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contrast_audit.py: WCAG AA 对比度与前端字号阶梯只读自检工具。

来源：§10.9 AUDIT-360-A11Y-CONTRAST-FONT（P1）。
检查项：
1. 解析 app.css 中的 :root 色彩令牌，精确计算 WCAG 2.1 相对亮度与对比度（正文标准 >= 4.5:1）；
2. 检查 font-size 字号阶梯，断言 <= 8px 出现次数为 0，统计 <= 9px 出现次数；
3. 统计 !important 与十六进制硬编码颜色绕过情况；
4. 提供 --strict 门禁模式，不达标时非零退出。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def hex_to_rgb(hex_code: str) -> tuple[int, int, int]:
    h = hex_code.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    elif len(h) >= 6:
        h = h[:6]
    else:
        raise ValueError(f"Invalid hex color: {hex_code}")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(val: int) -> float:
        cs = val / 255.0
        return cs / 12.92 if cs <= 0.04045 else ((cs + 0.055) / 1.055) ** 2.4

    r, g, b = [channel(v) for v in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex1: str, hex2: str) -> float:
    l1 = relative_luminance(hex_to_rgb(hex1))
    l2 = relative_luminance(hex_to_rgb(hex2))
    bright = max(l1, l2)
    dark = min(l1, l2)
    return (bright + 0.05) / (dark + 0.05)


def audit_css(css_path: Path) -> dict:
    text = css_path.read_text(encoding="utf-8")

    # 1. 提取 :root 变量
    root_match = re.search(r":root\s*\{([^}]+)\}", text)
    tokens: dict[str, str] = {}
    if root_match:
        for line in root_match.group(1).split(";"):
            line = line.strip()
            if line.startswith("--") and ":" in line:
                var_name, var_val = line.split(":", 1)
                tokens[var_name.strip()] = var_val.strip()

    # 标准对比度检查对
    # (fg_token, bg_token, min_ratio, description)
    test_pairs = [
        ("muted", "canvas", 4.5, "次要文字在画布背景 (muted/canvas)"),
        ("muted", "surface", 4.5, "次要文字在卡片背景 (muted/surface)"),
        ("white", "emerald", 4.5, "主按钮白字在主色背景 (white/emerald)"),
        ("emerald", "surface", 4.5, "主强调色在卡片背景 (emerald/surface)"),
        ("danger", "surface", 4.5, "危险/错误色在卡片背景 (danger/surface)"),
        ("amber", "surface", 4.5, "警示/状态色在卡片背景 (amber/surface)"),
        ("text", "canvas", 4.5, "正文在画布背景 (text/canvas)"),
        ("text", "surface", 4.5, "正文在卡片背景 (text/surface)"),
        ("sidebar-text", "sidebar", 4.5, "侧边栏文字在侧栏底色 (sidebar-text/sidebar)"),
        ("sidebar-muted", "sidebar", 4.5, "侧边栏次要文字在侧栏底色 (sidebar-muted/sidebar)"),
    ]

    contrast_results = []
    contrast_pass = True

    for fg_name, bg_name, required_min, desc in test_pairs:
        fg_val = "#ffffff" if fg_name == "white" else tokens.get(f"--{fg_name}")
        bg_val = "#ffffff" if bg_name == "white" else tokens.get(f"--{bg_name}")

        if not fg_val or not bg_val:
            contrast_results.append({
                "pair": f"{fg_name}/{bg_name}",
                "description": desc,
                "fg": fg_val,
                "bg": bg_val,
                "ratio": 0.0,
                "required": required_min,
                "passed": False,
                "error": "Token not found",
            })
            contrast_pass = False
            continue

        try:
            ratio = contrast_ratio(fg_val, bg_val)
            ok = ratio >= required_min
            if not ok:
                contrast_pass = False
            contrast_results.append({
                "pair": f"{fg_name}/{bg_name}",
                "description": desc,
                "fg": fg_val,
                "bg": bg_val,
                "ratio": round(ratio, 2),
                "required": required_min,
                "passed": ok,
            })
        except Exception as e:
            contrast_results.append({
                "pair": f"{fg_name}/{bg_name}",
                "description": desc,
                "fg": fg_val,
                "bg": bg_val,
                "ratio": 0.0,
                "required": required_min,
                "passed": False,
                "error": str(e),
            })
            contrast_pass = False

    # 2. 字号分布检查
    font_sizes = [int(m) for m in re.findall(r"font-size\s*:\s*(\d+)px", text)]
    fs_distribution: dict[int, int] = {}
    for sz in font_sizes:
        fs_distribution[sz] = fs_distribution.get(sz, 0) + 1

    le_8px_count = sum(cnt for sz, cnt in fs_distribution.items() if sz <= 8)
    le_9px_count = sum(cnt for sz, cnt in fs_distribution.items() if sz <= 9)
    font_pass = le_8px_count == 0

    # 3. !important 统计
    important_count = len(re.findall(r"!important", text))

    # 4. 硬编码颜色（排除 :root 内部）
    body_after_root = text[root_match.end() :] if root_match else text
    hex_outside_root = re.findall(r"#[0-9a-fA-F]{3,8}\b", body_after_root)
    # 允许的纯白/透明白阴影之外
    pure_white_aliases = {"#fff", "#ffffff", "#ffffff08", "#ffffff14", "#ffffff0b", "#ffffff03"}
    hardcoded_brand_colors = [h for h in hex_outside_root if h.lower() not in pure_white_aliases]

    overall_pass = contrast_pass and font_pass

    return {
        "schema_version": 1,
        "css_file": str(css_path),
        "contrast_pass": contrast_pass,
        "font_pass": font_pass,
        "overall_pass": overall_pass,
        "le_8px_count": le_8px_count,
        "le_9px_count": le_9px_count,
        "font_distribution": dict(sorted(fs_distribution.items())),
        "contrast_results": contrast_results,
        "important_count": important_count,
        "hardcoded_brand_colors_count": len(hardcoded_brand_colors),
        "tokens": {k: v for k, v in tokens.items() if k.startswith("--") and not k.startswith("--shadow") and not k.startswith("--elevation")},
    }


def main():
    parser = argparse.ArgumentParser(description="AgentBus 前端 WCAG AA 对比度与字号阶梯自检工具")
    parser.add_argument(
        "--css",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "agentbus" / "web_assets" / "app.css",
        help="app.css 路径",
    )
    parser.add_argument("--strict", action="store_true", help="严格门禁模式：对比度或字号不达标时非零退出")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出审计结果")

    args = parser.parse_args()

    if not args.css.exists():
        print(f"[错误] CSS 文件不存在: {args.css}", file=sys.stderr)
        sys.exit(2)

    res = audit_css(args.css)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("AgentBus 前端 A11Y 对比度与字号阶梯体检报告")
        print("=" * 60)
        print(f"目标文件: {res['css_file']}")
        print(f"字号 <= 8px 出现次数: {res['le_8px_count']} (要求: 0)")
        print(f"字号 <= 9px 出现次数: {res['le_9px_count']}")
        print(f"!important 规则总数: {res['important_count']}")
        print(f"品牌硬编码颜色数: {res['hardcoded_brand_colors_count']}")
        print("-" * 60)
        print("WCAG AA (>= 4.5:1) 关键色对实测结果:")
        for r in res["contrast_results"]:
            status_mark = "[PASS]" if r.get("passed") else "[FAIL]"
            print(f"  {r['pair']:<26} {r.get('ratio', 0):>5.2f}:1  {status_mark}  ({r['description']})")
        print("=" * 60)
        if res["overall_pass"]:
            print("[OK] 全项达标！符合 WCAG AA 规范与字号阶梯要求。")
        else:
            print("[WARN] 存在不达标项，请根据上述指标调整样式！")
        print("=" * 60)

    if args.strict and not res["overall_pass"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
