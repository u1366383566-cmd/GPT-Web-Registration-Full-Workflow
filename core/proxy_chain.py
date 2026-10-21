"""Small local SOCKS5 relay for chaining an upstream proxy to a target proxy."""
from __future__ import annotations

import ipaddress
import base64
import logging
import select
import socket
import threading
from typing import Any
from urllib.parse import urlsplit

from core.proxy_utils import mask_proxy_url, parse_proxy_url

try:
    import socks
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    socks = None


logger = logging.getLogger(__name__)


class ProxyChainError(ConnectionError):
    """A user-facing error from the outer proxy hop."""


class SocksConnectError(ProxyChainError):
    """SOCKS5 代理已返回的 CONNECT 错误，不代表账号密码错误。"""

    def __init__(self, code: int):
        self.code = code
        reasons = {1: "代理服务器一般性失败", 2: "代理规则拒绝", 3: "网络不可达", 4: "目标主机不可达",
                   5: "目标端口拒绝连接", 6: "连接 TTL 超时", 7: "不支持 CONNECT 命令", 8: "不支持目标地址类型"}
        super().__init__(f"动态代理 CONNECT 失败（SOCKS5 code {code}：{reasons.get(code, '未知错误')}）")

    @property
    def retryable(self) -> bool:
        return self.code in {1, 3, 4, 6}


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("SOCKS5 客户端提前断开")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_socks_address(sock: socket.socket, atyp: int) -> tuple[str, int]:
    if atyp == 1:
        host = socket.inet_ntoa(_read_exact(sock, 4))
    elif atyp == 3:
        length = _read_exact(sock, 1)[0]
        host = _read_exact(sock, length).decode("idna")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _read_exact(sock, 16))
    else:
        raise ValueError("SOCKS5 地址类型无效")
    port = int.from_bytes(_read_exact(sock, 2), "big")
    return host, port


