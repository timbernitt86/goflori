from dataclasses import dataclass
from pathlib import Path

from app.services.repo_clone import LocalRepoCloneService


@dataclass
class RepoAnalysisResult:
    framework: str
    entrypoint: str | None = None
    port: int = 8000
    uses_postgres: bool = False
    uses_redis: bool = False
    stack_files: list[str] | None = None


class RepoAnalyzer:
    """
    Deterministic analysis first. Replace or augment later with an AI call.
    """

    def __init__(self, cloner: LocalRepoCloneService | None = None):
        self.cloner = cloner or LocalRepoCloneService()

    def _detect_from_files(self, path: Path) -> RepoAnalysisResult:
        files: list[str] = []

        has_package_json = (path / "package.json").exists()
        has_requirements = (path / "requirements.txt").exists()
        has_pyproject = (path / "pyproject.toml").exists()
        has_artisan = (path / "artisan").exists()
        has_dockerfile = (path / "Dockerfile").exists()
        has_manage = (path / "manage.py").exists()

        if has_package_json:
            files.append("package.json")
        if has_requirements:
            files.append("requirements.txt")
        if has_pyproject:
            files.append("pyproject.toml")
        if has_artisan:
            files.append("artisan")
        if has_dockerfile:
            files.append("Dockerfile")
        if has_manage:
            files.append("manage.py")

        if has_artisan:
            return RepoAnalysisResult(
                framework="laravel",
                entrypoint="php-fpm",
                port=9000,
                uses_postgres=(path / "docker-compose.yml").exists(),
                stack_files=files,
            )

        if has_manage:
            return RepoAnalysisResult(
                framework="django",
                entrypoint="gunicorn app.wsgi:application",
                port=8000,
                stack_files=files,
            )

        if has_package_json:
            return RepoAnalysisResult(
                framework="node",
                entrypoint="npm start",
                port=3000,
                stack_files=files,
            )

        if has_requirements or has_pyproject:
            return RepoAnalysisResult(
                framework="flask",
                entrypoint="gunicorn app:app",
                port=8000,
                stack_files=files,
            )

        if has_dockerfile:
            return RepoAnalysisResult(
                framework="docker",
                entrypoint="docker build/run",
                port=8000,
                stack_files=files,
            )

        return RepoAnalysisResult(framework="unknown", stack_files=files)

    def analyze_path(self, repo_path: str) -> RepoAnalysisResult:
        path = Path(repo_path)
        return self._detect_from_files(path)

    def analyze_repository_url(self, repo_url: str, branch: str = "main") -> RepoAnalysisResult:
        clone = self.cloner.clone(repo_url=repo_url, branch=branch)
        return self._detect_from_files(Path(clone.local_path))
