# -*- coding: utf-8 -*-
"""Roxy Chromium 静态资源本地缓存。

通过页面 target 的 CDP WebSocket 同时使用：

* ``Network``：记录成功加载的静态资源正文；
* ``Fetch``：后续请求命中本地文件时用 ``fulfillRequest`` 直接返回。

只处理精确 URL 的 GET 静态资源。document、API、认证、Cloudflare、Sentinel、
CES 和 OBI 请求始终走真实网络。
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

import requests

from config import browser as _browser_cfg
from config import roxybrowser as _cfg

logger = logging.getLogger(__name__)

# 多个注册窗口共享同一缓存目录。原子替换只能保证文件不损坏，不能阻止两个
# 窗口同时把同一 URL 统计为新增，因此检查与写入需要进程级串行化。
_CACHE_STORE_LOCK = threading.Lock()

_ALLOWED_RESOURCE_TYPES = {"Script", "Stylesheet", "Font", "Image"}
_STATIC_EXTENSIONS = {
    ".js", ".mjs", ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico",
}
_ALLOWED_HOST_SUFFIXES = (
    "chatgpt.com",
    "auth.openai.com",
    "oaistatic.com",
    "cdn.openai.com",
)
_DENIED_PATH_PARTS = (
    "/api/", "/backend-api/", "/backend-anon/", "/ces/", "/cdn-cgi/",
    "/sentinel/", "/authorize", "/oauth", "/login", "/logout",
    "/email-verification", "/about-you", "/bazaar/", "/obi/",
)
_REPLAY_RESPONSE_HEADERS = {
    "content-type", "cache-control", "etag", "last-modified", "vary",
    "access-control-allow-origin", "access-control-expose-headers",
    "cross-origin-resource-policy", "timing-allow-origin",
}


def _mode() -> str:
    value = str(getattr(_cfg, "ROXY_LOCAL_ASSET_CACHE_MODE", "auto") or "auto").strip().lower()
    return value if value in {"record", "replay", "auto"} else "auto"


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()


def _debugger_http_base(address: str) -> str:
    text = str(address or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    return text


def _replace_ws_host(ws_url: str, debugger_address: str) -> str:
    """Roxy 有时在 json/list 中返回 localhost；按实际 debugger host 修正。"""
    try:
        ws = urlsplit(ws_url)
        debug = urlsplit(_debugger_http_base(debugger_address))
        if not debug.hostname or not ws.hostname:
            return ws_url
        port = ws.port or debug.port
        host = debug.hostname
        netloc = f"{host}:{port}" if port else host
        return ws._replace(netloc=netloc).geturl()
    except Exception:
        return ws_url


class RoxyLocalAssetCache:
    def __init__(self, debugger_address: str | None, *, label: str = "Roxy"):
        self.debugger_address = str(debugger_address or "").strip()
        self.label = label
        # 省流量模式开启时自动启用跨 Profile 静态资源缓存；显式缓存开关仍
        # 可在未开启省流量模式时单独启用缓存。
        self.enabled = bool(
            getattr(_cfg, "ROXY_LOCAL_ASSET_CACHE_ENABLED", False)
            or getattr(_browser_cfg, "BROWSER_DATA_SAVER_MODE", False)
        )
        self.mode = _mode()
        raw_dir = str(getattr(_cfg, "ROXY_LOCAL_ASSET_CACHE_DIR", "./cache/roxy-assets") or "./cache/roxy-assets")
        self.cache_dir = Path(raw_dir).expanduser().resolve()
        self.max_age = max(0, int(getattr(_cfg, "ROXY_LOCAL_ASSET_CACHE_MAX_AGE", 86400) or 0))
        self.max_item_bytes = max(1024, int(getattr(_cfg, "ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES", 25 * 1024 * 1024) or 0))
        self._thread: threading.Thread | None = None
        self._ws: Any | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending_bodies: dict[int, dict[str, Any]] = {}
        self._responses: dict[str, dict[str, Any]] = {}
        self._fulfilled_network_ids: set[str] = set()
        self._fulfilled_urls: Counter[str] = Counter()
        self._fulfilled_lock = threading.Lock()
        self._errors: queue.Queue[str] = queue.Queue(maxsize=20)
        self.recorded = 0
        self.recorded_bytes = 0
        self._recorded_items: dict[str, int] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.bytes_saved = 0
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
        rtype = str(resource_type or "")
        if rtype and rtype not in _ALLOWED_RESOURCE_TYPES:
            return False
        suffix = Path(path).suffix.lower()
        return (
            suffix in _STATIC_EXTENSIONS
            or "/_next/static/" in path
            or "/assets/" in path
        )

    def _entry_path(self, url: str) -> Path:
        return self.cache_dir / _cache_key(url)[:2] / f"{_cache_key(url)}.json"

    def _load(self, url: str) -> dict[str, Any] | None:
        path = self._entry_path(url)
        try:
            stat = path.stat()
            if self.max_age and time.time() - stat.st_mtime > self.max_age:
                return None
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("url") != url or not item.get("body_b64"):
                return None
            # 兼容旧缓存中按 base64 长度估算而产生的 1~2 字节偏差。
            item["body_bytes"] = len(base64.b64decode(item["body_b64"]))
            return item
        except Exception:
            return None

    def _store(self, item: dict[str, Any], body_b64: str) -> None:
        try:
            raw_size = len(base64.b64decode(body_b64))
            if raw_size <= 0 or raw_size > self.max_item_bytes:
                return
            url = str(item.get("url") or "")
            if not self.is_cacheable(url, str(item.get("resource_type") or "")):
                return
            payload = {
                "version": 1,
                "url": url,
                "status": int(item.get("status") or 200),
                "resource_type": item.get("resource_type") or "",
                "mime_type": item.get("mime_type") or "",
                "headers": item.get("headers") or {},
                "body_b64": body_b64,
                "body_bytes": raw_size,
                "stored_at": int(time.time()),
            }
            with _CACHE_STORE_LOCK:
                # Chromium 某些版本无法稳定关联 Fetch 与 Network 的请求 ID，命中
                # 本地缓存的响应也可能再次到达记录路径。已有有效文件不重复覆盖，
                # 也不再误报成“本轮新增”。过期文件仍会正常刷新。
                if self._load(url) is not None:
                    return
                path = self._entry_path(url)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, path)
                self.recorded += 1
                self.recorded_bytes += raw_size
                self._recorded_items[url] = raw_size
        except Exception as exc:
            self._remember_error(f"store: {type(exc).__name__}: {exc}")

    def _remember_error(self, message: str) -> None:
        try:
            self._errors.put_nowait(str(message)[:240])
        except queue.Full:
            pass

    def _send(self, method: str, params: dict[str, Any] | None = None) -> int:
        with self._send_lock:
            self._next_id += 1
            command_id = self._next_id
            self._ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
            return command_id

    def _discover_page_ws(self) -> str:
        base = _debugger_http_base(self.debugger_address)
        if not base:
            return ""
        http = requests.Session()
        http.trust_env = False
        response = http.get(base + "/json/list", timeout=5)
        response.raise_for_status()
        targets = response.json()
        pages = [x for x in targets if isinstance(x, dict) and x.get("type") == "page" and x.get("webSocketDebuggerUrl")]
        if not pages:
            return ""
        # 优先当前普通页面；about:blank 也可以，后续导航仍使用同一个 target。
        target = next((x for x in pages if not str(x.get("url") or "").startswith("devtools://")), pages[0])
        return _replace_ws_host(str(target["webSocketDebuggerUrl"]), self.debugger_address)

    def start(self) -> "RoxyLocalAssetCache":
        if not self.enabled:
            return self
        if not self.debugger_address:
            logger.warning("[%s][本地缓存] Roxy 未返回 debuggerAddress，无法启用", self.label)
            return self
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="roxy-asset-cache", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=8)
        if not self._ready.is_set():
            logger.warning("[%s][本地缓存] CDP 连接未及时就绪，本轮继续走网络", self.label)
        return self

    def _run(self) -> None:
        try:
            import websocket

            ws_url = self._discover_page_ws()
            if not ws_url:
                raise RuntimeError("未发现 page CDP target")
            self._ws = websocket.create_connection(
                ws_url,
                timeout=1,
                suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost", "::1"],
            )
            self._send("Network.enable", {
                "maxTotalBufferSize": 200 * 1024 * 1024,
                "maxResourceBufferSize": self.max_item_bytes,
            })
            if self.mode in {"replay", "auto"}:
                self._send("Fetch.enable", {"patterns": [
                    {"urlPattern": "*", "resourceType": kind, "requestStage": "Request"}
                    for kind in sorted(_ALLOWED_RESOURCE_TYPES)
                ]})
            self._ready.set()
            logger.info("[%s][本地缓存] 已启用 mode=%s dir=%s", self.label, self.mode, self.cache_dir)

            while not self._stop.is_set():
                try:
                    raw = self._ws.recv()
                except Exception as exc:
                    # websocket-client 的超时用于周期检查停止信号。
                    if "timed out" in str(exc).lower():
                        continue
                    if self._stop.is_set():
                        break
                    raise
                if not raw:
                    continue
                message = json.loads(raw)
                self._handle_message(message)
        except Exception as exc:
            self._remember_error(f"run: {type(exc).__name__}: {exc}")
            logger.warning("[%s][本地缓存] 已停用：%s: %s", self.label, type(exc).__name__, exc)
        finally:
            self._ready.set()
            try:
                if self._ws is not None:
                    self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _handle_message(self, message: dict[str, Any]) -> None:
        command_id = message.get("id")
        if isinstance(command_id, int) and command_id in self._pending_bodies:
            item = self._pending_bodies.pop(command_id)
            result = message.get("result") or {}
            body = result.get("body")
            if isinstance(body, str):
                body_b64 = body if result.get("base64Encoded") else base64.b64encode(body.encode("utf-8")).decode("ascii")
                self._store(item, body_b64)
            return

        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if method == "Fetch.requestPaused":
            self._handle_paused(params)
        elif method == "Network.responseReceived" and self.mode in {"record", "auto"}:
            self._handle_response(params)
        elif method == "Network.loadingFinished" and self.mode in {"record", "auto"}:
            self._handle_finished(params)

    def _handle_paused(self, params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        resource_type = str(params.get("resourceType") or "")
        try:
            if str(request.get("method") or "GET").upper() != "GET" or not self.is_cacheable(url, resource_type):
                self._send("Fetch.continueRequest", {"requestId": request_id})
                return
            cached = self._load(url)
            if not cached:
                self.cache_misses += 1
                self._send("Fetch.continueRequest", {"requestId": request_id})
                return
            headers = []
            for name, value in (cached.get("headers") or {}).items():
                # 不回放旧 cf-ray/date/age/server/x-ms-* 等边缘节点或时效字段。
                # Content-Encoding/Length 也由 fulfillRequest 根据解码后的 body 重建。
                if str(name).lower() not in _REPLAY_RESPONSE_HEADERS:
                    continue
                if isinstance(value, (str, int, float)):
                    headers.append({"name": str(name), "value": str(value)})
            if not any(x["name"].lower() == "content-type" for x in headers) and cached.get("mime_type"):
                headers.append({"name": "Content-Type", "value": str(cached["mime_type"])})
            network_id = str(params.get("networkId") or "")
            self._send("Fetch.fulfillRequest", {
                "requestId": request_id,
                "responseCode": int(cached.get("status") or 200),
                "responseHeaders": headers,
                "body": str(cached["body_b64"]),
            })
            with self._fulfilled_lock:
                if network_id:
                    self._fulfilled_network_ids.add(network_id)
                # 部分 Chromium 在 Request 阶段的 Fetch.requestPaused 不提供
                # networkId；另有版本返回的 ID 与 Network.requestId 不一致。
                # 无论是否拿到 networkId，都用精确 URL 计数作关联兜底。
                self._fulfilled_urls[url] += 1
            self.cache_hits += 1
            self.bytes_saved += int(cached.get("body_bytes") or 0)
        except Exception as exc:
            self._remember_error(f"paused: {type(exc).__name__}: {exc}")
            try:
                self._send("Fetch.continueRequest", {"requestId": request_id})
            except Exception:
                pass

    def _handle_response(self, params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        response = params.get("response") or {}
        url = str(response.get("url") or "")
        with self._fulfilled_lock:
            if request_id and request_id in self._fulfilled_network_ids:
                # ID 已匹配时也要消费 URL 兜底计数，避免残留计数误匹配后续请求。
                if self._fulfilled_urls.get(url, 0) > 0:
                    self._fulfilled_urls[url] -= 1
                    if self._fulfilled_urls[url] <= 0:
                        self._fulfilled_urls.pop(url, None)
                return
            if self._fulfilled_urls.get(url, 0) > 0:
                self._fulfilled_urls[url] -= 1
                if self._fulfilled_urls[url] <= 0:
                    self._fulfilled_urls.pop(url, None)
                if request_id:
                    self._fulfilled_network_ids.add(request_id)
                return
        resource_type = str(params.get("type") or "")
        status = int(response.get("status") or 0)
        if status != 200 or not self.is_cacheable(url, resource_type):
            return
        self._responses[request_id] = {
            "url": url,
            "status": status,
            "resource_type": resource_type,
            "mime_type": response.get("mimeType") or "",
            "headers": response.get("headers") or {},
        }

    def _handle_finished(self, params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        if self.was_fulfilled_network_request(request_id):
            self._responses.pop(request_id, None)
            return
        item = self._responses.pop(request_id, None)
        if not item:
            return
        try:
            command_id = self._send("Network.getResponseBody", {"requestId": request_id})
            self._pending_bodies[command_id] = item
        except Exception as exc:
            self._remember_error(f"body: {type(exc).__name__}: {exc}")

    def was_fulfilled_network_request(self, request_id: str) -> bool:
        """供流量统计器识别 CDP 本地响应，避免误计为代理下载流量。"""
        if not request_id:
            return False
        with self._fulfilled_lock:
            return str(request_id) in self._fulfilled_network_ids

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
                    self._recorded_items.items(), key=lambda pair: pair[1], reverse=True
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
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.settimeout(0.1)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        result = self.snapshot()
        self._final_snapshot = dict(result)
        if self.enabled:
            logger.info(
                "[%s][本地缓存] 结束 recorded=%s hits=%s misses=%s saved=%sB errors=%s",
                self.label, result["recorded"], result["hits"], result["misses"],
                result["bytes_saved"], len(result["errors"]),
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


__all__ = ["RoxyLocalAssetCache"]
