"""One offline, focused test for each spec 61 acceptance criterion."""
import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from a2a import types as at
from fastapi.testclient import TestClient
from mcp import Client
from mcp import types as mt

from app import audit
from app.a2a import proto
from app.agent_surface import surface
from app.api import create_app
from app.auth import Principal
from app.connection_notifications import lineage
from app.connection_notifications_models import NotificationAction, NotificationConsent, NotificationPreferences
from app.db import DomainError
from app.foundation import PRINCIPAL
from app.mcp_server import create_server
from app.models import ConnectionRequest, QuoteRequest
from app.webhooks import SubscriptionCreate, SubscriptionPatch, verify_webhook
from conftest import OPERATOR_HEADERS


def operator(gateway):
    return gateway.accounts.bind(gateway.settings.keys.resolve('test-operator-token', gateway.policy.get()))


def connect(gateway, agent='external', **changes):
    from app.connection_contract import manager
    changes.setdefault('contract_version', manager(gateway).current()['version'])
    return gateway.connections.create(ConnectionRequest(agent_id=agent, customer_ids=['1'], **changes),
                                      operator(gateway), 'http://localhost:8000', True)


def principal(gateway, connection):
    with gateway.db.connect() as conn:
        row = conn.execute('SELECT * FROM connections WHERE connection_id=?', (connection.connection_id,)).fetchone()
    return surface(gateway).connection_principal(row)


def pending_order(gateway, need):
    lookup = gateway.find_offers(need, actor='agent:external')
    quote = gateway.orders.create_quote(QuoteRequest(customer_id='1', deliver_by=need.deliver_by,
        need_ref=lookup.lookup_id, lines=[{'product_id': '1', 'quantity_sales_units': 200}]), 'external')
    return gateway.orders.place_order(quote.id, 'notification-order', 'external')


def emit(gateway, event_type='stockout.predicted', *, aggregate='product-1', kind='stockout', tenant='hotel-aurora', customer='1', **payload):
    event_id = uuid4().hex
    with gateway.db.connect(write=True) as conn:
        revision = conn.execute('SELECT COALESCE(MAX(aggregate_revision),0)+1 FROM events WHERE aggregate_type=? AND aggregate_id=?', (kind, aggregate)).fetchone()[0]
        entry = audit.append(conn, 'system', 'NOTIFICATION_TEST_EVENT', 'Synthetic domain event.', data={'tenant_id': tenant})
        entry = conn.execute('SELECT MAX(id) FROM audit').fetchone()[0]
        data = {'context': {'customer_id': customer}, 'product_id': aggregate, **payload, **lineage(event_id, payload)}
        conn.execute('INSERT INTO events(id,schema_version,type,customer_id,environment,aggregate_type,aggregate_id,aggregate_revision,actor,ts,correlation_id,causation_id,payload,audit_id,tenant_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (event_id, 1, event_type, customer, 'live', kind, aggregate, revision, 'system', audit.utcnow().isoformat(), event_id, None, json.dumps(data), entry, tenant))
        conn.execute("INSERT INTO outbox(event_id,consumer,status,attempts,next_attempt_at,tenant_id) SELECT ?,name,'pending',0,?,? FROM event_consumers WHERE enabled=1", (event_id, audit.utcnow().isoformat(), tenant))
    return event_id


def receiver(gateway, *, status=200):
    calls = []
    mode = {'status': status}
    def handle(request):
        value = json.loads(request.content)
        if value.get('type') == 'webhook.challenge':
            return httpx.Response(mode.get('challenge_status', 200), json={'challenge': value['challenge']})
        calls.append((value, dict(request.headers), request.content))
        return httpx.Response(mode['status'])
    gateway.webhooks.resolver = lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))]
    gateway.webhooks.client.close()
    gateway.webhooks.client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    return calls, mode


def subscribe(gateway, p, *, task_id=None):
    sub = gateway.webhooks.create(SubscriptionCreate(url='https://receiver.test/notify', customer_ids=['1'],
        event_types=['order.approval_required', 'order.confirmed', 'proposal.received']), p,
        task_id=task_id, push_config={'taskId': task_id, 'url': 'https://receiver.test/notify'} if task_id else None)
    preferences = gateway.notifications.preferences(p.connection_id, operator(gateway))
    gateway.notifications.configure(p.connection_id, NotificationPreferences(consent=NotificationConsent(**preferences['consent']),
        reconsent_channels=['subscription:' + sub['id']]), operator(gateway))
    return sub


