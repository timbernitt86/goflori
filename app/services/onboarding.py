import re

from app.models import Project, ProviderSetting, Repository

DEFAULT_SERVER_TYPE = "cx22"
DEFAULT_LOCATION = "nbg1"
DEFAULT_IMAGE = "ubuntu-24.04"


class OnboardingValidationError(ValueError):
    pass


def _normalize_repository_url(value: str) -> str:
    repo = (value or "").strip()
    if not repo:
        raise OnboardingValidationError("Bitte gib einen Repository-Link an.")
    if " " in repo:
        raise OnboardingValidationError("Repository-Link ist ungueltig.")

    if repo.startswith("github.com/") or repo.startswith("gitlab.com/") or repo.startswith("bitbucket.org/"):
        repo = f"https://{repo}"

    allowed_prefixes = ("https://", "http://", "git@")
    if not repo.startswith(allowed_prefixes):
        raise OnboardingValidationError("Repository-Link ist ungueltig.")

    if repo.startswith("http://"):
        repo = "https://" + repo[len("http://") :]

    if repo.startswith("https://") and repo.count("/") < 4:
        raise OnboardingValidationError("Repository-Link ist unvollstaendig.")

    return repo


def _normalize_domain(value: str | None) -> str | None:
    domain = (value or "").strip().lower()
    if not domain:
        return None
    if " " in domain or "/" in domain or ":" in domain:
        raise OnboardingValidationError("Domain ist ungueltig.")

    if not re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", domain):
        raise OnboardingValidationError("Domain ist ungueltig.")

    return domain


def _infer_repository_provider(repo_url: str) -> str | None:
    value = (repo_url or "").lower()
    if "github.com" in value:
        return "github"
    if "gitlab.com" in value:
        return "gitlab"
    if "bitbucket.org" in value:
        return "bitbucket"
    return None


def _derive_project_name(repo_url: str) -> str:
    trimmed = repo_url.rstrip("/")
    if ":" in trimmed and trimmed.startswith("git@"):
        candidate = trimmed.split(":", 1)[1]
    else:
        candidate = trimmed.rsplit("/", 1)[-1]

    if candidate.endswith(".git"):
        candidate = candidate[:-4]

    candidate = re.sub(r"[^a-zA-Z0-9_-]+", "-", candidate).strip("-")
    if not candidate:
        return "Neues Projekt"

    words = [part for part in re.split(r"[-_]+", candidate) if part]
    if not words:
        return "Neues Projekt"

    return " ".join(word.capitalize() for word in words)


def _generate_unique_project_slug(name: str, requested_slug: str | None = None) -> str:
    base_slug = (requested_slug or "").strip() or Project.slugify(name)
    if not base_slug:
        base_slug = "projekt"

    candidate = base_slug
    suffix = 2
    while Project.query.filter_by(slug=candidate).first() is not None:
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _resolve_provider_defaults() -> tuple[str, str, str]:
    setting = ProviderSetting.query.filter_by(provider_name="hetzner").first()
    server_type = (setting.default_server_type if setting else None) or DEFAULT_SERVER_TYPE
    location = (setting.default_location if setting else None) or DEFAULT_LOCATION
    image = (setting.default_image if setting else None) or DEFAULT_IMAGE
    return server_type, location, image


def create_project_with_defaults(
    *,
    company_id: int,
    repository_url: str,
    domain: str | None,
    requested_name: str | None = None,
    requested_slug: str | None = None,
    repository_branch: str | None = None,
    repository_access_token: str | None = None,
    repository_is_private: bool = False,
) -> Project:
    normalized_repo = _normalize_repository_url(repository_url)
    normalized_domain = _normalize_domain(domain)
    project_name = (requested_name or "").strip() or _derive_project_name(normalized_repo)
    project_slug = _generate_unique_project_slug(project_name, requested_slug=requested_slug)
    branch = (repository_branch or "").strip() or "main"

    server_type, location, image = _resolve_provider_defaults()

    project = Project(
        company_id=company_id,
        name=project_name,
        slug=project_slug,
        framework=None,
        environment="production",
        domain=normalized_domain,
        desired_server_type=server_type,
        desired_location=location,
        desired_image=image,
        branch=branch,
    )

    project.repository = Repository(
        provider=_infer_repository_provider(normalized_repo),
        repo_url=normalized_repo,
        branch=branch,
        access_token=(repository_access_token or "").strip() or None,
        is_private=bool(repository_is_private),
    )

    return project


def create_and_start_deployment(project: Project, start_deployment):
    return start_deployment(project)
