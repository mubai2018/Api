# 第一阶段：公开行情历史数据库与数据质量

仓库：mubai2018/Api。仅使用OKX公开REST接口；不读取交易账户，也不要求私人API密钥。

## 已部署的工作流

1. [行情采样](../.github/workflows/okx.yml)：计划每约5分钟执行一次 `collect.py`，再运行 `phase1.py snapshot`。
   - `data/latest.json`：兼容旧版监控的最新有效报价与分析数据。
   - `data/history/market/YYYY-MM-DD.jsonl`：按UTC日期归档全部三个合约的价格、标记价格、资金费、未平仓量及源时间戳。
   - `data/quality/latest.json`：最新快照的时效性、缺失数据和最近采样间隔告警。
2. [历史回填](../.github/workflows/history.yml)：首次和手动运行可完整回填，后续每天尝试增量。
   - `data/history/candles/<instId>/<bar>/YYYY-MM.jsonl`：OKX已确认K线，按月保存（数组列定义见下文）。
   - `data/history/funding/<instId>/YYYY-MM.jsonl`：历史已结算资金费率。
   - `data/history/index.json`：各时间序列的覆盖数量、开始/结束时间、间断次数、回填错误。
   - `data/metadata/instruments.json`：合约规格，如合约面值、报价步长、最小下单数量、交易状态。
   - `data/metadata/instrument_changes.jsonl`：监测到合约规格发生变更时记录变更前后信息。

## 历史覆盖目标（以官方可用实际数据为准）

| 合约 | 粒度 | 希望回填时长 |
|---|---|---|
| SPCX-USDT-SWAP | 1分钟 | 7天 |
| SPCX-USDT-SWAP | 5分钟 | 30天 |
| SPCX/BTC/ETH USDT永续 | 1小时 | 90天 |
| SPCX/BTC/ETH USDT永续 | 4小时 | 180天 |
| SPCX/BTC/ETH USDT永续 | 日线（UTC+8开盘） | 365天 |
| SPCX/BTC/ETH USDT永续 | 已结算资金费 | 最长90天目标 |

这些是**回填目标**，不是已经取得的样本保证。若合约上市不足对应天数，历史自然更短。用 `index.json` 的 `firstTs`、`lastTs`、`count`、`gapCount` 和 `warnings` 核对实际覆盖。

## 文件结构与精度

K线JSONL每行：
```
[ts_ms, open, high, low, close, volume]
```
只存入 `confirm=1` 的已完成K线，价格和成交量为浮点数，研究极小金额时仍应使用OKX原始字符串/Decimal复核。1D K线按OKX官方UTC+8时间开盘。

历史资金费每行：
```
[fundingTime_ms, realized_funding_rate]
```
**资金费率本身不是账户已支付资金费**，也不能乘以当前仓位直接得出历史实际扣费金额。未平仓量是全市场值，不区分多空。

每5分钟的公开行情归档中，`ts` 为采集完成时间，具体报价的真实生成时间见 `lastTs`、`markTs`。采集可能出现间断，不可把计划采样频率当作实际持续采样。

## 数据质量原则

- 只有OKX接口返回 `code=0`、交易品种匹配、时间戳有效、OHLC约束正确时才接收。
- 只接受已确认K线，按时间戳去重；失败不补造历史。
- `quality/latest.json` 及 `history/index.json` 可能标记 `degraded`；**不能因为最新文件仍能打开就认为实时数据有效**。
- 消费者始终需要验证 `latest.json.checkedAt` 和每个产品报价生成时间，建议不超过10分钟。
- 历史完整性依赖OKX可用历史深度、采集稳定性和GitHub Actions调度；`gapCount` 是发现的非连续片段数，不是缺失K线数量。

## SPCX历史重大事件

OKX于 **2026-06-02** 执行SPACEXUSDT盘前永续Rebase并更名为SPCXUSDT，公布比例 **12.52**。见 [OKX官方公告](https://www.okx.com/zh-hans/help/okx-to-execute-rebase-on-spacexusdt-pre-market-perpetual-futures-and-rename)。

本项目保留原始行情，**不会悄悄复权或拼接Rebase前后的原始价格**。后续任何跨该日期的收益回测都需要独立进行合约参数、价格、数量与交易费复权，或仅使用Rebase后的可比历史。

## 手动修复及验证

在GitHub Actions选取 `OKX phase1 history backfill and instrument specs`，点击 `Run workflow`。
- `auto`：还没有索引时完整回填，否则增量回填最近几天。
- `full`：按目标范围重新回填/按时间戳合并，适合修复过去的数据缺口。
- `incremental`：仅获取最近几天，适合日常维护。

查看 `data/history/index.json`、`data/quality/latest.json` 和工作流日志判断运行结果。使用 `python -m unittest discover -s tests -v` 做离线代码检查。

注意：GitHub Actions定时任务并非秒级实时流，可能延迟或漏执行。旧的价格警报与两小时研判仍首先读取 `data/latest.json`，严格检查时效。任何交易所实时强平预警仍应优先使用OKX自身风险系统。
