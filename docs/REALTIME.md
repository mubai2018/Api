# SPCX连续公开行情服务（待外部平台部署）

此模块与GitHub Actions定时采集分开。`services/okx_live.py` 运行在**常驻容器**中，订阅OKX官方 `tickers` 与 `mark-price` 公共WebSocket频道。

### 重要接口变更

OKX在2026-10-31停用公共WebSocket的8443端口。使用 `wss://ws.okx.com/ws/v5/public`（443），而不是 `:8443`。备用REST为 `https://openapi.okx.com`，仅在WebSocket行情过期时每约15秒尝试。

### 部署

- Dockerfile已放在仓库根目录。可通过Railway等常驻容器平台连接此GitHub仓库、导入该Dockerfile构建。
- 环境变量 `PORT` 使用平台自动分配值，`OKX_WS_URL` 可选。无需OKX私人API密钥。
- 容器启动后公开 `GET /health` 和 `GET /latest`。后者只在最新成交价、标记价各自数据时间戳距服务器当前时间不超过30秒时返回HTTP 200，否则返回503。不会返回过期行情冒充实时报价。
- `/latest` 只包含**官方公开行情**。不包含个人持仓、强平价、交易账户或API密钥。
- `activeEvents` 和 `recentTransitions` 只在本进程内存维护；云容器重启后状态重新开始，生产级去重/持久通知需外部持久存储。**当前版本不向手机直接推送警报，也没有Telegram集成**。
- `OBSERVED_60M_FALL_5PCT` 需进程积累超过55分钟样本才会生成，是观察到的Ticker更新净变动，不是1分钟K线或tick级精确最大回撤。
- 如果主机没有真实常驻运行（例如自动休眠），本服务依然无法保证持续行情。选择支持常驻服务的付费/免费套餐并核对成本和服务保活行为。

### 实测状态

代码已在GitHub仓库准备，仍需要外部常驻主机部署并验证 `/health`、`/latest` 返回时间戳。**GitHub Actions的短时测试不能证明常驻服务长期在线**。每小时ChatGPT任务只能读取已发布的链接，不能作为秒级消费端。

### 代码验证

`python -m py_compile services/okx_live.py`

可在支持外网的环境中使用 `python services/okx_live.py` 启动，访问本地 `http://127.0.0.1:$PORT/health`，再观察连接状态以及 `/latest`。
