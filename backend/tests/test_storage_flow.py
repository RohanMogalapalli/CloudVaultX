"""End-to-end tests of upload, placement, retrieval and self-healing.

These drive the real coordinator HTTP API while the storage nodes are
simulated by :class:`conftest.FakeCluster`, so the coordinator's
encryption, chunking, replica placement and repair logic are all
exercised for real.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

NODE_URLS = [
    "http://node-a:9000",
    "http://node-b:9000",
    "http://node-c:9000",
    "http://node-d:9000",
]


def _upload(client, auth, content: bytes, name: str = "payload.bin", **form):
    return client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": (name, content, "application/octet-stream")},
        data={key: str(value) for key, value in form.items()},
    )


def _refresh_metrics() -> dict:
    """Run one monitoring pass so the DB holds the fake nodes' current metrics."""
    import asyncio

    from app.services import monitor

    return asyncio.run(monitor.poll_all_nodes())


def _node_id_for_url(client, auth, url: str) -> int:
    host = urlparse(url).hostname
    nodes = client.get("/api/nodes", headers=auth).json()["nodes"]
    return next(node["id"] for node in nodes if node["name"] == host)


def _content(size: int, seed: int = 1) -> bytes:
    return bytes((index * 31 + seed) % 256 for index in range(size))


# ---------------------------------------------------------------------
# Upload & placement
# ---------------------------------------------------------------------
def test_upload_creates_chunks_with_full_replication(app_client, auth, cluster):
    content = _content(200_000, seed=3)
    response = _upload(app_client, auth, content, chunk_size=65_536)
    assert response.status_code == 201, response.text

    body = response.json()
    file_id = body["file"]["id"]

    # 200000 bytes at 64 KiB per chunk -> 4 chunks
    assert body["file"]["total_chunks"] == 4
    assert body["file"]["size_bytes"] == len(content)
    assert body["file"]["health"] == "healthy"
    assert body["file"]["healthy_replicas"] == 3
    assert body["warnings"] == []
    assert len(body["placement"]) == 4

    for index, holders in body["placement"].items():
        assert len(holders) == 3
        assert len(set(holders)) == 3, "replicas must live on distinct nodes"
        assert cluster.replica_count(file_id, int(index)) == 3

    # Every stored blob must be ciphertext, never the raw plaintext.
    for node in cluster.nodes.values():
        for blob in node.chunks.values():
            assert blob != content


def test_download_round_trip_matches_original(app_client, auth, cluster):
    content = _content(150_000, seed=9)
    upload = _upload(app_client, auth, content, name="report.bin", chunk_size=32_768)
    file_id = upload.json()["file"]["id"]

    download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert download.status_code == 200
    assert download.content == content
    assert "report.bin" in download.headers["content-disposition"]


def test_download_round_trip_for_exact_chunk_multiple(app_client, auth, cluster):
    content = _content(65_536, seed=4)  # exactly one chunk
    upload = _upload(app_client, auth, content, chunk_size=65_536)
    file_id = upload.json()["file"]["id"]
    assert upload.json()["file"]["total_chunks"] == 1

    download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert download.content == content


def test_empty_file_is_stored_and_retrieved(app_client, auth, cluster):
    upload = _upload(app_client, auth, b"", name="empty.bin")
    assert upload.status_code == 201
    file_id = upload.json()["file"]["id"]
    assert upload.json()["file"]["total_chunks"] == 1

    download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert download.status_code == 200
    assert download.content == b""


def test_upload_rejects_replication_factor_above_cluster_size(app_client, auth, cluster):
    response = _upload(app_client, auth, b"data", replication_factor=9)
    assert response.status_code == 422
    assert "exceeds" in response.json()["detail"]


def test_upload_rejects_out_of_range_chunk_size(app_client, auth, cluster):
    assert _upload(app_client, auth, b"data", chunk_size=100).status_code == 422
    assert _upload(app_client, auth, b"data", chunk_size=99_999_999).status_code == 422


def test_upload_requires_authentication(app_client, cluster):
    assert _upload(app_client, {}, b"data").status_code == 401


