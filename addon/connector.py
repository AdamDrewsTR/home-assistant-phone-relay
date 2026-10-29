"""Outbound phone-only connector with a local, administrator-facing enrollment UI."""

import argparse
import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

LOGGER = logging.getLogger("phone-relay.connector")
MAX_MESSAGE = 28_000
MAX_RESPONSE = 256_000
RESPONSE_CHUNK = 12_000
PHONE_TYPES = {
    "update_location", "get_config", "get_zones", "update_registration",
    "register_sensor", "update_sensor_states",
}
REGISTRATIONS_PATH = "/api/phone_relay/phones"


def atomic_write(path, text):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Connector:
    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "requests.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, created REAL)")
        self.db.commit()
        self.phones_path = self.data_dir / "phones.json"
        self.phones = json.loads(self.phones_path.read_text()) if self.phones_path.exists() else {}
        self.ws = None
        self.session = None
        self.csrf = secrets.token_urlsafe(32)
        self.tasks = set()
        self.semaphore = asyncio.Semaphore(4)
        self.enrollment_lock = asyncio.Lock()
        self.ready = False
        self.last_poll = 0
        self.synced_phones = []
        self.phone_relay_url = ""
        self.phone_updates = {}

    def redact(self, text):
        for value in (
            self.config.get("connector_key"), self.config.get("ha_token"),
            os.environ.get("SUPERVISOR_TOKEN"),
            *(phone["webhook_id"] for phone in self.phones.values()),
        ):
            if value:
                text = text.replace(value, "[redacted]")
        return text

    async def ha(self, method, path, body=None):
        if os.environ.get("SUPERVISOR_TOKEN"):
            url, token = "http://supervisor/core" + path, os.environ["SUPERVISOR_TOKEN"]
        else:
            url = self.config["ha_url"].rstrip("/") + path
            token = self.config["ha_token"]
        async with self.session.request(
            method, url, json=body, headers={"Authorization": "Bearer " + token},
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=3),
        ) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Local Home Assistant returned HTTP {response.status}")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise RuntimeError("Local Home Assistant response exceeds size limit")
            return json.loads(raw)

    async def respond(self, socket, request_id, result):
        raw = json.dumps(result)
        if len(raw) > MAX_RESPONSE:
            LOGGER.error("Phone response exceeds relay size limit")
            raw = json.dumps({"status": 413})
        for index, offset in enumerate(range(0, len(raw), RESPONSE_CHUNK)):
            if socket.closed:
                break
            await socket.send_json({
                "action": "response", "id": request_id, "index": index,
                "total": (len(raw) + RESPONSE_CHUNK - 1) // RESPONSE_CHUNK,
                "data": raw[offset:offset + RESPONSE_CHUNK],
            })

    async def dispatch(self, message, socket):
        async with self.semaphore:
            request_id = message.get("id", "")
            if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
                LOGGER.warning("Rejected invalid phone request ID")
                return
            self.db.execute("DELETE FROM seen WHERE created < ?", (time.time() - 86400,))
            try:
                # Persist before forwarding: an uncertain outcome must not be replayed.
                self.db.execute("INSERT INTO seen VALUES (?, ?)", (request_id, time.time()))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                LOGGER.warning("Duplicate phone request rejected")
                await self.respond(socket, request_id, {"status": 409})
                return
            phone_id = message.get("phone")
            phone = self.phones.get(phone_id) if isinstance(phone_id, str) else None
            event = message.get("event")
            if (not phone or "path" in message or not isinstance(event, dict)
                    or not isinstance(event.get("type"), str) or event["type"] not in PHONE_TYPES
                    or len(json.dumps(event).encode()) > 20_000
                    or type(message.get("expires")) is not int
                    or not time.time() < message["expires"] <= time.time() + 15):
                LOGGER.warning("Rejected unenrolled, expired, or unsupported phone request")
                await self.respond(socket, request_id, {"status": 403})
                return
            try:
                body = await self.ha("POST", "/api/webhook/" + phone["webhook_id"], event)
                result = {"status": 200, "body": body}
                if event["type"] == "update_location":
                    self.phone_updates[phone_id] = time.time()
            except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exception:
                LOGGER.error("Phone delivery failed: %s", self.redact(str(exception)) or type(exception).__name__)
                result = {"status": 502}
            await self.respond(socket, request_id, result)

    def completed(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            LOGGER.error("Phone request interrupted; no automatic replay")

    async def poll(self, socket):
        while not socket.closed:
            await socket.send_json({"action": "poll", "phones": list(self.phones)})
            await asyncio.sleep(10)

    async def connection(self):
        delay = 1
        while True:
            try:
                async with self.session.ws_connect(
                    self.config["websocket_url"],
                    headers={"Authorization": "Bearer " + self.config["connector_key"]},
                    heartbeat=60, max_msg_size=MAX_MESSAGE,
                ) as socket:
                    self.ws, self.ready, self.last_poll = socket, True, 0
                    delay = 1
                    LOGGER.info("Phone relay connected")
                    poll = asyncio.create_task(self.poll(socket))
                    try:
                        async for message in socket:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                body = json.loads(message.data)
                                if not isinstance(body, dict):
                                    raise ValueError("Invalid cloud message")
                                if body.get("type") == "status":
                                    self.last_poll = time.monotonic()
                                    self.synced_phones = body.get("phones", [])
                                    self.phone_relay_url = body.get("phone_relay_url", "")
                                elif body.get("type") == "request":
                                    if len(self.tasks) >= 4:
                                        LOGGER.warning("Phone connector busy; rejecting excess request")
                                        await self.respond(socket, body.get("id"), {"status": 429})
                                    else:
                                        task = asyncio.create_task(self.dispatch(body, socket))
                                        self.tasks.add(task)
                                        task.add_done_callback(self.completed)
                            elif message.type == aiohttp.WSMsgType.ERROR:
                                raise ConnectionError("WebSocket interrupted")
                    finally:
                        poll.cancel()
                        results = await asyncio.gather(poll, return_exceptions=True)
                        if isinstance(results[0], Exception):
                            LOGGER.error("Phone enrollment polling failed")
            except (aiohttp.ClientError, ConnectionError, TimeoutError, ValueError):
                LOGGER.error("Cloud connection unavailable; reconnecting with backoff")
            finally:
                self.ready, self.ws = False, None
                self.synced_phones = []
            await asyncio.sleep(delay + secrets.randbelow(1000) / 1000)
            delay = min(delay * 2, 60)

    def trusted(self, request):
        if not request.remote:
            return False
        if self.config.get("supervisor_ingress"):
            return request.remote == "172.30.32.2" and bool(request.headers.get("X-Ingress-Path"))
        try:
            return (ipaddress.ip_address(request.remote).is_loopback
                    and urlsplit("//" + request.host).hostname in ("127.0.0.1", "localhost", "::1"))
        except ValueError:
            return False

    def cloud_status(self):
        if not self.ready:
            return "Disconnected. Check the connector WSS URL and key."
        if not self.last_poll:
            return "Connected; waiting for the first cloud status reply."
        if time.monotonic() - self.last_poll > 30:
            return "Cloud enrollment status is stale. Check connector logs."
        return ""

    async def enroll(self, data):
        async with self.enrollment_lock:
            phones = dict(self.phones)
            if data.get("action") == "enroll":
                webhook, name = data.get("webhook_id", "").strip(), data.get("name", "").strip()
                if not re.fullmatch(r"[a-f0-9]{64}", webhook) or not 1 <= len(name) <= 80:
                    raise web.HTTPBadRequest(text="Invalid phone name or registration.")
                try:
                    candidates = await self.ha("GET", REGISTRATIONS_PATH)
                except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exception:
                    LOGGER.warning("Phone registration verification unavailable: %s", self.redact(str(exception)))
                    raise web.HTTPServiceUnavailable(text="Cannot verify phone registration. Check the Phone Relay integration.") from exception
                if not any(candidate["webhook_id"] == webhook for candidate in candidates):
                    raise web.HTTPBadRequest(text="Register this phone with local HA first.")
                key = hashlib.sha256(webhook.encode()).hexdigest()
                if key not in phones and len(phones) >= 20:
                    raise web.HTTPBadRequest(text="At most 20 phones may be enrolled.")
                phones[key] = {"name": name, "webhook_id": webhook}
            elif data.get("action") == "remove":
                phones.pop(data.get("id", ""), None)
            else:
                raise web.HTTPBadRequest(text="Unsupported enrollment action.")
            atomic_write(self.phones_path, json.dumps(phones))
            self.phones = phones
            if self.ws and not self.ws.closed:
                try:
                    await self.ws.send_json({"action": "poll", "phones": list(phones)})
                except (aiohttp.ClientError, ConnectionError) as exception:
                    LOGGER.warning("Enrollment saved locally; cloud sync pending (%s)", type(exception).__name__)

    def form(self, action, label, **fields):
        fields.update(action=action, csrf=self.csrf)
        return ("<form method='post'>" + "".join(
            f"<input type='hidden' name='{html.escape(key, quote=True)}' "
            f"value='{html.escape(value, quote=True)}'>" for key, value in fields.items()
        ) + "<button>" + html.escape(label) + "</button></form>")

    async def ui(self, request):
        if not self.trusted(request):
            raise web.HTTPForbidden()
        if request.method == "POST":
            data = await request.post()
            if not hmac.compare_digest(data.get("csrf", ""), self.csrf):
                raise web.HTTPForbidden()
            await self.enroll(data)
            raise web.HTTPSeeOther(location="./")
        content = (
            "<h1>Home Assistant Phone Relay</h1>"
            "<p>Experimental: Companion location and sensor updates only. No remote dashboard, "
            "login, service calls, or general HA API access.</p>"
            "<p>Register the Companion app with HA locally first. Keep its internal URL and home Wi-Fi "
            "configured; use this external URL in the phone app, not HA's global network settings:</p>"
        )
        status = self.cloud_status()
        if status:
            content += "<p role='status'>" + html.escape(status) + "</p>"
        if self.phone_relay_url:
            content += "<pre>" + html.escape(self.phone_relay_url) + "</pre>"
        elif not status:
            content += "<p>Cloud replied without a phone URL. Check the deployed relay's HTTP_URL setting.</p>"
        content += (
            "<p>Enable Always/background and precise location permissions. Associate the existing "
            "tracker with your HA Person. Refresh this page manually to update status. "
            "The ten-second enrollment poll is not a GPS interval.</p>"
        )
        for key, phone in self.phones.items():
            enrolled = "Cloud enrolled" if not status and key in self.synced_phones else "Waiting for cloud sync"
            updated = self.phone_updates.get(key)
            age = f"; last location accepted {int(time.time() - updated)} seconds ago" if updated else "; no location received this session"
            content += "<h2>" + html.escape(phone["name"]) + "</h2><p>" + enrolled + age + "</p>"
            content += self.form("remove", "Revoke phone", id=key)
        content += (
            "<h2>Enroll a phone</h2><p>Only enroll phones whose owners consent to sharing their location. "
            "Each phone must grant its own background location permission.</p>"
        )
        try:
            candidates = await self.ha("GET", REGISTRATIONS_PATH)
            for candidate in candidates:
                if hashlib.sha256(candidate["webhook_id"].encode()).hexdigest() not in self.phones:
                    content += self.form("enroll", "Enroll " + candidate["name"], **candidate)
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exception:
            LOGGER.warning("Phone list unavailable: %s", self.redact(str(exception)))
            content += (
                "<p>Phone list unavailable. Install custom_components/phone_relay from this repository, "
                "restart HA Core, and add Phone Relay under Settings &gt; Devices &amp; services. "
                "The local HA connection must have administrator access.</p>"
            )
        content += (
            "<p>Enrollment is not delivery. On cellular, send request_location_update using the phone's "
            "notify.mobile_app action from local HA, then check tracker coordinates and this page. "
            "Receipt times reset on restart and do not prove GPS freshness. Test actual locked-phone "
            "arrival and departure before relying on automations.</p>"
            "<p>Revocation is immediate locally. Cloud enrollment expires after two minutes without polling. "
            "There is no location history or offline queue. Missed updates leave the last-known location; "
            "never rely on GPS alone to unlock a door.</p>"
        )
        return web.Response(
            text="<!doctype html><html lang='en'><meta charset='utf-8'>"
                 "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                 "<title>Phone Relay</title><body><main>" + content + "</main></body></html>",
            content_type="text/html", headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'",
            },
        )

    async def health(self, request):
        return web.json_response({
            "alive": True, "connected": self.ready,
            "poll_age_seconds": int(time.monotonic() - self.last_poll) if self.last_poll else None,
        })

    def application(self):
        app = web.Application(client_max_size=16_384)
        app.router.add_get("/", self.ui)
        app.router.add_post("/", self.ui)
        app.router.add_get("/health", self.health)
        return app

    async def start(self):
        self.session = aiohttp.ClientSession()
        runner = web.AppRunner(self.application(), access_log=None)
        await runner.setup()
        host = "0.0.0.0" if self.config.get("supervisor_ingress") else "127.0.0.1"
        try:
            await web.TCPSite(runner, host, self.config.get("ui_port", 8099)).start()
            await self.connection()
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await runner.cleanup()
            await self.session.close()
            self.db.close()


