# 真实语料（17 份京东 PDF）→ 规则映射复核表（Path A 草案）

> 目的：以 17 份真实平台 PDF 为**唯一事实源**，重写 `policy/rules.json` 与证据闭包，使"规则→决策→依据条款"完全自洽、可 CI 断链校验。
> 状态：**已落地**（Path A 已执行：规则重写 + 代码改造 + 入库 + 验证）。执行结论见 §8。
> 生成依据：`policy_chunks`（205 chunk / 17 doc / v1）逐个条款通读。

## 0. 结论与待确认
1. 这 17 份是京东**商家运营/服务规则**，覆盖：7 天无理由、售后服务流程、运费争议、取消订单、在途拦截、闪电退款、上门换新、180 天只换不修、电器延保、家电维修、发货履约、黄金克重补差、新鲜烘焙、电动车三包、安装纠纷、隐私发货、恶意占库存/倒卖。
2. **勘误（重要）**：`预售`、`价保`（价格保护）在 17 份语料中**确实完全不存在**；但 `生鲜/服饰/图书/定制` **并非不存在** —— 它们以“7 天无理由**豁免清单 / 类目枚举**”的形式出现（`CL-JD4268-T1/T1#2/S4#2/S5/S5#3/S5#4`），可支撑“主观理由 + 豁免品类 → DENY”；缺的是它们各自的**专属售后政策**（如生鲜破损赔付、服饰质量细则、图书破损）。详见 §1b。
3. 有 8 条"服务资格类"规则（闪电退款/上门换新/只换不修/延保等）需要**当前事实词汇没有的字段**（实付金额、签收天数、信誉值、服务标识）。本方案先落为 `MANUAL`，待第 3 节的事实扩展后再升为自动判定。

## 1. 真实语料清单（doc_id → 主题 → chunk 数）
| doc_id | 主题 | chunk |
|---|---|---|
| jd_2595 | 售后服务管理规则（流程/时效/举证/质量定义） | 24 |
| jd_4268 | 7 天无理由退货规则（完好标准/豁免） | 22 |
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
## 1b. 关键问项在真实语料中的落点（勘误证据）

| 问项 | 是否在真实 PDF | 出现形式（含 clause_id） | 能否支撑规则 |
|---|---|---|---|
| 生鲜 / 鲜活易腐 | **有（豁免清单）** | `CL-JD4268-T1`「鲜活易腐类商品不适用 7 天无理由」；`CL-JD590903538377428992-A9`、`-S3#2` 取消订单“生鲜特殊类目”；`CL-JD2595-S11` 质保金类目表 | 支撑“无理由豁免(DENY)”；**无**生鲜破损赔付政策 |
| 服饰鞋包 | **有（判定条件 / 类目示例）** | `CL-JD4268-S5#4`（吊牌/防伪拆标、穿戴污损不支持无理由）；`CL-JD4268-T1#2`、`-T2#2` 类目示例 | 支撑“完好性判定”；**无**服饰专属退货政策 |
| 图书音像 | **有（豁免条款）** | `CL-JD4268-S4#2`、`CL-JD4268-S5`（图书/音像一次性密封破损不支持无理由） | 支撑“密封商品豁免(DENY)”；**无**图书质量赔付 |
| 定制 / 定作 | **有（豁免条款）** | `CL-JD4268-T1`、`-S5#3`、`-T2#3`（定制类不适用 7 天无理由） | 支撑“定制豁免(DENY)” |
| 预售 | **无** | 0 命中 | — |
| 定金 | **几乎无** | 仅 `CL-JD590903538377428992-S11#4` 类目表“定制定金补差” | — |
| 价保（价格保护） | **无** | “价格保护/降价/退差价/买贵”全 0；“保价”仅 `CL-JD616214789504503808-S4`“逆向保价保险”(不相关)；“补差/差价”仅黄金克重补差 `CL-JD1013329543295012864` | — |

> 结论：**能删的只有“品类专属赔付”和“预售/价保”**；“无理由豁免”这一族反而能在真实语料上重建。

## 2. 建议的新规则集（规则 ↔ 真实条款）

事实缩写：`reason` = `claim.reason_code`；`cat` = `order.category`；`status` = `order.status`；`parcel` = `logistics.parcel_status`；`intent` = 意图。

