from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_workspace_id: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    fmp_api_key: str = ""
    edgar_user_agent: str = "fundabot unknown@example.com"
    database_url: str = "postgresql://fundabot:fundabot@localhost:5433/fundabot"
    fundabot_monthly_budget_usd: float = 100.0
    cache_dir: Path = Path(".cache")


settings = Settings()
