"""End-to-end tests over the real DB and worker loops.

Everything runs against a temp SQLite file and the dev provider, so the whole
queue → send → status → fan-out path is exercised without a handset.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid

import pytest

TMPDIR = tempfile.mkdtemp(prefix="sms-relay-test-")

# Config is read at import time, so the environment has to be set before any
# sms_relay module is imported.
os.environ.update(
    SMS_RELAY_DB_PATH=os.path.join(TMPDIR, "test.db"),
    SMS_RELAY_PROVIDER="dev",
    SMS_RELAY_DEV_OUTPUT_DIR=os.path.join(TMPDIR, "out"),
    SMS_RELAY_API_KEYS=json.dumps({"talaria": "key-talaria", "money": "key-money"}),
    SMS_RELAY_WEBHOOK_SECRET="test-webhook-secret",
    SMS_RELAY_RATE_LIMIT_PER_MINUTE="600",
    SMS_RELAY_MAX_ATTEMPTS="3",
    SMS_RELAY_DEFAULT_REGION="US",
)

from fastapi.testclient import TestClient  # noqa: E402

from sms_relay.auth import sign  # noqa: E402
from sms_relay.main import app  # noqa: E402
from sms_relay.models import MessageStatus  # noqa: E402
from sms_relay.phone import InvalidPhoneNumber, normalize, redact  # noqa: E402
from sms_relay.providers.base import ProviderError  # noqa: E402
from sms_relay.worker import RateLimiter, backoff_seconds  # noqa: E402

TALARIA = {"X-API-Key": "key-talaria"}
MONEY = {"X-API-Key": "key-money"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait_for(client, message_id, headers, statuses, timeout=15.0):
    """Poll until the worker moves the message into one of `statuses`."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/messages/{message_id}", headers=headers).json()
        if body["status"] in statuses:
            return body
        time.sleep(0.2)
    raise AssertionError(f"{message_id} stuck at {body['status']}, wanted {statuses}")


# --- phone normalization ----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["2025550101", "(202) 555-0101", "202-555-0101", "+12025550101", "1 202 555 0101"],
)
def test_normalize_accepts_the_formats_talaria_used(raw):
    assert normalize(raw) == "+12025550101"


@pytest.mark.parametrize("raw", ["", "   ", "123", "not-a-number", "00000000000000"])
def test_normalize_rejects_junk(raw):
    with pytest.raises(InvalidPhoneNumber):
        normalize(raw)


def test_redact_keeps_only_the_last_four():
    assert redact("+12025550101") == "*******0101"
    assert redact(None) == "<none>"


# --- auth -------------------------------------------------------------------


def test_send_requires_a_key(client):
    assert client.post("/api/v1/messages", json={"to": "2025550101", "body": "hi"}).status_code == 401


def test_send_rejects_an_unknown_key(client):
    res = client.post(
        "/api/v1/messages",
        json={"to": "2025550101", "body": "hi"},
        headers={"X-API-Key": "nope"},
    )
    assert res.status_code == 401


def test_bearer_scheme_works_too(client):
    res = client.post(
        "/api/v1/messages",
        json={"to": "2025550101", "body": "bearer test"},
        headers={"Authorization": "Bearer key-talaria"},
    )
    assert res.status_code == 202


# --- send path --------------------------------------------------------------


def test_send_queues_and_worker_delivers(client):
    res = client.post(
        "/api/v1/messages",
        json={"to": "(202) 555-0102", "body": "hello from the relay"},
        headers=TALARIA,
    )
    assert res.status_code == 202
    message = res.json()["messages"][0]
    assert message["status"] == MessageStatus.QUEUED
    assert message["to"] == "+12025550102"  # normalized on the way in
    assert message["service"] == "talaria"

    sent = wait_for(client, message["id"], TALARIA, {MessageStatus.SENT, MessageStatus.DELIVERED})
    assert sent["provider_message_id"]
    assert sent["error"] is None

    written = os.listdir(os.path.join(TMPDIR, "out"))
    assert any("5550102" in f for f in written)


def test_send_fans_out_to_multiple_recipients(client):
    res = client.post(
        "/api/v1/messages",
        json={"to": ["2025550103", "2025550104"], "body": "broadcast"},
        headers=TALARIA,
    )
    assert res.status_code == 202
    assert {m["to"] for m in res.json()["messages"]} == {"+12025550103", "+12025550104"}


def test_invalid_number_is_rejected_before_queueing(client):
    res = client.post("/api/v1/messages", json={"to": "123", "body": "x"}, headers=TALARIA)
    assert res.status_code == 422


def test_empty_body_is_rejected(client):
    res = client.post("/api/v1/messages", json={"to": "2025550101", "body": ""}, headers=TALARIA)
    assert res.status_code == 422


# --- idempotency ------------------------------------------------------------


def test_idempotency_key_prevents_a_double_send(client):
    key = f"test-{uuid.uuid4()}"
    payload = {"to": "2025550107", "body": "only once", "idempotency_key": key}

    first = client.post("/api/v1/messages", json=payload, headers=TALARIA).json()
    second = client.post("/api/v1/messages", json=payload, headers=TALARIA).json()

    assert first["messages"][0]["id"] == second["messages"][0]["id"]


