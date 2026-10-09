"""三种推荐策略与统一入口（Phase 2 实现 / Phase 3 质量与稳定性优化）。

策略（与需求一一对应）：
- ``budget``    省钱型：在**满足用餐需求**（含主食、满足人数与指定商品）的前提下，**实付最低**。
- ``nutrition`` 高蛋白型：预算内**蛋白质最高**（仅统计营养可靠匹配的组合）。
- ``balanced``  综合型：预算 / 口味 / 蛋白质性价比 / 分量 / 数据完整度 的**可解释加权**评分。

Phase 3 的关键增强
------------------
1. **营养分层匹配**：菜单简名 → 归一化 → 人工别名 → ``query-meal-detail`` 规范全名 → 套餐组成。
   仍严格遵循"不确定即标缺失、绝不猜测"（见 :mod:`mcpilot.nutrition`）。
2. **按需解析且有界**：仅对**进入试算的候选**做 detail 解析，受 ``request.max_resolve`` 限制，
   同一名称只解析一次（见 :mod:`mcpilot.resolve`）。
3. **硬约束严格执行**：忌口（名称 / 标签 / 套餐组成）、指定商品、营养上限都是硬门槛，
   不满足者**直接剔除**，绝不作为"最优推荐"输出。
4. **只看真实价**：任何"实付/优惠"都来自 ``calculate-price``；展示价仅用于粗筛，
   **不缓存价格**、不以缓存冒充实时。

统一流程（**价格试算必须最后**）::

    门店菜单 → 营养表 → 门店券 → 候选组合生成（仅展示价粗筛）
        → 选取少量候选做真实 calculate-price 试算（+ 有界 detail 解析营养）
        → 硬约束过滤 → 三策略打分 → 取最优并生成理由
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from .advice import BlockInfo, blocked_summary, build_adjustments, summary_warning
from .combos import (
    ROLE_DESSERT,
    ROLE_DRINK,
    ComboConstraints,
    Candidate,
    generate_candidates,
    is_staple_role,
    yuan_to_cent,
)
from .coupon import (
    COUPON_SOURCES,
    CouponMatch,
    active_coupons,
    best_coupon,
    coupons_in_menu,
    match_coupons,
    parse_coupons,
)
from .mcp_client import McpClient, McpError
from .menu import MenuData, parse_menu
from .models import (
    STRATEGY_LABELS,
    Adjustment,
    Coupon,
    Nutrition,
    RecommendedItem,
    Recommendation,
    RecommendationPlan,
    Strategy,
    UserRequest,
)
from .nutrition import (
    TIER_MISSING,
    NutritionIndex,
    NutritionTotal,
    ResolvedNutrition,
    build_index,
    total_from_resolved,
)
from .pricing import cent_to_yuan, quote
from .resolve import NutritionEnricher

__all__ = [
    "recommend",
    "score_budget",
    "score_nutrition",
    "score_balanced",
    "build_reasons",
    "wants_max_protein",
    "protein_per_100kcal",
    "protein_per_yuan",
    "DATA_SOURCE",
    "PROGRESS_STAGES",
    "main",
]

# 进度事件回调：接收 ``{"stage","status","detail","index","total"}``。
# 回调异常**绝不**影响推荐本身（进度只是观测手段，不是业务逻辑）。
ProgressCallback = Callable[[dict[str, Any]], None]

# 供前端渲染"步骤条"的稳定阶段标识（顺序即展示顺序）。
PROGRESS_STAGES: tuple[tuple[str, str], ...] = (
    ("store", "确认门店"),
    ("menu", "查询门店菜单"),
    ("nutrition", "获取营养数据"),
    ("coupon", "核验门店优惠券"),
    ("candidates", "生成候选组合"),
    ("resolve", "营养按需解析"),
    ("price", "真实价格试算"),
    ("score", "方案打分择优"),
)

DATA_SOURCE = (
    "麦当劳官方 MCP（实时只读）· "
    "工具：query-meals / list-nutrition-foods / query-store-coupons / query-meal-detail / calculate-price"
)

_ALL_STRATEGIES: tuple[Strategy, ...] = ("budget", "nutrition", "balanced")


# ---------------------------------------------------------------------------
# 上下文与特征
# ---------------------------------------------------------------------------


#: 关键词——用户**明确提出**要把"绝对蛋白质吃满"时才切换为纯总量优先。
_PROTEIN_MAX_HINTS: tuple[str, ...] = (
    "蛋白质最高",
    "蛋白最高",
    "蛋白最多",
    "蛋白质最多",
    "最高蛋白",
    "蛋白拉满",
    "蛋白质拉满",
    "蛋白质越多越好",
    "蛋白越多越好",
    "max protein",
    "protein max",
)


def wants_max_protein(likes: Iterable[str], goals: Any = None) -> bool:
    """用户是否**明确**要求"最大化绝对蛋白质"。

    判定依据只有两条（可解释、可审计）：
    1. 在口味偏好 / 补充需求里出现明确的"要最多蛋白"表述（见 ``_PROTEIN_MAX_HINTS``）；
    2. 在营养目标里给出**较高的蛋白质下限**（≥ 30 g，即"我要很多蛋白"的量化表达）。

    未命中时返回 ``False``——此时才启用蛋白质效率（蛋白/热量、蛋白/价格）作为
    **辅助比较指标**，避免把"热量更低但蛋白同样多的更划算组合"排到后面。
    """
    text = " ".join(str(x).strip().lower() for x in likes if str(x).strip())
    if any(hint.lower() in text for hint in _PROTEIN_MAX_HINTS):
        return True
    if goals is not None:
        pmin = getattr(goals, "protein_g_min", None)
        if pmin is not None:
            try:
                if float(pmin) >= 30.0:
                    return True
            except (TypeError, ValueError):
                pass
    return False


@dataclass
class _Ctx:
    """打分所需的归一化上下文（全部来自真实数据）。"""

    budget_cent: Optional[int]
    max_payable_cent: int = 1
    max_protein_g: float = 0.0
    max_kcal: float = 0.0
    max_protein_per_yuan: float = 0.0  # 蛋白质/元 的最大值（性价比用）
    max_protein_per_100kcal: float = 0.0  # 蛋白质/100kcal 的最大值（热量效率用）
    likes: list[str] = field(default_factory=list)
    people: int = 1
    kcal_target: Optional[float] = None  # 用户给出的热量目标（可选）
    #: 用户是否**明确**要求最大化绝对蛋白质（直接决定高蛋白策略的主排序口径）
    explicit_max_protein: bool = False


@dataclass
class _Feat:
    """候选组合的真实特征（价格来自 calculate-price，营养来自营养表/详情）。"""

    cand: Candidate
    payable_cent: int
    original_cent: int
    discount_cent: int
    est_subtotal_cent: int
    coupon: Optional[Coupon]
    protein_g: Optional[float]
    kcal: Optional[float]
    complete: bool
    missing: list[str]
    taste_hits: list[str]
    distinct: int
    qty: int
    is_set: bool
    single_item: bool
    nutrition_total: Any = None  # NutritionTotal（仅计入可靠匹配项）
    item_nutrition: dict[str, Optional[Nutrition]] = field(default_factory=dict)
    line_by_code: dict[str, tuple[int, int]] = field(default_factory=dict)  # code -> (原价分, 应付分)
    resolved: dict[str, ResolvedNutrition] = field(default_factory=dict)  # 名称 -> 解析结果
    composition_used: bool = False  # 是否依据"套餐组成"计算营养
    roles: list[str] = field(default_factory=list)  # 各商品角色（与 cand.items 一一对应）
    dessert_kcal: Optional[float] = None  # 组合中甜品/饮料部分的热量（仅可靠匹配时）
    dessert_sugar_share: float = 0.0  # 甜品/饮料热量占组合总热量比例（0~1）

    def name_list(self) -> str:
        return self.cand.name_list()

    def treat_items(self) -> list[str]:
        """组合中的「甜品 / 饮料 / 冰淇淋」类商品名（用于可解释说明）。"""
        out: list[str] = []
        for it, role in zip(self.cand.items, self.roles or [""] * len(self.cand.items)):
            if role in (ROLE_DESSERT, ROLE_DRINK):
                out.append(it.name)
        return out


# ---------------------------------------------------------------------------
# 打分（越高越好；规则明确、可解释）
# ---------------------------------------------------------------------------


def _portion_fit(f: _Feat, people: int) -> float:
    """分量充足度（0~1）：以"每人 2 件"为满分，避免为省钱导致分量过少。"""
    target = max(2, 2 * max(1, people))
    return min(1.0, f.qty / target)


def score_budget(f: _Feat, ctx: _Ctx) -> float:
    """省钱型打分 = 0.85·实付占优 + 0.10·节省率 + 0.05·分量充足度。

    实付权重最高，确保"优先选择实付较低的方案"；同时用节省率与分量做次级区分，
    避免把"明显不满足用餐需求"的极简组合判为最优。
    候选生成阶段已强制**含主食**并满足人数/指定商品（见 ``combos.require_staple``）。
    """
    pay_score = 1.0 - (f.payable_cent / max(1, ctx.max_payable_cent))
    savings = (f.discount_cent / f.original_cent) if f.original_cent else 0.0
    return 0.85 * pay_score + 0.10 * savings + 0.05 * _portion_fit(f, ctx.people)


def protein_per_100kcal(protein_g: Optional[float], kcal: Optional[float]) -> Optional[float]:
    """蛋白质密度：每 100 kcal 的蛋白质克数（热量效率，可解释）。

    两者都必须已知且热量 > 0 才计算，否则返回 ``None``（不猜测）。
    """
    if protein_g is None or kcal is None or kcal <= 0:
        return None
    return float(protein_g) / (float(kcal) / 100.0)


def protein_per_yuan(protein_g: Optional[float], payable_cent: int) -> Optional[float]:
    """蛋白质性价比：每元实付的蛋白质克数（可解释）。"""
    if protein_g is None or payable_cent <= 0:
        return None
    return float(protein_g) / (float(payable_cent) / 100.0)


def score_nutrition(f: _Feat, ctx: _Ctx) -> Optional[float]:
    """高蛋白型打分（**仅当组合营养数据可靠时**返回分数，否则 ``None``）。

    口径分两种，由 ``ctx.explicit_max_protein`` 决定（用户是否明确要求"蛋白最多"）：

    - **明确要求最大化绝对蛋白质** → 以**总量**为主：
      ``0.70·蛋白质总量 + 0.15·热量偏好 + 0.15·预算利用``。
    - **未明确要求**（默认）→ 以**蛋白质密度**为主，把"同样蛋白但更清淡/更划算"的组合
      排到前面：``0.50·蛋白质密度(蛋白/100kcal) + 0.20·蛋白质总量 + 0.15·热量偏好 + 0.15·预算利用``。

    两种口径都**只用可可靠匹配营养的组合**；营养未知一律返回 ``None``，不按 0 计算。
    用户给出的蛋白质下限 / 热量上限始终作为**硬约束**先行过滤，不进入打分。
    """
    if not f.complete or f.protein_g is None:
        return None
    protein_fit = (f.protein_g / ctx.max_protein_g) if ctx.max_protein_g else 0.0

    kcal = f.kcal or 0.0
    if ctx.kcal_target:
        energy_fit = max(0.0, 1.0 - abs(kcal - ctx.kcal_target) / ctx.kcal_target)
    elif ctx.max_kcal:
        energy_fit = 1.0 - kcal / ctx.max_kcal
    else:
        energy_fit = 0.5
    energy_fit = max(0.0, min(1.0, energy_fit))

    if ctx.budget_cent:
        budget_util = max(0.0, min(1.0, 1.0 - f.payable_cent / ctx.budget_cent))
    else:
        budget_util = 0.5

    if ctx.explicit_max_protein:
        return 0.70 * protein_fit + 0.15 * energy_fit + 0.15 * budget_util

    density = protein_per_100kcal(f.protein_g, f.kcal)
    density_fit = (
        min(1.0, density / ctx.max_protein_per_100kcal)
        if (density is not None and ctx.max_protein_per_100kcal > 0)
        else 0.0
    )
    return (
        0.50 * density_fit
        + 0.20 * protein_fit
        + 0.15 * energy_fit
        + 0.15 * budget_util
    )


def _treat_fit(f: _Feat, likes: list[str]) -> Optional[float]:
    """甜品 / 饮料 / 冰淇淋的「适配度」（0~1，可解释）。

    本函数**不禁止**甜品/饮料/冰淇淋，而是在有真实营养数据时判断"值不值得加"：

    - 用户点名喜欢（``likes`` 命中甜品/饮料名称）→ 直接给 1.0（尊重偏好）；
    - 否则按甜品/饮料**占总热量的比例**评价：占比适中（≤ 25%）得高分，
      占比过高（把一餐变成甜品餐）逐步降分；
    - 组合里没有甜品/饮料 → 返回 ``None``（该维度**不参与**，不补 0）。

    只有在组合营养**完整可靠**时才计算；未知一律 ``None``（不猜测）。
    """
    names = f.treat_items()
    if not names:
        return None
    if any(any(str(k).strip() and str(k).strip() in n for k in likes) for n in names):
        return 1.0
    if f.dessert_kcal is None or f.kcal is None or f.kcal <= 0:
        return None
    share = f.dessert_sugar_share
    if share <= 0.25:
        return 1.0
    if share >= 0.60:
        return 0.4
    # 0.25 ~ 0.60 线性下降：0.25→1.0，0.60→0.4
    return 1.0 - (share - 0.25) / 0.35 * 0.6


def score_balanced(f: _Feat, ctx: _Ctx) -> tuple[float, dict[str, float]]:
    """综合型打分：对**可用**维度加权平均（缺失维度不参与、不补 0）。

    设计目标是把"综合型"做成**性价比**取舍，而不是与省钱/高蛋白重复：
    维度与权重 —— 蛋白质性价比 0.30 / 甜点搭配度 0.12 / 分量充足度 0.13 /
    口味命中度 0.15 / 预算余量 0.18 / 数据完整度 0.12。
    - 蛋白质性价比 = 蛋白质(g) ÷ 实付(元)，按候选中的最大值归一化（仅可靠营养参与）。
    - 甜点搭配度 = 依据**真实营养**判断甜品/饮料是否值得加入（见 :func:`_treat_fit`）：
      用户点名喜欢 → 满分；否则按甜品热量占比适中最优，占比过高则降分；
      组合无甜品/饮料时该维度**不参与**。本维度只用于"综合型"，不影响省钱/高蛋白口径。
    - 营养数据不完整时，性价比与甜点搭配度维度**不参与**（不把未知当 0），
      且"数据完整度"归零。
    - 用户未给口味偏好时，口味维度不参与（权重分摊给其余维度）。
    - 分量充足度只统计"件数"这一客观量，不使用任何主观饱腹感判断。
    """
    comps: dict[str, float] = {}
    # 蛋白质性价比（仅可靠营养）
    per_yuan = protein_per_yuan(f.protein_g, f.payable_cent) if f.complete else None
    if per_yuan is not None and ctx.max_protein_per_yuan > 0:
        comps["蛋白质性价比"] = min(1.0, per_yuan / ctx.max_protein_per_yuan)
    # 甜点搭配度（甜品/饮料；依据真实营养与用户偏好）
    if f.complete:
        treat = _treat_fit(f, ctx.likes)
        if treat is not None:
            comps["甜点搭配度"] = treat
    # 分量充足度（客观件数）
    comps["分量充足度"] = _portion_fit(f, ctx.people)
    # 口味命中度
    if ctx.likes:
        comps["口味命中度"] = len(f.taste_hits) / len(ctx.likes)
    # 预算余量（越省越大，仅作温和的成本偏好，避免与省钱型重复）
    base_pay = ctx.budget_cent or ctx.max_payable_cent or 1
    comps["预算余量"] = max(0.0, min(1.0, 1.0 - f.payable_cent / base_pay))
    # 数据完整度
    comps["数据完整度"] = 1.0 if f.complete else 0.0

    weights = {
        "蛋白质性价比": 0.30,
        "甜点搭配度": 0.12,
        "分量充足度": 0.13,
        "口味命中度": 0.15,
        "预算余量": 0.18,
        "数据完整度": 0.12,
    }
    total_w = sum(weights[k] for k in comps)
    score = sum(weights[k] * v for k, v in comps.items()) / total_w if total_w else 0.0
    return score, comps


# ---------------------------------------------------------------------------
# 数据获取与试算
# ---------------------------------------------------------------------------


def _fetch(
    client: McpClient,
    request: UserRequest,
    emit: Optional[ProgressCallback] = None,
) -> tuple[MenuData, NutritionIndex, list[Coupon], str, dict[str, int]]:
    """拉取菜单 / 营养 / 券。任何失败都抛出带上下文的异常（不编造）。

    注意：**不做任何跨请求缓存**——每次调用都取实时数据，避免用缓存价冒充实时价。

    ``emit`` 会在**每次真实 MCP 调用前后**发出进度事件，供界面如实反映处理状态。
    """
    calls = {"query-meals": 1, "list-nutrition-foods": 1, "query-store-coupons": 1}
    if not request.store_code:
        raise ValueError("缺少门店编码 store_code（请先执行门店查询）。")
    be_type = int(request.be_type)

    _emit = emit or (lambda *a, **k: None)
    _emit("store", "done", f"门店 {request.store_code}（beType={be_type}）")
    _emit("menu", "start", "调用 query-meals")

    menu_payload = client.query_meals(
        store_code=request.store_code, be_type=be_type, be_code=request.be_code  # type: ignore[arg-type]
    )
    menu = parse_menu(menu_payload)
    if len(menu) == 0:
        raise ValueError(f"门店 {request.store_code} 未返回任何在售餐品。")
    _emit("menu", "done", f"在售 {len(menu)} 件")
    _emit("nutrition", "start", "调用 list-nutrition-foods")

    nutrition_index = build_index(client.list_nutrition_foods())
    _emit("nutrition", "done", f"营养表 {len(nutrition_index)} 条")
    _emit("coupon", "start", "调用 query-store-coupons")

    coupons = parse_coupons(
        client.query_store_coupons(
            store_code=request.store_code, be_type=be_type, be_code=request.be_code  # type: ignore[arg-type]
        )
    )
    _emit("coupon", "done", f"门店券 {len(coupons)} 张")

    queried_at = str(menu_payload.get("datetime") or "").strip()
    return menu, nutrition_index, coupons, queried_at, calls


def _estimate_combo_nutrition(
    cand: Candidate, enricher: NutritionEnricher
) -> tuple[Optional[float], Optional[float], bool, list[str]]:
    """用**静态**分层匹配估算组合蛋白质/热量（不触发任何 MCP 调用）。

    用于试算前的候选排序；解析不到的项进入 missing。
    """
    resolved = enricher.resolve_all([i.name for i in cand.items], allow_fetch=False)
    tot = total_from_resolved([(i.name, i.quantity) for i in cand.items], resolved)
    if tot.has_missing:
        return None, None, False, tot.missing
    return tot.total.protein_g, tot.total.energy_kcal, True, []


def _taste_hits(cand: Candidate, likes: Iterable[str]) -> list[str]:
    hit: list[str] = []
    for kw in likes:
        k = str(kw).strip()
        if k and any(k in i.name for i in cand.items) and k not in hit:
            hit.append(k)
    return hit


def _applicable_coupon(cand: Candidate, coupons: list[Coupon]) -> Optional[CouponMatch]:
    """按真实编码比对判断券对候选组合是否适用（含时效判定）。"""
    matches = match_coupons(cand.codes, coupons)
    return best_coupon(matches)


def _verify(
    client: McpClient, request: UserRequest, cand: Candidate, coupon_match: Optional[CouponMatch]
) -> Any:
    """对单个候选做真实价格试算（有适用券则带上券，得到真实优惠）。"""
    coupon = coupon_match.coupon if coupon_match else None
    on_code = coupon_match.matched_codes[0] if (coupon_match and coupon_match.matched_codes) else None
    return quote(
        client,
        store_code=request.store_code or "",
        be_type=int(request.be_type),
        items=cand.items,
        be_code=request.be_code,
        coupon=coupon,
        coupon_on_code=on_code,
    )


def _build_feat(
    cand: Candidate,
    quoted: Any,
    enricher: NutritionEnricher,
    coupon: Optional[Coupon],
    likes: list[str],
) -> _Feat:
    """把候选 + 真实试算结果 + 营养解析结果组装为 :class:`_Feat`。

    营养解析在此处**允许按需拉取详情**（受 ``enricher.max_calls`` 限制），
    因为候选已经进入试算集合、数量有限。
    """
    item_names = [i.name for i in cand.items]
    resolved = enricher.resolve_all(item_names, allow_fetch=True)
    tot = total_from_resolved([(i.name, i.quantity) for i in cand.items], resolved)
    complete = tot.complete

    item_nutrition: dict[str, Optional[Nutrition]] = {}
    for it in cand.items:
        r = resolved.get(it.name)
        item_nutrition[it.code] = (r.nutrition if (r is not None and r.ok) else None)

    line_by_code: dict[str, tuple[int, int]] = {
        line.product_code: (int(line.original_subtotal_cent), int(line.subtotal_cent))
        for line in quoted.lines
    }
    composition_used = any(
        (resolved.get(n) is not None and resolved[n].tier == "composition") for n in item_names
    )
    # 甜品 / 饮料部分的热量（仅可靠匹配时计入；用于"是否值得加入"的可解释判断）
    dessert_kcal: Optional[float] = None
    if complete:
        d_sum = 0.0
        d_known = False
        for it, role in zip(cand.items, cand.roles):
            if role not in (ROLE_DESSERT, ROLE_DRINK):
                continue
            nut = item_nutrition.get(it.code)
            if nut is not None and nut.energy_kcal is not None:
                d_sum += float(nut.energy_kcal) * int(it.quantity)
                d_known = True
        dessert_kcal = d_sum if d_known else None
    total_kcal = tot.total.energy_kcal if complete else None
    dessert_share = (
        float(dessert_kcal) / float(total_kcal)
        if (dessert_kcal is not None and total_kcal and total_kcal > 0)
        else 0.0
    )
    return _Feat(
        cand=cand,
        payable_cent=int(quoted.payable_cent),
        original_cent=int(quoted.original_price_cent),
        discount_cent=int(quoted.discount_cent),
        est_subtotal_cent=cand.est_subtotal_cent,
        coupon=coupon,
        protein_g=tot.total.protein_g if complete else None,
        kcal=tot.total.energy_kcal if complete else None,
        complete=complete,
        missing=list(tot.missing),
        taste_hits=_taste_hits(cand, likes),
        distinct=len(cand.items),
        qty=cand.total_quantity,
        is_set=cand.is_set,
        single_item=cand.single_item,
        nutrition_total=tot,
        item_nutrition=item_nutrition,
        line_by_code=line_by_code,
        resolved=resolved,
        composition_used=composition_used,
        roles=list(cand.roles),
        dessert_kcal=dessert_kcal,
        dessert_sugar_share=dessert_share,
    )


def _dislike_violations(
    cand: Candidate, enricher: NutritionEnricher, dislikes: list[str]
) -> list[str]:
    """检查忌口是否被套餐**组成**违反（名称层已在候选生成阶段过滤）。

    仅使用**已获取**的套餐组成（不额外请求）；确认违反才返回。
    """
    kws = [str(d).strip() for d in dislikes if str(d).strip()]
    if not kws:
        return []
    out: list[str] = []
    for it in cand.items:
        comp = enricher.compositions.get(it.name)
        if not comp:
            continue
        for c in comp:
            for k in kws:
                if k in (c.name or ""):
                    out.append(f"{it.name} 的组成含「{c.name}」（命中忌口「{k}」）")
    return out


def _nutrition_tier_notes(feat: _Feat) -> list[str]:
    """把非精确的营养匹配依据放进"提示"，让结果可追溯。"""
    notes: list[str] = []
    for name, r in feat.resolved.items():
        if r.tier in ("normalized", "alias", "canonical", "composition"):
            notes.append(f"营养匹配【{r.tier}】{name} → {r.matched_name or name}：{r.evidence}")
    return notes


# ---------------------------------------------------------------------------
# 推荐理由
# ---------------------------------------------------------------------------


def _is_bare_single_staple(feat: _Feat) -> bool:
    """是否为「只有一件主食」的极简组合（既非套餐、也无饮料/小食/甜品搭配）。

    用于省钱型的**如实标注**：这种组合只是"最便宜的一餐骨架"，
    不能宣称是营养完整的一餐。套餐（``is_set``）本身即完整一餐，不算极简。
    """
    if feat.is_set:
        return False
    if feat.distinct != 1 or feat.qty != 1:
        return False
    return bool(feat.cand.has_staple)


def build_reasons(
    strategy: Strategy,
    feat: _Feat,
    ctx: _Ctx,
    *,
    rank_note: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> list[str]:
    """生成 1~3 条可追溯到真实数据的推荐理由。"""
    extra = extra or {}
    reasons: list[str] = []
    pay = cent_to_yuan(feat.payable_cent)
    orig = cent_to_yuan(feat.original_cent)
    disc = cent_to_yuan(feat.discount_cent)

    if strategy == "budget":
        if ctx.budget_cent:
            reasons.append(
                f"实付 ¥{pay:.2f}（原价 ¥{orig:.2f}，优惠 -¥{disc:.2f}），在预算 ¥{cent_to_yuan(ctx.budget_cent):.2f} 内"
                + (f"，{rank_note}" if rank_note else "")
            )
        else:
            reasons.append(f"实付 ¥{pay:.2f}（原价 ¥{orig:.2f}，优惠 -¥{disc:.2f}）" + (f"，{rank_note}" if rank_note else ""))
        if feat.discount_cent > 0 and feat.coupon is not None:
            reasons.append(f"使用优惠券「{feat.coupon.title}」实省 ¥{disc:.2f}")
        else:
            reasons.append("本组合未使用优惠券（门店券不适用于该组合或未命中）")
        # 单品兜底：明确标注为「经济单品方案」，不宣称是营养完整的一餐（Phase 4.3）
        if _is_bare_single_staple(feat):
            reasons.append(
                f"经济单品方案：仅 1 件主食（{feat.name_list()}），价格最低但**不代表营养完整的一餐**，"
                "如需配齐饮料 / 小食可提高预算或改看「综合推荐」。"
            )
        else:
            reasons.append(f"满足用餐需求：{feat.name_list()}，共 {feat.qty} 件（含主食）")

    elif strategy == "nutrition":
        density = protein_per_100kcal(feat.protein_g, feat.kcal)
        per_yuan = protein_per_yuan(feat.protein_g, feat.payable_cent)
        reasons.append(
            f"蛋白质合计 {feat.protein_g:.1f} g、热量 {feat.kcal:.0f} kcal"
            + (f"（{rank_note}）" if rank_note else "")
        )
        # 可解释的辅助比较指标：蛋白/热量（热量效率）与蛋白/价格（性价比）。
        # 未明确要求"蛋白最多"时，这两项是主排序依据；明确要求时仅作参考说明。
        if density is not None or per_yuan is not None:
            bits: list[str] = []
            if density is not None:
                bits.append(f"{density:.1f} g / 100 kcal")
            if per_yuan is not None:
                bits.append(f"{per_yuan:.2f} g / 元")
            lead = "排序依据" if not ctx.explicit_max_protein else "参考指标"
            reasons.append(f"蛋白质效率（{lead}）：{'｜'.join(bits)}")
        reasons.append(
            "营养数据完整：组合内每件商品均可靠匹配营养表，可参与定量排名"
        )
        reasons.append(f"实付 ¥{pay:.2f}" + (f"，在预算内" if ctx.budget_cent else ""))

    else:  # balanced
        comps = extra.get("components", {})
        detail = "｜".join(f"{k} {v:.2f}" for k, v in comps.items())
        reasons.append(f"综合评分 {extra.get('score', 0.0):.2f}：{detail}")
        if feat.complete and feat.protein_g is not None:
            reasons.append(f"营养：蛋白质 {feat.protein_g:.1f} g｜热量 {feat.kcal:.0f} kcal｜实付 ¥{pay:.2f}")
        else:
            reasons.append(f"实付 ¥{pay:.2f}；组合中存在营养未知商品，蛋白质等未参与评分（不臆造）")
        # 甜品 / 饮料 / 冰淇淋：说明"是否值得加入"的真实依据（不禁止，但可解释）
        treats = feat.treat_items()
        if treats:
            hit = [n for n in treats if any(str(k).strip() and str(k).strip() in n for k in ctx.likes)]
            if hit:
                reasons.append(f"含你点名的甜品/饮料（{'、'.join(hit)}），按真实营养数据计入搭配度")
            elif feat.dessert_kcal is not None:
                reasons.append(
                    f"含甜品/饮料（{'、'.join(treats)}）：共 {feat.dessert_kcal:.0f} kcal，"
                    f"占组合热量 {feat.dessert_sugar_share * 100:.0f}%，已按真实营养评估搭配度"
                )
            else:
                reasons.append(f"含甜品/饮料（{'、'.join(treats)}），但其营养数据未知，搭配度未参与评分")
        if feat.taste_hits:
            reasons.append(f"命中口味偏好：{'、'.join(feat.taste_hits)}")

    return reasons[:3]


def _notes_for(feat: _Feat) -> list[str]:
    """数据缺失 / 降级提示（如实标注，绝不臆造）。"""
    notes: list[str] = []
    if feat.missing:
        notes.append("营养未知（已尝试精确/归一化/别名/规范名/套餐组成匹配均失败，未计入合计）：" + "、".join(feat.missing))
    if feat.coupon is None:
        notes.append("未使用优惠券：门店券的适用商品未命中该组合。")
    if feat.single_item:
        if feat.is_set:
            notes.append("该方案为单件组合（套餐本身即完整一餐）。")
        else:
            notes.append(
                "该方案为经济单品方案（仅 1 件主食）：为满足预算的兜底选择，"
                "不构成营养完整的一餐，价格与营养仅覆盖该单品。"
            )
    notes.extend(_nutrition_tier_notes(feat))
    return notes


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _rank_lists(feats: list[_Feat], ctx: _Ctx) -> dict[str, list[_Feat]]:
    complete = [f for f in feats if f.complete]

    def _density_sort_key(f: _Feat) -> tuple[float, float, int]:
        """未明确要求最大蛋白时：先比蛋白质密度（蛋白/100kcal），再比总量，最后比实付。"""
        d = protein_per_100kcal(f.protein_g, f.kcal)
        return (-(d if d is not None else -1.0), -(f.protein_g or 0.0), f.payable_cent)

    nutrition_ranked = sorted(
        complete,
        key=(
            (lambda f: (-(f.protein_g or 0.0), f.payable_cent))
            if ctx.explicit_max_protein
            else _density_sort_key
        ),
    )
    return {
        # 省钱型：先保证"是一餐"（含主食且满足人数），再比实付
        "budget": sorted(
            feats,
            key=lambda f: (0 if (f.cand.has_staple and f.qty >= ctx.people) else 1, f.payable_cent, -f.discount_cent, -f.qty),
        ),
        "nutrition": nutrition_ranked,
        "balanced": sorted(
            feats,
            key=lambda f: (-score_balanced(f, ctx)[0], f.payable_cent),
        ),
    }


def _goals_eval(feat: _Feat, goals: Any) -> tuple[bool, set[str], str]:
    """判断组合是否满足用户的营养目标（硬约束），并给出**结构化**原因码。

    原因码（供无方案时归因与生成建议）：
    ``protein_low`` / ``kcal_high`` / ``fat_high`` /
    ``protein_unknown`` / ``kcal_unknown`` / ``fat_unknown``。

    对**营养未知**的组合：无法确认是否达标 → 视为不达标（不臆造、不假设达标、
    也**不按 0 计算**）。
    """
    if goals is None:
        return True, set(), ""
    codes: set[str] = set()
    notes: list[str] = []

    if getattr(goals, "protein_g_min", None) is not None:
        if not feat.complete or feat.protein_g is None:
            codes.add("protein_unknown")
            notes.append(f"营养未知，无法确认蛋白质 ≥ {goals.protein_g_min:g} g")
        elif feat.protein_g < goals.protein_g_min:
            codes.add("protein_low")
            notes.append(f"蛋白质 {feat.protein_g:.1f} g 未达下限 {goals.protein_g_min:g} g")

    if getattr(goals, "energy_kcal_max", None) is not None:
        if not feat.complete or feat.kcal is None:
            codes.add("kcal_unknown")
            notes.append(f"营养未知，无法确认热量 ≤ {goals.energy_kcal_max:g} kcal")
        elif feat.kcal > goals.energy_kcal_max:
            codes.add("kcal_high")
            notes.append(f"热量 {feat.kcal:.0f} kcal 超过上限 {goals.energy_kcal_max:g} kcal")

    if getattr(goals, "fat_g_max", None) is not None:
        tot = feat.nutrition_total.total if feat.nutrition_total is not None else None
        if not feat.complete or tot is None or tot.fat_g is None:
            codes.add("fat_unknown")
            notes.append("脂肪数据未知，无法确认脂肪上限")
        elif tot.fat_g > goals.fat_g_max:
            codes.add("fat_high")
            notes.append(f"脂肪 {tot.fat_g:.1f} g 超过上限 {goals.fat_g_max:g} g")

    if not codes:
        return True, set(), ""
    return False, codes, f"{feat.name_list()}：" + "；".join(notes)


def _goals_ok(feat: _Feat, goals: Any) -> tuple[bool, str]:
    """向后兼容的薄封装：``(是否达标, 原因说明)``。"""
    ok, _codes, why = _goals_eval(feat, goals)
    return ok, why


def recommend(
    request: UserRequest,
    *,
    client: Optional[McpClient] = None,
    strategies: Optional[Sequence[Strategy]] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> RecommendationPlan:
    """统一推荐入口，返回 :class:`~mcpilot.models.RecommendationPlan`。

    ``client`` 为空时使用 Python 直连 MCP 的默认客户端；也可注入由 Skill
    调度 MCP 得到的调用器（见 ``docs/02-architecture.md``）。

    ``on_progress``（可选）：真实进度回调。每个事件对应一次**真实**处理动作
    （真实 MCP 调用或真实的候选循环），界面据此如实反映状态；
    回调抛出的任何异常都会被忽略，**绝不影响推荐结果**。
    """

    def emit(
        stage: str,
        status: str,
        detail: str = "",
        index: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        if not on_progress:
            return
        event: dict[str, Any] = {"stage": stage, "status": status, "detail": detail}
        if index is not None:
            event["index"] = index
        if total is not None:
            event["total"] = total
        try:
            on_progress(event)
        except Exception:  # noqa: BLE001 — 进度观测不得影响业务
            pass

    t_start = time.perf_counter()
    client = client or McpClient()
    wanted: list[Strategy] = [s for s in (strategies or request.strategies) if s in _ALL_STRATEGIES]
    if not wanted:
        wanted = list(_ALL_STRATEGIES)

    menu, nutrition_index, coupons, queried_at, calls = _fetch(client, request, emit)
    if not queried_at:
        queried_at = _now_str()

    image_by_code: dict[str, str] = {it.code: it.image for it in menu.items if it.image}

    budget_cent = yuan_to_cent(request.budget) if request.budget is not None else None
    likes = request.effective_likes()
    dislikes = [str(d) for d in request.dislikes]

    # --- 营养解析器（静态分层 + 有界按需 detail）---------------------------
    calls["query-meal-detail"] = 0

    def _detail_fetcher(code: str) -> dict[str, Any]:
        payload = client.query_meal_detail(
            store_code=request.store_code or "",
            code=code,
            be_type=int(request.be_type),  # type: ignore[arg-type]
            be_code=request.be_code,
        )
        calls["query-meal-detail"] += 1
        emit(
            "resolve",
            "progress",
            f"解析规范名/套餐组成（{calls['query-meal-detail']}）",
            index=calls["query-meal-detail"],
            total=int(request.max_resolve),
        )
        return payload

    enricher = NutritionEnricher(
        nutrition_index,
        menu,
        _detail_fetcher,
        max_calls=int(request.max_resolve),
    )

    # 静态匹配下"可靠且有蛋白"的商品纳入候选池（营养值来自营养表，无额外调用）
    protein_items: list[tuple[float, str]] = []
    for it in menu.items:
        r = enricher.resolve(it.name, allow_fetch=False)
        if r.ok and r.nutrition is not None:
            protein_items.append((r.nutrition.protein_g or 0.0, it.code))
    protein_items.sort(key=lambda t: (-t[0], t[1]))
    top_protein_codes = [code for _, code in protein_items[:6]]

    # 券的适用商品若是本店在售商品，也要纳入候选池，否则"用券更省"的组合不会出现
    menu_codes = {it.code for it in menu.items}
    coupon_codes: list[str] = []
    for cp in coupons:
        for code in cp.product_codes:
            if code in menu_codes and code not in coupon_codes:
                coupon_codes.append(code)

    # 口味偏好命中的商品也必须纳入候选池——否则"只取最便宜的主餐"会把用户想要的
    # 商品（如「板烧鸡腿堡」）挡在候选之外，导致综合型无法体现口味偏好。
    taste_codes: list[str] = []
    for it in menu.items:
        if likes and any(str(k).strip() and str(k).strip() in it.name for k in likes):
            if it.code not in taste_codes:
                taste_codes.append(it.code)

    extra_codes = list(dict.fromkeys(top_protein_codes + coupon_codes + taste_codes))

    constraints = ComboConstraints(
        budget_cent=budget_cent,
        people=max(1, int(request.people)),
        dislikes=dislikes,
        include_codes=[str(c) for c in request.include_codes],
        extra_codes=extra_codes,
        require_staple=True,
    )
    candidates = generate_candidates(menu, constraints)

    warnings: list[str] = []
    suggestions: list[str] = []

    within = [c for c in candidates if budget_cent is None or c.est_subtotal_cent <= budget_cent]
    over = [c for c in candidates if budget_cent is not None and c.est_subtotal_cent > budget_cent]

    # --- 试算前的候选排序（仅用静态营养表，不触发 MCP）---------------------
    def protein_of(c: Candidate) -> float:
        p, _, _, _ = _estimate_combo_nutrition(c, enricher)
        return p if p is not None else -1.0

    pool = within or []
    by_est = sorted(pool, key=lambda c: c.est_subtotal_cent)
    by_protein = sorted([c for c in pool if protein_of(c) >= 0], key=lambda c: (-protein_of(c), c.est_subtotal_cent))
    by_taste = sorted(
        pool,
        key=lambda c: (
            -len(_taste_hits(c, likes)),
            c.est_subtotal_cent,
        ),
    )
    by_coupon = (
        sorted([c for c in pool if any(code in coupon_codes for code in c.codes)], key=lambda c: c.est_subtotal_cent)
        if coupon_codes
        else []
    )

    # 轮转选取，保证各策略相关候选都进入试算集合，且总次数受限
    verify_codes: list[str] = []
    seen_sig: set[str] = set()
    queues = [q for q in (by_est, by_protein, by_taste, by_coupon) if q]
    idx = 0
    while len(verify_codes) < max(0, int(request.max_verify)) and any(idx < len(q) for q in queues):
        for q in queues:
            if idx < len(q):
                c = q[idx]
                if c.signature not in seen_sig:
                    seen_sig.add(c.signature)
                    verify_codes.append(c.signature)
                    if len(verify_codes) >= max(0, int(request.max_verify)):
                        break
        idx += 1
    cand_by_sig = {c.signature: c for c in pool}
    verify_cands = [cand_by_sig[s] for s in verify_codes]

    calls["calculate-price"] = 0
    emit(
        "candidates",
        "done",
        f"生成候选 {len(candidates)} 个（预算内 {len(within)}），将真实试算 {len(verify_cands)} 个",
    )
    feats: list[_Feat] = []
    dropped_over = 0
    dropped_dislike = 0
    dislike_hits: list[str] = []  # 实际命中并导致剔除的忌口关键词（用于建议）
    failures: list[str] = []
    transport_failures = 0
    tier_counts: dict[str, int] = {}
    for cand in verify_cands:
        cm = _applicable_coupon(cand, coupons) if request.use_coupon else None
        try:
            quoted = _verify(client, request, cand, cm)
        except McpError as exc:  # 单个候选试算失败 → 记录并继续（不编造）
            failures.append(f"{cand.name_list()}：{exc}")
            if "网络" in str(exc) or "超时" in str(exc) or "HTTP" in str(exc):
                transport_failures += 1
            continue
        calls["calculate-price"] += 1
        emit(
            "verify",
            "progress",
            f"价格试算（{calls['calculate-price']}/{len(verify_cands)}）",
            index=calls["calculate-price"],
            total=len(verify_cands),
        )
        feat = _build_feat(cand, quoted, enricher, cm.coupon if cm else None, likes)

        # 忌口硬约束（含套餐组成）
        viol = _dislike_violations(cand, enricher, dislikes)
        if viol:
            dropped_dislike += 1
            for kw in dislikes:
                if kw and kw not in dislike_hits and any(kw in v for v in viol):
                    dislike_hits.append(kw)
            continue

        if budget_cent is not None and feat.payable_cent > budget_cent:
            dropped_over += 1
            continue
        for t in feat.resolved.values():
            tier_counts[t.tier] = tier_counts.get(t.tier, 0) + 1
        feats.append(feat)

    # --- 营养目标硬约束（若用户给出）：只保留可**确认**满足目标的组合 --------
    # 同时记录**结构化归因**与真实数值证据，供"无方案时的调整建议"使用。
    goals_dropped: list[str] = []
    dropped_kcal = 0
    dropped_protein = 0
    dropped_fat = 0
    dropped_unknown = 0
    # 仅因**单一**条件被剔除的候选——只有这些才能支撑"放宽这一项即可纳入"的建议
    kcal_fix: list[tuple[float, str]] = []  # (热量, 组合名)
    protein_fix: list[tuple[float, str]] = []  # (蛋白质, 组合名)
    if feats:
        kept: list[_Feat] = []
        for f in feats:
            ok, codes, why = _goals_eval(f, request.goals)
            if ok:
                kept.append(f)
                continue
            goals_dropped.append(why)
            if "kcal_high" in codes:
                dropped_kcal += 1
            if "protein_low" in codes:
                dropped_protein += 1
            if "fat_high" in codes:
                dropped_fat += 1
            if codes & {"protein_unknown", "kcal_unknown", "fat_unknown"}:
                dropped_unknown += 1
            if codes == {"kcal_high"} and f.kcal is not None:
                kcal_fix.append((float(f.kcal), f.name_list()))
            if codes == {"protein_low"} and f.protein_g is not None:
                protein_fix.append((float(f.protein_g), f.name_list()))
        feats = kept
        if goals_dropped and not feats:
            warnings.append(
                "没有候选能确认满足营养目标（已逐个核查，营养未知者无法确认达标，"
                "已剔除而非猜测）：" + "；".join(goals_dropped[:3])
            )

    # --- 预算不足：真实试算最低候选，作为建议的数据依据 ----------------------
    cheapest_over_cent: Optional[int] = None
    if not feats and budget_cent is not None and len(within) == 0:
        warnings.append(
            f"在预算 ¥{cent_to_yuan(budget_cent):.2f} 内没有找到可行组合"
            f"（共生成候选 {len(candidates)} 个，其中预算内 0 个，"
            f"真实试算 {calls['calculate-price']} 个）。"
        )
        cheapest_over_cent = _min_payable_report(client, request, over, coupons, calls)
        if cheapest_over_cent is None:
            warnings.append("未能试算出可用于比较的最低实付，因此不给出具体的预算数值建议。")
    elif failures and len(failures) == len(verify_cands):
        warnings.append("价格试算全部失败，未产生任何推荐（不返回未经 MCP 验证的价格）。")
        warnings.extend(failures[:3])

    if dropped_over and not feats:
        warnings.append(f"{dropped_over} 个候选因真实实付超出预算被剔除。")
    if dropped_dislike:
        warnings.append(f"{dropped_dislike} 个候选因套餐组成命中忌口被剔除（严格执行忌口约束）。")

    # --- 券的如实说明 ------------------------------------------------------
    if not coupons:
        warnings.append("当前门店/订单类型下无可用门店券（query-store-coupons 返回为空）。")
    else:
        in_menu = coupons_in_menu(coupons, menu_codes)
        actives = active_coupons(coupons)
        if not in_menu:
            warnings.append(
                "门店有券，但其适用商品不在本店在售菜单中，本次均无法使用（按真实编码比对判定，不套用）："
                + "、".join(f"「{c.title}」" for c in coupons)
            )
        if len(actives) < len(coupons):
            warnings.append(f"{len(coupons) - len(actives)} 张门店券已过期或尚未生效，已排除。")
        if request.use_coupon and all(_applicable_coupon(c, coupons) is None for c in verify_cands):
            warnings.append("门店券的适用商品未命中任何候选组合，本次均未使用优惠券。")
    warnings.append(f"券来源说明：{COUPON_SOURCES['store']}；{COUPON_SOURCES['account']}；{COUPON_SOURCES['claimable']}。")

    if not feats and budget_cent is None and not failures and not goals_dropped and not dropped_dislike:
        warnings.append("没有生成任何可行候选组合（可尝试放宽口味 / 忌口 / 指定商品限制）。")

    # --- 无可行方案时的「智能调整建议」（全部基于本次真实候选/试算）---------
    # 严格约束：不擅自放宽用户条件、不自动重新查询；建议只是"待用户确认"的候选变更。
    adjustments: list[Adjustment] = []
    block_summary: dict[str, int] = {}
    if not feats:
        info = BlockInfo(
            menu_size=len(menu),
            candidates=len(candidates),
            within_budget=len(within),
            verified=calls["calculate-price"],
            dropped_over_budget=dropped_over,
            dropped_by_dislike=dropped_dislike,
            dropped_by_kcal=dropped_kcal,
            dropped_by_protein=dropped_protein,
            dropped_by_fat=dropped_fat,
            dropped_nutrition_unknown=dropped_unknown,
            verify_failed=len(failures),
            cheapest_over_budget_cent=cheapest_over_cent,
            kcal_fix_kcal=(min(kcal_fix)[0] if kcal_fix else None),
            kcal_fix_combo=(min(kcal_fix)[1] if kcal_fix else ""),
            kcal_fix_count=len(kcal_fix),
            protein_fix_g=(max(protein_fix)[0] if protein_fix else None),
            protein_fix_combo=(max(protein_fix)[1] if protein_fix else ""),
            protein_fix_count=len(protein_fix),
            budget_cent=budget_cent,
            kcal_max=getattr(request.goals, "energy_kcal_max", None),
            protein_min=getattr(request.goals, "protein_g_min", None),
            fat_max=getattr(request.goals, "fat_g_max", None),
            dislikes=list(dislikes),
            dislike_hits=list(dislike_hits),
            include_codes=[str(c) for c in request.include_codes],
        )
        summary = summary_warning(info)
        if summary:
            warnings.append(summary)
        adjustments = build_adjustments(info)
        suggestions = [a.text for a in adjustments]
        block_summary = blocked_summary(info)

    # --- 打分与选优（保证三种策略尽量呈现不同方案）--------------------------
    recommendations: list[Recommendation] = []
    if feats:
        ratios = [
            (f.protein_g / (f.payable_cent / 100.0))
            for f in feats
            if f.complete and f.protein_g and f.payable_cent > 0
        ]
        densities = [
            d
            for d in (protein_per_100kcal(f.protein_g, f.kcal) for f in feats if f.complete)
            if d is not None
        ]
        ctx = _Ctx(
            budget_cent=budget_cent,
            max_payable_cent=max(f.payable_cent for f in feats),
            max_protein_g=max((f.protein_g or 0.0) for f in feats),
            max_kcal=max((f.kcal or 0.0) for f in feats),
            max_protein_per_yuan=max(ratios) if ratios else 0.0,
            max_protein_per_100kcal=max(densities) if densities else 0.0,
            likes=likes,
            people=max(1, int(request.people)),
            kcal_target=request.goals.energy_kcal_target,
            explicit_max_protein=wants_max_protein(likes, request.goals),
        )
        ranks = _rank_lists(feats, ctx)
        used: set[str] = set()
        diff_notes: dict[str, str] = {}
        for s in wanted:
            ordered = ranks[s]
            pick: Optional[_Feat] = None
            skipped = 0
            for f in ordered:
                if f.cand.signature not in used:
                    pick = f
                    break
                skipped += 1
            if pick is None and ordered:
                # 没有不重复的方案 → 允许复用，但**明确记录**"不同策略给的是同一真实组合"
                pick = ordered[0]
                diff_notes[s] = (
                    "三策略去重说明：本次真实可行组合不足，本方案与其它策略指向同一组合；"
                    "已按各策略口径分别给出评分与理由（未编造商品凑数）。"
                )
            elif pick is not None and skipped:
                diff_notes[s] = (
                    f"三策略去重说明：为与其它策略呈现不同方案，此处给出同样真实、"
                    f"按本策略口径评分次优的组合（跳过 {skipped} 个已被其它策略选用的组合）。"
                )
            if pick is None:
                continue
            used.add(pick.cand.signature)
            recommendations.append(
                _make_recommendation(
                    s, pick, ctx, ranks, queried_at, diff_notes.get(s, ""), image_by_code
                )
            )
        if len(recommendations) < len(wanted):
            missing = [STRATEGY_LABELS[s] for s in wanted if s not in {r.strategy for r in recommendations}]
            if missing:
                warnings.append("以下策略无符合条件的方案（未凑数编造）：" + "、".join(missing))

    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    emit("done", "done", f"完成（{len(recommendations)} 个方案，耗时 {elapsed_ms} ms）")
    stats = {
        "menu_size": len(menu),
        "candidates": len(candidates),
        "candidates_within_budget": len(within),
        "verified": calls["calculate-price"],
        "dropped_over_budget": dropped_over,
        "dropped_by_goals": len(goals_dropped),
        "dropped_by_dislike": dropped_dislike,
        "verify_failed": len(failures),
        "transport_failures": transport_failures,
        "detail_resolved": calls["query-meal-detail"],
        "nutrition_tiers": tier_counts,
        "elapsed_ms": elapsed_ms,
        "mcp_calls": calls,
        # 无方案时的**结构化**阻断归因（键与 advice.blocked_summary 一致）
        "blocked": block_summary,
        "adjustments": len(adjustments),
    }

    return RecommendationPlan(
        store_code=request.store_code or "",
        be_type=int(request.be_type),
        be_code=request.be_code,
        request=request,
        recommendations=recommendations,
        warnings=warnings,
        suggestions=suggestions,
        adjustments=adjustments,
        stats=stats,
        data_source=DATA_SOURCE,
        queried_at=queried_at,
    )


def _min_payable_report(
    client: McpClient,
    request: UserRequest,
    over: list[Candidate],
    coupons: list[Coupon],
    calls: dict[str, int],
) -> Optional[int]:
    """预算不足时，真实试算最便宜的 1~3 个候选，报告真实最低实付（分）。"""
    if not over:
        return None
    for cand in sorted(over, key=lambda c: c.est_subtotal_cent)[:3]:
        cm = _applicable_coupon(cand, coupons) if request.use_coupon else None
        try:
            q = _verify(client, request, cand, cm)
        except McpError:
            continue
        calls["calculate-price"] += 1
        return int(q.payable_cent)
    return None


def _make_recommendation(
    strategy: Strategy,
    feat: _Feat,
    ctx: _Ctx,
    ranks: dict[str, list[_Feat]],
    queried_at: str,
    reuse_note: str,
    image_by_code: Optional[dict[str, str]] = None,
) -> Recommendation:
    image_by_code = image_by_code or {}
    label = STRATEGY_LABELS[strategy]
    extra: dict[str, Any] = {}
    if strategy == "budget":
        rank_note = "为候选方案中实付最低"
        score = score_budget(feat, ctx)
    elif strategy == "nutrition":
        is_top = bool(ranks["nutrition"]) and ranks["nutrition"][0].cand.signature == feat.cand.signature
        if ctx.explicit_max_protein:
            rank_note = "为可可靠匹配营养的候选中蛋白质最高" if is_top else "蛋白质较高"
        else:
            rank_note = "为可可靠匹配营养的候选中蛋白质效率最高（蛋白/热量）" if is_top else "蛋白质效率较高"
        s = score_nutrition(feat, ctx)
        score = 0.0 if s is None else s
    else:
        score, comps = score_balanced(feat, ctx)
        extra["components"] = comps
        extra["score"] = score
        rank_note = ""

    reasons = build_reasons(strategy, feat, ctx, rank_note=rank_note, extra=extra)
    if reuse_note:
        reasons.append(reuse_note)

    role_by_code = {i.code: r for i, r in zip(feat.cand.items, feat.cand.roles)}
    items: list[RecommendedItem] = []
    for it in feat.cand.items:
        orig_cent, pay_cent = feat.line_by_code.get(it.code, (0, 0))
        items.append(
            RecommendedItem(
                code=it.code,
                name=it.name,
                quantity=it.quantity,
                line_original_cent=orig_cent,
                line_payable_cent=pay_cent,
                nutrition=feat.item_nutrition.get(it.code),
                role=role_by_code.get(it.code, ""),
                image=image_by_code.get(it.code, ""),
            )
        )

    return Recommendation(
        strategy=strategy,
        label=label,
        items=items,
        original_cent=feat.original_cent,
        discount_cent=feat.discount_cent,
        payable_cent=feat.payable_cent,
        # 营养合计：仅当**全部商品可靠匹配**时才给出（否则只报缺失，不用部分合计冒充整体）
        nutrition=(feat.nutrition_total.total if (feat.complete and feat.nutrition_total) else None),
        nutrition_missing=list(feat.missing),
        nutrition_complete=feat.complete,
        coupon=feat.coupon,
        coupon_note=("使用优惠券「%s」" % feat.coupon.title) if feat.coupon else "未使用优惠券",
        score=score,
        reasons=reasons,
        notes=_notes_for(feat),
        data_source=DATA_SOURCE,
        queried_at=queried_at,
        est_subtotal_cent=feat.est_subtotal_cent,
    )


def _now_str() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="McPilot 推荐（真实 MCP）")
    p.add_argument("--store", required=True, help="门店编码 storeCode")
    p.add_argument("--be-type", type=int, default=1, help="1=到店自取, 5=得来速")
    p.add_argument("--be-code", default=None, help="得来速场景必填")
    p.add_argument("--budget", type=float, default=None, help="预算上限（元）")
    p.add_argument("--people", type=int, default=1, help="用餐人数")
    p.add_argument("--like", action="append", default=[], help="口味偏好关键词，可重复")
    p.add_argument("--dislike", action="append", default=[], help="忌口关键词，可重复")
    p.add_argument("--include", action="append", default=[], help="必须包含的商品编码，可重复")
    p.add_argument("--strategy", action="append", choices=list(_ALL_STRATEGIES), default=None)
    p.add_argument("--max-verify", type=int, default=12, help="calculate-price 试算次数上限")
    p.add_argument("--max-resolve", type=int, default=10, help="query-meal-detail 解析次数上限")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    request = UserRequest(
        store_code=args.store,
        be_type=args.be_type,
        be_code=args.be_code,
        budget=args.budget,
        people=args.people,
        likes=list(args.like),
        dislikes=list(args.dislike),
        include_codes=list(args.include),
        max_verify=args.max_verify,
        max_resolve=args.max_resolve,
        strategies=list(args.strategy) if args.strategy else ["budget", "nutrition", "balanced"],
    )
    plan = recommend(request, client=McpClient())
    if args.json:
        print(plan.to_json(cent_to_yuan=cent_to_yuan))
        return 0

    print(f"门店 {plan.store_code}（beType={plan.be_type}）｜查询时间 {plan.queried_at}")
    st = plan.stats
    print(
        f"候选 {st['candidates']} 个（预算内 {st['candidates_within_budget']}）"
        f"｜试算 {st['verified']} 次｜详情解析 {st['detail_resolved']} 次"
        f"｜耗时 {st['elapsed_ms']} ms｜MCP 调用 {st['mcp_calls']}"
    )
    if not plan.recommendations:
        print("未找到符合条件的推荐方案。")
    for r in plan.recommendations:
        print(f"\n【{r.label}】评分 {r.score:.3f}")
        print("  组合：" + " + ".join(f"{i.name}×{i.quantity}" for i in r.items))
        print(
            f"  原价 ¥{cent_to_yuan(r.original_cent):.2f}｜优惠 -¥{cent_to_yuan(r.discount_cent):.2f}"
            f"｜实付 ¥{cent_to_yuan(r.payable_cent):.2f}"
        )
        if r.nutrition_complete and r.nutrition:
            print(f"  营养：{r.nutrition.energy_kcal} kcal｜蛋白 {r.nutrition.protein_g} g")
        elif r.nutrition_missing:
            print("  营养：部分商品未知 → " + "、".join(r.nutrition_missing))
        print("  券：" + r.coupon_note)
        for reason in r.reasons:
            print(f"  理由：{reason}")
        for note in r.notes:
            print(f"  提示：{note}")
    for w in plan.warnings:
        print(f"\n⚠ {w}")
    for s in plan.suggestions:
        print(f"建议：{s}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