@pytest.fixture
def time_control(monkeypatch):
    current = [audit.utcnow()]
    monkeypatch.setattr(audit, 'utcnow', lambda: current[0])
    def advance(seconds):
        current[0] += timedelta(seconds=seconds)
    return advance


def test_AC_1_default_consent_sdk_mcp_approval_link_and_agent_actions(gateway, need, upstream):
    connection = connect(gateway)
    p = principal(gateway, connection)
    async def run():
        logs = []
        updates = []
        async def message(value):
            if getattr(value, 'method', None) == 'notifications/resources/updated':
                updates.append(str(value.params.uri))
        async def log(params):
            if isinstance(params.data, dict) and params.data.get('type') == 'approval_needed':
                logs.append(params)
        token = PRINCIPAL.set(p)
        try:
            server = create_server(gateway, 'external')
            async with Client(server, mode='legacy', logging_callback=log, log_level='info', message_handler=message) as client:
                await client.read_resource('luotsi://inbox')
                def place():
                    token = PRINCIPAL.set(None)
                    try:
                        return pending_order(gateway, need)
                    finally:
                        PRINCIPAL.reset(token)
                order = await asyncio.to_thread(place)
                assert order.status == 'PENDING_APPROVAL'
                for _ in range(100):
                    if logs:
                        break
                    await asyncio.sleep(.01)
                await asyncio.to_thread(gateway.notifications.dispatch)
                assert len(logs) == 1, ([v.data['id'] for v in logs], [(type(e[3]).__name__, id(e[3])) for e in gateway.notifications.streams])
                value = logs[0].data
                assert value['type'] == 'approval_needed'
                assert value['resource'] == 'luotsi://orders/' + order.id
                assert value['resource'] in updates
                assert any(a.get('href', '').startswith('/approvals/' + order.id) for a in value['actions'])
                assert not any('approve' in a.get('tool', '') for a in value['actions'])
                assert gateway.notifications.health(p.connection_id)['delivered'] == 1
        finally:
            PRINCIPAL.reset(token)
    asyncio.run(run())


def test_AC_2_a2a_task_push_and_standard_webhook_share_event_id(gateway, need, upstream, time_control):
    calls, _ = receiver(gateway)
    a2a = connect(gateway, type='a2a', client='a2a')
    webhook = connect(gateway, type='webhook', webhook_url='https://receiver.test/webhook')
    p = principal(gateway, a2a)
    task = at.Task(id='notification-channel-task', context_id='notification-channel-task')
    task.metadata.update({'owner': p.agent_id, 'owner_scope': 'agent:' + p.agent_id, 'context': {'customer_id': '1'}})
    with gateway.db.connect(write=True, notify=False) as conn:
        gateway.a2a._save(conn, task, p.agent_id)
    config = proto(at.TaskPushNotificationConfig, {'taskId': task.id, 'url': 'https://receiver.test/a2a'})
    gateway.a2a.create_push(config, p)
    with gateway.db.connect() as conn:
        sub = conn.execute('SELECT subscription_id FROM a2a_push_configs WHERE task_id=?', (task.id,)).fetchone()[0]
    gateway.notifications.configure(p.connection_id, NotificationPreferences(reconsent_channels=['subscription:' + sub]), operator(gateway))
    order = pending_order(gateway, need)
    notifications = [v.get('statusUpdate', {}).get('metadata', {}).get('notification', v) for v, _, _ in calls]
    assert len(notifications) == 2
    assert len({v['id'] for v in notifications}) == 1
    assert all(v['resource'].endswith(order.id) and v['type'] == 'approval_needed' for v in notifications)
    for _, headers, body in calls:
        assert verify_webhook(body, headers, gateway.signing.jwks())
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM a2a_tasks WHERE id LIKE 'notification_%' AND json_extract(data,'$.metadata.notification_id') IS NOT NULL").fetchone()[0] == 1
    # A later event tied to a procurement task must not also push its task-projection echo.
    task.metadata.update({'order_id': order.id})
    with gateway.db.connect(write=True, notify=False) as conn:
        gateway.a2a._save(conn, task, p.agent_id)
    calls.clear()
    gateway.orders.approve(order.id, 'Ada')
    time_control(61)
    gateway.notifications.dispatch()
    decided = [v.get('statusUpdate', {}).get('metadata', {}).get('notification', v) for v, _, _ in calls]
    assert len(decided) == 2 and len({v['id'] for v in decided}) == 1
    assert all(v['type'] == 'order_decided' for v in decided)


