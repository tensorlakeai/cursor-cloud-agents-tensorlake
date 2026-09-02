"""Thin client for the Cursor Cloud Agents REST API (workers and pools).

Only the orchestrator side calls these endpoints. Authentication is HTTP Basic
with the service-account key as the username and an empty password, the same
form the Cursor docs use with ``curl -u "$CURSOR_API_KEY:"``.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

LOGGER = logging.getLogger(__name__)


class CursorAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CursorAPI:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.cursor.com",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            transport=transport,
            base_url=base_url.rstrip("/"),
            auth=(api_key, ""),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    # --- pools ---

    def register_pool(
        self,
        pool_name: str,
        *,
        worker_ready_timeout_seconds: int = 0,
        repo_url: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        scope: str = "team",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "scope": scope,
            "poolName": pool_name,
            "workerReadyTimeoutSeconds": worker_ready_timeout_seconds,
        }
        if repo_url:
            body["repoUrl"] = repo_url
        if repo_owner:
            body["repoOwner"] = repo_owner
        if repo_name:
            body["repoName"] = repo_name
        return self._json("POST", "/v0/private-workers/pools", json=body)

    def deregister_pool(
        self,
        pool_name: str,
        *,
        repo_url: str | None = None,
        scope: str = "team",
    ) -> dict[str, Any]:
        """Soft-delete one pool row. Without ``repo_url`` this removes the any-repo row.

        The endpoint takes snake_case query parameters, unlike registration.
        ``repo_owner`` and ``repo_name`` select a repo-bound row.
        """
        params: dict[str, Any] = {"scope": scope, "pool_name": pool_name}
        if repo_url:
            segments = [s for s in urlsplit(repo_url).path.split("/") if s]
            if len(segments) < 2:
                raise ValueError("repo_url must be https://host/owner/repo")
            name = segments[-1][:-4] if segments[-1].endswith(".git") else segments[-1]
            params["repo_owner"] = segments[-2]
            params["repo_name"] = name
        return self._json("DELETE", "/v0/private-workers/pools", params=params)

    def list_pools(self, scope: str = "team_pool", include_stale: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"scope": scope}
        if include_stale:
            params["includeStale"] = "true"
        return self._json("GET", "/v0/private-workers/pools", params=params)

    def summary(self) -> dict[str, Any]:
        return self._json("GET", "/v0/private-workers/summary")

    # --- workers and requests ---

    def list_workers(self, status: str = "all", scope: str = "team_pool", limit: int = 100) -> dict[str, Any]:
        return self._json(
            "GET",
            "/v0/private-workers",
            params={"status": status, "scope": scope, "limit": limit},
        )

    def list_pending_requests(self, pool: str | None = None, limit: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if pool:
            params["pool"] = pool
        return self._json("GET", "/v0/private-workers/pending-requests", params=params)

    def claim(self, request_id: str, worker_id: str) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v0/private-workers/claim",
            json={"id": request_id, "workerId": worker_id},
        )

    def release_claim(self, request_id: str) -> None:
        self._json("POST", f"/v0/private-workers/claims/{quote(request_id, safe='')}/release")

    # --- agents ---

    def create_agent(self, prompt: str, pool: str, repo_url: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": {"text": prompt}, "env": {"type": "pool", "name": pool}}
        if repo_url:
            body["repos"] = [{"url": repo_url}]
        return self._json("POST", "/v1/agents", json=body)

    # --- plumbing ---

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CursorAPIError(f"{method} {path} failed: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            detail = response.text.strip()[:300]
            raise CursorAPIError(
                f"{method} {path} returned HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"items": data}


def pending_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Tolerate the list living under a few plausible keys."""
    for key in ("requests", "pendingRequests", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


__all__ = ["CursorAPI", "CursorAPIError", "pending_entries"]
