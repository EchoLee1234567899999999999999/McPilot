"""无可行方案时的「智能调整建议」生成（Phase 4.2）。

目标：当本次查询返回 0 套方案时，给出**具体、可操作、有数据依据**的调整建议，
而不是只让用户自己猜。

硬约束（不可违反）
------------------
1. 只用**本次真实候选与真实试算**得到的数据；绝不编造热量 / 蛋白质 / 价格。
2. **不擅自放宽**用户原始硬约束：建议是"待用户确认"的候选变更（``patch``），
   本模块**只返回建议**，不改动请求、不重新查询、不静默放宽任何条件。
3. 措辞限定在**本次已生成并试算的候选范围**内，不宣称整份菜单无解。
4. 营养未知**不按 0 计**：因"营养未知、无法确认达标"被剔除的组合单独归因。
5. 数据不足时退回**通用提示**，不虚构具体数值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Adjustment

__all__ = [
    "BlockInfo",
    "build_adjustments",
    "blocked_summary",
    "summary_warning",
    "MAX_SUGGESTIONS",
]

#: 最多向用户展示的建议条数（需求明确：最多 3 条）
MAX_SUGGESTIONS = 3


@dataclass
class BlockInfo:
    """本次查询的**真实**阻断归因与数值证据（由推荐主流程填充）。

    所有计数字段都来自本次实际处理的候选；所有 ``*_cent`` / ``*_kcal`` /
    ``*_protein`` 都来自真实试算或可靠匹配的营养数据。
    """

    # --- 规模 ---
    menu_size: int = 0
    candidates: int = 0
    within_budget: int = 0
    verified: int = 0

    # --- 归因计数（相互之间可重叠；均为本次真实统计）---
    dropped_over_budget: int = 0  # 真实实付超预算
    dropped_by_dislike: int = 0  # 套餐组成命中忌口
    dropped_by_kcal: int = 0  # 热量超过上限
    dropped_by_protein: int = 0  # 蛋白质未达下限
    dropped_by_fat: int = 0  # 脂肪超过上限
    dropped_nutrition_unknown: int = 0  # 营养未知、无法确认达标
    verify_failed: int = 0  # 价格试算失败

    # --- 数值证据（全部真实；None 表示本次没有可用数据）---
    # 注意：只统计**仅因该条件**被剔除的候选——放宽该条件即可纳入它们；
    # 若某候选同时不满足多个条件，则不能用它来声称"放宽某一个就能成功"。
    cheapest_over_budget_cent: Optional[int] = None
    kcal_fix_kcal: Optional[float] = None  # 仅因热量超标被剔除者中的最低热量
    kcal_fix_combo: str = ""
    kcal_fix_count: int = 0
    protein_fix_g: Optional[float] = None  # 仅因蛋白质不足被剔除者中的最高蛋白质
    protein_fix_combo: str = ""
    protein_fix_count: int = 0

    # --- 用户原始条件（用于展示"当前值"）---
    budget_cent: Optional[int] = None
    kcal_max: Optional[float] = None
    protein_min: Optional[float] = None
    fat_max: Optional[float] = None
    dislikes: list[str] = field(default_factory=list)
    dislike_hits: list[str] = field(default_factory=list)
    include_codes: list[str] = field(default_factory=list)

    # --- 后端是否拿到过任何真实数据 ---
    has_data: bool = True


def _yuan(cent: Optional[int]) -> str:
    if cent is None:
        return "—"
    return f"¥{cent / 100:.2f}"


def _goals_desc(info: BlockInfo) -> str:
    bits: list[str] = []
    if info.kcal_max is not None:
        bits.append(f"热量 ≤ {info.kcal_max:g} kcal")
    if info.protein_min is not None:
        bits.append(f"蛋白 ≥ {info.protein_min:g} g")
    if info.fat_max is not None:
        bits.append(f"脂肪 ≤ {info.fat_max:g} g")
    return "、".join(bits) if bits else "未设置"


def build_adjustments(info: BlockInfo, *, max_items: int = MAX_SUGGESTIONS) -> list[Adjustment]:
    """依据**真实**阻断归因生成建议（最多 ``max_items`` 条，先到先得）。

    返回的建议**不改动**任何用户条件；``patch`` 仅供前端"一键带回表单"，
    是否应用、是否重新查询完全由用户决定。
    """
    out: list[Adjustment] = []
    limit = max(0, int(max_items))

    def add(a: Adjustment) -> None:
        if len(out) < limit:
            out.append(a)

    # 1) 菜单 / 条件把可选商品全部排除（候选为 0）
    if info.candidates == 0:
        cond_bits: list[str] = []
        if info.dislikes:
            cond_bits.append(f"忌口 {len(info.dislikes)} 项")
        if info.include_codes:
            cond_bits.append(f"指定商品 {len(info.include_codes)} 项")
        add(
            Adjustment(
                kind="menu",
                field_name="组合条件",
                current="、".join(cond_bits) if cond_bits else "当前条件",
                suggested="放宽忌口 / 去掉指定商品",
                reason=(
                    f"本次门店在售 {info.menu_size} 件商品，但在当前条件下生成到 0 个候选组合"
                    "（忌口或指定商品可能把可选商品全部排除；这是本次候选范围的结果，"
                    "不代表整份菜单无解）。"
                ),
                patch={},
                severity="warn",
            )
        )

    # 2) 预算不足：给出**真实试算**得到的最低实付
    if (
        info.budget_cent is not None
        and info.within_budget == 0
        and info.cheapest_over_budget_cent is not None
    ):
        cheapest = int(info.cheapest_over_budget_cent)
        gap = cheapest - int(info.budget_cent)
        add(
            Adjustment(
                kind="budget",
                field_name="预算",
                current=_yuan(info.budget_cent),
                suggested=_yuan(cheapest),
                reason=(
                    f"本次在售 {info.menu_size} 件、生成候选 {info.candidates} 个，其中预算内 0 个；"
                    f"在已试算的候选中，最便宜的可行组合实付 {_yuan(cheapest)}"
                    f"（超出当前预算 {_yuan(gap)}）。也可去掉组合中的一件商品。"
                ),
                patch={"budget": round(cheapest / 100.0, 2)},
                severity="warn",
            )
        )

    # 3) 热量上限过低：只有"**仅因热量**被剔除"的候选才能支撑"提高上限即可纳入"
    if info.dropped_by_kcal > 0 and info.kcal_max is not None:
        if info.kcal_fix_kcal is not None:
            ceil_kcal = int(math.ceil(float(info.kcal_fix_kcal)))
            if ceil_kcal > info.kcal_max:
                combo = f"「{info.kcal_fix_combo}」" if info.kcal_fix_combo else ""
                add(
                    Adjustment(
                        kind="kcal_max",
                        field_name="热量上限",
                        current=f"{info.kcal_max:g} kcal",
                        suggested=f"{ceil_kcal:g} kcal",
                        reason=(
                            f"本次预算内有 {info.dropped_by_kcal} 个候选热量超过上限；"
                            f"其中 {info.kcal_fix_count} 个只差热量这一项，"
                            f"最低的是{combo} {info.kcal_fix_kcal:.0f} kcal。"
                            f"把上限提高到 {ceil_kcal} kcal 后，该组合即可纳入（其余条件均已满足）。"
                        ),
                        patch={"goals.energy_kcal_max": ceil_kcal},
                        severity="info",
                    )
                )
        else:
            add(
                Adjustment(
                    kind="kcal_max",
                    field_name="热量上限",
                    current=f"{info.kcal_max:g} kcal",
                    suggested="提高上限",
                    reason=(
                        f"本次有 {info.dropped_by_kcal} 个候选因热量超过上限被剔除，"
                        "但没有候选是只差热量这一项（多数还同时不满足其它条件），"
                        "故不给出具体数值以免误导；可同时调整多项条件后重新查询。"
                    ),
                    patch={},
                    severity="info",
                )
            )

    # 4) 蛋白质下限过高：同上，只采用"**仅因蛋白质**被剔除"的候选
    if info.dropped_by_protein > 0 and info.protein_min is not None:
        if info.protein_fix_g is not None:
            floor_g = int(math.floor(float(info.protein_fix_g)))
            if floor_g < info.protein_min:
                combo = f"「{info.protein_fix_combo}」" if info.protein_fix_combo else ""
                add(
                    Adjustment(
                        kind="protein_min",
                        field_name="蛋白质下限",
                        current=f"{info.protein_min:g} g",
                        suggested=f"{floor_g:g} g",
                        reason=(
                            f"本次预算内有 {info.dropped_by_protein} 个候选蛋白质未达下限；"
                            f"其中 {info.protein_fix_count} 个只差蛋白质这一项，"
                            f"最高的是{combo} {info.protein_fix_g:.1f} g。"
                            f"把下限降到 {floor_g} g 后，该组合即可纳入（其余条件均已满足）。"
                        ),
                        patch={"goals.protein_g_min": floor_g},
                        severity="info",
                    )
                )
        else:
            add(
                Adjustment(
                    kind="protein_min",
                    field_name="蛋白质下限",
                    current=f"{info.protein_min:g} g",
                    suggested="降低下限",
                    reason=(
                        f"本次有 {info.dropped_by_protein} 个候选因蛋白质未达下限被剔除，"
                        "但没有候选是只差蛋白质这一项，故不给出具体数值以免误导。"
                    ),
                    patch={},
                    severity="info",
                )
            )

    # 5) 脂肪上限
    if info.dropped_by_fat > 0 and info.fat_max is not None:
        add(
            Adjustment(
                kind="fat_max",
                field_name="脂肪上限",
                current=f"{info.fat_max:g} g",
                suggested="提高或移除",
                reason=(
                    f"本次有 {info.dropped_by_fat} 个候选因脂肪超过上限被剔除。"
                    "（脂肪上限无候选池数值证据时不给具体数值，避免编造。）"
                ),
                patch={"goals.fat_g_max": None},
                severity="info",
            )
        )

    # 6) 营养缺失：无法确认达标，数值放宽也无用
    if info.dropped_nutrition_unknown > 0:
        add(
            Adjustment(
                kind="nutrition_goal",
                field_name="营养目标",
                current=_goals_desc(info),
                suggested="移除营养目标后再查询",
                reason=(
                    f"另有 {info.dropped_nutrition_unknown} 个候选因组合内存在营养未知的商品，"
                    "无法确认是否达标而按规则剔除（未按 0 计算）；这些组合即使放宽数值也无法确认达标。"
                ),
                patch={"goals.energy_kcal_max": None, "goals.protein_g_min": None},
                severity="warn",
            )
        )

    # 7) 忌口冲突
    if info.dropped_by_dislike > 0 and info.dislikes:
        hit = "、".join(info.dislike_hits) if info.dislike_hits else "、".join(info.dislikes)
        add(
            Adjustment(
                kind="dislikes",
                field_name="忌口",
                current="、".join(info.dislikes),
                suggested=f"移除忌口「{hit}」",
                reason=(
                    f"有 {info.dropped_by_dislike} 个候选因套餐组成命中忌口「{hit}」被剔除"
                    "（忌口是硬约束，命中即淘汰，不会作为推荐出现）。"
                ),
                patch={"dislikes": []},
                severity="warn",
            )
        )

    # 8) 价格试算失败（接口 / 网络）——建议稍后重试，不猜测价格
    if not out and info.verify_failed > 0:
        add(
            Adjustment(
                kind="retry",
                field_name="",
                current="",
                suggested="",
                reason=(
                    f"本次 {info.verify_failed} 个候选的价格试算全部失败（接口或网络问题），"
                    "未能得到可信实付；本项目不使用缓存价、也不臆造价格，因此不给具体数值建议。"
                    "建议稍后重试。"
                ),
                patch={},
                severity="warn",
            )
        )

    # 9) 兜底：数据不足时给通用提示（不伪造数值）
    if not out:
        add(
            Adjustment(
                kind="generic",
                field_name="其它调整方向",
                current="当前条件",
                suggested="提高预算 / 减少人数 / 放宽条件 / 换门店",
                reason=(
                    "本次未生成可行方案，且不足以给出可靠的具体数值建议。可尝试："
                    "① 适当提高预算；② 减少人数 / 份数；③ 放宽忌口或营养目标；"
                    "④ 更换门店（不同门店在售商品可能不同）后重试。"
                ),
                patch={},
                severity="info",
            )
        )
    elif len(out) < limit:
        add(
            Adjustment(
                kind="generic",
                field_name="其它",
                current="当前条件",
                suggested="更换门店 / 减少人数 / 去掉一件商品",
                reason=(
                    "也可尝试：更换门店（在售商品可能不同）、减少用餐人数，"
                    "或去掉组合中的一件商品后重新查询。以上为通用提示，未包含具体数值。"
                ),
                patch={},
                severity="info",
            )
        )

    return out[:limit]


def blocked_summary(info: BlockInfo) -> dict[str, int]:
    """阻断归因的机器可读统计（放入 ``stats``，供前端/报告引用）。"""
    return {
        "over_budget": int(info.dropped_over_budget),
        "dislike": int(info.dropped_by_dislike),
        "kcal": int(info.dropped_by_kcal),
        "protein": int(info.dropped_by_protein),
        "fat": int(info.dropped_by_fat),
        "nutrition_unknown": int(info.dropped_nutrition_unknown),
        "verify_failed": int(info.verify_failed),
        "candidates": int(info.candidates),
        "within_budget": int(info.within_budget),
    }


def summary_warning(info: BlockInfo) -> Optional[str]:
    """把阻断归因汇总成一条**可读**的说明（用于"数据说明与限制"）。

    措辞严格遵守：仅描述**本次已生成并试算的候选范围**，不宣称整份菜单无解。
    """
    parts: list[str] = []
    if info.dropped_over_budget:
        parts.append(f"真实实付超预算 {info.dropped_over_budget} 个")
    if info.dropped_by_dislike:
        parts.append(f"套餐组成命中忌口 {info.dropped_by_dislike} 个")
    if info.dropped_by_kcal:
        parts.append(f"热量超过上限 {info.dropped_by_kcal} 个")
    if info.dropped_by_protein:
        parts.append(f"蛋白质未达下限 {info.dropped_by_protein} 个")
    if info.dropped_by_fat:
        parts.append(f"脂肪超过上限 {info.dropped_by_fat} 个")
    if info.dropped_nutrition_unknown:
        parts.append(f"营养未知、无法确认达标 {info.dropped_nutrition_unknown} 个")
    if info.verify_failed:
        parts.append(f"价格试算失败 {info.verify_failed} 个")
    if not parts:
        return None
    return (
        f"本次已生成候选 {info.candidates} 个（预算内 {info.within_budget} 个），"
        f"在本次候选范围（本次已生成并试算的候选）内被排除的原因：" + "；".join(parts) + "。"
        "（这不代表整份菜单无解；如需更大搜索范围可调整条件后重新查询。）"
    )
