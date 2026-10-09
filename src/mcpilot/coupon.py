"""优惠券解析、时效判定与适用性匹配（Phase 1 匹配 / Phase 3 时效与来源分类）。

数据来源（只读）：``query-store-coupons``。

真实 schema：``data`` 为数组，元素形如::

    {"title":"麦旋风任选",
     "couponId":"...","couponCode":"...",
     "tradeDateTime":"2026-10-05 10:30:00-2026-10-09 23:59:59",
     "products":[{"productCode":"9900014239","productName":"麦旋风任选1"}],
     "promotionId":"112458030"}

券的三种来源（Phase 3 明确区分）
--------------------------------
1. **门店可用券** —— ``query-store-coupons``，**本项目唯一真实调用的券来源**；
2. **账户已有券** —— 需 ``query-my-coupons``（账号级接口）；
3. **可领取券** —— 需 ``auto-bind-coupons``（**写操作**）。

②③ 均**不在只读白名单内**，本项目**明确不调用**（避免触碰账号数据与写操作）。
因此本项目能真实验证的只有"门店可用券"，账户券/可领取券只做**接口层面的说明**，
不获取、不伪造。

匹配规则（硬性）：**只有**当组合中的商品编码命中券的 ``products[].productCode``
（或券未限定商品）时，才认为该券对组合适用；**绝不把不适用的券套用到商品上**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .models import Coupon

__all__ = [
    "CouponParseError",
    "CouponMatch",
    "parse_coupons",
    "parse_trade_window",
    "coupons_for_product",
    "match_coupons",
    "best_coupon",
    "active_coupons",
    "coupons_in_menu",
    "COUPON_SOURCES",
]

#: 券来源说明（用于文档/报告的如实表述）
COUPON_SOURCES: dict[str, str] = {
    "store": "门店可用券（query-store-coupons，本项目真实调用）",
    "account": "账户已有券（query-my-coupons，账号级接口，本项目不调用）",
    "claimable": "可领取券（auto-bind-coupons，写操作，本项目不调用）",
}


class CouponParseError(ValueError):
    """券结构不符合预期。"""


def parse_trade_window(trade_date_time: str) -> tuple[Optional[str], Optional[str]]:
    """解析 ``tradeDateTime`` 为 ``(生效时间, 失效时间)``。

    真实格式形如 ``2026-10-05 10:30:00-2026-10-09 23:59:59``（两段用 ``-`` 连接）。
    解析不出就返回 ``(None, None)``——**不猜测**有效期。
    """
    s = str(trade_date_time or "").strip()
    if not s:
        return None, None
    # 时间内部本身含 "-"（日期分隔），因此按 "-YYYY" 边界切分更稳妥
    parts = s.split(" - ")
    if len(parts) != 2:
        # 回退：用正则匹配 "日期时间-日期时间"
        import re

        m = re.match(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            s,
        )
        if not m:
            return None, None
        parts = [m.group(1), m.group(2)]

    def _ok(v: str) -> Optional[str]:
        try:
            datetime.strptime(v.strip(), "%Y-%m-%d %H:%M:%S")
            return v.strip()
        except ValueError:
            return None

    a, b = _ok(parts[0]), _ok(parts[1])
    if a and b:
        return a, b
    return (a or None), (b or None)


def parse_coupons(payload: dict[str, Any]) -> list[Coupon]:
    """解析 ``query-store-coupons`` 返回为 :class:`Coupon` 列表。"""
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise CouponParseError(f"query-store-coupons 返回失败：{payload.get('message')}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise CouponParseError("query-store-coupons.data 应为数组。")

    coupons: list[Coupon] = []
    for row in data:
        products = row.get("products") or []
        tdt = str(row.get("tradeDateTime", ""))
        vf, vt = parse_trade_window(tdt)
        coupons.append(
            Coupon(
                coupon_id=str(row.get("couponId", "")),
                coupon_code=str(row.get("couponCode", "")),
                title=str(row.get("title", "")),
                trade_date_time=tdt,
                product_codes=[str(p.get("productCode", "")) for p in products if p.get("productCode")],
                product_names=[str(p.get("productName", "")) for p in products],
                promotion_id=str(row.get("promotionId", "")),
                source="store",
                valid_from=vf,
                valid_to=vt,
            )
        )
    return coupons


@dataclass
class CouponMatch:
    """券与组合的匹配结果。"""

    coupon: Coupon
    applicable: bool = False
    matched_codes: list[str] = field(default_factory=list)
    reason: str = ""
    time_status: str = "unknown"  # active / not_started / expired / unknown

    @property
    def usable(self) -> bool:
        """既适用（编码命中）又未过期/未生效才算可用。"""
        return self.applicable and self.time_status in ("active", "unknown")


def coupons_for_product(product_code: str, coupons: list[Coupon]) -> list[Coupon]:
    """返回可作用于指定商品编码的券（未限定商品的券视为通用券）。"""
    code = str(product_code)
    out: list[Coupon] = []
    for c in coupons:
        if not c.product_codes:
            out.append(c)  # 未限定商品 → 视为通用
        elif code in c.product_codes:
            out.append(c)
    return out


def active_coupons(coupons: list[Coupon], now: Optional[datetime] = None) -> list[Coupon]:
    """过滤掉已过期 / 尚未生效的券（无法判定时效的保守保留）。"""
    return [c for c in coupons if c.status(now) in ("active", "unknown")]


def coupons_in_menu(coupons: list[Coupon], menu_codes: set[str]) -> list[Coupon]:
    """返回"适用商品在本店在售菜单中"的券（即真正可能用上的券）。"""
    out: list[Coupon] = []
    for c in coupons:
        if not c.product_codes:
            out.append(c)  # 通用券
        elif any(code in menu_codes for code in c.product_codes):
            out.append(c)
    return out


def match_coupons(
    combo_codes: list[str],
    coupons: list[Coupon],
    now: Optional[datetime] = None,
) -> list[CouponMatch]:
    """计算每张券对给定组合是否适用（含时效判定）。"""
    codes = {str(c) for c in combo_codes}
    matches: list[CouponMatch] = []
    for c in coupons:
        st = c.status(now)
        if not c.product_codes:
            matches.append(
                CouponMatch(coupon=c, applicable=True, reason="该券未限定商品，对组合通用。", time_status=st)
            )
            continue
        hit = sorted(codes & set(c.product_codes))
        if hit:
            reason = f"命中组合商品编码：{', '.join(hit)}。"
            if st == "expired":
                reason += "（但该券已过期）"
            elif st == "not_started":
                reason += "（但该券尚未生效）"
            matches.append(CouponMatch(coupon=c, applicable=True, matched_codes=hit, reason=reason, time_status=st))
        else:
            matches.append(
                CouponMatch(
                    coupon=c,
                    applicable=False,
                    reason="适用商品与组合不匹配，不可用于本组合。",
                    time_status=st,
                )
            )
    return matches


def best_coupon(matches: list[CouponMatch]) -> Optional[CouponMatch]:
    """在**可用**（适用且未过期）的券中返回其一。

    说明：具体省额必须以 ``calculate-price`` 的真实返回为准，本函数不估算金额。
    """
    for m in matches:
        if m.usable:
            return m
    return None
