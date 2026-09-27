"""Schedule + GPS arrival estimates + remote ML, independent of NDTP receive latency."""
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import logging
import time
import uuid

import httpx

from shared.contracts import PredictRequest, PredictResponse, TrafficPoint
from .schedule import Schedule, ArrivalEstimator, stamp
from .state import Telemetry
from .scenario import load_scenario
from .geometry import GeometryLibrary
from .dispatch import network_risk
from shared.contracts import SegmentContext

LOG = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, fleet, settings):
        self.fleet, self.settings = fleet, settings
        self.boot = datetime.now(timezone.utc).replace(microsecond=0)
        self.run_id = uuid.uuid4().hex[:12]
        self.epoch = -1
        self.bundle = None
        self.geometry = GeometryLibrary(settings.geometry_path, settings.scenario_start if settings.data_mode == 'emulator_replay' else None)
        self.schedule = Schedule.from_csv(settings.schedule_path, self.geometry)
        self.estimator = ArrivalEstimator(self.schedule)
        self.session_id = self.run_id
        self.shift_s = 0
        self.ml_status = "not_connected" if not settings.ml_url else "starting"
        self.ml_error = None
        self.last_inference = None
        self.alerts = deque(maxlen=100)
        self.active_alerts = set()
        self.alert_sent_at = {}
        self.recording = deque(maxlen=601)
        self.mapping = None
        self.input_guard = (lambda _: False) if settings.data_mode == "emulator_replay" else None
        if settings.data_mode == "emulator_replay":
            self.bundle = load_scenario(settings.scenario_dataset_dir, settings.scenario_start,
                                        settings.scenario_duration_s)
        if self.bundle:
            if self.bundle["duration_s"] <= 0:
                raise ValueError("Replay duration must be positive")
            self.mapping = {int(k): int(v) for k, v in self.bundle["mapping"].items()}
            self.ensure_session()

    def ensure_session(self, now=None):
        if not self.bundle:
            return
        elapsed = max(0, ((now or datetime.now(timezone.utc)) - self.boot).total_seconds())
        epoch = int(elapsed // self.bundle["duration_s"])
        if epoch == self.epoch:
            return
        self.epoch = epoch
        self.session_id = f"{self.run_id}-{epoch}"
        wall_start = self.boot + timedelta(seconds=epoch * self.bundle["duration_s"])
        self.shift_s = (wall_start - stamp(self.bundle["source_start"])).total_seconds()
        self.schedule = Schedule(self.bundle["schedule"], self.shift_s, self.geometry)
        self.estimator = ArrivalEstimator(self.schedule)
        self.fleet.reset()
        self.recording.clear()
        self.alerts.clear()
        self.active_alerts.clear()
        self.alert_sent_at.clear()
        self.last_inference = None

    def replay_config(self):
        self.ensure_session()
        return {"mode": self.settings.data_mode, "session_id": self.session_id,
                "shift_s": self.shift_s, "source_start": self.bundle["source_start"] if self.bundle else None,
                "duration_s": self.bundle["duration_s"] if self.bundle else None,
                "elapsed_s": (datetime.now(timezone.utc) - self.boot).total_seconds() % self.bundle["duration_s"] if self.bundle else None,
                "cycle": self.epoch + 1 if self.bundle else None,
                "label": "Официальный эмулятор · историческое движение" if self.settings.data_mode == "emulator_replay" else
                    "Историческая телеметрия · время сдвинуто к текущему" if self.bundle else (
                    "Официальный эмулятор · автогенерация NDTP" if self.settings.data_mode == "emulator" else "Живой NDTP")}

    def schedule_status(self):
        self.ensure_session()
        now = datetime.now(timezone.utc)
        stops = list(self.schedule.by_id.values())
        times = [stop.time for stop in stops]
        status = "missing" if not stops else "expired" if max(times) <= now else "loaded"
        return {"status": status, "vehicles": len(self.schedule.by_vehicle), "planned_arrivals": len(stops),
                "from": min(times).isoformat() if times else None, "until": max(times).isoformat() if times else None,
                "targets_now": sum(self.schedule.target(tr, now) is not None for tr in self.schedule.by_vehicle)}

    def ingest(self, event):
        self.ensure_session()
        if self.input_guard is not None and not self.input_guard(event):
            return
        point = Telemetry.model_validate(event)
        
        self.fleet.ingest(event)
        self.estimator.ingest(point)
        arrival = self.estimator.latest(point.tr_id, point.receive_time)
        if arrival:
            self.fleet.arrival_info[point.unit_id] = {
                "stop_id": arrival.stop_id, "event_time": arrival.event_time.isoformat(),
                "received_at": arrival.received_at.isoformat(), "cur_dev_s": arrival.deviation,
                "valid_until": (arrival.event_time + timedelta(seconds=self.estimator.max_estimate_age_s)).isoformat(),
                "method": "gps_estimate"}
        else:
            self.fleet.arrival_info.pop(point.unit_id, None)
        current = self.fleet.positions.get(point.unit_id)
        if current and current.packet_id == point.packet_id:
            result = self.schedule.match(point)
            if result:
                self.fleet.apply_map_match(point.unit_id, point.packet_id, result, self.schedule.version)

    def build_request(self, T):
        points, traffic, units = [], [], {}
        for unit, latest in self.fleet.latest.items():
            tr = latest.tr_id
            if tr is None:
                self.fleet.prediction_status[unit] = "missing_vehicle_mapping"
                continue
            target = self.schedule.target(tr, T)
            if target is None:
                self.fleet.prediction_status[unit] = "no_schedule" if tr not in self.schedule.by_vehicle else "no_target"
                self.fleet.predictions.pop(unit, None)
                continue
            if tr in units:
                self.fleet.prediction_status[unit] = "ambiguous_vehicle_mapping"
                self.fleet.prediction_status[units[tr]] = "ambiguous_vehicle_mapping"
                continue
            units[tr] = unit
            arrival = self.estimator.latest(tr, T)
            points.append({"sample_id": f"{tr}_{int(T.timestamp() * 1000)}", "tr_id": tr, "T": T,
                           "cur_dev_s": arrival.deviation if arrival else None,
                           "target_stop_id": target.id, "target_time_begin": target.time})
            
            for row in self.fleet.ml_history.get(unit, ()):
                if T - timedelta(seconds=self.settings.history_seconds) <= row.event_time <= T and row.receive_time <= T:
                    traffic.append({key: getattr(row, key) for key in TrafficPoint.model_fields})
            if self.fleet.prediction_status.get(unit) != "ml_unavailable":
                self.fleet.prediction_status[unit] = "calculating"
        ambiguous = {tr for tr, unit in units.items() if self.fleet.prediction_status[unit] == "ambiguous_vehicle_mapping"}
        points = [p for p in points if p["tr_id"] not in ambiguous]
        if not points:
            return None, units
        return PredictRequest(request_id=f"{self.session_id}:{T.isoformat()}", points=points,
                              traffic_buffer=traffic, schedule_as_of_T=self.schedule.plan_for({p["tr_id"] for p in points})), units

    async def infer_once(self, client):
        self.ensure_session()
        session = self.session_id
        now = datetime.now(timezone.utc)
        units = {}
        try:
            request, units = self.build_request(now)
            if request is None:
                
                response = await client.get(f"{self.settings.ml_url}/health")
                response.raise_for_status()
                if response.json().get("status") != "ok":
                    raise ValueError("ML health is not ready")
                self.ml_status, self.ml_error = "ready", None
                return
            response = await client.post(f"{self.settings.ml_url}/predict", json=request.model_dump(mode="json"))
            response.raise_for_status()
            result = PredictResponse.model_validate(response.json())
            if result.request_id != request.request_id:
                raise ValueError("ML response request_id mismatch")
            expected = {p.sample_id: p for p in request.points}
            if {p.sample_id for p in result.predictions} != set(expected):
                raise ValueError("ML response samples mismatch")
            if len(result.predictions) != len(expected):
                raise ValueError("ML response contains duplicate samples")
            
            for prediction in result.predictions:
                source = expected[prediction.sample_id]
                if (prediction.tr_id, prediction.T, prediction.target_stop_id, prediction.target_time_begin) != (
                        source.tr_id, source.T, source.target_stop_id, source.target_time_begin):
                    raise ValueError("ML response target mismatch")
            self.ensure_session()
            if session != self.session_id:
                return
            received = datetime.now(timezone.utc)
            self.alert_sent_at = {key: at for key, at in self.alert_sent_at.items()
                                  if (received - at).total_seconds() < 900}
            for prediction in result.predictions:
                unit = units[prediction.tr_id]
                previous = self.fleet.predictions.get(unit)
                if prediction.target_time_begin <= received or (previous and previous.T > prediction.T):
                    continue
                context = self.schedule.target_segment(prediction.target_stop_id)
                prediction.segment = SegmentContext(**context) if context else None
                self.fleet.predictions[unit] = prediction
                self.fleet.prediction_status[unit] = "ready"
                key = (unit, prediction.target_stop_id)
                vehicle = self.fleet.vehicle(unit, received)
                prediction.recommendations = vehicle.recommendations
                eligible = prediction.risk_alert and not prediction.telemetry_stale and not vehicle.is_stale and vehicle.latest.location_valid
                last_sent = self.alert_sent_at.get(key)
                if eligible and key not in self.active_alerts and (last_sent is None or (received - last_sent).total_seconds() >= 60):
                    self.alert_sent_at[key] = received
                    target = self.schedule.by_id[prediction.target_stop_id]
                    self.alerts.appendleft({"id": f"{unit}:{prediction.sample_id}", "unit_id": unit,
                        "tr_id": prediction.tr_id, "at": received.isoformat(), "target_stop_id": target.id,
                        "target_name": target.name, "target_time_begin": target.time.isoformat(),
                        "predicted_delay_s": prediction.predicted_delay_s,
                        "late_probability": prediction.late_probability,
                        "early_risk_alert": prediction.early_risk_alert, "explanations": prediction.explanations,
                        "observations": prediction.observations,
                        "factors": [f.model_dump() for f in prediction.factors],
                        "segment": prediction.segment.model_dump() if prediction.segment else None,
                        "forecast_at": prediction.T.isoformat(),
                        "recommendations": [r.model_dump() for r in prediction.recommendations]})
                if eligible:
                    self.active_alerts.add(key)
                else:
                    self.active_alerts.discard(key)
            self.ml_status, self.ml_error = "ready", None
            self.last_inference = {"at": received.isoformat(), "elapsed_ms": result.elapsed_ms,
                                   "vehicles": len(result.predictions)}
            self.fleet.sequence += 1
            self.fleet._broadcast(self.fleet.snapshot())
        except (httpx.HTTPError, ValueError) as error:
            self.ml_status, self.ml_error = "unavailable", str(error)
            for unit in units.values():
                self.fleet.prediction_status[unit] = "ml_unavailable"
            LOG.warning("ML batch failed: %s", error)

    async def run_ml(self):
        if not self.settings.ml_url:
            return
        async with httpx.AsyncClient(timeout=self.settings.ml_timeout_s) as client:
            while True:
                started = time.monotonic()
                await self.infer_once(client)
                await asyncio.sleep(max(.1, self.settings.ml_interval_s - (time.monotonic() - started)))

    async def record(self):
        while True:
            self.ensure_session()
            now = datetime.now(timezone.utc)
            self.recording.append({"at": now.isoformat(), "connection_id": self.session_id,
                                   "message": self.fleet.snapshot()})
            
            self.fleet._broadcast(self.fleet.snapshot())
            await asyncio.sleep(1)

    def export_recording(self):
        return {"format": "to-the-moon-recording-v1", "source": self.settings.data_mode,
                "network": self.schedule.network(), "events": list(self.recording)}

    def network(self):
        self.ensure_session()
        return network_risk(self.schedule.network(), self.fleet.vehicles(), datetime.now(timezone.utc))
