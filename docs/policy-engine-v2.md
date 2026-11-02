# Policy Engine v2：五项前置修正与 120 条金标

> 适用版本：`policy-engine/2.0`、`pe-ruleset/2`、政策版本 `2026.10-jd`
> 代码：`app/policy/{facts,dsl,engine,library,repository,db_repository}.py`
> 数据：`policy/rules.json`（25 条规则）、`policy/clauses_jd.json`（205 条真实条款）
> 评测：`tests/eval/run_policy.py`（120 条）＋`tests/eval/make_policy_review.py`（复核表）

本次没有回退任何扩展，只把“业务能力补齐”做成**可验证、可复现、可审计**的形态。下面按评审提出的 5 个问题逐条说明落地方式。

---

## 1. 时间口径：只认两个原始事实，不再用“整数天数”

### 之前的问题

`days_since_delivery = (as_of - delivered_at).days + 1` 这种写法有两个隐患：

1. 用“当前系统时间”当申请时点，重放/补算时同一订单会得到不同结论；
2. 把不同政策的起算方式混成一个口径，烘焙“签收次日 0 点起算 15 天”会被算错。

### 现在的做法（`app/policy/facts.py`）

| 事实 | 含义 | 来源 |
| --- | --- | --- |
| `order.delivered_at` | 签收时刻（带时区） | OMS 订单快照（系统权威事实） |
| `claim.claim_at` | 用户提出申请的时刻（带时区） | 消息入口写入 `state.claim_at`，可显式传入以支持回放 |
| `claim.day_index_signed` | 派生：签收日算第 1 天 | 由上面两个事实计算 |
| `claim.day_index_next` | 派生：签收次日 0 点算第 1 天 | 由上面两个事实计算 |

- 两个原始时间统一换算到 **东八区**，按自然日计算；任一缺失、不可解析、申请早于签收 → 派生值为 `None`，**绝不当作 0**。
- `day_index` 是唯一计算函数；规则只做纯比较，不做时间运算。
- 申请时点在 API 入口固定：`POST /api/v1/tickets/{id}/messages` 可传 `claim_at`（回放/补录），缺省取东八区当前时间，写进 `state.claim_at`，引擎不再调用 `now()`。
- 事实补齐统一在引擎入口 `enrich_facts()` 完成：品类归一化 + 两个 day_index 派生。节点、评测、回放走同一口径。

### 边界验证（节选，全部在 120 条金标里）

| 场景 | 签收 | 申请 | 结论 |
| --- | --- | --- | --- |
| 7 天无理由第 7 天 23:59 | 10-01 00:00 | 10-07 23:59 | ALLOW |
| 第 8 天 00:00 | 10-01 00:00 | 10-08 00:00 | DENY |
| 评审原例 | 10-01 23:50 | 10-08 23:55 | DENY（SIGN_DATE=8） |
| 烘焙第 15 天（次日口径） | 10-01 23:50 | 10-16 23:55 | ALLOW（NEXT_DAY=15，按签收日口径会误判第 16 天） |
| 烘焙第 16 天 | 10-01 00:00 | 10-17 00:00 | DENY |
| UTC 输入跨零点 | 09-30 16:30Z | 10-07 15:00Z | ALLOW（东八区第 7 天） |

对应用例：`POL-D01`–`POL-D20`。

---

## 2. 裁决顺序：用显式阶段取代“一个大 priority”

### 阶段定义（`app/policy/dsl.py`）

| stage | 名称 | 语义 | 典型规则 |
| --- | --- | --- | --- |
| 0 | `FACT_GUARD` | 事实缺失/无效守卫 | 预留（当前由 blocked 机制实现） |
| 10 | `MANDATORY_LIMIT` | 强制限制、特殊例外、必须人工签核 | 生鲜/定制/数码/图书无理由例外、未知品类、特殊品类取消 |
| 20 | `EXPLICIT_DENY` | 明确拒绝 | 超 7 天、烘焙超 15 天、已签收不能拦截 |
| 30 | `GENERAL_ALLOW` | 一般允许 | 质量问题退货、未发货取消、在途拦截、赔付 |
| 40 | `FALLBACK_MANUAL` | 兜底人工（有明确允许规则时让位） | 签收异常、安装纠纷、烘焙兜底 |

裁决算法：

1. 先按 `stage` 升序；
2. 同阶段按 `priority` 降序；
3. 再按 `specificity`（条件数）降序；
4. 仍并列且**结论不同** → `MANUAL / POLICY_CONFLICT`（不默认放行）；并列且结论相同 → 取 `rule_id` 最小者，保证确定性。

此外还有一道**兜底不变量**：若最终胜出是 `ALLOW`，但命中集合里存在任何 `DENY`，强制改判该 `DENY`，并在 trace 写 `forced_by=deny_overrides_allow`。理论上不可达，用于防御未来规则改错。

### 冲突示例

