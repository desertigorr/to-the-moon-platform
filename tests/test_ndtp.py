import asyncio
from datetime import datetime, timezone
import unittest

from ndtp.protocol import (ProtocolError, crc16_modbus, decode_frame,
                           decode_handshake, decode_navigation, read_packet)
from ndtp.receiver import make_row, parse_offset



HELLO = bytes.fromhex(
    "7e7e1c000000e3d402812c0a00000000006400010001000000060002000000812c0a00ffff000000000000")
REALTIME = bytes.fromhex(
    "7e7e7b000000cbad02812c0a000000010065000100020000000000d2b8b76a2d8965167ca13e21"
    "e08305001100b3001a45bd000a04080001b402b1000f1000000000000800000002008a0031011802"
    "2f0205005b003d005f0030001a4500000f01060e0a00f2b001000f14000028fe1a00d800000032"
    "008d074e0005e902ca02d402a102bb0200000000")


def with_nav_flags(flags):
    raw = bytearray(REALTIME)
    raw[39] = flags  
    
    raw[6:8] = crc16_modbus(raw[15:]).to_bytes(2, "big")
    return bytes(raw)


class ProtocolTests(unittest.TestCase):
    def test_crc_known_modbus_vector(self):
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)

    def test_actual_emulator_handshake(self):
        self.assertEqual(decode_handshake(decode_frame(HELLO))["unit_id"], 666753)

    def test_actual_emulator_navigation_with_other_sensors(self):
        nav = decode_navigation(decode_frame(REALTIME))
        self.assertAlmostEqual(nav["lon"], 37.5753005)
        self.assertAlmostEqual(nav["lat"], 55.77527)
        self.assertEqual((nav["speed"], nav["heading"], nav["alt"]), (5, 179, 189))
        self.assertEqual(nav["event_time"].isoformat(), "2026-09-26T12:21:38+00:00")
        self.assertTrue(nav["additional_cells_hex"])

    def test_coordinate_signs(self):
        nav = decode_navigation(decode_frame(with_nav_flags(0x80)))
        self.assertAlmostEqual(nav["lon"], -37.5753005)
        self.assertAlmostEqual(nav["lat"], -55.77527)

    def test_invalid_navigation_is_missing_not_stationary(self):
        frame = decode_frame(with_nav_flags(0x60))
        nav = decode_navigation(frame)
        row = make_row(frame, nav, {}, datetime.now(timezone.utc), 1, timezone.utc)
        for field in ("lon", "lat", "speed", "heading", "alt", "gps_time", "tr_id"):
            self.assertIsNone(row[field], field)
        self.assertFalse(row["location_valid"])

    def test_mapping_and_explicit_time_offset(self):
        frame = decode_frame(REALTIME)
        nav = decode_navigation(frame)
        row = make_row(frame, nav, {666753: 120439}, nav["event_time"], 123, parse_offset("+03:00"))
        self.assertEqual(row["tr_id"], 120439)
        self.assertEqual(row["event_time"], "2026-09-26 15:21:38.000000")
        self.assertEqual(row["packet_id"], 123)

    def test_corruption_is_rejected(self):
        raw = bytearray(REALTIME)
        raw[-1] ^= 1
        with self.assertRaisesRegex(ProtocolError, "CRC"):
            decode_frame(bytes(raw))

    def test_wrong_length_and_header_are_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_frame(REALTIME[:-1])
        with self.assertRaises(ProtocolError):
            decode_frame(b"XX" + REALTIME[2:])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_and_coalesced_tcp(self):
        reader = asyncio.StreamReader()
        task = asyncio.create_task(read_packet(reader))
        reader.feed_data(HELLO[:3])
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        reader.feed_data(HELLO[3:20])
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        
        reader.feed_data(HELLO[20:] + REALTIME)
        self.assertEqual(await task, HELLO)
        self.assertEqual(await read_packet(reader), REALTIME)

    async def test_mid_packet_disconnect(self):
        reader = asyncio.StreamReader()
        reader.feed_data(REALTIME[:30])
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_packet(reader)


if __name__ == "__main__":
    unittest.main()
