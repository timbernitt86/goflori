from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import Deployment, Project
from app.tasks.deployment import run_deployment_task

bp = Blueprint("deployments", __name__)


@bp.post("/projects/<int:project_id>/deployments")
def create_deployment(project_id: int):
    project = Project.query.get_or_404(project_id)
    data = request.get_json(silent=True) or {}

    deployment = Deployment(
        project_id=project.id,
        status="pending",
        mode=data.get("mode", "staging"),
        commit_sha=data.get("commit_sha"),
        trigger_source=data.get("trigger_source", "manual"),
    )
    db.session.add(deployment)
    db.session.commit()

    return jsonify(deployment.to_dict(include_steps=True)), 201


@bp.get("/deployments/<int:deployment_id>")
def get_deployment(deployment_id: int):
    deployment = Deployment.query.get_or_404(deployment_id)
    return jsonify(deployment.to_dict(include_steps=True))


@bp.post("/deployments/<int:deployment_id>/run")
def run_deployment(deployment_id: int):
    deployment = Deployment.query.get_or_404(deployment_id)
    async_result = run_deployment_task.delay(deployment.id)
    return jsonify(
        {
            "deployment": deployment.to_dict(include_steps=True),
            "task_id": async_result.id,
            "message": "Deployment queued",
        }
    )
