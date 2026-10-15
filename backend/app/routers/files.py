"""File endpoints: upload, list, inspect, download and delete."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..dependencies import CurrentUser, DbSession
from ..models import ChunkReplica, FileChunk, StorageFile, StorageNode
from ..schemas import (
    ChunkOut,
    FileDetailOut,
    FileOut,
    MessageOut,
    ReplicaOut,
    UploadResultOut,
)
from ..services import storage_service
from ..services.storage_service import StorageError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])
settings = get_settings()

MIN_CHUNK_SIZE = 16 * 1024
MAX_CHUNK_SIZE = 4 * 1024 * 1024

_FILE_LOAD_OPTIONS = (
    selectinload(StorageFile.chunks)
    .selectinload(FileChunk.replicas)
    .selectinload(ChunkReplica.node),
)


def _load_owned_file(db: DbSession, file_id: int, owner_id: int) -> StorageFile:
    """Fetch a file with its chunk/replica graph, enforcing ownership."""
    statement = (
        select(StorageFile)
        .where(StorageFile.id == file_id, StorageFile.owner_id == owner_id)
        .options(*_FILE_LOAD_OPTIONS)
    )
    record = db.scalar(statement)
    if record is None:
        raise HTTPException(status_code=404, detail="File not found")
    return record


def _to_file_out(record: StorageFile) -> FileOut:
    minimum, health = storage_service.assess_file(record)
    return FileOut(
        id=record.id,
        filename=record.filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        chunk_size=record.chunk_size,
        total_chunks=record.total_chunks,
        replication_factor=record.replication_factor,
        file_checksum=record.file_checksum,
        created_at=record.created_at,
        healthy_replicas=minimum,
        health=health,
    )


@router.post("/upload", response_model=UploadResultOut, status_code=status.HTTP_201_CREATED)
async def upload_file(
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File(description="The file to store")],
    chunk_size: Annotated[int | None, Form()] = None,
    replication_factor: Annotated[int | None, Form()] = None,
) -> UploadResultOut:
    """Encrypt, chunk and distribute a file across the storage nodes."""
    if chunk_size is not None and not (MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE):
        raise HTTPException(
            status_code=422,
            detail=f"chunk_size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes",
        )

    target_replicas = replication_factor or settings.replication_factor
    if not (1 <= target_replicas <= 10):
        raise HTTPException(status_code=422, detail="replication_factor must be between 1 and 10")

    available_nodes = db.scalar(
        select(func.count(StorageNode.id)).where(StorageNode.is_active.is_(True))
    ) or 0
    if available_nodes == 0:
        raise HTTPException(
            status_code=503,
            detail="No storage nodes are registered. Start the node containers first.",
        )
    if target_replicas > available_nodes:
        raise HTTPException(
            status_code=422,
            detail=(
                f"replication_factor {target_replicas} exceeds the "
                f"{available_nodes} registered storage node(s)"
            ),
        )

    try:
        outcome = await storage_service.store_file(
            db,
            owner_id=user.id,
            upload=file,
            chunk_size=chunk_size,
            replication_factor=target_replicas,
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await file.close()

    fresh = _load_owned_file(db, outcome.file.id, user.id)
    return UploadResultOut(
        file=_to_file_out(fresh),
        placement=outcome.placement,
        warnings=outcome.warnings,
    )


@router.get("", response_model=list[FileOut])
def list_files(user: CurrentUser, db: DbSession) -> list[FileOut]:
    """List the authenticated user's files with replication health."""
    statement = (
        select(StorageFile)
        .where(StorageFile.owner_id == user.id)
        .order_by(StorageFile.created_at.desc())
        .options(*_FILE_LOAD_OPTIONS)
    )
    return [_to_file_out(record) for record in db.scalars(statement).all()]


@router.get("/stats")
def file_stats(user: CurrentUser, db: DbSession) -> dict:
    """Aggregate storage statistics for the current user."""
    total_files = db.scalar(
        select(func.count(StorageFile.id)).where(StorageFile.owner_id == user.id)
    ) or 0
    total_bytes = db.scalar(
        select(func.coalesce(func.sum(StorageFile.size_bytes), 0)).where(
            StorageFile.owner_id == user.id
        )
    ) or 0
    total_chunks = db.scalar(
        select(func.count(FileChunk.id))
        .join(StorageFile, StorageFile.id == FileChunk.file_id)
        .where(StorageFile.owner_id == user.id)
    ) or 0
    total_replicas = db.scalar(
        select(func.count(ChunkReplica.id))
        .join(FileChunk, FileChunk.id == ChunkReplica.chunk_id)
        .join(StorageFile, StorageFile.id == FileChunk.file_id)
        .where(StorageFile.owner_id == user.id)
    ) or 0
    return {
        "total_files": total_files,
        "total_bytes": int(total_bytes),
        "total_chunks": total_chunks,
        "total_replicas": total_replicas,
    }


@router.get("/{file_id}", response_model=FileDetailOut)
def get_file(file_id: int, user: CurrentUser, db: DbSession) -> FileDetailOut:
    """Full metadata for one file, including chunk placement."""
    record = _load_owned_file(db, file_id, user.id)
    base = _to_file_out(record)
    chunks = [
        ChunkOut(
            index=chunk.index,
            plaintext_size=chunk.plaintext_size,
            blob_size=chunk.blob_size,
            checksum=chunk.checksum,
            healthy_replicas=storage_service.healthy_replica_count(chunk),
        )
        for chunk in sorted(record.chunks, key=lambda item: item.index)
    ]
    return FileDetailOut(**base.model_dump(), chunks=chunks)


@router.get("/{file_id}/replicas", response_model=list[ReplicaOut])
def get_file_replicas(file_id: int, user: CurrentUser, db: DbSession) -> list[ReplicaOut]:
    """Show exactly which node holds which chunk."""
    record = _load_owned_file(db, file_id, user.id)
    out: list[ReplicaOut] = []
    for chunk in sorted(record.chunks, key=lambda item: item.index):
        for replica in chunk.replicas:
            node = replica.node
            out.append(
                ReplicaOut(
                    node_id=replica.node_id,
                    node_name=node.name if node else f"node-{replica.node_id}",
                    node_status=node.status if node else "unknown",
                    status=replica.status,
                    checksum=replica.checksum,
                    stored_at=replica.stored_at,
                )
            )
    return out


@router.get("/{file_id}/download")
async def download_file(
    file_id: int,
    user: CurrentUser,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> FileResponse:
    """Reassemble and stream back the original file."""
    record = _load_owned_file(db, file_id, user.id)
    try:
        path = await storage_service.retrieve_file(db, record)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    background_tasks.add_task(_cleanup, path)
    return FileResponse(
        path,
        filename=record.filename,
        media_type=record.content_type or "application/octet-stream",
    )


def _cleanup(path) -> None:
    """Delete the temporary reassembly file after the response is sent."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove temporary file %s", path)


@router.delete("/{file_id}", response_model=MessageOut)
async def delete_file(file_id: int, user: CurrentUser, db: DbSession) -> MessageOut:
    """Delete a file and every copy of its chunks."""
    record = _load_owned_file(db, file_id, user.id)
    warnings = await storage_service.delete_file(db, record)
    message = f"Deleted '{record.filename}' from the cluster"
    if warnings:
        message += f" ({len(warnings)} node(s) unreachable, orphaned chunks may remain)"
    return MessageOut(message=message)
