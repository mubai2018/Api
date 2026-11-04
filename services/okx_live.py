#!/usr/bin/env python3
"""Public OKX WebSocket SPCX live quote monitor, for a persistent cloud service.

No account keys, no order endpoints. WebSocket uses port 443 (8443 retired Oct 2026).
Public HTTP /health, /latest expose ONLY market information.
"""
import asyncio
import collections
import datetime as dt
import json
import logging
import os
import time

from aiohttp import ClientSession, WSMsgType, web

SYMBOL = "SPCX-USDT-SWAP"
WS_URL = os.getenv("OKX_WS_URL", "wss://ws.okx.com/ws/v5/public")
REST_URL = "https://openapi.okx.com"
PORT = int(os.getenv("PORT", "8080"))
MAX_SOURCE_AGE_MS = 30_000
THRESHOLDS = (120, 110, 100, 90)

def clock_ms():
    return int(time.time() * 1000)

def valid_source_ms(raw, now):
    ms = int(raw)
    if ms < now - 30_000 or ms > now + 10_000:
        raise ValueError("OKX source timestamp stale or future")
    return ms

def as_iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()

class MarketState:
    def __init__(self):
        self.latest = {}
        self.price_samples = collections.deque(maxlen=100_000)
        self.active = set()
        self.events = collections.deque(maxlen=150)
        self.connection = "disconnected"
        self.last_error = None
        self.connected_at = None
        self.last_msg_at = None
        self.mode = "none"
        self.incoming_count = 0

    def ingest(self, channel, row, now=None, mode="websocket"):
        now = now or clock_ms()
        if row.get("instId") != SYMBOL:
            raise ValueError("instrument mismatch")
        ts = valid_source_ms(row["ts"], now)
        if channel == "tickers":
            val = float(row["last"])
            key = "last"
        elif channel == "mark-price":
            val = float(row["markPx"])
            key = "markPx"
        else:
            return
        if not 0 < val < 1_000_000:
            raise ValueError("unreasonable/invalid quote")
        previous = self.latest.get(key)
        if previous and ts < previous["ts"]:
            return  # stale out-of-order update, not an outage
        self.latest[key] = {"value": val, "ts": ts, "source": mode}
        self.mode = mode
        self.last_msg_at = now
        self.incoming_count += 1
        if key == "last":
            self.price_samples.append((ts, val))
            self._prune(now)
        self._events(now)

    def _prune(self, now):
        while self.price_samples and now - self.price_samples[0][0] > 3_660_000:
            self.price_samples.popleft()

    def _fresh(self, key, now):
        entry = self.latest.get(key)
        return entry if entry and -10_000 <= now-entry["ts"] <= MAX_SOURCE_AGE_MS else None

    def _events(self, now):
        quotes=[v["value"] for key in ("last","markPx") if (v:=self._fresh(key,now))]
        if quotes:
            minimum=min(quotes)
            for limit in THRESHOLDS:
                code=f"PRICE_BELOW_{limit}"
                observed=minimum < limit
                if observed and code not in self.active:
                    self._transition(code,"opened",now,minimum,"USDT")
                elif not observed and code in self.active and len(quotes)==2:
                    self._transition(code,"recovered",now,minimum,"USDT")
        # 1h net return is based on observed ticker updates, not a full trade tape.
        samples=list(self.price_samples)
        if len(samples)>4 and samples[-1][0]-samples[0][0]>=55*60*1000:
            percent=100*(samples[-1][1]/samples[0][1]-1)
            code="OBSERVED_60M_FALL_5PCT"
            if percent<=-5 and code not in self.active:
                self._transition(code,"opened",now,round(percent,3),"%")
            elif percent>-5 and code in self.active:
                self._transition(code,"recovered",now,round(percent,3),"%")

    def _transition(self,code,kind,now,value,unit):
        if kind=="opened":
            self.active.add(code)
        else:
            self.active.discard(code)
        event={"id":f"{now}|{code}|{kind}","at":as_iso(now),"code":code,
               "transition":kind,"value":value,"unit":unit,
               "source":"OKX public market websocket"}
        self.events.append(event)
        logging.warning("MARKET_EVENT %s",json.dumps(event,ensure_ascii=False))

    def report(self,now=None):
        now=now or clock_ms()
        quotes={}
        for key in ("last","markPx"):
            ent=self.latest.get(key)
            if ent:
                quotes[key]={"price":ent["value"],"sourceTs":ent["ts"],
                             "ageSeconds":round((now-ent["ts"])/1000,2),
                             "fresh":self._fresh(key,now) is not None,
                             "source":ent["source"]}
        complete=all(self._fresh(k,now) for k in ("last","markPx"))
        return {"instrument":SYMBOL,"serverTime":as_iso(now),"ok":bool(complete),
                "status":"live" if complete else "stale_or_unavailable",
                "connection":self.connection,"dataMode":self.mode,
                "quotes":quotes,"activeEvents":sorted(self.active),
                "recentTransitions":list(self.events)[-20:],
                "receivedUpdates":self.incoming_count,
                "connectedAt":as_iso(self.connected_at) if self.connected_at else None,
                "lastError":self.last_error,
                "note":"Public market data only. Never use stale quotes for alerts; this is not account liquidation monitoring."}

