import sqlite3
import threading
import fcntl
import logging
import os
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_files (path TEXT PRIMARY KEY, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS buyer_suspensions (agent_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_snapshots (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, bundle BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS agent_autonomy (
 agent_id TEXT PRIMARY KEY, level TEXT NOT NULL CHECK(level IN ('approval_required','autonomous_within_limits'))
);
CREATE TABLE IF NOT EXISTS commands (
 principal TEXT NOT NULL, key TEXT NOT NULL, command TEXT NOT NULL, digest TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(principal,key)
);
CREATE TABLE IF NOT EXISTS event_consumers (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trace_links (id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL, links TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, type TEXT NOT NULL, customer_id TEXT,
 environment TEXT NOT NULL, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
 aggregate_revision INTEGER NOT NULL, actor TEXT NOT NULL, ts TEXT NOT NULL,
 correlation_id TEXT NOT NULL, causation_id TEXT, payload TEXT NOT NULL, audit_id INTEGER NOT NULL,
 UNIQUE(aggregate_type,aggregate_id,aggregate_revision)
);
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT NOT NULL REFERENCES events(id), consumer TEXT NOT NULL REFERENCES event_consumers(name),
 status TEXT NOT NULL, attempts INTEGER NOT NULL, next_attempt_at TEXT NOT NULL,
 PRIMARY KEY(event_id,consumer)
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY, name_en TEXT, name_fi TEXT, category TEXT, sales_unit TEXT,
    base_unit TEXT, allowed_lot_size REAL, vendor_name TEXT, brand TEXT, country TEXT,
    temperature_condition, units TEXT, claims TEXT, allergens_contains TEXT,
    free_from TEXT, labels TEXT, base_volume_l REAL, base_net_weight_kg REAL,
    delivery_reliability REAL, co2_kg_per_kg REAL, data TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    id UNINDEXED, name_en, name_fi, key_ingredients, marketing,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS lookups (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lookup_offers (lookup_id TEXT PRIMARY KEY REFERENCES lookups(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, expires_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(id),
    idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    order_id TEXT NOT NULL REFERENCES orders(id), product_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0), PRIMARY KEY(order_id, product_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, order_id TEXT, quote_id TEXT, summary TEXT NOT NULL, data TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE INDEX IF NOT EXISTS audit_order ON audit(order_id);
CREATE INDEX IF NOT EXISTS audit_quote ON audit(quote_id);
CREATE INDEX IF NOT EXISTS audit_actor_ts ON audit(actor,ts);
CREATE INDEX IF NOT EXISTS orders_agent_created ON orders(json_extract(data,'$.agent_id'),json_extract(data,'$.created_at'));
CREATE INDEX IF NOT EXISTS quotes_agent ON quotes(json_extract(data,'$.agent_id'));
CREATE TABLE IF NOT EXISTS mandate_credentials (digest TEXT PRIMARY KEY, jwt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS object_mandates (
    kind TEXT NOT NULL, object_id TEXT NOT NULL, digest TEXT NOT NULL REFERENCES mandate_credentials(digest),
    PRIMARY KEY(kind, object_id)
);
CREATE TABLE IF NOT EXISTS mandate_revocations (
    issuer TEXT NOT NULL, mandate_id TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY(issuer, mandate_id)
);
CREATE TABLE IF NOT EXISTS audit_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entries INTEGER NOT NULL, head_hash TEXT NOT NULL,
    chain_id INTEGER NOT NULL, tx_hash TEXT, block_number INTEGER,
    status TEXT NOT NULL CHECK(status IN ('sent','confirmed','failed')), explorer_url TEXT, at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.on_commit = None
        self._connection = ContextVar("sqlite_connection", default=None)
        self._staged_files = ContextVar("sqlite_staged_files", default=None)
        self.audit_verify_lock = threading.Lock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            from .transport_storage import A2A_SCHEMA, WEBHOOK_SCHEMA
            conn.executescript(WEBHOOK_SCHEMA + A2A_SCHEMA)
        # Serialize additive migrations across gateway processes, including first start.
        with self.connect(write=True) as conn:
            # A subsequent build replaces catalog rows only.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
            for name, kind in {"allergen_status": "TEXT", "has_allergen_declaration": "INTEGER",
                               "size_suspect": "INTEGER", "volume_suspect": "INTEGER", "weight_suspect": "INTEGER",
                               "sales_volume_l": "REAL", "gtin": "TEXT", "unit_gtins": "TEXT",
                               "sales_net_weight_kg": "REAL", "variable_unit": "INTEGER", "deposits": "TEXT"}.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE products ADD COLUMN {name} {kind}")
            from .audit import migrate
            migrate(conn)
            anchor_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_anchors)")}
            if "raw_transaction" not in anchor_columns:
                conn.execute("ALTER TABLE audit_anchors ADD COLUMN raw_transaction TEXT")
            credential_columns = {row[1] for row in conn.execute("PRAGMA table_info(mandate_credentials)")}
            if "last_used" not in credential_columns:
                conn.execute("ALTER TABLE mandate_credentials ADD COLUMN last_used INTEGER NOT NULL DEFAULT 0")
                # Retain the old insertion ordering for historical credentials
                # until a successful use supplies a more recent sequence.
                conn.execute("UPDATE mandate_credentials SET last_used=rowid")
            revocation_columns = {row[1] for row in conn.execute("PRAGMA table_info(mandate_revocations)")}
            if "customer_id" not in revocation_columns:
                conn.execute("ALTER TABLE mandate_revocations RENAME TO legacy_mandate_revocations")
                conn.execute("CREATE TABLE mandate_revocations (customer_id TEXT NOT NULL, issuer TEXT NOT NULL, "
                             "mandate_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(customer_id,issuer,mandate_id))")
                # Old records lack signer/customer evidence. Preserve their prior
                # denial rather than silently restoring revoked authority.
                conn.execute("INSERT INTO mandate_revocations SELECT '',issuer,mandate_id,at FROM legacy_mandate_revocations")
                conn.execute("DROP TABLE legacy_mandate_revocations")

    def file_content(self, path):
        """Committed policy, or the caller's own staged transaction snapshot.

        No row means a historical file-only installation; the caller still loads it.
        """
        with self.connect() as conn:
            row = conn.execute("SELECT content FROM runtime_files WHERE path=?", (self._file_key(path),)).fetchone()
        return row[0] if row else None

    def _file_key(self, path):
        # Keep recovery local if a database is copied/restored into another directory.
        return str(Path(path).resolve().relative_to(self.path.parent.resolve()))

    @staticmethod
    def _prepare_file(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", dir=path.parent, prefix=".policy-", delete=False) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            return temporary
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def stage_file(self, path, content):
        """Enlist one durable file projection in the outermost writer transaction.

        Prepare/fsync before commit, publish only afterwards. SQLite retains the
        source of truth until publication, so a crash or a post-commit disk failure
        cannot lose authority/audit consistency. Startup/the next write repairs
        the projection. All policy-changing commands enter here via Policy._replace.
        """
        conn, staged = self._connection.get(), self._staged_files.get()
        if conn is None or staged is None:
            raise RuntimeError("File changes require a database writer transaction")
        path = Path(path).resolve()
        temporary = self._prepare_file(path, content)
        staged.append((path, content, temporary))
        conn.execute("INSERT INTO runtime_files VALUES(?,?) ON CONFLICT(path) DO UPDATE SET content=excluded.content",
                     (self._file_key(path), content))

    def _publish_files(self, conn, staged):
        for row in conn.execute("SELECT path,content FROM runtime_files").fetchall():
            path, content = (self.path.parent / row[0]).resolve(), row[1]
            temporary = None
            try:
                if not path.exists() or path.read_bytes() != content.encode():
                    temporary = next((temp for target, text, temp in reversed(staged)
                                      if target == path and text == content), None)
                    temporary = temporary or self._prepare_file(path, content)
                    os.replace(temporary, path)
                # Once published, preserve historical file-only loading and direct
                # runtime-file administration. A crash before cleanup is replay-safe.
                conn.execute("DELETE FROM runtime_files WHERE path=? AND content=?", (row[0], content))
                conn.commit()
            except OSError:
                # This transaction already committed. Do not report a failed change
                # or roll back authority in memory; retain the durable recovery row.
                logging.getLogger(__name__).exception("Committed policy projection awaits recovery: %s", path)
            except sqlite3.Error:
                conn.rollback()  # Cleanup only: authority/audit already committed.
                logging.getLogger(__name__).exception("Published policy recovery row awaits cleanup: %s", path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    @contextmanager
    def connect(self, write=False, *, notify=True):
        active = self._connection.get()
        if active is not None:
            # Nested business writes use savepoints: a refusal rolls its partial state
            # back while the enclosing command can retain the refusal's audit entry.
            from uuid import uuid4
            savepoint = "nested_" + uuid4().hex
            staged = self._staged_files.get()
            offset = len(staged) if staged is not None else 0
            if write:
                active.execute(f"SAVEPOINT {savepoint}")
            try:
                yield active
            except BaseException:
                if write:
                    active.execute(f"ROLLBACK TO {savepoint}")
                    for _, _, temporary in staged[offset:]:
                        temporary.unlink(missing_ok=True)
                    del staged[offset:]
                raise
            finally:
                if write:
                    active.execute(f"RELEASE {savepoint}")
            return
        # SQLite releases its writer lock at commit, before file publication.
        # Retain a process-shared lock through publication so the next writer
        # cannot base an update on the previous policy projection.
        writer_lock = self.path.with_suffix(self.path.suffix + ".writer.lock").open("a+b") if write else None
        conn = None
        try:
            if writer_lock is not None:
                fcntl.flock(writer_lock, fcntl.LOCK_EX)
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            if conn is not None:
                conn.close()
            if writer_lock is not None:
                writer_lock.close()
            raise
        committed = False
        token = self._connection.set(conn) if write else None
        staged = []
        file_token = self._staged_files.set(staged) if write else None
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
            committed = True
            if write:
                self._publish_files(conn, staged)
        except BaseException:
            conn.rollback()
            raise
        finally:
            if token is not None:
                self._connection.reset(token)
                self._staged_files.reset(file_token)
            for _, _, temporary in staged:
                temporary.unlink(missing_ok=True)
            conn.close()
            if writer_lock is not None:
                writer_lock.close()
        if committed and write and notify and self.on_commit is not None:
            try:
                self.on_commit()
            except Exception:
                pass  # An optional post-commit worker must never fail a committed business call.


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, data=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status
        self.data = data or {}
