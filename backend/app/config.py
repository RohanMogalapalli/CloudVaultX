"""Application settings, loaded from environment / .env file."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the CloudVaultX coordinator."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Basic app -----------------------------------------------------
    app_name: str = "CloudVaultX"
    environment: str = "development"
    api_prefix: str = "/api"

    # --- Database ------------------------------------------------------
    database_url: str = "sqlite:///./data/cloudvaultx.db"

    # --- Auth ----------------------------------------------------------
    # Must be at least 32 bytes for HS256 (PyJWT warns otherwise).
    jwt_secret: str = "dev-jwt-secret-change-me-before-deploying"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24

    # --- Encryption ----------------------------------------------------
    # Base64-encoded 32 byte AES-256 master key. If empty, an insecure
    # key derived from the JWT secret is used so local dev still works.
    master_key: str = ""

    # --- Storage nodes -------------------------------------------------
    # Shared bearer token the coordinator uses to talk to storage nodes.
    node_shared_token: str = "dev-node-token"
    # Comma separated list of storage node base URLs, e.g.
    # "http://node1:9000,http://node2:9000"
    node_urls: str = ""
    node_capacity_bytes: int = 10 * 1024**3
    node_request_timeout_seconds: float = 15.0

    # --- Chunking & replication ---------------------------------------
    chunk_size_bytes: int = 256 * 1024
    replication_factor: int = 3

    # --- Monitoring ----------------------------------------------------
    monitor_enabled: bool = True
    node_poll_interval_seconds: int = 10
    node_offline_after_failures: int = 3
    auto_repair_enabled: bool = True
    metric_history_limit: int = 500

    # --- ML model ------------------------------------------------------
    model_path: str = "./data/model/node_selector.joblib"
    auto_train_model: bool = True
    # Weight blending the Random Forest score with the analytic heuristic.
    ml_weight: float = 0.7

    # --- Administration ------------------------------------------------
    # Comma separated emails that receive administrator privileges at startup.
    # The very first account registered on a fresh deployment is also made an
    # administrator, so someone can always manage the cluster.
    admin_emails: str = ""

    # --- Rebalancing ---------------------------------------------------
    # Prune surplus (and corrupt) replicas once every node is healthy again.
    reconcile_enabled: bool = True
    reconcile_interval_seconds: int = 60

    # --- CORS ----------------------------------------------------------
    cors_origins: str = "*"

    # ------------------------------------------------------------------
    @property
    def node_url_list(self) -> list[str]:
        """Return the configured storage node URLs as a clean list."""
        return [url.strip().rstrip("/") for url in self.node_urls.split(",") if url.strip()]

    @property
    def admin_email_list(self) -> list[str]:
        """Emails that should always be administrators."""
        return [email.strip().lower() for email in self.admin_emails.split(",") if email.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*" or not raw:
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def master_key_bytes(self) -> bytes:
        """Return the 32 byte AES master key."""
        raw = self.master_key.strip()
        if not raw:
            # Deterministic dev fallback so restarts can still decrypt data.
            return hashlib.sha256(f"cloudvaultx-insecure::{self.jwt_secret}".encode()).digest()

        try:
            key = base64.b64decode(raw, validate=True)
        except Exception:  # noqa: BLE001 - fall back to raw text
            key = raw.encode("utf-8")

        if len(key) < 32:
            # Pad short keys instead of crashing so devs can use passphrases.
            key = hashlib.sha256(key).digest()
        return key[:32]

    @property
    def using_ephemeral_master_key(self) -> bool:
        """True when no explicit MASTER_KEY was supplied."""
        return not self.master_key.strip()


def assert_production_ready(settings: Settings) -> None:
    """Refuse to start a production deployment without a real master key.

    The development fallback derives an encryption key from ``JWT_SECRET``,
    which would silently protect stored files with a guessable key. That is
    acceptable for a local demo and never for a real deployment.
    """
    if settings.is_production and settings.using_ephemeral_master_key:
        raise RuntimeError(
            "MASTER_KEY must be set when ENVIRONMENT=production. Generate one with:\n"
            '  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor."""
    return Settings()
