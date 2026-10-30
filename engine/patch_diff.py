"""
Minimal, comment-preserving YAML edits and strict unified diffs (Stage 2).
==========================================================================
Pure helpers: no database, no LLM, no subprocess. Nothing here writes to the
repository. The only file writes happen inside a fresh TemporaryDirectory
created by sandbox_apply(), which is removed before it returns.

A proposal changes exactly one scalar. locate_scalar() finds it with
yaml.compose (so line/column come from the real parser), splice_scalar()
replaces only that scalar's characters (indent, key, quoting and inline
comment stay byte-identical), and the resulting unified diff is treated as
untrusted input by check_diff_policy() and apply_unified_diff_strict().
"""

import difflib
import hashlib
import os
import re
import tempfile
from pathlib import Path

import yaml

from engine.patch_safety import MAX_DIFF_BYTES, PatchSafetyError, find_hidden_character, validate_repo_relative_path


class PatchDiffError(ValueError):
    """A render, diff or apply check failed. `code` is the persisted reason code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ─── Locating and splicing one scalar ───

_MERGE_TAG = "tag:yaml.org,2002:merge"


def assert_plain_document(text: str):
    """Rejects anchors, aliases, explicit tags, merge keys (<<) and duplicate mapping keys anywhere."""
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise PatchDiffError("UNSUPPORTED_MANIFEST", "anchors/aliases are not supported")
            tag = getattr(event, "tag", None)
            implicit = getattr(event, "implicit", True)
            if tag and not (implicit is True or (isinstance(implicit, tuple) and any(implicit))):
                raise PatchDiffError("UNSUPPORTED_MANIFEST", f"explicit tag {tag} is not supported")
        nodes = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except (yaml.YAMLError, RecursionError) as exc:
        raise PatchDiffError("UNSUPPORTED_MANIFEST", f"YAML error: {type(exc).__name__}")

    def walk(node, depth=0):
        if depth > 100:
            raise PatchDiffError("UNSUPPORTED_MANIFEST", "YAML nesting is too deep")
        if isinstance(node, yaml.MappingNode):
            # Merge-key precedence differs between YAML libraries (PyYAML vs the Kubernetes decoder),
            # so the value we validate might not be the value the cluster uses.
            if any(k.tag == _MERGE_TAG or (isinstance(k, yaml.ScalarNode) and k.value == "<<")
                   for k, _ in node.value):
                raise PatchDiffError("UNSUPPORTED_MANIFEST", "YAML merge keys (<<) are not supported")
            keys = [k.value for k, _ in node.value if isinstance(k, yaml.ScalarNode)]
            if len(keys) != len(set(keys)):
                raise PatchDiffError("UNSUPPORTED_MANIFEST", "duplicate mapping key")
            for k, v in node.value:
                walk(k, depth + 1)
                walk(v, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item, depth + 1)

    for node in nodes:
        walk(node)
    return nodes


def _mapping_get(node, key):
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def locate_container_scalar(text: str, field_path: tuple) -> dict:
    """Finds spec.template.spec.containers[0].<field_path> as a single-line block scalar."""
    nodes = assert_plain_document(text)
    if len(nodes) != 1:
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "exactly one YAML document is required")
    node = nodes[0]
    for key in ("spec", "template", "spec", "containers"):
        node = _mapping_get(node, key)
    if not isinstance(node, yaml.SequenceNode) or len(node.value) != 1:
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "exactly one container is required")
    node = node.value[0]
    for key in field_path:
        node = _mapping_get(node, key)
        if node is None:
            raise PatchDiffError("CURRENT_VALUE_MISSING", ".".join(field_path) + " is not set")
    if not isinstance(node, yaml.ScalarNode):
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "target value is not a scalar")
    if node.style not in (None, '"', "'"):
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "block scalars are not supported")
    if node.start_mark.line != node.end_mark.line:
        raise PatchDiffError("UNSUPPORTED_MANIFEST", "multi-line scalars are not supported")
    return {
        "value": node.value, "style": node.style,
        "start": node.start_mark.index, "end": node.end_mark.index,
        "line": node.start_mark.line + 1, "column": node.start_mark.column + 1,
        "raw_slice": text[node.start_mark.index:node.end_mark.index],
    }


def render_scalar(new_value: str, style) -> str:
    if style == '"':
        return f'"{new_value}"'
    if style == "'":
        return f"'{new_value}'"
    try:
        reloads_as_same_str = yaml.safe_load(new_value) == new_value
    except yaml.YAMLError:
        reloads_as_same_str = False
    return new_value if reloads_as_same_str else f'"{new_value}"'


def splice_scalar(text: str, span: dict, new_value: str) -> str:
    return text[: span["start"]] + render_scalar(new_value, span["style"]) + text[span["end"]:]


# ─── Unified diff: build, parse, police, apply ───

def make_unified_diff(rel: str, before_lf: str, after_lf: str) -> str:
    if not before_lf.endswith("\n") or not after_lf.endswith("\n"):
        raise PatchDiffError("RENDER_FAILED", "file must end with a newline")
    # Split on "\n" only, exactly like the parser and applier below (str.splitlines also
    # breaks on U+0085/U+2028/U+2029 and other separators).
    lines = difflib.unified_diff(
        _split_keepends(before_lf), _split_keepends(after_lf),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3,
    )
    return "".join(lines)


def _split_keepends(text: str) -> list:
    return [line + "\n" for line in text.split("\n")[:-1]]


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FORBIDDEN_PREFIXES = (
    "diff --git", "index ", "old mode", "new mode", "deleted file", "new file",
    "similarity", "dissimilarity", "rename ", "copy ", "Binary files", "GIT binary patch",
    "\\ No newline",
)


def parse_unified_diff_strict(diff_text: str, expected_rel: str) -> dict:
    """Parses exactly one file's diff; rejects git extended headers and path tricks."""
    if not isinstance(diff_text, str) or not diff_text:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "empty diff")
    encoded = diff_text.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES:
        raise PatchDiffError("PATCH_TOO_LARGE", f"{len(encoded)} bytes > {MAX_DIFF_BYTES}")
    if "\r" in diff_text or "\x00" in diff_text:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "CR or NUL in diff")
    hidden = find_hidden_character(diff_text)
    if hidden is not None:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", f"hidden or line-separator character U+{ord(hidden[1]):04X}")
    if not diff_text.endswith("\n"):
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "diff must end with a newline")
    lines = diff_text[:-1].split("\n")
    for line in lines:
        if line.startswith(_FORBIDDEN_PREFIXES):
            raise PatchDiffError("DIFF_POLICY_VIOLATION", f"forbidden diff line: {line[:40]!r}")
    if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "missing ---/+++ headers")
    old_path, new_path = lines[0][4:], lines[1][4:]
    if not old_path.startswith("a/") or not new_path.startswith("b/"):
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "headers must be a/<path> and b/<path>")
    old_rel, new_rel = old_path[2:], new_path[2:]
    try:
        validate_repo_relative_path(old_rel)
        validate_repo_relative_path(new_rel)
    except PatchSafetyError as exc:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", f"header path rejected: {exc.detail}")
    if old_rel != new_rel or old_rel != expected_rel:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "diff targets a different file")

    hunks, i = [], 2
    while i < len(lines):
        match = _HUNK_RE.fullmatch(lines[i])
        if not match:
            raise PatchDiffError("DIFF_POLICY_VIOLATION", f"expected hunk header at line {i + 1}")
        old_start, old_len = int(match.group(1)), int(match.group(2) or 1)
        new_start, new_len = int(match.group(3)), int(match.group(4) or 1)
        i += 1
        body, seen_old, seen_new = [], 0, 0
        while i < len(lines) and not lines[i].startswith("@@"):
            line = lines[i]
            if line.startswith("--- ") or line.startswith("+++ "):
                raise PatchDiffError("DIFF_POLICY_VIOLATION", "more than one file in diff")
            tag = line[:1]
            if tag not in (" ", "-", "+"):
                raise PatchDiffError("DIFF_POLICY_VIOLATION", f"invalid hunk line {line[:40]!r}")
            body.append((tag, line[1:]))
            seen_old += tag in (" ", "-")
            seen_new += tag in (" ", "+")
            i += 1
        if (seen_old, seen_new) != (old_len, new_len):
            raise PatchDiffError("DIFF_POLICY_VIOLATION", "hunk header counts do not match its body")
        hunks.append({"old_start": old_start, "old_len": old_len,
                      "new_start": new_start, "new_len": new_len, "lines": body})
    if not hunks:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "no hunks")
    return {"path": old_rel, "hunks": hunks}


