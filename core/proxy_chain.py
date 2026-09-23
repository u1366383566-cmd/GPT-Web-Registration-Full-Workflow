"""Small local SOCKS5 relay for chaining an upstream proxy to a target proxy."""
from __future__ import annotations

import ipaddress
import base64
import logging
import select
import socket
import threading

from core.proxy_utils import mask_proxy_url, parse_proxy_url

try:
    import socks
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    socks = None


logger = logging.getLogger(__name__)


class ProxyChainError(ConnectionError):
    """A user-facing error from the outer proxy hop."""


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

    def __init__(self, target: str, upstream: str, *, timeout: float = 15.0):
        self.target = parse_proxy_url(target)
        self.upstream = parse_proxy_url(upstream)
        if self.target.scheme not in {"http", "socks5", "socks5h"}:
            raise ValueError("代理链目标代理必须是 http://、socks5:// 或 socks5h://")
        if self.upstream.scheme == "https":
            raise ValueError("代理链暂不支持 https:// 作为上游代理，请填写 http:// 或 socks5://")
        if socks is None:
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

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def proxy_url(self) -> str:
        if self._listener is None:
            raise RuntimeError("代理链尚未启动")
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def traffic_snapshot(self) -> dict[str, int | bool]:
        """返回整个代理链隧道的实际传输字节数。"""
        with self._traffic_lock:
            upload = int(self._upload_bytes)
            download = int(self._download_bytes)
        return {
            "available": True,
            "upload_bytes": upload,
            "download_bytes": download,
            "total_bytes": upload + download,
        }

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
        remote.connect((host, port))
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
            raise ConnectionError(f"动态代理 CONNECT 失败（SOCKS5 code {reply_code}）")

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
    def _read_http_request(client: socket.socket) -> tuple[str, str, str]:
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
        return parts[0].upper(), parts[1], parts[2]

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
        try:
            client.settimeout(self.timeout)
            method, destination, _ = self._read_http_request(client)
            if method != "CONNECT":
                self._send_http_error(client)
                return
            host, port = self._split_host_port(destination)
            remote = self._connect_target_through_upstream(self.target.host, self.target.port)
            if self.target.scheme in {"socks5", "socks5h"}:
                self._target_socks_connect(remote, host, port)
            else:
                self._target_http_connect(remote, host, port)
            remote.settimeout(0.5)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\nConnection: keep-alive\r\n\r\n")
            client.settimeout(0.5)
            self._relay(client, remote)
        except Exception as exc:
            detail = str(exc or "").strip()
            self._last_error = f"{type(exc).__name__}: {detail[:240]}" if detail else type(exc).__name__
            logger.warning(
                "代理链连接失败 target=%s upstream=%s error=%s",
                mask_proxy_url(self.target.raw),
                mask_proxy_url(self.upstream.raw),
                self._last_error,
            )
            self._send_http_error(client)
        finally:
            for conn in (client, remote):
                if conn is not None:
                    self._track(conn, False)
                    try:
                        conn.close()
                    except OSError:
                        pass

    def _relay(self, client: socket.socket, remote: socket.socket) -> None:
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
                destination = remote if source is client else client
                destination.sendall(data)
                with self._traffic_lock:
                    if source is client:
                        self._upload_bytes += len(data)
                    else:
                        self._download_bytes += len(data)


def open_proxy_pool_proxy(
    selected_proxy: str | None = None,
    *,
    timeout: float = 15.0,
):
    """Resolve a proxy-pool target and optionally wrap it with its own upstream.

    The package/Agent upstream is intentionally not consulted here.  An empty
    ``PROXY_POOL_UPSTREAM_PROXY`` returns the selected target unchanged and no
    relay, so callers can use the same cleanup path for both modes.
    """
    from config import proxy as proxy_cfg

    target = str(
        proxy_cfg.pick_proxy() if selected_proxy is None else selected_proxy
        or ""
    ).strip()
    upstream = str(getattr(proxy_cfg, "PROXY_POOL_UPSTREAM_PROXY", "") or "").strip()
    if not target or not upstream:
        return target, None

    relay = ProxyChainRelay(target, upstream, timeout=timeout).start()
    return relay.proxy_url, relay
