"""RFC 9457 recovery vocabulary, shared verbatim by REST and MCP."""
from .foundation import correlation_id

# Explicit inventory: adding a domain code requires choosing an authorised recovery.
RECOVERY = {}
RECOVERY.update(CONSENT_STALE=(False, "CONTACT_ADMIN"), STALE_EVENT=(False, "REVIEW_UPDATED_EVIDENCE"),
                EVENT_STORM=(False, "STOP"), ALREADY_CLAIMED=(False, "STOP"),
                NOTIFICATION_NOT_FOUND=(False, "STOP"), NOTIFICATION_TENANT_MISMATCH=(False, "STOP"),
                NOTIFICATION_NOT_ALLOWED=(False, "STOP"))
RECOVERY.update(AGENT_PAUSED=(False, "CONTACT_ADMIN"), FIX_NOT_FOUND=(False, "REFRESH_QUOTE"))
for _code in ("APPROVER_REQUIRED", "IDEMPOTENCY_REQUIRED", "INVALID_CHECKPOINT", "INVALID_LIMIT",
              "INVALID_LOT_SIZE", "INVALID_PLAN_INDEX", "INVALID_SEARCH_MODE", "NEED_MISMATCH",
              "UNKNOWN_CODE", "UNSUPPORTED_CURRENCY", "VALIDATION_ERROR", "FUNCTION_UNIT_MISMATCH",
              "BASKET_LINE_MISMATCH", "ON_HAND_UNIT_MISMATCH"):
    RECOVERY[_code] = (False, "FIX_REQUEST")
for _code in ("AGENT_NOT_REGISTERED", "CUSTOMER_NOT_IN_MANDATE", "MANDATE_INVALID", "OPERATOR_REQUIRED",
              "UNAUTHENTICATED", "REVOCATION_INVALID", "BUYER_ADMIN_REQUIRED", "AGENT_SUSPENDED", "BUYER_APPROVER_REQUIRED"):
    RECOVERY[_code] = (False, "CONTACT_ADMIN")
for _code in ("CUTOFF_PASSED", "EXPIRED_ORDER", "HARD_CONSTRAINT_VIOLATION", "IDEMPOTENCY_CONFLICT",
              "INVALID_TRANSITION", "LOOKUP_NOT_FOUND", "NEED_NOT_FOUND", "ORDER_NOT_FOUND",
              "PRODUCT_NOT_FOUND", "QUOTE_ALREADY_ACCEPTED", "QUOTE_NOT_FOUND", "QUOTE_SIGNATURE_INVALID",
              "HTTP_ERROR", "INTERNAL_ERROR", "AGENT_NOT_FOUND", "CONNECTION_NOT_FOUND"):
    RECOVERY[_code] = (False, "STOP")
for _code in ("VALIO_UNAVAILABLE", "AVAILABILITY_ERROR", "PRICE_ERROR", "UPSTREAM_BUSY", "UPSTREAM_RATE_LIMITED", "RATE_LIMITED"):
    RECOVERY[_code] = (True, "RETRY_SAME_COMMAND")
RECOVERY.update(NEED_REF_REQUIRED=(False, "LOOKUP_REQUIRED"), EXPIRED_QUOTE=(False, "REFRESH_QUOTE"),
                NOT_DECIDED=(False, "WAIT_FOR_APPROVAL"),
                BASKET_NOT_FOUND=(False, "LOOKUP_REQUIRED"), EMPTY_BASKET=(False, "LOOKUP_REQUIRED"),
                BASKET_STOCK_UNAVAILABLE=(False, "REVIEW_UPDATED_EVIDENCE"),
                EVIDENCE_CHANGED=(False, "REVIEW_UPDATED_EVIDENCE"))
RECOVERY.update(WEBHOOK_DESTINATION_UNSAFE=(False, "FIX_REQUEST"), WEBHOOK_CHALLENGE_FAILED=(False, "FIX_REQUEST"),
                WEBHOOK_NOT_FOUND=(False, "STOP"), TASK_NOT_FOUND=(False, "STOP"))
for _code in ("INVALID_OCCURRENCE", "APPROVED_TEMPLATE_REQUIRED", "INVALID_PRICE_EVIDENCE", "CUSTOMER_REQUIRED"):
    RECOVERY[_code] = (False, "FIX_REQUEST")
for _code in ("RECURRING_NOT_AUTHORIZED", "RECURRING_FORBIDDEN", "BUYER_REQUIRED", "CUSTOMER_FORBIDDEN",
              "SUPPLIER_REQUIRED", "BUYER_AGENT_REQUIRED"):
    RECOVERY[_code] = (False, "CONTACT_ADMIN")
for _code in ("RECURRING_NOT_FOUND", "PROPOSAL_NOT_FOUND", "CAMPAIGN_NOT_FOUND", "PROPOSAL_EXPIRED", "SUGGESTION_NOT_FOUND"):
    RECOVERY[_code] = (False, "STOP")
for _code in ("NO_RECURRING_OFFER", "PROPOSAL_NO_MATCH"):
    RECOVERY[_code] = (False, "LOOKUP_REQUIRED")
RECOVERY.update(UNKNOWN_SUPPLIER=(False, "FIX_REQUEST"), SUPPLIER_NOT_IN_MANDATE=(False, "CONTACT_ADMIN"),
                SUPPLIER_UNAVAILABLE=(True, "RETRY_SAME_COMMAND"), SANDBOX_ONLY=(False, "FIX_REQUEST"),
                UNKNOWN_SCENARIO=(False, "FIX_REQUEST"))
