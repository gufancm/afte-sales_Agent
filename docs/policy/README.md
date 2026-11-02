# 电商售后政策库（真实平台规则）说明

> 本目录的**机器可读事实源**是：
> - `policy/clauses_jd.json` —— 17 份京东开放平台官方 PDF 解析出的 205 条条款（child chunk，含 `logical_clause_id` / `parent_id`）；
> - `policy/rules.json` —— 20 条消费者裁决规则（`rules`）+ 11 条非裁决清单（`catalog`）。
>
> 本说明是它们的索引与依据说明。**边界声明**：本仓库按「平台公示规则 + 可配置基线」处理，
> 内容非法律意见；正式上线前须由法务与业务复核，具体赔付比例/时限以平台公示规则为准。

## 1. 事实源与加载

| 事实源 | 说明 |
|---|---|
| `policy/clauses_jd.json` | 真实语料条款（205 条 / 17 篇），由 `scripts/export_clauses_jd.py` 从 `policy_chunks` 导出 |
| `policy/rules.json` | 规则集（`rules` 20 条）+ 非裁决清单（`catalog` 11 条）+ 文档清单（17 篇） |
| `policy/response_templates.json` | 面向用户的回复模板，**按命中规则绑定**（`basis_rule_ids`），避免代码里写死政策话术 |
| `app/policy/library.py` | 内存后端（`RAG_BACKEND=memory` / 无 DB）加载同一套规则与条款 |
| `scripts/gen_rules_from_corpus.py` | 生成 `rules.json`，**强校验**每条规则的 `required_clause_ids` 在真实语料中存在，缺失即 `exit 1` |
| `scripts/check_rule_clause_links.py` | 规则→条款 断链检查（`--source corpus` 离线 / `--source db` 校验已入库条款） |

> `docs/policy/*.md`（00-法定基线、20-服饰…70-在途拦截、生鲜售后政策）是**早期合成政策稿**，
> 已**不再是事实源**，仅作历史参考；它们对应的旧规则（`R-REFUND-FRESH-001`、`CL-FRESH-A` 等）已全部下线。

## 2. 真实语料清单（doc_id → 主题 → 条款数）

| doc_id | 主题 | 条款 |
|---|---|---|
| jd_2595 | 售后服务管理规则（流程/时效/举证/质量定义） | 24 |
| jd_4268 | 7 天无理由退货规则（完好标准/豁免清单） | 22 |
| jd_3727 | 运费问题纠纷处理标准（谁过错谁承担） | 10 |
| jd_1190896203588571136 | 货源平台供应商发货管理规则（延迟/缺货/虚假发货/违约金） | 21 |
| jd_590903538377428992 | 取消订单服务管理规则（未出库/已出库/拦截） | 17 |
| jd_616214789504503808 | 闪电退款服务规则 | 5 |
| jd_845914392943005696 | 上门换新服务规则 | 5 |
| jd_1023830024685490176 | 180 天只换不修保险服务 | 14 |
| jd_1023834451186159616 | 电器延保险服务 | 13 |
| jd_1189815359696408576 | 家电家居生活品类维修服务规范 | 22 |
| jd_1013329543295012864 | 计价黄金克重补差与质量问题售后 | 6 |
| jd_1148155761021554688 | 新鲜烘焙服务规则 | 9 |
| jd_3836 | 电动车类目三包服务 | 10 |
| jd_3724 | 安装问题纠纷处理标准 | 12 |
| jd_1244115246973587456 | 隐私发货规则 | 7 |
| jd_1206154529616302080 | 恶意占库存细则 | 5 |
| jd_902003809503547392 | 恶意倒卖纠纷处理标准 | 3 |

合计 **17 篇 / 205 条**。全文条款见 `policy/clauses_jd.json`；逐条映射见 `docs/real-corpus-rule-mapping.md`。

## 3. 客观事实：真实语料覆盖了什么、没覆盖什么

| 问项 | 真实语料 | 形式 |
|---|---|---|
| 鲜活易腐（生鲜） | **有（豁免清单）** | `CL-JD4268-T1` 不适用 7 天无理由；**无**生鲜破损赔付政策 |
| 服饰鞋包 | **有（判定条件/类目示例）** | `CL-JD4268-S5#4` 吊牌/防伪拆除、穿戴污损不支持无理由；**无**服饰专属退货细则 |
| 图书音像 | **有（豁免条款）** | `CL-JD4268-S4#2`、`CL-JD4268-S5` 一次性密封破损不支持无理由；**无**图书质量赔付 |
| 定制/定作 | **有（豁免条款）** | `CL-JD4268-T1`、`CL-JD4268-S5#3` 不适用 7 天无理由 |
| 预售 / 定金 | **无** | 0 命中 |
| 价保（价格保护） | **无** | "价格保护/降价/退差价"全 0 命中 |

