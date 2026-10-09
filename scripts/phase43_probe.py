#!/usr/bin/env python
"""Phase 4.3 探针：用真实 MCP 跑一次指定预算的推荐，导出 JSON 供前后对比。

用法::

    python scripts/phase43_probe.py --budget 50 --out before.json

仅只读调用；**不会**写任何凭据。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from mcpilot.mcp_client import McpClient  # noqa: E402
from mcpilot.models import UserRequest  # noqa: E402
from mcpilot.pricing import cent_to_yuan  # noqa: E402
from mcpilot.recommender import recommend  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--store", default="3330324")
    p.add_argument("--be-type", type=int, default=1)
    p.add_argument("--budget", type=float, default=50)
    p.add_argument("--people", type=int, default=1)
    p.add_argument("--out", default="")
    args = p.parse_args()

    plan = recommend(
        UserRequest(
            store_code=args.store,
            be_type=args.be_type,
            budget=args.budget,
            people=args.people,
            max_verify=12,
            max_resolve=10,
        ),
        client=McpClient(),
    )
    payload = plan.to_dict(cent_to_yuan=cent_to_yuan)

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[saved] {args.out}")

    print(f"门店 {plan.store_code}｜查询时间 {plan.queried_at}")
    print("stats:", json.dumps(plan.stats, ensure_ascii=False))
    for r in plan.recommendations:
        n = r.nutrition
        print(f"\n【{r.label}】score={r.score:.4f} 实付 ¥{cent_to_yuan(r.payable_cent):.2f}")
        print("  组合：" + " + ".join(f"{i.name}×{i.quantity}" for i in r.items))
        if n:
            print(f"  营养：{n.energy_kcal} kcal / 蛋白 {n.protein_g} g / 脂肪 {n.fat_g} g")
        for x in r.reasons:
            print("  理由：" + x)
    for w in plan.warnings:
        print("⚠", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
