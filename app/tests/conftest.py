"""Shared test environment.

`Settings` reads most of its values once at import time, so the environment has
to be set before any sms_relay module loads. conftest is imported before test
modules, which makes this the only place it can safely happen — and doing it
here rather than per-module keeps two test files from fighting over the same
process-global env and the same SQLite file.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

TMPDIR = tempfile.mkdtemp(prefix="sms-relay-test-")
OUTPUT_DIR = os.path.join(TMPDIR, "out")

# Every service used by any test module, so importing one module can't strip a
# key another one needs.
API_KEYS = {"talaria": "key-talaria", "money": "key-money"}
WEBHOOK_SECRET = "test-webhook-secret"

os.environ.update(
    SMS_RELAY_DB_PATH=os.path.join(TMPDIR, "test.db"),
    SMS_RELAY_PROVIDER="dev",
    SMS_RELAY_DEV_OUTPUT_DIR=OUTPUT_DIR,
    SMS_RELAY_API_KEYS=json.dumps(API_KEYS),
    SMS_RELAY_WEBHOOK_SECRET=WEBHOOK_SECRET,
    SMS_RELAY_RATE_LIMIT_PER_MINUTE="600",
    SMS_RELAY_MAX_ATTEMPTS="3",
    SMS_RELAY_DEFAULT_REGION="US",
    SMS_RELAY_SUBSCRIBERS="[]",
)


@pytest.fixture(scope="session")
def client():
    """One app instance for the whole run.

    The lifespan starts the worker loops, so a second concurrent instance would
    mean two send loops racing over one SQLite file.
    """
    from fastapi.testclient import TestClient

    from sms_relay.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def talaria_headers():
    return {"X-API-Key": API_KEYS["talaria"]}


@pytest.fixture
def money_headers():
    return {"X-API-Key": API_KEYS["money"]}
