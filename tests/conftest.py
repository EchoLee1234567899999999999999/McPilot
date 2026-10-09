"""pytest 公共配置与夹具。

- 将 ``src/`` 加入 ``sys.path``，使测试可直接 ``import mcpilot``；
- 提供基于**真实采集响应**的夹具（见 ``tests/fixtures/README.md``），
  以及一个离线 ``FixtureInvoker``，让 ``McpClient`` 在无网络时可被完整驱动。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureInvoker:
    """离线工具调用器：按工具名返回真实采集的夹具负载。

    仅用于**离线回归**（解析/匹配逻辑）；真实调用见 ``tests/integration``。
    """

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail_on = fail_on

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from mcpilot.mcp_client import McpToolError, McpTransportError

        self.calls.append((tool_name, arguments))
        if self._fail_on == tool_name:
            raise McpTransportError(f"模拟传输失败：{tool_name}")
        mapping = {
            "query-meals": "menu_3330324.json",
            "list-nutrition-foods": "nutrition.json",
            "query-store-coupons": "coupons_3330324.redacted.json",
            "query-meal-detail": "detail_1100.json",
            "calculate-price": "price_1100.json",
        }
        if tool_name not in mapping:
            raise McpToolError(f"夹具未覆盖工具：{tool_name}")
        return load_fixture(mapping[tool_name])


@pytest.fixture
def menu_payload() -> dict[str, Any]:
    return load_fixture("menu_3330324.json")


@pytest.fixture
def nutrition_payload() -> dict[str, Any]:
    return load_fixture("nutrition.json")


@pytest.fixture
def coupons_payload() -> dict[str, Any]:
    return load_fixture("coupons_3330324.redacted.json")


@pytest.fixture
def price_payload() -> dict[str, Any]:
    return load_fixture("price_1100.json")


@pytest.fixture
def detail_payload() -> dict[str, Any]:
    return load_fixture("detail_1100.json")


@pytest.fixture
def fixture_invoker() -> FixtureInvoker:
    return FixtureInvoker()


# ---------------------------------------------------------------------------
# Phase 2：推荐引擎的离线"计算价"测试替身
# ---------------------------------------------------------------------------


class RecommenderInvoker:
    """推荐引擎离线调用器（**测试替身**，非真实查询）。

    用途：在无网络时确定性地驱动 :func:`mcpilot.recommender.recommend` 的完整逻辑。

    - ``query-meals`` / ``list-nutrition-foods`` / ``query-store-coupons``：
      返回 ``tests/fixtures`` 下**真实采集**的负载（券可选真实或合成）。
    - ``calculate-price``：**按菜单夹具中的真实展示价**推导金额（分），并在
      券命中行按固定规则减免，模拟 MCP 的价格试算契约。

    因此金额本身基于真实展示价，但**由替身计算而非 MCP 返回**；
    真实试算务必以 ``tests/integration`` 的实时测试为准。
    """

    #: 合成券每命中行优惠（分）——与 Phase 1 实测到的「麦旋风 ¥14→¥9.9」量级一致
    COUPON_DISCOUNT_CENT = 410

    def __init__(
        self,
        *,
        coupons_fixture: str = "coupons_3330324.redacted.json",
        menu_fixture: str = "menu_3330324.json",
        nutrition_fixture: str = "nutrition.json",
        menu_payload: dict[str, Any] | None = None,
        nutrition_payload: dict[str, Any] | None = None,
        removed_codes: Iterable[str] = (),
        fail_on: str | None = None,
        detail_fixtures: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.menu_payload = menu_payload if menu_payload is not None else load_fixture(menu_fixture)
        self.nutrition_payload = (
            nutrition_payload if nutrition_payload is not None else load_fixture(nutrition_fixture)
        )
        self.coupons_payload = load_fixture(coupons_fixture)
        self._fail_on = fail_on
        self._removed = set(removed_codes)
        # 可选：为 query-meal-detail 提供夹具（code -> 夹具文件名）。
        # 默认 None 表示**不提供**，此时 query-meal-detail 会报错 →
        # 营养解析保持"不猜测"，与 Phase 2 行为一致。
        self._detail_fixtures = detail_fixtures
        self._unit_cent, self._name = self._build_menu_index()

    def _build_menu_index(self) -> tuple[dict[str, int], dict[str, str]]:
        from mcpilot.menu import parse_menu

        unit: dict[str, int] = {}
        name: dict[str, str] = {}
        for it in parse_menu(self.menu_payload).items:
            if it.code in self._removed:
                continue
            if it.price_yuan is not None:
                unit[it.code] = int(round(it.price_yuan * 100))
            name[it.code] = it.name
        return unit, name

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from mcpilot.mcp_client import McpToolError, McpTransportError

        self.calls.append((tool_name, arguments))
        if self._fail_on == tool_name:
            raise McpTransportError(f"模拟传输失败：{tool_name}")

        if tool_name == "query-meals":
            return self.menu_payload
        if tool_name == "list-nutrition-foods":
            return self.nutrition_payload
        if tool_name == "query-store-coupons":
            return self.coupons_payload
        if tool_name == "query-meal-detail":
            code = str(arguments.get("code", ""))
            if self._detail_fixtures and code in self._detail_fixtures:
                return load_fixture(f"detail/{self._detail_fixtures[code]}")
            raise McpToolError("测试替身未配置该商品的 detail 夹具")
        if tool_name == "calculate-price":
            return self._calculate(arguments)
        raise McpToolError(f"测试替身未覆盖工具：{tool_name}")

    def _calculate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        lines: list[dict[str, Any]] = []
        orig_total = 0
        disc_total = 0
        for it in arguments.get("items") or []:
            code = str(it["productCode"])
            qty = int(it.get("quantity", 1))
            if code not in self._unit_cent:
                # 商品下架 / 不在售：模拟服务端拒绝（不返回编造价格）
                raise McpToolError(f"商品 {code} 当前不可售，试算失败。")
            unit = self._unit_cent[code]
            disc = self.COUPON_DISCOUNT_CENT if it.get("couponId") else 0
            lines.append(
                {
                    "productCode": code,
                    "productName": self._name.get(code, ""),
                    "quantity": qty,
                    "originalSubtotal": unit * qty,
                    "subtotal": unit * qty - disc,
                }
            )
            orig_total += unit * qty
            disc_total += disc
        return {
            "success": True,
            "datetime": "2026-10-09 17:45:00",
            "data": {
                "productOriginalPrice": orig_total,
                "productPrice": orig_total,
                "originalPrice": orig_total,
                "discount": disc_total,
                "price": orig_total - disc_total,
                "productList": lines,
            },
        }


@pytest.fixture
def recommender_invoker() -> RecommenderInvoker:
    return RecommenderInvoker()
