# workbuddy.md — WorkBuddy 开发对话上下文

> 本文件按 2026 麦当劳程序员创意开发大赛「WorkBuddy 专项奖励」要求提交，
> 用于核验本项目**真实使用腾讯 WorkBuddy 智能体开发**。
>
> **说明**：本文件是对 WorkBuddy 会话记录（`818c2c64-4712-4e8d-b4c0-8be352efa615.jsonl`，
> 3180 条事件 / 358 条消息 / 956 次工具调用）的**人工整理摘要**。原始导出含本地会话令牌
> （WorkBuddy 连接器注入的临时 Bearer 凭据），按参赛声明"信息安全声明"要求**不予原样公开**，
> 本摘要已脱敏；如赛事方需核验原始记录，可通过 GitHub 联系作者提供。
> 全部开发（代码、测试、文档、截图）均在 WorkBuddy 会话中完成。

---

## 1. 项目信息

| 项 | 值 |
|---|---|
| 项目名称 | McPilot · 麦门决策局（mcpilot-meal-recommender） |
| 开发工具 | 腾讯 WorkBuddy（Agent 模式，含 MCP 连接器、Skill 机制、CDP 浏览器验证） |
| 会话日期 | 2026-10-09（单日完成 Phase 0 → Phase 5 全部开发） |
| 开发模型 | WorkBuddy 内置模型（会话记录 `providerData.model` 字段可核验） |

## 2. 开发过程（按阶段，均由用户在 WorkBuddy 对话中下达指令）

| 阶段 | 用户指令要点 | WorkBuddy 产出 |
|---|---|---|
| Phase 0 | 项目基建：目录、文档、Skill 初始文件、测试计划 | 目录结构 / `.gitignore` / SKILL.md 初版 |
| Phase 1 | 接入麦当劳 MCP 六个只读工具，端到端跑通 | `src/mcpilot/`（mcp_client / menu / nutrition / pricing 等）+ 真实集成测试 |
| Phase 2 | 三种推荐策略（省钱 / 高蛋白 / 综合） | `combos.py` + `recommender.py`（budget / nutrition / balanced） |
| Phase 3 | 营养匹配优化、券时效、推荐质量、健壮性 | 分层营养匹配、券适用性、有界重试、覆盖率报告 |
| Phase 4 | 产品化：像素风 Web 界面 + 决策卡 | `src/mcpilot/web/`（标准库 HTTP + 原生前端，零构建） |
| Phase 4.2 | 无结果时的智能调整建议 | `advice.py` 结构化归因 + 前端一键带回表单 |
| Phase 4.3 | 推荐质量与产品说明优化：高蛋白双口径（密度 vs 总量）、甜品可解释维度、经济单品诚实标注、结果页信息层级 | recommender 改造 + 新增 24 条测试 + 真实 50 元前后对比 |
| Phase 5 | 参赛资格审核与 GitHub 发布准备 | 本文件、CONTEST_DECLARATION.md、MCP_INTEGRATION.md、安全扫描脚本、真实截图 |

## 3. WorkBuddy 能力的真实使用证据

- **MCP 连接器**：在 WorkBuddy 中配置 `mcd-mcp`（streamable HTTP → `https://mcp.mcd.cn`），
  会话内通过 `mcp__mcd-mcp__*` 工具真实调用门店 / 菜单 / 营养 / 券 / 试算接口。
- **Skill 机制**：项目级 Skill 位于 `.workbuddy/skills/mcpilot-meal-recommender/`，
  按 WorkBuddy Skill 规范（YAML frontmatter：`name` / `description` / `agent_created: true`）编写。
- **工程化验证**：会话内驱动 headless Chrome（CDP）对 Web 界面做桌面 + 移动仿真验证；
  用 `node --check` 做前端语法门禁；pytest 离线单元 233 条 + 真实集成 21 条全绿。
- **记忆系统**：使用项目级 `.workbuddy/memory/` 记录跨会话约定（schema 坑位、参数规则、测试法）。

## 4. 关键工程决策（对话中确认）

1. **只用真实 MCP 数据**：价格 / 营养 / 券一律来自工具返回，禁止编造或写死；
   营养未知不按 0 计、不给部分合计。
2. **只读红线**：客户端白名单守卫拦截一切写操作（下单 / 领券 / 抽奖 / 积分）。
3. **凭据零落盘**：Token 只存在于 WorkBuddy 连接器与本地 `.env`（已忽略），绝不进仓库。
4. **金额单位统一**：内部整数分，展示元；`query-meals`（元）与 `calculate-price`（分）的坑位已适配。

## 5. 测试结果（会话内真实执行）

| 项 | 结果 |
|---|---|
| 离线单元测试 | 233 passed, 1 skipped |
| 真实 MCP 集成测试（`MCPILOT_LIVE=1`） | 21 passed |
| 安全扫描（`scripts/security_scan.py --strict`） | 0 凭据 / 0 敏感信息命中 |
| 前端语法门禁（`node --check`） | 通过 |
| 移动端布局（CDP 390px 仿真） | 无横向溢出（scrollWidth === clientWidth === 390） |
