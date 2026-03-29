from celery import shared_task
from datetime import datetime, timezone
import logging
import time
from types import SimpleNamespace
from typing import Any

from app.extensions import db
from app.models import ActivityLog, Deployment, DeploymentStep, Project, Server
from app.services.error_analysis import analyze_deployment_errors, analyze_deployment_failure
from app.services.project_state_engine import (
    compute_project_runtime_state,
    mark_deployment_as_active,
    run_project_healthcheck,
)
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
    "start_containers": "docker_build_error",
    "configure_reverse_proxy": "nginx_error",
    "check_dns": "dns_error",
    "run_certbot": "certbot_error",
    "verify_https": "healthcheck_error",
    "healthcheck": "healthcheck_error",
}

INFRA_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "hetzner_api_error",
        "ssh_error",
        "remote_command_error",
        "upload_error",
        "repo_clone_error",
        "repo_analysis_error",
        "dns_error",
        "timeout_error",
        "unknown_error",
    }
)

BUILD_ERROR_CATEGORIES: frozenset[str] = frozenset({"docker_build_error", "build_error"})

RUNTIME_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "docker_runtime_error",
        "nginx_error",
        "certbot_error",
        "healthcheck_error",
        "runtime_error",
    }
)

# ---------------------------------------------------------------------------
# Retry configuration per step name
# max_retries: how many additional attempts after the first failure
# delay: seconds to wait before the next attempt
# backoff: multiply delay by this factor on each subsequent attempt
# ---------------------------------------------------------------------------
STEP_RETRY_CONFIG: dict[str, dict] = {
    "prepare_host":           {"max_retries": 2, "delay": 10, "backoff": 1.5},
    "start_containers":       {"max_retries": 2, "delay": 15, "backoff": 2.0},
    "configure_reverse_proxy":{"max_retries": 3, "delay": 5,  "backoff": 1.5},
    "upload_artifacts":       {"max_retries": 2, "delay": 5,  "backoff": 1.0},
    "run_certbot":            {"max_retries": 1, "delay": 10, "backoff": 1.0},
    "verify_https":           {"max_retries": 2, "delay": 10, "backoff": 1.5},
    "healthcheck":            {"max_retries": 2, "delay": 10, "backoff": 2.0},
}

# Steps that should NOT abort the whole deployment when they ultimately fail
OPTIONAL_STEPS: frozenset[str] = frozenset({"run_certbot", "verify_https"})


def _host_port_for_project(project_id: int) -> int:
    # Reserve a stable per-project host port in a safe range to avoid clashes.
    return 10000 + (project_id % 50000)


class RemoteCommandError(RuntimeError):
    def __init__(self, step_name: str, failed_commands: list[dict], message: str):
        super().__init__(message)
        self.step_name = step_name
        self.failed_commands = failed_commands


class DeploymentTimeoutError(TimeoutError):
    """Raised when a deployment step exceeds its allowed wall-clock budget."""

    def __init__(self, step_name: str, timeout_seconds: int, message: str):
        super().__init__(message)
        self.step_name = step_name
        self.timeout_seconds = timeout_seconds


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


def _event_timestamp() -> str:
    return _utcnow().isoformat()


def _default_step_details() -> dict[str, Any]:
    return {
        "error_type": None,
        "error_category": None,
        "events": [],
    }


def _normalized_step_details(
    existing: dict | None = None,
    extra: dict | None = None,
    *,
    preserve_error_fields: bool = True,
) -> dict[str, Any]:
    details: dict[str, Any] = _default_step_details()
    if isinstance(existing, dict):
        details.update(existing)
    if isinstance(extra, dict):
        details.update(extra)

    if not isinstance(details.get("events"), list):
        details["events"] = []

    if not preserve_error_fields:
        details["error_type"] = None
        details["error_category"] = None

    return details


def _new_event(level: str, message: str, source: str, context: dict | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "timestamp": _event_timestamp(),
        "level": level,
        "message": message,
        "source": source,
    }
    if context:
        event["context"] = context
    return event


