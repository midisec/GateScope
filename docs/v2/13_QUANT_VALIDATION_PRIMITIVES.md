# 量化验证与 LIVE eligibility 聚合实现

状态：`研究计算原语与 fail-closed 聚合已实现；完整训练/回测工件生产流水线未实现`  
更新时间：`2026-09-08`（Asia/Shanghai）

## 1. 已实现的计算边界

`src/gatescope/quant/validation.py` 提供无交易所访问权的纯计算组件：

- 事件唯一性：在预注册 dependency group 内，对半开标签区间精确积分 `1 / concurrency`。100 个完全共时、等长事件的 `N_unique=1`；100 个不重叠事件的 `N_unique=100`，不使用会高估共时样本的 Kish 公式；
- 残余设计效应：按固定 `global-mean-biased-acf-v1` 估计器计算文档第 8.1 节的 `D_n`，再且只再一次计算 `floor(N_unique / D_n)`；常数序列、非有限输入和无法估计的 lag 会 fail closed；
- 循环时间块 bootstrap：固定 block length、seed、重采样数、单侧 alpha 和方法版本，输出均值下界、以零假设中心化的一侧 p 值及输入摘要；
- Holm step-down FWER：调用方声明的 family size 必须与传入 trial 数完全一致，失败/弃用 trial 不能从家族中消失；
- EV 功效近似：使用训练段 sigma、单侧 `alpha_i`、功效和最小经济效应计算 iid block 下限；该值与 `N_ind` 比较，不重复乘设计效应；
- purged/embargo walk-forward：显式 train/calibration/test 窗口，跨边界标签进入 `purged_ids`，两个隔离间隙均不得短于最大持有期；外层至少五折、测试窗不得重叠，最终 sealed holdout 前也必须有足够 embargo。

`src/gatescope/quant/selection_bias.py` 按原论文实现两项选择偏差诊断：

- DSR：输入同一非年化频率下的 observed Sharpe 与全 trial Sharpe 方差、独立 trial 数、样本量、偏度和 Pearson 峰度；使用极值理论期望最大 Sharpe 作为 PSR 拒绝阈值。golden test 复现论文示例中 `N=88 → DSR≈0.910153`、`N=46 → DSR≈0.950502`；
- CSCV/PBO：要求同步、完整、无缺失的 `T×N` trial 日收益矩阵，分割为偶数个连续等宽 slice，枚举所有对称 IS/OOS 组合；PBO 是 IS 唯一最优 trial 在 OOS 的相对 rank logit 小于零的频率。缺 trial、行不齐、资源上限超限、Sharpe 不可计算或 IS 并列最优均 fail closed。

`src/gatescope/quant/live_eligibility.py` 只聚合已经由独立研究流水线计算并封存的统计工件。19 个门禁全部通过才输出 `CERTIFY / LIVE_ELIGIBLE`；任意失败输出 `ABSTAIN / PAPER`。认证摘要绑定源工件摘要、工件时点、策略版本、政策版本、全部门禁、假设与决策时点。

## 2. 持久化与权限

`PostgresStrategyCertificationRepository` 只能在 strategy workload 的数据库身份下使用。记录是幂等且 append-only 的；同一摘要若对应不同事实会拒绝。

数据库迁移 `0010_gate_testnet_execution` 进一步强制：

- 只有 `gatescope_strategy` 可写，API/executor 只能读；
- 认证工件不能来自批准时点之后，批准不能来自数据库记录创建之后；
- 有效期最多 90 天；
- gate report 必须恰好含有按固定顺序排列的 19 个 gate code；
- `CERTIFY` 必须没有失败门禁，`ABSTAIN` 必须至少有一个失败门禁，reason code 顺序与失败报告一致；
- 批准者必须仍是同租户的活跃 OWNER；
- UPDATE/DELETE 由授权和触发器双重禁止。

创建 Gate TESTNET drill 时，应用事务和数据库 admission trigger 都重新检查相同策略版本存在当前有效的 `CERTIFY / LIVE_ELIGIBLE / TESTNET` 认证。MFA、PAPER 盈利或手工修改数据库投影都不能替代它。

## 3. 尚未实现，因此不能放行

目前没有生产任务把行情、执行仿真和完整 trial registry 转换为 `LiveEligibilityArtifact`。下列布尔值/统计量不能由人工填写后视为已验证：

- nested walk-forward 的模型训练、fold-local 预处理与独立校准；
- block/cluster bootstrap 的联合收益分布及策略/事件 EV 下界；
- DSR 所需的完整 trial 元数据、相关性调整后独立 trial 数和含无交易日零收益的 UTC 日 NAV 序列；
- CSCV/PBO 所需的生产级完整对齐 `T × N` trial 矩阵、slice/持有期政策和 trial 完整性证明；
- sealed holdout 的一次性消费状态机；
- 成本模型、两倍联合成本压力、回撤、calibration、OOD 与数据质量的正式工件；
- 独立实现复算、golden fixture 和对象存储 content-addressed 验证。

因此生产组合根目前不会自动调用认证写入器，正常部署仍没有 TESTNET 订单可入队。后续实现顺序固定为：trial/holdout 不可变账本 → fold-local backtest/cost simulator → bootstrap/DSR/PBO/calibration 输入工件装配 → 独立复算与摘要验证 → OWNER recent-auth/MFA 批准 → 认证写入。任何一步缺失均保持 `PAPER`。

## 4. 测试口径

本地确定性测试覆盖完全重叠/不重叠事件、dependency group、不可估计相关序列、bootstrap 固定重放与版本摘要、Holm 完整家族、EV 功效公式、跨边界 purge、embargo、五折与 holdout 边界、DSR 论文数值示例、稳定/过拟合 CSCV 矩阵，以及 19 项聚合器的全部通过和关键失败路径。

当前开发机没有 PostgreSQL/Docker；迁移 SQL 已做 AST 解析，但 RLS、角色 grant、触发器与真实事务必须在预发布 PostgreSQL 再执行集成门禁。
