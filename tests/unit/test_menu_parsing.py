"""菜单与详情解析测试（T1 的一部分）。"""

from __future__ import annotations

from mcpilot.menu import MenuParseError, parse_meal_detail, parse_menu


def test_parse_menu_contains_big_mac(menu_payload):
    menu = parse_menu(menu_payload)
    assert len(menu) > 50  # 真实门店在售餐品数量级
    big_mac = menu.get("1100")
    assert big_mac is not None
    assert big_mac.name == "巨无霸"
    assert big_mac.price_yuan == 26.0  # 真实展示价（元）


def test_menu_prices_are_yuan_not_cent(menu_payload):
    """query-meals 的价格字段为「元」，不应被当作分处理。"""
    menu = parse_menu(menu_payload)
    assert menu.get("1100").price_yuan == 26.0
    assert all((it.price_yuan is None) or (it.price_yuan < 1000) for it in menu.items)


def test_menu_categories_indexed(menu_payload):
    menu = parse_menu(menu_payload)
    assert menu.categories, "应解析出菜单分类"
    assert any("1100" in codes for codes in menu.categories.values())


def test_parse_meal_detail_support_modify(detail_payload):
    detail = parse_meal_detail(detail_payload)
    assert detail.code == "1100"
    assert detail.name == "巨无霸"
    assert detail.support_modify is True
    assert detail.modifications, "巨无霸应含可特调项"


def test_parse_menu_rejects_failed_payload():
    import pytest

    with pytest.raises(MenuParseError):
        parse_menu({"success": False, "message": "门店不存在"})
