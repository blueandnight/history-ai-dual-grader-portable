import threading
import time
import unittest
from unittest.mock import patch

import app


class StatusCacheTests(unittest.TestCase):
    def test_cold_poll_does_not_wait_for_slow_enumeration_and_only_one_refresh_runs(self):
        started, release = threading.Event(), threading.Event()
        threads = []
        real_thread = threading.Thread

        def thread_factory(*a, **kw):
            thread = real_thread(*a, **kw)
            threads.append(thread)
            return thread

        def slow():
            started.set()
            if not release.wait(3):
                raise RuntimeError('test cleanup failed')
            return [{'address': '192.168.1.2', 'connection_label': 'Wi-Fi'}]

        with patch.object(app, 'NETWORK_INTERFACE_CACHE', []), \
             patch.object(app, 'NETWORK_INTERFACE_CACHE_AT', 0), \
             patch.object(app, 'NETWORK_INTERFACE_REFRESH_IN_FLIGHT', False), \
             patch.object(app, 'local_network_interfaces', side_effect=slow) as enumerate_, \
             patch.object(app, '_socket_network_interfaces', return_value=[{'address': '192.168.1.2'}]), \
             patch.object(app.threading, 'Thread', side_effect=thread_factory):
            try:
                self.assertEqual(app.cached_local_network_interfaces(), [{'address': '192.168.1.2'}])
                self.assertTrue(started.wait(1))
                app.cached_local_network_interfaces()
                self.assertEqual(len(threads), 1)
                self.assertEqual(enumerate_.call_count, 1)
            finally:
                release.set()
                for thread in threads:
                    thread.join(3)
            result = app.cached_local_network_interfaces()
            self.assertEqual(result[0]['connection_label'], 'Wi-Fi')
            result[0]['address'] = 'changed'
            self.assertEqual(app.cached_local_network_interfaces()[0]['address'], '192.168.1.2')

    def test_fresh_empty_cache_does_not_restart_discovery(self):
        with patch.object(app, 'NETWORK_INTERFACE_CACHE', []), \
             patch.object(app, 'NETWORK_INTERFACE_CACHE_AT', time.monotonic()), \
             patch.object(app, '_start_network_interface_refresh') as refresh:
            self.assertEqual(app.cached_local_network_interfaces(), [])
            refresh.assert_not_called()

    def test_refresh_failure_retains_previous_cache_and_clears_inflight(self):
        threads = []
        real_thread = threading.Thread
        def thread_factory(*a, **kw):
            thread = real_thread(*a, **kw)
            threads.append(thread)
            return thread
        with patch.object(app, 'NETWORK_INTERFACE_CACHE', [{'address': '192.168.1.9'}]), \
             patch.object(app, 'NETWORK_INTERFACE_CACHE_AT', 0), \
             patch.object(app, 'NETWORK_INTERFACE_REFRESH_IN_FLIGHT', False), \
             patch.object(app, 'local_network_interfaces', side_effect=OSError('synthetic failure')), \
             patch.object(app.threading, 'Thread', side_effect=thread_factory):
            self.assertEqual(app.cached_local_network_interfaces(), [{'address': '192.168.1.9'}])
            for thread in threads:
                thread.join(3)
            self.assertFalse(app.NETWORK_INTERFACE_REFRESH_IN_FLIGHT)
            self.assertEqual(app.NETWORK_INTERFACE_CACHE, [{'address': '192.168.1.9'}])

    def test_explicit_refresh_remains_synchronous(self):
        with patch.object(app, 'NETWORK_INTERFACE_CACHE', []), patch.object(app, 'NETWORK_INTERFACE_CACHE_AT', 0), \
             patch.object(app, 'local_network_interfaces', return_value=[{'address': '192.168.1.3'}]), \
             patch.object(app, '_start_network_interface_refresh') as refresh:
            self.assertEqual(app.cached_local_network_interfaces(force_refresh=True), [{'address': '192.168.1.3'}])
            refresh.assert_not_called()
