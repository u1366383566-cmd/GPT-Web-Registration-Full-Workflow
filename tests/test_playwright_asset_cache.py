import json
import tempfile
import unittest
from unittest.mock import patch

from core.playwright_asset_cache import PlaywrightLocalAssetCache


class _Request:
    def __init__(self, url, resource_type="script", method="GET"):
        self.url = url
        self.resource_type = resource_type
        self.method = method


class _Response:
    def __init__(self, request, body=b"console.log(1)", status=200, headers=None):
        self.request = request
        self._body = body
        self.status = status
        self.headers = headers or {
            "content-type": "application/javascript",
            "content-encoding": "br",
            "etag": '"asset"',
        }

    def body(self):
        return self._body


class _Route:
    def __init__(self, request):
        self.request = request
        self.fulfilled = None
        self.continued = False

    def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    def continue_(self):
        self.continued = True


class _Context:
    def __init__(self):
        self.listeners = {}

    def on(self, event, handler):
        self.listeners[event] = handler

    def remove_listener(self, event, handler):
        if self.listeners.get(event) is handler:
            self.listeners.pop(event, None)


class PlaywrightAssetCacheTests(unittest.TestCase):
    def _cache(self, root):
        context = _Context()
        with patch("core.playwright_asset_cache._cfg.CLOAK_LOCAL_ASSET_CACHE_DIR", root), \
             patch("core.playwright_asset_cache._browser_cfg.BROWSER_DATA_SAVER_MODE", True):
            return PlaywrightLocalAssetCache(context).start()

    def test_cacheability_is_limited_to_versioned_static_assets(self):
        self.assertTrue(
            PlaywrightLocalAssetCache.is_cacheable(
                "https://chatgpt.com/cdn/assets/app-123.js",
                "script",
            )
        )
        self.assertTrue(
            PlaywrightLocalAssetCache.is_cacheable(
                "https://auth-cdn.oaistatic.com/assets/app.css",
                "stylesheet",
            )
        )
        self.assertFalse(
            PlaywrightLocalAssetCache.is_cacheable(
                "https://chatgpt.com/backend-api/sentinel/sdk.js",
                "script",
            )
        )
        self.assertFalse(
            PlaywrightLocalAssetCache.is_cacheable(
                "https://chatgpt.com/cdn/assets/app.js",
                "fetch",
            )
        )

    def test_response_is_recorded_and_replayed_without_encoding_header(self):
        with tempfile.TemporaryDirectory() as root:
            cache = self._cache(root)
            request = _Request("https://chatgpt.com/cdn/assets/app-123.js")
            cache.context.listeners["response"](_Response(request))

            route = _Route(request)
            self.assertTrue(cache.handle_route(route))
            self.assertEqual(route.fulfilled["status"], 200)
            self.assertEqual(route.fulfilled["body"], b"console.log(1)")
            header_names = {name.lower() for name in route.fulfilled["headers"]}
            self.assertIn("content-type", header_names)
            self.assertNotIn("content-encoding", header_names)
            snapshot = cache.stop()

        self.assertEqual(snapshot["recorded"], 1)
        self.assertEqual(snapshot["hits"], 1)
        self.assertEqual(snapshot["bytes_saved"], len(b"console.log(1)"))

    def test_auth_api_and_non_200_responses_are_not_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            cache = self._cache(root)
            handler = cache.context.listeners["response"]
            handler(_Response(_Request("https://chatgpt.com/api/assets/app.js"), body=b"secret"))
            handler(_Response(_Request("https://chatgpt.com/cdn/assets/missing.js"), status=404))
            snapshot = cache.stop()
        self.assertEqual(snapshot["recorded"], 0)

    def test_corrupt_cache_entry_falls_through_to_network(self):
        with tempfile.TemporaryDirectory() as root:
            cache = self._cache(root)
            request = _Request("https://chatgpt.com/cdn/assets/app-123.js")
            path = cache._entry_path(request.url)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"url": request.url, "body_b64": ""}), encoding="utf-8")
            route = _Route(request)
            self.assertFalse(cache.handle_route(route))
            self.assertIsNone(route.fulfilled)
            self.assertEqual(cache.snapshot()["misses"], 1)
            cache.stop()


if __name__ == "__main__":
    unittest.main()