RECOVERY.update(OAUTH_REQUEST_NOT_FOUND=(False, "STOP"), OAUTH_REQUEST_EXPIRED=(False, "CONTACT_ADMIN"),
                OAUTH_REQUEST_DECIDED=(False, "STOP"), OAUTH_CONSENT_CSRF=(False, "CONTACT_ADMIN"))

for _code in ("CSRF_REQUIRED", "CONFIRMATION_INVALID", "ALREADY_ACTIVE", "EMAIL_EXISTS", "AGENT_EXISTS", "CUSTOMER_ASSIGNED"):
    RECOVERY[_code] = (False, "FIX_REQUEST")
for _code in ("WRONG_AUDIENCE", "HUMAN_REQUIRED", "INSUFFICIENT_SCOPE", "TENANT_ADMIN_REQUIRED", "LAST_TENANT_ADMIN",
              "POLICY_CEILING", "LINK_EXPIRED", "CONNECTION_REVOKED"):
    RECOVERY[_code] = (False, "CONTACT_ADMIN")
for _code in ("USER_NOT_FOUND", "TENANT_NOT_FOUND", "MANDATE_NOT_FOUND"):
    RECOVERY[_code] = (False, "STOP")
RECOVERY["LOGIN_THROTTLED"] = (True, "RETRY_SAME_COMMAND")
for _code in ("CONFIRMATION_DECLINED", "OAUTH_REQUEST_NOT_FOUND", "OAUTH_REQUEST_EXPIRED", "TOOL_NOT_FOUND"):
    RECOVERY[_code] = (False, "STOP")
for _code in ("APPROVAL_LIMIT", "FOUR_EYES", "CONSTRAINTS_REQUIRED", "GUARDRAIL_CHANGE_REQUIRED"):
    RECOVERY[_code] = (False, "CONTACT_ADMIN")
for _code in ("AGENT_PAUSED", "APPROVAL_ROUTING_REQUIRED", "SELF_APPROVAL_FORBIDDEN", "OWNER_UNAVAILABLE"):
    RECOVERY[_code] = (False, "CONTACT_ADMIN")
for _code in ("OUTSIDE_GUARDRAIL", "OPERATION_NOT_ALLOWED", "CATEGORY_NOT_ALLOWED", "OUTSIDE_ORDERING_HOURS", "OUTSIDE_DELIVERY_WINDOW"):
    RECOVERY[_code] = (False, "FIX_REQUEST")
for _code in ("POLICY_VERSION_NOT_FOUND", "POLICY_REQUEST_NOT_FOUND"):
    RECOVERY[_code] = (False, "STOP")
RECOVERY["POLICY_CHANGED"] = (False, "REVIEW_UPDATED_EVIDENCE")
RECOVERY.update(UNKNOWN_ARGUMENT=(False, "FIX_REQUEST"), ARGUMENT_REQUIRED=(False, "FIX_REQUEST"),
                CONTRACT_STALE=(False, "FIX_REQUEST"), REEVALUATION_REQUIRED=(False, "FIX_REQUEST"),
                DRIVING_TEST_REQUIRED=(False, "CONTACT_ADMIN"), CONNECTION_REQUIRED=(False, "CONTACT_ADMIN"),
                DRIVING_TEST_NOT_FOUND=(False, "STOP"))

# Keep the published version-1 vocabulary stable. Contract-aware failures use
# version 2, which adds an explicit refresh transition instead of changing v1.
RECOVERY_V2 = {**RECOVERY, "CONTRACT_STALE": (False, "REFRESH_CONTRACT"),
               "REEVALUATION_REQUIRED": (False, "REFRESH_CONTRACT")}


def problem(error, instance=None):
    import re
    if instance:
        instance = re.sub(r"(/v1/auth/(?:invites|password-resets)/)[^/]+", r"\1[redacted]", instance)
    from .runtime import ENVIRONMENT
    retryable, action = RECOVERY_V2[error.code]
    version = 2 if RECOVERY_V2[error.code] != RECOVERY[error.code] else 1
    data = error.data if error.status != 401 else {}
    result = {"type": f"urn:trusted-trade:problem:{error.code.lower().replace('_', '-')}",
              "title": error.code.replace("_", " ").title(), "status": error.status,
              "detail": error.message, "instance": instance or "urn:correlation:" + correlation_id(),
              "code": error.code, "message": error.message, "recovery_version": version,
              "retryable": retryable, "next_action": action,
              "retry_after_seconds": data.get("retry_after_seconds", 1 if retryable else None),
              "correlation_id": correlation_id(), "violations": data.get("violations", []), "data": data}
    # References are deliberately omitted unless a transport has verified read access.
    if ENVIRONMENT.get() == "sandbox":
        result["environment"] = "sandbox"
    return result


def validation_problem(error):
    from .db import DomainError
    violations = []
    for item in error.errors():
        message = item["msg"]
        values = message.split("valid values:", 1)[1].strip().split(", ") if "valid values:" in message else []
        violations.append({"field": ".".join(map(str, item["loc"])), "problem": message, "valid_values": values})
    code = "UNKNOWN_CODE" if any(e["type"] == "UNKNOWN_CODE" for e in error.errors()) else "VALIDATION_ERROR"
    return DomainError(code, "; ".join(v["field"] + ": " + v["problem"] for v in violations), 422,
                       {"violations": violations})
