"""Historical movement controlled through the unmodified organizer emulator REST API.

CSV is input to the emulator controller, never a shortcut into FleetState or ML.
The emulator timestamps its output now; the known plan receives the same fixed
wall-clock shift. This is a causal scenario at 1x, not an exact CSV replay.
"""
import asyncio
import csv
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import time

import httpx

from .schedule import stamp

LOG = logging.getLogger("scenario")


def load_scenario(directory, start, duration):
    """Read telemetry and plan only; labels and actual arrival times are excluded."""
    directory = Path(directory)
    start = stamp(start)
    end = start + timedelta(seconds=duration)
    rows, mapping = [], {}
    with (directory / "traffic.csv").open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            received, event = stamp(row["receive_time"]), stamp(row["event_time"])
            if not start <= received < end:
                continue
            unit, tr = int(row["unit_id"]), int(row["tr_id"])
            if not 0 <= unit <= 2147483647 or (unit in mapping and mapping[unit] != tr):
                raise ValueError("Invalid or ambiguous scenario unit mapping")
            mapping[unit] = tr
            valid = row["location_valid"].lower() == "true"
            values = {k: float(row[k]) if valid and row[k] else None
                      for k in ("lon", "lat", "speed", "heading", "alt")}
            if valid and (values["lon"] is None or values["lat"] is None or
                          not -180 <= values["lon"] <= 180 or not -90 <= values["lat"] <= 90):
                raise ValueError("Scenario contains invalid GPS coordinates")
            if any(v is not None and not math.isfinite(v) for v in values.values()):
                raise ValueError("Scenario contains non-finite telemetry")
            rows.append({"unit_id": unit, "tr_id": tr, "event_time": event,
                         "receive_time": received, "location_valid": valid, **values})
    if not rows:
        raise ValueError("No telemetry in SCENARIO_START / SCENARIO_DURATION_S window")
    rows.sort(key=lambda r: (max(r["receive_time"], r["event_time"]), r["event_time"], r["unit_id"]))
    ids, plan = set(mapping.values()), []
    with (directory / "schedule_plan.csv").open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if int(row["tr_id"]) in ids and start - timedelta(minutes=35) <= stamp(row["time_begin"]) <= end + timedelta(minutes=20):
                plan.append({k: row[k] for k in ("tt_action_item_id", "tr_id", "time_begin", "geom")}
                            | {"building_address": row.get("building_address", "")})
    if not plan:
        raise ValueError("No planned arrivals for the selected scenario")
    return {"source_start": start.isoformat(), "duration_s": duration,
            "mapping": mapping, "schedule": plan, "traffic": rows}


def nav_fields(row):
    valid = row["location_valid"]
    lon, lat = row["lon"] or 0, row["lat"] or 0
    integer = lambda key, limit=65535: max(0, min(limit, round(row[key] or 0)))
    return {"longitude": round(abs(lon) * 1e7), "latitude": round(abs(lat) * 1e7),
            "extraDopBit5": lat >= 0, "extraDopBit6": lon >= 0, "extraDopBit7": valid,
            "speedAvg": integer("speed"), "speedMax": integer("speed"),
            "course": integer("heading", 360), "altitude": integer("alt"), "nsat": 12 if valid else 0}


class ScenarioCursor:
    def __init__(self, rows, max_age_s=30):
        
        
        self.rows = sorted(rows, key=lambda r: (max(r["receive_time"], r["event_time"]), r["event_time"]))
        self.max_age_s = max_age_s
        self.reset()

    def reset(self):
        self.index = 0
        self.last_event = {}
        self.skipped = {"old_or_duplicate": 0, "stale": 0, "future": 0, "coalesced": 0}

    def collect(self, source_now):
        """New, already available measurements only; do not refresh a silent bus."""
        batch = {}
        while self.index < len(self.rows) and max(self.rows[self.index]["receive_time"], self.rows[self.index]["event_time"]) <= source_now:
            row = self.rows[self.index]
            self.index += 1
            event, unit = row["event_time"], row["unit_id"]
            if event > source_now:
                self.skipped["future"] += 1
                continue
            if event <= self.last_event.get(unit, datetime.min.replace(tzinfo=timezone.utc)):
                self.skipped["old_or_duplicate"] += 1
                continue
            self.last_event[unit] = event
            if (source_now - event).total_seconds() > self.max_age_s:
                self.skipped["stale"] += 1
                continue
            if unit in batch:
                self.skipped["coalesced"] += 1
            batch[unit] = row
        return batch


