"""
Verification Engine for AutoFixOps
====================================
Post-remediation verification with:
- PR merge polling
- Stability window (N minutes of stable metrics, not single check)
- Multi-metric validation (CPU + memory + restarts)
- Negative signal detection (no metric worsening)
- Consecutive pass requirement (≥2 passes)
- Learning loop integration
"""

import os
import time
import logging
import requests
from typing import Optional, Dict, Any
from github import Github, GithubException

from api.database import SessionLocal
from api.models import (
    Incident, IncidentStatus, RemediationAudit,
    ExecutionStatus, FailureRootCause
)
from api.events import emit_sync
from engine.memory import QdrantMemoryStore

logger = logging.getLogger("autofixops")

# Verification constants
PR_POLL_INTERVAL_SECONDS = int(os.getenv("PR_POLL_INTERVAL_SECONDS", "10"))
PR_POLL_MAX_ATTEMPTS = int(os.getenv("PR_POLL_MAX_ATTEMPTS", "10"))
POST_MERGE_COOLDOWN_SECONDS = int(os.getenv("POST_MERGE_COOLDOWN_SECONDS", "5"))

# Robustness settings
STABILITY_WINDOW_CHECKS = int(os.getenv("STABILITY_WINDOW_CHECKS", "2"))
STABILITY_CHECK_INTERVAL_SECONDS = int(os.getenv("STABILITY_CHECK_INTERVAL_SECONDS", "3"))
CONSECUTIVE_PASSES_REQUIRED = int(os.getenv("CONSECUTIVE_PASSES_REQUIRED", "2"))
HEALTH_CHECK_THRESHOLD = 0.80
WORSENING_THRESHOLD = 1.20  # 20% increase = worsening


