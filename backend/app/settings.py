from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    spectrum_project_id: str | None = None
    spectrum_project_secret: str | None = None
    backend_internal_token: str | None = None
    public_base_url: str | None = None
    # Public URL for the evidence desk (API + frontend). Prefer this over PUBLIC_BASE_URL,
    # which is often the Spectrum gateway tunnel.
    public_web_url: str | None = None
    # Extra browser origins allowed to call the API (comma-separated), e.g. Vercel URLs.
    cors_origins: str = ""
    allowed_sender_ids: str = ""
    # When true (or when Supabase is unset), conversation state uses in-memory storage.
    demo_mode: bool = True
    # When true, CPSC network failures fall back to the labeled historical myCharge fixture.
    cpsc_fixture_fallback: bool = True

    # Optional Snowflake analytics warehouse (async copy + /api/insights)
    snowflake_account: str | None = None
    snowflake_user: str | None = None
    snowflake_password: str | None = None
    snowflake_warehouse: str | None = None
    snowflake_database: str | None = None
    snowflake_schema: str = "PUBLIC"
    snowflake_role: str | None = None
    snowflake_use_cortex: bool = True
    snowflake_cortex_model: str = "llama3.1-8b"

    @property
    def allowed_sender_id_set(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.allowed_sender_ids.split(",") if item.strip())

    @property
    def allow_all_senders(self) -> bool:
        """True when ALLOWED_SENDER_IDS contains * (public / open demo)."""
        return "*" in self.allowed_sender_id_set

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
