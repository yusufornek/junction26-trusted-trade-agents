import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
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
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        # Serialize additive migrations across gateway processes, including first start.
        with self.connect(write=True) as conn:
            # A subsequent build replaces catalog rows only.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
            for name, kind in {"allergen_status": "TEXT", "has_allergen_declaration": "INTEGER",
                               "size_suspect": "INTEGER", "sales_volume_l": "REAL", "gtin": "TEXT", "unit_gtins": "TEXT",
                               "sales_net_weight_kg": "REAL", "variable_unit": "INTEGER", "deposits": "TEXT"}.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE products ADD COLUMN {name} {kind}")
            from .audit import migrate
            migrate(conn)

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        committed = False
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
            committed = True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        if committed and write and self.on_commit is not None:
            try:
                self.on_commit()
            except Exception:
                pass  # An optional post-commit worker must never fail a committed business call.


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, data=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status
        self.data = data or {}
