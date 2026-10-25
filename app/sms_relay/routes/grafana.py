"""Public POST-only Grafana webhook; its key cannot select recipients/topics."""
import hashlib
import json
import time
from datetime import datetime
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
        entries = []
        # Validate the entire webhook before queueing its single notification.
        for alert in alerts:
            if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict) or not isinstance(alert.get("annotations", {}), dict):
                raise ValueError()
            state = alert["status"]
            if state not in ("firing", "resolved"):
                raise ValueError()
            labels = alert["labels"]
            name = labels["alertname"]
            summary = alert.get("annotations", {}).get("summary", name)
            fingerprint, starts = alert["fingerprint"], alert["startsAt"]
            if not all(isinstance(x, str) and x for x in [name, summary, fingerprint, starts]):
                raise ValueError()
            started = datetime.fromisoformat(starts.replace("Z", "+00:00"))
            if started.tzinfo is None:
                raise ValueError()
            # Anchor reminders to this incident, not UTC wall-clock buckets.
            reminder = max(0, int((time.time() - started.timestamp()) // 14400)) if state == "firing" else 0
            affected = ", ".join(f"{label}={labels[label]}" for label in
                ["app", "checks", "node", "namespace", "persistentvolumeclaim", "job_name", "cronjob", "service"]
                if labels.get(label))
            if not affected:
                affected = ", ".join(f"{label}={labels[label]}" for label in ["instance", "pod"] if labels.get(label))
            # Recovery summaries must not repeat the stale firing instructions.
            description = summary if state == "firing" else "Alert condition cleared."
            text = f"[{state.upper()}] {name}: {description}"
            if affected:
                text += "\n" + affected
            critical = labels.get("severity") == "critical"
            priority = (4 if state == "firing" else 3) if critical else (3 if state == "firing" else 1)
            entries.append(([fingerprint, starts, state, reminder], text[:1800], priority))
        entries.sort(key=lambda x: (-x[2], x[0]))
        # Order-independent, persistent dedup of the whole group. A newly firing
        # or resolved member changes the key; identical webhook retries do not.
        key = hashlib.sha256(json.dumps([x[0] for x in entries]).encode()).hexdigest()
        lines = []
        for _, text, _ in entries:
            if sum(len(s) + 2 for s in lines) + len(text) > 3800:
                break
            lines.append(text)
        text = "\n\n".join(lines)
        if len(lines) < len(entries):
            text += f"\n\n{len(entries)-len(lines)} additional alerts; see Grafana."
        message = SendRequest(to=route["to"], body=text, notification_priority=max(x[2] for x in entries),
                              idempotency_key="grafana-group-"+key)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "invalid Grafana webhook") from None
    await send_message(message, service, session)
    return {"accepted": len(entries), "notifications": 1}
