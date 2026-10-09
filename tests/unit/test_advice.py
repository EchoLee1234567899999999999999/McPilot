"""无可行方案时的「智能调整建议」验证（Phase 4.2，离线）。

覆盖需求：

一、无结果原因分析
   区分 预算不足 / 热量超标 / 蛋白质不足 / 忌口冲突 / 营养缺失 / 菜单商品不足；
   不把"有限候选搜索"说成"整份菜单无解"；营养未知不按 0 计；不擅自放宽原始硬约束。

二、智能调整建议
   最多 3 条；每条含 字段 / 当前值 / 建议值 / 说明；数值必须有真实依据；
   建议只"带回表单"，**不自动重新查询、不静默放宽**；数据不足时给通用提示。

关键验证手段：把建议里的 ``patch`` **真的应用到请求上重跑一次**，
确认确实能产生可行方案 —— 以此证明建议是"可操作且真实"的，而非编造数值。
"""

from __future__ import annotations

from dataclasses import replace

from mcpilot.advice import (
    MAX_SUGGESTIONS,
    BlockInfo,
    blocked_summary,
    build_adjustments,
    summary_warning,
)
from mcpilot.mcp_client import McpClient
from mcpilot.models import NutritionGoals, UserRequest
from mcpilot.recommender import recommend
from tests.conftest import RecommenderInvoker


def _adjust(plan):
    """把 ``RecommendationPlan.adjustments`` 转成便于断言的 dict 列表。"""
    return [a.to_dict() for a in plan.adjustments]


def _run(**kw):
    """离线跑一次推荐（真实夹具、确定性替身）。"""
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


def _apply(plan, adjustment, **extra_kw):
    """把一条建议的 patch 合并回原始请求并重跑（**模拟用户点击并重新查询**）。"""
    req = plan.request
    goals = req.goals
    kw: dict = {}
    for key, val in adjustment["patch"].items():
        if key == "budget":
            kw["budget"] = val
        elif key == "people":
            kw["people"] = val
        elif key == "dislikes":
            kw["dislikes"] = list(val)
        elif key == "likes":
            kw["likes"] = list(val)
        elif key == "goals.energy_kcal_max":
            goals = replace(goals, energy_kcal_max=val)
        elif key == "goals.protein_g_min":
            goals = replace(goals, protein_g_min=val)
        elif key == "goals.fat_g_max":
            goals = replace(goals, fat_g_max=val)
    kw["goals"] = goals
    kw.update(extra_kw)
    return recommend(
        replace(req, **kw), client=McpClient(invoker=RecommenderInvoker())
    )


# ===========================================================================
# 一、纯逻辑：六类阻断归因可区分
# ===========================================================================


def test_budget_block_produces_numeric_budget_suggestion():
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=0,
        budget_cent=300,
        cheapest_over_budget_cent=1390,
    )
    adj = build_adjustments(info)
    assert adj and adj[0].kind == "budget"
    assert adj[0].field_name == "预算"
    assert adj[0].current == "¥3.00"
    assert adj[0].suggested == "¥13.90"
    assert adj[0].patch == {"budget": 13.9}
    assert "13.90" in adj[0].reason and "超出" in adj[0].reason


def test_kcal_block_uses_only_sole_failure_candidates():
    """只有"仅因热量被剔除"的候选才能支撑数值建议（避免误导）。"""
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_by_kcal=3,
        kcal_max=500,
        kcal_fix_kcal=578.0,
        kcal_fix_combo="双层深海鳕鱼堡×1 + 圆筒冰淇淋×1",
        kcal_fix_count=1,
    )
    adj = build_adjustments(info)
    k = next(a for a in adj if a.kind == "kcal_max")
    assert k.field_name == "热量上限"
    assert k.current == "500 kcal"
    assert k.suggested == "578 kcal"
    assert k.patch == {"goals.energy_kcal_max": 578}
    assert "只差热量" in k.reason and "578" in k.reason


def test_kcal_block_without_sole_failure_gives_no_number():
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_by_kcal=2,
        kcal_max=300,
        kcal_fix_kcal=None,  # 没有"只差热量"的候选
    )
    adj = build_adjustments(info)
    k = next(a for a in adj if a.kind == "kcal_max")
    assert k.suggested == "提高上限"
    assert k.patch == {}, "无可靠数值时不得给出可应用的 patch（不编造）"
    assert "不给出具体数值" in k.reason


