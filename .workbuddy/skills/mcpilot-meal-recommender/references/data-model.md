# 数据模型（Data Model）

本文件描述输入/输出与中间结构。字段为设计约定，**不代表任何真实数值**（数值一律来自 MCP）；
与 `src/mcpilot/models.py` 一一对应。

---

## 输入：UserRequest

| 字段 | 类型 | 说明 |
|------|------|------|
| `scene` | `"pickup" \| "drive_thru"` | 到店自取 / 得来速 |
| `be_type` | `1 \| 5` | 显式场景：1=到店自取，5=得来速 |
| `be_code` | `str?` | 得来速必填（来自门店查询） |
| `city` / `keyword` | `str?` | 按位置搜索门店时必填 |
| `store_code` | `str?` | 用户直接指定的门店 |
| `budget` | `float?` | 预算上限（元） |
| `people` | `int` | 用餐人数（默认 1），决定最少份数 |
| `goals` | `NutritionGoals` | 营养目标：`protein_g_min` / `energy_kcal_max` / `energy_kcal_target` / `fat_g_max` |
| `likes` | `list[str]` | 口味偏好关键词（命中加分） |
| `dislikes` | `list[str]` | 忌口关键词（命中淘汰） |
| `include_codes` | `list[str]` | 必须包含的商品编码（不在售则报错） |
| `use_coupon` | `bool` | 是否尝试用券，默认 `true` |
| `strategies` | `list[str]` | 要计算的策略，默认三种全算 |
| `max_verify` | `int` | `calculate-price` 试算次数上限（默认 12） |
| `max_resolve` | `int` | `query-meal-detail` 按需解析次数上限（默认 10） |
| `taste_prefs` | `list[str]` | 兼容字段，等价于 `likes` |

## 中间结构

### Store
`store_code`、`be_code?`（得来速）、`name`、`address`、`reservation_options?`

### MenuItem
`code`、`name`、`price_yuan`（现价，元）、`original_price_yuan`、`tags`、`can_with_order`

### Nutrition
`name`、`energy_kcal`、`protein_g`、`fat_g`、`carb_g`、`sodium_mg`、`calcium_mg`
> 任一核心字段缺失即视为"营养未知"（`is_complete()` 为假）。

### MealDetail / SetComponent（套餐组成，Phase 3 新增）
- `MealDetail`：`code`、`name`（**规范全名**）、`support_modify`、`image`、`modifications[]`、`components[]`。
- `SetComponent`：`name`（**规范全名**）、`code`、`quantity`。
> 来自 `query-meal-detail`。`components[]` 仅取 `rounds[]` 中 `isDefault==1` 的默认组成，
> 用于在"套餐整体营养"缺失时，按其**逐项组成**加权合计（见 `composition_nutrition`）。

### Coupon
`coupon_id`、`coupon_code`、`title`、`trade_date_time`、`product_codes`、`product_names`、`promotion_id`、
`source`、`valid_from`、`valid_to`
> - **是否适用于某组合**：只看 `product_codes` 与组合商品编码是否**相交**（不套用名称近似）。
> - `source`：`store`（门店券，`query-store-coupons`，本项目真实调用）/ `account`（账户券，
>   `query-my-coupons`，账号级接口，**不调用**）/ `claimable`（可领取券，`auto-bind-coupons`，写操作，**不调用**）。
> - `valid_from` / `valid_to` 由 `trade_date_time` 解析为**时间窗**；`coupon.status()` 据此返回
>   `active` / `expired` / `not_started` / `unknown`，`CouponMatch.time_status` 决定其是否 `usable`。
> - 序列化时 `coupon_id` / `coupon_code` **不外露**（属账号相关内容）。

### Candidate（组合候选，仅生成期）
`items`、`est_subtotal_cent`（展示价估算，**仅供筛选**）、`roles`、`is_set`、`single_item`

### Quote（价格试算）
`original_price_cent`、`discount_cent`、`payable_cent`、`lines[]`（全部来自 `calculate-price`，单位分）

### NutritionMatch（营养匹配结果，Phase 3 新增）

`ok`、`tier`、`matched_name`、`nutrition`、`evidence`、`components[]`。
`tier` 为**分层匹配**的命中层级，见下（`nutrition.py`）：

