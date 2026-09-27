import asyncio
from pathlib import Path
import tempfile
import unittest

from ndtp.receiver import Receiver, ReceiverOptions


class ReceiverLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_idle_tcp_client_without_waiting_for_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Receiver(ReceiverOptions(host="127.0.0.1", port=0, output_dir=Path(directory)))
            await receiver.start()
            reader, writer = await asyncio.open_connection("127.0.0.1", receiver.server.sockets[0].getsockname()[1])
            try:
                await asyncio.sleep(.01)
                self.assertEqual(len(receiver.clients), 1)
                await asyncio.wait_for(receiver.close(), timeout=1)
                self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
                self.assertFalse(receiver.clients)
                self.assertIsNone(receiver.sink)
            finally:
                writer.close()
                await writer.wait_closed()
                await receiver.close()


if __name__ == "__main__":
    unittest.main()
