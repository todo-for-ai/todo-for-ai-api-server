#!/usr/bin/env python3
"""架构治理审计（软件工程管理蓝图的自动化门禁，见主仓 docs/ENGINEERING_AT_SCALE.md）。

检查项与失败级别：
  FAIL（exit 1，阻塞合并）：
    1. 单文件 > --max-hard（默认 800 行）
    2. 分层边界违规：models/core 不得 import api/services；services 不得 import api
    3. 顶层包循环依赖（api ↔ services ↔ core 之间的 import 环）
  WARN（exit 0，报告中列出，进入待办）：
    1. 单文件 500~800 行（500 行硬上限的新增约束只拦新代码）
    2. 单包（一层目录）平铺文件 > 30 个 → 提示按域拆子包
    3. 单包总 LOC > 20000 → 提示评估拆分

棘轮机制：FAIL 级违规若已登记在基线文件（默认 arch_debt_baseline.txt，
--update-baseline 重写）则降级为 BASELINE（存量债务，只许减不许增）；
未登记的新违规才 FAIL。这样存量债务不阻塞日常合并，但任何新增立即被拦。

用法：python scripts/arch_audit.py [--root .] [--max-warn 500] [--max-hard 800]
       [--baseline arch_debt_baseline.txt] [--update-baseline]
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import sys
from collections import defaultdict
from pathlib import Path

SKIP_PATTERNS = ("tests/*", "test_*", "migrations/*", "scripts/*", "benchmark/*", "venv/*", ".venv/*")
LAYER_RULES = {  # 下层禁止 import 的上层
    "models": ("api", "services"),
    "core": ("api", "services"),
    "services": ("api",),
}


def iter_py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("*")) for pat in SKIP_PATTERNS):
            continue
        yield p, rel


def top_module_of(rel: str) -> str:
    return rel.split("/")[0]


def pkg_of(rel: str, depth: int = 1) -> str:
    parts = rel.split("/")
    return "/".join(parts[:depth])


def module_deps(root: Path):
    """rel_path → 依赖的顶层包集合（仅跨顶层包的边）。"""
    edges: dict[str, set[str]] = defaultdict(set)
    for p, rel in iter_py_files(root):
        src = top_module_of(rel)
        try:
            tree = ast.parse(p.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for m in mods:
                dst = m.split(".")[0]
                if dst != src and dst in LAYER_RULES or dst in ("api", "services", "models", "core"):
                    edges[src].add(dst)
    return edges


def find_cycles(edges) -> list[list[str]]:
    cycles, seen = [], set()
    def dfs(node, path):
        for nxt in sorted(edges.get(node, ())):
            if nxt in path:
                cycles.append(path[path.index(nxt):] + [nxt])
            elif nxt not in seen and nxt in edges:
                seen.add(nxt)
                dfs(nxt, path + [nxt])
    for start in sorted(edges):
        if start not in seen:
            seen.add(start)
            dfs(start, [start])
    return cycles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--max-warn", type=int, default=500)
    ap.add_argument("--max-hard", type=int, default=800)
    ap.add_argument("--baseline", default="arch_debt_baseline.txt")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前全部 FAIL 写入基线文件（棘轮重置，慎用）")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    baseline_path = root / args.baseline

    fails, warns = [], []

    # 1. 文件行数
    pkg_loc: dict[str, int] = defaultdict(int)
    pkg_files: dict[str, int] = defaultdict(int)
    for p, rel in iter_py_files(root):
        lines = len(p.read_text(errors="ignore").splitlines())
        pkg = pkg_of(rel)
        pkg_loc[pkg] += lines
        pkg_files[pkg] += 1
        if lines > args.max_hard:
            fails.append(f"文件超硬上限 {lines} 行（>{args.max_hard}）: {rel}")
        elif lines > args.max_warn:
            warns.append(f"文件超软上限 {lines} 行（>{args.max_warn}）: {rel}")

    # 2. 分层边界
    for p, rel in iter_py_files(root):
        layer = top_module_of(rel)
        banned = LAYER_RULES.get(layer)
        if not banned:
            continue
        try:
            tree = ast.parse(p.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            bad = None
            if isinstance(node, ast.Import):
                bad = next((a.name for a in node.names if a.name.split(".")[0] in banned), None)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] in banned:
                    bad = node.module
            if bad:
                fails.append(f"分层违规: {layer} 层不得 import {bad.split('.')[0]} 层 — {rel}:{node.lineno} → {bad}")

    # 3. 顶层包循环依赖
    for cycle in find_cycles(module_deps(root)):
        fails.append("顶层包循环依赖: " + " → ".join(cycle))

    # 4. 巨型平铺包预警
    for pkg in sorted(pkg_files):
        depth = pkg.count("/") + 1
        if depth > 2:  # 只看一/二层包
            continue
        if pkg_files[pkg] > 30:
            warns.append(f"包 {pkg}/ 平铺 {pkg_files[pkg]} 个文件——按域拆子包（参照 api/agents/workflow 模式）")
        if pkg_loc[pkg] > 20000:
            warns.append(f"包 {pkg}/ 达 {pkg_loc[pkg]} 行——评估拆分或拆仓")

    print(f"扫描 {sum(pkg_files.values())} 个文件 / {sum(pkg_loc.values())} 行（不含测试/迁移/脚本）")
    for w in warns:
        print(f"WARN  {w}")

    baseline = set()
    if baseline_path.exists():
        baseline = {ln.strip() for ln in baseline_path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    if args.update_baseline:
        baseline_path.write_text("\n".join(sorted(fails)) + ("\n" if fails else ""))
        print(f"\n基线已重置：{len(fails)} 项 FAIL 写入 {args.baseline}")
        return 0
    tracked, blocking = [], []
    for f in fails:
        (tracked if f in baseline else blocking).append(f)
    for f in tracked:
        print(f"BASELINE（存量债务，待清偿）  {f}")
    for f in blocking:
        print(f"FAIL  {f}")
    if baseline - set(fails):
        print("NOTE  基线中以下债务已消失，可从基线删除（棘轮只紧不松）：")
        for gone in sorted(baseline - set(fails)):
            print(f"  GONE  {gone}")
    if blocking:
        print(f"\n结果：{len(blocking)} 项新增 FAIL，{len(tracked)} 项存量债务，{len(warns)} 项 WARN")
        return 1
    print(f"\n结果：PASS（存量债务 {len(tracked)} 项，WARN {len(warns)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
