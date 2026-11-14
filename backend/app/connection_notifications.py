"""Durable, consent-bound notifications projected from the procurement outbox.

Transport delivery uses the spec 33 sender and SDK MCP sessions. No supplier
text or credential is copied into a notification. Each send reauthorizes scope.
"""
import asyncio
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import audit
from .auth import check_scope
from .canonical import canonical_json
from .db import DomainError
from .foundation import PRINCIPAL, EVENT_ACTIONS
from .connection_notifications_models import NotificationConsent, NotificationPreferences

RESPONSE_CAUSE = ContextVar('notification_response_cause', default=None)
EVENT_ACTIONS.update(DELIVERY_ISSUE='order.delivery_issue', DELIVERY_DELAYED='order.delivery_delayed')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS connection_notification_preferences (
 connection_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connection_notification_channels (
 connection_id TEXT NOT NULL, channel TEXT NOT NULL, tenant_id TEXT NOT NULL,
 digest TEXT NOT NULL, recipient TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(connection_id,channel));
CREATE TABLE IF NOT EXISTS connection_notification_items (
 connection_id TEXT NOT NULL, event_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
 data TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT,
 PRIMARY KEY(connection_id,event_id));
CREATE TABLE IF NOT EXISTS connection_notification_sources (
 connection_id TEXT NOT NULL, event_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
 PRIMARY KEY(connection_id,event_id));
CREATE TABLE IF NOT EXISTS connection_notification_resource_events (
 event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, resource TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS connection_notification_revisions (
 resource TEXT NOT NULL, tenant_id TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(resource,tenant_id));
CREATE TABLE IF NOT EXISTS connection_notification_deliveries (
 connection_id TEXT NOT NULL, event_id TEXT NOT NULL, channel TEXT NOT NULL, tenant_id TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due_at TEXT NOT NULL,
 delivered_at TEXT, last_error TEXT,
 PRIMARY KEY(connection_id,event_id,channel));
CREATE TABLE IF NOT EXISTS connection_notification_attempts (
 connection_id TEXT NOT NULL, event_id TEXT NOT NULL, channel TEXT NOT NULL, tenant_id TEXT NOT NULL, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS connection_notification_attempt_window ON connection_notification_attempts(connection_id,ts);
CREATE TABLE IF NOT EXISTS connection_notification_claims (
 event_id TEXT NOT NULL, tenant_id TEXT NOT NULL, offered_to TEXT NOT NULL, state TEXT NOT NULL,
 deadline TEXT NOT NULL, holder TEXT, lease_until TEXT, visited TEXT NOT NULL,
 PRIMARY KEY(event_id,tenant_id));
CREATE TABLE IF NOT EXISTS connection_notification_storms (
 cause_id TEXT NOT NULL, tenant_id TEXT NOT NULL, event_id TEXT NOT NULL,
 PRIMARY KEY(cause_id,tenant_id));
CREATE TABLE IF NOT EXISTS connection_notification_escalations (
 event_id TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, position INTEGER NOT NULL,
 state TEXT NOT NULL, review_url TEXT, PRIMARY KEY(event_id,tenant_id,user_id));
'''
TEMPLATES = {
 'approval_needed': ('urgent', 'An order needs human approval.'),
 'order_decided': ('info', 'An order decision is available.'),
 'stockout_predicted': ('warning', 'A product may run out of stock.'),
 'anomaly': ('warning', 'A purchasing anomaly needs attention.'),
 'proposal_received': ('info', 'A supplier proposal is ready for review.'),
 'suggestion_due': ('info', 'A replenishment suggestion is due.'),
 'agent_paused': ('urgent', 'An agent has been paused.'),
 'policy_changed': ('info', 'Your purchasing policy has changed.'),
 'delivery_issue': ('urgent', 'A delivery issue needs attention.'),
}
EVENTS = {
 'order.approval_required': 'approval_needed', 'order.evidence_changed': 'approval_needed',
 **{f'order.{s}': 'order_decided' for s in ('confirmed', 'rejected', 'cancelled', 'expired')},
 'proposal.received': 'proposal_received', 'stockout.predicted': 'stockout_predicted',
 'stockout.alert': 'stockout_predicted', 'stockout.detected': 'stockout_predicted',
 'suggestion.shown': 'suggestion_due', 'suggestion.due': 'suggestion_due',
 'quote.anomaly_detected': 'anomaly', 'agent.anomaly_alert': 'anomaly',
 'governance.alert': 'anomaly', 'agent.suspended': 'agent_paused',
 'governance.agent_paused': 'agent_paused', 'governance.tenant_paused': 'agent_paused',
 'policy.updated': 'policy_changed', 'governance.policy_changed': 'policy_changed',
 'governance.guardrails_changed': 'policy_changed', 'governance.rolled_back': 'policy_changed',
 'delivery.issue': 'delivery_issue', 'delivery.delayed': 'delivery_issue', 'order.delivery_issue': 'delivery_issue',
 'order.delivery_delayed': 'delivery_issue',
}
ACTION_NEEDED = {'approval_needed', 'delivery_issue', 'anomaly', 'stockout_predicted'}


def lineage(event_id, data):
    parent = RESPONSE_CAUSE.get()
    return {'cause_id': parent['cause_id'] if parent else event_id, 'hop': parent['hop'] + 1 if parent else 0}


def classify(event):
    kind = event['type']
    if kind == 'order.placed':
        kind = {'PENDING_APPROVAL': 'order.approval_required', 'CONFIRMED': 'order.confirmed',
                'REJECTED': 'order.rejected'}.get(event['payload'].get('status'))
    return EVENTS.get(kind)


class ConnectionNotifications:
    max_hops = 6
    fanout_budget = 50
    coalesce_seconds = 60
    claim_seconds = 300
    max_attempts = 5

    def __init__(self, gateway):
        self.gateway, self.db = gateway, gateway.db
        self.lock = threading.RLock()
        self.active = threading.local()
        self.streams = []
        with self.db.connect(write=True, notify=False) as conn:
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    conn.execute(statement)
        # Include additive tables in tenant TEMP views on subsequent connections.
        with self.db.connect() as conn:
            self.db._tenant_columns.update({r[0]: [c[1] for c in conn.execute(f'PRAGMA main.table_info("{r[0]}")')]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'connection_notification_%' AND type='table'").fetchall()})
        gateway.outbox.register('connection-notifications', self.project)

    def connection(self, cid, conn):
        row = conn.execute('SELECT * FROM connections WHERE connection_id=?', (cid,)).fetchone()
        if not row or row['revoked_at'] or row['expires_at'] and row['expires_at'] <= audit.utcnow().isoformat():
            raise DomainError('CONNECTION_NOT_FOUND', 'Active connection was not found', 404)
        return row

    def initialize(self, cid, consent=None):
        with self.db.connect(write=True, notify=False) as conn:
            row = self.connection(cid, conn)
            preferences = NotificationPreferences(consent=consent or NotificationConsent())
            conn.execute('INSERT OR IGNORE INTO connection_notification_preferences(connection_id,tenant_id,data) VALUES(?,?,?)',
                         (cid, row['tenant_id'], preferences.model_dump_json()))
            self.bind_channel(cid, 'mcp', 'stream', self.stream_recipient(row), human=True)

    @staticmethod
    def stream_recipient(row):
        return row['agent_id'] + ':' + (row['created_by'] or '')

    @staticmethod
    def transport(row):
        # OAuth is a credential kind; spec 43 stores it in type for both MCP/A2A.
        if row['type'] == 'oauth':
            return 'a2a' if row['client'] == 'a2a' else 'mcp'
        return row['type']

    def prefs(self, cid, conn):
        row = conn.execute('SELECT data FROM connection_notification_preferences WHERE connection_id=?', (cid,)).fetchone()
        return NotificationPreferences.model_validate_json(row[0]) if row else NotificationPreferences()

    def authorize(self, cid, p, *, human=False):
        if human:
            from .connections import require_connection_admin
            require_connection_admin(p)
        if p.connection_id != cid or human:
            self.gateway.connections.get(cid, p)
        with self.db.connect() as conn:
            return self.connection(cid, conn)

    def preferences(self, cid, p):
        self.authorize(cid, p)
        with self.db.connect() as conn:
            pref = self.prefs(cid, conn).model_dump(exclude={'reconsent_channels'})
            pref['channels'] = [dict(r) for r in conn.execute(
                'SELECT channel,digest AS destination_digest,recipient,status FROM connection_notification_channels WHERE connection_id=?', (cid,))]
        return pref

    def configure(self, cid, body, p):
        row = self.authorize(cid, p, human=True)
        if body.quiet_hours:
            try:
                ZoneInfo(body.quiet_hours.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise DomainError('VALIDATION_ERROR', 'Use an IANA quiet-hours timezone', 422) from None
        # Consent always challenges the current destination, not a cached URL.
        verified = {}
        for channel in body.reconsent_channels:
            if channel != 'mcp':
                with self.db.connect() as conn:
                    sub = self.subscription(channel, cid, conn)
                self.gateway.webhooks._challenge(sub['url'], self.headers(sub))
                verified[channel] = self.subscription_identity(sub)
        with self.db.connect(write=True) as conn:
            self.authorize(cid, p, human=True)
            conn.execute('INSERT INTO connection_notification_preferences(connection_id,tenant_id,data) VALUES(?,?,?) '
                         'ON CONFLICT(connection_id) DO UPDATE SET data=excluded.data',
                         (cid, row['tenant_id'], body.model_dump_json(exclude={'reconsent_channels'})))
            for channel in body.reconsent_channels:
                if channel == 'mcp':
                    self.bind_channel(cid, channel, 'stream', self.stream_recipient(row), human=True)
                else:
                    sub = self.subscription(channel, cid, conn)
                    if self.subscription_identity(sub) != verified[channel]:
                        raise DomainError('CONSENT_STALE', 'Destination changed during consent; review and retry', 409)
                    self.bind_subscription(sub, human=True)
        return self.preferences(cid, p)

    def bind_channel(self, cid, channel, destination, recipient, *, human=False):
        digest = sha256(destination.encode()).hexdigest()
        with self.db.connect(write=True, notify=False) as conn:
            row = self.connection(cid, conn)
            previous = conn.execute('SELECT * FROM connection_notification_channels WHERE connection_id=? AND channel=?', (cid, channel)).fetchone()
            status = 'active' if human else 'CONSENT_STALE'
            if previous and previous['digest'] == digest and previous['recipient'] == recipient and not human:
                status = previous['status']
            conn.execute('INSERT INTO connection_notification_channels(connection_id,channel,tenant_id,digest,recipient,status) VALUES(?,?,?,?,?,?) '
                         'ON CONFLICT(connection_id,channel) DO UPDATE SET digest=excluded.digest,recipient=excluded.recipient,status=excluded.status',
                         (cid, channel, row['tenant_id'], digest, recipient, status))

    def bind_subscription(self, sub, *, human=False):
        if sub.get('connection_id'):
            self.bind_channel(sub['connection_id'], 'subscription:' + sub['id'], *self.subscription_identity(sub), human=human)

    @staticmethod
    def subscription_identity(sub):
        # Authentication changes are destination/recipient changes too.
        destination = canonical_json({'url': sub['url'], 'authentication': sub.get('push_config'),
                                      'audience': sub['audience'], 'customers': sub['customer_ids']}).decode()
        return destination, sub.get('recipient') or sub['owner']

    @staticmethod
    def headers(sub):
        from .webhooks import notification_headers
        return notification_headers(sub.get('push_config') or {})

    def delegate(self, event, sub):
        """Retire the legacy queue entry only after the durable projection exists."""
        self.project(event)
        with self.db.connect(write=True, notify=False) as conn:
            if sub.get('tenant_id') != event['tenant_id']:
                audit.append(conn, 'system', 'NOTIFICATION_TENANT_MISMATCH', 'Dropped an event assigned to a foreign subscription.', data={'connection_id': sub['connection_id'], 'tenant_id': sub.get('tenant_id')})
            conn.execute("UPDATE outbox SET status='skipped' WHERE event_id=? AND consumer=?", (event['id'], 'webhook:' + sub['id']))

    def legacy_allowed(self, event, sub):
        """The same consent binding protects non-notification A2A lifecycle pushes."""
        if not sub.get('connection_id'):
            return True
        with self.db.connect(write=True, notify=False) as conn:
            try:
                row = self.connection(sub['connection_id'], conn)
                if event['tenant_id'] != row['tenant_id'] or sub.get('tenant_id') != row['tenant_id']:
                    audit.append(conn, 'system', 'NOTIFICATION_TENANT_MISMATCH', 'Dropped a push with a mismatched tenant.', data={'connection_id': row['connection_id'], 'tenant_id': row['tenant_id']})
                    return False
                self.bind_subscription(sub)
                binding = conn.execute('SELECT status FROM connection_notification_channels WHERE connection_id=? AND channel=?',
                                       (row['connection_id'], 'subscription:' + sub['id'])).fetchone()
                if not binding or binding[0] != 'active':
                    return False
                order_id = event['payload'].get('task', {}).get('metadata', {}).get('order_id')
                if event['type'] == 'task.status_updated' and order_id:
                    source = conn.execute("SELECT id FROM events WHERE aggregate_type='order' AND aggregate_id=? ORDER BY aggregate_revision DESC LIMIT 1", (order_id,)).fetchone()
                    source = self.event(source[0], conn) if source else None
                    if source and classify(source):
                        # The outbox's domain consumer owns the original id;
                        # suppress this echo without projecting a later event early.
                        conn.execute("UPDATE outbox SET status='skipped' WHERE event_id=? AND consumer=?", (event['id'], 'webhook:' + sub['id']))
                        return False
                return True
            except DomainError:
                return False

    def subscription_deliveries(self, cid, subscription_id):
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute('SELECT event_id AS id,event_id,status,attempts,due_at AS next_attempt_at,last_error,NULL AS http_status '
                'FROM connection_notification_deliveries WHERE connection_id=? AND channel=? ORDER BY rowid', (cid, 'subscription:' + subscription_id))]

    def redeliver(self, cid, subscription_id, event_id):
        with self.db.connect(write=True) as conn:
            count = conn.execute("UPDATE connection_notification_deliveries SET status='pending',attempts=0,due_at=? WHERE connection_id=? AND event_id=? AND channel=?",
                                 (audit.utcnow().isoformat(), cid, event_id, 'subscription:' + subscription_id)).rowcount
            if not count:
                raise DomainError('WEBHOOK_NOT_FOUND', 'Delivery was not found', 404)
        return {'id': event_id, 'status': 'pending'}

    def subscription(self, channel, cid, conn):
        row = conn.execute('SELECT data FROM webhook_subscriptions WHERE id=?', (channel.removeprefix('subscription:'),)).fetchone()
        sub = json.loads(row[0]) if row else None
        if not sub or sub.get('connection_id') != cid or not sub['enabled']:
            raise DomainError('WEBHOOK_NOT_FOUND', 'Active destination was not found', 404)
        return sub

    def allowed(self, conn, event, row, kind, *, handoff=False):
        if event.get('tenant_id') != row['tenant_id'] or event['environment'] != self.gateway.settings.environment:
            return False
        if not getattr(self.prefs(row['connection_id'], conn).consent, kind):
            return False
        customer = event.get('customer_id')
        if customer and customer not in json.loads(row['customer_ids']):
            return False
        try:
            from .agent_surface import surface
            p = surface(self.gateway).connection_principal(row)
            check_scope(p, self.scope_for(kind), 'read')
            if kind in {'policy_changed', 'agent_paused'}:
                return not event['payload'].get('agent_id') or event['payload']['agent_id'] == row['agent_id'] or p.kind != 'agent'
            if handoff and kind in ACTION_NEEDED:
                return bool(customer)
            from .visibility import event_visible
            return event_visible(self.gateway, conn, event, p)
        except DomainError:
            return False

    @staticmethod
    def scope_for(kind):
        return {'approval_needed': 'orders', 'order_decided': 'orders', 'delivery_issue': 'orders',
                'stockout_predicted': 'products', 'proposal_received': 'proposals', 'suggestion_due': 'suggestions',
                'policy_changed': 'policy', 'agent_paused': 'agents', 'anomaly': 'orders'}[kind]

    def resource(self, event, kind):
        payload = event['payload']
        if event['aggregate_type'] == 'order' or payload.get('order_id'):
            return 'luotsi://orders/' + (payload.get('order_id') or event['aggregate_id'])
        if event['aggregate_type'] in {'product', 'catalog_product'}:
            return 'luotsi://catalog/products/' + event['aggregate_id']
        if kind is None:
            return None
        if kind == 'stockout_predicted' and payload.get('product_id'):
            return 'luotsi://catalog/products/' + str(payload['product_id'])
        if kind == 'policy_changed':
            return 'luotsi://policy/effective'
        return 'luotsi://agent/context' if kind == 'agent_paused' else 'luotsi://inbox'

    def envelope(self, event, row, kind):
        severity, summary = TEMPLATES[kind]
        resource = self.resource(event, kind)
        actions = []
        # Fixed vocabulary; never use a supplier-provided URL or tool name.
        if kind == 'approval_needed':
            oid = resource.rsplit('/', 1)[-1]
            revision = event['payload'].get('evidence_revision') or 1
            actions.append({'href': f'/approvals/{oid}?revision={int(revision)}'})
        tool, scope = ('get_order_status', 'orders') if '/orders/' in resource else ('get_agent_context', 'read')
        p = None
        try:
            from .agent_surface import surface
            p = surface(self.gateway).connection_principal(row)
            check_scope(p, scope, 'read')
            actions.append({'tool': tool})
        except DomainError:
            pass
        if kind in ACTION_NEEDED and p is not None:
            try:
                check_scope(p, 'notifications', 'write')
                actions.append({'tool': 'notification_action'})
            except DomainError:
                pass
        payload = event['payload']
        with self.db.connect() as conn:
            version = conn.execute('SELECT revision FROM connection_notification_resource_events WHERE event_id=? AND tenant_id=?', (event['id'], row['tenant_id'])).fetchone()
        result = {'id': event['id'], 'type': kind, 'severity': severity, 'summary': summary, 'resource': resource,
                  'actions': actions, 'expires_at': (datetime.fromisoformat(event['ts']) + timedelta(days=1)).isoformat(),
                  'aud': row['connection_id'], 'tenant': row['tenant_id'], 'resource_revision': version[0] if version else event['aggregate_revision'],
                  'state': payload.get('status') if payload.get('status') in {'PENDING_APPROVAL', 'CONFIRMED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'PENDING_EXECUTION'} else 'attention_needed',
                  'cause_id': payload.get('cause_id') or event['id'], 'hop': int(payload.get('hop', 0)), 'count': 1}
        if event.get('storm'):
            result.update(code='EVENT_STORM', summary='A notification response chain was stopped.', severity='urgent')
        result['signature'] = self.gateway.signing.sign(result)
        return result

    def resign(self, value):
        value.pop('signature', None)
        value['signature'] = self.gateway.signing.sign(value)
        return value

    def queue(self, conn, event, row, kind, *, handoff=False):
        cid = row['connection_id']
        if not conn.execute('INSERT OR IGNORE INTO connection_notification_sources(connection_id,event_id,tenant_id) VALUES(?,?,?)',
                            (cid, event['id'], row['tenant_id'])).rowcount:
            return
        value = self.envelope(event, row, kind)
        now = audit.utcnow()
        # Buffer non-urgent updates for a full window; delivery never races the tenth event.
        delay = 0 if value['severity'] == 'urgent' else self.coalesce_seconds
        previous = conn.execute('SELECT i.event_id,i.data FROM connection_notification_items i WHERE i.connection_id=? AND i.created_at>=? '
            'AND json_extract(i.data,\'$.type\')=? AND json_extract(i.data,\'$.resource\')=? '
            "AND i.read_at IS NULL AND NOT EXISTS(SELECT 1 FROM connection_notification_deliveries d WHERE d.connection_id=i.connection_id AND d.event_id=i.event_id AND (d.attempts>0 OR d.status='delivered')) "
            'ORDER BY i.rowid DESC LIMIT 1', (cid, (now - timedelta(seconds=self.coalesce_seconds)).isoformat(), kind, value['resource'])).fetchone() if delay else None
        if previous:
            old = json.loads(previous['data'])
            old.update(count=old['count'] + 1, resource_revision=value['resource_revision'], state=value['state'],
                       cause_id=value['cause_id'], hop=value['hop'])
            conn.execute('UPDATE connection_notification_items SET data=? WHERE connection_id=? AND event_id=?',
                         (json.dumps(self.resign(old)), cid, previous['event_id']))
            return
        conn.execute('INSERT INTO connection_notification_items(connection_id,event_id,tenant_id,data,created_at) VALUES(?,?,?,?,?)',
                     (cid, event['id'], row['tenant_id'], json.dumps(value), now.isoformat()))
        transport = self.transport(row)
        channels = ['mcp'] if transport in {'mcp', 'rest', 'tool_pack'} else []
        task_destination = ''
        for subrow in conn.execute('SELECT data FROM webhook_subscriptions WHERE json_extract(data,\'$.connection_id\')=? AND json_extract(data,\'$.enabled\')=1', (cid,)).fetchall():
            sub = json.loads(subrow[0])
            if sub.get('task_id'):
                taskrow = conn.execute('SELECT data FROM a2a_tasks WHERE id=?', (sub['task_id'],)).fetchone()
                task = json.loads(taskrow[0]) if taskrow else {}
                oid = task.get('metadata', {}).get('order_id')
                event_task = event['payload'].get('links', {}).get('task_id')
                if event_task and event_task != sub['task_id'] or oid and '/orders/' in value['resource'] and not value['resource'].endswith('/' + oid):
                    continue
                task_destination = sub['task_id']
            elif self.gateway.webhooks.event_type(event) not in sub['event_types']:
                continue
            channels.append('subscription:' + sub['id'])
        if transport == 'a2a':
            self.a2a_body(value, {'task_id': task_destination}, row)
        if not channels:
            channels = ['inbox']
        for channel in channels:
            conn.execute('INSERT OR IGNORE INTO connection_notification_deliveries(connection_id,event_id,channel,tenant_id,status,due_at) VALUES(?,?,?,?,?,?)',
                         (cid, event['id'], channel, row['tenant_id'], 'pending', (now + timedelta(seconds=delay)).isoformat()))
        if kind in ACTION_NEEDED:
            conn.execute('INSERT OR IGNORE INTO connection_notification_claims(event_id,tenant_id,offered_to,state,deadline,visited) VALUES(?,?,?,?,?,?)',
                         (event['id'], row['tenant_id'], cid, 'delivered', (now + timedelta(seconds=self.claim_seconds)).isoformat(), json.dumps([cid])))

    def project(self, event):
        kind = classify(event)
        resource = self.resource(event, kind)
        if resource is None:
            return
        with self.db.connect(write=True, notify=False) as conn:
            if not conn.execute('SELECT 1 FROM connection_notification_resource_events WHERE event_id=?', (event['id'],)).fetchone():
                conn.execute('INSERT INTO connection_notification_revisions(resource,tenant_id,revision) VALUES(?,?,1) '
                             'ON CONFLICT(resource,tenant_id) DO UPDATE SET revision=revision+1', (resource, event['tenant_id']))
                revision = conn.execute('SELECT revision FROM connection_notification_revisions WHERE resource=? AND tenant_id=?', (resource, event['tenant_id'])).fetchone()[0]
                conn.execute('INSERT INTO connection_notification_resource_events(event_id,tenant_id,resource,revision) VALUES(?,?,?,?)',
                             (event['id'], event['tenant_id'], resource, revision))
            if kind is None:
                return  # Resource revisions still advance for non-notifying changes.
            if kind == 'order_decided':
                conn.execute("UPDATE connection_notification_claims SET state='resolved',holder=NULL,lease_until=NULL WHERE tenant_id=? AND event_id IN "
                             "(SELECT event_id FROM connection_notification_items WHERE tenant_id=? AND json_extract(data,'$.resource')=? AND json_extract(data,'$.type')='approval_needed')",
                             (event['tenant_id'], event['tenant_id'], resource))
            rows = conn.execute('SELECT * FROM connections WHERE revoked_at IS NULL ORDER BY created_at,rowid').fetchall()
            cause = event['payload'].get('cause_id') or event['id']
            count = conn.execute("SELECT COUNT(*) FROM events WHERE tenant_id=? AND json_extract(payload,'$.cause_id')=?", (event['tenant_id'], cause)).fetchone()[0]
            if int(event['payload'].get('hop', 0)) > self.max_hops or count > self.fanout_budget:
                self.storm(conn, event)
                return
            matched = [r for r in rows if self.allowed(conn, event, r, kind)]
            fanout = conn.execute("SELECT COUNT(*) FROM connection_notification_items WHERE tenant_id=? AND json_extract(data,'$.cause_id')=?", (event['tenant_id'], cause)).fetchone()[0]
            new_recipients = sum(not conn.execute('SELECT 1 FROM connection_notification_sources WHERE connection_id=? AND event_id=?',
                                                  (r['connection_id'], event['id'])).fetchone() for r in matched)
            if fanout + new_recipients > self.fanout_budget:
                self.storm(conn, event)
                return
            for row in matched:
                self.queue(conn, event, row, kind)

    def storm(self, conn, event):
        cause = event['payload'].get('cause_id') or event['id']
        digest = 'storm_' + sha256((event['tenant_id'] + cause).encode()).hexdigest()[:24]
        inserted = conn.execute('INSERT OR IGNORE INTO connection_notification_storms(cause_id,tenant_id,event_id) VALUES(?,?,?)',
                                (cause, event['tenant_id'], digest)).rowcount
        if not inserted:
            return
        audit.append(conn, 'system', 'EVENT_STORM', 'A notification response chain exceeded its budget.', data={'cause_id': cause, 'tenant_id': event['tenant_id']})
        digest_event = {**event, 'id': digest, 'storm': True, 'payload': {**event['payload'], 'hop': 0}}
        for row in conn.execute('SELECT * FROM connections WHERE tenant_id=? AND revoked_at IS NULL ORDER BY created_at,rowid', (event['tenant_id'],)).fetchall():
            if self.allowed(conn, event, row, 'anomaly', handoff=True):
                self.queue(conn, digest_event, row, 'anomaly')
                item = conn.execute('SELECT data FROM connection_notification_items WHERE connection_id=? AND event_id=?', (row['connection_id'], digest)).fetchone()
                if item:
                    value = json.loads(item[0]); value.update(code='EVENT_STORM', summary='A notification response chain was stopped.', severity='urgent')
                    conn.execute('UPDATE connection_notification_items SET data=? WHERE connection_id=? AND event_id=?', (json.dumps(self.resign(value)), row['connection_id'], digest))
                    conn.execute('UPDATE connection_notification_deliveries SET due_at=? WHERE event_id=?', (audit.utcnow().isoformat(), digest))
                break

    def quiet(self, preferences, now):
        hours = preferences.quiet_hours
        if not hours:
            return False
        current = now.astimezone(ZoneInfo(hours.timezone)).strftime('%H:%M')
        return hours.start <= current < hours.end if hours.start < hours.end else current >= hours.start or current < hours.end

    def event(self, event_id, conn):
        row = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not row:
            return None
        event = dict(row); event['payload'] = json.loads(event['payload'])
        return event

    def check_send(self, conn, job, value):
        row = self.connection(job['connection_id'], conn)
        event = self.event(value['id'], conn)
        if not event and value.get('code') == 'EVENT_STORM':
            event = self.event(value['cause_id'], conn)
        if not event or event['tenant_id'] != row['tenant_id'] or job['tenant_id'] != event['tenant_id'] or value['tenant'] != row['tenant_id'] or value['aud'] != row['connection_id']:
            audit.append(conn, 'system', 'NOTIFICATION_TENANT_MISMATCH', 'Dropped a notification with mismatched audience or tenant.', data={'connection_id': job['connection_id'], 'tenant_id': row['tenant_id']})
            raise DomainError('NOTIFICATION_TENANT_MISMATCH', 'Notification audience mismatch', 403)
        if not self.allowed(conn, event, row, value['type'], handoff=True):
            raise DomainError('NOTIFICATION_NOT_ALLOWED', 'Consent or scope no longer permits this event', 403)
        channel = job['channel']
        sub = None
        if channel.startswith('subscription:'):
            sub = self.subscription(channel, row['connection_id'], conn)
            if sub.get('tenant_id') != event['tenant_id'] or not set(sub['customer_ids']) <= set(json.loads(row['customer_ids'])):
                audit.append(conn, 'system', 'NOTIFICATION_TENANT_MISMATCH', 'Dropped a notification with mismatched subscription tenant.', data={'connection_id': row['connection_id'], 'tenant_id': row['tenant_id']})
                raise DomainError('NOTIFICATION_TENANT_MISMATCH', 'Subscription tenant mismatch', 403)
            self.bind_subscription(sub)
        elif channel == 'mcp':
            self.bind_channel(row['connection_id'], 'mcp', 'stream', self.stream_recipient(row))
        if channel != 'inbox':
            binding = conn.execute('SELECT status FROM connection_notification_channels WHERE connection_id=? AND channel=?', (row['connection_id'], channel)).fetchone()
            if not binding or binding[0] != 'active':
                raise DomainError('CONSENT_STALE', 'A human must re-consent to this destination and recipient', 409)
        return row, sub

    def dispatch(self, limit=100):
        if getattr(self.active, 'running', False) or self.gateway.outbox.lock.locked():
            return
        with self.lock:
            self.active.running = True
            token = PRINCIPAL.set(None)
            try:
                self.handoff()
                with self.db.connect() as conn:
                    jobs = conn.execute("SELECT d.*,i.data FROM connection_notification_deliveries d JOIN connection_notification_items i USING(connection_id,event_id) WHERE d.status='pending' ORDER BY d.rowid LIMIT ?", (limit,)).fetchall()
                blocked = set()
                for job in jobs:
                    cid = job['connection_id']
                    queue = (cid, job['channel'])
                    if queue in blocked:
                        continue
                    value, now = json.loads(job['data']), audit.utcnow()
                    status, error, attempt = 'pending', None, 0
                    with self.db.connect(write=True, notify=False) as conn:
                        try:
                            row, sub = self.check_send(conn, job, value)
                        except DomainError as exc:
                            # Commit audit entries even when a delivery is refused.
                            status, error = ('pending' if exc.code == 'CONSENT_STALE' else 'dropped'), exc.code
                            row, sub = None, None
                        pref = self.prefs(cid, conn)
                        sent = conn.execute('SELECT COUNT(*) FROM connection_notification_attempts WHERE connection_id=? AND ts>=?',
                                            (cid, (now - timedelta(minutes=1)).isoformat())).fetchone()[0]
                        if row is not None and (job['due_at'] > now.isoformat() or sent >= pref.rate_per_minute or value['severity'] != 'urgent' and self.quiet(pref, now)):
                            blocked.add(queue)
                            continue
                        if row is not None and value['expires_at'] <= now.isoformat():
                            row, status, error = None, 'dropped', 'EXPIRED'
                    if row is not None:
                        try:
                            if sub:
                                body = self.a2a_body(value, sub, row) if sub.get('task_id') else value
                                response = self.gateway.webhooks._post(sub['url'], canonical_json(body), value['id'], self.headers(sub))
                                attempt = 1
                                if not 200 <= response.status_code < 300:
                                    raise ValueError('Destination refused delivery')
                            elif job['channel'] == 'mcp':
                                if not self.send_mcp(value):
                                    blocked.add(queue)
                                    continue
                                attempt = 1
                            else:
                                blocked.add(queue)
                                continue  # No stream: the durable inbox is the delivery channel.
                            status = 'delivered'
                        except Exception:
                            attempt = 1
                            status = 'failed' if job['attempts'] + 1 >= self.max_attempts else 'pending'
                            error = 'DESTINATION_UNAVAILABLE'
                    delay = 2 ** min(job['attempts'] + 1, 12) * (1 + self.gateway.webhooks.jitter() * .25)
                    with self.db.connect(write=True, notify=False) as conn:
                        if attempt:
                            conn.execute('INSERT INTO connection_notification_attempts(connection_id,event_id,channel,tenant_id,ts) VALUES(?,?,?,?,?)',
                                         (cid, job['event_id'], job['channel'], value['tenant'], now.isoformat()))
                        conn.execute('UPDATE connection_notification_deliveries SET status=?,attempts=attempts+?,due_at=?,delivered_at=?,last_error=? WHERE connection_id=? AND event_id=? AND channel=?',
                                     (status, attempt, (now + timedelta(seconds=delay)).isoformat(), now.isoformat() if status == 'delivered' else None, error, cid, job['event_id'], job['channel']))
                    if status == 'pending':
                        blocked.add(queue)
            finally:
                PRINCIPAL.reset(token)
                self.active.running = False

    def a2a_body(self, value, sub, row):
        from a2a import types as t
        from google.protobuf.json_format import MessageToDict
        from .a2a import proto, artifact, status_message
        from .agent_surface import surface
        p = surface(self.gateway).connection_principal(row)
        owner = p.kind + ':' + p.id
        with self.db.connect(write=True, notify=False) as conn:
            stored = conn.execute('SELECT data,tenant_id,owner FROM a2a_tasks WHERE id=?', (sub['task_id'],)).fetchone()
            if stored and (stored['tenant_id'] != value['tenant'] or stored['owner'] != owner):
                raise DomainError('NOTIFICATION_TENANT_MISMATCH', 'A2A task ownership mismatch', 403)
            original = json.loads(stored['data']) if stored else {}
            tied = original.get('metadata', {}).get('order_id') and value['resource'].endswith('/' + original['metadata']['order_id'])
            task_id = sub['task_id'] if tied else 'notification_' + value['id'] + '_' + row['connection_id']
            stored = conn.execute('SELECT data FROM a2a_tasks WHERE id=?', (task_id,)).fetchone()
            task = proto(t.Task, json.loads(stored[0])) if stored else t.Task(id=task_id, context_id=task_id)
            task.metadata.update({'owner': p.id, 'owner_scope': owner, 'notification_id': value['id']})
            artifact(task, 'notification', value)
            if not tied:
                task.status.CopyFrom(t.TaskStatus(state=t.TaskState.TASK_STATE_INPUT_REQUIRED,
                    message=status_message(task_id, value['summary'], value)))
            conn.execute('INSERT INTO a2a_tasks(id,owner,data,tenant_id) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                         (task.id, owner, json.dumps(MessageToDict(task)), row['tenant_id']))
        # A task's existing status message may contain supplier artifacts. A push
        # contains only the template summary and signed notification envelope.
        safe_status = t.TaskStatus(state=task.status.state, message=status_message(task.id, value['summary'], value))
        return MessageToDict(t.StreamResponse(status_update=t.TaskStatusUpdateEvent(task_id=task.id, context_id=task.context_id,
            status=safe_status, metadata={'notification': value, 'event_id': value['id']})))

    def send_mcp(self, value):
        entries = [(loop, server, p, session) for loop, server, p, session in self.streams
                   if p.connection_id == value['aud'] and not loop.is_closed()]
        if not entries:
            return False
        for loop, server, p, session in entries:
            try:
                if asyncio.get_running_loop() is loop:
                    return False  # The background delivery worker will retry off-loop.
            except RuntimeError:
                pass
            future = asyncio.run_coroutine_threadsafe(self.emit_mcp(server, p, session, value), loop)
            # Dispatch always runs on the sync worker; the event loop remains free.
            try:
                future.result(timeout=5)
            except Exception:
                future.cancel()
                self.streams[:] = [e for e in self.streams if e[3]._connection is not session._connection]
                raise
        return True

    async def emit_mcp(self, server, p, session, value):
        from mcp.shared.subscriptions import ResourceUpdated
        token = PRINCIPAL.set(p)
        try:
            with self.db.connect(write=True, notify=False) as conn:
                self.check_send(conn, {'connection_id': p.connection_id, 'channel': 'mcp', 'tenant_id': value['tenant']}, value)
            await session.send_resource_updated(value['resource'])
            await session.send_log_message('warning' if value['severity'] == 'urgent' else 'info', value, logger='luotsi.notifications')
            await server._subscriptions.publish(ResourceUpdated(uri=value['resource']))
            if value['resource'] != 'luotsi://inbox':
                await server._subscriptions.publish(ResourceUpdated(uri='luotsi://inbox'))
        finally:
            PRINCIPAL.reset(token)

    def inbox(self, p, cursor=0, limit=50, mark_read=False):
        if not p.connection_id:
            return {'notifications': [], 'next_cursor': str(cursor), 'escalations': self.human_inbox(p)}
        with self.db.connect(write=True, notify=False) as conn:
            connection = self.connection(p.connection_id, conn)
            try:
                cursor = int(cursor)
                if cursor < 0:
                    raise ValueError()
            except (TypeError, ValueError):
                raise DomainError('VALIDATION_ERROR', 'Use the opaque cursor returned by get_notifications', 422) from None
            rows = conn.execute('SELECT rowid AS seq,* FROM connection_notification_items WHERE connection_id=? AND tenant_id=? AND rowid>? ORDER BY rowid LIMIT ?',
                                (p.connection_id, p.tenant_id, cursor, limit)).fetchall()
            result = []
            for item in rows:
                value = json.loads(item['data'])
                if not getattr(self.prefs(p.connection_id, conn).consent, value['type']) or value['expires_at'] <= audit.utcnow().isoformat():
                    continue
                try:
                    self.check_send(conn, {'connection_id': p.connection_id, 'channel': 'mcp', 'tenant_id': p.tenant_id}, value)
                except DomainError:
                    continue
                preference = self.prefs(p.connection_id, conn)
                pending = conn.execute("SELECT MIN(due_at) FROM connection_notification_deliveries WHERE connection_id=? AND event_id=? AND status='pending'", (p.connection_id, item['event_id'])).fetchone()[0]
                if pending and pending > audit.utcnow().isoformat() or value['severity'] != 'urgent' and self.quiet(preference, audit.utcnow()):
                    # Do not advance past held items: reconnecting after quiet hours must find them.
                    return {'notifications': result, 'next_cursor': str(int(item['seq']) - 1)}
                check_scope(p, self.scope_for(value['type']), 'read')
                seen = conn.execute("SELECT 1 FROM connection_notification_deliveries WHERE connection_id=? AND event_id=? AND status='delivered'", (p.connection_id, item['event_id'])).fetchone()
                sent = conn.execute('SELECT COUNT(*) FROM connection_notification_attempts WHERE connection_id=? AND ts>=?',
                                    (p.connection_id, (audit.utcnow() - timedelta(minutes=1)).isoformat())).fetchone()[0]
                if not seen and sent >= preference.rate_per_minute:
                    return {'notifications': result, 'next_cursor': str(int(item['seq']) - 1)}
                result.append(value)
                now = audit.utcnow().isoformat()
                channel = 'mcp' if self.transport(connection) in {'mcp', 'rest', 'tool_pack'} else 'inbox'
                if not seen:
                    conn.execute('INSERT INTO connection_notification_attempts(connection_id,event_id,channel,tenant_id,ts) VALUES(?,?,?,?,?)', (p.connection_id, item['event_id'], channel, p.tenant_id, now))
                conn.execute("INSERT OR IGNORE INTO connection_notification_deliveries(connection_id,event_id,channel,tenant_id,status,due_at,delivered_at,attempts) VALUES(?,?,?,?,'delivered',?,?,1)",
                             (p.connection_id, item['event_id'], channel, p.tenant_id, now, now))
                conn.execute("UPDATE connection_notification_deliveries SET status='delivered',delivered_at=COALESCE(delivered_at,?),attempts=MAX(attempts,1) WHERE connection_id=? AND event_id=? AND channel=?", (now, p.connection_id, item['event_id'], channel))
                if mark_read:
                    conn.execute('UPDATE connection_notification_items SET read_at=? WHERE connection_id=? AND event_id=?', (now, p.connection_id, item['event_id']))
            return {'notifications': result, 'next_cursor': str(rows[-1]['seq'] if rows else cursor), 'escalations': self.human_inbox(p)}

    def health(self, cid):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT status,attempts,COUNT(*) n FROM connection_notification_deliveries WHERE connection_id=? GROUP BY status,attempts', (cid,)).fetchall()
            read = conn.execute('SELECT COUNT(*) FROM connection_notification_items WHERE connection_id=? AND read_at IS NOT NULL', (cid,)).fetchone()[0]
        return {'sent': sum(r['attempts'] * r['n'] for r in rows), 'delivered': sum(r['n'] for r in rows if r['status'] == 'delivered'),
                'read': read, 'failed': sum(r['n'] for r in rows if r['status'] == 'failed')}

    def current(self, conn, value):
        event = self.event(value['id'], conn)
        if not event:
            return value['resource_revision'], value['state'], 'get_notifications'
        latest = conn.execute('SELECT aggregate_revision,payload FROM events WHERE aggregate_type=? AND aggregate_id=? ORDER BY aggregate_revision DESC LIMIT 1',
                              (event['aggregate_type'], event['aggregate_id'])).fetchone()
        resource_revision = conn.execute('SELECT revision FROM connection_notification_revisions WHERE resource=? AND tenant_id=?', (value['resource'], value['tenant'])).fetchone()
        state, next_action = value['state'], 'get_notifications'
        if '/orders/' in value['resource']:
            row = conn.execute('SELECT status,data FROM orders WHERE id=?', (value['resource'].rsplit('/', 1)[-1],)).fetchone()
            if row:
                state = row['status']; next_action = json.loads(row['data']).get('next_action') or 'get_order_status'
        return resource_revision[0] if resource_revision else latest[0], state, next_action

    @contextmanager
    def response(self, p, *, notification_id=None, cause_id=None):
        if not notification_id and not cause_id:
            yield
            return
        with self.db.connect(write=True, notify=False) as conn:
            self.connection(p.connection_id, conn)
            row = conn.execute('SELECT data FROM connection_notification_items WHERE connection_id=? AND tenant_id=? AND ' +
                ('event_id=?' if notification_id else "json_extract(data,'$.cause_id')=? ORDER BY rowid DESC LIMIT 1"),
                (p.connection_id, p.tenant_id, notification_id or cause_id)).fetchone()
            if not row:
                raise DomainError('NOTIFICATION_NOT_FOUND', 'Notification was not found', 404)
            value = json.loads(row[0])
            if cause_id and cause_id != value['cause_id']:
                raise DomainError('NOTIFICATION_NOT_FOUND', 'Notification cause was not found', 404)
            revision, state, next_action = self.current(conn, value)
            if revision != value['resource_revision'] or state != value['state'] or value['expires_at'] <= audit.utcnow().isoformat():
                raise DomainError('STALE_EVENT', 'Refresh the resource before acting on this notification', 409,
                                  {'state': state, 'resource_revision': revision, 'next_action': next_action})
            count = conn.execute("SELECT COUNT(*) FROM events WHERE tenant_id=? AND json_extract(payload,'$.cause_id')=?", (p.tenant_id, value['cause_id'])).fetchone()[0]
            fanout = conn.execute("SELECT COUNT(*) FROM connection_notification_items WHERE tenant_id=? AND json_extract(data,'$.cause_id')=?", (p.tenant_id, value['cause_id'])).fetchone()[0]
            storm = value['hop'] >= self.max_hops or count >= self.fanout_budget or fanout >= self.fanout_budget
            if storm:
                event = self.event(value['id'], conn)
                if event:
                    event['payload'].update(cause_id=value['cause_id'], hop=value['hop'])
                    self.storm(conn, event)
        if storm:
            raise DomainError('EVENT_STORM', 'The notification response budget is exhausted', 409)
        token = RESPONSE_CAUSE.set({'cause_id': value['cause_id'], 'hop': value['hop']})
        try:
            yield
        finally:
            RESPONSE_CAUSE.reset(token)

    def act(self, event_id, body, p):
        with self.db.connect(write=True) as conn:
            identity = p.connection_id
            item = conn.execute('SELECT data FROM connection_notification_items WHERE event_id=? AND connection_id=?', (event_id, identity)).fetchone()
            if item:
                self.connection(identity, conn)
                value = json.loads(item[0])
                check_scope(p, self.scope_for(value['type']), 'read')
                revision, state, next_action = self.current(conn, value)
                if revision != value['resource_revision'] or state != value['state']:
                    raise DomainError('STALE_EVENT', 'Refresh the resource before taking ownership', 409,
                                      {'state': state, 'resource_revision': revision, 'next_action': next_action})
            elif p.kind == 'tenant_user' and p.role in {'approver', 'tenant_admin'}:
                offered = conn.execute("SELECT state FROM connection_notification_escalations WHERE event_id=? AND tenant_id=? AND user_id=? AND state IN ('offered','claimed','resolved')", (event_id, p.tenant_id, p.user_id)).fetchone()
                if offered:
                    identity = 'human:' + p.user_id
            claim = conn.execute('SELECT * FROM connection_notification_claims WHERE event_id=? AND tenant_id=?', (event_id, p.tenant_id)).fetchone()
            if not claim or not item and identity == p.connection_id:
                raise DomainError('NOTIFICATION_NOT_FOUND', 'Notification was not found', 404)
            now = audit.utcnow()
            if claim['holder'] and claim['lease_until'] and claim['lease_until'] > now.isoformat() and claim['holder'] != identity:
                raise DomainError('ALREADY_CLAIMED', 'Another connection holds the exclusive lease', 409)
            if claim['offered_to'] != identity:
                raise DomainError('ALREADY_CLAIMED', 'The event has been offered to the next recipient', 409)
            if claim['state'] == 'resolved':
                return {'id': event_id, 'state': 'resolved'}
            if item and body.action == 'resolve' and value['type'] == 'approval_needed' and value['state'] == 'PENDING_APPROVAL':
                raise DomainError('INVALID_TRANSITION', 'A human must decide the order through the approval flow', 409)
            if body.action == 'resolve' and (claim['holder'] != identity or not claim['lease_until'] or claim['lease_until'] <= now.isoformat()):
                raise DomainError('ALREADY_CLAIMED', 'An active exclusive lease is required to resolve', 409)
            state = {'acknowledge': 'acknowledged', 'claim': 'claimed', 'resolve': 'resolved'}[body.action]
            if state == 'claimed' and claim['state'] in {'delivered', 'escalated'}:
                audit.append(conn, identity, 'NOTIFICATION_ACKNOWLEDGED', 'Claiming the event acknowledged its delivery.', data={'event_id': event_id, 'tenant_id': p.tenant_id})
            if state == 'acknowledged' and claim['state'] == 'claimed':
                state = 'claimed'
            holder = identity if state in {'claimed', 'resolved'} else None
            until = (now + timedelta(seconds=body.lease_seconds)).isoformat() if state == 'claimed' else None
            conn.execute('UPDATE connection_notification_claims SET state=?,holder=?,lease_until=? WHERE event_id=? AND tenant_id=?',
                         (state, holder, until, event_id, p.tenant_id))
            if identity.startswith('human:'):
                conn.execute('UPDATE connection_notification_escalations SET state=? WHERE event_id=? AND tenant_id=? AND user_id=?',
                             (state, event_id, p.tenant_id, p.user_id))
            audit.append(conn, 'agent:' + p.agent_id, 'NOTIFICATION_' + state.upper(), 'Updated notification ownership.', data={'event_id': event_id, 'connection_id': p.connection_id, 'tenant_id': p.tenant_id})
            return {'id': event_id, 'state': state, 'lease_until': until}

    def human_inbox(self, p):
        if p.kind != 'tenant_user' or p.role not in {'approver', 'tenant_admin'}:
            return []
        with self.db.connect() as conn:
            rows = conn.execute("SELECT x.event_id,x.state,i.data FROM connection_notification_escalations x JOIN connection_notification_items i ON i.event_id=x.event_id AND i.tenant_id=x.tenant_id WHERE x.user_id=? AND x.tenant_id=? AND x.state IN ('offered','acknowledged','claimed') GROUP BY x.event_id ORDER BY x.rowid", (p.user_id, p.tenant_id)).fetchall()
        return [{'id': r['event_id'], 'state': r['state'], **{k: json.loads(r['data'])[k] for k in ('summary', 'resource', 'actions')}} for r in rows]

    def handoff(self):
        with self.db.connect(write=True, notify=False) as conn:
            now = audit.utcnow()
            claims = conn.execute("SELECT * FROM connection_notification_claims WHERE state NOT IN ('resolved','exhausted') AND deadline<=? AND (lease_until IS NULL OR lease_until<=?)", (now.isoformat(), now.isoformat())).fetchall()
            for claim in claims:
                event = self.event(claim['event_id'], conn)
                if not event:
                    continue
                kind, visited = classify(event), json.loads(claim['visited'])
                if claim['state'] == 'escalated' or claim['offered_to'].startswith('human:'):
                    conn.execute("UPDATE connection_notification_escalations SET state='expired' WHERE event_id=? AND tenant_id=? AND state IN ('offered','acknowledged','claimed')", (event['id'], event['tenant_id']))
                    next_user = conn.execute("SELECT user_id FROM connection_notification_escalations WHERE event_id=? AND tenant_id=? AND state='waiting' ORDER BY position LIMIT 1", (event['id'], event['tenant_id'])).fetchone()
                    if next_user:
                        conn.execute("UPDATE connection_notification_escalations SET state='offered' WHERE event_id=? AND tenant_id=? AND user_id=?", (event['id'], event['tenant_id'], next_user[0]))
                    conn.execute('UPDATE connection_notification_claims SET state=?,offered_to=?,holder=NULL,lease_until=NULL,deadline=? WHERE event_id=? AND tenant_id=?',
                                 ('escalated' if next_user else 'exhausted', 'human:' + next_user[0] if next_user else claim['offered_to'], (now + timedelta(seconds=self.claim_seconds)).isoformat(), event['id'], event['tenant_id']))
                    continue
                candidate = next((r for r in conn.execute('SELECT * FROM connections WHERE tenant_id=? AND revoked_at IS NULL ORDER BY created_at,rowid', (claim['tenant_id'],)).fetchall()
                                  if r['connection_id'] not in visited and self.allowed(conn, event, r, kind, handoff=True)), None)
                if candidate:
                    self.queue(conn, event, candidate, kind, handoff=True)
                    visited.append(candidate['connection_id'])
                    conn.execute("UPDATE connection_notification_claims SET offered_to=?,state='delivered',holder=NULL,lease_until=NULL,deadline=?,visited=? WHERE event_id=? AND tenant_id=?",
                                 (candidate['connection_id'], (now + timedelta(seconds=self.claim_seconds)).isoformat(), json.dumps(visited), event['id'], event['tenant_id']))
                else:
                    # Durable human ladder, approvers first and tenant admins next.
                    users = conn.execute("SELECT id FROM users WHERE tenant_id=? AND disabled_at IS NULL AND removed_at IS NULL AND role IN ('approver','tenant_admin') ORDER BY CASE role WHEN 'approver' THEN 0 ELSE 1 END,id", (claim['tenant_id'],)).fetchall()
                    for position, user in enumerate(users):
                        conn.execute('INSERT OR IGNORE INTO connection_notification_escalations(event_id,tenant_id,user_id,position,state,review_url) VALUES(?,?,?,?,?,?)',
                                     (event['id'], event['tenant_id'], user[0], position, 'offered' if position == 0 else 'waiting', '/approvals/' + event['aggregate_id'] if kind == 'approval_needed' else None))
                    conn.execute("UPDATE connection_notification_claims SET state=?,offered_to=?,deadline=?,holder=NULL,lease_until=NULL WHERE event_id=? AND tenant_id=?",
                                 ('escalated' if users else 'exhausted', 'human:' + users[0][0] if users else claim['offered_to'], (now + timedelta(seconds=self.claim_seconds)).isoformat(), event['id'], event['tenant_id']))
                    audit.append(conn, 'system', 'NOTIFICATION_ESCALATED', 'Offered an unclaimed event to the tenant human escalation ladder.', data={'event_id': event['id'], 'tenant_id': event['tenant_id']})
