# 正式单机部署与配置 Runbook

更新时间：2026-09-12（Asia/Shanghai）

本文记录 PostgreSQL/Timescale 正式栈的单机运行方式。它不是 LIVE 实盘授权；`LIVE_TRADING_ENABLED=false` 仍是不可绕过的启动门禁。

## 当前入口与服务

- 公网 Web/API：`https://43.108.44.74`，业务仅通过 Nginx 的 HTTPS 443 提供；80 只保留 Let’s Encrypt HTTP-01 验证路径，其他请求直接断开且不记 access log；
- 应用后端：仅监听 `127.0.0.1:17758`，公网不能绕过 Nginx 直接访问；
- PostgreSQL/Timescale：仅监听 `127.0.0.1:5432`；
- MCP：仅监听 `127.0.0.1:17800/mcp`；
- systemd：`gatescope-api`、`gatescope-market`、`gatescope-strategy`、`gatescope-research`、`gatescope-paper`、`gatescope-mcp`、`gatescope-executor-validation`、`gatescope-executor-testnet` 和 `gatescope-notification` 均有独立 unit；`gatescope-healthcheck.timer` 每分钟验证后台任务、本机 readiness 和公网 HTTPS，并在连续三次端点失败后自动恢复 API 或 Nginx。实际启用状态以 `systemctl is-enabled`、`systemctl is-active` 为准。

公网访问不要附加 `:17758`，也不要依赖 HTTP 自动跳转。该端口故意只绑定回环地址，唯一业务入口是 `https://43.108.44.74`；这可以防止绕过 TLS、Secure Cookie 和反向代理安全头。

首次登录使用工作区 `default`。OWNER 密码只保存在以下本机文件中，必须保持 `0600`，不得发到聊天、日志或 Git：

- `data/runtime-secrets/owner-login.json`

登录页的工作区下拉列表来自 `GATESCOPE_LOGIN_WORKSPACES` 部署白名单，不会匿名枚举数据库中的其他租户。新增工作区后把其 slug 加入该逗号分隔变量，再重新分拆 Secret 并重启 API。

密码验证成功后，尚未配置 MFA 的账号只会进入受限的“用户设置”页：

1. 点击“添加验证器”，使用 Google Authenticator、Microsoft Authenticator、Authy 或 1Password 扫描网页二维码；
2. 无法扫码时复制网页上只显示一次的手动密钥；二维码由浏览器本地生成，不调用外部二维码服务；
3. 输入当前 6 位动态码，点击“验证并绑定”；
4. 离线保存网页只显示一次的恢复码，确认后进入业务系统。

以后登录时在同一页面输入动态码或一次性恢复码。已完成认证后，可在“用户设置”中查看、添加、取消待确认或移除非最后一个验证器，并管理活动会话。已有有效验证器时，密码级受限会话不能新增或移除验证器；这可以防止仅泄露密码时替换 MFA。数据库中的 TOTP Secret 使用 AES-256-GCM 加密，恢复码只保存带 pepper 的摘要，服务器不保存网页展示过的明文副本。

## Web 模块 URL

Web 主模块使用稳定的二级 canonical URL。顶部导航和用户菜单使用真实链接，服务器可直接响应这些路径；刷新、收藏或粘贴完整 URL 不依赖前端内存中的页面状态。

| 模块 | Canonical URL |
| --- | --- |
| 行情图表 | `/markets/chart` |
| 概率告警 | `/strategies/alerts` |
| PAPER 交易 | `/trading/paper` |
| 交易所账户 | `/trading/exchange-accounts` |
| Codex MCP | `/integrations/mcp` |
| 数据源 | `/integrations/data-sources` |
| 消息通知 | `/notifications/inbox` |
| 账号与验证器 | `/settings/account` |
| Telegram 与通知设置 | `/settings/notifications` |

