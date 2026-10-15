"""Tests for the real storage node HTTP service.

The node app is driven in-process through httpx's ASGI transport, so the
actual FastAPI routes, token check and on-disk chunk writes are covered.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from node_app.main import app as node_app
from node_app.main import settings as node_settings

TOKEN_HEADERS = {"X-Node-Token": node_settings.shared_token}


@pytest.fixture()
async def http():
    """Async client bound to the storage node ASGI app."""
    transport = httpx.ASGITransport(app=node_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        yield client


def _chunk_headers(blob: bytes) -> dict:
    return {**TOKEN_HEADERS, "X-Chunk-Checksum": hashlib.sha256(blob).hexdigest()}


# ---------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------
async def test_health_is_open(http):
    response = await http.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["uptime_seconds"] >= 0


async def test_metrics_reports_expected_fields(http):
    body = (await http.get("/metrics")).json()
    for field in (
        "cpu_usage",
        "ram_usage",
        "disk_usage",
        "capacity_bytes",
        "used_bytes",
        "free_bytes",
        "chunk_count",
        "uptime_seconds",
    ):
        assert field in body, f"missing metric {field}"

    assert 0.0 <= body["cpu_usage"] <= 1.0
    assert 0.0 <= body["ram_usage"] <= 1.0
    assert 0 <= body["used_bytes"] <= body["capacity_bytes"]


# ---------------------------------------------------------------------
# Chunk lifecycle
# ---------------------------------------------------------------------
async def test_put_get_head_delete_chunk(http):
    file_id, index = 900001, 0
    blob = b"encrypted-ish payload" * 20

    stored = await http.put(f"/chunks/{file_id}/{index}", content=blob, headers=_chunk_headers(blob))
    assert stored.status_code == 201
    assert stored.json()["checksum"] == hashlib.sha256(blob).hexdigest()

    head = await http.head(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)
    assert head.status_code == 200
    assert head.headers["X-Chunk-Size"] == str(len(blob))

    fetched = await http.get(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)
    assert fetched.status_code == 200
    assert fetched.content == blob

    removed = await http.delete(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)
    assert removed.status_code == 200
    assert removed.json()["status"] == "deleted"

    assert (await http.get(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)).status_code == 404
    assert (await http.head(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)).status_code == 404


async def test_overwriting_a_chunk_replaces_content(http):
    file_id, index = 900002, 0
    for blob in (b"first version", b"second version"):
        response = await http.put(
            f"/chunks/{file_id}/{index}", content=blob, headers=_chunk_headers(blob)
        )
        assert response.status_code == 201
    fetched = await http.get(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)
    assert fetched.content == b"second version"


async def test_missing_chunk_returns_404(http):
    assert (await http.get("/chunks/999999/0", headers=TOKEN_HEADERS)).status_code == 404


# ---------------------------------------------------------------------
# Validation & auth
# ---------------------------------------------------------------------
async def test_chunk_operations_require_token(http):
    assert (await http.get("/chunks/1/0")).status_code == 401
    assert (await http.put("/chunks/1/0", content=b"x")).status_code == 401
    assert (await http.delete("/chunks/1/0")).status_code == 401
    assert (
        await http.put(
            "/chunks/1/0",
            content=b"x",
            headers={**TOKEN_HEADERS, "X-Node-Token": "wrong-token"},
        )
    ).status_code == 401


async def test_wrong_checksum_header_is_rejected(http):
    blob = b"payload that will not match"
    headers = {**TOKEN_HEADERS, "X-Chunk-Checksum": "0" * 64}
    response = await http.put("/chunks/900003/0", content=blob, headers=headers)
    assert response.status_code == 400
    assert "checksum" in response.json()["detail"].lower()


async def test_empty_chunk_body_is_rejected(http):
    response = await http.put("/chunks/900004/0", content=b"", headers=TOKEN_HEADERS)
    assert response.status_code == 400


# ---------------------------------------------------------------------
# Purge & simulated outage
# ---------------------------------------------------------------------
async def test_delete_file_chunks_removes_everything(http):
    file_id = 900005
    for index in range(3):
        blob = f"chunk-{index}".encode()
        await http.put(
            f"/chunks/{file_id}/{index}", content=blob, headers=_chunk_headers(blob)
        )

    response = await http.delete(f"/files/{file_id}", headers=TOKEN_HEADERS)
    assert response.status_code == 200
    assert response.json()["removed"] == 3

    for index in range(3):
        assert (await http.get(f"/chunks/{file_id}/{index}", headers=TOKEN_HEADERS)).status_code == 404


async def test_simulated_outage_blocks_traffic(http):
    try:
        assert (await http.post("/admin/offline", headers=TOKEN_HEADERS)).status_code == 200
        assert (await http.get("/health")).status_code == 503
        assert (await http.get("/metrics")).status_code == 503
        blob = b"nope"
        assert (
            await http.put("/chunks/900006/0", content=blob, headers=_chunk_headers(blob))
        ).status_code == 503
    finally:
        assert (await http.post("/admin/online", headers=TOKEN_HEADERS)).status_code == 200

    assert (await http.get("/health")).status_code == 200
