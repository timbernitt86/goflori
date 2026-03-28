import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from flask import current_app, has_app_context


@dataclass
class CloneResult:
    repo_url: str
    branch: str
    local_path: str
    commit_hash: str | None
    command_results: list["GitCommandResult"]

    # Backward compatibility with older call sites.
    @property
    def commit_sha(self) -> str | None:
        return self.commit_hash


@dataclass
class GitCommandResult:
    command: str
    return_code: int
    stdout: str
    stderr: str


class LocalRepoCloneService:
    def __init__(self):
        configured_root = ""
        if has_app_context():
            configured_root = current_app.config.get("ORBITAL_REPO_CLONE_ROOT", "")
        if configured_root:
            self.clone_root = self._resolve_clone_root(configured_root)
        else:
            # Default to /tmp/orbital/repos on Unix; use system temp on Windows.
            if Path("/tmp").exists():
                self.clone_root = Path("/tmp") / "orbital" / "repos"
            else:
                self.clone_root = Path(tempfile.gettempdir()) / "orbital" / "repos"
        self.clone_root.mkdir(parents=True, exist_ok=True)

    def _resolve_clone_root(self, configured_root: str) -> Path:
        configured = Path(configured_root)
        if configured.is_absolute():
            if os.name == "nt" and configured.drive == "" and configured_root.replace("\\", "/").startswith("/"):
                return Path(tempfile.gettempdir()) / configured_root.replace("\\", "/").strip("/")
            return configured

        normalized = configured_root.replace("\\", "/")
        if normalized.startswith("/"):
            if os.name != "nt" and Path("/tmp").exists():
                return Path(normalized)
            return Path(tempfile.gettempdir()) / normalized.strip("/")

        return Path(normalized)

    def _reset_clone_dir(self, target_dir: Path) -> Path:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def _allocate_clone_dir(self, deployment_id: int | str | None, repo_url: str) -> Path:
        if deployment_id is None:
            repo_name = self._safe_name(repo_url)
            return Path(tempfile.mkdtemp(prefix=f"{repo_name}-", dir=str(self.clone_root)))

        preferred_dir = self._deployment_clone_dir(deployment_id)
        try:
            return self._reset_clone_dir(preferred_dir)
        except OSError:
            repo_name = self._safe_name(repo_url)
            return Path(
                tempfile.mkdtemp(
                    prefix=f"{deployment_id}-{repo_name}-",
                    dir=str(self.clone_root),
                )
            )

    def _safe_name(self, repo_url: str) -> str:
        value = repo_url.rstrip("/").split("/")[-1]
        value = value.removesuffix(".git")
        value = re.sub(r"[^a-zA-Z0-9_-]", "-", value)
        return value or "repository"

    def _prepare_repository_url(self, repo_url: str, access_token: str | None = None) -> tuple[str, str]:
        # Extension point for token-based auth support in future iterations.
        if access_token:
            return repo_url, "token_placeholder"
        return repo_url, "public"

    def _run_git(self, args: list[str], cwd: Path | None = None) -> GitCommandResult:
        command = " ".join(args)
        try:
            completed = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return GitCommandResult(
                command=command,
                return_code=completed.returncode,
                stdout=(completed.stdout or "").strip(),
                stderr=(completed.stderr or "").strip(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError("git executable not found on PATH") from exc

    def _assert_ok(self, result: GitCommandResult) -> None:
        if result.return_code == 0:
            return
        raise RuntimeError(f"git command failed: {result.command} | {result.stderr}")

    def _deployment_clone_dir(self, deployment_id: int | str) -> Path:
        return self.clone_root / str(deployment_id)

    def clone(self, repo_url: str, branch: str = "main", depth: int = 1, deployment_id: int | str | None = None, access_token: str | None = None) -> CloneResult:
        branch_name = (branch or "main").strip() or "main"
        authenticated_url, _auth_strategy = self._prepare_repository_url(repo_url=repo_url, access_token=access_token)

        local_dir = self._allocate_clone_dir(deployment_id=deployment_id, repo_url=repo_url)

        command_results: list[GitCommandResult] = []
        clone_result = self._run_git(
            [
                "git",
                "clone",
                "--depth",
                str(depth),
                "--branch",
                branch_name,
                authenticated_url,
                str(local_dir),
            ]
        )
        command_results.append(clone_result)
        self._assert_ok(clone_result)

        rev_parse = self._run_git(["git", "rev-parse", "HEAD"], cwd=local_dir)
        command_results.append(rev_parse)
        self._assert_ok(rev_parse)

        commit_hash = rev_parse.stdout.strip() or None
        return CloneResult(
            repo_url=repo_url,
            branch=branch_name,
            local_path=str(local_dir),
            commit_hash=commit_hash,
            command_results=command_results,
        )

    def cleanup(self, local_path: str) -> None:
        target = Path(local_path)
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
