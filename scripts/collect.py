#!/usr/bin/env python3
"""Read-only OKX public swaps market data. No authentication and no trading."""
import datetime
import json
import pathlib
import time
import urllib.parse
import urllib.request

BASES = ["https://openapi.okx.com", "https://www.okx.com"]
SYMBOLS = ["SPCX-USDT-SWAP", "BTC-USDT-SWAP", "ETH-USDT-SWAP"]
OUT = pathlib.Path("data/latest.json")

def fetch(path):
    errors = []
    for base in BASES:
        try:
            req = urllib.request.Request(
                base + path,
                headers={"User-Agent": "okx-public-market-collector/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                obj = json.load(resp)
            if obj.get("code") != "0" or not obj.get("data"):
                raise ValueError("OKX invalid result code/data: " + str(obj.get("code")))
            return obj["data"], base
        except Exception as exc:
            errors.append(f"{base}: {type(exc).__name__}: {str(exc)[:120]}")
    raise RuntimeError("; ".join(errors))

def fresh_ms(value, now_ms, max_age_s=150):
    stamp = int(value)
    lag = now_ms - stamp
    if lag < -10_000 or lag > max_age_s * 1000:
        raise ValueError(f"OKX data timestamp stale or future (age_ms={lag})")
    return stamp

def get_symbol(symbol, now_ms):
    qs = urllib.parse.urlencode({"instId": symbol})
    ticker, ticker_host = fetch("/api/v5/market/ticker?" + qs)
    row = ticker[0]
    if row.get("instId") != symbol or row.get("instType") != "SWAP":
        raise ValueError("OKX ticker instrument mismatch")
    last_ts = fresh_ms(row["ts"], now_ms)
    last = float(row["last"])
    if last <= 0:
        raise ValueError("invalid last price")

    qsm = urllib.parse.urlencode({"instType": "SWAP", "instId": symbol})
    marks, mark_host = fetch("/api/v5/public/mark-price?" + qsm)
    mark = marks[0]
    if mark.get("instId") != symbol or mark.get("instType") != "SWAP":
        raise ValueError("OKX mark instrument mismatch")
    mark_ts = fresh_ms(mark["ts"], now_ms)
    mark_px = float(mark["markPx"])
    if mark_px <= 0:
        raise ValueError("invalid mark price")

    return {
        "instId": symbol,
        "last": last,
        "markPx": mark_px,
        "lastTs": last_ts,
        "markTs": mark_ts,
        "open24h": float(row["open24h"]),
        "high24h": float(row["high24h"]),
        "low24h": float(row["low24h"]),
        "tickerEndpoint": ticker_host,
        "markEndpoint": mark_host,
    }

def main():
    now_ms = int(time.time() * 1000)
    payload = {
        "checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ok": False,
        "source": "OKX official public REST API",
        "symbols": {},
    }
    errs = []
    for symbol in SYMBOLS:
        try:
            payload["symbols"][symbol] = get_symbol(symbol, now_ms)
        except Exception as exc:
            errs.append(f"{symbol}: {exc}")
    if errs:
        print("DATA_CHECK_FAILED: " + " | ".join(errs))
        raise SystemExit(1)
    payload["ok"] = True
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "checkedAt": payload["checkedAt"],
                      "symbols": {k: v["last"] for k, v in payload["symbols"].items()}}, ensure_ascii=False))

if __name__ == "__main__":
    main()
