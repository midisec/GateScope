# MCP / Web 共用模拟交易

实现版本：MCP 1.3.0；数据库迁移 `0046_paper_order_execution`。
上线验收结果另行记录，本文描述协议与实现，不把实现完毕等同于线上已验证。

## 入口与授权

网页入口 `/trading/paper`。账户沿用 `paper_accounts`，不因 MCP 建立连接而自动生成第二份账户。
网页可以创建 USDT 模拟账户；选择账户后查看现金、可用余额、占用资金、风险额度、委托、持仓和成交。
原有信号／提案／K 线模拟交易保留原记录与原模型，不伪装成新盘口撮合成交。

在用户名下的 MCP 设置创建令牌，或重新连接 ChatGPT OAuth 时明确勾选：

- `paper:read`：读取本人模拟账户、持仓和账本，允许不落单的盘口预估。
- `paper:trade`：允许模拟开仓、平仓与撤单；必须同时选择 `paper:read`。

默认仍是原有只读权限；迁移、刷新令牌、读取工具列表均不自动扩权。
后端每次变更检查当前用户、工作区和 OWNER/OPERATOR 身份，而非仅相信客户端声明。
网页操作经过会话、MFA、CSRF；新开仓额外要求十分钟内验证过当前密码。平仓和撤单不增加近期密码门槛。
MCP 不传网页密码或 MFA，使用显式授权令牌；变更工具必须 `confirm_paper=true`。
不要把这个参数解释成客户端一定会弹确认窗：模型／客户端仍须遵循用户授权范围。

