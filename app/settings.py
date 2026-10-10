from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql://gateai:gateai@localhost:5432/gateai"
    database_schema: str = "gateai"
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "auto"
    asset_allowed_hosts: str = "fal.media"
    asset_max_bytes: int = 104857600
    redis_url: str = "redis://localhost:6379/0"
    admin_key: str
    litellm_url: str = "http://litellm:4000"
    litellm_master_key: str
    openrouter_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    fal_key: str = ""
    runpod_api_key: str = ""
    cloudflare_worker_url: str = ""
    cloudflare_worker_token: str = ""
    global_daily_cap_micros: int = 20_000_000
    routes_file: str = "config/routes.yaml"


settings = Settings()
