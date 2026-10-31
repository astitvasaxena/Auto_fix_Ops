"""
Patch Generator for AutoFixOps
================================
Generates deterministic YAML patches for Kubernetes manifests.
Every patch is bounded and reversible — never LLM-generated.

Safety bounds:
  - Max 2x increase for any resource (configurable per project)
  - Stores previous values for rollback
  - Hard caps per resource type
"""

import copy
import re
import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("autofixops")

# ─── Hard caps (absolute maximums regardless of scale factor) ───
HARD_CAPS = {
    "memory": "4Gi",     # Never exceed 4Gi
    "cpu": "4000m",      # Never exceed 4 cores
}


def parse_memory(value: str) -> int:
    """Converts Kubernetes memory string to bytes."""
    if not value or value == "unset":
        return 0
    value = str(value).strip()
    multipliers = {
        "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3,
        "K": 1000, "M": 1000**2, "G": 1000**3,
    }
    for suffix, mult in multipliers.items():
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * mult)
    return int(value)


def format_memory(bytes_val: int) -> str:
    """Converts bytes back to Kubernetes memory string."""
    if bytes_val >= 1024**3:
        return f"{bytes_val // (1024**3)}Gi"
    if bytes_val >= 1024**2:
        return f"{bytes_val // (1024**2)}Mi"
    if bytes_val >= 1024:
        return f"{bytes_val // 1024}Ki"
    return str(bytes_val)


def parse_cpu(value: str) -> int:
    """Converts Kubernetes CPU string to millicores."""
    if not value or value == "unset":
        return 0
    value = str(value).strip()
    if value.endswith("m"):
        return int(float(value[:-1]))
    return int(float(value) * 1000)


def format_cpu(millicores: int) -> str:
    """Converts millicores back to Kubernetes CPU string."""
    if millicores >= 1000 and millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


class PatchBoundsError(Exception):
    """Raised when a patch exceeds safety bounds."""
    pass


