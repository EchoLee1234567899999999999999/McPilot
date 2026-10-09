"""推荐引擎 · 省钱型与预算/失败场景（Phase 2，离线）。

使用 ``RecommenderInvoker``（明确标注的**测试替身**）确定性驱动；
真实调用见 ``tests/integration/test_live_recommend.py``。
"""

from __future__ import annotations

import copy

import pytest

from mcpilot.mcp_client import McpClient, McpTransportError
from mcpilot.models import UserRequest
from mcpilot.pricing import cent_to_yuan
from mcpilot.recommender import recommend
from tests.conftest import RecommenderInvoker


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


@pytest.mark.parametrize("budget", [20, 30, 50])
def test_budget_plans_stay_within_budget_and_are_cheapest(budget):
    plan, _ = _plan(budget=budget)
    assert plan.recommendations, f"预算 {budget} 应能给出方案"
    for r in plan.recommendations:
        assert r.payable_cent <= round(budget * 100)
    budget_rec = next(r for r in plan.recommendations if r.strategy == "budget")
    others = [r.payable_cent for r in plan.recommendations if r.strategy != "budget"]
    assert all(budget_rec.payable_cent <= p for p in others)


def test_budget_strategy_is_not_an_empty_or_degenerate_pick():
    plan, _ = _plan(budget=30)
    budget_rec = next(r for r in plan.recommendations if r.strategy == "budget")
    assert budget_rec.items, "不允许空组合"
    assert budget_rec.payable_cent > 0, "实付必须为正"
    # 组合必须真实存在于菜单（由 calculate-price 试算成功保证），且有明确件数
    assert sum(i.quantity for i in budget_rec.items) >= 1


def test_price_consistency_from_calculate_price():
    """价格一致性：原价 - 优惠 = 实付；行小计之和 = 原价。"""
    plan, _ = _plan(budget=30)
    for r in plan.recommendations:
        assert r.original_cent - r.discount_cent == r.payable_cent
        assert sum(i.line_original_cent for i in r.items) == r.original_cent


def test_money_unit_consistency_yuan_vs_cent():
    plan, _ = _plan(budget=30)
    for r in plan.recommendations:
        assert abs(cent_to_yuan(r.payable_cent) - r.payable_cent / 100) < 1e-9


def test_insufficient_budget_returns_no_plan_with_suggestion():
    plan, _ = _plan(budget=5)
    assert plan.recommendations == []
    assert any("没有找到可行组合" in w for w in plan.warnings)
    assert plan.suggestions
    assert any("预算" in s for s in plan.suggestions)


def test_menu_change_missing_required_item_raises():
    """菜单变化/商品下架：指定的商品已不在售 → 明确报错，不编造。"""
    payload = copy.deepcopy(RecommenderInvoker().menu_payload)
    # 从 categories 与 meals 中移除 1100（巨无霸），模拟下架
    for cat in payload["data"]["categories"]:
        cat["meals"] = [m for m in (cat.get("meals") or []) if m.get("code") != "1100"]
    payload["data"]["meals"].pop("1100", None)
    invoker = RecommenderInvoker(menu_payload=payload)
    request = UserRequest(store_code="3330324", be_type=1, include_codes=["1100"])
    with pytest.raises(ValueError) as exc:
        recommend(request, client=McpClient(invoker=invoker))
    assert "不在门店在售菜单" in str(exc.value)


def test_transport_failure_on_menu_is_surfaced():
    with pytest.raises(McpTransportError) as exc:
        _plan(budget=30, invoker=RecommenderInvoker(fail_on="query-meals"))
    assert "query-meals" in str(exc.value)


def test_price_verification_total_failure_is_reported_not_faked():
    plan, _ = _plan(budget=30, invoker=RecommenderInvoker(fail_on="calculate-price"))
    assert plan.recommendations == []
    assert any("试算全部失败" in w for w in plan.warnings)
    assert plan.stats["verify_failed"] > 0


def test_mcp_call_budget_is_respected():
    plan, invoker = _plan(budget=30, max_verify=6)
    price_calls = [c for c in invoker.calls if c[0] == "calculate-price"]
    assert len(price_calls) <= 6
    assert plan.stats["mcp_calls"]["calculate-price"] <= 6
    # 只调用白名单只读工具（Phase 3 起合法使用 query-meal-detail 做规范名/套餐组成解析）
    assert {c[0] for c in invoker.calls} <= {
        "query-meals",
        "list-nutrition-foods",
        "query-store-coupons",
        "query-meal-detail",
        "calculate-price",
    }
    # 详情解析同样受限
    assert plan.stats["mcp_calls"]["query-meal-detail"] <= 10
    assert plan.stats["detail_resolved"] <= 10


def test_two_people_scales_portions():
    plan, _ = _plan(budget=60, people=2)
    assert plan.recommendations
    for r in plan.recommendations:
        assert sum(i.quantity for i in r.items) >= 2


def test_include_codes_forced_into_every_plan():
    plan, _ = _plan(budget=35, include_codes=["1100"])
    assert plan.recommendations
    for r in plan.recommendations:
        assert "1100" in [i.code for i in r.items]
