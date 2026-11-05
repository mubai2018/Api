import { Database } from "bun:sqlite";
import { unlink } from "node:fs/promises";
/**
 * SPCX public realtime feed. No credentials or trading.
 * HTTP: /health, /latest, /events
 * OKX official public WebSocket over TLS 443, with REST fallback.
 */
const INST = "SPCX-USDT-SWAP";
const SOURCES = ["https://openapi.okx.com", "https://www.okx.com"];
const WS_URLS = ["wss://ws.okx.com/ws/v5/public"]; // validated official endpoint; wsaws DNS intermittently fails
const port = Number(Bun.env.PORT || "3000");
const startAt = Date.now();
type Quote = { price: number; ts: number; source: string; receivedAt: number };
type Event = { code:string; price:number; at:number; source:string };
let last: Quote | null = null;
let mark: Quote | null = null;
let ws: WebSocket | null = null;
let wsConnected = false;
let wsMessages = 0;
let reconnectCount = 0;
let wsIndex = 0;
let lastWsMessage = 0;
let lastWsError = "";
let lastRestError = "";
let lastRestSuccess = 0;
let lastRestAttempt = 0;
let lastPeriodicLogAt = 0;
const events:Event[] = [];
const samples:{ts:number;price:number}[] = [];
const alertThresholds = [120,110,100,90];
const active = new Set<string>();
const MAX_AGE_MS = 30000;
function now() { return Date.now(); }
function fresh(v:Quote|null, time=now()) { return !!v && time-v.ts >= -10000 && time-v.ts <= MAX_AGE_MS; }
function record(q:Quote, kind:"last"|"mark") {
  if (!Number.isFinite(q.price) || q.price<=0 || !Number.isFinite(q.ts) || q.ts>now()+10000 || now()-q.ts>90000) return;
  const prev=kind==="last"?last:mark;
  if(prev && q.ts < prev.ts) return;
  if(kind==="last") {
    last=q;
    if(!prev || prev.ts!==q.ts) {
      samples.push({ts:q.ts,price:q.price});
      const floor=q.ts-3600000;
      while(samples.length && samples[0].ts < floor) samples.shift();
      if(samples.length>50000) samples.splice(0,samples.length-50000);
    }
  } else mark=q;
  for(const t of alertThresholds){
    const code="PRICE_BELOW_"+t;
    const px=Math.min(fresh(last)?last!.price:Infinity,fresh(mark)?mark!.price:Infinity);
    if(px<t && !active.has(code)) {
      active.add(code);
      const evt={code,price:px,at:now(),source:q.source};
      events.push(evt);archiveEvent(evt);
    }else if(px>=t && active.has(code)){
      active.delete(code);
      const evt={code:"RECOVERED_"+code,price:px,at:now(),source:q.source};
      events.push(evt);archiveEvent(evt);
    }
  }
  if(events.length>500) events.splice(0,events.length-500);
}
function parseWs(raw:string) {
  if(raw==="pong") return;
  let payload:any;
  try{payload=JSON.parse(raw)}catch{return}
  if(payload?.event === "error"){lastWsError=String(payload?.msg||"OKX subscription rejected"); return}
  if(!Array.isArray(payload?.data))return;
  const ch=payload?.arg?.channel;
  if(ch!=="tickers"&&ch!=="mark-price") return;
  const at=now();
  for(const row of payload.data){
    if(row.instId!==INST)continue;
    const px=Number(ch==="tickers"?row.last:row.markPx);
    const ts=Number(row.ts);
    if(Number.isFinite(px)&&Number.isFinite(ts)) record({price:px,ts,receivedAt:at,source:"OKX-public-WS-"+ch},ch==="tickers"?"last":"mark");
  }
}
function connect() {
  if(ws && (ws.readyState===WebSocket.OPEN || ws.readyState===WebSocket.CONNECTING))return;
  const url=WS_URLS[wsIndex%WS_URLS.length];
  try{
    const socket=new WebSocket(url);
    ws=socket;
    // Prevent a half-open WebSocket handshake from blocking reconnect forever.
    setTimeout(()=>{
      if(ws===socket && socket.readyState===WebSocket.CONNECTING){
        lastWsError="websocket_handshake_timeout";
        console.log("OKX_WS_HANDSHAKE_TIMEOUT",url);
        ws=null;
        try{socket.close()}catch{}
        reconnectCount++;
        setTimeout(connect,1500);
      }
    },20000);
    socket.onopen=()=>{
      wsConnected=true;
      lastWsMessage=now();
      lastWsError="";
      socket.send(JSON.stringify({op:"subscribe",args:[{channel:"tickers",instId:INST},{channel:"mark-price",instId:INST}]}));
      console.log("OKX_WS_CONNECTED",url,new Date().toISOString());
    };
    socket.onmessage=(evt)=>{
      wsMessages++;
      lastWsMessage=now();
      parseWs(typeof evt.data==="string"?evt.data:String(evt.data));
    };
    socket.onerror=(evt)=>{lastWsError="websocket_transport_error";console.log("OKX_WS_ERROR",url)};
    socket.onclose=(evt)=>{
      wsConnected=false;
      lastWsError="closed "+evt.code+" "+String(evt.reason||"");
      reconnectCount++;
      wsIndex++;
      console.log("OKX_WS_CLOSED",lastWsError);
      if(ws===socket)ws=null;
      setTimeout(connect,Math.min(20000,1000*Math.max(1,reconnectCount)));
    };
  }catch(e){
    lastWsError=String(e);
    wsConnected=false; ws=null;
    reconnectCount++;wsIndex++;
    setTimeout(connect,Math.min(20000,1000*Math.max(1,reconnectCount)));
  }
}
async function getJson(path:string):Promise<any>{
  const errs:string[]=[];
  for(const base of SOURCES){
    try {
      const abort=new AbortController();
      const timeout=setTimeout(()=>abort.abort(),6000);
      try{
        const r=await fetch(base+path,{headers:{"Accept":"application/json"},signal:abort.signal});
        if(!r.ok)throw new Error("HTTP "+r.status);
        const obj=await r.json() as any;
        if(obj?.code!=="0"||!Array.isArray(obj?.data)||!obj.data.length)throw new Error("Bad API code");
        return obj.data[0];
      }finally{clearTimeout(timeout)}
    }catch(e){errs.push(base+" "+String(e).slice(0,100))}
  }
  throw new Error(errs.join(" | "));
}
let runningRest=false;
async function pollRest(){
  if(runningRest)return;
  runningRest=true;lastRestAttempt=now();
  try{
    const [t,m]=await Promise.all([
      getJson("/api/v5/market/ticker?instId="+INST),
      getJson("/api/v5/public/mark-price?instType=SWAP&instId="+INST)
    ]);
    if(t.instId!==INST||m.instId!==INST)throw new Error("Instrument mismatch");
    const at=now();
    record({price:Number(t.last),ts:Number(t.ts),receivedAt:at,source:"OKX-public-REST"},"last");
    record({price:Number(m.markPx),ts:Number(m.ts),receivedAt:at,source:"OKX-public-REST"},"mark");
    lastRestSuccess=now();lastRestError="";
  }catch(e){lastRestError=String(e);console.log("OKX_REST_FAIL",lastRestError)}
  finally{runningRest=false}
}


