from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class ErrorAnalysis:
    error_type: str
    probable_cause: str
    suggested_fix: str
    confidence: float = 0.5
    matched_patterns: list[str] | None = None
    affected_step: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "error_type": self.error_type,
            "confidence": round(float(self.confidence), 3),
            "probable_cause": self.probable_cause,
            "matched_patterns": self.matched_patterns or [],
            "affected_step": self.affected_step,
            "suggested_fix": self.suggested_fix,
        }


def _compose_haystack(*parts: str | None) -> str:
    chunks = [(part or "").strip().lower() for part in parts if part]
    return "\n".join(chunks)


def _contains_any(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)


ERROR_CLASS_PATTERNS: dict[str, list[str]] = {
    "missing_env": [
        r"keyerror",
        r"environment variable not set",
        r"missing env",
        r"missing config",
        r"database_url not set",
        r"secret_key not set",
        r"no such file or directory: '.env'",
        r"undefined variable",
    ],
    "port_conflict": [
        r"address already in use",
        r"port is already allocated",
        r"bind failed",
        r"listen tcp .*: bind",
        r"eaddrinuse",
        r"port .* already in use",
    ],
    "db_connection": [
        r"could not connect to server",
        r"connection refused",
        r"timeout while connecting to database",
        r"authentication failed",
        r"sqlalchemy\.exc\.operationalerror",
        r"psycopg2",
        r"db is unavailable",
        r"no route to host",
    ],
    "build_fail": [
        r"docker build",
        r"failed to solve",
        r"failed to build",
        r"error building",
        r"pip install",
        r"npm install",
        r"yarn install",
        r"requirements\.txt",
        r"package-lock\.json",
        r"module not found",
        r"no such file or directory",
        r"build failed",
    ],
}


ERROR_CLASS_META: dict[str, dict[str, str]] = {
    "missing_env": {
        "probable_cause": "Deiner App fehlt eine notwendige Umgebungsvariable.",
        "suggested_fix": "Pruefe die App-Einstellungen, ergaenze fehlende Variablen (z. B. DATABASE_URL/SECRET_KEY) und starte Redeploy.",
    },
    "port_conflict": {
        "probable_cause": "Ein benoetigter Port ist bereits belegt.",
        "suggested_fix": "Pruefe Portbelegung auf dem Server und passe Container-/Compose-Portmapping an.",
    },
    "db_connection": {
        "probable_cause": "Die Anwendung kann die Datenbank nicht erreichen.",
        "suggested_fix": "Pruefe DB-Zugangsdaten, DB-Host/Port, Netzwerkregeln und ob die Datenbankinstanz laeuft.",
    },
    "build_fail": {
        "probable_cause": "Der Build deiner Anwendung ist fehlgeschlagen.",
        "suggested_fix": "Pruefe Build-Logs, Dockerfile und Abhaengigkeiten (requirements/package lock files).",
    },
    "unknown_error": {
        "probable_cause": "Die genaue Ursache konnte nicht automatisch klassifiziert werden.",
        "suggested_fix": "Oeffne die Step-Logs (stdout/stderr) und pruefe den ersten klaren Fehlerblock.",
    },
}


def classify_log_patterns(log_text: str) -> list[dict[str, Any]]:
    text = (log_text or "").lower()
    scored: list[dict[str, Any]] = []

    for error_type, patterns in ERROR_CLASS_PATTERNS.items():
        matched: list[str] = []
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                matched.append(pattern)
        if not matched:
            continue

        coverage = len(matched) / max(len(patterns), 1)
        confidence = min(0.55 + (0.12 * len(matched)) + coverage * 0.1, 0.99)
        scored.append(
            {
                "error_type": error_type,
                "confidence": round(confidence, 3),
                "matched_patterns": matched,
            }
        )

    scored.sort(key=lambda item: item["confidence"], reverse=True)
    return scored


