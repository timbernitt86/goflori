import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests.exceptions
import redis
from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.dashboard import bp
from app.extensions import db
from app.models import ActivityLog, Deployment, DeploymentStep, EnvironmentVariable, Project, ProjectHealthCheck, ProviderSetting, Repository, Server
from app.services.auto_fix import execute_autofix, suggest_autofix_action
from app.services.hetzner import HetznerAPIError, HetznerClient
from app.services.execution import DeploymentExecutor, PipelineContext
from app.services.monitoring_light import compute_light_monitoring_status
from app.services.project_state_engine import (
    compute_project_runtime_state,
    get_last_successful_deployment,
    run_project_healthcheck,
)
from app.services.suggestions import generate_deployment_suggestions, generate_project_suggestions
from app.tasks.deployment import STEP_NAMES, run_deployment_task

logger = logging.getLogger(__name__)


DEFAULT_SERVER_TYPE = "cx22"
DEFAULT_LOCATION = "nbg1"
DEFAULT_IMAGE = "ubuntu-24.04"
SECRET_ENV_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_task_queue_usable() -> tuple[bool, str | None]:
    broker_url = ((current_app.config.get("CELERY") or {}).get("broker_url") or "").strip()

    # memory:// is only valid when a worker runs in the same process (tests/dev).
    # In production there is no such worker, so tasks would be silently swallowed.
    if broker_url.startswith("memory://") or broker_url.startswith("cache+memory://"):
        return False, "Kein Redis konfiguriert – Deployment wird direkt ausgefuehrt."

    if broker_url.startswith("redis://") or broker_url.startswith("rediss://"):
        try:
            client = redis.Redis.from_url(
                broker_url,
                socket_connect_timeout=1,
                socket_timeout=1,
                retry_on_timeout=False,
            )
            client.ping()
        except Exception as exc:
            return False, f"redis backend unreachable for task queue: {exc}"

    return True, None


def _deployment_preflight_error() -> str | None:
    if current_app.config.get("ORBITAL_DRY_RUN", True):
        return None

    key_path = (current_app.config.get("ORBITAL_SSH_KEY_PATH") or "").strip()
    key_material = (current_app.config.get("ORBITAL_SSH_PRIVATE_KEY") or "").strip()
    if key_path or key_material:
        return None

    # Also accept the private key stored in the dashboard's Hetzner settings.
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    if setting and setting.ssh_private_key:
        return None

    has_hetzner_pubkey = bool((setting.ssh_key_name if setting else None) or (setting.ssh_public_key if setting else None))
    if has_hetzner_pubkey:
        return (
            "Fast fertig: Du hast einen SSH-Key in den Hetzner-Einstellungen hinterlegt, "
            "aber Orbital braucht noch den passenden Private Key. "
            "Bitte trage ihn in den Hetzner-Einstellungen unter 'SSH Private Key' ein."
        )

    return (
        "Live-Deployment nicht moeglich: Bitte trage einen SSH Private Key in den "
        "Hetzner-Einstellungen ein (oder aktiviere ORBITAL_DRY_RUN=true fuer Simulation)."
    )


def _is_mock_server(server: Server | None) -> bool:
    if server is None:
        return False
    provider_id = (server.provider_server_id or "").strip()
    if provider_id.startswith("dry-run-") or provider_id == "dry-run-server-1":
        return True
    return False


def _run_deployment_inline_async(deployment_id: int) -> None:
    app = current_app._get_current_object()

    def _worker() -> None:
        with app.app_context():
            try:
                run_deployment_task.run(deployment_id)
            except Exception as exc:
                logger.exception("Inline async deployment failed for deployment id=%s", deployment_id)
                deployment = Deployment.query.filter_by(id=deployment_id).first()
                if deployment and deployment.status != "failed":
                    deployment.status = "failed"
                    deployment.error_message = f"Inline async deployment error: {exc}"
                    db.session.commit()

    thread = threading.Thread(target=_worker, name=f"deployment-inline-{deployment_id}", daemon=True)
    thread.start()


def _create_and_queue_deployment(
    project: Project,
    *,
    selected_server: Server | None,
    mode: str,
    commit_sha: str | None,
    trigger_source: str,
) -> tuple[Deployment | None, bool]:
    preflight_error = _deployment_preflight_error()
    if preflight_error:
        logger.warning("Deployment preflight blocked for project id=%s: %s", project.id, preflight_error)
        flash(preflight_error, "error")
        return None, False

    deployment: Deployment | None = None

    try:
        deployment = Deployment(
            project_id=project.id,
            server_id=selected_server.id if selected_server else None,
            status="pending",
            mode=mode,
            commit_sha=commit_sha,
            trigger_source=trigger_source,
        )
        db.session.add(deployment)
        db.session.flush()

        # Create initial steps so the UI can render progress immediately.
        for index, name in enumerate(STEP_NAMES):
            db.session.add(
                DeploymentStep(
                    deployment_id=deployment.id,
                    name=name,
                    status="pending",
                    order_index=index,
                )
            )

        db.session.commit()
        logger.info(
            "Dashboard created deployment id=%s for project id=%s source=%s",
            deployment.id,
            project.id,
            trigger_source,
        )
    except Exception:
        db.session.rollback()
        logger.exception("Failed to create deployment for project id=%s", project.id)
        return None, False

    try:
        queue_usable, queue_reason = _is_task_queue_usable()
        if not queue_usable:
            raise RuntimeError(queue_reason or "task queue unavailable")

        async_result = run_deployment_task.delay(deployment.id)
        logger.info(
            "Queued deployment id=%s for project id=%s with task id=%s",
            deployment.id,
            project.id,
            async_result.id,
        )
        flash(f"Deployment gestartet (Task ID: {async_result.id}).", "success")
    except Exception as exc:
        queue_reason = str(exc)
        if current_app.config.get("ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR", False):
            logger.info(
                "Queue unavailable for deployment id=%s (%s). Running inline.",
                deployment.id,
                queue_reason,
            )
            _run_deployment_inline_async(deployment.id)
            flash("Deployment gestartet.", "success")
        else:
            logger.exception(
                "Failed to queue deployment id=%s for project id=%s",
                deployment.id,
                project.id,
            )
            deployment.status = "failed"
            deployment.error_message = f"Task queue error: {exc}"
            db.session.commit()
            flash("Deployment wurde angelegt, konnte aber nicht gestartet werden.", "error")
            return deployment, False

    return deployment, True


def _format_command_results(results) -> str:
    lines: list[str] = []
    for item in results:
        lines.append(f"cmd={item.command}")
        lines.append(f"exit_code={item.return_code}")
        if item.stdout:
            lines.append(f"stdout={item.stdout.strip()}")
        if item.stderr:
            lines.append(f"stderr={item.stderr.strip()}")
        lines.append("---")
    return "\n".join(lines).rstrip("-\n")


def _resolve_selected_project_server(project: Project, selected_server_id: str | None) -> tuple[Server | None, str | None]:
    selected_server = None
    selected_server_id = (selected_server_id or "").strip()

    if selected_server_id:
        try:
            selected_server = Server.query.filter_by(project_id=project.id, id=int(selected_server_id)).first()
        except ValueError:
            selected_server = None
        if selected_server is None:
            return None, "Ausgewaehlter Zielserver wurde nicht gefunden."

    target_server = selected_server or _resolve_target_server(project)
    if target_server is None or not target_server.ipv4:
        return None, "Kein Zielserver mit gueltiger IP gefunden."
    return target_server, None


def _resolve_target_server(project: Project, deployment: Deployment | None = None) -> Server | None:
    """Resolve target machine for a deployment.

    Priority order:
    1) explicit deployment.server assignment
    2) project.active_server
    3) inferred fallback from project server timestamps
    """
    if deployment and deployment.server is not None and not _is_mock_server(deployment.server):
        return deployment.server
    if project.active_server is not None and not _is_mock_server(project.active_server):
        return project.active_server

    servers = sorted([s for s in project.servers if not _is_mock_server(s)], key=lambda s: s.created_at)
    if not servers:
        return None
    if deployment is None:
        return servers[-1]

    candidates = [server for server in servers if server.created_at <= deployment.created_at]
    if candidates:
        return candidates[-1]
    return servers[-1]


