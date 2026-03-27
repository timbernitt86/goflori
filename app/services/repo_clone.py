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
    commit_sha: str | None


class LocalRepoCloneService:
    def __init__(self):
        configured_root = ""
        if has_app_context():
            configured_root = current_app.config.get("ORBITAL_REPO_CLONE_ROOT", "")
        if configured_root:
            self.clone_root = Path(configured_root)
        else:
            self.clone_root = Path(tempfile.gettempdir()) / "orbital-repos"
        self.clone_root.mkdir(parents=True, exist_ok=True)

    def _safe_name(self, repo_url: str) -> str:
        value = repo_url.rstrip("/").split("/")[-1]
        value = value.removesuffix(".git")
        value = re.sub(r"[^a-zA-Z0-9_-]", "-", value)
        return value or "repository"

    def _run_git(self, args: list[str], cwd: Path | None = None) -> str:
        try:
            completed = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=120,
                check=True,
            )
            return completed.stdout.strip()
        except FileNotFoundError as exc:
            raise RuntimeError("git executable not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(f"git command failed: {' '.join(args)} | {stderr}") from exc

    def clone(self, repo_url: str, branch: str = "main", depth: int = 1) -> CloneResult:
        repo_name = self._safe_name(repo_url)
        local_dir = Path(tempfile.mkdtemp(prefix=f"{repo_name}-", dir=str(self.clone_root)))

        self._run_git(
            [
                "git",
                "clone",
                "--depth",
                str(depth),
                "--branch",
                branch,
                repo_url,
                str(local_dir),
            ]
        )

        commit_sha = self._run_git(["git", "rev-parse", "HEAD"], cwd=local_dir)
        return CloneResult(
            repo_url=repo_url,
            branch=branch,
            local_path=str(local_dir),
            commit_sha=commit_sha or None,
        )

    def cleanup(self, local_path: str) -> None:
        target = Path(local_path)
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
