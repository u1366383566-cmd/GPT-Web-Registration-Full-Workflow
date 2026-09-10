# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from core.browser_data_saver import BrowserDataSaver
from core.browser_traffic import PlaywrightTrafficTracker, SeleniumTrafficTracker


class _Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners.get(event, []).remove(callback)

    def emit(self, event, *args):
        for callback in list(self.listeners.get(event, [])):
            callback(*args)


class _Request:
    method = "POST"
    url = "https://example.test/register"
    headers = {"content-type": "application/json"}
    post_data = "{}"

    def sizes(self):
        return {
            "requestBodySize": 2,
            "requestHeadersSize": 10,
            "responseBodySize": 20,
            "responseHeadersSize": 8,
        }


class _Response:
    status = 201
    headers = {"content-type": "application/json"}
    from_service_worker = False


class _DetailedRequest(_Request):
    resource_type = "xhr"
    url = "https://example.test/api/register?token=do-not-log&step=1"

    def response(self):
        return _Response()


class _FailedRequest(_Request):
    resource_type = "document"
    url = "https://chatgpt.com/auth/login"
    headers = {"accept": "text/html"}
    post_data = None

    def __init__(self):
        self.sizes_called = 0
        self.response_called = 0

    def sizes(self):
        self.sizes_called += 1
        raise AssertionError("Request.sizes() must not be called for requestfailed")

    def response(self):
        self.response_called += 1
        raise AssertionError("Request.response() must not be called for requestfailed")


class _WebSocket(_Emitter):
    pass


def _performance_event(method, params):
    return {
        "message": json.dumps({"message": {"method": method, "params": params}}),
    }


class _SeleniumDriver:
    def __init__(self, entries):
        self.entries = list(entries)
        self.cdp_commands = []

    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        return {}

    def get_log(self, name):
        entries, self.entries = self.entries, []
        return entries

    def execute_script(self, script):
        return {}


class _CoverageSeleniumDriver(_SeleniumDriver):
    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))
        if command == "Profiler.takePreciseCoverage":
            return {
                "result": [
                    {
                        "scriptId": "10",
                        "url": "https://chatgpt.com/cdn/assets/used.js?token=secret",
                        "functions": [
                            {"functionName": "", "ranges": [{"startOffset": 0, "endOffset": 80, "count": 1}]},
                            {"functionName": "neverCalled", "ranges": [{"startOffset": 90, "endOffset": 140, "count": 0}]},
                        ],
                    },
                    {
                        "scriptId": "11",
                        "url": "https://chatgpt.com/cdn/assets/unused.js",
                        "functions": [
                            {"functionName": "unused", "ranges": [{"startOffset": 0, "endOffset": 60, "count": 0}]},
                        ],
                    },
                    {
                        "scriptId": "12",
                        "url": "data:text/javascript,inline-secret",
                        "functions": [
                            {"functionName": "inline", "ranges": [{"startOffset": 0, "endOffset": 10, "count": 0}]},
                        ],
                    },
                ],
            }


class _CoverageSession:
    def __init__(self):
        self.commands = []
        self.detached = False

    def send(self, command, params):
        self.commands.append((command, params))
        if command == "Profiler.takePreciseCoverage":
            return {
                "result": [
                    {
                        "scriptId": "pw-1",
                        "url": "https://cdn.example/used.js",
                        "functions": [
                            {"functionName": "boot", "ranges": [{"startOffset": 0, "endOffset": 20, "count": 2}]},
                        ],
                    },
                ],
            }
        return {}

    def detach(self):
        self.detached = True


class _CoveragePage(_Emitter):
    url = "https://chatgpt.com/auth/login?state=secret"


class _CoverageContext(_Emitter):
    def __init__(self):
        super().__init__()
        self.page = _CoveragePage()
        self.pages = [self.page]
        self.sessions = []

    def new_cdp_session(self, page):
        session = _CoverageSession()
        self.sessions.append(session)
        return session


