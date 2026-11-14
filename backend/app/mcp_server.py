import argparse
import json
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from .db import DomainError
from .auth import Credentials, MCPBearerMiddleware
from .models import LookupResult, NeedSpec, Order, PlanRef, Product, Quote, QuoteLineRequest, QuoteRequest
from .service import Gateway

WARNINGS = (
    "Warnings: PARTIAL_STOCK means insufficient on-time sales units; DATE_SHIFT means delivery after the deadline; "
    "CUTOFF_PASSED means closingTime < upstream timeNow (meaning unconfirmed, requires review); SALES_BLOCK means not for sale; "
    "UNKNOWN_PRODUCT means no schedule lines; PRICE_ERROR means invalid/unavailable price; SIZE_UNKNOWN means no unit conversion; "
    "SIZE_MISMATCH means exact pack content differs from the live price ratio by more than 3% and requires review; "
    "OUTSIDE_PREFERENCE marks substitutes and lists differences from soft brand, price, pack-size or label preferences; these need approval by default. "
    "NO_FIT_SCORE means unavailable Decisions probability; AVAILABILITY_ERROR means stock is unverified. "
    "Hard constraints and sale blocks cannot be overridden by fit scores."
    " SIZE_UNCONFIRMED, UNIT_MISMATCH (zero usable amount), STOCK_MODEL_UNKNOWN and CUTOFF_UNKNOWN require review; "
    "NO_STOCK rejects zero-stock products with no delivery date. Missing/zero/negative prices or missing currency give PRICE_ERROR. "
    "VARIABLE_WEIGHT (final price follows delivered weight) and DEPOSIT (details supplied) are informational. "
    "All warnings fail closed to approval unless explicitly ignored by policy; protected evidence warnings cannot be ignored. "
    "Naive closingTime uses Europe/Helsinki. Missing reliability/CO2 is neutral, not proof."
)


class GatewayMCPServer(MCPServer):
    def streamable_http_app(self, **kwargs):
        if not self.keys.has_agent(self.agent_id):
            raise RuntimeError("AGENT_KEYS must contain the launched MCP agent; run scripts/gen_keys.py --write")
        app = super().streamable_http_app(**kwargs)
        app.add_middleware(MCPBearerMiddleware, keys=self.keys, agent_id=self.agent_id)
        return app

    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except (ToolError, DomainError, ValidationError) as error:
            # SDK argument validation happens before the function and otherwise adds prose around errors.
            cause = error
            while cause is not None:
                if isinstance(cause, DomainError):
                    raise ToolError(json.dumps({"code": cause.code, "message": cause.message})) from error
                if isinstance(cause, ValidationError):
                    message = "; ".join(f"{'.'.join(str(v) for v in e['loc'])}: {e['msg']}" for e in cause.errors())
                    code = "UNKNOWN_CODE" if any(e["type"] == "UNKNOWN_CODE" for e in cause.errors()) else "VALIDATION_ERROR"
                    raise ToolError(json.dumps({"code": code, "message": message})) from error
                cause = cause.__cause__
            raise


