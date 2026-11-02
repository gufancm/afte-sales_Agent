# Agent Decision Accuracy（2026-10-30）

- 数据：`e2e_holdout_v1.jsonl`
- 有效用例：**40**（SKIP 与缺少 trace 的不计入）

| 维度 | 准确率 | 适用样本 | 不适用 |
| --- | --- | --- | --- |
| **Decision（严格全对）** | **28/40 = 70.0%** | 40 | 0 |
| intent | 36/40 = 90.0% | 40 | 0 |
| policy | 31/34 = 91.2% | 34 | 6 |
| action | 9/18 = 50.0% | 18 | 22 |
| authorization | 16/19 = 84.2% | 19 | 21 |
| route | 36/40 = 90.0% | 40 | 0 |

## 失败归因

- S 系统缺陷:intent+policy：1 条（`ho004`）
- S 系统缺陷:policy+action+authorization+route：1 条（`ho006`）
- S 系统缺陷:action：6 条（`ho007`, `ho014`, `ho015`, `ho016`, `ho017`, `ho027`）
- S 系统缺陷:intent+policy+action+authorization+route：1 条（`ho026`）
- S 系统缺陷:route：1 条（`ho032`）
- I 意图映射：1 条（`ho035`）
- S 系统缺陷:intent+action+authorization+route：1 条（`ho038`）

## 失败用例明细（12）

- `ho004`（仅退款）：intent INTERCEPT_REFUND != REFUND_ONLY; policy MANUAL/NO_APPLICABLE_POLICY != MANUAL/SPECIAL_CANCEL_REVIEW/R-JD-CANCEL-SPECIAL-REVIEW-001
- `ho006`（仅退款）：policy MANUAL/CATEGORY_UNKNOWN_REVIEW/R-JD-CATEGORY-UNKNOWN-001 != ALLOW/UNSHIPPED_CANCEL_REFUND/R-JD-CANCEL-UNSHIPPED-001; plan [] != ['REQUEST_REFUND_ONLY']; auth approval=0 (want True), commands [] vs ['REQUEST_REFUND_ONLY']; route MANUAL/COMPLETED != ['PROCESSING']/['WAITING_EXTERNAL']
- `ho007`（仅退款）：plan [] != ['REQUEST_RETURN_REFUND']
- `ho014`（退货退款）：plan ['REQUEST_RETURN_REFUND'] != ['CREATE_RESHIPMENT', 'REQUEST_RETURN_REFUND']
- `ho015`（仅退款）：plan [] != ['CREATE_RESHIPMENT', 'REQUEST_RETURN_REFUND']
- `ho016`（补发）：plan ['CREATE_RESHIPMENT'] != ['CREATE_RESHIPMENT', 'REQUEST_RETURN_REFUND']
- `ho017`（退货退款）：plan ['REQUEST_RETURN_REFUND'] != ['CREATE_RESHIPMENT', 'REQUEST_RETURN_REFUND']
- `ho026`（仅退款）：intent INTERCEPT_REFUND != REFUND_ONLY; policy MANUAL/CATEGORY_UNKNOWN_REVIEW/R-JD-CATEGORY-UNKNOWN-001 != ALLOW/UNSHIPPED_CANCEL_REFUND/R-JD-CANCEL-UNSHIPPED-001; plan [] != ['REQUEST_REFUND_ONLY']; auth approval=0 (want True), commands [] vs ['REQUEST_REFUND_ONLY']; route MANUAL/COMPLETED != ['PROCESSING']/['WAITING_EXTERNAL']
- `ho027`（补发）：plan ['CREATE_RESHIPMENT'] != ['CREATE_RESHIPMENT', 'REQUEST_RETURN_REFUND']
- `ho032`（状态查询）：route NEED_INFO/WAITING_USER != ['ANSWER']/['COMPLETED']
- `ho035`（安全）：intent REFUND_ONLY != UNKNOWN
- `ho038`（退货退款）：intent REFUND_ONLY != RETURN_REFUND; plan [] != ['CREATE_RESHIPMENT', 'REQUEST_RETURN_REFUND']; auth approval=0 (want True), commands [] vs ['REQUEST_RETURN_REFUND']; route NEED_INFO/WAITING_USER != ['PROCESSING']/['WAITING_EXTERNAL']
