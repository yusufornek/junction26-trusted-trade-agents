"""Authenticated, advisory authority snapshot shared by REST and MCP."""
from .models import Model


class AgentContext(Model):
    principal: dict
    customer_ids: list[str]
    mandate: dict
    effective_limits: dict
    remaining_budget: dict
    environment: str
    rate_limits: dict
    supported_operations: list[str]
    standing_constraints: dict


def agent_context(service, principal, mandate=None):
    policy = service.policy.get()
    customers = list(principal.customer_ids)
    if mandate:
        # The credential's customer is verified before it is exposed as scope.
        from .mandates import decode
        customer = decode(mandate)[1].get("vc", {}).get("credentialSubject", {}).get("customer_id")
        with service.db.connect() as conn:
            policy, record, _ = service.mandates.authorize(conn, mandate, principal.id, customer)
        customers = [customer]
        summary = record.model_dump(mode="json")
    else:
        row = policy.agents.get(principal.id)
        summary = {"source": "operator_policy", "registered": row is not None,
                   "autonomy": (row.autonomy or policy.autonomy.default) if row else None}
    row = policy.agents.get(principal.id)
    if row:
        customers = [customer for customer in row.customer_ids if not customers or customer in customers]
    with service.db.connect() as conn:
        spent = float(service.policy.autonomous_total(conn, principal.id))
    if row:
        per_order = min(policy.approval.max_auto_total_eur,
                        row.max_auto_total_eur if row.max_auto_total_eur is not None else float("inf"))
        daily = row.max_auto_daily_total_eur
        level = row.autonomy or policy.autonomy.default
        summary["autonomy"] = level
    else:
        per_order, daily = 0, 0
    operations = ["get_agent_context", "search_products", "get_order_status", "get_order_ubl", "get_order_evidence", "cancel_order"]
    if row:
        operations += ["find_offers", "create_quote", "place_order"]
    return AgentContext(principal=principal.public(), customer_ids=customers, mandate=summary,
        effective_limits={"per_order_eur": per_order, "rolling_24h_eur": daily},
        remaining_budget={"currency": "EUR", "window": "rolling_24h", "spent": spent, "remaining": max(0, daily - spent)},
        environment=service.settings.environment, rate_limits=service.limiter.snapshot(principal.id),
        supported_operations=operations, standing_constraints={customer: {
            "required_claims": policy.customers[customer].required_claims,
            "exclude_allergens": policy.customers[customer].exclude_allergens}
            for customer in customers if customer in policy.customers})