| tier | 含义 | 是否需额外调用 | 可靠性 |
|------|------|----------------|--------|
| `exact` | 菜单名与营养表名**完全一致** | 否 | 高 |
| `normalized` | 归一化后一致（全/半角、空白、括号、™®© 差异） | 否 | 高 |
| `alias` | 命中**可审计别名表** `NUTRITION_ALIASES`（含包装说明、套餐简称） | 否 | 高 |
| `canonical` | 经 `query-meal-detail` 得到**规范全名**后精确/归一化命中 | 是（有界） | 高 |
| `composition` | 无整体营养，但按套餐**默认组成**逐项匹配并加权合计 | 是（有界） | 高（全部组件可匹配时） |

> **严格性约定**：只有**同一商品、同一规格**才允许匹配；**绝不**用"套餐 ⊇ 单品"这类包含关系近似。
> 组合营养**仅当组合内每一件都可可靠匹配**时才给出；否则该组合 `nutrition=null`、`nutrition_complete=false`，
> 并将其名称计入 `nutrition_missing`（**不猜测、不按 0 计**）。

## 输出：Recommendation

| 字段 | 类型 | 说明 |
|------|------|------|
| `strategy` | `"budget" \| "nutrition" \| "balanced"` | 使用策略 |
| `label` | `str` | 策略中文名 |
| `items[]` | `RecommendedItem` | `code`/`name`/`quantity`/`role`/逐行金额/单项营养 |
| `price` | `object` | `original_*` / `discount_*` / `payable_*`（同时给出 `*_cent` 与 `*_yuan`） |
| `nutrition` | `Nutrition?` | 组合营养合计；**仅当全部商品营养可靠**时给出，否则为 `null` |
| `nutrition_missing` | `list[str]` | 营养未知的商品名（如实标注） |
| `nutrition_complete` | `bool` | 营养是否完整可定量 |
| `coupon` | `Coupon?` | 实际使用/试算的券 |
| `coupon_note` | `str` | 用券说明 |
| `score` | `float` | 该策略下的得分 |
| `reasons` | `list[str]` | 推荐理由（可追溯到真实数据） |
| `notes` | `list[str]` | 数据缺失 / 降级提示 |
| `data_source` | `str` | 数据来源与所用工具 |
| `queried_at` | `str` | 数据查询时间（来自 MCP 返回的 `datetime`） |

## 输出：RecommendationPlan

`store_code`、`be_type`、`be_code`、`request`、`recommendations[]`、`warnings[]`、
`suggestions[]`、`adjustments[]`（Phase 4.2）、`stats`（含各工具真实调用次数）、
`data_source`、`queried_at`。
可用 `plan.to_json(...)` 直接序列化为 JSON 供前端消费。

### Adjustment（结构调整建议，Phase 4.2 新增）

仅当**无可行方案**时生成（最多 3 条），全部依据**本次真实候选与真实试算**：

| 字段 | 说明 |
|------|------|
| `kind` | `budget` / `kcal_max` / `protein_min` / `fat_max` / `nutrition_goal` / `dislikes` / `menu` / `retry` / `generic` |
| `field` | 调整的字段中文名（如「热量上限」） |
| `current` / `suggested` | 当前值 / 建议值（展示用字符串） |
| `reason` | 数据依据说明（含本次候选统计） |
| `patch` | 可写回表单的最小变更（键如 `budget`、`goals.energy_kcal_max`；`null` 表示移除该条件） |
| `severity` | `info` / `warn` |
| `text` | 单行人类可读文本（`suggestions[]` 即由它派生，向后兼容） |

**硬约束**：生成建议不改请求、不自动重查、不静默放宽条件；
数值建议只采用"**仅因该条件**被剔除"的候选（放宽一项即可纳入），
无可靠数值时退回通用提示，绝不编造热量 / 蛋白质 / 价格。
`stats.blocked` 为机器可读的阻断归因计数（`over_budget`/`dislike`/`kcal`/`protein`/`fat`/`nutrition_unknown`/`verify_failed`）。

## 单位约定

- 内部计算：金额以**分**（整数）避免浮点误差。
- 对外展示：金额同时给出**分**与**元**（元保留 2 位）。
- 营养：能量 kcal、蛋白质/脂肪/碳水 g、钠/钙 mg（以 MCP 返回口径为准）。
