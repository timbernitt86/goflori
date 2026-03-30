from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.extensions import db
from app.models import Deployment, DeploymentStep
from app.services.execution import DeploymentExecutor


AUTOFIX_MAX_ATTEMPTS_PER_DEPLOYMENT = 3
AUTOFIX_MAX_AUTO_RETRY_DEPLOYS = 1


@dataclass(frozen=True)
class AutoFixDecision:
    detected_error_type: str
    recommended_fix_action: str | None
    confidence: float
    safe_to_execute_automatically: bool
    trigger_reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["confidence"] = round(float(self.confidence), 3)
        return payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _latest_failed_step(deployment: Deployment) -> DeploymentStep | None:
    steps = sorted(deployment.steps, key=lambda step: (step.order_index, step.id), reverse=True)
    return next((step for step in steps if step.status == "failed"), None)


def _normalize_runtime_state(runtime_state: Any) -> dict[str, Any]:
    if runtime_state is None:
        return {}
    if isinstance(runtime_state, dict):
        return runtime_state
    if hasattr(runtime_state, "__dict__"):
        return dict(runtime_state.__dict__)
    return {}


def _autofix_history(deployment: Deployment) -> list[dict[str, Any]]:
    return _safe_json_list(deployment.autofix_history_json)


def _count_retry_actions(history: list[dict[str, Any]]) -> int:
    return sum(1 for item in history if item.get("action_name") == "retry_deploy")


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(item in text for item in needles)


def _decision_no_action(error_type: str, confidence: float, reason: str) -> AutoFixDecision:
    return AutoFixDecision(
        detected_error_type=error_type,
        recommended_fix_action=None,
        confidence=confidence,
        safe_to_execute_automatically=False,
        trigger_reason=reason,
    )


def suggest_autofix_action(
    deployment: Deployment,
    *,
    runtime_state: dict[str, Any] | Any | None = None,
    max_attempts: int = AUTOFIX_MAX_ATTEMPTS_PER_DEPLOYMENT,
) -> dict[str, Any]:
    analysis = _safe_json_dict(deployment.error_analysis_json)
    error_type = str(analysis.get("error_type") or "unknown_error")
    confidence = float(analysis.get("confidence") or 0.2)

    attempt_count = int(deployment.autofix_attempt_count or 0)
    if attempt_count >= max_attempts:
        return _decision_no_action(error_type, confidence, "max_attempts_reached").to_dict()

    runtime = _normalize_runtime_state(runtime_state)
    runtime_status = str(runtime.get("current_runtime_status") or "").lower()
    runtime_reason = str(runtime.get("reason") or "").lower()

    failed_step = _latest_failed_step(deployment)
    affected_step = str(analysis.get("affected_step") or (failed_step.name if failed_step else "")).lower()

    if error_type == "missing_env":
        return _decision_no_action(error_type, confidence, "requires_manual_configuration").to_dict()

    if "nginx" in runtime_reason or affected_step == "configure_reverse_proxy":
        return AutoFixDecision(
            detected_error_type=error_type,
            recommended_fix_action="reload_nginx",
            confidence=max(confidence, 0.72),
            safe_to_execute_automatically=True,
            trigger_reason="nginx_related_issue",
        ).to_dict()

    if runtime_status in {"failed", "degraded"} and _contains_any(
        runtime_reason,
        (
            "healthcheck",
            "connection refused",
            "verbindung abgelehnt",
            "timeout",
            "max retries exceeded",
            "active version",
            "nicht erreichbar",
        ),
    ):
        return AutoFixDecision(
            detected_error_type=error_type,
            recommended_fix_action="restart_container",
            confidence=max(confidence, 0.75),
            safe_to_execute_automatically=True,
            trigger_reason="runtime_instability_detected",
        ).to_dict()

    # Controlled auto-retry only for transient/network style failure signals.
    lowered_error_message = str(deployment.error_message or "").lower()
    transient_signals = (
        "timed out",
        "timeout",
        "temporary failure",
        "network is unreachable",
        "connection reset",
        "tls handshake timeout",
    )
    history = _autofix_history(deployment)
    retry_count = _count_retry_actions(history)

    if retry_count < AUTOFIX_MAX_AUTO_RETRY_DEPLOYS and _contains_any(lowered_error_message, transient_signals):
        return AutoFixDecision(
            detected_error_type=error_type,
            recommended_fix_action="retry_deploy",
            confidence=max(confidence, 0.68),
            safe_to_execute_automatically=True,
            trigger_reason="transient_error_detected",
        ).to_dict()

    # Manual recommendation fallback.
    if error_type in {"build_fail", "port_conflict", "db_connection"}:
        return AutoFixDecision(
            detected_error_type=error_type,
            recommended_fix_action="retry_deploy",
            confidence=max(confidence, 0.6),
            safe_to_execute_automatically=False,
            trigger_reason="manual_review_recommended",
        ).to_dict()

    return _decision_no_action(error_type, confidence, "unknown_or_unsafe_error").to_dict()


