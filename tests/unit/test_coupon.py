"""优惠券解析与适用性测试（T1 / T2）。"""

from __future__ import annotations

from mcpilot.coupon import best_coupon, coupons_for_product, match_coupons, parse_coupons


def test_parse_coupons(coupons_payload):
    coupons = parse_coupons(coupons_payload)
    assert len(coupons) == 2
    assert {c.title for c in coupons} == {"麦旋风任选", "薯薯任选"}
    assert all(c.product_codes for c in coupons)


def test_coupon_not_applicable_to_unrelated_combo(coupons_payload):
    """巨无霸+薯条的组合不得套用「麦旋风任选 / 薯薯任选」券。"""
    coupons = parse_coupons(coupons_payload)
    matches = match_coupons(["1100", "4810"], coupons)
    assert matches
    assert all(not m.applicable for m in matches)
    assert best_coupon(matches) is None


def test_coupon_applicable_to_its_own_target(coupons_payload):
    coupons = parse_coupons(coupons_payload)
    for c in coupons:
        m = match_coupons(c.product_codes, [c])[0]
        assert m.applicable
        assert m.matched_codes == c.product_codes


def test_coupons_for_product(coupons_payload):
    coupons = parse_coupons(coupons_payload)
    target = coupons[0]
    assert target in coupons_for_product(target.product_codes[0], coupons)
    assert coupons_for_product("1100", coupons) == []


def test_redacted_fixture_has_no_real_coupon_code(coupons_payload):
    """夹具中的券码/券ID/promotionId 必须已脱敏，防止泄露账号相关信息。"""
    for row in coupons_payload["data"]:
        for key in ("couponId", "couponCode", "promotionId"):
            if key in row:
                assert row[key] == "<REDACTED>"
