"""Tests for the Random Forest node-selection model."""

from __future__ import annotations

import numpy as np
import pytest

from app.ml.node_selector import (
    FEATURE_NAMES,
    NodeSelector,
    build_feature_vector,
    ground_truth_success_probability,
    heuristic_score,
)

HEALTHY = [0.9, 0.05, 0.2, 5.0, 1.0, 1.0, 0.0]
STRESSED = [0.02, 0.95, 0.95, 600.0, 0.55, 0.35, 0.98]


@pytest.fixture(scope="module")
def trained_selector(tmp_path_factory) -> NodeSelector:
    selector = NodeSelector(model_path=str(tmp_path_factory.mktemp("ml") / "model.joblib"))
    selector.train(n_samples=2000, n_estimators=80)
    return selector


def test_build_feature_vector_orders_features():
    vector = build_feature_vector(
        free_bytes=50,
        capacity_bytes=100,
        cpu_usage=0.5,
        ram_usage=1.5,  # clamps to 1.0
        latency_ms=-3,  # clamps to 0
        uptime_ratio=0.8,
        reliability=0.9,
        chunk_count=2500,
    )
    assert len(vector) == len(FEATURE_NAMES)
    assert vector[0] == pytest.approx(0.5)
    assert vector[2] == 1.0
    assert vector[3] == 0.0
    assert vector[6] == pytest.approx(0.5)


def test_build_feature_vector_without_capacity_is_safe():
    vector = build_feature_vector(
        free_bytes=10,
        capacity_bytes=0,
        cpu_usage=0.1,
        ram_usage=0.1,
        latency_ms=10,
        uptime_ratio=1.0,
        reliability=1.0,
        chunk_count=0,
    )
    assert vector[0] == 0.0


def test_heuristic_prefers_healthy_node():
    assert heuristic_score(HEALTHY) > heuristic_score(STRESSED)
    assert 0.0 <= heuristic_score(STRESSED) <= 1.0
    assert 0.0 <= heuristic_score(HEALTHY) <= 1.0


def test_training_produces_a_good_model(trained_selector: NodeSelector):
    assert trained_selector.is_trained
    metadata = trained_selector.metadata
    assert metadata.accuracy is not None and metadata.accuracy > 0.8
    assert metadata.roc_auc is not None and metadata.roc_auc > 0.85
    assert metadata.n_samples == 2000
    assert set(metadata.feature_importances) == set(FEATURE_NAMES)
    assert sum(metadata.feature_importances.values()) == pytest.approx(1.0, abs=1e-6)


def test_model_ranks_healthy_node_above_stressed_node(trained_selector: NodeSelector):
    scores = trained_selector.score_nodes([HEALTHY, STRESSED])
    assert scores[0] > scores[1]
    assert scores[0] > 0.7
    assert scores[1] < 0.3


def test_model_predicts_probabilities(trained_selector: NodeSelector):
    probabilities = trained_selector.predict_success_probability([HEALTHY, STRESSED])
    assert probabilities.shape == (2,)
    assert all(0.0 <= value <= 1.0 for value in probabilities)


def test_model_round_trips_through_disk(tmp_path):
    path = tmp_path / "selector.joblib"
    selector = NodeSelector(model_path=str(path))
    selector.train(n_samples=1000, n_estimators=40)
    selector.save()
    assert path.exists()

    reloaded = NodeSelector(model_path=str(path))
    assert reloaded.load()
    assert reloaded.metadata.accuracy == pytest.approx(selector.metadata.accuracy)

    original = selector.predict_success_probability([HEALTHY])
    restored = reloaded.predict_success_probability([HEALTHY])
    assert restored[0] == pytest.approx(original[0], abs=1e-9)


def test_untrained_selector_falls_back_to_heuristic(tmp_path):
    selector = NodeSelector(model_path=str(tmp_path / "missing.joblib"))
    assert not selector.load()
    assert not selector.is_trained
    scores = selector.score_nodes([HEALTHY, STRESSED])
    assert scores[0] == pytest.approx(heuristic_score(HEALTHY))
    assert scores[0] > scores[1]


def test_load_returns_false_for_missing_file(tmp_path):
    assert NodeSelector(model_path=str(tmp_path / "nope.joblib")).load() is False


def test_ground_truth_is_monotonic_in_free_space():
    base = np.array([[0.1, 0.2, 0.2, 20.0, 1.0, 1.0, 0.0]])
    richer = np.array([[0.9, 0.2, 0.2, 20.0, 1.0, 1.0, 0.0]])
    assert ground_truth_success_probability(richer)[0] > ground_truth_success_probability(base)[0]


def test_ground_truth_captures_crowding_interaction():
    """A full+loaded node should score below the pure linear expectation."""
    full_and_loaded = np.array([[0.05, 0.2, 0.2, 20.0, 1.0, 1.0, 1.0]])
    full_only = np.array([[0.05, 0.2, 0.2, 20.0, 1.0, 1.0, 0.0]])
    probability_loaded = ground_truth_success_probability(full_and_loaded)[0]
    probability_idle = ground_truth_success_probability(full_only)[0]
    assert probability_loaded < probability_idle
