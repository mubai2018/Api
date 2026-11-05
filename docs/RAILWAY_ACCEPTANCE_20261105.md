# 2026-11-05 SPCX新架构首轮真实验收

## 目标与范围

用户要求完全取消GitHub每5分钟采集，Railway承担OKX公开行情持续接收、历史持久化、回溯与统计，支持ChatGPT分析。不得访问个人OKX账户、使用私人API密钥、执行交易或建议更改网格数量。

## 实际完成的链路

1. OKX官方公共WebSocket SPCX ticker/mark-price（443端口），REST回退。
2. Railway Bun服务 `spcx-live-feed`，唯一主数据源。
3. Railway Volume `spcx-durable-sqlite`（480MB，挂载`/data`）中的SQLite WAL数据库。
4. 分钟快照与公开资金费、OI写入；通过 `history-candles` 按序列分页补充11组官方已确认K线。
5. Railway私有S3兼容Bucket `spcx-backups`，每6小时备份，首次部署约60秒备份一次。
6. 公共只读 `/v1/gpt`、`/v1/history`、`/v1/storage`、`/v1/quality`、`/v1/funding`、`/v1/oi`、`/v1/events`。
7. Railway `SPCX_GPT_REPORT` 日志为ChatGPT连接器提供来源、历史数据、时间戳、覆盖率和缺口，避免web.open无法直接解析JSON时误判数据故障。
8. GitHub三份工作流（`okx.yml`、`watchdog.yml`、`history.yml`）已移除所有`schedule`与`push`触发，只保留手动`workflow_dispatch`；旧历史档案保留。

## 验收证据（均为真实运行）

| 测试 | 证据 | 判定 |
|---|---|---|
| Railway部署 | 服务部署状态SUCCESS，官方WebSocket、REST正常 | 通过 |
| 新数据库启动 | 日志 `SPCX_DURABLE_DATABASE_READY` | 通过 |
| 重启数据持续性 | 实际重启后仍有2条行情、2,067根K线、100条资金费、1条OI；之后反复部署中记录继续增加 | 通过 |
| 历史回填 | `SPCX_BACKFILL_PROGRESS` 实际从OKX `history-candles`读取，最新已验证启动中K线总数超过15,000 | 持续进行 |
| 资金费历史回填 | 2026-11-05T00:24:11Z日志 `SPCX_FUNDING_ARCHIVED {"processed":281,"total":281}` | 通过 |
| SQLite质量 | `SPCX_GPT_REPORT` 中 `integrity:["ok"]` 且 `archiveVerified:true` | 通过 |
| Bucket一致性备份 | `SPCX_DURABLE_BACKUP_OK`，成功上传私有对象且HEAD验证 | 通过 |
| Bucket隔离读取恢复 | 2026-11-05T00:23:06Z `SPCX_DURABLE_BACKUP_RESTORE_TEST_OK`，2,498,560字节完整读取为隔离SQLite，16条快照可查 | 通过 |
| 公共历史与GPT接口 | `/v1/gpt`、`/v1/history`、`/v1/storage`、`/v1/quality` 等实际返回HTTP200，`SPCX_GPT_REPORT` JSON解析成功 | 通过 |
| GitHub定时禁用 | 读取上述3个workflow的`on:`配置，只包含手动触发 | 通过 |
| GPT自动研判提示词 | 已更新为优先Railway /v1/gpt→SPCX_GPT_REPORT，禁止旧GitHub行情作为当前报价 | 已配置，尚需下一次独立定时运行验收 |

## 当前仍有限制

- **首次回填进行中**：所有历史长度、首尾时刻与缺口必须以查询时的数据库结果为准。不可将部分覆盖当成完整30/90/365天。
- **真实24小时稳定性**：本轮验证不能证明连续24小时在线或绝对不丢分钟数据，应根据后续日志、源时间戳、数据库快照的间隔持续验收。
- **灾难恢复**：已完成小文件从Bucket读取并校验SQLite的隔离恢复测试，但尚未实施正式生产Volume损坏后的替换恢复流程；不能宣称完全自动灾难恢复。
- **实时通知**：ChatGPT巡检每小时一次、市场研判每两小时一次，暂无毫秒级手机推送。紧急风控依靠OKX App原生预警。
- **Trial资源与容量**：Railway试用额度、存储480MB容量及账单可能限制服务可用率，需持续检查。
- **风险推断**：所有信号均只基于OKX公开市场，不包含实际仓位、账户权益或强平价。SPCX上市前数据不可补造，2026年6月合约Rebase前后原始价格不可未经调整直接对比。

## 查阅

- Railway行情与报告：<https://spcx-live-feed-production.up.railway.app/v1/gpt>
- 历史数据：<https://spcx-live-feed-production.up.railway.app/v1/history?symbol=SPCX-USDT-SWAP&bar=1H&limit=100>
- 数据库统计：<https://spcx-live-feed-production.up.railway.app/v1/storage>
- 健康状态：<https://spcx-live-feed-production.up.railway.app/health>
- 完整源代码：`railway/spcx-live-feed.ts`
- 架构说明：`docs/RAILWAY_DURABLE_MONITOR.md`
