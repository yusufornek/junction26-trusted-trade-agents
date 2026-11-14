"""Tenant persistence and connection-local row visibility for all business readers.

TEMP views shadow business tables for reads, including joins and nested queries.
Writes target main explicitly and retain the same predicate. The SQL remains
parameterized; the tenant comes from the authenticated principal, never input SQL.
"""
import json
import re
import sqlite3
from contextvars import ContextVar

GLOBAL_AUDIT = ContextVar("global_audit", default=False)
TARGET_TENANT = ContextVar("target_tenant", default=None)
HOTEL = "hotel-aurora"
HOSPITAL = "kaari-hospital"
SHARED = {"products", "products_fts", "catalog_metadata", "supplier_products", "workspace",
          "runtime_files", "event_consumers", "audit_anchors", "tenants", "tenant_customers",
          "users", "sessions", "account_links", "login_attempts", "tenant_agents",
          "tenant_issuers", "connection_secrets", "decision_confirmations",
          "audience_oauth_clients", "audience_oauth_requests", "audience_oauth_codes", "audience_oauth_tokens"}


def principal():
    from .foundation import PRINCIPAL
    return PRINCIPAL.get()


def tenant():
    p = principal()
    return p.tenant_id if p and not p.operator else None


def migrate(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,disabled_at TEXT);
    CREATE TABLE IF NOT EXISTS tenant_customers(customer_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL REFERENCES tenants(id));
    CREATE TABLE IF NOT EXISTS tenant_agents(agent_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,name TEXT NOT NULL,disabled_at TEXT);
    CREATE TABLE IF NOT EXISTS tenant_issuers(tenant_id TEXT PRIMARY KEY,private_key TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,tenant_id TEXT,email TEXT NOT NULL UNIQUE,role TEXT NOT NULL,
        password_hash TEXT,created_at TEXT NOT NULL,disabled_at TEXT,last_sign_in TEXT,invited_by TEXT,removed_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf_digest TEXT NOT NULL,expires_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS account_links(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL,kind TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT);
    CREATE TABLE IF NOT EXISTS login_attempts(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL,reset_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS connection_secrets(digest TEXT PRIMARY KEY,connection_id TEXT NOT NULL,expires_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS decision_confirmations(digest TEXT PRIMARY KEY,user_id TEXT NOT NULL,tenant_id TEXT,
        operation TEXT NOT NULL,target TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER,expires_at TEXT NOT NULL,used_at TEXT);
    CREATE TABLE IF NOT EXISTS tenant_policy_overlays(id TEXT PRIMARY KEY,data TEXT NOT NULL,tenant_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audience_oauth_clients(id TEXT PRIMARY KEY,data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audience_oauth_requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,data TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT);
    CREATE TABLE IF NOT EXISTS audience_oauth_codes(digest TEXT PRIMARY KEY,connection_id TEXT NOT NULL,data TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT);
    CREATE TABLE IF NOT EXISTS audience_oauth_tokens(digest TEXT PRIMARY KEY,kind TEXT NOT NULL,family TEXT NOT NULL,
        connection_id TEXT NOT NULL,data TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT,revoked_at TEXT);
    CREATE TABLE IF NOT EXISTS tenant_guardrail_profiles(tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tenant_profile_versions(tenant_id TEXT NOT NULL REFERENCES tenants(id),version INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(tenant_id,version));
    CREATE TABLE IF NOT EXISTS profile_agent_owners(agent_id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,tenant_id TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS tenant_profile_versions_no_update BEFORE UPDATE ON tenant_profile_versions
        BEGIN SELECT RAISE(ABORT, 'profile versions are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS tenant_profile_versions_no_delete BEFORE DELETE ON tenant_profile_versions
        BEGIN SELECT RAISE(ABORT, 'profile versions are immutable'); END;
    """)
    from .audit import utcnow
    for tid, name, customer in [(HOTEL, "Hotel Aurora chain", "1"), (HOSPITAL, "Kaari Hospital Kitchen", "2")]:
        conn.execute("INSERT OR IGNORE INTO tenants(id,name,created_at,disabled_at) VALUES(?,?,?,NULL)", (tid, name, utcnow().isoformat()))
        conn.execute("INSERT OR IGNORE INTO tenant_customers VALUES(?,?)", (customer, tid))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    # Audit is append-only except for this additive, non-hash-changing backfill.
    conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
    migrated = set()
    for table in tables:
        if table in SHARED or table.startswith(("sqlite_", "products_fts_")):
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if "tenant_id" not in columns:
            migrated.add(table)
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN tenant_id TEXT NOT NULL DEFAULT \'{HOTEL}\'')
            if "customer_id" in columns:
                conn.execute(f"UPDATE {table} SET tenant_id=? WHERE customer_id='2'", (HOSPITAL,))
            if "data" in columns:
                conn.execute(f"UPDATE {table} SET tenant_id=? WHERE json_valid(data) AND "
                             "(json_extract(data,'$.customer_id')='2' OR json_extract(data,'$.need.customer_id')='2' "
                             "OR json_extract(data,'$.agent_id')='hospital-kitchen-agent')", (HOSPITAL,))
            if "agent_id" in columns:
                conn.execute(f"UPDATE {table} SET tenant_id=? WHERE agent_id='hospital-kitchen-agent'", (HOSPITAL,))
            if "customer_ids" in columns:
                conn.execute(f"UPDATE {table} SET tenant_id=? WHERE EXISTS(SELECT 1 FROM json_each(customer_ids) WHERE value='2')", (HOSPITAL,))
    for child, foreign_key, parent, parent_key in [
        ("lookup_offers", "lookup_id", "lookups", "id"), ("basket_lookups", "id", "lookups", "id"),
        ("quotes", "id", "quotes", "id"), ("orders", "quote_id", "quotes", "id"),
        ("reservations", "order_id", "orders", "id"), ("evidence_snapshots", "order_id", "orders", "id"),
        ("audit", "order_id", "orders", "id"), ("outbox", "event_id", "events", "id"),
        ("webhook_deliveries", "subscription_id", "webhook_subscriptions", "id"),
        ("a2a_messages", "task_id", "a2a_tasks", "id"), ("a2a_push_configs", "task_id", "a2a_tasks", "id"),
    ]:
        if child in migrated:
            conn.execute(f"UPDATE {child} SET tenant_id=COALESCE((SELECT p.tenant_id FROM {parent} p WHERE p.{parent_key}={child}.{foreign_key}),tenant_id)")
    if "mandate_credentials" in migrated:
        from .mandates import decode
        for row in conn.execute("SELECT digest,jwt FROM mandate_credentials").fetchall():
            try:
                customer = decode(row[1])[1]["vc"]["credentialSubject"]["customer_id"]
                conn.execute("UPDATE mandate_credentials SET tenant_id=COALESCE((SELECT tenant_id FROM tenant_customers WHERE customer_id=?),tenant_id) WHERE digest=?", (customer, row[0]))
            except (KeyError, ValueError):
                continue
    if "object_mandates" in migrated:
        conn.execute("UPDATE object_mandates SET tenant_id=COALESCE((SELECT tenant_id FROM mandate_credentials m WHERE m.digest=object_mandates.digest),tenant_id)")
    if "mandate_revocations" in migrated:
        conn.execute("UPDATE mandate_revocations SET tenant_id='platform' WHERE customer_id=''")
    conn.execute("CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(connections)")}
    for name, decl in {"type": "TEXT NOT NULL DEFAULT 'mcp'", "audience": "TEXT NOT NULL DEFAULT 'buyer'",
                       "scopes": "TEXT NOT NULL DEFAULT '[\"*\"]'", "created_by": "TEXT", "expires_at": "TEXT"}.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE connections ADD COLUMN {name} {decl}")
    from .governance import migrate as migrate_governance
    migrate_governance(conn)


class TenantConnection(sqlite3.Connection):
    def install_scope(self, columns=None):
        self.create_function("caller_tenant", 0, tenant)
        self.create_function("caller_user", 0, lambda: principal().user_id if principal() else None)
        self.create_function("caller_member", 0, lambda: int(bool(principal() and principal().kind == "tenant_user" and principal().role == "member")))
        self.create_function("global_audit", 0, lambda: int(GLOBAL_AUDIT.get()))
        self.create_function("write_tenant", 0, lambda: self.inserting_tenant)
        self.business_columns = columns or {}
        self.scoped_views = set()

    def ensure_views(self, sql):
        if tenant() is None or not getattr(self, "business_columns", None):
            return
        referenced = set(re.findall(r'\b(?:FROM|JOIN)\s+["`\[]?(\w+)', sql, re.I))
        for name in referenced & self.business_columns.keys() - self.scoped_views:
            extra = " OR global_audit()=1" if name == "audit" else ""
            if name == "mandate_revocations":
                # Pre-customer revocations are global denials, never purchasing
                # grants. Scoping must not silently restore revoked authority.
                extra = " OR customer_id=''"
            owned_agents = "SELECT agent_id FROM main.tenant_agents WHERE owner_user_id=caller_user()"
            owned_orders = f"SELECT id FROM main.orders WHERE json_extract(data,'$.agent_id') IN ({owned_agents})"
            owned_quotes = f"SELECT id FROM main.quotes WHERE json_extract(data,'$.agent_id') IN ({owned_agents})"
            owned_lookups = f"SELECT id FROM main.lookups WHERE json_extract(data,'$.agent_id') IN ({owned_agents})"
            ownership = {
                "lookups": f"json_extract(data,'$.agent_id') IN ({owned_agents})",
                "quotes": f"json_extract(data,'$.agent_id') IN ({owned_agents})",
                "orders": f"json_extract(data,'$.agent_id') IN ({owned_agents})",
                "connections": f"agent_id IN ({owned_agents})",
                "lookup_offers": f"lookup_id IN ({owned_lookups})",
                "basket_lookups": f"id IN ({owned_lookups})",
                "reservations": f"order_id IN ({owned_orders})",
                "evidence_snapshots": f"order_id IN ({owned_orders})",
                "audit": f"global_audit()=1 OR actor IN (SELECT 'agent:'||agent_id FROM main.tenant_agents WHERE owner_user_id=caller_user()) OR json_extract(data,'$.agent_id') IN ({owned_agents}) OR order_id IN ({owned_orders}) OR quote_id IN ({owned_quotes})",
            }.get(name)
            member_filter = f" AND (caller_member()=0 OR ({ownership}))" if ownership else ""
            super().execute(f'CREATE TEMP VIEW IF NOT EXISTS "{name}" AS SELECT *,rowid AS rowid FROM main."{name}" '
                            f'WHERE (caller_tenant() IS NULL OR tenant_id=caller_tenant(){extra}){member_filter}')
            self.scoped_views.add(name)

    def execute(self, sql, parameters=(), /):
        tables = getattr(self, "business_columns", {})
        self.ensure_views(sql)
        match = re.match(r'\s*(INSERT(?: OR \w+)? INTO|REPLACE INTO|UPDATE|DELETE FROM)\s+["`\[]?(\w+)(?:["`\]]|\b)', sql, re.I)
        if match and match[2] in tables:
            table = match[2]
            op = match[1].upper()
            tail = sql[match.end():]
            if op.startswith(("INSERT", "REPLACE")):
                # Preserve legacy VALUES arity while supplying the tenant explicitly.
                cols = re.match(r"\s*\(([^)]+)\)", tail)
                if cols:
                    names = cols[1]
                    tail = tail[cols.end():]
                else:
                    names = ",".join(c for c in tables[table] if c != "tenant_id")
                if "tenant_id" not in [n.strip(' "`[]') for n in names.split(",")]:
                    self.inserting_tenant = tenant() or self.infer_tenant(names, parameters)
                    names += ",tenant_id"
                    values = re.search(r"\bVALUES\s*\(", tail, re.I)
                    if values:
                        start, depth, quoted, i = values.end(), 1, False, values.end()
                        while i < len(tail) and depth:
                            c = tail[i]
                            if c == "'":
                                if quoted and i+1 < len(tail) and tail[i+1] == "'":
                                    i += 2
                                    continue
                                quoted = not quoted
                            if not quoted:
                                depth += (c == "(") - (c == ")")
                            i += 1
                        tail = tail[:i-1] + ",write_tenant()" + tail[i-1:]
                    elif re.match(r"\s*SELECT", tail, re.I):
                        at = re.search(r"\bFROM\b", tail, re.I)
                        if at:
                            tail = tail[:at.start()] + ",write_tenant() " + tail[at.start():]
                sql = f'{match[1]} main.{table} ({names}){tail}'
                if "DO UPDATE SET" in sql.upper():
                    predicate = f"(caller_tenant() IS NULL OR {table}.tenant_id=caller_tenant())"
                    # UPSERT's trailing WHERE belongs to its update arm.
                    pos = sql.upper().index("DO UPDATE SET")
                    sql += (" AND " if " WHERE " in sql[pos:].upper() else " WHERE ") + predicate
            else:
                predicate = "(caller_tenant() IS NULL OR tenant_id=caller_tenant())"
                sql = f"{match[1]} main.{table}{tail}"
                pos = re.search(r"\bWHERE\b", sql, re.I)
                if pos:
                    sql = sql[:pos.end()] + " " + predicate + " AND (" + sql[pos.end():] + ")"
                else:
                    sql += " WHERE " + predicate
        result = super().execute(sql, parameters)
        if re.match(r"\s*ROLLBACK\b", sql, re.I):
            # SQLite rolls back TEMP VIEW DDL too. Never let the Python cache
            # make a subsequent read fall through to the unscoped main table.
            getattr(self, "scoped_views", set()).clear()
        return result

    def rollback(self):
        try:
            return super().rollback()
        finally:
            getattr(self, "scoped_views", set()).clear()

    def infer_tenant(self, names, parameters):
        """System/platform writes inherit the target's tenant, including worker events."""
        if TARGET_TENANT.get():
            return TARGET_TENANT.get()
        values = dict(zip([n.strip(' "`[]') for n in names.split(",")], parameters)) if not isinstance(parameters, dict) else parameters
        for field, table in (("order_id", "orders"), ("quote_id", "quotes"), ("lookup_id", "lookups"),
                             ("event_id", "events"), ("subscription_id", "webhook_subscriptions"),
                             ("recurring_id", "recurring_orders"), ("campaign_id", "proposal_campaigns")):
            if values.get(field):
                row = super().execute(f"SELECT tenant_id FROM main.{table} WHERE id=?", (values[field],)).fetchone()
                if row:
                    return row[0]
        obj = {}
        try:
            obj = json.loads(values.get("data") or values.get("payload") or values.get("result") or "{}")
            obj = obj if isinstance(obj, dict) else {}
            if isinstance(obj.get("value"), dict):
                obj = obj["value"]
        except (ValueError, TypeError):
            pass
        if obj.get("tenant_id"):
            return obj["tenant_id"]
        customer = values.get("customer_id") or obj.get("customer_id") or (obj.get("need") or {}).get("customer_id")
        if customer:
            row = super().execute("SELECT tenant_id FROM main.tenant_customers WHERE customer_id=?", (customer,)).fetchone()
            if row:
                return row[0]
        agent = values.get("agent_id") or obj.get("agent_id")
        if agent:
            row = super().execute("SELECT tenant_id FROM main.tenant_agents WHERE agent_id=?", (agent,)).fetchone()
            if row:
                return row[0]
        return HOTEL

    def executemany(self, sql, seq_of_parameters, /):
        # Keep the identical tenant write path for batch imports.
        match = re.match(r"\s*(?:INSERT(?: OR \w+)? INTO|REPLACE INTO|UPDATE|DELETE FROM)\s+(\w+)\b", sql, re.I)
        if not match or match[1] not in getattr(self, "business_columns", {}):
            return super().executemany(sql, seq_of_parameters)
        result = None
        for parameters in seq_of_parameters:
            result = self.execute(sql, parameters)
        return result
