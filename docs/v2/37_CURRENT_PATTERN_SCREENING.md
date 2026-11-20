# 当前形态评分筛选与结果详情

日期：2026-09-13。范围为行情页面当前结构的只读筛选与展示；不接入实盘、不新增开平仓策略，不改变形态识别、历史回放、概率算法、交易或 TG 配置。

## 页面使用

入口：`/markets/chart` → 形态与波浪 → 当前结构。

- 几何评分区间支持 0–100 分，可只填下限或上限；提供全部、70 分以上、85 分以上、仅未评分快捷入口。
- “应用筛选”有加载反馈，非法区间在网页提示且不发送请求；“重置筛选”恢复类别、状态、搜索及评分条件。
- 条件作用于当前币种/周期的整个跟踪集合，再分页，每页 12 个。不会只在已经加载的一页内筛选，也不会删除被筛掉的候选。
- “筛选结果详情”冻结此次打开的筛选快照，显示全部匹配量、各类型数量与占比、方向和阶段分布；类型列表每页 8 个，点击“查看匹配”回到该类型的候选列表。
- 每个候选增加独立“查看详情”：原始目标、形成条件、历史形成/确认概率、同类历史净胜率、样本/区间、原始档案图；原理线叠加仍最多 3 个，使用原有时间价格锚点。
- 两个新增详情弹窗的标题与关闭按钮固定在滚动区域顶部，手机查看长内容或翻到最后一页后仍可直接关闭；不修改其他模块弹窗。

## 口径边界

“置信度筛选”筛选的是已有 `latest.confidence` 几何评分，不是盈利概率。默认保留未评分，指定数值范围时排除未评分；0 分是有效评分，不能当成缺失值。异常非有限评分只在响应副本中归为未知，不回写原始 checkpoint。

当前统计范围是 `RECENT_TRACKED`，不是过去一年所有候选。单个候选可以重叠，方向数量占比仅为列表分布，不等于未来上涨/下跌概率。

汇总中的历史概率引用同模型报告：形成概率取 FORMING / ALL 的 formation，突破确认概率取 FORMED / ALL；历史净胜率取所选 FORMED 或 CONFIRMED 阶段、5/10/20/50 根观察期。报告不匹配当前模型时不显示旧结果，部分回放明确标识；没有统计不是 0%。概率不足 20 个有效样本不显示精确概率，样本量与区间可展开。

现有收益投影没有任意精确评分区间字段，因此汇总和单个候选的历史净胜率均明确标为“同类全部评分”，**未按当前评分区间或方向重算**。固定期限方向净收益也不是实际止盈止损成交胜率。单个候选的形成/确认历史参考继续使用原始阶段、原始评分分组和原判断时点，首次预测不被当前统计覆盖。

不能相加各候选引用的历史样本数或平均概率合成为市场概率。若以后需要精确范围条件下的历史胜率，应新增原始评分/方向的索引投影并后台有界回填，不能以最终评分倒推历史，也不能每次打开页面扫描全部原始 JSON。

## 接口与性能

原 `GET /api/v2/patterns/current` 增加可选参数：`confidence_min`、`confidence_max`（0–1、闭区间）、`unscored_only`、`pattern`（exact ID）。区间倒置、非有限值、未评分与范围同时设置返回 422；默认调用保持旧集合语义。

新增 `summary` 含 `total/scored_count/unscored_count/directions/phase_counts/patterns`；类型项含数量、占比、方向计数及实际评分范围。原 `tracked_total/phase_counts` 仍是筛选前跟踪集合统计；summary 是全部匹配项统计，翻页不改变分母。

评分过滤与分布汇总只遍历已有内存 checkpoint，仍然仅对当页候选查询历史参考，避免数百候选造成额外历史查询。缓存键包含全部条件。历史报告只在用户打开详情时 GET，关闭取消请求；没有新增业务写接口或权限。

首轮线上浏览器验收发现旧的逐候选历史参考查询发生大量回表读取：当前集合和报告读取仅需几十毫秒，但个别历史参考耗时数秒，整页超过 30 秒。不是通过延长浏览器超时、截断历史、去掉失败样本或补造概率来处理。新增迁移 `0039_structure_reference_cover`，以 `(context_id, pattern, entry_phase, score_bucket, stage_time)` 为键，覆盖 `candidate_id, phase, resolved_time, sampled, formed`，保留原概率 SQL 和旧索引。

