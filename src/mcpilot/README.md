# src/mcpilot/

核心库（Phase 1 打通真实数据链路，Phase 2 实现三种推荐策略，Phase 3 优化匹配与健壮性）。

## 模块职责

| 模块 | 职责 | 状态 |
|------|------|------|
| `config.py` | 运行时解析 MCP 接入点与鉴权头（凭据零落盘）；请求头脱敏 | ✅ |
| `mcp_client.py` | 6 个**只读**工具的调用：直连 streamable HTTP（JSON-RPC）+ 白名单守卫 + 参数规则 + **有界重试/退避** | ✅ Phase 3 |
| `menu.py` | 解析 `query-meals` / `query-meal-detail`（价格单位为**元**）+ 套餐默认组成 | ✅ |
| `nutrition.py` | **营养分层匹配**（`exact/normalized/alias/canonical/composition`）+ **覆盖率报告**；不确定即标注缺失 | ✅ Phase 3 |
| `resolve.py` | **按需解析**：用 `query-meal-detail` 取规范全名与套餐组成（有界缓存） | ✅ Phase 3 |
| `coupon.py` | 解析券、**来源区分**（门店/账户/可领取）与**时效判定**，按编码判定适用性 | ✅ Phase 3 |
| `pricing.py` | 解析 `calculate-price`（金额单位为**分**）并转「元」 | ✅ |
| `combos.py` | **候选组合生成与约束**：角色分类、**主食硬约束**、预算/口味/人数/指定商品过滤、去重、规模封顶 | ✅ Phase 3 |
| `recommender.py` | **三种推荐策略**（budget/nutrition/balanced）+ 打分 + 理由 + 结构化输出 + CLI + 调用统计 | ✅ Phase 3 |
| `pipeline.py` | 端到端最小链路（门店→菜单→营养→券→价格）+ CLI | ✅ |

## 两种调用方式

- **Python 直连 MCP**：`McpClient()` 默认使用 `HttpToolInvoker`，直接以 JSON-RPC 调用
  `https://mcp.mcd.cn`，凭据运行时解析、绝不落盘。
- **由 Skill 调度 MCP**：WorkBuddy 智能体调用 `mcp__mcd-mcp__*`，把返回注入
  `McpClient(invoker=...)` 即可复用全部解析/匹配/推荐逻辑。

## 快速运行

```bash
# 最小链路：门店 → 菜单 → 营养 → 券 → 价格
PYTHONPATH=src python -m mcpilot.pipeline --store 3330324 --be-type 1 --items 1100:1,4810:1

# 三种策略推荐（真实试算）
PYTHONPATH=src python -m mcpilot.recommender --store 3330324 --be-type 1 --budget 30
PYTHONPATH=src python -m mcpilot.recommender --store 3330324 --be-type 1 --budget 40 \
    --people 2 --like 炸鸡 --dislike 辣 --include 1100 --json
```
