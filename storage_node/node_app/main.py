"""Storage node HTTP service.

Each node owns a directory tree of encrypted chunks and exposes a tiny
API for the coordinator:

* ``GET  /health``                        liveness probe
* ``GET  /metrics``                       CPU, RAM, disk, uptime, chunk stats
* ``PUT  /chunks/{file_id}/{index}``      store an encrypted chunk
* ``GET  /chunks/{file_id}/{index}``      fetch an encrypted chunk
* ``HEAD /chunks/{file_id}/{index}``      existence check
* ``DELETE /chunks/{file_id}/{index}``    remove a chunk
* ``DELETE /files/{file_id}``             remove every chunk of a file
* ``POST /admin/offline`` / ``online``    simulate node failure for demos

All chunk operations require the shared ``X-Node-Token`` header.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Annotated, Any

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from .config import get_node_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloudvaultx.node")

settings = get_node_settings()
STARTED_AT = time.time()

app = FastAPI(
    title=f"CloudVaultX Storage Node ({settings.node_name})",
    version="1.0.0",
    description="Stores encrypted chunks for the CloudVaultX coordinator.",
)

# Simulated outage flag, toggled through /admin/*. Lets a demo show the
# coordinator detecting a dead node and re-replicating its chunks.
_state: dict[str, Any] = {"offline": False}

# Prime psutil's CPU sampler so the first real reading is meaningful.
psutil.cpu_percent(interval=None)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _chunk_path(file_id: int, index: int) -> Path:
    return Path(settings.chunk_dir) / str(file_id) / f"{index}.chunk"


def require_token(x_node_token: Annotated[str | None, Header()] = None) -> None:
    """Reject requests that do not carry the shared node token."""
    if settings.shared_token and x_node_token != settings.shared_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Node-Token header",
        )


def _ensure_online() -> None:
    if _state["offline"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Node is simulating an outage",
        )


def _dir_stats() -> tuple[int, int]:
    """Return (total chunk bytes, chunk file count) for this node."""
    root = Path(settings.chunk_dir)
    if not root.exists():
        return 0, 0
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".chunk"):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
            count += 1
    return total, count


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------
# Make sure the data directory exists as soon as the module is imported.
Path(settings.chunk_dir).mkdir(parents=True, exist_ok=True)
logger.info(
    "Storage node '%s' ready | data_dir=%s capacity=%.2f GiB",
    settings.node_name,
    settings.chunk_dir,
    settings.capacity_bytes / 1024**3,
)


# ---------------------------------------------------------------------
# Health & metrics
# ---------------------------------------------------------------------
@app.get("/health", tags=["ops"])
def health() -> dict[str, Any]:
    if _state["offline"]:
        raise HTTPException(status_code=503, detail="Simulated outage")
    return {
        "status": "ok",
        "node_name": settings.node_name,
        "uptime_seconds": round(time.time() - STARTED_AT, 2),
    }


@app.get("/metrics", tags=["ops"])
def metrics() -> dict[str, Any]:
    """Live resource metrics plus chunk usage for this node."""
    if _state["offline"]:
        raise HTTPException(status_code=503, detail="Simulated outage")

    root = Path(settings.chunk_dir)
    root.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(str(root))
    used_bytes, chunk_count = _dir_stats()

    capacity = settings.capacity_bytes
    free_bytes = max(0, min(disk.free, capacity - used_bytes))

    return {
        "node_name": settings.node_name,
        "status": "ok",
        "cpu_usage": round(psutil.cpu_percent(interval=None) / 100.0, 4),
        "ram_usage": round(psutil.virtual_memory().percent / 100.0, 4),
        "disk_usage": round(used_bytes / capacity, 6) if capacity else 0.0,
        "capacity_bytes": capacity,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "chunk_count": chunk_count,
        "uptime_seconds": round(time.time() - STARTED_AT, 2),
        "load_average": round(os.getloadavg()[0], 3) if hasattr(os, "getloadavg") else None,
        "simulated_offline": _state["offline"],
    }


# ---------------------------------------------------------------------
# Chunk operations
# ---------------------------------------------------------------------
@app.put(
    "/chunks/{file_id}/{index}",
    status_code=status.HTTP_201_CREATED,
    tags=["chunks"],
    dependencies=[Depends(require_token)],
)
async def put_chunk(
    file_id: int,
    index: int,
    request: Request,
    x_chunk_checksum: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Persist one encrypted chunk, verifying its checksum first."""
    _ensure_online()

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty chunk body")

    checksum = hashlib.sha256(body).hexdigest()
    if x_chunk_checksum and x_chunk_checksum != checksum:
        raise HTTPException(
            status_code=400,
            detail="Chunk checksum does not match X-Chunk-Checksum header",
        )

    if len(body) > settings.capacity_bytes:
        raise HTTPException(status_code=413, detail="Chunk larger than node capacity")

    path = _chunk_path(file_id, index)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write then atomically replace so a crash never leaves a partial chunk.
    tmp_path = path.with_suffix(".chunk.tmp")
    with open(tmp_path, "wb") as handle:
        handle.write(body)
    os.replace(tmp_path, path)

    logger.info("Stored chunk %s/%s (%d bytes)", file_id, index, len(body))
    return {
        "status": "stored",
        "node_name": settings.node_name,
        "file_id": file_id,
        "index": index,
        "size_bytes": len(body),
        "checksum": checksum,
    }