> 因此「主观理由 + 豁免品类 → DENY」可在真实语料上重建；**删除**的仅是品类专属赔付/质量细则与预售/价保——
> 这些诉求在引擎里落 `MANUAL/NO_APPLICABLE_POLICY`（转人工），不做无依据的自动裁决。

## 4. 规则清单（`rules`：进入确定性引擎，decision 由引擎求出，不调用 LLM）

| rule_id | 优先级 | 决策 | reason_code | 触发条件（可信事实） | 依据条款 |
|---|---|---|---|---|---|
| `R-JD-NR-DENY-FRESH-001` | 130 | DENY | `NO_REASON_FRESH_EXEMPT` | 主观理由 ∧ `category=FRESH` ∧ `status=DELIVERED` | `CL-JD4268-T1` |
| `R-JD-NR-DENY-CUSTOM-001` | 130 | DENY | `NO_REASON_CUSTOM_EXEMPT` | 主观理由 ∧ `category=CUSTOM` ∧ `status=DELIVERED` | `CL-JD4268-T1`、`-S5#3` |
| `R-JD-NR-DENY-DIGITAL-001` | 130 | DENY | `NO_REASON_DIGITAL_EXEMPT` | 主观理由 ∧ `category∈{DIGITAL,APPLIANCE,VIRTUAL}` ∧ `status=DELIVERED` | `CL-JD4268-T1#2`、`-S5` |
| `R-JD-NR-DENY-SEALED-001` | 130 | DENY | `NO_REASON_SEALED_EXEMPT` | 主观理由 ∧ `category=BOOK_MEDIA` ∧ `status=DELIVERED` | `CL-JD4268-S4#2`、`-S5` |
| `R-JD-INTERCEPT-DELIVERED-DENY-001` | 130 | DENY | `INTERCEPT_ALREADY_DELIVERED` | `parcel_status∈{DELIVERED,SIGNED}` ∧ `intent=INTERCEPT_REFUND` | `CL-JD590903538377428992-S9` |
| `R-JD-GOLD-WEIGHT-001` | 118 | ALLOW | `GOLD_WEIGHT_COMPENSATION` | `category∈{GOLD,JEWELRY}` ∧ `reason=WEIGHT_DIFF` | `CL-JD1013329543295012864-CH1`、`-S4` |
| `R-JD-GOLD-QUALITY-001` | 116 | ALLOW | `GOLD_QUALITY_RETURN` | `category∈{GOLD,JEWELRY}` ∧ 质量理由 | `CL-JD1013329543295012864-CH3` |
| `R-JD-EBIKE-3BAO-001` | 114 | ALLOW | `EBIKE_MOTOR_RETURN` | `category=EBIKE` ∧ 质量理由 | `CL-JD3836-CH2`、`-CH2#2` |
| `R-JD-Q-RETURN-001` | 110 | ALLOW | `QUALITY_RETURN_WINDOW` | 质量理由 ∧ `status=DELIVERED` | `CL-JD2595-S16`、`CL-JD4268-A5` |
| `R-JD-Q-REPAIR-001` | 105 | ALLOW | `QUALITY_REPAIR` | 质量理由 ∧ `intent=RETURN_REFUND` | `CL-JD2595-S15`、`-S16` |
| `R-JD-NR-ALLOW-001` | 100 | ALLOW | `NO_REASON_RETURN_OK` | 主观理由 ∧ `status=DELIVERED` | `CL-JD4268-A3`、`-A5`、`-S4` |
| `R-JD-CANCEL-UNSHIPPED-001` | 100 | ALLOW | `UNSHIPPED_CANCEL_REFUND` | `status=PAID_UNSHIPPED` ∧ `intent=REFUND_ONLY` | `CL-JD590903538377428992-S3#2`、`-S3` |
| `R-JD-INTERCEPT-INTRANSIT-001` | 100 | ALLOW | `INTERCEPT_OK` | `parcel_status=IN_TRANSIT` ∧ `intent=INTERCEPT_REFUND` | `CL-JD590903538377428992-A9#2` |
| `R-JD-COMP-LATE-001` | 96 | ALLOW | `LATE_DELIVERY_COMPENSATION` | `reason=LATE_DELIVERY` | `CL-JD1190896203588571136-S13`、`-S20`（cap 2000） |
| `R-JD-COMP-OOS-001` | 96 | ALLOW | `OUT_OF_STOCK_COMPENSATION` | `reason∈{OUT_OF_STOCK,FALSE_SHIPMENT}` | `CL-JD1190896203588571136-S17`、`-S20`（cap 2000） |
| `R-JD-CANCEL-SPECIAL-REVIEW-001` | 105 | MANUAL | `SPECIAL_CANCEL_REVIEW` | `status=PAID_UNSHIPPED` ∧ `category∈{FRESH,CUSTOM,VIRTUAL}` | `CL-JD590903538377428992-A9`、`-S3#2` |
| `R-JD-COMP-DELIVERY-ABNORMAL-001` | 95 | MANUAL | `DELIVERY_ABNORMAL_REVIEW` | `reason=DELIVERY_ABNORMAL` | `CL-JD1190896203588571136-S14`、`-S21` |
| `R-JD-FRESH-BAKERY-001` | 95 | MANUAL | `FRESH_BAKERY_EXCHANGE` | `category=BAKERY` ∧ `reason=DATE_DISSATISFIED` | `CL-JD1148155761021554688-A1`、`-S4` |
| `R-JD-INSTALL-DISPUTE-001` | 95 | MANUAL | `INSTALL_DISPUTE_REVIEW` | `reason∈{INSTALL_LATE,INSTALL_FEE,INSTALL_DEFECT}` | `CL-JD3724-A5`、`-A7`、`-A9` |
| `R-JD-COMP-SIGN-ABNORMAL-001` | 90 | MANUAL | `SIGN_ABNORMAL_REVIEW` | `reason∈{MISSING_ITEM,WRONG_ITEM}` | `CL-JD1190896203588571136-S18`、`-S22` |

