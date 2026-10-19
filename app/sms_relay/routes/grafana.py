"""Public POST-only Grafana webhook; its key cannot select recipients/topics."""
import hashlib
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from sms_relay.auth import require_api_key
from sms_relay.config import settings
from sms_relay.db import get_session
from sms_relay.routes.messages import send_message
from sms_relay.schemas import SendRequest

router = APIRouter()


@router.post("/api/v1/grafana", status_code=202)
async def grafana(request: Request, service: Annotated[str, Depends(require_api_key)],
                  session: Annotated[AsyncSession, Depends(get_session)]):
    if service != "grafana":
        raise HTTPException(403, "Grafana credential required")
    route = settings.notification_routes.get(service)
    if not route:
        raise HTTPException(503, "Grafana notifications not configured")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "payload too large")
    try:
        data = json.loads(body)
        alerts = data["alerts"]
        if not isinstance(alerts, list) or not 1 <= len(alerts) <= 100:
            raise ValueError()
        messages = []
        # Validate the whole group before accepting any alert.
        for alert in alerts:
            if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict) or not isinstance(alert.get("annotations", {}), dict):
                raise ValueError()
            state = alert["status"]
            if state not in ("firing", "resolved"):
                raise ValueError()
            name = alert["labels"]["alertname"]
            summary = alert.get("annotations", {}).get("summary", name)
            fingerprint = alert["fingerprint"]
            starts = alert["startsAt"]
            if not all(isinstance(x, str) and x for x in [name, summary, fingerprint, starts]):
                raise ValueError()
            # Persistent per-instance dedup, including recovery and 4h reminders.
            key = hashlib.sha256(json.dumps([fingerprint, starts, state, int(time.time() // 14400) if state == "firing" else 0]).encode()).hexdigest()
            affected = ", ".join(f"{label}={alert['labels'][label]}" for label in
                ["instance", "node", "namespace", "pod", "persistentvolumeclaim", "job_name", "cronjob", "service"]
                if label in alert["labels"])
            text = f"[{state.upper()}] {name}: {summary}"
            if affected:
                text += "\n" + affected
            messages.append(SendRequest(to=route["to"], body=text[:4000], idempotency_key="grafana-"+key))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "invalid Grafana webhook") from None
    for message in messages:
        await send_message(message, service, session)
    return {"accepted": len(messages)}