def restart_active_container(
    *,
    host: str,
    project_slug: str,
    executor: DeploymentExecutor | None = None,
) -> dict[str, Any]:
    runner = executor or DeploymentExecutor()
    deploy_dir = f"/opt/orbital/{project_slug}"
    commands = [
        f"test -f {deploy_dir}/docker-compose.yml",
        f"docker compose -f {deploy_dir}/docker-compose.yml restart web",
    ]
    results = runner.ssh.run_many(host, commands)
    stdout = "\n\n".join((item.stdout or "").strip() for item in results if item.stdout)
    stderr = "\n\n".join((item.stderr or "").strip() for item in results if item.stderr)
    success = all(item.return_code == 0 for item in results)
    return {
        "execution_result": "container_restarted" if success else "container_restart_failed",
        "stdout": stdout,
        "stderr": stderr,
        "success": success,
    }


def reload_nginx_safely(*, host: str, executor: DeploymentExecutor | None = None) -> dict[str, Any]:
    runner = executor or DeploymentExecutor()
    results = runner.ssh.run_many(host, ["nginx -t", "systemctl reload nginx"])
    stdout = "\n\n".join((item.stdout or "").strip() for item in results if item.stdout)
    stderr = "\n\n".join((item.stderr or "").strip() for item in results if item.stderr)
    success = all(item.return_code == 0 for item in results)
    return {
        "execution_result": "nginx_reloaded" if success else "nginx_reload_failed",
        "stdout": stdout,
        "stderr": stderr,
        "success": success,
    }


def retry_failed_deployment(
    deployment: Deployment,
    *,
    step_names: list[str],
    queue_retry: Callable[[int], Any] | None = None,
    trigger_source: str = "autofix-retry-deploy",
) -> dict[str, Any]:
    history = _autofix_history(deployment)
    retry_count = _count_retry_actions(history)
    if retry_count >= AUTOFIX_MAX_AUTO_RETRY_DEPLOYS:
        return {
            "execution_result": "retry_limit_reached",
            "stdout": "Automatischer Redeploy wurde nicht erneut gestartet (Retry-Limit erreicht).",
            "stderr": "",
            "success": False,
        }

    new_deployment = Deployment(
        project_id=deployment.project_id,
        server_id=deployment.server_id,
        status="pending",
        mode=deployment.mode,
        trigger_source=trigger_source,
        commit_sha=deployment.commit_sha,
    )
    db.session.add(new_deployment)
    db.session.flush()

    for index, name in enumerate(step_names):
        db.session.add(
            DeploymentStep(
                deployment_id=new_deployment.id,
                name=name,
                status="pending",
                order_index=index,
            )
        )

    db.session.commit()

    queue_error = ""
    if queue_retry is not None:
        try:
            queue_retry(new_deployment.id)
        except Exception as exc:
            queue_error = str(exc)
            new_deployment.status = "failed"
            new_deployment.error_message = f"Auto-Fix Retry Queue Error: {exc}"
            db.session.commit()

    if queue_error:
        return {
            "execution_result": "retry_deploy_queue_failed",
            "stdout": f"Redeploy #{new_deployment.id} erstellt, Queue fehlgeschlagen.",
            "stderr": queue_error,
            "success": False,
            "new_deployment_id": new_deployment.id,
        }

    return {
        "execution_result": "retry_deploy_started",
        "stdout": f"Redeploy #{new_deployment.id} wurde automatisch angestossen.",
        "stderr": "",
        "success": True,
        "new_deployment_id": new_deployment.id,
    }


