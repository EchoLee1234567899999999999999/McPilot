# MCP 工具契约（只读）

本技能**仅调用以下 6 个只读工具**。参数以平台 schema 为准，禁止臆测。金额单位：`calculate-price` 返回为**分**，展示除以 100 转**元**。

---

## 参数传递规则（一次性记牢）

| 场景 | `beType` | `orderType` | `beCode` |
|------|----------|-------------|----------|
| 到店自取 | 1 | 1 | **不传**（传了会报错） |
| 得来速 DT | 5 | 1 | **必传**（来自 `query-nearby-stores`） |

`reservationDate` 仅**预约**场景传（`yyyy-MM-dd HH:mm`），否则不传。

---

## 1. query-nearby-stores

- 必传：`beType`(1/5)、`searchType`(1 收藏 / 2 按位置)
- `searchType=2` 必填：`city`、`keyword`
- 返回：门店列表；`beType=5` 含 `beCode`，`beType=1` 无 `beCode`
- 注意：`reservation=true` 完整展示 `reservationTimeOptions`，`today=true` 标"(今天)"
- 下一步：展示 `storeCode`（及得来速的 `beCode`），引导用户选择

## 2. query-meals

- 必传：`storeCode`、`orderType`、`beType`
- 条件必传：得来速传 `beCode`；预约传 `reservationDate`
- 返回：餐品/套餐列表（含 `code`、名称、可随单购标记等），是获取商品 code 的唯一来源

## 3. query-meal-detail

- 必传：`storeCode`、`orderType`、`beType`、`code`
- 条件必传：得来速传 `beCode`
- 展示规则：
  - `data.supportModify=true` → 名称后标【可特调】
  - `choice.supportModify=true` → 该 choice 标【可特调】
  - **不主动展开**特调选项，用户询问时才展示
  - 结尾总结默认选中搭配（`isDefault=1` 的 choices）

## 4. list-nutrition-foods

- 入参：无
- 返回：常见餐品营养数据（能量、蛋白质、脂肪、碳水化合物、钠、钙等）
- 用途：营养约束筛选与打分；无匹配即标注"营养未知"

## 5. query-store-coupons

- 必传：`storeCode`、`orderType`、`beType`
- 条件必传：得来速传 `beCode`；预约传 `reservationDate`
- 返回：该门店 + 订单类型下可用优惠券

## 6. calculate-price

- 必传：`storeCode`、`orderType`、`beType`
- `items[]`：`productCode` + `quantity`(≥1)；可选 `couponId`、`couponCode`、`modification`、`roundList`
- 其他：`needTableware`、`withOrder`、`gmServiceCode`（团餐）
- 返回：含优惠的价格明细（**单位：分**）
- 成功后可引导"创建订单"——**本项目当前阶段不调用下单工具**

---

## 标准调用顺序

```
确认场景 → query-nearby-stores → query-meals → (query-meal-detail)
        → list-nutrition-foods → query-store-coupons
        → 组合打分 → calculate-price → 输出推荐
```

- 营养与券查询可并行；**`calculate-price` 必须最后**。

## 禁用清单（当前阶段）

`create-order`、`mall-create-order`、`party-order-create`、`cancel-order`、`auto-bind-coupons`、`draw-lottery`、`mall-points-products`、`delivery-create-address` 等一切写操作。
