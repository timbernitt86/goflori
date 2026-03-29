from celery import shared_task
from datetime import datetime, timezone
import logging

from app.extensions import db
from app.models import ActivityLog, Deployment, DeploymentStep, Project, Server
from app.services.error_analysis import analyze_deployment_failure
from app.services.repo_analyzer import RepoAnalyzer
from app.services.repo_clone import LocalRepoCloneService
from app.services.hetzner import HetznerAPIError
from app.services.execution import DeploymentExecutor, PipelineContext
from app.services.ssh import CommandNotAllowedError, SSHWaitTimeoutError


logger = logging.getLogger(__name__)


STEP_NAMES = [
    "provision_server",
    "wait_for_ssh",
    "clone_repository",
    "analyze_repository",
    "prepare_host",
    "render_artifacts_from_repo",
    "upload_artifacts",
    "start_containers",
    "configure_reverse_proxy",
    "check_dns",
    "run_certbot",
    "verify_https",
    "healthcheck",
]


STEP_ERROR_CATEGORY: dict[str, str] = {
    "provision_server": "hetzner_api_error",
    "wait_for_ssh": "ssh_error",
    "clone_repository": "repo_clone_error",
    "analyze_repository": "repo_analysis_error",
    "prepare_host": "remote_command_error",
    "render_artifacts_from_repo": "remote_command_error",
    "upload_artifacts": "upload_error",
    "start_containers": "remote_command_error",
    "configure_reverse_proxy": "nginx_error",
    "check_dns": "dns_error",
    "run_certbot": "certbot_error",
    "verify_https": "healthcheck_error",
    "healthcheck": "healthcheck_error",
}


def _host_port_for_project(project_id: int) -> int:
    # Reserve a stable per-project host port in a safe range to avoid clashes.
    return 10000 + (project_id % 50000)


class RemoteCommandError(RuntimeError):
    def __init__(self, step_name: str, failed_commands: list[dict], message: str):
        super().__init__(message)
        self.step_name = step_name
        self.failed_commands = failed_commands


def _pick_reusable_server(project: Project) -> Server | None:
    # Prefer explicitly assigned active server when it is usable.
    if project.active_server and project.active_server.ipv4 and project.active_server.status in {
        "running",
        "starting",
        "initializing",
    }:
        return project.active_server

    candidates = sorted(project.servers, key=lambda s: s.updated_at, reverse=True)
    for server in candidates:
        if server.ipv4 and server.status in {"running", "starting", "initializing"}:
            return server
    return None


def _pick_server_for_deployment(project: Project, deployment: Deployment) -> Server | None:
    if deployment.server and deployment.server.ipv4 and deployment.server.status in {"running", "starting", "initializing"}:
        return deployment.server
    return _pick_reusable_server(project)


def _ensure_steps(deployment: Deployment):
    if deployment.steps:
        return
    for index, name in enumerate(STEP_NAMES):
        deployment.steps.append(DeploymentStep(name=name, status="pending", order_index=index))
    db.session.commit()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _error_category(step_name: str, exc: Exception) -> str:
    if isinstance(exc, HetznerAPIError):
        return "hetzner_api_error"
    if isinstance(exc, (SSHWaitTimeoutError, CommandNotAllowedError)):
        return "ssh_error"
    if isinstance(exc, RemoteCommandError):
        return STEP_ERROR_CATEGORY.get(step_name, "remote_command_error")
    return STEP_ERROR_CATEGORY.get(step_name, "unknown_error")


def _error_metadata(step_name: str, exc: Exception) -> dict:
    data = {
        "error_category": _error_category(step_name, exc),
        "exception_type": type(exc).__name__,
        "step_name": step_name,
    }
    if isinstance(exc, RemoteCommandError):
        data["failed_commands"] = exc.failed_commands
    if isinstance(exc, HetznerAPIError):
        data["http_status"] = exc.status_code
    return data


