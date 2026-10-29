"""Exercise the small integration against real Home Assistant classes."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aiohttp.web_exceptions import HTTPForbidden
from homeassistant import config_entries
from homeassistant.components.http import HomeAssistantView

from custom_components.phone_relay import PhoneRegistrationsView, async_setup_entry
from custom_components.phone_relay.config_flow import PhoneRelayConfigFlow


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_listing_requires_administrator_and_is_not_cacheable(self):
        hass = Mock()
        hass.config_entries.async_entries.return_value = [
            SimpleNamespace(title="Fallback name", data={"webhook_id": "a" * 64}),
            SimpleNamespace(title="Other", data={"device_name": "My phone", "webhook_id": "b" * 64}),
            SimpleNamespace(title="Incomplete", data={}),
        ]
        view = PhoneRegistrationsView(hass)
        self.assertIsInstance(view, HomeAssistantView)
        self.assertTrue(view.requires_auth)
        with self.assertRaises(HTTPForbidden):
            await view.get({"hass_user": SimpleNamespace(is_admin=False)})
        hass.config_entries.async_entries.assert_not_called()
        response = await view.get({"hass_user": SimpleNamespace(is_admin=True)})
        self.assertEqual(json.loads(response.body), [
            {"name": "Fallback name", "webhook_id": "a" * 64},
            {"name": "My phone", "webhook_id": "b" * 64},
        ])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        hass.config_entries.async_entries.assert_called_once_with("mobile_app")

    async def test_setup_registers_only_phone_view(self):
        hass = Mock()
        self.assertTrue(await async_setup_entry(hass, Mock()))
        hass.http.register_view.assert_called_once()
        view = hass.http.register_view.call_args.args[0]
        self.assertEqual(view.url, "/api/phone_relay/phones")

    async def test_configuration_flow_requires_no_cloud_account_and_is_single_instance(self):
        flow = PhoneRelayConfigFlow()
        self.assertIsInstance(flow, config_entries.ConfigFlow)
        with patch.object(flow, "_async_current_entries", return_value=[]):
            self.assertEqual((await flow.async_step_user())["type"], "form")
            result = await flow.async_step_user({})
            self.assertEqual(result["type"], "create_entry")
            self.assertEqual(result["data"], {})
        with patch.object(flow, "_async_current_entries", return_value=[Mock()]):
            self.assertEqual((await flow.async_step_user())["reason"], "single_instance_allowed")
