import importlib.util
import unittest
from unittest.mock import patch
from config import browser
from core.session import BrowserSession
from core.protocol_profiles import supported_profiles, align_browser_profile
from webui.config_editor import get_config, update_config


class ProtocolProfileTests(unittest.TestCase):
    def test_choices_match_installed_backends(self):
        fields = {f['key']: f for f in get_config()}
        for backend, key in [('curl_cffi', 'CURL_CFFI_IMPERSONATE'), ('wreq', 'WREQ_EMULATION')]:
            expected = supported_profiles(backend)
            if expected:
                self.assertEqual([x['value'] for x in fields[key]['choices']], expected)
                self.assertNotIn('chrome', expected)
                self.assertFalse(any('android' in x or 'ios' in x for x in expected))

    def test_profile_alignment_and_validation(self):
        for name in ['chrome136', 'edge101', 'firefox135', 'safari184', 'safari2601', 'safari18_3']:
            with self.subTest(name=name):
                profile = align_browser_profile(browser.build_browser_environment(), name)
                self.assertEqual(browser.validate_browser_profile(profile), [])
                session = object.__new__(BrowserSession)
                session.browser_profile = profile
                headers = session._get_common_headers()
                if name.startswith(('safari', 'firefox')):
                    self.assertNotIn('sec-ch-ua', headers)
                else:
                    self.assertIn(profile['chrome_major'], headers['sec-ch-ua'])
                    self.assertIn('Chrome/' + profile['chrome_full_version'], headers['User-Agent'])
        self.assertIn('Version/26.0.1', align_browser_profile({}, 'safari2601')['user_agent'])

    def test_curl_uses_own_setting_and_keeps_existing_session(self):
        with patch.object(browser, 'PROTOCOL_HTTP_CLIENT', 'curl_cffi'), \
             patch.object(browser, 'CURL_CFFI_IMPERSONATE', 'chrome136'), \
             patch.object(browser, 'WREQ_EMULATION', 'chrome146'), \
             patch('core.session.Session') as factory, \
             patch.object(BrowserSession, '_enforce_proxy_quality'):
            session = BrowserSession(proxy='', detect_exit_geo=False)
            factory.assert_called_once_with(impersonate='chrome136')
            self.assertIn('Chrome/136.', session.browser_profile['user_agent'])
            with patch.object(browser, 'CURL_CFFI_IMPERSONATE', 'chrome146'):
                self.assertEqual(session.impersonate, 'chrome136')
            session.close()

    @unittest.skipUnless(importlib.util.find_spec('wreq'), 'wreq 未安装')
    def test_wreq_uses_own_setting(self):
        with patch.object(browser, 'PROTOCOL_HTTP_CLIENT', 'wreq'), \
             patch.object(browser, 'WREQ_EMULATION', 'safari18_3'), \
             patch.object(BrowserSession, '_enforce_proxy_quality'):
            session = BrowserSession(proxy='', detect_exit_geo=False)
            self.addCleanup(session.close)
            self.assertEqual(session.impersonate, 'safari18_3')
            self.assertIn('Version/18.3', session.browser_profile['user_agent'])

    def test_invalid_profile_rejected_before_saving(self):
        with self.assertRaises(ValueError):
            update_config({'CURL_CFFI_IMPERSONATE': 'not-a-profile'})

    def test_missing_inactive_backend_does_not_block_save(self):
        from core import protocol_profiles
        original = protocol_profiles.supported_profiles
        def available(backend):
            return [] if backend == 'wreq' else original(backend)
        with patch.object(protocol_profiles, 'supported_profiles', side_effect=available), \
             patch('config.env_loader.write_env_values', return_value=[]) as write:
            current = next(f['value'] for f in get_config() if f['key'] == 'WREQ_EMULATION')
            update_config({'PROTOCOL_HTTP_CLIENT': 'curl_cffi', 'WREQ_EMULATION': current})
            self.assertNotIn('WREQ_EMULATION', write.call_args.args[0])
            with self.assertRaises(ValueError):
                update_config({'PROTOCOL_HTTP_CLIENT': 'wreq'})
