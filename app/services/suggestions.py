from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


SEVERITY_RANK: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class FloriSuggestion:
    suggestion_type: str
    title: str
    message: str
    severity: str
    source: str
    suggested_action: str | None = None
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "suggestion_type": self.suggestion_type,
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "source": self.source,
            "suggested_action": self.suggested_action,
            "confidence": self.confidence,
        }
        if payload["confidence"] is not None:
            payload["confidence"] = round(float(payload["confidence"]), 3)
        return payload


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_str(value: Any) -> str:
    return str(value or "")


def _join_nonempty(parts: list[str]) -> str:
    return "\n".join(part for part in parts if part)


def _steps_sorted(deployment: Any) -> list[Any]:
    return sorted(getattr(deployment, "steps", []) or [], key=lambda step: (getattr(step, "order_index", 0), getattr(step, "id", 0)))


def _latest_step(deployment: Any, name: str) -> Any | None:
    matches = [step for step in _steps_sorted(deployment) if getattr(step, "name", "") == name]
    return matches[-1] if matches else None


def _latest_failed_step(deployment: Any) -> Any | None:
    steps = _steps_sorted(deployment)
    failed = [step for step in steps if getattr(step, "status", "") == "failed"]
    return failed[-1] if failed else None


def _extract_missing_env_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"([A-Z][A-Z0-9_]{2,})\s+not\s+set",
        r"environment variable\s+([A-Z][A-Z0-9_]{2,})",
        r"keyerror:\s*'([A-Z][A-Z0-9_]{2,})'",
        r"missing env(?:ironment variable)?[:\s]+([A-Z][A-Z0-9_]{2,})",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            normalized = _safe_str(match).upper()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
    return candidates


def translate_technical_issue_to_flori_message(
    suggestion_type: str,
    *,
    technical_issue: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, str]:
    ctx = context or {}

    if suggestion_type == "missing_env":
        env_name = _safe_str(ctx.get("env_name") or "").strip().upper()
        if env_name:
            return {
                "title": "Eine App-Einstellung fehlt noch",
                "message": f"Deine App braucht noch die Umgebungsvariable {env_name}.",
            }
        return {
            "title": "Eine App-Einstellung fehlt noch",
            "message": "Deine App braucht noch mindestens eine fehlende Umgebungsvariable.",
        }

    if suggestion_type == "detected_port":
        port = _safe_str(ctx.get("port") or "").strip()
        if port:
            return {
                "title": "Port erkannt",
                "message": f"Ich habe Port {port} erkannt und nutze ihn fuer deine App.",
            }
        return {
            "title": "Port erkannt",
            "message": "Ich habe den App-Port erkannt und fuer dein Deployment verwendet.",
        }

    if suggestion_type == "db_unreachable":
        return {
            "title": "Datenbank nicht erreichbar",
            "message": "Deine Datenbank ist aktuell nicht erreichbar.",
        }

    if suggestion_type == "dns_mismatch":
        resolved_ip = _safe_str(ctx.get("resolved_ip") or "-").strip() or "-"
        expected_ip = _safe_str(ctx.get("expected_ip") or "-").strip() or "-"
        return {
            "title": "Domain zeigt auf die falsche IP",
            "message": (
                "Deine Domain zeigt noch nicht auf den Zielserver "
                f"(aktuell {resolved_ip}, erwartet {expected_ip})."
            ),
        }

    if suggestion_type == "deployment_stable":
        return {
            "title": "Deployment laeuft stabil",
            "message": "Dein letzter Deploy laeuft stabil.",
        }

    if suggestion_type == "runtime_warning":
        return {
            "title": "Dein Projekt braucht Aufmerksamkeit",
            "message": "Dein Projekt ist erreichbar, aber es gibt gerade einen Warnzustand.",
        }

    return {
        "title": "Hinweis von Flori",
        "message": technical_issue or "Ich habe einen neuen Hinweis fuer dich gefunden.",
    }


def _deduplicate_and_sort(items: list[FloriSuggestion]) -> list[dict[str, Any]]:
    dedup: dict[tuple[str, str], FloriSuggestion] = {}
    for item in items:
        key = (item.suggestion_type, item.message)
        prev = dedup.get(key)
        if prev is None:
            dedup[key] = item
            continue
        prev_rank = SEVERITY_RANK.get(prev.severity, 99)
        cur_rank = SEVERITY_RANK.get(item.severity, 99)
        if cur_rank < prev_rank:
            dedup[key] = item

    ordered = sorted(
        dedup.values(),
        key=lambda row: (
            SEVERITY_RANK.get(row.severity, 99),
            -(row.confidence if row.confidence is not None else 0.0),
            row.title,
        ),
    )
    return [entry.to_dict() for entry in ordered]


