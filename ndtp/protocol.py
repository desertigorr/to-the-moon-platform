"""Decode the emulator's packed little-endian frames (see dataset/docs)."""

from dataclasses import dataclass
from datetime import datetime, timezone
import struct

NPL = struct.Struct("<HHHHBIH")  
NPH = struct.Struct("<HHHI")  
HANDSHAKE = struct.Struct("<HHHIII")  
NAV = struct.Struct("<IIIBBHHHHHBB")  


class ProtocolError(ValueError):
    """Malformed or unsupported NDTP frame."""


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def payload_size(header: bytes) -> int:
    if len(header) != NPL.size:
        raise ProtocolError("NPL header must contain 15 bytes")
    signature, size, flags, _, kind, _, _ = NPL.unpack(header)
    if signature != 0x7E7E or kind != 2:
        raise ProtocolError("Invalid NPL signature or payload type")
    if flags != 0:
        raise ProtocolError("Only the emulator's unencrypted zero-flag NPL is supported")
    if size < NPH.size:
        raise ProtocolError("NPL dataSize is smaller than NPH")
    return size


async def read_packet(reader) -> bytes:
    """Read exactly one frame regardless of TCP read boundaries."""
    header = await reader.readexactly(NPL.size)
    return header + await reader.readexactly(payload_size(header))


@dataclass(frozen=True)
class Frame:
    unit_id: int
    service_id: int
    message_type: int
    request_id: int
    body: bytes


def decode_frame(raw: bytes) -> Frame:
    size = payload_size(raw[:NPL.size])
    if len(raw) != NPL.size + size:
        raise ProtocolError("Frame length does not match NPL dataSize")
    _, _, _, checksum, _, unit_id, _ = NPL.unpack_from(raw)
    actual = crc16_modbus(raw[NPL.size:])
    swapped = ((actual & 0xFF) << 8) | (actual >> 8)
    if checksum != swapped:
        raise ProtocolError("CRC mismatch")
    service, kind, flags, request = NPH.unpack_from(raw, NPL.size)
    if flags != 1:
        raise ProtocolError("Only emulator request packets are supported")
    return Frame(unit_id, service, kind, request, raw[NPL.size + NPH.size:])


def decode_handshake(frame: Frame) -> dict:
    if (frame.service_id, frame.message_type) != (0, 100):
        raise ProtocolError("Expected CONN_REQUEST handshake")
    if len(frame.body) != HANDSHAKE.size:
        raise ProtocolError("Handshake body must contain 18 bytes")
    high, low, flags, peer, max_size, reserved = HANDSHAKE.unpack(frame.body)
    if (high, low) != (6, 2) or flags != 0 or peer != frame.unit_id:
        raise ProtocolError("Unsupported handshake version, flags, or peerAddress")
    return {"version": "6.2", "unit_id": peer, "max_packet_size": max_size,
            "reserved": reserved}


def decode_navigation(frame: Frame) -> dict:
    if (frame.service_id, frame.message_type) != (1, 101):
        raise ProtocolError("Expected NAVDATA REALTIME")
    if len(frame.body) < 2 + NAV.size or frame.body[:2] != b"\x00\x00":
        raise ProtocolError("First cell must be G6CellNav00 number 0 with 26-byte payload")
    (timestamp, lon, lat, flags, battery, speed, max_speed, course,
     track, altitude, satellites, pdop) = NAV.unpack_from(frame.body, 2)
    try:
        event_time = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ProtocolError("Invalid navigation timestamp") from error
    valid = bool(flags & 0x80)
    longitude = lon / 10_000_000 * (1 if flags & 0x40 else -1)
    latitude = lat / 10_000_000 * (1 if flags & 0x20 else -1)
    if valid and (abs(longitude) > 180 or abs(latitude) > 90 or course > 360):
        raise ProtocolError("Invalid navigation coordinates or course")
    return {
        "event_time": event_time,
        "location_valid": valid,
        "lon": longitude if valid else None,
        "lat": latitude if valid else None,
        "alt": altitude if valid else None,
        "speed": speed if valid else None,
        "heading": course if valid else None,
        "nav_flags": flags,
        "battery_mv": battery * 20,
        "speed_max": max_speed,
        "track_m": track,
        "satellites": satellites,
        "pdop": pdop,
        
        "additional_cells_hex": frame.body[2 + NAV.size:].hex(),
    }
