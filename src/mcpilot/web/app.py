"""McPilot Web 后端（Phase 4 新增）。

技术选型
--------
**Python 标准库 ``http.server``（``ThreadingHTTPServer``）+ 原生 HTML/CSS/JS，零构建、零第三方依赖。**

选型理由：

1. 项目核心 ``src/mcpilot`` 已是**零第三方依赖**的 Python 包，继续用标准库可保证
   "克隆即运行"，评审无需安装 Node / 打包器。
2. 需要复用的推荐逻辑就是 ``mcpilot.recommender.recommend``——它是 Python 函数，
   用 Python 直接承载 HTTP 层最自然，**无需跨语言桥接**。
3. 真实调用 ~10 秒，需要**服务端后台任务 + 进度推送**；``ThreadingHTTPServer`` 每请求
   一线程，配合 :mod:`mcpilot.web.jobs` 即可实现 SSE，不引入 Web 框架。

安全边界
--------
- **MCP 凭据只在服务端**：前端只与本服务的 ``/api/*`` 通信，服务端自行解析端点与鉴权头
  （见 ``config.resolve_endpoint``），**任何响应体都不会包含 Token / 请求头**。
- 默认**只绑定 127.0.0.1**（本机访问），不对外网暴露。
- 静态资源服务做**路径穿越防护**；请求体有大小上限。

API
---
======================  ======  ============================================
路径                     方法     说明
======================  ======  ============================================
``/api/health``         GET     健康检查（不触发 MCP）
``/api/config``         GET     返回是否具备 MCP 凭据（**不含凭据本身**）
``/api/stores``         GET     真实门店查询（``query-nearby-stores``）
``/api/recommend``      POST    创建推荐任务 → 返回 ``job_id``
``/api/jobs/<id>``      GET     任务快照（轮询兜底）
``/api/jobs/<id>/events`` GET   SSE 真实进度事件流
======================  ======  ============================================
"""

from __future__ import annotations

import json
import re
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..config import McpConfigError, resolve_endpoint
from ..mcp_client import (
    McpClient,
    McpConfigMissingError,
    McpError,
    McpForbiddenToolError,
    McpParamError,
    McpToolError,
    McpTransportError,
)
from ..models import NutritionGoals, UserRequest
from ..pricing import cent_to_yuan
from ..recommender import recommend
from ..stores import stores_to_dicts
from .jobs import Job, JobManager

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_BODY = 64 * 1024  # 请求体上限 64KB
_STORE_CACHE_TTL = 300.0  # 门店列表短缓存（秒）：门店变动很慢，避免重复查询

ClientFactory = Callable[[], McpClient]

_ERROR_STATUS: dict[str, HTTPStatus] = {
    "config": HTTPStatus.SERVICE_UNAVAILABLE,
    "network": HTTPStatus.BAD_GATEWAY,
    "timeout": HTTPStatus.GATEWAY_TIMEOUT,
    "server": HTTPStatus.BAD_GATEWAY,
    "param": HTTPStatus.BAD_REQUEST,
    "internal": HTTPStatus.INTERNAL_SERVER_ERROR,
}


def _classify(exc: BaseException) -> tuple[str, int]:
    """把异常映射为 ``(kind, http_status)``，便于前端给出可操作提示。"""
    if isinstance(exc, (McpConfigMissingError, McpConfigError)):
        kind = "config"
    elif isinstance(exc, McpParamError):
        kind = "param"
    elif isinstance(exc, McpTransportError):
        kind = "network" if "超时" not in str(exc) and "timeout" not in str(exc).lower() else "timeout"
    elif isinstance(exc, McpForbiddenToolError):
        kind = "internal"
    elif isinstance(exc, McpToolError):
        kind = "server"
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        kind = "param"
    elif isinstance(exc, McpError):
        kind = "server"
    else:
        kind = "internal"
    return kind, int(_ERROR_STATUS.get(kind, HTTPStatus.INTERNAL_SERVER_ERROR))


class _ServerState:
    """所有请求线程共享的状态。"""

    def __init__(self, client_factory: ClientFactory) -> None:
        self.client_factory = client_factory
        self.jobs = JobManager()
        self._store_lock = threading.Lock()
        self._store_cache: tuple[float, str, list[dict[str, Any]]] | None = None


