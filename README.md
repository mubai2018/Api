# OKX Public Market Monitor

Public, read-only OKX market snapshots for **SPCX-USDT-SWAP**, **BTC-USDT-SWAP** and **ETH-USDT-SWAP**. No OKX API key, private data, or trading actions.

**Latest snapshot:** https://raw.githubusercontent.com/mubai2018/Api/main/data/latest.json

**Workflows:** https://github.com/mubai2018/Api/actions/workflows/okx.yml

## Live SPCX quote source (Railway, primary)

- **Real-time quote**: https://spcx-live-feed-production.up.railway.app/latest
- **Source freshness/health**: https://spcx-live-feed-production.up.railway.app/health
- **In-memory threshold transitions**: https://spcx-live-feed-production.up.railway.app/events
- Railway project `spcx-live-monitor`, function service `spcx-live-feed` (public OKX WS over port 443, REST fallback). No OKX private keys.
- **Verified live** 2026-11-05 02:43:42 KST: WS ticker 137.68, mark 137.68 USDT, source ages 228ms/137ms; 45 websocket messages and 0 reconnects; HTTP health 200.
- Railway is designed as the primary read-only price source; GitHub Actions below remains the periodic market archive and historical data source. GitHub has not yet demonstrated successful `schedule` trigger runs. These are distinct components.
- Live service holds its recent threshold events and sampled tick history **in memory**; they reset upon restart. No instant phone notification channel has been deployed. An HTTP 200 is not enough: consumers must inspect `healthy`, `last.ts`, `mark.ts` and `ageMs` and reject stale data.
- Real-time service may incur Railway usage fees; inspect Railway billing/usage in your account. This does not access the trading account and does not trade.

## Collection

- GitHub Actions polls OKX official `openapi.okx.com` approximately every five minutes, subject to GitHub scheduling delays and dropped jobs.
- For each contract it collects `last`, `markPx`, `lastTs`, `markTs`, 24h open/high/low, and the latest 13 candles at 1H, 4H, and 1D.
- It also tries to collect funding rate, open interest (USD), and for SPCX a rolling ~60-minute analysis based on confirmed 1-minute candle closes.
- Supplemental indicators may be missing. An error is described in `supplementalErrors` instead of guessed.
- The snapshot is published only after all three quotes and mark prices pass source timestamp and instrument checks. If collection fails the workflow fails and the previously published snapshot remains on GitHub, possibly **stale**.

## Safe reading

Before using a snapshot as fresh: require `ok == true`, UTC `checkedAt` within **10 minutes** of now, and each symbol's `lastTs` and `markTs` within 10 minutes. Do not interpret a stale file as current just because the HTTP request succeeded.

`rolling60m.netReturnPct`: percentage change between first and last confirmed 1m closes in the sampled window.

`rolling60m.maxCloseDrawdownPct`: worst peak-to-later-trough move **among confirmed 1m closing prices** in that window. Neither is tick-level drawdown.

## Phase 1 — historical data (verified)

- [Historical index](data/history/index.json): 11 candle series for SPCX/BTC/ETH, plus settled funding history and instrument specifications.
- [Independent acceptance](data/quality/acceptance.json): latest run status and row-level checks.
- [Chinese phase1 documentation](docs/PHASE1.md).
- Historical SPCX data crosses the June 2026 contract rebase. Never combine pre/post-rebase raw values as though directly comparable.

## Phase 3 — public market risk monitoring (enabled)

- [SPCX risk snapshot](data/risk/latest.json) — order-book spread/visible liquidity, latest up to 100 public trades, 5/15/60m confirmed-close moves, funding, open interest and threshold signals.
- [Pipeline heartbeat](data/quality/pipeline.json) — last attempted collection job outcomes, including failures.
- [Independent watchdog](data/quality/watchdog.json) — 10/20-minute source freshness checks and actual GitHub schedule-run evidence.
- [Unique risk event lifecycle](data/risk/alert_state.json) — opened/escalated/recovered events, with transitions archived under `data/risk/alerts/`.
- [Reliability and deduplication runbook](docs/RELIABILITY.md) — independent watchdog, fault recovery, notification dedup and remaining scheduler validation.
- [Chinese phase3 documentation](docs/PHASE3.md).

Price threshold signals use only fresh validated data. GitHub Actions samples approximately every 5 minutes, while ChatGPT user-facing risk alerts are checked hourly; neither is real-time tick monitoring. There is no personal account, balance, liquidation price, or private OKX API key.

## Risk limits

A periodic GitHub Actions job is not a continuous live feed or a substitute for exchange liquidation alerts. Market predictions remain uncertain; no user account or trading positions are monitored by this public-only collector.