/** SCHEMA_VERSION_MARKET_V2: validated, read-only historical market archive. */
const MARKET_DB_PATH="/data/spcx-market.sqlite";
let storageError:string|null=null;
let lastStoredAt=0;
let lastBackupAt=0;
let backupError:string|null=null;
let backupBusy=false;
let backupBytes=0;
let backfillRequests=0;
let backfillFailures=0;
const archives=new Database(MARKET_DB_PATH,{create:true});
archives.run("PRAGMA journal_mode=WAL");
archives.run("PRAGMA synchronous=FULL");
archives.run("PRAGMA busy_timeout=5000");
archives.run("PRAGMA foreign_keys=ON");
archives.run("CREATE TABLE IF NOT EXISTS quote_samples (ts INTEGER PRIMARY KEY, last REAL NOT NULL,mark REAL NOT NULL,last_ts INTEGER NOT NULL,mark_ts INTEGER NOT NULL,last_source TEXT NOT NULL,mark_source TEXT NOT NULL,btc REAL,eth REAL,funding REAL,oi_usd REAL,checked_at TEXT NOT NULL)");
archives.run("CREATE TABLE IF NOT EXISTS candles (symbol TEXT NOT NULL,bar TEXT NOT NULL,ts INTEGER NOT NULL,open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,volume REAL NOT NULL,source TEXT NOT NULL,PRIMARY KEY(symbol,bar,ts))");
archives.run("CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(ts)");
archives.run("CREATE TABLE IF NOT EXISTS event_history (id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER NOT NULL,code TEXT NOT NULL,kind TEXT NOT NULL,price REAL NOT NULL,source TEXT NOT NULL,UNIQUE(ts,code,kind))");
archives.run("CREATE TABLE IF NOT EXISTS funding_history (symbol TEXT NOT NULL,ts INTEGER NOT NULL,realized_rate REAL,funding_rate REAL,source TEXT NOT NULL,PRIMARY KEY(symbol,ts))");
archives.run("CREATE TABLE IF NOT EXISTS oi_history (symbol TEXT NOT NULL,ts INTEGER NOT NULL,oi_usd REAL NOT NULL,source TEXT NOT NULL,PRIMARY KEY(symbol,ts))");
archives.run("CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)");
try{lastStoredAt=Number((archives.query("SELECT MAX(ts) AS ts FROM quote_samples").get() as any)?.ts)||0;}
catch(err){console.log("SPCX_SNAPSHOT_CURSOR_ERROR",String(err).slice(0,100));}
try{
  for(const bound of alertThresholds){
    const opened="PRICE_BELOW_"+bound;
    const lastEvent=archives.query("SELECT code FROM event_history WHERE code IN (?,?) ORDER BY ts DESC LIMIT 1").get(opened,"RECOVERED_"+opened) as any;
    if(lastEvent?.code===opened)active.add(opened);
  }
}catch(err){console.log("SPCX_EVENT_STATE_RECOVERY_ERROR",String(err).slice(0,120));}
try{
  const saved=archives.query("SELECT value FROM archive_meta WHERE key='last_verified_backup'").get() as any;
  if(saved){const val=JSON.parse(saved.value);lastBackupAt=Number(val.ts)||0;backupBytes=Number(val.bytes)||0;}
}catch(err){console.log("SPCX_BACKUP_METADATA_ERROR",String(err).slice(0,140));}
const insQuote=archives.query("INSERT OR IGNORE INTO quote_samples(ts,last,mark,last_ts,mark_ts,last_source,mark_source,btc,eth,funding,oi_usd,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)");
const insBar=archives.query("INSERT OR IGNORE INTO candles(symbol,bar,ts,open,high,low,close,volume,source) VALUES(?,?,?,?,?,?,?,?,?)");
const insEvent=archives.query("INSERT OR IGNORE INTO event_history(ts,code,kind,price,source) VALUES(?,?,?,?,?)");
const insFunding=archives.query("INSERT OR REPLACE INTO funding_history(symbol,ts,realized_rate,funding_rate,source) VALUES(?,?,?,?,?)");
const insOi=archives.query("INSERT OR REPLACE INTO oi_history(symbol,ts,oi_usd,source) VALUES(?,?,?,?)");
const allowedSymbols=new Set(["SPCX-USDT-SWAP","BTC-USDT-SWAP","ETH-USDT-SWAP"]);
const allowedBars=new Set(["1m","5m","1H","4H","1D"]);
const intervalMs:Record<string,number>={"1m":60000,"5m":300000,"1H":3600000,"4H":14400000,"1D":86400000};
function safeDb(label:string,run:()=>void){
  try{run();storageError=null;}
  catch(err){storageError=label+": "+String(err).slice(0,180);console.log("SPCX_STORAGE_ERROR",storageError);}
}
function archiveEvent(event:Event){
  safeDb("event",()=>{insEvent.run(event.at,event.code,event.code.startsWith("RECOVERED")?"recovered":"opened",event.price,event.source);});
}
function archiveQuoteSample(q:any){
  if(!q.healthy||!q.last||!q.mark)return;
  const stamp=Date.parse(q.asOf);
  if(!Number.isFinite(stamp))return;
  const ts=Math.floor(stamp/60000)*60000;
  if(ts<=lastStoredAt)return;
  const c=marketContext && stamp-Date.parse(marketContext.checkedAt)<=300000?marketContext:null;
  safeDb("quote",()=>{
    insQuote.run(ts,q.last.price,q.mark.price,q.last.ts,q.mark.ts,q.last.source,q.mark.source,
      c?.btc?.last??null,c?.eth?.last??null,c?.funding?.fundingRate??null,c?.openInterest?.oiUsd??null,q.asOf);
    lastStoredAt=ts;
  });
}
function archiveBars(symbol:string,bar:string,rows:any[],source="OKX-REST-history"){
  if(!allowedSymbols.has(symbol)||!allowedBars.has(bar))throw new Error("symbol/bar invalid");
  const validated=closedCandleRows(rows,Math.max(rows.length,1));
  let accepted=0;
  const insert=archives.transaction(()=>{
    for(const r of validated){
      if(r.ts>Date.now()+10000||r.ts<0)continue;
      insBar.run(symbol,bar,r.ts,r.open,r.high,r.low,r.close,r.volume,source);
      accepted++;
    }
  });
  insert();
  return {accepted,oldest:validated[0]?.ts??null,newest:validated.at(-1)?.ts??null};
}
function archiveSnapshotQuality(){
  const cnt=(query:string,args:any[]=[])=>Number((archives.query(query).get(...args) as any)?.count??0);
  const sizes=allowedSymbols;
  const candleSeries=archives.query("SELECT symbol,bar,COUNT(*) AS count,MIN(ts) AS firstTs,MAX(ts) AS lastTs FROM candles GROUP BY symbol,bar ORDER BY symbol,bar").all();
  return {persistent:true,dbPath:"Railway mounted persistent volume (not container ephemeral layer)",
    schemaVersion:2,lastStoredAt,storageError,
    samples:cnt("SELECT COUNT(*) AS count FROM quote_samples"),candles:cnt("SELECT COUNT(*) AS count FROM candles"),
    funding:cnt("SELECT COUNT(*) AS count FROM funding_history"),oi:cnt("SELECT COUNT(*) AS count FROM oi_history"),
    events:cnt("SELECT COUNT(*) AS count FROM event_history"),series:candleSeries,
    backup:{lastBackupAt,bytes:backupBytes,error:backupError},
    backfill:{requests:backfillRequests,failedRequests:backfillFailures}};
}
function readCandleWindow(symbol:string,bar:string,after:number,before:number,limit:number){
  const sql="SELECT symbol,bar,ts,open,high,low,close,volume,source FROM candles WHERE symbol=? AND bar=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT ?";
  const rows=archives.query(sql).all(symbol,bar,after,before,limit) as any[];
  return rows.reverse();
}
function historyView(url:URL){
  const symbol=url.searchParams.get("symbol")||INST;
  const bar=url.searchParams.get("bar")||"1m";
  if(!allowedSymbols.has(symbol)||!allowedBars.has(bar))return {error:"unsupported symbol/bar"};
  const limit=Math.max(1,Math.min(500,Number(url.searchParams.get("limit")||"200")||200));
  const after=Math.max(0,Number(url.searchParams.get("from")||"0")||0);
  const before=Math.min(Date.now(),Number(url.searchParams.get("to")||String(Date.now()))||Date.now());
  const rows=readCandleWindow(symbol,bar,after,before,limit);
  const step=intervalMs[bar];let gaps=0,missing=0;
  for(let i=1;i<rows.length;i++){const delta=rows[i].ts-rows[i-1].ts;if(delta>step){gaps++;if(delta%step===0)missing+=delta/step-1;}}
  return {symbol,bar,requested:{from:after,to:before,limit},count:rows.length,
    firstTs:rows[0]?.ts??null,lastTs:rows.at(-1)?.ts??null,insideReturnedWindow:{gapCount:gaps,missingBars:missing},
    completeRequestedRange:false,meaning:"bounded descending pagination converted to ascending; missing outside returned window not assumed zero",rows};
}
function historicalAnalytics(minutes=1440){
  const windowMinutes=Math.max(60,Math.min(10080,Math.floor(minutes)));
  const begin=Date.now()-windowMinutes*60000;
  const rows=archives.query("SELECT ts,last,mark FROM quote_samples WHERE ts>=? ORDER BY ts ASC LIMIT 15000").all(begin) as any[];
  const out:any={requestedMinutes:windowMinutes,samples:rows.length,firstTs:rows[0]?.ts??null,lastTs:rows.at(-1)?.ts??null,
    maxGapMinutes:null,coveragePct:null,netPct:null,high:null,low:null,realizedVolPct:null};
  if(rows.length>=2){
    const elapsed=rows.at(-1)!.ts-rows[0].ts;let maxGap=0;let logSquare=0;
    let high=-Infinity,low=Infinity;
    for(let i=0;i<rows.length;i++){
      high=Math.max(high,rows[i].last);low=Math.min(low,rows[i].last);
      if(i){maxGap=Math.max(maxGap,rows[i].ts-rows[i-1].ts);let v=Math.log(rows[i].last/rows[i-1].last);logSquare+=v*v;}
    }
    out.maxGapMinutes=Number((maxGap/60000).toFixed(2));
    out.coveragePct=Number((100*rows.length/(windowMinutes+1)).toFixed(2));
    out.coverageWithinObservedSpanPct=Number(Math.min(100,100*rows.length/(Math.floor(elapsed/60000)+1)).toFixed(2));
    out.netPct=Number((100*(rows.at(-1)!.last/rows[0].last-1)).toFixed(4));
    out.high=high;out.low=low;out.realizedVolPct=Number((100*Math.sqrt(logSquare)).toFixed(4));
  }
  out.note="Observed 1-minute snapshots only; not every trade; do not interpret missing rows as unchanged price";
  return out;
}
function shortAnalyticsReport(){
  const q=snapshot();
  const ctx=q.marketContext;
  const snap=archives.query("SELECT COUNT(*) AS count,MIN(ts) AS firstTs,MAX(ts) AS lastTs FROM quote_samples").get() as any;
  const barSummary=archives.query("SELECT symbol,bar,COUNT(*) AS count,MIN(ts) AS firstTs,MAX(ts) AS lastTs FROM candles GROUP BY symbol,bar ORDER BY symbol,bar").all();
  return {schemaVersion:2,generatedAt:new Date().toISOString(),
    healthy:q.healthy && storageError===null,quote:{symbol:INST,last:q.last,mark:q.mark,websocket:q.websocket},
    contextCheckedAt:ctx?.checkedAt??null,contextErrors:ctx?.errors??["context unavailable"],
    rolling60m:ctx?.spcx?.rolling60m??null,btc:ctx?.btc??null,eth:ctx?.eth??null,
    funding:ctx?.funding??null,oi:ctx?.openInterest??null,
    coverage:{snapshots:snap,series:barSummary},history24h:historicalAnalytics(1440),
    storage:{dbHealthy:storageError===null,lastStoredAt,lastBackupAt,backupError},warnings:["Only closed candles; time-series gaps are never interpolated"]};
}

