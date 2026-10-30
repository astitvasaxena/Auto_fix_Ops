"""
Path and file safety for AI patch proposals (Stage 2).
=======================================================
Pure helpers: no database, no LLM, no subprocess, no writes. Patch proposals
may only ever target files listed in ALLOWED_PATCH_TARGETS; project
configuration can choose among them but can never widen the list.

Every read goes through read_allowlisted_file(), which rejects symlinks,
Windows junctions/reparse points, hard links, non-regular files, oversized
files, non-UTF-8 content and hidden characters (bidi/format controls and the
Unicode line separators U+0085/U+2028/U+2029), and records hashes so later
steps can prove the real file was never modified.
"""

import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

TARGET_MANIFEST = "kubernetes_integration/target_app/deployment.yaml"

# The only files and fields a proposal may change. Code constant by design.
ALLOWED_PATCH_TARGETS = {
    TARGET_MANIFEST: {
        "kinds": frozenset({"Deployment"}),
        "fields": {
            "INCREASE_MEMORY_LIMIT": ("resources", "limits", "memory"),
            "INCREASE_CPU_LIMIT": ("resources", "limits", "cpu"),
        },
    },
}

MAX_MANIFEST_BYTES = 64 * 1024
MAX_MANIFEST_LINES = 2000
MAX_DIFF_BYTES = 4096

_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
# Characters a reviewer cannot see, or that some parsers treat as line breaks
# while git and difflib do not. Tab, LF and CR are handled separately.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})
# Blank-rendering characters whose category is a letter, symbol or combining mark: Hangul fillers,
# braille blank, combining grapheme joiner, Khmer inherent vowels, Mongolian free variation selectors.
_BLANK_LOOKALIKES = frozenset("ᅟᅠㅤﾠ⠀͏឴឵᠋᠌᠍᠏")


def _is_variation_selector(ch: str) -> bool:
    code = ord(ch)
    return 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def find_hidden_character(text: str):
    """(index, char) of the first control, format, separator, private-use or blank look-alike character.

    Space separators other than U+0020 (NBSP, ideographic space, ...) and variation selectors count too.
    """
    for index, ch in enumerate(text):
        if ch in "\t\n\r":
            continue
        category = unicodedata.category(ch)
        if (ch in "\x85\u2028\u2029" or ch in _BLANK_LOOKALIKES or category in _HIDDEN_CATEGORIES
                or (category == "Zs" and ch != " ") or _is_variation_selector(ch)):
            return index, ch
    return None


