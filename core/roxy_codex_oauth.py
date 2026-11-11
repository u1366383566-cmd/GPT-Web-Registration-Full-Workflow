# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器执行 Codex OAuth 授权。"""
from __future__ import annotations

import logging
import random
import time
from contextvars import ContextVar
from urllib.parse import urlparse

from config import roxybrowser as _roxy_cfg
from core.email_provider import wait_for_otp
from core.humanize import delay as human_delay
from core import sms_provider
from core.openai_auth import AccountUnusableError, detect_account_unusable_response_body
from core.roxybrowser_client import RoxyBrowserClient
from core import codex_oauth as _codex_proto
from core.roxy_registration import (
    _build_driver,
    _center_browser_window,
    _click_any,
    _click_continue,
    _find_any,
    _maybe_accept,
    _human_click,
    _human_type_text,
    _type_any,
    _type_email_address,
    _submit_email_step,
    _click_email_entry_option,
    _type_otp,
    _clear_otp_inputs,
    _email_otp_page_state,
    _is_email_verification_page,
    _is_login_password_page,
    _click_passwordless_signup_if_present,
)

_base_logger = logging.getLogger(__name__)
_CODEX_BROWSER_KIND: ContextVar[str] = ContextVar("codex_browser_kind", default="Roxy")


def _codex_prefix() -> str:
    return f"[Codex][{_CODEX_BROWSER_KIND.get()}]"


def _codex_driver_name() -> str:
    return _CODEX_BROWSER_KIND.get()


def _detect_browser_kind(opened=None) -> str:
    try:
        raw = getattr(opened, "raw", None) or {}
        if isinstance(raw, dict) and str(raw.get("driver") or "").lower().startswith("cloak"):
            return "Cloak"
    except Exception:
        pass
    return "Roxy"


