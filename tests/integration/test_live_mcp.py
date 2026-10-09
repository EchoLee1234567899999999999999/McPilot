"""真实 MCP 集成测试（默认跳过）。

这些测试会**真实调用**麦当劳 MCP（门店 3330324），因此默认跳过，避免无谓的
网络与额度消耗。需要时显式开启::

    MCPILOT_LIVE=1 python -m pytest tests/integration -v

前置：WorkBuddy 已连接 ``mcd-mcp``（或已设置 ``MCD_MCP_ENDPOINT``）。
断言只覆盖**结构与规则**，不断言易变的业务数值（价格会随活动变化）。
"""

from __future__ import annotations

import os

import pytest

from mcpilot.coupon import best_coupon, match_coupons, parse_coupons
from mcpilot.mcp_client import McpClient, McpToolError, McpTransportError
from mcpilot.menu import parse_menu
from mcpilot.nutrition import build_index
from mcpilot.pipeline import run_minimal

LIVE = os.environ.get("MCPILOT_LIVE") == "1"
STORE = "3330324"

pytestmark = pytest.mark.skipif(not LIVE, reason="真实 MCP 集成测试需设置 MCPILOT_LIVE=1")


def test_live_menu():
    c = McpClient()
    menu = parse_menu(c.query_meals(store_code=STORE, be_type=1))
    assert len(menu) > 0
    assert menu.get("1100") is not None
    assert menu.get("1100").name == "巨无霸"


def test_live_nutrition_matching():
    c = McpClient()
    idx = build_index(c.list_nutrition_foods())
    assert len(idx) > 0
    big_mac = idx.get("巨无霸")
    assert big_mac is not None and big_mac.energy_kcal and big_mac.protein_g


def test_live_coupons_scope():
    c = McpClient()
    coupons = parse_coupons(c.query_store_coupons(store_code=STORE, be_type=1))
    matches = match_coupons(["1100"], coupons)
    # 不吹毛求疵：只要匹配结论自洽（适用则命中，否则不命中）
    for m in matches:
        if m.applicable:
            assert set(m.coupon.product_codes) & {"1100"} or not m.coupon.product_codes
    assert best_coupon(matches) in (None, *[m for m in matches if m.applicable])


def test_live_price_matches_cent_and_yuan_consistency():
    c = McpClient()
    menu = parse_menu(c.query_meals(store_code=STORE, be_type=1))
    item = menu.get("1100")
    payload = c.calculate_price(store_code=STORE, be_type=1, items=[{"productCode": "1100", "quantity": 1}])
    price_cent = int(payload["data"]["price"])
    # 展示价（元）与试算价（分）在同一口径下应一致
    assert round(price_cent / 100, 2) == round(item.price_yuan, 2)


def test_live_full_pipeline():
    result = run_minimal(McpClient(), store_code=STORE, item_specs=[("1100", 1)])
    assert result.quote.payable_cent > 0
    assert result.nutrition_total.total.energy_kcal  # 巨无霸营养可匹配
    assert result.data_source


def test_live_error_is_surfaced_for_bad_store():
    """不存在的门店应抛出明确错误，而不是返回编造数据。"""
    c = McpClient()
    with pytest.raises((McpToolError, McpTransportError, ValueError)):
        run_minimal(c, store_code="0000000", item_specs=[("1100", 1)])
