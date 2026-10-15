"""Role-based access control and the replica rebalancing pass."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

ADMIN_ONLY_ENDPOINTS: list[tuple[str, str]] = [
    ("post", "/api/nodes?name=extra&url=http://extra:9000"),
    ("post", "/api/nodes/sync"),
    ("post", "/api/nodes/1/offline"),
    ("post", "/api/nodes/1/online"),
    ("delete", "/api/nodes/1"),
    ("post", "/api/cluster/model/train"),
    ("post", "/api/cluster/repair"),
    ("post", "/api/cluster/reconcile"),
    ("get", "/api/cluster/repair/events"),
]

MEMBER_READABLE_ENDPOINTS: list[str] = [
    "/api/nodes",
    "/api/nodes/1",
    "/api/nodes/1/metrics",
    "/api/cluster/overview",
    "/api/cluster/model",
    "/api/files",
    "/api/files/stats",
]


# ---------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------
def test_admin_flag_is_reported(app_client, auth, member_auth, cluster):
    assert app_client.get("/api/auth/me", headers=auth).json()["is_admin"] is True
    assert app_client.get("/api/auth/me", headers=member_auth).json()["is_admin"] is False


def test_member_is_blocked_from_admin_endpoints(app_client, member_auth, cluster):
    for method, path in ADMIN_ONLY_ENDPOINTS:
        response = getattr(app_client, method)(path, headers=member_auth)
        assert response.status_code == 403, f"{method.upper()} {path} -> {response.status_code}"


def test_admin_endpoints_require_authentication(app_client, cluster):
    assert app_client.post("/api/cluster/repair").status_code == 401
    assert app_client.post("/api/nodes/sync").status_code == 401


def test_member_can_read_cluster_state(app_client, member_auth, cluster):
    for path in MEMBER_READABLE_ENDPOINTS:
        response = app_client.get(path, headers=member_auth)
        assert response.status_code == 200, f"GET {path} -> {response.status_code}"

    preview = app_client.post(
        "/api/cluster/placement/preview?required_bytes=4096", headers=member_auth
    )
    assert preview.status_code == 200


def test_member_can_manage_their_own_files(app_client, member_auth, cluster):
    upload = app_client.post(
        "/api/files/upload",
        headers=member_auth,
        files={"file": ("member.bin", b"member data" * 100, "application/octet-stream")},
    )
    assert upload.status_code == 201
    file_id = upload.json()["file"]["id"]

    assert app_client.get("/api/files", headers=member_auth).status_code == 200
    assert app_client.get(f"/api/files/{file_id}", headers=member_auth).status_code == 200
    assert app_client.get(f"/api/files/{file_id}/download", headers=member_auth).status_code == 200

    # Integrity verification is scoped to the caller's own files.
    assert app_client.post("/api/cluster/verify", headers=member_auth).status_code == 200
    assert app_client.delete(f"/api/files/{file_id}", headers=member_auth).status_code == 200


def test_admin_can_use_safe_admin_endpoints(app_client, auth, cluster):
    assert app_client.post("/api/nodes/sync", headers=auth).status_code == 200
    assert app_client.get("/api/cluster/repair/events", headers=auth).status_code == 200
    assert app_client.post("/api/cluster/repair", headers=auth).status_code == 200
    assert app_client.post("/api/cluster/reconcile", headers=auth).status_code == 200


# ---------------------------------------------------------------------
# Rebalancing
# ---------------------------------------------------------------------
def _add_surplus_replica(app_client, auth, cluster, name: str) -> tuple[int, str]:
    """Upload a single-chunk file and add a fourth replica by hand."""
    from app.database import SessionLocal
    from app.models import ChunkReplica, FileChunk, StorageNode

    upload = app_client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": (name, b"rebalance payload" * 80, "application/octet-stream")},
        data={"chunk_size": "65536"},
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["file"]["id"]
    assert cluster.replica_count(file_id, 0) == 3

    db = SessionLocal()
    try:
        chunk = db.scalar(
            select(FileChunk)
            .where(FileChunk.file_id == file_id, FileChunk.index == 0)
            .options(selectinload(FileChunk.replicas))
        )
        used = {replica.node_id for replica in chunk.replicas}
        spare = db.scalar(select(StorageNode).where(StorageNode.id.notin_(used)))
        source = chunk.replicas[0].node

        # Copy an existing chunk blob onto the spare node, as a stale
        # self-healing copy would look after the original node returned.
        cluster.nodes[spare.url].chunks[(file_id, 0)] = cluster.nodes[source.url].chunks[
            (file_id, 0)
        ]
        db.add(
            ChunkReplica(
                chunk_id=chunk.id,
                node_id=spare.id,
                status="active",
                checksum=chunk.checksum,
            )
        )
        db.commit()
        return file_id, spare.url
    finally:
        db.close()


def test_reconcile_prunes_surplus_replicas(app_client, auth, cluster):
    file_id, spare_url = _add_surplus_replica(app_client, auth, cluster, "surplus.bin")
    assert cluster.replica_count(file_id, 0) == 4

    before_total = cluster.total_chunks()
    result = app_client.post("/api/cluster/reconcile", headers=auth).json()

    assert result["pruned"] == 1
    assert result["failed"] == 0
    assert result["surplus_chunks"] >= 1

    # Back to exactly three copies, and the surplus blob is gone from the node.
    assert cluster.replica_count(file_id, 0) == 3
    assert cluster.total_chunks() == before_total - 1
    assert (file_id, 0) not in cluster.nodes[spare_url].chunks

    # The file is still complete and readable.
    detail = app_client.get(f"/api/files/{file_id}", headers=auth).json()
    assert detail["health"] == "healthy"
    assert detail["healthy_replicas"] == 3
    download = app_client.get(f"/api/files/{file_id}/download", headers=auth)
    assert download.status_code == 200
    assert download.content == b"rebalance payload" * 80


def test_reconcile_is_idempotent(app_client, auth, cluster):
    _add_surplus_replica(app_client, auth, cluster, "idempotent.bin")
    app_client.post("/api/cluster/reconcile", headers=auth)

    second = app_client.post("/api/cluster/reconcile", headers=auth).json()
    assert second["pruned"] == 0


def test_reconcile_purges_corrupt_replicas(app_client, auth, cluster):
    upload = app_client.post(
        "/api/files/upload",
        headers=auth,
        files={"file": ("corrupt.bin", b"damaged payload" * 40, "application/octet-stream")},
        data={"chunk_size": "65536"},
    )
    file_id = upload.json()["file"]["id"]

    # Damage every copy so they are all flagged corrupt on read.
    for url in cluster.holder_urls(file_id, 0):
        blob = bytearray(cluster.nodes[url].chunks[(file_id, 0)])
        blob[7] ^= 0x5A
        cluster.nodes[url].chunks[(file_id, 0)] = bytes(blob)

    assert app_client.get(f"/api/files/{file_id}/download", headers=auth).status_code == 502

    result = app_client.post("/api/cluster/reconcile", headers=auth).json()
    assert result["pruned"] == 3
    assert cluster.replica_count(file_id, 0) == 0

    replicas = app_client.get(f"/api/files/{file_id}/replicas", headers=auth).json()
    assert replicas == []


def test_monitor_prunes_surplus_when_every_node_is_healthy(app_client, auth, cluster, monkeypatch):
    """The background pass rebalances on its own once the cluster recovers."""
    from app.config import get_settings
    from app.services import monitor

    _add_surplus_replica(app_client, auth, cluster, "auto.bin")
    monkeypatch.setattr(get_settings(), "reconcile_enabled", True)
    monkeypatch.setattr(monitor, "_last_reconcile_at", 0.0)

    summary = asyncio.run(monitor.poll_all_nodes())

    assert summary["offline"] == 0
    assert summary["pruned"] >= 1


def test_monitor_does_not_rebalance_while_a_node_is_down(app_client, auth, cluster, monkeypatch):
    from app.config import get_settings
    from app.services import monitor

    file_id, _spare = _add_surplus_replica(app_client, auth, cluster, "down.bin")
    monkeypatch.setattr(get_settings(), "reconcile_enabled", True)
    monkeypatch.setattr(monitor, "_last_reconcile_at", 0.0)

    node_id = None
    try:
        nodes = app_client.get("/api/nodes", headers=auth).json()["nodes"]
        node_id = nodes[0]["id"]
        app_client.post(f"/api/nodes/{node_id}/offline", headers=auth)

        summary = asyncio.run(monitor.poll_all_nodes())
        assert summary["offline"] >= 1
        # Rebalancing must wait until the cluster is whole again.
        assert summary["pruned"] == 0
        assert cluster.replica_count(file_id, 0) == 4
    finally:
        if node_id is not None:
            app_client.post(f"/api/nodes/{node_id}/online", headers=auth)
        for node in cluster.nodes.values():
            node.offline = False
