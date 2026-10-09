"""推荐引擎 · 综合型与口味偏好（Phase 2，离线）。"""

from __future__ import annotations

from mcpilot.mcp_client import McpClient
from mcpilot.models import UserRequest
from mcpilot.recommender import recommend, score_balanced
from tests.conftest import RecommenderInvoker


def _plan(**kw):
    invoker = kw.pop("invoker", None) or RecommenderInvoker()
    request = UserRequest(store_code="3330324", be_type=1, **kw)
    return recommend(request, client=McpClient(invoker=invoker)), invoker


def test_balanced_plan_exists_and_is_explainable():
    plan, _ = _plan(budget=30)
    bal = next(r for r in plan.recommendations if r.strategy == "balanced")
    assert bal.items
    joined = " ".join(bal.reasons)
    assert "综合评分" in joined
    # 评分维度必须来自真实数据（金额/营养/口味/完整度），不得出现主观饱腹感
    for forbidden in ("饱腹", "口感评分", "主观"):
        assert forbidden not in joined


def test_dislike_filters_out_spicy_items_everywhere():
    plan, _ = _plan(budget=40, dislikes=["辣"])
    assert plan.recommendations
    for r in plan.recommendations:
        assert all("辣" not in i.name for i in r.items)


def test_likes_make_taste_hit_in_balanced():
    plan, _ = _plan(budget=45, likes=["板烧"])
    bal = next(r for r in plan.recommendations if r.strategy == "balanced")
    # 若综合型命中口味，应在理由中说明；若未命中，也应给出可解释的理由
    assert bal.reasons
    hit_names = [i.name for i in bal.items if "板烧" in i.name]
    if hit_names:
        assert any("口味" in x for x in bal.reasons)


def test_balanced_prefers_complete_nutrition_when_available():
    """综合型应避免让"营养未知但更便宜"的组合压过"营养可靠的组合"。"""
    plan, _ = _plan(budget=50)
    bal = next(r for r in plan.recommendations if r.strategy == "balanced")
    complete = [r for r in plan.recommendations if r.nutrition_complete]
    if complete:
        assert bal.nutrition_complete is True


def test_strategies_are_diverse_when_possible():
    plan, _ = _plan(budget=30)
    sigs = ["|".join(sorted(f"{i.code}x{i.quantity}" for i in r.items)) for r in plan.recommendations]
    assert len(sigs) == len(set(sigs)) or any("重复" in x for r in plan.recommendations for x in r.reasons)


def test_balanced_score_components_do_not_use_unknown_as_zero():
    """营养未知的组合：性价比维度必须缺省，而非以 0 参与。"""
    from mcpilot.recommender import _Ctx, _Feat  # noqa: PLC0415

    from mcpilot.combos import Candidate
    from mcpilot.models import ComboItem

    f = _Feat(
        cand=Candidate(items=[ComboItem("x", "未知品", 1)], est_subtotal_cent=1000),
        payable_cent=1000,
        original_cent=1000,
        discount_cent=0,
        est_subtotal_cent=1000,
        coupon=None,
        protein_g=None,
        kcal=None,
        complete=False,
        missing=["未知品"],
        taste_hits=[],
        distinct=1,
        qty=1,
        is_set=False,
        single_item=True,
    )
    ctx = _Ctx(budget_cent=2000, max_payable_cent=2000, max_protein_per_yuan=1.0)
    score, comps = score_balanced(f, ctx)
    assert "蛋白质性价比" not in comps
    assert comps["数据完整度"] == 0.0
    assert 0.0 <= score <= 1.0
