"""组合生成与约束测试（Phase 2）。"""

from __future__ import annotations

import pytest

from mcpilot.combos import (
    ROLE_DRINK,
    ROLE_MAIN,
    ROLE_SET,
    Candidate,
    ComboConstraints,
    candidates_stats,
    classify_role,
    generate_candidates,
    yuan_to_cent,
)
from mcpilot.menu import parse_menu


def _menu(menu_payload):
    return parse_menu(menu_payload)


def test_yuan_to_cent_is_integer_minor_unit():
    assert yuan_to_cent(9.5) == 950
    assert yuan_to_cent(26) == 2600
    assert isinstance(yuan_to_cent(13.9), int)


@pytest.mark.parametrize(
    "name,role",
    [
        ("巨无霸", ROLE_MAIN),
        ("麦辣鸡腿汉堡", ROLE_MAIN),
        ("可乐", ROLE_DRINK),
        ("中薯条", "side"),
        ("圆筒冰淇淋", "dessert"),
        ("巨无霸三件套", ROLE_SET),
        ("麦当劳联名复古棒球帽", None),  # 非餐品
        ("韩式烟熏芝士风味酱", None),  # 纯调味酱
        ("蘸酱炸鸡", "side"),  # 含"酱"但是餐品
    ],
)
def test_classify_role(name, role):
    assert classify_role(name) == role


def test_generate_respects_people_scaling(menu_payload):
    menu = _menu(menu_payload)
    one = generate_candidates(menu, ComboConstraints(people=1))
    two = generate_candidates(menu, ComboConstraints(people=2))
    assert one and two
    for c in two:
        assert c.total_quantity >= 2  # 每人至少一份
    # 单件套餐按人数配份
    sets_two = [c for c in two if c.is_set]
    assert sets_two and all(c.total_quantity >= 2 for c in sets_two)


def test_generate_excludes_disliked_items(menu_payload):
    menu = _menu(menu_payload)
    spicy = [it.name for it in menu.items if "辣" in it.name]
    assert spicy, "夹具中应存在含'辣'的商品"
    cands = generate_candidates(menu, ComboConstraints(dislikes=["辣"]))
    assert cands
    assert all("辣" not in c.name_list() for c in cands)


def test_generate_enforces_include_codes(menu_payload):
    menu = _menu(menu_payload)
    cands = generate_candidates(menu, ComboConstraints(include_codes=["1100"]))
    assert cands
    assert all("1100" in c.codes for c in cands)


def test_generate_raises_for_unknown_include_code(menu_payload):
    menu = _menu(menu_payload)
    with pytest.raises(ValueError):
        generate_candidates(menu, ComboConstraints(include_codes=["99999999"]))


def test_generate_is_deduplicated(menu_payload):
    menu = _menu(menu_payload)
    cands = generate_candidates(menu, ComboConstraints())
    sigs = [c.signature for c in cands]
    assert len(sigs) == len(set(sigs))
    assert candidates_stats(cands)["count"] == len(cands)


def test_generate_caps_candidate_count(menu_payload):
    menu = _menu(menu_payload)
    cands = generate_candidates(menu, ComboConstraints(max_candidates=30))
    assert len(cands) <= 30


def test_candidate_signature_is_order_insensitive():
    from mcpilot.models import ComboItem

    a = Candidate(items=[ComboItem("a", "甲", 1), ComboItem("b", "乙", 2)])
    b = Candidate(items=[ComboItem("b", "乙", 2), ComboItem("a", "甲", 1)])
    assert a.signature == b.signature