| # | rule_id | 决策 | reason_code | 触发条件（现有事实） | 真实依据条款 | 需新增事实 |
|---|---|---|---|---|---|---|
| 1 | `R-JD-NR-ALLOW-001` | ALLOW | `NO_REASON_RETURN_OK` | `reason∈{NO_LONGER_NEEDED,SIZE_ISSUE,COLOR_MISMATCH,NOT_AS_DESCRIBED}` ∧ `status=DELIVERED` | `CL-JD4268-A3`、`CL-JD4268-A5`、`CL-JD4268-S4` | 服务标识（可选） |
| 2 | `R-JD-NR-DENY-FRESH-001` | DENY | `NO_REASON_FRESH_EXEMPT` | `reason∈主观` ∧ `cat=FRESH`（鲜活易腐） | `CL-JD4268-T1` | — |
| 2b | `R-JD-NR-DENY-CUSTOM-001` | DENY | `NO_REASON_CUSTOM_EXEMPT` | `reason∈主观` ∧ `cat=CUSTOM`（定作） | `CL-JD4268-T1`、`CL-JD4268-S5#3` | — |
| 2c | `R-JD-NR-DENY-DIGITAL-001` | DENY | `NO_REASON_DIGITAL_EXEMPT` | `reason∈主观` ∧ `cat∈{DIGITAL,VIRTUAL}`（下载/拆封数字化） | `CL-JD4268-T1#2`、`CL-JD4268-S5` | — |
| 2d | `R-JD-NR-DENY-SEALED-001` | DENY | `NO_REASON_SEALED_EXEMPT` | `reason∈主观` ∧ `cat=BOOK_MEDIA`（一次性密封破损） | `CL-JD4268-S4#2`、`CL-JD4268-S5` | — |
| 3 | `R-JD-NR-DENY-INTACT-001` | DENY | `ITEM_NOT_INTACT` | `reason∈主观` ∧ 服饰类吊牌/防伪拆除、穿戴污损 | `CL-JD4268-S5#4` | — |
| 4 | `R-JD-Q-RETURN-001` | ALLOW | `QUALITY_RETURN_WINDOW` | `reason∈{QUALITY_DEFECT,DAMAGED,WRONG_ITEM,MISSING_ITEM,NOT_AS_DESCRIBED}` ∧ `status=DELIVERED` | `CL-JD2595-S16`、`CL-JD4268-A5` | 签收天数 |
| 5 | `R-JD-Q-REPAIR-001` | ALLOW | `QUALITY_REPAIR` | 质量类 ∧ `intent=RETURN_REFUND` | `CL-JD2595-S15`、`CL-JD2595-S16` | — |
| 6 | `R-JD-COMP-LATE-001` | ALLOW | `LATE_DELIVERY_COMPENSATION` | `reason=LATE_DELIVERY` | `CL-JD1190896203588571136-S13`、`-S20` | cap 2000（20 元） |
| 7 | `R-JD-COMP-OOS-001` | ALLOW | `OUT_OF_STOCK_COMPENSATION` | `reason∈{OUT_OF_STOCK,FALSE_SHIPMENT}` | `CL-JD1190896203588571136-S17`、`-S20` | cap 2000 |
| 8 | `R-JD-COMP-DELIVERY-ABNORMAL-001` | MANUAL | `DELIVERY_ABNORMAL_REVIEW` | `reason=DELIVERY_ABNORMAL` | `CL-JD1190896203588571136-S14`、`-S21` | — |
| 9 | `R-JD-COMP-SIGN-ABNORMAL-001` | MANUAL | `SIGN_ABNORMAL_REVIEW` | `reason∈{MISSING_ITEM,WRONG_ITEM}` | `CL-JD1190896203588571136-S18`、`-S22` | — |
| 10 | `R-JD-CANCEL-UNSHIPPED-001` | ALLOW | `UNSHIPPED_CANCEL_REFUND` | `status=PAID_UNSHIPPED` ∧ `intent=REFUND_ONLY` | `CL-JD590903538377428992-S3#2`、`-S3` | — |
| 11 | `R-JD-CANCEL-SPECIAL-REVIEW-001` | MANUAL | `SPECIAL_CANCEL_REVIEW` | `status=PAID_UNSHIPPED` ∧ `cat∈{FRESH,CUSTOM,VIRTUAL}` | `CL-JD590903538377428992-A9`、`-S3#2` | — |
| 12 | `R-JD-INTERCEPT-INTRANSIT-001` | ALLOW | `INTERCEPT_OK` | `parcel=IN_TRANSIT` ∧ `intent=INTERCEPT_REFUND` | `CL-JD590903538377428992-A9#2` | — |
| 13 | `R-JD-INTERCEPT-DELIVERED-DENY-001` | DENY | `INTERCEPT_ALREADY_DELIVERED` | `parcel∈{DELIVERED,SIGNED}` ∧ `intent=INTERCEPT_REFUND` | `CL-JD590903538377428992-S9` | — |
| 14 | `R-JD-FLASH-REFUND-001` | MANUAL | `FLASH_REFUND_ELIGIBILITY` | 服务类资格（需标识/金额/信誉） | `CL-JD616214789504503808-CH3`、`-S4` | 实付金额/信誉/服务标识 |
| 15 | `R-JD-DOOR-EXCHANGE-001` | MANUAL | `DOOR_EXCHANGE_ELIGIBILITY` | 服务类资格 | `CL-JD845914392943005696-CH3`、`-S3` | 服务标识/信誉 |
| 16 | `R-JD-REPLACE-ONLY-001` | MANUAL | `REPLACE_ONLY_ELIGIBILITY` | 需"订单完成第 8–180 天"+类目 | `CL-JD1023830024685490176-A1`、`-S7` | 订单完成天数 |
| 17 | `R-JD-EXT-WARRANTY-001` | MANUAL | `EXTENDED_WARRANTY_ELIGIBILITY` | 需延保期/类目 | `CL-JD1023834451186159616-A1`、`-A2` | 延保期事实 |
| 18 | `R-JD-HOME-REPAIR-001` | MANUAL | `HOME_REPAIR_SERVICE_REVIEW` | 维修服务纠纷 | `CL-JD1189815359696408576-S3`、`-S12` | — |
| 19 | `R-JD-GOLD-WEIGHT-001` | ALLOW | `GOLD_WEIGHT_COMPENSATION` | `cat∈{GOLD,JEWELRY}` ∧ `reason=WEIGHT_DIFF` | `CL-JD1013329543295012864-CH1`、`-S4` | 差异克重/金额 |
| 20 | `R-JD-GOLD-QUALITY-001` | ALLOW | `GOLD_QUALITY_RETURN` | `cat∈{GOLD,JEWELRY}` ∧ 质量类 | `CL-JD1013329543295012864-CH3` | 签收天数 |
| 21 | `R-JD-FRESH-BAKERY-001` | MANUAL | `FRESH_BAKERY_EXCHANGE` | `cat∈{BAKERY}` ∧ `reason=DATE_DISSATISFIED` | `CL-JD1148155761021554688-A1`、`-S4` | 生产/签收日期 |
| 22 | `R-JD-EBIKE-3BAO-001` | ALLOW | `EBIKE_MOTOR_RETURN` | `cat=EBIKE` ∧ 质量类 | `CL-JD3836-CH2`、`CL-JD3836-CH2#2` | 签收天数 |
| 23 | `R-JD-INSTALL-DISPUTE-001` | MANUAL | `INSTALL_DISPUTE_REVIEW` | `reason∈{INSTALL_LATE,INSTALL_FEE,INSTALL_DEFECT}` | `CL-JD3724-A5`、`-A7`、`-A9` | — |
| 24 | `R-JD-FREIGHT-BUYER-001` | ALLOW | `FREIGHT_BUYER_PAYS` | `reason∈主观`（无理由运费消费者承担） | `CL-JD4268-A6`、`CL-JD3727-A3#2` | 非资金动作 |
| 25 | `R-JD-FREIGHT-SELLER-001` | ALLOW | `FREIGHT_SELLER_PAYS` | 质量类 或 商家过错 | `CL-JD3727-CH1`、`CL-JD3727-A3#2`、`CL-JD3727-A9` | 非资金动作 |
| 26 | `R-JD-COMPLIANCE-PRIVACY-001` | MANUAL | `PRIVACY_SHIPPING_REQUIRED` | 合规约束（对商家） | `CL-JD1244115246973587456-A1` | 非消费者裁决 |
| 27 | `R-JD-COMPLIANCE-STOCK-001` | MANUAL | `MALICIOUS_STOCK_HOLD` | 违规处罚（对商家） | `CL-JD1206154529616302080-S1`、`-S2` | 非消费者裁决 |
| 28 | `R-JD-COMPLIANCE-RESALE-001` | MANUAL | `MALICIOUS_RESALE` | 违规处罚（对商家） | `CL-JD902003809503547392-CH2` | 非消费者裁决 |

