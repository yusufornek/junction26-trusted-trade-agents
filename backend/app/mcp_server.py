import argparse
import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .db import DomainError
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
)


def create_server(gateway, agent_id="mcp-agent"):
    # The agent identity is fixed when the server is launched for that agent, not self-declared per call.
    server = MCPServer("Valio Aimo Trusted Trade Agents", instructions=(
        "Outside buyer procurement gateway: search locally, find live offers, create a fixed-price quote, then place an order. "
        "No negotiation. Prices are locked only until quote expiry; orders may require human approval in the REST inbox. "
        "Unregistered agents always need human approval. Registered agents have customer scope, per-order and cumulative rolling 24-hour limits. "
        "Lookups include single/split/partial plans, coverage and uncovered quantities, plus side-effect-free policy_preview for this server's identity. "
        "Previews include mandate and daily limits; stock and policy may change, so placement is authoritative. "
        "Quote a plan with plan_ref {lookup_id, plan_index}, or pass explicit lines and lookup_id as need_ref to preserve fit and constraints. " + WARNINGS))

    @server.tool(description="Search the local catalog using AND terms and structured hard filters. Returns at most 10 products; makes no live API calls. Excluding allergens checks catalog CONTAINS, not a safety certification.")
    def search_products(query: str, required_claims: list[str] | None = None,
                        exclude_allergens: list[str] | None = None, limit: int = 10) -> list[Product]:
        products, _ = gateway.catalog.search(query, required_claims, exclude_allergens, limit)
        return products

    @server.tool(description="Find up to 10 ranked live priced offers for a structured need. Hard filters run locally; Decisions gates suitable candidates for one price and one stock batch, up to shortlist.live_max (default 25), before ranking and truncating. Returns screened_count, priced_count, lookup_id, conversions, provenance, fit and stock. Also returns single/split/partial plans with total, coverage, uncovered and policy_preview for this server identity, including mandate and daily limits, without extra calls or commitments. Stock/policy can change before placement; the order result is authoritative. Soft preferences rank matching products above substitutes; no_match_reason explains empty hard-filter results. " + WARNINGS)
    def find_offers(need: NeedSpec) -> LookupResult:
        return gateway.find_offers(need, actor=f"agent:{agent_id}")

    @server.tool(description="Recheck live price and stock for selected lines or a stored fulfilment plan, then lock fixed net EUR prices until expires_at (at most 30 minutes). Supply exactly one of lines or plan_ref {lookup_id, plan_index}; plan_index is zero-based, need_ref defaults to the plan lookup. Partial-plan uncovered warnings persist into ordering. Registered agents may quote only for customers in their mandate (CUSTOMER_NOT_IN_MANDATE); unregistered agents may quote but always need human approval to order. Quantity is in sales units. Explicit lines may pass lookup_id as need_ref to carry fit, preferences and hard constraints. No negotiation or signing in MVP.")
    def create_quote(customer_id: str, deliver_by: str, lines: list[QuoteLineRequest] | None = None,
                     need_ref: str | None = None, plan_ref: PlanRef | None = None) -> Quote:
        try:
            return gateway.orders.create_quote(QuoteRequest(customer_id=customer_id, deliver_by=deliver_by,
                                                           lines=lines, need_ref=need_ref, plan_ref=plan_ref), agent_id=agent_id)
        except DomainError as error:
            raise ToolError(json.dumps({"code": error.code, "message": error.message})) from error

    @server.tool(description="Accept an unexpired fixed-price quote. Unregistered agents always need human approval (unregistered_agent). Registered agents must stay in customer scope, below the lower of global and mandate per-order limits, and within a cumulative rolling 24-hour autonomous spend limit; cancelled and human-approved orders are excluded. Rechecks stock and policy; returns CONFIRMED, PENDING_APPROVAL or REJECTED with every rule hit. Retry with the same idempotency_key to get the same order. EXPIRED_QUOTE requires a new quote. Human approval happens in the REST inbox; no upstream order endpoint exists.")
    def place_order(quote_id: str, idempotency_key: str) -> Order:
        try:
            return gateway.orders.place_order(quote_id, idempotency_key, agent_id=agent_id)
        except DomainError as error:
            raise ToolError(json.dumps({"code": error.code, "message": error.message})) from error

    @server.tool(description="Read current local order state, fixed-price lines, warnings and approval rule hits. Pending orders can expire; this does not submit another order or call upstream APIs.")
    def get_order_status(order_id: str) -> Order:
        return gateway.orders.get_order(order_id)

    @server.tool(description="Cancel only a CONFIRMED order placed by this server's agent identity (--agent-id), before every known line cutoff. Lines without a cutoff do not block. Releases local stock reservations, records the reason and actor, and leaves fixed prices unchanged. No upstream calls. NOT_ORDER_OWNER rejects another agent's order; INVALID_TRANSITION or CUTOFF_PASSED rejects ineligible orders.")
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