async def websocket_forever(state,session):
    delay=2
    while True:
        try:
            async with session.ws_connect(WS_URL,timeout=15,max_msg_size=2_000_000) as ws:
                state.connection="connected"
                state.connected_at=clock_ms()
                state.last_error=None
                delay=2
                await ws.send_json({"op":"subscribe","args":[
                    {"channel":"tickers","instId":SYMBOL},
                    {"channel":"mark-price","instId":SYMBOL}]})
                while True:
                    try:
                        msg=await asyncio.wait_for(ws.receive(),timeout=22)
                    except asyncio.TimeoutError:
                        await ws.send_str("ping")  # OKX text ping/pong
                        continue
                    if msg.type==WSMsgType.TEXT:
                        if msg.data=="pong":
                            continue
                        obj=json.loads(msg.data)
                        if obj.get("event")=="error":
                            raise RuntimeError("OKX subscription error: "+str(obj.get("msg")))
                        if obj.get("event")=="notice":
                            raise RuntimeError("OKX scheduled websocket restart")
                        channel=obj.get("arg",{}).get("channel")
                        if channel in ("tickers","mark-price"):
                            for row in obj.get("data",[]):
                                state.ingest(channel,row)
                    elif msg.type in (WSMsgType.ERROR,WSMsgType.CLOSED,WSMsgType.CLOSE):
                        raise RuntimeError("OKX websocket disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.last_error=type(exc).__name__+": "+str(exc)[:180]
            logging.warning("OKX_WS_RECONNECT %s",state.last_error)
        state.connection="disconnected"
        await asyncio.sleep(delay)
        delay=min(40,delay*2)

async def rest_fallback(state,session):
    """Keep a backup quote while websocket is down. This is polling, NOT streaming."""
    while True:
        try:
            now=clock_ms()
            if not all(state._fresh(k,now) for k in ("last","markPx")):
                for path,channel in [
                    ("/api/v5/market/ticker?instId="+SYMBOL,"tickers"),
                    ("/api/v5/public/mark-price?instType=SWAP&instId="+SYMBOL,"mark-price")
                ]:
                    async with session.get(REST_URL+path,timeout=10) as response:
                        response.raise_for_status()
                        j=await response.json()
                    if j.get("code")!="0" or not j.get("data"):
                        raise ValueError("invalid OKX REST result")
                    state.ingest(channel,j["data"][0],mode="rest-fallback")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.last_error="REST: "+type(exc).__name__+": "+str(exc)[:150]
        await asyncio.sleep(15)

async def health(request):
    state=request.app["market"]
    report=state.report()
    return web.json_response({k:report[k] for k in
        ("serverTime","status","connection","dataMode","receivedUpdates","lastError")})

async def latest(request):
    result=request.app["market"].report()
    # HTTP 503 prevents clients from silently treating old quotes as live.
    return web.json_response(result,status=200 if result["ok"] else 503,
                             headers={"Cache-Control":"no-store"})

async def context(app):
    state=MarketState()
    app["market"]=state
    async with ClientSession(headers={"User-Agent":"okx-public-live-monitor/1.0"}) as session:
        tasks=[asyncio.create_task(websocket_forever(state,session)),
               asyncio.create_task(rest_fallback(state,session))]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    app=web.Application()
    app.cleanup_ctx.append(context)
    app.router.add_get("/health",health)
    app.router.add_get("/latest",latest)
    web.run_app(app,host="0.0.0.0",port=PORT)

if __name__=="__main__":
    main()
