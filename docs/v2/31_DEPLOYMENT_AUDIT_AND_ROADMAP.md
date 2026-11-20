# 线上代码审查与交易体验改进建议

审查日期：2026-09-12，主要生产检查在 10:25–10:35 UTC+8。

范围：`43.108.44.74` 的已部署程序、只读运行状态，以及与之对应的本地源码。此次未修复或发布业务代码、未重启服务、未修改用户配置、未创建交易或发送 TG 消息；本文件仅为本地审查交付物。

## 结论

当前运行资源充足，研究功能可用，但“形态识别 → 告警 → 执行 → 实际收益验证”仍存在断点，不能认定为已经完成可靠的自动交易产品。发现了可复现的功能问题，其中包含上一轮高置信筛选的长周期门槛设计缺陷。已有测试通过，不等于已覆盖本文故障场景。

证据标签：**生产核实**＝只读配置/状态或低频未登录 GET；**本机复现**＝真实函数/模拟外部依赖的确定性复现；**代码审查**＝已部署同版本代码路径检查；**待外部验证**＝交易所行为、云安全组或云快照尚未验证。

## 运行快照与检查边界

- `src/apps/web/src/migrations/deploy` 中 222 个源码/配置文件 SHA256 与生产完全一致；`uv.lock`、`pyproject.toml`、前端 lockfile 和 `web/dist/index.html` 也一致。首次清单命令因远端未安装 rg 失效，已改用 find 重查；未采用失效清单作结论。
- 8 个应用服务均 active/running，当前 systemd `NRestarts=0`。这不是整个部署历史从未重启的证明。
- 内存 7,266 MiB，可用 4,611 MiB；磁盘约 43 GiB 可用。当前没有内存瓶颈证据。
- 2 个自选币种、16 个周期的行情更新年龄 0–6 秒；当前无 live_error。3.2 跟踪 16/16、错误 0；一年回放 8 个 READY、8 个 RUNNING，无 safe_error。
- 每币 1m 历史仍缺 3,835 根，5m 缺 3 根、15m 缺 2 根、30m 缺 1 根；状态 SOURCE_LIMIT，不应表示为完整一年覆盖。
- 最近 24 小时 TG 查询队列 9 条，均 SENT。本轮没有通过 TG 发送测试消息。
- 交易账户数 0，执行认证数 0；executor 启动代码硬禁用 LIVE。本文执行风险是 TESTNET 获准执行后的潜在故障，不是已发生实盘亏损。
- HTTPS readiness 200，证书校验成功、TLS 1.3，证书到期时间 2026-09-17 17:34:23 UTC；续期与健康检查 timer 已启用。
- 未登录访问 `.env`、`.git/config`、`config.json`、`openapi.json`、交易所账户 API 均 401，具备 CSP/HSTS/nosniff/frame-deny。未读取响应中的秘密内容，未进行口令喷洒、压力攻击或跨用户数据读取。

## 必须优先处理的交易与信号问题

### T1 · P1：新图表与正式告警使用不同的“已确认”定义

**本机真实检测器复现 + 部署代码一致。**

