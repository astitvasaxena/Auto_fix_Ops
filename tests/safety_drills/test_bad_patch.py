"""
Safety Drill: Bad Patch
========================
Injects invalid action types and malformed manifests.
Verifies PatchGenerator raises errors cleanly without corrupting state.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from engine.patch_generator import PatchGenerator


def test_unknown_action_raises():
    """An unsupported action type must raise ValueError, not silently pass."""
    gen = PatchGenerator()
    manifest = {"spec": {"template": {"spec": {"containers": [{"name": "app"}]}}}}
    
    try:
        gen.generate({"type": "FORMAT_DISK"}, manifest)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "FORMAT_DISK" in str(e)
        print("✅ PASS: Unknown action 'FORMAT_DISK' raised ValueError")


def test_empty_containers_raises():
    """Patching a manifest with no containers must raise ValueError."""
    gen = PatchGenerator()
    manifest = {"spec": {"template": {"spec": {"containers": []}}}}
    
    try:
        gen.generate({"type": "INCREASE_MEMORY_LIMIT", "patch_value": "256Mi"}, manifest)
        assert False, "Should have raised ValueError for empty containers"
    except ValueError as e:
        assert "No containers" in str(e)
        print("✅ PASS: Empty containers list correctly raises ValueError")


def test_patch_does_not_mutate_original():
    """Patch generator must deep-copy — never mutate the input manifest."""
    gen = PatchGenerator()
    original = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "resources": {"limits": {"memory": "100Mi"}},
                        }
                    ]
                }
            }
        }
    }
    
    patched, previous = gen.generate({"type": "INCREASE_MEMORY_LIMIT", "patch_value": "150Mi"}, original)
    
    # Original must be unchanged
    assert original["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "100Mi", \
        "Original manifest was mutated!"
    assert patched["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "150Mi"
    assert previous == {"memory_limit_app": "100Mi"}, previous   # recorded for exact rollback
    
    # A request above the 2x safety bound is clamped, and the input is still untouched.
    clamped, _ = gen.generate({"type": "INCREASE_MEMORY_LIMIT", "patch_value": "256Mi"}, original)
    assert clamped["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "200Mi"
    assert original["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "100Mi"

    print("✅ PASS: Original manifest not mutated by patch")


def test_restart_annotation_format():
    """Restart annotation must be a valid ISO timestamp."""
    gen = PatchGenerator()
    manifest = {"spec": {"template": {"metadata": {}, "spec": {"containers": [{"name": "app"}]}}}}
    
    patched, _ = gen.generate({"type": "RESTART_POD"}, manifest)
    annotation = patched["spec"]["template"]["metadata"]["annotations"].get(
        "kubectl.kubernetes.io/restartedAt", ""
    )
    
    assert annotation.endswith("Z"), f"Expected ISO timestamp ending with Z, got: {annotation}"
    print(f"✅ PASS: Restart annotation is valid: {annotation}")


if __name__ == "__main__":
    test_unknown_action_raises()
    test_empty_containers_raises()
    test_patch_does_not_mutate_original()
    test_restart_annotation_format()
    print("\n🛡️  All bad patch drills passed.")
