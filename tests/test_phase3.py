import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
import phase3

class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_root=phase3.ROOT
        phase3.ROOT=pathlib.Path(self.tmp.name)/"data"
        self.now=1793810000000
    def tearDown(self):
        phase3.ROOT=self.old_root
        self.tmp.cleanup()

    def test_book_notional_and_spread(self):
        bid=[["135.00","10","0","1"],["134.90","20","0","1"]]
        ask=[["135.10","10","0","1"],["135.20","20","0","1"]]
        row={"ts":str(self.now-1000),"bids":bid,"asks":ask}
        result=phase3.book_metrics([row],1.0,self.now)
        self.assertAlmostEqual(result["depthBidUsd05pct"],1350+2698)
        self.assertGreater(result["spreadBps"],0)
        self.assertLess(result["spreadBps"],35)

    def test_book_crossed_fails_closed(self):
        book={"ts":str(self.now-1000),
              "bids":[["135.5","10"],["135.4","10"]],
              "asks":[["135.4","10"],["135.6","10"]]}
        with self.assertRaises(ValueError):
            phase3.book_metrics([book],1,self.now)

    def test_recent_taker_trades_notional_contract_units(self):
        data=[
            {"instId":phase3.SYMBOL,"side":"buy","px":"135","sz":"3","ts":str(self.now-3000),"tradeId":"1"},
            {"instId":phase3.SYMBOL,"side":"sell","px":"135","sz":"1","ts":str(self.now-1500),"tradeId":"2"},
        ]
        result=phase3.trades_metrics(data,1,self.now)
        self.assertAlmostEqual(result["takerBuyRatio"],.75)
        self.assertEqual(result["sampleCount"],2)

    def test_price_threshold_and_fall_signals(self):
        q={"last":108,"markPx":108.1,"lastTs":self.now-1000,"markTs":self.now-2000,
           "rolling60m":{"points":60,"toTs":self.now-75000,"net5mPct":-3.5,
                         "net15mPct":-5.1,"netReturnPct":-6,
                         "maxCloseDrawdownPct":-7},
           "openInterestUsd":100_000,"openInterestTs":self.now-1000}
        events,context=phase3.evaluate(self.now,q,None,None,[])
        codes={x["code"] for x in events}
        self.assertIn("PRICE_BELOW_120",codes)
        self.assertIn("PRICE_BELOW_110",codes)
        self.assertNotIn("PRICE_BELOW_100",codes)
        self.assertIn("FALL_5M",codes)
        self.assertIn("DRAWDOWN_60M",codes)

    def test_missing_optional_data_does_not_fake_alert(self):
        q={"last":135,"markPx":135.1,"lastTs":self.now-1000,"markTs":self.now-2000}
        events,notes=phase3.evaluate(self.now,q,None,None,[])
        self.assertEqual(events,[])
        self.assertTrue(notes)

    def test_reject_stale_ticker(self):
        phase3.ROOT.mkdir(parents=True)
        item={"ok":True,"source":"OKX official public REST API",
              "checkedAt":dt.datetime.fromtimestamp((self.now-240000)/1000,dt.timezone.utc).isoformat(),
              "symbols":{}}
        phase3.save(phase3.ROOT/"latest.json",item)
        with self.assertRaises(ValueError):
            phase3.read_inputs(self.now)

if __name__=="__main__":
    unittest.main()
