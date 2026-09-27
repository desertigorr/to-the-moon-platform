"""Receive NDTP over TCP, print JSON and save traffic-compatible CSV columns."""

import argparse
import asyncio
import csv
import errno
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import time
import uuid

from .protocol import (ProtocolError, decode_frame, decode_handshake,
                       decode_navigation, read_packet)

FIELDS = ["packet_id", "tr_id", "unit_id", "event_time", "device_event_id",
          "location_valid", "gps_time", "lon", "lat", "alt", "speed",
          "heading", "receive_time", "is_hist_data"]
LOG = logging.getLogger("ndtp")


def load_mapping(path: Path | None) -> dict[int, int]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Mapping must be a JSON object: unit_id -> tr_id")
    return {int(unit): int(vehicle) for unit, vehicle in data.items()}


def parse_offset(value: str) -> timezone:
    try:
        sign = 1 if value[0] == "+" else -1
        hours, minutes = map(int, value[1:].split(":"))
        if value[0] not in "+-" or not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            raise ValueError
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    except (IndexError, ValueError) as error:
        raise argparse.ArgumentTypeError("Use +00:00 or +03:00, for example") from error


def make_row(frame, nav: dict, mapping: dict[int, int], received: datetime,
             packet_id: int, output_tz: timezone) -> dict:
    def stamp(value: datetime) -> str:
        return value.astimezone(output_tz).strftime("%Y-%m-%d %H:%M:%S.%f")

    return {
        "packet_id": packet_id,  
        "tr_id": mapping.get(frame.unit_id),
        "unit_id": frame.unit_id,
        "event_time": stamp(nav["event_time"]),
        "device_event_id": 0,  
        "location_valid": nav["location_valid"],
        "gps_time": stamp(nav["event_time"]) if nav["location_valid"] else None,
        "lon": nav["lon"], "lat": nav["lat"], "alt": nav["alt"],
        "speed": nav["speed"], "heading": nav["heading"],
        "receive_time": stamp(received),
        "is_hist_data": False,  
    }


class Sink:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=False)
        self.csv_file = (directory / "traffic.csv").open("w", encoding="utf-8", newline="")
        self.raw_file = (directory / "packets.jsonl").open("w", encoding="utf-8")
        self.writer = csv.DictWriter(self.csv_file, fieldnames=FIELDS)
        self.writer.writeheader()
        self.csv_file.flush()
        self.packet_id = time.time_ns()

    def raw(self, raw: bytes, received: datetime, **metadata):
        self.raw_file.write(json.dumps({"receive_time_utc": received.isoformat(),
                                        "hex": raw.hex(), **metadata}) + "\n")
        self.raw_file.flush()

    def row(self, row: dict):
        self.writer.writerow(row)
        self.csv_file.flush()
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def close(self):
        self.csv_file.close()
        self.raw_file.close()


@dataclass
class ReceiverOptions:
    host: str = "0.0.0.0"
    port: int = 9201
    mapping: Path | None = None
    output_dir: Path = Path("artifacts/ndtp")
    timezone_offset: timezone = timezone.utc
    duration: float | None = None
    max_events: int | None = None


