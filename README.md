# Home Assistant Phone Relay

Send location and sensor updates from the official Home Assistant Companion app
to your local HA instance without a VPN, router port forwarding, or a public HA
server. No Alexa skills, Amazon developer account, or account linking.

**Experimental.** Automated transport, Home Assistant integration, and mocked
infrastructure tests are included. A live AWS/HA OS deployment and locked-phone
arrival/departure testing are still required before relying on presence automations.

```text
Companion app -> AWS HTTPS webhook -> relay Lambda
                                       |
                              API Gateway WebSocket
                                       |
                            outbound local connector
                                       |
                            HA's native mobile_app
                                       |
                             tracker / Person / zones
```

This is **phone location and sensor synchronization, not remote dashboard access**.
The phone must already be registered with HA locally. Local HA continues to manage
its native tracker, Person, zones, and automations.

## What is included

- One Lambda, one on-demand DynamoDB table, and HTTP/WebSocket API Gateways,
  provisioned with Terraform. No always-on cloud server or general-purpose tunnel.
- A HA OS add-on with an ingress UI to enroll/revoke individual phones (up to 20).
- A small `phone_relay` HA integration that lets administrators list local
  Companion registrations for enrollment. It has no cloud credentials or entity
  configuration.
- A standalone connector command for non-HA OS installations.

The only public HTTP route is `POST /api/webhook/{webhook_id}`. Both cloud and
connector accept only `update_location`, `get_config`, `get_zones`,
`update_registration`, `register_sensor`, and `update_sensor_states`.
There are no OAuth routes, Alexa command handlers, service-call forwarding,
remote login/token refresh, or frontend proxy.

## 1. Provision a separate AWS deployment

Requirements: Terraform >= 1.7 and < 2, AWS CLI v2, Python >= 3.11 for key
generation, and deployment credentials for your chosen AWS account. Runtime
infrastructure uses Python 3.13 on Lambda.

```sh
git clone https://github.com/AdamDrewsTR/home-assistant-phone-relay.git
cd home-assistant-phone-relay
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`, especially **`aws_account_id`**, to match the intended
account. Set `aws_region` to a region with API Gateway HTTP and WebSocket support.
The default resource prefix is `ha-phone-relay`. Use a unique prefix for each home.

Select credentials using your normal AWS profile or environment. Check
`aws sts get-caller-identity` before proceeding. Use the same profile/region for
SSM and Terraform; the examples below assume the default name and `us-east-1`.
Prefer short-lived deployment credentials.

Create a dedicated random connector key in SSM SecureString **once**, outside
Terraform state. Review the command before running it: it writes a new parameter
and deliberately does not overwrite an existing one.

```sh
python3 -c 'import json,secrets; print(json.dumps({"Name":"/ha-phone-relay/connector-key","Type":"SecureString","Value":secrets.token_urlsafe(48)}))' \
  | aws ssm put-parameter --region us-east-1 --cli-input-json file:///dev/stdin

terraform init
terraform plan -out=phone.tfplan
# Review the plan before applying:
terraform apply phone.tfplan
terraform output
```

Adjust the SSM parameter name if you change `name`. The key stays out of command
history, Terraform variables, and Terraform state. To copy it into your local
connector configuration, display it only in a private terminal:

```sh
aws ssm get-parameter --region us-east-1 \
  --name /ha-phone-relay/connector-key --with-decryption \
  --query Parameter.Value --output text
```

Do not share this output or phone webhook URLs. The Lambda reads only that SSM
parameter at runtime. No HA access token is stored in AWS.

Keep the Terraform state and credentials private and backed up. Do not copy an
existing Alexa deployment's state, `.setup` directory, secrets, or resource name
into this checkout. `prevent_destroy` protects the two API gateways and DynamoDB
table against accidental removal; intentional teardown requires reviewing and
removing those lifecycle guards first. AWS charges apply: WebSocket connection
minutes, ten-second enrollment polls, Lambda, API requests, logs, and DynamoDB.
Set an AWS Budget; this is inexpensive at small scale, not guaranteed free.

## 2. Install the local HA integration

The connector needs a small administrator-only registration endpoint; it does not
need the original Alexa integration.

1. Copy the repository's entire `custom_components/phone_relay` directory into
   your HA configuration directory's `custom_components/phone_relay`. On HA OS,
   use your existing Samba/SSH/file-management workflow. On HA Container, this
   is normally `/config/custom_components/phone_relay` inside the HA container.
2. Restart **HA Core** to load the custom integration.
3. In **Settings > Devices & services > Add integration**, select **Phone Relay**
   and submit. No credentials or YAML changes are needed.
4. Ensure the official Companion app is registered with HA while on home Wi-Fi.
   Keep an existing phone registration; do not delete/re-add it.

The integration exposes only `/api/phone_relay/phones`, requires authenticated
HA administrator access, and returns `Cache-Control: no-store`. It does not expose
phones to the internet. Removing/updating it requires an HA restart to remove or
reload its HTTP view.

