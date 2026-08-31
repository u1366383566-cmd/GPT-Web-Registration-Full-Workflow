# -*- coding: utf-8 -*-
"""
Remail (remail.aishop6.com) 邮箱池辅助工具。

功能：
  1. 批量下单购买邮箱（API Key 认证）
  2. 拉取已有订单，生成注册项目「通用 API 邮箱」导入行：邮箱----取件URL

用法：
  # 下单购买 2 个 chatgpt 项目的 @outlook.com 长效邮箱
  python tools/remail_import.py buy --api-key rk-xxxx --project-id 2 --suffix outlook.com --count 2

  # 拉取全部已交付订单，生成导入行（默认追加写入 用于注册的API邮箱.txt）
  python tools/remail_import.py collect --api-key rk-xxxx
  python tools/remail_import.py collect --api-key rk-xxxx --dry-run   # 只预览不写入

说明：
  - 取件 URL 形如 https://remail.aishop6.com/v1/pickup?email=xx&token=st_xx，
    注册项目取码时直接 GET 该地址，从 items[].verificationCode 提取验证码。
  - API Key 在 Remail「个人设置」页生成，以 rk- 开头。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "用于注册的API邮箱.txt"
DEFAULT_BASE = "https://remail.aishop6.com"

# 这些状态的订单还没有可用的交付邮箱
_SKIP_STATUS = {"pending_payment", "payment_failed", "refunded", "closed", "cancelled"}


def _request(base: str, path: str, *, api_key: str | None = None, query: dict | None = None,
             payload: dict | None = None, idempotency_key: str | None = None, timeout: int = 30) -> dict:
    url = base.rstrip("/") + path
    if query:
        url += "?" + urlencode(query)
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = Request(url, data=body, headers=headers, method="POST" if payload is not None else "GET")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def _iter_orders(base: str, api_key: str) -> list[dict]:
    """翻页拉取订单列表，兼容 items/数组 两种返回形态。"""
    orders: list[dict] = []
    page = 1
    while True:
        data = None
        last_err: Exception | None = None
        for query in ({"page": page, "pageSize": 50}, {"page": page, "page_size": 50}, {"page": page}):
            try:
                data = _request(base, "/v1/open/orders", api_key=api_key, query=query)
                break
            except Exception as exc:  # 参数名不兼容时逐个尝试
                last_err = exc
                data = None
        if data is None:
            raise SystemExit(f"查询订单失败：{last_err}")
        items = data.get("items") if isinstance(data, dict) else data
        if isinstance(data, dict) and not items and isinstance(data.get("list"), list):
            items = data["list"]
        if not items:
            break
        orders.extend(item for item in items if isinstance(item, dict))
        total = data.get("total") if isinstance(data, dict) else None
        try:
            if total is not None and len(orders) >= int(total):
                break
        except (TypeError, ValueError):
            pass
        if len(items) < 50:
            break
        page += 1
        if page > 50:
            break
    return orders


def cmd_buy(args: argparse.Namespace) -> None:
    idem = args.idempotency_key or str(uuid.uuid4())
    data = _request(
        args.base,
        "/v1/open/orders",
        api_key=args.api_key,
        query={"serviceMode": args.service_mode},
        payload={"projectId": args.project_id, "emailSuffix": args.suffix},
        idempotency_key=idem,
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print("\n提示：用 collect 子命令把已交付订单生成导入行。")


def cmd_collect(args: argparse.Namespace) -> None:
    orders = _iter_orders(args.base, args.api_key)
    lines: list[str] = []
    seen: set[str] = set()
    for order in orders:
        email = str(order.get("deliveryEmail") or "").strip()
        token = str(order.get("serviceToken") or "").strip()
        status = str(order.get("status") or "").strip().lower()
        if not email or not token:
            continue
        if status in _SKIP_STATUS:
            continue
        if email in seen:
            continue
        seen.add(email)
        pickup_url = f"{args.base.rstrip('/')}/v1/pickup?{urlencode({'email': email, 'token': token})}"
        lines.append(f"{email}----{pickup_url}")
        note = f"  [status={status or '未知'}]"
        print(f"{email}----{pickup_url[:80]}...{note}")
    if not lines:
        print("没有找到可用的已交付订单（先去购买，或确认订单状态）。")
        return
    if args.dry_run:
        print(f"\n共 {len(lines)} 条（dry-run，未写入文件）。")
        return
    existing = ""
    if DEFAULT_OUT.exists():
        existing = DEFAULT_OUT.read_text(encoding="utf-8", errors="replace")
    with DEFAULT_OUT.open("a", encoding="utf-8") as fh:
        for line in lines:
            if line in existing:
                continue
            fh.write(line + "\n")
    print(f"\n共追加 {len(lines)} 条到 {DEFAULT_OUT}")
    print("下一步：WebUI「邮箱池」页导入该文件（或粘贴），并确认 EMAIL_SOURCE 包含 generic_api。")


def main() -> None:
    parser = argparse.ArgumentParser(description="Remail 邮箱池辅助工具")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"API 基址（默认 {DEFAULT_BASE}）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_buy = sub.add_parser("buy", help="下单购买邮箱")
    p_buy.add_argument("--api-key", required=True, help="rk- 开头的 API Key")
    p_buy.add_argument("--project-id", type=int, required=True, help="项目 ID（chatgpt 见 /v1/open/projects）")
    p_buy.add_argument("--suffix", default="outlook.com", help="邮箱后缀（默认 outlook.com）")
    p_buy.add_argument("--count", type=int, default=1, help="购买数量（循环下单）")
    p_buy.add_argument("--service-mode", default="purchase", choices=["purchase", "code"], help="purchase=长效, code=短效")
    p_buy.add_argument("--idempotency-key", default="", help="幂等键（默认随机）")

    p_col = sub.add_parser("collect", help="拉取订单生成导入行")
    p_col.add_argument("--api-key", required=True, help="rk- 开头的 API Key")
    p_col.add_argument("--dry-run", action="store_true", help="只预览不写入文件")

    args = parser.parse_args()
    if args.cmd == "buy":
        for i in range(max(1, args.count)):
            if i:
                args.idempotency_key = str(uuid.uuid4())
            print(f"--- 下单 {i + 1}/{args.count} ---")
            cmd_buy(args)
    else:
        cmd_collect(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
