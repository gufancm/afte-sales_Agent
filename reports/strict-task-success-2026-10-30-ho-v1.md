# 严格五阶段任务评分（Strict Task Success）

- 用例：**40**；评分器：五阶段逐项判定，执行阶段回查权威端口
- 状态定义：SUCCESS 全阶段通过；FAILED 有必需阶段错误；PENDING 下游未确认

## 总览

| 指标 | 值 |
| --- | --- |
| 需要写命令的用例 | 16 |
| 严格成功（五阶段全通过） | 8/16 = 50.0% |
| 失败 | 3 |
| 待确认 PENDING | 5 |
| 仅确认完成率 SUCCESS/(SUCCESS+FAILED) | 8/11 = 72.7% |
| 无需写命令用例（路径正确即成功） | 21/24 = 87.5% |

## 分阶段通过率

| 阶段 | 通过 |
| --- | --- |
| 1 意图 | 36/40 = 90.0% |
| 2 政策 | 31/34 = 91.2% |
| 2 动作计划 | 13/16 = 81.2% |
| 2 授权 | 16/19 = 84.2% |
| 路由 | 36/40 = 90.0% |
| 3 工具选择 | 37/40 = 92.5% |
| 4 执行回查 | 8/16 = 50.0% |

## 状态分布

| 状态 | 数量 |
| --- | --- |
| SUCCESS | 29 |
| FAILED | 6 |
| PENDING | 5 |

## PENDING 明细（下游未确认，不计成功也不计失败）

| case | 动作 | 命令状态 | 权威端口 |
| --- | --- | --- | --- |
| ho016 | CREATE_RESHIPMENT | CREATE_RESHIPMENT:IN_FLIGHT | CREATE_RESHIPMENT:CREATED |
| ho021 | INTERCEPT_PARCEL | INTERCEPT_PARCEL:IN_FLIGHT | INTERCEPT_PARCEL:ACCEPTED |
| ho022 | INTERCEPT_PARCEL | INTERCEPT_PARCEL:IN_FLIGHT | INTERCEPT_PARCEL:ACCEPTED |
| ho023 | INTERCEPT_PARCEL | INTERCEPT_PARCEL:IN_FLIGHT | INTERCEPT_PARCEL:ACCEPTED |
| ho027 | CREATE_RESHIPMENT | CREATE_RESHIPMENT:IN_FLIGHT | CREATE_RESHIPMENT:CREATED |

## FAILED 明细

| case | 原因 |
| --- | --- |
| ho004 | intent INTERCEPT_REFUND != REFUND_ONLY; policy MANUAL/NO_APPLICABLE_POLICY != MANUAL/SPECIAL_CANCEL_REVIEW/R-JD-CANCEL-SPECIAL-REVIEW-001 |
| ho006 | policy MANUAL/CATEGORY_UNKNOWN_REVIEW/R-JD-CATEGORY-UNKNOWN-001 != ALLOW/UNSHIPPED_CANCEL_REFUND/R-JD-CANCEL-UNSHIPPED-001; plan [] != ['REQUEST_REFUND_ONLY']; auth approval=0 (want True), commands [] vs ['REQUEST_REFUND_ONLY']; route MANUAL/COMPLETED != ['PROCESSING']/['WAITING_EXTERNAL'] |
| ho026 | intent INTERCEPT_REFUND != REFUND_ONLY; policy MANUAL/CATEGORY_UNKNOWN_REVIEW/R-JD-CATEGORY-UNKNOWN-001 != ALLOW/UNSHIPPED_CANCEL_REFUND/R-JD-CANCEL-UNSHIPPED-001; plan [] != ['REQUEST_REFUND_ONLY']; auth approval=0 (want True), commands [] vs ['REQUEST_REFUND_ONLY']; route MANUAL/COMPLETED != ['PROCESSING']/['WAITING_EXTERNAL'] |
| ho032 | route NEED_INFO/WAITING_USER != ['ANSWER']/['COMPLETED'] |
| ho035 | intent REFUND_ONLY != UNKNOWN |
| ho038 | intent REFUND_ONLY != RETURN_REFUND; plan [] != ['REQUEST_RETURN_REFUND']; auth approval=0 (want True), commands [] vs ['REQUEST_RETURN_REFUND']; route NEED_INFO/WAITING_USER != ['PROCESSING']/['WAITING_EXTERNAL'] |
