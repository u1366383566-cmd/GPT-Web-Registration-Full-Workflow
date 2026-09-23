# -*- coding: utf-8 -*-
import base64
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.roxy_asset_cache import RoxyLocalAssetCache


class RoxyLocalAssetCacheTests(unittest.TestCase):
    def test_data_saver_mode_automatically_enables_cache(self):
        with patch.multiple(
            "core.roxy_asset_cache._cfg",
            ROXY_LOCAL_ASSET_CACHE_ENABLED=False,
        ), patch("core.roxy_asset_cache._browser_cfg.BROWSER_DATA_SAVER_MODE", True):
            cache = RoxyLocalAssetCache("")
            self.assertTrue(cache.enabled)

    def test_only_allows_static_assets(self):
        self.assertTrue(RoxyLocalAssetCache.is_cacheable(
            "https://chatgpt.com/_next/static/chunks/app-abc.js", "Script"
        ))
        self.assertTrue(RoxyLocalAssetCache.is_cacheable(
            "https://cdn.oaistatic.com/assets/font.woff2", "Font"
        ))
        self.assertFalse(RoxyLocalAssetCache.is_cacheable(
            "https://chatgpt.com/auth/login", "Document"
        ))
        self.assertFalse(RoxyLocalAssetCache.is_cacheable(
            "https://chatgpt.com/backend-api/me", "Fetch"
        ))
        self.assertFalse(RoxyLocalAssetCache.is_cacheable(
            "https://sentinel.openai.com/sentinel/version/sdk.js", "Script"
        ))

    def test_record_and_replay_exact_url(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            "core.roxy_asset_cache._cfg",
            ROXY_LOCAL_ASSET_CACHE_ENABLED=True,
            ROXY_LOCAL_ASSET_CACHE_MODE="auto",
            ROXY_LOCAL_ASSET_CACHE_DIR=directory,
            ROXY_LOCAL_ASSET_CACHE_MAX_AGE=86400,
            ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES=1024 * 1024,
        ):
            cache = RoxyLocalAssetCache("127.0.0.1:9222")
            url = "https://chatgpt.com/_next/static/chunks/app-abc.js"
            body = b"console.log('cached')"
            cache._store({
                "url": url,
                "status": 200,
                "resource_type": "Script",
                "mime_type": "text/javascript",
                "headers": {"content-type": "text/javascript", "content-encoding": "br"},
            }, base64.b64encode(body).decode("ascii"))

            loaded = cache._load(url)
            self.assertIsNotNone(loaded)
            self.assertEqual(base64.b64decode(loaded["body_b64"]), body)
            self.assertEqual(cache.recorded, 1)
            self.assertTrue(Path(cache._entry_path(url)).exists())

            # 已存在且未过期的 URL 不应再次覆盖，也不能再次计入“本轮新增”。
            cache._store({
                "url": url,
                "status": 200,
                "resource_type": "Script",
                "mime_type": "text/javascript",
                "headers": {"content-type": "text/javascript"},
            }, base64.b64encode(body).decode("ascii"))
            self.assertEqual(cache.recorded, 1)

            commands = []
            cache._send = lambda method, params=None: commands.append((method, params)) or len(commands)
            cache._handle_paused({
                "requestId": "fetch-1",
                "networkId": "network-1",
                "resourceType": "Script",
                "request": {"method": "GET", "url": url},
            })
            self.assertEqual(commands[0][0], "Fetch.fulfillRequest")
            response_headers = commands[0][1]["responseHeaders"]
            self.assertNotIn("content-encoding", {x["name"].lower() for x in response_headers})
            self.assertEqual(cache.cache_hits, 1)
            self.assertEqual(cache.bytes_saved, len(body))
            self.assertTrue(cache.was_fulfilled_network_request("network-1"))
            cache._handle_response({
                "requestId": "network-1",
                "type": "Script",
                "response": {"url": url, "status": 200, "mimeType": "text/javascript"},
            })
            cache._handle_finished({"requestId": "network-1"})
            self.assertEqual(cache.recorded, 1)

    def test_cache_miss_continues_network_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            "core.roxy_asset_cache._cfg",
            ROXY_LOCAL_ASSET_CACHE_ENABLED=True,
            ROXY_LOCAL_ASSET_CACHE_MODE="auto",
            ROXY_LOCAL_ASSET_CACHE_DIR=directory,
            ROXY_LOCAL_ASSET_CACHE_MAX_AGE=86400,
            ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES=1024 * 1024,
        ):
            cache = RoxyLocalAssetCache("127.0.0.1:9222")
            commands = []
            cache._send = lambda method, params=None: commands.append((method, params)) or len(commands)
            cache._handle_paused({
                "requestId": "fetch-2",
                "resourceType": "Stylesheet",
                "request": {
                    "method": "GET",
                    "url": "https://chatgpt.com/_next/static/css/missing.css",
                },
            })
            self.assertEqual(commands[0][0], "Fetch.continueRequest")
            self.assertEqual(cache.cache_misses, 1)

    def test_concurrent_instances_only_count_same_url_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            "core.roxy_asset_cache._cfg",
            ROXY_LOCAL_ASSET_CACHE_ENABLED=True,
            ROXY_LOCAL_ASSET_CACHE_MODE="auto",
            ROXY_LOCAL_ASSET_CACHE_DIR=directory,
            ROXY_LOCAL_ASSET_CACHE_MAX_AGE=86400,
            ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES=1024 * 1024,
        ):
            caches = [RoxyLocalAssetCache("127.0.0.1:9222") for _ in range(2)]
            url = "https://auth-cdn.oaistatic.com/assets/shared.js"
            item = {
                "url": url,
                "status": 200,
                "resource_type": "Script",
                "mime_type": "application/javascript",
                "headers": {"content-type": "application/javascript"},
            }
            body = base64.b64encode(b"export default 1").decode("ascii")
            barrier = threading.Barrier(2)

            def store(cache):
                barrier.wait()
                cache._store(item, body)

            threads = [threading.Thread(target=store, args=(cache,)) for cache in caches]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(sum(cache.recorded for cache in caches), 1)

    def test_fulfilled_url_recovers_network_id_when_fetch_event_omits_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            "core.roxy_asset_cache._cfg",
            ROXY_LOCAL_ASSET_CACHE_ENABLED=True,
            ROXY_LOCAL_ASSET_CACHE_MODE="auto",
            ROXY_LOCAL_ASSET_CACHE_DIR=directory,
            ROXY_LOCAL_ASSET_CACHE_MAX_AGE=86400,
            ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES=1024 * 1024,
        ):
            cache = RoxyLocalAssetCache("127.0.0.1:9222")
            url = "https://auth-cdn.oaistatic.com/assets/app-core-test.js"
            cache._store({
                "url": url,
                "status": 200,
                "resource_type": "Script",
                "mime_type": "application/javascript",
                "headers": {"content-type": "application/javascript"},
            }, base64.b64encode(b"export default 1").decode("ascii"))
            cache._send = lambda method, params=None: 1

            # Chromium 某些版本在 Request stage 不返回 networkId。
            cache._handle_paused({
                "requestId": "fetch-only-id",
                "resourceType": "Script",
                "request": {"method": "GET", "url": url},
            })
            self.assertFalse(cache.was_fulfilled_network_request("network-later"))
            cache._handle_response({
                "requestId": "network-later",
                "type": "Script",
                "response": {"url": url, "status": 200, "mimeType": "application/javascript"},
            })
            self.assertTrue(cache.was_fulfilled_network_request("network-later"))
            self.assertNotIn("network-later", cache._responses)

    def test_fulfilled_url_is_fallback_when_fetch_network_id_differs(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            "core.roxy_asset_cache._cfg",
            ROXY_LOCAL_ASSET_CACHE_ENABLED=True,
            ROXY_LOCAL_ASSET_CACHE_MODE="auto",
            ROXY_LOCAL_ASSET_CACHE_DIR=directory,
            ROXY_LOCAL_ASSET_CACHE_MAX_AGE=86400,
            ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES=1024 * 1024,
        ):
            cache = RoxyLocalAssetCache("127.0.0.1:9222")
            url = "https://auth-cdn.oaistatic.com/assets/app-core-test.js"
            cache._store({
                "url": url,
                "status": 200,
                "resource_type": "Script",
                "mime_type": "application/javascript",
                "headers": {"content-type": "application/javascript"},
            }, base64.b64encode(b"export default 1").decode("ascii"))
            cache._send = lambda method, params=None: 1

            cache._handle_paused({
                "requestId": "fetch-id",
                "networkId": "network-id-from-fetch",
                "resourceType": "Script",
                "request": {"method": "GET", "url": url},
            })
            cache._handle_response({
                "requestId": "different-network-id",
                "type": "Script",
                "response": {"url": url, "status": 200, "mimeType": "application/javascript"},
            })

            self.assertTrue(cache.was_fulfilled_network_request("different-network-id"))
            self.assertNotIn("different-network-id", cache._responses)


if __name__ == "__main__":
    unittest.main()
