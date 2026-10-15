"""Pydantic request/response schemas for the public API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# --- Auth -------------------------------------------------------------
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str | None = Field(default=None, max_length=255)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str | None = None
    is_admin: bool = False
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# --- Files ------------------------------------------------------------
class ReplicaOut(BaseModel):
    node_id: int
    node_name: str
    node_status: str
    status: str
    checksum: str
    stored_at: datetime


class ChunkOut(BaseModel):
    index: int
    plaintext_size: int
    blob_size: int
    checksum: str
    healthy_replicas: int


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    content_type: str | None = None
    size_bytes: int
    chunk_size: int
    total_chunks: int
    replication_factor: int
    file_checksum: str
    created_at: datetime
    healthy_replicas: int = 0
    health: str = "healthy"


class FileDetailOut(FileOut):
    chunks: list[ChunkOut] = Field(default_factory=list)


class UploadResultOut(BaseModel):
    file: FileOut
    placement: dict[int, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# --- Nodes ------------------------------------------------------------
class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    status: str
    is_active: bool
    capacity_bytes: int
    used_bytes: int
    free_bytes: int
    cpu_usage: float
    ram_usage: float
    disk_usage: float
    latency_ms: float
    uptime_seconds: float
    uptime_ratio: float
    reliability: float
    chunk_count: int
    write_requests: int
    write_failures: int
    read_requests: int
    read_failures: int
    last_seen: datetime | None = None
    last_error: str | None = None


class NodeListOut(BaseModel):
    nodes: list[NodeOut]
    online: int
    total: int
    total_capacity_bytes: int
    total_used_bytes: int


class MetricPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    recorded_at: datetime
    cpu_usage: float
    ram_usage: float
    disk_usage: float
    latency_ms: float
    free_bytes: int
    reliability: float
    uptime_ratio: float
    is_online: bool
    predicted_score: float | None = None


class NodeScoreOut(BaseModel):
    """A node ranked by the AI selector for a hypothetical placement."""

    node_id: int
    name: str
    ml_score: float
    heuristic_score: float
    final_score: float
    eligible: bool
    reason: str | None = None


class ModelInfoOut(BaseModel):
    trained: bool
    model_type: str = "RandomForestClassifier"
    trained_at: datetime | None = None
    n_samples: int = 0
    n_estimators: int = 0
    max_depth: int | None = None
    accuracy: float | None = None
    roc_auc: float | None = None
    feature_names: list[str] = Field(default_factory=list)
    feature_importances: dict[str, float] = Field(default_factory=dict)
    model_path: str = ""


class RepairResultOut(BaseModel):
    checked_chunks: int
    under_replicated: int
    repaired: int
    failed: int
    offline_nodes: int
    details: list[str] = Field(default_factory=list)


class ReconcileResultOut(BaseModel):
    """Outcome of the rebalancing pass that trims surplus replicas."""

    checked_chunks: int
    surplus_chunks: int
    pruned: int
    failed: int
    details: list[str] = Field(default_factory=list)


class MessageOut(BaseModel):
    message: str