class Receiver:
    """Reusable TCP receiver; on_event is a quick synchronous in-process callback."""

    def __init__(self, args: ReceiverOptions, on_event=None):
        self.args = args
        self.on_event = on_event
        self.mapping = load_mapping(args.mapping)
        self.stop = asyncio.Event()
        self.clients = set()
        self.warned = set()
        self.counts = {"telemetry": 0, "rejected": 0}
        self.server = None
        self.sink = None
        self.directory = None

    async def start(self):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.directory = self.args.output_dir / run_id
        self.sink = Sink(self.directory)
        try:
            self.server = await asyncio.start_server(self.handle, self.args.host, self.args.port)
        except BaseException:
            self.sink.close()
            self.sink = None
            raise
        LOG.info("NDTP listening on %s; output=%s; timestamps=%s",
                 [socket.getsockname() for socket in self.server.sockets],
                 self.directory.resolve(), self.args.timezone_offset)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self.clients.add(task)
        peer = writer.get_extra_info("peername")
        unit = None
        LOG.info("Connected %s", peer)
        try:
            while not self.stop.is_set():
                
                raw = await read_packet(reader)
                received = datetime.now(timezone.utc)
                try:
                    frame = decode_frame(raw)
                    if (frame.service_id, frame.message_type) == (0, 100):
                        hello = decode_handshake(frame)
                        unit = frame.unit_id
                        self.sink.raw(raw, received, kind="handshake", **hello)
                        LOG.info("Handshake unit_id=%s NDTP=%s", unit, hello["version"])
                        
                        continue
                    if unit is None or frame.unit_id != unit:
                        raise ProtocolError("Realtime packet has no matching handshake")
                    nav = decode_navigation(frame)
                    self.sink.raw(raw, received, kind="realtime", unit_id=unit,
                             request_id=frame.request_id,
                             additional_cells_hex=nav["additional_cells_hex"])
                    if unit not in self.mapping and unit not in self.warned:
                        self.warned.add(unit)
                        LOG.warning("No tr_id mapping for unit_id=%s; leaving tr_id empty", unit)
                    self.sink.packet_id += 1
                    row = make_row(frame, nav, self.mapping, received,
                                   self.sink.packet_id, self.args.timezone_offset)
                    self.sink.row(row)
                    if self.on_event is not None:
                        
                        event = dict(row, packet_id=str(row["packet_id"]),
                                     event_time=nav["event_time"].isoformat(),
                                     gps_time=nav["event_time"].isoformat() if nav["location_valid"] else None,
                                     receive_time=received.isoformat())
                        self.on_event(event)
                    self.counts["telemetry"] += 1
                    if self.args.max_events and self.counts["telemetry"] >= self.args.max_events:
                        self.stop.set()
                except ProtocolError as error:
                    self.counts["rejected"] += 1
                    self.sink.raw(raw, received, kind="rejected", error=str(error))
                    LOG.warning("Rejected packet from %s: %s", peer, error)
        except asyncio.IncompleteReadError as error:
            if error.partial:
                LOG.warning("Truncated packet from %s (%s bytes)", peer, len(error.partial))
        except (ProtocolError, ConnectionError, OSError) as error:
            LOG.warning("Closing connection %s: %s", peer, error)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.clients.discard(task)
            LOG.info("Disconnected %s", peer)

    async def wait(self):
        try:
            if self.args.duration:
                await asyncio.wait_for(self.stop.wait(), timeout=self.args.duration)
            else:
                await self.stop.wait()
        except TimeoutError:
            pass

    async def close(self):
        self.stop.set()
        if self.server is not None:
            self.server.close()
        
        
        
        tasks = list(self.clients)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
            self.server = None
        if self.sink is not None:
            self.sink.close()
            self.sink = None
        LOG.info("Finished: %s; output=%s", self.counts, self.directory)


async def serve(args):
    receiver = Receiver(args)
    try:
        await receiver.start()
        await receiver.wait()
    finally:
        await receiver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Listen on host interfaces for Docker")
    parser.add_argument("--port", type=int, default=9201)
    parser.add_argument("--mapping", type=Path, help="JSON mapping unit_id -> tr_id")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/ndtp"))
    parser.add_argument("--timezone-offset", type=parse_offset, default=timezone.utc,
                        help="CSV time offset; UTC by default, use +03:00 only when intended")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds")
    parser.add_argument("--max-events", type=int, help="Stop after this many navigation rows")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or (args.duration is not None and args.duration <= 0) or (
            args.max_events is not None and args.max_events <= 0):
        parser.error("Port must be 1..65535; duration and max-events must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        if error.errno == errno.EADDRINUSE or getattr(error, "winerror", None) == 10048:
            LOG.error("Port %s is already in use. start-backend.ps1 includes this NDTP receiver; "
                      "run either the backend or receive.ps1. Stop the existing process first "
                      "if you want to switch modes.", args.port)
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()
