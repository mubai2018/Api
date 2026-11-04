#!/usr/bin/env python3
"""Record each GitHub collection attempt, including failed official API requests.

This file is a health heartbeat, not itself market data. It must never claim a
fresh quote when the quote step failed.
"""
import datetime as dt
import json
import os
import pathlib

def main():
    names=("quote","archive","risk","lifecycle")
    outcomes={name:os.environ.get(name.upper()+"_OUTCOME","unknown") for name in names}
    at=dt.datetime.now(dt.timezone.utc).isoformat()
    healthy=all(value=="success" for value in outcomes.values())
    doc={"attemptedAt":at,"status":"healthy" if healthy else "degraded",
         "steps":outcomes,"warning":"Schedule is best effort; check quote source timestamps, not only this heartbeat"}
    path=pathlib.Path("data/quality/pipeline.json")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("PIPELINE_HEALTH",json.dumps({"at":at,"status":doc["status"],"steps":outcomes}))

if __name__=="__main__":
    main()
