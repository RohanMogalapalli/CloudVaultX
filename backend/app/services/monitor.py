"""Background monitoring: node health polling, metrics history, self-healing."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..models import (
    NODE_OFFLINE,
    NODE_ONLINE,
    NodeMetric,
    StorageNode,
)
from ..utils import elapsed_seconds, utcnow
from . import node_client, placement, storage_service
from .node_client import NodeRequestError

logger = logging.getLogger(__name__)

settings = get_settings()

# Monotonic timestamp of the last rebalancing sweep, so prune passes are
# throttled independently of the polling interval.
_last_reconcile_at = 0.0


async def poll_node(db: Session, node: StorageNode) -> NodeMetric:
    """Probe one node, refresh its metrics and append a history point."""
    started = time.perf_counter()
    online = True
    error: str | None = None

    try:
        payload = await node_client.metrics(node.url)
    except NodeRequestError as exc:
        online = False
        error = str(exc)
        payload = {}
    latency_ms = (time.perf_counter() - started) * 1000.0

    if online:
        node.cpu_usage = float(payload.get("cpu_usage", 0.0))
        node.ram_usage = float(payload.get("ram_usage", 0.0))
        node.disk_usage = float(payload.get("disk_usage", 0.0))
        node.used_bytes = int(payload.get("used_bytes", 0))
        node.free_bytes = int(payload.get("free_bytes", 0))
        capacity = int(payload.get("capacity_bytes", 0))
        if capacity > 0:
            node.capacity_bytes = capacity
        node.uptime_seconds = float(payload.get("uptime_seconds", node.uptime_seconds))
        node.chunk_count = int(payload.get("chunk_count", node.chunk_count))
        node.latency_ms = latency_ms
        node.last_seen = utcnow()
        node.last_error = None

        if node.status == NODE_OFFLINE and node.last_offline_at is not None:
            node.total_downtime_seconds += elapsed_seconds(node.last_offline_at)
            node.last_offline_at = None
        node.status = NODE_ONLINE
        node.consecutive_failures = 0
    else:
        node.consecutive_failures += 1
        node.last_error = (error or "unreachable")[:500]
        if node.consecutive_failures >= settings.node_offline_after_failures:
            if node.status != NODE_OFFLINE:
                node.last_offline_at = utcnow()
                logger.warning("Node %s marked offline after %d failures",
                               node.name, node.consecutive_failures)
            node.status = NODE_OFFLINE

    score = placement.predict_scores_for_all(db).get(node.id)

    metric = NodeMetric(
        node_id=node.id,
        cpu_usage=node.cpu_usage,
        ram_usage=node.ram_usage,
        disk_usage=node.disk_usage,
        free_bytes=node.free_bytes,
        latency_ms=node.latency_ms if online else 0.0,
        uptime_ratio=node.uptime_ratio,
        reliability=node.reliability,
        is_online=online,
        predicted_score=score,
    )
    db.add(metric)
    db.commit()
    return metric


def prune_metric_history(db: Session, node_id: int, keep: int | None = None) -> int:
    """Keep only the newest ``keep`` metric rows for a node."""
    keep = keep or settings.metric_history_limit
    total = db.scalar(
        select(func.count(NodeMetric.id)).where(NodeMetric.node_id == node_id)
    ) or 0
    if total <= keep:
        return 0
    stale_ids = list(
        db.scalars(
            select(NodeMetric.id)
            .where(NodeMetric.node_id == node_id)
            .order_by(NodeMetric.recorded_at.desc(), NodeMetric.id.desc())
            .offset(keep)
        ).all()
    )
    if not stale_ids:
        return 0
    db.execute(delete(NodeMetric).where(NodeMetric.id.in_(stale_ids)))
    db.commit()
    return len(stale_ids)


async def poll_all_nodes() -> dict:
    """Poll every active node once, then optionally run self-healing."""
    db = SessionLocal()
    summary = {"polled": 0, "online": 0, "offline": 0, "repaired": 0, "pruned": 0}
    try:
        nodes = list(db.scalars(select(StorageNode).where(StorageNode.is_active.is_(True))).all())
        for node in nodes:
            metric = await poll_node(db, node)
            summary["polled"] += 1
            summary["online" if metric.is_online else "offline"] += 1
            prune_metric_history(db, node.id)

        if settings.auto_repair_enabled:
            offline = summary["offline"]
            if offline:
                outcome = await storage_service.repair_under_replicated(db)
                summary["repaired"] = outcome.repaired
                if outcome.repaired:
                    logger.info(
                        "Self-healing restored %d replica(s) after %d node(s) went offline",
                        outcome.repaired,
                        offline,
                    )

        # Once every node is healthy again, trim any surplus replicas left
        # behind by self-healing (throttled and gated by a cheap check first).
        if settings.reconcile_enabled and summary["offline"] == 0:
            global _last_reconcile_at
            now = time.monotonic()
            if now - _last_reconcile_at >= settings.reconcile_interval_seconds:
                if storage_service.needs_reconcile(db):
                    reconcile = await storage_service.reconcile_replicas(db)
                    _last_reconcile_at = now
                    summary["pruned"] = reconcile.pruned
                    if reconcile.pruned:
                        logger.info(
                            "Rebalanced the cluster: pruned %d surplus replica(s)",
                            reconcile.pruned,
                        )
    except Exception:  # noqa: BLE001 - never let the loop die
        logger.exception("Monitoring pass failed")
    finally:
        db.close()
    return summary


async def monitor_loop(stop_event: asyncio.Event) -> None:
    """Continuously poll the cluster until shutdown is requested."""
    logger.info(
        "Node monitor started (interval=%ss, auto_repair=%s)",
        settings.node_poll_interval_seconds,
        settings.auto_repair_enabled,
    )
    while not stop_event.is_set():
        try:
            summary = await poll_all_nodes()
            logger.debug("Monitoring pass: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error in monitor loop")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.node_poll_interval_seconds)
        except asyncio.TimeoutError:
            continue
    logger.info("Node monitor stopped")