def test_protein_block_uses_only_sole_failure_candidates():
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_by_protein=6,
        protein_min=30,
        protein_fix_g=28.0,
        protein_fix_combo="双层深海鳕鱼堡×1",
        protein_fix_count=4,
    )
    adj = build_adjustments(info)
    p = next(a for a in adj if a.kind == "protein_min")
    assert p.current == "30 g"
    assert p.suggested == "28 g"
    assert p.patch == {"goals.protein_g_min": 28}
    assert "只差蛋白质" in p.reason


def test_nutrition_unknown_is_separately_attributed():
    """营养未知必须单独归因，且说明"即使放宽数值也无法确认达标"。"""
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_nutrition_unknown=5,
        kcal_max=500,
        protein_min=30,
    )
    adj = build_adjustments(info)
    n = next(a for a in adj if a.kind == "nutrition_goal")
    assert n.severity == "warn"
    assert n.patch == {"goals.energy_kcal_max": None, "goals.protein_g_min": None}
    assert "营养未知" in n.reason and "未按 0 计算" in n.reason


def test_dislike_conflict_reports_hit_keyword():
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=30,
        dropped_by_dislike=4,
        dislikes=["辣", "牛肉"],
        dislike_hits=["辣"],
    )
    adj = build_adjustments(info)
    d = next(a for a in adj if a.kind == "dislikes")
    assert d.field_name == "忌口"
    assert d.current == "辣、牛肉"
    assert "辣" in d.suggested
    assert d.patch == {"dislikes": []}
    assert "4 个" in d.reason


def test_menu_insufficient_when_no_candidates():
    info = BlockInfo(menu_size=125, candidates=0, within_budget=0, dislikes=["辣"], include_codes=["1100"])
    adj = build_adjustments(info)
    m = next(a for a in adj if a.kind == "menu")
    assert "忌口 1 项" in m.current and "指定商品 1 项" in m.current
    assert m.patch == {}
    assert "不代表整份菜单无解" in m.reason


def test_verify_failure_suggests_retry_without_fabricating_prices():
    info = BlockInfo(menu_size=125, candidates=448, within_budget=74, verify_failed=12)
    adj = build_adjustments(info)
    r = next(a for a in adj if a.kind == "retry")
    assert r.severity == "warn"
    assert r.patch == {}
    assert "不使用缓存价" in r.reason


def test_generic_fallback_when_no_evidence():
    info = BlockInfo(menu_size=125, candidates=448, within_budget=74)
    adj = build_adjustments(info)
    assert len(adj) == 1
    assert adj[0].kind == "generic"
    assert adj[0].patch == {}
    # 通用提示不得包含任何具体热量 / 蛋白质 / 价格数值
    assert "kcal" not in adj[0].reason and "¥" not in adj[0].reason and " g" not in adj[0].reason


def test_at_most_three_suggestions_and_priority_order():
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_by_kcal=3,
        dropped_by_protein=6,
        dropped_nutrition_unknown=5,
        dropped_by_dislike=4,
        dislikes=["辣"],
        dislike_hits=["辣"],
        kcal_max=500,
        protein_min=30,
        kcal_fix_kcal=578.0,
        kcal_fix_combo="A",
        kcal_fix_count=1,
        protein_fix_g=28.0,
        protein_fix_combo="B",
        protein_fix_count=4,
    )
    adj = build_adjustments(info)
    assert len(adj) == MAX_SUGGESTIONS == 3
    assert [a.kind for a in adj] == ["kcal_max", "protein_min", "nutrition_goal"]


def test_suggestions_carry_field_current_suggested_and_reason():
    """需求二.3：每条建议都必须包含 字段 / 当前值 / 建议值 / 说明。"""
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_by_kcal=3,
        kcal_max=500,
        kcal_fix_kcal=578.0,
        kcal_fix_combo="A",
        kcal_fix_count=1,
        protein_min=30,
        dropped_by_protein=6,
        protein_fix_g=28.0,
        protein_fix_combo="B",
        protein_fix_count=4,
        budget_cent=None,
    )
    for a in build_adjustments(info):
        assert a.field_name and a.current and a.suggested and a.reason


def test_blocked_summary_and_summary_warning():
    info = BlockInfo(
        menu_size=125,
        candidates=448,
        within_budget=74,
        dropped_by_kcal=3,
        dropped_by_protein=6,
        dropped_nutrition_unknown=5,
    )
    s = blocked_summary(info)
    assert s["kcal"] == 3 and s["protein"] == 6 and s["nutrition_unknown"] == 5
    w = summary_warning(info)
    assert w and "本次候选范围" in w and "不代表整份菜单无解" in w


