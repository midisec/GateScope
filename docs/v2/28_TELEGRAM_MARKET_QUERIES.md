# Telegram 自选行情查询与未推送排查

2026-09-12；迁移 0030；通知工作进程扩展，无新常驻进程。

## 使用

绑定后向机器人发送 `/watchlist`、`/menu`，或 `/chart BTC 1h`。
菜单可选自己的币种、八种周期、形成中 / 已形成 / 已确认；列表每页六条，点击返回带原理线的 K 线图。
详情支持首次识别图和最新状态图。EMA20/60、BOLL20、RSI14、MACD 使用与 Web 相同的指标计算实现。
所有查询只读，不创建告警、不改变阈值、不下单。Web 通知设置提供机器人入口。

## 2026-09-11 晚间未推送的证据

生产当前只有 ETH 4h 一条有效规则，要求已确认、匹配评分至少 70，选择了双底、三底、倒头肩、杯柄和下降楔形。
规则名称与这些偏多方向不一致；BTC 规则已被删除，不自动恢复。
9 月 12 日 04:00 的三个杯柄确认评分约 51.98、53.13、57.37，低于 70；其他确认类型不在该规则选择范围。
窗口内没有生成通知 outbox、alert event 或 delivery，因此不是 Telegram 投递失败。

另外查到告警 runner 仍读取 legacy pattern-engine-1.0.0，新图表读取 structure-study；两条识别链路不一致。
这项差异不能被解释为此次一定漏发：新版杯柄也未达到原规则阈值。
本次未降低用户阈值、未恢复已删除规则、未把全自选订阅成自动通知。新菜单统一读图表的持久跟踪和原始事件档案。
告警链路统一仍须单独处理模型版本、事件可知时间、去重与冷却边界，不能直接重放历史通知。

## 可靠性与权限

- Webhook 校验秘密头、绑定用户、私聊身份；回调由服务端已保存 action 解析，不接受客户端任意查询目标。
- 数据库队列 FORCE RLS，用户与绑定再次匹配；重复 update ID 只排一次，按钮有效期 24 小时，每用户每分钟最多 12 个查询。
- 立即回答 callback 去除 Telegram 转圈；图表由通知 worker 异步生成，每轮至多两个请求。
- 发送前和重试前检查绑定、会员及自选权限；按冻结的图表数据重试，不按未来行情重绘初始证据。
- 业务码、message ID 和 HTTP 状态一并检查；429 有界退避，最多四次尝试；任务 15 分钟到期。
- 七天后仅清理临时菜单任务，绝不删除形态、交易或失败证据。网络不确定时仍可能重复投递，不宣称 Telegram exactly-once。
- Bot token 仅送 Telegram 域名，不进入 URL 日志、用户回答或图表。

Telegram 的按钮长度、回调应答及图片限制以 [Bot API 官方文档](https://core.telegram.org/bots/api) 为准。

## 验证入口

`tests/unit/test_telegram_market.py`、`tests/integration/test_telegram_market.py` 覆盖私聊/跨用户隔离、重复入队、重启、PNG、撤销绑定、Webhook 和业务码。
`scripts/enable_telegram_market_menu.py --apply` 更新已有 webhook 的 callback 接收和命令菜单，不改变绑定。
`scripts/smoke_telegram_market.py` 默认只读；显式 `--send` 才向配置中的唯一 OWNER 绑定发送一条标注为部署验收的只读图表消息。