## 3. Install and configure the connector

### HA OS

Add `https://github.com/AdamDrewsTR/home-assistant-phone-relay` under
**Settings > Apps (or Add-ons) > App store > Repositories**. Install **Phone Relay**.
The add-on builds locally for `amd64` or `aarch64`.

Configure:

```yaml
websocket_url: wss://YOUR-SOCKET-API.execute-api.us-east-1.amazonaws.com/connector
connector_key: YOUR_PRIVATE_SSM_CONNECTOR_KEY
supervisor_ingress: true
```

Use Terraform's **`websocket_url`** output, including `/connector`. Start the
add-on and open its Web UI. It uses Supervisor's local HA access; no HA token
needs to be copied. The add-on does not modify HA's configuration files.

### Standalone (HA Container/Core)

Run the connector on the HA host or another trusted local machine:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Create a private `options.json` (excluded from git), with filesystem permissions
restricted to your account, and a local administrator long-lived HA access token:

```json
{
  "websocket_url": "wss://YOUR-SOCKET-API.execute-api.us-east-1.amazonaws.com/connector",
  "connector_key": "YOUR_PRIVATE_SSM_CONNECTOR_KEY",
  "ha_url": "http://homeassistant.local:8123",
  "ha_token": "YOUR_LOCAL_ADMIN_TOKEN",
  "supervisor_ingress": false
}
```

```sh
chmod 600 options.json
.venv/bin/ha-phone-relay --config options.json --data-dir .local-data
```

Open `http://127.0.0.1:8099` on that machine. The standalone UI binds only to
loopback and validates its Host header; do not publish it through a proxy.
Keep the data directory private and persistent. It stores enrolled webhook IDs
with owner-only permissions and request IDs for duplicate suppression, not
location history. The HA token stays local; replace it if revoked.

## 4. Enroll and configure the phone

In the connector UI, enroll the intended phone and refresh until **Cloud enrolled**
appears. Use the displayed external URL or Terraform's **`phone_external_url`**
output for the Companion app's external address. The phone's HTTPS API ID and
the connector's WSS API ID are different.

Keep the Companion app's internal URL set to your working local HA address and
configure your home Wi-Fi networks **before changing its external address**.
Do not set HA's global external/network URL to this restricted relay.
Enable background/Always and precise location permissions. Associate the existing
`device_tracker` with your HA Person.

Read [phone setup and acceptance tests](PHONE_LOCATION.md) for platform settings,
cellular verification, and troubleshooting.

## Limitations and privacy

- Remote dashboards, registration, login, and HA token refresh are unavailable.
  At home the app must use its internal URL for these. An existing HA Cloud
  cloudhook can take precedence over the external URL.
- No cloud location history or offline delivery queue. Disconnected requests
  fail explicitly. Enrollment expires after two minutes without connector
  polling; revocation blocks subsequent requests locally immediately.
- The ten-second poll synchronizes enrollment, **not GPS**. Background location
  scheduling and retry behavior remain controlled by Android/iOS and the app.
  Receipt time is not a reliable GPS measurement timestamp.
- iOS webhook encryption is passed through unchanged. The source project's
  compatibility review covered Companion Android/iOS 2026.10.1; Android in that
  review sent HTTPS payloads without end-to-end encryption, so AWS can see them.
  Phone support remains experimental on both platforms.
- Webhook IDs are bearer credentials in request URLs. The cloud enrollment list
  contains hashes, but AWS API Gateway still receives the actual webhook URL.
  Do not enable API request/body/access logging that records these credentials.
  Application logs omit webhook IDs and location bodies. Short-lived DynamoDB
  response fragments may contain configuration, zone, or sensor-registration
  data; logical expiry is enforced even before AWS physically removes TTL rows.
- Compromise of the connector or AWS account can affect the permitted mobile
  webhook operations. Limit access and use only with consenting phone owners.
  Missed updates leave the last-known location; never use GPS alone to unlock
  doors or for safety-critical decisions.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
terraform init -backend=false -input=false
terraform fmt -check -recursive
terraform validate
terraform test
docker build -t ha-phone-relay:test addon
```

HA integration compatibility tests run separately with Python 3.14 and
Home Assistant 2026.10.3:

```sh
python3.14 -m venv .venv-ha
.venv-ha/bin/python -m pip install -r tests/ha_runtime/requirements.txt
.venv-ha/bin/python -m unittest discover -s tests/ha_runtime -v
```

GitHub Actions runs both suites, mocked Terraform tests, and a container build.
Tests never apply AWS infrastructure or contact a real phone/HA installation.

Extracted from [alexa_home_assistant](https://github.com/AdamDrewsTR/alexa_home_assistant),
source commit `685538be05acca31c45d5a83436a8e76d9e23575`, under the MIT license.
The original repository and its deployment remain independent. This is a fresh
deployment, not an automatic migration of existing phones or infrastructure.
