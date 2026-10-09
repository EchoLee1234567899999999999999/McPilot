"""推荐引擎 · 结构化输出与安全（Phase 2，离线）。"""

from __future__ import annotations

import json

from mcpilot.mcp_client import McpClient
from mcpilot.models import UserRequest
from mcpilot.pricing import cent_to_yuan
from mcpilot.recommender import recommend
from tests.conftest import RecommenderInvoker


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


def test_plan_is_json_serializable_for_frontend():
    plan, _ = _plan(budget=30)
    text = plan.to_json(cent_to_yuan=cent_to_yuan)
    data = json.loads(text)
    assert data["store_code"] == "3330324"
    assert data["recommendations"]
    rec = data["recommendations"][0]
    for key in (
        "strategy",
        "label",
        "items",
        "price",
        "nutrition",
        "coupon",
        "reasons",
        "notes",
        "data_source",
        "queried_at",
    ):
        assert key in rec
    assert rec["price"]["payable_yuan"] == round(rec["price"]["payable_cent"] / 100, 2)
    assert rec["data_source"]
    assert rec["queried_at"]


def test_output_contains_no_credentials_or_account_fields():
    plan, _ = _plan(budget=30)
    text = plan.to_json(cent_to_yuan=cent_to_yuan)
    for bad in ("Bearer", "Authorization", "REDACTED", "couponCode", "couponId", "promotionId"):
        assert bad not in text


def test_every_recommendation_has_traceable_reasons_and_source():
    plan, _ = _plan(budget=30)
    for r in plan.recommendations:
        assert r.reasons, "每条推荐必须给出可解释理由"
        assert r.data_source and "MCP" in r.data_source
        assert r.queried_at


def test_stats_report_real_call_counts():
    plan, invoker = _plan(budget=30, max_verify=8)
    stats = plan.stats
    assert stats["mcp_calls"]["query-meals"] == 1
    assert stats["mcp_calls"]["list-nutrition-foods"] == 1
    assert stats["mcp_calls"]["query-store-coupons"] == 1
    assert stats["mcp_calls"]["calculate-price"] == stats["verified"]
    assert stats["candidates"] > 0