def test_idempotency_keys_are_scoped_per_service(client):
    key = f"shared-{uuid.uuid4()}"
    payload = {"to": "2025550108", "body": "per-service", "idempotency_key": key}

    a = client.post("/api/v1/messages", json=payload, headers=TALARIA).json()
    b = client.post("/api/v1/messages", json=payload, headers=MONEY).json()

    assert a["messages"][0]["id"] != b["messages"][0]["id"]


def test_idempotency_key_rejected_for_multiple_recipients(client):
    res = client.post(
        "/api/v1/messages",
        json={"to": ["2025550105", "2025550106"], "body": "x", "idempotency_key": "k"},
        headers=TALARIA,
    )
    assert res.status_code == 422


# --- read scoping -----------------------------------------------------------


def test_one_service_cannot_read_anothers_message(client):
    created = client.post(
        "/api/v1/messages",
        json={"to": "2025550109", "body": "talaria private"},
        headers=TALARIA,
    ).json()["messages"][0]

    assert client.get(f"/api/v1/messages/{created['id']}", headers=MONEY).status_code == 404
    assert client.get(f"/api/v1/messages/{created['id']}", headers=TALARIA).status_code == 200


def test_list_is_scoped_to_the_calling_service(client):
    client.post(
        "/api/v1/messages", json={"to": "2025550110", "body": "money only"}, headers=MONEY
    )
    listed = client.get("/api/v1/messages?direction=outbound", headers=MONEY).json()
    assert listed["messages"]
    assert {m["service"] for m in listed["messages"]} == {"money"}


# --- inbound ----------------------------------------------------------------


def _post_inbound(client, event, secret="test-webhook-secret"):
    body = json.dumps(event).encode()
    return client.post(
        "/api/v1/inbound/android-gateway",
        content=body,
        headers={"Content-Type": "application/json", "X-Signature": sign(body, secret)},
    )


def test_inbound_requires_a_valid_signature(client):
    body = json.dumps({"event": "sms:received"}).encode()
    unsigned = client.post("/api/v1/inbound/android-gateway", content=body)
    assert unsigned.status_code == 401

    wrong = _post_inbound(client, {"event": "sms:received"}, secret="wrong-secret")
    assert wrong.status_code == 401


def test_inbound_message_is_stored(client):
    event = {
        "event": "sms:received",
        "id": f"gw-{uuid.uuid4()}",
        "payload": {"phoneNumber": "+12025550111", "message": "inbound hello"},
    }
    res = _post_inbound(client, event)
    assert res.status_code == 202
    assert res.json()["status"] == "accepted"

    stored = client.get(f"/api/v1/messages/{res.json()['id']}", headers=TALARIA).json()
    assert stored["direction"] == "inbound"
    assert stored["status"] == MessageStatus.RECEIVED
    assert stored["from"] == "+12025550111"
    assert stored["body"] == "inbound hello"


def test_inbound_is_deduplicated_by_provider_id(client):
    event = {
        "event": "sms:received",
        "id": f"gw-{uuid.uuid4()}",
        "payload": {"phoneNumber": "+12025550112", "message": "retry me"},
    }
    first = _post_inbound(client, event)
    second = _post_inbound(client, event)

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    assert first.json()["id"] == second.json()["id"]


def test_inbound_accepts_the_flat_payload_shape(client):
    event = {
        "event": "sms:received",
        "id": f"gw-{uuid.uuid4()}",
        "from": "+12025550113",
        "text": "flat shape",
    }
    res = _post_inbound(client, event)
    assert res.status_code == 202
    stored = client.get(f"/api/v1/messages/{res.json()['id']}", headers=TALARIA).json()
    assert stored["body"] == "flat shape"


def test_inbound_keeps_alphanumeric_senders(client):
    event = {
        "event": "sms:received",
        "id": f"gw-{uuid.uuid4()}",
        "payload": {"phoneNumber": "VERIZON", "message": "shortcode sender"},
    }
    res = _post_inbound(client, event)
    assert res.status_code == 202
    stored = client.get(f"/api/v1/messages/{res.json()['id']}", headers=TALARIA).json()
    assert stored["from"] == "VERIZON"


def test_inbound_is_visible_to_every_service(client):
    event = {
        "event": "sms:received",
        "id": f"gw-{uuid.uuid4()}",
        "payload": {"phoneNumber": "+12025550114", "message": "shared inbound"},
    }
    message_id = _post_inbound(client, event).json()["id"]
    assert client.get(f"/api/v1/messages/{message_id}", headers=MONEY).status_code == 200


# --- retry / backoff --------------------------------------------------------


def test_backoff_grows_and_is_capped():
    assert [backoff_seconds(n) for n in range(1, 7)] == [30, 60, 120, 240, 480, 900]
    assert backoff_seconds(99) == 900  # capped, never unbounded


def test_permanent_provider_errors_are_flagged():
    assert ProviderError("bad request", permanent=True).permanent
    assert not ProviderError("timeout").permanent


async def test_rate_limiter_blocks_past_the_window():
    limiter = RateLimiter(per_minute=2)
    await limiter.acquire()
    await limiter.acquire()
    # The third has to wait out the window, so it must not complete promptly.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire(), timeout=0.5)


# --- health -----------------------------------------------------------------


def test_health_is_open_and_reports_the_provider(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["provider"] == "dev"
    assert body["queued"] >= 0
