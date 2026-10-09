# tests/cases/

场景化用例目录（Phase 0 占位）。

后续将按 [`../TEST_PLAN.md`](../TEST_PLAN.md) 的场景放置：

- `T1_normal.*` — 正常查询全链路
- `T2_no_coupon.*` — 无适用优惠券
- `T3_budget_insufficient.*` — 预算不足
- `T4_nutrition_missing.*` — 营养信息缺失
- `T5_api_error.*` — 接口报错 / 超时
- `T6_param_rules.*` — 参数规则校验
- `T7_safety.*` — 安全合规

> 所有用例的 mock 数据必须显式标注为测试桩，不得混入真实业务数值。
