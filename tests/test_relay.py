import base64
import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws

SPEC = importlib.util.spec_from_file_location(
    "relay", Path(__file__).resolve().parents[1] / "lambda" / "relay.py",
)
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


class RelayTests(unittest.TestCase):
    def setUp(self):
        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)
        env = patch.dict(os.environ, {
            "AWS_DEFAULT_REGION": "us-east-1", "TABLE": "relay",
            "CONNECTOR_PARAMETER": "/phone/connector-key",
            "SOCKET_ENDPOINT": "https://example.execute-api.us-east-1.amazonaws.com/connector",
            "HTTP_URL": "https://phones.execute-api.us-east-1.amazonaws.com",
        })
        env.start()
        self.addCleanup(env.stop)
        relay.PARAMETERS.clear()
        relay.table.cache_clear()
        relay.aws_client.cache_clear()
        boto3.resource("dynamodb").create_table(
            TableName="relay", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("ssm").put_parameter(
            Name="/phone/connector-key", Type="SecureString", Value="test-connector-key",
        )

    def socket(self, body=None, connection="home", route="$default", headers=None):
        return relay.handler({
            "requestContext": {"connectionId": connection, "routeKey": route},
            "headers": headers or {}, "body": json.dumps(body or {}),
        }, None)

    def connect(self, connection="home"):
        return self.socket(
            route="$connect", connection=connection,
            headers={"Authorization": "Bearer test-connector-key"},
        )

    def enroll(self, phones=None):
        self.assertEqual(self.connect()["statusCode"], 200)
        with patch.object(relay, "send") as send:
            result = self.socket({"action": "poll", "phones": phones or [relay.digest("a" * 64)]})
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(send.call_args.args[1]["type"], "status")
        return send.call_args.args[1]

    def phone(self, payload, webhook="a" * 64, **overrides):
        return relay.handler({
            "routeKey": "POST /api/webhook/{webhook_id}",
            "pathParameters": {"webhook_id": webhook}, "body": json.dumps(payload),
            **overrides,
        }, None)

    def respond(self, request_id, result):
        raw = json.dumps(result)
        frames = [
            {"action": "response", "id": request_id, "index": index,
             "total": (len(raw) + relay.RESPONSE_CHUNK - 1) // relay.RESPONSE_CHUNK,
             "data": raw[offset:offset + relay.RESPONSE_CHUNK]}
            for index, offset in enumerate(range(0, len(raw), relay.RESPONSE_CHUNK))
        ]
        for frame in reversed(frames):
            self.assertLessEqual(len(json.dumps(frame).encode()), relay.MAX_MESSAGE)
            self.assertEqual(self.socket(frame)["statusCode"], 200)

    def test_roundtrip_preserves_encryption_and_does_not_store_location(self):
        self.assertEqual(self.phone({"type": "update_location"})["statusCode"], 503)
        status = self.enroll()
        self.assertEqual(status["phone_relay_url"], os.environ["HTTP_URL"])
        payload = {"type": "update_location", "encrypted": True, "encrypted_data": "opaque-ciphertext"}

        def respond(connection, message):
            self.assertEqual(message["phone"], relay.digest("a" * 64))
            self.assertNotIn("a" * 64, json.dumps(message))
            self.assertEqual(message["event"], payload)
            self.assertNotIn("path", message)
            self.assertGreater(message["expires"], time.time())
            self.respond(message["id"], {"status": 200, "body": {}})

        with patch.object(relay, "send", side_effect=respond):
            self.assertEqual(self.phone(payload)["statusCode"], 200)
        stored = str(relay.table().scan()["Items"])
        self.assertNotIn("a" * 64, stored)
        self.assertNotIn("opaque-ciphertext", stored)
        self.assertEqual(self.phone(payload, webhook="b" * 64)["statusCode"], 403)
        with patch.object(relay, "send"):
            self.socket({"action": "poll", "phones": []})
        self.assertEqual(self.phone(payload)["statusCode"], 403)

    def test_only_phone_http_route_is_available(self):
        for route in ("GET /authorize", "GET /status", "POST /token", "GET /", "POST /api/services"):
            with self.subTest(route=route):
                result = relay.handler({"routeKey": route}, None)
                self.assertEqual(result["statusCode"], 404)
        self.assertEqual(relay.handler({"operation": "invoke", "path": "/api/alexa"}, None)["statusCode"], 404)

    def test_scope_body_limits_and_invalid_encoding(self):
        self.enroll()
        for payload in (
            {"type": "call_service"}, {"type": "fire_event"},
            {"type": "render_template"}, {"type": []}, [], {},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.phone(payload)["statusCode"], 400)
        self.assertEqual(self.phone({"type": "update_location", "data": "x" * 23_000})["statusCode"], 413)
        self.assertEqual(self.phone({}, webhook="../auth/token")["statusCode"], 403)
        for raw in ("not-base64", base64.b64encode(b"\xff").decode()):
            self.assertEqual(self.phone({}, body=raw, isBase64Encoded=True)["statusCode"], 400)
        self.assertEqual(self.phone({}, body="not-json")["statusCode"], 400)

    def test_base64_webhook_and_fragmented_response(self):
        self.enroll()
        result = {"status": 200, "body": {"zones": ["x" * 30_000]}}

        def respond(connection, message):
            self.assertEqual(message["event"], {"type": "get_zones"})
            self.respond(message["id"], result)

        with patch.object(relay, "send", side_effect=respond):
            response = self.phone(
                {}, body=base64.b64encode(b'{"type":"get_zones"}').decode(),
                isBase64Encoded=True,
            )
        self.assertEqual(json.loads(response["body"]), result["body"])

    def test_enrollment_expires_and_is_bound_to_connection(self):
        self.enroll()
        relay.table().update_item(
            Key={"pk": "phones"}, UpdateExpression="SET expires = :expires",
            ExpressionAttributeValues={":expires": int(time.time()) - 1},
        )
        self.assertEqual(self.phone({"type": "update_location"})["statusCode"], 503)
        self.enroll()
        self.connect("replacement")
        self.assertEqual(self.phone({"type": "update_location"})["statusCode"], 503)
        self.assertEqual(self.socket(route="$disconnect")["statusCode"], 403)
        self.assertEqual(relay.get("connection")["connection"], "replacement")
        self.socket(connection="replacement", route="$disconnect")
        self.assertIsNone(relay.get("connection"))

    def test_authentication_and_removed_account_actions(self):
        self.assertEqual(self.socket(route="$connect")["statusCode"], 403)
        self.assertEqual(self.socket({"action": "poll"})["statusCode"], 403)
        self.connect()
        for action in ("approve", "revoke", "invoke"):
            self.assertEqual(self.socket({"action": action})["statusCode"], 400)
        for phones in ("bad", ["not-a-hash"], ["a" * 64] * 21):
            self.assertEqual(self.socket({"action": "poll", "phones": phones})["statusCode"], 400)

    def test_fragment_validation_and_connection_ownership(self):
        self.connect()
        key = "c" * 32
        relay.put("request#" + key, 60, connection="different-home", parts={})
        valid = {"action": "response", "id": key, "index": 0, "total": 1, "data": '{"status":200}'}
        self.assertEqual(self.socket(valid)["statusCode"], 400)
        relay.put("request#" + key, 60, connection="home", parts={})
        for override in (
            {"index": -1}, {"total": True}, {"total": 30}, {"data": ""},
            {"total": 2}, {"data": "\u2600"}, {"id": []},
        ):
            self.assertEqual(self.socket({**valid, **override})["statusCode"], 400)
        self.assertEqual(relay.get("request#" + key)["parts"], {})
        self.assertEqual(self.socket(valid)["statusCode"], 200)
        self.assertEqual(self.socket(valid)["statusCode"], 503)

    def test_timeout_and_gone_connection_fail_explicitly(self):
        from botocore.exceptions import ClientError

        self.enroll()
        with patch.object(relay, "send"), patch.object(
            relay.time, "monotonic", side_effect=[0, 5],
        ):
            self.assertEqual(self.phone({"type": "get_config"})["statusCode"], 504)
        gone = ClientError({"Error": {"Code": "GoneException"}}, "PostToConnection")
        with patch.object(relay, "send", side_effect=gone):
            self.assertEqual(self.phone({"type": "get_config"})["statusCode"], 503)
