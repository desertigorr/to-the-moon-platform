import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app import Settings
from backend.pipeline import Pipeline
from backend.schedule import Schedule, ArrivalEstimator
from backend.state import FleetState, Telemetry
from shared.contracts import PredictRequest
from ndtp.encoding import hello, navigation
from ndtp.protocol import decode_frame, decode_navigation, decode_handshake

BASE = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


def point(seconds=0, received=None, lon=37.6, lat=55.7, speed=0, valid=True, packet=None):
    when = BASE + timedelta(seconds=seconds)
    return Telemetry(packet_id=str(packet or seconds), tr_id=1, unit_id=10,
        event_time=when, receive_time=BASE + timedelta(seconds=seconds if received is None else received),
        device_event_id=0, location_valid=valid, gps_time=when if valid else None,
        lon=lon if valid else None, lat=lat if valid else None, speed=speed if valid else None,
        heading=90 if valid else None, alt=100 if valid else None, is_hist_data=False)


def plan():
    return Schedule([{"tt_action_item_id": str(i), "tr_id": 1,
                      "time_begin": (BASE + timedelta(seconds=seconds)).isoformat(),
                      "geom": f"POINT ({lon} 55.7)"}
                     for i, seconds, lon in [(1, 0, 37.6), (2, 600, 37.61), (3, 660, 37.62), (4, 900, 37.63)]])


