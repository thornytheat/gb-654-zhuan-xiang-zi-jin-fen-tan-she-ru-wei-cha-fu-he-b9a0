# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |

## 财政报送（M7）

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `report_id` | string | 否 | 报送编号；缺省由契约版本与原文摘要派生，稳定不变 |
| `contract_version` | integer | 否 | 首次报送时冻结的契约版本；1=合并 `payee`，2=法人 `legal_entity`+分支 `branch` |
| `contract_generation` | integer | 否 | 冻结时的换版代数，用于区分同一版本下的多轮换版 |
| `content_digest` | string | 否 | 契约版本与原文的规范化摘要 |
| `status` | string | 否 | `PENDING` 未投递 / `ACCEPTED_NO_RECEIPT` 已受理未取回回执 / `ACCEPTED` 已取回回执 |
| `receipt_no` | string | 否 | 本地接收端回执编号，由内容摘要派生 |
| `duplicate_deliveries` | integer | 否 | 同报送同内容的重复投递次数，不重复生成回执 |
| `allocation_total` | string | 否 | 复用 M4 分摊后的已分配合计 |
| `unallocated` | string | 否 | 封顶后未分配合计 |

契约 v2 缺 `branch` 或 `legal_entity` 时拒收（`MISSING_BRANCH` / `MISSING_LEGAL_ENTITY`）。
旧报送恢复时只读取其冻结契约要求的栏目，不补写新栏。

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