class _CodexLogger:
    """把流程内部统一占位前缀替换成当前真实浏览器类型。"""
    def __init__(self, base):
        self._base = base

    def _msg(self, msg):
        return str(msg).replace("[Codex][Browser]", _codex_prefix())

    def debug(self, msg, *args, **kwargs):
        return self._base.debug(self._msg(msg), *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        return self._base.info(self._msg(msg), *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        return self._base.warning(self._msg(msg), *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        return self._base.error(self._msg(msg), *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        return self._base.exception(self._msg(msg), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base, name)


logger = _CodexLogger(_base_logger)


def _is_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.port == 1455
        and parsed.path == "/auth/callback"
    )


def _extract_callback_url_from_page(driver) -> str:
    """从当前页面提取 OAuth callback URL。

    浏览器跳转到 http://localhost:1455/auth/callback?... 时，本地没有服务监听会显示
    chrome-error://chromewebdata/。地址栏可能变成 chrome-error，但 Chromium 的
    performance navigation entry 仍保留原始 callback URL，可直接提取后提交 CPA。
    """
    try:
        current = str(driver.current_url or "")
        if _is_callback_url(current):
            return current
    except Exception:
        pass
    try:
        urls = driver.execute_script(r"""
        const out = [];
        const push = v => { if (v && typeof v === 'string') out.push(v); };
        try { push(location.href); } catch (e) {}
        try { push(document.URL); } catch (e) {}
        try { push(document.documentURI); } catch (e) {}
        try { for (const e of performance.getEntriesByType('navigation')) push(e.name); } catch (e) {}
        try { for (const e of performance.getEntries()) push(e.name); } catch (e) {}
        return [...new Set(out)];
        """) or []
        for url in urls:
            if _is_callback_url(str(url)):
                logger.info("[Codex][Browser] 已从浏览器性能记录提取 callback URL：%s", str(url)[:160])
                return str(url)
    except Exception as exc:
        logger.debug("[Codex][Browser] 从页面提取 callback URL 失败：%s", exc)
    return ""


def _extract_callback_url_from_any_window(driver) -> str:
    found = _extract_callback_url_from_page(driver)
    if found:
        return found
    try:
        for handle in list(getattr(driver, "window_handles", []) or []):
            try:
                driver.switch_to.window(handle)
                found = _extract_callback_url_from_page(driver)
                if found:
                    return found
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _wait_for_callback(driver, timeout: int | None = None) -> str:
    end = time.time() + (timeout or int(_roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT))
    last_url = ""
    while time.time() < end:
        try:
            current = str(driver.current_url or "")
            if current != last_url:
                logger.debug("[Codex][Browser] 当前 URL: %s", current)
                last_url = current
            callback = _extract_callback_url_from_any_window(driver)
            if callback:
                return callback
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"等待 Codex callback 超时，最后 URL={last_url}")


def _click_if_present(driver, selectors: list[str], timeout: int = 3) -> bool:
    try:
        _click_any(driver, selectors, timeout=timeout)
        return True
    except Exception:
        return False


def _maybe_click_passwordless_after_email(driver, email: str, timeout: int = 18) -> None:
    """
    Codex OAuth 提交邮箱后也可能跳到 /log-in/password 或 /create-account/password。
    优先点击“使用一次性验证码/one-time code”入口，进入邮箱验证码页。
    """
    end = time.time() + timeout
    last_url = ""
    clicked = False
    while time.time() < end:
        try:
            if _is_email_verification_page(driver):
                if clicked:
                    logger.info("[Codex][Browser] 一次性验证码入口已进入邮箱验证码页")
                return
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] 提交邮箱后检测密码/OTP 跳转：url=%s", url or "-")
                last_url = url
            lower = url.lower()
            if any(x in lower for x in ("phone", "workspace", "consent", "authorize", "localhost:1455")):
                return
            if "/password" in lower or "auth.openai.com" in lower:
                result = _click_passwordless_signup_if_present(driver)
                if result.get("ok"):
                    clicked = True
                    logger.info("[Codex][Browser] 已点击一次性验证码入口：email=%s detail=%s", email, result)
                    human_delay("form")
                    continue
        except Exception as exc:
            logger.debug("[Codex][Browser] 密码页一次性验证码入口探测失败：%s", str(exc)[:140])
        time.sleep(0.5)
    if clicked:
        logger.info("[Codex][Browser] 已点击一次性验证码入口，未立即检测到 OTP 页，继续后续 OTP 轮询")


def _wait_for_otp_input(driver, timeout: int = 30) -> None:
    """验证码已收到但 OTP 输入框可能尚未出现（点完一次性验证码后常有中间页/延迟渲染）。

    等待期间若仍停留在登录密码页，则补点一次性验证码入口（最多 2 次、间隔 6s）；
    超时仍未出现时打印页面状态便于定位。
    """
    end = time.time() + timeout
    passwordless_retries = 0
    while time.time() < end:
        if _is_email_verification_page(driver):
            return
        if _is_login_password_page(driver) and passwordless_retries < 2:
            passwordless_retries += 1
            result = _click_passwordless_signup_if_present(driver)
            if result.get("ok"):
                logger.info("[Codex][Browser] 仍停留登录密码页，补点一次性验证码入口：%s", result.get("reason"))
                human_delay("form")
            time.sleep(6)
            continue
        time.sleep(0.8)
    state = _email_otp_page_state(driver)
    logger.warning(
        "[Codex][Browser] 等待 OTP 输入框超时，页面 url=%s inputs=%s buttons=%s 文本前300字=%s",
        str(state.get("url") or ""),
        len(state.get("inputs") or []),
        [(b.get("text") or "")[:24] for b in (state.get("buttons") or [])][:8],
        str(state.get("text") or "")[:300],
    )
    raise RuntimeError("等待 OTP 输入框超时，页面未出现验证码输入框")


def _account_password_for_email(email: str) -> str:
    try:
        return _codex_proto._account_registration_password(email)
    except Exception:
        return ""


def _account_totp_code_for_email(email: str) -> str:
    try:
        return _codex_proto._account_totp_code(email)
    except Exception:
        return ""


def _is_mfa_challenge_page(driver) -> bool:
    try:
        url = str(driver.current_url or "").lower()
        if "/mfa-challenge/" in url or "/mfa-challenge" in url:
            return True
        state = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const form = [...document.querySelectorAll('form')].find(f => /\/mfa-challenge/i.test(f.getAttribute('action') || ''));
        const input = form ? [...form.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[maxlength="6"]')].find(visible) : null;
        return {ok: !!(form && input), url: location.href};
        """) or {}
        return bool(state.get("ok"))
    except Exception:
        return False


def _fill_mfa_challenge_if_present(driver, email: str, timeout: int = 15) -> bool:
    """如果当前进入 MFA challenge 页面，自动填入账号 TOTP 并提交。"""
    code = _account_totp_code_for_email(email)
    if not code:
        return False
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not _is_mfa_challenge_page(driver):
                time.sleep(0.4)
                continue
            result = driver.execute_script(r"""
            const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
              && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
              && !el.disabled && !el.readOnly;
            const form = [...document.querySelectorAll('form')].find(f => /\/mfa-challenge/i.test(f.getAttribute('action') || ''));
            if (!form) return {ok:false, reason:'missing_form'};
            const input = [...form.querySelectorAll('input[name="code"], input[autocomplete="one-time-code"], input[maxlength="6"]')].find(visible);
            if (!input) return {ok:false, reason:'missing_code_input'};
            const button = [...form.querySelectorAll('button[type="submit"], button[data-dd-action-name="Continue"], button')].find(visible);
            if (!button) return {ok:false, reason:'missing_submit'};
            return {ok:true, input, button};
            """) or {}
            if not result.get("ok"):
                time.sleep(0.4)
                continue
            _human_type_text(driver, result.get("input"), code, clear=True)
            human_delay("otp_input")
            _human_click(driver, result.get("button"), label="codex_mfa_submit")
            logger.info("[Codex][Browser] 已填写并提交 MFA 验证码：%s", email)
            wait_end = time.time() + 12
            while time.time() < wait_end:
                if not _is_mfa_challenge_page(driver):
                    return True
                time.sleep(0.4)
            return True
        except Exception as exc:
            logger.debug("[Codex][Browser] MFA challenge 处理失败：%s", str(exc)[:160])
            time.sleep(0.5)
    return False


def _fill_login_password_if_present(driver, email: str, timeout: int = 18) -> str | None:
    """Codex OAuth 若账号有密码，优先在登录密码页输入密码。返回 next_step / email_otp / None。"""
    password = _account_password_for_email(email)
    if not password:
        return None
    end = time.time() + timeout
    while time.time() < end:
        if _is_email_verification_page(driver):
            return "email_otp"
        if not _is_login_password_page(driver):
            time.sleep(0.4)
            continue
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="current-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        buttons[0].el.scrollIntoView({block:'center'});
        return {ok:true, reason:'password_targets', input, button: buttons[0].el};
        """) or {}
        if not result.get("ok"):
            logger.info("[Codex][Browser] 登录密码页未找到输入/提交按钮：%s", result)
            time.sleep(0.5)
            continue
        _human_type_text(driver, result.get("input"), password, clear=True)
        human_delay("form", minimum=2.0, maximum=3.6)
        _human_click(driver, result.get("button"), label="codex_password_submit")
        logger.info("[Codex][Browser] 已填写并提交登录密码：%s", email)
        wait_end = time.time() + 12
        while time.time() < wait_end:
            if _is_mfa_challenge_page(driver):
                _fill_mfa_challenge_if_present(driver, email, timeout=15)
                return "next_step"
            if _is_email_verification_page(driver):
                return "email_otp"
            if not _is_login_password_page(driver):
                return "next_step"
            time.sleep(0.5)
        return "next_step"
    return None


def _fill_email_and_otp(driver, email: str, otp_provider, auth_url: str) -> None:
    otp_after_ts = time.time()
    logger.info("[Codex][Browser] 打开授权地址")
    logger.info("[Codex][Browser] 完整授权地址: %s", auth_url)
    driver.get(auth_url)
    human_delay("navigate")
    logger.info("[Codex][Browser] 授权页加载完成，检查是否需要邮箱登录")
    _maybe_accept(driver)

    # 可能已经处于账号选择/授权页；如果有邮箱输入框则完整登录。
    # 非日本出口时按钮文案/顺序会变，不能按可见文字点“继续”，否则可能误点 Google。
    try:
        _type_email_address(driver, email, timeout=12)
        logger.info("[Codex][Browser] 已填写邮箱：%s", email)
        human_delay("form")
        _submit_email_step(driver)
        logger.info("[Codex][Browser] 已提交邮箱，等待邮箱 OTP 页面")
        pw_result = _fill_login_password_if_present(driver, email, timeout=18)
        if pw_result == "next_step":
            if _is_mfa_challenge_page(driver):
                _fill_mfa_challenge_if_present(driver, email, timeout=15)
            logger.info("[Codex][Browser] 账号已用密码完成登录，直接进入后续步骤")
            return
        if pw_result == "email_otp":
            logger.info("[Codex][Browser] 密码登录后仍进入邮箱 OTP 页面")
        else:
            _maybe_click_passwordless_after_email(driver, email, timeout=18)
    except Exception as exc:
        logger.info("[Codex][Browser] 未检测到邮箱输入框，可能已登录或进入下一步：%s", str(exc)[:120])
        return

    # 提交邮箱后不再执行任何全局“继续/授权/分支”兜底点击；后续只等待验证码页。
    # 避免页面已进入 OAuth consent 时误点授权按钮。

    used_codes: set[str] = set()
    max_otp_attempts = 3

    def _restart_email_otp_flow(reason: str) -> None:
        """Codex Auth 上直接点 resend 可能触发服务端 500；这里改为重新打开授权地址并提交邮箱。"""
        nonlocal otp_after_ts
        logger.info("[Codex][Browser] 重新触发邮箱 OTP：%s", reason)
        otp_after_ts = time.time()
        driver.get(auth_url)
        human_delay("navigate")
        _maybe_accept(driver)
        try:
            _type_email_address(driver, email, timeout=12)
            human_delay("form")
            _submit_email_step(driver)
            logger.info("[Codex][Browser] 已重新提交邮箱触发 OTP")
            pw_result = _fill_login_password_if_present(driver, email, timeout=12)
            if pw_result == "next_step":
                if _is_mfa_challenge_page(driver):
                    _fill_mfa_challenge_if_present(driver, email, timeout=15)
                logger.info("[Codex][Browser] 重新提交邮箱后已用密码完成登录，进入后续步骤")
                return
            if pw_result != "email_otp":
                _maybe_click_passwordless_after_email(driver, email, timeout=12)
        except Exception as exc:
            # 如果重进授权地址后已经停在验证码/下一步页面，就不要再强行提交。
            if not _is_email_verification_page(driver):
                logger.warning("[Codex][Browser] 重新提交邮箱失败，继续按当前页面轮询：%s", str(exc)[:180])
            else:
                logger.info("[Codex][Browser] 重开授权后已在邮箱 OTP 页面")
        human_delay("api")

    for otp_attempt in range(1, max_otp_attempts + 1):
        logger.info("[Codex][Browser] 等待邮箱 OTP：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
        try:
            code = _wait_for_fresh_email_otp(
                otp_provider,
                email,
                after_ts=otp_after_ts,
                used_codes=used_codes,
                timeout=90,
            )
        except Exception as exc:
            if otp_attempt >= max_otp_attempts:
                raise
            logger.warning(
                "[Codex][Browser] 一直未收到邮箱 OTP，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                otp_attempt + 1,
                max_otp_attempts,
                type(exc).__name__,
                str(exc)[:180],
            )
            _restart_email_otp_flow("等待验证码超时，避免点击 resend 导致 500")
            continue
        used_codes.add(str(code))
        logger.info("[Codex][Browser] 邮箱 OTP 收到：%s", code)
        _wait_for_otp_input(driver, timeout=30)
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        logger.info("[Codex][Browser] 已填写邮箱 OTP")
        human_delay("otp_input")
        _install_email_otp_validate_hook(driver)
        clicked = _click_if_present(driver, [
            "button[type='submit']",
            "//button[contains(., 'Continue')]",
            "//button[contains(., '继续')]",
            "//button[contains(., 'Verify')]",
            "//button[contains(., '验证')]",
        ], timeout=8)
        if clicked:
            logger.info("[Codex][Browser] 已提交邮箱 OTP，等待后续授权/手机号页面")
        else:
            logger.info("[Codex][Browser] 未找到显式提交按钮，继续等待页面状态")

        outcome = _wait_after_email_otp_submit(driver, timeout=45)
        logger.info("[Codex][Browser] 邮箱 OTP 提交后状态：%s", outcome)
        if _is_mfa_challenge_page(driver):
            _fill_mfa_challenge_if_present(driver, email, timeout=15)
            return
        if outcome == "accepted":
            return
        if str(outcome).startswith("deactivated:"):
            error_code = str(outcome).split(":", 1)[1] or "account_deactivated"
            raise AccountUnusableError(f"账号已废（{error_code}）", error_code=error_code)

        if otp_attempt >= max_otp_attempts:
            raise RuntimeError("Codex 邮箱验证码连续错误/过期，已达到最大重试次数")

        logger.warning(
            "[Codex][Browser] 邮箱验证码错误/过期或页面未跳转，准备重新发送并重新获取最新验证码（%s/%s）",
            otp_attempt + 1,
            max_otp_attempts,
        )
        _restart_email_otp_flow("验证码错误/过期或页面未跳转，避免点击 resend 导致 500")



def _wait_for_fresh_email_otp(otp_provider, email: str, after_ts: float, used_codes: set[str] | None = None, timeout: int = 90) -> str:
    """获取一个未提交过的邮箱 OTP。

    通用 API 邮箱的取码接口有时会先返回缓存旧码；验证码错误后重发时，
    这里会拒绝复用已失败的 code，持续轮询直到出现新 code 或超时。
    """
    used_codes = {str(x) for x in (used_codes or set()) if x}
    end = time.time() + timeout
    last_code = ""
    while True:
        code = str(otp_provider(email, after_ts=after_ts) or "").strip()
        if code and code not in used_codes:
            return code
        last_code = code or last_code
        remaining = int(end - time.time())
        if remaining <= 0:
            raise RuntimeError(f"等待新的邮箱验证码超时，取码接口仍返回已失败验证码：{last_code or '-'}")
        logger.warning(
            "[Codex][Browser] 取码接口仍返回已提交过的旧 OTP=%s，继续等待最新验证码（剩余 %ss）",
            last_code or "-",
            remaining,
        )
        time.sleep(min(5, max(1, remaining)))


def _install_email_otp_validate_hook(driver) -> None:
    """
    在页面内 hook fetch/XHR，捕获 email-otp/validate 的接口响应体。

    指纹浏览器不能像纯协议模式一样直接拿 requests.Response，因此在提交邮箱 OTP 前
    注入此 hook，后续只读取接口 JSON error.code，不靠页面文字判断废号。
    """
    script = r"""
    (() => {
      window.__codexEmailOtpValidateResponses = [];
      if (window.__codexEmailOtpValidateHooked) return true;
      window.__codexEmailOtpValidateHooked = true;
      const hit = (url) => String(url || '').includes('/api/accounts/email-otp/validate');
      const save = (url, status, body) => {
        try {
          if (!hit(url)) return;
          window.__codexEmailOtpValidateResponses.push({
            url: String(url || ''),
            status: Number(status || 0),
            body: String(body || '').slice(0, 2000),
            ts: Date.now(),
          });
        } catch (e) {}
      };
      const origFetch = window.fetch;
      if (origFetch) {
        window.fetch = async function(input, init) {
          const resp = await origFetch.apply(this, arguments);
          try {
            const url = (typeof input === 'string') ? input : (input && input.url);
            if (hit(url)) {
              resp.clone().text().then(t => save(url, resp.status, t)).catch(() => {});
            }
          } catch (e) {}
          return resp;
        };
      }
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url) {
        this.__codexOtpValidateUrl = url;
        return origOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function() {
        try {
          this.addEventListener('loadend', function() {
            try {
              if (hit(this.__codexOtpValidateUrl)) save(this.__codexOtpValidateUrl, this.status, this.responseText);
            } catch (e) {}
          });
        } catch (e) {}
        return origSend.apply(this, arguments);
      };
      return true;
    })();
    """
    try:
        driver.execute_script(script)
    except Exception as exc:
        logger.debug("[Codex][Browser] 注入 email-otp/validate 响应 hook 失败：%s", exc)


def _read_email_otp_validate_dead_code(driver) -> str:
    try:
        rows = driver.execute_script("return window.__codexEmailOtpValidateResponses || [];") or []
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        code = detect_account_unusable_response_body(str(row.get("body") or ""))
        if code:
            logger.warning(
                "[Codex][Browser] email-otp/validate 响应识别账号已废：code=%s status=%s",
                code,
                row.get("status"),
            )
            return code
    return ""


# 邮箱验证码页判断复用 roxy_registration 的强版本（URL + 输入框属性识别，
# 且明确排除 /log-in/password），不使用本地弱化版，避免点完一次性验证码后
# 页面已渲染 OTP 输入框却因 URL 不含 email-verification 而识别失败。

def _wait_after_email_otp_submit(driver, timeout: int = 45) -> str:
    """
    提交邮箱 OTP 后等待页面离开 /email-verification。

    返回：
      - accepted：已离开邮箱验证码页 / 进入手机号页 / 进入 callback；
      - invalid：页面明确报错、输入框标红，或长时间停留验证码页。
    """
    end = time.time() + timeout
    last_url = ""
    last_log = 0.0
    while time.time() < end:
        try:
            dead_code = _read_email_otp_validate_dead_code(driver)
            if dead_code:
                return f"deactivated:{dead_code}"
            url = str(driver.current_url or "")
            if url != last_url:
                logger.info("[Codex][Browser] 邮箱 OTP 后等待跳转：url=%s", url)
                last_url = url
            if _is_callback_url(url):
                return "accepted"
            if _has_strict_add_phone_form(driver) or _is_phone_code_page(driver):
                return "accepted"
            # 已经离开 email-verification，交给后续授权/手机号/consent 流程处理。
            if "email-verification" not in url.lower():
                return "accepted"

            state = _email_otp_page_state(driver)
            invalid = any(str(i.get("ariaInvalid") or "").lower() == "true" for i in (state.get("inputs") or []))
            errors = [str(x) for x in (state.get("errors") or []) if str(x).strip()]
            body_text = str(state.get("text") or "").lower()
            error_hit = any(x in body_text for x in (
                "invalid code", "incorrect code", "wrong code", "expired",
                "验证码错误", "验证码无效", "验证码已过期", "コードが正しく", "無効", "期限",
            ))
            if invalid or errors or error_hit:
                logger.warning(
                    "[Codex][Browser] 邮箱 OTP 提交后检测到错误/仍需验证码：errors=%s invalid=%s url=%s",
                    errors[:3],
                    invalid,
                    url,
                )
                return "invalid"

            if time.time() - last_log > 6:
                logger.info("[Codex][Browser] 邮箱 OTP 后仍在 email-verification，继续等待页面自动跳转")
                last_log = time.time()
        except Exception:
            pass
        time.sleep(0.5)
    logger.warning("[Codex][Browser] 邮箱 OTP 后等待跳转超时，当前 url=%s，按验证码无效/过期处理", getattr(driver, "current_url", ""))
    return "invalid"


def _phone_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        // React-Aria segmented-control 的 radio input 可能是视觉隐藏的；不能按 visible 过滤，
        // 否则会误以为页面没有 SMS/WhatsApp radio。
        const radios = [...document.querySelectorAll('input[type=radio]')].map(el => ({
          name: el.name || '', value: el.value || '', checked: !!el.checked, id: el.id || ''
        }));
        const inputs = [...document.querySelectorAll('input,select,textarea')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', placeholder: el.getAttribute('placeholder') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const bodyText = (document.body?.innerText || '').slice(0, 1200);
        return {url: location.href, radios, inputs, forms, bodyText};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _select_sms_channel_or_raise(driver) -> None:
    state = _phone_page_state(driver)
    radios = state.get('radios') or []
    # 如果存在 WhatsApp 且没有 SMS/text 可选，当前接码平台无法读取 WhatsApp，直接换号。
    has_whatsapp = any('whatsapp' in str(r.get('value','')).lower().replace(' ', '') for r in radios)
    has_sms = any(str(r.get('value','')).lower() in ('sms', 'text', 'text_message', 'text-message') for r in radios)
    if has_whatsapp and not has_sms:
        raise RuntimeError(f"whatsapp_channel: 页面仅提供 WhatsApp 通道 state={state}")
    # 选择 SMS/text radio。不能只 sms.click()：React-Aria 的分段控件里 radio 可能是
    # 视觉隐藏的，直接点 input 偶发不改变真实 React 状态，最终仍按 WhatsApp 发送。
    selected = driver.execute_script(r"""
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''))
      || document;
    const radios = [...form.querySelectorAll('input[type=radio]')];
    const norm = v => String(v || '').toLowerCase().replace(/[\s_-]+/g, '');
    const sms = radios.find(el => ['sms','text','textmessage'].includes(norm(el.value)));
    const wa = radios.find(el => norm(el.value).includes('whatsapp'));
    const hiddenChannel = form.querySelector('input[name="channel"]');
    if (!sms && hiddenChannel) {
      hiddenChannel.value = 'sms';
      hiddenChannel.dispatchEvent(new Event('input', {bubbles:true}));
      hiddenChannel.dispatchEvent(new Event('change', {bubbles:true}));
      return {ok:true, method:'hidden-only', channel:hiddenChannel.value};
    }
    if (!sms) return {ok:false, reason:'sms_radio_missing'};

    const setChecked = (el, checked) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
      if (setter) setter.call(el, checked); else el.checked = checked;
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
    };

    // 先点 label，让 React-Aria 正常更新 thumb/data-state。
    const label = sms.closest('label');
    if (label) label.click(); else sms.click();
    setChecked(sms, true);
    if (wa) setChecked(wa, false);
    if (hiddenChannel) {
      hiddenChannel.value = 'sms';
      hiddenChannel.dispatchEvent(new Event('input', {bubbles:true}));
      hiddenChannel.dispatchEvent(new Event('change', {bubbles:true}));
    }
    document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', bubbles:true}));
    return {
      ok: !!sms.checked && (!wa || !wa.checked) && (!hiddenChannel || String(hiddenChannel.value || '').toLowerCase() === 'sms'),
      method: label ? 'label+native' : 'input+native',
      smsChecked: !!sms.checked,
      waChecked: wa ? !!wa.checked : null,
      channel: hiddenChannel ? hiddenChannel.value : ''
    };
    """)
    if selected and selected.get("ok"):
        logger.info("[Codex][Browser] 已选择 SMS 短信通道：%s", selected)
        return
    raise RuntimeError(f"sms_channel_select_failed: result={selected} state={_phone_page_state(driver)}")


def _is_phone_code_state(state: dict) -> bool:
    url = str(state.get('url') or '').lower()
    if 'email-verification' in url:
        # 邮箱 OTP 页面也会出现 autocomplete=one-time-code，不能误判成手机验证码页。
        return False
    if 'phone-verification' in url:
        return True
    forms = state.get('forms') or []
    form_actions = ' '.join(str(f.get('action') or '') for f in forms).lower()
    if 'phone-verification' in form_actions:
        return True
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','placeholder')) for i in inputs).lower()
    body = str(state.get('bodyText') or '').lower()
    has_code_input = 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs
    phone_hint = (
        'phone' in url or 'phone' in form_actions
        or 'check your phone' in body
        or 'verification code we just sent' in body
        or 'enter the verification code' in body and ('text message' in body or 'phone' in body)
        or 'resend text message' in body
        or 'sent to +' in body
    )
    return bool(phone_hint and has_code_input)


def _is_phone_code_page(driver) -> bool:
    return _is_phone_code_state(_phone_page_state(driver))


def _is_add_phone_page(driver) -> bool:
    state = _phone_page_state(driver)
    url = str(state.get('url') or '').lower()
    inputs = state.get('inputs') or []
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete')) for i in inputs).lower()
    return 'add-phone' in url or 'type tel' in attrs or 'phone' in attrs or 'tel' in attrs


_PHONE_INPUT_SELECTORS = [
    "input[type='tel']",
    "input[name='phone']",
    "input[name='phone_number']",
    "input[autocomplete='tel']",
    "input[id*='phone']",
    "input[placeholder*='Phone']",
    "input[placeholder*='phone']",
]


def _has_strict_add_phone_form(driver) -> bool:
    try:
        return bool(driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return false;
        return !![...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
        """))
    except Exception:
        return False


def _auth_origin(driver) -> str:
    try:
        parsed = urlparse(str(driver.current_url or ""))
        if parsed.scheme and parsed.netloc and parsed.hostname and parsed.hostname.endswith("openai.com"):
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return "https://auth.openai.com"


def _dismiss_phone_country_dropdown(driver) -> None:
    """关闭国家码下拉/移开焦点，避免遮挡 Continue 或吞掉点击。"""
    try:
        from selenium.webdriver.common.keys import Keys
        active = driver.switch_to.active_element
        try:
            active.send_keys(Keys.ESCAPE)
            time.sleep(0.12)
            active.send_keys(Keys.TAB)
            time.sleep(0.12)
        except Exception:
            pass
    except Exception:
        pass
    try:
        driver.execute_script(r"""
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') active.blur();
        document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', bubbles:true}));
        document.body?.click?.();
        document.body?.focus?.();
        """)
    except Exception:
        pass


def _clear_phone_inputs(driver) -> None:
    """清理手机号相关输入，避免换号时旧值/React 内部状态残留。"""
    try:
        driver.execute_script(r"""
        const inputs = [...document.querySelectorAll('input')];
        for (const el of inputs) {
          const hay = [el.type, el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute('aria-label')]
            .join(' ').toLowerCase();
          if (/(phone|tel|mobile|sms|手机号|手机|電話|携帯)/i.test(hay) || (el.type || '').toLowerCase() === 'tel') {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            if (setter) setter.call(el, ''); else el.value = '';
            el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward', data:null}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
        }
        """)
    except Exception:
        pass


def _try_click_change_phone(driver) -> bool:
    """验证码页换号时优先点击 Change/Edit/Back，保留 auth transaction。"""
    try:
        clicked = driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const candidates = [...document.querySelectorAll('button,a,[role="button"]')].filter(visible);
        const re = /(change|edit|back|phone|電話番号を変更|変更|戻る|更改|返回|뒤로|변경|전화번호 변경)/i;
        const target = candidates.find(el => re.test([el.innerText, el.textContent, el.value, el.getAttribute('aria-label')].join(' ')));
        if (!target) return false;
        target.scrollIntoView({block:'center'});
        target.click();
        return true;
        """)
        if clicked:
            human_delay("click")
            return _has_strict_add_phone_form(driver)
    except Exception:
        pass
    return False


def _ensure_add_phone_input(driver, *, reason: str = ""):
    """确保当前页面回到 add-phone，并返回手机号输入框。

    换号时如果还停留在 phone-verification/OTP 页，必须先回到手机号页，
    再把新号码重新写入页面并重新提交。
    """
    if _has_strict_add_phone_form(driver):
        return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)

    current = str(getattr(driver, "current_url", "") or "")
    if "email-verification" in current.lower():
        logger.info("[Codex][Browser] 当前仍在 email-verification，先等待授权流程自动跳转，避免 invalid_auth_step")
        _wait_after_email_otp_submit(driver, timeout=45)
        if _has_strict_add_phone_form(driver):
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
        current = str(getattr(driver, "current_url", "") or "")

    # 如果已经在验证码页，优先用页面上的 Change/Back 或浏览器后退回表单；
    # 比直接打开 /add-phone 更稳定，也更接近 BrowserUse 的逻辑。
    if _is_phone_code_page(driver):
        logger.info("[Codex][Browser] 当前在手机验证码页，优先返回手机号输入页：reason=%s", reason or "retry")
        if _try_click_change_phone(driver):
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=5)
        try:
            driver.back()
            human_delay("navigate")
            if _has_strict_add_phone_form(driver):
                return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=5)
        except Exception:
            pass

    target = _auth_origin(driver).rstrip("/") + "/add-phone"
    logger.info(
        "[Codex][Browser] 当前不在手机号输入页，准备重新打开 add-phone 后换号：reason=%s url=%s target=%s",
        reason or "retry", current, target,
    )
    try:
        driver.get(target)
        human_delay("navigate")
        try:
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=10)
        except Exception:
            # Roxy 偶发打开 add-phone 后 body 短暂空白，reload 一次。
            driver.refresh()
            human_delay("navigate")
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
    except Exception as first_exc:
        # 某些流程不允许直接打开 /add-phone，尝试浏览器返回到上一页。
        logger.info("[Codex][Browser] 直接打开 add-phone 未拿到输入框，尝试 history back：%s", str(first_exc)[:160])
        try:
            driver.back()
            human_delay("navigate")
            return _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
        except Exception as back_exc:
            raise RuntimeError(
                f"无法回到手机号输入页以重新换号: direct={type(first_exc).__name__}: {first_exc}; "
                f"back={type(back_exc).__name__}: {back_exc}; state={_phone_page_state(driver)}"
            )


def _set_phone_value(driver, phone: str, *, timeout: int = 10) -> dict:
    """按 FlowPilot 第 9 步逻辑填写 add-phone 表单。

    要点：
    - 所有元素 scoped 到 form[action*="/add-phone"]；
    - 可见 tel 输入框写入“页面期望显示的号码”；
    - 如果页面存在隐藏 input[name="phoneNumber"]，同步写入完整 E.164 号码；
    - 触发 input/change 并 blur，让 React/React-Aria 完成校验。
    """
    if not _has_strict_add_phone_form(driver):
        raise RuntimeError(f"当前不是 add-phone 手机号输入页，不能填写手机号: state={_phone_page_state(driver)}")
    result = driver.execute_script(r"""
    const rawPhone = String(arguments[0] || '').trim();
    const e164 = rawPhone.startsWith('+') ? rawPhone : ('+' + rawPhone.replace(/\D+/g, ''));
    const digits = e164.replace(/\D+/g, '');
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
    if (!form) {
      return {ok:false, error:'missing_add_phone_form', url: location.href};
    }
    const phoneInput = [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')]
      .find(visible);
    if (!phoneInput) {
      return {ok:false, error:'missing_phone_input', url: location.href};
    }

    const hiddenPhoneNumberInput = form.querySelector('input[name="phoneNumber"]');
    const select = form.querySelector('select');
    let dialCode = '';
    let selectedText = '';
    let selectedChanged = false;
    // 页面隐藏 select 的 option 文案在日文环境下只有国家名，通常不带 (+55) 之类区号。
    // 所以不能只从 option.textContent 解析区号；这里用常见 ISO->区号表按 E.164 前缀反推国家。
    const dialToIso = {
      '1':'US','7':'RU','20':'EG','27':'ZA','30':'GR','31':'NL','32':'BE','33':'FR','34':'ES','36':'HU','39':'IT',
      '40':'RO','41':'CH','43':'AT','44':'GB','45':'DK','46':'SE','47':'NO','48':'PL','49':'DE',
      '51':'PE','52':'MX','53':'CU','54':'AR','55':'BR','56':'CL','57':'CO','58':'VE',
      '60':'MY','61':'AU','62':'ID','63':'PH','64':'NZ','65':'SG','66':'TH',
      '81':'JP','82':'KR','84':'VN','86':'CN','90':'TR','91':'IN','92':'PK','93':'AF','94':'LK','95':'MM','98':'IR',
      '212':'MA','213':'DZ','216':'TN','218':'LY','234':'NG','254':'KE','255':'TZ','256':'UG','380':'UA',
      '351':'PT','352':'LU','353':'IE','354':'IS','355':'AL','356':'MT','357':'CY','358':'FI','359':'BG',
      '370':'LT','371':'LV','372':'EE','374':'AM','375':'BY','376':'AD','377':'MC','381':'RS','382':'ME',
      '385':'HR','386':'SI','387':'BA','389':'MK','420':'CZ','421':'SK','852':'HK','853':'MO','886':'TW',
      '971':'AE','972':'IL','974':'QA','966':'SA'
    };
    const matchedDial = Object.keys(dialToIso).sort((a,b) => b.length - a.length).find(code => digits.startsWith(code)) || '';
    const matchedIso = matchedDial ? dialToIso[matchedDial] : '';
    const optionDialCode = (opt) => {
      const text = String(opt?.textContent || opt?.label || opt?.value || '').replace(/\s+/g, ' ').trim();
      const m = text.match(/\+(\d{1,4})\b/);
      return m ? m[1] : '';
    };
    if (select) {
      // 参考 FlowPilot ensureCountrySelected：按号码前缀选择对应国家/区号，避免默认国家与号码不一致。
      const options = [...select.options];
      let matched = options.find(opt => matchedIso && String(opt.value || '').toUpperCase() === matchedIso);
      if (!matched) matched = options
        .map(opt => ({opt, code: optionDialCode(opt)}))
        .filter(x => x.code && digits.startsWith(x.code))
        .sort((a, b) => b.code.length - a.code.length)[0]?.opt;
      if (matched && select.value !== matched.value) {
        const selectSetter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
        if (selectSetter) selectSetter.call(select, matched.value); else select.value = matched.value;
        select.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertReplacementText', data: matched.value}));
        select.dispatchEvent(new Event('change', {bubbles:true}));
        selectedChanged = true;
      }
      if (select.selectedIndex >= 0 && select.options[select.selectedIndex]) {
        const opt = select.options[select.selectedIndex];
        selectedText = String(opt.textContent || opt.label || opt.value || '').replace(/\s+/g, ' ').trim();
        dialCode = optionDialCode(opt) || matchedDial;
      }
    }
    if (!dialCode) dialCode = matchedDial;

    // 可见框直接填完整 +E.164。
    // 之前填 national number 时，BR 会被组件格式化成 "(88) 95157-7449"；
    // 手工输入 +5588951577449 不会出现括号，因此这里保持和手工输入一致。
    let visibleValue = e164;

    const setNativeValue = (el, value) => {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      el.focus();
      if (setter) setter.call(el, ''); else el.value = '';
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      if (setter) setter.call(el, value); else el.value = value;
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
    };

    phoneInput.scrollIntoView({block:'center'});
    setNativeValue(phoneInput, visibleValue);
    if (hiddenPhoneNumberInput) {
      hiddenPhoneNumberInput.value = e164;
      hiddenPhoneNumberInput.dispatchEvent(new Event('input', {bubbles:true}));
      hiddenPhoneNumberInput.dispatchEvent(new Event('change', {bubbles:true}));
    }
    phoneInput.blur();
    document.body?.focus?.();
    document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', bubbles:true}));
    return {
      ok: true,
      e164,
      visibleValue,
      actualVisible: phoneInput.value || '',
      hiddenValue: hiddenPhoneNumberInput ? (hiddenPhoneNumberInput.value || '') : '',
      dialCode,
      selectedText,
      selectedChanged,
      inputName: phoneInput.getAttribute('name') || '',
      inputId: phoneInput.id || '',
      url: location.href,
    };
    """, phone)
    if not result or not result.get("ok"):
        raise RuntimeError(f"手机号写入失败 result={result} state={_phone_page_state(driver)}")
    actual = str(result.get("actualVisible") or "").strip()
    visible_value = str(result.get("visibleValue") or "").strip()
    hidden_value = str(result.get("hiddenValue") or "").strip()
    e164 = str(result.get("e164") or "").strip()
    # OpenAI/React-Aria 电话框会自动格式化，例如 +84925154291 -> +84 925 154 291。
    # 不能按界面字符串精确比较，只比较数字归一化后的值。
    actual_digits = ''.join(ch for ch in actual if ch.isdigit())
    visible_digits = ''.join(ch for ch in visible_value if ch.isdigit())
    e164_digits = ''.join(ch for ch in e164 if ch.isdigit())
    hidden_digits = ''.join(ch for ch in hidden_value if ch.isdigit())
    # 国家已正确选择时，可见框会只保留 national number（例如 BR: +5551926346338
    # 显示为 51 92634 6338）；未正确选择/无法判断时可能显示完整 +E.164。
    # 既接受“完整号码”，也接受“完整号码去掉当前 dialCode 后的 national 部分”。
    expected_visible_ok = bool(actual_digits) and (actual_digits == visible_digits or actual_digits == e164_digits)
    if not expected_visible_ok:
        raise RuntimeError(f"手机号可见输入框校验失败 expected_digits={visible_digits or e164_digits} actual={actual} result={result} state={_phone_page_state(driver)}")
    if hidden_value and hidden_digits != e164_digits:
        raise RuntimeError(f"手机号隐藏字段校验失败 expected={e164} actual={hidden_value} result={result} state={_phone_page_state(driver)}")
    return result


def _blur_active_input_and_wait(driver, *, label: str = "输入完成") -> None:
    """输入手机号后移开焦点，并给前端校验/格式化留处理时间。"""
    try:
        driver.execute_script(r"""
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') active.blur();
        document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', bubbles:true}));
        document.body?.focus?.();
        document.dispatchEvent(new Event('change', {bubbles:true}));
        """)
    except Exception:
        pass
    seconds = random.uniform(1.8, 3.2)
    logger.info("[Codex][Browser] %s，已移开焦点，等待页面处理 %.1f 秒", label, seconds)
    time.sleep(seconds)


def _verify_add_phone_value_before_submit(driver, expected_e164: str) -> dict:
    result = driver.execute_script(r"""
    const expected = String(arguments[0] || '').trim();
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
    if (!form) return {ok:false, error:'missing_add_phone_form', url: location.href};
    const input = [...form.querySelectorAll('input[type="tel"], input[name="__reservedForPhoneNumberInput_tel"], input[autocomplete="tel"], input[name="phone"], input[name="phone_number"]')].find(visible);
    const hidden = form.querySelector('input[name="phoneNumber"]');
    const visibleValue = String(input?.value || '').trim();
    const hiddenValue = String(hidden?.value || '').trim();
    const digits = value => String(value || '').replace(/\D+/g, '');
    const visibleDigits = digits(visibleValue);
    const hiddenDigits = digits(hiddenValue);
    const expectedDigits = digits(expected);
    const dialCodes = ['971','972','974','966','852','853','886','420','421','380','389','387','386','385','382','381','376','377','375','374','372','371','370','359','358','357','356','355','354','353','352','351','254','255','256','234','218','216','213','212','98','95','94','93','92','91','90','86','84','82','81','66','65','64','63','62','61','60','58','57','56','55','54','53','52','51','49','48','47','46','45','44','43','41','40','39','36','34','33','32','31','30','27','20','7','1'];
    const dialCode = dialCodes.find(code => expectedDigits.startsWith(code)) || '';
    const expectedNationalDigits = dialCode ? expectedDigits.slice(dialCode.length) : '';
    // 输入框可能被自动格式化：接受完整 E.164 数字，或国家码选择正确后的 national number。
    // 隐藏字段如果存在，必须等于完整 E.164。
    const visibleOk = !!visibleDigits && (visibleDigits === expectedDigits || (!!expectedNationalDigits && visibleDigits === expectedNationalDigits));
    const ok = visibleOk && (!hidden || hiddenDigits === expectedDigits);
    return {ok, visibleValue, hiddenValue, expected, visibleDigits, hiddenDigits, expectedDigits, expectedNationalDigits, dialCode, url: location.href};
    """, expected_e164)
    if not result or not result.get("ok"):
        raise RuntimeError(f"手机号提交前校验失败 result={result} state={_phone_page_state(driver)}")
    return result


def _verify_sms_channel_before_submit(driver) -> dict:
    """提交 add-phone 前二次确认通道仍是 SMS，防止 React 状态回弹到 WhatsApp。"""
    result = driver.execute_script(r"""
    const form = document.querySelector('form[action*="/add-phone" i]')
      || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''))
      || document;
    const radios = [...form.querySelectorAll('input[type=radio]')];
    const norm = v => String(v || '').toLowerCase().replace(/[\s_-]+/g, '');
    const sms = radios.find(el => ['sms','text','textmessage'].includes(norm(el.value)));
    const wa = radios.find(el => norm(el.value).includes('whatsapp'));
    const hiddenChannel = form.querySelector('input[name="channel"]');

    const setChecked = (el, checked) => {
      if (!el) return;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
      if (setter) setter.call(el, checked); else el.checked = checked;
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
    };

    if (sms && !sms.checked) {
      const label = sms.closest('label');
      if (label) label.click(); else sms.click();
      setChecked(sms, true);
    }
    if (wa && wa.checked) setChecked(wa, false);
    if (hiddenChannel && String(hiddenChannel.value || '').toLowerCase() !== 'sms') {
      hiddenChannel.value = 'sms';
      hiddenChannel.dispatchEvent(new Event('input', {bubbles:true}));
      hiddenChannel.dispatchEvent(new Event('change', {bubbles:true}));
    }
    const ok = (!sms || sms.checked) && (!wa || !wa.checked) && (!hiddenChannel || String(hiddenChannel.value || '').toLowerCase() === 'sms');
    return {
      ok,
      smsExists: !!sms,
      smsChecked: sms ? !!sms.checked : null,
      waExists: !!wa,
      waChecked: wa ? !!wa.checked : null,
      channel: hiddenChannel ? hiddenChannel.value : '',
      url: location.href
    };
    """)
    if not result or not result.get("ok"):
        raise RuntimeError(f"sms_channel_verify_failed: result={result} state={_phone_page_state(driver)}")
    return result


def _wait_page_settle_after_submit() -> None:
    """点击提交后先等待页面处理，再检查发送状态。"""
    seconds = random.uniform(2.0, 4.0)
    logger.info("[Codex][Browser] 已点击提交，等待页面发送/跳转处理 %.1f 秒后检查状态", seconds)
    time.sleep(seconds)


def _refresh_add_phone_for_retry(driver, *, reason: str = "") -> None:
    """发送失败/换号前刷新手机号页，避免旧错误状态和旧号码残留。"""
    try:
        logger.info("[Codex][Browser] 发送失败/准备换号，刷新手机号页面：%s", reason or "retry")
        driver.refresh()
        human_delay("navigate")
        try:
            _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
            return
        except Exception:
            pass
        # 如果刷新后仍不在输入页，强制回 add-phone。
        target = _auth_origin(driver).rstrip("/") + "/add-phone"
        logger.info("[Codex][Browser] 刷新后未找到手机号输入框，重新打开：%s", target)
        driver.get(target)
        human_delay("navigate")
        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=8)
    except Exception as exc:
        logger.info("[Codex][Browser] 刷新手机号页失败，下一轮会再次尝试回到 add-phone：%s", str(exc)[:180])


