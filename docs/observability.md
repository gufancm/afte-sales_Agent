# 可观测性（Prometheus 指标）

系统通过 API 暴露 Prometheus 抓取端点 `GET /metrics`，指标在**持久化写入路径统一埋点**，因此图节点与
Worker 产生的业务动作都会被计入（内存与 SQL 后端一致）。实现见 `app/observability/metrics.py`。

## 指标清单

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `aftersale_http_requests_total` | counter | method, path, status | HTTP 请求数（path 已归一化，UUID→`{id}`） |
| `aftersale_http_request_duration_ms` | histogram | method | HTTP 耗时(ms) |
| `aftersale_llm_calls_total` | counter | node, status | LLM 调用数（按节点） |
| `aftersale_llm_latency_ms` | histogram | node | LLM 耗时(ms) |
| `aftersale_llm_tokens_total` | counter | direction | LLM token 数（input/output） |
| `aftersale_tool_calls_total` | counter | tool, status | 只读 MCP 工具调用数 |
| `aftersale_tool_latency_ms` | histogram | tool | 只读工具耗时(ms) |
| `aftersale_tickets_total` | counter | - | 新建工单数 |
| `aftersale_commands_total` | counter | action | 入队写命令数 |
| `aftersale_command_results_total` | counter | status | 写命令终态数（CONFIRMED/FAILED_CONFIRMED/…） |
| `aftersale_approvals_total` | counter | decision | 审批决定数 |
| `aftersale_reconciliation_total` | counter | status | 对账任务完成数（DONE/PENDING/MANUAL_RECONCILE） |

## 抓取示例

```bash
curl -s http://localhost:8000/metrics | grep '^aftersale_'
```

Prometheus `scrape_configs` 片段：

```yaml
scrape_configs:
  - job_name: aftersale-agent
    metrics_path: /metrics
    static_configs:
      - targets: ["api:8000"]
```

## 健康与就绪

- `GET /health`：存活探针。
- `GET /ready`：就绪 + 启动校验（LLM/资金写开关等）。
- `GET /metrics`：Prometheus 文本（`text/plain; version=0.0.4`）。

## 边界

- 指标为**进程内**注册表：单副本直接抓取即可；多副本需集中式（Pushgateway / OTel Collector）或改用
  从数据库聚合的持久化指标。
- 标签基数受控：HTTP path 去 UUID；工具/动作/状态均为有限枚举。

## 实测

Docker 环境下抓取（真实 ticket `bbf783f5-…`，退货退款触发 HITL）：

```
aftersale_http_requests_total{method="POST",path="/api/v1/tickets",status="200"} 1.0
aftersale_llm_calls_total{node="intent_guardrail",status="SUCCESS"} 1.0
aftersale_tool_calls_total{status="SUCCESS",tool="oms.get_order_detail"} 1.0
aftersale_tickets_total 1.0
aftersale_commands_total{action="REQUEST_RETURN_REFUND"} 1.0
aftersale_approvals_total{decision="APPROVED"} 1.0
```
