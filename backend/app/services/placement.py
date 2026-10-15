"""AI-driven placement: rank storage nodes and pick replica targets."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ml.node_selector import build_feature_vector, heuristic_score, selector
from ..models import NODE_ONLINE, StorageNode

logger = logging.getLogger(__name__)

# Random jitter added to final scores so placement spreads out over time
# instead of hammering whichever node scored best on the first poll.
EXPLORATION_JITTER = 0.04

# Penalty applied per consecutive failure observed for a node.
FAILURE_PENALTY = 0.15


@dataclass
class PlacementCandidate:
    """A scored node, used both for placement and for the UI explain endpoint."""

    node: StorageNode
    ml_score: float
    heuristic_score: float
    final_score: float
    eligible: bool
    reason: str | None = None


def features_for_node(node: StorageNode) -> list[float]:
    """Build the ML feature vector for a node from its latest metrics."""
    return build_feature_vector(
        free_bytes=node.effective_free_bytes,
        capacity_bytes=node.capacity_bytes,
        cpu_usage=node.cpu_usage,
        ram_usage=node.ram_usage,
        latency_ms=node.latency_ms,
        uptime_ratio=node.uptime_ratio,
        reliability=node.reliability,
        chunk_count=node.chunk_count,
    )


def rank_nodes(
    db: Session,
    *,
    required_bytes: int = 0,
    exclude_node_ids: set[int] | None = None,
    include_offline: bool = False,
) -> list[PlacementCandidate]:
    """Score every active node, best first.

    Nodes that cannot host the chunk (offline, full, excluded) are still
    returned but flagged ineligible so the UI can explain the decision.
    """
    exclude_node_ids = exclude_node_ids or set()
    statement = select(StorageNode).where(StorageNode.is_active.is_(True))
    nodes = list(db.scalars(statement).all())
    if not nodes:
        return []

    feature_matrix = [features_for_node(node) for node in nodes]
    ml_scores = selector.predict_success_probability(feature_matrix)

    candidates: list[PlacementCandidate] = []
    for node, features, ml_score in zip(nodes, feature_matrix, ml_scores, strict=True):
        heuristic = heuristic_score(features)
        score = selector.ml_weight * float(ml_score) + (1 - selector.ml_weight) * heuristic
        score -= FAILURE_PENALTY * node.consecutive_failures

        reason: str | None = None
        eligible = True

        if not include_offline and node.status != NODE_ONLINE:
            eligible, reason = False, f"node status is {node.status}"
        elif node.id in exclude_node_ids:
            eligible, reason = False, "already holds a replica of this chunk"
        elif required_bytes and node.effective_free_bytes < required_bytes:
            eligible, reason = False, "insufficient free space for this chunk"

        score = max(0.0, min(1.0, score))
        candidates.append(
            PlacementCandidate(
                node=node,
                ml_score=float(ml_score),
                heuristic_score=float(heuristic),
                final_score=score,
                eligible=eligible,
                reason=reason,
            )
        )

    # Sort by score with a little jitter for eligible nodes.
    def sort_key(candidate: PlacementCandidate) -> float:
        if not candidate.eligible:
            return -1.0
        return candidate.final_score + random.uniform(0.0, EXPLORATION_JITTER)

    candidates.sort(key=sort_key, reverse=True)
    return candidates


def select_nodes(
    db: Session,
    *,
    count: int,
    required_bytes: int = 0,
    exclude_node_ids: set[int] | None = None,
) -> list[StorageNode]:
    """Pick up to ``count`` distinct eligible nodes, best-scored first."""
    if count <= 0:
        return []
    ranked = rank_nodes(
        db, required_bytes=required_bytes, exclude_node_ids=exclude_node_ids
    )
    return [candidate.node for candidate in ranked if candidate.eligible][:count]


def predict_scores_for_all(db: Session) -> dict[int, float]:
    """Return {node_id: blended score} for every active node (for dashboards)."""
    statement = select(StorageNode).where(StorageNode.is_active.is_(True))
    nodes = list(db.scalars(statement).all())
    if not nodes:
        return {}
    feature_matrix = [features_for_node(node) for node in nodes]
    ml_scores = selector.predict_success_probability(feature_matrix)
    result: dict[int, float] = {}
    for node, features, ml_score in zip(nodes, feature_matrix, ml_scores, strict=True):
        heuristic = heuristic_score(features)
        result[node.id] = max(
            0.0,
            min(1.0, selector.ml_weight * float(ml_score) + (1 - selector.ml_weight) * heuristic),
        )
    return result
