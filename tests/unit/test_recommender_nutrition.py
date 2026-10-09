"""推荐引擎 · 高蛋白型（Phase 2，离线）。"""

from __future__ import annotations

import pytest

from mcpilot.mcp_client import McpClient
from mcpilot.models import NutritionGoals, UserRequest
from mcpilot.nutrition import build_index
from mcpilot.recommender import recommend
from tests.conftest import RecommenderInvoker, load_fixture


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


def test_nutrition_plan_is_always_nutrition_complete():
    """高蛋白排名只接受营养可可靠匹配的组合。"""
    plan, _ = _plan(budget=50)
    nut = next(r for r in plan.recommendations if r.strategy == "nutrition")
    assert nut.nutrition_complete is True
    assert nut.nutrition is not None
    assert nut.nutrition.protein_g and nut.nutrition.protein_g > 0
    assert nut.nutrition.energy_kcal and nut.nutrition.energy_kcal > 0


def test_nutrition_unknown_items_are_never_ranked_nor_zeroed():
    """营养未知的商品不得进入定量排名，也不得当 0。

    「薯条」(code 4810) 在真实营养表中没有精确条目 → 含它的组合应标为营养未知。
    """
    idx = build_index(load_fixture("nutrition.json"))
    assert idx.get("薯条") is None
    plan, _ = _plan(budget=40, include_codes=["4810"])
    if plan.recommendations:
        for r in plan.recommendations:
            assert r.nutrition_complete is False
            assert "薯条" in r.nutrition_missing
            assert any("营养未知" in n for n in r.notes)


def test_nutrition_strategy_beats_budget_on_protein():
    plan, _ = _plan(budget=50)
    nut = next(r for r in plan.recommendations if r.strategy == "nutrition")
    bud = next(r for r in plan.recommendations if r.strategy == "budget")
    if bud.nutrition_complete and bud.nutrition:
        assert (nut.nutrition.protein_g or 0) >= (bud.nutrition.protein_g or 0)


def test_protein_goal_is_a_hard_constraint():
    plan, _ = _plan(budget=50, goals=NutritionGoals(protein_g_min=30))
    assert plan.recommendations
    for r in plan.recommendations:
        assert r.nutrition_complete
        assert r.nutrition.protein_g >= 30


def test_energy_ceiling_is_enforced_and_unknown_is_excluded():
    plan, _ = _plan(budget=50, goals=NutritionGoals(energy_kcal_max=400))
    if plan.recommendations:
        for r in plan.recommendations:
            assert r.nutrition_complete
            assert r.nutrition.energy_kcal <= 400
    else:
        assert any("营养目标" in w for w in plan.warnings)


def test_impossible_goal_yields_no_fake_plan():
    """目标无法达成时不得编造：返回空方案 + 明确告警。"""
    plan, _ = _plan(budget=50, goals=NutritionGoals(protein_g_min=999))
    assert plan.recommendations == []
    assert any("营养目标" in w for w in plan.warnings)


def test_nutrition_reasons_mention_protein_and_kcal():
    plan, _ = _plan(budget=50)
    nut = next(r for r in plan.recommendations if r.strategy == "nutrition")
    joined = " ".join(nut.reasons)
    assert "蛋白质" in joined and "kcal" in joined
    assert "营养数据完整" in joined
