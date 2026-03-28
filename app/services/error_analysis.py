from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorAnalysis:
    error_type: str
    probable_cause: str
    suggested_fix: str

    def to_dict(self) -> dict[str, str]:
        return {
            "error_type": self.error_type,
            "probable_cause": self.probable_cause,
            "suggested_fix": self.suggested_fix,
        }


def _compose_haystack(*parts: str | None) -> str:
    chunks = [(part or "").strip().lower() for part in parts if part]
    return "\n".join(chunks)


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)


def analyze_deployment_failure(
    *,
    step_name: str,
    stdout: str | None,
    stderr: str | None,
    error_category: str | None = None,
    exception_type: str | None = None,
) -> dict[str, str]:
    haystack = _compose_haystack(step_name, error_category, exception_type, stdout, stderr)

    if _contains_any(
        haystack,
        [
            "environment variable",
            "is not set",
            "missing env",
            "keyerror",
            "undefined variable",
            "env_file",
            "missing required setting",
            "no such file or directory: '.env'",
        ],
    ):
        return ErrorAnalysis(
            error_type="env_missing",
            probable_cause="Eine benoetigte Environment-Variable fehlt oder ist leer.",
            suggested_fix="Ergaenze die fehlenden Variablen im Projekt unter App-Einstellungen und starte danach ein Redeploy.",
        ).to_dict()

    if _contains_any(
        haystack,
        [
            "database",
            "could not connect to server",
            "operationalerror",
            "connection to database",
            "psycopg2",
            "sqlalchemy.exc.operationalerror",
            "econnrefused",
            "db is unavailable",
            "no route to host",
            "timeout expired",
        ],
    ):
        return ErrorAnalysis(
            error_type="db_unreachable",
            probable_cause="Die Anwendung erreicht die Datenbank nicht (Host, Port, Credentials oder Netzwerk).",
            suggested_fix="Pruefe DB_HOST/DB_PORT/DB_USER/DB_PASSWORD, Netzwerkfreigaben und ob die Datenbankinstanz laeuft.",
        ).to_dict()

    if _contains_any(
        haystack,
        [
            "connection refused",
            "failed to connect",
            "timed out",
            "upstream timed out",
            "healthcheck",
            "curl: (7)",
            "127.0.0.1:",
            "port is not reachable",
            "cannot connect to",
            "connection reset by peer",
        ],
    ):
        return ErrorAnalysis(
            error_type="port_unreachable",
            probable_cause="Die Anwendung lauscht nicht auf dem erwarteten Port oder ist noch nicht gestartet.",
            suggested_fix="Pruefe APP/PORT-Einstellungen, Container-Logs und ob der Dienst auf 0.0.0.0:<PORT> gebunden ist.",
        ).to_dict()

    if _contains_any(
        haystack,
        [
            "container",
            "exited",
            "restart",
            "crashloop",
            "no such container",
            "pull access denied",
            "image not found",
            "oci runtime",
            "failed to create task",
            "is unhealthy",
            "start_containers",
            "docker-compose",
        ],
    ):
        return ErrorAnalysis(
            error_type="container_start_failure",
            probable_cause="Der Container konnte nicht korrekt gebaut oder gestartet werden.",
            suggested_fix="Pruefe Dockerfile/Compose, Image-Namen, Build-Fehler und den Startbefehl in den Container-Logs.",
        ).to_dict()

    if _contains_any(
        haystack,
        [
            "nginx",
            "host not found in upstream",
            "invalid number of arguments in",
            "emerg",
            "no live upstreams",
            "nginx -t",
            "configure_reverse_proxy",
            "certbot --nginx",
        ],
    ):
        return ErrorAnalysis(
            error_type="nginx_error",
            probable_cause="Die Nginx-Konfiguration ist ungueltig oder Upstream/Domain-Aufloesung ist fehlerhaft.",
            suggested_fix="Pruefe die generierte nginx.conf, DNS-Aufloesung und teste die Konfiguration mit nginx -t.",
        ).to_dict()

    return ErrorAnalysis(
        error_type="unknown_error",
        probable_cause="Die genaue Ursache konnte nicht automatisch klassifiziert werden.",
        suggested_fix="Oeffne die Step-Logs (stdout/stderr) und pruefe den ersten Fehlerblock fuer die konkrete Ursache.",
    ).to_dict()
