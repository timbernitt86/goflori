from __future__ import annotations

from types import SimpleNamespace

from app.services.error_analysis import (
    analyze_deployment_errors,
    build_error_summary,
    classify_log_patterns,
)


def test_classify_log_patterns_missing_env():
    logs = "KeyError: DATABASE_URL not set - environment variable not set"
    result = classify_log_patterns(logs)

    assert result
    assert result[0]["error_type"] == "missing_env"
    assert result[0]["confidence"] >= 0.55


def test_classify_log_patterns_port_conflict():
    logs = "Error response from daemon: driver failed programming external connectivity: bind failed: address already in use"
    result = classify_log_patterns(logs)

    assert result
    assert result[0]["error_type"] == "port_conflict"


def test_analyze_deployment_errors_prefers_failed_step_and_secondary_errors():
    ok_step = SimpleNamespace(
        id=1,
        order_index=1,
        name="prepare_host",
        status="success",
        stdout="apt-get update ok",
        stderr="",
        output="",
        error_message="",
        json_details={"events": [{"message": "Step erfolgreich", "source": "step_runner"}]},
    )
    failed_step = SimpleNamespace(
        id=2,
        order_index=2,
        name="start_containers",
        status="failed",
        stdout="docker build failed to solve",
        stderr="address already in use",
        output="",
        error_message="",
        json_details={
            "events": [{"message": "Step fehlgeschlagen", "source": "step_runner"}],
            "error_details": {"message": "bind failed"},
        },
    )
    deployment = SimpleNamespace(
        id=123,
        error_message="Deployment failed",
        output="",
        steps=[ok_step, failed_step],
    )

    analysis = analyze_deployment_errors(deployment)

    assert analysis["error_type"] in {"build_fail", "port_conflict"}
    assert analysis["affected_step"] == "start_containers"
    assert isinstance(analysis.get("secondary_errors", []), list)


def test_build_error_summary_unknown_fallback():
    summary = build_error_summary(primary_error=None, secondary_errors=None, affected_step="healthcheck")

    assert summary["error_type"] == "unknown_error"
    assert summary["affected_step"] == "healthcheck"