# ---------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------
def test_download_falls_back_when_one_replica_is_corrupt(app_client, auth, cluster):
    """A single damaged copy must never make a file unreadable."""
    content = _content(40_000, seed=11)  # single chunk
    upload = _upload(app_client, auth, content, chunk_size=65_536)
    file_id = upload.json()["file"]["id"]

    holders = cluster.holder_urls(file_id, 0)
    assert len(holders) == 3
    victim = holders[0]
    blob = bytearray(cluster.nodes[victim].chunks[(file_id, 0)])
    blob[20] ^= 0xFF
    cluster.nodes[victim].chunks[(file_id, 0)] = bytes(blob)

    # Two intact replicas remain, so retrieval must still succeed.
    first = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert first.status_code == 200
    assert first.content == content

    # And the file survives repeated reads while the copy stays damaged.
    second = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert second.status_code == 200
    assert second.content == content


def test_corrupt_replicas_are_detected_and_marked(app_client, auth, cluster):
    """When every copy is damaged the coordinator must refuse to serve it."""
    content = _content(20_000, seed=13)  # single chunk
    upload = _upload(app_client, auth, content, chunk_size=65_536)
    file_id = upload.json()["file"]["id"]

    for url in cluster.holder_urls(file_id, 0):
        blob = bytearray(cluster.nodes[url].chunks[(file_id, 0)])
        blob[5] ^= 0xAA
        cluster.nodes[url].chunks[(file_id, 0)] = bytes(blob)

    download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert download.status_code == 502
    assert "chunk 0" in download.json()["detail"]

    replicas = app_client.get(f"/api/files/{file_id}/replicas", headers=auth).json()
    assert len(replicas) == 3
    assert all(replica["status"] == "corrupt" for replica in replicas), replicas

    # Corruption is also surfaced as an audit event.
    events = app_client.get("/api/cluster/repair/events", headers=auth).json()
    assert any(event["kind"] == "corruption_detected" for event in events)

    # And the file is reported as unhealthy rather than silently broken.
    detail = app_client.get(f"/api/files/{file_id}", headers=auth).json()
    assert detail["health"] == "critical"
    assert detail["healthy_replicas"] == 0


