# Phase 4.2 开发报告 —— 无推荐结果时的智能调整建议

> 项目：McPilot · 麦门决策局 ｜ 日期：2026-10-09 ｜ 版本：v0.4.2
> 前置：Phase 4（Web 界面）已完成；本阶段在其上**增量开发**，未重建项目、未改动 Phase 0–3 的 Skill / MCP 客户端 / 推荐算法语义。

---

## 1. 背景与问题

用户真实查询：**到店自取 ｜ ¥30 ｜ 热量 ≤ 500 kcal ｜ 蛋白质 ≥ 30 g ｜ 不吃辣**。
系统正确返回 0 套方案（不会用违规方案凑数），但用户只能点「修改需求」自己猜怎么改——
缺少**具体、可操作、有数据依据**的调整建议。

## 2. 设计原则（硬约束）

1. **只用本次真实候选与真实试算的数据**；绝不编造热量 / 蛋白质 / 价格。
2. **不擅自放宽用户硬约束**：建议只是"待用户确认"的候选变更（`patch`），
   生成建议**不改请求、不自动重新查询**；必须由用户点击带回表单并主动开始决策。
3. **不把有限候选搜索说成整份菜单无解**：所有措辞限定在"本次已生成并试算的候选范围"。
4. **营养未知不按 0 计**：因"营养未知、无法确认达标"被剔除的组合单独归因。
5. 数据不足时退回**通用提示**，不虚构具体数值。

## 3. 一、无结果原因分析（实现）

新增 `src/mcpilot/advice.py`：`BlockInfo` 汇集本次**真实**阻断归因，`blocked_summary()` 输出机器可读统计（进入 `stats.blocked`），`summary_warning()` 生成可读说明（进入 `warnings`）。

| 类别 | 数据来源 | 计数键 |
|------|----------|--------|
| 预算不足 | 真实实付 > 预算（`calculate-price`） | `over_budget` |
| 热量超标 | 可靠营养合计 > 上限 | `kcal` |
| 蛋白质不足 | 可靠营养合计 < 下限 | `protein` |
| 忌口冲突 | 套餐**组成**命中忌口（名称层在候选生成期已过滤） | `dislike` |
| 营养缺失 | 分层匹配未命中、无法确认达标（**未按 0 计**） | `nutrition_unknown` |
| 菜单商品不足 | 当前条件下候选数 = 0 | `candidates` |
| （附）试算失败 | 接口 / 网络失败 | `verify_failed` |

配套改动：`_goals_ok` 升级为 `_goals_eval`，返回**结构化原因码**（`kcal_high` / `protein_low` / `fat_high` / `*_unknown`），并额外收集"**仅因该条件**被剔除"的候选——只有这些才能支撑"放宽一项即可纳入"的数值建议，避免"某组合同时差热量和蛋白质"被误用于单项建议。

## 4. 二、智能调整建议（实现）

- **模型**：`models.Adjustment` —— `kind` / `field` / `current` / `suggested` / `reason` / `patch` / `severity` / `text`。
  `RecommendationPlan` 新增 `adjustments[]`（结构化），保留 `suggestions[]`（由 `text` 派生，向后兼容，CLI 与旧测试不受影响）。
- **生成规则**（优先级序，最多 3 条）：
  1. `menu`：候选数为 0 → 放宽忌口 / 去掉指定商品（不给数值）；
  2. `budget`：预算内 0 个 → 提高到**真实试算**的最低实付；
  3. `kcal_max`：仅差热量的候选中的**最低真实热量** → 向上取整；
  4. `protein_min`：仅差蛋白质的候选中的**最高真实蛋白质** → 向下取整；
  5. `fat_max` / `nutrition_goal` / `dislikes` / `retry`：按归因给出（营养缺失不给数值，只说明"放宽数值也无法确认达标"）；
  6. `generic`：兜底通用提示（**不含任何数值**）。
- **前端**：结果页空状态下方新增醒目「试试这样调整」模块：
  每条展示 `字段｜当前值 → 建议值` 徽章 + 数据依据 + 「带回表单」按钮；
  点击只写表单并滚回首页（toast 提示"请确认后点击开始决策"），**绝不自动查询**；
  patch 键与表单字段一一映射（`budget`/`people`/`dislikes`/`likes`/`goals.energy_kcal_max`/`goals.protein_g_min`），越界键一律不写。

## 5. 三、交互设计（实现）

