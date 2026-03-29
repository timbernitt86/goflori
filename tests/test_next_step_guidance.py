from __future__ import annotations

from types import SimpleNamespace

from app.dashboard.routes import _build_next_step_guidance


def test_guidance_marks_live_when_only_dns_failed_and_active_version_exists():
    project = SimpleNamespace(status="live")
    runtime_state = {
        "current_runtime_status": "failed",
        "reason": "DNS-Pruefung fehlgeschlagen: Domain zeigt nicht auf den Zielserver.",
        "active_deployment_id": 65,
    }

    guidance = _build_next_step_guidance(project, runtime_state, [], {})

    assert guidance["is_live"] is True
    assert "domain" in guidance["headline"].lower()


def test_guidance_marks_not_live_when_failed_without_active_version():
    project = SimpleNamespace(status="failed")
    runtime_state = {
        "current_runtime_status": "failed",
        "reason": "Container nicht erreichbar",
        "active_deployment_id": None,
    }

    guidance = _build_next_step_guidance(project, runtime_state, [], {})

    assert guidance["is_live"] is False
    assert "nicht live" in guidance["headline"].lower()
