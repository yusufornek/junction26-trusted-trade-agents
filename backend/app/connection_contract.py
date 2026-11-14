"""Versioned commercial meaning, connection negotiation and pinned intents (63)."""
import asyncio
import copy
import json
import threading
from contextvars import ContextVar
from hashlib import sha256

from . import audit
from .canonical import canonical_json
from .db import DomainError
from .foundation import PRINCIPAL

ECHO = ContextVar("connection_contract_echo", default=None)
CONTRACT_META = "luotsi/contract"
DEMO_AGENTS = {"hotel-chain-agent", "web-console"}


def manager(gateway):
    if not hasattr(gateway, "connection_contract"):
        gateway.connection_contract = ConnectionContract(gateway)
    return gateway.connection_contract


def differences(before, after, prefix=""):
    """Semantic paths, including removed keys, rather than only changed examples."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            result += differences(before.get(key), after.get(key), prefix + "/" + str(key))
        return result
    return [] if before == after else [{"field": prefix, "before": before, "after": after}]


class ConnectionContract:
    def __init__(self, gateway):
        self.gateway, self.schemas = gateway, {}
        self.lock = threading.RLock()
        self.building = False
        self.sessions = []
        self.streams = []
        with gateway.db.connect(write=True, notify=False) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS connection_contract_versions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT UNIQUE NOT NULL,
                    digest TEXT NOT NULL, document TEXT NOT NULL, changes TEXT NOT NULL,
                    material INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS connection_contract_state (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS connection_contract_intents (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    version TEXT NOT NULL, reevaluated_version TEXT);
                CREATE TABLE IF NOT EXISTS connection_contract_runs (id TEXT PRIMARY KEY, connection_id TEXT NOT NULL,
                    data TEXT NOT NULL);
            """)

    def profile(self):
        products = {}
        with self.gateway.db.connect() as conn:
            rows = conn.execute("SELECT id,sales_unit,base_unit,units,base_volume_l,base_net_weight_kg FROM products ORDER BY id").fetchall()
            supplier_rows = conn.execute("SELECT supplier_id,supplier_sku,data FROM supplier_products ORDER BY supplier_id,supplier_sku").fetchall()
            override = conn.execute("SELECT value FROM workspace WHERE key='connection_contract_profile'").fetchone()
        for row in rows:
            units = json.loads(row["units"] or "{}")
            if isinstance(units, list):
                units = {v["unitId"]: v["sizeInBaseUnits"] for v in units}
            products[row["id"]] = {"sale_unit": row["sales_unit"], "base_unit": row["base_unit"],
                "base_units_per_sale_unit": units.get(row["sales_unit"], 1 if row["sales_unit"] == row["base_unit"] else None),
                "litres_per_base_unit": row["base_volume_l"], "kg_per_base_unit": row["base_net_weight_kg"]}
        for row in supplier_rows:
            product = json.loads(row["data"])
            sale, base = product.get("sales_unit"), product.get("base_unit")
            products[row["supplier_id"] + ":" + row["supplier_sku"]] = {"sale_unit": sale, "base_unit": base,
                "base_units_per_sale_unit": product.get("units", {}).get(sale, 1 if sale == base else None),
                "litres_per_base_unit": product.get("base_volume_l"), "kg_per_base_unit": product.get("base_net_weight_kg")}
        profile = {"profile_version": 1, "currency": "EUR", "time_zone": "Europe/Helsinki",
            "quantity": {"requested": ["L", "KG", "SALES_UNIT"], "quote": "quantity_sales_units is explicitly in sale units",
                         "case_contents": "base_units_per_sale_unit; multiply base content exactly once", "products": products},
            "price_basis": {"unit_price": "per sale unit", "vat": "net, excludes VAT", "total": "whole sale units times net unit price"},
            "delivery": {"deliver_by": "requested delivery date, not order cut-off", "closingTime": "order cut-off for availableDate",
                         "date_shift": "requires explicit review"},
            "rounding": {"sale_units": "ceiling to whole multiples of max(1, allowedLotSize), including KG",
                         "content_decimals": 6, "money": "decimal arithmetic; EUR cents"}}
        if override:
            profile.update(json.loads(override[0]))
        return profile

    def ensure_schemas(self):
        if self.building or set(self.schemas) == {"buyer", "tenant", "operator"}:
            return
        self.building = True
        try:
            from .mcp_server import create_server
            for audience in ("buyer", "tenant", "operator"):
                if audience not in self.schemas:
                    create_server(self.gateway, agent_id=None, audience=audience, full_surface=True)
        finally:
            self.building = False

    def current(self):
        from .recovery import RECOVERY_V2
        with self.lock:
            self.ensure_schemas()
            document = {"profile": self.profile(), "tools": copy.deepcopy(self.schemas), "recovery_version": 2,
                        "recovery": {k: list(v) for k, v in sorted(RECOVERY_V2.items())}}
            fingerprint = sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).digest()
            if getattr(self, "_fingerprint", None) == fingerprint:
                return dict(self._current)
            digest = sha256(canonical_json(document)).hexdigest()
            version = "contract_" + digest
            with self.gateway.db.connect(write=True, notify=False) as conn:
                last = conn.execute("SELECT * FROM connection_contract_versions ORDER BY seq DESC LIMIT 1").fetchone()
                if last and last["digest"] == digest:
                    self._fingerprint, self._current = fingerprint, {"version": last["version"], "digest": digest}
                    return dict(self._current)
                changes = differences(json.loads(last["document"]) if last else {}, document)
                # Titles/examples/docs are editorial. Field descriptions can
                # change meaning, so they are conservatively material too.
                material = bool(last) and any(not any(part in c["field"].split("/") for part in ("title", "examples", "documentation")) for c in changes)
                # A return to an earlier digest is still a new historical transition.
                if conn.execute("SELECT 1 FROM connection_contract_versions WHERE version=?", (version,)).fetchone():
                    version += "_" + str(last["seq"] + 1)
                conn.execute("INSERT INTO connection_contract_versions(version,digest,document,changes,material,created_at) VALUES(?,?,?,?,?,?)",
                    (version, digest, json.dumps(document), json.dumps(changes), int(material), audit.utcnow().isoformat()))
            if last:
                self.notify_changed()
            self._fingerprint, self._current = fingerprint, {"version": version, "digest": digest}
            return dict(self._current)

    def public(self):
        return {**self.current(), "recovery_version": 2, "profile_url": "/v1/connect/trading-profile", "profile_resource": "luotsi://trading-profile"}

    def changes_since(self, version):
        self.current()
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT seq FROM connection_contract_versions WHERE version=?", (version,)).fetchone()
            if not row:
                return [{"field": "/contract", "before": version, "after": self.current()["version"], "material": True}]
            rows = conn.execute("SELECT changes,material FROM connection_contract_versions WHERE seq>? ORDER BY seq", (row[0],)).fetchall()
        return [{**change, "material": bool(row["material"])} for row in rows for change in json.loads(row["changes"])]

    def state(self, cid):
        with self.gateway.db.connect() as conn:
            row = conn.execute("SELECT data FROM connection_contract_state WHERE id=?", (cid,)).fetchone()
        return json.loads(row[0]) if row else {"contract_version": None, "protocol_revision": None, "client": None,
            "capabilities": {}, "fallback_removed": []}

    def save_state(self, cid, state):
        with self.gateway.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT OR REPLACE INTO connection_contract_state VALUES(?,?)", (cid, json.dumps(state)))

    def acknowledge(self, cid, version, protocol=None, client=None, capabilities=None):
        with self.gateway.db.connect() as conn:
            found = conn.execute("SELECT 1 FROM connection_contract_versions WHERE version=?", (version,)).fetchone()
        if not found:
            raise DomainError("CONTRACT_STALE", "Refresh the published connection contract", 409, {"contract": self.public(), "changes": self.changes_since(version)})
        state = self.state(cid)
        # A different implementation must establish its capabilities anew.
        if client and state["client"] and state["client"] != client:
            state["capabilities"] = {}
            state["fallback_removed"] = []
        state.update(contract_version=version, protocol_revision=protocol or state["protocol_revision"], client=client or state["client"])
        for name, declaration in (capabilities or {}).items():
            state["capabilities"].setdefault(name, {"status": "declared", "declaration": declaration})
            if name in state["fallback_removed"]:
                state["fallback_removed"].remove(name)
        self.save_state(cid, state)
        return state

    def prove(self, cid, capability):
        state = self.state(cid)
        item = state["capabilities"].setdefault(capability, {"declaration": {}})
        item["status"] = "proven"
        self.save_state(cid, state)

    def fallback(self, cid, capability):
        state = self.state(cid)
        if capability not in state["fallback_removed"]:
            state["fallback_removed"].append(capability)
        self.save_state(cid, state)

    def enforce(self, p, version=None, signed_quote=None):
        if not p or not p.connection_id:
            return
        version = version or ECHO.get() or self.state(p.connection_id)["contract_version"]
        changes = self.changes_since(version)
        if any(c["material"] for c in changes):
            # An explicitly selected, verified signed promise is the only pinning
            # exception. The ordinary order engine still enforces its expiry.
            if signed_quote and self.signed_pin(signed_quote, p.id, version):
                return
            raise DomainError("CONTRACT_STALE", "Re-evaluate under the refreshed contract", 409,
                {"changes": changes, "contract": self.public()})

    def signed_pin(self, quote_id, owner, version):
        with self.gateway.db.connect() as conn:
            pin = conn.execute("SELECT * FROM connection_contract_intents WHERE id=? AND owner=?", (quote_id, owner)).fetchone()
            if not pin or pin["version"] != version:
                return False
            quote = self.gateway.orders._verified_quote(conn, quote_id, owner)
            return quote.expires_at > audit.utcnow()

    def pin(self, ref, owner, version=None):
        version = version or self.current()["version"]
        with self.gateway.db.connect(write=True, notify=False) as conn:
            conn.execute("INSERT OR IGNORE INTO connection_contract_intents VALUES(?,?,?,NULL)", (ref, owner, version))

    def intent(self, ref, owner):
        with self.gateway.db.connect() as conn:
            pin = conn.execute("SELECT * FROM connection_contract_intents WHERE id=? AND owner=?", (ref, owner)).fetchone()
        if not pin:
            return None
        current = self.current()["version"]
        changes = self.changes_since(pin["reevaluated_version"] or pin["version"])
        return {"started_version": pin["version"], "current_version": current,
                "status": "REEVALUATION_REQUIRED" if any(c["material"] for c in changes) else "CURRENT", "changes": changes}

    def before(self, route, arguments, p, category=None):
        if route == "connections.create":
            version = arguments["body"].contract_version or ECHO.get()
            if version:
                self.current()
                with self.gateway.db.connect() as conn:
                    exists = conn.execute("SELECT 1 FROM connection_contract_versions WHERE version=?", (version,)).fetchone()
                if not exists:
                    raise DomainError("CONTRACT_STALE", "Refresh the published contract before connecting", 409,
                        {"contract": self.public(), "changes": self.changes_since(version)})
        if (category and category != "read" and route != "connections.create") or route in {"orders.place_order", "orders.create_quote", "gateway.find_offers", "baskets.find_basket_offers"}:
            self.enforce(p, signed_quote=arguments.get("quote_id") if SIGNED_QUOTE.get() else None)
        if route == "orders.place_order":
            from .connection_contract_driving import driving
            driving(self.gateway).require_order(p, arguments.get("quote_id") if SIGNED_QUOTE.get() else None)
        elif route == "orders.create_quote":
            from .connection_contract_driving import driving
            driving(self.gateway).require_quote(p)
        if p and p.connection_id and route in {"orders.create_quote", "orders.place_order"}:
            request = arguments.get("request")
            ref = request.need_ref if request is not None else arguments.get("quote_id")
            state = self.intent(ref, p.id) if ref else None
            if state and state["status"] == "REEVALUATION_REQUIRED" and not (route == "orders.place_order" and SIGNED_QUOTE.get()):
                raise DomainError("REEVALUATION_REQUIRED", "Refresh the lookup and quote, or explicitly complete the original signed promise", 409,
                    {"intent_id": ref, "changes": state["changes"], "contract": self.public()})

        return self.current()["version"] if p and p.connection_id and route in {"gateway.find_offers", "baskets.find_basket_offers", "orders.create_quote"} else None

    def after(self, route, arguments, result, p, started_version=None):
        owner = getattr(result, "agent_id", None) or (p.id if p else None)
        ref = getattr(result, "lookup_id", None) or getattr(result, "basket_lookup_id", None) or (getattr(result, "id", None) if route in {"orders.create_quote", "orders.get_quote"} else None)
        if ref and owner and p and p.connection_id:
            self.pin(ref, owner, started_version)
            state = self.intent(ref, owner)
            if p and p.connection_id and hasattr(result, "links"):
                result.links["connection_contract"] = state
            elif p and p.connection_id and hasattr(result, "connection_contract"):
                result.connection_contract = state
        if route == "connections.create":
            body = arguments["body"]
            version = body.contract_version or ECHO.get() or (self.current()["version"] if body.agent_id in DEMO_AGENTS else None)
            if version:
                self.acknowledge(result.connection_id, version, client=body.client)
        return result

    def notify_changed(self):
        for loop, session in list(self.sessions):
            if loop.is_closed():
                continue
            async def send(session=session):
                try:
                    await session.send_tool_list_changed()
                except Exception:
                    self.sessions[:] = [(l, s) for l, s in self.sessions if s is not session]
            loop.call_soon_threadsafe(lambda send=send: asyncio.create_task(send()))
        for loop, server_ref, p in list(self.streams):
            server = server_ref()
            if not server or loop.is_closed():
                continue
            async def publish(server=server, p=p):
                from mcp.shared.subscriptions import ToolsListChanged
                token = PRINCIPAL.set(p)
                try:
                    await server._subscriptions.publish(ToolsListChanged())
                finally:
                    PRINCIPAL.reset(token)
            loop.call_soon_threadsafe(lambda publish=publish: asyncio.create_task(publish()))


SIGNED_QUOTE = ContextVar("connection_contract_signed_quote", default=False)
