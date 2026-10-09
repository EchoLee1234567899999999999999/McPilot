"""价格解析与单位换算测试（T1：价格与 MCP 返回一致）。"""

from __future__ import annotations

import pytest

from mcpilot.mcp_client import McpClient
from mcpilot.pricing import PriceParseError, build_price_items, cent_to_yuan, parse_quote, quote
from tests.conftest import FixtureInvoker


def test_cent_to_yuan():
    assert cent_to_yuan(2600) == 26.0
    assert cent_to_yuan(990) == 9.9
    assert cent_to_yuan(0) == 0.0
    assert cent_to_yuan(None) == 0.0


def test_parse_quote_uses_cent(price_payload):
    q = parse_quote(price_payload)
    assert q.payable_cent == 2600
    assert q.original_price_cent == 2600
    assert q.discount_cent == 0
    assert cent_to_yuan(q.payable_cent) == 26.0
    assert q.lines[0].product_code == "1100"


def test_quote_rejects_failed_payload():
    with pytest.raises(PriceParseError):
        parse_quote({"success": False, "message": "商品不存在"})


def test_build_price_items_attaches_coupon():
    from mcpilot.models import Combo, ComboItem, Coupon

    combo = Combo(items=[ComboItem(code="1100", quantity=1), ComboItem(code="4810", quantity=2)])
    coupon = Coupon(coupon_id="CID", coupon_code="CCODE", title="测试券")
    items = build_price_items(combo, coupon=coupon, coupon_on_code="4810")
    by_code = {i["productCode"]: i for i in items}
    assert "couponId" not in by_code["1100"]
    assert by_code["4810"]["couponId"] == "CID"
    assert by_code["4810"]["quantity"] == 2


def test_quote_via_fixture_invoker():
    client = McpClient(invoker=FixtureInvoker())
    q = quote(client, store_code="3330324", be_type=1, items=[{"productCode": "1100", "quantity": 1}])
    assert q.payable_cent == 2600
