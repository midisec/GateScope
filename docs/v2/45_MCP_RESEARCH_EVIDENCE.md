# MCP 研究证据与图表接口

日期：2026-09-13。目标：向 ChatGPT 提供既有行情、形态/波浪、失败历史、统计与本人回测证据，辅助研判，不提供下单能力。

## 工具清单

在原有 10 个工具基础上新增 10 个工具。币种与周期是每次请求的参数，不绑定到管理页面。

| 工具 | 内容 | 权限与边界 |
| --- | --- | --- |
| `market_chart_snapshot` | 实际 PNG + 与图片一致的 OHLCV、EMA9/21、RSI14、成交量 | `market:read analysis:read`；30–300 根，默认150；推荐分别请求5m/15m/1h |
| `market_research_bundle` | 多周期行情、8项默认指标、Coinglass缓存、每周期前5个结构 | `market:read analysis:read`；最多4周期、每周期50–500根；逐项标记数据质量 |
| `indicator_get_series` | 支持显式参数的历史指标序列 | `market:read analysis:read`；50–2000根计算窗口，每页最多500点 |
| `structure_current` | 既有形态/波浪筛选、原预测、历史参考、筛选后分布 | `analysis:read`；每页1–50条 |
| `structure_study` | 已存历史研究汇总、统计分母、样本区间和回放进度 | `analysis:read`；不请求或重算全年研究 |
| `structure_event_list` | 既有形成、确认、失败、过期、删失案例列表 | `analysis:read`；RECORDED/REPLAY分开，每页1–50条 |
| `structure_event_detail` | 单案例首次预测、生命周期分页、原理线与冻结原图数据 | `analysis:read`；每页1–50节点，原图最多500根，可设0省略K线 |
| `watchlist_list` | 本人自选、默认周期与已有缓存信息 | 显式 `research:read`；每页1–100条 |
| `research_backtest_list` | 本人已创建回测的轻量摘要 | 显式 `research:read`；每页1–50条 |
| `research_backtest_detail` | summary/validation/trades/equity/cases/events/inputs | 显式 `research:read`；每页1–100条；不适用的部分明确标记 |

所有工具仍需 `mcp:connect`。新工具不改变业务状态，不排队回放、不订阅币种、不调用交易所或Coinglass上游、不创建或推进回测。调用审计和原有凭据使用记录继续保留。

原有 `market_analysis_snapshot` / `coinglass_get_snapshot` 保持既有受限缓存刷新行为；新 `market_research_bundle` 显式使用 `request=False`，不会放大Coinglass队列。

## 旧会话兼容修复：1.2.1

2026-09-13 21:05（北京时间）已部署并验收：用户20:55:42查询在生产审计20:55:43对应`market_analysis_snapshot`成功。此前新图像与形态仅在新增工具中，旧入口仍为`market-analysis-1`纯文本；该响应版本不是旧服务器的证据。隧道和nginx均指向新版MCP，未发现工具清单缓存或图片过滤。此次修复旧工具本身，不要求调用方先发现新工具：

- 原工具名与参数保持不变；返回顶层`version=market-analysis-2`，原数据格式标记`market_data_version=market-analysis-1`。
- 保留原K线、实时观察、指标、统计、Coinglass和数据质量字段；追加`chart`、`structures`和`image_status`，MCP内容实际包含`text`与`image/png`。
- `chart`严格复用本次`candles/as_of/revision`，只绘制最近最多150根已收盘K线；EMA9/21、RSI14用本次已返回的完整连续尾段预热，不进行第二次行情查询。预热不足保留null，故100根输入中有效EMA9/EMA21/RSI14点数分别为92/80/86，不能把它误认成缺失K线。
- `structures`返回同一币种/周期最多5项既有形态/波浪，保留关键价位、触发/失效条件、确认状态与历史参考；结构有独立检查点时间，不能视为与K线原子同步。
- 图像或可选结构失败时保留行情并明确`UNAVAILABLE/NO_DATA`，不输出异常原文，不将未知历史胜率填0。认证错误不降级。
- 仅使用原有`market:read`与`analysis:read`权限；截图与公开形态不需要`research:read`，也不扩大旧授权。Web管理页内部预览仍可保持旧数据格式，不代表MCP升级失败。

