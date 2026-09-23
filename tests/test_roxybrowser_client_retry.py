# -*- coding: utf-8 -*-
import copy
import unittest
from unittest.mock import patch

from core.roxybrowser_client import RoxyBrowserClient


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **copy.deepcopy(kwargs)})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RoxyBrowserClientRetryTests(unittest.TestCase):
    def _client(self, responses):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        client.http = _FakeSession(responses)
        return client

    @patch("core.roxybrowser_client.time.sleep")
    @patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRY_DELAY", 3)
    @patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRIES", 3)
    def test_create_timeout_response_retries_with_same_payload(self, sleep_mock):
        client = self._client([
            _FakeResponse({"code": 1, "msg": "timeout of 15000ms exceeded"}),
            _FakeResponse({"code": 0, "data": {"dirId": "profile-1"}}),
        ])
        body = {
            "workspaceId": 90143,
            "projectId": 97471,
            "name": "rb-fixed-name",
            "proxyInfo": {"protocol": "SOCKS5", "host": "127.0.0.1", "port": "7897"},
        }

        result = client.request("POST", "/browser/create", json_body=body)

        self.assertEqual(result["data"]["dirId"], "profile-1")
        self.assertEqual(len(client.http.calls), 2)
        self.assertEqual(client.http.calls[0]["json"], body)
        self.assertEqual(client.http.calls[1]["json"], body)
        self.assertEqual(client.http.calls[0]["json"]["name"], "rb-fixed-name")
        self.assertEqual(client.http.calls[1]["json"]["name"], "rb-fixed-name")
        sleep_mock.assert_called_once_with(3.0)

    @patch("core.roxybrowser_client.time.sleep")
    @patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRY_DELAY", 3)
    @patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRIES", 3)
    def test_create_stops_after_configured_attempts(self, sleep_mock):
        client = self._client([
            TimeoutError("connection timed out"),
            TimeoutError("connection timed out"),
            TimeoutError("connection timed out"),
        ])

        with self.assertRaisesRegex(TimeoutError, "connection timed out"):
            client.request("POST", "/browser/create", json_body={"name": "rb-fixed-name"})

        self.assertEqual(len(client.http.calls), 3)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [3.0, 6.0])

    @patch("core.roxybrowser_client.time.sleep")
    @patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRIES", 3)
    def test_create_non_transient_business_error_is_not_retried(self, sleep_mock):
        client = self._client([
            _FakeResponse({"code": 1, "msg": "workspaceId is invalid"}),
        ])

        with self.assertRaisesRegex(RuntimeError, "workspaceId is invalid"):
            client.request("POST", "/browser/create", json_body={"name": "rb-fixed-name"})

        self.assertEqual(len(client.http.calls), 1)
        sleep_mock.assert_not_called()

    @patch("core.roxybrowser_client.time.sleep")
    @patch("core.roxybrowser_client._cfg.ROXY_API_RETRY_DELAY", 2)
    @patch("core.roxybrowser_client._cfg.ROXY_API_RETRIES", 2)
    @patch("core.roxybrowser_client._cfg.ROXY_CREATE_RETRIES", 5)
    def test_non_create_uses_normal_api_retry_count(self, sleep_mock):
        client = self._client([
            _FakeResponse({"code": 1, "msg": "temporarily unavailable"}),
            _FakeResponse({"code": 0, "data": {"ok": True}}),
        ])

        result = client.request("POST", "/browser/open", json_body={"dirId": "profile-1"})

        self.assertTrue(result["data"]["ok"])
        self.assertEqual(len(client.http.calls), 2)
        sleep_mock.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