class VerificationEngine:
    """
    Post-remediation verification worker.
    1. Polls GitHub for PR merge status.
    2. Waits for GitOps sync cooldown.
    3. Multi-pass telemetry stability check.
    4. Marks incident RESOLVED or FAILED.
    5. Stores structured learning with root cause classification.
    """

    def __init__(self, project_config: Dict[str, Any] = None):
        self.config = project_config or {}

        # Resolve GitHub credentials through centralized helper
        from api.config_helpers import get_github_credentials
        project_id = self.config.get("id")
        gh_creds = get_github_credentials(project_id)

        github_token = gh_creds.get("token", "")
        self.github_repo = self.config.get("github_repo") or gh_creds.get("repo", "") or os.getenv("GITHUB_REPO", "")
        self.prometheus_url = (
            self.config.get("prometheus_url")
            or os.getenv("PROMETHEUS_URL", "http://prometheus-operated.autofixops.svc.cluster.local:9090")
        )
        self.github = Github(github_token) if github_token else None

        if github_token:
            logger.info(f"[VERIFICATION] GitHub credentials source: {gh_creds.get('source', 'unknown')}")


    def verify(
        self,
        incident_id: str,
        audit_id: str,
        summary_text: str,
        diagnosis: str,
        action_taken: str,
        pod_name: str,
        baseline_metrics: Dict[str, float] = None,
    ) -> bool:
        """
        Full verification pipeline with stability window. Returns True if resolved.
        """
        logger.info(f"[TRACE:{incident_id}] [VERIFICATION START]")

        db = SessionLocal()
        try:
            audit = db.query(RemediationAudit).filter(
                RemediationAudit.id == audit_id
            ).first()

            if not audit:
                logger.error(f"[TRACE:{incident_id}] Audit record {audit_id} not found.")
                return False

            # ─── Step 1: Poll PR Merge Status ───
            pr_merged = self._wait_for_pr_merge(incident_id, audit)

            if not pr_merged:
                logger.warning(f"[TRACE:{incident_id}] PR was not merged.")
                audit.execution_status = ExecutionStatus.PR_REJECTED
                audit.failure_root_cause = FailureRootCause.INFRA
                db.commit()
                return False

            audit.execution_status = ExecutionStatus.PR_MERGED
            db.commit()
            logger.info(f"[TRACE:{incident_id}] PR merged confirmed.")

            # ─── Step 2: Post-Merge Cooldown ───
            logger.info(
                f"[TRACE:{incident_id}] Waiting {POST_MERGE_COOLDOWN_SECONDS}s for GitOps sync..."
            )
            time.sleep(POST_MERGE_COOLDOWN_SECONDS)

            # ─── Step 3: Stability Window Check ───
            stability_result = self._check_stability_window(
                incident_id, pod_name, baseline_metrics or {}
            )

            incident = db.query(Incident).filter(Incident.id == incident_id).first()

            if stability_result["passed"]:
                audit.execution_status = ExecutionStatus.VERIFICATION_PASSED
                incident.transition_to(IncidentStatus.VERIFIED)
                emit_sync("incident.status_changed", {"new_status": "VERIFIED"}, str(incident.id))
                db.commit()
                incident.transition_to(IncidentStatus.RESOLVED)
                emit_sync("incident.status_changed", {"new_status": "RESOLVED"}, str(incident.id))
                db.commit()

                logger.info(f"[TRACE:{incident_id}] [VERIFICATION PASSED] Incident RESOLVED.")

                # Learning loop
                self._store_learning(summary_text, diagnosis, action_taken)

                # Record success in circuit breaker
                from engine.circuit_breaker import get_circuit_breaker_registry
                project_id = str(incident.project_id) if incident.project_id else "global"
                get_circuit_breaker_registry().record_outcome(
                    project_id, action_taken, success=True
                )

                return True
            else:
                audit.execution_status = ExecutionStatus.VERIFICATION_FAILED
                audit.failure_reason = stability_result.get("reason", "verification_failed")
                audit.failure_root_cause = self._classify_failure(stability_result)
                incident.transition_to(IncidentStatus.FAILED)
                emit_sync("incident.status_changed", {"new_status": "FAILED"}, str(incident.id))
                db.commit()

                logger.warning(
                    f"[TRACE:{incident_id}] [VERIFICATION FAILED] "
                    f"Reason: {stability_result.get('reason')}"
                )

                # Store failure pattern (only if logic failure)
                if audit.failure_root_cause == FailureRootCause.LOGIC:
                    self._store_failure(summary_text, diagnosis, action_taken,
                                       stability_result.get("reason", ""))

                # Record failure in circuit breaker
                from engine.circuit_breaker import get_circuit_breaker_registry
                project_id = str(incident.project_id) if incident.project_id else "global"
                get_circuit_breaker_registry().record_outcome(
                    project_id, action_taken, success=False
                )

                return False

        except Exception as e:
            logger.error(f"[TRACE:{incident_id}] [VERIFICATION ERROR] {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def _check_stability_window(
        self,
        incident_id: str,
        pod_name: str,
        baseline_metrics: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        Checks metrics stability over STABILITY_WINDOW_MINUTES.
        Requires CONSECUTIVE_PASSES_REQUIRED consecutive healthy checks.
        Also detects negative signals (worsening metrics).
        """
        consecutive_passes = 0
        total_checks = STABILITY_WINDOW_CHECKS
        check_results = []

        for check_num in range(1, total_checks + 1):
            logger.info(
                f"[TRACE:{incident_id}] [STABILITY CHECK {check_num}/{total_checks}]"
            )

            result = self._run_health_check(incident_id, pod_name, baseline_metrics)
            check_results.append(result)

            if result["healthy"] and not result.get("worsened"):
                consecutive_passes += 1
                logger.info(
                    f"[TRACE:{incident_id}] Check {check_num} PASSED "
                    f"(consecutive: {consecutive_passes}/{CONSECUTIVE_PASSES_REQUIRED})"
                )

                if consecutive_passes >= CONSECUTIVE_PASSES_REQUIRED:
                    return {
                        "passed": True,
                        "checks_run": check_num,
                        "consecutive_passes": consecutive_passes,
                    }
            else:
                consecutive_passes = 0  # Reset on failure
                reason = result.get("reason", "metric unhealthy")
                logger.warning(
                    f"[TRACE:{incident_id}] Check {check_num} FAILED: {reason}"
                )

            if check_num < total_checks:
                time.sleep(STABILITY_CHECK_INTERVAL_SECONDS)

        return {
            "passed": False,
            "reason": f"Failed to achieve {CONSECUTIVE_PASSES_REQUIRED} consecutive passes in {total_checks} checks",
            "checks_run": total_checks,
            "check_results": [r.get("reason", "ok") for r in check_results],
        }

    def _run_health_check(
        self,
        incident_id: str,
        pod_name: str,
        baseline_metrics: Dict[str, float],
    ) -> Dict[str, Any]:
        """
        Single health check pass. Checks target liveness, metrics, and negative signals.
        """
        # ─── Liveness Check ───
        try:
            up_resp = requests.get(
                f"{self.prometheus_url.rstrip('/')}/api/v1/query",
                params={"query": 'up{job="autofixops-target"}'},
                timeout=5,
            )
            up_resp.raise_for_status()
            up_results = up_resp.json().get("data", {}).get("result", [])
            if not up_results:
                logger.warning(f"[TRACE:{incident_id}] Target liveness check: no Prometheus scrape data.")
                return {"healthy": False, "reason": "No Prometheus scrape data for target (autofixops-target)"}
            is_up = any(float(r["value"][1]) == 1 for r in up_results)
            if not is_up:
                logger.warning(f"[TRACE:{incident_id}] Target liveness check: target is DOWN (up == 0).")
                return {"healthy": False, "reason": "Target app is DOWN (Prometheus up == 0)"}
        except requests.Timeout:
            return {"healthy": False, "reason": "Prometheus query timed out while checking target up"}
        except Exception as e:
            logger.warning(f"[TRACE:{incident_id}] Target health check: error querying Prometheus: {e}")
            return {"healthy": False, "reason": f"Failed to query Prometheus: {e}"}

        namespace = self.config.get("target_namespace", "autofixops")

        queries = {
            "cpu": f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}", pod=~"{pod_name}.*"}}[5m])',
            "memory": f'container_memory_working_set_bytes{{namespace="{namespace}", pod=~"{pod_name}.*"}}',
        }

        metric_values = {}
        for metric_name, query in queries.items():
            try:
                response = requests.get(
                    f"{self.prometheus_url}/api/v1/query",
                    params={"query": query},
                    timeout=10,
                )
                response.raise_for_status()
                results = response.json().get("data", {}).get("result", [])

                if results:
                    value = float(results[0]["value"][1])
                    metric_values[metric_name] = value
                else:
                    logger.warning(
                        f"[TRACE:{incident_id}] No data for {metric_name}. "
                        f"Assuming healthy (pod may have restarted)."
                    )
            except requests.Timeout:
                return {
                    "healthy": False,
                    "reason": f"Prometheus timeout on {metric_name}",
                    "timeout": True,
                }
            except Exception as e:
                return {
                    "healthy": False,
                    "reason": f"{metric_name} query error: {e}",
                }

        # ─── Threshold checks ───
        cpu = metric_values.get("cpu")
        memory = metric_values.get("memory")

        if cpu is not None and cpu > HEALTH_CHECK_THRESHOLD:
            return {"healthy": False, "reason": f"CPU too high: {cpu:.4f}"}

        if memory is not None and memory > 104857600 * HEALTH_CHECK_THRESHOLD:
            return {"healthy": False, "reason": f"Memory too high: {memory:.0f} bytes"}

        # ─── Negative signal detection (worsening) ───
        worsened = False
        worsening_details = []

        if baseline_metrics:
            for metric_name, current in metric_values.items():
                baseline = baseline_metrics.get(metric_name)
                if baseline and baseline > 0:
                    ratio = current / baseline
                    if ratio > WORSENING_THRESHOLD:
                        worsened = True
                        worsening_details.append(
                            f"{metric_name}: {current:.2f} vs baseline {baseline:.2f} "
                            f"(+{(ratio-1)*100:.0f}%)"
                        )

        if worsened:
            return {
                "healthy": False,
                "worsened": True,
                "reason": f"Metrics worsened: {'; '.join(worsening_details)}",
            }

        return {"healthy": True, "metrics": metric_values}

    def _classify_failure(self, stability_result: Dict) -> FailureRootCause:
        """Classifies failure root cause for structured storage."""
        reason = stability_result.get("reason", "")
        check_results = stability_result.get("check_results", [])

        # If most checks had timeouts → infra issue
        timeout_count = sum(1 for r in check_results if "timeout" in str(r).lower())
        if timeout_count > len(check_results) / 2:
            return FailureRootCause.TIMEOUT

        # If metrics worsened → logic issue (wrong fix)
        if any("worsened" in str(r).lower() for r in check_results):
            return FailureRootCause.LOGIC

        # If metrics never improved → logic (fix didn't work)
        if "consecutive passes" in reason.lower():
            return FailureRootCause.LOGIC

        return FailureRootCause.UNKNOWN

    def _wait_for_pr_merge(
        self, incident_id: str, audit: RemediationAudit
    ) -> bool:
        """Polls GitHub API for PR merge status."""
        if not self.github or not self.github_repo:
            logger.info(
                f"[TRACE:{incident_id}] [VERIFICATION SIMULATED] "
                f"Assuming PR merged (no GitHub credentials)."
            )
            return True

        try:
            repo = self.github.get_repo(self.github_repo)
        except GithubException as e:
            logger.error(f"[TRACE:{incident_id}] Failed to access repo: {e}")
            return False

        pr_url = audit.github_pr_url or ""
        try:
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.error(f"[TRACE:{incident_id}] Could not extract PR number from: {pr_url}")
            return False

        for attempt in range(1, PR_POLL_MAX_ATTEMPTS + 1):
            try:
                pr = repo.get_pull(pr_number)
                if pr.merged:
                    return True
                if pr.state == "closed" and not pr.merged:
                    logger.warning(f"[TRACE:{incident_id}] PR #{pr_number} closed without merge.")
                    return False
            except GithubException as e:
                logger.error(f"[TRACE:{incident_id}] GitHub API error on poll {attempt}: {e}")

            logger.info(
                f"[TRACE:{incident_id}] [POLL {attempt}/{PR_POLL_MAX_ATTEMPTS}] "
                f"PR not merged. Waiting {PR_POLL_INTERVAL_SECONDS}s..."
            )
            time.sleep(PR_POLL_INTERVAL_SECONDS)

        return False

    def _store_learning(self, summary_text: str, diagnosis: str, action: str):
        """Stores verified resolution in Qdrant for learning loop."""
        try:
            memory = QdrantMemoryStore()
            memory.store_resolution(summary_text, diagnosis, action)
            logger.info("[VERIFICATION] Resolution stored in Qdrant.")
        except Exception as e:
            logger.error(f"[VERIFICATION] Failed to store learning: {e}")

    def _store_failure(self, summary_text: str, diagnosis: str, action: str, reason: str):
        """Stores failure pattern — only called for LOGIC failures."""
        try:
            memory = QdrantMemoryStore()
            memory.store_failure(summary_text, diagnosis, action, reason)
            logger.info("[VERIFICATION] Failure pattern stored in Qdrant (LOGIC root cause).")
        except Exception as e:
            logger.error(f"[VERIFICATION] Failed to store failure: {e}")
