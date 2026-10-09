"""Phase 4：Web 后端 API 与静态资源的离线测试。

特点
----
- **零第三方依赖**：仅用标准库 ``http.client`` / ``threading`` 启动被测服务。
- 使用 `:class:`WebInvoker``（**测试替身**）驱动 ``McpClient``，因此无需网络；
  真实 MCP 端到端验证见 ``tests/integration``。
- 重点断言：接口契约、错误分类、SSE 真实进度事件、**凭据绝不泄漏到响应**。
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from typing import Any, Iterator

import pytest

from mcpilot.mcp_client import McpClient, McpConfigMissingError, McpTransportError
from mcpilot.web.app import _request_from_payload, create_server

from tests.conftest import RecommenderInvoker

# ---------------------------------------------------------------------------
# 测试替身：扩展推荐替身，补上 query-nearby-stores
# ---------------------------------------------------------------------------


class WebInvoker(RecommenderInvoker):
    """在推荐替身基础上补 ``query-nearby-stores``，供 Web 层离线使用。"""

    def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "query-nearby-stores":
            self.calls.append((tool_name, arguments))
            city = arguments.get("city", "")
            keyword = arguments.get("keyword", "")
            return {
                "success": True,
                "datetime": "2026-10-09 18:00:00",
                "data": [
                    {
                        "storeCode": "3330324",
                        "storeName": f"麦当劳{keyword}餐厅",
                        "address": f"{city}{keyword}某路 1 号",
                        "distance": 403,
                        "businessStatus": True,
                        "businessStartTime": "07:00",
                        "businessEndTime": "23:00",
                        "reservation": True,
                    }
                ],
            }
        return super().__call__(tool_name, arguments)


def _web_client(invoker: Any) -> McpClient:
    return McpClient(invoker=invoker)


# ---------------------------------------------------------------------------
# 服务夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[dict[str, Any]]:
    """启动被测服务（临时端口），返回连接信息与关闭句柄。"""
    invoker = WebInvoker()
    httpd = create_server(host="127.0.0.1", port=0, client_factory=lambda: _web_client(invoker), quiet=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    try:
        yield {"host": "127.0.0.1", "port": port, "httpd": httpd, "invoker": invoker}
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(server: dict[str, Any], path: str) -> tuple[int, dict[str, str], bytes]:
    conn = HTTPConnection(server["host"], server["port"], timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def _post(server: dict[str, Any], path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
    conn = HTTPConnection(server["host"], server["port"], timeout=10)
    try:
        raw = json.dumps(payload).encode("utf-8")
        conn.request("POST", path, body=raw, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _read_sse(base_host: str, port: int, path: str, *, deadline: float = 20.0) -> list[dict[str, Any]]:
    """读取 SSE 直到收到 result/error 事件或超时。返回解析后的事件列表。"""
    events: list[dict[str, Any]] = []
    conn = HTTPConnection(base_host, port, timeout=deadline)
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


# ---------------------------------------------------------------------------
# 1. 基础接口
# ---------------------------------------------------------------------------


def test_health_endpoint(server):
    status, headers, body = _get(server, "/api/health")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["service"] == "mcpilot-web"
    assert headers["Content-Type"].startswith("application/json")


def test_unknown_api_returns_404(server):
    status, _, body = _get(server, "/api/nope")
    assert status == 404
    assert json.loads(body)["ok"] is False


def test_config_endpoint_never_leaks_token(server, monkeypatch):
    """配置接口只能暴露"是否已配置"，**绝不能**回显令牌。"""
    secret = "SUPER_SECRET_TOKEN_ABC123"
    from mcpilot import config as cfg

    class _EP:
        url = "https://mcp.mcd.cn"
        headers = {"Authorization": f"Bearer {secret}"}

    monkeypatch.setattr("mcpilot.web.app.resolve_endpoint", lambda: _EP())
    status, _, body = _get(server, "/api/config")
    assert status == 200
    text = body.decode("utf-8")
    assert secret not in text
    assert "Bearer" not in text
    data = json.loads(text)
    assert data["mcp_configured"] is True
    assert data["endpoint_host"] == "mcp.mcd.cn"
    assert data["auth_mode"] == "header"


def test_config_reports_not_configured(server, monkeypatch):
    def _boom():
        raise McpConfigMissingError("未找到 MCP 配置")

    monkeypatch.setattr("mcpilot.web.app.resolve_endpoint", _boom)
    status, _, body = _get(server, "/api/config")
    assert status == 200
    data = json.loads(body)
    assert data["mcp_configured"] is False
    assert "令牌" not in json.dumps(data, ensure_ascii=False)  # 仅描述，不含凭据


# ---------------------------------------------------------------------------
# 2. 静态资源
# ---------------------------------------------------------------------------


def test_index_and_assets_served(server):
    for path, needle in (
        ("/", b"McPilot"),
        ("/index.html", b"\xe9\xba\xa6\xe9\x97\xa8\xe5\x86\xb3\xe7\xad\x96\xe5\xb1\x80"),  # 麦门决策局
        ("/styles.css", b"--teal"),
        ("/app.js", b"/api/recommend"),
    ):
        status, headers, body = _get(server, path)
        assert status == 200, path
        assert needle in body, path
        assert "text/html" in headers["Content-Type"] or "css" in headers["Content-Type"] or "javascript" in headers["Content-Type"]


def test_spa_fallback_for_unknown_route(server):
    status, _, body = _get(server, "/some/deep/route")
    assert status == 200
    assert b"McPilot" in body


def test_path_traversal_blocked(server):
    status, _, body = _get(server, "/../src/mcpilot/app.py")
    assert status in (403, 404)
    assert b"def " not in body


# ---------------------------------------------------------------------------
# 3. 门店接口
# ---------------------------------------------------------------------------


def test_stores_endpoint_returns_real_shape(server):
    status, _, body = _get(server, "/api/stores?city=%E6%B7%B1%E5%9C%B3%E5%B8%82&keyword=%E7%A7%91%E6%8A%80%E5%9B%AD")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert len(data["stores"]) >= 1
    s = data["stores"][0]
    assert s["store_code"] == "3330324"
    assert s["distance"] == 403
    assert s["business_status"] is True


def test_stores_endpoint_requires_city_and_keyword(server):
    status, _, body = _get(server, "/api/stores?city=%E6%B7%B1%E5%9C%B3%E5%B8%82")
    assert status == 400
    assert json.loads(body)["kind"] == "param"


# ---------------------------------------------------------------------------
# 4. 推荐接口：参数校验
# ---------------------------------------------------------------------------


def test_recommend_requires_store(server):
    status, body = _post(server, "/api/recommend", {"budget": 30})
    assert status == 400
    assert "store_code" in json.loads(body)["error"]


def test_recommend_rejects_bad_betype(server):
    status, body = _post(server, "/api/recommend", {"store_code": "3330324", "be_type": 9})
    assert status == 400
    assert json.loads(body)["kind"] == "param"


def test_recommend_rejects_bad_budget(server):
    status, body = _post(server, "/api/recommend", {"store_code": "3330324", "budget": -3})
    assert status == 400


def test_recommend_rejects_non_json_body(server):
    conn = HTTPConnection(server["host"], server["port"], timeout=10)
    try:
        conn.request("POST", "/api/recommend", body=b"not-json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 400
        assert json.loads(resp.read())["kind"] == "param"
    finally:
        conn.close()


def test_request_from_payload_maps_fields():
    req = _request_from_payload(
        {
            "store_code": "3330324",
            "be_type": 1,
            "budget": 30,
            "people": 2,
            "likes": "鸡腿堡, 咖啡",
            "dislikes": ["辣"],
            "goals": {"energy_kcal_max": 600, "protein_g_min": 30},
        }
    )
    assert req.store_code == "3330324"
    assert req.budget == 30.0
    assert req.people == 2
    assert req.effective_likes() == ["鸡腿堡", "咖啡"]
    assert req.dislikes == ["辣"]
    assert req.goals.energy_kcal_max == 600
    assert req.goals.protein_g_min == 30


# ---------------------------------------------------------------------------
# 5. 推荐接口：完整流程 + SSE 真实进度
# ---------------------------------------------------------------------------


def test_recommend_flow_returns_job_and_real_plan(server):
    status, body = _post(
        server, "/api/recommend", {"store_code": "3330324", "be_type": 1, "budget": 30, "people": 1}
    )
    assert status == 202
    job_id = json.loads(body)["job_id"]
    assert job_id

    # 轮询直到完成
    deadline = time.time() + 20
    snap: dict[str, Any] = {}
    while time.time() < deadline:
        st, _, b = _get(server, f"/api/jobs/{job_id}")
        assert st == 200
        snap = json.loads(b)
        if snap["status"] in ("done", "error"):
            break
        time.sleep(0.05)

    assert snap["status"] == "done", snap.get("error")
    plan = snap["result"]
    assert plan["store_code"] == "3330324"
    assert plan["data_source"]
    assert isinstance(plan["recommendations"], list)
    assert plan["recommendations"], "离线夹具下应至少给出一个方案"

    rec = plan["recommendations"][0]
    assert rec["items"], "方案必须包含商品"
    # 价格为真实试算字段（分与元都在）
    assert rec["price"]["payable_cent"] >= 0
    assert "payable_yuan" in rec["price"]
    # 商品带官方图片 URL（来自 query-meals 的 image 字段）
    imgs = [it["image"] for it in rec["items"] if it.get("image")]
    assert imgs, "菜单夹具含官方图片，方案商品应带 image"
    assert all(str(u).startswith("http") for u in imgs)
    # 结构里不出现凭据
    assert "Bearer" not in json.dumps(plan, ensure_ascii=False)


def test_sse_stream_emits_real_stage_events(server):
    _, body = _post(server, "/api/recommend", {"store_code": "3330324", "be_type": 1, "budget": 30})
    job_id = json.loads(body)["job_id"]
    events = _read_sse(server["host"], server["port"], f"/api/jobs/{job_id}/events")

    stages = {ev.get("stage") for ev in events if ev.get("stage")}
    # 这些阶段必须真实出现（对应真实 MCP 调用/真实循环）
    for expect in ("menu", "nutrition", "coupon", "candidates", "verify"):
        assert expect in stages, f"缺少阶段事件：{expect}（实际：{sorted(stages)}）"
    assert any(ev.get("type") == "result" for ev in events)


def test_job_snapshot_404_for_unknown_job(server):
    status, _, body = _get(server, "/api/jobs/deadbeef")
    assert status == 404


# ---------------------------------------------------------------------------
# 6. 错误分类
# ---------------------------------------------------------------------------


def test_config_missing_surfaces_as_config_error(monkeypatch):
    """凭据缺失时：任务以 kind=config 结束（前端据此给出可操作提示）。"""
    invoker = WebInvoker()

    def _factory():
        raise McpConfigMissingError("未配置 MCP 端点或令牌")

    httpd = create_server(host="127.0.0.1", port=0, client_factory=_factory, quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = _post({"host": "127.0.0.1", "port": port}, "/api/recommend", {"store_code": "3330324", "be_type": 1, "budget": 30})
        assert status == 202
        job_id = json.loads(body)["job_id"]
        deadline = time.time() + 10
        snap = {}
        while time.time() < deadline:
            _, _, b = _get({"host": "127.0.0.1", "port": port}, f"/api/jobs/{job_id}")
            snap = json.loads(b)
            if snap["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert snap["status"] == "error"
        assert snap["error_kind"] == "config"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_transport_failure_surfaces_as_network_error(monkeypatch):
    """传输失败时：kind=network，而非把编造数据当结果返回。"""

    class _FailInvoker(WebInvoker):
        def __call__(self, tool_name, arguments):  # noqa: ANN001
            self.calls.append((tool_name, arguments))
            if tool_name == "query-meals":
                raise McpTransportError("模拟 MCP 网络错误：Connection reset")
            return super().__call__(tool_name, arguments)

    invoker = _FailInvoker()
    httpd = create_server(host="127.0.0.1", port=0, client_factory=lambda: McpClient(invoker=invoker), quiet=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/recommend", body=json.dumps({"store_code": "3330324", "be_type": 1, "budget": 30}).encode(), headers={"Content-Type": "application/json"})
        job_id = json.loads(conn.getresponse().read())["job_id"]
        conn.close()
        deadline = time.time() + 10
        snap = {}
        while time.time() < deadline:
            st, _, b = _get({"host": "127.0.0.1", "port": port}, f"/api/jobs/{job_id}")
            snap = json.loads(b)
            if snap["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert snap["status"] == "error"
        assert snap["error_kind"] == "network"
        assert snap["result"] is None  # 绝不返回结果
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 7. 大请求体
# ---------------------------------------------------------------------------


def test_oversized_body_rejected(server):
    huge = {"store_code": "3330324", "be_type": 1, "budget": 30, "junk": "x" * (70 * 1024)}
    status, body = _post(server, "/api/recommend", huge)
    assert status == 400
    assert json.loads(body)["kind"] == "param"


# ---------------------------------------------------------------------------
# 8. 无结果时的「智能调整建议」（Phase 4.2）
# ---------------------------------------------------------------------------


def _run_job(server: dict[str, Any], payload: dict[str, Any], *, deadline: float = 20.0) -> dict[str, Any]:
    status, body = _post(server, "/api/recommend", payload)
    assert status == 202, body
    job_id = json.loads(body)["job_id"]
    snap: dict[str, Any] = {}
    end = time.time() + deadline
    while time.time() < end:
        st, _, b = _get(server, f"/api/jobs/{job_id}")
        assert st == 200
        snap = json.loads(b)
        if snap["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert snap["status"] == "done", snap.get("error")
    return snap["result"]


def test_no_result_returns_structured_adjustments(server):
    """无方案时接口必须返回结构化建议：字段 / 当前值 / 建议值 / 说明 / patch。"""
    plan = _run_job(
        server,
        {
            "store_code": "3330324",
            "be_type": 1,
            "budget": 30,
            "people": 1,
            "dislikes": ["辣"],
            "goals": {"energy_kcal_max": 500, "protein_g_min": 30},
        },
    )
    assert plan["recommendations"] == []
    adj = plan["adjustments"]
    assert adj, "无方案时必须给出调整建议"
    assert len(adj) <= 3, "最多 3 条"
    for a in adj:
        for key in ("kind", "field", "current", "suggested", "reason", "patch", "severity", "text"):
            assert key in a, f"建议缺少字段：{key}"
    # 阻断归因结构化输出，且可区分"热量 / 蛋白质 / 营养未知"
    blocked = plan["stats"]["blocked"]
    assert blocked["kcal"] > 0 and blocked["protein"] > 0 and blocked["nutrition_unknown"] > 0
    # 建议里不含凭据
    assert "Bearer" not in json.dumps(plan, ensure_ascii=False)


def test_adjustment_patch_only_sets_form_fields(server):
    """patch 的键必须是前端可映射的字段路径，且不得包含凭据类字段。"""
    plan = _run_job(
        server,
        {
            "store_code": "3330324",
            "be_type": 1,
            "budget": 30,
            "people": 1,
            "goals": {"energy_kcal_max": 500, "protein_g_min": 30},
        },
    )
    allowed = {
        "budget",
        "people",
        "dislikes",
        "likes",
        "goals.energy_kcal_max",
        "goals.protein_g_min",
        "goals.fat_g_max",
    }
    for a in plan["adjustments"]:
        assert set(a["patch"]) <= allowed, f"越界字段：{set(a['patch']) - allowed}"


def test_result_page_has_adjustment_markup(server):
    """静态页面必须包含「试试这样调整」模块与默认折叠的技术说明。"""
    status, _, body = _get(server, "/")
    assert status == 200
    html = body.decode("utf-8")
    assert 'id="adjust-box"' in html
    assert "试试这样调整" in html
    assert 'id="adjust-list"' in html
    assert 'id="btn-back-2"' in html
    # 技术细节默认折叠（details/summary）
    assert "<details" in html and "<summary" in html
    assert '<details class="note note--warn" id="warn-box" hidden>' in html


def test_frontend_js_maps_all_patch_keys(server):
    """前端必须能映射后端可能返回的全部 patch 键（否则"一键带回"会失效）。"""
    status, _, body = _get(server, "/app.js")
    assert status == 200
    js = body.decode("utf-8")
    for key in ("budget", "people", "dislikes", "likes", "goals.energy_kcal_max", "goals.protein_g_min"):
        assert f'"{key}"' in js, f"前端缺少 patch 映射：{key}"
    assert "applyAdjustment" in js
    # 不得在前端出现任何凭据处理（token / 鉴权头 / 端点密钥）
    low = js.lower()
    for bad in ("authorization", "bearer", "mcp_token", "mcd_mcp", "secret"):
        assert bad not in low, f"前端出现凭据相关字样：{bad}"


def test_adjustments_empty_when_result_exists(server):
    plan = _run_job(
        server, {"store_code": "3330324", "be_type": 1, "budget": 30, "people": 1}
    )
    assert plan["recommendations"]
    assert plan["adjustments"] == []
