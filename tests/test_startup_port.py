import socket
import threading
import unittest

from tools.wait_for_port import wait_for_port


class StartupPortTests(unittest.TestCase):
    def test_waits_for_previous_listener_without_stopping_it(self):
        with socket.socket() as previous:
            previous.bind(('127.0.0.1', 0))
            port = previous.getsockname()[1]
            previous.listen()
            release = threading.Timer(0.3, previous.close)
            release.start()
            try:
                wait_for_port('127.0.0.1', port, timeout=3)
            finally:
                release.join()

    def test_timeout_keeps_existing_listener_intact(self):
        with socket.socket() as previous:
            previous.bind(('127.0.0.1', 0))
            previous.listen()
            with self.assertRaises(TimeoutError):
                wait_for_port('127.0.0.1', previous.getsockname()[1], timeout=0.1)
            self.assertGreaterEqual(previous.fileno(), 0)
