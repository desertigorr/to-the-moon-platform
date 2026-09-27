"""Bounded telemetry history and current state; all operations run on one event loop."""

import asyncio
from collections import deque
from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict

from .map_matching import MapMatch, MatchResult
from shared.contracts import Prediction, Recommendation
from .dispatch import recommendations


class Telemetry(BaseModel):
    model_config = ConfigDict(frozen=True)

    packet_id: str  
    tr_id: int | None
    unit_id: int
    event_time: AwareDatetime
    device_event_id: int
    location_valid: bool
    gps_time: AwareDatetime | None
    lon: float | None
    lat: float | None
    alt: float | None
    speed: float | None
    heading: float | None
    receive_time: AwareDatetime
    is_hist_data: bool

    def measurement_key(self) -> tuple:
        """Same measurement resent under a different packet ID is not new history."""
        return (self.event_time, self.location_valid, self.gps_time, self.lon,
                self.lat, self.alt, self.speed, self.heading, self.device_event_id)


class Vehicle(BaseModel):
    unit_id: int
    tr_id: int | None
    latest: Telemetry
    position: Telemetry | None
    last_received_at: AwareDatetime
    history_count: int
    is_stale: bool
    position_age_s: float | None
    map_match: MapMatch | None = None
    prediction: Prediction | None = None
    prediction_status: str = "not_connected"
    arrival: dict | None = None
    recommendations: list[Recommendation] = []


class History(BaseModel):
    unit_id: int
    capacity: int
    retained_count: int
    count: int
    items: list[Telemetry]


class FleetState:
    def __init__(self, history_size=300, stale_after_s=30, subscriber_buffer=64, history_seconds=900):
        if history_size < 1 or stale_after_s <= 0 or subscriber_buffer < 1:
            raise ValueError("History size, stale timeout and stream buffer must be positive")
        self.history_size = history_size
        self.stale_after_s = stale_after_s
        self.subscriber_buffer = subscriber_buffer
        self.histories: dict[int, deque[Telemetry]] = {}
        self.latest: dict[int, Telemetry] = {}
        self.positions: dict[int, Telemetry] = {}
        self.map_matches: dict[int, MapMatch] = {}
        self.last_received: dict[int, datetime] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.sequence = 0
        self.history_seconds = history_seconds
        self.ml_history: dict[int, deque[Telemetry]] = {}
        self.predictions: dict[int, Prediction] = {}
        self.prediction_status: dict[int, str] = {}
        self.arrival_info: dict[int, dict] = {}

    def ingest(self, event: dict):
        item = Telemetry.model_validate(event)
        unit = item.unit_id
        raw_history = self.ml_history.setdefault(unit, deque(maxlen=self.history_size))
        raw_history.append(item)
        cutoff = item.receive_time.timestamp() - self.history_seconds
        
        while raw_history and raw_history[0].receive_time.timestamp() < cutoff:
            raw_history.popleft()
        history = self.histories.setdefault(unit, deque(maxlen=self.history_size))
        if not any(old.measurement_key() == item.measurement_key() for old in history):
            
            ordered = sorted([*history, item], key=lambda row: (row.event_time, row.receive_time))
            history.clear()
            history.extend(ordered[-self.history_size:])
        if unit not in self.latest or item.event_time >= self.latest[unit].event_time:
            self.latest[unit] = item
        if item.location_valid and (unit not in self.positions or
                                   item.event_time >= self.positions[unit].event_time):
            self.positions[unit] = item
            
            self.map_matches.pop(unit, None)
        previous = self.last_received.get(unit, item.receive_time)
        self.last_received[unit] = max(previous, item.receive_time)
        self.sequence += 1
        message = {"type": "telemetry", "sequence": self.sequence,
                   "data": item.model_dump(mode="json"),
                   "vehicle": self.vehicle(unit).model_dump(mode="json")}
        self._broadcast(message)

    def _broadcast(self, message: dict):
        for queue in self.subscribers:
            if queue.full():
                
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot())
            else:
                queue.put_nowait(message)

    def apply_map_match(self, unit_id: int, source_packet_id: str,
                        result: dict | MatchResult, graph_version: str) -> bool:
        """Apply a matcher result on the event loop; discard obsolete computations."""
        position = self.positions.get(unit_id)
        if position is None or position.packet_id != source_packet_id:
            return False
        parsed = MatchResult.model_validate(result)
        attached = MapMatch(**parsed.model_dump(), source_packet_id=position.packet_id,
                            source_event_time=position.event_time, graph_version=graph_version)
        self.map_matches[unit_id] = attached
        self.sequence += 1
        
        self._broadcast(self.snapshot())
        return True

    def vehicle(self, unit: int, now: datetime | None = None) -> Vehicle:
        latest = self.latest[unit]
        now = now or datetime.now(timezone.utc)
        position = self.positions.get(unit)
        received = self.last_received[unit]
        prediction = self.predictions.get(unit)
        arrival = self.arrival_info.get(unit)
        if arrival and arrival.get("valid_until") and datetime.fromisoformat(arrival["valid_until"]) < now:
            arrival = None
        result = Vehicle(
            unit_id=unit, tr_id=latest.tr_id, latest=latest, position=position,
            last_received_at=received, history_count=len(self.histories[unit]),
            is_stale=(now - latest.event_time).total_seconds() > self.stale_after_s or
                     (now - received).total_seconds() > self.stale_after_s,
            position_age_s=max(0, (now - position.event_time).total_seconds()) if position else None,
            map_match=self.map_matches.get(unit),
            prediction=prediction,
            prediction_status=self.prediction_status.get(unit, "not_connected"),
            arrival=arrival,
        )
        result.recommendations = recommendations(result, now)
        return result

    def reset(self):
        """Start a new replay epoch while retaining subscribers and monotonic sequence."""
        for store in (self.histories, self.latest, self.positions, self.map_matches,
                      self.last_received, self.ml_history, self.predictions,
                      self.prediction_status, self.arrival_info):
            store.clear()
        self.sequence += 1
        self._broadcast(self.snapshot())

    def vehicles(self) -> list[Vehicle]:
        now = datetime.now(timezone.utc)
        return [self.vehicle(unit, now) for unit in sorted(self.latest)]

    def history(self, unit: int, limit: int | None = None,
                until: datetime | None = None) -> History:
        if until is not None and until.tzinfo is None:
            raise ValueError("until must include a timezone")
        items = list(self.histories[unit])
        if until is not None:
            
            items = [item for item in items if item.event_time <= until and item.receive_time <= until]
        selected = items[-(limit or self.history_size):]
        return History(unit_id=unit, capacity=self.history_size,
                       retained_count=len(self.histories[unit]), count=len(selected), items=selected)

    def snapshot(self) -> dict:
        return {"type": "snapshot", "sequence": self.sequence,
                "vehicles": [vehicle.model_dump(mode="json") for vehicle in self.vehicles()]}

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=self.subscriber_buffer)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)
