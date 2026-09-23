# -*- coding: utf-8 -*-
import socket
import threading
import unittest

from core.proxy_chain import ProxyChainRelay


class ProxyChainTrafficTests(unittest.TestCase):
    def test_relay_counts_both_directions(self):
        relay = object.__new__(ProxyChainRelay)
        relay._stop = threading.Event()
        relay._traffic_lock = threading.Lock()
        relay._upload_bytes = 0
        relay._download_bytes = 0

        browser, relay_client = socket.socketpair()
        relay_remote, upstream = socket.socketpair()
        thread = threading.Thread(target=relay._relay, args=(relay_client, relay_remote))
        thread.start()
        try:
            browser.sendall(b"upload")
            self.assertEqual(upstream.recv(6), b"upload")
            upstream.sendall(b"download")
            self.assertEqual(browser.recv(8), b"download")
        finally:
            relay._stop.set()
            browser.close()
            upstream.close()
            thread.join(timeout=2)
            relay_client.close()
            relay_remote.close()

        self.assertEqual(relay.traffic_snapshot(), {
            "available": True,
            "upload_bytes": 6,
            "download_bytes": 8,
            "total_bytes": 14,
        })


if __name__ == "__main__":
    unittest.main()
