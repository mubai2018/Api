#!/usr/bin/env python3
"""Read-only, timestamp-validated OKX public perpetual market snapshot."""
import datetime
import json
import pathlib
import time
import urllib.parse
import urllib.request

BASES = ["https://openapi.okx.com", "https://www.okx.com"]
SYMBOLS = ["SPCX-USDT-SWAP", "BTC-USDT-SWAP", "ETH-USDT-SWAP"]

def fetch(path):
    errors = []
    for base in BASES:
        try:
            req = urllib.request.Request(base + path, headers={
                "Accept": "application/json", "User-Agent": "okx-public-monitor/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                obj = json.load(resp)
            if obj.get("code") != "0" or not obj.get("data"):
                raise ValueError("OKX invalid code/data: " + str(obj.get("code")))
            return obj["data"], base
        except Exception as exc:
            errors.append(f"{base}: {type(exc).__name__}: {str(exc)[:100]}")
    raise RuntimeError(" / ".join(errors))

def check_ts(value, now, age=150):
    ms = int(value)
    if ms < now - 1000*age or ms > now + 10000:
        raise ValueError(f"Stale or future market timestamp: {ms}")
    return ms

def quote(symbol, now):
    qs = urllib.parse.urlencode({"instId": symbol})
    data, host = fetch("/api/v5/market/ticker?" + qs)
    t = data[0]
    if t.get("instId") != symbol or t.get("instType") != "SWAP":
        raise ValueError("ticker instrument mismatch")
    ts = check_ts(t["ts"], now)
    qsm = urllib.parse.urlencode({"instType": "SWAP", "instId": symbol})
    data, mark_host = fetch("/api/v5/public/mark-price?" + qsm)
    m = data[0]
    if m.get("instId") != symbol or m.get("instType") != "SWAP":
        raise ValueError("mark instrument mismatch")
    mt = check_ts(m["ts"], now)
    last, mark = float(t["last"]), float(m["markPx"])
    if last <= 0 or mark <= 0:
        raise ValueError("invalid quote")
    return {"instId": symbol, "last": last, "markPx": mark,
            "lastTs": ts, "markTs": mt,
            "open24h": float(t["open24h"]),
            "high24h": float(t["high24h"]), "low24h": float(t["low24h"]),
            "tickerEndpoint": host, "markEndpoint": mark_host}

def candles(symbol, bar, limit):
    params = urllib.parse.urlencode({"instId": symbol, "bar": bar, "limit": limit})
    rows, _ = fetch("/api/v5/market/candles?" + params)
    return [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]),
             "volume": float(c[5]), "confirmed": str(c[8]) == "1"}
            for c in rows]  # newest first

def rolling_60m(symbol, now):
    bars = candles(symbol, "1m", 75)
    done = sorted((b for b in bars if b["confirmed"] and
                  now - 3600000 <= b["ts"] < now), key=lambda b: b["ts"])
    if len(done) < 56 or now - done[-1]["ts"] > 180000 or now - done[0]["ts"] < 3300000:
        raise ValueError("Not enough recent closed 1m candles")
    if any(b["ts"] - a["ts"] > 120000 for a, b in zip(done, done[1:])):
        raise ValueError("1m candle gaps")
    closes = [b["close"] for b in done]
    peak, drawdown = closes[0], 0.0
    for px in closes:
        peak = max(peak, px)
        drawdown = min(drawdown, 100*(px/peak - 1))
    return {"method": "confirmed 1m close-to-close",
            "fromTs": done[0]["ts"], "toTs": done[-1]["ts"],
            "firstClose": closes[0], "lastClose": closes[-1],
            "netReturnPct": round(100*(closes[-1]/closes[0] - 1), 4),
            "maxCloseDrawdownPct": round(drawdown, 4), "points": len(closes)}

def extras(symbol, now, errors):
    out = {}
    for bar, name in [("1H", "candles1H"), ("4H", "candles4H"), ("1D", "candles1D")]:
        try:
            out[name] = candles(symbol, bar, 13)
        except Exception as exc:
            errors.append(f"{symbol} {bar}: {str(exc)[:150]}")
    if symbol == "SPCX-USDT-SWAP":
        try:
            out["rolling60m"] = rolling_60m(symbol, now)
        except Exception as exc:
            errors.append(f"{symbol} rolling60m: {str(exc)[:150]}")
    try:
        f, _ = fetch("/api/v5/public/funding-rate?" +
                     urllib.parse.urlencode({"instId": symbol}))
        if f[0].get("instId") == symbol:
            out["fundingRate"] = float(f[0]["fundingRate"])
            out["nextFundingTime"] = int(f[0].get("nextFundingTime") or 0)
    except Exception as exc:
        errors.append(f"{symbol} funding: {str(exc)[:150]}")
    try:
        oi, _ = fetch("/api/v5/public/open-interest?" +
                      urllib.parse.urlencode({"instType": "SWAP", "instId": symbol}))
        if oi[0].get("instId") == symbol:
            check_ts(oi[0]["ts"], now, 300)
            out["openInterestUsd"] = float(oi[0]["oiUsd"])
            out["openInterestTs"] = int(oi[0]["ts"])
    except Exception as exc:
        errors.append(f"{symbol} open-interest: {str(exc)[:150]}")
    return out

def main():
    start = int(time.time()*1000)
    doc = {"checkedAt": None, "ok": False, "source": "OKX official public REST API",
           "symbols": {}, "supplementalErrors": []}
    for symbol in SYMBOLS:
        doc["symbols"][symbol] = quote(symbol, start)
    for symbol in SYMBOLS:
        doc["symbols"][symbol].update(extras(symbol, start, doc["supplementalErrors"]))
    now = int(time.time()*1000)
    for row in doc["symbols"].values():
        check_ts(row["lastTs"], now, 180)
        check_ts(row["markTs"], now, 180)
    doc["checkedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    doc["ok"] = True
    outpath = pathlib.Path("data/latest.json")
    outpath.parent.mkdir(parents=True, exist_ok=True)
    outpath.write_text(json.dumps(doc, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"ok": True, "checkedAt": doc["checkedAt"],
                      "quotes": {k: v["last"] for k, v in doc["symbols"].items()},
                      "supplementalErrors": doc["supplementalErrors"]}, ensure_ascii=False))

if __name__ == "__main__":
    main()
