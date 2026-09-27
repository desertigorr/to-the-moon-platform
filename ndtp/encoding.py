"""NDTP encoding helpers for protocol verification."""
from datetime import datetime
from .protocol import NPL, NPH, NAV, HANDSHAKE, crc16_modbus

def frame(unit, service, kind, request, body):
    payload = NPH.pack(service, kind, 1, request) + body
    crc = crc16_modbus(payload)
    return NPL.pack(0x7e7e, len(payload), 0, ((crc & 255) << 8) | (crc >> 8), 2, unit, 0) + payload


def hello(unit):
    return frame(unit, 0, 100, 1, HANDSHAKE.pack(6, 2, 0, unit, 65535, 0))


def navigation(row, shift, request):
    timestamp = int(datetime.fromisoformat(row["event_time"]).timestamp() + shift)
    valid = row["location_valid"]
    lon, lat = row["lon"] or 0, row["lat"] or 0
    flags = (128 if valid else 0) | (64 if lon >= 0 else 0) | (32 if lat >= 0 else 0)
    integer = lambda key, ceiling=65535: max(0, min(ceiling, round(row[key] or 0)))
    nav = NAV.pack(timestamp, round(abs(lon) * 1e7), round(abs(lat) * 1e7), flags, 180,
                   integer("speed"), integer("speed"), integer("heading", 360), 0,
                   integer("alt"), 12, 1)
    return frame(row["unit_id"], 1, 101, request, b"\x00\x00" + nav)


