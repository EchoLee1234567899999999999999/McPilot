"""MCP 接入配置解析（Phase 1）。

在**运行时**解析麦当劳 MCP 的接入点与鉴权头，优先级：

1. 环境变量 ``MCD_MCP_ENDPOINT`` / ``MCD_MCP_TOKEN``（推荐；CI 与独立运行）
2. WorkBuddy 本地配置 ``~/.workbuddy/mcp.json`` 中名为 ``mcd-mcp`` 的服务
3. 项目根目录 ``.env``（若存在）

安全约定（硬性）：
- 本模块**只读**配置，绝不把凭据写入仓库、日志或异常信息。
- 对外统一使用 :func:`redact_headers` 生成可安全打印的头信息。
- 凭据始终保留在本地配置中，不落盘到项目目录。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

WORKBUDDY_MCP_CONFIG = Path.home() / ".workbuddy" / "mcp.json"
SERVER_NAME = "mcd-mcp"

# 需要脱敏的请求头字段（大小写不敏感）
_SENSITIVE_HEADER_KEYS = ("authorization", "token", "cookie", "api-key", "apikey", "secret")


class McpConfigError(RuntimeError):
    """无法解析 MCP 接入配置时抛出（不包含任何凭据内容）。"""


@dataclass
class McpEndpoint:
    """MCP 接入点。``headers`` 可能含凭据，**禁止**直接打印或序列化。"""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    source: str = "unknown"

    def safe_repr(self) -> str:
        """返回可安全打印的字符串（凭据已脱敏）。"""
        return f"McpEndpoint(url={self.url!r}, source={self.source!r}, headers={redact_headers(self.headers)!r})"


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """对请求头脱敏，仅保留键名与掩码，供日志使用。"""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        if any(s in k.lower() for s in _SENSITIVE_HEADER_KEYS):
            out[k] = "***REDACTED***"
        else:
            out[k] = v
    return out


def _load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析（无第三方依赖）。不覆盖已存在的环境变量。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _from_env(dotenv: dict[str, str]) -> McpEndpoint | None:
    url = os.environ.get("MCD_MCP_ENDPOINT") or dotenv.get("MCD_MCP_ENDPOINT")
    token = os.environ.get("MCD_MCP_TOKEN") or dotenv.get("MCD_MCP_TOKEN")
    if url:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return McpEndpoint(url=url, headers=headers, source="env")
    return None


def _from_workbuddy() -> McpEndpoint | None:
    if not WORKBUDDY_MCP_CONFIG.is_file():
        return None
    try:
        cfg = json.loads(WORKBUDDY_MCP_CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    srv = (cfg.get("mcpServers") or {}).get(SERVER_NAME)
    if not srv or not srv.get("url"):
        return None
    headers = srv.get("headers") or {}
    # 仅保留字符串型头字段
    headers = {str(k): str(v) for k, v in headers.items()}
    return McpEndpoint(url=str(srv["url"]), headers=headers, source="workbuddy")


def resolve_endpoint(project_root: Path | None = None) -> McpEndpoint:
    """按优先级解析 MCP 接入点。

    不读取、不返回任何可落盘的凭据文本；凭据只保留在内存中的 headers。
    """
    root = project_root or Path(__file__).resolve().parents[2]
    dotenv = _load_dotenv(root / ".env")

    for resolver in (_from_env, _from_workbuddy):
        ep = resolver(dotenv) if resolver is _from_env else resolver()
        if ep is not None:
            return ep

    raise McpConfigError(
        "未找到 mcd-mcp 接入配置。请任选其一：\n"
        "  1) 设置环境变量 MCD_MCP_ENDPOINT（以及可选的 MCD_MCP_TOKEN）；\n"
        f"  2) 确认 WorkBuddy 已连接 mcd-mcp（配置文件 {WORKBUDDY_MCP_CONFIG}）。\n"
        "注意：不要把凭据写入仓库内文件。"
    )