class ProxyChainRelay:
    """Expose a local HTTP CONNECT endpoint that reaches target through upstream."""

    def __init__(self, target: str, upstream: str = "", *, timeout: float = 15.0, blocked_hosts=()):
        self.target = parse_proxy_url(target)
        self.upstream = parse_proxy_url(upstream) if upstream else None
        if self.target.scheme not in {"http", "socks5", "socks5h"}:
            raise ValueError("代理链目标代理必须是 http://、socks5:// 或 socks5h://")
        if self.upstream and self.upstream.scheme == "https":
            raise ValueError("代理链暂不支持 https:// 作为上游代理，请填写 http:// 或 socks5://")
        if self.upstream and socks is None:
            raise RuntimeError("代理链需要 PySocks，请先安装 requirements.txt 中的依赖")
        self.timeout = max(1.0, float(timeout or 15.0))
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._last_error = ""
        self._traffic_lock = threading.Lock()
        self._upload_bytes = 0
        self._download_bytes = 0
        self._traffic_by_destination: dict[tuple[str, int], dict[str, Any]] = {}
        self._blocked_hosts = tuple(sorted({self._destination_key(str(host).removeprefix("*."), 443)[0]
                                           for host in blocked_hosts}))

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def proxy_url(self) -> str:
        if self._listener is None:
            raise RuntimeError("代理链尚未启动")
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def traffic_snapshot(self) -> dict[str, Any]:
        """返回整个代理链隧道的实际传输字节数。"""
        with self._traffic_lock:
            upload = int(self._upload_bytes)
            download = int(self._download_bytes)
            destinations = [dict(item, total_bytes=item["upload_bytes"] + item["download_bytes"])
                            for item in self._traffic_by_destination.values()]
            destinations.sort(key=lambda item: (-item["total_bytes"], item["host"], item["port"]))
        return {
            "available": True,
            "upload_bytes": upload,
            "download_bytes": download,
            "total_bytes": upload + download,
            "destinations": destinations,
            "attribution": "proxy_connection_destination",
            "blocked_hosts": list(self._blocked_hosts),
            "blocked_connection_count": sum(item["blocked_connection_count"] for item in destinations),
        }

    @staticmethod
    def _destination_key(host: str, port: int) -> tuple[str, int]:
        host = str(host).strip().lower().rstrip(".")
        if not host or len(host) > 253 or any(char.isspace() or char in "/\\@?#" or ord(char) < 32 for char in host):
            raise ValueError("代理请求目标主机无效")
        if not 1 <= int(port) <= 65535:
            raise ValueError("代理请求目标端口无效")
        return host, int(port)

    def _destination_stats(self, destination: tuple[str, int] | None) -> dict[str, Any]:
        """调用方持有 _traffic_lock；直接调用 _relay 的测试/工具也可归入 unknown。"""
        host, port = destination or ("<unknown>", 0)
        return self._traffic_by_destination.setdefault((host, port), {
            "host": host, "port": port, "connection_count": 0, "failed_connection_count": 0,
            "blocked_connection_count": 0,
            "connect_retry_count": 0, "recovered_connection_count": 0,
            "upload_bytes": 0, "download_bytes": 0,
        })

    def _record_transfer(self, destination: tuple[str, int] | None, *, upload: int = 0, download: int = 0) -> None:
        with self._traffic_lock:
            self._upload_bytes += upload
            self._download_bytes += download
            stats = self._destination_stats(destination)
            stats["upload_bytes"] += upload
            stats["download_bytes"] += download

    def start(self) -> "ProxyChainRelay":
        if self._listener is not None:
            return self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.25)
        self._listener = listener
        self._accept_thread = threading.Thread(target=self._accept_loop, name="proxy-chain-accept", daemon=True)
        self._accept_thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def __enter__(self) -> "ProxyChainRelay":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _track(self, conn: socket.socket, add: bool) -> None:
        with self._connections_lock:
            if add:
                self._connections.add(conn)
            else:
                self._connections.discard(conn)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._track(client, True)
            threading.Thread(target=self._handle_client, args=(client,), name="proxy-chain-client", daemon=True).start()

    def _connect_target_through_upstream(self, host: str, port: int) -> socket.socket:
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=self.timeout)
        proxy_type = socks.HTTP if self.upstream.scheme == "http" else socks.SOCKS5
        remote = socks.socksocket()
        remote.set_proxy(
            proxy_type,
            addr=self.upstream.host,
            port=self.upstream.port,
            username=self.upstream.username or None,
            password=self.upstream.password or None,
            rdns=self.upstream.scheme == "socks5h",
        )
        remote.settimeout(self.timeout)
        try:
            remote.connect((host, port))
        except Exception:
            remote.close()
            raise
        return remote

    @staticmethod
    def _pack_socks_address(host: str, port: int) -> bytes:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise ValueError("SOCKS5 目标域名过长")
            return b"\x03" + bytes([len(encoded)]) + encoded + int(port).to_bytes(2, "big")
        if address.version == 4:
            return b"\x01" + address.packed + int(port).to_bytes(2, "big")
        return b"\x04" + address.packed + int(port).to_bytes(2, "big")

    @staticmethod
    def _read_socks_reply(sock: socket.socket) -> int:
        header = _read_exact(sock, 4)
        if header[0] != 5:
            raise ValueError("动态代理返回了非 SOCKS5 响应")
        _read_socks_address(sock, header[3])
        return header[1]

    def _target_socks_connect(self, remote: socket.socket, host: str, port: int) -> None:
        methods = b"\x00"
        if self.target.username or self.target.password:
            methods += b"\x02"
        remote.sendall(b"\x05" + bytes([len(methods)]) + methods)
        selected = _read_exact(remote, 2)
        if selected[0] != 5:
            response = selected
            try:
                response += remote.recv(512)
            except OSError:
                pass
            if response.startswith(b"HTTP/"):
                first_line = response.splitlines()[0].decode("latin1", errors="replace")
                raise ProxyChainError(f"动态代理经本地上游返回 {first_line}，不是 SOCKS5 响应")
            raise ProxyChainError("动态代理经本地上游返回了非 SOCKS5 响应")
        if selected[1] == 0xFF:
            raise ProxyChainError("动态代理不接受当前认证方式")
        if selected[1] == 2:
            username = self.target.username.encode("utf-8")
            password = self.target.password.encode("utf-8")
            remote.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            auth = _read_exact(remote, 2)
            if auth[0] != 1 or auth[1] != 0:
                raise PermissionError("动态代理账号密码认证失败")
        elif selected[1] != 0:
            raise ConnectionError("动态代理选择了不支持的认证方式")

        remote.sendall(b"\x05\x01\x00" + self._pack_socks_address(host, port))
        reply_code = self._read_socks_reply(remote)
        if reply_code:
            raise SocksConnectError(reply_code)

    def _open_target_tunnel(self, host: str, port: int, method: str, destination: tuple[str, int]) -> socket.socket:
        """仅在转发浏览器数据前重试握手；绝不重放 HTTP 请求或 TLS 数据。"""
        for attempt in range(1, 4):
            remote = None
            try:
                if self._stop.is_set():
                    raise ConnectionAbortedError("代理中继正在关闭")
                remote = self._connect_target_through_upstream(self.target.host, self.target.port)
                self._track(remote, True)
                if self.target.scheme in {"socks5", "socks5h"}:
                    self._target_socks_connect(remote, host, port)
                elif method == "CONNECT":
                    self._target_http_connect(remote, host, port)
                if attempt > 1:
                    with self._traffic_lock:
                        self._destination_stats(destination)["recovered_connection_count"] += 1
                    logger.info("代理重连已恢复 destination=%s:%s attempts=%s", *destination, attempt)
                return remote
            except Exception as exc:
                if remote is not None:
                    self._track(remote, False)
                    try:
                        remote.close()
                    except OSError:
                        pass
                if not isinstance(exc, SocksConnectError) or not exc.retryable or attempt == 3 or self._stop.is_set():
                    raise
                with self._traffic_lock:
                    self._destination_stats(destination)["connect_retry_count"] += 1
                delay = 0.25 * attempt
                logger.info("代理临时建连失败 destination=%s:%s code=%s，%.2fs 后重试（%s/3）",
                            *destination, exc.code, delay, attempt + 1)
                if self._stop.wait(delay):
                    raise
        raise AssertionError("unreachable")

    def _target_http_connect(self, remote: socket.socket, host: str, port: int) -> None:
        """经上游连接到 HTTP 目标代理，并在目标代理上建立 CONNECT 隧道。"""
        destination = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
        headers = [
            f"CONNECT {destination} HTTP/1.1",
            f"Host: {destination}",
            "Proxy-Connection: Keep-Alive",
        ]
        if self.target.username or self.target.password:
            credentials = f"{self.target.username or ''}:{self.target.password or ''}".encode("utf-8")
            headers.append("Proxy-Authorization: Basic " + base64.b64encode(credentials).decode("ascii"))
        remote.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin1"))

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = remote.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 目标代理提前断开")
            response.extend(chunk)
            if len(response) > 64 * 1024:
                raise ValueError("HTTP 目标代理响应头过大")
        first_line = bytes(response).split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise ProxyChainError("目标代理返回了无效的 HTTP 响应")
        try:
            status = int(parts[1])
        except ValueError as exc:
            raise ProxyChainError("目标代理返回了无效的 HTTP 状态码") from exc
        if status != 200:
            raise ProxyChainError(f"目标代理 CONNECT 失败（HTTP {status}）")

    @staticmethod
    def _read_http_request(client: socket.socket) -> tuple[str, str, bytes]:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = client.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 代理客户端提前断开")
            request.extend(chunk)
            if len(request) > 64 * 1024:
                raise ValueError("HTTP 代理请求头过大")
        first_line = bytes(request).split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) != 3:
            raise ValueError("HTTP 代理请求行无效")
        return parts[0].upper(), parts[1], bytes(request)

    @staticmethod
    def _split_host_port(destination: str) -> tuple[str, int]:
        if destination.startswith("["):
            host, port_text = destination.rsplit("]:", 1)
            host = host[1:]
        else:
            host, port_text = destination.rsplit(":", 1)
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError("HTTP 代理目标端口无效")
        return host, port

    @staticmethod
    def _send_http_error(client: socket.socket) -> None:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass

    def _handle_client(self, client: socket.socket) -> None:
        remote: socket.socket | None = None
        destination_key: tuple[str, int] | None = None
        connect_response_sent = False
        try:
            client.settimeout(self.timeout)
            method, destination, initial = self._read_http_request(client)
            if method == "CONNECT":
                host, port = self._split_host_port(destination)
            else:
                parsed = urlsplit(destination)
                if parsed.scheme != "http" or not parsed.hostname:
                    raise ValueError("HTTP 代理请求必须使用 http:// 绝对 URL")
                host, port = parsed.hostname, parsed.port or 80
            destination_key = self._destination_key(host, port)
            with self._traffic_lock:
                self._destination_stats(destination_key)["connection_count"] += 1
            normalized_host = destination_key[0]
            if any(normalized_host == rule or normalized_host.endswith("." + rule) for rule in self._blocked_hosts):
                with self._traffic_lock:
                    stats = self._destination_stats(destination_key)
                    stats["blocked_connection_count"] += 1
                    first_block = stats["blocked_connection_count"] == 1
                if first_block:
                    logger.info("[代理省流量] 已在连接上游前拦截 target=%s:%s（后续重试仅累计次数）", *destination_key)
                try:
                    client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
                return
            remote = self._open_target_tunnel(host, port, method, destination_key)
            remote.settimeout(0.5)
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\nConnection: keep-alive\r\n\r\n")
                connect_response_sent = True
                # CONNECT 头不走目标隧道；保留同一 recv 中提前到达的 TLS 数据。
                initial = initial.split(b"\r\n\r\n", 1)[1]
            else:
                header, body = initial.split(b"\r\n\r\n", 1)
                lines = header.split(b"\r\n")
                version = lines[0].rsplit(b" ", 1)[1]
                path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                if self.target.scheme in {"socks5", "socks5h"}:
                    lines[0] = method.encode("ascii") + b" " + path.encode("ascii") + b" " + version
                lines = [line for line in lines if not line.lower().startswith((b"proxy-authorization:", b"proxy-connection:"))]
                # 普通 HTTP 禁止跨目标复用同一代理连接，保证域名归因准确。
                # Upgrade 流量仍保留 Connection/Upgrade，以便透传升级连接。
                if not any(line.lower().startswith(b"upgrade:") for line in lines):
                    lines = [line for line in lines if not line.lower().startswith(b"connection:")]
                    lines.append(b"Connection: close")
                if self.target.scheme == "http" and (self.target.username or self.target.password):
                    credentials = f"{self.target.username or ''}:{self.target.password or ''}".encode("utf-8")
                    lines.append(b"Proxy-Authorization: Basic " + base64.b64encode(credentials))
                initial = b"\r\n".join(lines) + b"\r\n\r\n" + body
            if initial:
                remote.sendall(initial)
                self._record_transfer(destination_key, upload=len(initial))
            client.settimeout(0.5)
            self._relay(client, remote, destination=destination_key)
        except Exception as exc:
            if self._stop.is_set():
                logger.debug("代理中继关闭时连接终止 destination=%s", destination_key)
                return
            detail = str(exc or "").strip()
            self._last_error = f"{type(exc).__name__}: {detail[:240]}" if detail else type(exc).__name__
            if destination_key is not None:
                with self._traffic_lock:
                    self._destination_stats(destination_key)["failed_connection_count"] += 1
            logger.warning(
                "代理链连接失败 target=%s upstream=%s destination=%s error=%s",
                mask_proxy_url(self.target.raw),
                mask_proxy_url(self.upstream.raw) if self.upstream else "direct",
                "%s:%s" % destination_key if destination_key else "unknown",
                self._last_error,
            )
            if not connect_response_sent:
                self._send_http_error(client)
        finally:
            for conn in (client, remote):
                if conn is not None:
                    self._track(conn, False)
                    try:
                        conn.close()
                    except OSError:
                        pass

    def _relay(self, client: socket.socket, remote: socket.socket, *, destination: tuple[str, int] | None = None) -> None:
        sockets = [client, remote]
        while not self._stop.is_set():
            try:
                readable, _, exceptional = select.select(sockets, [], sockets, 0.5)
            except (OSError, ValueError):
                return
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                try:
                    data = source.recv(64 * 1024)
                except socket.timeout:
                    # select 与 recv 之间连接状态可能发生变化；不要因为一次
                    # 短暂的空读就关闭整个 HTTPS 隧道。
                    continue
                if not data:
                    return
                recipient = remote if source is client else client
                recipient.sendall(data)
                if source is client:
                    self._record_transfer(destination, upload=len(data))
                else:
                    self._record_transfer(destination, download=len(data))


