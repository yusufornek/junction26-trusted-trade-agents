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


class Gateway:
    def __init__(self, settings=None):
        self.settings = settings or Settings.from_env()
        self.db = Database(self.settings.db_path)
        self.signing = SigningKeys(self.settings.signing_key_dir or self.settings.db_path.parent / "keys")
        self.catalog = Catalog(self.db)
        self.valio = ValioClient(self.settings.valio_api_base, self.settings.valio_api_key)
        self.decisions = OpenAIDecisions(self.settings.openai_api_key)
        self.policy = Policy(self.settings.policy_path, self.settings.db_path.parent / "policy.yaml", self.db)
        self.anchoring = Anchoring(self.db, self.settings.anchoring)
        self.db.on_commit = self.anchoring.trigger
        self.mandates = Mandates(self.db, self.policy, self.catalog)
        from .buyer import Buyer
        self.buyer = Buyer(self)
        self.lookup = Lookup(self.db, self.catalog, self.valio, self.decisions, self.policy, self.mandates)
        self.orders = Orders(self.db, self.catalog, self.valio, self.policy, self.signing, self.mandates)
        from .autonomy import Autonomy
        self.autonomy = Autonomy(self)
        from .foundation import Outbox
        from .limits import Limiter, SourceLimiter
        from .commands import install
        self.outbox = Outbox(self.db)
        self.limiter = Limiter(self.policy, self.db)
        self.source_limiter = SourceLimiter()
        from .connections import Connections
        self.connections = Connections(self)
        from .oauth import OAuth
        self.oauth = OAuth(self)
        install(self, self.connections, "create", "mutation")
        for name in ("create_quote", "place_order", "cancel", "approve", "reject"):
            install(self, self.orders, name, "mutation")
        for name in ("get_order", "get_quote", "list_orders", "timeline", "get_order_ubl"):
            install(self, self.orders, name, "read")
        install(self, self, "find_offers", "lookup")
        install(self, self.policy, "update", "mutation")
        install(self, self.mandates, "revoke", "mutation")
        install(self, self.autonomy, "change", "mutation")
        install(self, self.buyer, "control", "mutation")
        install(self, self.buyer, "revoke", "mutation")

        from .webhooks import Webhooks
        self.webhooks = Webhooks(self)
        for name in ("create", "update", "disable", "redeliver"):
            install(self, self.webhooks, name, "mutation")
        from .a2a import ProcurementA2A
        self.a2a = ProcurementA2A(self)

        def after_commit():
            self.anchoring.trigger()
            self.outbox.dispatch()
            self.webhooks.dispatch()
        self.db.on_commit = after_commit

    def find_offers(self, need, actor="agent:external", *, mandate=None):
        self.orders.expire()
        return self.lookup.find_offers(need, actor, mandate=mandate)

    def audit(self, order_id=None, quote_id=None):
        return audit.read(self.db, order_id, quote_id)

    def close(self):
        self.webhooks.client.close()
        self.anchoring.close()
        self.valio.close()
        self.decisions.close()