def test_summary_warning_none_when_no_blockers():
    assert summary_warning(BlockInfo(menu_size=125, candidates=448, within_budget=74)) is None


# ===========================================================================
# 二、端到端：用户真实场景（离线夹具，确定性）
# ===========================================================================


def test_user_scenario_budget30_kcal500_protein30_no_spicy():
    """用户本次真实场景：¥30 / 热量 ≤500 kcal / 蛋白 ≥30 g / 不吃辣。"""
    plan, _ = _run(
        budget=30,
        people=1,
        dislikes=["辣"],
        goals=NutritionGoals(energy_kcal_max=500, protein_g_min=30),
    )
    assert not plan.recommendations, "该条件下应无可行方案（与用户实测一致）"
    adj = _adjust(plan)
    assert adj, "必须给出调整建议"
    assert len(adj) <= 3
    kinds = {a["kind"] for a in adj}
    assert "kcal_max" in kinds and "protein_min" in kinds

    # 每条建议的"字段 / 当前值 / 建议值 / 说明"齐全
    for a in adj:
        assert {"kind", "field", "current", "suggested", "reason", "patch", "text"} <= set(a)

    # 阻断归因可区分
    b = plan.stats["blocked"]
    assert b["kcal"] > 0 and b["protein"] > 0 and b["nutrition_unknown"] > 0


def test_suggested_kcal_ceiling_is_actually_actionable():
    """把建议的热量上限真的应用后重跑，必须真的出现可行方案（证明数值有依据）。"""
    plan, _ = _run(
        budget=30,
        people=1,
        dislikes=["辣"],
        goals=NutritionGoals(energy_kcal_max=500, protein_g_min=30),
    )
    k = next(a for a in _adjust(plan) if a["kind"] == "kcal_max")
    assert k["patch"], "热量建议应带可应用数值"
    after = _apply(plan, k)
    assert after.recommendations, "应用建议后应能产生方案"


def test_suggested_protein_floor_is_actually_actionable():
    plan, _ = _run(
        budget=30,
        people=1,
        dislikes=["辣"],
        goals=NutritionGoals(energy_kcal_max=500, protein_g_min=30),
    )
    p = next(a for a in _adjust(plan) if a["kind"] == "protein_min")
    assert p["patch"]
    after = _apply(plan, p)
    assert after.recommendations, "应用建议后应能产生方案"


def test_suggested_budget_is_actually_actionable():
    plan, _ = _run(budget=3.0, people=1)
    assert not plan.recommendations
    b = next(a for a in _adjust(plan) if a["kind"] == "budget")
    assert b["patch"]["budget"] > 3.0
    after = _apply(plan, b)
    assert after.recommendations, "提高预算到建议值后应能产生方案"
    cheapest = min(r.payable_cent for r in after.recommendations)
    assert cheapest <= int(round(b["patch"]["budget"] * 100)) + 1


def test_adjustments_do_not_mutate_original_request():
    """需求：建议不得擅自改动用户原始条件（只有用户点击后才生效）。"""
    goals = NutritionGoals(energy_kcal_max=500, protein_g_min=30)
    plan, _ = _run(budget=30, people=1, dislikes=["辣"], goals=goals)
    assert plan.request.goals.energy_kcal_max == 500
    assert plan.request.goals.protein_g_min == 30
    assert plan.request.budget == 30
    assert plan.request.dislikes == ["辣"]
    # 建议本身只是数据，不影响本次结果
    assert not plan.recommendations


def test_no_adjustments_when_recommendations_exist():
    plan, _ = _run(budget=30, people=1)
    assert plan.recommendations
    assert plan.adjustments == []
    assert plan.suggestions == []


def test_suggestions_backward_compatible_strings():
    """向后兼容：``suggestions`` 仍为字符串列表，且预算场景含"预算"字样。"""
    plan, _ = _run(budget=3.0, people=1)
    assert plan.suggestions
    assert all(isinstance(s, str) for s in plan.suggestions)
    assert any("预算" in s for s in plan.suggestions)
    assert plan.suggestions == [a.text for a in plan.adjustments]


def test_plan_serialization_includes_adjustments():
    plan, _ = _run(
        budget=30,
        people=1,
        goals=NutritionGoals(energy_kcal_max=500, protein_g_min=30),
    )
    d = plan.to_dict(cent_to_yuan=lambda c: round(c / 100.0, 2))
    assert "adjustments" in d
    assert d["adjustments"] and all("patch" in a for a in d["adjustments"])
    # 序列化结果里不得出现凭据类字样
    import json

    assert "Bearer" not in json.dumps(d, ensure_ascii=False)
