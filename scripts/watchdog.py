#!/usr/bin/env python3
"""Independent GitHub job watchdog for SPCX market collection.

Operates without OKX private APIs, does not trade or notify externally.
Outputs source freshness and observed GitHub schedule-run evidence.
A second scheduler still cannot guarantee instant detection; ChatGPT's hourly
risk task separately reads these outputs.
"""
import datetime as dt
import json
import os
import pathlib
import time
import urllib.request

ROOT=pathlib.Path("data")
UTC=dt.timezone.utc
OWNER="mubai2018"
REPO="Api"
SYMBOL="SPCX-USDT-SWAP"

def stamp(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp()*1000)

def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        return None

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")

def grade(latest,risk,pipeline,now):
    issues=[]
    ages={}
    try:
        if latest.get("ok") is not True or latest.get("source")!="OKX official public REST API":
            raise ValueError("ticker lacks valid OKX-source marker")
        quote=latest["symbols"][SYMBOL]
        if quote.get("instId") != SYMBOL:
            raise ValueError("ticker instrument mismatch")
        times={"tickerCheckedAt":stamp(latest["checkedAt"]),
               "lastTs":int(quote["lastTs"]),"markTs":int(quote["markTs"])}
        ages.update({k:round((now-v)/60000,2) for k,v in times.items()})
        if any(age < -.17 for age in ages.values()):
            issues.append("future_quote_timestamp")
    except (AttributeError,KeyError,TypeError,ValueError):
        issues.append("invalid_or_missing_ticker")
    try:
        if risk.get("ok") is not True or risk.get("symbol")!=SYMBOL or risk.get("quoteCheckedAt")!=latest.get("checkedAt"):
            raise ValueError("invalid linked risk quote")
        ages["riskCheckedAt"]=round((now-stamp(risk["checkedAt"]))/60000,2)
    except (AttributeError,KeyError,TypeError,ValueError):
        issues.append("invalid_or_missing_risk")
    try:
        if pipeline.get("status")!="healthy" or any(pipeline.get("steps",{}).get(k)!="success" for k in ("quote","archive","risk","lifecycle")):
            issues.append("last_pipeline_attempt_failed")
        ages["lastAttempt"]=round((now-stamp(pipeline["attemptedAt"]))/60000,2)
    except (AttributeError,KeyError,TypeError,ValueError):
        issues.append("invalid_or_missing_pipeline")
    if any(v<-.17 for v in ages.values()):
        issues.append("future_source_time")
    if issues:
        return "outage",issues,ages
    maxage=max(ages.values())
    if maxage>20:
        return "outage",["quote_pipeline_or_risk_older_than_20_minutes"],ages
    if maxage>10:
        return "stale",["quote_pipeline_or_risk_older_than_10_minutes"],ages
    return "healthy",[],ages

def github_schedule_evidence(now):
    token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"status":"not_checked","reason":"no GitHub token"}
    uri=f"https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/okx.yml/runs?per_page=100"
    request=urllib.request.Request(uri,headers={
        "Accept":"application/vnd.github+json",
        "Authorization":"Bearer "+token,
        "X-GitHub-Api-Version":"2022-11-28",
        "User-Agent":"public-market-schedule-audit/1.0"})
    try:
        with urllib.request.urlopen(request,timeout=10) as response:
            payload=json.load(response)
        scheduled=[]
        for run in payload.get("workflow_runs",[]):
            if run.get("event")!="schedule":
                continue
            when=stamp(run["created_at"])
            if 0<=now-when<=86400_000:
                scheduled.append({"runId":run["id"],"createdAt":run["created_at"],
                                  "status":run.get("status"),"conclusion":run.get("conclusion")})
        scheduled.sort(key=lambda x:x["createdAt"],reverse=True)
        successes=sum(1 for run in scheduled if run["conclusion"]=="success")
        evidence="not_yet_observed" if not scheduled else ("seen_but_not_successful" if not successes else "successful_schedule_observed")
        return {"status":"checked","evidence":evidence,
                "scheduledRunsLast24h":len(scheduled),"successfulScheduledRunsLast24h":successes,
                "lastScheduledRun":scheduled[0] if scheduled else None,
                "sampleLimit":100,
                "warning":"No scheduled runs observed does NOT prove a configuration failure; GitHub may delay or omit runs"}
    except Exception as exc:
        return {"status":"unavailable","reason":type(exc).__name__+": "+str(exc)[:160]}

def transition(state,status,now):
    """Outage alerts are raised once; clear only after two healthy watchdog executions."""
    state=state or {"outageActive":False,"healthyStreak":0,"openedAt":None}
    old=bool(state.get("outageActive"))
    changes=[]
    at=dt.datetime.fromtimestamp(now/1000,UTC).isoformat()
    if status=="outage":
        state["healthyStreak"]=0
        if not old:
            state["outageActive"]=True
            state["openedAt"]=at
            changes.append({"at":at,"code":"DATA_PIPELINE_OUTAGE","transition":"opened",
                            "severity":"high","message":"Official SPCX data pipeline failed or sources older than 20m"})
    elif status=="healthy":
        if old:
            state["healthyStreak"]=state.get("healthyStreak",0)+1
            if state["healthyStreak"]>=2:
                state["outageActive"]=False
                state["openedAt"]=None
                state["healthyStreak"]=0
                changes.append({"at":at,"code":"DATA_PIPELINE_OUTAGE","transition":"recovered",
                                "severity":"info","message":"Two healthy independent watchdog checks"})
        else:
            state["healthyStreak"]=0
    else:
        state["healthyStreak"]=0
    state["lastCheckAt"]=at
    state["lastStatus"]=status
    state["lastTransitions"]=changes
    return state,changes

def main():
    now=int(time.time()*1000)
    latest=read(ROOT/"latest.json")
    risk=read(ROOT/"risk"/"latest.json")
    pipeline=read(ROOT/"quality"/"pipeline.json")
    status,issues,ages=grade(latest,risk,pipeline,now)
    schedule=github_schedule_evidence(now)
    state,events=transition(read(ROOT/"quality"/"watchdog_state.json"),status,now)
    doc={"checkedAt":dt.datetime.fromtimestamp(now/1000,UTC).isoformat(),
         "status":status,"issues":issues,"sourceAgeMinutes":ages,
         "schedule":schedule,"outageActive":state["outageActive"],"lastTransitions":events,
         "note":"Checks GitHub-persisted official market data, not a continuous OKX connection; independent ChatGPT hourly audit is still needed"}
    write(ROOT/"quality"/"watchdog.json",doc)
    write(ROOT/"quality"/"watchdog_state.json",state)
    if events:
        date=dt.datetime.fromtimestamp(now/1000,UTC).strftime("%Y-%m-%d")
        out=ROOT/"quality"/"outage_events"/(date+".jsonl")
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open("a",encoding="utf-8") as file:
            for ev in events:
                file.write(json.dumps(ev,ensure_ascii=False,separators=(",",":"))+"\n")
    print("WATCHDOG",json.dumps({"status":status,"issues":issues,"scheduled":schedule.get("evidence"),
                                 "events":[e["transition"] for e in events]},ensure_ascii=False))

if __name__=="__main__":
    main()
