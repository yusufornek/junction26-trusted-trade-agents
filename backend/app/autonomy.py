"""Human-granted autonomy and an audit-derived reliability record (no supplier calls)."""
import json
from datetime import datetime, timedelta

from . import audit
from .db import DomainError
from .mandates import decode
from .models import AgentTrackRecord, AutonomyChangeResult, PolicyUpdateRequest


def buyer_scope(policy, principal, agent_id, customer_ids):
    target = policy.identities.get(agent_id)
    if target and target.organisation_id:
        return bool(principal.organisation_id and principal.organisation_id == target.organisation_id
                    and set(customer_ids).issubset(principal.customer_ids))
    return bool(principal.organisation_id and customer_ids and set(customer_ids).issubset(principal.customer_ids))


def authorize_review(policy, principal, order):
    if principal is None or principal.operator:
        return
    if not buyer_scope(policy, principal, order.agent_id, [order.customer_id]):
        raise DomainError("ORDER_NOT_FOUND", "Order was not found", 404)
    if principal.kind != "buyer_user" or principal.role not in {"buyer_admin", "buyer_approver"}:
        raise DomainError("APPROVER_REQUIRED", "A buyer approver or operator credential is required", 403)


class Autonomy:
    def __init__(self, gateway):
        self.gateway = gateway
        self.db, self.policy = gateway.db, gateway.policy

    def _target(self, conn, agent_id, principal, *, change=False):
        policy = self.policy.get()
        row = policy.agents.get(agent_id)
        if row is None:
            # Only successfully used credentials enter this store. Retain historical records
            # even after expiration/revocation; promotion cannot override their signed level.
            for stored in conn.execute("SELECT jwt FROM mandate_credentials ORDER BY last_used DESC"):
                subject = decode(stored[0])[1]["vc"]["credentialSubject"]
                if subject["agent_id"] == agent_id:
                    if row is None:
                        from .models import AgentMandate
                        row = AgentMandate(customer_ids=[subject["customer_id"]], autonomy=subject.get("autonomy"),
                            max_auto_total_eur=subject["max_auto_total_eur"],
                            max_auto_daily_total_eur=subject["max_auto_daily_total_eur"])
                    elif subject["customer_id"] not in row.customer_ids:
                        row.customer_ids.append(subject["customer_id"])
        if row is None:
            raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
        if principal.operator:
            return policy, row
        same_buyer = principal.kind == "buyer_user" and buyer_scope(policy, principal, agent_id, row.customer_ids)
        if not same_buyer and (change or principal.id != agent_id):
            raise DomainError("AGENT_NOT_FOUND", "Agent was not found", 404)
        if change and principal.role != "buyer_admin":
            raise DomainError("BUYER_ADMIN_REQUIRED", "A buyer admin or operator credential is required", 403)
        return policy, row

    def track_record(self, agent_id, principal, window_days=30):
        self.gateway.orders.expire()
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            policy, _ = self._target(conn, agent_id, principal)
            return self._record(conn, agent_id, policy, window_days)

    def _record(self, conn, agent_id, policy, window_days=30):
        now = audit.utcnow()
        start = now - timedelta(days=window_days)
        orders = {r["id"]: json.loads(r["data"]) for r in conn.execute(
            "SELECT id,data FROM orders WHERE json_extract(data,'$.agent_id')=?", (agent_id,))}
        cohort = {oid for oid, obj in orders.items() if start <= datetime.fromisoformat(obj["created_at"]) <= now}
        actions = {oid: {} for oid in orders}
        hard, last_rejection, last_refusal = set(), None, None
        for r in conn.execute("SELECT * FROM audit ORDER BY id"):
            ts = datetime.fromisoformat(r["ts"])
            if ts > now:
                continue
            data = json.loads(r["data"])
            oid = r["order_id"]
            own = oid in orders or r["actor"] == f"agent:{agent_id}"
            if not own and r["quote_id"]:
                quote = conn.execute("SELECT data FROM quotes WHERE id=?", (r["quote_id"],)).fetchone()
                own = bool(quote and json.loads(quote[0]).get("agent_id") == agent_id)
            if not own:
                continue
            if oid in actions:
                actions[oid].setdefault(r["action"], []).append(ts)
            rejected = r["action"] == "REJECT" or (r["action"] == "PLACE_ORDER" and data.get("outcome") == "REJECTED")
            if rejected:
                last_rejection = max(last_rejection, ts) if last_rejection else ts
            hits = data.get("rule_hits", [])
            refusal = (r["action"] in {"PLACE_ORDER", "APPROVE_FAILED", "PLACE_ORDER_FAILED", "CREATE_QUOTE_FAILED"}
                       and any(h.get("rule") == "hard_constraint_violation" for h in hits))
            if refusal:
                last_refusal = max(last_refusal, ts) if last_refusal else ts
                if start <= ts:
                    hard.add(oid or f"audit:{r['id']}")
        def count(action):
            return sum(any(start <= t <= now for t in actions[oid].get(action, [])) for oid in cohort)
        unchanged = sum(bool(actions[oid].get("APPROVE")) and not actions[oid].get("EVIDENCE_CHANGED")
                        and orders[oid].get("evidence_revision", 1) == 1 for oid in cohort)
        rejected = sum(bool(actions[oid].get("REJECT")) or (orders[oid]["status"] == "REJECTED"
                       and not actions[oid].get("APPROVE")) for oid in cohort)
        times = [(min(actions[oid]["APPROVE"]) - datetime.fromisoformat(orders[oid]["created_at"])).total_seconds()
                 for oid in cohort if actions[oid].get("APPROVE")]
        total = len(cohort)
        share = rejected / total if total else 0.0
        promotion, reasons = policy.autonomy.promotion, []
        if unchanged < promotion.min_approved_unchanged:
            reasons.append(f"Approved unchanged {unchanged} is below minimum {promotion.min_approved_unchanged}.")
        if share > promotion.max_rejection_share:
            reasons.append(f"Rejection share {share:.1%} exceeds maximum {promotion.max_rejection_share:.1%}.")
        for label, last in (("rejection", last_rejection), ("hard-constraint refusal", last_refusal)):
            if last and now - last < timedelta(days=promotion.days_since_last_rejection_or_refusal):
                reasons.append(f"Last {label} was less than {promotion.days_since_last_rejection_or_refusal} days ago.")
        return AgentTrackRecord(agent_id=agent_id, window_days=window_days, window_start=start, window_end=now,
            orders_placed=total, approved_unchanged=unchanged, rejected=rejected, expired=count("ORDER_EXPIRED"),
            evidence_changes=count("EVIDENCE_CHANGED"), cancellations=count("CANCEL"), hard_constraint_refusals=len(hard),
            average_approval_time_seconds=sum(times) / len(times) if times else None,
            approved_unchanged_share=unchanged / total if total else 0.0, rejection_share=share,
            last_rejection_at=last_rejection, last_hard_constraint_refusal_at=last_refusal,
            autonomy_recommendation={"eligible": not reasons, "reasons": reasons})

    def change(self, agent_id, request, principal):
        self.gateway.orders.expire()
        # Policy.update owns the writer transaction and its file rollback. Keep target,
        # permission and snapshot reads under that same lock via its request factory.
        def prepare():
            with self.db.connect() as conn:
                policy, row = self._target(conn, agent_id, principal, change=True)
                record = self._record(conn, agent_id, policy)
                grant = conn.execute("SELECT level FROM agent_autonomy WHERE agent_id=?", (agent_id,)).fetchone()
            before = (grant[0] if agent_id not in policy.agents and grant else row.autonomy) or policy.autonomy.default
            row = row.model_copy(update={"autonomy": request.level})
            body = self.policy._editable(policy)
            body["agents"][agent_id] = row.model_dump(mode="json")
            result = AutonomyChangeResult(agent_id=agent_id, level=request.level, reason=request.reason, track_record=record)
            data = {
                "agent_id": agent_id, "before": before, "level": request.level, "reason": request.reason,
                "track_record": record.model_dump(mode="json"), "customer_ids": row.customer_ids,
                "customer_id": row.customer_ids[0] if len(row.customer_ids) == 1 else None}
            if agent_id not in policy.agents:
                with self.db.connect(write=True) as conn:
                    conn.execute("INSERT INTO agent_autonomy VALUES(?,?) ON CONFLICT(agent_id) DO UPDATE SET level=excluded.level",
                                 (agent_id, request.level))
                return None, data, result
            return PolicyUpdateRequest(policy=body, updated_by=principal.id, comment=request.reason), data, result
        return self.policy.change_autonomy(prepare, principal)
