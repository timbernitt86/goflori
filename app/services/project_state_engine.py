from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time

import requests

from app.extensions import db
from app.models import Deployment, Project, ProjectHealthCheck, Server


RUNTIME_RUNNING = "running"
RUNTIME_FAILED = "failed"
RUNTIME_DEGRADED = "degraded"


@dataclass
class ProjectRuntimeState:
    current_runtime_status: str
    last_successful_deployment_id: int | None
    active_deployment_id: int | None
    active_version: str | None
    active_source_reference: str | None
    last_healthcheck_at: datetime | None
    reason: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _deployment_sort_time(deployment: Deployment) -> datetime:
    return deployment.successful_at or deployment.updated_at or deployment.created_at


def get_last_successful_deployment(project: Project) -> Deployment | None:
    if project.last_successful_deployment and project.last_successful_deployment.project_id == project.id:
        return project.last_successful_deployment

    return (
        Deployment.query.filter(
            Deployment.project_id == project.id,
            (Deployment.successful.is_(True)) | (Deployment.status == "success"),
        )
        .order_by(Deployment.successful_at.desc(), Deployment.updated_at.desc(), Deployment.id.desc())
        .first()
    )


def _latest_deployment(project: Project) -> Deployment | None:
    return (
        Deployment.query.filter(Deployment.project_id == project.id)
        .order_by(Deployment.created_at.desc(), Deployment.id.desc())
        .first()
    )


def _latest_healthcheck(project: Project) -> ProjectHealthCheck | None:
    return (
        ProjectHealthCheck.query.filter(ProjectHealthCheck.project_id == project.id)
        .order_by(ProjectHealthCheck.checked_at.desc(), ProjectHealthCheck.id.desc())
        .first()
    )


def _active_server_for_project(project: Project, deployment: Deployment | None = None) -> Server | None:
    if project.active_server and project.active_server.ipv4:
        return project.active_server
    if deployment and deployment.server and deployment.server.ipv4:
        return deployment.server
    servers = sorted(project.servers, key=lambda s: s.updated_at, reverse=True)
    for server in servers:
        if server.ipv4:
            return server
    return None


def _target_url(project: Project, deployment: Deployment | None = None) -> str | None:
    domain = (project.domain or "").strip()
    if domain:
        return f"https://{domain}"

    server = _active_server_for_project(project, deployment)
    if server and server.ipv4:
        return f"http://{server.ipv4}"
    return None


def _has_runtime_warnings(active_deployment: Deployment | None) -> bool:
    if not active_deployment:
        return False

    for step in active_deployment.steps:
        if step.name in {"run_certbot", "verify_https"} and step.status == "failed":
            return True
    return False


def _humanize_healthcheck_failure(raw_error: str, target_url: str) -> tuple[str, str]:
    error_text = (raw_error or "").strip()
    lower = error_text.lower()

    if "hostname mismatch" in lower or "certificate is not valid for" in lower:
        return (
            "HTTPS-Zertifikat passt nicht zur Domain (Hostname-Mismatch). "
            "Bitte DNS-Eintrag und Zertifikat (inkl. www/non-www) pruefen.",
            "tls_hostname_mismatch",
        )

    if "certificate verify failed" in lower or "ssl" in lower:
        return (
            "HTTPS-Zertifikat konnte nicht verifiziert werden. "
            "Bitte Zertifikat, Kette und Domain-Zuordnung pruefen.",
            "tls_verification_failed",
        )

    if "read timed out" in lower or "connect timeout" in lower or "timed out" in lower:
        return (
            f"Healthcheck-Timeout auf {target_url}. "
            "Die Anwendung antwortet zu langsam oder ist gerade nicht erreichbar.",
            "timeout",
        )

    if "name or service not known" in lower or "nodename nor servname" in lower or "name resolution" in lower:
        return (
            "Domain konnte nicht aufgeloest werden. Bitte DNS-Eintrag pruefen.",
            "dns_resolution_failed",
        )

    if "connection refused" in lower:
        return (
            "Verbindung abgelehnt. Der Dienst laeuft vermutlich nicht auf dem Zielserver/Port.",
            "connection_refused",
        )

    if "max retries exceeded" in lower:
        return (
            f"Healthcheck konnte {target_url} nicht erreichen (mehrere Verbindungsversuche fehlgeschlagen).",
            "max_retries_exceeded",
        )

    return (
        "Healthcheck fehlgeschlagen. Details im technischen Fehlerprotokoll.",
        "unknown_request_error",
    )


def _humanize_http_status_failure(status_code: int, target_url: str) -> tuple[str, str]:
    if 500 <= status_code <= 599:
        return (
            f"Anwendung ist erreichbar, liefert aber Serverfehler (HTTP {status_code}) auf {target_url}.",
            "http_5xx",
        )
    if 400 <= status_code <= 499:
        return (
            f"Anwendung ist erreichbar, liefert aber Clientfehler (HTTP {status_code}) auf {target_url}.",
            "http_4xx",
        )
    return (
        f"Healthcheck lieferte unerwarteten HTTP-Status {status_code} auf {target_url}.",
        "http_unexpected_status",
    )


def _friendly_health_reason(entry: ProjectHealthCheck) -> str:
    details = entry.details if isinstance(entry.details, dict) else {}
    user_msg = details.get("user_message") if isinstance(details, dict) else None
    if isinstance(user_msg, str) and user_msg.strip():
        return user_msg

    fallback = entry.error_message or "Healthcheck fehlgeschlagen"
    friendly, _ = _humanize_healthcheck_failure(fallback, entry.target_url)
    return friendly


