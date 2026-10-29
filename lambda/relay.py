"""Single-home relay for explicitly enrolled Companion app webhooks."""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal
from functools import cache

import boto3
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
MAX_MESSAGE = 28_000
MAX_RESPONSE = 256_000
RESPONSE_CHUNK = 12_000
PHONE_TYPES = {
    "update_location", "get_config", "get_zones", "update_registration",
    "register_sensor", "update_sensor_states",
}
PARAMETERS = {}


@cache
def table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE"])


@cache
def aws_client(service):
    options = {"endpoint_url": os.environ["SOCKET_ENDPOINT"]} if service == "apigatewaymanagementapi" else {}
    return boto3.client(service, **options)


def connector_key():
    cached = PARAMETERS.get("connector")
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    value = aws_client("ssm").get_parameter(
        Name=os.environ["CONNECTOR_PARAMETER"], WithDecryption=True,
    )["Parameter"]["Value"]
    PARAMETERS["connector"] = (time.monotonic(), value)
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def get(key):
    item = table().get_item(Key={"pk": key}, ConsistentRead=True).get("Item")
    return plain(item) if item and item["expires"] > int(time.time()) else None


def put(key, ttl, **fields):
    table().put_item(Item={"pk": key, "expires": int(time.time()) + ttl, **fields})


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
        "body": json.dumps(body),
    }


def error(message, status=400):
    return reply(status, {"error": message})


def send(connection, body):
    encoded = json.dumps(body).encode()
    if len(encoded) > MAX_MESSAGE:
        raise ValueError("Relay message exceeds size limit")
    aws_client("apigatewaymanagementapi").post_to_connection(
        ConnectionId=connection, Data=encoded,
    )


def http(event):
    if event.get("routeKey") != "POST /api/webhook/{webhook_id}":
        return error("not_found", 404)
    webhook = (event.get("pathParameters") or {}).get("webhook_id", "")
    if not isinstance(webhook, str) or not re.fullmatch(r"[a-f0-9]{64}", webhook):
        return error("phone_not_enrolled", 403)
    connection, phones = get("connection"), get("phones")
    if not connection or not phones or phones["connection"] != connection["connection"]:
        return error("phone_unavailable", 503)
    if digest(webhook) not in phones["phones"]:
        return error("phone_not_enrolled", 403)
    raw = event.get("body") or ""
    if not isinstance(raw, str):
        return error("invalid_request")
    if len(raw.encode()) > 22_000:
        return error("payload_too_large", 413)
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw, validate=True).decode()
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or not isinstance(payload.get("type"), str)
            or payload["type"] not in PHONE_TYPES
            or len(json.dumps(payload).encode()) > 20_000):
        return error("unsupported_phone_request")
    result = forward(connection, secrets.token_hex(16), {
        "phone": digest(webhook), "event": payload, "expires": int(time.time()) + 10,
    })
    return reply(result["status"], result.get("body", {}))


def socket(event):
    context = event["requestContext"]
    connection, route = context["connectionId"], context["routeKey"]
    if route == "$connect":
        headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
        if not hmac.compare_digest(
            headers.get("authorization", ""), "Bearer " + connector_key(),
        ):
            return {"statusCode": 403}
        put("connection", 7200, connection=connection)
        return {"statusCode": 200}
    current = get("connection")
    if not current or current["connection"] != connection:
        return {"statusCode": 403}
    if route == "$disconnect":
        table().delete_item(
            Key={"pk": "connection"},
            ConditionExpression="#connection = :connection",
            ExpressionAttributeNames={"#connection": "connection"},
            ExpressionAttributeValues={":connection": connection},
        )
        return {"statusCode": 200}
    if route != "$default":
        return {"statusCode": 400}
    if len((event.get("body") or "").encode()) > MAX_MESSAGE:
        return {"statusCode": 413}
    body = json.loads(event.get("body") or "{}")
    if not isinstance(body, dict):
        return {"statusCode": 400}
    if body.get("action") == "poll":
        phones = body.get("phones", [])
        if (not isinstance(phones, list) or len(phones) > 20
                or not all(isinstance(phone, str) and re.fullmatch(r"[a-f0-9]{64}", phone)
                           for phone in phones)):
            return {"statusCode": 400}
        put("phones", 120, connection=connection, phones=phones)
        send(connection, {
            "type": "status", "phones": phones, "phone_relay_url": os.environ["HTTP_URL"],
        })
    elif body.get("action") == "response":
        request_id = body.get("id", "")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            return {"statusCode": 400}
        request = get("request#" + request_id)
        if not request or request["connection"] != connection:
            return {"statusCode": 400}
        index, total, data = body.get("index"), body.get("total"), body.get("data")
        if (type(index) is not int or type(total) is not int
                or not 0 <= index < total <= (MAX_RESPONSE + RESPONSE_CHUNK - 1) // RESPONSE_CHUNK
                or not isinstance(data, str) or not data.isascii()
                or not 0 < len(data) <= RESPONSE_CHUNK
                or (index < total - 1 and len(data) != RESPONSE_CHUNK)
                or (index == total - 1 and index * RESPONSE_CHUNK + len(data) > MAX_RESPONSE)):
            return {"statusCode": 400}
        table().update_item(
            Key={"pk": "request#" + request_id},
            UpdateExpression="SET #parts.#part = :data, #total = :total",
            ConditionExpression=(
                "attribute_not_exists(#parts.#part) AND expires > :now "
                "AND (attribute_not_exists(#total) OR #total = :total)"
            ),
            ExpressionAttributeNames={"#part": str(index), "#parts": "parts", "#total": "total"},
            ExpressionAttributeValues={
                ":data": data, ":total": total, ":now": int(time.time()),
            },
        )
    else:
        return {"statusCode": 400}
    return {"statusCode": 200}


def forward(connection, request_id, message):
    key = "request#" + request_id
    put(key, 60, connection=connection["connection"], parts={})
    send(connection["connection"], {"type": "request", "id": request_id, **message})
    deadline = time.monotonic() + 4.5
    while time.monotonic() < deadline:
        response = get(key)
        if response and response.get("total") and len(response["parts"]) == response["total"]:
            result = json.loads("".join(
                response["parts"][str(index)] for index in range(response["total"])
            ))
            if (not isinstance(result, dict) or type(result.get("status")) is not int
                    or not 200 <= result["status"] <= 599):
                raise ValueError("Invalid connector response")
            return result
        time.sleep(0.15)
    LOGGER.error("Phone relay response deadline exceeded")
    return {"status": 504}


def handler(event, context):
    try:
        if "connectionId" in event.get("requestContext", {}):
            return socket(event)
        return http(event)
    except (ValueError, KeyError, TypeError, UnicodeError, binascii.Error):
        LOGGER.warning("Invalid phone relay request")
        return error("invalid_request")
    except ClientError as exception:
        code = exception.response["Error"]["Code"]
        LOGGER.error("AWS relay operation failed (%s)", code)
        return error("relay_unavailable", 503)
