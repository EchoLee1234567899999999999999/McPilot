# MCP 工具清单与调用顺序（MCP Tools）

> 项目：McPilot · 麦门决策局 ｜ 版本：v0.1（Phase 0）
> 本文档**仅使用已核验的麦当劳 MCP 工具**，参数以平台 schema 为准，不做臆测。

---

## 1. 工具总览（当前阶段仅用只读工具）

| # | 工具名 | 作用 | 读/写 |
|---|--------|------|-------|
| 1 | `query-nearby-stores` | 查询可点餐门店（到店自取 / 得来速） | 只读 |
| 2 | `query-meals` | 查询门店餐品 / 套餐列表 | 只读 |
| 3 | `query-meal-detail` | 查询餐品详情与可特调项 | 只读 |
| 4 | `list-nutrition-foods` | 获取常见餐品营养数据 | 只读 |
| 5 | `query-store-coupons` | 查询"指定门店 + 订单类型"可用优惠券 | 只读 |
| 6 | `calculate-price` | 计算商品价格（含优惠） | 只读 |

在 WorkBuddy 中，以上工具以 `mcp__mcd-mcp__<tool-name>` 形式暴露（例如 `mcp__mcd-mcp__query-meals`）。

---

## 2. 统一参数规则（关键）

### 2.1 `beType` / `orderType` / `beCode`

| 场景 | `beType` | `orderType` | `beCode` 传递 |
|------|----------|-------------|----------------|
| 到店自取 | 1 | 1 | **不传**（"传了会报错"） |
| 得来速 DT | 5 | 1 | **必传**（来自 `query-nearby-stores`） |
| 麦乐送 | 2 | 2 | 必传（来自 `delivery-query-stores`，Phase 0 不覆盖） |
| 企业团餐 | 6 | 2 | 必传（来自 `delivery-query-stores`，Phase 0 不覆盖） |

### 2.2 预约

`reservationDate` 仅**预约场景**必传，非预约不传；格式 `yyyy-MM-dd HH:mm`。

### 2.3 金额单位

`calculate-price` 返回的价格单位为**分**；展示时**除以 100 转元**，保留 2 位小数。

---

## 3. 各工具调用契约

### 3.1 `query-nearby-stores`

- **必传**：`beType`（1/5）、`searchType`（1 收藏 / 2 按位置）
- **searchType=2 时必填**：`city`、`keyword`
- **返回**：门店列表；`beType=5` 的门店含 `beCode`，`beType=1` 无 `beCode`
- **注意**：`reservation=true` 时完整展示 `reservationTimeOptions`，`today=true` 标注"(今天)"
- **下一步**：展示 `storeCode`（及得来速的 `beCode`）并引导用户选择

### 3.2 `query-meals`

- **必传**：`storeCode`、`orderType`、`beType`
- **条件必传**：得来速/外送/团餐传 `beCode`；预约场景传 `reservationDate`
- **返回**：餐品 / 套餐列表（含商品 `code`、名称、可随单购标记等）

### 3.3 `query-meal-detail`

- **必传**：`storeCode`、`orderType`、`beType`、`code`
- **条件必传**：得来速/外送/团餐传 `beCode`
- **展示规则**：`data.supportModify=true` → 名称后标【可特调】；`choice.supportModify=true` → 该 choice 标【可特调】；**不主动展开**特调项；结尾总结默认选中搭配（`isDefault=1`）

### 3.4 `list-nutrition-foods`

- **入参**：无
- **返回**：常见餐品营养数据（能量、蛋白质、脂肪、碳水化合物、钠、钙等）
- **用途**：营养约束筛选与打分

### 3.5 `query-store-coupons`

- **必传**：`storeCode`、`orderType`、`beType`
- **条件必传**：得来速/外送/团餐传 `beCode`；预约场景传 `reservationDate`
- **返回**：该门店 + 订单类型下可使用的优惠券

### 3.6 `calculate-price`

- **必传**：`storeCode`、`orderType`、`beType`
- **items[]**：`productCode` + `quantity`（≥1）；可选 `couponId`、`couponCode`、`modification`、`roundList`
- **其他**：`needTableware`、`withOrder`、`gmServiceCode`（团餐）
- **返回**：含优惠的价格明细；成功后可引导"创建订单"（**本项目当前阶段不调用下单**）

---

## 4. 标准调用顺序（到店自取 / 得来速）

```
① 确认场景（自取 or 得来速）
        │
        ▼
② query-nearby-stores          → 选定门店（拿到 storeCode / beCode）
        │
        ▼
③ query-meals                  → 门店在售餐品/套餐
        │
        ├──④ query-meal-detail  → （按需）套餐组成 / 特调
        │
        ▼
⑤ list-nutrition-foods         → 营养约束筛选与打分
        │
        ▼
⑥ query-store-coupons          → 门店+订单类型可用券
        │
        ▼
⑦ 组合生成（三种策略打分）
        │
        ▼
⑧ calculate-price              → 逐个组合试算（含券 / 不含券）
        │
        ▼
⑨ 输出最终推荐（Top-N）
```

