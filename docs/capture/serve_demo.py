#!/usr/bin/env python3
"""Local demo instance for README captures: the real app, invented messages.

Runs against the built-in `dev` provider, which writes outbound messages to a
directory instead of a handset, plus a throwaway SQLite file seeded with a
plausible mix of traffic. No real phone number, message body or gateway
credential is involved, and nothing leaves the machine.

    python3 docs/capture/serve_demo.py     # serves on :8323
    python3 docs/capture/capture.py        # then shoot it

The seed is deterministic, so re-captures diff cleanly.
"""
import asyncio
import datetime as dt
import json
import os
import random
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(ROOT, "app")

TMP = os.path.join(tempfile.gettempdir(), "sms-relay-demo")
os.makedirs(TMP, exist_ok=True)
DB = os.path.join(TMP, "demo.db")
if os.path.exists(DB):
    os.remove(DB)

DEMO_KEY = "demo-key"
os.environ.update(
    SMS_RELAY_DB_PATH=DB,
    SMS_RELAY_PROVIDER="dev",                      # never touches a handset
    SMS_RELAY_DEV_OUTPUT_DIR=os.path.join(TMP, "out"),
    # One key per service, 1:1 -- the demo key is talaria's, and the log view
    # deliberately shows a caller its own outbound plus every inbound.
    SMS_RELAY_API_KEYS=json.dumps({"talaria": DEMO_KEY, "money": "key-money",
                                   "gamedex": "key-gamedex"}),
    SMS_RELAY_WEBHOOK_SECRET="demo-webhook-secret",
    SMS_RELAY_RATE_LIMIT_PER_MINUTE="600",
    SMS_RELAY_DEFAULT_REGION="US",
    SMS_RELAY_SUBSCRIBERS="[]",
)

sys.path.insert(0, APP)
from sms_relay import db, models  # noqa: E402

PORT = int(os.environ.get("PORT", "8323"))

# (service, direction, to/from, body, status, minutes ago)
SEED = [
    ("talaria", "outbound", "+15550100", "Watch hit: 1979 Krugerrand at $2,520 - 51 bids, ends 18:04", "delivered", 4),
    ("talaria", "outbound", "+15550100", "Ending soon: 6 watched auctions close in the next 2 hours.", "delivered", 22),
    ("talaria", "inbound",  "+15550142", "STOP", "received", 38),
    ("money",   "outbound", "+15550100", "Net worth snapshot written. Investable $1.1M.", "delivered", 47),
    ("talaria", "outbound", "+15550100", "Outbid on 'Cast Iron Barber Pole' at $1,700.", "delivered", 63),
    ("talaria", "outbound", "+15550199", "Watch hit: Halo 5 Xbox One at $6.00, 90% under typical", "failed", 88),
    ("gamedex", "inbound",  "+15550142", "status", "received", 104),
    ("talaria", "outbound", "+15550100", "Price drop: 'Seiken Densetsu 3' now $41.00 (-18%).", "delivered", 131),
    ("talaria", "outbound", "+15550100", "Rare find: sealed Trials of Mana, 2 bids, 4h left.", "sent", 160),
    ("talaria", "inbound",  "+15550188", "SNOOZE 2h", "received", 186),
    ("talaria", "outbound", "+15550100", "Snoozed. Next digest at 21:00.", "delivered", 187),
    ("gamedex", "outbound", "+15550100", "Workbook re-parsed: 47 rows changed.", "delivered", 214),
    ("talaria", "outbound", "+15550100", "Daily digest: 34 new listings across 6 watches.", "delivered", 262),
    ("talaria", "outbound", "+15550100", "Gateway unreachable - queued, retrying.", "queued", 305),
]


async def seed():
    await db.init_db()
    rng = random.Random(20260826)
    now = dt.datetime.now(dt.timezone.utc)
    sm = db.get_sessionmaker()
    async with sm() as s:
        for i, (svc, direction, addr, body, status, mins) in enumerate(SEED):
            created = now - dt.timedelta(minutes=mins)
            row = models.Message(
                id=str(uuid.uuid4()),
                service=svc,
                direction=direction,
                to_number=addr if direction == "outbound" else None,
                from_number=addr if direction == "inbound" else None,
                body=body,
                status=status,
                attempts=3 if status == "failed" else 1,
                idempotency_key=f"demo-{i}",
                created_at=created,
                updated_at=created + dt.timedelta(seconds=rng.randint(1, 40)),
            )
            s.add(row)
        await s.commit()
    print(f"seeded {len(SEED)} messages - all invented, dev provider, no handset")
    print("the log view shows talaria's own outbound plus every inbound, by design")


def main():
    asyncio.run(seed())
    import uvicorn
    from sms_relay.main import app
    print(f"demo instance on http://127.0.0.1:{PORT}  (API key: {DEMO_KEY})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
