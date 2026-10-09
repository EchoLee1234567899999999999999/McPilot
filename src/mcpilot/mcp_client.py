"""MCP 只读客户端（Phase 1 实现）。

本模块提供两种**真实可运行**的调用方式，二者产出完全一致：

A. **Python 直连 MCP**（本模块 :class:`HttpToolInvoker`）
   通过麦当劳 MCP 的 streamable HTTP 端点（JSON-RPC 2.0）直接调用工具。
   凭据在运行时由 :mod:`mcpilot.config` 解析，**不落盘、不打印**。

B. **由 Skill 调度 MCP**（WorkBuddy 运行时）
   WorkBuddy 智能体通过内置工具（``mcp__mcd-mcp__<tool>``）调用同样的 6 个工具，
   再把返回交给本包的服务层做解析/匹配。此路径同样调用真实 MCP，
   ``McpClient`` 可通过注入一个"转发给智能体的 invoker"复用全部逻辑。

两条路径的关系与取舍见 ``docs/02-architecture.md`` 第 9 节。

安全约束：
- 仅暴露只读工具；写操作（下单/领券/抽奖/积分/地址写入）在本模块不可达。
- 不读取、不记录、不写入任何 Token / Authorization / 账号信息。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Literal, Optional, Protocol

from .config import McpEndpoint, resolve_endpoint

BeType = Literal[1, 2, 5, 6]
OrderType = Literal[1, 2]

# ---------------------------------------------------------------------------
# 工具白名单 / 黑名单
# ---------------------------------------------------------------------------

ALLOWED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "query-nearby-stores",
        "query-meals",
        "query-meal-detail",
        "list-nutrition-foods",
        "query-store-coupons",
        "calculate-price",
    }
)

FORBIDDEN_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create-order",
        "mall-create-order",
        "party-order-create",
        "cancel-order",
        "auto-bind-coupons",
        "draw-lottery",
        "mall-points-products",
        "delivery-create-address",
        "query-survey-coupon",
        "delivery-query-addresses",
        "query-my-account",
        "query-my-coupons",
        "query-my-prizes",
        "query-order",
        "order-list",
        "mall-order-list",
        "mall-order-detail",
        "mall-product-detail",
        "query-lottery-info",
    }
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class McpError(RuntimeError):
    """MCP 调用相关错误的基类。"""


class McpConfigMissingError(McpError):
    """未解析到 MCP 接入配置。"""


class McpTransportError(McpError):
    """网络/传输层错误（超时、连接失败、HTTP 非 2xx）。"""


class McpToolError(McpError):
    """MCP 工具返回业务错误（isError=true 或 success=false）。"""


class McpForbiddenToolError(McpError):
    """试图调用白名单外的工具（尤其是写操作）。"""


class McpParamError(ValueError):
    """场景参数不满足工具契约。"""


# ---------------------------------------------------------------------------
# 参数规则
# ---------------------------------------------------------------------------


def order_type_for(be_type: BeType) -> OrderType:
    """按 beType 推导 orderType：1/5 → 1；2/6 → 2。"""
    if be_type in (1, 5):
        return 1
    if be_type in (2, 6):
        return 2
    raise McpParamError(f"非法 beType: {be_type!r}（应为 1/2/5/6）")


def validate_scene_params(be_type: BeType, be_code: Optional[str]) -> None:
    """校验场景参数规则（真实工具契约）：

    - 到店自取(beType=1)：**不得**传 be_code（传了会报错）。
    - 得来速(beType=5)：**必须**传 be_code。
    - 外送(2)/团餐(6)：必须传 be_code。
    """
    has_code = bool(be_code and str(be_code).strip())
    if be_type == 1 and has_code:
        raise McpParamError("到店自取(beType=1)不能传 beCode：请置空。")
    if be_type in (5, 2, 6) and not has_code:
        raise McpParamError(f"beType={be_type} 必须提供 beCode（来自门店查询结果）。")


def build_scene_args(
    be_type: BeType,
    be_code: Optional[str] = None,
    reservation_date: Optional[str] = None,
) -> dict[str, Any]:
    """构造含场景参数（orderType / beType / beCode / reservationDate）的公共参数。

    严格遵循真实契约：自取不传 beCode；预约才传 reservationDate。
    """
    validate_scene_params(be_type, be_code)
    args: dict[str, Any] = {"beType": be_type, "orderType": order_type_for(be_type)}
    if be_code and str(be_code).strip():
        args["beCode"] = str(be_code)
    if reservation_date:
        args["reservationDate"] = reservation_date
    return args


# ---------------------------------------------------------------------------
# 响应解析：MCP 工具把业务 JSON 嵌在 markdown 文本里
# ---------------------------------------------------------------------------


def extract_json_payload(text: str) -> dict[str, Any]:
    """从工具返回文本中提取业务 JSON 对象。

    麦当劳 MCP 的 ``tools/call`` 返回形如:

        # 说明...
        ## Original Response
        {"success":true,"code":200,...,"data":...}
        （可能还有后续文字）

    采用**括号配对**扫描以稳定截取完整 JSON，避免依赖固定小标题。
    """
    start = text.find('{"success"')
    if start < 0:
        start = text.find("{")
    if start < 0:
        raise McpToolError("响应中未找到 JSON 负载。")

    depth = 0
    in_str = False
    esc = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : idx + 1])
    raise McpToolError("响应 JSON 不完整（括号未闭合）。")


# ---------------------------------------------------------------------------
# 传输层：streamable HTTP + JSON-RPC 2.0
# ---------------------------------------------------------------------------


class ToolInvoker(Protocol):
    """工具调用器协议：``(tool_name, arguments) -> 业务 JSON``。"""

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class HttpToolInvoker:
    """直连麦当劳 MCP（streamable HTTP）的低层调用器。

    每次调用执行 ``initialize`` 握手 + ``tools/call``：
    该端点为无状态设计，无需维护 session id。

    可靠性（Phase 3）：
    - 显式 **超时**（``timeout``，默认 40s）；
    - 对**传输层**错误（超时 / 连接失败 / HTTP 429 / 5xx）做**限次重试**，
      指数退避（``backoff`` 秒起，最多 ``max_retries`` 次）；
    - 只读工具是幂等的，重试安全；**绝不**重试 4xx 这类确定性客户端错误。
    """

    def __init__(
        self,
        endpoint: McpEndpoint,
        timeout: float = 40.0,
        *,
        max_retries: int = 2,
        backoff: float = 0.6,
    ) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._backoff = max(0.0, float(backoff))
        self._rpc_id = 0
        self.retry_count = 0  # 累计重试次数（用于报告/测试）

    # -- 内部 ---------------------------------------------------------------
    def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self._endpoint.headers)
        req = urllib.request.Request(
            self._endpoint.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # 不把响应头/凭据带入异常
            if exc.code in (429, 500, 502, 503, 504):
                raise McpTransportError(f"MCP HTTP 错误：{exc.code}") from None
            raise McpToolError(f"MCP HTTP 错误：{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpTransportError(f"MCP 网络错误：{type(exc).__name__}") from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise McpTransportError("MCP 返回非 JSON 内容。") from None

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """带限次重试的 POST（仅对传输层错误重试）。"""
        attempt = 0
        while True:
            try:
                return self._post_once(payload)
            except McpTransportError:
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                self.retry_count += 1
                if self._backoff:
                    time.sleep(self._backoff * attempt)

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _initialize(self) -> None:
        self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcpilot", "version": "0.1"},
                },
            }
        )

    # -- 公共 ---------------------------------------------------------------
    def list_tools(self) -> list[str]:
        """返回服务端暴露的工具名列表（用于联通性检查）。"""
        self._initialize()
        resp = self._post({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}})
        tools = (resp.get("result") or {}).get("tools") or []
        return [t.get("name", "") for t in tools]

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name in FORBIDDEN_WRITE_TOOLS:
            raise McpForbiddenToolError(f"禁止调用写操作工具：{tool_name}")
        if tool_name not in ALLOWED_READ_TOOLS:
            raise McpForbiddenToolError(f"工具不在只读白名单内：{tool_name}")

        self._initialize()
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
        if "error" in resp:
            msg = (resp["error"] or {}).get("message", "unknown")
            raise McpToolError(f"工具 {tool_name} 调用失败：{msg}")

        result = resp.get("result") or {}
        if result.get("isError"):
            raise McpToolError(f"工具 {tool_name} 返回 isError=true")

        # 优先使用结构化内容，否则从文本中提取 JSON
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        for block in result.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                return extract_json_payload(block["text"])
        raise McpToolError(f"工具 {tool_name} 未返回可解析内容。")


# ---------------------------------------------------------------------------
# 业务客户端
# ---------------------------------------------------------------------------


class McpClient:
    """麦当劳 MCP 只读客户端（6 个工具）。

    通过依赖注入接收 :class:`ToolInvoker`，便于测试注入桩实现；
    默认使用直连的 :class:`HttpToolInvoker`。
    """

    def __init__(
        self,
        invoker: ToolInvoker | None = None,
        *,
        endpoint: McpEndpoint | None = None,
        timeout: float = 40.0,
        max_retries: int = 2,
        backoff: float = 0.6,
    ) -> None:
        if invoker is None:
            try:
                ep = endpoint or resolve_endpoint()
            except Exception as exc:  # 归一化配置错误
                raise McpConfigMissingError(str(exc)) from None
            self._invoke: ToolInvoker = HttpToolInvoker(
                ep, timeout=timeout, max_retries=max_retries, backoff=backoff
            )
        else:
            self._invoke = invoker

    # -- 白名单守卫（无论注入何种调用器都生效）-------------------------------
    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用前强制白名单校验，再委托给底层调用器。

        写操作与未知工具一律拒绝——防线在客户端层，即使注入自定义调用器也无法绕过。
        """
        if tool_name in FORBIDDEN_WRITE_TOOLS:
            raise McpForbiddenToolError(f"禁止调用写操作工具：{tool_name}")
        if tool_name not in ALLOWED_READ_TOOLS:
            raise McpForbiddenToolError(f"工具不在只读白名单内：{tool_name}")
        return self._invoke(tool_name, arguments)

    # -- 1. 门店 -----------------------------------------------------------
    def query_nearby_stores(
        self,
        *,
        be_type: BeType,
        search_type: int = 1,
        city: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> dict[str, Any]:
        """查询可点餐门店。search_type=2 时 city/keyword 必填。"""
        args: dict[str, Any] = {"beType": be_type, "searchType": search_type}
        if search_type == 2:
            if not city or not keyword:
                raise McpParamError("searchType=2 时必须提供 city 与 keyword。")
            args["city"] = city
            args["keyword"] = keyword
        return self.call_tool("query-nearby-stores", args)

    # -- 2. 菜单 -----------------------------------------------------------
    def query_meals(
        self,
        *,
        store_code: str,
        be_type: BeType,
        be_code: Optional[str] = None,
        reservation_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """查询门店在售餐品 / 套餐列表。"""
        args = {"storeCode": str(store_code), **build_scene_args(be_type, be_code, reservation_date)}
        return self.call_tool("query-meals", args)

    # -- 3. 餐品详情 -------------------------------------------------------
    def query_meal_detail(
        self,
        *,
        store_code: str,
        code: str,
        be_type: BeType,
        be_code: Optional[str] = None,
        reservation_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """查询餐品详情与可特调项。"""
        args = {
            "storeCode": str(store_code),
            "code": str(code),
            **build_scene_args(be_type, be_code, reservation_date),
        }
        return self.call_tool("query-meal-detail", args)

    # -- 4. 营养 -----------------------------------------------------------
    def list_nutrition_foods(self) -> dict[str, Any]:
        """获取营养数据（无入参）。"""
        return self.call_tool("list-nutrition-foods", {})

    # -- 5. 优惠券 ---------------------------------------------------------
    def query_store_coupons(
        self,
        *,
        store_code: str,
        be_type: BeType,
        be_code: Optional[str] = None,
        reservation_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """查询门店 + 订单类型下可用优惠券。"""
        args = {"storeCode": str(store_code), **build_scene_args(be_type, be_code, reservation_date)}
        return self.call_tool("query-store-coupons", args)

    # -- 6. 价格试算 -------------------------------------------------------
    def calculate_price(
        self,
        *,
        store_code: str,
        be_type: BeType,
        items: list[dict[str, Any]],
        be_code: Optional[str] = None,
        need_tableware: Optional[bool] = None,
        reservation_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """价格试算（含优惠）。返回金额单位为**分**。"""
        if not items:
            raise McpParamError("items 不能为空。")
        for it in items:
            if not it.get("productCode"):
                raise McpParamError("每个 item 必须包含 productCode。")
            if int(it.get("quantity", 0)) < 1:
                raise McpParamError("每个 item 的 quantity 必须 ≥ 1。")
        args: dict[str, Any] = {
            "storeCode": str(store_code),
            "items": items,
            **build_scene_args(be_type, be_code, reservation_date),
        }
        if need_tableware is not None:
            args["needTableware"] = need_tableware
        return self.call_tool("calculate-price", args)


def make_client(
    invoker: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> McpClient:
    """便捷工厂：``make_client()`` 使用直连；传入调用器则复用其逻辑。"""
    return McpClient(invoker=invoker)
