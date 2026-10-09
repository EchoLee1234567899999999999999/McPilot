"""推荐质量优化验证（Phase 3，离线）。

覆盖需求三：省钱型必须"是一餐"、高蛋白型预算内最大化、综合型与高蛋白型去重、
硬约束（忌辣 / 指定商品 / 热量上限）严格执行、以及"不满足需求不得作为最优输出"。
"""

from __future__ import annotations

import pytest

from mcpilot.combos import Candidate
from mcpilot.mcp_client import McpClient
from mcpilot.models import ComboItem, NutritionGoals, UserRequest
from mcpilot.nutrition import build_index
from mcpilot.recommender import _dislike_violations, recommend
from mcpilot.resolve import NutritionEnricher
from tests.conftest import RecommenderInvoker, load_fixture

STAPLE_ROLES = {"main", "set"}


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


# --- 省钱型：必须含主食、不是"全菜单最便宜的单品"--------------------------


@pytest.mark.parametrize("budget", [20, 30, 50])
def test_budget_pick_is_a_meal_with_staple(budget):
    plan, _ = _plan(budget=budget)
    bud = next(r for r in plan.recommendations if r.strategy == "budget")
    assert any(i.role in STAPLE_ROLES for i in bud.items), "省钱型方案必须含主食（主餐或套餐）"
    assert sum(i.quantity for i in bud.items) >= 1


def test_budget_does_not_pick_a_bare_drink_or_side():
    """即使饮品更便宜，省钱型也不得把"一杯饮料"当作一餐。"""
    plan, _ = _plan(budget=30)
    bud = next(r for r in plan.recommendations if r.strategy == "budget")
    roles = {i.role for i in bud.items}
    assert roles & STAPLE_ROLES
    # 若主餐/套餐存在，则方案里必须出现
    assert any(i.role in STAPLE_ROLES for i in bud.items)


def test_budget_respects_people():
    plan, _ = _plan(budget=60, people=2)
    for r in plan.recommendations:
        assert sum(i.quantity for i in r.items) >= 2


# --- 高蛋白型：预算内最大化、仅可靠营养参与 -------------------------------


def test_nutrition_is_top_protein_among_complete():
    plan, _ = _plan(budget=50)
    nut = next(r for r in plan.recommendations if r.strategy == "nutrition")
    assert nut.nutrition_complete
    others = [r.nutrition.protein_g for r in plan.recommendations if r.nutrition_complete and r.nutrition]
    assert nut.nutrition.protein_g >= max(others)


def test_nutrition_never_ranks_incomplete():
    plan, _ = _plan(budget=50)
    for r in plan.recommendations:
        if r.strategy == "nutrition":
            assert r.nutrition_complete


# --- 综合型与高蛋白型去重 -------------------------------------------------


def test_balanced_differs_from_nutrition_when_possible():
    plan, _ = _plan(budget=30)
    nut = next((r for r in plan.recommendations if r.strategy == "nutrition"), None)
    bal = next((r for r in plan.recommendations if r.strategy == "balanced"), None)
    if nut is None or bal is None:
        pytest.skip("真实可行组合不足，跳过去重断言")
    sig = lambda r: "|".join(sorted(f"{i.code}x{i.quantity}" for i in r.items))  # noqa: E731
    assert sig(nut) != sig(bal) or any("重复" in x for x in bal.reasons)


def test_balanced_reasons_are_explainable_and_not_subjective():
    plan, _ = _plan(budget=30)
    bal = next(r for r in plan.recommendations if r.strategy == "balanced")
    joined = " ".join(bal.reasons)
    assert "综合评分" in joined
    for forbidden in ("饱腹", "口感评分", "主观"):
        assert forbidden not in joined


# --- 硬约束严格执行 -------------------------------------------------------


def test_dislike_is_hard_constraint_in_names():
    plan, _ = _plan(budget=40, dislikes=["辣"])
    assert plan.recommendations
    for r in plan.recommendations:
        assert all("辣" not in i.name for i in r.items)


def test_dislike_violation_detected_in_set_composition():
    """套餐组成里含有忌口商品时，必须能被检出（即便套餐名不含"辣"）。"""
    idx = build_index(load_fixture("nutrition.json"))
    invoker = RecommenderInvoker(
        detail_fixtures={"9900000888": "9900000888.json"},
    )
    from mcpilot.menu import parse_menu

    menu = parse_menu(invoker.menu_payload)
    enr = NutritionEnricher(idx, menu, lambda code: load_fixture(f"detail/{invoker._detail_fixtures[code]}"))
    # 人为把组成改成含"辣"的项，验证检出逻辑
    enr.compositions["巨无霸四件套"] = enr.compositions.get("巨无霸四件套") or []
    from mcpilot.models import SetComponent

    enr.compositions["巨无霸四件套"] = [SetComponent(name="麦辣鸡腿汉堡", code="1440", quantity=1)]
    cand = Candidate(items=[ComboItem(code="9900000888", name="巨无霸四件套", quantity=1)], roles=["set"])
    viol = _dislike_violations(cand, enr, ["辣"])
    assert viol and "辣" in viol[0]


def test_include_codes_is_hard_constraint():
    plan, _ = _plan(budget=35, include_codes=["1100"])
    assert plan.recommendations
    for r in plan.recommendations:
        assert "1100" in [i.code for i in r.items]


def test_energy_ceiling_is_hard_constraint():
    plan, _ = _plan(budget=50, goals=NutritionGoals(energy_kcal_max=450))
    for r in plan.recommendations:
        assert r.nutrition_complete and r.nutrition.energy_kcal <= 450


def test_unsatisfiable_goal_yields_no_best_claim():
    """不满足用户需求时不得以"最优推荐"名义输出：应给出空方案 + 明确告警。"""
    plan, _ = _plan(budget=50, goals=NutritionGoals(protein_g_min=999))
    assert plan.recommendations == []
    assert any("营养目标" in w for w in plan.warnings)


# --- 口味偏好：偏好的商品必须能进入候选池 ---------------------------------


def test_liked_item_enters_candidate_pool_and_balanced_surfaces_it():
    """用户点名"想吃板烧"时，偏好商品不得被"只取最便宜的主餐"挡在候选之外。"""
    plan, _ = _plan(budget=60, people=2, likes=["板烧"])
    bal = next(r for r in plan.recommendations if r.strategy == "balanced")
    # 综合型应命中口味（板烧），并在理由中给出可解释说明
    hit = [i for i in bal.items if "板烧" in i.name]
    assert hit, "综合型应能在候选池中找到并推荐用户偏好的商品"
    assert any("口味" in x for x in bal.reasons)


# --- 套餐组成参与推荐（真实 detail 结构）----------------------------------


def test_set_nutrition_via_composition_in_recommendation():
    """含套餐的方案，其营养可按 detail 默认组成算出（1119 kcal）。"""
    invoker = RecommenderInvoker(detail_fixtures={"9900000888": "9900000888.json"})
    plan, _ = _plan(budget=40, include_codes=["9900000888"], invoker=invoker)
    assert plan.recommendations
    complete = [r for r in plan.recommendations if r.nutrition_complete and r.nutrition]
    assert complete, "含该套餐的方案应能给出可靠营养（来自默认组成）"
    assert any((r.nutrition.energy_kcal or 0) >= 1119 for r in complete)
    # 依据可追溯
    assert any("composition" in n for r in plan.recommendations for n in r.notes)