_PLUS_LINE_RE = re.compile(r"^(\s*)(memory|cpu):\s*([\"']?)([0-9]+(?:Mi|Gi|m)?)\3(\s+#.*)?$")


def check_diff_policy(parsed: dict, base_lines: list, located_line: int, field_key: str):
    """Exactly one hunk with exactly one '-' (the located line) and one matching '+' line."""
    if len(parsed["hunks"]) != 1:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "exactly one hunk is allowed")
    body = parsed["hunks"][0]["lines"]
    minus = [text for tag, text in body if tag == "-"]
    plus = [text for tag, text in body if tag == "+"]
    if len(minus) != 1 or len(plus) != 1:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "exactly one line may change")
    if minus[0] != base_lines[located_line - 1]:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "removed line is not the located limit line")
    old_m, new_m = _PLUS_LINE_RE.fullmatch(minus[0]), _PLUS_LINE_RE.fullmatch(plus[0])
    if not old_m or not new_m or new_m.group(2) != field_key:
        raise PatchDiffError("DIFF_POLICY_VIOLATION", "changed line is not a resource-limit line")
    for group in (1, 2, 3, 5):
        if old_m.group(group) != new_m.group(group):
            raise PatchDiffError("DIFF_POLICY_VIOLATION", "indent, key, quoting or comment changed")


