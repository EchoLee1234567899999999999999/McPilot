"""真实 MCP 集成测试 · 无结果时的「智能调整建议」（默认跳过）。

需要显式开启::

    MCPILOT_LIVE=1 python -m pytest tests/integration -v

场景即用户实际遇到的那一次查询：**门店自取 / ¥30 / 热量 ≤ 500 kcal /
蛋白质 ≥ 30 g / 不吃辣**，系统应返回 0 套方案，并给出**具体可操作**的调整建议。

只断言**结构与可操作性**（建议条数、字段齐全、patch 键合法、建议值来自真实数据），
不断言易变的具体数值（价格与营养随菜单/活动变化）。
"""

from __future__ import annotations

import os

import pytest

from mcpilot.mcp_client import McpClient
from mcpilot.models import NutritionGoals, UserRequest
from mcpilot.recommender import recommend

LIVE = os.environ.get("MCPILOT_LIVE") == "1"
STORE = "3330324"

pytestmark = pytest.mark.skipif(not LIVE, reason="真实 MCP 集成测试需设置 MCPILOT_LIVE=1")

_ALLOWED_PATCH_KEYS = {
    "budget",
    "people",
    "dislikes",
    "likes",
    "goals.energy_kcal_max",
    "goals.protein_g_min",
    "goals.fat_g_max",
}


def _client() -> McpClient:
    return McpClient()


def _scenario_plan():
    return recommend(
        UserRequest(
            store_code=STORE,
            be_type=1,
            budget=30,
            people=1,
            dislikes=["辣"],
            goals=NutritionGoals(energy_kcal_max=500, protein_g_min=30),
        ),
        client=_client(),
    )


def test_live_no_result_returns_actionable_adjustments():
    plan = _scenario_plan()
    print("\n[LIVE] 无方案场景 stats:", plan.stats)
    for a in plan.adjustments:
        print("[LIVE] 建议:", a.kind, "|", a.text)

    assert not plan.recommendations, "该条件下应无可行方案（与用户实测一致）"
    assert plan.adjustments, "无方案时必须给出调整建议"
    assert len(plan.adjustments) <= 3

    for a in plan.adjustments:
        # 需求二.3：字段 / 当前值 / 建议值 / 说明 必须齐全
        assert a.field_name and a.current and a.suggested and a.reason
        # patch 只允许写入前端可映射的表单字段
        assert set(a.patch) <= _ALLOWED_PATCH_KEYS

    # 向后兼容：suggestions 仍为字符串列表
    assert plan.suggestions == [a.text for a in plan.adjustments]
    assert all(isinstance(s, str) for s in plan.suggestions)


def test_live_blocking_reasons_are_structured_and_scoped():
    """阻断归因必须结构化，且措辞限定在"本次候选范围"，不宣称整菜单无解。"""
    plan = _scenario_plan()
    blocked = plan.stats.get("blocked") or {}
    assert blocked, "应输出结构化阻断归因"
    for key in ("over_budget", "dislike", "kcal", "protein", "fat", "nutrition_unknown"):
        assert key in blocked

    joined = " ".join(plan.warnings)
    assert "本次候选范围" in joined
    assert "不代表整份菜单无解" in joined


def test_live_original_request_is_never_relaxed():
    """建议不得改动用户原始硬约束，也不得自动重新查询。"""
    goals = NutritionGoals(energy_kcal_max=500, protein_g_min=30)
    plan = _scenario_plan()
    assert plan.request.goals.energy_kcal_max == 500
    assert plan.request.goals.protein_g_min == 30
    assert plan.request.budget == 30
    assert plan.request.dislikes == ["辣"]
    # 本次调用只做只读查询，不含任何写操作
    assert set(plan.stats["mcp_calls"]) <= {
        "query-meals",
        "list-nutrition-foods",
        "query-store-coupons",
        "query-meal-detail",
        "calculate-price",
    }


def test_live_budget_scenario_suggestion_matches_real_cheapest():
    """预算不足时，建议的预算数值必须等于真实试算得到的最低实付。"""
    plan = recommend(
        UserRequest(store_code=STORE, be_type=1, budget=3, people=1),
        client=_client(),
    )
    assert not plan.recommendations
    b = next((a for a in plan.adjustments if a.kind == "budget"), None)
    assert b is not None, "预算不足必须给出预算类建议"
    suggested = float(b.patch["budget"])
    print(f"\n[LIVE] 预算建议：¥3.00 → ¥{suggested:.2f}")

    # 用建议值重跑：必须真的能产生方案，且最便宜方案 ≤ 建议预算
    after = recommend(
        UserRequest(store_code=STORE, be_type=1, budget=suggested, people=1),
        client=_client(),
    )
    assert after.recommendations, "按建议提高预算后应能产生方案"
    cheapest = min(r.payable_cent for r in after.recommendations)
    assert cheapest <= int(round(suggested * 100)) + 1
