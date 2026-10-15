"""Async HTTP client the coordinator uses to talk to storage nodes."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Tests replace this with an httpx ASGITransport to talk to the node app
# in-process instead of over a real socket.
_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None


class NodeRequestError(RuntimeError):
    """Raised when a storage node call fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Install a custom transport (used by tests) and drop the cached client."""
    global _transport, _client
    _transport = transport
    _client = None


def get_client() -> httpx.AsyncClient:
    """Return the shared async HTTP client, creating it on first use."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            transport=_transport,
            timeout=httpx.Timeout(settings.node_request_timeout_seconds),
            headers={"X-Node-Token": settings.node_shared_token},
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
    return _client


async def close_client() -> None:
    """Close the shared client (called on application shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(method: str, base_url: str, path: str, **kwargs) -> httpx.Response:
    url = f"{base_url.rstrip('/')}{path}"
    client = get_client()
    try:
        return await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:  # covers connect/read/timeout errors
        raise NodeRequestError(f"{method} {url} failed: {exc}") from exc


async def health(base_url: str) -> dict[str, Any]:
    """Cheap liveness probe."""
    response = await _request("GET", base_url, "/health")
    if response.status_code != 200:
        raise NodeRequestError(
            f"health check returned {response.status_code}", status_code=response.status_code
        )
    return response.json()


async def metrics(base_url: str) -> dict[str, Any]:
    """Fetch live CPU / RAM / disk / uptime metrics from a node."""
    response = await _request("GET", base_url, "/metrics")
    if response.status_code != 200:
        raise NodeRequestError(
            f"metrics returned {response.status_code}", status_code=response.status_code
        )
    return response.json()


async def put_chunk(
    base_url: str, file_id: int, index: int, blob: bytes, checksum: str
) -> dict[str, Any]:
    """Store an encrypted chunk on a node."""
    response = await _request(
        "PUT",
        base_url,
        f"/chunks/{file_id}/{index}",
        content=blob,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Chunk-Checksum": checksum,
        },
    )
    if response.status_code not in (200, 201):
        raise NodeRequestError(
            f"put_chunk returned {response.status_code}: {response.text[:200]}",
            status_code=response.status_code,
        )
    return response.json()


async def get_chunk(base_url: str, file_id: int, index: int) -> bytes:
    """Download an encrypted chunk from a node."""
    response = await _request("GET", base_url, f"/chunks/{file_id}/{index}")
    if response.status_code != 200:
        raise NodeRequestError(
            f"get_chunk returned {response.status_code}", status_code=response.status_code
        )
    return response.content


async def delete_chunk(base_url: str, file_id: int, index: int) -> None:
    """Remove a chunk from a node."""
    response = await _request("DELETE", base_url, f"/chunks/{file_id}/{index}")
    if response.status_code not in (200, 204, 404):
        raise NodeRequestError(
            f"delete_chunk returned {response.status_code}", status_code=response.status_code
        )


async def set_simulated_offline(base_url: str, offline: bool) -> dict[str, Any]:
    """Toggle a node's simulated outage switch (demo/ops helper)."""
    path = "/admin/offline" if offline else "/admin/online"
    response = await _request("POST", base_url, path)
    if response.status_code != 200:
        raise NodeRequestError(
            f"{path} returned {response.status_code}", status_code=response.status_code
        )
    return response.json()


async def delete_file_chunks(base_url: str, file_id: int) -> None:
    """Remove every chunk of a file from a node."""
    response = await _request("DELETE", base_url, f"/files/{file_id}")
    if response.status_code not in (200, 204, 404):
        raise NodeRequestError(
            f"delete_file_chunks returned {response.status_code}",
            status_code=response.status_code,
        )