def _append_autofix_log(
    deployment: Deployment,
    *,
    action_name: str,
    trigger_reason: str,
    result: dict[str, Any],
    decision: dict[str, Any],
    increment_attempt: bool = True,
) -> dict[str, Any]:
    now = _utcnow()
    log_entry = {
        "action_name": action_name,
        "triggered_at": now.isoformat(),
        "trigger_reason": trigger_reason,
        "execution_result": result.get("execution_result"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "success": bool(result.get("success", False)),
        "detected_error_type": decision.get("detected_error_type"),
        "recommended_fix_action": decision.get("recommended_fix_action"),
        "confidence": decision.get("confidence"),
        "safe_to_execute_automatically": decision.get("safe_to_execute_automatically"),
    }

    history = _autofix_history(deployment)
    history.append(log_entry)

    deployment.autofix_history_json = history
    if increment_attempt:
        deployment.autofix_attempt_count = int(deployment.autofix_attempt_count or 0) + 1
    deployment.last_autofix_action = action_name
    deployment.last_autofix_at = now
    if log_entry["success"]:
        deployment.autofix_status = "succeeded"
    elif increment_attempt:
        deployment.autofix_status = "failed"
    else:
        deployment.autofix_status = "skipped"
    db.session.commit()
    return log_entry


def execute_autofix(
    deployment: Deployment,
    *,
    decision: dict[str, Any],
    project_slug: str,
    target_host: str | None,
    executor: DeploymentExecutor | None = None,
    auto_trigger: bool = True,
    step_names: list[str] | None = None,
    queue_retry: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    action_name = decision.get("recommended_fix_action")
    if not action_name:
        result = {
            "execution_result": "no_action",
            "stdout": "Kein sicherer Auto-Fix fuer diesen Fehler verfuegbar.",
            "stderr": "",
            "success": False,
        }
        return _append_autofix_log(
            deployment,
            action_name="none",
            trigger_reason=str(decision.get("trigger_reason") or "no_action"),
            result=result,
            decision=decision,
            increment_attempt=False,
        )

    if auto_trigger and not bool(decision.get("safe_to_execute_automatically", False)):
        result = {
            "execution_result": "skipped_unsafe_for_auto",
            "stdout": "Aktion wurde nur vorgeschlagen und nicht automatisch ausgefuehrt.",
            "stderr": "",
            "success": False,
        }
        return _append_autofix_log(
            deployment,
            action_name=action_name,
            trigger_reason=str(decision.get("trigger_reason") or "unsafe_auto_skip"),
            result=result,
            decision=decision,
            increment_attempt=False,
        )

    if int(deployment.autofix_attempt_count or 0) >= AUTOFIX_MAX_ATTEMPTS_PER_DEPLOYMENT:
        result = {
            "execution_result": "autofix_policy_limit_reached",
            "stdout": "Auto-Fix-Limit pro Deployment erreicht.",
            "stderr": "",
            "success": False,
        }
        return _append_autofix_log(
            deployment,
            action_name=action_name,
            trigger_reason="policy_limit_reached",
            result=result,
            decision=decision,
            increment_attempt=False,
        )

    runner = executor or DeploymentExecutor()

    if action_name == "restart_container":
        if not target_host:
            result = {
                "execution_result": "missing_target_host",
                "stdout": "",
                "stderr": "Kein Zielserver fuer restart_container vorhanden.",
                "success": False,
            }
        else:
            result = restart_active_container(host=target_host, project_slug=project_slug, executor=runner)
    elif action_name == "reload_nginx":
        if not target_host:
            result = {
                "execution_result": "missing_target_host",
                "stdout": "",
                "stderr": "Kein Zielserver fuer reload_nginx vorhanden.",
                "success": False,
            }
        else:
            result = reload_nginx_safely(host=target_host, executor=runner)
    elif action_name == "retry_deploy":
        result = retry_failed_deployment(
            deployment,
            step_names=step_names or [],
            queue_retry=queue_retry,
        )
    else:
        result = {
            "execution_result": "unknown_action",
            "stdout": "",
            "stderr": f"Unbekannte Auto-Fix-Aktion: {action_name}",
            "success": False,
        }

    return _append_autofix_log(
        deployment,
        action_name=action_name,
        trigger_reason=str(decision.get("trigger_reason") or "manual"),
        result=result,
        decision=decision,
    )
