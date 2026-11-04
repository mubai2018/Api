# OKX Public Market Monitor

Public, read-only OKX market snapshots for **SPCX-USDT-SWAP**, **BTC-USDT-SWAP** and **ETH-USDT-SWAP**. No OKX API key, private data, or trading actions.

**Latest snapshot:** https://raw.githubusercontent.com/mubai2018/Api/main/data/latest.json

**Workflows:** https://github.com/mubai2018/Api/actions/workflows/okx.yml

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

## Risk limits

A periodic GitHub Actions job is not a continuous live feed or a substitute for exchange liquidation alerts. Market predictions remain uncertain; no user account or trading positions are monitored by this public-only collector.
