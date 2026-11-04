#!/usr/bin/env python3
"""Phase 1 OKX public-only archival, instrument metadata and data-quality checks.

Usage: python scripts/phase1.py snapshot
       python scripts/phase1.py backfill [auto|full|incremental]
No external dependencies and no private APIs.
"""
import datetime as dt
import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

ROOT = pathlib.Path("data")
SYMBOLS = ("SPCX-USDT-SWAP", "BTC-USDT-SWAP", "ETH-USDT-SWAP")
BASES = ("https://openapi.okx.com", "https://www.okx.com")
UTC = dt.timezone.utc
SIZES = {"1m": 60_000, "5m": 300_000, "1H": 3_600_000,
         "4H": 14_400_000, "1D": 86_400_000}
# Higher-frequency history is prioritized for SPCX, while BTC/ETH provide context.
PLAN = {
    "SPCX-USDT-SWAP": {"1m": 7, "5m": 30, "1H": 90, "4H": 180, "1D": 365},
    "BTC-USDT-SWAP": {"1H": 90, "4H": 180, "1D": 365},
    "ETH-USDT-SWAP": {"1H": 90, "4H": 180, "1D": 365},
}
REBASE_UTC = "2026-06-02T07:10:00Z"
MAX_PAGES = 180  # fail-safe per series, not an assertion of complete coverage

def utcnow():
    return dt.datetime.now(UTC)

def iso():
    return utcnow().isoformat().replace("+00:00", "Z")

def msnow():
    return int(time.time()*1000)

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)

def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def jsonl_rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

def append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")

