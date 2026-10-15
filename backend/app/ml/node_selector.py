"""Random Forest model that ranks storage nodes by predicted reliability.

Each storage node is described by seven features (free space, CPU, RAM,
network latency, uptime, historical reliability and current load).  A
Random Forest classifier is trained on node operating conditions to
predict the probability that a chunk write/read against that node will
succeed quickly.  Those probabilities are then blended with an analytic
heuristic so placement stays sensible even before the model is trained.

Why a Random Forest rather than a pure formula?  The synthetic ground
truth used for training encodes *interactions* between metrics (a nearly
full node that is also heavily loaded fails far more often than either
signal alone suggests).  The forest learns those interactions and
produces a calibrated success probability we can rank nodes with,
instead of a single hand-tuned linear score.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split

logger = logging.getLogger(__name__)

FEATURE_NAMES: list[str] = [
    "free_space_ratio",
    "cpu_usage",
    "ram_usage",
    "latency_ms",
    "uptime_ratio",
    "reliability",
    "load_ratio",
]

# Load is expressed relative to this many stored chunks per node.
NOMINAL_CHUNKS_PER_NODE = 5000

# Centre and steepness of the latent success curve.  The threshold sits at
# the median of the simulated operating conditions, so roughly half of the
# sampled nodes succeed - which keeps the classes balanced and the sampled
# labels close to deterministic.
LATENT_THRESHOLD = 0.5889
LATENT_SLOPE = 48.0

# Weights of the analytic heuristic (sum to 1.0 before penalties).
_HEURISTIC_WEIGHTS = {
    "free_space_ratio": 0.30,
    "cpu_idle": 0.20,
    "ram_idle": 0.12,
    "latency": 0.18,
    "uptime_ratio": 0.10,
    "reliability": 0.10,
}
_LOAD_PENALTY = 0.08


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def latency_quality(latency_ms: float) -> float:
    """Map latency in ms to a 0..1 quality score (fast = 1)."""
    return float(math.exp(-max(0.0, latency_ms) / 250.0))


def build_feature_vector(
    *,
    free_bytes: int,
    capacity_bytes: int,
    cpu_usage: float,
    ram_usage: float,
    latency_ms: float,
    uptime_ratio: float,
    reliability: float,
    chunk_count: int,
) -> list[float]:
    """Build the ordered feature vector consumed by the model."""
    if capacity_bytes > 0:
        free_space_ratio = _clip01(free_bytes / capacity_bytes)
    else:
        free_space_ratio = 0.0
    load_ratio = _clip01(chunk_count / NOMINAL_CHUNKS_PER_NODE)
    return [
        free_space_ratio,
        _clip01(cpu_usage),
        _clip01(ram_usage),
        max(0.0, float(latency_ms)),
        _clip01(uptime_ratio),
        _clip01(reliability),
        load_ratio,
    ]


def heuristic_score(features: list[float] | np.ndarray) -> float:
    """Transparent analytic score in 0..1, used as ML fallback and prior."""
    free, cpu, ram, latency, uptime, reliability, load = (float(v) for v in features)
    score = (
        _HEURISTIC_WEIGHTS["free_space_ratio"] * free
        + _HEURISTIC_WEIGHTS["cpu_idle"] * (1.0 - cpu)
        + _HEURISTIC_WEIGHTS["ram_idle"] * (1.0 - ram)
        + _HEURISTIC_WEIGHTS["latency"] * latency_quality(latency)
        + _HEURISTIC_WEIGHTS["uptime_ratio"] * uptime
        + _HEURISTIC_WEIGHTS["reliability"] * reliability
        - _LOAD_PENALTY * load
    )
    return _clip01(score)


def ground_truth_success_probability(x: np.ndarray) -> np.ndarray:
    """Latent success probability used to label synthetic training rows.

    Deliberately includes non-linear interactions that the heuristic
    above does not model, so the forest has something real to learn.
    """
    free, cpu, ram, latency, uptime, reliability, load = (x[:, i] for i in range(x.shape[1]))
    latency_q = np.exp(-latency / 250.0)

    base = (
        _HEURISTIC_WEIGHTS["free_space_ratio"] * free
        + _HEURISTIC_WEIGHTS["cpu_idle"] * (1.0 - cpu)
        + _HEURISTIC_WEIGHTS["ram_idle"] * (1.0 - ram)
        + _HEURISTIC_WEIGHTS["latency"] * latency_q
        + _HEURISTIC_WEIGHTS["uptime_ratio"] * uptime
        + _HEURISTIC_WEIGHTS["reliability"] * reliability
    )

    # Crowding: a loaded node that is also nearly full degrades sharply.
    crowding = 0.30 * np.clip(load - 0.5, 0.0, 1.0) * np.clip(1.0 - free, 0.0, 1.0)
    # Saturated CPU combined with slow network multiplies timeouts.
    saturation = 0.12 * np.clip(cpu - 0.8, 0.0, 1.0) * np.clip(1.0 - latency_q, 0.0, 1.0)
    # A very reliable node with memory pressure still mostly succeeds.
    resilience = 0.06 * reliability * np.clip(ram - 0.8, 0.0, 1.0)

    margin = base - crowding - saturation + resilience - LATENT_THRESHOLD
    # A steep logistic keeps the sampled labels close to deterministic:
    # a node with a healthy margin almost always succeeds, which is what
    # makes the learned success probability worth ranking on.
    return 1.0 / (1.0 + np.exp(-LATENT_SLOPE * margin))


def generate_synthetic_dataset(
    n_samples: int = 6000, random_state: int = 42
) -> tuple[np.ndarray, np.ndarray]:
    """Sample node operating conditions and label them with the latent model."""
    rng = np.random.default_rng(random_state)

    # Deliberately wide, roughly symmetric distributions: a synthetic node
    # population where excellent and failing nodes are both well represented.
    # That spread - not a lopsided sample - is what lets the forest learn a
    # decision boundary that generalises.
    free = rng.beta(1.6, 1.6, n_samples)
    cpu = rng.beta(1.5, 1.5, n_samples)
    ram = rng.beta(1.5, 1.5, n_samples)
    latency = np.clip(rng.lognormal(mean=3.6, sigma=1.1, size=n_samples), 1.0, 1500.0)
    uptime = rng.beta(5.0, 1.5, n_samples)
    reliability = rng.beta(3.0, 1.5, n_samples)
    load = rng.beta(1.4, 1.8, n_samples)

    features = np.column_stack([free, cpu, ram, latency, uptime, reliability, load])
    probabilities = ground_truth_success_probability(features)
    labels = rng.binomial(1, probabilities).astype(int)
    return features, labels


@dataclass
class ModelMetadata:
    """Everything we know about the currently loaded model."""

    trained: bool = False
    trained_at: datetime | None = None
    n_samples: int = 0
    n_estimators: int = 0
    max_depth: int | None = None
    accuracy: float | None = None
    roc_auc: float | None = None
    feature_names: list[str] = field(default_factory=lambda: list(FEATURE_NAMES))
    feature_importances: dict[str, float] = field(default_factory=dict)
    model_path: str = ""


class NodeSelector:
    """Wraps the Random Forest plus its metadata and persistence."""

    def __init__(self, model_path: str, ml_weight: float = 0.7) -> None:
        self.model_path = Path(model_path)
        self.ml_weight = _clip01(ml_weight)
        self._model: RandomForestClassifier | None = None
        self._metadata = ModelMetadata(model_path=str(self.model_path))
        self._lock = threading.Lock()

    # -- properties ----------------------------------------------------
    @property
    def is_trained(self) -> bool:
        return self._model is not None

    @property
    def metadata(self) -> ModelMetadata:
        return self._metadata

    # -- persistence ---------------------------------------------------
    def load(self) -> bool:
        """Load a previously saved model, returning False if unavailable."""
        if not self.model_path.exists():
            return False
        try:
            payload = joblib.load(self.model_path)
            model = payload["model"]
            metadata: ModelMetadata = payload.get("metadata", ModelMetadata())
            metadata.feature_names = list(FEATURE_NAMES)
            metadata.model_path = str(self.model_path)
            with self._lock:
                self._model = model
                self._metadata = metadata
            logger.info("Loaded node-selection model from %s", self.model_path)
            return True
        except Exception:  # noqa: BLE001 - corrupt cache should not crash startup
            logger.exception("Could not load node-selection model from %s", self.model_path)
            return False

    def save(self) -> None:
        """Persist the model and its metadata to disk."""
        with self._lock:
            if self._model is None:
                return
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(
                {"model": self._model, "metadata": self._metadata},
                self.model_path,
            )

    # -- training ------------------------------------------------------
    def train(
        self,
        n_samples: int = 6000,
        n_estimators: int = 200,
        max_depth: int | None = 14,
        random_state: int = 42,
    ) -> ModelMetadata:
        """Train the Random Forest on freshly generated synthetic data."""
        features, labels = generate_synthetic_dataset(n_samples, random_state)

        x_train, x_test, y_train, y_test = train_test_split(
            features, labels, test_size=0.25, random_state=random_state, stratify=labels
        )

        model = RandomForestClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            min_samples_leaf=4,
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=random_state,
        )
        model.fit(x_train, y_train)

        predictions = model.predict(x_test)
        probabilities = model.predict_proba(x_test)[:, 1]
        accuracy = float(accuracy_score(y_test, predictions))
        try:
            roc_auc = float(roc_auc_score(y_test, probabilities))
        except ValueError:
            roc_auc = None

        importances = {
            name: float(importance)
            for name, importance in zip(FEATURE_NAMES, model.feature_importances_, strict=True)
        }

        metadata = ModelMetadata(
            trained=True,
            trained_at=datetime.now(timezone.utc),
            n_samples=int(len(features)),
            n_estimators=n_estimators,
            max_depth=max_depth,
            accuracy=accuracy,
            roc_auc=roc_auc,
            feature_names=list(FEATURE_NAMES),
            feature_importances=importances,
            model_path=str(self.model_path),
        )

        with self._lock:
            self._model = model
            self._metadata = metadata

        logger.info(
            "Trained node-selection Random Forest: accuracy=%.4f roc_auc=%s samples=%d",
            accuracy,
            f"{roc_auc:.4f}" if roc_auc is not None else "n/a",
            len(features),
        )
        return metadata

    def train_and_save(self, **kwargs) -> ModelMetadata:
        metadata = self.train(**kwargs)
        self.save()
        return metadata

    # -- inference -----------------------------------------------------
    def predict_success_probability(self, features: list[list[float]]) -> np.ndarray:
        """Predicted success probability for a batch of node feature vectors."""
        matrix = np.asarray(features, dtype=float)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        with self._lock:
            model = self._model
        if model is None:
            return np.array([heuristic_score(row) for row in matrix], dtype=float)
        return model.predict_proba(matrix)[:, 1]

    def score_nodes(self, features: list[list[float]]) -> list[float]:
        """Blend ML probabilities with the analytic heuristic."""
        if not features:
            return []
        ml_scores = self.predict_success_probability(features)
        blended = []
        for row, ml_score in zip(features, ml_scores, strict=True):
            heuristic = heuristic_score(row)
            if self.is_trained:
                blended.append(_clip01(self.ml_weight * float(ml_score) + (1 - self.ml_weight) * heuristic))
            else:
                blended.append(heuristic)
        return blended


# Module-level singleton used by the API layer.
selector = NodeSelector(
    model_path="",  # replaced by configure() during startup
)


def configure(model_path: str, ml_weight: float = 0.7, auto_train: bool = True) -> bool:
    """Point the singleton at a model path, loading or training as needed."""
    selector.model_path = Path(model_path)
    selector.ml_weight = _clip01(ml_weight)
    selector.metadata.model_path = str(selector.model_path)

    if selector.load():
        return True
    if auto_train:
        selector.train_and_save()
        return True
    return False
