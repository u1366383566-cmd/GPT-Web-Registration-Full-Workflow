# -*- coding: utf-8 -*-
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from core import twofa_service


class TwofaServiceSettingsTests(unittest.TestCase):
    def test_executor_supports_runtime_configured_worker_count(self):
        settings = twofa_service.queue_settings()

        self.assertGreaterEqual(settings["workers"], 1)
        self.assertLessEqual(settings["workers"], 16)
        self.assertGreaterEqual(settings["queue_limit"], settings["workers"])
        self.assertGreaterEqual(twofa_service._EXECUTOR._max_workers, settings["workers"])

    def test_actual_workers_obey_limit_and_runtime_increase(self):
        # 在网络入口前截停真实 worker，验证逻辑门控而不是线程池容量。
        entered = []
        changed = threading.Condition()
        release = threading.Event()
        def mark(account_id):
            with changed:
                entered.append(account_id)
                changed.notify_all()
            if not release.wait(5):
                raise TimeoutError("test worker did not release")
            return False
        with patch.object(twofa_service, "_RUNNING", set()), \
             patch.object(twofa_service, "_OUTSTANDING", 3), \
             patch.object(twofa_service, "_WORKERS", 1), \
             patch.object(twofa_service._twofa_cfg, "TWOFA_WORKERS", 2), \
             patch.object(twofa_service.db, "mark_account_totp_setup_running", side_effect=mark), \
             ThreadPoolExecutor(max_workers=3) as pool:
            try:
                futures = [pool.submit(twofa_service._run_twofa, account_id=i,
                    email=f"{i}@example.test", access_token="token", proxy=None, trigger="test")
                    for i in range(3)]
                with changed:
                    self.assertTrue(changed.wait_for(lambda: len(entered) == 1, timeout=3))
                    self.assertFalse(changed.wait_for(lambda: len(entered) > 1, timeout=0.1))
                twofa_service.apply_settings()
                with changed:
                    self.assertTrue(changed.wait_for(lambda: len(entered) == 2, timeout=3))
                    self.assertFalse(changed.wait_for(lambda: len(entered) > 2, timeout=0.1))
                # 降低并发不强杀已运行 worker；剩余任务等它们退出后再领取。
                with patch.object(twofa_service._twofa_cfg, "TWOFA_WORKERS", 1):
                    twofa_service.apply_settings()
                with changed:
                    self.assertFalse(changed.wait_for(lambda: len(entered) > 2, timeout=0.1))
            finally:
                release.set()
                for future in futures:
                    future.result(timeout=5)
            self.assertEqual(len(entered), 3)
            self.assertEqual(twofa_service._RUNNING, set())
            self.assertEqual(twofa_service._OUTSTANDING, 0)

    def test_integer_settings_are_bounded(self):
        original = getattr(twofa_service._twofa_cfg, "TWOFA_WORKERS", None)
        try:
            twofa_service._twofa_cfg.TWOFA_WORKERS = 999
            self.assertEqual(twofa_service._int_setting("TWOFA_WORKERS", 4, 1, 16), 16)
            twofa_service._twofa_cfg.TWOFA_WORKERS = -5
            self.assertEqual(twofa_service._int_setting("TWOFA_WORKERS", 4, 1, 16), 1)
        finally:
            twofa_service._twofa_cfg.TWOFA_WORKERS = original


if __name__ == "__main__":
    unittest.main()