def test_AC_3_consent_changes_apply_to_next_event_only(gateway, time_control):
    connection = connect(gateway)
    p = principal(gateway, connection)
    first = emit(gateway, 'proposal.received', kind='proposal', aggregate='proposal-one')
    assert gateway.notifications.inbox(p)['notifications'] == []
    with TestClient(create_app(gateway, serve_mcp=False)) as http:
        url = f'/v1/connections/{p.connection_id}/notifications'
        response = http.get(url, headers=OPERATOR_HEADERS)
        assert response.json()['consent']['proposal_received'] is False
        assert http.put(url, headers=OPERATOR_HEADERS, json={'consent': {'proposal_received': True}}).status_code == 200
    second = emit(gateway, 'proposal.received', kind='proposal', aggregate='proposal-two')
    time_control(61)
    assert [v['id'] for v in gateway.notifications.inbox(p)['notifications']] == [second]
    assert first != second


def test_AC_4_sdk_cursor_inbox_and_mark_read_without_stream(gateway, need, upstream):
    connection = connect(gateway)
    p = principal(gateway, connection)
    a2a = connect(gateway, type='a2a', client='a2a')
    with gateway.db.connect(write=True, notify=False) as conn:
        # The OAuth issuer retains the protocol in client but changes type to oauth.
        conn.execute("UPDATE connections SET type='oauth',credential_kind='oauth' WHERE connection_id=?", (a2a.connection_id,))
    order = pending_order(gateway, need)
    with gateway.db.connect() as conn:
        assert conn.execute("SELECT 1 FROM a2a_tasks WHERE json_extract(data,'$.metadata.notification_id') IS NOT NULL AND id LIKE ?",
                            ('%_' + a2a.connection_id,)).fetchone()
    async def run():
        token = PRINCIPAL.set(p)
        try:
            server = create_server(gateway, 'external')
            result = await server.call_tool('get_notifications', {'mark_read': True})
            assert not result.is_error
            value = result.structured_content
            assert value['notifications'][0]['resource'].endswith(order.id)
            assert (await server.call_tool('get_notifications', {'cursor': value['next_cursor']})).structured_content['notifications'] == []
            resource = await server.read_resource('luotsi://inbox')
            assert order.id in str(resource)
            health = surface(gateway).health(p.connection_id, operator(gateway))['notifications']
            assert health['read'] == health['delivered'] == 1
        finally:
            PRINCIPAL.reset(token)
    asyncio.run(run())


def test_AC_5_coalescing_quiet_hours_rate_order_and_retry(gateway, time_control):
    calls, mode = receiver(gateway)
    consent = NotificationConsent(stockout_predicted=True, proposal_received=True)
    connection = connect(gateway, notifications=consent)
    p = principal(gateway, connection)
    sub = subscribe(gateway, p)
    gateway.webhooks.update(sub['id'], SubscriptionPatch(event_types=['order.approval_required', 'order.confirmed', 'proposal.received', 'stockout.predicted']), p)
    gateway.notifications.configure(p.connection_id, NotificationPreferences(consent=consent,
        quiet_hours={'start': '00:00', 'end': '00:00'}, rate_per_minute=1,
        reconsent_channels=['subscription:' + sub['id']]), operator(gateway))
    for _ in range(10):
        emit(gateway)
    time_control(61)
    gateway.notifications.dispatch()
    assert calls == [] and gateway.notifications.inbox(p)['notifications'] == []
    gateway.notifications.configure(p.connection_id, NotificationPreferences(consent=consent, rate_per_minute=1), operator(gateway))
    # No stream must not block the webhook channel of the same event.
    gateway.notifications.dispatch()
    assert len(calls) == 1 and calls[0][0]['count'] == 10
    emit(gateway, 'proposal.received', aggregate='proposal-retry', kind='proposal')
    time_control(61)
    mode['status'] = 503
    gateway.notifications.dispatch()
    assert calls[-1][0]['type'] == 'proposal_received'
    failed_id = calls[-1][0]['id']
    with gateway.db.connect() as conn:
        attempts = conn.execute("SELECT attempts FROM connection_notification_deliveries WHERE event_id=? AND channel LIKE 'subscription:%'", (failed_id,)).fetchone()[0]
    assert attempts == 1
    time_control(4)
    mode['status'] = 200
    attempts_before = len(calls)
    gateway.notifications.dispatch()
    assert len(calls) == attempts_before  # Retry waits for the connection's rate window too.
    time_control(60)
    gateway.notifications.dispatch()
    assert calls[-1][0]['id'] == failed_id