def _classify_error_type(step_name: str, exc: Exception, *, error_category: str | None = None) -> str:
    category = (error_category or _error_category(step_name, exc)).strip().lower()
    if category in BUILD_ERROR_CATEGORIES:
        return "build_error"
    if category in RUNTIME_ERROR_CATEGORIES:
        return "runtime_error"
    if category in INFRA_ERROR_CATEGORIES:
        return "infra_error"
    if "build" in category:
        return "build_error"
    if "runtime" in category:
        return "runtime_error"
    return "infra_error"


def _error_category(step_name: str, exc: Exception) -> str:
    if isinstance(exc, HetznerAPIError):
        return "hetzner_api_error"
    if isinstance(exc, DeploymentTimeoutError):
        return "timeout_error"
    if isinstance(exc, (SSHWaitTimeoutError, CommandNotAllowedError)):
        return "ssh_error"
    if isinstance(exc, RemoteCommandError):
        category = STEP_ERROR_CATEGORY.get(step_name, "remote_command_error")
        # Refine docker step categories based on which command failed
        if step_name == "start_containers" and isinstance(exc, RemoteCommandError):
            cmds = " ".join(c.get("command", "") for c in exc.failed_commands)
            if "up" in cmds or "build" in cmds:
                return "docker_build_error"
            return "docker_runtime_error"
        if step_name == "configure_reverse_proxy":
            return "nginx_error"
        return category
    return STEP_ERROR_CATEGORY.get(step_name, "unknown_error")


def _error_metadata(step_name: str, exc: Exception) -> dict:
    error_category = _error_category(step_name, exc)
    data = {
        "error_type": _classify_error_type(step_name, exc, error_category=error_category),
        "error_category": error_category,
        "exception_type": type(exc).__name__,
        "step_name": step_name,
    }
    if isinstance(exc, RemoteCommandError):
        data["failed_commands"] = exc.failed_commands
    if isinstance(exc, HetznerAPIError):
        data["http_status"] = exc.status_code
    if isinstance(exc, DeploymentTimeoutError):
        data["timeout_seconds"] = exc.timeout_seconds
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
    step.json_details = _normalized_step_details(step.json_details, metadata, preserve_error_fields=False)
    step.json_details["events"].append(_new_event("info", "Step gestartet", source="step_runner.start"))
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
    step.json_details = _normalized_step_details(step.json_details, metadata, preserve_error_fields=False)
    step.json_details["events"].append(
        _new_event(
            "info",
            "Step erfolgreich abgeschlossen",
            source="step_runner.finish",
            context={"status": "success", "exit_code": exit_code},
        )
    )
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
    resolved_metadata = _normalized_step_details(step.json_details, metadata)
    resolved_metadata["error_type"] = resolved_metadata.get("error_type") or _classify_error_type(
        name,
        RuntimeError(stderr or "step_failed"),
        error_category=resolved_metadata.get("error_category"),
    )
    error_analysis = analyze_deployment_failure(
        step_name=name,
        stdout=stdout,
        stderr=stderr,
        error_category=resolved_metadata.get("error_category"),
        exception_type=resolved_metadata.get("exception_type"),
    )
    resolved_metadata["error_analysis"] = error_analysis
    resolved_metadata["events"].append(
        _new_event(
            "error",
            "Step fehlgeschlagen",
            source="step_runner.finish",
            context={
                "status": "failed",
                "exit_code": exit_code,
                "error_type": resolved_metadata.get("error_type"),
                "error_category": resolved_metadata.get("error_category"),
                "exception_type": resolved_metadata.get("exception_type"),
            },
        )
    )
    resolved_metadata["error_details"] = {
        "message": stderr or "Unbekannter Fehler",
        "error_type": resolved_metadata.get("error_type"),
        "error_category": resolved_metadata.get("error_category"),
        "exception_type": resolved_metadata.get("exception_type"),
        "exit_code": exit_code,
    }
    step.json_details = resolved_metadata
    # Keep legacy fields in sync.
    step.output = stdout
    step.error_message = stderr
    db.session.commit()
    logger.error("deployment=%s step=%s status=failed exit_code=%s stderr=%s", deployment.id, name, exit_code, (stderr or "")[:500])
    return step


