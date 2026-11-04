#!/usr/bin/env python3
"""Read-only risk event lifecycle: open, escalation and 2-sample recovery.

Consumes fresh validated phase3 output, persists transition history to Git.
No private API, no outbound notifications, no trading.
"""
import datetime as dt
import json
import pathlib
import re
import time

ROOT = pathlib.Path("data")
ALLOWED = {"FALL_5M", "FALL_15M", "FALL_60M", "DRAWDOWN_60M",
           "MARK_DEVIATION", "FUNDING_EXTREME", "SPREAD_WIDE",
           "BID_DEPTH_COLLAPSE", "OI_1H_CHANGE"}
SEVERITY = {"medium":1, "high":2, "critical":3}
UTC = dt.timezone.utc

def millis(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp()*1000)

def read(path,default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def write(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")

def permitted(code):
    return code in ALLOWED or re.fullmatch(r"PRICE_BELOW_(120|110|100|90)",code) is not None

def observable(code,risk):
    if code in ("SPREAD_WIDE","BID_DEPTH_COLLAPSE"):
        return isinstance(risk.get("book"),dict) and risk["book"].get("ts") is not None
    if code=="OI_1H_CHANGE":
        return risk.get("openInterestUsd") is not None and risk.get("openInterestTs") is not None
    if code=="FUNDING_EXTREME":
        return risk.get("fundingRate") is not None
    if code in {"FALL_5M","FALL_15M","FALL_60M","DRAWDOWN_60M"}:
        r=risk.get("rolling60m")
        return isinstance(r,dict) and r.get("points",0)>=56
    return True

def validate(risk,quote,now):
    if risk.get("ok") is not True or risk.get("source") != "OKX official public market data":
        raise ValueError("Unverified risk source")
    if quote.get("ok") is not True or quote.get("source") != "OKX official public REST API":
        raise ValueError("Unverified ticker source")
    if risk.get("symbol")!="SPCX-USDT-SWAP" or risk.get("quoteCheckedAt") != quote.get("checkedAt"):
        raise ValueError("Mismatched instrument or quote source")
    row=quote["symbols"]["SPCX-USDT-SWAP"]
    if float(risk["last"]) != float(row["last"]) or float(risk["markPx"]) != float(row["markPx"]):
        raise ValueError("Ticker / risk price mismatch")
    for stamp in (millis(risk["checkedAt"]),millis(quote["checkedAt"]),
                  int(row["lastTs"]),int(row["markTs"]),int(risk["lastTs"]),int(risk["markTs"])):
        if now-stamp>600000 or stamp>now+10000:
            raise ValueError("Stale or future source data")
    seen=set()
    for ev in risk.get("events",[]):
        code=ev.get("code")
        if not code or not permitted(code) or ev.get("severity") not in SEVERITY:
            raise ValueError("Invalid risk event")
        if code in seen:
            raise ValueError("Duplicate risk event code")
        seen.add(code)
        source=int(ev.get("sourceTs",0))
        if now-source>600000 or source>now+10000:
            raise ValueError("Stale event timestamp")
        if not observable(code,risk):
            raise ValueError("Event without matching validated supporting data")
    return True

def update(previous,risk,now):
    """Pure transition calculation; stores no alert on incomplete optional signal."""
    checked=risk["checkedAt"]
    st=previous or {"schemaVersion":1,"active":{},"pending":{},"lastProcessedCheckedAt":None}
    if st.get("lastProcessedCheckedAt")==checked:
        return st, []
    active=st.setdefault("active",{})
    pending=st.setdefault("pending",{})
    current={e["code"]:e for e in risk.get("events",[])}
    transitions=[]
    def transition(code,kind,severity,value=None,unit=None):
        transitions.append({"id":checked+"|"+code+"|"+kind,"at":checked,
                            "code":code,"transition":kind,"severity":severity,
                            "value":value,"unit":unit,"last":risk["last"],"markPx":risk["markPx"]})
    for code,event in current.items():
        level=event["severity"]
        if code in active:
            old=active[code]
            old["absentStreak"]=0
            old["lastSeenAt"]=checked
            old["lastValue"]=event.get("value")
            if SEVERITY[level]>SEVERITY[old["severity"]]:
                transition(code,"escalated",level,event.get("value"),event.get("unit"))
                old["severity"]=level
            pending.pop(code,None)
        else:
            entry=pending.get(code,{"seen":0,"lastSeenAt":None,"firstSeenAt":checked})
            # Medium severity requires two distinct fresh snapshots.
            entry["seen"]+=1
            entry["lastSeenAt"]=checked
            entry["severity"]=level
            pending[code]=entry
            if level in ("high","critical") or entry["seen"]>=2:
                active[code]={"openedAt":entry["firstSeenAt"],"lastSeenAt":checked,
                              "severity":level,"lastValue":event.get("value"),"absentStreak":0}
                transition(code,"opened",level,event.get("value"),event.get("unit"))
                pending.pop(code,None)
    for code in list(active):
        if code in current:
            continue
        if not observable(code,risk):
            active[code]["observation"]="unavailable"
            continue
        active[code].pop("observation",None)
        active[code]["absentStreak"]=active[code].get("absentStreak",0)+1
        if active[code]["absentStreak"]>=2:
            transition(code,"recovered",active[code]["severity"])
            del active[code]
    for code in list(pending):
        if code not in current and observable(code,risk):
            del pending[code]
    st["lastProcessedCheckedAt"]=checked
    st["lastUpdatedAt"]=dt.datetime.fromtimestamp(now/1000,UTC).isoformat()
    st["lastTransitions"]=transitions
    st["activeCount"]=len(active)
    return st,transitions

def main():
    now=int(time.time()*1000)
    risk=read(ROOT/"risk"/"latest.json")
    quote=read(ROOT/"latest.json")
    if risk is None or quote is None:
        raise ValueError("Missing market snapshots")
    validate(risk,quote,now)
    path=ROOT/"risk"/"alert_state.json"
    old=read(path)
    state,changes=update(old,risk,now)
    write(path,state)
    if changes:
        date=dt.datetime.fromtimestamp(now/1000,UTC).strftime("%Y-%m-%d")
        out=ROOT/"risk"/"alerts"/(date+".jsonl")
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("a",encoding="utf-8") as fh:
            for event in changes:
                fh.write(json.dumps(event,ensure_ascii=False,separators=(",",":"))+"\n")
    print(json.dumps({"lifecycle":"ok","active":len(state["active"]),
                      "transitions":[e["id"] for e in changes]},ensure_ascii=False))

if __name__=="__main__":
    main()