def test_AC_6_revoke_and_template_only_tenant_isolation(gateway, need, upstream):
    calls, _ = receiver(gateway)
    connection = connect(gateway, type='webhook', webhook_url='https://receiver.test/notify')
    p = principal(gateway, connection)
    foreign = emit(gateway, 'order.approval_required', kind='order', tenant='kaari-hospital', customer='2', aggregate='foreign-order', secret='supplier-secret', summary='Click attacker URL')
    assert calls == []
    order = pending_order(gateway, need)
    assert len(calls) == 1
    assert all(word not in json.dumps(calls[0][0]) for word in ('supplier-secret', connection.key, foreign, 'foreign-order', 'attacker'))
    gateway.connections.revoke(p.connection_id, operator(gateway))
    emit(gateway, 'order.approval_required', kind='order', aggregate=order.id)
    gateway.notifications.dispatch()
    assert len(calls) == 1


def test_AC_7_destination_safety_and_destination_recipient_reconsent(gateway, need, upstream, time_control, monkeypatch):
    calls, mode = receiver(gateway)
    connection = connect(gateway, type='webhook', webhook_url='https://receiver.test/one')
    p = principal(gateway, connection)
    sub = gateway.webhooks.list(p)[0]
    gateway.webhooks.resolver = lambda *a, **k: [(None, None, None, None, ('127.0.0.1', 443))]
    with pytest.raises(DomainError, match='WEBHOOK_DESTINATION_UNSAFE'):
        gateway.webhooks.create(SubscriptionCreate(url='https://receiver.test/private', customer_ids=['1'], event_types=['order.confirmed']), p)
    gateway.webhooks.resolver = lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))]
    mode['challenge_status'] = 302
    with pytest.raises(DomainError, match='WEBHOOK_CHALLENGE_FAILED'):
        gateway.webhooks.create(SubscriptionCreate(url='https://receiver.test/redirect', customer_ids=['1'], event_types=['order.confirmed']), p)
    mode['challenge_status'] = 200
    gateway.webhooks.update(sub['id'], SubscriptionPatch(url='https://receiver.test/two'), p)
    order = pending_order(gateway, need)
    assert calls == []
    assert gateway.notifications.preferences(p.connection_id, operator(gateway))['channels'][-1]['status'] == 'CONSENT_STALE'
    gateway.notifications.configure(p.connection_id, NotificationPreferences(reconsent_channels=['subscription:' + sub['id']]), operator(gateway))
    gateway.webhooks.resolver = lambda *a, **k: [(None, None, None, None, ('10.0.0.4', 443))]
    time_control(3); gateway.notifications.dispatch()
    assert calls == []
    gateway.webhooks.resolver = lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))]
    mode['status'] = 302
    time_control(6); gateway.notifications.dispatch()
    assert len(calls) == 1  # One POST, never a follow-up request to a redirect target.
    assert gateway.notifications.health(p.connection_id)['delivered'] == 0
    mode['status'] = 200
    time_control(10); gateway.notifications.dispatch()
    assert calls[-1][0]['resource'].endswith(order.id)
    assert gateway.notifications.health(p.connection_id)['delivered'] == 1
    gateway.webhooks.update(sub['id'], SubscriptionPatch(recipient='replacement-recipient'), p)
    assert next(c for c in gateway.notifications.preferences(p.connection_id, operator(gateway))['channels'] if c['channel'].endswith(sub['id']))['status'] == 'CONSENT_STALE'
    with pytest.raises(DomainError):
        gateway.notifications.configure(p.connection_id, NotificationPreferences(reconsent_channels=['subscription:' + sub['id']]), p)
    challenge = gateway.webhooks._challenge
    def changed_during_challenge(url, headers=None):
        challenge(url, headers)
        with gateway.db.connect(write=True, notify=False) as conn:
            current = json.loads(conn.execute('SELECT data FROM webhook_subscriptions WHERE id=?', (sub['id'],)).fetchone()[0])
            current['recipient'] = 'changed-during-consent'
            conn.execute('UPDATE webhook_subscriptions SET data=? WHERE id=?', (json.dumps(current), sub['id']))
    monkeypatch.setattr(gateway.webhooks, '_challenge', changed_during_challenge)
    with pytest.raises(DomainError, match='CONSENT_STALE'):
        gateway.notifications.configure(p.connection_id, NotificationPreferences(reconsent_channels=['subscription:' + sub['id']]), operator(gateway))


