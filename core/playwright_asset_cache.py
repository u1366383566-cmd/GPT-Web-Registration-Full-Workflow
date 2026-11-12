# -*- coding: utf-8 -*-
"""Cloak/Playwright 跨任务本地静态资源缓存。

只缓存公开、版本化的 JS/CSS/font。认证、API、OTP、document、XHR、
fetch、WebSocket 和验证码资源始终直接联网，避免缓存改变账号状态。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import browser as _browser_cfg
from config import cloakbrowser as _cfg

logger = logging.getLogger(__name__)

_CACHE_STORE_LOCK = threading.Lock()
_ALLOWED_RESOURCE_TYPES = {"script", "stylesheet", "font"}
_STATIC_EXTENSIONS = {".js", ".mjs", ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot"}
_ALLOWED_HOST_SUFFIXES = ("chatgpt.com", "oaistatic.com", "cdn.openai.com")
_DENIED_PATH_PARTS = (
    "/api/", "/backend-api/", "/backend-anon/", "/ces/", "/cdn-cgi/",
    "/sentinel/", "/authorize", "/oauth", "/login", "/logout", "/auth/",
    "/email-verification", "/about-you", "/bazaar/", "/obi/",
)
_REPLAY_RESPONSE_HEADERS = {
    "content-type", "cache-control", "etag", "last-modified", "vary",
    "access-control-allow-origin", "access-control-expose-headers",
    "cross-origin-resource-policy", "timing-allow-origin",
}


def _mode() -> str:
    value = str(getattr(_cfg, "CLOAK_LOCAL_ASSET_CACHE_MODE", "auto") or "auto").strip().lower()
    return value if value in {"record", "replay", "auto"} else "auto"


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()


class PlaywrightLocalAssetCache:
    def __init__(self, context: Any, *, label: str = "Cloak"):
        self.context = context
        self.label = str(label or "Cloak")
        self.enabled = bool(
            getattr(_cfg, "CLOAK_LOCAL_ASSET_CACHE_ENABLED", False)
            or getattr(_browser_cfg, "BROWSER_DATA_SAVER_MODE", False)
        )
        self.mode = _mode()
        raw_dir = str(
            getattr(_cfg, "CLOAK_LOCAL_ASSET_CACHE_DIR", "./cache/cloak-assets")
            or "./cache/cloak-assets"
        )
        self.cache_dir = Path(raw_dir).expanduser().resolve()
        self.max_age = max(0, int(getattr(_cfg, "CLOAK_LOCAL_ASSET_CACHE_MAX_AGE", 86400) or 0))
        self.max_item_bytes = max(
            1024,
            int(getattr(_cfg, "CLOAK_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES", 25 * 1024 * 1024) or 0),
        )
        self.recorded = 0
        self.recorded_bytes = 0
        self._recorded_items: dict[str, int] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.bytes_saved = 0
        self._errors: queue.Queue[str] = queue.Queue(maxsize=20)
        self._fulfilled_requests: Counter[int] = Counter()
        self._lock = threading.RLock()
        self._response_handler: Any | None = None
        self._started = False
        self._stopped = False
        self._final_snapshot: dict[str, Any] | None = None

    @staticmethod
    def is_cacheable(url: str, resource_type: str = "") -> bool:
        try:
            parsed = urlsplit(str(url or ""))
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if not any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
            return False
        path = parsed.path.lower()
        if any(part in path for part in _DENIED_PATH_PARTS):
            return False
        rtype = str(resource_type or "").lower()
        if rtype not in _ALLOWED_RESOURCE_TYPES:
            return False
        suffix = Path(path).suffix.lower()
        return suffix in _STATIC_EXTENSIONS or "/_next/static/" in path or "/assets/" in path

    def _entry_path(self, url: str) -> Path:
        key = _cache_key(url)
        return self.cache_dir / key[:2] / f"{key}.json"

    def _load(self, url: str) -> dict[str, Any] | None:
        path = self._entry_path(url)
        try:
            stat = path.stat()
            if self.max_age and time.time() - stat.st_mtime > self.max_age:
                return None
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("url") != url or not item.get("body_b64"):
                return None
            item["body_bytes"] = len(base64.b64decode(item["body_b64"]))
            return item
        except Exception:
            return None

    def _store(
        self,
        *,
        url: str,
        resource_type: str,
        status: int,
        mime_type: str,
        headers: dict[str, Any],
        body: bytes,
    ) -> None:
        try:
            if not body or len(body) > self.max_item_bytes:
                return
            if status != 200 or not self.is_cacheable(url, resource_type):
                return
            payload = {
                "version": 1,
                "url": url,
                "status": status,
                "resource_type": resource_type,
                "mime_type": mime_type,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode("ascii"),
                "body_bytes": len(body),
                "stored_at": int(time.time()),
            }
            with _CACHE_STORE_LOCK:
                if self._load(url) is not None:
                    return
                path = self._entry_path(url)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
                self.recorded += 1
                self.recorded_bytes += len(body)
                self._recorded_items[url] = len(body)
        except Exception as exc:
            self._remember_error(f"store: {type(exc).__name__}: {exc}")

    def _remember_error(self, message: str) -> None:
        try:
            self._errors.put_nowait(str(message)[:240])
        except queue.Full:
            pass

    def _on_response(self, response: Any) -> None:
        if not self.enabled or self.mode not in {"record", "auto"}:
            return
        try:
            request = response.request
            if str(getattr(request, "method", "GET") or "GET").upper() != "GET":
                return
            url = str(getattr(request, "url", "") or "")
            resource_type = str(getattr(request, "resource_type", "") or "").lower()
            if not self.is_cacheable(url, resource_type):
                return
            status = int(getattr(response, "status", 0) or 0)
            if status != 200:
                return
            headers = {
                str(name).lower(): str(value)
                for name, value in (getattr(response, "headers", {}) or {}).items()
            }
            mime_type = headers.get("content-type", "")
            content_length = str(headers.get("content-length") or "").strip()
            if content_length.isdigit() and int(content_length) > self.max_item_bytes:
                return
            self._store(
                url=url,
                resource_type=resource_type,
                status=status,
                mime_type=mime_type,
                headers=headers,
                body=response.body(),
            )
        except Exception as exc:
            self._remember_error(f"response: {type(exc).__name__}: {exc}")

    def handle_route(self, route: Any) -> bool:
        """缓存命中时本地响应；未命中或异常返回 False，由调用方继续代理请求。"""
        if not self.enabled or self.mode not in {"replay", "auto"} or self._stopped:
            return False
        try:
            request = route.request
            if str(getattr(request, "method", "GET") or "GET").upper() != "GET":
                return False
            url = str(getattr(request, "url", "") or "")
            resource_type = str(getattr(request, "resource_type", "") or "").lower()
            if not self.is_cacheable(url, resource_type):
                return False
            cached = self._load(url)
            if cached is None:
                self.cache_misses += 1
                return False
            headers = {
                str(name): str(value)
                for name, value in (cached.get("headers") or {}).items()
                if str(name).lower() in _REPLAY_RESPONSE_HEADERS
                and isinstance(value, (str, int, float))
            }
            if not any(name.lower() == "content-type" for name in headers) and cached.get("mime_type"):
                headers["Content-Type"] = str(cached["mime_type"])
            route.fulfill(
                status=int(cached.get("status") or 200),
                headers=headers,
                body=base64.b64decode(cached["body_b64"]),
            )
            with self._lock:
                self._fulfilled_requests[id(request)] += 1
                self.cache_hits += 1
                self.bytes_saved += int(cached.get("body_bytes") or 0)
            return True
        except Exception as exc:
            self._remember_error(f"route: {type(exc).__name__}: {exc}")
            return False

    def was_playwright_fulfilled(self, request: Any) -> bool:
        with self._lock:
            count = self._fulfilled_requests.get(id(request), 0)
            if count > 0:
                self._fulfilled_requests[id(request)] = count - 1
            return count > 0

    def start(self) -> "PlaywrightLocalAssetCache":
        if not self.enabled or self._started:
            return self
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._response_handler = self._on_response
        self.context.on("response", self._response_handler)
        self._started = True
        logger.info(
            "[%s][本地缓存] 已启用 mode=%s dir=%s",
            self.label,
            self.mode,
            self.cache_dir,
        )
        return self

    def snapshot(self) -> dict[str, Any]:
        errors: list[str] = []
        while True:
            try:
                errors.append(self._errors.get_nowait())
            except queue.Empty:
                break
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "cache_dir": str(self.cache_dir),
            "recorded": self.recorded,
            "recorded_bytes": self.recorded_bytes,
            "recorded_top": [
                {"url": url, "bytes": size}
                for url, size in sorted(
                    self._recorded_items.items(),
                    key=lambda pair: pair[1],
                    reverse=True,
                )[:30]
            ],
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "bytes_saved": self.bytes_saved,
            "errors": errors,
        }

    def stop(self) -> dict[str, Any]:
        if self._final_snapshot is not None:
            return dict(self._final_snapshot)
        self._stopped = True
        if self._response_handler is not None:
            try:
                self.context.remove_listener("response", self._response_handler)
            except Exception:
                pass
        self._response_handler = None
        result = self.snapshot()
        self._final_snapshot = dict(result)
        if self.enabled:
            logger.info(
                "[%s][本地缓存] 结束 recorded=%s hits=%s misses=%s saved=%sB errors=%s",
                self.label,
                result["recorded"],
                result["hits"],
                result["misses"],
                result["bytes_saved"],
                len(result["errors"]),
            )
            if result.get("recorded_top"):
                logger.info(
                    "[%s][本地缓存] 本轮新增资源 Top：%s",
                    self.label,
                    [
                        f"{item['bytes']}B {item['url']}"
                        for item in result["recorded_top"][:10]
                    ],
                )
        return result


__all__ = ["PlaywrightLocalAssetCache"]
