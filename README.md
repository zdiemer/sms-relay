# sms-relay

A self-hosted SMS gateway for the cluster — the thing every other service calls
instead of holding phone credentials of its own. Sends and receives real text
messages through an Android handset running
[SMS Gateway for Android](https://github.com/capcom6/android-sms-gateway),
with the durability layer a bare handset doesn't have.

Runs at `sms-relay.zachd.duckdns.org` in the `infra` namespace.

## Why this exists

Talaria talked to the phone directly: one `httpx.post` with a 5-second timeout,
and on failure a log line. A phone that was asleep, off wifi, or had moved IP
silently dropped the message — alerts and signup confirmation codes included.
There was no record that a message had ever been sent.

This service is that call plus everything that was missing:

| | Before (in Talaria) | Now |
|---|---|---|
| Failure handling | logged, dropped | retried with exponential backoff, capped at 5 attempts |
| Record of sends | none | every message persisted with status and error |
| Delivery status | unknown | `sent` → `delivered` via gateway receipts |
| Duplicate sends | possible on retry | idempotency keys |
| Burst behaviour | whole batch at once | rate-limited (default 30/min) |
| Inbound SMS | not received at all | stored and fanned out to subscribers |
| Phone format | three copies of `^\d{10}$` | E.164 via libphonenumber |
| Credentials | in every consumer | only here |

## API

All endpoints except `/api/health` and the inbound webhook need an API key,
either `Authorization: Bearer <key>` or `X-API-Key: <key>`. Keys map to a
service name, and that name is recorded on every message.

### Send

```bash
curl -X POST https://sms-relay.zachd.duckdns.org/api/v1/messages \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"to": "2025550101", "body": "hello", "idempotency_key": "optional"}'
```

Returns `202` with the queued message(s). `to` accepts a single number or a
list, in any format libphonenumber understands. A repeat POST with the same
`idempotency_key` returns the original message rather than sending twice.

The response is *acceptance*, not delivery — poll `GET /api/v1/messages/{id}`
if you need the outcome. Callers should never block on the handset.

### Read

- `GET /api/v1/messages/{id}` — one message
- `GET /api/v1/messages?direction=&status=&limit=&offset=` — the log

Outbound messages are visible only to the service that sent them; inbound is
visible to every authenticated caller, since it has no single owner.

### Receive

The handset POSTs to `/api/v1/inbound/android-gateway`, signing the raw body
with `SMS_RELAY_WEBHOOK_SECRET` (HMAC-SHA256, `X-Signature`). Each inbound
message is stored, then pushed to every configured subscriber:

```json
{
  "event": "message.received",
  "message": {"id": "...", "from": "+1...", "to": null,
              "body": "...", "received_at": "..."}
}
```

Subscribers are configured in `values.local.yaml`. Each gets its own retry
schedule (6 attempts, backoff to an hour) so one dead consumer doesn't hold up
the others. If a subscriber has a `secret`, its payload is signed the same way
— verify it before trusting the body.

### Status

`GET /api/health` — DB reachability, provider, queue depth. Deliberately open
(the kubelet has no API key) and deliberately *not* a probe of the handset: the
phone being briefly unreachable is what the queue is for, and failing readiness
would restart the pod that owns the queue.

A read-only log viewer is served at `/`, behind Authelia.

## Deploying

Same shape as everything else in the selfhosted repo:

```bash
cp values.local.yaml.example values.local.yaml   # fill in, gitignored
kubectl create namespace infra                   # if it doesn't exist
./build.sh                                       # → ghcr.io/zdiemer/sms-relay:vN
./upgrade.sh
```

Bump `image.tag` in `values.yaml` and `appVersion` in `Chart.yaml` together
with the code, in one commit. `upgrade.sh` refuses to roll onto a tag that
isn't in the registry — `strategy: Recreate` means a typo would be an outage
rather than a failed rollout.

Bring the pod up with `ingress.enabled: false` first, confirm it's healthy,
then flip the ingress on.

### The handset

1. Install SMS Gateway for Android, enable local server mode, note the IP,
   username and password → `provider.gatewayUrl` / `secrets.gatewayUser` /
   `secrets.gatewayPassword`.
2. Give the phone a DHCP reservation. `gatewayUrl` is an IP; if it moves, sends
   queue and retry rather than fail, but nothing will get through until it's
   corrected.
3. For inbound, register a webhook in the gateway app pointing at
   `https://sms-relay.zachd.duckdns.org/api/v1/inbound/android-gateway` with
   the same secret as `secrets.webhookSecret`.

### Why there are two Ingress objects

The phone can't complete an Authelia login, so the inbound path can't sit
behind forward-auth. Traefik attaches middleware per *router* and has no
per-path exclusion — but separate Ingress objects are separate routers. So
`ingress.yaml` carries the forward-auth middleware and `ingress-webhook.yaml`
serves `/api/v1/inbound` without it. That path is not unauthenticated, it's
*differently* authenticated: the HMAC signature is checked before the handler
runs, and the service refuses inbound entirely if no secret is configured.

This is the only place in the cluster that does this. Don't "simplify" it back
into one Ingress.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e './app[dev]'
.venv/bin/python -m pytest app/tests -q
```

Set `SMS_RELAY_PROVIDER=dev` to write messages to files under
`SMS_RELAY_DEV_OUTPUT_DIR` instead of touching a handset — the full
queue/retry/fan-out path still runs. That's how the tests work, including
`test_fanout.py`, which stands up a real HTTP subscriber and asserts the
signature and retry behaviour.

## Notes

- SQLite on a ReadWriteOnce PVC. `replicas: 1` and `strategy: Recreate` are
  load-bearing — never run two writers.
- Numbers are redacted to the last four digits in logs. This service holds
  every message for every consumer, so its logs are a bigger target than any
  one caller's.
- Terminal messages are pruned after `SMS_RELAY_RETENTION_DAYS` (default 90) so
  the database doesn't grow forever on a 1Gi volume.
- There's no STOP/HELP keyword handling or consent tracking. Everything here
  goes to numbers that opted in elsewhere; if this ever sends to strangers,
  that needs to exist first.
