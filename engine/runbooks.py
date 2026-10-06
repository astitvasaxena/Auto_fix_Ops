"""
Curated operational guidance derived from official documentation (RAG knowledge base).
=======================================================================================
Each document paraphrases operational guidance from official Kubernetes and
Prometheus documentation. They are NOT records of real incidents, NOT
historical AutoFixOps outcomes, and NOT automatic remediation rules. Each one
is structured as: symptom → evidence to collect → possible interpretations →
diagnostic steps → remediation considerations → safety limitations.

Every document carries:

  source_type   = "official_documentation"   (what kind of knowledge this is)
  source_status = "curated"                  (paraphrased and reviewed by a human)
  verified      = True                       (checked against the cited official page)
  source_organization / source_title / source_url   (primary official source)
  additional_sources                         (other official pages it draws on)

Seeded explicitly via:  python scripts/seed_rag_runbooks.py
Stored only in the `operational_runbooks` Qdrant collection.
"""

from typing import Dict, List

from engine.memory import RUNBOOK_SOURCE_STATUS, RUNBOOK_SOURCE_TYPE

RUNBOOK_VERSION = 2
REVIEWED_ON = "2026-10-06"
GUIDANCE_TYPE = "operational_guidance"

DISCLAIMER = (
    "Curated from official documentation; not a historical incident and not an "
    "automatic remediation rule."
)

# Official sources (the only authorities this knowledge base cites).
K8S_DEBUG = ("Kubernetes", "Monitoring, Logging, and Debugging",
             "https://kubernetes.io/docs/tasks/debug/")
K8S_TROUBLESHOOT_APPS = ("Kubernetes", "Troubleshooting Applications",
                         "https://kubernetes.io/docs/tasks/debug/debug-application/")
K8S_DEBUG_RUNNING_POD = ("Kubernetes", "Debug Running Pods",
                         "https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/")
K8S_ASSIGN_MEMORY = ("Kubernetes", "Assign Memory Resources to Containers and Pods",
                     "https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/")
K8S_MANAGE_RESOURCES = ("Kubernetes", "Resource Management for Pods and Containers",
                        "https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/")
PROM_ALERTING_RULES = ("Prometheus", "Alerting rules",
                       "https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/")
PROM_ALERTING_PRACTICES = ("Prometheus", "Alerting (best practices)",
                           "https://prometheus.io/docs/practices/alerting/")
PROM_ALERTING_OVERVIEW = ("Prometheus", "Alerting overview",
                          "https://prometheus.io/docs/alerting/latest/overview/")

OFFICIAL_SOURCES = (
    K8S_DEBUG, K8S_TROUBLESHOOT_APPS, K8S_DEBUG_RUNNING_POD, K8S_ASSIGN_MEMORY,
    K8S_MANAGE_RESOURCES, PROM_ALERTING_RULES, PROM_ALERTING_PRACTICES, PROM_ALERTING_OVERVIEW,
)

REQUIRED_FIELDS = (
    "runbook_id", "source_type", "source_status", "source_organization", "source_title",
    "source_url", "verified", "topic", "title", "incident_type", "incident_types",
    "guidance_type", "content", "disclaimer", "embedding_text",
)

SECTION_TITLES = (
    ("symptom", "Symptom"),
    ("operational_guidance", "Operational guidance (from the official source)"),
    ("evidence_to_collect", "Evidence to collect"),
    ("possible_interpretations", "Possible interpretations"),
    ("diagnostic_steps", "Recommended diagnostic steps"),
    ("remediation_considerations", "Remediation considerations"),
    ("safety_limitations", "Safety limitations"),
)

