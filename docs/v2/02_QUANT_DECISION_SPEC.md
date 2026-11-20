# GOD 概率决策与量化验证规范

## 1. GOD 的职责边界

本项目把 GOD 定义为 **Gated Opportunity Decision**：一个可复现、可拒绝交易、受风险约束的决策核。

GOD 可以给出的最强结论是：

> 对一个冻结且预注册的策略版本，在已声明的部署总体、数据可用时间、成本模型和统计假设下，其样本外平均净收益的保守单侧下界为正；对某一笔候选单只能给出模型条件估计和鲁棒敏感性下界。

它不能证明未来必赚，也不能为单笔订单提供分布无关的条件覆盖保证。市场分布会漂移，交易所会失效，价格会跳空，流动性会消失。“不交易/等待”必须是正常且常见的输出。

### 1.1 必须区分的概念

```text
shape_confidence         形态几何与启发式规则的匹配度，非概率
raw_probability         模型未经校准的输出
calibrated_probability  在独立校准集修正后的结果概率
net_profit_probability  净收益大于 0 的模型概率，不等于 TP 概率
probability_lower_bound 概率估计的模型依赖单侧下界
expected_value_net      计入成本后的净期望点估计
event_ev_robust_lcb     单笔模型鲁棒敏感性下界，不宣称无条件覆盖
policy_value_lcb        冻结策略样本外平均净收益的单侧下界
decision                ABSTAIN / REJECT / ELIGIBLE
evidence_tier           RESEARCH / SHADOW / PAPER / LIVE_ELIGIBLE
environment             PAPER / TESTNET / LIVE
authorization_mode      NONE / MANUAL / AUTO
```

四个状态轴正交：GOD 在账户无关的 signal evidence 中固化模型证据和最多可达到的 `evidence_tier`，Risk/Sizing 服务结合该证据与账户运行时门禁产生最终 `decision`；部署决定 `environment`；审批/自动授权服务决定 `authorization_mode`。`MANUAL` 只表示由人批准精确草案，不能放宽 LIVE 的统计、数据、账户、组合、执行或熔断门禁。`WATCH` 是 UI/提案生命周期状态，不是量化结论。

当前 `opportunity_scanner.py:356-365` 的 `actionable` 和加权 `score` 只能迁移为 `research_candidate`/`research_score`。在新规范验收前，UI 不得称其为“盈利概率”，执行器也不得订阅它。

## 2. 单笔模型估计与策略级统计命题

### 2.1 统一经济单位和完整结果空间

令 $x_t\in\mathcal F_t$ 为决策信息，$a$ 为包含 entry、退出、保护和 time stop 的执行策略，$q$ 为按交易所合约/数量单位表达的目标规模，$N_0(q,x_t)$ 为按决策参考价折算到账户结算货币的目标入场名义价值。

所有 `cost`、`EV`、$\sigma_H$、障碍距离和 Kelly 收益统一为 **相对 $N_0$ 的十进制收益率**；`5 bp = 0.0005`。现金 PnL 另以结算货币保存。反向/quanto 合约不得套用线性现货公式，必须由版本化 `PnLAdapter` 计算。

完整终局结果为：

$$
J\in\{NO\_FILL,TP,SL,TIMEOUT,EXECUTION\_FAILURE\}
$$

部分成交不是终局类别；实际成交量 $Q_f\in[0,q]$、成交均价和退出成交进入该笔收益。`NO_FILL` 的交易 PnL 为 0；执行失败、未保护仓位的紧急降险损失必须进入 `EXECUTION_FAILURE`，不能从回测中删除。该类样本不足时，以版本化可靠性先验/故障注入结果给出概率上界和损失压力分布，纳入 $\mathcal U_t$，不得假定其概率为 0。

令 $Y^{net}(q,a)$ 为包含实际成交、手续费、资金费率和执行摩擦后的归一化净收益，定义：

$$
p_j(x_t,q,a)=P(J=j\mid x_t,q,a)
$$

$$
\mu_j(x_t,q,a)=E[Y^{net}\mid J=j,x_t,q,a]
$$

则全概率公式给出单笔模型点估计：

$$
EV(q,a\mid x_t)=\sum_jp_j(x_t,q,a)\mu_j(x_t,q,a)
$$

首版可以使用“两阶段模型”：先估计 `NO_FILL/positive fill`，再对正成交条件下的 `TP/SL/TIMEOUT` 建模；GOD 必须把两阶段与执行失败压力合成为完整 EV。不得把条件于成交的三分类概率直接当作订单总体结果概率。

### 2.2 单笔鲁棒值不等于单笔频率学保证

由训练、校准、联合成本不确定性和数据质量构造版本化联合集合 $\mathcal U_t$，且集合内概率必须位于 simplex。系统可以计算：

$$
EV^{event}_{robust}(q,a\mid x_t)=
\min_{\theta\in\mathcal U_t}
\sum_jp_j(x_t,q,a;\theta)\mu_j(x_t,q,a;\theta)
$$

这是单笔模型敏感性下界。除非另行声明并验证参数模型、适用域与同时覆盖构造，否则不得把它描述为“真实单笔条件 EV 以 $1-\alpha$ 概率高于该值”。边际概率区间、成本分位和收益区间也不得各自取 95% 后宣称联合覆盖 95%；必须由同一个联合 bootstrap/后验抽样或显式 Bonferroni alpha 分配构造。

### 2.3 LIVE 的策略级统计对象

所有 LIVE 新开仓（无论 MANUAL 或 AUTO）的主要统计对象是冻结策略 $\pi$ 在预注册部署总体 $\mathcal D$ 上的平均净收益：

$$
V(\pi,\mathcal D)=E_{(x,Y)\sim\mathcal D}[Y^{net}_{\pi}]
$$

Nested walk-forward 为每个外层测试事件只产生一次 point-in-time OOS 决策和成交仿真收益。将同一时间块内所有币种/事件整体保留，按 `block_scheme_version` 对这些外层 OOS 记录重采样；默认至少 10,000 次，随机种子、块宽、分层规则和 alpha 写入 `backtest_run`。一侧 percentile 下界为：

$$
V_{LCB}=Quantile_{\alpha_i}\{V^{*(1)},\ldots,V^{*(B)}\}
$$

