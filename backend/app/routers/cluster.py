"""Cluster-level endpoints: overview, ML model control, repair and placement."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..dependencies import AdminUser, CurrentUser, DbSession
from ..ml import node_selector
from ..models import (
    ChunkReplica,
    FileChunk,
    RepairEvent,
    StorageFile,
    StorageNode,
)
from ..schemas import ModelInfoOut, NodeScoreOut, ReconcileResultOut, RepairResultOut
from ..services import node_registry, placement, storage_service
from ..services.storage_service import StorageError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cluster", tags=["cluster"])
settings = get_settings()


def _model_info() -> ModelInfoOut:
    metadata = node_selector.selector.metadata
    return ModelInfoOut(
        trained=node_selector.selector.is_trained,
        trained_at=metadata.trained_at,
        n_samples=metadata.n_samples,
        n_estimators=metadata.n_estimators,
        max_depth=metadata.max_depth,
        accuracy=metadata.accuracy,
        roc_auc=metadata.roc_auc,
        feature_names=metadata.feature_names,
        feature_importances=metadata.feature_importances,
        model_path=metadata.model_path,
    )


@router.get("/overview")
def cluster_overview(db: DbSession, user: CurrentUser) -> dict:
    """High-level health of the whole cluster, for the dashboard."""
    nodes = node_registry.list_nodes(db)
    online = [node for node in nodes if node.status == "online" and node.is_active]

    total_chunks = db.scalar(select(func.count(FileChunk.id))) or 0
    total_files = db.scalar(select(func.count(StorageFile.id))) or 0
    total_bytes = db.scalar(select(func.coalesce(func.sum(StorageFile.size_bytes), 0))) or 0
    total_replicas = db.scalar(select(func.count(ChunkReplica.id))) or 0

    healthy_replicas = db.scalar(
        select(func.count(ChunkReplica.id))
        .join(StorageNode, StorageNode.id == ChunkReplica.node_id)
        .where(
            ChunkReplica.status == "active",
            StorageNode.is_active.is_(True),
            StorageNode.status == "online",
        )
    ) or 0

    degraded_files = 0
    statement = select(StorageFile).options(
        selectinload(StorageFile.chunks)
        .selectinload(FileChunk.replicas)
        .selectinload(ChunkReplica.node)
    )
    for record in db.scalars(statement).all():
        _minimum, health = storage_service.assess_file(record)
        if health != "healthy":
            degraded_files += 1

    scores = placement.predict_scores_for_all(db)
    avg_score = sum(scores.values()) / len(scores) if scores else 0.0

    return {
        "nodes": {
            "total": len(nodes),
            "online": len(online),
            "offline": sum(1 for node in nodes if node.status != "online"),
            "total_capacity_bytes": sum(node.capacity_bytes for node in nodes),
            "total_used_bytes": sum(node.used_bytes for node in nodes),
        },
        "storage": {
            "files": total_files,
            "chunks": total_chunks,
            "logical_bytes": int(total_bytes),
            "replicas": total_replicas,
            "healthy_replicas": int(healthy_replicas),
            "replication_target": settings.replication_factor,
            "degraded_files": degraded_files,
        },
        "ai": {
            "trained": node_selector.selector.is_trained,
            "ml_weight": node_selector.selector.ml_weight,
            "average_node_score": round(avg_score, 4),
            "accuracy": node_selector.selector.metadata.accuracy,
        },
    }


@router.get("/model", response_model=ModelInfoOut)
def get_model(user: CurrentUser) -> ModelInfoOut:
    """Details of the Random Forest that ranks storage nodes."""
    return _model_info()


@router.post("/model/train", response_model=ModelInfoOut)
async def retrain_model(
    user: AdminUser,
    samples: Annotated[int, Query(ge=500, le=50_000)] = 6000,
    n_estimators: Annotated[int, Query(ge=10, le=1000)] = 200,
) -> ModelInfoOut:
    """Retrain the node-selection Random Forest on fresh synthetic data."""
    # Training is CPU bound: keep the event loop responsive.
    await asyncio.to_thread(
        node_selector.selector.train_and_save,
        n_samples=samples,
        n_estimators=n_estimators,
    )
    return _model_info()


@router.post("/placement/preview", response_model=list[NodeScoreOut])
def placement_preview(
    db: DbSession,
    user: CurrentUser,
    required_bytes: Annotated[int, Query(ge=0)] = 262_144,
    count: Annotated[int, Query(ge=1, le=20)] = 3,
) -> list[NodeScoreOut]:
    """Explain which nodes the AI would pick for a chunk of a given size."""
    ranked = placement.rank_nodes(db, required_bytes=required_bytes)
    return [
        NodeScoreOut(
            node_id=candidate.node.id,
            name=candidate.node.name,
            ml_score=round(candidate.ml_score, 4),
            heuristic_score=round(candidate.heuristic_score, 4),
            final_score=round(candidate.final_score, 4),
            eligible=candidate.eligible,
            reason=candidate.reason,
        )
        for candidate in ranked[: max(count * 2, 6)]
    ]


@router.post("/repair", response_model=RepairResultOut)
async def trigger_repair(
    db: DbSession,
    user: AdminUser,
    max_repairs: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> RepairResultOut:
    """Run the self-healing replication pass on demand."""
    outcome = await storage_service.repair_under_replicated(db, max_repairs=max_repairs)
    return RepairResultOut(
        checked_chunks=outcome.checked_chunks,
        under_replicated=outcome.under_replicated,
        repaired=outcome.repaired,
        failed=outcome.failed,
        offline_nodes=outcome.offline_nodes,
        details=outcome.details[:50],
    )


@router.post("/reconcile", response_model=ReconcileResultOut)
async def trigger_reconcile(
    db: DbSession,
    user: AdminUser,
    max_prune: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> ReconcileResultOut:
    """Rebalance the cluster: trim surplus and corrupt replicas."""
    outcome = await storage_service.reconcile_replicas(db, max_prune=max_prune)
    return ReconcileResultOut(
        checked_chunks=outcome.checked_chunks,
        surplus_chunks=outcome.surplus_chunks,
        pruned=outcome.pruned,
        failed=outcome.failed,
        details=outcome.details[:50],
    )


@router.get("/repair/events")
def repair_events(
    db: DbSession,
    user: AdminUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> list[dict]:
    """Recent self-healing / corruption events."""
    statement = select(RepairEvent).order_by(RepairEvent.created_at.desc(), RepairEvent.id.desc()).limit(limit)
    return [
        {
            "id": event.id,
            "kind": event.kind,
            "file_id": event.file_id,
            "chunk_index": event.chunk_index,
            "detail": event.detail,
            "created_at": event.created_at,
        }
        for event in db.scalars(statement).all()
    ]


@router.post("/verify")
async def verify_integrity(db: DbSession, user: CurrentUser) -> dict:
    """Re-read every chunk of the user's files and verify its checksum."""
    statement = (
        select(StorageFile)
        .where(StorageFile.owner_id == user.id)
        .options(
            selectinload(StorageFile.chunks)
            .selectinload(FileChunk.replicas)
            .selectinload(ChunkReplica.node)
        )
    )
    checked = 0
    verified = 0
    failures: list[str] = []

    for record in db.scalars(statement).all():
        try:
            path = await storage_service.retrieve_file(db, record)
            path.unlink(missing_ok=True)
            verified += 1
        except StorageError as exc:
            failures.append(f"file {record.id} '{record.filename}': {exc}")
        checked += 1

    return {
        "files_checked": checked,
        "files_verified": verified,
        "failures": failures,
    }
