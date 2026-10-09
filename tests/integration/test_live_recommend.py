"""真实 MCP 集成测试 · 推荐引擎（默认跳过）。

需要显式开启::

    MCPILOT_LIVE=1 python -m pytest tests/integration -v

前置：WorkBuddy 已连接 ``mcd-mcp``（或已设置 ``MCD_MCP_ENDPOINT``）。

原则：
- 只断言**结构与规则**（金额一致性、营养口径、调用次数、只读工具），
  不断言易变的业务数值（价格随活动变化）。
- 记录**实际调用情况**与成功/失败结果（打印 + 断言 stats）。
"""

from __future__ import annotations

import os

import pytest

from mcpilot.mcp_client import McpClient
from mcpilot.models import UserRequest
from mcpilot.pricing import cent_to_yuan
from mcpilot.recommender import recommend

LIVE = os.environ.get("MCPILOT_LIVE") == "1"
STORE = "3330324"

pytestmark = pytest.mark.skipif(not LIVE, reason="真实 MCP 集成测试需设置 MCPILOT_LIVE=1")


def _real_client() -> McpClient:
    return McpClient()


def test_live_recommend_30_yuan_three_strategies():
    plan = recommend(
        UserRequest(store_code=STORE, be_type=1, budget=30, max_verify=12),
        client=_real_client(),
    )
    stats = plan.stats
    print("\n[LIVE] 30元推荐调用统计:", stats["mcp_calls"], "| 候选", stats["candidates"])

    assert stats["mcp_calls"]["query-meals"] == 1
    assert stats["mcp_calls"]["list-nutrition-foods"] == 1
    assert stats["mcp_calls"]["query-store-coupons"] == 1
    assert stats["mcp_calls"]["calculate-price"] <= 12

    assert plan.recommendations, "30 元预算下应至少给出一个真实可行方案"
    for r in plan.recommendations:
        # 金额一致性（真实 calculate-price 返回）
        assert r.original_cent - r.discount_cent == r.payable_cent
        assert r.payable_cent <= 3000
        assert r.payable_cent > 0
        assert sum(i.line_original_cent for i in r.items) == r.original_cent
        assert abs(r.payable_cent / 100 - cent_to_yuan(r.payable_cent)) < 1e-9
        assert r.reasons and r.data_source
        # 营养口径：要么完整（可定量），要么如实标注缺失
        if not r.nutrition_complete:
            assert r.nutrition_missing


def test_live_nutrition_plan_uses_only_reliable_data():
    plan = recommend(
        UserRequest(store_code=STORE, be_type=1, budget=50, max_verify=12),
        client=_real_client(),
    )
    nut = [r for r in plan.recommendations if r.strategy == "nutrition"]
    for r in nut:
        assert r.nutrition_complete is True
        assert r.nutrition and r.nutrition.protein_g and r.nutrition.protein_g > 0


def test_live_insufficient_budget_reports_real_minimum():
    plan = recommend(
        UserRequest(store_code=STORE, be_type=1, budget=1, max_verify=8),
        client=_real_client(),
    )
    assert plan.recommendations == []
    assert any("没有找到可行组合" in w for w in plan.warnings)
    assert plan.suggestions, "预算不足必须给出可解释的调整建议"


def test_live_coupon_applicability_is_evidence_based():
    """券是否使用，完全由真实返回的适用商品编码决定。"""
    plan = recommend(
        UserRequest(store_code=STORE, be_type=1, budget=30, max_verify=10),
        client=_real_client(),
    )
    for r in plan.recommendations:
        if r.coupon is not None:
            codes = {i.code for i in r.items}
            assert codes & set(r.coupon.product_codes), "券只能用于其适用商品"
            assert r.discount_cent >= 0