def mark_deployment_as_active(project: Project, deployment: Deployment, *, commit: bool = True) -> None:
    if deployment.project_id != project.id:
        raise ValueError("Deployment does not belong to project")

    project.active_deployment_id = deployment.id
    project.active_version = deployment.commit_sha or deployment.source_snapshot_path
    project.active_source_reference = deployment.source_snapshot_path

    if deployment.successful or deployment.status == "success":
        project.last_successful_deployment_id = deployment.id

    if commit:
        db.session.commit()


def record_healthcheck_result(
    project: Project,
    *,
    target_url: str,
    success: bool,
    status_code: int | None,
    response_time_ms: int | None,
    error_message: str | None = None,
    deployment_id: int | None = None,
    details: dict | None = None,
    commit: bool = True,
) -> ProjectHealthCheck:
    checked_at = _utcnow()
    entry = ProjectHealthCheck(
        project_id=project.id,
        deployment_id=deployment_id,
        checked_at=checked_at,
        target_url=target_url,
        success=success,
        status_code=status_code,
        response_time_ms=response_time_ms,
        error_message=error_message,
        details=details,
    )
    db.session.add(entry)
    project.last_healthcheck_at = checked_at
    if commit:
        db.session.commit()
    return entry


def run_project_healthcheck(
    project: Project,
    *,
    deployment: Deployment | None = None,
    timeout_seconds: int = 8,
    commit: bool = True,
) -> ProjectHealthCheck:
    target = _target_url(project, deployment)
    if not target:
        return record_healthcheck_result(
            project,
            target_url="-",
            success=False,
            status_code=None,
            response_time_ms=None,
            error_message="Kein Healthcheck-Ziel vorhanden (Domain/Server-IP fehlt)",
            deployment_id=deployment.id if deployment else None,
            details={"reason": "no_target_url"},
            commit=commit,
        )

    started = time.perf_counter()
    try:
        response = requests.get(target, timeout=timeout_seconds, allow_redirects=True)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        success = 200 <= response.status_code < 400
        err_msg = None
        err_code = None
        if not success:
            err_msg, err_code = _humanize_http_status_failure(response.status_code, target)
        return record_healthcheck_result(
            project,
            target_url=target,
            success=success,
            status_code=response.status_code,
            response_time_ms=elapsed_ms,
            error_message=err_msg,
            deployment_id=deployment.id if deployment else None,
            details={
                "redirected": len(response.history) > 0,
                "error_code": err_code,
                "raw_status": response.status_code,
            },
            commit=commit,
        )
    except requests.RequestException as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        user_message, error_code = _humanize_healthcheck_failure(str(exc), target)
        return record_healthcheck_result(
            project,
            target_url=target,
            success=False,
            status_code=None,
            response_time_ms=elapsed_ms,
            error_message=user_message,
            deployment_id=deployment.id if deployment else None,
            details={
                "exception_type": type(exc).__name__,
                "error_code": error_code,
                "raw_error": str(exc),
                "user_message": user_message,
            },
            commit=commit,
        )


def compute_project_runtime_state(project: Project, *, commit: bool = True) -> ProjectRuntimeState:
    last_successful = get_last_successful_deployment(project)

    active = project.active_deployment
    if not active or active.project_id != project.id:
        active = None
    if active and not (active.successful or active.status == "success"):
        active = None
    if active is None:
        active = last_successful

    latest_deployment = _latest_deployment(project)
    latest_health = _latest_healthcheck(project)

    reason = ""
    runtime_status = RUNTIME_FAILED

    if active is None:
        runtime_status = RUNTIME_FAILED
        reason = "Keine aktive erfolgreiche Version vorhanden"
    elif latest_health is None:
        runtime_status = RUNTIME_DEGRADED
        reason = "Noch kein Healthcheck vorhanden"
    elif not latest_health.success:
        runtime_status = RUNTIME_FAILED
        reason = _friendly_health_reason(latest_health)
    elif latest_deployment and latest_deployment.status == "failed" and _deployment_sort_time(latest_deployment) >= _deployment_sort_time(active):
        runtime_status = RUNTIME_FAILED
        reason = "Letzter produktiver Deploy fehlgeschlagen"
    elif _has_runtime_warnings(active):
        runtime_status = RUNTIME_DEGRADED
        reason = "Aktive Version laeuft mit Warnzustand (optionale Checks fehlgeschlagen)"
    else:
        runtime_status = RUNTIME_RUNNING
        reason = "Healthcheck erfolgreich und aktive Version erreichbar"

    project.current_runtime_status = runtime_status
    project.last_successful_deployment_id = last_successful.id if last_successful else None
    project.active_deployment_id = active.id if active else None
    project.active_version = (active.commit_sha or active.source_snapshot_path) if active else None
    project.active_source_reference = active.source_snapshot_path if active else None
    project.last_healthcheck_at = latest_health.checked_at if latest_health else project.last_healthcheck_at

    if commit:
        db.session.commit()

    return ProjectRuntimeState(
        current_runtime_status=project.current_runtime_status,
        last_successful_deployment_id=project.last_successful_deployment_id,
        active_deployment_id=project.active_deployment_id,
        active_version=project.active_version,
        active_source_reference=project.active_source_reference,
        last_healthcheck_at=project.last_healthcheck_at,
        reason=reason,
    )