默认研究 family 的单侧预算 $\alpha_{family}=0.05$。包含 $M$ 个登记假设时，功效规划和 $V_{LCB}$ 默认保守使用 $\alpha_i=\alpha_{family}/M$；最终显著性另报告 Holm 调整 p 值。新增试验会产生新的 family 版本并重算 $M$，不能保留旧的较宽界。块宽至少覆盖最大标签持有期，并只能用训练段依赖结构确定。该下界仍以时间稳定/弱依赖、仿真真实性和部署总体不变为前提；它支持“该冻结策略的历史样本外平均净期望下界为正”，不支持“下一笔必赚”。sealed holdout 不参与阈值选择或 bootstrap 调参，只作一次最终审计。

### 2.4 单笔经济安全边际

通过策略级证据后，每笔 LIVE 候选还必须满足：

$$
EV^{event}_{robust}(q,a\mid x_t)>m_{EV},\quad
m_{EV}=\max(0.0005,0.5C^{UCB}_{0.95},0.05\sigma_H)
$$

其中 $C^{UCB}_{0.95}$ 是该订单量**联合总执行成本 95% 预测分位的统计上界**，$\sigma_H$ 是未年化、与持有期一致的收益波动估计。LIVE 同时要求策略级 $V_{LCB}>0$。门槛值、alpha 和估计器均进入策略/统计方法版本和实验登记，只能通过新的完整样本外审批修改。

## 3. 当前统计框架必须先修的错误

### 3.1 前视偏差

`pattern_engine.py:166-182` 用中心窗口确认 pivot，某个 $i$ 时点的 pivot 至少要看到其后 `order` 根 K 线才知道；当前回测却在 `pattern_engine.py:883-916` 以事件 `end_time` 的收盘价作为入场。这会把尚不可知的信息放到过去。

修复方式：

- 每个 indicator/event 同时输出 `event_time` 和 `available_at`；
- pivot 的 `available_at = pivot_bar.close_time + order * timeframe`；
- 形态可用时间是所有输入和确认条件 `available_at` 的最大值；
- 仿真入场是决策完成后的首个可成交 bid/ask，不是形态 K 线收盘；
- 回测逐时推进或使用严格 point-in-time DAG，禁止在完整 DataFrame 上计算后再倒填可用时间；
- pivot 的 `order` 不得由完整样本长度动态改变，必须是策略版本参数。

### 3.2 选择偏差与非独立样本

当前系统在同一数据集内从 5/10/20/50 根里挑“最好”持有期，再用同一结果过门槛；多个重叠形态又被 Wilson 公式当作独立 Bernoulli 样本。修复要求：

- 止盈、止损、持有期、形态阈值和特征都在内层训练/验证选择；外层测试只评估冻结版本；
- 所有尝试写 `trial_registry`，不得只记录赢家；
- 按标签时间区间去重/加唯一性权重，并用时间块 bootstrap；
- 多币种同一市场冲击下的事件归入相同时间 fold；
- 统计门禁使用有效样本数而不是原始事件数。

### 3.3 未计执行成本

现有方向收益、MFE/MAE 不含费用、价差、冲击、延迟、资金费率和部分成交，不能用于实盘。旧统计保留为描述性研究，新的所有回测/决策必须复用同一个成交模拟和成本模型。

## 4. 时间与数据不变量

每条输入至少保存：

- `event_time`：市场事件发生时间；
- `source_time`：供应商声明时间；
- `available_at`：本系统首次真正可读时间；
- `ingested_at`：持久化时间；
- `revision`：供应商或本系统修订号；
- `payload_hash`：canonical 原始负载摘要。

在决策时刻 $t_d$，合法信息集为：

$$
\mathcal F_{t_d}=\{x_i:available\_at_i\le t_d\}
$$

硬规则：

- 收盘策略只使用 `is_final=true` 的 K 线；
- 第三方当前快照只能从接入日开始积累，不能倒填历史；
- 被修订的数据追加新 revision，旧交易仍引用原 revision；
- 训练期标准化、分位数、缺失填充和 regime 参数不能从未来拟合；
- Coinglass/LSR 的供应商 `signal` 也只是一项候选特征；在确认其 point-in-time 生成过程前不得作为自动门禁。

## 5. 标签与成交仿真

### 5.1 三重障碍

方向 $d\in\{-1,+1\}$。成交模拟从 `decision_at + measured_decision_latency` 后的首个可执行 bid/ask 开始，按 entry policy 的 TTL、盘口队列和订单量得到 $Q_f$ 与加权成交价 $P_e^{fill}$。TTL 内 $Q_f=0$ 标为 `NO_FILL`；部分成交只对实际数量设置保护并按目标名义 $N_0$ 归一化收益。

用决策时可得波动率设置障碍：

$$
u_t=k_u\sigma_{t,H},\qquad l_t=k_l\sigma_{t,H}
$$

对第 $k$ 根 bar 内可执行价格路径 $\mathcal P_{t,k}$ 定义：

$$
r^{best}_{t,k}=\max_{P\in\mathcal P_{t,k}}d\left(\frac{P}{P_e^{fill}}-1\right),\qquad
r^{worst}_{t,k}=\min_{P\in\mathcal P_{t,k}}d\left(\frac{P}{P_e^{fill}}-1\right)
$$

首次满足 $r^{best}_{t,k}\ge u_t$、$r^{worst}_{t,k}\le-l_t$ 或到达 $H$ 的时刻为 $\tau$。

- 首先触及上障碍：`TP`；
- 首先触及下障碍：`SL`；
- 到 $H$ 未触及：`TIMEOUT`，在 deadline 后首个可执行报价退出，而不是假定按 bar close 无滑点成交。

有逐笔/更细数据时按真实可执行路径判定。只有 OHLC 时使用方向正确的 high/low；同一根同时覆盖 TP 和 SL 一律保守判为 SL。数据粒度和 tie-break 是标签版本的一部分，不能用对策略有利的顺序。

### 5.2 净收益

$$
Y^{net}=
\frac{PnLAdapter(Q_f,P_e^{fill},P_x^{fill},d)
-fee_{entry}-fee_{exit}-funding}{N_0(q,x_t)}
$$

`funding` 是有符号现金流：支付为正成本、收到为负成本。若 $P_e^{fill}/P_x^{fill}$ 来自盘口与延迟模拟，spread、impact 和 latency adverse 已进入成交价，不能再次从 $Y^{net}$ 扣除。成本报告以同一订单在决策 mid 上的无摩擦基准与净成交结果之差定义 `total_implementation_cost`。

成本分布随订单量、订单类型和市场状态变化：

