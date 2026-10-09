"""优惠券时效、来源分类与门店适用性（Phase 3）。

说明：
- 门店券（query-store-coupons）是**本项目唯一真实调用的券来源**；
- 账户已有券 / 可领取券需账号级或写操作接口，本项目**明确不调用**（见 ``coupon.COUPON_SOURCES``）。
"""

from __future__ import annotations

import copy
from datetime import datetime

from mcpilot.coupon import (
    COUPON_SOURCES,
    active_coupons,
    best_coupon,
    coupons_in_menu,
    match_coupons,
    parse_coupons,
    parse_trade_window,
)
from mcpilot.menu import parse_menu
from tests.conftest import load_fixture

NOW = datetime(2026, 10, 9, 18, 7, 0)


def _coupons():
    return parse_coupons(load_fixture("coupons_3330324.redacted.json"))


def _with_window(payload, window: str, idx: int = 0):
    data = copy.deepcopy(payload)
    data["data"][idx]["tradeDateTime"] = window
    return parse_coupons(data)[idx]


# --- 有效期解析 -----------------------------------------------------------


def test_parse_trade_window_real_format():
    assert parse_trade_window("2026-10-05 10:30:00-2026-10-09 23:59:59") == (
        "2026-10-05 10:30:00",
        "2026-10-09 23:59:59",
    )
    assert parse_trade_window("") == (None, None)
    assert parse_trade_window("not-a-date") == (None, None)


def test_coupon_status_active_expired_not_started():
    payload = load_fixture("coupons_3330324.redacted.json")
    assert _with_window(payload, "2026-10-05 10:30:00-2026-10-09 23:59:59").status(NOW) == "active"
    assert _with_window(payload, "2026-09-01 00:00:00-2026-09-02 00:00:00").status(NOW) == "expired"
    assert _with_window(payload, "2026-11-01 00:00:00-2026-11-02 00:00:00").status(NOW) == "not_started"


def test_expired_coupon_is_not_usable_even_if_codes_match():
    payload = load_fixture("coupons_3330324.redacted.json")
    c = _with_window(payload, "2026-09-01 00:00:00-2026-09-02 00:00:00")
    m = match_coupons(c.product_codes, [c], NOW)[0]
    assert m.applicable is True  # 编码层面适用
    assert m.time_status == "expired"
    assert m.usable is False
    assert best_coupon([m]) is None  # 过期券不得被选用


def test_active_window_filters_expired():
    payload = load_fixture("coupons_3330324.redacted.json")
    cs = _coupons()
    assert len(active_coupons(cs, NOW)) == len(cs)  # 真实券在采集时点均有效
    expired = _with_window(payload, "2026-09-01 00:00:00-2026-09-02 00:00:00")
    assert active_coupons([expired], NOW) == []


# --- 来源分类 -------------------------------------------------------------


def test_coupon_source_is_store_and_account_sources_are_documented():
    for c in _coupons():
        assert c.source == "store"
    # 账户券 / 可领取券的来源说明必须存在且明确"不调用"
    assert "不调用" in COUPON_SOURCES["account"]
    assert "不调用" in COUPON_SOURCES["claimable"]


def test_serialized_coupon_has_no_account_fields():
    for c in _coupons():
        d = c.to_dict()
        assert "coupon_id" not in d and "coupon_code" not in d
        assert d["source"] == "store"


# --- 门店适用性 -----------------------------------------------------------


def test_real_coupons_are_not_applicable_to_this_store_menu():
    """真实门店券的适用商品编码不在本店在售菜单中 → 门店不适用（如实标注）。"""
    menu = parse_menu(load_fixture("menu_3330324.json"))
    codes = {it.code for it in menu.items}
    cs = _coupons()
    assert coupons_in_menu(cs, codes) == []  # 本店无券可用
    for c in cs:
        # 券的适用商品编码确实不在菜单里（不是"没试"，而是"确实不适用"）
        assert not (set(c.product_codes) & codes)


def test_synthetic_coupon_path_is_usable_and_marked_synthetic():
    """合成券（明确标注 SYNTHETIC）用于验证"券适用"的代码路径，不冒充真实。"""
    payload = load_fixture("coupons_applicable.synthetic.json")
    assert "SYNTHETIC" in payload["_fixture_note"]
    cs = parse_coupons(payload)
    m = match_coupons(["1100"], cs, NOW)[0]
    assert m.usable and m.applicable
    assert best_coupon([m]) is not None
