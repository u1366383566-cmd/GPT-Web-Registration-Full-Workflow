import socket
import unittest
from unittest.mock import Mock, patch

from core.proxy_chain import ProxyChainRelay, SocksConnectError, _read_exact
from core.roxybrowser_client import RoxyBrowserClient


class ProxyChainRetryTests(unittest.TestCase):
    def make_relay(self):
        return ProxyChainRelay("socks5h://user:password@proxy.example:1080")

    def test_socks_reply_has_structured_code_and_reason(self):
        relay = self.make_relay()
        remote = Mock()
        remote.recv.return_value = b"\x05\x00"
        with patch.object(relay, "_read_socks_reply", return_value=1):
            with self.assertRaises(SocksConnectError) as caught:
                relay._target_socks_connect(remote, "chatgpt.com", 443)
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("代理服务器一般性失败", str(caught.exception))

    def test_transient_handshake_retries_with_new_socket_and_recovers(self):
        relay = self.make_relay()
        remotes = [Mock(), Mock()]
        with patch.object(relay, "_connect_target_through_upstream", side_effect=remotes) as connect, patch.object(
            relay, "_target_socks_connect", side_effect=[SocksConnectError(1), None]
        ), patch.object(relay._stop, "wait", return_value=False) as wait:
            with self.assertLogs("core.proxy_chain", level="INFO") as logs:
                result = relay._open_target_tunnel("chatgpt.com", 443, "CONNECT", ("chatgpt.com", 443))
        self.assertIs(result, remotes[1])
        self.assertEqual(connect.call_count, 2)
        remotes[0].close.assert_called_once()
        remotes[1].close.assert_not_called()
        self.assertNotIn(remotes[0], relay._connections)
        self.assertIn(remotes[1], relay._connections)
        wait.assert_called_once_with(0.25)
        item = relay.traffic_snapshot()["destinations"][0]
        self.assertEqual(item["connect_retry_count"], 1)
        self.assertEqual(item["recovered_connection_count"], 1)
        self.assertEqual(item["failed_connection_count"], 0)
        self.assertTrue(any("代理重连已恢复 destination=chatgpt.com:443" in line for line in logs.output))
        self.assertEqual(relay.last_error, "")

    def test_permanent_socks_errors_are_not_retried(self):
        for error in (SocksConnectError(2), SocksConnectError(5), SocksConnectError(7), SocksConnectError(8), PermissionError("认证失败")):
            with self.subTest(error=error):
                relay = self.make_relay()
                remote = Mock()
                with patch.object(relay, "_connect_target_through_upstream", return_value=remote) as connect, patch.object(
                    relay, "_target_socks_connect", side_effect=error
                ), patch.object(relay._stop, "wait") as wait:
                    with self.assertRaises(type(error)):
                        relay._open_target_tunnel("auth.openai.com", 443, "CONNECT", ("auth.openai.com", 443))
                connect.assert_called_once()
                wait.assert_not_called()
                remote.close.assert_called_once()

    def test_exhausted_retries_record_one_final_failure_and_destination(self):
        relay = self.make_relay()
        remotes = [Mock(), Mock(), Mock()]
        browser, client = socket.socketpair()
        try:
            browser.sendall(b"CONNECT auth.openai.com:443 HTTP/1.1\r\n\r\n")
            with patch.object(relay, "_connect_target_through_upstream", side_effect=remotes) as connect, patch.object(
                relay, "_target_socks_connect", side_effect=SocksConnectError(1)
            ), patch.object(relay._stop, "wait", return_value=False):
                with self.assertLogs("core.proxy_chain", level="WARNING") as logs:
                    relay._handle_client(client)
            self.assertIn(b"502 Bad Gateway", browser.recv(4096))
            self.assertEqual(connect.call_count, 3)
            for remote in remotes:
                remote.close.assert_called_once()
            item = relay.traffic_snapshot()["destinations"][0]
            self.assertEqual(item["connect_retry_count"], 2)
            self.assertEqual(item["failed_connection_count"], 1)
            self.assertEqual(item["recovered_connection_count"], 0)
            self.assertEqual(item["total_bytes"], 0)
            self.assertEqual(len(logs.output), 1)
            self.assertIn("destination=auth.openai.com:443", logs.output[0])
        finally:
            browser.close()

    def test_retry_does_not_replay_early_browser_data(self):
        relay = self.make_relay()
        browser, client = socket.socketpair()
        remote1, server1 = socket.socketpair()
        remote2, server2 = socket.socketpair()
        early = b"early-tls-bytes"
        try:
            browser.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n" + early)
            with patch.object(relay, "_connect_target_through_upstream", side_effect=[remote1, remote2]), patch.object(
                relay, "_target_socks_connect", side_effect=[SocksConnectError(1), None]
            ), patch.object(relay._stop, "wait", return_value=False), patch.object(relay, "_relay"):
                relay._handle_client(client)
            self.assertIn(b"200 Connection Established", browser.recv(4096))
            self.assertEqual(server1.recv(100), b"")
            self.assertEqual(_read_exact(server2, len(early)), early)
            self.assertEqual(server2.recv(100), b"")
            self.assertEqual(relay.traffic_snapshot()["upload_bytes"], len(early))
        finally:
            browser.close()
            server1.close()
            server2.close()

    def test_stop_interrupts_retry_and_does_not_log_connection_failure(self):
        relay = self.make_relay()
        remote = Mock()
        browser, client = socket.socketpair()
        def stop(_delay):
            relay._stop.set()
            return True
        try:
            browser.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n")
            with patch.object(relay, "_connect_target_through_upstream", return_value=remote) as connect, patch.object(
                relay, "_target_socks_connect", side_effect=SocksConnectError(1)
            ), patch.object(relay._stop, "wait", side_effect=stop), patch("core.proxy_chain.logger.warning") as warning:
                relay._handle_client(client)
            connect.assert_called_once()
            warning.assert_not_called()
            self.assertEqual(relay.last_error, "")
            self.assertEqual(relay.traffic_snapshot()["destinations"][0]["failed_connection_count"], 0)
        finally:
            browser.close()

    def test_established_tunnel_failure_is_not_retried_or_sent_http_error(self):
        relay = self.make_relay()
        browser, client = socket.socketpair()
        remote, server = socket.socketpair()
        try:
            browser.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n")
            with patch.object(relay, "_connect_target_through_upstream", return_value=remote) as connect, patch.object(
                relay, "_target_socks_connect"
            ), patch.object(relay, "_relay", side_effect=ConnectionResetError("peer reset")), patch.object(relay._stop, "wait") as wait:
                with self.assertLogs("core.proxy_chain", level="WARNING"):
                    relay._handle_client(client)
            data = b""
            while True:
                chunk = browser.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.assertIn(b"200 Connection Established", data)
            self.assertNotIn(b"502 Bad Gateway", data)
            connect.assert_called_once()
            wait.assert_not_called()
        finally:
            browser.close()
            server.close()

    def test_roxy_cleanup_summarizes_history_without_repeating_warning(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        relay = Mock()
        relay.last_error = "historical code 1"
        relay.traffic_snapshot.return_value = {"destinations": [{"failed_connection_count": 1,
            "connect_retry_count": 2, "recovered_connection_count": 1}]}
        client._proxy_pool_relay = relay
        with self.assertLogs("core.roxybrowser_client", level="INFO") as logs:
            client.close_proxy_pool_relay()
        relay.close.assert_called_once()
        self.assertIsNone(client._proxy_pool_relay)
        self.assertTrue(any("握手重试=2，重试恢复=1，失败连接=1" in line for line in logs.output))
        self.assertFalse(any("WARNING" in line or "最后一次错误" in line for line in logs.output))
