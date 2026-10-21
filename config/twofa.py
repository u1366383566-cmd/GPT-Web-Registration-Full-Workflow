# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 2FA：
    True:  注册完成 → 拉新 OTP 邮件 → enroll TOTP → activate → 把 secret 写入 DB
    False: 跳过整个 2FA 流程，只保存 邮箱 + accessToken

关掉 2FA 不会影响账号可用性，仅意味着账号没有动态口令保护，且少收一封 OTP 邮件。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = False

# 2FA 网络代理模式：
#   saved = 优先使用账号保存的有效代理（无有效代理时回退代理池）
#   pool  = 忽略账号保存的代理，每次任务都从 PROXY_POOL 随机抽取
TWOFA_PROXY_MODE = "saved"

# 发起 reauth（CSRF + signin）时的临时网络错误重试。403 会先清理当前会话的
# 本地熔断，再按指数退避重试；业务类 4xx 不重试。
# OTP 提交也复用此次数/退避配置；只重试无业务错误码、无交互挑战的 403/429。
# OTP 重试复用同一会话/验证码；明确账号拒绝、挑战以及结果未知的网络超时不自动重放。
TWOFA_REAUTH_MAX_ATTEMPTS = 3
TWOFA_REAUTH_RETRY_DELAY = 3.0

# 2FA 后台队列。workers 是实际同时执行的账号数，修改后需重启进程以重建线程池。
TWOFA_WORKERS = 4
TWOFA_QUEUE_LIMIT = 200

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_2FA': 'bool',
    'TWOFA_PROXY_MODE': 'str',
    'TWOFA_REAUTH_MAX_ATTEMPTS': 'int',
    'TWOFA_REAUTH_RETRY_DELAY': 'float',
    'TWOFA_WORKERS': 'int',
    'TWOFA_QUEUE_LIMIT': 'int',
})
