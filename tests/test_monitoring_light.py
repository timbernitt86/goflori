from __future__ import annotations

from types import SimpleNamespace

import requests

from app.extensions import db
from app.models import Deployment, DeploymentStep, Project, Server
from app.services.monitoring_light import (
    check_app_reachability,
    check_container_status,
    compute_light_monitoring_status,
)
from app.services.ssh import CommandResult


def _seed_project_with_active_deployment() -> Project:
    project = Project(name="Monitor Project", slug="monitor-project", environment="production", status="live")
    db.session.add(project)
    db.session.flush()

    server = Server(
        project_id=project.id,
        name="srv-monitor",
        provider="hetzner",
        provider_server_id="srv-monitor",
        server_type="cx22",
        region="nbg1",
        ipv4="127.0.0.1",
        status="running",
    )
    db.session.add(server)
    db.session.flush()

    deployment = Deployment(
        project_id=project.id,
        server_id=server.id,
        status="success",
        successful=True,
        mode="production",
        trigger_source="test",
        error_analysis_json={"probable_cause": "Container hat zuletzt nicht korrekt geantwortet."},
    )
    db.session.add(deployment)
    db.session.flush()

    db.session.add(
        DeploymentStep(
            deployment_id=deployment.id,
            name="healthcheck",
            status="success",
            order_index=1,
        )
    )

    project.active_server_id = server.id
    project.active_deployment_id = deployment.id
    db.session.commit()
    return project


def test_check_app_reachability_prefers_health_path(app, monkeypatch):
    with app.app_context():
        project = _seed_project_with_active_deployment()

        class _Resp:
            status_code = 200
            history = []

        called_urls: list[str] = []

        def _fake_get(url: str, timeout: int, allow_redirects: bool):
            called_urls.append(url)
            return _Resp()

        monkeypatch.setattr(requests, "get", _fake_get)

        result = check_app_reachability(project, persist=False)

        assert result["success"] is True
        assert result["target_url"].endswith("/health")
        assert called_urls[0].endswith("/health")


def test_check_container_status_reports_running_container(app, monkeypatch):
    with app.app_context():
        project = _seed_project_with_active_deployment()

        class _FakeExecutor:
            def __init__(self):
                self.ssh = SimpleNamespace(
                    run_one=lambda host, command: CommandResult(
                        command=command,
                        return_code=0,
                        stdout="NAME                IMAGE               COMMAND             SERVICE   STATUS\nmonitor-project-web  myimg               \"start\"           web       Up 12 seconds",
                        stderr="",
                    )
                )

        monkeypatch.setattr("app.services.execution.DeploymentExecutor", _FakeExecutor)

        result = check_container_status(project)

        assert result["running"] is True
        assert result["container_names"]


def test_compute_light_monitoring_status_critical_with_unreachable_app(app, monkeypatch):
    with app.app_context():
        project = _seed_project_with_active_deployment()

        def _fake_get(url: str, timeout: int, allow_redirects: bool):
            raise requests.ConnectionError("connection refused")

        class _FakeExecutor:
            def __init__(self):
                self.ssh = SimpleNamespace(
                    run_one=lambda host, command: CommandResult(
                        command=command,
                        return_code=1,
                        stdout="",
                        stderr="container missing",
                    )
                )

        monkeypatch.setattr(requests, "get", _fake_get)
        monkeypatch.setattr("app.services.execution.DeploymentExecutor", _FakeExecutor)

        result = compute_light_monitoring_status(project, force_refresh=True, persist=False)

        assert result["monitoring_status"] == "critical"
        assert result["app_reachable"] is False
        assert result["container_running"] is False
        assert result["last_error_summary"]
