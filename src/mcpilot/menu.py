"""菜单与餐品详情解析（Phase 1）。

数据来源（只读）：
- ``query-meals``         → :func:`parse_menu`
- ``query-meal-detail``   → :func:`parse_meal_detail`

真实 schema 要点：
- ``data.categories[].meals[].code`` 提供分类与商品编码；
- ``data.meals[code]`` 提供 ``name`` / ``currentPrice`` / ``originalPrice`` 等，
  价格字段是**字符串且单位为「元」**；
- 详情 ``data.modification.items[].values[]`` 提供特调项。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import MealDetail, MenuItem, ModificationValue, SetComponent


class MenuParseError(ValueError):
    """菜单结构不符合预期。"""


@dataclass
class MenuData:
    """解析后的菜单。"""

    items: list[MenuItem] = field(default_factory=list)
    by_code: dict[str, MenuItem] = field(default_factory=dict)
    categories: dict[str, list[str]] = field(default_factory=dict)
    by_name: dict[str, MenuItem] = field(default_factory=dict)

    def get(self, code: str) -> MenuItem | None:
        return self.by_code.get(str(code))

    def __len__(self) -> int:  # 便于断言
        return len(self.items)


def _to_float(value: Any) -> float | None:
    """把价格字符串安全转为 float（元）；空/非法返回 None（不猜测）。"""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_menu(payload: dict[str, Any]) -> MenuData:
    """把 ``query-meals`` 返回解析为 :class:`MenuData`。

    仅使用真实返回字段；缺失字段留空/None，**不填充臆造值**。
    """
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise MenuParseError(f"query-meals 返回失败：{payload.get('message') if isinstance(payload, dict) else payload}")
    data = payload.get("data") or {}
    categories_raw = data.get("categories") or []
    meals_map = data.get("meals") or {}
    if not isinstance(meals_map, dict):
        raise MenuParseError("query-meals.data.meals 结构异常。")

    menu = MenuData()
    seen: set[str] = set()

    for cat in categories_raw:
        cat_name = str(cat.get("name", "")).strip()
        codes: list[str] = []
        for m in cat.get("meals") or []:
            code = str(m.get("code", "")).strip()
            if not code:
                continue
            codes.append(code)
            if code in seen:
                continue
            seen.add(code)
            detail = meals_map.get(code) or {}
            tags = list(m.get("tags") or detail.get("tags") or [])
            item = MenuItem(
                code=code,
                name=str(detail.get("name") or m.get("name") or "").strip(),
                price_yuan=_to_float(detail.get("currentPrice")),
                original_price_yuan=_to_float(detail.get("originalPrice")),
                discount_type=(detail.get("discountType") or None),
                can_with_order=bool(detail.get("canWithOrder", False)),
                tags=[str(t) for t in tags],
                image=str(detail.get("image") or ""),
            )
            menu.items.append(item)
            menu.by_code[code] = item
            if item.name and item.name not in menu.by_name:
                menu.by_name[item.name] = item
        if cat_name:
            menu.categories[cat_name] = codes

    return menu


def parse_meal_detail(payload: dict[str, Any]) -> MealDetail:
    """把 ``query-meal-detail`` 返回解析为 :class:`MealDetail`。

    除特调项外，还解析 ``data.rounds[]`` 的**默认组成**：
    每个分组（round）取 ``isDefault == 1`` 的选项，作为该套餐的默认构成。
    若某分组没有任何默认项，则该分组的构成**未知**——此时不臆造，
    整份组成视为不可靠（由调用方决定是否采用，见 :mod:`mcpilot.nutrition`）。
    """
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise MenuParseError(f"query-meal-detail 返回失败：{payload.get('message')}")
    data = payload.get("data") or {}
    code = str(data.get("code", "")).strip()
    mods: list[ModificationValue] = []
    mod_block = data.get("modification") or {}
    for group in mod_block.get("items") or []:
        for v in group.get("values") or []:
            mods.append(
                ModificationValue(
                    code=str(v.get("code", "")),
                    name=str(v.get("name", "")),
                    price=int(v.get("price") or 0),
                    selected_quantity=int(v.get("selectedQuantity") or 0),
                    selected_key=str(v.get("selectedKey") or ""),
                    unselected_key=str(v.get("unselectedKey") or ""),
                )
            )

    components: list[SetComponent] = []
    for rnd in data.get("rounds") or []:
        if not isinstance(rnd, dict):
            continue
        defaults = [c for c in (rnd.get("choices") or []) if int(c.get("isDefault") or 0) == 1]
        for ch in defaults:
            qty = int(ch.get("quantity") or 0)
            if qty <= 0:
                continue
            components.append(
                SetComponent(
                    name=str(ch.get("name", "")).strip(),
                    code=str(ch.get("code", "")).strip(),
                    quantity=qty,
                )
            )

    return MealDetail(
        code=code,
        name=str(data.get("name", "")).strip(),
        support_modify=bool(data.get("supportModify", False)),
        image=str(data.get("image") or ""),
        modifications=mods,
        components=components,
    )
