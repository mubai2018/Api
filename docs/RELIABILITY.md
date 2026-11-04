# SPCX 监控可靠性、告警去重和恢复

本项目只采集 **OKX官方公开SPCX-USDT-SWAP行情**，不需要账户API密钥、不读取个人仓位、不执行交易。第二阶段网格数量研究已暂停。

## 可靠性架构

1. 主采集器 `.github/workflows/okx.yml`：GitHub Actions计划每5分钟触发，提交 `data/latest.json`、`data/risk/latest.json`、`data/quality/pipeline.json`。GitHub计划任务不是持续运行保证。
2. 独立看门狗 `.github/workflows/watchdog.yml`：另一条GitHub Actions计划每15分钟检查已有行情、风险和采集心跳，写入 `data/quality/watchdog.json`、`data/quality/watchdog_state.json`；如果采集缺失超过20分钟，记录一次故障open；连续两次健康看门狗检查才记录recovered。状态变迁留在 `data/quality/outage_events/YYYY-MM-DD.jsonl`。
3. 看门狗用GitHub自身Actions REST API查询 **真正由 `schedule` 事件触发** 的主采集运行记录，写入 `watchdog.schedule`。由`push`手动触发成功不等于`schedule`触发成功，`not_yet_observed`表示尚未验证。
4. **独立ChatGPT每小时巡检**：继续使用已启用的 `SPCX风险监测` 自动任务；会检查所有源时间戳及故障记录。此通道比GitHub定时看门狗独立，但仅每小时一次，不能替代秒级交易预警。
5. `data/quality/notification_cursor.json` 储存用户侧通知处理的事件ID。ChatGPT巡检实际通知后才尝试标记已处理，降低重复通知；写入若失败不可宣称成功。

## 市场风险生命周期

`scripts/alert_lifecycle.py` 对已经由 `scripts/phase3.py` 验证的风险事件再次执行来源校验，写入：
- `data/risk/alert_state.json`：active、pending、lastTransitions 和最后处理的源快照时间戳；
- `data/risk/alerts/YYYY-MM-DD.jsonl`：**仅**记录新事件opened、同事件severity升级escalated、恢复recovered；不将持续事件每轮都计为新通知。

事件规则：
- 高风险或严重风险：首次验证触发即可opened。
- 中风险：必须由**两个不同的有效快照**连续确认才opened。
- 恢复：必须连续两个有效采集快照确认事件消失；如果本次盘口/资金费/OI等支撑数据缺失，则不误判为恢复。
- 相同已处理 `risk.checkedAt` 不重复计算，事件ID含源时间、代码、动作。
- 从事件记录中可以看到过去发生的短期风险，但事件已恢复时不得说当前仍触发。

## 数据故障分级

| 状态 | 判定 | 处理 |
|---|---|---|
| healthy | 行情、风险、采集心跳都在10分钟内，且最后一个pipeline成功 | 无故障 |
| stale | 最大源年龄10–20分钟 | 数据过期，不冒充实时 |
| outage | 超过20分钟、来源错误或最近一次采集失败 | 记录一次数据故障opened |
| recovered | outage后连续2次独立看门狗检查healthy | 记录recovered |

**重要限制：** 如果GitHub整个调度系统不运行，看门狗也可能不运行。此时 `data/quality/watchdog.json` 本身会过期，应由独立的ChatGPT小时任务检测。缺乏定时运行证据不能靠修改配置文件伪造通过。

## 验收标准

1. 主采集工作流成功运行，`data/risk/alert_state.json` 真实生成、心跳步骤新增 `lifecycle=success`。
2. 看门狗通过独立流程执行，输出sourceAgeMinutes、schedule evidence，并正确区分新鲜数据和过期数据。
3. 自动化单元测试覆盖风险首次触发、升级、2次采集恢复、防重复，以及源数据过期、采集故障、2次看门狗恢复等异常路径。
4. 至少观察**连续24小时** GitHub `schedule` 实际运行、成功率、间隔和缺失情况后，才能宣称长期自动调度稳定。一次由push触发的成功不能证明这一点。
5. 无第三方即时推送集成，ChatGPT风险通知仍只有每小时一次；如需更低延时，需采用常驻服务与显式配置通知渠道。

所有风险信号只是公开市场信息，不能用于推算个人账户精确强平价。
