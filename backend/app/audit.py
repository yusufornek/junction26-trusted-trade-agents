import json
import hashlib
from datetime import datetime, timezone

from .models import AuditEntry
from .canonical import canonical_json

from functools import wraps
from .tenancy import GLOBAL_AUDIT


def global_chain(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = GLOBAL_AUDIT.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            GLOBAL_AUDIT.reset(token)
    return wrapped


GENESIS = "0" * 64


def entry_payload(row):
    return {"seq": row["id"], **{k: row[k] for k in ("ts", "actor", "action", "summary", "order_id", "quote_id")},
            "data": json.loads(row["data"])}


def entry_hash(previous, payload):
    # prev_hash is the lowercase 64-character hex string, concatenated as UTF-8.
    return hashlib.sha256(previous.encode("ascii") + canonical_json(payload)).hexdigest()


def migrate(conn):
    """One atomic migration; never silently repair an already-chained database."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(audit)")}
    if {"prev_hash", "hash"} <= columns:
        return
    for name in ("prev_hash", "hash"):
        if name not in columns:
            conn.execute(f"ALTER TABLE audit ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    rows = conn.execute("SELECT * FROM audit ORDER BY id").fetchall()
    if not rows:
        return
    conn.execute("DROP TRIGGER audit_no_update")
    previous = GENESIS
    for row in rows:
        digest = entry_hash(previous, entry_payload(row))
        conn.execute("UPDATE audit SET prev_hash=?,hash=? WHERE id=?", (previous, digest, row["id"]))
        previous = digest
    conn.execute("""CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
                    BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END""")
    append(conn, "system", "AUDIT_CHAIN_START", "Existing audit entries chained by migration.",
           data={"migrated_entries": len(rows)})


def utcnow():
    from .runtime import CLOCK
    if clock := CLOCK.get():
        return clock()
    return datetime.now(timezone.utc)


def bounded_summary(value):
    """Bound failure storage without serializing or traversing an unbounded request."""
    remaining = 8000

    def visit(item, depth=0):
        nonlocal remaining
        if remaining <= 0 or depth > 8:
            return "<truncated>"
        if isinstance(item, str):
            limit = min(1024, remaining)
            text = item[:limit]
            remaining -= len(text)
            return text + ("<truncated>" if len(item) > limit else "")
        if isinstance(item, dict):
            result = {}
            for index, (key, val) in enumerate(item.items()):
                if index >= 20 or remaining <= 0:
                    result["truncated"] = True
                    break
                result[str(key)[:128]] = visit(val, depth + 1)
                remaining -= 32
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for val in item[:20]:
                if remaining <= 0:
                    break
                result.append(visit(val, depth + 1))
                remaining -= 16
            if len(result) < len(item):
                result.append("<truncated>")
            return result
        return item if item is None or isinstance(item, (bool, int, float)) else visit(str(item), depth + 1)

    result = visit(value)
    if len(json.dumps(result, default=str)) > 16000:
        return {"truncated": True, "summary": str(result)[:2000]}
    return result


@global_chain
def append(conn, actor, action, summary, *, order_id=None, quote_id=None, data=None):
    from .foundation import CORRELATION, TRACE, emit
    data = dict(data or {})
    if action.endswith(("_FAILED", "_REFUSED", "_RATE_LIMITED")) or action == "RATE_LIMITED":
        data = bounded_summary(data)
        summary = summary[:2000]
        actor = actor[:256]
    from .tenancy import principal, HOTEL, TARGET_TENANT
    p = principal()
    tenant_id = data.get("tenant_id") or (p.tenant_id if p and not p.operator else None) or TARGET_TENANT.get()
    if not tenant_id:
        for table, value in (("orders", order_id), ("quotes", quote_id)):
            if value:
                owned = conn.execute(f"SELECT tenant_id FROM main.{table} WHERE id=?", (value,)).fetchone()
                if owned:
                    tenant_id = owned[0]
                    break
    tenant_id = tenant_id or ("platform" if actor == "authentication" or p and p.operator else HOTEL)
    from .runtime import ENVIRONMENT
    if ENVIRONMENT.get() == "sandbox":
        data["environment"] = "sandbox"
    if CORRELATION.get():
        data["correlation_id"] = CORRELATION.get()
    if TRACE.get():
        data["trace_id"] = TRACE.get()
    if not conn.in_transaction:
        # Support direct SQLite callers too, without a deferred read/write race.
        # The caller still owns commit/rollback of the transaction we open.
        conn.execute("BEGIN IMMEDIATE")
    previous = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    previous = previous[0] if previous else GENESIS
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='audit'").fetchone()
    seq = (sequence[0] if sequence else 0) + 1
    ts = utcnow().isoformat()
    encoded = json.dumps(data or {}, default=str, allow_nan=False)
    payload = {"seq": seq, "ts": ts, "actor": actor, "action": action, "summary": summary,
               "order_id": order_id, "quote_id": quote_id, "data": json.loads(encoded)}
    digest = entry_hash(previous, payload)
    columns = "id,ts,actor,action,order_id,quote_id,summary,data,prev_hash,hash"
    values = (seq, ts, actor, action, order_id, quote_id, summary, encoded, previous, digest)
    if "tenant_id" in {r[1] for r in conn.execute("PRAGMA main.table_info(audit)")}:
        columns += ",tenant_id"
        values += (tenant_id,)
    conn.execute(f"INSERT INTO audit({columns}) VALUES({','.join('?' for _ in values)})", values)
    emit(conn, action, actor, ts, seq, data, order_id, quote_id)


@global_chain
def head(db, signing):
    from .runtime import ENVIRONMENT
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS entries, (SELECT hash FROM audit ORDER BY id DESC LIMIT 1) AS head_hash FROM audit").fetchone()
    payload = {"entries": row["entries"], "head_hash": row["head_hash"] or GENESIS, "at": utcnow().isoformat()}
    if ENVIRONMENT.get() == "sandbox":
        payload["environment"] = "sandbox"
    return {**payload, "jws": signing.sign(payload)}


@global_chain
def verify(db, checkpoint_entries=None, checkpoint_hash=None):
    # Serialize full scans per gateway so concurrent readers do not multiply
    # verification allocations. Keep just one row and one requested prefix hash.
    with db.audit_verify_lock, db.connect() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN")
        previous, first_break, entries, head_hash = GENESIS, None, 0, GENESIS
        checkpoint_digest = GENESIS if checkpoint_entries == 0 else None
        for index, row in enumerate(conn.execute("SELECT * FROM audit ORDER BY id"), 1):
            try:
                digest = entry_hash(previous, entry_payload(row))
                matches = row["id"] == index and row["prev_hash"] == previous and row["hash"] == digest
            except (ValueError, TypeError, UnicodeError):
                digest, matches = "", False
            if not matches and first_break is None:
                first_break = index
            previous = digest
            entries, head_hash = index, row["hash"]
            if index == checkpoint_entries:
                checkpoint_digest = digest
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='audit'").fetchone()
    # AUTOINCREMENT also reveals removal of the tail, unless the attacker rewrites
    # SQLite's sequence too; independently-held checkpoints cover that limit.
    if first_break is None and sequence and sequence[0] != entries:
        first_break = entries + 1
    result = {"valid": first_break is None, "entries": entries,
              "head_hash": head_hash, "first_break": first_break}
    if checkpoint_entries is not None:
        result["checkpoint_match"] = (0 <= checkpoint_entries <= entries and
            (first_break is None or first_break > checkpoint_entries) and
            checkpoint_digest == checkpoint_hash)
    return result


def read(db, order_id=None, quote_id=None):
    clauses, args = [], []
    for name, value in (("order_id", order_id), ("quote_id", quote_id)):
        if value is not None:
            clauses.append(f"{name}=?")
            args.append(value)
    sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id"
    with db.connect() as conn:
        return [AuditEntry(**{**{k: r[k] for k in AuditEntry.model_fields if k in r.keys()}, "data": json.loads(r["data"])}) for r in conn.execute(sql, args)]


def read_timeline(conn, order_id, quote_id, lookup_id):
    # OR joins the complete chain without duplicating events carrying both IDs.
    rows = conn.execute(
        """SELECT * FROM audit WHERE order_id=? OR quote_id=?
           OR (action='LOOKUP' AND json_extract(data, '$.lookup_id')=?)""",
        (order_id, quote_id, lookup_id),
    )
    events = [AuditEntry(**{**{k: row[k] for k in AuditEntry.model_fields if k in row.keys()}, "data": json.loads(row["data"])}) for row in rows]
    return sorted(events, key=lambda event: (datetime.fromisoformat(event.ts), event.id))