旧的根级入口（例如 `/markets`、`/paper`、`/exchange`、`/mcp`、`/settings`）只作兼容入口，并以 HTTP 308 跳转到对应 canonical URL。新增页面应继续归入现有一级模块；只有职责独立且会包含多个页面时才新增一级路径。认证/API 路径仍分别使用 `/login`、`/auth/*` 和版本化的 `/api/v2/*`。

## 自选实时行情

Web 的“行情图表”页左侧只展示“我的自选”，不常驻渲染全部市场目录。点击当前币种、空状态或“搜索添加”会打开币种搜索弹层；弹层支持输入筛选、方向键选择、Enter 打开以及直接加入自选。首次进入页面优先打开第一项自选，不会用市场目录中的任意币种覆盖用户选择。

加入自选后，数据库订阅函数会为每个有效标的展开 `1m`、`5m`、`15m`、`1h`、`4h`、`1d` 六个周期；`gatescope-market` 每 15 秒读取去重后的 `自选 × 六周期` 订阅，只请求已结束 K 线。页面每 5 秒请求当前标的和周期的最近窗口，切换周期时会丢弃上一次请求的迟到响应，并在同一图表实例上更新 K 线和成交量序列。Worker 状态在 OWNER 的“数据源”页每 15 秒刷新。

行情页“指标”菜单提供 EMA、SMA、BOLL、RSI、MACD、ATR、ADX 和 OBV；计算只读取服务器已终结 K 线，选择可保存为用户自己的指标模板。策略页提供结构化形态组合，启用后 `gatescope-strategy` 按新数据水位扫描固定版本规则，将检测事实追加到 `pattern_event_facts`，再以幂等事件写入告警与通知队列。普通形态置信度不是获利概率，也不会直接产生订单；PAPER 提案仍必须消费单独的冻结概率证据并通过风险门禁。

新加入的标的需要等待至少一个 market worker 周期完成首批写入；若切换周期后持续无图，先检查 worker 和该标的六周期订阅：

```bash
systemctl --no-pager --full status gatescope-market
journalctl -u gatescope-market --since "10 minutes ago" --no-pager
```

不要为临时缺失改回“每个自选只订阅默认周期”。六周期集合同时由 Web 周期按钮和数据库订阅函数约束，扩展周期时必须两处同步修改并补迁移/测试。

健康检查：

```bash
curl -fsS http://127.0.0.1:17758/health/ready
curl -fsS https://43.108.44.74/health/ready
systemctl --no-pager --full status gatescope-api gatescope-market gatescope-strategy gatescope-paper gatescope-healthcheck.timer
journalctl -u gatescope-healthcheck --since "10 minutes ago" --no-pager
```

API、后台任务和 Nginx 均配置重启策略。健康检查需要连续三次失败才执行端点恢复，避免一次短暂网络抖动导致进程反复重启；后台任务若意外退出则在下一次检查立即恢复。`/run/gatescope-healthcheck` 只保存临时失败计数，重启主机后自动清空，不包含业务数据或 Secret。

旧的自动化验收脚本只有在运维方另行保管了自动化 MFA 材料时才能使用；普通网页注册不会、也不应把验证器密钥留在服务器。日常健康检查优先使用上面的匿名健康端点和登录后的 Web 状态页。

若仍使用受控的自动化验收材料，可重复执行登录、自选和最近 K 线验收；脚本不打印密码、TOTP Secret 或恢复码：

```bash
GATESCOPE_PUBLIC_ORIGIN=https://43.108.44.74 \
  uv run python scripts/verify_formal_runtime.py
```

## Secret 配置边界

集中引导文件是 `data/runtime-secrets/formal-runtime.env`。部署级 Secret 只能在服务器的受限编辑器或 Secret Manager 中修改。登录密码、Telegram Bot Token、交易所 API Key/Secret 可由用户在 Web 表单中正常输入；浏览器会在发起请求前加密这些敏感字段，不把它们作为明文 JSON 发送。完成部署级配置修改后执行：