$$
C^{pred}_{0.95}(q,a,x)=
Quantile_{0.95}\left(C_{total}\mid q,a,x\right)
$$

`C^{UCB}_{0.95}` 再对该联合分位的估计误差取预注册单侧上界。禁止把各成本分量的边际 p95 相加后称为总成本 p95，除非使用联合样本直接估计，或对分量做了可证明的同时覆盖分配。

只有仿真证明 maker 单能按队列/盘口成交时才能使用 maker 费用；否则使用双边 taker。回测至少模拟部分成交、撤单、最小数量/价格刻度、触发价类型、entry TTL 和 time stop。仿真无法支持的交易所能力对应 `evidence_tier` 最多为 `PAPER`。

### 5.3 盈亏平衡概率

二元固定收益中，毛盈利 $G>0$、毛亏损幅度 $L>0$，且无论结果都支付相同成本 $C$ 时：

$$
p_{BE}=\frac{L+C}{G+L}
$$

若已将成本并入净赢幅 $G_{net}$ 和净亏幅 $L_{net}$：

$$
p_{BE}=\frac{L_{net}}{G_{net}+L_{net}}
$$

若赢/亏成本分别为 $C_G,C_L$，则应先用 $G_{net}=G-C_G$、$L_{net}=L+C_L$；不得代入共同 $C$ 的公式。固定“胜率 ≥55%”没有普适意义。低胜率高赔率可能正期望，高胜率低赔率也可能负期望。

`p_TP` 是触及上障碍概率，`p_net_profit=P(Y^{net}>0)` 才是净盈利概率。含 `NO_FILL/TIMEOUT/EXECUTION_FAILURE` 或类内可变收益时不存在唯一标量 $p_{BE}$，主门禁必须使用完整 EV。只有预注册的二元固定收益解释器可以附加：

$$
p^{LCB}_{TP}\ge p^{UCB}_{BE}+\delta_p,\qquad \delta_p=0.03
$$

其中两侧界必须来自同一个联合不确定性构造；否则该项状态为 `NOT_APPLICABLE`，不能以 0 填充。

## 6. 特征体系

### 6.1 技术/市场特征

- 现有形态、pivot、结构比例、趋势线、ATR、突破状态；
- 多尺度对数收益、gap、实体/影线比例；
- ATR/价格、实现/下行波动、波动期限结构；
- EMA 距离/斜率、MACD/ATR、RSI、ADX/DMI；
- Bollinger 带宽/位置、Stochastic；
- 成交量 robust z-score、OBV、CVD（有数据时）、量价背离；
- bid/ask、盘口深度、成交量、预期冲击与成交概率；
- BTC/ETH 大盘方向、截面强弱、滚动/尾部相关性；
- 趋势/震荡、正常/高波动、正常/低流动性 regime。

稳健标准化只拟合过去窗口，例如：

$$
z_t=clip\left(
\frac{x_t-median(x_{<t})}
{1.4826MAD(x_{<t})+\epsilon},-5,5
\right)
$$

若使用 HMM/regime 模型，只能用实时过滤概率，不能使用包含未来观测的平滑状态。

### 6.2 Coinglass/LSR 特征

适配器以实际 schema 中存在的字段为准，候选因子包括：

- OI 水平、`Δlog(1+OI)`、OI/市值；
- 资金费率水平、变化、历史分位；
- 全市场/账户/持仓/顶级交易员多空比；
- long/short notional、交易员/whale 数量及平均入场；
- 主动买卖量和爆仓不平衡（套餐实际提供时）；
- 合约基差（实际提供时）；
- 顶级交易员与全市场 LSR 分歧；
- 价格 × OI、资金 × LSR、爆仓 × 波动等预注册交互项。

对候选方向 (d)：

$$
z_d=[d\cdot orderflow, -d\cdot funding_z,
-d\cdot log(LSR)_z,
d\cdot(LSR_{top}-LSR_{global})_z,
(d\cdot return_z)OI_z,\ldots]
$$

这些分别代表“顺向订单流、反拥挤、头部与大众分歧、价格/OI 确认”等待验证假设，不是预设真理。参数只能在训练集学习并正则化。

每项特征声明 TTL：

- 关键特征过期/缺失：该模型 `ABSTAIN`；
- 可选特征过期：只能切换到事先单独训练、校准、验收的降级模型；
- 禁止临时补 0 或无限 forward-fill 后继续自动交易。

## 7. 模型、校准与适用域

### 7.1 首版模型

首版 champion 使用低方差、可解释的两阶段 Elastic-Net/分层逻辑模型：entry fill 模型预测 `NO_FILL/positive fill`，正成交条件下的结果模型预测 `TP/SL/TIMEOUT`。令 $z=(x,q,a)$ 包含当时信息、候选数量和冻结执行策略，结果模型为：

$$
\eta_{j,g}(z)=\beta_{0,j,g}+\beta_{j,g}^{T}z,
\qquad
P(J=j\mid Q_f>0,z,g)=\frac{e^{\eta_{j,g}(z)}}
{\sum_{k}e^{\eta_{k,g}(z)}}
$$

其中 $j,k\in\{TP,SL,TIMEOUT\}$，固定 `TIMEOUT` 为参考类（对应系数为 0）保证可识别。$g$ 表示币种/形态/周期组，组参数向全局参数分层收缩，避免小样本单独拟合极端概率。执行失败概率/损失在积累到预注册最低样本前使用保守压力上界。LightGBM/树模型可做 shadow challenger，但不能仅凭样本内分数替代 champion。

模型注册键至少包含：

```text
universe + venue/market + timeframe + horizon
+ TP/SL definition + fee tier + feature schema
+ entry/exit policy + supported quantity domain
+ execution/cost model + training cutoff
```

不同 TP/SL、执行模型或费用等级定义的是不同标签，不能共用概率。

### 7.2 校准

