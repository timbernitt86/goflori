from __future__ import annotations

from datetime import datetime, timezone

from app.extensions import db
from app.models import Project, ProjectHealthCheck


def test_project_detail_handles_naive_healthcheck_timestamp(app, client):
    email = "tz@example.com"
    password = "very-secure-test-password"

    register_response = client.post(
        "/auth/register",
        data={
            "company_name": "TZ Company",
            "name": "TZ User",
            "email": email,
            "password": password,
            "password_confirm": password,
        },
        follow_redirects=True,
    )
    assert register_response.status_code == 200

    create_response = client.post(
        "/dashboard/projects",
        data={
            "create_action": "create",
            "name": "Timezone Test",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200

    with app.app_context():
        project = Project.query.filter_by(name="Timezone Test").first()
        assert project is not None
        naive_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # Intentionally naive datetime to reproduce historical sqlite/runtime mismatch.
        db.session.add(
            ProjectHealthCheck(
                project_id=project.id,
                checked_at=naive_checked_at,
                target_url="http://127.0.0.1",
                success=True,
                status_code=200,
                response_time_ms=12,
                error_message=None,
            )
        )
        db.session.commit()

        project_id = project.id

    response = client.get(f"/dashboard/projects/{project_id}")
    assert response.status_code == 200
    assert "Timezone Test" in response.get_data(as_text=True)
