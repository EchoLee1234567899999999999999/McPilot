# Phase 1 开发报告：MCP 数据链路

> 版本：v0.1 ｜ 日期：2026-10-09 ｜ 状态：✅ 完成

---

## 1. 目标回顾

让 McPilot 通过**真实**麦当劳 MCP 跑通「门店 → 菜单 → 营养 → 优惠券 → 价格试算」最小链路，
并明确两种 MCP 接入方式的可行性与边界。

## 2. 架构结论：两种方式都真实可运行

| 方式 | 机制 | 是否需要凭据 | 本项目用途 |
|------|------|--------------|-----------|
| **A. Python 直连 MCP** | streamable HTTP + JSON-RPC 2.0 调 `https://mcp.mcd.cn` | 运行时解析（不落盘） | 自动化、真实集成测试、CLI |
| **B. 由 Skill 调度 MCP** | WorkBuddy 智能体调 `mcp__mcd-mcp__*` | 平台托管 | 面向用户的 AI Skill |

两者**调用同一个真实端点**，且共用 `src/mcpilot` 服务层。详见 `docs/02-architecture.md` 第 9 节。

实测：直连方式 `initialize` 返回 `serverInfo.name = "mcd-mcp"`，`tools/list` 返回 **35 个工具**。

## 3. 交付物

### 新增 / 重写源码

| 文件 | 说明 |
|------|------|
| `src/mcpilot/config.py` | 新增。运行时解析接入点；`redact_headers()` 脱敏 |
| `src/mcpilot/mcp_client.py` | 重写。真实 JSON-RPC 客户端 + 白名单守卫 + 参数规则 + 响应解析 |
| `src/mcpilot/menu.py` | 新增。菜单 / 详情解析（元） |
| `src/mcpilot/nutrition.py` | 实现。营养表解析 + 精确匹配 + 加权合计 |
| `src/mcpilot/coupon.py` | 实现。券解析 + 适用性匹配 |
| `src/mcpilot/pricing.py` | 实现。价格解析（分）+ 分/元换算 + 组合试算 |
| `src/mcpilot/pipeline.py` | 新增。端到端最小链路 + CLI |
| `src/mcpilot/models.py` | 扩展。对齐真实 schema 的字段 |
| `src/mcpilot/__init__.py` | 更新导出与版本 |

### 测试

| 文件 | 类型 | 结果 |
|------|------|------|
| `tests/unit/*` | 离线单元（63 条） | ✅ 63 passed |
| `tests/integration/test_live_mcp.py` | 真实 MCP 集成（6 条） | ✅ 6 passed |
| `tests/fixtures/*` | 真实采集夹具（券已脱敏） | — |
| `tests/conftest.py` | `FixtureInvoker` 离线驱动 | — |
| `pytest.ini` | 测试配置 | — |

### 文档

`docs/02-architecture.md`（新增两种方式）、`docs/03-mcp-tools.md`（新增真实响应格式实录）、
`docs/05-phase1-report.md`（本文件）；`README.md`、`src/mcpilot/README.md` 同步更新。

## 4. 六个工具的验证结果

| # | 工具 | 真实调用 | 实测要点 |
|---|------|:---:|------|
| 1 | `query-nearby-stores` | ✅ | 收藏(`searchType=1`)与位置搜索(`searchType=2`)均返回；含 `reservationTimeOptions` |
| 2 | `query-meals` | ✅ | 门店 3330324 返回 **125 项**；巨无霸 `1100` 现价 **26 元** |
| 3 | `query-meal-detail` | ✅ | 巨无霸 `supportModify=true`，含 5 个特调项（附 `selectedKey`/`unselectedKey`） |
| 4 | `list-nutrition-foods` | ✅ | **160 条**营养；巨无霸 513 kcal / 蛋白 27 g / 脂肪 26 g / 碳水 42 g |
| 5 | `query-store-coupons` | ✅ | 该门店 2 张券，适用商品分别为麦旋风/薯薯专有编码 |
| 6 | `calculate-price` | ✅ | 巨无霸 `price=2600`（分）= ¥26.00，与菜单展示价一致 |

## 5. 关键验证结论

1. **菜单查询成功**：125 项，含商品编码、名称、展示价。
2. **营养匹配**：巨无霸**精确匹配**成功；菜单"薯条"因营养表只有"中/大/小薯条"（歧义）被**标注为"营养未知"**——符合"不确定不猜测"。
3. **券适用范围**：对"巨无霸+薯条"组合，门店两张券均**判定不适用**并排除；对券自身目标商品判定适用。
4. **价格一致**：`calculate-price` 返回分为单位，`¥26.00` 与菜单一致；**最终价一律取自该工具**，不用展示价替代。
5. **带券试算**：麦旋风任选券真实产生优惠 ¥4.10（¥14.00 → ¥9.90），来源为 MCP 返回。
6. **接口失败**：不存在的门店 / 模拟传输失败均抛出**明确异常**，不返回编造数据。

## 6. 安全与合规

- 仅调用 6 个只读工具；`create-order`/`auto-bind-coupons`/`draw-lottery` 等写操作在
  **客户端层即被拒绝**（`McpClient.call_tool` 白名单守卫），且 `HttpToolInvoker` 二次校验。
- 凭据仅在**运行时**从本地配置/环境变量解析，**不写入仓库、日志或示例**；
  集成测试断言仓库内不存在疑似凭据（`test_no_credentials_committed`）。
- 券夹具中的 `couponId`/`couponCode`/`promotionId` 已脱敏为 `<REDACTED>`。

## 7. 已知限制与未决项

- 当前仅覆盖**到店自取(beType=1)**；得来速(5)已实现参数规则但**尚未用真实 beCode 跑通**。
- 麦乐送(2)/团餐(6)未覆盖（属 roadmap）。
- 价格随门店与活动变化，集成测试只断言**结构与规则**，不断言具体数值。
- `recommender.py`（三种策略）仍为骨架，属 Phase 2。

## 8. 是否可进入 Phase 2

**可以。** 数据链路已真实可用且被测试覆盖，推荐算法可在 `pipeline` 输出的结构化结果之上开发。
