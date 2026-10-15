"""Shared pytest fixtures for the CloudVaultX coordinator test-suite.

The environment is configured *before* any application module is
imported so the settings singleton and the SQLAlchemy engine pick up the
throwaway test database.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STORAGE_NODE_DIR = REPO_ROOT / "storage_node"
if str(STORAGE_NODE_DIR) not in sys.path:
    sys.path.insert(0, str(STORAGE_NODE_DIR))

TMP_ROOT = Path(tempfile.mkdtemp(prefix="cloudvaultx-tests-"))

NODE_URLS = [
    "http://node-a:9000",
    "http://node-b:9000",
    "http://node-c:9000",
    "http://node-d:9000",
]
SHARED_TOKEN = "test-node-token"

os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{(TMP_ROOT / 'test.db').as_posix()}",
        "MASTER_KEY": base64.b64encode(bytes(range(32))).decode(),
        "JWT_SECRET": "test-jwt-secret-that-is-long-enough-for-hs256",
        "NODE_TOKEN": SHARED_TOKEN,
        "NODE_URLS": ",".join(NODE_URLS),
        "NODE_CAPACITY_BYTES": str(64 * 1024 * 1024),
        "CHUNK_SIZE_BYTES": str(64 * 1024),
        "REPLICATION_FACTOR": "3",
        "MONITOR_ENABLED": "false",
        "RECONCILE_ENABLED": "false",
        "AUTO_REPAIR_ENABLED": "true",
        "NODE_OFFLINE_AFTER_FAILURES": "2",
        "AUTO_TRAIN_MODEL": "true",
        "ML_WEIGHT": "0.7",
        "MODEL_PATH": str(TMP_ROOT / "model.joblib"),
        "API_PREFIX": "/api",
        # Storage node service settings (used by the real node app tests).
        "NODE_NAME": "test-node",
        "DATA_DIR": str(TMP_ROOT / "node_data"),
        "SHARED_TOKEN": SHARED_TOKEN,
        "CAPACITY_BYTES": str(64 * 1024 * 1024),
    }
)


# ---------------------------------------------------------------------
# Fake in-memory storage cluster
# ---------------------------------------------------------------------
class FakeNode:
    """A simulated storage node with its own chunk store and metrics."""

    def __init__(self, url: str, capacity_bytes: int = 64 * 1024 * 1024) -> None:
        self.url = url
        self.capacity_bytes = capacity_bytes
        self.chunks: dict[tuple[int, int], bytes] = {}
        self.offline = False
        self.cpu_usage = 0.2
        self.ram_usage = 0.3
        self.uptime_seconds = 3600.0
        self.calls = {"put": 0, "get": 0, "delete": 0}

    @property
    def used_bytes(self) -> int:
        return sum(len(blob) for blob in self.chunks.values())

    def metrics_payload(self) -> dict:
        used = self.used_bytes
        return {
            "node_name": self.url,
            "status": "ok",
            "cpu_usage": self.cpu_usage,
            "ram_usage": self.ram_usage,
            "disk_usage": used / self.capacity_bytes,
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": used,
            "free_bytes": max(0, self.capacity_bytes - used),
            "chunk_count": len(self.chunks),
            "uptime_seconds": self.uptime_seconds,
        }


class FakeCluster:
    """Routes node_client calls to in-memory nodes and patches the client."""

    def __init__(self, urls: list[str]) -> None:
        self.nodes: dict[str, FakeNode] = {url: FakeNode(url) for url in urls}

    # -- helpers -------------------------------------------------------

    def node(self, url: str) -> FakeNode:
        node = self.nodes.get(url.rstrip("/"))
        if node is None:
            from app.services.node_client import NodeRequestError

            raise NodeRequestError(f"unknown node {url}")
        return node

    def _guard(self, url: str) -> FakeNode:
        node = self.node(url)
        if node.offline:
            from app.services.node_client import NodeRequestError

            raise NodeRequestError(f"{url} is offline", status_code=503)
        return node

    def replica_count(self, file_id: int, index: int) -> int:
        return sum(
            1 for node in self.nodes.values() if (file_id, index) in node.chunks
        )

    def holder_urls(self, file_id: int, index: int) -> list[str]:
        return [url for url, node in self.nodes.items() if (file_id, index) in node.chunks]

    def total_chunks(self) -> int:
        return sum(len(node.chunks) for node in self.nodes.values())

    # -- client API shims ---------------------------------------------

    async def health(self, base_url: str) -> dict:
        return {"status": "ok", "node_name": base_url}

    async def metrics(self, base_url: str) -> dict:
        return self._guard(base_url).metrics_payload()

    async def put_chunk(self, base_url: str, file_id: int, index: int, blob: bytes, checksum: str) -> dict:
        import hashlib

        node = self._guard(base_url)
        if hashlib.sha256(blob).hexdigest() != checksum:
            from app.services.node_client import NodeRequestError

            raise NodeRequestError("checksum mismatch", status_code=400)
        node.chunks[(file_id, index)] = blob
        node.calls["put"] += 1
        return {"status": "stored", "size_bytes": len(blob), "checksum": checksum}

    async def get_chunk(self, base_url: str, file_id: int, index: int) -> bytes:
        from app.services.node_client import NodeRequestError

        node = self._guard(base_url)
        node.calls["get"] += 1
        blob = node.chunks.get((file_id, index))
        if blob is None:
            raise NodeRequestError("chunk not found", status_code=404)
        return blob

    async def delete_chunk(self, base_url: str, file_id: int, index: int) -> None:
        node = self._guard(base_url)
        node.chunks.pop((file_id, index), None)
        node.calls["delete"] += 1

    async def delete_file_chunks(self, base_url: str, file_id: int) -> None:
        node = self._guard(base_url)
        for key in [key for key in node.chunks if key[0] == file_id]:
            del node.chunks[key]

    async def set_simulated_offline(self, base_url: str, offline: bool) -> dict:
        self.node(base_url).offline = offline
        return {"status": "offline" if offline else "online"}


@pytest.fixture(scope="session")
def fake_cluster():
    """Session-wide fake cluster (patching happens per-test via attach)."""
    return FakeCluster(NODE_URLS)


@pytest.fixture()
def cluster(monkeypatch, fake_cluster):
    """Patch node_client so the coordinator talks to the fake cluster."""
    from app.services import node_client

    for name in (
        "health",
        "metrics",
        "put_chunk",
        "get_chunk",
        "delete_chunk",
        "delete_file_chunks",
        "set_simulated_offline",
    ):
        monkeypatch.setattr(node_client, name, getattr(fake_cluster, name))
    return fake_cluster


@pytest.fixture(scope="session")
def app_client():
    """A TestClient driving the coordinator app (lifespan enabled)."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


def register_account(app_client, *, as_admin: bool, label: str = "user") -> str:
    """Register a fresh account and return its access token.

    ``as_admin`` promotes the account directly in the database, which is
    deterministic regardless of how many users already exist (only the very
    first account of a deployment is auto-promoted).
    """
    import uuid

    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    response = app_client.post(
        "/api/auth/register",
        json={"email": email, "password": "supersecret123", "full_name": "Test User"},
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]

    if as_admin:
        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import User

        db = SessionLocal()
        try:
            user = db.scalar(select(User).where(User.email == email))
            user.is_admin = True
            db.commit()
        finally:
            db.close()
    return token


@pytest.fixture()
def user_token(app_client, cluster) -> str:
    """Token for an administrator, since most tests manage the cluster."""
    return register_account(app_client, as_admin=True)


@pytest.fixture()
def member_token(app_client, cluster) -> str:
    """Token for a regular (non-administrator) account."""
    return register_account(app_client, as_admin=False, label="member")


@pytest.fixture()
def auth(user_token) -> dict:
    return {"Authorization": f"Bearer {user_token}"}


@pytest.fixture()
def member_auth(member_token) -> dict:
    return {"Authorization": f"Bearer {member_token}"}
