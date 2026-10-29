import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from addon.connector import Connector, load_config


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.connector = Connector({}, self.temp.name)
        self.addCleanup(self.connector.db.close)
        self.webhook = "a" * 64
        self.phone_id = hashlib.sha256(self.webhook.encode()).hexdigest()
        self.connector.phones[self.phone_id] = {"name": "Phone", "webhook_id": self.webhook}

    def message(self, **overrides):
        return {
            "id": "c" * 32, "phone": self.phone_id,
            "event": {"type": "update_location", "data": {"gps": [1, 2]}},
            "expires": int(time.time()) + 10, **overrides,
        }

    def socket(self):
        socket = AsyncMock()
        socket.closed = False
        return socket

    def result(self, socket):
        return json.loads("".join(call.args[0]["data"] for call in socket.send_json.call_args_list))

    async def test_delivery_and_immediate_local_revocation(self):
        self.connector.ha = AsyncMock(return_value={})
        socket = self.socket()
        await self.connector.dispatch(self.message(), socket)
        self.connector.ha.assert_awaited_once_with(
            "POST", "/api/webhook/" + self.webhook, self.message()["event"],
        )
        self.assertEqual(self.result(socket), {"status": 200, "body": {}})
        self.assertIn(self.phone_id, self.connector.phone_updates)
        self.connector.phones.clear()
        socket = self.socket()
        await self.connector.dispatch(self.message(id="d" * 32), socket)
        self.assertEqual(self.result(socket)["status"], 403)
        self.assertEqual(self.connector.ha.await_count, 1)

    async def test_rejects_nonphone_paths_unsupported_and_stale_events(self):
        self.connector.ha = AsyncMock()
        cases = [
            {"phone": "unenrolled"}, {"phone": []},
            {"path": "/api/alexa/smart_home"},
            {"path": "/api/services/lock/unlock"},
            {"event": {"type": "call_service"}},
            {"event": {"type": "update_location", "data": "x" * 20_000}},
            {"event": []}, {"expires": int(time.time()) - 1},
            {"expires": int(time.time()) + 60}, {"expires": True},
        ]
        for index, override in enumerate(cases):
            socket = self.socket()
            await self.connector.dispatch(self.message(**{**override, "id": f"{index:032x}"}), socket)
            self.assertEqual(self.result(socket)["status"], 403)
        self.connector.ha.assert_not_awaited()

    async def test_duplicate_is_not_replayed_even_after_restart(self):
        self.connector.ha = AsyncMock(return_value={})
        await self.connector.dispatch(self.message(), self.socket())
        second = Connector({}, self.temp.name)
        try:
            second.ha = AsyncMock()
            second.phones = self.connector.phones
            socket = self.socket()
            await second.dispatch(self.message(), socket)
            second.ha.assert_not_awaited()
            self.assertEqual(self.result(socket)["status"], 409)
        finally:
            second.db.close()

    async def test_fragmented_and_oversized_responses(self):
        for length, expected in ((30_000, 200), (256_000, 413)):
            socket = self.socket()
            self.connector.ha = AsyncMock(return_value={"value": "x" * length})
            await self.connector.dispatch(self.message(id=f"{length:032x}"), socket)
            self.assertEqual(self.result(socket)["status"], expected)
            self.assertTrue(all(
                len(json.dumps(call.args[0]).encode()) < 28_000
                for call in socket.send_json.call_args_list
            ))

    async def test_ui_enrollment_csrf_persistence_and_revocation(self):
        self.connector.phones.clear()
        self.connector.ha = AsyncMock(return_value=[{"name": "My phone <test>", "webhook_id": self.webhook}])
        client = TestClient(TestServer(self.connector.application()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/")
        self.assertIn("Enroll My phone &lt;test&gt;", await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        form = {"action": "enroll", "name": "My phone", "webhook_id": self.webhook}
        self.assertEqual((await client.post("/", data=form)).status, 403)
        form["csrf"] = self.connector.csrf
        self.assertEqual((await client.post("/", data=form)).status, 200)
        self.assertIn(self.phone_id, self.connector.phones)
        self.assertEqual(self.connector.phones_path.stat().st_mode & 0o777, 0o600)
        restarted = Connector({}, self.temp.name)
        try:
            self.assertEqual(restarted.phones, self.connector.phones)
        finally:
            restarted.db.close()
        response = await client.post("/", data={
            "action": "remove", "id": self.phone_id, "csrf": self.connector.csrf,
        })
        self.assertEqual(response.status, 200)
        self.assertEqual(self.connector.phones, {})

    async def test_enrollment_requires_local_registration_and_enforces_limit(self):
        self.connector.ha = AsyncMock(return_value=[])
        data = {"action": "enroll", "name": "Phone", "webhook_id": "b" * 64}
        with self.assertRaises(web.HTTPBadRequest):
            await self.connector.enroll(data)
        self.connector.ha.return_value = [{"name": "Phone", "webhook_id": "b" * 64}]
        self.connector.phones = {
            f"{index:064x}": {"name": str(index), "webhook_id": f"{index:064x}"}
            for index in range(20)
        }
        with self.assertRaises(web.HTTPBadRequest):
            await self.connector.enroll(data)
        self.connector.ha.side_effect = RuntimeError("Unavailable")
        with self.assertRaises(web.HTTPServiceUnavailable):
            await self.connector.enroll(data)

    async def test_ui_trust_rejects_dns_rebinding_and_spoofed_ingress(self):
        client = TestClient(TestServer(self.connector.application()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.get("/", headers={"Host": "attacker.example", "X-Ingress-Path": "/fake"})
        self.assertEqual(response.status, 403)
        self.connector.config["supervisor_ingress"] = True
        self.assertEqual((await client.get("/", headers={"X-Ingress-Path": "/fake"})).status, 403)

    async def test_connection_status_and_delivery_errors_are_explicit_and_redacted(self):
        self.connector.config["connector_key"] = "private-connector-key"
        self.connector.ha = AsyncMock(side_effect=RuntimeError(self.webhook + " private-connector-key"))
        with self.assertLogs("phone-relay.connector", level="ERROR") as logs:
            socket = self.socket()
            await self.connector.dispatch(self.message(), socket)
        self.assertEqual(self.result(socket)["status"], 502)
        self.assertNotIn(self.webhook, " ".join(logs.output))
        self.assertNotIn("private-connector-key", " ".join(logs.output))
        self.assertIn("Disconnected", self.connector.cloud_status())
        self.connector.ready = True
        self.assertIn("waiting", self.connector.cloud_status())
        self.connector.last_poll = time.monotonic() - 60
        self.assertIn("stale", self.connector.cloud_status())

    async def test_real_websocket_and_local_webhook_roundtrip_reconnect(self):
        calls, connections = [], []
        received = asyncio.Queue()

        async def ha(request):
            self.assertEqual(request.headers["Authorization"], "Bearer local-token")
            calls.append(await request.json())
            return web.json_response({}, status=201)

        local = web.Application()
        local.router.add_post("/api/webhook/" + self.webhook, ha)
        local_server = TestServer(local)
        await local_server.start_server()
        self.addAsyncCleanup(local_server.close)
        payload = {"type": "register_sensor", "encrypted": True, "encrypted_data": "opaque"}

        async def cloud(request):
            self.assertEqual(request.headers["Authorization"], "Bearer connector-test-key")
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            connections.append(socket)
            await socket.send_json({"type": "request", **self.message(event=payload)})
            async for message in socket:
                body = json.loads(message.data)
                if body.get("action") == "poll":
                    self.assertEqual(body["phones"], [self.phone_id])
                    await socket.send_json({
                        "type": "status", "phones": body["phones"],
                        "phone_relay_url": "https://phone.example",
                    })
                elif body.get("action") == "response":
                    await received.put(body)
            return socket

        cloud_app = web.Application()
        cloud_app.router.add_get("/connector", cloud)
        cloud_server = TestServer(cloud_app)
        await cloud_server.start_server()
        self.addAsyncCleanup(cloud_server.close)
        self.connector.config = {
            "websocket_url": str(cloud_server.make_url("/connector")).replace("http:", "ws:"),
            "connector_key": "connector-test-key", "ha_token": "local-token",
            "ha_url": str(local_server.make_url("/")).rstrip("/"),
        }
        async with aiohttp.ClientSession() as session:
            self.connector.session = session
            task = asyncio.create_task(self.connector.connection())
            try:
                response = await asyncio.wait_for(received.get(), 5)
                self.assertEqual(json.loads(response["data"]), {"status": 200, "body": {}})
                self.assertEqual(calls, [payload])
                self.assertEqual(self.connector.phone_relay_url, "https://phone.example")
                await connections[0].close()
                duplicate = await asyncio.wait_for(received.get(), 5)
                self.assertEqual(json.loads(duplicate["data"])["status"], 409)
                self.assertEqual(calls, [payload])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.gather(*self.connector.tasks, return_exceptions=True)

    async def test_real_local_ha_error_is_not_treated_as_success(self):
        async def unauthorized(request):
            return web.Response(status=401)

        app = web.Application()
        app.router.add_post("/api/webhook/" + self.webhook, unauthorized)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.connector.config = {
            "ha_url": str(server.make_url("/")).rstrip("/"), "ha_token": "expired",
        }
        async with aiohttp.ClientSession() as session:
            self.connector.session = session
            socket = self.socket()
            await self.connector.dispatch(self.message(), socket)
        self.assertEqual(self.result(socket)["status"], 502)
        self.assertNotIn(self.phone_id, self.connector.phone_updates)


class ConfigTests(unittest.TestCase):
    def test_config_normalizes_socket_and_rejects_unsafe_urls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "options.json"
            valid = {
                "websocket_url": " wss://cloud.example/connector/ ", "connector_key": "k" * 48,
                "ha_url": "http://homeassistant.local:8123", "ha_token": "local-admin",
            }
            path.write_text(json.dumps(valid))
            self.assertEqual(load_config(path)["websocket_url"], "wss://cloud.example/connector")
            for value in (None, "https://cloud.example/connector", "wss://cloud.example/",
                          "wss://cloud.example/connector?token=key", "wss://user@cloud.example/connector",
                          "wss://cloud .example/connector"):
                path.write_text(json.dumps({**valid, "websocket_url": value}))
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text(json.dumps({**valid, "supervisor_ingress": True}))
            with self.assertRaises(ValueError):
                load_config(path)
