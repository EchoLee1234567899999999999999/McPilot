# MCP 集成说明（MCP_INTEGRATION.md）

> 本文档说明 McPilot · 麦门决策局**实际使用的**麦当劳 MCP Server、Tool、调用流程与业务价值。
> 所有工具均为**只读**；本项目不调用任何写操作工具。

---

## 1. 使用的 MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | 麦当劳 MCP Server（WorkBuddy 连接器标识：`mcd-mcp`） |
| 接入端点 | `https://mcp.mcd.cn` |
| 协议 | MCP Streamable HTTP + JSON-RPC 2.0 |
| 鉴权 | `Authorization: Bearer <MCP Token>`（**Token 需使用者自行申请**，见第 6 节） |
| 在 WorkBuddy 中的工具前缀 | `mcp__mcd-mcp__<tool-name>` |

配置示例见仓库根目录 [`mcp-config.example.json`](mcp-config.example.json)（**仅含占位符，不含任何真实凭据**）。

### 1.1 两种等价的调用方式

本项目对 MCP 的使用有两条**互不冒充**的路径，二者共用同一套业务规则：

| 方式 | 调用者 | 说明 |
|---|---|---|
| **A. Skill 引导 MCP 调用** | WorkBuddy 智能体 | 智能体读取项目级 Skill（`.workbuddy/skills/mcpilot-meal-recommender/`），按 `SKILL.md` 编排 `mcp__mcd-mcp__*` 工具，并把真实返回交给规则层解析。**面向对话用户**。 |
| **B. Python 直连 MCP** | 本仓库代码 | `src/mcpilot/mcp_client.py` 直连端点发起 JSON-RPC 请求，由 `recommender.py` 产出推荐。**面向命令行与 Web 后端**。 |

> 两条路径都**必须**以 MCP 实时返回为准，不得使用写死的价格/营养/券。

---

## 2. 实际调用的 MCP Tool（6 个，全部只读）

| # | Tool | 用途 | 本项目真实用途 |
|---|------|------|----------------|
| 1 | `query-nearby-stores` | 查询可点餐门店 | 定位门店，获取 `storeCode`（得来速另取 `beCode`） |
| 2 | `query-meals` | 查询门店在售餐品/套餐 | 拉取菜单与商品编码 `code`、展示价、官方图片 |
| 3 | `query-meal-detail` | 查询餐品详情 | 取**规范全名**（用于营养匹配）与套餐**默认组成**（`rounds[].isDefault==1`） |
| 4 | `list-nutrition-foods` | 获取营养数据 | 获取热量/蛋白质等，用于营养匹配与硬约束过滤 |
| 5 | `query-store-coupons` | 查询门店优惠券 | 取门店 + 订单类型下可用券，做门槛/适用商品/时效匹配 |
| 6 | `calculate-price` | 价格试算 | 对候选组合试算**真实实付**（返回单位为「分」） |

### 2.1 明确禁用的写操作

以下工具**被客户端白名单守卫拒绝**，本项目在任何路径下都不会调用：

`create-order`、`auto-bind-coupons`、`draw-lottery`、`mall-create-order`、
`party-order-create`、`cancel-order`，以及任何积分 / 地址写入操作。

> 守卫位置：`src/mcpilot/mcp_client.py::McpClient.call_tool`（客户端层，写操作不可达）。

---

## 3. 调用流程

```
┌─ 1. 参数确认 ────────────────────────────────────────┐
│  场景：到店自取 beType=1 → orderType=1，不传 beCode    │
│       得来速   beType=5 → orderType=1，必传 beCode     │
└──────────────────────┬───────────────────────────────┘
                       ▼
   2. query-nearby-stores   → 门店列表 → 选定 storeCode
                       ▼
   3. query-meals           → 在售餐品/套餐（code / 展示价 / 图片）
      └─ query-meal-detail （按需，≤ max_resolve，默认 10）→ 规范全名 + 默认组成
                       ▼
   4. list-nutrition-foods  → 营养表 → 分层匹配（exact→normalized→alias→canonical→composition）
                       ▼
   5. query-store-coupons   → 可用券 → 门槛 / 适用商品编码 / 时效匹配
                       ▼
   6. 组合生成与打分        → 三策略择优（budget / nutrition / balanced）
                       ▼
   7. calculate-price       → 候选真实试算（≤ max_verify，默认 12）→ 实付金额
                       ▼
   8. 输出推荐（含理由、用券、营养、数据来源与查询时间）
```

### 3.1 调用次数有界（防止过量请求）

