# Phase 4 开发报告 —— 产品界面与交互设计（Web）

> 项目：McPilot · 麦门决策局 ｜ 版本：v0.4 ｜ 日期：2026-10-09
> 上游文档：[01-requirements](01-requirements.md) · [02-architecture](02-architecture.md) · [07-phase3-report](07-phase3-report.md)

---

## 1. 本阶段目标与结果

| 目标 | 结果 |
|------|------|
| 复古像素游戏 × 现代产品交互的响应式网页 | ✅ 三视图（首页 / 查询状态 / 结果）+ 决策卡，桌面与手机双端验证 |
| 通过**后端真实调用** McPilot 推荐系统 | ✅ 浏览器 → 本地 HTTP 服务 → `recommender.recommend` → 真实 MCP |
| 保持 WorkBuddy Skill 可独立使用 | ✅ Skill 与 Web 是**两个交付物**，共享 `src/mcpilot` 同一份核心规则 |
| 不重建项目、保留 Phase 0–3 全部能力 | ✅ 仅增量修改（`models`/`recommender` 增字段与回调），**201 条测试全绿** |

## 2. 技术选型

**Python 标准库 `http.server`（ThreadingHTTPServer）+ 原生 HTML/CSS/JS，零构建、零第三方依赖。**

| 决策 | 理由 |
|------|------|
| 后端用 Python 标准库 | 要复用的推荐逻辑就是 `mcpilot.recommender.recommend`（Python 函数），HTTP 层用 Python 承载最自然；无需 Flask/FastAPI 依赖即可"克隆即运行" |
| 进度用 SSE + 轮询兜底 | 真实推荐 ~8–10s，进度事件来自**服务端真实回调**（`on_progress`），不是前端计时器伪造；SSE 断线自动降级轮询 |
| 前端零框架 | 页面复杂度不需要 React/Vue；省去 npm install 与打包步骤，评审门槛最低 |
| 餐品图片用官方 `query-meals.image` | 禁止用无关图片冒充餐品；无图显示占位符，绝不编造 |

## 3. 架构与安全边界

```
浏览器（无凭据）
   │  fetch / EventSource
   ▼
127.0.0.1:8765  mcpilot.web.app（ThreadingHTTPServer）
   │  ├─ 静态资源：路径穿越防护
   │  ├─ /api/stores        → query-nearby-stores（只读）
   │  └─ /api/recommend     → 后台线程跑 recommend(on_progress=真实回调)
   │        └─ /api/jobs/<id>/events（SSE）/ /api/jobs/<id>（轮询）
   ▼
McpClient（白名单守卫；凭据运行时解析，仅存在于本进程内存）
   ▼
麦当劳官方 MCP（6 个只读工具）
```

- **Token 只在服务端**：`/api/config` 只回 `mcp_configured` 与主机名，测试断言响应体不含令牌。
- **默认只绑定 127.0.0.1**；请求体上限 64KB；错误信息不泄漏堆栈。
- **写操作不可达**：Web 层没有新增任何 MCP 能力，下单/领券等在 `McpClient.call_tool` 白名单处即被拒绝。

### API 一览

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查（不触发 MCP） |
| `/api/config` | GET | 是否具备 MCP 凭据（不含凭据本身） |
| `/api/stores` | GET | 真实门店查询（city + keyword） |
| `/api/recommend` | POST | 创建推荐任务 → `{job_id}` |
| `/api/jobs/<id>` | GET | 任务快照（轮询兜底） |
| `/api/jobs/<id>/events` | GET | SSE 真实进度事件流 |

## 4. 核心增量（代码层）

Phase 4 对 Phase 0–3 代码的改动是**纯增量、向后兼容**的：

| 文件 | 改动 |
|------|------|
| `models.py` | `MenuItem.image` / `RecommendedItem.image`（官方图 URL，无则空串）；新增 `Store` |
| `menu.py` | 解析 `image` 字段 |
| `stores.py`（新增） | `query-nearby-stores` 解析 + 前端 dict 序列化（距离/营业状态） |
| `recommender.py` | ① `on_progress` 真实进度回调（回调异常被吞掉，绝不影响推荐结果）；② 商品图注入推荐项；③ 新增阶段事件 `store/menu/nutrition/coupon/resolve/candidates/verify/done` |
| `web/`（新增） | `app.py`（HTTP + API + SSE）、`jobs.py`（线程安全任务/事件）、`static/`（前端三件套） |
| `scripts/run_web.py`（新增） | 启动脚本（自动补 `src` 到 import 路径） |

## 5. 界面功能与真实验证

### 5.1 首页
品牌标识、简介与三条"数据可信"承诺；预算（含 **¥20/¥30/¥50 快捷键**）、人数、口味偏好、
忌口、营养目标（热量上限/蛋白下限）、补充需求；**真实门店选择**（城市+关键词 → 门店接口，
含距离/营业状态，默认选第一家，5 分钟短缓存）。

### 5.2 查询状态页
八个阶段（门店→菜单→营养→券→解析→候选→试算→完成）由**后端真实事件**驱动，
含逐条日志（时间戳 + 真实调用详情）、计时器、45 秒无事件提示、120 秒硬超时、中断与重试。

