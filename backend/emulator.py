"""Configure the organizer's emulator over REST; telemetry still arrives over NDTP TCP."""
import asyncio
from datetime import datetime, timezone
import logging

import httpx

LOG = logging.getLogger("emulator")


class EmulatorController:
    def __init__(self, url, mapping, interval_ms=5000, target_host="backend", target_port=9201):
        if not mapping or interval_ms < 1:
            raise ValueError("Emulator needs a nonempty unit mapping and positive interval")
        if any(not 0 <= int(unit) <= 2147483647 for unit in mapping):
            raise ValueError("Emulator unit IDs must fit the documented int32 range")
        self.url = url.rstrip("/")
        self.config = {"targetHost": target_host, "targetPort": target_port, "units": [
            {"unitId": int(unit), "intervalMs": interval_ms, "autoGenerate": True, "cells": []}
            for unit in sorted(mapping)]}
        self.status = "starting"
        self.error = None
        self.configured_at = None
        self.applied = False
        self.active_units = 0

    def snapshot(self):
        return {"status": self.status, "error": self.error, "configured_at": self.configured_at,
                "configured_units": len(self.config["units"]), "active_units": self.active_units,
                "interval_ms": self.config["units"][0]["intervalMs"], "auto_generate": True}

    async def sync(self, client):
        response = await client.get(f"{self.url}/api/config")
        response.raise_for_status()
        current = response.json()
        if not isinstance(current, dict) or not isinstance(current.get("units"), list):
            raise ValueError("Unexpected emulator /api/config response")
        
        
        if not self.applied or not current["units"]:
            response = await client.post(f"{self.url}/api/config", json=self.config)
            response.raise_for_status()
            self.configured_at = datetime.now(timezone.utc).isoformat()
            self.applied = True
            current = self.config
            LOG.info("Configured official emulator: %s devices", len(current["units"]))
        self.active_units = len(current["units"])
        self.status, self.error = "ready", None

    async def run(self):
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                try:
                    await self.sync(client)
                except (httpx.HTTPError, ValueError) as error:
                    if self.status != "unavailable":
                        LOG.warning("Official emulator unavailable: %s", error)
                    self.status, self.error = "unavailable", str(error)
                await asyncio.sleep(5)
