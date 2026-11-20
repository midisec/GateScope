# 波浪与形态严谨性修复（2026-10-04）

状态：本地实现与回归验证；**未部署到生产**。本次没有修改服务器、告警配置、历史数据库或触发全量重算，不构成实盘胜率提升的证明。

版本：`structure-study-3.3` / `geometry-3.3` / `structure-forecast-2`。

## 1. 本次修复

| 问题 | 修复与可验证边界 |
| --- | --- |
| 波浪价格存在 NaN、Inf、非法点数时可能穿过比较逻辑 | 明确验证有限正价格、标准推进浪已知点数、方向类型；重复时间和同根高低点不虚构先后顺序。 |
| 识别输入可能先被排序、去重，掩盖因果窗口问题 | 进入识别前验证时间递增、OHLC 一致性、成交量及显式收盘标记；不静默修复错误输入。省略 `is_final` 的旧适配器仍须保证只传入已收盘数据。 |
| 斐波那契指导范围与合法波浪结构不相交时，没有目标 | 波浪 4/5 优先使用指导范围与硬约束交集；无交集时给结构可行区，不剔除合法候选。延长第五浪没有硬上限时，仅给突破位外一个第一浪长度的有限观察范围，并明确不是结构上限或命中概率。 |
| 新增枢轴导致失效线收紧，但当根已越界被漏记 | 首次接纳、新增枢轴、形成时均立即复核当根已知的失效条件；按顺序保存阶段变化及失败，不能等下一根反弹后漏掉失败。 |
| 第三浪较短时缺少第五浪长度约束；完整周期缺少原点边界 | 新增冻结的 `levels.hard_limits`，画出对应失效参考线。第三浪可以并列最短，但不能独自最短；完整周期原点约束属于当前模型路径，不代表所有 Elliott 数浪都必须如此。 |
| 当前可用列表未使用上述新增边界 | 只读筛选对同样的高低价、严格/含等号边界执行否决，非法约束或所需价格缺失时不展示为可用；盘中否决不回写生命周期或确认形态。旧筛选阈值不变。 |
| 双/三顶底及头肩锚点数量、颈线位置约束不完整 | 分阶段验证所需锚点数和交替位置，防止把头部误作颈线；头部必须突出。保留允许的不对称肩部。 |
| 通道、楔形、三角形两条线跨度不同，直接比较端点变化 | 要求唯一上下边界、合法时间与重叠范围，在相同时间坐标比较上下边界；交叉边界不能交换名称后重新接纳。 |
| 杯柄深度与真实极值不一致 | 使用右杯沿到底部的深度检查柄部位置，并检查收盘序列真实杯底、柄底；仍使用原有收盘价口径，不混入另一套影线定义。 |
| 斜颈线目标高度与触发价存在时间/价格偏差 | 头肩量度高度按“头部到头部时刻颈线”的垂直距离；触发价使用当前 K 线开盘坐标，与生命周期确认逻辑一致，避免用收盘可知时间多外推一根。 |
| 展示检查项与实际执行不一致 | 几何审核不再自行声称已验证真实 OHLC、收盘输入及边界残差；仅在这些检查实际通过后追加。 |

没有提高最低评分来压缩数量，没有用 Fibonacci 比例替代硬规则，也没有降低确认收盘根数来伪装更及时。

## 2. 失败、预测与概率口径

- 首次证据与首次预测继续冻结；后续枢轴和目标仅作为新版本追加，不能用后来的走势改写原图。
- 已发布目标在同一根后续 K 线上触价并失效时记为 `AMBIGUOUS`，不假定先止盈；当根收盘才发布的目标不能用此前的当根影线记命中。
- 形成后又失效可以同时保留“结构形成”和“后续确认失败”；结构形成不等于盈利。
- 失败记录保留，独立样本只计一次，终止候选不因后续重新出现而复活；价格观测缺口不硬判成败。
- 几何评分、历史形成率、突破确认率、目标触及与扣成本收益不是同一个量。目标修正仍来自此前独立完成的腿，不能解释为包含所有失败的总体交易胜率。
- 新增预测版本、区间来源、确认参考时间保留在紧凑比较数据中；完整原始预测仍留在压缩证据中，历史详情不使用摘要替代原图。
- Web 对无合适 Fibonacci 区间的结果显示“结构约束候选区 · 非斐波那契目标”；无历史预测的记录不事后补写。

## 3. 验证方法与证据

局部反例与回归覆盖：有限数值、上涨/下跌镜像、不同价格尺度、浅/深回撤、短第三浪、延长第五浪、同根新增边界越界、同根触价失效、已记录结果不改写、历史证据压缩、告警及 MCP 图表兼容性。

合法合成对照在修改前后共 14 组，目标类别均仍可识别，候选总数 **40 → 40**：

| 对照 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 双顶 / 双底 | 1 / 1 | 1 / 1 |
| 三顶 / 三底 | 4 / 4 | 4 / 4 |
| 头肩顶 / 倒头肩 | 3 / 3 | 3 / 3 |
| 上升 / 下降推进浪 | 3 / 3 | 3 / 3 |
| ABC 下跌 / 上涨修正 | 2 / 2 | 2 / 2 |
| 上升 / 下降完整周期 | 3 / 3 | 3 / 3 |
| 杯柄 / 反转杯柄 | 4 / 4 | 4 / 4 |

