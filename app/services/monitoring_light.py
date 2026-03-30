from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from app.extensions import db
from app.models import Deployment, Project, ProjectHealthCheck, Server
from app.services.project_state_engine import record_healthcheck_result
from app.services.ssh import CommandNotAllowedError


MONITORING_HEALTHY = "healthy"
MONITORING_WARNING = "warning"
MONITORING_CRITICAL = "critical"
MONITORING_UNKNOWN = "unknown"


@dataclass(frozen=True)
class MonitoringLightResult:
    app_reachable: bool
    container_running: bool
    last_error_summary: str
    checked_at: str | None
    active_deployment_id: int | None
    monitoring_status: str
    app_check: dict[str, Any]
    container_check: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_reachable": self.app_reachable,
            "container_running": self.container_running,
            "last_error_summary": self.last_error_summary,
            "checked_at": self.checked_at,
            "active_deployment_id": self.active_deployment_id,
            "monitoring_status": self.monitoring_status,
            "app_check": self.app_check,
            "container_check": self.container_check,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cached_monitoring(project: Project) -> dict[str, Any] | None:
    return project.light_monitoring_json if isinstance(project.light_monitoring_json, dict) else None


def _cached_monitoring_fresh(project: Project, *, max_age_minutes: int = 2) -> bool:
    cached = _cached_monitoring(project)
    if not cached:
        return False
    checked_at_raw = cached.get("checked_at")
    if not isinstance(checked_at_raw, str) or not checked_at_raw.strip():
        return False
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return False
    checked_at = _as_utc(checked_at)
    return checked_at is not None and (_utcnow() - checked_at) <= timedelta(minutes=max_age_minutes)


def _active_deployment_for_project(project: Project) -> Deployment | None:
    if project.active_deployment and project.active_deployment.project_id == project.id:
        return project.active_deployment

    deployments = sorted(getattr(project, "deployments", []) or [], key=lambda d: (getattr(d, "created_at", _utcnow()), getattr(d, "id", 0)), reverse=True)
    for deployment in deployments:
        if getattr(deployment, "successful", False) or getattr(deployment, "status", "") == "success":
            return deployment
    return deployments[0] if deployments else None


def _active_server_for_project(project: Project, deployment: Deployment | None = None) -> Server | None:
    if project.active_server and project.active_server.ipv4:
        return project.active_server
    if deployment and deployment.server and deployment.server.ipv4:
        return deployment.server
    servers = sorted(getattr(project, "servers", []) or [], key=lambda s: getattr(s, "updated_at", _utcnow()), reverse=True)
    for server in servers:
        if getattr(server, "ipv4", None):
            return server
    return None


def _monitoring_target_candidates(project: Project, deployment: Deployment | None = None) -> list[str]:
    targets: list[str] = []
    domain = (project.domain or "").strip()
    if domain:
        targets.append(f"https://{domain}/health")
        targets.append(f"https://{domain}")

    server = _active_server_for_project(project, deployment)
    if server and server.ipv4:
        targets.append(f"http://{server.ipv4}/health")
        targets.append(f"http://{server.ipv4}")
    return targets


def _serialize_latest_health(entry: ProjectHealthCheck | None) -> dict[str, Any]:
    if entry is None:
        return {
            "checked_at": None,
            "target_url": "-",
            "success": False,
            "status_code": None,
            "response_time_ms": None,
            "error_message": "Noch kein Healthcheck vorhanden.",
        }
    return {
        "checked_at": entry.checked_at.isoformat() if entry.checked_at else None,
        "target_url": entry.target_url,
        "success": entry.success,
        "status_code": entry.status_code,
        "response_time_ms": entry.response_time_ms,
        "error_message": entry.error_message,
    }


def _latest_health(project: Project) -> ProjectHealthCheck | None:
    return (
        ProjectHealthCheck.query.filter_by(project_id=project.id)
        .order_by(ProjectHealthCheck.checked_at.desc(), ProjectHealthCheck.id.desc())
        .first()
    )


def check_app_reachability(project: Project, *, timeout_seconds: int = 5, persist: bool = True) -> dict[str, Any]:
    deployment = _active_deployment_for_project(project)
    targets = _monitoring_target_candidates(project, deployment)
    checked_at = _utcnow()

    if not targets:
        result = {
            "checked_at": checked_at.isoformat(),
            "target_url": "-",
            "success": False,
            "status_code": None,
            "response_time_ms": None,
            "error_message": "Kein aktives Ziel fuer Monitoring vorhanden.",
        }
        if persist:
            record_healthcheck_result(
                project,
                target_url=result["target_url"],
                success=False,
                status_code=None,
                response_time_ms=None,
                error_message=result["error_message"],
                deployment_id=deployment.id if deployment else None,
                details={"source": "monitoring_light", "reason": "no_target"},
                commit=True,
            )
        return result

    last_failure: dict[str, Any] | None = None
    for target_url in targets:
        started = _utcnow()
        try:
            response = requests.get(target_url, timeout=timeout_seconds, allow_redirects=True)
            response_time_ms = int((_utcnow() - started).total_seconds() * 1000)
            success = 200 <= response.status_code < 400
            result = {
                "checked_at": checked_at.isoformat(),
                "target_url": target_url,
                "success": success,
                "status_code": response.status_code,
                "response_time_ms": response_time_ms,
                "error_message": None if success else f"HTTP {response.status_code}",
            }
            if persist:
                record_healthcheck_result(
                    project,
                    target_url=target_url,
                    success=success,
                    status_code=response.status_code,
                    response_time_ms=response_time_ms,
                    error_message=result["error_message"],
                    deployment_id=deployment.id if deployment else None,
                    details={"source": "monitoring_light", "redirected": len(response.history) > 0},
                    commit=True,
                )
            if success:
                return result
            last_failure = result
        except requests.RequestException as exc:
            response_time_ms = int((_utcnow() - started).total_seconds() * 1000)
            last_failure = {
                "checked_at": checked_at.isoformat(),
                "target_url": target_url,
                "success": False,
                "status_code": None,
                "response_time_ms": response_time_ms,
                "error_message": str(exc),
            }

    if persist and last_failure is not None:
        record_healthcheck_result(
            project,
            target_url=last_failure["target_url"],
            success=False,
            status_code=last_failure["status_code"],
            response_time_ms=last_failure["response_time_ms"],
            error_message=last_failure["error_message"],
            deployment_id=deployment.id if deployment else None,
            details={"source": "monitoring_light", "attempted_targets": targets},
            commit=True,
        )
    return last_failure or {
        "checked_at": checked_at.isoformat(),
        "target_url": "-",
        "success": False,
        "status_code": None,
        "response_time_ms": None,
        "error_message": "HTTP-Check fehlgeschlagen.",
    }


