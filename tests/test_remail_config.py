# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from config import email
from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


class RemailConfigTests(unittest.TestCase):
    def test_email_config_declares_remail_defaults(self):
        source = Path(email.__file__).read_text(encoding="utf-8")
        self.assertIn('REMAIL_API_KEY = env_str("REMAIL_API_KEY", "")', source)
        self.assertIn('REMAIL_API_BASE = "https://remail.aishop6.com"', source)
        self.assertIn("REMAIL_PROJECT_ID = 2", source)
        self.assertIn('REMAIL_SERVICE_MODE = "purchase"', source)
        self.assertIn('REMAIL_SUPPLY_POLICY = "public_only"', source)
        self.assertIn('"remail"', source)

    def test_secret_registry_includes_remail_api_key(self):
        self.assertEqual(SECRET_ENV_KEYS["REMAIL_API_KEY"], "Remail 开放 API Key")

    def test_webui_exposes_remail_fields(self):
        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        self.assertEqual(fields["REMAIL_API_BASE"]["group"], "邮箱 / OTP")
        self.assertEqual(
            fields["REMAIL_API_BASE"]["external_url"],
            "https://remail.aishop6.com/register?aff=AFFLGYQMTYIXH",
        )
        self.assertTrue(fields["REMAIL_API_KEY"]["secret"])
        self.assertEqual(fields["REMAIL_API_KEY"]["storage"], "env")
        self.assertEqual(fields["REMAIL_PROJECT_ID"]["type"], "int")
        self.assertEqual(fields["REMAIL_EMAIL_SUFFIX"]["type"], "str")

    def test_remail_promo_is_rendered_after_section_help(self):
        modern = Path("webui/templates/index.html").read_text(encoding="utf-8")
        legacy = Path("webui/templates/index_legacy.html").read_text(encoding="utf-8")
        promo = "通过作者卡网购买积分9折优惠"
        self.assertIn(promo, modern)
        self.assertIn(promo, legacy)
        self.assertIn(
            'const sectionHelp = current.help\n    ? `<p class="config-section-v2-subhelp">${esc(current.help)}${promo}</p>`\n    : promo;',
            modern,
        )
        self.assertIn(
            '${help ? `<span class="hint">${esc(help)}</span>` : \'\'}${promo}',
            legacy,
        )


if __name__ == "__main__":
    unittest.main()
