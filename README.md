# OKX Public Market Monitor

This repository runs a **read-only public market data collector** for `SPCX-USDT-SWAP`, `BTC-USDT-SWAP`, and `ETH-USDT-SWAP` using official OKX API endpoints. It never uses exchange account credentials.

## Files

- `.github/workflows/okx.yml` — GitHub Actions schedule every ~5 minutes (best effort) and manual dispatch.
- `scripts/collect.py` — fetches public ticker and mark price with instrument and millisecond timestamp validation.
- `data/latest.json` — only published after every instrument successfully passes freshness checks.

## Verify

Open GitHub **Actions** → **OKX public market snapshot** → **Run workflow**. Confirm a successful run and `data/latest.json` with `"ok": true`. Read it at:

https://raw.githubusercontent.com/mubai2018/Api/main/data/latest.json

Consumers must independently validate `checkedAt` and individual `lastTs`/`markTs`. Old persisted snapshots are **not** live quotes. A failed job preserves the previous snapshot, which must then be considered stale.

Note: GitHub Actions cron scheduling is not guaranteed to execute precisely on time. This is approximately five-minute **sampling**, not a real-time tick stream or liquidation-safety system. Use OKX exchange alerts for immediate risk notification.
