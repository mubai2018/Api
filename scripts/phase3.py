#!/usr/bin/env python3
"""Phase 3: read-only official OKX SPCX risk intelligence; 5-minute sampling, not streaming.

Input: validated data/latest.json, public OKX order book & trades, contract metadata.
Output: data/risk/latest.json, data/risk/history/YYYY-MM-DD.jsonl.
No account access, trade actions, user secrets, or Telegram tokens.
"""
import datetime as dt
import json
import math
import pathlib
import statistics
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path("data")
SYMBOL = "SPCX-USDT-SWAP"
BASES = ("https://openapi.okx.com", "https://www.okx.com")
THRESHOLDS = (120, 110, 100, 90)
UTC = dt.timezone.utc

def msnow():
    return int(time.time()*1000)

def load(path):
    return json.loads(path.read_text(encoding="utf-8"))

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def finite(x):
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite numeric field")
    return v

def fetch(endpoint, **params):
    suffix = endpoint + "?" + urllib.parse.urlencode(params)
    problems = []
    for base in BASES:
        try:
            req = urllib.request.Request(base+suffix, headers={
                "Accept": "application/json", "User-Agent": "okx-public-phase3/1.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.load(response)
            if data.get("code") != "0" or not data.get("data"):
                raise ValueError("OKX nonzero code or empty data")
            return data["data"]
        except Exception as exc:
            problems.append(type(exc).__name__ + " " + str(exc)[:130])
    raise RuntimeError("Official OKX API unavailable: " + " / ".join(problems))

def validate_stamp(stamp, now, limit=120_000):
    delta = now-int(stamp)
    if delta < -10_000 or delta > limit:
        raise ValueError("timestamp outside freshness bound (ms): " + str(delta))
    return int(stamp)

def read_inputs(now):
    market = load(ROOT/"latest.json")
    if market.get("ok") is not True or market.get("source") != "OKX official public REST API":
        raise ValueError("unverified official ticker snapshot")
    age = now-int(dt.datetime.fromisoformat(market["checkedAt"].replace("Z","+00:00")).timestamp()*1000)
    if age < -10_000 or age > 180_000:
        raise ValueError("ticker snapshot stale (ageMs=%d)"%age)
    q = market["symbols"][SYMBOL]
    if q.get("instId") != SYMBOL:
        raise ValueError("wrong SPCX instrument")
    validate_stamp(q["lastTs"],now,180_000)
    validate_stamp(q["markTs"],now,180_000)
    last,mark = finite(q["last"]),finite(q["markPx"])
    if min(last,mark)<=0:
        raise ValueError("nonpositive market price")
    meta = load(ROOT/"metadata"/"instruments.json")["symbols"][SYMBOL]
    if meta.get("instId") != SYMBOL or meta.get("state") != "live" or meta.get("ctValCcy") != "SPCX":
        raise ValueError("unexpected SPCX contract spec")
    ctval=finite(meta["ctVal"])
    if ctval<=0:
        raise ValueError("nonpositive contract value")
    return market,q,ctval

def book_metrics(data,ctval,now):
    b=data[0]
    stamp=validate_stamp(b["ts"],now,120_000)
    bids,asks=b["bids"],b["asks"]
    if len(bids)<2 or len(asks)<2:
        raise ValueError("insufficient book levels")
    def levels(rows):
        return [(finite(v[0]),finite(v[1])) for v in rows]
    bids=levels(bids);asks=levels(asks)
    if min(x[0] for x in bids+asks)<=0 or min(x[1] for x in bids+asks)<0:
        raise ValueError("invalid order book level")
    if any(bids[i][0]<bids[i+1][0] for i in range(len(bids)-1)) or any(asks[i][0]>asks[i+1][0] for i in range(len(asks)-1)):
        raise ValueError("book levels not correctly ordered")
    best_bid,best_ask=bids[0][0],asks[0][0]
    if best_ask<=best_bid:
        raise ValueError("crossed or locked orderbook")
    mid=(best_ask+best_bid)/2
    spread_bps=10_000*(best_ask-best_bid)/mid
    boundary=.005
    bid_usd=sum(p*sz*ctval for p,sz in bids if p>=mid*(1-boundary))
    ask_usd=sum(p*sz*ctval for p,sz in asks if p<=mid*(1+boundary))
    combined=bid_usd+ask_usd
    return {"ts":stamp,"levelsBid":len(bids),"levelsAsk":len(asks),
            "depthUnit":"USDT notional, 0.5% band, public visible book (50 levels/side)",
            "bestBid":best_bid,"bestAsk":best_ask,"spreadBps":round(spread_bps,3),
            "depthBidUsd05pct":round(bid_usd,2),"depthAskUsd05pct":round(ask_usd,2),
            "imbalance05pct":round((bid_usd-ask_usd)/combined,4) if combined>0 else None}

def trades_metrics(data,ctval,now):
    traded=[]
    seen=set()
    for x in data:
        if x.get("instId")!=SYMBOL or x.get("side") not in ("buy","sell"):
            raise ValueError("wrong trade symbol or aggressor side")
        stamp=int(x["ts"])
        if stamp>now+10_000:
            raise ValueError("future trade timestamp")
        if x.get("tradeId") in seen:
            continue
        seen.add(x.get("tradeId"))
        if now-stamp > 300_000:
            continue
        px,contracts=finite(x["px"]),finite(x["sz"])
        if px<=0 or contracts<=0:
            raise ValueError("invalid trade price or size")
        traded.append((stamp,x["side"],px*contracts*ctval))
    if not traded:
        raise ValueError("no recent public trades")
    most_recent=max(x[0] for x in traded)
    validate_stamp(most_recent,now,120_000)
    buy=sum(x[2] for x in traded if x[1]=="buy")
    sell=sum(x[2] for x in traded if x[1]=="sell")
    return {"lastTradeTs":most_recent,"firstTradeTs":min(x[0] for x in traded),
            "sampleCount":len(traded),"sampleLimit":100,
            "buyTakerNotionalUsd":round(buy,2),"sellTakerNotionalUsd":round(sell,2),
            "takerBuyRatio":round(buy/(buy+sell),4),
            "note":"Only last 100 public trades, NOT exhaustive 5-minute market order flow"}

def past_records(now):
    folder=ROOT/"risk"/"history"
    dates=[dt.datetime.fromtimestamp(now/1000,UTC).date() - dt.timedelta(days=d) for d in (1,0)]
    data=[]
    for date in dates:
        file=folder/(date.strftime("%Y-%m-%d")+".jsonl")
        if not file.exists(): continue
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                row=json.loads(line)
                if now-86_400_000<=row["ts"]<now:
                    data.append(row)
            except (ValueError,KeyError,TypeError):
                pass
    return sorted(data,key=lambda x:x["ts"])

def add_event(events,code,severity,message,value,unit,source_ts):
    events.append({"code":code,"severity":severity,"message":message,
                   "value":value,"unit":unit,"sourceTs":source_ts})

def evaluate(now,q,book,trades,past):
    events=[];context=[];last,mark=finite(q["last"]),finite(q["markPx"])
    for bound in THRESHOLDS:
        if min(last,mark) < bound:
            add_event(events,"PRICE_BELOW_%d"%bound,"critical" if bound<=100 else "high",
                      "SPCX trade/mark price below %d USDT"%bound,min(last,mark),"USDT",
                      min(int(q["lastTs"]),int(q["markTs"])))
    roll=q.get("rolling60m")
    if isinstance(roll,dict) and int(roll.get("points",0))>=56:
        try:
            validate_stamp(int(roll["toTs"])+60_000,now,240_000)
            for key,bound,code in [
                ("net5mPct",-3,"FALL_5M"),
                ("net15mPct",-5,"FALL_15M"),
                ("netReturnPct",-5,"FALL_60M"),
                ("maxCloseDrawdownPct",-5,"DRAWDOWN_60M")]:
                if key not in roll: continue
                move=finite(roll[key])
                if move<=bound:
                    add_event(events,code,"high",
                              "SPCX confirmed 1m-close metric: "+key,move,"%",int(roll["toTs"])+60_000)
        except (ValueError,TypeError,KeyError):
            context.append("Rolling 60-minute candle summary not fresh/complete")
    else:
        context.append("Rolling 60-minute candle summary missing")
    deviation=100*(last/mark-1)
    if abs(deviation)>=1:
        add_event(events,"MARK_DEVIATION","high","Last/mark deviation >=1%",round(deviation,3),"%",
                  min(int(q["lastTs"]),int(q["markTs"])))
    rate=q.get("fundingRate")
    if rate is not None and abs(finite(rate))>=.0025:
        # raw percentage shown to user: multiply by 100
        add_event(events,"FUNDING_EXTREME","medium",
                  "Current indicative funding rate absolute value >=0.25% per interval",
                  round(finite(rate)*100,4),"% (indicative)",now)
    if book is not None:
        if book["spreadBps"]>=35:
            add_event(events,"SPREAD_WIDE","medium","SPCX best bid/ask spread >=35 basis points",
                      book["spreadBps"],"bps",book["ts"])
        metric="depthBidUsd05pct"
        history=[r["book"][metric] for r in past if isinstance(r.get("book"),dict)
                 and r["book"].get(metric) is not None and now-int(r["ts"])<=86_400_000]
        if len(history)>=12:
            baseline=statistics.median(history)
            if baseline>0 and book[metric]<=baseline*.25:
                add_event(events,"BID_DEPTH_COLLAPSE","high",
                          "Visible bid depth within 0.5% fell >=75% vs median of previous 24h samples",
                          round(book[metric]/baseline,4),"ratio",book["ts"])
    else:
        context.append("Order book unavailable or stale; liquidity risks cannot be verified")
    if trades is None:
        context.append("Recent taker trades unavailable; buy/sell pressure cannot be verified")
    oi=q.get("openInterestUsd")
    if oi is not None and q.get("openInterestTs") is not None:
        oi_ts=int(q["openInterestTs"])
        try:
            validate_stamp(oi_ts,now,300_000)
            reference=[x for x in past if 45*60_000 <= now-int(x["ts"]) <= 90*60_000
                       and x.get("oiUsd") is not None and float(x["oiUsd"])>0]
            if reference:
                nearest=min(reference,key=lambda z:abs(now-int(z["ts"])-60*60_000))
                change=100*(finite(oi)/finite(nearest["oiUsd"])-1)
                if abs(change)>=15:
                    add_event(events,"OI_1H_CHANGE","medium",
                              "SPCX total open interest in USD changed >=15% versus ~1h prior sample",
                              round(change,3),"%",oi_ts)
        except (ValueError,TypeError):
            context.append("Open interest timestamp invalid/stale")
    return events,context

def main():
    now=msnow()
    market,q,ctval=read_inputs(now)
    warnings=[];book=trades=None
    try:
        book=book_metrics(fetch("/api/v5/market/books",instId=SYMBOL,sz=50),ctval,now)
    except Exception as exc:
        warnings.append("book: "+str(exc)[:220])
    try:
        trades=trades_metrics(fetch("/api/v5/market/trades",instId=SYMBOL,limit=100),ctval,now)
    except Exception as exc:
        warnings.append("trades: "+str(exc)[:220])
    past=past_records(now)
    events,context=evaluate(now,q,book,trades,past)
    doc={"schemaVersion":1,"ok":True,
         "checkedAt":dt.datetime.now(UTC).isoformat(),
         "quoteCheckedAt":market["checkedAt"],
         "symbol":SYMBOL,"source":"OKX official public market data",
         "last":q["last"],"markPx":q["markPx"],
         "lastTs":q["lastTs"],"markTs":q["markTs"],
         "fundingRate":q.get("fundingRate"),
         "nextFundingTime":q.get("nextFundingTime"),
         "openInterestUsd":q.get("openInterestUsd"),
         "openInterestTs":q.get("openInterestTs"),
         "rolling60m":q.get("rolling60m"),
         "book":book,"trades":trades,
         "events":events,"context":context,"supplementalErrors":warnings,
         "monitoring":"approximately 5-minute GitHub polling; not websocket or tick-streaming",
         "disclaimer":"signals describe the public market, not personal account liquidation risk"}
    save(ROOT/"risk"/"latest.json",doc)
    row={"ts":now,"last":q["last"],"markPx":q["markPx"],
         "lastTs":q["lastTs"],"markTs":q["markTs"],
         "oiUsd":q.get("openInterestUsd"),"fundingRate":q.get("fundingRate"),
         "book":book,"trades":trades,"eventCodes":[e["code"] for e in events]}
    day=dt.datetime.fromtimestamp(now/1000,UTC).strftime("%Y-%m-%d")
    daily=ROOT/"risk"/"history"/(day+".jsonl")
    daily.parent.mkdir(parents=True,exist_ok=True)
    if not past or now-int(past[-1]["ts"])>=30_000:
        with daily.open("a",encoding="utf-8") as f:
            f.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
    print(json.dumps({"phase3":"ok","symbol":SYMBOL,"last":q["last"],"events":[e["code"] for e in events],
                      "book":book is not None,"trades":trades is not None,"warnings":warnings},
                     ensure_ascii=False))

if __name__=="__main__":
    main()
