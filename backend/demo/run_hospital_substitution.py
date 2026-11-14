"""Offline hospital stock-out → evidence-based substitute → buyer clinical approval."""
import json
from datetime import timedelta
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audit
from app.auth import Principal
from app.db import DomainError
from app.foundation import PRINCIPAL
from app.models import NeedSpec, QuoteRequest
from app.sandbox import scenario_gateway
from app.signing import verify_quote


def main():
    with scenario_gateway("hospital_stockout") as g:
        need = NeedSpec(customer_id="hospital", deliver_by=audit.utcnow().date() + timedelta(days=3),
            intent="Oat drink for hospital kitchen cooking", search_terms=["oat"], quantity={"value": 20, "unit": "L"},
            function={"product_type": "oat drink", "intended_use": "hospital kitchen cooking", "preparation": "UHT",
                      "acceptable_units": ["L"]}, substitute_for={"product_id": "usual-oat"})
        result = g.find_offers(need, "agent:sandbox-agent")
        best = result.offers[0]
        quote = g.orders.create_quote(QuoteRequest(customer_id=need.customer_id, deliver_by=need.deliver_by,
            need_ref=result.lookup_id, lines=[{"product_id": best.product_id, "quantity_sales_units": best.units_needed}]), "sandbox-agent")
        verify_quote(quote, g.signing.jwks())
        order = g.orders.place_order(quote.id, "hospital-demo", "sandbox-agent")
        assert order.status == "PENDING_APPROVAL" and order.clinical_review == "required"
        try:
            g.orders.approve(order.id, "Operator")
        except DomainError as error:
            assert error.code == "BUYER_APPROVER_REQUIRED"
        token = PRINCIPAL.set(Principal("hospital-approver", kind="buyer_user", role="buyer_approver",
                                       organisation_id="hospital", customer_ids=("hospital",)))
        try:
            approved = g.orders.approve(order.id, "Hospital buyer", evidence_revision=order.evidence_revision)
        finally:
            PRINCIPAL.reset(token)
        assert approved.status == "CONFIRMED" and approved.approval_side == "buyer"
        report = {"environment": "sandbox", "scenario": "hospital_stockout", "lookup": result.model_dump(mode="json"),
                  "quote": quote.model_dump(mode="json"), "order": approved.model_dump(mode="json")}
        with NamedTemporaryFile(mode="w", prefix="hospital-substitution-", suffix=".json", delete=False) as file:
            json.dump(report, file, indent=2)
        print(f"{file.name} — compliant substitute reviewed and approved by the hospital buyer.")


if __name__ == "__main__":
    main()
