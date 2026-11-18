import sys
from pathlib import Path
import unittest
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))
import test_review_20261108_desktop_audit as fixture_api

class RejectedJobStatusTests(unittest.TestCase):
    def test_independent_rejection_cannot_look_like_waiting_for_handoff(self):
        fixture = fixture_api.DesktopAuditRegression();fixture.setUp()
        try:
            bridge, forest = fixture.pending()
            task = forest.bus.find_message(forest.snapshot()["leaves"][0]["task_id"])
            bridge.call("claim_audit", {"leaf_id":"work", "agent":"reviewer"})
            response = fixture.decision(bridge, decision="reject", reason="Need a clearer acceptance case")
            self.assertTrue(response["ok"], response)
            paths = [p for a in forest.bus.agents_dir.iterdir() for d in ("ready","processing","blocked","done") for p in (a/d).glob("*.json")]
            paths.extend([forest.path, fixture.workspace / ".agentbus/jobs" / (task.job_id + ".json")])
            before = {p:p.read_bytes() for p in paths}
            from agentbus import jobs
            state=jobs.status_job(fixture.cfg, task.job_id)
            self.assertEqual(state["state"], "blocked")
            self.assertIn("rejected", state["reason"])
            self.assertIn("Need a clearer acceptance case", state["reason"])
            self.assertEqual(forest.snapshot()["leaves"][0]["state"], "rejected")
            self.assertEqual(before,{p:p.read_bytes() for p in paths})
        finally:fixture.doCleanups()