验收通过：1382项后端单元/黄金回归；真实OAuth下只调用旧工具取得SOL_USDT的5m/15m/1h三张PNG，每张100根K线、5条形态，三次`image_status`及`structures.status`均为`READY`。同时重新验证全部新增工具、OAuth轮换/撤销，专用测试连接已撤销。只重启MCP，没有变更数据库或前端，没有调用OpenAI模型/用户ChatGPT会话。

要让ChatGPT发现新增的独立工具，按[OpenAI Docs的刷新流程](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)：在该连接详情选择Refresh，确认元数据更新，再开启新会话重新选择连接。已发布插件的元数据另有审核发布流程；服务器重启不等于会话工具列表自动刷新。旧入口富返回不依赖刷新工具列表。

脱敏验收记录：[evidence/mcp-legacy-snapshot-20260913.json](evidence/mcp-legacy-snapshot-20260913.json)。

## 图片与数值的一致性

`market_chart_snapshot` 返回 MCP `image/png` 内容块与 `structuredContent`：

- `instrument`：精确交易对、市场、交易所；不能仅凭基础币名匹配合约。
- `captured_at`、`last_closed_at`、`timezone=UTC`、`includes_forming_candle`。
- `candles`：开收盘时间、OHLCV、修订号、数据可见时间与final标记。
- `overlays.ema9/ema21/rsi14`：与K线open_time精确对齐的数值。
- `quality`、`quality_flags`、`data_watermark`：缺口、陈旧和可用性。

图片为固定1200×900三面板；不跨缺口连接指标，不把未预热值填零。默认仅收盘K线；显式 `include_forming=true` 时仅绘制当前有效未收盘观察，并以浅色标记，指标仍只计算已收盘部分。

最多额外读取200根作为指标预热。指标仍是有限输入窗口的计算结果，不保证和任意第三方无限历史初始化值完全相同。原始OHLCV不能替代账户风险预算、合约乘数和下单步长；本接口不假装已具备这些仓位计算输入。

## 形态与统计解释

`shape_summary` 提供 `name/timeframe/key_levels/trigger_price/invalidation_price/is_confirmed`，并保留条件与字段来源。`FORMED`是几何形成、尚待确认；只有`CONFIRMED`才标记确认。双向触发、动态边界或缺少冻结价位时，固定价返回null，保留原理线与条件，不从新行情倒推旧预测。

几何匹配评分、形成概率、目标观察率、扣成本交易盈利率必须分开。无样本/未完成/超出查询预算是UNKNOWN，不是零胜率。失败、过期、删失保持原样；统计明确分母、重叠处理、观察期、数据覆盖和模型版本。历史回放不证明这些历史数据当时已实时可用。

`structure_event_detail` 的原图与首次预测使用冻结时间和历史修订；后续几何单独输出，不覆盖原始图。绘图窗口裁剪、锚点缺失和修订差异会明确标记。

个人回测按原引擎解释。结构案例统计没有组合资金曲线时，`equity`返回`NOT_APPLICABLE`，不拼造资金曲线。已有策略参数、费用/滑点/资金费率假设、训练与验证划分随报告保留。

## OAuth与最小数据库权限

新个人研究权限 `research:read` 默认不选，必须在同意页明确勾选；只能批准客户端本次已请求的权限子集。旧token/grant/client的权限不自动扩大。新DCR默认支持4项权限，但authorize省略scope仍仅请求原3项；客户端能力上限不是实际授权。

缺少研究权限时，私有工具返回 `isError` 与 `_meta["mcp/www_authenticate"]`；工具列表同时声明顶层和`_meta`的`securitySchemes`，供ChatGPT识别增权授权需求。原有alert/signal/PAPER交易工具仍不向OAuth连接开放。

0045仅增加必要SELECT、scope约束和MCP自选owner限制策略。私有仓储同时使用显式tenant/owner条件与RLS，且在数据库强制READ ONLY的REPEATABLE READ事务内执行；公开结构也使用只读一致性快照。密钥、通知配置、账户凭据均不在导出范围。

旧连接先刷新工具列表；个人研究提示权限不足时重新授权并勾选研究权限。若旧注册客户端只支持原3项且无法增权，需要重新添加连接，不应通过修改旧token权限绕过同意。

## 负载与边界

