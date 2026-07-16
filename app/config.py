from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    retell_api_key: str = ""
    openai_api_key: str = ""

    cliniko_api_key: str = ""
    cliniko_vendor_name: str = "clinic-voice-agent"
    cliniko_vendor_email: str = ""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/clinic"
    database_url_direct: str = ""

    app_base_url: str = "http://localhost:8080"
    tool_shared_secret: str = ""
    clinic_tz: str = "Asia/Kolkata"

    retell_agent_id: str = ""
    retell_phone_number: str = ""

    # Dropped-call resume window
    session_resume_ttl_minutes: int = 15

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_tz)

    @property
    def cliniko_shard(self) -> str:
        # Cliniko API keys end with their shard, e.g. "...-au1"
        return self.cliniko_api_key.rsplit("-", 1)[-1] if "-" in self.cliniko_api_key else "au1"

    @property
    def cliniko_base_url(self) -> str:
        return f"https://api.{self.cliniko_shard}.cliniko.com/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