/** SCHEMA_VERSION_GPT_V3: bounded source-grounded analysis contract for GPT. */
function queryCandleTail(symbol:string,bar:string,count=8){
  const rows=archives.query("SELECT ts,open,high,low,close,volume,source FROM candles WHERE symbol=? AND bar=? ORDER BY ts DESC LIMIT ?").all(symbol,bar,count) as any[];
  return rows.reverse();
}
function historicalCoverage(symbol:string,bar:string,windowDays:number){
  const from=Date.now()-windowDays*86400000,step=intervalMs[bar];
  const rows=archives.query("SELECT ts FROM candles WHERE symbol=? AND bar=? AND ts>=? ORDER BY ts LIMIT 16000").all(symbol,bar,from) as any[];
  let missing=0,gapCount=0,longest=0;
  for(let i=1;i<rows.length;i++){
    const diff=rows[i].ts-rows[i-1].ts;
    if(diff>step){missing+=Math.max(0,Math.floor(diff/step)-1);gapCount++;longest=Math.max(longest,diff/60000);}
  }
  const expected=rows.length+missing;
  return {symbol,bar,windowDays,observed:rows.length,firstTs:rows[0]?.ts??null,lastTs:rows.at(-1)?.ts??null,
    missingBetweenObserved:missing,gapCount,maxGapMinutes:longest,
    observedIntervalCoveragePct:expected>0?Number((100*rows.length/expected).toFixed(2)):null,
    note:"This is coverage only between first and last observed timestamp, not proof the entire requested window is present"};
}
function gptReport(){
  const base=shortAnalyticsReport();
  const nowTs=Date.now();
  const candleGroups:any={};const coverage:any[]=[];
  for(const symbol of allowedSymbols){
    candleGroups[symbol]={};
    for(const bar of symbol===INST?["1m","5m","1H","4H","1D"]:["1H","4H","1D"]){
      candleGroups[symbol][bar]=queryCandleTail(symbol,bar,bar==="1m"?15:8);
      coverage.push(historicalCoverage(symbol,bar,bar==="1m"?3:bar==="5m"?7:bar==="1H"?14:90));
    }
  }
  const funding=archives.query("SELECT symbol,ts,realized_rate,funding_rate FROM funding_history WHERE symbol=? ORDER BY ts DESC LIMIT 16").all(INST).reverse();
  const oi=archives.query("SELECT symbol,ts,oi_usd FROM oi_history WHERE symbol=? ORDER BY ts DESC LIMIT 20").all(INST).reverse();
  const events=archives.query("SELECT ts,code,kind,price,source FROM event_history WHERE ts>=? ORDER BY ts DESC LIMIT 50").all(nowTs-48*3600000).reverse();
  const quality=(archives.query("PRAGMA quick_check").all() as any[]).map(x=>Object.values(x)[0]);
  return {schemaVersion:3,generatedAt:new Date(nowTs).toISOString(),source:"OKX official public APIs archived in Railway SQLite",
    archiveVerified:storageError===null&&quality.every(x=>x==="ok"),websocketQuoteHealthy:base.healthy,
    last:base.quote.last,mark:base.quote.mark,btc:base.btc,eth:base.eth,rolling60m:base.rolling60m,
    fundingCurrent:base.funding,openInterestCurrent:base.oi,
    archivedKlines:candleGroups,coverage,fundingHistory:funding,oiHistory:oi,thresholdEventHistory:events,
    sampleAnalytics24h:base.history24h,storage:base.storage,integrity:quality,
    caveats:["Public market only; no personal liquidation data","No interpolation of missing intervals",
      "Historical SPCX prices may cross a contract rebase; do not treat unadjusted early history as comparable",
      "Source timestamps must be checked at read time; report is a snapshot, not live quote streaming"]};
}

