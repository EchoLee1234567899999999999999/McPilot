"""价格试算（Phase 1）。

数据来源（只读）：``calculate-price``。

真实 schema：``data`` 中的金额字段均为**整数、单位「分」**：:

    {"productOriginalPrice":2600,"productPrice":2600,
     "originalPrice":2600,"discount":0,"price":2600,
     "productList":[{"productCode":"1100","productName":"巨无霸","quantity":1,
                     "originalSubtotal":2600,"subtotal":2600}],
     "takeWayList":[...]}

约定：
- 展示金额一律由 :func:`cent_to_yuan` 转「元」；
- **不得**把菜单展示价当作最终价，最终价必须来自本工具返回；
- ``discount`` 为真实优惠金额（分）。
"""

from __future__ import annotations

from typing import Any, Iterable

from .models import Combo, ComboItem, Coupon, PricedLine, Quote


class PriceParseError(ValueError):
    """价格结构不符合预期。"""


def cent_to_yuan(cent: int | float | None) -> float:
    """分 → 元，保留 2 位。"""
    if cent is None:
        return 0.0
    return round(float(cent) / 100.0, 2)


def parse_quote(payload: dict[str, Any]) -> Quote:
    """解析 ``calculate-price`` 返回为 :class:`Quote`（金额单位：分）。"""
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise PriceParseError(f"calculate-price 返回失败：{payload.get('message')}")
    data = payload.get("data") or {}

    lines: list[PricedLine] = []
    for row in data.get("productList") or []:
        lines.append(
            PricedLine(
                product_code=str(row.get("productCode", "")),
                product_name=str(row.get("productName", "")),
                quantity=int(row.get("quantity") or 0),
                original_subtotal_cent=int(row.get("originalSubtotal") or 0),
                subtotal_cent=int(row.get("subtotal") or 0),
            )
        )

    return Quote(
        product_original_price_cent=int(data.get("productOriginalPrice") or 0),
        product_price_cent=int(data.get("productPrice") or 0),
        original_price_cent=int(data.get("originalPrice") or 0),
        discount_cent=int(data.get("discount") or 0),
        payable_cent=int(data.get("price") or 0),
        lines=lines,
    )


def _as_combo_items(items: Iterable[ComboItem | dict[str, Any]]) -> list[ComboItem]:
    out: list[ComboItem] = []
    for it in items:
        if isinstance(it, ComboItem):
            out.append(it)
        else:
            out.append(ComboItem(code=str(it["productCode"]), quantity=int(it.get("quantity", 1))))
    return out


def build_price_items(
    combo: Combo | list[ComboItem | dict[str, Any]],
    coupon: Coupon | None = None,
    coupon_on_code: str | None = None,
) -> list[dict[str, Any]]:
    """把组合转成 ``calculate-price`` 的 ``items`` 结构（可选挂券）。

    ``coupon_on_code`` 指定券挂到哪个商品行；为空则挂到第一行。
    """
    items = combo.items if isinstance(combo, Combo) else _as_combo_items(combo)
    payload: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        row: dict[str, Any] = {"productCode": it.code, "quantity": it.quantity}
        if it.modification:
            row["modification"] = it.modification
        if coupon is not None:
            target = coupon_on_code or (items[0].code if items else None)
            if it.code == target:
                if coupon.coupon_id:
                    row["couponId"] = coupon.coupon_id
                if coupon.coupon_code:
                    row["couponCode"] = coupon.coupon_code
        payload.append(row)
    return payload


def quote(
    client: Any,
    *,
    store_code: str,
    be_type: int,
    items: Combo | list[ComboItem | dict[str, Any]],
    be_code: str | None = None,
    coupon: Coupon | None = None,
    coupon_on_code: str | None = None,
    need_tableware: bool | None = None,
) -> Quote:
    """调用 ``calculate-price`` 并解析为 :class:`Quote`。

    ``client`` 为 :class:`mcpilot.mcp_client.McpClient`（或任何提供
    ``calculate_price(...)`` 的对象，便于测试注入）。
    """
    price_items = build_price_items(items, coupon=coupon, coupon_on_code=coupon_on_code)
    payload = client.calculate_price(
        store_code=store_code,
        be_type=be_type,  # type: ignore[arg-type]
        items=price_items,
        be_code=be_code,
        need_tableware=need_tableware,
    )
    q = parse_quote(payload)
    if coupon is not None:
        q.coupons_used = [coupon]
    return q