### 5.3 推荐结果页
三策略卡片（黄/青/橙），每张含：真实试算价与优惠、官方图片+数量+行小计、
可靠营养合计或**明确"营养缺失"**、券是否适用、评分与推荐理由；方案不足时**如实说明**；
底部为数据说明（warnings）、调整建议（suggestions）、数据来源（含 MCP 调用次数）。

### 5.4 可分享决策卡
Canvas 绘制的像素风卡片（高度自适应内容），可切换方案、**导出 PNG**（已验证生成 119KB
PNG 且画布未被跨域图片污染）、复制数据说明；含门店、beType、查询时间与数据来源声明。

### 5.5 真实验证记录（本机 Chrome 142px / 390px + 真实 MCP）

- 桌面全流程：查询门店 → 5 家真实门店 → 选店 → 状态页真实事件日志（在售 114 件、营养表 160 条）
  → 结果页 3 方案 → 决策卡导出。
- **¥30 真实结果**（门店 #1420762，试算 12 次，~8.6s）：
  省钱 精选超值随心配×1 **¥13.90**（营养缺失如实标注）｜
  高蛋白 双层深海鳕鱼堡+圆筒 **¥29.00**（485kcal/28g+93kcal/2g）｜
  综合 双层吉士汉堡+圆筒 **¥28.00**（429kcal/27g+93kcal/2g）。
- 手机 390px：全流程可用，单列卡片、无横向溢出。
- **四种状态**：加载（真实进度）✔／失败（config/network 等分类提示+重试）✔／
  超时（120s 硬超时）✔／无数据（¥3 预算 → 0 方案 + "最便宜可行组合 ¥13.90" 建议）✔。

## 6. 恢复与修复记录（本次会话）

Phase 4 上次执行在约 2 分 20 秒处中断（仅完成 `stores.py` 与 `models.py`/`recommender.py`
的部分编辑，Web 层尚未开始）。本次在**原文件基础上**恢复并完成：

1. **修复中断遗留的语法契约不一致**：`_fetch` 内 `emit({...})`（dict）与 `recommend` 内
   `emit(stage, status, detail)`（位置参数）签名冲突 → 统一为位置参数，**164 条旧测试恢复全绿**。
2. **补完图片链路**：`image_by_code` 已计算但未消费 → 注入 `_make_recommendation` → `RecommendedItem.image`。
3. **补全 Web 层**（上次完全未开始）：后端 API、任务/SSE、前端三件套、启动脚本、测试。
4. **修复前端阻断性 JS 语法错误**（`const parts = \`..\`, \`..\``）——该错误会导致整个
   `app.js` 不执行（页面完全无交互），已修复并用 Node `--check` 门禁。
5. **修复移动端顶部栏横向溢出**（nowrap 标题缺 `min-width:0`），并用 CDP 在 390px
   真实移动视口下确认 `scrollWidth === 390`、无溢出元素。
6. **决策卡高度自适应**（原固定 1000px 高，内容只占上半屏）。

## 7. 测试结果

| 套件 | 数量 | 结果 |
|------|------|------|
| 离线单元（含 **20 条 Web API 测试**） | **184** | ✅ 全部通过 |
| 真实 MCP 集成（含 **3 条 Web 端到端**） | **17** | ✅ 全部通过（MCPILOT_LIVE=1） |

新增 Web 测试覆盖：接口契约、参数校验、**凭据绝不回显**、静态资源与路径穿越、
SSE 真实阶段事件、任务快照、错误分类（config/network）、超大请求体拒绝、
SPA 回退；真实端到端断言"绝不超预算、图片必为官方 URL 或空"。

## 8. 交付清单（本阶段新增/修改）

```
新增：
  src/mcpilot/web/__init__.py  __main__.py  app.py  jobs.py
  src/mcpilot/web/static/index.html  styles.css  app.js
  src/mcpilot/stores.py
  scripts/run_web.py
  tests/unit/test_web_api.py（20 条）
  tests/integration/test_live_web.py（3 条）
  docs/08-phase4-report.md（本文件）
修改：
  src/mcpilot/models.py（image / Store）
  src/mcpilot/menu.py（image 解析）
  src/mcpilot/recommender.py（on_progress / 图片注入 / 阶段事件）
  src/mcpilot/__init__.py（0.4.0）
  README.md、tests/README.md、tests/TEST_PLAN.md、docs/04-roadmap.md
```

## 9. 运行方式

```powershell
# Windows（项目根目录）
python scripts/run_web.py            # → http://127.0.0.1:8765/
python scripts/run_web.py --port 9000 --verbose

# 测试
python -m pytest tests/unit
MCPILOT_LIVE=1 python -m pytest tests/integration
```

## 10. 仍未覆盖（如实声明）

1. 真实"用券成功减免"仍无样本（Phase 3 遗留，Web 层如实展示"未使用券"）。
2. 120 秒硬超时为前端兜底逻辑，未在真实网络下人为触发验证（代码路径简单）。
3. 移动端验证基于 Chrome 设备模拟（390px）+ 真实后端，未覆盖 iOS Safari 等真机差异。
4. Web 层未做并发压测（设计为单机自用；任务管理器有界：最多保留 64 个任务记录）。
5. 决策卡不包含餐品照片（避免跨域图片污染画布导致导出失败），仅文字与像素装饰。