规则语义：命中取**优先级最高**者；同优先级看「条件更具体」（`when` 条数更多）；仍并列 → `MANUAL/POLICY_CONFLICT`。
无任何命中：有缺失事实 → `NEED_INFO`（追问）；无缺失事实 → `MANUAL/NO_APPLICABLE_POLICY`（转人工）。

### 4.1 非裁决清单（`catalog`：不进引擎求值，只做覆盖度审计）

| item_id | 决策 | reason_code | 依据条款 | 为何不进引擎 |
|---|---|---|---|---|
| `R-JD-NR-DENY-INTACT-001` | DENY | `ITEM_NOT_INTACT` | `CL-JD4268-S5#4` | 需新增事实 `order.item_intact`（吊牌/防伪是否拆除） |
| `R-JD-FLASH-REFUND-001` | MANUAL | `FLASH_REFUND_ELIGIBILITY` | `CL-JD616214789504503808-CH3`、`-S4` | 服务资格，需实付金额/信誉/服务标识 |
| `R-JD-DOOR-EXCHANGE-001` | MANUAL | `DOOR_EXCHANGE_ELIGIBILITY` | `CL-JD845914392943005696-CH3`、`-S3` | 服务资格 |
| `R-JD-REPLACE-ONLY-001` | MANUAL | `REPLACE_ONLY_ELIGIBILITY` | `CL-JD1023830024685490176-A1`、`-S7` | 需订单完成天数（第 8–180 天） |
| `R-JD-EXT-WARRANTY-001` | MANUAL | `EXTENDED_WARRANTY_ELIGIBILITY` | `CL-JD1023834451186159616-A1`、`-A2` | 需延保期与服务标识 |
| `R-JD-HOME-REPAIR-001` | MANUAL | `HOME_REPAIR_SERVICE_REVIEW` | `CL-JD1189815359696408576-S3`、`-S12` | 维修按类目服务规范，需人工判责 |
| `R-JD-FREIGHT-BUYER-001` | ADVISORY | `FREIGHT_BUYER_PAYS` | `CL-JD4268-A6`、`CL-JD3727-A3#2` | 运费承担说明，非资金裁决 |
| `R-JD-FREIGHT-SELLER-001` | ADVISORY | `FREIGHT_SELLER_PAYS` | `CL-JD3727-CH1`、`-A3#2`、`-A9` | 运费承担说明，非资金裁决 |
| `R-JD-COMPLIANCE-PRIVACY-001` | MANUAL | `PRIVACY_SHIPPING_REQUIRED` | `CL-JD1244115246973587456-A1` | 对商家的合规约束 |
| `R-JD-COMPLIANCE-STOCK-001` | MANUAL | `MALICIOUS_STOCK_HOLD` | `CL-JD1206154529616302080-S1`、`-S2` | 对商家的违规处罚 |
| `R-JD-COMPLIANCE-RESALE-001` | MANUAL | `MALICIOUS_RESALE` | `CL-JD902003809503547392-CH2` | 对商家的违规处罚 |

## 5. 判定所用事实（来自 OMS/TMS/Finance/意图，不由 LLM 决定）