def generate_deployment_suggestions(deployment: Any, runtime_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    suggestions: list[FloriSuggestion] = []
    runtime = runtime_state or {}

    analysis = _safe_dict(getattr(deployment, "error_analysis_json", None))
    error_type = _safe_str(analysis.get("error_type")).strip().lower()
    confidence = analysis.get("confidence")

    failed_step = _latest_failed_step(deployment)
    failed_details = _safe_dict(getattr(failed_step, "json_details", None)) if failed_step else {}

    failed_text = _join_nonempty(
        [
            _safe_str(getattr(deployment, "error_message", None)),
            _safe_str(getattr(deployment, "output", None)),
            _safe_str(getattr(failed_step, "stderr", None) if failed_step else ""),
            _safe_str(getattr(failed_step, "stdout", None) if failed_step else ""),
            _safe_str(failed_details.get("error_details")),
        ]
    )

    missing_env_names = _extract_missing_env_candidates(failed_text)
    if error_type == "missing_env" and not missing_env_names:
        missing_env_names = ["DATABASE_URL"]

    for env_name in missing_env_names[:2]:
        translation = translate_technical_issue_to_flori_message("missing_env", context={"env_name": env_name})
        suggestions.append(
            FloriSuggestion(
                suggestion_type="missing_env",
                title=translation["title"],
                message=translation["message"],
                severity="warning",
                source="error_analysis",
                suggested_action="App-Einstellungen oeffnen und Variable setzen",
                confidence=confidence if isinstance(confidence, (int, float)) else 0.7,
            )
        )

    dns_step = _latest_step(deployment, "check_dns")
    dns_meta = _safe_dict(getattr(dns_step, "json_details", None)) if dns_step else {}
    dns_matches = dns_meta.get("matches") if isinstance(dns_meta.get("matches"), bool) else None
    lower_failed_text = failed_text.lower()
    dns_signal = (
        (getattr(dns_step, "status", "") == "failed" and dns_matches is False)
        or "dns-pruefung fehlgeschlagen" in lower_failed_text
        or "domain zeigt nicht auf den zielserver" in lower_failed_text
    )
    if dns_signal:
        translation = translate_technical_issue_to_flori_message(
            "dns_mismatch",
            context={
                "resolved_ip": dns_meta.get("resolved_ip"),
                "expected_ip": dns_meta.get("expected_ip"),
            },
        )
        suggestions.append(
            FloriSuggestion(
                suggestion_type="dns_mismatch",
                title=translation["title"],
                message=translation["message"],
                severity="critical",
                source="deployment_step_logs",
                suggested_action="DNS A-Record auf die erwartete Ziel-IP setzen",
                confidence=0.95,
            )
        )

    db_signal = (
        "psycopg2" in lower_failed_text
        or "db connection" in lower_failed_text
        or "database unreachable" in lower_failed_text
        or ("database" in lower_failed_text and "database_url" not in lower_failed_text)
    )
    if error_type == "db_connection" or db_signal:
        translation = translate_technical_issue_to_flori_message("db_unreachable")
        suggestions.append(
            FloriSuggestion(
                suggestion_type="db_unreachable",
                title=translation["title"],
                message=translation["message"],
                severity="critical",
                source="runtime_state" if runtime else "error_analysis",
                suggested_action="DB-Zugangsdaten und Netzwerk pruefen",
                confidence=confidence if isinstance(confidence, (int, float)) else 0.78,
            )
        )

    analyze_step = _latest_step(deployment, "analyze_repository")
    analyze_meta = _safe_dict(getattr(analyze_step, "json_details", None)) if analyze_step else {}
    detected_port = analyze_meta.get("port")
    if isinstance(detected_port, int) and detected_port > 0:
        translation = translate_technical_issue_to_flori_message("detected_port", context={"port": detected_port})
        suggestions.append(
            FloriSuggestion(
                suggestion_type="detected_port",
                title=translation["title"],
                message=translation["message"],
                severity="info",
                source="repository_analysis",
                confidence=0.9,
            )
        )

    runtime_status = _safe_str(runtime.get("current_runtime_status")).lower()
    if runtime_status == "running":
        translation = translate_technical_issue_to_flori_message("deployment_stable")
        suggestions.append(
            FloriSuggestion(
                suggestion_type="deployment_stable",
                title=translation["title"],
                message=translation["message"],
                severity="info",
                source="runtime_state",
                confidence=0.88,
            )
        )
    elif runtime_status == "degraded":
        translation = translate_technical_issue_to_flori_message("runtime_warning")
        suggestions.append(
            FloriSuggestion(
                suggestion_type="runtime_warning",
                title=translation["title"],
                message=translation["message"],
                severity="warning",
                source="runtime_state",
                confidence=0.7,
            )
        )

    return _deduplicate_and_sort(suggestions)


def generate_project_suggestions(project: Any, runtime_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    runtime = runtime_state or {}
    suggestions: list[FloriSuggestion] = []

    deployments = sorted(getattr(project, "deployments", []) or [], key=lambda d: getattr(d, "created_at", 0), reverse=True)
    latest_deployment = deployments[0] if deployments else None

    if latest_deployment is not None:
        suggestions.extend(
            FloriSuggestion(**item) for item in generate_deployment_suggestions(latest_deployment, runtime_state=runtime)
        )

    configured_env_keys = {
        _safe_str(getattr(item, "key", "")).upper()
        for item in (getattr(project, "environment_variables", []) or [])
        if _safe_str(getattr(item, "key", "")).strip()
    }

    if "DATABASE_URL" not in configured_env_keys and _safe_str(getattr(project, "framework", "")).lower() in {"flask", "django", "laravel"}:
        translation = translate_technical_issue_to_flori_message("missing_env", context={"env_name": "DATABASE_URL"})
        suggestions.append(
            FloriSuggestion(
                suggestion_type="missing_env",
                title=translation["title"],
                message=translation["message"],
                severity="warning",
                source="environment_variables",
                suggested_action="App-Einstellungen oeffnen",
                confidence=0.65,
            )
        )

    if latest_deployment is None and getattr(project, "repository", None):
        translation = translate_technical_issue_to_flori_message("detected_port", context={"port": 3000 if _safe_str(getattr(project, "framework", "")).lower() == "node" else 8000})
        suggestions.append(
            FloriSuggestion(
                suggestion_type="detected_port",
                title=translation["title"],
                message=translation["message"],
                severity="info",
                source="stack_detection",
                confidence=0.6,
            )
        )

    return _deduplicate_and_sort(suggestions)
