# -*- coding: utf-8 -*-
"""ChatGPT2API account-pool import client."""
from __future__ import annotations

from typing import Any

import requests

from config.env_loader import env_int, env_value


DEFAULT_BASE_URL = "http://127.0.0.1:8021"


class ChatGPT2ApiImportError(RuntimeError):
    """Raised when the local ChatGPT2API account import cannot complete."""


def _settings() -> tuple[str, str, int]:
    base_url = str(env_value("CHATGPT2API_BASE_URL", DEFAULT_BASE_URL, "str") or "").strip().rstrip("/")
    auth_key = str(env_value("CHATGPT2API_AUTH_KEY", "", "str") or "").strip()
    timeout = max(3, min(60, env_int("CHATGPT2API_IMPORT_TIMEOUT", 20)))
    if not base_url:
        raise ChatGPT2ApiImportError("请先在配置页填写 ChatGPT2API 地址")
    if not auth_key:
        raise ChatGPT2ApiImportError("请先在配置页填写 ChatGPT2API 管理员密钥")
    return base_url, auth_key, timeout


def build_account_payload(account: dict[str, Any]) -> dict[str, Any] | None:
    token = str(account.get("access_token") or "").strip()
    if not token:
        return None
    return {
        "access_token": token,
        "email": str(account.get("email") or "").strip() or None,
        "user_id": str(account.get("user_id") or "").strip() or None,
        "type": str(account.get("current_plan_type") or account.get("plan_type") or "free").strip() or "free",
        # This is a regular ChatGPT web account, not a native Codex credential.
        "source_type": "web",
    }


def import_accounts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    payloads = [payload for account in accounts if (payload := build_account_payload(account)) is not None]
    if not payloads:
        raise ChatGPT2ApiImportError("所选账号没有可导入的 access_token")

    base_url, auth_key, timeout = _settings()
    try:
        response = requests.post(
            f"{base_url}/api/accounts",
            headers={"Authorization": f"Bearer {auth_key}"},
            json={"accounts": payloads},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ChatGPT2ApiImportError(f"无法连接 ChatGPT2API：{exc}") from exc

    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.ok:
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("error") or detail.get("message")
        message = str(detail or (data.get("error") if isinstance(data, dict) else "") or f"HTTP {response.status_code}")
        raise ChatGPT2ApiImportError(f"ChatGPT2API 导入失败：{message[:300]}")

    return {
        "submitted": len(payloads),
        "added": int(data.get("added") or 0) if isinstance(data, dict) else 0,
        "skipped": int(data.get("skipped") or 0) if isinstance(data, dict) else 0,
    }
