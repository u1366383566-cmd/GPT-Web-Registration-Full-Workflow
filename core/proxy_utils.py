"""Common parsing helpers for HTTP and SOCKS proxy URLs."""
from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})


@dataclass(frozen=True)
class ProxySpec:
    raw: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def parse_proxy_url(value: str, *, allow_bare: bool = False) -> ProxySpec:
    text = str(value or "").strip()
    if not text:
        raise ValueError("代理地址为空")
    text = text.replace("\\@", "@")
    if allow_bare and "://" not in text:
        text = f"socks5://{text}"
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme or '-'}")
    if not parsed.hostname:
        raise ValueError("代理格式缺少 host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if not port or not 1 <= port <= 65535:
        raise ValueError("代理格式缺少有效 port")
    return ProxySpec(
        raw=text,
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def mask_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        spec = parse_proxy_url(text)
    except ValueError:
        return "***"
    auth = "***:***@" if spec.username or spec.password else ""
    return f"{spec.scheme}://{auth}{spec.host}:{spec.port}"


def validate_proxy_lines(lines) -> None:
    for index, line in enumerate(lines or [], 1):
        try:
            parse_proxy_url(line)
        except ValueError as exc:
            raise ValueError(f"代理池第 {index} 行格式错误: {exc}") from exc


def diagnose_proxy_endpoint(value: str, *, timeout: float = 2.0) -> str:
    try:
        spec = parse_proxy_url(value)
        with socket.create_connection((spec.host, spec.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"\x05\x01\x00")
            response = sock.recv(512)
    except Exception:
        return ""
    if response.startswith(b"HTTP/"):
        first_line = response.splitlines()[0].decode("latin1", errors="replace")
        return f"代理端口返回 {first_line}，当前端口可能不是 SOCKS5 或授权/白名单不匹配"
    if response[:1] == b"\x05":
        return "SOCKS5 握手响应正常，可能是账号密码或目标地址被拒绝"
    return "代理端口返回了非 SOCKS5 响应，请确认协议和端口"
