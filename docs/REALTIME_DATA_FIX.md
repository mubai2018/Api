# SPCX实时行情失效排障与修复记录（2026-11-05）

## 真正的故障位置

旧系统包含三个彼此独立的部分：

1. **OKX官方市场源**：公开REST与WebSocket，不需要账户API密钥。
2. **运行与采集环境**：GitHub Actions负责历史数据归档，Railway负责WebSocket常驻监测。
3. **ChatGPT分析任务**：每小时风险检查和每两小时市场研判。

此前ChatGPT的网页抓取环境无法直接访问或解析 `openapi.okx.com` / Railway `/latest` JSON。即使Railway服务正常并且WebSocket持续收到官方数据，ChatGPT任务仍回退到**过期的GitHub快照**，因而反复报告“数据失效”。

GitHub Actions曾出现旧提交推送冲突，计划 `schedule` 的成功运行频率不稳定。它不应当是实时行情唯一来源。

Railway原服务日志仅在用户或检查任务调用 `/health` 时产生完整 `SPCX_FEED_DIAGNOSTIC`；没有请求就没有持续日志，ChatGPT任务会取得旧记录。另有`wsaws.okx.com`备用WebSocket域名连接失败及重连竞态。以上是独立缺陷，不能笼统归咎于OKX断网。

## 当前运行架构

**主行情：Railway常驻WebSocket**（公开只读，无账户密钥）

- 项目：`spcx-live-monitor`
- 服务：`spcx-live-feed`
- 正常数据：<https://spcx-live-feed-production.up.railway.app/latest>
- 健康状态：<https://spcx-live-feed-production.up.railway.app/health>
- 最近阈值事件：<https://spcx-live-feed-production.up.railway.app/events>
- 生产日志关键词：`SPCX_FEED_SAMPLE`、`SPCX_MARKET_CONTEXT_UPDATED`、`SPCX_FEED_DIAGNOSTIC`、`OKX_WS_CONNECTED`、`OKX_REST_FAIL`

**采集**：官方 `wss://ws.okx.com/ws/v5/public` 订阅SPCX的 `tickers`、`mark-price`，断线时从 `https://openapi.okx.com` REST回退。关闭不可达的旧备用WebSocket域名；修复重复发起CONNECTING连接的竞态。

**关键修复**：服务器每约60秒主动输出一条 `SPCX_FEED_SAMPLE` 结构化日志，不依赖用户访问网页。ChatGPT已连接Railway工具可以通过 `get_logs(types=["deploy"],limit=30)` 读取最后一条带源时间戳的样本。若网页工具无法解析Railway JSON，**不得推断市场数据已经中断**。

**研究补充**：每约两分钟调用OKX公开REST获取BTC、ETH的Ticker，SPCX最近已确认1分钟/1小时/4小时/日线K线、资金费与未平仓量。以 `marketContext` 发布；要求checkedAt距当前≤5分钟且各字段无相关错误。1小时变化必须由连续61根**已确认**分钟K线计算，不使用进程重启后覆盖不足1小时的内存WebSocket样本。

**历史备用**：GitHub `mubai2018/Api` 的 `data/latest.json`、`data/history`、`data/risk`。任何消费者需要独立校验checkedAt及原始lastTs/markTs，不应把GitHub归档看作当前价格。GitHub的 `watchdog.json` 监测GitHub采集，不代表Railway实时源状态。

## 有效性规则（不可省略）

| 数据 | 可用要求 |
|---|---|
| Railway日志最新 `SPCX_FEED_SAMPLE` | 日志asOf距离当前不超过90秒，healthy=true |
| 成交价与标记价 | 各自源时间戳在本次样本时的ageMs≤30000，品种SPCX-USDT-SWAP |
| `marketContext` | checkedAt距当前≤5分钟，errors中相关字段没有报错 |
| 连续60分钟跌幅 | rolling60m.source=OKX confirmed 1m close，points≥61、spanMinutes=60，K线源时间戳新鲜 |
| GitHub备用 | 最新文件及每个报价源ts距当前≤10分钟，否则只能标为历史 |
| `/health` | HTTP200是必要但不充分，必须配合源时间戳和数据新鲜度 |

`/latest` 当前在行情失效时以HTTP503返回，`healthy=false` 不再冒充成功行情。Railway Connector 的 `test_webhook` 只能提供状态码；需要价格内容时读取 `get_logs` 后解析对应结构化日志。

## 验证证据

2026-11-05 08:47:21 KST旧版本日志显示SPCX成交价135.11、标记价135.12 USDT，源年龄311/250毫秒，WebSocket持续已收232989条消息。

修复后2026-11-05 08:52:59 KST自动日志显示SPCX成交价134.90、标记价134.91 USDT，源年龄275/153毫秒，WebSocket正常；官方已确认61根分钟K线、BTC/ETH报价、1H/4H/日线、资金费和未平仓量上下文全部获取且errors为空。以上均是历史验收快照，不是文档阅读时的实时价格。

## 尚未完成/不可过度承诺

- 连续24小时及更长Railway主服务可用率仍需要长期运行证据，进程重启及网络故障仍可能发生。
- Railway当前 `/events` 和WebSocket内存样本**不跨进程重启持久化**，不能作为完整历史审计。
- ChatGPT风险自动任务最低每小时检查，不能变成秒级手机推送。真正即时手机预警需要独立的通知渠道和持久化事件状态；未部署前依赖OKX自身强平/价格预警。
- Railway当前为Trial额度；试用期或余额耗尽可能停止服务，应检查Billing及资源用量。
- GitHub Actions schedule不保证固定每5分钟执行，它是归档/备份，不再承担主实时数据职责。
- 不访问私人账户，不交易，不调整网格数量，也不进行被暂停的第二阶段回测。
