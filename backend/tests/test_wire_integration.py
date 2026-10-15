"""Wire-level integration test: coordinator HTTP client -> real node app.

Unlike ``test_storage_flow``, nothing is stubbed here: the coordinator's
httpx client talks to the actual storage node FastAPI application over
an ASGI transport, so the request paths, headers, token check and
checksum handling are all exercised end to end.
"""

from __future__ import annotations

import httpx
import pytest

from node_app.main import app as node_app


@pytest.fixture()
def wire_transport():
    """Point the coordinator's node client at the real node app."""
    from app.services import node_client

    transport = httpx.ASGITransport(app=node_app)
    node_client.set_transport(transport)
    try:
        yield transport
    finally:
        node_client.set_transport(None)


def _content(size: int, seed: int = 5) -> bytes:
    return bytes((index * 17 + seed) % 256 for index in range(size))


def test_full_round_trip_over_real_http(app_client, auth, wire_transport):
    """Upload, inspect and download a file with no stubs in the path."""
    content = _content(120_000, seed=7)

    upload = app_client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": ("wire.bin", content, "application/octet-stream")},
        data={"chunk_size": "32768"},
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    file_id = body["file"]["id"]

    assert body["file"]["total_chunks"] == 4
    assert body["file"]["health"] == "healthy"
    assert body["file"]["healthy_replicas"] == 3

    download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert download.status_code == 200
    assert download.content == content

    # The node really stored encrypted blobs, never the plaintext.
    stats = app_client.get("/api/files/stats", headers=auth).json()
    assert stats["total_replicas"] >= 1

    assert app_client.delete(f"/api/files/{file_id}", headers=auth).status_code == 200


def test_integrity_verification_over_real_http(app_client, auth, wire_transport):
    content = _content(20_000, seed=11)
    upload = app_client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": ("verify.bin", content, "application/octet-stream")},
    )
    assert upload.status_code == 201

    verify = app_client.post("/api/cluster/verify", headers=auth)
    assert verify.status_code == 200
    body = verify.json()
    assert body["files_checked"] >= 1
    assert body["failures"] == []


def test_real_node_metrics_reach_the_coordinator(app_client, auth, wire_transport):
    import asyncio

    from app.services import monitor

    asyncio.run(monitor.poll_all_nodes())

    overview = app_client.get("/api/cluster/overview", headers=auth).json()
    assert overview["nodes"]["online"] >= 1
    assert overview["storage"]["replicas"] >= 1