class PatchSafetyError(ValueError):
    """A path or file failed a safety check. `code` is the persisted reason code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def validate_repo_relative_path(path) -> str:
    """Accepts only plain repo-relative POSIX paths to .yaml/.yml files."""
    if not isinstance(path, str) or not path:
        raise PatchSafetyError("PATH_REJECTED", "path is empty or not a string")
    if not _PATH_RE.fullmatch(path):
        raise PatchSafetyError("PATH_REJECTED", "path contains disallowed characters")
    if path.startswith("/") or "//" in path:
        raise PatchSafetyError("PATH_REJECTED", "absolute or empty path component")
    win = PureWindowsPath(path)
    if win.drive or win.is_absolute():
        raise PatchSafetyError("PATH_REJECTED", "drive-qualified or absolute path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise PatchSafetyError("PATH_REJECTED", "'.' or '..' path component")
    if PurePosixPath(path).suffix not in (".yaml", ".yml"):
        raise PatchSafetyError("PATH_REJECTED", "only .yaml/.yml files may be targeted")
    return path


def allowlisted_field(rel: str, action_type: str):
    """Returns (spec, field_path) for an allowlisted file + action, else raises."""
    spec = ALLOWED_PATCH_TARGETS.get(rel)
    if spec is None:
        raise PatchSafetyError("TARGET_NOT_ALLOWLISTED", rel)
    field = spec["fields"].get(action_type)
    if field is None:
        raise PatchSafetyError("UNSUPPORTED_ACTION", f"{action_type} is not allowlisted for {rel}")
    return spec, field


def _is_link_like(path: Path, st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(os.path, "isjunction", lambda p: False)(path):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def git_blob_sha(lf_bytes: bytes) -> str:
    """Git blob id of LF content; equals `git rev-parse HEAD:<path>` when unchanged."""
    return hashlib.sha1(b"blob %d\0" % len(lf_bytes) + lf_bytes).hexdigest()


@dataclass(frozen=True)
class SafeRead:
    rel: str
    text: str            # decoded file content, original line endings
    text_lf: str         # LF-normalised content
    eol: str             # "LF" or "CRLF"
    size: int
    sha256_raw: str
    sha256_lf: str
    git_blob_sha: str
    mtime_ns: int

    def base_record(self) -> dict:
        return {
            "source": "working_tree", "eol": self.eol, "size": self.size,
            "sha256_raw": self.sha256_raw, "sha256_lf": self.sha256_lf,
            "git_blob_sha": self.git_blob_sha,
        }


def _walk_components(repo_root: Path, rel: str) -> tuple:
    """lstat every component below repo_root; returns (path, final_stat)."""
    current = repo_root
    parts = rel.split("/")
    st = None
    for index, part in enumerate(parts):
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise PatchSafetyError("TARGET_MISSING", rel)
        except OSError as exc:
            raise PatchSafetyError("TARGET_UNREADABLE", f"{type(exc).__name__}")
        if _is_link_like(current, st):
            raise PatchSafetyError("SYMLINK_REJECTED", "/".join(parts[: index + 1]) + " is a link")
        if index < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            raise PatchSafetyError("NOT_REGULAR_FILE", "/".join(parts[: index + 1]) + " is not a directory")
    return current, st


def read_allowlisted_file(repo_root, rel: str) -> SafeRead:
    """Reads an allowlisted file with containment, type, size and encoding checks."""
    validate_repo_relative_path(rel)
    root = Path(repo_root)
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        raise PatchSafetyError("TARGET_UNREADABLE", "repository root is missing")
    path, st = _walk_components(root, rel)
    if not stat.S_ISREG(st.st_mode):
        raise PatchSafetyError("NOT_REGULAR_FILE", rel)
    if getattr(st, "st_nlink", 1) != 1:
        raise PatchSafetyError("NOT_REGULAR_FILE", "hard-linked files are not supported")
    if st.st_size > MAX_MANIFEST_BYTES:
        raise PatchSafetyError("TARGET_TOO_LARGE", f"{st.st_size} bytes > {MAX_MANIFEST_BYTES}")
    try:
        path.resolve(strict=True).relative_to(root_resolved)
    except (OSError, ValueError):
        raise PatchSafetyError("SYMLINK_REJECTED", "target resolves outside the repository")

    try:
        with open(path, "rb") as fh:
            fst = os.fstat(fh.fileno())
            raw = fh.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise PatchSafetyError("TARGET_UNREADABLE", type(exc).__name__)
    same_file = (fst.st_size == st.st_size and fst.st_mtime_ns == st.st_mtime_ns
                 and getattr(fst, "st_nlink", 1) == 1
                 and (not st.st_ino or not fst.st_ino or st.st_ino == fst.st_ino))
    if not same_file:
        raise PatchSafetyError("TARGET_UNREADABLE", "file changed between lstat and open")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise PatchSafetyError("TARGET_TOO_LARGE", f"> {MAX_MANIFEST_BYTES} bytes")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PatchSafetyError("UNSUPPORTED_MANIFEST", "byte-order mark present")
    if b"\x00" in raw:
        raise PatchSafetyError("UNSUPPORTED_MANIFEST", "NUL byte present")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PatchSafetyError("UNSUPPORTED_MANIFEST", "not valid UTF-8")

    crlf = text.count("\r\n")
    if text.count("\r") != crlf or (crlf and crlf != text.count("\n")):
        raise PatchSafetyError("UNSUPPORTED_MANIFEST", "mixed or bare-CR line endings")
    hidden = find_hidden_character(text)
    if hidden is not None:
        index, ch = hidden
        raise PatchSafetyError("UNSUPPORTED_MANIFEST", f"hidden or line-separator character U+{ord(ch):04X} "
                                                       f"at line {text.count(chr(10), 0, index) + 1}")
    text_lf = text.replace("\r\n", "\n")
    if text_lf.count("\n") + 1 > MAX_MANIFEST_LINES:
        raise PatchSafetyError("TARGET_TOO_LARGE", f"> {MAX_MANIFEST_LINES} lines")
    lf_bytes = text_lf.encode("utf-8")
    return SafeRead(
        rel=rel, text=text, text_lf=text_lf, eol="CRLF" if crlf else "LF", size=len(raw),
        sha256_raw=hashlib.sha256(raw).hexdigest(), sha256_lf=hashlib.sha256(lf_bytes).hexdigest(),
        git_blob_sha=git_blob_sha(lf_bytes), mtime_ns=st.st_mtime_ns,
    )


def rehash(repo_root, rel: str) -> tuple:
    """(sha256_raw, mtime_ns) of the real file, re-read with the same checks."""
    current = read_allowlisted_file(repo_root, rel)
    return current.sha256_raw, current.mtime_ns