class ScenarioController:
    def __init__(self, pipeline, settings):
        self.pipeline, self.settings = pipeline, settings
        self.url = settings.emulator_url.rstrip("/")
        self.cursor = ScenarioCursor(pipeline.bundle["traffic"], settings.stale_after_s)
        self.session = None
        self.started = False
        self.expected = {}
        self.status, self.error = "starting", None
        self.configured_at = None
        self.active_units = 0
        self.sent = self.accepted = self.ignored = self.missed = self.posts = 0
        self.last_post_ms = None
        self.last_received_at = None
        self.next_probe = 0

    def snapshot(self):
        return {"status": self.status, "error": self.error, "configured_at": self.configured_at,
                "configured_units": len(self.pipeline.mapping), "active_units": self.active_units,
                "auto_generate": False, "interval_ms": round(self.settings.scenario_tick_s * 1000),
                "sent_measurements": self.sent, "accepted_measurements": self.accepted,
                "ignored_packets": self.ignored, "missed_measurements": self.missed,
                "configuration_posts": self.posts, "last_post_ms": self.last_post_ms,
                "last_received_at": self.last_received_at,
                "source_rows_processed": self.cursor.index, "source_rows_total": len(self.cursor.rows),
                "skipped": dict(self.cursor.skipped)}

    def accept(self, event):
        """Only actual decoded NDTP matching a pending scenario command can enter state."""
        expected = self.expected.get(event["unit_id"])
        if not expected or self.session != self.pipeline.session_id:
            self.ignored += 1
            return False
        row, sent_at = expected
        fields = nav_fields(row)
        received, measured = stamp(event["receive_time"]), stamp(event["event_time"])
        good = (sent_at.replace(microsecond=0) <= measured <= received and
                0 <= (received - sent_at).total_seconds() <= 5 and
                event["location_valid"] == row["location_valid"])
        if good and row["location_valid"]:
            lon = fields["longitude"] / 1e7 * (1 if fields["extraDopBit6"] else -1)
            lat = fields["latitude"] / 1e7 * (1 if fields["extraDopBit5"] else -1)
            good = (abs(event["lon"] - lon) < 1e-8 and abs(event["lat"] - lat) < 1e-8 and
                    event["speed"] == fields["speedAvg"] and event["heading"] == fields["course"])
        if not good:
            self.ignored += 1
            return False
        del self.expected[event["unit_id"]]
        self.accepted += 1
        self.last_received_at = received.isoformat()
        return True

    async def post(self, client, batch, now):
        self.missed += len(self.expected)
        self.expected = {unit: (row, now) for unit, row in batch.items()}
        config = {"targetHost": self.settings.emulator_target_host, "targetPort": self.settings.ndtp_port,
                  "units": [{"unitId": unit, "autoGenerate": False,
                             
                             
                             "intervalMs": 86400000,
                             "cells": [{"type": "G6CellNav00", "fields": nav_fields(row)}]}
                            for unit, row in sorted(batch.items())]}
        started = time.monotonic()
        response = await client.post(f"{self.url}/api/config", json=config)
        response.raise_for_status()
        self.last_post_ms = round((time.monotonic() - started) * 1000, 2)
        self.configured_at = now.isoformat()
        self.active_units = len(batch)
        self.sent += len(batch)
        self.posts += 1

    async def step(self, client, now=None):
        now = now or datetime.now(timezone.utc)
        if not self.started:
            response = await client.get(f"{self.url}/api/config")
            response.raise_for_status()
            
            self.pipeline.boot, self.pipeline.epoch = now, -1
            self.started = True
        self.pipeline.ensure_session(now)
        if self.session != self.pipeline.session_id:
            
            await self.post(client, {}, now)
            self.session = self.pipeline.session_id
            self.cursor.reset()
        source_now = now - timedelta(seconds=self.pipeline.shift_s)
        batch = self.cursor.collect(source_now)
        if batch:
            await self.post(client, batch, now)
        elif now.timestamp() >= self.next_probe:
            response = await client.get(f"{self.url}/api/config")
            response.raise_for_status()
            self.next_probe = now.timestamp() + 5
        
        for unit, (_, sent_at) in list(self.expected.items()):
            if (now - sent_at).total_seconds() > 5:
                self.expected.pop(unit)
                self.missed += 1
        self.status, self.error = "ready", None

    async def run(self):
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                started = time.monotonic()
                try:
                    await self.step(client)
                except (httpx.HTTPError, ValueError) as error:
                    if self.status != "unavailable":
                        LOG.warning("Scenario emulator unavailable: %s", error)
                    self.status, self.error = "unavailable", str(error)
                    self.missed += len(self.expected)
                    self.expected.clear()
                await asyncio.sleep(max(.1, self.settings.scenario_tick_s - (time.monotonic() - started)))