def _get_or_init_hetzner_setting() -> ProviderSetting:
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    if setting:
        return setting
    return ProviderSetting(
        provider_name="hetzner",
        default_location=DEFAULT_LOCATION,
        default_server_type=DEFAULT_SERVER_TYPE,
        default_image=DEFAULT_IMAGE,
    )


def _get_hetzner_defaults() -> dict[str, str | None]:
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    return {
        "default_server_type": (setting.default_server_type if setting else None) or DEFAULT_SERVER_TYPE,
        "default_location": (setting.default_location if setting else None) or DEFAULT_LOCATION,
        "default_image": (setting.default_image if setting else None) or DEFAULT_IMAGE,
        "ssh_key_name": setting.ssh_key_name if setting else None,
    }


def _infer_repository_provider(repo_url: str | None) -> str | None:
    value = (repo_url or "").strip().lower()
    if not value:
        return None
    if "github.com" in value:
        return "github"
    if "gitlab.com" in value:
        return "gitlab"
    if "bitbucket.org" in value:
        return "bitbucket"
    return None


def _latest_project_health(project: Project) -> ProjectHealthCheck | None:
    return (
        ProjectHealthCheck.query.filter_by(project_id=project.id)
        .order_by(ProjectHealthCheck.checked_at.desc(), ProjectHealthCheck.id.desc())
        .first()
    )


def _refresh_project_runtime_state(project: Project, *, allow_healthcheck: bool = False) -> dict:
    latest_health = _latest_project_health(project)
    if allow_healthcheck:
        stale = latest_health is None
        if latest_health is not None and latest_health.checked_at is not None:
            checked_at = _as_utc_aware(latest_health.checked_at)
            stale = (_utcnow() - checked_at) > timedelta(minutes=2) if checked_at else True
        if stale:
            try:
                run_project_healthcheck(project, deployment=project.active_deployment, commit=False)
                latest_health = _latest_project_health(project)
            except Exception as exc:
                logger.warning("Runtime healthcheck refresh failed for project id=%s: %s", project.id, exc)

    runtime = compute_project_runtime_state(project, commit=False)
    return {
        "current_runtime_status": runtime.current_runtime_status,
        "reason": runtime.reason,
        "last_successful_deployment_id": runtime.last_successful_deployment_id,
        "active_deployment_id": runtime.active_deployment_id,
        "active_version": runtime.active_version,
        "active_source_reference": runtime.active_source_reference,
        "last_healthcheck_at": runtime.last_healthcheck_at,
        "last_healthcheck": latest_health,
    }


def _build_next_step_guidance(
    project: Project,
    runtime_state: dict,
    deployments: list[Deployment],
    deployment_meta: dict,
) -> dict:
    runtime_status = (runtime_state or {}).get("current_runtime_status") or "unknown"
    runtime_reason = (runtime_state or {}).get("reason") or ""
    active_deployment_id = (runtime_state or {}).get("active_deployment_id")

    latest_deployment = deployments[0] if deployments else None
    latest_meta = deployment_meta.get(latest_deployment.id, {}) if latest_deployment else {}
    latest_error = (latest_meta.get("last_error") or "") if isinstance(latest_meta, dict) else ""

    combined_error = f"{runtime_reason}\n{latest_error}".lower()
    has_dns_issue = "dns-pruefung fehlgeschlagen" in combined_error or "domain zeigt nicht" in combined_error
    has_tls_issue = "zertifikat" in combined_error or "tls" in combined_error or "ssl" in combined_error

    if runtime_status == "running":
        if latest_deployment and latest_deployment.status == "failed" and active_deployment_id:
            user_action = "Pruefe den Fehler des letzten Deployments und fuehre danach Redeploy aus."
            flori_action = "Flori kann den Redeploy und die Runtime-Pruefung automatisch erneut ausfuehren."
            if has_dns_issue:
                user_action = "Ja, Nutzer muss handeln: A-Record der Domain auf die Ziel-IP setzen."
                flori_action = "Flori kann nach DNS-Fix automatisch redeployen und certbot/Healthcheck erneut laufen lassen."
            elif has_tls_issue:
                user_action = "Ja, Nutzer muss Domain-/Zertifikatszuordnung pruefen (www/non-www)."
                flori_action = "Flori kann danach certbot erneut ausfuehren und den Runtime-State aktualisieren."

            return {
                "is_live": True,
                "headline": "Projekt ist LIVE (aktive Version laeuft), aber der letzte Deploy ist fehlgeschlagen.",
                "user_action": user_action,
                "flori_action": flori_action,
            }

        return {
            "is_live": True,
            "headline": "Projekt ist LIVE und erreichbar.",
            "user_action": "Kein akuter Handlungsbedarf. Fuer Code-Aenderungen reicht meist ein Redeploy.",
            "flori_action": "Flori kann weiter Runtime-Checks, Deployments und Logs uebernehmen.",
        }

    if runtime_status == "degraded":
        user_action = "Bitte Warnursache pruefen und danach Redeploy oder Healthcheck erneut ausfuehren."
        flori_action = "Flori kann Healthcheck/Container-Checks erneut ausfuehren und den Status aktualisieren."
        if has_dns_issue:
            user_action = "Ja, Nutzer muss handeln: DNS A-Record auf die Ziel-IP korrigieren."
            flori_action = "Flori kann nach DNS-Korrektur certbot, Healthcheck und Redeploy erneut ausfuehren."
        elif has_tls_issue:
            user_action = "Ja, Nutzer muss Zertifikats-/Domain-Mapping pruefen (www/non-www)."
            flori_action = "Flori kann certbot erneut starten und die Erreichbarkeit danach pruefen."

        return {
            "is_live": bool(active_deployment_id),
            "headline": "Projekt ist LIVE, aber im Warnzustand (degraded)." if active_deployment_id else "Projekt ist im Warnzustand (degraded).",
            "user_action": user_action,
            "flori_action": flori_action,
        }

    if runtime_status == "failed":
        user_action = "Pruefe die letzte Fehlermeldung und korrigiere die Ursache, dann Redeploy starten."
        flori_action = "Flori kann danach Redeploy, certbot und Healthcheck erneut ausfuehren."
        is_domain_only_issue = (has_dns_issue or has_tls_issue) and bool(active_deployment_id)
        if has_dns_issue:
            user_action = "Ja, Nutzer muss handeln: A-Record auf die Ziel-IP setzen."
            flori_action = "Flori kann nach DNS-Fix den technischen Teil komplett neu ausrollen."
        elif has_tls_issue:
            user_action = "Ja, Nutzer muss handeln: Zertifikat/Domain-Zuordnung (www/non-www) korrigieren."
            flori_action = "Flori kann danach certbot/HTTPS-Checks erneut ausfuehren."

        return {
            "is_live": bool(active_deployment_id),
            "headline": (
                "Projekt ist LIVE auf Server-Ebene, aber ueber die Domain aktuell nicht erreichbar."
                if is_domain_only_issue
                else "Projekt ist aktuell NICHT live erreichbar."
            ),
            "user_action": user_action,
            "flori_action": flori_action,
        }

    return {
        "is_live": project.status == "live",
        "headline": "Projektstatus wird ermittelt.",
        "user_action": "Bitte Runtime-Status und letzte Deploy-Logs pruefen.",
        "flori_action": "Flori kann Healthcheck und Deploy-Validierung erneut ausfuehren.",
    }


def _is_secret_env_key(key: str) -> bool:
    return any(key.upper().endswith(suffix) for suffix in SECRET_ENV_SUFFIXES)


def _parse_env_lines(raw_value: str) -> list[tuple[str, str, bool]]:
    items: list[tuple[str, str, bool]] = []
    for line in raw_value.splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        items.append((key, value.strip(), _is_secret_env_key(key)))
    return items


