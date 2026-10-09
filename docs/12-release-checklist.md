# GitHub 发布准备（Phase 5 交付）

> 本文件为发布前的内部核对清单与报名材料草稿。**当前尚未创建公开仓库 / 未 push / 未提交报名 Issue**，
> 等作者确认后再执行。

## 1. 报名 Issue 草稿（严格按官方模板，<1000 字，无图片）

```
【参赛申请】
项目名称：McPilot·麦门决策局（mcpilot-meal-recommender）
项目地址：https://github.com/<你的用户名>/mcpilot-meal-recommender
项目简介：预算内帮你决策"麦当劳怎么点"的 AI Skill：真实调用麦当劳官方 MCP 六个只读工具
（门店/菜单/营养/券/试算），按预算·营养目标·口味·优惠券生成三种可解释方案（省钱/高蛋白/综合），
价格来自真实试算、营养缺失如实标注、绝不编造数据、不做任何下单写操作。
```

- 项目名称备选：`McPilot 麦门决策局`（Issue 内可用中文名；仓库名建议 ASCII：`mcpilot-meal-recommender`）
- **注意**：Issue 正文 ≤1000 字、不带图片。

## 2. 仓库信息建议

| 项 | 建议值 |
|---|---|
| Repository name | `mcpilot-meal-recommender` |
| Description | 🍟 真实调用麦当劳官方 MCP 的 AI Skill：按预算/营养/口味/优惠券推荐三种可解释的餐品组合（2026 麦当劳 1024 大赛参赛作品） |
| Website | （可留空，或填 GitHub Pages） |
| Topics（关键词） | `mcdonalds` `mcp` `ai-skill` `workbuddy` `meal-recommendation` `nutrition` `budget` `model-context-protocol` `1024` |
| License | MIT（已就位） |
| 可见性 | Public（报名硬性要求） |

## 3. 发布前核对清单

| # | 项 | 状态 |
|---|---|---|
| 1 | README.md（介绍/安装/使用示例/目标用户 + 截图） | ✅ |
| 2 | CONTEST_DECLARATION.md（官方原文，逐字节一致） | ✅ md5 91a7b32ec28098b609ca3f215cdf7147 |
| 3 | MCP_INTEGRATION.md | ✅ |
| 4 | mcp-config.example.json（仅占位符） | ✅ |
| 5 | workbuddy.md（WorkBuddy 专项奖励） | ✅（脱敏摘要版） |
| 6 | SKILL.md（frontmatter 官方必填字段齐全；含 Phase 4.3 规则；已可注册触发） | ✅ |
| 7 | 源代码 + 运行说明 + 测试说明 | ✅ 233 单测 + 21 集成 |
| 8 | 真实截图（docs/images/，3 张） | ✅ |
| 9 | 安全扫描（scripts/security_scan.py --strict） | ✅ 0 命中 |
| 10 | Markdown 链接/图片路径检查 | ✅ 0 断链 |
| 11 | .gitignore 覆盖 .env / 日志 / .tmp_* / .workbuddy 运行时 | ✅ |
| 12 | Git 历史泄露风险 | ✅ 仓库 0 commit（尚无历史） |
| 13 | 项目创建时间在 2025-12-25 ~ 2026-10-25 窗口内 | ✅ 2026-10-09 |

## 4. 发布步骤（确认后执行）

1. 在 GitHub 创建**公开空仓库** `mcpilot-meal-recommender`（不要初始化 README）。
2. `git remote add origin <url> && git branch -M main && git push -u origin main`
   （当前本地仓库 0 commit，首次 commit 即首条历史，无历史泄露）。
3. push 前最后跑一次：`python scripts/security_scan.py --strict` 与 `python -m pytest tests/unit -q`。
4. 按第 1 节模板提交报名 Issue，等待官方在 Issue 下回复"报名成功"。
5. 报名成功后关注 RANKING.md 榜单（Star > 0 进入排行）。

## 5. 已知限制（README 与报名材料如实声明）

1. 真实门店 3330324 的两张门店券适用商品不在该店在售菜单 → 真实"用券成功减免"无样本
  （仅合成夹具覆盖代码路径，已在测试中明确标注 SYNTHETIC）。
2. 账户券（query-my-coupons）/ 可领取券（auto-bind-coupons）属账号级或写操作 → 不调用，未验证。
3. 得来速（beType=5）缺真实 beCode 端到端样本；麦乐送 / 企业团餐未覆盖。
4. 营养覆盖率受官方数据现状限制（菜单简名 vs 营养表全名），未 100%；未命中如实标注"缺失"。
5. Skill 已可被 WorkBuddy 注册与触发（见 §8）。需注意：**WorkBuddy 5.7.7 只扫描用户级
  Skill 目录 `~/.workbuddy/skills/`，不扫描项目级 `.workbuddy/skills/`**。因此本仓库中的
  `.workbuddy/skills/…` 是**源码形态**，使用者需按 README 第六节第 8 步**复制安装到用户级目录**
  后方可被新会话自动触发；仓库内原件保留不动。

## 6. 当前 Git 状态（Phase 5 复核，2026-10-09 21:15）

| 项 | 状态 | 说明 |
|---|---|---|
| 仓库类型 | 已初始化（`git init`，开发中有意为之） | 分支 `master`，HEAD 指向未创建的 `refs/heads/master` |
| 提交数 | **0** | `git log` 报 "does not have any commits yet" |
| 引用（refs） | 空 | `git show-ref` 无输出 |
| 暂存区（index） | **空**（`git ls-files` = 0） | 已 `git reset`，无待提交内容 |
| 远端 | 无 | `git remote -v` 为空 |
| 松散对象 | 96 个 **blob**（无 tree / commit） | 来自开发中一次 `git add -A` 的暂存快照 |
| 对象内容审查 | **0 条凭据** | 逐一读取 96 个 blob，无 Bearer / JWT / 真实 Token |
| `.gitignore` 生效 | ✅ | `.tmp_phase43/` `.env` `.workbuddy/memory/` `.pytest_cache/` 均被正确忽略 |