_DOCUMENT_SPECS: List[Dict] = [
    {
        "runbook_id": "k8s-repeated-container-failure",
        "title": "Kubernetes: Repeated Container Failure (CrashLoopBackOff)",
        "topic": "Containers that repeatedly crash and restart",
        "source": K8S_DEBUG_RUNNING_POD,
        "additional_sources": [K8S_TROUBLESHOOT_APPS],
        "incident_types": ["CRASH_LOOP", "TARGET_APP_DOWN", "DEPLOYMENT_FAILURE"],
        "related_alerts": ["TargetAppCrashLoop", "PodCrashLooping", "CrashLoopBackOff"],
        "retrieval_terms": "application keeps crashing, container restarting repeatedly, crash loop, "
                           "restart count increasing, back-off, process exits after start",
        "symptom": (
            "A container keeps starting and then exiting or being killed, so the application is "
            "repeatedly or intermittently unavailable. Its restart count keeps rising and Kubernetes "
            "waits longer between restarts, reported as a back-off waiting reason such as CrashLoopBackOff."
        ),
        "operational_guidance": (
            "kubectl describe pod shows each container's state (Waiting, Running or Terminated), its "
            "restart count and recent events. The restart count is the documented signal for detecting "
            "crash loops in containers whose restart policy is Always. It shows that the container keeps "
            "failing, not why: the cause has to come from termination details, logs and events."
        ),
        "evidence_to_collect": [
            "Restart count and current container state from kubectl describe pod",
            "The last termination reason and exit code of the container",
            "Recent events for the Pod: the reporting component, reason and message",
            "Logs from the current container and from the previous, crashed instance (kubectl logs --previous)",
            "Recent changes: image tag, configuration, Secrets/ConfigMaps, dependencies",
        ],
        "possible_interpretations": [
            "An error exit soon after start, with errors in the previous instance's logs, points to an "
            "application, configuration or dependency fault",
            "A termination reason of OOMKilled means the memory limit was exceeded (see the memory guidance)",
            "Restarts that began with a rollout point to the new release or its configuration",
            "A rising restart count alone does not identify the cause",
        ],
        "diagnostic_steps": [
            "Run kubectl describe pod and read the state, last state, restart count and events",
            "Read the crashed instance's output with kubectl logs <pod> --previous and compare it with current logs",
            "Correlate the first restart with deployments or configuration changes",
            "If the image has no shell, use kubectl debug with an ephemeral container instead of kubectl exec",
        ],
        "remediation_considerations": [
            "Fix the configuration or dependency the logs and events identify, or roll back a release that "
            "introduced the failure, once the evidence points there",
            "Change memory limits only if the termination reason is OOMKilled",
            "Restarting a crash-looping Pod rarely helps: Kubernetes is already restarting it",
        ],
        "safety_limitations": [
            "Do not assume exit codes, termination reasons, events or log lines that were not observed",
            "Without Pod status, logs or events the root cause cannot be confirmed; escalate to a human",
            "A rollback is only safe when a known-good previous version exists",
        ],
    },
    {
        "runbook_id": "k8s-oomkilled-memory-pressure",
        "title": "Kubernetes: OOMKilled and Memory Pressure",
        "topic": "Container memory usage, memory limits and OOM kills",
        "source": K8S_ASSIGN_MEMORY,
        "additional_sources": [K8S_MANAGE_RESOURCES],
        "incident_types": ["HIGH_MEMORY", "CRASH_LOOP", "TARGET_APP_DOWN"],
        "related_alerts": ["HighMemoryUsage", "TargetAppMemoryLeak"],
        "retrieval_terms": "memory usage high, memory growing, memory leak suspected, container restarted, "
                           "OOMKilled, out of memory, memory limit",
        "symptom": (
            "A container's memory usage is high or growing, and the container may have been terminated "
            "and restarted."
        ),
        "operational_guidance": (
            "A container is guaranteed the memory it requests and is not allowed to use more than its "
            "limit. Memory limits are enforced reactively by the kernel with out-of-memory (OOM) kills when "
            "it detects memory pressure; the kubelet restarts a killed container if it can be restarted. "
            "The documented evidence of this is a status of OOMKilled, with lastState.terminated showing "
            "reason OOMKilled and exit code 137."
        ),
        "evidence_to_collect": [
            "The actual memory usage metric over time, not a single sample",
            "The container's configured memory request and limit",
            "The termination reason and exit code in the container's lastState (OOMKilled or not)",
            "Restart count and recent events from kubectl describe pod",
            "Whether the Pod is Pending because its memory request does not fit on any node",
        ],
        "possible_interpretations": [
            "High memory usage alone does NOT prove an OOM kill or a memory leak",
            "OOMKilled together with a rising restart count shows the container exceeds its limit; trend data "
            "is still needed to tell a leak from legitimate demand",
            "Steady growth that never plateaus suggests a leak; growth that follows load suggests demand",
            "Restarts without an OOMKilled reason have another cause (see the crash guidance)",
            "A Pod stuck Pending with a large memory request is a scheduling or capacity problem, not runtime usage",
        ],
        "diagnostic_steps": [
            "Check kubectl get pod / kubectl describe pod for an OOMKilled status, Last State and Restart Count",
            "Inspect kubectl get pod <pod> --output=yaml for lastState.terminated.reason and exitCode",
            "Compare the memory usage trend with the configured limit",
            "Correlate the growth with releases and traffic",
        ],
        "remediation_considerations": [
            "Raising the memory limit may be appropriate only after evidence shows a resource-capacity "
            "problem, such as OOMKilled with usage at the limit under normal load",
            "If the evidence points to a leak, a higher limit only delays the next kill; the fix belongs in the application",
            "Requests drive scheduling: a larger request can leave the Pod Pending if no node has room",
        ],
        "safety_limitations": [
            "Do not claim OOMKilled, exit code 137, memory values or restart counts unless they were observed",
            "Without telemetry, state that memory behaviour is unconfirmed",
            "Changing limits requires human review and policy approval; this guidance does not authorize it",
        ],
    },
    {
        "runbook_id": "k8s-cpu-limits-throttling",
        "title": "Kubernetes: High CPU, CPU Limits and Throttling",
        "topic": "Container CPU usage, CPU requests and limits, CPU throttling",
        "source": K8S_MANAGE_RESOURCES,
        "additional_sources": [],
        "incident_types": ["HIGH_CPU"],
        "related_alerts": ["HighCPUUsage"],
        "retrieval_terms": "CPU usage is high, CPU spike, CPU stress, busy loop, CPU throttling, slow responses "
                           "under load, CPU limit, cores, millicores",
        "symptom": (
            "CPU usage is high, or the application is slow while its CPU usage sits near the configured limit."
        ),
        "operational_guidance": (
            "The scheduler places Pods using their resource requests, and limits cap what a running container "
            "may use. CPU limits are enforced by throttling: when a container approaches its CPU limit the "
            "kernel restricts its CPU access, so it cannot use more than the limit. Unlike exceeding a memory "
            "limit, reaching the CPU limit slows the container down; it does not terminate it."
        ),
        "evidence_to_collect": [
            "CPU usage over time (cores or millicores) compared with the configured CPU request and limit",
            "CPU throttling metrics, if they are collected",
            "Request rate and latency, to see whether load increased",
            "Recent deployments that could have changed CPU cost",
            "Restarts and events: high CPU on its own does not terminate a container",
        ],
        "possible_interpretations": [
            "High usage well below the limit is a busy workload, not throttling",
            "Usage pinned at the limit while latency rises suggests throttling: a capacity or efficiency problem",
            "High CPU starting with a release suggests a regression",
            "Restarts during high CPU have another cause (for example failing probes under load or memory), "
            "because CPU limits throttle rather than kill",
        ],
        "diagnostic_steps": [
            "Compare usage with the container's configured request and limit (shown by kubectl describe pod)",
            "Decide whether the usage is sustained or a short spike",
            "Correlate with traffic changes and releases",
            "Check events for probe failures under load",
        ],
        "remediation_considerations": [
            "Raising the CPU limit is reasonable only when evidence shows sustained, legitimate usage at the "
            "limit with user impact",
            "If a regression or runaway work causes the usage, a higher limit hides it and consumes capacity",
            "Raising requests changes scheduling and can leave Pods Pending",
        ],
        "safety_limitations": [
            "Do not state CPU values, throttling or configured limits that are not in the incident data",
            "Resource changes require human review and policy approval; this guidance does not authorize them",
        ],
    },
    {
        "runbook_id": "k8s-application-troubleshooting",
        "title": "Kubernetes: Application Troubleshooting Workflow",
        "topic": "Locating why an application on Kubernetes is not working",
        "source": K8S_TROUBLESHOOT_APPS,
        "additional_sources": [K8S_DEBUG, K8S_DEBUG_RUNNING_POD],
        "incident_types": ["DEPLOYMENT_FAILURE", "CRASH_LOOP", "TARGET_APP_DOWN", "UNKNOWN"],
        "related_alerts": [],
        "retrieval_terms": "application not working, failed deployment, rollout problem, new release broken, "
                           "pod pending, pod not ready, unknown cause",
        "symptom": (
            "An application running on Kubernetes is not working as expected, for example after a new "
            "release, and the cause is not yet known."
        ),
        "operational_guidance": (
            "Kubernetes documents application troubleshooting as focused tasks: debugging Pods, Services, "
            "StatefulSets and init containers, determining the reason for Pod failure, debugging running Pods "
            "and getting a shell in a running container. The first step is to establish which object is failing "
            "and in what state, then follow the matching task."
        ),
        "evidence_to_collect": [
            "Pod phase, container states, readiness and restart counts",
            "Events for the Pod and its owning workload (for example FailedScheduling)",
            "Current and previous container logs",
            "Termination reasons and messages",
            "Service and endpoint state when Pods run but cannot be reached",
            "What changed recently: image, configuration, resource settings",
        ],
        "possible_interpretations": [
            "Scheduling: a Pending Pod with a FailedScheduling event lacks resources or matching nodes",
            "Startup or configuration: failing init containers, missing configuration, crashes at start",
            "Application: the container runs but returns errors (check its logs)",
            "Resources: OOMKilled terminations or CPU limits",
            "Service or networking: healthy Pods but a Service without matching endpoints",
            "Dependencies: logs show failures reaching databases or other services",
        ],
        "diagnostic_steps": [
            "List and describe the Pods; read their events",
            "Read current and previous container logs",
            "Pending → investigate scheduling; Running but not ready → probes and application; "
            "restarting → follow the repeated-failure guidance",
            "Use kubectl exec, or an ephemeral debug container, for live inspection",
            "If the Pods are healthy, debug the Service in front of them",
        ],
        "remediation_considerations": [
            "Choose a remediation only after the failure is categorized",
            "Consider a rollback when the failure began with a release and a known-good version exists",
            "Escalate to a human when the evidence is insufficient",
        ],
        "safety_limitations": [
            "These are investigation steps for an operator; AutoFixOps does not run kubectl",
            "Do not invent object names, events or log lines",
        ],
    },
    {
        "runbook_id": "prometheus-target-availability",
        "title": "Prometheus: Target Down and Service Availability",
        "topic": "Interpreting an up == 0 (target down) alert",
        "source": PROM_ALERTING_RULES,
        "additional_sources": [PROM_ALERTING_PRACTICES],
        "incident_types": ["TARGET_APP_DOWN", "DEPLOYMENT_FAILURE"],
        "related_alerts": ["TargetAppDown", "InstanceDown"],
        "retrieval_terms": "target is down, target down, app down, application unavailable, service unreachable, "
                           "instance down, up == 0, scrape failing, health check failing",
        "symptom": (
            "An alert reports that a monitored target is down or unreachable, for example a rule on up == 0."
        ),
        "operational_guidance": (
            "The official alerting-rules example InstanceDown fires when up == 0 has held for five minutes and "
            "uses label templating to name the affected instance and job; the for clause keeps a briefly true "
            "condition pending rather than firing. up == 0 means Prometheus could not scrape the target. It is "
            "a symptom: on its own it does not say whether the application crashed, was redeployed, is "
            "overloaded, or whether the network or scrape configuration is at fault."
        ),
        "evidence_to_collect": [
            "Alert labels (instance, job, namespace, pod) and annotations (summary, description, runbook link)",
            "How long the condition has held (pending versus firing)",
            "Whether the application process or Pod is running, and whether it is restarting",
            "Whether the metrics endpoint is reachable",
            "Other alerts for the same component, and recent deployments",
        ],
        "possible_interpretations": [
            "Target down while the Pod restarts → the application is crashing (see the repeated-failure guidance)",
            "Target down while the Pod is running and healthy → a scrape path, network, Service or endpoint problem",
            "Target down right after a deployment → the release or its configuration",
            "A briefly true condition may be transient; the for clause exists to reduce such noise",
        ],
        "diagnostic_steps": [
            "Use the alert labels and annotations to identify the exact instance and job",
            "Check whether the process or Pod is running and how often it restarts",
            "Check whether the target's metrics endpoint responds",
            "Correlate with deployments and with other alerts for the component",
        ],
        "remediation_considerations": [
            "The remediation depends on the cause: fix the application if it crashes, roll back if a release "
            "caused it, fix scrape or network configuration if the application is healthy",
            "Restart a workload only if it is hung and the cause is understood",
        ],
        "safety_limitations": [
            "The alert proves the scrape failed, not the root cause",
            "Do not invent instance names, durations or Pod states",
        ],
    },
    {
        "runbook_id": "prometheus-alert-triage",
        "title": "Prometheus: Using Alerts as Investigation Evidence",
        "topic": "Triaging a fired alert into a diagnosis",
        "source": PROM_ALERTING_PRACTICES,
        "additional_sources": [PROM_ALERTING_RULES, PROM_ALERTING_OVERVIEW],
        "incident_types": ["TARGET_APP_DOWN", "HIGH_CPU", "HIGH_MEMORY", "CRASH_LOOP", "DEPLOYMENT_FAILURE"],
        "related_alerts": [],
        "retrieval_terms": "alert fired, how to triage an alert, alert annotations, runbook link, Alertmanager "
                           "notification, symptom versus cause",
        "symptom": "An alert has fired and has to be turned into a diagnosis.",
        "operational_guidance": (
            "Prometheus best practice is to keep alerting simple, alert on symptoms, have good consoles to "
            "pinpoint causes, and avoid pages where there is nothing to do; alerts should link to relevant "
            "consoles or runbooks. Alerting rules attach labels and annotations, such as descriptions and "
            "runbook links, with templated values. Prometheus evaluates the rules and sends alerts to "
            "Alertmanager, which groups, silences, inhibits and routes notifications. An alert is evidence "
            "that a condition held; it is not a diagnosis."
        ),
        "evidence_to_collect": [
            "Alert name, labels and annotations, including any runbook link",
            "The rule expression and threshold, to know exactly what condition held",
            "How long it was pending and firing, and the evaluated value if available",
            "The underlying metrics before and during the alert",
            "Related alerts for the same component",
        ],
        "possible_interpretations": [
            "Symptom alerts (unavailable, slow, erroring) show user impact; the cause still has to be found",
            "Capacity alerts (resources near a limit) show risk rather than a failure",
            "Several alerts on one component may share a single cause",
            "Annotation text is static template text written by the rule author; verify it against labels and metrics",
        ],
        "diagnostic_steps": [
            "Read the rule expression to understand exactly what fired",
            "Identify the affected component from the labels",
            "Follow the runbook link if the annotations provide one",
            "Inspect the underlying metrics and related alerts",
        ],
        "remediation_considerations": [
            "Choose actions from the evidence collected; the alert itself does not authorize a change",
            "If the alert is not actionable, improving the alert is a valid outcome",
        ],
        "safety_limitations": [
            "Do not treat the alert name as the root cause",
            "Do not invent alert values, durations or label values",
        ],
    },
]