def _find_or_create_step(deployment: Deployment, name: str) -> DeploymentStep:
    step = next((item for item in deployment.steps if item.name == name), None)
    if not step:
        step = DeploymentStep(deployment_id=deployment.id, name=name, order_index=len(deployment.steps))
        db.session.add(step)
    return step


def _start_step(deployment: Deployment, name: str, *, metadata: dict | None = None) -> DeploymentStep:
    step = _find_or_create_step(deployment, name)
    step.status = "running"
    step.started_at = _utcnow()
    step.finished_at = None
    step.stdout = None
    step.stderr = None
    step.exit_code = None
    step.json_details = metadata
    # Keep legacy fields in sync.
    step.output = None
    step.error_message = None
    db.session.commit()
    logger.info("deployment=%s step=%s status=running", deployment.id, name)
    return step


def _finish_step_success(
    deployment: Deployment,
    name: str,
    *,
    stdout: str | None = None,
    stderr: str | None = None,
    exit_code: int = 0,
    metadata: dict | None = None,
) -> DeploymentStep:
    step = _find_or_create_step(deployment, name)
    step.status = "success"
    if step.started_at is None:
        step.started_at = _utcnow()
    step.finished_at = _utcnow()
    step.stdout = stdout
    step.stderr = stderr
    step.exit_code = exit_code
    step.json_details = metadata
    # Keep legacy fields in sync.
    step.output = stdout
    step.error_message = stderr
    db.session.commit()
    logger.info("deployment=%s step=%s status=success exit_code=%s", deployment.id, name, exit_code)
    return step


def _finish_step_failed(
    deployment: Deployment,
    name: str,
    *,
    stdout: str | None = None,
    stderr: str | None = None,
    exit_code: int = 1,
    metadata: dict | None = None,
) -> DeploymentStep:
    step = _find_or_create_step(deployment, name)
    step.status = "failed"
    if step.started_at is None:
        step.started_at = _utcnow()
    step.finished_at = _utcnow()
    step.stdout = stdout
    step.stderr = stderr
    step.exit_code = exit_code
    resolved_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    error_analysis = analyze_deployment_failure(
        step_name=name,
        stdout=stdout,
        stderr=stderr,
        error_category=resolved_metadata.get("error_category"),
        exception_type=resolved_metadata.get("exception_type"),
    )
    resolved_metadata["error_analysis"] = error_analysis
    step.json_details = resolved_metadata
    # Keep legacy fields in sync.
    step.output = stdout
    step.error_message = stderr
    db.session.commit()
    logger.error("deployment=%s step=%s status=failed exit_code=%s stderr=%s", deployment.id, name, exit_code, (stderr or "")[:500])
    return step


def _latest_error_analysis(deployment: Deployment) -> dict | None:
    for step in sorted(deployment.steps, key=lambda s: (s.order_index, s.id), reverse=True):
        if step.status != "failed":
            continue
        details = step.json_details if isinstance(step.json_details, dict) else {}
        analysis = details.get("error_analysis") if isinstance(details, dict) else None
        if isinstance(analysis, dict) and analysis.get("error_type"):
            return analysis
    return None


def _assert_command_results_ok(step_name: str, results) -> None:
    failed = [item for item in results if getattr(item, "return_code", 0) != 0]
    if not failed:
        return

    failed_commands: list[dict] = []
    lines = [f"{step_name}: {len(failed)} command(s) failed"]
    for item in failed:
        lines.append(f"cmd={item.command}")
        lines.append(f"rc={item.return_code}")
        if item.stderr:
            lines.append(f"stderr={item.stderr.strip()}")
        failed_commands.append(
            {
                "command": item.command,
                "exit_code": item.return_code,
                "stdout": (item.stdout or "").strip(),
                "stderr": (item.stderr or "").strip(),
            }
        )
    raise RemoteCommandError(step_name=step_name, failed_commands=failed_commands, message="\n".join(lines))


