# MCP 只读研究面：实施与运维

更新时间：2026-09-07（Asia/Shanghai）

## 1. 已实现边界

MCP 是独立进程和独立容器，使用 Streamable HTTP `POST /mcp`。它只包含以下工具：

| 工具 | 服务端 scope | 上限 | 副作用 |
| --- | --- | --- | --- |
| `market_search_instruments` | `market:read` | 100 个标的 | 无 |
| `market_get_candles` | `market:read` | 1,000 根终结 K 线 | 无 |
| `indicator_get_snapshot` | `analysis:read` | 8 个指标、2,000 根 K 线 | 只写调用审计 |
| `alert_rule_list` | `alerts:read` | 100 条当前规则 | 只写调用审计 |
| `signal_list` | `analysis:read` | 100 个冻结信号 | 只写调用审计 |
| `signal_explain` | `analysis:read` | 单信号、最多 250 根冻结 revision | 只写调用审计 |
| `trade_get_status` | `analysis:read` | 单笔已持久化 PAPER 状态 | 只写调用审计 |
| `trade_get_timeline` | `analysis:read` | 单笔追加式事件链 | 只写调用审计 |

工具本身带 MCP `readOnlyHint=true`、`destructiveHint=false`。这些标注只是客户端提示；真正的授权在服务端每次调用时再次执行。服务说明要求 Codex 保留 `data_watermark`、`quality_flags` 和 `research_only` 语义，不得将结果表述为获利保证或订单指令。

MCP 不 import executor，不持有交易所凭据，没有 proposal/order/approve/cancel 工具。`LIVE_TRADING_ENABLED` 仍为硬关闭。官方 Codex 支持 Streamable HTTP、Bearer Token 与 `enabled_tools`，配置口径参见 [OpenAI Codex MCP 文档](https://developers.openai.com/codex/mcp/)。

## 2. 凭据生命周期

1. OWNER 登录 Web，完成 MFA；
2. 在“Codex MCP”页创建客户端；
3. 输入新的 MFA 动态码，选择最小 scopes 和 1 小时至 90 天 TTL；
4. 服务端生成 288-bit 随机 secret，只在成功响应中显示一次；
5. 数据库仅保存 SHA-256 hash 与不可用于认证的首尾 hint；
6. Bearer token 经 security-definer locator 定位 tenant 后，还会检查 token/client/user/membership 状态与到期时间；
7. 客户端或单 token 吊销需要新的 MFA，事务内删除 locator，立即拒绝后续请求。

原始 Bearer token 禁止进入日志、TG、前端持久化、数据库明文字段或 Codex 配置文件。前端只把本次签发值保存在当前 React 内存状态，用户确认保存后立即清除。

## 3. Codex 配置

把 Web 一次性显示的 token 放进进程环境，而不是 TOML：

```bash
export GATESCOPE_MCP_TOKEN='从密码管理器注入'
```

复制 `.codex/config.toml.example` 的 stanza 到项目 `.codex/config.toml`，并把 URL 改为实际 HTTPS 地址：

```toml
[mcp_servers.gatescope]
url = "https://gatescope.example.com/mcp"
bearer_token_env_var = "GATESCOPE_MCP_TOKEN"
enabled_tools = [
  "market_search_instruments",
  "market_get_candles",
  "indicator_get_snapshot",
  "alert_rule_list",
  "signal_list",
  "signal_explain",
  "trade_get_status",
  "trade_get_timeline",
]
```

对外地址必须是 HTTPS；只有 loopback 开发地址允许 HTTP。MCP server 只应使用 `gatescope_mcp` 的 `NOBYPASSRLS` 数据库凭据，不能复用 migration owner、API 或 executor 凭据。

## 4. 启动

先运行 PostgreSQL migration，再分别注入 API 与 MCP 数据库凭据：

```bash
alembic upgrade head
docker compose --profile formal up -d --build gatescope-api mcp-server
```

关键变量：

- `GATESCOPE_MCP_DATABASE_URL`：映射到容器内 `GATESCOPE_DATABASE_URL`，数据库角色必须最小化；
- `GATESCOPE_MCP_PUBLIC_URL`：完整外部 `https://.../mcp` 地址；
- `GATESCOPE_MCP_HOST` / `GATESCOPE_MCP_PORT`：容器监听，默认 `0.0.0.0:8780`。

Compose 只把 MCP 端口绑定到宿主 loopback。互联网部署应由受控反向代理终止 TLS，限制 request body，并保持 Authorization header 不被访问日志记录。

## 5. 审计与不可变性

每个工具调用保存：tenant、token/client/subject、工具名、参数 canonical JSON 的 SHA-256、结果类别、安全错误码、trace ID 和时间。审计表禁止 UPDATE/DELETE，RLS 默认拒绝跨租户读取。审计只保存参数摘要，不保存完整查询文本，避免把未来可能出现的敏感参数复制到日志域。

认证失败且无法识别 tenant 的随机 token 不写 tenant 审计表；应由入口代理用不包含 header/body 的指标统计 401 数量。

## 6. 尚未实现

- MCP `proposal_create/cancel`：signal evidence、PAPER proposal、确定性 Risk/Sizing 与幂等记录虽已存在，但尚未完成 MCP 写 scope、默认绑定歧义拒绝和专用负向测试，因此仍不开放；
- 交互 OAuth/CIMD/DCR：当前是明确标记的 `SCOPED_SERVICE_TOKEN_V1`，不冒充 OAuth；
- Redis/网关级共享限流、每 token 并发预算与输出游标；当前已有单工具硬上限；
- trade status/timeline 已可读；trade chart/PNG artifact 尚未加入 MCP，且 MCP 查询不会触发 PAPER reducer；
- 实际 PostgreSQL/Timescale migration 与 RLS 集成验证：本机没有 Docker/PostgreSQL，CI 或预发布环境必须执行。

这些缺口不允许通过新增写工具、放宽 scope 或复用执行器凭据来绕过。
