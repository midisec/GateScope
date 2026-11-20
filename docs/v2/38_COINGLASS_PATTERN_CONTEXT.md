# Coinglass 与形成中形态联动设计

日期：2026-09-13。状态：**用户已确认许可，图表/缓存/研判及 MCP 输出已实现，生产验收见发布记录**。

本轮要求：数据维度必须匹配；图表副图辅助当前形态；形成中的形态可点击 Coinglass 数据支撑，进一步研判入场和目标区间。不接入实盘，不修改既有识别规则、原始预测、确认状态或历史概率。

## 1. 已核实的能力与待确认项

生产现有配置通过只读方式解密到进程内存，密钥仅在 `X-API-Key` 请求头发送到固定 `https://vip2.coinglass.site`。禁止重定向，单次探测不自动重试，遇 429 即停止本轮后续请求并报告 Retry-After。不输出密钥、账户余额、到期时间或完整响应。

有限实测结果：

| 接口 | 结果 | 证据边界 |
| --- | --- | --- |
| `/api/gateway/account` | HTTP/业务校验成功 | 响应包含速率与 WebSocket 元数据，不能据此确认留存、衍生分析或多用户展示许可 |
| `/api/futures/supported-exchange-pairs` | HTTP/业务校验成功 | Gate 中 BTC / USDT / USDT 匹配到唯一 `BTC_USDT`；存在一个 base_asset 为龙虾的候选，未将此等同于所有历史端点都支持龙虾 |
| `/api/futures/open-interest/history` | Gate BTC_USDT、1m、16 条成功 | 字段 time/open/high/low/close；不代表所有币种、周期或一年历史都可获取 |
| `/api/futures/funding-rate/history` | 同上，16 条成功 | 费率数值单位、计费周期仍须作为独立契约核实，不仅凭 OHLC 字段名归一化 |
| `/api/futures/global-long-short-account-ratio/history` | 同上，16 条成功 | 明确是账户比例，不是大户持仓比例或净买入金额 |
| `/api/futures/liquidation/history` | 同上，16 条成功 | 多空实际爆仓金额，不能充当未来清算价格分布 |

以上为设计阶段的有限探测；当时未修改生产配置、数据库、服务或代码，也未持久化探测行情值。本次用户确认后另行实现和部署。

用户已于本次部署请求确认留存、衍生分析和授权转发许可；`feature-source-matrix.yaml` 仅开放本次原生历史图表源，保留旧 coins-markets/LSR 信号及自动交易限制。当前实现仅配置创建者本人及其 MCP 令牌可读取该账号数据，不自动共享给同工作区其他用户。资金费率单位仍待验证，不参与计算。

