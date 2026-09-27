import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app import Settings
from backend.pipeline import Pipeline
from backend.scenario import ScenarioController, ScenarioCursor, load_scenario, nav_fields
from backend.state import FleetState

SOURCE = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)


def row(event_s, receive_s=None, unit=10, valid=True):
    return {"unit_id": unit, "tr_id": unit + 1, "event_time": SOURCE + timedelta(seconds=event_s),
            "receive_time": SOURCE + timedelta(seconds=event_s if receive_s is None else receive_s),
            "location_valid": valid, "lon": 37.6 if valid else None, "lat": 55.7 if valid else None,
            "speed": 2 if valid else None, "heading": 90 if valid else None, "alt": 100 if valid else None}


class CursorTests(unittest.TestCase):
    def test_no_future_no_retransmission_no_held_position_refresh(self):
        cursor = ScenarioCursor([row(1), row(1, 2), row(3, 4), row(5, 6)])
        self.assertFalse(cursor.collect(SOURCE))
        self.assertEqual(cursor.collect(SOURCE + timedelta(seconds=1))[10]["event_time"], SOURCE + timedelta(seconds=1))
        self.assertFalse(cursor.collect(SOURCE + timedelta(seconds=2)))
        self.assertFalse(cursor.collect(SOURCE + timedelta(seconds=3)))
        self.assertEqual(cursor.collect(SOURCE + timedelta(seconds=4))[10]["event_time"], SOURCE + timedelta(seconds=3))
        self.assertFalse(cursor.collect(SOURCE + timedelta(seconds=5)))

    def test_outage_never_relabels_old_packets_as_fresh(self):
        cursor = ScenarioCursor([row(1), row(40), row(45, valid=False), row(100, 46)])
        batch = cursor.collect(SOURCE + timedelta(seconds=46))
        self.assertFalse(batch[10]["location_valid"])
        self.assertEqual(cursor.skipped, {"stale": 1, "coalesced": 1, "future": 0, "old_or_duplicate": 0})
        self.assertFalse(cursor.collect(SOURCE + timedelta(seconds=99)))
        self.assertEqual(cursor.collect(SOURCE + timedelta(seconds=100))[10]["event_time"], SOURCE + timedelta(seconds=100))

    def test_signed_positions_and_integer_rounding_match_protocol(self):
        p = row(0) | {"lon": -37.61732101, "lat": -55.7551234, "speed": 12.6}
        f = nav_fields(p)
        self.assertEqual(f["longitude"], 376173210)
        self.assertFalse(f["extraDopBit5"])
        self.assertFalse(f["extraDopBit6"])
        self.assertEqual(f["speedAvg"], 13)


class ScenarioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        with (root / "traffic.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(row(0)))
            writer.writeheader()
            for p in [row(1), row(15), row(20, unit=20)]:
                writer.writerow(p)
        with (root / "schedule_plan.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["tt_action_item_id", "tr_id", "time_begin", "geom", "time_fact_begin"])
            writer.writeheader()
            writer.writerow({"tt_action_item_id": "stop", "tr_id": 11,
                             "time_begin": (SOURCE + timedelta(minutes=12)).isoformat(),
                             "geom": "POINT (37.6 55.7)", "time_fact_begin": "DO NOT LEAK"})
        self.settings = Settings(data_mode="emulator_replay", emulator_url="http://emulator", geometry_path="",
            scenario_dataset_dir=str(root), scenario_start=SOURCE.isoformat(), scenario_duration_s=60)
        self.fleet = FleetState()
        self.pipeline = Pipeline(self.fleet, self.settings)
        self.controller = ScenarioController(self.pipeline, self.settings)
        self.pipeline.input_guard = self.controller.accept
        self.now = datetime.now(timezone.utc)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def respond(self, request):
        self.calls.append((request.method, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"units": []})

    def decoded_event(self, when, packet="one", unit=10):
        p = row(1, unit=unit)
        return p | {"event_time": when.replace(microsecond=0).isoformat(),
                    "receive_time": when.isoformat(), "gps_time": when.replace(microsecond=0).isoformat(),
                    "packet_id": packet, "device_event_id": 0, "is_hist_data": False}

    async def test_csv_only_configures_emulator_and_cannot_prefill_fleet(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.respond)) as client:
            await self.controller.step(client, self.now)
            await self.controller.step(client, self.now + timedelta(seconds=1))
        self.assertFalse(self.fleet.latest)
        self.assertFalse(self.fleet.ml_history)
        self.assertEqual(self.calls[-1][1]["units"][0]["autoGenerate"], False)
        self.assertEqual(self.calls[-1][1]["units"][0]["cells"][0]["fields"]["longitude"], 376000000)
        self.assertNotIn("time_fact_begin", self.pipeline.bundle["schedule"][0])
        delta = self.pipeline.schedule.by_id["stop"].time - self.pipeline.boot
        self.assertEqual(delta.total_seconds(), 720)
        p = self.decoded_event(self.now + timedelta(seconds=1.3))
        self.pipeline.ingest(p)
        self.assertEqual(self.fleet.vehicle(10).position.lon, 37.6)
        self.pipeline.ingest(p | {"packet_id": "repeat"})
        self.assertEqual(len(self.fleet.ml_history[10]), 1)
        self.assertEqual(self.controller.accepted, 1)

    async def test_wrong_or_expired_packet_is_not_a_scenario_observation(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.respond)) as client:
            await self.controller.step(client, self.now)
            await self.controller.step(client, self.now + timedelta(seconds=1))
        self.assertFalse(self.controller.accept(self.decoded_event(self.now + timedelta(seconds=1.3)) | {"lon": 38.0}))
        self.assertFalse(self.controller.accept(self.decoded_event(self.now + timedelta(seconds=7))))
        self.assertFalse(self.fleet.latest)

    async def test_cycle_clears_history_deviation_and_pending_previous_packets(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.respond)) as client:
            await self.controller.step(client, self.now)
            await self.controller.step(client, self.now + timedelta(seconds=1))
            self.pipeline.ingest(self.decoded_event(self.now + timedelta(seconds=1.3)))
            first_session = self.pipeline.session_id
            await self.controller.step(client, self.now + timedelta(seconds=60))
        self.assertNotEqual(self.pipeline.session_id, first_session)
        self.assertFalse(self.fleet.latest)
        self.assertFalse(self.controller.expected)
        self.assertEqual(self.pipeline.schedule.by_id["stop"].time, self.now + timedelta(seconds=780))

    async def test_failed_post_never_advances_fleet_or_retries_stale_gps(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.respond)) as client:
            await self.controller.step(client, self.now)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await self.controller.step(client, self.now + timedelta(seconds=1))
        self.assertFalse(self.fleet.latest)
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.respond)) as client:
            await self.controller.step(client, self.now + timedelta(seconds=55))
        self.assertFalse(self.controller.expected)
        self.assertFalse(self.fleet.latest)

    def test_empty_window_is_an_explicit_configuration_error(self):
        with self.assertRaisesRegex(ValueError, "No telemetry"):
            load_scenario(self.temp.name, "2026-01-07T00:00:00Z", 60)


if __name__ == "__main__":
    unittest.main()
