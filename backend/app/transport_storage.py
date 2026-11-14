"""Additive transport persistence, also installed by catalog/demo database creation."""

WEBHOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_subscriptions (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
 id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES webhook_subscriptions(id),
 event_id TEXT NOT NULL REFERENCES events(id), body TEXT NOT NULL, last_error TEXT,
 http_status INTEGER, UNIQUE(subscription_id,event_id)
);
"""

A2A_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_tasks (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS a2a_messages (
 owner TEXT NOT NULL, message_id TEXT NOT NULL, digest TEXT NOT NULL,
 task_id TEXT NOT NULL REFERENCES a2a_tasks(id), PRIMARY KEY(owner,message_id)
);
CREATE TABLE IF NOT EXISTS a2a_push_configs (
 task_id TEXT NOT NULL REFERENCES a2a_tasks(id), id TEXT NOT NULL,
 subscription_id TEXT NOT NULL REFERENCES webhook_subscriptions(id), data TEXT NOT NULL,
 PRIMARY KEY(task_id,id)
);
"""