def check_container_status(project: Project) -> dict[str, Any]:
    deployment = _active_deployment_for_project(project)
    if deployment is None:
        return {
            "running": False,
            "container_names": [],
            "raw_status": "",
            "error_message": "Kein aktives Deployment vorhanden.",
        }

    server = _active_server_for_project(project, deployment)
    if server is None or not server.ipv4:
        return {
            "running": False,
            "container_names": [],
            "raw_status": "",
            "error_message": "Kein Zielserver mit IP gefunden.",
        }

    deploy_dir = f"/opt/orbital/{project.slug}"
    command = f"docker compose -f {deploy_dir}/docker-compose.yml ps"

    try:
        from app.services.execution import DeploymentExecutor

        result = DeploymentExecutor().ssh.run_one(server.ipv4, command)
    except (RuntimeError, CommandNotAllowedError) as exc:
        return {
            "running": False,
            "container_names": [],
            "raw_status": "",
            "error_message": str(exc),
        }
    except Exception as exc:
        return {
            "running": False,
            "container_names": [],
            "raw_status": "",
            "error_message": f"SSH-Fehler: {exc}",
        }

    raw_status = (result.stdout or "").strip()
    if result.return_code != 0:
        return {
            "running": False,
            "container_names": [],
            "raw_status": raw_status,
            "error_message": (result.stderr or raw_status or "Container-Status konnte nicht gelesen werden.").strip(),
        }

    container_names: list[str] = []
    running = False
    for line in raw_status.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name") or set(stripped) == {"-"}:
            continue
        parts = stripped.split()
        if parts:
            container_names.append(parts[0])
        if "running" in stripped.lower() or "up" in stripped.lower():
            running = True

    return {
        "running": running,
        "container_names": container_names,
        "raw_status": raw_status,
        "error_message": None if running else "Container laeuft aktuell nicht stabil oder wurde nicht gefunden.",
    }


def get_last_relevant_error(project: Project) -> str:
    latest_health = _latest_health(project)
    if latest_health and not latest_health.success and latest_health.error_message:
        return latest_health.error_message

    deployments = sorted(getattr(project, "deployments", []) or [], key=lambda d: (getattr(d, "created_at", _utcnow()), getattr(d, "id", 0)), reverse=True)
    for deployment in deployments:
        analysis = deployment.error_analysis_json if isinstance(deployment.error_analysis_json, dict) else None
        if analysis and analysis.get("probable_cause"):
            return str(analysis.get("probable_cause"))
        if deployment.error_message:
            return str(deployment.error_message).split("\n", 1)[0]
        steps = sorted(getattr(deployment, "steps", []) or [], key=lambda s: (getattr(s, "order_index", 0), getattr(s, "id", 0)), reverse=True)
        for step in steps:
            if getattr(step, "status", "") != "failed":
                continue
            details = step.json_details if isinstance(step.json_details, dict) else {}
            analysis = details.get("error_analysis") if isinstance(details, dict) else None
            if isinstance(analysis, dict) and analysis.get("probable_cause"):
                return str(analysis.get("probable_cause"))
            if step.error_message:
                return str(step.error_message).split("\n", 1)[0]

    return "Kein relevanter Fehler bekannt."


def compute_light_monitoring_status(
    project: Project,
    *,
    force_refresh: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    if not force_refresh and _cached_monitoring_fresh(project):
        cached = _cached_monitoring(project)
        if cached is not None:
            return cached

    active_deployment = _active_deployment_for_project(project)
    app_check = check_app_reachability(project, persist=persist)
    container_check = check_container_status(project)
    last_error_summary = get_last_relevant_error(project)

    if app_check.get("success") and container_check.get("running"):
        monitoring_status = MONITORING_HEALTHY
    elif container_check.get("running") and (not app_check.get("success") or last_error_summary != "Kein relevanter Fehler bekannt."):
        monitoring_status = MONITORING_WARNING
    elif not app_check.get("success") or not container_check.get("running"):
        monitoring_status = MONITORING_CRITICAL
    else:
        monitoring_status = MONITORING_UNKNOWN

    result = MonitoringLightResult(
        app_reachable=bool(app_check.get("success")),
        container_running=bool(container_check.get("running")),
        last_error_summary=last_error_summary,
        checked_at=str(app_check.get("checked_at") or _utcnow().isoformat()),
        active_deployment_id=active_deployment.id if active_deployment else None,
        monitoring_status=monitoring_status,
        app_check=app_check,
        container_check=container_check,
    ).to_dict()

    if persist:
        project.light_monitoring_status = monitoring_status
        project.light_monitoring_json = result
        db.session.commit()

    return result
