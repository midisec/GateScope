# Phase 1 实施记录：数据时态、指标内核与服务隔离

更新时间：2026-09-09（Asia/Shanghai）

本记录只描述已经落到代码并有测试证据的内容。LIVE 仍由配置层和 executor guard 双重拒绝。

## 1. 当前交付

2026-09-09 新增趋势/突破/回归策略研究与正式回测队列，独立页面 `/strategies/backtests`。执行与统计边界、已完成及仍未关闭的模块详见 [趋势策略与回测系统](11_RESEARCH_BACKTESTS.md)。

| Gate | 已实现 | 主要证据 |
| --- | --- | --- |
| DB-001 | PostgreSQL/Timescale Alembic 基础；identity/config/market/outbox/alert/auth/TG/MCP 表；工作负载 NOBYPASSRLS 角色；tenant 表 FORCE RLS；transaction-local tenant/subject context 与连接 reset；Web Telegram 配置 RLS/opaque locator；六周期自选订阅函数 | `0002_postgres_foundation.py` 至 `0013_watchlist_multitimeframe.py`、`deploy/postgres/roles.sql`、`test_postgres_rls.py` |
| DB-002 | SQLite `mode=ro&immutable=1` + `query_only` importer；确定性 UUID；旧 K 线强制 quality flags；源文件前后 SHA-256 校验；幂等 PostgreSQL 导入 | `legacy_sqlite.py`、`import_legacy_sqlite.py`、`test_legacy_sqlite.py` |
| DAT-001 | canonical `Bar`、OHLC/时间/Decimal invariant、final/revision/available_at、水位可见性、LIVE eligibility、append-only revision book、gap reconcile | `domain/market.py`、`test_market.py` |
| IND-001/002 | registry、参数 schema、DAG/cycle、version/warm-up、cache key、batch/stream 同状态机；SMA/EMA/MACD/RSI/BOLL/ATR/ADX/OBV | `indicators/*`、`test_indicators.py` |
| IND-003 | 旧 26 形态黄金输出不变；V2 固定 pivot order，仅扫描当前可见前缀；事实 append-only，变化通过 revision/supersedes 表达；未来时间输出 fail closed | `patterns_v2.py`、V1 golden、`test_patterns_v2.py` |
| API-001（读取切片） | `/api/v2/instruments`、`/api/v2/bars`、指标 catalog/preview；query-bound HMAC cursor；`/ws/v2` subscribe/unsubscribe、stream sequence 与 resync_required | `apps/api/v2.py`、OpenAPI/WS schema、`test_v2_api.py` |
| ALERT-001（核心） | 规则版本快照、因果时间窗、ANY/ALL、冷却、确定性 event key；唯一 alert fact 与 notification outbox 同事务；事实禁止 UPDATE/DELETE | `application/alerts.py`、`0003_alert_facts.py`、`test_alerts.py` |
| ARC-001（进程隔离） | Web API 配置明确拒绝嵌入 worker；market/strategy 独立入口、独立非 root/read-only 容器；边界检查阻断 Web 重新启动 scanner | `worker_runtime.py`、`apps/*_worker`、`research-worker.Dockerfile`、`verify_boundaries.py` |
| AUTH-001（后端切片） | VIEWER/RESEARCHER/OPERATOR/OWNER deny-by-default 矩阵；tenant-bound PostgreSQL session；密码锁定；TOTP enroll/confirm/verify 与防重放；一次性恢复码；AES-256-GCM context binding；哈希链 identity audit；未完成 MFA 的会话不能访问业务 API | `formal_auth.py`、`formal_app.py`、`domain/identity.py`、`security_secrets.py`、`0004_formal_identity.py`、identity/API tests |
| CFG/WEB（纵向切片） | layout/template/watchlist/alert ETag CRUD；React/TypeScript/Vite 工作台、TradingView Lightweight Charts K 线/成交量、九类可选指标叠加和独立 pane、认证/MFA、告警与安全会话页面；按业务域划分的二级 canonical URL、服务端直达/刷新和旧路径 308 兼容跳转；顶栏仅保留行情/告警/交易和消息高频入口，用户名菜单集中账号、TG、交易所、MCP、数据源等低频配置 | `postgres_resources.py`、`postgres_watchlists.py`、`postgres_alert_rules.py`、`web/src/*`、`formal_app.py`、formal OpenAPI |
| TG（通知切片） | 工作区共享 Bot 由 OWNER 以当前密码 + MFA 配置；所有已登录成员用 5 分钟单次 deep link + Web MFA 自助绑定，无需管理员逐个维护 allowlist；绑定/通道/路由按 tenant + user 双重隔离；可选预授权 ID 只过滤未绑定消息；API webhook 以最小权限写入 inbox；Token/webhook secret 以 tenant-bound AES-GCM + 独立 RSA-OAEP wrapping 信封落库，API 只持公钥、notification worker 才持私钥；outbox lease、重试、dead-letter 与 Telegram business-code/429 处理；Web 测试发送展示排队、发送和送达状态 | `0005_telegram_notifications.py`、`0012_web_telegram_configuration.py`、`0014_telegram_member_self_service.py`、`0015_telegram_webhook_inbox_grant.py`、`0016_notification_worker_runtime_grants.py`、`postgres_notifications.py`、`postgres_telegram_config.py`、`application/notifications.py`、`notification_worker`、TG tests |
| STRATEGY（正式切片） | Web 结构化形态策略配置、方向预设、ANY/ALL、置信度/共振窗口/冷却和 Telegram 通道；独立 PostgreSQL strategy worker 读取 final/revision-aware K 线，固定 pivot order 生成 append-only pattern facts，并以 event key 幂等产生 alert fact 与通知 outbox；独立最小权限角色、RLS、heartbeat 和 systemd 守护 | `0017_formal_strategy_worker.py`、`0018_market_strategy_subscriptions.py`、`postgres_strategy.py`、`strategy_runtime.py`、`apps/strategy_worker`、`test_formal_strategy_worker.py` |
| MCP（只读切片） | 独立 Streamable HTTP 服务；一次性 hash-only scoped token、过期/吊销、tenant locator；8 个只读 market/indicator/alert/signal/trade 工具、每调用 scope 与不可变摘要审计；Web OWNER/MFA 管理页 | `0006_mcp_read_plane.py`、`postgres_mcp.py`、`application/mcp_tools.py`、`apps/mcp_server`、`test_mcp_read_plane.py` |
| CG-001（安全客户端） | 固定 HTTPS origin、发送前 host 校验、header-only key、禁止 redirect、200 业务码、响应大小/schema、最多 2 attempts、Retry-After/deadline、熔断、60 秒账户能力缓存；许可矩阵 UNKNOWN 时不注册自动特征 | `adapters/coinglass.py`、`feature-source-matrix.yaml`、`test_coinglass_client.py` |
| CG-WEB（配置与检测） | OWNER Web 加密提交、tenant RLS 密文配置、不可逆指纹、API 公钥/market worker 私钥隔离；保存后异步检测市场、BTC LSR、账户能力，逐接口记录 HTTP/业务校验/耗时，任务领取与崩溃恢复均有界 | `0019_web_coinglass_configuration.py`、`postgres_coinglass.py`、`coinglass_runtime.py`、`test_coinglass_web_configuration.py` |
| QNT-001（数学核心） | 有界独立净收益 block 的 Hoeffding 获利概率/净期望单侧下界；Bonferroni family alpha；future/support/重复 block fail closed；fresh/OOD/license/cost/holdout 强制 abstain；最高仅 PAPER | `quant/evidence.py`、`test_god_evidence.py`、`08_GOD_EVIDENCE_IMPLEMENTATION.md` |
| OMS/PROTECTION（纯状态机） | 确定 client order ID；timeout→UNKNOWN→同 ID 对账；ACK/fill 去重与上界；非 ENTRY 强制 reduce-only；部分成交保护 deadline、未覆盖超时撤剩余 entry/降险/冻结 | `gatescope_executor/oms.py`、`test_execution_oms.py`、`09_OMS_PROTECTION_IMPLEMENTATION.md` |
| PAPER-LEDGER（纵向切片） | 冻结 GOD signal/bar revision/gates；服务端 sizing；proposal 版本/幂等确认/revalidation；PAPER entry、STOP_FIRST 三重障碍、费用/PnL、hash-chained timeline；专用最小权限 paper worker；Web 交易页、TG 查询和 4 个 MCP 只读工具 | `0008_paper_trade_ledger.py`、`postgres_paper.py`、`paper_monitor.py`、`apps/paper_worker`、`domain/paper_trading.py`、PAPER tests、`10_PAPER_TRADE_LEDGER_IMPLEMENTATION.md` |
| FORMAL-RUNTIME | PostgreSQL 14 + TimescaleDB 2.19 实机迁移；旧库在线一致性快照导入；正式行情按自选展开 `1m/5m/15m/1h/4h/1d`，并合并策略精确订阅；closed-bar revision/heartbeat；最近窗口 API 与 Web 5 秒刷新；API/market/strategy/paper/notification/MCP/validator/TESTNET executor 的 systemd unit 和 Secret 分拆 | `0011_formal_market_worker.py`、`0013_watchlist_multitimeframe.py`、`0018_market_strategy_subscriptions.py`、`postgres_market_ingest.py`、`prepare_service_envs.py`、`deploy/systemd/*`、formal integration/runtime verification |

