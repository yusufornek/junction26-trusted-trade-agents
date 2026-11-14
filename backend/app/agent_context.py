"""Authenticated, advisory authority snapshot shared by REST and MCP."""
from .models import Model
from pydantic import Field


class AgentContext(Model):
    guardrail_profile: dict | None = Field(default=None, exclude_if=lambda v: v is None,
        description="This tenant's business profile and initial guardrail values with per-field preset source; absent on legacy demo tenants.")
    fixture_clock: str | None = Field(default=None, exclude_if=lambda v: v is None)
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
    from .guardrail_profiles import read_profile
    with service.db.connect() as conn:
        guardrail_profile = read_profile(conn, principal.tenant_id) if principal.tenant_id else None
    if principal.kind in {"tenant_user", "platform_admin"} and principal.user_id != "legacy-operator":
        from .accounts import ROLES
        customers = list(principal.customer_ids)
        if principal.operator:
            with service.db.connect() as conn:
                customers = [r[0] for r in conn.execute("SELECT customer_id FROM tenant_customers ORDER BY customer_id")]
        return AgentContext(guardrail_profile=guardrail_profile, fixture_clock=service.sandbox.now().isoformat() if service.sandbox else None,
            principal=principal.public(), customer_ids=customers,
            mandate={"source": "human_role", "role": principal.role, "registered": True},
            effective_limits={}, remaining_budget={}, environment=service.settings.environment,
            rate_limits=service.limiter.snapshot(principal.id), supported_operations=ROLES.get(principal.role, []),
            standing_constraints={})
    policy = service.policy.get()
    policy = service.governance.project(principal.id, None, policy)
    customers = list(principal.customer_ids)
    if mandate:
        # The credential's customer is verified before it is exposed as scope.
        from .mandates import decode, invalid
        vc = decode(mandate)[1].get("vc")
        if not isinstance(vc, dict) or not isinstance(vc.get("credentialSubject"), dict):
            invalid("format")
        customer = vc["credentialSubject"].get("customer_id")
        with service.db.connect() as conn:
            policy, record, _ = service.mandates.authorize(conn, mandate, principal.id, customer)
        from dataclasses import replace
        principal = replace(principal, customer_ids=(customer,))
        customers = [customer]
        summary = record.model_dump(mode="json")
    else:
        row = policy.agents.get(principal.id)
        summary = {"source": "operator_policy", "registered": row is not None,
                   "autonomy": (row.autonomy or policy.autonomy.default) if row else None}
    row = policy.agents.get(principal.id)
    if row:
        legacy_unbound = not principal.tenant_id and not principal.connection_id and not customers
        customers = [customer for customer in row.customer_ids if legacy_unbound or customer in customers]
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
    if row and customers:
        operations += ["find_offers", "create_quote", "place_order"]
    return AgentContext(guardrail_profile=guardrail_profile, fixture_clock=service.sandbox.now().isoformat() if service.sandbox else None,
        principal=principal.public(), customer_ids=customers, mandate=summary,
        effective_limits={"per_order_eur": per_order, "rolling_24h_eur": daily},
        remaining_budget={"currency": "EUR", "window": "rolling_24h", "spent": spent, "remaining": max(0, daily - spent)},
        environment=service.settings.environment, rate_limits=service.limiter.snapshot(principal.id),
        supported_operations=operations, standing_constraints={customer: {
            "required_claims": policy.customers[customer].required_claims,
            "exclude_allergens": policy.customers[customer].exclude_allergens}
            for customer in customers if customer in policy.customers})