def _serialize_command_results(results) -> tuple[str, str, int, dict]:
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    details: list[dict] = []
    max_exit_code = 0

    for item in results:
        rc = int(getattr(item, "return_code", 0) or 0)
        max_exit_code = max(max_exit_code, rc)
        cmd = getattr(item, "command", "")
        out = (getattr(item, "stdout", "") or "").rstrip()
        err = (getattr(item, "stderr", "") or "").rstrip()

        stdout_lines.append(f"cmd={cmd}")
        stdout_lines.append(f"exit_code={rc}")
        stdout_lines.append("stdout:")
        stdout_lines.append(out)
        stdout_lines.append("---")

        if err:
            stderr_lines.append(f"cmd={cmd}")
            stderr_lines.append(f"exit_code={rc}")
            stderr_lines.append("stderr:")
            stderr_lines.append(err)
            stderr_lines.append("---")

        details.append({
            "command": cmd,
            "exit_code": rc,
            "stdout": out,
            "stderr": err,
        })

    stdout_blob = "\n".join(stdout_lines).rstrip("-\n")
    stderr_blob = "\n".join(stderr_lines).rstrip("-\n")
    return stdout_blob, stderr_blob, max_exit_code, {"commands": details}


def _run_command_step(deployment: Deployment, name: str, command_runner):
    _start_step(deployment, name)
    try:
        results = command_runner()
        _assert_command_results_ok(name, results)
        stdout, stderr, exit_code, meta = _serialize_command_results(results)
        _finish_step_success(
            deployment,
            name,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            metadata=meta,
        )
        return results
    except Exception as exc:
        _finish_step_failed(
            deployment,
            name,
            stderr=str(exc),
            exit_code=1,
            metadata=_error_metadata(name, exc),
        )
        logger.exception("deployment=%s step=%s command step failed", deployment.id, name)
        raise


def _fail_running_steps(deployment: Deployment, exc: Exception) -> None:
    running_steps = [step for step in deployment.steps if step.status == "running"]
    for step in running_steps:
        _finish_step_failed(
            deployment,
            step.name,
            stderr=str(exc),
            exit_code=1,
            metadata=_error_metadata(step.name, exc),
        )