## 2. 旧数据库只读核验

2026-09-07 使用新 importer 对现有数据库做了一次完整只读 snapshot：

```text
source_sha256     db89307d071db226df794d7c179c7a8df6e8fe19267a5bb070ba5289d859782a
snapshot_digest   fb565fa1896b44751f414787651fb58f8c3b868ed722314643f4c014193e4a7e
instruments       3323
candles           109135
minimum_open_time 2019-11-18T00:00:00+00:00
maximum_open_time 2026-09-06T06:00:00+00:00
source_unchanged  true
```

源 SHA-256 与 Phase 0 冻结 manifest 完全一致。旧 schema 没有交易所 finality 和首次可用时间，因此所有导入 bar 都带 `LEGACY_SOURCE`、`LEGACY_FINALITY_ASSUMED`、`AVAILABLE_AT_UNKNOWN`。它们可以用于回看和研究，但 `live_eligible=false`。

## 3. 指标与形态的时间语义

- stream 只接受 final bar，且 `available_at <= watermark`；同时间修订或乱序会要求从 immutable facts 重建；
- batch 调用逐根复用同一个 stream 状态机，所以不是两套容易漂移的公式；
- cache key 包含 bar payload hashes、参数、依赖与 indicator version；
- fixed-order pivot 只有在右侧确认 bar 已进入当前前缀后才可能成为事件；fact 的 `available_at` 不早于全部输入；
- 后续走势只能新增 superseding fact，不能修改旧 payload。交易证据未来必须引用具体 `fact_id/payload_hash`。

