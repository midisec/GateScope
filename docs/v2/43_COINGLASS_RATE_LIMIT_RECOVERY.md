# Coinglass 限流恢复与缓存保真

日期：2026-09-13。对应数据库迁移：`0043_coinglass_cache_cooldown`。

## 故障与边界

生产 BTC_USDT 1h、ETH_USDT 1d 缓存留有约七小时前的 RATE_LIMITED，原始 Retry-After 为 10 秒。旧投影没有让限流状态过期；MCP 只读缓存且不申请补采，非默认周期不再进入后台任务。同期默认 15m 缓存正常刷新，所以不能把旧错误描述为持续七小时的上游限流。

本次不提高上游额度、不绕过限流、不把过期数据当实时数据，也不修改形态历史证据。资金费率单位仍未获得可验证的 relay 元数据，继续标注 UNKNOWN 并不参与定量研判。

## 实现

- 客户端：同密钥图表采集和连接测试共享有界请求节奏；账户、目录也计入预算。根据 relay 返回的 rate_limits 调整间隔，缺少元数据时采用保守默认。HTTP 200 仍检查业务码 429。密钥只发送到固定允许域名的请求头。
- 持久冷却：私有数据库表按密钥指纹共享 Retry-After；每次 HTTP 前检查，重启后仍有效。MCP 只有受当前用户和凭据版本约束的等待查询和缓存入队权限，无凭据/冷却表直读权限。长 Retry-After 不截成一小时。
- 任务：每用户最多 256 个缓存键，全局 8192。默认自选周期常驻预热，额外请求周期保持热度两小时；不恢复所有周期的无差别轮询。任务租约 180 秒；主动节流 defer 不伪造采集失败或修改原数据时间。8 个有界身份缓存复用目录和额度信息，轮换时关闭旧客户端。
- 失败保留：仅相同用户、配置版本、合约维度、周期与验证单位的数据可保留。保留系列强制 STALE，原 fetched_at/as_of/expires_at/逐点 available_at 不改；不进入新入场研判。部分刷新可同时含新系列与明确过期的旧系列。
- 当前与历史：retry_at 是绝对时间；旧相对提示锚定原采集时刻，读取不会重新开始倒计时。过期 RATE_LIMITED 转 QUEUED/DEFERRED，原错误放在 last_refresh_status/last_safe_errors/last_reason。refresh_requested 反映真实入队，不在队列满时声称成功。
- UI：显示剩余冷却时间、历史缓存标识与原时间；冷却中禁用刷新/研判，暂停轮询，到期恢复。切换用户、币种、周期取消旧请求，避免串数据。
- MCP：币种和周期仍来自请求参数，返回当前用户缓存，同时申请异步补采；MCP 请求线程不调用 Coinglass。
- 连接测试：遇到限流停止后续探测；暂时限流/网络错误不撤销已验证 ACTIVE 配置。新密钥仍需通过验证，明确鉴权错误不被当成暂时故障。

## 验证与运维

自动化覆盖并发请求节奏、HTTP/业务码 429、长期 Retry-After、身份隔离、单位/维度保护、失败保留、冷却到期、凭据轮换、过期任务、队列 cap、MCP 最小权限、前端倒计时与切换取消。PostgreSQL 集成使用隔离测试库和 fake provider，不向上游制造限流。

部署顺序：保护备份 → 先应用 0043 → 同步代码与构建资源 → 重启 API/MCP/market → HTTPS 健康与 MCP 实测。保留 OAuth 隧道、现有授权及服务配置。新代码依赖 0043，回滚先恢复原代码再降级数据库。

`scripts/verify_deployed_mcp.py --coinglass-recovery` 创建仅本次验证的一小时只读客户端，经真实 HTTPS MCP 请求 BTC 1h / ETH 1d，输出状态/时间/点数等脱敏摘要，最后撤销该新客户端并验证 HTTP 401。脚本成功只代表 MCP 协议和清理通过；是否恢复新数据还必须检查摘要中的状态、时间和各系列点数，不能仅依据 ok=true 判断。

## 上线实测结果

2026-09-13 19:08（北京时间）已在 43.108.44.74 部署。保护备份位于生产项目 `data/release-backups/coinglass-recovery-GmHZmjHi`（root-only）；包含修改前文件、数据库 schema 与目标缓存表备份，不含明文 API key 输出。事务迁移先校验旧版本为 0042，成功升至 0043。同步 39 个文件的 SHA-256 与本地一致；market 停止超时提升为 180 秒，避免限速等待中的请求被提前强杀。

- 首次真实 HTTPS MCP 查询：BTC 1h、ETH 1d 都由旧 RATE_LIMITED 投影为 QUEUED，refresh_requested=true、retry_after_seconds=0。
- 后台实际取数完成：BTC 1h 于 19:09:35、ETH 1d 于 19:10:34 形成新缓存。各自的 oi、lsr、liquidation_long、liquidation_short 均 READY，各 255 点、单位 VERIFIED；资金费率 UNKNOWN/MISMATCH、0 点，因此整体状态准确地为 PARTIAL，不宣称全部维度可用。
- 再次真实 HTTPS MCP 查询返回上述新缓存和 0 秒冷却；两次验证均只撤销本次新建客户端，撤销后 HTTP 401，现有客户端未修改，没有调用 OpenAI 模型或下单。
- API、MCP、market、nginx、既有 tunnel 均 active；数据库 readiness 200，新前端资源 200。OAuth 同源发现检查通过；隧道严格健康（含成功控制面轮询）退出 0/result=ok，保留 8 个 OAuth 目标。
- 最终后端全量单元测试 1114 项通过（1 项第三方弃用警告）；独立 PostgreSQL 集成 11 项通过；前端 233 项、TypeScript 和构建通过。隔离 PG 测试集群恢复原先停止状态。

脱敏验收摘要见 [coinglass-recovery-20260913.json](evidence/coinglass-recovery-20260913.json)。未对真实上游故意制造 429；真实限流仍会按其 Retry-After 冷却，此修复不保证上游永远不再限流。