@shared_task(ignore_result=False)
def run_deployment_task(deployment_id: int):
    deployment = Deployment.query.get_or_404(deployment_id)
    project = Project.query.get_or_404(deployment.project_id)
    executor = DeploymentExecutor()
    repo_cloner = LocalRepoCloneService()
    repo_analyzer = RepoAnalyzer()

    _ensure_steps(deployment)
    deployment.status = "running"
    db.session.commit()

    ctx = PipelineContext(
        project_name=project.name,
        slug=project.slug,
        framework=project.framework or "flask",
        domain=project.domain,
        repository_url=project.repository.repo_url if project.repository else None,
        repository_branch=project.repository.branch if project.repository and project.repository.branch else project.branch,
        deployment_mode="repository" if (project.repository and project.repository.repo_url) else "fallback",
        project_environment=project.environment or "production",
        env_values={item.key: item.value for item in project.environment_variables},
        env_secret_keys={item.key for item in project.environment_variables if item.is_secret},
        host_port=_host_port_for_project(project.id),
        # is_update is set to True after we determine a live server already exists.
        is_update=False,
    )

    try:
        _start_step(deployment, "provision_server")
        try:
            existing_server = _pick_server_for_deployment(project, deployment)
            if existing_server:
                server = existing_server
                ctx.is_update = True  # Server already live → this is a code update, not initial deploy
                deployment.server_id = server.id
                project.active_server_id = server.id
                db.session.commit()
                _finish_step_success(
                    deployment,
                    "provision_server",
                    stdout=(
                        "action=reuse_existing_server\n"
                        f"server_id={server.id}\n"
                        f"provider_server_id={server.provider_server_id or '-'}\n"
                        f"name={server.name}\n"
                        f"status={server.status}\n"
                        f"ipv4={server.ipv4 or '-'}\n"
                        f"server_type={server.server_type}\n"
                        f"location={server.region}"
                    ),
                    exit_code=0,
                    metadata={"action": "reuse_existing_server", "server_id": server.id},
                )
            else:
                server, provisioned = executor.hetzner.create_server_for_project(
                    project=project,
                    deployment=deployment,
                    name=f"orbital-{ctx.slug}",
                    server_type=project.desired_server_type,
                    location=project.desired_location,
                    image=project.desired_image,
                )
                deployment.server_id = server.id
                project.active_server_id = server.id
                db.session.commit()
                _finish_step_success(
                    deployment,
                    "provision_server",
                    stdout=(
                        "action=provision_new_server\n"
                        f"server_id={server.id}\n"
                        f"provider_server_id={provisioned.provider_server_id}\n"
                        f"name={provisioned.name}\n"
                        f"status={provisioned.status}\n"
                        f"ipv4={provisioned.ipv4 or '-'}\n"
                        f"server_type={provisioned.server_type}\n"
                        f"location={provisioned.location}"
                    ),
                    exit_code=0,
                    metadata={"action": "provision_new_server", "server_id": server.id},
                )
        except Exception as exc:
            _finish_step_failed(
                deployment,
                "provision_server",
                stderr=str(exc),
                exit_code=1,
                metadata=_error_metadata("provision_server", exc),
            )
            logger.exception("deployment=%s step=provision_server failed", deployment.id)
            raise

        _start_step(deployment, "wait_for_ssh")
        try:
            wait_logs = executor.wait_for_ssh(server.ipv4 or "", max_attempts=20, delay_seconds=10)
        except SSHWaitTimeoutError as exc:
            _finish_step_failed(
                deployment,
                "wait_for_ssh",
                stdout="\n".join(exc.attempts_log),
                stderr=str(exc),
                exit_code=1,
                metadata=_error_metadata("wait_for_ssh", exc),
            )
            raise
        except Exception as exc:
            _finish_step_failed(
                deployment,
                "wait_for_ssh",
                stderr=str(exc),
                exit_code=1,
                metadata=_error_metadata("wait_for_ssh", exc),
            )
            logger.exception("deployment=%s step=wait_for_ssh failed", deployment.id)
            raise
        else:
            _finish_step_success(
                deployment,
                "wait_for_ssh",
                stdout="\n".join(wait_logs),
                exit_code=0,
            )

        _start_step(deployment, "clone_repository")
        try:
            if ctx.repository_url:
                clone_result = repo_cloner.clone(
                    repo_url=ctx.repository_url,
                    branch=ctx.repository_branch,
                    deployment_id=deployment.id,
                    access_token=project.repository.access_token if project.repository else None,
                )
                ctx.local_repository_path = clone_result.local_path

                stdout, stderr, exit_code, details = _serialize_command_results(clone_result.command_results)
                _finish_step_success(
                    deployment,
                    "clone_repository",
                    stdout=f"deployment_mode=repository\n{stdout}",
                    stderr=stderr,
                    exit_code=exit_code,
                    metadata={
                        "deployment_mode": "repository",
                        "local_path": clone_result.local_path,
                        "branch": clone_result.branch,
                        "commit_hash": clone_result.commit_hash,
                        **details,
                    },
                )
            else:
                ctx.deployment_mode = "fallback"
                _finish_step_success(
                    deployment,
                    "clone_repository",
                    stdout="deployment_mode=fallback\nRepository nicht hinterlegt: clone_repository uebersprungen.",
                    exit_code=0,
                    metadata={
                        "deployment_mode": "fallback",
                        "skipped": True,
                        "reason": "repository_not_configured",
                    },
                )
        except Exception as exc:
            _finish_step_failed(
                deployment,
                "clone_repository",
                stderr=str(exc),
                exit_code=1,
                metadata=_error_metadata("clone_repository", exc),
            )
            logger.exception("deployment=%s step=clone_repository failed", deployment.id)
            raise

        _start_step(deployment, "analyze_repository")
        try:
            if ctx.local_repository_path:
                analysis = repo_analyzer.analyze_path(ctx.local_repository_path)
                if analysis.detected_stack != "unknown":
                    ctx.framework = analysis.framework
                if analysis.port:
                    ctx.app_port = analysis.port

                _finish_step_success(
                    deployment,
                    "analyze_repository",
                    stdout=(
                        f"deployment_mode={ctx.deployment_mode}\n"
                        f"detected_stack={analysis.detected_stack}\n"
                        f"confidence={analysis.confidence}\n"
                        f"framework={analysis.framework}\n"
                        f"relevant_files={', '.join(analysis.relevant_files) if analysis.relevant_files else '-'}"
                    ),
                    exit_code=0,
                    metadata={
                        "deployment_mode": ctx.deployment_mode,
                        **analysis.to_dict(),
                    },
                )
            else:
                _finish_step_success(
                    deployment,
                    "analyze_repository",
                    stdout="deployment_mode=fallback\nRepository-Analyse uebersprungen (kein lokaler Clone).",
                    exit_code=0,
                    metadata={
                        "deployment_mode": "fallback",
                        "detected_stack": "fallback",
                        "confidence": 1.0,
                        "relevant_files": [],
                        "skipped": True,
                    },
                )
        except Exception as exc:
            _finish_step_failed(
                deployment,
                "analyze_repository",
                stderr=str(exc),
                exit_code=1,
                metadata=_error_metadata("analyze_repository", exc),
            )
            logger.exception("deployment=%s step=analyze_repository failed", deployment.id)
            raise

        if ctx.is_update:
            # Verify Docker AND nginx are actually installed before skipping prepare_host.
            # A server may be marked "running" in the DB but still be missing software
            # (e.g. freshly provisioned, re-assigned, or partially set up server).
            docker_check = executor.ssh.run_one(server.ipv4 or "127.0.0.1", "docker ps")
            nginx_check = executor.ssh.run_one(server.ipv4 or "127.0.0.1", "nginx -t")
            host_ready = docker_check.return_code == 0 and nginx_check.return_code == 0
            if host_ready:
                _finish_step_success(
                    deployment,
                    "prepare_host",
                    stdout="action=skipped\nreason=server_already_live_update_deploy\ndocker_check=ok\nnginx_check=ok",
                    exit_code=0,
                    metadata={"action": "skipped", "reason": "update_deploy"},
                )
            else:
                # Docker or nginx not ready – run full host preparation.
                logger.info(
                    "deployment=%s server=%s host not ready (docker_rc=%s nginx_rc=%s), running prepare_host",
                    deployment.id, server.id, docker_check.return_code, nginx_check.return_code,
                )
                _run_command_step(deployment, "prepare_host", lambda: executor.prepare_host(server.ipv4 or "127.0.0.1"))
        else:
            _run_command_step(deployment, "prepare_host", lambda: executor.prepare_host(server.ipv4 or "127.0.0.1"))

        _start_step(deployment, "render_artifacts_from_repo")
        try:
            rendered = executor.render_artifacts_from_repo(ctx)
            _finish_step_success(
                deployment,
                "render_artifacts_from_repo",
                stdout=(
                    f"deployment_mode={ctx.deployment_mode}\n"
                    f"Dockerfile, compose and nginx config rendered for {ctx.framework}\n"
                    f"build_context={ctx.local_repository_path or '-'}\n"
                    f"container_port={ctx.app_port}\n"
                    f"host_port={ctx.host_port}\n"
                    f"project_env={ctx.project_environment}\n"
                    f"env_keys={len(ctx.env_values)} (secret_keys={len(ctx.env_secret_keys)})\n"
                    "deployment_structure=/opt/orbital/<slug>/{Dockerfile,docker-compose.yml,nginx.conf,repo/}"
                ),
                exit_code=0,
                metadata={
                    "deployment_mode": ctx.deployment_mode,
                    **rendered.metadata,
                    "repository_local_path": ctx.local_repository_path,
                    # ENV values flow into /opt/orbital/<slug>/.env during upload_artifacts.
                    # We only persist key names/counts in metadata to avoid leaking secrets.
                    "env_injection": {
                        "project_environment": ctx.project_environment,
                        "env_key_count": len(ctx.env_values),
                        "env_keys": sorted(ctx.env_values.keys()),
                        "secret_key_count": len(ctx.env_secret_keys),
                        "secret_keys": sorted(ctx.env_secret_keys),
                        "target_env_file": "/opt/orbital/<slug>/.env",
                    },
                    "deployment_layout": {
                        "root": "/opt/orbital/<slug>",
                        "artifact_files": ["Dockerfile", "docker-compose.yml", "nginx.conf", ".env"],
                        "repo_snapshot_dir": "repo",
                    },
                },
            )
        except Exception as exc:
            _finish_step_failed(
                deployment,
                "render_artifacts_from_repo",
                stderr=str(exc),
                exit_code=1,
                metadata=_error_metadata("render_artifacts_from_repo", exc),
            )
            logger.exception("deployment=%s step=render_artifacts_from_repo failed", deployment.id)
            raise

        _run_command_step(
            deployment,
            "upload_artifacts",
            lambda: executor.upload_artifacts(server.ipv4 or "127.0.0.1", ctx, rendered),
        )

        if ctx.is_update:
            # Update: rebuild image + restart container IN-PLACE.
            # Named Docker volumes (app DB / SQLite) are NOT removed.
            _run_command_step(
                deployment,
                "start_containers",
                lambda: executor.update_containers(server.ipv4 or "127.0.0.1", ctx),
            )
        else:
            _run_command_step(
                deployment,
                "start_containers",
                lambda: executor.start_containers(server.ipv4 or "127.0.0.1", ctx),
            )

        # configure_reverse_proxy is idempotent (ln -sf / nginx reload) and must
        # run on every deploy – including updates – to handle servers whose first
        # deploy failed before reaching this step.
        _run_command_step(
            deployment,
            "configure_reverse_proxy",
            lambda: executor.configure_reverse_proxy(server.ipv4 or "127.0.0.1", ctx),
        )

        if ctx.is_update:
            # DNS and SSL are already configured on update deploys – skip.
            for skip_step in ("check_dns", "run_certbot", "verify_https"):
                _finish_step_success(
                    deployment,
                    skip_step,
                    stdout=f"action=skipped\nreason=update_deploy\nserver_already_configured",
                    exit_code=0,
                    metadata={"action": "skipped", "reason": "update_deploy"},
                )

        if not ctx.is_update:
            _start_step(deployment, "check_dns")
            dns_result = executor.check_dns(ctx.domain, server.ipv4)
            dns_output = (
                f"resolved_ip={dns_result.resolved_ip}\n"
                f"expected_ip={dns_result.expected_ip}\n"
                f"matches={dns_result.matches}"
            )
            if not ctx.domain:
                _finish_step_success(
                    deployment,
                    "check_dns",
                    stdout="resolved_ip=-\nexpected_ip=-\nmatches=False\ninfo=no_domain_configured",
                    exit_code=0,
                    metadata={"matches": False, "info": "no_domain_configured"},
                )
                _finish_step_success(
                    deployment,
                    "run_certbot",
                    stdout="SSL skipped: no domain configured.",
                    exit_code=0,
                    metadata={"skipped": True, "reason": "no_domain_configured"},
                )
                _finish_step_success(
                    deployment,
                    "verify_https",
                    stdout="HTTPS verification skipped: no domain configured.",
                    exit_code=0,
                    metadata={"skipped": True, "reason": "no_domain_configured"},
                )
            elif not dns_result.matches:
                mismatch_message = (
                    "DNS-Pruefung fehlgeschlagen: Domain zeigt nicht auf den Zielserver.\n"
                    f"resolved_ip={dns_result.resolved_ip}\n"
                    f"expected_ip={dns_result.expected_ip}\n"
                    "Bitte A-Record korrigieren und Deployment erneut starten."
                )
                _finish_step_failed(
                    deployment,
                    "check_dns",
                    stdout=dns_output,
                    stderr=mismatch_message,
                    exit_code=1,
                    metadata={
                        "error_category": "dns_error",
                        "matches": False,
                        "resolved_ip": dns_result.resolved_ip,
                        "expected_ip": dns_result.expected_ip,
                    },
                )
                _finish_step_failed(
                    deployment,
                    "run_certbot",
                    stdout=(
                        "certbot nicht gestartet, weil DNS-Pruefung fehlgeschlagen ist.\n"
                        f"resolved_ip={dns_result.resolved_ip}\n"
                        f"expected_ip={dns_result.expected_ip}\n"
                        "matches=False"
                    ),
                    stderr="certbot skipped due to DNS mismatch",
                    exit_code=1,
                    metadata={"error_category": "certbot_error", "skipped": True, "reason": "dns_mismatch"},
                )
                _finish_step_failed(
                    deployment,
                    "verify_https",
                    stdout="HTTPS verification skipped, because certbot was not executed.",
                    stderr="verify_https skipped due to DNS mismatch",
                    exit_code=1,
                    metadata={"error_category": "healthcheck_error", "skipped": True, "reason": "dns_mismatch"},
                )
                raise RuntimeError(mismatch_message)
            else:
                _finish_step_success(
                    deployment,
                    "check_dns",
                    stdout=dns_output,
                    exit_code=0,
                    metadata={"matches": True, "resolved_ip": dns_result.resolved_ip, "expected_ip": dns_result.expected_ip},
                )
                _run_command_step(deployment, "run_certbot", lambda: executor.run_certbot(server.ipv4 or "127.0.0.1", ctx.domain))
                _run_command_step(deployment, "verify_https", lambda: executor.verify_https(server.ipv4 or "127.0.0.1", ctx.domain))

        _run_command_step(deployment, "healthcheck", lambda: executor.verify_deployment(server.ipv4 or "127.0.0.1", ctx))

        clone_step = next((step for step in deployment.steps if step.name == "clone_repository"), None)
        clone_meta = clone_step.json_details if clone_step and isinstance(clone_step.json_details, dict) else {}

        deployment.status = "success"
        deployment.successful = True
        deployment.successful_at = datetime.now(timezone.utc)
        if not deployment.commit_sha:
            deployment.commit_sha = clone_meta.get("commit_hash")
        deployment.source_snapshot_path = clone_meta.get("local_path") or ctx.local_repository_path
        deployment.artifact_snapshot_path = f"/opt/orbital/{ctx.slug}"
        project.status = "live"
        deployment.output = rendered.compose
        db.session.add(
            ActivityLog(
                project_id=project.id,
                action="deployment.completed",
                actor="system",
                message=(
                    "Deployment finished "
                    f"(mode={ctx.deployment_mode}, commit={deployment.commit_sha or '-'}, "
                    f"artifact_path={deployment.artifact_snapshot_path})"
                ),
            )
        )
        db.session.commit()

        return deployment.to_dict(include_steps=True)
    except Exception as exc:
        _fail_running_steps(deployment, exc)
        deployment.status = "failed"
        deployment.successful = False
        analysis = _latest_error_analysis(deployment)
        if analysis:
            deployment.error_message = (
                f"{exc}\n\n"
                f"Automatische Analyse: {analysis.get('error_type', '-')}; "
                f"Ursache: {analysis.get('probable_cause', '-')}; "
                f"Fix: {analysis.get('suggested_fix', '-')}"
            )
        else:
            deployment.error_message = str(exc)
        db.session.add(
            ActivityLog(project_id=project.id, action="deployment.failed", actor="system", message=str(exc))
        )
        db.session.commit()
        logger.exception("deployment=%s failed", deployment.id)
        raise
