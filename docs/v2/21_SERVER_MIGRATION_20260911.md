# 2026-09-11 新服务器迁移与运行记录

后续状态：按用户要求，旧机 Nginx 和旧证书 timer 已停止，旧地址不再跳转。最新验收、性能优化和运行状态见 [部署复验记录](22_DEPLOYMENT_ACCEPTANCE_AND_PERFORMANCE.md)；下文保留迁移当时的事实。

## 访问与范围

- 正式地址：**https://43.108.44.74/markets/chart**，登录页 `/login`。
- 管理连接：`ssh root@43.108.44.74`。应用以无 sudo 权限的 `ubuntu` 用户运行，不以 root 运行。
- 项目目录：`/home/ubuntu/workspace/crypto_gate_trade`。
- 原账号密码、工作区、验证器、配置加密密钥全部保留；没有重置账号、关闭 MFA 或开启 LIVE。
- 旧主机 `43.155.154.25` 的八个应用服务和健康检查定时器已停止并禁用。旧数据库、文件、备份保留；不能直接同时启动两套 worker。
- 旧 HTTPS 入口暂时只对 GET/HEAD 跳转新地址，旧 POST 返回 503，不转发或重放登录/订单请求。新站运行不依赖旧主机。
- 本次是现有版本的完整迁移与部署，不代表 README 中尚未完成的实盘资格和统计认证模块已完成。

## 新机实际容量

| 项目 | 实际配置 |
| --- | --- |
| 系统 | Ubuntu 22.04.5 LTS，x86_64 |
| CPU / 内存 | 4 vCPU，7266 MiB 可见物理内存（8 GB 规格） |
| 系统盘 | 约 69 GiB；部署后约 58 GiB 可用，并非 200 GB |
| 交换空间 | 2 GiB，`swapfile.swap` 开机启用，swappiness=10 |
| 数据库 | PostgreSQL 14.24 + TimescaleDB 2.19.3，与源库版本一致 |
| 应用依赖 | `uv sync --frozen`，重新创建虚拟环境；没有复制旧 `.venv` |
| 前端 | 迁移已有正式 `web/dist`，全部文件 checksum 比较无差异 |

部署验证时可用内存约 5.1 GiB，swap 使用为 0。此数值是当前自选规模下的实测，不是压力测试容量保证。增加币种、回放周期、用户后，需要持续检查磁盘和数据库增长。

## 一致性与恢复证据

旧库因 OOM 停止。先停止全部应用写入，采用 `migration-source-memory.conf` 降低旧库内存恢复数据库，再导出最新快照；没有用前一天的旧备份代替最新数据。

迁移快照相对目录：`data/runtime-secrets/migration-20260911-new-server-v2/`。

- 71 张 public 业务表合计 **1,250,903** 条记录；新库启动任何应用写入前逐表 `count(*)`、schema revision、扩展版本全部一致。
- `bars`：**1,035,632**；形态事件：42,391；结构收益：49,132；结构阶段：14,709。
- 1 个自选列表 / 2 个标的；7 条回测及 7 条结果；1 个 Telegram Bot 配置。
- Schema：`0025_annual_structure_history`。
- `gatescope_security.mcp_token_locators` 源库、新库均为 0。
- dump 大小 122,845,657 字节；dump、角色 SQL、数据库创建 SQL、清单的 SHA-256 均验证通过。
- 仅迁移 GateScope 角色及其成员关系，不导出其他应用角色密码。角色口令哈希、表所有者、GRANT/RLS 随迁移保留。
- 按 TimescaleDB 的 `timescaledb_pre_restore()` → 单进程 `pg_restore --exit-on-error` → `timescaledb_post_restore()` 恢复，随后 ANALYZE；恢复模式已确认为 `off`。
- 新机另外保留 postgres 专用的恢复材料目录 `/var/lib/postgresql/gatescope-migration-20260911/`，目录 0700、文件 0600。

业务启动后计数正常增长，所以不能再直接用停机时的清单判定当前库“完全一致”。旧快照用于迁移证据与灾难恢复；回退前必须先保存新机新增数据。

## HTTPS、端口与凭据

