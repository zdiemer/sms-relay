import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sms_relay import worker
from sms_relay.models import MessageStatus
from sms_relay.notifications import publish, topic_for


def config():
    return SimpleNamespace(ntfy_url='http://ntfy', ntfy_user='alerts', ntfy_password='private',
        notification_routes={
            'talaria': {'topic': 'talaria', 'to': '+12025550101',
                        'bodyPattern': r'^\d+ (?:listings ending soon!|new listings found!) '},
            'grafana': {'topic': 'alerts', 'to': '+12025550101'},
        })


def test_owner_listing_only():
    with patch('sms_relay.notifications.settings', config()):
        assert topic_for('talaria', '+12025550101', '2 new listings found! items') == 'talaria'
        assert topic_for('talaria', '+12025550102', '2 new listings found! items') is None
        assert topic_for('talaria', '+12025550101', 'Your Talaria confirmation code is: 1234') is None
        assert topic_for('talaria', '+12025550101', 'TALARIA ALERT system') is None
        assert topic_for('unconfigured', '+12025550101', 'anything') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [httpx.Response(401, request=httpx.Request('POST','http://ntfy')),
    httpx.Response(200, json={}, request=httpx.Request('POST','http://ntfy'))])
async def test_invalid_ntfy_receipt_is_failure(response):
    with patch('sms_relay.notifications.settings', config()), patch('httpx.AsyncClient') as factory:
        factory.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
        with pytest.raises((httpx.HTTPError, ValueError)):
            await publish(SimpleNamespace(notification_topic='alerts', body='test', service='test'))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_worker_selects_exactly_one_channel(failure):
    stop = asyncio.Event()
    message = SimpleNamespace(id='test', transport='ntfy', notification_topic='alerts',
                              to_number='+12025550101', body='test', attempts=0, error=None)
    session = AsyncMock()
    maker = MagicMock()
    maker.return_value.__aenter__.return_value = session
    provider = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(provider_message_id='sms-id')))
    async def ntfy(_):
        stop.set()
        if failure:
            raise httpx.ConnectError('unavailable')
        return 'ntfy-id'
    with patch.object(worker, 'get_sessionmaker', return_value=maker), \
         patch.object(worker, 'get_provider', return_value=provider), \
         patch.object(worker, '_requeue_stuck', new=AsyncMock()), \
         patch.object(worker, '_claim_next', new=AsyncMock(return_value=message)), \
         patch.object(worker, 'publish', side_effect=ntfy):
        await worker.send_loop(stop)
    if failure:
        assert message.transport == 'sms-fallback'
        assert message.status == MessageStatus.SENT
        provider.send.assert_awaited_once()
        assert session.commit.await_count == 2
    else:
        assert message.status == MessageStatus.PUBLISHED
        provider.send.assert_not_awaited()


def alert():
    return {'alerts':[{'status':'firing', 'fingerprint':'test', 'startsAt':'2026-10-19T00:00:00Z',
                      'labels':{'alertname':'Test'},'annotations':{'summary':'test'}}]}


def test_grafana_auth_size_validation_and_dedup(client):
    settings = config()
    settings.api_keys = {'grafana-test': 'grafana', 'caller-test':'talaria'}
    with patch('sms_relay.auth.settings', settings), patch('sms_relay.routes.grafana.settings', settings):
        assert client.post('/api/v1/grafana',json=alert()).status_code == 401
        assert client.post('/api/v1/grafana',json=alert(),headers={'X-API-Key':'caller-test'}).status_code == 403
        headers={'Authorization':'Bearer grafana-test'}
        assert client.post('/api/v1/grafana',content=b'x'*65537,headers=headers).status_code == 413
        assert client.post('/api/v1/grafana',json={'alerts':[None]},headers=headers).status_code == 400
        malformed = alert(); malformed['alerts'][0]['annotations'] = None
        assert client.post('/api/v1/grafana',json=malformed,headers=headers).status_code == 400
        first=client.post('/api/v1/grafana',json=alert(),headers=headers)
        second=client.post('/api/v1/grafana',json=alert(),headers=headers)
        assert first.status_code == second.status_code == 202
        messages=client.get('/api/v1/messages?direction=outbound',headers=headers).json()['messages']
        assert len(messages) == 1
        payload=alert();payload['alerts'][0]['status']='resolved'
        assert client.post('/api/v1/grafana',json=payload,headers=headers).status_code == 202
        assert len(client.get('/api/v1/messages?direction=outbound',headers=headers).json()['messages']) == 2
        assert client.get('/api/v1/grafana',headers=headers).status_code == 405


@pytest.mark.asyncio
async def test_existing_database_migration_preserves_rows(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sms_relay import db
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'old.db'))
    async with engine.begin() as conn:
        await conn.execute(text('CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT)'))
        await conn.execute(text("INSERT INTO messages VALUES ('old', 'preserved')"))
    with patch.object(db, 'get_engine', return_value=engine):
        await db.init_db()
        await db.init_db()
    async with engine.connect() as conn:
        row = (await conn.execute(text('SELECT body,transport,notification_topic FROM messages'))).one()
        assert tuple(row) == ('preserved', 'sms', None)
    await engine.dispose()
