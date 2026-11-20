# OMS 与保护单不变量

更新时间：2026-09-08（Asia/Shanghai）

## 已实现

`src/gatescope_executor/oms.py` 保留交易所无关的确定性状态机；`gate_testnet.py`、`testnet_execution.py`、迁移 `0010` 和 TESTNET executor 已把这些不变量落实到受限 Gate TESTNET 执行切片：

- `OrderIntent.client_order_id` 由 intent/revision/kind 确定生成；网络超时进入 `UNKNOWN`，禁止直接再提交；
- 只有对同一 client order ID 完成 reconciliation 并确认 not-found 后，才回到可提交状态，而且仍复用原 ID；
- 同一 client order ID 若解析到两个 exchange order ID 会立即失败；
- fill 以 exchange fill ID 去重，累计量不得超过意图数量；
- 除 ENTRY 外的 exit/stop/take-profit command 全部强制 `reduce_only=true`；
- 任意部分 entry fill 都从经时钟修正后映射的 monotonic event time 启动保护倒计时，不从“晚收到事件”的时间重新计时；
- 只有 exchange 已确认 ACTIVE、trigger 合法、且具有 reduce-only/close-position 保证的 stop 才计入覆盖；
- 默认 5,000 ms hard deadline 到期仍有非 dust uncovered quantity 时，同时要求：撤销剩余 entry、提交独立 reduce-only exit、冻结新开仓；
- 退出成交不能超过当前净仓，防止降险命令反向开仓；保护超额也会触发取消/缩量与冻结。

测试覆盖 submit ACK 丢失、UNKNOWN 盲重试阻断、冲突 ACK、重复/超量 fill、部分成交保护 deadline、无效/超额 stop 和 reduce 反转。

完整实现与运行边界见 [12_GATE_TESTNET_EXECUTION.md](./12_GATE_TESTNET_EXECUTION.md)。

## 仍未开放

这个切片只证明本地 reducer 的不变量，不证明交易所接受具体参数，也不能消除交易所宕机、跳空或流动性消失风险。以下条件完成前 executor 对订单提交继续 fail closed：

1. 真实 PostgreSQL 与 Gate TESTNET 环境中的迁移、RLS、下单、partial fill 和保护单联调；
2. WebSocket 成交流、WS gap、长时间断网、交易所维护与重启故障注入；
3. 双向持仓模式、保护单修改/缩量和手工冻结解除向导；
4. TESTNET shadow/soak 的量化验收与完整 SLO 证据。

LIVE 仍不可启用。API 不能解密凭据或提交订单；只有显式启动、环境固定为 TESTNET 的隔离 executor 具有受限订单路径。
