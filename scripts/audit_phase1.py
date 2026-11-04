#!/usr/bin/env python3
"""Independent acceptance audit for public OHLCV, settled funding, market samples."""
import datetime as dt
import json, math, pathlib, sys, time

ROOT=pathlib.Path("data")
SYMS=("SPCX-USDT-SWAP","BTC-USDT-SWAP","ETH-USDT-SWAP")
PLAN={"SPCX-USDT-SWAP":{"1m":7,"5m":30,"1H":90,"4H":180,"1D":365},
      "BTC-USDT-SWAP":{"1H":90,"4H":180,"1D":365},
      "ETH-USDT-SWAP":{"1H":90,"4H":180,"1D":365}}
STEP={"1m":60000,"5m":300000,"1H":3600000,"4H":14400000,"1D":86400000}

def entries(path):
    for f in sorted(path.glob("????-??.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip(): yield f,json.loads(line)

def audit_candles(symbol,bar,days,now):
    folder=ROOT/"history"/"candles"/symbol/bar
    prev=None;first=None;last=None;count=0;invalid=0;dup=0;missing=0;files=set()
    for f,row in entries(folder):
        files.add(f.name)
        try:
            assert isinstance(row,list) and len(row)==6
            ts=int(row[0]);o,h,l,c,v=map(float,row[1:])
            assert all(math.isfinite(x) for x in (o,h,l,c,v))
            assert min(o,h,l,c)>0 and v>=0 and h>=max(o,c) and l<=min(o,c)
            assert (ts-(16*3600000 if bar=="1D" else 0))%STEP[bar]==0
            assert f.name==dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).strftime("%Y-%m")+".jsonl"
        except (ValueError,TypeError,AssertionError,OverflowError):
            invalid+=1;continue
        if prev is not None:
            if ts<=prev:dup+=1
            elif ts-prev>STEP[bar]:missing+=round((ts-prev)/STEP[bar])-1
        if first is None:first=ts
        last=ts;prev=ts;count+=1
    coverage=(last-first+STEP[bar])/86400000 if first is not None else 0
    age_hours=(now-last)/3600000 if last is not None else None
    return {"count":count,"firstTs":first,"lastTs":last,"fileCount":len(files),
            "targetDays":days,"coveredDays":round(coverage,2),"coveragePct":round(100*min(1,coverage/days),2),
            "missingBarsWithinCoverage":missing,"duplicateOrUnordered":dup,"invalidRows":invalid,
            "latestAgeHours":round(age_hours,2) if age_hours is not None else None,
            "valid":count>0 and missing+dup+invalid==0}

def audit_funding(symbol):
    prev=None;first=None;last=None;count=0;invalid=0;dup=0
    for f,row in entries(ROOT/"history"/"funding"/symbol):
        try:
            assert isinstance(row,list) and len(row)==3
            ts=int(row[0]);realized=float(row[1]);indicated=float(row[2])
            assert all(math.isfinite(x) and abs(x)<=1 for x in (realized,indicated))
            assert f.name==dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).strftime("%Y-%m")+".jsonl"
        except (ValueError,TypeError,AssertionError,OverflowError):
            invalid+=1;continue
        if prev is not None and ts<=prev:dup+=1
        if first is None:first=ts
        last=ts;prev=ts;count+=1
    return {"count":count,"firstTs":first,"lastTs":last,"invalidRows":invalid,
            "duplicateOrUnordered":dup,"valid":count>0 and invalid+dup==0}

def audit_market(now):
    files=sorted((ROOT/"history"/"market").glob("????-??-??.jsonl"))
    total=bad=dups=gaps=0;first=None;last=None;seen=set()
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():continue
            try:
                r=json.loads(line);ts=int(r["ts"])
                assert f.name==dt.datetime.fromtimestamp(ts/1000,dt.timezone.utc).strftime("%Y-%m-%d")+".jsonl"
                for symbol in SYMS:
                    item=r["symbols"][symbol]
                    assert float(item["last"])>0 and float(item["markPx"])>0
                    assert abs(ts-int(item["lastTs"]))<=180000
                    assert abs(ts-int(item["markTs"]))<=180000
            except (ValueError,TypeError,AssertionError,KeyError,OverflowError):
                bad+=1;continue
            if ts in seen:dups+=1
            seen.add(ts)
            if last is not None and ts-last>900000:gaps+=1
            if first is None:first=ts
            last=ts;total+=1
    return {"count":total,"firstTs":first,"lastTs":last,"invalidRows":bad,
            "duplicate":dups,"gapsOver15Minutes":gaps,
            "ageMinutes":round((now-last)/60000,1) if last else None,
            "valid":total>0 and bad+dups==0}

def main():
    now=int(time.time()*1000);issues=[];warnings=[];candles={}
    for symbol,plan in PLAN.items():
        for bar,days in plan.items():
            key=symbol+"/"+bar
            d=audit_candles(symbol,bar,days,now);candles[key]=d
            if not d["valid"]:issues.append("invalid/missing candle data: "+key)
            if d["coveragePct"]<90:warnings.append("less than 90% target coverage: "+key)
            if d["latestAgeHours"] is not None and d["latestAgeHours"]>3*STEP[bar]/3600000:
                warnings.append("stale completed candles: "+key)
    funding={s:audit_funding(s) for s in SYMS}
    for s,item in funding.items():
        if not item["valid"]:issues.append("funding history invalid/missing: "+s)
    market=audit_market(now)
    if not market["valid"]:issues.append("market snapshot history invalid/missing")
    if market["ageMinutes"] is None or market["ageMinutes"]>15:
        warnings.append("live market snapshot history is stale")
    meta=ROOT/"metadata"/"instruments.json"
    if not meta.exists():issues.append("instrument metadata missing")
    else:
        try:
            j=json.loads(meta.read_text(encoding="utf-8"))
            for s in SYMS:
                d=j["symbols"][s]
                if any(not d.get(k) for k in ("instId","ctVal","tickSz","lotSz","minSz")):
                    issues.append("incomplete instrument metadata: "+s)
        except (ValueError,KeyError,TypeError):issues.append("invalid instrument metadata")
    index=ROOT/"history"/"index.json"
    if not index.exists():issues.append("historical index absent")
    report={"checkedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
        "status":"fail" if issues else ("degraded" if warnings else "pass"),
        "issues":issues,"warnings":warnings,"candles":candles,"funding":funding,
        "market":market,"indexExists":index.exists(),
        "definition":"No fabricated/interpolated bars. Coverage percent is actual span vs target; gaps are measured inside observed span."}
    dest=ROOT/"quality"/"acceptance.json";dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("PHASE1_ACCEPTANCE:",json.dumps({"status":report["status"],"issues":len(issues),"warnings":len(warnings)}))
    return 2 if issues else 0

if __name__=="__main__":sys.exit(main())
