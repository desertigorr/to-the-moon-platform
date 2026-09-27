from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import Settings, create_app
from backend.state import FleetState
from backend.map_matching import MatchResult
from pydantic import ValidationError
from test_ndtp import HELLO, REALTIME


BASE = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def event(seconds=0, valid=True, packet="1", unit=666753, receive_seconds=None):
    when = BASE + timedelta(seconds=seconds)
    return {"packet_id": packet, "tr_id": 120439, "unit_id": unit,
            "event_time": when.isoformat(), "device_event_id": 0,
            "location_valid": valid, "gps_time": when.isoformat() if valid else None,
            "lon": 37.6 if valid else None, "lat": 55.7 if valid else None,
            "alt": 150 if valid else None, "speed": 32 if valid else None,
            "heading": 90 if valid else None,
            "receive_time": (BASE + timedelta(seconds=seconds if receive_seconds is None
                                               else receive_seconds)).isoformat(),
            "is_hist_data": False}


class BufferTests(unittest.TestCase):
    def test_per_device_history_is_bounded_and_ordered_by_event_time(self):
        state = FleetState(history_size=3)
        for second in (0, 2, 4, 3, 1):
            state.ingest(event(second, packet=str(second)))
        state.ingest(event(0, unit=123))
        self.assertEqual([row.event_time.second for row in state.history(666753).items], [2, 3, 4])
        self.assertEqual(state.history(123).count, 1)
        self.assertEqual(state.vehicle(666753).position.event_time.second, 4)

    def test_retransmission_does_not_duplicate_history(self):
        state = FleetState()
        state.ingest(event(0, packet="1"))
        state.ingest(event(0, packet="2", receive_seconds=1))
        self.assertEqual(state.history(666753).count, 1)
        self.assertEqual(state.vehicle(666753).last_received_at, BASE + timedelta(seconds=1))

    def test_invalid_navigation_and_late_packets_do_not_erase_position(self):
        state = FleetState()
        state.ingest(event(5))
        state.ingest(event(7, valid=False))
        state.ingest(event(1, receive_seconds=8))
        vehicle = state.vehicle(666753, now=BASE + timedelta(seconds=9))
        self.assertFalse(vehicle.latest.location_valid)
        self.assertEqual(vehicle.position.event_time.second, 5)
        self.assertEqual(vehicle.position_age_s, 4)

    def test_forecast_history_excludes_future_and_late_deliveries(self):
        state = FleetState()
        state.ingest(event(1))
        state.ingest(event(2, receive_seconds=10))
        state.ingest(event(7))
        self.assertEqual(state.history(666753, until=BASE + timedelta(seconds=5)).count, 1)
        with self.assertRaises(ValueError):
            state.history(666753, until=BASE.replace(tzinfo=None))

    def test_stale_state_is_explicit(self):
        state = FleetState(stale_after_s=30)
        state.ingest(event())
        self.assertFalse(state.vehicle(666753, BASE + timedelta(seconds=29)).is_stale)
        self.assertTrue(state.vehicle(666753, BASE + timedelta(seconds=31)).is_stale)

    def test_slow_frontend_gets_fresh_snapshot_and_bounded_buffer(self):
        state = FleetState(subscriber_buffer=2)
        queue = state.subscribe()
        for second in range(3):
            state.ingest(event(second))
        self.assertEqual(queue.qsize(), 1)
        message = queue.get_nowait()
        self.assertEqual(message["type"], "snapshot")
        self.assertEqual(message["sequence"], 3)
        self.assertEqual(message["vehicles"][0]["latest"]["event_time"], "2026-09-26T12:00:02Z")
        state.unsubscribe(queue)
        self.assertEqual(len(state.subscribers), 0)


MATCHED = {"status": "matched", "snapped_position": {"lon": 37.6001, "lat": 55.7001},
           "segment_id": "segment-42", "route_id": None, "direction": None}


