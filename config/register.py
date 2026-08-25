# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 注册密码（OTP-only 流程已不需要，留作备用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 注册成功落库后是否自动查询套餐/Plus 资格。
# 关闭后不会在注册完成后立刻访问 backend-api/accounts/check，后续可在账号列表手动查询。
AUTO_PLAN_CHECK_AFTER_REGISTER = False

# ChatGPT2API 本地账号池导入：由 WebUI 账号页调用；管理员密钥仅保存在 .env。
CHATGPT2API_BASE_URL = "http://127.0.0.1:8021"
CHATGPT2API_AUTH_KEY = ""
CHATGPT2API_IMPORT_TIMEOUT = 20

# 注册成功并拿到 accessToken 后，在浏览器里随机停留一段时间再关闭连接。
# 格式：最小秒,最大秒。设为 "0,0" 表示不额外停留。
POST_REGISTER_DWELL_SECONDS_RANGE = "18,45"

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_NAME': 'str',
    'AUTO_PLAN_CHECK_AFTER_REGISTER': 'bool',
    'CHATGPT2API_BASE_URL': 'str',
    'CHATGPT2API_AUTH_KEY': 'str',
    'CHATGPT2API_IMPORT_TIMEOUT': 'int',
    'POST_REGISTER_DWELL_SECONDS_RANGE': 'str',
})
