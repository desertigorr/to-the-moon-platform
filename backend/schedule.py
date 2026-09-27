"""Known plan, approximate stop-to-stop geometry and causal arrival observations."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import re


def stamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def distance(a, b):
    lon1, lat1 = a
    lon2, lat2 = b
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000


def project(point, a, b):
    scale = math.cos(math.radians(point[1]))
    dx, dy = (b[0] - a[0]) * scale, b[1] - a[1]
    denom = dx * dx + dy * dy
    fraction = max(0, min(1, (((point[0] - a[0]) * scale * dx +
                              (point[1] - a[1]) * dy) / denom))) if denom else 0
    snapped = (a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1]))
    return distance(point, snapped), fraction, snapped


@dataclass(frozen=True)
class Stop:
    id: str
    tr_id: int
    time: datetime
    lon: float
    lat: float
    name: str

    def plan(self):
        return {"tt_action_item_id": self.id, "tr_id": self.tr_id,
                "time_begin": self.time.isoformat(), "geom": f"POINT ({self.lon} {self.lat})"}


class Schedule:
    def __init__(self, rows=(), shift_s=0, geometry=None):
        self.geometry = geometry
        self.previous_match = {}
        self.by_vehicle: dict[int, list[Stop]] = {}
        self.by_id = {}
        for row in rows:
            xy = re.fullmatch(r"POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)", row["geom"])
            if not xy:
                continue
            lon, lat = map(float, xy.groups())
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                continue
            stop = Stop(str(row["tt_action_item_id"]), int(row["tr_id"]),
                        stamp(row["time_begin"]) + timedelta(seconds=shift_s), lon, lat,
                        row.get("building_address") or "Остановка")
            if stop.id in self.by_id:
                raise ValueError("Duplicate planned arrival ID")
            self.by_id[stop.id] = stop
            self.by_vehicle.setdefault(stop.tr_id, []).append(stop)
        for stops in self.by_vehicle.values():
            stops.sort(key=lambda s: (s.time, s.id))
        identity = [(s.id, s.lon, s.lat) for stops in self.by_vehicle.values() for s in stops]
        self.version = "plan-" + hashlib.sha256(json.dumps([identity, geometry.version if geometry else None]).encode()).hexdigest()[:12]
        self.segments = []
        for tr, stops in self.by_vehicle.items():
            for index, (a, b) in enumerate(zip(stops, stops[1:])):
                chord = distance((a.lon, a.lat), (b.lon, b.lat))
                if not 0 < (b.time - a.time).total_seconds() <= 1800 or not 5 <= chord <= 6000:
                    continue
                observed = geometry.get(tr, (a.lon, a.lat), (b.lon, b.lat)) if geometry else None
                coordinates = observed['coordinates'] if observed else [[a.lon, a.lat], [b.lon, b.lat]]
                physical = hashlib.sha256(json.dumps([a.lon, a.lat, b.lon, b.lat]).encode()).hexdigest()[:12]
                self.segments.append({'id': f'{a.id}:{b.id}', 'physical_id': physical,
                    'tr_id': tr, 'index': index, 'a': a, 'b': b, 'coordinates': coordinates,
                    'geometry_source': 'historical_gps' if observed else 'schedule_chord'})

    @classmethod
    def from_csv(cls, path, geometry=None):
        if not path:
            return cls(geometry=geometry)
        with Path(path).open(encoding="utf-8-sig", newline="") as source:
            return cls(csv.DictReader(source), geometry=geometry)

    def target(self, tr_id, now):
        return next((s for s in self.by_vehicle.get(tr_id, ())
                     if 600 < (s.time - now).total_seconds() <= 900), None)

    def plan_for(self, ids):
        return [s.plan() for tr in ids for s in self.by_vehicle.get(tr, ())]

    def network(self):
        routes, features = [], []
        for tr, stops in sorted(self.by_vehicle.items()):
            routes.append({"id": f"schedule-{tr}", "tr_id": tr, "name": f"Рейс ТС {tr}",
                           "stops": [{"id": s.id, "name": s.name, "lon": s.lon, "lat": s.lat,
                                      "time_begin": s.time.isoformat()} for s in stops]})
        for segment in self.segments:
            a, b = segment['a'], segment['b']
            features.append({'type': 'Feature', 'properties': {
                'segment_id': segment['id'], 'physical_id': segment['physical_id'], 'tr_id': segment['tr_id'],
                'from_stop_id': a.id, 'to_stop_id': b.id, 'from_name': a.name, 'to_name': b.name,
                'geometry_source': segment['geometry_source'], 'direction': f'{a.name} → {b.name}'},
                'geometry': {'type': 'LineString', 'coordinates': segment['coordinates']}})
        return {"graph_version": self.version, "geometry_kind": "historical_gps_with_plan_fallback",
                'geometry_history_until': self.geometry.history_until if self.geometry else None,
                "routes": routes, "type": "FeatureCollection", "features": features}

    def target_segment(self, stop_id):
        segment = next((s for s in self.segments if s['b'].id == stop_id), None)
        if not segment:
            return None
        return {'segment_id': segment['id'], 'graph_version': self.version,
                'from_stop_id': segment['a'].id, 'to_stop_id': segment['b'].id,
                'from_name': segment['a'].name, 'to_name': segment['b'].name,
                'basis': 'target_approach', 'geometry_source': segment['geometry_source']}

    def match(self, point):
        if not point.location_valid or point.tr_id is None:
            return None
        if point.tr_id not in self.by_vehicle:
            return None
        candidates = {}
        previous = self.previous_match.get(point.tr_id)
        for segment in self.segments:
            if segment['tr_id'] != point.tr_id:
                continue
            a, b = segment['a'], segment['b']
            if not a.time - timedelta(minutes=20) <= point.event_time <= b.time + timedelta(minutes=20):
                continue
            pieces = [(project((point.lon, point.lat), c, d), c, d)
                      for c, d in zip(segment['coordinates'], segment['coordinates'][1:]) if distance(c, d) > .1]
            if not pieces:
                continue
            (meters, fraction, xy), c, d = min(pieces, key=lambda x: x[0][0])
            if meters > 80:
                continue
            score = meters
            if point.heading is not None and point.speed is not None and point.speed >= 3:
                bearing = math.degrees(math.atan2((d[0]-c[0]) * math.cos(math.radians(point.lat)), d[1]-c[1])) % 360
                delta = abs((point.heading-bearing+180) % 360-180)
                if delta > 120:
                    continue
                score += delta / 180 * 30
            outside = max(0, (a.time-point.event_time).total_seconds(), (point.event_time-b.time).total_seconds())
            score += min(15, outside / 60)
            if previous and 0 <= (point.event_time-previous[0]).total_seconds() <= 120:
                jump = segment['index']-previous[1]
                score += 35 if jump < 0 or jump > 3 else 0
            key = segment['physical_id']
            candidate = (score, segment, xy)
            if key not in candidates or score < candidates[key][0]:
                candidates[key] = candidate
        candidates = sorted(candidates.values(), key=lambda x: x[0])
        if not candidates:
            return {"status": "unmatched"}
        first = candidates[0]
        if len(candidates) > 1 and candidates[1][0] - first[0] < 8:
            return {"status": "ambiguous"}
        segment = first[1]
        if not previous or point.event_time > previous[0]:
            self.previous_match[point.tr_id] = (point.event_time, segment['index'])
        return {"status": "matched", "snapped_position": {"lon": first[2][0], "lat": first[2][1]},
                "segment_id": segment['id'], "route_id": f"schedule-{point.tr_id}",
                "direction": f"{segment['a'].name} → {segment['b'].name}"}


@dataclass(frozen=True)
class Arrival:
    tr_id: int
    stop_id: str
    event_time: datetime
    received_at: datetime
    planned_time: datetime

    @property
    def deviation(self):
        return (self.event_time - self.planned_time).total_seconds()


class ArrivalEstimator:
    """Conservative GPS-only estimate. Future schedule actuals are never read."""
    def __init__(self, schedule, radius_m=50, speed_kmh=5, dwell_s=10, max_gap_s=45,
                 max_early_s=180, max_estimate_age_s=300):
        self.schedule = schedule
        self.radius_m, self.speed_kmh, self.dwell_s, self.max_gap_s = radius_m, speed_kmh, dwell_s, max_gap_s
        self.candidates = {}
        self.arrivals: dict[int, list[Arrival]] = {}
        self.seen = set()
        self.last_time = {}
        self.last_stop = {}
        self.departed = {}
        self.max_early_s = max_early_s
        self.max_estimate_age_s = max_estimate_age_s
        self.stationary_since = {}

    def ingest(self, point):
        tr = point.tr_id
        if tr is None or point.event_time <= self.last_time.get(tr, datetime.min.replace(tzinfo=timezone.utc)):
            return
        prior_time = self.last_time.get(tr)
        self.last_time[tr] = point.event_time
        if not point.location_valid or point.speed is None:
            self.candidates.pop(tr, None)
            self.stationary_since.pop(tr, None)
            return
        if point.speed > self.speed_kmh:
            self.stationary_since.pop(tr, None)
        else:
            anchor = self.stationary_since.get(tr)
            if (not anchor or (prior_time and (point.event_time-prior_time).total_seconds() > self.max_gap_s)
                    or distance((point.lon, point.lat), anchor[1]) > self.radius_m):
                self.stationary_since[tr] = (point.event_time, (point.lon, point.lat))
        previous = self.last_stop.get(tr)
        if previous and distance((point.lon, point.lat), (previous.lon, previous.lat)) > self.radius_m + 30:
            self.departed[tr] = True
        if previous and not self.departed.get(tr):
            return
        candidates = []
        recent = self.latest(tr, point.receive_time)
        expected_deviation = recent.deviation if recent else 0
        if point.speed <= self.speed_kmh:
            first_seen = self.stationary_since[tr][0]
            for stop in self.schedule.by_vehicle.get(tr, ()):
                if stop.id in self.seen or (previous and stop.time <= previous.time):
                    continue
                if abs((stop.time - point.event_time).total_seconds()) > 1200:
                    continue
                if (first_seen-stop.time).total_seconds() < -self.max_early_s:
                    continue
                meters = distance((point.lon, point.lat), (stop.lon, stop.lat))
                if meters <= self.radius_m:
                    residual = abs((stop.time-point.event_time).total_seconds()+expected_deviation)
                    candidates.append((meters, residual, stop))
        candidates.sort(key=lambda x: (x[1], x[0]))
        if not candidates or (len(candidates) > 1 and abs(candidates[1][1] - candidates[0][1]) < 60):
            self.candidates.pop(tr, None)
            return
        stop = candidates[0][2]
        old = self.candidates.get(tr)
        if not old or old[0].id != stop.id or (point.event_time - old[2]).total_seconds() > self.max_gap_s:
            self.candidates[tr] = (stop, point.event_time, point.event_time)
            return
        self.candidates[tr] = (stop, old[1], point.event_time)
        if (point.event_time - old[1]).total_seconds() < self.dwell_s:
            return
        event = Arrival(tr, stop.id, old[1], point.receive_time, stop.time)
        self.arrivals.setdefault(tr, []).append(event)
        self.seen.add(stop.id)
        self.last_stop[tr] = stop
        self.departed[tr] = False
        self.candidates.pop(tr, None)

    def latest(self, tr_id, T):
        return next((a for a in reversed(self.arrivals.get(tr_id, ()))
                     if 0 <= (T-a.event_time).total_seconds() <= self.max_estimate_age_s and a.received_at <= T), None)
