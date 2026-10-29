# Phone Relay

An outbound-only connection for enrolled Companion phone location and sensor
webhooks. No Alexa skills or account linking. It does not provide remote
dashboards, login, or a general Home Assistant API tunnel.

## Before starting

Deploy the independent AWS stack from this repository, then install
`custom_components/phone_relay` into HA's configuration directory.
Restart HA Core and add **Phone Relay** under **Settings > Devices & services**.
Register the official Companion app with local HA, keeping any existing registration.

Full deployment/integration instructions:
https://github.com/AdamDrewsTR/home-assistant-phone-relay#readme

## Configuration

- `websocket_url`: Terraform's `websocket_url` output, including `/connector`.
- `connector_key`: Matching dedicated SSM SecureString value; keep private.
- `supervisor_ingress`: Leave `true` for HA OS.

The add-on uses Supervisor's local HA authorization. It does not modify
configuration files or need a manually copied HA token.
Do not expose its management UI outside HA ingress.

## Phone setup

Open the Web UI, enroll an already-registered phone, and refresh until
**Cloud enrolled** appears. Configure the Companion app's external address with
the displayed HTTPS origin, keeping its separate internal URL and home Wi-Fi
networks configured. Do not change HA's global external URL.

Grant background/Always and precise location permissions; enable background and
zone-based location collection. Associate the existing tracker with its HA Person.
On cellular, a failed dashboard is expected. Verify delivery through the tracker
and local UI, then test actual locked-phone arrival/departure.

Detailed checklist:
https://github.com/AdamDrewsTR/home-assistant-phone-relay/blob/main/PHONE_LOCATION.md

## Safety and privacy

Only enroll consenting owners' phones. Revocation blocks subsequent forwarding
locally immediately. Enrollment expires in AWS after two minutes without polling.
The ten-second poll is not a GPS interval. Receipt times reset on restart and do
not prove measurement freshness. There is no offline queue or location history.
Use conservative presence automations; never GPS alone to unlock doors.

Local `/data/phones.json` contains private webhook credentials with owner-only
permissions. Add-on/HA backups can contain those credentials: keep them private.
AWS sees incoming webhook URLs and, for apps without payload encryption, location
payloads. Do not enable cloud request-body/access logging.