def _source_dict(source) -> Dict[str, str]:
    organization, title, url = source
    return {"organization": organization, "title": title, "url": url}


def _render_content(spec: Dict) -> str:
    org, title, url = spec["source"]
    lines = [
        f"# {spec['title']}",
        f"Source: {org} — {title} ({url})",
    ]
    for extra in spec["additional_sources"]:
        lines.append(f"Also based on: {extra[0]} — {extra[1]} ({extra[2]})")
    lines += ["", DISCLAIMER, ""]
    for key, heading in SECTION_TITLES:
        lines.append(f"## {heading}")
        value = spec[key]
        lines.extend([value] if isinstance(value, str) else [f"- {item}" for item in value])
        lines.append("")
    return "\n".join(lines).strip()


def _embedding_text(spec: Dict) -> str:
    """Compact retrieval profile. Kept short because the embedding model
    truncates long inputs; the full guidance lives in `content`."""
    parts = [spec["title"], spec["topic"], spec["symptom"], spec["retrieval_terms"]]
    if spec["related_alerts"]:
        parts.append("Alerts: " + ", ".join(spec["related_alerts"]))
    return ". ".join(parts)


def load_runbooks() -> List[Dict]:
    """Returns the curated official-source documents as Qdrant-ready payloads."""
    documents = []
    for spec in _DOCUMENT_SPECS:
        org, title, url = spec["source"]
        document = {
            "runbook_id": spec["runbook_id"],
            "version": RUNBOOK_VERSION,
            "source_type": RUNBOOK_SOURCE_TYPE,
            "source_status": RUNBOOK_SOURCE_STATUS,
            "source_organization": org,
            "source_title": title,
            "source_url": url,
            "additional_sources": [_source_dict(s) for s in spec["additional_sources"]],
            "verified": True,
            "reviewed_on": REVIEWED_ON,
            "guidance_type": GUIDANCE_TYPE,
            "title": spec["title"],
            "topic": spec["topic"],
            "incident_type": spec["incident_types"][0],
            "incident_types": list(spec["incident_types"]),
            "related_alerts": list(spec["related_alerts"]),
            "disclaimer": DISCLAIMER,
            "sections": {key: spec[key] for key, _ in SECTION_TITLES},
            "content": _render_content(spec),
            "embedding_text": _embedding_text(spec),
        }
        missing = [f for f in REQUIRED_FIELDS if document.get(f) in (None, "", [])]
        if missing:
            raise ValueError(f"Document {spec['runbook_id']} missing fields: {missing}")
        documents.append(document)
    return documents
