"""
Confidence-based routing for AI patch generation (Stage 2).
===========================================================
Pure policy, no I/O. The route decides only whether a human must approve
*generating* a patch proposal. It never relaxes a safety check and never
authorises applying, committing or deploying anything.

  confidence >= ROUTING_THRESHOLD  -> AUTOMATIC_AI             (generate without asking)
  confidence <  ROUTING_THRESHOLD  -> HUMAN_APPROVAL_REQUIRED  (generate only after APPROVED)

The confidence always comes from the stored diagnosis report, never from a client.
"""

import math

from engine.policy import CONFIDENCE_FLOOR

ROUTING_THRESHOLD = CONFIDENCE_FLOOR   # 0.80, the same floor the policy engine uses

AUTOMATIC_AI = "AUTOMATIC_AI"
HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
HUMAN_APPROVED = "HUMAN_APPROVED"
HUMAN_REJECTED = "HUMAN_REJECTED"

# Decision values stored on a routing row.
AUTO, PENDING, APPROVED, REJECTED = "AUTO", "PENDING", "APPROVED", "REJECTED"


class RoutingError(ValueError):
    """The report cannot be routed. `code` is a stable reason code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def valid_confidence(value) -> float:
    """value as a float if it is a real number in [0, 1]; raises RoutingError otherwise.

    bool, str, None, NaN and +/-infinity are rejected (no coercion).
    """
    if value is None:
        raise RoutingError("CONFIDENCE_MISSING", "the diagnosis report has no confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingError("CONFIDENCE_INVALID", f"confidence must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise RoutingError("CONFIDENCE_INVALID", "confidence must be finite")
    if not 0.0 <= value <= 1.0:
        raise RoutingError("CONFIDENCE_INVALID", "confidence must be between 0 and 1")
    return float(value)


def threshold_conflict(value, source: str) -> "str | None":
    """Why a configured confidence threshold conflicts with ROUTING_THRESHOLD, or None if it matches.

    There is exactly one threshold. A different configured value is refused (CONFIG_INVALID) rather
    than letting an incident route automatically and then be refused by a stricter generation gate.
    None means "not configured" and is fine.
    """
    if value is None:
        return None
    # Exact equality: any tolerance would let a value just above 0.80 recreate the dead end.
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or float(value) != ROUTING_THRESHOLD):
        return f"{source} is {value!r}; the only supported confidence threshold is {ROUTING_THRESHOLD:.2f}"
    return None


def route_for_confidence(value) -> str:
    return AUTOMATIC_AI if valid_confidence(value) >= ROUTING_THRESHOLD else HUMAN_APPROVAL_REQUIRED


def routing_mode(route: str, decision: str) -> str:
    """Display mode: AUTOMATIC_AI, HUMAN_APPROVAL_REQUIRED (pending), HUMAN_APPROVED or HUMAN_REJECTED."""
    if route == AUTOMATIC_AI:
        return AUTOMATIC_AI
    return {APPROVED: HUMAN_APPROVED, REJECTED: HUMAN_REJECTED}.get(decision, HUMAN_APPROVAL_REQUIRED)