class BrowserTrafficTests(unittest.TestCase):
    def test_playwright_counts_http_and_websocket_without_payload_capture(self):
        context = _Emitter()
        page = _Emitter()
        context.pages = [page]
        tracker = PlaywrightTrafficTracker(context)
        request = _Request()
        context.emit("request", request)
        context.emit("requestfinished", request)

        websocket = _WebSocket()
        page.emit("websocket", websocket)
        websocket.emit("framesent", "abc")
        websocket.emit("framereceived", b"1234")

        result = tracker.stop()
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["http_upload_bytes"], 12)
        self.assertEqual(result["http_download_bytes"], 28)
        self.assertEqual(result["websocket_upload_bytes"], 3)
        self.assertEqual(result["websocket_download_bytes"], 4)
        self.assertEqual(result["total_bytes"], 47)

    def test_selenium_parses_network_events_and_websocket_frames(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "1",
                    "request": {
                        "method": "POST",
                        "url": "https://example.test/register",
                        "headers": {"content-type": "application/json"},
                        "postData": "{}",
                    },
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "1",
                    "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "application/json"}},
                },
            ),
            _performance_event(
                "Network.loadingFinished",
                {"requestId": "1", "encodedDataLength": 20},
            ),
            _performance_event(
                "Network.webSocketCreated",
                {"requestId": "ws-1"},
            ),
            _performance_event(
                "Network.webSocketFrameSent",
                {"requestId": "ws-1", "response": {"payloadData": "xy"}},
            ),
            _performance_event(
                "Network.webSocketFrameReceived",
                {"requestId": "ws-1", "response": {"payloadData": "z"}},
            ),
        ]
        driver = _SeleniumDriver(entries)
        tracker = SeleniumTrafficTracker(driver)
        result = tracker.stop()

        self.assertEqual(driver.cdp_commands[0][0], "Network.enable")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["completed_request_count"], 1)
        self.assertEqual(result["websocket_count"], 1)
        self.assertEqual(result["websocket_upload_bytes"], 2)
        self.assertEqual(result["websocket_download_bytes"], 1)
        self.assertGreater(result["http_upload_bytes"], 0)
        self.assertGreaterEqual(result["http_download_bytes"], 20)

    def test_selenium_does_not_count_cdp_blocked_request_as_network_bytes(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "blocked-1",
                    "type": "Image",
                    "request": {
                        "method": "GET",
                        "url": "https://cdn.example/avatar.png",
                        "headers": {},
                    },
                },
            ),
            _performance_event(
                "Network.loadingFailed",
                {"requestId": "blocked-1", "blockedReason": "inspector"},
            ),
        ]
        driver = _SeleniumDriver(entries)
        with patch("core.browser_data_saver._cfg.BROWSER_DATA_SAVER_MODE", True), patch(
            "core.browser_data_saver._cfg.BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", ["image"]
        ):
            saver = BrowserDataSaver(label="test")
            saver.install_selenium(driver)
            tracker = SeleniumTrafficTracker(driver)
            tracker.attach_data_saver(saver)
            result = tracker.stop()

        self.assertEqual(result["http_upload_bytes"], 0)
        self.assertEqual(result["http_download_bytes"], 0)
        self.assertEqual(result["failed_request_count"], 0)
        self.assertEqual(result["data_saver_blocked_count"], 1)

    def test_playwright_logs_redacted_resource_detail_and_sizes(self):
        context = _Emitter()
        context.pages = []
        with patch("core.browser_traffic._browser_cfg.BROWSER_TRAFFIC_DETAIL_LOG", True), patch(
            "core.browser_traffic._browser_cfg.BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES", 20
        ):
            tracker = PlaywrightTrafficTracker(context, label="detail")
            request = _DetailedRequest()
            context.emit("request", request)
            context.emit("requestfinished", request)
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        detail_lines = [line for line in captured.output if "[资源明细]" in line]
        self.assertEqual(len(detail_lines), 1)
        line = detail_lines[0]
        self.assertIn("xhr POST", line)
        self.assertIn("status=201", line)
        self.assertIn("upload=12B", line)
        self.assertIn("download=28B", line)
        self.assertIn("body=20B", line)
        self.assertIn("headers=8B", line)
        self.assertIn("cache=unknown", line)
        self.assertIn("token=<redacted>", line)
        self.assertNotIn("do-not-log", line)
        self.assertEqual(result["detail_recorded_count"], 1)

    def test_playwright_requestfailed_does_not_reenter_sync_api(self):
        context = _Emitter()
        context.pages = []
        with patch("core.browser_traffic._browser_cfg.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = PlaywrightTrafficTracker(context, label="failed")
            request = _FailedRequest()
            context.emit("request", request)
            context.emit("requestfailed", request)
            result = tracker.stop()

        self.assertEqual(request.sizes_called, 0)
        self.assertEqual(request.response_called, 0)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["failed_request_count"], 1)
        self.assertEqual(result["completed_request_count"], 0)
        self.assertEqual(result["http_download_bytes"], 0)
        self.assertGreater(result["http_upload_bytes"], 0)
        self.assertEqual(result["detail_recorded_count"], 1)

    def test_selenium_logs_status_cache_failure_and_unfinished_details(self):
        entries = [
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "ok",
                    "type": "Script",
                    "request": {"method": "GET", "url": "https://example.test/app.js", "headers": {}},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "ok",
                    "response": {"status": 200, "statusText": "OK", "headers": {"content-type": "text/javascript"}, "mimeType": "text/javascript"},
                },
            ),
            _performance_event("Network.loadingFinished", {"requestId": "ok", "encodedDataLength": 40}),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "cached",
                    "type": "Image",
                    "request": {"method": "GET", "url": "https://cdn.example/cached.png", "headers": {}},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "cached",
                    "response": {"status": 200, "statusText": "OK", "headers": {}, "fromMemoryCache": True},
                },
            ),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "failed",
                    "type": "Fetch",
                    "request": {"method": "POST", "url": "https://example.test/api?secret=hidden", "headers": {}, "postData": "{}"},
                },
            ),
            _performance_event(
                "Network.responseReceived",
                {
                    "requestId": "failed",
                    "response": {"status": 500, "statusText": "Error", "headers": {}},
                },
            ),
            _performance_event(
                "Network.loadingFailed",
                {"requestId": "failed", "errorText": "net::ERR_FAILED"},
            ),
            _performance_event(
                "Network.requestWillBeSent",
                {
                    "requestId": "unfinished",
                    "type": "XHR",
                    "request": {"method": "GET", "url": "https://example.test/pending", "headers": {}},
                },
            ),
            _performance_event("Network.dataReceived", {"requestId": "unfinished", "encodedDataLength": 7}),
        ]
        driver = _SeleniumDriver(entries)
        with patch("core.browser_traffic._browser_cfg.BROWSER_TRAFFIC_DETAIL_LOG", True):
            tracker = SeleniumTrafficTracker(driver, label="detail")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        detail_lines = [line for line in captured.output if "[资源明细]" in line]
        self.assertEqual(len(detail_lines), 4)
        joined = "\n".join(detail_lines)
        self.assertIn("status=200", joined)
        self.assertIn("cache=hit", joined)
        self.assertIn("failed=1", joined)
        self.assertIn("unfinished=1", joined)
        self.assertIn("secret=<redacted>", joined)
        self.assertNotIn("hidden", joined)
        self.assertEqual(result["detail_recorded_count"], 4)

    def test_selenium_records_precise_js_coverage_and_unexecuted_candidates(self):
        driver = _CoverageSeleniumDriver([])
        with patch("core.browser_traffic._browser_cfg.BROWSER_JS_COVERAGE_LOG", True), patch(
            "core.browser_traffic._browser_cfg.BROWSER_JS_COVERAGE_MAX_ENTRIES", 20
        ):
            tracker = SeleniumTrafficTracker(driver, label="coverage")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        coverage = result["js_coverage"]
        self.assertTrue(coverage["enabled"])
        self.assertTrue(coverage["supported"])
        self.assertTrue(coverage["collected"])
        self.assertEqual(coverage["script_count"], 3)
        self.assertEqual(coverage["executed_script_count"], 1)
        self.assertEqual(coverage["candidate_script_count"], 1)
        self.assertEqual(coverage["function_count"], 4)
        self.assertEqual(coverage["executed_function_count"], 1)
        self.assertEqual(coverage["candidate_scripts"], ["https://chatgpt.com/cdn/assets/unused.js"])
        self.assertIn("Profiler.startPreciseCoverage", [command for command, _ in driver.cdp_commands])
        self.assertIn("Profiler.takePreciseCoverage", [command for command, _ in driver.cdp_commands])
        joined = "\n".join(captured.output)
        self.assertIn("[JS执行汇总]", joined)
        self.assertIn("[JS执行]", joined)
        self.assertIn("function=<anonymous>", joined)
        self.assertIn("[JS候选]", joined)
        self.assertIn("unused.js", joined)
        self.assertIn("token=<redacted>", joined)
        self.assertNotIn("secret", joined)

    def test_playwright_records_coverage_for_each_page_cdp_session(self):
        context = _CoverageContext()
        with patch("core.browser_traffic._browser_cfg.BROWSER_JS_COVERAGE_LOG", True):
            tracker = PlaywrightTrafficTracker(context, label="coverage-pw")
            with self.assertLogs("core.browser_traffic", level="INFO") as captured:
                result = tracker.stop()

        coverage = result["js_coverage"]
        self.assertTrue(coverage["supported"])
        self.assertEqual(coverage["target_count"], 1)
        self.assertEqual(coverage["started_target_count"], 1)
        self.assertEqual(coverage["script_count"], 1)
        self.assertEqual(coverage["executed_function_count"], 1)
        self.assertTrue(context.sessions[0].detached)
        self.assertIn("[JS执行]", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
