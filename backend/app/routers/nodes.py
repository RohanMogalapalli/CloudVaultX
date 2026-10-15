"""Storage node endpoints: inventory, metrics history and failure drills."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from ..dependencies import AdminUser, CurrentUser, DbSession
from ..models import NODE_OFFLINE, NODE_ONLINE, NodeMetric, StorageNode
from ..schemas import MessageOut, MetricPointOut, NodeListOut, NodeOut
from ..services import node_client, node_registry
from ..services.node_client import NodeRequestError
from ..utils import elapsed_seconds, utcnow

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("", response_model=NodeListOut)
def list_nodes(db: DbSession, user: CurrentUser) -> NodeListOut:
    """Every known storage node, with its latest metrics."""
    nodes = node_registry.list_nodes(db)
    online = [node for node in nodes if node.status == NODE_ONLINE and node.is_active]
    return NodeListOut(
        nodes=[NodeOut.model_validate(node) for node in nodes],
        online=len(online),
        total=len(nodes),
        total_capacity_bytes=sum(node.capacity_bytes for node in nodes),
        total_used_bytes=sum(node.used_bytes for node in nodes),
    )


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
def register_node(
    db: DbSession,
    user: AdminUser,
    name: Annotated[str, Query(min_length=1, max_length=120)],
    url: Annotated[str, Query(min_length=4, max_length=255)],
    capacity_bytes: Annotated[int | None, Query(ge=0)] = None,
) -> NodeOut:
    """Manually register a storage node (normally done via NODE_URLS)."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must start with http:// or https://")
    node = node_registry.register_node(db, name=name, url=url, capacity_bytes=capacity_bytes)
    return NodeOut.model_validate(node)


@router.post("/sync", response_model=NodeListOut)
def sync_nodes(db: DbSession, user: AdminUser) -> NodeListOut:
    """Re-apply the NODE_URLS configuration to the registry."""
    node_registry.sync_nodes_from_settings(db)
    return list_nodes(db, user)


@router.get("/{node_id}", response_model=NodeOut)
def get_node(node_id: int, db: DbSession, user: CurrentUser) -> NodeOut:
    node = db.get(StorageNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return NodeOut.model_validate(node)


@router.get("/{node_id}/metrics", response_model=list[MetricPointOut])
def node_metrics(
    node_id: int,
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=1000)] = 60,
) -> list[MetricPointOut]:
    """Recent metric history for one node, oldest first."""
    node = db.get(StorageNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    statement = (
        select(NodeMetric)
        .where(NodeMetric.node_id == node_id)
        .order_by(NodeMetric.recorded_at.desc(), NodeMetric.id.desc())
        .limit(limit)
    )
    rows = list(db.scalars(statement).all())
    rows.reverse()
    return [MetricPointOut.model_validate(row) for row in rows]


@router.delete("/{node_id}", response_model=MessageOut)
def deactivate_node(node_id: int, db: DbSession, user: AdminUser) -> MessageOut:
    """Take a node out of rotation without deleting its stored chunks."""
    node = db.get(StorageNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    node.is_active = False
    node.status = NODE_OFFLINE
    node.last_offline_at = utcnow()
    db.commit()
    return MessageOut(message=f"Node '{node.name}' deactivated")


@router.post("/{node_id}/offline", response_model=NodeOut)
async def simulate_node_failure(node_id: int, db: DbSession, user: AdminUser) -> NodeOut:
    """Make a node fail (flips its outage switch and marks it offline)."""
    node = db.get(StorageNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        await node_client.set_simulated_offline(node.url, True)
    except NodeRequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach node: {exc}") from exc

    node.status = NODE_OFFLINE
    node.consecutive_failures = max(node.consecutive_failures, 1)
    node.last_offline_at = utcnow()
    node.last_error = "simulated failure via API"
    db.commit()
    db.refresh(node)
    return NodeOut.model_validate(node)


@router.post("/{node_id}/online", response_model=NodeOut)
async def recover_node(node_id: int, db: DbSession, user: AdminUser) -> NodeOut:
    """Bring a previously failed node back into service."""
    node = db.get(StorageNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        await node_client.set_simulated_offline(node.url, False)
    except NodeRequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach node: {exc}") from exc

    if node.last_offline_at is not None:
        node.total_downtime_seconds += elapsed_seconds(node.last_offline_at)
        node.last_offline_at = None
    node.status = NODE_ONLINE
    node.is_active = True
    node.consecutive_failures = 0
    node.last_error = None
    db.commit()
    db.refresh(node)
    return NodeOut.model_validate(node)
