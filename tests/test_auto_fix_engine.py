from __future__ import annotations

from app.extensions import db
from app.models import Deployment, DeploymentStep, Project, Server
from app.services.auto_fix import (
    execute_autofix,
    retry_failed_deployment,
    suggest_autofix_action,
)
from app.services.ssh import CommandResult


class _FakeSSH:
    def __init__(self, results):
        self._results = results

    def run_many(self, host: str, commands):
        return self._results


class _FakeExecutor:
    def __init__(self, results):
        self.ssh = _FakeSSH(results)


def _seed_failed_deployment() -> Deployment:
    project = Project(name="AutoFix Project", slug="autofix-project", environment="production", status="live")
    db.session.add(project)
    db.session.flush()

    server = Server(
        project_id=project.id,
        name="srv-1",
        provider="hetzner",
        provider_server_id="srv-1",
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
        status="failed",
        mode="production",
        trigger_source="test",
        error_message="Healthcheck timeout while connecting",
        error_analysis_json={
            "error_type": "db_connection",
            "confidence": 0.61,
            "affected_step": "healthcheck",
        },
    )
    db.session.add(deployment)
    db.session.flush()

    db.session.add(
        DeploymentStep(
            deployment_id=deployment.id,
            name="healthcheck",
            status="failed",
            order_index=1,
            stderr="connection refused",
        )
    )
    db.session.commit()
    return deployment


def test_suggest_autofix_action_runtime_instability(app):
    with app.app_context():
        deployment = _seed_failed_deployment()

        decision = suggest_autofix_action(
            deployment,
            runtime_state={"current_runtime_status": "failed", "reason": "Healthcheck nicht erreichbar"},
        )

        assert decision["detected_error_type"] == "db_connection"
        assert decision["recommended_fix_action"] == "restart_container"
        assert decision["safe_to_execute_automatically"] is True


def test_execute_autofix_restart_container_logs_structured_attempt(app):
    with app.app_context():
        deployment = _seed_failed_deployment()
        fake_executor = _FakeExecutor(
            [
                CommandResult(command="test compose", return_code=0, stdout="ok", stderr=""),
                CommandResult(command="restart web", return_code=0, stdout="restarted", stderr=""),
            ]
        )

        entry = execute_autofix(
            deployment,
            decision={
                "detected_error_type": "db_connection",
                "recommended_fix_action": "restart_container",
                "confidence": 0.8,
                "safe_to_execute_automatically": True,
                "trigger_reason": "test_runtime_failed",
            },
            project_slug="autofix-project",
            target_host="127.0.0.1",
            executor=fake_executor,
            auto_trigger=True,
            step_names=["healthcheck"],
        )

        db.session.refresh(deployment)
        assert entry["action_name"] == "restart_container"
        assert entry["execution_result"] == "container_restarted"
        assert entry["success"] is True
        assert deployment.autofix_attempt_count == 1
        assert isinstance(deployment.autofix_history_json, list)
        assert deployment.autofix_history_json[-1]["trigger_reason"] == "test_runtime_failed"


def test_retry_failed_deployment_respects_retry_limit(app):
    with app.app_context():
        deployment = _seed_failed_deployment()
        deployment.autofix_history_json = [{"action_name": "retry_deploy"}]
        db.session.commit()

        result = retry_failed_deployment(deployment, step_names=["provision_server", "healthcheck"])

        assert result["success"] is False
        assert result["execution_result"] == "retry_limit_reached"


def test_retry_failed_deployment_creates_new_deployment(app):
    with app.app_context():
        deployment = _seed_failed_deployment()

        queued_ids: list[int] = []

        def _queue(new_deployment_id: int):
            queued_ids.append(new_deployment_id)

        result = retry_failed_deployment(
            deployment,
            step_names=["provision_server", "healthcheck"],
            queue_retry=_queue,
        )

        assert result["success"] is True
        assert result["execution_result"] == "retry_deploy_started"
        assert queued_ids

        created = Deployment.query.filter_by(id=result["new_deployment_id"]).first()
        assert created is not None
        assert created.project_id == deployment.project_id
        assert len(created.steps) == 2