```
order.category / order.status / order.version / order.currency / order.lines
logistics.parcel_status / logistics.waybill_no
finance.refundable_balance_minor / finance.reserved_amount_minor
claim.reason_code     # 来自意图识别的槽位（规范取值见 app/llm/prompts/intent_guardrail.txt）
intent                # REFUND_ONLY / RETURN_REFUND / INTERCEPT_REFUND / RESHIPMENT / EXPEDITE / ...
```

> v1 尚未扩展的事实（服务资格类所需）：`order.paid_amount_minor`、`order.days_since_signed`、
> `order.service_flags[]`、`user.credit_level`、`GOLD/JEWELRY/EBIKE/BAKERY/HOME_APPLIANCE` 品类词表。
> 补齐后 §4.1 的服务资格条目可从 catalog 升为引擎规则。

## 6. 决策矩阵（`scripts/verify_policy_rules.py` 实测：DB 规则 + DB 条款 **24/24 通过**）

| 场景 | 决策 | reason_code |
|---|---|---|
| 生鲜"不想要了"（已签收） | DENY | NO_REASON_FRESH_EXEMPT |
| 定制 / 数码 / 图书密封（主观理由） | DENY | NO_REASON_CUSTOM/DIGITAL/SEALED_EXEMPT |
| 服饰无理由 / 尺码不合 | ALLOW | NO_REASON_RETURN_OK |
| 质量问题（已签收） | ALLOW | QUALITY_RETURN_WINDOW |
| 质量问题（未签收，要求退货退款） | ALLOW | QUALITY_REPAIR |
| 黄金克重补差 / 黄金质量 / 电动车三包 | ALLOW | GOLD_WEIGHT_COMPENSATION / GOLD_QUALITY_RETURN / EBIKE_MOTOR_RETURN |
| 未出库取消（普通） | ALLOW | UNSHIPPED_CANCEL_REFUND |
| 未出库取消（生鲜/定制/虚拟） | MANUAL | SPECIAL_CANCEL_REVIEW |
| 在途拦截 / 签收后拦截 | ALLOW / DENY | INTERCEPT_OK / INTERCEPT_ALREADY_DELIVERED |
| 延迟发货 / 缺货 / 虚假发货 | ALLOW | LATE_DELIVERY_COMPENSATION / OUT_OF_STOCK_COMPENSATION |
| 配送异常 / 安装纠纷 / 签收异常 / 新鲜烘焙 | MANUAL | DELIVERY_ABNORMAL/INSTALL_DISPUTE/SIGN_ABNORMAL/FRESH_BAKERY_*_REVIEW |
| 品类与理由均未覆盖 | MANUAL | NO_APPLICABLE_POLICY |

## 7. 入库与验证

```bash
# 1) 生成规则（强校验：每条规则的依据条款必须存在于真实语料）
python scripts/gen_rules_from_corpus.py
# 2) 断链检查（离线 / 入库后）
python scripts/check_rule_clause_links.py
python scripts/check_rule_clause_links.py --source db --database-url postgresql+asyncpg://aftersale_app:app_pw_change_me@localhost:5432/aftersale
# 3) 入库规则（清空该租户后写入 policy_rules；并重建 policy_rule_clause_map）
python scripts/ingest_policy.py --rules --replace --database-url <APP_DSN>
# 4) 真语料重建条款（可选；含 parent_id/logical_clause_id，--embed 生成向量）
python scripts/ingest_policy.py --from-clauses-jd --replace --embed --database-url <APP_DSN>
# 5) 决策矩阵验证
python scripts/verify_policy_rules.py      # -> POLICY_RULES_OK
# 6) 查看已入库
python scripts/ingest_policy.py --list --database-url <APP_DSN>
```

容器内主机名为 `postgres`，宿主机为 `localhost`。RAG 走库：`.env` 设 `RAG_BACKEND=pg`。

## 8. 新增/修改一条政策的正确姿势

1. 若来自新 PDF：先解析入库（Docling 主干 + pdfplumber 兜底）→ 更新 `policy/clauses_jd.json`（或用 `export_clauses_jd.py` 重新导出）；
2. 在 `scripts/gen_rules_from_corpus.py` 增加/修改规则，`required_clause_ids` 引用真实条款 id（**生成器会校验存在性**）；
3. `python scripts/gen_rules_from_corpus.py` → `python scripts/check_rule_clause_links.py`；
4. `python scripts/ingest_policy.py --rules --replace --database-url <APP_DSN>`；
5. 若面向用户有文案，在 `policy/response_templates.json` 增加模板并声明 `basis_rule_ids`；
6. `python scripts/verify_policy_rules.py` 与 `docker compose run --rm api python -m pytest -q` 应通过；
7. 内存后端（`RAG_BACKEND=memory`）经 `app/policy/library.py` 自动读到同一套内容，无需另改代码。