- 内层训练/验证、独立校准、外层测试和 sealed holdout 严格分离；所有预处理和分箱边界也只在相应训练/校准段拟合；
- binary fill 模型可用 Platt scaling；三分类结果模型首选单一 temperature scaling。one-vs-rest Platt/isotonic 只有在归一化方式单独通过校准验收后可用；
- 校准器独立版本化，不得在最终测试或实盘结果上偷偷重校准旧版本；
- multiclass Brier 固定为 $n^{-1}\sum_i\sum_j(p_{ij}-1\{y_i=j\})^2$；log loss 使用同一 clipping epsilon；
- 每个关键类别使用校准集确定并冻结的 10 个等频 bin 边界，在外层 OOS 上计算 classwise ECE 与最大分箱误差；空 bin 报 `NOT_EVALUABLE`，不得跳过；
- calibration intercept/slope 使用外层 OOS 的 one-vs-rest logistic recalibration，概率按版本化 epsilon 截断；报告每类估计值和 block-bootstrap 区间；
- LIVE 开仓概率区域中，每个实际使用的关键 bin 必须满足第 8 节定义的 `N_ind ≥ 100`。

首版 LIVE 证据门禁：

- 相对 Brier skill score $1-Brier_{model}/Brier_{ref}\ge0.02$；`ref` 是在该外层 fold 训练段冻结的类别基准率，不能从测试段估计；
- 所有用于开仓的关键类别 classwise ECE ≤0.03，最大分箱误差 ≤0.08；
- 所有用于开仓的关键类别校准截距绝对值 ≤0.10，斜率处于 `[0.8,1.2]`；
- 二元固定收益解释器另执行第 5.3 节的联合 $p^{LCB}_{TP}/p^{UCB}_{BE}$ 门禁；其他情形为 `NOT_APPLICABLE`。

这些阈值、bin 定义、epsilon 和 bootstrap 方法必须按策略在实验登记中预注册。样本不足或任一关键 bin 不可评估时，`evidence_tier` 最多为 `PAPER`；人工确认不能把它提升为 LIVE。

### 7.3 OOD/漂移

保存训练分布、特征缺失率、regime 覆盖和 Mahalanobis/密度分数。首版使用训练段拟合的稳健多变量距离，阈值冻结为训练段 99% 分位；另为有物理/业务含义的关键字段设置硬范围。超过任一硬范围或多变量阈值时该事件 `ABSTAIN`。不得对每个普通特征分别套 99% 分位后仍声称总体 1% 拒绝率；方法版本必须记录在独立 OOS 上的实际拒绝率和覆盖区域。

## 8. 有效样本与验证

### 8.1 事件唯一性

重叠标签区间 $I_i$ 的权重：

$$
u_i=mean_{t\in I_i}\frac{1}{concurrency_t},\qquad
N_{unique}=\sum_i u_i
$$

`concurrency_t` 必须把同一相关币种簇、相同市场冲击窗口内的重叠标签一起计数。不能对事件权重直接使用 Kish 公式并称为独立样本数：若 100 个完全共时事件均有 $u_i=0.01$，Kish 公式仍会错误返回 100，而 $N_{unique}=1$。

对按时间排序、唯一性加权后的 OOS 得分/净收益序列，以训练段预注册的截断 $K$ 估计残余设计效应：

$$
D_n=\max\left(1,1+2\sum_{k=1}^{K}\left(1-\frac{k}{n}\right)\rho_k\right),
\qquad
N_{ind}=\left\lfloor\frac{N_{unique}}{D_n}\right\rfloor
$$

$\rho_k$ 的序列、估计器、$K$ 和缺失处理均属于 `block_scheme_version`；无法稳定估计时不得进入 `LIVE_ELIGIBLE`。`N_ind` 只是功效/最小样本守门的保守代理，正式区间仍使用第 2.3 节的联合 block bootstrap。每个概率 bin 也按同一方法重算，不能使用全局样本数。

Wilson 区间只可用于明确标注“未校正相关性的原始胜率描述”，不能进入 LIVE 门禁。

### 8.2 Nested walk-forward

- 外层至少 5 个按时间推进的测试窗口；
- 每折建议训练 ≥12 个月、校准 ≥3 个月、测试 ≥3 个月；若数据不足则不允许自动化；
- purge 所有标签区间跨越边界的样本；
- embargo 至少为最大持有期 $H_{max}$；
- 标准化、缺失处理、特征选择、regime、币种相关簇和 OOD 阈值全部只在该 fold 训练段拟合并冻结；
- 最后保留最近至少 3 个月 sealed holdout，模型/阈值选择过程中不可查看；每个策略家族只允许打开一次；
- 一旦查看 sealed holdout 后修改任何特征、阈值、barrier、模型、校准或执行假设，该 holdout 永久标为 `CONSUMED`，新版本必须等待新数据或使用此前从未访问的独立 holdout；
- 同时刻、相关币种簇的样本放同一 fold，且同一事件不能通过不同周期副本跨 fold；
- 每次参数、特征、模型和规则尝试写入 `trial_registry`。

### 8.3 功效和多重检验

仅在第 5.3 节二元固定收益解释器中，胜率近似所需 iid 样本为：

$$
n_{iid}\ge
\left[
\frac{z_{1-\alpha_i}\sqrt{p_0(1-p_0)}+
z_{1-\beta}\sqrt{p_1(1-p_1)}}{p_1-p_0}
\right]^2
$$

其中 $p_0=p^{UCB}_{BE}$，$p_1=p_0+\delta_p$，$\alpha_i$ 使用第 2.3 节分配，功效 $1-\beta=80\%$。在未做多重性收紧的演示值 $\alpha_i=0.05,p_0=0.50,\delta_p=0.03$ 下约需 1,716 个 iid 样本；正式所需量通常更大。当前 `min_samples=12` 远不足以支持“可靠盈利概率”。

主策略的功效针对净收益最小经济效应 $\delta_{EV}>0$。初始近似为：

$$
n^{EV}_{iid}\ge
\left[\frac{(z_{1-\alpha_i}+z_{1-\beta})\sigma_Y}{\delta_{EV}}\right]^2
$$

$\sigma_Y$ 只从训练段净收益估计；最终所需样本必须用预注册的 block/cluster 仿真复核。有效样本门禁比较的是 $N_{ind}$ 与 iid 要求，不得再把右侧乘一次 $D_n$。等价地，也可比较 $N_{unique}$ 与 $D_n n_{iid}$，两种口径只能选一种并写入方法版本。

默认对同一研究 family 内所有 universe、特征、barrier、持有期、阈值和模型试验，对“$H_0:V\le0$”的一侧 block-bootstrap p 值做 Holm FWER 校正；family 定义、失败试验和人工看图后提出的变体都写 `trial_registry`。DSR 不能替代该登记。

`LIVE_ELIGIBLE` 最低证据：