def _latest_error_analysis(deployment: Deployment) -> dict | None:
    if isinstance(deployment.error_analysis_json, dict) and deployment.error_analysis_json.get("error_type"):
        return deployment.error_analysis_json

    for step in sorted(deployment.steps, key=lambda s: (s.order_index, s.id), reverse=True):
        if step.status != "failed":
            continue
        details = step.json_details if isinstance(step.json_details, dict) else {}
        analysis = details.get("error_analysis") if isinstance(details, dict) else None
        if isinstance(analysis, dict) and analysis.get("error_type"):
            return analysis
    return None


def log_step_event(
    deployment: Deployment,
    step_name: str,
    *,
    level: str,
    message: str,
    source: str,
    context: dict | None = None,
) -> None:
    step = _find_or_create_step(deployment, step_name)
    details = _normalized_step_details(step.json_details)
    details["events"].append(_new_event(level=level, message=message, source=source, context=context))
    step.json_details = details
    db.session.commit()


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


def _run_step(
    deployment: Deployment,
    name: str,
    runner,
    *,
    success_mapper,
    start_metadata: dict | None = None,
) -> Any:
    """Run a step with unified start/success/failed transitions."""
    _start_step(deployment, name, metadata=start_metadata)
    try:
        payload = runner()
        success = success_mapper(payload)
        _finish_step_success(
            deployment,
            name,
            stdout=success.get("stdout"),
            stderr=success.get("stderr"),
            exit_code=success.get("exit_code", 0),
            metadata=success.get("metadata"),
        )
        return payload
    except Exception as exc:
        _finish_step_failed(
            deployment,
            name,
            stderr=str(exc),
            exit_code=1,
            metadata=_error_metadata(name, exc),
        )
        logger.exception("deployment=%s step=%s failed", deployment.id, name)
        raise


