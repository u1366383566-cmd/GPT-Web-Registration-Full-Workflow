# -*- coding: utf-8 -*-
"""Mail.cx 临时邮箱 API 客户端。"""
from __future__ import annotations

import logging
import random
import re
import secrets
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, looks_like_openai_email

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 35
_DOMAIN_CACHE: tuple[float, list[str]] | None = None
_DOMAIN_CACHE_TTL = 300
_CONTEXT_CACHE: dict[str, "MailCXAccount"] = {}
# Mail.cx limits every token to one concurrent long-poll request.
_LONG_POLL_LOCK = threading.Lock()


class MailCXError(RuntimeError):
    """Mail.cx API 请求或验证码读取失败。"""


@dataclass
class MailCXAccount:
    email: str
    domain: str


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url() -> str:
    base = str(getattr(_email_cfg, "MAILCX_API_BASE", "") or "https://api.mail.cx/v1").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        base = "https://" + base
    return base


def _headers() -> dict[str, str]:
    token = str(getattr(_email_cfg, "MAILCX_API_TOKEN", "") or "").strip()
    if not token:
        raise MailCXError("Mail.cx API Token 未配置，请在 WebUI「配置 → 邮箱 / OTP → Mail.cx」填写。")
    return {"Accept": "application/json", "x-api-token": token}


def _request(path: str, *, params: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> tuple[int, dict | None]:
    try:
        response = requests.get(_base_url() + path, headers=_headers(), params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise MailCXError(f"Mail.cx 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc
    if response.status_code == 204:
        return 204, None
    try:
        payload = response.json()
    except ValueError as exc:
        raise MailCXError(f"Mail.cx 响应不是 JSON ({path}): HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        error = payload.get("error") if isinstance(payload, dict) else ""
        raise MailCXError(f"Mail.cx 请求失败 ({path}): HTTP {response.status_code}; {error or str(payload)[:160]}")
    if not isinstance(payload, dict):
        raise MailCXError(f"Mail.cx 响应格式无效 ({path})")
    return response.status_code, payload


def _parse_domains(payload: dict) -> list[str]:
    raw = payload.get("system_domains") or payload.get("domains") or []
    out: list[str] = []
    defaults: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            domain = str(item.get("domain") or "").strip().lower().lstrip("@")
            if item.get("default") and domain:
                defaults.append(domain)
        else:
            domain = str(item or "").strip().lower().lstrip("@")
        if domain and "." in domain and domain not in out:
            out.append(domain)
    return defaults + [domain for domain in out if domain not in defaults]


def fetch_domains(force: bool = False) -> list[str]:
    global _DOMAIN_CACHE
    now = time.monotonic()
    if not force and _DOMAIN_CACHE and now - _DOMAIN_CACHE[0] < _DOMAIN_CACHE_TTL:
        return list(_DOMAIN_CACHE[1])
    status, payload = _request("/config")
    if status != 200 or payload is None:
        raise MailCXError("Mail.cx /config 未返回系统域名")
    domains = _parse_domains(payload)
    if not domains:
        raise MailCXError("Mail.cx /config 响应中未找到 system_domains")
    _DOMAIN_CACHE = (now, domains)
    return list(domains)


def _domain() -> str:
    configured = str(getattr(_email_cfg, "MAILCX_DOMAIN", "") or "").strip().lower().lstrip("@")
    if configured:
        return configured
    return fetch_domains()[0]


def _random_local_part() -> str:
    length = int(getattr(_email_cfg, "MAILCX_RANDOM_LOCAL_LENGTH", 12) or 12)
    length = max(2, min(20, length))
    alphabet = string.ascii_lowercase + string.digits
    return random.choice(string.ascii_lowercase) + "".join(secrets.choice(alphabet) for _ in range(length - 1))


def pick_account() -> MailCXAccount:
    # Mail.cx mailboxes are implicit: SMTP starts buffering once an address receives mail.
    # 即使使用固定自有域名，也在领取时尽早验证 Token，避免任务走到 OTP 阶段才报配置错误。
    _headers()
    domain = _domain()
    account = MailCXAccount(email=f"{_random_local_part()}@{domain}", domain=domain)
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    logger.info("[Mail.cx] 已生成临时邮箱: %s", account.email)
    return account


def get_account_context(email: str) -> MailCXAccount | None:
    return _CONTEXT_CACHE.get(_cache_key(email))


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    _CONTEXT_CACHE.pop(_cache_key(email), None)
    logger.info("[Mail.cx] 已释放临时邮箱: %s（status=%s, note=%s）", email, status, note or "")


def _timestamp(item: dict) -> float | None:
    raw = item.get("created_at") or item.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _otp_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "from": item.get("from_email") or item.get("from") or item.get("sender") or "",
        "subject": item.get("subject") or "",
        "text": item.get("text") or item.get("text_body") or item.get("body") or "",
        "html": item.get("html") or item.get("html_body") or "",
    }


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """使用 Mail.cx 的服务端长轮询等待并提取最新 OpenAI OTP。"""
    target = str(email or "").strip()
    if not target:
        raise MailCXError("Mail.cx 取码缺少邮箱地址")
    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    deadline = time.monotonic() + max(0, wait_seconds)
    cursor: str | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"
    logger.info("[Mail.cx] 开始长轮询邮箱 %s，最长 %ss", target, wait_seconds)

    while (remaining := deadline - time.monotonic()) > 0:
        # The API holds at most 25 seconds; leave room for the HTTP response itself.
        if not _LONG_POLL_LOCK.acquire(timeout=min(remaining, 26)):
            continue
        try:
            params = {"count": 1, "limit": 20}
            if cursor:
                params["since"] = cursor
            status, payload = _request(f"/inbox/{target}", params=params, timeout=max(30, min(REQUEST_TIMEOUT, int(remaining) + 5)))
        except MailCXError as exc:
            last_error = str(exc)
            if "token_busy" in last_error:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            continue
        finally:
            _LONG_POLL_LOCK.release()

        if status == 204 or not payload:
            continue
        next_cursor = str(payload.get("next_since") or "").strip()
        if next_cursor:
            cursor = next_cursor
        emails = payload.get("emails")
        if not isinstance(emails, list):
            last_error = "Mail.cx 收件箱响应缺少 emails 数组"
            continue
        for summary in emails:
            if not isinstance(summary, dict):
                continue
            message_time = _timestamp(summary)
            if after_ts is not None and message_time is not None and message_time < after_ts - 30:
                continue
            if not looks_like_openai_email(_otp_item(summary)):
                continue
            message_id = str(summary.get("id") or "").strip()
            if not message_id:
                continue
            try:
                _, detail = _request(f"/email/{message_id}")
            except MailCXError as exc:
                last_error = str(exc)
                continue
            if not detail or not looks_like_openai_email(_otp_item(detail)):
                continue
            otp = extract_otp(_otp_item(detail))
            if otp:
                logger.info("[Mail.cx] 已收到 OpenAI OTP: %s", target)
                return otp

    raise MailCXError(f"等待 Mail.cx 验证码超时: {target}; {last_error}")
