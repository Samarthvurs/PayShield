"""Application settings, read from environment variables or the .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration in one typed object. Real environment variables win over .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+psycopg://payshield:payshield@localhost:5433/payshield"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "payshield"
    card_token_secret: str = "change-me-to-a-long-random-string"
    api_key: str = "change-me-api-key"
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"

    model_path: str = "models/fraud_model.joblib"
    data_dir: str = "data/raw"
    test_cards_path: str = "scripts/test_cards.json"

    @property
    def is_dev(self) -> bool:
        """True when dev-only helpers (demo button, test-card list) are enabled."""
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    """Return the settings, loaded once and cached."""
    return Settings()