def apply_unified_diff_strict(before_lf: str, parsed: dict) -> str:
    """Applies hunks at their stated positions only (no fuzz, no offset)."""
    src = before_lf.split("\n")
    trailing = src[-1] == ""
    if trailing:
        src = src[:-1]
    out, cursor = [], 0
    for hunk in parsed["hunks"]:
        start = hunk["old_start"] - 1 if hunk["old_len"] else hunk["old_start"]
        if start < cursor or start > len(src):
            raise PatchDiffError("APPLY_FAILED", "hunk position out of range or overlapping")
        out.extend(src[cursor:start])
        position = start
        for tag, text in hunk["lines"]:
            if tag in (" ", "-"):
                if position >= len(src) or src[position] != text:
                    raise PatchDiffError("APPLY_FAILED", f"context mismatch at line {position + 1}")
                position += 1
            if tag in (" ", "+"):
                out.append(text)
        cursor = position
    out.extend(src[cursor:])
    return "\n".join(out) + ("\n" if trailing else "")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def sandbox_apply(rel: str, before_lf: str, diff_text: str, expected_after_lf: str, repo_root) -> dict:
    """Writes the base into a fresh temp dir, applies the diff there, and removes it."""
    repo_resolved = Path(repo_root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if _is_within(temp_root, repo_resolved):
        raise PatchDiffError("APPLY_FAILED", "the temporary directory is inside the repository")
    record = {}
    with tempfile.TemporaryDirectory(prefix="autofixops-patch-", dir=temp_root) as tmp:
        sandbox = Path(tmp).resolve()
        record["sandbox"] = str(sandbox)
        if _is_within(sandbox, repo_resolved):
            raise PatchDiffError("APPLY_FAILED", "sandbox must not be inside the repository")
        target = (sandbox / rel).resolve()
        if not _is_within(target, sandbox):
            raise PatchDiffError("APPLY_FAILED", "sandbox target escapes the sandbox")
        target.parent.mkdir(parents=True, exist_ok=True)
        base_bytes = before_lf.encode("utf-8")
        with open(target, "xb") as fh:
            fh.write(base_bytes)
        if not target.is_file() or _sha256(target.read_bytes()) != _sha256(base_bytes):
            raise PatchDiffError("APPLY_FAILED", "sandbox base copy does not match")
        parsed = parse_unified_diff_strict(diff_text, rel)
        applied = apply_unified_diff_strict(target.read_bytes().decode("utf-8"), parsed)
        with open(target, "wb") as fh:
            fh.write(applied.encode("utf-8"))
        result = target.read_bytes().decode("utf-8")
        if result != expected_after_lf:
            raise PatchDiffError("APPLY_FAILED", "applied result differs from the rendered edit")
        record["after_text"] = result
    if os.path.exists(record["sandbox"]):
        raise PatchDiffError("APPLY_FAILED", "sandbox directory was not removed")
    return record


# ─── Semantic comparison ───

def _flatten(obj, prefix=()):
    if isinstance(obj, dict):
        items = {}
        for key, value in obj.items():
            items.update(_flatten(value, prefix + (str(key),)))
        return items or {prefix: {}}
    if isinstance(obj, list):
        items = {}
        for index, value in enumerate(obj):
            items.update(_flatten(value, prefix + (f"[{index}]",)))
        return items or {prefix: []}
    return {prefix: obj}


def changed_paths(before, after) -> set:
    flat_before, flat_after = _flatten(before), _flatten(after)
    return {
        ".".join(path) for path in set(flat_before) | set(flat_after)
        if flat_before.get(path, object()) != flat_after.get(path, object())
    }
