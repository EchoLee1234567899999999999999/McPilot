"""推荐质量 · 高蛋白口径与热量效率（Phase 4.3，离线）。

覆盖需求 1~4 的**引擎侧**断言：

1. 高蛋白策略：未明确要求"蛋白最多"时，用**蛋白质密度（蛋白/100kcal）**作为主排序口径；
   明确要求时才切回**绝对总量**优先；硬约束（蛋白下限 / 热量上限 / 忌口）在两口径下都保留。
2. 甜品 / 饮料 / 冰淇淋**不被禁止**：由真实营养数据 + 用户偏好 + 评分规则决定是否加入，
   并在推荐理由中给出可解释说明。
3. 省钱型若只给一件主食，必须标注为「经济单品方案」，且**不得**宣称营养完整的一餐。
4. 三策略去重说明措辞一致、可解释。

所有断言只依赖**真实采集夹具**与**公开函数**，不硬编码易变业务数值。
"""

from __future__ import annotations

import pytest

from mcpilot.combos import Candidate
from mcpilot.mcp_client import McpClient
from mcpilot.models import ComboItem, Nutrition, UserRequest
from mcpilot.recommender import (
    _Ctx,
    _Feat,
    _is_bare_single_staple,
    _treat_fit,
    protein_per_100kcal,
    protein_per_yuan,
    recommend,
    score_nutrition,
    wants_max_protein,
)
from tests.conftest import RecommenderInvoker


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


def _make_feat(*, protein_g, kcal, payable_cent=3000, complete=True, roles=None, items=None,
               dessert_kcal=None, dessert_share=0.0, taste_hits=None):
    items = items or [ComboItem("x", "测试品", 1)]
    roles = roles or ["main"] * len(items)
    return _Feat(
        cand=Candidate(items=items, roles=roles, est_subtotal_cent=payable_cent),
        payable_cent=payable_cent,
        original_cent=payable_cent,
        discount_cent=0,
        est_subtotal_cent=payable_cent,
        coupon=None,
        protein_g=protein_g,
        kcal=kcal,
        complete=complete,
        missing=[] if complete else ["未知品"],
        taste_hits=list(taste_hits or []),
        distinct=len(items),
        qty=sum(i.quantity for i in items),
        is_set=False,
        single_item=len(items) == 1,
        roles=roles,
        dessert_kcal=dessert_kcal,
        dessert_sugar_share=dessert_share,
    )


# --- 1. 单位换算辅助函数：可解释、未知不猜测 ---------------------------------


def test_protein_per_100kcal_and_per_yuan_helpers():
    assert protein_per_100kcal(30.0, 600.0) == pytest.approx(5.0)
    assert protein_per_yuan(30.0, 3000) == pytest.approx(1.0)
    # 未知 / 非法输入一律 None（绝不当 0）
    assert protein_per_100kcal(None, 600.0) is None
    assert protein_per_100kcal(30.0, 0) is None
    assert protein_per_100kcal(30.0, None) is None
    assert protein_per_yuan(None, 3000) is None
    assert protein_per_yuan(30.0, 0) is None


# --- 2. 「是否明确要求最大化绝对蛋白质」判定 --------------------------------


@pytest.mark.parametrize("likes", ["蛋白最多", "蛋白质最高", "把蛋白拉满", "max protein"])
def test_wants_max_protein_by_keyword(likes):
    assert wants_max_protein([likes]) is True


def test_wants_max_protein_by_high_protein_goal():
    from mcpilot.models import NutritionGoals

    assert wants_max_protein([], NutritionGoals(protein_g_min=35)) is True
    assert wants_max_protein([], NutritionGoals(protein_g_min=20)) is False
    assert wants_max_protein([], None) is False


def test_wants_max_protein_default_is_false():
    assert wants_max_protein([]) is False
    assert wants_max_protein(["鸡腿堡", "咖啡"]) is False


# --- 3. 高蛋白打分：两种口径 ------------------------------------------------


def _ctx(explicit: bool) -> _Ctx:
    return _Ctx(
        budget_cent=5000,
        max_payable_cent=5000,
        max_protein_g=40.0,
        max_kcal=1000.0,
        max_protein_per_yuan=1.5,
        max_protein_per_100kcal=5.0,
        explicit_max_protein=explicit,
    )


def test_score_nutrition_density_favors_leaner_on_default():
    """默认口径：蛋白相同但热量更低（密度更高）的组合得分更高。"""
    ctx = _ctx(explicit=False)
    lean = _make_feat(protein_g=30.0, kcal=500.0)  # 密度 6.0
    fatty = _make_feat(protein_g=30.0, kcal=900.0)  # 密度 3.33
    assert score_nutrition(lean, ctx) > score_nutrition(fatty, ctx)


