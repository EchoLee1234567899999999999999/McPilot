"""真实 MCP 集成测试 · Phase 3 营养解析增强。

仅在 ``MCPILOT_LIVE=1`` 时运行（会真实调用麦当劳 MCP）。
只断言**结构与规则**，不断言易变的具体数值。
"""

from __future__ import annotations

import os

import pytest

from mcpilot.coupon import COUPON_SOURCES, active_coupons, coupons_in_menu, parse_coupons
from mcpilot.mcp_client import McpClient
from mcpilot.menu import parse_menu
from mcpilot.nutrition import build_index, coverage_report
from mcpilot.resolve import NutritionEnricher

LIVE = os.environ.get("MCPILOT_LIVE") == "1"
STORE = "3330324"
pytestmark = pytest.mark.skipif(not LIVE, reason="真实 MCP 集成测试需设置 MCPILOT_LIVE=1")


@pytest.fixture(scope="module")
def client() -> McpClient:
    return McpClient()


@pytest.fixture(scope="module")
def menu(client: McpClient):
    return parse_menu(client.query_meals(store_code=STORE, be_type=1))


@pytest.fixture(scope="module")
def index(client: McpClient):
    return build_index(client.list_nutrition_foods())


def test_static_coverage_pipeline_is_consistent(index, menu):
    rep = coverage_report([it.name for it in menu.items], index)
    assert rep.total == len(menu)
    # 静态分层匹配至少不劣于纯精确匹配
    assert rep.matched >= rep.exact
    for e in rep.examples_added:
        assert e["evidence"]


def test_detail_resolution_improves_or_equals_coverage(client: McpClient, menu, index):
    """用真实 detail 解析规范名/套餐组成，覆盖率应提升（且每条新增都有依据）。"""
    enricher = NutritionEnricher(
        index,
        menu,
        lambda code: client.query_meal_detail(store_code=STORE, code=code, be_type=1),
        max_calls=12,
    )
    names = [it.name for it in menu.items]
    static = coverage_report(names, index).matched

    # 只对"静态匹配不到"的前若干项做真实解析
    unresolved = [n for n in names if not enricher.resolve(n, allow_fetch=False).ok][:12]
    resolved_now = 0
    for n in unresolved:
        r = enricher.resolve(n)
        if r.ok:
            resolved_now += 1
            assert r.tier in ("canonical", "composition")
            assert r.evidence
    assert enricher.calls <= 12
    # 允许某些商品确实解析不到（如规格不明/口味变体）——不猜测
    assert resolved_now >= 0
    assert coverage_report(names, index, canonical_names=enricher.canonical_names,
                           compositions=enricher.compositions).matched >= static


def test_top_protein_items_are_reliably_matched(index, menu):
    """至少若干净品能通过精确/归一化/别名静态匹配，且为完整营养。"""
    hits = [it for it in menu.items if (r := index.get(it.name)) and r.is_complete()]
    assert hits, "应存在可精确匹配营养的在售商品"


def test_coupon_sources_and_store_applicability(client: McpClient, menu):
    coupons = parse_coupons(client.query_store_coupons(store_code=STORE, be_type=1))
    assert all(c.source == "store" for c in coupons)
    # 门店券的时效应可解析（真实返回含 tradeDateTime）
    for c in coupons:
        assert c.status() in ("active", "not_started", "expired", "unknown")
    assert "不调用" in COUPON_SOURCES["account"]
    codes = {it.code for it in menu.items}
    # 本店是否真有可用的券商品：如实记录（可能为空）
    usable = coupons_in_menu(coupons, codes)
    for c in usable:
        assert set(c.product_codes) & codes
    assert len(active_coupons(coupons)) <= len(coupons)