```bash
chmod 600 data/runtime-secrets/formal-runtime.env
GATESCOPE_SERVICE_PUBLIC_ORIGIN=https://43.108.44.74 \
GATESCOPE_SERVICE_API_HOST=127.0.0.1 \
  uv run python scripts/prepare_service_envs.py
sudo systemctl restart gatescope-api gatescope-market gatescope-strategy gatescope-paper gatescope-mcp
```

脚本会生成每个工作负载独立的 `formal-*.env`：API 得到身份密钥、浏览器提交 RSA 公私钥、交易所凭据 wrapping 公钥、Telegram wrapping 公钥和只读供应商 Key；executor 才得到交易所 wrapping 私钥；notification worker 才得到 Telegram wrapping 私钥及可选的旧式环境变量 Bot Token；market/paper/MCP 使用各自 PostgreSQL 登录。浏览器提交私钥只用于 API 收到请求后的瞬时解封，不用于数据库静态密文解封。不要直接把集中引导文件配置为 systemd `EnvironmentFile`。

### 浏览器敏感提交信封

以下 Web 操作已经使用独立的浏览器应用层加密信封：

- 账号密码登录；
- 保存 Telegram Bot 配置；
- 新增交易所账户；
- 轮换交易所凭据。

浏览器从公开且禁止缓存的 `GET /api/v2/security/submission-key` 取得当前公钥和 key version，为每次提交生成新的 AES-256-GCM 数据密钥，再以 RSA-3072 OAEP-SHA256 包装数据密钥。外层算法标识为 `RSA-OAEP-256+A256GCM`；purpose 绑定租户及操作，交易所轮换还绑定账户 ID。请求 ID、签发时间及上下文进入认证附加数据，服务端检查时效、用途和有限容量的防重放记录。公网远程客户端直接发送旧式明文 JSON 会收到 `ENCRYPTED_SUBMISSION_REQUIRED`；回环兼容路径只用于本机维护和测试，不应成为公网客户端协议。

这组提交密钥与交易所凭据 wrapping key、Telegram wrapping key 完全不同：

- `GATESCOPE_SUBMISSION_WRAP_PUBLIC_KEY`
- `GATESCOPE_SUBMISSION_WRAP_PRIVATE_KEY`
- `GATESCOPE_SUBMISSION_KEY_VERSION`

若三项全部缺失，`prepare_service_envs.py` 会生成独立 RSA-3072 密钥对；若只存在其中一部分则失败，不会猜测或覆盖。提交私钥必须只进入 API 的受限环境文件，不能进入 Web 静态资源、executor、notification worker、日志或版本库。

应用层信封只是裸 HTTP 下的降级兼容措施，不等同于安全传输：主动中间人仍可替换页面 JavaScript 或提交公钥，也能窃取/篡改会话 Cookie。它只能避免敏感字段以明文请求体出现在被动抓包或常规代理正文日志中。因此裸 HTTP 入口不得被标记为安全生产环境，不适合产品推广、真实用户或真实资金；正式互联网发布仍必须启用 HTTPS、Secure Cookie 和 HSTS。

Telegram 使用与交易所凭据不同的 RSA wrapping key。若以下三项全部缺失，分拆脚本会生成独立的 RSA-3072 密钥对及 key version，并以 `0600` 原子写回集中引导文件；若只存在其中一部分，脚本会失败而不是重建或覆盖：

- `GATESCOPE_TELEGRAM_WRAP_PUBLIC_KEY`
- `GATESCOPE_TELEGRAM_WRAP_PRIVATE_KEY`
- `GATESCOPE_TELEGRAM_KEY_VERSION`

不得手工复制 Telegram 私钥到 `formal-api.env`，也不得把公私钥或 Bot Token 写入文档、工单、聊天或版本库。轮换尚未提供自动重加密流程；轮换前必须另行制定旧 key version 解密、重包和回滚方案。

## 策略研究与回测

