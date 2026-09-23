# -*- coding: utf-8 -*-
import subprocess
import unittest
from unittest.mock import patch

from core import sentinel_runner


class SentinelRunnerAlignmentTests(unittest.TestCase):
    def test_password_flow_uses_auth_wrapper_sdk_and_real_screen_shape(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"p":"proof","c":"challenge","id":"device-id","flow":"username_password_create"}\n',
            stderr="",
        )
        profile = {
            "screen_width": 1680,
            "screen_height": 1050,
            "screen_avail_width": 1680,
            "screen_avail_height": 1025,
            "viewport_width": 1680,
            "viewport_height": 938,
            "color_depth": 24,
            "hardware_concurrency": 4,
            "webgl_vendor": "Google Inc. (Apple)",
            "webgl_renderer": "ANGLE (Apple, Apple M2, OpenGL 4.1)",
        }
        with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
            sentinel_runner.generate_sentinel_token(
                challenge={"token": "challenge", "_request_p": "request-proof"},
                flow="username_password_create",
                device_id="device-id",
                browser_profile=profile,
            )

        cmd = run.call_args.args[0]
        values = {cmd[i]: cmd[i + 1] for i in range(0, len(cmd) - 1) if cmd[i].startswith("--")}
        self.assertEqual(
            values["--script-src"],
            "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
        )
        self.assertEqual(values["--avail-width"], "1680")
        self.assertEqual(values["--avail-height"], "1025")
        self.assertEqual(values["--outer-width"], "1680")
        self.assertEqual(values["--outer-height"], "1025")
        self.assertEqual(values["--inner-width"], "1680")
        self.assertEqual(values["--inner-height"], "938")
        self.assertEqual(values["--color-depth"], "24")
        self.assertEqual(values["--cores"], "4")
        self.assertEqual(values["--challenge-proof"], "request-proof")
        self.assertEqual(values["--webgl-vendor"], "Google Inc. (Apple)")
        self.assertEqual(values["--webgl-renderer"], "ANGLE (Apple, Apple M2, OpenGL 4.1)")

    def test_otp_flow_uses_versioned_sdk(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"p":"proof","c":"challenge","id":"device-id","flow":"email_otp_validate"}\n',
            stderr="",
        )
        with patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
            sentinel_runner.generate_sentinel_token(
                challenge={"token": "challenge"},
                flow="email_otp_validate",
                device_id="device-id",
            )
        cmd = run.call_args.args[0]
        index = cmd.index("--script-src")
        self.assertEqual(
            cmd[index + 1],
            "https://sentinel.openai.com/sentinel/20260810913b/sdk.js",
        )


if __name__ == "__main__":
    unittest.main()
