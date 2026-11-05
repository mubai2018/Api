# SPCX 公共行情：Railway采集、持久存储、历史分析及GPT取数

## 架构（2026-11-05）

**主链路**：OKX官方公开WebSocket + 官方REST → Railway持续运行的单实例Bun Function → Railway Volume上的 SQLite（WAL、同步事务）→ 带数据质量和时间戳的公开只读API → ChatGPT的Railway连接器查询分析。

**灾备**：SQLite `serialize()`生成一致性快照，每6小时写入Railway私有S3兼容Bucket（首次启动约60秒即执行一次）。上传完成后使用对象HEAD/exists确认，并写入最新备份指针（只存元数据，不把私有密钥发布到API）。此为**备份而不是自动恢复**；灾难恢复需人工核实后从Bucket恢复。

**GitHub职责**：保留代码及原有历史数据，不再负责定时采集。以下三份工作流已移除`schedule`及`push`触发，只可手动运行：`.github/workflows/okx.yml`、`watchdog.yml`、`history.yml`。避免新数据写入与人工部署造成冲突。当前Railway服务与`railway/spcx-live-feed.ts`对应；代码仓库本身不会自动部署到Railway。

所有信息仅为无需登录的 **OKX公开市场数据**，不包含交易账户、私人API密钥、实际仓位、资金、网格参数修改或交易动作。

## Railway资源

- Railway项目：`spcx-live-monitor`（projectId `7a2780f0-81ef-4682-88d1-1dc3cf986200`）
- 服务：`spcx-live-feed`（serviceId `b8ebc1ec-be0d-40ee-a061-ce68eb5b0146`）
- SQLite Volume：`spcx-durable-sqlite`，挂载`/data`，Trial计划480MB
- 数据库路径：`/data/spcx-market.sqlite`
- 私有Bucket：`spcx-backups`（S3兼容，凭证通过Railway变量引用，未写入GitHub）
- WebSocket：`wss://ws.okx.com/ws/v5/public`订阅SPCX的`tickers`和`mark-price`；REST回退会注明来源而非假称逐笔推送。

### Railway公开API

| 路径 | 功能 |
|---|---|
| `/health` | 行情新鲜度、WebSocket与落盘健康；不健康返回503 |
| `/latest` | 最新公开成交价、标记价、BTC/ETH市场上下文 |
| `/v1/storage` | 数据库记录总数、K线序列、备份与回填状态 |
| `/v1/summary` | 风险、报价、24小时行情取样统计 |
| `/v1/gpt` | GPT分析专用结构化数据，含已存储K线/资金费/OI/质量 |
| `/v1/history?symbol=SPCX-USDT-SWAP&bar=1m&limit=100` | 分页历史K线，支持`from`/`to`的Unix毫秒时间戳 |
| `/v1/events` | 持久阈值事件（打开、恢复） |
| `/v1/funding` | 历史结算资金费率 |
| `/v1/oi` | 历史未平仓量（USD名义值，不能区分多空） |
| `/v1/quality` | SQLite内部完整性检查及源时间戳 |
| `/events` | 兼容旧版内存事件，**不能替代持久事件查询** |

所有接口均是只读。查询范围仅允许SPCX、BTC、ETH三个永续合约，以及1m、5m、1H、4H、1D周期；历史单次分页最多500条，防止无界查询。

## GPT无法直接获取网站JSON时的取数方法

1. 使用已连接Railway连接器 `test_webhook` 访问 `https://spcx-live-feed-production.up.railway.app/v1/gpt`。此工具只返回HTTP状态码，但会使Railway记录一条 `SPCX_GPT_REPORT` JSON。
2. 使用Railway `get_logs` `types=["deploy"]` 检索最新 `SPCX_GPT_REPORT `开头的结构化日志。在Functions工具的JS中先解析JSON，再只向模型输出实际需要的字段。
3. 检查 `generatedAt` 是否距当前≤90秒、`last.ts`和`mark.ts`相对于该快照是否≤30秒、`archiveVerified`及`storage.dbHealthy`是否为true。未符合不可将其当作实时行情。
4. 若需具体历史K线，调用 `test_webhook` 访问带查询参数的 `/v1/history`，然后读取新写入的 `SPCX_HISTORY_QUERY` 日志。日志仅回传最近最多100根K线，获取更多应分页。
5. 不能把 Railway `test_webhook` 只返回HTTP200视为数据校验通过，必须读取实际JSON和时间戳。

服务器自身每5分钟产生 `SPCX_GPT_REPORT`，并每分钟产生简短快照 `SPCX_FEED_SAMPLE`。ChatGPT分析任务每两小时运行，风险任务每小时运行；**没有秒级手机推送**。

## 历史真实性与回填

- SPCX最近的成交/标记价约每分钟生成一条持久快照。SQL主键防止相同分钟重复写入。
- 官方已完成K线以 `symbol/bar/ts`唯一键写入，OHLC满足 `high≥open/close≥low`等校验，只有`confirm=1`的K线会入库。
- 最新SPCX 1m/1H/4H/1D、BTC/ETH 1H/4H/1D在市场上下文刷新时增量入库（一般约2分钟）。
- 另有后台分批回填 `history-candles`，按日期向过去推进。SPCX 1m计划30天、5m计划90天、1H90天、4H180天、1D上市后可用日线；BTC/ETH对应1H90天、4H180天、1D365天。
- API不提供更早数据时会标注 `source limit` 并停止该序列，**不能补造旧数据**。回填未完成之前不得宣称覆盖完整。
- 合约历史可能存在SPCX Rebase，历史原始价格未复权，因此跨Rebase分析必须额外校验。
- GPT查询同时提供各周期观察到的区间、缺失根数、最大间隔与数据质量；“观察区间100%”不代表全部目标窗口已覆盖。

## 备份与可靠性

- Railway已挂载Volume，不随容器重启删除；已通过实际重启测试验证数据仍在。
- Bucket为私有存储，`S3_*`运行环境变量引用Railway Bucket凭证，严禁在聊天、GitHub、公开日志中打印真实密钥。
- 每6小时产生SQLite一致性备份；首次启动约60秒后先备份一次。若上传失败应出现`SPCX_DURABLE_BACKUP_FAIL`并在`/v1/storage`与`/v1/gpt`标注。
- 备份已验证上传并进行对象存在性检查；**尚未执行灾难恢复演练**。不应宣称自动恢复或双区域高可用。
- 主服务单副本，WebSocket断线自动重连，REST回退，Railway配置始终重启。若Railway试用期或额度耗尽会停止，需关注Railway Billing。
- Trial Volume容量约480MB，历史范围与备份随使用增长；达到容量上限后写入会失败，应尽早监控数据库体积与存储异常。
- 长期24小时/7天连续可用率需要继续收集日志观察，当前仅完成部署、实际写入、重启后可读和首份Bucket备份验收。

## 当前维护范围

第二阶段网格参数及回测暂停。Railway只监控公开行情并提供分析依据，不尝试计算私人账户强平价，不发送交易指令。用户如需即时价格提醒，应优先使用OKX App提供的原生通知。
