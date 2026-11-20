# ChatGPT 网页 MCP OAuth

## 接入方式

在 ChatGPT 的自定义 MCP 连接中填入 `https://43.108.44.74/mcp`，认证选 OAuth。采用动态客户端注册（DCR），客户端 ID 和 Secret 不需要手工填写。如果界面允许选择注册方式，选 DCR。然后跳转 GateScope，用自己的账号登录、完成 MFA，并明确点击「允许只读访问」。授权过期或撤销后需要重新连接。

连接后，每次工具调用指定币种、市场和周期，例如 `market_analysis_snapshot(symbol="BTC_USDT", market="futures:usdt", timeframe="15m", bar_limit=500)`。无需在管理页面固定币种。ChatGPT 获得本人的市场研究访问权，不获得密码、交易所/Coinglass Key、配置修改或订单权限。

当前沿用平台 MCP 的 OWNER 权限政策，不扩大其他角色权限。授权前要求完整 MFA 会话和十分钟内密码认证；过期时可通过已有的用户绑定加密提交重新确认密码，然后再次明确授权。原有手动 PAT 接入、签发/吊销的鲜活 MFA 规则保留。

## 协议与边界

依据 [OpenAI Docs：Authentication](https://developers.openai.com/plugins/build/auth) 的授权发现、DCR、PKCE S256、resource 和 issuer 回调要求实现。不宣称实现 OIDC、CIMD 或 Enterprise 邮箱域限制；不需要提供 OpenAI API Key，也不会为验证而调用付费模型。

| 地址 | 作用与身份边界 |
| --- | --- |
| `/.well-known/oauth-protected-resource/mcp`、无 `/mcp` 后缀的同名路径 | 公开资源元数据，resource 为 HTTPS `/mcp` |
| `/.well-known/oauth-authorization-server` | 公开授权服务器元数据、S256、注册/授权/换令牌/撤销地址 |
| `POST /oauth/register` | 有上限的 DCR；仅允许 ChatGPT 精确回调地址；不使用浏览器会话 |
| `GET /oauth/authorize` | 校验注册客户端、回调、scope、resource 和 PKCE，建立浏览器绑定的短期请求 |
| `/integrations/mcp/authorize?request=…` | 独立网页登录/MFA/授权页，不在地址中携带密码或访问令牌 |
| `GET/POST /api/v2/mcp/oauth/consent` | 本人完整会话、浏览器随机值与请求绑定；POST 另需 CSRF，批准另需近期身份认证 |
| `POST /oauth/token` | 授权码换令牌、refresh rotation；只验证 OAuth 客户端与凭据，不使用 Cookie |
| `POST /oauth/revoke` | RFC 7009 风格撤销；未知或其他客户端的令牌不泄露存在性 |
| `/mcp` | SDK 强制 Bearer；匿名/失效令牌 401，并提供资源元数据 challenge |

客户端认证支持 `none`、`client_secret_post`、`client_secret_basic`。`none` 也强制 S256，禁止密码 grant、client_credentials 和 scope 扩权。固定稳定回调为 `https://chatgpt.com/connector_platform_oauth_redirect`，兼容 `https://chatgpt.com/connector/oauth/<安全 callback_id>`；拒绝任意主机、查询串、片段、HTTP 或近似域名。所有成功和拒绝授权的回调均含原 `state` 和精确 `iss`。

请求有效期十分钟、授权码五分钟且原子单次使用、访问令牌十五分钟、刷新授权绝对期限三十天。PKCE、客户端、回调或 resource 错误不会消耗正确的授权码；正确凭据重放已使用的授权码或刷新令牌会撤销关联授权族。缩小 scope 后不能借刷新扩大回来。丢失成功换令牌响应时需要重新连接，不无限重试。

凭据均随机生成，仅保存 SHA-256 摘要；scope、issuer、audience/resource、用户、租户、客户端、状态与期限均从持久化授权记录验证，而非信任请求中的用户标识。API 与 MCP 继续使用各自数据库角色，MCP 无法读取 OAuth 注册密钥或待确认请求表。窄范围 SECURITY DEFINER 函数只按高熵凭据摘要定位/校验，业务表保持 RLS。用户/成员/租户停用、OWNER 资格取消或连接撤销都会拒绝后续访问。

每次授权创建独立的 `ChatGPT OAuth · …` 管理项，可在现有 MCP 管理页吊销。单独吊销该授权的一个访问令牌也使整个刷新授权失效，避免用户以为已撤销但被刷新恢复。正常令牌到期不撤销刷新授权。

OAuth 只允许五个市场工具：搜索、K 线、指标快照、综合行情快照、Coinglass 快照。既有 PAT 的其他只读工具不删除，但 OAuth 调用信号/告警/PAPER 交易工具会被拒绝。任何接入方式都不能下单。工具返回的数据新鲜度、缺口、NO_DATA/STALE/PARTIAL 继续如实保留，不代表收益保证。

## 部署与回退

0042 为增量迁移，不改历史行情、不删除旧 Token。先备份本次涉及代码、完整旧前端资源和 nginx 配置，设置迁移锁等待/语句超时，再执行 Alembic upgrade，重启 API/MCP、验证 nginx 后 reload。Web API/MCP 的 loopback 端口保持不对外开放，HTTP 80 仍只有 ACME。不修改现有 PostgreSQL 内存限制。

OAuth 注册和交换入口设置 nginx 跨 worker 的限速，应用另有进程内限速，数据库另有全局注册/待处理请求/用户授权上限。nginx 对含授权请求参数和回调响应的路径禁止记录请求/响应内容，关闭请求错误日志、缓存和压缩；Uvicorn 关闭包含查询串的访问日志。发现故障排查时另用严格裁剪的路径/状态日志，格式见后续记录。不得在控制台、截图、URL 示例、审计或备份清单输出真实凭据。客户端应尊重 429 和等待提示，不无限重试。

如需回退应保留 0042 数据与历史审计，只恢复经过验证的前版代码/前端/代理配置。不要直接 downgrade 删除用户新授权记录。回退为仅 PAT 的版本会使 OAuth 接入不可用；需明确告知已连接用户。

## 验证记录

本地前端：36 文件、215 项测试通过，包含登录→MFA→原请求继续、允许/拒绝、过期、双击、重新确认密码、回调目的地校验和既有 MCP 管理回归。

后端：92 项相关单元/API 回归通过，其中 OAuth HTTP 边界 20 项；真实 PostgreSQL 的 21 项 OAuth 权限/持久化/并发集成及 2 项既有模块/迁移回归通过。OpenAPI 生成一致、10 项 JSON 合约、架构边界、敏感信息扫描和定向 Ruff 均通过。

生产部署完成（2026-09-13）：数据库已升级 `0042_mcp_oauth`，API/MCP 重启、nginx 配置校验及 reload 成功。9 个常驻业务服务均运行，可用内存约 4.1 GB，未重启行情/策略/通知采集进程。

首次浏览器验收实际完成了登录/MFA、授权和桌面/手机布局验证，但测试脚本在拦截并中止外部回调导航后读取 Chromium 已清除的响应体而失败。该次临时授权已撤销。修复的是测试脚本：直接验证真实浏览器导航前截获的回调地址，不再在导航后读取响应体；并未放松服务端校验。

修复后生产完整验收通过，证据位于服务器受保护的 `/tmp/gatescope-oauth-smoke-k02h7mnv`：

- 两个资源元数据入口和授权服务器发现均 200；匿名 MCP 401，带正确 `resource_metadata` challenge；DCR 201。
- 加密登录、MFA、原请求路径恢复、1440/390 px 布局通过，无横向溢出、JavaScript 错误或意外外部请求；缺 CSRF 的批准请求 403。
- 错误 PKCE 和 resource 均 400；正确兑换 200；访问令牌十五分钟。信号/PAPER 侧非市场工具调用被拒绝。
- 同一 OAuth Token：BTC_USDT / 1m 和龙虾_USDT / 15m 均返回 100 根闭合 K 线、8/8 就绪指标；全流程分别约 0.312、0.305 秒。BTC 当次未闭合观察为 `NO_CURRENT_OBSERVATION`、Coinglass 为 `STALE`；龙虾为 `CURRENT` / `PARTIAL`。这是采集时刻的真实质量状态，未伪装成完整实时数据或更高胜率。
- 刷新轮换成功；旧刷新令牌重用 400，同时新旧访问令牌均 401。第二个独立授权的 `/oauth/revoke` 返回 200，访问与刷新均失效。
- 两次 ChatGPT 回调均在发出前被拦截；这次两个临时客户端均已撤销，浏览器退出登录 200。连同首次验收，共三个临时 OAuth 授权均已撤销。
- 独立 PAT 兼容性复测通过：BTC / 1m 与龙虾 / 15m 均 100 根、8 项就绪指标；专用临时 PAT 已撤销，撤销后 401。已有客户端和用户配置未修改。

代码/旧前端/nginx 回退资料：`data/release-backups/chatgpt-oauth-qt5heYHn`。初始部署归档 `release.tgz` SHA-256 为 `f52fd2fa9c187254ef158a281de1fd9015dedd2559c43f44ffdd4025971a673f`；验收后完整代码归档另存 `accepted.tgz`，包含修复后的测试脚本和最终记录。旧资料保留，不自动 downgrade。

生产验收使用独立的临时 DCR 客户端/授权，回调在浏览器中拦截，不把测试授权码发送给 ChatGPT；只输出状态、数量和安全错误位置。临时授权在 finally 中撤销，既有用户连接不变。协议与浏览器通过不等于已在用户本人 ChatGPT 账户完成连接，也不代替模型数据分析结果的验证。

运维容量注意：刷新按历史审计要求保留访问令牌行，正常每个连接约 96 行/天；授权绝对期限 30 天、每个授权签发上限 10 次/分钟和 256 次/天。后续多用户规模扩大时，应将管理页的到期令牌历史做服务端分页/折叠。注册从未授权且无待确认请求的客户端在 30 天后可清理，曾经授权的注册客户端不会自动过期；授权请求及已过保存期的失效凭据清理不删除行情或审计事实。

## ChatGPT 仍报“不支持 OAuth”的二次排查

用户在上述部署后仍报告 `does not implement OAuth`。不能把本机/外部协议验收成功当作用户的 ChatGPT 连接已经成功；原先“该提示已解决”的判断缺乏 ChatGPT 实际请求证据，本节记录后续诊断。

2026-09-13 14:08（Asia/Shanghai）独立机器经公网 HTTPS 检查：服务和 TLS 验证正常；用已安装的 MCP SDK 2.1.1 实际 `OAuthClientProvider.async_auth_flow` 依次 GET `/mcp`（401，解析 challenge）、资源元数据（200，模型校验通过）、授权服务器元数据（200，issuer 精确匹配）。SDK 正确生成下一步 DCR 请求，检查在发送 POST 前终止，没有注册客户端或调用模型。因此标准 GET 发现流程可用，仍不能解释 ChatGPT 自身的失败。

同时查到可复现的兼容缺口：标准元数据 HEAD 为 405，精确尾斜杠路径以及 `/mcp/` 落入 Web 登录边界，后者 401 不带资源发现 challenge。匿名发现请求还与页面/静态请求共用同一个 80 次/分钟限速桶。它们是真实缺口，但没有证据能认定就是本次 ChatGPT 报错的原因。

已完成窄范围修复并部署：

- 三个标准元数据入口支持 GET/HEAD/OPTIONS，以及它们各自的精确尾斜杠版本；issuer 和 resource 不变。
- `/mcp/` 精确代理到同一个受 Bearer 保护的 MCP 上游，保留请求体，不跳转登录；不开放任意 `/mcp/*` 路由。
- 发现请求使用独立的每客户端 120 次/分钟预算，仍忽略浏览器 Cookie/MFA 状态，不扩大任何业务权限。
- 不新增未经证明需要的 AS 路径别名，不把 OAuth 元数据伪装成 OIDC；避免错误 issuer 导致严格客户端校验失败。
- nginx 增加 `/var/log/nginx/gatescope-oauth-discovery.log`，只写时间、方法、固定路径（`$uri`，无查询串）、HTTP/upstream 状态、耗时及粗粒度来源/UA 分类。不记录原始 IP/UA、Authorization、Cookie、state、code、Token、请求体或响应体。UA 分类是客户端自报线索，不代表可信 OpenAI 身份。

本轮 62 项定向回归通过，含 46 项 OAuth HTTP 测试、nginx 日志字段/代理边界与正式路由回归；只读诊断脚本另有 26 项测试，覆盖注册前停止、禁止凭据写入、越域/路径/方法限制、次数上限及脱敏错误输出。OpenAPI、架构边界及敏感信息扫描通过，无数据库迁移和前端资源变更。备份：`data/release-backups/oauth-discovery-7MNNXE82`。

14:15 部署后外部 SDK 再次完整发现到注册前；元数据 HEAD 200、`/mcp/` HEAD 401 且带正确 challenge。可执行 `python scripts/check_mcp_oauth_discovery.py` 重现该只读检查，脚本不使用任何凭据，并在所有写请求发出前停止。最终仍需用户在 ChatGPT 重新发起一次连接，再按精简日志定位实际请求是否到达、用了哪条路径、失败在哪一步；不能先假定是缓存、IP 限制或用户配置错误。

用户随后明确反馈已经点击且仍报同样错误。14:17:45 检查发现，在开始记录之后仅有本次标记为 diagnostic 的探测，没有该次操作对应的 MCP/发现/授权 HTTP 请求；检查普通访问日志也没有遗漏到其他 OAuth 路径的请求。近半小时 nginx 错误日志无 TLS 握手失败记录。可将问题边界缩小到“尚未到达本服务 HTTP 处理链路”，但不能由此确定 OpenAI 的缓存、URL/IP 策略或公网网络哪一项是根因。已请求用户用同样通过 SDK 校验的 `/mcp/` 末尾斜杠地址做对照；在取得用户侧结果或错误详情之前，仍标记为未解决，不能声称 ChatGPT 连接成功。