let bucketClient:any=null;
function configureBucket(){
  const vars=["S3_ENDPOINT","S3_BUCKET","S3_REGION","S3_ACCESS_KEY_ID","S3_SECRET_ACCESS_KEY"];
  if(vars.some(k=>!Bun.env[k])){backupError="missing Railway private bucket variable references";return null;}
  const endpoint=new URL(Bun.env.S3_ENDPOINT!);
  const bucket=Bun.env.S3_BUCKET!;
  if(!/^[a-z0-9][a-z0-9.-]+$/.test(bucket))throw new Error("invalid S3 bucket name");
  endpoint.hostname=bucket+"."+endpoint.hostname;
  return new Bun.S3Client({endpoint:endpoint.origin,bucket,region:Bun.env.S3_REGION||"auto",
    accessKeyId:Bun.env.S3_ACCESS_KEY_ID,secretAccessKey:Bun.env.S3_SECRET_ACCESS_KEY,virtualHostedStyle:true});
}
async function backupDatabase(){
  if(backupBusy)return;backupBusy=true;
  try{
    if(!bucketClient)bucketClient=configureBucket();
    if(!bucketClient)return;
    const data=archives.serialize();const stamp=new Date().toISOString().replace(/[:.]/g,"-");
    const name="snapshots/"+stamp+".sqlite";
    const length=await bucketClient.write(name,data,{type:"application/x-sqlite3",acl:"private"});
    if(length!==data.byteLength)throw new Error("S3 uploaded byte count mismatch");
    const exists=await bucketClient.exists(name);
    if(!exists)throw new Error("S3 HEAD verification failed");
    // Verify a small backup can actually be read back and opened as SQLite.
    // Cap restore-in-memory check to avoid exceeding Trial RAM on a large archive.
    if(length<=16*1024*1024){
      const downloaded=new Uint8Array(await bucketClient.file(name).arrayBuffer());
      if(downloaded.byteLength!==length)throw new Error("S3 restore download length mismatch");
      const testPath="/data/restore-test-"+Date.now()+".sqlite";
      await Bun.write(testPath,downloaded);
      let restored:Database|null=null;
      try{
        restored=new Database(testPath,{readonly:true,create:false});
        const integrity=(restored.query("PRAGMA quick_check").all() as any[]).map(x=>Object.values(x)[0]);
        if(!integrity.length||integrity.some(x=>x!=="ok"))throw new Error("Restored SQLite integrity invalid");
        const restoredRows=(restored.query("SELECT COUNT(*) AS count FROM quote_samples").get() as any)?.count;
        console.log("SPCX_DURABLE_BACKUP_RESTORE_TEST_OK",JSON.stringify({key:name,bytes:length,restoredQuoteRows:restoredRows}));
      }finally{
        if(restored)restored.close();
        await unlink(testPath).catch(()=>{});
      }
    }
    await bucketClient.write("latest.json",JSON.stringify({key:name,bytes:length,createdAt:new Date().toISOString(),
      quoteCount:(archives.query("SELECT COUNT(*) AS count FROM quote_samples").get() as any)?.count??0}),
      {type:"application/json",acl:"private"});
    lastBackupAt=Date.now();backupBytes=length;backupError=null;
    safeDb("backup-metadata",()=>{archives.query("INSERT OR REPLACE INTO archive_meta(key,value) VALUES(?,?)").run("last_verified_backup",JSON.stringify({ts:lastBackupAt,bytes:length,key:name}));});
    console.log("SPCX_DURABLE_BACKUP_OK",JSON.stringify({at:new Date(lastBackupAt).toISOString(),key:name,bytes:length}));
  }catch(err){backupError=String(err).slice(0,250);console.log("SPCX_DURABLE_BACKUP_FAIL",backupError);}
  finally{backupBusy=false;}
}
const historyPlans:{symbol:string;bar:string;days:number}[]=[
  {symbol:INST,bar:"1m",days:30},{symbol:INST,bar:"5m",days:90},
  {symbol:INST,bar:"1H",days:90},{symbol:INST,bar:"4H",days:180},{symbol:INST,bar:"1D",days:210},
  {symbol:"BTC-USDT-SWAP",bar:"1H",days:90},{symbol:"BTC-USDT-SWAP",bar:"4H",days:180},{symbol:"BTC-USDT-SWAP",bar:"1D",days:365},
  {symbol:"ETH-USDT-SWAP",bar:"1H",days:90},{symbol:"ETH-USDT-SWAP",bar:"4H",days:180},{symbol:"ETH-USDT-SWAP",bar:"1D",days:365}
];
let historyPlanIndex=0;
let historyBusy=false;
const archivedComplete=new Set<string>();
try{
  for(const row of archives.query("SELECT key FROM archive_meta WHERE key LIKE 'backfill_complete/%'").all() as any[]){
    archivedComplete.add(String(row.key).slice("backfill_complete/".length));
  }
}catch(err){console.log("SPCX_BACKFILL_META_LOAD_ERROR",String(err).slice(0,160));}
function completeBackfill(key:string){
  archivedComplete.add(key);
  safeDb("backfill-meta",()=>{archives.query("INSERT OR REPLACE INTO archive_meta(key,value) VALUES(?,?)").run("backfill_complete/"+key,new Date().toISOString());});
}
async function backfillNext(){
  if(historyBusy)return;historyBusy=true;
  try{
    let plan:any=null;
    for(let n=0;n<historyPlans.length;n++){
      const x=historyPlans[(historyPlanIndex++)%historyPlans.length];
      if(!archivedComplete.has(x.symbol+"/"+x.bar)){plan=x;break;}
    }
    if(!plan)return;
    const key=plan.symbol+"/"+plan.bar;
    const earliest=archives.query("SELECT MIN(ts) AS ts FROM candles WHERE symbol=? AND bar=?").get(plan.symbol,plan.bar) as any;
    const cursor=Number(earliest?.ts??0);
    const stop=Date.now()-plan.days*86400000;
    if(cursor&&cursor<=stop){completeBackfill(key);return;}
    let path="/api/v5/market/history-candles?instId="+encodeURIComponent(plan.symbol)+"&bar="+plan.bar+"&limit=100";
    if(cursor)path+="&after="+cursor;
    const rows=await fetchPublicRows(path);
    backfillRequests++;
    if(rows.length===0){
      completeBackfill(key);
      console.log("SPCX_BACKFILL_REACHED_SOURCE_LIMIT",JSON.stringify({key,requests:backfillRequests,oldestAvailable:cursor}));
      return;
    }
    const r=archiveBars(plan.symbol,plan.bar,rows,"OKX-official-history-candles");
    if(!r.accepted || (cursor&&r.oldest!==null&&r.oldest>=cursor)) completeBackfill(key);
    if(backfillRequests<=11||backfillRequests%30===0||archivedComplete.has(key))
      console.log("SPCX_BACKFILL_PROGRESS",JSON.stringify({key,rows:r.accepted,oldest:r.oldest,requests:backfillRequests,complete:archivedComplete.has(key)}));
  }catch(err){backfillFailures++;console.log("SPCX_BACKFILL_RETRY",String(err).slice(0,200));}
  finally{historyBusy=false;}
}
async function archiveFunding(){
  try{
    const path="/api/v5/public/funding-rate-history?instId="+INST+"&limit=100";
    let accepted=0;
    const save=(rows:any[])=>{
      for(const r of rows){
        if(r.instId!==INST)continue;
        const ts=Number(r.fundingTime),settled=r.realizedRate===""||r.realizedRate==null?null:Number(r.realizedRate);
        const indicated=r.fundingRate===""||r.fundingRate==null?null:Number(r.fundingRate);
        if(!Number.isFinite(ts)||ts>Date.now()+10000||ts<=0)continue;
        if(settled!==null&&!Number.isFinite(settled))continue;
        if(indicated!==null&&!Number.isFinite(indicated))continue;
        insFunding.run(INST,ts,settled,indicated,"OKX-public-settled-funding");accepted++;
      }
    };
    save(await fetchPublicRows(path)); // Always refresh latest settlements
    for(let page=0;page<2;page++){
      const row=archives.query("SELECT MIN(ts) AS ts FROM funding_history WHERE symbol=?").get(INST) as any;
      const cursor=Number(row?.ts??0);
      if(!cursor||cursor<=Date.now()-90*86400000)break;
      const older=await fetchPublicRows(path+"&after="+cursor);
      if(!older.length)break;
      save(older);
      if(Number(older.at(-1)?.fundingTime)>=cursor)break;
    }
    console.log("SPCX_FUNDING_ARCHIVED",JSON.stringify({processed:accepted,total:(archives.query("SELECT COUNT(*) AS count FROM funding_history WHERE symbol=?").get(INST) as any)?.count}));
  }catch(err){console.log("SPCX_FUNDING_ERROR",String(err).slice(0,220));}
}
console.log("SPCX_DURABLE_DATABASE_READY",JSON.stringify({path:MARKET_DB_PATH,counts:archiveSnapshotQuality()}));