def _request_from_payload(payload: dict[str, Any]) -> UserRequest:
    """把前端 JSON 映射为 :class:`UserRequest`（严格校验，绝不臆造字段）。"""
    if not isinstance(payload, dict):
        raise McpParamError("请求体必须是 JSON 对象。")

    store_code = str(payload.get("store_code") or "").strip() or None
    be_type = int(payload.get("be_type") or 1)
    if be_type not in (1, 2, 5, 6):
        raise McpParamError(f"非法 beType：{be_type}（应为 1/2/5/6）。")
    if not store_code:
        raise McpParamError("缺少门店编码 store_code。")

    budget = payload.get("budget")
    budget_val: Optional[float] = None
    if budget not in (None, ""):
        try:
            budget_val = float(budget)
        except (TypeError, ValueError):
            raise McpParamError("预算必须是数字。") from None
        if budget_val <= 0:
            raise McpParamError("预算必须大于 0。")

    people = int(payload.get("people") or 1)
    if people < 1:
        raise McpParamError("人数必须 ≥ 1。")

    def _str_list(key: str) -> list[str]:
        raw = payload.get(key) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,，;；\s]+", raw) if x]
        return [str(x).strip() for x in raw if str(x).strip()]

    g = payload.get("goals") or {}
    if not isinstance(g, dict):
        g = {}

    def _f(key: str) -> Optional[float]:
        v = g.get(key)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise McpParamError(f"营养目标 {key} 必须是数字。") from None

    goals = NutritionGoals(
        protein_g_min=_f("protein_g_min"),
        energy_kcal_max=_f("energy_kcal_max"),
        energy_kcal_target=_f("energy_kcal_target"),
        fat_g_max=_f("fat_g_max"),
    )

    strategies = _str_list("strategies")
    valid = {"budget", "nutrition", "balanced"}
    strategies = [s for s in strategies if s in valid] or ["budget", "nutrition", "balanced"]

    return UserRequest(
        scene="pickup" if be_type == 1 else "drive_thru",
        be_type=be_type,
        be_code=(str(payload.get("be_code")).strip() or None) if payload.get("be_code") else None,
        store_code=store_code,
        budget=budget_val,
        people=people,
        goals=goals,
        likes=_str_list("likes"),
        dislikes=_str_list("dislikes"),
        include_codes=_str_list("include_codes"),
        use_coupon=bool(payload.get("use_coupon", True)),
        strategies=strategies,  # type: ignore[arg-type]
        max_verify=int(payload.get("max_verify") or 12),
        max_resolve=int(payload.get("max_resolve") or 10),
    )