- `N_ind ≥ max(300, n^{EV}_{iid})`；仅二元解释器再要求 `N_ind ≥ n_{iid}`；
- Holm 调整后单侧 `p_adj ≤ 0.05`；
- 外层 OOS 联合时间块 bootstrap `policy_value_lcb > 0`；
- Deflated Sharpe Ratio `≥0.95`；
- CSCV 估计 Probability of Backtest Overfitting `<0.10`；
- 基础成本下通过策略级和单笔 EV 门禁，2 倍**联合总实现成本**下策略级 OOS EV 点估计与事件点估计仍大于 0；
- 定义 $P_g$ 为各 regime 的 OOS 净 PnL，正贡献占比 $s_g=\max(P_g,0)/\sum_h\max(P_h,0)$；分母必须大于 0、$\max_g s_g\le0.70$，且每个允许 AUTO 的 regime 达到预注册最低 $N_{ind}$；
- leave-one-regime-out 的 OOS EV 点估计均大于 0；若策略明确只服务某一 regime，则部署总体只能限定该 regime，其他 regime 运行时 `ABSTAIN`，不能使用贡献占比门禁伪装全 regime 稳定；
- sealed holdout 上策略级 EV、最大回撤、校准、成本压力和数据/OOD 门禁全部通过；结果只允许记录一次，不得反向调参。

DSR 使用扣除全部成本、包含无交易日 0 收益、按 UTC 日聚合的策略 NAV 收益；年化因子首版固定为 365，基准收益、样本长度、偏度、峰度、trial Sharpe 方差和相关性调整后的独立 trial 数全部入 artifact。`DSR≥0.95` 中的 DSR 是概率统计量，不是 Sharpe 值。

CSCV 使用同一时间索引上完整对齐的 $T\times N$ 策略日收益矩阵，列包含研究 family 中所有合理的最终配置；时间切片保持连续，切片宽度不得小于 $H_{max}$，同一配置在每个切片使用相同成本与风险规则。引导式优化只登记每次独立搜索的最终候选，不把中间迭代伪装为独立策略。PBO 是选择过程过拟合诊断，不证明回测正确或未来盈利。

方法参考：[Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) 与 [The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)。实现时固定论文公式、收益频率、trial 口径、切片方法和公开/自建测试 fixture，不从论文标题臆测实现。

## 9. 仓位与组合风险

### 9.1 分数 Kelly

二元、固定净赔率且观测独立时，财富变化为赢时 $1+fG$、输时 $1-fL$，完全 Kelly 的解析式为：

$$
f_K=\frac{pG-(1-p)L}{GL}
$$

该式只作 binary golden fixture；只要存在 `NO_FILL/TIMEOUT/EXECUTION_FAILURE`、部分成交、类内可变收益或规模相关成本，就必须使用完整净收益分布。对每个候选订单量 $q$：

$$
g(q)=\min_{\theta\in\mathcal U_t}
E_{\theta}\left[
\log\left(1+\frac{N_0(q,x_t)}{E}Y^{net}(q,a)\right)
\right]
$$

其中 $E$ 是当前净值；每个联合压力情景都必须满足财富乘数严格大于 0，否则该 $q$ 不可行。禁止把分别估计的 $p^{LCB},G^{LCB},L^{UCB},C^{UCB}$ 当作具有同一置信度的联合情景。

首版只部署鲁棒 0.1 Kelly。实现采用版本化有限 `quantity_grid`，避免成本、成交率、EV 和规模循环引用：

1. 从交易所最小量至绝对硬上限，按预注册名义比例 `{0, 0.05, ..., 1.00}`、风险/保证金断点和 lot step 向安全方向取整，去重得到有限集合 $\mathcal Q$；
2. 对每个 $q\in\mathcal Q$ 重新计算成交概率、联合成本、$EV^{event}_{robust}$、压力损失、保证金和组合占用；
3. 在只受绝对硬上限的可行集合上求 $q_K=\arg\max g(q)$，相同目标值选择较小量；若 $g(q_K)\le0$，返回 0；
4. 将 `0.10 × N_0(q_K)` 向下映射回网格，重新执行全部门禁；在不超过该名义值的通过项中选择 $g(q)$ 最大者，相同则选较小量；
5. 任一输入、价格容差或 rounding 改变都从第 2 步重算，不能沿用旧数量。

约定 $N_0(0)=0,g(0)=0$，不对零名义调用收益归一化公式。

对每个候选量还必须满足：

$$
N_0(q)\ell_{stress}(q)\le r_{trade}E,
\quad N_0(q)\le N_{liquidity},
\quad N_0(q)\le N_{margin}
$$

$\ell_{stress}$ 是包含止损滑点、gap、费用、资金费率和紧急降险的名义损失率上界。Kelly 只能缩小仓位，永远不能突破单笔、组合、流动性、杠杆或保证金硬上限。

首版保守默认：

- 自动单单笔压力损失预算 ≤净值 0.25%；任何 LIVE 授权模式硬上限 0.50%。这是模型压力上界，不是跳空时的成交价格保证；
- 全部持仓计划止损风险 ≤1.50%；单币 ≤0.50%；
- 默认总杠杆 ≤1x，硬上限 2x；
- 预计冲击成本 ≤止损距离的 20%；
- 强平价与计划止损的缓冲距离至少为止损距离的 3 倍；
- 禁止马丁格尔；加仓是新决策并重新占用组合预算。

### 9.2 多币种组合

同币种多周期先合成一个净目标，禁止自己与自己对敲。按滚动相关性和尾部相关聚类，共享风险预算。

$$
\max_{w,t}\ \mu_L^Tw-
\frac{\gamma}{2}t-
\kappa\lVert w-w_{prev}\rVert_1
$$

约束：

$$
 t\ge w^T\Sigma_s w\quad\forall s\in\mathcal S,
\qquad
\sum_i|w_i|\le L_{max},\quad
|w_i|\le w_{asset},\quad
\sum_{i\in cluster}risk_i\le r_{cluster}
$$

$\mu_L$ 是对同时在候选集中的资产做多重性调整后的净收益下界。每个 $\Sigma_s$ 必须由版本化历史/压力联合收益情景构造并为 positive semidefinite；至少包含正常 shrinkage covariance，以及波动乘 1.5、相关性向单一市场因子收缩的压力场景。禁止逐元素取多个协方差矩阵的“较坏值”，因为结果可能非 PSD；优化器通过辅助变量 $t$ 对所有合法场景取最坏组合方差。