def _click_add_phone_continue_button(driver, *, timeout: int = 10) -> dict:
    """点击 add-phone 表单里的 Continue/続行 按钮。

    参考 FlowPilot 的 getAddPhoneSubmitButton + simulateClick：优先在 add-phone form 内找
    enabled submit，点击失败时用 form.requestSubmit(button) 兜底。
    """
    _dismiss_phone_country_dropdown(driver)
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => {
              if (!el) return false;
              if (el.disabled) return false;
              if (String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
              return true;
            };
            const form = document.querySelector('form[action*="/add-phone" i]')
              || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
            if (!form) return null;
            const buttons = [...form.querySelectorAll('button[type="submit"], input[type="submit"]')];
            return buttons.find(b => visible(b) && enabled(b) && (b.getAttribute('data-dd-action-name') || '').toLowerCase() === 'continue')
              || buttons.find(b => visible(b) && enabled(b))
              || buttons.find(b => visible(b))
              || null;
            """)
            if btn:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(random.uniform(0.3, 0.8))
                try:
                    text = str(getattr(btn, 'text', '') or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                except Exception:
                    text = ''
                try:
                    btn.click()
                    _wait_page_settle_after_submit()
                    return {"ok": True, "method": "click", "text": text}
                except Exception as click_exc:
                    last = click_exc
                    submitted = driver.execute_script(r"""
                    const btn = arguments[0];
                    const form = btn?.form || btn?.closest?.('form');
                    if (form && typeof form.requestSubmit === 'function') {
                      form.requestSubmit(btn);
                      return true;
                    }
                    if (btn && typeof btn.click === 'function') {
                      btn.click();
                      return true;
                    }
                    return false;
                    """, btn)
                    if submitted:
                        _wait_page_settle_after_submit()
                        return {"ok": True, "method": "requestSubmit", "text": text, "click_error": str(click_exc)[:160]}
        except Exception as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"submit_missing: add-phone Continue/続行 submit button not found last={last} state={_phone_page_state(driver)}")


def _force_submit_add_phone_form(driver) -> dict:
    """add-phone 页面点击按钮没生效时，直接 requestSubmit 当前 form。"""
    try:
        _dismiss_phone_country_dropdown(driver)
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const form = document.querySelector('form[action*="/add-phone" i]')
          || [...document.querySelectorAll('form')].find(f => /add-phone/i.test(f.getAttribute('action') || ''));
        if (!form) return {ok:false, reason:'missing_form', url: location.href};
        const btn = [...form.querySelectorAll('button[type="submit"],input[type="submit"]')]
          .find(el => visible(el) && !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true')
          || form.querySelector('button[type="submit"],input[type="submit"]');
        if (btn) btn.scrollIntoView({block:'center'});
        if (typeof form.requestSubmit === 'function') form.requestSubmit(btn || undefined);
        else if (btn && typeof btn.click === 'function') btn.click();
        else form.submit();
        return {ok:true, method: btn ? 'requestSubmit(button)' : 'requestSubmit(form)', url: location.href};
        """) or {}
    except Exception as exc:
        return {ok:false, reason:f'{type(exc).__name__}: {exc}', url:getattr(driver, 'current_url', '')}