工具声明遵循 [OpenAI MCP 服务文档](https://developers.openai.com/plugins/build/mcp-server) 与
[OAuth 文档](https://developers.openai.com/plugins/build/auth)：逐工具 `securitySchemes`、缺 scope 的认证挑战，
交易工具明确 `readOnlyHint=false`、`destructiveHint=true`、幂等性和外部公开行情访问声明。
旧研究工具仍然只读，系统没有通过这些新接口发送实盘或 TESTNET 委托的能力。

## 工具合同

| 工具 | 权限 | 内容 |
| --- | --- | --- |
| `paper_account_list` | paper:read | 本人网页已有模拟账户及 ID，不自动建账户 |
| `paper_account_snapshot` | paper:read | 现金、可用余额、保证金占用、挂单冻结、风险、标记时间与浮盈亏 |
| `paper_order_list` / `paper_order_get` | paper:read | 开平仓委托、成交进度、原因、原始事件与成交证据 |
| `paper_order_chart` | paper:read | 根据订单冻结的决策快照返回真实 PNG；不读取最新 K 线，不重新识别形态 |
| `paper_position_list` | paper:read | OPEN/CLOSED 仓位、开平仓时间、均价、张数、保护触发状态 |
| `paper_fill_list` | paper:read | 每个价位的不可变开／平成交、数量、费用、毛盈亏 |
| `paper_order_preview` | paper:read | 新鲜公开盘口、合约单位与精度、预计可成交量；不预占资金、不落委托 |
| `paper_order_submit` | paper:read + paper:trade | 提交开仓委托，必须确认 PAPER、提供唯一幂等键及止盈止损 |
| `paper_order_cancel` | paper:read + paper:trade | 撤销未成交余量；不撤销已经发生的成交、不自动平仓 |
| `paper_position_close` | paper:read + paper:trade | 指定持仓减仓，支持部分／全量；不能反手或超平 |

列表按账户过滤，最多 100 条／页，offset 最大 10000，返回 `has_more/next_offset`。
`paper_order_get` 保留订单顶层字段，并返回有界 fills/events 和显式截断标记，不能将截断列表当完整历史。
币种用 `market_search_instruments` 解析的 `instrument_id`，每笔开仓指定 `timeframe`，不在 MCP 管理页面绑定唯一币种。
仅接受已启用的 Gate USDT 线性合约；其他币本位、现货或交易所不混用资金／数量单位。

开仓关键字段：`account_id, instrument_id, side(LONG/SHORT), contracts, timeframe,
stop_price, take_profit_price, order_type, time_in_force, limit_price,
max_slippage_bps, ttl_seconds, idempotency_key, confirm_paper`。
价格用十进制字符串；contracts 是整数**合约张数**，不是币数、USDT 或杠杆倍数。
基础币数量 = 张数 × 合约 multiplier；名义价值 = 成交价 × 基础币数量。

### 决策证据（迁移 0048）

开仓可附带 `decision_context`；未附带的旧／手工订单明确返回 `decision_status=UNASSOCIATED`，不自动归因于策略。

```json
{
  "observed_at": "2026-09-14T08:00:00Z",
  "rationale": "记录当时的决策摘要，不包含密码、API Key 或模型内部思考过程",
  "analysis_model": "user-selected-model",
  "pattern_events": [{"context_id": "exact-context", "candidate_id": "exact-candidate", "sequence": 1}],
  "coinglass_claims": [{"metric": "open_interest", "value": "12345.67", "unit": "USD", "source_at": "2026-09-14T07:59:00Z", "market_scope": "SOL aggregate futures"}]
}
```

分析时间必须带时区、位于过去七天内且不晚于当前时刻；摘要最多 2000 字符、模型标记 80 字符、精确形态引用最多 5 个、Coinglass 数值摘要最多 12 个。
没有通用任意 JSON、URL 或凭据字段。Coinglass 摘要与分析结论始终标记 `UNVERIFIED_CLIENT_REPORTED_NOT_SERVER_FINDINGS`，不能伪称为服务器核验的上游事实。

服务器在订单同一事务内冻结最多 256 根当时已收盘且 `available_at/created_at/close_time <= observed_at` 的 K 线，保留 Decimal OHLCV、修订、哈希及时间；晚到的历史回填和后来修订不能出现在原始依据中。
冻结 EMA9/EMA21、RSI14 及各根时间，使用现有 SMA 初始化 EMA、Wilder RSI 实现；缺失 K 线重置预热，不跨缺口插值，不冒充完整无限历史的指标值。
形态按原始 `context_id/candidate_id/sequence` 精确关联，检查币种周期、`observed_time/recorded_at` 及记录内部快照时间，解压并验证档案后保存原始规则、预测和画线，而不是读取当前候选补写过去。

每笔决策内容上限 256 KiB，超限拒绝且不截断。一个开仓只保存一次快照；部分成交、后续平仓与保护退出仅引用同一 `decision_snapshot_id`，不按盘口档位重复存整份分析。
快照有 owner RLS、内容摘要、不可变触发器；订单、快照及成交通知原子提交。证据上下文参加幂等哈希，改理由后复用旧键也会冲突；缺省／null 上下文不改变旧订单哈希。
快照时间表示服务器对指定历史边界的可用证据冻结，不证明客户端实际看过其中所有数据，也不证明盈利能力。

`paper_order_get` 返回 `decision_snapshot={id,evidence_digest,evidence_json}`。`paper_order_chart(order_id,pattern_index=0)` 返回原始图像和 ID/摘要；没有原始 K 线／未关联快照会返回安全错误，不用今天的行情重画原图。
旧策略订单不批量补造分析。当前缺失缓存可保存分析摘要，但明确 `NO_CACHED_CANDLES`；图表不可用不应伪造 K 线。
当前不开放最小委托量大于一张的合约新开仓，避免未建模的减仓尾数例外。

`MARKET` 仅 `IOC`；`LIMIT` 支持 `IOC/GTC/FOK`。价格须符合 tick；滑点限制 0–100 bps，
挂单最长 24 小时。close 省略 contracts 表示**锁内计算**当前未被其他平仓委托占用的剩余量。

## 真实度与账务

完整数学与数据来源见 [撮合模型](47_PAPER_MATCHING_MODEL.md)，合约字段以
[Gate 官方 API 文档](https://www.gate.com/docs/developers/apiv4/en/) 及当次公开 metadata 为依据。

买方逐档吃卖盘、卖方逐档吃买盘，单档只使用可见数量的 10%。盘口时间、深度、合约规格、费用与原始级别证据被冻结。
同一 `quote_id + side + price` 的模拟流动性预算由全账户共享，不能靠重复请求重复吃同一份盘口。
限价只有对手盘可成交时才成交，不用 K 线碰价代替排队成交，不编造 maker 排名或返佣。
FOK 不够量不部分成交；IOC 余量撤销；GTC 保留余量和相应资金预占。

采用 **1 倍全额抵押模型**，只读取公开接口；费用取公开 taker 与模拟账户配置中的较高者。
每个撮合批次在同一数据库事务内提交：订单、逐档成交、持仓、现金变动、不可变事件与通知 outbox。
账户行锁保护 Web/MCP/旧提案共用预算；开仓撮合前再次检查实际成交成本，不能双花、无限负余额开仓或并发超平。
新订单的开仓费即时扣现金；旧模拟交易开仓费在可用余额中预占，保持其原账本结算语义，避免重复扣费。
新生成的旧路径提案采用 `paper-account-v2` 尺寸规则，为允许的漂移和滑点预留名义额度；旧已冻结提案不重写，超过当前共享预算则明确拒绝。

现金 = 初始资金 + 已结算净变动；可用余额还要减去现有持仓抵押、挂单保证金、待支付手续费及旧模拟未结费用。
新 position/fill 的 `realized_pnl` 是**已实现毛盈亏**，`fees`/`fee` 单列；完全平仓后的策略净结果须扣完整开平仓费用。
浮盈亏来自后台采集的可退出盘口并附带时钟；超过有效期或混有未标记的旧仓位时，返回 null/未知，不显示虚构的零或“实时净值”。

同一个交易意图生成一个 16–128 字符幂等键。重复键与完整规范化请求哈希一致才重放；相同键改数量／账户／方向会拒绝。
发生超时先查询订单，再用**相同键与相同参数**重试，不换新键“补单”。网页暂存当前用户未确认请求，刷新后不自动提交。

## 止盈止损与后台

后台 `gatescope-paper` 持续处理，不依赖网页是否打开。每轮先处理持仓保护，再检查普通挂单；取数失败仍处理到期释放。
止损／止盈使用可退出方向盘口，触发后持久保存原因与观测时间。退出尝试失败使用独立保存点保留触发事实，反弹也不能抹除。
保护单抢占冲突的剩余开仓／平仓挂单，市价单数量上限分批退出；不超过当前持仓。
盘口过期、断网、限流时等待并显示原因，不用旧 K 线、未来价格或预定止损价补写成交。

这里不是交易所托管保护单：网络断开期间未观测到的瞬间穿价不能保证捕获；跳空与流动性不足可能让退出价格更差或暂时未完全退出。
还没有模拟资金费率支付、借贷、保证金阶梯、杠杆强平、ADL、撮合队列与市场冲击反馈。
因此不是“真实交易所等价账户”，也不能以这些模拟收益直接证明策略胜率或获利能力。

## Telegram 与留痕

在用户设置绑定有效 Telegram 通道后，新模型开仓、平仓以及止盈止损**实际产生模拟成交**时都会入队通知。
每个撮合批次合并通知成交量、均价、费用、毛盈亏、委托余量、原因和账户／订单／仓位 ID；逐价位明细仍全部保存。
通知只选订单所属用户的活动通道，发送前再次确认绑定；未成交挂单不冒充“开仓成功”。
Telegram 限流和失败走已有持久重试与状态记录；没有有效通道不能宣称收到。
网络导致 Telegram 已接收但回执丢失时仍可能重复通知，不承诺跨系统 exactly-once。
模拟撮合和账务事务不等待 Telegram，发送失败不回滚已发生的成交。

## ChatGPT 推荐工作顺序

示例用户指令：

> 仅操作我的 PAPER 模拟账户，不允许实盘。先列出账户让我指定，再读取可用余额与持仓；
> 获取 SOL_USDT 5m/15m/1h 图表和形态证据，说明不确定性；结合合约单位和账户风险预算做委托预估。
> 我确认参数后再提交，保留幂等键。提交后查询实际成交与剩余委托，后续用账户和持仓接口跟踪，不将受理当成成交。

模型输出建议不能跳过用户授权，也不能把形态置信度直接当作交易胜率。

## 发布／回退

先在隔离 `gatescope_test` 验证迁移、实际数据库角色/RLS、余额/并发/停止保护、OAuth 与通知，再备份代码和 DDL、升级到 0046。
重启 API/MCP/PAPER/notification，检查 HTTPS、工具声明、旧只读授权拒单和 PAPER 专用验收账户的开平仓。
验收记录永久保留、带显式名称；撤销本次验收专用客户端，不修改用户已有账户或授权。
有新委托或 REQUESTED 审计后禁止降级删除表；采用向前修复。旧代码回滚前须确保保护持仓仍由兼容 worker 管理，不能停掉保护后遗留模拟仓位。

## 2026-09-14 线上验收

已部署到现有服务器，MCP `1.3.0`、数据库 `0046`，HTTPS readiness 正常；API/MCP/PAPER/notification 持续运行，检查时均无自动重启。
专用「MCP模拟验收」账户通过真实 HTTPS MCP 完成 SOL 1 张开仓、同幂等键重放、全量平仓及现金核对：
2 笔委托、2 笔成交、1 条已平仓记录、0 持仓；Web 读取与 MCP 同库一致。两条 Telegram 通知均获得 SENT 回执。
此账户与交易事实保留；两次验收专用客户端分别撤销并确认 HTTP 401，未改已有连接、账户或仓位。

生产原三项只读 scope 被确认不能读取模拟账户、不能提交模拟订单，返回 `PAPER_SCOPE_REQUIRED`，没有创建交易事实。
原 `market_analysis_snapshot` 的 SOL 5m 仍返回 `market-analysis-2`、真实 1200×900 PNG 和 5 条形态。
真实浏览器通过 1440/390 宽度的三页签、详情、只打开不开单的表单测试；没有横向溢出或 JavaScript 错误。
这验证的是实际 MCP/HTTPS/Web 链路，没有调用 OpenAI 模型，也不声称已替用户在 ChatGPT 网页重新授权。

验证记录：后端完整测试 1732 通过、113 跳过（主要是未提供数据库的集成项）；另在隔离真 PostgreSQL 执行本次相关 41 项全部通过。
前端完整测试 274 项、类型检查、构建和最终浏览器验证通过。生产代码 36 个文件及当前 3 个前端资产与本地摘要一致。
结构和代码备份约 21MB，不包含全量行情备份，也未删除任何用户记录。验收时磁盘约 83% 使用、剩余约 12GB，需另行规划存储扩容。
