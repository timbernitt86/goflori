from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class JourneyContext:
    client: object
    state: dict[str, object]


@dataclass(frozen=True)
class JourneyStep:
    name: str
    run: Callable[[JourneyContext], None]


class ApplicationJourney:
    """Reusable end-to-end smoke journey for the whole app.

    Extend by appending steps in `default_steps()` or defining a new scenario.
    Each step should focus on one business capability and store shared IDs in
    `ctx.state` for following steps.
    """

    def default_steps(self) -> list[JourneyStep]:
        return [
            JourneyStep("health endpoint", self.step_health_endpoint),
            JourneyStep("landing page", self.step_landing_page),
            JourneyStep("register", self.step_register_user),
            JourneyStep("projects dashboard", self.step_projects_dashboard),
            JourneyStep("create project", self.step_create_project),
            JourneyStep("project detail", self.step_project_detail),
            JourneyStep("logout", self.step_logout),
            JourneyStep("login", self.step_login),
        ]

    def run(self, ctx: JourneyContext, *, steps: list[JourneyStep] | None = None) -> None:
        for step in steps or self.default_steps():
            step.run(ctx)

    def step_health_endpoint(self, ctx: JourneyContext) -> None:
        response = ctx.client.get("/health")
        assert response.status_code == 200
        payload = response.get_json() or {}
        assert payload.get("status") == "ok"

    def step_landing_page(self, ctx: JourneyContext) -> None:
        response = ctx.client.get("/")
        assert response.status_code in {301, 302}
        assert "/auth/" in (response.headers.get("Location") or "")

    def step_register_user(self, ctx: JourneyContext) -> None:
        email = "journey@example.com"
        password = "very-secure-test-password"
        response = ctx.client.post(
            "/auth/register",
            data={
                "company_name": "Journey Company",
                "name": "Journey Admin",
                "email": email,
                "password": password,
                "password_confirm": password,
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Projekte" in body
        ctx.state["email"] = email
        ctx.state["password"] = password

    def step_projects_dashboard(self, ctx: JourneyContext) -> None:
        response = ctx.client.get("/dashboard/projects")
        assert response.status_code == 200
        assert "Schnellstart" in response.get_data(as_text=True)

    def step_create_project(self, ctx: JourneyContext) -> None:
        response = ctx.client.post(
            "/dashboard/projects",
            data={
                "create_action": "create",
                "name": "Journey Project",
                "repository_url": "https://github.com/example/repo.git",
                "repository_branch": "main",
                "domain": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Journey Project" in body

    def step_project_detail(self, ctx: JourneyContext) -> None:
        response = ctx.client.get("/dashboard/projects")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Journey Project" in body

    def step_logout(self, ctx: JourneyContext) -> None:
        response = ctx.client.post("/auth/logout", follow_redirects=True)
        assert response.status_code == 200
        assert "Einloggen" in response.get_data(as_text=True) or "Login" in response.get_data(as_text=True)

    def step_login(self, ctx: JourneyContext) -> None:
        response = ctx.client.post(
            "/auth/login",
            data={
                "email": ctx.state["email"],
                "password": ctx.state["password"],
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Projekte" in response.get_data(as_text=True)


def test_application_default_journey(journey_context: JourneyContext) -> None:
    ApplicationJourney().run(journey_context)
