from celery import shared_task

from app.extensions import db
from app.models import ActivityLog, Deployment, DeploymentStep, Project, Server
from app.services.execution import DeploymentExecutor, PipelineContext
from app.services.ssh import SSHWaitTimeoutError


STEP_NAMES = [
    "provision_server",
    "wait_for_ssh",
    "prepare_host",
    "render_files",
    "upload_and_deploy",
    "configure_reverse_proxy",
    "check_dns",
    "run_certbot",
    "verify_https",
    "healthcheck",
]


def _pick_reusable_server(project: Project) -> Server | None:
    # Prefer explicitly assigned active server when it is usable.
    if project.active_server and project.active_server.ipv4 and project.active_server.status in {
        "running",
        "starting",
        "initializing",
    }:
        return project.active_server

    candidates = sorted(project.servers, key=lambda s: s.updated_at, reverse=True)
    for server in candidates:
        if server.ipv4 and server.status in {"running", "starting", "initializing"}:
            return server
    return None


def _pick_server_for_deployment(project: Project, deployment: Deployment) -> Server | None:
    if deployment.server and deployment.server.ipv4 and deployment.server.status in {"running", "starting", "initializing"}:
        return deployment.server
    return _pick_reusable_server(project)


def _ensure_steps(deployment: Deployment):
    if deployment.steps:
        return
    for index, name in enumerate(STEP_NAMES):
        deployment.steps.append(DeploymentStep(name=name, status="pending", order_index=index))
    db.session.commit()


def _set_step(deployment: Deployment, name: str, *, status: str, output: str | None = None, error: str | None = None):
    step = next((item for item in deployment.steps if item.name == name), None)
    if not step:
        step = DeploymentStep(deployment_id=deployment.id, name=name, order_index=len(deployment.steps))
        db.session.add(step)
    step.status = status
    if output:
        step.output = output
    if error:
        step.error_message = error
    db.session.commit()


def _assert_command_results_ok(step_name: str, results) -> None:
    failed = [item for item in results if getattr(item, "return_code", 0) != 0]
    if not failed:
        return

    lines = [f"{step_name}: {len(failed)} command(s) failed"]
    for item in failed:
        lines.append(f"cmd={item.command}")
        lines.append(f"rc={item.return_code}")
        if item.stderr:
            lines.append(f"stderr={item.stderr.strip()}")
    raise RuntimeError("\n".join(lines))


def _format_command_results(results) -> str:
    lines: list[str] = []
    for item in results:
        lines.append(f"cmd={item.command}")
        lines.append(f"exit_code={item.return_code}")
        lines.append("stdout:")
        lines.append(item.stdout.rstrip() if item.stdout else "")
        lines.append("stderr:")
        lines.append(item.stderr.rstrip() if item.stderr else "")
        lines.append("---")
    return "\n".join(lines).rstrip("-\n")


