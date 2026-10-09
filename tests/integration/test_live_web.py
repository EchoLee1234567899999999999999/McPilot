"""真实 MCP 集成测试 · Phase 4 Web 后端。

仅在 ``MCPILOT_LIVE=1`` 时运行。会：
1. 启动真实 Web 服务（**不注入替身**，使用真实 ``McpClient``）；
2. 通过 HTTP 走完整流程：健康检查 → 门店查询 → 创建推荐任务 → SSE 进度 → 读取结果。

只断言**结构与规则**（价格/营养来自真实接口，数值本身可变，不做硬编码断言）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.client import HTTPConnection
from typing import Any, Iterator

import pytest

from mcpilot.web.app import create_server

LIVE = os.environ.get("MCPILOT_LIVE") == "1"
STORE = "3330324"
pytestmark = pytest.mark.skipif(not LIVE, reason="真实 MCP 集成测试需设置 MCPILOT_LIVE=1")


@pytest.fixture(scope="module")
def live_server() -> Iterator[dict[str, Any]]:
    httpd = create_server(host="127.0.0.1", port=0, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.05)
    try:
        yield {"host": "127.0.0.1", "port": port}
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(srv: dict[str, Any], path: str) -> tuple[int, bytes]:
    conn = HTTPConnection(srv["host"], srv["port"], timeout=120)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def _post(srv: dict[str, Any], path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    conn = HTTPConnection(srv["host"], srv["port"], timeout=120)
    try:
        conn.request(
            "POST", path, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def _read_sse(srv: dict[str, Any], path: str, deadline: float = 120.0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    conn = HTTPConnection(srv["host"], srv["port"], timeout=deadline)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        buf = b""
        start = time.time()
        while time.time() - start < deadline:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
                if events and events[-1].get("type") in ("result", "error"):
                    return events
        return events
    finally:
        conn.close()


def test_live_health_and_config(live_server):
    st, body = _get(live_server, "/api/health")
    assert st == 200 and json.loads(body)["ok"] is True
    st, body = _get(live_server, "/api/config")
    cfg = json.loads(body)
    assert cfg["ok"] is True
    # 真实环境下应已配置 MCP；无论如何都不得泄漏凭据
    assert "Bearer" not in body.decode("utf-8")


def test_live_store_query(live_server):
    st, body = _get(live_server, "/api/stores?city=%E6%B7%B1%E5%9C%B3%E5%B8%82&keyword=%E7%A7%91%E6%8A%80%E5%9B%AD")
    assert st == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert len(data["stores"]) >= 1
    assert all("store_code" in s and "name" in s for s in data["stores"])


def test_live_recommend_end_to_end(live_server):
    """真实 ¥30 预算：SSE 收到真实阶段事件，最终返回真实方案。"""
    st, body = _post(
        live_server, "/api/recommend", {"store_code": STORE, "be_type": 1, "budget": 30, "people": 1}
    )
    assert st == 202
    job_id = json.loads(body)["job_id"]
    assert job_id

    events = _read_sse(live_server, f"/api/jobs/{job_id}/events", deadline=120)
    assert any(e.get("type") == "result" for e in events), f"未收到结果事件：{events[-3:]}"

    # 真实阶段必须出现
    stages = {e.get("stage") for e in events if e.get("stage")}
    for expect in ("menu", "nutrition", "coupon", "verify"):
        assert expect in stages, f"缺少阶段 {expect}，实际 {sorted(stages)}"

    _, snap_body = _get(live_server, f"/api/jobs/{job_id}")
    snap = json.loads(snap_body)
    assert snap["status"] == "done"
    plan = snap["result"]
    assert plan["store_code"] == STORE
    assert plan["stats"]["mcp_calls"]["query-meals"] == 1
    assert plan["stats"]["verified"] <= 12  # 调用有界

    if plan["recommendations"]:
        rec = plan["recommendations"][0]
        assert rec["price"]["payable_yuan"] <= 30.0 + 1e-6  # 绝不超预算
        assert rec["items"]
        for it in rec["items"]:
            # 有图则必须是官方图片 URL；无图则为空串（不编造）
            assert it["image"] == "" or it["image"].startswith("http")
