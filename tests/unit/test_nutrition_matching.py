"""营养分层匹配与覆盖率（Phase 3）。

覆盖：
- 名称归一化（仅编码差异）可靠匹配；
- 人工别名表匹配，并**逐条验证依据字段存在**；
- **明确拒绝的规则**：套餐名 ⊉ 单品名、去"套餐"后缀、"口味变体"、规格不明；
- ``query-meal-detail`` 规范全名解析（canonical）与套餐默认组成（composition）；
- 覆盖率报告的前后对比。
"""

from __future__ import annotations

from mcpilot.menu import parse_menu
from mcpilot.nutrition import (
    NUTRITION_ALIASES,
    build_index,
    coverage_report,
    normalize_name,
    resolve_name,
    total_from_resolved,
)
from mcpilot.resolve import NutritionEnricher
from tests.conftest import load_fixture


def _idx():
    return build_index(load_fixture("nutrition.json"))


def _menu():
    return parse_menu(load_fixture("menu_3330324.json"))


# --- 归一化 ---------------------------------------------------------------


def test_normalize_handles_fullwidth_whitespace_and_symbols():
    assert normalize_name("纯牛奶(盒装)") == normalize_name("纯牛奶（盒装）")
    assert normalize_name("麦咖啡™美式") == normalize_name("麦咖啡美式")
    assert normalize_name(" 100% 苹果汁 ") == normalize_name("100%苹果汁")


def test_normalized_match_only_for_encoding_differences():
    idx = _idx()
    # 「纯牛奶(盒装)」与营养表「纯牛奶（盒装）」仅括号全/半角之差 → 可靠匹配
    r = resolve_name("纯牛奶(盒装)", idx)
    assert r.tier == "normalized"
    assert r.nutrition is not None and r.nutrition.protein_g == 7


def test_exact_semantics_unchanged_for_backward_compat():
    idx = _idx()
    # 精确匹配语义未被削弱：「薯条」在营养表中仍无同名项
    assert idx.get("薯条") is None
    assert idx.contains("薯条") is False


# --- 别名表 ---------------------------------------------------------------


def test_alias_rules_are_auditable():
    for rule in NUTRITION_ALIASES:
        assert rule.reason.strip(), f"别名规则 {rule.menu_name} 缺少依据说明"
        assert rule.kind in ("packaging", "set_name")


def test_alias_matches_are_used():
    idx = _idx()
    r = resolve_name("100% 苹果汁(盒装)", idx)
    assert r.tier == "alias" and r.matched_name == "100%苹果汁"
    r2 = resolve_name("牛气满满套餐", idx)
    assert r2.tier == "alias" and r2.matched_name == "牛气满满"


# --- 明确拒绝的规则（防错误匹配）------------------------------------------


def test_set_name_is_never_matched_to_its_main_item():
    """「巨无霸四件套」不得匹配到「巨无霸」（否则整份套餐被少算成主餐一份）。"""
    idx = _idx()
    r = resolve_name("巨无霸四件套", idx)
    assert r.tier == "missing" and r.nutrition is None


def test_combo_suffix_strip_is_not_applied():
    """「麦香鸡套餐」不得因去掉"套餐"而匹配到单品「麦香鸡」。"""
    idx = _idx()
    assert resolve_name("麦香鸡套餐", idx).tier == "missing"
    assert resolve_name("巨无霸三件套", idx).tier == "missing"


def test_flavor_variant_is_not_matched():
    """「那么大鸡排（椒盐风味）」与营养表「那么大鸡排」口味不同 → 不得匹配。"""
    idx = _idx()
    assert resolve_name("那么大鸡排（椒盐风味）", idx).tier == "missing"
    assert resolve_name("蓝莓爆爆珠麦旋风", idx).tier == "missing"


def test_ambiguous_size_is_not_guessed():
    """规格不明（薯条/可乐/雪碧）在**未经详情解析**时一律标缺失，不按经验补规格。"""
    idx = _idx()
    for name in ("薯条", "可乐", "雪碧", "玉米杯", "麦乐鸡"):
        assert resolve_name(name, idx).tier == "missing", name