@shared_task(ignore_result=False)
def run_deployment_task(deployment_id: int):
    deployment = Deployment.query.get_or_404(deployment_id)
    project = Project.query.get_or_404(deployment.project_id)
    executor = DeploymentExecutor()

    _ensure_steps(deployment)
    deployment.status = "running"
    db.session.commit()

    ctx = PipelineContext(
        project_name=project.name,
        slug=project.slug,
        framework=project.framework or "flask",
        domain=project.domain,
        repository_url=project.repository.url if project.repository else None,
        repository_branch=project.repository.branch if project.repository and project.repository.branch else project.branch,
    )

    try:
        _set_step(deployment, "provision_server", status="running")
        existing_server = _pick_server_for_deployment(project, deployment)
        if existing_server:
            server = existing_server
            deployment.server_id = server.id
            project.active_server_id = server.id
            db.session.commit()
            _set_step(
                deployment,
                "provision_server",
                status="success",
                output=(
                    "action=reuse_existing_server\n"
                    f"server_id={server.id}\n"
                    f"provider_server_id={server.provider_server_id or '-'}\n"
                    f"name={server.name}\n"
                    f"status={server.status}\n"
                    f"ipv4={server.ipv4 or '-'}\n"
                    f"server_type={server.server_type}\n"
                    f"location={server.region}"
                ),
            )
        else:
            server, provisioned = executor.hetzner.create_server_for_project(
                project=project,
                deployment=deployment,
                name=f"orbital-{ctx.slug}",
                server_type=project.desired_server_type,
                location=project.desired_location,
                image=project.desired_image,
            )
            deployment.server_id = server.id
            project.active_server_id = server.id
            db.session.commit()
            _set_step(
                deployment,
                "provision_server",
                status="success",
                output=(
                    "action=provision_new_server\n"
                    f"server_id={server.id}\n"
                    f"provider_server_id={provisioned.provider_server_id}\n"
                    f"name={provisioned.name}\n"
                    f"status={provisioned.status}\n"
                    f"ipv4={provisioned.ipv4 or '-'}\n"
                    f"server_type={provisioned.server_type}\n"
                    f"location={provisioned.location}"
                ),
            )

        _set_step(deployment, "wait_for_ssh", status="running")
        try:
            wait_logs = executor.wait_for_ssh(server.ipv4 or "", max_attempts=20, delay_seconds=10)
        except SSHWaitTimeoutError as exc:
            _set_step(
                deployment,
                "wait_for_ssh",
                status="failed",
                output="\n".join(exc.attempts_log),
                error=str(exc),
            )
            raise
        except Exception as exc:
            _set_step(
                deployment,
                "wait_for_ssh",
                status="failed",
                error=str(exc),
            )
            raise
        else:
            _set_step(deployment, "wait_for_ssh", status="success", output="\n".join(wait_logs))

        _set_step(deployment, "prepare_host", status="running")
        result = executor.prepare_host(server.ipv4 or "127.0.0.1")
        _assert_command_results_ok("prepare_host", result)
        _set_step(deployment, "prepare_host", status="success", output=_format_command_results(result))

        _set_step(deployment, "render_files", status="running")
        rendered = executor.render_files(ctx)
        _set_step(
            deployment,
            "render_files",
            status="success",
            output=f"Dockerfile, compose and nginx config rendered for {ctx.framework}",
        )

        _set_step(deployment, "upload_and_deploy", status="running")
        result = executor.upload_and_deploy(server.ipv4 or "127.0.0.1", ctx, rendered)
        _assert_command_results_ok("upload_and_deploy", result)
        _set_step(deployment, "upload_and_deploy", status="success", output=_format_command_results(result))

        _set_step(deployment, "configure_reverse_proxy", status="running")
        result = executor.configure_reverse_proxy(server.ipv4 or "127.0.0.1", ctx)
        _assert_command_results_ok("configure_reverse_proxy", result)
        _set_step(
            deployment,
            "configure_reverse_proxy",
            status="success",
            output=_format_command_results(result),
        )

        _set_step(deployment, "check_dns", status="running")
        dns_result = executor.check_dns(ctx.domain, server.ipv4)
        dns_output = (
            f"resolved_ip={dns_result.resolved_ip}\n"
            f"expected_ip={dns_result.expected_ip}\n"
            f"matches={dns_result.matches}"
        )
        if not ctx.domain:
            _set_step(deployment, "check_dns", status="success", output="resolved_ip=-\nexpected_ip=-\nmatches=False\ninfo=no_domain_configured")
            _set_step(
                deployment,
                "run_certbot",
                status="success",
                output="SSL skipped: no domain configured.",
            )
            _set_step(
                deployment,
                "verify_https",
                status="success",
                output="HTTPS verification skipped: no domain configured.",
            )
        elif not dns_result.matches:
            mismatch_message = (
                "DNS-Pruefung fehlgeschlagen: Domain zeigt nicht auf den Zielserver.\n"
                f"resolved_ip={dns_result.resolved_ip}\n"
                f"expected_ip={dns_result.expected_ip}\n"
                "Bitte A-Record korrigieren und Deployment erneut starten."
            )
            _set_step(
                deployment,
                "check_dns",
                status="failed",
                output=dns_output,
                error=mismatch_message,
            )
            _set_step(
                deployment,
                "run_certbot",
                status="failed",
                output=(
                    "certbot nicht gestartet, weil DNS-Pruefung fehlgeschlagen ist.\n"
                    f"resolved_ip={dns_result.resolved_ip}\n"
                    f"expected_ip={dns_result.expected_ip}\n"
                    "matches=False"
                ),
                error="certbot skipped due to DNS mismatch",
            )
            _set_step(
                deployment,
                "verify_https",
                status="failed",
                output="HTTPS verification skipped, because certbot was not executed.",
                error="verify_https skipped due to DNS mismatch",
            )
            raise RuntimeError(mismatch_message)
        else:
            _set_step(deployment, "check_dns", status="success", output=dns_output)
            _set_step(deployment, "run_certbot", status="running")
            result = executor.run_certbot(server.ipv4 or "127.0.0.1", ctx.domain)
            _assert_command_results_ok("run_certbot", result)
            _set_step(deployment, "run_certbot", status="success", output=_format_command_results(result))

            _set_step(deployment, "verify_https", status="running")
            result = executor.verify_https(server.ipv4 or "127.0.0.1", ctx.domain)
            _assert_command_results_ok("verify_https", result)
            _set_step(deployment, "verify_https", status="success", output=_format_command_results(result))

        _set_step(deployment, "healthcheck", status="running")
        result = executor.healthcheck(server.ipv4 or "127.0.0.1", ctx)
        _assert_command_results_ok("healthcheck", result)
        _set_step(deployment, "healthcheck", status="success", output=_format_command_results(result))

        deployment.status = "success"
        project.status = "live"
        deployment.output = rendered.compose
        db.session.add(
            ActivityLog(project_id=project.id, action="deployment.completed", actor="system", message="Deployment finished")
        )
        db.session.commit()

        return deployment.to_dict(include_steps=True)
    except Exception as exc:
        provision_step = next((item for item in deployment.steps if item.name == "provision_server"), None)
        if provision_step is None or provision_step.status in {"pending", "running"}:
            _set_step(deployment, "provision_server", status="failed", error=str(exc))
        deployment.status = "failed"
        deployment.error_message = str(exc)
        db.session.add(
            ActivityLog(project_id=project.id, action="deployment.failed", actor="system", message=str(exc))
        )
        db.session.commit()
        raise