> 规则 1–25 是**消费者售后决策**（进决策/证据/执行链路）；26–28 是**商家合规约束**，建议放在"审计/合规"清单，不参与消费者退款裁决（本表列出以便完整覆盖 17 份文档）。
## 3. 事实词汇缺口（需扩展 ContextAggregator + OMS/TMS Mock）
当前引擎可用事实只有：`order.{status,category,version,currency,lines}`、`logistics.{parcel_status,waybill_no}`、`finance.{refundable_balance_minor,reserved_amount_minor}`、`claim.reason_code`、`intent`。
服务资格类规则需要下面的字段（缺失时规则只能停在 `MANUAL`）：

| 需要的字段 | 用途 | 来源 |
|---|---|---|
| `order.paid_amount_minor` | 闪电退款/上门换新 ≤3000 元、只换不修赔偿限额 | OMS |
| `order.days_since_signed` / `order.completed_days` | 7 天无理由窗口、质量 7/15 日、只换不修 8–180 天 | OMS/TMS |
| `order.service_flags[]`（如 `7DAY_NO_REASON`、`FLASH_REFUND`、`DOOR_EXCHANGE`、`REPLACE_ONLY`、`EXT_WARRANTY`） | 服务资格判定 | 商品/服务 |
| `user.credit_level` | 上门换新 ≥2 星、闪电退款信誉 | 用户域 |
| `order.category` 词表扩展 | 新增 `GOLD/JEWELRY/EBIKE/BAKERY/HOME_APPLIANCE` 等 | OMS |
| `order.status` 词表扩展 | `SIGNED/REJECTED/PARTIAL`、`PAID_SHIPPED` 等 | OMS |