def _upsert_project_environment(project: Project, raw_value: str) -> dict[str, int]:
    created = 0
    updated = 0
    existing_by_key = {item.key: item for item in project.environment_variables}

    for key, value, is_secret in _parse_env_lines(raw_value):
        existing = existing_by_key.get(key)
        if existing:
            existing.value = value
            existing.is_secret = is_secret
            updated += 1
            continue

        project.environment_variables.append(
            EnvironmentVariable(
                key=key,
                value=value,
                is_secret=is_secret,
            )
        )
        created += 1

    return {"created": created, "updated": updated}


def _project_env_lines(project: Project) -> str:
    return "\n".join(f"{item.key}={item.value}" for item in sorted(project.environment_variables, key=lambda env: env.key))


def _generate_unique_project_slug(name: str, requested_slug: str | None = None) -> str:
    base_slug = (requested_slug or "").strip() or Project.slugify(name)
    if not base_slug:
        base_slug = f"projekt-{uuid4().hex[:6]}"

    candidate = base_slug
    suffix = 2
    while Project.query.filter_by(slug=candidate).first() is not None:
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _extract_error_analysis(step: DeploymentStep | None) -> dict | None:
    if step is None or not isinstance(step.json_details, dict):
        return None
    analysis = step.json_details.get("error_analysis")
    if isinstance(analysis, dict) and analysis.get("error_type"):
        return analysis
    return None


def _extract_deployment_error_analysis(deployment: Deployment, step: DeploymentStep | None = None) -> dict | None:
    if isinstance(deployment.error_analysis_json, dict) and deployment.error_analysis_json.get("error_type"):
        return deployment.error_analysis_json
    return _extract_error_analysis(step)


def _latest_failed_step(deployment: Deployment) -> DeploymentStep | None:
    steps = sorted(deployment.steps, key=lambda s: (s.order_index, s.id), reverse=True)
    return next((step for step in steps if step.status == "failed"), None)


def _suggest_fix_issue(error_analysis: dict | None, failed_step_name: str | None) -> dict:
    error_type = (error_analysis or {}).get("error_type")
    if error_type in {"missing_env", "env_missing"}:
        return {
            "action": "redeploy",
            "title": "Fix Issue",
            "label": "Deployment erneut ausfuehren",
            "description": "Startet ein neues Deployment, nachdem App-Einstellungen aktualisiert wurden.",
        }
    if error_type == "nginx_error" or failed_step_name in {"configure_reverse_proxy", "run_certbot", "verify_https"}:
        return {
            "action": "reload_nginx",
            "title": "Fix Issue",
            "label": "Nginx neu laden",
            "description": "Prueft die Nginx-Konfiguration und laedt Nginx neu.",
        }
    if error_type in {"port_conflict", "db_connection", "build_fail", "container_start_failure", "port_unreachable", "db_unreachable"} or failed_step_name in {
        "start_containers",
        "healthcheck",
    }:
        return {
            "action": "restart_container",
            "title": "Fix Issue",
            "label": "Container neu starten",
            "description": "Startet den Web-Container neu, ohne ein komplettes Deployment auszufuehren.",
        }
    return {
        "action": "redeploy",
        "title": "Fix Issue",
        "label": "Deployment erneut ausfuehren",
        "description": "Startet ein neues Deployment mit denselben Projekteinstellungen.",
    }


def _autofix_action_ui(action: str | None) -> dict:
    if action == "restart_container":
        return {
            "action": "restart_container",
            "title": "Fix Issue",
            "label": "Container neu starten",
            "description": "Flori versucht, den aktiven Web-Container sicher neu zu starten.",
        }
    if action == "reload_nginx":
        return {
            "action": "reload_nginx",
            "title": "Fix Issue",
            "label": "nginx neu laden",
            "description": "Flori prueft zuerst nginx -t und laedt danach nginx neu.",
        }
    if action == "retry_deploy":
        return {
            "action": "retry_deploy",
            "title": "Fix Issue",
            "label": "Deployment erneut anstossen",
            "description": "Flori startet ein kontrolliertes Retry-Deployment mit Schutz gegen Endlosschleifen.",
        }
    return {
        "action": "redeploy",
        "title": "Fix Issue",
        "label": "Deployment erneut ausfuehren",
        "description": "Startet ein neues Deployment mit denselben Projekteinstellungen.",
    }


def _autofix_result_message(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return "Flori konnte keine Aktion ausfuehren."
    action = entry.get("action_name")
    success = bool(entry.get("success"))
    if action == "restart_container":
        return "Flori hat versucht, den Container neu zu starten." if success else "Flori konnte den Container nicht neu starten."
    if action == "reload_nginx":
        return "Flori hat nginx neu geladen." if success else "Flori konnte nginx nicht neu laden."
    if action == "retry_deploy":
        return "Flori hat das Deployment erneut angestossen." if success else "Flori konnte das Retry-Deployment nicht starten."
    return "Flori hat keine automatische Aktion ausgefuehrt."


def _apply_hetzner_settings_from_form(setting: ProviderSetting) -> None:
    api_token = (request.form.get("api_token") or "").strip()
    default_location = (request.form.get("default_location") or "").strip() or None
    default_server_type = (request.form.get("default_server_type") or "").strip() or None
    default_image = (request.form.get("default_image") or "").strip() or None
    ssh_key_name = (request.form.get("ssh_key_name") or "").strip() or None
    ssh_public_key = (request.form.get("ssh_public_key") or "").strip() or None
    ssh_private_key = (request.form.get("ssh_private_key") or "").strip() or None

    # Keep existing token when field is intentionally left empty.
    if api_token:
        setting.api_token = api_token

    setting.default_location = default_location
    setting.default_server_type = default_server_type
    setting.default_image = default_image
    setting.ssh_key_name = ssh_key_name
    # Keep existing keys when fields are intentionally left empty.
    if ssh_public_key:
        setting.ssh_public_key = ssh_public_key
    if ssh_private_key:
        setting.ssh_private_key = ssh_private_key


@bp.before_request
@login_required
def require_login():
    pass


@bp.get("/")
def dashboard_home():
    return redirect(url_for("dashboard.projects"))


@bp.get("/settings/hetzner")
def hetzner_settings():
    setting = _get_or_init_hetzner_setting()
    resources = {
        "server_types": [],
        "locations": [],
        "images": [],
        "ssh_keys": [],
    }
    resource_errors: list[str] = []

    if setting.api_token:
        client = HetznerClient()
        loaders = {
            "Server-Typen": client.list_server_types,
            "Locations": client.list_locations,
            "Images": client.list_images,
            "SSH-Keys": client.list_ssh_keys,
        }

        for label, loader in loaders.items():
            try:
                items = loader(force_live=True)
            except HetznerAPIError as exc:
                logger.warning("Failed to load Hetzner %s: %s (HTTP %s)", label, exc, exc.status_code)
                if exc.status_code == 401:
                    resource_errors.append(f"{label}: Ungültiges API-Token.")
                elif exc.status_code is None:
                    resource_errors.append(f"{label}: {exc}")
                else:
                    resource_errors.append(f"{label}: API-Fehler {exc.status_code} ({exc}).")
                continue
            except requests.exceptions.Timeout:
                logger.warning("Timed out while loading Hetzner %s", label)
                resource_errors.append(f"{label}: Zeitüberschreitung bei der API-Abfrage.")
                continue
            except requests.exceptions.ConnectionError:
                logger.warning("Network error while loading Hetzner %s", label)
                resource_errors.append(f"{label}: Hetzner API ist nicht erreichbar.")
                continue
            except Exception:
                logger.exception("Unexpected error while loading Hetzner %s", label)
                resource_errors.append(f"{label}: Unbekannter Fehler beim Laden.")
                continue

            if label == "Server-Typen":
                resources["server_types"] = items
            elif label == "Locations":
                resources["locations"] = items
            elif label == "Images":
                resources["images"] = items
            elif label == "SSH-Keys":
                resources["ssh_keys"] = items
    else:
        resource_errors.append("Kein API-Token gespeichert. Ressourcen können erst nach dem Speichern geladen werden.")

    return render_template(
        "dashboard/hetzner_settings.html",
        setting=setting,
        token_configured=bool(setting.api_token),
        ssh_public_key_configured=bool(setting.ssh_public_key),
        ssh_private_key_configured=bool(setting.ssh_private_key),
        resources=resources,
        resource_errors=resource_errors,
    )


@bp.post("/settings/hetzner")
def save_hetzner_settings():
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    if not setting:
        setting = ProviderSetting(provider_name="hetzner")
        db.session.add(setting)
    _apply_hetzner_settings_from_form(setting)

    try:
        db.session.commit()
        logger.info(
            "Saved Hetzner settings (provider=%s, location=%s, server_type=%s, image=%s, ssh_key_name=%s, token_set=%s)",
            setting.provider_name,
            setting.default_location,
            setting.default_server_type,
            setting.default_image,
            setting.ssh_key_name,
            bool(setting.api_token),
        )
        flash("Hetzner-Konfiguration wurde gespeichert.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save global Hetzner provider settings")
        flash("Hetzner-Konfiguration konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.hetzner_settings"))


