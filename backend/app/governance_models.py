"""Spec 44 policy vocabulary shared by REST, MCP and tenant A2A."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeliveryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_days: int | None = Field(default=None, ge=0)
    max_days: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.max_days is not None and self.min_days is not None and self.max_days < self.min_days:
            raise ValueError("max_days must be at least min_days")
        return self


class ApprovalRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approver_user_ids: list[str] | None = None
    tenant_admin_above_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class AgentPolicy(BaseModel):
    """Null limits/allowlists mean unrestricted; empty allowlists permit nothing.

    Ordering hours are UTC hours (0..23); the delivery window is relative to the
    operation's UTC date. No local-time ambiguity is hidden in stored versions.
    """
    model_config = ConfigDict(extra="forbid")
    allowed_customers: list[str] | None = None
    allowed_operations: list[Literal["lookup", "quote", "order"]] | None = None
    per_order_ceiling_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    daily_budget_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    monthly_budget_eur: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    allowed_categories: list[str] | None = None
    blocked_categories: list[str] = Field(default_factory=list)
    required_claims: list[str] = Field(default_factory=list)
    excluded_allergens: list[str] = Field(default_factory=list)
    substitutes_allowed: bool = True
    ordering_hours: list[int] | None = Field(default=None, description="Allowed UTC hours, 0 through 23; null means all hours.")
    delivery_day_window: DeliveryWindow = Field(default_factory=DeliveryWindow)
    autonomy_level: Literal["approval_required", "autonomous_within_limits"] = "autonomous_within_limits"
    approval_routing: ApprovalRouting = Field(default_factory=ApprovalRouting)

    @model_validator(mode="after")
    def valid_values(self):
        if self.ordering_hours is not None and any(type(h) is not int or not 0 <= h <= 23 for h in self.ordering_hours):
            raise ValueError("ordering_hours must contain UTC hours 0..23")
        for field in ("allowed_customers", "allowed_categories", "blocked_categories", "required_claims", "excluded_allergens"):
            if any(not x.strip() for x in (getattr(self, field) or [])):
                raise ValueError(field + " cannot contain blank identifiers")
        return self


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: AgentPolicy = Field(description="Partial policy: omitted fields retain their current values; nested objects merge with the current layer.")
    reason: str = Field(min_length=1, max_length=2000)
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)


class PolicySimulation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: AgentPolicy


class GovernanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)
    confirmation_token: str | None = Field(default=None, repr=False, exclude=True)


class Transfer(GovernanceDecision):
    owner_user_id: str


class PolicyField(BaseModel):
    value: object
    source_layer: Literal["platform", "tenant", "agent"]
    version_id: str
    author: str
    sources: list[dict] = Field(description="All contributing layers, including intersections and unions.")


class EffectivePolicy(BaseModel):
    agent_id: str
    tenant_id: str
    version_id: str = Field(description="Immutable effective snapshot identifier recorded on every new lookup, quote and order.")
    fields: dict[str, PolicyField]