def _wait_after_phone_send(driver, timeout: int = 12) -> str:
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(1)
        last = _phone_page_state(driver)
        # 必须优先判断验证码页：页面文案里可能包含 send/limit/check 等词，不能把
        # “Check your phone / Enter the verification code...” 误判成发送失败。
        if _is_phone_code_state(last):
            body_lower = str(last.get('bodyText') or '').lower()
            if 'whatsapp' in body_lower or 'whats app' in body_lower:
                raise RuntimeError(f"whatsapp_channel: 已进入 WhatsApp 验证码页 body={(last.get('bodyText') or '')[:240]}")
            return 'code_page'
        body = str(last.get('bodyText') or '')
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {body[:240]}")
        # 仍在 add-phone 且字段有 aria-invalid，认为号码被拒。
        if _is_add_phone_page(driver):
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
            if invalid:
                raise RuntimeError(f"invalid_phone: add-phone input aria-invalid state={last}")
            # 不在这里自动补 requestSubmit。日志显示二次 requestSubmit 容易在 React 状态未稳定时
            # 把 channel 打成 WhatsApp，且后续回退可能触发 invalid_auth_step。
            # 只等待；超时后由外层换号/恢复页面。
    if _is_phone_code_state(last) or _is_phone_code_page(driver):
        return 'code_page'
    if _is_add_phone_page(driver):
        raise RuntimeError(f"send_not_accepted: 提交后仍停留在 add-phone state={last}")
    return 'unknown'


