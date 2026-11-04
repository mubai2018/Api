#!/usr/bin/env python3
"""One-shot official OKX public WebSocket connectivity test (no secrets)."""
import asyncio
import json
import time

from aiohttp import ClientSession,WSMsgType

URL="wss://ws.okx.com/ws/v5/public"
SYMBOL="SPCX-USDT-SWAP"

async def run():
    got={}
    async with ClientSession() as session:
        async with session.ws_connect(URL,timeout=15) as socket:
            await socket.send_json({"op":"subscribe","args":[
                {"channel":"tickers","instId":SYMBOL},
                {"channel":"mark-price","instId":SYMBOL}]})
            limit=time.monotonic()+45
            while time.monotonic()<limit and len(got)<2:
                try:
                    msg=await asyncio.wait_for(socket.receive(),timeout=10)
                except asyncio.TimeoutError:
                    await socket.send_str("ping")
                    continue
                if msg.type!=WSMsgType.TEXT:
                    raise RuntimeError("WebSocket closed before both subscribed channels arrived")
                if msg.data=="pong":
                    continue
                obj=json.loads(msg.data)
                if obj.get("event")=="error":
                    raise RuntimeError("OKX rejected subscribe: "+str(obj.get("msg")))
                channel=obj.get("arg",{}).get("channel")
                if channel not in ("tickers","mark-price"):
                    continue
                for row in obj.get("data",[]):
                    if row.get("instId")!=SYMBOL:
                        raise RuntimeError("wrong symbol in WebSocket message")
                    now=int(time.time()*1000)
                    ts=int(row.get("ts","0"))
                    if ts>now+10_000 or ts<now-30_000:
                        raise RuntimeError("stale/future official source timestamp: "+str(ts))
                    key="last" if channel=="tickers" else "markPx"
                    price=float(row[key])
                    if price<=0:
                        raise RuntimeError("invalid price in "+channel)
                    got[channel]={"price":price,"sourceTs":ts,"lagMs":now-ts}
    print("OKX_WS_PORT_443_VERIFIED",json.dumps({"channels":got,"endpoint":URL},ensure_ascii=False))
    if set(got)!={"tickers","mark-price"}:
        raise RuntimeError("Missing required OKX WebSocket channels: "+str(set(got)))

if __name__=="__main__":
    asyncio.run(run())