策略回测入口为 `/strategies/backtests`。从 `0038_shadow_ledger` 版本起，独立 `gatescope-research` 处理 PostgreSQL 研究队列，strategy 不再消费研究任务。旧指标回测保留 5000 根上限；`?view=structure` 同源形态研究支持最多 550000 根、后台分片与断点续算。`/trading/shadow` 展示启用后真实信号和送达事件的模拟结果，由 paper worker 自动推进。

部署必须同时安装并启用 `deploy/systemd/gatescope-research.service`、重启 API/strategy/paper。新服务复用已有 `formal-strategy.env` 数据库角色但使用独立研究心跳，限 1 核、MemoryHigh=512M、MemoryMax=768M。原始信号/事件/结果保留，绩效门槛不恢复，实盘不开启。数学口径及验收见 [同源研究与影子账本](../v2/35_SAME_ENGINE_RESEARCH_AND_SHADOW_LEDGER.md)，旧指标回测见 `docs/v2/11_RESEARCH_BACKTESTS.md`。

## Coinglass / LSR

以 OWNER 登录“数据源与运行状态”页，在 Coinglass / LSR 配置区输入 API Key、当前密码和一个新的 MFA 动态码，点击“保存并检测”。浏览器先用应用层信封加密整个敏感表单；API 只用独立 RSA 公钥把 Key 包装为 tenant/config/version 绑定的 AES-GCM 信封，行情 worker 独占对应私钥并异步检测。页面会分别展示下列三个固定接口的 HTTP、业务校验、耗时和安全错误码：

- `/api/futures/coins-markets?per_page=10`
- `/api/lsr/signal/BTC?mode=trader`
- `/api/gateway/account`

Key 只通过 `X-API-Key` 发给固定 HTTPS 域名；不进入 URL、Telegram、MCP、普通日志或响应，Web 也不提供明文读回。HTTP 200 仍检查业务码或 LSR 的已知无包装 schema；429 每个接口最多进行一次受限重试并遵循 `Retry-After`，检测任务崩溃恢复也有两次上限。在许可和 point-in-time 历史样本完成前，它只可用于人工研究，不能提升自动开单资格。

Coinglass 使用第三套独立 RSA-3072 wrapping key。`prepare_service_envs.py` 首次运行时生成 `GATESCOPE_COINGLASS_WRAP_PUBLIC_KEY`、`GATESCOPE_COINGLASS_WRAP_PRIVATE_KEY` 和 `GATESCOPE_COINGLASS_KEY_VERSION`；公钥只进入 API 环境，私钥只进入行情 worker 环境。不要再把供应商 API Key 写入 `formal-api.env`。

## Telegram 与消息通知

推荐从用户菜单进入 `/settings/notifications` 配置，不再要求把 Bot Token 写入环境变量。仅 OWNER 可保存 Bot；操作要求近期认证和一个未重放的 MFA 动态码。浏览器把 Token 放入租户绑定的应用层加密信封后再提交；公网远程明文 JSON 会被拒绝。API 会先调用 Telegram `getMe` 验证 Token，只向页面返回 Bot username、状态和不可逆指纹，不提供 Token 读回接口。

“允许的 Telegram 用户 ID”旁提供跨端 `t.me` 助手链接：手机可唤起 Telegram App，电脑可选择 Desktop 或 Web。用户从助手回复复制数字 ID 后可点击“粘贴并识别”；解析完全在浏览器本地完成，剪贴板正文不会提交到服务端。该助手属于第三方，只能用于查询数字 ID，不得向其发送 Bot Token、密码、API Key、Secret 或 MFA 验证码。已有 GateScope 私聊绑定会直接展示 user ID，并可一键加入允许名单输入框。

保存时，API 为 Bot Token 和自动生成的 webhook secret 建立带 tenant/config/secret-version 上下文的 AES-256-GCM 信封，并用独立 RSA-OAEP-SHA256 公钥包装数据密钥。密文、nonce、wrapped data key 和上下文写入 tenant RLS 表；opaque webhook 定位只保存散列索引。API 常驻进程没有对应私钥，通知 worker 按租户取信封并在投递时解密。

