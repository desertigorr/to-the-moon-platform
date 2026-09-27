"""Official emulator startup/restart and forecast-input readiness are separate concerns."""
import json
import unittest
from datetime import datetime, timezone

import httpx

from backend.app import Settings
from backend.emulator import EmulatorController
from backend.pipeline import Pipeline
from backend.state import FleetState


class EmulatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_configure_once_and_restore_after_emulator_restart(self):
        saved = {"units": []}
        posts = []
        def respond(request):
            nonlocal saved
            if request.method == "POST":
                saved = json.loads(request.content)
                posts.append(saved)
            return httpx.Response(200, json=saved)
        controller = EmulatorController("http://emulator:18080", {10: 1, 20: 2}, 5000)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await controller.sync(client)
            await controller.sync(client)
            self.assertEqual(len(posts), 1)  
            self.assertEqual(posts[0]["targetHost"], "backend")
            self.assertTrue(all(u["autoGenerate"] for u in posts[0]["units"]))
            self.assertEqual([u["unitId"] for u in posts[0]["units"]], [10, 20])
            saved = {"units": []}
            await controller.sync(client)
        self.assertEqual(len(posts), 2)
        self.assertEqual(controller.snapshot()["active_units"], 2)

    async def test_failed_configuration_is_retried(self):
        controller = EmulatorController("http://emulator", {10: 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                200 if req.method == "GET" else 503, json={"units": []}))) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await controller.sync(client)
        self.assertFalse(controller.applied)

    def test_empty_or_invalid_emulator_configuration_fails_early(self):
        for mapping, interval in [({}, 5000), ({10: 1}, 0), ({2147483648: 1}, 5000)]:
            with self.assertRaises(ValueError):
                EmulatorController("http://emulator", mapping, interval)

    async def test_models_can_be_ready_without_a_schedule_and_no_forecast_is_fabricated(self):
        now = datetime.now(timezone.utc)
        fleet = FleetState()
        pipeline = Pipeline(fleet, Settings(ml_url="http://ml"))
        pipeline.ingest({"packet_id": "1", "tr_id": 1, "unit_id": 10, "event_time": now,
            "receive_time": now, "gps_time": now, "device_event_id": 0, "location_valid": True,
            "lon": 37.6, "lat": 55.7, "speed": 0, "heading": 0, "alt": 100, "is_hist_data": False})
        calls = []
        def respond(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json={"status": "ok", "models_loaded": 2})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await pipeline.infer_once(client)
        self.assertEqual(calls, [("GET", "/health")])
        self.assertEqual(pipeline.ml_status, "ready")
        self.assertEqual(pipeline.schedule_status()["status"], "missing")
        self.assertEqual(fleet.vehicle(10).prediction_status, "no_schedule")
        self.assertIsNone(fleet.vehicle(10).prediction)
        self.assertIsNone(fleet.vehicle(10).arrival)


if __name__ == "__main__":
    unittest.main()
