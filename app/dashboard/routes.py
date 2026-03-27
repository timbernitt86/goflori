import logging
import threading

import requests.exceptions
from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.dashboard import bp
from app.extensions import db
from app.models import Deployment, DeploymentStep, EnvironmentVariable, Project, ProviderSetting, Repository, Server
from app.services.hetzner import HetznerAPIError, HetznerClient
from app.tasks.deployment import STEP_NAMES, run_deployment_task

logger = logging.getLogger(__name__)


def _is_task_queue_usable() -> tuple[bool, str | None]:
    broker_url = ((current_app.config.get("CELERY") or {}).get("broker_url") or "").strip()
    if broker_url.startswith("redis://"):
        try:
            import redis  # noqa: F401
        except Exception as exc:
            return False, f"redis client missing in current interpreter: {exc}"
    return True, None


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


def _resolve_target_server(project: Project, deployment: Deployment | None = None) -> Server | None:
    """Resolve target machine for a deployment.

    Priority order:
    1) explicit deployment.server assignment
    2) project.active_server
    3) inferred fallback from project server timestamps
    """
    if deployment and deployment.server is not None:
        return deployment.server
    if project.active_server is not None:
        return project.active_server

    servers = sorted(project.servers, key=lambda s: s.created_at)
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
        default_location="nbg1",
        default_server_type="cx22",
        default_image="ubuntu-24.04",
    )


def _get_hetzner_defaults() -> dict[str, str | None]:
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    return {
        "default_server_type": setting.default_server_type if setting else None,
        "default_location": setting.default_location if setting else None,
        "default_image": setting.default_image if setting else None,
        "ssh_key_name": setting.ssh_key_name if setting else None,
    }


def _apply_hetzner_settings_from_form(setting: ProviderSetting) -> None:
    api_token = (request.form.get("api_token") or "").strip()
    default_location = (request.form.get("default_location") or "").strip() or None
    default_server_type = (request.form.get("default_server_type") or "").strip() or None
    default_image = (request.form.get("default_image") or "").strip() or None
    ssh_key_name = (request.form.get("ssh_key_name") or "").strip() or None
    ssh_public_key = (request.form.get("ssh_public_key") or "").strip() or None

    # Keep existing token when field is intentionally left empty.
    if api_token:
        setting.api_token = api_token

    setting.default_location = default_location
    setting.default_server_type = default_server_type
    setting.default_image = default_image
    setting.ssh_key_name = ssh_key_name
    # Keep existing public key when field is intentionally left empty.
    if ssh_public_key:
        setting.ssh_public_key = ssh_public_key


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
    deployment_counts = dict(
        db.session.query(Deployment.project_id, func.count(Deployment.id)).group_by(Deployment.project_id).all()
    )
    projects_data = (
        Project.query.options(joinedload(Project.repository)).order_by(Project.created_at.desc()).all()
    )
    return render_template(
        "dashboard/projects.html",
        projects=projects_data,
        deployment_counts=deployment_counts,
        hetzner_defaults=_get_hetzner_defaults(),
    )


@bp.post("/projects")
def create_project():
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
    repository_provider = (request.form.get("repository_provider") or "github").strip() or "github"
    env_lines = (request.form.get("env_lines") or "").splitlines()

    if not name:
        flash("Projektname ist erforderlich.", "error")
        return redirect(url_for("dashboard.projects"))

    final_slug = slug or Project.slugify(name)
    project = Project(
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
        project.repository = Repository(provider=repository_provider, url=repository_url, branch=branch)

    for line in env_lines:
        item = line.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        project.environment_variables.append(
            EnvironmentVariable(
                key=key,
                value=value.strip(),
                is_secret=key.upper().endswith("KEY")
                or key.upper().endswith("TOKEN")
                or key.upper().endswith("SECRET")
                or key.upper().endswith("PASSWORD"),
            )
        )

    db.session.add(project)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Projekt konnte nicht erstellt werden (Slug vermutlich bereits vergeben).", "error")
        return redirect(url_for("dashboard.projects"))

    flash(f"Projekt '{project.name}' wurde erstellt.", "success")
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
    latest_server = _resolve_target_server(project)
    domain_target_ip = latest_server.ipv4 if project.domain and latest_server else None
    deployment_meta = {}
    for deployment in deployments:
        steps = sorted(deployment.steps, key=lambda s: (s.order_index, s.id))
        failed_steps = [step for step in steps if step.status == "failed"]
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
            "target_machine": target_server,
        }

    return render_template(
        "dashboard/project_detail.html",
        project=project,
        deployments=deployments,
        deployment_meta=deployment_meta,
        latest_server=latest_server,
        domain_target_ip=domain_target_ip,
        hetzner_defaults=hetzner_defaults,
        effective_server_type=project.desired_server_type or hetzner_defaults.get("default_server_type"),
        effective_location=project.desired_location or hetzner_defaults.get("default_location"),
        effective_image=project.desired_image or hetzner_defaults.get("default_image"),
    )


