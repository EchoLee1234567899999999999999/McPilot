"""McPilot · 麦门决策局 —— 核心库。

封装麦当劳官方 MCP 的**只读**工具（6 个），并在其上实现菜单/营养/优惠券解析、
适用性匹配与价格试算；推荐算法见 Phase 2。

两种可运行架构：
- **Python 直连 MCP**：:class:`mcpilot.mcp_client.HttpToolInvoker` 经 streamable HTTP 调用。
- **由 Skill 调度 MCP**：WorkBuddy 智能体调用 ``mcp__mcd-mcp__*`` 工具，注入结果复用同一逻辑。

设计约束：只读、真实数据、凭据零落盘。
"""

from __future__ import annotations

__version__ = "0.4.3"

__all__ = [
    "config",
    "mcp_client",
    "models",
    "menu",
    "nutrition",
    "resolve",
    "coupon",
    "pricing",
    "combos",
    "advice",
    "pipeline",
    "recommender",
    "stores",
    "web",
]
