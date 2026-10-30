"""
GitHub gateway for Stage 3 (pull requests only).
=================================================
A thin wrapper over PyGithub with exactly the operations the remediation
workflow needs, so the workflow can be tested with an in-memory fake that has
the same methods. It never merges, never pushes to the default branch and never
force-updates anything.

Credentials come from api.config_helpers.get_github_credentials (Fernet-encrypted
service-account token in Postgres, else GITHUB_TOKEN in .env). Tokens are never
logged or stored in workflow records.
"""

import logging
from typing import Optional

logger = logging.getLogger("autofixops")


class GitHubError(RuntimeError):
    """A GitHub call failed. `code` is a stable reason code for workflow records."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _pr_dict(pr) -> dict:
    return {"number": pr.number, "url": pr.html_url, "state": pr.state, "merged": bool(pr.merged),
            "draft": bool(getattr(pr, "draft", False)), "head_ref": pr.head.ref, "head_sha": pr.head.sha,
            "base_ref": pr.base.ref, "merge_commit_sha": pr.merge_commit_sha if pr.merged else None}


class PyGithubGateway:
    """Real GitHub access. Construct with a token and an "owner/name" repository."""

    def __init__(self, token: str, repo_full_name: str):
        from github import Auth, Github

        self.repo_full_name = repo_full_name
        self._gh = Github(auth=Auth.Token(token), timeout=20)
        self._repo = None

    def _call(self, what: str, fn):
        from github import GithubException

        try:
            return fn()
        except GithubException as exc:
            message = (exc.data or {}).get("message") if isinstance(exc.data, dict) else str(exc.data)
            raise GitHubError("GITHUB_API_ERROR", f"{what}: HTTP {exc.status} {message}"[:300])
        except Exception as exc:   # network errors, timeouts
            raise GitHubError("GITHUB_UNREACHABLE", f"{what}: {exc.__class__.__name__}")

    @property
    def repo(self):
        if self._repo is None:
            self._repo = self._call("get repository", lambda: self._gh.get_repo(self.repo_full_name))
        return self._repo

    def default_branch(self) -> str:
        return self.repo.default_branch

    def get_file(self, path: str, ref: str) -> tuple:
        """(content bytes, git blob sha) of a file at a branch or commit."""
        item = self._call(f"read {path}@{ref}", lambda: self.repo.get_contents(path, ref=ref))
        return item.decoded_content, item.sha

    def branch_head(self, branch: str) -> Optional[str]:
        from github import GithubException

        try:
            return self.repo.get_branch(branch).commit.sha
        except GithubException as exc:
            if exc.status == 404:
                return None
            raise GitHubError("GITHUB_API_ERROR", f"read branch {branch}: HTTP {exc.status}")

    def create_branch(self, branch: str, from_sha: str):
        self._call(f"create branch {branch}",
                   lambda: self.repo.create_git_ref(ref=f"refs/heads/{branch}", sha=from_sha))

    def put_file(self, path: str, content: bytes, base_blob_sha: str, branch: str, message: str) -> str:
        """Commits new content on `branch`; GitHub refuses if the file's blob is not base_blob_sha."""
        result = self._call(f"commit {path} on {branch}", lambda: self.repo.update_file(
            path=path, message=message, content=content, sha=base_blob_sha, branch=branch))
        return result["commit"].sha

    def find_pr(self, head_branch: str) -> Optional[dict]:
        owner = self.repo_full_name.split("/")[0]
        pulls = self._call("list pull requests",
                           lambda: list(self.repo.get_pulls(state="all", head=f"{owner}:{head_branch}")))
        return _pr_dict(pulls[0]) if pulls else None

    def create_pr(self, title: str, body: str, head: str, base: str, draft: bool) -> dict:
        pr = self._call("create pull request",
                        lambda: self.repo.create_pull(title=title, body=body, head=head, base=base, draft=draft))
        return _pr_dict(pr)

    def get_pr(self, number: int) -> dict:
        return _pr_dict(self._call(f"read pull request #{number}", lambda: self.repo.get_pull(number)))


def gateway_from_config(project_id=None) -> tuple:
    """(gateway or None, info). None means GitHub is not configured; info says why (no secrets)."""
    import os

    from api.config_helpers import get_github_credentials

    creds = get_github_credentials(project_id) or {}
    token = creds.get("token") or ""
    repo = (creds.get("repo") or os.getenv("GITHUB_REPO") or "").strip()
    if not token or not repo or repo.count("/") != 1:
        missing = [name for name, ok in (("token", token), ("repository (owner/name)", repo.count("/") == 1)) if not ok]
        return None, {"configured": False, "missing": missing}
    return PyGithubGateway(token, repo), {"configured": True, "repo": repo, "source": creds.get("source", "unknown")}
