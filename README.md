# OKX公开SPCX监控与历史数据分析

本项目不访问OKX个人账户、不需要私人API密钥、不交易。2026-11-05已切换为**Railway持续采集 + Railway持久SQLite + Railway私有Bucket备份 + ChatGPT按需读取**。GitHub是代码和历史档案仓库，不再承担计划采集。

## 当前主链路（Railway）

- [实时行情与来源时间戳](https://spcx-live-feed-production.up.railway.app/latest)
- [运行/落盘健康状态](https://spcx-live-feed-production.up.railway.app/health)
- [数据库记录数量及备份状态](https://spcx-live-feed-production.up.railway.app/v1/storage)
- [GPT专用历史分析数据](https://spcx-live-feed-production.up.railway.app/v1/gpt)
- [持久化历史K线接口](https://spcx-live-feed-production.up.railway.app/v1/history?symbol=SPCX-USDT-SWAP&bar=1H&limit=100)
- [数据完整性与时效检查](https://spcx-live-feed-production.up.railway.app/v1/quality)
- [资金费记录](https://spcx-live-feed-production.up.railway.app/v1/funding) · [OI历史](https://spcx-live-feed-production.up.railway.app/v1/oi) · [持久事件](https://spcx-live-feed-production.up.railway.app/v1/events)

`spcx-live-monitor` Railway项目中，`spcx-live-feed`连接OKX官方公开WebSocket（TLS 443）和REST作为回退。按分钟保存SPCX公开价格快照，并按官方时间戳逐步回填已确认K线、资金费与未平仓量。SQLite位于挂载的Volume `/data`，容器重启后不会抹除；私有Bucket `spcx-backups`接收一致性快照，备份状态在上述API可核验。

**官方K线数据依序回填，目标范围仍在积累中。** 不填补缺失K线，不把跨Rebase历史直接拼接成可比价格序列。当前历史数据不包含个人仓位、成本价或强平价。Railway Trial到期、容量满、网络中断可能造成服务不可用，需要监控时效、备份并管理付费状态。

[Railway单文件完整服务源代码（版本快照）](railway/spcx-live-feed.ts) · [架构、API、存储、恢复及GPT取数说明](docs/RAILWAY_DURABLE_MONITOR.md)

## GitHub现存历史档案（只作参考/手动维护）

- [第一阶段归档索引](data/history/index.json)
- [第一阶段独立验收](data/quality/acceptance.json)
- [原有旧行情快照](data/latest.json) — **不保证新鲜，不再自动更新**
- [原有风险文件](data/risk/latest.json) — **不保证新鲜，不再自动更新**

以下三个GitHub Actions工作流已**移除schedule和push自动触发**，只保留`workflow_dispatch`手动操作，避免与Railway新数据源相互竞争：

- [旧行情采集](.github/workflows/okx.yml)
- [旧独立看门狗](.github/workflows/watchdog.yml)
- [旧历史回填](.github/workflows/history.yml)

不要因为旧仓库数据存在就推断当前价格。必须检查Railway新数据的官方源时间戳与`archiveVerified`。

## GPT分析任务

已启用的SPCX每小时风险巡检与每两小时市场研判优先读取Railway `/v1/gpt` 对应的 `SPCX_GPT_REPORT` 结构化日志（Railway连接器可读取），而不是依赖网页工具能否解析HTTP JSON。若需要特定日期历史，可以通过`/v1/history`查询并从`SPCX_HISTORY_QUERY`日志读取返回的有限记录。

ChatGPT任务每小时/两小时运行，**不是秒级手机提醒**。行情仍可能出现短暂断连，应使用OKX App的原生价格风险通知作为紧急防线。

其他历史文档：[第一阶段](docs/PHASE1.md) · [第三阶段风险定义](docs/PHASE3.md) · [故障排查](docs/REALTIME_DATA_FIX.md)。