def call(path):
    failures = []
    for base in BASES:
        for attempt in range(2):
            try:
                req = urllib.request.Request(base + path, headers={
                    "Accept": "application/json", "User-Agent": "okx-public-phase1/1.0"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    response = json.load(resp)
                if response.get("code") != "0" or not isinstance(response.get("data"), list):
                    raise ValueError("invalid OKX code/data")
                return response["data"]
            except Exception as exc:
                failures.append(type(exc).__name__ + ": " + str(exc)[:100])
                if attempt == 0:
                    time.sleep(.35)
    raise RuntimeError("official API unavailable: " + "; ".join(failures[-2:]))

def path_with_params(endpoint, **params):
    return endpoint + "?" + urllib.parse.urlencode(params)

def dt_from_ms(stamp):
    return dt.datetime.fromtimestamp(stamp/1000, UTC)

def snapshot():
    src = read_json(ROOT/"latest.json")
    if not src or src.get("ok") is not True:
        raise ValueError("latest.json not OK")
    checked = dt.datetime.fromisoformat(src["checkedAt"].replace("Z", "+00:00"))
    delay = (utcnow() - checked).total_seconds()
    if delay < -10 or delay > 180:
        raise ValueError("snapshot checkedAt too old or future: %.1fs" % delay)
    warnings = []
    record = {"ts": int(checked.timestamp()*1000), "symbols": {}}
    per_symbol = {}
    for symbol in SYMBOLS:
        s = src["symbols"].get(symbol)
        if not s or s.get("instId") != symbol:
            raise ValueError("missing/wrong instrument: " + symbol)
        price = float(s["last"])
        mark = float(s["markPx"])
        if not (math.isfinite(price) and math.isfinite(mark) and price > 0 and mark > 0):
            raise ValueError("invalid price " + symbol)
        tlag = record["ts"] - int(s["lastTs"])
        mlag = record["ts"] - int(s["markTs"])
        if max(tlag, mlag) > 180_000 or min(tlag, mlag) < -10_000:
            raise ValueError("stale ticker or mark: " + symbol)
        row = {"last": price, "markPx": mark, "lastTs": int(s["lastTs"]),
               "markTs": int(s["markTs"])}
        for k in ("fundingRate", "nextFundingTime", "openInterestUsd", "openInterestTs"):
            if k in s:
                row[k] = s[k]
            elif k in ("fundingRate", "openInterestUsd"):
                warnings.append(symbol + " missing " + k)
        if symbol == "SPCX-USDT-SWAP" and "rolling60m" not in s:
            warnings.append("SPCX rolling60m unavailable")
        record["symbols"][symbol] = row
        per_symbol[symbol] = {"quoteAgeSeconds": round(max(tlag, mlag)/1000, 2),
                              "hasFunding": "fundingRate" in row,
                              "hasOpenInterest": "openInterestUsd" in row}
    date = checked.strftime("%Y-%m-%d")
    daily = ROOT/"history"/"market"/(date + ".jsonl")
    previous = jsonl_rows(daily)
    if previous:
        last_ts = int(previous[-1]["ts"])
        if record["ts"] > last_ts + 900_000:
            warnings.append("market sampling gap %.1f min" % ((record["ts"]-last_ts)/60_000))
        if record["ts"] <= last_ts:
            warnings.append("duplicate/out-of-order snapshot skipped")
        else:
            append_jsonl(daily, record)
    else:
        append_jsonl(daily, record)
    for err in src.get("supplementalErrors", []):
        warnings.append("supplemental: " + str(err)[:180])
    meta = read_json(ROOT/"metadata"/"instruments.json", {})
    if not meta:
        warnings.append("instrument metadata not yet available")
    health = {"generatedAt": iso(), "sourceCheckedAt": src["checkedAt"],
              "status": "ok" if not warnings else "degraded",
              "marketHistoryFile": str(daily), "marketSamplesToday": len(jsonl_rows(daily)),
              "symbols": per_symbol, "warnings": warnings[:30],
              "note": "GitHub polling is best effort; stale files are not current data"}
    write_json(ROOT/"quality"/"latest.json", health)
    print("PHASE1_SNAPSHOT " + json.dumps({"status": health["status"],
          "samples": health["marketSamplesToday"], "warnings": len(warnings)}))

def parse_candle(c, bar, upper):
    if len(c) < 9 or str(c[8]) != "1":
        return None
    ts = int(c[0])
    if ts >= upper or ts <= 0:
        return None
    o, h, l, cl, vol = [float(c[i]) for i in range(1, 6)]
    if not all(math.isfinite(v) for v in (o, h, l, cl, vol)):
        raise ValueError("non-finite candle")
    if min(o, h, l, cl) <= 0 or vol < 0 or h < max(o, cl) or l > min(o, cl) or h < l:
        raise ValueError("invalid OHLCV")
    if (ts - (16 * 3_600_000 if bar == "1D" else 0)) % SIZES[bar] != 0:
        raise ValueError("unaligned candle timestamp")
    return [ts, o, h, l, cl, vol]

def fetch_history(symbol, bar, days, mode):
    now = msnow()
    horizon = now - int((days if mode == "full" else 4) * 86_400_000)
    endpoint = "/api/v5/market/history-candles"
    cursor = None
    out = {}
    pages = 0
    while pages < MAX_PAGES:
        kwargs = {"instId": symbol, "bar": bar, "limit": 100}
        if cursor is not None:
            kwargs["after"] = cursor  # OKX: older than supplied timestamp
        data = call(path_with_params(endpoint, **kwargs))
        if not data:
            break
        stamps = [int(c[0]) for c in data]
        oldest = min(stamps)
        for c in data:
            row = parse_candle(c, bar, now)
            if row and row[0] >= horizon:
                out[row[0]] = row
        pages += 1
        if oldest <= horizon or len(data) < 100:
            break
        if cursor is not None and oldest >= cursor:
            raise ValueError("history pagination did not advance")
        cursor = oldest
        time.sleep(.24)  # conservative within IP rate limits
    if pages == MAX_PAGES:
        raise ValueError("history page safety cap hit for %s %s" % (symbol, bar))
    return sorted(out.values()), pages

def fetch_funding(symbol, mode):
    now = msnow()
    horizon = now - (90 if mode == "full" else 7)*86_400_000
    cursor = None
    out = {}
    pages = 0
    while pages < MAX_PAGES:
        kwargs = {"instId": symbol, "limit": 100}
        if cursor is not None:
            kwargs["after"] = cursor
        data = call(path_with_params("/api/v5/public/funding-rate-history", **kwargs))
        if not data:
            break
        stamps = [int(r["fundingTime"]) for r in data]
        oldest = min(stamps)
        for x in data:
            if x.get("instId") != symbol:
                raise ValueError("wrong funding instrument")
            stamp = int(x["fundingTime"])
            realized = float(x["realizedRate"])
            indicated = float(x["fundingRate"])
            if not (math.isfinite(realized) and math.isfinite(indicated)):
                raise ValueError("non-finite history funding rate")
            if stamp >= horizon and stamp <= now:
                out[stamp] = [stamp, realized, indicated]
        pages += 1
        if oldest <= horizon or len(data) < 100:
            break
        if cursor is not None and oldest >= cursor:
            raise ValueError("funding pagination did not advance")
        cursor = oldest
        time.sleep(.24)
    if pages == MAX_PAGES:
        raise ValueError("funding pagination safety cap")
    return sorted(out.values()), pages

def persist_months(prefix, rows):
    months = defaultdict(list)
    for row in rows:
        months[dt_from_ms(row[0]).strftime("%Y-%m")].append(row)
    for month, added in months.items():
        file = prefix/(month + ".jsonl")
        existing = {int(row[0]): row for row in jsonl_rows(file)}
        existing.update({int(row[0]): row for row in added})
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("".join(json.dumps(existing[k], separators=(",", ":")) + "\n"
                          for k in sorted(existing)), encoding="utf-8")

def stats(prefix, period):
    files = sorted(prefix.glob("????-??.jsonl"))
    if not files:
        return {"count": 0, "firstTs": None, "lastTs": None, "gapCount": None}
    total = 0
    first = last = None
    gap_count = 0
    for file in files:
        rows = jsonl_rows(file)
        for row in rows:
            stamp = int(row[0])
            if last is not None and period and stamp - last > period:
                gap_count += 1  # gap intervals, NOT number of absent rows
            first = stamp if first is None else min(first, stamp)
            last = stamp
            total += 1
    return {"count": total, "firstTs": first, "lastTs": last,
            "gapCount": gap_count, "fileCount": len(files)}

def instrument_meta():
    old = read_json(ROOT/"metadata"/"instruments.json", {})
    old_rows = old.get("symbols", {})
    rows = {}
    for symbol in SYMBOLS:
        data = call(path_with_params("/api/v5/public/instruments", instType="SWAP", instId=symbol))
        ins = next((i for i in data if i.get("instId") == symbol), None)
        if not ins:
            raise ValueError("missing instrument metadata " + symbol)
        fields = ("instId", "instFamily", "state", "ctVal", "ctMult",
                  "ctValCcy", "ctType", "tickSz", "lotSz", "minSz",
                  "lever", "listTime")
        rows[symbol] = {k: ins.get(k) for k in fields}
        changed = [k for k in ("ctVal", "ctMult", "ctValCcy", "tickSz", "lotSz",
                              "minSz", "state") if old_rows.get(symbol) and
                   old_rows[symbol].get(k) != rows[symbol].get(k)]
        if changed:
            append_jsonl(ROOT/"metadata"/"instrument_changes.jsonl",
                         {"at": iso(), "symbol": symbol, "changed": changed,
                          "old": old_rows[symbol], "new": rows[symbol]})
    write_json(ROOT/"metadata"/"instruments.json", {
        "checkedAt": iso(), "source": "OKX GET /api/v5/public/instruments",
        "symbols": rows, "spcxHistoricalRebase": {
            "effectiveUtc": REBASE_UTC, "preRebaseSymbol": "SPACEXUSDT",
            "toRatio": 12.52,
            "rule": "DO NOT join raw pre/post rebase prices for unadjusted backtests",
            "source": "OKX notice published 2026-06-01"}})

def backfill(mode):
    if mode not in ("auto", "full", "incremental"):
        raise ValueError("mode must be auto, full or incremental")
    if mode == "auto":
        mode = "incremental" if (ROOT/"history"/"index.json").exists() else "full"
    warnings = []
    index = {"checkedAt": iso(), "mode": mode, "source": "OKX official history-candles and funding-rate-history",
             "candles": {}, "funding": {}, "warnings": [],
             "rebase": {"symbol": "SPCX-USDT-SWAP", "effectiveUtc": REBASE_UTC,
                        "toRatio": 12.52, "policy": "raw data preserved; pre-rebase not merged without adjustment"}}
    try:
        instrument_meta()
    except Exception as exc:
        warnings.append("instrument metadata: " + str(exc)[:220])
    critical_ok = False
    for symbol, plans in PLAN.items():
        for bar, days in plans.items():
            key = symbol + "/" + bar
            try:
                rows, pages = fetch_history(symbol, bar, days, mode)
                if not rows:
                    raise ValueError("no confirmed candles")
                prefix = ROOT/"history"/"candles"/symbol/bar
                persist_months(prefix, rows)
                st = stats(prefix, SIZES[bar])
                st.update({"targetDays": days, "fetchedRows": len(rows), "requestPages": pages})
                index["candles"][key] = st
                if key == "SPCX-USDT-SWAP/1H" and st["count"] >= 10:
                    critical_ok = True
                print("ARCHIVED", key, st["count"], "rows", pages, "pages")
            except Exception as exc:
                warnings.append(key + ": " + str(exc)[:220])
                print("BACKFILL_WARNING", key, str(exc)[:120])
        try:
            rows, pages = fetch_funding(symbol, mode)
            if not rows:
                raise ValueError("no funding history")
            prefix = ROOT/"history"/"funding"/symbol
            persist_months(prefix, rows)
            st = stats(prefix, None)
            st.update({"fetchedRows": len(rows), "requestPages": pages})
            index["funding"][symbol] = st
            print("ARCHIVED funding", symbol, st["count"])
        except Exception as exc:
            warnings.append(symbol + " funding: " + str(exc)[:220])
    index["checkedAt"] = iso()
    index["warnings"] = warnings
    index["status"] = "ok" if not warnings else "degraded"
    write_json(ROOT/"history"/"index.json", index)
    if not critical_ok:
        raise RuntimeError("SPCX 1H historical candles unavailable: critical data check failed")
    print("PHASE1_BACKFILL " + json.dumps({
        "mode": mode, "series": len(index["candles"]),
        "funding": len(index["funding"]), "warnings": len(warnings)}))

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if command == "snapshot":
        snapshot()
    elif command == "backfill":
        backfill(sys.argv[2] if len(sys.argv) > 2 else "auto")
    else:
        raise SystemExit("Usage: phase1.py [snapshot|backfill [auto|full|incremental]]")
