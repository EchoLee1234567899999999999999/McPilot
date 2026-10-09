"""离线端到端链路测试（用真实采集夹具驱动，不触网）。

覆盖：正常链路、无适用券、营养缺失、接口失败。
"""

from __future__ import annotations

import pytest

from mcpilot.mcp_client import McpClient, McpTransportError
from mcpilot.pipeline import parse_item_specs, run_minimal
from tests.conftest import FixtureInvoker


def _client(**kw) -> McpClient:
    return McpClient(invoker=FixtureInvoker(**kw))


def test_parse_item_specs():
    assert parse_item_specs("1100:1,4810:2") == [("1100", 1), ("4810", 2)]
    assert parse_item_specs("1100") == [("1100", 1)]
    with pytest.raises(ValueError):
        parse_item_specs("")


def test_pipeline_normal_path():
    result = run_minimal(_client(), store_code="3330324", item_specs=[("1100", 1)])
    assert len(result.menu) > 50
    assert result.combo.items[0].name == "巨无霸"
    # 营养匹配成功
    assert result.nutrition_total.total.energy_kcal == 513
    assert not result.nutrition_total.has_missing
    # 价格来自 calculate-price 真实返回（2600 分）
    assert result.quote.payable_cent == 2600
    assert result.payable_yuan == 26.0


def test_pipeline_marks_missing_nutrition():
    result = run_minimal(_client(), store_code="3330324", item_specs=[("1100", 1), ("4810", 1)])
    assert "薯条" in result.nutrition_total.missing
    assert any("营养未知" in w for w in result.warnings)


def test_pipeline_no_applicable_coupon():
    result = run_minimal(_client(), store_code="3330324", item_specs=[("1100", 1)])
    assert result.coupons  # 门店有券
    assert all(not m.applicable for m in result.coupon_matches)
    assert result.applied_coupon is None
    assert any("没有一张适用" in w for w in result.warnings)


def test_pipeline_unknown_item_code_raises():
    with pytest.raises(ValueError) as exc:
        run_minimal(_client(), store_code="3330324", item_specs=[("99999999", 1)])
    assert "不在门店" in str(exc.value)


def test_pipeline_transport_error_is_clear():
    with pytest.raises(McpTransportError) as exc:
        run_minimal(_client(fail_on="query-meals"), store_code="3330324", item_specs=[("1100", 1)])
    assert "query-meals" in str(exc.value)


def test_pipeline_summary_is_json_safe():
    import json

    result = run_minimal(_client(), store_code="3330324", item_specs=[("1100", 1)])
    text = json.dumps(result.summary(), ensure_ascii=False)
    assert "巨无霸" in text
    assert "REDACTED" not in text
