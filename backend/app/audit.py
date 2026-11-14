import json
import hashlib
from datetime import datetime, timezone

from .models import AuditEntry
from .canonical import canonical_json

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
    return datetime.now(timezone.utc)


def append(conn, actor, action, summary, *, order_id=None, quote_id=None, data=None):
    from .foundation import CORRELATION, TRACE, emit
    data = dict(data or {})
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
    conn.execute(
        "INSERT INTO audit(id,ts,actor,action,order_id,quote_id,summary,data,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (seq, ts, actor, action, order_id, quote_id, summary, encoded, previous, digest),
    )
    emit(conn, action, actor, ts, seq, data, order_id, quote_id)


def head(db, signing):
    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS entries, (SELECT hash FROM audit ORDER BY id DESC LIMIT 1) AS head_hash FROM audit").fetchone()
    payload = {"entries": row["entries"], "head_hash": row["head_hash"] or GENESIS, "at": utcnow().isoformat()}
    return {**payload, "jws": signing.sign(payload)}


def verify(db, checkpoint_entries=None, checkpoint_hash=None):
    # Serialize full scans per gateway so concurrent readers do not multiply
    # verification allocations. Keep just one row and one requested prefix hash.
    with db.audit_verify_lock, db.connect() as conn:
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
        return [AuditEntry(**{**dict(r), "data": json.loads(r["data"])}) for r in conn.execute(sql, args)]


def read_timeline(conn, order_id, quote_id, lookup_id):
    # OR joins the complete chain without duplicating events carrying both IDs.
    rows = conn.execute(
        """SELECT * FROM audit WHERE order_id=? OR quote_id=?
           OR (action='LOOKUP' AND json_extract(data, '$.lookup_id')=?)""",
        (order_id, quote_id, lookup_id),
    )
    events = [AuditEntry(**{**dict(row), "data": json.loads(row["data"])}) for row in rows]
    return sorted(events, key=lambda event: (datetime.fromisoformat(event.ts), event.id))