| 环节 | 上限 | 说明 |
|---|---|---|
| 固定只读查询 | 3 次 | `query-nearby-stores` / `query-meals` / `list-nutrition-foods` 各 1 次 |
| `query-meal-detail` | ≤ 10（`max_resolve`） | 同一名称只解析一次，带缓存 |
| `calculate-price` | ≤ 12（`max_verify`） | 候选去重后按策略轮转试算 |
| `query-store-coupons` | 1 次 | 门店券只在开始时查一次 |

> **不缓存价格**：每一轮推荐都重新调用 `calculate-price`，绝不以缓存价冒充实时价。

---

## 4. 业务价值

麦当劳 MCP 提供了**权威、实时**的门店 / 菜单 / 营养 / 优惠 / 价格数据。McPilot 在其之上解决的是
"**数据有了，但用户仍不知道该怎么点**"这一步：

| 用户痛点 | 本项目用 MCP 如何解决 |
|---|---|
| 菜单 100+ 项，不知怎么搭配 | 用 `query-meals` 全量菜单 + 组合枚举，按角色（主食/配菜/饮料/甜品）生成"是一餐"的搭配 |
| 预算有限但想吃饱 | 用 `calculate-price` 试算**真实实付**（含券），确保不超预算 |
| 想控热量/增蛋白 | 用 `list-nutrition-foods` 做营养匹配，计算蛋白质密度（g/100kcal），未命中如实标注 |
| 有券但不知道能不能用 | 用 `query-store-coupons` 按**商品编码相交**判定适用性，不套用不命中的券 |
| 不知道优惠力度 | 用 `calculate-price` 给出原价 / 优惠 / 实付三项对比 |

**关键原则**：所有金额、营养、券信息**只来自 MCP 返回**；工具未返回或无法可靠匹配的，一律标注
"未知 / 缺失"，**绝不猜测、绝不用部分数据冒充整体、绝不编造**。

---

## 5. 真实响应格式的已知坑位（实现要点）

MCP 工具返回 markdown，业务 JSON 嵌在 `## Original Response` 之后。本项目已处理：

1. **提取内嵌 JSON**：`extract_json_payload` 用括号配对提取，不依赖正则贪心。
2. **金额单位不统一**：`query-meals` 返回「元」字符串，`calculate-price` 返回「分」整数。
   内部统一以**整数分**计算，展示转「元」保留 2 位。
3. **营养表为 TSV 字符串**：`list-nutrition-foods.data` 形如 `[N]{cols}:` + 每行 2 空格缩进逗号分隔。
4. **菜单用简名、营养表用规范全名**（如「薯条」vs「中薯条」）→ 用 `query-meal-detail` 的规范全名补齐。
5. **券适用性只看编码**：以 `products[].productCode` 与组合商品编码比对为准。

---

## 6. 如何自行申请 MCP Token（使用者必读）

> ⚠️ **仓库中不包含、也不会包含任何真实 Token。** 你需要用自己的账号申请。

1. 打开 <https://mcp.mcd.cn>，点击右上角【登录】。
2. 使用**手机号**完成验证登录（登录后右上角按钮变为「控制台」）。
3. 点击右上角【控制台】→ 点击【激活】→ 阅读并同意服务协议 → **复制 MCP Token**。
4. 在 WorkBuddy 中：左侧边栏【专家·技能·连接器】→【连接器】→ 右上角【自定义连接器】→【配置 MCP】，
   粘贴 [`mcp-config.example.json`](mcp-config.example.json) 中的结构，把 `Bearer ${MCD_MCP_TOKEN}`
   换成你的真实 Token 后保存并【启用】。
5. 如需脱离 WorkBuddy 独立运行，把 Token 写入本地 `.env` 的 `MCD_MCP_TOKEN`（`.env` 已被 `.gitignore` 忽略）。

参考：官方 [麦当劳 MCP Server 使用指南](https://github.com/M-China/mcd-mcp-server)。

---

## 7. 安全与合规

- **凭据零落盘**：Token / `Authorization` 请求头 / 账号信息不写入仓库、日志或示例。
- **只读**：白名单守卫在客户端层拦截一切写操作。
- **只读幂等重试**：仅对 429/5xx 与传输超时做**有界重试 + 指数退避**；4xx 直接报错不重试。
- **降级不造假**：接口异常时如实告知并降级，绝不返回编造结果。

详见 [`README.md`](README.md) 第九节与 [`CONTEST_DECLARATION.md`](CONTEST_DECLARATION.md)。
