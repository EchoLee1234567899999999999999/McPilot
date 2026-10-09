#!/usr/bin/env python
"""Phase 3 验收脚本：营养覆盖率变化 + 三种预算的推荐耗时/调用数 + 券验证。

**真实调用** 麦当劳 MCP（只读）。用法::

    PYTHONPATH=src python scripts/phase3_report.py --store 3330324 --be-type 1

输出为可读报告；也可加 ``--json`` 输出机读结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence

from mcpilot.coupon import (
    COUPON_SOURCES,
    active_coupons,
    best_coupon,
    coupons_in_menu,
    match_coupons,
    parse_coupons,
)
from mcpilot.mcp_client import McpClient
from mcpilot.menu import parse_menu
from mcpilot.models import UserRequest
from mcpilot.nutrition import build_index, coverage_report
from mcpilot.pricing import cent_to_yuan
from mcpilot.recommender import recommend
from mcpilot.resolve import NutritionEnricher


def coverage_section(client: McpClient, store: str, be_type: int) -> dict[str, Any]:
    menu = parse_menu(client.query_meals(store_code=store, be_type=be_type))
    index = build_index(client.list_nutrition_foods())
    names = [it.name for it in menu.items]

    before = sum(1 for n in names if index.contains(n))  # 等价 Phase 2：纯精确匹配
    static = coverage_report(names, index)  # 归一化 + 别名（零额外调用）

    # 对静态未匹配者做**有界**真实解析，量化"按需解析"的实际增益
    enricher = NutritionEnricher(
        index, menu,
        lambda code: client.query_meal_detail(store_code=store, code=code, be_type=be_type),
        max_calls=12,
    )
    unresolved = [n for n in names if not enricher.resolve(n, allow_fetch=False).ok][:12]
    added: list[dict[str, str]] = []
    for n in unresolved:
        r = enricher.resolve(n)
        if r.ok:
            added.append({"menu_name": n, "matched": r.matched_name, "tier": r.tier, "evidence": r.evidence})

    after = coverage_report(
        names, index, canonical_names=enricher.canonical_names, compositions=enricher.compositions
    )
    return {
        "menu_size": len(menu),
        "before_exact_matched": before,
        "after_static_matched": static.matched,
        "after_with_resolve_matched": after.matched,
        "static_breakdown": {
            "exact": static.exact, "normalized": static.normalized, "alias": static.alias,
        },
        "static_evidence": static.examples_added,
        "resolve_calls": enricher.calls,
        "resolve_evidence": added,
    }


def coupon_section(client: McpClient, store: str, be_type: int, menu) -> dict[str, Any]:
    coupons = parse_coupons(client.query_store_coupons(store_code=store, be_type=be_type))
    codes = {it.code for it in menu.items}
    in_menu = coupons_in_menu(coupons, codes)
    rows = []
    for c in coupons:
        m = match_coupons(c.product_codes, [c])[0]
        rows.append({
            "title": c.title,
            "source": c.source,
            "status": c.status(),
            "valid_window": [c.valid_from, c.valid_to],
            "product_codes_in_store_menu": sorted(set(c.product_codes) & codes),
            "usable_for_own_target": best_coupon([m]) is not None,
        })
    return {
        "store_coupon_count": len(coupons),
        "coupons_usable_in_store_menu": [c.title for c in in_menu],
        "active_count": len(active_coupons(coupons)),
        "rows": rows,
        "sources": COUPON_SOURCES,
    }


def budget_section(client: McpClient, store: str, be_type: int, budgets: Sequence[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in budgets:
        plan = recommend(UserRequest(store_code=store, be_type=be_type, budget=b), client=client)
        st = plan.stats
        recs = []
        for r in plan.recommendations:
            recs.append({
                "strategy": r.strategy,
                "label": r.label,
                "items": [{"name": i.name, "qty": i.quantity, "role": i.role} for i in r.items],
                "payable_yuan": cent_to_yuan(r.payable_cent),
                "discount_yuan": cent_to_yuan(r.discount_cent),
                "nutrition_complete": r.nutrition_complete,
                "kcal": (r.nutrition.energy_kcal if r.nutrition else None),
                "protein_g": (r.nutrition.protein_g if r.nutrition else None),
                "reasons": r.reasons,
            })
        out.append({
            "budget": b,
            "elapsed_ms": st["elapsed_ms"],
            "candidates": st["candidates"],
            "candidates_within_budget": st["candidates_within_budget"],
            "verified": st["verified"],
            "detail_resolved": st["detail_resolved"],
            "mcp_calls": st["mcp_calls"],
            "recommendations": recs,
            "warnings": plan.warnings,
        })
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="McPilot Phase 3 验收报告（真实 MCP）")
    ap.add_argument("--store", default="3330324")
    ap.add_argument("--be-type", type=int, default=1)
    ap.add_argument("--budgets", default="20,30,50")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    client = McpClient()
    menu = parse_menu(client.query_meals(store_code=args.store, be_type=args.be_type))

    report = {
        "store": args.store,
        "be_type": args.be_type,
        "coverage": coverage_section(client, args.store, args.be_type),
        "coupons": coupon_section(client, args.store, args.be_type, menu),
        "budgets": budget_section(client, args.store, args.be_type, [int(x) for x in args.budgets.split(",")]),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    cov = report["coverage"]
    print("=" * 78)
    print(f"McPilot Phase 3 验收 · 门店 {args.store}（beType={args.be_type}）")
    print("=" * 78)
    print("\n【一、营养匹配覆盖率】")
    print(f"  菜单商品数: {cov['menu_size']}")
    print(f"  修改前（纯精确匹配）        : {cov['before_exact_matched']}/{cov['menu_size']}"
          f" = {cov['before_exact_matched']/cov['menu_size']:.1%}")
    print(f"  静态分层（归一化+别名，0调用）: {cov['after_static_matched']}/{cov['menu_size']}"
          f" = {cov['after_static_matched']/cov['menu_size']:.1%}  {cov['static_breakdown']}")
    print(f"  叠加按需解析（detail）       : {cov['after_with_resolve_matched']}/{cov['menu_size']}"
          f" = {cov['after_with_resolve_matched']/cov['menu_size']:.1%}  （detail 调用 {cov['resolve_calls']} 次）")
    print("  静态新增依据:")
    for e in cov["static_evidence"]:
        print(f"    - {e['menu_name']} → {e['matched']} [{e['tier']}] {e['evidence'][:56]}")
    print("  按需解析新增依据:")
    for e in cov["resolve_evidence"]:
        print(f"    - {e['menu_name']} → {e['matched']} [{e['tier']}] {e['evidence'][:56]}")

    cp = report["coupons"]
    print("\n【二、优惠券验证】")
    print(f"  门店券数量: {cp['store_coupon_count']}（有效 {cp['active_count']}）")
    print(f"  可在本店菜单使用的券: {cp['coupons_usable_in_store_menu'] or '无'}")
    for row in cp["rows"]:
        print(f"    - {row['title']}｜{row['status']}｜适用商品在本店菜单: {row['product_codes_in_store_menu'] or '无'}")
    print(f"  券来源: 门店券=已调用；账户券={COUPON_SOURCES['account']}")

    print("\n【三、三种预算的推荐耗时与结果】")
    for b in report["budgets"]:
        print(f"\n  ── 预算 ¥{b['budget']} ── 耗时 {b['elapsed_ms']} ms｜候选 {b['candidates']}"
              f"（预算内 {b['candidates_within_budget']}）｜试算 {b['verified']}｜详情 {b['detail_resolved']}")
        print(f"     MCP 调用: {b['mcp_calls']}")
        for r in b["recommendations"]:
            nut = (f"{r['kcal']:.0f}kcal/蛋白{r['protein_g']:.0f}g"
                   if r["nutrition_complete"] else "营养不完整")
            combo = " + ".join(f"{i['name']}×{i['qty']}" for i in r["items"])
            print(f"     [{r['label']}] ¥{r['payable_yuan']:.2f} 优惠¥{r['discount_yuan']:.2f} {nut}")
            print(f"        {combo}")
            for reason in r["reasons"][:2]:
                print(f"        理由：{reason}")
        if not b["recommendations"]:
            print("     （无可行方案）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