type MarketContext = {checkedAt:string; btc?:any; eth?:any; spcx?:any; funding?:any; openInterest?:any; errors:string[]};
let marketContext:MarketContext|null=null;
let marketContextAt=0;
let marketContextAttemptAt=0;
let marketContextInFlight=false;
async function fetchPublicRows(path:string):Promise<any[]>{
  const errors:string[]=[];
  for(const base of SOURCES){
    const controller=new AbortController();
    const cancel=setTimeout(()=>controller.abort(),7000);
    try{
      const response=await fetch(base+path,{headers:{Accept:"application/json"},signal:controller.signal});
      if(!response.ok)throw new Error("HTTP "+response.status);
      const object=await response.json() as any;
      if(object?.code!=="0"||!Array.isArray(object?.data))throw new Error("Bad OKX data code="+String(object?.code)+" msg="+String(object?.msg).slice(0,60));
      if(object.data.length===0 && !path.includes("/history-candles"))throw new Error("Empty OKX data");

      return object.data;
    }catch(error){errors.push(base+" "+String(error).slice(0,90));}
    finally{clearTimeout(cancel);}
  }
  throw new Error(errors.join(" / "));
}
function closedCandleRows(raw:any[],maxBars=8):any[]{
  const items=raw.filter(v=>Array.isArray(v)&&v.length>=9&&String(v[8])==="1").map(v=>({
    ts:Number(v[0]),open:Number(v[1]),high:Number(v[2]),low:Number(v[3]),close:Number(v[4]),volume:Number(v[5])
  })).filter(v=>Number.isFinite(v.ts)&&[v.open,v.high,v.low,v.close,v.volume].every(Number.isFinite)&&v.open>0&&v.close>0&&v.high>=Math.max(v.open,v.close)&&v.low<=Math.min(v.open,v.close));
  items.sort((a,b)=>a.ts-b.ts);
  return items.slice(-maxBars);
}
function closedMinuteRisk(raw:any[]):any|null{
  const bars=closedCandleRows(raw,80);
  if(bars.length<61)return null;
  const last=bars.slice(-61);
  for(let i=1;i<last.length;i++)if(last[i].ts-last[i-1].ts!==60_000)return null;
  const anchor=last.at(-1)!;
  if(now()-anchor.ts<60_000||now()-anchor.ts>180_000)return null;
  let peak=last[0].close,worst=0;
  for(const row of last){peak=Math.max(peak,row.close);worst=Math.min(worst,100*(row.close/peak-1));}
  const close=last.at(-1)!.close;
  return {source:"OKX confirmed 1m close",firstTs:last[0].ts,lastTs:anchor.ts,
    points:last.length,spanMinutes:60,
    change5mPct:Number((100*(close/last[55].close-1)).toFixed(4)),
    change15mPct:Number((100*(close/last[45].close-1)).toFixed(4)),
    change60mPct:Number((100*(close/last[0].close-1)).toFixed(4)),
    maxCloseDrawdown60mPct:Number(worst.toFixed(4))};
}
function tickerContext(rows:any[],symbol:string,at:number):any{
  const x=rows[0];
  if(x?.instId!==symbol||x.instType!=="SWAP")throw new Error("wrong symbol "+symbol);
  const ts=Number(x.ts),last=Number(x.last);
  if(!Number.isFinite(ts)||!Number.isFinite(last)||last<=0||ts>at+10000||at-ts>120000)throw new Error("stale "+symbol);
  return {instId:symbol,last,ts,ageMs:at-ts,open24h:Number(x.open24h),high24h:Number(x.high24h),low24h:Number(x.low24h)};
}
async function refreshMarketContext(){
  if(marketContextInFlight)return;
  marketContextInFlight=true;marketContextAttemptAt=now();
  const queries=[
    ["btc","/api/v5/market/ticker?instId=BTC-USDT-SWAP"],
    ["eth","/api/v5/market/ticker?instId=ETH-USDT-SWAP"],
    ["oneMinute","/api/v5/market/candles?instId=SPCX-USDT-SWAP&bar=1m&limit=75"],
    ["oneHour","/api/v5/market/candles?instId=SPCX-USDT-SWAP&bar=1H&limit=12"],
    ["fourHours","/api/v5/market/candles?instId=SPCX-USDT-SWAP&bar=4H&limit=12"],
    ["daily","/api/v5/market/candles?instId=SPCX-USDT-SWAP&bar=1D&limit=12"],
    ["btc1H","/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1H&limit=16"],
    ["btc4H","/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=4H&limit=16"],
    ["btc1D","/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1D&limit=16"],
    ["eth1H","/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=1H&limit=16"],
    ["eth4H","/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=4H&limit=16"],
    ["eth1D","/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=1D&limit=16"],
    ["funding","/api/v5/public/funding-rate?instId=SPCX-USDT-SWAP"],
    ["openInterest","/api/v5/public/open-interest?instType=SWAP&instId=SPCX-USDT-SWAP"]
  ] as const;
  try{
    const results=await Promise.allSettled(queries.map(q=>fetchPublicRows(q[1])));
    const obj:MarketContext={checkedAt:new Date().toISOString(),errors:[],spcx:{}};
    const at=now();
    for(let i=0;i<results.length;i++){
      const key=queries[i][0],result=results[i];
      if(result.status!=="fulfilled"){obj.errors.push(key+": "+String(result.reason).slice(0,150));continue;}
      const rows=result.value;
      try{
        const candleSpec:Record<string,[string,string]>={
          oneMinute:[INST,"1m"],oneHour:[INST,"1H"],fourHours:[INST,"4H"],daily:[INST,"1D"],
          btc1H:["BTC-USDT-SWAP","1H"],btc4H:["BTC-USDT-SWAP","4H"],btc1D:["BTC-USDT-SWAP","1D"],
          eth1H:["ETH-USDT-SWAP","1H"],eth4H:["ETH-USDT-SWAP","4H"],eth1D:["ETH-USDT-SWAP","1D"]};
        if(candleSpec[key])safeDb("candles-"+key,()=>{archiveBars(candleSpec[key][0],candleSpec[key][1],rows,"OKX-official-candles");});
        if(key==="btc")obj.btc=tickerContext(rows,"BTC-USDT-SWAP",at);
        else if(key==="eth")obj.eth=tickerContext(rows,"ETH-USDT-SWAP",at);
        else if(key==="oneMinute"){
          obj.spcx.rolling60m=closedMinuteRisk(rows);
          if(!obj.spcx.rolling60m)obj.errors.push("oneMinute: insufficient fresh confirmed contiguous candles");
        }
        else if(key==="oneHour")obj.spcx.candles1H=closedCandleRows(rows,8);
        else if(key==="fourHours")obj.spcx.candles4H=closedCandleRows(rows,8);
        else if(key==="daily")obj.spcx.candles1D=closedCandleRows(rows,8);
        else if(key==="funding"){
          const v=rows[0];
          if(v.instId!=="SPCX-USDT-SWAP")throw new Error("wrong symbol");
          obj.funding={fundingRate:Number(v.fundingRate),nextFundingTime:Number(v.nextFundingTime)};
        } else if(key==="openInterest"){
          const v=rows[0],ts=Number(v.ts);
          if(v.instId!=="SPCX-USDT-SWAP"||at-ts>300000||ts>at+10000)throw new Error("stale OI");
          obj.openInterest={oiUsd:Number(v.oiUsd),ts};
          if(Number.isFinite(obj.openInterest.oiUsd)&&obj.openInterest.oiUsd>0)safeDb("openInterest",()=>insOi.run(INST,ts,obj.openInterest.oiUsd,"OKX-public-open-interest"));
        }
      }catch(error){obj.errors.push(key+": "+String(error).slice(0,150));}
    }
    marketContext=obj;marketContextAt=now();
    console.log("SPCX_MARKET_CONTEXT_UPDATED",JSON.stringify({at:obj.checkedAt,
      btc:obj.btc?.last??null,eth:obj.eth?.last??null,
      minutePoints:obj.spcx?.rolling60m?.points??0,errors:obj.errors}));
  }catch(err){console.log("SPCX_MARKET_CONTEXT_ERROR",String(err).slice(0,200));}
  finally{marketContextInFlight=false;}
}