索引使用并发建立，避免阻塞业务表正常写入。失败重试仅清理名字、表和定义全部匹配的 INVALID 索引；合法同构索引保留，同名异构对象拒绝操作。新增索引不修改 K 线、识别事件、原始预测或收益事实。Index Only Scan 仍受 PostgreSQL 可见性映射影响，须以线上实际耗时验收，不能仅凭计划名称声称提速。

## 发布与验收

生产变更前代码/静态资源备份：`data/release-backups/pattern-screening-EkjIyi/code-before.tgz`，SHA-256 `c6a255cdb503e28f76afe7f592d09df7630035cc6d6de0d2ffe124075ca1ef18`。代码回退只恢复本次 API / 仓储 / 前端及入口资源；新增索引与旧代码兼容，无需恢复数据库。若确需移除索引，可单独降级 0039（精确核验后并发删除该索引）；不删除 K 线或候选事件。

首批代码发布包 `/tmp/gatescope-screening-release-LHrWby.tgz` 的本地与生产 SHA-256 一致：`8db03bb82efa76190d3886f488922b36b934b0f6892c2da31783ae53ee2e968a`。随后增补 0039 迁移、详情小样本提示与弹窗固定标题，最终前端资源为 `index-Ddg03tSq.js`、`index-BJZtr_Mc.css`、`lib-DZHLdoSx.js`；保留旧散列文件，不删除已打开页面需要的资源。只重新加载 API，market / strategy / research 无需重启。

最终验收版本的源码、迁移、测试、验收脚本与当前静态资源归档为 `/tmp/gatescope-screening-verified-0q75dp.tgz`，SHA-256 `34011f2b3a02e6b3e3a8d5748302e46b56570a9d0111310585116316d5fc126d`；生产持久副本 `data/release-backups/pattern-screening-EkjIyi/release-verified.tgz`（0600）。归档不含凭据、数据库和用户配置；本文作为单独发布回执维护。

生产迁移已完成：`0039_structure_reference_cover`，索引 valid / ready / live 均为 true，大小 368 MB，耗时 86.44 秒。并发建立使用会话级 64 MB maintenance_work_mem、单维护进程及有界超时，没有修改数据库全局配置。迁移文件 SHA-256 `c812206d3a375facc53ad5827fd5b1e559e2ec6597c365ee33b0b9c518abf160`。

建索引后，旧页可见性导致部分查询仍需大量回表。对且仅对 `public.market_structure_stages` 执行普通限速 `VACUUM (ANALYZE, INDEX_CLEANUP ON, TRUNCATE false, PARALLEL 0)`，不是 VACUUM FULL；64 MB 维护内存、2 ms cost delay、cost limit 500、300 秒超时，不修改历史事实。all-visible 从 57.6% 提升到 99.9%。随后 BTC 1m、70–95 分首次仓储请求（不命中仓储缓存）当页 12 条实测 248.3 ms，limit 96 返回全部 37 条实测 360.1 ms；最慢参考的再测计划为 Index Only Scan、6 次 Heap Fetches / 3969 行、7.883 ms。该结果是当时缓存与负载下的即时测量，不是所有币种和冷磁盘的 SLA。

自动化验收：