def _wait_after_phone_otp_submit(driver, timeout: int = 20) -> str:
    """手机验证码提交后等待结果。

    成功时通常会跳出 phone-verification，进入 consent/workspace/callback；不能在提交后
    3 秒立刻读取旧页面文案并按 send_limited 判失败。只有明确仍在手机号流程且出现错误时
    才返回失败。
    """
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(1)
        current = str(getattr(driver, "current_url", "") or "")
        if _is_callback_url(current):
            return "callback"
        last = _phone_page_state(driver)
        # 已离开手机验证码/加手机号页面，说明验证码被接受，后续交给 consent/callback 流程。
        if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
            return "left_phone_flow"
        # 仍在验证码页时，只把明确错误当失败；普通 Check your phone 页面继续等。
        if _is_phone_code_state(last):
            inputs = last.get('inputs') or []
            invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in inputs)
            body = str(last.get('bodyText') or '').lower()
            if invalid or any(k in body for k in (
                'invalid code', 'incorrect code', 'wrong code', 'expired code',
                'code is invalid', 'code was invalid', '验证码无效', '验证码错误', '验证码已过期',
                '認証コードが無効', 'コードが正しく',
            )):
                raise RuntimeError(f"invalid_phone_code: {(last.get('bodyText') or '')[:240]}")
            continue
        reason = _classify_phone_page_failure(last)
        if reason:
            raise RuntimeError(f"{reason}: {(last.get('bodyText') or '')[:240]}")
    # 超时后再看一次：如果已经离开手机号流程，视为通过；如果仍在验证码页但没明确错误，交给后续流程继续试。
    current = str(getattr(driver, "current_url", "") or "")
    if _is_callback_url(current):
        return "callback"
    last = _phone_page_state(driver)
    if not _is_phone_code_state(last) and not _is_add_phone_page(driver):
        return "left_phone_flow"
    if _is_phone_code_state(last):
        return "still_code_page"
    return "unknown"


