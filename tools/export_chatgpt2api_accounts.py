# -*- coding: utf-8 -*-
"""
把注册库（turb.sqlite3）里的账号导出为「GPT网页额度反代旧版(chatgpt2api)」可导入的
accounts JSON 数组。

反代旧版导入接口 POST /api/accounts 接受:
    {"tokens": [...]} 或 {"accounts": [ {...}, ... ]}
accounts 里的条目只要求 access_token 必填，其余字段原样保留；
quota / limits_progress / success / fail 等运行时字段由反代导入后自动刷新填充，
因此这里只导出注册侧真正有数据的字段。

用法：
    python tools/export_chatgpt2api_accounts.py                # 全部账号 → 导出_反代账号.json
    python tools/export_chatgpt2api_accounts.py -o out.json    # 指定输出文件
    python tools/export_chatgpt2api_accounts.py --status ok    # 只导指定状态（如 ok / registered）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "turb.sqlite3"
DEFAULT_OUT = PROJECT_ROOT / "导出_反代账号.json"


def load_accounts(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT email, status, archived, payload FROM accounts ORDER BY id").fetchall()
    finally:
        conn.close()

    result = []
    for email, status, archived, payload_text in rows:
        try:
            payload = json.loads(payload_text or "{}")
        except json.JSONDecodeError:
            continue
        payload["_row_status"] = status or ""
        payload["_archived"] = bool(archived)
        result.append(payload)
    return result


def to_chatgpt2api_item(payload: dict) -> dict | None:
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return None
    email = str(payload.get("email") or "").strip()
    created_at = str(payload.get("created_at") or "").strip()
    # 反代的 created_at 是 "YYYY-MM-DD HH:MM:SS"，注册库是 ISO 带 T，做个兼容转换
    if "T" in created_at:
        try:
            created_at = datetime.fromisoformat(created_at).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    plan_type = str(payload.get("current_plan_type") or payload.get("plan_type") or "free").strip()
    return {
        "created_at": created_at,
        "access_token": access_token,
        "email": email,
        "user_id": str(payload.get("user_id") or "").strip(),
        "type": plan_type or "free",
        "status": "正常",
        "proxy": str(payload.get("proxy_used") or "").strip(),
        "source_type": "web",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出注册账号为 chatgpt2api(反代旧版) 可导入的 JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="注册库路径（默认 turb.sqlite3）")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT, help="输出 JSON 路径")
    parser.add_argument("--status", default="", help="按注册状态过滤（如 ok），留空不过滤")
    parser.add_argument("--include-archived", action="store_true", help="包含已归档账号")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"注册库不存在: {args.db}", file=sys.stderr)
        return 1

    accounts = load_accounts(args.db)
    total = len(accounts)
    items: list[dict] = []
    skipped_no_token = 0
    for payload in accounts:
        if args.status and payload["_row_status"] != args.status:
            continue
        if payload["_archived"] and not args.include_archived:
            continue
        item = to_chatgpt2api_item(payload)
        if item is None:
            skipped_no_token += 1
            continue
        items.append(item)

    args.output.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"注册库账号总数: {total}")
    print(f"已导出: {len(items)} 条 → {args.output}")
    if skipped_no_token:
        print(f"跳过（无 access_token）: {skipped_no_token} 条")
    if items:
        print(f"示例: {items[0]['email']}  plan={items[0]['type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