官方字段参考（官方能力不等同于中转账户授权）：[持仓历史](https://docs.coinglass.com/reference/oi-ohlc-histroy)、[资金费率历史](https://docs.coinglass.com/reference/fr-ohlc-histroy)、[账户多空比](https://docs.coinglass.com/reference/global-longshort-account-ratio)、[实际爆仓历史](https://docs.coinglass.com/reference/liquidation-history)。

## 2. 必须匹配的维度

| 维度 | 规则 | 不匹配时 |
| --- | --- | --- |
| 资产身份 | 本地 instrument_id + venue/market/base/quote/settle 对照供应商支持表；保留原始合约标识 | 拒绝同名币、现货替代、倍数币种与猜测式代码转换 |
| 交易所与范围 | 优先 Gate 同合约；全市场聚合仅作为另一个明确标注的背景层 | 不标为本合约精确证据，不用于本合约价格区间调整 |
| 产品类型 | 永续/交割/现货及结算资产分别校验；有歧义不自动选择 | AMBIGUOUS 或 UNSUPPORTED |
| 指标群体 | 全账户、头部账户、头部持仓分别定义与展示 | 不合并成一个多空比 |
| 数值单位 | OI USD/币本位/合约张数分开，比例与百分数分开；资金费率保留计费周期 | 单位未核实就不能参与计算 |
| 时间粒度 | 使用真实原生周期；较粗数据只能标为较粗周期背景 | 不将 5m 插值成 1m，不将 24h 滚动总额拆成每根 K 线 |
| 已知时间 | 保留 period_start/end、fetched_at、available_at；研判只能使用当时已可知数据 | 未来数据排除，迟到数据不回写旧研判 |
| 完整性与新鲜度 | 有序、唯一时间、有限值、闭合时间窗、缺口、覆盖率与 TTL 分别校验 | STALE/EMPTY/PARTIAL/MISMATCH，缺失不补零 |

已有历史端点今天返回的旧数据，可以用于今天的研究；不能冒称它已在旧形态出现时被本平台获取。历史重演与实时采集证据需明确区分。

## 3. 页面与图表交互

1. 在 FORMING 卡片及结构详情增加「Coinglass 数据支撑」。请求绑定当前币种、周期、context、candidate 和 evidence；服务端重新读取真实候选，不相信前端传入的阶段或目标价。
2. 关闭原详情后打开独立研判弹窗，避免双弹窗争用焦点。内容分为原始结构、匹配检查、资金面证据、条件研判和情景试算；加载、重试、限频、过期及不支持状态完整可见。
3. 主图保留原理线；OI、费率、账户多空比、爆仓量使用可选副图。不同单位不共用数值轴。电脑可多选，手机一次查看一个副图。
4. 十字线与时间轴联动，突出所选形态的观察窗口。读数明确区分“原始判断时已保存的数据”和“本次最新研判的数据”。旧判断没有留存时显示无当时证据，不拿今天的值补上。
5. 只有用户明确点击「在图表查看情景」才显示新的虚线区间；从本次研判发布时间开始，不画回历史，不修改原始形态线。支持清除/撤销。
6. 切换币种、周期、用户或候选立即取消旧请求并移除旧情景。迟到响应不得显示在新标的上；新数据刷新不静默改变用户已经打开的研判快照。
7. 长弹窗标题与关闭按钮保持可见，手机触控区至少 44px。数据说明放折叠区，不在主图堆叠长解释。

## 4. 入场与目标区间的严格区分

当前代码把 FORMING 的 signal_direction 保持为 neutral。`NEXT_PIVOT` 预测第二峰、B/C 浪或下一浪的候选拐点，不是突破后的止盈目标。Coinglass 不得改变这一语义，也不能跳过几何条件直接将形态升级为已形成或已确认。

展示三个独立层次：

- **原始预测**：锁定展示原 forecast / initial_forecast / recognition、确认条件和结构失效位。
- **当前资金面证据**：描述同口径 OI 变化、费率、账户比例及已发生爆仓；未经过样本验证的信号不叫“胜率提升”，不为原几何评分加分。
- **用户情景**：在原始预测之外试算观察入场价、目标区间及方向风险。若用户保存，必须创建独立版本，引用父 forecast，不回写原事件或原命中统计。

第一期不能仅凭四类标量自动平移或放大价格目标：这些量没有价格分布坐标。OI_USD 还受标的价格变化影响，价格与美元持仓同时上涨不等于净多资金流入；账户数量偏多不等于多头持仓金额更多。

FORMING + NEXT_PIVOT：保持“等待结构形成”，可对照/试算下一拐点观察区；不将该区间命名为止盈区，不计算虚假的交易收益率。确需提前入场，必须是单独、明确标识的研究假设，不能冒用该形态已经确认的交易方向。

CONDITIONAL_MOVE 且方向明确：展示当前已收盘 K 线时点投影的确认线、原失效位和条件目标。斜线不能把旧 forecast.confirmation.price 永久当水平价格；须保留投影时间。独立用户情景可计算价差风险收益比：

```text
s = +1（多）或 -1（空）
E = 情景入场，S = 原结构失效位，T = 情景目标
R = s(T-E) / s(E-S)
```

只在分子、分母均为正且目标区间有序时计算，标明未扣成本、不是胜率、不代表可成交。目标不在盈利侧、入场跨越失效位、非有限价格或区间反转需拦截。终止/过期形态不生成新的有效入场建议；不建议为使情景成立而扩大原止损。

自动新价区若要后续加入，需有获准的价格密度/清算分布源或独立回测过的定价规则；形态下一拐点、实际爆仓量、估计清算热力图三者不可混用。

## 5. 已实现的数据与接口

采用独立用户范围的标准化数据快照与研判快照，API 不接触解密后的第三方密钥。market workload 负责固定端点读取、限额调度、币种映射与质量校验，API 只读取获准投影。后台调度不以网页是否打开为条件，但须在使用许可、启用状态和配额允许后启动。

标准化数据至少包含：

```text
snapshot_id, user/tenant scope, config_secret_version,
instrument_id, provider_exchange/symbol, base/quote/settle/market,
alignment = EXACT | AGGREGATE_CONTEXT | COARSER_CONTEXT | AMBIGUOUS | UNSUPPORTED,
metric_id, population, unit, native_interval, funding_interval,
period_start/end, fetched_at, available_at,
points, gaps, coverage, state, stale_after, source/schema version
```

独立研判至少包含：

```text
assessment_id, assessment_version, tenant/user,
context_id, candidate_id, evidence_id,
forecast_id, initial_forecast_id, snapshot_ids,
assessed_at, decision_bar_time,
basis = CURRENT_REASSESSMENT_NOT_ORIGINAL_EVIDENCE,
state, metric_evidence, original_conditions,
observation_action, scenario(optional), reason_codes,
adjusted_formation_probability = null,
adjusted_win_rate = null,
auto_order = false
```

缓存/任务去重键必须包含用户与配置版本、资产与数据范围、周期、候选证据、数据快照和规则版本。限频按配额桶有界调度；429 遵守 Retry-After，不循环轰炸，不阻塞 K 线主更新线程。凭据更换/停用与用户权限变化必须影响在途任务和新请求，不能跨用户共享可见性。

权限未确认时不写入第三方市场历史或衍生研判，不将来源标为 automatic_feature_eligible。未实现的入口不能在正式页面伪装可用。

## 6. 实施顺序与验收

1. 确认数据使用范围及端点字段/单位；建立逐源能力契约，保持未知项不可用。
2. 实现精确资产映射和原生周期归一化，覆盖 BTC、中文币种、倍数币种、同名冲突、现货/永续隔离。
3. 增加用户范围的数据采集任务、投影、缺口/新鲜度状态和配额控制；真实 PostgreSQL 验证权限隔离、配置轮换、重试与崩溃恢复。
4. 增加副图、形成中入口和独立研判弹窗；保持原始预测完全不变。
5. 加入独立情景试算与可撤销覆盖层；保存能力必须独立追加，不覆盖既有档案。
6. 验证重复/未来/缺失/较粗周期数据、USD OI 误推方向、下一拐点误作止盈、斜线投影、多空 R 对称和非法区间。
7. 电脑与手机真实交互验收：维度标签、切币/切周期取消、空数据、不支持、过期、配额不足、副图与原理线对齐、情景清除、长弹窗关闭和原预测/历史概率不变。

上述功能现已实现。接口为 GET `/api/v2/coinglass/chart`、POST `/api/v2/coinglass/reviews`、GET `/api/v2/coinglass/reviews/{id}`；MCP 见 [数据输出说明](39_MCP_MARKET_EXPORT.md)。未声称这些描述性信号已经提高胜率，也没有放宽实盘保护。

生产验收修正：

- 0040 建立用户隔离的最多 256 期缓存与独立研判；0041 将自动采集收敛到自选默认周期，其他周期按网页请求预热并保温 2 小时，最近 5 分钟查看的周期优先。Gate K 线自身的全周期后台同步不受影响。
- 采集按账户速率窗保留至少一半额度，批次最低 5 秒、未知时 30 秒；每个有效端点间隔 2.1 秒，429 结束本批并按 Retry-After 和自适应降频调度。单位未核实的资金费率不浪费请求额度，仍返回 UNKNOWN 状态。
- 新查看仅创建独立研究快照；若卡片 evidence 已更新，返回明确的 `evidence_changed` 标志和最新结构，不让旧卡片陷入重试冲突。用户保存情景时必须匹配当前 evidence，冲突仍返回 409，不悄悄改变用户已经填好的情景。
- 原始预测、历史识别档案、概率与形态阶段不更新。形成中目标仅作为下一拐点观察区；保存/清除覆盖层不等于交易所入场、止盈或止损。