@bp.post("/settings/hetzner/test")
def test_hetzner_connection():
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    if not setting:
        setting = ProviderSetting(provider_name="hetzner")
        db.session.add(setting)
    _apply_hetzner_settings_from_form(setting)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save Hetzner settings before connection test")
        flash("Einstellungen konnten vor dem Test nicht gespeichert werden.", "error")
        return redirect(url_for("dashboard.hetzner_settings"))

    try:
        client = HetznerClient()
        result = client.test_connection(force_live=True)
    except HetznerAPIError as exc:
        logger.warning("Hetzner connection test failed: %s (HTTP %s)", exc, exc.status_code)
        if exc.status_code == 401:
            flash("Verbindungstest fehlgeschlagen: Ungültiges API-Token.", "error")
        elif exc.status_code is None:
            flash(f"Verbindungstest fehlgeschlagen: {exc}", "error")
        else:
            flash(f"Verbindungstest fehlgeschlagen: API-Fehler {exc.status_code} – {exc}", "error")
    except requests.exceptions.Timeout as exc:
        logger.warning("Hetzner connection test timed out: %s", exc)
        flash("Verbindungstest fehlgeschlagen: Zeitüberschreitung beim Verbinden mit der Hetzner API.", "error")
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Hetzner connection test network error: %s", exc)
        flash("Verbindungstest fehlgeschlagen: Hetzner API nicht erreichbar. Netzwerk prüfen.", "error")
    except Exception:
        logger.exception("Unexpected error during Hetzner connection test")
        flash("Verbindungstest fehlgeschlagen: Unbekannter Fehler.", "error")
    else:
        dc = result.get("datacenters", 0)
        flash(
            f"Verbindungstest erfolgreich. Hetzner API erreichbar – {dc} Rechenzentrum/Rechenzentren gefunden.",
            "success",
        )
        logger.info("Hetzner connection test succeeded: %s", result)

    return redirect(url_for("dashboard.hetzner_settings"))


@bp.get("/projects")
def projects():
    try:
        deployment_counts = dict(
            db.session.query(Deployment.project_id, func.count(Deployment.id)).group_by(Deployment.project_id).all()
        )
        projects_data = (
            Project.query.options(
                joinedload(Project.repository),
                joinedload(Project.active_server),
                joinedload(Project.active_deployment),
                joinedload(Project.deployments).joinedload(Deployment.steps),
                joinedload(Project.deployments).joinedload(Deployment.server),
            )
            .filter_by(company_id=current_user.company_id)
            .order_by(Project.created_at.desc())
            .all()
        )
    except Exception as exc:
        logger.exception("Failed to load projects dashboard data. Trying DB migration recovery: %s", exc)
        try:
            migrate_upgrade()
            db.session.remove()
            deployment_counts = dict(
                db.session.query(Deployment.project_id, func.count(Deployment.id)).group_by(Deployment.project_id).all()
            )
            projects_data = (
                Project.query.options(
                    joinedload(Project.repository),
                    joinedload(Project.active_server),
                    joinedload(Project.active_deployment),
                    joinedload(Project.deployments).joinedload(Deployment.steps),
                    joinedload(Project.deployments).joinedload(Deployment.server),
                )
                .filter_by(company_id=current_user.company_id)
                .order_by(Project.created_at.desc())
                .all()
            )
            flash("Datenbank wurde aktualisiert. Dashboard-Daten wurden neu geladen.", "warning")
        except Exception as recovery_exc:
            logger.exception("DB migration recovery for dashboard projects failed: %s", recovery_exc)
            flash(
                "Dashboard konnte nicht geladen werden. Bitte Server-Logs pruefen (DB-Schema oder Migration).",
                "error",
            )
            deployment_counts = {}
            projects_data = []

    runtime_state_by_project: dict[int, dict] = {}
    monitoring_by_project: dict[int, dict] = {}
    for project in projects_data:
        runtime_state_by_project[project.id] = _refresh_project_runtime_state(project, allow_healthcheck=False)
        monitoring_by_project[project.id] = compute_light_monitoring_status(project, force_refresh=False, persist=True)
    db.session.commit()

    return render_template(
        "dashboard/projects.html",
        projects=projects_data,
        deployment_counts=deployment_counts,
        runtime_state_by_project=runtime_state_by_project,
        monitoring_by_project=monitoring_by_project,
        hetzner_defaults=_get_hetzner_defaults(),
    )


@bp.post("/projects")
def create_project():
    create_action = (request.form.get("create_action") or "create").strip()
    name = (request.form.get("name") or "").strip()
    slug = (request.form.get("slug") or "").strip()
    framework = (request.form.get("framework") or "").strip() or None
    domain = (request.form.get("domain") or "").strip() or None
    environment = (request.form.get("environment") or "production").strip() or "production"
    branch = (request.form.get("branch") or "main").strip() or "main"
    desired_server_type = (request.form.get("desired_server_type") or "").strip() or None
    desired_location = (request.form.get("desired_location") or "").strip() or None
    desired_image = (request.form.get("desired_image") or "").strip() or None
    repository_url = (request.form.get("repository_url") or "").strip()
    repository_provider = (request.form.get("repository_provider") or "").strip() or _infer_repository_provider(repository_url)
    repository_branch = (request.form.get("repository_branch") or "").strip() or branch
    repository_access_token = (request.form.get("repository_access_token") or "").strip() or None
    repository_is_private = (request.form.get("repository_is_private") or "").strip() in {"1", "true", "on", "yes"}
    env_lines = request.form.get("env_lines") or ""

    if not name:
        flash("Projektname ist erforderlich.", "error")
        return redirect(url_for("dashboard.projects"))

    final_slug = _generate_unique_project_slug(name=name, requested_slug=slug)
    project = Project(
        company_id=current_user.company_id,
        name=name,
        slug=final_slug,
        framework=framework,
        environment=environment,
        domain=domain,
        desired_server_type=desired_server_type,
        desired_location=desired_location,
        desired_image=desired_image,
        branch=branch,
    )

    if repository_url:
        project.repository = Repository(
            provider=repository_provider,
            repo_url=repository_url,
            branch=repository_branch,
            access_token=repository_access_token,
            is_private=repository_is_private,
        )

    _upsert_project_environment(project, env_lines)

    db.session.add(project)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raw_error = str(getattr(exc, "orig", exc)).lower()
        if "projects.slug" in raw_error or "unique constraint failed: projects.slug" in raw_error:
            flash("Projekt konnte nicht erstellt werden, obwohl ein eindeutiger Slug erzeugt wurde. Bitte erneut versuchen.", "error")
        else:
            flash("Projekt konnte wegen eines Datenbankkonflikts nicht erstellt werden. Bitte Eingaben pruefen.", "error")
        logger.exception("Project creation failed with integrity error for name=%s slug=%s", name, final_slug)
        return redirect(url_for("dashboard.projects"))

    if create_action == "create_and_deploy":
        deployment, created_ok = _create_and_queue_deployment(
            project,
            selected_server=None,
            mode="production",
            commit_sha=None,
            trigger_source="dashboard-onboarding",
        )
        if created_ok and deployment is not None:
            flash(
                "Projekt wurde erstellt und Deployment direkt gestartet.",
                "success",
            )
            return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))

        flash(
            "Projekt wurde erstellt, aber das automatische Deployment konnte nicht gestartet werden.",
            "warning",
        )
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    flash(f"Projekt '{project.name}' wurde erstellt.", "success")
    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/setup")