组合损失 $L^{port}_{1d}(w)$ 使用 UTC 日、现有仓位与候选订单的联合重估，包含基差、资金费率、流动性/gap 和强平压力。为正确处理离散/有原子分布，定义 Expected Shortfall 形式：

$$
CVaR_{0.99}(L)=\min_z\left[
z+\frac{1}{1-0.99}E[(L-z)_+]
\right]
$$

门禁使用对该量的 95% block-bootstrap 上界 `CVaR_0.99_UCB`，而不是样本点估计。风险模型必须预注册 `min_tail_effective_count`（首版默认 20），它只统计超过经验 VaR 的不同原始历史独立块，bootstrap 复制不增加该计数；低于该值时只能用更坏的明确压力损失上界，不能报告经验 99% CVaR 为已验证。默认单币名义 ≤净值 10%，相关簇名义 ≤25%，`CVaR_0.99_UCB≤净值2%`。历史尾部观测不足且无已批准替代压力界、场景矩阵非 PSD、优化器不收敛或风险结果不可复现时一律拒绝新仓。

## 10. 止盈、止损与持有期研究

首版预注册有限搜索空间，例如：

```text
SL = {1.0, 1.5, 2.0} × ATR
TP/SL = {1.25, 1.5, 2.0}
H = {5, 10, 20, 50} bars
```

只能在内层选择，外层测试和部署期冻结。规则：

- 止损只允许收紧，不得因“等反弹”放宽；
- 移动止损、保本和分批止盈各自是新策略，需单独验证；
- 到 $H$ 执行 time stop；
- 结构失效位与 ATR 位之间使用哪个、取近还是取远，必须由策略版本给出明确运算符并参与内层选择；禁止运行时用“更保守”这一歧义词临时挑选；
- 止损距离必须大于价差/费用/噪声下限；
- 回测、paper 和实盘共用相同 barrier/rounding 代码。

## 11. 可验证的策略假设

以下是进入实验登记的假设，不是直接上线规则：

### H1 形态突破 + 非拥挤趋势延续

确认型看涨/看跌形态 + 多周期趋势同向 + 成交量/订单流确认；OI 增长但 funding/LSR 未到同向拥挤极值。目标是筛掉弱突破与过度拥挤追价。

### H2 拥挤反转

funding、LSR、OI/市值位于滚动极端，价格出现反向结构确认且 OI 开始收缩。目标是捕捉拥挤平仓，不允许仅凭 LSR 极端逆势入场。

### H3 波动压缩后的清算/突破

实现波动和带宽压缩、OI 堆积，随后价格/成交量/清算方向共同确认。目标是识别状态转换，未突破前只观察。

### H4 头部与大众分歧

top trader/whale 与全市场 LSR 出现稳定分歧，同时价格结构支持头部方向。供应商定义、发布时间和可交易性必须先验证。

每个假设单独定义 universe、周期、barrier、成本、预期方向、消融实验和失败标准。组合只有在组合版自身通过验证后才能发布。

## 12. LIVE 开单资格与授权门禁

统计、校准和 sealed holdout 是策略版本级认证；数据、价格、账户、规模、组合和执行状态在每个事件运行时重算。GOD 只固化账户无关证据和 `evidence_tier`；Risk/Sizing 服务必须引用该证据并验证所有适用硬门禁，全部为 `PASS` 才能在 execution decision 中输出 `decision=ELIGIBLE`。之后控制面依据独立 policy 选择 `authorization_mode=MANUAL|AUTO`；GOD 本身不输出环境或授权。

| 类别 | 默认要求 |
| --- | --- |
| 数据 | 所有关键输入 `available_at≤decision_at`，K 线 closed，无关键缺失 |
| 时效 | 行情未超策略 TTL；Coinglass/LSR 未超两个声明发布周期 |
| 时钟 | 本机与交易所偏差 ≤500ms |
| 适用域 | 未超过训练分布 99% OOD 门槛 |
| 版本 | 模型、校准器、特征、成本和风险版本均 `APPROVED` |
| 统计 | 满足 `N_ind`、功效、`policy_value_lcb`、DSR、PBO、Holm 和 sealed holdout 门禁 |
| 概率 | 仅二元解释适用时 `p_TP_LCB≥p_BE_UCB+0.03`；否则 `NOT_APPLICABLE` |
| 期望 | `event_ev_robust_lcb>m_EV`，2 倍联合总成本压力下点估计仍为正 |
| 流动性 | 深度、价差、冲击、最小数量和预计成交率达标 |
| 组合 | 单笔/单币/相关簇/CVaR/杠杆均达标 |
| 执行 | 账户/订单/仓位已对账，支持所需保护能力 |
| 熔断 | 全局、租户、账户、策略、币种均未暂停 |

`ABSTAIN` 表示关键数据、适用域或估计能力不足，系统没有形成可比较的交易结论；`REJECT` 表示已完成计算但至少一个适用硬门禁失败。两者都不能创建 LIVE 开仓 intent。统计证据不足的候选最多进入 `PAPER`；人工 Web/TG 确认也不能把它提升为 LIVE。人工降低数量也必须生成新的 execution decision；修改 entry policy/价格容差必须重新计算 signal evidence 和 execution decision，修改 TP/SL 或 $H$ 还必须产生新策略/模型版本。

## 13. 漂移、降级和熔断

每个批准版本必须保存 `approval_epoch`、`approved_at`、`review_expires_at`（默认 90 天）、训练参考分箱和监控方法版本。`strategy_certification` 状态为 `ACTIVE|SUSPENDED|EXPIRED`，所有状态变化写追加事件；旧 signal evidence 的 `evidence_tier` 永不改写。运行时只有当前 certification 为 `ACTIVE` 才能创建 LIVE 新仓。

默认预测窗口为最近 500 次 eligible prediction，标签窗口为最近 500 个成熟结果；两者都报告实际日历跨度。成熟结果 `N_ind<200` 或预测数 <500 时状态为 `INSUFFICIENT`，只能报告趋势，不能声称新显著性。该状态本身不在审批有效期内暂停旧认证，但到 `review_expires_at` 时追加 `EXPIRED` 事件，不得续期 LIVE。

PSI 使用训练段冻结的 10 个等频边界；比例以 `epsilon=1e-6` 截断后重新归一化：

$$
PSI=\sum_b(a_b-e_b)\log(a_b/e_b)
$$

PSI 是漂移启发式而非盈利显著性检验。默认动作：

