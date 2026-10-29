"""Expose local mobile-app registrations to the administrator-only enrollment UI."""

from aiohttp.web_exceptions import HTTPForbidden
from homeassistant.components.http import HomeAssistantView

DOMAIN = "phone_relay"


class PhoneRegistrationsView(HomeAssistantView):
    url = "/api/phone_relay/phones"
    name = "api:phone_relay:phones"

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request):
        if not request["hass_user"].is_admin:
            raise HTTPForbidden()
        response = self.json([
            {"name": entry.data.get("device_name", entry.title),
             "webhook_id": entry.data["webhook_id"]}
            for entry in self.hass.config_entries.async_entries("mobile_app")
            if entry.data.get("webhook_id")
        ])
        response.headers["Cache-Control"] = "no-store"
        return response


async def async_setup_entry(hass, entry):
    hass.http.register_view(PhoneRegistrationsView(hass))
    return True
