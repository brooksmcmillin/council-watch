from pydantic_settings import BaseSettings


class CityConfig(BaseSettings):
    """Per-city configuration for the CivicPlus source being monitored.

    Everything Campbell-specific lives here so additional CivicPlus cities can
    be added without forking the scraper/summarizer/notifier code — override any
    field via a ``CITY_``-prefixed env var (e.g. ``CITY_BASE_URL``,
    ``CITY_CATEGORY_ID``). Defaults describe the City of Campbell, CA.
    """

    model_config = {
        "env_prefix": "CITY_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # The shared .env holds non-CITY_ keys (ANTHROPIC_API_KEY, SMTP_*, ...)
        # meant for Settings; ignore them here instead of erroring on extras.
        "extra": "ignore",
    }

    # Short name used in email subjects and the bot User-Agent (e.g. "Campbell").
    name: str = "Campbell"
    # City + state as it should read inside the summarizer prompts
    # (e.g. "Campbell, California").
    location: str = "Campbell, California"
    # Human display name for notifications (e.g. "Campbell City Council").
    display_name: str = "Campbell City Council"

    # CivicPlus site root, no trailing slash (e.g. "https://www.campbellca.gov").
    base_url: str = "https://www.campbellca.gov"
    # AgendaCenter category path segment (e.g. "City-Council-10").
    agenda_path: str = "City-Council-10"
    # CivicPlus category id (the ``catID`` used by the backfill endpoint).
    category_id: str = "10"

    # Identifies the bot to the city's servers.
    user_agent: str = (
        "CampbellCouncilMonitor/1.0 (+https://github.com/brooksmcmillin/council-meetings)"
    )

    @property
    def agenda_center_url(self) -> str:
        return f"{self.base_url}/AgendaCenter/{self.agenda_path}"

    @property
    def backfill_url(self) -> str:
        return f"{self.base_url}/AgendaCenter/UpdateCategoryList"


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Required
    anthropic_api_key: str = ""

    # Summarization model (env-overridable via SUMMARIZATION_MODEL)
    summarization_model: str = "claude-sonnet-5"

    # Database
    database_url: str = "sqlite:///data/council.db"
    pdf_storage_dir: str = "data/pdfs"

    # SMTP email (all optional — skip email if blank)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    # Comma-separated static recipients. Optional now that delivery is
    # subscriber-driven; kept as a fallback/admin channel — these addresses
    # always receive notifications (no unsubscribe link).
    email_to: str = ""

    # Bluesky (optional — skip if blank)
    bluesky_handle: str = ""
    bluesky_app_password: str = ""

    # App
    scrape_interval_minutes: int = 60
    app_base_url: str = "http://localhost:8000"

    # Historical backfill: periodically re-scrape prior years so late-posted
    # minutes and older meetings are captured without manual runs.
    backfill_years: int = 2  # number of prior years to re-scrape (0 disables)
    backfill_interval_hours: int = 168  # default weekly

    @property
    def email_enabled(self) -> bool:
        # Recipients come from the subscribers table (plus the optional static
        # email_to list), so the channel is enabled on SMTP config alone.
        return bool(self.smtp_host and self.email_from)

    @property
    def bluesky_enabled(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_app_password)


settings = Settings()
city = CityConfig()
