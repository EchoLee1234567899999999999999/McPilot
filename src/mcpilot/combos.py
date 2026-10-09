"""候选组合生成与约束（Phase 2）。

本模块**只做组合枚举与粗筛**，不产生任何"最终价格"：
- 价格仅使用 ``query-meals`` 的展示价（**元**）做**筛选/排序**，最终实付必须由
  ``calculate-price`` 真实试算得出（见 :mod:`mcpilot.recommender`）。
- 组合只由**门店真实在售**且**真实在菜单中**的商品构成。

设计要点：
1. **角色分类**（main/side/drink/dessert/set）用于**生成**合理搭配，属启发式，
   不对外宣称为客观营养结论。
2. **候选规模受限**：每个角色只取最便宜的 K 个，避免无限排列组合。
3. **硬过滤**：忌口关键词（命中商品名即排除）、预算粗筛、指定商品必含。
4. **去重**：按 (编码, 数量) 排序签名去重。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .menu import MenuData
from .models import ComboItem, MenuItem

__all__ = [
    "ROLE_MAIN",
    "ROLE_SIDE",
    "ROLE_DRINK",
    "ROLE_DESSERT",
    "ROLE_SET",
    "classify_role",
    "ComboConstraints",
    "Candidate",
    "generate_candidates",
    "yuan_to_cent",
]

ROLE_MAIN = "main"
ROLE_SIDE = "side"
ROLE_DRINK = "drink"
ROLE_DESSERT = "dessert"
ROLE_SET = "set"

#: 「主食」角色集合：一份合理的一餐至少要包含其一（主餐或套餐）。
STAPLE_ROLES: frozenset[str] = frozenset({ROLE_MAIN, ROLE_SET})


def is_staple_role(role: str) -> bool:
    """该角色是否属于「主食」（main / set）。"""
    return role in STAPLE_ROLES

# --- 商品名为「非餐品」（周边/纯调味酱），不参与组合 -----------------------
_NON_MEAL_HINTS = ("棒球帽", "按摩捶", "痒痒挠", "百宝袋")
# 纯调味酱：以「酱」结尾（如「韩式烟熏芝士风味酱」）；「蘸酱炸鸡」等仍是餐品。
_CONDIMENT_SUFFIX = "酱"

# 判定顺序很重要：set → dessert → drink → side → main
_SET_HINTS = (
    "套餐",
    "三件套",
    "四件套",
    "随心配",
    "单人餐",
    "双人餐",
    "分享餐",
    "开心乐园",
    "双全盒",
    "任选",
)
_DESSERT_HINTS = ("冰淇淋", "圆筒", "新地", "麦旋风", "派", "扭扭薯")
_DRINK_HINTS = (
    "可乐",
    "雪碧",
    "怡泉",
    "美汁源",
    "柠柠",
    "橙橙",
    "红茶",
    "麦炫酷",
    "麦咖啡",
    "咖啡",
    "美式",
    "奶铁",
    "卡布奇诺",
    "抹茶",
    "牛奶",
    "燕麦奶",
    "苹果汁",
    "奶绿",
    "雪冰",
)
_SIDE_HINTS = (
    "薯条",
    "薯饼",
    "玉米杯",
    "鸡翅",
    "V翅",
    "麦乐鸡",
    "鸡块",
    "鸡柳",
    "脆汁鸡",
    "炸鸡",
    "芝士条",
    "苹果片",
    "小食盘",
)
_MAIN_HINTS = ("堡", "巨无霸", "麦香鸡", "麦香鱼", "卷", "鸡排")


def yuan_to_cent(yuan: float) -> int:
    """元 → 分（四舍五入到整数分），避免浮点误差累积。"""
    return int(round(float(yuan) * 100))


def classify_role(name: str) -> Optional[str]:
    """按商品名把餐品分到生成角色；无法归类返回 None（不猜测）。"""
    n = str(name or "")
    if not n:
        return None
    if any(h in n for h in _NON_MEAL_HINTS):
        return None
    if n.endswith(_CONDIMENT_SUFFIX):
        return None
    for hints, role in (
        (_SET_HINTS, ROLE_SET),
        (_DESSERT_HINTS, ROLE_DESSERT),
        (_DRINK_HINTS, ROLE_DRINK),
        (_SIDE_HINTS, ROLE_SIDE),
        (_MAIN_HINTS, ROLE_MAIN),
    ):
        if any(h in n for h in hints):
            return role
    return None


@dataclass
class ComboConstraints:
    """组合生成约束。"""

    budget_cent: Optional[int] = None  # 预算上限（分）；None 表示不限
    people: int = 1  # 用餐人数（≥1）
    dislikes: list[str] = field(default_factory=list)  # 忌口关键词
    include_codes: list[str] = field(default_factory=list)  # 必须包含的商品
    #: 是否要求候选至少含一份「主食」（main / set）——避免把单独一份饮料/小食当"一餐"。
    require_staple: bool = True
    # 额外必须纳入候选池的商品（如"营养可匹配且蛋白最高"的商品），
    # 使其不受"只取最便宜 K 个"的限制。
    extra_codes: list[str] = field(default_factory=list)
    max_distinct_items: int = 3  # 组合中不同商品数上限
    pool_main: int = 5
    pool_side: int = 4
    pool_drink: int = 4
    pool_dessert: int = 3
    pool_set: int = 8
    max_candidates: int = 2000  # 候选总数上限（性能约束）


@dataclass
class Candidate:
    """候选组合（价格仅为展示价估算，非最终价）。"""

    items: list[ComboItem] = field(default_factory=list)
    est_subtotal_cent: int = 0  # 展示价 × 数量（仅筛选用）
    roles: list[str] = field(default_factory=list)
    is_set: bool = False  # 是否为单件套餐（本身即完整一餐）
    single_item: bool = False  # 是否只有一个不同商品

    @property
    def has_staple(self) -> bool:
        """是否含「主食」（main / set）——一份合理的一餐的必要条件。"""
        return any(is_staple_role(r) for r in self.roles)

    @property
    def codes(self) -> list[str]:
        return [i.code for i in self.items]

    @property
    def total_quantity(self) -> int:
        return sum(i.quantity for i in self.items)

    @property
    def signature(self) -> str:
        return "|".join(sorted(f"{i.code}x{i.quantity}" for i in self.items))

    def name_list(self) -> str:
        return " + ".join(f"{i.name}×{i.quantity}" for i in self.items)


def _unit_cent(item: MenuItem) -> Optional[int]:
    if item.price_yuan is None:
        return None
    return yuan_to_cent(item.price_yuan)


def _disliked(item: MenuItem, dislikes: Iterable[str]) -> bool:
    """忌口命中判定：检查商品名与商品标签（标签可能含风味/属性信息）。

    注意：这里只做**字面包含**判定（用户说"不吃辣"→命中含"辣"的名称或标签），
    不做语义推断；无法确认时不会自作主张排除。
    """
    kws = [str(d).strip() for d in dislikes if str(d).strip()]
    if not kws:
        return False
    haystack = [item.name, *item.tags]
    return any(k in h for k in kws for h in haystack)


def _pool(
    menu: MenuData,
    role: str,
    k: int,
    *,
    dislikes: list[str],
    exclude: set[str],
    extra: Iterable[MenuItem] = (),
) -> list[MenuItem]:
    """取某角色候选：最便宜的 k 个 **并集** 额外指定的高价值商品。

    ``extra`` 用于把"营养可靠且蛋白高"等商品纳入候选池，避免只按价格截断。
    忌口与缺价商品始终排除。
    """
    chosen: dict[str, MenuItem] = {}

    def ok(it: MenuItem) -> bool:
        if it.code in exclude or it.code in chosen:
            return False
        if _unit_cent(it) is None or it.price_yuan is None or it.price_yuan <= 0:
            return False
        if _disliked(it, dislikes):
            return False
        return classify_role(it.name) == role

    # 额外指定的商品优先纳入
    for it in extra:
        if ok(it):
            chosen[it.code] = it
    # 最便宜的 k 个
    cheap = [it for it in menu.items if ok(it)]
    cheap.sort(key=lambda x: (x.price_yuan or 0, x.code))
    for it in cheap[: max(0, k)]:
        chosen[it.code] = it
    return sorted(chosen.values(), key=lambda x: (x.price_yuan or 0, x.code))


def generate_candidates(
    menu: MenuData,
    constraints: ComboConstraints,
) -> list[Candidate]:
    """生成候选组合列表。

    组合模式（``n = people``，每样按人数取量）：
    - ``set``            ：单件套餐（本身即完整一餐）；
    - ``main+side+drink`` / ``main+side`` / ``main+drink`` / ``main+dessert``；
    - ``main``           ：仅主餐（**兜底**，会在候选中标记为 ``single_item``）。

    指定商品（``include_codes``）会**固定在每个候选**中；若其角色已被占位，
    该角色不再另加商品。组合数与每个角色的池大小均受限，避免组合爆炸。
    """
    people = max(1, int(constraints.people))
    used: set[str] = set()

    # 指定商品（必须真实在售）
    required: list[MenuItem] = []
    required_roles: set[str] = set()
    for code in constraints.include_codes:
        item = menu.get(str(code))
        if item is None:
            raise ValueError(f"指定商品编码 {code} 不在门店在售菜单中。")
        if _unit_cent(item) is None:
            raise ValueError(f"指定商品 {code} 缺少价格，无法参与组合。")
        required.append(item)
        used.add(item.code)
        r = classify_role(item.name)
        if r is not None:
            required_roles.add(r)

    # 额外商品（按角色归类）
    extra_items: list[MenuItem] = []
    for code in constraints.extra_codes:
        it = menu.get(str(code))
        if it is not None:
            extra_items.append(it)

    pools = {
        ROLE_SET: _pool(
            menu, ROLE_SET, constraints.pool_set, dislikes=constraints.dislikes, exclude=used, extra=extra_items
        ),
        ROLE_MAIN: _pool(
            menu, ROLE_MAIN, constraints.pool_main, dislikes=constraints.dislikes, exclude=used, extra=extra_items
        ),
        ROLE_SIDE: _pool(
            menu, ROLE_SIDE, constraints.pool_side, dislikes=constraints.dislikes, exclude=used, extra=extra_items
        ),
        ROLE_DRINK: _pool(
            menu, ROLE_DRINK, constraints.pool_drink, dislikes=constraints.dislikes, exclude=used, extra=extra_items
        ),
        ROLE_DESSERT: _pool(
            menu,
            ROLE_DESSERT,
            constraints.pool_dessert,
            dislikes=constraints.dislikes,
            exclude=used,
            extra=extra_items,
        ),
    }

    candidates: list[Candidate] = []
    seen: set[str] = set()

    def add(items: list[tuple[MenuItem, int, str]]) -> None:
        # 去重（同编码合并数量）
        merged: dict[str, tuple[MenuItem, int, str]] = {}
        for it, qty, role in items:
            if it.code in merged:
                mi, mq, mr = merged[it.code]
                merged[it.code] = (mi, mq + qty, mr)
            else:
                merged[it.code] = (it, qty, role)
        objs: list[ComboItem] = []
        roles: list[str] = []
        est = 0
        for it, qty, role in merged.values():
            objs.append(ComboItem(code=it.code, name=it.name, quantity=qty))
            roles.append(role)
            est += (_unit_cent(it) or 0) * qty
        if not objs:
            return
        if len(objs) > constraints.max_distinct_items:
            return
        if sum(o.quantity for o in objs) < people:
            # 份额不足：不允许把"明显不满足用餐人数"的组合纳入候选
            return
        cand = Candidate(
            items=objs,
            est_subtotal_cent=est,
            roles=roles,
            is_set=all(r == ROLE_SET for r in roles),
            single_item=len(objs) == 1,
        )
        if constraints.require_staple and not cand.has_staple:
            # 不含主食（例如仅一份饮料/甜点）→ 不作为"一餐"候选
            return
        if cand.signature in seen:
            return
        seen.add(cand.signature)
        candidates.append(cand)

    base = [(it, people, classify_role(it.name) or ROLE_MAIN) for it in required]
    base_roles = {r for _, _, r in base}

    def fill(role: str, qty: int) -> list[tuple[MenuItem, int, str]]:
        """若角色未被指定商品占位，返回该角色候选（用于拼接组合）。"""
        if role in base_roles:
            return []
        return [(it, qty, role) for it in pools[role]]

    # 1) 套餐（单件即完整一餐；按人数配份）
    for s in fill(ROLE_SET, people):
        add(base + [s])

    # 2) 主餐为骨架的搭配（主餐已被指定时，仅用指定主餐）
    main_options: list[Optional[MenuItem]]
    if ROLE_MAIN in base_roles:
        main_options = [None]
    else:
        main_options = list(pools[ROLE_MAIN]) or [None]

    # 指定商品本身即可能构成完整一餐（例如指定一个"套餐"）：先给出"仅指定商品"的候选。
    if base:
        add(list(base))

    for m in main_options:
        m_items = list(base)
        if m is not None:
            m_items.append((m, people, ROLE_MAIN))
        # main（兜底单品；若已含指定商品则非单品）
        add(m_items)
        # main + 单配（drink / side / dessert）
        for extra_role in (ROLE_DRINK, ROLE_SIDE, ROLE_DESSERT):
            for extra_item in fill(extra_role, people):
                add(m_items + [extra_item])
        # main + side + drink
        for s in fill(ROLE_SIDE, people):
            for d in fill(ROLE_DRINK, people):
                add(m_items + [s, d])
        # main + side + dessert
        for s in fill(ROLE_SIDE, people):
            for ds in fill(ROLE_DESSERT, people):
                add(m_items + [s, ds])

    # 3) 规模上限：按估算金额升序保留（保证预算敏感的组合优先保留）
    if len(candidates) > constraints.max_candidates:
        candidates.sort(key=lambda c: (c.est_subtotal_cent, c.signature))
        candidates = candidates[: constraints.max_candidates]

    return candidates


def candidates_stats(candidates: list[Candidate]) -> dict[str, Any]:
    """候选集合的统计（用于报告/测试）。"""
    return {
        "count": len(candidates),
        "min_est_cent": min((c.est_subtotal_cent for c in candidates), default=0),
        "max_est_cent": max((c.est_subtotal_cent for c in candidates), default=0),
        "sets": sum(1 for c in candidates if c.is_set),
        "single_item": sum(1 for c in candidates if c.single_item),
    }
