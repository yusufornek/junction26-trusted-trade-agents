import json
from datetime import datetime, timezone

from .models import AuditEntry


def utcnow():
    return datetime.now(timezone.utc)


def append(conn, actor, action, summary, *, order_id=None, quote_id=None, data=None):
    conn.execute(
        "INSERT INTO audit(ts,actor,action,order_id,quote_id,summary,data) VALUES(?,?,?,?,?,?,?)",
        (utcnow().isoformat(), actor, action, order_id, quote_id, summary,
         json.dumps(data or {}, default=str, allow_nan=False)),
    )


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
    return sorted(events, key=lambda event: (event.ts, event.id))
