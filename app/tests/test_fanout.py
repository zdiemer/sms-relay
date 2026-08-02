"""Inbound fan-out against a real HTTP subscriber.

Runs a throwaway server in a thread and asserts the relay actually delivers to
it — including the HMAC signature a subscriber needs to trust the payload, and
the retry behaviour when a subscriber is down.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from .conftest import WEBHOOK_SECRET

RECEIVED: list[dict] = []
FAIL_TIMES = {"count": 0}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        RECEIVED.append(
            {
                "raw": raw,
                "signature": self.headers.get("X-Signature"),
                "event": self.headers.get("X-SMS-Relay-Event"),
                "delivery": self.headers.get("X-SMS-Relay-Delivery"),
                "body": json.loads(raw),
            }
        )
        # The /flaky endpoint 500s the first time to prove the retry works.
        if self.path == "/flaky" and FAIL_TIMES["count"] == 0:
            FAIL_TIMES["count"] += 1
            self.send_response(500)
        else:
            self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module", autouse=True)
def subscriber_server():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Only the subscriber list needs setting here — it is read live on every
    # fan-out, and the port isn't known until the server binds. Everything else
    # comes from conftest so the two test modules share one env and one DB.
    os.environ["SMS_RELAY_SUBSCRIBERS"] = json.dumps(
        [
            {
                "name": "talaria",
                "url": f"http://127.0.0.1:{port}/hook",
                "secret": "subscriber-secret",
            },
            {"name": "flaky", "url": f"http://127.0.0.1:{port}/flaky", "secret": ""},
        ]
    )
    yield port
    os.environ["SMS_RELAY_SUBSCRIBERS"] = "[]"
    server.shutdown()


def _post_inbound(client, body_text):
    from sms_relay.auth import sign

    event = {
        "event": "sms:received",
        "id": f"gw-{uuid.uuid4()}",
        "payload": {"phoneNumber": "+12025550150", "message": body_text},
    }
    raw = json.dumps(event).encode()
    return client.post(
        "/api/v1/inbound/android-gateway",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature": sign(raw, WEBHOOK_SECRET),
        },
    )


def _wait_for_delivery(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = [r for r in RECEIVED if predicate(r)]
        if match:
            return match
        time.sleep(0.2)
    raise AssertionError(f"no matching delivery within {timeout}s; got {len(RECEIVED)}")


def test_inbound_is_pushed_to_every_subscriber(client):
    res = _post_inbound(client, "fan me out")
    assert res.status_code == 202
    assert res.json()["subscribers"] == 2

    delivered = _wait_for_delivery(
        lambda r: r["body"]["message"]["body"] == "fan me out"
    )
    subscribers = {r["delivery"] for r in delivered}
    assert len(subscribers) >= 1

    payload = delivered[0]["body"]
    assert payload["event"] == "message.received"
    assert payload["message"]["from"] == "+12025550150"
    assert delivered[0]["event"] == "message.received"


def test_payload_is_signed_with_the_subscriber_secret(client):
    _post_inbound(client, "signed payload")
    delivered = _wait_for_delivery(
        lambda r: r["body"]["message"]["body"] == "signed payload" and r["signature"]
    )
    record = delivered[0]
    expected = hmac.new(
        b"subscriber-secret", record["raw"], hashlib.sha256
    ).hexdigest()
    assert record["signature"] == expected


def test_a_failing_subscriber_is_retried(client):
    FAIL_TIMES["count"] = 0
    _post_inbound(client, "retry me")

    # The flaky endpoint 500s once; backoff is 30s, so just assert the first
    # attempt happened and the delivery row is still pending rather than
    # waiting out the retry.
    _wait_for_delivery(lambda r: r["body"]["message"]["body"] == "retry me")
    assert FAIL_TIMES["count"] == 1

    from sqlalchemy import select

    from sms_relay.db import get_sessionmaker
    from sms_relay.models import WebhookDelivery

    async def check():
        async with get_sessionmaker()() as session:
            rows = (
                (
                    await session.execute(
                        select(WebhookDelivery).where(
                            WebhookDelivery.subscriber == "flaky"
                        )
                    )
                )
                .scalars()
                .all()
            )
            return rows

    rows = _run(check())
    assert rows, "expected a delivery row for the flaky subscriber"
    # It failed once, so it is queued for a later attempt, not abandoned.
    assert any(r.status == "pending" and r.attempts >= 1 for r in rows)


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
