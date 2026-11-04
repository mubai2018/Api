import datetime as dt
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
import alert_lifecycle as alerts

NOW=1793814400000
TS=NOW-8000

def snapshot(t,events,book=True,rolling=True):
    return {"checkedAt":dt.datetime.fromtimestamp(t/1000,dt.timezone.utc).isoformat(),
            "last":138.0,"markPx":138.01,"events":events,
            "book":{"ts":t-1000} if book else None,
            "rolling60m":{"points":60} if rolling else None}

def event(code,severity="high",value=1):
    return {"code":code,"severity":severity,"value":value,"unit":"%","sourceTs":TS}

class LifecycleTests(unittest.TestCase):
    def test_high_open_only_once_then_two_sample_recovery(self):
        a=snapshot(TS,[event("FALL_5M")])
        st,trans=alerts.update(None,a,TS)
        self.assertEqual([(x["code"],x["transition"]) for x in trans],[("FALL_5M","opened")])
        st,trans=alerts.update(st,a,TS)
        self.assertEqual(trans,[])
        st,trans=alerts.update(st,snapshot(TS+60000,[event("FALL_5M")]),TS+60000)
        self.assertEqual(trans,[])
        st,trans=alerts.update(st,snapshot(TS+120000,[]),TS+120000)
        self.assertEqual(trans,[])
        st,trans=alerts.update(st,snapshot(TS+180000,[]),TS+180000)
        self.assertEqual(trans[0]["transition"],"recovered")
        self.assertEqual(st["activeCount"],0)

    def test_medium_needs_two_samples_and_escalates(self):
        st,trans=alerts.update(None,snapshot(TS,[event("SPREAD_WIDE","medium")]),TS)
        self.assertEqual(trans,[])
        st,trans=alerts.update(st,snapshot(TS+60000,[event("SPREAD_WIDE","medium")]),TS+60000)
        self.assertEqual(trans[0]["transition"],"opened")
        st,trans=alerts.update(st,snapshot(TS+120000,[event("SPREAD_WIDE","high")]),TS+120000)
        self.assertEqual(trans[0]["transition"],"escalated")

    def test_missing_book_does_not_resolve_book_alarm(self):
        st,_=alerts.update(None,snapshot(TS,[event("BID_DEPTH_COLLAPSE")]),TS)
        st,x=alerts.update(st,snapshot(TS+60000,[],book=False),TS+60000)
        st,y=alerts.update(st,snapshot(TS+120000,[],book=False),TS+120000)
        self.assertFalse(x or y)
        self.assertIn("BID_DEPTH_COLLAPSE",st["active"])

    def test_quote_staleness_and_mismatch_rejected(self):
        q={"ok":True,"source":"OKX official public REST API",
           "checkedAt":dt.datetime.fromtimestamp((NOW-15000)/1000,dt.timezone.utc).isoformat(),
           "symbols":{"SPCX-USDT-SWAP":{"last":138.0,"markPx":138.01,
             "lastTs":NOW-20000,"markTs":NOW-20000}}}
        r={"ok":True,"source":"OKX official public market data","symbol":"SPCX-USDT-SWAP",
           "checkedAt":dt.datetime.fromtimestamp((NOW-10000)/1000,dt.timezone.utc).isoformat(),
           "quoteCheckedAt":q["checkedAt"],"last":138.0,"markPx":138.01,
           "lastTs":NOW-20000,"markTs":NOW-20000,"events":[]}
        self.assertTrue(alerts.validate(r,q,NOW))
        with self.assertRaises(ValueError):
            alerts.validate(r,q,NOW+700000)
        r["quoteCheckedAt"]="2025-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            alerts.validate(r,q,NOW)

    def test_unknown_event_rejected(self):
        q={"ok":True,"source":"OKX official public REST API",
           "checkedAt":dt.datetime.fromtimestamp((NOW-15000)/1000,dt.timezone.utc).isoformat(),
           "symbols":{"SPCX-USDT-SWAP":{"last":138.0,"markPx":138.01,
             "lastTs":NOW-20000,"markTs":NOW-20000}}}
        r={"ok":True,"source":"OKX official public market data","symbol":"SPCX-USDT-SWAP",
           "checkedAt":dt.datetime.fromtimestamp((NOW-10000)/1000,dt.timezone.utc).isoformat(),
           "quoteCheckedAt":q["checkedAt"],"last":138.0,"markPx":138.01,
           "lastTs":NOW-20000,"markTs":NOW-20000,
           "events":[{"code":"UNKNOWN","severity":"critical","sourceTs":NOW-20000}]}
        with self.assertRaises(ValueError):
            alerts.validate(r,q,NOW)

if __name__=="__main__":unittest.main()
