"""McPilot Web 层（Phase 4 新增）。

只读原则不变：Web 后端复用 ``mcpilot.recommender`` 与 ``mcpilot.mcp_client``，
写操作在客户端层即被拒绝；MCP 凭据仅在服务端解析，绝不进入前端。
"""

from __future__ import annotations

from .app import create_server, serve

__all__ = ["create_server", "serve"]