**结论**：临时 `.git` 目录**仅是空的仓库骨架 + 无引用的文件快照**，不会被 push、
不构成历史泄露。**建议保留**（删除它属于破坏性操作，需要你明确授权；
保留也不影响本阶段审核与后续发布——发布时直接 `git add` + 首次 commit 即可）。

> 若你希望得到一个"绝对干净"的仓库，可选的**非破坏性**做法是在发布时改用新目录重新初始化，
> 而不是删除现有 `.git`。此事需要你决定，我不会自行操作。

## 7. 发布前最终复核（2026-10-09 21:15 实测）

| 项 | 结果 |
|---|---|
| 离线单元测试 | **233 passed, 1 skipped**（15.3s） |
| 真实 MCP 集成测试 | **21 passed**（76.5s，`MCPILOT_LIVE=1`） |
| 安全扫描（strict） | **P0 = 0，P1 = 0**（97 个文件） |
| Git 对象凭据扫描 | **0 / 96 个 blob 含凭据** |
| 前端语法门禁 | `node --check app.js` 通过 |
| CONTEST_DECLARATION.md | md5 `91a7b32ec28098b609ca3f215cdf7147`，**与官方逐字节一致** |
| Markdown 链接 | 全仓 0 断链 |
| 截图 | 3 张（home / result-desktop / result-mobile），真实 CDP 抓取 |

## 8. Skill 独立验收结论（2026-10-09 21:30 复测，已通过）

> **复测背景**：首次验收（T1/T2 未通过）后定位到**两个根因**，已修复并复测通过：
> ① SKILL.md frontmatter **缺少 WorkBuddy 官方必填字段 `version` / `author`**；
> ② Skill 位于**项目级** `.workbuddy/skills/`，而 WorkBuddy 5.7.7 **只扫描用户级 `~/.workbuddy/skills/`**。

| 判定 | 结果 | 证据 |
|---|---|---|
| T1 发现性 | ✅ **通过** | 复制安装到 `~/.workbuddy/skills/mcpilot-meal-recommender/` 后，注册缓存 `~/.workbuddy/.skill-list-cache.json` 出现条目：`{"name":"mcpilot-meal-recommender","filePath":"...\\.workbuddy\\skills\\mcpilot-meal-recommender\\SKILL.md","source":"userSettings","type":"prompt","version":"0.5.0","disable":false,"disableModelInvocation":false}` |
| T2 触发 | ✅ **通过** | 通过 Skill 工具调用成功加载，返回完整 SKILL.md，并回显 `Base directory for this skill: <用户级技能目录>/mcpilot-meal-recommender`（此前为 `Can not find skill`） |
| T3 内容自足 | ✅ 通过 | SKILL.md 自含 6 工具契约 / 调用顺序 / 参数规则 / 输出格式，引用文件齐备，无凭据 |
| T4 真实 MCP | ✅ 通过 | 触发后真实调用 `mcd-mcp` 完成 30 元推荐（见下），门店 3330324 实时数据 |
| T5 真实预算推荐 | ✅ **通过** | `mcpilot.recommender --store 3330324 --be-type 1 --budget 30 --json` 端到端成功，三策略产出真实结果（§8.1） |

### 8.1 T5 真实 30 元推荐实测（2026-10-09 21:30）

请求：门店 `3330324`，到店自取（beType=1），预算 ¥30，1 人，三策略（budget/nutrition/balanced）。

| 策略 | 组合 | 实付 | 营养（可靠匹配） | 关键理由 |
|---|---|---|---|---|
| **省钱优先** budget | 精选超值随心配 ×1 | **¥13.90** | 营养缺失（如实标注） | 候选方案中实付最低；单件套餐即完整一餐，标为"经济单品" |
| **高蛋白优先** nutrition | 双层吉士汉堡 ×1 | **¥23.00** | 蛋白 27.0 g｜429 kcal｜6.3 g/100kcal｜1.17 g/元 | 可靠匹配候选中蛋白效率最高 |
| **综合推荐** balanced | 双层吉士汉堡 + 圆筒冰淇淋 | **¥28.00** | 蛋白 29.0 g｜522 kcal | 综合评分 0.76：蛋白性价比 0.88｜甜点搭配度 1.00｜分量 1.00｜预算余量 0.07 |

调用统计（全部只读）：菜单 127 件 → 候选 448 → 预算内 74 → 试算 12 次；MCP 调用
`query-meals×1 / list-nutrition-foods×1 / query-store-coupons×1 / query-meal-detail×5 / calculate-price×12`，
耗时 10.5s，**0 传输失败**。价格全部来自 `calculate-price` 真实试算。

**券**：门店有券但适用商品不在本店在售菜单 → 本次均未使用（按真实编码比对判定，不套用）。

### 8.2 结论

Skill **内容验收与平台触发验收均已通过**；已可在**不依赖原开发对话上下文的新会话**中自动触发，
并真实调用麦当劳 MCP 完成 30 元预算推荐。发布材料中可如实声明"Skill 可在 WorkBuddy 中触发"，
但须同时注明**安装步骤**（复制到用户级 `~/.workbuddy/skills/`），因为平台不扫描仓库内项目级目录。
