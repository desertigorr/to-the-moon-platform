"""Run inside the ML image, where the real CatBoost dependencies/models are available."""
from datetime import datetime, timedelta, timezone
import importlib.util
import math
import unittest

AVAILABLE = importlib.util.find_spec("catboost") is not None
if AVAILABLE:
    from fastapi.testclient import TestClient
    from ml_service.app import create_app


@unittest.skipUnless(AVAILABLE, "Real model tests run in the ML container")
class MLServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def payload(self):
        T = datetime(2026, 1, 6, 12, 30, 0, 123456, tzinfo=timezone.utc)
        target = T + timedelta(minutes=12)
        return {"request_id": "test-1", "points": [{"sample_id": "one", "tr_id": 1,
            "T": T.isoformat(), "cur_dev_s": None, "target_stop_id": "stop1",
            "target_time_begin": target.isoformat()}], "traffic_buffer": [{"tr_id": 1,
            "event_time": (T-timedelta(seconds=15)).isoformat(), "receive_time": T.isoformat(),
            "location_valid": True, "lat": 55.7, "lon": 37.6, "speed": 25, "heading": 90}],
            "schedule_as_of_T": [{"tt_action_item_id": "stop1", "tr_id": 1,
                "time_begin": target.isoformat(), "geom": "POINT (37.61 55.7)"}]}

    def test_real_models_preserve_precision_null_and_risk_semantics(self):
        payload = self.payload()
        response = self.client.post("/predict", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        p = response.json()["predictions"][0]
        self.assertEqual(datetime.fromisoformat(p["T"].replace("Z", "+00:00")), datetime.fromisoformat(payload["points"][0]["T"]))
        self.assertIsNone(p["cur_dev_s"])
        self.assertFalse(p["early_risk_alert"])
        self.assertTrue(math.isfinite(p["predicted_delay_s"]))
        self.assertEqual(p["risk_alert"], p["late_probability"] >= p["alert_threshold"])
        self.assertEqual(p['explanation_method'], 'tree_shap')
        self.assertTrue(p['explanations'])
        self.assertTrue(p['observations'])
        self.assertLessEqual(len(p['factors']), 5)
        logit = p['risk_baseline_log_odds'] + p['risk_other_contribution'] + sum(f['contribution'] for f in p['factors'])
        self.assertAlmostEqual(1 / (1 + math.exp(-logit)), p['late_probability'], places=9)
        for factor in p['factors']:
            self.assertEqual(factor['direction'] == 'increases_risk', factor['contribution'] > 0)
        self.assertEqual(self.client.get("/health").json()["models_loaded"], 2)

    def test_future_and_not_received_packets_cannot_change_forecast(self):
        payload = self.payload()
        first = self.client.post("/predict", json=payload).json()["predictions"][0]
        future = dict(payload["traffic_buffer"][0], receive_time="2026-01-07T00:00:00Z", speed=125, lat=60)
        payload["traffic_buffer"].append(future)
        payload["traffic_buffer"].append(dict(future, receive_time=payload["points"][0]["T"], event_time="2026-01-07T00:00:00Z"))
        second = self.client.post("/predict", json=payload).json()["predictions"][0]
        self.assertEqual(first["predicted_delay_s"], second["predicted_delay_s"])
        self.assertEqual(first["late_probability"], second["late_probability"])
        self.assertEqual(first['factors'], second['factors'])
        self.assertEqual(first['observations'], second['observations'])

    def test_empty_traffic_and_invalid_target_are_explicit(self):
        payload = self.payload()
        payload["traffic_buffer"] = []
        response = self.client.post("/predict", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["predictions"][0]["telemetry_stale"])
        payload["points"][0]["target_stop_id"] = "missing"
        self.assertEqual(self.client.post("/predict", json=payload).status_code, 422)


if __name__ == "__main__":
    unittest.main()