- 任一关键特征 PSI ≥0.10：警告，实际风险乘数上限降为 0.5；
- 任一关键特征 PSI ≥0.25，或超过 20% 的已注册特征达到 0.25：追加 certification `SUSPENDED` 事件并禁止所有 LIVE 新开仓；
- 成熟标签窗口满足 `N_ind≥200` 后，按第 7.2 节冻结分箱计算的 classwise ECE > `max(0.05, 2×验证期 ECE)`：追加 `SUSPENDED`；
- 两个预注册、互不重叠且各自 `N_ind≥100` 的成熟窗口 `policy_value_lcb≤0`：追加 `SUSPENDED`；
- 最近 100 笔实际 fill 的联合实现成本 p95 超对应模型预测 p95 的 1.5 倍：风险乘数上限降为 0.5；连续 20 笔中每笔实现成本均超过其预测 p95 的 2 倍：追加 `SUSPENDED`；
- 行情/关键外部数据 stale、时钟异常、订单/仓位失配、保护单未知：立即禁新仓；
- 以 UTC 00:00 净值为日基准，日内 mark-to-market 净值损失 2%：当日熔断；以周一 UTC 00:00 为周基准，周损失 4%：暂停并人工复核；
- 以该账户/策略上线后的 mark-to-market 净值高水位计算回撤：5% 时风险乘数上限降为 0.5，8% 时全局暂停。

连续亏损几笔不是策略失效的统计证明，但始终受资金硬限制。默认只在每月首个 UTC 日做一次正式统计 look；第 $k$ 次 look 使用 $\alpha_k=0.05/[k(k+1)]$，其总和不超过 0.05。每个 p 值仍必须由同一版本的有效 block 方法产生。新的策略版本/approval epoch 重新登记序贯方案；不能每天重复普通 p 值直到得到想要的结论。任何暂停后的恢复都要求数据恢复、完整对账、漂移复核和有权限人员重新批准；不得因窗口滚出坏样本自动恢复。

## 14. 两层决策快照与输出契约

### 14.1 账户无关的 signal evidence

`signal_evidence_snapshot` 由 GOD 生成并保持账户无关：保存 instrument/venue、point-in-time 输入、特征与模型版本、未条件化终局概率、支持的 order-policy/quantity domain、归一化收益/成本曲线、barrier 比率、策略级认证引用和 evidence digest。它不得保存账户净值、可用保证金、账户实际费率、组合仓位或最终下单数量。

持久化字段必须至少覆盖：`signal_evidence_snapshot_id/digest`、instrument 与 market snapshot revision、策略/模型/校准器/统计方法版本、`side`、`shape_confidence`、完整终局的 `raw/calibrated probabilities`、可支持时的 `net_profit_probability/model_lcb`、按 order-policy/quantity domain 参数化的归一化收益与执行分布、`policy_value_lcb`、不确定性方法及 `evidence_tier/strategy_certification_id`。这些字段只在 signal evidence 保存一次。

`raw` 和 `calibrated` 各自的终局概率必须在版本化精度内和为 1；舍入使用固定 Decimal 规则并把最后一类设为余数，禁止浮点误差产生负值。`net_profit_probability` 只有模型直接估计或能从同一联合收益分布积分时才有值，否则为 JSON `null`；不得用 TP 概率或 0 代填。

### 14.2 账户相关的 execution decision

Risk/Sizing 服务引用一份 signal evidence，再绑定不可变的账户、组合、交易所能力和 instrument metadata revision，按账户实际费率/保证金/持仓重新计算成本、EV、数量和所有运行时门禁，生成 `execution_decision_snapshot`。下列 JSON 是该**执行决策信封**，不是 GOD 核心的账户无关输出：

```json
{
  "schema_version": 3,
  "execution_decision_snapshot_id": "uuidv7",
  "signal_evidence_snapshot_id": "uuidv7",
  "signal_evidence_digest": "sha256",
  "decided_at": "UTC timestamp",
  "exchange_account_id": "uuid",
  "portfolio_snapshot_id": "uuidv7",
  "capability_revision_id": "uuid",
  "instrument_id": "uuid",
  "instrument_revision_id": "uuid",
  "decision": "ABSTAIN|REJECT|ELIGIBLE",
  "environment": "PAPER|TESTNET|LIVE",
  "authorization_mode": "NONE|MANUAL|AUTO",
  "economics": {
    "return_unit": "fraction_of_target_entry_notional",
    "target_entry_notional": {
      "amount": "decimal string",
      "currency": "asset_id"
    },
    "break_even_probability": null,
    "account_net_profit_probability": null,
    "account_net_profit_model_lcb": null,
    "cost_p50_pred": "decimal string",
    "cost_p95_pred": "decimal string",
    "cost_p95_ucb": "decimal string",
    "event_ev_net": "decimal string",
    "event_ev_robust_lcb": "decimal string",
    "stress_ev_2x_total_cost": "decimal string"
  },
  "plan": {
    "side": "BUY|SELL|NONE",
    "entry_type": "MARKET|LIMIT",
    "entry_reference": "decimal string",
    "price_unit": "quote_currency_per_base_unit",
    "stop": "decimal string",
    "take_profit": ["decimal string"],
    "time_stop_at": "UTC timestamp",
    "quantity": "decimal string",
    "quantity_unit": "base|contracts",
    "max_slippage_bps": "decimal string"
  },
  "gates": [{"code": "DATA_FRESH", "status": "PASS|FAIL|NOT_APPLICABLE|INSUFFICIENT", "detail": "safe text"}],
  "versions": {},
  "provenance": [],
  "reason_codes": [],
  "digest": "sha256"
}
```

以上 JSON 是 `execution_decision_snapshot` 的**持久化核心**。`shape_confidence`、终局概率、`policy_value_lcb`、校准/统计方法和 `evidence_tier` 不得复制进该记录；它们只能经 `signal_evidence_snapshot_id + signal_evidence_digest` 读取。兼容 API 可以返回 `execution_decision` 与只读的 `signal_evidence` 展开投影，但投影不写入 execution 表、不参与 execution digest；缓存投影必须同时校验 evidence ID 和 digest。

`account_net_profit_probability/model_lcb` 是绑定账户实际费用、数量和 order policy 后从 evidence 的同一联合分布重新积分出的执行期派生值，不是复制 signal 的账户无关概率；无法计算时必须为 JSON `null`，不得以 0 表示缺失。`break_even_probability` 只在第 5.3 节二元解释适用时有值，其余为 JSON `null`。

