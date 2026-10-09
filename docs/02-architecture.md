# 技术方案与架构（Architecture）

> 项目：McPilot · 麦门决策局 ｜ 版本：v0.2（Phase 2）
> 关联文档：[01-requirements.md](01-requirements.md)、[03-mcp-tools.md](03-mcp-tools.md)

---

## 1. 总体架构

系统分为四层，职责单一、边界清晰：

```
┌─────────────────────────────────────────────────────────┐
│ ① Skill 层（WorkBuddy 内置）                              │
│   SKILL.md：理解意图 → 编排工具调用顺序 → 生成推荐话术      │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│ ② 编排 / 服务层（src/mcpilot）                            │
│   nutrition / coupon / pricing / recommender / models     │
│   职责：营养匹配、券匹配、价格试算、三策略打分、数据建模      │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│ ③ MCP 客户端层（src/mcpilot/mcp_client.py）               │
│   统一封装 6 个只读工具的调用与错误重试                     │
└───────────────────────────┬─────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────┐
│ ④ 麦当劳官方 MCP（只读）                                   │
│   query-nearby-stores / query-meals / query-meal-detail    │
│   list-nutrition-foods / query-store-coupons / calculate-price │
└─────────────────────────────────────────────────────────┘
```

## 2. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 运行载体 | WorkBuddy Skill（项目级） | 参赛要求"真正的 AI Skill"，且天然接入平台 MCP |
| Skill 位置 | `.workbuddy/skills/mcpilot-meal-recommender/` | **符合 WorkBuddy 项目级 Skill 规范**，随仓库分发 |
| 核心语言 | Python 3.13 | 可读性强、零依赖、评审易复现 |
| 鉴权 | 由平台连接器托管 | 满足"凭据零落盘"，仓库可直接公开 |
| 数据单位 | 内部以"分"整数计算 | 避免浮点误差；仅在展示层转"元" |
| 降级策略 | 缺失即标注、不臆造 | 满足"只用真实数据"的硬约束 |

## 3. 模块职责（src/mcpilot）

| 模块 | 职责 | 关键接口（计划） |
|------|------|------------------|
| `models.py` | 数据模型：请求、门店、餐品、营养、券、报价、推荐结果 | dataclass / TypedDict |
| `mcp_client.py` | 封装 6 个只读 MCP 工具调用、参数规则、白名单守卫与错误映射 | `McpClient` |
| `menu.py` | 菜单 / 详情解析（展示价单位「元」） | `parse_menu()` |
| `nutrition.py` | 营养表解析、**精确名称匹配**、组合营养加权合计（缺失即标注） | `build_index()` / `total_nutrition()` |
| `coupon.py` | 券适用性判断（按 `products[].productCode`）与组合匹配 | `match_coupons()` |
| `pricing.py` | 调用 `calculate-price`、分/元换算、带券试算 | `quote()` |
| `combos.py` | **候选组合生成与约束**：角色分类、预算/口味/人数/指定商品过滤、规模封顶 | `generate_candidates()` |
| `recommender.py` | **三种策略**（budget/nutrition/balanced）打分、理由生成、结构化输出 | `recommend()` |

> Phase 1 已用真实 MCP 打通数据链路；Phase 2 在**试算前**先用展示价 + 营养表做粗筛，
> 再对少量候选调用 `calculate-price`，从而把 MCP 调用控制在有限次数内。

## 4. 数据流

```
UserRequest(预算, 人数, 营养目标, 口味/忌口, 指定商品, 场景)
        │
        ▼
 query-meals ──► MenuItem[]  ────────────────┐
        │                                     │
        ├──► list-nutrition-foods ──► 营养索引 ├─► 候选组合生成（combos.py）
        │                                     │   角色分类 + 预算/忌口/人数过滤
        ▼                                     │   + 规模封顶（不调用 MCP）
 query-store-coupons ──► Coupon[]  ───────────┤
                                              ▼
                         轮转选出少量候选（≤ max_verify）
                                              │
                                              ▼
                 calculate-price（逐候选真实试算，含适用券）──► Quote[]
                                              │
                                              ▼
            硬约束过滤（实付 ≤ 预算；营养目标达标）
                                              │
                                              ▼
        策略打分（budget / nutrition / balanced）──► 每策略取最优
                                              │
                                              ▼
 Recommendation[]（含理由、真实实付、券、营养口径、数据缺失提示）
                                              │
                                              ▼
                       RecommendationPlan（可 JSON 序列化，供前端）
```

