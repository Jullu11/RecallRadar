from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    keepa_api_key: str | None = None
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None
    spectrum_project_id: str | None = None
    spectrum_project_secret: str | None = None
    backend_internal_token: str | None = None
    public_base_url: str | None = None
    allowed_sender_ids: str = ""
    # When true (or when Supabase is unset), conversation state uses in-memory storage.
    demo_mode: bool = False
    # When true, CPSC network failures fall back to the labeled historical myCharge fixture.
    cpsc_fixture_fallback: bool = True

    @property
    def allowed_sender_id_set(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.allowed_sender_ids.split(",") if item.strip())

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
