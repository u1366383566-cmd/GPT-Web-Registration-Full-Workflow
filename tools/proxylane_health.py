# -*- coding: utf-8 -*-
"""ProxyLane 代理池健康检查与排序。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe tools\\proxylane_health.py
    .venv\\Scripts\\python.exe tools\\proxylane_health.py --rounds 15 --candidates 10001 10000 10004

对 PROXY_POOL 里每个代理按 OpenAI 注册真实用到的几个域名做压测，输出
「成功数 / 失败数 / 平均延迟 / P90」，并按 (失败数, 平均延迟) 排序。

只做只读网络探测，不创建账号、不下单、不消耗 Remail 额度；流量消耗约
每 10 次请求 100 KB 量级（只取响应头级别的小页面）。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.proxy as proxy_cfg  # noqa: E402

# 注册流程真正会打到的域名：auth 提交、chatgpt 页面、资源 CDN、sentinel。
PROBE_TARGETS = (
    "https://auth.openai.com/",
    "https://chatgpt.com/cdn-cgi/trace",
    "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
)


def _label(url: str) -> str:
    tail = url.rsplit("@", 1)[-1]
    return tail or "(empty)"


def probe_one(url: str, *, rounds: int, timeout: float) -> dict:
    latencies: list[float] = []
    failures: list[str] = []
    for _ in range(rounds):
        for target in PROBE_TARGETS:
            started = time.perf_counter()
            try:
                resp = requests.get(
                    target,
                    proxies={"http": url, "https": url},
                    timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                elapsed = time.perf_counter() - started
                # 403/401 属于「链路通、被 Cloudflare 正常拒绝」，算成功。
                if resp.status_code < 500:
                    latencies.append(elapsed)
                else:
                    failures.append(f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                failures.append(type(exc).__name__)
    total = len(latencies) + len(failures)
    avg = round(statistics.fmean(latencies), 3) if latencies else 0.0
    p90 = round(sorted(latencies)[max(0, int(len(latencies) * 0.9) - 1)], 3) if latencies else 0.0
    return {
        "proxy": _label(url),
        "ok": len(latencies),
        "fail": len(failures),
        "total": total,
        "avg": avg,
        "p90": p90,
        "errors": sorted(set(failures))[:4],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ProxyLane 代理池健康检查")
    parser.add_argument("--rounds", type=int, default=8, help="每个代理的轮数（每轮 3 个域名）")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--candidates",
        nargs="*",
        default=None,
        help="只测这些端口（例如 10001 10000）；默认测 PROXY_POOL 全部",
    )
    args = parser.parse_args()

    pool = list(proxy_cfg.PROXY_POOL)
    if args.candidates:
        wanted = {str(port) for port in args.candidates}
        pool = [u for u in pool if u.rsplit(":", 1)[-1] in wanted] or pool
    if not pool:
        print("PROXY_POOL 为空，先在 .env 或 WebUI 代理池里配置")
        return 2

    rows = [probe_one(url, rounds=args.rounds, timeout=args.timeout) for url in pool]
    rows.sort(key=lambda r: (r["fail"], r["avg"] or 99))

    print(f"{'代理':<48}{'成功':>6}{'失败':>6}{'平均':>8}{'P90':>8}  错误")
    for row in rows:
        print(
            f"{row['proxy']:<48}{row['ok']:>6}{row['fail']:>6}"
            f"{row['avg']:>7}s{row['p90']:>7}s  {','.join(row['errors'])}"
        )
    best = rows[0]
    print(f"\n最稳节点：{best['proxy']}（失败 {best['fail']}/{best['total']}，平均 {best['avg']}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
