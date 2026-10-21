# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import main


class ProtocolFingerprintModeTests(unittest.TestCase):
    def test_fresh_mode_does_not_supply_cross_task_seed(self):
        with patch.object(main._register_cfg, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", False):
            self.assertIsNone(main._protocol_registration_fingerprint_seed("User@Example.com"))

    def test_same_email_mode_normalizes_identity(self):
        with patch.object(main._register_cfg, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True):
            first = main._protocol_registration_fingerprint_seed(" User@Example.com ")
            second = main._protocol_registration_fingerprint_seed("user@example.com")
            other = main._protocol_registration_fingerprint_seed("other@example.com")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


if __name__ == "__main__":
    unittest.main()
