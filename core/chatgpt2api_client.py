# -*- coding: utf-8 -*-
"""ChatGPT2API account-pool import client."""
from __future__ import annotations

from typing import Any, Literal

import requests

from config.env_loader import env_int, env_str, env_value


DEFAULT_BASE_URL = "http://127.0.0.1:8021"
_TARGET_LABELS = {"legacy": "旧版", "new": "新版"}
_ImportTarget = Literal["legacy", "new"]


class ChatGPT2ApiImportError(RuntimeError):
    """Raised when the local ChatGPT2API account import cannot complete."""


def resolve_target(target: str | None = None) -> Literal["legacy", "new", "both"]:
    value = target if target is not None else env_str("CHATGPT2API_IMPORT_TARGET", "legacy")
    normalized = str(value or "").strip().lower()
    if normalized not in {"legacy", "new", "both"}:
        raise ChatGPT2ApiImportError("导入目标只能是 legacy、new 或 both")
    return normalized  # type: ignore[return-value]


def _settings(target: _ImportTarget) -> tuple[str, str, int]:
    if target == "new":
        prefix = "CHATGPT2API_NEW_"
        timeout_key = "CHATGPT2API_NEW_IMPORT_TIMEOUT"
    else:
        prefix = "CHATGPT2API_"
        timeout_key = "CHATGPT2API_IMPORT_TIMEOUT"
    default_base_url = DEFAULT_BASE_URL if target == "legacy" else ""
    base_url = str(env_value(f"{prefix}BASE_URL", default_base_url, "str") or "").strip().rstrip("/")
    auth_key = str(env_value(f"{prefix}AUTH_KEY", "", "str") or "").strip()
    timeout = max(3, min(60, env_int(timeout_key, 20)))
    label = _TARGET_LABELS[target]
    if not base_url:
        raise ChatGPT2ApiImportError(f"请先在配置页填写 {label}ChatGPT2API 地址")
    if not auth_key:
        raise ChatGPT2ApiImportError(f"请先在配置页填写 {label}ChatGPT2API 管理员密钥")
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


def build_payloads(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads = [payload for account in accounts if (payload := build_account_payload(account)) is not None]
    if not payloads:
        raise ChatGPT2ApiImportError("所选账号没有可导入的 access_token")
    return payloads


def _response_error(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        data = {}
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("error") or detail.get("message")
    message = str(detail or (data.get("error") if isinstance(data, dict) else "") or f"HTTP {response.status_code}")
    return message[:300]


def _normalize_counts(response_data: Any, *, submitted: int) -> dict[str, int]:
    root = response_data if isinstance(response_data, dict) else {}
    nested = root.get("result") if isinstance(root.get("result"), dict) else {}
    errors = nested.get("errors") or root.get("errors")
    return {
        "submitted": submitted,
        "added": max(0, int(nested.get("added") or root.get("added") or 0)),
        "skipped": max(0, int(nested.get("skipped") or root.get("skipped") or 0)),
        "errors": len(errors) if isinstance(errors, list) else 0,
    }


def _post_accounts(payloads: list[dict[str, Any]], *, target: _ImportTarget) -> dict[str, Any]:
    base_url, auth_key, timeout = _settings(target)
    label = _TARGET_LABELS[target]
    try:
        response = requests.post(
            f"{base_url}/api/accounts",
            headers={"Authorization": f"Bearer {auth_key}"},
            json={"accounts": payloads},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ChatGPT2ApiImportError(f"无法连接 {label}ChatGPT2API：{exc}") from exc
    if not response.ok:
        raise ChatGPT2ApiImportError(f"{label}ChatGPT2API 导入失败：{_response_error(response)}")
    try:
        response_data = response.json()
    except ValueError:
        response_data = {}
    return {
        "target": target,
        "label": label,
        **_normalize_counts(response_data, submitted=len(payloads)),
    }


def probe_version(target: _ImportTarget) -> dict[str, Any]:
    """读取免鉴权版本号，用于保存配置后确认地址指向的是预期版本。"""
    base_url, _, timeout = _settings(target)
    label = _TARGET_LABELS[target]
    try:
        response = requests.get(f"{base_url}/version", timeout=timeout)
    except requests.RequestException as exc:
        return {"target": target, "label": label, "ok": False, "version": "", "error": str(exc)}
    try:
        data = response.json() if response.ok else {}
    except ValueError:
        data = {}
    return {
        "target": target,
        "label": label,
        "ok": bool(response.ok),
        "version": str(data.get("version") or "") if isinstance(data, dict) else "",
        "error": "" if response.ok else f"HTTP {response.status_code}",
    }


def probe_targets(target: str | None = None) -> list[dict[str, Any]]:
    resolved = resolve_target(target)
    selected = ["new"] if resolved == "new" else (["legacy"] if resolved == "legacy" else ["legacy", "new"])
    return [probe_version(target) for target in selected]  # type: ignore[arg-type]


def import_accounts_to_target(
    accounts: list[dict[str, Any]],
    *,
    target: str | None = None,
) -> dict[str, Any]:
    payloads = build_payloads(accounts)
    resolved = resolve_target(target)
    selected = ["new"] if resolved == "new" else (["legacy"] if resolved == "legacy" else ["legacy", "new"])
    results: list[dict[str, Any]] = []
    target_errors: list[dict[str, str]] = []
    for selected_target in selected:
        try:
            results.append(_post_accounts(payloads, target=selected_target))  # type: ignore[arg-type]
        except ChatGPT2ApiImportError as exc:
            label = _TARGET_LABELS[selected_target]
            target_errors.append({"target": selected_target, "label": label, "error": str(exc)})

    if not results:
        raise ChatGPT2ApiImportError(target_errors[-1]["error"] if target_errors else "导入失败")

    counts = {
        key: sum(int(result.get(key) or 0) for result in results)
        for key in ("submitted", "added", "skipped", "errors")
    }
    return {
        **counts,
        "targets": results,
        "target_errors": target_errors,
        "partial_success": bool(target_errors),
    }


def import_accounts(accounts: list[dict[str, Any]], *, target: str | None = None) -> dict[str, Any]:
    return import_accounts_to_target(accounts, target=target)