`POL-B17`：生鲜、无理由、第 3 天。窗口允许规则（stage 30）与生鲜例外（stage 10）同时命中 → **DENY**。
`POL-F05`：未知品类 + 无理由第 2 天 → 未知品类（stage 10, MANUAL）压过通用允许 → **MANUAL**。
`POL-F11`：烘焙第 15 天 → 窗口允许（30）压过兜底人工（40）→ **ALLOW**。

---

## 3. 金额：区分“政策上限”和“自动执行阈值”

### 字段

- 统一使用整数分：`finance.requested_amount_minor`（用户申请金额，单位：分）。
- 规则侧：`amount_cap_minor` + `amount_cap_type`。

| `cap_type` | 超限结果 | 含义 |
| --- | --- | --- |
| `POLICY_LIMIT` | `DENY / AMOUNT_EXCEEDS_POLICY_LIMIT` | 政策规定的业务金额上限，超了就是不符合政策 |
| `AUTO_EXEC_THRESHOLD` | `MANUAL / AMOUNT_EXCEEDS_AUTO_THRESHOLD` | 用户**仍有资格**，只是必须人工审批（`requires_manual_approval=True`） |

- 现有两条赔付规则（`R-JD-COMP-LATE-001`、`R-JD-COMP-OOS-001`，上限 2000 分）设为 `AUTO_EXEC_THRESHOLD`，因为条款写的是“扣收违约金 6 元/20 元”，是平台对商家的处置标准，不等于消费者单笔赔付的业务上限。
- **金额缺失**：不改变业务资格（`decision` 仍是 ALLOW），但置 `execution_blocked=True` + `requires_manual_approval=True`；`action_preparation` 在进入资金执行前直接返回 `POLICY_EXECUTION_BLOCKED`，必须等 Finance 报价补齐金额。
- 金额检查结果全部落 trace：`amount_check = {status: WITHIN|EXCEEDED|UNKNOWN, amount_minor, amount_source, cap_minor, cap_type}`。

对应用例：`POL-E01`–`POL-E10`。

> 已知覆盖不足：205 条真实条款里没有“消费者赔付金额上限”的表述，因此 `POLICY_LIMIT` 目前只有单元测试覆盖（`test_policy_limit_over_cap_denies`），120 条金标不虚构该类业务上限。

---

## 4. 缺事实的安全兜底：blocked 规则参与竞争

### 机制

对每条规则，若“除缺失事实外其余条件全部成立”，标记为 **blocked**，并把缺失条件映射回**原始输入事实**：

| 派生事实 | 原始输入 | 缺失时归类 |
| --- | --- | --- |
| `claim.day_index_signed` / `_next` | `order.delivered_at` + `claim.claim_at` | 原始输入缺失 → 用户可补 / 系统权威 |
| `claim.reason_code` | 自身 | 用户可补 → `NEED_INFO` |
| `order.status` / `order.category` / `logistics.parcel_status` | 自身 | 系统权威 → `MANUAL` |
| `finance.requested_amount_minor` | 自身 | 缺失 → 阻断执行 |

blocked 规则与已命中规则一起按 `stage/priority/specificity` 排序；如果 blocked 规则胜出：

- 缺的是**用户可补充**的事实 → `NEED_INFO / MISSING_FACT`，`missing_facts` 精确到原始字段（如 `claim.claim_at`）；
- 缺的是**系统权威事实**，或原始值存在但不可解析/自相矛盾（申请早于签收） → `MANUAL / AUTHORITATIVE_FACT_UNAVAILABLE`。

这条机制确保：**通用允许规则不会因为“某个更具体规则缺事实”而静默放行**。

### 兜底路径

- 品类未知（含缺失、无法识别的别名）→ `UNKNOWN` → `CATEGORY_UNKNOWN_REVIEW`（MANUAL）。
- 事实齐全但没有任何规则覆盖 → `MANUAL / NO_APPLICABLE_POLICY`。
- 残留的“全部规则缺失字段”清单只作为最后兜底，且输出的是原始输入字段，不再暴露派生字段。

对应用例：`POL-C01`–`POL-C15`。

---

## 5. 规则集指纹：对“规则内容”做哈希，可复现

### 之前

`sha256("rule_id@version|...")` 只对标识做哈希：改条件、改金额、改条款绑定，指纹都不变，无法回答“为什么同一订单裁决变了”。

### 现在

- `Rule.canonical()` 输出影响裁决的全部字段（rule_id、version、priority、stage、when、decision、reason_code、required_clause_ids、amount_cap_minor、cap_type、scope、effective_from/to、status）。
- `ruleset_hash = sha256(json.dumps(sorted(rules, key=rule_id), sort_keys, separators))`；
- `facts_snapshot_id = sha256(规范化 facts)`；
- 落库与审计新增：`ruleset_version`、`ruleset_hash`、`facts_snapshot_id`、`evaluated_at`、`time_basis`、`requires_manual_approval`、`execution_blocked`。

