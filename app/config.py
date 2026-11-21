from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_app_id: str = ""
    plivo_mock: bool = True
    plivo_api_base: str = "https://api.plivo.com/v1/Account"

    # Plivo's documented reseller flow: one subaccount per customer; KYC + numbers live in it.
    plivo_use_subaccounts: bool = True

    country_iso: str = "IN"
    # India: landline (080/022...) is the self-serve type. 140/160-series are provisioned manually by Plivo.
    allowed_number_types: str = "local"
    # "all": every document type in Plivo's Requirements response (Compliance API spec: India needs
    #        Registration Certificate AND GST Certificate). "one": exactly one of them (console guide wording).
    kyc_documents_mode: str = "all"

    public_base_url: str = ""
    database_url: str = "sqlite:///./plivo_numbers.db"
    jwt_secret: str = "dev-secret-change-me"
    admin_api_key: str = "dev-admin-key"
    dev_mode: bool = False

    max_numbers_per_client: int = 10
    compliance_poll_seconds: int = 300
    # Re-check active numbers against Plivo (catches numbers deleted in the Plivo console). 0 = off.
    number_sync_seconds: int = 900
    price_markup: float = 1.0
    # Cities offered in the "Buy numbers" dropdown, spelled exactly as Plivo spells them.
    # As of Nov 2026 Plivo only has India landline stock in these two.
    number_cities: str = "Bangalore,Mumbai"

    # ---- Integration API (/v1) for your own backend ----
    # Header X-API-Key. Empty = the /v1 API is disabled.
    integration_api_key: str = ""
    # Where to POST event notifications (kyc.accepted, number.purchased, ...). Empty = no webhooks.
    webhook_url: str = ""
    # Shared secret used to sign webhook bodies (HMAC-SHA256). Required when webhook_url is set.
    webhook_secret: str = ""
    webhook_poll_seconds: int = 5

    # Browser origins allowed to call this API (CORS), comma-separated, e.g.
    # "https://app.bigship.direct,http://localhost:4200". Empty = CORS off (server-to-server only).
    allowed_origins: str = ""

    # ---- Wallet billing: deduct number rental from the seller's Bigship wallet ----
    # Off = no wallet calls at all (purchases are not checked or charged).
    wallet_billing_enabled: bool = False
    # Price per number per billing period, in rupees, before GST. GST is added on top.
    number_monthly_price_inr: float = 300.0
    number_gst_percent: float = 18.0
    billing_period_days: int = 30
    # Days a renewal may stay unpaid (retried daily) before the number is taken off the dialer.
    billing_grace_days: int = 3
    # Days unpaid after which the number is RELEASED on Plivo (irreversible). 0 = never auto-release.
    billing_release_after_days: int = 0
    # Seconds between runs of the wallet charge loop.
    wallet_poll_seconds: int = 30
    # Same Bigship wallet API and login as AICall (BIGSHIP_WALLET_* in ~/AICall/bigagent/.env).
    bigship_wallet_base_url: str = "https://appapi.bigship.direct"
    bigship_wallet_email: str = ""
    bigship_wallet_password: str = ""
    wallet_http_timeout_seconds: float = 10.0

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def cities(self) -> list[str]:
        return [c.strip() for c in self.number_cities.split(",") if c.strip()]

    @property
    def number_types(self) -> list[str]:
        return [t.strip() for t in self.allowed_number_types.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