## 4. PostgreSQL 上线顺序

1. 由 cluster admin 运行 `deploy/postgres/roles.sql`；
2. 给每个 workload 独立 login/证书，只允许切换到一个最小工作负载角色；
3. 设置 `GATESCOPE_DATABASE_URL=postgresql+psycopg://...` 后执行 `alembic upgrade head`；
4. 运行 `TEST_DATABASE_URL=... pytest tests/integration`；
5. 在维护窗口运行只读 importer，并比较 report 与本节 frozen count/hash；
6. API 与 worker 使用不同凭据，不得给应用 `SUPERUSER`、`BYPASSRLS` 或表 owner；
7. 同一物理连接完成 A→无 context→rollback→B 的负向验证后，才允许进入下一门禁。

2026-09-08 当前单机已在真实 PostgreSQL/Timescale 上完成迁移、RLS、MFA、行情 revision/heartbeat 和全套集成测试；PostgreSQL 只监听回环地址。CI 仍使用独立 TimescaleDB 服务，不能用 SQLite 测试替代该门禁。

## 5. 尚未关闭的 Phase 1 gate

- 正式身份已接入独立 PostgreSQL API composition root，但 password change、MFA remove/最后方法保护、OWNER 用户/成员管理页面与 bootstrap 数据迁移仍需补齐；未配置 PostgreSQL 时的兼容应用仍使用 Phase 0 单 OWNER session；
- React 页面已有价格 pane + 成交量、九类指标动态叠加/独立 pane、layout/watchlist/alert/TG/MCP 控制面；WS 驱动增量与无障碍 E2E 仍未关闭；
- TG route 已执行过滤、静默延期、关键风险绕过、语义去重和 route 级限流，并可查询 PAPER signal/trade；digest 聚合、chat/bot 全局分布式限流、图片 artifact 与 callback-token 原子交易确认尚未进入 dispatcher；
- MCP 已可只读查询 frozen signal、解释 gates、读取 PAPER trade status/timeline；proposal_create 仍未开放，服务也不推进交易状态；
- WS durable fan-out/heartbeat/slow-client budget；当前实现的是认证、订阅、sequence 与 REST resync 协议切片；
- formal market 与 strategy worker 已替代旧 SQLite 行情/形态告警扫描；legacy opportunity 请求仍待改用 durable job/outbox；策略事件属于因果形态证据，不等同于盈利概率证明，真实自动开单仍受 evidence gate 和 LIVE guard 阻断；
- migration up/down、RLS、immutable trigger 和行情 revision 已取得真实 TimescaleDB 绿色证据，仍需持续备份恢复与故障注入演练。

因此当前状态是“Phase 1 核心已实现、出口未关闭”，不是可接真实交易所开单的版本。
