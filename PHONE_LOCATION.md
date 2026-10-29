# Phone setup, verification, and troubleshooting

This relay supports already-registered official Companion apps. It forwards
location/sensor webhooks to HA's native `mobile_app`, not dashboards, login,
or arbitrary HA API requests.

## Settings checklist

| Setting | Where | Value |
|---|---|---|
| Connector WebSocket | Phone Relay add-on Configuration | Terraform `websocket_url`: `wss://<socket-api>.execute-api.<region>.amazonaws.com/connector` |
| Connector key | Same add-on Configuration | Matching dedicated SSM connector key |
| Local registration | HA > Devices & services > Mobile App | Keep the existing registered phone |
| Relay enrollment | Phone Relay Web UI | Enroll intended phone; refresh until **Cloud enrolled** |
| Phone external address | Companion app server connection settings | Terraform `phone_external_url`, or HTTPS origin shown by connector |
| Phone internal address | Companion app home network/connection settings | Working local HA URL, including port where needed |
| Home networks | Same phone settings | Your home Wi-Fi SSIDs |
| Background permission | Phone OS app permissions | Always/background location, with precise location enabled |
| Location collection | Companion app sensor settings | Background location and zone-based tracking enabled |
| Person association | HA > Settings > People | Select the existing phone tracker |

Phone menu labels vary by OS/app version. On Android versions with **Home
Assistant URL**, **Home network**, and **Persistent connection**, set your home
Wi-Fi and **Internal connection URL** under **Home network** first. Then set
the main **Home Assistant URL** to the relay's external HTTPS origin. Leave
Persistent connection unchanged for this setup. Location permissions are also
needed for home Wi-Fi detection.

On iOS, configure the separate internal and external connection addresses and
home network selection, and grant Always/precise location. The relay supports
the `get_config` webhook used by the app's URL check, but cannot provide remote
authentication maintenance.

Do not overwrite a working local address until you have identified the separate
internal setting. Do not append `/connector` or a webhook path to the phone's
external address. Do not use this URL as HA's global external URL.
An existing HA Cloud webhook may take precedence.

## Verify delivery, not just enrollment

1. On home Wi-Fi, confirm the dashboard works and the phone's `device_tracker`
   has real coordinates in **Developer Tools > States**. An `unknown` state
   without coordinates means it does not yet have a usable fix.
2. Turn off the phone's Wi-Fi and leave mobile data enabled.
3. From local HA, use the phone's existing `notify.mobile_app_...` action:

   ```yaml
   message: request_location_update
   ```

4. Manually refresh the connector UI. Look for **last location accepted ...
   seconds ago**, then verify the actual tracker coordinates in HA.
5. Test real departure/arrival with the phone locked. Repeat after restarting
   the connector and after the app's HA access token has expired. Verify
   automatic background updates, not just a requested update.

The notification uses push delivery, not the dashboard. Android/iOS scheduling
and push delivery can delay the response. Turning Wi-Fi off while physically
at home should still report `home`: Wi-Fi networks are not geographic zones.
Configure zones in HA's zone settings.

**Cloud enrolled** means enrollment synchronized, not that any GPS reached HA.
The receipt counter records successful local webhook responses and resets on
connector restart; it cannot prove the underlying GPS measurement is fresh.
HA's Last updated can remain unchanged for identical states/attributes.
Do not use Developer Tools **Set state** as a delivery test.

The connector's ten-second polling interval does not control phone GPS.
Several minutes without movement can be normal. There is no live-map feed or
offline relay queue; missed updates leave the last-known location.

## Troubleshooting

| Symptom | Check |
|---|---|
| Disconnected | WSS URL must end in `/connector`; use the matching key/account/region and inspect connector logs. Do not blindly rotate keys or recreate APIs. |
| Phone list unavailable | Install `custom_components/phone_relay`, restart HA Core, add Phone Relay under Devices & services, and verify local administrator access. Restarting only the add-on does not load an HA integration. |
| Cloud enrolled, no accepted location | Verify phone permissions, per-server sensors, home network selection, and external URL. Run the controlled cellular test; inspect the app's location troubleshooting history. |
| Waiting for cloud sync | Refresh after the next poll. Ensure the deployed cloud relay and add-on both come from this repository. This protocol is deliberately separate from the Alexa relay. |
| No phone external URL | Check cloud deployment and Lambda `HTTP_URL`. Updating the add-on alone does not update AWS. |
| Dashboard fails only on cellular | Expected: the relay does not serve the frontend or authentication. Do not log out or delete the server registration. |
| Dashboard fails at home | Check internal address, selected home Wi-Fi networks, and OS permissions. The relay cannot replace local HA access. |
| Delivery fails after HA/app restore | Keep the current registration if possible. If it changed, revoke old relay enrollment and enroll the new registration. |
| Standalone local HTTP 401/403 | Check the local administrator token; replace it if revoked. HA tokens never belong in the cloud configuration. |

Use the saved state from **this** repository when updating its AWS resources.
Never reuse the Alexa project's Terraform state or change accounts to work around
an error. A new standalone deployment has different HTTPS/WSS URLs and a different
connector key; enrolling here does not update the phone's external URL for you.

For official behavior, see Companion
[network selection](https://companion.home-assistant.io/docs/troubleshooting/networking/#setting-up-the-app)
and [location documentation](https://companion.home-assistant.io/docs/core/location/).