- 公网业务入口仅 HTTPS **443**。17758（API）、17800（只读 MCP）和 5432（数据库）均只监听回环。
- 80 仅用于 `/.well-known/acme-challenge/` 证书验证，其余请求直接断开，不提供明文业务页面。不能在安全组关闭 80 后仍假设 HTTP-01 能自动续期。
- 新 IP 已取得受信任 Let’s Encrypt IP 证书。Certbot 5.8.0 独立安装在 `/opt/gatescope-certbot`。
- `gatescope-certbot-renew.timer` 每六小时检查续期；deploy hook 校验并 reload Nginx。已成功执行 `renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew`。
- 密钥只通过 SSH/rsync 传输。`data/runtime-secrets` 为 0700，环境和原登录记录文件为 0600；密钥未写入此文档或源码。
- 原始登录记录在新机 `data/runtime-secrets/owner-login.json`。不要在聊天、工单、git 或截图中展示该文件。
- 新站沿用前端加密提交。验证脚本必须使用公开提交密钥构造加密登录封装；公网直接提交旧脚本的明文登录 JSON 会返回 403，这不是账号丢失。
- 已有 Telegram Bot 的密钥成功解密，回调已从旧 origin 切换到新 origin，并通过 Telegram `getWebhookInfo` 回读验证；没有发送测试消息、丢弃待处理更新或提交订单。
- 如今后设置交易所/API IP 白名单，应使用新机出口 IP 并以交易所实际校验为准；本次快照没有交易所账户（`exchange_accounts=0`）。MCP 保持原有仅回环部署方式，不另行开放公网端口。

## 稳定性防护

- 八个工作负载都由 systemd 开机启动、失败自动重启。
- `gatescope.slice` 限制应用合计内存：soft 2 GiB / hard 3 GiB；单服务 soft 512 MiB / hard 768 MiB，BLAS/OMP 单线程，限制 task 数。
- PostgreSQL shared buffers=768 MiB、work_mem=4 MiB、maintenance=128 MiB、最大连接=60；整个实际数据库 cluster soft 2 GiB / hard 2.5 GiB。限制覆盖文件缓存，不应把 systemd MemoryCurrent 当成纯 RSS。
- 对 **`postgresql@14-main.service`** 配置 `Restart=on-failure`，不能只看 `postgresql.service` 的 active/exited 状态。
- 健康检查每分钟检查数据库、启用的应用服务、内外网 readiness。数据库不可用时不循环重启所有 worker；连续失败后只尝试恢复已失活的实际 cluster。
- 手工维护前停止 `gatescope-healthcheck.timer`，或放置 `/run/gatescope-maintenance` 标记。检查器不会重新拉起明确禁用的 TESTNET 或其他服务。
- 数据库硬限额和 swap 不能保证永不 OOM；外部云故障、磁盘耗尽、上游断网仍需要告警和运维响应。

## 验证记录

- 公网受信任 HTTPS readiness：200，数据库 `ok`。
- 原账号密码验证、加密登录、MFA 验证：通过；验证会话最后已主动退出，没有创建/重置验证器或恢复码。
- BTC_USDT、ETH_USDT × 全部八周期：每个接口返回 100 根唯一、有序、符合当前已收盘周期的新鲜 K 线。
- 自选历史进度接口：16 项；策略目录：3 项；回测列表：7 项。
- Telegram、交易所配置接口可读；market worker healthy；LIVE hard gate=false。
- 十个正式 Web 二级模块路由返回构建后的应用页面；并非本次重新做了所有功能的写入/实盘端到端测试。
- 健康检查实际执行成功，证书续期演练成功；服务资源配置更新后重新启动并复验。
- 新增健康检查用例与密钥准备用例共 6 项通过；迁移相关 Python 文件 Ruff 通过；源码秘密扫描通过。

## 运维与回退

```bash
ssh root@43.108.44.74
systemctl status gatescope-api gatescope-market gatescope-strategy
systemctl status postgresql@14-main
systemctl list-timers gatescope-healthcheck.timer gatescope-certbot-renew.timer
curl --fail https://43.108.44.74/health/ready
free -h
df -h /
```

配置文件修改后需要 `systemctl daemon-reload` 并重启相应服务。不要执行 owner 初始化、密钥轮换、重新 enroll MFA 来“修复”迁移；这会破坏现有用户配置。

`scripts/migrate_snapshot.py export <新私有目录>` 仅适用于停止写入后的迁移快照；不是在线备份调度器。`verify` 只应在目标恢复完成、尚未启动写入时使用。`scripts/verify_migrated_runtime.py` 在新机执行已授权的私有登录验证，不输出密码或 MFA 秘密。

迁移快照不能代替持续备份。当前还没有本次新增的异地自动备份服务，应在扩大正式用户使用前安排独立备份存储和恢复演练。

必须回退时：先停止新机 timer 和全部 worker、导出新机最新库并保留；确定新增记录的回迁方案后，恢复旧 Nginx 配置、旧 public origin 和 Telegram 回调，最后才能启动旧机服务。不要同时运行两个写入端。旧机内存问题依旧存在，回退只适合短时应急，不是长期部署方案。

恢复依据：[PostgreSQL 14 逻辑备份](https://www.postgresql.org/docs/14/backup-dump.html)、[TimescaleDB 逻辑恢复](https://github.com/timescale/Tiger-Data-Docs/blob/main/src/content/docs/deploy/self-hosted/backup-and-restore/logical-backup.mdx)。IP 证书自动化依据：[Let’s Encrypt Certbot IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)。