- 后端最终全量 **829 passed**，171.83 秒，9 条既有弃用警告，无失败、无跳过；专用测试 PostgreSQL 已停止。
- 前端 28 个测试文件 **155 passed**，TypeScript 和生产构建通过；构建保留现有大 chunk 提示，不把此提示等同性能验收。
- 新增真实 PostgreSQL 最小权限验收覆盖 240 条候选、评分范围筛出 120 条后跨 96 条分页、摘要总量不随页长变化、null/0/1、六种阶段和三类方向、未知类型、非法 422、空集及缓存隔离。
- API 角色不能 UPDATE 原始跟踪 checkpoint；筛选前后的 JSON 及文本完全一致，没有新增 journal / stages / returns。
- 前端覆盖空样本、低样本、旧模型隔离、形成与确认概率分离、收益观察期、类型分页、关闭 Abort、失败重试和换标的丢弃迟到响应。
- 新增 15 项迁移单元测试和 2 项真实 PostgreSQL 索引生命周期测试。3 万行测试自然选用新覆盖索引，历史参考逐字段一致，事实摘要、旧索引 OID 和权限不变；真实取消并发建立后产生的 INVALID 索引可重试恢复，合法索引幂等复用。
- V1/V2 契约一致性、秘密扫描与架构边界通过；18 个部署源码/迁移/脚本/契约/入口与资源文件同本地 SHA-256 一致。
- 生产 9 个受检服务 active，内网和公开 HTTPS `/health/ready` 返回 ready。维护后可用内存约 4.1 GB、磁盘余量 21 GB，未发现本轮内存瓶颈；这是即时检查，不是长期负载保证。
- 首次线上验收未通过：BTC 1m K 线正常，形态历史参考超时，尚未进入筛选断言；证据 `/tmp/gatescope-pattern-screening-browser-ykxzcwfl/report.json`。此次失败不计为验收通过，需索引优化后重新验证。
- 修复后 BTC_USDT 1m 浏览器验收通过，1440 / 390 双端共 20.695 秒：70–95 分匹配 37 条；70 分以上 38 条、85 分以上 3 条、未评分 63 条、全部 233 条（动态跟踪快照，后续数量会变化）。主列表分页、12 / 96 页长下全部摘要不变性、非法区间零请求、空结果、原始目标、返回与图表叠加、类型占比和历史率对照实际报告均通过。50 个 API 响应、28 个 current 响应，API / JS 错误、越界业务写入和非法范围请求均为 0，注销 200。
- BTC 本次 70 分以上仅有 8 个类型，汇总只验证到单页边界；不能把该次报告中的 type_pagination=passed 解释为真实多页翻页已覆盖。生产报告 `/tmp/gatescope-pattern-screening-browser-k3u4dsmd/report.json`，SHA-256 `eb2b2f06d7e9fc16bbe7423ebe0a9a1359139d20354b98e40f85a409a0f01897`；本地安全报告与脱敏双端截图在 `/tmp/gatescope-pattern-screening-pass.fkooLV/`（0700）。
- 龙虾_USDT 1m 双端补测通过：全部 190 个结构（108 已评 / 82 未评）、20 类型，电脑和手机都实际翻完 3 页并逐类对照统计；70–95 分 9 个、85 分以上为真实空集。51 个 API 响应、24 个 current 响应，无 API / JS 错误或越界业务写入，注销 200。浏览器 current 请求中位 17.9 ms、P95 170.2 ms、最大 356.8 ms，仅代表这次验收，不是长期性能保证。生产报告 `/tmp/gatescope-pattern-screening-browser-m1j88e56/report.json`，SHA-256 `ecc530ea3f9d77073fb6681fbb12b16777dfe699a14a45831633c69c98643e1a`；本地报告和清晰视口截图 `/tmp/gatescope-pattern-screening-chinese.DS3bBS/`（0700）。
- 固定弹窗标题后最终再验收通过，23.047 秒：两端候选详情滚到底部、20 类型翻到第 3 页，关闭按钮仍 100% 位于视口内。56 个 API 响应、25 个 current 响应，无 API / JS 错误、非法范围请求或越界业务写入，注销 200。生产报告 `/tmp/gatescope-pattern-screening-browser-5ptuwlfl/report.json`，SHA-256 `5cf43455e9f4c824fcff9da256d2b0f1fbc95227ea01dea804067b0a246a71a8`；本地最终报告与视口截图 `/tmp/gatescope-pattern-screening-final.lY2BGX/`（0700）。主代理已亲自检查手机最后一页及详情底部截图。
- 最终验收脚本 `scripts/smoke_pattern_screening.py` SHA-256 `16a69b7cb69a296399fe9191f584b8d637a5fa312a2a486011cd5707def14e07`。仅允许同源读取、登录/MFA/自身注销和既有只读指标预览，不保存布局、不创建自选、通知、交易或设置；证据只记录安全计数、分类耗时和脱敏截图，不输出认证材料。