这里数量为每组行情识别出的全部候选，不等于该形态的历史样本数。另有既有 26 类形态及 2 类突破回测形态的规则测试、嵌套边界候选保留与低拟合合法结构测试。**合成对照不能外推为真实行情无漏检或胜率提高。**

本地最终测试结果另见下方“验收记录”。前端验证为组件与类型测试，不是生产浏览器点击验收。

## 4. 范围限制与上线顺序

1. 推进浪仍是普通非对角线、非截短第五浪的几何子集，不实现全部 Elliott 分型、低一级 5-3-5-3-5 子浪验证，也不能证明数浪唯一。不得把合法对角线波浪的重叠误称为普遍理论错误。
2. 本次没有消除右侧枢轴确认和连续两根收盘确认的固有等待；早期预警应单独标为形成中，不能回填成当时已确认。
3. 模型语义变化使用 3.3 独立版本。既有按版本查询、`MODEL_CHANGED` 中断机制继续生效；3.2 历史证据保留，不把旧统计直接冒充新规则概率。切换后新统计可能暂时不足，不能填造样本。
4. 上线前先检查生产磁盘、内存、实时 worker 与暂停回填状态；在容量受限环境不得因为版本变化直接开启全年全币种重算。
5. 先对少量自选/周期做有界、逐根、仅使用当时已收盘数据的影子回放，比较新旧候选保留率、失败率、形成/告警延迟、扣成本结果和计算/写入开销。不能只挑成功例子或用同批数据调参后宣称样本外有效。
6. 通过上述容量与语义验收后，再协调 API、实时识别、历史研究 worker 同版本发布，并验证 Web/TG/MCP 原图与失效边界一致。回滚保留旧证据与新版独立证据，不覆盖历史。

## 5. 理论参考

- Elliott Wave International：[Impulse](https://www.elliottwave.com/waveopedia/impulse/) 与 [Extension](https://www.elliottwave.com/waveopedia/extension/)。本实现只覆盖其中有限几何约束；比例指导不等于强制比例。
- StockCharts：[Head and Shoulders Top](https://chartschool.stockcharts.com/table-of-contents/chart-analysis/chart-patterns/head-and-shoulders-top)。用于区分头部、颈线及量度目标；目标是观察依据，不是价格保证。
- StockCharts：[Introduction to Chart Patterns](https://chartschool.stockcharts.com/table-of-contents/chart-analysis/introduction-to-chart-patterns)。识别与解释存在主观性及失败风险，需由独立历史验证补充。

## 6. 验收记录

- 最终后端选定回归集：**666 passed**，12.19 秒。仅有一条现有 Starlette/AnyIO 弃用提示，无失败；不是全仓库测试或生产数据库验收。
- 前端 `StructureForecast`、`patternModel`、`patternOverlays`：**14 passed**；`npm run typecheck` 通过。
- 本次修改的 Python 源码与相关新增/修改测试：Ruff 通过。
- 14 组合法合成对照再次运行，40 个候选、14 个目标类别均保留；不代表真实市场召回率已验证。
- 未执行生产部署、实盘交易、真实 TG 投递、新版全量历史重算或浏览器端端到端验收。

后端复验命令（项目根目录）：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -o addopts='' \
  tests/unit/test_structure_precision.py \
  tests/unit/test_wave_rules_precision.py \
  tests/unit/test_structure_geometry_precision.py \
  tests/unit/test_structure_rules.py \
  tests/unit/test_structure_lifecycle.py \
  tests/unit/test_structure_forecast.py \
  tests/unit/test_structure_coverage.py \
  tests/unit/test_structure_replay_parity.py \
  tests/unit/test_pattern_study.py \
  tests/unit/test_structure_archive_codec.py \
  tests/unit/test_structure_progress_precision.py \
  tests/unit/test_structure_trade.py \
  tests/unit/test_structure_trade_review.py \
  tests/unit/test_pattern_trade_notice.py \
  tests/unit/test_alerts.py \
  tests/unit/test_mcp_structures.py \
  tests/unit/test_structure_research.py \
  tests/unit/test_structure_record_reference.py \
  tests/unit/test_forecast_training_merge.py \
  tests/unit/test_pattern_write_amplification.py \
  tests/unit/test_telegram_signal_evidence.py \
  tests/unit/test_mcp_chart.py \
  tests/unit/test_mcp_chart_content.py \
  tests/golden/test_pattern_engine_golden.py \
  tests/unit/test_structure_screening.py \
  tests/unit/test_structure_screening_hard_limits.py \
  tests/unit/test_current_pattern_screening.py \
  tests/unit/test_current_pattern_screening_api.py -q
```

前端复验命令（`web/` 目录）：

```bash
npm test -- --maxWorkers=1 src/StructureForecast.test.tsx src/patternModel.test.ts src/patternOverlays.test.ts
npm run typecheck
```