def test_score_nutrition_total_wins_when_explicit_protein():
    """明确要求口径：热量更高但**蛋白总量更高**的组合胜出。"""
    ctx = _ctx(explicit=True)
    high_total = _make_feat(protein_g=40.0, kcal=1000.0)
    low_total = _make_feat(protein_g=30.0, kcal=500.0)
    assert score_nutrition(high_total, ctx) > score_nutrition(low_total, ctx)


def test_score_nutrition_returns_none_when_incomplete():
    """营养未知一律不参与定量排名（返回 None，而非 0）。"""
    ctx = _ctx(explicit=False)
    unknown = _make_feat(protein_g=None, kcal=None, complete=False)
    assert score_nutrition(unknown, ctx) is None


# --- 4. 端到端：默认口径下高蛋白方案密度不低于纯总量方案 --------------------


def _density(r):
    n = r.nutrition
    if not n or not n.protein_g or not n.energy_kcal:
        return None
    return n.protein_g / (n.energy_kcal / 100.0)


def test_nutrition_plan_is_density_optimal_offline():
    """离线夹具：默认（未点名）时高蛋白方案应达到候选中的最高蛋白质密度。"""
    plan, _ = _plan(budget=50)
    nut = next((r for r in plan.recommendations if r.strategy == "nutrition"), None)
    assert nut is not None and nut.nutrition_complete
    d_nut = _density(nut)
    others = [_density(r) for r in plan.recommendations if r.strategy != "nutrition" and _density(r)]
    if others:
        assert d_nut >= max(others) - 1e-9, "默认口径下高蛋白方案应有最高（或并列最高）蛋白质密度"


def test_explicit_protein_changes_selection_to_higher_total():
    """点名「蛋白最多」时，高蛋白方案的蛋白质总量应 ≥ 默认口径下的方案。"""
    plan_default, _ = _plan(budget=50)
    plan_max, _ = _plan(budget=50, likes=["蛋白最多"])

    def _prot(p):
        r = next((x for x in p.recommendations if x.strategy == "nutrition"), None)
        return (r.nutrition.protein_g if (r and r.nutrition) else None), r

    p_def, r_def = _prot(plan_default)
    p_max, r_max = _prot(plan_max)
    if p_def is None or p_max is None:
        pytest.skip("离线夹具未产出高蛋白方案")
    assert p_max >= p_def, "明确要求最大化蛋白时，蛋白总量不应低于默认口径"
    assert "蛋白质效率" in " ".join(r_max.reasons), "理由中应给出可解释的效率指标"
    assert "排序依据" in " ".join(r_def.reasons), "默认口径应说明效率指标是排序依据"


def test_hard_constraints_survive_both_protein_policies():
    """两种口径下，蛋白下限与热量上限都必须仍是硬约束。"""
    from mcpilot.models import NutritionGoals

    for likes in ([], ["蛋白最多"]):
        plan, _ = _plan(budget=60, goals=NutritionGoals(protein_g_min=25, energy_kcal_max=900), likes=likes)
        for r in plan.recommendations:
            assert r.nutrition_complete
            assert r.nutrition.protein_g >= 25
            assert r.nutrition.energy_kcal <= 900


# --- 5. 甜品 / 饮料 / 冰淇淋：不禁止，但要能解释 ----------------------------


def test_treat_fit_scores_named_preference_full():
    """用户点名的甜品/饮料 → 搭配度满分（尊重偏好）。"""
    feat = _make_feat(
        protein_g=30.0,
        kcal=800.0,
        items=[ComboItem("a", "巨无霸", 1), ComboItem("b", "圆筒冰淇淋", 1)],
        roles=["main", "dessert"],
        dessert_kcal=300.0,
        dessert_share=0.375,
    )
    assert _treat_fit(feat, ["圆筒"]) == 1.0


def test_treat_fit_penalizes_dessert_dominant_combo():
    """甜品热量占比过高（几乎把一餐变成甜品餐）→ 搭配度明显下降。"""
    modest = _make_feat(
        protein_g=30.0, kcal=900.0,
        items=[ComboItem("a", "汉堡", 1), ComboItem("b", "冰淇淋", 1)],
        roles=["main", "dessert"], dessert_kcal=180.0, dessert_share=0.20,
    )
    dominant = _make_feat(
        protein_g=10.0, kcal=600.0,
        items=[ComboItem("a", "汉堡", 1), ComboItem("b", "冰淇淋", 1)],
        roles=["main", "dessert"], dessert_kcal=420.0, dessert_share=0.70,
    )
    assert _treat_fit(modest, []) == 1.0
    assert _treat_fit(dominant, []) < 0.6
    assert _treat_fit(dominant, []) > 0.0  # 仍允许存在，不"一刀切禁止"


def test_treat_fit_absent_when_no_dessert():
    """组合没有甜品/饮料 → 该维度不参与（None），而不是给 0。"""
    feat = _make_feat(protein_g=30.0, kcal=600.0, items=[ComboItem("a", "汉堡", 1)], roles=["main"])
    assert _treat_fit(feat, []) is None


