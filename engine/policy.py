import logging
import datetime
from typing import Dict, Any, Optional
from api.database import SessionLocal
from api.models import Incident, IncidentStatus

logger = logging.getLogger("autofixops")

# ─── Action Allowlist ───
ALLOWED_ACTIONS = {
    "RESTART_POD",
    "INCREASE_MEMORY_LIMIT",
    "INCREASE_CPU_LIMIT",
    "ROLLBACK_DEPLOYMENT",
    "DISABLE_FEATURE_FLAG",   # allowlisted flags only, "true" -> "false" (engine/patch_safety.py)
    "ESCALATE",
}

# ─── Namespace Restrictions ───
# These namespaces require human approval regardless of confidence.
RESTRICTED_NAMESPACES = {"prod", "billing", "payments", "kube-system"}

# ─── Thresholds ───
CONFIDENCE_FLOOR = 0.80
ANTI_THRASH_WINDOW_MINUTES = 60
ANTI_THRASH_MAX_ACTIONS = 3


class PolicyVerdict:
    """Immutable result of a policy evaluation."""
    def __init__(self, decision: str, reason: str):
        # decision is one of: APPROVED, ESCALATED, REJECTED
        self.decision = decision
        self.reason = reason

    def __repr__(self):
        return f"PolicyVerdict(decision={self.decision}, reason={self.reason})"


class PolicyDecisionEngine:
    """
    Deterministic gate between AI diagnosis and remediation execution.
    Evaluates action safety using allowlists, confidence thresholds,
    anti-thrashing counters, and namespace restrictions.
    """

    def evaluate(
        self,
        incident: Incident,
        verdict: Dict[str, Any],
        namespace: str = "autofixops",
    ) -> PolicyVerdict:
        action = verdict.get("action", {})
        action_type = action.get("type", "UNKNOWN")
        confidence = verdict.get("confidence", 0.0)

        logger.info(
            f"[TRACE:{incident.id}] [POLICY] Evaluating action={action_type}, "
            f"confidence={confidence}, namespace={namespace}"
        )

        # ─── Gate 0: Circuit Breaker ───
        from engine.circuit_breaker import get_circuit_breaker
        breaker = get_circuit_breaker()
        if not breaker.should_allow_action():
            reason = (
                "Circuit breaker is OPEN — too many recent verification failures. "
                "All actions forced to ESCALATE until system recovers."
            )
            logger.warning(f"[TRACE:{incident.id}] [POLICY ESCALATED] {reason}")
            return PolicyVerdict("ESCALATED", reason)

        # ─── Gate 1: Action Allowlist ───
        if action_type not in ALLOWED_ACTIONS:
            reason = f"Action '{action_type}' is not in the permitted allowlist."
            logger.warning(f"[TRACE:{incident.id}] [POLICY REJECTED] {reason}")
            return PolicyVerdict("REJECTED", reason)

        # ─── Gate 2: Escalation pass-through ───
        if action_type == "ESCALATE":
            reason = "Action is ESCALATE — forwarding to human operator."
            logger.info(f"[TRACE:{incident.id}] [POLICY ESCALATED] {reason}")
            return PolicyVerdict("ESCALATED", reason)

        # ─── Gate 3: Confidence Floor ───
        if confidence < CONFIDENCE_FLOOR:
            reason = (
                f"Confidence {confidence:.2f} is below the {CONFIDENCE_FLOOR} threshold. "
                f"Escalating for human review."
            )
            logger.warning(f"[TRACE:{incident.id}] [POLICY ESCALATED] {reason}")
            return PolicyVerdict("ESCALATED", reason)

        # ─── Gate 4: Namespace Restrictions ───
        if namespace in RESTRICTED_NAMESPACES:
            reason = (
                f"Namespace '{namespace}' is restricted. "
                f"Automated remediation requires human approval."
            )
            logger.warning(f"[TRACE:{incident.id}] [POLICY ESCALATED] {reason}")
            return PolicyVerdict("ESCALATED", reason)

        # ─── Gate 5: Anti-Thrashing ───
        if self._is_thrashing(incident, action_type):
            reason = (
                f"Anti-thrash guard triggered: >{ANTI_THRASH_MAX_ACTIONS} "
                f"'{action_type}' actions on this target in the last "
                f"{ANTI_THRASH_WINDOW_MINUTES} minutes."
            )
            logger.warning(f"[TRACE:{incident.id}] [POLICY ESCALATED] {reason}")
            return PolicyVerdict("ESCALATED", reason)

        # ─── All Gates Passed ───
        reason = (
            f"All policy gates passed. Action '{action_type}' approved "
            f"with confidence {confidence:.2f} in namespace '{namespace}'."
        )
        logger.info(f"[TRACE:{incident.id}] [POLICY APPROVED] {reason}")
        return PolicyVerdict("APPROVED", reason)

    def _is_thrashing(self, incident: Incident, action_type: str) -> bool:
        """Checks if too many identical actions have been applied to the same target recently."""
        try:
            db = SessionLocal()
        except Exception:
            return False  # Can't check, assume safe

        try:
            from api.models import RemediationAudit
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(
                minutes=ANTI_THRASH_WINDOW_MINUTES
            )
            count = (
                db.query(RemediationAudit)
                .filter(
                    RemediationAudit.proposed_action == action_type,
                    RemediationAudit.policy_verdict == "APPROVED",
                    RemediationAudit.created_at >= cutoff,
                )
                .count()
            )
            return count >= ANTI_THRASH_MAX_ACTIONS
        except Exception:
            return False  # DB error — allow action, don't block on infra failure
        finally:
            db.close()
