from . import audit
from .catalog import Catalog
from .config import Settings
from .db import Database
from .decisions import OpenAIDecisions
from .lookup import Lookup
from .orders import Orders
from .policy import Policy
from .valio import ValioClient
from .signing import SigningKeys
from .mandates import Mandates
from .anchoring import Anchoring
from contextlib import contextmanager, nullcontext
from .runtime import scope
from .telemetry import Telemetry


class Gateway:
    def __init__(self, settings=None, *, telemetry=None):
        self.settings = settings or Settings.from_env()
        self.db = Database(self.settings.db_path)
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT value FROM workspace WHERE key='environment'").fetchone()
            if row and row[0] != self.settings.environment:
                raise ValueError("A database cannot be shared between live and sandbox environments")
            conn.execute("INSERT OR IGNORE INTO workspace VALUES('environment',?)", (self.settings.environment,))
        self.telemetry = telemetry or Telemetry(self.settings.environment)
        self.telemetry.db = self.db
        self.signing = SigningKeys(self.settings.signing_key_dir or self.settings.db_path.parent / "keys")
        self.catalog = Catalog(self.db)
        self.valio = ValioClient(self.settings.valio_api_base, self.settings.valio_api_key)
        self.decisions = OpenAIDecisions(self.settings.openai_api_key)
        policy_source = self.settings.policy_path
        from .config import BACKEND
        if self.settings.environment == "sandbox" and policy_source == BACKEND / "policy.yaml":
            import yaml
            policy_source = self.settings.db_path.parent / "sandbox-defaults.yaml"
            defaults = Policy._editable(Policy._load(self.settings.policy_path))
            defaults["agents"] = {identity: {"customer_ids": ["sandbox-customer"], "max_auto_daily_total_eur": 5000,
                "autonomy": "approval_required", "note": "Sandbox-only synthetic authority"}
                for identity in (self.settings.keys.agent_ids if self.settings.keys else ())}
            defaults["customers"] = {"sandbox-customer": {}}
            defaults["identities"], defaults["organisations"] = {}, {}
            policy_source.write_text(yaml.safe_dump(defaults))
        self.policy = Policy(policy_source, self.settings.db_path.parent / "policy.yaml", self.db)
        from .suppliers import ValioAdapter, NordfoodAdapter, SupplierRegistry
        self.suppliers = SupplierRegistry(ValioAdapter(self.valio, self.catalog),
                                          NordfoodAdapter(self.db, self.settings.nordfood_api_base))
        self.sandbox = None
        if self.settings.environment == "sandbox":
            from .sandbox_workspace import Workspace, FixtureSupplierAdapter, FixtureDecisions
            self.sandbox = Workspace(self)
            self.suppliers.get("nordfood").client.close()
            self.suppliers.adapters = {supplier: FixtureSupplierAdapter(self.sandbox, supplier) for supplier in ("valio", "nordfood")}
            self.catalog = self.suppliers.get("valio").catalog
            self.decisions.close()
            self.decisions = FixtureDecisions()
        self.policy.suppliers = self.suppliers
        self.anchoring = Anchoring(self.db, self.settings.anchoring)
        self.db.on_commit = self.anchoring.trigger
        self.mandates = Mandates(self.db, self.policy, self.catalog)
        from .buyer import Buyer
        self.buyer = Buyer(self)
        self.lookup = Lookup(self.db, self.catalog, self.valio, self.decisions, self.policy, self.mandates)
        self.orders = Orders(self.db, self.catalog, self.valio, self.policy, self.signing, self.mandates)
        from .baskets import Baskets
        self.baskets = Baskets(self)
        self.lookup.suppliers = self.orders.suppliers = self.suppliers

        from .autonomy import Autonomy
        self.autonomy = Autonomy(self)
        from .foundation import Outbox
        from .limits import Limiter, SourceLimiter
        from .commands import install
        self.outbox = Outbox(self.db)
        from .recurring import Recurring
        from .proposals import Proposals
        self.recurring = Recurring(self)
        self.proposals = Proposals(self)
        self.limiter = Limiter(self.policy, self.db)
        self.source_limiter = SourceLimiter()
        for name in ("create_quote", "place_order", "cancel", "approve", "reject"):
            install(self, self.orders, name, "mutation")
        for name in ("get_order", "get_quote", "list_orders", "timeline", "get_order_ubl", "get_order_receipt"):
            install(self, self.orders, name, "read")
        install(self, self, "find_offers", "lookup")
        install(self, self.baskets, "find_basket_offers", "lookup")
        install(self, self.lookup, "get", "read")

        install(self, self.policy, "update", "mutation")
        install(self, self.mandates, "revoke", "mutation")
        install(self, self.autonomy, "change", "mutation")
        install(self, self.buyer, "control", "mutation")
        install(self, self.buyer, "revoke", "mutation")

        from .webhooks import Webhooks
        self.webhooks = Webhooks(self)
        self.webhooks.telemetry = self.anchoring.telemetry = self.telemetry
        for name in ("create", "update", "disable", "redeliver"):
            install(self, self.webhooks, name, "mutation")
        from .a2a import ProcurementA2A
        self.a2a = ProcurementA2A(self)

        def after_commit():
            self.anchoring.trigger()
            self.outbox.dispatch()
            self.webhooks.dispatch()
        self.db.on_commit = after_commit
        for client in (self.valio.client, getattr(self.decisions, "client", None), self.webhooks.client,
                       getattr(self.suppliers.get("nordfood"), "client", None)):
            if client is not None:
                self.telemetry.instrument_client(client)

    @contextmanager
    def activate(self):
        self.telemetry.db = self.db
        with (self.sandbox.lock if self.sandbox else nullcontext()), scope(self.settings.environment, self.sandbox.now if self.sandbox else None), self.telemetry.activate():
            yield

    def find_offers(self, need, actor="agent:external", *, mandate=None):
        self.orders.expire()
        return self.lookup.find_offers(need, actor, mandate=mandate)

    def audit(self, order_id=None, quote_id=None):
        return audit.read(self.db, order_id, quote_id)

    def close(self):
        self.webhooks.client.close()
        self.recurring.close()
        self.anchoring.close()
        self.valio.close()
        self.decisions.close()
        if client := getattr(self.suppliers.get("nordfood"), "client", None):
            client.close()
        self.telemetry.close()