def browser_blocked_proxy_hosts() -> tuple[str, ...]:
    """只由本地浏览器调用；协议请求、邮箱 API 等代理链不自动套用规则。"""
    from config import browser as browser_cfg
    if not bool(getattr(browser_cfg, "BROWSER_DATA_SAVER_MODE", False)):
        return ()
    hosts = getattr(browser_cfg, "BROWSER_DATA_SAVER_BLOCKED_PROXY_HOSTS", ["dl.google.com", "gvt1.com"])
    if isinstance(hosts, str):
        hosts = hosts.replace(",", "\n").splitlines()
    return tuple(str(host).strip() for host in (hosts or []) if str(host).strip())


def open_proxy_pool_proxy(
    selected_proxy: str | None = None,
    *,
    timeout: float = 15.0,
    measure_transport: bool = False,
):
    """Resolve a proxy-pool target and optionally wrap it with its own upstream.

    The package/Agent upstream is intentionally not consulted here.  An empty
    ``PROXY_POOL_UPSTREAM_PROXY`` returns the selected target unchanged unless
    ``measure_transport`` requests a local counting relay for browser traffic.
    """
    from config import proxy as proxy_cfg

    target = str(
        proxy_cfg.pick_proxy() if selected_proxy is None else selected_proxy
        or ""
    ).strip()
    upstream = str(getattr(proxy_cfg, "PROXY_POOL_UPSTREAM_PROXY", "") or "").strip()
    if not target or (not upstream and not measure_transport):
        return target, None

    # 保持未支持协议的既有直连行为，不能为了统计改变出口。
    if not upstream and parse_proxy_url(target).scheme not in {"http", "socks5", "socks5h"}:
        return target, None

    options = {"timeout": timeout}
    if measure_transport:
        options["blocked_hosts"] = browser_blocked_proxy_hosts()
    relay = ProxyChainRelay(target, upstream, **options).start()
    return relay.proxy_url, relay