def _classify_phone_page_failure(state: dict) -> str:
    if _is_phone_code_state(state):
        return ''
    # WhatsApp 用 DOM radio value 判断；其它发送失败用服务端/页面错误文本兜底。
    radios = state.get('radios') or []
    has_sms = any(str(r.get('value','')).lower() in ('sms', 'text', 'text_message', 'text-message') for r in radios)
    whatsapp_checked = any('whatsapp' in str(r.get('value','')).lower().replace(' ', '') and r.get('checked') for r in radios)
    if whatsapp_checked and not has_sms:
        return 'whatsapp_channel'
    text = str(state.get('bodyText') or '').lower()
    if 'invalid_auth_step' in text or 'invalid auth step' in text:
        return 'invalid_auth_step'
    # add-phone 页面会同时显示 SMS / WhatsApp 两个通道，bodyText 里出现 WhatsApp
    # 不代表当前走的是 WhatsApp。不能仅凭文案换号，否则会在可提交页面误判。
    if any(k in text for k in ('invalid phone', 'not a valid phone', 'phone number is not valid', '号码无效', '手机号无效')):
        return 'invalid_phone'
    if any(k in text for k in (
        'cannot send', 'could not send', 'unable to send', 'failed to send', 'send failed',
        '发送失败', '发送失败了', '无法发送', '不能发送', '无法向',
        '送信できません', '送信に失敗', '送信できなかった',
    )):
        return 'delivery_refused'
    if any(k in text for k in ('too many', 'rate limit', 'throttle', '频繁', '限流')):
        return 'send_limited'
    return ''

