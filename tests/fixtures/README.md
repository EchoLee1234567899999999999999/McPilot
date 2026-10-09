# tests/fixtures/

本目录保存**真实采集**的麦当劳 MCP 返回，用于**离线回归测试**（解析/匹配逻辑）。

## 来源

- 采集时间：2026-10-09
- 来源工具：麦当劳 MCP（`mcd-mcp`）
- 门店：`storeCode=3330324`，`orderType=1`，`beType=1`

| 文件 | 对应工具 | 说明 |
|------|----------|------|
| `menu_3330324.json` | `query-meals` | 门店在售菜单（125 项） |
| `nutrition.json` | `list-nutrition-foods` | 营养表（160 条） |
| `price_1100.json` | `calculate-price` | 巨无霸单价试算（分） |
| `detail_1100.json` | `query-meal-detail` | 巨无霸详情与特调项 |
| `detail/*.json` | `query-meal-detail` | 单品/套餐详情（含 `rounds[]` 默认组成），用于分层解析回归 |
| `coupons_3330324.redacted.json` | `query-store-coupons` | 门店券（**已脱敏**） |
| `coupons_applicable.synthetic.json` | `query-store-coupons` | ⚠️ **合成夹具（非真实返回）**，见下 |

### `detail/` 目录（Phase 3 新增）

从真实 `query-meal-detail` 采集中**裁剪**出的最小必要字段（保留 `code`/`name`/`rounds[]`），用于离线验证分层解析：

| 文件 | 门店商品 | 规范全名（detail 返回） | 验证点 |
|------|----------|------------------------|--------|
| `detail/4810.json` | 薯条 | **中薯条** | 简名→规范全名（规格补齐） |
| `detail/1401.json` | 麦乐鸡 | **麦乐鸡5块** | 简名→规范全名（件数补齐） |
| `detail/4437.json` | 玉米杯 | **小杯玉米杯** | 简名→规范全名（规格补齐） |
| `detail/507387.json` | 卡布奇诺 | **卡布奇诺中杯** | 简名→规范全名 + 特调项（`不加`/`加`） |
| `detail/9900000888.json` | 巨无霸四件套 | 巨无霸四件套 | `rounds[]` 默认组成（巨无霸+中薯条+麦乐鸡4块+可乐中杯） |

> `9900000888` 为**测试用编码占位**（`code` 字段被改写以便离线注入），其余字段与组成均来自真实返回，未改动任何业务数值。
> 这些文件只保留 `name`/`supportModify`/`rounds` 的必要字段以控制体积。



## ⚠️ 关于 `coupons_applicable.synthetic.json`

这是**明确标注为 SYNTHETIC 的合成夹具**，仅用于离线验证"券**确实适用**于某菜单商品、
并真实产生优惠"的代码路径 —— 因为真实门店 `3330324` 的两张券，其适用商品编码
（`9900014239`、`9900016370`）都不在该店菜单中，属于**真实的"券不适用"**情形。

它**不是**真实 MCP 返回，**不得**被当成真实查询结果引用；真实券行为以
`coupons_3330324.redacted.json` 与实时集成测试为准。

## 重要说明

1. 这些是**真实响应**（除上述合成文件外），不是编造数据；但它们是**采集快照**，不等于"当前实时结果"。
2. 价格会随门店与活动变化，因此离线测试**只断言结构与规则**，不断言易变数值。
3. `coupons_*.redacted.json` 已将 `couponId` / `couponCode` / `promotionId`
   替换为 `<REDACTED>`，避免泄露可能与账号相关的信息。
4. 需要**实时**验证时，运行：`MCPILOT_LIVE=1 python -m pytest tests/integration -v`。
5. `tests/conftest.py::RecommenderInvoker` 是**离线测试替身**：价格由菜单夹具中的
   真实展示价推导（并非 MCP 返回），用于确定性驱动推荐逻辑；真实试算以实时测试为准。

> 重新采集夹具：删除本目录文件后，用 `McpClient()` 拉取并覆盖即可（注意保持券信息脱敏）。
