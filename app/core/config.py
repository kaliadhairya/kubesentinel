import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration settings for KubeSentinel."""

    APP_ENV: str = "development"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = True

    # Kubernetes Engine
    K8S_MOCK_MODE: bool = True
    KUBECONFIG_PATH: str = os.getenv("KUBECONFIG", "")
    GUARDRAIL_ALLOWED_NAMESPACES: str = "default,production,staging,monitoring"

    # Prometheus Engine
    PROMETHEUS_MOCK_MODE: bool = True
    PROMETHEUS_URL: str = "http://localhost:9090"

    # Database & Storage
    DATABASE_URL: str = "postgresql://kubesentinel:sentinel_secret@localhost:5438/kubesentinel_db"
    CLUSTER_ID: str = "default-cluster"
    ALERTMANAGER_HMAC_SECRET: str = "sentinel-hmac-secret-dev"

    # Intelligence & LLM Provider
    LLM_PROVIDER: str = "mock"  # "mock", "gemini", "openai"
    LLM_MODEL: str = "gemini-2.5-flash"
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    CASSETTE_MODE: str = "replay"  # "replay", "record", "live"
    CASSETTES_DIR: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "tests",
        "fixtures",
        "cassettes",
    )

    # Knowledge & Runbooks
    RUNBOOKS_DIR: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data",
        "runbooks",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_namespaces(self) -> List[str]:
        return [ns.strip() for ns in self.GUARDRAIL_ALLOWED_NAMESPACES.split(",") if ns.strip()]


settings = Settings()
