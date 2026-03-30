from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.execution import DeploymentExecutor, PipelineContext


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RedeployService:
    """Lightweight release manager for faster redeploys and future rolling updates."""

    def __init__(self, *, executor: DeploymentExecutor, host: str, ctx: PipelineContext):
        self.executor = executor
        self.host = host
        self.ctx = ctx

    def prepare_redeploy(self) -> dict[str, Any]:
        deploy_root = f"/opt/orbital/{self.ctx.slug}"
        release_id = f"r{self.ctx.release_id or int(_utcnow().timestamp())}"
        release_dir = f"{deploy_root}/releases/{release_id}"
        previous_release_dir = f"{deploy_root}/current"

        strategy = "rolling" if self.ctx.rolling_update_enabled else "fast"
        self.ctx.redeploy_strategy = strategy
        self.ctx.deploy_root = deploy_root
        self.ctx.release_dir = release_dir
        self.ctx.previous_release_dir = previous_release_dir
        self.ctx.minimal_downtime_attempted = True

        commands = [
            f"mkdir -p {deploy_root}",
            f"mkdir -p {deploy_root}/releases",
            f"mkdir -p {release_dir}",
            f"test -L {deploy_root}/current || true",
        ]
        results = self.executor.ssh.run_many(self.host, commands)

        return {
            "strategy": strategy,
            "deploy_root": deploy_root,
            "release_dir": release_dir,
            "previous_release_dir": previous_release_dir,
            "minimal_downtime_attempted": True,
            "prepare_results": results,
        }

    def activate_new_release(self) -> list:
        if not self.ctx.deploy_root or not self.ctx.release_dir:
            return []
        commands = [
            f"ln -sf {self.ctx.release_dir} {self.ctx.deploy_root}/current",
        ]
        return self.executor.ssh.run_many(self.host, commands)

    def rollback_to_previous_release(self) -> list:
        """Basis/Stub: keep interface stable for future fully automatic rollback."""
        if not self.ctx.deploy_root:
            return []
        commands = [
            f"test -L {self.ctx.deploy_root}/current || true",
        ]
        return self.executor.ssh.run_many(self.host, commands)

    def cleanup_old_release(self) -> list:
        if not self.ctx.deploy_root or not self.ctx.release_dir:
            return []
        commands = [
            f"find {self.ctx.deploy_root}/releases -mindepth 1 -maxdepth 1 -type d ! -path '{self.ctx.release_dir}' -print",
        ]
        return self.executor.ssh.run_many(self.host, commands)