def _sleep_before_phone_retry(attempt: int, max_retries: int, *, prefix: str = "[Codex][Browser]") -> None:
    """换号前随机等待，至少 3 秒，避免连续提交号码过快。"""
    if attempt >= max_retries:
        return
    seconds = random.uniform(3.0, 8.0)
    logger.info("%s 换号前随机等待 %.1f 秒", prefix, seconds)
    time.sleep(seconds)


def _do_phone_verification_if_present(driver) -> None:
    """如果页面要求手机号验证，则用当前 sms_provider 自动完成。"""
    provider = str(getattr(sms_provider._cfg, "SMS_PROVIDER", "") or "").strip().lower() if hasattr(sms_provider, "_cfg") else ""
    http = sms_provider._http()
    max_retries = int(getattr(sms_provider._cfg, "SMS_MAX_RETRIES", 10) or 10) if hasattr(sms_provider, "_cfg") else 10
    try:
        # 如果页面没有手机号输入框，直接返回。
        try:
            # 对齐 BrowserUse：邮箱 OTP 后跳手机号页可能较慢，给 20 秒观察窗口。
            end_detect = time.time() + 20
            while time.time() < end_detect and not _has_strict_add_phone_form(driver):
                # 如果已经在验证码页，说明手机步骤之前已提交过；继续处理验证码页，不应当跳过。
                if _is_phone_code_page(driver):
                    break
                time.sleep(0.5)
            if not (_has_strict_add_phone_form(driver) or _is_phone_code_page(driver)):
                raise RuntimeError("not_phone_flow")
        except Exception:
            logger.info("[Codex][Browser] 未检测到手机号验证页，跳过手机步骤")
            return

        last_err = None
        for attempt in range(1, max_retries + 1):
            activation_id = None
            try:
                # 先确认/恢复到手机号输入页，再取号；避免页面还在验证码页或空白页时浪费号码。
                logger.info("[Codex][Browser] 准备手机号输入页，重新设置新手机号")
                _ensure_add_phone_input(driver, reason=f"attempt-{attempt}")
                _dismiss_phone_country_dropdown(driver)
                _clear_phone_inputs(driver)

                activation_id, phone = sms_provider.acquire_number(http)
                logger.info("[Codex][Browser] 手机验证尝试 %s/%s，provider=%s，号码=+%s", attempt, max_retries, provider, phone)
                phone_fill = _set_phone_value(driver, f"+{phone}", timeout=10)
                logger.info(
                    "[Codex][Browser] 已重新设置手机号：e164=%s visible=%s hidden=%s dialCode=%s country=%s",
                    phone_fill.get("e164"), phone_fill.get("actualVisible"), phone_fill.get("hiddenValue") or "-",
                    phone_fill.get("dialCode") or "-", (str(phone_fill.get("selectedText") or "-") + (" [changed]" if phone_fill.get("selectedChanged") else "")),
                )
                _blur_active_input_and_wait(driver, label="手机号输入完成")
                phone_verify = _verify_add_phone_value_before_submit(driver, str(phone_fill.get("e164") or f"+{phone}"))
                logger.info("[Codex][Browser] 手机号提交前校验通过：visible=%s hidden=%s", phone_verify.get("visibleValue"), phone_verify.get("hiddenValue") or "-")
                logger.info("[Codex][Browser] 检查并选择 SMS 短信通道")
                _select_sms_channel_or_raise(driver)
                _blur_active_input_and_wait(driver, label="短信通道确认完成")
                channel_verify = _verify_sms_channel_before_submit(driver)
                logger.info("[Codex][Browser] 手机号提交前短信通道校验通过：%s", channel_verify)
                submit_info = _click_add_phone_continue_button(driver, timeout=10)
                logger.info("[Codex][Browser] 已点击手机号 Continue/続行 按钮：%s，等待进入短信验证码页", submit_info)

                # 等待页面进入 phone-verification；若号码无效/无法发送/WhatsApp 通道，立即换号。
                _wait_after_phone_send(driver, timeout=22)
                logger.info("[Codex][Browser] 已进入手机验证码页")

                sms_provider.set_status(activation_id, 1, http=http)
                logger.info(
                    "[Codex][Browser] 短信已发送，开始轮询验证码 activation_id=%s wait=%ss interval=%ss",
                    activation_id, sms_provider._cfg.SMS_CODE_WAIT, sms_provider._cfg.SMS_POLL_INTERVAL
                )
                sms_code = sms_provider.wait_for_sms_code(activation_id, http)
                logger.info("[Codex][Browser] 手机 OTP 收到：%s", sms_code)
                _type_otp(driver, sms_code)
                logger.info("[Codex][Browser] 已填写手机 OTP")
                human_delay("otp_input")
                if not _click_if_present(driver, ["button[type='submit']", "input[type='submit']"], timeout=10):
                    raise RuntimeError(f"verify_submit_missing: phone verification submit not found state={_phone_page_state(driver)}")
                logger.info("[Codex][Browser] 已提交手机 OTP，等待验证结果")
                otp_outcome = _wait_after_phone_otp_submit(driver, timeout=25)
                logger.info("[Codex][Browser] 手机 OTP 提交后状态：%s", otp_outcome)
                sms_provider.complete(activation_id, http)
                return
            except Exception as exc:
                last_err = exc
                err_text = str(exc) or ""
                logger.warning("[Codex][Browser] 手机验证尝试失败，换号：%s", err_text[:240])
                if activation_id:
                    try:
                        sms_provider.cancel(activation_id, http)
                    except Exception:
                        pass
                # 余额不足 / 无可用号码：重试多少次都不会成功，立即失败止损，
                # 避免白等 N 轮换号重试（每轮还要刷新页面 + 随机等待）。
                if any(k in err_text for k in (
                    "NO_BALANCE", "NO_NUMBERS", "BALANCE", "余额不足",
                    "暂无可用号码", "没有可用号码", "insufficient", "not enough balance",
                )):
                    raise RuntimeError(
                        f"接码平台余额不足或无可用号码，已停止换号止损：{err_text[:180]}"
                    ) from exc
                if "invalid_auth_step" in str(exc):
                    raise RuntimeError(
                        "手机号流程进入 invalid_auth_step，说明授权状态还未从 email-verification 正常跳转或已失效；"
                        "已停止继续换号，避免继续消耗号码"
                    ) from exc
                # 如果已经离开手机号/验证码相关页面，认为通过或不再需要；
                # 如果仍在 phone-verification，则下一轮必须回 add-phone 重新填新号码再提交。
                try:
                    if _is_phone_code_page(driver):
                        logger.info("[Codex][Browser] 当前仍在手机验证码页，下一轮将返回 add-phone 重新设置新号码")
                    else:
                        _find_any(driver, _PHONE_INPUT_SELECTORS, timeout=2)
                except Exception:
                    if _is_add_phone_page(driver) or _is_phone_code_page(driver):
                        logger.info("[Codex][Browser] 仍处于手机号流程，继续换号重试")
                    else:
                        logger.info("[Codex][Browser] 手机输入页已消失，继续后续流程")
                        return
                if attempt < max_retries:
                    if _is_phone_code_page(driver):
                        # 关键：验证码页不要 refresh。刷新/直接开 add-phone 容易破坏 auth step，
                        # 下一次提交出现 invalid_auth_step。优先走 Change/Back 保留事务。
                        try:
                            _ensure_add_phone_input(driver, reason=f"after-code-fail-{attempt}")
                        except Exception as back_exc:
                            logger.info("[Codex][Browser] 从验证码页返回手机号页失败，下一轮会再次尝试：%s", str(back_exc)[:180])
                    else:
                        _refresh_add_phone_for_retry(driver, reason=str(exc)[:120])
                    _dismiss_phone_country_dropdown(driver)
                    _clear_phone_inputs(driver)
                _sleep_before_phone_retry(attempt, max_retries)
        raise RuntimeError(f"Roxy 手机验证重试 {max_retries} 次仍失败，最后错误：{last_err}")
    finally:
        try:
            http.close()
        except Exception:
            pass


def _finish_consent_workspace(driver) -> str:
    """点击 Codex consent/workspace 页面里的继续/允许按钮，直到 callback。"""
    end = time.time() + int(_roxy_cfg.ROXY_CODEX_CALLBACK_TIMEOUT)
    while time.time() < end:
        callback = _extract_callback_url_from_any_window(driver)
        if callback:
            return callback
        current = str(driver.current_url or "")
        clicked = False
        for selectors in [
            ["//button[contains(., 'Allow')]", "//button[contains(., 'Authorize')]", "//button[contains(., 'Continue')]"],
            ["//button[contains(., 'Select')]", "//button[contains(., 'Use workspace')]", "//button[contains(., 'Confirm')]"],
            ["//button[contains(., '允许')]", "//button[contains(., '授权')]", "//button[contains(., '继续')]", "//button[contains(., '确认')]"],
            ["button[type='submit']"],
        ]:
            if _click_if_present(driver, selectors, timeout=2):
                clicked = True
                human_delay("form")
                break
        if not clicked:
            time.sleep(0.8)
    return _wait_for_callback(driver, timeout=5)