正式告警 [strategy_runtime.py](../../src/gatescope/application/strategy_runtime.py#L58) 仍调用 legacy `scan_patterns`；[postgres_strategy.py](../../src/gatescope/adapters/postgres_strategy.py#L160) 持久化版本为 `pattern-engine-1.0.0`。形态面板与新筛选使用 `structure-study-3.2` 生命周期。

取现有双底测试样本顺序回放，增加第一根突破收盘 142：legacy 已返回已确认，3.2 仍为 FORMED、连续确认计数 1；再增加 143，3.2 才确认。用户因此可能收到“确认”推送，图表却仍显示“已形成”；新版历史概率也不能直接解释旧引擎告警。

建议：统一版本、候选 ID、阶段迁移和方向，新增共享事件订阅。迁移旧规则需保留旧版本和用户选择，不能悄悄改写历史确认时间。验收：同输入在图表、TG、回测/影子账本上的阶段和确认时间一致。

### T2 · P1：正式告警缺少相对于当前时间的新鲜度与发送过期闸门

**本机真实 runner + 模拟 scanner/仓储复现。**

[strategy_runtime.py](../../src/gatescope/application/strategy_runtime.py#L55) 以旧数据的 available_at 为 watermark；[alerts.py](../../src/gatescope/application/alerts.py#L80) 只比较形态与最后一根旧 K 线，发送端 [notifications.py](../../src/gatescope/application/notifications.py#L193) 没有业务 TTL。

输入截至 2024-01-01 的 20 根 K 线，当前为 2026-09-12，runner 仍可首次发出 1 条告警。既有事件去重不能阻止首次发送陈旧事实。当前行情虽然新鲜，断网、重启或积压时仍会触发这一缺陷。

建议：检测端检查应收盘时间、缓存错误与可用时间；发送前检查信号 TTL。旧事件保留入档但标注过期，不再作为“当前可交易”信号。

### T3 · P1：一次查询失败会停止已有仓位的自动保护监控

**本机故障注入复现；生产目前未开交易账户。**

[application/testnet_execution.py](../../src/gatescope/application/testnet_execution.py#L148) 将普通网络异常也转换为 MANUAL_INTERVENTION_REQUIRED；[postgres_testnet_execution.py](../../src/gatescope/adapters/postgres_testnet_execution.py#L643) 清空 next_reconcile_at，而领取逻辑只接受 PROTECTED。

仅一次 get_position 网络异常，就得到 frozen=True 并永久离开自动检查队列。已有交易所条件单不会因此自动取消，但它之后若部分成交、到期或异常，程序不再继续检查。

建议：把“禁止新开仓”与“维护已有风险”分开。网络未知状态进入可恢复的只读对账队列，有限退避、持续告警，恢复后根据交易所事实决定下一步；不能把未知回执直接当作重发订单的依据。

### T4 · P1：部分止损后的剩余仓位没有自动降险

**本机故障注入复现。**

[gatescope_executor/testnet_execution.py](../../src/gatescope_executor/testnet_execution.py#L489) 要求仓位等于最初成交量，否则在检查止损腿之前直接返回人工状态。

复现：原 10 张，止损只退出 6 张、原止损不再 open，剩余 4 张；程序返回 SIZE_MISMATCH，后续交易所 mutation 数 0。应区分同方向减仓、意外增仓和反向仓位，而不是全部视为同一异常。

建议：部分退出后按事实仓位重建保护或 reduce-only 降险，并处理撤单/成交竞态。验收必须覆盖 10→4→0、零仓复核、保护单到期和进程重启。

### T5 · P1：3 秒保护期限是事后检查，不是端到端时间约束

**虚拟时钟复现。**

成交后先同步查询仓位、挂止损并回查，直到 [testnet_execution.py](../../src/gatescope_executor/testnet_execution.py#L246) 才检查期限。查询允许 8 秒 read timeout。

模拟仓位查询耗时 8 秒，止损直到 t=8s 才首次提交，随后才报告超时并尝试退出。最终尝试降险不等于兑现 3 秒内保护的承诺。

建议：成交起统一 deadline，所有外部调用使用剩余预算；保护与降险有独立调度，记录实际未保护时长。交易所侧条件单也不能保证极端行情按指定价格成交，应区分挂单覆盖与最终成交。

### T6 · P2：ALL 可把多空冲突当成同时满足

**本机复现。**

[alerts.py](../../src/gatescope/application/alerts.py#L44) 的观察对象没有 direction；ALL 仅要求形态集合齐全。双顶+双底可得到有效组合告警，而研究侧 [research_rules.py](../../src/gatescope/quant/research_rules.py#L29) 同向投票相抵时弃权。

建议：保留冲突观察，但明确标识“方向冲突”，同向共振与冲突观察使用不同规则，回测和告警采用一致语义。

### T7 · P2：高置信筛选对长周期存在不可达门槛

**数学上界 + 本机纯计算复现。上一轮新增功能中的设计问题。**

[structure_screening.py](../../src/gatescope/quant/structure_screening.py#L13) 固定持有 20 根、最少 100 个样本；[postgres_telegram_market.py](../../src/gatescope/adapters/postgres_telegram_market.py#L124) 只取一年，收益窗口不可重叠。

| 周期 | 一年 K 线 | 每种形态/阶段的非重叠理论上界 | 统一门槛 |
| --- | ---: | ---: | ---: |
| 4h | 2,190 | 109 | 100 |
| 8h | 1,095 | 54 | 100 |
| 1d | 365 | 18 | 100 |

实际还要扣除前置窗口、结构起点、缺失数据和方向分组，通常更少。8h/1d 在当前政策下永远不达标，不是再等待缓存一阵就会解决。

建议：显示样本可达性；长周期单独设计更长历史和预先冻结的验证政策。不要为了出现达标项而随意降低门槛，亦不能混合不同周期/方向来虚增独立样本。

### T8 · P2：筛选达标不等于可交易正期望

**研究能力边界，不是隐瞒成本的计算错误；界面已有免责声明。**

当前统计是固定持有期减 10bp，总体平均净收益只要求 >0，没有实际资金费、结构止损、执行延迟及收益下界。

本机输入 800/1000 胜、平均净收益 0.002%，仍 qualified=True；额外 2bp 成本后变为 −0.018%。此外，实际跟踪路径 [postgres_pattern_studies.py](../../src/gatescope/adapters/postgres_pattern_studies.py#L302) 清空 facts，没有像回放路径一样累计真实发现后的收益账本。

建议：增加实际信号影子账本、真实成本区间、成本压力检验、样本外净期望下界；胜率同时展示平均盈亏、频率、回撤与执行损耗。现有夏普 >1.5、最大回撤 <15%、命中率 >55%、t >2 的部署条件继续保留，不因筛选列表达标就豁免。

## 体验缺陷

| 编号 | 问题与证据 | 建议/验收 |
| --- | --- | --- |
| U1 / P2 | [App.tsx:541](../../web/src/App.tsx#L541) 自选 BTC 请求未返回时，通过搜索选 ETH，旧 BTC 响应会覆盖 ETH。原样源码函数本机复现最终 BTC。两个自选连续点击受 useAction 锁影响，不是这一复现入口。 | 所有选择入口共享 generation/取消标志；旧请求不能覆盖最后选择，初始化也必须遵守。 |
| U2 / P2 | [App.tsx:514](../../web/src/App.tsx#L514) 指标请求失败后，网络恢复但闭合 K 线指纹未变，不触发重试；刷新状态只重新拉 bars。源码 effect 复现调用数保持 1，出现新闭合 K 线才到 2。 | 独立指标错误/重试状态，连接恢复有限退避；手动刷新应覆盖图表与指标。 |
| U3 / P3 | [telegram_market.py:92](../../src/gatescope/application/telegram_market.py#L92) 旧 offset=12、当前仅 5 个候选时，仍显示第3页且没有详情按钮，退一页仍为空。真实菜单函数复现。 | 切片前钳位到最新合法页；币种组与状态页都覆盖列表缩小测试。 |

这些复现属于源码函数与依赖层，不是本轮真实浏览器操作测试。前端 48 项测试、类型检查通过；TG/筛选 11 项、量化相关 28 项、执行相关 24 项既有测试通过。测试组部分可能重叠，不将其简单相加作独立测试总数。

合理但需要增强的既有设计：终态图冻结到状态时间，保护了历史证据，不应直接改画。另增“追踪至今”视图，保留首次图、终态图，同时显示当前价格、预测目标/失效距离、最大有利/不利变动。返回筛选应保留币种组、模式与页码。

## 安全与部署风险

依据 security-audit 的源码优先方法，生产只作低频只读验证；不将配置风险或依赖公告自动等同于已被攻击。

1. **MFA 缺少独立的账号失败预算（本机复现 + 代码审查）。** [formal_auth.py:302](../../src/gatescope/formal_auth.py#L302) 失败只写审计；[security.py:140](../../src/gatescope/security.py#L140) 是通用每会话 240/min。隔离的真实 service/middleware 连续 12 个错误验证码都是 401，没有 429。口令已有 DB 失败锁定，不能把它误说成 MFA 也有。建议账户级持久化预算、冷却、异常提示，保留合法恢复路径。参考 [OWASP 登录节流](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html#login-throttling)。
2. **主机 SSH 加固不足（生产配置核实）。** root 密码状态 P，sshd 允许 root 登录和密码认证，UFW inactive。云安全组未核实，因此不声称所有来源都能访问。建议先建立/验证受限管理入口及第二条可用密钥会话，再关闭 root 密码登录；本轮未改 SSH，避免锁出。参考 [Ubuntu OpenSSH](https://documentation.ubuntu.com/server/how-to/security/openssh-server/)。
3. **服务间秘密隔离不足（生产权限核实）。** API 与 executor 都运行于 ubuntu；总 runtime 密钥文件虽然 0600、目录 0700，但同一服务 UID 可读，ProtectHome=read-only 不阻止读取。此项不是已发现的 HTTP 读取漏洞，而是任一服务失陷后的扩大影响面。建议不同系统用户与服务独立凭证装载。
4. **依赖维护缺口（版本核实，漏洞可达性分开判断）。** 前端 production npm audit 0 公告；生产 cryptography=46.0.7、内嵌 OpenSSL=3.5.6，安装器 uv=0.8.22。cryptography 有下列官方公告，pyproject 的 <47 上限会阻止修复版本。未发现本项目调用相关 X.509 verifier/PKCS#7 解密 API，不能宣称当前 HTTP 可利用或 RCE。现有信封采用 OAEP+GCM，不是 PKCS#7。本机 audit 的 pip/setuptools/pytest 告警不自动算作生产运行时告警；生产 venv 未安装 pip/setuptools/uv 包，uv 是独立安装工具。安排隔离环境升级兼容性回归，不在线盲升。
5. **灾备与存储需补齐（部分生产核实）。** DB 11,926 MiB，形态事件表含索引约 6.48 GiB；archive_mode=off，未启用 WAL 连续归档。仅查看到既有手工备份，云快照及其他异地机制未核实。建议自动备份、异地副本、PITR 与恢复演练，并评估形态事件分区/压缩/冷热存储，不能删除失败证据凑空间。PostgreSQL 的时间点恢复依赖基础备份及连续 WAL：[官方说明](https://www.postgresql.org/docs/14/continuous-archiving.html)。

| cryptography 官方公告 | 修复版本 | 已核实使用范围 |
| --- | --- | --- |
| [GHSA-m2h6-j472-rp4c](https://github.com/pyca/cryptography/security/advisories/GHSA-m2h6-j472-rp4c) | 49.0.0 | X.509 名称约束；未发现应用调用对应 verifier |
| [GHSA-g6cj-pr64-35w5](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5) | 50.0.0 | PKCS#7 解密 oracle；未发现应用调用该接口 |
| [GHSA-jwv3-5hgf-82ww](https://github.com/pyca/cryptography/security/advisories/GHSA-jwv3-5hgf-82ww) | 49.0.0 | X.509 路径构造 DoS；未发现应用调用该 verifier |
| [GHSA-537c-gmf6-5ccf](https://github.com/pyca/cryptography/security/advisories/GHSA-537c-gmf6-5ccf) | 48.0.1 | wheel 内嵌 OpenSSL；生产后端版本已查，未证明攻击入口可达 |

另外两个执行检查项为外部条件待验证：IOC ACK 为 open 且 left=size 时被当作安全未成交；保护重验只核数量/合约/reduce-only，未完整比对触发价、规则和 price_type。纯模拟输入能够触发，但真实 Gate 是否在约定时序返回/允许这些变化，需专门 TESTNET 联调，不等同已发生故障。

## 建议实施顺序与验收

| 批次 | 交付 | 必须通过的验收 |
| --- | --- | --- |
| A：正确性与风险闭环 | T1–T7，优先时效、模型统一和保护恢复；MFA/SSH/依赖隔离计划 | 同事件各端一致；过期告警不推；网络未知不盲重发；部分退出仍有保护；长周期明确可达性；不改旧事实 |
| B：交易体感 | U1–U3、TG 原消息内更新、返回筛选保留上下文、独立追踪至今 | 快速切换最后选择获胜；断网恢复指标能重试；列表缩小不空页；首次/终态图不重画 |
| C：真实表现闭环 | 自动研究用 PAPER 影子账本、执行质量面板、风险预算卡 | 记录发现/确认/入队/送达/可成交时间，费用与资金费、漏单、滑点、止损覆盖；每个样本可追溯 |
| D：经过验证的信号改进 | 趋势/震荡/高波动分层、多周期方向一致性、回踩确认、成本/流动性过滤 | 先固定规则，时间顺序样本外验证，与基线同数据同成本对比；报告净期望/回撤/频率，保留失败样本 |

批次 D 是待验证研究方向，不是已证明有效策略。更高周期只能使用当时已收盘的数据；不同指标和形态可能来自同一价格变化，不能简单把“多个同向信号”当成独立概率相乘。Coinglass OI/资金费等特征仅在确认数据权限、历史覆盖及 available_at 后接入，不能用今天的快照解释过去。

反复筛选直到样本内指标好看，会产生选择偏差；建议复用已有的滚动样本外、选择偏差与绩效门禁组件，增加未参与调参的 PAPER 观测验证。[Bailey 等《The Probability of Backtest Overfitting》](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) 解释了这种风险；本文具体交付方案是基于该项目的工程建议，不是论文对这些策略有效性的背书。

衡量目标是扣除真实成本后的可靠决策与净收益风险比，不只看命中率。任何新增过滤器都同时报告减少了多少机会、样本区间是否变宽、平均盈亏和回撤是否改善；无法用现有证据承诺胜率或利润提升。
