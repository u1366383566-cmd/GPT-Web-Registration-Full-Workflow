import unittest
from unittest.mock import patch

from core.browser_data_saver import BrowserDataSaver, configured_resource_types
from core.roxybrowser_client import _apply_data_saver_open_args


class _Request:
    def __init__(self, resource_type, url):
        self.resource_type = resource_type
        self.url = url


class _Route:
    def __init__(self, request):
        self.request = request
        self.action = None
        self.error_code = None

    def abort(self, error_code=None):
        self.action = "abort"
        self.error_code = error_code

    def continue_(self):
        self.action = "continue"


class _Context:
    def __init__(self):
        self.route_pattern = None
        self.handler = None
        self.unroute_args = None

    def route(self, pattern, handler):
        self.route_pattern = pattern
        self.handler = handler

    def unroute(self, pattern, handler):
        self.unroute_args = (pattern, handler)


class _Driver:
    def __init__(self):
        self.commands = []

    def execute_cdp_cmd(self, command, params):
        self.commands.append((command, params))
        return {}


class BrowserDataSaverTests(unittest.TestCase):
    def test_disabled_mode_does_not_install_route(self):
        context = _Context()
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", False):
            saver = BrowserDataSaver(label="test")
            saver.install_playwright(context)

        self.assertFalse(saver.enabled)
        self.assertIsNone(context.handler)
        self.assertEqual(saver.snapshot()["data_saver_blocked_count"], 0)

    def test_playwright_blocks_optional_types_but_keeps_critical_requests(self):
        context = _Context()
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES",
            ["image", "media"],
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_playwright(context)
            image_request = _Request("image", "https://cdn.example/avatar.png")
            image = _Route(image_request)
            script = _Route(_Request("script", "https://cdn.example/app.js"))
            captcha = _Route(_Request("image", "https://auth.example/captcha.png"))
            context.handler(image)
            context.handler(script)
            context.handler(captcha)

        self.assertEqual(context.route_pattern, "**/*")
        self.assertEqual(image.action, "abort")
        self.assertEqual(image.error_code, "blockedbyclient")
        self.assertEqual(script.action, "continue")
        self.assertEqual(captcha.action, "continue")
        stats = saver.snapshot()
        self.assertEqual(stats["data_saver_blocked_count"], 1)
        self.assertEqual(stats["data_saver_blocked_by_type"], {"image": 1})
        self.assertTrue(saver.was_playwright_blocked(image_request))
        # 这里传入的是另一个对象；只有实际被 route.abort 的 request 才会被标记。
        self.assertFalse(saver.was_playwright_blocked(_Request("image", "https://cdn.example/avatar.png")))

        saver.stop()
        self.assertIsNotNone(context.unroute_args)

    def test_playwright_url_pattern_can_block_script_independently_of_resource_type(self):
        context = _Context()
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", []
        ), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS",
            ["**://metrics.example/**"],
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_playwright(context)
            telemetry = _Route(_Request("script", "https://metrics.example/sdk/main.js"))
            critical = _Route(_Request("script", "https://auth.example/assets/app.js"))
            context.handler(telemetry)
            context.handler(critical)

        self.assertEqual(telemetry.action, "abort")
        self.assertEqual(critical.action, "continue")
        stats = saver.snapshot()
        self.assertEqual(stats["data_saver_blocked_count"], 1)
        self.assertEqual(stats["data_saver_blocked_by_type"], {"script": 1})
        self.assertEqual(
            stats["data_saver_blocked_by_url_pattern"],
            {"**://metrics.example/**": 1},
        )
        self.assertEqual(stats["data_saver_blocked_url_patterns"], ["**://metrics.example/**"])

    def test_selenium_uses_static_resource_url_patterns(self):
        driver = _Driver()
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES",
            ["image", "media"],
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_selenium(driver)

        self.assertEqual(driver.commands[0][0], "Network.enable")
        self.assertEqual(driver.commands[1][0], "Network.setBlockedURLs")
        patterns = driver.commands[1][1]["urls"]
        self.assertIn("*.png*", patterns)
        self.assertIn("*.mp4*", patterns)
        self.assertNotIn("*.js*", patterns)
        self.assertEqual(saver.snapshot()["data_saver_method"], "selenium.cdp.Network.setBlockedURLs")

    def test_selenium_uses_configured_url_patterns_even_without_resource_types(self):
        driver = _Driver()
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", []
        ), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS",
            ["**://metrics.example/**"],
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_selenium(driver)
            patterns = driver.commands[-1][1]["urls"]
            self.assertIn("*://metrics.example/*", patterns)
            handled = saver.observe_cdp_event(
                "Network.loadingFailed",
                {"blockedReason": "inspector"},
                {"url": "https://metrics.example/collect", "resourceType": "Script"},
            )

        self.assertTrue(handled)
        self.assertEqual(saver.snapshot()["data_saver_blocked_count"], 1)
        self.assertEqual(saver.snapshot()["data_saver_blocked_by_type"], {"script": 1})
        self.assertEqual(
            saver.snapshot()["data_saver_blocked_by_url_pattern"],
            {"**://metrics.example/**": 1},
        )

    def test_resource_type_aliases_and_invalid_values(self):
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["images", "font", "not-a-type", "image"]):
            self.assertEqual(configured_resource_types(), ["image", "font"])

    def test_selenium_inspector_failure_is_counted(self):
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image"]
        ):
            saver = BrowserDataSaver(label="test")
            saver.observe_cdp_event(
                "Network.loadingFailed",
                {"blockedReason": "inspector"},
                {"resourceType": "Image"},
            )

        self.assertEqual(saver.snapshot()["data_saver_blocked_count"], 1)
        self.assertEqual(saver.snapshot()["data_saver_blocked_by_type"], {"image": 1})

    def test_roxy_adds_early_image_switch_only_when_image_is_selected(self):
        params = {"args": ["--lang=ja"]}
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image", "media"]
        ):
            _apply_data_saver_open_args(params)
        self.assertIn("--lang=ja", params["args"])
        self.assertIn("--blink-settings=imagesEnabled=false", params["args"])

        params = {"args": []}
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["media"]
        ):
            _apply_data_saver_open_args(params)
        self.assertNotIn("--blink-settings=imagesEnabled=false", params["args"])

    def test_stylesheet_is_a_supported_optional_resource_type(self):
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["stylesheet"]
        ):
            self.assertEqual(configured_resource_types(), ["stylesheet"])
            saver = BrowserDataSaver(label="test")
            driver = _Driver()
            saver.install_selenium(driver)

        self.assertIn("*.css*", driver.commands[-1][1]["urls"])

    def test_cdp_observer_rejects_unmatched_inspector_failure(self):
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image"]
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_selenium(_Driver())
            self.assertFalse(
                saver.observe_cdp_event(
                    "Network.loadingFailed",
                    {"blockedReason": "inspector"},
                    {"url": "https://example.test/app.js", "resourceType": "Script"},
                )
            )

        self.assertEqual(saver.snapshot()["data_saver_blocked_count"], 0)


if __name__ == "__main__":
    unittest.main()
