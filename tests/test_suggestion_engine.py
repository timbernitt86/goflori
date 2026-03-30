from __future__ import annotations

from types import SimpleNamespace

from app.services.suggestions import (
    generate_deployment_suggestions,
    generate_project_suggestions,
    translate_technical_issue_to_flori_message,
)


def test_translate_technical_issue_to_flori_message_missing_env():
    msg = translate_technical_issue_to_flori_message("missing_env", context={"env_name": "DATABASE_URL"})
    assert "DATABASE_URL" in msg["message"]


def test_generate_deployment_suggestions_detected_port_and_db_unreachable():
    analyze_step = SimpleNamespace(
        id=1,
        order_index=1,
        name="analyze_repository",
        status="success",
        stdout="",
        stderr="",
        json_details={"port": 3000, "detected_stack": "nodejs"},
    )
    failed_step = SimpleNamespace(
        id=2,
        order_index=2,
        name="healthcheck",
        status="failed",
        stdout="",
        stderr="psycopg2 connection refused",
        json_details={"error_details": {"message": "db connection refused"}},
    )
    deployment = SimpleNamespace(
        id=8,
        steps=[analyze_step, failed_step],
        error_message="database timeout while connecting",
        output="",
        error_analysis_json={"error_type": "db_connection", "confidence": 0.82},
    )

    suggestions = generate_deployment_suggestions(deployment, runtime_state={"current_runtime_status": "failed"})

    types = [item["suggestion_type"] for item in suggestions]
    assert "db_unreachable" in types
    assert "detected_port" in types


def test_generate_deployment_suggestions_missing_env_from_logs():
    failed_step = SimpleNamespace(
        id=2,
        order_index=2,
        name="start_containers",
        status="failed",
        stdout="",
        stderr="KeyError: 'DATABASE_URL'",
        json_details={},
    )
    deployment = SimpleNamespace(
        id=9,
        steps=[failed_step],
        error_message="DATABASE_URL not set",
        output="",
        error_analysis_json={"error_type": "missing_env", "confidence": 0.75},
    )

    suggestions = generate_deployment_suggestions(deployment)

    assert suggestions
    assert suggestions[0]["suggestion_type"] == "missing_env"
    assert "DATABASE_URL" in suggestions[0]["message"]


def test_generate_project_suggestions_not_spammy():
    deployment = SimpleNamespace(
        id=1,
        created_at=2,
        steps=[],
        error_message="DATABASE_URL not set",
        output="",
        error_analysis_json={"error_type": "missing_env", "confidence": 0.8},
    )
    project = SimpleNamespace(
        framework="flask",
        repository=SimpleNamespace(repo_url="https://github.com/example/app"),
        deployments=[deployment],
        environment_variables=[],
    )

    suggestions = generate_project_suggestions(project, runtime_state={"current_runtime_status": "failed"})

    missing_env_messages = [item["message"] for item in suggestions if item["suggestion_type"] == "missing_env"]
    assert len(set(missing_env_messages)) == len(missing_env_messages)


def test_generate_deployment_suggestions_dns_mismatch_from_check_step():
    check_dns_step = SimpleNamespace(
        id=3,
        order_index=3,
        name="check_dns",
        status="failed",
        stdout="resolved_ip=203.0.113.10\nexpected_ip=198.51.100.7\nmatches=False",
        stderr="DNS-Pruefung fehlgeschlagen: Domain zeigt nicht auf den Zielserver.",
        json_details={
            "matches": False,
            "resolved_ip": "203.0.113.10",
            "expected_ip": "198.51.100.7",
        },
    )
    deployment = SimpleNamespace(
        id=11,
        steps=[check_dns_step],
        error_message="DNS-Pruefung fehlgeschlagen: Domain zeigt nicht auf den Zielserver.",
        output="",
        error_analysis_json={"error_type": "unknown_error", "confidence": 0.4},
    )

    suggestions = generate_deployment_suggestions(deployment)

    assert suggestions
    assert suggestions[0]["suggestion_type"] == "dns_mismatch"
    assert "zeigt noch nicht auf den Zielserver" in suggestions[0]["message"]
    assert "203.0.113.10" in suggestions[0]["message"]
