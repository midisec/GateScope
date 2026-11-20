# PAPER 盘口成交模型（gate-depth-paper-1）

本模型只估算模拟交易，不能向交易所发单；不保证用户能在真实交易所获得相同成交。MCP 与 Web 必须共用用户原有 PAPER 账户、权限和账本，以下纯函数不自行创建账户、修改余额或发送通知。

## 输入与边界

仅 Gate USDT 线性永续，固定 1x 全额名义本金抵押，不模拟杠杆、强平、资金费结算、借贷或 VIP 手续费。做空仍可能因大幅上涨亏损超过初始抵押；仓储不得把“1x”解释成亏损自动封顶，也不得通过隐式新增余额掩盖缺口。

数量为正整数 `contracts`，不是币数。`base_quantity = contracts × quanto_multiplier`。保留交易所真实 Decimal 最小/最大张数、market_order_size_max、价格 tick 和公开 taker 费率。允许整数订单这一真实规则的子集，不把交易所最小 0.1 张偷偷改成 1 张，用户输入小数张数则明确拒绝。限价、止盈和止损价不符合 tick 时拒绝，不静默四舍五入。

v1 仓储不开放 `min_contracts > 1` 的新建仓，避免部分成交后形成缺乏已验证合法退出方式的小尾仓；明确返回不支持该最小数量，不能伪称已平仓。领域层 `remaining_order=True` 仅供已接受且经过最小数量验证的既有委托余量使用，跳过再次校验新委托最小数量，不跳过整数、最大张数、价格及新鲜度规则；不可暴露为用户绕过建仓限制的参数。

Provider 仅接受合约名称，无自定义域名和凭据参数，固定读取：

- `GET https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}`
- `GET https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={symbol}&limit=100&with_id=true&interval=0`

