# -*- coding: utf-8 -*-
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult


class _ConcurrentFakeClient(RoxyBrowserClient):
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def __init__(self):
        # 测试不需要建立 requests.Session。
        pass

    def _open_profile_locked(self, profile_id=None):
        with self.state_lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            time.sleep(0.03)
            value = str(profile_id or "generated")
            return RoxyOpenResult(value, {}, debugger_address="127.0.0.1:9222")
        finally:
            with self.state_lock:
                type(self).active -= 1


class RoxyWindowCreateLockTests(unittest.TestCase):
    def test_concurrent_open_profile_calls_are_serialized(self):
        _ConcurrentFakeClient.active = 0
        _ConcurrentFakeClient.max_active = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda index: _ConcurrentFakeClient().open_profile(str(index)), range(4)))

        self.assertEqual([item.profile_id for item in results], ["0", "1", "2", "3"])
        self.assertEqual(_ConcurrentFakeClient.max_active, 1)


if __name__ == "__main__":
    unittest.main()