def load_config(path):
    config = json.loads(Path(path).read_text())
    value = config.get("websocket_url")
    if not isinstance(value, str):
        raise ValueError("Provide a wss:// endpoint ending in /connector")
    config["websocket_url"] = value.strip().removesuffix("/")
    url = urlsplit(config["websocket_url"])
    if (url.scheme != "wss" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path != "/connector"
            or any(character.isspace() for character in config["websocket_url"])):
        raise ValueError("Provide a wss:// endpoint ending in /connector without credentials, query, or fragment")
    if not isinstance(config.get("connector_key"), str) or len(config["connector_key"]) < 32:
        raise ValueError("Connector key must be at least 32 characters")
    if not os.environ.get("SUPERVISOR_TOKEN"):
        ha_url = urlsplit(config.get("ha_url", ""))
        if (ha_url.scheme not in ("http", "https") or not ha_url.hostname
                or ha_url.username or ha_url.password or ha_url.path not in ("", "/")
                or ha_url.query or ha_url.fragment or not config.get("ha_token")):
            raise ValueError("Outside HA OS provide a local HA HTTP(S) origin and administrator ha_token")
        if config.get("supervisor_ingress"):
            raise ValueError("Supervisor ingress requires HA OS and a Supervisor token")
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/data/options.json")
    parser.add_argument("--data-dir", default="/data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(Connector(load_config(args.config), args.data_dir).start())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError) as exception:
        parser.exit(1, f"Phone relay configuration error: {exception}\n")


if __name__ == "__main__":
    main()
