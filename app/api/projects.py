from flask import Blueprint, jsonify, request, abort

from app.extensions import db
from app.models import Project, Repository, EnvironmentVariable

bp = Blueprint("projects", __name__)


@bp.get("/projects")
def list_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify([project.to_dict(include_children=True) for project in projects])


@bp.post("/projects")
def create_project():
    data = request.get_json(force=True)

    if not data.get("name"):
        abort(400, description="'name' is required")

    project = Project(
        name=data["name"],
        slug=data.get("slug") or Project.slugify(data["name"]),
        framework=data.get("framework"),
        environment=data.get("environment", "production"),
        domain=data.get("domain"),
        branch=data.get("branch", "main"),
    )

    repo_url = data.get("repository_url")
    if repo_url:
        project.repository = Repository(
            provider=data.get("repository_provider", "github"),
            url=repo_url,
            branch=project.branch,
        )

    for item in data.get("env", []):
        if not item.get("key"):
            continue
        project.environment_variables.append(
            EnvironmentVariable(
                key=item["key"],
                value=item.get("value", ""),
                is_secret=bool(item.get("is_secret", False)),
            )
        )

    db.session.add(project)
    db.session.commit()
    return jsonify(project.to_dict(include_children=True)), 201


@bp.get("/projects/<int:project_id>")
def get_project(project_id: int):
    project = Project.query.get_or_404(project_id)
    return jsonify(project.to_dict(include_children=True))