def _run_command_step(
    deployment: Deployment,
    name: str,
    command_runner,
    *,
    max_retries: int | None = None,
    retry_delay: float | None = None,
    backoff: float | None = None,
    raises: bool = True,
):
    """Execute a deployment step with automatic retry on failure.

    Retry config is taken from STEP_RETRY_CONFIG unless overridden via kwargs.
    Each failed attempt is logged in the step stdout so the UI shows full history.

    Args:
        raises: If False the step failure is recorded but no exception is raised
                (use for OPTIONAL_STEPS like run_certbot/verify_https).
    """
    cfg = STEP_RETRY_CONFIG.get(name, {})
    total_attempts = 1 + (max_retries if max_retries is not None else cfg.get("max_retries", 0))
    delay = retry_delay if retry_delay is not None else cfg.get("delay", 5)
    bk = backoff if backoff is not None else cfg.get("backoff", 1.0)

    _start_step(deployment, name)
    log_step_event(
        deployment,
        name,
        level="info",
        message="Kommando-Step gestartet",
        source="step_runner.command",
        context={"step_name": name, "total_attempts": total_attempts},
    )

    attempt_logs: list[str] = []
    last_exc: Exception | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            results = command_runner()
            _assert_command_results_ok(name, results)
            stdout, stderr, exit_code, meta = _serialize_command_results(results)

            # Prepend attempt history to stdout so it's always visible in the UI
            prefix = f"attempts={attempt}/{total_attempts}\n"
            if len(attempt_logs) > 0:
                prefix += "retry_history:\n" + "\n".join(attempt_logs) + "\n---\n"

            _finish_step_success(
                deployment,
                name,
                stdout=prefix + (stdout or ""),
                stderr=stderr,
                exit_code=exit_code,
                metadata={**meta, "attempts": attempt, "total_attempts": total_attempts},
            )
            return results

        except Exception as exc:
            last_exc = exc
            attempt_log = (
                f"attempt={attempt}/{total_attempts} "
                f"error={type(exc).__name__}: {str(exc)[:300]}"
            )
            attempt_logs.append(attempt_log)
            log_step_event(
                deployment,
                name,
                level="warning",
                message="Step-Versuch fehlgeschlagen",
                source="step_runner.command",
                context={
                    "attempt": attempt,
                    "total_attempts": total_attempts,
                    "exception_type": type(exc).__name__,
                    "error_preview": str(exc)[:300],
                },
            )
            logger.warning(
                "deployment=%s step=%s %s",
                deployment.id, name, attempt_log,
            )

            if attempt < total_attempts:
                sleep_time = delay * (bk ** (attempt - 1))
                logger.info(
                    "deployment=%s step=%s retrying in %.1fs (attempt %d/%d)",
                    deployment.id, name, sleep_time, attempt + 1, total_attempts,
                )
                log_step_event(
                    deployment,
                    name,
                    level="info",
                    message="Retry eingeplant",
                    source="step_runner.command",
                    context={
                        "next_attempt": attempt + 1,
                        "sleep_seconds": sleep_time,
                        "backoff": bk,
                    },
                )
                time.sleep(sleep_time)

    # All attempts exhausted
    assert last_exc is not None
    retry_summary = "\n".join(attempt_logs)
    _finish_step_failed(
        deployment,
        name,
        stdout=f"attempts={total_attempts}/{total_attempts} – alle Versuche fehlgeschlagen\n{retry_summary}",
        stderr=str(last_exc),
        exit_code=1,
        metadata={
            **_error_metadata(name, last_exc),
            "attempts": total_attempts,
            "total_attempts": total_attempts,
            "retry_history": attempt_logs,
        },
    )
    logger.error(
        "deployment=%s step=%s FAILED after %d attempt(s): %s",
        deployment.id, name, total_attempts, last_exc,
    )
    if raises:
        raise last_exc


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

    # Prevent concurrent deployments on the same project
    already_running = (
        Deployment.query.filter(
            Deployment.project_id == deployment.project_id,
            Deployment.status == "running",
            Deployment.id != deployment_id,
        ).first()
    )
    if already_running:
        msg = (
            f"Abgebrochen: Deployment #{already_running.id} läuft bereits auf diesem Projekt. "
            "Bitte warte auf den Abschluss oder breche es manuell ab."
        )
        deployment.status = "failed"
        deployment.error_message = msg
        db.session.commit()
        raise RuntimeError(msg)

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

        def _clone_repository_runner() -> dict[str, Any]:
            if ctx.repository_url:
                clone_result = repo_cloner.clone(
                    repo_url=ctx.repository_url,
                    branch=ctx.repository_branch,
                    deployment_id=deployment.id,
                    access_token=project.repository.access_token if project.repository else None,
                )
                ctx.local_repository_path = clone_result.local_path
                stdout, stderr, exit_code, details = _serialize_command_results(clone_result.command_results)
                return {
                    "stdout": f"deployment_mode=repository\n{stdout}",
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "metadata": {
                        "deployment_mode": "repository",
                        "local_path": clone_result.local_path,
                        "branch": clone_result.branch,
                        "commit_hash": clone_result.commit_hash,
                        **details,
                    },
                }

            ctx.deployment_mode = "fallback"
            return {
                "stdout": "deployment_mode=fallback\nRepository nicht hinterlegt: clone_repository uebersprungen.",
                "exit_code": 0,
                "metadata": {
                    "deployment_mode": "fallback",
                    "skipped": True,
                    "reason": "repository_not_configured",
                },
            }

        _run_step(
            deployment,
            "clone_repository",
            _clone_repository_runner,
            success_mapper=lambda payload: payload,
        )

        def _analyze_repository_runner() -> dict[str, Any]:
            if ctx.local_repository_path:
                analysis = repo_analyzer.analyze_path(ctx.local_repository_path)
                if analysis.detected_stack != "unknown":
                    ctx.framework = analysis.framework
                if analysis.port:
                    ctx.app_port = analysis.port
                return {
                    "stdout": (
                        f"deployment_mode={ctx.deployment_mode}\n"
                        f"detected_stack={analysis.detected_stack}\n"
                        f"confidence={analysis.confidence}\n"
                        f"framework={analysis.framework}\n"
                        f"relevant_files={', '.join(analysis.relevant_files) if analysis.relevant_files else '-'}"
                    ),
                    "exit_code": 0,
                    "metadata": {
                        "deployment_mode": ctx.deployment_mode,
                        **analysis.to_dict(),
                    },
                }

            return {
                "stdout": "deployment_mode=fallback\nRepository-Analyse uebersprungen (kein lokaler Clone).",
                "exit_code": 0,
                "metadata": {
                    "deployment_mode": "fallback",
                    "detected_stack": "fallback",
                    "confidence": 1.0,
                    "relevant_files": [],
                    "skipped": True,
                },
            }

        _run_step(
            deployment,
            "analyze_repository",
            _analyze_repository_runner,
            success_mapper=lambda payload: payload,
        )

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

        # DNS check and SSL must run on every deploy (initial AND update).
        # Skipping on update caused false "success" when domain pointed to wrong IP.
        dns_result_holder: dict[str, Any] = {}

        def _check_dns_runner() -> dict[str, Any]:
            dns_result = executor.check_dns(ctx.domain, server.ipv4)
            dns_result_holder["value"] = dns_result
            return {
                "stdout": (
                    f"resolved_ip={dns_result.resolved_ip}\n"
                    f"expected_ip={dns_result.expected_ip}\n"
                    f"matches={dns_result.matches}"
                ),
                "exit_code": 0,
                "metadata": {
                    "matches": dns_result.matches,
                    "resolved_ip": dns_result.resolved_ip,
                    "expected_ip": dns_result.expected_ip,
                },
            }

        if ctx.domain:
            _run_step(
                deployment,
                "check_dns",
                _check_dns_runner,
                success_mapper=lambda payload: payload,
            )
            dns_result = dns_result_holder["value"]
        else:
            dns_result = SimpleNamespace(resolved_ip="-", expected_ip=server.ipv4 or "-", matches=False)

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
            _run_command_step(deployment, "run_certbot", lambda: executor.run_certbot(server.ipv4 or "127.0.0.1", ctx.domain), raises=False)
            _run_command_step(deployment, "verify_https", lambda: executor.verify_https(server.ipv4 or "127.0.0.1", ctx.domain), raises=False)

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
        # Mark this deployment as active version and persist runtime-derived fields.
        mark_deployment_as_active(project, deployment, commit=False)

        # Persist a runtime healthcheck for the newly activated version.
        run_project_healthcheck(project, deployment=deployment, commit=False)

        # Compute runtime status from active version + latest health information.
        runtime_state = compute_project_runtime_state(project, commit=False)
        db.session.add(
            ActivityLog(
                project_id=project.id,
                action="deployment.completed",
                actor="system",
                message=(
                    "Deployment finished "
                    f"(mode={ctx.deployment_mode}, commit={deployment.commit_sha or '-'}, "
                    f"artifact_path={deployment.artifact_snapshot_path}, "
                    f"runtime_status={runtime_state.current_runtime_status})"
                ),
            )
        )
        db.session.commit()

        return deployment.to_dict(include_steps=True)
    except Exception as exc:
        _fail_running_steps(deployment, exc)
        deployment.status = "failed"
        deployment.successful = False

        # Error Analysis Engine v2: deployment-wide classification with primary/secondary errors.
        deployment.error_analysis_json = analyze_deployment_errors(deployment)

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

        # Recompute project runtime state after failed deployment.
        compute_project_runtime_state(project)
        logger.exception("deployment=%s failed", deployment.id)
        raise
