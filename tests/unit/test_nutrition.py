"""营养解析与匹配测试（T1 / T4）。"""

from __future__ import annotations

from mcpilot.nutrition import build_index, parse_nutrition_table, total_nutrition


def test_parse_nutrition_table(nutrition_payload):
    rows = parse_nutrition_table(nutrition_payload)
    assert len(rows) > 100
    names = {r.name for r in rows}
    assert "巨无霸" in names
    assert "中薯条" in names


def test_big_mac_nutrition_values(nutrition_payload):
    idx = build_index(nutrition_payload)
    n = idx.get("巨无霸")
    assert n is not None and n.is_complete()
    assert n.energy_kcal == 513
    assert n.protein_g == 27
    assert n.fat_g == 26
    assert n.carb_g == 42


def test_exact_match_only_ambiguous_name_is_missing(nutrition_payload):
    """菜单里的「薯条」在营养表中没有同名项（只有中/大/小薯条）→ 必须标记缺失，不得猜测。"""
    idx = build_index(nutrition_payload)
    assert idx.get("薯条") is None
    assert idx.contains("薯条") is False


def test_total_nutrition_marks_missing_not_zero(nutrition_payload):
    idx = build_index(nutrition_payload)
    total = total_nutrition([("巨无霸", 1), ("薯条", 1)], idx)
    assert total.total.energy_kcal == 513  # 只累计已匹配项
    assert total.missing == ["薯条"]
    assert total.has_missing
    assert not total.complete


def test_total_nutrition_weights_quantity(nutrition_payload):
    idx = build_index(nutrition_payload)
    total = total_nutrition([("巨无霸", 2)], idx)
    assert total.total.energy_kcal == 1026