> 建议：**v1 先不扩事实**，服务类规则落 `MANUAL` 但带真实条款依据；v2 再扩 `ContextAggregator` + Mock 字段，把 14–17、19–22 升为自动判定。

## 4. reason_code 词表（需与意图提示词 §2.4 对齐）
新增/保留的 `claim.reason_code` 取值：`NO_LONGER_NEEDED,SIZE_ISSUE,COLOR_MISMATCH,NOT_AS_DESCRIBED`（主观）、`QUALITY_DEFECT,DAMAGED,WRONG_ITEM,MISSING_ITEM`（质量）、`LATE_DELIVERY,OUT_OF_STOCK,FALSE_SHIPMENT,DELIVERY_ABNORMAL,NOT_RECEIVED`（履约）、`WEIGHT_DIFF`（黄金克重）、`DATE_DISSATISFIED`（新鲜烘焙）、`INSTALL_LATE,INSTALL_FEE,INSTALL_DEFECT`（安装）、`PRICE_DROP`（价保，语料未覆盖）。
> 与旧合成稿相比：**删除** `SPOILED/FRESH_DAMAGED`（生鲜未覆盖）；**新增** 履约/黄金/烘焙/安装类。需同步更新意图识别的槽位提示词。

## 5. 与旧 16 条规则的差异
- **改写（保留业务含义，改引真实条款）**：7 天无理由、质量问题退换修、运费承担、在途拦截。
- **部分改写保留**：`R-FRESH-NO-REASON-001` / `R-CUSTOM-NO-REASON-001` / `R-MEDIA-SEALED-001` / `R-DIGITAL-NO-REASON-001` 的“主观理由 + 豁免品类 → DENY”语义，改引真实豁免条款 `CL-JD4268-T1`、`-T1#2`、`-S4#2`、`-S5`、`-S5#3`、`-S5#4`（见 §2 的 2/2b/2c/2d/3）。
- **删除（真实语料确无对应政策）**：品类专属**赔付/质量细则** `R-REFUND-FRESH-001`（生鲜破损赔付）、`R-APPAREL-QUALITY-001`（服饰质量）、`R-MEDIA-QUALITY-001`（图书质量）；以及 `R-PRESALE-DEPOSIT-001`（预售定金）、`R-PRICE-PROTECT-001`（价保）。
- **新增（真实语料带来）**：取消订单未出库、发货履约赔付（延迟/缺货/虚假发货）、配送/签收异常、闪电退款、上门换新、180 天只换不修、电器延保、家电维修、黄金克重补差、新鲜烘焙、电动车三包、安装纠纷，以及 3 条商家合规约束。

