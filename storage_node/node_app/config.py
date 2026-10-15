"""Storage node settings (env driven, no external config files needed)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class NodeSettings(BaseSettings):
    """Configuration for a single storage node container.

    Environment variables: ``NODE_NAME``, ``DATA_DIR``, ``SHARED_TOKEN``,
    ``CAPACITY_BYTES``.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    node_name: str = "node"
    data_dir: str = "./node_data"
    shared_token: str = "dev-node-token"
    capacity_bytes: int = 10 * 1024**3

    @property
    def chunk_dir(self) -> str:
        return f"{self.data_dir.rstrip('/')}/chunks"


@lru_cache
def get_node_settings() -> NodeSettings:
    return NodeSettings()