def test_balanced_includes_treat_dimension_only_when_present():
    """综合型：有甜品时出现「甜点搭配度」维度；无甜品时不出现。"""
    from mcpilot.recommender import score_balanced

    ctx = _Ctx(
        budget_cent=5000, max_payable_cent=5000, max_protein_g=40.0, max_kcal=1000.0,
        max_protein_per_yuan=1.5, max_protein_per_100kcal=5.0,
    )
    with_treat = _make_feat(
        protein_g=30.0, kcal=800.0,
        items=[ComboItem("a", "汉堡", 1), ComboItem("b", "冰淇淋", 1)],
        roles=["main", "dessert"], dessert_kcal=200.0, dessert_share=0.25,
    )
    without = _make_feat(protein_g=30.0, kcal=600.0)
    _, comps_with = score_balanced(with_treat, ctx)
    _, comps_without = score_balanced(without, ctx)
    assert "甜点搭配度" in comps_with
    assert "甜点搭配度" not in comps_without


def test_balanced_reason_explains_dessert_with_real_nutrition():
    """综合型命中甜品时，理由需给出**真实营养依据**（热量与占比），而非主观评价。"""
    plan, _ = _plan(budget=50)
    bal = next(r for r in plan.recommendations if r.strategy == "balanced")
    treats = [i for i in bal.items if i.role in ("dessert", "drink")]
    if not treats:
        pytest.skip("本次综合型方案不含甜品/饮料")
    joined = " ".join(bal.reasons)
    assert ("甜品" in joined or "饮料" in joined or "冰淇淋" in joined)
    assert "kcal" in joined and "%" in joined


def test_dessert_not_banned_from_candidates():
    """甜品/饮料可进入候选并被推荐（不被无条件禁止）。"""
    plan, _ = _plan(budget=50)
    roles = {i.role for r in plan.recommendations for i in r.items}
    assert roles & {"dessert", "drink"}, "甜品/饮料应能出现在方案中（由数据与规则决定）"


# --- 6. 省钱型经济单品方案：如实标注 ---------------------------------------


def test_is_bare_single_staple_helper():
    bare = _make_feat(protein_g=None, kcal=None, items=[ComboItem("a", "随心配", 1)], roles=["set"])
    bare.is_set = False
    assert _is_bare_single_staple(bare) is True
    # 套餐（is_set）本身即完整一餐，不算极简
    full_set = _make_feat(protein_g=None, kcal=None, items=[ComboItem("a", "四件套", 1)], roles=["set"])
    full_set.is_set = True
    assert _is_bare_single_staple(full_set) is False
    # 主餐 + 配餐 → 不是单品
    combo = _make_feat(
        protein_g=None, kcal=None,
        items=[ComboItem("a", "汉堡", 1), ComboItem("b", "可乐", 1)], roles=["main", "drink"],
    )
    assert _is_bare_single_staple(combo) is False


def test_budget_single_item_plan_is_labeled_economy():
    """省钱型若只有一件主食，理由必须标注「经济单品方案」且**不**宣称营养完整。"""
    plan, _ = _plan(budget=50)
    bud = next((r for r in plan.recommendations if r.strategy == "budget"), None)
    if bud is None:
        pytest.skip("无省钱方案")
    is_bare = len(bud.items) == 1 and not bud.items[0].role == "set" and any(
        i.role in ("main", "set") for i in bud.items
    )
    joined = " ".join(bud.reasons)
    if is_bare:
        assert "经济单品方案" in joined
        assert "不代表营养完整的一餐" in joined
        assert "满足用餐需求" not in joined
    else:
        # 含配餐 / 套餐 → 可以声称是一餐
        assert "满足用餐需求" in joined or "套餐" in joined


def test_budget_single_item_note_is_honest():
    plan, _ = _plan(budget=50)
    bud = next((r for r in plan.recommendations if r.strategy == "budget"), None)
    if bud is None:
        pytest.skip("无省钱方案")
    if len(bud.items) == 1 and bud.items[0].role != "set":
        assert any("经济单品方案" in n or "完整的一餐" in n for n in bud.notes)


# --- 7. 三策略去重说明一致 -------------------------------------------------


def test_dedup_notes_are_consistent_when_repeated():
    """若两策略指向同一组合，必须给出「三策略去重说明」措辞一致的提示。"""
    plan, _ = _plan(budget=3)
    sigs = ["|".join(sorted(f"{i.code}x{i.quantity}" for i in r.items)) for r in plan.recommendations]
    if len(sigs) == len(set(sigs)):
        pytest.skip("本次三策略方案互不相同，无需去重说明")
    assert any("去重说明" in x for r in plan.recommendations for x in r.reasons)


def test_strategies_are_distinct_or_explained():
    plan, _ = _plan(budget=50)
    sigs = ["|".join(sorted(f"{i.code}x{i.quantity}" for i in r.items)) for r in plan.recommendations]
    assert len(sigs) == len(set(sigs)) or any(
        "去重说明" in x for r in plan.recommendations for x in r.reasons
    )