@app.get(
    "/chunks/{file_id}/{index}",
    tags=["chunks"],
    dependencies=[Depends(require_token)],
)
def get_chunk(file_id: int, index: int) -> Response:
    """Return the raw encrypted chunk bytes."""
    _ensure_online()
    path = _chunk_path(file_id, index)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Chunk not found")
    return Response(content=path.read_bytes(), media_type="application/octet-stream")


@app.head(
    "/chunks/{file_id}/{index}",
    tags=["chunks"],
    dependencies=[Depends(require_token)],
)
def head_chunk(file_id: int, index: int) -> Response:
    path = _chunk_path(file_id, index)
    if _state["offline"] or not path.is_file():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(
        status_code=status.HTTP_200_OK,
        headers={"X-Chunk-Size": str(path.stat().st_size)},
    )


@app.delete(
    "/chunks/{file_id}/{index}",
    tags=["chunks"],
    dependencies=[Depends(require_token)],
)
def delete_chunk(file_id: int, index: int) -> dict[str, Any]:
    _ensure_online()
    path = _chunk_path(file_id, index)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    if not existed:
        return JSONResponse(status_code=404, content={"detail": "Chunk not found"})
    return {"status": "deleted", "file_id": file_id, "index": index}


@app.delete("/files/{file_id}", tags=["chunks"], dependencies=[Depends(require_token)])
def delete_file_chunks(file_id: int) -> dict[str, Any]:
    """Delete every chunk belonging to a file."""
    _ensure_online()
    folder = Path(settings.chunk_dir) / str(file_id)
    removed = 0
    if folder.is_dir():
        removed = len(list(folder.glob("*.chunk")))
        shutil.rmtree(folder, ignore_errors=True)
    return {"status": "purged", "file_id": file_id, "removed": removed}


# ---------------------------------------------------------------------
# Failure simulation (demos only)
# ---------------------------------------------------------------------
@app.post("/admin/offline", tags=["ops"])
def go_offline(_: None = Depends(require_token)) -> dict[str, Any]:
    """Make this node behave like a failed node."""
    _state["offline"] = True
    logger.warning("Node '%s' is now simulating an outage", settings.node_name)
    return {"status": "offline", "node_name": settings.node_name}


@app.post("/admin/online", tags=["ops"])
def go_online(_: None = Depends(require_token)) -> dict[str, Any]:
    """Restore this node to service."""
    _state["offline"] = False
    logger.info("Node '%s' is back online", settings.node_name)
    return {"status": "online", "node_name": settings.node_name}
