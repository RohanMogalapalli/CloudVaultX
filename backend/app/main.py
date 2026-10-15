"""CloudVaultX coordinator API.

Responsibilities:
* authentication and per-user file ownership
* AES-256-GCM encryption and fixed-size chunking
* AI-based (Random Forest) storage node selection
* replica placement, retrieval, integrity verification and self-healing
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import assert_production_ready, get_settings
from .database import SessionLocal, init_db
from .ml import node_selector
from .routers import auth, cluster, files, nodes
from .services import accounts, monitor, node_client, node_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cloudvaultx.coordinator")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown: database, ML model, node registry and monitor."""
    logger.info("Starting %s v%s (%s)", settings.app_name, __version__, settings.environment)

    # Fail fast rather than silently protecting production data with a
    # key derived from JWT_SECRET.
    assert_production_ready(settings)

    init_db()

    if settings.using_ephemeral_master_key:
        logger.warning(
            "MASTER_KEY is not set - deriving one from JWT_SECRET. "
            "Set MASTER_KEY (base64, 32 bytes) before storing real data."
        )

    # Loading/training the Random Forest is CPU bound; keep startup async-friendly.
    trained = await asyncio.to_thread(
        node_selector.configure,
        settings.model_path,
        settings.ml_weight,
        settings.auto_train_model,
    )
    if trained:
        metadata = node_selector.selector.metadata
        logger.info(
            "Node-selection model ready (accuracy=%s, roc_auc=%s, samples=%d)",
            f"{metadata.accuracy:.4f}" if metadata.accuracy is not None else "n/a",
            f"{metadata.roc_auc:.4f}" if metadata.roc_auc is not None else "n/a",
            metadata.n_samples,
        )
    else:
        logger.warning("No node-selection model loaded; using the heuristic scorer")

    db = SessionLocal()
    try:
        registered = node_registry.sync_nodes_from_settings(db)
        logger.info("Storage nodes registered: %s", [node.name for node in registered] or "none")
        accounts.promote_configured_admins(db, settings.admin_email_list)
        accounts.ensure_admin_exists(db)
    finally:
        db.close()

    stop_event = asyncio.Event()
    monitor_task: asyncio.Task | None = None
    if settings.monitor_enabled:
        # One synchronous pass so capacity/latency metrics exist immediately
        # instead of after the first poll interval.
        first_pass = await monitor.poll_all_nodes()
        logger.info("Initial node poll: %s", first_pass)
        monitor_task = asyncio.create_task(monitor.monitor_loop(stop_event))

    try:
        yield
    finally:
        stop_event.set()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await node_client.close_client()
        logger.info("Coordinator shut down cleanly")


app = FastAPI(
    title=f"{settings.app_name} Coordinator",
    version=__version__,
    description=(
        "AI-based decentralized cloud storage: AES-256 encryption, chunking, "
        "Random-Forest node selection and replicated, self-healing storage."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_prefix = settings.api_prefix
app.include_router(auth.router, prefix=_prefix)
app.include_router(files.router, prefix=_prefix)
app.include_router(nodes.router, prefix=_prefix)
app.include_router(cluster.router, prefix=_prefix)


@app.get(f"{_prefix}/health", tags=["ops"])
def health() -> dict:
    """Liveness probe plus a little deployment context."""
    metadata = node_selector.selector.metadata
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "model_trained": node_selector.selector.is_trained,
        "model_accuracy": metadata.accuracy,
        "storage_nodes_configured": len(settings.node_url_list),
    }