def clear_roxy_browser_auth_state(driver) -> None:
    """清空当前 Roxy 浏览器里的 OpenAI/ChatGPT 登录态与缓存，用于注册后复用同一环境跑 Codex。"""
    origins = [
        "https://auth.openai.com",
        "https://chatgpt.com",
        "https://openai.com",
        "https://platform.openai.com",
    ]
    logger.info("[Codex][Browser] 复用注册窗口：开始清理 Cookie / localStorage / sessionStorage / cache")
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        logger.info("[Codex][Browser] 已清理浏览器 Cookie")
    except Exception as exc:
        logger.info("[Codex][Browser] 清理 Cookie 失败，继续尝试其它缓存：%s", str(exc)[:160])
    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        logger.info("[Codex][Browser] 已清理浏览器 Cache")
    except Exception as exc:
        logger.info("[Codex][Browser] 清理 Cache 失败，继续：%s", str(exc)[:160])
    for origin in origins:
        try:
            driver.execute_cdp_cmd("Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": "all",
            })
            logger.info("[Codex][Browser] 已清理站点数据：%s", origin)
        except Exception as exc:
            logger.debug("[Codex][Browser] 清理站点数据失败 %s: %s", origin, exc)
    try:
        driver.get("about:blank")
    except Exception:
        pass
    time.sleep(1.0)
    logger.info("[Codex][Browser] 注册窗口登录态清理完成，准备开始 Codex 授权")

def _run_roxy_codex_oauth_once(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
) -> dict:
    """指纹浏览器 Codex OAuth 入口。

    existing_driver/existing_opened 用于“注册成功后立刻跑 Codex”：
    复用注册时的 Roxy 窗口，不新建环境，只清理浏览器状态后开始授权。
    """
    from core import codex_oauth as proto

    if not force and not proto._cfg.ENABLE_CODEX_AUTO:
        return proto._codex_result(status="skipped", message="ENABLE_CODEX_AUTO=False")
    if not email:
        return proto._codex_result(status="skipped", message="email 为空")
    if otp_provider is None:
        otp_provider = wait_for_otp

    client = None if reuse_existing_profile else RoxyBrowserClient()
    opened = existing_opened if reuse_existing_profile else client.open_profile()
    browser_kind_token = _CODEX_BROWSER_KIND.set(_detect_browser_kind(opened))
    driver = existing_driver if reuse_existing_profile else None
    owns_driver = not reuse_existing_profile
    try:
        auth_source = proto._codex_auth_url_source()
        code_verifier = None
        if auth_source == "cpa":
            cpa_auth = proto._request_cpa_authorize_url()
            state = cpa_auth["state"]
            auth_url = cpa_auth["auth_url"]
            logger.info("[Codex][Browser] 当前使用 CPA 授权地址: %s", auth_url)
        elif auth_source == "sub2":
            sub2_auth = proto._request_sub2_authorize_url()
            state = sub2_auth["state"]
            auth_url = sub2_auth["auth_url"]
            logger.info("[Codex][Browser] 当前使用 sub2 授权地址: %s", auth_url)
        elif auth_source == "local":
            code_verifier, code_challenge = proto._generate_pkce()
            state = proto._generate_state()
            auth_url = proto._build_authorize_url(state, code_challenge, prompt="login")
            logger.info("[Codex][Browser] 当前使用本地 PKCE 授权地址: %s", auth_url)
        else:
            raise RuntimeError(f"[Codex][Browser] 不支持的 CODEX_AUTH_URL_SOURCE={auth_source!r}")

        if not driver:
            driver = _build_driver(opened)
            _center_browser_window(driver)
        driver.set_page_load_timeout(int(_roxy_cfg.ROXY_SELENIUM_TIMEOUT))
        logger.info("[Codex][Browser] 开始授权：%s，profile=%s，reuse_existing_profile=%s", email, opened.profile_id, reuse_existing_profile)
        if reuse_existing_profile and clear_existing_state:
            clear_roxy_browser_auth_state(driver)

        _fill_email_and_otp(driver, email, otp_provider, auth_url)
        human_delay("api")
        logger.info("[Codex][Browser] 检查是否需要手机号验证")
        _do_phone_verification_if_present(driver)
        logger.info("[Codex][Browser] 手机验证处理完成/无需处理，等待授权确认和 callback")
        callback_url = _finish_consent_workspace(driver)
        code = proto._extract_code(callback_url, state)
        logger.info("[Codex][Browser] 已捕获 callback code：%s...", code[:24])

        if auth_source == "cpa":
            submit_payload = proto._submit_cpa_callback(callback_url)
            path = proto._save_cpa_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url,
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "CPA callback submitted"
            return proto._codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=f"{_codex_driver_name()}: {msg}",
            )

        if auth_source == "sub2":
            submit_payload = proto._submit_sub2_callback(
                callback_url,
                session_id=(sub2_auth or {}).get("session_id", ""),
                redirect_uri=(proto.parse_qs(proto.urlparse(auth_url or "").query).get("redirect_uri") or [""])[0],
            )
            path = proto._save_sub2_local_record(
                email=email,
                callback_url=callback_url,
                auth_url=auth_url,
                state=state,
                submit_payload=submit_payload,
            )
            msg = submit_payload.get("message") or submit_payload.get("status_message") or "sub2 callback uploaded"
            return proto._codex_result(
                status="success",
                ok=True,
                email=email,
                file_path=str(path) if path else None,
                callback_url=callback_url,
                message=f"{_codex_driver_name()}: {msg}",
            )

        if not code_verifier:
            raise RuntimeError("[Codex][Browser] local 模式缺少 code_verifier")
        session = proto.BrowserSession(proxy=proxy, fingerprint_seed=f"account:{email.lower()}")
        token_resp = proto.exchange_codex_token(session, code, code_verifier)
        id_claims = proto._parse_id_token(token_resp.get("id_token", ""))
        effective_email = id_claims.get("email") or email
        storage = proto.build_codex_storage(token_resp, id_claims)
        path = proto.save_codex_credential(storage, effective_email, id_claims.get("plan_type", ""))
        return proto._codex_result(
            status="success",
            ok=True,
            email=effective_email,
            file_path=str(path),
            callback_url=callback_url,
            message=f"{_codex_driver_name()} plan={id_claims.get('plan_type') or 'unknown'}",
        )
    except AccountUnusableError as exc:
        logger.warning("[Codex][Browser] 账号已废：%s，%s", email, exc.error_code)
        return proto._codex_result(
            status="deactivated",
            email=email,
            message=f"账号已废（{exc.error_code or 'account_deactivated'}）",
        )
    except Exception as exc:
        logger.warning("[Codex][Browser] 失败：%s，%s: %s", email, type(exc).__name__, str(exc)[:240])
        logger.debug("[Codex][Browser] 失败详情", exc_info=True)
        return proto._codex_result(status="failed", email=email, message=f"{type(exc).__name__}: {str(exc)[:220]}")
    finally:
        # 注册后复用窗口时，driver/profile 生命周期由注册流程统一清理，
        # 这里不能 quit/delete，否则会提前销毁注册环境。
        if owns_driver and driver and not bool(_roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if owns_driver and client and not bool(_roxy_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
        try:
            _CODEX_BROWSER_KIND.reset(browser_kind_token)
        except Exception:
            pass


def run_roxy_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    existing_driver=None,
    existing_opened=None,
    reuse_existing_profile: bool = False,
    clear_existing_state: bool = True,
) -> dict:
    """指纹浏览器 Codex OAuth 入口；CPA callback 409 timeout 时重新开启一轮授权。"""
    from core import codex_oauth as proto

    max_rounds = 2
    last_result = None
    for round_no in range(1, max_rounds + 1):
        if round_no > 1:
            logger.warning(
                "[Codex][Browser] CPA callback 返回 Timeout waiting for OAuth callback，重新开启第 %s/%s 轮 Codex 授权：%s",
                round_no, max_rounds, email,
            )
        result = _run_roxy_codex_oauth_once(
            email=email,
            otp_provider=otp_provider,
            proxy=proxy,
            force=force,
            existing_driver=existing_driver,
            existing_opened=existing_opened,
            reuse_existing_profile=reuse_existing_profile,
            clear_existing_state=clear_existing_state,
        )
        last_result = result
        if result.get("ok"):
            return result
        msg = result.get("message") or result.get("error") or ""
        if not proto._is_cpa_callback_reauth_error(msg):
            return result
    if last_result:
        last_result = dict(last_result)
        last_result["message"] = f"CPA callback 超时，已重新授权 {max_rounds} 轮仍失败：{last_result.get('message') or ''}"
        return last_result
    return proto._codex_result(status="failed", email=email, message="CPA callback 超时，重新授权失败")
