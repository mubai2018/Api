# 第三阶段：SPCX公开市场风险监控

**范围：仅对公开市场风险提供监测，不读取个人仓位、不使用私人交易API密钥、不发送交易指令。第二阶段网格回测/参数优化不在本阶段范围内。**

## 数据链路

GitHub Actions `.github/workflows/okx.yml`（cron计划约每5分钟）：

1. `scripts/collect.py`：SPCX/BTC/ETH官方Ticker、标记价、K线、资金费率、未平仓量；SPCX近60分钟闭合1分钟K线衍生5/15/60分钟涨跌。
2. `scripts/phase1.py snapshot`：沿用第一阶段真实价格归档与数据质量检查。
3. `scripts/phase3.py`：OKX公开SPCX订单簿 `GET /api/v5/market/books?instId=SPCX-USDT-SWAP&sz=50` 和公开成交 `GET /api/v5/market/trades?instId=SPCX-USDT-SWAP&limit=100`，计算风险指标和只在**已校验数据**上成立的事件。
4. `scripts/pipeline_health.py`：即使采集失败，也写入该次执行情况，然后提交至仓库。
5. 缺少实时数据时保留旧文件，但任何消费者必须检查时间戳，不可将旧文件当作当前行情。

## 新增数据

- `data/risk/latest.json`：当前风险快照、事件列表、各源时间戳及未取得的辅助数据。
- `data/risk/history/YYYY-MM-DD.jsonl`：约5分钟一次的公开微观结构样本，含SPCX盘口、最近100笔成交统计、OI/资金费。
- `data/quality/pipeline.json`：最新工作流的采集心跳。失败会写 `degraded`，但**定时工作流没有运行时该文件本身也会过期**。
- `data/quality/latest.json`：第一阶段原有质量报告。

上述JSON中的 `ok=true` 仅表示**当次数据处理成功**，消费方仍需验证 `checkedAt`、`lastTs`、`markTs` 和微观数据的 `ts` 距当前≤10分钟。盘口和成交因采样高频变化，建议额外要求其自身时间戳在当次采集时≤120秒。

## 信号规则（可调整，但不会自动交易）

| 信号代码 | 校验条件 | 含义 |
|---|---|---|
| `PRICE_BELOW_120/110/100/90` | 成交价或标记价低于对应阈值 | 用户自定的公开价格风险阈值，不是强平价 |
| `FALL_5M` | 最近5分钟已确认1m收盘价回报≤-3% | 短线快速下跌 |
| `FALL_15M` | 最近15分钟已确认1m收盘价回报≤-5% | 15分钟快速下跌 |
| `FALL_60M` | 最近约60分钟已确认1m收盘价回报≤-5% | 1小时价格下跌 |
| `DRAWDOWN_60M` | 最近60分钟已确认1m收盘价的峰谷回撤≤-5% | 小时内阶段性急跌 |
| `MARK_DEVIATION` | 成交价与标记价差异绝对值≥1% | 指标异常/基差观察 |
| `SPREAD_WIDE` | 可见最佳买卖价差≥35bps | 盘口流动性风险 |
| `BID_DEPTH_COLLAPSE` | 在至少12个有效历史样本基础上，0.5%带宽可见买盘深度跌至24小时样本中位数的25%以下 | 可能流动性收缩 |
| `OI_1H_CHANGE` | 有45–90分钟前的有效样本，未平仓量USD变化绝对值≥15% | 市场杠杆结构快速变化 |
| `FUNDING_EXTREME` | 当期指示资金费率绝对值≥0.25% | 资金费异常风险 |

公开**主动成交**数据从最新最多100条计算，数据不是完整5分钟交易历史，不用于单独的确定性涨跌预警。买盘/卖盘深度仅是当次可见50档，包含订单可能瞬时撤销；盘口快照不代表真实可成交数量或未来方向。未平仓量变化也不能独立指示做多或做空。

## 告警边界

当前GitHub Actions只是**约5分钟轮询采集**；ChatGPT已有的风险检查任务每小时检查，不能构成秒级通知。紧急交易风险以欧易App价格提醒和自身风控为主。

没有行情数据、时间戳过期或辅助字段缺失时，不报告已触发该信号。 `pipeline.json` 表示“最后一次已记录执行结果”，不等于GitHub的cron从不漏执行。

采集器完全使用OKX无需授权的公开REST接口；不保存任何API key、口令、账号余额、网格实际仓位或个人强平价。

## 观测与验收

查看：
- [行情工作流](https://github.com/mubai2018/Api/actions/workflows/okx.yml)
- [风险快照](https://github.com/mubai2018/Api/blob/main/data/risk/latest.json)
- [流水线健康状态](https://github.com/mubai2018/Api/blob/main/data/quality/pipeline.json)

验收至少要求：GitHub Actions最新版本成功执行；有时间戳新鲜的 `data/risk/latest.json`；盘口与成交（如果OKX支持且访问成功）数据单位与合约规格一致；风险规则单元测试通过；存档中无重复或非法记录。历史基线型预警须积累至少12个有效快照才能启用，不用凭空伪造基准。