class McPilotHandler(BaseHTTPRequestHandler):
    """请求处理器（每个请求一个线程）。"""

    server_version = "McPilot/0.4"
    protocol_version = "HTTP/1.1"

    # 由 create_server 注入
    state: _ServerState
    quiet: bool = True

    # -- 日志（默认安静，避免刷屏；可用 --verbose 打开）--------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if not self.quiet:  # pragma: no cover
            super().log_message(fmt, *args)

    # -- 响应工具 ----------------------------------------------------------
    def _send_json(self, obj: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str, kind: str) -> None:
        self._send_json({"ok": False, "error": message, "kind": kind}, status=status)

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise McpParamError("Content-Length 非法。") from None
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            raise McpParamError("请求体过大（上限 64KB）。")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise McpParamError("请求体不是合法 JSON。") from None
        if not isinstance(data, dict):
            raise McpParamError("请求体必须是 JSON 对象。")
        return data

    # =====================================================================
    # GET
    # =====================================================================
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                return self._send_json({"ok": True, "service": "mcpilot-web", "version": __version__})
            if path == "/api/config":
                return self._api_config()
            if path == "/api/stores":
                return self._api_stores(query)
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9]+)", path)
            if m:
                return self._api_job_snapshot(m.group(1))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9]+)/events", path)
            if m:
                return self._api_job_events(m.group(1))
            if path.startswith("/api/"):
                return self._send_error_json(HTTPStatus.NOT_FOUND, "未知接口。", "param")
            return self._serve_static(path)
        except McpParamError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), "param")
        except Exception as exc:  # noqa: BLE001 — 兜底，绝不泄漏堆栈给前端
            kind, status = _classify(exc)
            self._send_error_json(HTTPStatus(status), str(exc), kind)

    # =====================================================================
    # POST
    # =====================================================================
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/recommend":
            self._send_error_json(HTTPStatus.NOT_FOUND, "未知接口。", "param")
            return
        try:
            payload = self._read_body()
            request = _request_from_payload(payload)
        except McpParamError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc), "param")
            return
        except Exception as exc:  # noqa: BLE001
            kind, status = _classify(exc)
            self._send_error_json(HTTPStatus(status), str(exc), kind)
            return

        job = self.state.jobs.create()
        threading.Thread(
            target=self._run_job, args=(job, request), name=f"mcpilot-job-{job.job_id}", daemon=True
        ).start()
        self._send_json({"ok": True, "job_id": job.job_id}, status=HTTPStatus.ACCEPTED)

    # =====================================================================
    # 业务实现
    # =====================================================================
    def _api_config(self) -> None:
        """返回后端是否具备 MCP 凭据——**绝不返回凭据内容**。"""
        try:
            ep = resolve_endpoint()
            self._send_json(
                {
                    "ok": True,
                    "mcp_configured": True,
                    "endpoint_host": ep.url.split("/")[2] if "://" in ep.url else "",  # 仅主机名
                    "auth_mode": "header" if ep.headers else "none",
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {
                    "ok": True,
                    "mcp_configured": False,
                    "reason": str(exc),
                }
            )

    def _api_stores(self, query: dict[str, list[str]]) -> None:
        city = (query.get("city") or [""])[0].strip()
        keyword = (query.get("keyword") or [""])[0].strip()
        be_type = int((query.get("be_type") or ["1"])[0])
        if not city or not keyword:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST, "查询门店需要同时提供 city 与 keyword。", "param"
            )
            return

        cache_key = f"{be_type}|{city}|{keyword}"
        import time as _t

        with self.state._store_lock:
            cached = self.state._store_cache
            if cached and cached[1] == cache_key and (_t.time() - cached[0]) < _STORE_CACHE_TTL:
                self._send_json({"ok": True, "stores": cached[2], "cached": True})
                return

        client = self.state.client_factory()
        payload = client.query_nearby_stores(be_type=be_type, search_type=2, city=city, keyword=keyword)  # type: ignore[arg-type]
        stores = stores_to_dicts(payload)
        with self.state._store_lock:
            self.state._store_cache = (_t.time(), cache_key, stores)
        self._send_json({"ok": True, "stores": stores, "cached": False})

    def _api_job_snapshot(self, job_id: str) -> None:
        job = self.state.jobs.get(job_id)
        if job is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在或已过期。", "param")
            return
        self._send_json({"ok": True, **job.snapshot()})

    def _api_job_events(self, job_id: str) -> None:
        job = self.state.jobs.get(job_id)
        if job is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "任务不存在或已过期。", "param")
            return
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # SSE 为不定长流：用 Connection: close 明确告知客户端"读到 EOF 结束"，
        # 避免 HTTP/1.1 keep-alive 下客户端无法判断边界。
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            for ev in job.subscribe():
                if ev.get("type") == "heartbeat":
                    self.wfile.write(b": ping\n\n")
                else:
                    data = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端断开
            pass

    def _run_job(self, job: Job, request: UserRequest) -> None:
        """后台线程：真实调用推荐（带真实进度回调）。"""
        job.status = "running"
        try:
            client = self.state.client_factory()
            plan = recommend(request, client=client, on_progress=job.publish)
            job.finish(json.loads(plan.to_json(cent_to_yuan=cent_to_yuan)))
        except Exception as exc:  # noqa: BLE001
            kind, _ = _classify(exc)
            job.fail(str(exc), kind)

    # =====================================================================
    # 静态资源
    # =====================================================================
    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (_STATIC_DIR / rel).resolve()
        try:
            target.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径。", "param")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            # 前端为单页：未知非 API 路径回退到 index.html
            fallback = _STATIC_DIR / "index.html"
            if fallback.is_file() and "." not in Path(rel).name:
                target = fallback
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "资源不存在。", "param")
                return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    client_factory: Optional[ClientFactory] = None,
    quiet: bool = True,
) -> ThreadingHTTPServer:
    """创建（但不启动）HTTP 服务。

    ``client_factory`` 允许测试注入替身客户端；默认 :class:`McpClient`（真实 MCP）。
    """
    factory: ClientFactory = client_factory or (lambda: McpClient())
    state = _ServerState(factory)

    handler = McPilotHandler
    handler.state = state  # type: ignore[attr-defined]
    handler.quiet = quiet  # type: ignore[attr-defined]

    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.state = state  # type: ignore[attr-defined]
    return httpd


def serve(*, host: str = "127.0.0.1", port: int = 8765, verbose: bool = False) -> None:
    """启动服务并阻塞（供 ``python -m mcpilot.web`` 调用）。"""
    httpd = create_server(host=host, port=port, quiet=not verbose)
    bound_host, bound_port = httpd.server_address[:2]
    print("McPilot Web 已启动")
    print(f"  本地预览： http://{bound_host}:{bound_port}/")
    print("  按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\n正在停止…")
    finally:
        httpd.server_close()


__all__ = ["create_server", "serve", "McPilotHandler", "_request_from_payload"]
