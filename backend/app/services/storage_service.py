"""Storage orchestration: encrypt, chunk, distribute, retrieve and self-heal."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import crypto
from ..chunking import sha256_hex
from ..config import get_settings
from ..models import (
    NODE_ONLINE,
    REPLICA_ACTIVE,
    REPLICA_CORRUPT,
    ChunkReplica,
    FileChunk,
    RepairEvent,
    StorageFile,
    StorageNode,
)
from ..utils import utcnow
from . import node_client, placement
from .node_client import NodeRequestError

logger = logging.getLogger(__name__)

settings = get_settings()


class StorageError(RuntimeError):
    """Raised when a storage operation cannot be completed."""


@dataclass
class UploadOutcome:
    """Result of storing a file across the node cluster."""

    file: StorageFile
    placement: dict[int, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ReconcileOutcome:
    """Result of a rebalancing pass that trims surplus replicas."""

    checked_chunks: int = 0
    surplus_chunks: int = 0
    pruned: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


@dataclass
class RepairOutcome:
    """Result of a self-healing pass."""

    checked_chunks: int = 0
    under_replicated: int = 0
    repaired: int = 0
    failed: int = 0
    offline_nodes: int = 0
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _is_healthy_replica(replica: ChunkReplica) -> bool:
    """A replica counts as healthy when its node is active and online."""
    return (
        replica.status == REPLICA_ACTIVE
        and replica.node is not None
        and replica.node.is_active
        and replica.node.status == NODE_ONLINE
    )


def healthy_replica_count(chunk: FileChunk) -> int:
    return sum(1 for replica in chunk.replicas if _is_healthy_replica(replica))


def assess_file(record: StorageFile) -> tuple[int, str]:
    """Return (min healthy replicas across chunks, health label)."""
    if not record.chunks:
        return 0, "critical"
    healthies = [healthy_replica_count(chunk) for chunk in record.chunks]
    minimum = min(healthies)
    if minimum <= 0:
        return minimum, "critical"
    if minimum < record.replication_factor:
        return minimum, "degraded"
    return minimum, "healthy"


def _record_node_success(node: StorageNode) -> None:
    node.write_requests += 1
    node.consecutive_failures = 0
    node.last_error = None


def _record_node_failure(node: StorageNode, error: str) -> None:
    node.write_requests += 1
    node.write_failures += 1
    node.consecutive_failures += 1
    node.last_error = error[:500]


async def _try_write(node: StorageNode, file_id: int, index: int, blob: bytes, checksum: str) -> None:
    """Write one chunk to one node, updating its reliability counters."""
    try:
        await node_client.put_chunk(node.url, file_id, index, blob, checksum)
    except NodeRequestError as exc:
        _record_node_failure(node, str(exc))
        raise
    _record_node_success(node)


async def _write_replica_set(
    db: Session,
    record: StorageFile,
    index: int,
    blob: bytes,
    checksum: str,
    replication_factor: int,
    warnings: list[str],
) -> list[StorageNode]:
    """Place ``replication_factor`` copies of a chunk, with fallbacks."""
    candidates = placement.select_nodes(
        db,
        count=max(replication_factor * 3, replication_factor),
        required_bytes=len(blob),
    )
    if not candidates:
        warnings.append(f"chunk {index}: no eligible storage nodes available")
        return []

    primary, fallback = candidates[:replication_factor], candidates[replication_factor:]
    chosen: list[StorageNode] = []

    # Phase 1: write the primary replica set concurrently.
    results = await asyncio.gather(
        *(_try_write(node, record.id, index, blob, checksum) for node in primary),
        return_exceptions=True,
    )
    for node, result in zip(primary, results, strict=True):
        if isinstance(result, BaseException):
            warnings.append(f"chunk {index}: node {node.name} rejected the write")
            logger.warning("Write to node %s failed: %s", node.name, result)
        else:
            chosen.append(node)

    # Phase 2: top up from the next best candidates, sequentially.
    for node in fallback:
        if len(chosen) >= replication_factor:
            break
        try:
            await _try_write(node, record.id, index, blob, checksum)
            chosen.append(node)
        except NodeRequestError as exc:
            warnings.append(f"chunk {index}: fallback node {node.name} failed")
            logger.warning("Fallback write to node %s failed: %s", node.name, exc)

    if len(chosen) < replication_factor:
        warnings.append(
            f"chunk {index}: stored {len(chosen)}/{replication_factor} replicas "
            "(cluster capacity or availability limited)"
        )
    return chosen


# ---------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------
async def store_file(
    db: Session,
    *,
    owner_id: int,
    upload: UploadFile,
    chunk_size: int | None = None,
    replication_factor: int | None = None,
) -> UploadOutcome:
    """Encrypt, chunk and distribute an uploaded file."""
    chunk_size = chunk_size or settings.chunk_size_bytes
    replication_factor = int(replication_factor or settings.replication_factor)
    if replication_factor < 1:
        raise StorageError("replication_factor must be at least 1")

    salt = crypto.generate_salt()
    file_key = crypto.derive_file_key(settings.master_key_bytes, salt)

    record = StorageFile(
        owner_id=owner_id,
        filename=upload.filename or "untitled",
        content_type=upload.content_type or "application/octet-stream",
        size_bytes=0,
        chunk_size=chunk_size,
        total_chunks=0,
        replication_factor=replication_factor,
        file_checksum="",
        key_salt=crypto.salt_to_b64(salt),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    warnings: list[str] = []
    placement_map: dict[int, list[str]] = {}
    overall = hashlib.sha256()
    total_bytes = 0
    index = 0

    try:
        while True:
            block = await upload.read(chunk_size)
            if not block:
                break

            overall.update(block)
            total_bytes += len(block)

            placed = await _store_single_chunk(
                db, record, index, block, file_key, replication_factor, warnings
            )
            placement_map[index] = [node.name for node in placed]
            index += 1

        if index == 0:
            # Empty upload still gets exactly one (empty) encrypted chunk.
            placed = await _store_single_chunk(
                db, record, 0, b"", file_key, replication_factor, warnings
            )
            placement_map[0] = [node.name for node in placed]
            index = 1

        record.size_bytes = total_bytes
        record.total_chunks = index
        record.file_checksum = overall.hexdigest()
        db.commit()
        db.refresh(record)

    except Exception:
        db.rollback()
        logger.exception("Upload failed for %s; rolling back rows", record.filename)
        # Clean up whatever made it onto the nodes before the failure.
        for node in _nodes_touched(db, record.id):
            try:
                await node_client.delete_file_chunks(node.url, record.id)
            except NodeRequestError as exc:
                logger.warning("Cleanup on node %s failed: %s", node.name, exc)
        db.rollback()
        await _purge_file_rows(db, record.id)
        raise

    return UploadOutcome(file=record, placement=placement_map, warnings=warnings)


async def _store_single_chunk(
    db: Session,
    record: StorageFile,
    index: int,
    plaintext: bytes,
    file_key: bytes,
    replication_factor: int,
    warnings: list[str],
) -> list[StorageNode]:
    """Encrypt one chunk, write its replicas and persist the metadata."""
    aad = crypto.build_aad(record.id, index)
    blob = crypto.encrypt_chunk(file_key, plaintext, aad)
    checksum = sha256_hex(blob)

    nodes = await _write_replica_set(
        db, record, index, blob, checksum, replication_factor, warnings
    )

    chunk = FileChunk(
        file_id=record.id,
        index=index,
        plaintext_size=len(plaintext),
        blob_size=len(blob),
        checksum=checksum,
    )
    db.add(chunk)
    db.flush()

    for node in nodes:
        db.add(
            ChunkReplica(
                chunk_id=chunk.id,
                node_id=node.id,
                status=REPLICA_ACTIVE,
                checksum=checksum,
            )
        )

    record.chunks.append(chunk)
    db.commit()
    return nodes


# ---------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------
async def retrieve_file(db: Session, record: StorageFile) -> Path:
    """Fetch, verify, decrypt and reassemble a file into a temp file."""
    statement = (
        select(FileChunk)
        .where(FileChunk.file_id == record.id)
        .options(selectinload(FileChunk.replicas).selectinload(ChunkReplica.node))
        .order_by(FileChunk.index)
    )
    chunks = list(db.scalars(statement).all())
    if not chunks:
        raise StorageError("File has no stored chunks")

    file_key = crypto.derive_file_key(
        settings.master_key_bytes, crypto.salt_from_b64(record.key_salt)
    )

    suffix = Path(record.filename).suffix[:16]
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    overall = hashlib.sha256()
    try:
        for chunk in chunks:
            blob = await _fetch_verified_blob(db, record, chunk)
            try:
                plaintext = crypto.decrypt_chunk(
                    file_key, blob, crypto.build_aad(record.id, chunk.index)
                )
            except Exception as exc:  # noqa: BLE001 - InvalidTag and friends
                raise StorageError(
                    f"Chunk {chunk.index} failed to decrypt (key mismatch or tampering)"
                ) from exc
            overall.update(plaintext)
            handle.write(plaintext)
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise

    handle.close()

    digest = overall.hexdigest()
    if record.file_checksum and digest != record.file_checksum:
        Path(handle.name).unlink(missing_ok=True)
        raise StorageError("Reassembled file failed its integrity checksum")

    return Path(handle.name)


async def _fetch_verified_blob(db: Session, record: StorageFile, chunk: FileChunk) -> bytes:
    """Fetch a chunk from the healthiest replica that passes checksum check."""
    replicas = sorted(
        chunk.replicas,
        key=lambda replica: (
            not _is_healthy_replica(replica),
            replica.node.latency_ms if replica.node else 9999,
        ),
    )
    if not replicas:
        raise StorageError(f"Chunk {chunk.index} has no replicas")

    last_error: str | None = None
    for replica in replicas:
        node = replica.node
        if node is None or replica.status != REPLICA_ACTIVE:
            continue
        try:
            blob = await node_client.get_chunk(node.url, record.id, chunk.index)
        except NodeRequestError as exc:
            node.read_requests += 1
            node.read_failures += 1
            node.consecutive_failures += 1
            node.last_error = str(exc)[:500]
            last_error = str(exc)
            db.commit()
            continue

        node.read_requests += 1
        node.consecutive_failures = 0

        if sha256_hex(blob) != chunk.checksum:
            replica.status = REPLICA_CORRUPT
            replica.last_error = "checksum mismatch on read"
            node.last_error = f"corrupt chunk {chunk.index}"
            last_error = f"checksum mismatch on node {node.name}"
            db.add(
                RepairEvent(
                    kind="corruption_detected",
                    file_id=record.id,
                    chunk_index=chunk.index,
                    source_node_id=node.id,
                    detail=last_error,
                )
            )
            db.commit()
            continue

        replica.verified_at = utcnow()
        db.commit()
        return blob

    raise StorageError(
        f"Could not retrieve chunk {chunk.index} from any replica"
        + (f": {last_error}" if last_error else "")
    )


# ---------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------
def _nodes_touched(db: Session, file_id: int) -> list[StorageNode]:
    """Nodes that may hold a chunk of an (about-to-be-deleted) file."""
    statement = (
        select(StorageNode)
        .join(ChunkReplica, ChunkReplica.node_id == StorageNode.id)
        .join(FileChunk, FileChunk.id == ChunkReplica.chunk_id)
        .where(FileChunk.file_id == file_id)
        .distinct()
    )
    return list(db.scalars(statement).all())


async def _purge_file_rows(db: Session, file_id: int) -> None:
    """Remove chunk/replica rows for a file after a failed upload."""
    db.execute(delete(ChunkReplica).where(ChunkReplica.chunk_id.in_(
        select(FileChunk.id).where(FileChunk.file_id == file_id)
    )))
    db.execute(delete(FileChunk).where(FileChunk.file_id == file_id))
    db.execute(delete(StorageFile).where(StorageFile.id == file_id))
    db.commit()


async def delete_file(db: Session, record: StorageFile) -> list[str]:
    """Best-effort removal of every chunk copy, then delete the metadata."""
    warnings: list[str] = []
    by_node: dict[int, StorageNode] = {}

    for chunk in record.chunks:
        for replica in chunk.replicas:
            if replica.node is not None:
                by_node[replica.node_id] = replica.node

    for node in by_node.values():
        try:
            await node_client.delete_file_chunks(node.url, record.id)
        except NodeRequestError as exc:
            warnings.append(f"node {node.name}: {exc}")
            logger.warning("Failed to purge file %s from %s: %s", record.id, node.name, exc)

    db.delete(record)
    db.commit()
    return warnings


# ---------------------------------------------------------------------
# Rebalancing: trim surplus and unusable replicas
# ---------------------------------------------------------------------
def needs_reconcile(db: Session) -> bool:
    """Cheap gate so the monitor only runs the full sweep when it may help."""
    max_target = db.scalar(select(func.max(StorageFile.replication_factor))) or 0

    unusable = (
        select(ChunkReplica.id)
        .join(StorageNode, StorageNode.id == ChunkReplica.node_id)
        .where(
            or_(
                ChunkReplica.status != REPLICA_ACTIVE,
                StorageNode.is_active.is_(False),
            )
        )
        .limit(1)
    )
    if db.scalar(select(unusable.exists())):
        return True

    if max_target <= 0:
        return False

    surplus = (
        select(FileChunk.id)
        .join(ChunkReplica, ChunkReplica.chunk_id == FileChunk.id)
        .where(ChunkReplica.status == REPLICA_ACTIVE)
        .group_by(FileChunk.id)
        .having(func.count(ChunkReplica.id) > max_target)
        .limit(1)
    )
    return bool(db.scalar(select(surplus.exists())))


async def reconcile_replicas(db: Session, *, max_prune: int = 500) -> ReconcileOutcome:
    """Bring every chunk back to its target replica count.

    Self-healing only ever *adds* replicas, so once a failed node returns the
    cluster holds more copies than the target. This pass removes the surplus,
    dropping the lowest-quality copies first (offline nodes, then the busiest
    and slowest nodes) and always keeping exactly ``replication_factor``
    usable copies. Corrupt/missing replicas and copies held by deactivated
    nodes are removed as well.
    """
    outcome = ReconcileOutcome()

    statement = select(FileChunk).options(
        selectinload(FileChunk.replicas).selectinload(ChunkReplica.node),
        selectinload(FileChunk.file),
    )
    for chunk in db.scalars(statement).all():
        outcome.checked_chunks += 1
        record = chunk.file
        if record is None:
            continue

        usable = [
            replica
            for replica in chunk.replicas
            if replica.status == REPLICA_ACTIVE
            and replica.node is not None
            and replica.node.is_active
        ]
        # Anything corrupt, missing, or sitting on a deactivated node.
        dead = [replica for replica in chunk.replicas if replica not in usable]

        keep: list[ChunkReplica] = []
        if len(usable) > record.replication_factor:
            # Best copies first: online nodes, then least loaded, then fastest.
            ordered = sorted(
                usable,
                key=lambda replica: (
                    0 if replica.node.status == NODE_ONLINE else 1,
                    replica.node.chunk_count,
                    replica.node.latency_ms,
                ),
            )
            keep = ordered[: record.replication_factor]
            dead.extend(ordered[record.replication_factor :])

        if not dead:
            continue

        outcome.surplus_chunks += 1
        for replica in dead:
            if outcome.pruned >= max_prune:
                continue
            node = replica.node
            if node is not None and node.status == NODE_ONLINE and node.is_active:
                try:
                    await node_client.delete_chunk(node.url, record.id, chunk.index)
                except NodeRequestError as exc:
                    outcome.failed += 1
                    logger.warning(
                        "Could not delete surplus chunk %s from %s: %s",
                        chunk.index,
                        node.name,
                        exc,
                    )
                    continue
            node_name = node.name if node else f"node-{replica.node_id}"
            db.delete(replica)
            outcome.pruned += 1
            outcome.details.append(
                f"chunk {chunk.index} of file {record.id}: dropped replica on {node_name}"
            )
        db.add(
            RepairEvent(
                kind="replicas_reconciled",
                file_id=record.id,
                chunk_index=chunk.index,
                detail=(
                    f"trimmed chunk {chunk.index} of '{record.filename}' back to "
                    f"{record.replication_factor} replica(s)"
                ),
            )
        )
        db.commit()

    return outcome


# ---------------------------------------------------------------------
# Self-healing replication
# ---------------------------------------------------------------------
async def repair_under_replicated(db: Session, *, max_repairs: int = 100) -> RepairOutcome:
    """Re-replicate chunks whose healthy copy count dropped below target."""
    outcome = RepairOutcome()

    outcome.offline_nodes = len(
        list(
            db.scalars(
                select(StorageNode).where(
                    StorageNode.is_active.is_(True),
                    StorageNode.status != NODE_ONLINE,
                )
            ).all()
        )
    )

    statement = select(FileChunk).options(
        selectinload(FileChunk.replicas).selectinload(ChunkReplica.node),
        selectinload(FileChunk.file),
    )
    chunks = list(db.scalars(statement).all())

    for chunk in chunks:
        outcome.checked_chunks += 1
        record = chunk.file
        if record is None:
            continue

        healthy = [replica for replica in chunk.replicas if _is_healthy_replica(replica)]
        needed = record.replication_factor - len(healthy)
        if needed <= 0:
            continue

        outcome.under_replicated += 1
        if outcome.repaired >= max_repairs:
            continue
        if not healthy:
            outcome.failed += 1
            outcome.details.append(
                f"file {record.id} chunk {chunk.index}: no healthy replica to copy from"
            )
            continue

        blob: bytes | None = None
        source: StorageNode | None = None
        for replica in healthy:
            node = replica.node
            try:
                fetched = await node_client.get_chunk(node.url, record.id, chunk.index)
            except NodeRequestError:
                continue
            if sha256_hex(fetched) != chunk.checksum:
                replica.status = REPLICA_CORRUPT
                replica.last_error = "checksum mismatch during repair"
                db.commit()
                continue
            blob, source = fetched, node
            break

        if blob is None or source is None:
            outcome.failed += 1
            outcome.details.append(
                f"file {record.id} chunk {chunk.index}: no replica passed verification"
            )
            continue

        existing_ids = {replica.node_id for replica in chunk.replicas}
        targets = placement.select_nodes(
            db,
            count=needed,
            required_bytes=len(blob),
            exclude_node_ids=existing_ids,
        )
        for target in targets:
            try:
                await _try_write(target, record.id, chunk.index, blob, chunk.checksum)
            except NodeRequestError as exc:
                logger.warning("Repair write to %s failed: %s", target.name, exc)
                continue

            db.add(
                ChunkReplica(
                    chunk_id=chunk.id,
                    node_id=target.id,
                    status=REPLICA_ACTIVE,
                    checksum=chunk.checksum,
                    verified_at=utcnow(),
                )
            )
            db.add(
                RepairEvent(
                    kind="replica_created",
                    file_id=record.id,
                    chunk_index=chunk.index,
                    source_node_id=source.id,
                    target_node_id=target.id,
                    detail=(
                        f"restored chunk {chunk.index} of '{record.filename}' "
                        f"from {source.name} to {target.name}"
                    ),
                )
            )
            outcome.repaired += 1
            outcome.details.append(
                f"chunk {chunk.index} of file {record.id}: {source.name} -> {target.name}"
            )
        db.commit()

    return outcome
