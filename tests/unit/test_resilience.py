"""性能与稳定性（Phase 3）：超时/限次重试、调用上限、无缓存冒充实时、降级提示。"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from mcpilot.config import McpEndpoint
from mcpilot.mcp_client import (
    HttpToolInvoker,
    McpClient,
    McpToolError,
    McpTransportError,
)
from mcpilot.models import UserRequest
from mcpilot.recommender import recommend
from tests.conftest import RecommenderInvoker


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- 重试 -----------------------------------------------------------------


def test_transport_error_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.URLError("boom")
        return _FakeResp(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    inv = HttpToolInvoker(McpEndpoint(url="https://example.invalid", headers={}), max_retries=2, backoff=0.0)
    out = inv._post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert out["result"]["ok"] is True
    assert calls["n"] == 3  # 2 次失败 + 1 次成功
    assert inv.retry_count == 2


def test_transport_error_exhausts_retries_and_raises(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("always down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    inv = HttpToolInvoker(McpEndpoint(url="https://example.invalid", headers={}), max_retries=1, backoff=0.0)
    with pytest.raises(McpTransportError):
        inv._post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert calls["n"] == 2  # 首次 + 1 次重试
    assert inv.retry_count == 1


def test_http_500_is_transport_error_but_400_is_not_retried(monkeypatch):
    def five_hundred(req, timeout=None):
        raise urllib.error.HTTPError("u", 503, "svc unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", five_hundred)
    inv = HttpToolInvoker(McpEndpoint(url="https://example.invalid", headers={}), max_retries=0, backoff=0.0)
    with pytest.raises(McpTransportError):
        inv._post({"jsonrpc": "2.0", "id": 1})

    def four_hundred(req, timeout=None):
        raise urllib.error.HTTPError("u", 400, "bad request", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", four_hundred)
    inv2 = HttpToolInvoker(McpEndpoint(url="https://example.invalid", headers={}), max_retries=3, backoff=0.0)
    with pytest.raises(McpToolError):  # 4xx 属确定性错误，不重试、直接抛业务错误
        inv2._post({"jsonrpc": "2.0", "id": 1})
    assert inv2.retry_count == 0


# --- 调用上限 -------------------------------------------------------------


def test_verify_and_resolve_call_caps_are_enforced():
    invoker = RecommenderInvoker()
    req = UserRequest(store_code="3330324", be_type=1, budget=30, max_verify=5, max_resolve=3)
    plan = recommend(req, client=McpClient(invoker=invoker))
    assert plan.stats["mcp_calls"]["calculate-price"] <= 5
    assert plan.stats["mcp_calls"]["query-meal-detail"] <= 3
    assert plan.stats["verified"] <= 5


def test_pure_read_tools_only_even_with_detail():
    invoker = RecommenderInvoker()
    recommend(UserRequest(store_code="3330324", be_type=1, budget=30), client=McpClient(invoker=invoker))
    allowed = {
        "query-meals",
        "list-nutrition-foods",
        "query-store-coupons",
        "query-meal-detail",
        "calculate-price",
    }
    assert {c[0] for c in invoker.calls} <= allowed


# --- 不缓存价格（每轮都取实时数据）----------------------------------------


def test_no_cross_run_cache_each_run_refetches():
    invoker = RecommenderInvoker()
    req = UserRequest(store_code="3330324", be_type=1, budget=30)
    recommend(req, client=McpClient(invoker=invoker))
    recommend(req, client=McpClient(invoker=invoker))
    names = [c[0] for c in invoker.calls]
    # 每轮都必须重新查询菜单/营养/券，不能用上一轮结果冒充实时
    assert names.count("query-meals") == 2
    assert names.count("list-nutrition-foods") == 2
    assert names.count("query-store-coupons") == 2


# --- 降级提示 -------------------------------------------------------------


def test_empty_menu_raises_clear_error():
    payload = {"success": True, "data": {"categories": [], "meals": {}}}
    invoker = RecommenderInvoker(menu_payload=payload)
    with pytest.raises(ValueError) as exc:
        recommend(UserRequest(store_code="3330324", be_type=1, budget=30), client=McpClient(invoker=invoker))
    assert "未返回任何在售餐品" in str(exc.value)


def test_no_menu_on_transport_failure_raises_not_faked():
    invoker = RecommenderInvoker(fail_on="query-meals")
    with pytest.raises(McpTransportError):
        recommend(UserRequest(store_code="3330324", be_type=1, budget=30), client=McpClient(invoker=invoker))


def test_no_coupon_warning_is_explicit():
    invoker = RecommenderInvoker()
    plan = recommend(UserRequest(store_code="3330324", be_type=1, budget=30), client=McpClient(invoker=invoker))
    assert any("券来源说明" in w for w in plan.warnings)