除计数/枚举外，概率、收益、成本、金额、价格和数量全部以 canonical decimal string 输出；禁止 JSON binary float 进入 digest。`quantity_unit/price_unit` 必须与 `instrument_revision_id` 引用的强类型定义一致，禁止仅靠 symbol 猜单位。时间统一 UTC 固定精度，字段顺序/Unicode/空值/Decimal 规范由 `canonical_json_version` 固定。解释文本只由 reason code 和版本化模板生成，不能让 LLM 自由修改数值、门禁或四个状态轴。

组合 API 的四轴来源固定：`decision` 来自 execution decision，`evidence_tier` 来自所引用的 signal evidence，`environment/authorization_mode` 来自 execution decision 中冻结的控制面上下文。它们、账户余额或用户确认都不能反向修改 evidence 中的模型概率。任何账户、组合、能力 revision、instrument revision、实际费率、价格容差或数量变化都必须产生新的 `execution_decision_snapshot_id` 和 digest，但继续引用未被修改的原 signal evidence。

## 15. 必须自动化的量化验收

### 15.1 数据、标签与成本

- 任一特征 `available_at > decision_at` 时数据集构建失败；
- 给历史数据追加未来行后，旧 `signal_evidence_snapshot/digest` 完全不变；
- batch 与 stream 指标结果一致；
- 未闭合 K 线不会触发 closed-bar 策略；
- 同根双障碍且无更细数据时恒判 SL；
- 市价/限价的 `NO_FILL`、部分成交、entry TTL、time stop 和 `EXECUTION_FAILURE` fixture 均进入完整 EV，终局概率和为 1；
- fill-to-fill 模式下改变 spread/impact 只通过成交价影响一次净收益；禁止再扣同名成本；
- 用相关成本分量 fixture 证明 `cost_p95_pred` 来自联合总成本分布，而不是边际 p95 求和；
- 反向合约 fixture 使用 `PnLAdapter` 后现金 PnL 与交易所账单定义一致。

### 15.2 公式和统计协议

- 三分类不同类别系数产生不同 logit/probability，参考类约束唯一，temperature scaling 后概率仍和为 1；
- binary fixture `G=0.02,L=0.01,C=0.001` 得到 `p_BE=0.366666...`；含 TIMEOUT/NO_FILL 的 fixture 输出 `break_even_probability=null`；
- 100 个完全重叠且等长事件得到 `N_unique=1`，100 个不重叠等长事件得到 `N_unique=100`；`N_ind` 与 iid 功效阈值比较时不会再次乘设计效应；
- block-bootstrap 固定 seed/块定义 fixture 的 `policy_value_lcb` 在规定容差内，改变 alpha 或 block version 必须改变统计方法版本；
- sealed holdout 打开后状态为 `CONSUMED`；对策略任何实质修改都不能再次使用它审批；
- DSR 使用净 UTC 日 NAV 收益和完整 trial 元数据；PBO 使用完整对齐矩阵。以论文示例/独立参考实现建立 golden fixture，缺任一 trial 列时测试失败；
- Holm family 包含失败试验；删除/隐藏失败试验不会改善已冻结 gate report；
- 校准 bin 边界只来自校准段，向测试数据追加未来记录不会改变旧 bin/指标；关键 bin `N_ind<100` 时为 `INSUFFICIENT`。

### 15.3 仓位、组合和状态

- 二元解析 Kelly 与数值求解在其适用 fixture 内一致；三分类/NO_FILL 使用完整 log-wealth 目标；任何压力情景财富乘数 ≤0 时该数量不可行；
- `quantity_grid`、Decimal rounding 和 tie-break 确定性；相同 signal evidence、账户/组合/capability/instrument revisions 与市场快照重放得到相同 `execution_decision_snapshot` 数量；
- 在同一 `execution_decision_snapshot` 的非成本输入、订单策略、成交结果和候选网格固定时，给每个候选量增加非负成本后，EV 与最终数量不得上升；
- `event_ev_robust_lcb≤0` 时最终数量恒为 0；`event_ev_robust_lcb≤m_EV` 或 `policy_value_lcb≤0` 时不得创建任何 LIVE 开仓 intent；
- 两个完全相关币不能各拿完整风险预算；
- 每个压力协方差矩阵通过 PSD 检查；逐元素拼接出的非 PSD fixture 必须被拒绝；最坏场景目标与逐场景穷举一致；
- CVaR 使用 loss 正号、99% tail 和 95% UCB；尾部样本不足或 bootstrap 失败时拒绝 LIVE；
- `decision/evidence_tier/environment/authorization_mode` 四轴不能互相提升；`MANUAL` 对未达 `LIVE_ELIGIBLE` 的事件仍无法创建 LIVE intent；
- PSI 固定分箱、监控窗口、alpha-spending 和审批到期均有边界值测试；暂停不会因坏样本滚出窗口自动恢复；
- 修改持有期/TP/SL 会强制产生新模型和策略版本；
- 所有 rejected/no-trade 也进入账本，防止只审计赢家；
- 新模型部署后仍能按 canonical byte/规定数值容差复现旧交易的 signal evidence、execution decision、概率、门禁和图层；不同语言序列化得到相同 digest。

## 16. 上线路径

1. **Backtest**：泄漏、成本、校准、PBO/DSR、压力门禁全部通过。
2. **Shadow**：至少 30 天且 100 个候选，只记录“本应怎么做”。
3. **Paper/Testnet**：至少 200 个可执行成交，验证实际延迟、滑点、部分成交、保护单和对账。
4. **Canary live**：正常风险额度的 10%，至少 30 天且 100 笔成熟交易，人工确认、白名单币种。
5. **Controlled auto**：先单策略单币，再逐级放大；每级重新审查漂移、执行 SLO 和最大回撤。

Shadow/Paper/Canary 的候选或成交数是执行与运维 SLO 样本，不能替代第 8 节的独立统计样本、功效和校准门禁。时间和样本两个条件同时满足；若策略频率不足，等待是正确结果，不能降低门槛凑上线。

第三方“当前快照”从接入日起才具有可信 point-in-time 历史。任何把 Coinglass/LSR 设为关键特征的模型，在积累到本规范要求的训练/校准/外测时间和 `N_ind` 前最多为 `PAPER`；只能切换到事先独立训练、校准和批准的不依赖该源的降级模型，不能把当前接口返回倒填成历史。