class MapMatchingTests(unittest.TestCase):
    def test_matching_preserves_raw_gps_and_broadcasts_snapshot(self):
        state = FleetState()
        state.ingest(event(0, packet="source"))
        queue = state.subscribe()
        self.assertTrue(state.apply_map_match(666753, "source", MATCHED, "graph-v1"))
        vehicle = state.vehicle(666753)
        self.assertEqual(vehicle.position.lon, 37.6)
        self.assertEqual(vehicle.latest.lon, 37.6)
        self.assertEqual(state.history(666753).items[0].lon, 37.6)
        self.assertEqual(vehicle.map_match.snapped_position.lon, 37.6001)
        self.assertEqual(vehicle.map_match.source_packet_id, "source")
        self.assertEqual(vehicle.map_match.source_event_time, vehicle.position.event_time)
        update = queue.get_nowait()
        self.assertEqual(update["type"], "snapshot")
        self.assertEqual(update["vehicles"][0]["map_match"]["graph_version"], "graph-v1")

    def test_new_gps_clears_mapping_and_late_computation_is_ignored(self):
        state = FleetState()
        state.ingest(event(0, packet="old"))
        state.apply_map_match(666753, "old", MATCHED, "graph-v1")
        state.ingest(event(5, packet="new"))
        self.assertIsNone(state.vehicle(666753).map_match)
        self.assertFalse(state.apply_map_match(666753, "old", MATCHED, "graph-v1"))
        self.assertIsNone(state.vehicle(666753).map_match)
        self.assertFalse(state.apply_map_match(123, "old", MATCHED, "graph-v1"))

    def test_invalid_gps_retains_result_for_last_valid_position(self):
        state = FleetState()
        state.ingest(event(0, packet="valid"))
        state.apply_map_match(666753, "valid", MATCHED, "graph-v1")
        state.ingest(event(5, valid=False, packet="invalid"))
        vehicle = state.vehicle(666753)
        self.assertEqual(vehicle.position.packet_id, "valid")
        self.assertEqual(vehicle.map_match.source_packet_id, "valid")
        self.assertEqual(vehicle.latest.packet_id, "invalid")

    def test_ambiguous_and_unmatched_are_distinct_from_not_attempted(self):
        state = FleetState()
        state.ingest(event(packet="gps"))
        self.assertIsNone(state.vehicle(666753).map_match)
        for status in ("ambiguous", "unmatched"):
            state.apply_map_match(666753, "gps", {"status": status}, "graph-v1")
            result = state.vehicle(666753).map_match
            self.assertEqual(result.status, status)
            self.assertIsNone(result.snapped_position)

    def test_invalid_outputs_do_not_replace_valid_mapping(self):
        state = FleetState()
        state.ingest(event(packet="gps"))
        state.apply_map_match(666753, "gps", MATCHED, "graph-v1")
        bad_outputs = [
            {"status": "matched"},
            {**MATCHED, "status": "ambiguous"},
            {**MATCHED, "snapped_position": {"lon": 181, "lat": 55}},
            {**MATCHED, "direction": "outbound"},
        ]
        for bad in bad_outputs:
            with self.assertRaises(ValidationError):
                state.apply_map_match(666753, "gps", bad, "graph-v1")
        self.assertEqual(state.vehicle(666753).map_match.segment_id, "segment-42")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(ndtp_host="127.0.0.1", ndtp_port=0,
                                       output_dir=Path(self.tmp.name), history_size=3))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.port = self.app.state.receiver.server.sockets[0].getsockname()[1]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def wait_count(self, key, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.client.get("/api/health").json()[key] >= expected:
                return
            time.sleep(0.01)
        self.fail(f"{key} did not reach {expected}")

    def test_real_binary_packet_reaches_http_and_websocket_and_csv(self):
        with self.client.websocket_connect("/api/stream") as stream:
            self.assertEqual(stream.receive_json(), {"type": "snapshot", "sequence": 0, "vehicles": []})
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as tcp:
                
                tcp.sendall(HELLO[:4])
                tcp.sendall(HELLO[4:] + REALTIME)
                update = stream.receive_json()
                self.assertEqual(update["type"], "telemetry")
                self.assertEqual(update["data"]["unit_id"], 666753)
                self.assertIsInstance(update["data"]["packet_id"], str)
                self.assertAlmostEqual(update["vehicle"]["position"]["lon"], 37.5753005)
            vehicles = self.client.get("/api/vehicles").json()
            self.assertEqual(len(vehicles), 1)
            self.assertEqual(vehicles[0]["tr_id"], 120439)
            response = self.client.get("/api/vehicles/666753/history?limit=1").json()
            self.assertEqual(response["count"], 1)
            self.assertTrue(response["items"][0]["event_time"].endswith("Z"))
            self.assertEqual(self.client.get("/api/vehicles/666753").status_code, 200)
            self.assertEqual(self.client.get("/docs").status_code, 200)
            self.assertIn("Telemetry", self.client.get("/openapi.json").json()["components"]["schemas"])
        self.assertTrue(list(Path(self.tmp.name).glob("*/traffic.csv")))

    def test_bad_crc_does_not_reach_consumers_and_reconnect_works(self):
        corrupt = bytearray(REALTIME)
        corrupt[-1] ^= 1
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as tcp:
            tcp.sendall(HELLO + corrupt + REALTIME)
            self.wait_count("rejected_packets", 1)
            self.wait_count("received_packets", 1)
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as tcp:
            tcp.sendall(HELLO + REALTIME)
            self.wait_count("received_packets", 2)
        self.assertEqual(self.client.get("/api/vehicles/666753/history").json()["count"], 1)

    def test_unknown_device_and_invalid_history_requests(self):
        self.assertEqual(self.client.get("/api/vehicles/123").status_code, 404)
        self.assertEqual(self.client.get("/api/vehicles/123/history").status_code, 404)
        self.assertEqual(self.client.get("/api/vehicles/123/history?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/vehicles/123/history?limit=4").status_code, 422)
        self.assertEqual(self.client.get("/api/vehicles/123/history?until=2026-09-26T12:00:00").status_code, 422)

    def test_cors_and_websocket_origin(self):
        headers = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"}
        response = self.client.options("/api/vehicles", headers=headers)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/stream", headers={"Origin": "https://unknown.example"}):
                pass

    def test_matching_update_reaches_existing_http_and_websocket_contract(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as tcp:
            tcp.sendall(HELLO + REALTIME)
            self.wait_count("received_packets", 1)
        raw = self.client.get("/api/vehicles/666753").json()
        self.assertIsNone(raw["map_match"])
        with self.client.websocket_connect("/api/stream") as stream:
            stream.receive_json()
            self.client.portal.call(self.app.state.fleet.apply_map_match,
                                    666753, raw["position"]["packet_id"], MATCHED, "graph-v1")
            update = stream.receive_json()
            self.assertEqual(update["type"], "snapshot")
            self.assertEqual(update["vehicles"][0]["map_match"]["status"], "matched")
        current = self.client.get("/api/vehicles/666753").json()
        self.assertEqual(current["position"], raw["position"])
        self.assertEqual(current["map_match"]["source_packet_id"], raw["position"]["packet_id"])


if __name__ == "__main__":
    unittest.main()