**调用顺序要点**：门店 → 菜单 → （营养 / 券）→ 候选生成 → **价格试算（最后）**。
价格试算依赖"门店 + 场景 + 餐品 code + 券"，必须最后执行；且展示价**不得**当作最终实付。

## 5. 参数传递约定（务必遵守）

见 [03-mcp-tools.md](03-mcp-tools.md) 第 2 节。要点：

- 到店自取（`beType=1`）：`orderType=1`，**严禁传** `beCode`（传了会报错）。
- 得来速（`beType=5`）：`orderType=1`，**必须传** `beCode`。
- 预约场景才传 `reservationDate`（格式 `yyyy-MM-dd HH:mm`）。

## 6. 安全与合规设计

1. **工具白名单**：客户端只暴露 6 个只读工具，写操作（下单/领券/抽奖/积分）在代码层不可达。
2. **凭据不入库**：不读取、不写入任何 Token / Authorization；鉴权交给平台连接器。
3. **日志脱敏**：日志只记录工具名与参数键，不记录响应中的敏感字段与凭据。
4. **仓库洁净**：`.gitignore` 拦截 `.env`、日志、WorkBuddy 运行时数据；`.env.example` 值为空。
5. **真实数据**：所有业务数值直达展示，禁止本地写死。

## 7. 可测试性设计

- 服务层与策略层通过**依赖注入**接收 MCP 客户端，便于在测试中注入 mock 返回。
- 纯函数化的营养/券/打分逻辑便于单元测试。
- 覆盖场景见 [../tests/TEST_PLAN.md](../tests/TEST_PLAN.md)。

## 8. 目录结构（源码视角）

```
src/mcpilot/
├── __init__.py          # 包入口，导出公共 API
├── config.py            # 运行时解析 MCP 接入点（凭据零落盘）
├── mcp_client.py        # MCP 只读客户端（直连 + 白名单守卫 + 参数规则）
├── menu.py              # 菜单 / 详情解析
├── models.py            # 数据模型（含推荐结果模型）
├── nutrition.py         # 营养匹配（精确名称 + 加权合计）
├── coupon.py            # 优惠券匹配（按适用商品编码）
├── pricing.py           # 价格试算（分/元）
├── combos.py            # 候选组合生成与约束
├── recommender.py       # 三种推荐策略 + 打分 + 结构化输出
└── pipeline.py          # 端到端最小链路
```

## 9. 两种可运行的 MCP 接入方式（Phase 1 已落地）

> 结论：**两种方式都真实可运行**，且共用同一套服务层逻辑。区别只在"谁发起工具调用"。

### 方式 A —— Python 直连 MCP（推荐用于自动化与测试）

麦当劳 MCP 暴露为 **streamable HTTP + JSON-RPC 2.0** 端点（`https://mcp.mcd.cn`）。
`src/mcpilot/mcp_client.py::HttpToolInvoker` 直接发起 `initialize` + `tools/call`：

```
Python 进程 ──POST JSON-RPC──► https://mcp.mcd.cn ──► 麦当劳 MCP
```

- 凭据由 `src/mcpilot/config.py` **在运行时**解析，优先级：
  环境变量 `MCD_MCP_ENDPOINT`/`MCD_MCP_TOKEN` → WorkBuddy 本地配置 `~/.workbuddy/mcp.json`。
- **凭据不落盘、不打印**；`redact_headers()` 保证日志安全。
- 优点：可自动化、可写真实集成测试、可独立部署。

### 方式 B —— 由 Skill 调度 MCP（WorkBuddy 运行时）

WorkBuddy 智能体通过内置工具（`mcp__mcd-mcp__query-meals` 等）调用同样的 6 个工具，
再把返回交给 `mcpilot` 服务层解析：

```
用户 ─► WorkBuddy 智能体（SKILL.md 编排）─► mcp__mcd-mcp__* ─► 麦当劳 MCP
```

- 优点：零凭据管理（平台托管）、天然贴合"AI Skill"形态。
- 复用方式：把智能体拿到的 JSON 注入 `McpClient(invoker=agent_invoker)`，
  下游 `menu/nutrition/coupon/pricing/pipeline` 完全一致。

### 为什么不写"伪 MCP 服务"

两种方式都指向**同一个真实端点**，不存在模拟实现。测试中的 `FixtureInvoker` 仅在
**离线回归**中使用，且夹具是**真实采集**的响应（见 `tests/fixtures/README.md`），
明确的实时验证由 `tests/integration`（真实调用）承担。

## 10. 后续演进（概要）

详见 [04-roadmap.md](04-roadmap.md)。Phase 1 已完成数据链路；Phase 2 将在其上实现
三种推荐策略与打分。
