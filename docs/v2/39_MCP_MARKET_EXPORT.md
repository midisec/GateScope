# MCP 行情、指标与 Coinglass 分析输出

日期：2026-09-13。状态：实现、生产 HTTPS MCP 协议及桌面/手机 Web 验收通过；未配置 OpenAI API Key，尚未进行第三方模型调用。完整发布记录见 [40_COINGLASS_MCP_RELEASE.md](40_COINGLASS_MCP_RELEASE.md)。

本轮仅扩展只读研究数据，不连接实盘、不下单、不将模型输出回写原形态或历史概率。用户已确认允许 Coinglass 留存、衍生分析和授权转发；MCP 仍按当前令牌绑定的租户/用户读取，不共享其他用户的数据权限。

## 1. 工具与权限

| 工具 | 必需范围 | 内容 |
| --- | --- | --- |
| `market_search_instruments` | `market:read` | 查找标的及其精确 instrument_id |
| `market_get_candles` | `market:read` | 闭合 OHLCV、真实 revision/venue/quality/available_at；无时间范围时返回最近一段 |
| `indicator_get_snapshot` | `analysis:read` | 最新连续闭合段上的指标及参数、热身、版本与可用时间 |
| `market_analysis_snapshot` | `market:read` **且** `analysis:read` | 指定币种/周期的一次组合输出：K 线、指标、窗口统计、未闭合观察、Coinglass 缓存 |
| `coinglass_get_snapshot` | `analysis:read` | 单独读取本用户授权的 Coinglass 缓存与维度/单位/质量 |

连接本身还需 `mcp:connect`。已有告警/PAPER 只读工具保留，但外接 OpenAI 研究示例只允许上述市场工具，不开放告警路由与交易档案。所有工具标注 read-only、non-destructive；调用仍会写安全审计事实和令牌使用时间，这是审计，不是市场或交易状态变更。

新迁移须让 `gatescope_mcp` 只读 `market_sync_state`、`market_live_candles` 及用户隔离的 Coinglass 缓存；不授予这些表的写入权限或供应商密钥访问权限。

## 2. 参数示例

Web 入口：用户下拉 → MCP。首页管理客户端和令牌；「调试单次调用（可选）」默认折叠，展开后搜索选择币种、周期与 K 线条数，点击「读取最新缓存」；可复制 `market_analysis_snapshot` 调用参数或分析 JSON。切换标的/周期/条数会中止旧请求并清空旧结果。预览使用登录会话，不签发外部令牌、不向 OpenAI 发送数据，也不触发 Coinglass 上游采集。

币种、市场和周期是**每次工具调用的参数**，不存为客户端或令牌的固定配置。同一令牌可以连续查询 BTC/1m、ETH/1h、龙虾/15m，无需先打开管理页或修改预览。Coinglass 同样按此次 instrument/timeframe 读取本人缓存；无缓存时返回实际状态，不读取上一个币种的结果。

Web 预览 `GET /api/v2/mcp/market-preview?instrument_id=...&timeframe=15m&limit=500` 与 MCP 共用计算函数；必须完成登录验证，用户级 12 次/分钟，响应 no-store。预览不会伪造 MCP token/client ID 写审计事实；外部 MCP 仍分别验证 connect/market/analysis scopes。

```json
{
  "symbol": "BTC_USDT",
  "market": "futures:usdt",
  "timeframe": "15m",
  "indicator_ids": ["ema", "rsi", "macd", "boll", "atr", "adx", "obv"],
  "bar_limit": 500
}
```

也可只给 `instrument_id`，但不能与 `symbol`/`market` 混用。symbol 必须完整精确匹配；`BTC` 不自动转换成 `BTC_USDT`，不能把现货/币本位/USDT 合约混在一起。有歧义时先搜索并使用 ID。中文币种同样保留原始符号，不做拼音或相似币猜测。

- 周期：`1m,5m,15m,30m,1h,4h,8h,1d`。
- `bar_limit`：50–2000，默认 500。不是一年回测接口；窗口范围随输出返回。
- 指标：`sma,ema,rsi,macd,boll,atr,adx,obv`，默认全部八项；自选 1–8 项、不可重复。参数和热身长度以系统实际 registry 为准，不暗示 EMA 默认为用户主图上的任意自定义周期。
- Coinglass MCP 永远 `request=False`，不因模型多次调用而触发上游流量或无限重试；网页/后台负责预热。未预热明确返回 NO_DATA/WARMING，不能补零或借用别人的配置。

