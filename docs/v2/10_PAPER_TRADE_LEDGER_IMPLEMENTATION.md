# PAPER 提案、确认与不可变交易账本

更新时间：2026-09-07（Asia/Shanghai）

## 1. 已落地的纵向链路

本切片把 GOD 的账户无关结果接到了一个严格限定为 `PAPER` 的控制面：

```text
GodDecision
  → signal_evidence_snapshot + exact bar revision manifest + gates
  → server-side PAPER execution_decision
  → immutable proposal + status events/current projection
  → approval revalidation
  → PAPER trade + entry/protection facts
  → append-only bar observations
  → TP / SL / time-stop exit fact + PnL
```

迁移 `0008_paper_trade_ledger.py` 为 signal、bar manifest、gate、execution decision、proposal、approval revalidation、approval、trade、bar observation 和 trade event 建立事实表。事实表由 trigger 拒绝 `UPDATE/DELETE`，状态只在 `proposal_status_current` 与 `trade_state_current` 中作 CAS 投影。全部 tenant-private 表启用并强制 RLS。

Web 不能提交 side、quantity、entry、TP、SL 或 time stop：

- `POST /signals/{id}/execution-decisions` 只接受 PAPER account ID 和 client request ID；
- `POST /proposals` 只接受 execution decision ID 和 client request ID；
- `POST /proposals/{id}/approve` 只接受 proposal version，幂等键放在 header；
- 方向、障碍、数量、成本假设和摘要都从冻结的服务端事实复制。

普通形态告警不会自动变成 PAPER 证据。只有 strategy role 能调用 `record_god_signal`，且 entry reference 必须等于 decision time 前最后一根已终结、已可用 K 线的 close。`ABSTAIN/RESEARCH` 信号可解释、可画图，但不能生成 eligible proposal。

## 2. PAPER 成交和风险口径

默认模拟账户门禁为：

- 单笔压力损失预算 0.25% equity，用户最多只能收紧/配置到 0.50%；
- 单标的计划风险最多 0.50% equity；
- 组合全部 OPEN PAPER trade 计划风险最多 1.50% equity；
- 单笔名义最多 10% equity；
- 同一 account/instrument/side 最多一个 OPEN trade，数据库 partial unique index 兜底；
- 默认单边 fee 5 bps、market slippage 3 bps、entry drift 最大 50 bps；
- quantity 只能向下映射到固定 base quantum。

批准事务锁定 PAPER account 和 proposal current 行，然后重新读取最新 final bar，检查 freshness、entry drift、证据等级、账户状态、精确条款摘要、同方向 OPEN trade、单标的风险和组合风险。任一 gate 失败都会写 immutable revalidation + rejection；不会静默改价、缩放后继续成交，也不会创建 trade。

通过后以最新 final close 加不利滑点模拟 entry。后续 reducer 只接纳终结 K 线，每个 open time 首次观察后冻结具体 revision；后来的数据修订不能重写先前 PAPER 走向。同根 bar 同时触碰 TP/SL 时固定采用 `STOP_FIRST`。止损跳空按更差的 bar open 再加 market slippage，止盈跳空不计额外价格改善，time stop 以第 N 根已观察 bar close 加不利滑点成交。费用在开平两边计入净 PnL。

这是一套可复现的 OHLC PAPER 模拟，不是逐笔成交仿真，更不代表真实市场一定能在模拟价格成交。

## 3. 页面、TG 与 MCP

React 新增 `PAPER 交易` 页面，包含：

- 模拟账户及保守风险参数；
- 冻结信号、概率/EV 下界、样本块和摘要；
- 精确 proposal、版本、过期时间、风险预算与确认/取消；
- trade 状态、PnL、事件 timeline、冻结原图与追加走势的分界水位。

TG 绑定私聊现支持 `/signals`、`/trades`、`/trade paper_…`。单笔查询会幂等吸收新 final bars 后返回当前 PAPER 状态。命令 update 先持久化去重，principal 由 active Telegram connection + active user + active membership 解析；未绑定 chat 不能读取 tenant 数据。TG 当前仍拒绝 `/approve`、自由文本条款和任何 LIVE 指令。

MCP 新增四个只读工具：`signal_list`、`signal_explain`、`trade_get_status`、`trade_get_timeline`。输出保留 evidence digest、watermark、quality/assumption 和 `research_only` 说明；工具不会推进状态或创建 proposal。

`apps.paper_worker` 是独立、无网络/凭据能力的 PAPER 状态推进进程。它通过 security-definer locator 只发现有 OPEN PAPER trade 的 tenant，再在 tenant RLS context 中有界扫描交易；每笔 reconcile 持有 current row lock，重复 worker/重复 cycle 不会复制 bar observation 或 exit。专用 `gatescope_paper` 数据库角色只能读取公共 bars/PAPER facts，写 bar observation、trade event/current 和 outbox，不能读取凭据或访问交易所。

## 4. 仍未完成，不能误报

- 已有自动 PAPER reducer 与逐 trade 行锁/唯一约束，但尚无跨区域 leader lease/fencing；当前适合单集群并发 worker，不宣称多区域主动-主动；
- 尚无 Telegram 一次性 callback token 原子确认，因此 TG 确认被明确拒绝；
- 尚无 PNG chart worker、对象存储 object lock 和 TG 图片回传；当前页面使用冻结 JSON/bar revision 直接画图；
- 尚无 AUTO authorization policy、多币种目标优化和 proposal supersede；
- Gate TESTNET 凭据 admission 与只读连接探测已独立实现，但尚无私有订单 adapter；PAPER 表仍没有任何 API key/真实订单端口；
- PostgreSQL/Timescale 集成测试已写，但此工作区没有可用 PostgreSQL/Docker，测试仍会 skip，不能声称迁移已在真实数据库通过。

LIVE 继续 hard-disabled。下一阶段应完成 Telegram callback-token、chart artifact，以及 Gate TESTNET 私有订单/保护/对账 adapter。
