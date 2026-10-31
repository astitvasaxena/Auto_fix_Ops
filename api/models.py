from sqlalchemy import Column, String, DateTime, Enum, JSON, ForeignKey, Float, Text, Integer, Boolean, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
import datetime
import enum
from .database import Base


# ─── Valid State Transitions ───
# Each key maps to the set of states it is allowed to transition TO.
VALID_TRANSITIONS = {
    "INGESTED":         {"CONTEXT_BUILT", "FAILED"},
    "CONTEXT_BUILT":    {"DIAGNOSED", "FAILED"},
    "DIAGNOSED":        {"POLICY_APPROVED", "ESCALATED", "FAILED"},
    "POLICY_APPROVED":  {"REMEDIATING", "FAILED"},
    # ESCALATED: an approved remediation failed or was rolled back; a human takes over.
    "REMEDIATING":      {"PENDING_PR_MERGE", "VERIFIED", "ESCALATED", "FAILED"},  # VERIFIED: PR step skipped
    "PENDING_PR_MERGE": {"VERIFIED", "ESCALATED", "FAILED"},
    "VERIFIED":         {"RESOLVED", "FAILED"},
    "ESCALATED":        {"POLICY_APPROVED", "RESOLVED"},  # Human can approve or resolve
    "FAILED":           set(),          # Terminal state
    "RESOLVED":         set(),          # Terminal state
}


class IncidentStatus(str, enum.Enum):
    INGESTED = "INGESTED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    DIAGNOSED = "DIAGNOSED"
    POLICY_APPROVED = "POLICY_APPROVED"
    ESCALATED = "ESCALATED"
    REMEDIATING = "REMEDIATING"
    PENDING_PR_MERGE = "PENDING_PR_MERGE"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    RESOLVED = "RESOLVED"
    # Inert legacy values from the removed Human Approval #1 feature. They are
    # kept ONLY so rows already stored with them still load. No transition
    # leads to or from them (absent from VALID_TRANSITIONS → terminal).
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"


class ExecutionStatus(str, enum.Enum):
    PR_CREATED = "PR_CREATED"
    PR_MERGED = "PR_MERGED"
    PR_REJECTED = "PR_REJECTED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class SystemMode(str, enum.Enum):
    """Global kill switch modes."""
    ACTIVE = "ACTIVE"
    SHADOW = "SHADOW"
    DISABLED = "DISABLED"


