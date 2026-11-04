"""Offline unit checks for phase-1 public data validation."""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import phase1

class Phase1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = phase1.ROOT
        phase1.ROOT = pathlib.Path(self.temp.name) / "data"

    def tearDown(self):
        phase1.ROOT = self.old_root
        self.temp.cleanup()

    def sample(self):
        now = phase1.msnow()
        return {
            "checkedAt": dt.datetime.fromtimestamp(now / 1000, dt.timezone.utc).isoformat(),
            "ok": True, "symbols": {
                symbol: {
                    "instId": symbol, "last": 100, "markPx": 100.01,
                    "lastTs": now - 3000, "markTs": now - 2000,
                    "fundingRate": 0.0001, "openInterestUsd": 100000,
                    "rolling60m": {"points": 60} if symbol == "SPCX-USDT-SWAP" else None
                } for symbol in phase1.SYMBOLS
            }, "supplementalErrors": []
        }

    def test_candle_reject_unconfirmed_and_bad_ohlc(self):
        good = [str(3_600_000 * 400000), "100", "110", "95", "103", "9", "1", "1", "1"]
        self.assertIsNotNone(phase1.parse_candle(good, "1H", 2_000_000_000_000))
        self.assertIsNone(phase1.parse_candle(good[:-1] + ["0"], "1H", 2_000_000_000_000))
        with self.assertRaises(ValueError):
            phase1.parse_candle(good[:2] + ["98"] + good[3:], "1H", 2_000_000_000_000)

    def test_utc8_daily_candle_alignment(self):
        midnight_utc8 = int(dt.datetime(2026, 11, 4, 16, tzinfo=dt.timezone.utc).timestamp() * 1000)
        row = [str(midnight_utc8), "100", "110", "90", "105", "5", "1", "1", "1"]
        self.assertIsNotNone(phase1.parse_candle(row, "1D", midnight_utc8 + 100_000_000))

    def test_funding_uses_realized_rate(self):
        old_call = phase1.call
        old_sleep = phase1.time.sleep
        try:
            now = phase1.msnow() - 3_600_000
            phase1.call = lambda path: [{
                "instId": "SPCX-USDT-SWAP",
                "fundingTime": str(now),
                "fundingRate": "0.00010",
                "realizedRate": "0.00012"
            }]
            phase1.time.sleep = lambda _: None
            records, pages = phase1.fetch_funding("SPCX-USDT-SWAP", "incremental")
            self.assertEqual(pages, 1)
            self.assertEqual(records[0][1], 0.00012)
            self.assertEqual(records[0][2], 0.00010)
        finally:
            phase1.call = old_call
            phase1.time.sleep = old_sleep

    def test_deduplicated_archive_and_quality_report(self):
        src = self.sample()
        phase1.write_json(phase1.ROOT / "latest.json", src)
        phase1.snapshot()
        phase1.snapshot()
        market = list((phase1.ROOT / "history" / "market").glob("*.jsonl"))
        self.assertEqual(len(market), 1)
        self.assertEqual(len(phase1.jsonl_rows(market[0])), 1)
        quality = phase1.read_json(phase1.ROOT / "quality" / "latest.json")
        self.assertIn(quality["status"], ("ok", "degraded"))
        self.assertEqual(quality["marketSamplesToday"], 1)

    def test_reject_stale_market(self):
        src = self.sample()
        src["checkedAt"] = (phase1.utcnow() - dt.timedelta(minutes=20)).isoformat()
        phase1.write_json(phase1.ROOT / "latest.json", src)
        with self.assertRaises(ValueError):
            phase1.snapshot()

    def test_monthly_merge_deduplicates_by_timestamp(self):
        prefix = phase1.ROOT / "history" / "candles" / "SPCX-USDT-SWAP" / "1H"
        time_a = int(dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
        a = [time_a, 100, 101, 99, 100, 5]
        b = [time_a + 3_600_000, 101, 102, 100, 102, 3]
        phase1.persist_months(prefix, [a, b])
        phase1.persist_months(prefix, [a, b])
        self.assertEqual(phase1.stats(prefix, 3_600_000)["count"], 2)

if __name__ == "__main__":
    unittest.main()