function spin(){
  const time=now();
  // Export a source-timestamped snapshot to Railway runtime logs once per minute.
  // This bridge works even when the ChatGPT web fetcher cannot parse Railway JSON.
  if(time-marketContextAttemptAt>=120_000)void refreshMarketContext();
  if(time-lastPeriodicLogAt>=60_000){
    lastPeriodicLogAt=time;
    const state=snapshot();
    archiveQuoteSample(state);
    console.log("SPCX_ARCHIVE_STATUS",JSON.stringify({asOf:state.asOf,dbHealthy:storageError===null,quotes:lastStoredAt,backup:lastBackupAt,backupError}));
    if(Math.floor(time/60000)%5===0){
      console.log("SPCX_GPT_REPORT",JSON.stringify(gptReport()));
    }
    console.log("SPCX_FEED_SAMPLE",JSON.stringify({asOf:state.asOf,healthy:state.healthy,
      symbol:state.symbol,last:state.last,mark:state.mark,websocket:state.websocket,
      restFallback:state.restFallback,activePriceThresholds:state.priceAlertsActive,
      sampled60m:state.sampled60m,
      marketContext:marketContext&&time-marketContextAt<=300000?marketContext:null}));
  }
  // Fallback if WS tickers/mark are stale. Do not pretend fallback is a tick stream.
  if(!fresh(last,time)||!fresh(mark,time)||time-lastRestSuccess>60000)void pollRest();
  if(wsConnected && ws && ws.readyState===WebSocket.OPEN){
    if(time-lastWsMessage>12000){try{ws.send("ping")}catch{}}
    if(time-lastWsMessage>50000){try{ws.close()}catch{}}
  }else if(!ws || ws.readyState===WebSocket.CLOSED) connect();
  setTimeout(spin,5000);
}
function snapshot(){
  const at=now();
  const ok=fresh(last,at)&&fresh(mark,at);
  const valid=samples.filter(x=>at-x.ts>=0 && at-x.ts<=3600000);
  const calc= valid.length>=2? {
    points:valid.length,
    spanMinutes:Math.round((valid.at(-1)!.ts-valid[0].ts)/60000),
    firstTs:valid[0].ts,lastTs:valid.at(-1)!.ts,
    returnPct:Number(((valid.at(-1)!.price/valid[0].price)-1)*100).toFixed(4),
    sampleOnly:true
  }:null;
  return {symbol:INST,asOf:new Date(at).toISOString(),healthy:!!ok,
    last:last?{price:last.price,source:last.source,ts:last.ts,ageMs:at-last.ts}:null,
    mark:mark?{price:mark.price,source:mark.source,ts:mark.ts,ageMs:at-mark.ts}:null,
    websocket:{connected:wsConnected,lastMessageAt:lastWsMessage?new Date(lastWsMessage).toISOString():null,messages:wsMessages,reconnects:reconnectCount,error:lastWsError||null},
    restFallback:{lastSuccessAt:lastRestSuccess?new Date(lastRestSuccess).toISOString():null,error:lastRestError||null},
    monitoring:"continuous WS while running; REST fallback; server timestamps; not a guarantee against exchange/network/hosting outages",
    priceAlertsActive:[...active],sampled60m:calc,
    marketContext:marketContext&&at-marketContextAt<=300000?marketContext:null};
}
const server=Bun.serve({
  port,
  fetch(req){
    const url=new URL(req.url);
    if(url.pathname.startsWith("/v1/")){
      let body:any;const key=url.pathname;
      try{
        if(key==="/v1/storage")body=archiveSnapshotQuality();
        else if(key==="/v1/summary"||key==="/v1/analytics")body=shortAnalyticsReport();
        else if(key==="/v1/gpt"){
          body=gptReport();
          console.log("SPCX_GPT_REPORT",JSON.stringify(body));
        }
        else if(key==="/v1/history"){
          body=historyView(url);
          // ChatGPT can retrieve bounded raw history by triggering this endpoint
          // through the connected Railway test-webhook tool, then reading runtime logs.
          console.log("SPCX_HISTORY_QUERY",JSON.stringify({...body,rows:body?.rows?.slice(-100)}));
        }
        else if(key==="/v1/events"){
          const limit=Math.min(200,Math.max(1,Number(url.searchParams.get("limit")||100)||100));
          body={events:(archives.query("SELECT ts,code,kind,price,source FROM event_history ORDER BY ts DESC LIMIT ?").all(limit) as any[]).reverse()};
        }
        else if(key==="/v1/funding")body={rows:archives.query("SELECT symbol,ts,realized_rate,funding_rate FROM funding_history ORDER BY ts DESC LIMIT 200").all()};
        else if(key==="/v1/oi")body={rows:archives.query("SELECT symbol,ts,oi_usd FROM oi_history ORDER BY ts DESC LIMIT 200").all()};
        else if(key==="/v1/quality"){
          const result=archives.query("PRAGMA quick_check").all();
          body={at:new Date().toISOString(),integrity:result,storage:archiveSnapshotQuality(),
            monitored:!!(fresh(last)&&fresh(mark)),contextAgeMs:marketContext?Date.now()-Date.parse(marketContext.checkedAt):null};
        }
        else return new Response("not found",{status:404});
        if(key==="/v1/summary"||key==="/v1/quality")console.log("SPCX_ARCHIVE_QUERY",JSON.stringify({endpoint:key,asOf:new Date().toISOString(),summary:{samples:body?.coverage?.snapshots?.count??body?.storage?.samples??null,storageHealthy:storageError===null}}));
        return Response.json(body,{status:body?.error?400:200,headers:{"Cache-Control":"no-store","Access-Control-Allow-Origin":"*"}});
      }catch(err){console.log("SPCX_ARCHIVE_QUERY_ERROR",String(err));return Response.json({error:"archive query unavailable",storageError},{status:503});}
    }
    if(url.pathname==="/"||url.pathname==="/latest"||url.pathname==="/health"||url.pathname==="/events"){
      const data=snapshot();
      if(url.pathname==="/health") console.log("SPCX_FEED_DIAGNOSTIC",JSON.stringify({asOf:data.asOf,healthy:data.healthy,last:data.last,mark:data.mark,websocket:data.websocket,restFallback:data.restFallback}));
      const diskOk=storageError===null && (Date.now()-startAt<120000 || Date.now()-lastStoredAt<150000);
      const response=url.pathname==="/events"?{...data,events:events.slice(-100)}:url.pathname==="/health"?
        {status:data.healthy&&diskOk?"ok":"degraded",at:data.asOf,lastAgeMs:data.last?.ageMs??null,markAgeMs:data.mark?.ageMs??null,
         wsConnected:data.websocket.connected,wsMessages:data.websocket.messages,restError:data.restFallback.error,
         archive:{healthy:diskOk,lastStoredAt,writeError:storageError,backupAt:lastBackupAt,backupError}}:
        {...data,archive:{healthy:diskOk,lastStoredAt,backupAt:lastBackupAt}};
      return Response.json(response,{status:(url.pathname==="/health"?!(data.healthy&&diskOk):!data.healthy)?503:200,
        headers:{"Cache-Control":"no-store","Access-Control-Allow-Origin":"*"}});
    }
    return new Response("not found",{status:404});
  }
});
console.log("SPCX_MONITOR_STARTED",new Date(startAt).toISOString(),"port",server.port);
connect();
void pollRest();
void refreshMarketContext();
void archiveFunding();
setTimeout(backupDatabase,60000);
setInterval(backupDatabase,6*3600*1000);
setInterval(backfillNext,5000);
setInterval(archiveFunding,4*3600*1000);
setTimeout(spin,5000);
