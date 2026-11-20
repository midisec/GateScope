# 2026-09-19 生产磁盘写满与数据服务恢复

## 结论和处置范围

北京时间 2026-09-19 19:34–19:44 检查生产服务器 `43.108.44.74`。
系统盘可用空间为 0，PostgreSQL 数据文件/WAL 写入失败、持续重启，导致页面无法获取数据。
用户授权“如果硬盘满了删除备份数据”；本次只删除经逐一核验的旧数据库备份，不删除行情、形态历史、订单、配置或 WAL。

删除 12 个备份后数据库自动恢复；API 仍不响应，因此仅重启 `gatescope-api.service` 一次。
后台行情、策略、研究和模拟交易循环自行恢复，没有重启这些 worker。

## 证据

- 根分区 `/dev/vda3`：约 69 GiB，总使用率 100%，普通进程可用空间为 0；inode 使用率约 4%。
- 内存：总计 7,266 MiB、可用约 5,773 MiB，检查窗口未发现 OOM；不是内存不足。
- PostgreSQL 日志包含 relation 扩展写入失败和 WAL 磁盘空间不足。
- `postgresql.service` 的 umbrella 状态不能代表数据库就绪；实际 `postgresql@14-main.service` 在重试，`pg_isready` 最初无响应。
- 19:37:47 PostgreSQL 完成启动恢复并接受连接。
- 清理后根分区约 97% 使用率，普通进程可用约 2.6 GiB。

恢复后数据库总占用约 56,940,440,923 字节（53.03 GiB）。主要关系总占用（含相关索引/TOAST）如下；这是物理容量，不是行数：

| 关系 | 字节数 |
| --- | ---: |
| `market_pattern_events` | 33,219,420,160 |
| `market_structure_stages` | 10,185,007,104 |
| `market_structure_returns` | 8,567,046,144 |

前三项合计约 48.40 GiB。事故后的累计统计可能已重置，不能用当时的 `n_live_tup` 推断业务记录数量或数据丢失。

## 删除清单与边界

删除前核验：普通文件、非符号链接、精确文件大小、没有打开句柄、没有发现服务配置引用，且没有进程使用安装目录作为当前目录。
删除采用精确路径白名单，不使用递归目录删除。

| 备份路径 | 文件字节数 |
| --- | ---: |
| `/root/gatescope-install/before-strategy-20260911.dump` | 198204155 |
| `/root/gatescope-install/before-discovery-20260911.dump` | 249384203 |
| `/root/gatescope-install/before-directional-20260911.dump` | 344470677 |
| `/root/gatescope-install/db-before-structure-3.2-20260912.dump` | 1400575728 |
| `/root/gatescope-install/before-structure-coverage-20260911.dump` | 378958107 |
| `/root/gatescope-install/before-background-memory-20260911-verified.dump` | 499709575 |
| `data/runtime-secrets/before-pattern-studies-0022-20260910.dump` | 37726713 |
| `data/runtime-secrets/before-structure-rules-0024-20260910-025309.dump` | 39836260 |
| `data/runtime-secrets/before-structure-lifecycle-0023-20260910-024036.dump` | 38140485 |
| `data/runtime-secrets/before-market-cache-0021-20260909T121839Z.dump` | 27302000 |
| `data/runtime-secrets/before-annual-history-0025-20260910-040020.dump` | 41931867 |
| `data/runtime-secrets/migration-20260911-new-server-v2/gatescope.dump` | 122845657 |

相对路径均相对于生产项目 `/home/ubuntu/workspace/crypto_gate_trade`。
合计文件大小 3,379,085,427 字节（3.147 GiB），实际释放文件分配块约 3,379,130,368 字节。
文件系统保留空间及持续业务写入使普通进程最终可用空间不等于删除文件的总大小。

这些服务器端备份已删除，未验证是否存在其他可恢复副本。未清空 `runtime-secrets`、安装目录或数据库目录，未修改磁盘保留块比例，未执行 `pg_resetwal`、数据库重建或历史数据清理。

## 恢复验证

19:41–19:44 的复核结果：

- 公网 HTTPS `/health/ready` 返回 200，`status=ready`、`database=ok`；本地 live/ready 均为 200。
- 实际现有 OWNER 加密登录及现有 MFA 验证成功，未重置密码/MFA；测试结束正常注销会话。
- 读取自选 BTC、ETH、龙虾、SOL、BNB，逐一验证 `1m / 5m / 15m / 30m / 1h / 4h / 8h / 1d`。
- 40/40 个认证行情请求成功，每个抽样最近 5 根 K 线，币种/周期字段匹配、时间递增；耗时 13–80 ms（服务器侧经公网 HTTPS 地址访问，不代表终端用户网络延迟）。
- 1m 最近返回的已收盘 K 线开盘时间为 19:42 CST，其他周期与对应收盘边界匹配。
- 40 个自选周期缓存均在最近 60 秒内成功同步，未见 live_error。
- 最后复核六个业务心跳：market 0 秒、paper-order 0 秒、paper-legacy 2 秒、research 4 秒、shadow 5 秒、strategy 3 秒，均 running，safe_error_code 为空。
- 历史研究状态 36 READY / 4 RUNNING，运行中的进度及形态跟踪时间继续更新。

本次是容量/服务恢复与行情抽样验证，不是全库完整性检查，也没有验证所有功能、发送 Telegram 测试消息或创建任何实盘/模拟订单。未承诺事故窗口内不存在缺失数据。

## 剩余风险与后续建议（未实施）

1. 97% 使用率仍危险，2.6 GiB 只是应急余量；备份已经不是主要占用，不能依赖反复删除备份维持运行。应尽快由用户扩容磁盘，或授权制定存储优化方案。
2. 优先检查历史形态事件、阶段快照、收益记录的重复载荷、保留粒度与索引占用；保留判断时点、失败案例和统计依据，不能为省空间直接丢弃历史证据。
3. 备份应转移到异机/对象存储并验证可恢复性；本次没有购买资源、挂载新盘或转移数据。
4. 容量告警应结合可用字节数和增长速度，并与历史回放限流协调；暂停非关键任务会改变后台能力，需要另行设计和批准。本次没有改变任务策略。
5. 在仅剩少量空间时不要执行 `VACUUM FULL`、全表重写或大型新索引构建；需要事先估算临时空间和停机影响。普通删除记录也不会自动等量归还操作系统磁盘空间。