审计 payload（`POLICY_DECIDED`）与 `policy_decisions.trace` 都带这些字段，回答三个问题：

1. 这次裁决用的是哪一版规则内容？（`ruleset_hash`）
2. 用的是哪一份事实快照？（`facts_snapshot_id`）
3. 以哪个时点为准？（`evaluated_at` / `claim_at`）

评测侧校验：同一规则集下所有用例的 `ruleset_hash` 必须完全一致；`as_of` 早于生效日会载入空集（`POL-G09`），空集有自己的指纹，属于正确行为。

---

## 6. 120 条金标的构成与口径

| 分组 | 数量 | 重点 |
| --- | --- | --- |
| 正常允许 | 20 | 每条规则至少一条正例，覆盖金/珠宝/电动车/质量/取消/拦截/赔付/烘焙 |
| 明确拒绝 | 20 | 品类例外、时效超期、已签收拦截，含冲突用例 |
| 缺少事实 | 15 | NEED_INFO vs MANUAL 的分界、未知品类、金额缺失阻断执行、事实冲突 |
| 时间边界 | 20 | 7/8 天、15/16 天、23:59/00:00、UTC 换算、跨零点 |
| 金额与数量 | 15 | 1999/2000/2001/0、缺失、极端数量 |
| 特殊例外 | 20 | 例外压过允许、未知品类压过允许、窗口压过兜底人工 |
| 品类与版本 | 10 | 中文/英文别名归一、UNKNOWN、生效边界 |

指标（`reports/policy-eval-v2-<date>.md`）：

- Decision Accuracy / Reason Code Accuracy / 条款绑定一致率 / 胜出规则一致率
- **关键条件可判定率**：胜出规则的每个条件都拿到了实际事实值，不允许缺值决策
- **Policy Version Accuracy**
- **危险错误放行次数**：期望 DENY/MANUAL/NEED_INFO 但实际 ALLOW，必须为 0
- 规则集指纹一致性、重放确定性

当前结果：**120/120，各项 100%，危险放行 0**（`reports/policy-eval-v2-2026-10-28.md`）。

### 金标不是引擎自证

- 期望值写在 `tests/eval/gen_policy_cases_v2.py` 的用例定义里，逐条手工指定；
- 生成器只负责序列化为 jsonl，不读取引擎输出；
- 条款 ID 取自冻结的规则绑定，复核表同时展开条款原文，供人工判断“这条规则是否真的支撑这个裁决”；
- `review` 字段保持 `pending`，需人工在 `tests/eval/policy-review-v2.csv` 中逐条确认后改 `confirmed`。

### 已知未覆盖（诚实标注）

1. **数量阈值**：205 条真实条款中“数量”只出现 1 次且与售后数量无关，因此没有按数量改判的规则；`POL-E11`–`POL-E15` 只验证“数量缺失/极值不改变裁决”。
2. **政策金额上限**：见第 3 节说明，仅有单测覆盖。
3. **历史版本**：目前只有 `2026.10-jd` 一个发布版本，`POL-G09/G10` 验证的是生效期边界与空规则集，不是真实的多版本并存。
4. **预售、运费险**：205 条条款中无对应政策，未建规则，也未造用例。

---

## 7. 变更清单

| 文件 | 变更 |
| --- | --- |
| `app/policy/facts.py` | 新增 `day_index` / `claim_time_facts` / `parse_ts` / `ts_before` / `ts_after` / `enrich_facts` |
| `app/policy/dsl.py` | 修复 `not_exists`；新增 `stage`、`cap_type`、`canonical()` |
| `app/policy/engine.py` | 阶段化裁决、blocked 机制、金额语义、内容指纹、重放确定性 |
| `app/policy/repository.py` / `db_repository.py` | 有效期按带时区 datetime 比较；读取 `stage`/`cap_type` |
| `app/policy/library.py` | 解析 `stage` / `amount_cap_type` |
| `policy/rules.json` | `claim.days_since_delivery` → `day_index_signed` / `day_index_next`；补 `stage`、`cap_type`；拦截拒绝条款改为 `CL-JD590903538377428992-S5` |
| `app/graph/nodes.py` | `claim_time_facts` 装配；审计记录指纹；退款执行前校验 `execution_blocked` |
| `app/graph/state.py` / `app/api/routes.py` | 新增 `claim_at`（可回放） |
| `migrations/sql/0021_policy_rule_stage_cap.sql` | `policy_rules.stage` / `cap_type` |
| `scripts/ingest_policy.py` | 写入 `stage` / `cap_type` |
| `tests/test_policy_facts.py` | 20 条单测（时间口径、阶段冲突、金额语义、指纹） |
| `tests/eval/gen_policy_cases_v2.py` | 120 条金标生成器 |
| `tests/eval/run_policy.py` | 120 条 runner + 指标 + 报告 |
| `tests/eval/make_policy_review.py` | Excel 复核表 + 条款原文 |