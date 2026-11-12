import unittest
from unittest.mock import Mock, patch

from core.cloakbrowser_driver import CloakSeleniumDriver
from core.roxy_registration import _type_otp, _wait_after_email_otp_submit


class _OtpElement:
    def __init__(self):
        self.values = []

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return {
            "autocomplete": "one-time-code",
            "name": "code",
            "inputmode": "numeric",
            "type": "text",
            "aria-label": "",
            "id": "",
        }.get(name, "")

    def send_keys(self, value):
        self.values.append(value)


class _LateOtpDriver:
    def __init__(self, *, appear_on_scan=3):
        self.appear_on_scan = appear_on_scan
        self.primary_scans = 0
        self.element = _OtpElement()
        self.refresh_count = 0

    def find_elements(self, _by, selector):
        if selector != "input[autocomplete='one-time-code']":
            return []
        self.primary_scans += 1
        return [self.element] if self.primary_scans >= self.appear_on_scan else []

    def refresh(self):
        self.refresh_count += 1


class OtpSubmitRecoveryTests(unittest.TestCase):
    def test_late_otp_input_is_polled_without_refreshing(self):
        driver = _LateOtpDriver(appear_on_scan=3)
        with patch("core.roxy_registration.time.sleep"), \
             patch("core.roxy_registration._human_type_text") as type_text:
            _type_otp(driver, "123456", attempts=4, retry_delay=0)
        type_text.assert_called_once_with(driver, driver.element, "123456", clear=True)
        self.assertEqual(driver.refresh_count, 0)

    def test_missing_otp_input_has_finite_refreshes_and_diagnostics(self):
        driver = _LateOtpDriver(appear_on_scan=9999)
        with patch("core.roxy_registration.time.sleep"), \
             patch("core.roxy_registration._page_warmup"), \
             patch("core.roxy_registration._email_otp_page_state", return_value={"url": "https://auth.example/email-verification", "inputs": []}):
            with self.assertRaisesRegex(RuntimeError, "state=.*email-verification"):
                _type_otp(driver, "123456", attempts=1, retry_delay=0)
        self.assertEqual(driver.refresh_count, 3)

    def test_cloak_refresh_waits_only_for_navigation_commit(self):
        page = Mock()
        driver = CloakSeleniumDriver(browser=Mock(), context=Mock(), page=page)
        driver.refresh()
        page.reload.assert_called_once_with(wait_until="commit", timeout=30000)

    def test_stuck_page_retries_continue_then_reports_timeout(self):
        button = Mock()
        driver = Mock()
        driver.find_elements.return_value = [button]
        with patch('core.roxy_registration.time.time', side_effect=[0, 0, 1, 2, 3, 4, 5]), \
             patch('core.roxy_registration.time.sleep'), \
             patch('core.roxy_registration._is_email_verification_page', return_value=True), \
             patch('core.roxy_registration._has_access_token', return_value=False), \
             patch('core.roxy_registration._email_otp_page_state', return_value={'inputs': [], 'errors': []}), \
             patch('core.roxy_registration._visible', return_value=True), \
             patch('core.roxy_registration._human_click') as click:
            with self.assertRaisesRegex(RuntimeError, '仍停留验证页'):
                _wait_after_email_otp_submit(driver, timeout=4)
        click.assert_called_once_with(driver, button, label='email_otp_retry')

    def test_page_transition_is_accepted(self):
        with patch('core.roxy_registration.time.sleep'), \
             patch('core.roxy_registration._is_email_verification_page', return_value=False):
            self.assertEqual(_wait_after_email_otp_submit(Mock()), 'accepted')


if __name__ == '__main__':
    unittest.main()
