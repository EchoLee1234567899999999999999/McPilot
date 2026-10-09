# 示例文件说明

| 文件 | 说明 |
|------|------|
| `sample-request.json` | 推荐请求的**结构示例**（不含任何真实数值或凭据） |
| `sample-recommendation.30yuan.json` | **真实运行**得到的一次推荐输出（门店 `3330324`、预算 ¥30、到店自取），用于说明结构化输出格式、供后续前端联调 |

## 关于 `sample-recommendation.30yuan.json`

- 内容由真实 MCP 调用产生（`python -m mcpilot.recommender --store 3330324 --be-type 1 --budget 30 --json`），
  **不是模拟数据**。
- 其中的价格、营养、券信息都是**采集当时**的真实快照；麦当劳的价格与活动会变化，
  重新运行时数值可能不同，请以实时调用为准。
- 文件内**不含** Token、`Authorization` 或任何账号信息（券字段已做脱敏设计，仅保留标题与适用商品编码）。
