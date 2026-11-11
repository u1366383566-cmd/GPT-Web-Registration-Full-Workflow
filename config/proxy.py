# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
import logging
import os
import random
import time
from urllib.parse import quote, urlparse

from config.env_loader import apply_env_overrides

logger = logging.getLogger(__name__)


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# 代理池使用的本地上游代理。填写后形成：本地上游 -> 代理池目标代理 -> ChatGPT；
# 留空则直接使用代理池中的目标代理，不启动链式中继。
PROXY_POOL_UPSTREAM_PROXY = ""

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；没有代理时才按 direct 运行
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = []

# 套餐查询 / Codex Agent Token 生成的上游代理。填写后形成：本地代理 -> 动态代理 -> ChatGPT。
# 例如 http://127.0.0.1:7897；留空则直接连接 PLAN_CHECK_PROXY。
PLAN_CHECK_UPSTREAM_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 3
PLAN_CHECK_RETRY_DELAY = 2.0

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 1.0
PLAN_CHECK_JITTER = 0.8


def _valid_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    """Normalize common proxy shorthand while preserving explicit URLs."""
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":", 3)
    if len(parts) == 4 and parts[0] and _valid_port(parts[1]) and parts[2] and parts[3]:
        host, port, username, password = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 4 and parts[0] and parts[1] and parts[2] and _valid_port(parts[3]):
        username, password, host, port = parts
        return f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    parsed = urlparse(f"//{text}")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.hostname and port:
        return f"{default_scheme}://{text}"
    return text


def normalize_proxy_list(values, default_scheme: str = "http") -> list[str]:
    """Normalize a multiline proxy list and omit empty entries."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [
        normalized
        for value in values
        if (normalized := normalize_proxy_url(value, default_scheme=default_scheme))
    ]


# 代理预检：注册前先探一下这个节点通不通，避免把一个已经抖掉的节点写进浏览器。
# 关闭方法：.env 里设 PROXY_PREFLIGHT_ENABLED=False
_PROXY_PREFLIGHT_TARGETS = (
    "https://chatgpt.com/cdn-cgi/trace",
    "https://auth-cdn.oaistatic.com/",
)
_PREFLIGHT_CACHE: dict[str, tuple[float, bool]] = {}
_PREFLIGHT_CACHE_TTL = 90.0
# 最近一次被选中的代理，以及本次进程内判定为「刚失败」的代理。
# 任务重试时会调用 exclude_proxy()，让下一次选择避开刚掉线的节点。
LAST_PICKED = ""
_EXCLUDED: set[str] = set()


def exclude_proxy(url: str) -> None:
    """把某个代理标记为暂时不可用（本次进程内有效，直到 restart）。"""
    if url:
        _EXCLUDED.add(url)
        logger.warning("[ProxyPool] 已暂时排除节点 %s", url.rsplit("@", 1)[-1])


def clear_exclusions() -> None:
    _EXCLUDED.clear()
    _PREFLIGHT_CACHE.clear()


def _preflight_enabled() -> bool:
    raw = str(os.getenv("PROXY_PREFLIGHT_ENABLED", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "n", "f")


def _preflight_timeout() -> float:
    try:
        return max(2.0, min(20.0, float(os.getenv("PROXY_PREFLIGHT_TIMEOUT", "") or 6.0)))
    except ValueError:
        return 6.0


def probe_proxy(url: str, timeout: float | None = None) -> bool:
    """探测单个代理能否跑到 OpenAI 的静态资源域；任一目标成功即视为可用。"""
    import requests

    limit = timeout if timeout is not None else _preflight_timeout()
    for target in _PROXY_PREFLIGHT_TARGETS:
        try:
            resp = requests.get(
                target,
                proxies={"http": url, "https": url},
                timeout=limit,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
        except Exception:
            continue
        if resp.status_code < 500:
            return True
    return False


def pick_proxy(preflight: bool | None = None) -> str:
    """从代理池里挑一个可用代理。

    代理池顺序即优先级：按顺序预检，返回第一个探测通过的节点；全部探测失败时
    退回池内第一条（并在日志里说明），不让注册因为预检直接失败。
    ``preflight=False`` 时不探测，直接按池内顺序取第一条。
    """
    pool = PROXY_POOL
    if not pool:
        return ""
    use_preflight = _preflight_enabled() if preflight is None else bool(preflight)

    candidates = [url for url in pool if url not in _EXCLUDED]
    if not candidates:
        # 全部被排除时解除排除，避免把自己锁死。
        _EXCLUDED.clear()
        candidates = list(pool)
    if not use_preflight:
        globals()["LAST_PICKED"] = candidates[0]
        return candidates[0]

    now = time.time()
    for url in candidates:
        cached = _PREFLIGHT_CACHE.get(url)
        if cached and now - cached[0] < _PREFLIGHT_CACHE_TTL:
            ok = cached[1]
        else:
            ok = probe_proxy(url)
            _PREFLIGHT_CACHE[url] = (now, ok)
        if ok:
            if url != pool[0]:
                logger.warning("[ProxyPool] 首选节点不可用，改用 %s", url.rsplit("@", 1)[-1])
            globals()["LAST_PICKED"] = url
            return url

    logger.warning(
        "[ProxyPool] 全部 %s 个节点预检未通过，仍使用 %s",
        len(candidates),
        candidates[0].rsplit("@", 1)[-1],
    )
    globals()["LAST_PICKED"] = candidates[0]
    return candidates[0]


# 兼容入口：进程启动时只做纯本地选择，不做网络预检，避免拖慢启动。
PROXY = random.choice(PROXY_POOL) if PROXY_POOL else ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PROXY_POOL_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'list_str_multiline',
    'PLAN_CHECK_UPSTREAM_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY_POOL = normalize_proxy_list(PROXY_POOL)
PLAN_CHECK_PROXY = normalize_proxy_list(PLAN_CHECK_PROXY)
# 归一化后的兼容入口：启动阶段不做网络预检，注册流程请调用 pick_proxy()。
PROXY = random.choice(PROXY_POOL) if PROXY_POOL else ""
