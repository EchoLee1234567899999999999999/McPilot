#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Phase 5 安全审查：扫描「将被公开」的文件，查找真实凭据与敏感信息。

用法：
    python scripts/security_scan.py            # 扫描
    python scripts/security_scan.py --strict   # 有命中则以非 0 退出（CI 用）

设计原则：
- 只扫描「会进入公开仓库」的文件（遵循 .gitignore 的排除思想，但独立实现，
  不依赖 git，因为发布前可能尚未初始化仓库）。
- 区分「真实凭据泄露」（P0）与「占位符/示例」（OK），避免误报。
- 不打印命中的完整密钥，只打印前缀，防止扫描输出本身泄露。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# 顶层目录中「不参与公开」的路径（与 .gitignore 保持一致）
EXCLUDE_DIRS = {
    ".git", ".pytest_cache", "__pycache__", "node_modules", ".venv", "venv",
    "env", ".mypy_cache", ".ruff_cache", "htmlcov",
    ".tmp_phase43", ".tmp_phase5",
    # WorkBuddy 运行时数据（.gitignore 已忽略，不进公开仓库）
    os.path.join(".workbuddy", "memory"),
    os.path.join(".workbuddy", "cache"),
    os.path.join(".workbuddy", "logs"),
}
EXCLUDE_DIR_PREFIXES = (".tmp_",)
EXCLUDE_FILES = {".env"}

# 只检查文本类文件
TEXT_EXTS = {
    ".md", ".py", ".json", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".js", ".sh", ".ps1", ".example", ".gitignore", "",
}

# ---- 允许的占位符（出现这些不算泄露）----
PLACEHOLDER_TOKENS = (
    "YOUR_MCP_TOKEN", "${MCD_MCP_TOKEN}", "$MCD_MCP_TOKEN", "<your-token>",
    "<MCP Token>", "SECRETVALUE", "REDACTED", "xxx", "***", "your_token",
    "placeholder", "example", "TOKEN_HERE", "PLEASE_REPLACE",
)

# ---- P0：真实凭据特征 ----
P0_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("HTTP Bearer 令牌", re.compile(r"Bearer\s+([A-Za-z0-9._\-]{16,})")),
    ("JWT 令牌", re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\.[A-Za-z0-9._\-]{10,}")),
    ("疑似访问密钥字段", re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|private[_-]?key)"
        r"\s*[\"']?\s*[:=]\s*[\"']([A-Za-z0-9._\-]{20,})[\"']"
    )),
    # 中国手机号（11 位，1[3-9] 开头）——注意 11 位门店编号会误报，需结合上下文
    ("疑似手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

# ---- P1：需要注意但可能是合法数据 ----
P1_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("疑似身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("疑似邮箱", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("内网/本机绝对路径含用户名", re.compile(r"C:\\Users\\(?!<)[A-Za-z0-9_\-]+")),
    ("POSIX 家目录真实用户名", re.compile(r"/(?:home|Users)/(?!<|user|username)[A-Za-z0-9_\-]{3,}")),
]

# 「门店编号」上下文关键词：11 位数字若紧邻这些词，判为门店码而非手机号
STORE_CONTEXT = re.compile(r"(store|storeCode|beCode|门店|店铺|shopCode)", re.I)


def is_excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if any(p.startswith(EXCLUDE_DIR_PREFIXES) for p in parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix.lower() not in TEXT_EXTS:
        return True
    return False


def is_placeholder(value: str) -> bool:
    up = value.upper()
    return any(t.upper() in up for t in PLACEHOLDER_TOKENS)


def scan_file(path: Path) -> tuple[list[str], list[str]]:
    p0: list[str] = []
    p1: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return p0, p1
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for label, pat in P0_PATTERNS:
            for m in pat.finditer(line):
                raw = m.group(m.lastindex) if m.lastindex else m.group(0)
                if is_placeholder(raw):
                    continue
                if label == "疑似手机号" and STORE_CONTEXT.search(line):
                    # 11 位数字 + 门店上下文 → 判为门店编号，降级为 P1 提示
                    p1.append(f"{label}(疑似门店码，非手机号) @ L{lineno}: {raw[:6]}***")
                    continue
                p0.append(f"{label} @ L{lineno}: {raw[:6]}***")
        for label, pat in P1_PATTERNS:
            for m in pat.finditer(line):
                raw = m.group(0)
                if is_placeholder(raw):
                    continue
                p1.append(f"{label} @ L{lineno}: {raw[:40]}")
    return p0, p1


def is_excluded_dir(rel_dir: str) -> bool:
    """rel_dir：相对 root 的目录路径。"""
    norm = rel_dir.replace("\\", "/")
    if norm in {d.replace("\\", "/") for d in EXCLUDE_DIRS}:
        return True
    parts = norm.split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    if any(p.startswith(EXCLUDE_DIR_PREFIXES) for p in parts):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描待公开文件的敏感信息")
    ap.add_argument("--root", default=".", help="扫描根目录（默认当前目录）")
    ap.add_argument("--strict", action="store_true", help="有 P0 命中则返回非 0")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    all_p0: list[str] = []
    all_p1: list[str] = []
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(root):
        rel_base = os.path.relpath(dirpath, root)
        rel_base = "" if rel_base == "." else rel_base
        # 剪枝：跳过被排除的目录
        dirnames[:] = [d for d in dirnames if not is_excluded_dir(os.path.join(rel_base, d))]
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root)
            if is_excluded(p, root):
                continue
            file_count += 1
            p0, p1 = scan_file(p)
            all_p0.extend(f"{rel} :: {x}" for x in p0)
            all_p1.extend(f"{rel} :: {x}" for x in p1)

    print(f"扫描文件数：{file_count}")
    print(f"排除目录：{sorted(EXCLUDE_DIRS)}")
    print()
    print(f"【P0 真实凭据/敏感信息】命中 {len(all_p0)} 处")
    for x in all_p0:
        print("  ✗", x)
    if not all_p0:
        print("  ✅ 未发现真实凭据或敏感信息")
    print()
    print(f"【P1 需人工确认】命中 {len(all_p1)} 处")
    for x in all_p1[:60]:
        print("  ?", x)
    if len(all_p1) > 60:
        print(f"  ...（其余 {len(all_p1) - 60} 处省略）")
    print()
    if all_p0:
        print("结论：❌ 存在 P0 命中，禁止公开，需先清除。")
        return 1 if args.strict else 0
    print("结论：✅ 未发现真实凭据；P1 项需人工确认是否为合规的公开数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