# --- 规范全名（canonical）-------------------------------------------------


def test_canonical_resolution_via_detail_name():
    idx = _idx()
    canonical = {"薯条": "中薯条", "麦乐鸡": "麦乐鸡5块", "卡布奇诺": "卡布奇诺中杯"}
    r = resolve_name("薯条", idx, canonical_names=canonical)
    assert r.tier == "canonical"
    assert r.matched_name == "中薯条" and r.nutrition.energy_kcal == 289


def test_enricher_fetches_detail_and_resolves_simple_name():
    """营养增强器：对无法静态匹配的名称按需拉详情，得到规范名后匹配成功。"""
    idx = _idx()
    menu = _menu()
    details = {"4810": "4810.json", "1401": "1401.json", "4437": "4437.json"}

    def fetcher(code: str):
        return load_fixture(f"detail/{details[code]}")

    enr = NutritionEnricher(idx, menu, fetcher, max_calls=5)
    assert enr.resolve("薯条").matched_name == "中薯条"
    assert enr.resolve("麦乐鸡").matched_name == "麦乐鸡5块"
    assert enr.resolve("玉米杯").matched_name == "小杯玉米杯"
    assert enr.calls == 3


def test_enricher_respects_max_calls():
    idx = _idx()
    menu = _menu()

    def fetcher(code: str):
        return load_fixture("detail/4810.json")

    enr = NutritionEnricher(idx, menu, fetcher, max_calls=0)
    assert enr.resolve("薯条").tier == "missing"
    assert enr.calls == 0


# --- 套餐组成（composition）-----------------------------------------------


def test_set_composition_sums_components():
    idx = _idx()
    menu = _menu()
    enr = NutritionEnricher(idx, menu, lambda code: load_fixture("detail/9900000888.json"), max_calls=5)
    r = enr.resolve("巨无霸四件套")
    assert r.tier == "composition"
    # 巨无霸 513 + 中薯条 289 + 麦乐鸡4块 170 + 可乐中杯 147 = 1119
    assert r.nutrition.energy_kcal == 1119
    assert r.nutrition.protein_g == 27 + 4 + 10 + 0


def test_composition_with_unknown_component_is_missing():
    """组成项无法可靠匹配 → 整份套餐营养标缺失（不猜测、不用部分合计冒充整体）。"""
    idx = _idx()
    from mcpilot.models import SetComponent

    comps = {
        "某套餐": [
            SetComponent(name="巨无霸", code="1100", quantity=1),
            SetComponent(name="神秘新品", code="x", quantity=1),
        ]
    }
    r = resolve_name("某套餐", idx, compositions=comps)
    assert r.tier == "missing" and r.nutrition is None
    assert "无法可靠匹配" in r.evidence


def test_total_from_resolved_marks_missing_not_zero():
    idx = _idx()
    resolved = {"巨无霸": resolve_name("巨无霸", idx), "薯条": resolve_name("薯条", idx)}
    tot = total_from_resolved([("巨无霸", 1), ("薯条", 1)], resolved)
    assert tot.total.energy_kcal == 513
    assert tot.missing == ["薯条"]


# --- 覆盖率报告 -----------------------------------------------------------


def test_coverage_report_before_and_after():
    idx = _idx()
    names = [it.name for it in _menu().items]

    # 前：只做精确匹配（等价于 Phase 2）
    before = sum(1 for n in names if idx.contains(n))
    assert before == 20

    # 后：分层静态匹配（归一化 + 别名），零额外 MCP 调用
    static = coverage_report(names, idx)
    assert static.total == 125
    assert static.exact == 20
    assert static.matched == static.exact + static.normalized + static.alias
    assert static.matched > before, "静态分层匹配应提升覆盖率"
    # 每条新增匹配都必须带可追溯依据
    for e in static.examples_added:
        assert e["evidence"] and e["matched"]

    # 叠加 canonical（模拟详情解析）后进一步提升
    canonical = {"薯条": "中薯条", "麦乐鸡": "麦乐鸡5块", "玉米杯": "小杯玉米杯"}
    after = coverage_report(names, idx, canonical_names=canonical)
    assert after.matched > static.matched