@bp.post("/projects/<int:project_id>/infrastructure")
def save_project_infrastructure(project_id: int):
    project = Project.query.get_or_404(project_id)

    project.domain = (request.form.get("domain") or "").strip() or None
    project.desired_server_type = (request.form.get("desired_server_type") or "").strip() or None
    project.desired_location = (request.form.get("desired_location") or "").strip() or None
    project.desired_image = (request.form.get("desired_image") or "").strip() or None

    try:
        db.session.commit()
        flash("Projekt-Infrastruktur wurde gespeichert.", "success")
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save project infrastructure for project id=%s", project.id)
        flash("Projekt-Infrastruktur konnte nicht gespeichert werden.", "error")

    return redirect(url_for("dashboard.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/env")
def add_project_env(project_id: int):
    project = Project.query.get_or_404(project_id)
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
        is_secret=key.upper().endswith("KEY")
        or key.upper().endswith("TOKEN")
        or key.upper().endswith("SECRET")
        or key.upper().endswith("PASSWORD"),
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
    deployment = None
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

    try:
        deployment = Deployment(
            project_id=project.id,
            server_id=selected_server.id if selected_server else None,
            status="pending",
            mode=(request.form.get("mode") or "staging").strip() or "staging",
            commit_sha=(request.form.get("commit_sha") or "").strip() or None,
            trigger_source="dashboard",
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
            "Dashboard created deployment id=%s for project id=%s",
            deployment.id,
            project.id,
        )
    except Exception:
        db.session.rollback()
        logger.exception("Failed to create deployment for project id=%s", project.id)
        flash("Deployment konnte nicht erstellt werden.", "error")
        return redirect(url_for("dashboard.project_detail", project_id=project.id))

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
        logger.exception(
            "Failed to queue deployment id=%s for project id=%s",
            deployment.id,
            project.id,
        )

        if current_app.config.get("ORBITAL_INLINE_DEPLOY_ON_QUEUE_ERROR", False):
            logger.warning(
                "Queue unavailable for deployment id=%s. Falling back to inline execution.",
                deployment.id,
            )
            _run_deployment_inline_async(deployment.id)
            flash(
                "Queue nicht erreichbar. Deployment laeuft im Hintergrund (Inline-Fallback asynchron).",
                "warning",
            )
        else:
            deployment.status = "failed"
            deployment.error_message = f"Task queue error: {exc}"
            db.session.commit()
            flash("Deployment wurde angelegt, konnte aber nicht in die Queue gestellt werden.", "error")

    return redirect(url_for("dashboard.deployment_detail", deployment_id=deployment.id))


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
    step_status = {step.name: step.status for step in steps}
    target_server = _resolve_target_server(deployment.project, deployment)
    return render_template(
        "dashboard/deployment_detail.html",
        deployment=deployment,
        steps=steps,
        target_server=target_server,
        provision_server_ok=step_status.get("provision_server") == "success",
        wait_for_ssh_ok=step_status.get("wait_for_ssh") == "success",
        healthcheck_ok=step_status.get("healthcheck") == "success",
    )


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
                "output": step.output or "",
                "error_message": step.error_message or "",
                "created_at": step.created_at.isoformat(),
                "updated_at": step.updated_at.isoformat(),
            }
            for step in steps
        ],
    }
    return jsonify(payload)
