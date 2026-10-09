"""推荐引擎 · 优惠券适用性（Phase 2，离线）。

- **真实券夹具**（``coupons_3330324.redacted.json``）：其适用商品编码不在菜单中，
  因此"券不适用"是**真实**情形。
- **合成券夹具**（``coupons_applicable.synthetic.json``，明确标注 SYNTHETIC）：
  仅用于离线验证"券适用并真实产生优惠"的代码路径。
"""

from __future__ import annotations

from mcpilot.coupon import parse_coupons
from mcpilot.mcp_client import McpClient
from mcpilot.models import UserRequest
from mcpilot.recommender import recommend
from tests.conftest import RecommenderInvoker, load_fixture


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


def test_real_coupons_are_truly_not_applicable():
    coupons = parse_coupons(load_fixture("coupons_3330324.redacted.json"))
    assert coupons
    for c in coupons:
        # 真实券的适用商品编码都不是本店在售商品
        assert all(code.startswith("99") for code in c.product_codes)


def test_no_applicable_coupon_is_reported_and_not_faked():
    plan, _ = _plan(budget=30)
    assert plan.recommendations
    for r in plan.recommendations:
        assert r.coupon is None
        assert r.discount_cent == 0
        assert "未使用优惠券" in r.coupon_note
        assert any("未命中" in n for n in r.notes)
    assert any("未命中" in w for w in plan.warnings)


def test_applicable_coupon_is_used_and_reduces_payable():
    invoker = RecommenderInvoker(coupons_fixture="coupons_applicable.synthetic.json")
    plan, _ = _plan(budget=40, include_codes=["1100"], invoker=invoker)
    assert plan.recommendations
    used = [r for r in plan.recommendations if r.coupon is not None]
    assert used, "合成券适用于巨无霸，包含巨无霸的方案应使用该券"
    for r in used:
        assert r.discount_cent == RecommenderInvoker.COUPON_DISCOUNT_CENT
        assert r.coupon.title.startswith("【合成测试】")
        assert r.original_cent - r.discount_cent == r.payable_cent


def test_coupon_is_never_applied_to_inapplicable_combo():
    """券只能挂到其适用商品行；不适用的组合不得产生优惠。"""
    invoker = RecommenderInvoker(coupons_fixture="coupons_applicable.synthetic.json")
    plan, _ = _plan(budget=40, invoker=invoker)
    for r in plan.recommendations:
        if r.coupon is None:
            assert r.discount_cent == 0
        else:
            assert "1100" in [i.code for i in r.items]


def test_use_coupon_false_disables_coupon():
    invoker = RecommenderInvoker(coupons_fixture="coupons_applicable.synthetic.json")
    plan, _ = _plan(budget=40, include_codes=["1100"], use_coupon=False, invoker=invoker)
    assert plan.recommendations
    assert all(r.coupon is None and r.discount_cent == 0 for r in plan.recommendations)