- 保留复古像素风与配色：调整模块用像素顶边 + 黄/青方块 + 打字机光标动画，视觉权重高于底部日志。
- **技术细节默认折叠**：「数据说明与限制（拒绝原因明细）」与「数据来源与 MCP 调用明细」改为 `<details>`，默认收起，用户可自行展开。
- 「返回修改需求」按钮两处（结果页头部 + 调整模块底部），**保留其他已填条件**（前端从不重置表单）。
- 桌面 / 手机（390px）均验证：无横向溢出（`scrollWidth === clientWidth`），建议卡片单列、按钮整行。

## 6. 四、测试与安全（结果）

| 项 | 结果 |
|----|------|
| 离线单元 | **210 passed**（新增 `test_advice.py` 21 条 + Web 建议 5 条；Phase 0–3 全部保留通过） |
| 真实 MCP 集成 | **21 passed**（新增 `test_live_advice.py` 4 条） |
| 用户真实场景 | ¥30 / ≤500 kcal / ≥30 g / 忌辣 → 0 方案 + 3 条建议（见下） |
| 建议可操作性 | **把建议真的应用后重跑**：热量 578 / 蛋白 28 / 预算 13.90 三条路径均产生可行方案（证明数值有据） |
| 多条件冲突 | 用户场景同时出现 热量超标 3 / 蛋白不足 6 / 营养缺失 4 / 忌口命中 1，归因互不混淆 |
| 不擅自放宽 | 单测断言 `plan.request` 原样；浏览器实测点击「带回表单」后**未**触发查询 |
| 安全 | 扫描无凭据；无写操作调用；`~/.workbuddy/mcp.json` 未改动（mtime 早于本阶段） |

**真实 MCP 实测（门店 3330324，2026-10-09）**：

- 建议 1（热量上限）：500 → **578 kcal**——"3 个候选热量超标，其中 1 个只差热量：双层深海鳕鱼堡×1 + 圆筒冰淇淋×1（578 kcal）"。
- 建议 2（蛋白质下限）：30 → **28 g**——"6 个候选蛋白不足，其中 4 个只差蛋白质：双层深海鳕鱼堡×1（28.0 g）"。
- 建议 3（营养目标）：另有 4 个候选因营养未知无法确认达标被剔除（未按 0 计），可移除营养目标后重查。

## 7. 修改文件清单

**新增**

- `src/mcpilot/advice.py` — 阻断归因 + 建议生成（`BlockInfo` / `build_adjustments` / `blocked_summary` / `summary_warning`）
- `tests/unit/test_advice.py`（21 条）、`tests/integration/test_live_advice.py`（4 条）
- `docs/09-phase42-report.md`（本文件）

**修改**

- `src/mcpilot/models.py` — 新增 `Adjustment`；`RecommendationPlan.adjustments`（`to_dict` 序列化）
- `src/mcpilot/recommender.py` — `_goals_eval` 结构化归因；收集"仅因单项被剔除"的候选与忌口命中词；生成建议；`stats.blocked`
- `src/mcpilot/web/static/index.html` — 调整建议模块 + 折叠式数据说明
- `src/mcpilot/web/static/app.js` — `renderAdjustments` / `applyAdjustment` / `syncQuickBudget`；折叠渲染；阻断归因中文标签
- `src/mcpilot/web/static/styles.css` — `.adjust` / `.adj` 样式 + 响应式
- `src/mcpilot/web/app.py` — `/api/health` 版本号取自包版本
- `src/mcpilot/__init__.py` — 0.4.2，导出 `advice`
- `README.md`、`docs/04-roadmap.md`、`tests/TEST_PLAN.md`、Skill `references/data-model.md`、项目记忆

## 8. 如实声明（仍未验证）

1. **忌口建议受"最多 3 条"限制**：真实运行中确有 1 个候选因套餐组成命中忌口被剔除（`blocked.dislike=1`），但按优先级排序后被热量 / 蛋白质 / 营养缺失三条占满，未进入展示——归因计数已如实输出（`stats.blocked` 与折叠区可见）。忌口建议本身的逻辑与文案由离线单测覆盖。
2. `fat_max` 建议仅离线逻辑覆盖（真实场景未设置脂肪上限）。
3. 营养缺失建议是"移除目标"而非数值调整——这是数据现状决定的（营养覆盖率非 100%），不是代码缺陷。
4. 建议数值会随菜单 / 活动价格波动，每次查询实时计算（不做缓存）。
5. 未上传 GitHub、未报名、未执行任何下单 / 领券 / 积分 / 抽奖操作。
