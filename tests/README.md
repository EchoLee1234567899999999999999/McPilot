# tests/

测试目录。**Phase 1~4 已落地可运行测试。**

```
tests/
├── conftest.py            # sys.path 配置 + 夹具加载 + RecommenderInvoker（离线驱动）
├── fixtures/              # 真实采集的 MCP 响应（券已脱敏），见其 README
├── unit/                  # 离线单元测试（无需网络；含 Web API 测试）
└── integration/           # 真实 MCP 集成测试（默认跳过；含 Web 端到端）
```

## 运行

```bash
# 离线单元测试（184 条，秒级；其中 test_web_api.py 会在本机临时端口起真实 HTTP 服务）
python -m pytest tests/unit -q

# 真实 MCP 集成测试（17 条，需已连接 mcd-mcp 或配置 MCD_MCP_ENDPOINT）
MCPILOT_LIVE=1 python -m pytest tests/integration -v
```

## 分层约定

| 层级 | 数据来源 | 断言范围 |
|------|----------|----------|
| 单元 | 真实采集夹具（`fixtures/`）或纯逻辑 | 解析结果、匹配规则、参数规则、安全 |
| 集成 | **真实** MCP 调用 | 仅**结构与规则**，不断言易变业务数值 |

## 硬性约定

- 集成测试**真实调用** MCP，不跳过、不 mock 业务数值。
- 单元测试的夹具是**真实响应快照**，明确标注来源与时间；不得伪造数值冒充"实时结果"。
- 券相关夹具的 `couponId`/`couponCode`/`promotionId` 必须脱敏。
- 任何测试都不得触及写操作工具。