首次部署或本轮迁移后按以下顺序操作：

```bash
# 从受限文件读取 migration admin URL，不把凭据写入命令历史
set -a
source data/runtime-secrets/formal-runtime.env
set +a
formal_migration_url="$GATESCOPE_MIGRATION_DATABASE_URL"
GATESCOPE_DATABASE_URL="$formal_migration_url" uv run alembic upgrade head
unset formal_migration_url GATESCOPE_DATABASE_URL GATESCOPE_MIGRATION_DATABASE_URL

GATESCOPE_SERVICE_PUBLIC_ORIGIN=https://43.108.44.74 \
GATESCOPE_SERVICE_API_HOST=127.0.0.1 \
  uv run python scripts/prepare_service_envs.py
sudo systemctl restart gatescope-api
sudo systemctl enable --now gatescope-notification
systemctl --no-pager --full status gatescope-api gatescope-notification
```

`GATESCOPE_PUBLIC_ORIGIN` 必须是浏览器和 Telegram 实际访问的准确 HTTPS origin，反向代理也必须把 Webhook 路径转发到 `127.0.0.1:17758`。在 HTTPS origin 下保存配置时，API 会调用 `setWebhook`；成功状态为 `ACTIVE`。首次从旧 HTTP 部署切换完成后，需要在 `https://43.108.44.74/settings/notifications` 再次提交 Bot Token 和 MFA，让服务生成并注册新的 HTTPS Webhook。不要通过 HTTP 本机隧道登录：Secure Cookie 不会在明文 HTTP 上传输。

之后每个已登录成员都可在同一设置页发起一次性深链、完成 Web MFA 确认、创建仅属于自己的通道和路由，并发送测试消息，不需要 OWNER 逐个添加 Telegram ID。消息铃入口 `/notifications/inbox` 每 15 秒读取最近投递记录；需要关注的 `PENDING`、`RETRY`、`FAILED`、`DEAD_LETTER` 状态计入角标。入站 Webhook 校验 opaque path、Telegram secret header、deep-link 所属 tenant/user/session、私聊 user/chat 一致性和 update ID 去重；可选预授权名单只过滤未绑定消息。

通知 worker 可在没有旧式环境变量 Token 时启动；它使用 `formal-notification.env` 中的 Telegram wrapping 私钥按租户解析 Web 配置。租户没有 `ACTIVE` 配置时投递以安全错误码 `TG_NOT_CONFIGURED` 失败，不会尝试猜测其他租户的配置。Telegram 429 按服务端 Retry-After 受限重试，单条投递最多 8 次。

旧式 `GATESCOPE_TELEGRAM_BOT_*` 集中环境配置仅作兼容回退。若继续使用，username、token、webhook ID 和 webhook secret 必须整组存在，否则分拆脚本会将整组视为未启用；新部署应使用 Web 加密配置，避免把 Bot Token 长期分发给 API 配置域。

## 交易所配置

“交易所”页只接受 Gate TESTNET Key。新增或轮换时，浏览器将 API Key/Secret 放入应用层加密信封；公网远程明文 JSON 会被拒绝。API 解封后立即用另一套、面向静态存储的 wrapping 公钥重新封装，密文落库后只能由隔离 validator/executor 解密。浏览器提交私钥不会赋予 API 读取数据库既有交易所凭据的能力。

当前公网业务入口只接受 HTTPS，Cookie 带 `Secure`；80 除 ACME challenge 外直接断开，且不能直连 17758。应用层信封仍作为纵深防御保留；不得开放另一个绕过 Nginx 的 HTTP 监听端口。

即使 TESTNET 凭据验证通过，订单仍需 MFA、多项明确确认、未过期 `LIVE_ELIGIBLE / TESTNET` 策略认证和冻结 PAPER 条款。LIVE host、LIVE 账户和 `LIVE_TRADING_ENABLED=true` 没有允许路径。

## MCP