def create_server(gateway, agent_id="mcp-agent"):
    # The agent identity is fixed when the server is launched for that agent, not self-declared per call.
    server = GatewayMCPServer("Valio Aimo Trusted Trade Agents", instructions=(
        "Outside buyer procurement gateway: search locally, find live offers, create a fixed-price quote, then place an order. "
        "No negotiation. Prices are locked only until quote expiry; orders may require human approval in the REST inbox. Approval is REST-only: changed stock/policy evidence returns EVIDENCE_CHANGED and must be reviewed again at the new evidence_revision. "
        "Lookup, quote and order require a mandate; unregistered agents receive AGENT_NOT_REGISTERED. Registered agents have customer scope, per-order and cumulative rolling 24-hour limits. "
        "Lookups include single/split/partial plans, coverage and uncovered quantities, plus side-effect-free policy_preview for this server's identity. "
        "Previews include mandate and daily limits; stock and policy may change, so placement is authoritative. "
        "Every quote is bound to a stored request: quote a plan with plan_ref {lookup_id, plan_index}, or explicit lines with required need_ref. "
        "Current customer standing constraints are unioned with request constraints at lookup, quote, order and approval. "
        "customer_id is required; deliver_by must be today through +60 days (Europe/Helsinki); quantities are strict numbers >0 and <=1,000,000. "
        "Unknown claim/allergen/label codes return UNKNOWN_CODE with valid values. "
        "Quotes have Ed25519 detached JWS signatures: download public /.well-known/jwks.json, save the quote, "
        "then run scripts/verify_quote.py quote.json --jwks jwks.json --customer ID offline. "
        "RFC 8785 quote_payload uses decimal-string money/quantities, provenance and price_commitment=gateway-locked. "
        "Order receipts include commitment=gateway-simulated; get_order_ubl exports UBL 2.1 XML. " + WARNINGS))
    server.keys = gateway.settings.keys or Credentials()
    server.agent_id = agent_id

    @server.tool(description="Search the local catalog using AND terms and validated structured hard filters. Returns at most 10 products; no live API calls. Exclusions require an uncontradicted free-from entry or a catalog allergen declaration not listing the code; contains, may_contain, undeclared and missing data fail. X99 is only a declaration marker. Contradicted claims fail. Customer standing constraints apply in find_offers, which is the procurement path.")
    def search_products(query: str, required_claims: list[str] | None = None,
                        exclude_allergens: list[str] | None = None, limit: int = 10) -> list[Product]:
        products, _ = gateway.catalog.search(query, required_claims, exclude_allergens, limit)
        return products

    @server.tool(description="Find up to 10 ranked live priced offers for a structured need. Union current customer standing constraints with the request; effective_constraints and exclusion_counts name request/customer_policy sources and evidence reasons. Decisions screens the catalog pool, then one price and stock batch checks suitable products up to shortlist.live_max (default 25). Rank orderable offers first, then scored before unscored, then preference matches before substitutes. Price factor uses purchase_total (whole packs), overbuy {amount, unit} states excess, score_breakdown exposes weighted factors; unit_price is per sales unit. Reliability is absolute and CO2 uses the global catalog range; missing values use neutral catalog medians. Plans use ALL ranked priced offers before the returned slice, without extra calls; previews include mandate and daily limits. Placement rechecks stock/policy. Explicit search_terms use AND; otherwise intent uses AND, broadening only empty results deterministically to meaningful OR words and hyphenated parts. RETRIEVAL_BROADENED names tried words; POOL_TRUNCATED discloses unscreened matches. Returns retrieval {mode, terms_used}, matched_count, screened_count, priced_count, lookup_id, evidence, provenance, fit, stock and single/split/partial plans. no_match_reason explains empty hard-filter results and suggests search_terms after empty broadening. " + WARNINGS)
    def find_offers(need: NeedSpec) -> LookupResult:
        return gateway.find_offers(need, actor=f"agent:{agent_id}")

    @server.tool(description="Recheck live price and stock for selected lines or a stored fulfilment plan, then lock fixed net EUR prices until expires_at (at most 30 minutes). Supply exactly one of lines or plan_ref {lookup_id, plan_index}; plan_index is zero-based, need_ref defaults to the plan lookup. Explicit lines REQUIRE need_ref to a stored lookup, otherwise 422 NEED_REF_REQUIRED. Current customer standing constraints are unioned with the stored request at quotation, placement and approval. Partial-plan uncovered warnings persist into ordering. Registered agents may quote only for customers in their mandate (CUSTOMER_NOT_IN_MANDATE); unregistered agents receive AGENT_NOT_REGISTERED on lookup, quote and order. Quantities are strict numbers >0 and <=1,000,000 in sales units. Invalid price or missing currency prevents quoting. Customer and delivery date must match the stored need. Ed25519 detached JWS over RFC 8785 quote_payload, decimal-string quantities/money and provenance; kid names a public key at /.well-known/jwks.json. Offline: scripts/verify_quote.py quote.json --jwks jwks.json --customer ID. price_commitment=gateway-locked. No negotiation.")
    def create_quote(customer_id: str, deliver_by: str, lines: list[QuoteLineRequest] | None = None,
                     need_ref: str | None = None, plan_ref: PlanRef | None = None) -> Quote:
        try:
            return gateway.orders.create_quote(QuoteRequest(customer_id=customer_id, deliver_by=deliver_by,
                                                           lines=lines, need_ref=need_ref, plan_ref=plan_ref), agent_id=agent_id)
        except DomainError as error:
            raise ToolError(json.dumps({"code": error.code, "message": error.message})) from error

    @server.tool(description="Accept an unexpired fixed-price quote. Ordering requires a mandate: unregistered agents receive AGENT_NOT_REGISTERED with no order or reservation created. Lookup and quote also require a mandate. Registered agents must stay in customer scope, below the lower of global and mandate per-order limits, and within a cumulative rolling 24-hour autonomous spend limit; cancelled and human-approved orders are excluded. Rechecks stock and policy; returns CONFIRMED, PENDING_APPROVAL or REJECTED with every rule hit. Retry with the same idempotency_key to get the same order. EXPIRED_QUOTE requires a new quote. Human approval is REST-only in the inbox, optionally bound to evidence_revision. Changed evidence stays pending with 409 EVIDENCE_CHANGED; a human must review the refreshed revision before approving again. No upstream order endpoint exists.")
    def place_order(quote_id: str, idempotency_key: str) -> Order:
        try:
            return gateway.orders.place_order(quote_id, idempotency_key, agent_id=agent_id)
        except DomainError as error:
            raise ToolError(json.dumps({"code": error.code, "message": error.message})) from error

    @server.tool(description="Read current local order state, fixed-price lines, order and line warnings, evidence_revision and approval rule hits. Pending orders can expire; this does not submit another order or call upstream APIs.")
    def get_order_status(order_id: str) -> Order:
        return gateway.orders.get_order(order_id, agent_id)

    @server.tool(description="Export this agent's own order as UBL 2.1 XML, identical to GET /v1/orders/{id}/ubl. document=order (default) or response; response codes AP confirmed, RE rejected/cancelled/expired, CA pending. Includes sales-unit GTINs and UN/ECE unit codes. Party endpoint placeholders need data from Valio Aimo; gateway-simulated, no supplier transmission. Another agent's order is ORDER_NOT_FOUND.")
    def get_order_ubl(order_id: str, document: Literal["order", "response"] = "order") -> str:
        return gateway.orders.get_order_ubl(order_id, document, agent_id)

    @server.tool(description="Cancel only a CONFIRMED order placed by this server's agent identity (--agent-id), before every known line cutoff. Lines without a cutoff do not block. Releases local stock reservations, records the reason and actor, and leaves fixed prices unchanged. No upstream calls. ORDER_NOT_FOUND (404) hides another agent's order; INVALID_TRANSITION or CUTOFF_PASSED rejects ineligible orders.")
    def cancel_order(order_id: str, reason: str = "") -> Order:
        try:
            return gateway.orders.cancel(order_id, actor=f"agent:{agent_id}", reason=reason)
        except DomainError as error:
            # Expected refusals must retain their public code instead of becoming SDK crash errors.
            raise ToolError(json.dumps({"code": error.code, "message": error.message})) from error

    return server


def main():
    parser = argparse.ArgumentParser(description="Run the shared buyer-agent MCP gateway.")
    parser.add_argument("--http", action="store_true", help="Streamable HTTP at /mcp instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--agent-id", default="mcp-agent", help="Buyer agent identity recorded in the audit log")
    args = parser.parse_args()
    gateway = Gateway()
    try:
        server = create_server(gateway, args.agent_id)
        if args.http:
            server.run(transport="streamable-http", host=args.host, port=args.port)
        else:
            server.run(transport="stdio")
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