def test_AC_8_worker_audits_cross_tenant_and_reference_rejects_wrong_audience(gateway, need, upstream):
    calls, _ = receiver(gateway)
    connection = connect(gateway, type='webhook', webhook_url='https://receiver.test/notify')
    pending_order(gateway, need)
    with gateway.db.connect() as conn:
        states = [dict(r) for r in conn.execute('SELECT status,last_error,channel FROM connection_notification_deliveries')]
    assert calls, states
    value = calls[0][0]
    module_spec = importlib.util.spec_from_file_location('notification_receiver', Path(__file__).parents[2] / 'clients/connection_notifications.py')
    module = importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(module)
    assert module.verify_notification(value, gateway.signing.jwks(), connection_id=connection.connection_id, tenant='hotel-aurora')
    with pytest.raises(ValueError, match='audience'):
        module.verify_notification(value, gateway.signing.jwks(), connection_id='another-connection', tenant='hotel-aurora')
    with gateway.db.connect(write=True, notify=False) as conn:
        sub = json.loads(conn.execute('SELECT data FROM webhook_subscriptions LIMIT 1').fetchone()[0])
        sub['tenant_id'] = 'kaari-hospital'
        conn.execute('UPDATE webhook_subscriptions SET data=? WHERE id=?', (json.dumps(sub), sub['id']))
        conn.execute("UPDATE connection_notification_deliveries SET status='pending',due_at=?", (audit.utcnow().isoformat(),))
    gateway.notifications.dispatch()
    assert len(calls) == 1
    assert any(e.action == 'NOTIFICATION_TENANT_MISMATCH' for e in gateway.audit())


def test_AC_9_feedback_chain_budget_single_digest_and_stale_action_409(gateway, need, upstream, time_control):
    consent = NotificationConsent(stockout_predicted=True, anomaly=True)
    first = connect(gateway, notifications=consent)
    second = connect(gateway, 'receiver-agent', notifications=consent)
    principals = [principal(gateway, first), principal(gateway, second)]
    root = emit(gateway)
    for hop in range(gateway.notifications.max_hops + 1):
        time_control(61)
        p = principals[hop % 2]
        current = next(v for v in reversed(gateway.notifications.inbox(p)['notifications']) if v['cause_id'] == root and not v.get('code'))
        if hop == gateway.notifications.max_hops:
            for _ in range(2):
                with pytest.raises(DomainError, match='EVENT_STORM'):
                    with gateway.notifications.response(p, notification_id=current['id'], cause_id=root):
                        emit(gateway)
        else:
            with gateway.notifications.response(p, notification_id=current['id'], cause_id=root):
                emit(gateway)
    assert sum(e.action == 'EVENT_STORM' for e in gateway.audit()) == 1
    with gateway.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM connection_notification_storms').fetchone()[0] == 1
    order = pending_order(gateway, need)
    notification = next(v for v in gateway.notifications.inbox(principals[0])['notifications'] if v['type'] == 'approval_needed')
    gateway.orders.approve(order.id, 'Ada')
    with TestClient(create_app(gateway, serve_mcp=False)) as http:
        response = http.post(f'/v1/orders/{order.id}/cancel', headers={'Authorization': 'Bearer ' + first.key,
            'X-Notification-Id': notification['id'], 'X-Cause-Id': notification['cause_id']}, json={'reason': 'Review retry', 'cancelled_by': 'Buyer'})
    assert response.status_code == 409 and response.json()['code'] == 'STALE_EVENT'
    assert response.json()['data']['state'] == 'CONFIRMED'
    assert gateway.orders.get_order(order.id, 'external').status == 'CONFIRMED'
    async def native_metadata():
        token = PRINCIPAL.set(principals[0])
        try:
            async with Client(create_server(gateway, 'external'), mode='legacy') as client:
                storm = await client.call_tool('get_notifications', {}, meta=mt.RequestParamsMeta(notification_id=current['id'], cause_id=root))
                assert storm.is_error and storm.structured_content['code'] == 'EVENT_STORM'
                stale = await client.call_tool('cancel_order', {'order_id': order.id}, meta=mt.RequestParamsMeta(notification_id=notification['id'], cause_id=notification['cause_id']))
                assert stale.is_error and stale.structured_content['code'] == 'STALE_EVENT'
        finally:
            PRINCIPAL.reset(token)
    asyncio.run(native_metadata())
    gateway.notifications.fanout_budget = 2
    wide = emit(gateway, aggregate='another-product')
    time_control(61)
    offered = next(v for v in gateway.notifications.inbox(principals[0])['notifications'] if v['id'] == wide)
    with pytest.raises(DomainError, match='EVENT_STORM'):
        with gateway.notifications.response(principals[0], notification_id=offered['id']):
            emit(gateway, aggregate='another-product')
    assert sum(e.action == 'EVENT_STORM' and e.data.get('cause_id') == wide for e in gateway.audit()) == 1