## 3. 输出口径

组合工具版本 `market-analysis-1`，主要字段：

```text
instrument / timeframe / as_of
candles[] / latest_closed
realtime.status / realtime.candle / realtime.used_for_indicators=false
indicators[].indicator_id, version, parameters, warmup_bars, ready, values,
             window_start, bars_evaluated, open_time, available_at, bar_revision
statistics.window_start, window_end, bar_count, return_sample_count,
           close_to_close_change_percent, high, low, volume_sum,
           return_sample_stddev_percent, strategy_win_rate=null
quality.flags, gaps, missing_bars, contiguous_indicator_bars, stale,
        latest_close_age_seconds, data_watermark
cache / coinglass / policies / trace_id / research_only=true
```

K 线按时间升序；无范围请求先定位最近闭合 open_time，再以 `[latest-(limit-1)×周期, latest]` 限定最近日历周期，缺失样本不向更老历史扩充。最新查询绑定目录中真实 venue/market，使用 `DISTINCT ON(open_time)` 与时间/revision 倒序匹配已有索引，避免对全年历史按 venue/market 排序。指定历史 from/to 的原有首段语义不变。保留真正 canonical revision、来源和质量标记，不再把每条真实规范数据都伪装成 legacy。`data_watermark` 是整段最大 available_at，不假设最后一根的入库时间一定最大。

指标只使用请求 watermark 前已闭合且已可知的 K 线。窗口缺口之后重新热身，并返回实际连续样本数；缺口、样本不足、未准备好的指标值都是可见状态，不补价格或输出假数。实时未闭合 K 线独立展示，超过 45 秒观察年龄标为 STALE，不混入指标或窗口统计。

窗口涨跌是首尾**收盘价**变化；收益标准差只统计相邻闭合 K 线，跳过缺口。成交量保留交易所原生单位，未宣称为美元或基础币；上涨 K 线比例不等于策略胜率，本工具不编造胜率、置信度或盈利承诺。

Coinglass 保留 `scope/match/unit/native_timeframe/time/close_time/available_at/as_of/fetched_at/expires_at/quality`。这是多源读取，不是跨供应商原子快照；每源时钟与匹配状态都需检查。旧时间段在今天抓取的数据不能当作旧时已知证据；OI USD 不等于净多资金流入，账户多空比不等于持仓金额比，实际爆仓金额不是未来清算价格地图。

仅投影批准的分析字段，不输出 Coinglass Key、用户登录数据、供应商账户详情、配额或完整上游响应。

## 4. OpenAI GPT-6 Astra 连接示例

