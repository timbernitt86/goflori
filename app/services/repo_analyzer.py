from dataclasses import dataclass
from pathlib import Path

from app.services.repo_clone import LocalRepoCloneService


@dataclass
class RepoAnalysisResult:
    detected_stack: str
    confidence: float
    relevant_files: list[str]
    framework: str
    entrypoint: str | None = None
    port: int = 8000
    uses_postgres: bool = False
    uses_redis: bool = False
    stack_files: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "detected_stack": self.detected_stack,
            "confidence": self.confidence,
            "relevant_files": self.relevant_files,
            "framework": self.framework,
            "entrypoint": self.entrypoint,
            "port": self.port,
            "uses_postgres": self.uses_postgres,
            "uses_redis": self.uses_redis,
            "stack_files": self.stack_files,
        }


class RepoAnalyzer:
    """
    Deterministic analysis first. Replace or augment later with an AI call.
    """

    def __init__(self, cloner: LocalRepoCloneService | None = None):
        self.cloner = cloner or LocalRepoCloneService()

    def _detect_from_files(self, path: Path) -> RepoAnalysisResult:
        def exists(name: str) -> bool:
            return (path / name).exists()

        has_package_json = exists("package.json")
        has_requirements = exists("requirements.txt")
        has_pyproject = exists("pyproject.toml")
        has_app_py = exists("app.py")
        has_wsgi_py = exists("wsgi.py")
        has_artisan = exists("artisan")
        has_composer_json = exists("composer.json")
        has_dockerfile = exists("Dockerfile")
        has_docker_compose = exists("docker-compose.yml") or exists("compose.yaml") or exists("compose.yml")

        python_files = [
            item
            for item, present in [
                ("requirements.txt", has_requirements),
                ("pyproject.toml", has_pyproject),
                ("app.py", has_app_py),
                ("wsgi.py", has_wsgi_py),
            ]
            if present
        ]
        node_files = [item for item, present in [("package.json", has_package_json)] if present]
        laravel_files = [
            item
            for item, present in [("artisan", has_artisan), ("composer.json", has_composer_json)]
            if present
        ]
        docker_files = [
            item
            for item, present in [
                ("Dockerfile", has_dockerfile),
                ("docker-compose.yml", exists("docker-compose.yml")),
                ("compose.yaml", exists("compose.yaml")),
                ("compose.yml", exists("compose.yml")),
            ]
            if present
        ]

        if has_artisan and has_composer_json:
            return RepoAnalysisResult(
                detected_stack="laravel",
                confidence=0.98,
                relevant_files=laravel_files,
                framework="laravel",
                entrypoint="php-fpm",
                port=8000,
                uses_postgres=exists("docker-compose.yml"),
                stack_files=laravel_files,
            )

        if has_artisan or has_composer_json:
            return RepoAnalysisResult(
                detected_stack="laravel",
                confidence=0.85,
                relevant_files=laravel_files,
                framework="laravel",
                entrypoint="php-fpm",
                port=8000,
                stack_files=laravel_files,
            )

        if has_package_json:
            return RepoAnalysisResult(
                detected_stack="nodejs",
                confidence=0.92,
                relevant_files=node_files,
                framework="node",
                entrypoint="npm start",
                port=3000,
                stack_files=node_files,
            )

        if python_files:
            confidence = min(0.6 + (0.1 * len(python_files)), 0.95)
            return RepoAnalysisResult(
                detected_stack="flask",
                confidence=confidence,
                relevant_files=python_files,
                framework="flask",
                entrypoint="gunicorn app:app",
                port=8000,
                stack_files=python_files,
            )

        if has_dockerfile or has_docker_compose:
            return RepoAnalysisResult(
                detected_stack="docker",
                confidence=0.8,
                relevant_files=docker_files,
                framework="docker",
                entrypoint="docker compose up -d",
                port=8000,
                stack_files=docker_files,
            )

        return RepoAnalysisResult(
            detected_stack="unknown",
            confidence=0.2,
            relevant_files=[],
            framework="flask",
            entrypoint=None,
            port=8000,
            stack_files=[],
        )

    def analyze_path(self, repo_path: str) -> RepoAnalysisResult:
        path = Path(repo_path)
        return self._detect_from_files(path)

    def analyze_repository_url(self, repo_url: str, branch: str = "main") -> RepoAnalysisResult:
        clone = self.cloner.clone(repo_url=repo_url, branch=branch)
        return self._detect_from_files(Path(clone.local_path))