def test_AC_10_expiring_exclusive_claim_handoff_and_human_ladder(gateway, need, upstream, time_control):
    consent = NotificationConsent(delivery_issue=True)
    purchasing = connect(gateway, notifications=consent)
    receiving = connect(gateway, 'receiving-agent', notifications=consent)
    p, r = principal(gateway, purchasing), principal(gateway, receiving)
    order = pending_order(gateway, need)
    event_id = emit(gateway, 'delivery.delayed', kind='order', aggregate=order.id, status='PENDING_APPROVAL')
    assert any(v['id'] == event_id for v in gateway.notifications.inbox(p, mark_read=True)['notifications'])
    assert not any(v['id'] == event_id for v in gateway.notifications.inbox(r)['notifications'])
    time_control(gateway.notifications.claim_seconds + 1)
    gateway.notifications.handoff()
    assert any(v['id'] == event_id for v in gateway.notifications.inbox(r)['notifications'])
    assert gateway.notifications.act(event_id, NotificationAction(action='acknowledge'), r)['state'] == 'acknowledged'
    assert gateway.notifications.act(event_id, NotificationAction(action='claim', lease_seconds=20), r)['state'] == 'claimed'
    with pytest.raises(DomainError) as failure:
        gateway.notifications.act(event_id, NotificationAction(action='claim'), p)
    assert failure.value.status == 409 and failure.value.code == 'ALREADY_CLAIMED'
    assert gateway.notifications.act(event_id, NotificationAction(action='resolve'), r)['state'] == 'resolved'
    assert gateway.notifications.act(event_id, NotificationAction(action='resolve'), r)['state'] == 'resolved'
    # When no connected agent takes ownership, the same event enters the human ladder.
    late = emit(gateway, 'delivery.delayed', kind='order', aggregate=order.id, status='PENDING_APPROVAL')
    for _ in range(2):
        time_control(gateway.notifications.claim_seconds + 1)
        gateway.notifications.handoff()
    human = Principal('aurora-chef', kind='tenant_user', role='approver', user_id='aurora-chef', tenant_id=p.tenant_id, customer_ids=('1',))
    assert any(v['id'] == late for v in gateway.notifications.inbox(human)['escalations'])
    time_control(gateway.notifications.claim_seconds + 1)
    gateway.notifications.handoff()
    admin = Principal('aurora-director', kind='tenant_user', role='tenant_admin', user_id='aurora-director', tenant_id=p.tenant_id, customer_ids=('1',))
    assert any(v['id'] == late for v in gateway.notifications.inbox(admin)['escalations'])
    assert gateway.notifications.act(late, NotificationAction(action='claim'), admin)['state'] == 'claimed'
    assert gateway.notifications.act(late, NotificationAction(action='resolve'), admin)['state'] == 'resolved'
