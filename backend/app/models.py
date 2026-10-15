"""ORM models: users, files, chunks, replicas, storage nodes and metrics."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# Node status values (kept as plain strings for SQLite friendliness).
NODE_ONLINE = "online"
NODE_OFFLINE = "offline"
NODE_DEGRADED = "degraded"

# Replica status values.
REPLICA_ACTIVE = "active"
REPLICA_MISSING = "missing"
REPLICA_CORRUPT = "corrupt"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Administrators may manage nodes, retrain the model and trigger repairs.
    # server_default keeps this column addable to an existing table.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    files: Mapped[list["StorageFile"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class StorageNode(Base):
    """A storage node known to the coordinator, plus its latest metrics."""

    __tablename__ = "storage_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=NODE_ONLINE, nullable=False)
    capacity_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Latest metrics pushed by the node / measured by the coordinator.
    cpu_usage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ram_usage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    disk_usage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    free_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    uptime_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_downtime_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Reliability bookkeeping.
    write_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    write_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    read_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    read_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_offline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    metrics: Mapped[list["NodeMetric"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )
    replicas: Mapped[list["ChunkReplica"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )

    @property
    def reliability(self) -> float:
        """Ratio of successful storage operations (0..1)."""
        total = self.write_requests + self.read_requests
        failures = self.write_failures + self.read_failures
        if total <= 0:
            return 0.5  # unknown history -> neutral prior
        return max(0.0, (total - failures) / total)

    @property
    def uptime_ratio(self) -> float:
        """Observed availability (0..1) from uptime vs accumulated downtime."""
        total = self.uptime_seconds + self.total_downtime_seconds
        if total <= 0:
            return 1.0
        return max(0.0, min(1.0, self.uptime_seconds / total))

    @property
    def effective_free_bytes(self) -> int:
        """Free space available for planning.

        A node that the coordinator has never polled (``last_seen`` is NULL)
        is assumed to have its full configured capacity available, so a
        freshly registered node can accept chunks before the first poll.
        """
        if self.last_seen is None and self.capacity_bytes > 0:
            return self.capacity_bytes
        return self.free_bytes

    @property
    def free_space_ratio(self) -> float:
        if self.capacity_bytes <= 0:
            return 0.0
        return max(0.0, min(1.0, self.effective_free_bytes / self.capacity_bytes))


class NodeMetric(Base):
    """Time-series snapshot of a node reading, used for dashboards and ML."""

    __tablename__ = "node_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("storage_nodes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    cpu_usage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ram_usage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    disk_usage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    free_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    uptime_ratio: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    reliability: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    is_online: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    predicted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    node: Mapped[StorageNode] = relationship(back_populates="metrics")


class StorageFile(Base):
    """An uploaded file owned by a user."""

    __tablename__ = "storage_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replication_factor: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # SHA-256 of the original plaintext file (integrity check on download).
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    # Per-file HKDF salt (base64) used to derive the AES-256 file key.
    key_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    owner: Mapped[User] = relationship(back_populates="files")
    chunks: Mapped[list["FileChunk"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="FileChunk.index"
    )


class FileChunk(Base):
    """One encrypted chunk of a file."""

    __tablename__ = "file_chunks"
    __table_args__ = (UniqueConstraint("file_id", "index", name="uq_chunk_file_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("storage_files.id", ondelete="CASCADE"), index=True, nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    plaintext_size: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # SHA-256 of the encrypted blob as stored on the node.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    file: Mapped[StorageFile] = relationship(back_populates="chunks")
    replicas: Mapped[list["ChunkReplica"]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan"
    )


class ChunkReplica(Base):
    """A copy of a chunk held by a specific storage node."""

    __tablename__ = "chunk_replicas"
    __table_args__ = (
        UniqueConstraint("chunk_id", "node_id", name="uq_replica_chunk_node"),
        Index("ix_replica_node_status", "node_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("file_chunks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    node_id: Mapped[int] = mapped_column(
        ForeignKey("storage_nodes.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default=REPLICA_ACTIVE, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    chunk: Mapped[FileChunk] = relationship(back_populates="replicas")
    node: Mapped[StorageNode] = relationship(back_populates="replicas")


class RepairEvent(Base):
    """Audit trail of self-healing actions taken by the coordinator."""

    __tablename__ = "repair_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