已按 OpenAI Docs 技能先搜索再实际打开官方文档：请求模型 ID 为 `gpt-6-astra`，官方模型页列有 MCP 支持；是否可用仍取决于调用方账户权限。[GPT-6 Astra 官方模型页](https://developers.openai.com/api/docs/models/gpt-6-astra)

Responses API 支持 Streamable HTTP 或 HTTP/SSE 远程 MCP。GateScope 使用前者，公开地址取用户设置页面的 MCP 地址。通过 `server_url` 与 `authorization` 连接；authorization 每次请求都需提供，不放 URL。以下例子仅供用户部署在自己的调用程序中，本轮未运行付费模型请求。[官方远程 MCP 文档](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)

```python
import os
from openai import OpenAI

client = OpenAI()  # 从受保护的 OPENAI_API_KEY 环境变量读取
response = client.responses.create(
    model="gpt-6-astra",
    store=False,
    tools=[{
        "type": "mcp",
        "server_label": "gatescope_market",
        "server_url": os.environ["GATESCOPE_MCP_URL"],
        "authorization": os.environ["GATESCOPE_MCP_TOKEN"],
        "allowed_tools": [
            "market_search_instruments",
            "market_analysis_snapshot",
            "coinglass_get_snapshot",
        ],
        # 用户已明确允许这些只读市场数据转发。其他工具不在允许列表中。
        "require_approval": "never",
    }],
    input=(
        "读取 BTC_USDT、futures:usdt、15m、最近500根闭合K线，"
        "使用 market_analysis_snapshot 获取常见指标和Coinglass数据。"
        "先检查币种/交易所/合约/单位/时间/缺口/过期情况；分别列出事实、"
        "证据分歧、条件情景和失效条件。未闭合K线仅作当前观察，"
        "不要编造缺失数据、胜率或保证收益，不要执行交易。"
    ),
)
print(response.output_text)
```

OpenAI Key 只给 OpenAI；GateScope MCP Token 给 MCP 鉴权，Coinglass Key 不传给模型。不要把令牌放入前端源码、Git、聊天文本或带查询参数的链接。只配置本文 market/analysis/connect 三种 scope，并设置短有效期；关闭或撤销令牌后后续调用应被拒绝。

本节是服务端 Responses API + PAT 的接入路径，不代表已验证用户 OpenAI 账户能调用模型。ChatGPT 网页 OAuth 的独立实现、部署与验证说明见 [41_CHATGPT_MCP_OAUTH.md](41_CHATGPT_MCP_OAUTH.md)；不要把两种认证方式混填。

MCP 进程使用显式 TransportSecuritySettings：DNS rebinding 防护保持开启，只允许配置的公开 Host/Origin 以及监听端口的精确 loopback 地址，没有通配 host/port。nginx 应将 `/mcp` 精确代理至 loopback MCP 端口，并保留公开 Host；错误 Host/Origin 仍拒绝，不能为解决外网 421 而关闭来源校验。

## 5. 验证与运维

隔离测试覆盖最小权限、ID/symbol 解歧义、字段界限、真实 revision、闭合/未来数据、缺口热身、统计样本、未知缓存与敏感字段投影。现有 MCP 只读工具清单同步扩展为 10 个。

本功能定向验证：55 项 Python 单元/API测试、8 项前端渲染/请求生命周期测试通过（2026-09-13）；不包含生产或付费模型调用结论。

最新 watermark 查找与最新 K 线查询都设置事务局部 `statement_timeout=5s`，不改变显式历史范围查询的 timeout。MCP 超时记录安全审计码 `DATA_QUERY_TIMEOUT` 并提示稍后重试，不透传 SQL/异常文本；Web 预览返回 503 与 `Retry-After: 5`。HTTP 客户端断开后，数据库查询也不会无限继续。

使用 `scripts/smoke_mcp_market_export.py` 可以对指定 MCP 服务做 initialize / tools/list / 三个市场工具的协议检查；从环境变量读取已有专用令牌，不创建令牌、不修改配置、不调用 OpenAI，也不输出令牌或完整市场响应。日志只包含工具名、状态、条数、年龄和结构校验结果。

经本次部署调试授权，`scripts/verify_deployed_mcp.py` 可在生产用 API 最小权限连接创建**全新的**一小时测试客户端/令牌，仅在内存中使用，测试 BTC_USDT/1m 与龙虾_USDT/15m；`finally` 只撤销本次分配的客户端及其令牌，再验证撤销后的 HTTP 401。失败只记录异常类和脚本行号，不记录异常文本。不会操作用户原有客户端；测试审计和已撤销的专用客户端记录保留，不进行删除。脚本已覆盖成功、工具失败、签发失败、撤销失败的隔离清理测试。

生产部署后的实际连接、延迟、数据状态与资源检查由发布记录补充。不要把单元测试通过当作生产连接成功；NO_DATA/STALE 属于真实数据状态，不包装成 READY。

首次生产协议验收记录：旧脚本的 httpx 导入失败发生在创建凭据之前，已改为生产 MCP SDK 同源的 httpx2。随后 BTC 的 snapshot 和龙虾的 initialize 出现 ReadTimeout；本次专用客户端成功撤销，撤销后 HTTP 401，未触碰用户原有客户端。该次失败发现最新 K 线无范围查询可能全历史排序，叠加 PostgreSQL 服务内存软限回收造成严重延迟。上文硬时间边界/索引友好查询已补单元验证，后续生产复测结果另行追加，不能用这次超时报告声称已联通。

热修后生产协议复测（2026-09-13，HTTPS `/mcp`，串行单次）：

| 标的/周期 | 协议与全流程耗时 | 闭合数据 | 指标 | 实时观察 | Coinglass |
| --- | --- | --- | --- | --- | --- |
| BTC_USDT / 1m | 通过，0.258 秒 | 请求 100 个周期，实得 99 根，最近闭合年龄约 47 秒 | 8 项中 1 项就绪；存在真实缺口，其他重新热身 | CURRENT | STALE，5 个序列，未声称新鲜 |
| 龙虾_USDT / 15m | 通过，0.187 秒 | 100 根，最近闭合年龄约 647 秒 | 8 项全部就绪 | CURRENT | PARTIAL，5 个序列，未声称完整 |

两次匿名请求均 401，工具清单均为 10 个只读工具。本次专用客户端/令牌已撤销，撤销后请求为 401；现有客户端未修改，没有调用 OpenAI。协议成功和数据质量分开验收：BTC 缺口/指标未热身、Coinglass 过期/部分源仍是需要显示的真实状态，不由 MCP 自动填补或改写。

最终生产复测（2026-09-13 12:07 Asia/Shanghai）：BTC 缺少的一根 K 线已从交易所按精确时刻核实补录，没有插值，也没有重写历史预测。BTC_USDT/1m 与龙虾_USDT/15m 均返回 100 根闭合 K 线、8/8 就绪指标，协议全流程分别为 0.259 秒和 0.185 秒；实时观察均 CURRENT，Coinglass 均 PARTIAL。仍保留上述早期失败/缺口记录，不把修复后的结果当成旧时状态。

最终桌面 1440 px、手机 390 px 浏览器验收通过：输出周期/条数切换、两种周期的 100 根数据及 8 项指标、无页面横向溢出；客户端长名称自动折行。匿名和撤销令牌均 401，最后一次临时客户端已撤销，用户已有客户端未修改。以上延迟是服务器本机经公网 HTTPS 的单次验收，不是 OpenAI 调用延迟或持续性能承诺。

## 6. OWNER 创建与近期身份确认（2026-09-13 补充）

OWNER 仍可管理本人 MCP 客户端/令牌，权限范围没有扩大到其他角色或其他用户。修复创建成功后页面把不含 `tokens` 的响应直接当完整列表项、导致分页组件渲染失败的问题；创建响应现在在前端规范为 `tokens=[]`，自动选中后可继续签发。

敏感操作仍需十分钟内密码认证，令牌签发/吊销还需新的 MFA 验证码。超过时限的 OWNER 返回 `RECENT_AUTH_REQUIRED`，页面弹出「确认登录身份」，不再误导为没有 OWNER 权限。非 OWNER 返回独立 `ROLE_FORBIDDEN`。

新增 `POST /auth/reauthenticate`：已有会话、MFA、CSRF 都保持强制；公网提交为用户绑定的加密信封，purpose `user:{user_id}:reauthenticate`，内容仅 `{password}`。5 次/5 分钟用户级预算，信封不可重放，成功仅更新原会话最近认证时间。页面不保存密码、不自动重试创建/签发，避免重复操作。令牌有效期增加 1 小时选项供临时只读调试。

隔离回归：本次后端相关 66 项单元/API 测试、4 项真实 PostgreSQL 集成，前端 34 文件/194 项测试通过。新增覆盖 OWNER 创建→签发→吊销、过期后的加密重认证、MFA/CSRF/租户隔离；同一 principal/service 连续变更币种/周期，确认工具参数及对应 Coinglass 读取互不干扰。生产验收另行记录，不以单元测试代替。

补充生产验收（2026-09-13 13:18，脚本 `scripts/smoke_mcp_owner_browser.py`）：真实 OWNER Web 创建 201（响应不含 tokens，页面正确显示空列表）、用户绑定加密密码确认 200、全新 MFA 步签发 1 小时三项最小权限令牌 201。未选择管理页预览币种/周期；同一令牌连续 BTC_USDT/1m → 龙虾_USDT/15m → BTC_USDT/15m，均返回 100 根闭合数据、8/8 就绪指标，耗时分别 0.368/0.170/0.162 秒。Coinglass 真实状态依次 STALE/PARTIAL/PARTIAL，没有冒充全部新鲜。

通过 Web 新 MFA 步吊销本轮客户端 200，撤销后 MCP 401；没有使用后端兜底清理，没有修改原有客户端，没有调用 OpenAI。桌面 1440 px、手机 390 px 管理/签发后/吊销后截图均无横向溢出，无 JS 错误，登出 200；私有证据 `/tmp/gatescope-owner-mcp-browser-arrq2ltq`。首次自动验收在标签定位时超时，已补充客户端/时长选择框的明确无障碍标签；首次测试客户端亦已精确撤销，未遗留可用测试令牌。

部署未新增迁移，head 仍 0041。本次回退资料 `data/release-backups/mcp-owner-yq348opJ`；最终页面资产 `index-yZyCzjoK.js`、`index-CQr_3S12.css`、`lib-BRVYcm49.js`。API/MCP 已重启并验证健康，行情后台保持运行。OpenAPI V2 已重新生成并通过生成一致性检查，秘密/架构/JSON 合同检查通过。
