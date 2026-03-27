from app.services.repo_analyzer import RepoAnalyzer


def test_detects_python_from_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")

    result = RepoAnalyzer().analyze_path(str(tmp_path))

    assert result.framework == "flask"
    assert result.entrypoint == "gunicorn app:app"
    assert "requirements.txt" in (result.stack_files or [])


def test_detects_node_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")

    result = RepoAnalyzer().analyze_path(str(tmp_path))

    assert result.framework == "node"
    assert result.entrypoint == "npm start"
    assert "package.json" in (result.stack_files or [])


def test_detects_laravel_from_artisan(tmp_path):
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n", encoding="utf-8")

    result = RepoAnalyzer().analyze_path(str(tmp_path))

    assert result.framework == "laravel"
    assert result.entrypoint == "php-fpm"
    assert "artisan" in (result.stack_files or [])


def test_detects_docker_only_repo(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    result = RepoAnalyzer().analyze_path(str(tmp_path))

    assert result.framework == "docker"
    assert "Dockerfile" in (result.stack_files or [])