`X-Gate-Size-Decimal: 1` 是必须的兼容头，不是认证信息；遗漏会导致支持小数的合约数量被旧格式截断。已核对 [Gate 官方 API 更新记录](https://www.gate.com/docs/developers/apiv4/en/)；真实 SOL_USDT 检查中，不加该头 `order_size_min` 返回 0，加头后正确返回 0.1。

合约元数据 TTL 30 秒，最多缓存 128 个合约；过期失败不回退旧规格。盘口不缓存，最多两侧各 100 档。禁止代理环境继承、认证、Cookie 复用、重定向及交易请求；响应体最大 512 KiB。每个 `get` 的单调时钟预算 5 秒，socket 连接、读取各不超过 1 秒；429 最多一次不超过 0.25 秒的短等待，长 Retry-After 返回调用方调度，不无限重试。受 Requests/OS DNS 行为限制，预算并非操作系统级硬实时 SLA；预算超时或过期报价均拒绝成交。

## 时间、快照及有效性

`source_at` 是交易所 `current`（盘口响应生成时间），`updated_at` 是 `update`（盘口最后变化时间），`captured_at` 为收到完整响应的本地 UTC 时间。三者独立保存；不以本地时间覆盖旧交易所时间。执行时响应和本地获取时间均不得超过 5 秒，未来时钟误差最多 2 秒。未变化但刚被交易所观测的盘口可使用原有剩余预算，不能因重新请求自动补充深度。

`quote_id` 对合约、盘口序号、更新时间和规范化数值后的两侧深度做 SHA-256，排除本地时间和响应重新生成时间，避免同一未变化快照重复获得可成交数量。仓储必须在数据库事务中：

1. 对账户、订单、仓位及该快照的预算加锁。
2. 同一订单和快照仅处理一次；同一快照、方向、价位的全部订单共用已消费预算。
3. 原子写入成交、资金变化、仓位变化、订单剩余量及 TG outbox 事件。

拒绝交叉或锁定盘口、空侧、重复/乱序价位、非法时间、NaN/无穷、非正价格/数量、tick 不匹配或合约身份不一致。不会排序修补坏盘口后假装有效。

## 成交算法

BUY 消耗 ask，SELL 消耗 bid，按价格逐档扫单。每档最多使用 `floor(visible_contracts × 0.10)`，再减同快照同方向已消费张数；剩余不足一张不向上凑整。这是显式保守参与率，不代表真实队列优先权，也不能保证真实市场允许这些成交。

| 类型 | 成交行为 | 未成交余量 |
| --- | --- | --- |
| MARKET IOC | 按当前最佳对手价计算最大滑点边界，只取边界内深度 | 取消 |
| LIMIT IOC | 仅可穿越限价的当前对手深度 | 取消 |
| LIMIT FOK | 所有张数在限价和可用预算内才能一次成交 | 不满足则全部取消，零成交零手续费 |
| LIMIT GTC | 当前报价穿越时按 taker 估算；不凭 K 线触碰认定成交 | 保留等待下一有效盘口 |

实际逐档成交价计算 VWAP、基础币数量、USDT 名义金额、公开 taker 手续费；不使用 last/mark 替代盘口成交价，不假造 maker 返佣。订单已成交数量与剩余状态分开返回；IOC 部分成交为 `PARTIALLY_FILLED`，但 `remainder_cancelled=true`，不是仍在挂单。

`participation_rate` 是领域层受控模型参数，默认 0.10；对外下单接口不应让用户或模型自行更改。每笔保存 `model_version`、实际参与率、原始快照标识、逐档 fill 和费用，以供复查。

## 保护与断网

LONG 要求止损低于入场参考、止盈高于参考；SHORT 反向。触发观察 LONG 的 bid / SHORT 的 ask，符合实际退出方向。SL/TP 触发价不是保证成交价：跳空后使用新鲜盘口实际可成交档位计算，不按旧止损价伪造成交。

触发原因必须锁存：已经触发止损但部分/全部未成交，反弹也不能自动撤销退出意图。仓储对未退出数量在后续有效新快照重试，保留失败/等待原因；断网期间不编造价位、K 线插值或强行成功通知。后台轮询不能证明断网或两次轮询之间未观测到的触发事件不存在，页面与 MCP 应明确此局限。

公开元数据中的 mark_price、funding_rate 只作信息展示，带 `metadata_as_of`、资金费率 ratio 单位和周期。不会据此宣称已经结算资金费，也不会以缓存 mark 进行即时成交。

## 对接契约

```python
from gatescope.adapters.gate_paper_market import GatePaperMarket, MarketSnapshot
from gatescope.domain.paper_matching import match_order, validate_protection, exit_trigger

snapshot = provider.get("SOL_USDT")  # MarketSnapshot(spec, book, mark_price, funding_rate, metadata_as_of, ...)
result = match_order(
    snapshot.spec, snapshot.book,
    side="BUY", contracts=1, order_type="MARKET", time_in_force="IOC",
    consumed={},  # 当前quote/BUY每档Decimal价格 -> 已消费整数contracts
)
```

`MatchResult` 含 status、requested_contracts、filled_contracts、remaining_contracts、fills、vwap、notional、fee、quote_id、reason、resting、remainder_cancelled、participation_rate、model_version。全部 DTO 的 `to_dict()` 返回 JSON 安全数据，Decimal 以字符串输出。

## 验证记录

- 2026-09-14：132 项领域/provider 单元测试通过；覆盖精确数量/费用、BUY/SELL、三种限价时效、滑点、共享深度、相同报价去重、小数规格、既有委托余量、跳空/锁存、旧盘口/异常数据拒绝、429/HTTP 错误/响应上限和凭据隔离。
- 2026-09-14 03:09:24 UTC：实际只读 SOL_USDT 元数据及两侧各 100 档 GET 成功，总耗时 0.223 秒。随后仅在内存运行纯函数，1 张模拟买入以 101.16 USDT、0.07587 USDT 公开费率费用成交；未发任何交易请求、未写账本。此示例是一次历史观测，不是当前报价或持续可用性承诺。
- 仓储、MCP、Web 和 TG 的端到端部署验收由主任务单独记录；本领域/provider 单测不等同整个平台已上线。

## 后台调度

`PaperOrderMonitor` 接入现有 `paper-worker`，只增加独立循环，不替换旧 signal PAPER / shadow 监控。每周期先完成所选目标的仓位保护，再处理普通待成交订单；保护失败的用户/账户本周期不继续开单，其他用户和旧监控仍继续。

0048 配套运行改进：正式长期运行时，盘口订单／保护、旧 PAPER、shadow 使用三个独立循环，各自持有最多 2 条连接的连接池（总上限 6），只有盘口循环拥有公开行情客户端。耗时 shadow 计算不能占用保护循环的连接池或阻止其下一次调度。
资源仅由所属循环关闭；关闭时发出统一停止信号、有界 join，systemd 原有 30 秒停止预算保持不变。`GATESCOPE_WORKER_ONCE=1` 仍顺序执行一次，供可重复诊断，不代表线上串行调度。

每个循环写独立 `paper-order-worker / paper-legacy-worker / shadow-worker` 业务心跳，包含本轮结果、耗时、公开计数及最后成功时间；失败或停止不覆盖最后成功时间。心跳写入失败不能阻止保护检查。
健康检查额外读取业务进展：盘口心跳超过 45 秒、旧 PAPER 超过 120 秒、shadow 超过 180 秒、开放仓位最老保护检查超过 60 秒均提示异常。上游限流／断网导致降级时只报告，不触发重启风暴。磁盘 85% 起提示容量风险，不自动删数据或重启交易服务。
这些是观测阈值，不是硬实时承诺或新增策略部署门槛；仍无法保证断网期间捕获未观测的瞬间触价。

数据库 due-target 函数同时扫描已持仓和活动订单，不能因买单已经全部成交就漏掉持仓保护。调度读取最多 100 个目标，默认每周期处理 12 个账户/币种组合，每个目标最多检查 3 个仓位、3 个订单；周期默认 8 秒软调度预算。数据库按 `last_checked_at` 排序，循环另外轮转已处理目标，避免失败目标始终占据第一位。各步骤始终显式传入 tenant、owner、account 身份。

同币种不同用户可复用同一公开报价，但每次使用前按当下 UTC 再检查 5 秒新鲜度，私人持仓与资金不可共享。同周期每币种最多获取两次报价；上游 429 保存 Retry-After 冷却，不在循环中 sleep 或向下一个用户重复冲击限流接口。无行情时给仓储传 `None`，保留旧价格及其时间并标注等待，不写入零值“现价”。

新增 24 项 monitor/worker 单测覆盖保护优先、多用户隔离、失败继续、同币种复用、过期重取、冷却、软预算、公平轮转，以及新模块抛错时旧监控照常执行。领域/provider/monitor 合计 156 项测试通过。

规格及时间字段核对：[Gate 官方 USDT 永续 API 文档](https://www.gate.com/docs/developers/apiv4/en/futures/)。本模型是保守模拟，不是 Gate 撮合引擎副本。
