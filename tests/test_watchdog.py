import datetime as dt
import pathlib
import sys
import unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"scripts"))
import watchdog as wd

NOW=1793814400000
def iso(ms):
    return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()
def fixtures(age=3):
    t=NOW-age*60000
    latest={"ok":True,"source":"OKX official public REST API",
            "checkedAt":iso(t),"symbols":{wd.SYMBOL:{"instId":wd.SYMBOL,
                "lastTs":t-1000,"markTs":t-2000}}}
    risk={"ok":True,"symbol":wd.SYMBOL,"quoteCheckedAt":latest["checkedAt"],
          "checkedAt":iso(t+500)}
    pipeline={"status":"healthy","attemptedAt":iso(t+1000),
              "steps":{"quote":"success","archive":"success","risk":"success","lifecycle":"success"}}
    return latest,risk,pipeline

class WatchdogTests(unittest.TestCase):
    def test_fresh_data_healthy(self):
        self.assertEqual(wd.grade(*fixtures(4),NOW)[0],"healthy")
    def test_stale_then_outage(self):
        self.assertEqual(wd.grade(*fixtures(13),NOW)[0],"stale")
        self.assertEqual(wd.grade(*fixtures(22),NOW)[0],"outage")
    def test_pipeline_failure_is_outage_not_price_alert(self):
        latest,risk,pipeline=fixtures(4)
        pipeline["steps"]["risk"]="failure"
        status,issues,ages=wd.grade(latest,risk,pipeline,NOW)
        self.assertEqual(status,"outage")
        self.assertIn("last_pipeline_attempt_failed",issues)
    def test_no_source_outage(self):
        status,issues,_=wd.grade(None,None,None,NOW)
        self.assertEqual(status,"outage")
        self.assertTrue(issues)
    def test_outage_dedup_and_two_healthy_recovery(self):
        state,changes=wd.transition(None,"outage",NOW)
        self.assertEqual(changes[0]["transition"],"opened")
        state,changes=wd.transition(state,"outage",NOW+60000)
        self.assertEqual(changes,[])
        state,changes=wd.transition(state,"healthy",NOW+120000)
        self.assertEqual(changes,[])
        state,changes=wd.transition(state,"healthy",NOW+180000)
        self.assertEqual(changes[0]["transition"],"recovered")
        self.assertFalse(state["outageActive"])
    def test_stale_does_not_recover(self):
        state,_=wd.transition(None,"outage",NOW)
        state,_=wd.transition(state,"healthy",NOW+60000)
        state,_=wd.transition(state,"stale",NOW+120000)
        self.assertTrue(state["outageActive"])
        self.assertEqual(state["healthyStreak"],0)

if __name__=="__main__":unittest.main()
