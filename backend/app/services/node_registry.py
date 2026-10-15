"""Node registry: keeping the ``storage_nodes`` table in sync with config."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import NODE_OFFLINE, NODE_ONLINE, StorageNode

logger = logging.getLogger(__name__)

settings = get_settings()


def _name_from_url(url: str, taken: set[str]) -> str:
    """Derive a unique, readable node name from its URL."""
    parsed = urlparse(url)
    base = parsed.hostname or parsed.netloc or url
    base = base.split(".")[0] or "node"
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


def sync_nodes_from_settings(db: Session) -> list[StorageNode]:
    """Register configured node URLs, and deactivate nodes no longer listed."""
    urls = settings.node_url_list
    if not urls:
        return db.scalars(select(StorageNode)).all()

    existing = {node.url: node for node in db.scalars(select(StorageNode)).all()}
    taken_names = {node.name for node in existing.values() if node.url not in urls}

    registered: list[StorageNode] = []
    for url in urls:
        node = existing.get(url)
        if node is None:
            name = _name_from_url(url, taken_names)
            taken_names.add(name)
            node = StorageNode(
                name=name,
                url=url,
                capacity_bytes=settings.node_capacity_bytes,
                status=NODE_ONLINE,
                is_active=True,
            )
            db.add(node)
            db.flush()
            logger.info("Registered new storage node %s at %s", name, url)
        else:
            node.is_active = True
            if node.capacity_bytes <= 0:
                node.capacity_bytes = settings.node_capacity_bytes
            if node.status == NODE_OFFLINE:
                node.status = NODE_ONLINE
        registered.append(node)

    for url, node in existing.items():
        if url not in urls and node.is_active:
            node.is_active = False
            logger.info("Deactivated storage node %s (no longer configured)", node.name)

    db.commit()
    return registered


def list_nodes(db: Session, *, active_only: bool = False) -> list[StorageNode]:
    """Return storage nodes ordered by name."""
    statement = select(StorageNode).order_by(StorageNode.name)
    if active_only:
        statement = statement.where(StorageNode.is_active.is_(True))
    return list(db.scalars(statement).all())


def register_node(
    db: Session,
    *,
    name: str,
    url: str,
    capacity_bytes: int | None = None,
) -> StorageNode:
    """Manually add (or update) a storage node via the API."""
    url = url.rstrip("/")
    node = db.scalar(select(StorageNode).where(StorageNode.url == url))
    if node is None:
        node = db.scalar(select(StorageNode).where(StorageNode.name == name))
    if node is None:
        node = StorageNode(
            name=name,
            url=url,
            capacity_bytes=capacity_bytes or settings.node_capacity_bytes,
            status=NODE_ONLINE,
            is_active=True,
        )
        db.add(node)
    else:
        node.name = name
        node.url = url
        node.is_active = True
        node.status = NODE_ONLINE
        if capacity_bytes:
            node.capacity_bytes = capacity_bytes
    db.commit()
    db.refresh(node)
    return node