- ③④⑤⑥ 中，营养与券查询可并行；**⑧ 价格试算必须最后执行**（依赖门店、场景、餐品 code、券）。
- 全流程**只读**，不进入下单环节。

### 4.1 Phase 2 的调用预算（实现约定）

推荐引擎对调用次数做了明确约束，避免"组合爆炸 × 逐个试算"：

| 阶段 | 调用 | 次数 |
|------|------|------|
| 取数 | `query-meals` / `list-nutrition-foods` / `query-store-coupons` | 各 **1** 次（固定） |
| 粗筛 | 用**展示价 + 营养表**生成并筛选候选组合 | **0** 次（不消耗 MCP） |
| 真实试算 | `calculate-price`，仅对轮转选出的少量候选 | ≤ `max_verify`（默认 **12**） |

- 试算候选的选取采用"轮转"，分别覆盖：最便宜、蛋白最高、口味命中、券适用商品，
  确保三种策略都有对应的候选被真实验证。
- 最终"实付 / 优惠"一律取自 `calculate-price`；菜单展示价只用于粗筛。

---

## 5. 禁用清单（当前阶段绝对不调用）

| 类别 | 工具（示例） |
|------|--------------|
| 下单类 | `create-order`、`mall-create-order`、`party-order-create` |
| 取消/退款 | `cancel-order` |
| 领券类 | `auto-bind-coupons`、`query-survey-coupon` 等写入型领券 |
| 抽奖 / 积分 | `draw-lottery`、`mall-points-products`、`mall-create-order` |
| 地址写入 | `delivery-create-address` |

> 原则：**看与算可以，改与买不行**。一旦涉及资产或订单变更，一律不做。

---

## 6. 错误与降级约定

| 工具 | 常见异常 | 处理 |
|------|----------|------|
| `query-nearby-stores` | 无收藏 / 无结果 | 回退按位置搜索；仍无则引导更换城市或关键词 |
| `query-meals` | 门店不存在 / 场景参数错误 | 校验 `beType`/`orderType`/`beCode` 传参规则 |
| `query-meal-detail` | `code` 失效 | 剔除该餐品并说明 |
| `list-nutrition-foods` | 某餐品无营养数据 | 标注"营养未知"，营养策略降权/排除 |
| `query-store-coupons` | 无可用券 | 正常返回，注明"无可用券"，不阻断 |
| `calculate-price` | 商品下架 / 参数错误 | 剔除该组合，继续试算其余组合 |

> 所有异常都必须**如实告知用户**，严禁以编造数据填补。

---

## 7. 真实响应格式实录（Phase 1 实测，2026-10-09）

> 本节为**实测记录**，用于解析实现与排查；示例数值仅为当次快照。

### 7.1 响应信封

`tools/call` 的返回统一为：

```json
{"jsonrpc":"2.0","id":N,"result":{"content":[{"type":"text","text":"<markdown...>"}],"isError":false}}
```

其中 `content[0].text` 是一段 markdown，**业务 JSON 嵌在 `## Original Response` 之后**（部分工具无该小标题）。
因此解析需**括号配对**截取（见 `mcpilot.mcp_client.extract_json_payload`），不可依赖固定标题。
业务 JSON 形如 `{"success":true,"code":200,"message":"请求成功","datetime":"...","traceId":"...","data":...}`。

### 7.2 各工具 `data` 形状

| 工具 | `data` 形状 | 金额单位 |
|------|-------------|----------|
| `query-nearby-stores` | 数组：`storeCode`/`storeName`/`address`/`distance`/`reservation`/`reservationTimeOptions[]`（得来速另有 `beCode`） | — |
| `query-meals` | 对象：`categories[]{name,meals[]{code,tags}}` + `meals{code:{name,image,currentPrice,originalPrice,discountType,canWithOrder,withOrder}}` | **元（字符串）** |
| `query-meal-detail` | 对象：`code`/`name`/`image`/`supportModify`/`rounds[]`/`modification.items[].values[]`（含 `selectedKey`/`unselectedKey`） | — |
| `list-nutrition-foods` | **字符串**（TSV）：`[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:` 后每行 2 空格缩进的逗号分隔记录 | — |
| `query-store-coupons` | 数组：`title`/`couponId`/`couponCode`/`tradeDateTime`/`products[]{productCode,productName}`/`promotionId` | — |
| `calculate-price` | 对象：`productOriginalPrice`/`productPrice`/`originalPrice`/`discount`/`price`/`productList[]{productCode,productName,quantity,originalSubtotal,subtotal}`/`takeWayList[]` | **分（整数）** |

### 7.3 关键坑位

1. **金额单位不统一**：`query-meals` 为「元」字符串；`calculate-price` 为「分」整数。**必须分别处理**。
2. **营养表名称与菜单名称不完全一致**：菜单"薯条"在营养表只有"中/大/小薯条"→ 属**歧义**，按规则标注"营养未知"，不猜测。
3. **券的适用商品**：`products[].productCode` 决定券能用于哪些商品；本例门店券指向"麦旋风任选/薯薯任选"专有编码，**对巨无霸不适用**，必须排除。
4. **`calculate-price` 的 `discount`** 才是真实优惠金额；不得自行估算。