class FailureRootCause(str, enum.Enum):
    """Structured failure root cause classification."""
    INFRA = "INFRA"
    LOGIC = "LOGIC"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("project_config.id"), nullable=True, index=True)
    alert_fingerprint = Column(String, index=True, nullable=False)
    status = Column(Enum(IncidentStatus, name="incident_status"), default=IncidentStatus.INGESTED, nullable=False)
    severity = Column(String, nullable=False)
    alert_name = Column(String, nullable=False)
    raw_payload_cache = Column(JSON, nullable=True)

    # Diagnosis fields (populated after engine runs)
    diagnosis_classification = Column(String, nullable=True)
    diagnosis_confidence = Column(Float, nullable=True)
    diagnosis_reasoning = Column(Text, nullable=True)
    diagnosed_by = Column(String, nullable=True)  # "RULE_ENGINE" or "AI_ENGINE"

    # Structured AI Incident Report (analysis stage; recommendation only)
    ai_report = Column(JSON, nullable=True)

    # Target resolution (populated by target resolver before remediation)
    resolved_target = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    status_timeline = Column(JSON, default=list)

    # Relationships
    remediation_audits = relationship("RemediationAudit", back_populates="incident")
    project = relationship("ProjectConfig", back_populates="incidents")

    def transition_to(self, new_status: IncidentStatus):
        """Enforces valid state transitions. Raises ValueError on illegal jumps."""
        allowed = VALID_TRANSITIONS.get(self.status.value, set())
        if new_status.value not in allowed:
            raise ValueError(
                f"Illegal state transition: {self.status.value} → {new_status.value}. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        if new_status == IncidentStatus.RESOLVED:
            self.resolved_at = datetime.datetime.utcnow()
        
        # Append to timeline. Assign a new list: SQLAlchemy does not detect
        # in-place mutation of a plain JSON column, so appending to the loaded
        # list would never be persisted.
        self.status_timeline = list(self.status_timeline or []) + [
            {"status": new_status.value, "timestamp": datetime.datetime.utcnow().isoformat()}
        ]


class RemediationAudit(Base):
    __tablename__ = "remediation_audits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("project_config.id"), nullable=True, index=True)
    proposed_action = Column(String, nullable=False)
    patch_details = Column(JSON, nullable=True)       # The exact YAML diff applied
    previous_values = Column(JSON, nullable=True)     # Pre-patch values for rollback
    policy_verdict = Column(String, nullable=False)     # APPROVED / ESCALATED / REJECTED
    policy_reason = Column(Text, nullable=True)
    github_pr_url = Column(String, nullable=True)
    github_branch = Column(String, nullable=True)
    execution_status = Column(
        Enum(ExecutionStatus, name="execution_status"),
        default=ExecutionStatus.PR_CREATED,
        nullable=True
    )

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Trust calibration fields
    is_shadow_run = Column(String, default="false")  # "true" / "false"
    human_agreed = Column(String, nullable=True)      # "true" / "false" / None (pending)
    failure_reason = Column(String, nullable=True)     # verification_failed, policy_blocked, invalid_patch
    failure_root_cause = Column(
        Enum(FailureRootCause, name="failure_root_cause"),
        nullable=True
    )

    # Relationship back to parent incident
    incident = relationship("Incident", back_populates="remediation_audits")


class ProjectConfig(Base):
    """
    Multi-tenant project configuration. Each project gets its own row.
    GitHub token is encrypted at rest using Fernet (AES-128-CBC).
    """
    __tablename__ = "project_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, default="Default Project")
    github_repo = Column(String, nullable=True)
    prometheus_url = Column(String, nullable=True)
    target_namespace = Column(String, default="autofixops")
    target_manifest_path = Column(String, default="kubernetes_integration/target_app/deployment.yaml")
    shadow_mode = Column(String, default="true")
    confidence_threshold = Column(Float, default=0.80)

    # Safety bounds
    allowed_chaos_namespaces = Column(JSON, default=["staging", "test", "dev", "default", "autofixops"])
    max_resource_scale_factor = Column(Float, default=2.0)  # Max 2x increase

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    incidents = relationship("Incident", back_populates="project")


class SystemConfig(Base):
    """
    Global system configuration — kill switch, mode control.
    Exactly one row (id='global'). Checked at top of every Celery task.
    """
    __tablename__ = "system_config"

    id = Column(String, primary_key=True, default="global")
    system_mode = Column(
        Enum(SystemMode, name="system_mode"),
        default=SystemMode.ACTIVE,
        nullable=False
    )
    disabled_reason = Column(Text, nullable=True)
    disabled_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ServiceAccount(Base):
    """
    System-level GitHub service account (bot identity).
    Singleton row (id='github'). All engine components resolve
    credentials through this before falling back to .env.
    """
    __tablename__ = "service_accounts"

    id = Column(String, primary_key=True, default="github")
    account_type = Column(String, nullable=False, default="github")
    display_name = Column(String, nullable=False, default="AutoFixOps Bot")
    github_username = Column(String, nullable=True)
    github_token_encrypted = Column(Text, nullable=True)
    is_active = Column(String, default="true")  # "true" / "false"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    @staticmethod
    def _get_fernet():
        """Returns a Fernet cipher using the ENCRYPTION_KEY env var."""
        import os
        from cryptography.fernet import Fernet
        import base64
        import hashlib
        key_material = os.getenv("ENCRYPTION_KEY", "autofixops-default-key-change-me")
        key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
        return Fernet(key)

    def set_github_token(self, plaintext_token: str):
        """Encrypts and stores the GitHub token."""
        if not plaintext_token:
            self.github_token_encrypted = None
            return
        f = self._get_fernet()
        self.github_token_encrypted = f.encrypt(plaintext_token.encode()).decode()

    def get_github_token(self) -> str:
        """Decrypts and returns the GitHub token."""
        if not self.github_token_encrypted:
            return ""
        f = self._get_fernet()
        return f.decrypt(self.github_token_encrypted.encode()).decode()

    def get_masked_token(self) -> str:
        """Returns masked token for UI display."""
        token = self.get_github_token()
        if not token or len(token) < 8:
            return "***"
        return token[:4] + "****" + token[-4:]


class PatchProposal(Base):
    """
    Stage 2 AI patch proposal: a reviewable, sandbox-validated diff that is
    NEVER applied, committed, pushed or deployed. One row per proposer run.

    status: RUNNING | VALIDATED | NOT_GENERATED | REJECTED | FAILED
    Invariant (enforced in engine.patch_proposer.finalize): unified_diff is set
    only when status == VALIDATED, and sha256(unified_diff) == diff_sha256.
    Human approval belongs to Stage 3; the incident itself stays DIAGNOSED.
    """
    __tablename__ = "patch_proposals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("project_config.id"), nullable=True)
    status = Column(String(16), nullable=False, index=True)
    reason_code = Column(String(48), nullable=True)
    reason_detail = Column(Text, nullable=True)
    action_type = Column(String(32), nullable=True)
    target_file = Column(String(255), nullable=True)
    target_ref = Column(JSON, nullable=True)            # kind/name/namespace/container/field/line
    base = Column(JSON, nullable=True)                  # eol, size, sha256s, git_blob_sha
    patched_git_blob_sha = Column(String(40), nullable=True)
    previous_values = Column(JSON, nullable=True)       # PatchGenerator-compatible keys
    proposed_values = Column(JSON, nullable=True)
    candidates = Column(JSON, nullable=True)
    unified_diff = Column(Text, nullable=True)
    diff_sha256 = Column(String(64), nullable=True)
    evidence = Column(JSON, nullable=True)
    llm_decision = Column(JSON, nullable=True)
    rationale = Column(Text, nullable=True)             # LLM inference; display only
    review_notes = Column(JSON, nullable=True)
    validation = Column(JSON, nullable=True)            # ordered per-check results
    model_info = Column(JSON, nullable=True)
    source_report = Column(JSON, nullable=True)
    proposer_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class PatchRoutingDecision(Base):
    """
    Stage 2 confidence-based routing for AI patch generation. One row per
    incident and diagnosis version (sha256 of the stored ai_report).

    route:    AUTOMATIC_AI (confidence >= 0.80) | HUMAN_APPROVAL_REQUIRED
    decision: AUTO | PENDING | APPROVED | REJECTED  (APPROVED/REJECTED are final)
    The route is computed by the backend from the stored report, never from
    client input. It decides only whether a human must approve *generation*;
    it never relaxes a safety check and never authorises applying a patch.
    """
    __tablename__ = "patch_routing_decisions"
    __table_args__ = (UniqueConstraint("incident_id", "report_sha256", name="uq_routing_incident_report"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True)
    report_sha256 = Column(String(64), nullable=False)
    diagnosis_confidence = Column(Float, nullable=False)
    route = Column(String(32), nullable=False)
    approval_required = Column(Boolean, nullable=False)
    decision = Column(String(16), nullable=False)
    decided_by = Column(JSON, nullable=True)            # actor info as available; no invented identity
    decision_reason = Column(Text, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    generation_requested_at = Column(DateTime, nullable=True)   # atomic claim: at most one generation
    proposal_id = Column(UUID(as_uuid=True), ForeignKey("patch_proposals.id"), nullable=True)
    proposal_status = Column(String(16), nullable=True)
    proposal_reason_code = Column(String(48), nullable=True)
    audit_log = Column(JSON, nullable=True)             # append-only list of events
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)


class RemediationRun(Base):
    """
    Stages 3-4: one approved remediation of one VALIDATED Stage 2 proposal.

    stage: APPROVED -> PR_OPEN -> READY_TO_DEPLOY -> DEPLOYING -> VERIFYING -> RESOLVED
           (APPROVED -> READY_TO_DEPLOY when the PR step is disabled by config)
           REJECTED | FAILED | ROLLED_BACK | ROLLBACK_FAILED are final.
    A step that cannot run yet (e.g. GitHub or deployment not configured, PR not
    merged) leaves the stage unchanged and records blocked_code; it can be retried.
    Every human decision and step result is appended to audit_log.
    deploy_lock is True only while this run may change the cluster; a partial
    unique index allows one such run per target at a time.
    """
    __tablename__ = "remediation_runs"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_remediation_proposal"),
        Index("uq_remediation_deploy_lock", "target_key", unique=True,
              postgresql_where=Column("deploy_lock").is_(True), sqlite_where=Column("deploy_lock").is_(True)),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False, index=True)
    proposal_id = Column(UUID(as_uuid=True), ForeignKey("patch_proposals.id"), nullable=False)
    report_sha256 = Column(String(64), nullable=False)          # diagnosis version that was approved
    diff_sha256 = Column(String(64), nullable=False)            # exact diff that was approved
    target_key = Column(String(255), nullable=False)            # namespace/Deployment/name
    stage = Column(String(24), nullable=False, index=True)
    step_token = Column(String(64), nullable=True)              # claim for the step in progress
    step_started_at = Column(DateTime, nullable=True)
    blocked_code = Column(String(48), nullable=True)
    blocked_detail = Column(Text, nullable=True)
    decided_by = Column(JSON, nullable=True)                    # approver as verified; no invented identity
    decision_reason = Column(Text, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    github = Column(JSON, nullable=True)                        # mode, branch, pr number/url/state, shas
    deployment = Column(JSON, nullable=True)                    # mode, context, previous snapshot, patch, output
    verification = Column(JSON, nullable=True)                  # rounds of checks with timestamps
    rollback = Column(JSON, nullable=True)
    deploy_lock = Column(Boolean, nullable=True)
    audit_log = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
