# Coinglass / LSR 连通性记录

验证时间：`2026-09-07 13:16 +08:00`  
目标主机：`vip2.coinglass.site`  
请求方式：HTTPS `GET`，认证仅使用请求头 `X-API-Key`，禁止重定向，未使用环境代理  
密钥处理：未写入仓库、配置文件或本文档；本文档不包含密钥值

## 验证结果

| 接口 | HTTP | 业务层检查 | 结果 |
| --- | ---: | --- | --- |
| `/api/futures/coins-markets?per_page=10` | 200 | 顶层包含 `code`/`data`，业务码为字符串 `0`，数据列表 10 条 | 成功 |
| `/api/lsr/signal/BTC?mode=trader` | 200 | 返回结构化 BTC trader 信号对象，包含计算时间、缓存年龄、多窗口 delta、long/short、whale 和 OI/市值类字段 | 成功 |
| `/api/gateway/account` | 200 | 顶层业务码为数值 `0`；所需权限、配额和到期类字段可解析，账户级值不写入仓库 | 成功 |

套餐、剩余额度、到期时间、WebSocket 限额等均属于动态账户元数据，本文档故意不记录。生产页面仅向有权限的管理员实时查询或展示短 TTL 脱敏结果；日志、TG、MCP 和公开健康检查不返回这些值。

## 生产适配器约束

1. 基础 URL 必须从受控配置读取，但最终解析后的 scheme/host 必须严格等于 `https` 和 `vip2.coinglass.site`。
2. `X-API-Key` 只在完成 host 校验后、发送前注入。禁止 query string、前端、Telegram、MCP、异常文本和普通日志携带密钥。
3. 禁止把带认证头的请求跟随 3xx；重定向视为安全错误并熔断。
4. HTTP 200 仍检查业务码。兼容字符串/数值 `0`；对无包装的 LSR 对象，以必需 schema 校验通过作为业务成功。
5. 429 优先解析 `Retry-After`（秒或 HTTP 日期），采用有界等待和抖动；默认 `max_attempts=2`（首次请求 + 至多 1 次重试），超过任务时限进入延后队列，绝不无限循环。实现和其他文档统一使用 `max_attempts`，避免“重试次数”是否包含首次请求的歧义。
6. 连接、读取和总请求分别设置超时；仅 GET 可自动重试。记录状态码、业务码、延迟、响应大小和 request ID，不记录完整正文。
7. `/api/gateway/account` 用于启动能力发现和限额调度，缓存 60 秒；不能把套餐、字段或额度永久硬编码。
8. Coinglass 与 LSR 使用独立 token bucket。交互查询优先于后台补数，交易退出链路不依赖它们。
9. 时间字段统一为 `source_time`（供应商事件时间）、可选 `calculated_at`、`received_at`、`available_at`（通过 schema/质量校验后首次可供系统读取）、`ingested_at`、`cache_age_seconds`，并保存 schema 版本和 payload hash。模型只能读取 `available_at <= decision_at` 的 revision。
10. 数据超出策略 TTL 或关键字段缺失时标记 `STALE/MISSING`，禁止新开仓；不能用 0 代替缺失，也不能静默沿用旧值。

## 建议健康状态

| 状态 | 条件 | 系统行为 |
| --- | --- | --- |
| `HEALTHY` | HTTP/业务码/schema 正常且未超 TTL | 正常产生特征 |
| `DEGRADED` | 临近额度、单次超时或部分可选字段缺失 | 降低刷新频率，展示数据质量，策略按版本化缺失规则处理 |
| `STALE` | 数据超过策略 TTL | 禁止依赖该源的新开仓，允许查询和持仓退出 |
| `RATE_LIMITED` | 429 且等待队列未到期 | 遵守 `Retry-After`，交互请求排队，不忙重试 |
| `OPEN_CIRCUIT` | 连续失败达到阈值或出现重定向/schema 漂移 | 熔断并告警，人工确认后半开探测 |

## 已落地的安全客户端

`src/gatescope/adapters/coinglass.py` 已实现固定 origin、header-only key injection、禁止 redirect、200 业务码校验、响应大小和 JSON shape 检查、两个总 attempts、`Retry-After`/deadline、熔断与 60 秒账户能力内存缓存。对应 synthetic contract tests 覆盖重定向、非零业务码、无包装 LSR、429 和缓存。

它目前有意不接入自动策略：`contracts/external/coinglass/feature-source-matrix.yaml` 仍为 `BLOCKED_PENDING_LICENSE_AND_CAPTURED_FIXTURE`，全部 feature 标为 `automatic_feature_eligible=false`。

## 进入自动特征前仍需完成

- 为当前获准能力的实际响应建立 JSON fixture 和版本化 schema；fixture 必须删除套餐、额度、到期、账户和认证信息。
- 明确服务的数据留存与再分发许可，尤其是把原始字段发给 Telegram、MCP 或长期存储之前。
- 以真实 `Retry-After`、业务码非零、HTTP 200 非 JSON、schema 新增/删除字段和时钟漂移做契约测试。
- 本次验证使用的凭据在生产部署前必须轮换；新值只通过云端 Secret Manager 注入，不写入文档或工单。