def save_project_setup(project_id: int):
    project = Project.query.options(joinedload(Project.repository), joinedload(Project.environment_variables)).filter_by(id=project_id).first()
    if not project:
        abort(404)

    project.domain = (request.form.get("domain") or "").strip() or None

    repo_url = (request.form.get("repo_url") or "").strip()
    branch = (request.form.get("branch") or "").strip() or project.branch or "main"
    provider = (request.form.get("provider") or "").strip() or _infer_repository_provider(repo_url)
    access_token = (request.form.get("access_token") or "").strip()
    is_private = (request.form.get("is_private") or "").strip() in {"1", "true", "on", "yes"}

    if repo_url:
        repository = project.repository or Repository(project_id=project.id)
        repository.repo_url = repo_url
        repository.branch = branch
        repository.provider = provider
        repository.is_private = is_private
        if access_token:
            repository.access_token = access_token
        project.repository = repository
        project.branch = branch
        db.session.add(repository)
    elif project.repository:
        db.session.delete(project.repository)

    env_summary = {"created": 0, "updated": 0}
    env_lines = request.form.get("env_lines") or ""
    if env_lines.strip():
        env_summary = _upsert_project_environment(project, env_lines)

    try:
        db.session.commit()
        flash(
            (
                "Schnellstart-Konfiguration gespeichert. "
                f"ENV erstellt: {env_summary['created']}, aktualisiert: {env_summary['updated']}."
            ),
            "success",
        )
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save quick setup for project id=%s", project.id)
        flash("Schnellstart-Konfiguration konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.get("/projects/<int:project_id>")
def project_detail(project_id: int):
    project = (
        Project.query.options(
            joinedload(Project.repository),
            joinedload(Project.servers),
            joinedload(Project.active_server),
            joinedload(Project.environment_variables),
            joinedload(Project.deployments).joinedload(Deployment.steps),
            joinedload(Project.deployments).joinedload(Deployment.server),
        )
        .filter_by(id=project_id)
        .first()
    )
    if not project:
        abort(404)

    hetzner_defaults = _get_hetzner_defaults()

    deployments = sorted(project.deployments, key=lambda d: d.created_at, reverse=True)
    successful_deployments = [deployment for deployment in deployments if deployment.successful or deployment.status == "success"]
    successful_deployments = sorted(
        successful_deployments,
        key=lambda d: d.successful_at or d.updated_at,
        reverse=True,
    )[:5]
    visible_servers = [server for server in project.servers if not _is_mock_server(server)]
    latest_server = _resolve_target_server(project)
    domain_target_ip = latest_server.ipv4 if project.domain and latest_server else None
    deployment_meta = {}
    for deployment in deployments:
        steps = sorted(deployment.steps, key=lambda s: (s.order_index, s.id))
        failed_steps = [step for step in steps if step.status == "failed"]
        latest_failed_step = failed_steps[-1] if failed_steps else None
        latest_analysis = _extract_deployment_error_analysis(deployment, latest_failed_step)
        fix_suggestion = _suggest_fix_issue(latest_analysis, latest_failed_step.name if latest_failed_step else None)
        target_server = _resolve_target_server(project, deployment)

        latest_step_error = None
        for step in reversed(steps):
            if step.error_message:
                latest_step_error = step.error_message
                break

        deployment_meta[deployment.id] = {
            "steps_count": len(steps),
            "failed_steps_count": len(failed_steps),
            "last_error": deployment.error_message or latest_step_error,
            "error_analysis": latest_analysis,
            "fix_suggestion": fix_suggestion,
            "target_machine": target_server,
        }

    runtime_state = _refresh_project_runtime_state(project, allow_healthcheck=True)
    light_monitoring = compute_light_monitoring_status(project, force_refresh=False, persist=True)
    project_suggestions = generate_project_suggestions(project, runtime_state=runtime_state)
    next_step_guidance = _build_next_step_guidance(project, runtime_state, deployments, deployment_meta)
    health_history = (
        ProjectHealthCheck.query.filter_by(project_id=project.id)
        .order_by(ProjectHealthCheck.checked_at.desc(), ProjectHealthCheck.id.desc())
        .limit(10)
        .all()
    )
    db.session.commit()

    return render_template(
        "dashboard/project_detail.html",
        project=project,
        quickstart_env_lines=_project_env_lines(project),
        quickstart_state={
            "has_repository": bool(project.repository and (project.repository.repo_url or "").strip()),
            "has_env": bool(project.environment_variables),
            "has_domain": bool((project.domain or "").strip()),
        },
        visible_servers=visible_servers,
        deployments=deployments,
        successful_deployments=successful_deployments,
        deployment_meta=deployment_meta,
        latest_server=latest_server,
        domain_target_ip=domain_target_ip,
        hetzner_defaults=hetzner_defaults,
        effective_server_type=project.desired_server_type or hetzner_defaults.get("default_server_type"),
        effective_location=project.desired_location or hetzner_defaults.get("default_location"),
        effective_image=project.desired_image or hetzner_defaults.get("default_image"),
        runtime_state=runtime_state,
        light_monitoring=light_monitoring,
        project_suggestions=project_suggestions,
        next_step_guidance=next_step_guidance,
        health_history=health_history,
        last_successful_deployment=get_last_successful_deployment(project),
    )


@bp.post("/projects/<int:project_id>/monitoring-light-refresh")
def refresh_project_monitoring_light(project_id: int):
    project = Project.query.options(
        joinedload(Project.active_deployment),
        joinedload(Project.active_server),
        joinedload(Project.deployments).joinedload(Deployment.steps),
        joinedload(Project.deployments).joinedload(Deployment.server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    try:
        result = compute_light_monitoring_status(project, force_refresh=True, persist=True)
    except Exception as exc:
        logger.exception("Monitoring Light refresh failed for project id=%s", project_id)
        return jsonify({"error": str(exc)}), 500

    return jsonify({"project_id": project.id, "monitoring": result})


@bp.post("/projects/<int:project_id>/runtime-healthcheck")
def run_project_runtime_healthcheck(project_id: int):
    project = Project.query.options(joinedload(Project.active_deployment), joinedload(Project.active_server)).filter_by(id=project_id).first()
    if not project:
        abort(404)

    try:
        health = run_project_healthcheck(project, deployment=project.active_deployment)
        state = compute_project_runtime_state(project)
    except Exception as exc:
        logger.exception("Runtime healthcheck failed for project id=%s", project_id)
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "project_id": project.id,
            "current_runtime_status": state.current_runtime_status,
            "reason": state.reason,
            "last_healthcheck": health.to_dict(),
        }
    )


@bp.post("/projects/<int:project_id>/infrastructure")
def save_project_infrastructure(project_id: int):
    project = Project.query.get_or_404(project_id)

    project.domain = (request.form.get("domain") or "").strip() or None
    project.desired_server_type = (request.form.get("desired_server_type") or "").strip() or None
    project.desired_location = (request.form.get("desired_location") or "").strip() or None
    project.desired_image = (request.form.get("desired_image") or "").strip() or None
    project.rolling_update_enabled = (request.form.get("rolling_update_enabled") or "").strip() in {"1", "true", "on", "yes"}

    try:
        db.session.commit()
        flash("Projekt-Infrastruktur wurde gespeichert.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save project infrastructure for project id=%s", project.id)
        flash("Projekt-Infrastruktur konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/repository")
def save_project_repository(project_id: int):
    project = Project.query.get_or_404(project_id)

    repo_url = (request.form.get("repo_url") or "").strip()
    branch = (request.form.get("branch") or "").strip() or project.branch or "main"
    provider = (request.form.get("provider") or "").strip() or _infer_repository_provider(repo_url)
    access_token = (request.form.get("access_token") or "").strip()
    is_private = (request.form.get("is_private") or "").strip() in {"1", "true", "on", "yes"}

    if not repo_url and project.repository:
        db.session.delete(project.repository)
        try:
            db.session.commit()
            flash("Repository-Verknuepfung wurde entfernt.", "success")
        except Exception:
            db.session.rollback()
            logger.exception("Failed to delete repository settings for project id=%s", project.id)
            flash("Repository-Verknuepfung konnte nicht entfernt werden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    if not repo_url:
        flash("Repository-URL darf nicht leer sein.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    repository = project.repository or Repository(project_id=project.id)
    repository.repo_url = repo_url
    repository.branch = branch
    repository.provider = provider
    repository.is_private = is_private
    # Keep existing token if field is intentionally left empty.
    if access_token:
        repository.access_token = access_token

    db.session.add(repository)
    try:
        db.session.commit()
        flash("Repository-Einstellungen wurden gespeichert.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save repository settings for project id=%s", project.id)
        flash("Repository-Einstellungen konnten nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/env")
def add_project_env(project_id: int):
    project = Project.query.get_or_404(project_id)
    env_lines = request.form.get("env_lines") or ""

    if env_lines.strip():
        summary = _upsert_project_environment(project, env_lines)
        try:
            db.session.commit()
            flash(
                (
                    "Environment-Variablen gespeichert. "
                    f"Neu: {summary['created']}, aktualisiert: {summary['updated']}."
                ),
                "success",
            )
        except Exception:
            db.session.rollback()
            logger.exception("Failed to bulk upsert env vars for project id=%s", project.id)
            flash("Environment-Variablen konnten nicht gespeichert werden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    key = (request.form.get("key") or "").strip()
    value = request.form.get("value") or ""

    if not key:
        flash("Environment-Variable konnte nicht gespeichert werden: Key darf nicht leer sein.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    existing = EnvironmentVariable.query.filter_by(project_id=project.id, key=key).first()
    if existing:
        flash(f"Environment-Variable '{key}' existiert bereits.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    env_var = EnvironmentVariable(
        project_id=project.id,
        key=key,
        value=value,
        is_secret=_is_secret_env_key(key),
    )
    db.session.add(env_var)
    try:
        db.session.commit()
        flash(f"Environment-Variable '{key}' gespeichert.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to create env var for project id=%s", project.id)
        flash("Environment-Variable konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/servers/import")
def import_project_server(project_id: int):
    project = Project.query.get_or_404(project_id)
    provider_server_id = (request.form.get("provider_server_id") or "").strip()

    if not provider_server_id:
        flash("Bitte eine Hetzner Server-ID angeben.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    try:
        provisioned = HetznerClient().get_server(provider_server_id, force_live=True)
    except HetznerAPIError as exc:
        logger.warning("Failed to import Hetzner server %s for project id=%s: %s", provider_server_id, project.id, exc)
        flash(f"Hetzner-Server konnte nicht geladen werden: {exc}", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))
    except requests.exceptions.RequestException as exc:
        logger.warning("Network error while importing Hetzner server %s for project id=%s: %s", provider_server_id, project.id, exc)
        flash("Hetzner-Server konnte wegen eines Netzwerkfehlers nicht geladen werden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    server = Server.query.filter_by(provider="hetzner", provider_server_id=provider_server_id).first()
    if server is None:
        server = Server(project_id=project.id, provider="hetzner", provider_server_id=provider_server_id, name=provisioned.name)
        db.session.add(server)

    server.project_id = project.id
    server.name = provisioned.name
    server.server_type = provisioned.server_type
    server.region = provisioned.location
    server.ipv4 = provisioned.ipv4
    server.status = provisioned.status

    try:
        db.session.commit()
        flash(f"Hetzner-Server '{server.name}' wurde dem Projekt zugeordnet.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save imported Hetzner server %s for project id=%s", provider_server_id, project.id)
        flash("Hetzner-Server konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/servers/<int:server_id>/activate")
def activate_project_server(project_id: int, server_id: int):
    project = Project.query.get_or_404(project_id)
    server = Server.query.filter_by(project_id=project.id, id=server_id).first()

    if not server:
        flash("Server nicht gefunden oder gehoert nicht zu diesem Projekt.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    project.active_server_id = server.id
    try:
        db.session.commit()
        flash(f"Server '{server.name}' ist jetzt aktiv fuer Deployments.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to activate server id=%s for project id=%s", server.id, project.id)
        flash("Aktiver Server konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/cleanup")
def cleanup_project_server_artifacts(project_id: int):
    project = Project.query.get_or_404(project_id)
    selected_server_id = (request.form.get("server_id") or "").strip()

    selected_server = None
    if selected_server_id:
        try:
            selected_server = Server.query.filter_by(project_id=project.id, id=int(selected_server_id)).first()
        except ValueError:
            selected_server = None
        if selected_server is None:
            flash("Ausgewaehlter Zielserver wurde nicht gefunden.", "error")
            return redirect(url_for("dashboard.project_detail", project_id=project.id))

    target_server = selected_server or _resolve_target_server(project)
    if target_server is None or not target_server.ipv4:
        flash("Kein Zielserver mit gueltiger IP fuer Cleanup gefunden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    executor = DeploymentExecutor()
    ctx = PipelineContext(
        project_name=project.name,
        slug=project.slug,
        framework=project.framework or "flask",
        domain=project.domain,
        repository_url=project.repository.repo_url if project.repository else None,
        repository_branch=project.repository.branch if project.repository and project.repository.branch else project.branch,
    )

    try:
        results = executor.cleanup_project_from_server(target_server.ipv4, ctx)
        failed = [item for item in results if item.return_code != 0]
        if failed:
            message = _format_command_results(results)
            flash("Projekt-Cleanup ist teilweise fehlgeschlagen. Details siehe Log.", "error")
            logger.error("Project cleanup failed project_id=%s server_id=%s\n%s", project.id, target_server.id, message)
            return redirect(url_for("dashboard.project_detail", project_id=project.id))

        project.status = "draft"
        db.session.add(
            ActivityLog(
                project_id=project.id,
                action="project.cleanup.completed",
                actor="dashboard",
                message=f"Server cleanup completed on {target_server.name} ({target_server.ipv4})",
            )
        )
        db.session.commit()
        flash("Projekt wurde auf dem Server rueckstandslos bereinigt. Du kannst jetzt neu deployen.", "success")
    except Exception as exc:
        db.session.rollback()
        logger.exception("Project cleanup failed project_id=%s server_id=%s", project.id, target_server.id)
        flash(f"Projekt-Cleanup fehlgeschlagen: {exc}", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/env/<int:env_id>/delete")
def delete_project_env(project_id: int, env_id: int):
    project = Project.query.get_or_404(project_id)
    env_var = EnvironmentVariable.query.filter_by(project_id=project.id, id=env_id).first()

    if not env_var:
        flash("Environment-Variable nicht gefunden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    key = env_var.key
    db.session.delete(env_var)
    try:
        db.session.commit()
        flash(f"Environment-Variable '{key}' geloescht.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to delete env var id=%s for project id=%s", env_id, project.id)
        flash("Environment-Variable konnte nicht geloescht werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/deploy")
def deploy_project(project_id: int):
    project = Project.query.get_or_404(project_id)
    selected_server_id = (request.form.get("server_id") or "").strip()

    selected_server = None
    if selected_server_id:
        try:
            selected_server = Server.query.filter_by(project_id=project.id, id=int(selected_server_id)).first()
        except ValueError:
            selected_server = None
        if selected_server is None:
            flash("Ausgewaehlter Zielserver wurde nicht gefunden.", "error")
            return redirect(url_for("dashboard.project_detail", project_id=project.id))

    deployment, created_ok = _create_and_queue_deployment(
        project,
        selected_server=selected_server,
        mode=(request.form.get("mode") or "production").strip() or "production",
        commit_sha=(request.form.get("commit_sha") or "").strip() or None,
        trigger_source="dashboard",
    )
    if not created_ok or deployment is None:
        flash("Deployment konnte nicht erstellt werden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))


@bp.post("/projects/<int:project_id>/redeploy")
def redeploy_project(project_id: int):
    project = Project.query.options(joinedload(Project.servers), joinedload(Project.active_server)).filter_by(id=project_id).first()
    if not project:
        abort(404)

    preferred_server = _resolve_target_server(project)
    deployment, created_ok = _create_and_queue_deployment(
        project,
        selected_server=preferred_server,
        mode="production",
        commit_sha=None,
        trigger_source="dashboard-redeploy",
    )
    if not created_ok or deployment is None:
        flash("Redeploy konnte nicht erstellt werden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    if preferred_server:
        flash(
            f"Redeploy gestartet: Server '{preferred_server.name}' wird wiederverwendet.",
            "success",
        )
    else:
        flash("Redeploy gestartet: Kein bestehender Server verfuegbar, Server wird bei Bedarf neu provisioniert.", "warning")

    return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))


@bp.get("/projects/<int:project_id>/runtime-logs")
def project_runtime_logs(project_id: int):
    project = Project.query.options(
        joinedload(Project.servers),
        joinedload(Project.active_server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    target_server, error_message = _resolve_selected_project_server(project, request.args.get("server_id"))
    if error_message:
        return jsonify({"error": error_message}), 400

    deploy_dir = f"/opt/orbital/{project.slug}"
    command = f"docker compose -f {deploy_dir}/docker-compose.yml logs --tail=200 web"

    executor = DeploymentExecutor()
    result = executor.ssh.run_one(target_server.ipv4, command)

    payload = {
        "project_id": project.id,
        "project_slug": project.slug,
        "server": {
            "id": target_server.id,
            "name": target_server.name,
            "ipv4": target_server.ipv4,
        },
        "command": result.command,
        "exit_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(payload), (200 if result.return_code == 0 else 500)


@bp.get("/projects/<int:project_id>/container-status")
def project_container_status(project_id: int):
    project = Project.query.options(
        joinedload(Project.servers),
        joinedload(Project.active_server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    target_server, error_message = _resolve_selected_project_server(project, request.args.get("server_id"))
    if error_message:
        return jsonify({"error": error_message}), 400

    deploy_dir = f"/opt/orbital/{project.slug}"
    compose_file = f"{deploy_dir}/docker-compose.yml"
    executor = DeploymentExecutor()

    id_cmd = f"docker compose -f {compose_file} ps -q web"
    id_result = executor.ssh.run_one(target_server.ipv4, id_cmd)
    container_id = (id_result.stdout or "").strip()

    if id_result.return_code != 0:
        payload = {
            "project_id": project.id,
            "project_slug": project.slug,
            "server": {
                "id": target_server.id,
                "name": target_server.name,
                "ipv4": target_server.ipv4,
            },
            "status": "error",
            "container": None,
            "command": id_result.command,
            "exit_code": id_result.return_code,
            "stdout": id_result.stdout,
            "stderr": id_result.stderr,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        return jsonify(payload), 500

    if not container_id:
        payload = {
            "project_id": project.id,
            "project_slug": project.slug,
            "server": {
                "id": target_server.id,
                "name": target_server.name,
                "ipv4": target_server.ipv4,
            },
            "status": "not_found",
            "container": None,
            "command": id_result.command,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        return jsonify(payload), 200

    inspect_cmd = (
        f"docker ps -a --filter id={container_id} "
        "--format \"{{.ID}}\\t{{.Names}}\\t{{.Status}}\\t{{.Ports}}\""
    )
    inspect_result = executor.ssh.run_one(target_server.ipv4, inspect_cmd)
    raw = (inspect_result.stdout or "").strip()
    columns = raw.split("\t") if raw else []
    container_name = columns[1] if len(columns) > 1 else f"{project.slug}-web"
    container_status_text = columns[2] if len(columns) > 2 else "unknown"
    container_ports = columns[3] if len(columns) > 3 else ""
    running = container_status_text.lower().startswith("up")

    payload = {
        "project_id": project.id,
        "project_slug": project.slug,
        "server": {
            "id": target_server.id,
            "name": target_server.name,
            "ipv4": target_server.ipv4,
        },
        "status": "running" if running else "stopped",
        "container": {
            "id": container_id,
            "name": container_name,
            "status": container_status_text,
            "ports": container_ports,
        },
        "command": inspect_result.command,
        "exit_code": inspect_result.return_code,
        "stdout": inspect_result.stdout,
        "stderr": inspect_result.stderr,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(payload), (200 if inspect_result.return_code == 0 else 500)


@bp.post("/projects/<int:project_id>/container-restart")
def restart_project_container(project_id: int):
    project = Project.query.options(
        joinedload(Project.servers),
        joinedload(Project.active_server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    target_server, error_message = _resolve_selected_project_server(project, request.form.get("server_id"))
    if error_message:
        flash(error_message, "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    deploy_dir = f"/opt/orbital/{project.slug}"
    command = f"docker compose -f {deploy_dir}/docker-compose.yml restart web"
    result = DeploymentExecutor().ssh.run_one(target_server.ipv4, command)

    if result.return_code == 0:
        flash("Web-Container wurde neu gestartet.", "success")
    else:
        flash(
            "Container-Neustart fehlgeschlagen: "
            f"{(result.stderr or '').strip() or (result.stdout or '').strip() or '-'}",
            "error",
        )
    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/container-delete")
def delete_project_container(project_id: int):
    project = Project.query.options(
        joinedload(Project.servers),
        joinedload(Project.active_server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    target_server, error_message = _resolve_selected_project_server(project, request.form.get("server_id"))
    if error_message:
        flash(error_message, "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

    deploy_dir = f"/opt/orbital/{project.slug}"
    commands = [
        f"docker compose -f {deploy_dir}/docker-compose.yml stop web",
        f"docker compose -f {deploy_dir}/docker-compose.yml rm -f -s web",
    ]
    results = DeploymentExecutor().ssh.run_many(target_server.ipv4, commands)
    failed = [item for item in results if item.return_code != 0]

    if not failed:
        flash("Web-Container wurde gestoppt und geloescht.", "success")
    else:
        detail = "; ".join((item.stderr or item.stdout or item.command).strip() for item in failed)
        flash(f"Container-Loeschen fehlgeschlagen: {detail}", "error")
    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.get("/deployments/<int:deployment_id>")
def deployment_detail(deployment_id: int):
    deployment = (
        Deployment.query.options(
            joinedload(Deployment.project).joinedload(Project.servers),
            joinedload(Deployment.project).joinedload(Project.active_server),
            joinedload(Deployment.server),
            joinedload(Deployment.steps),
        )
        .filter_by(id=deployment_id)
        .first()
    )
    if not deployment:
        abort(404)

    steps = sorted(deployment.steps, key=lambda s: (s.order_index, s.id))
    latest_failed_step = _latest_failed_step(deployment)
    deployment_error_analysis = _extract_deployment_error_analysis(deployment, latest_failed_step)
    step_status = {step.name: step.status for step in steps}
    target_server = _resolve_target_server(deployment.project, deployment)
    project_runtime_state = _refresh_project_runtime_state(deployment.project, allow_healthcheck=False)
    deployment_suggestions = generate_deployment_suggestions(deployment, runtime_state=project_runtime_state)
    autofix_decision = suggest_autofix_action(deployment, runtime_state=project_runtime_state)
    fix_suggestion = _autofix_action_ui(autofix_decision.get("recommended_fix_action"))
    if not autofix_decision.get("recommended_fix_action"):
        fix_suggestion = _suggest_fix_issue(deployment_error_analysis, latest_failed_step.name if latest_failed_step else None)
    autofix_history = deployment.autofix_history_json if isinstance(deployment.autofix_history_json, list) else []
    db.session.commit()
    return render_template(
        "dashboard/deployment_detail.html",
        deployment=deployment,
        steps=steps,
        deployment_error_analysis=deployment_error_analysis,
        deployment_suggestions=deployment_suggestions,
        fix_suggestion=fix_suggestion,
        autofix_decision=autofix_decision,
        autofix_history=autofix_history,
        project_runtime_state=project_runtime_state,
        target_server=target_server,
        provision_server_ok=step_status.get("provision_server") == "success",
        wait_for_ssh_ok=step_status.get("wait_for_ssh") == "success",
        healthcheck_ok=step_status.get("healthcheck") == "success",
    )


@bp.post("/deployments/<int:deployment_id>/fix-issue")
def fix_deployment_issue(deployment_id: int):
    deployment = (
        Deployment.query.options(
            joinedload(Deployment.project).joinedload(Project.servers),
            joinedload(Deployment.project).joinedload(Project.active_server),
            joinedload(Deployment.server),
            joinedload(Deployment.steps),
        )
        .filter_by(id=deployment_id)
        .first()
    )
    if not deployment:
        abort(404)

    if deployment.status != "failed":
        flash("Fix Issue ist nur fuer fehlgeschlagene Deployments verfuegbar.", "warning")
        return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))

    failed_step = _latest_failed_step(deployment)
    error_analysis = _extract_deployment_error_analysis(deployment, failed_step)
    runtime_state = _refresh_project_runtime_state(deployment.project, allow_healthcheck=False)
    autofix_decision = suggest_autofix_action(deployment, runtime_state=runtime_state)
    suggestion = _autofix_action_ui(autofix_decision.get("recommended_fix_action"))
    requested_action = (request.form.get("action") or "").strip()
    action = requested_action or suggestion["action"]

    project = deployment.project
    target_server = _resolve_target_server(project, deployment)
    if action in {"restart_container", "reload_nginx"}:
        if target_server is None or not target_server.ipv4:
            flash("Fix Issue konnte nicht ausgefuehrt werden: Kein Zielserver mit gueltiger IP gefunden.", "error")
            return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))

    executor = DeploymentExecutor()

    manual_decision = {
        "detected_error_type": (error_analysis or {}).get("error_type") or "unknown_error",
        "recommended_fix_action": action,
        "confidence": (error_analysis or {}).get("confidence") or 0.5,
        "safe_to_execute_automatically": False,
        "trigger_reason": "manual_fix_issue",
    }
    entry = execute_autofix(
        deployment,
        decision=manual_decision,
        project_slug=project.slug,
        target_host=target_server.ipv4 if target_server else None,
        executor=executor,
        auto_trigger=False,
        step_names=STEP_NAMES,
        queue_retry=lambda new_deployment_id: run_deployment_task.delay(new_deployment_id),
    )
    flash(_autofix_result_message(entry), "success" if entry.get("success") else "error")

    if action == "retry_deploy" and entry.get("success") and entry.get("new_deployment_id"):
        return redirect(url_for("dashboard.deployment_detail", deployment_id=entry["new_deployment_id"]))
    return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))


@bp.get("/projects/<int:project_id>/ssl-info")
def project_ssl_info(project_id: int):
    project = Project.query.options(
        joinedload(Project.servers),
        joinedload(Project.active_server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    target_server, error_message = _resolve_selected_project_server(project, request.args.get("server_id"))
    if error_message:
        return jsonify({"error": error_message}), 400

    result = DeploymentExecutor().ssh.run_one(target_server.ipv4, "certbot certificates")
    raw = (result.stdout or "").strip()

    certs = []
    current: dict | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("Certificate Name:"):
            if current is not None:
                certs.append(current)
            current = {"name": stripped.split(":", 1)[1].strip()}
        elif current is not None:
            if stripped.startswith("Domains:"):
                current["domains"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Expiry Date:"):
                current["expiry"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Certificate Path:"):
                current["certificate_path"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Private Key Path:"):
                current["private_key_path"] = stripped.split(":", 1)[1].strip()
    if current is not None:
        certs.append(current)

    return jsonify({
        "project_id": project.id,
        "server": {"id": target_server.id, "name": target_server.name, "ipv4": target_server.ipv4},
        "certificates": certs,
        "raw_output": raw,
        "exit_code": result.return_code,
        "stderr": result.stderr,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }), (200 if result.return_code == 0 else 500)


@bp.post("/projects/<int:project_id>/ssl-run")
def project_ssl_run(project_id: int):
    project = Project.query.options(
        joinedload(Project.servers),
        joinedload(Project.active_server),
    ).filter_by(id=project_id).first()
    if not project:
        abort(404)

    if not project.domain:
        return jsonify({"error": "Keine Domain konfiguriert."}), 400

    target_server, error_message = _resolve_selected_project_server(
        project, request.form.get("server_id") or request.args.get("server_id")
    )
    if error_message:
        return jsonify({"error": error_message}), 400

    domain = project.domain.strip()
    server_ip = target_server.ipv4 or ""

    # DNS pre-check: domain must point to this server before certbot can succeed.
    dns_result = DeploymentExecutor().check_dns(domain, server_ip)
    if not dns_result.matches:
        return jsonify({
            "error": (
                f"DNS-Fehler: {domain} zeigt auf {dns_result.resolved_ip}, "
                f"erwartet wird {dns_result.expected_ip}. "
                "Bitte den A-Record aktualisieren und danach erneut versuchen. "
                "Let's Encrypt kann das Zertifikat erst ausstellen wenn die Domain auf diesen Server zeigt."
            ),
            "resolved_ip": dns_result.resolved_ip,
            "expected_ip": dns_result.expected_ip,
        }), 400

    executor = DeploymentExecutor()
    result = executor.ssh.run_one(
        server_ip,
        f"certbot --nginx -d {domain} --non-interactive --agree-tos -m admin@{domain}",
    )
    return jsonify({
        "project_id": project.id,
        "domain": domain,
        "server": {"id": target_server.id, "name": target_server.name, "ipv4": server_ip},
        "exit_code": result.return_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }), (200 if result.return_code == 0 else 500)


@bp.get("/deployments/<int:deployment_id>/status")
def deployment_status(deployment_id: int):
    deployment = (
        Deployment.query.options(
            joinedload(Deployment.steps),
            joinedload(Deployment.project),
        )
        .filter_by(id=deployment_id)
        .first()
    )
    if not deployment:
        abort(404)

    steps = sorted(deployment.steps, key=lambda s: (s.order_index, s.id))
    terminal_steps = [step for step in steps if step.status in {"success", "failed"}]
    running_step = next((step for step in steps if step.status == "running"), None)
    total_steps = len(steps) if steps else len(STEP_NAMES)
    progress_percent = int((len(terminal_steps) / total_steps) * 100) if total_steps else 0

    payload = {
        "deployment": {
            "id": deployment.id,
            "status": deployment.status,
            "error_message": deployment.error_message,
            "autofix_status": deployment.autofix_status,
            "autofix_attempt_count": deployment.autofix_attempt_count,
            "last_autofix_action": deployment.last_autofix_action,
            "last_autofix_at": deployment.last_autofix_at.isoformat() if deployment.last_autofix_at else None,
            "autofix_history_json": deployment.autofix_history_json,
            "updated_at": deployment.updated_at.isoformat(),
        },
        "progress": {
            "total_steps": total_steps,
            "done_steps": len(terminal_steps),
            "percent": progress_percent,
            "running_step": running_step.name if running_step else None,
        },
        "steps": [
            {
                "id": step.id,
                "name": step.name,
                "status": step.status,
                "order_index": step.order_index,
                "started_at": step.started_at.isoformat() if step.started_at else None,
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                "stdout": (step.stdout if step.stdout is not None else step.output) or "",
                "stderr": (step.stderr if step.stderr is not None else step.error_message) or "",
                "exit_code": step.exit_code,
                "json_details": step.json_details,
                # Legacy keys (compatibility)
                "output": (step.stdout if step.stdout is not None else step.output) or "",
                "error_message": (step.stderr if step.stderr is not None else step.error_message) or "",
                "created_at": step.created_at.isoformat(),
                "updated_at": step.updated_at.isoformat(),
            }
            for step in steps
        ],
    }
    return jsonify(payload)
