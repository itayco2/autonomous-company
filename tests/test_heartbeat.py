"""The heartbeat's decisions, fed synthetic readings: no Docker, no model, no clock.

Run:  python3 -m unittest discover -s tests
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import heartbeat  # noqa: E402
from heartbeat import Meter, classify_result, highest_cycle, quick_failure  # noqa: E402

WEEK = 1791284400


def reading(week_used, week=WEEK, status="allowed", overage="rejected", five_hour_reset=1790793600, **extra):
    return {"status": status, "overageStatus": overage, "isUsingOverage": False,
            "unifiedWindows": {"five_hour": {"utilization": 0.3, "resetsAt": five_hour_reset},
                               "seven_day": {"utilization": week_used, "resetsAt": week}}, **extra}


def meter(share=20, used=0.0, week=WEEK):
    return Meter({"next_cycle": 2, "week": week, "week_used_pp": used}, share)


class Share(unittest.TestCase):
    def test_only_growth_inside_a_cycle_counts(self):
        m = meter()
        for used in (0.12, 0.13, 0.15):
            self.assertIsNone(m.read(reading(used)))
        self.assertEqual(m.state["week_used_pp"], 3.0)

    def test_the_first_reading_of_a_cycle_is_only_a_baseline(self):
        # Between cycles the owner may have used the account; that is not the company's share.
        m = meter(used=3.0)
        m.last = None
        m.read(reading(0.40))
        self.assertEqual(m.state["week_used_pp"], 3.0)

    def test_the_cap_ends_the_cycle(self):
        m = meter(share=20, used=19.0)
        m.read(reading(0.50))
        self.assertEqual(m.read(reading(0.51)), "weekly_share")

    def test_a_new_week_starts_the_share_from_zero(self):
        m = meter(used=19.5)
        m.read(reading(0.60))
        self.assertIsNone(m.read(reading(0.01, week=WEEK + 604800)))
        self.assertEqual((m.state["week"], m.state["week_used_pp"]), (WEEK + 604800, 0.0))

    def test_a_falling_reading_never_lowers_the_share(self):
        m = meter(used=5.0)
        m.read(reading(0.30))
        m.read(reading(0.29))
        self.assertEqual(m.state["week_used_pp"], 5.0)


class Stops(unittest.TestCase):
    def test_paid_overage_available_or_in_use_stops_everything(self):
        self.assertEqual(meter().read(reading(0.1, overage="allowed")), "overage")
        self.assertEqual(meter().read(reading(0.1, isUsingOverage=True)), "overage")
        self.assertIsNone(meter().read(reading(0.1, overage="rejected")))

    def test_a_rejected_window_is_a_rate_limit_with_its_reset_time(self):
        m = meter()
        self.assertEqual(m.read(reading(0.1, status="rejected", resetsAt=1790800000)), "rate_limited")
        self.assertEqual(m.five_hour_reset, 1790800000)

    def test_a_bad_token_stops_for_good(self):
        for text in ("Failed to authenticate. API Error: 401 OAuth access token is invalid.",
                     "Not logged in · Please run /login"):
            self.assertEqual(classify_result({"is_error": True, "result": text}), "auth")

    def test_a_429_is_a_rate_limit_even_when_subtype_says_success(self):
        event = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429, "result": "x"}
        self.assertEqual(classify_result(event), "rate_limited")

    def test_ordinary_errors_do_not_stop_the_heartbeat(self):
        self.assertIsNone(classify_result({"is_error": True, "errors": ["tool failed"]}))
        self.assertIsNone(classify_result({"is_error": False, "result": "401 is a number I like"}))


class Numbering(unittest.TestCase):
    def test_cycle_numbers_continue_from_the_workspace(self):
        # The founding cycle ran by hand as cycle 1; the heartbeat must not start another cycle 1.
        names = ["c0001-01-t.md", "c0001-06-t.md", "queue.jsonl", "running-3.jsonl", "before-cycle-2-queue.jsonl", "done"]
        self.assertEqual(highest_cycle(names), 3)
        self.assertEqual(highest_cycle([]), 0)

    def test_a_damaged_state_file_stops_the_start_instead_of_resetting_the_share(self):
        import tempfile
        original = heartbeat.STATE
        heartbeat.STATE = pathlib.Path(tempfile.mkdtemp()) / "state.json"
        try:
            heartbeat.STATE.write_text('{"next_cycle": 7, "week_used')
            with self.assertRaises(heartbeat.StateUnreadable):
                heartbeat.load_state()
            heartbeat.STATE.unlink()
            self.assertEqual(heartbeat.load_state()["next_cycle"], 1)
        finally:
            heartbeat.STATE = original


class QuickFailure(unittest.TestCase):
    def outcome(self, **kw):
        base = {"seconds": 5, "errors": 0, "exit": 0, "results": 2}
        return {**base, **kw}

    def test_docker_or_compose_failing_before_any_session_counts(self):
        self.assertTrue(quick_failure(self.outcome(exit=1, results=0)))
        self.assertTrue(quick_failure(self.outcome(results=0)))

    def test_an_error_in_a_short_cycle_counts(self):
        self.assertTrue(quick_failure(self.outcome(errors=1)))

    def test_a_short_clean_cycle_or_a_long_one_does_not(self):
        self.assertFalse(quick_failure(self.outcome()))
        self.assertFalse(quick_failure(self.outcome(seconds=900, errors=3, exit=1, results=0)))


if __name__ == "__main__":
    unittest.main()
