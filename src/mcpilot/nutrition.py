"""营养数据解析与匹配（Phase 1 解析 / Phase 3 分层匹配）。

数据来源（只读）：``list-nutrition-foods``（营养表）、``query-meal-detail``（规范名 / 套餐组成）。

真实 schema：``data`` 是一段 TSV 文本，形如::

    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      巨无霸,null,2146,513,27,26,42,961,171

为什么需要"分层匹配"（Phase 3 结论）
------------------------------------
菜单（``query-meals``）里的名称是 **UI 简名**，会省略规格 / 系列前缀 / 包装说明；
营养表却使用**完整规范名**。这导致直接「名称精确匹配」时，125 个菜单商品里
只有约 20 个命中。分层匹配在**严格保证同一餐品、同一规格**的前提下，依次尝试：

1. ``exact``       —— 字面完全相同（最可靠）；
2. ``normalized``  —— 仅全/半角、空白、括号、引号、™® 等**编码差异**，字符实质相同；
3. ``alias``       —— **人工审核**的别名表（每条含依据），如「包装说明」或"营养表直接收录
   该套餐整体营养"的套名缩写；
4. ``canonical``   —— 用 ``query-meal-detail`` 返回的**规范全名**再匹配（简名→全名）；
5. ``composition`` —— 套餐：按 ``query-meal-detail`` 返回的**默认组成**逐项匹配并加权合计
   （仅当全部组成项都能可靠匹配时才成立）。

**明确拒绝的规则（会导致错误匹配）**：
- ❌ 把套餐名"包含"某单品名当作匹配（如「巨无霸四件套」⊇「巨无霸」）——
  这会把整份套餐的热量少算成主餐一份；
- ❌ 把菜单名去掉「套餐」后缀再匹配（如「麦香鸡套餐」→「麦香鸡」）——套餐≠单品；
- ❌ 规格不明时按经验补默认规格（如「可乐」→「可乐中杯」）——属猜测，一律不采用；
- ❌ 未知项按 0 或低热量处理。

任何一层都匹配不到，即标记「营养信息缺失」，**绝不猜测**。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .models import Nutrition, SetComponent

_HEADER_RE_LEFT = "{"
_HEADER_RE_RIGHT = "}"

# 归一化时移除的"非语义"字符：空白 + 各类括号与引号（全/半角）
_STRIP_CHARS = set(" \t\u3000()（）[]［］【】{}｛｝「」『』“”\"'‘’")
# 归一化时移除的商标/版权符号
_STRIP_SYMBOLS = ("™", "®", "©", "℠")


class NutritionParseError(ValueError):
    """营养结构不符合预期。"""


# ---------------------------------------------------------------------------
# 名称归一化
# ---------------------------------------------------------------------------


def normalize_name(name: Any) -> str:
    """把名称归一化到"仅编码差异"可比较的形式。

    仅做**无损**变换：去除商标/版权符号、NFKC（全角→半角等）、去除空白、去除括号/引号。
    **不**做同义替换、**不**删词、**不**改数字——因此不会把不同规格归一为同一串。

    注意：商标符号必须在 NFKC **之前**去除——NFKC 会把 ``™`` 展开成 ``TM``。
    """
    s = str(name or "")
    for sym in _STRIP_SYMBOLS:
        s = s.replace(sym, "")
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if ch not in _STRIP_CHARS)
    return s.strip()


# ---------------------------------------------------------------------------
# 人工审核的别名表（每条都可追溯依据）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AliasRule:
    """一条经人工审核的菜单名 → 营养表名映射。"""

    menu_name: str
    nutrition_name: str
    kind: str  # "packaging"（包装说明差异）| "set_name"（营养表直接收录该套餐整体营养）
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "menu_name": self.menu_name,
            "nutrition_name": self.nutrition_name,
            "kind": self.kind,
            "reason": self.reason,
        }


# 规则：只收录"同一餐品、同一规格"的确定性别名；每条都必须写明依据。
# 命名原则：宁可少收录，也不收录有一点不确定的条目（不确定即标缺失）。
NUTRITION_ALIASES: tuple[AliasRule, ...] = (
    AliasRule(
        menu_name="100% 苹果汁(盒装)",
        nutrition_name="100%苹果汁",
        kind="packaging",
        reason="营养表仅收录该款苹果汁（100%苹果汁，88 kcal）；菜单名中的「(盒装)」为包装说明，非规格差异。",
    ),
    AliasRule(
        menu_name="牛气满满套餐",
        nutrition_name="牛气满满",
        kind="set_name",
        reason="营养表直接以「牛气满满」收录了该套餐**整体**营养（495 kcal / 蛋白 26 g），与菜单套餐名唯一对应。",
    ),
    AliasRule(
        menu_name="“苹板”支撑Pro套餐",
        nutrition_name="“苹板”支撑Pro",
        kind="set_name",
        reason="营养表直接以「“苹板”支撑Pro」收录该套餐整体营养（476 kcal / 蛋白 25 g），与菜单套餐名唯一对应。",
    ),
)

_ALIAS_BY_MENU: dict[str, AliasRule] = {r.menu_name: r for r in NUTRITION_ALIASES}
_ALIAS_BY_NORM: dict[str, AliasRule] = {normalize_name(r.menu_name): r for r in NUTRITION_ALIASES}


# ---------------------------------------------------------------------------
# 营养表解析
# ---------------------------------------------------------------------------


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() in {"null", "none", "-", ""}:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_nutrition_table(payload: dict[str, Any]) -> list[Nutrition]:
    """解析 ``list-nutrition-foods`` 返回的营养表。

    ``payload['data']`` 为 TSV 文本；若服务端改为结构化数组也应能兼容（见下）。
    """
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise NutritionParseError(f"list-nutrition-foods 返回失败：{payload.get('message')}")
    data = payload.get("data")

    # 兼容：若某天返回结构化数组，直接映射
    if isinstance(data, list):
        out: list[Nutrition] = []
        for row in data:
            if isinstance(row, dict):
                out.append(_row_from_mapping(row))
        return out

    if not isinstance(data, str):
        raise NutritionParseError("list-nutrition-foods.data 既非文本也非数组。")

    head, sep, body = data.partition(":\n")
    if not sep:
        # 尝试仅有换行分隔
        head, _, body = data.partition("\n")
    lbrace = head.find(_HEADER_RE_LEFT)
    rbrace = head.rfind(_HEADER_RE_RIGHT)
    if lbrace < 0 or rbrace <= lbrace:
        raise NutritionParseError("营养表缺少列头定义。")
    cols = [c.strip() for c in head[lbrace + 1 : rbrace].split(",")]

    rows: list[Nutrition] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(cols):
            # 行残缺则跳过（不猜测）
            continue
        rec = dict(zip(cols, parts))
        rows.append(_row_from_mapping(rec))
    return rows


def _row_from_mapping(rec: dict[str, Any]) -> Nutrition:
    return Nutrition(
        name=str(rec.get("productName", "")).strip(),
        energy_kj=_to_float(_as_str(rec.get("energyKj"))),
        energy_kcal=_to_float(_as_str(rec.get("energyKcal"))),
        protein_g=_to_float(_as_str(rec.get("protein"))),
        fat_g=_to_float(_as_str(rec.get("fat"))),
        carb_g=_to_float(_as_str(rec.get("carbohydrate"))),
        sodium_mg=_to_float(_as_str(rec.get("sodium"))),
        calcium_mg=_to_float(_as_str(rec.get("calcium"))),
    )


def _as_str(v: Any) -> str | None:
    return None if v is None else str(v)


# ---------------------------------------------------------------------------
# 索引与分层匹配
# ---------------------------------------------------------------------------


class NutritionIndex:
    """营养表索引。

    - :meth:`get` / :meth:`contains` 保持**精确匹配**语义（Phase 1 行为，向后兼容）。
    - :meth:`get_normalized` / :meth:`get_alias` 提供 Phase 3 的额外匹配层。
    """

    def __init__(self, rows: list[Nutrition]) -> None:
        self.rows = rows
        self._by_name: dict[str, Nutrition] = {r.name: r for r in rows if r.name}
        self._by_norm: dict[str, Nutrition] = {}
        for r in rows:
            key = normalize_name(r.name)
            if key and key not in self._by_norm:
                self._by_norm[key] = r

    def __len__(self) -> int:
        return len(self.rows)

    # -- 精确 --------------------------------------------------------------
    def get(self, name: str) -> Nutrition | None:
        return self._by_name.get(str(name).strip())

    def contains(self, name: str) -> bool:
        return str(name).strip() in self._by_name

    # -- 归一化 ------------------------------------------------------------
    def get_normalized(self, name: str) -> Nutrition | None:
        return self._by_norm.get(normalize_name(name))

    # -- 别名 --------------------------------------------------------------
    def get_alias(self, name: str) -> tuple[Nutrition, AliasRule] | None:
        rule = _ALIAS_BY_MENU.get(str(name).strip()) or _ALIAS_BY_NORM.get(normalize_name(name))
        if rule is None:
            return None
        found = self._by_name.get(rule.nutrition_name) or self._by_norm.get(normalize_name(rule.nutrition_name))
        if found is None:
            return None
        return found, rule


# 匹配层级名（用于报告/测试）
TIER_EXACT = "exact"
TIER_NORMALIZED = "normalized"
TIER_ALIAS = "alias"
TIER_CANONICAL = "canonical"
TIER_COMPOSITION = "composition"
TIER_MISSING = "missing"


@dataclass
class ResolvedNutrition:
    """单个名称的匹配结果（含层级与依据）。"""

    query_name: str
    nutrition: Optional[Nutrition] = None
    tier: str = TIER_MISSING
    matched_name: str = ""
    evidence: str = ""

    @property
    def ok(self) -> bool:
        return self.nutrition is not None and self.nutrition.is_complete()

    @property
    def is_missing(self) -> bool:
        return not self.ok


def build_index(payload_or_rows: dict[str, Any] | list[Nutrition]) -> NutritionIndex:
    rows = payload_or_rows if isinstance(payload_or_rows, list) else parse_nutrition_table(payload_or_rows)
    return NutritionIndex(rows)


def resolve_name(
    name: Any,
    index: NutritionIndex,
    *,
    canonical_names: Optional[dict[str, str]] = None,
    compositions: Optional[dict[str, list[SetComponent]]] = None,
    composition_name_of: Optional[dict[str, str]] = None,
    use_alias: bool = True,
) -> ResolvedNutrition:
    """对单个名称做分层匹配。

    参数
    ----
    canonical_names : ``menu_name -> 规范全名``（来自 ``query-meal-detail`` 的 ``name``）。
    compositions    : ``menu_name -> 默认组成``（来自 detail 的 ``rounds``）。
    composition_name_of : 反查表 ``code -> menu_name``，用于把组成项的名字再解析。
    """
    raw = str(name or "").strip()
    if not raw:
        return ResolvedNutrition(query_name=raw, tier=TIER_MISSING, evidence="名称为空。")

    # 1) 精确
    hit = index.get(raw)
    if hit is not None and hit.is_complete():
        return ResolvedNutrition(raw, hit, TIER_EXACT, hit.name, "菜单名与营养表名称字面完全相同。")

    # 2) 归一化（仅编码差异）
    norm_hit = index.get_normalized(raw)
    if norm_hit is not None and norm_hit.is_complete():
        return ResolvedNutrition(
            raw, norm_hit, TIER_NORMALIZED, norm_hit.name,
            f"归一化后与营养表『{norm_hit.name}』一致（仅全/半角、空白、括号或商标符号差异）。",
        )

    # 3) 人工审核别名
    if use_alias:
        alias_hit = index.get_alias(raw)
        if alias_hit is not None and alias_hit[0].is_complete():
            found, rule = alias_hit
            return ResolvedNutrition(raw, found, TIER_ALIAS, found.name, f"[{rule.kind}] {rule.reason}")

    # 4) 规范全名（来自 query-meal-detail）
    if canonical_names:
        canon = canonical_names.get(raw)
        if canon and canon.strip() and canon.strip() != raw:
            c = index.get(canon) or index.get_normalized(canon)
            if c is not None and c.is_complete():
                return ResolvedNutrition(
                    raw, c, TIER_CANONICAL, c.name,
                    f"菜单简名『{raw}』经 query-meal-detail 解析为规范全名『{canon}』，与营养表一致。",
                )

    # 5) 套餐：按默认组成逐项匹配后加权合计
    if compositions and raw in compositions:
        comp = compositions[raw]
        if len(comp) >= 2:
            total = composition_nutrition(
                comp, index,
                canonical_names=canonical_names,
                compositions=compositions,
                composition_name_of=composition_name_of,
            )
            if total.complete:
                return ResolvedNutrition(
                    raw, total.total, TIER_COMPOSITION, raw,
                    "按 query-meal-detail 返回的**默认组成**（" + total.describe() + "）逐项匹配并加权合计。",
                )
            detail = "、".join(f"{n}(未知)" for n in total.missing)
            return ResolvedNutrition(
                raw, None, TIER_MISSING, "",
                f"套餐默认组成中有无法可靠匹配的项：{detail}（不猜测、不计入）。",
            )

    return ResolvedNutrition(
        raw, None, TIER_MISSING, "",
        "菜单名无法与营养表可靠对应（可能是规格不明、口味变体或营养表未收录）。",
    )


@dataclass
class NutritionTotal:
    """组合营养合计。``total`` 仅累计已匹配项；``missing`` 记录未匹配餐品名。"""

    total: Nutrition = field(default_factory=lambda: Nutrition(name="合计"))
    counted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: 每个组成项的匹配层级（名称 -> tier），便于报告与审计
    tiers: dict[str, str] = field(default_factory=dict)

    @property
    def has_missing(self) -> bool:
        return bool(self.missing)

    @property
    def complete(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        return "、".join(self.counted)


def _accumulate(acc: Nutrition, found: Nutrition, qty: int) -> None:
    acc.energy_kj = (acc.energy_kj or 0) + (found.energy_kj or 0) * qty
    acc.energy_kcal = (acc.energy_kcal or 0) + (found.energy_kcal or 0) * qty
    acc.protein_g = (acc.protein_g or 0) + (found.protein_g or 0) * qty
    acc.fat_g = (acc.fat_g or 0) + (found.fat_g or 0) * qty
    acc.carb_g = (acc.carb_g or 0) + (found.carb_g or 0) * qty
    acc.sodium_mg = (acc.sodium_mg or 0) + (found.sodium_mg or 0) * qty
    acc.calcium_mg = (acc.calcium_mg or 0) + (found.calcium_mg or 0) * qty


def total_nutrition(items: list[tuple[str, int]], index: NutritionIndex) -> NutritionTotal:
    """按 ``[(餐品名, 数量)]`` **精确匹配**加权求和组合营养（Phase 1 行为）。

    未匹配到营养的餐品进入 ``missing``，且**不计入**合计（不用 0 冒充）。
    """
    result = NutritionTotal()
    acc = Nutrition(name="合计")
    for name, qty in items:
        found = index.get(name)
        if found is None or not found.is_complete():
            result.missing.append(name)
            result.tiers[name] = TIER_MISSING
            continue
        result.counted.append(name)
        result.tiers[name] = TIER_EXACT
        _accumulate(acc, found, qty)
    result.total = acc
    return result


def total_nutrition_resolved(
    items: list[tuple[str, int]],
    index: NutritionIndex,
    *,
    canonical_names: Optional[dict[str, str]] = None,
    compositions: Optional[dict[str, list[SetComponent]]] = None,
    composition_name_of: Optional[dict[str, str]] = None,
    use_alias: bool = True,
) -> NutritionTotal:
    """分层匹配版组合营养合计（Phase 3）：任一必需项无法可靠匹配即整体缺失。"""
    result = NutritionTotal()
    acc = Nutrition(name="合计")
    for name, qty in items:
        r = resolve_name(
            name, index,
            canonical_names=canonical_names,
            compositions=compositions,
            composition_name_of=composition_name_of,
            use_alias=use_alias,
        )
        result.tiers[name] = r.tier
        if not r.ok or r.nutrition is None:
            result.missing.append(name)
            continue
        result.counted.append(name)
        _accumulate(acc, r.nutrition, qty)
    result.total = acc
    return result


def total_from_resolved(
    items: list[tuple[str, int]],
    resolved: dict[str, ResolvedNutrition],
) -> NutritionTotal:
    """按"已完成解析"的映射加权求和（避免重复解析/重复请求）。"""
    result = NutritionTotal()
    acc = Nutrition(name="合计")
    for name, qty in items:
        r = resolved.get(name)
        result.tiers[name] = r.tier if r is not None else TIER_MISSING
        if r is None or not r.ok or r.nutrition is None:
            result.missing.append(name)
            continue
        result.counted.append(name)
        _accumulate(acc, r.nutrition, qty)
    result.total = acc
    return result


def composition_nutrition(
    components: Iterable[SetComponent],
    index: NutritionIndex,
    *,
    canonical_names: Optional[dict[str, str]] = None,
    compositions: Optional[dict[str, list[SetComponent]]] = None,
    composition_name_of: Optional[dict[str, str]] = None,
) -> NutritionTotal:
    """按套餐**默认组成**计算营养合计（全部组成项都需可靠匹配）。

    组成项的名称本身通常已是**规范全名**（如「中薯条」），因此优先直接使用；
    仅当名称为空才回退到 ``composition_name_of``（code → 菜单名）。
    对每一项再走一次 :func:`resolve_name`（含 canonical / 别名 / 归一化）。
    递归时**不再**传递 compositions 以免无限展开（组成项本身应为单品）。
    """
    items: list[tuple[str, int]] = []
    for c in components:
        nm = (c.name or "").strip()
        if not nm and composition_name_of:
            nm = composition_name_of.get(c.code, "")
        items.append((nm or c.name, max(1, int(c.quantity))))
    return total_nutrition_resolved(
        items,
        index,
        canonical_names=canonical_names,
        compositions=None,  # 组成项不再展开
        composition_name_of=composition_name_of,
    )


# ---------------------------------------------------------------------------
# 覆盖率报告
# ---------------------------------------------------------------------------


@dataclass
class CoverageReport:
    """菜单营养匹配覆盖率报告（含逐条依据）。"""

    total: int = 0
    exact: int = 0
    normalized: int = 0
    alias: int = 0
    canonical: int = 0
    composition: int = 0
    missing: int = 0
    examples_added: list[dict[str, str]] = field(default_factory=list)
    missing_examples: list[str] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return self.exact + self.normalized + self.alias + self.canonical + self.composition

    @property
    def rate(self) -> float:
        return (self.matched / self.total) if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "exact": self.exact,
            "normalized": self.normalized,
            "alias": self.alias,
            "canonical": self.canonical,
            "composition": self.composition,
            "matched": self.matched,
            "missing": self.missing,
            "rate": round(self.rate, 4),
            "examples_added": list(self.examples_added),
            "missing_examples": list(self.missing_examples),
        }


def coverage_report(
    menu_names: Iterable[str],
    index: NutritionIndex,
    *,
    canonical_names: Optional[dict[str, str]] = None,
    compositions: Optional[dict[str, list[SetComponent]]] = None,
    composition_name_of: Optional[dict[str, str]] = None,
    use_alias: bool = True,
    max_missing_examples: int = 40,
) -> CoverageReport:
    """统计一组菜单名的营养匹配覆盖率，并收集新增匹配的**实际依据**。"""
    rep = CoverageReport()
    for name in menu_names:
        rep.total += 1
        r = resolve_name(
            name, index,
            canonical_names=canonical_names,
            compositions=compositions,
            composition_name_of=composition_name_of,
            use_alias=use_alias,
        )
        if r.tier == TIER_EXACT:
            rep.exact += 1
        elif r.tier == TIER_NORMALIZED:
            rep.normalized += 1
            rep.examples_added.append({"menu_name": r.query_name, "matched": r.matched_name, "tier": r.tier, "evidence": r.evidence})
        elif r.tier == TIER_ALIAS:
            rep.alias += 1
            rep.examples_added.append({"menu_name": r.query_name, "matched": r.matched_name, "tier": r.tier, "evidence": r.evidence})
        elif r.tier == TIER_CANONICAL:
            rep.canonical += 1
            rep.examples_added.append({"menu_name": r.query_name, "matched": r.matched_name, "tier": r.tier, "evidence": r.evidence})
        elif r.tier == TIER_COMPOSITION:
            rep.composition += 1
            rep.examples_added.append({"menu_name": r.query_name, "matched": r.matched_name, "tier": r.tier, "evidence": r.evidence})
        else:
            rep.missing += 1
            if len(rep.missing_examples) < max_missing_examples:
                rep.missing_examples.append(r.query_name)
    return rep