class ArrivalTests(unittest.TestCase):
    def test_missing_stays_missing_then_confirmation_is_causal(self):
        estimator = ArrivalEstimator(plan())
        self.assertIsNone(estimator.latest(1, BASE))
        estimator.ingest(point(30))
        self.assertIsNone(estimator.latest(1, BASE + timedelta(seconds=31)))
        estimator.ingest(point(45, received=60))
        self.assertIsNone(estimator.latest(1, BASE + timedelta(seconds=59)))
        result = estimator.latest(1, BASE + timedelta(seconds=60))
        self.assertEqual(result.event_time, BASE + timedelta(seconds=30))
        self.assertEqual(result.deviation, 30)
        self.assertEqual(result.stop_id, "1")

    def test_duplicate_and_stationary_packets_never_repeat_arrival(self):
        estimator = ArrivalEstimator(plan())
        for second in (30, 30, 45, 60, 75, 90):
            estimator.ingest(point(second))
        self.assertEqual(len(estimator.arrivals[1]), 1)
        self.assertEqual(estimator.arrivals[1][0].deviation, 30)

    def test_departure_advances_and_late_packets_do_not_rewind(self):
        estimator = ArrivalEstimator(plan())
        for p in [point(30), point(45), point(100, lon=37.605, speed=25),
                  point(620, lon=37.61), point(635, lon=37.61), point(20, received=650)]:
            estimator.ingest(p)
        self.assertEqual([a.stop_id for a in estimator.arrivals[1]], ["1", "2"])
        self.assertEqual(estimator.latest(1, BASE + timedelta(seconds=650)).deviation, 20)

    def test_gps_loss_high_speed_and_long_gaps_do_not_confirm(self):
        for middle in (point(10, valid=False), point(10, speed=40), point(60)):
            estimator = ArrivalEstimator(plan())
            estimator.ingest(point())
            estimator.ingest(middle)
            self.assertFalse(estimator.arrivals)

    def test_unknown_vehicle_and_overlapping_arrivals_remain_unconfirmed(self):
        same = Schedule([{"tt_action_item_id": str(i), "tr_id": 1,
                          "time_begin": (BASE + timedelta(seconds=i)).isoformat(), "geom": "POINT (37.6 55.7)"}
                         for i in (1, 2)])
        estimator = ArrivalEstimator(same)
        estimator.ingest(point(10)); estimator.ingest(point(25))
        self.assertFalse(estimator.arrivals)

    def test_target_lower_boundary_excluded_upper_included(self):
        self.assertEqual(plan().target(1, BASE).id, "3")
        self.assertEqual(plan().target(1, BASE + timedelta(seconds=299)).id, "4")
        self.assertIsNone(plan().target(1, BASE + timedelta(seconds=301)))

    def test_stationary_wait_does_not_become_a_future_arrival(self):
        schedule = Schedule([{"tt_action_item_id": "future", "tr_id": 1,
            "time_begin": (BASE + timedelta(seconds=600)).isoformat(), "geom": "POINT (37.6 55.7)"}])
        estimator = ArrivalEstimator(schedule)
        for second in range(0, 700, 15):
            estimator.ingest(point(second))
        self.assertFalse(estimator.arrivals)

    def test_old_estimate_expires_without_fabricated_zero(self):
        estimator = ArrivalEstimator(plan())
        estimator.ingest(point(30))
        estimator.ingest(point(45))
        self.assertEqual(estimator.latest(1, BASE + timedelta(seconds=330)).deviation, 30)
        self.assertIsNone(estimator.latest(1, BASE + timedelta(seconds=331)))

    def test_confirmed_early_arrival_keeps_its_negative_deviation(self):
        estimator = ArrivalEstimator(plan())
        estimator.ingest(point(-120))
        estimator.ingest(point(-105))
        self.assertEqual(estimator.latest(1, BASE).deviation, -120)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = FleetState(history_size=10000)
        self.pipeline = Pipeline(self.state, Settings(ml_url="http://ml:8001"))
        self.pipeline.schedule = plan()
        self.pipeline.estimator = ArrivalEstimator(self.pipeline.schedule)

    def test_request_excludes_unavailable_packets_and_unknown_deviation_is_null(self):
        self.state.ingest(point(-10).model_dump(mode="json"))
        self.state.ingest(point(-5, received=5).model_dump(mode="json"))
        self.state.ingest(point(5).model_dump(mode="json"))
        request, units = self.pipeline.build_request(BASE)
        self.assertEqual(len(request.traffic_buffer), 1)
        self.assertIsNone(request.points[0].cur_dev_s)
        self.assertEqual(request.points[0].target_stop_id, "3")

    def test_arrival_expires_in_api_even_without_more_packets(self):
        self.pipeline.ingest(point(30).model_dump(mode="json"))
        self.pipeline.ingest(point(45).model_dump(mode="json"))
        self.assertEqual(self.state.vehicle(10, BASE + timedelta(seconds=60)).arrival['cur_dev_s'], 30)
        self.assertIsNone(self.state.vehicle(10, BASE + timedelta(seconds=331)).arrival)
        request, _ = self.pipeline.build_request(BASE + timedelta(seconds=46))
        self.assertEqual(request.points[0].cur_dev_s, 30)

    def test_ten_minutes_are_retained_at_half_second_frequency(self):
        for i in range(1800):
            self.state.ingest(point(i / 2, packet=str(i)).model_dump(mode="json"))
        self.assertEqual(len(self.state.ml_history[10]), 1800)
        self.assertGreater((self.state.ml_history[10][-1].event_time - self.state.ml_history[10][0].event_time).total_seconds(), 600)

    async def test_ml_failure_does_not_interrupt_gps(self):
        now = datetime.now(timezone.utc)
        self.pipeline.schedule = Schedule([{"tt_action_item_id": "1", "tr_id": 1,
            "time_begin": (now + timedelta(minutes=12)).isoformat(), "geom": "POINT (37.6 55.7)"}])
        p = point().model_copy(update={"event_time": now, "receive_time": now, "gps_time": now})
        self.pipeline.ingest(p.model_dump(mode="json"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            await self.pipeline.infer_once(client)
        self.assertEqual(self.pipeline.ml_status, "unavailable")
        self.assertEqual(self.state.vehicle(10).position.lon, 37.6)
        self.assertEqual(self.state.prediction_status[10], "ml_unavailable")
        self.pipeline.build_request(now)
        self.assertEqual(self.state.prediction_status[10], "ml_unavailable")

    async def test_repeated_risk_after_brief_dip_does_not_spam_alerts(self):
        now = datetime.now(timezone.utc)
        self.pipeline.schedule = Schedule([{"tt_action_item_id": "1", "tr_id": 1,
            "time_begin": (now + timedelta(minutes=12)).isoformat(), "geom": "POINT (37.6 55.7)"}])
        p = point().model_copy(update={"event_time": now, "receive_time": now, "gps_time": now})
        self.pipeline.ingest(p.model_dump(mode="json"))
        risk = iter([True, False, True])
        def respond(req):
            body = json.loads(req.content)
            row = body["points"][0]
            row.update(predicted_delay_s=180, late_probability=.8, risk_alert=next(risk),
                       early_risk_alert=False, telemetry_stale=False, delay_threshold_s=120,
                       alert_threshold=.164, generated_at=now.isoformat(), model_version="test", explanations=[])
            return httpx.Response(200, json={"request_id": body["request_id"], "predictions": [row], "elapsed_ms": 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            for _ in range(3):
                await self.pipeline.infer_once(client)
        self.assertEqual(self.pipeline.ml_status, "ready")
        self.assertEqual(len(self.pipeline.alerts), 1)
        self.assertTrue(self.state.predictions[10].risk_alert)

    def test_real_ndtp_encoder_roundtrips_signs_and_invalid_gps(self):
        self.assertEqual(decode_handshake(decode_frame(hello(10)))["unit_id"], 10)
        row = point(lon=-37.6, lat=-55.7).model_dump(mode="json")
        decoded = decode_navigation(decode_frame(navigation(row, 100, 2)))
        self.assertAlmostEqual(decoded["lon"], -37.6)
        self.assertEqual(decoded["event_time"], BASE + timedelta(seconds=100))
        row = point(valid=False).model_dump(mode="json")
        self.assertIsNone(decode_navigation(decode_frame(navigation(row, 0, 3)))["lon"])


if __name__ == "__main__":
    unittest.main()
