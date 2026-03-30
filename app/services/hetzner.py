from dataclasses import dataclass
from typing import Any, Optional

import requests
import requests.exceptions
from flask import current_app, has_app_context

from app.models import ProviderSetting


@dataclass
class ProvisionedServer:
    provider_server_id: str
    name: str
    ipv4: Optional[str]
    server_type: str
    location: str
    status: str

    @property
    def region(self) -> str:
        # Backward compatible alias for existing call sites.
        return self.location

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_server_id": self.provider_server_id,
            "name": self.name,
            "status": self.status,
            "ipv4": self.ipv4,
            "server_type": self.server_type,
            "location": self.location,
        }


class HetznerAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HetznerClient:
    BASE_URL = "https://api.hetzner.cloud/v1"

    @staticmethod
    def _provider_setting() -> ProviderSetting | None:
        if not has_app_context():
            return None
        return ProviderSetting.query.filter_by(provider_name="hetzner").first()

    def __init__(self, api_token: str | None = None):
        setting = self._provider_setting()
        config_token = current_app.config.get("HETZNER_API_TOKEN", "")
        # Prefer explicit argument, then DB-backed provider settings, then env/config fallback.
        self.api_token = api_token or (setting.api_token if setting else "") or config_token
        self.dry_run = current_app.config.get("ORBITAL_DRY_RUN", True)
        if not self.dry_run and not self.api_token:
            raise HetznerAPIError("HETZNER_API_TOKEN is not set")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.ok:
            return
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        raise HetznerAPIError(
            f"Hetzner API error {response.status_code}: {detail}",
            status_code=response.status_code,
        )

    def _post_json(self, path: str, *, payload: dict[str, Any], timeout: int = 30) -> dict:
        if not self.api_token:
            raise HetznerAPIError("Kein API-Token konfiguriert.", status_code=None)

        try:
            response = requests.post(
                f"{self.BASE_URL}{path}",
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise requests.exceptions.Timeout(
                "Verbindung zur Hetzner API hat zu lange gedauert (Timeout)."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise requests.exceptions.ConnectionError(
                "Hetzner API ist nicht erreichbar. Netzwerk prüfen."
            ) from exc

        if response.status_code == 401:
            raise HetznerAPIError("Ungültiges API-Token.", status_code=401)

        self._raise_for_status(response)
        return response.json()

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int = 15,
        force_live: bool = False,
    ) -> dict:
        if self.dry_run and not force_live:
            raise HetznerAPIError(
                "Dry-Run-Modus ist aktiv. Live-Abfrage deaktiviert.",
                status_code=None,
            )
        if not self.api_token:
            raise HetznerAPIError("Kein API-Token konfiguriert.", status_code=None)

        try:
            response = requests.get(
                f"{self.BASE_URL}{path}",
                headers=self._headers(),
                params=params,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise requests.exceptions.Timeout(
                "Verbindung zur Hetzner API hat zu lange gedauert (Timeout)."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise requests.exceptions.ConnectionError(
                "Hetzner API ist nicht erreichbar. Netzwerk prüfen."
            ) from exc

        if response.status_code == 401:
            raise HetznerAPIError("Ungültiges API-Token.", status_code=401)

        self._raise_for_status(response)
        return response.json()

    def _list_collection(
        self,
        *,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        force_live: bool = False,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        page = 1

        while page <= max_pages:
            query = {"page": page, "per_page": 50}
            if params:
                query.update(params)

            payload = self._get_json(path, params=query, force_live=force_live)
            combined.extend(payload.get(key, []))

            pagination = payload.get("meta", {}).get("pagination", {})
            next_page = pagination.get("next_page")
            if not next_page:
                break
            page = int(next_page)

        return combined

    @staticmethod
    def _parse_server(data: dict) -> ProvisionedServer:
        datacenter = data.get("datacenter") or {}
        location = (datacenter.get("location") or {}).get("name") or data.get("location") or ""
        server_type = (data.get("server_type") or {}).get("name") or data.get("server_type") or ""
        return ProvisionedServer(
            provider_server_id=str(data["id"]),
            name=data["name"],
            ipv4=data.get("public_net", {}).get("ipv4", {}).get("ip"),
            server_type=server_type,
            location=location,
            status=data.get("status") or "unknown",
        )

    def _resolve_server_defaults(
        self,
        *,
        server_type: str | None,
        location: str | None,
        image: str | None,
        project=None,
    ) -> tuple[str, str, str]:
        setting = self._provider_setting()

        project_server_type = getattr(project, "desired_server_type", None) if project else None
        project_location = getattr(project, "desired_location", None) if project else None
        project_image = getattr(project, "desired_image", None) if project else None

        resolved_server_type = server_type or project_server_type or (setting.default_server_type if setting else None) or "cx22"
        resolved_location = location or project_location or (setting.default_location if setting else None) or "nbg1"
        resolved_image = image or project_image or (setting.default_image if setting else None) or "ubuntu-24.04"
        return resolved_server_type, resolved_location, resolved_image

    def create_server(
        self,
        *,
        name: str,
        server_type: str | None = None,
        location: str | None = None,
        region: str | None = None,
        image: str | None = None,
        ssh_keys: list[str] | None = None,
        user_data: str | None = None,
        project=None,
    ) -> ProvisionedServer:
        resolved_server_type, resolved_location, resolved_image = self._resolve_server_defaults(
            server_type=server_type,
            location=location or region,
            image=image,
            project=project,
        )

        if self.dry_run:
            return ProvisionedServer(
                provider_server_id="dry-run-server-1",
                name=name,
                ipv4="203.0.113.10",
                server_type=resolved_server_type,
                location=resolved_location,
                status="running",
            )

        payload: dict = {
            "name": name,
            "server_type": resolved_server_type,
            "location": resolved_location,
            "image": resolved_image,
        }
        if not ssh_keys:
            setting = self._provider_setting()
            preferred_key = (setting.ssh_key_name if setting else "") or ""
            if preferred_key.strip():
                ssh_keys = [preferred_key.strip()]
        if ssh_keys:
            payload["ssh_keys"] = ssh_keys
        if user_data:
            payload["user_data"] = user_data

        data = self._post_json("/servers", payload=payload, timeout=30)
        if "server" not in data:
            raise HetznerAPIError("Hetzner API Antwort enthält keinen 'server'-Block.", status_code=None)
        return self._parse_server(data["server"])

    def create_server_for_project(
        self,
        *,
        project,
        deployment=None,
        name: str | None = None,
        server_type: str | None = None,
        location: str | None = None,
        image: str | None = None,
        ssh_keys: list[str] | None = None,
        user_data: str | None = None,
    ):
        # Local import avoids circulars at import time.
        from app.extensions import db
        from app.models import Server

        if name:
            final_name = name
        elif deployment is not None:
            final_name = f"orbital-{project.slug}-{deployment.id}"
        else:
            final_name = f"orbital-{project.slug}"
        provisioned = self.create_server(
            name=final_name,
            server_type=server_type,
            location=location,
            image=image,
            ssh_keys=ssh_keys,
            user_data=user_data,
            project=project,
        )

        server = Server(
            project_id=project.id,
            provider="hetzner",
            provider_server_id=provisioned.provider_server_id,
            name=provisioned.name,
            server_type=provisioned.server_type,
            region=provisioned.location,
            ipv4=provisioned.ipv4,
            status=provisioned.status,
        )
        db.session.add(server)
        db.session.commit()
        return server, provisioned

    def get_server(self, server_id: str, force_live: bool = False) -> ProvisionedServer:
        if self.dry_run and not force_live:
            return ProvisionedServer(
                provider_server_id=server_id,
                name=f"dry-run-{server_id}",
                ipv4="203.0.113.10",
                server_type="cx22",
                location="nbg1",
                status="running",
            )

        data = self._get_json(f"/servers/{server_id}", timeout=30, force_live=force_live)
        if "server" not in data:
            raise HetznerAPIError("Hetzner API Antwort enthält keinen 'server'-Block.", status_code=None)
        return self._parse_server(data["server"])

    def delete_server(self, server_id: str) -> None:
        if self.dry_run:
            return

        response = requests.delete(
            f"{self.BASE_URL}/servers/{server_id}",
            headers=self._headers(),
            timeout=30,
        )
        # 404 is acceptable – server may already be gone
        if response.status_code == 404:
            return
        self._raise_for_status(response)

    def test_connection(self, force_live: bool = False) -> dict:
        """Verify that the configured API token is valid.

        Calls GET /v1/datacenters – a lightweight read-only endpoint that requires
        authentication and returns basic infrastructure data.

        Returns a dict with ``ok=True`` and meta-info on success.
        Raises ``HetznerAPIError`` on authentication or API failure.
        Raises ``requests.exceptions.RequestException`` on network errors.
        """
        if self.dry_run and not force_live:
            return {"ok": True, "dry_run": True, "datacenters": 0}

        if not self.api_token:
            raise HetznerAPIError("Kein API-Token konfiguriert.", status_code=None)

        try:
            response = requests.get(
                f"{self.BASE_URL}/datacenters",
                headers=self._headers(),
                timeout=10,
            )
        except requests.exceptions.Timeout as exc:
            raise requests.exceptions.Timeout(
                "Verbindung zur Hetzner API hat zu lange gedauert (Timeout)."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise requests.exceptions.ConnectionError(
                "Hetzner API ist nicht erreichbar. Netzwerk prüfen."
            ) from exc

        if response.status_code == 401:
            raise HetznerAPIError("Ungültiges API-Token.", status_code=401)

        self._raise_for_status(response)
        data = response.json()
        dc_count = len(data.get("datacenters", []))
        return {"ok": True, "dry_run": False, "datacenters": dc_count}

    def list_server_types(self, force_live: bool = False) -> list[dict]:
        rows = self._list_collection(
            path="/server_types",
            key="server_types",
            force_live=force_live,
        )
        result = []
        for row in rows:
            result.append(
                {
                    "name": row.get("name") or "",
                    "description": row.get("description") or "",
                    "cores": row.get("cores"),
                    "memory_gb": row.get("memory"),
                    "disk_gb": row.get("disk"),
                    "architecture": row.get("architecture") or "",
                    "deprecated": bool(row.get("deprecated", False)),
                }
            )
        return sorted(result, key=lambda item: (item.get("cores") or 0, item.get("memory_gb") or 0, item["name"]))

    def list_locations(self, force_live: bool = False) -> list[dict]:
        rows = self._list_collection(
            path="/locations",
            key="locations",
            force_live=force_live,
        )
        result = []
        for row in rows:
            result.append(
                {
                    "name": row.get("name") or "",
                    "description": row.get("description") or "",
                    "city": row.get("city") or "",
                    "country": row.get("country") or "",
                    "network_zone": row.get("network_zone") or "",
                }
            )
        return sorted(result, key=lambda item: item["name"])

    def list_images(self, force_live: bool = False) -> list[dict]:
        rows = self._list_collection(
            path="/images",
            key="images",
            force_live=force_live,
            params={"type": "system", "status": "available"},
        )
        result = []
        for row in rows:
            image_id = row.get("id")
            image_name = row.get("name") or ""
            selector = image_name or (str(image_id) if image_id is not None else "")
            result.append(
                {
                    "id": image_id,
                    "selector": selector,
                    "name": row.get("name") or "",
                    "description": row.get("description") or "",
                    "type": row.get("type") or "",
                    "architecture": row.get("architecture") or "",
                    "os_flavor": row.get("os_flavor") or "",
                    "os_version": row.get("os_version") or "",
                    "deprecated": bool(row.get("deprecated", False)),
                }
            )
        return sorted(result, key=lambda item: (item["os_flavor"], item["name"] or item["description"]))

    def list_ssh_keys(self, force_live: bool = False) -> list[dict]:
        rows = self._list_collection(
            path="/ssh_keys",
            key="ssh_keys",
            force_live=force_live,
        )
        result = []
        for row in rows:
            result.append(
                {
                    "id": row.get("id"),
                    "name": row.get("name") or "",
                    "fingerprint": row.get("fingerprint") or "",
                }
            )
        return sorted(result, key=lambda item: item["name"])

    def create_ssh_key(self, *, name: str, public_key: str, force_live: bool = False) -> dict[str, Any]:
        key_name = (name or "").strip()
        key_value = (public_key or "").strip()
        if not key_name:
            raise HetznerAPIError("SSH-Key-Name fehlt.", status_code=None)
        if not key_value:
            raise HetznerAPIError("SSH Public Key fehlt.", status_code=None)

        if self.dry_run and not force_live:
            return {"id": 0, "name": key_name, "fingerprint": "dry-run"}

        payload = self._post_json(
            "/ssh_keys",
            payload={"name": key_name, "public_key": key_value},
            timeout=20,
        )
        row = payload.get("ssh_key") or {}
        return {
            "id": row.get("id"),
            "name": row.get("name") or key_name,
            "fingerprint": row.get("fingerprint") or "",
        }