## 6. 待确认后执行的代码改动清单
1. 重写 `policy/rules.json`（本表 1–25 为规则，26–28 归合规清单）+ `documents` 指向真实 doc_id。
2. `app/rag/pg.py: PgClauseStore.fetch_many` 支持按 `clause_id` **或** `logical_clause_id` 匹配（让规则可引用逻辑条款）。
3. 内存后端 `app/policy/library.py: load_clauses` 改读**真实语料**（新增 `policy/clauses_jd.json` 或直接读解析产物），保证 `RAG_BACKEND=memory` 与 DB 一致。
4. `scripts/verify_policy_rules.py` 决策矩阵改为本表场景；期望全绿。
5. 新增 `scripts/check_rule_clause_links.py`：校验每条规则引用的 `clause_id` 必须在 `policy_chunks` 存在，缺失即 `exit 1`（CI 断链检查）。
6. `policy_rule_clause_map` 由 `UNRESOLVED` 置为 `RESOLVED`（rule_id → clause_id）。
7. 同步文档：`docs/policy/README.md`（规则/条款清单）、LLD §4/§5 相关表。

## 7. 需要你确认的点
1. 新规则集**范围**是否认可（尤其"删除生鲜/服饰/图书/预售/价保、新增履约/服务类"）？
2. 服务资格类（14–17、19–22）**v1 落 MANUAL** 是否接受？还是要我这轮就扩 OMS/TMS 事实并做自动判定？
3. 26–28 商家合规约束是否**只入合规清单、不进消费者裁决**？

## 8. 执行结论（Path A 已落地）

### 8.1 落地物（本次改动）

| 类别 | 文件 | 说明 |
|---|---|---|
| 规则 | `policy/rules.json` | 由 `scripts/gen_rules_from_corpus.py` 生成：`rules` **20 条**（消费者裁决）+ `catalog` **11 条**（服务资格/运费/商家合规） |
| 真实条款 | `policy/clauses_jd.json` | 205 条 child chunk（含 `logical_clause_id` / `parent_id` / `heading_path` / `token_count`），version 对齐为 `2026.10-jd` |
| 回复模板 | `policy/response_templates.json` | 15 条，全部绑定新规则 `basis_rule_ids`（旧 `FRESH_*` 话术已清除） |
| 生成器 | `scripts/gen_rules_from_corpus.py` | 生成规则时**强校验**每条规则/清单条目的 `required_clause_ids` 必须存在于真实语料，缺失即 `exit 1` |
| 断链检查 | `scripts/check_rule_clause_links.py` | `--source corpus`（离线）/ `--source db`（校验 `policy_chunks`），退出码 0/1，可进 CI |
| 入库脚本 | `scripts/ingest_policy.py` | 新增 `--from-clauses-jd`（从 `clauses_jd.json` 写文档+条款，含 parent/logical id）；`--rules` 现在会重建 `policy_rule_clause_map`；修复 timestamptz 传参 |
| 语料导出 | `scripts/export_clauses_jd.py` | 增加 `parent_id` 字段；版本号改为取自 DB 行（不再硬编码 `v1`） |
| 版本对齐 | `migrations/sql/0013_policy_version_alignment.sql` | 把 `policy_chunks` / `policy_parents` / `policy_documents`（jd_*）版本统一为 `2026.10-jd` |
| 引擎/存储 | `app/policy/library.py`、`app/rag/pg.py`、`app/rag/memory.py`、`app/rag/retrieval.py` | 条款加载改读真实语料；`fetch_many` 支持按 `clause_id` **或** `logical_clause_id` 命中；`PolicyChunk` 增加 `logical_clause_id` |
| 后端一致性 | `app/bootstrap.py` | 内存后端演示数据与 `app/mcp/mocks/server.py`（HTTP mock）对齐（o_1001–o_1005 / wb_1–wb_3 / 余额） |
| 验证脚本 | `scripts/verify_policy_rules.py`、`scripts/smoke_e2e.py`、`scripts/verify_db_saga.py` | 决策矩阵改为新场景（24 例）；端到端脚本改为「质量退换 + 人工审批」路径 |
| 提示词 | `app/llm/prompts/intent_guardrail.txt` | `reason_code` 词表同步（删 `SPOILED/FRESH_DAMAGED`，新增履约/黄金/烘焙/安装类；品类词表 + GOLD/JEWELRY/EBIKE/BAKERY/HOME_APPLIANCE） |
| 测试 | `tests/test_policy_engine.py`、`tests/test_review_fixes.py`、`tests/test_worker_and_hitl.py` | 改为真实语料规则场景 |