MCP 默认只在本机回环地址运行。OWNER 在 Web“Codex MCP”页创建客户端并用新的 MFA 码签发最小 scope token；明文只显示一次。Codex 与本服务在同一主机时使用：

```toml
[mcp_servers.gatescope]
url = "http://127.0.0.1:17800/mcp"
bearer_token_env_var = "GATESCOPE_MCP_TOKEN"
```

远程接入必须先提供 HTTPS 或受控 SSH 隧道，不能把 17800 直接暴露公网。MCP 只有读取和分析工具，不导入 executor，也不能创建、确认或取消订单。

## 发布与回滚检查

```bash
npm --prefix web run build
npm --prefix web test
uv run pytest -q tests/security/test_submission_crypto.py \
  tests/security/test_submission_api_boundary.py \
  tests/unit/test_service_env_preparation.py
set -a
source data/runtime-secrets/formal-runtime.env
set +a
formal_test_url="$TEST_DATABASE_URL"
unset GATESCOPE_DATABASE_URL GATESCOPE_MIGRATION_DATABASE_URL
TEST_DATABASE_URL="$formal_test_url" uv run pytest -q
```

每次首次启用或轮换浏览器提交密钥后，重新执行 Secret 分拆并重启 API：

```bash
GATESCOPE_SERVICE_PUBLIC_ORIGIN=https://43.108.44.74 \
GATESCOPE_SERVICE_API_HOST=127.0.0.1 \
  uv run python scripts/prepare_service_envs.py
sudo systemctl restart gatescope-api
```

验证公开公钥端点可用且不输出完整公钥内容：

```bash
curl -fsS http://127.0.0.1:17758/api/v2/security/submission-key \
  | uv run python -c 'import json,sys; d=json.load(sys.stdin); print(d["algorithm"], d["key_version"], len(d["public_key_spki"]))'
```

预期算法为 `RSA-OAEP-256+A256GCM`，key version 非空，公钥长度合理。随后在浏览器开发者工具的 Network 面板分别测试登录、Telegram 保存、交易所新增和轮换：请求正文应只含 `encrypted_submission` 信封，不得出现密码、Bot Token、API Key 或 Secret 原文，页面应处于安全上下文并使用原生 WebCrypto。不要通过 shell 历史构造包含真实 Secret 的 curl 命令。

### 公网 HTTPS

截至 2026 年，Let's Encrypt 已正式提供 IP 地址证书。当前主机已使用 Certbot 5.8 为 `43.108.44.74` 签发约 6 天的 ECDSA 短期证书，Nginx 从 `/etc/letsencrypt/live/43.108.44.74/` 加载证书。`snap.certbot.renew.timer` 每天检查两次，部署 hook `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx` 会在续期成功后先检查配置再重载 Nginx；续期 dry-run 已通过。

运维检查：

```bash
sudo certbot certificates
systemctl status snap.certbot.renew.timer nginx
sudo certbot renew --cert-name 43.108.44.74 --dry-run \
  --run-deploy-hooks --no-random-sleep-on-renew
curl -fsS https://43.108.44.74/health/ready
```

证书仅约 6 天有效，不能关闭续期 timer、80 端口的唯一例外 ACME 路径或部署 hook。80 上的其他 URI 使用 Nginx 非标准状态 `444` 直接关闭连接，不提供跳转、登录页或业务响应。Nginx 配置源文件为 `deploy/nginx/gatescope.conf`，续期 hook 源文件为 `deploy/certbot/reload-nginx`。

- [Let's Encrypt：6-day and IP Address Certificates are Generally Available](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability/)
- [Let's Encrypt：Short-lived certificates and IP address support in Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)

查看安全脱敏日志：

```bash
journalctl -u gatescope-api -u gatescope-market -u gatescope-paper -u gatescope-notification --since today
```

若正式 API 启动失败，不得回退为携带正式 Secret 的 SQLite 进程。先保持服务停止，检查迁移、工作负载角色和 `formal-api.env` 完整性，再恢复上一已验证构建。