def _build_step_haystack(step: Any) -> str:
    details = step.json_details if isinstance(getattr(step, "json_details", None), dict) else {}
    events = details.get("events") if isinstance(details, dict) else []
    event_lines: list[str] = []
    if isinstance(events, list):
        for evt in events:
            if not isinstance(evt, dict):
                continue
            event_lines.append(str(evt.get("message") or ""))
            event_lines.append(str(evt.get("source") or ""))
            if isinstance(evt.get("context"), dict):
                event_lines.append(str(evt.get("context")))

    return _compose_haystack(
        getattr(step, "name", None),
        getattr(step, "stdout", None),
        getattr(step, "stderr", None),
        getattr(step, "output", None),
        getattr(step, "error_message", None),
        "\n".join(event_lines),
        str(details.get("error_details") or ""),
        str(details.get("failed_commands") or ""),
    )


def build_error_summary(
    *,
    primary_error: dict[str, Any] | None,
    secondary_errors: list[dict[str, Any]] | None,
    affected_step: str | None,
) -> dict[str, Any]:
    if not primary_error:
        meta = ERROR_CLASS_META["unknown_error"]
        fallback = {
            "error_type": "unknown_error",
            "confidence": 0.3,
            "probable_cause": meta["probable_cause"],
            "matched_patterns": [],
            "affected_step": affected_step,
            "suggested_fix": meta["suggested_fix"],
        }
        return {
            **fallback,
            "primary_error": fallback,
            "secondary_errors": [],
        }

    error_type = primary_error["error_type"]
    meta = ERROR_CLASS_META.get(error_type, ERROR_CLASS_META["unknown_error"])
    summary = {
        "error_type": error_type,
        "confidence": primary_error.get("confidence", 0.5),
        "probable_cause": meta["probable_cause"],
        "matched_patterns": primary_error.get("matched_patterns", []),
        "affected_step": affected_step,
        "suggested_fix": meta["suggested_fix"],
    }

    secondaries: list[dict[str, Any]] = []
    for item in secondary_errors or []:
        sec_type = item.get("error_type", "unknown_error")
        sec_meta = ERROR_CLASS_META.get(sec_type, ERROR_CLASS_META["unknown_error"])
        secondaries.append(
            {
                "error_type": sec_type,
                "confidence": item.get("confidence", 0.4),
                "probable_cause": sec_meta["probable_cause"],
                "matched_patterns": item.get("matched_patterns", []),
                "suggested_fix": sec_meta["suggested_fix"],
            }
        )

    summary["primary_error"] = dict(summary)
    summary["secondary_errors"] = secondaries
    return summary


def analyze_deployment_errors(deployment: Any) -> dict[str, Any]:
    steps = sorted(getattr(deployment, "steps", []) or [], key=lambda s: (getattr(s, "order_index", 0), getattr(s, "id", 0)))
    failed_steps = [step for step in steps if getattr(step, "status", "") == "failed"]
    weighted_segments: list[str] = []

    for step in steps:
        segment = _build_step_haystack(step)
        if not segment:
            continue
        # Failed step gets higher weight for better primary classification.
        if step in failed_steps:
            weighted_segments.extend([segment, segment, segment])
        else:
            weighted_segments.append(segment)

    deployment_haystack = _compose_haystack(
        getattr(deployment, "error_message", None),
        getattr(deployment, "output", None),
        "\n".join(weighted_segments),
    )

    classifications = classify_log_patterns(deployment_haystack)
    primary = classifications[0] if classifications else None
    secondary = classifications[1:3] if len(classifications) > 1 else []
    affected_step = failed_steps[0].name if failed_steps else (steps[-1].name if steps else None)
    return build_error_summary(primary_error=primary, secondary_errors=secondary, affected_step=affected_step)


def analyze_deployment_failure(
    *,
    step_name: str,
    stdout: str | None,
    stderr: str | None,
    error_category: str | None = None,
    exception_type: str | None = None,
) -> dict[str, str]:
    haystack = _compose_haystack(step_name, error_category, exception_type, stdout, stderr)
    classified = classify_log_patterns(haystack)
    primary = classified[0] if classified else None
    summary = build_error_summary(primary_error=primary, secondary_errors=classified[1:2], affected_step=step_name)
    return summary
