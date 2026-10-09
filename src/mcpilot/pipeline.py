"""端到端最小链路（Phase 1 交付物）。

串联「门店 → 菜单 → 营养 → 优惠券 → 价格试算」，全部基于**真实 MCP 数据**：

1. ``query-meals``         门店菜单 → 商品编码 / 名称 / 展示价
2. ``list-nutrition-foods`` 营养表  → 组合营养加权合计（缺失即标注）
3. ``query-store-coupons``  门店券  → 组合适用性匹配（不适用即排除）
4. ``calculate-price``     真实实付金额（含优惠，单位为分）

用法（Python 直连 MCP）::

    python -m mcpilot.pipeline --store 3330324 --be-type 1 --items 1100:1,4810:1

由 Skill 调度时，把智能体工具返回的 JSON（``{"success":...}``）注入
:class:`~mcpilot.mcp_client.McpClient` 的 invoker 即可复用同一套逻辑。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Sequence

from .coupon import CouponMatch, best_coupon, match_coupons, parse_coupons
from .mcp_client import McpClient
from .menu import MenuData, parse_menu
from .models import Combo, ComboItem, Coupon, Quote
from .nutrition import NutritionIndex, NutritionTotal, build_index, total_nutrition
from .pricing import cent_to_yuan, quote


@dataclass
class PipelineResult:
    """最小链路的聚合结果。"""

    store_code: str
    be_type: int
    be_code: str | None
    menu: MenuData
    combo: Combo
    nutrition_index: NutritionIndex
    nutrition_total: NutritionTotal
    coupons: list[Coupon]
    coupon_matches: list[CouponMatch]
    applied_coupon: Coupon | None
    quote: Quote
    warnings: list[str] = field(default_factory=list)
    data_source: str = "McDonald's MCP (live)"

    @property
    def payable_yuan(self) -> float:
        return cent_to_yuan(self.quote.payable_cent)

    @property
    def discount_yuan(self) -> float:
        return cent_to_yuan(self.quote.discount_cent)

    def summary(self) -> dict[str, Any]:
        """可 JSON 序列化的摘要（不含任何凭据）。"""
        return {
            "store_code": self.store_code,
            "be_type": self.be_type,
            "be_code": self.be_code,
            "menu_size": len(self.menu),
            "combo": [
                {"code": i.code, "name": i.name, "quantity": i.quantity} for i in self.combo.items
            ],
            "nutrition": {
                "energy_kcal": self.nutrition_total.total.energy_kcal,
                "protein_g": self.nutrition_total.total.protein_g,
                "fat_g": self.nutrition_total.total.fat_g,
                "carb_g": self.nutrition_total.total.carb_g,
                "missing": self.nutrition_total.missing,
            },
            "coupons_available": len(self.coupons),
            "coupons_applicable": [m.coupon.title for m in self.coupon_matches if m.applicable],
            "coupons_not_applicable": [m.coupon.title for m in self.coupon_matches if not m.applicable],
            "applied_coupon": self.applied_coupon.title if self.applied_coupon else None,
            "price": {
                "original_yuan": cent_to_yuan(self.quote.original_price_cent),
                "discount_yuan": self.discount_yuan,
                "payable_yuan": self.payable_yuan,
                "raw_cent": {
                    "originalPrice": self.quote.original_price_cent,
                    "discount": self.quote.discount_cent,
                    "price": self.quote.payable_cent,
                },
            },
            "warnings": self.warnings,
            "data_source": self.data_source,
        }


def parse_item_specs(spec: str | Sequence[str]) -> list[tuple[str, int]]:
    """解析 ``"1100:1,4810:2"`` 形式的商品规格。"""
    raw = spec if isinstance(spec, str) else ",".join(spec)
    out: list[tuple[str, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            code, _, qty = chunk.partition(":")
            out.append((code.strip(), int(qty or 1)))
        else:
            out.append((chunk, 1))
    if not out:
        raise ValueError("未提供任何商品（--items 不能为空）。")
    return out


def run_minimal(
    client: McpClient,
    *,
    store_code: str,
    item_specs: list[tuple[str, int]],
    be_type: int = 1,
    be_code: str | None = None,
    apply_coupon: bool = True,
) -> PipelineResult:
    """执行最小链路。任何工具失败都会抛出带上下文的异常（不编造结果）。"""
    warnings: list[str] = []

    # 1) 门店菜单
    menu_payload = client.query_meals(store_code=store_code, be_type=be_type, be_code=be_code)  # type: ignore[arg-type]
    menu = parse_menu(menu_payload)
    if len(menu) == 0:
        raise ValueError(f"门店 {store_code} 未返回任何在售餐品。")

    # 2) 组装组合（编码必须真实存在于菜单）
    combo_items: list[ComboItem] = []
    for code, qty in item_specs:
        item = menu.get(code)
        if item is None:
            raise ValueError(f"商品编码 {code} 不在门店 {store_code} 的在售菜单中。")
        combo_items.append(ComboItem(code=item.code, name=item.name, quantity=qty))
    combo = Combo(items=combo_items)

    # 3) 营养匹配
    nutrition_payload = client.list_nutrition_foods()
    nutrition_index = build_index(nutrition_payload)
    nut_total = total_nutrition([(i.name, i.quantity) for i in combo.items], nutrition_index)
    if nut_total.has_missing:
        warnings.append("以下餐品未在营养表中匹配到数据，已标注为「营养未知」：" + "、".join(nut_total.missing))

    # 4) 优惠券
    coupon_payload = client.query_store_coupons(store_code=store_code, be_type=be_type, be_code=be_code)  # type: ignore[arg-type]
    coupons = parse_coupons(coupon_payload)
    matches = match_coupons(combo.codes, coupons)
    applied = best_coupon(matches) if apply_coupon else None
    if not coupons:
        warnings.append("当前门店/订单类型下无可用优惠券。")
    elif applied is None:
        warnings.append("有可用券，但没有一张适用于当前组合。")

    # 5) 价格试算（真实应付）
    coupon = applied.coupon if applied else None
    coupon_on_code = (applied.matched_codes[0] if applied and applied.matched_codes else None)
    q = quote(
        client,
        store_code=store_code,
        be_type=be_type,
        items=combo,
        be_code=be_code,
        coupon=coupon,
        coupon_on_code=coupon_on_code,
    )

    return PipelineResult(
        store_code=store_code,
        be_type=be_type,
        be_code=be_code,
        menu=menu,
        combo=combo,
        nutrition_index=nutrition_index,
        nutrition_total=nut_total,
        coupons=coupons,
        coupon_matches=matches,
        applied_coupon=coupon,
        quote=q,
        warnings=warnings,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="McPilot 最小链路（真实 MCP）")
    p.add_argument("--store", required=True, help="门店编码 storeCode")
    p.add_argument("--be-type", type=int, default=1, help="1=到店自取, 5=得来速")
    p.add_argument("--be-code", default=None, help="得来速场景必填")
    p.add_argument("--items", default="1100:1,4810:1", help='商品规格，如 "1100:1,4810:1"')
    p.add_argument("--no-coupon", action="store_true", help="不使用优惠券试算")
    p.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client = McpClient()
    result = run_minimal(
        client,
        store_code=args.store,
        item_specs=parse_item_specs(args.items),
        be_type=args.be_type,
        be_code=args.be_code,
        apply_coupon=not args.no_coupon,
    )
    summary = result.summary()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"门店 {summary['store_code']}（beType={summary['be_type']}）｜菜单 {summary['menu_size']} 项")
    print("组合：")
    for it in summary["combo"]:
        print(f"  - {it['name']}（{it['code']}）×{it['quantity']}")
    n = summary["nutrition"]
    missing = f"  未知项: {'、'.join(n['missing'])}" if n["missing"] else ""
    print(f"营养合计：{n['energy_kcal']} kcal｜蛋白 {n['protein_g']} g｜脂肪 {n['fat_g']} g｜碳水 {n['carb_g']} g{missing}")
    print(f"可用券 {summary['coupons_available']} 张｜适用: {summary['coupons_applicable'] or '无'}"
          f"｜不适用: {summary['coupons_not_applicable'] or '无'}")
    p = summary["price"]
    print(f"价格：原价 ¥{p['original_yuan']:.2f}｜优惠 -¥{p['discount_yuan']:.2f}｜实付 ¥{p['payable_yuan']:.2f}")
    for w in summary["warnings"]:
        print(f"提示：{w}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
