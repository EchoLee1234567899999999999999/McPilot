"""数据模型（Phase 1 对齐真实 schema；Phase 2 扩展推荐结果模型）。

来源与单位约定（真实 schema）：
- ``query-meals``：``currentPrice`` / ``originalPrice`` 为**字符串，单位「元」**。
- ``calculate-price``：``price`` / ``discount`` / ``originalPrice`` 等为**整数，单位「分」**。
- ``list-nutrition-foods``：``data`` 为 TSV 文本，数值为字符串。
- ``query-store-coupons``：``data[].products[].productCode`` 决定券的适用商品。

单位约定（全项目一致）：
- 内部计算**一律使用整数「分」**（``*_cent``），避免浮点误差；
- 仅在**展示 / 序列化**时用 :func:`mcpilot.pricing.cent_to_yuan` 转「元」（``*_yuan``）。

字段含义另见
``.workbuddy/skills/mcpilot-meal-recommender/references/data-model.md``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Scene = Literal["pickup", "drive_thru"]
# Phase 2 的三种推荐策略（与需求一一对应）。
Strategy = Literal["budget", "nutrition", "balanced"]

STRATEGY_LABELS: dict[str, str] = {
    "budget": "省钱优先",
    "nutrition": "高蛋白优先",
    "balanced": "综合推荐",
}


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


@dataclass
class NutritionGoals:
    """营养目标（均可选；None 表示用户未提出该目标）。"""

    protein_g_min: Optional[float] = None  # 蛋白质下限（g）
    energy_kcal_max: Optional[float] = None  # 热量上限（kcal）
    energy_kcal_target: Optional[float] = None  # 热量目标（kcal）
    fat_g_max: Optional[float] = None  # 脂肪上限（g）

    def to_dict(self) -> dict[str, Any]:
        return {
            "protein_g_min": self.protein_g_min,
            "energy_kcal_max": self.energy_kcal_max,
            "energy_kcal_target": self.energy_kcal_target,
            "fat_g_max": self.fat_g_max,
        }


@dataclass
class UserRequest:
    """用户请求（Phase 2 扩展：人数、口味偏好、忌口、指定商品）。

    兼容 Phase 0/1 字段（``scene`` / ``city`` / ``keyword`` / ``store_code`` /
    ``budget`` / ``taste_prefs`` / ``use_coupon``）。
    """

    scene: Scene = "pickup"
    be_type: int = 1  # 1=到店自取, 5=得来速；与 scene 保持一致的显式表达
    be_code: Optional[str] = None  # 仅得来速(beType=5)/外送/团餐需要
    city: Optional[str] = None
    keyword: Optional[str] = None
    store_code: Optional[str] = None
    budget: Optional[float] = None  # 预算上限（元）
    people: int = 1  # 用餐人数
    goals: NutritionGoals = field(default_factory=NutritionGoals)
    likes: list[str] = field(default_factory=list)  # 口味偏好关键词（命中加分）
    dislikes: list[str] = field(default_factory=list)  # 忌口关键词（命中淘汰）
    include_codes: list[str] = field(default_factory=list)  # 必须包含的商品编码
    use_coupon: bool = True  # 是否尝试用券
    # 兼容 Phase 1 旧字段名（等价于 likes）
    taste_prefs: list[str] = field(default_factory=list)
    strategies: list[Strategy] = field(
        default_factory=lambda: ["budget", "nutrition", "balanced"]
    )
    max_verify: int = 12  # calculate-price 试算次数上限（性能约束）
    max_resolve: int = 10  # 为解析规范名/套餐组成而调用 query-meal-detail 的次数上限

    def effective_likes(self) -> list[str]:
        """合并 ``likes`` 与旧字段 ``taste_prefs``。"""
        out = list(self.likes)
        for t in self.taste_prefs:
            if t not in out:
                out.append(t)
        return out

    def to_dict(self) -> dict[str, Any]:
        """可 JSON 序列化的请求回显（不含任何凭据）。"""
        return {
            "scene": self.scene,
            "be_type": self.be_type,
            "be_code": self.be_code,
            "city": self.city,
            "keyword": self.keyword,
            "store_code": self.store_code,
            "budget": self.budget,
            "people": self.people,
            "goals": self.goals.to_dict(),
            "likes": self.effective_likes(),
            "dislikes": list(self.dislikes),
            "include_codes": list(self.include_codes),
            "use_coupon": self.use_coupon,
            "strategies": list(self.strategies),
            "max_verify": self.max_verify,
            "max_resolve": self.max_resolve,
        }


# ---------------------------------------------------------------------------
# 门店 / 菜单
# ---------------------------------------------------------------------------


@dataclass
class Store:
    """门店。"""

    store_code: str
    name: str = ""
    address: str = ""
    be_code: Optional[str] = None  # 仅得来速(beType=5)有值
    reservation_options: list[str] = field(default_factory=list)


@dataclass
class MenuItem:
    """餐品 / 套餐（来自 ``query-meals``，价格单位「元」）。"""

    code: str
    name: str
    price_yuan: Optional[float] = None  # 现价 currentPrice
    original_price_yuan: Optional[float] = None  # 划线价 originalPrice
    discount_type: Optional[str] = None
    can_with_order: bool = False
    tags: list[str] = field(default_factory=list)
    image: str = ""

    @property
    def is_combo(self) -> bool:
        return "套餐" in self.tags

    @property
    def support_modify(self) -> bool:
        # 是否可特调以 query-meal-detail 的 supportModify 为准，此处仅占位
        return False


@dataclass
class ModificationValue:
    """单个特调项。"""

    code: str
    name: str
    price: int = 0
    selected_quantity: int = 0
    selected_key: str = ""
    unselected_key: str = ""


@dataclass
class SetComponent:
    """套餐（或可选分组）的**默认组成**中的一项（来自 ``query-meal-detail`` 的 ``rounds``）。

    仅当 ``isDefault`` 为真时才会被收录——代表该套餐在**未做任何特调**时的默认构成。
    """

    name: str
    code: str = ""
    quantity: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "quantity": self.quantity}


@dataclass
class MealDetail:
    """餐品详情（来自 ``query-meal-detail``）。

    - ``name`` 是**规范全名**（菜单列表里的简名可能被省略规格，如「薯条」→「中薯条」）。
    - ``components`` 是**默认组成**：单品的 ``rounds`` 通常为空；套餐则为各分组的默认选项。
    """

    code: str
    name: str
    support_modify: bool = False
    image: str = ""
    modifications: list[ModificationValue] = field(default_factory=list)
    components: list[SetComponent] = field(default_factory=list)

    @property
    def has_composition(self) -> bool:
        """是否存在可用于推算营养的默认组成（≥2 项才算真正的"套餐组成"）。"""
        return len(self.components) >= 2


# ---------------------------------------------------------------------------
# 营养
# ---------------------------------------------------------------------------


@dataclass
class Nutrition:
    """营养数据（来自 ``list-nutrition-foods``）。任一字段为 None 视为「未知」。"""

    name: str
    energy_kj: Optional[float] = None
    energy_kcal: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carb_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    calcium_mg: Optional[float] = None

    def is_complete(self) -> bool:
        """核心营养（热量/蛋白质/脂肪/碳水）是否齐全。"""
        return None not in (self.energy_kcal, self.protein_g, self.fat_g, self.carb_g)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "energy_kcal": self.energy_kcal,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "carb_g": self.carb_g,
            "sodium_mg": self.sodium_mg,
            "calcium_mg": self.calcium_mg,
        }


# ---------------------------------------------------------------------------
# 优惠券
# ---------------------------------------------------------------------------


@dataclass
class Coupon:
    """优惠券（来自 ``query-store-coupons``）。

    ``product_codes`` 为券**适用商品编码**；为空表示不限商品（按真实返回为准）。

    ``source`` 标明券的来源（Phase 3）：
    - ``store``：门店可用券（``query-store-coupons``，本项目唯一真实调用的券来源）；
    - 账户已有券 / 可领取券分别需 ``query-my-coupons`` / ``auto-bind-coupons``，
      均属**账号级或写操作**，本项目**明确不调用**（只读白名单外），因此不会出现。
    """

    coupon_id: str
    coupon_code: str = ""
    title: str = ""
    trade_date_time: str = ""
    product_codes: list[str] = field(default_factory=list)
    product_names: list[str] = field(default_factory=list)
    promotion_id: str = ""
    source: str = "store"  # store / account / claimable
    valid_from: Optional[str] = None  # "YYYY-MM-DD HH:MM:SS"
    valid_to: Optional[str] = None

    def is_time_limited(self) -> bool:
        return bool(self.valid_from or self.valid_to)

    def status(self, now: Any = None) -> str:
        """按时效返回 ``active`` / ``not_started`` / ``expired`` / ``unknown``。"""
        from datetime import datetime

        if not self.is_time_limited():
            return "unknown"

        def _parse(s: Optional[str]) -> Optional[datetime]:
            if not s:
                return None
            try:
                return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

        cur = now or datetime.now()
        start = _parse(self.valid_from)
        end = _parse(self.valid_to)
        if start and cur < start:
            return "not_started"
        if end and cur > end:
            return "expired"
        if start or end:
            return "active"
        return "unknown"

    def to_dict(self) -> dict[str, Any]:
        """序列化券；``coupon_id`` / ``coupon_code`` 属账号相关内容，默认脱敏。"""
        return {
            "title": self.title,
            "source": self.source,
            "trade_date_time": self.trade_date_time,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "applicable_product_codes": list(self.product_codes),
            "applicable_product_names": list(self.product_names),
        }


# ---------------------------------------------------------------------------
# 组合 / 报价
# ---------------------------------------------------------------------------


@dataclass
class ComboItem:
    """组合中的单个餐品。"""

    code: str
    name: str = ""
    quantity: int = 1
    modification: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "quantity": self.quantity}


@dataclass
class Combo:
    """餐品组合。"""

    items: list[ComboItem] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [i.code for i in self.items]


@dataclass
class PricedLine:
    """试算中单行商品价格（单位：分）。"""

    product_code: str
    product_name: str
    quantity: int
    original_subtotal_cent: int
    subtotal_cent: int


@dataclass
class Quote:
    """价格试算结果（金额单位：分，来自 ``calculate-price``）。"""

    product_original_price_cent: int = 0
    product_price_cent: int = 0
    original_price_cent: int = 0
    discount_cent: int = 0
    payable_cent: int = 0
    lines: list[PricedLine] = field(default_factory=list)
    coupons_used: list[Coupon] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 推荐结果（Phase 2）
# ---------------------------------------------------------------------------


@dataclass
class RecommendedItem:
    """推荐方案中的单个商品（金额单位：分；营养可能为 None=未知）。"""

    code: str
    name: str
    quantity: int
    line_original_cent: int = 0  # 来自 calculate-price 的 originalSubtotal
    line_payable_cent: int = 0  # 来自 calculate-price 的 subtotal
    nutrition: Optional[Nutrition] = None  # 单个商品的营养（精确匹配；否则 None）
    role: str = ""  # 生成时的角色标签（main/side/drink/dessert/set），仅用于解释
    image: str = ""  # 官方餐品图片 URL（来自 query-meals 的 image 字段；无则为空）

    def to_dict(self, *, cent_to_yuan) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "quantity": self.quantity,
            "role": self.role,
            "image": self.image,
            "line_original_yuan": cent_to_yuan(self.line_original_cent),
            "line_payable_yuan": cent_to_yuan(self.line_payable_cent),
            "nutrition": self.nutrition.to_dict() if self.nutrition else None,
        }


@dataclass
class Recommendation:
    """单条推荐（对外金额同时给出「分」与「元」）。"""

    strategy: Strategy
    label: str
    items: list[RecommendedItem] = field(default_factory=list)
    original_cent: int = 0  # 由 calculate-price 返回
    discount_cent: int = 0  # 由 calculate-price 返回
    payable_cent: int = 0  # 由 calculate-price 返回（真实实付）
    nutrition: Optional[Nutrition] = None  # 组合合计（仅计入可靠匹配项）
    nutrition_missing: list[str] = field(default_factory=list)
    nutrition_complete: bool = False
    coupon: Optional[Coupon] = None
    coupon_note: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    data_source: str = ""
    queried_at: str = ""
    est_subtotal_cent: int = 0  # 仅用于筛选用；**不是**最终价

    def to_dict(self, *, cent_to_yuan) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "items": [i.to_dict(cent_to_yuan=cent_to_yuan) for i in self.items],
            "price": {
                "original_cent": self.original_cent,
                "discount_cent": self.discount_cent,
                "payable_cent": self.payable_cent,
                "original_yuan": cent_to_yuan(self.original_cent),
                "discount_yuan": cent_to_yuan(self.discount_cent),
                "payable_yuan": cent_to_yuan(self.payable_cent),
            },
            "nutrition": self.nutrition.to_dict() if self.nutrition else None,
            "nutrition_missing": list(self.nutrition_missing),
            "nutrition_complete": self.nutrition_complete,
            "coupon": self.coupon.to_dict() if self.coupon else None,
            "coupon_note": self.coupon_note,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "data_source": self.data_source,
            "queried_at": self.queried_at,
        }


@dataclass
class Adjustment:
    """一条**可操作**的调整建议（Phase 4.2）。

    全部字段都来自**本次真实候选与真实试算**的数据，不编造热量 / 蛋白质 / 价格。
    ``patch`` 是可直接合并回需求表单的**最小变更**（键为表单字段路径）。

    重要：生成建议**不会**改动用户原始请求，也**不会**自动重新查询；
    它只是"待用户确认"的候选变更，必须由用户主动重新查询才会生效。
    """

    kind: str  # budget / kcal_max / protein_min / nutrition_goal / dislikes / menu / generic
    field_name: str = ""  # 字段中文名（如「预算」）；序列化为 ``field``
    current: str = ""  # 当前值（展示用）
    suggested: str = ""  # 建议值（展示用）
    reason: str = ""  # 数据依据说明
    patch: dict[str, Any] = field(default_factory=dict)  # 应用到表单的最小变更
    severity: str = "info"  # info / warn

    @property
    def text(self) -> str:
        """单行人类可读文本（供 CLI 与向后兼容的 ``suggestions`` 使用）。"""
        parts: list[str] = []
        if self.field_name:
            parts.append(f"{self.field_name}：{self.current} → {self.suggested}")
        if self.reason:
            parts.append(self.reason)
        return "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field_name,
            "current": self.current,
            "suggested": self.suggested,
            "reason": self.reason,
            "patch": dict(self.patch),
            "severity": self.severity,
            "text": self.text,
        }


@dataclass
class RecommendationPlan:
    """一次推荐的整体结果（可 JSON 序列化，供后续前端消费）。"""

    store_code: str
    be_type: int
    be_code: Optional[str]
    request: UserRequest
    recommendations: list[Recommendation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    data_source: str = ""
    queried_at: str = ""

    def to_dict(self, *, cent_to_yuan) -> dict[str, Any]:
        return {
            "store_code": self.store_code,
            "be_type": self.be_type,
            "be_code": self.be_code,
            "request": self.request.to_dict(),
            "recommendations": [r.to_dict(cent_to_yuan=cent_to_yuan) for r in self.recommendations],
            "warnings": list(self.warnings),
            "suggestions": list(self.suggestions),
            "adjustments": [a.to_dict() for a in self.adjustments],
            "stats": dict(self.stats),
            "data_source": self.data_source,
            "queried_at": self.queried_at,
        }

    def to_json(self, *, cent_to_yuan, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(cent_to_yuan=cent_to_yuan), ensure_ascii=False, indent=indent)