def test_verify_endpoint_reports_integrity(app_client, auth, cluster):
    content = _content(30_000, seed=17)
    _upload(app_client, auth, content, chunk_size=65_536)

    response = app_client.post("/api/cluster/verify", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["files_verified"] == body["files_checked"] >= 1
    assert body["failures"] == []


def test_file_checksum_matches_plaintext(app_client, auth, cluster):
    import hashlib

    content = _content(5000, seed=19)
    upload = _upload(app_client, auth, content)
    recorded = upload.json()["file"]["file_checksum"]
    assert recorded == hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------
# Fault tolerance & self-healing
# ---------------------------------------------------------------------
def test_self_healing_restores_replicas_after_node_failure(app_client, auth, cluster):
    content = _content(100_000, seed=23)
    upload = _upload(app_client, auth, content, chunk_size=65_536)
    file_id = upload.json()["file"]["id"]

    holders_before = set(cluster.holder_urls(file_id, 0))
    assert len(holders_before) == 3
    failed_url = sorted(holders_before)[0]
    failed_id = _node_id_for_url(app_client, auth, failed_url)

    try:
        # Take the node down: the coordinator marks it offline.
        assert (
            app_client.post(f"/api/nodes/{failed_id}/offline", headers=auth).status_code == 200
        )

        degraded = app_client.get(f"/api/files/{file_id}", headers=auth).json()
        assert degraded["health"] == "degraded"
        assert degraded["healthy_replicas"] == 2

        repair = app_client.post("/api/cluster/repair", headers=auth)
        assert repair.status_code == 200
        assert repair.json()["repaired"] >= 1

        healed = app_client.get(f"/api/files/{file_id}", headers=auth).json()
        assert healed["health"] == "healthy"
        assert healed["healthy_replicas"] == 3

        # The replacement replica landed on a node that did not hold it before.
        holders_after = set(cluster.holder_urls(file_id, 0))
        new_holders = holders_after - holders_before
        assert new_holders, "expected a replica on a previously unused node"

        # And the file is still downloadable while the node is down.
        download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
        assert download.status_code == 200
        assert download.content == content
    finally:
        app_client.post(f"/api/nodes/{failed_id}/online", headers=auth)
        cluster.nodes[failed_url].offline = False


def test_repair_is_idempotent_when_cluster_healthy(app_client, auth, cluster):
    _upload(app_client, auth, _content(10_000, seed=29))
    response = app_client.post("/api/cluster/repair", headers=auth)
    assert response.status_code == 200
    assert response.json()["repaired"] == 0


def test_offline_node_is_excluded_from_placement(app_client, auth, cluster):
    node_id = _node_id_for_url(app_client, auth, NODE_URLS[0])
    try:
        app_client.post(f"/api/nodes/{node_id}/offline", headers=auth)
        preview = app_client.post(
            "/api/cluster/placement/preview",
            headers=auth,
            params={"required_bytes": 1024, "count": 3},
        ).json()
        excluded = [row for row in preview if row["node_id"] == node_id]
        assert excluded and excluded[0]["eligible"] is False
        assert "status" in excluded[0]["reason"]
    finally:
        app_client.post(f"/api/nodes/{node_id}/online", headers=auth)
        cluster.nodes[NODE_URLS[0]].offline = False


def test_placement_preview_explains_ranking(app_client, auth, cluster):
    response = app_client.post(
        "/api/cluster/placement/preview", headers=auth, params={"required_bytes": 4096}
    )
    assert response.status_code == 200
    rows = response.json()
    assert rows
    for row in rows:
        assert 0.0 <= row["ml_score"] <= 1.0
        assert 0.0 <= row["final_score"] <= 1.0
        assert row["reason"] is None or isinstance(row["reason"], str)


def test_prefers_the_healthier_node(app_client, auth, cluster):
    """A heavily loaded node should not outrank an idle one."""
    for url in NODE_URLS:
        node = cluster.nodes[url]
        node.cpu_usage, node.ram_usage, node.uptime_seconds = 0.1, 0.2, 7200.0

    stressed = cluster.nodes[NODE_URLS[0]]
    stressed.cpu_usage, stressed.ram_usage, stressed.uptime_seconds = 0.97, 0.95, 5.0

    # Refresh the coordinator's cached metrics for every node.
    _refresh_metrics()

    preview = app_client.post(
        "/api/cluster/placement/preview", headers=auth, params={"required_bytes": 1024}
    ).json()
    stressed_row = next(row for row in preview if row["name"] == "node-a")
    idle_row = next(row for row in preview if row["name"] == "node-b")
    assert idle_row["final_score"] > stressed_row["final_score"]


# ---------------------------------------------------------------------
# Listing, isolation and deletion
# ---------------------------------------------------------------------
def test_list_files_returns_uploaded_files(app_client, auth, cluster):
    name = f"listing-{uuid.uuid4().hex[:8]}.bin"
    file_id = _upload(app_client, auth, _content(1000, seed=31), name=name).json()["file"]["id"]

    files = app_client.get("/api/files", headers=auth).json()
    assert any(item["id"] == file_id and item["filename"] == name for item in files)


def test_file_stats_endpoint(app_client, auth, cluster):
    _upload(app_client, auth, _content(2048, seed=37))
    stats = app_client.get("/api/files/stats", headers=auth).json()
    assert stats["total_files"] >= 1
    assert stats["total_chunks"] >= 1
    assert stats["total_replicas"] >= stats["total_chunks"]
    assert stats["total_bytes"] >= 2048


def test_files_are_isolated_between_users(app_client, auth, cluster):
    file_id = _upload(app_client, auth, _content(500, seed=41)).json()["file"]["id"]

    other = app_client.post(
        "/api/auth/register",
        json={"email": f"intruder-{uuid.uuid4().hex[:8]}@example.com", "password": "supersecret123"},
    ).json()["access_token"]
    other_auth = {"Authorization": f"Bearer {other}"}

    assert app_client.get(f"/api/files/{file_id}", headers=other_auth).status_code == 404
    assert app_client.get(f"/api/files/{file_id}/download", headers=other_auth).status_code == 404
    assert app_client.delete(f"/api/files/{file_id}", headers=other_auth).status_code == 404
    assert app_client.get(f"/api/files/{file_id}", headers=auth).status_code == 200


def test_delete_removes_every_replica(app_client, auth, cluster):
    file_id = _upload(app_client, auth, _content(60_000, seed=43), chunk_size=32_768).json()[
        "file"
    ]["id"]
    assert cluster.total_chunks() > 0

    response = app_client.delete(f"/api/files/{file_id}", headers=auth)
    assert response.status_code == 200

    assert cluster.replica_count(file_id, 0) == 0
    assert cluster.replica_count(file_id, 1) == 0
    assert app_client.get(f"/api/files/{file_id}", headers=auth).status_code == 404
    assert app_client.get(f"/api/files/{file_id}/download", headers=auth).status_code == 404


def test_missing_file_returns_404(app_client, auth, cluster):
    assert app_client.get("/api/files/99999999", headers=auth).status_code == 404
    assert app_client.get("/api/files/99999999/download", headers=auth).status_code == 404


# ---------------------------------------------------------------------
# Cluster & node endpoints
# ---------------------------------------------------------------------
def test_cluster_overview_reports_health(app_client, auth, cluster):
    body = app_client.get("/api/cluster/overview", headers=auth).json()
    assert body["nodes"]["total"] == 4
    assert body["nodes"]["online"] == 4
    assert body["storage"]["files"] >= 1
    assert body["storage"]["replication_target"] == 3
    assert body["ai"]["trained"] is True
    assert 0.0 <= body["ai"]["average_node_score"] <= 1.0


def test_node_list_and_metrics_history(app_client, auth, cluster):
    listing = app_client.get("/api/nodes", headers=auth).json()
    assert listing["total"] == 4
    node_id = listing["nodes"][0]["id"]

    detail = app_client.get(f"/api/nodes/{node_id}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["capacity_bytes"] > 0

    _refresh_metrics()

    metrics = app_client.get(f"/api/nodes/{node_id}/metrics", headers=auth).json()
    assert metrics
    assert "cpu_usage" in metrics[-1]


def test_model_endpoint_reports_random_forest(app_client, auth, cluster):
    body = app_client.get("/api/cluster/model", headers=auth).json()
    assert body["trained"] is True
    assert body["model_type"] == "RandomForestClassifier"
    assert body["accuracy"] > 0.8
    assert len(body["feature_names"]) == 7
    assert body["feature_importances"]


def test_model_can_be_retrained(app_client, auth, cluster):
    response = app_client.post(
        "/api/cluster/model/train", headers=auth, params={"samples": 800, "n_estimators": 40}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trained"] is True
    assert body["n_samples"] == 800
    assert body["trained_at"] is not None


def test_unknown_node_returns_404(app_client, auth, cluster):
    assert app_client.get("/api/nodes/424242", headers=auth).status_code == 404


def test_node_recovery_handles_timezone_aware_downtime(app_client, auth, cluster):
    """Regression: Postgres returns TIMESTAMPTZ values as aware datetimes.

    Recovery and the monitor loop both subtract ``last_offline_at`` from the
    current (naive) time, which raises TypeError when the value read back from
    Postgres is timezone aware.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal
    from app.models import StorageNode
    from app.routers import nodes as nodes_router

    node_id = _node_id_for_url(app_client, auth, NODE_URLS[2])
    app_client.post(f"/api/nodes/{node_id}/offline", headers=auth)

    db = SessionLocal()
    try:
        node = db.get(StorageNode, node_id)
        # Mimic what psycopg returns for a TIMESTAMPTZ column.
        node.last_offline_at = datetime.now(timezone.utc) - timedelta(seconds=45)
        before = node.total_downtime_seconds
        db.commit()

        # expire_on_commit=False keeps the aware value in the identity map,
        # so the router sees exactly what Postgres would hand it.
        result = asyncio.run(nodes_router.recover_node(node_id, db, None))

        assert result.status == "online"
        assert result.uptime_ratio <= 1.0

        refreshed = db.get(StorageNode, node_id)
        assert refreshed.total_downtime_seconds > before
        # The downtime window is closed once the node is back.
        assert refreshed.last_offline_at is None
    finally:
        node = db.get(StorageNode, node_id)
        node.status = "online"
        node.is_active = True
        db.commit()
        db.close()
        cluster.nodes[NODE_URLS[2]].offline = False


def test_health_endpoint_is_public(app_client, cluster):
    body = app_client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["model_trained"] is True