- MCP分页offset上限10000；历史大窗口按币种/周期/来源/批次筛选后逐页获取。
- 结构SQL1500ms、历史参考单次500ms/合计约1.5s；个人研究SQL2s；K线SQL5s，锁等待另有上限。
- 多周期为12秒软调度预算：超过预算不再发起后续部分，在途有界查询可能稍后完成，不伪称严格12秒端到端SLA。
- 数据源并非原子同步快照，必须检查各自时钟、单位、范围和过期状态。
- Coinglass仍受上游权限/配额、Gate口径及近期窗口限制，资金费率单位未确认时保持不可用；此版本没有编造或补零。
- 只输出既有证据，不复制全年K线/完整事件库，不新增批量后台存储任务。

## 可直接给ChatGPT的使用指令

> 通过GateScope MCP分析BTC_USDT、futures:usdt。先查精确合约身份，再获取5m、15m、1h的market_chart_snapshot，交叉核对OHLCV、EMA9/21、RSI14、成交量和收盘时间。读取当前形态与波浪、原触发/失效条件、同源历史样本和失败案例，并检查Coinglass每项数据的范围、单位与新鲜度。输出支持与反对证据、触发条件、失效条件和缺失数据。不得把几何评分当胜率，不得据未收盘K线宣称确认，不得编造仓位输入或下单。需要个人自选/回测时先取得research:read授权。

## 验证与部署记录

已于2026-09-13 20:41–20:44（北京时间）部署并验收：MCP `gatescope-research` 1.2.0，数据库0045，前端资源 `index-Dt2nIsPE.js`。仅重启API/MCP；行情采集、通知和隧道没有重启。代码与数据库结构备份约21MB，本次没有复制历史数据库。0045迁移在3秒锁等待、30秒语句预算下成功执行，没有扩大旧授权。

| 验证 | 结果 |
| --- | --- |
| 后端单元/黄金回归 | 1350通过；1项既有Starlette弃用警告 |
| PostgreSQL专项 | 29项通过，覆盖真实角色/本人隔离、只读、旧scope不变、迁移升降级 |
| Web | 252项通过；TypeScript、生产构建与OpenAPI契约一致性通过 |
| 线上MCP | 20工具可发现；新增10工具均实际调用，包括本人已有回测的4个报告分区 |
| 实际图像 | BTC_USDT合约5m/15m/1h各100根收盘K线、1200×900 PNG，三项指标各100个有效点，价格/时间/修订与EMA递推核验通过；另已目视检查实际图像 |
| OAuth/页面 | MFA登录、显式研究scope、1440/390像素无横向溢出、CSRF、PKCE、resource绑定、刷新轮换/重放撤销、主动撤销均通过；仅创建并撤销专用测试连接 |
| 部署状态 | HTTPS与数据库ready；API/MCP/行情/隧道active；隧道控制面成功轮询严格检查退出0 |

第二轮线上图像请求耗时分别0.108/0.114/1.747秒，多周期研究包2.176秒；这是单次验收观测，不是持续服务SLA。发布后API/MCP各约114/116MiB内存，磁盘仍76%，未发现本次变更导致的内存瓶颈。

证据不等于全部数据就绪：本次100根窗口的历史案例原图标记`CLIPPED`；5m组合查询的部分历史参考标记`PARTIAL`。调用方可以增加原图`bar_limit`（最多500），并检查锚点覆盖，不能将裁剪坐标替换成新坐标。历史参考缺失/超预算仍为UNKNOWN，不填零。1h部分旧数据保留`LEGACY_SOURCE`与可用时点估算标记；Coinglass资金费率单位未确认、陈旧缓存等既有约束仍在。

验收脚本仅拦截ChatGPT回调目标，没有向ChatGPT发送代码或调用OpenAI模型。浏览器对被主动中止的同一回调URL发生一次重试，脚本按完整URL去重并记录重试次数；没有放宽不同code/state目标的数量校验。授权凭据仅保留内存，已有用户连接与交易状态不变。

脱敏结果见[evidence/mcp-research-20260913.json](evidence/mcp-research-20260913.json)。`live_contract_pass=true`表示接口验收通过，`data_ready=false`保留上述证据不完整状态；不能据此宣称所有数据均就绪或交易胜率已提升。

接口设计参照OpenAI Docs的[结构化工具返回](https://developers.openai.com/plugins/build/mcp-server)与[逐工具OAuth声明和错误挑战](https://developers.openai.com/plugins/build/auth)。