### 8.2 与本文档 §2 的刻意偏差（及其原因）

1. **DENY 豁免规则增加 `order.status=DELIVERED`**：`R-JD-NR-DENY-FRESH/CUSTOM/DIGITAL/SEALED-001` 均加 `status=DELIVERED`。
   原因：7 天无理由豁免只针对「签收后退货」；未出库订单属于「取消订单」权利，不能被豁免规则 DENY 掉（否则生鲜未出库取消会被误拒）。
2. **品类专项质量规则优先级提高**：`GOLD-WEIGHT=118`、`GOLD-QUALITY=116`、`EBIKE=114`（原 §2 为 112/108/106）。
   原因：加权低于通用质量规则 110 会让品类专项规则**永不命中**（被通用规则抢占），属不可达规则。
3. **服务资格类（§2 的 14–18）与运费（24–25）、商家合规（26–28）移入 `catalog`，不进引擎**。
   原因：`PolicyEngine` 在「无命中」时会用**全部适用规则**所需事实判断是否缺事实；把涉及 `paid_amount_minor` 等缺失事实的规则塞进引擎，会把无关请求误判为 `NEED_INFO/MISSING_FACT`，污染确定性语义。
4. **未生成 `R-JD-NR-DENY-INTACT-001`（服饰完好性）为引擎规则**：需新增 `order.item_intact` 事实，v1 落在 catalog。
5. **退款无金额上限 → 风控默认 `REVIEW`（人工审批）**：真实条款未给退款金额上限，`app/risk_rules.py` 对 `cap=None` 的退款判 `REVIEW`，不做无依据的自动放行。

### 8.3 验证结果（实测）

| 项目 | 命令 | 结果 |
|---|---|---|
| 单元/集成测试 | `docker compose run --rm api python -m pytest -q` | **39 passed** |
| 规则生成 + 条款链接 | `python scripts/gen_rules_from_corpus.py` | rules=20 catalog=11 docs=17，条款链接全部命中 |
| 断链检查（离线） | `python scripts/check_rule_clause_links.py` | 31 个条目 / 58 处引用 / 205 条款 → OK |
| 断链检查（DB） | `python scripts/check_rule_clause_links.py --source db` | 58 处引用全部命中 → OK |
| 决策矩阵 | `python scripts/verify_policy_rules.py` | **24/24 通过**（POLICY_RULES_OK） |
| 端到端（内存） | `python scripts/smoke_e2e.py` | `REFUND_SAGA_OK`（ALLOW → REVIEW → HITL 通过 → 1 条命令） |
| 端到端（DB + Worker） | `python scripts/verify_db_saga.py` | 证据 `OK` → 命令 `CONFIRMED` → 资金 `SUCCEEDED` |
| 库内状态 | `policy_chunks` 205 @ `2026.10-jd`；`policy_rules` 20（旧 16 条已清）；`policy_rule_clause_map` 38 条全 `RESOLVED` | 一致 |

### 8.4 仍未完成（v2 建议）

- 扩展事实（`order.paid_amount_minor` / `days_since_signed` / `service_flags[]` / `user.credit_level` / 品类词表），把 §4.1 的服务资格条目升为引擎规则；
- `parent_id` 内嵌的是构建期版本 token（`@v1:`），属不透明 id，不参与解析；若将来重建父块，建议 `build_policy_index.py --version 2026.10-jd` 保持一致；
- 真实语料未覆盖的品类专属赔付（生鲜破损、服饰/图书质量、预售、价保）如需支持，须先补真实依据文档再建规则。
