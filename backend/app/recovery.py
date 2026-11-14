"""RFC 9457 recovery vocabulary, shared verbatim by REST and MCP."""
from .foundation import correlation_id

# Explicit inventory: adding a domain code requires choosing an authorised recovery.
RECOVERY = {}
for _code in ("APPROVER_REQUIRED", "IDEMPOTENCY_REQUIRED", "INVALID_CHECKPOINT", "INVALID_LIMIT",
              "INVALID_LOT_SIZE", "INVALID_PLAN_INDEX", "INVALID_SEARCH_MODE", "NEED_MISMATCH",
              "UNKNOWN_CODE", "UNSUPPORTED_CURRENCY", "VALIDATION_ERROR", "FUNCTION_UNIT_MISMATCH",
              "BASKET_LINE_MISMATCH"):
    RECOVERY[_code] = (False, "FIX_REQUEST")
for _code in ("AGENT_NOT_REGISTERED", "CUSTOMER_NOT_IN_MANDATE", "MANDATE_INVALID", "OPERATOR_REQUIRED",
              "UNAUTHENTICATED", "REVOCATION_INVALID", "BUYER_ADMIN_REQUIRED", "AGENT_SUSPENDED", "BUYER_APPROVER_REQUIRED"):
    RECOVERY[_code] = (False, "CONTACT_ADMIN")
for _code in ("CUTOFF_PASSED", "EXPIRED_ORDER", "HARD_CONSTRAINT_VIOLATION", "IDEMPOTENCY_CONFLICT",
              "INVALID_TRANSITION", "LOOKUP_NOT_FOUND", "NEED_NOT_FOUND", "ORDER_NOT_FOUND",
              "PRODUCT_NOT_FOUND", "QUOTE_ALREADY_ACCEPTED", "QUOTE_NOT_FOUND", "QUOTE_SIGNATURE_INVALID",
              "HTTP_ERROR", "INTERNAL_ERROR", "AGENT_NOT_FOUND"):
    RECOVERY[_code] = (False, "STOP")
for _code in ("VALIO_UNAVAILABLE", "AVAILABILITY_ERROR", "PRICE_ERROR", "UPSTREAM_BUSY", "UPSTREAM_RATE_LIMITED", "RATE_LIMITED"):
    RECOVERY[_code] = (True, "RETRY_SAME_COMMAND")
RECOVERY.update(NEED_REF_REQUIRED=(False, "LOOKUP_REQUIRED"), EXPIRED_QUOTE=(False, "REFRESH_QUOTE"),
                BASKET_NOT_FOUND=(False, "LOOKUP_REQUIRED"), EMPTY_BASKET=(False, "LOOKUP_REQUIRED"),
                BASKET_STOCK_UNAVAILABLE=(False, "REVIEW_UPDATED_EVIDENCE"),
                EVIDENCE_CHANGED=(False, "REVIEW_UPDATED_EVIDENCE"))


def problem(error, instance=None):
    retryable, action = RECOVERY[error.code]
    data = error.data if error.status != 401 else {}
    result = {"type": f"urn:trusted-trade:problem:{error.code.lower().replace('_', '-')}",
              "title": error.code.replace("_", " ").title(), "status": error.status,
              "detail": error.message, "instance": instance or "urn:correlation:" + correlation_id(),
              "code": error.code, "message": error.message, "recovery_version": 1,
              "retryable": retryable, "next_action": action,
              "retry_after_seconds": data.get("retry_after_seconds", 1 if retryable else None),
              "correlation_id": correlation_id(), "violations": data.get("violations", []), "data": data}
    # References are deliberately omitted unless a transport has verified read access.
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