class PatchGenerator:
    """
    Generates structured YAML-compatible patches with safety bounds.
    Tracks previous values for rollback capability.
    """

    def __init__(self, max_scale_factor: float = 2.0):
        self.max_scale_factor = max_scale_factor
        self.previous_values: Dict[str, Any] = {}

    def generate(
        self, action: Dict[str, Any], manifest: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Takes an action dict and manifest, returns (patched_manifest, previous_values).
        
        Returns:
            Tuple of (patched_manifest, previous_values_dict)
        """
        action_type = action.get("type")
        
        dispatch = {
            "INCREASE_MEMORY_LIMIT": self._patch_memory_limit,
            "INCREASE_CPU_LIMIT": self._patch_cpu_limit,
            "RESTART_POD": self._patch_restart_annotation,
            "ROLLBACK_DEPLOYMENT": self._patch_rollback,
            "DISABLE_FEATURE_FLAG": self._patch_feature_flag,
        }

        handler = dispatch.get(action_type)
        if not handler:
            raise ValueError(f"No patch template for action type: {action_type}")

        # Deep copy to avoid mutating the original
        patched = copy.deepcopy(manifest)
        self.previous_values = {}
        handler(patched, action)

        logger.info(f"[PATCH] Generated patch for action: {action_type}")
        return patched, self.previous_values

    def apply_rollback(self, manifest: Dict[str, Any], previous_values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Takes a manifest and a previous_values dict (from RemediationAudit),
        and reverts the manifest exactly to those stored values.
        """
        patched = copy.deepcopy(manifest)
        
        # 1. Revert container resources and images
        try:
            containers = self._get_containers(patched)
        except ValueError:
            containers = []
            
        for container in containers:
            c_name = container.get("name", "default")
            
            # Memory
            mem_key = f"memory_limit_{c_name}"
            if mem_key in previous_values:
                container.setdefault("resources", {}).setdefault("limits", {})["memory"] = previous_values[mem_key]
                
            # CPU
            cpu_key = f"cpu_limit_{c_name}"
            if cpu_key in previous_values:
                container.setdefault("resources", {}).setdefault("limits", {})["cpu"] = previous_values[cpu_key]
                
            # Image
            img_key = f"image_{c_name}"
            if img_key in previous_values:
                container["image"] = previous_values[img_key]

            # Feature flags (env values)
            for entry in container.get("env") or []:
                flag_key = f"env_{c_name}_{entry.get('name')}" if isinstance(entry, dict) else None
                if flag_key in previous_values:
                    entry["value"] = previous_values[flag_key]
                
        # 2. Revert restart annotation
        if "restart_annotation" in previous_values:
            old_restart = previous_values["restart_annotation"]
            annotations = (
                patched.get("spec", {})
                .get("template", {})
                .setdefault("metadata", {})
                .setdefault("annotations", {})
            )
            if old_restart == "never" and "kubectl.kubernetes.io/restartedAt" in annotations:
                del annotations["kubectl.kubernetes.io/restartedAt"]
            elif old_restart != "never":
                annotations["kubectl.kubernetes.io/restartedAt"] = old_restart

        logger.info(f"[PATCH] Applied rollback for {len(previous_values)} previous values.")
        return patched

    def _patch_memory_limit(self, manifest: Dict, action: Dict):
        """Patches memory limit with bounds checking."""
        requested_value = action.get("patch_value", "256Mi")
        containers = self._get_containers(manifest)

        for container in containers:
            resources = container.setdefault("resources", {})
            limits = resources.setdefault("limits", {})
            old_value = limits.get("memory", "128Mi")

            # Store previous
            self.previous_values[f"memory_limit_{container.get('name', 'default')}"] = old_value

            # Bounds check
            old_bytes = parse_memory(old_value)
            new_bytes = parse_memory(requested_value)
            max_bytes = int(old_bytes * self.max_scale_factor) if old_bytes > 0 else new_bytes
            cap_bytes = parse_memory(HARD_CAPS["memory"])

            if new_bytes > max_bytes and old_bytes > 0:
                bounded_value = format_memory(max_bytes)
                logger.warning(
                    f"[PATCH BOUNDS] Memory {requested_value} exceeds {self.max_scale_factor}x of {old_value}. "
                    f"Capping to {bounded_value}."
                )
                new_bytes = max_bytes

            if new_bytes > cap_bytes:
                logger.warning(f"[PATCH BOUNDS] Memory exceeds hard cap. Capping to {HARD_CAPS['memory']}.")
                new_bytes = cap_bytes

            final_value = format_memory(new_bytes)
            limits["memory"] = final_value
            logger.info(
                f"[PATCH] Container '{container.get('name')}': "
                f"memory limit {old_value} → {final_value}"
            )

    def _patch_cpu_limit(self, manifest: Dict, action: Dict):
        """Patches CPU limit with bounds checking."""
        requested_value = action.get("patch_value", "1000m")
        containers = self._get_containers(manifest)

        for container in containers:
            resources = container.setdefault("resources", {})
            limits = resources.setdefault("limits", {})
            old_value = limits.get("cpu", "500m")

            self.previous_values[f"cpu_limit_{container.get('name', 'default')}"] = old_value

            old_mc = parse_cpu(old_value)
            new_mc = parse_cpu(requested_value)
            max_mc = int(old_mc * self.max_scale_factor) if old_mc > 0 else new_mc
            cap_mc = parse_cpu(HARD_CAPS["cpu"])

            if new_mc > max_mc and old_mc > 0:
                bounded_value = format_cpu(max_mc)
                logger.warning(
                    f"[PATCH BOUNDS] CPU {requested_value} exceeds {self.max_scale_factor}x of {old_value}. "
                    f"Capping to {bounded_value}."
                )
                new_mc = max_mc

            if new_mc > cap_mc:
                logger.warning(f"[PATCH BOUNDS] CPU exceeds hard cap. Capping to {HARD_CAPS['cpu']}.")
                new_mc = cap_mc

            final_value = format_cpu(new_mc)
            limits["cpu"] = final_value
            logger.info(
                f"[PATCH] Container '{container.get('name')}': "
                f"cpu limit {old_value} → {final_value}"
            )

    def _patch_restart_annotation(self, manifest: Dict, action: Dict):
        """Adds restart annotation to force rollout."""
        import datetime

        template_metadata = (
            manifest.get("spec", {})
            .get("template", {})
            .setdefault("metadata", {})
        )
        annotations = template_metadata.setdefault("annotations", {})
        old_restart = annotations.get("kubectl.kubernetes.io/restartedAt", "never")
        self.previous_values["restart_annotation"] = old_restart

        restart_time = datetime.datetime.utcnow().isoformat() + "Z"
        annotations["kubectl.kubernetes.io/restartedAt"] = restart_time
        logger.info(f"[PATCH] Added restart annotation: {restart_time}")

    def _patch_rollback(self, manifest: Dict, action: Dict):
        """Rollback: revert image tag."""
        containers = self._get_containers(manifest)

        for container in containers:
            current_image = container.get("image", "unknown:latest")
            self.previous_values[f"image_{container.get('name', 'default')}"] = current_image

            base_image = current_image.rsplit(":", 1)[0]
            container["image"] = f"{base_image}:rollback"
            logger.info(
                f"[PATCH] Container '{container.get('name')}': "
                f"image {current_image} → {container['image']}"
            )

    def _patch_feature_flag(self, manifest: Dict, action: Dict):
        """Turns one allowlisted feature flag off: its env value "true" -> "false". Nothing else is
        allowed: an unknown flag, another value or a manifest without the flag set to "true" is refused."""
        from engine.patch_safety import FEATURE_FLAGS

        flag = action.get("flag")
        if flag not in FEATURE_FLAGS or action.get("patch_value") != "false":
            raise PatchBoundsError(f"only an allowlisted feature flag can be set to \"false\" (got {flag!r})")
        changed = 0
        for container in self._get_containers(manifest):
            for entry in container.get("env") or []:
                if isinstance(entry, dict) and entry.get("name") == flag:
                    if entry.get("value") != "true":
                        raise PatchBoundsError(f"{flag} is {entry.get('value')!r}, not \"true\"")
                    self.previous_values[f"env_{container.get('name', 'default')}_{flag}"] = entry["value"]
                    entry["value"] = "false"
                    changed += 1
        if not changed:
            raise PatchBoundsError(f"no container sets the feature flag {flag}")
        logger.info(f"[PATCH] feature flag {flag}: true -> false")

    def _get_containers(self, manifest: Dict) -> list:
        """Extracts containers list from manifest."""
        containers = (
            manifest.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if not containers:
            raise ValueError("No containers found in manifest to patch.")
        return containers
