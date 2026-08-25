# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock

from core.cloakbrowser_driver import CloakElement
from core import roxy_registration


class CloakElementTests(unittest.TestCase):
    def test_send_keys_appends_with_keyboard_type(self):
        page = Mock()
        locator = Mock()
        element = CloakElement(page=page, locator=locator)

        element.send_keys("a")
        element.send_keys("b")
        element.send_keys("c")

        self.assertEqual(page.keyboard.type.call_args_list[0].args, ("a",))
        self.assertEqual(page.keyboard.type.call_args_list[1].args, ("b",))
        self.assertEqual(page.keyboard.type.call_args_list[2].args, ("c",))
        locator.fill.assert_not_called()

    def test_send_keys_uses_fill_only_when_keyboard_fails(self):
        page = Mock()
        page.keyboard.type.side_effect = RuntimeError("keyboard unavailable")
        locator = Mock()
        element = CloakElement(page=page, locator=locator)

        element.send_keys("email@example.com")

        locator.fill.assert_called_once_with("email@example.com", timeout=10000)

    def test_send_keys_maps_backspace_and_control_a_to_keyboard_shortcuts(self):
        page = Mock()
        element = CloakElement(page=page, locator=Mock())

        element.send_keys("\ue003")
        element.send_keys("\ue009", "a")

        self.assertEqual(page.keyboard.press.call_args_list[0].args, ("Backspace",))
        self.assertEqual(page.keyboard.press.call_args_list[1].args, ("Control+A",))

    def test_human_type_uses_atomic_setter_for_cloak_adapter(self):
        driver = Mock()
        driver._is_cloak_selenium_adapter = True
        element = Mock()
        with unittest.mock.patch("core.roxy_registration._set_element_value") as set_value:
            roxy_registration._human_type_text(driver, element, "mail@example.com")
        set_value.assert_called_once_with(driver, element, "mail@example.com")


if __name__ == "__main__":
    unittest.main()
