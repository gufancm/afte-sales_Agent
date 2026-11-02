# Agent Decision Accuracy（2026-10-30）

- 数据：`task_success_v2.v2.5.jsonl`
- 有效用例：**100**（SKIP 与缺少 trace 的不计入）

| 维度 | 准确率 | 适用样本 | 不适用 |
| --- | --- | --- | --- |
| **Decision（严格全对）** | **99/100 = 99.0%** | 100 | 0 |
| intent | 99/100 = 99.0% | 100 | 0 |
| policy | 78/78 = 100.0% | 78 | 22 |
| action | 22/22 = 100.0% | 22 | 78 |
| authorization | 34/34 = 100.0% | 34 | 66 |
| route | 100/100 = 100.0% | 100 | 0 |

## 失败归因

- I 意图映射：1 条（`TS-090`）

## 失败用例明细（1）

- `TS-090`（投诉和人工转接）：intent REFUND_ONLY != UNKNOWN
