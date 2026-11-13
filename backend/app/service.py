from . import audit
from .catalog import Catalog
from .config import Settings
from .db import Database
from .decisions import OpenAIDecisions
from .lookup import Lookup
from .orders import Orders
from .policy import Policy
from .valio import ValioClient


class Gateway:
    def __init__(self, settings=None):
        self.settings = settings or Settings.from_env()
        self.db = Database(self.settings.db_path)
        self.catalog = Catalog(self.db)
        self.valio = ValioClient(self.settings.valio_api_base, self.settings.valio_api_key)
        self.decisions = OpenAIDecisions(self.settings.openai_api_key)
        self.policy = Policy(self.settings.policy_path, self.settings.db_path.parent / "policy.yaml", self.db)
        self.lookup = Lookup(self.db, self.catalog, self.valio, self.decisions, self.policy)
        self.orders = Orders(self.db, self.catalog, self.valio, self.policy)

    def find_offers(self, need, actor="agent:external"):
        self.orders.expire()
        return self.lookup.find_offers(need, actor)

    def audit(self, order_id=None, quote_id=None):
        return audit.read(self.db, order_id, quote_id)

    def close(self):
        self.valio.close()
        self.decisions.close()
