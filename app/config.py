from functools import lru_cache
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_db_url(url: str) -> str:
    """Accept Neon/Heroku-style URLs verbatim: force the asyncpg driver and
    translate libpq-only params (sslmode, channel_binding) to asyncpg's `ssl`."""
    if not url:
        return url
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query))
    if "sslmode" in params or "channel_binding" in params:
        params.pop("sslmode", None)
        params.pop("channel_binding", None)
        params.setdefault("ssl", "require")
    return urlunparse(parsed._replace(query=urlencode(params)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    retell_api_key: str = ""
    openai_api_key: str = ""

    cliniko_api_key: str = ""
    cliniko_vendor_name: str = "clinic-voice-agent"
    cliniko_vendor_email: str = ""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/clinic"
    database_url_direct: str = ""

    @field_validator("database_url", "database_url_direct")
    @classmethod
    def _normalize_urls(cls, value: str) -> str:
        return _normalize_db_url(value)

    app_base_url: str = "http://localhost:8080"
    tool_shared_secret: str = ""
    clinic_tz: str = "Asia/Kolkata"

    retell_agent_id: str = ""
    retell_phone_number: str = ""
    retell_voice_id: str = ""  # optional explicit voice; else auto-picked

    # Twilio: SIP-imported PSTN number (scripts/import_twilio_number.py) and
    # OTP delivery via Twilio Verify (scripts/setup_twilio_verify.py)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_verify_service_sid: str = ""

    # Caller identity verification (OTP to the number on file) before existing
    # appointments may be disclosed or changed. Numbers starting with
    # otp_dev_prefix (demo personas + eval fixtures) use a fixed dev code
    # instead of a real SMS — they are fictional patients on unreachable
    # numbers by design.
    # Ships dark (False): flip REQUIRE_VERIFICATION=true only AFTER the agent
    # version with the send/check_verification_code tools is published —
    # enforcing against an older agent would dead-end existing-appointment
    # flows with no way to verify.
    require_verification: bool = False
    otp_dev_prefix: str = "+919000000"
    otp_dev_code: str = "000000"
    verified_session_ttl_minutes: int = 30

    # Abuse protection (see scripts/hardening_runbook.md)
    # Cloudflare Turnstile gates web-call token minting when configured; unset
    # keys skip the check (local dev) — the server logs that it is off.
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""
    # Emergency stop. The DB flag (scripts/kill_switch.py) is the operative
    # switch — it also unbinds the phone number's agent so PSTN calls actually
    # disconnect; this env flag alone only stops web-call minting + context.
    kill_switch: bool = False
    # Web-call channel is free for callers and costs us Retell credit per
    # minute — cap the daily volume. 0 disables the cap.
    max_web_calls_per_day: int = 60

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
